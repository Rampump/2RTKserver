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

# 特征 → 说明。前端原样显示这个列表，让用户自己判断。
FEATURE_LABEL = {
    'nmea': 'NMEA 语句',
    'rtcm': 'RTCM 差分数据',
    'ubx': 'u-blox UBX 二进制',
    'unicore': '和芯星通 ASCII',
    'pq': 'Quectel PQTM/PAIR 命令',
    'novatel': 'NovAtel OEM 二进制',
    'trimble': 'Trimble TSIP 二进制',
}

# NMEA 会话 ID。$GP $GN $BD $GL $GA $GB $GQ $GI $GS
# 只认 GPS/GLONASS/Galileo/北斗/QZSS/NavIC 这几家的组合 —— 不写成 `\$[A-Z]{2}`
# 是因为那样连 `$PMTK`（联发科私有）和 `$PUBX`（u-blox 私有 NMEA）都会被算成
# "NMEA 定位语句"，而那两种恰好是**非 RTK** 的廉价模块常发的。
_NMEA_RX = re.compile(rb'\$(?:GP|GN|GL|GA|GB|GQ|GI|GS|BD|GZ|DT)(GGA|RMC|GSA|GSV|GLL|GNS|VTG|ZDA|GST)')
# 和芯星通：应答以 `#VERSIONA` / `#MODE` 开头，命令回显以 `$command,response:` 开头
_UNICORE_RX = re.compile(rb'(?:#(?:VERSION|MODE|SAT|CONFIG|ANT)[A-Z]*,|\$command,response:)')
# Quectel：PQTM / PAIR 两族
_QUECTEL_RX = re.compile(rb'\$(?:PQTM[AC-Z]|PAIR\d{3})')
# NovAtel OEM6/OEM7：AA 44 12 组帧头
_NOVATEL_RX = re.compile(rb'\xaa\x44\x12')
# Trimble TSIP：DLE(0x10) ETX(0x03) 帧头
_TSIP_RX = re.compile(rb'\x10\x03')


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
    count = 0
    index = 0
    total = len(buf)
    while True:
        index = buf.find(b'\xd3', index)
        if index < 0 or index + 6 > total:
            break
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
            index += 1                         # 帧被截断：这一档没读全，不计入
            continue
        expect = ((buf[end - 3] << 16) | (buf[end - 2] << 8) | buf[end - 1])
        if _crc24q(buf[index:end - 3]) == expect:
            count += 1
            index = end                        # 校验通过才跳过整帧，避免重复数
        else:
            index += 1
    return count


def _count_ubx_frames(buf):
    """数头部自洽的 UBX 帧（B5 62 + class + id + 2 字节长度 ≤ 1024）。"""
    count = 0
    index = 0
    while True:
        index = buf.find(b'\xb5\x62', index)
        if index < 0 or index + 6 > len(buf):
            break
        length = buf[index + 4] | (buf[index + 5] << 8)
        if length <= 1024:
            count += 1
        index += 2
    return count


def sniff(raw):
    """从一段原始字节里认出 GNSS 家族特征。

    返回 dict：
        family     FAMILY_GNSS / FAMILY_UNKNOWN / FAMILY_NOISE
        features   ['nmea', 'rtcm', ...]（按可信度排序）
        counts     各特征的命中次数，给前端显示"读到了多少"
        bytes      收到的字节数

    **判定不等于结论**：`family='gnss'` 只说明"这条流里有 GNSS 数据"，
    不说明是哪块模块、也不说明它能当基准站。选择权在前端/用户手里。
    """
    raw = raw or b''
    counts = {}
    if not raw:
        return {'family': FAMILY_UNKNOWN, 'features': [], 'counts': counts,
                'bytes': 0, 'note': '没有读到任何字节'}

    nmea = len(_NMEA_RX.findall(raw))
    if nmea:
        counts['nmea'] = nmea
    rtcm = _count_rtcm_frames(raw)
    if rtcm:
        counts['rtcm'] = rtcm
    ubx = _count_ubx_frames(raw)
    if ubx:
        counts['ubx'] = ubx
    unicore = len(_UNICORE_RX.findall(raw))
    if unicore:
        counts['unicore'] = unicore
    quectel = len(_QUECTEL_RX.findall(raw))
    if quectel:
        counts['pq'] = quectel
    novatel = len(_NOVATEL_RX.findall(raw))
    if novatel:
        counts['novatel'] = novatel
    tsip = len(_TSIP_RX.findall(raw))
    if tsip:
        counts['trimble'] = tsip

    if not counts:
        # 有字节、但一个特征都没有。**如实说"有数据但认不出"**，
        # 不要说"没有模块"—— 有的私有二进制协议不在上表里。
        return {'family': FAMILY_NOISE, 'features': [], 'counts': {},
                'bytes': len(raw), 'note': '收到数据但不含已知的 GNSS 特征'}

    # **唯一的特征是 UBX，且帧内容不像 GNSS 报文** → 这是透传/蓝牙桥，
    # 不是 GNSS 模块。真机实测 COM12 每个波特率都吐 01-3B 心跳，
    # 若照计数判就会把它当成 GNSS 模块推给前端（用户会去选它、然后标定必失败）。
    # 判据放在这里而不是扫完再改：`probe_passive` / `probe_active` 都靠
    # `family == gnss` 筛候选，漏进去一个假口，它的"最高分"会顶掉真口。
    if set(counts) == {'ubx'} and _ubx_frames_are_heartbeat(_verify_ubx(raw)):
        return {'family': FAMILY_NOISE, 'features': [], 'counts': {},
                'bytes': len(raw),
                'note': 'UBX 帧内容不像 GNSS 报文（透传/蓝牙桥的私有心跳），按非 GNSS 处理'}

    # 排序：RTCM / NMEA / UBX 这三种是"实锤"（有帧结构校验），
    # 私有命令回显次之（可能只是设备在打招呼）。
    order = ['rtcm', 'nmea', 'ubx', 'unicore', 'pq', 'novatel', 'trimble']
    features = sorted(counts, key=lambda k: (order.index(k) if k in order else 99))
    return {'family': FAMILY_GNSS, 'features': features, 'counts': counts,
            'bytes': len(raw), 'note': '读' + '、'.join(FEATURE_LABEL[f] for f in features)}


def _verify_nmea(buf):
    """数**校验和正确**的 NMEA 语句。

    NMEA 每句 `$body*HH`，HH 是 body 逐字节 XOR。这个校验极便宜，
    但只要波特率错一个档位，字节就错位，XOR 永远对不上 ——
    所以它是"这条流真的被正确采样了"的最硬证据。
    真机实测：LG290P@115200 → 47/47 全过；@230400/@57600/@19200 → 0 条成形。
    """
    ok = 0
    seen = 0
    for line in buf.split(b'\r\n'):
        if not line.startswith(b'$'):
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

    # 私有 ASCII 协议没有可独立校验的校验位，只能算"弱证据"。
    for key, rx in (('unicore', _UNICORE_RX), ('pq', _QUECTEL_RX)):
        if rx.search(raw):
            strict.setdefault(key, 1)

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
      - 同为 strict 时比"验实的帧数"。F9P 在错档位也回帧，但正确档位
        会多回一条 48 字节的 NAV-PVT（那是它自己的定位输出，错档位没有）。
      - 都没有时退回到读到的字节量：乱码流在荒谬的高波特率下会灌进来
        一大堆字节，这反而是"档位不对"的信号，所以字节量只作最后兜底。
    """
    if not info or not info.get('bytes'):
        return (0, 0, 0)
    strict = (verified or {}).get('strict') or {}
    frames = sum(strict.values())
    tier = 2 if (verified or {}).get('verdict') == 'strict' else 1
    return (tier, frames, info['bytes'])


def probe_passive(port, bauds=None, window=PASSIVE_WINDOW, on_progress=None):
    """**纯读**扫描：逐档打开、听一会儿，看有没有特征字节。

    **不再"遇到第一个 GNSS 档位就停"。** 这条策略在真机上给的都是错答案：
    从高到低扫，930000 档位的噪声里出现一对自洽的 `B5 62` 就会先撞线，
    真正的 38400 还没轮到就已经返回了。现在的做法是**全扫一遍再排序**，
    把"最像"的那档作为 `baud` 报上去，其余档位连同证据一起放进 `tried`。

    时间代价：9 档 × 0.35 秒 ≈ 3.2 秒/口。这是可接受的 ——
    扫错了波特率，后面整条标定流程都要重来，代价远大于多等 3 秒。

    返回 dict：
        port / baud / family / features / counts / bytes / tried / note
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
            return {'port': port, 'baud': None, 'family': 'busy', 'features': [],
                    'counts': {}, 'bytes': 0, 'tried': tried,
                    'verdict': 'none', 'detail': '', 'confidence': 'low',
                    'note': '串口打不开（可能正被数据面占用）'}
        info = sniff(raw)
        verified = verify(raw)
        tried.append({'baud': baud, 'bytes': len(raw), 'family': info['family'],
                      'features': info['features'], 'counts': info['counts'],
                      'verdict': verified['verdict'], 'detail': verified['detail'],
                      'strict': verified['strict'],
                      'score': _score(info, verified)})

    candidates = [item for item in tried if item['family'] == FAMILY_GNSS]
    if candidates:
        best = max(candidates, key=lambda item: item.get('score') or (0, 0, 0))
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
                'features': best['features'], 'counts': best['counts'],
                'bytes': best['bytes'], 'tried': tried,
                'verdict': best['verdict'], 'detail': best['detail'],
                'confidence': confidence, 'note': note}

    # 一档都没读到特征：把"最好"的那一档（读到的字节最多）报上去，
    # 让前端能显示"收到过 137 字节但认不出"，而不是一个干巴巴的"无"。
    best = max(tried, key=lambda item: item.get('bytes') or 0) if tried else None
    return {'port': port, 'baud': None,
            'family': (best or {}).get('family') or FAMILY_UNKNOWN,
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
            return {'port': port, 'baud': None, 'family': 'busy', 'features': [],
                    'counts': {}, 'bytes': 0, 'tried': tried, 'active': True,
                    'verdict': 'none', 'detail': '', 'confidence': 'low',
                    'note': '串口打不开（可能正被数据面占用）'}
        info = sniff(raw)
        verified = verify(raw, active=True)
        # 回环档位**必须**打成 noise：它读到的字节数很大（本轮真机 4512B 全是
        # 自己写出去的东西），如果留在 `FAMILY_GNSS` 里，`_score()` 的第三档
        # "字节量兜底"会把它推成最佳档位。回环恰恰说明这一档**根本不通**。
        family = FAMILY_NOISE if verified.get('loopback') else info['family']
        tried.append({'baud': baud, 'bytes': len(raw), 'family': family,
                      'features': [] if verified.get('loopback') else info['features'],
                      'counts': {} if verified.get('loopback') else info['counts'],
                      'verdict': verified['verdict'], 'reply': verified['reply'],
                      'loopback': verified.get('loopback', False),
                      'detail': verified['detail'], 'strict': verified['strict'],
                      'score': (0, 0, 0) if verified.get('loopback')
                               else _score(info, verified)})

    candidates = [item for item in tried if item['family'] == FAMILY_GNSS]
    if candidates:
        best = max(candidates, key=lambda item: item.get('score') or (0, 0, 0))
        others = [item['baud'] for item in candidates if item['baud'] != best['baud']]
        note = best['detail'] or ''
        if others:
            note += f'；另有 {len(others)} 档也有应答（{"、".join(str(b) for b in others)}）'
            note += '，波特率以驱动默认档为准，请以实际连线为准'
        return {'port': port, 'baud': best['baud'], 'family': FAMILY_GNSS,
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
    return {'port': port, 'baud': None, 'family': FAMILY_NOISE, 'features': [],
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
        if _rate_free(kind):
            # 这个口没有"正确波特率"可言。把选出来的档位保留在 `baud` 里
            # （建链时仍要填一个值，随便填一个能通的就行），但明确标注
            # 它不是设备状态，避免用户以为"扫出来就是这个数"。
            item['note'] = ('USB 直连（CDC）：该口没有波特率概念，速率由主机侧协商；'
                            '上面显示的数值只是建链时用的占位值。'
                            + (' ' + item.get('detail', '') if item.get('detail') else ''))
            item['confidence'] = 'high' if item['family'] == FAMILY_GNSS else 'low'
        results.append(item)
    return results
