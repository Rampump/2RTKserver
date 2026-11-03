# -*- coding: utf-8 -*-
"""NMEA 0183 通用组帧与解析（标定专用）。

用在哪：
    - 向 CORS 上报虚拟位置 → `build_gga()` 组帧；
    - 读模块自身输出 → `parse()` 解析 GGA/RMC/GLL/GSA；
    - 标定收尾校验 1005 → 与 `proto.rtcm` 配合，本模块只负责 NMEA 侧。

三条硬规则：
    1. 校验和一律现算，绝不抄常量（抄错一位，模块静默不认，排查极痛苦）。
    2. 空字段按 NMEA 标准留空，**不伪造数值**：不知道 HDOP 就输出空，
       宁可让对端看到一个空字段，也不要编一个 0.9 让日志看起来"正常"。
    3. Talker 统一 GN（多星座）。解析时三个 talker（GP/GL/GA/GN/BD）都接受。

自测（不需要硬件）：
    python -m rtkserver.calibration.proto.nmea
"""
import logging
import time

# NMEA 0183 单条语句上限 82 字符（含 $ 与 CRLF）。超长语句部分接收机直接丢弃。
NMEA_MAX_LEN = 82

TALKER = 'GN'


# ==================== 校验和 ====================
def checksum(body: str) -> str:
    """8 位异或校验，返回 2 位大写十六进制。body 不含 `$` 与 `*XX`。"""
    value = 0
    for ch in body:
        value ^= ord(ch)
    return f'{value:02X}'


def frame(body: str) -> str:
    """把命令体组帧成完整 NMEA 语句（含 `$`、`*XX` 与 CRLF）。"""
    body = body.lstrip('$')
    return f'${body}*{checksum(body)}\r\n'


def verify(sentence: str):
    """校验一条语句的校验和。

    返回 (是否带校验和, 是否通过)：没有 `*XX` 的语句返回 (False, False)，
    由调用方决定是否接受——模块的 `$PQTM` 应答必须带校验和。
    """
    text = sentence.strip()
    if not text.startswith('$') or '*' not in text:
        return False, False
    body, _, tail = text[1:].partition('*')
    given = tail.strip()[:2].upper()
    return True, given == checksum(body)


# ==================== 组装 ====================
def _dm(value, is_lat):
    """十进制度 → NMEA 的 ddmm.mmmmm / dddmm.mmmmm 与时半球字母。

    注意分钟四舍五入后可能进位到 60.00000（例如 59.9999995），
    此时必须把度加一并把分钟归零，否则会输出 `3060.00000` 这种非法值。
    """
    hemi_pos, hemi_neg = ('N', 'S') if is_lat else ('E', 'W')
    hemi = hemi_pos if value >= 0 else hemi_neg
    abs_value = abs(float(value))
    degrees = int(abs_value)
    minutes = (abs_value - degrees) * 60.0
    if round(minutes, 5) >= 60.0:
        degrees += 1
        minutes = 0.0
    width = 2 if is_lat else 3
    return f'{degrees:0{width}d}{minutes:08.5f}', hemi


def _fmt(value, spec):
    """None → 空字段（NMEA 允许必填字段留空，但绝不能编数值）。"""
    if value is None or value == '':
        return ''
    return format(float(value), spec)


def build_gga(lat, lon, height, utc=None, quality=1, num_sats=None, hdop=None,
              geoid_sep=None, age=None, station_id=None):
    """组一条标准 `$GNGGA`。

    参数：
        lat/lon      十进制度（南纬/西经为负）
        height       椭球高或海拔，米，按 quality 的语义由调用方保证一致
        utc          'hhmmss.ss'；None 则取当前 UTC
        quality      0=无效 1=单点 2=差分 4=RTK 固定 5=RTK 浮动；None 留空
        num_sats     参与解算卫星数；None 留空
        hdop         水平精度因子；None 留空
        geoid_sep    大地水准面差距（米，GGA 里是 "M" 前面那个数）；None 留空
        age          差分龄期（秒，RTK 才有）；None 留空
        station_id   差分基准站 ID；None 留空

    为什么默认 quality=1：这条 GGA 是给 CORS 做 VRS 选点用的，
    此刻我们并没有真的解算出 RTK 固定解，谎报 4 只会污染对端日志。
    """
    if utc is None:
        now = time.gmtime()
        utc = f'{now.tm_hour:02d}{now.tm_min:02d}{now.tm_sec:02d}.00'

    lat_dm, lat_hemi = _dm(float(lat), True)
    lon_dm, lon_hemi = _dm(float(lon), False)

    parts = [
        f'{TALKER}GGA',
        str(utc),
        lat_dm, lat_hemi,
        lon_dm, lon_hemi,
        '' if quality is None else str(int(quality)),
        _fmt(num_sats, '.0f'),
        _fmt(hdop, '.1f'),
        _fmt(height, '.3f'),
        'M' if height is not None else '',
        _fmt(geoid_sep, '.1f'),
        'M' if geoid_sep is not None else '',
        _fmt(age, '.1f'),
        '' if station_id is None else str(station_id),
    ]
    return frame(','.join(parts))


# ==================== 解析 ====================
def _to_float(value):
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(value):
    number = _to_float(value)
    return None if number is None else int(number)


def _dm_to_deg(value, hemi):
    """ddmm.mmmmm → 十进制度。用 `//100` 拆度，经纬度都适用（不用数字符个数）。"""
    number = _to_float(value)
    if number is None:
        return None
    degrees = int(number // 100)
    minutes = number - degrees * 100
    decimal = degrees + minutes / 60.0
    if hemi in ('S', 'W'):
        decimal = -decimal
    return decimal


def _field(fields, index):
    return fields[index] if index < len(fields) else ''


def _parse_gga(fields):
    return {
        'utc': _field(fields, 1) or None,
        'lat': _dm_to_deg(_field(fields, 2), _field(fields, 3)),
        'lon': _dm_to_deg(_field(fields, 4), _field(fields, 5)),
        'quality': _to_int(_field(fields, 6)),
        'num_sats': _to_int(_field(fields, 7)),
        'hdop': _to_float(_field(fields, 8)),
        'height': _to_float(_field(fields, 9)),
        'geoid_sep': _to_float(_field(fields, 11)),
        'age': _to_float(_field(fields, 13)),
        'station_id': _field(fields, 14) or None,
    }


def _parse_rmc(fields):
    return {
        'utc': _field(fields, 1) or None,
        'status': _field(fields, 2) or None,       # A=有效 V=无效
        'lat': _dm_to_deg(_field(fields, 3), _field(fields, 4)),
        'lon': _dm_to_deg(_field(fields, 5), _field(fields, 6)),
        'speed_kn': _to_float(_field(fields, 7)),
        'course': _to_float(_field(fields, 8)),
        'date': _field(fields, 9) or None,          # ddmmyy
        'mode': _field(fields, 12) or None,         # A/D/E/M/N/S
    }


def _parse_gll(fields):
    return {
        'lat': _dm_to_deg(_field(fields, 1), _field(fields, 2)),
        'lon': _dm_to_deg(_field(fields, 3), _field(fields, 4)),
        'utc': _field(fields, 5) or None,
        'status': _field(fields, 6) or None,
        'mode': _field(fields, 7) or None,
    }


def _parse_gsa(fields):
    """GSA 是唯一带 PDOP/HDOP/VDOP 的语句——GGA 里没有 PDOP。

    字段位（1-based，含语句头）：1 模式、2 定位类型、3..14 卫星号、15 PDOP、
    16 HDOP、17 VDOP、18 系统 ID。换算成 0-based 后 PDOP 在索引 15。
    """
    sats = [s for s in fields[3:15] if s]
    return {
        'mode': _field(fields, 1) or None,          # A=自动 2D/3D，M=手动
        'fix_type': _to_int(_field(fields, 2)),     # 1=未定位 2=2D 3=3D
        'sats': sats,
        'pdop': _to_float(_field(fields, 15)),
        'hdop': _to_float(_field(fields, 16)),
        'vdop': _to_float(_field(fields, 17)),
        'system_id': _to_int(_field(fields, 18)),
    }


_PARSERS = {
    'GGA': _parse_gga,
    'RMC': _parse_rmc,
    'GLL': _parse_gll,
    'GSA': _parse_gsa,
}


def parse(sentence):
    """解析一条 NMEA 语句，返回统一字段字典。

    返回键（固定存在）：
        talker      'GN' / 'GP' / 'PQ' …
        type        'GGA' / 'RMC' / 'GLL' / 'GSA'，不认识的语句为原样类型串
        fields      原始字段列表（含语句头，便于调用方自己取冷门字段）
        checksum_ok 校验和是否正确；语句没带 `*XX` 时为 None
        valid       是否可用（格式对 + 校验和对 + 类型认识）
        error       不可用原因，可用时为 None

    类型专属字段见 `_parse_gga/_parse_rmc/_parse_gll/_parse_gsa`。
    解析失败**不抛异常**：串口噪声、半截语句是常态，调用方按 valid 判断。
    """
    result = {
        'talker': None, 'type': None, 'fields': [],
        'checksum_ok': None, 'valid': False, 'error': None, 'raw': sentence,
    }

    if not sentence:
        result['error'] = 'empty'
        return result

    text = sentence.strip()
    if not text.startswith('$'):
        result['error'] = 'no_dollar'
        return result

    body, _, tail = text[1:].partition('*')
    if tail:
        result['checksum_ok'] = tail.strip()[:2].upper() == checksum(body)
        if not result['checksum_ok']:
            result['error'] = 'checksum'
            return result

    fields = body.split(',')
    head = fields[0]
    if len(head) < 3:
        result['error'] = 'bad_header'
        return result

    if head[0] == 'P':
        # 厂商私有语句：$P + 3 位厂商标识 + 语句名，例如 $PQTMVERNO → P/QTM/VERNO。
        # 拆对了才能让驱动层用 'VERNO' 这样的语义键去匹配，而不是背着 'TMVERNO'。
        result['talker'] = 'P'
        result['manufacturer'] = head[1:4]
        result['type'] = head[4:]
    else:
        result['talker'] = head[:2]
        result['type'] = head[2:]
    result['fields'] = fields

    parser = _PARSERS.get(result['type'])
    if parser is None:
        # 认识格式但不认识的语句类型：valid=False 但仍回填字段，
        # 便于通道层把 `$PQTM...` 之类的应答原样转给驱动解析。
        result['error'] = 'unknown_type'
        return result

    try:
        result.update(parser(fields))
    except Exception as e:                      # 解析器自身绝不能把线程带崩
        result['error'] = f'parse_error:{e}'
        return result

    result['valid'] = True
    return result


def iter_sentences(data: bytes):
    """从字节流里切出完整语句，返回 `(sentences, remainder)`。

    串口 read() 会在任意位置断开，`remainder` 必须由调用方保存并拼到下一次。
    """
    if not data:
        return [], b''
    cut = data.rfind(b'\n')
    if cut < 0:
        return [], data
    chunk = data[:cut + 1]
    rest = data[cut + 1:]

    sentences = []
    for raw in chunk.split(b'\n'):
        raw = raw.strip()
        if not raw:
            continue
        try:
            sentences.append(raw.decode('ascii', 'replace'))
        except Exception:                       # decode 的 errors 已兜住，不会再抛
            continue
    return sentences, rest


# ==================== 自测入口（不需要硬件） ====================
_REFERENCE_GGA = '$GNGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*59'
_REFERENCE_RMC = '$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*74'
_REFERENCE_GSA = '$GNGSA,A,3,04,05,09,12,24,25,29,31,,,,,1.8,1.0,1.2,1*39'


def _selftest():
    """校验和、组帧往返、四种语句解析、字节流切分。

    跑法：python -m rtkserver.calibration.proto.nmea
    """
    print('=== nmea 自测 ===')

    # 1) 校验和与独立参考实现一致
    def ref(body):
        value = 0
        for ch in body:
            value ^= ord(ch)
        return f'{value:02X}'

    for body in ('GNGGA,1,2,3', 'GNRMC,123519,A', 'PQTMVERNO'):
        assert checksum(body) == ref(body)
    assert checksum('') == '00'
    print('[1] 校验和 OK')

    # 2) 真实语句的校验和必须能验过（这三条是公开文档里的样例）
    for sample in (_REFERENCE_GGA, _REFERENCE_RMC, _REFERENCE_GSA):
        has_cs, ok = verify(sample)
        assert has_cs and ok, sample
    print('[2] 样例语句校验和 OK')

    # 3) GGA 组帧 → 解析往返，经纬度误差 < 1e-6 度
    lat, lon, height = 39.904211, 116.407394, 52.36
    line = build_gga(lat, lon, height, utc='120000.00', quality=1, num_sats=18,
                     hdop=0.8, geoid_sep=-9.5)
    assert line.startswith('$GNGGA,') and line.endswith('\r\n')
    assert len(line) <= NMEA_MAX_LEN, len(line)
    parsed = parse(line)
    assert parsed['valid'], parsed['error']
    assert abs(parsed['lat'] - lat) < 1e-6, parsed['lat']
    assert abs(parsed['lon'] - lon) < 1e-6, parsed['lon']
    assert abs(parsed['height'] - height) < 1e-3
    assert parsed['num_sats'] == 18 and abs(parsed['hdop'] - 0.8) < 1e-9
    print(f'[3] GGA 往返 OK  len={len(line)}  {line.strip()}')

    # 4) 南纬/西经必须带 S/W，且解析回来是负数
    line = build_gga(-33.868800, -70.669300, 570.0, utc='000000.00')
    parsed = parse(line)
    assert ',S,' in line and ',W,' in line
    assert abs(parsed['lat'] + 33.8688) < 1e-6 and abs(parsed['lon'] + 70.6693) < 1e-6
    print(f'[4] 南纬西经 OK  {line.strip()}')

    # 5) 空字段留空，不伪造数值
    line = build_gga(30.0, 120.0, 10.0, utc='010203.00')
    body = line.split('*')[0]
    # quality=1 之后：卫星数、HDOP、大地水准面差距、龄期、站号全部留空
    assert body == '$GNGGA,010203.00,3000.00000,N,12000.00000,E,1,,,10.000,M,,,,', body
    parsed = parse(line)
    assert parsed['num_sats'] is None and parsed['hdop'] is None
    assert parsed['age'] is None and parsed['station_id'] is None
    # 高度是 0 时必须写 0.000，而不是当成"没有值"留空
    zero_line = build_gga(30.0, 120.0, 0.0, utc='010203.00')
    assert ',0.000,M,' in zero_line, zero_line
    assert parse(zero_line)['height'] == 0.0
    print('[5] 空字段策略 OK')

    # 6) 分钟进位：59.9999995 分不能输出 60.00000
    line = build_gga(30.0 + 59.9999999 / 60.0, 120.0, 0.0, utc='000000.00')
    assert '3100.00000' in line, line
    print(f'[6] 分钟进位 OK  {line.strip()}')

    # 7) 解析样例 GGA
    gga = parse(_REFERENCE_GGA)
    assert gga['valid'] and gga['type'] == 'GGA' and gga['talker'] == 'GN'
    assert abs(gga['lat'] - 48.1173) < 1e-6, gga['lat']
    assert abs(gga['lon'] - 11.5166667) < 1e-6, gga['lon']
    assert gga['quality'] == 1 and gga['num_sats'] == 8
    assert abs(gga['height'] - 545.4) < 1e-6
    print(f'[7] GGA 解析 OK  lat={gga["lat"]:.6f} lon={gga["lon"]:.6f}')

    # 8) RMC / GSA / GLL
    rmc = parse(_REFERENCE_RMC)
    assert rmc['valid'] and rmc['status'] == 'A' and rmc['date'] == '230394'
    assert abs(rmc['speed_kn'] - 22.4) < 1e-9 and abs(rmc['lat'] - 48.1173) < 1e-6

    gsa = parse(_REFERENCE_GSA)
    assert gsa['valid'] and gsa['fix_type'] == 3 and len(gsa['sats']) == 8
    assert abs(gsa['pdop'] - 1.8) < 1e-9 and abs(gsa['hdop'] - 1.0) < 1e-9
    assert abs(gsa['vdop'] - 1.2) < 1e-9 and gsa['system_id'] == 1

    gll = parse('$GNGLL,4807.038,N,01131.000,E,123519,A,A*56')
    assert gll['valid'], gll['error']
    assert abs(gll['lat'] - 48.1173) < 1e-6
    print(f'[8] RMC/GSA/GLL 解析 OK  PDOP={gsa["pdop"]} 卫星={len(gsa["sats"])}')

    # 9) 坏帧不能抛异常，且 valid=False
    bad_cs = parse('$GNGGA,1,2,3*00')
    assert bad_cs['valid'] is False and bad_cs['error'] == 'checksum'
    assert parse('')['valid'] is False
    assert parse('garbage')['error'] == 'no_dollar'
    proprietary = parse(frame('PQTMVERNO,LG290P,1.0'))
    assert proprietary['talker'] == 'P' and proprietary['manufacturer'] == 'QTM'
    assert proprietary['type'] == 'VERNO'
    # 认识格式但没有专用解析器：valid=False，字段仍原样保留给驱动层
    assert proprietary['valid'] is False and proprietary['error'] == 'unknown_type'
    assert proprietary['fields'][1] == 'LG290P'
    print('[9] 坏帧容错 OK')

    # 10) 字节流切分：半截语句必须原样留到下一次
    stream = (build_gga(30.0, 120.0, 1.0, utc='000000.00')
              + build_gga(30.1, 120.1, 2.0, utc='000001.00')).encode()
    lines, rest = iter_sentences(stream[:40])
    assert lines == [] and rest == stream[:40]
    lines, rest2 = iter_sentences(rest + stream[40:])
    assert len(lines) == 2 and rest2 == b'', (len(lines), rest2)
    print('[10] 字节流切分 OK')

    print('=== nmea 自测全部通过 ===')
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(_selftest())
