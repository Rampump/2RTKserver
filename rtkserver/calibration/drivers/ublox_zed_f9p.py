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

但 1006/1007 与 1033 的"不支持"性质**不同**，报错文案必须分开（已按此实现）：
    · 1006/1007 —— 配置键存在（`RTCM_INPUT_ONLY`），能 VALGET 到 0，
                   只是模块不输出 → 文案"只支持输入"
    · 1033      —— **配置键根本不存在**（pyubx2 的 1343 个键里没有
                   `CFG_MSGOUT_RTCM_3X_TYPE1033_*` 任何一个）→ 写它必 NAK，
                   文案要直接指向"必须改用 1230"（`RTCM_NO_KEY`）

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

## CFG-CFG 载荷是 13 字节，最后 1 字节是 deviceMask

    clearMask(4) | saveMask(4) | loadMask(4) | deviceMask(**1**)

**最后那个字段只有 1 字节**，写成 `<IIII` 会多送 3 个 `0x00`，模块按错位
解析后直接 NAK。这个坑在 `save()` 里潜伏过很久（表现是"配置写了能用，
重启就丢"，极易被归因成 Flash 坏了或固件问题）。现在统一用 `<IIIB`，
`deviceMask` 的位也和另外三个掩码**不是同一套**：bit0=BBR bit1=Flash
bit2=EEPROM（`K.CFG_CFG_DEV_*`）。

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
    'msg_rate': True,       # CFG-MSGOUT-* 逐条可配（NAV/NMEA/RXM/RTCM 四类）
    'rtcm_out': True,       # CFG-MSGOUT-RTCM_3X_*
    'err3': False,          # UBX 用 ACK-NAK 表达拒绝，没有"错误码 3"这套
    'valdel': True,         # CFG-VALDEL 删键（只能作用 BBR/Flash，不能作用 RAM）
    'factory_reset': True,  # CFG-CFG clear+load，注意会把波特率打回 38400
    'persist_check': True,  # 能 poll Flash 层回读，证明配置真的落盘（不是只读 RAM）
    # 切到基准站后**不会自动停掉**多余的 NAV / NMEA 输出，必须显式逐条写 0。
    # Quectel（LG290P / LC29H(BS)）切 base 就自动改播 RTCM，没有这个需求，
    # 所以它们不置这个 cap —— 流程层只按 cap 走，不按型号分支。
    'silence': True,
    # 能对 NAV / NMEA / RXM 那组 MSGOUT 键做"整组快照 / 整组开启 / 整组还原"。
    # `silence` 只会关；没有这个 cap，关掉的输出就**无从放回** ——
    # 流程层据此决定"静音前要不要先快照"（见 writer._step_setup_messages）。
    'output_snapshot': True,
    # **没定位就不发 NMEA**。真机实测（2026-11，COM12，无天线）：
    #   NMEAMOUT_GGA_USB/I2C/UART1/UART2/SPI 全部 = 1、RATE_MEAS=1000（1 Hz），
    #   6 秒原始字节里 **0 个 `$GNGGA`**，而同期 NAV-PVT 稳定 1 Hz
    #   （fixType=0 gnssFixOk=0 numSV=0，时间停在 2026-07 未同步）。
    # 原因是 NAV-PVT 内容合法、GGA 的经纬度/质量位/星数字段在 fixType=0 时
    # 无值可填，u-blox 干脆整句不出。Quectel 系在无定位时仍吐 GGA（字段留空），
    # 所以它们不置这个 cap —— 流程层只按 cap 换解释文案，判定逻辑不变。
    'nmea_needs_fix': True,
    # **没有定位时精度字段是垃圾值，不是"信号差"**。真机实测（无天线，
    # fixType=0）：NAV-SVIN 的 meanAcc 原始值 0xE8D4A510（=4e9×0.1mm），
    # 折算出来是 94868.3264 m —— 这是个毫无物理意义的溢出数。流程层如果
    # 原样打印，现场只会看到「当前 3D 精度 94868.3264 m」，把它当成
    # "天线没接好、精度很差"，而真相是模块压根没在解算（obs 恒为 0）。
    # Quectel 的 $PQTMSVINSTATUS 在未定位时给的是 0 或小值，不需要这个 cap。
    'svin_acc_meaningless_when_no_fix': True,
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

# 本型号能逐星座配速率的星座（流程层按这张表逐条下发）。
#
# 这张表描述的是**"这块板子能同时播哪几个星座"**，不是报文名。
# 流程层 `_step_setup_messages` 会把它逐条喂给 `set_message_rate()`
# 当成"再确认一遍"，所以每条都必须能被 `_resolve_msg_key()` 认下来 ——
# F9P 这边用具体号（1074 等），X 占位写法是 Quectel 的口径，F9P 不认。
# 用 MSM4 的具体号：`config_rtcm()` 默认就配 MSM4，两者一致；
# 若流程要 MSM7，`config_rtcm()` 会把 1077 等一并打开，这里的补确认不影响结果
# （补的是 1074=1，本来就是 1，幂等）。
# **只列本固件真有键的四个星座。** QZSS（111X）在 ZED-F9P 的 MSGOUT 表里没有
# 输出键 —— 真机 VALGET 直接静默不回。列进来只会换来一条"不支持"的假警告，
# 让人以为漏配了一个星座。MSM_TIER 表里也没有 111x，两处保持一致。
MSM_CONSTELLATIONS = ('RTCM3-1074',   # GPS      MSM4
                      'RTCM3-1084',   # GLONASS  MSM4
                      'RTCM3-1094',   # Galileo  MSM4
                      'RTCM3-1124')   # BDS      MSM4

# 输出观测报文时必须带上的 1230（否则 GLO 只能浮点解）
REQUIRED_RTCM = (1005, 1230)

# 视作"观测报文"的 RTCM 号（1074~1127 全段，MSM4/MSM7 都在内）
OBSERVATION_RTCM = (1074, 1077, 1084, 1087, 1094, 1097, 1124, 1127)
MSM_TIER = {4: (1074, 1084, 1094, 1124), 7: (1077, 1087, 1097, 1127)}

# 持久化自检的默认键集（`verify_persisted()` 不传参时用它）。
# 只挑"标定流程每次都会写、且值可预期"的键：
#   · TMODE_MODE    —— 模式，写错的后果最严重（基站当流动站、反之亦然）
#   · RTCM_1005     —— 基准站必开（坐标系基准）
#   · RTCM_1230     —— 基准站必开（GLONASS 偏差，不开只能浮点）
# 注意用 `RTCM_1005` 这种**键表里的名字**，不是 `MSGOUT_RTCM_3X_TYPE1005_UART1`
# （那是 pyubx2 的叫法，本项目的 `ubx_keys.ALL_KEYS` 里只有前者）。
# 不选 MSM 观测报文：档位（MSM4/7）会随方案变，写死会让自检假报不符。
PERSIST_CHECK_KEYS = (
    'TMODE_MODE',
    'RTCM_1005',
    'RTCM_1230',
)

# ==================== 端口维度（MSGOUT 键按口拆分） ====================
# `CFG-MSGOUT-*` 每条报文都有 5 个端口各一份键，**后缀不同、ID 不同**：
#     I2C → UART1 → UART2 → USB → SPI   （ID 逐个 +1）
# 写错端口的键模块**照样 ACK**，但那个物理口上的输出纹丝不动 ——
# 这是本项目在 F9P 上踩过的最贵的一个坑（真机排查记录见 ubx_keys 头部注释）：
# COM12 是 USB CDC（`VID:PID=1546:01A9`），驱动却一律写 `_UART1` 键，
# 于是"关掉多余输出"这个动作在 USB 口上完全无效，模块继续吐 NAV-SAT/NAV-SIG。
#
# 所以端口不能写死，必须按**实际链路**选：`detect_port()` 探出来，
# 探不到再按端口名猜（COM / /dev/ttyACM 都是 USB CDC）。
# 这里只用来**校验端口名合法**；后缀一律是 `_<端口名>`，由 `_port_key()` 拼。
# 端口全集以**键表**为准（`K.OUTPUT_PORTS` 是 `OUTPUT_FANOUT` 里真实出现过的
# 后缀去重），不在这里再抄一份 —— 两处各写一份的话，将来固件多一个口，
# 驱动会先把"合法的口"判成非法并 `raise DriverBug`，整条静音/快照链路直接废掉。
PORTS = tuple(K.OUTPUT_PORTS) or ('I2C', 'UART1', 'UART2', 'USB', 'SPI')
PORT_SUFFIX = {p: f'_{p}' for p in PORTS}
# 每类输出在整个键表里的实际键名，以及"裸名 → 键名"的映射前缀。
# `(裸名所在组, 键名前缀, 规范名去前缀)`，三组共用一套逻辑。
OUTPUT_GROUPS = (
    ('NAV', 'NAVOUT_', 'NAV_'),
    ('NMEA', 'NMEAMOUT_', ''),
    ('RXM', 'RXMOUT_', 'RXM_'),
)

# ACK 等待上限。F9P 处理一条 VALSET 是毫秒级（实测 <50 ms），
# 但刚开机时要等 Flash 读完，给 1.5 s 足够宽。
ACK_TIMEOUT = 1.5
# 读 VALGET 应答的上限：一条 VALGET 最多回 1~2 KB，38400 下约 0.5 s，
# 但若同时有 RTCM 流在抢带宽，得留余量。
VALGET_TIMEOUT = 2.0
# 一次 VALGET 最多问几个键。
#
# 真机教训（ZED-F9P，EXT CORE 1.00）：**只要请求里有哪怕一个本固件不认识的键，
# 模块就整条请求静默丢弃** —— 不回应答、也不回 NAK，2 秒超时后我们得到空结果，
# 看起来像"链路坏了"。而 F9P 的 HPG 1.00 固件并不带全 pyubx2 键表里的全部
# CFG-MSGOUT 项（实测 `NAVOUT_NAV_AOPSTATUS_USB`、`NAVOUT_NAV_ATT_USB`
# 单独问、原样发帧也是零应答；这两条是 ADR / 姿态用的，单频板没有）。
#
# 所以读多个键必须**小批量 + 失败降级**：一批不通就拆开逐个问，
# 把"这个固件没有"的键挑出来单独标注 —— 而不是让整批一起超时。
VALGET_BATCH = 12

# 查询类报文的等待上限。
#
# 与 `ACK_TIMEOUT` 分开写、且**必须分开**：ACK_TIMEOUT 等的是 8 字节的
# `05 01`，查询等的是带载荷的整条报文（MON-VER 228 字节、MON-HW 68 字节）。
# 38400 下 228 字节 ≈ 60 ms，但 USB CDC 有主机侧缓冲、模块也可能正在吐
# NAV-* 流，实测首帧有时要 300 ms 才到；1.5 s 留了 5 倍余量。
#
# 为什么不能省成"复用 ACK_TIMEOUT"：ACK 超时要短（配置键多，一条一条等，
# 5 秒一条会让一批 10 键的配置拖到一分钟），查询却必须长。两个语义
# 用一个常量，就得在两边都不合适。
MON_VER_TIMEOUT = 1.5
MON_HW_TIMEOUT = 1.5

# 配置类报文（模块回 ACK/NAK，**不回内容**）。只用于 `_poll_ubx()` 的误用拦截，
# 不参与任何收发 —— 见 `_poll_ubx` 的注释。
_CONFIG_CLASS_MESSAGES = frozenset({
    (0x06, 0x8A),   # CFG-VALSET
    (0x06, 0x8C),   # CFG-VALDEL
    (0x06, 0x09),   # CFG-CFG
    (0x06, 0x04),   # CFG-RST
    (0x06, 0x00),   # CFG-PRT
    (0x06, 0x31),   # CFG-TP5
})


def windows_bitlen(baud):
    """这个波特率下，一个 Windows 读超时窗口最多装多少个字节（近似）。

    来源是 pyubx2 `examples/ubxsetrates.py` 那条看似多余的 `sleep(1)`：
    它逐条下发 CFG-MSG 并每条等 1 秒。作者的解释是"等 ACK"，但真机上的
    实际作用是**给串口留出收发窗口**——38400 下一个 1 s 窗口只装得下
    约 3.8 KB，而 CFG-MSG 每条 10 字节、应答 10 字节。

    ## 为什么本项目需要这个数

    `Channel.read_line(timeout)` 的超时语义是"等这么久没有新字节就返回"，
    所以**只要模块一直在吐数据（RTCM/NMEA 流），单次读就不会超时**。
    也就是说，`VALGET_TIMEOUT = 2.0` 在满负荷 RTCM 输出下根本不会到期，
    读循环会一直转——它统计的是"有没有数据"，不是"有没有 VALGET 应答"。

    真正把循环从"空转"里拉出来的是 `ubx_last()` 查缓存那一句；
    而 `windows_bitlen` 给的是一个**上界参考**：如果要在 timeout 窗口里
    收完 n 字节，窗口至少要 `n * 10 / baud` 秒（10 bit/字节，8N1）。

    本驱动**不**用这个值去调超时（那样会随流量抖动），只
    （a）在 `set_message_rate` 之类批量下发后按它算"多久之内不该再发下一帧"，
    （b）在诊断日志里解释"为什么 38400 + RTCM 满负荷时 VALGET 容易读不到"。
    与 `set_baud` 的 100 ms 抖动要求是同一类"物理层时间预算"，
    但那个是手册硬指标（§3.8.1），这个是纯算术近似。
    """
    try:
        baud = int(baud)
    except (TypeError, ValueError):
        return 0
    if baud <= 0:
        return 0
    # 10 bit/字节（1 起始 + 8 数据 + 1 停止）
    return baud // 10


def pack_value(type_info, value):
    """按键的类型打包一个值。返回 bytes。

    `type_info` 是 `(name, size, signed)` 三元组。调用方负责缩放换算，
    这里只收**已经换算好的数值**。

    浮点键（`R4`/`R8`）的 name 首字母是 `R`，值原样按 IEEE754 打包，
    **不做 int(round())** —— 那会把 0.5 Hz 之类的值抹成 0。
    """
    name, size, signed = type_info
    if name[:1] == 'R':
        fmt = {4: '<f', 8: '<d'}.get(size)
        if fmt is None:
            raise DriverBug(f'不支持的浮点键长：{size}')
        return struct.pack(fmt, float(value))
    value = int(round(value))
    if size == 1:
        if not (-128 <= value <= 127 if signed else 0 <= value <= 255):
            raise DriverBug(f'单字节键值越界：{value}（{"有" if signed else "无"}符号）')
        return struct.pack('<b' if signed else '<B', value)
    if size == 2:
        return struct.pack('<h' if signed else '<H', value)
    if size == 4:
        return struct.pack('<i' if signed else '<I', value)
    if size == 8:
        # 8 字节整数键（X8）只可能是无符号，有符号的 I8 在 F9P 键表里不存在
        return struct.pack('<q' if signed else '<Q', value)
    raise DriverBug(f'不支持的键长：{size}')


def unpack_value(type_info, raw):
    """从字节里解一个值（原始数值，未缩放）。"""
    name, size, signed = type_info
    if len(raw) < size:
        raise DriverBug(f'值长度不足：要 {size} 字节，只有 {len(raw)}')
    if name[:1] == 'R':
        fmt = {4: '<f', 8: '<d'}.get(size)
        if fmt is None:
            raise DriverBug(f'不支持的浮点键长：{size}')
        return struct.unpack(fmt, raw[:size])[0]
    if size == 1:
        return struct.unpack('<b' if signed else '<B', raw[:1])[0]
    if size == 2:
        return struct.unpack('<h' if signed else '<H', raw[:2])[0]
    if size == 4:
        return struct.unpack('<i' if signed else '<I', raw[:4])[0]
    if size == 8:
        return struct.unpack('<q' if signed else '<Q', raw[:8])[0]
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
    # 双模：TMODE_MODE 0=disabled（流动站）/ 2=fixed（基准站），两边都能跑。
    # 4 种标定方式全支持。
    ROLE = 'dual'
    DUAL_MODE = True
    CAPS = CAPS
    # UBX 没有文本命令模板：`CMD` 留空，所有下发走 `_valset` / `_valget`。
    # 留空而不是删掉，是为了让误用 `body()` 时立刻抛 DriverBug 而不是 AttributeError。
    CMD = {}
    CS_EXPECT = {}
    MSM_CONSTELLATIONS = MSM_CONSTELLATIONS
    # 持久化自检键集（模块级常量同名，这里挂到类上）。
    # 不加这行的话 `read_persisted()` / `verify_persisted()` 里的
    # `self.PERSIST_CHECK_KEYS` 直接 AttributeError —— 而 CAPS 里
    # `persist_check=True` 会让流程层在每次标定收尾时调用它，
    # 真机表现就是"配置都写成功了，最后一步自检崩掉，整单标定判失败"。
    PERSIST_CHECK_KEYS = PERSIST_CHECK_KEYS

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

    def _valset(self, items, layers=None, transaction=0):
        """写一组配置。`items` = [(key_name, 物理值), ...]。

        **一次一条 VALSET，等一个 ACK**（§3.1.1）：一条里塞多个 key 是合法的，
        多个 key 共享同一个 ACK；但两条 VALSET 之间必须等上一条的 ACK。

        `layers` 默认写全部三层（RAM|BBR|Flash = 0x07），与 pyubx2 官方
        `f9p_basestation.py` 的写法一致：只写 RAM 掉电就丢，只写 Flash 要重启
        才生效，三层全写才是"立刻生效、断电不丢、断电前也留在后备 RAM"。

        **单条 key 数上限 64**（`K.MAX_KEYS_PER_VALSET`，pyubx2 里同样卡这个数）。
        超了模块直接 NAK，且 NAK 不告诉你是哪个键，所以在这里先拦。

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

        ## `transaction` 会影响 `version` 字节

        pyubx2 的 `UBXMessage.config_set()` 写的是
        `version = 0 if transaction == 0 else 1`。也就是说：
            transaction = 0 → version 0（单条写，每条独立生效）
            transaction ≠ 0 → version 1（多条同号事务，模块一次提交）
        默认 0 与现有全部调用方一致；保留参数是为了在"一组键必须同时生效"
        的场景（比如模式 + 坐标必须一起切，不能中间出现 base 却没坐标）里
        能发同一个事务号，而不是靠调用方自己拼帧。
        """
        if layers is None:
            layers = K.LAYER_ALL
        transaction = int(transaction or 0) & 0xFF
        if not items:
            return True, '无需写入', {'keys': 0}
        if len(items) > K.MAX_KEYS_PER_VALSET:
            raise DriverBug(
                f'一条 VALSET 最多 {K.MAX_KEYS_PER_VALSET} 个键，收到 {len(items)} 个；'
                f'请拆成多条分批下发')

        # version: 0=单条写  1=带事务号（pyubx2 config_set 的同一规则）
        version = 0x00 if transaction == 0 else 0x01
        payload = bytearray([version, layers & 0xFF, transaction, 0x00])
        written = []
        for name, value in items:
            rec = K.lookup(name)
            if rec is None:
                raise DriverBug(f'配置键 {name!r} 不在键表里（ubx_keys.py）')
            key_id, type_info, scale, _note = rec
            # 缩放：调用方给物理值（cm / mm / deg），这里换算成原始值。
            # 浮点键（R4/R8）不取整，保留小数；整数键必须 round 成 int，
            # 否则 pack_value 的 int(round()) 会把 1e-7 度之类的残差抹掉。
            scaled = float(value) / scale if scale != 1.0 else float(value)
            raw_value = scaled if type_info[0][:1] == 'R' else int(round(scaled))
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

    def _valget(self, key_names, layer=None):
        """读一组配置，返回 `({name: 物理值}, layer_flags)`。

        VALGET 请求：`version(1) | layer(1) | position(2) | key(4)...`
        VALGET 应答：`version(1) | layer(1) | position(2) | (key(4) + value(N))...`
        `position=0` 表示从第一个 key 开始；一小批一次拿全。

        ## `layer` 的取值是 **poll 口径**，和 VALSET 不是一套

        pyubx2 `ubxconfigdb.py` 顶部那行注释（"SET and DEL messages use
        different memory layer values to POLL"）说的就是这里：

            poll（本方法）：0=RAM  1=BBR  2=Flash  7=Default
            set （`_valset`）：1=RAM  2=BBR  4=Flash

        两套都叫 1，含义却从 RAM 变成 BBR —— 拿 VALSET 的常量去 poll，
        读 BBR 会读成 RAM，读 Flash 会读成 BBR，**读回来的值看着正常但根本不是那一层**。
        所以本方法默认 `K.POLL_LAYER_RAM`，传入 VALSET 的 layer 常量会被明确拒绝。

        `layer=None` 走 RAM 层（当前运行值）。要确认"重启后还在不在"，
        必须显式 poll BBR 或 Flash —— `save()` 之后读 RAM 永远是对的，
        只有读回持久层才能证明真的落盘了（`ubxconfigdb.py` 的六步演示就是这个流程）。
        """
        if layer is None:
            layer = K.POLL_LAYER_RAM
        if layer not in (K.POLL_LAYER_RAM, K.POLL_LAYER_BBR, K.POLL_LAYER_FLASH,
                         K.POLL_LAYER_DEFAULT):
            raise DriverBug(
                f'CFG-VALGET 的 layer 是 poll 口径（0=RAM 1=BBR 2=Flash 7=Default），'
                f'收到 {layer:#04x}；'
                f'VALSET 的 4=Flash 在这里是非法值 —— 拿 set 常量去 poll 会把 '
                f'Flash 读成"默认值"、BBR 读成 RAM，读回来的数看着正常却不是那一层')
        names = [n for n in key_names if K.lookup(n) is not None]
        if not names:
            return {}, 0
        values, layer, _missing = self._valget_batched(names, layer)
        return values, layer

    def _valget_batched(self, names, layer, batch=VALGET_BATCH):
        """小批量问 + 失败降级，返回 `({name: value}, layer, [本固件没有的键])`。

        为什么要降级（真机教训）：一条 VALGET 里只要含一个本固件不认识的键，
        模块就**整条静默丢弃**（不回应答也不回 NAK）。原先一次性问 69 个
        MSGOUT 键，2 秒后拿到空结果，`silence_outputs()` 报"读取当前输出速率失败"，
        现象和"串口断了"一模一样，查了很久才发现是两个未实现的键在拖累整批。

        策略：
            一批正常 → 收下；
            一批没应答 → 拆成单个逐个问，谁不应答谁就是"这个固件没有的键"，
                        记账后跳过，不影响其余键。
        代价是最坏情况多花几次超时，但静音只在标定开头跑一次，值得。
        """
        values, got_layer, missing = {}, layer, []
        for start in range(0, len(names), max(1, int(batch))):
            chunk = names[start:start + max(1, int(batch))]
            got, sub_layer = self._valget_once(chunk, layer)
            if got:
                values.update(got)
                got_layer = sub_layer or got_layer
                continue
            # 整批被静默丢弃 → 逐个归因
            for name in chunk:
                one, sub_layer = self._valget_once([name], layer)
                if one:
                    values.update(one)
                    got_layer = sub_layer or got_layer
                else:
                    missing.append(name)
        return values, got_layer, missing

    def _valget_once(self, names, layer):
        """一条 VALGET 请求/应答。没应答返回 `({}, 0)`，**不抛**。"""
        payload = bytearray([0x00, layer & 0xFF, 0x00, 0x00])
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

        got_layer = raw[1]
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
        return values, got_layer

    def _valdel(self, key_names, layers=None):
        """删掉一组配置键（UBX-CFG-VALDEL，0x06/0x8C）。

        载荷与 VALSET **同构，但只有 key 没有 value**：
            `version(1) | layer(1) | transaction(1) | reserved0(1) | key(4) ...`

        用途：把某个键从 Flash/BBR 里删掉，让它回落到默认值或上一层的值。
        **不能作用于 RAM 层** —— 删 RAM 没有语义（RAM 本来就是易失的），
        模块对 `layer=RAM` 的 VALDEL 直接 NAK。所以这里默认
        `BBR|Flash`，并且显式拒绝只带 RAM 的调用。
        """
        if layers is None:
            layers = K.LAYER_BBR | K.LAYER_FLASH
        if layers & K.LAYER_RAM and not (layers & ~K.LAYER_RAM):
            raise DriverBug('CFG-VALDEL 不能只作用于 RAM 层（模块会 NAK）；'
                            '请改用 LAYER_BBR | LAYER_FLASH')
        names = [n for n in key_names if K.lookup(n) is not None]
        if not names:
            return True, '无需删除', {'keys': 0}
        if len(names) > K.MAX_KEYS_PER_VALSET:
            raise DriverBug(
                f'一条 VALDEL 最多 {K.MAX_KEYS_PER_VALSET} 个键，收到 {len(names)} 个')

        payload = bytearray([0x00, layers & 0xFF, 0x00, 0x00])
        deleted = []
        for name in names:
            key_id = K.lookup(name)[0]
            payload += struct.pack('<I', key_id)
            deleted.append((name, key_id))
        ok, message = self._send_ubx(*K.MSG_CFG_VALDEL, bytes(payload))
        data = {'keys': len(deleted), 'layers': layers,
                'items': [{'key': n, 'key_id': f'0x{i:08X}'} for n, i in deleted]}
        if not ok:
            data['error'] = 'nak' if message == 'NAK' else 'timeout'
        return ok, message, data

    # ==================== 识别 ====================
    def detect(self):
        """发 UBX-MON-VER 认型。

        MON-VER 载荷 = 30 字节 swVersion + 10 字节 hwVersion + 重复的
        (30 字节 extension) 串。ZED-F9P 的 extension 里有 `PROTVER=` / `MOD=`，
        固件串含 `HPG`。**不能只认 `ZED-F9P` 字面量**：有的固件不把它写进
        swVersion，只在 extension 里带 `F9P`。

        ## 必须走"查询语义"，不能走"配置语义"（本驱动最贵的一条教训）

        UBX 报文分两类，**应答方式完全不同**：

            配置类（CFG-VALSET / CFG-VALDEL / CFG-RST …）
                → 模块回 `UBX-ACK-ACK`(05 01) 或 `UBX-ACK-NAK`(05 00)，**不带内容**
                  → 用 `_send_ubx()`，判 ACK 即可

            查询类（MON-VER / MON-HW / MON-COMMS / CFG-VALGET …）
                → 模块**不回 ACK**，只回一条带载荷的同名报文（0A 04 等）
                  → **必须**用 `_poll_ubx()`，等的是载荷本身

        旧实现这里调的是 `_send_ubx(*K.MSG_MON_VER, b'', retries=3)` ——
        那是个等 `05 01` 的函数。MON-VER 永远不会回 `05 01`，于是：
        每轮白等到超时（1.5 s），重试 3 次共 **4.5 s + 0.15×3 ≈ 5 s**，
        返回 `'timeout'`；而**模块其实早就把 MON-VER 答完了**，只是被这个
        函数当成"无关帧"丢在缓存里没人看。失败文案于是写着
        "未收到 UBX-MON-VER 应答"，把上层彻底带偏：

          · 扫描器（裸读字节）说"有 MON-VER"
          · 驱动说"没收到"
          · 两个组件对同一块板、同一帧给出相反结论

        现场还会被这句话误导去查硬件（流控、TX/RX 接反、固件升级模式），
        而真正的错因是"用错了等应答的方式"。`detect_port()` 一直是对的
        （它走 `_poll_ubx(MON-COMMS)`），这也解释了为什么同一驱动里
        有的查询通、有的查询不通 —— 不是硬件，是两条路径写法不一致。

        修法：改成 `_poll_ubx()`，超时给足（查询 + USB CDC 缓冲，1.5 s 够），
        且**不再重试补发** —— 查询是幂等的，重发只会让模块多答一遍、
        白白把 5 秒拉长到 15 秒。
        """
        self.channel.drain()
        raw = self._poll_ubx(K.MSG_MON_VER, timeout=MON_VER_TIMEOUT)
        if raw is None:
            return False, (
                '未收到 UBX-MON-VER 应答：模块没有对查询回内容。'
                '已确认帧格式与 CRC 正确，请检查 TX/RX 是否接反、'
                '是否处于命令静默（CFG-PRT inProtoMask 关了 UBX）、'
                '或该口被别的程序占用'), {'error': 'no_reply'}

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

        # MON-HW 里有唯一 ID 与天线状态（F9P 的 MON-HW 载荷 60 字节）。
        # 同样是**查询类**报文（不回 ACK、只回内容），必须走 `_poll_ubx`，
        # 不能手写"写一帧 + 读循环"—— 那种写法每个调用方一份，
        # 迟早有一份忘了清缓存或忘了喂 `absorb()`，症状就是"偶发读不到"。
        hw_info = {}
        raw = self._poll_ubx(K.MSG_MON_HW, timeout=MON_HW_TIMEOUT)
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
        """读工作模式，返回 `(ok, message, data)`，与其它型号驱动同一契约。

        ## 曾经返回裸字符串 —— 真机上必然 AttributeError

        本方法早先 `return 'base'`，而流程层（`service.py`）写的是
        `ok, message, mode = driver.get_mode()` 之后 `mode.get('mode')`。
        裸字符串没有 `.get()`，只要走到"读模块信息"这一步就崩，
        而且崩在 `ok` 已经是真值之后，报出来是 AttributeError 而不是
        "读取工作模式失败"，很难往驱动层想。契约测试当时只校验了基类默认
        实现的三元组，没校验各具体驱动的返回结构，所以漏了。

        ## 三个状态要分清

        F9P 没有独立的"模式"寄存器，只有一个 `TMODE_MODE`：
            0 = 流动站（禁用 TMODE）
            1 = Survey-in（基准站，自收敛）
            2 = 定点   （基准站，用固定坐标）
        `mode` 字段给流程层认的 `rover`/`base` 两级口径（基类文档约定），
        `mode_code` 给原始枚举，`svin_mode` 给三级口径 —— 不把 1 和 2
        一律压成 `base`，否则"当前是 Survey-in 还是定点"这个信息就丢了，
        前端也就没法显示"基准站（Survey-in 收敛中）"。

        ## `mode_code` 必须是跨型号统一口径，不能直接透传 TMODE

        Quectel 全系的 `mode_code` 是**接收机角色**（1=流动站，2=基站）；
        F9P 的 `TMODE_MODE` 是**TMODE 状态**（0=关，1=Survey-in，2=定点）。
        两套枚举的 `2` 碰巧都是基站，`1` 却完全相反 —— 直接透传就会出两个错：
            · `backup.usable()` 判 `code in (1, 2)`，F9P 处于流动站时报 0，
              备份被判"工作模式不可识别，无法还原"；
            · F9P 处于 Survey-in 时报 1，`writer` 拿 `mode_code` 当 rover 用，
              一块已经配成基站的模块会被当成流动站去连 CORS。
        所以对外只给 1/2 两级角色，原始 TMODE 值放 `tmode_code`
        （前端要显示"Survey-in 收敛中"读 `svin_mode` / `tmode_code` 即可）。
        """
        values, _layer = self._valget(['TMODE_MODE'])
        if 'TMODE_MODE' not in values:
            return False, '读取工作模式失败（无 VALGET 应答）', {
                'mode': 'unknown', 'mode_code': None, 'error': 'timeout'}

        code = int(values['TMODE_MODE'])
        label = MODE_LABEL.get(code, 'unknown')
        is_base = code in (MODE_SURVEY_IN, MODE_FIXED)
        data = {
            'mode': 'base' if is_base else 'rover',
            # 跨型号角色码：1=流动站 2=基站（与 Quectel 对齐，backup/前端都按这个认）
            'mode_code': 2 if is_base else 1,
            # 模块原始 TMODE 枚举：0/1/2，诊断与"收敛中/已定点"区分靠它
            'tmode_code': code,
            # 三级口径：disabled / survey_in / fixed / unknown
            'svin_mode': label,
            'source': 'TMODE_MODE',
        }
        return True, f'当前模式：{label}', data

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
            # 不要 * 0.1：`_valget_once()` 已按 scale 折算成物理 mm
            'acc_mm': float(values.get('TMODE_FIXED_POS_ACC', 0)),
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
            # **不要再乘 0.1**：`_valget_once()` 已经按 key 表的 scale（=0.1）
            # 把原始 0.1mm 折算成物理 mm 了。这里再乘一次会得到 1/10 的值
            # （真机 3000mm 的门限被读成 300mm），而且"看起来像合理的数"
            # 所以极难发现。测试 `test_get_svin_config_provides_flow_field_names`
            # 用 30000 → 3000.0 钉住了这条。
            'svin_acc_limit_mm': float(values.get('TMODE_SVIN_ACC_LIMIT', 0)),
            'x': _combine('TMODE_ECEF_X', 'TMODE_ECEF_X_HP'),
            'y': _combine('TMODE_ECEF_Y', 'TMODE_ECEF_Y_HP'),
            'z': _combine('TMODE_ECEF_Z', 'TMODE_ECEF_Z_HP'),
            # 不要 * 0.1：`_valget_once()` 已按 scale 折算成物理 mm
            'acc_mm': float(values.get('TMODE_FIXED_POS_ACC', 0)),
            'layer': layer,
        }
        # **流程层契约：必须同时给出 `ecef_x/y/z`。**
        # 真机踩过一次：驱动只回 `x/y/z`，而 writer 的 `_compare_fixed()` 和
        # `_step_svin_resume()` 都读 `ecef_x/y/z` —— 于是每个轴都取到 None → 兜成 0.0，
        # 顶点（-2428448, 5028827, 3071012）与 0 的差就是 5 026 715 m，
        # 报"回读不一致：最大轴差 5028827 m"，看起来像写失败了，
        # 而实际上模块里存的是完全正确的坐标。这种"写对了却判失败"最难查，
        # 因为现场只有一句含糊的差值。
        #
        # 两个名字都保留：`x/y/z` 是 UBX 键的自然写法（TMODE_ECEF_X），
        # `ecef_x/y/z` 是与 Quectel / Unicore 对齐的统一口径。
        # 复制而不是改名，是为了不让已有的调用方（诊断页、备份）掉。
        data['ecef_x'] = data['x']
        data['ecef_y'] = data['y']
        data['ecef_z'] = data['z']

        # **同一个坑的第二次**：流程层的 `_svin_config_verified()` 判据写的是
        # `readback.get('count') != int(count)`，而这里返回的叫 `svin_min_dur`，
        # 于是 `_to_int(None) != 600` 永远成立 —— 真机表现是
        #   回读次数是 None，应为 600（重发一次再读，仍然 None）
        # 然后整条子收敛流程判失败并回滚。而模块里 `TMODE_SVIN_MIN_DUR` 分明是 600。
        # 补 `count` 别名，语义与 `svin_min_dur` 完全相同（都是**秒**）。
        # 注意单位：F9P 是秒，Quectel 的 count 是历元数；1 Hz 下相等，10 Hz 下不等。
        # 这里不换算，只保证"同一份配置读回来能对上写入值"。
        data['count'] = data['svin_min_dur']
        # `acc` 是流程层口径（**米**），`svin_acc_limit_mm` 是驱动原生的毫米。
        data['acc'] = data['svin_acc_limit_mm'] / 1000.0
        return True, '坐标配置已读取', data

    def enable_survey_status(self, enabled=True, rate=1):
        """开关 NAV-SVIN 的周期输出（F9P 的"收敛状态"就是 UBX-NAV-SVIN）。

        **这个方法之前漏了**，而流程层是用 `getattr` 探测的：
        `_step_svin_cfg()` 里 `enabler is None` 就**静默跳过**，于是 NAV-SVIN
        速率一直是 0，`survey_status()` 永远读不到状态，SVIN_WAIT 只能干等到超时。
        真机表现（2026-11，COM12）：
            [SVIN_WAIT] 等待状态输出…（已 3 秒）…（已 22 秒）
            然后报"Survey-in 在 20 秒内未收敛（已 0/600 次）"
        看起来像"信号差、收不到卫星"，实际是**根本没打开状态输出**。
        两个坑叠在一起时最难查：一个是信号（真），一个是配置（假）。

        与 Quectel 的差别：那边是 `PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1` 文本语句，
        这边是 MSGOUT 键 `NAVOUT_NAV_SVIN_<PORT>`，且**必须写在当前链路的口上**
        （按端口分的键，写错口会安静 ACK 而毫无效果）。

        `persist=True`：收敛状态是标定期间的临时输出，但流程失败回滚会把它
        连同其它速率一起回放，所以这里随写入一起落盘，保证"回滚也能关回去"。
        """
        name = 'NAV-SVIN'
        want = int(rate) if enabled else 0
        if want < 0:
            return False, 'NAV-SVIN 输出速率不能为负', {'error': 'bad_rate'}
        port = self.detect_port()[0] or 'UART1'
        ok, message, data = self.set_message_rate(name, want, port=port)
        if not ok:
            return False, f'开启收敛状态输出失败：{message}', {
                'error': 'nak' if message == 'NAK' else 'timeout', 'port': port}
        return True, f'收敛状态输出已{"开启（%d Hz）" % want if want else "关闭"}', {
            'msg': name, 'rate': want, 'port': port,
            'key': f'NAVOUT_NAV_SVIN_{port}'}

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
            # 名字必须写对它自己能认的那个：这个 hint 会被现场直接照抄进诊断页，
            # 写 `RTCM3-NAVSVIN` 的话 `set_message_rate()` 会回"不认识报文名"，
            # 用户就以为模块不支持 —— 而真正要设的只是 NAV-SVIN 的输出速率。
            port = (self.detect_port()[0] or 'UART1')
            return False, (f'没有收到 UBX-NAV-SVIN。F9P 的收敛状态是周期输出，'
                           f'需要先把 NAV-SVIN 在 {port} 口的输出速率设为 ≥1'), {
                'error': 'no_nav_svin',
                'hint': f'set_message_rate("NAV-SVIN", 1)  # 当前链路 {port}'}

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
            # **流程层契约（第三个同类坑）**：`_step_svin_wait()` 的收敛判定、
            # 进度文案、以及落库字段全读 `mean_acc`（口径是**米**），
            # 驱动只给 `mean_acc_mm` 的话每个读点都拿到 None ——
            # 真机表现是 `当前 3D 精度 ? m`，然后哪怕 obs 攒够了也永远判不收敛，
            # 一路等到超时。米/毫米同时给，谁用哪套都不会错。
            'mean_acc': mean_acc * 0.1 / 1000.0,
            'valid': valid,
            'active': active,
            'x': _combine(mx, mxhp),
            'y': _combine(my, myhp),
            'z': _combine(mz, mzhp),
        }
        # **流程层契约**：`_step_svin_wait()` 收敛后读的是 `mean_x/mean_y/mean_z`。
        # 驱动给 `x/y/z` 的话，每个轴都是 None，收敛判定通过后照样会抛
        # "没有拿到任何有效收敛均值（观测数为 0），无法写入" ——
        # 明明模块已经收敛出坐标了，我们却因为读错字段名把整次标定判失败。
        # 和 `mean_acc` / `count` / `ecef_x` 是同一类错的第四次。
        data['mean_x'] = data['x']
        data['mean_y'] = data['y']
        data['mean_z'] = data['z']
        if valid:
            return True, f'Survey-in 已收敛（{dur}s，精度 {mean_acc * 0.1:.1f}mm）', data
        if active:
            return True, f'Survey-in 收敛中（{dur}s / {obs} 次观测）', data
        return True, 'Survey-in 未启动', data

    def disable_survey_in(self, persist=True, restart=False):
        """停用 Survey-in / 定点，回到移动站模式。"""
        return self.set_mode('disabled', persist=persist, restart=restart)

    # ==================== 报文速率 / RTCM ====================
    @staticmethod
    def _resolve_msg_key(name):
        """把报文名翻成键表里的**裸键名**（不带端口后缀）。返回 `(key_name, err)`。

        支持四类写法（不区分大小写，`RTCM3-` / `RTCM-` / `NMEA-` 前缀可省）：
            1005 / RTCM3-1005          → `RTCM_1005`
            NAVSVIN / NAV_SVIN         → `NAVOUT_NAV_SVIN`
            GGA / NMEA-GGA             → `NMEAMOUT_GGA`
            RAWX / RXM-RAWX            → `RXMOUT_RXM_RAWX`

        **返回的是不带端口的键名**，加哪个端口后缀由 `_port_key()` 决定 ——
        端口在 F9P 上不是常量（见 `PORT_SUFFIX` 上方那段注释）。

        **设置和读取共用这一个函数**：以前两处各写一份 if/elif，
        加一个报文类型就得记着改两个地方，漏一个就表现为"能设不能读"。
        """
        token = str(name).strip().upper()
        for prefix in ('RTCM3-', 'RTCM-', 'RTCM', 'NMEA-', 'NMEA', 'RXM-', 'UBX-'):
            if token.startswith(prefix):
                token = token[len(prefix):]
                break
        token = token.strip('-_')

        # 4072.0 / 4072.1 带小数点，不满足 isdigit，得先单独识别，
        # 否则会被归成"不认识的名字"，用户看不出是固件不支持。
        if '.' in token:
            try:
                frac = float(token)
            except ValueError:
                return None, f'F9P 驱动不认识报文名 {name!r}'
            if frac in K.UNSUPPORTED_RTCM:
                return None, f'RTCM {token} {K.UNSUPPORTED_RTCM[frac]}'
            if frac in K.RTCM_4072_KEYS:
                return None, (f'RTCM {token} 的键（{K.RTCM_4072_KEYS[frac]:#010x}）'
                              f'在键表里，但本机固件实测 VALGET 静默不回、'
                              f'不确认能否输出，暂不下发')
            return None, f'F9P 不支持输出 RTCM {token}'

        if token.isdigit():
            num = int(token)
            if num in K.RTCM_OUT:
                return f'RTCM_{num}', None
            if num in K.RTCM_INPUT_ONLY:
                return None, (f'RTCM {num} 在 ZED-F9P 上只支持输入、不支持输出'
                              f'（底噪/天线相位中心等基准站元数据类报文）')
            if num in K.RTCM_NO_KEY:
                return None, (f'RTCM {num} 在 ZED-F9P 上没有对应的配置键：'
                              f'{K.RTCM_NO_KEY[num]}')
            return None, f'F9P 不支持输出 RTCM {num}'

        if token in K.NAV_OUT:
            return f'NAVOUT_{token}', None
        # `107X` / `108X` … 里的 X 是"MSM 档位"的占位符（Quectel 口径），
        # 不是通配符：`RTCM3-107X` 表示"GPS 的 MSM 观测报文，具体档位另一处定"。
        # F9P 这边 1074 和 1077 是两个独立键，**单个键解析不了** ——
        # 所以这里明确返回"请用具体号"，而不是含混的"不认识"，
        # 免得流程层看到报错还以为是自己传错了名字。
        #
        # 注意：`MSM_CONSTELLATIONS` 里保留 `RTCM3-107X` 是给流程层描述
        # "要开哪几个星座"用的；真正配速率走 `config_rtcm(msm_type=4|7)`，
        # 那一步会把 X 展开成 1074/1077。
        msm_placeholder = token.rstrip('X')
        if token.endswith('X') and msm_placeholder.isdigit():
            num = int(msm_placeholder)
            if num in (107, 108, 109, 110, 111, 112, 113):
                return None, (f'{name!r} 是 MSM 占位写法，X 代表档位；'
                              f'F9P 请用具体号（MSM4 = {num}4，MSM7 = {num}7）'
                              f'或直接用 config_rtcm(msm_type=...) 一次配齐')
        # NMEA 常写成 GGA / GPGGA / NMEA-GGA，剥掉 2 字母 talker 前缀再认一次
        nmea = token[2:] if len(token) == 5 and token[:2].isalpha() else token
        if nmea in K.NMEA_OUT:
            return f'NMEAMOUT_{nmea}', None

        # 归一化兜底：把两边的下划线全去掉再比，让 `NAVSVIN` / `NAV_SVIN`
        # 与 `RAWX` / `RXM_RAWX` 都能命中。**不做前缀穷举** —— 键表里
        # 每个组的前缀不一样（NAVOUT_/RXMOUT_/NMEAMOUT_），穷举必漏。
        # 这里只认 UART1 视图（`K.*_OUT`）；端口后缀由 `_port_key()` 事后加，
        # 所以不必也不该把分端口的 `*_PORTS` 表列进来。
        flat = token.replace('_', '').replace('-', '')
        for group, prefix, strip in (
                (K.NAV_OUT, 'NAVOUT_', 'NAV_'),
                (K.RXM_OUT, 'RXMOUT_', 'RXM_'),
                (K.NMEA_OUT, 'NMEAMOUT_', '')):
            for key in group:
                candidates = {key.replace('_', ''),
                              key[len(strip):].replace('_', '') if strip else None}
                if flat in candidates:
                    return f'{prefix}{key}', None
        return None, f'F9P 驱动不认识报文名 {name!r}'

    def set_message_rate(self, name, rate, port=None, **kwargs):
        """配某条报文的输出速率。

        `name` 的写法见 `_resolve_msg_key()`：
            'RTCM3-1005' / '1005'  → CFG-MSGOUT-RTCM_3X_TYPE1005
            'NAVSVIN' / 'NAV_SVIN' → CFG-MSGOUT-UBX_NAV_SVIN_<port>
            'GGA' / 'NMEA-GGA'     → CFG-MSGOUT-NMEA_ID_GGA_<port>
            'RAWX'                 → CFG-MSGOUT-RXM_RAWX_<port>
        `rate` = 每历元输出次数（Hz），0 = 关闭。

        `port` 默认 `None` = 用 `detect_port()` 探出的链路端口。
        **不要传 'UART1' 当默认值** —— 链路在 USB 上时那组键是另一个物理口，
        写完 ACK 正常、输出一动不动。
        RTCM 键没有端口维度，`port` 对它无效。

        流程层只会用 `COORD_MSG` 里的名字（'RTCM3-1005'）来"表达要播坐标"，
        本驱动把它翻成 1005 的 MSGOUT 键 —— 和 LC29H(BS) 把
        `set_message_rate('RTCM3-1005', 1)` 翻成 PAIR434 是同一件事。
        """
        bare_key, err = self._resolve_msg_key(name)
        if bare_key is None:
            return False, err, {'error': 'unsupported', 'msg': name}
        key_name = self._port_key(bare_key, port)

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

    def get_message_rate(self, name, msg_ver=None, port=None):
        """查某条报文的输出速率。解析规则与 `set_message_rate` 完全一致。

        ## `fields` 不是装饰，是流程层的唯一读法

        `writer._read_rate()` 写的是：

            fields = data.get('fields') or []
            return int(float(fields[1]))        # 速率是第 2 个字段

        这是 Quectel `$PQTMCFGMSGRATE,R` 的口径（`msg,rate,ver` → `fields[1]`）。
        F9P 早期只返回 `{'rate': ...}`，没有 `fields`，于是**每一次速率回读都得到
        `None`**，writer 就判"回读速率是 None（应为 1），配置没有生效"，
        在 `MSG_SETUP` 直接中止。但这不能让流程层去认 F9P 的字段名 ——
        反过来才安全：驱动把 `fields` 按通用形状补出来，流程层不用知道型号。

        形状对齐 Quectel：`[报文名, 速率]`（Quectel 还有第 3 个版本号，
        F9P 没有，就不硬凑一个假的 —— `fields[1]` 才是被读的那个）。
        """
        bare_key, err = self._resolve_msg_key(name)
        if bare_key is None:
            return False, err, {'error': 'unsupported', 'msg': name}
        key_name = self._port_key(bare_key, port)
        values, _ = self._valget([key_name])
        if key_name not in values:
            return False, f'读取 {name} 速率失败（无应答）', {'error': 'timeout'}
        rate = int(values[key_name])
        return True, f'{name} 速率 = {rate}', {
            'msg': name, 'rate': rate, 'key': key_name,
            # 流程层契约：fields[1] 必须是速率
            'fields': [str(name), str(rate)],
            'port': self._port,
        }

    # ==================== 构造 ====================
    def __init__(self, channel):
        super().__init__(channel)
        # 链路端口缓存（'UART1' / 'USB'）。None = 还没探过，`detect_port()` 会去探。
        # 不在这里主动探：构造驱动时串口可能刚打开、缓冲里全是开机流，
        # 探测会被淹没；等真正要用端口的动作（set_message_rate / silence_outputs）
        # 发生时再探，那时流已经稳定。
        self._port = None

    # ==================== 端口识别 ====================
    def _port_from_name(self, port_name=None):
        """按串口名猜它是哪个 UBX 端口。猜不出返回 None。

        **只作兜底**：Windows 的 COM 号不带任何物理口信息，它是不是 CDC 只能靠
        `detect_port()` 真去问模块。但 `detect_port()` 要读几百字节、
        没星时还得等超时，所以先用名字做一次便宜判断：
            COM* / /dev/ttyACM*  → 可能是 USB（CDC）；也可能是 FTDI 转串口的 UART1
            其它（/dev/ttyS*、/dev/ttyUSB*）→ 偏 UART1
        名字判断只用来定"先探哪个"，最终以 `detect_port()` 的结果为准。
        """
        name = str(port_name or getattr(self.channel, 'port', '') or '').upper()
        if name.startswith('COM') or 'TTYACM' in name:
            return 'USB'
        return None

    def _candidate_ports(self):
        """要探测的端口，按可能性从高到低。"""
        # 按链路名猜出来的排前面（省一次没必要的探测）
        guessed = self._port_from_name()
        order = ['USB', 'UART1'] if guessed == 'USB' else ['UART1', 'USB']
        return order

    def detect_port(self, force=False):
        """探出**当前这条链路**实际走的是 USB 还是 UART1，返回端口名。

        ## 为什么非探不可（真机教训）

        `CFG-MSGOUT-*` 的键是**按端口分开**的：`NAVOUT_NAV_PVT`（UART1）和
        `NAVOUT_NAV_PVT_USB` 是两个不同的键 ID。往 UART1 那组写 0，
        模块回一个漂亮的 ACK，而 USB 口上的输出一个字节都不会变 ——
        表现为"关不掉的多余输出"，非常难查。

        探测方法不靠猜名字，也不靠"看哪个口的键非 0"（用户可能本来就把
        两个口都关着，那样全是 0，判不出来）。改成**问模块要"最近的输出口"**：

            UBX-MON-COMMS（0x0A/0x36）的 `protIds` 数组列出所有当前激活的
            协议端口，每个元素 40 字节，第一个字节就是 portId：
                0=I2C 1=UART1 2=UART2 3=USB 4=SPI
            **本机正在用的那个口一定在列表里且 `txPending`/`msgs` 有值。**

        拿不到 MON-COMMS（老固件 / 被关掉了）时退回名字判断，
        并明确标注 `source`，让上层知道这个结论有多可信。

        结果缓存到 `self._port`；`force=True` 重新探。
        """
        if self._port and not force:
            return self._port, f'链路端口：{self._port}（缓存）', {
                'port': self._port, 'source': 'cache'}

        raw = self._poll_ubx(K.MSG_MON_COMMS, timeout=1.2)
        if raw:
            port_id = self._active_port_from_comms(raw)
            if port_id is not None:
                name = {0: 'I2C', 1: 'UART1', 2: 'UART2', 3: 'USB', 4: 'SPI'}.get(port_id)
                if name in ('UART1', 'USB'):
                    self._port = name
                    return name, f'链路端口：{name}（MON-COMMS 识别）', {
                        'port': name, 'source': 'MON-COMMS', 'port_id': port_id}

        guessed = self._port_from_name() or 'UART1'
        self._port = guessed
        return guessed, f'链路端口：{guessed}（按串口名推断，MON-COMMS 未应答）', {
            'port': guessed, 'source': 'name', 'port_id': None}

    @staticmethod
    def _active_port_from_comms(raw):
        """从 MON-COMMS 载荷里挑出"本轮对话所在的那个端口"。

        载荷头（PROTVER 27 起）：
            version(1) reserved1(1) nPorts(1) txErrors(1) reserved2(1)
            protIds[0..nPorts-1]  其中 reserved2 是配置个数，容易和 nPorts 搞混，
            所以**以 nPorts 为准**并做长度校验。

        `nPorts` 最多的版本是 5；每个 port 结构 40 字节：
            portId(2) txPending(2) txBytes(4) txUsage(1) txPeakUsage(1)
            rxPending(2) rxBytes(4) rxUsage(1) rxPeakUsage(1)
            overrunErrs(2) msgs[4](8) reserved3(8) skipped(4)
        第一个字节的低 8 位是 portId。

        选法：优先出现顺序里**第一个**有流量的 UART1/USB。理由：模块是按
        内部顺序报的，两者同时存在时（边充边连的板子很少见）以先报的为准，
        且顺序稳定比"取最新"更可预测。真正的判断依据是"这个口有没有在收发"，
        所以先筛 `msgs` 非 0 的，再退回第一个合法口。

        **返回的是整数 portId（1=UART1、3=USB），不是名字。**
        调用方 `detect_port()` 负责翻译成名字 —— 这里只做解包，
        让"怎么认"和"叫什么"分开，测试也能直接钉住原始字节含义。
        """
        if len(raw) < 5:
            return None
        n_ports = raw[2]
        if not (0 < n_ports <= 8):
            return None
        body = raw[5:]
        if len(body) < n_ports * 40:
            return None
        candidates = []
        for index in range(n_ports):
            rec = body[index * 40:(index + 1) * 40]
            if len(rec) < 40:
                break
            port_id = rec[0] & 0xFF
            # msgs[0..3] 是四个协议方向的计数（每 2 字节），全 0 表示这口没流量
            msgs = sum(int.from_bytes(rec[24 + k * 2:26 + k * 2], 'little')
                       for k in range(4))
            candidates.append((port_id, msgs))
        if not candidates:
            return None
        # ① 有流量且是 UART1/USB 的，按列表顺序取第一个
        for port_id, msgs in candidates:
            if msgs > 0 and port_id in (1, 3):
                return port_id
        # ② 都没流量（只发不收也会 msgs=0 的旧固件）：取第一个合法口
        for port_id, msgs in candidates:
            if port_id in (1, 3):
                return port_id
        return None

    def _poll_ubx(self, msg_id, timeout=1.0):
        """发一条无载荷查询（MON-VER / MON-HW / MON-COMMS 这类）并回传载荷。

        `_send_ubx()` 只把 ACK 结果返回，**拿不到载荷**；查询类报文
        **模块根本不回 ACK、只回内容本身**，所以这里独立走一遍：
        清缓存 → 写帧 → 边读边喂解析器 → 超时就放弃。

        与 `_send_ubx()` 的分工是**报文类别**，不是"想不想拿载荷"：

            `_send_ubx`  → 配置类（CFG-VALSET/VALDEL/RST），等 `05 01`/`05 00`
            `_poll_ubx`  → 查询类（MON-*, CFG-VALGET），等同名报文本身

        传错了不会报错，只会**静默超时**（等一个永远不来的 ACK），
        然后失败文案还指向硬件 —— 这类误用在 `detect()` 里真实发生过一次。
        所以这里显式拦一下：配置类报文不该出现在查询路径上。
        """
        if msg_id in _CONFIG_CLASS_MESSAGES:
            raise DriverBug(
                f'UBX {msg_id[0]:02X}/{msg_id[1]:02X} 是配置类报文（模块会回 ACK/NAK、'
                f'不回内容），不能走 _poll_ubx；请改用 _send_ubx()')
        cls, mid = msg_id
        self.channel.cache.get('ubx', {}).pop((cls, mid), None)
        self.channel.write_ubx(cls, mid)
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            line = self.channel.read_line(min(0.25, max(0.05, deadline - time.time())))
            if line is not None:
                self.channel.absorb(line)
            raw = self.channel.ubx_last(cls, mid)
            if raw is not None:
                return raw
        return None

    def _port_key(self, bare_key, port=None):
        """给裸键名加端口后缀。RTCM 键**没有端口维度**（RTCM 只从指定口出，
        且键表里只有一组），原样返回。

        `bare_key` 形如 `NAVOUT_NAV_PVT` / `NMEAMOUT_GGA` / `RXMOUT_RXM_RAWX` /
        `RTCM_1005`。返回 `NAVOUT_NAV_PVT_UART1` / `NAVOUT_NAV_PVT_USB` 这样
        **带完整端口后缀**的名字。

        **UART1 的后缀是 `_UART1`，不是空串**。旧实现把 UART1 映射成空串，
        靠键表里那些"裸名"记录兜着 —— 但裸名只是 UART1 的兼容别名，
        一旦键表按端口重新登记（2026-11 的改动），空串就会拼出查不到的名字，
        `silence_outputs()` 于是静默跳过，症状又是"关不掉输出"。
        宁可多打五个字符，也不要一个隐式约定。
        """
        if port is None:
            port, _msg, _data = self.detect_port()
        if bare_key.startswith('RTCM_'):
            return bare_key
        suffix = PORT_SUFFIX.get(port)
        if suffix is None:
            raise DriverBug(f'不认识的端口 {port!r}，可选 {sorted(PORT_SUFFIX)}')
        # NAVOUT_ / NMEAMOUT_ / RXMOUT_ 三组一律拼后缀，规则完全相同
        for prefix in ('NAVOUT_', 'NMEAMOUT_', 'RXMOUT_'):
            if bare_key.startswith(prefix):
                return f'{prefix}{bare_key[len(prefix):]}_{port}'
        return bare_key

    def _output_keys(self, ports):
        """列出 `ports` 上**本固件键表里存在**的全部 NAV / NMEA / RXM 输出键。

        `silence_outputs()` / `snapshot_outputs()` / `restore_outputs()` /
        `enable_outputs()` 共用这一个枚举，保证"关掉的那批键"和"能开回来、
        能还原的那批键"是**同一集合**。三处各写一份枚举的话，键表一改
        （比如某固件多一条 NAV 报文）就会出现"静音关了 49 条，恢复只认得 37 条"
        这种不对称。它不会报错，只会让用户觉得"还原了但流还是不对"。

        用分端口表遍历，**不用** `K.NAV_OUT` 那几个 UART1 视图：
        视图里的键名是 `NAV_SVIN`（已带 `NAV_`），拼上前缀正好等于规范名；
        但 NMEA 视图里的键名是 `GGA`（不带 `NMEA_`），三组规则不一致，
        靠视图拼会拼出 `NAVOUT_AOPSTATUS_UART1` 这种查不到的半截名字。
        直接按 `(family, 规范名)` 走 `_port_key()`，三组一条路。
        """
        keys = []
        fanout = getattr(K, 'OUTPUT_FANOUT', None)
        for port in ports:
            if port not in PORT_SUFFIX:
                raise DriverBug(f'不认识的端口 {port!r}，可选 {sorted(PORT_SUFFIX)}')
            if fanout:
                # 键表自己宣称了这个口有哪些键（`ubx_keys` 是按固件实测登记的表，
                # 它比"用端口名拼字符串"权威）。拼完仍要 `K.lookup()` 复验：
                # 两者一旦不一致，宁可少枚举一条，也不能下发一个不存在的键。
                candidates = [k for k in fanout if k.endswith(f'_{port}')]
                keys.extend(k for k in candidates if K.lookup(k) is not None)
                continue
            for _family, prefix, name_prefix in OUTPUT_GROUPS:
                table = {'NAVOUT_': K.NAV_OUT_PORTS,
                         'NMEAMOUT_': K.NMEA_OUT_PORTS,
                         'RXMOUT_': K.RXM_OUT_PORTS}[prefix]
                for name, ids in table.items():
                    if not ids.get(port):
                        continue          # 固件里这个口没这个键（如 RXM-MEAS20 无 USB）
                    full = f'{prefix}{name_prefix}{name}_{port}'
                    if K.lookup(full) is None:
                        continue          # 键表里查不到，跳过而不是报错
                    keys.append(full)
        return keys

    def silence_outputs(self, keep=(), persist=True, ports=None):
        """把**当前链路端口**上除 `keep` 以外的所有 NAV / NMEA / RXM 输出全关掉。

        **为什么 F9P 必须单独做这件事**（和 LG290P / LC29H(BS) 不一样）：
            Quectel 那两家切到 base 模式后会**自动**停掉 NMEA、改播 RTCM，
            所以流程层只要配好 RTCM 就行。F9P **没有这个自动行为** ——
            模式一切，NAV-PVT / NAV-SAT / NAV-ODO / NAV-TIMEUTC 照旧按 1 Hz 往外吐，
            外加模块出厂默认开着的 GGA/GSA/GSV/RMC/VTG。真机实测（无天线）：
            38400 下多出来的那几路已经吃掉 ~5% 带宽；有星时 NMEA-GSV 一条
            就是几百字节，串口 3840 B/s 的预算会被 RTCM 观测值挤爆 ——
            下游拿到的是"坐标有、观测值缺"的基站。

        `keep` 接受 `set_message_rate()` 认的任何写法（'NAV_SVIN' / 'GGA' / 'PVT'）。
        返回值里分开列"关掉了哪些 / 本来就没开哪些"：只报"关掉 N 条"会让用户
        以为白折腾一场，看不出哪些本来就是关的。

        ## `ports` 默认跟着链路走（`None` = 只关当前链路那一个口）

        以前默认写死 `('UART1',)`，在本机（USB CDC）上 100% 无效：
        键名对不上物理口，模块 ACK 了但输出照旧。现在默认取
        `detect_port()` 的结果 —— 链路在 USB 就关 USB 口的键。
        **另一个口一概不动**：USB 口常被用来接 u-center 看星图，
        UART1 口可能接着电台，替用户关掉等于断了他的调试通道。
        要同时清两个口得显式传 `('UART1','USB')`。
        """
        keep_keys = set()
        port_note = {}
        if ports is None:
            port, _msg, port_note = self.detect_port()
            ports = (port,)
        ports = tuple(ports)

        # `_resolve_msg_key()` 给的是**不带端口后缀**的裸名（RTCM 除外，它本就没有
        # 端口维度），这里按要清的每个口把后缀补上，才能和 targets 里的名字比。
        keep_raw = []
        for name in keep:
            key_name, err = self._resolve_msg_key(name)
            if key_name is None:
                # `keep` 写错算调用方编程错误：默默忽略会让"以为留着"的报文被关掉
                raise DriverBug(f'silence_outputs 的 keep 里有无法识别的报文名 {name!r}：{err}')
            keep_raw.append(key_name)
        for key_name in keep_raw:
            for port in ports:
                keep_keys.add(self._port_key(key_name, port))

        targets = [k for k in self._output_keys(ports) if k not in keep_keys]
        if not targets:
            return True, '没有需要关闭的输出', {'turned_off': [], 'already_off': [],
                                              'skipped': sorted(keep_keys)}

        # 先读一遍现状：只有真开着的才写 0，避免把"本来就是 0"也算进"关掉了"，
        # 更避免对没开过的键做无谓写入（Flash 有擦写寿命）。
        #
        # 走批量降级版：单条请求里混进本固件没有的键（如 HPG 1.00 没有
        # NAV-AOPSTATUS / NAV-ATT 的输出键）会让模块**整条静默丢弃**，
        # 不降级的话这里会拿到空结果并报"读取失败"，看起来像链路故障。
        before, _layer, absent = self._valget_batched(targets, K.POLL_LAYER_RAM)
        if not before and not absent:
            return False, '读取当前输出速率失败（无 VALGET 应答）', {'error': 'timeout'}
        if not before:
            return False, (f'{len(absent)} 个键本固件不回应，且没有一个能读到当前值'), {
                'error': 'no_key', 'absent': sorted(absent)[:10]}

        to_zero, already = [], []
        for key in targets:
            if key not in before:
                continue              # 这个键在本端口不存在，静默跳过
            if int(before[key]) > 0:
                to_zero.append((key, 0))
            else:
                already.append(key)

        layers = (K.LAYER_RAM | K.LAYER_FLASH) if persist else K.LAYER_RAM
        if to_zero:
            # 一条 VALSET 装得下（NAV 13 + NMEA 7 + RXM 4 = 24 个键，上限 64），
            # 不用分批 —— 分批就得按 windows_bitlen() 算间隔，38 条命令能慢到几秒。
            ok, message, data = self._valset(to_zero, layers=layers)
            if not ok:
                return False, f'关闭多余输出失败（{message}）', {
                    'error': 'nak' if message == 'NAK' else 'timeout',
                    'attempted': [k for k, _ in to_zero]}

        # 回读验证：写了 0 不等于模块真的停了。这条对 F9P 尤其要紧 ——
        # VALSET 的 ACK 只代表"消息被接受"，不代表"值已经换掉"。
        after, _layer2 = self._valget([k for k, _ in to_zero]) if to_zero else ({}, 0)
        still_on = [k for k, _ in to_zero if int(after.get(k, 0) or 0) > 0]

        result = {
            'turned_off': [k for k, _ in to_zero if k not in still_on],
            'already_off': already,
            'kept': sorted(keep_keys),
            'still_on': still_on,
            # 本固件没有这些键（问了不回）：**不是错误**，只是这块板子没实现那条报文。
            # 单独列出来，免得用户看到 "turned_off 少了三条" 以为是漏关了。
            'absent': sorted(absent),
            'persist': bool(persist),
            'ports': list(ports),
            'port_source': port_note.get('source', 'explicit'),
        }
        if still_on:
            return False, (f'{len(still_on)} 条输出关闭后回读仍非 0：' +
                           '、'.join(still_on[:5])), result
        note = ''
        if absent:
            note = f'；{len(absent)} 条本固件不支持，已跳过'
        return True, (f'已关闭 {len(result["turned_off"])} 条非必要输出'
                      f'（{len(already)} 条本来就关着，保留 {len(keep_keys)} 条{note}）'), result

    # ---------- 输出配置：快照 / 开启 / 还原 ----------
    #
    # `silence_outputs()` 只能关，配套必须有三件事，缺一个就是单向破坏：
    #
    #   ① `snapshot_outputs()` —— 关之前刮一份，记住"原来开的哪些"
    #   ② `enable_outputs()`   —— 模块处于"被别的流程关光了"的状态时，
    #                             按流程需要把该开的开回来（不依赖模块里存着啥）
    #   ③ `restore_outputs()`  —— 关完按快照放回
    #
    # 真机教训（ZED-F9P / COM12，HPG 1.32）：那块板子上 `RTCM_1005/1074/1084/
    # 1094/1124/1230` 全是 1，而 NAV / NMEA / RXM 那 69 个输出键里只有
    # `NAVOUT_NAV_SVIN_USB = 1`，其余全 0 —— 它是一台"只剩 RTCM 的纯净基站"。
    # 这种状态本身没错（是用户自己配的），但流程层一旦按出厂默认去要
    # GGA / NAV-PVT，就会拿到一坨沉默的数据，然后在界面上显示"模块无数据"，
    # 排查方向全被带去查接线。所以流程开始前必须能**主动开**，
    # 而不是指望模块里恰好留着需要的输出。

    def snapshot_outputs(self, ports=None):
        """把**当前链路端口**上的全部 NAV / NMEA / RXM 输出速率原样记下来。

        这是 `BaseDriver.snapshot_outputs()` 的**覆盖实现**：签名兼容
        （无位置参数时行为一致），`ports` 只是给驱动自测/诊断用的扩展位；
        流程层一律 `driver.snapshot_outputs()` 无参调用，
        端口由 `detect_port()` 按真实链路探出来。

        `silence_outputs()` 会把除 `keep` 以外的输出全写 0，但流程层的备份表
        `backup.RATE_TARGETS` 只记 12 条（GGA/RMC/GSA/GLL + 七条 RTCM），
        **一条 NAV-* 都不在里面**。于是：

            模块开着 NAV-SVIN + 用户自己配的 NAV-PVT/POSLLH/...
            → silence_outputs 把 49 个键写 0
            → 标定失败，流程回滚
            → `_restore_messages()` 只回放那 12 条，NAV 一条都不放回
            → 模块从此只剩 RTCM + NAV-SVIN，用户在 u-center 里星图没了

        这个损坏**不报任何错**，还原界面还会显示"已按备份回放"。
        所以要另存一份完整快照，还原时按它放回。

        返回 `(ok, message, data)`，`data['rates']` 是**全量**（含 0 的）——
        0 也要记：还原时"本来是关的"同样得写回去，否则会把用户原本关着的
        输出打开。
        """
        if ports is None:
            port, _msg, port_note = self.detect_port()
            ports = (port,)
        else:
            port_note = {'source': 'explicit'}
        ports = tuple(ports)

        keys = self._output_keys(ports)
        if not keys:
            return False, ('没有可快照的输出键（端口不认识）'), {
                'error': 'no_key', 'ports': list(ports)}

        values, layer, absent = self._valget_batched(keys, K.POLL_LAYER_RAM)
        if not values:
            return False, (f'{len(keys)} 个输出键全部无应答，无法快照；'
                           f'此时静音会关掉一批无从恢复的输出'), {
                'error': 'timeout', 'ports': list(ports), 'keys': len(keys)}

        rates = {k: int(v) for k, v in values.items()}
        on = {k: v for k, v in rates.items() if v > 0}
        data = {
            'ports': list(ports),
            'layer': layer,
            'rates': rates,
            'on': on,
            'absent': sorted(absent),
            'port_source': port_note.get('source', 'detect'),
        }
        return True, (f'已快照 {len(ports)} 个口的 {len(rates)} 条输出配置'
                      f'（其中 {len(on)} 条开着）'), data

    def enable_outputs(self, want=(), rate=1, persist=False, ports=None,
                       strict=False):
        """把 `want` 里点名的输出按 `rate` 打开（**只开点名的，不碰别的**）。

        这是 `BaseDriver.enable_outputs()` 的**覆盖实现**：签名兼容
        （前 3 个位置参数与基类完全相同），多出来的 `ports` / `strict`
        都是**带默认值的扩展位** —— 流程层永远按基类签名调用，
        F9P 特有的"开哪个物理口"由驱动自己 `detect_port()` 决定，
        **流程层不需要知道世界上存在 USB / UART1 这种概念**。

        与 `silence_outputs()` 正好互补。场景是"模块的输出被之前的流程关光了，
        现在需要 GGA / NAV-PVT 这些才能干活"。

        `want` 接受 `set_message_rate()` 认的写法（'GGA' / 'NAV_PVT' / 'PVT' /
        'GSA' ...）。`strict=True` 时任一名子解析不出来就抛 `DriverBug`；
        默认 `strict=False` 记为 `unknown` 返回，因为报文名多半来自配置，
        不该让一条写错的配置把整个流程炸掉。

        `persist` 默认 **False**：只改 RAM。开输出属于"临时让链路有数据"，
        没有理由动 Flash —— Flash 有擦写寿命，而且用户可能是特意关掉的。
        真要落盘由调用方显式传 True。

        回读验证与 `silence_outputs()` 同口径：VALSET 的 ACK 只代表
        "消息被接受"，不代表"值已经换掉"。
        """
        if ports is None:
            port, _msg, port_note = self.detect_port()
            ports = (port,)
        else:
            port_note = {'source': 'explicit'}
        ports = tuple(ports)

        try:
            rate = max(1, int(rate))
        except (TypeError, ValueError):
            raise DriverBug(f'开启输出时 rate 必须是正整数，收到 {rate!r}')

        items, resolved, unknown = [], [], []
        for name in want:
            bare_key, err = self._resolve_msg_key(name)
            if bare_key is None:
                if strict:
                    raise DriverBug(f'enable_outputs 的 want 里有无法识别的报文名 {name!r}：{err}')
                unknown.append({'msg': name, 'reason': err})
                continue
            for p in ports:
                key_name = self._port_key(bare_key, p)
                if key_name not in resolved:
                    resolved.append(key_name)
                    items.append((key_name, rate))

        if not items:
            return False, '没有可开启的报文（want 全部无法识别）', {
                'error': 'unsupported', 'unknown': unknown}

        # 先读现状：已经是目标值的就不写（省一次 VALSET，也免得给 Flash
        # 无谓地增加擦写；persist=True 时这点尤其要紧）。
        before, _layer, absent = self._valget_batched(
            [k for k, _ in items], K.POLL_LAYER_RAM)
        to_write = [(k, r) for k, r in items
                    if int(before.get(k, 0) or 0) != r]

        layers = (K.LAYER_RAM | K.LAYER_FLASH) if persist else K.LAYER_RAM
        if to_write:
            ok, message, data = self._valset(to_write, layers=layers)
            if not ok:
                return False, f'开启输出失败（{message}）', {
                    'error': 'nak' if message == 'NAK' else 'timeout',
                    'attempted': [k for k, _ in to_write]}

        after, _layer2 = self._valget([k for k, _ in items])
        failed = [k for k, r in items if int(after.get(k, 0) or 0) != r]

        written = {k for k, _r in to_write}
        result = {
            # `enabled` = 本次真正写进去并回读通过的
            'enabled': [k for k, _r in to_write if k not in failed],
            # 读的时候就已经是目标值，一条都没写
            'already_on': [k for k, _r in items if k not in written],
            'failed': failed,
            'unknown': unknown,
            'absent': sorted(absent),
            'rate': rate,
            'persist': bool(persist),
            'ports': list(ports),
            'port_source': port_note.get('source', 'explicit'),
        }
        if failed:
            return False, (f'{len(failed)} 条输出开启后回读不符：'
                           + '、'.join(failed[:5])), result
        note = f'，{len(result["already_on"])} 条本来就开着' if result['already_on'] else ''
        unknown_note = f'，{len(unknown)} 条报文名不认识已跳过' if unknown else ''
        return True, (f'已开启 {len(result["enabled"])} 条输出'
                      f'（速率 {rate}Hz{note}{unknown_note}）'), result

    def restore_outputs(self, snapshot):
        """按 `snapshot_outputs()` 的快照把输出速率放回。

        这是 `BaseDriver.restore_outputs()` 的**覆盖实现**：签名与基类完全一致
        （只吃快照本身）。基类那份是"逐条喂 set_message_rate"的通用版，
        F9P 这边必须整批走 VALGET / VALSET，并且**只写快照里有的键**。

        `snapshot` 接受：
            · `snapshot_outputs()` 返回的整个 data（取其中 `rates`）
            · 直接给 `{键名: 速率}` 的 dict

        **只写快照里有的键**，快照没读到的键保持模块当前值 —— 快照时
        无应答的键（`absent`）本来就不知道该写什么，硬写 0 会把一个
        原本可能开着的输出关掉，等于换了个方向继续破坏。

        写入用 RAM 层：快照本身是 RAM 快照，回放也只回放 RAM。
        落盘配置（Flash）在快照/还原这条路上完全不动 —— 这样即使还原
        出错，模块重启后仍是用户原来那份 Flash 配置，不会两头都坏。
        """
        if isinstance(snapshot, dict) and 'rates' in snapshot:
            rates = snapshot.get('rates') or {}
            layer = snapshot.get('layer', 0)
        elif isinstance(snapshot, dict):
            rates = snapshot
            layer = 0
        else:
            raise DriverBug(f'restore_outputs 需要快照 dict 或 {{键名:速率}}，收到 {type(snapshot).__name__}')

        if not rates:
            return False, '快照为空，没有可还原的输出配置', {'error': 'empty'}

        items = []
        for key, val in rates.items():
            try:
                items.append((key, max(0, int(val))))
            except (TypeError, ValueError):
                continue
        if not items:
            return False, '快照里的值都不是整数，无法还原', {'error': 'bad_snapshot'}

        # 现状读一遍：已经一致的不写
        current, _l, _absent = self._valget_batched([k for k, _ in items],
                                                    K.POLL_LAYER_RAM)
        to_write = [(k, v) for k, v in items
                    if int(current.get(k, 0) or 0) != v]
        if not to_write:
            return True, f'输出配置与快照一致（{len(items)} 条），无需还原', {
                'restored': [], 'unchanged': [k for k, _ in items],
                'snapshot_layer': layer}

        ok, message, data = self._valset(to_write, layers=K.LAYER_RAM)
        if not ok:
            return False, f'还原输出配置失败（{message}）', {
                'error': 'nak' if message == 'NAK' else 'timeout',
                'attempted': [k for k, _ in to_write]}

        after, _layer2 = self._valget([k for k, _ in to_write])
        bad = [k for k, v in to_write if int(after.get(k, 0) or 0) != v]
        written = {k for k, _v in to_write}
        result = {
            'restored': [k for k, _v in to_write if k not in bad],
            'unchanged': [k for k, _v in items if k not in written],
            'failed': bad,
            'snapshot_layer': layer,
        }
        if bad:
            return False, (f'{len(bad)} 条输出还原后回读不符：'
                           + '、'.join(bad[:5])), result
        return True, f'已按快照还原 {len(result["restored"])} 条输出配置', result

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
            # `MSM_CONSTELLATIONS` 用的是流程层口径的报文名（`RTCM3-107X`），
            # 它同时也是 `set_message_rate()` 认的名字 —— 两种写法都得认，
            # 否则默认值一改就和上面的映射对不上了。
            'RTCM3-107X': tier_msgs[0], 'RTCM3-108X': tier_msgs[1],
            'RTCM3-109X': tier_msgs[2], 'RTCM3-111X': tier_msgs[3],
            'RTCM3-112X': tier_msgs[3],
        }

        plan = {1005: 1, 1230: 1}
        for name in wanted:
            num = constellation_map.get(name)
            if num:
                plan[num] = 1
                continue
            # 也接受直接给具体号（`RTCM3-1074` / `1074`）—— `MSM_CONSTELLATIONS`
            # 用的就是这种写法，两个入口得能对接上。
            digits = name.replace('RTCM3-', '').replace('RTCM-', '').replace('RTCM', '')
            if digits.isdigit() and int(digits) in OBSERVATION_RTCM:
                plan[int(digits)] = 1
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
        """带 `port` 就按它选，否则跟链路走。

        与 MSGOUT 同一个道理：`UART1_BAUDRATE` 和 `UART1_BAUD` 是 UART1 口的键，
        链路在 USB 上时改它们的波特率毫无意义 —— USB CDC 的速率由主机侧决定，
        模块的 `USB_*` 配置里根本没有波特率这一项。所以：
            链路 = UART1 → 返回 'UART1_BAUDRATE'
            链路 = USB   → 返回 None（USB 没有波特率概念）
        调用方拿到 None 要如实回答"USB 链路没有波特率可设"，不要硬写 UART1。
        """
        if port is None:
            port, _msg, _data = self.detect_port()
        if port == 'UART1':
            return 'UART1_BAUDRATE'
        if port == 'UART2':
            return 'UART2_BAUDRATE'
        return None

    def read_uart(self, port=None):
        """回读模块自己报的串口配置。

        `port=None` 时跟链路走：链路在 USB 上就**如实说 USB 无波特率配置**，
        不再像以前那样硬读 UART1 的值然后标成 `'port': 'UART1'` ——
        那种"看起来读到了"的谎报，正是排查"改了没反应"时最误导人的地方。
        """
        link_port, _msg, port_note = self.detect_port() if port is None else (port, '', {})
        if port_note.get('source') == 'explicit':
            port_note = {'port': link_port, 'source': 'explicit'}
        baud_key = self._baud_key(link_port)
        if baud_key is None:
            return True, f'链路走 {link_port}，该口没有波特率配置（由主机侧决定）', {
                'port': link_port, 'baud': None, 'databits': None,
                'parity': None, 'stopbits': None, 'enabled': True,
                'raw': {}, 'note': 'USB CDC 无波特率/帧格式配置项',
                'port_source': port_note.get('source', 'detect'),
            }

        prefix = baud_key[:-len('BAUDRATE')]           # 'UART1_'
        names = [f'{prefix}BAUDRATE', f'{prefix}DATABITS', f'{prefix}PARITY',
                 f'{prefix}STOPBITS', f'{prefix}ENABLED']
        values, _layer = self._valget(names)
        if baud_key not in values:
            return False, '读取串口配置失败（无 VALGET 应答）', {'error': 'timeout'}
        raw = {k: int(v) for k, v in values.items()}
        # 枚举值（E1）的**具体编号**在集成手册里没有、Interface description 里
        # 也只写 stopbits={0,1,2} / databits={0,1} 而不给编号对应关系。
        # 真机 HPG 1.32 回读：STOPBITS=1、DATABITS=0，而模块显然工作在 8N1，
        # 所以这组编号不能按"0=1位、1=1.5位"去猜 —— 猜错会把用户带偏。
        # 因此：原始值照实给（`*_raw`），推断值放前面但标注为推断。
        data = {
            'port': link_port,
            'baud': raw[baud_key],
            # DATABITS: 0 → 8（默认），1 → 7。依据是默认值必须等于出厂 8 位。
            'databits': 7 if raw.get(f'{prefix}DATABITS', 0) == 1 else 8,
            'parity': {0: 'N', 1: 'E', 2: 'O'}.get(raw.get(f'{prefix}PARITY', 0), 'N'),
            # STOPBITS: 默认 1 位停止位，真机读到 1 且能 8N1 通讯 → 1 就是"1 位"。
            'stopbits': 1 if raw.get(f'{prefix}STOPBITS', 1) in (0, 1) else
                        (1.5 if raw.get(f'{prefix}STOPBITS') == 2 else 2),
            'enabled': bool(raw.get(f'{prefix}ENABLED', 1)),
            'raw': {k: v for k, v in raw.items() if k != baud_key},
            'enum_note': '枚举编号来自真机回读推断，手册未给出对应表',
            'port_source': port_note.get('source', 'detect'),
        }
        return True, (f'{link_port} = {data["baud"]} {data["databits"]}'
                      f'{data["parity"]}{data["stopbits"]}'), data

    def set_baud(self, baud, persist=True, restart=False):
        """改 UART1 波特率。

        §3.8.1：改完要留 **100 ms** 再按新速率喂数据，否则模块还没切完，
        后面那几帧会被当成帧错误（一秒内超 100 个帧错误会让 RX 直接关闭）。
        所以这里发完配置就 sleep 0.2 s，并主动把通道的波特率改到新值。

        链路在 USB 上时**直接拒绝**：USB CDC 的速率由主机侧协商，
        改 `UART1_BAUDRATE` 对当前链路没有任何作用，写进去只会让
        UART1 口下次被插上时跑在一个用户不知道的速率上。
        """
        try:
            value = int(baud)
        except (TypeError, ValueError):
            raise DriverBug(f'波特率必须是整数，收到 {baud!r}')
        if value not in self.BAUDS:
            return False, (f'ZED-F9P 不支持 {value}；合法档位：'
                           f'{", ".join(str(b) for b in self.BAUDS)}'), {
                'error': 'bad_baud', 'baud': value, 'supported': list(self.BAUDS)}

        link_port, _msg, _note = self.detect_port()
        baud_key = self._baud_key(link_port)
        if baud_key is None:
            return False, (f'当前链路是 {link_port}，该口没有波特率配置'
                           f'（USB CDC 由主机侧决定速率），无需也不能修改'), {
                'error': 'unsupported', 'port': link_port, 'baud': value}

        layers = (K.LAYER_RAM | K.LAYER_FLASH) if persist else K.LAYER_RAM
        old = self.channel.baud
        ok, message, data = self._valset([(baud_key, value)], layers=layers)
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
        data.update({'old_baud': old, 'baud': value, 'persist': bool(persist),
                     'port': link_port})
        return True, f'{link_port} 波特率 {old} → {value}', data

    def get_baud(self):
        """回读当前波特率（`BAUD_READ_KEY` 指向本方法）。"""
        link_port, _msg, _note = self.detect_port()
        baud_key = self._baud_key(link_port)
        if baud_key is None:
            return False, (f'当前链路是 {link_port}，该口没有波特率配置'), {
                'error': 'unsupported', 'port': link_port, 'baud': None}
        values, _ = self._valget([baud_key])
        if baud_key not in values:
            return False, '读取波特率失败', {'error': 'timeout'}
        return True, f'当前波特率 {int(values[baud_key])}', {
            'baud': int(values[baud_key]), 'port': link_port}

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
        # 我们只保存配置类（IO/MSG/INF/NAV/RINV），不清除任何东西。
        save_mask = K.CFG_CFG_LOAD_MASK       # 0x1F1F：低字节 lproto、高字节 classes
        # deviceMask：bit0=BBR  bit1=Flash  bit2=EEPROM。F9P 只有 BBR + Flash。
        # **旧代码写的 0x11 是错的** —— 那是 bit0|bit4，bit4 在 F9P 上无意义。
        # 载荷固定 13 字节（最后 1 字节是 deviceMask），写成 `<IIII` 会多 3 个 0。
        payload = struct.pack('<IIIB', 0, save_mask, 0, K.CFG_CFG_DEV_BBR_FLASH)
        ok, message = self._send_ubx(*K.MSG_CFG_CFG, payload)
        if not ok:
            return False, f'保存配置失败（{message}）', {
                'error': 'nak' if message == 'NAK' else 'timeout'}
        return True, '配置已固化到 Flash', {'save_mask': hex(save_mask),
                                         'device_mask': hex(K.CFG_CFG_DEV_BBR_FLASH)}

    def read_persisted(self, key_names=None):
        """读 Flash 层的配置（poll 层 2），用来证明"真的落盘了"。

        ## 为什么不能只读 RAM

        `save()` 之后读 RAM 永远是对的 —— RAM 里本来就是刚写进去的值，
        哪怕 Flash 写失败也读不出来。这正是 `ubxconfigdb.py` 六步演示
        （VALGET RAM → VALGET BBR → VALSET BBR → VALGET BBR → VALDEL → VALGET）
        要教的东西：**只有 poll 持久层，才能证明重启后还在**。

        F9P 的持久层是 Flash（BBR 只保 SRAM 且在断电时机上更脆），
        所以这里默认读 Flash。`layer=` 可以换成 `K.POLL_LAYER_BBR`
        或 `K.POLL_LAYER_DEFAULT`（读默认值，用于确认"这个键出厂是多少"）。

        返回 `(values, layer)`，与 `_valget` 一致；读不到时 `values` 为空 dict。
        `key_names=None` 表示读 `CAPS` 里声明的那批常用键（`PERSIST_CHECK_KEYS`）。
        """
        if key_names is None:
            key_names = self.PERSIST_CHECK_KEYS
        return self._valget(list(key_names), layer=K.POLL_LAYER_FLASH)

    def verify_persisted(self, expected=None):
        """确认配置在 Flash 层与预期一致，返回 `(ok, message, data)`。

        `expected` 是 `{键名: 期望物理值}`；不给就用 `read_persisted()` 的返回值
        做一次"能读到就算过"的检查（这种情况只证明 Flash 里有值，不证明值对不对）。

        比较**按键逐个比，不按整表比** —— 模块会对没写过的键回默认值，
        整表比会把"这个键本来就是默认值"误判成失败。
        浮点值按 1e-6 相对误差比，避免 1.2 与 1.1999999 互相判错。
        """
        values, layer = self.read_persisted(list(expected) if expected else None)
        if not values:
            return False, '读不到 Flash 层配置（无 VALGET 应答）', {
                'error': 'timeout', 'layer': layer}

        data = {'layer': layer, 'read': dict(values), 'mismatch': {}}
        if not expected:
            return True, f'Flash 层读到 {len(values)} 个键', data

        missing = [name for name in expected if name not in values]
        if missing:
            data['mismatch'] = {name: {'want': expected[name], 'got': None}
                                for name in missing}
            return False, f'Flash 层缺少 {len(missing)} 个键：{", ".join(missing[:5])}', data

        mismatch = {}
        for name, want in expected.items():
            got = values[name]
            if isinstance(want, float) or isinstance(got, float):
                same = abs(float(got) - float(want)) <= max(1e-6, abs(float(want)) * 1e-6)
            else:
                same = got == want
            if not same:
                mismatch[name] = {'want': want, 'got': got}
        data['mismatch'] = mismatch
        if mismatch:
            head = list(mismatch.items())[:3]
            detail = '；'.join(f'{name} 要 {item["want"]} 实际 {item["got"]}'
                              for name, item in head)
            return False, f'Flash 层有 {len(mismatch)} 个键不符：{detail}', data
        return True, f'Flash 层 {len(expected)} 个键全部对上（已持久化）', data

    def factory_reset(self, clear_flash=True, reboot_after=True):
        """恢复出厂设置（UBX-CFG-CFG 带 clearMask + loadMask）。

        **这是"清空并重载"，不是 save**：`clearMask = loadMask = 0x1F1F`
        表示"IO/msgconf/navconf/tpconf 全清掉，再按默认值重载"。
        pyubx2 的 `ubxfactoryreset.py` 用的就是这一对值。

        ## 两个必须提醒调用方的后果

        1. **波特率会回到 38400**。如果当前跑在别的档位，下发完这条命令
           就再也收不到模块的回话了 —— 所以本方法先 `_send_ubx` 再
           主动把通道波特率改回 `DEFAULT_BAUD`。
        2. **Survey-in / 定点坐标全部丢失**。基准站标定场景里这是"从零开始"，
           流程层如果只是想把配置存盘，应该用 `save()` 而不是这个方法。

        `clear_flash=False` 时不清 Flash，只把 RAM/BBR 恢复默认 ——
        用于"配置改坏了，先回到能动起来的状态，但保留 Flash 里的老配置"。
        """
        device = K.CFG_CFG_DEV_BBR | (K.CFG_CFG_DEV_FLASH if clear_flash else 0)
        # 载荷是 13 字节：clearMask(4) saveMask(4) loadMask(4) deviceMask(1)。
        # **最后一个字段只有 1 字节** —— 写成 4 字节会多出 3 个 0，
        # 模块按错位解析整个载荷，直接 NAK。
        payload = struct.pack('<IIIB',
                              K.CFG_CFG_CLEAR_MASK,        # clearMask：清掉
                              0,                           # saveMask：不存
                              K.CFG_CFG_LOAD_MASK,         # loadMask：重载默认值
                              device)                      # 清哪些存储介质
        ok, message = self._send_ubx(*K.MSG_CFG_CFG, payload, timeout=3.0, retries=2)
        data = {'clear_mask': hex(K.CFG_CFG_CLEAR_MASK),
                'load_mask': hex(K.CFG_CFG_LOAD_MASK),
                'device_mask': hex(device),
                'clear_flash': bool(clear_flash)}
        if not ok:
            data['error'] = 'nak' if message == 'NAK' else 'timeout'
            return False, f'恢复出厂设置失败（{message}）', data

        # 复位后波特率回默认档，本端口必须跟着切，否则后面全是乱码
        time.sleep(0.3)
        if self.channel.baud != self.DEFAULT_BAUD:
            try:
                self.channel.baud = self.DEFAULT_BAUD
                if self.channel.is_open:
                    self.channel._serial.baudrate = self.DEFAULT_BAUD
            except Exception as e:                          # noqa: BLE001
                logging.debug(f'恢复出厂后切波特率失败（忽略）：{e}')
        data['baud'] = self.channel.baud

        if reboot_after:
            # CFG-CFG 的清除动作在模块重启后才真正生效
            r_ok, r_msg = self.reboot()
            data['reboot'] = r_msg
            if not r_ok:
                data['error'] = 'reboot_incomplete'
                return False, f'配置已清除，但重启未确认（{r_msg}）', data
        return True, '已恢复出厂设置', data

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
