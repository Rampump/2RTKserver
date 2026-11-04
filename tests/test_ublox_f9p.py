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

# 帧里出现的 (cls, id) 二元组，写短名省得每处都带 `K.` 前缀
VALSET = K.MSG_CFG_VALSET
VALGET = K.MSG_CFG_VALGET


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

    def test_valset_transaction_drives_version_byte(self):
        """`transaction != 0` 时 version 字节要变 1（pyubx2 config_set 的规则）。

        `version` 和 `transaction` 是两个字节，光看 transaction 位看不出
        有没有跟着改 version —— 服务器漏改这一位时，模块仍会 ACK，
        但事务语义根本没生效。所以两个字节都要钉。
        """
        seen = []

        def on_write(data):
            seen.append(bytes(data))
            return _ack_ack()

        drv, _ch, _s = make_f9p(on_write)

        drv._valset([('TMODE_MODE', 0)])
        body = seen[-1][6:-2]                  # 去掉 B5 62 同步头 + 长度 + 校验
        self.assertEqual(body[0], 0x00, 'transaction=0 时 version 必须是 0')
        self.assertEqual(body[2], 0x00)

        seen.clear()
        drv._valset([('TMODE_MODE', 0)], transaction=7)
        body = seen[-1][6:-2]
        self.assertEqual(body[0], 0x01, 'transaction!=0 时 version 必须变 1')
        self.assertEqual(body[2], 7)

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
            'GGA': 'NMEAMOUT_GGA_UART1',
            'NMEA-GGA': 'NMEAMOUT_GGA_UART1',
            'GPGGA': 'NMEAMOUT_GGA_UART1',   # 带 talker 前缀
            'RMC': 'NMEAMOUT_RMC_UART1',
            'RAWX': 'RXMOUT_RXM_RAWX_UART1',
            'RXM-RAWX': 'RXMOUT_RXM_RAWX_UART1',
            'NAVSVIN': 'NAVOUT_NAV_SVIN_UART1',
            'NAV_SVIN': 'NAVOUT_NAV_SVIN_UART1',
            'RTCM3-1005': 'RTCM_1005',
            '1005': 'RTCM_1005',
        }
        for name, want in cases.items():
            ok, msg, data = drv.set_message_rate(name, 1)
            self.assertTrue(ok, f'{name}: {msg}')
            self.assertEqual(data['key'], want, name)
            self.assertEqual(K.lookup(want)[2], 1.0, f'{want} 的 scale')
        # 不能断言"写了几帧"：`_valset()` 带写后回读，回读不符会重发一次，
        # 帧数取决于假模块怎么答 ACK，和本用例要验的东西（名字解析）无关。
        self.assertTrue(seen, '一条 VALSET 都没发出去')
        self.assertEqual(len(cases), 10, '用例表本身少了/多了')

    def test_resolve_msg_key_shares_one_parser(self):
        """`set_message_rate` 与 `get_message_rate` 必须用同一个解析器。

        以前两处各抄一份 if/elif，加一种报文就得改两个地方，
        漏一个的表现是"能设不能读"——这种 bug 很难从现象反推。
        """
        # `_resolve_msg_key()` 给的是**裸键名**（不带端口），端口后缀由
        # `_port_key()` 补。这里钉住"裸名"这一层契约，端口维度另有测试覆盖。
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

    def test_get_mode_returns_triple_and_keeps_svin_detail(self):
        """`get_mode()` 必须回三元组，且不能把 survey_in 和 fixed 都压成 base。

        曾经 `return 'base'`（裸字符串），流程层 `mode.get('mode')` 直接
        AttributeError —— 崩在 ok 已经是真值之后，报错信息跟驱动层毫无关系。
        这里同时钉死三级口径：`mode` 给流程层的 rover/base，
        `svin_mode` 给前端的 survey_in/fixed，两个字段都要有。

        还要钉死 `mode_code` 的**跨型号口径**：必须是 1=rover / 2=base，
        不能直接透传 TMODE 枚举。TMODE=1 是 Survey-in（基站），透传出去
        会让 `backup.usable()` 和 `writer` 都当成流动站，一块已经配成
        基站的模块会被拉去连 CORS。原始值只能放在 `tmode_code` 里。
        """
        for code, want_svin, want_mode, want_code in (
                (0, 'disabled', 'rover', 1),
                (1, 'survey_in', 'base', 2),
                (2, 'fixed', 'base', 2)):
            payload = struct.pack('<I', K.lookup('TMODE_MODE')[0])
            payload += bytes([code])

            def on_write(data, _p=payload):
                cls, msg_id, _pl = ubx_parse(data)
                if (cls, msg_id) == VALSET:
                    return _ack_ack()
                if (cls, msg_id) == VALGET:
                    return ubx_frame(0x06, 0x8B, bytes([0x01, 0x00, 0x00, 0x00]) + _p)
                return b''

            drv, _ch, _s = make_f9p(on_write)
            result = drv.get_mode()
            self.assertIsInstance(result, tuple, f'TMODE_MODE={code} 返回了裸值 {result!r}')
            self.assertEqual(len(result), 3)
            ok, _message, data = result
            self.assertIsInstance(data, dict)
            self.assertTrue(ok)
            self.assertEqual(data['mode'], want_mode)
            self.assertEqual(data['mode_code'], want_code,
                             f'TMODE_MODE={code} 的 mode_code 必须报跨型号角色码 '
                             f'{want_code}（1=rover 2=base），不能透传 TMODE '
                             f'（透传值 {code} 会让 backup/writer 认错角色）')
            self.assertEqual(data['tmode_code'], code,
                             f'TMODE_MODE={code} 的原始枚举要原样留在 tmode_code')
            self.assertEqual(data['svin_mode'], want_svin,
                             f'TMODE_MODE={code} 应报 {want_svin}，'
                             f'报成 {data.get("svin_mode")} 会让前端分不清收敛中还是定点')

    def test_get_mode_without_reply_is_unknown_triple(self):
        """读不到 TMODE_MODE 时也必须回三元组，`ok=False`，不能抛。"""
        drv, _ch, _s = make_f9p()
        ok, message, data = drv.get_mode()
        self.assertFalse(ok)
        self.assertEqual(data['mode'], 'unknown')
        self.assertEqual(data['error'], 'timeout')

    def test_windows_bitlen_is_ten_bits_per_byte(self):
        """`windows_bitlen` 按 8N1 的 10 bit/字节 折算一个超时窗口的容量。

        38400 → 3840 字节/秒。这个数只做诊断和批量下发的间隔估算，
        **不**用来调超时——超时随流量抖动会更难查。
        """
        self.assertEqual(F9P.windows_bitlen(38400), 3840)
        self.assertEqual(F9P.windows_bitlen(921600), 92160)
        self.assertEqual(F9P.windows_bitlen(0), 0)
        self.assertEqual(F9P.windows_bitlen(-1), 0)
        self.assertEqual(F9P.windows_bitlen(None), 0)
        self.assertEqual(F9P.windows_bitlen('115200'), 11520)

    def test_valget_uses_poll_layer_not_set_layer(self):
        """VALGET 的 layer 是 poll 口径（0=RAM 1=BBR 2=Flash），不是 VALSET 那套。

        `ubxconfigdb.py` 顶部注释明说 "SET and DEL use different memory layer
        values to POLL"。两套都有个 1，含义却从 RAM 变成 BBR —— 拿 set 常量
        去 poll，读 BBR 会读成 RAM、读 Flash 会读成"默认值"，**读回来的数
        看着正常，却根本不是那一层**，这是最难在真机上发现的一类错。
        """
        seen = []

        def on_write(data, _s=seen):
            cls, msg_id, payload = ubx_parse(data)
            if (cls, msg_id) == VALGET:
                _s.append(bytes(payload))
            return b''

        drv, _ch, _s = make_f9p(on_write)

        # 默认（不传 layer）必须是 poll RAM = 0x00，而不是 set RAM = 0x01
        drv._valget(['TMODE_MODE'])
        self.assertEqual(seen[-1][1], 0x00,
                         f'默认层应为 poll RAM(0x00)，实际 {seen[-1][1]:#04x}')

        # 显式读 Flash 层：poll 口径是 2
        seen.clear()
        drv._valget(['TMODE_MODE'], layer=K.POLL_LAYER_FLASH)
        self.assertEqual(seen[-1][1], 0x02)

        # 误传 VALSET 的 LAYER_FLASH(4) 必须当场拦下，而不是发出一个非法请求
        seen.clear()
        with self.assertRaises(Exception) as ctx:
            drv._valget(['TMODE_MODE'], layer=K.LAYER_FLASH)
        self.assertIn('poll', str(ctx.exception))
        self.assertEqual(seen, [], '非法 layer 不该真的发出去')

    def test_verify_persisted_compares_per_key(self):
        """`verify_persisted` 按键比较，不按整表比。

        模块对没写过的键会回默认值；整表比会把"这个键本来就是默认值"
        误判成失败，也会把"少回了一个键"当成"值不符"。
        """
        mode_key = K.lookup('TMODE_MODE')[0]
        rtcm_key = K.lookup('RTCM_1005')[0]

        def on_write(data):
            cls, msg_id, _pl = ubx_parse(data)
            if (cls, msg_id) == VALGET:
                # 应答带 version|layer|position 头，layer 回 Flash(2) 表示取自耗时层
                body = bytes([0x01, 0x02, 0x00, 0x00])
                body += struct.pack('<IB', mode_key, 2)      # TMODE=fixed
                body += struct.pack('<IB', rtcm_key, 1)      # 1005 开
                return ubx_frame(0x06, 0x8B, body)
            return b''

        drv, _ch, _s = make_f9p(on_write)
        ok, message, data = drv.verify_persisted({'TMODE_MODE': 2, 'RTCM_1005': 1})
        self.assertTrue(ok, message)
        self.assertEqual(data['mismatch'], {})

        # 值不符要报出来，并指出是哪个键
        ok, message, data = drv.verify_persisted({'TMODE_MODE': 2, 'RTCM_1005': 5})
        self.assertFalse(ok)
        self.assertIn('RTCM_1005', data['mismatch'])
        self.assertNotIn('TMODE_MODE', data['mismatch'])

        # 少回一个键是"缺失"，不能混进"值不符"
        ok, message, data = drv.verify_persisted({'TMODE_MODE': 2, 'RTCM_1230': 1})
        self.assertFalse(ok)
        self.assertIn('RTCM_1230', data['mismatch'])

    def test_verify_persisted_without_reply_fails_softly(self):
        """读不到 Flash 层时必须回三元组 `ok=False`，不能抛。"""
        drv, _ch, _s = make_f9p()
        ok, message, data = drv.verify_persisted({'TMODE_MODE': 2})
        self.assertFalse(ok)
        self.assertEqual(data['error'], 'timeout')

    def test_optional_surface_never_raises(self):
        """流程层会无条件调用每个可选方法，一个漏实现就是一个 500。"""
        drv, _ch, _s = make_f9p()
        for name in ('get_svin_config', 'read_rtcm_config', 'restore_rtcm_config',
                     'config_rtcm', 'get_message_rate', 'set_message_rate',
                     'disable_survey_in', 'read_antenna_info', 'save', 'reboot',
                     'get_mode', 'get_fixed_ecef', 'survey_status', 'read_uart',
                     'enable_survey_status'):
            method = getattr(drv, name)
            self.assertTrue(callable(method), f'{name} 不可调用')

    def test_survey_status_provides_flow_field_names(self):
        """`survey_status()` 的字段名与单位必须对上流程层的读法。

        `_step_svin_wait()` 读的是 `mean_acc`（**米**）和 `mean_x/y/z`。
        驱动只给 `mean_acc_mm` + `x/y/z` 时，真机表现依次是：
          ① `当前 3D 精度 ? m` —— 收敛判据拿不到值，永远判不收敛直到超时；
          ② 就算 obs 够了，也会抛"没有拿到任何有效收敛均值（观测数为 0）"，
             而模块里其实是好好的。这是与 `count` / `ecef_x` 同类的名字错配。
        """
        # 载荷：version(1)+reserved(3)+iTOW(4)+dur(4)
        #       +meanX/Y/Z(4×4)+meanHP(1×3)+reserved(1)+meanAcc(4)+obs(4)
        #       +valid(1)+active(1)
        body = bytearray(40)
        body[0] = 1
        struct.pack_into('<I', body, 8, 600)          # dur = 600s
        struct.pack_into('<i', body, 12, -2221810)    # meanX cm
        struct.pack_into('<i', body, 16, 5173277)     # meanY cm
        struct.pack_into('<i', body, 20, 2986962)     # meanZ cm
        body[24:27] = bytes([0, 0, 0])                # HP
        struct.pack_into('<I', body, 28, 25)          # meanAcc = 25 × 0.1mm = 2.5mm
        struct.pack_into('<I', body, 32, 600)         # obs
        body[36] = 1                                  # valid
        body[37] = 0                                  # active

        frame = ubx_frame(0x01, 0x3B, bytes(body))

        drv, ch, ser = make_f9p(lambda _d: _ack_ack())
        # NAV-SVIN 是**周期输出**，不由我们发命令触发 —— `survey_status()` 只读缓存，
        # 读不到就如实报 `no_nav_svin`（这正是它在真机上该有的行为，不能为了好测就
        # 改成"顺手发个 VALGET"）。所以这里得模拟总线上已经有一帧在流：
        # 直接把它塞进假串口的接收缓冲，效果等同于模块自己推了一帧过来。
        ser._rx += frame
        ok, message, st = drv.survey_status()
        self.assertTrue(ok, message)

        # ① 米/毫米两套都在，值一致
        self.assertAlmostEqual(st['mean_acc_mm'], 2.5, places=3)
        self.assertAlmostEqual(st['mean_acc'], 0.0025, places=6,
                               msg='流程层按“米”读 mean_acc')
        # ② 收敛均值两套名字都在
        for alias in ('mean_x', 'mean_y', 'mean_z'):
            self.assertIn(alias, st, f'流程层要读 {alias}')
        self.assertAlmostEqual(st['mean_x'], st['x'])
        self.assertAlmostEqual(st['mean_x'], -22218.10, places=2)
        # ③ 基本状态
        self.assertTrue(st['valid'])
        self.assertEqual(st['obs'], 600)
        self.assertEqual(st['duration_s'], 600)


        """Survey-in 的状态源是 NAV-SVIN，必须真的把它的 MSGOUT 打开。

        流程层用 `getattr(driver, 'enable_survey_status', None)` 探测，缺失时
        **静默跳过**。真机踩过：方法不存在 → NAV-SVIN 速率恒 0 →
        `survey_status()` 永远 `no_nav_svin` → SVIN_WAIT 干等 20 秒后报
        "Survey-in 未收敛（0/600）"，看起来像信号问题，其实状态输出没开。

        另外必须写在**当前链路的口**上：MSGOUT 键按端口分，写错口会安静 ACK。
        """
        written = []

        def on_write(data):
            data = bytes(data)
            if data[2:4] == bytes(VALSET):
                payload = data[6:6 + struct.unpack('<H', data[4:6])[0]]
                for i in range(4, len(payload) - 4, 5):
                    written.append((struct.unpack('<I', payload[i:i + 4])[0],
                                    payload[i + 4]))
            return _ack_ack()

        drv, _ch, _s = make_f9p(on_write)
        ok, message, data = drv.enable_survey_status(True, 1)
        self.assertTrue(ok, message)
        self.assertEqual(data['msg'], 'NAV-SVIN')

        sv_keys = {K.lookup(n)[0]: n for n in
                   ('NAVOUT_NAV_SVIN_I2C', 'NAVOUT_NAV_SVIN_UART1',
                    'NAVOUT_NAV_SVIN_UART2', 'NAVOUT_NAV_SVIN_USB',
                    'NAVOUT_NAV_SVIN_SPI')}
        hit = [(sv_keys[k], v) for k, v in written if k in sv_keys]
        self.assertTrue(hit, '没有写任何 NAV-SVIN 的 MSGOUT 键')
        for name, value in hit:
            self.assertEqual(value, 1, f'{name} 应写 1')
        # 只写一个口，不是五个口全写
        self.assertEqual(len(hit), 1, f'只应写当前链路口，实际写了 {hit}')

        # 关闭走同一路径，值 0
        written.clear()
        ok, _message, _data = drv.enable_survey_status(False, 1)
        self.assertTrue(ok)
        hit = [(sv_keys[k], v) for k, v in written if k in sv_keys]
        self.assertEqual([v for _n, v in hit], [0], '关闭应写 0')

    def test_get_svin_config_provides_flow_field_names(self):
        """`get_svin_config()` 必须同时给出流程层读取的字段名。

        真机踩过两次，都是"写对了却判失败"：
          ① `x/y/z` 没有 `ecef_x/y/z` → `_compare_fixed()` 全取到 None，
             报"最大轴差 5028827 m"；`_revert()` 里还抛过
             `float() argument must be ... not 'NoneType'`。
          ② `svin_min_dur` 没有 `count` → `_svin_config_verified()` 判
             `None != 600`，子收敛流程在 SVIN_CFG 就判失败并回滚，
             而模块里 `TMODE_SVIN_MIN_DUR` 明明是 600。

        这里钉死"两套名字都在、且值一致"，避免第三次。
        """
        mode_key = K.lookup('TMODE_MODE')[0]
        pos_key = K.lookup('TMODE_POS_TYPE')[0]
        dur_key = K.lookup('TMODE_SVIN_MIN_DUR')[0]
        acc_key = K.lookup('TMODE_SVIN_ACC_LIMIT')[0]
        x_key = K.lookup('TMODE_ECEF_X')[0]

        answer = {
            mode_key: struct.pack('<B', 1),            # survey_in
            pos_key: struct.pack('<B', 0),
            dur_key: struct.pack('<I', 600),
            acc_key: struct.pack('<I', 30000),         # 原始 0.1mm → 3000 mm
            x_key: struct.pack('<i', -2221810),        # cm
        }

        def on_write(data):
            data = bytes(data)
            if data[2:4] == bytes(VALGET):
                payload = data[6:6 + struct.unpack('<H', data[4:6])[0]]
                layer, keys = payload[1], [struct.unpack('<I', payload[i:i + 4])[0]
                                           for i in range(4, len(payload) - 3, 4)]
                body = bytearray([0x01, layer, 0x00, 0x00])
                for key_id in keys:
                    body += struct.pack('<I', key_id)
                    body += answer.get(key_id, struct.pack('<I', 0))
                return ubx_frame(*VALGET, bytes(body)) + _ack_ack()
            return _ack_ack()

        drv, _ch, _s = make_f9p(on_write)
        ok, message, cfg = drv.get_svin_config()
        self.assertTrue(ok, message)

        # ① ECEF 两套名字
        for alias in ('ecef_x', 'ecef_y', 'ecef_z'):
            self.assertIn(alias, cfg, f'流程层要读 {alias}')
        self.assertAlmostEqual(cfg['ecef_x'], -22218.10, places=2)
        self.assertAlmostEqual(cfg['x'], cfg['ecef_x'])

        # ② Survey-in 两套名字 + 单位
        self.assertEqual(cfg['mode'], 'survey_in')
        self.assertEqual(cfg['svin_min_dur'], 600)
        self.assertEqual(cfg['count'], 600,
                         '流程层 _svin_config_verified() 读的是 count')
        self.assertAlmostEqual(cfg['svin_acc_limit_mm'], 3000.0)   # 物理 mm
        self.assertAlmostEqual(cfg['acc'], 3.0)                    # 流程层口径：米


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


# ==================== 5. 端口维度（MSGOUT 键按口拆分） ====================
class TestPortDimension(unittest.TestCase):
    """**MSGOUT 键是分端口的**，这一节钉死这个事实。

    驱动早期的 bug：一律写 `_UART1` 后缀键。真机链路其实是 USB CDC
    （COM12 = VID:PID 1546:01A9），模块对写错的端口**照样回 ACK**，
    但 USB 口上的 NAV-PVT/NAV-SAT 一个字节没少 —— 症状是"关不掉输出"，
    而日志里全绿，极难从现象反推。
    """

    def test_port_suffix_table_matches_offsets(self):
        """端口偏移固定：I2C → UART1 → UART2 → USB → SPI，逐个 +1。"""
        for name, ids in K.NAV_OUT_PORTS.items():
            seq = [ids[p] for p in ('I2C', 'UART1', 'UART2', 'USB', 'SPI')]
            self.assertNotIn(None, seq, name)
            self.assertEqual(seq, [seq[0] + i for i in range(5)], f'{name} 端口 ID 不连续')

    def test_zda_uart1_is_not_i2c_plus_one_typo(self):
        """`ZDA` 的 UART1 曾抄成 0x209100D7（那是 `GST` 的 SPI 位）。

        权威序列是 I2C 0xd8 / UART1 0xd9 / UART2 0xda / USB 0xdb / SPI 0xdc。
        抄错的后果：写下去的速率作用在别的报文上，ZDA 永远关不掉。
        """
        self.assertEqual(K.lookup_msgout('NMEA', 'ZDA', 'UART1'), 0x209100D9)
        self.assertEqual(K.lookup_msgout('NMEA', 'ZDA', 'I2C'), 0x209100D8)

    def test_full_msgout_coverage(self):
        """三族的条数必须与固件一致：NAV 40 / RXM 18 / NMEA 15。"""
        self.assertEqual(len(K.NAV_OUT_PORTS), 40)
        self.assertEqual(len(K.RXM_OUT_PORTS), 18)
        self.assertEqual(len(K.NMEA_OUT_PORTS), 15)

    def test_missing_port_key_is_none_not_zero(self):
        """`None` 表示"固件里没这个键"，**绝对不是**"值是 0"。

        RXM-MEAS20/50/MEASC12/MEASD12 只给了 I2C/UART1/SPI 三个口。
        把 None 当 0 去写，写的是一个不存在的键 —— 要么 NAK 要么静默。
        """
        for name in ('MEAS20', 'MEAS50', 'MEASC12', 'MEASD12'):
            self.assertIsNone(K.lookup_msgout('RXM', name, 'USB'), name)
            self.assertIsNone(K.lookup_msgout('RXM', name, 'UART2'), name)
            self.assertIsNotNone(K.lookup_msgout('RXM', name, 'UART1'), name)

    def test_port_key_appends_correct_suffix(self):
        drv, _ch, _s = make_f9p()
        self.assertEqual(drv._port_key('NAVOUT_NAV_PVT', 'UART1'), 'NAVOUT_NAV_PVT_UART1')
        self.assertEqual(drv._port_key('NAVOUT_NAV_PVT', 'USB'), 'NAVOUT_NAV_PVT_USB')
        self.assertEqual(drv._port_key('NAVOUT_NAV_PVT', 'UART2'), 'NAVOUT_NAV_PVT_UART2')
        self.assertEqual(drv._port_key('NAVOUT_NAV_PVT', 'I2C'), 'NAVOUT_NAV_PVT_I2C')
        self.assertEqual(drv._port_key('NAVOUT_NAV_PVT', 'SPI'), 'NAVOUT_NAV_PVT_SPI')
        # RTCM 键没有端口维度，原样返回
        self.assertEqual(drv._port_key('RTCM_1005', 'USB'), 'RTCM_1005')
        with self.assertRaises(F9P.DriverBug):
            drv._port_key('NAVOUT_NAV_PVT', 'COM9')

    def test_every_port_key_resolves_to_real_id(self):
        """`_port_key()` 拼出来的名字必须都能在键表里查到真 ID。

        查不到就说明拼错了（比如少了 `NAV_` 前缀），`silence_outputs()`
        会静默跳过这条 —— 表现又是"关不掉"。
        """
        drv, _ch, _s = make_f9p()
        for prefix, table, np in (('NAVOUT_', K.NAV_OUT_PORTS, 'NAV_'),
                                  ('RXMOUT_', K.RXM_OUT_PORTS, 'RXM_'),
                                  ('NMEAMOUT_', K.NMEA_OUT_PORTS, '')):
            for name, ids in table.items():
                for port, key_id in ids.items():
                    if key_id is None:
                        continue
                    full = drv._port_key(f'{prefix}{np}{name}', port)
                    rec = K.lookup(full)
                    self.assertIsNotNone(rec, f'{full} 查不到')
                    self.assertEqual(rec[0], key_id, full)


class TestDetectPort(unittest.TestCase):
    """`detect_port()` 用 MON-COMMS 问模块"你在哪个口跟我说话"。"""

    @staticmethod
    def _comms_payload(port_stats):
        """造一条 MON-COMMS：port_stats = {portId: [msgs_rx, msgs_tx, ...]}。"""
        n = 5
        head = bytes([0x00, 0x00, n, 0x00, 0x00])
        recs = b''
        for pid in range(n):
            rec = bytearray(40)
            rec[0] = pid
            counts = port_stats.get(pid, [0, 0, 0, 0])
            for i, c in enumerate(counts[:4]):
                rec[24 + i * 2: 26 + i * 2] = int(c).to_bytes(2, 'little')
            recs += bytes(rec)
        return head + recs

    def test_picks_port_with_traffic(self):
        """只有 USB 口有收发，就必须认成 USB。"""
        payload = self._comms_payload({3: [10, 20, 0, 0]})
        got = F9P.ZEDF9PDriver._active_port_from_comms(payload)
        self.assertEqual(got, 3, 'portId 3 = USB')

    def test_prefers_uart1_when_that_has_traffic(self):
        payload = self._comms_payload({1: [5, 5, 0, 0]})
        self.assertEqual(F9P.ZEDF9PDriver._active_port_from_comms(payload), 1,
                         'portId 1 = UART1')

    def test_rejects_garbage(self):
        """长度不对 / nPorts 离谱的载荷必须拒掉，不能瞎猜。"""
        self.assertIsNone(F9P.ZEDF9PDriver._active_port_from_comms(b'\x00\x00\x00'))
        self.assertIsNone(F9P.ZEDF9PDriver._active_port_from_comms(
            bytes([0x00, 0x00, 0xFF, 0])))
        self.assertIsNone(F9P.ZEDF9PDriver._active_port_from_comms(b''))

    def test_name_fallback_when_no_comms(self):
        """问不到 MON-COMMS 时按口名兜底，并如实标 `source='name'`。

        `COM*` 无从知道背后是 USB 还是 UART，但 F9P 模块自带的 USB 接上电脑
        是最常见的情形，认成 USB 的期望收益最高。来源必须标出来，
        免得调用方以为这是模块给的确定答案。
        """
        drv, _ch, _s = make_f9p()
        drv.channel.port = 'COM12'
        port, _msg, data = drv.detect_port()
        self.assertEqual(port, 'USB')
        self.assertEqual(data['source'], 'name')

    def test_detect_port_caches(self):
        drv, _ch, _s = make_f9p()
        drv._port = 'UART1'
        _p, _m, data = drv.detect_port()
        self.assertEqual(data['source'], 'cache')
        self.assertEqual(drv._port, 'UART1')


class TestUsbLinkHasNoBaud(unittest.TestCase):
    """USB CDC 没有波特率。绝不能因为链路是 USB 就去写 `UART1_BAUDRATE` ——
    那样 UART1 那个真实物理口会被悄悄改成别的速度，接电台时直接不通。"""

    def test_baud_key_is_none_on_usb(self):
        drv, _ch, _s = make_f9p()
        drv._port = 'USB'
        self.assertIsNone(drv._baud_key())

    def test_baud_key_uart1_and_uart2(self):
        drv, _ch, _s = make_f9p()
        drv._port = 'UART1'
        self.assertEqual(drv._baud_key(), 'UART1_BAUDRATE')
        drv._port = 'UART2'
        self.assertEqual(drv._baud_key(), 'UART2_BAUDRATE')

    def test_read_uart_reports_usb_honestly(self):
        drv, _ch, _s = make_f9p()
        drv._port = 'USB'
        ok, _msg, data = drv.read_uart()
        self.assertTrue(ok)
        self.assertEqual(data['port'], 'USB')
        self.assertIsNone(data['baud'], 'USB 口不能编一个波特率出来')
        self.assertIn('note', data)

    def test_set_baud_refuses_on_usb(self):
        drv, _ch, _s = make_f9p()
        drv._port = 'USB'
        ok, _msg, data = drv.set_baud(115200)
        self.assertFalse(ok)
        self.assertEqual(data['error'], 'unsupported')

    def test_get_baud_refuses_on_usb(self):
        drv, _ch, _s = make_f9p()
        drv._port = 'USB'
        ok, _msg, data = drv.get_baud()
        self.assertFalse(ok)
        self.assertEqual(data['error'], 'unsupported')


class TestSilenceOutputs(unittest.TestCase):
    """`silence_outputs()` 必须真的往"链路所在的那个口"写 0。

    用能回 VALGET 的假模块：真 F9P 上 VALSET 的 ACK 只代表"消息收下了"，
    不代表值换了，所以驱动在写 0 之前先 VALGET 一遍现状（只有真开着的才写，
    省 Flash 擦写），写完再 VALGET 一遍确认。假模块不回 VALGET 的话，
    这一步直接 timeout 返回 False —— 本用例的价值就在这里。
    """

    def _open_all(self, *keys):
        """把指定的键先置成"开着"（=1），这样 silence 才有东西可关。"""
        return {K.lookup(k)[0]: 1 for k in keys if K.lookup(k)}

    def test_defaults_to_detected_port_not_uart1(self):
        drv, _ch, _s = make_f9p_with_valget(
            self._open_all('NMEAMOUT_GGA_USB', 'NMEAMOUT_GSV_USB',
                           'NAVOUT_NAV_PVT_USB'))
        drv._port = 'USB'
        ok, msg, data = drv.silence_outputs(persist=False)
        self.assertTrue(ok, msg)
        self.assertEqual(data['ports'], ['USB'])
        # `_port` 已缓存时 detect_port() 返回 source='cache'，不是 'explicit'：
        # 'explicit' 只在调用方显式传 ports 时出现。
        self.assertEqual(data['port_source'], 'cache')
        self.assertIn('NMEAMOUT_GGA_USB', data['turned_off'])
        self.assertIn('NAVOUT_NAV_PVT_USB', data['turned_off'])
        # **绝不能**碰 UART1 的键：那是另一个物理口，可能接着电台
        self.assertEqual([k for k in data['turned_off'] if k.endswith('_UART1')], [])

    def test_keep_skips_that_message(self):
        drv, _ch, _s = make_f9p_with_valget(
            self._open_all('NMEAMOUT_GGA_USB', 'NAVOUT_NAV_PVT_USB',
                           'NAVOUT_NAV_SVIN_USB'))
        drv._port = 'USB'
        ok, msg, data = drv.silence_outputs(keep=('NAV_PVT', 'GGA'), persist=False)
        self.assertTrue(ok, msg)
        written = [k for k in data['turned_off'] if 'NAVOUT_NAV_PVT' in k]
        self.assertEqual(written, [], 'NAV_PVT 在 keep 里，不该被关')
        self.assertEqual([k for k in data['turned_off'] if 'NMEAMOUT_GGA' in k], [])
        self.assertIn('NAVOUT_NAV_SVIN_USB', data['turned_off'])
        self.assertIn('NMEAMOUT_GGA_USB', data['kept'])
        self.assertIn('NAVOUT_NAV_PVT_USB', data['kept'])

    def test_already_off_not_counted_as_turned_off(self):
        """本来就是 0 的键不能算"关掉了"，否则用户以为白折腾一场。"""
        drv, _ch, _s = make_f9p_with_valget(self._open_all('NMEAMOUT_GGA_USB'))
        drv._port = 'USB'
        ok, msg, data = drv.silence_outputs(persist=False)
        self.assertTrue(ok, msg)
        self.assertIn('NMEAMOUT_GGA_USB', data['turned_off'])
        self.assertIn('NAVOUT_NAV_PVT_USB', data['already_off'])

    def test_bad_port_raises(self):
        drv, _ch, _s = make_f9p_with_valget()
        with self.assertRaises(F9P.DriverBug):
            drv.silence_outputs(ports=('COM9',), persist=False)


# ==================== 6. 假模块：能回 VALGET 的最小实现 ====================
def make_valget_answer(request_payload, values=None):
    """按一条 VALGET 请求造对应的应答帧。

    `values` = {key_id: 当前速率}，缺省一律回 0（"本来就是关的"）。
    U1 类型的键回 1 字节。
    """
    layer = request_payload[1]
    keys = [struct.unpack('<I', request_payload[i:i + 4])[0]
            for i in range(4, len(request_payload) - 3, 4)]
    body = bytearray([0x01, layer, 0x00, 0x00])
    for key_id in keys:
        body += struct.pack('<I', key_id)
        body += bytes([(values or {}).get(key_id, 0) & 0xFF])
    return ubx_frame(*VALGET, bytes(body))


def make_f9p_with_valget(initial=None):
    """假模块：VALSET 回 ACK，VALGET 回一条带当前值的应答。"""
    state = dict(initial or {})

    def on_write(data):
        data = bytes(data)
        if data[2:4] == bytes(VALGET):
            payload = data[6:6 + struct.unpack('<H', data[4:6])[0]]
            return make_valget_answer(payload, state) + _ack_ack()
        if data[2:4] == bytes(VALSET):
            payload = data[6:6 + struct.unpack('<H', data[4:6])[0]]
            for i in range(4, len(payload) - 4, 5):
                key_id = struct.unpack('<I', payload[i:i + 4])[0]
                state[key_id] = payload[i + 4]
            return _ack_ack()
        return _ack_ack()

    return make_f9p(on_write)
