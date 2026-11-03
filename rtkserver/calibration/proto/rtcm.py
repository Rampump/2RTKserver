# -*- coding: utf-8 -*-
"""RTCM3 轻量解码：解 1005 / 1006（基准站坐标），供标定收尾自检用。

为什么不去用全局 `rtcm_parser`：
    那个模块属于"数据面"，靠 `state.rtcm_buffer` 驱动，而且此刻已被租约停掉。
    标定要的是"我刚往串口里读了 2 秒，模块到底播了什么坐标"，
    必须能同步、按需、拿一批字节就出结果，所以这里自带走帧+解码。
    CRC24Q 与解帧**复用全局实现**，不重复造轮子（抄两份 CRC 表迟早对不上）。

1005 与 1006 的关系（RTCM 10403.3）：
    1006 = 1005 的全部字段 + DF027 天线高度（16 bit，0.1 mm）。
    所以能播 1006 的模块不必再播 1005 —— 两者都带完整的 ECEF，
    标定只关心"写进去的坐标有没有真的播出来"，任一条都能用。
    MSM 消息仍不在这里解（只数帧数，不解析观测值）。

自测（不需要硬件）：
    python -m rtkserver.calibration.proto.rtcm

硬件验证（插一块模块，看它到底在播什么）：
    python -m rtkserver.calibration.proto.rtcm --port /dev/ttyUSB0 --seconds 10
    注意：主程序正在用这个口时会互抢字节，请先 systemctl stop 2rtkserver。
"""
import logging
import time

from ...sources.rtcm_parser import crc24q, extract_rtcm_frames

MSG_1005 = 1005
MSG_1006 = 1006
PAYLOAD_1005_LEN = 19          # 152 bit，刚好整除
PAYLOAD_1006_LEN = 21          # 168 bit = 1005 的 152 + 天线高度 16
STATION_COORD_TYPES = (MSG_1005, MSG_1006)
ECEF_RESOLUTION = 0.0001       # 1005/1006 的 ECEF 分辨率 0.1mm
ANTENNA_HEIGHT_RESOLUTION = 0.0001   # 1006 DF027 的分辨率 0.1mm


class BitReader:
    """按位读字节串。RTCM 的字段不按字节对齐，没有这个就只能手写移位。"""

    __slots__ = ('_data', '_pos')

    def __init__(self, data):
        self._data = data
        self._pos = 0

    @property
    def remaining(self):
        return len(self._data) * 8 - self._pos

    def read(self, bits):
        if bits <= 0:
            return 0
        if self.remaining < bits:
            raise ValueError(f'位流不足：需要 {bits} bit，剩余 {self.remaining} bit')
        value = 0
        data = self._data
        for _ in range(bits):
            byte = data[self._pos >> 3]
            value = (value << 1) | ((byte >> (7 - (self._pos & 7))) & 1)
            self._pos += 1
        return value


def _to_signed(value, bits):
    """无符号位域 → 有符号。38 位的 ECEF 靠这个才能表示南半球/负值。"""
    if value >= (1 << (bits - 1)):
        value -= (1 << bits)
    return value


def msg_type_of(payload):
    """取 RTCM 消息号（载荷前 12 bit）。载荷不足 2 字节时返回 None。"""
    if not payload or len(payload) < 2:
        return None
    return (payload[0] << 4) | (payload[1] >> 4)


def decode_station_coord(payload):
    """解 1005 / 1006 载荷，返回站点 ID、ITRF 年份标志与 ECEF 坐标（米）。

    字段顺序严格按 RTCM 10403.3（1006 只在末尾多一个 DF027 天线高度）：
        DF002(12) DF003(12) DF021(6) DF022(1) DF023(1) DF024(1) DF141(1)
        DF025(38) DF142(1) DF001(1) DF026(38) DF364(2) DF027(38)  = 152 bit
        1006 再追加 DF027 天线高度(16)                              = 168 bit
    """
    if payload is None or len(payload) < PAYLOAD_1005_LEN:
        raise ValueError(f'基准站坐标载荷长度不足：{0 if payload is None else len(payload)}')

    reader = BitReader(payload)
    message_type = reader.read(12)
    if message_type not in STATION_COORD_TYPES:
        raise ValueError(f'不是 1005/1006 报文：{message_type}')
    if message_type == MSG_1006 and len(payload) < PAYLOAD_1006_LEN:
        raise ValueError(f'1006 载荷长度不足：{len(payload)}')

    station_id = reader.read(12)
    itrf_year = reader.read(6)
    gps_indicator = reader.read(1)
    glonass_indicator = reader.read(1)
    galileo_indicator = reader.read(1)
    reference_station_indicator = reader.read(1)
    ecef_x = _to_signed(reader.read(38), 38) * ECEF_RESOLUTION
    single_receiver_oscillator = reader.read(1)
    reader.read(1)                                   # DF001 保留位
    ecef_y = _to_signed(reader.read(38), 38) * ECEF_RESOLUTION
    quarter_cycle_indicator = reader.read(2)
    ecef_z = _to_signed(reader.read(38), 38) * ECEF_RESOLUTION

    antenna_height = None
    if message_type == MSG_1006:
        antenna_height = reader.read(16) * ANTENNA_HEIGHT_RESOLUTION

    return {
        'msg_type': message_type,
        'station_id': station_id,
        'itrf_year': itrf_year,
        'gps_indicator': gps_indicator,
        'glonass_indicator': glonass_indicator,
        'galileo_indicator': galileo_indicator,
        'reference_station_indicator': reference_station_indicator,
        'single_receiver_oscillator': single_receiver_oscillator,
        'quarter_cycle_indicator': quarter_cycle_indicator,
        'ecef_x': ecef_x,
        'ecef_y': ecef_y,
        'ecef_z': ecef_z,
        'antenna_height': antenna_height,
    }


def decode_1005(payload):
    """只接受 1005（保留原名，老调用方与自测仍可用）。"""
    info = decode_station_coord(payload)
    if info['msg_type'] != MSG_1005:
        raise ValueError(f'不是 1005 报文：{info["msg_type"]}')
    return info


def decode_frame(frame):
    """整帧（含 0xD3 头与 CRC）→ 1005/1006 字典；其它类型或坏帧返回 None。"""
    if not frame or len(frame) < 6 + PAYLOAD_1005_LEN:
        return None
    payload = frame[3:-3]
    if msg_type_of(payload) not in STATION_COORD_TYPES:
        return None
    try:
        return decode_station_coord(payload)
    except ValueError as e:
        logging.debug(f'解码基准站坐标失败：{e}')
        return None


def find_1005(data, limit=None):
    """在一段字节里找出所有 1005（CRC 已由全局解帧器校验过）。

    返回解码后的字典列表；`limit` 用于"只要最近一条"的场景。
    """
    frames, _remainder = extract_rtcm_frames(bytes(data))
    result = []
    for frame in frames:
        info = decode_frame(frame)
        if info is not None:
            result.append(info)
            if limit is not None and len(result) >= limit:
                break
    return result


class Rtcm1005Scanner:
    """流式扫描器：串口 read() 会在帧中间断开，这里负责把半帧留住。

    用法：
        scanner = Rtcm1005Scanner()
        for chunk in chunks:
            for info in scanner.feed(chunk):
                ...
    """

    __slots__ = ('_buffer', 'max_buffer')

    def __init__(self, max_buffer=65536):
        self._buffer = b''
        # 防止喂进来一堆非 RTCM 垃圾（例如串口接错、波特率不对）时缓冲无限增长
        self.max_buffer = int(max_buffer)

    def feed(self, chunk):
        if chunk:
            self._buffer += bytes(chunk)
            if len(self._buffer) > self.max_buffer:
                logging.warning(f'RTCM 扫描缓冲超过 {self.max_buffer} 字节，丢弃最旧数据')
                self._buffer = self._buffer[-self.max_buffer:]
        if not self._buffer:
            return []
        frames, remainder = extract_rtcm_frames(self._buffer)
        self._buffer = remainder
        result = []
        for frame in frames:
            info = decode_frame(frame)
            if info is not None:
                result.append(info)
        return result

    def reset(self):
        self._buffer = b''

    @property
    def buffered(self):
        return len(self._buffer)


# ==================== 自测入口（不需要硬件） ====================
def _build_1005_frame(x, y, z, station_id=1234, itrf_year=0, quarter_cycle=0,
                      antenna_height=None):
    """按位拼一条合法 1005/1006 帧（仅自测用，生产环境只解不组）。

    `antenna_height` 给 None 就是 1005；给数值则拼 1006（末尾多 16 bit 天线高度）。
    """
    bits = []

    def put(value, width):
        mask = (1 << width) - 1
        value &= mask
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    def scaled(value):
        return int(round(value / ECEF_RESOLUTION))

    is_1006 = antenna_height is not None
    put(MSG_1006 if is_1006 else MSG_1005, 12)
    put(station_id, 12)
    put(itrf_year, 6)
    put(1, 1)            # GPS indicator
    put(1, 1)            # GLONASS indicator
    put(1, 1)            # Galileo indicator
    put(0, 1)            # reference station indicator
    put(scaled(x), 38)
    put(0, 1)            # single receiver oscillator
    put(0, 1)            # reserved
    put(scaled(y), 38)
    put(quarter_cycle, 2)
    put(scaled(z), 38)
    if is_1006:
        put(int(round(antenna_height / ANTENNA_HEIGHT_RESOLUTION)), 16)

    total_bits = 168 if is_1006 else 152
    assert len(bits) == total_bits, len(bits)
    payload = bytearray()
    for index in range(0, total_bits, 8):
        byte = 0
        for bit in bits[index:index + 8]:
            byte = (byte << 1) | bit
        payload.append(byte)

    head = bytes([0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF])
    body = head + bytes(payload)
    return body + crc24q(body).to_bytes(3, 'big')


def _selftest():
    """组帧→解码往返、负坐标、流式分片、坏 CRC、pyrtcm 交叉验证。

    跑法：python -m rtkserver.calibration.proto.rtcm
    """
    print('=== rtcm 自测 ===')

    # 1) 正坐标往返（取一个真实基准站量级的值）
    x, y, z = -2148744.1234, 4426641.5678, 4044655.9012
    frame = _build_1005_frame(x, y, z, station_id=1234)
    info = decode_1005(frame[3:-3])
    assert info['station_id'] == 1234
    assert abs(info['ecef_x'] - x) < 5e-5, info['ecef_x']
    assert abs(info['ecef_y'] - y) < 5e-5, info['ecef_y']
    assert abs(info['ecef_z'] - z) < 5e-5, info['ecef_z']
    print(f'[1] 往返 OK  X={info["ecef_x"]:.4f} Y={info["ecef_y"]:.4f} Z={info["ecef_z"]:.4f}')

    # 2) 负值（南半球/西半球）必须能正确解出，不能被当成无符号
    frame_s = _build_1005_frame(-5000000.0, -3000000.0, -2000000.0)
    info_s = decode_1005(frame_s[3:-3])
    assert abs(info_s['ecef_x'] + 5000000.0) < 5e-5, info_s['ecef_x']
    assert abs(info_s['ecef_z'] + 2000000.0) < 5e-5, info_s['ecef_z']
    print(f'[2] 负坐标 OK  X={info_s["ecef_x"]:.1f} Z={info_s["ecef_z"]:.1f}')

    # 3) 解帧 + CRC 校验：整帧能过
    decoded = find_1005(frame + frame_s)
    assert len(decoded) == 2, len(decoded)
    print(f'[3] 解帧 OK  一段字节里识别出 {len(decoded)} 条 1005')

    # 4) 坏 CRC 必须丢弃（否则总线噪声会被当成"坐标变了"）
    broken = bytearray(frame)
    broken[10] ^= 0xFF
    assert find_1005(bytes(broken)) == []
    print('[4] 坏 CRC 丢弃 OK')

    # 5) 非 1005 报文不能误报（伪造一条 1074 的帧头）
    fake = bytes([0xD3, 0x00, PAYLOAD_1005_LEN]) + bytes(PAYLOAD_1005_LEN) + b'\x00\x00\x00'
    assert find_1005(fake) == []
    payload_1074 = bytes([0x43, 0x20]) + bytes(PAYLOAD_1005_LEN - 2)   # 0x432 = 1074
    assert msg_type_of(payload_1074) == 1074
    assert decode_frame(b'\xd3\x00\x13' + payload_1074 + b'\x00\x00\x00') is None
    print('[5] 非 1005 忽略 OK')

    # 6) 流式分片：按 5 字节切，跨片也要能拼出来
    scanner = Rtcm1005Scanner()
    got = []
    stream = frame + frame_s
    for index in range(0, len(stream), 5):
        got.extend(scanner.feed(stream[index:index + 5]))
    assert len(got) == 2, (len(got), scanner.buffered)
    assert scanner.buffered == 0
    print('[6] 流式分片 OK')

    # 7) 垃圾数据不会让缓冲无限增长（伪造"长度声明 1023 但永远收不满"的帧头）
    scanner = Rtcm1005Scanner(max_buffer=4096)
    for _ in range(20):
        scanner.feed(b'\xd3\x03\xff' * 200)
    assert scanner.buffered <= 4096, scanner.buffered
    print(f'[7] 缓冲上限 OK  buffered={scanner.buffered}')

    # 8) 与 pyrtcm 交叉验证：同一帧两边解出来的坐标必须一致
    try:
        from io import BytesIO
        from pyrtcm import RTCMReader
    except ImportError:
        print('[8] pyrtcm 未安装，跳过交叉验证')
    else:
        matched = False
        with BytesIO(frame) as stream_io:
            for _raw, parsed in RTCMReader(stream_io):
                assert parsed.DF002 == 1005
                assert parsed.DF003 == 1234
                diff = max(abs(parsed.DF025 - info['ecef_x']),
                           abs(parsed.DF026 - info['ecef_y']),
                           abs(parsed.DF027 - info['ecef_z']))
                assert diff < 5e-5, diff
                print(f'[8] pyrtcm 交叉验证 OK  最大偏差={diff:.6f}m')
                matched = True
                break
        if not matched:
            print('[8] pyrtcm 未返回解析结果（跳过）')

    # 9) 1006：与 1005 同结构，只在末尾多 16 bit 天线高度。
    #    用户可以在页面上选"坐标报文 1006"，收尾校验必须认得它。
    f6 = _build_1005_frame(x, y, z, station_id=77, antenna_height=1.2345)
    i6 = decode_station_coord(f6[3:-3])
    assert i6['msg_type'] == MSG_1006, i6['msg_type']
    assert i6['station_id'] == 77
    assert abs(i6['ecef_x'] - x) < 5e-5 and abs(i6['ecef_z'] - z) < 5e-5
    assert abs(i6['antenna_height'] - 1.2345) < 5e-5, i6['antenna_height']
    assert decode_frame(f6)['msg_type'] == MSG_1006, 'decode_frame 必须认 1006'
    try:
        decode_1005(f6[3:-3])
        raise AssertionError('decode_1005 不该接受 1006')
    except ValueError:
        pass
    print(f'[9] 1006 解码 OK  坐标一致 + 天线高度 {i6["antenna_height"]:.4f} m')

    print('=== rtcm 自测全部通过 ===')
    return 0


def _sniff(port, baud=115200, seconds=10.0, show_nmea=True):
    """硬件验证入口：监听一个串口，把模块真实播出的 1005 与 NMEA 打出来。

    这是"插一块 LG290P 就能验证"的那条路：标定完想知道坐标到底写进去没有，
    不需要开主程序，直接看模块自己播的 1005 是不是你写的那个值。

    前提：主程序（2rtkserver.service）必须停掉。Linux 的 tty 不排他，
    两个 fd 同时 open 会随机瓜分字节，两边都收不全。
    """
    import serial

    from . import nmea

    logging.info(f'打开 {port} @ {baud}，监听 {seconds:.0f} 秒…')
    try:
        ser = serial.Serial(port=port, baudrate=baud, timeout=0.2)
    except Exception as e:
        print(f'打开串口失败：{e}')
        return 2

    scanner = Rtcm1005Scanner()
    nmea_buffer = b''
    frames_1005 = []
    nmea_counts = {}
    deadline = time.time() + max(1.0, float(seconds))

    try:
        ser.reset_input_buffer()
        while time.time() < deadline:
            chunk = ser.read(512)
            if not chunk:
                continue
            for info in scanner.feed(chunk):
                frames_1005.append(info)
                print(f'[1005] 站号={info["station_id"]}  '
                      f'X={info["ecef_x"]:.4f} Y={info["ecef_y"]:.4f} Z={info["ecef_z"]:.4f}')
            if show_nmea:
                lines, nmea_buffer = nmea.iter_sentences(nmea_buffer + chunk)
                for line in lines:
                    parsed = nmea.parse(line)
                    if not parsed['valid']:
                        continue
                    nmea_counts[parsed['type']] = nmea_counts.get(parsed['type'], 0) + 1
                    if parsed['type'] in ('GGA', 'GSA'):
                        print(f'[{parsed["type"]}] {line.strip()}')
    finally:
        try:
            ser.close()
        except Exception:
            pass

    print('--- 汇总 ---')
    print(f'1005 帧数：{len(frames_1005)}')
    if frames_1005:
        last = frames_1005[-1]
        from ..geo import ecef_to_geodetic
        lat, lon, height = ecef_to_geodetic(last['ecef_x'], last['ecef_y'], last['ecef_z'])
        print(f'最新坐标：{lat:.9f}, {lon:.9f}, 高程 {height:.4f} m')
    else:
        print('没有收到 1005：模块可能没进基站模式，或没开 RTCM3-1005 输出')
    if show_nmea:
        print(f'NMEA 统计：{nmea_counts or "无"}')
    return 0


def main(argv=None):
    """命令行入口：不给参数就跑离线自测；给 --port 就上硬件。"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog='python -m rtkserver.calibration.proto.rtcm',
        description='RTCM 1005 解码自测 / 串口监听（标定阶段0工具）')
    parser.add_argument('--port', help='串口设备，例如 /dev/ttyUSB0 或 COM5')
    parser.add_argument('--baud', type=int, default=115200, help='波特率，默认 115200')
    parser.add_argument('--seconds', type=float, default=10.0, help='监听时长，默认 10 秒')
    parser.add_argument('--no-nmea', action='store_true', help='只打 1005，不打 NMEA')
    args = parser.parse_args(argv)

    if args.port:
        return _sniff(args.port, args.baud, args.seconds, show_nmea=not args.no_nmea)
    return _selftest()


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
