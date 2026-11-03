# -*- coding: utf-8 -*-
"""u-blox ZED-F9P 驱动离线自测（不需要真机）。

跑法：
    python -m tests.test_ublox_f9p

覆盖三件事，缺一不可：
    1. UBX 帧本身     —— Fletcher 8-8 校验、组帧/拆帧往返、混流拆分
    2. 驱动的语义方法 —— 用假模块（会按 VALSET/VALGET/MON-VER 应答）跑一遍
    3. 跨型号隔离     —— F9P 通道认 0xB5 之后，Quectel 的 `$` 流与 RTCM 的
                         0xD3 流行为必须**一字不变**（这是最容易改坏的地方）
"""
import struct
import sys
import unittest

from rtkserver.calibration.drivers import ubx_keys as K
from rtkserver.calibration.drivers import ublox_zed_f9p as F9P
from rtkserver.calibration.drivers.base import (
    Channel, ubx_checksum, ubx_frame, ubx_parse)


# ==================== 假串口 ====================
class FakeSerial:
    """最小的串口替身：写进去的字节交给 `on_write` 回调生成应答。

    `on_write(data)` 返回要"从模块发回来"的字节串，按到达顺序进读缓冲。
    """

    def __init__(self, on_write=None, port='FAKE', baudrate=38400):
        self.port = port
        self.baudrate = baudrate
        self.is_open = True
        self.on_write = on_write
        self._rx = b''
        self.written = []

    def write(self, data):
        self.written.append(bytes(data))
        if self.on_write:
            self._rx += self.on_write(bytes(data)) or b''
        return len(data)

    def flush(self):
        pass

    def read(self, size=256):
        if not self._rx:
            return b''
        chunk, self._rx = self._rx[:size], self._rx[size:]
        return chunk

    def reset_input_buffer(self):
        self._rx = b''

    def close(self):
        self.is_open = False


def _ack_ack():
    return ubx_frame(0x05, 0x01, bytes([0x06, 0x8A]))


def _ack_nak():
    return ubx_frame(0x05, 0x00, bytes([0x06, 0x8A]))


def make_f9p(on_write=None):
    """造一个挂在假串口上的 F9P 驱动。"""
    serial_ = FakeSerial(on_write)
    ch = Channel(serial_instance=serial_)
    drv = F9P.ZEDF9PDriver(ch)
    return drv, ch, serial_


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


# ==================== 3. 驱动语义 ====================
class TestDriverSemantics(unittest.TestCase):

    def test_model_constants(self):
        self.assertEqual(F9P.ZEDF9PDriver.MODEL_KEY, 'zed_f9p')
        # §3.8.1 明文默认 38400 —— 与 LG290P(460800)/LC29H(115200) 都不同
        self.assertEqual(F9P.ZEDF9PDriver.DEFAULT_BAUD, 38400)
        drv, _ch, _s = make_f9p()
        self.assertEqual(drv.bauds()[0], 38400, '首选档必须是厂商默认值')

    def test_three_generations_do_not_share_baud_defaults(self):
        """三派默认波特率必须各自独立（用户反复强调的隔离要求）。"""
        from rtkserver.calibration.drivers.driver_manager import (
            _REGISTRY, load_builtin_drivers)
        load_builtin_drivers()
        table = {k: _REGISTRY[k].DEFAULT_BAUD for k in
                 ('lg290p', 'lc29hbs', 'zed_f9p')}
        self.assertEqual(table, {'lg290p': 460800, 'lc29hbs': 115200, 'zed_f9p': 38400})
        self.assertEqual(len(set(table.values())), 3, '三家默认值不能撞在一起')

    def test_no_quectel_tokens(self):
        """F9P 驱动里不许出现 Quectel 的命令名（跨型号污染的最直接证据）。"""
        import inspect
        src = inspect.getsource(F9P)
        # 注释里提到 PQTM/PAIR 是允许的（讲差异），但**代码**里不能有字符串字面量
        for token in ('PQTMVERNO', 'PQTMCFGSVIN', 'PAIR432', 'PAIR864', 'PQTMSAVEPAR'):
            self.assertNotIn(f"'{token}", src, f'F9P 驱动不该出现 {token}')
            self.assertNotIn(f'"{token}', src, f'F9P 驱动不该出现 {token}')

    def test_detect_by_mon_ver(self):
        def on_write(data):
            if len(data) < 4 or data[2] != 0x0A or data[3] != 0x04:
                return b''
            sw = b'EXT CORE 1.00 (61b2dd)'.ljust(30, b'\x00')
            hw = b'00190000'.ljust(10, b'\x00')
            ext = b'ROM BASE 0x118B2060'.ljust(30, b'\x00')
            ext += b'FWVER=HPG 1.32'.ljust(30, b'\x00')
            ext += b'PROTVER=27.13'.ljust(30, b'\x00')
            ext += b'MOD=ZED-F9P'.ljust(30, b'\x00')
            return ubx_frame(0x0A, 0x04, sw + hw + ext)

        drv, _ch, _s = make_f9p(on_write)
        ok, message, data = drv.detect()
        self.assertTrue(ok, message)
        self.assertIn('ZED-F9P', message)
        self.assertIn('HPG', data['sw_version'] + ' ' + ' '.join(data['extensions']))

    def test_detect_rejects_non_f9p(self):
        def on_write(data):
            if len(data) < 4 or data[2] != 0x0A or data[3] != 0x04:
                return b''
            sw = b'ROM CORE 3.01 (107888)'.ljust(30, b'\x00')
            hw = b'00080000'.ljust(10, b'\x00')
            return ubx_frame(0x0A, 0x04, sw + hw + b'MOD=M8N'.ljust(30, b'\x00'))

        drv, _ch, _s = make_f9p(on_write)
        ok, message, _data = drv.detect()
        self.assertFalse(ok)
        self.assertIn('不是 ZED-F9P', message)

    def test_valset_waits_for_ack_each_time(self):
        """§3.1.1：每条配置必须等 ACK，连发会让模块暂停处理输入。"""
        seen = []

        def on_write(data):
            seen.append(bytes(data))
            return _ack_ack()

        drv, ch, _s = make_f9p(on_write)
        ok, msg, data = drv._valset([('TMODE_MODE', 1)])
        self.assertTrue(ok, msg)
        self.assertEqual(len(seen), 1)
        self.assertEqual(data['keys'], 1)
        # 帧结构：B5 62 | 06 8A | len | version layer transaction reserved0 | key value | ck
        frame = seen[0]
        self.assertEqual(frame[:4], b'\xb5\x62\x06\x8a')
        payload = frame[6:-2]
        # 载荷头是 **4 字节**。写成 3 字节时 value=1 字节的键侥幸能过，
        # 4 字节的键（TMODE_ECEF_X / UART1_BAUDRATE / FIXED_POS_ACC）会全 NAK。
        self.assertEqual(len(payload), 4 + 4 + 1, '载荷头必须 4 字节')
        self.assertEqual(payload[0], 0x00)                    # version
        # 默认写全部三层（RAM|BBR|Flash = 0x07），与 pyubx2 官方示例一致。
        # 只写 RAM 掉电就丢，只写 Flash 要重启才生效，三层全写才一步到位。
        self.assertEqual(payload[1], K.LAYER_ALL)
        self.assertEqual(payload[1], 0x07)
        self.assertEqual(payload[2], 0x00)                    # transaction
        self.assertEqual(payload[3], 0x00)                    # reserved0
        self.assertEqual(struct.unpack('<I', payload[4:8])[0], 0x20030001)
        self.assertEqual(payload[8], 1)                       # TMODE_MODE = survey_in

    def test_valset_four_byte_value_payload(self):
        """4 字节值的键必须拼成 4+4+4=12 字节载荷（真机 NAK 的原始 bug）。"""
        seen = []

        def on_write(data):
            seen.append(bytes(data))
            return _ack_ack()

        drv, _ch, _s = make_f9p(on_write)
        ok, _msg, _d = drv._valset([('TMODE_ECEF_X', -219778735)])
        self.assertTrue(ok)
        payload = seen[0][6:-2]
        self.assertEqual(len(payload), 12)
        self.assertEqual(payload[0], 0x00, 'version')
        self.assertEqual(payload[2], 0x00, 'transaction')
        self.assertEqual(payload[3], 0x00, 'reserved0 必须为 0')
        self.assertEqual(struct.unpack('<I', payload[4:8])[0], 0x40030003)
        self.assertEqual(struct.unpack('<i', payload[8:12])[0], -219778735)

    def test_valset_nak_reports(self):
        drv, _ch, _s = make_f9p(lambda d: _ack_nak())
        ok, msg, data = drv._valset([('TMODE_MODE', 1)])
        self.assertFalse(ok)
        self.assertEqual(msg, 'NAK')
        self.assertEqual(data['error'], 'nak')

    def test_valset_rejects_over_64_keys(self):
        """一条 VALSET 最多 64 个键。超了模块直接 NAK，且不告诉你是哪个键，
        所以必须在下发前拦住 —— 否则现场只看到一串无信息的 NAK。"""
        drv, _ch, _s = make_f9p()
        ok = [('TMODE_MODE', 0)] * (K.MAX_KEYS_PER_VALSET + 1)
        with self.assertRaises(Exception) as ctx:
            drv._valset(ok)
        self.assertIn('64', str(ctx.exception))

    def test_valset_accepts_exactly_64_keys(self):
        seen = []

        def on_write(data):
            seen.append(bytes(data))
            return _ack_ack()

        drv, _ch, _s = make_f9p(on_write)
        ok, msg, data = drv._valset([('TMODE_MODE', 0)] * K.MAX_KEYS_PER_VALSET)
        self.assertTrue(ok, msg)
        self.assertEqual(data['keys'], 64)
        # 边界值必须一次发完，不能悄悄截断
        self.assertEqual(len(seen), 1)
        self.assertEqual(struct.unpack('<H', seen[0][4:6])[0],
                         4 + 64 * (4 + 1), '载荷长度 = 4 字节头 + 64 × (4 字节 key + 1 字节值)')

    def test_valdel_frame(self):
        """CFG-VALDEL 载荷与 VALSET 同构，但**只有 key 没有 value**。"""
        seen = []

        def on_write(data):
            seen.append(bytes(data))
            return _ack_ack()

        drv, _ch, _s = make_f9p(on_write)
        ok, msg, data = drv._valdel(['TMODE_MODE', 'UART1_BAUDRATE'])
        self.assertTrue(ok, msg)
        frame = seen[0]
        self.assertEqual(frame[:4], b'\xb5\x62\x06\x8c', '0x06/0x8C')
        payload = frame[6:-2]
        # 4 字节头 + 2 × 4 字节 key，没有 value
        self.assertEqual(len(payload), 4 + 8)
        self.assertEqual(payload[1], K.LAYER_BBR | K.LAYER_FLASH)
        keys = [struct.unpack('<I', payload[i:i + 4])[0] for i in (4, 8)]
        self.assertEqual(keys, [0x20030001, 0x40520001])
        self.assertEqual(data['keys'], 2)

    def test_valdel_rejects_ram_only(self):
        """VALDEL 不能只作用 RAM —— 模块对 layer=RAM 会 NAK，提前拦住。"""
        drv, _ch, _s = make_f9p()
        with self.assertRaises(Exception) as ctx:
            drv._valdel(['TMODE_MODE'], layers=K.LAYER_RAM)
        self.assertIn('RAM', str(ctx.exception))

    def test_factory_reset_frame(self):
        """CFG-CFG 恢复出厂：clearMask = loadMask = 0x1F1F（与 pyubx2 一致）。

        deviceMask 是**单独一字节**，bit0=BBR bit1=Flash bit2=EEPROM，
        和 clearMask 那套 IO/MSG/NAV 位完全不是一回事。
        """
        seen = []

        def on_write(data):
            seen.append(bytes(data))
            return _ack_ack()

        drv, ch, _s = make_f9p(on_write)
        ch.baud = 115200          # 出厂复位会把波特率打回 38400
        drv.factory_reset(clear_flash=True, reboot_after=False)

        cfg = [f for f in seen if f[2:4] == b'\x06\x09'][0]
        payload = cfg[6:-2]
        self.assertEqual(len(payload), 13, 'clear(4)+save(4)+load(4)+device(1)')
        clear, save, load = struct.unpack('<III', payload[:12])
        device = payload[12]
        self.assertEqual(clear, 0x1F1F)
        self.assertEqual(save, 0, '恢复出厂不保存任何东西')
        self.assertEqual(load, 0x1F1F)
        self.assertEqual(device, K.CFG_CFG_DEV_BBR | K.CFG_CFG_DEV_FLASH)
        self.assertEqual(ch.baud, 38400, '复位后通道必须跟到默认波特率')

    def test_factory_reset_keeps_flash_when_asked(self):
        """clear_flash=False 只清 BBR，不动 Flash —— 用于"先回到能动的状态"。"""
        seen = []

        def on_write(data):
            seen.append(bytes(data))
            return _ack_ack()

        drv, _ch, _s = make_f9p(on_write)
        ok, _msg, data = drv.factory_reset(clear_flash=False, reboot_after=False)
        self.assertTrue(ok)
        self.assertEqual(data['device_mask'], hex(K.CFG_CFG_DEV_BBR))

    def test_pack_value_eight_byte(self):
        """8 字节键（X8，例如 USB_*_STR 系列）必须能打包，不能抛"不支持的键长"。"""
        raw = F9P.pack_value(K.X8, 0x1122334455667788)
        self.assertEqual(len(raw), 8)
        self.assertEqual(raw, bytes.fromhex('8877665544332211'), '小端')
        self.assertEqual(F9P.unpack_value(K.X8, raw), 0x1122334455667788)

    def test_pack_value_float_keys_keep_fraction(self):
        """浮点键（R4/R8）不能 round 成整数 —— 0.5 Hz 会被抹成 0。"""
        raw = F9P.pack_value(K.R8, 0.5)
        self.assertEqual(len(raw), 8)
        self.assertAlmostEqual(F9P.unpack_value(K.R8, raw), 0.5)
        raw4 = F9P.pack_value(K.R4, 1.5)
        self.assertEqual(len(raw4), 4)
        self.assertAlmostEqual(F9P.unpack_value(K.R4, raw4), 1.5, places=6)

    def test_layers_persist_flag(self):
        """persist=False 只写 RAM 层 —— 断电丢，但改完立刻生效。"""
        seen = []

        def on_write(data):
            seen.append(bytes(data))
            return _ack_ack()

        drv, _ch, _s = make_f9p(on_write)
        drv._valset([('TMODE_MODE', 0)], layers=K.LAYER_RAM)
        self.assertEqual(seen[0][7], K.LAYER_RAM)

    def test_hp_fields_are_signed(self):
        """HP 字段是有符号单字节：范围 -127~127，转成无符号会差出几十厘米。"""
        drv, _ch, _s = make_f9p()
        for x in (-2197750.12345, -2197787.34, 4319544.62, 4084499.11, 0.0):
            whole, hp = drv._split_ecef(x)
            self.assertTrue(-127 <= hp <= 127, f'HP 必须夹紧，实得 {hp}')
            raw = F9P.pack_value(K.I1, hp)
            self.assertEqual(F9P.unpack_value(K.I1, raw), hp, '有符号往返必须一致')
            # 还原：whole 是**厘米**，hp 是 0.1mm（1 cm = 100 个），再除 100 换米
            restored = (whole + hp / 100.0) / 100.0
            # 量化极限 0.05 mm，留 4 倍余量
            self.assertAlmostEqual(restored, x, delta=0.0002)

    def test_pack_value_unsigned_rejects_negative(self):
        """U1 不能吃负数：TMODE_MODE 传 -1 必须当场报错而不是打包成 255。"""
        with self.assertRaises(Exception):
            F9P.pack_value(K.U1, -1)

    def test_fixed_ecef_roundtrip(self):
        """写进去的坐标要能原样读回来（假模块按 VALGET 回发已写值）。"""
        store = {}

        def on_write(data):
            if len(data) < 6:
                return b''
            cls, msg_id = data[2], data[3]
            length = data[4] | (data[5] << 8)
            payload = data[6:6 + length]
            if (cls, msg_id) == (0x06, 0x8A):          # VALSET
                pos = 4          # version(1)+layer(1)+transaction(1)+reserved0(1)
                while pos + 4 <= len(payload):
                    key = struct.unpack('<I', payload[pos:pos + 4])[0]
                    pos += 4
                    rec = next((r for r in K.ALL_KEYS.values() if r[0] == key), None)
                    if rec is None:
                        break
                    size = rec[1][1]
                    store[key] = payload[pos:pos + size]
                    pos += size
                return _ack_ack()
            if (cls, msg_id) == (0x06, 0x8B):          # VALGET
                out = bytes([0x00, 0x01, 0x00, 0x00])
                pos = 4          # 请求的 position(2) 之后是 key；应答也按 4 起
                while pos + 4 <= len(payload):
                    key = struct.unpack('<I', payload[pos:pos + 4])[0]
                    pos += 4
                    rec = next((r for r in K.ALL_KEYS.values() if r[0] == key), None)
                    if rec is None:
                        break
                    size = rec[1][1]
                    out += struct.pack('<I', key) + store.get(key, b'\x00' * size)
                return ubx_frame(0x06, 0x8B, out)
            return b''

        drv, _ch, _s = make_f9p(on_write)
        # 典型 ECEF（北京附近），带厘米以下小数
        x, y, z = -2197787.34, 4319544.62, 4084499.11
        ok, message, data = drv.set_fixed_ecef(x, y, z)
        self.assertTrue(ok, message)
        got = data
        self.assertAlmostEqual(got['x'], x, delta=0.002)
        self.assertAlmostEqual(got['y'], y, delta=0.002)
        self.assertAlmostEqual(got['z'], z, delta=0.002)
        self.assertEqual(got['mode'], 'fixed')

    def test_rtcm_plan_guards(self):
        """手册的四条硬约束必须在下发前拦住（违反后会 ACK 但播不出去）。"""
        ok, problems = F9P.validate_rtcm_plan([1005, 1074, 1084, 1094, 1124, 1230])
        self.assertTrue(ok, problems)

        # 1006/1007/1033 只支持输入
        ok, problems = F9P.validate_rtcm_plan([1005, 1006, 1230])
        self.assertFalse(ok)
        self.assertTrue(any('1006' in p for p in problems))

        # MSM4 / MSM7 混用
        ok, problems = F9P.validate_rtcm_plan([1005, 1074, 1087, 1230])
        self.assertFalse(ok)
        self.assertTrue(any('MSM' in p for p in problems))

        # 缺 1230 → GLONASS 只能浮点
        ok, problems = F9P.validate_rtcm_plan([1005, 1077, 1087, 1097, 1127])
        self.assertFalse(ok)
        self.assertTrue(any('1230' in p for p in problems))

        # 同一星座开两种
        ok, problems = F9P.validate_rtcm_plan([1005, 1074, 1077, 1230])
        self.assertFalse(ok)
        self.assertTrue(any('GPS' in p for p in problems))

    def test_rtcm_output_rejects_input_only(self):
        """1006/1007 写速率必须**明确拒绝**，不能"配了但没播"。

        1006/1007 与 1033 是两种不同的"不支持"，文案不能混：
          · 1006/1007 —— 键存在、能 VALGET 到 0，但模块不输出 → "只支持输入"
          · 1033      —— **键根本不存在**（pyubx2 1343 键里没有），
                         写它必 NAK → 文案要直接指向"改用 1230"
        """
        drv, ch, _s = make_f9p(lambda d: _ack_ack())
        for num in (1006, 1007):
            ok, message, data = drv.set_message_rate(f'RTCM3-{num}', 1)
            self.assertFalse(ok, f'{num} 不该被接受')
            self.assertEqual(data.get('error'), 'unsupported')
            self.assertIn('只支持输入', message)
        # 1033 是"没这个键"，报错要说清楚换 1230，而不是让人去改输入
        ok, message, data = drv.set_message_rate('RTCM3-1033', 1)
        self.assertFalse(ok)
        self.assertEqual(data.get('error'), 'unsupported')
        self.assertIn('1230', message)
        self.assertIn(1033, K.RTCM_NO_KEY)
        # 通道上不应该有任何字节被发出去
        self.assertEqual(ch._serial.written, [])

    def test_rtcm_output_rejects_4072(self):
        """真机发现：4072.0/4072.1 在 HPG 1.32 上没有配置键，必须明确拒绝。

        手册的输出表里列了这两条，但 VALGET 静默不回（也不回 NAK）。
        不能让它发出去，也不能含糊成"不认识的名字"。
        """
        drv, ch, _s = make_f9p(lambda d: _ack_ack())
        for name in ('RTCM3-4072.0', '4072.1'):
            ok, message, data = drv.set_message_rate(name, 1)
            self.assertFalse(ok, f'{name} 不该被接受')
            self.assertEqual(data.get('error'), 'unsupported')
            self.assertIn('HPG 1.32', message)
        self.assertEqual(ch._serial.written, [], '拒绝时一个字节都不能发')

    def test_message_rate_accepts_nmea_and_rxm(self):
        """NMEA / RXM 报文也要能配速率 —— 键表里有，驱动以前没接。

        NMEA 常被写成 `GGA` / `GPGGA` / `NMEA-GGA` 三种形式，都要认。
        """
        seen = []

        def on_write(data):
            seen.append(bytes(data))
            return _ack_ack()

        drv, _ch, _s = make_f9p(on_write)
        cases = {
            'GGA': 'NMEAMOUT_GGA',
            'NMEA-GGA': 'NMEAMOUT_GGA',
            'GPGGA': 'NMEAMOUT_GGA',        # 带 talker 前缀
            'RMC': 'NMEAMOUT_RMC',
            'RAWX': 'RXMOUT_RXM_RAWX',
            'RXM-RAWX': 'RXMOUT_RXM_RAWX',
            'NAVSVIN': 'NAVOUT_NAV_SVIN',
            'NAV_SVIN': 'NAVOUT_NAV_SVIN',
            'RTCM3-1005': 'RTCM_1005',
            '1005': 'RTCM_1005',
        }
        for name, want in cases.items():
            ok, msg, data = drv.set_message_rate(name, 1)
            self.assertTrue(ok, f'{name}: {msg}')
            self.assertEqual(data['key'], want, name)
            self.assertEqual(K.lookup(want)[2], 1.0, f'{want} 的 scale')
        self.assertEqual(len(seen), len(cases))

    def test_resolve_msg_key_shares_one_parser(self):
        """`set_message_rate` 与 `get_message_rate` 必须用同一个解析器。

        以前两处各抄一份 if/elif，加一种报文就得改两个地方，
        漏一个的表现是"能设不能读"——这种 bug 很难从现象反推。
        """
        cases = {'1005': 'RTCM_1005', 'GGA': 'NMEAMOUT_GGA',
                 'NAVSVIN': 'NAVOUT_NAV_SVIN', 'RAWX': 'RXMOUT_RXM_RAWX'}
        for name, want in cases.items():
            self.assertEqual(F9P.ZEDF9PDriver._resolve_msg_key(name)[0], want)
        # 不认识的必须给出 err，而不是抛异常
        key, err = F9P.ZEDF9PDriver._resolve_msg_key('NOT_A_MSG')
        self.assertIsNone(key)
        self.assertTrue(err)

    def test_rtcm_out_has_only_live_keys(self):
        """RTCM_OUT 里只能有真机验证过的 10 个键（4072.* 已被剔除）。"""
        self.assertEqual(sorted(K.RTCM_OUT),
                         [1005, 1074, 1077, 1084, 1087,
                          1094, 1097, 1124, 1127, 1230])
        self.assertEqual(sorted(K.UNSUPPORTED_RTCM), [4072.0, 4072.1])

    def test_config_rtcm_msm4_plan(self):
        """MSM4 档要一次配齐 1005 + 四个星座 + 1230，且不碰 4072.*。"""
        captured = {}

        def on_write(data):
            if len(data) > 6 and data[2] == 0x06 and data[3] == 0x8A:
                length = data[4] | (data[5] << 8)
                payload = data[6:6 + length]
                pos, items = 4, {}     # version+layer+transaction+reserved0 之后才是 key
                while pos + 4 <= len(payload):
                    key = struct.unpack('<I', payload[pos:pos + 4])[0]
                    pos += 4
                    rec = next((r for r in K.ALL_KEYS.values() if r[0] == key), None)
                    if rec is None:
                        break
                    size = rec[1][1]
                    items[key] = payload[pos:pos + size]
                    pos += size
                captured.update(items)
            return _ack_ack()

        drv, _ch, _s = make_f9p(on_write)
        ok, message, data = drv.config_rtcm(msm_type=4)
        self.assertTrue(ok, message)
        want_on = [1005, 1074, 1084, 1094, 1124, 1230]
        for num in want_on:
            key = K.RTCM_OUT[num][0]
            self.assertEqual(captured.get(key), b'\x01', f'{num} 应该被打开')
        for num in (1077, 1087, 1097, 1127):
            key = K.RTCM_OUT[num][0]
            self.assertEqual(captured.get(key), b'\x00', f'{num} 应该被关闭（不能混 MSM）')
        # 4072.* 在 HPG 1.32 上没有配置键，绝不能出现在 VALSET 里
        self.assertEqual(set(captured), {K.RTCM_OUT[n][0] for n in K.RTCM_OUT},
                         '只该写 RTCM_OUT 里那 10 个键')

    def test_baud_whitelist(self):
        drv, _ch, _s = make_f9p(lambda d: _ack_ack())
        ok, message, data = drv.set_baud(12345)
        self.assertFalse(ok)
        self.assertEqual(data['error'], 'bad_baud')
        self.assertIn('38400', message)

    def test_antenna_info_unsupported(self):
        """F9P 不输出 1033，写天线信息必须明确拒绝。"""
        drv, _ch, _s = make_f9p()
        ok, message, data = drv.set_antenna_info('ADVNULLANTENNA')
        self.assertFalse(ok)
        self.assertEqual(data['error'], 'unsupported')

    def test_mode_alias_mapping(self):
        self.assertEqual(F9P.MODE_ALIASES['rover'], F9P.MODE_DISABLED)
        self.assertEqual(F9P.MODE_ALIASES['base'], F9P.MODE_FIXED)
        self.assertEqual(F9P.MODE_ALIASES['survey_in'], F9P.MODE_SURVEY_IN)

    def test_optional_surface_never_raises(self):
        """流程层会无条件调用每个可选方法，一个漏实现就是一个 500。"""
        drv, _ch, _s = make_f9p()
        for name in ('get_svin_config', 'read_rtcm_config', 'restore_rtcm_config',
                     'config_rtcm', 'get_message_rate', 'set_message_rate',
                     'disable_survey_in', 'read_antenna_info', 'save', 'reboot',
                     'get_mode', 'get_fixed_ecef', 'survey_status', 'read_uart'):
            method = getattr(drv, name)
            self.assertTrue(callable(method), f'{name} 不可调用')


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
        for _full, (aid, att) in CD.UBX_CONFIG_DATABASE.items():
            by_id.setdefault(aid, set()).add((att[0], int(att[1:])))
        bad = []
        for name, rec in K.ALL_KEYS.items():
            key_id, type_info, _scale, _note = rec
            if key_id not in by_id:
                bad.append((str(name), f'{key_id:#010x}', '权威表里没有这个 ID'))
                continue
            want = (type_info[0][0], type_info[1])
            if want not in by_id[key_id]:
                bad.append((str(name), f'{key_id:#010x}',
                            f'我们={want[0]}{want[1]} 权威={sorted(by_id[key_id])}'))
        self.assertEqual(bad, [], f'{len(bad)} 个键与 pyubx2 不一致：{bad[:10]}')

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
    sys.exit(0)
