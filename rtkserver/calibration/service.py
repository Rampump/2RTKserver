# -*- coding: utf-8 -*-
"""标定服务：单实例、单工作线程的状态机（方案 §9）+ P0 只读诊断（方案 §16）。

职责边界（改代码前先读这一段）：
    本模块**只做流程**。串口时序在 `lease.py`，命令在 `drivers/*.py`，
    解析在 `proto/*.py`。所以本文件里不允许出现 `$PQTM` 字样 ——
    一旦出现，换型号就必须改流程，多机型维护立刻崩盘（契约测试会拦住）。

线程模型（方案 §2.2）：
    `calib-worker` 一条线程跑完整个任务：所有串口读写、观测、判定都在里面。
    刻意不新增第二条线程：心跳必须由**工作线程自己**推进，否则线程卡死时
    心跳照旧，看门狗永远不触发（见 lease.py 顶部说明）。

故障隔离（本文件的核心契约）：
    - 任何异常都不许冒泡到进程级：`_worker` 顶层兜住一切异常 → FAILED。
    - `finally` 里**一定**调 `lease.release()`：租约幂等，重复调用无副作用，
      漏了它就是"网页能开、设备不转发、日志没报错"。
    - 每 1 秒把状态原子写进 `.calibration/state.json`：用户会关页面、换手机看、
      十分钟后回来看进度，纯内存状态满足不了（方案 §9.3）。

P0 阶段只读：
    只发**查询**命令（VERNO / UNIQID / RCVRMODE,R / CFGSVIN,R / CFGRTCM,R），
    以及两条能力探测（PQTMSN / PQTMCFGANTINF,R，也是查询）。
    全程不发 SAVEPAR / SRR / 任何 `,W` 命令 —— 模块 NVM 一个字节都不动，
    所以可以在真机上随便跑，失败了最多是数据面断几十秒。

P1 写入（阶段3）：
    `action='write'` 走备份 → 写坐标 → 切模式 → 配报文 → 保存 → 重启 → 三级校验 → 落库，
    真正的"每一步怎么判"在 `writer.py`，本文件只负责租约、阶段推进、失败收尾与落库。
    `action='restore'` 复用同一条链路，只是目标值取自 `backup.json`。
"""
import argparse
import json
import logging
import os
import threading
import time

from ..core import state
from . import backup as backup_mod
from . import store, workspace
from .drivers import driver_manager
from .drivers.base import DriverBug, frame
from .geo import ecef_to_geodetic, max_axis_delta
from .lease import SerialLease, leftover_lease
from .proto.rtcm import decode_frame
from .writer import (COORD_TOLERANCE, DEFAULT_RTCM_SAMPLES, DEFAULT_RTCM_TIMEOUT,
                     RTCM_FAIL_LIMIT, RTCM_WARN_LIMIT, STEPS as WRITE_STEPS,
                     SURVEY_PROFILES, CORS_DURATIONS, CORS_DEFAULT_DURATION,
                     CoordinateWriter, WriteFailed, WritePlan)

# ==================== 状态清单（方案 §9.1） ====================
STAGE_TEXT = {
    'IDLE': '空闲',
    'PREPARE': '校验参数',
    'HOLD': '等待数据面让出串口',
    'OPEN': '打开串口',
    'DETECT': '识别模块',
    'BACKUP': '备份原配置',
    'WRITE_COORD': '写入坐标',
    'BASE_SETUP': '切换工作模式',
    'MSG_SETUP': '配置报文输出',
    'SVIN_CFG': '配置自收敛',
    'SVIN_WAIT': '等待自收敛',
    'ROVER_SETUP': '切换流动站',
    'NMEA_SETUP': '配置定位输出',
    'GGA_WAIT': '等待定位',
    'CORS_CONNECT': '连接 CORS',
    'FIX_WAIT': '等待固定解',
    'COLLECT': '采集与滤波',
    'CORS_CLOSE': '断开 CORS',
    'SAVE': '保存到模块 NVM',
    'RESTART': '重启模块',
    'VERIFY_MODE': '校验模式与坐标',
    'VERIFY_RTCM': '校验 1005 播发坐标',
    'INJECT': '标定结果上链',
    'OBSERVE': '只读观测',
    'PLAN': '汇总判定',
    'DONE': '完成',
    'FAILED': '失败',
    'ABORTED': '已取消',
    'INTERRUPTED': '被中断',
}

# 还原任务复用同一批阶段，只换文案 —— 否则用户点"还原"时看到"写入坐标"会以为又标定了一次
STAGE_TEXT_RESTORE = {
    'WRITE_COORD': '还原坐标配置',
    'BASE_SETUP': '还原工作模式',
    'MSG_SETUP': '还原报文配置',
    'SAVE': '保存到模块 NVM',
    'RESTART': '重启模块',
    'VERIFY_MODE': '校验还原结果',
    'VERIFY_RTCM': '校验 1005 播发坐标',
    'INJECT': '还原结果上链',
    'BACKUP': '读取备份',
}

STAGE_PROGRESS = {
    'IDLE': 0, 'PREPARE': 3, 'HOLD': 8, 'OPEN': 14, 'DETECT': 20,
    'BACKUP': 26,
    'WRITE_COORD': 36, 'BASE_SETUP': 46, 'MSG_SETUP': 56, 'SAVE': 64,
    'SVIN_CFG': 30, 'SVIN_WAIT': 40,
    'ROVER_SETUP': 24, 'NMEA_SETUP': 28, 'GGA_WAIT': 32, 'CORS_CONNECT': 36,
    'FIX_WAIT': 40, 'COLLECT': 60, 'CORS_CLOSE': 66,
    'RESTART': 72, 'VERIFY_MODE': 80, 'VERIFY_RTCM': 88, 'INJECT': 96,
    'OBSERVE': 45, 'PLAN': 95,
    'DONE': 100, 'FAILED': 0, 'ABORTED': 0, 'INTERRUPTED': 0,
}

TERMINAL_STAGES = ('DONE', 'FAILED', 'ABORTED', 'INTERRUPTED')

# 坐标一致性的判据（方案 §8.2）：1005 的量化步长是 0.1 mm，
# 所以"播发值 == 配置值"本该精确相等，1 cm 已经是极宽松的门槛。
COORD_MATCH_THRESHOLD = 0.01

# GGA 质量位（NMEA 0183 标准定义，基站模式下 NMEA 默认关闭，拿不到也不影响结论）
_QUALITY_TEXT = {
    0: '无效', 1: '单点', 2: '差分', 3: 'PPS', 4: 'RTK 固定', 5: 'RTK 浮动',
    6: '推算', 7: '手工', 8: '模拟',
}

DEFAULT_BAUD = 115200
DEFAULT_OBSERVE_SECONDS = 20
MIN_OBSERVE_SECONDS = 3
MAX_OBSERVE_SECONDS = 300

# 识别失败后的波特率枚举（方案 §14 #3 / #25）。
# 顺序按真机出现概率排：LG290P 出厂 115200，工程上常被改成 460800；
# 9600 太慢，一轮识别要 6 秒，所以放最后且只在其它档全失败时才试。
# **只在用户没有显式指定波特率时才枚举**：用户明确填了 460800 却被
# 悄悄改成 115200 去试，等于把用户的判断覆盖掉，而且改完还不告诉他。
#
# 这份只是**兜底**：真正该用哪些档位，由各驱动类的 BAUDS（厂商文档口径）决定，
# 见 `_baud_candidates()`。驱动没声明 BAUDS 的型号才回落到这里。
DETECT_BAUD_CANDIDATES = (460800, 230400, 921600, 38400, 9600)


class TaskAborted(Exception):
    """用户取消 / 进程退出。

    `evicted` 这个类属性是给 `writer` 判"能不能回写"用的（鸭子类型，不 import）：
    取消要回写（把模块从"半个基准站"救回来），被看门狗回收**不能**回写
    （串口已经还给数据面了，再发命令就是抢字节）。
    """
    evicted = False


class TaskEvicted(Exception):
    """租约被看门狗强制回收（心跳超时），本任务必须立刻收手。"""
    evicted = True


class TaskFailed(Exception):
    """流程性失败，消息直接给用户看。"""


# ==================== 服务本体 ====================
class CalibrationService:
    """全局只允许一个任务（方案 §9.4）。

    单实例由两层保证：
        `_start_lock`  —— 进程内同一时刻只允许一个 start() 在推进；
        `_thread.is_alive()` —— 上一个任务的工作线程还活着就拒绝新任务。
    跨进程不考虑：只有 Web 单进程能发起；残留租约由 `lease.leftover_lease()` 兜。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._thread = None
        self._cancel = threading.Event()
        self._lease = None
        self._driver = None
        self._writer = None                  # 正在跑的写链路（取消时用来判能否回写）

        self._stage = 'IDLE'
        self._message = ''
        self._error = ''
        self._progress = 0
        self._task = {}
        self._result = {}
        self._task_id = ''
        self._started_at = ''
        self._finished_at = ''
        self._last_persist = 0.0
        self._persist_interval = 1.0

    # ---------- 对外快照 ----------
    def status(self):
        """给 Web / CLI 看的完整快照。永远不抛异常。"""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        lease = self._lease
        action = self._task.get('action', '')
        texts = STAGE_TEXT_RESTORE if action == 'restore' else STAGE_TEXT
        return {
            'task_id': self._task_id,
            'action': action,
            'stage': self._stage,
            'stage_text': texts.get(self._stage, STAGE_TEXT.get(self._stage, self._stage)),
            'progress': self._progress,
            'message': self._message,
            'error': self._error,
            'running': bool(self._thread is not None and self._thread.is_alive()),
            'port': self._task.get('port', ''),
            'baud': ((lease.baud if lease is not None and getattr(lease, 'baud', None)
                      else self._task.get('baud', ''))),
            # 用户是否手填了波特率。False = 自动探测，识别失败时允许逐档重试；
            # 前端据此决定要不要在结果里显示"已自动改为 X"这类说明。
            'baud_explicit': bool(self._task.get('baud_explicit')),
            'started_at': self._started_at,
            'finished_at': self._finished_at,
            'lease': lease.snapshot() if lease is not None else None,
            # 上次被硬中断的详情（只有启动钩子置过 INTERRUPTED 才有值）。
            # 单独给一份是因为 `result` 会在下次任务开始时被清空，
            # 而横幅要在"还没开始新任务"这段时间里一直显示。
            'interrupted': ((self._result or {}).get('interrupted')
                            if self._stage == 'INTERRUPTED' else None),
            # 运行中把写链路的实时结果一并带出去（CORS 采集的 live 指标在这里）；
            # 任务结束后 _result 才有值，以它为准。
            'result': self._result or (getattr(self._writer, 'result', None) or {}),
        }

    # ---------- 启动（公共骨架） ----------
    def _start_task(self, action, task, task_id=None, log_text=''):
        """统一的开机流程：校验单实例 → 置初始状态 → 起工作线程 → 落盘。

        三种任务（diagnose / write / restore）的差别全在 `task` 字典里，
        "谁能同时跑、gate 什么时候置、状态怎么落盘"只有这一份实现。
        """
        with self._start_lock:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return False, '已有标定任务在进行，请等它结束或先取消', self._snapshot_locked()

                self._cancel.clear()
                self._task_id = task_id or _new_task_id()
                self._task = dict(task, action=action)
                self._stage = 'PREPARE'
                self._message = '准备中'
                self._error = ''
                self._progress = STAGE_PROGRESS['PREPARE']
                self._result = {}
                self._started_at = _now_text()
                self._finished_at = ''
                self._driver = None
                self._writer = None
                payload = self._snapshot_locked()

                self._thread = threading.Thread(target=self._worker, name='calib-worker',
                                                daemon=True)
                self._thread.start()

        self._persist(payload, force=True)
        logging.info(f'{log_text}（任务 {self._task_id}）')
        return True, f'已开始{log_text.split("：")[0]}：{task.get("port")}', self.status()

    @staticmethod
    def _resolve_port(port):
        port = str(port or '').strip()
        return port or _config_serial_port()

    @staticmethod
    def _resolve_baud(baud):
        """`(baud, explicit, error)`。

        `explicit` 区分两种"看起来一样"的情况：
          - 前端留空（自动）→ 落 DEFAULT_BAUD，`explicit=False`，
            识别失败时允许按该型号官方档位逐档重试（见 `_scan_bauds`）。
          - 用户手填 115200 → 值恰好等于默认，但它仍是**用户的判断**，
            `explicit=True`，不允许被悄悄改成别的档位。

        如果只看"值等不等于默认"来判，手填 115200 就会被当成没填，
        接错线时程序反而会去乱换波特率，把用户的排除动作全推翻。
        """
        raw = baud
        given = raw not in (None, '') and str(raw).strip() != ''
        if not given:
            # 留空 = 自动：落默认档，但标记成"非显式"，
            # 这样识别失败时允许按型号官方档位逐档重试。
            return int(DEFAULT_BAUD), False, ''
        try:
            if isinstance(raw, bool):
                raise TypeError
            value = int(raw)
            if value <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return None, False, f'波特率不合法：{baud}'
        return value, True, ''

    # ---------- 启动：P0 只读诊断 ----------
    def start_diagnose(self, port=None, baud=None, seconds=DEFAULT_OBSERVE_SECONDS,
                       task_id=None, module=None):
        """启动一次 P0 只读诊断。返回 `(success, message, status)`。"""
        port = self._resolve_port(port)
        if not port:
            return False, '没有指定串口，配置里也没有设置串口', self.status()
        baud, baud_explicit, error = self._resolve_baud(baud)
        if error:
            return False, error, self.status()
        try:
            seconds = int(seconds or DEFAULT_OBSERVE_SECONDS)
        except (TypeError, ValueError):
            seconds = DEFAULT_OBSERVE_SECONDS
        seconds = max(MIN_OBSERVE_SECONDS, min(seconds, MAX_OBSERVE_SECONDS))

        return self._start_task(
            'diagnose', {'port': port, 'baud': baud, 'seconds': seconds,
                         'module': _clean_module(module),
                         'baud_explicit': baud_explicit},
            task_id=task_id,
            log_text=f'只读诊断：{port} @ {baud}，观测 {seconds} 秒')

    # ---------- 启动：P1 写入历史坐标 ----------
    def start_write(self, port=None, baud=None, ecef=None, lat=None, lon=None,
                    height=0.0, method='manual', msm=4, antenna=None,
                    remark='', dry_run=False, rtcm_timeout=DEFAULT_RTCM_TIMEOUT,
                    task_id=None, module=None, coord_msg='1005', arp=None):
        """写入固定坐标并跑完整校验链路（方案 §8）。返回 `(success, message, status)`。

        参数校验**同步做完**：ECEF 越界这类问题必须在下发任何命令之前就报出来，
        而不是等占住串口、切了基站才发现参数是错的。

        `arp=` 给「复用历史坐标」用：库里存的就是当年写进去的值，直接复用。
        """
        port = self._resolve_port(port)
        if not port:
            return False, '没有指定串口，配置里也没有设置串口', self.status()
        baud, baud_explicit, error = self._resolve_baud(baud)
        if error:
            return False, error, self.status()

        # 能力校验放在 WritePlan.build 之前：先挡掉"这块模块根本写不了"，
        # 再谈参数对不对。顺序反了用户会看到"参数不合法"这种驴唇不对马嘴的提示。
        # `arp=` 是复用历史坐标，同样要写进 NVM，所以一并按 fixed 能力判。
        error = _reject_write_without_fixed(module)
        if error:
            return False, error, self.status()

        try:
            plan = WritePlan.build(ecef=ecef, lat=lat, lon=lon, height=height,
                                   method=method, msm=msm,
                                   antenna=antenna, remark=remark,
                                   coord_msg=coord_msg, arp=arp)
        except ValueError as e:
            return False, f'参数不合法：{e}', self.status()

        return self._start_task(
            'write',
            {'port': port, 'baud': baud, 'plan': plan, 'dry_run': bool(dry_run),
             'module': _clean_module(module), 'baud_explicit': baud_explicit,
             'rtcm_timeout': max(5.0, float(rtcm_timeout or DEFAULT_RTCM_TIMEOUT))},
            task_id=task_id,
            log_text=f'写入坐标：{port} @ {baud}，{plan.written()}')

    # ---------- 启动：Survey-in 自收敛（方案 P3） ----------
    def start_survey_in(self, port=None, baud=None, profile='standard', msm=4,
                        antenna=None, remark='', distance=None,
                        rtcm_timeout=DEFAULT_RTCM_TIMEOUT, svin_timeout=None,
                        task_id=None, module=None, coord_msg='1005', resume=True):
        """启动模块自收敛标定：模块自己收敛出坐标，再走后续校验链路。

        档位（方案 P3）：fast=600 次/3.0m、standard=1800 次/1.5m、high=3600 次/0.8m。
        精度等级为**亚米级**，与 CORS 的厘米级区分（记录里 `method=survey_in`）。
        """
        port = self._resolve_port(port)
        if not port:
            return False, '没有指定串口，配置里也没有设置串口', self.status()
        baud, baud_explicit, error = self._resolve_baud(baud)
        if error:
            return False, error, self.status()

        key = str(profile or 'standard').strip().lower()
        if key not in SURVEY_PROFILES:
            return False, f'档位只能是 {"/".join(sorted(SURVEY_PROFILES))}，收到 {profile!r}', \
                self.status()

        # Survey-in 要写 NVM：既能下发命令，也要有 SVIN 能力。
        # 只查 `svin` 不查 `fixed`：走这条路本来就是为了让模块自己收敛出坐标，
        # 只要它能跑 SVIN 就能把结果存进 NVM，这两件事在驱动里是同一个能力组。
        error = _reject_without_caps(module, 'svin')
        if error:
            return False, error, self.status()

        try:
            plan = WritePlan.build_survey_in(profile=key, mode='base', msm=msm,
                                             antenna=antenna, remark=remark,
                                             distance=distance, verify_rtcm=True,
                                             coord_msg=coord_msg, resume_svin=resume)
        except ValueError as e:
            return False, f'参数不合法：{e}', self.status()

        # 收敛超时：None = 不限时（一直等收敛或用户手动终止）。传了就用传入值，
        # 不再设下限——真机验证/测试会传小值，且"手动终止"本身就是更可靠的兜底。
        timeout = None if svin_timeout is None else max(1.0, float(svin_timeout))
        preset = SURVEY_PROFILES[key]
        return self._start_task(
            'write',
            {'port': port, 'baud': baud, 'plan': plan, 'dry_run': False,
             'module': _clean_module(module), 'baud_explicit': baud_explicit,
             'rtcm_timeout': max(5.0, float(rtcm_timeout or DEFAULT_RTCM_TIMEOUT)),
             'svin_timeout': timeout},
            task_id=task_id,
            log_text=(f'Survey-in 自收敛：{port}，{preset["label"]}'
                      f'（{preset["count"]} 次 / {preset["acc"]} m，亚米级）'))

    # ---------- 启动：CORS 差分标定（方案 P1） ----------
    def start_cors(self, port=None, baud=None, host='', cors_port=None, mountpoint='',
                   user='', password='', duration='standard', msm=4, antenna=None,
                   remark='', rtcm_timeout=DEFAULT_RTCM_TIMEOUT, task_id=None,
                   fix_wait_timeout=None, module=None, coord_msg='1005',
                   measure_only=False, on_slow='adopt', reuse_arp=None):
        """启动 CORS 采集。

        `measure_only=True` 走**只测不写**（`external_rover`）：同一条采集链路，
        但测出坐标就结束，不写坐标/不配报文/不保存 NVM。若模块当前是基准站，
        会做一次**仅内存**的临时切换（不下发 SAVEPAR），结束/失败/中止都恢复原模式。
        用于"这块模块是外接流动站，或没有命令集的未知模块，我只要一个天线坐标"。

        `on_slow` 决定"到时长仍未收敛"怎么办（§14 #14）：adopt 采用当前值并标注，
        fail 直接失败、不写坐标。

        `reuse_arp=` 是标定流程 **A1 的复用分支**：库内已有该天线的坐标且用户确认
        天线没搬过 → 跳过整段 CORS 测量，不连网、不占 CORS 账号，直接设基准站。
        与 `measure_only` 互斥（前者要写 NVM，后者一个字节都不许写）。
        """
        port = self._resolve_port(port)
        if not port:
            return False, '没有指定串口，配置里也没有设置串口', self.status()
        baud, baud_explicit, error = self._resolve_baud(baud)
        if error:
            return False, error, self.status()

        key = str(duration or 'standard').strip().lower()
        seconds = CORS_DURATIONS.get(key)
        if seconds is None:
            return False, f'采集时长只能是 {"/".join(sorted(CORS_DURATIONS))}，收到 {duration!r}', \
                self.status()

        reuse = reuse_arp is not None
        if reuse and measure_only:
            return False, ('「复用历史坐标」与「只测不写」互相矛盾：'
                           '前者要把坐标写进模块 NVM，后者一个字节都不许写。'
                           '想只测坐标请选「只测坐标」，想复用坐标请选「CORS 差分标定」'), \
                self.status()

        # 两种用法能力要求完全不同，必须分开判：
        #   measure_only=True  只测坐标。哪怕模块一个命令都不认识（未知模块/纯基准站）
        #                      也能用——只要能输出 NMEA 就够了，所以这里**不拦**。
        #   measure_only=False 要临时切流动站、再切回基准站并写 NVM，三样都得有。
        #                      `writer.py` 的 `_step_cors_rover_setup` 里还有一次同样的
        #                      判断（那时驱动已确认），这里是提前挡，省掉白等一次串口占用。
        #   reuse_arp 分支不切流动站（不测量），但同样要切回基准站并写 NVM，
        #   所以一样按 fixed + set_mode 判；不需要 dual_mode（不必先当流动站）。
        if not measure_only:
            need = ('fixed', 'set_mode') if reuse else ('fixed', 'set_mode', 'dual_mode')
            error = _reject_without_caps(module, *need)
            if error:
                return False, error, self.status()

        try:
            plan = WritePlan.build_cors(
                host=host, port=cors_port, mountpoint=mountpoint, user=user,
                password=password, duration=seconds, msm=msm, antenna=antenna,
                remark=remark, verify_rtcm=True, coord_msg=coord_msg,
                method='external_rover' if measure_only else 'cors',
                on_slow=on_slow, reuse_arp=reuse_arp)
        except ValueError as e:
            return False, f'参数不合法：{e}', self.status()

        if reuse:
            log_text = f'复用历史坐标设基准站：{port}（跳过 CORS 测量，天线未移动）'
        elif measure_only:
            log_text = (f'只测不写（外接流动站测坐标）：{port} → {host}:{cors_port}/{mountpoint}'
                        f'（{seconds} 秒采集，不修改模块）')
        else:
            log_text = (f'CORS 差分标定：{port} → {host}:{cors_port}/{mountpoint}'
                        f'（{seconds} 秒采集，厘米级）')

        return self._start_task(
            'write',
            {'port': port, 'baud': baud, 'plan': plan, 'dry_run': False,
             'module': _clean_module(module), 'baud_explicit': baud_explicit,
             'rtcm_timeout': max(5.0, float(rtcm_timeout or DEFAULT_RTCM_TIMEOUT)),
             'fix_wait_timeout': fix_wait_timeout},
            task_id=task_id,
            log_text=log_text)

    # ---------- 启动：还原标定前配置 ----------
    def start_restore(self, port=None, baud=None, task_id=None, rtcm_timeout=DEFAULT_RTCM_TIMEOUT):
        """把模块恢复到上次标定前的配置（读 `.calibration/backup.json`）。"""
        snapshot = backup_mod.load()
        usable, why = backup_mod.usable(snapshot)
        if not usable:
            return False, why, self.status()
        try:
            plan = WritePlan.from_backup(snapshot)
        except ValueError as e:
            return False, f'备份内容不可用于还原：{e}', self.status()

        port = self._resolve_port(port or snapshot.get('port'))
        if not port:
            return False, '没有指定串口，备份里也没有记录端口', self.status()
        baud, baud_explicit, error = self._resolve_baud(baud)
        if error:
            return False, error, self.status()

        return self._start_task(
            'restore',
            {'port': port, 'baud': baud, 'plan': plan, 'dry_run': False,
             'baud_explicit': baud_explicit,
             'rtcm_timeout': max(5.0, float(rtcm_timeout or DEFAULT_RTCM_TIMEOUT))},
            task_id=task_id,
            log_text=f'还原标定前配置：{port}（{backup_mod.describe(snapshot)}）')

    def abort(self):
        """请求取消。状态机在下一个检查点收尾，不做硬中断（方案 §9.2）。"""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False, '当前没有进行中的任务'
            self._cancel.set()
            stage = self._stage
            self._message = '收到取消请求，正在安全收尾'
        logging.info(f'标定任务收到取消请求（当前阶段 {stage}）')
        return True, '已请求取消，正在收尾'

    def wait(self, timeout=None):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status()

    # ---------- 启动钩子：上次被硬中断 ----------
    def mark_interrupted(self, lease, message=''):
        """把"上次进程被硬中断"落进状态里，让页面能显示警告横幅（方案 §14 #20）。

        为什么必须落进 `state.json` 而不是只在日志里说一句：
            进程被 `kill -9` / 断电之后，模块可能停在"已经切了基准站但坐标还是旧的"
            这种中间态。用户第二天打开页面，如果只看到"空闲"，就会直接拿它当基准站用。
            横幅是唯一能在动手之前拦住他的东西。

        为什么要把 `lease.json` 也改成终态：
            不改的话每次开机都会重新报一次"上次被中断"，用户点掉提示重开又出现，
            最后所有人都学会无视它——提示失去意义比没有提示更糟。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stage = 'INTERRUPTED'
            self._message = str(message or '上次标定被中断')
            self._error = self._message
            self._progress = 0
            self._task_id = str(lease.get('task_id') or '')
            self._task = {'action': lease.get('action') or 'write',
                          'port': lease.get('port') or '',
                          'baud': lease.get('baud') or ''}
            self._started_at = str(lease.get('started_at') or '')
            self._finished_at = _now_text()
            self._result = {'interrupted': self._interrupted_info(lease, message)}
            payload = self._snapshot_locked()
        self._persist(payload, force=True)
        return True

    @staticmethod
    def _interrupted_info(lease, message=''):
        module = lease.get('module') or {}
        return {
            'stage': str(lease.get('stage') or ''),
            'stage_text': STAGE_TEXT.get(lease.get('stage'), str(lease.get('stage') or '')),
            'port': str(lease.get('port') or ''),
            'baud': lease.get('baud'),
            'module': module.get('model') or '',
            'sn': module.get('sn') or '',
            'started_at': str(lease.get('started_at') or ''),
            'heartbeat_at': str(lease.get('heartbeat_at') or ''),
            'message': str(message or ''),
        }

    # ---------- 工作线程 ----------
    def _detect_module(self, lease, only, allow_baud_scan):
        """识别模块，带两级兜底（方案 §14 #3 / #4 / #25）。

        识别失败有三种完全不同的原因，对应的动作也不同，**不能让用户自己去猜**：

        | 现象 | 真实原因 | 这里的处理 |
        | --- | --- | --- |
        | 串口有字节、命令无应答 | 硬件流控 RTS/CTS 被接错，或 TX/RX 反了 | 文案直接点出来 |
        | 认不出来、但换成别的型号能认出 | 页面选的型号不对 | 报出"实际检测到 X" |
        | 完全没字节/全是噪声 | 波特率不对 | 依次换波特率重开再识别 |

        波特率枚举**只在用户没显式指定波特率时做**：用户明确填了 460800，
        却被悄悄改成 115200 去试，等于把用户的判断覆盖掉，而且还不告诉他。
        """
        channel = lease.channel
        driver, message, attempts = driver_manager.match_driver(channel, only=only)
        if driver is not None:
            return driver, message, attempts, ''

        # ① 型号不符：换其余候选再问一次（跳过刚失败的那个，省一轮超时）
        others = [key for key in driver_manager.supported_driver_ids()
                  if not only or key not in only]
        if only and others:
            alt, _alt_message, _alt_attempts = driver_manager.match_driver(channel, only=others)
            if alt is not None:
                detected = {'id': alt.MODEL_KEY, 'name': alt.DISPLAY_NAME}
                self._result['detected_module'] = detected
                raise TaskFailed(
                    f'模块型号与所选不符：你选的是「{only[0]}」，实际检测到「{alt.DISPLAY_NAME}」'
                    f'（{_alt_message}）。请把「模块型号」改成 {alt.DISPLAY_NAME} 后重试，'
                    f'或直接选「自动探测」')

        # ② 波特率不对：换档重开串口再识别
        if allow_baud_scan:
            found = self._scan_bauds(lease, only)
            if found is not None:
                found_driver, found_baud = found
                return found_driver, f'{found_driver.DISPLAY_NAME}（{found_baud} 波特率识别）', \
                    attempts, f'已自动把波特率改为 {found_baud}'

        # ③ 都不成立：返回 None 由调用方决定（只测不写可以退化到 PassiveDriver，
        #    其它任务必须失败）。文案在这里拼好，避免上层再猜一遍病因。
        return None, self._detect_failure_text(channel, message, attempts), attempts, ''

    def _scan_bauds(self, lease, only):
        """依次换波特率重开串口再识别。成功返回 `(driver, baud)`，全失败返回 None。

        候选档位的来源分两种：
          - 用户选了具体型号（only 非空）→ 用**该型号厂商文档写的官方档位**。
            LG290P 只认 9600~921600，拿 4800 去试纯属浪费时间；
            LC29H(BS) 反而支持 4800/3000000，全局表里根本没这两档。
          - 自动探测（only 为空）→ 各已注册驱动的官方档位并集，再补全局兜底表。

        两种情况都把**当前打开用的这个波特率**放在候选表首位（`current`）：
        用户在手填框里写的值即使不是官方档位，也是现场依据，
        先按它试一轮，别先花四五轮超时再回到用户早说过的话。
        （它在这一轮里刚刚试过并失败，下面的 `continue` 会跳过它；
        之所以仍排在首位，是为了让它占据"优先级最高"的位置、
        在测速排序时不被打散，语义上也与前端"手填值最优先"一致。）

        改完没成功必须把串口按原波特率重新打开：调用方接下来要报错收尾，
        留一个打不开/参数不对的通道，会让"归还租约"这一步也异常。
        """
        channel = lease.channel
        original = channel.baud
        for baud in self._baud_candidates(only, current=original):
            if int(baud) == int(original):
                continue
            self._set_stage('DETECT', f'识别失败，改用 {baud} 波特率重试')
            try:
                channel.close()
                channel.baud = int(baud)
                channel.open()
                channel.drain()
                channel.flush(0.2)
            except Exception as e:                   # noqa: BLE001 - 换档失败就试下一个
                logging.debug(f'以 {baud} 重开串口失败：{e}')
                continue
            driver, message, _attempts = driver_manager.match_driver(channel, only=only)
            if driver is not None:
                lease.baud = int(baud)
                self._result['baud_switched'] = {'from': original, 'to': int(baud)}
                logging.warning(f'识别时自动切换波特率 {original} → {baud}：{message}')
                self._warn(f'已自动把波特率从 {original} 改为 {baud}（模块在这个档位才有应答）')
                return driver, int(baud)
            logging.debug(f'{baud} 波特率下仍未识别到模块')

        # 全失败：恢复原波特率重新打开，别把一个"参数被改过"的通道交回去
        try:
            channel.close()
            channel.baud = int(original)
            channel.open()
            channel.drain()
        except Exception as e:                       # noqa: BLE001
            logging.debug(f'恢复原波特率 {original} 失败：{e}')
        return None

    @staticmethod
    def _baud_candidates(only, current=None):
        """采集/识别该试哪些波特率（按优先级，已去重）。

        - 指定了型号 → 该型号厂商文档写的官方档位（降序，高的先试；
          高波特率能通说明线材和电平都正常，采样也更稳）。
        - 自动探测   → 所有已注册型号的官方档位并集 + 全局兜底表，
          保证这块模块哪怕型号键还没被 `only` 覆盖到也能被找到。
        - 总把 `current`（用户手填的那个值）排在最前：它虽然没被声明为
          官方档位，但用户这么填通常是因为现场模块就跑在这个档上
          （换过固件、改过 CONFIG COM1、或是工程上约定的非标值）。
          把它排到官方档位后面，等于先花四五轮超时再回到用户早就说过的答案。
        """
        def ordered(seq):
            out = []
            for value in seq:
                try:
                    n = int(value)
                except (TypeError, ValueError):
                    continue
                if n > 0 and n not in out:
                    out.append(n)
            return out

        head = []
        try:
            if current and int(current) > 0:
                head = [int(current)]
        except (TypeError, ValueError):
            head = []

        if only:
            merged = []
            for key in only:
                merged.extend(driver_manager.detect_bauds(key))
            candidates = ordered(merged)
            if candidates:
                return head + [b for b in candidates if b not in head]

        merged = []
        for key in driver_manager.supported_driver_ids():
            merged.extend(driver_manager.detect_bauds(key))
        merged.extend(DETECT_BAUD_CANDIDATES)
        tail = ordered(merged) or list(DETECT_BAUD_CANDIDATES)
        return head + [b for b in tail if b not in head]

    @staticmethod
    def _detect_failure_text(channel, message, attempts):
        """把"识别不到模块"翻译成用户能照做的下一步（方案 §14 #2 / #25）。"""
        received = getattr(channel, 'rx_bytes', 0)
        noise = (getattr(channel, 'counters', {}) or {}).get('noise', 0)
        lines = [f'模块无应答：{message}']
        if received:
            # 有字节但一句合法应答都没有：典型是流控/接线问题，不是模块坏
            lines.append(
                f'串口收到了 {received} 字节但没有一条合法应答——'
                f'优先检查硬件流控（RTS/CTS 是否被短接或错接）与 TX/RX 是否接反，'
                f'其次确认模块是否处于命令静默/固件升级模式')
        elif noise:
            lines.append(f'收到 {noise} 字节无法解析的数据——波特率或数据位/校验位不匹配，'
                         f'请确认模块当前波特率（已尝试枚举常见档位）')
        else:
            lines.append('串口一个字节都没收到——检查模块供电、USB 线（有的线只供电不传数据）、'
                         '以及 TX/RX 是否接反')
        return '；'.join(lines)

    def _worker(self):
        task = dict(self._task)
        lease = SerialLease(port=task['port'], baud=task['baud'], task_id=self._task_id)
        self._lease = lease
        final_stage = 'FAILED'
        final_message = ''
        try:
            self._set_stage('PREPARE', f'校验参数：{task["port"]} @ {task["baud"]}')
            self._checkpoint()

            self._set_stage('HOLD', '等待主程序数据面让出串口')
            ok, message = lease.acquire()
            if not ok:
                raise TaskFailed(message)
            self._checkpoint()

            self._set_stage('OPEN', '独占打开串口并清残帧')
            ok, message = lease.open()
            if not ok:
                raise TaskFailed(message)
            self._checkpoint()

            # 用户在前端选了型号就只问这一个（省 1~2 秒，且选错立刻报错）；
            # 没选（None）才挨个问 —— 全量探测只作为兜底，不是默认路径。
            only = [task.get('module')] if task.get('module') else None
            self._set_stage('DETECT',
                            f'识别模块型号（{"按所选型号 " + only[0] if only else "自动探测"}）')
            driver, message, attempts, note = self._detect_module(
                lease, only, allow_baud_scan=not task.get('baud_explicit'))
            if note:
                self._set_stage('DETECT', f'{message}（{note}）')
            if driver is None:
                # 未知模块：只有"只测不写"能继续——它不需要命令集，
                # 只要能读到模块自己吐出来的 NMEA 就够了。
                plan = task.get('plan')
                if plan is not None and getattr(plan, 'method', '') == 'external_rover':
                    from .drivers.passive import PassiveDriver
                    driver = PassiveDriver(lease.channel)
                    self._set_stage('DETECT',
                                    '没有驱动认领这块模块，按未知模块（只读 NMEA）继续')
                    logging.warning(f'未知模块走只测不写：{message}')
                else:
                    raise TaskFailed(message)
            self._driver = driver
            self._checkpoint()

            action = task.get('action') or 'diagnose'
            if action == 'diagnose':
                report = self._run_diagnose(driver, lease, task)
            elif action in ('write', 'restore'):
                report = self._run_write(driver, lease, task)
            else:
                raise TaskFailed(f'未知任务类型：{action}')

            final_stage = 'DONE'
            final_message = report['summary']['headline']
            self._set_stage('DONE', final_message, progress=100)

        except TaskAborted as e:
            final_stage, final_message = 'ABORTED', str(e) or '任务已取消'
            self._set_stage('ABORTED', final_message, error=final_message)
        except TaskEvicted as e:
            final_stage, final_message = 'FAILED', str(e)
            self._set_stage('FAILED', final_message, error=final_message)
            logging.error(f'标定任务被强制中断：{final_message}')
        except TaskFailed as e:
            final_stage, final_message = 'FAILED', str(e)
            self._set_stage('FAILED', final_message, error=final_message)
            logging.error(f'标定任务失败（{self._stage}）：{final_message}')
        except (DriverBug, OSError) as e:
            final_stage, final_message = 'FAILED', f'{type(e).__name__}: {e}'
            self._set_stage('FAILED', final_message, error=final_message)
            logging.error(f'标定任务异常退出（{self._stage}）：{final_message}')
        except Exception as e:                       # noqa: BLE001 - 兜住一切，绝不上抛
            final_stage, final_message = 'FAILED', f'{type(e).__name__}: {e}'
            self._set_stage('FAILED', final_message, error=final_message)
            logging.exception(f'标定任务未捕获异常（{self._stage}）')
        finally:
            # 这一行是整个功能的安全带：任何路径都必须把串口还给主程序
            try:
                lease.release(stage=final_stage, message=final_message)
            except Exception as e:                   # noqa: BLE001
                logging.error(f'归还租约时发生异常（已强制清 gate）：{e}')
                try:
                    state.calibration_hold.clear()
                    state.calibration_beat = 0.0
                    state.restart_event.set()
                except Exception:
                    pass
            with self._lock:
                self._finished_at = self._finished_at or _now_text()
                payload = self._snapshot_locked()
            self._persist(payload, force=True)
            if final_stage != 'DONE':
                self._record_events(final_stage, {'error': final_message})

    # ---------- 任务一：P0 只读诊断 ----------
    def _run_diagnose(self, driver, lease, task):
        self._set_stage('OBSERVE', f'只读读取 {driver.DISPLAY_NAME} 的配置')
        report = self._diagnose(driver, lease, task['seconds'])

        self._set_stage('PLAN', '汇总判定')
        report['summary'] = _summarize(report)
        self._result = report
        self._record_events('DONE', report)
        return report

    # ---------- 任务二/三：P1 写入 / 还原 ----------
    def _run_write(self, driver, lease, task):
        """写入链路的外壳：备份 → 交给 writer 跑 8 步 → 落库。

        为什么备份放在 service 而不是 writer：
            备份要用到 `lease.port`、`self._task_id`（落盘文件里要能对上任务），
            这些都是流程层的知识。writer 只认"给我一个 plan 和一份备份"。
        """
        plan = task['plan']
        is_restore = task.get('action') == 'restore'
        # 只测不写：模块可能是别人的流动站、也可能是没有命令集的未知模块，
        # 备份要发 12 条查询命令，对未知模块全是 2 秒超时，纯浪费时间；
        # 而且"没有写入"就不需要备份。
        readonly = plan.method == 'external_rover'

        # ---- 1. 模块身份（写进记录与租约，中断后页面才知道是哪块模块）----
        self._set_stage('BACKUP',
                        '读取备份并准备还原' if is_restore else '读取模块身份并备份标定前配置')
        ok, message, info = driver.info()
        if ok:
            plan.module = {
                'sn': info.get('sn') or '',
                'model_key': info.get('model_key') or driver.MODEL_KEY,
                'firmware': info.get('firmware') or '',
                'display_name': info.get('display_name') or driver.DISPLAY_NAME,
            }
            lease.set_module(model=info.get('display_name'), firmware=info.get('firmware'),
                             sn=info.get('sn'))
            self._save_profile(driver, info)
        else:
            # 读不到身份仍然允许写（现场常见：用户就是要覆盖一块读不出 SN 的模块），
            # 只是记录里会缺 SN/固件 —— 必须告诉用户，否则以后按 SN 查历史会查不到。
            plan.warnings.append(f'读取模块信息失败：{message}；本次记录将缺少 SN/固件')
            plan.module = {'model_key': driver.MODEL_KEY, 'firmware': ''}
        self._checkpoint()

        # ---- 2. 备份标定前配置（还原任务用现成的备份，不再覆盖它）----
        if readonly:
            snapshot = {'ok': True, 'skipped': True, 'readonly': True}
            self._set_stage('BACKUP', '只测不写：不备份、不修改模块配置')
        elif is_restore:
            snapshot = backup_mod.load()
            usable, why = backup_mod.usable(snapshot)
            if not usable:
                raise TaskFailed(f'{why}（备份文件可能在任务期间被删了）')
        else:
            snapshot = backup_mod.capture(driver, task_id=self._task_id, port=lease.port,
                                          module=dict(plan.module))
            try:
                backup_mod.save(snapshot)
            except OSError as e:
                plan.warnings.append(f'备份落盘失败：{e}（本次失败将无法自动回写）')
            if not snapshot.get('ok', True):
                plan.warnings.append(
                    '备份不完整，本次失败可能无法完整回写：'
                    + '；'.join((snapshot.get('warnings') or [])[:3]))
        self._checkpoint()

        # ---- 3. 演练模式：只算不写，让用户先核对要写进去的坐标 ----
        if task.get('dry_run'):
            report = {
                'dry_run': True,
                'task_id': self._task_id,
                'plan': plan.summary(),
                'module': dict(plan.module),
                'backup': _backup_brief(snapshot),
                'warnings': list(plan.warnings),
            }
            report['summary'] = {
                'headline': '演练完成：未向模块写入任何内容',
                'verdict': 'dry_run',
                'apc': report['plan'].get('apc'),
                'arp': report['plan'].get('arp'),
                'advice': '核对换算结果无误后，去掉「演练」再执行一次即可真正写入',
                'warnings': list(plan.warnings),
            }
            self._result = report
            return report

        # ---- 4. 交给写链路 ----
        writer = CoordinateWriter(
            driver, channel=lease.channel,
            on_stage=self._writer_stage,
            checkpoint=self._checkpoint,
            on_record=self._save_record,
            rtcm_timeout=task.get('rtcm_timeout') or DEFAULT_RTCM_TIMEOUT,
            rtcm_samples=DEFAULT_RTCM_SAMPLES,
            task_id=self._task_id,
            svin_timeout=task.get('svin_timeout'),
            fix_wait_timeout=task.get('fix_wait_timeout'))
        self._writer = writer
        try:
            result = writer.run(plan, snapshot)
        except WriteFailed as e:
            result = dict(writer.result or {})
            result.setdefault('plan', plan.summary())
            result['failed_step'] = e.step
            result['nvm_state'] = e.nvm_state
            result['error'] = e.message
            result['summary'] = _summarize_write(result, plan, failed=e.message)
            self._result = result
            # 失败的写入**不落 records**：历史坐标里必须只有"真写进去且过了校验"的值，
            # 否则用户下次选"历史坐标"会拿到一个从没生效过的坐标。原因记在 events 里。
            self._record_events('FAILED', result)
            raise TaskFailed(e.message) from e
        finally:
            self._writer = None

        result['summary'] = _summarize_write(result, plan)
        self._result = result
        self._record_events('DONE', result)
        return result

    # ---------- 写链路回调 ----------
    def _writer_stage(self, stage, message, progress=None):
        """writer 推进阶段时回调：落 state.json、刷心跳都由 `_set_stage` 统一做。"""
        self._set_stage(stage, message, progress=progress)

    def _save_record(self, record):
        """把标定记录写进 calibration.db。返回主键；失败返回 None（不阻断任务）。"""
        try:
            record_id = store.insert_record(record)
        except store.StoreError as e:
            logging.warning(f'标定记录落库失败（坐标已写入模块，不影响使用）：{e}')
            return None
        try:
            store.insert_event(self._task_id, 'INJECT', 'info',
                               f'标定记录 #{record_id}', record_id)
        except Exception as e:                       # noqa: BLE001
            logging.debug(f'记录事件失败（忽略）：{e}')
        return record_id

    # ---------- 检查点 ----------
    def _checkpoint(self):
        """状态机唯一的中断点。所有耗时步骤之间都要调一次。

        除了"用户取消"，这里还兜住一种隐蔽故障：**租约已经被看门狗强制回收**
        （我们 30 秒没喂心跳）。此时数据面已经重新打开串口，我们再发命令就是
        两个 fd 抢字节 —— 必须立刻停手，而不是继续跑一个注定读到垃圾的流程。
        """
        if state.shutdown_event.is_set():
            raise TaskAborted('进程正在退出')
        if self._cancel.is_set():
            raise TaskAborted('用户取消')
        lease = self._lease
        if lease is not None and lease.is_held and not state.calibration_hold.is_set():
            raise TaskEvicted('租约已被看门狗强制回收（30 秒无心跳），为避免与数据面抢串口，任务终止')

    # ---------- 状态推进 ----------
    def _set_stage(self, stage, message=None, progress=None, error=''):
        with self._lock:
            changed = stage != self._stage
            self._stage = stage
            if message is not None:
                self._message = str(message)
            if progress is not None:
                self._progress = int(progress)
            elif changed:
                self._progress = STAGE_PROGRESS.get(stage, self._progress)
            if error:
                self._error = str(error)
            if stage in TERMINAL_STAGES:
                self._finished_at = self._finished_at or _now_text()
                if stage == 'DONE':
                    self._progress = 100
            payload = self._snapshot_locked()

        if changed:
            logging.info(f'标定阶段 → {stage}（{STAGE_TEXT.get(stage, stage)}）：'
                         f'{self._message}')
        self._persist(payload, force=changed)

        lease = self._lease
        if lease is not None and stage not in TERMINAL_STAGES:
            lease.stage = stage
            lease.beat(force=changed)

    def _persist(self, payload, force=False):
        """原子写 state.json。默认 1 秒节流：观测阶段每秒刷新一次就够。"""
        now = time.time()
        if not force and now - self._last_persist < self._persist_interval:
            return
        self._last_persist = now
        try:
            workspace.write_json(workspace.state_path(), payload, durable=force)
        except OSError as e:
            # 落盘失败只降级成警告：进度条不更新远没有"标定因此中断"严重
            logging.warning(f'写标定状态文件失败（不影响任务）：{e}')

    def _record_events(self, stage, report):
        """把本次任务的关键结论写进 calibration_events（失败也不影响任务）。"""
        try:
            if stage == 'DONE':
                summary = (report.get('summary') or {}).get('headline', '只读诊断完成')
                store.insert_event(self._task_id, 'DONE', 'info', summary)
            else:
                message = str(report.get('error') or '')[:400]
                store.insert_event(self._task_id, stage, 'error', message)
        except store.StoreError as e:
            logging.warning(f'标定事件落库失败（不影响任务）：{e}')
        except Exception as e:                       # noqa: BLE001
            logging.debug(f'标定事件落库异常（忽略）：{e}')

    # ==================== P0 只读诊断 ====================
    def _diagnose(self, driver, lease, seconds):
        """读模块信息 → 读配置 → 观测 1005 → 比对。**全程不发任何写命令。**"""
        channel = lease.channel
        report = {
            'readonly': True,
            'task_id': self._task_id,
            'port': lease.port,
            'baud': lease.baud,
            'module': {},
            'mode': {},
            'svin': {},
            'rtcm': {},
            'coordinates': {},
            'gga': None,
            'observations': {'seconds': 0, 'frames_1005': 0, 'lines': 0, 'frames': 0},
            'warnings': [],
        }
        warn = report['warnings'].append

        # ---- 1. 模块身份（VERNO / UNIQID / RCVRMODE + 能力探测，均为查询）----
        ok, message, info = driver.info()
        if ok:
            report['module'] = {
                # 优先用 `info()` 识别到的**实际**型号：同一份手册里的 UM960/UM980/UM982
                # 可能由同一个基类认领，`driver.MODEL_KEY` 只是"谁认领的"，
                # 落库和前端展示都该是板上真实的那个型号（与第 875 行口径一致）。
                'model_key': info.get('model_key') or driver.MODEL_KEY,
                'display_name': info.get('display_name'),
                'firmware': info.get('firmware'),
                'build_date': info.get('build_date'),
                'sn': info.get('sn'),
                'sn_source': info.get('sn_source'),
                'uniq_id': info.get('uniq_id'),
                'caps': info.get('caps') or {},
            }
            lease.set_module(model=info.get('display_name'),
                             firmware=info.get('firmware'), sn=info.get('sn'))
            self._save_profile(driver, info)
        else:
            warn(f'读取模块信息失败：{message}')
            report['module'] = {'display_name': driver.DISPLAY_NAME,
                                'model_key': driver.MODEL_KEY}
        self._checkpoint()

        # ---- 2. 工作模式 ----
        ok, message, mode = driver.get_mode()
        if ok:
            # `source` 是语义来源（配置回读），不是命令名 —— 命令名由各厂商驱动自己管，
            # 这里写死一个 `PQTM...` 的话，换成和芯星通/以后别的型号就对不上了。
            report['mode'] = {'text': mode.get('mode'), 'code': mode.get('mode_code'),
                              'source': 'config_readback'}
        else:
            warn(f'读取工作模式失败：{message}')
            report['mode'] = {'text': 'unknown', 'code': None}
        self._checkpoint()

        # ---- 3. 当前坐标配置（Survey-in 模式下 ECEF 恒为 0，不是收敛结果）----
        ok, message, svin = driver.get_svin_config()
        if ok:
            report['svin'] = svin
            if svin.get('mode') == 'survey_in':
                warn('模块处于 Survey-in 模式：CFGSVIN 里的 ECEF 是配置值（恒为 0），'
                     '收敛结果只能从 1005 或 SVINSTATUS 里读')
        else:
            warn(f'读取坐标配置失败：{message}')
        self._checkpoint()

        # ---- 4. RTCM 配置回读（看 MSM 类型，不解释每一个保留字段）----
        ok, message, rtcm = driver.read_rtcm_config()
        if ok:
            fields = rtcm.get('fields') or []
            report['rtcm'] = {
                'msm_type': _safe_int(fields[0]) if fields else None,
                'fields': fields,
                'raw': rtcm.get('raw', ''),
            }
        else:
            # rover 模式下某些固件会回 ERROR,1（"没配过"），不是故障
            report['rtcm'] = {'msm_type': None, 'fields': [], 'raw': '',
                              'error': message}
            logging.debug(f'RTCM 配置回读失败（在移动站模式下属正常）：{message}')
        self._checkpoint()

        # ---- 5. 观测：抓 1005（基准站坐标）与 GGA（质量旁证）----
        broadcast = self._observe_1005(lease, seconds, report)

        # ---- 6. 比对 ----
        report['coordinates'] = _compare(report, broadcast, warn)
        report['gga'] = _gga_summary(channel)
        report['observations'].update({
            'lines': channel.counters.get('lines', 0),
            'frames': channel.counters.get('frames', 0),
            'noise': channel.counters.get('noise', 0),
        })
        if channel.counters.get('noise', 0) > channel.counters.get('lines', 0):
            warn('丢字节数超过收到的行数：波特率可能不对，或串口线受干扰')
        return report

    def _observe_1005(self, lease, seconds, report):
        """静默观测 `seconds` 秒，返回最后一条 1005 的解码结果。

        为什么用 `read_line` 而不是直接读字节：
            `Channel._next_line()` 已经是"混合流拆分器"（0xD3 走 RTCM、$ 走 NMEA、
            其它当噪声），直接用它的结果比自己再写一遍帧同步可靠得多。
            解出来的 1005 会落在 `cache['rtcm_frames']` 里，这里按键增量消费。
        """
        channel = lease.channel
        frames_seen = channel.counters.get('frames', 0)
        start = time.time()
        deadline = start + max(1.0, float(seconds))
        count = 0
        last = None
        last_at = 0.0

        while time.time() < deadline:
            self._checkpoint()
            channel.read_line(0.5)

            total = channel.counters.get('frames', 0)
            if total > frames_seen:
                pool = channel.cache.get('rtcm_frames') or []
                delta = min(total - frames_seen, len(pool))
                for raw in (pool[-delta:] if delta else []):
                    parsed = decode_frame(raw)
                    if parsed is None:
                        continue
                    count += 1
                    last = parsed
                    last_at = time.time()
                frames_seen = total

            elapsed = time.time() - start
            lease.beat()
            progress = 45 + int(45 * min(1.0, elapsed / max(1.0, float(seconds))))
            self._set_stage('OBSERVE',
                            f'观测中 {elapsed:.0f}/{seconds:.0f} 秒，'
                            f'已收到 {count} 条 1005',
                            progress=progress)

        report['observations']['seconds'] = round(time.time() - start, 1)
        report['observations']['frames_1005'] = count
        if last is not None:
            last['last_seen_in'] = round(time.time() - last_at, 2)
        return last

    @staticmethod
    def _save_profile(driver, info):
        """把驱动里的能力矩阵同步进 module_profiles（页面没连硬件时也能列型号）。"""
        try:
            store.save_profile(driver.MODEL_KEY,
                               display_name=driver.DISPLAY_NAME,
                               match_prefix=driver.MATCH_PREFIX,
                               caps=info.get('caps') or driver.runtime_caps)
        except store.StoreError as e:
            logging.debug(f'型号能力落库失败（忽略）：{e}')
        except Exception as e:                       # noqa: BLE001
            logging.debug(f'型号能力落库异常（忽略）：{e}')


# ==================== 结果组装 ====================
def _compare(report, broadcast, warn):
    """把"配置坐标"与"1005 播发坐标"摆在一起，给出结论与偏差。

    方向问题：1005 播发的与 `PQTMCFGSVIN` 写进去的是同一个点（同一根天线的馈点），
    所以两者可以直接相减，不接受任何"天线高偏移"解释。
    如果模块实际播发值与你填的坐标差了一个天线高，说明填错了点（例如填成了地面标石）。
    """
    svin = report.get('svin') or {}
    result = {
        'configured': None,
        'broadcast': None,
        'delta': None,
        'verdict': 'unknown',
        'threshold': COORD_MATCH_THRESHOLD,
        'advice': '',
    }

    configured = None
    if svin.get('mode') == 'fixed':
        x, y, z = svin.get('ecef_x'), svin.get('ecef_y'), svin.get('ecef_z')
        if None not in (x, y, z) and max(abs(x), abs(y), abs(z)) > 1.0:
            configured = {'x': x, 'y': y, 'z': z, 'source': 'config_readback'}
            configured.update(_geodetic(x, y, z))
        else:
            warn('坐标模式是「固定」，但回读的 ECEF 全为 0 —— 多半是刚切模式还没写坐标')
    result['configured'] = configured

    if broadcast is None:
        mode_text = (report.get('mode') or {}).get('text')
        if mode_text == 'rover':
            result['verdict'] = 'no_broadcast'
            result['advice'] = ('模块当前是移动站模式，本来就不播发 1005。'
                                '要核对基准站坐标，先把模块切到基站模式（属 P1 的写操作）')
        elif mode_text == 'base':
            result['verdict'] = 'no_broadcast'
            result['advice'] = ('模块已是基站模式但没收到 1005：检查 RTCM3-1005 的输出速率'
                                '（PQTMCFGMSGRATE 里应为 1），或串口是否只接了 TX')
        else:
            result['verdict'] = 'no_broadcast'
            result['advice'] = '没有收到 1005 报文，无法核对播发坐标'
        return result

    broadcast_view = {
        'x': broadcast['ecef_x'],
        'y': broadcast['ecef_y'],
        'z': broadcast['ecef_z'],
        'station_id': broadcast.get('station_id'),
        'itrf_year': broadcast.get('itrf_year'),
        'source': 'RTCM3-1005',
    }
    broadcast_view.update(_geodetic(broadcast['ecef_x'], broadcast['ecef_y'],
                                    broadcast['ecef_z']))
    result['broadcast'] = broadcast_view

    if configured is None:
        result['verdict'] = 'no_config'
        result['advice'] = ('模块在播 1005，但 CFGSVIN 里没有固定坐标'
                            '（Survey-in 模式或未配置），无从比对 —— '
                            '播发值本身是可信的，可直接作为参考坐标')
        return result

    dx = broadcast['ecef_x'] - configured['x']
    dy = broadcast['ecef_y'] - configured['y']
    dz = broadcast['ecef_z'] - configured['z']
    axis = max_axis_delta(broadcast['ecef_x'], broadcast['ecef_y'], broadcast['ecef_z'],
                          configured['x'], configured['y'], configured['z'])
    norm = (dx * dx + dy * dy + dz * dz) ** 0.5
    result['delta'] = {'dx': round(dx, 4), 'dy': round(dy, 4), 'dz': round(dz, 4),
                       'max_axis': round(axis, 4), 'norm': round(norm, 4)}

    if axis <= COORD_MATCH_THRESHOLD:
        result['verdict'] = 'match'
        result['advice'] = f'配置坐标与 1005 播发值一致（最大轴差 {axis * 1000:.2f} mm）'
    else:
        result['verdict'] = 'mismatch'
        result['advice'] = (f'配置坐标与播发值相差 {norm:.3f} m（最大轴差 {axis:.3f} m）。'
                            f'若差值接近天线高，多半是把地面标石坐标填成了天线坐标')
    return result


def _geodetic(x, y, z):
    try:
        lat, lon, height = ecef_to_geodetic(x, y, z)
        return {'lat': round(lat, 9), 'lon': round(lon, 9), 'height': round(height, 4)}
    except Exception:                                # noqa: BLE001
        return {}


def _gga_summary(channel):
    """抓最近一条 GGA 的质量位/卫星数，作为"模块是否真的在定位"的旁证。

    没有 GGA 不算错误：基站模式下 NMEA 默认是关掉的，这时只能靠 1005 判断。
    """
    gga = (channel.cache or {}).get('gga')
    if not gga:
        return None
    quality = gga.get('quality')
    return {
        'quality': quality,
        'quality_text': _QUALITY_TEXT.get(quality, '未知'),
        'num_sats': gga.get('num_sats'),
        'hdop': gga.get('hdop'),
        'height': gga.get('height'),
        'lat': gga.get('lat'),
        'lon': gga.get('lon'),
        'raw': gga.get('raw'),
    }


def _backup_brief(snapshot):
    if not snapshot:
        return None
    return {
        'created_at': snapshot.get('created_at'),
        'mode': (snapshot.get('mode') or {}).get('text'),
        'svin_mode': (snapshot.get('svin') or {}).get('mode'),
        'complete': bool(snapshot.get('ok', True)),
    }


def _write_advice(result, verdict, failed):
    """失败/警告时给用户"下一步做什么"。空串表示不需要额外提示。"""
    nvm = result.get('nvm_state') or ''
    plan_info = result.get('plan') or {}
    method = plan_info.get('method') or ''
    if failed:
        if method == 'external_rover':
            return ('本次没有向模块写入任何配置（NVM 未改动），可以直接重试；'
                    '若中途临时切过模式，收尾时已自动切回。反复失败请看上面卡住的步骤')
        if nvm == 'unknown':
            return '模块当前处于临时状态：断电重启后会回到标定前的配置，请重新标定'
        if nvm == 'saved':
            return ('配置已保存进模块，但后续校验没过；可断电重启后重新校验，'
                    '或点「还原标定前配置」退回')
        if nvm == 'reverted':
            return '已恢复为标定前的配置，可以直接重试；若反复失败请看上面的失败步骤'
        return '没有改动模块配置，可以直接重试'
    if method == 'external_rover':
        # 只测不写：结论就是"坐标"，模块的持久配置没动。下一步是用户自己的事，
        # 必须把"拿这个坐标去设基准站"和"别动天线"两句话说清楚。
        return ('坐标测量完成：本次没有向模块写入任何配置（NVM 未改动）。'
                '可以用上面这组 ECEF / 经纬度手动把基准站模块设为固定坐标；'
                '天线位置已经确定，搬动或转动过就必须重新测')
    if verdict == 'warning':
        return '1005 偏差在 5~50 cm，模块可能做了内部平滑；坐标可用，建议在流动站侧核对一次'
    if verdict == 'skipped':
        return '还原完成，模块已回到标定前的工作状态'
    # 复用历史坐标（A1 分支）：没有重新测量，必须把"为什么它能用"和
    # "什么情况下它就不成立了"同时说清楚，否则用户会以为这是一次新标定。
    cors_info = result.get('cors') or {}
    if cors_info.get('reused'):
        return ('已用库内历史坐标把模块设为基准站（本次未连接 CORS、未重新测量）。'
                '【前提】天线必须与历史标定时**完全同一位置**——搬动、转动、更换底座或'
                '支架修过，这组坐标就作废了，必须重新做一次 CORS 测量。'
                '【注意】1005 校验只证明坐标写进去了，不证明它还准。'
                '接下来可把这个串口设为数据源转发到 NTRIP，或开启本地 Caster 供下游流动站接入')
    # 成功：必须明确告诉用户"接下来干什么"。
    # 标定只是把这个串口配成了合格的基准站，数据要不要往外发是另一件事：
    # 直接转发到 NTRIP、或开本地 Caster 给下游流动站用，都得用户点一下。
    if ((result.get('plan') or {}).get('mode') or '') == 'base':
        return ('模块写入完成，RTCM-1005 报文坐标与写入值链路校验通过。'
                '【注意】1005 仅证明模块成功播发该组坐标，不能代表天线绝对地理精度，'
                '请使用控制点 / 多源 CORS 做外部核验。'
                '接下来可把这个串口设为数据源转发到 NTRIP，或开启本地 Caster 供下游流动站接入；'
                '转发前请先确认天线没有被移动过')
    return ''


def _summarize_write(result, plan, failed=''):
    """写入/还原任务的结论。失败时也要给出"停在哪一步、模块现在是什么状态"。"""
    plan_info = result.get('plan') or {}
    verdict = result.get('verdict') or ('failed' if failed else 'unknown')
    step = result.get('failed_step') or result.get('error_step') or ''
    if failed:
        headline = f'标定失败（停在 {step or "?"}）：{str(failed).split("；")[0]}'
    elif plan.kind == 'restore':
        headline = '还原完成：模块已回到标定前的配置'
    elif plan.method == 'external_rover':
        headline = '坐标测量完成：模块的持久配置未被修改，可用该坐标手动把基准站设为固定站'
    elif (result.get('cors') or {}).get('reused'):
        # 复用历史坐标不是一次新标定：措辞里不能出现"标定完成"，否则用户会以为
        # 我们刚测了一遍，下次天线搬了也不会想起来要重测。
        headline = ('已用历史坐标把模块设为基准站（未重新测量）：'
                    '1005 播发坐标与写入值链路校验通过')
    else:
        headline = {
            # 措辞是硬要求（附录H.1）：1005 只能证明"播发值与写入值一致"，
            # 不能写成"标定坐标验证正确"——那句话会被理解成"绝对位置已经准了"，
            # 而 1005 一个字节都不包含外部精度信息。
            'match': '模块写入完成：RTCM-1005 播发坐标与写入值链路校验通过',
            'warning': '标定完成（有警告）：1005 播发坐标与写入值有偏差',
            'skipped': '标定完成（未做 1005 校验）',
            'unknown': '标定完成，结论待定',
        }.get(verdict, '标定完成')
    return {
        'headline': headline,
        'verdict': verdict,
        'ok': not failed,
        'kind': plan.kind,
        'step': step,
        'mode': plan.mode,
        'svin_mode': plan.svin_mode,
        'apc': plan_info.get('apc'),
        'arp': plan_info.get('arp'),
        'geodetic': plan_info.get('geodetic'),
        'broadcast': result.get('broadcast'),
        'delta': result.get('delta'),
        'nvm_state': result.get('nvm_state') or '',
        'mode_ack': result.get('mode_ack') or '',
        'record_id': result.get('record_id'),
        'steps': result.get('steps') or [],
        'warnings': list(result.get('warnings') or []),
        'advice': _write_advice(result, verdict, failed),
    }


def _summarize(report):
    """一句话结论 + 关键数字，给页面顶部和日志用。"""
    module = report.get('module') or {}
    coords = report.get('coordinates') or {}
    verdict = coords.get('verdict')
    observations = report.get('observations') or {}

    headline_map = {
        'match': '配置坐标与播发坐标一致',
        'mismatch': '配置坐标与播发坐标不一致',
        'no_broadcast': '未收到 1005，无法核对播发坐标',
        'no_config': '模块在播 1005，但没有固定坐标配置',
        'unknown': '诊断完成，结论待定',
    }
    headline = headline_map.get(verdict, '只读诊断完成')
    return {
        'headline': headline,
        'verdict': verdict,
        'module': module.get('display_name') or driver_name(module),
        'firmware': module.get('firmware', ''),
        'sn': module.get('sn', ''),
        'mode': (report.get('mode') or {}).get('text'),
        'configured': coords.get('configured'),
        'broadcast': coords.get('broadcast'),
        'delta': coords.get('delta'),
        'advice': coords.get('advice', ''),
        'samples_1005': observations.get('frames_1005', 0),
        'seconds': observations.get('seconds', 0),
        'warnings': list(report.get('warnings') or []),
    }


def driver_name(module):
    return module.get('model_key') or '未知模块'


def _safe_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clean_module(module):
    """把前端传来的型号键规整成 `'lg290p'` 形态；没指定一律返回 None。

    返回 None 的语义是"没指定，让 `match_driver` 挨个问" —— 不能返回空字符串，
    那会被当成"只试这个空型号"，一个候选都匹配不上。

    前端"自动识别"下发的是 `null`（见 `buildPayload`），`__auto__` 是 DOM 里的
    option value，不发出去；这两个都按"没指定"处理。`'passive'` 不在此列——
    它是 `passive.py` 的真实 `MODEL_KEY`，虽然刻意不注册进注册表，但若哪天
    从别处（CLI / 旧缓存 / 手写请求）传进来，应当原样保留而不是退化成自动识别。
    """
    text = str(module or '').strip().lower()
    if not text or text in ('unknown', 'auto', 'none', '__auto__'):
        return None
    return text


def _module_caps(module):
    """取用户**明确选定**型号的能力表；没选/选了未知型号返回 None。

    返回 None 的语义是"不知道是哪块模块，放行到运行时再说"：此时不能替用户
    猜能力，最后由 `writer.py` 在 DETECT 之后用真实驱动再判一次。这与
    `_clean_module()` 的 None 语义一致，两边必须同时改。
    """
    key = _clean_module(module)
    if not key:
        return None
    try:
        driver = driver_manager.get_driver_by_id(key)
    except Exception as e:                           # noqa: BLE001 - 取能力失败不该拦住标定
        logging.warning(f'取型号 {key} 的能力表失败（按未知处理）：{e}')
        return None
    if driver is None:
        return None
    return {
        'name': getattr(driver, 'DISPLAY_NAME', key),
        'caps': dict(getattr(driver, 'CAPS', {}) or {}),
        'command_count': len(getattr(driver, 'CMD', {}) or {}),
    }


_NO_COMMAND_ADVICE = '只能做「只观测」或「外接流动站测坐标」，无法写入坐标'


def _reject_write_without_fixed(module):
    """「写坐标」要能下发命令，还要有写固定坐标的能力。

    前端已经把「写入历史坐标」「Survey-in」灰掉了，但灰态只是提示 ——
    直接 POST 接口、或前端 JS 被旧缓存住，都还能走到这里。所以入口必须再判一次，
    否则会在 `writer.py` 的 BACKUP 阶段才炸，用户白等十几秒还占着串口。

    未指定型号时返回 None 放行：真正的判断在 `writer.py` 拿到驱动之后。
    """
    info = _module_caps(module)
    if info is None:
        return None
    if info['command_count'] == 0:
        return f'{info["name"]} 没有可下发命令，{_NO_COMMAND_ADVICE}'
    if not info['caps'].get('fixed'):
        return f'{info["name"]} 不支持写入固定坐标，请改用「Survey-in 自收敛」或「CORS 差分」'
    return None


# 能力位 → 给人看的中文名。报错时直接说"不支持切换工作模式"，
# 而不是把 `set_mode` 这种内部键抛给用户。
_CAP_TEXT = {
    'dual_mode': '双模切换',
    'set_mode': '切换工作模式',
    'svin': '自收敛（Survey-in）',
    'fixed': '写入固定坐标',
    'msg_rate': '配置报文频率',
    'rtcm_out': '配置 RTCM 输出',
}


def _reject_without_caps(module, *needed):
    """按需检查若干能力位；未指定型号时放行（运行时再判）。"""
    info = _module_caps(module)
    if info is None:
        return None
    caps = info['caps']
    if info['command_count'] == 0:
        return f'{info["name"]} 没有可下发命令，{_NO_COMMAND_ADVICE}'
    missing = [_CAP_TEXT.get(key, key) for key in needed if not caps.get(key)]
    if not missing:
        return None
    return f'{info["name"]} 不支持「{"」「".join(missing)}」，无法使用这种方式标定'


def _now_text():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _new_task_id():
    return time.strftime('%Y%m%d-%H%M%S') + f'-{os.getpid() % 1000:03d}'


def _config_serial_port():
    try:
        from ..core.config import get_config
        return str((get_config() or {}).get('serial_port') or '').strip()
    except Exception:                                # noqa: BLE001
        return ''


# ==================== 模块级单例（给 Web / CLI 用） ====================
_SERVICE = None
_SERVICE_LOCK = threading.Lock()


def get_service():
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            # 建表放在这里而不是 import 期：import 必须零副作用（契约测试锁着），
            # 而"真的要用标定"这个动作来建库完全合理。
            try:
                store.init_db()
            except Exception as e:                   # noqa: BLE001 - 建库失败不该阻止诊断
                logging.warning(f'标定库初始化失败（事件与记录将不落库）：{e}')
            _SERVICE = CalibrationService()
        return _SERVICE


def start_diagnose(port=None, baud=None, seconds=DEFAULT_OBSERVE_SECONDS, module=None):
    return get_service().start_diagnose(port=port, baud=baud, seconds=seconds,
                                        module=module)


def start_write(port=None, baud=None, ecef=None, lat=None, lon=None, height=0.0,
                method='manual', msm=4, antenna=None, remark='',
                dry_run=False, rtcm_timeout=DEFAULT_RTCM_TIMEOUT, module=None,
                coord_msg='1005', arp=None):
    """写入历史坐标（阶段3 的对外入口，阶段4 的 Web 接口直接调这个）。

    `arp=` 是「复用历史坐标」：库里存的就是当年写进模块的值，直通写入，
    Web 接口的 `method='manual'` 分支会把它透传进来。
    """
    return get_service().start_write(
        port=port, baud=baud, ecef=ecef, lat=lat, lon=lon, height=height,
        method=method, msm=msm, antenna=antenna, remark=remark,
        dry_run=dry_run, rtcm_timeout=rtcm_timeout, module=module,
        coord_msg=coord_msg, arp=arp)


def start_restore(port=None, baud=None, rtcm_timeout=DEFAULT_RTCM_TIMEOUT):
    """一键还原标定前配置。"""
    return get_service().start_restore(port=port, baud=baud,
                                       rtcm_timeout=rtcm_timeout)


def start_survey_in(port=None, baud=None, profile='standard', msm=4, antenna=None,
                    remark='', distance=None, rtcm_timeout=DEFAULT_RTCM_TIMEOUT,
                    svin_timeout=None, module=None, coord_msg='1005', resume=True):
    """模块自收敛标定（方案 P3，亚米级）。resume=True 时已收敛过就直接复用。"""
    return get_service().start_survey_in(
        port=port, baud=baud, profile=profile, msm=msm, antenna=antenna,
        remark=remark, distance=distance, rtcm_timeout=rtcm_timeout,
        svin_timeout=svin_timeout, module=module, coord_msg=coord_msg, resume=resume)


def start_cors(port=None, baud=None, host='', cors_port=None, mountpoint='',
               user='', password='', duration='standard', msm=4, antenna=None,
               remark='', rtcm_timeout=DEFAULT_RTCM_TIMEOUT, fix_wait_timeout=None,
               module=None, coord_msg='1005', measure_only=False, on_slow='adopt',
               reuse_arp=None):
    """CORS 高精度标定（方案 P1，厘米级）；measure_only=True 时只测不写。

    `on_slow` 决定"到时长仍未收敛"怎么办（§14 #14）：adopt 采用当前值并标注，
    fail 直接失败、不写坐标。
    `reuse_arp=` 走标定流程 A1 的分支：库内已有该天线坐标且未移动 → 跳过测量。

    注意：本函数是**模块级转发**，参数表必须与 `CalibrationService.start_cors`
    完全一致。此前漏掉 `on_slow` 会让 Web 接口一调用就 TypeError 500 ——
    标定的主入口整个不可用，属于必须保证同步的地方。
    """
    return get_service().start_cors(
        port=port, baud=baud, host=host, cors_port=cors_port, mountpoint=mountpoint,
        user=user, password=password, duration=duration, msm=msm, antenna=antenna,
        remark=remark, rtcm_timeout=rtcm_timeout, fix_wait_timeout=fix_wait_timeout,
        module=module, coord_msg=coord_msg, measure_only=measure_only,
        on_slow=on_slow, reuse_arp=reuse_arp)


def backup_info():
    """给页面看的备份摘要（不存在时返回 None）。"""
    snapshot = backup_mod.load()
    if not snapshot:
        return None
    usable, why = backup_mod.usable(snapshot)
    return {
        'created_at': snapshot.get('created_at'),
        'port': snapshot.get('port'),
        'mode': (snapshot.get('mode') or {}).get('text'),
        'svin_mode': (snapshot.get('svin') or {}).get('mode'),
        'complete': bool(snapshot.get('ok', True)),
        'usable': usable,
        'text': backup_mod.describe(snapshot),
        'reason': '' if usable else why,
    }


def status():
    return get_service().status()


def abort():
    return get_service().abort()


_STARTUP = {'checked': False, 'notice': None}


def on_startup():
    """进程启动钩子：判定"上次是否被硬中断"并**标记 INTERRUPTED**（方案 §14 #20 / #30）。

    三件事，顺序不能换：

    1. `store.init_db()` —— 顺带把老库按 `PRAGMA user_version` 增量迁移到当前
       schema。升级到新版本后，这是唯一保证"旧库缺列不会在读列时炸"的地方，
       而且它必须发生在任何读库动作之前。
    2. 判定残留租约（`lease.json` 非终态 + 持有者已死）。
    3. 把事实落到页面与数据库：租约改终态、写一条 `INTERRUPTED` 事件、
       把服务状态置成 `INTERRUPTED` 让横幅出来。

    刻意**不做任何自动修复**：此刻串口还没被打开，任何"自动改模块"都是盲操作。
    模块可能停在"切了基准站但坐标是旧的"这种中间态，只有用户知道该重标还是还原。

    幂等：同一个进程里重复调用只跑一次（Web 工厂可能被测试重复创建）。
    """
    if _STARTUP['checked']:
        return _STARTUP['notice']
    _STARTUP['checked'] = True

    # 1) 残留租约（先判，下面的建表要不要做取决于它）
    lease = leftover_lease()
    # 2) schema 迁移（升级兼容，方案 §12.1.1）。
    #    只在库**已经存在**或确实要写事件时才建表/迁移：库里没数据时提前建一个
    #    空库没有任何收益，只会让"没标过定"的环境凭空多出一个文件。
    if lease or os.path.exists(store.CALIBRATION_DB):
        try:
            store.init_db()
        except Exception as e:                       # noqa: BLE001 - 迁移失败不影响启动
            logging.warning(f'标定库初始化/迁移失败（标定页可能读不到历史）：{e}')

    if not lease:
        return None
    stage = str(lease.get('stage') or '?')
    module = lease.get('module') or {}
    message = (f'上次标定在「{STAGE_TEXT.get(stage, stage)}」阶段被中断'
               f'（端口 {lease.get("port") or "?"}'
               + (f'，模块 {module.get("sn")}' if module.get('sn') else '')
               + '）。模块可能停在中间配置上，建议重新标定或先做一次只读诊断')
    logging.warning(message)

    # 3a) 租约改终态：不改的话每次开机都会再报一次，最后没人再看这条提示
    try:
        payload = dict(lease)
        payload.update({
            'stage': 'INTERRUPTED', 'held': False,
            'message': message,
            'finished_at': _now_text(),
            'interrupted_stage': stage,
        })
        workspace.write_json(workspace.lease_path(), payload)
    except OSError as e:
        logging.warning(f'标记中断租约失败（页面仍会提示）：{e}')

    # 3b) 事件入库（失败不影响启动）
    try:
        store.insert_event(str(lease.get('task_id') or ''), 'INTERRUPTED', 'warn',
                           message[:400])
    except Exception as e:                           # noqa: BLE001
        logging.debug(f'记录中断事件失败（忽略）：{e}')

    # 3c) 服务状态置 INTERRUPTED，页面据此显示横幅
    notice = CalibrationService._interrupted_info(lease, message)
    _STARTUP['notice'] = notice
    try:
        get_service().mark_interrupted(lease, message)
    except Exception as e:                           # noqa: BLE001
        logging.debug(f'标记中断状态失败（忽略）：{e}')
    return notice


def interrupted_notice():
    """给 Web 层读的"上次被中断"摘要（没有则 None）。"""
    return _STARTUP['notice']


# ==================== 命令行入口（真机验证用） ====================
def _print_report(report):
    module = report.get('module') or {}
    mode = report.get('mode') or {}
    svin = report.get('svin') or {}
    coords = report.get('coordinates') or {}
    summary = report.get('summary') or {}
    obs = report.get('observations') or {}

    print('\n=== 模块 ===')
    print(f'  型号      {module.get("display_name") or "?"}'
          f'（{module.get("model_key") or "?"}）')
    print(f'  固件      {module.get("firmware") or "未读到"}'
          f'  构建 {module.get("build_date") or "?"}')
    print(f'  SN        {module.get("sn") or "未读到"}（来源 {module.get("sn_source") or "无"}）')
    caps = module.get('caps') or {}
    if caps:
        print('  能力      ' + '  '.join(f'{k}={v}' for k, v in sorted(caps.items())))

    print('\n=== 当前配置（只读回读）===')
    print(f'  工作模式  {mode.get("text")}（{mode.get("code")}）')
    print(f'  坐标模式  {svin.get("mode")}（{svin.get("mode_code")}）')
    if svin.get('mode') == 'fixed':
        print(f'  配置 ECEF {svin.get("ecef_x")}, {svin.get("ecef_y")}, {svin.get("ecef_z")}')
    if svin.get('mode') == 'survey_in':
        print(f'  Survey-in {svin.get("count")} 次 / 精度门限 {svin.get("acc_limit")} m')
    rtcm = report.get('rtcm') or {}
    if rtcm.get('fields'):
        print(f'  RTCM      MSM{rtcm.get("msm_type")}  原始字段 {rtcm.get("fields")}')

    print(f'\n=== 观测（{obs.get("seconds")} 秒）===')
    print(f'  1005 条数 {obs.get("frames_1005")}，NMEA {obs.get("lines")} 行，'
          f'RTCM 帧 {obs.get("frames")} 个，噪声 {obs.get("noise")} 字节')
    gga = report.get('gga')
    if gga:
        print(f'  GGA      质量 {gga.get("quality")}({gga.get("quality_text")})，'
              f'卫星 {gga.get("num_sats")}，HDOP {gga.get("hdop")}')
    else:
        print('  GGA      无（基站模式默认关闭 NMEA，正常）')

    print('\n=== 坐标比对 ===')
    configured, broadcast, delta = coords.get('configured'), coords.get('broadcast'), coords.get('delta')
    if configured:
        print(f'  配置值    {configured["x"]:.4f}, {configured["y"]:.4f}, {configured["z"]:.4f}')
        if configured.get('lat') is not None:
            print(f'            {configured["lat"]:.9f}, {configured["lon"]:.9f}, '
                  f'{configured["height"]:.3f} m')
    else:
        print('  配置值    （无固定坐标）')
    if broadcast:
        print(f'  1005 播发 {broadcast["x"]:.4f}, {broadcast["y"]:.4f}, {broadcast["z"]:.4f}'
              f'  站号 {broadcast.get("station_id")}')
        if broadcast.get('lat') is not None:
            print(f'            {broadcast["lat"]:.9f}, {broadcast["lon"]:.9f}, '
                  f'{broadcast["height"]:.3f} m')
    else:
        print('  1005 播发 （未收到）')
    if delta:
        print(f'  偏差      dx={delta["dx"]:+.4f} dy={delta["dy"]:+.4f} '
              f'dz={delta["dz"]:+.4f}  最大轴 {delta["max_axis"]:.4f} m  '
              f'三维 {delta["norm"]:.4f} m')

    print(f'\n结论：{summary.get("headline") or "?"}'
          f'（verdict={coords.get("verdict")}）')
    if summary.get('advice'):
        print(f'建议：{summary["advice"]}')
    for item in summary.get('warnings') or []:
        print(f'  注意：{item}')


def _print_write_result(report):
    """写链路结果的终端回显。**逐步骤打勾**，用户能一眼看出停在哪一步。"""
    summary = report.get('summary') or {}
    plan = report.get('plan') or {}
    steps = report.get('steps') or []
    verify = report.get('verify') or {}

    print('\n=== 坐标 ===')
    apc, arp = plan.get('apc'), plan.get('arp')
    if apc:
        print(f'  写入坐标  {apc["x"]:.4f}, {apc["y"]:.4f}, {apc["z"]:.4f}')
    if arp and arp != apc:
        print(f'  实际写入  {arp["x"]:.4f}, {arp["y"]:.4f}, {arp["z"]:.4f}'
              f'（{plan.get("conversion", {}).get("formula") or ""}）')
    geo = plan.get('geodetic')
    if geo:
        print(f'  经纬高    {geo["lat"]:.9f}, {geo["lon"]:.9f}, {geo["height"]:.3f} m')

    print('\n=== 执行步骤 ===')
    for item in steps:
        mark = '✓' if item.get('ok') else '×'
        print(f'  {mark} {item.get("stage"):<12} {item.get("message")}')

    print('\n=== 校验 ===')
    print(f'  模式确认  {report.get("mode_ack") or "未记录"}')
    print(f'  NVM 状态  {report.get("nvm_state") or "?"}')
    delta = report.get('delta')
    broadcast = report.get('broadcast')
    if broadcast:
        print(f'  1005 播发 {broadcast["x"]:.4f}, {broadcast["y"]:.4f}, '
              f'{broadcast["z"]:.4f}（站号 {broadcast.get("station_id")}）')
    if delta:
        print(f'  偏差      dx={delta["dx"]:+.4f} dy={delta["dy"]:+.4f} '
              f'dz={delta["dz"]:+.4f}  最大轴 {delta["max_axis"]:.4f} m')
    if verify.get('frames_1005') is not None:
        print(f'  1005 条数 {verify.get("frames_1005")}（RTCM 总帧 {verify.get("frames_total")}）')

    print(f'\n结论：{summary.get("headline") or "?"}（verdict={summary.get("verdict")}）')
    if summary.get('advice'):
        print(f'建议：{summary["advice"]}')
    if summary.get('record_id'):
        print(f'记录：#{summary["record_id"]}')
    for item in summary.get('warnings') or []:
        print(f'  注意：{item}')


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='基准站标定：--selftest 离线自测；不带参数 = P0 只读诊断；'
                    '--write-ecef / --write-latlon = P1 写入并全流程校验')
    parser.add_argument('--port', help='串口，例如 COM3 或 /dev/ttyUSB0；不填则用配置里的 serial_port')
    parser.add_argument('--baud', type=int, help=f'波特率，默认 {DEFAULT_BAUD}')
    parser.add_argument('--seconds', type=int, default=DEFAULT_OBSERVE_SECONDS,
                        help=f'只读诊断观测 1005 的秒数，默认 {DEFAULT_OBSERVE_SECONDS}')
    parser.add_argument('--json', action='store_true', help='输出 JSON（机器解析用）')
    parser.add_argument('--selftest', action='store_true', help='离线自测（假模块，不需要硬件）')
    parser.add_argument('--write-selftest', action='store_true',
                        help='离线自测写链路（假模块，含保存失败回写、一键还原等 7 个场景）')
    parser.add_argument('--gate-selftest', action='store_true',
                        help='真机联调：验证标定期间数据面暂停、结束后自动恢复、看门狗兜底')
    parser.add_argument('--status', action='store_true', help='打印当前任务状态后退出')

    write_group = parser.add_argument_group('P1 写入（真机会改模块配置并重启模块）')
    write_group.add_argument('--write-ecef', metavar='X,Y,Z',
                             help='直接给 ECEF（即要写进模块的坐标），如 '
                                  '-2519265.0514,4849534.9045,3277834.6432')
    write_group.add_argument('--write-latlon', metavar='LAT,LON[,H]',
                             help='给经纬高（度/米），内部先转 ECEF 再写')
    write_group.add_argument('--msm', default='4',
                             help='MSM 档位 4/5/6/7；keep = 沿用模块当前配置（不下发）。默认 4')
    write_group.add_argument('--coord-msg', default='1005',
                             choices=['1005', '1006', 'keep'],
                             help='坐标报文：1005（默认）/ 1006（带天线高度）/ '
                                  'keep（沿用模块当前配置，不下发）')
    write_group.add_argument('--remark', default='', help='记录备注')
    write_group.add_argument('--dry-run', action='store_true',
                             help='演练：只读配置 + 算换算，不写模块')
    write_group.add_argument('--rtcm-timeout', type=float, default=DEFAULT_RTCM_TIMEOUT,
                             help=f'等 1005 的秒数，默认 {DEFAULT_RTCM_TIMEOUT:.0f}')
    write_group.add_argument('--restore', action='store_true',
                             help='还原标定前配置（读 .calibration/backup.json）')
    write_group.add_argument('--survey-in', action='store_true',
                             help='模块自收敛标定（无网可用，亚米级）：模块自己收敛出坐标'
                                  '再自动写入并切基准站')
    write_group.add_argument('--profile', default='standard',
                             choices=sorted(SURVEY_PROFILES),
                             help='Survey-in 档位：fast=600次/3.0m、standard=1800次/1.5m、'
                                  'high=3600次/0.8m（默认 standard）')
    write_group.add_argument('--survey-timeout', type=float, default=None,
                             help='Survey-in 收敛超时（秒）。默认不限时，信号差时可用它兜底')
    cors_group = parser.add_argument_group('CORS 差分标定（方案 P1，厘米级）')
    cors_group.add_argument('--cors', action='store_true',
                            help='CORS 高精度标定：切流动站 → 连 CORS → 固定解采集 → 写坐标切基准站')
    cors_group.add_argument('--measure-only', action='store_true',
                            help='只测坐标、不写模块（外接流动站/未知模块用它）')
    cors_group.add_argument('--cors-host', help='CORS 服务器地址（IP 或域名）')
    cors_group.add_argument('--cors-port', type=int, help='CORS 端口（如 8002 WGS84）')
    cors_group.add_argument('--mountpoint', help='接入点 / 挂载点，如 RTCM33_GRCEJ')
    cors_group.add_argument('--cors-user', default='', help='CORS 账号')
    cors_group.add_argument('--cors-password', default='', help='CORS 密码')
    cors_group.add_argument('--duration', default='standard', choices=sorted(CORS_DURATIONS),
                            help='采集时长：short=3分钟、standard=5分钟、long=10分钟（默认 standard）')
    cors_group.add_argument('--fix-timeout', type=float, default=None,
                            help='等待固定解的超时秒数（默认 180）。天线被遮挡时用它兜底，'
                                 '避免无限占串口')
    cors_group.add_argument('--list-mountpoints', action='store_true',
                            help='只读取并打印 CORS 挂载点列表后退出（不碰串口、不写模块）')
    write_group.add_argument('--show-backup', action='store_true',
                             help='打印备份摘要后退出（不碰串口）')
    args = parser.parse_args(argv)

    if args.selftest:
        logging.basicConfig(level=logging.CRITICAL)
        return _selftest()

    if args.write_selftest:
        logging.basicConfig(level=logging.CRITICAL)
        return _write_selftest()

    if args.gate_selftest:
        logging.basicConfig(level=logging.WARNING,
                            format='%(asctime)s %(levelname)-7s %(message)s')
        if not args.port:
            print('× --gate-selftest 必须用 --port 指定串口（要真开串口验证让位）')
            return 2
        return gate_selftest(args.port, args.baud or DEFAULT_BAUD, args.seconds)

    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0

    if args.show_backup:
        info = backup_info()
        print(json.dumps(info, ensure_ascii=False, indent=2) if info
              else '没有备份（还没标定过，或 .calibration/backup.json 不存在）')
        return 0 if info else 1

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(message)s')
    service = get_service()

    if args.restore:
        ok, message, _payload = service.start_restore(port=args.port, baud=args.baud)
        if not ok:
            print(f'× {message}')
            return 1
        print(f'✓ {message}（还原期间主程序数据面已暂停）')
        final = service.wait(timeout=180)
        return _print_final(final, args.json, _print_write_result)

    if args.survey_in:
        ok, message, _payload = service.start_survey_in(
            port=args.port, baud=args.baud, profile=args.profile, msm=args.msm,
            remark=args.remark, rtcm_timeout=args.rtcm_timeout,
            svin_timeout=args.survey_timeout, coord_msg=args.coord_msg)
        if not ok:
            print(f'× {message}')
            return 1
        print(f'✓ {message}（收敛期间主程序数据面已暂停，可 Ctrl+C 手动终止取当前均值）')
        # Survey-in 可能跑 10~60 分钟。注意 `wait(timeout)` 的 timeout 是"多久没结束就
        # 返回当前快照"，不是"收敛超时"——收敛超时是 writer 内部的 `svin_timeout`，
        # 会抛 WriteFailed 并正常收尾。这里 wait 给足时间（7200s），让内部超时/收敛/手动
        # 终止都能走完并把最终结果带回来；用户 Ctrl+C 由 _checkpoint 兜底。
        final = service.wait(timeout=7200)
        return _print_final(final, args.json, _print_write_result)

    if args.list_mountpoints:
        if not args.cors_host or not args.cors_port:
            print('× --list-mountpoints 必须同时给 --cors-host、--cors-port')
            return 1
        from .cors import CorsError, read_mountpoints
        try:
            header, items = read_mountpoints(args.cors_host, args.cors_port,
                                             args.cors_user, args.cors_password)
        except CorsError as e:
            print(f'× 读取挂载点失败（{e.kind}）：{e.message}')
            return 1
        print(f'✓ {header}  共 {len(items)} 个挂载点'
              f'（{args.cors_host}:{args.cors_port}）')
        width = max((len(i['mountpoint']) for i in items), default=10)
        print(f'  {"挂载点".ljust(width)}  格式      系统                    认证')
        for item in items:
            print(f'  {item["mountpoint"].ljust(width)}  '
                  f'{item["format"]:<9} {item["nav"]:<22} {item["auth"]}')
        return 0

    if args.cors:
        if not args.cors_host or not args.cors_port or not args.mountpoint:
            print('× --cors 必须同时给 --cors-host、--cors-port、--mountpoint')
            return 1
        ok, message, _payload = service.start_cors(
            port=args.port, baud=args.baud, host=args.cors_host,
            cors_port=args.cors_port, mountpoint=args.mountpoint,
            user=args.cors_user, password=args.cors_password,
            duration=args.duration, msm=args.msm, remark=args.remark,
            rtcm_timeout=args.rtcm_timeout, fix_wait_timeout=args.fix_timeout,
            coord_msg=args.coord_msg, measure_only=args.measure_only)
        if not ok:
            print(f'× {message}')
            return 1
        print(f'✓ {message}（标定期间主程序数据面已暂停，可 Ctrl+C 终止）')
        final = service.wait(timeout=3600)
        return _print_final(final, args.json, _print_write_result)

    if args.write_ecef or args.write_latlon:
        ecef = _parse_triple(args.write_ecef, '--write-ecef') if args.write_ecef else None
        lat = lon = height = None
        if args.write_latlon:
            values = _parse_triple(args.write_latlon, '--write-latlon', allow_two=True)
            lat, lon = values[0], values[1]
            height = values[2] if len(values) > 2 else 0.0
        ok, message, _payload = service.start_write(
            port=args.port, baud=args.baud, ecef=ecef, lat=lat, lon=lon, height=height,
            msm=args.msm, remark=args.remark,
            dry_run=args.dry_run, rtcm_timeout=args.rtcm_timeout,
            coord_msg=args.coord_msg)
        if not ok:
            print(f'× {message}')
            return 1
        print(f'✓ {message}' + ('（演练，不写模块）' if args.dry_run else ''))
        final = service.wait(timeout=300)
        return _print_final(final, args.json, _print_write_result)

    ok, message, _payload = service.start_diagnose(port=args.port, baud=args.baud,
                                                   seconds=args.seconds)
    if not ok:
        print(f'× {message}')
        return 1
    print(f'✓ {message}（{args.seconds} 秒观测中，主程序数据面已暂停）')

    final = service.wait(timeout=args.seconds + 120)
    if args.json:
        print(json.dumps(final, ensure_ascii=False, indent=2, default=str))
    else:
        if final.get('stage') == 'DONE':
            _print_report(final.get('result') or {})
        else:
            print(f'× 任务未完成：{final.get("stage")} / {final.get("error")}')
    return 0 if final.get('stage') == 'DONE' else 1


def _parse_triple(text, flag, allow_two=False):
    """解析 `a,b,c` 这种三元素命令行参数。给了非法值直接抛 SystemExit(2)。"""
    parts = [item.strip() for item in str(text).replace('，', ',').split(',') if item.strip()]
    need = 2 if allow_two else 3
    if len(parts) < need:
        raise SystemExit(f'× {flag} 需要 {need} 个数字（用逗号分隔），收到 {text!r}')
    try:
        return [float(item) for item in parts]
    except ValueError:
        raise SystemExit(f'× {flag} 里有非数字：{text!r}')


def _print_final(final, as_json, printer):
    if as_json:
        print(json.dumps(final, ensure_ascii=False, indent=2, default=str))
    elif final.get('stage') == 'DONE':
        printer(final.get('result') or {})
    else:
        print(f'× 任务未完成：{final.get("stage")} / {final.get("error")}')
        result = final.get('result') or {}
        if result.get('steps'):
            printer(result)
    return 0 if final.get('stage') == 'DONE' else 1


# ==================== 门闸联调自测（真数据面 + 真串口，需要在场硬件） ====================
class _FakeDataPlane(threading.Thread):
    """"数据面"的替身：只做 `run_once()` 的退出契约，不做业务。

    为什么可以替身：门闸要验证的只有"数据面会不会在租约期间碰串口"，
    它依赖的契约是固定的四件事 —— 置 `worker_thread`、看 `restart_event`、
    退出时清 `worker_thread`、关串口。业务是转发 NMEA 还是 RTCM 与此无关。

    但它**必须**调真正的 `lifecycle._calibration_gate()`，而不是抄一份逻辑：
    抄一份就等于自己验证自己，门闸写错了自测照样绿。
    """

    def __init__(self, port, baud):
        super().__init__(name='fake-dataplane', daemon=True)
        self.port = port
        self.baud = int(baud)
        self.bytes_in = 0        # 从串口读到的总字节数（用来判断"有没有在转发"）
        self.serial_opens = 0
        self.rounds = 0
        self.errors = 0
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        from ..core import lifecycle
        import serial

        while not state.shutdown_event.is_set() and not self._stop.is_set():
            if lifecycle._calibration_gate():
                continue
            state.thread_exit_event.clear()
            ser = None
            try:
                ser = serial.Serial(self.port, self.baud, timeout=0.2)
                self.serial_opens += 1
                self.rounds += 1
                state.worker_thread = self
                while (not state.restart_event.is_set()
                       and not state.shutdown_event.is_set()
                       and not self._stop.is_set()):
                    chunk = ser.read(256)
                    if chunk:
                        self.bytes_in += len(chunk)
                    else:
                        time.sleep(0.01)
            except Exception as e:                   # noqa: BLE001
                self.errors += 1
                logging.error(f'假数据面打开 {self.port} 失败：{e}')
                time.sleep(0.5)
            finally:
                state.thread_exit_event.set()
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                state.worker_thread = None
                state.restart_event.clear()


def gate_selftest(port, baud=DEFAULT_BAUD, seconds=8.0):
    """真机联调：证明"标定期间数据面暂停、结束后自动恢复、线程死了看门狗兜底"。

    这条自测必须跑在**同一进程**里，因为 gate 是进程内的事件 ——
    跨进程（另一个终端起 CLI）测的是"两个进程抢串口"，不是租约语义。
    """
    from ..core import lifecycle

    print('=== 门闸联调自测（真数据面 + 真串口）===')
    state.shutdown_event.clear()
    state.calibration_hold.clear()
    state.calibration_beat = 0.0
    state.restart_event.clear()
    state.worker_thread = None

    plane = _FakeDataPlane(port, baud)
    plane.start()
    try:
        # 1) 数据面先跑起来（等价于"主程序正在转发"）
        deadline = time.time() + 10
        while time.time() < deadline and plane.bytes_in < 512:
            time.sleep(0.1)
        if plane.bytes_in < 512:
            print(f'× 数据面没读到数据（{plane.bytes_in} 字节）。'
                  f'检查 {port} 上是否有模块在输出、波特率是否 {baud}')
            return 1
        print(f'[1] 数据面运行中 OK  已读 {plane.bytes_in} 字节，串口打开 {plane.serial_opens} 次')

        # 2) 启动只读诊断：数据面必须让位
        service = get_service()
        ok, message, _ = service.start_diagnose(port=port, baud=baud, seconds=int(seconds))
        if not ok:
            print(f'× 启动诊断失败：{message}')
            return 1

        deadline = time.time() + 10
        while time.time() < deadline and not state.calibration_hold.is_set():
            time.sleep(0.05)
        if not state.calibration_hold.is_set():
            print('× 10 秒内没有置 gate，数据面还在抢串口')
            return 1
        time.sleep(0.8)                              # 等数据面收手（一次 read 的 timeout）
        frozen = plane.bytes_in
        time.sleep(2.0)
        if plane.bytes_in != frozen:
            print(f'× 标定期间数据面仍在读串口：{frozen} → {plane.bytes_in} 字节')
            return 1
        print(f'[2] 标定期间数据面暂停 OK  2 秒内字节数保持 {frozen}（未增长）')

        # 3) 诊断跑完 → 数据面自动恢复
        final = service.wait(timeout=seconds + 60)
        if final.get('stage') != 'DONE':
            print(f'× 诊断未完成：{final.get("stage")} / {final.get("error")}')
            return 1
        summary = (final.get('result') or {}).get('summary') or {}
        print(f'[3] 诊断完成 OK  {summary.get("headline")}（{summary.get("samples_1005")} 条 1005）')

        deadline = time.time() + 15
        while time.time() < deadline and plane.bytes_in <= frozen + 256:
            time.sleep(0.1)
        if plane.bytes_in <= frozen + 256:
            print(f'× 15 秒内数据面没有恢复：{frozen} → {plane.bytes_in} 字节')
            return 1
        print(f'[4] 数据面自动恢复 OK  {frozen} → {plane.bytes_in} 字节，'
              f'串口重新打开 {plane.serial_opens} 次')

        # 4) 看门狗：心跳停 60 秒必须被强制解除（模拟标定线程被 kill）
        lease = SerialLease(port=port, baud=baud, task_id='WATCHDOG')
        ok, message = lease.acquire()
        if not ok:
            print(f'× 申请租约失败：{message}')
            return 1
        base_bytes = plane.bytes_in
        state.calibration_beat = time.time() - 60      # 假装 60 秒没喂心跳
        forced = lifecycle._calibration_gate()
        if forced or state.calibration_hold.is_set():
            print('× 心跳停了 60 秒，门闸没有强制解除')
            return 1
        deadline = time.time() + 10
        while time.time() < deadline and plane.bytes_in <= base_bytes + 256:
            time.sleep(0.1)
        if plane.bytes_in <= base_bytes + 256:
            print(f'× 看门狗解除后数据面没有恢复：{base_bytes} → {plane.bytes_in}')
            return 1
        print(f'[5] 看门狗强制解除 OK  gate 已清，数据面恢复（{base_bytes} → {plane.bytes_in} 字节）')
        lease.release('INTERRUPTED')

        print('=== 门闸联调自测全部通过：暂停 / 恢复 / 看门狗 三条都成立 ===')
        return 0
    finally:
        plane.stop()
        try:
            state.thread_exit_event.set()
            state.restart_event.set()
        except Exception:
            pass
        plane.join(3)
        state.thread_exit_event.clear()
        state.calibration_hold.clear()
        state.calibration_beat = 0.0
        state.restart_event.clear()
        state.worker_thread = None


# ==================== 离线自测（假模块，不需要硬件） ====================
def _fake_serial_class():
    """造一个"会回命令、还会每 200ms 播一条 1005"的假串口。

    刻意不复用 driver_manager 里的 `_FakeSerial`：那个只回命令，不播 RTCM，
    而 P0 的核心恰恰是"从混合流里把 1005 拆出来比对"，必须两样都有。
    """
    import serial as _serial

    from .proto.rtcm import _build_1005_frame

    class _FakeSerial:
        # 与真实 1005 播发值一致的一份坐标（下方 REPLY 里 CFGSVIN 用的也是它）
        X, Y, Z = -2519265.0514, 4849534.9045, 3277834.6432

        def __init__(self, port=None, baudrate=None, **kwargs):
            self.port = port
            self.baudrate = baudrate or 115200
            self.is_open = True
            self._pending = b''
            self._last_frame = 0.0
            self._frame = _build_1005_frame(self.X, self.Y, self.Z, station_id=4321)
            self.writes = []

        # ---- 写：按命令名回一条应答 ----
        def write(self, data):
            self.writes.append(bytes(data))
            body = bytes(data).decode('ascii', 'replace').strip().lstrip('$').split('*')[0]
            name = body.split(',')[0]
            reply = self._reply(name, body)
            if reply:
                # 必须用 frame() 组出 `$...*XX\r\n`：少了 `$` 会被当成噪声逐字节丢弃
                # （表现为"命令无应答"），少了 `\n` 会被当成"还没收完"。
                self._pending += frame(reply).encode('ascii')
            return len(data)

        def _reply(self, name, body):
            table = {
                'PQTMVERNO': 'PQTMVERNO,LG290P03AANR01A03S,2024/05/22,18:09:06',
                'PQTMSN': 'PQTMSN,ERROR,3',
                'PQTMUNIQID': 'PQTMUNIQID,OK,8,00006FE9326C3E41',
                'PQTMCFGANTINF': 'PQTMCFGANTINF,ERROR,3',
                'PQTMCFGRCVRMODE': 'PQTMCFGRCVRMODE,OK,2',
                'PQTMCFGSVIN': ('PQTMCFGSVIN,OK,2,0,0.0,'
                                f'{self.X:.4f},{self.Y:.4f},{self.Z:.4f}'),
                'PQTMCFGRTCM': 'PQTMCFGRTCM,OK,4,0,-90,07,06,1,0',
            }
            return table.get(name, f'{name},OK')

        # ---- 读：先吐应答，空闲时每 200ms 吐一条 1005 ----
        def read(self, size=256):
            if self._pending:
                data, self._pending = self._pending, b''
                return data
            now = time.time()
            if now - self._last_frame >= 0.2:
                self._last_frame = now
                return self._frame
            time.sleep(0.01)
            return b''

        def flush(self):
            pass

        def reset_input_buffer(self):
            self._pending = b''

        @property
        def in_waiting(self):
            return len(self._pending)

        def close(self):
            self.is_open = False

    return _FakeSerial, _serial


def _selftest():
    """离线跑完整生命周期：让位 → 识别 → 只读诊断 → 比对 → 归还 → 恢复。"""
    import tempfile

    print('=== service 自测 ===')
    original_dir = workspace.STATE_DIR
    original_db = store.CALIBRATION_DB
    tmpdir = tempfile.mkdtemp(prefix='2rtk-service-')
    workspace.STATE_DIR = tmpdir

    fake_class, serial_module = _fake_serial_class()
    original_serial = getattr(serial_module, 'Serial', None)
    serial_module.Serial = fake_class
    store.CALIBRATION_DB = os.path.join(tmpdir, 'calibration.db')
    try:
        store.init_db()
        state.calibration_hold.clear()
        state.calibration_beat = 0.0
        state.restart_event.clear()
        state.worker_thread = None

        service = CalibrationService()

        # 1) 参数校验
        ok, message, _ = service.start_diagnose(port='')
        assert not ok and '串口' in message, message
        print(f'[1] 空串口拒绝 OK  {message}')

        # 2) 完整跑一次
        ok, message, _snapshot = service.start_diagnose(port='FAKE0', baud=115200,
                                                        seconds=3, task_id='SELFTEST')
        assert ok, message
        # gate 由工作线程在 acquire() 里置位，与 start_diagnose 返回有几毫秒竞争，
        # 所以这里等它一下（真机上同样有这段窗口，不影响正确性）
        for _ in range(40):
            if state.calibration_hold.is_set():
                break
            time.sleep(0.05)
        assert state.calibration_hold.is_set(), '任务开始后必须置 gate（数据面暂停）'
        print(f'[2] 启动 OK  运行中 gate={state.calibration_hold.is_set()}')

        # 3) 重复启动必须被拒
        ok2, message2, _ = service.start_diagnose(port='FAKE0', seconds=3)
        assert not ok2 and '已有标定任务' in message2, message2
        print(f'[3] 重复启动拒绝 OK  {message2}')

        final = service.wait(timeout=60)
        assert final['stage'] == 'DONE', f'{final["stage"]}: {final.get("error")}'
        report = final['result']
        assert report['readonly'] is True
        print(f'[4] 任务完成 OK  {final["stage"]} / {final["message"]}')

        # 5) 归还：gate 必须清、数据面必须被唤醒
        assert not state.calibration_hold.is_set(), '任务结束后 gate 必须清除'
        assert state.calibration_beat == 0.0
        assert state.restart_event.is_set(), '任务结束后必须唤醒主循环恢复数据面'
        print('[5] 归还与恢复 OK  gate=0 restart=1')

        # 6) 模块信息（SN 走 UNIQID 兜底）
        module = report['module']
        assert module['firmware'] == 'LG290P03AANR01A03S', module
        assert module['sn'] == '00006FE9326C3E41' and module['sn_source'] == 'uniqid', module
        assert module['caps']['serial_no'] is False, 'PQTMSN 回 ERROR,3，能力位必须关掉'
        print(f'[6] 模块信息 OK  {module["firmware"]} / SN {module["sn"]}（{module["sn_source"]}）')

        # 7) 模式与配置回读
        assert report['mode']['text'] == 'base' and report['mode']['code'] == 2, report['mode']
        assert report['svin']['mode'] == 'fixed', report['svin']
        assert report['rtcm']['msm_type'] == 4, report['rtcm']
        print(f'[7] 配置回读 OK  模式 {report["mode"]["text"]}，'
              f'坐标 {report["svin"]["mode"]}，MSM{report["rtcm"]["msm_type"]}')

        # 8) 1005 观测 + 比对（假模块播的坐标与配置一致 → match）
        obs = report['observations']
        assert obs['frames_1005'] >= 3, obs
        coords = report['coordinates']
        assert coords['verdict'] == 'match', coords
        assert coords['delta']['max_axis'] < 0.001, coords['delta']
        print(f'[8] 1005 观测与比对 OK  {obs["frames_1005"]} 条，'
              f'最大轴差 {coords["delta"]["max_axis"]:.6f} m，verdict={coords["verdict"]}')

        # 9) 状态文件与租约文件都落到磁盘，且都是终态
        disk_state = workspace.read_json(workspace.state_path())
        disk_lease = workspace.read_json(workspace.lease_path())
        assert disk_state['stage'] == 'DONE' and disk_state['progress'] == 100, disk_state
        assert disk_lease['stage'] == 'DONE' and disk_lease['held'] is False, disk_lease
        print(f'[9] 落盘 OK  state={disk_state["stage"]}/{disk_state["progress"]} '
              f'lease={disk_lease["stage"]}')

        # 10) 事件落库（DONE 一条）
        events = store.list_events(task_id='SELFTEST')
        assert events and events[0]['stage'] == 'DONE', events
        print(f'[10] 事件落库 OK  {len(events)} 条')

        # 11) 取消：任务进行中被 abort，必须走 ABORTED 且恢复数据面
        state.restart_event.clear()
        ok, _message, _ = service.start_diagnose(port='FAKE0', seconds=10, task_id='CANCEL')
        assert ok
        time.sleep(0.6)
        ok, message = service.abort()
        assert ok, message
        final = service.wait(timeout=30)
        assert final['stage'] == 'ABORTED', f'{final["stage"]}: {final.get("error")}'
        assert not state.calibration_hold.is_set()
        assert state.restart_event.is_set()
        print(f'[11] 中途取消 OK  stage={final["stage"]}，数据面已恢复')

        # 12) 模块无应答：DETECT 失败 → FAILED → 仍然归还租约
        state.restart_event.clear()
        fake_class._reply = lambda self, name, body: 'PING,OK'   # 所有命令都答非所问
        ok, _message, _ = service.start_diagnose(port='FAKE0', seconds=3, task_id='NOANSWER')
        assert ok
        final = service.wait(timeout=40)
        assert final['stage'] == 'FAILED' and '无应答' in final['error'], final
        assert not state.calibration_hold.is_set(), '失败路径也必须清 gate'
        print(f'[12] 无应答失败路径 OK  {final["error"][:40]}…')

        print('=== service 自测全部通过 ===')
        return 0
    finally:
        if original_serial is not None:
            serial_module.Serial = original_serial
        workspace.STATE_DIR = original_dir
        store.CALIBRATION_DB = original_db
        state.calibration_hold.clear()
        state.calibration_beat = 0.0
        state.restart_event.clear()
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



# ==================== 写链路端到端自测（假模块，不需要硬件） ====================
def _write_selftest():
    """把 service 的租约/门闸/备份/落库与 writer 的 8 步链路串起来跑一遍。

    与 writer 自己的自测分工：
        writer 测"每一级校验的判据对不对"（阈值、回写、NVM 三态）；
        本函数测"串起来之后，数据面能不能恢复、备份能不能还原、库里有没有记录"。
    真机验收的三条（写入成功 / 保存失败回滚 / 重启后持久化）在这里都能先过一遍，
    剩下的只有"模块固件行为是否符合文档"这一个未知量。
    """
    import tempfile

    import serial as serial_module

    from .writer import _FakeModule, _deep, _fake_serial_class

    print('=== service 写链路自测（假模块：内存态 / NVM 态分离）===')
    original = (workspace.STATE_DIR, store.CALIBRATION_DB, serial_module.Serial)
    tmpdir = tempfile.mkdtemp(prefix='2rtk-write-selftest-')
    workspace.STATE_DIR = tmpdir
    store.CALIBRATION_DB = os.path.join(tmpdir, 'calibration.db')
    store.init_db()

    X, Y, Z = -2519265.0514, 4849534.9045, 3277834.6432
    try:
        state.shutdown_event.clear()
        state.calibration_hold.clear()
        state.calibration_beat = 0.0
        state.restart_event.clear()
        state.worker_thread = None

        service = CalibrationService()

        # 1) 参数校验必须同步拒绝：等占了串口、切了基站才发现参数错，代价太大
        ok, message, _ = service.start_write(port='FAKE0', ecef=(30.5, 114.3, 50.0))
        assert not ok and 'ECEF' in message, message
        print(f'[1] 参数同步校验 OK  {message[:38]}…')

        module = _FakeModule()
        serial_module.Serial = _fake_serial_class(module)

        # 2) 演练模式：只读 + 换算，模块一个字节都不改
        rates_before = _deep(module.mem['rates'])
        state.restart_event.clear()
        ok, message, _ = service.start_write(port='FAKE0', ecef=(X, Y, Z),
                                             dry_run=True)
        assert ok, message
        final = service.wait(timeout=60)
        assert final['stage'] == 'DONE', final
        result = final['result']
        assert result['dry_run'] is True
        assert result['plan']['apc'], '演练也要算出要写入的坐标'
        assert module.mem['mode'] == 1 and module.mem['rates'] == rates_before, '演练不能改模块'
        assert not state.calibration_hold.is_set(), '演练结束也必须清 gate'
        print(f'[2] 演练模式 OK  坐标已解析，模块零改动（模式仍 {module.mem["mode"]}）')

        # 3) 完整写入：备份 → 8 步 → 落库
        state.restart_event.clear()
        records_before = store.count_records()
        ok, message, _ = service.start_write(port='FAKE0', ecef=(X, Y, Z),
                                             remark='端到端自测')
        assert ok, message
        for _ in range(40):
            if state.calibration_hold.is_set():
                break
            time.sleep(0.05)
        assert state.calibration_hold.is_set(), '任务开始后必须置 gate'
        final = service.wait(timeout=180)
        assert final['stage'] == 'DONE', f'{final["stage"]}: {final.get("error")}'
        summary = final['result']['summary']
        assert summary['verdict'] == 'match', summary
        assert summary['nvm_state'] == 'saved', summary
        assert module.mem['mode'] == 2 and module.nvm['mode'] == 2
        assert store.count_records() == records_before + 1
        record = store.get_latest_by_sn(final['result']['record'].get('module_sn') or '')
        assert record and record['verified'] == 1 and record['nvm_state'] == 'saved', record
        assert not state.calibration_hold.is_set() and state.restart_event.is_set()
        print(f'[3] 完整写入 OK  {summary["headline"][:24]}…  记录 #{summary["record_id"]}，'
              f'数据面已恢复')

        # 4) 备份可还原：文件在盘上、内容可读
        info = backup_info()
        assert info and info['usable'] is True, info
        print(f'[4] 备份 OK  {info["text"][:46]}…')

        # 5) 一键还原：基站 → 移动站。**两件事必须同时成立**：
        #    模块回到 rover（模式对了），且报文速率回到标定前（1005 关回去）；
        #    并且不新增历史坐标记录（还原不是一次新的标定成果）。
        records_before = store.count_records()
        state.restart_event.clear()
        ok, message, _ = service.start_restore(port='FAKE0')
        assert ok, message
        final = service.wait(timeout=180)
        assert final['stage'] == 'DONE', f'{final["stage"]}: {final.get("error")}'
        assert final['result']['summary']['verdict'] == 'skipped'
        assert module.mem['mode'] == 1 and module.nvm['mode'] == 1, module.mem
        assert module.mem['rates'].get('RTCM3-1005') == 0, \
            f'还原必须把标定打开的 1005 关回去（当前 {module.mem["rates"].get("RTCM3-1005")}）'
        assert store.count_records() == records_before, '还原不能新增历史坐标记录'
        print(f'[5] 还原 OK  模块回到 {module.mem["mode"]}（移动站），1005 已关回 0，'
              f'历史库未新增记录')

        # 6) 保存失败：必须自动回写内存态，且数据面恢复、库不新增记录
        module = _FakeModule()
        module.fail_save = 99
        serial_module.Serial = _fake_serial_class(module)
        state.restart_event.clear()
        records_before = store.count_records()
        ok, message, _ = service.start_write(port='FAKE0', ecef=(X, Y, Z))
        assert ok, message
        final = service.wait(timeout=180)
        assert final['stage'] == 'FAILED', final
        result = final['result']
        assert result['nvm_state'] == 'reverted', result['nvm_state']
        assert module.mem['mode'] == 1, '回写必须把模式改回 rover'
        assert store.count_records() == records_before, '失败的写入不能进历史坐标'
        assert not state.calibration_hold.is_set()
        print(f'[6] 保存失败回滚 OK  nvm={result["nvm_state"]}，'
              f'模式已回 {module.mem["mode"]}，历史库未污染')

        # 7) 中途取消：gate 必须归还，NVM 已保存（不回写）
        module = _FakeModule()
        module.publish_1005 = False            # 卡在 VERIFY_RTCM 里
        serial_module.Serial = _fake_serial_class(module)
        state.restart_event.clear()
        ok, message, _ = service.start_write(port='FAKE0', ecef=(X, Y, Z), rtcm_timeout=10)
        assert ok, message
        time.sleep(3.0)
        ok, message = service.abort()
        assert ok, message
        final = service.wait(timeout=60)
        assert final['stage'] == 'ABORTED', final
        assert not state.calibration_hold.is_set(), '取消后必须清 gate'
        assert state.restart_event.is_set(), '取消后必须唤醒数据面'
        assert module.nvm['mode'] == 2, '已保存的配置不因取消而回滚'
        print(f'[7] 中途取消 OK  stage={final["stage"]}，数据面已恢复，'
              f'NVM 保持已保存状态')

        print('=== service 写链路自测全部通过（7 项）===')
        return 0
    finally:
        workspace.STATE_DIR, store.CALIBRATION_DB, serial_module.Serial = original
        state.calibration_hold.clear()
        state.calibration_beat = 0.0
        state.restart_event.clear()
        state.worker_thread = None
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
