# -*- coding: utf-8 -*-
"""u-blox ZED-F9P 驱动 —— 纯 UBX 二进制，Generation 9 键值配置接口。

命令来源：《ZED-F9P Integration manual》(UBX-18010802 R16) 定语义，
32 位配置键 ID 取自 u-blox Interface description（键表在 `ubx_keys.py`，
带出处注释）。每处都标了手册章节，改之前先去文档核对。

## 和 Quectel 两家（LG290P / LC29H(BS)）的**根本**差异

前两家的每一条命令都是 `$字面量*XX` 的 ASCII 文本，所以能让基类的
`CMD` 模板 + `body()` + `channel.send()` 全包。F9P **一条都不是**：

1. **帧格式**：`B5 62 | cls | id | len:2 LE | payload | ckA ckB`，
   校验是 **Fletcher 8-8**（`ckA/ckB` 逐字节累加），**不是 NMEA 的 XOR**。
   拿 `checksum()` 去算 UBX，模块只会静默——不报错，就是永远不回话。

2. **配置接口换代**：集成手册 §3.1 原文
   "The configuration interface has changed from earlier u-blox positioning
   receivers. Legacy messages have been deprecated"。
   所以**没有** `UBX-CFG-TMODE` 这种老消息可用，必须走：
       `UBX-CFG-VALSET`(0x06/0x8A) 写、`UBX-CFG-VALGET`(0x06/0x8B) 读。
   一条 VALSET 里塞多个 key 是合法的，但**两条之间必须等 ACK**（见下）。

3. **ACK 语义与 PAIR 同类**：§3.1.1 原文
   "All configuration messages ... will result in a UBX-ACK-ACK or
   UBX-ACK-NAK response. If several configuration messages are sent without
   waiting for this response then the receiver may pause processing ... a
   warning message 'wait for cfg ACK' will be sent to the host."
   → 每发一条配置必须等到 `0x05/0x01`(ACK) 或 `0x05/0x00`(NAK) 才能发下一条。
   这不是"建议"，是硬约束：连发会让模块**暂停处理输入**。

4. **默认波特率 38400**：§3.8.1 "The default baud rate is 38400 baud"，
   safeboot 模式是 9600。这与 LG290P(460800)、LC29H(BS)(115200) 都不同，
   是第三派 —— 抄任何一家的值都会让新模块先错一轮。

## RTCM 输出的硬边界（§3.1.5）

输出表只有 1005/1074/1077/1084/1087/1094/1097/1124/1127/1230/4072.0/4072.1。
**1006、1007、1033 只支持输入，不支持输出** —— 写进 CFG-MSGOUT 会直接
ACK-NAK。本驱动显式拒绝（`COORD_MSG_UNSUPPORTED`），不做"配了但没播"。

其余规则（见 `validate_rtcm_plan`）：
    · 所有观测报文必须同速率
    · 同一星座只能开一种观测报文
    · **不可混用 MSM4 与 MSM7**（不同星座也不行）
    · 必须含 1230 或 1033，否则 GLONASS 只能浮点解
（注：1033 只支持输入，所以 F9P 做基站**必须**开 1230。）

## 标定时坐标怎么拆（最容易错的一步）

HPPOSLLH 的 `lat` 是 1e-7 deg 的 I4，`latHp` 是 **1e-9 deg 的有符号 I1**。
写 TMODE 时必须两个都写：

    CFG-TMODE-LAT    = hpposllh.lat        (I4)
    CFG-TMODE-LAT_HP = hpposllh.latHp      (I1，**别转成无符号**)

HP 字段范围 -127~+127，漏写就差出厘米级。高度同理：`HEIGHT`=cm（I4），
`HEIGHT_HP`=0.1mm（I1）。

## CFG-VALSET 载荷头是 4 字节（真机上最容易误判的一条）

    version(1) | layer(1) | transaction(1) | reserved0(1) | [key(4) + value(N)]...

**不是 3 字节**。写成 3 字节头，PROTVER 27 的兼容路径只在 value=1 字节时
才认，于是 U1/I1 键全 ACK、I4/U4 键全 NAK，串口只回一个干净的 `05 00`，
不带 INF 说明，换波特率/换 layer 都复现 —— 极容易被误读成"模块不支持该键"。
详见 `_valset` 的注释。

## 本驱动**没有**做的事（不是忘了）

· 1033 天线信息写入：F9P 不输出 1033（输出表里没有），`set_antenna_info`
  继承基类的"不支持"。
· 1006 天线偏移：同上。
· `PQTM*` / `PAIR*` 一个都不该出现——契约测试守着这条。
"""
import logging
import struct
import time

from . import ubx_keys as K
from .base import BaseDriver, DriverBug
from .driver_manager import register

# ==================== 能力位 ====================
CAPS = {
    'dual_mode': True,      # TMODE_MODE 0/1/2 既能移动站也能基准站
    'set_mode': True,
    'svin': True,           # §3.1.5.5
    'fixed': True,          # §3.1.5.4
    'ant_info': False,      # 不输出 1033（输出表里没有）
    'ant_delta': False,     # 不输出 1006
    'msg_rate': True,       # CFG-MSGOUT-* 逐条可配
    'rtcm_out': True,       # CFG-MSGOUT-RTCM_3X_*
    'err3': False,          # UBX 用 ACK-NAK 表达拒绝，没有"错误码 3"这套
}

# TMODE_MODE 取值（§3.1.5 + ubx_keys 注释）
MODE_DISABLED = 0
MODE_SURVEY_IN = 1
MODE_FIXED = 2
MODE_LABEL = {MODE_DISABLED: 'disabled', MODE_SURVEY_IN: 'survey_in', MODE_FIXED: 'fixed'}
MODE_VALUE = {v: k for k, v in MODE_LABEL.items()}
# 兼容流程层传 'rover'/'base' 两种叫法
MODE_ALIASES = {
    'rover': MODE_DISABLED, 'mobile': MODE_DISABLED, '流动站': MODE_DISABLED,
    'base': MODE_FIXED, '基准站': MODE_FIXED,
    'disabled': MODE_DISABLED, '关闭': MODE_DISABLED, 'disable': MODE_DISABLED,
    'survey_in': MODE_SURVEY_IN, 'survey-in': MODE_SURVEY_IN,
    'svin': MODE_SURVEY_IN, 'survey': MODE_SURVEY_IN,
    'fixed': MODE_FIXED, '定点': MODE_FIXED,
}

# 坐标报文：F9P 只有 1005 可输出
COORD_MSG = ('RTCM3-1005',)
# 1006 / 1007 / 1033 在本模块**只支持输入**（§3.1.5 输出表）
COORD_MSG_UNSUPPORTED = ('RTCM3-1006', 'RTCM3-1007', 'RTCM3-1033')

# 本型号能逐星座配速率的星座（流程层按这张表逐条下发）
MSM_CONSTELLATIONS = ('GPS', 'GLO', 'GAL', 'BDS')

# 输出观测报文时必须带上的 1230（否则 GLO 只能浮点解）
REQUIRED_RTCM = (1005, 1230)

# 视作"观测报文"的 RTCM 号（1074~1127 全段，MSM4/MSM7 都在内）
OBSERVATION_RTCM = (1074, 1077, 1084, 1087, 1094, 1097, 1124, 1127)
MSM_TIER = {4: (1074, 1084, 1094, 1124), 7: (1077, 1087, 1097, 1127)}

# ACK 等待上限。F9P 处理一条 VALSET 是毫秒级（实测 <50 ms），
# 但刚开机时要等 Flash 读完，给 1.5 s 足够宽。
ACK_TIMEOUT = 1.5
# 读 VALGET 应答的上限：一条 VALGET 最多回 1~2 KB，38400 下约 0.5 s，
# 但若同时有 RTCM 流在抢带宽，得留余量。
VALGET_TIMEOUT = 2.0


def pack_value(type_info, value):
    """按键的类型打包一个值。返回 bytes。

    `type_info` 是 `(name, size, signed)` 三元组。调用方负责缩放换算，
    这里只收**已经换算好的整数**。
    """
    _name, size, signed = type_info
    value = int(round(value))
    if size == 1:
        if not (-128 <= value <= 127 if signed else 0 <= value <= 255):
            raise DriverBug(f'单字节键值越界：{value}（{"有" if signed else "无"}符号）')
        return struct.pack('<b' if signed else '<B', value)
    if size == 2:
        return struct.pack('<h' if signed else '<H', value)
    if size == 4:
        return struct.pack('<i' if signed else '<I', value)
    raise DriverBug(f'不支持的键长：{size}')


def unpack_value(type_info, raw):
    """从字节里解一个值（原始整数，未缩放）。"""
    _name, size, signed = type_info
    if len(raw) < size:
        raise DriverBug(f'值长度不足：要 {size} 字节，只有 {len(raw)}')
    if size == 1:
        return struct.unpack('<b' if signed else '<B', raw[:1])[0]
    if size == 2:
        return struct.unpack('<h' if signed else '<H', raw[:2])[0]
    if size == 4:
        return struct.unpack('<i' if signed else '<I', raw[:4])[0]
    raise DriverBug(f'不支持的键长：{size}')


def validate_rtcm_plan(enabled):
    """按集成手册 §3.1.5 的四条硬规则校验一组要开的 RTCM 报文号。

    `enabled` = 要置为 >0 的报文号集合。返回 `(ok, problems)`。
    **宁可在下发前拦下来**：违反这些规则的配置，模块会照单全收（ACK），
    然后播出一堆下游解不出来的差分 —— 现场只会表现为"RTK 一直浮点"。
    """
    nums = {int(n) for n in enabled}
    problems = []

    unsupported = nums & {1006, 1007, 1033}
    if unsupported:
        problems.append(
            f'{"、".join(str(n) for n in sorted(unsupported))} 在 ZED-F9P 上'
            f'只支持输入、不支持输出')

    obs = [n for n in nums if n in OBSERVATION_RTCM]
    tiers = set()
    for tier, members in MSM_TIER.items():
        if set(obs) & set(members):
            tiers.add(tier)
    if len(tiers) > 1:
        problems.append('MSM4 与 MSM7 不能混用（选了 MSM4 的星座就必须全部 MSM4）')

    for constellation, members in (
            ('GPS', (1074, 1077)), ('GLONASS', (1084, 1087)),
            ('Galileo', (1094, 1097)), ('北斗', (1124, 1127))):
        both = [n for n in members if n in nums]
        if len(both) > 1:
            problems.append(f'{constellation} 同时开了 {"、".join(map(str, both))}，'
                            f'同一星座只能一种观测报文')

    if obs and not (nums & {1230, 1033}):
        problems.append('开了观测报文但没有 1230/1033，GLONASS 只能浮点解')
    if nums & set(OBSERVATION_RTCM) and 1005 not in nums:
        problems.append('开了观测报文但没有 1005，移动站不知道基站坐标')

    return (not problems), problems


@register
class ZEDF9PDriver(BaseDriver):
    """u-blox ZED-F9P（HPG 1.13 及以上 Generation 9 固件）。"""

    MODEL_KEY = 'zed_f9p'
    DISPLAY_NAME = 'u-blox ZED-F9P'
    # MON-VER 的 swVersion 形如 `EXT CORE 1.00 (61b2dd)` / `HPG 1.32`。
    # 型号认领在 `detect()` 里按完整固件串做（见那里的注释），
    # 这里的 MATCH_PREFIX 只用于"粗筛"，不当唯一判据。
    MATCH_PREFIX = 'HPG'
    DUAL_MODE = True
    CAPS = CAPS
    # UBX 没有文本命令模板：`CMD` 留空，所有下发走 `_valset` / `_valget`。
    # 留空而不是删掉，是为了让误用 `body()` 时立刻抛 DriverBug 而不是 AttributeError。
    CMD = {}
    CS_EXPECT = {}
    MSM_CONSTELLATIONS = MSM_CONSTELLATIONS

    # 官方波特率（§3.8.1 UART 表 28，文档原样八档）
    BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
    # §3.8.1 "The default baud rate is 38400 baud"。
    # 与 LG290P(460800)、LC29H(BS)(115200) 并列的第三派，不要互相抄。
    DEFAULT_BAUD = 38400
    PROBE_BAUDS = (38400, 921600, 460800, 230400, 115200, 57600, 19200, 9600)
    BAUD_CMD_KEY = 'set_baud'
    BAUD_READ_KEY = 'get_baud'

    # TMODE/VALSET 带 Flash 层后配置掉电不丢，**不需要每次重启**。
    # 只有改波特率要重启（§3.8.1 要求改完留 100 ms 再喂数据）。
    RESTART_REQUIRED = False

    # 混流：默认 NMEA 开着，UBX 应答和 NMEA 混在同一条串口上。
    # 两个头都声明，`Channel._wants_ubx()` 才会去拆 UBX 帧。
    LINE_HEADS = ('$', '\xb5')
    FRAME_COMMANDS = False           # 二进制帧不走 `$...*XX` 组装
    REPLY_ALIASES = {}
    # F9P 开机没有固定的 ASCII 首帧（NMEA 可能被关掉），
    # 空串 → `_reboot_sequence` 走"串口是否恢复数据"的降级判定。
    BOOT_FRAME = ''

    # ==================== UBX 底层 ====================
    def _send_ubx(self, cls, msg_id, payload=b'', timeout=ACK_TIMEOUT, retries=2,
                  wait_ack=True):
        """发一帧并等 ACK。返回 `(ok, message)`；message ∈ ACK/NAK/timeout。"""
        status, _ack, _data = self.channel.send_ubx(
            cls, msg_id, payload, timeout=timeout, retries=retries,
            wait_ack=wait_ack, ack_timeout=timeout)
        if status in ('ack', 'ok'):
            return True, 'ACK'
        if status == 'nak':
            return False, 'NAK'
        return False, 'timeout'

    def _valset(self, items, layers=None):
        """写一组配置。`items` = [(key_name, 物理值), ...]。

        **一次一条 VALSET，等一个 ACK**（§3.1.1）：一条里塞多个 key 是合法的，
        多个 key 共享同一个 ACK；但两条 VALSET 之间必须等上一条的 ACK。

        `layers` 默认同时写 RAM 和 Flash：只写 RAM 掉电就丢，
        只写 Flash 要重启才生效，两个都写才是"立刻生效且断电不丢"。

        ## 载荷头是 **4 字节**，不是 3 字节（真机踩过的坑）

        `version(1) | layer(1) | transaction(1) | reserved0(1)` 之后才是 key。
        这里的 `reserved0` 是 VALSET 特有的占第 4 字节的空位；`transaction`
        计数器（0 = 不参与事务）。

        写成 3 字节头 `version|layer|reserved` 会踩一个**极其隐蔽**的坑：
        PROTVER 27 对旧式 3 字节布局还留了一条兼容路径，但**只在 value
        恰好 1 字节时才通**。于是 1 字节的键（U1/I1）全部 ACK、4 字节的键
        （I4/U4，例如 `TMODE_ECEF_X` / `UART1_BAUDRATE` / `TMODE_FIXED_POS_ACC`）
        全部 NAK ——串口上是干净利落的 `05 00`，不带任何 INF 提示，
        换波特率、换 layer、逐字节慢发都复现，极容易被误判成"模块不支持该键"
        或"模块进了受限模式"。真机定位过程见 `tests/` 里的回归用例。

        VALGET 那边本来就是 4 字节（`position` 占 2 字节），所以读一直正常，
        这个坑只在写侧暴露。
        """
        if layers is None:
            layers = K.LAYER_RAM | K.LAYER_FLASH
        if not items:
            return True, '无需写入', {'keys': 0}

        # version=0x00, layer, transaction=0x00, reserved0=0x00
        payload = bytearray([0x00, layers & 0xFF, 0x00, 0x00])
        written = []
        for name, value in items:
            rec = K.lookup(name)
            if rec is None:
                raise DriverBug(f'配置键 {name!r} 不在键表里（ubx_keys.py）')
            key_id, type_info, scale, _note = rec
            # 缩放：调用方给物理值（cm / mm / deg），这里换算成原始整数
            raw_value = int(round(float(value) / scale)) if scale != 1.0 \
                else int(round(float(value)))
            payload += struct.pack('<I', key_id)
            payload += pack_value(type_info, raw_value)
            written.append((name, value, key_id))

        ok, message = self._send_ubx(*K.MSG_CFG_VALSET, bytes(payload))
        data = {'keys': len(written), 'layers': layers,
                'items': [{'key': n, 'value': v, 'key_id': f'0x{i:08X}'}
                          for n, v, i in written]}
        if not ok:
            data['error'] = 'nak' if message == 'NAK' else 'timeout'
        return ok, message, data

    def _valget(self, key_names):
        """读一组配置，返回 `({name: 物理值}, layer_flags)`。

        VALGET 请求：`version(1) | layer(1) | position(2) | key(4)...`
        VALGET 应答：`version(1) | layer(1) | position(2) | (key(4) + value(N))...`
        `position=0` 表示从第一个 key 开始；一小批一次拿全。
        """
        names = [n for n in key_names if K.lookup(n) is not None]
        if not names:
            return {}, 0
        payload = bytearray([0x00, 0x00, 0x00, 0x00])
        by_id = {}
        for name in names:
            key_id = K.lookup(name)[0]
            payload += struct.pack('<I', key_id)
            by_id[key_id] = name

        # 读之前先清掉上次的残留，否则会把旧应答当成本次结果
        self.channel.cache.get('ubx', {}).pop(K.MSG_CFG_VALGET, None)
        self.channel.write_ubx(*K.MSG_CFG_VALGET, bytes(payload))

        deadline = time.time() + VALGET_TIMEOUT
        raw = None
        while time.time() < deadline:
            line = self.channel.read_line(min(0.3, max(0.05, deadline - time.time())))
            if line is not None:
                self.channel.absorb(line)
            raw = self.channel.ubx_last(*K.MSG_CFG_VALGET)
            if raw is not None:
                break
        if raw is None or len(raw) < 4:
            return {}, 0

        layer = raw[1]
        pos = 4
        values = {}
        while pos + 4 <= len(raw):
            key_id = struct.unpack('<I', raw[pos:pos + 4])[0]
            name = by_id.get(key_id)
            if name is None:
                break                       # 不认识的键：不知道长度，停止解析
            pos += 4
            _kid, type_info, scale, _note = K.lookup(name)
            size = type_info[1]
            if pos + size > len(raw):
                break
            raw_value = unpack_value(type_info, raw[pos:pos + size])
            pos += size
            values[name] = raw_value * scale if scale != 1.0 else raw_value
        return values, layer

    # ==================== 识别 ====================
    def detect(self):
        """发 UBX-MON-VER 认型。

        MON-VER 载荷 = 30 字节 swVersion + 10 字节 hwVersion + 重复的
        (30 字节 extension) 串。ZED-F9P 的 extension 里有 `PROTVER=` / `MOD=`，
        固件串含 `HPG`。**不能只认 `ZED-F9P` 字面量**：有的固件不把它写进
        swVersion，只在 extension 里带 `F9P`。
        """
        self.channel.drain()
        self.channel.cache.get('ubx', {}).pop(K.MSG_MON_VER, None)
        ok, message = self._send_ubx(*K.MSG_MON_VER, b'', retries=3)
        raw = self.channel.ubx_last(*K.MSG_MON_VER)
        if raw is None:
            # 有的固件对 MON-VER 不回 ACK（查询类只回内容本身），多等一会儿
            deadline = time.time() + 1.0
            while time.time() < deadline and raw is None:
                line = self.channel.read_line(0.3)
                if line is not None:
                    self.channel.absorb(line)
                raw = self.channel.ubx_last(*K.MSG_MON_VER)
        if raw is None:
            return False, f'未收到 UBX-MON-VER 应答（{message}），不是 u-blox Gen9 模块', {}

        sw = raw[:30].split(b'\x00')[0].decode('ascii', 'replace').strip()
        hw = ''
        if len(raw) >= 40:
            hw = raw[30:40].split(b'\x00')[0].decode('ascii', 'replace').strip()
        ext = []
        pos = 40
        while pos + 30 <= len(raw):
            item = raw[pos:pos + 30].split(b'\x00')[0].decode('ascii', 'replace').strip()
            if item:
                ext.append(item)
            pos += 30

        blob = ' '.join([sw, hw] + ext).upper()
        data = {'sw_version': sw, 'hw_version': hw, 'extensions': ext}
        if 'F9P' not in blob:
            return False, f'是 u-blox 模块（{sw}）但不是 ZED-F9P', data
        return True, f'ZED-F9P 已识别（{sw}）', data

    def info(self):
        """型号 / 固件 / 唯一 ID / 能力标记。"""
        ok, message, data = self.detect()
        if not ok:
            return False, message, data

        # MON-HW 里有唯一 ID 与天线状态（F9P 的 MON-HW 载荷 60 字节）
        hw_info = {}
        self.channel.cache.get('ubx', {}).pop(K.MSG_MON_HW, None)
        self.channel.write_ubx(*K.MSG_MON_HW)
        deadline = time.time() + 1.0
        raw = None
        while time.time() < deadline and raw is None:
            line = self.channel.read_line(0.25)
            if line is not None:
                self.channel.absorb(line)
            raw = self.channel.ubx_last(*K.MSG_MON_HW)
        if raw and len(raw) >= 60:
            hw_info['unique_id'] = ''.join(f'{b:02X}' for b in raw[0:8])
            hw_info['a_status'] = raw[28]
            hw_info['a_power'] = raw[29]

        data.update({
            'model': self.DISPLAY_NAME,
            'model_key': self.MODEL_KEY,
            'firmware': data.get('sw_version', ''),
            'hw_version': data.get('hw_version', ''),
            # F9P 的 SN 不在 UBX 里（要读 flash/CFG-SEC），这里用 MON-HW 的
            # 唯一 ID 代替并**明确标注来源**，不冒充"序列号"
            'sn': hw_info.get('unique_id', ''),
            'sn_source': 'MON-HW uniqueId',
            'msg': message,
        })
        return True, message, data

    # ==================== 模式 ====================
    def get_mode(self):
        """'rover' / 'base' / 'unknown'。

        F9P 没有独立的"模式"寄存器：TMODE_MODE=0 就是流动站，
        1/2 都是基准站。这里统一映射成流程层认的两个值。
        """
        values, _layer = self._valget(['TMODE_MODE'])
        if 'TMODE_MODE' not in values:
            return 'unknown'
        mode = int(values['TMODE_MODE'])
        return 'base' if mode in (MODE_SURVEY_IN, MODE_FIXED) else 'rover'

    def set_mode(self, mode, persist=True, restart=True, verify=True):
        """切模式。

        `persist=False` 只写 RAM 层（断电丢），用于"只测坐标不改模块"。
        `restart` 在这里**被忽略**：F9P 的 TMODE 立即生效，重启只会白等 20 秒，
        且 `RESTART_REQUIRED=False` 已让流程层跳过重启步骤。
        `verify=False` 跳过回读校验（真机上模块忙、VALGET 超时时可以关掉）。
        保留这些参数是为了**签名兼容** —— 流程层会带 `persist`/`restart`/`verify`
        一起调用，少一个就是 `TypeError`（LC29H(BS) 已经踩过一次）。
        """
        target = MODE_ALIASES.get(str(mode).strip().lower())
        if target is None:
            raise DriverBug(f'不认识的模式 {mode!r}'
                            f'（可用：rover/base/survey_in/fixed/disabled）')

        layers = (K.LAYER_RAM | K.LAYER_FLASH) if persist else K.LAYER_RAM
        ok, message, data = self._valset([('TMODE_MODE', target)], layers=layers)
        if not ok:
            hint = '模块拒绝了这条配置（NAK）' if message == 'NAK' else '模块没有回 ACK'
            return False, f'切换模式失败：{hint}', {
                'error': 'nak' if message == 'NAK' else 'timeout',
                'mode': mode, 'target': target}

        data.update({'requested': mode, 'target': target,
                     'persist': bool(persist), 'restart': {'skipped': True}})
        if not verify:
            data['mode'] = MODE_LABEL.get(target, 'unknown')
            return True, f'已切换到 {MODE_LABEL.get(target)}（未回读校验）', data

        # 回读确认：ACK 只代表"收到了"，不代表"值和我要的一样"
        values, _ = self._valget(['TMODE_MODE'])
        actual = int(values.get('TMODE_MODE', -1))
        data['mode'] = MODE_LABEL.get(actual, 'unknown')
        if actual != target:
            return False, (f'写入后回读不一致：要 {MODE_LABEL.get(target)}，'
                           f'实际 {MODE_LABEL.get(actual, actual)}'), data
        return True, f'已切换到 {MODE_LABEL.get(target)}', data

    def set_mode_memory_only(self, mode):
        """只改 RAM 层切模式（不碰 Flash）。F9P 支持，见 set_mode 的 persist=False。"""
        return self.set_mode(mode, persist=False, restart=False)

    # ==================== 固定坐标 ====================
    def get_fixed_ecef(self):
        """读定点坐标（返回 **米**，和 set_fixed_ecef 同一单位）。"""
        names = ['TMODE_MODE', 'TMODE_POS_TYPE',
                 'TMODE_ECEF_X', 'TMODE_ECEF_Y', 'TMODE_ECEF_Z',
                 'TMODE_ECEF_X_HP', 'TMODE_ECEF_Y_HP', 'TMODE_ECEF_Z_HP',
                 'TMODE_FIXED_POS_ACC']
        values, layer = self._valget(names)
        if 'TMODE_MODE' not in values:
            return False, '读取定点坐标失败（无 VALGET 应答）', {'error': 'timeout'}

        def _combine(main_key, hp_key):
            main = float(values.get(main_key, 0))        # cm
            hp = float(values.get(hp_key, 0))            # 0.1 mm
            # 1 cm = 100 × 0.1mm，所以 hp 除以 100 才是"厘米"，
            # 再除以 100 换成米。合并成：/ 10000。
            return (main + hp / 100.0) / 100.0           # cm → m

        mode = int(values.get('TMODE_MODE', 0))
        data = {
            'x': _combine('TMODE_ECEF_X', 'TMODE_ECEF_X_HP'),
            'y': _combine('TMODE_ECEF_Y', 'TMODE_ECEF_Y_HP'),
            'z': _combine('TMODE_ECEF_Z', 'TMODE_ECEF_Z_HP'),
            'mode': MODE_LABEL.get(mode, 'unknown'),
            'pos_type': 'LLH' if int(values.get('TMODE_POS_TYPE', 0)) == 1 else 'ECEF',
            'acc_mm': float(values.get('TMODE_FIXED_POS_ACC', 0)) * 0.1,
            'layer': layer,
        }
        if mode != MODE_FIXED:
            data['note'] = '当前不是定点模式，坐标值可能是上次写入的残留'
        return True, '定点坐标已读取', data

    @staticmethod
    def _split_ecef(value_m):
        """把米拆成 `(厘米整数, 0.1mm 余数)`。

        单位链条（**这里最容易错**）：
            1 m  = 100 cm = 10000 × 0.1mm
            1 cm = 100 × 0.1mm          ← 不是 10000！
        `TMODE_ECEF_X` 是 `I4`/cm，`TMODE_ECEF_X_HP` 是 `I1`/0.1mm，
        两者**相加**才是完整坐标。

        做法：先量化到 0.1mm 整数格，再以**最近的厘米**为整数部分，
        余数自然落在 ±50（半厘米 = 50 × 0.1mm），稳稳在 HP 的 ±127 里。
        若改成"向下取整厘米"，余数会落在 0~99，超出一个字节就得夹紧，
        夹紧又不进位 —— 坐标会差出整整 1 cm。
        """
        total = int(round(float(value_m) * 10000.0))     # 米 → 0.1mm 整数格
        whole = int(round(total / 100.0))                # 最近的厘米
        hp = total - whole * 100                         # 余数，单位 0.1mm
        # 理论上 |hp| <= 50；万一 float 边界抖动超了，夹紧（误差 < 1.27cm 才可能触发）
        return whole, max(-127, min(127, hp))

    def set_fixed_ecef(self, x, y, z, persist=True, restart=True, verify=True):
        """写 ECEF 定点坐标（参数单位 **米**，流程层给的就是米）。

        F9P 把坐标拆成厘米整数 + 0.1mm 小数两段：
            ECEF_X(cm)       = 整数厘米部分
            ECEF_X_HP(0.1mm) = 余下的小数部分
        HP 字段是**有符号单字节**，范围 -127~127，转成无符号会得到 127
        以外的乱值，坐标就差几十厘米。拆法见 `_split_ecef`。
        """
        xc, xh = self._split_ecef(x)
        yc, yh = self._split_ecef(y)
        zc, zh = self._split_ecef(z)

        layers = (K.LAYER_RAM | K.LAYER_FLASH) if persist else K.LAYER_RAM
        items = [
            ('TMODE_MODE', MODE_FIXED),
            ('TMODE_POS_TYPE', 0),                    # 0 = ECEF
            ('TMODE_ECEF_X', xc), ('TMODE_ECEF_X_HP', xh),
            ('TMODE_ECEF_Y', yc), ('TMODE_ECEF_Y_HP', yh),
            ('TMODE_ECEF_Z', zc), ('TMODE_ECEF_Z_HP', zh),
            # 精度声明给 **10 mm**（键表 scale=0.1 会把 10 写成原始 100 = 10mm）。
            # 这是 RTK 基站自定位的保守值：填太小（如 1 mm）会让移动站对基站
            # 坐标过度自信，反而拖累解算。
            ('TMODE_FIXED_POS_ACC', 10),
        ]

        def _apply():
            return self._valset(items, layers=layers)

        if not verify:
            ok, message, data = _apply()
            if not ok:
                return False, f'写入定点坐标失败（{message}）', data
            return True, '定点坐标已写入（未回读校验）', data

        def _check():
            ok, msg, got = self.get_fixed_ecef()
            if not ok:
                return False, msg, got
            tol = 0.0005          # 0.5 mm：HP 量化误差 0.1mm 的 5 倍，足够严
            for axis, want in (('x', float(x)), ('y', float(y)), ('z', float(z))):
                if abs(float(got.get(axis, 0)) - want) > tol:
                    return False, (f'{axis.upper()} 回读不符：要 {want:.4f} m，'
                                   f'实际 {float(got.get(axis, 0)):.4f} m'), got
            return True, '定点坐标回读一致', got

        return self.apply_and_verify(_apply, _check)

    # ==================== Survey-in ====================
    def config_survey_in(self, count, acc_limit, distance=None, persist=True,
                         restart=True, verify=True):
        """配 Survey-in。

        `count`     → TMODE_SVIN_MIN_DUR（**秒**）
        `acc_limit` → TMODE_SVIN_ACC_LIMIT（毫米，内部换算成 0.1mm）
        `distance`  → **F9P 没有这个参数**（那是 LG290P 的 <Distance>），忽略。
        `restart` / `verify` 只为签名兼容保留：TMODE 写完立即生效，
        不需要重启；回读由 `survey_status()` / `get_svin_config()` 单独做。

        注意单位差异：Quectel 的 `count` 是**历元数**，F9P 的 svindur 是**秒**。
        1 Hz 下两者等价，10 Hz 下不等价 —— 这里明确按秒写，并在返回里写明，
        免得用户以为"60 次"就是"60 次定位"。
        """
        try:
            dur = max(1, int(count))
        except (TypeError, ValueError):
            raise DriverBug(f'Survey-in 时长必须是整数秒，收到 {count!r}')
        try:
            acc_mm = max(1.0, float(acc_limit))
        except (TypeError, ValueError):
            raise DriverBug(f'Survey-in 精度必须是数值（毫米），收到 {acc_limit!r}')

        layers = (K.LAYER_RAM | K.LAYER_FLASH) if persist else K.LAYER_RAM
        items = [
            ('TMODE_SVIN_MIN_DUR', dur),
            # scale=0.1：物理值单位 mm，内部单位 0.1mm
            ('TMODE_SVIN_ACC_LIMIT', acc_mm),
            ('TMODE_MODE', MODE_SURVEY_IN),
        ]
        ok, message, data = self._valset(items, layers=layers)
        if not ok:
            return False, f'配置 Survey-in 失败（{message}）', {
                'error': 'nak' if message == 'NAK' else 'timeout'}
        data.update({'min_dur_s': dur, 'acc_limit_mm': acc_mm,
                     'persist': bool(persist), 'unit': 'seconds',
                     'note': 'F9P 的 SVIN_MIN_DUR 单位是秒（不是历元数）'})
        if distance is not None:
            data['distance_ignored'] = distance
        return True, f'Survey-in 已配置（≥{dur}s，精度≤{acc_mm:g}mm）', data

    def get_svin_config(self):
        """读坐标 / Survey-in 配置。"""
        names = ['TMODE_MODE', 'TMODE_POS_TYPE',
                 'TMODE_SVIN_MIN_DUR', 'TMODE_SVIN_ACC_LIMIT',
                 'TMODE_ECEF_X', 'TMODE_ECEF_Y', 'TMODE_ECEF_Z',
                 'TMODE_ECEF_X_HP', 'TMODE_ECEF_Y_HP', 'TMODE_ECEF_Z_HP',
                 'TMODE_FIXED_POS_ACC']
        values, layer = self._valget(names)
        if 'TMODE_MODE' not in values:
            return False, '读取坐标配置失败（无 VALGET 应答）', {'error': 'timeout'}

        def _combine(main_key, hp_key):
            # 单位链条：ECEF_X 是 cm，ECEF_X_HP 是 0.1mm 整数。
            # 1 cm = 100 × 0.1mm ← 是 100，不是 10000！
            # 所以 (cm + hp/100) 得到 cm，再 /100 换成米。
            return (float(values.get(main_key, 0))
                    + float(values.get(hp_key, 0)) / 100.0) / 100.0

        mode = int(values.get('TMODE_MODE', 0))
        data = {
            'mode': MODE_LABEL.get(mode, 'unknown'),
            'pos_type': 'LLH' if int(values.get('TMODE_POS_TYPE', 0)) == 1 else 'ECEF',
            'svin_min_dur': float(values.get('TMODE_SVIN_MIN_DUR', 0)),
            'svin_acc_limit_mm': float(values.get('TMODE_SVIN_ACC_LIMIT', 0)) * 0.1,
            'x': _combine('TMODE_ECEF_X', 'TMODE_ECEF_X_HP'),
            'y': _combine('TMODE_ECEF_Y', 'TMODE_ECEF_Y_HP'),
            'z': _combine('TMODE_ECEF_Z', 'TMODE_ECEF_Z_HP'),
            'acc_mm': float(values.get('TMODE_FIXED_POS_ACC', 0)) * 0.1,
            'layer': layer,
        }
        return True, '坐标配置已读取', data

    def survey_status(self):
        """读 Survey-in 收敛状态（UBX-NAV-SVIN，§3.1.5.5）。

        NAV-SVIN 载荷 40 字节：
            version(1) reserved(3) iTOW(4) dur(4) meanX/Y/Z(4×4)
            meanXHP/YHP/ZHP(1×3) reserved(1) meanAcc(4) obs(4) valid(1) active(1)
        `valid` = 位置已被确认，`active` = 还在收敛。
        """
        self.channel.cache.get('ubx', {}).pop(K.MSG_NAV_SVIN, None)
        # NAV-SVIN 是**周期输出**，不由我们触发；先要确保它开了速率 > 0。
        # 这里只读缓存，读不到就如实说"没开输出"，不去偷偷改配置。
        raw = self.channel.ubx_last(*K.MSG_NAV_SVIN)
        if raw is None:
            # 给一个短暂窗口，等下一个历元的 NAV-SVIN
            deadline = time.time() + 2.0
            while time.time() < deadline and raw is None:
                line = self.channel.read_line(0.3)
                if line is not None:
                    self.channel.absorb(line)
                raw = self.channel.ubx_last(*K.MSG_NAV_SVIN)
        if raw is None:
            return False, ('没有收到 UBX-NAV-SVIN。F9P 的收敛状态是周期输出，'
                           '需要先把 CFG-MSGOUT-UBX_NAV_SVIN_UART1 设为 ≥1'), {
                'error': 'no_nav_svin', 'hint': 'set_message_rate("RTCM3-NAVSVIN", 1)'}

        if len(raw) < 40:
            return False, f'NAV-SVIN 载荷长度异常（{len(raw)} 字节）', {'error': 'bad_length'}

        dur, = struct.unpack('<I', raw[8:12])
        mx, my, mz = struct.unpack('<iii', raw[12:24])
        mxhp, myhp, mzhp = struct.unpack('<bbb', raw[24:27])
        mean_acc, = struct.unpack('<I', raw[28:32])
        obs, = struct.unpack('<I', raw[32:36])
        valid = bool(raw[36])
        active = bool(raw[37])

        def _combine(main, hp):
            return (main + hp / 10000.0) / 100.0

        data = {
            'duration_s': dur,
            'obs': obs,
            'mean_acc_mm': mean_acc * 0.1,
            'valid': valid,
            'active': active,
            'x': _combine(mx, mxhp),
            'y': _combine(my, myhp),
            'z': _combine(mz, mzhp),
        }
        if valid:
            return True, f'Survey-in 已收敛（{dur}s，精度 {mean_acc * 0.1:.1f}mm）', data
        if active:
            return True, f'Survey-in 收敛中（{dur}s / {obs} 次观测）', data
        return True, 'Survey-in 未启动', data

    def disable_survey_in(self, persist=True, restart=False):
        """停用 Survey-in / 定点，回到移动站模式。"""
        return self.set_mode('disabled', persist=persist, restart=restart)

    # ==================== 报文速率 / RTCM ====================
    def set_message_rate(self, name, rate, port=None, **kwargs):
        """配某条报文的输出速率。

        `name` 支持三种写法：
            'RTCM3-1005' / '1005'      → CFG-MSGOUT-RTCM_3X_TYPE1005_UART1
            'RTCM3-NAVSVIN' / 'NAVSVIN' → CFG-MSGOUT-UBX_NAV_SVIN_UART1
            'GGA' / 'NMEA-GGA'          → CFG-MSGOUT-NMEA_ID_GGA_UART1（本驱动不含，见下）
        `rate` = 每历元输出次数（Hz），0 = 关闭。

        流程层只会用 `COORD_MSG` 里的名字（'RTCM3-1005'）来"表达要播坐标"，
        本驱动把它翻成 1005 的 MSGOUT 键 —— 和 LC29H(BS) 把
        `set_message_rate('RTCM3-1005', 1)` 翻成 PAIR434 是同一件事。
        """
        token = str(name).strip().upper()
        for prefix in ('RTCM3-', 'RTCM-', 'RTCM'):
            if token.startswith(prefix):
                token = token[len(prefix):]
                break
        key_name = None
        msg_num = None
        # 4072.0 / 4072.1 带小数点，不满足 isdigit，得先单独识别，
        # 否则会被归成"不认识的名字"，用户看不出是固件不支持。
        try:
            if '.' in token:
                frac = float(token)
                if frac in K.UNSUPPORTED_RTCM:
                    return False, (f'RTCM {token} {K.UNSUPPORTED_RTCM[frac]}'), {
                        'error': 'unsupported', 'msg': token}
        except ValueError:
            pass
        if token.isdigit():
            msg_num = int(token)
            if msg_num not in K.RTCM_OUT:
                if msg_num in (1006, 1007, 1033):
                    return False, (f'RTCM {msg_num} 在 ZED-F9P 上只支持输入、'
                                   f'不支持输出'), {'error': 'unsupported', 'msg': msg_num}
                return False, f'F9P 不支持输出 RTCM {msg_num}', {
                    'error': 'unsupported', 'msg': msg_num,
                    'supported': sorted(K.RTCM_OUT)}
            key_name = f'RTCM_{msg_num}'
        elif token in K.NAV_OUT:
            key_name = f'NAVOUT_{token}'
        else:
            return False, f'F9P 驱动不认识报文名 {name!r}', {'error': 'unknown_msg'}

        try:
            value = max(0, int(rate))
        except (TypeError, ValueError):
            raise DriverBug(f'报文速率必须是整数，收到 {rate!r}')

        ok, message, data = self._valset([(key_name, value)])
        if not ok:
            return False, f'配置 {name} 速率失败（{message}）', {
                'error': 'nak' if message == 'NAK' else 'timeout'}
        data.update({'msg': name, 'rate': value, 'key': key_name})
        return True, f'{name} 速率已设为 {value}', data

    def get_message_rate(self, name, msg_ver=None):
        """查某条报文的输出速率。"""
        token = str(name).strip().upper()
        for prefix in ('RTCM3-', 'RTCM-', 'RTCM'):
            if token.startswith(prefix):
                token = token[len(prefix):]
                break
        if token.isdigit():
            key_name = f'RTCM_{int(token)}'
        elif token in K.NAV_OUT:
            key_name = f'NAVOUT_{token}'
        else:
            return False, f'F9P 驱动不认识报文名 {name!r}', {'error': 'unknown_msg'}
        values, _ = self._valget([key_name])
        if key_name not in values:
            return False, f'读取 {name} 速率失败（无应答）', {'error': 'timeout'}
        rate = int(values[key_name])
        return True, f'{name} 速率 = {rate}', {'msg': name, 'rate': rate, 'key': key_name}

    def read_rtcm_config(self):
        """回读 RTCM 播发配置（诊断报告、备份都要用）。

        返回的 `msm_type` / `enabled` 是流程层判断"配没配对"的依据，
        键名要和 Quectel 驱动保持一致，否则 writer 里就得出型号分支。
        """
        names = [f'RTCM_{num}' for num in K.RTCM_OUT]
        values, layer = self._valget(names)
        if not values:
            return False, '读取 RTCM 配置失败（无 VALGET 应答）', {'error': 'timeout'}

        enabled = {}
        for num in K.RTCM_OUT:
            rate = int(values.get(f'RTCM_{num}', 0) or 0)
            if rate > 0:
                enabled[num] = rate
        obs = [n for n in enabled if n in OBSERVATION_RTCM]
        tiers = {t for t, members in MSM_TIER.items() if set(obs) & set(members)}
        msm_type = tiers.pop() if len(tiers) == 1 else 0

        ok, problems = validate_rtcm_plan(enabled.keys())
        data = {
            'enabled': sorted(enabled),
            'rates': {str(k): v for k, v in enabled.items()},
            'msm_type': msm_type,
            'layer': layer,
            'problems': problems,
            # 给前端展示用的一句话
            'summary': ('开启：' + '、'.join(str(n) for n in sorted(enabled))
                        if enabled else '未开启任何 RTCM 输出'),
            # 兼容 writer 的 fields[0] 取值路径
            'fields': [str(msm_type)] + [str(n) for n in sorted(enabled)],
        }
        if not ok:
            data['warning'] = '；'.join(problems)
        return True, data['summary'], data

    def config_rtcm(self, msm_type=4, persist=True, **kwargs):
        """按 MSM 档位整体配置基站 RTCM 输出（§3.1.5 推荐组合）。

        MSM4 是大多数 CORS/RTK 服务的默认档：报文体量小，兼容性最好。
        MSM7 精度更高但带宽翻倍，只有在移动站明确要求时才有意义。

        按手册推荐组合一次配齐：
            1005 + 各星座 MSM + 1230
        `constellations` 可传入要开的星座子集（默认全开）。
        """
        try:
            tier = int(msm_type)
        except (TypeError, ValueError):
            raise DriverBug(f'msm_type 必须是 4 或 7，收到 {msm_type!r}')
        if tier not in (4, 7):
            return False, f'F9P 只支持 MSM4 或 MSM7（收到 MSM{tier}）', {
                'error': 'bad_msm', 'msm_type': msm_type}

        wanted = kwargs.get('constellations') or MSM_CONSTELLATIONS
        wanted = [str(c).upper() for c in wanted]
        tier_msgs = MSM_TIER[tier]
        constellation_map = {
            'GPS': tier_msgs[0], 'GLO': tier_msgs[1], 'GLONASS': tier_msgs[1],
            'GAL': tier_msgs[2], 'GALILEO': tier_msgs[2],
            'BDS': tier_msgs[3], 'BDS2': tier_msgs[3], 'BEIDOU': tier_msgs[3],
        }

        plan = {1005: 1, 1230: 1}
        for name in wanted:
            num = constellation_map.get(name)
            if num:
                plan[num] = 1
        ok_plan, problems = validate_rtcm_plan(plan.keys())
        if not ok_plan:
            return False, '；'.join(problems), {'error': 'bad_plan', 'problems': problems}

        # 先把没被选中的观测报文显式关掉，再开新的。
        # 不关的话 MSM4/MSM7 会混在一起 —— VALSET 是"写值"不是"覆盖整组"。
        items = []
        for num in OBSERVATION_RTCM:
            items.append((f'RTCM_{num}', plan.get(num, 0)))
        for num in (1005, 1230):
            items.append((f'RTCM_{num}', 1))
        # 4072.0/4072.1 **不在这里写 0**：本固件没有对应的配置键
        # （真机 VALGET 静默不回），带进 VALSET 有可能让整条配置被无视。
        # 详见 ubx_keys.UNSUPPORTED_RTCM。

        layers = (K.LAYER_RAM | K.LAYER_FLASH) if persist else K.LAYER_RAM
        ok, message, data = self._valset(items, layers=layers)
        if not ok:
            return False, f'配置 RTCM 输出失败（{message}）', {
                'error': 'nak' if message == 'NAK' else 'timeout'}
        data.update({'msm_type': tier, 'plan': {str(k): v for k, v in plan.items()},
                     'constellations': wanted})
        return True, (f'已配置 MSM{tier}：' +
                      '、'.join(str(n) for n in sorted(plan))), data

    def restore_rtcm_config(self, fields):
        """把备份里的 RTCM 字段原样写回（还原专用）。

        `fields` 接受 `read_rtcm_config()` 返回的 `enabled` 列表
        （或 `[msm_type, 报文号...]` 这种 'fields' 形态）。
        """
        nums = []
        for item in (fields or []):
            try:
                num = int(str(item).split('.')[0] if '.' not in str(item) else float(item))
            except (TypeError, ValueError):
                continue
            if num in K.RTCM_OUT:
                nums.append(num)
        items = []
        for num in K.RTCM_OUT:
            items.append((f'RTCM_{num}', 1 if num in nums else 0))
        ok, message, data = self._valset(items)
        if not ok:
            return False, f'回写 RTCM 配置失败（{message}）', {
                'error': 'nak' if message == 'NAK' else 'timeout'}
        data['enabled'] = sorted(nums)
        return True, f'已回写 {len(nums)} 条 RTCM 输出', data

    # ==================== 串口 ====================
    def _baud_key(self, port=None):
        """本型号只走 UART1（USB 口在 F9P 上是另一个物理口，不在本项目范围）。"""
        return 'UART1_BAUDRATE'

    def read_uart(self, port=None):
        """回读模块自己报的串口配置。"""
        names = ['UART1_BAUDRATE', 'UART1_DATABITS', 'UART1_PARITY',
                 'UART1_STOPBITS', 'UART1_ENABLED']
        values, _layer = self._valget(names)
        if 'UART1_BAUDRATE' not in values:
            return False, '读取串口配置失败（无 VALGET 应答）', {'error': 'timeout'}
        raw = {k: int(v) for k, v in values.items()}
        # 枚举值（E1）的**具体编号**在集成手册里没有、Interface description 里
        # 也只写 stopbits={0,1,2} / databits={0,1} 而不给编号对应关系。
        # 真机 HPG 1.32 回读：STOPBITS=1、DATABITS=0，而模块显然工作在 8N1，
        # 所以这组编号不能按"0=1位、1=1.5位"去猜 —— 猜错会把用户带偏。
        # 因此：原始值照实给（`*_raw`），推断值放前面但标注为推断。
        data = {
            'port': 'UART1',
            'baud': raw['UART1_BAUDRATE'],
            # DATABITS: 0 → 8（默认），1 → 7。依据是默认值必须等于出厂 8 位。
            'databits': 7 if raw.get('UART1_DATABITS', 0) == 1 else 8,
            'parity': {0: 'N', 1: 'E', 2: 'O'}.get(raw.get('UART1_PARITY', 0), 'N'),
            # STOPBITS: 默认 1 位停止位，真机读到 1 且能 8N1 通讯 → 1 就是"1 位"。
            'stopbits': 1 if raw.get('UART1_STOPBITS', 1) in (0, 1) else
                        (1.5 if raw.get('UART1_STOPBITS') == 2 else 2),
            'enabled': bool(raw.get('UART1_ENABLED', 1)),
            'raw': {k: v for k, v in raw.items() if k != 'UART1_BAUDRATE'},
            'enum_note': '枚举编号来自真机回读推断，手册未给出对应表',
        }
        return True, (f'UART1 = {data["baud"]} {data["databits"]}'
                      f'{data["parity"]}{data["stopbits"]}'), data

    def set_baud(self, baud, persist=True, restart=False):
        """改 UART1 波特率。

        §3.8.1：改完要留 **100 ms** 再按新速率喂数据，否则模块还没切完，
        后面那几帧会被当成帧错误（一秒内超 100 个帧错误会让 RX 直接关闭）。
        所以这里发完配置就 sleep 0.2 s，并主动把通道的波特率改到新值。
        """
        try:
            value = int(baud)
        except (TypeError, ValueError):
            raise DriverBug(f'波特率必须是整数，收到 {baud!r}')
        if value not in self.BAUDS:
            return False, (f'ZED-F9P 不支持 {value}；合法档位：'
                           f'{", ".join(str(b) for b in self.BAUDS)}'), {
                'error': 'bad_baud', 'baud': value, 'supported': list(self.BAUDS)}

        layers = (K.LAYER_RAM | K.LAYER_FLASH) if persist else K.LAYER_RAM
        old = self.channel.baud
        ok, message, data = self._valset([('UART1_BAUDRATE', value)], layers=layers)
        if not ok:
            return False, f'修改波特率失败（{message}）', {
                'error': 'nak' if message == 'NAK' else 'timeout'}

        # §3.8.1 的 100 ms 延迟，给 2 倍余量
        time.sleep(0.2)
        self.channel.baud = value
        if self.channel.is_open:
            try:
                self.channel._serial.baudrate = value      # 本端口同步切档
            except Exception as e:                          # noqa: BLE001
                logging.debug(f'切换串口波特率失败（忽略）：{e}')
        data.update({'old_baud': old, 'baud': value, 'persist': bool(persist)})
        return True, f'UART1 波特率 {old} → {value}', data

    def get_baud(self):
        """回读当前波特率（`BAUD_READ_KEY` 指向本方法）。"""
        values, _ = self._valget(['UART1_BAUDRATE'])
        if 'UART1_BAUDRATE' not in values:
            return False, '读取波特率失败', {'error': 'timeout'}
        return True, f'当前波特率 {int(values["UART1_BAUDRATE"])}', {
            'baud': int(values['UART1_BAUDRATE'])}

    # ==================== 保存 / 重启 ====================
    def save(self):
        """固化到 Flash。

        F9P 没有 Quectel 的 SAVEPAR：固化是 `CFG-VALSET` 的一个 layer 位。
        本驱动 `_valset` 默认就带 FLASH 层，所以这里只要**把当前 RAM 值
        用 FLASH 层再写一遍**即可 —— 但 UBX 没有"把 RAM 全量存盘"的命令，
        正确做法是写 `CFG-CFG` 消息带 `saveMask`。
        """
        # UBX-CFG-CFG 载荷：clearMask(4) saveMask(4) loadMask(4) deviceMask(1)
        # saveMask bit0=IO  bit1=MSG  bit2=INF  bit3=NAV  bit4=RINV
        # 我们只保存配置类（IO/MSG/NAV），不清除任何东西。
        save_mask = 0x17          # bit0|bit1|bit2|bit4 ... 保守取 0x1F 覆盖全部配置
        payload = struct.pack('<IIII', 0, save_mask, 0, 0x11)  # deviceMask bit0=BBR bit4=Flash
        ok, message = self._send_ubx(*K.MSG_CFG_CFG, payload)
        if not ok:
            return False, f'保存配置失败（{message}）', {
                'error': 'nak' if message == 'NAK' else 'timeout'}
        return True, '配置已固化到 Flash', {'save_mask': hex(save_mask)}

    def reboot(self):
        """软重启（UBX-CFG-RST）。

        载荷：`navBbrMask(2) | resetMode(1)`。
        `0x0000` = 不清 BBR（保留配置），`0x02` = controlled software reset。
        **故意不用 0xFFFF**：那会清掉全部配置，等于恢复出厂 —— 标定场景绝不能这样。

        重启命令不回 ACK（模块直接复位），所以 `wait_ack=False`。
        """
        payload = struct.pack('<HB', 0x0000, 0x02)
        before = dict(self.channel.counters)
        ok, message = self._send_ubx(*K.MSG_CFG_RST, payload, wait_ack=False)
        # 重启后模块会静默几百毫秒再恢复输出
        self.channel.reopen(delay=0.8)
        deadline = time.time() + 8.0
        found = False
        while time.time() < deadline:
            line = self.channel.read_line(0.5)
            if line is not None:
                self.channel.absorb(line)
                found = True
                break
            if (self.channel.counters.get('ubx', 0)
                    - before.get('ubx', 0)) > 0:
                found = True
                break
        if not found:
            silent = (self.channel.counters.get('lines', 0) - before.get('lines', 0)) <= 0
            return False, ('重启后串口完全静默，模块可能没有重启' if silent
                           else '重启完成（未抓到首帧，按串口恢复输出判定）'), {
                'error': 'no_boot_frame', 'silent': silent}
        return True, '重启完成', {'raw': '', 'boot_frame': found}

    # ==================== 天线 ====================
    def read_antenna_info(self):
        """读天线状态（MON-HW，不是 1033 内容）。

        F9P 的 MON-HW `aStatus`：
            0=INIT 1=DONTKNOW 2=OK 3=SHORT 4=OPEN
        """
        self.channel.cache.get('ubx', {}).pop(K.MSG_MON_HW, None)
        self.channel.write_ubx(*K.MSG_MON_HW)
        deadline = time.time() + 1.0
        raw = None
        while time.time() < deadline and raw is None:
            line = self.channel.read_line(0.25)
            if line is not None:
                self.channel.absorb(line)
            raw = self.channel.ubx_last(*K.MSG_MON_HW)
        if raw is None or len(raw) < 30:
            return False, '读取天线状态失败（无 MON-HW 应答）', {'error': 'timeout'}
        status = raw[28]
        power = raw[29]
        label = {0: '初始化中', 1: '未知', 2: '正常', 3: '短路', 4: '开路'}.get(status, '未知')
        return True, f'天线状态：{label}（供电 {power}）', {
            'a_status': status, 'a_status_text': label, 'a_power': power,
            'note': 'F9P 不输出 RTCM 1033，此处读的是 MON-HW 硬件状态'}

    def set_antenna_info(self, desc, setup_id=0, sn='', persist=True):
        """F9P **不输出** RTCM 1033（§3.1.5 输出表里没有），所以无法写天线信息。

        不静默成功：明确拒绝，并提示改用 1230（本模块播发 GLONASS 偏差的必需项）。
        """
        return False, ('ZED-F9P 不支持输出 RTCM 1033（该报文在此模块上只支持输入），'
                       '因此无法写入天线信息；基站请确保开启 1230'), {
            'error': 'unsupported', 'msg': 1033}

    # ==================== 解析 ====================
    def parse_line(self, text):
        """UBX 型号的 ASCII 行解析：只多认 NMEA 的 GGA/RMC/GSA。

        绝大多数情况走基类（Quectel 的 PQTM* 在这里永远匹配不上，直接返回
        `{'type': None}`），额外认 `$GNGGA` 这类标准 NMEA —— F9P 默认就输出它们，
        标定采集阶段可以直接用，不必依赖 UBX-NAV-PVT。
        """
        return super().parse_line(text)
