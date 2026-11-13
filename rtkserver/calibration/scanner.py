# -*- coding: utf-8 -*-
"""串口扫描与"是不是 RTK 模块"的快速判定。

**这里只做判定，不做决断。**

为什么这句话要写在第一行：这个模块的输出会直接决定界面显示什么。
一旦它开始"替用户决定"（比如判定不像 GNSS 就不显示这个口、或者判成 F9P
就自动切过去），现场就会出现两种很难查的故障：
  - 模块真的在、但输出是私有二进制（NovAtel OEM / Trimble TSIP），
    被判定成"不是 GNSS"而从列表里消失 —— 用户根本不知道该去点哪里；
  - 一口多设备的分线器上同时挂着模块和别的串口设备，
    自动选中了错的那个，用户以为在标这块板子，实际在标另一块。
所以：**判定结果全部推给前端，由人决定点哪个口、是不是模块。**

两级速度，对应现场的两种诉求：

    probe_passive()   纯读，不发任何字节。0.3 秒/档。
                      用于"扫一遍所有口，看看哪些在吐 GNSS 数据"。
                      不发字节这一点很重要：有的设备收到意外字节会进命令模式、
                      或者改变波特率，扫一眼就把它搞乱了。

    probe_active()    被动读不到时，**才**发最小查询。最多 2 秒/档。
                      用于"这个口我认定了是模块，帮我把波特率找出来"。

为什么判定看**数据特征**而不是看型号：
波特率不对时收到的全是噪声字节，任何驱动都问不出来；反过来波特率对了，
模块自己就会一直吐 NMEA / RTCM / UBX —— 一个字都不用发就能认出来。
"能读懂它在说什么"这件事，比"它能回答我的提问"更早成立。

**但"认出特征"不等于"波特率对了"。** 这条是踩过坑才补上的：
波特率错了字节流会错位采样，`0xD3`、`B5 62` 这类两字节帧头随机出现时
"看起来"完全自洽 —— 真机实测 9600/19200/230400/921600 四个错档位都能
从 F9P 身上凑出校验和正确的 UBX 帧。所以判定分两层：

    第一层 特征（cheap）        这条流里有没有 GNSS 的味道
    第二层 帧完整性（strict）   报文的**校验位**对不对、功能域合不合理

第二层才是"波特率对不对"的真判据：真机实测 LG290P，
正确档位 NMEA 校验和 47/47 全过，错档位一个完整的 `$...*hh` 行都凑不出来。
两层的结论都写进返回值的 `verdict` 字段推给前端，不做过滤 ——
扫描器依旧只判定、不决断。

## 第三件事：这块到底是 RTK 还是普通 GNSS

`family='gnss'` 这个判定太粗：**接到 RTCM 差分的基准站模块**和**九块九的
单点定位 GPS 模块**会落进同一档。用户照着这行去挑，挑到廉价模块，等整条
标定跑到 `FIX_WAIT` 才发现它根本不会差分 —— 时间全白花。

所以返回值多一个 `class` 字段，判据只认硬件事实：

    CLASS_RTK    读到 **CRC-24Q 校验通过**的 RTCM3 帧 —— 差分数据是 RTK 的身份证
    CLASS_GNSS   只有定位数据（NMEA / UBX / 各家私有），没有差分

**一条 `$GNGGA` 绝不等于 RTK。** 单点定位的 GPS 模块同样每秒发 `$GNGGA`，
它只是"知道自己在哪"，不知道"厘米级在哪"。能证明 RTK 的最小充分证据是
RTCM 帧，其余一切（NMEA / UBX / 私有协议）只证明"这是一台 GNSS 设备"。

`features` 也因此扩了一圈：除了 NMEA / RTCM / UBX，还识别
Septentrio SBF、Ashtech/Hemisphere `$PASHR`、NovAtel OEM、Trimble TSIP、
以及 `$PUBX` / `$PMTK` 这一族厂商私有 NMEA。认得出厂牌就报厂牌
（`vendor`），认不出就只报"厂商私有 NMEA"—— 不猜。
"""
import logging
import re
import time

# 扫描波特率：**从高到低**。
#
# 高的在前而不是低的在前，理由是现场的物理事实：模块一旦被配成基准站，
# 输出的数据量是流动站的好几倍（5 个星座 MSM4 + 1005 + NMEA），
# 工程上都会把波特率提到 115200 以上。低档位（9600/4800）在这套部署里
# 几乎只出现在"刚出厂、还没人配过"的板子上。
# 所以按高到低排，命中的平均轮次更少 —— 而每一轮都是 0.3 秒起的等待。
SCAN_BAUDS = (921600, 460800, 230400, 115200, 57600, 38400, 19200, 9600, 4800)

# 每档被动监听多久。0.35 秒的依据：1 Hz 输出的模块在 0.35 秒内
# 只有 35% 的概率吐一句；但基准站/流动站在标定场景下至少 1 Hz，
# 多数是 5~10 Hz（F9P 默认 1 Hz，LG290P 默认 1 Hz，N4 默认 1 Hz）。
# 真机上把这一档调到 1 秒，9 档就要 9 秒，而 9 秒里有一半时间是
# 在等一个**已经知道答案**的口 —— 所以宁可多扫一轮，不要一次扫太久。
PASSIVE_WINDOW = 0.35
# 主动询问窗口：发一条最短的查询，等这么久。波特率不对时模块
# 收到的是乱码、不会回应，所以这里等不到就是这一档不对。
ACTIVE_WINDOW = 1.2

# 判定标签。给前端直接显示，所以是中文短词，不带括号。
FAMILY_GNSS = 'gnss'          # 确认是 GNSS/RTK 模块家族
FAMILY_UNKNOWN = 'unknown'    # 没读到特征字节（空口、或模块静默）
FAMILY_NOISE = 'noise'        # 有字节但一个特征都不符合

# 设备等级：比 `family` 再细一层，回答"这块到底能不能差分"。
# 判据见模块 docstring —— RTCM 帧（CRC 通过）才判 RTK，其余只判 GNSS。
CLASS_RTK = 'rtk'
CLASS_GNSS = 'gnss'

# 特征 → 说明。前端原样显示这个列表，让用户自己判断。
# 顺序也是可信度顺序：RTCM / NMEA / UBX 有帧结构与校验位，
# 私有 ASCII 只是"设备在打招呼"。
FEATURE_LABEL = {
    'rtcm': 'RTCM 差分数据',
    'nmea': 'NMEA 语句',
    'ubx': 'u-blox UBX 二进制',
    'sbf': 'Septentrio SBF 二进制',
    'sirf': 'SiRF 二进制',
    'ashtech': 'Ashtech/Hemisphere 定位语句',
    'novatel': 'NovAtel OEM 二进制',
    'trimble': 'Trimble TSIP 二进制',
    'unicore': '和芯星通 ASCII',
    'pq': 'Quectel PQTM/PAIR 命令',
    'nmea_priv': '厂商私有 NMEA 语句',
    'nmea_qx': 'QX 私有语句（姿态/DR）',
}

# NMEA 会话 ID。$GP $GN $BD $GL $GA $GB $GQ $GI $GS
# 只认 GPS/GLONASS/Galileo/北斗/QZSS/NavIC 这几家的组合 —— 不写成 `\$[A-Z]{2}`
# 是因为那样连 `$PMTK`（联发科私有）和 `$PUBX`（u-blox 私有 NMEA）都会被算成
# "NMEA 定位语句"，而那两种恰好是**非 RTK** 的廉价模块常发的。
# 那两个走 `_PRIVATE_NMEA_RX`，单独归到 `nmea_priv`。
#
# `TXT` 也算：`$GNTXT` / `$GPGGA` 之外的这条是模块**自报厂名与版本**的通道
# （u-blox 默认就发 `$GNTXT,01,01,02,u-blox AG ...`）。不算它的话，
# 一条只发 TXT 的流会被判成噪声，厂牌信息白白丢掉。
_NMEA_RX = re.compile(rb'\$(?:GP|GN|GL|GA|GB|GQ|GI|GS|BD|GZ|DT)'
                      rb'(GGA|RMC|GSA|GSV|GLL|GNS|VTG|ZDA|GST|TXT)')
# 和芯星通 / NovAtel 都用 `#` 开头的 ASCII 日志头，**两者格式几乎一样**
# （Unicore 的日志格式就是照着 NovAtel 做的，`#VERSIONA` 两家都有）。
# 所以这里绝不按"看到 `#` 就当 Unicore"下结论 —— 那会把 NovAtel OEM7
# 整条产线认成和芯星通。区分交给 `_ascii_log_vendor()` 看头部内容。
_ASCII_LOG_RX = re.compile(rb'^#([A-Z][A-Z0-9]{2,12}),')
# Unicore 的命令回显格式，NovAtel 没有这一种
_UNICORE_CMD_RX = re.compile(rb'\$command,response:')
# 兼容旧调用点：`#` 日志头（任一厂）或 Unicore 命令回显
_UNICORE_RX = re.compile(rb'(?:^#[A-Z][A-Z0-9]{2,12},|\$command,response:)', re.M)
# Quectel：PQTM / PAIR 两族
_QUECTEL_RX = re.compile(rb'\$(?:PQTM[AC-Z]|PAIR\d{3})')
# Quectel 版本应答：`$PQTMVERNO,<型号>,<固件>,...` —— 直接给出型号
_QUECTEL_VERNO_RX = re.compile(rb'\$PQTMVERNO,([^,*\r\n]{2,40})')
# Septentrio SBF：块头 `$@`（0x24 0x40）
_SBF_RX = re.compile(rb'\x24\x40')
# u-blox 常把厂名写进 NMEA TXT 语句：`$GNTXT,01,01,02,u-blox AG - www.u-blox.com`
_UBX_FWVER_RX = re.compile(rb'FWVER=([A-Z ]{2,8}\s?[0-9.]{2,10})')
# 厂商私有 NMEA：`$P` + 三个大写字母的厂商码。
# **是 3 个字母不是 4 个**：NMEA 私有语句的惯例是 `$P` + 3 字符厂商码
# （`$PUBX` / `$PMTK` / `$PSTI` / `$PSRF` / `$PTNL` / `$PJAV` / `$PGRMZ`），
# 只有 Ashtech 的 `$PASHR` 是 `P` + 4 字符，所以它也会被这条命中 ——
# 归类的分支在 `sniff()` 里按 talker 前 4 字符再分一次。
_PRIVATE_NMEA_RX = re.compile(rb'\$P[A-Z]{3}')
# `$QX` 是一个**不在 NMEA 标准 talker 表里、也不是 `$P<厂商码>` 形式**的私有族。
# 2026-11-13 真机实测（COM9）：一块带 IMU 的多模模组在 0 星时也在发
# `$QXATT`（姿态）与 `$QXDRS`（DR 状态），同时对所有 `$P...` 查询回 `$NACK,0`。
# 两套现有判据（标准 talker / `$P` 私有语句）都匹配不到它，
# 于是扫描结果只显示"NMEA 语句"，看不出"这块带 DR/姿态"这个关键差异。
_QX_RX = re.compile(rb'\$QX[A-Z]{3},')
# 私有 NMEA 的厂商标识（`$` 之后的前 4 个字符）→ 品牌。
#
# **认不出就不报**，不要拿"P"开头就当厂牌 —— 编出来的名字比空白更糟。
# 这张表只收"该厂确有这条私有语句"的确定项，且都要求校验和通过
# （见 `_private_nmea_talkers()`），所以撞中的概率极低。
#
# 为什么值得收这么多：现场最常见的困惑是"这口到底是哪家的模块"，
# 而一家厂往往只有一两条私有语句露头，收漏一条就等于这个厂认不出来。
_PRIVATE_TALKER_VENDOR = {
    b'PUBX': 'u-blox',              # u-blox 的 NMEA 扩展（$PUBX,00 定位 / 40 卫星）
    b'PMTK': 'MediaTek',            # 联发科 MT33xx/MTK 平台（$PMTK001 应答）
    b'PSTI': 'SkyTraq',             # SkyTraq Venus 系列（$PSTI,030,OK）
    b'PSRF': 'SiRF',                # SiRF Star III/IV（$PSRF100 串口配置）
    b'PTNL': 'Trimble',             # Trimble（$PTNLGGK / $PTNLDG / $PTNLPJT）
    b'PJAV': 'Javad',               # Javad（$PJAV）
    b'PASH': 'Ashtech/Hemisphere',  # Ashtech / Hemisphere（$PASHR,ATT / $PASHR,POS）
    b'PGRM': 'Garmin',              # Garmin（$PGRMZ / $PGRME / $PGRMM）
    b'PSTM': 'ST Teseo',            # ST Teseo（$PSTMVER / $PSTMSETPAR）
    b'PSSN': 'Septentrio',          # Septentrio 私有 NMEA（$PSSN,HRP 姿态）
    b'PFEC': 'Furuno',              # Furuno（$PFEC）
    b'PMGN': 'Magellan',            # Magellan（$PMGNST / $PMGNSM）
}
# ---------------------------------------------------------------------------
# 设备自述字符串里的厂名关键词。
#
# 为什么要有这一层：很多模块把厂名写在**文本**里 —— NMEA TXT 语句、
# `#VERSION` 日志头、命令应答的版本串。这些字节用正则认不出来，
# 但一个 6~10 字符的品牌名在文本行里出现，几乎不可能是巧合。
#
# **只在文本行里搜**（见 `_banner_vendor()`），不在整个二进制缓冲里搜：
# 二进制载荷里凑出 "u-blox" 的概率虽低，但把判据限制在"以 $ 或 # 开头的
# 可打印行"里是零成本的严格化。
# 键是 **bytes**：既因为要比对的是原始字节流，也因为 `和芯` 这种中文
# 厂名没法用 ASCII 编码 —— 存成字节就不用每次匹配时再做编码转换。
_BANNER_KEYS = (
    (b'U-BLOX', 'u-blox'),
    (b'UBLOX', 'u-blox'),
    (b'QUECTEL', 'Quectel'),
    (b'UNICORE', 'Unicore'),
    (b'BDSTAR', 'Unicore'),
    ('和芯'.encode('utf-8'), 'Unicore'),
    (b'SEPTENTRIO', 'Septentrio'),
    (b'NOVATEL', 'NovAtel'),
    (b'HEXAGON', 'NovAtel'),
    (b'TRIMBLE', 'Trimble'),
    (b'ASHTECH', 'Ashtech/Hemisphere'),
    (b'HEMISPHERE', 'Ashtech/Hemisphere'),
    (b'JAVAD', 'Javad'),
    (b'COMNAV', 'ComNav'),
    (b'CHCNAV', 'CHC'),
    (b'BYNAV', 'Bynav'),
    (b'ALLYSTAR', 'Allystar'),
    (b'TESEO', 'ST Teseo'),
    (b'SKYTRAQ', 'SkyTraq'),
    (b'MEDIATEK', 'MediaTek'),
    (b'SIRF', 'SiRF'),
    (b'GARMIN', 'Garmin'),
    (b'FURUNO', 'Furuno'),
)
# 厂牌信息在"逐档 → 最终结果"之间的携带字段。
#
# 单独列成元组是因为这几个字段要在四五处返回值里各带一遍（每档证据、
# 打不开、最佳档、全空）。手写会漏 —— 漏一处的后果是前端显示
# "这个口认不出厂家"，而那恰好是用户最需要信息的时刻。
VENDOR_FIELDS = ('vendor', 'vendor_confidence', 'vendor_evidence', 'model', 'firmware')


def _vendor_slice(info, empty=False):
    """从 `sniff()` 结果里抽出厂牌字段，供 probe 的各级返回值展开。"""
    if empty or not info:
        return {key: ([] if key == 'vendor_evidence' else '') for key in VENDOR_FIELDS}
    return {key: info.get(key, [] if key == 'vendor_evidence' else '')
            for key in VENDOR_FIELDS}


# Ashtech / Hemisphere 的 talker（`$PASHR,` 的 `$` 后前 4 字符是 `PASH`）
_ASHTECH_TALKER = b'PASH'


def _crc24q(data):
    """RTCM3 的 CRC-24Q（多项式 0x1864CFB，初值 0）。"""
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def _iter_rtcm_frames(buf, limit=512):
    """逐帧产出 **CRC-24Q 校验通过** 的 RTCM3：`(消息号, 载荷)`。

    抽成迭代器是因为厂牌判定要从 `1033`（接收机/天线自述）里取字符串，
    而"哪些帧算数"必须和 `_count_rtcm_frames()` 用**同一套**判据 ——
    两处各写一份 CRC 走帧，迟早会漂。

    `limit` 只是防呆：一段 0.35 秒的缓冲里不可能有几百帧，
    真到了那个数量说明字节流是乱的，继续扫也没意义。
    """
    index = 0
    total = len(buf)
    seen = 0
    while seen < limit:
        index = buf.find(b'\xd3', index)
        if index < 0 or index + 6 > total:
            return
        second = buf[index + 1]
        if second & 0xC0:                      # 保留位必须为 0
            index += 1
            continue
        length = ((second & 0x03) << 8) | buf[index + 2]
        if not (12 <= length <= 1023):         # 最短的 1005 也有 19 字节
            index += 1
            continue
        end = index + 3 + length + 3           # 头 3 + 载荷 + CRC 3
        if end > total:
            return                             # 帧被截断：后面的不可能完整
        expect = ((buf[end - 3] << 16) | (buf[end - 2] << 8) | buf[end - 1])
        if _crc24q(buf[index:end - 3]) == expect:
            payload = buf[index + 3:end - 3]
            seen += 1
            yield ((payload[0] << 4) | (payload[1] >> 4), payload)
            index = end                        # 校验通过才跳过整帧，避免重复数
        else:
            index += 1


def _count_rtcm_frames(buf):
    """数一数缓冲里有几个**CRC 校验通过**的 RTCM3 帧。

    早先只判"头两字节自洽"（0xD3 + 保留位 0 + 长度 12~1023），
    真机实测这不够：**错波特率下的乱码流里两字节帧头撞中的概率相当高**。
    实测 COM3（LG290P，正确档 115200、34 句 NMEA 全过校验）被误判成
    38400 —— 因为 38400 档那段乱码里凑出了一对 `D3 0x`，长度字段又恰好
    落在 12~1023 内，于是判 `strict / RTCM 1 帧`，还因为"档位最靠前 +
    frames=1"把正确的 115200 压了下去。

    所以现在必须走完整帧 + CRC-24Q 校验。真 RTCM 的 CRC 过得了，
    乱码凑出来的"帧"过不了 —— 这是**唯一**能把两者分开的判据。
    代价是要求缓冲里有完整帧（含 3 字节 CRC），长度不够就跳过、不计入。
    """
    return sum(1 for _msg, _payload in _iter_rtcm_frames(buf))


def _count_sbf(buf):
    """数**结构自洽**的 Septentrio SBF 块（`$@` 块头）。

    SBF 的块头 8 字节：`$@`(2) + 头 CRC(2) + 块 ID(2) + 块长度(2)，
    随后是块体、再跟 4 字节尾 CRC，整块长度 = 12 + 块长度。

    这里**只做结构校验**，不去算那两套 CRC：SBF 头 CRC 的覆盖范围在
    几版手册里写法不一致，按错的算法算会把真块判成假块 —— 宁可少判一层。
    所以 SBF 只作为"这条流是 GNSS 二进制"的证据，不给 `strict`。

    为什么还要卡"块长度 4 字节对齐"：SBF 所有块体都是 4 字节对齐的，
    这是协议硬约束，也是乱码最难凑出来的那一条。
    """
    count = 0
    index = 0
    total = len(buf)
    while True:
        index = buf.find(b'\x24\x40', index)
        if index < 0 or index + 8 > total:
            break
        block_id = buf[index + 4] | (buf[index + 5] << 8)
        length = buf[index + 6] | (buf[index + 7] << 8)
        # 块 ID 实际落在 4000~6000（观测 4xxx、状态 59xx）；
        # 长度非 0、4 字节对齐、整块在缓冲内。
        if length and length % 4 == 0 and 4000 <= block_id <= 6100 \
                and index + 12 + length <= total:
            count += 1
            index += 12 + length
        else:
            index += 2
    return count


def _count_novatel(buf):
    """数**结构自洽**的 NovAtel OEM 帧（`AA 44 12` + 头长 + 载荷长）。

    只看三字节帧头不够：真机上的乱码流里 `AA 44 12` 会随机撞出来。
    有区分度的是**两个长度字段互相自洽**：NovAtel 头里 `header_len`
    （0x1C / 0x20 常见）与 `payload_len` 相加再加 4 字节 CRC，
    正好等于一帧的长度。乱码要同时凑对这两处，概率极低。

    仍不给 `strict`：NovAtel 的 CRC32 是自家变体（初值取反、结果取反），
    和 `zlib.crc32` 不是一回事，算错会把真帧判假。
    """
    count = 0
    index = 0
    total = len(buf)
    while True:
        index = buf.find(b'\xaa\x44\x12', index)
        if index < 0 or index + 10 > total:
            break
        header_len = buf[index + 3]
        payload_len = buf[index + 8] | (buf[index + 9] << 8)
        if 0x1C <= header_len <= 0x40 \
                and index + header_len + payload_len + 4 <= total:
            count += 1
            index += header_len + payload_len + 4
        else:
            index += 3
    return count


def _count_tsip(buf):
    """数**结构自洽**的 Trimble TSIP 帧（DLE `10 03` 头 + 包 ID + 长度 + 体 + `10 03` 尾）。

    真帧体里若出现 0x10 会做 DLE 转义（`10 10`），所以不能拿"下一个
    `10 03`"当帧尾 —— 那样会在转义处提前截断。这里按协议走一遍：
    累积**未转义**字节数，遇到 `10 03` 收尾，再与长度字段比对。

    帧结构：`DLE ETX` + 包 ID(1B) + 长度(2B) + 数据 + `DLE ETX`。
    **长度字段从第 4 个字节开始（跳过包 ID）** —— 一开始把它算到包 ID 上，
    真帧的长度就变成了 `包ID<<8 | 长度高位`，永远对不上。

    长度字段的字节序按 little-endian 读，同时兼容 big-endian：
    Trimble 不同产品线两种都出现过，而这里只拿它和"实际帧尾位置"比对，
    多试一种字节序不会凭空造出帧。

    乱码要凑出帧头、合理长度、位置刚好的帧尾三件事才能骗过它。
    """
    count = 0
    index = 0
    total = len(buf)
    while True:
        index = buf.find(b'\x10\x03', index)
        if index < 0 or index + 6 > total:
            break
        raw_len = buf[index + 3:index + 5]
        candidates = (raw_len[0] | (raw_len[1] << 8), (raw_len[0] << 8) | raw_len[1])
        lengths = [n for n in candidates if 0 < n <= 1024]
        if not lengths:
            index += 2
            continue
        pos = index + 5
        seen = 0
        matched = False
        while pos < total:
            if buf[pos] == 0x10:
                nxt = buf[pos + 1] if pos + 1 < total else None
                if nxt == 0x03:                      # 未转义的帧尾
                    matched = seen in lengths
                    break
                if nxt == 0x10:                      # 转义的数据字节
                    seen += 1
                    pos += 2
                    continue
            seen += 1
            pos += 1
        if matched:
            count += 1
            index = pos + 2
        else:
            index += 2
    return count


def _count_sirf(buf):
    """数**校验和正确**的 SiRF 二进制帧（`A0 A2` 头 + 长度 + 载荷 + 校验 + `B0 B3` 尾）。

    SiRF Star III/IV（含大量国产模块的早期方案）的 NMEA 和所有厂家一样，
    但二进制协议只有它一家用 `A0 A2 … B0 B3` 这个框，所以认出来就能报厂。

    校验和是"字节 2 到载荷末尾"的累加和（mod 0x10000），**必须验** ——
    两字节头 `A0 A2` 在乱码里撞中率不低，只数头会把噪声当成 SiRF。
    """
    count = 0
    index = 0
    total = len(buf)
    while True:
        index = buf.find(b'\xa0\xa2', index)
        if index < 0 or index + 8 > total:
            break
        length = ((buf[index + 2] & 0x7F) << 8) | buf[index + 3]
        end = index + 4 + length + 2 + 2        # 头 4 + 载荷 + 校验 2 + 尾 2
        if 0 < length <= 1024 and end <= total \
                and buf[end - 2:end] == b'\xb0\xb3':
            expect = (buf[end - 4] << 8) | buf[end - 3]
            if sum(buf[index + 2:end - 4]) & 0xFFFF == expect:
                count += 1
                index = end
                continue
        index += 2
    return count


def _private_nmea_talkers(buf):
    """统计**校验和正确**的厂商私有 NMEA 语句，返回 {厂商标识: 条数}。

    标识取 `$` 之后的前 4 个字符：`$PUBX,` → `PUBX`，`$PASHR,` → `PASH`。

    **必须验校验和。** `$Pxxxx,` 在错波特率的乱码里撞中的概率不低
    （4 个字母 + 逗号，比标准 NMEA 的 `$GNGGA` 更容易随机出现）。
    校验和用的是和标准 NMEA 一样的逐字节 XOR —— 同一条判据，
    在"真在说私有协议"和"字节错位凑出来的巧合"之间划线。

    为什么单独拿出来：这一族里除了 `$PASHR`（Ashtech/Hemisphere 的定位语句）
    之外，绝大多数是**单点定位模块**的私有语句（`$PMTK` 是联发科、
    `$PSTI` 是 SkyTraq）。认出来只说明"这是台 GNSS 设备"，
    绝不能因此判成 RTK。
    """
    found = {}
    for line in buf.split(b'\r\n'):
        if len(line) < 9 or not _PRIVATE_NMEA_RX.match(line):
            continue
        star = line.rfind(b'*')
        if star < 0 or star + 3 > len(line):
            continue
        try:
            given = int(line[star + 1:star + 3], 16)
        except ValueError:
            continue
        checksum = 0
        for byte in line[1:star]:
            checksum ^= byte
        if checksum != given:
            continue
        talker = line[1:5]
        found[talker] = found.get(talker, 0) + 1
    return found


def _count_qx_sentences(buf):
    """数**校验和正确**的 `$QX...` 私有语句。

    为什么要验校验和：和 `$P` 私有族一样的理由 —— `$QXATT,` 这种 7 字节头
    在错波特率的乱码里撞中的概率不低。校验位是唯一有区分度的判据。

    为什么单独统计：这一族说明模块**带惯性/姿态输出**（ATT=姿态，
    DRS=DR 状态），它跟"能不能做 RTK"无关，但能解释
    "为什么这块模块的 NMEA 里有一堆看不懂的语句"，
    也提醒用户"这不是一块纯 GNSS 板子"。厂牌认不出就留空，不编。
    """
    hits = 0
    for line in buf.split(b'\r\n'):
        if len(line) < 9 or not _QX_RX.match(line):
            continue
        star = line.rfind(b'*')
        if star < 0 or star + 3 > len(line):
            continue
        try:
            given = int(line[star + 1:star + 3], 16)
        except ValueError:
            continue
        checksum = 0
        for byte in line[1:star]:
            checksum ^= byte
        if checksum == given:
            hits += 1
    return hits


def _ascii_log_vendor(raw):
    """`#` 开头的 ASCII 日志头 → `(厂牌, 头行数, 第一条头行)`。

    为什么必须区分：Unicore 的日志格式大量借鉴 NovAtel，`#VERSIONA,...`
    两家都发。按"看到 `#` 就当和芯星通"判，会把 NovAtel OEM7 整条产线
    认成国产模块 —— 认错厂牌比不认更糟，用户会拿错的命令集去试。

    区分点（看头部第 2 个字段）：
        NovAtel   端口名 `COM1` / `COM2` / `THISPORT` / `USB1` / `IP1` / `NCOM1`
                  时间状态常写作 `FINESTEERING`
        Unicore   端口是个纯数字（`79`），时间状态 `FINE`
    另外 `$command,response:` 是 Unicore 独有的命令回显，也归 Unicore。
    """
    count = 0
    first = b''
    for line in raw.split(b'\r\n'):
        if len(line) < 8 or not line.startswith(b'#'):
            continue
        if not _ASCII_LOG_RX.match(line):
            continue
        count += 1
        if not first:
            first = line[:160]
    if not first:
        return '', 0, b''
    if b'FINESTEERING' in first or re.match(rb'^#[A-Z0-9]+,(?:N?COM|USB|IP|THISPORT)\d*,', first):
        return 'NovAtel', count, first
    if b',FINE,' in first or b',COARSE,' in first:
        return 'Unicore', count, first
    return '', count, first


class _Bits:
    """按位读。RTCM 的字符串字段前面挂的是 8 bit 计数器，必须先读位再切字节。"""

    __slots__ = ('_data', '_pos')

    def __init__(self, data):
        self._data = data
        self._pos = 0

    def read(self, bits):
        value = 0
        for _ in range(bits):
            index = self._pos >> 3
            if index >= len(self._data):
                raise ValueError('位流不足')
            value = (value << 1) | ((self._data[index] >> (7 - (self._pos & 7))) & 1)
            self._pos += 1
        return value

    def text(self):
        """`<8bit 长度><N×8bit ASCII>`。返回去掉尾部空白与控制符的字符串。"""
        length = self.read(8)
        if length > 64:                       # 描述串不会超过 64 字符，超了必是错位
            raise ValueError('字符串长度不合理')
        out = bytearray()
        for _ in range(length):
            out.append(self.read(8))
        return out.decode('ascii', 'ignore').strip('\x00 ')


def _decode_1033(payload):
    """解 RTCM3-1033（接收机与天线描述），返回厂牌/型号/固件/序列号。

    为什么这个比任何 NMEA 都值钱：**基准站模式下模块通常把 NMEA 全关了**，
    这时串口上只有 RTCM。而 1033 是模块自己报的"我是谁" ——
    `RECEIVER TYPE` 里就写着 `Quectel  LG290P` / `u-blox ZED-F9P` 这类字符串。
    不解析它，基准站口就永远只能显示"RTCM 差分数据"，认不出厂家。

    字段顺序（RTCM 10403.3）：
        DF002(12) DF003(12) DF029(8) DF030×N DF031(8)
        DF032(8) DF033×N DF227(8) DF228×N DF229(8) DF230×N DF231(8) DF232×N
    """
    if not payload or len(payload) < 12:
        return None
    bits = _Bits(payload)
    try:
        if bits.read(12) != 1033:
            return None
        station_id = bits.read(12)
        antenna = bits.text()
        bits.read(8)                          # DF031 天线安装方式
        bits.text()                           # DF032 天线序列号
        receiver = bits.text()                # DF227/228 接收机型号
        firmware = bits.text()                # DF229/230 固件版本
        serial = bits.text()                  # DF231/232 接收机序列号
    except (ValueError, IndexError):
        return None
    return {'station_id': station_id, 'antenna': antenna, 'receiver': receiver,
            'firmware': firmware, 'serial': serial}


def _rtcm_descriptor(buf):
    """从缓冲里的 1033 帧取设备自述。没有就返回 None。"""
    for msg, payload in _iter_rtcm_frames(buf):
        if msg != 1033:
            continue
        info = _decode_1033(payload)
        if info and (info['receiver'] or info['antenna']):
            return info
    return None


def _banner_vendor(raw):
    """在**文本行**里找厂名关键词，返回 `(厂牌, 命中原文, rank)`。

    只扫"以 `$` / `#` 开头且不含控制字符"的行。理由写死在 `_BANNER_KEYS`
    上面：二进制载荷里凑出品牌名的概率虽低，但这条限制是零成本的严格化，
    而且能把"整条流里恰好出现 u-blox"这种假阳性挡在外面。

    u-blox 是这里最典型的场景：它默认往 `$GNTXT` 里发
    `u-blox AG - www.u-blox.com`，一行就认全了。

    rank 分两档：
        2（中）  命中在 `$...TXT` 语句里 —— 这是设备自己的输出
        1（低）  命中在 `#` 日志头或其他行 —— 可能是集成商/代理写的
    """
    for line in raw.split(b'\r\n'):
        line = line.strip()
        if len(line) < 8 or line[:1] not in (b'$', b'#'):
            continue
        # 只排除**控制字符**，不排除高位字节：国产模块的横幅会有中文
        # （`和芯星通` / `上海华测`），按 `<=0x7E` 过滤会把它们整行丢掉。
        if any(byte < 0x20 or byte == 0x7F for byte in line):
            continue
        upper = line.upper()
        for key, vendor in _BANNER_KEYS:
            if key in upper:
                shown = line[:80].decode('utf-8', 'ignore')
                is_txt = line[:1] == b'$' and b'TXT,' in upper[:16]
                return vendor, shown, (2 if is_txt else 1)
    return '', '', 0


def _version_hints(raw):
    """从版本应答里取型号/固件。返回 dict（可能为空）。

    覆盖两种"一问就自报家门"的应答：
        Quectel  `$PQTMVERNO,LG290P03AANR01A03S,...`  → 型号直接在前两段
        u-blox   UBX-MON-VER 扩展字段 `FWVER=HPG 1.32` → 固件版本
    两者都只在**主动探测**或模块自发输出时出现，被动扫描多半拿不到 ——
    所以这里拿到是加分，拿不到不影响判定。
    """
    hints = {}
    match = _QUECTEL_VERNO_RX.search(raw or b'')
    if match:
        hints['model'] = match.group(1).decode('ascii', 'ignore').strip()
    for cls, msg_id, payload in _verify_ubx(raw or b''):
        if (cls, msg_id) != (0x0A, 0x04):
            continue
        sw = payload[:30].decode('ascii', 'ignore').strip('\x00 ')
        match = _UBX_FWVER_RX.search(payload)
        hints['firmware'] = match.group(1).decode('ascii', 'ignore').strip() if match \
            else sw
        break
    return hints


def _resolve_vendor(raw, counts, private, ascii_vendor, descriptor):
    """汇总所有厂牌证据，返回 `{vendor, vendor_confidence, vendor_evidence, ...}`。

    **证据分级**（rank 3 = 高，2 = 中，1 = 低）：

        high   设备自述字符串   RTCM-1033 的 `RECEIVER TYPE`、`#VERSION` 日志头、
                              `$PQTMVERNO` 版本应答 —— 设备自己说的名字
        high   专属二进制协议   UBX→u-blox / SBF→Septentrio / AA4412→NovAtel
                              / TSIP→Trimble / SiRF `A0 A2`（都验了校验位）
        high   私有命令族       `$PQTM`/`$PAIR`→Quectel、`$command,response:`→Unicore
        medium 私有 NMEA talker `$PUBX`→u-blox、`$PSTM`→ST Teseo（校验和已验）
        low    文本横幅里的厂名 用户看得到原文，但可能是代理/集成商写的

    为什么要分级而不是"命中即返回"：现场要能解释"凭什么是这家的"。
    每条证据都进 `vendor_evidence`，前端放 tooltip，用户和厂家对质时有据可查。
    """
    evidence = []
    vendors = []
    best_rank = 0

    def add(name, why, rank):
        nonlocal best_rank
        if not name:
            return
        if name in vendors:
            return
        vendors.append(name)
        evidence.append(why)
        best_rank = max(best_rank, rank)

    # ---- 1. 设备自述：最强，因为设备直接报了自己的名字 ----
    if descriptor:
        text = descriptor.get('receiver') or ''
        blob = f'{text} {descriptor.get("antenna") or ""}'.upper().encode('utf-8', 'ignore')
        for key, vendor in _BANNER_KEYS:
            if key in blob:
                add(vendor, f'RTCM-1033 自述接收机型号「{text}」', 3)
                break

    if ascii_vendor:
        add(ascii_vendor, f'`#` ASCII 日志头（{ascii_vendor} 格式）', 3)

    # ---- 2. 专属二进制协议 ----
    if counts.get('ubx'):
        add('u-blox', f'UBX 二进制帧（校验通过 {counts["ubx"]} 帧）', 3)
    if counts.get('sbf'):
        add('Septentrio', f'SBF 块结构（{counts["sbf"]} 块）', 3)
    if counts.get('trimble'):
        add('Trimble', f'TSIP 帧结构（{counts["trimble"]} 帧）', 3)
    if counts.get('sirf'):
        add('SiRF', f'SiRF 二进制帧（校验正确 {counts["sirf"]} 帧）', 3)
    # `counts['novatel']` 里可能混着 ASCII 日志行（见 `sniff()` 的归因），
    # 那种情况上面第 1 步已经报过，这里只报真正的二进制帧。
    if counts.get('novatel') and ascii_vendor != 'NovAtel':
        add('NovAtel', f'NovAtel OEM 帧结构（{counts["novatel"]} 帧）', 3)

    # ---- 3. 私有命令族 ----
    if counts.get('pq'):
        add('Quectel', f'Quectel PQTM/PAIR 命令（{counts["pq"]} 条）', 3)
    if counts.get('unicore') and ascii_vendor != 'Unicore':
        add('Unicore', f'和芯星通 ASCII 命令应答（{counts["unicore"]} 条）', 3)

    # ---- 4. 私有 NMEA talker ----
    # `$PASHR` 在 `sniff()` 里已被单独挑出来归到 `ashtech`（它带伪距/姿态，
    # 是定位语句），所以不在 `private` 里，得单独补一条。
    if counts.get('ashtech'):
        add('Ashtech/Hemisphere', f'`$PASHR` 定位语句 {counts["ashtech"]} 句', 2)
    for talker, hits in sorted(private.items()):
        brand = _PRIVATE_TALKER_VENDOR.get(talker)
        if brand:
            add(brand, f'`${talker.decode("ascii", "ignore")}` 私有语句 {hits} 句', 2)

    # ---- 5. 文本横幅（兜底）----
    banner_vendor, banner_text, banner_rank = _banner_vendor(raw)
    if banner_vendor:
        add(banner_vendor, f'文本里的厂名「{banner_text}」', banner_rank)

    hints = _version_hints(raw)
    if descriptor and descriptor.get('receiver'):
        hints.setdefault('model', descriptor['receiver'])
    if descriptor and descriptor.get('firmware'):
        hints.setdefault('firmware', descriptor['firmware'])

    confidence = {3: 'high', 2: 'medium', 1: 'low'}.get(best_rank, '')
    return {'vendor': vendors[0] if vendors else '',
            'vendor_confidence': confidence if vendors else '',
            'vendor_evidence': evidence,
            'model': hints.get('model', ''), 'firmware': hints.get('firmware', '')}


def sniff(raw):
    """从一段原始字节里认出 GNSS 家族特征与设备等级。

    返回 dict：
        family     FAMILY_GNSS / FAMILY_UNKNOWN / FAMILY_NOISE
        class      CLASS_RTK / CLASS_GNSS（非 GNSS 时为空串）
        vendor     认得出的厂牌（'u-blox' / 'Septentrio' / ...），认不出为 ''
        features   ['rtcm', 'nmea', ...]（按可信度排序）
        counts     各特征的命中次数，给前端显示"读到了多少"
        bytes      收到的字节数

    **判定不等于结论**：`family='gnss'` 只说明"这条流里有 GNSS 数据"，
    `class='rtk'` 也只是"实测到 RTCM 差分数据"。是哪块模块、能不能当基准站，
    仍然推给前端/用户判断。
    """
    raw = raw or b''
    counts = {}
    if not raw:
        return {'family': FAMILY_UNKNOWN, 'class': '', 'vendor': '',
                'vendor_confidence': '', 'vendor_evidence': [],
                'model': '', 'firmware': '', 'features': [],
                'counts': counts, 'bytes': 0, 'note': '没有读到任何字节'}

    nmea = len(_NMEA_RX.findall(raw))
    if nmea:
        counts['nmea'] = nmea
    rtcm = _count_rtcm_frames(raw)
    if rtcm:
        counts['rtcm'] = rtcm

    # UBX 只认**校验通过**的帧。早先数的是 `B5 62` 头部，真机实测在
    # 错波特率的乱码里两字节头撞中率很高（见 `_verify_ubx` 注释），
    # 会把透传桥和纯噪声一起算成 GNSS。校验位是这里唯一有区分度的东西。
    ubx_frames = _verify_ubx(raw)
    if ubx_frames:
        counts['ubx'] = len(ubx_frames)

    sbf = _count_sbf(raw)
    if sbf:
        counts['sbf'] = sbf
    novatel = _count_novatel(raw)
    if novatel:
        counts['novatel'] = novatel
    tsip = _count_tsip(raw)
    if tsip:
        counts['trimble'] = tsip
    sirf = _count_sirf(raw)
    if sirf:
        counts['sirf'] = sirf

    # 厂商私有 NMEA：`$PASHR` 单独归到 ashtech（它是定位语句，含伪距/姿态），
    # 其余（PUBX / PMTK / PSTI / PSRF / PTNL / PJAV / PGRM / PSTM / PSSN）
    # 归 nmea_priv。两者都要求校验和通过 —— 见 `_private_nmea_talkers()`。
    private = _private_nmea_talkers(raw)
    ashtech = private.pop(_ASHTECH_TALKER, 0)
    if ashtech:
        counts['ashtech'] = ashtech
    rest = sum(private.values())
    if rest:
        counts['nmea_priv'] = rest

    # `$QX` 私有族（姿态/DR）：不属标准 talker，也不是 `$P` 形式，单独计。
    qx = _count_qx_sentences(raw)
    if qx:
        counts['nmea_qx'] = qx

    # `#` 开头的 ASCII 日志头：**先分厂再计数**。Unicore 与 NovAtel 的
    # 日志格式几乎一样，按 `#` 直接记成 Unicore 会把 NovAtel 认错厂。
    ascii_vendor, ascii_logs, _first = _ascii_log_vendor(raw)
    if ascii_logs:
        if ascii_vendor == 'NovAtel':
            counts['novatel'] = counts.get('novatel', 0) + ascii_logs
        elif ascii_vendor == 'Unicore':
            counts['unicore'] = ascii_logs
    if _UNICORE_CMD_RX.search(raw):
        counts.setdefault('unicore', len(_UNICORE_CMD_RX.findall(raw)))
    quectel = len(_QUECTEL_RX.findall(raw))
    if quectel:
        counts['pq'] = quectel

    if not counts:
        # 有字节、但一个特征都没有。**如实说"有数据但认不出"**，
        # 不要说"没有模块"—— 有的私有二进制协议不在上表里。
        return {'family': FAMILY_NOISE, 'class': '', **_vendor_slice(None),
                'features': [], 'counts': {}, 'bytes': len(raw),
                'note': '收到数据但不含已知的 GNSS 特征'}

    # **唯一的特征是 UBX，且帧内容不像 GNSS 报文** → 这是透传/蓝牙桥，
    # 不是 GNSS 模块。真机实测 COM12 每个波特率都吐 01-3B 心跳，
    # 若照计数判就会把它当成 GNSS 模块推给前端（用户会去选它、然后标定必失败）。
    # 判据放在这里而不是扫完再改：`probe_passive` / `probe_active` 都靠
    # `family == gnss` 筛候选，漏进去一个假口，它的"最高分"会顶掉真口。
    if set(counts) == {'ubx'} and _ubx_frames_are_heartbeat(ubx_frames):
        return {'family': FAMILY_NOISE, 'class': '', **_vendor_slice(None),
                'features': [], 'counts': {}, 'bytes': len(raw),
                'note': 'UBX 帧内容不像 GNSS 报文（透传/蓝牙桥的私有心跳），按非 GNSS 处理'}

    # 排序：有帧结构/校验位的排前面，私有命令回显垫底。
    order = ['rtcm', 'nmea', 'ubx', 'sbf', 'sirf', 'ashtech', 'novatel', 'trimble',
             'unicore', 'pq', 'nmea_qx', 'nmea_priv']
    features = sorted(counts, key=lambda k: (order.index(k) if k in order else 99))

    # 厂牌判定：**只有 RTCM 时也要试** —— 1033 里写着接收机型号，
    # 而基准站模式恰恰是"只有 RTCM、没有 NMEA"的典型场景。
    descriptor = _rtcm_descriptor(raw) if counts.get('rtcm') else None
    vendor_info = _resolve_vendor(raw, counts, private, ascii_vendor, descriptor)
    vendor = vendor_info['vendor']

    # **RTK 的判据只有一条：校验通过的 RTCM 帧。** 其余一切特征
    # （NMEA / UBX / 私有协议）只证明"这是台 GNSS 设备"。
    klass = CLASS_RTK if counts.get('rtcm') else CLASS_GNSS
    note = '读' + '、'.join(FEATURE_LABEL[f] for f in features)
    if vendor:
        note += f'；厂牌 {vendor}'
    note += '；判定为 RTK 模块' if klass == CLASS_RTK else '；未读到差分数据，仅判为 GNSS 模块'
    return {'family': FAMILY_GNSS, 'class': klass, 'vendor': vendor,
            'vendor_confidence': vendor_info['vendor_confidence'],
            'vendor_evidence': vendor_info['vendor_evidence'],
            'model': vendor_info['model'], 'firmware': vendor_info['firmware'],
            'features': features, 'counts': counts, 'bytes': len(raw), 'note': note}


def _verify_nmea(buf):
    """数**校验和正确**的 NMEA 语句。

    NMEA 每句 `$body*HH`，HH 是 body 逐字节 XOR。这个校验极便宜，
    但只要波特率错一个档位，字节就错位，XOR 永远对不上 ——
    所以它是"这条流真的被正确采样了"的最硬证据。
    真机实测：LG290P@115200 → 47/47 全过；@230400/@57600/@19200 → 0 条成形。

    **`$P` 开头的厂商私有语句不计入这里**（`$PUBX` / `$PMTK` / `$PASHR` …）。
    两类语句的校验算法一样，但含义差得远：标准 NMEA 是"GNSS 定位输出"，
    私有语句可能只是厂家的命令回显。混在一起数，`detail` 会报出
    "NMEA 校验通过 5 句"这种让人以为看到 5 条定位数据的结论。
    私有语句由 `_private_nmea_talkers()` 单独统计。
    """
    ok = 0
    seen = 0
    for line in buf.split(b'\r\n'):
        if not line.startswith(b'$') or line.startswith(b'$P'):
            continue
        star = line.rfind(b'*')
        if star < 0 or star + 3 > len(line) + 1 or len(line) < 8:
            continue
        try:
            given = int(line[star + 1:star + 3], 16)
        except ValueError:
            continue
        seen += 1
        checksum = 0
        for byte in line[1:star]:
            checksum ^= byte
        if checksum == given:
            ok += 1
    return ok, seen


def _verify_ubx(buf):
    """数**校验和正确**的 UBX 帧（Fletcher-like：ck_a / ck_b）。

    只看 `B5 62` 是绝对不够的：u-blox 固件（HPG 1.32 起）默认开启
    波特率自动探测，一条错档位的查询它照样会回一个格式完美的 MON-VER ——
    真机实测 F9P 在 9600/38400/230400/921600 四个档位都能回出
    length=220 的 MON-VER。所以"能回包"不能用来定波特率。

    真正的判据在帧的**内容**上：见 `_ubx_is_probe_reply()`。
    这里只负责把校验通过的帧挑出来。
    """
    frames = []
    index = 0
    while True:
        index = buf.find(b'\xb5\x62', index)
        if index < 0 or index + 8 > len(buf):
            break
        length = buf[index + 4] | (buf[index + 5] << 8)
        end = index + 8 + length
        if end <= len(buf):
            ck_a = ck_b = 0
            for byte in buf[index + 2:end - 2]:
                ck_a = (ck_a + byte) & 0xFF
                ck_b = (ck_b + ck_a) & 0xFF
            if ck_a == buf[end - 2] and ck_b == buf[end - 1]:
                frames.append((buf[index + 2], buf[index + 3], buf[index + 6:end - 2]))
        index += 2
    return frames


# 我们主动发出的查询 → 期望的应答功能域。第二个元素是"这确实是应答"的强证据。
#
# 为什么要这张表：单看 UBX 帧存在与否分不出波特率（见 `_verify_ubx` 注释）。
# 但"我发了 UBX-MON-VER 查询，回来的恰好是 class=0A id=04"这件事，
# 只有真的听懂了我那句话的模块才做得到。所以主动探测的档位，
# 判定依据从"有没有帧"升级成"有没有对答"。
_UBX_QUERY = b'\xb5\x62\x0a\x04\x00\x00\x0e\x34'          # MON-VER 查询
_UBX_MONVER = (0x0A, 0x04)                               # 版本应答
_UBX_ACK = {(0x05, 0x01), (0x05, 0x00)}                  # ACK-ACK / ACK-NAK


def _is_uart_loopback(raw, frames):
    """读到的是**我们自己写出去的东西**（串口回环）吗？

    ## 为什么必须有这条判定

    真机 COM12（`1546:01A9` USB 串行设备，实为 ZED-F9P）在**高波特率**下
    偶尔会把主机写进去的字节原样回读。`probe_active` 发的是
    `B5 62 0A 04 00 00 0E 34`，回环送回来的就是这**一模一样的 8 个字节**，
    而 `_verify_ubx()` 会把它当成一条合法的 `0A-04` 帧（长度字段 0，
    Fletcher 校验 `0E 34` 也自洽）—— 于是 `_ubx_is_probe_reply()` 命中，
    直接判 `strict`。**这是自问自答，模块根本没答**。

    ## 判据：只有"完整查询帧"才算回环

    **不能只比前两个字节 `B5 62 0A 04`** —— 这正是 F9P 真实 MON-VER
    应答的帧头（`B5 62 0A 04 DC 00 ...`，载荷 220 字节）。只比类/ID 会把
    **真的应答判成回环**，反过来把好模块拒掉。

    区分点在**长度字段**：
        查询帧   `B5 62 0A 04 00 00 0E 34`   —— 载荷长度 **0**，帧共 8 字节
        应答帧   `B5 62 0A 04 DC 00 ...`     —— 载荷长度 220（0x00DC），帧共 228 字节

    所以这里整帧 8 字节逐字节比对：只有"长度 0 的那条空查询"原样出现，
    才是回环。真实应答长度不是 0，永远命中不了这条规则。

    文本命令（`$PQTMVERNO*58` / `version`）同理按完整原文比对：
    模块回的是 `$PQTMVERNO,<版本>*XX` 这种**不同**内容，不会命中。
    """
    if not raw:
        return False
    for _label, payload in passive_probes():
        if payload.startswith(b'\xb5\x62'):
            # 整帧逐字节比对（含长度字段 0 和校验）。真实应答长度非 0，撞不上。
            if raw.find(payload) >= 0:
                return True
        else:
            body = payload.strip()
            if body and raw.find(body) >= 0:
                return True
    return False


def _ubx_is_probe_reply(frames):
    """返回 (是否对答成功, 说明)。

    **调用方必须先排除回环**（见 `_is_uart_loopback()`）：本函数只看
    "流里有没有 0A-04 帧"，分不出这帧是模块答的还是自己写的。
    """
    for cls, msg_id, payload in frames:
        if (cls, msg_id) == _UBX_MONVER:
            text = payload[:30].decode('ascii', 'ignore').strip('\x00')
            return True, f'UBX-MON-VER 应答（{text or "无版本串"}）'
        if (cls, msg_id) in _UBX_ACK:
            return True, 'UBX 查询被确认（ACK）'
    return False, ''


def _ubx_frames_are_heartbeat(frames):
    """帧全是"帧头自洽、但内容不是 GNSS 报文"的心跳/私有协议吗？

    真机实测 COM12（USB 串口设备 VID 1546:01A9，实为蓝牙/透传桥）：
    它在 **所有波特率** 下都稳定吐 `B5 62 01 3B 28 00 ...`，CRC 也对得上，
    于是一路被判成 `gnss / strict / UBX 校验通过 1 帧（01-3B）`。
    实际上 `0x01 0x3B` 是 u-blox 的 **UBX-NAV-ORB**（星历轨道信息），
    而且它的载荷是**逐字节递增的内存指针值**（0x0012B050 → 0x0012B438 …），
    是固件里的一个定时器/计数器被直接当载荷发出来了，不是任何 GNSS 消息。

    **判定依据是"功能域合不合理"，不是"帧完不完整"** —— 后者对假帧毫无区分度。
    两块证据任一成立即认定为非 GNSS：

      1. 出现的功能域全部落在 u-blox 公开文档里不存在的组合上
         （`0x01 0x3B` 在 F9P/M9N/M10 的接口描述里都没有）；
      2. 载荷里出现明显的"指针/计数器"特征：同一位置的值单调递增，
         且高位恒定（真实 GNSS 报文不会有这种形态）。

    这条**只影响"这个口像不像 GNSS 模块"的判定**，不碰任何驱动逻辑：
    真的 u-blox 模块在 `probe_active` 里会因为回了 MON-VER 而走
    `reply` 分支，根本不会走到这里。
    """
    if not frames:
        return False
    # 只认 u-blox 公开文档里确实存在的功能域。/ 这些是 GNSS 模块的常规输出。
    known = {
        (0x01, 0x01), (0x01, 0x02), (0x01, 0x03), (0x01, 0x04), (0x01, 0x05),
        (0x01, 0x06), (0x01, 0x07), (0x01, 0x10), (0x01, 0x11), (0x01, 0x12),
        (0x01, 0x13), (0x01, 0x14), (0x01, 0x20), (0x01, 0x21), (0x01, 0x22),
        (0x01, 0x23), (0x01, 0x24), (0x01, 0x25), (0x01, 0x26), (0x01, 0x27),
        (0x01, 0x28), (0x01, 0x30), (0x01, 0x31), (0x01, 0x32), (0x01, 0x34),
        (0x01, 0x35), (0x01, 0x36), (0x01, 0x39), (0x02, 0x10), (0x02, 0x13),
        (0x02, 0x14), (0x02, 0x15), (0x02, 0x32), (0x02, 0x35), (0x02, 0x36),
        (0x02, 0x41), (0x04, 0x10), (0x04, 0x11), (0x04, 0x13), (0x04, 0x14),
        (0x04, 0x24), (0x04, 0x25), (0x04, 0x26), (0x04, 0x31), (0x05, 0x00),
        (0x05, 0x01), (0x06, 0x04), (0x06, 0x13), (0x06, 0x24), (0x06, 0x31),
        (0x06, 0x8A), (0x06, 0x8B), (0x06, 0x8D), (0x09, 0x14), (0x09, 0x15),
        (0x0A, 0x04), (0x0A, 0x06), (0x0A, 0x09), (0x0A, 0x0B), (0x0A, 0x0D),
        (0x0D, 0x01), (0x0D, 0x03), (0x10, 0x00), (0x10, 0x02), (0x10, 0x03),
        (0x10, 0x09), (0x13, 0x40), (0x13, 0x60), (0x21, 0x01), (0x21, 0x02),
        (0x21, 0x03), (0x27, 0x01), (0x27, 0x02), (0x27, 0x03), (0x28, 0x01),
        (0x28, 0x02), (0x28, 0x03), (0x28, 0x05), (0x28, 0x06),
    }
    if all((cls, msg_id) not in known for cls, msg_id, _p in frames):
        return True
    # 证据 2：载荷里出现"单调递增、高位恒定"的指针/计数器。
    for _cls, _msg_id, payload in frames:
        for width in (4, 2):
            if len(payload) < width * 2:
                continue
            prev = None
            rising = 0
            for off in range(0, len(payload) - width + 1, width):
                value = int.from_bytes(payload[off:off + width], 'little')
                if prev is not None and value > prev:
                    rising += 1
                prev = value
            if rising >= 2 and _hi_bits_stable(payload, width):
                return True
    return False


def _hi_bits_stable(payload, width):
    """载荷按 `width` 切分后，高字节是否全部相同（指针的典型形态）。"""
    chunks = [payload[off:off + width] for off in range(0, len(payload) - width + 1, width)]
    if len(chunks) < 3:
        return False
    highs = {c[width - 1] for c in chunks}
    return len(highs) == 1


def verify(raw, active=False):
    """第二层判定：帧完整性。

    返回 dict：
        verdict    'strict'（有校验通过的帧）/ 'weak'（有特征但没验成）/ 'none'
        strict     各特征"校验通过"的帧数
        detail     一句人话，解释为什么这么判
        reply      主动模式下，模块有没有对上我们的询问

    **这一层照样不下"是/不是"的结论**：`weak` 不等于"不是模块"，
    只说明"看到了 GNSS 的味道，但没能验实"。现场有些板子的输出
    是私有二进制，本来就进不了校验表，如实报 `weak` 比硬判成噪声好。
    """
    raw = raw or b''
    strict = {}
    nmea_ok, nmea_seen = _verify_nmea(raw)
    if nmea_ok:
        strict['nmea'] = nmea_ok

    frames = _verify_ubx(raw)
    ubx_is_gnss = bool(frames) and not _ubx_frames_are_heartbeat(frames)
    if ubx_is_gnss:
        strict['ubx'] = len(frames)

    rtcm = _count_rtcm_frames(raw)
    if rtcm:
        strict['rtcm'] = rtcm

    # 厂商私有 NMEA 有校验和，能验实 —— 但**只证明"这是 GNSS 设备"**，
    # 不能升格成 RTK（`$PMTK` 那一族大量出现在单点定位模块上）。
    private = _private_nmea_talkers(raw)
    ashtech = private.pop(_ASHTECH_TALKER, 0)
    if ashtech:
        strict['ashtech'] = ashtech
    priv_total = sum(private.values())
    if priv_total:
        strict['nmea_priv'] = priv_total

    # 二进制私有协议（SBF / NovAtel / TSIP）只做了结构校验、
    # 没有可独立验证的校验位，因此**只能算弱证据**，不进 `strict`。
    # 把它们记在这里是为了 detail 能说出"看到了什么"，而不是干巴巴一句"没验出帧"。
    structural = {}
    for key, value in (('sbf', _count_sbf(raw)),
                       ('novatel', _count_novatel(raw)),
                       ('trimble', _count_tsip(raw)),
                       ('sirf', _count_sirf(raw))):
        if value:
            structural[key] = value

    # 私有 ASCII 协议没有可独立校验的校验位，只能算"弱证据"。
    # `#` 日志头要按厂归因：Unicore 与 NovAtel 格式雷同，混算会让
    # NovAtel 的流被记成和芯星通（进而影响 `_score` 的帧数比较）。
    ascii_vendor, ascii_logs, _first = _ascii_log_vendor(raw)
    if ascii_logs:
        if ascii_vendor == 'NovAtel':
            structural.setdefault('novatel', ascii_logs)
        else:
            strict.setdefault('unicore', ascii_logs)
    if _UNICORE_CMD_RX.search(raw):
        strict.setdefault('unicore', 1)
    if _QUECTEL_RX.search(raw):
        strict.setdefault('pq', 1)

    reply = None
    loopback = False
    if active:
        # **回环优先于对答**：自己写出去的字节被读回来，帧头帧尾全对，
        # 光看帧分不出是模块答的还是自己写的。必须先判回环（真机 COM12 在
        # 921600 档就是这样伪造出一条 0A-04，把整个扫描结论带偏了）。
        loopback = _is_uart_loopback(raw, frames)
        if not loopback:
            ok, text = _ubx_is_probe_reply(frames)
            if ok:
                reply = text
                strict.setdefault('ubx', len(frames))

    if loopback:
        return {'verdict': 'weak', 'strict': {}, 'reply': None, 'loopback': True,
                'detail': '读到的是本次探测自己写出去的字节（串口回环），'
                          '不是模块应答'}
    if not raw:
        return {'verdict': 'none', 'strict': {}, 'detail': '没有读到任何字节',
                'reply': reply}
    if reply:
        return {'verdict': 'strict', 'strict': strict, 'reply': reply,
                'detail': f'主动询问得到对答：{reply}'}
    if nmea_ok:
        return {'verdict': 'strict', 'strict': strict, 'reply': reply,
                'detail': f'NMEA 校验通过 {nmea_ok}/{nmea_seen} 句'}
    if ubx_is_gnss:
        cls = '、'.join(f'{c:02X}-{i:02X}' for c, i, _p in frames[:3])
        return {'verdict': 'strict', 'strict': strict, 'reply': reply,
                'detail': f'UBX 校验通过 {len(frames)} 帧（{cls}）'}
    if rtcm:
        return {'verdict': 'strict', 'strict': strict, 'reply': reply,
                'detail': f'RTCM 帧 CRC 校验通过 {rtcm} 帧'}
    if priv_total or ashtech:
        parts = []
        if ashtech:
            parts.append(f'Ashtech/Hemisphere 定位语句 {ashtech} 句')
        if priv_total:
            names = '、'.join(sorted(t.decode('ascii', 'ignore') for t in private))
            parts.append(f'厂商私有 NMEA {priv_total} 句（{names}）')
        return {'verdict': 'strict', 'strict': strict, 'reply': reply,
                'detail': '校验和通过：' + '；'.join(parts)}
    if structural:
        names = '、'.join(FEATURE_LABEL[k] for k in structural)
        return {'verdict': 'weak', 'strict': strict, 'reply': reply,
                'detail': f'识别到 {names} 的完整帧结构，但该协议没有可独立校验的校验位'}
    if frames:
        cls = '、'.join(f'{c:02X}-{i:02X}' for c, i, _p in frames[:3])
        return {'verdict': 'weak', 'strict': strict, 'reply': reply,
                'detail': f'有 UBX 帧但内容不像 GNSS 报文（{cls}），可能是透传/蓝牙桥'}
    if strict:
        return {'verdict': 'weak', 'strict': strict, 'reply': reply,
                'detail': '有私有协议回显，但没有可独立校验的帧'}
    return {'verdict': 'weak', 'strict': {}, 'reply': reply,
            'detail': '有字节但没能验出完整帧'}


def _listen(port, baud, window):
    """以指定波特率打开串口，**只读**一段时间，返回读到的字节。

    打开失败（口被占用 / 不存在 / 无权限）返回 None，与"读到了 0 字节"区分开：
    前者是"这个口现在轮不到我们看"，后者是"能看，但对方没说话"。
    """
    import serial
    handle = None
    try:
        handle = serial.Serial(port=port, baudrate=int(baud), timeout=0.05,
                               write_timeout=0.5, rtscts=False, dsrdtr=False,
                               xonxoff=False)
        handle.reset_input_buffer()
        deadline = time.time() + window
        chunks = []
        while time.time() < deadline:
            try:
                chunk = handle.read(512)
            except Exception as e:                # noqa: BLE001 - 读异常按"没读到"处理
                logging.debug(f'{port}@{baud} 读串口异常：{e}')
                break
            if chunk:
                chunks.append(chunk)
            else:
                time.sleep(0.02)
        return b''.join(chunks)
    except Exception as e:                        # noqa: BLE001 - 串口异常种类极杂
        logging.debug(f'{port}@{baud} 打开失败：{e}')
        return None
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:                     # noqa: BLE001
                pass


def _ask(port, baud, window, probes):
    """主动发查询再读。`probes` 是 [(标签, 字节串), ...]，逐条发、每条后读一小段。"""
    import serial
    handle = None
    try:
        handle = serial.Serial(port=port, baudrate=int(baud), timeout=0.05,
                               write_timeout=0.5, rtscts=False, dsrdtr=False,
                               xonxoff=False)
        handle.reset_input_buffer()
        chunks = []
        for _label, payload in probes:
            try:
                handle.write(payload)
                handle.flush()
            except Exception as e:                # noqa: BLE001
                logging.debug(f'{port}@{baud} 发送探测失败：{e}')
                break
            deadline = time.time() + window / max(1, len(probes))
            while time.time() < deadline:
                try:
                    chunk = handle.read(512)
                except Exception:                 # noqa: BLE001
                    break
                if chunk:
                    chunks.append(chunk)
                else:
                    time.sleep(0.02)
        return b''.join(chunks)
    except Exception as e:                        # noqa: BLE001
        logging.debug(f'{port}@{baud} 打开失败：{e}')
        return None
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:                     # noqa: BLE001
                pass


def passive_probes():
    """主动探测用的最小查询集。

    三种协议各发一条**最便宜、无副作用**的查询：
        NMEA/PQTM  `$PQTMVERNO*58`        读版本（Quectel 全系）
        UBX        UBX-MON-VER 查询       读版本（u-blox）
        ASCII      `version\\r\\n`           读版本（UniCore N4 简化 ASCII）
    三条加起来 26 字节。都是"读"，没有任何一条会改配置 ——
    扫一个不属于我们的口，最坏的结果就是对方回了句看不懂。
    """
    ubx_mon_ver = bytes([0xB5, 0x62, 0x0A, 0x04, 0x00, 0x00, 0x0E, 0x34])
    return (
        ('quectel', b'$PQTMVERNO*58\r\n'),
        ('ublox', ubx_mon_ver),
        ('unicore', b'version\r\n'),
    )


def _score(info, verified):
    """给一档打分。分数只用来**排序**，不用来做"是/不是"的结论。

    排序的依据来自真机：
      - `strict`（校验通过）永远压过 `weak`。波特率对了才验得实，
        错了只能靠两字节帧头撞大运。
      - 同为 strict 时**先看有没有 RTCM**：差分数据只会在正确档位出现，
        而且它是"这块是 RTK 模块"的唯一证据；比帧数更能说明问题。
      - 再看"验实的帧数"。F9P 在错档位也回帧，但正确档位
        会多回一条 48 字节的 NAV-PVT（那是它自己的定位输出，错档位没有）。
      - 都没有时退回到读到的字节量：乱码流在荒谬的高波特率下会灌进来
        一大堆字节，这反而是"档位不对"的信号，所以字节量只作最后兜底。
    """
    if not info or not info.get('bytes'):
        return (0, 0, 0, 0)
    strict = (verified or {}).get('strict') or {}
    frames = sum(strict.values())
    tier = 2 if (verified or {}).get('verdict') == 'strict' else 1
    differential = 1 if strict.get('rtcm') else 0
    return (tier, differential, frames, info['bytes'])


def probe_identity(port, baud, window=ACTIVE_WINDOW):
    """在**已知波特率**上补发一次"报名字"查询，只为认厂牌/型号。

    为什么需要这一步（真机实测得出的）：
    基准站模式下模块通常关掉 NMEA、只播 RTCM，而 **1033 不一定会开**
    —— 实测本机 LG290P 的基准站流里只有 1005 / 1074 / 1084 / 1094 /
    1114 / 1124，一条 1033 都没有。这时按数据特征判，厂牌永远是空，
    用户看到的「厂家」列只能显示破折号。

    但模块的命令接口在基准站模式下照样活着：发一条 `$PQTMVERNO` /
    UBX-MON-VER / `version`，它会回自己的型号与固件。这三条都是**读**查询，
    不改任何配置 —— 和 `probe_active` 用的是同一组探针。

    返回厂牌字段（`vendor` / `vendor_confidence` / `vendor_evidence` /
    `model` / `firmware`），认不出就是空串。**绝不改 family/class** ——
    这一步只回答"谁家的"，不回答"是不是 RTK"。
    """
    raw = _ask(port, baud, window, passive_probes())
    if not raw:
        return _vendor_slice(None)
    info = _vendor_slice(sniff(raw))
    # 证据措辞要交代"这条是怎么来的"：这里的 UBX 帧是我们**问**出来的应答，
    # 不是模块自己在播。直接复用 `sniff()` 的证据会写成"UBX 二进制帧（1 帧）"，
    # 用户会以为这块模块平时就在吐 UBX —— 而他可能正是因为"没看到 UBX"才来查。
    if info.get('vendor'):
        info['vendor_evidence'] = ['主动版本查询的应答：' + str(item)
                                   for item in (info.get('vendor_evidence') or [])]
    return info


def probe_passive(port, bauds=None, window=PASSIVE_WINDOW, on_progress=None):
    """**纯读**扫描：逐档打开、听一会儿，看有没有特征字节。

    **不再"遇到第一个 GNSS 档位就停"。** 这条策略在真机上给的都是错答案：
    从高到低扫，930000 档位的噪声里出现一对自洽的 `B5 62` 就会先撞线，
    真正的 38400 还没轮到就已经返回了。现在的做法是**全扫一遍再排序**，
    把"最像"的那档作为 `baud` 报上去，其余档位连同证据一起放进 `tried`。

    时间代价：9 档 × 0.35 秒 ≈ 3.2 秒/口。这是可接受的 ——
    扫错了波特率，后面整条标定流程都要重来，代价远大于多等 3 秒。

    返回 dict：
        port / baud / family / class / vendor / features / counts / bytes / tried / note
        verdict / detail     第二层（帧完整性）的结论
        confidence           'high' / 'medium' / 'low'，给前端决定要不要提示复核
    `baud` 为 None 表示所有档位都没读到特征。
    """
    tried = []
    for baud in (bauds or SCAN_BAUDS):
        if on_progress:
            on_progress({'port': port, 'baud': baud, 'phase': 'passive'})
        raw = _listen(port, baud, window)
        if raw is None:
            # 打不开：这个口现在不归我们（被主程序占着 / 不存在）。
            # 后面几档也不用试了 —— 同一个口打不开一次，九次都一样。
            tried.append({'baud': baud, 'bytes': None, 'family': 'busy'})
            return {'port': port, 'baud': None, 'family': 'busy', 'class': '',
                    **_vendor_slice(None), 'features': [],
                    'counts': {}, 'bytes': 0, 'tried': tried,
                    'verdict': 'none', 'detail': '', 'confidence': 'low',
                    'note': '串口打不开（可能正被数据面占用）'}
        info = sniff(raw)
        verified = verify(raw)
        tried.append({'baud': baud, 'bytes': len(raw), 'family': info['family'],
                      'class': info.get('class', ''), **_vendor_slice(info),
                      'features': info['features'], 'counts': info['counts'],
                      'verdict': verified['verdict'], 'detail': verified['detail'],
                      'strict': verified['strict'],
                      'score': _score(info, verified)})

    candidates = [item for item in tried if item['family'] == FAMILY_GNSS]
    if candidates:
        best = max(candidates, key=lambda item: item.get('score') or (0, 0, 0, 0))
        others = [item['baud'] for item in candidates if item['baud'] != best['baud']]
        confidence = 'high' if best['verdict'] == 'strict' else 'medium'
        # 有别的档位也叫 gnss：`confidence` 降级，前端会提示"建议复核"。
        # 不是错，是事实 —— 如实说比悄悄替你选好。
        if others and confidence == 'high' and len(others) >= 3:
            confidence = 'medium'
        note = best['detail'] or ''
        if others:
            note += f'；另有 {len(others)} 档也读到特征（{"、".join(str(b) for b in others)}）'
        return {'port': port, 'baud': best['baud'], 'family': FAMILY_GNSS,
                'class': best.get('class', ''), **_vendor_slice(best),
                'features': best['features'], 'counts': best['counts'],
                'bytes': best['bytes'], 'tried': tried,
                'verdict': best['verdict'], 'detail': best['detail'],
                'confidence': confidence, 'note': note}

    # 一档都没读到特征：把"最好"的那一档（读到的字节最多）报上去，
    # 让前端能显示"收到过 137 字节但认不出"，而不是一个干巴巴的"无"。
    best = max(tried, key=lambda item: item.get('bytes') or 0) if tried else None
    return {'port': port, 'baud': None,
            'family': (best or {}).get('family') or FAMILY_UNKNOWN,
            'class': '', **_vendor_slice(None),
            'features': [], 'counts': {},
            'bytes': (best or {}).get('bytes') or 0, 'tried': tried,
            'verdict': (best or {}).get('verdict') or 'none',
            'detail': (best or {}).get('detail') or '',
            'confidence': 'low',
            'note': '各档位都没有读到 GNSS 特征' if tried else '没有可试的波特率'}


def probe_active(port, bauds=None, window=ACTIVE_WINDOW, on_progress=None):
    """主动扫描：被动读不到时，发最小查询把波特率问出来。

    比 `probe_passive` 慢（每档 1.2 秒 vs 0.35 秒），但能覆盖
    "模块输出关掉了 / 只在被问时才说话"的情况（基准站模式关掉 NMEA 的板子）。

    **这一层的判定依据比被动层强得多**：不是"有没有数据"，而是
    "我发出去的查询，它有没有回一个对得上的应答"。
    真机实测 F9P 在每一个档位都能回格式完美的 MON-VER（固件自带波特率探测），
    所以这里不能靠"有没有 UBX 帧"定档 —— 这条路上没有区分度。
    有区分度的是驱动的默认波特率：`probe_active` 因此把
    "与驱动默认波特率一致"作为平手时的加分项。

    和被动扫描一样：**全扫一遍再排序**，不在首个命中处返回。
    """
    probes = passive_probes()
    tried = []
    for baud in (bauds or SCAN_BAUDS):
        if on_progress:
            on_progress({'port': port, 'baud': baud, 'phase': 'active'})
        raw = _ask(port, baud, window, probes)
        if raw is None:
            tried.append({'baud': baud, 'bytes': None, 'family': 'busy'})
            return {'port': port, 'baud': None, 'family': 'busy', 'class': '',
                    **_vendor_slice(None), 'features': [],
                    'counts': {}, 'bytes': 0, 'tried': tried, 'active': True,
                    'verdict': 'none', 'detail': '', 'confidence': 'low',
                    'note': '串口打不开（可能正被数据面占用）'}
        info = sniff(raw)
        verified = verify(raw, active=True)
        # 回环档位**必须**打成 noise：它读到的字节数很大（本轮真机 4512B 全是
        # 自己写出去的东西），如果留在 `FAMILY_GNSS` 里，`_score()` 最后那档
        # "字节量兜底"会把它推成最佳档位。回环恰恰说明这一档**根本不通**。
        loopback = verified.get('loopback', False)
        family = FAMILY_NOISE if loopback else info['family']
        tried.append({'baud': baud, 'bytes': len(raw), 'family': family,
                      'class': '' if loopback else info.get('class', ''),
                      **_vendor_slice(info, empty=loopback),
                      'features': [] if loopback else info['features'],
                      'counts': {} if loopback else info['counts'],
                      'verdict': verified['verdict'], 'reply': verified['reply'],
                      'loopback': loopback,
                      'detail': verified['detail'], 'strict': verified['strict'],
                      'score': (0, 0, 0, 0) if loopback
                               else _score(info, verified)})

    candidates = [item for item in tried if item['family'] == FAMILY_GNSS]
    if candidates:
        best = max(candidates, key=lambda item: item.get('score') or (0, 0, 0, 0))
        others = [item['baud'] for item in candidates if item['baud'] != best['baud']]
        note = best['detail'] or ''
        if others:
            note += f'；另有 {len(others)} 档也有应答（{"、".join(str(b) for b in others)}）'
            note += '，波特率以驱动默认档为准，请以实际连线为准'
        return {'port': port, 'baud': best['baud'], 'family': FAMILY_GNSS,
                'class': best.get('class', ''), **_vendor_slice(best),
                'features': best['features'], 'counts': best['counts'],
                'bytes': best['bytes'], 'tried': tried, 'active': True,
                'verdict': best['verdict'], 'detail': best['detail'],
                # 多个档位都能回包时，这一层的区分度已经用尽 ——
                # 把 confidence 标成 low，让前端明确提示用户核对波特率。
                'confidence': 'high' if not others else 'low',
                'note': note}

    best = max(tried, key=lambda item: item.get('bytes') or 0) if tried else None
    # 如果"最好"的那一档是回环，说明这个口**所有档位都没有真正的模块**，
    # 而最佳档位的高字节量纯粹是自己写出去的东西 —— 必须如实说，
    # 否则用户看到 4512 字节会以为"有数据流、只是没认出来"。
    loopbacks = [item['baud'] for item in tried if item.get('loopback')]
    note = '主动询问各档位均无应答'
    if loopbacks and best is not None and best.get('loopback'):
        note = (f'各档位均为串口回环（{"、".join(str(b) for b in loopbacks)} 档读到的'
                f'是探测帧自己），没有真实模块应答')
    return {'port': port, 'baud': None, 'family': FAMILY_NOISE, 'class': '',
            **_vendor_slice(None), 'features': [],
            'counts': {}, 'bytes': (best or {}).get('bytes') or 0, 'tried': tried,
            'active': True, 'verdict': 'none',
            'detail': (best or {}).get('detail') or '', 'confidence': 'low',
            'note': note}


def list_ports():
    """列出系统所有串口，含 VID/PID 与描述（判定用的旁证，不做决定）。

    顺序按端口名自然排序（COM2 在 COM10 前面），否则 COM3 会排到 COM12 之后。

    额外派生一个 `kind`：
        'usb-cdc'  USB CDC 虚拟串口（VID:PID 里带 `1546:01A9` 这类模块厂 ID，
                   或者描述/硬件 ID 能看出是 CDC）—— **这一档没有真实波特率**，
                   速率由 USB 主机侧协商，串口驱动的 `baudrate` 参数被内核忽略。
        'bridge'   USB 转串口芯片（CH340 / CP210x / FTDI / PL2303），
                   后面接的是模块的真 UART，**波特率是有意义的**。
        'uart'     主板自带 / 未知

    为什么这个字段必须存在：真机上 F9P 挂在 COM12（`1546:01A9`，USB CDC），
    它在**所有**波特率下都能正常收发 —— 因为压根没有波特率这回事。
    扫描器如果不认这件事，就会把"每档都能收到数据"当成"波特率识别失败"，
    然后挑一个根本不存在的档位报给用户。见 `_rate_free()`。
    """
    import serial.tools.list_ports
    items = []
    for info in serial.tools.list_ports.comports():
        vid = f'{info.vid:04X}' if getattr(info, 'vid', None) is not None else ''
        pid = f'{info.pid:04X}' if getattr(info, 'pid', None) is not None else ''
        hwid = info.hwid or ''
        items.append({
            'device': info.device,
            'description': info.description or '',
            'hwid': hwid,
            'vid': vid,
            'pid': pid,
            'serial_number': getattr(info, 'serial_number', '') or '',
            'manufacturer': getattr(info, 'manufacturer', '') or '',
            'kind': _port_kind(info.description or '', hwid, vid, pid),
        })

    def natural(item):
        text = item['device'] or ''
        digits = re.sub(r'\D', '', text)
        return (re.sub(r'\d+', '', text), int(digits) if digits else 0)

    items.sort(key=natural)
    return items


# USB 转串口桥的厂牌 VID（这些后面接的是模块真 UART，波特率有意义）
_BRIDGE_VIDS = {'1A86', '10C4', '0403', '067B', '1CBE'}
# 已知 GNSS 模块自称的 USB VID（u-blox / Quectel / Unicore / Septentrio ...）。
# 这几个 VID 出现在串口列表里，几乎可以肯定是模块自带的 USB CDC 口。
_MODULE_VIDS = {'1546', '2C7C', '0483', '152A'}


def _port_kind(description, hwid, vid, pid):
    text = f'{description} {hwid}'.upper()
    if vid in _BRIDGE_VIDS or any(tag in text for tag in
                                  ('CH340', 'CH341', 'CP210', 'FTDI', 'FT232',
                                   'PL2303', 'USB-SERIAL')):
        return 'bridge'
    if vid in _MODULE_VIDS or 'CDC' in text or 'ACM' in text:
        return 'usb-cdc'
    return 'uart'


def _rate_free(port_kind):
    """这个口上"波特率"是不是个无意义的参数。

    USB CDC 虚拟串口就是这样：`serial.Serial(baudrate=...)` 传进去什么
    内核都不管，实际速率由 USB 协商。所以在这类口上问"哪个波特率对"
    本身就是个错问题 —— 真机实测 F9P 在 9600~921600 每一档都能收能发，
    而且**每档的字节数完全一样**（都是它自己的 2 Hz 输出）。
    """
    return port_kind == 'usb-cdc'


def scan(ports=None, bauds=None, active=True, on_progress=None):
    """扫一批串口，返回每个口的判定结果。

    `active=True` 时：被动扫不到的口再走一遍主动询问。
    （默认开：现场大部分板子在基准站模式下是不说话的，纯被动扫会漏。）

    **返回全部端口，不做过滤。**没有读到特征的也返回，带上 family 与 note，
    由前端决定怎么显示 —— 这正是"只做判定不做决断"。

    对 USB CDC 口额外做一件事：扫完填上 `baud_fixed=False`，
    并提示"该口无波特率概念"。不给一个假答案，比给一个假答案好。
    """
    listing = list_ports()
    kinds = {item['device']: item.get('kind', 'uart') for item in listing}
    targets = ports or [item['device'] for item in listing]
    results = []
    for port in targets:
        if on_progress:
            on_progress({'port': port, 'phase': 'start'})
        kind = kinds.get(port, 'uart')
        item = probe_passive(port, bauds=bauds, on_progress=on_progress)
        if item['baud'] is None and active and item['family'] != 'busy':
            if on_progress:
                on_progress({'port': port, 'phase': 'fallback'})
            item = probe_active(port, bauds=bauds, on_progress=on_progress)

        item['kind'] = kind
        item['baud_fixed'] = not _rate_free(kind)
        # 厂牌还空着、但波特率已经确定 → 补一次只读版本查询。
        #
        # 这一步只对"认不出厂牌"的口做，代价是每口多 1.2 秒；换来的是
        # 基准站模式（只播 RTCM、不开 1033）下也能显示厂家 —— 那恰好是
        # 用户最想知道"这块是谁家的"的时候。已经认出厂牌的（UBX / SBF /
        # 1033 / 私有语句）不重复问，省掉这一秒。
        # `baud_fixed` 不能作为这一枪的前提：USB 直连（CDC）的口没有波特率概念，
        # 但**恰恰是这种口最需要一个厂牌** —— u-blox 的 ZED-F9P 走 USB 时就是
        # 虚拟串口，出厂只播标准 NMEA（无私有语句），被动扫永远认不出是谁家的；
        # 而 UBX-MON-VER 在 CDC 上跟波特率无关，照样一问就回。
        # 2026-11-13 真机实测：COM13 修前 vendor 为空，修后稳定认出 u-blox。
        if not item.get('vendor') and item.get('baud'):
            identity = probe_identity(port, item['baud'])
            if identity.get('vendor'):
                item.update(identity)
                item['note'] = ((item.get('note') or '')
                                + ('；' if item.get('note') else '')
                                + '厂牌来自主动版本查询')
        if _rate_free(kind):
            # 这个口没有"正确波特率"可言。把选出来的档位保留在 `baud` 里
            # （建链时仍要填一个值，随便填一个能通的就行），但明确标注
            # 它不是设备状态，避免用户以为"扫出来就是这个数"。
            # `note` 本身已经以 `detail` 开头（见 `probe_passive` 的组装），
            # 两个都拼会把"NMEA 校验通过 14/14 句"重复一遍。
            extra = item.get('note') or ''
            item['note'] = ('USB 直连（CDC）：该口没有波特率概念，速率由主机侧协商；'
                            '上面显示的数值只是建链时用的占位值。'
                            + (' ' + extra if extra else ''))
            item['confidence'] = 'high' if item['family'] == FAMILY_GNSS else 'low'
        results.append(item)
    return results
