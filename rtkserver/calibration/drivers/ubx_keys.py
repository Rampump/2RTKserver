# -*- coding: utf-8 -*-
"""u-blox Generation 9 配置键表（CFG-* 键名 → 32 位 key ID + 类型 + 缩放）。

**为什么单独一个文件**：这不是"某个型号的命令集"，而是 u-blox Gen9 的
协议常量（同一份键表被 F9P / M9N / F9R / F10 共用）。放进驱动文件会让
驱动膨胀到看不出主逻辑；放进 base.py 又会让 Quectel/和芯星通的通道
无端背上 u-blox 的知识。

32 位 ID 的读法（自己推别的键时用）：

    0x  20   03   00   01
         │    │    └────┴─ item：组内序号
         │    └────────── group：0x03=TMODE 0x91=MSGOUT 0x52=UART1 0x74=OUTPROT
         └─────────────── type：0x1=U1/布尔 0x2=I1 0x3=U2 0x4=U4/I4 0x5=R4 0x6=R8

注意 type 只决定**值占几个字节**，不区分有无符号 —— LAT/LON 是 `0x4` 但按
**有符号** I4 解释，BAUDRATE 也是 `0x4` 但按无符号解释。具体有无符号看下面的
`SIGNED` 表，别按 type 猜。

键表来源：gpsd 的 ubx.py（rtkbase / ubxtool 内部用的那张表）与
u-blox ZED-F9P Interface description。集成手册（UBX-18010802）只给键名，
**不给 ID**，这是它和协议手册的分工，不要指望在手册里搜到 `0x40030009`。
"""

# 值类型（按字节长度 + 有无符号区分，写入时用来定长度和打包方式）
U1 = ('U1', 1, False)
I1 = ('I1', 1, True)
U2 = ('U2', 2, False)
U4 = ('U4', 4, False)
I4 = ('I4', 4, True)

# ==================== 基准站 / 定点（CFG-TMODE-*，group 0x03） ====================
# 集成手册 §3.1.5 只写键名与语义，取值在这里补齐：
#   MODE     0=disabled 1=survey-in 2=fixed
#   POS_TYPE 0=ECEF     1=LLH
TMODE = {
    'TMODE_MODE':         (0x20030001, U1, 1.0,   '基准站模式 0=禁用 1=Survey-in 2=定点'),
    'TMODE_POS_TYPE':     (0x20030002, U1, 1.0,   '坐标类型 0=ECEF 1=LLH'),
    'TMODE_ECEF_X':       (0x40030003, I4, 1.0,   'ECEF X，cm'),
    'TMODE_ECEF_Y':       (0x40030004, I4, 1.0,   'ECEF Y，cm'),
    'TMODE_ECEF_Z':       (0x40030005, I4, 1.0,   'ECEF Z，cm'),
    # --- HP（高精度小数段）：这三个键**本身就是 0.1mm / 1e-9deg 的整数**，
    #     scale 必须是 1.0。写 0.1 会让 `_valset` 把已经转好的整数再除一次 0.1
    #     （33 → 330），直接超出 I1 的 ±127 被拒，真机上是 WriteBug。
    'TMODE_ECEF_X_HP':    (0x20030006, I1, 1.0,   'ECEF X 小数段，整数单位 0.1mm（有符号！）'),
    'TMODE_ECEF_Y_HP':    (0x20030007, I1, 1.0,   'ECEF Y 小数段，整数单位 0.1mm'),
    'TMODE_ECEF_Z_HP':    (0x20030008, I1, 1.0,   'ECEF Z 小数段，整数单位 0.1mm'),
    'TMODE_LAT':          (0x40030009, I4, 1e-7,  '纬度，1e-7 deg'),
    'TMODE_LON':          (0x4003000A, I4, 1e-7,  '经度，1e-7 deg'),
    'TMODE_HEIGHT':       (0x4003000B, I4, 1.0,   '椭球高，cm'),
    'TMODE_LAT_HP':       (0x2003000C, I1, 1.0,   '纬度小数段，整数单位 1e-9 deg（有符号！）'),
    'TMODE_LON_HP':       (0x2003000D, I1, 1.0,   '经度小数段，整数单位 1e-9 deg'),
    'TMODE_HEIGHT_HP':    (0x2003000E, I1, 1.0,   '椭球高小数段，整数单位 0.1mm'),
    # ACC 族的物理值是 **mm**，原始值是 0.1mm，所以 scale=0.1（10mm → 100）
    'TMODE_FIXED_POS_ACC': (0x4003000F, U4, 0.1,  '定点位置精度，物理值 mm（原始 0.1mm）'),
    'TMODE_SVIN_MIN_DUR': (0x40030010, U4, 1.0,   'Survey-in 最短时长，s'),
    'TMODE_SVIN_ACC_LIMIT': (0x40030011, U4, 0.1, 'Survey-in 精度门限，物理值 mm（原始 0.1mm）'),
}

# ==================== 串口（CFG-UART1-*，group 0x52） ====================
UART1 = {
    'UART1_BAUDRATE':     (0x40520001, U4, 1.0,  'UART1 波特率'),
    'UART1_STOPBITS':     (0x20520002, U1, 1.0,  '停止位 0=1 1=1.5 2=2'),
    'UART1_DATABITS':     (0x20520003, U1, 1.0,  '数据位 0=8 1=7'),
    'UART1_PARITY':       (0x20520004, U1, 1.0,  '校验 0=无 1=偶 2=奇'),
    'UART1_ENABLED':      (0x10520005, U1, 1.0,  'UART1 使能'),
}

# ==================== 端口协议开关（CFG-*OUTPROT-*，group 0x74=UART1 0x78=USB） ====================
# 关掉 NMEA 只留 UBX+RTCM 是基准站的标准做法：NMEA 会白占带宽。
# group 字节：0x74 = UART1，0x78 = USB。USB 侧单独一组，别和 UART1 混。
OUTPROT = {
    'UART1OUTPROT_UBX':   (0x10740001, U1, 1.0,  'UART1 输出 UBX'),
    'UART1OUTPROT_NMEA':  (0x10740002, U1, 1.0,  'UART1 输出 NMEA'),
    'USBOUTPROT_UBX':     (0x10780001, U1, 1.0,  'USB 输出 UBX'),
    'USBOUTPROT_NMEA':    (0x10780002, U1, 1.0,  'USB 输出 NMEA'),
    'USBOUTPROT_RTCM3X':  (0x10780004, U1, 1.0,  'USB 输出 RTCM3'),
    'UART1OUTPROT_RTCM3X': (0x10740004, U1, 1.0, 'UART1 输出 RTCM3'),
}

# ==================== RTCM3 输出到 UART1（CFG-MSGOUT-*，group 0x91） ====================
# 值 = 每个导航历元的输出次数（Hz）。0 = 关闭。
# 集成手册 §3.1.5 的硬约束：
#   1) 所有观测报文必须同速率；2) 不可混用 MSM4 与 MSM7；
#   3) 必须含 1230 或 1033，否则 GLONASS 只能浮点解；
#   4) 同一星座只能开一种观测报文。
# **1006 / 1007 / 1033 在这个模块上只支持输入，不支持输出** —— 表里没有它们，
# 不是漏了。写进去会直接 ACK-NAK。
RTCM_OUT = {
    1005:  (0x209102BE, U1, 1.0, '基站坐标（必需）'),
    1074:  (0x209102CC, U1, 1.0, 'GPS MSM4'),
    1077:  (0x209102CD, U1, 1.0, 'GPS MSM7'),
    1084:  (0x209102D1, U1, 1.0, 'GLONASS MSM4'),
    1087:  (0x209102D2, U1, 1.0, 'GLONASS MSM7'),
    1094:  (0x20910318, U1, 1.0, 'Galileo MSM4'),
    1097:  (0x20910319, U1, 1.0, 'Galileo MSM7'),
    1124:  (0x209102D6, U1, 1.0, '北斗 MSM4'),
    1127:  (0x209102D7, U1, 1.0, '北斗 MSM7'),
    1230:  (0x20910303, U1, 1.0, 'GLONASS 码相位偏差（必需）'),
}

# 手册（§3.1.5 输出表）列了 4072.0 / 4072.1 这两条 u-blox 私有 MSM，
# 但 **HPG 1.32 固件上对应的配置键不存在**：实测 VALGET 静默不回、
# 也不回 NAK（2026-02 真机 COM12 验证）。所以：
#   - 不放进 RTCM_OUT（否则 read_rtcm_config 一次查 12 个键会整条超时，
#     连带把 10 个真实键的结果也吞掉）；
#   - 不给它们写 0（VALSET 里带一个不存在的键，整条配置可能被无视）。
# 保留这个集合，是为了让"用户要求配 4072"时能明确回一句"本固件不支持"。
UNSUPPORTED_RTCM = {
    4072.0: '本固件（HPG 1.32）无对应配置键，无法开启',
    4072.1: '本固件（HPG 1.32）无对应配置键，无法开启',
}

# 各星座的 MSM4 / MSM7 报文号（同一星座只能开一种）
MSM_BY_CONSTELLATION = {
    'GPS':  {4: 1074, 7: 1077},
    'GLO':  {4: 1084, 7: 1087},
    'GAL':  {4: 1094, 7: 1097},
    'BDS':  {4: 1124, 7: 1127},
    'QZSS': {},          # F9P 不单独输出 QZSS 观测报文（并入 GPS MSM）
}

# ==================== NAV 输出（CFG-MSGOUT-UBX_NAV_*_UART1） ====================
NAV_OUT = {
    'NAV_PVT':      (0x20910007, U1, 1.0, 'NAV-PVT 定位解算结果'),
    'NAV_SVIN':     (0x20910088, U1, 1.0, 'NAV-SVIN Survey-in 状态'),
    'NAV_HPPOSLLH': (0x20910034, U1, 1.0, 'NAV-HPPOSLLH 高精度经纬高'),
    'NAV_EOE':      (0x20910263, U1, 1.0, 'NAV-EOE 历元结束标志'),
    'NAV_SAT':      (0x20910016, U1, 1.0, 'NAV-SAT 卫星状态（调试用）'),
    'NAV_SIG':      (0x20910346, U1, 1.0, 'NAV-SIG 信号质量（调试用）'),
    'NAV_STATUS':   (0x2091001B, U1, 1.0, 'NAV-STATUS 解状态'),
}

# ==================== 导航引擎（CFG-NAVSPG-*，group 0x11） ====================
NAVSPG = {
    'NAVSPG_DYNMODEL':  (0x20110021, U1, 1.0, '动态模型：2=静止 4=车载 0=便携'),
    'NAVSPG_FIXMODE':   (0x20110011, U1, 1.0, '定位模式 1=2D 2=3D 3=auto'),
}

# ==================== 历元速率（CFG-RATE-*，group 0x21） ====================
RATE = {
    'RATE_MEAS':      (0x30210001, U2, 1.0, '测量周期，ms'),
    'RATE_NAV':       (0x30210002, U2, 1.0, '解算倍率（每 N 个测量历元解一次）'),
    'RATE_TIMEREF':   (0x20210003, U1, 1.0, '时基 0=UTC 1=GPS 2=GLO 3=BDS 4=GAL'),
}

# ==================== 信号（CFG-SIGNAL-*，group 0x31） ====================
# 关掉某个频点能显著降低带宽，但**改错会让模块直接不出解** —— 只在明确需要时动。
SIGNAL = {
    'SIGNAL_GPS_ENA':      (0x1031001F, U1, 1.0, 'GPS 使能'),
    'SIGNAL_GLO_ENA':      (0x10310025, U1, 1.0, 'GLONASS 使能'),
    'SIGNAL_GAL_ENA':      (0x10310021, U1, 1.0, 'Galileo 使能'),
    'SIGNAL_BDS_ENA':      (0x10310022, U1, 1.0, '北斗使能'),
    'SIGNAL_SBAS_ENA':     (0x10310020, U1, 1.0, 'SBAS 使能'),
    'SIGNAL_QZSS_ENA':     (0x10310024, U1, 1.0, 'QZSS 使能'),
}

# ==================== 其它 ====================
MISC = {
    'CFG_UART1INPROT_UBX':   (0x10730001, U1, 1.0, 'UART1 接收 UBX'),
    'CFG_UART1INPROT_NMEA':  (0x10730002, U1, 1.0, 'UART1 接收 NMEA'),
    'CFG_UART1INPROT_RTCM3X': (0x10730004, U1, 1.0, 'UART1 接收 RTCM3（移动站用）'),
}

# 全表合并：写入时按 key 名查。两张子表共用同一批 ID，不重复登记。
ALL_KEYS = {}
for _group in (TMODE, UART1, OUTPROT, NAV_OUT, NAVSPG, RATE, SIGNAL, MISC):
    ALL_KEYS.update(_group)
ALL_KEYS.update({f'RTCM_{num}': rec for num, rec in RTCM_OUT.items()})
ALL_KEYS.update({f'NAVOUT_{name}': rec for name, rec in NAV_OUT.items()})


def lookup(name):
    """按名字取 `(key_id, type, scale, note)`；没有返回 None。"""
    return ALL_KEYS.get(name)


# ==================== UBX 消息号 ====================
# (class, id)。配置类消息的 ACK 一律是 UBX-ACK-ACK(0x05/0x01) 或 UBX-ACK-NAK(0x05/0x00)。
MSG_ACK_ACK = (0x05, 0x01)
MSG_ACK_NAK = (0x05, 0x00)

MSG_CFG_VALSET = (0x06, 0x8A)
MSG_CFG_VALGET = (0x06, 0x8B)
MSG_CFG_VALDEL = (0x06, 0x8C)
MSG_CFG_RST = (0x06, 0x04)
MSG_CFG_CFG = (0x06, 0x09)

MSG_MON_VER = (0x0A, 0x04)
MSG_MON_HW = (0x0A, 0x09)
MSG_NAV_SVIN = (0x01, 0x3B)
MSG_NAV_PVT = (0x01, 0x07)
MSG_NAV_HPPOSLLH = (0x01, 0x14)
MSG_NAV_STATUS = (0x01, 0x03)

# 配置层（CFG-VALSET 的 layer 字节）
LAYER_RAM = 0x01      # 易失，立即生效，断电丢
LAYER_BBR = 0x02      # 电池后备 RAM
LAYER_FLASH = 0x04    # 固化到 Flash（**固化必须带这个**）

# CFG-VALSET 的 transaction 动作
TRANS_NONE = 0x00
TRANS_START = 0x01
TRANS_ONGOING = 0x02
TRANS_APPLY = 0x03
