# -*- coding: utf-8 -*-
"""u-blox ZED-F9P 驱动离线自测（不需要硬件）。

跑法：
    python -m pytest tests/test_ublox_f9p.py -v

本文件只放**不依赖串口**的东西：
    1. UBX 帧本身     —— Fletcher 8-8 校验、组帧/拆帧往返、长度小端
    2. 通道混流       —— UBX / NMEA / RTCM 三路并流时各拿各的、半帧等待、坏帧重同步
    3. 键表           —— 键 ID、类型宽度、scale、与 pyubx2 权威表交叉校验

要有硬件才能验的语义（VALSET 参数拼装、重启流程、恢复出厂、持久化回读），
本文件不覆盖 —— 那些必须对着真模块手动验，不适合放进自动化回归。

## 为什么把假模块那套删了

原来这里用 `FakeSerial` + 回调造了一个"假 F9P"来跑语义测试。它的问题是
**假的东西只会照着写的假设回话**：那条假模块对 CFG-RST 的应答、对重启后
波特率的处理，全是按我们自己的想象写的。于是 3 字节的 CFG-RST 载荷
（真机要 4 字节）、`resetMode=0x02`（真机不重载配置）这类错误，
在假模块上是绿的 —— 测试通过反而把错误固化了。

假模块和依赖它的用例已全部删除。本文件退回到它真正擅长的部分：
纯字节和纯表，这些在任何机器上结果都是确定的，适合当回归基线。
"""
import struct
import sys
import unittest

from rtkserver.calibration.drivers import ubx_keys as K
from rtkserver.calibration.drivers.base import Channel, ubx_checksum, ubx_frame, ubx_parse


# ==================== 1. UBX 帧 ====================
class TestUbxFrame(unittest.TestCase):

    def test_fletcher_not_xor(self):
        """UBX 用 Fletcher 8-8，**不是** NMEA 的 XOR —— 混用会让模块永远不回话。"""
        body = bytes([0x06, 0x8A, 0x00, 0x00])
        a, b = ubx_checksum(body)
        self.assertEqual((a, b), (0x90, 0x90 + 0x06 + 0x90 + 0x00 + 0x90 + 0x00) if False else (a, b))
        # 手工算一遍，钉死算法
        a_ref = (0x06 + 0x8A + 0x00 + 0x00) & 0xFF
        self.assertEqual(a, a_ref)
        self.assertEqual(b, (0x06 + (0x06 + 0x8A) + (0x06 + 0x8A) + (0x06 + 0x8A)) & 0xFF)

    def test_frame_roundtrip(self):
        payload = struct.pack('<IBB', 0x20030001, 0, 0x05)
        data = ubx_frame(0x06, 0x8A, payload)
        self.assertEqual(data[:2], b'\xb5\x62')
        self.assertEqual(data[2], 0x06)
        self.assertEqual(data[3], 0x8A)
        self.assertEqual(data[4] | (data[5] << 8), len(payload))
        cls, msg_id, back = ubx_parse(data)
        self.assertEqual((cls, msg_id), (0x06, 0x8A))
        self.assertEqual(back, payload)

    def test_bad_checksum_rejected(self):
        data = bytearray(ubx_frame(0x06, 0x8A, b'\x01\x02\x03'))
        data[-1] ^= 0xFF
        self.assertIsNone(ubx_parse(bytes(data)))

    def test_frame_length_is_little_endian(self):
        """长度字段是小端：写成大端的话 >255 字节的 VALGET 应答会整帧错位。"""
        payload = bytes(range(256)) + b'\x01'
        data = ubx_frame(0x06, 0x8B, payload)
        self.assertEqual(data[4], 0x01)
        self.assertEqual(data[5], 0x01)


# ==================== 2. 通道混流 ====================
class TestChannelMixedStream(unittest.TestCase):

    def test_ubx_only_when_declared(self):
        """没声明 0xB5 的型号，UBX 字节必须继续当噪声（不能改变既有行为）。"""
        plain = Channel(port='FAKE')
        plain._buffer = ubx_frame(0x05, 0x01, b'\x06\x8A')
        self.assertIsNone(plain._next_line())
        self.assertEqual(plain.counters.get('ubx', 0), 0)
        self.assertGreater(plain.counters['noise'], 0)

    def test_ubx_split_from_nmea_and_rtcm(self):
        """UBX + NMEA + RTCM 混在一条流里，三条通路都要各拿各的。"""
        from rtkserver.sources.rtcm_parser import crc24q
        # RTCM3：D3 | 6bit 保留 + 10bit 长度 | 载荷 | CRC24Q
        rtcm_body = b'\xd3\x00\x03' + b'\x3e\xd0\x00'
        rtcm = rtcm_body + crc24q(rtcm_body).to_bytes(3, 'big')
        ublox = ubx_frame(0x05, 0x01, bytes([0x06, 0x8A]))
        nmea = b'$GNGGA,1,2,3*00\r\n'

        ch = Channel(port='FAKE')
        ch.heads = ('$', '\xb5')
        ch._buffer = ublox + nmea + rtcm
        line = ch._next_line()
        self.assertEqual(line, nmea.decode().strip())
        self.assertIsNotNone(ch.ubx_last(0x05, 0x01), 'UBX 帧要进缓存')
        self.assertEqual(ch.counters.get('ubx'), 1)
        self.assertIsNone(ch._next_line())
        self.assertIsNotNone(ch.cache.get('rtcm_last'), 'RTCM 帧要进缓存')

    def test_partial_ubx_waits(self):
        """半帧必须等后续字节，不能丢。"""
        ch = Channel(port='FAKE')
        ch.heads = ('$', '\xb5')
        full = ubx_frame(0x06, 0x8B, b'\x00' * 40)
        ch._buffer = full[:10]
        self.assertIsNone(ch._next_line())
        self.assertEqual(len(ch._buffer), 10, '半帧不能被丢掉')
        ch._buffer += full[10:]
        ch._next_line()
        self.assertIsNotNone(ch.ubx_last(0x06, 0x8B))

    def test_fake_sync_in_payload_is_resynced(self):
        """伪同步头（能过长度检查、但校验不过）要被跳过并重新同步到真帧。"""
        # 造一个**完整但校验错**的帧：长度字段合法（4 字节载荷），
        # 载荷中间还藏着 `B5 62`。模块收到这种帧必然丢弃并重新同步。
        body = bytes([0x06, 0x8A, 0x04, 0x00]) + b'\x00\xb5\x62\x00'
        a, b = ubx_checksum(body)
        fake = b'\xb5\x62' + body + bytes([a ^ 0xFF, b ^ 0xFF])   # 故意改坏校验
        real = ubx_frame(0x05, 0x01, bytes([0x06, 0x8A]))

        ch = Channel(port='FAKE')
        ch.heads = ('$', '\xb5')
        ch._buffer = fake + real
        ch._next_line()
        self.assertIsNotNone(ch.ubx_last(0x05, 0x01), '真帧必须被找到')
        self.assertGreater(ch.counters['noise'], 0, '坏帧要记噪声')
        self.assertEqual(ch.counters.get('ubx'), 1, '只认那一个真帧')




class TestKeyTable(unittest.TestCase):

    def test_key_ids_match_spec(self):
        """钉死几个最容易抄错的键 ID（来自 Interface description）。"""
        self.assertEqual(K.lookup('TMODE_MODE')[0], 0x20030001)
        self.assertEqual(K.lookup('TMODE_POS_TYPE')[0], 0x20030002)
        self.assertEqual(K.lookup('TMODE_ECEF_X')[0], 0x40030003)
        self.assertEqual(K.lookup('TMODE_LAT')[0], 0x40030009)
        self.assertEqual(K.lookup('TMODE_LAT_HP')[0], 0x2003000C)
        self.assertEqual(K.lookup('UART1_BAUDRATE')[0], 0x40520001)
        self.assertEqual(K.lookup('RTCM_1005')[0], 0x209102BE)
        # 2026-11 修正的三个"端口后缀错位"错误 ID（原来抄的是 I2C 那一行）：
        # MSGOUT 端口偏移固定 I2C→UART1→UART2→USB→SPI，UART1 = I2C+1、USB = I2C+3。
        self.assertEqual(K.lookup('RTCM_1230')[0], 0x20910304, '1230 原来是 0x20910303（I2C）')
        self.assertEqual(K.lookup('NAVOUT_NAV_SVIN')[0], 0x20910089, 'SVIN 原来是 0x20910088（I2C）')
        self.assertEqual(K.lookup('NAVOUT_NAV_EOE')[0], 0x20910160, 'EOE 原来的 0x20910263 不存在')

    def test_rtcm_msm4_ids_match_spec(self):
        """钉死 4 个 MSM4 的 UART1 / USB 键 ID —— 这是本项目代价最大的一次抄错。

        原表把 1074/1084/1094/1124 全部写成了 **1077/1087/1097/1127 的 I2C 键**
        （0x209102CC / 0x209102D1 / 0x20910318 / 0x209102D6）。这几个 ID 真实存在、
        类型也是 U1，`_self_check()` 的撞车检查只看 canonical 不看 ID 指向谁，
        `test_matches_pyubx2_authoritative_table()` 也只查"ID 在库里 + 类型匹配"
        —— 于是导入期自检和测试双双放行，而 `config_rtcm()` 默认 `msm_type=4`，
        默认路径下物理口一条观测报文都不播，移动站永远进不了固定解。

        期望值来自 pyubx2 的 `UBX_CONFIG_DATABASE`（权威表）。
        """
        expect = {
            # 报文号: (I2C, UART1, UART2, USB, SPI)
            1005: (0x209102BD, 0x209102BE, 0x209102BF, 0x209102C0, 0x209102C1),
            1074: (0x2091035E, 0x2091035F, 0x20910360, 0x20910361, 0x20910362),
            1077: (0x209102CC, 0x209102CD, 0x209102CE, 0x209102CF, 0x209102D0),
            1084: (0x20910363, 0x20910364, 0x20910365, 0x20910366, 0x20910367),
            1087: (0x209102D1, 0x209102D2, 0x209102D3, 0x209102D4, 0x209102D5),
            1094: (0x20910368, 0x20910369, 0x2091036A, 0x2091036B, 0x2091036C),
            1097: (0x20910318, 0x20910319, 0x2091031A, 0x2091031B, 0x2091031C),
            1124: (0x2091036D, 0x2091036E, 0x2091036F, 0x20910370, 0x20910371),
            1127: (0x209102D6, 0x209102D7, 0x209102D8, 0x209102D9, 0x209102DA),
            1230: (0x20910303, 0x20910304, 0x20910305, 0x20910306, 0x20910307),
        }
        ports = ('I2C', 'UART1', 'UART2', 'USB', 'SPI')
        for num, ids in expect.items():
            self.assertIn(num, K.RTCM_OUT_PORTS, f'{num} 不在 RTCM_OUT_PORTS 里')
            for port, want in zip(ports, ids):
                self.assertEqual(K.lookup_rtcm(num, port), want,
                                 f'RTCM_{num}_{port} 应为 {want:#010x}')
        # 4 个 MSM4 的 UART1 值单独再钉一遍，附带失败文案说明错在哪
        self.assertEqual(K.lookup_rtcm(1074, 'UART1'), 0x2091035F,
                         'GPS MSM4 曾写成 0x209102CC（= 1077_I2C）')
        self.assertEqual(K.lookup_rtcm(1084, 'UART1'), 0x20910364,
                         'GLONASS MSM4 曾写成 0x209102D1（= 1087_I2C）')
        self.assertEqual(K.lookup_rtcm(1094, 'UART1'), 0x20910369,
                         'Galileo MSM4 曾写成 0x20910318（= 1097_I2C）')
        self.assertEqual(K.lookup_rtcm(1124, 'UART1'), 0x2091036E,
                         '北斗 MSM4 曾写成 0x209102D6（= 1127_I2C）')
        # 反向确认：MSM4 与 MSM7 的 ID 绝不能相同（这正是当年抄错后的表象）
        for m4, m7 in ((1074, 1077), (1084, 1087), (1094, 1097), (1124, 1127)):
            for port in ports:
                self.assertNotEqual(K.lookup_rtcm(m4, port), K.lookup_rtcm(m7, port),
                                    f'MSM4({m4}) 与 MSM7({m7}) 在 {port} 上撞了 ID')

    def test_i2c_address_is_one_byte(self):
        """I2C_ADDRESS 是 U1（0x20510001）。写成 U2 会多带一个 0x00，
        模块按错位解析后 NAK —— 这也是抄表时容易顺手写成 2 字节的键。"""
        rec = K.lookup('I2C_ADDRESS')
        self.assertEqual(rec[0], 0x20510001)
        self.assertEqual(rec[1][0], 'U1')
        self.assertEqual(rec[1][1], 1)

    def test_matches_pyubx2_authoritative_table(self):
        """全表与 pyubx2 的 `UBX_CONFIG_DATABASE` 交叉校验（1343 个键）。

        pyubx2 是**可选**的参考库：拿不到就跳过，不能让主仓库因缺它而红。
        校验的是 ID 与「类型类别 + 字节长度」，**不校验 scale** ——
        权威表没有物理单位信息，scale 是我们自己按手册补的。

        **还校验"名字语义"**：ID 在权威表里对应的键名必须与我们的键名同义。
        这一步是补 2026-11 那次漏检的关键 —— 当时 `RTCM_1074` 指向了
        `CFG_MSGOUT_RTCM_3X_TYPE1077_I2C`，ID 真实存在、类型也对，
        只查"ID 在库里"的旧版本完全放行，于是默认 MSM4 路径一条观测报文
        都不播。同义判定规则：剥掉前缀/端口后缀/类型后缀后，剩下的
        标识部分必须相等（如 `1005`、`NAV_PVT`、`GGA`）。
        """
        import os
        for cand in (os.environ.get('PYUBX2_SRC'),
                     os.path.join(os.path.dirname(os.path.dirname(
                         os.path.abspath(__file__))), '..', 'pyubx2', 'src'),
                     r'E:\GitHub\pyubx2\src'):
            if cand and os.path.isdir(os.path.join(cand, 'pyubx2')) \
                    and cand not in sys.path:
                sys.path.insert(0, cand)
                break
        try:
            from pyubx2 import ubxtypes_configdb as CD
        except ImportError:
            self.skipTest('未安装 pyubx2，跳过交叉校验')
        by_id = {}
        for full, (aid, att) in CD.UBX_CONFIG_DATABASE.items():
            by_id.setdefault(aid, {})[full] = (att[0], int(att[1:]))
        bad = []
        for name, rec in K.ALL_KEYS.items():
            key_id, type_info, _scale, _note = rec
            if key_id not in by_id:
                bad.append((str(name), f'{key_id:#010x}', '权威表里没有这个 ID'))
                continue
            want = (type_info[0][0], type_info[1])
            if want not in by_id[key_id].values():
                bad.append((str(name), f'{key_id:#010x}',
                            f'我们={want[0]}{want[1]} '
                            f'权威={sorted(by_id[key_id].values())}'))
                continue
            # ---- 名字语义：我们的名字必须与权威键名同义 ----
            official = [full for full, att in by_id[key_id].items() if att == want]
            mine = self._semantic_token(str(name))
            theirs = {self._semantic_token(f) for f in official}
            if mine not in theirs:
                bad.append((str(name), f'{key_id:#010x}',
                            f'名字语义不符：我们="{mine}"，权威={sorted(theirs)}'))
        self.assertEqual(bad, [], f'{len(bad)} 个键与 pyubx2 不一致：{bad[:10]}')

    @staticmethod
    def _semantic_token(name):
        """把键名剥成"这条键代表哪个配置项"的标识部分。

        `CFG_MSGOUT_RTCM_3X_TYPE1074_UART1` → `1074`
        `NAVOUT_NAV_PVT_USB`               → `NAV_PVT`
        `RTCM_1005`                        → `1005`
        `CFG_NAVSPG_FIXMODE`               → `NAVSPG_FIXMODE`
        `TMODE_MODE`                       → `TMODE_MODE`
        """
        s = str(name).upper()
        for prefix in ('CFG_', 'NAVOUT_', 'RXMOUT_', 'NMEAMOUT_'):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        else:
            # `RTCM_1005` 是我们的报文号写法，要剥；
            # `RTCM_DF003_OUT` 是 RTCM 配置族，pyubx2 里也叫这个名字，**不能剥**。
            if s.startswith('RTCM_') and s[5:].split('_')[0].isdigit():
                s = s[len('RTCM_'):]
        # 端口后缀
        for suffix in ('_I2C', '_UART1', '_UART2', '_USB', '_SPI'):
            if s.endswith(suffix):
                s = s[:-len(suffix)]
                break
        # pyubx2 的 RTCM 键名带品类前缀 `MSGOUT_RTCM_3X_TYPE`
        if s.startswith('MSGOUT_RTCM_3X_TYPE'):
            s = s[len('MSGOUT_RTCM_3X_TYPE'):]
        elif s.startswith('MSGOUT_'):
            s = s[len('MSGOUT_'):]
        # 报文族前缀：pyubx2 是 `MSGOUT_UBX_NAV_PVT_*` / `MSGOUT_NMEA_ID_GGA_*`，
        # 我们是 `NAV_PVT` / `GGA`。统一剥掉 `UBX_` / `NMEA_ID_` / `NMEA_` 族段。
        for fam in ('UBX_', 'NMEA_ID_', 'NMEA_'):
            if s.startswith(fam):
                s = s[len(fam):]
                break
        if s.startswith('ID_'):
            s = s[len('ID_'):]
        return s

    def test_scale_values(self):
        self.assertEqual(K.lookup('TMODE_LAT')[2], 1e-7)
        # HP 族：键本身就是 1e-9 deg / 0.1mm 的整数，scale 必须是 1.0。
        # 写成 1e-9 会让 `_valset` 把已转好的整数再除一次，真机直接越界 NAK。
        self.assertEqual(K.lookup('TMODE_LAT_HP')[2], 1.0)
        self.assertEqual(K.lookup('TMODE_LON_HP')[2], 1.0)
        self.assertEqual(K.lookup('TMODE_HEIGHT_HP')[2], 1.0)
        self.assertEqual(K.lookup('TMODE_ECEF_X_HP')[2], 1.0)
        self.assertEqual(K.lookup('TMODE_HEIGHT')[2], 1.0)   # cm
        self.assertEqual(K.lookup('TMODE_ECEF_X')[2], 1.0)   # cm
        # ACC 族：物理值是 mm，原始值是 0.1mm，所以 scale=0.1
        self.assertEqual(K.lookup('TMODE_FIXED_POS_ACC')[2], 0.1)
        self.assertEqual(K.lookup('TMODE_SVIN_ACC_LIMIT')[2], 0.1)

    def test_signedness(self):
        self.assertTrue(K.lookup('TMODE_LAT')[1][2], 'LAT 是 I4 有符号')
        self.assertTrue(K.lookup('TMODE_LAT_HP')[1][2], 'LAT_HP 是 I1 有符号')
        self.assertFalse(K.lookup('UART1_BAUDRATE')[1][2], '波特率是无符号')

    def test_no_duplicate_ids(self):
        """键 ID 不能重复 —— 同 ID 挂两个**不同的键**，写入时必有一个走错组。

        别名（`NAV_PVT` / `NAVOUT_NAV_PVT`、`RXM_RAWX` / `RXMOUT_RXM_RAWX`、
        `GGA` / `NMEAMOUT_GGA`、`1005` / `RTCM_1005`）是设计：裸名给通用查询用，
        带前缀的名给 `set_message_rate` 用。它们的 canonical 名相同，按 ID
        分组后只剩一个 canon，不算撞车 —— 与 `ubx_keys._self_check()` 同一判据。
        """
        seen = {}
        for name, rec in K.ALL_KEYS.items():
            seen.setdefault(rec[0], {}).setdefault(
                K._canonical(str(name)), []).append(name)
        dups = {hex(k): sorted(by_canon) for k, by_canon in seen.items()
                if len(by_canon) > 1}
        self.assertEqual(dups, {}, f'键 ID 撞车（不同键共用一个 ID）：{dups}')

if __name__ == '__main__':
    unittest.main(verbosity=2, exit=False)
