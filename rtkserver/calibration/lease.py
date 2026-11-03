# -*- coding: utf-8 -*-
"""串口租约：把串口从主程序数据面手里「借」过来，用完必须还（方案 §3）。

为什么必须有这一层（而不是"打开串口前先 sleep 一下"）：
    Linux 的 tty **不排他**，两个 fd 同时 open 同一个口不会报错，只会
    **随机瓜分字节流** —— 主程序读到半条 RTCM、标定读到半条应答，两边都
    表现为"偶发解析失败"，且日志里看不出任何异常。所以必须先让数据面
    主动退出（关掉 fd），再独占打开，用完再还给主程序。

    「让位 → 独占 → 归还」的时序全部收敛在本模块：

        acquire()   前置校验 → 写租约 → 置 gate → 唤醒主循环 → 等 worker 退出（§3.2 1~5）
        open()      反复尝试打开 → 清残帧 → 置 held → 首次心跳            （§3.2 6~8）
        release()   关 fd → 落终态 → **先清 gate 再置 restart** → 数据面恢复（§3.3）

两条铁律（写反了现场表现极难查，所以在这里写死）：

1. `release()` 里 `hold.clear()` 必须在 `restart_event.set()` **之前**。
   顺序反了：主循环看到 gate 还置着就跳过这一轮，等 gate 清了，restart 却还挂着，
   `run_once()` 一起来就立刻退出 —— 数据面进入"起来即退出"的死循环，
   用户看到的是"标定完了但一直不转发数据"。

2. 置 hold 的同一段代码里**必须**写 `state.calibration_beat = time.time()`。
   心跳初值是 0.0，漏写会让看门狗在 1 秒内判定"标定线程已死"并强制回收租约。

心跳的两级设计（方案 §3.4）：
    内存 `state.calibration_beat` —— 由**标定工作线程**推进，每 2 秒一次。
        刻意不开独立心跳线程：独立线程会在工作线程卡死时继续喂心跳，
        看门狗就永远不触发，等于白做。
    磁盘 `lease.json.heartbeat` —— 每 5 秒刷一次。用途只是"进程被杀后还能知道
        上次活到什么时刻"，不需要秒级精度，少写几次 SD 卡更划算。
"""
import logging
import os
import time

from ..core import state
from ..version import __version__
from . import workspace
from .drivers.base import Channel

# ==================== 时序常量（方案 §3.2 / §3.3） ====================
YIELD_TIMEOUT = 8.0          # 等数据源线程退出的上限：read timeout 1s + join 5s + 2s 余量
YIELD_POLL = 0.1
OPEN_ATTEMPTS = 10           # 打开串口重试次数（每次 200ms，合计 2s）
OPEN_INTERVAL = 0.2
DRAIN_SECONDS = 0.3          # 清残帧窗口
HEARTBEAT_INTERVAL = 2.0     # 内存心跳建议间隔（由调用方节奏决定，这里只做说明性常量）
HEARTBEAT_DISK_INTERVAL = 5.0

STAGE_TERMINAL = ('DONE', 'FAILED', 'ABORTED', 'INTERRUPTED')


class LeaseError(Exception):
    """租约申请被拒绝或申请失败。消息直接给用户看，必须能指出下一步动作。"""


# ==================== 磁盘态读取（页面 / 启动钩子共用） ====================
def read_lease():
    """读 `lease.json`，不存在或损坏返回 None。"""
    return workspace.read_json(workspace.lease_path())


def is_terminal(stage):
    return str(stage or '').upper() in STAGE_TERMINAL


def pid_alive(pid):
    """判断进程是否还活着。Windows 上**不能**用 `os.kill(pid, 0)`。

    在 Windows 上 `os.kill(pid, 0)` 走的是 TerminateProcess —— 那是真的去杀进程，
    不是探活。这里用 OpenProcess + GetExitCodeProcess（259 = STILL_ACTIVE）。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:                                       # noqa: BLE001 - 探活失败按"不活"处理
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def leftover_lease():
    """返回"上次被硬中断"的租约（非终态且持有者已不在），否则 None。

    这是方案 §3.5 的判定内核：断电/`kill -9` 之后 `lease.json` 一定停在
    某个中间阶段，而 pid 已经不存在了。此时**不做任何自动修复**（串口还没
    被打开，任何"自动改模块"都是盲操作），只把事实交给上层去提示用户。
    """
    lease = read_lease()
    if not lease:
        return None
    if is_terminal(lease.get('stage')):
        return None
    pid = lease.get('pid')
    if pid and int(pid) != os.getpid() and pid_alive(pid):
        return None                      # 别的进程真的还在跑，不是残留
    return lease


# ==================== 租约本体 ====================
class SerialLease:
    """一次标定独占一个串口的全生命周期。

    用法（**必须**放在 try/finally 最外层，见方案 §3.3）：

        lease = SerialLease(port='/dev/ttyUSB0', baud=115200)
        try:
            ok, msg = lease.acquire()
            if not ok:
                return
            ok, msg = lease.open()
            ...
        finally:
            lease.release(stage)     # 幂等，重复调用无副作用
    """

    def __init__(self, port, baud=115200, task_id=''):
        self.port = str(port or '')
        self.baud = int(baud) if baud else 115200
        self.task_id = str(task_id or '')
        self.stage = 'IDLE'
        self.channel = None
        self.driver = None                  # 归还时若要恢复模式，需要它
        self.restore_mode_to = ''           # 非空 = 归还前尽力切回该模式（P0 恒为空）
        self.held = False
        self.acquired_at = 0.0
        self.last_beat = 0.0
        self.module = {}                    # model / firmware / sn，写进 lease.json 供中断后提示
        self._released = False
        self._last_disk_beat = 0.0
        self._started_at = time.strftime('%Y-%m-%d %H:%M:%S')

    # ---------- 对外只读属性 ----------
    @property
    def is_held(self):
        return bool(self.held)

    def snapshot(self):
        """给 Web 页面看的租约快照（不落盘）。"""
        return {
            'task_id': self.task_id,
            'port': self.port,
            'baud': self.baud,
            'stage': self.stage,
            'held': self.held,
            'acquired_at': self.acquired_at,
            'held_seconds': int(time.time() - self.acquired_at) if self.acquired_at else 0,
            'module': dict(self.module),
        }

    def set_module(self, **info):
        """记录模块标识：中断后页面还能告诉用户"上次是在哪块模块上操作的"。"""
        for key in ('model', 'firmware', 'sn'):
            if info.get(key):
                self.module[key] = info[key]
        self._write_lease_safe()

    # ---------- 一、让位（§3.2 第 1~5 步） ----------
    def acquire(self):
        """申请独占：校验 → 写租约 → 置 gate → 等数据面退出。

        返回 `(success, message)`。失败时**不残留任何状态**（gate 已清、租约已落终态），
        调用方不需要再做兜底。
        """
        if self.held:
            return True, '租约已持有'
        try:
            self._precheck()
        except LeaseError as e:
            return False, str(e)

        self.stage = 'PREPARE'
        try:
            workspace.write_json(workspace.lease_path(), self._lease_payload())
        except OSError as e:
            return False, f'写租约文件失败：{e}（检查磁盘空间与 {workspace.STATE_DIR} 的权限）'

        # gate 与心跳必须一起写：心跳初值 0.0，漏写会被看门狗当成"线程已死"
        state.calibration_beat = time.time()
        state.calibration_hold.set()
        # 唤醒主循环：不用等它自己那 1 秒醒来，也不等 run_once 里的 10 秒轮询
        state.restart_event.set()

        if not self._wait_dataplane_yield():
            self._drop_gate()
            self._write_terminal('FAILED', '数据面未能让出串口')
            return False, ('数据源线程仍在读取串口，8 秒内没有退出。'
                           '请稍后重试；若反复出现请重启服务（此时模块可能正在做波特率扫描）')

        self.stage = 'HOLD'
        self.beat(force=True)
        logging.info(f'标定已取得 {self.port} 的独占权（数据面已让位）')
        return True, f'数据面已让位，{self.port} 可独占'

    def _precheck(self):
        """§3.2 第 1 步：全部满足才动手，任何一条不满足都不改状态。"""
        if not self.port:
            raise LeaseError('没有指定串口')
        if state.calibration_hold.is_set():
            raise LeaseError('已有标定任务正在进行（串口锁未释放），请等它结束或先取消')

        # 残留租约：另一个进程还活着就不许抢（同机跑两个实例的经典事故）
        leftover = leftover_lease()
        if leftover and leftover.get('pid') and int(leftover['pid']) != os.getpid():
            raise LeaseError(
                f'进程 {leftover["pid"]} 正在使用 {leftover.get("port")} 标定'
                f'（阶段 {leftover.get("stage")}），请先结束那个进程')

        # 数传同口：SerialBridge 会一直重连同一个口，数据面恢复后立刻把口抢回去
        cfg = {}
        try:
            from ..core.config import get_config
            cfg = get_config() or {}
        except Exception as e:                      # 读配置失败不该拦住标定
            logging.debug(f'读取配置失败（跳过数传同口校验）：{e}')
        radio_port = str(cfg.get('radio_port') or '').strip()
        radio_on = str(cfg.get('radio_enabled', '0')) == '1'
        if radio_port and radio_port == self.port and radio_on:
            raise LeaseError(
                f'数传出口正在使用 {self.port}。标定期间它会被停掉，但数据面一恢复就会把口抢回去，'
                f'标定结果会被打断。请先在配置页把数传口改掉或关掉数传')

    def _wait_dataplane_yield(self):
        """§3.2 第 5 步：等 `run_once` 的 finally 把数据源线程 join 掉。

        注意 `worker_thread` 被置 None 之后，run_once 还要走完"停出口、关 ser"，
        所以 open() 那边必须有重试，不能只看这一个信号。
        """
        deadline = time.time() + YIELD_TIMEOUT
        while time.time() < deadline:
            if state.shutdown_event.is_set():
                return False
            worker = state.worker_thread
            if worker is None or not worker.is_alive():
                return True
            self.beat()                              # 让位期间也要喂心跳，否则 30 秒被误杀
            time.sleep(YIELD_POLL)
        return False

    # ---------- 二、独占打开（§3.2 第 6~8 步） ----------
    def open(self):
        """打开串口并清掉残帧。返回 `(success, message)`。"""
        if self.channel is not None and self.channel.is_open:
            return True, '串口已打开'

        if self.channel is None:
            self.channel = Channel(port=self.port, baud=self.baud)

        last_error = ''
        for attempt in range(OPEN_ATTEMPTS):
            try:
                self.channel.open()
                break
            except Exception as e:                   # noqa: BLE001 - 串口异常种类极杂
                last_error = f'{type(e).__name__}: {e}'
                logging.debug(f'打开 {self.port} 第 {attempt + 1}/{OPEN_ATTEMPTS} 次失败：{e}')
                self.beat()
                time.sleep(OPEN_INTERVAL)
        else:
            hint = self._open_hint(last_error)
            self._drop_gate()
            self._write_terminal('FAILED', f'打开串口失败：{last_error}')
            self.channel = None
            return False, f'打开 {self.port} 失败（重试 {OPEN_ATTEMPTS} 次）：{last_error}{hint}'

        # 清残帧：旧 fd 关闭时内核缓冲里可能还留着半帧，带着半帧去识别会把
        # 第一条 `$PQTMVERNO` 当成噪声丢掉。只清 300ms，别按长时间窗口清。
        self.channel.drain()
        self.channel.flush(DRAIN_SECONDS)

        self.held = True
        self.acquired_at = time.time()
        self.stage = 'OPEN'
        self.beat(force=True)
        logging.info(f'标定通道已独占：{self.port} @ {self.baud}')
        return True, f'{self.port} 已独占打开'

    @staticmethod
    def _open_hint(error_text):
        """把 errno 翻译成用户能照做的动作（方案 §3.2 第 6 步的失败提示）。"""
        text = str(error_text).lower()
        if 'permission' in text or 'access is denied' in text or 'errno 13' in text:
            return '。当前用户没有串口权限，把账号加入 dialout 组后重新登录（sudo usermod -aG dialout $USER）'
        if 'busy' in text or 'being used' in text or 'errno 16' in text:
            return '。串口仍被别的进程占用：确认没有第二个 2RTK 实例或 minicom 开着'
        if 'not exist' in text or '找不到' in text or 'errno 2' in text:
            return '。设备节点不存在：检查 USB 是否插好、是否已枚举（dmesg | tail）'
        return ''

    # ---------- 三、心跳 ----------
    def beat(self, force=False):
        """推进内存心跳；磁盘心跳按 5 秒节流。

        **只由标定工作线程调用**（阶段切换、长循环的每一轮）。
        如果改成独立线程定时喂，工作线程卡死时心跳照旧，看门狗就失效了。
        """
        now = time.time()
        self.last_beat = now
        state.calibration_beat = now
        if force or now - self._last_disk_beat >= HEARTBEAT_DISK_INTERVAL:
            self._last_disk_beat = now
            # force=True 用于阶段切换：此时 held 可能还是 False（还没 open），
            # 但磁盘上的 stage 必须同步过去，否则页面永远显示 PREPARE。
            if self.held or force:
                self._write_lease_safe(durable=False)

    # ---------- 四、归还（§3.3） ----------
    def release(self, stage='DONE', message=''):
        """归还串口。**幂等**，且必须放在 try/finally 最外层。

        即使 `acquire()` 失败过、即使串口已经拔掉、即使这一步之前抛过异常，
        这个方法都必须能把 gate 清掉并唤醒主循环 —— 它的失败模式只能是"记一条日志"。
        """
        if self._released:
            return
        self._released = True

        # 1) 尽力把模块切回原模式（P0 只读，restore_mode_to 恒为空）
        #    必须放在关 fd 之前：关了就没法发命令了。方案 §3.3 把"关 fd"写在第一步，
        #    但那样第 2 步根本执行不了，所以这里按可执行顺序调整。
        if self.restore_mode_to and self.driver is not None and self.channel is not None:
            try:
                ok, detail, _data = self.driver.set_mode(self.restore_mode_to)
                logging.info(f'归还租约前切回 {self.restore_mode_to} 模式：'
                             f'{"成功" if ok else "失败（已忽略）"} {detail}')
            except Exception as e:                   # noqa: BLE001 - 尽力而为
                logging.warning(f'归还租约前切回原模式失败（已忽略）：{e}')

        # 2) 关 fd：任何异常都吞掉（设备可能已经拔线）
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception as e:                   # noqa: BLE001
                logging.debug(f'关闭标定通道失败（忽略）：{e}')
            self.channel = None

        # 3) 落终态
        self.held = False
        if not is_terminal(stage):
            stage = 'ABORTED'
        self.stage = stage
        self._write_terminal(stage, message)

        # 4) 清 gate —— **必须在置 restart 之前**，否则数据面"起来即退出"
        state.calibration_hold.clear()
        state.calibration_beat = 0.0

        # 5) 唤醒主循环立即恢复数据面，不等它自己那 1 秒醒来
        state.restart_event.set()

        held_for = int(time.time() - self.acquired_at) if self.acquired_at else 0
        logging.info(f'标定租约已归还（{stage}，占用 {held_for} 秒）：{self.port}，数据面恢复中')

    def _drop_gate(self):
        """失败路径专用：清 gate 并把数据面放回去（顺序与 release 相同）。"""
        state.calibration_hold.clear()
        state.calibration_beat = 0.0
        state.restart_event.set()

    # ---------- 五、落盘 ----------
    def _lease_payload(self):
        return {
            'version': __version__,
            'task_id': self.task_id,
            'port': self.port,
            'baud': self.baud,
            'stage': self.stage,
            'held': self.held,
            'pid': os.getpid(),
            'started_at': self._started_at,
            'heartbeat': time.time(),
            'heartbeat_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'module': dict(self.module),
        }

    def _write_lease_safe(self, durable=False):
        try:
            workspace.write_json(workspace.lease_path(), self._lease_payload(),
                                 durable=durable)
        except OSError as e:
            logging.warning(f'写租约文件失败（不影响本次标定）：{e}')

    def _write_terminal(self, stage, message=''):
        payload = self._lease_payload()
        payload['stage'] = stage
        payload['held'] = False
        payload['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        payload['message'] = str(message or '')
        try:
            workspace.write_json(workspace.lease_path(), payload)
        except OSError as e:
            logging.warning(f'写租约终态失败（不影响数据面恢复）：{e}')


# ==================== 自测（不需要硬件） ====================
def _selftest():
    """离线验证：让位时序、gate 顺序、幂等、残留判定、pid 探活。

    全程把串口换成假对象，不碰真实硬件，也不改真实 `.calibration/`。
    """
    import tempfile
    import threading

    print('=== lease 自测 ===')
    original_dir = workspace.STATE_DIR
    tmpdir = tempfile.mkdtemp(prefix='2rtk-lease-')
    workspace.STATE_DIR = tmpdir

    class _FakeSerial:
        def __init__(self):
            self.is_open = True
            self.written = []

        def read(self, _size=256):
            return b''

        def write(self, data):
            self.written.append(data)

        def flush(self):
            pass

        def reset_input_buffer(self):
            pass

        def close(self):
            self.is_open = False

    class _SlowWorker(threading.Thread):
        """模拟"还在跑的数据源线程"：让位必须等它退出。"""

        def __init__(self):
            super().__init__(daemon=True)
            self.stop = threading.Event()
            self.alive_window = 0.4

        def run(self):
            self.stop.wait(self.alive_window)

    try:
        state.calibration_hold.clear()
        state.calibration_beat = 0.0
        state.restart_event.clear()
        state.worker_thread = None

        # 1) 正常申请：gate 置位、restart 置位、租约落盘
        lease = SerialLease(port='FAKE0', baud=115200, task_id='T1')
        lease.channel = Channel(port='FAKE0', serial_instance=_FakeSerial())
        lease.channel.serial_factory = lambda port, baud: _FakeSerial()
        ok, message = lease.acquire()
        assert ok, message
        assert state.calibration_hold.is_set(), 'acquire 后 gate 必须置位'
        assert state.restart_event.is_set(), 'acquire 后必须唤醒主循环'
        assert state.calibration_beat > 0, '心跳必须与 gate 同时写入'
        disk = read_lease()
        assert disk and disk['port'] == 'FAKE0' and disk['stage'] == 'HOLD', disk
        assert disk['pid'] == os.getpid()
        print(f'[1] 申请让位 OK  gate={state.calibration_hold.is_set()} stage={disk["stage"]}')

        # 2) 重复申请必须被拒（单实例）
        again = SerialLease(port='FAKE1')
        ok2, message2 = again.acquire()
        assert not ok2 and '正在进行' in message2, message2
        print(f'[2] 重复申请拒绝 OK  {message2}')

        # 3) 数传同口拒绝（用一个假的配置读取）
        import rtkserver.core.config as core_config
        original_get = core_config.get_config
        core_config.get_config = lambda: {'radio_port': 'FAKE2', 'radio_enabled': '1'}
        state.calibration_hold.clear()          # 先放开，才能走到同口校验
        radio = SerialLease(port='FAKE2')
        ok3, message3 = radio.acquire()
        core_config.get_config = original_get
        assert not ok3 and '数传' in message3, message3
        print(f'[3] 数传同口拒绝 OK  {message3[:34]}…')

        # 4) 让位要真的等数据源线程退出
        state.calibration_hold.clear()
        state.restart_event.clear()
        slow = _SlowWorker()
        state.worker_thread = slow
        slow.start()
        started = time.time()
        waiter = SerialLease(port='FAKE3')
        waiter.channel = Channel(port='FAKE3', serial_instance=_FakeSerial())
        ok4, _message4 = waiter.acquire()
        elapsed = time.time() - started
        assert ok4 and elapsed >= slow.alive_window, (ok4, elapsed)
        print(f'[4] 等待数据面让位 OK  耗时 {elapsed:.2f}s（线程活 {slow.alive_window}s）')
        waiter.release('ABORTED')

        # 5) release 清 gate 且先清后置（数据面能立刻恢复）
        state.restart_event.clear()
        lease.release('DONE')
        assert not state.calibration_hold.is_set(), 'release 后 gate 必须清除'
        assert state.calibration_beat == 0.0, 'release 后心跳要归零，避免残留值'
        assert state.restart_event.is_set(), 'release 后必须唤醒主循环恢复数据面'
        disk = read_lease()
        assert disk['stage'] == 'DONE' and disk['held'] is False, disk
        print('[5] 归还与恢复唤醒 OK  gate=0 restart=1 stage=DONE')

        # 6) 幂等：连调三次不炸、状态不变
        for _ in range(3):
            lease.release('FAILED')
        assert read_lease()['stage'] == 'DONE', '重复 release 不能改写终态'
        print('[6] release 幂等 OK  重复调用不改写终态')

        # 7) 残留判定：非终态 + pid 已死 = 被硬中断
        workspace.write_json(workspace.lease_path(), {
            'port': 'FAKE9', 'stage': 'SAVE', 'pid': 999999, 'held': True,
            'started_at': '2026-11-01 12:00:00', 'heartbeat': 0,
            'module': {'model': 'LG290P', 'sn': 'AB12'},
        })
        leftover = leftover_lease()
        assert leftover and leftover['stage'] == 'SAVE', leftover
        print(f'[7] 残留租约判定 OK  上次停在 {leftover["stage"]} / 模块 {leftover["module"]["sn"]}')

        # 8) 终态租约不算残留
        workspace.write_json(workspace.lease_path(), {'stage': 'DONE', 'pid': 999999})
        assert leftover_lease() is None
        print('[8] 终态不算残留 OK')

        # 9) pid 探活：自己是活的，999999 是死的
        assert pid_alive(os.getpid()), '自己的 pid 必须判活'
        assert not pid_alive(999999), '不存在的 pid 必须判死'
        assert not pid_alive('abc') and not pid_alive(0)
        print(f'[9] pid 探活 OK  self={os.getpid()} alive；999999 dead')

        # 10) 串口打开失败必须把 gate 放回去（不能卡住数据面）
        state.calibration_hold.clear()
        state.restart_event.clear()
        state.worker_thread = None
        broken = SerialLease(port='NO_SUCH_PORT')
        broken.channel = Channel(port='NO_SUCH_PORT')

        def _boom(_port, _baud):
            raise OSError('could not open port: Permission denied')

        broken.channel.serial_factory = _boom
        broken.held = False
        ok6, _msg6 = broken.acquire()
        assert ok6, 'acquire 本身应当成功'
        ok7, message7 = broken.open()
        assert not ok7 and 'Permission' in message7 and 'dialout' in message7, message7
        assert not state.calibration_hold.is_set(), '打开失败必须清 gate'
        assert state.restart_event.is_set(), '打开失败必须唤醒主循环'
        print(f'[10] 打开失败自动放开 gate OK  {message7[-26:]}')

        print('=== lease 自测全部通过 ===')
        return 0
    finally:
        workspace.STATE_DIR = original_dir
        state.calibration_hold.clear()
        state.calibration_beat = 0.0
        state.worker_thread = None
        for name in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, name))
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


if __name__ == '__main__':
    logging.basicConfig(level=logging.CRITICAL)
    raise SystemExit(_selftest())
