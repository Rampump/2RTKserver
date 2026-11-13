# -*- coding: utf-8 -*-
"""端口扫描器的判定契约（stdlib unittest，零新增依赖）。

这份测试锁的是**"扫描说这块是什么"这件事本身**。它比一般单元测试更要紧：
扫描结果直接决定界面上「判定」那一列写什么，用户照着这一列挑模块。
判宽了 —— 把单点定位模块标成 RTK，用户标定跑到 `FIX_WAIT` 才发现不会差分；
判严了 —— 把真模块漏成噪声，那个口直接从候选里消失，用户不知道该点哪里。

所以这里的用例全部是**合成字节流**：校验和、CRC-24Q、UBX Fletcher 校验
都在测试里现算，不依赖任何真机抓包。判据一旦被改动（比如又回去数
`B5 62` 帧头而不验校验位），这里会立刻红。

跑法：

    python -m unittest tests.test_scanner -v
"""
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from rtkserver.calibration import scanner  # noqa: E402


# --------------------------------------------------------------------------
# 造字节流的小工具。全部按协议现算校验，不写死任何抓包片段。
# --------------------------------------------------------------------------

def nmea(body, talker='GP'):
    """拼一句带正确校验和的 NMEA。`body` 不含 `$` 与 `*HH`。"""
    text = f'{talker}{body}'
    checksum = 0
    for byte in text.encode('ascii'):
        checksum ^= byte
    return f'${text}*{checksum:02X}\r\n'.encode('ascii')


def ubx(cls, msg_id, payload=b''):
    """拼一条 Fletcher 校验正确的 UBX 帧。"""
    body = bytes([cls, msg_id]) + len(payload).to_bytes(2, 'little') + payload
    ck_a = ck_b = 0
    for byte in body:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return b'\xb5\x62' + body + bytes([ck_a, ck_b])


def rtcm(payload, corrupt=False):
    """拼一帧 CRC-24Q 正确的 RTCM3（载荷 ≥12 字节，否则不算合法帧）。"""
    assert len(payload) >= 12
    head = bytes([0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF])
    body = head + payload
    crc = scanner._crc24q(body)
    frame = body + bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])
    if corrupt:
        frame = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    return frame


def sbf(block_id=4027, body_len=16):
    """拼一块结构自洽的 Septentrio SBF（头 8 + 体 + 尾 4）。"""
    return (b'\x24\x40' + b'\x00\x00'
            + block_id.to_bytes(2, 'little')
            + body_len.to_bytes(2, 'little')
            + bytes(body_len) + bytes(4))


def novatel(payload_len=8):
    """拼一帧结构自洽的 NovAtel OEM（头 28 + 载荷 + CRC 4）。"""
    head = (b'\xaa\x44\x12' + bytes([0x1C]) + bytes([0x01, 0x02])
            + bytes([0x01]) + bytes([0x01])
            + payload_len.to_bytes(2, 'little'))
    return head + bytes(28 - len(head)) + bytes(payload_len) + bytes(4)


def tsip(body=b'\x01\x02\x03\x04'):
    """拼一帧结构自洽的 Trimble TSIP（DLE ETX + ID + 长度 + 体 + DLE ETX）。"""
    return (b'\x10\x03' + bytes([0x13])
            + len(body).to_bytes(2, 'big') + body + b'\x10\x03')


class _BitWriter:
    """按位拼 RTCM 载荷。1033 的字符串字段前面挂 8 bit 长度，没法按字节拼。"""

    def __init__(self):
        self.bits = []

    def put(self, value, width):
        for shift in range(width - 1, -1, -1):
            self.bits.append((value >> shift) & 1)

    def text(self, value):
        raw = value.encode('ascii')
        self.put(len(raw), 8)
        for byte in raw:
            self.put(byte, 8)

    def payload(self):
        while len(self.bits) % 8:
            self.bits.append(0)
        out = bytearray()
        for index in range(0, len(self.bits), 8):
            value = 0
            for bit in self.bits[index:index + 8]:
                value = (value << 1) | bit
            out.append(value)
        return bytes(out)


def rtcm_1033(receiver='Quectel  LG290P', antenna='QUECTEL L1L5',
              firmware='1.0', serial='1234', station=290):
    """拼一帧 1033（接收机与天线描述）。字段顺序按 RTCM 10403.3。"""
    writer = _BitWriter()
    writer.put(1033, 12)
    writer.put(station, 12)
    writer.text(antenna)
    writer.put(0, 8)                       # DF031 天线安装方式
    writer.text('ANT-SN')
    writer.text(receiver)
    writer.text(firmware)
    writer.text(serial)
    return rtcm(writer.payload())


def sirf(body=b'\x01\x02\x03', msg_id=0x02):
    """拼一帧校验和正确的 SiRF 二进制（A0 A2 + 长度 + 体 + 校验 + B0 B3）。"""
    payload = bytes([msg_id, 0x00]) + body
    length = len(payload)
    frame = b'\xa0\xa2' + length.to_bytes(2, 'big') + payload
    checksum = sum(frame[2:]) & 0xFFFF
    return frame + checksum.to_bytes(2, 'big') + b'\xb0\xb3'


class SniffTest(unittest.TestCase):
    """第一层：这条流里有没有 GNSS 味道，以及是哪一等。"""

    def test_standard_nmea_is_gnss_but_not_rtk(self):
        """只有 NMEA 的流 = GNSS 模块，**绝不能**判成 RTK。

        这是整套判定的底线：单点定位的 GPS 模块每秒都在发 `$GPGGA`，
        它只能说明"它知道自己在哪"，不能说明"它能厘米级定位"。
        """
        raw = (nmea(',123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,', 'GPGGA')
               + nmea(',123519,A,4807.038,N,01131.000,E,0.5,54.7,120426,,,A', 'GPRMC'))
        info = scanner.sniff(raw)
        self.assertEqual(info['family'], scanner.FAMILY_GNSS)
        self.assertEqual(info['class'], scanner.CLASS_GNSS)
        self.assertEqual(info['features'][0], 'nmea')

    def test_rtcm_promotes_to_rtk(self):
        """RTCM 帧（CRC 通过）是 RTK 的唯一充分证据，排序要压过 NMEA。"""
        raw = nmea(',123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,', 'GPGGA')
        raw += rtcm(bytes(19))
        info = scanner.sniff(raw)
        self.assertEqual(info['class'], scanner.CLASS_RTK)
        self.assertEqual(info['features'][0], 'rtcm',
                         '差分数据必须排第一，它才是"能 RTK"的证据')

    def test_rtcm_only_is_rtk(self):
        """基准站关掉 NMEA 后只剩 RTCM —— 仍然要认成 RTK 模块。"""
        info = scanner.sniff(rtcm(bytes(19)))
        self.assertEqual(info['family'], scanner.FAMILY_GNSS)
        self.assertEqual(info['class'], scanner.CLASS_RTK)

    def test_rtcm_with_bad_crc_is_not_rtk(self):
        """CRC 错一位就不是差分数据。

        真机实测过：错波特率下的乱码里 `D3 xx` 撞中率和长度字段落进
        12~1023 的概率都不低。只靠帧头会把它判成 RTCM，于是整条链路
        被判定"有差分"，实际一个字节都用不了。
        """
        info = scanner.sniff(rtcm(bytes(19), corrupt=True))
        self.assertEqual(info['class'], '')
        self.assertNotIn('rtcm', info['features'])

    def test_private_nmea_is_gnss_not_rtk(self):
        """`$PUBX` 是 u-blox 私有 NMEA —— 认厂牌，但不升格成 RTK。

        这一族里 `$PMTK`（联发科）、`$PSTI`（SkyTraq）大量出现在
        单点定位模块上，把它们当 RTK 是这类判定最容易犯的错。
        """
        raw = nmea(',00,123519,4807.038,N,01131.000,E,545.4,G3,2.1,2.0,,,', 'PUBX')
        info = scanner.sniff(raw)
        self.assertEqual(info['class'], scanner.CLASS_GNSS)
        self.assertEqual(info['vendor'], 'u-blox')
        self.assertIn('nmea_priv', info['features'])
        self.assertNotIn('nmea', info['features'],
                         '私有语句不能混进标准 NMEA 计数')

    def test_private_nmea_without_valid_checksum_is_ignored(self):
        """校验和错的 `$PUBX` 不算证据 —— 乱码里撞出 `$P` + 4 字母太容易了。"""
        raw = b'$PUBX,00,123519,4807.038*00\r\n'
        info = scanner.sniff(raw)
        self.assertEqual(info['family'], scanner.FAMILY_NOISE)

    def test_ashtech_talker_is_recognised(self):
        """`$PASHR` 是 Ashtech/Hemisphere 的专有定位语句，单独归类并报厂牌。"""
        raw = nmea(',POS,0,1,123519,4807.038,N,01131.000,E,545.4,,', 'PASHR')
        info = scanner.sniff(raw)
        self.assertIn('ashtech', info['features'])
        self.assertEqual(info['vendor'], 'Ashtech/Hemisphere')

    def test_ubx_monver_is_gnss(self):
        info = scanner.sniff(ubx(0x0A, 0x04, b'ROM CORE 3.01 (107888)\x00'))
        self.assertEqual(info['family'], scanner.FAMILY_GNSS)
        self.assertEqual(info['vendor'], 'u-blox')
        self.assertIn('ubx', info['features'])

    def test_bogus_ubx_header_is_not_ubx(self):
        """只有 `B5 62` 帧头、校验位对不上的字节流不算 UBX。

        真机实测错波特率下 9600/230400/921600 每档都能从噪声里
        "凑"出这种头部，所以这一条必须靠校验位而不是帧头。
        """
        raw = b'\xb5\x62\x0a\x04\xdc\x00' + bytes(40) + b'\xde\xad'
        info = scanner.sniff(raw)
        self.assertEqual(info['family'], scanner.FAMILY_NOISE)
        self.assertNotIn('ubx', info['features'])

    def test_ubx_heartbeat_still_rejected(self):
        """透传桥的 `01-3B` 心跳依旧要拦掉（真机 COM12 的既有反例）。"""
        payload = bytes([0x50, 0xB0, 0x12, 0x00, 0x38, 0xB4, 0x12, 0x00])
        info = scanner.sniff(ubx(0x01, 0x3B, payload))
        self.assertEqual(info['family'], scanner.FAMILY_NOISE)

    def test_sbf_is_recognised(self):
        info = scanner.sniff(sbf())
        self.assertEqual(info['vendor'], 'Septentrio')
        self.assertIn('sbf', info['features'])

    def test_novatel_is_recognised(self):
        info = scanner.sniff(novatel())
        self.assertEqual(info['vendor'], 'NovAtel')
        self.assertIn('novatel', info['features'])

    def test_tsip_is_recognised(self):
        info = scanner.sniff(tsip())
        self.assertEqual(info['vendor'], 'Trimble')
        self.assertIn('trimble', info['features'])

    def test_plain_noise_stays_noise(self):
        info = scanner.sniff(b'the quick brown fox jumps over the lazy dog')
        self.assertEqual(info['family'], scanner.FAMILY_NOISE)
        self.assertEqual(info['class'], '')
        self.assertEqual(info['vendor'], '')

    def test_qx_private_sentences_are_recognised(self):
        """`$QX` 私有族（姿态/DR）要能认出来，且不能被当成标准 GNSS 语句。

        2026-11-13 真机（COM9）：一块带 IMU 的模组在 0 星时持续发
        `$QXATT`（姿态）与 `$QXDRS`（DR 状态）。它既不是 `$P<厂商码>`
        的私有格式，也不在 NMEA 标准 talker 表里，老扫描器两套判据都漏 ——
        界面上只显示"NMEA 语句"，看不出这块模块带 DR/姿态能力。
        """
        stream = (nmea('ATT,,0,,,,,,,,,', 'QX')
                  + nmea('DRS,,1,2,1,2,1,0,0,0', 'QX')
                  + nmea(',123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,', 'GNGGA'))
        info = scanner.sniff(stream)
        self.assertIn('nmea_qx', info['features'])
        self.assertEqual(info['counts'].get('nmea_qx'), 2)
        self.assertEqual(info['family'], scanner.FAMILY_GNSS)
        self.assertEqual(info['class'], scanner.CLASS_GNSS,
                         'QX 语句只说明是 GNSS 设备，没有 RTCM 就不能判 RTK')

    def test_qx_needs_valid_checksum(self):
        """`$QX` 计数必须验校验和 —— 错波特率的乱码里撞中 7 字节头太容易。"""
        bad = b'$QXATT,,0,,,,,,,,,*00\r\n'
        self.assertEqual(scanner._count_qx_sentences(bad), 0)
        self.assertEqual(scanner._count_qx_sentences(nmea('ATT,,0,,,,,,,,,', 'QX')), 1)


class VendorTest(unittest.TestCase):
    """厂牌判定：认得出就报，认不出就空着 —— 不许猜。

    这组用例的由来：现场最常问的一句是"这个口是哪家的模块"。
    判错厂牌比不判更糟 —— 用户会拿错的命令集去试，把模块配乱。
    """

    def test_rtcm_1033_identifies_vendor_in_base_mode(self):
        """基准站只播 RTCM 时，**1033 是唯一能认出厂牌的通道**。

        LG290P 配成基准站后 NMEA 全关，串口上只有 RTCM。1033 的
        `RECEIVER TYPE` 里写着 `Quectel  LG290P`，不解析它就永远只能
        显示"RTCM 差分数据"，认不出厂家。
        """
        info = scanner.sniff(rtcm_1033())
        self.assertEqual(info['class'], scanner.CLASS_RTK)
        self.assertEqual(info['vendor'], 'Quectel')
        self.assertEqual(info['vendor_confidence'], 'high')
        self.assertIn('LG290P', info['model'])
        self.assertTrue(any('1033' in line for line in info['vendor_evidence']))

    def test_rtcm_1033_ublox(self):
        info = scanner.sniff(rtcm_1033('u-blox ZED-F9P', 'HX-CSX601A'))
        self.assertEqual(info['vendor'], 'u-blox')
        self.assertIn('ZED-F9P', info['model'])

    def test_novatel_ascii_log_is_not_unicore(self):
        """`#VERSIONA` 两家都有 —— 不能看到 `#` 就判成和芯星通。

        Unicore 的日志格式照着 NovAtel 做的，`#VERSIONA,...` 完全一样。
        区分点在第 2 字段：NovAtel 是端口名（COM1），Unicore 是数字。
        判错的后果是让用户拿 Unicore 的命令去配 NovAtel。
        """
        novatel_log = (b'#VERSIONA,COM1,0,60.0,FINESTEERING,1932,306322.000,'
                       b'02000020,4dad,1984;*1a1b1c1d\r\n')
        info = scanner.sniff(novatel_log)
        self.assertEqual(info['vendor'], 'NovAtel')
        self.assertNotIn('unicore', info['features'])

    def test_unicore_ascii_log(self):
        unicore_log = b'#VERSIONA,79,GPS,FINE,2299,417336000,0,0,18,835;*8f1e2d3c\r\n'
        info = scanner.sniff(unicore_log)
        self.assertEqual(info['vendor'], 'Unicore')
        self.assertIn('unicore', info['features'])

    def test_st_teseo_private_nmea(self):
        """`$PSTM` 是 ST Teseo 的私有语句 —— 私有 NMEA 表要收得够全。

        一家厂往往只有一两条私有语句露头，表里漏一个 talker 就等于
        这个厂整条线认不出来。
        """
        raw = nmea(',VER,1.0,ST', 'PSTM')
        info = scanner.sniff(raw)
        self.assertEqual(info['vendor'], 'ST Teseo')
        self.assertEqual(info['vendor_confidence'], 'medium')

    def test_septentrio_private_nmea(self):
        info = scanner.sniff(nmea(',HRP,1,2,3,4', 'PSSN'))
        self.assertEqual(info['vendor'], 'Septentrio')

    def test_sirf_binary(self):
        """SiRF 二进制（`A0 A2 … B0 B3`）要验校验和才认。"""
        info = scanner.sniff(sirf())
        self.assertEqual(info['vendor'], 'SiRF')
        self.assertIn('sirf', info['features'])

    def test_sirf_bad_checksum_is_ignored(self):
        frame = bytearray(sirf())
        frame[-3] ^= 0xFF                     # 打坏校验和
        info = scanner.sniff(bytes(frame))
        self.assertEqual(info['vendor'], '')

    def test_txt_banner_gives_vendor(self):
        """`$GNTXT` 里写着厂名时也要认 —— 这是 u-blox 的默认行为。"""
        raw = nmea(',01,01,02,u-blox AG - www.u-blox.com', 'GNTXT')
        info = scanner.sniff(raw)
        self.assertEqual(info['vendor'], 'u-blox')
        self.assertEqual(info['vendor_confidence'], 'medium')

    def test_quectel_version_reply_gives_model(self):
        raw = nmea('VERNO,LG290P03AANR01A03S,001S,0,0,0,0,0', 'PQTM')
        info = scanner.sniff(raw)
        self.assertEqual(info['vendor'], 'Quectel')
        self.assertEqual(info['model'], 'LG290P03AANR01A03S')

    def test_vendor_is_empty_when_nothing_identifies_it(self):
        """纯标准 NMEA 认不出厂牌 —— 空着，不猜。

        `$GPGGA` 谁都会发，它只证明"这是台 GNSS 设备"。
        这种情况下前端显示破折号 + 原因，比编一个厂牌好。
        """
        raw = nmea(',123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,', 'GPGGA')
        info = scanner.sniff(raw)
        self.assertEqual(info['vendor'], '')
        self.assertEqual(info['vendor_confidence'], '')
        self.assertEqual(info['vendor_evidence'], [])

    def test_vendor_reaches_probe_result(self):
        """厂牌必须一路带到 probe 的最终结果上，否则前端拿不到。"""
        stream = rtcm_1033()
        with mock.patch.object(scanner, '_listen', side_effect=lambda p, b, w: stream):
            item = scanner.probe_passive('COM_TEST', bauds=[115200])
        self.assertEqual(item['vendor'], 'Quectel')
        self.assertTrue(item['vendor_evidence'])
        self.assertTrue(item['model'])


class VerifyTest(unittest.TestCase):
    """第二层：帧完整性。`strict` 只给真的验过校验位的东西。"""

    def test_nmea_checksum_counts(self):
        raw = nmea(',123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,', 'GPGGA')
        result = scanner.verify(raw)
        self.assertEqual(result['verdict'], 'strict')
        self.assertEqual(result['strict']['nmea'], 1)

    def test_rtcm_detail_mentions_crc(self):
        result = scanner.verify(rtcm(bytes(19)))
        self.assertEqual(result['verdict'], 'strict')
        self.assertIn('CRC', result['detail'])

    def test_structural_only_binaries_are_weak(self):
        """SBF / NovAtel / TSIP 只做了结构校验，不给 `strict`。

        这三家各自的 CRC 都是自家变体（SBF 两套、NovAtel 取反、TSIP 无 CRC），
        按错的算法算会把真帧判成假帧 —— 诚实降级成 `weak` 比硬判好。
        """
        for frame in (sbf(), novatel(), tsip()):
            result = scanner.verify(frame)
            self.assertEqual(result['verdict'], 'weak', frame[:8])
            self.assertEqual(result['strict'], {}, frame[:8])

    def test_empty_is_none(self):
        self.assertEqual(scanner.verify(b'')['verdict'], 'none')


class ProbeTest(unittest.TestCase):
    """第三层：`probe_passive` 要把 class / vendor 带到最终结果上。"""

    def test_passive_picks_rtk_port(self):
        good = nmea(',123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,', 'GPGGA')
        good += rtcm(bytes(19))
        streams = {9600: b'', 115200: good}

        def fake_listen(port, baud, window):
            return streams.get(baud, b'')

        with mock.patch.object(scanner, '_listen', side_effect=fake_listen):
            item = scanner.probe_passive('COM_TEST', bauds=[9600, 115200])
        self.assertEqual(item['baud'], 115200)
        self.assertEqual(item['family'], scanner.FAMILY_GNSS)
        self.assertEqual(item['class'], scanner.CLASS_RTK)
        self.assertEqual(item['features'][0], 'rtcm')

    def test_passive_reports_gnss_without_rtcm(self):
        stream = nmea(',123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,', 'GPGGA')

        with mock.patch.object(scanner, '_listen', side_effect=lambda p, b, w: stream):
            item = scanner.probe_passive('COM_TEST', bauds=[115200])
        self.assertEqual(item['class'], scanner.CLASS_GNSS,
                         '只有 NMEA 时必须如实说 GNSS，不能吹成 RTK')

    def test_passive_nothing_read_reports_no_class(self):
        with mock.patch.object(scanner, '_listen', side_effect=lambda p, b, w: b''):
            item = scanner.probe_passive('COM_TEST', bauds=[115200])
        self.assertIsNone(item['baud'])
        self.assertEqual(item['class'], '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
