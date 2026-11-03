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
         └─────────────── type：0x1=L 0x2=E1/I1 0x3=U2 0x4=I4/U4 0x5=R4 0x6=R8

注意 type 只决定**值占几个字节**，不区分有无符号 —— LAT/LON 是 `0x4` 但按
**有符号** I4 解释，BAUDRATE 也是 `0x4` 但按无符号解释。具体有无符号看下面的
类型元组第三位，别按 type 猜。

键表来源：pyubx2 的 `ubxtypes_configdb.py`（1343 个键，与 u-blox
ZED-F9P Interface description 逐条对过）与 gpsd 的 ubx.py。
集成手册（UBX-18010802）只给键名，**不给 ID**，这是它和协议手册的分工，
不要指望在手册里搜到 `0x40030009`。

## 端口后缀与 ID 的差值规律（防抄错用）

MSGOUT 组里同一条消息的五个端口 ID **连续递增**，顺序固定：

    I2C → UART1 → UART2 → USB → SPI      即  UART1 = I2C+1, USB = I2C+3, SPI = I2C+4

`RTCM_1230` 和 `NAVOUT_NAV_SVIN` 都在这里踩过坑：抄的是 I2C 那行，
比 UART1 小 1，写下去"ACK 成功但完全没效果"。**凡是要动 UART1 的键，
先确认 ID 不是 I2C 版本。** 见文件末尾的 `_self_check()`。
"""

# 值类型（按字节长度 + 有无符号区分，写入时用来定长度和打包方式）
#
# 名字沿用 u-blox 的 att 记法：L=布尔、E1/E2=枚举、X=位域、R=浮点。
# 布尔和枚举在字节层面确实都是 U1，但把它们标成 `U1` 会让后来的维护者
# 按"数值"去理解 7/1/0 这种取值，也会让前端把 `0/1` 渲染成输入框而不是开关。
U1 = ('U1', 1, False)
I1 = ('I1', 1, True)
U2 = ('U2', 2, False)
I2 = ('I2', 2, True)
U4 = ('U4', 4, False)
I4 = ('I4', 4, True)
R4 = ('R4', 4, False)      # 4 字节 IEEE754 单精度
R8 = ('R8', 8, False)      # 8 字节 IEEE754 双精度
L = ('L', 1, False)        # 布尔，0/1
E1 = ('E1', 1, False)      # 1 字节枚举
E2 = ('E2', 2, False)      # 2 字节枚举
X1 = ('X1', 1, False)      # 位域
X2 = ('X2', 2, False)
X8 = ('X8', 8, False)

# ==================== 基准站 / 定点（CFG-TMODE-*，group 0x03） ====================
# 集成手册 §3.1.5 只写键名与语义，取值在这里补齐：
#   MODE     0=disabled 1=survey-in 2=fixed
#   POS_TYPE 0=ECEF     1=LLH
TMODE = {
    'TMODE_MODE':         (0x20030001, E1, 1.0,   '基准站模式 0=禁用 1=Survey-in 2=定点'),
    'TMODE_POS_TYPE':     (0x20030002, E1, 1.0,   '坐标类型 0=ECEF 1=LLH'),
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

# ==================== 串口（CFG-UART1-* / CFG-UART2-*，group 0x52 / 0x53） ====================
UART1 = {
    'UART1_BAUDRATE':     (0x40520001, U4, 1.0,  'UART1 波特率'),
    'UART1_STOPBITS':     (0x20520002, E1, 1.0,  '停止位 0=1 1=1.5 2=2'),
    'UART1_DATABITS':     (0x20520003, E1, 1.0,  '数据位 0=8 1=7'),
    'UART1_PARITY':       (0x20520004, E1, 1.0,  '校验 0=无 1=偶 2=奇'),
    'UART1_ENABLED':      (0x10520005, L, 1.0,   'UART1 使能'),
}

# UART2 在 F9P 模块上没引出（板子只有 UART1 + USB + I2C/SPI），但键在固件里
# 是存在的 —— 走 UART2 转接的载板、以及同族 M9N/F9R 会用到。
UART2 = {
    'UART2_BAUDRATE':     (0x40530001, U4, 1.0,  'UART2 波特率'),
    'UART2_STOPBITS':     (0x20530002, E1, 1.0,  '停止位 0=1 1=1.5 2=2'),
    'UART2_DATABITS':     (0x20530003, E1, 1.0,  '数据位 0=8 1=7'),
    'UART2_PARITY':       (0x20530004, E1, 1.0,  '校验 0=无 1=偶 2=奇'),
    'UART2_ENABLED':      (0x10530005, L, 1.0,   'UART2 使能'),
}

# ==================== USB（CFG-USB-*，group 0x65） ====================
USB = {
    'USB_ENABLED':     (0x10650001, L, 1.0,  'USB 口使能'),
    'USB_SELFPOW':     (0x10650002, L, 1.0,  'USB 自供电（1=不从总线取电）'),
    'USB_VENDOR_ID':   (0x3065000A, U2, 1.0, 'USB VID'),
    'USB_PRODUCT_ID':  (0x3065000B, U2, 1.0, 'USB PID'),
    'USB_POWER':       (0x3065000C, U2, 1.0, 'USB 最大电流，mA'),
}

# ==================== I2C / SPI（group 0x51 / 0x64） ====================
I2C = {
    'I2C_ADDRESS':         (0x20510001, U1, 1.0, 'I2C 从机地址（默认 0x42）'),
    'I2C_ENABLED':         (0x10510003, L, 1.0,  'I2C 口使能'),
    'I2C_EXTENDEDTIMEOUT': (0x10510002, L, 1.0,  'I2C 关闭超时（调试断点用）'),
}
SPI = {
    'SPI_ENABLED':         (0x10640006, L, 1.0,  'SPI 口使能'),
    'SPI_CPOLARITY':       (0x10640002, L, 1.0,  'SPI CPOL'),
    'SPI_CPHASE':          (0x10640003, L, 1.0,  'SPI CPHA'),
    'SPI_EXTENDEDTIMEOUT': (0x10640005, L, 1.0,  'SPI 关闭超时'),
    'SPI_MAXFF':           (0x20640001, U1, 1.0, 'SPI 单次最大字节数'),
}

# ==================== 端口协议开关（CFG-*INPROT-* / CFG-*OUTPROT-*） ====================
# group 字节：0x71=I2CIN 0x72=I2COUT 0x73=UART1IN 0x74=UART1OUT 0x75=UART2IN
#             0x76=UART2OUT 0x77=USBIN 0x78=USBOUT 0x79=SPIIN 0x7A=SPIOUT
# 关掉 NMEA 只留 UBX+RTCM 是基准站的标准做法：NMEA 会白占带宽。
# 用 UBX 配置时**必须先确认 UART1INPROT_UBX=1**，否则下一帧配置就进不来。
OUTPROT = {
    'UART1OUTPROT_UBX':    (0x10740001, L, 1.0, 'UART1 输出 UBX'),
    'UART1OUTPROT_NMEA':   (0x10740002, L, 1.0, 'UART1 输出 NMEA'),
    'UART1OUTPROT_RTCM3X': (0x10740004, L, 1.0, 'UART1 输出 RTCM3'),
    'USBOUTPROT_UBX':      (0x10780001, L, 1.0, 'USB 输出 UBX'),
    'USBOUTPROT_NMEA':     (0x10780002, L, 1.0, 'USB 输出 NMEA'),
    'USBOUTPROT_RTCM3X':   (0x10780004, L, 1.0, 'USB 输出 RTCM3'),
    'UART2OUTPROT_UBX':    (0x10760001, L, 1.0, 'UART2 输出 UBX'),
    'UART2OUTPROT_NMEA':   (0x10760002, L, 1.0, 'UART2 输出 NMEA'),
    'UART2OUTPROT_RTCM3X': (0x10760004, L, 1.0, 'UART2 输出 RTCM3'),
    'I2COUTPROT_UBX':      (0x10720001, L, 1.0, 'I2C 输出 UBX'),
    'I2COUTPROT_NMEA':     (0x10720002, L, 1.0, 'I2C 输出 NMEA'),
    'I2COUTPROT_RTCM3X':   (0x10720004, L, 1.0, 'I2C 输出 RTCM3'),
    'SPIOUTPROT_UBX':      (0x107A0001, L, 1.0, 'SPI 输出 UBX'),
    'SPIOUTPROT_NMEA':     (0x107A0002, L, 1.0, 'SPI 输出 NMEA'),
    'SPIOUTPROT_RTCM3X':   (0x107A0004, L, 1.0, 'SPI 输出 RTCM3'),
}

INPROT = {
    'UART1INPROT_UBX':    (0x10730001, L, 1.0, 'UART1 接收 UBX（**关掉就再也发不进配置**）'),
    'UART1INPROT_NMEA':   (0x10730002, L, 1.0, 'UART1 接收 NMEA'),
    'UART1INPROT_RTCM2X': (0x10730003, L, 1.0, 'UART1 接收 RTCM2'),
    'UART1INPROT_RTCM3X': (0x10730004, L, 1.0, 'UART1 接收 RTCM3（移动站用）'),
    'UART1INPROT_SPARTN': (0x10730005, L, 1.0, 'UART1 接收 SPARTN（PPP-RTK 用）'),
    'UART2INPROT_UBX':    (0x10750001, L, 1.0, 'UART2 接收 UBX'),
    'UART2INPROT_NMEA':   (0x10750002, L, 1.0, 'UART2 接收 NMEA'),
    'UART2INPROT_RTCM3X': (0x10750004, L, 1.0, 'UART2 接收 RTCM3'),
    'USBINPROT_UBX':      (0x10770001, L, 1.0, 'USB 接收 UBX'),
    'USBINPROT_NMEA':     (0x10770002, L, 1.0, 'USB 接收 NMEA'),
    'USBINPROT_RTCM3X':   (0x10770004, L, 1.0, 'USB 接收 RTCM3（把 USB 当移动站串口用）'),
    'I2CINPROT_UBX':      (0x10710001, L, 1.0, 'I2C 接收 UBX'),
    'SPIINPROT_UBX':      (0x10790001, L, 1.0, 'SPI 接收 UBX'),
}

# ==================== RTCM3 输出到 UART1（CFG-MSGOUT-*，group 0x91） ====================
# 值 = 每个导航历元的输出次数（Hz）。0 = 关闭。
# 集成手册 §3.1.5 的硬约束：
#   1) 所有观测报文必须同速率；2) 不可混用 MSM4 与 MSM7；
#   3) 必须含 1230 或 1033，否则 GLONASS 只能浮点解；
#   4) 同一星座只能开一种观测报文。
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
    # 曾有 bug：写成 0x20910303，那是 _I2C 端口，比 UART1 小 1。
    # 于是"开了 1230"的 ACK 落在 I2C 上，UART1 上根本没有 GLONASS 码相位偏差，
    # 现场表现为 RTK 一直浮点、怎么调星座都没用。
    1230:  (0x20910304, U1, 1.0, 'GLONASS 码相位偏差（必需）'),
}

# 这些键存在、也 ACK，但 F9P 不会因此输出报文（只支持输入）。
# **不进 `RTCM_OUT`**：那是"能配置成输出"的白名单，`config_rtcm` 靠它决定
# 写哪些键。放进这里只是为了能 VALGET 到 0、以及把"为什么不让开"解释清楚。
RTCM_INPUT_ONLY = {
    1001: (0x209102AA, U1, 1.0, 'GPS L1 观测（仅输入，F9P 不输出）'),
    1002: (0x209102B4, U1, 1.0, 'GPS L1 扩展观测（仅输入）'),
    1006: (0x209102C3, U1, 1.0, '基站坐标+天线高（仅输入；F9P 改播 1005）'),
    1007: (0x209102C8, U1, 1.0, '天线描述（仅输入；F9P 不输出 1033）'),
    1009: (0x209102AF, U1, 1.0, 'GLONASS L1 观测（仅输入）'),
    1010: (0x209102B9, U1, 1.0, 'GLONASS L1 扩展观测（仅输入）'),
}

# **1033（天线描述）连键都不存在** —— pyubx2 的 1343 个键里没有
# `CFG_MSGOUT_RTCM_3X_TYPE1033_*` 任何一个（已逐条核对）。它和
# `RTCM_INPUT_ONLY` 是两种不同的"不支持"：
#   · INPUT_ONLY：键在、能写 0、但模块不会输出 → VALGET 得到 0，可解释
#   · NO_KEY    ：键根本不存在 → 写它必 NAK，连"设成 0"都做不到
# 所以不能混进同一组，否则报错文案会误导人去"改成输入"而不是"换 1230"。
RTCM_NO_KEY = {
    1033: '天线描述：F9P 无对应配置键，做基准站必须改用 1230（GLONASS 码相位偏置）',
}

# 手册（§3.1.5 输出表）列了 4072.0 / 4072.1 这两条 u-blox 私有 MSM，
# 但 **HPG 1.32 固件上对应的配置键不存在**：实测 VALGET 静默不回、
# 也不回 NAK（2026-02 真机 COM12 验证）。所以：
#   - 不放进 RTCM_OUT（否则 read_rtcm_config 一次查 12 个键会整条超时，
#     连带把 10 个真实键的结果也吞掉）；
#   - 不给它们写 0（VALSET 里带一个不存在的键，整条配置可能被无视）。
# 保留这个集合，是为了让"用户要求配 4072"时能明确回一句"本固件不支持"。
#
# 注意：pyubx2 的配置库里有 `CFG_MSGOUT_RTCM_3X_TYPE4072_0_UART1 = 0x209102ff`
# 与 `..._4072_1_UART1 = 0x20910382`。ID 记在 `RTCM_4072_KEYS` 里作复验线索：
# 换到更新固件后先 VALGET 这两个 ID，回了非零就说明能用了。
UNSUPPORTED_RTCM = {
    4072.0: '本固件（HPG 1.32）无对应配置键，无法开启',
    4072.1: '本固件（HPG 1.32）无对应配置键，无法开启',
}
RTCM_4072_KEYS = {          # 仅作复验线索，**不要**拿去写 VALSET
    4072.0: 0x209102FF,
    4072.1: 0x20910382,
}

# 各星座的 MSM4 / MSM7 报文号（同一星座只能开一种）
MSM_BY_CONSTELLATION = {
    'GPS':  {4: 1074, 7: 1077},
    'GLO':  {4: 1084, 7: 1087},
    'GAL':  {4: 1094, 7: 1097},
    'BDS':  {4: 1124, 7: 1127},
    'QZSS': {},          # F9P 不单独输出 QZSS 观测报文（并入 GPS MSM）
}

# ==================== RTCM 非 MSGOUT 配置（group 0x09） ====================
# DF003 = 基站站号（0~4095）。单站部署可以不动；但**接了 CORS 播发端、
# 或者两个站共用一台 caster 时必须改**，否则移动站会把两路差分当成同一来源。
RTCM_CFG = {
    'RTCM_DF003_OUT':              (0x30090001, U2, 1.0, '本机作为基站播出的站号 DF003（0~4095）'),
    'RTCM_DF003_IN':               (0x30090008, U2, 1.0, '只接收该站号的差分（0=不过滤）'),
    'RTCM_DF003_IN_FILTER':        (0x20090009, E1, 1.0, 'DF003 过滤 0=全部 1=仅匹配 2=除匹配'),
    'RTCM_DF028_OUT':              (0x30090010, U2, 1.0, '播出的 DF028 站号（MSM 头部用）'),
    'RTCM_MSM_OUT_RANGERATE_SIGN': (0x10090011, L, 1.0,  'MSM 伪距率符号反转（兼容老接收机）'),
}

# ==================== NAV 输出（CFG-MSGOUT-UBX_NAV_*，group 0x91） ====================
# 只列标定/日常运维真正会用的那些；全 60 多条见 pyubx2 的配置库。
# 端口规律：USB = UART1 + 2（I2C→UART1→UART2→USB→SPI，中间隔着 UART2）。
NAV_OUT = {
    'NAV_PVT':       (0x20910007, U1, 1.0, 'NAV-PVT 定位解算结果'),
    # 曾有 bug：写成 0x20910088，那是 _I2C 端口，Survey-in 状态永远收不到
    'NAV_SVIN':      (0x20910089, U1, 1.0, 'NAV-SVIN Survey-in 状态'),
    'NAV_HPPOSLLH':  (0x20910034, U1, 1.0, 'NAV-HPPOSLLH 高精度经纬高'),
    # 曾有 bug：写成 0x20910263 —— 这个 ID 在权威表里根本不存在
    'NAV_EOE':       (0x20910160, U1, 1.0, 'NAV-EOE 历元结束标志'),
    'NAV_SAT':       (0x20910016, U1, 1.0, 'NAV-SAT 卫星状态（调试用）'),
    'NAV_SIG':       (0x20910346, U1, 1.0, 'NAV-SIG 信号质量（调试用）'),
    'NAV_STATUS':    (0x2091001B, U1, 1.0, 'NAV-STATUS 解状态'),
    'NAV_RELPOSNED': (0x2091008E, U1, 1.0, 'NAV-RELPOSNED 相对位置（移动站看基线）'),
    'NAV_HPPOSECEF': (0x2091002F, U1, 1.0, 'NAV-HPPOSECEF 高精度 ECEF'),
    'NAV_CLOCK':     (0x20910066, U1, 1.0, 'NAV-CLOCK 钟差'),
    'NAV_DOP':       (0x20910039, U1, 1.0, 'NAV-DOP 精度因子'),
    'NAV_TIMEUTC':   (0x2091005C, U1, 1.0, 'NAV-TIMEUTC UTC 时间'),
    'NAV_VELNED':    (0x20910043, U1, 1.0, 'NAV-VELNED 地速'),
}

NAV_OUT_USB = {
    'NAV_PVT_USB':       (0x20910009, U1, 1.0, 'NAV-PVT 从 USB 输出'),
    'NAV_SVIN_USB':      (0x2091008B, U1, 1.0, 'NAV-SVIN 从 USB 输出'),
    'NAV_HPPOSLLH_USB':  (0x20910036, U1, 1.0, 'NAV-HPPOSLLH 从 USB 输出'),
    'NAV_EOE_USB':       (0x20910162, U1, 1.0, 'NAV-EOE 从 USB 输出'),
    'NAV_SAT_USB':       (0x20910018, U1, 1.0, 'NAV-SAT 从 USB 输出'),
    'NAV_SIG_USB':       (0x20910348, U1, 1.0, 'NAV-SIG 从 USB 输出'),
    'NAV_STATUS_USB':    (0x2091001D, U1, 1.0, 'NAV-STATUS 从 USB 输出'),
    'NAV_RELPOSNED_USB': (0x20910090, U1, 1.0, 'NAV-RELPOSNED 从 USB 输出'),
}

# ==================== RXM 原始观测（CFG-MSGOUT-UBX_RXM_*_UART1） ====================
# 做原始数据记录 / 后处理解算（PPK）才需要，日常基准站不用开。
# **RAWX 在 38400 下塞不下**（每历元几 KB），开了会把 RTCM 挤掉。
RXM_OUT = {
    'RXM_RAWX':  (0x209102A5, U1, 1.0, 'RXM-RAWX 原始观测（PPK/后处理用，需 ≥115200）'),
    'RXM_SFRBX': (0x20910232, U1, 1.0, 'RXM-SFRBX 原始星历（需 ≥115200）'),
    'RXM_MEASX': (0x20910205, U1, 1.0, 'RXM-MEASX 测量值'),
    'RXM_RTCM':  (0x20910269, U1, 1.0, 'RXM-RTCM 收到的差分统计（诊断移动站用）'),
}

# ==================== NMEA 输出（CFG-MSGOUT-NMEA_ID_*_UART1） ====================
# 基准站默认只留 UBX+RTCM，NMEA 全关省带宽。但接老式上位机 / 电台转发时
# 常常必须留 GGA+RMC。
NMEA_OUT = {
    'GGA': (0x209100BB, U1, 1.0, 'NMEA GGA 定位质量'),
    'GLL': (0x209100CA, U1, 1.0, 'NMEA GLL 经纬度'),
    'GSA': (0x209100C0, U1, 1.0, 'NMEA GSA DOP/参与解算卫星'),
    'GSV': (0x209100C5, U1, 1.0, 'NMEA GSV 可见卫星（带宽大户）'),
    'RMC': (0x209100AC, U1, 1.0, 'NMEA RMC 推荐最小定位信息'),
    'VTG': (0x209100B1, U1, 1.0, 'NMEA VTG 地面速度航向'),
    'ZDA': (0x209100D7, U1, 1.0, 'NMEA ZDA 日期时间'),
}

# ==================== NMEA 格式配置（CFG-NMEA-*，group 0x93） ====================
# `NMEA_HIGHPREC` 是标定阶段最常有用的一个：默认 7 位小数看不出一台
# 好基站的毫米级重复性，开了才到 9 位。
NMEA_CFG = {
    'NMEA_HIGHPREC':      (0x10930006, L, 1.0, '高精度坐标（小数位 7→9，标定对比要用）'),
    'NMEA_PROTVER':       (0x20930001, E1, 1.0, 'NMEA 版本 0x21=V2.1 0x40=V4.0 0x41=V4.10'),
    'NMEA_MAXSVS':        (0x20930002, E1, 1.0, 'GSV 每条最多卫星数（0=4，1=8…）'),
    'NMEA_COMPAT':        (0x10930003, L, 1.0, '兼容模式（老接收机）'),
    'NMEA_CONSIDER':      (0x10930004, L, 1.0, '考虑模式'),
    'NMEA_LIMIT82':       (0x10930005, L, 1.0, '限制到 NMEA 0x82 定义的范围'),
    'NMEA_SVNUMBERING':   (0x20930007, E1, 1.0, '卫星编号 0=严格 1=扩展'),
    'NMEA_MAINTALKERID':  (0x20930031, E1, 1.0, '主 talker ID 0=GP 1=GN 2=GL 3=GA 4=GB 5=BD'),
    'NMEA_GSVTALKERID':   (0x20930032, E1, 1.0, 'GSV talker ID（0=GN 1=GP）'),
    'NMEA_BDSTALKERID':   (0x30930033, U2, 1.0, '北斗 talker ID'),
    'NMEA_OUT_INVFIX':    (0x10930021, L, 1.0, '无效定位也输出'),
    'NMEA_OUT_MSKFIX':    (0x10930022, L, 1.0, 'MSK 定位也输出'),
    'NMEA_OUT_INVTIME':   (0x10930023, L, 1.0, '无效时间也输出'),
    'NMEA_OUT_INVDATE':   (0x10930024, L, 1.0, '无效日期也输出'),
    'NMEA_OUT_ONLYGPS':   (0x10930025, L, 1.0, '只输出 GPS 卫星'),
    'NMEA_OUT_FROZENCOG': (0x10930026, L, 1.0, '静止时航向值冻结'),
}

# ==================== 天线监控（CFG-HW-ANT_*，group 0xA3） ====================
# **天线接口损坏时这一组最有用**：短路检测一旦误报，模块会切到
# "antenna short" 状态并关掉偏置供电，表现就是"收不到星但模块一切正常"。
# 关掉 SHORTDET（或把 SUP_ENGINE 设 0）能让有源天线接口烧过的板子继续用无源天线。
HW_ANT = {
    'HW_ANT_CFG_VOLTCTRL':      (0x10A3002E, L, 1.0, '天线偏置供电（0=不供电；接无源天线时关掉）'),
    'HW_ANT_CFG_SHORTDET':      (0x10A3002F, L, 1.0, '天线短路检测（接口烧毁后会误报，可关）'),
    'HW_ANT_CFG_SHORTDET_POL':  (0x10A30030, L, 1.0, '短路检测极性 0=低有效 1=高有效'),
    'HW_ANT_CFG_OPENDET':       (0x10A30031, L, 1.0, '天线开路检测'),
    'HW_ANT_CFG_OPENDET_POL':   (0x10A30032, L, 1.0, '开路检测极性'),
    'HW_ANT_CFG_PWRDOWN':       (0x10A30033, L, 1.0, '短路时掉电保护'),
    'HW_ANT_CFG_PWRDOWN_POL':   (0x10A30034, L, 1.0, '掉电保护极性'),
    'HW_ANT_CFG_RECOVER':       (0x10A30035, L, 1.0, '短路恢复后自动重新供电'),
    'HW_ANT_SUP_SWITCH_PIN':    (0x20A30036, U1, 1.0, '天线电源开关引脚（模块内部定义）'),
    'HW_ANT_SUP_SHORT_PIN':     (0x20A30037, U1, 1.0, '短路检测引脚'),
    'HW_ANT_SUP_OPEN_PIN':      (0x20A30038, U1, 1.0, '开路检测引脚'),
    'HW_ANT_SUP_ENGINE':        (0x20A30054, E1, 1.0, '天线监控引擎 0=关闭 1=自动（**关掉可彻底止住误报**）'),
    'HW_ANT_SUP_SHORT_THR':     (0x20A30055, U1, 1.0, '短路判定门限'),
    'HW_ANT_SUP_OPEN_THR':      (0x20A30056, U1, 1.0, '开路判定门限'),
    'HW_ANT_ON_SHORT_US':       (0x30A3003C, U2, 1.0, '短路后重新供电的间隔，us'),
}

# ==================== 导航引擎（CFG-NAVSPG-*，group 0x11） ====================
NAVSPG = {
    'NAVSPG_DYNMODEL':      (0x20110021, E1, 1.0, '动态模型 0=便携 2=静止 3=步行 4=车载 5=海面 6=飞控1 7=飞控2 8=骑行 9=腕表'),
    'NAVSPG_FIXMODE':       (0x20110011, E1, 1.0, '定位模式 1=仅2D 2=仅3D 3=auto 2D/3D'),
    'NAVSPG_INIFIX3D':      (0x10110013, L, 1.0, '开机直接出 3D 解（不等高度收敛）'),
    'NAVSPG_UTCSTANDARD':   (0x2011001C, E1, 1.0, 'UTC 标准 0=自动 3=USNO 6=SU 7=NTSC'),
    'NAVSPG_DCMMODE':       (0x20110023, E1, 1.0, '差分改正模式 0=禁用 2=RTCM3 等'),
    'NAVSPG_WKNROLLOVER':   (0x30110017, U2, 1.0, 'GPS 周数翻转基准（默认 2137，2026-11 前有效）'),
    'NAVSPG_USE_PPP':       (0x10110019, L, 1.0, 'PPP 模式（需要 L 波段/SSR 改正数据）'),
    'NAVSPG_PL_ENA':        (0x101100D7, L, 1.0, '保护级别输出（NAV-PL）'),
    'NAVSPG_SIGATTCOMP':    (0x201100D6, E1, 1.0, '信号衰减补偿'),
    # 输入滤波：静止基准站可以卡得很紧，掉星能更快被识别出来
    'NAVSPG_INFIL_MINSVS':  (0x201100A1, U1, 1.0, '参与解算最少卫星数'),
    'NAVSPG_INFIL_MAXSVS':  (0x201100A2, U1, 1.0, '参与解算最多卫星数'),
    'NAVSPG_INFIL_MINCNO':  (0x201100A3, U1, 1.0, '最低载噪比门限，dBHz'),
    'NAVSPG_INFIL_MINELEV': (0x201100A4, I1, 1.0, '最低仰角，deg（有符号）'),
    'NAVSPG_INFIL_NCNOTHRS': (0x201100AA, U1, 1.0, '新卫星载噪比门限'),
    'NAVSPG_INFIL_CNOTHRS': (0x201100AB, U1, 1.0, '持续跟踪载噪比门限'),
    # 输出滤波：超过门限的解直接不输出（基准站别用，会吞掉 Survey-in 进度）
    'NAVSPG_OUTFIL_PDOP':   (0x301100B1, U2, 0.1, 'PDOP 门限，物理值（原始 0.1）'),
    'NAVSPG_OUTFIL_TDOP':   (0x301100B2, U2, 0.1, 'TDOP 门限，物理值'),
    'NAVSPG_OUTFIL_PACC':   (0x301100B3, U2, 0.01, '位置精度门限，物理值 m（原始 cm）'),
    'NAVSPG_OUTFIL_TACC':   (0x301100B4, U2, 0.01, '时间精度门限，物理值 m'),
    'NAVSPG_OUTFIL_FACC':   (0x301100B5, U2, 0.01, '频率精度门限，物理值 m/s'),
}

# ==================== 历元速率（CFG-RATE-*，group 0x21） ====================
RATE = {
    'RATE_MEAS':      (0x30210001, U2, 1.0, '测量周期，ms'),
    'RATE_NAV':       (0x30210002, U2, 1.0, '解算倍率（每 N 个测量历元解一次）'),
    'RATE_TIMEREF':   (0x20210003, E1, 1.0, '时基 0=UTC 1=GPS 2=GLO 3=BDS 4=GAL'),
}

# ==================== 信号 / 星座（CFG-SIGNAL-*，group 0x31） ====================
# 关掉某个频点能显著降低带宽，但**改错会让模块直接不出解** —— 只在明确需要时动。
# 注意这组全是 `L`（布尔），写 2 之类的值会 ACK 但行为未定义。
SIGNAL = {
    'SIGNAL_GPS_ENA':      (0x1031001F, L, 1.0, 'GPS 使能'),
    'SIGNAL_GPS_L1CA_ENA': (0x10310001, L, 1.0, 'GPS L1C/A'),
    'SIGNAL_GPS_L2C_ENA':  (0x10310003, L, 1.0, 'GPS L2C'),
    'SIGNAL_GPS_L5_ENA':   (0x10310004, L, 1.0, 'GPS L5'),
    'SIGNAL_GLO_ENA':      (0x10310025, L, 1.0, 'GLONASS 使能'),
    'SIGNAL_GLO_L1_ENA':   (0x10310018, L, 1.0, 'GLONASS L1'),
    'SIGNAL_GLO_L2_ENA':   (0x1031001A, L, 1.0, 'GLONASS L2'),
    'SIGNAL_GAL_ENA':      (0x10310021, L, 1.0, 'Galileo 使能'),
    'SIGNAL_GAL_E1_ENA':   (0x10310007, L, 1.0, 'Galileo E1'),
    'SIGNAL_GAL_E5A_ENA':  (0x10310009, L, 1.0, 'Galileo E5a'),
    'SIGNAL_GAL_E5B_ENA':  (0x1031000A, L, 1.0, 'Galileo E5b'),
    'SIGNAL_BDS_ENA':      (0x10310022, L, 1.0, '北斗使能'),
    'SIGNAL_BDS_B1_ENA':   (0x1031000D, L, 1.0, '北斗 B1I'),
    'SIGNAL_BDS_B1C_ENA':  (0x1031000F, L, 1.0, '北斗 B1C'),
    'SIGNAL_BDS_B2_ENA':   (0x1031000E, L, 1.0, '北斗 B2I'),
    'SIGNAL_BDS_B2A_ENA':  (0x10310028, L, 1.0, '北斗 B2a'),
    'SIGNAL_SBAS_ENA':     (0x10310020, L, 1.0, 'SBAS 使能'),
    'SIGNAL_SBAS_L1CA_ENA': (0x10310005, L, 1.0, 'SBAS L1C/A'),
    'SIGNAL_QZSS_ENA':     (0x10310024, L, 1.0, 'QZSS 使能'),
    'SIGNAL_QZSS_L1CA_ENA': (0x10310012, L, 1.0, 'QZSS L1C/A'),
    'SIGNAL_QZSS_L2C_ENA': (0x10310015, L, 1.0, 'QZSS L2C'),
    'SIGNAL_QZSS_L5_ENA':  (0x10310017, L, 1.0, 'QZSS L5'),
    'SIGNAL_PLAN':         (0x2031003A, E1, 1.0, '频点规划 0=默认（别乱动）'),
}

# ==================== 安全（CFG-SEC-*，group 0xF6） ====================
SEC = {
    'SEC_CFG_LOCK':        (0x10F60009, L, 1.0, '**配置锁**：置 1 后所有 CFG-* 写入被拒（含本键）'),
    'SEC_JAMDET_SENSITIVITY_HI': (0x10F60051, L, 1.0, '干扰检测高灵敏度'),
    'SEC_SPOOFDET_SIM_SIG_DIS':  (0x10F6005D, L, 1.0, '关闭欺骗检测的"相似信号"告警'),
}

# ==================== 其它 ====================
MISC = {
    'HW_OSC_VOLTAGE':      (0x40A30052, U4, 1.0, 'TCXO 电压（出厂标定，别动）'),
    'HW_OSC_CURRENT':      (0x40A30053, U4, 1.0, 'TCXO 电流（出厂标定，别动）'),
    'HW_TCXO_DC_BIAS_ENABLE': (0x10A3005E, L, 1.0, 'TCXO 直流偏置'),
    'HW_RF_LNA_MODE':      (0x20A30057, E1, 1.0, 'RF LNA 模式 0=正常 1=低增益'),
}

# 全表合并：写入时按 key 名查。
ALL_KEYS = {}
for _group in (TMODE, UART1, UART2, USB, I2C, SPI, OUTPROT, INPROT,
               NAVSPG, RATE, SIGNAL, SEC, MISC, NMEA_CFG, HW_ANT,
               NAV_OUT, NAV_OUT_USB, RXM_OUT, NMEA_OUT, RTCM_CFG,
               RTCM_INPUT_ONLY):
    ALL_KEYS.update(_group)
ALL_KEYS.update({f'RTCM_{num}': rec for num, rec in RTCM_OUT.items()})
ALL_KEYS.update({f'RTCM_{num}': rec for num, rec in RTCM_INPUT_ONLY.items()})
ALL_KEYS.update({f'NAVOUT_{name}': rec for name, rec in NAV_OUT.items()})
ALL_KEYS.update({f'NAVOUT_{name}': rec for name, rec in NAV_OUT_USB.items()})
ALL_KEYS.update({f'RXMOUT_{name}': rec for name, rec in RXM_OUT.items()})
ALL_KEYS.update({f'NMEAMOUT_{name}': rec for name, rec in NMEA_OUT.items()})


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
MSG_NAV_RELPOSNED = (0x01, 0x3C)

# ==================== 配置层（CFG-VALSET / VALDEL 的 layer 字节） ====================
# **注意 set 与 poll 的取值不同**，这是 u-blox 的一个反直觉设计：
#   SET:  RAM=1  BBR=2  Flash=4        （可以 OR 起来，7 = 三层全写）
#   POLL: RAM=0  BBR=1  Flash=2  Default=7
LAYER_RAM = 0x01      # 易失，立即生效，断电丢
LAYER_BBR = 0x02      # 电池后备 RAM
LAYER_FLASH = 0x04    # 固化到 Flash（**固化必须带这个**）
LAYER_ALL = 0x07      # RAM|BBR|Flash —— pyubx2 官方示例的推荐写法

POLL_LAYER_RAM = 0x00
POLL_LAYER_BBR = 0x01
POLL_LAYER_FLASH = 0x02
POLL_LAYER_DEFAULT = 0x07

# CFG-VALSET / VALDEL 的 transaction 动作
TRANS_NONE = 0x00
TRANS_START = 0x01
TRANS_ONGOING = 0x02
TRANS_APPLY = 0x03

# 单条 VALSET/VALDEL 的 key 数上限（模块硬限制，pyubx2 里同样卡在 64）
MAX_KEYS_PER_VALSET = 64

# CFG-CFG（工厂复位）的掩码：clearMask 与 loadMask 都取 0x1F1F 表示
# "IO/msgconf/navconf/tpconf 全部清掉并按默认值重载"。
# **它会把波特率也复位回 38400**，下发前务必确认串口能跟着切。
# 取值与 pyubx2 的 `ubxfactoryreset.py` 一致。
CFG_CFG_CLEAR_MASK = 0x1F1F
CFG_CFG_LOAD_MASK = 0x1F1F

# CFG-CFG 的 deviceMask 位（第 4 字节）：选清哪些存储介质。
# **注意不是 IO/MSG/NAV 那一套位**，这一字节单独定义：
#   bit0 = devBBR   bit1 = devFlash   bit2 = devEEPROM
CFG_CFG_DEV_BBR = 0x01
CFG_CFG_DEV_FLASH = 0x02
CFG_CFG_DEV_EEPROM = 0x04
CFG_CFG_DEV_BBR_FLASH = 0x03        # F9P 上只有 BBR + Flash，这是 safe() 用的值

# UART1 默认波特率（出厂值，也是工厂复位后的值）
DEFAULT_BAUD = 38400


# 同一 ID 会被登记两次的"别名前缀"。这些前缀**只改查找名，不改语义**：
#   NAVOUT_  给 set_message_rate('NAV_PVT') 用，同时 NAV_OUT 里有裸名 `NAV_PVT`
#   RXMOUT_  同上
#   NMEAMOUT_ 同上
#   RTCM_    报文号是 int（RTCM_OUT 的键），ALL_KEYS 里必须转成字符串
# 其余前缀之间撞 ID 就是真的抄错了，自检会拦。
_ALIAS_PREFIXES = ('NAVOUT_', 'RXMOUT_', 'NMEAMOUT_', 'RTCM_')


def _canonical(name):
    """把别名前缀剥掉，得到"这条记录究竟代表哪个键"。"""
    name = str(name)        # RTCM_OUT 的键是 int 报文号
    for prefix in _ALIAS_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _self_check():
    """导入时自检键表结构。任何一条不满足都是抄表错误，直接炸掉。

    只查**结构性**问题（元组长度、ID 范围、类型合法、scale 为正、ID 不撞），
    不查具体 ID 是否等于某个值 —— 那种断言放在 `tests/test_ublox_f9p.py` 里，
    改表时能看清 diff。

    ID 撞车按 `_canonical()` 判：`NAV_PVT` 与 `NAVOUT_NAV_PVT` 是同一键的
    两个入口，canonical 相同，放行；`RTCM_1006` 跑到了 1005 的 ID 上，
    canonical 不同（`1006` vs `1005`），直接炸。
    """
    valid_types = {U1, I1, U2, I2, U4, I4, R4, R8, L, E1, E2, X1, X2, X8}
    seen = {}
    for name, rec in ALL_KEYS.items():
        if len(rec) != 4:
            raise AssertionError(f'{name}: 记录必须是 (id, type, scale, note) 四元组')
        key_id, type_info, scale, note = rec
        if not (0 < key_id <= 0xFFFFFFFF):
            raise AssertionError(f'{name}: ID {key_id:#x} 越界')
        if type_info not in valid_types:
            raise AssertionError(f'{name}: 未知类型 {type_info}')
        if not isinstance(scale, (int, float)) or scale <= 0:
            raise AssertionError(f'{name}: scale 必须为正数，收到 {scale!r}')
        if not note:
            raise AssertionError(f'{name}: 缺注释（前端要拿它显示）')
        seen.setdefault(key_id, {}).setdefault(_canonical(name), []).append(name)
    bad = {}
    for key_id, by_canon in seen.items():
        # 真撞车 = 同一个 ID 上挂着**两个不同的 canonical 名**。
        # 同一 canonical 下的多个名字（裸名 + NAVOUT_/RXMOUT_ 别名）是同一键，放行。
        if len(by_canon) > 1:
            bad[hex(key_id)] = {c: sorted(ns, key=str) for c, ns in by_canon.items()}
    if bad:
        raise AssertionError(
            f'键 ID 撞车（不同键共用一个 ID，写入必错一组）：{bad}'
            f'\n提示：MSGOUT 的端口偏移是 I2C→UART1→UART2→USB→SPI，'
            f'UART1 = I2C+1、USB = I2C+3。')


_self_check()
