# -*- coding: utf-8 -*-
"""标定驱动抽象基类 + 命令通道（方案 §5.1 / §4.1 ~ §4.4）。

三层职责，别混：
    Channel     只认字节：组帧、收发、把混合流拆成 NMEA 行 / RTCM 帧。零型号知识。
    parse_line  只认语句：GGA/RMC/GSA/SVINSTATUS → 字典。零型号知识。
    BaseDriver  只认语义：'set_base' 这种键 → 具体命令 → 校验返回值。

统一返回格式（全项目标定层唯一约定）：
    (success: bool, message: str, data: dict)
    message 是给人看的中文，data 是给流程层判断用的结构化结果。
    失败时 data 里必须带 'err_code'（模块错误码）或 'error'（内部错误标记）。

为什么通道先放在 base.py：
    方案 §2.1 里 `channel.py` 是独立文件，但它与驱动的边界完全由本文件定义。
    阶段2 抽出 channel.py 时只做文件搬迁、签名不变；现在分成两个文件反而让
    "驱动基类"这个交付物跑不起来（没有通道就没法发命令，也没法真机验证）。
"""
import logging
import time

from ...sources.rtcm_parser import crc24q
from ..proto import nmea

# ==================== 帧与校验和（方案 §4.1） ====================
MAX_LINE = 256            # 单行上限，超长判噪声（方案 §4.4）
MAX_FRAME = 1029          # RTCM3 最大帧长（3 头 + 1023 载荷 + 3 CRC）


def checksum(body):
    """XOR 校验和，2 位大写十六进制。范围是 `$` 与 `*` 之间的全部字符。"""
    return nmea.checksum(body)


def frame(body):
    """命令体 → 完整下发帧（含 `$`、`*XX`、CRLF）。"""
    body = body.lstrip('$')
    return f'${body}*{checksum(body)}\r\n'


class DriverBug(Exception):
    """驱动自身写错了（典型：表里记的校验和与实时算出来的不一致）。

    方案 §4.1 要求"算出的校验和与驱动里记录的常量不一致 → 判 BUG 并中止"，
    因为这种错误发出去只会得到 ERROR 或静默，现场根本分不清是模块不认还是我们写错。
    """


# ==================== 超时与重试策略（方案 §4.3） ====================
# (首等秒, 重试次数, 重试间隔秒)
TIMING = {
    'identify': (2.0, 3, 0.3),
    'query':    (1.5, 3, 0.2),
    'config':   (1.5, 2, 0.2),
    'save':     (2.0, 2, 0.5),
}

_IDENTIFY_NAMES = ('PQTMVERNO', 'PQTMSN', 'PQTMUNIQID')

# 重启时序（方案 §8.5）。真机实测（LG290P03AANR01A03S / CH340 / 115200）：
#   SRR → 仍继续输出约 5.6 s 的旧 NMEA → 5.67 s 出现 `$PQTMVER` 开机首帧。
# 所以 20 s 是"约 3.5 倍余量"：既不会白等，也不会因固件冷启动慢一点就误判失败。
REBOOT_REOPEN_DELAY = 0.8      # 关串口后等多久再重开（避开复位瞬间的残帧）
REBOOT_BOOT_TIMEOUT = 20.0     # 等开机首帧的上限（首帧是哪条语句由驱动声明）

# 回读不一致的重试次数（方案 §14 #6）。
# 为什么是 1 而不是 3：偶发的"回读还是旧值"（刚重启完参数还没加载、串口里残留
# 上一条应答）重发一次就对了；真的是权限/固件/型号不支持，重发 100 次也不会变，
# 多试只会让失败晚 20 秒报出来 —— 而这 20 秒正是用户盯着页面等结果的时间。
VERIFY_RETRY = 1


def command_name(body):
    """从命令体里取语句名，用于 classify 比对。

    `$PQTM...` 用逗号分隔，N4 的简化 ASCII 用空格分隔（`MODE BASE TIME 60`），
    所以两种都要切。多词命令（`CONFIG COM1 115200`）只取首词 —— 应答也是
    `$CONFIG,COM1,...`，只有首词能对上，取全部反而永远匹配不到。
    """
    text = (body or '').strip().lstrip('$#')
    for sep in (',', ' '):
        if sep in text:
            text = text.split(sep)[0]
    return text.strip().upper()


def policy_for(body):
    """按命令形态自动归到 §4.3 的四档之一，驱动不必逐条声明。

    查询判定必须看**第 2 个字段**，不能只看最后一个：
    `PQTMCFGMSGRATE,R,GGA` 的末字段是语句名，只看末字段会被当成配置命令。
    两种查询形态都要覆盖：`,R` 结尾（`PQTMCFGRTCM,R`）和 `,R,<参数>`
    （`PQTMCFGMSGRATE,R,PQTMEPE,2`）。
    """
    # `VERSION` / `CONFIG` 这类不带参数的查询：按逗号切出来只有一段，
    # 会落到 config 档（1.5 s），对 N4 冷启动读取明显偏短。
    parts = [part.strip().upper() for part in body.split(',')]
    name = command_name(body)
    if name in _IDENTIFY_NAMES or name == 'VERSION':
        return 'identify'
    if 'SAVEPAR' in name or 'SAVECONFIG' in name:
        return 'save'
    if len(parts) > 1 and parts[1] == 'R':
        return 'query'
    if parts[-1] == 'R':
        return 'query'
    return 'config'


# ==================== 错误码文案（方案 §4.5） ====================
ERROR_TEXT = {
    1: ('模块认为参数不合法', '检查端口/波特率/型号选择是否与实际模块一致'),
    2: ('模块执行失败', '重试一次；仍失败则重启模块后再试'),
    3: ('该型号不支持此命令', '不要重试，改用能力集里支持的替代命令'),
}


def error_info(code):
    text, advice = ERROR_TEXT.get(code, ('未知错误码', '记录该错误码并向厂家确认'))
    return {'err_code': code, 'error_text': text, 'advice': advice}


def _with_attempts(data, attempts):
    """给回读结果补上 `verify_attempts`（回读了几次才对上）。

    不原地改传入的 dict：调用方可能已经把同一个对象挂进了 `channel.cache`，
    原地加字段会污染别的读取方。
    """
    if isinstance(data, dict):
        merged = dict(data)
    else:
        merged = {'value': data}
    merged['verify_attempts'] = int(attempts)
    return merged


# ==================== 语句解析（方案 §5.1 的 parse_line） ====================
def parse_svin_status(fields):
    """解析 `$PQTMSVINSTATUS,<MsgVer>,<TOW>,<Valid>,<Res0>,<Res1>,<Obs>,<CfgDur>,
    <MeanX>,<MeanY>,<MeanZ>,<MeanAcc>`。

    字段顺序见官方《GNSS 协议规范》§2.3.23，`fields[0]` 是语句名，所以参数从下标 1 起。
    这里**按下标宽松取**：字段少了就填 None，绝不因为某一版固件少一个预留字段就把整条
    语句判废（LC29HBS 的字段名还不一样，由子类覆盖本函数）。

    官方示例与真机对照（基站指导 §3.2.2）：
        ...,1,,11,57,60,...    ← 收敛中，Obs=57 / CfgDur=60
        ...,2,,11,60,60,...    ← 已收敛，Obs=60 / CfgDur=60
    所以"收敛完成"只认 `<Valid> == 2`；`Obs == CfgDur` 只是必要条件，
    文档示例里 59/60 时 `<Valid>` 仍为 1。
    """
    def num(index, cast=float):
        if index >= len(fields):
            return None
        raw = fields[index].strip()
        if not raw:
            return None
        try:
            return cast(float(raw))
        except ValueError:
            return None

    state = num(3, int)
    obs = num(6, int)
    # 官方协议规范 §2.3.23：<CfgDur> 的说明是"通过 PQTMCFGSVIN 配置的 <CFG_CNT> 字段
    # （Survey-in 模式下最少定位次数）"——是**次数**，不是秒。字段名里的 Dur 是厂家历史包袱，
    # 按秒算超时会得出"已配 3600 秒"的错误结论（基站模式 1 Hz 时才恰好等价）。
    cfg_count = num(7, int)
    progress = None
    if obs is not None and cfg_count:
        progress = min(1.0, obs / float(cfg_count))
    return {
        'type': 'SVINSTATUS',
        'msg_ver': num(1, int),
        'tow': num(2, int),
        # 注意键名：'state' 才是语句里的 <Valid>（0/1/2）。
        # 老版本这里写 'valid'，随后被 parse_line 用"解析成功=True"覆盖，
        # 于是 0/1/2 永远丢失，只能靠 valid_text 猜——已修正，别改回去。
        'state': state,
        'valid_text': {0: '无效', 1: '收敛中', 2: '已收敛'}.get(state, '未知'),
        'done': state == 2,
        'obs': obs,
        'cfg_count': cfg_count,
        'cfg_dur': cfg_count,        # 兼容旧键名；语义同 cfg_count（次数，非秒）
        'progress': progress,        # 0.0~1.0，直接给进度条用
        'mean_x': num(8),
        'mean_y': num(9),
        'mean_z': num(10),
        'mean_acc': num(11),
        'raw_fields': fields,
    }


def parse_line(text):
    """通用语句解析（所有驱动共用；型号特有语句由子类覆盖）。

    返回字典一定带 `type` 与 `valid`：
        GGA / RMC / GLL / GSA / SVINSTATUS / VER / 其它原样类型
    认不出来的语句返回 `{'type': 'UNKNOWN', 'valid': False, 'raw': ...}`，不抛异常。
    """
    if not text:
        return {'type': 'UNKNOWN', 'valid': False, 'raw': text}

    parsed = nmea.parse(text)
    sentence_type = parsed.get('type')

    if sentence_type == 'SVINSTATUS':
        result = parse_svin_status(parsed['fields'])
        result['valid'] = True          # 解析成功标志，别和 <Valid> 字段（键 state）混淆
        result['raw'] = text
        return result

    if sentence_type == 'VER':
        # 开机首帧 `$PQTMVER,<MsgVer>,<VerName>,<VerStr>,<BuildDate>,<BuildTime>`
        # （协议规范 §2.3.1，"模块每次成功启动后，均会先输出该语句"）。
        # 注意它和查询应答 `$PQTMVERNO,<VerStr>,<BuildDate>,<BuildTime>` 的字段**不一样**：
        # 前者 6 个、后者 5 个，前者第 1 个字段是 MsgVer 而不是固件串。
        # 真机实测：SRR 后 5.67 s 出现 `$PQTMVER,1,MODULE,LG290P03AANR01A03S,2024/05/22,18:09:06*36`。
        fields = parsed['fields']
        return {
            'type': 'VER', 'valid': True, 'raw': text, 'boot': True,
            'msg_ver': fields[1] if len(fields) > 1 else None,
            'ver_name': fields[2] if len(fields) > 2 else None,
            'ver_str': fields[3] if len(fields) > 3 else None,
            'build_date': fields[4] if len(fields) > 4 else None,
            'build_time': fields[5] if len(fields) > 5 else None,
        }

    if parsed['valid']:
        return parsed

    # 未建模的语句（例如查询应答 `$PQTMVERNO,...`）也要把字段带出去：
    # 校验和可能因为固件笔误算错，但字段本身仍然可用，丢掉等于把可用的证据扔了。
    fallback = {'type': sentence_type or 'UNKNOWN', 'valid': False,
                'raw': text, 'error': parsed.get('error')}
    if parsed.get('fields'):
        fallback['fields'] = parsed['fields']
    return fallback


def message_name(text):
    """从一行里取语句名（不含语句头，不含参数）。

    `$PQTMVER,1,...` → `PQTMVER`；`#VERSIONA,94,...` → `VERSIONA`（和芯星通 N4）。
    `#` 必须在这里剥掉：N4 的 `version` / `mode` 应答都是 `#` 开头，
    不剥的话 classify 里永远拼不上命令名，只表现为"发了命令没应答"。
    """
    text = (text or '').strip().lstrip('$#')
    return text.split(',')[0].strip().upper()


# ==================== 命令通道（方案 §4.2 ~ §4.4） ====================
class Channel:
    """串口通道：组帧、收发、混合流拆分、应答分类。

    串口参数按方案 §4.1.1 显式写死三个默认值 —— 流控不关会出现
    "一直能收到模块数据，但发命令永远无应答"，现场看起来就像模块不认命令。
    """

    def __init__(self, port=None, baud=115200, serial_instance=None):
        self.port = port
        self.baud = int(baud) if baud else 115200
        self._serial = serial_instance
        # 测试注入点：给了工厂函数就用它建串口（离线自测用假模块，不碰真硬件）
        self.serial_factory = None
        self._buffer = b''
        self.parser = parse_line          # 驱动可覆盖，用于型号特有语句
        # 语句同步头（方案 §4.4 的补充）。默认只认 '$'：Quectel 全系只有 `$PQTM...`
        # 一种 ASCII 语句头，认多了反而会把 RTCM 二进制里的 0x23 当语句起点。
        # 和芯星通 N4（Unicore）不一样：应答是 `#VERSIONA,...` / `$CONFIG,...` 两种头混排，
        # 且二进制 LOG 也会以 0x23 出现在输出流里 —— 认不出来的由 drive 自己 parse 兜住。
        self.heads = ('$',)
        # 下发时是否自动补 `$` 与 `*XX`。N4 的"简化 ASCII"命令（`freset` / `version` /
        # `mode base ...` / `rtcm1074 com2 1`）**既没有 `$` 也没有校验和**，
        # 直接按默认帧格式发出去，模块一个字都不认（而且不回 ERROR，就是静默）。
        self.frame_out = True
        # 命令名 → 应答名别名表。默认空（命令名与应答名相同）；
        # 驱动声明 `REPLY_ALIASES` 后由 `BaseDriver.__init__` 注入，
        # `classify()` 靠它把 `VERSION → #VERSIONA` 这类名字不等的应答认下来。
        self.reply_aliases = {}
        self.cache = {}                   # gga / rmc / gsa / svin / ver / last_line
        self.counters = {'lines': 0, 'frames': 0, 'noise': 0}
        # 链路活性（方案 §14 #1 / #28）：只用来判"**曾经有数据、现在突然没有**"。
        # 从未收到过任何字节时不能判死——模块本来就可能不输出（基准站模式关 NMEA），
        # 那种情况要靠命令应答超时去兜，判据不是这里。
        self.last_rx_at = 0.0
        self.rx_bytes = 0
        self.link_error = ''          # 最近一次读异常（拔线/断电的第一手证据）
        self._owns_serial = serial_instance is None
        if serial_instance is not None:
            # 外部传进来的句柄，配置可能不对，这里只做兜底不改它
            self.port = getattr(serial_instance, 'port', port)
            self.baud = getattr(serial_instance, 'baudrate', self.baud)

    # ---------- 生命周期 ----------
    @property
    def is_open(self):
        return self._serial is not None and getattr(self._serial, 'is_open', False)

    def open(self):
        if self.is_open:
            return self
        if self.serial_factory is not None:
            self._serial = self.serial_factory(self.port, self.baud)
            self._buffer = b''
            self.last_rx_at = 0.0
            self.rx_bytes = 0
            return self
        import serial
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            timeout=0.2,          # 读轮询粒度：默认 None 会让"终止"卡在 read 里
            write_timeout=0.5,    # 不设则模块不收数据时 write 永久阻塞
            rtscts=False,         # 显式关硬件流控（USB 转串口常不接 RTS/CTS）
            dsrdtr=False,
            xonxoff=False,
        )
        self._owns_serial = True
        self._buffer = b''
        self.last_rx_at = 0.0
        self.rx_bytes = 0
        logging.debug(f'标定通道已打开：{self.port} @ {self.baud}')
        return self

    def close(self):
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception as e:
                logging.debug(f'关闭标定通道时出错（忽略）：{e}')
        self._serial = None
        self._buffer = b''

    def reopen(self, delay=REBOOT_REOPEN_DELAY, attempts=3):
        """重启判定专用：关串口 → 等模块真正断电复位 → 重开（方案 §8.5）。

        不关直接等也能收到 `$PQTMVER`，但复位瞬间的残帧会混进缓冲，
        让"开机首帧"的判定不可靠；关一次代价只有 0.8 秒，换来干净的重同步。

        `attempts` 是给 USB CDC 模块留的：CH340 复位时设备节点不消失，重开一次就行；
        但原生 USB 的模块会重新枚举，`open()` 会有几十到几百毫秒的"设备不存在"窗口。
        重试而不是直接抛，避免"重启成功却报串口打不开"这种假故障。
        """
        self.close()
        time.sleep(max(0.0, delay))
        last_error = None
        for index in range(max(1, int(attempts))):
            try:
                return self.open()
            except Exception as e:                 # noqa: BLE001 - 串口异常种类极杂
                last_error = e
                logging.debug(f'重开串口第 {index + 1} 次失败（{self.port}）：{e}')
                time.sleep(0.3)
        raise DriverBug(f'重启后无法重开串口 {self.port}：{last_error}')

    # ---------- 链路活性（方案 §14 #1 / #28） ----------
    def silent_seconds(self):
        """距上一次**收到任何字节**过了多少秒；从未收到过返回 None。"""
        if not self.last_rx_at:
            return None
        return max(0.0, time.time() - self.last_rx_at)

    def link_lost(self, limit=3.0):
        """判"曾经有数据、现在突然没有"。

        为什么必须区分"从未收到"和"读到一半突然断"：
            基准站模式下模块本来就可能不输出 NMEA，此时沉默是正常的；
            但采集阶段已经读到过成千上万字节，再连续 3 秒一个字节都没有，
            只能是拔线、断电或驱动崩了。前者的正确动作是继续等，
            后者必须**立刻失败**——否则程序会拿着断线前的旧位置继续算，
            最后写进模块一组看不出问题的坐标。
        """
        if self.link_error:
            return True
        silent = self.silent_seconds()
        return silent is not None and silent >= max(0.0, float(limit))

    def drain(self):
        """丢弃**当前已经到达**的字节。

        刻意只清"调用瞬间已有的残留"，不读一段时间窗口：
        窗口期内到达的正是"重开之后的第一帧"（也就是我们要等的开机首帧，
        名字由驱动的 `BOOT_FRAME` 决定），按时间窗口清会把开机首帧一起吃掉，
        表现为"重启后永远等不到开机帧"。
        """
        try:
            if self.is_open:
                self._serial.reset_input_buffer()
        except Exception as e:
            logging.debug(f'清空串口缓冲失败（忽略）：{e}')
        self._buffer = b''

    # ---------- 清缓冲 ----------
    def flush(self, seconds=0.3):
        """丢掉 `seconds` 时间内到达的**全部**字节（方案 §3.2 第 7 步）。

        与 `drain()` 的分工：`drain()` 只清"调用瞬间已到达"的字节，用于命令之间；
        `flush()` 额外读一段短窗口，用于"刚取得独占权"时清掉旧 fd 留在内核缓冲里的
        半帧。窗口必须短（300ms 级），长了会把模块真正的新数据也丢掉。
        """
        if not self.is_open:
            return
        deadline = time.time() + max(0.0, float(seconds))
        while time.time() < deadline:
            try:
                chunk = self._serial.read(256)
            except Exception as e:                    # noqa: BLE001
                logging.debug(f'清残帧时读串口失败（按链路中断处理）：{e}')
                self.link_error = f'{type(e).__name__}: {e}'
                return
            if chunk:
                self.last_rx_at = time.time()
                self.rx_bytes += len(chunk)
            if not chunk:
                time.sleep(0.02)
        self._buffer = b''

    # ---------- 发送 ----------
    def write_line(self, body):
        """下发一条命令体（默认自动补 `$`、`*XX`、CRLF）。

        驱动把 `channel.frame_out` 置 False 时按原样发：和芯星通 N4 的简化 ASCII
        命令本来就是裸文本，多加了 `$` 和 `*XX` 只会让模块当成非法指令忽略掉。
        """
        if not self.is_open:
            raise DriverBug(f'通道未打开（{self.port}）')
        if self.frame_out:
            payload = frame(body).encode('ascii')
        else:
            payload = (body.strip() + '\r\n').encode('ascii')
        self._serial.write(payload)
        self._serial.flush()
        logging.debug(f'→ {payload!r}')

    def _head_bytes(self):
        """`heads` 的字节集合。每次现算而不是缓存 —— 驱动可能在构造后才改 heads。"""
        return frozenset(ord(ch) for ch in (self.heads or ('$',)))

    # ---------- 接收 ----------
    def _next_line(self):
        """从缓冲里取出下一条完整 NMEA 行（含 `$`，不含 CRLF）。

        混合流拆分规则（方案 §4.4）：
            0xD3 → 按长度字段读整帧 + CRC24Q 校验，喂 RTCM 缓存；
            '$'  → 读到 `\\n`，超 256 字节判噪声重新同步；
            其它 → 丢一个字节并计数（连续大量垃圾 = 波特率不对）。
        """
        while self._buffer:
            head = self._buffer[0]
            if head == 0xD3:
                if len(self._buffer) < 3:
                    return None
                length = ((self._buffer[1] & 0x03) << 8) | self._buffer[2]
                total = 3 + length + 3
                if total > MAX_FRAME:
                    self._buffer = self._buffer[1:]
                    self.counters['noise'] += 1
                    continue
                if len(self._buffer) < total:
                    return None
                candidate = self._buffer[:total]
                if crc24q(candidate[:-3]) != int.from_bytes(candidate[-3:], 'big'):
                    # 载荷里恰好出现 0xD3 很常见，滑一个字节重新同步
                    self._buffer = self._buffer[1:]
                    self.counters['noise'] += 1
                    continue
                self._buffer = self._buffer[total:]
                self.counters['frames'] += 1
                self.cache['rtcm_last'] = candidate
                self.cache.setdefault('rtcm_frames', []).append(candidate)
                if len(self.cache['rtcm_frames']) > 32:
                    del self.cache['rtcm_frames'][0]
                continue

            if head in self._head_bytes():
                end = self._buffer.find(b'\n')
                if end < 0:
                    if len(self._buffer) > MAX_LINE:
                        self._buffer = self._buffer[1:]
                        self.counters['noise'] += 1
                        continue
                    return None
                raw = self._buffer[:end + 1]
                self._buffer = self._buffer[end + 1:]
                if len(raw) > MAX_LINE:
                    self.counters['noise'] += 1
                    continue
                text = raw.decode('ascii', 'replace').strip()
                self.counters['lines'] += 1
                self.cache['last_line'] = text
                return text

            # 既不是 '$' 也不是 0xD3，丢一个字节继续找同步头
            self._buffer = self._buffer[1:]
            self.counters['noise'] += 1
        return None

    def read_line(self, timeout=1.0):
        """读一行，超时返回 None。期间收到的无关语句会自动进缓存。"""
        deadline = time.time() + max(0.0, timeout)
        while True:
            line = self._next_line()
            if line is not None:
                return line
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            if not self.is_open:
                return None
            try:
                chunk = self._serial.read(256)
            except Exception as e:
                # 拔线/断电时很多 USB 转串口驱动会在 read 上直接抛 SerialException。
                # 这里不抛给上层（上层有更完整的阶段信息），但**必须立刻把链路判死**：
                # 把 last_rx_at 归零会让 link_lost() 失去判据，所以这里显式置
                # `link_error`，由 link_lost() 一并兜住。
                logging.debug(f'读串口异常（按链路中断处理）：{e}')
                self.link_error = f'{type(e).__name__}: {e}'
                return None
            if chunk:
                self.last_rx_at = time.time()
                self.rx_bytes += len(chunk)
                self._buffer += chunk
            # 读超时（chunk 为空）时继续循环，由 deadline 控制总时长

    def absorb(self, line):
        """把一条非应答语句喂给缓存。GGA/GSA/SVINSTATUS 都是标定要用的旁路数据。"""
        parsed = self.parser(line)
        kind = (parsed or {}).get('type')
        if kind == 'GGA':
            self.cache['gga'] = parsed
        elif kind == 'RMC':
            self.cache['rmc'] = parsed
        elif kind == 'GSA':
            self.cache['gsa'] = parsed
        elif kind == 'SVINSTATUS':
            self.cache['svin'] = parsed
        elif kind == 'VER':
            self.cache['ver'] = parsed
        return parsed

    # ---------- 应答分类（方案 §4.2） ----------
    def _reply_aliases(self, name):
        """这个命令名下，应答可能用哪些语句名。

        大多数协议里"命令名 == 应答名"（`$PQTMVERNO` 发出去，`$PQTMVERNO` 回来），
        所以默认只需要名字本身。但 N4 是简化 ASCII 命令配 NMEA 应答，名字会对不上：

            VERSION → #VERSIONA     （A 是 ASCII 输出的后缀，改了名字）
            MODE    → #MODE         （这条对得上）
            RTCM1074 → $RTCM1074,OK

        别名表由驱动声明（`BaseDriver.REPLY_ALIASES`），构造时注入到 `self.reply_aliases`。
        放在 Channel 上而不是直接读驱动类：Channel 不认识驱动，反向依赖会成环。
        """
        base = {name.upper()}
        table = getattr(self, 'reply_aliases', None) or {}
        extra = table.get(name.upper()) or table.get(name)
        if extra:
            if isinstance(extra, str):
                extra = (extra,)
            base.update(str(item).upper() for item in extra)
        return base

    def classify(self, line, name, sent_body):
        """区分 目标应答 / 错误应答 / 回显 / 无关输出。

        必须"认前缀不认整行"：模块会主动输出 `$PQTMSVINSTATUS`，
        把所有 `$PQTM` 都当应答会永久错位。只认当前等待的那个命令名。
        """
        head = message_name(line)
        if head != name.upper() and head not in self._reply_aliases(name):
            return 'other'

        payload = line.strip().lstrip(''.join(self.heads or ('$',)))
        body_part = payload.split('*')[0].strip()
        # 回显判定必须放在最前面：`,R` 类命令的回显 `$PQTMCFGRCVRMODE,R*32`
        # 在"名字对得上但不是 OK/ERROR"这条规则下会被误当成应答，
        # 于是模型查询永远读到自己的回显，解析出一堆空字段。
        if body_part == sent_body.strip():
            return 'echo'

        rest = body_part[len(head):].lstrip(',')
        upper = rest.upper()
        if upper == 'OK' or upper.startswith('OK,'):
            return 'ok'
        if upper == 'ERROR' or upper.startswith('ERROR,'):
            return 'error'
        # 该命令名下的其它形态一律当目标应答，由驱动自己解析字段，通道不猜语义
        return 'ok'

    def send(self, body, timeout=None, retries=None, interval=None):
        """发一条命令并等应答，返回 `(status, line)`。

        status: 'ok' / 'error' / 'timeout'
        重试规则（方案 §4.3）：**ERROR 不重试**，只有超时才重试。
        """
        name = command_name(body)
        default_timeout, default_retries, default_interval = TIMING[policy_for(body)]
        timeout = default_timeout if timeout is None else timeout
        retries = default_retries if retries is None else retries
        interval = default_interval if interval is None else interval

        for attempt in range(int(retries) + 1):
            self.write_line(body)
            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                line = self.read_line(remaining)
                if line is None:
                    break
                kind = self.classify(line, name, body)
                if kind == 'ok':
                    return 'ok', line
                if kind == 'error':
                    return 'error', line          # 明确拒绝，重试无意义
                if kind == 'echo':
                    continue
                self.absorb(line)                 # 无关输出：进缓存，不算应答
            if attempt < retries:
                logging.debug(f'{name} 无应答，第 {attempt + 1} 次重试')
                time.sleep(max(0.0, interval))
        return 'timeout', ''

    def wait_for(self, name, timeout=15.0):
        """等某个语句名出现（重启后等开机首帧用）。返回 `(found, line)`。"""
        deadline = time.time() + max(0.0, timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False, ''
            line = self.read_line(remaining)
            if line is None:
                return False, ''
            if message_name(line) == name.upper():
                return True, line
            self.absorb(line)


# ==================== 驱动基类（方案 §5.1） ====================
class BaseDriver:
    """所有型号驱动的抽象基类。

    子类必须提供的三张表：
        MODEL_KEY       'lg290p' 这样的稳定标识（写进 calibration.db，永不改）
        MATCH_PREFIX    固件串前缀，用于自动识别（'LG290P'）
        CMD             语义键 → 命令体模板（**流程层只能通过键名调用**）
        CAPS            能力标记（见下）

    CAPS 的固定键（缺键按 False 处理，前端据此灰掉按钮）：
        dual_mode   能切移动站/基站
        set_mode    有模式命令
        svin        支持 Survey-in
        fixed       支持写固定坐标
        ant_info    支持写天线信息（1033）
        ant_delta   支持写天线偏移（1006 DF028）
        msg_rate    支持逐条配置语句速率
        err3        会返回错误码 3（"该命令不支持"）
    """

    MODEL_KEY = 'unknown'
    DISPLAY_NAME = '未知模块'
    #True = 该型号对外隐藏：`list_supported_modules()` 不返回、`match_driver()`
    # 自动探测不试它。用于"协议支持但业务上用不上"的型号（例：UM982 双天线定向板
    # 不适合做 RTK 基准站）。隐藏不等于删除 —— 历史记录/手工指定型号仍可正常加载。
    HIDDEN = False
    MATCH_PREFIX = ''
    DUAL_MODE = False
    CAPS = {}
    CMD = {}
    # 文末附录 E 里给过校验和的固定命令，填了就在下发前自检（方案 §4.1）
    CS_EXPECT = {}
    # 基站要播发哪些 MSM 星座（每项一个独立开关）。流程层按这个清单逐条下发，
    # 少配一个星座，下游就少一个系统的差分。不支持多星座的型号就保持这一档。
    MSM_CONSTELLATIONS = ('RTCM3-107X',)

    # ---------- 波特率（官方口径） ----------
    # 该型号厂商文档里写明支持的波特率档位，**按厂商文档原样抄**，不要自行扩展。
    # 采集/识别时按这个清单逐档试，比全局那份候选表准得多：
    #   LG290P 只认 9600~921600，用 4800 去试纯属浪费时间；
    #   LC29H(BS) 反而支持 4800 / 3000000，全局表里根本没有这两档。
    # 前端仍然允许用户手动输入任意值，这里只是"自动探测的顺序"。
    BAUDS = ()
    # 出厂/复位后的默认波特率。FRESET / 恢复出厂会回到这一档。
    DEFAULT_BAUD = 115200
    # 波特率按哪个命令下发（CMD 里的键名）；没填则该型号不支持改波特率。
    BAUD_CMD_KEY = ''
    # 波特率按哪个命令回读（CMD 里的键名）；没填 = 该型号没有回读命令。
    # 回读是"优先问模块"的前提：没有它就只能按档位盲试。
    BAUD_READ_KEY = ''

    # ---------- 串口帧格式（少数厂商不用 `$...*XX`） ----------
    # Quectel 全系命令都是 `$定长体*XX`，这两个值保持默认即可。
    # 和芯星通 N4 是"简化 ASCII"：命令裸文本无校验和，应答却是带校验和的 `#...`/`$...`，
    # 所以两个属性各自独立 —— 不能因为应答带头就以为命令也要带头。
    # 取多个头是有意的：N4 同时输出 `#VERSIONA,...`、`$CONFIG,...`、`$GNGGA,...`，
    # 只认 '$' 会让 `version` / `mode` 的应答全部丢掉（表现为"命令无应答"）。
    LINE_HEADS = ('$',)
    FRAME_COMMANDS = True
    # 命令名 → 应答名别名。默认空：Quectel 的 `$PQTMVERNO` 发什么回什么。
    # 只有"命令名与应答名不同"的型号才需要（N4 的 `VERSION` 回 `#VERSIONA`）。
    REPLY_ALIASES = {}

    # ---------- 重启同步点（厂商口径） ----------
    # "每次成功启动后必先输出"的那条语句名。Quectel 是 `PQTMVER`（协议规范 §2.3.1）。
    # 这是**厂商协议知识**，必须由驱动声明，不能写死在流程层的 `_reboot_sequence()` 里：
    # 和芯星通 N4 的命令是简化 ASCII，开机根本不打 `PQTMVER`，写死这五个字母
    # 会让每一块 N4 模块都白等 20 秒，然后被判"模块没有重启"。
    # 留空 = 该型号没有可用的开机首帧，`_reboot_sequence()` 直接走
    # "串口是否恢复出数据"的降级判定，不再白等一轮超时。
    BOOT_FRAME = 'PQTMVER'

    def __init__(self, channel):
        self.channel = channel
        if channel is not None:
            channel.parser = self.parse_line
            # 帧格式由驱动说了算，不要求调用方在建 Channel 时就知道插的是谁 ——
            # `probe()` 是先建通道再逐个驱动试的，那时还不知道型号。
            channel.heads = tuple(self.LINE_HEADS) or ('$',)
            channel.frame_out = bool(self.FRAME_COMMANDS)
            # 命令名与应答名不一致的型号（N4 的 VERSION→#VERSIONA）在这里注入
            channel.reply_aliases = dict(self.REPLY_ALIASES or {})
        # 运行期能力集：以类上的 CAPS 为初值，随后由 probe_caps() 用真实应答修正。
        # 为什么不能只信文档：实测 LG290P03AANR01A03S 对 PQTMSN / PQTMCFGANTINF
        # 都回 ERROR,3（文档说支持），页面上"点了没反应"比"按钮灰掉"难查得多。
        self.runtime_caps = dict(self.CAPS)

    # ---------- 通用 ----------
    @property
    def model_key(self):
        return self.MODEL_KEY

    def caps(self, key, default=False):
        return bool(self.runtime_caps.get(key, default))

    # ---------- 波特率 ----------
    def bauds(self):
        """该型号该先试哪些波特率（`DEFAULT_BAUD` 打头，其余降序）。

        为什么 `DEFAULT_BAUD` 必须排第一 —— 曾经这里是纯降序，注释的理由是
        "先用高的试，高的能通说明线材好"。听起来合理，但把顺序搞反了：
        **首次插上一块全新模块时，唯一已知的信息就是厂商示例里那个值**
        （LG290P 手册 §2.3.10 的全部示例都是 115200）。纯降序会让 921600 先试，
        错一档白等 2 秒，一块 115200 的模块要错 3 档才命中，正好是用户抱怨的
        "一档档试"——只是从低往高试换成了从高往低试，总等待没变少。

        `DEFAULT_BAUD` 的语义因此被钉死为"厂商示例/实测最可能命中的那一档"，
        不是"出厂默认值"（手册并没有承诺出厂值，见 LG290P 驱动的注释）。
        它排在**最前**只是押注，押错照旧往下走，代价为零。

        驱动没声明 BAUDS 时返回空元组，由调用方决定退回全局兜底表。
        """
        seen = []
        for value in self.BAUDS or ():
            try:
                n = int(value)
            except (TypeError, ValueError):
                continue
            if n > 0 and n not in seen:
                seen.append(n)
        if not seen:
            return ()

        # 最可能的那一档提到最前，其余保持降序（高档位通常意味着更好的线材与更小的采样延迟）
        likely = []
        try:
            default = int(self.DEFAULT_BAUD or 0)
        except (TypeError, ValueError):
            default = 0
        if default > 0:
            likely = [default]
        return tuple(likely + [n for n in sorted(seen, reverse=True) if n not in likely])

    def supports_baud(self, baud):
        """该型号是否声明支持这个波特率（未声明 BAUDS 时一律返回 True，不做限制）。"""
        table = self.bauds()
        if not table:
            return True
        try:
            return int(baud) in table
        except (TypeError, ValueError):
            return False

    def read_uart(self, port=None):
        """回读模块自己报的串口配置（波特率/数据位/校验/停止位/流控）。

        为什么要问模块而不是靠"逐档递增去试"：波特率是**模块持有的状态**，
        厂商给了回读命令就应该是第一信息来源。真机上的差别很直观 ——
        逐档探测每错一档要白等 2 秒超时（LG290P 五档最坏 8 秒以上），
        而回读是一次 200 ms 的查询，还能顺带确认数据位/校验位有没有被人改过。

        回读**不能替代首连**：串口参数不对时命令根本发不进去，所以流程仍是
        「先按候选表把连接建立起来 → 立刻回读 → 用回读值校正并记住」。
        回应格式各家不同（Quectel 一条定长字段、和芯星通三个口各回一行），
        所以解析必须由驱动实现；基类只负责"没声明就别假装支持"。
        """
        return False, f'{self.DISPLAY_NAME} 不支持回读串口配置', {'error': 'unsupported'}

    def set_cap(self, key, value):
        """运行期改写能力位（错误码 3 时用，方案 §4.5）。返回是否发生了变化。"""
        changed = bool(self.runtime_caps.get(key)) != bool(value)
        self.runtime_caps[key] = bool(value)
        if changed:
            logging.info(f'{self.DISPLAY_NAME} 能力位 {key} 更新为 {bool(value)}')
        return changed

    def body(self, key, **kwargs):
        """按键名取命令体并做参数替换。缺键/缺参数直接抛 DriverBug（属于编程错误）。"""
        template = self.CMD.get(key)
        if template is None:
            raise DriverBug(f'{self.MODEL_KEY} 驱动没有命令键 {key!r}')
        if not kwargs:
            if '{' in template:
                # 少传参数会把 '{x}' 原样发出去，模块只会回 ERROR,1，现场极难定位
                raise DriverBug(f'命令 {key} 需要参数，但一个都没传：{template}')
            return template
        return template.format(**kwargs)

    def verify_checksum(self, key, body):
        """方案 §4.1：表里记过校验和的命令，下发前必须与实时计算值一致。"""
        expected = self.CS_EXPECT.get(key)
        if expected and checksum(body) != expected.upper():
            raise DriverBug(
                f'命令 {key} 的校验和不符：表里记 {expected}，实算 {checksum(body)}'
                f'（命令体 {body}）—— 先修表，不要发出去')

    # ---------- 回读校验（方案 §14 #6） ----------
    def apply_and_verify(self, apply, check, retries=None):
        """下发 → 回读比对；不一致就**重发一次**再回读，两次都不符才判失败。

        `apply()` 负责重发配置（需要时再存一次 NVM），`check()` 负责"回读并比对"，
        两者都返回 `(success, message, data)`。把重试放在基类而不是每条 `set_*`
        里各写一遍，是因为三处各写一遍必然有一处忘了重试 —— 而漏掉的那处，
        表现就是"偶尔标定失败一次，重来就好"，现场只会当成玄学。

        `data['verify_attempts']` 记录实际回读了几次，随记录落库，事后能答
        "这条参数当初是不是靠重试才生效的"。
        """
        retries = VERIFY_RETRY if retries is None else max(0, int(retries))
        ok, message, data = check()
        if ok:
            return True, message, _with_attempts(data, 1)

        first_message, first_data = message, data
        for attempt in range(2, retries + 2):
            applied, apply_message, apply_data = apply()
            if not applied:
                return False, f'{first_message}；重发配置也失败：{apply_message}', apply_data
            ok, message, data = check()
            if ok:
                logging.warning(
                    f'{self.MODEL_KEY} 首次回读不一致（{first_message}），'
                    f'重发一次配置后已对上')
                return True, f'{message}（首次回读不一致，已重发一次）', \
                    _with_attempts(data, attempt)

        detail = f'{first_message}（已重发配置并再次回读，结果仍然不符）'
        final = _with_attempts(first_data, retries + 1)
        if isinstance(final, dict):
            final['retried'] = True
        return False, detail, final

    def request(self, key, line_timeout=None, retries=None, **kwargs):
        """按语义键下发并等应答，返回 `(success, message, data)` 三元组。

        这是所有接口的唯一出口，保证错误码文案、超时重试、回显丢弃只有一份实现。
        """
        body = self.body(key, **kwargs)
        self.verify_checksum(key, body)

        if key in ('reboot',):
            # §7.3：SRR 文档没有 OK 应答，等它必然超时
            return self._reboot_sequence(body)

        status, line = self.channel.send(body, timeout=line_timeout, retries=retries)
        if status == 'timeout':
            return False, f'{body.split(",")[0]} 无应答（已重试）', {'error': 'timeout', 'body': body}
        if status == 'error':
            code = self._parse_error_code(line)
            info = error_info(code)
            return False, f'{info["error_text"]}（错误码 {code}）；{info["advice"]}', info

        # 去掉语句头和 `*XX` 校验和尾巴再切字段：否则最后一个字段会带上 `*3C`，
        # 表现为"SN 末尾多了三个字符""模式解析成 unknown"这类诡异问题。
        # 语句头用 `heads` 而不是写死 `$`：N4 的应答是 `#VERSIONA,...`，
        # 写死 `$` 的话首字段仍是 `#VERSIONA`，`fields[0]` 会取到版本号而不是语句名。
        payload = line.strip().lstrip(''.join(self.channel.heads or ('$',))).split('*')[0]
        fields = payload.split(',')[1:]
        if fields and fields[0].upper() == 'OK':
            fields = fields[1:]
        return True, line.strip(), {'fields': fields, 'raw': line.strip()}

    @staticmethod
    def _parse_error_code(line):
        # 先掐掉 `*XX` 再找数字，否则最后一个字段是 '1*3F'，isdigit() 永远 False，
        # 错误码恒为 -1，用户拿到的永远是"未知错误码"。
        payload = line.strip().lstrip('$#').split('*')[0]
        parts = payload.split(',')
        for item in parts[1:]:
            item = item.strip()
            if item.lstrip('-').isdigit():
                return int(item)
        return -1

    # ---------- 重启判定（方案 §8.5） ----------
    def boot_frame_name(self):
        """重启后该等哪条开机首帧。由驱动声明 `BOOT_FRAME`，基类不自带厂商知识。

        返回空串表示"本型号没有可靠的开机首帧"——此时 `_reboot_sequence()`
        不做盲等，直接按"串口是否恢复出数据"判定。
        """
        return str(getattr(self, 'BOOT_FRAME', '') or '').strip().upper()

    def _reboot_sequence(self, body):
        """发重启命令 → 关串口 → 等 800ms → 重开 → 等**该型号的**开机首帧。

        为什么不 `sleep(5)`：文档没给重启耗时，固定 sleep 要么白等要么不够；
        "每次成功启动都会先输出"的那条语句是唯一可靠的同步点。**那条语句叫什么，
        各家不一样**，所以名字取自 `BOOT_FRAME`（驱动声明），这里不出现任何厂商命令名。

        Quectel 真机实测（LG290P03AANR01A03S / CH340 / 115200）：首帧出现在 5.67 s，
        而 SRR 之后模块还会继续吐 5.6 s 旧数据 —— 所以"看到数据"不代表"重启完了"。

        **降级判定（方案 §8.5）**：等不到首帧但串口确实在出数据，也算重启完成。
        基站模式下模块关掉 NMEA 只播 RTCM，部分固件连开机首帧都不打，此时若死等，
        会把"重启成功"判成"模块未重启"——而重试一次又要多等一轮。
        真正的"没重启"特征是**串口完全静默**，只有那种情况才判失败。

        驱动声明 `BOOT_FRAME = ''` 时（如和芯星通 N4）跳过盲等，直接按数据流判定：
        这类型号根本没有公认的开机首帧，等也是一定超时的。
        """
        before = dict(self.channel.counters)
        self.channel.write_line(body)
        self.channel.reopen(delay=REBOOT_REOPEN_DELAY)

        boot_name = self.boot_frame_name()
        if boot_name:
            # 注意不要在这里 drain：重开之后第一个到达的就是开机首帧本身。
            found, line = self.channel.wait_for(boot_name, timeout=REBOOT_BOOT_TIMEOUT)
        else:
            # 没有开机首帧的型号：给一小段窗口收数据，够判"活着"就行，
            # 不必拖满 REBOOT_BOOT_TIMEOUT —— 那 20 秒是留给"等一个特定帧"的。
            found, line = self._await_boot_activity()

        received = ((self.channel.counters.get('lines', 0) - before.get('lines', 0))
                    + (self.channel.counters.get('frames', 0) - before.get('frames', 0)))
        if not found:
            if received <= 0:
                return False, (f'重启后 {REBOOT_BOOT_TIMEOUT:.0f} 秒内串口完全静默'
                               f'（既无开机首帧也无数据），模块可能没有重启'), {
                    'error': 'no_boot_frame', 'silent': True,
                    'boot_frame_name': boot_name}
            boot = self.channel.cache.get('ver') or {}
            logging.warning(
                f'{self.MODEL_KEY} 重启后没等到开机首帧'
                f'（{boot_name or "该型号无开机首帧声明"}），但串口已收到 {received} 条数据，'
                f'按"已重启"处理')
            return True, '重启完成（无开机首帧，按串口恢复输出判定）', {
                'ver': boot, 'firmware': boot.get('ver_str'),
                'build_date': boot.get('build_date'), 'boot_frame': False,
                'received': received, 'raw': ''}

        boot = self.channel.cache.get('ver') or {}
        return True, '重启完成', {'ver': boot, 'firmware': boot.get('ver_str'),
                                 'build_date': boot.get('build_date'),
                                 'boot_frame': True, 'raw': line.strip()}

    def _await_boot_activity(self, timeout=REBOOT_BOOT_TIMEOUT):
        """没有开机首帧的型号：等到**任意一条**有效行就返回，最多等 `timeout` 秒。

        和 `wait_for()` 的区别只是"不等某个特定名字"。返回 `(found, line)`，
        found 为 False 时由调用方用计数器区分"静默"和"来了点数据但不是语句"。
        """
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False, ''
            line = self.channel.read_line(min(remaining, 1.0))
            if line:
                return True, line
            # 整帧 RTCM 不进 `_next_line` 的返回值，但它确实是"活着"的证据：
            # 基站模式下模块只播 RTCM，这时一行 NMEA 都等不到。
            if (self.channel.counters.get('frames') or 0) > 0:
                return True, ''

    # ---------- 必须由子类实现的能力（方案 §5.1） ----------
    def detect(self):
        """发识别命令，判断眼前这块是不是本驱动的型号。"""
        raise NotImplementedError

    def info(self):
        """型号 / 固件 / SN / 唯一 ID / 能力标记。"""
        raise NotImplementedError

    def get_mode(self):
        """'rover' / 'base' / 'unknown'。"""
        return self._unsupported('读取工作模式')

    def set_mode(self, mode, persist=True, restart=True):
        """切模式（含可选保存 + 重启 + 回读）。"""
        return self._unsupported(f'切换到 {mode} 模式')

    def set_mode_memory_only(self, mode):
        """**只改运行态**切模式：不 SAVEPAR、不重启、不碰 NVM（附录D）。

        给「只测坐标（不改模块）」用：模块当前是基准站时不切就没法做 CORS 采集，
        切了又怕把用户正在用的配置改掉。仅内存切换两头都满足 —— 任务结束后
        由调用方切回去，断电重启也会从 NVM 读回原配置。

        子类只有在"不下发 SAVEPAR 也能让模式立刻生效"时才实现这个方法。
        做不到就必须保持这里的默认实现（返回失败），让上层明确告诉用户
        "这个型号没法在不改配置的前提下测量"，而不是偷偷写一次 NVM。
        返回 `(ok, message, data)`，与其它驱动方法一致。
        """
        return (False, '本型号驱动不支持仅内存切换模式（必须保存到 NVM 才生效）',
                {'error': 'unsupported_mem_mode'})

    def get_fixed_ecef(self):
        """读固定坐标配置（注意：Survey-in 模式下是 0.0000，不是收敛结果）。"""
        return self._unsupported('读取固定坐标')

    def set_fixed_ecef(self, x, y, z, persist=True, restart=True, verify=True):
        """写固定坐标（含可选回读校验）。"""
        return self._unsupported('写入固定坐标')

    def config_survey_in(self, count, acc_limit, distance=None, persist=True, restart=True):
        """配 Survey-in。"""
        return self._unsupported('配置 Survey-in')

    def survey_status(self):
        """读最近一条 `$PQTMSVINSTATUS`（模块主动输出，不能主动查询）。"""
        return self._unsupported('读取 Survey-in 状态')

    def config_messages(self, **kwargs):
        """配 GGA / RTCM / MSM 的输出速率。"""
        return self._unsupported('配置报文输出速率')

    def set_antenna_info(self, desc, setup_id=0, sn='', persist=True):
        """写 RTCM3-1033 用的天线信息。不支持时返回 success=False 并说明。"""
        return self._unsupported('写入天线信息（1033）')

    # ---------- 可选协议面：默认"不支持"，绝不允许 AttributeError ----------
    # 下面这组方法不是每个型号都有对应命令，但**流程层会无条件调用**：
    # 「读取模块信息」的诊断报告要读 RTCM 配置，备份/还原要逐项回读。
    # 早先 LC29H(BS) 漏实现 `read_rtcm_config`，真机上点一次诊断就 500：
    #   AttributeError: 'LC29HBSDriver' object has no attribute 'read_rtcm_config'
    # 漏一个方法 = 一个 500；而"这个型号没有这条命令"本来是一件正常、可解释的事。
    # 所以基类把整张可选协议面给全默认实现：子类有命令就覆盖，没有就继承，
    # 上层永远拿到 `(False, '为什么不行', {...})` 而不是异常。
    # 这也是新增驱动时的一方保险 —— 新驱动哪怕只实现 detect/info 也能跑完整条链路。
    def _unsupported(self, what, hint=''):
        message = f'{self.DISPLAY_NAME} 不支持{what}（驱动未实现该命令）'
        if hint:
            message = f'{message}；{hint}'
        return False, message, {'error': 'unsupported', 'what': what}

    def get_svin_config(self):
        """读坐标/Survey-in 配置。没有该命令的型号继承默认实现。"""
        return self._unsupported('读取坐标配置')

    def read_rtcm_config(self):
        """回读 RTCM 播发配置（诊断报告、备份都要用）。"""
        return self._unsupported('查询 RTCM 配置')

    def restore_rtcm_config(self, fields):
        """把备份里的 RTCM 字段原样写回（还原专用）。"""
        return self._unsupported('回写 RTCM 配置')

    def config_rtcm(self, msm_type=4, persist=True, **kwargs):
        """配置 RTCM 播发参数（MSM 档位等）。"""
        return self._unsupported('配置 RTCM 参数')

    def get_message_rate(self, name, msg_ver=None):
        """查某条语句的输出速率。"""
        return self._unsupported(f'查询报文 {name} 的速率')

    def set_message_rate(self, name, rate, port=None, **kwargs):
        """配某条语句的输出速率。"""
        return self._unsupported(f'配置报文 {name} 的速率')

    def disable_survey_in(self, persist=True, restart=False):
        """停用 Survey-in / 固定坐标，避免旧配置继续收敛。"""
        return self._unsupported('停用 Survey-in')

    def read_antenna_info(self):
        """回读天线信息（1033 用）。"""
        return self._unsupported('读取天线信息')

    def save(self):
        """写 NVM。"""
        return self._unsupported('保存参数到 NVM')

    def reboot(self):
        """软重启并等开机首帧（等哪一条由 `BOOT_FRAME` 声明，见 `_reboot_sequence`）。"""
        return self._unsupported('重启模块')

    def parse_line(self, text):
        """解析一行。基类已覆盖 GGA/RMC/GLL/GSA/SVINSTATUS/VER，子类按需扩展。"""
        return parse_line(text)
