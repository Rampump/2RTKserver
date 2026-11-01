# -*- coding: utf-8 -*-
"""RTCM 解析（旁路）：CRC24Q 校验、解帧、消息统计、站点/天线信息提取。

定位：只做统计和 UI 展示，绝不修改原始数据，也不参与分发决策。
解析慢只会影响监控页面刷新，不会拖慢 distribute()。

注意：本模块不 import 任何数据源，只被数据源/生命周期调用。
"""
import logging
import time
from collections import defaultdict, deque
from io import BytesIO

from pyrtcm import RTCMReader, parse_msm

from ..core import state
from ..core.constants import (MAX_BUFFER_SIZE, MSM_BROADCAST_INTERVAL,
                              MSM_CELL_FIELDS)
from ..utils.common import ecef_to_latlon

# ==================== CRC24Q（RTCM3 帧校验） ====================
CRC24Q_POLY = 0x1864CFB
CRC24Q_TABLE = []
for _i in range(256):
    _crc = _i << 16
    for _ in range(8):
        _crc <<= 1
        if _crc & 0x1000000:
            _crc ^= CRC24Q_POLY
    CRC24Q_TABLE.append(_crc & 0xFFFFFF)


def crc24q(data: bytes) -> int:
    """计算 RTCM3 / GPS 使用的 CRC-24Q"""
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ CRC24Q_TABLE[((crc >> 16) ^ byte) & 0xFF]
    return crc


def extract_rtcm_frames(data: bytes):
    """从二进制数据中提取完整的RTCM帧"""
    frames = []
    index = 0
    data_len = len(data)

    while index < data_len:
        if data[index] == 0xD3 and index + 2 < data_len:
            length = ((data[index+1] & 0x03) << 8) | data[index+2]
            frame_length = 3 + length + 3
            if index + frame_length <= data_len:
                frame = data[index:index+frame_length]
                # 校验 CRC24Q：载荷里偶然出现的 0xD3 会被误判成帧头，
                # 不校验就会把后续合法帧整段吞掉，并让缓冲区一直堆积。
                if crc24q(frame[:-3]) != int.from_bytes(frame[-3:], 'big'):
                    logging.debug("RTCM 帧 CRC 校验失败，跳过一个字节重新同步")
                    index += 1
                    continue
                frames.append(frame)
                index += frame_length
            else:
                break
        else:
            index += 1
    return frames, data[index:]


# ==================== 消息频率统计 ====================
class FrequencyTracker:
    def __init__(self, window_size=180):
        self.window_size = window_size
        self.msg_timestamps = defaultdict(deque)

    def add_message(self, msg_type):
        current_time = time.time()
        self.msg_timestamps[msg_type].append(current_time)
        self._prune_old_timestamps(msg_type, current_time)

    def get_all_frequencies(self):
        current_time = time.time()
        frequencies = {}
        for msg_type, timestamps in self.msg_timestamps.items():
            self._prune_old_timestamps(msg_type, current_time)
            freq = len(timestamps) // self.window_size
            frequencies[msg_type] = max(1, freq)
        return frequencies

    def _prune_old_timestamps(self, msg_type, current_time):
        cutoff = current_time - self.window_size
        timestamps = self.msg_timestamps[msg_type]
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()


frequency_tracker = FrequencyTracker(window_size=30)

_msm_last_sent = {}                 # gnss -> 上次广播时间，仅 process_rtcm_buffer 线程写


def _broadcast_msm(gnss: str, header: dict, cells) -> bool:
    """把一条 MSM 结果推给监控页面，但每个星座每秒最多一次。

    两处优化：
      1. 降频：多星座多信号同时播发时，原来每条 MSM 都广播一次全量 cell 数据
         （约 10 条/秒）。实测单条 22.5KB，等于每个监控页面 200KB/s 以上的流量，
         而页面本来就是 1Hz 刷新，多推的都被前端覆盖掉了。
      2. 裁剪：前端只读 CELLPRN / CELLSIG / DF403 / DF405 / DF408，
         DF400/401/402/404/406/407/420 发过去没人用，去掉能再省约 40% 流量。
    返回 True 表示本次确实发出了一条。
    """
    now = time.time()
    if now - _msm_last_sent.get(gnss, 0.0) < MSM_BROADCAST_INTERVAL:
        return False
    _msm_last_sent[gnss] = now

    slim_cells = [{k: c[k] for k in MSM_CELL_FIELDS if k in c} for c in cells]
    state.broadcast({
        "identity": header.get("identity"),
        "gnss": gnss,
        "station": header.get("station"),
        "sats": header.get("sats"),
        "cell_data": slim_cells,
        "station_info": state.station_info,
        "msg_types": sorted(list(state.msg_types_seen)),
        "msg_type_frequency": frequency_tracker.get_all_frequencies()
    })
    return True


def upload_data(data):
    """统一接口，将收到的RTCM数据追加到全局缓冲区并限制大小。"""
    with state.buffer_lock:
        overflow = len(state.rtcm_buffer) + len(data) - MAX_BUFFER_SIZE
        if overflow > 0:
            # 原先是整体 clear()，一次溢出会把还没解析的合法数据全丢掉；
            # 改为只丢最旧的部分，保留最新的数据继续解析。
            logging.warning(f"RTCM缓冲区溢出，丢弃最旧的 {overflow} 字节")
            del state.rtcm_buffer[:overflow]
        state.rtcm_buffer.extend(data)
    # 注意：这里不再做"上传统计"。原先把"进入解析缓冲的字节"记成"已上传字节"，
    # 语义是错的（没开上传出口时也会累加）。真正的发送量由 CasterUploader 线程统计。


def process_rtcm_buffer():
    while True:
        try:
            current_buffer = b""
            with state.buffer_lock:
                if state.rtcm_buffer:
                    current_buffer = bytes(state.rtcm_buffer)
                    state.rtcm_buffer.clear()

            if not current_buffer:
                time.sleep(0.1)
                continue

            frames, remaining = extract_rtcm_frames(current_buffer)

            if remaining:
                with state.buffer_lock:
                    state.rtcm_buffer.extend(remaining)

            for frame in frames:
                try:
                    with BytesIO(frame) as frame_io:
                        rtr = RTCMReader(frame_io)

                        for (raw_data, parsed_data) in rtr:
                            if not parsed_data:
                                continue

                            try:
                                msg_type = parsed_data.DF002
                            except AttributeError:
                                continue
                            frequency_tracker.add_message(msg_type)
                            state.msg_types_seen.add(msg_type)
                            state.msg_types_recent[msg_type] = time.time()

                            if msg_type in (1005, 1006):
                                process_station_message(parsed_data, msg_type)

                            if msg_type == 1033:
                                antenna_info, device_info = process_1033_message(parsed_data)

                                if antenna_info:
                                    state.broadcast({
                                        "antenna_info": antenna_info,
                                        "msg_types": sorted(list(state.msg_types_seen)),
                                        "msg_type_frequency": frequency_tracker.get_all_frequencies()
                                    })

                                if device_info:
                                    state.broadcast({
                                        "device_info": device_info,
                                        "msg_types": sorted(list(state.msg_types_seen)),
                                        "msg_type_frequency": frequency_tracker.get_all_frequencies()
                                    })

                            msm_array = parse_msm(parsed_data)
                            if msm_array:
                                header, sats, cells = msm_array
                                gnss = header.get("gnss", "UNKNOWN").upper()

                                # 降频 + 精简字段（详见 _broadcast_msm 的说明）
                                _broadcast_msm(gnss, header, cells)
                except Exception as e:
                    logging.error(f"处理RTCM帧时出错: {str(e)}")
                    continue

            time.sleep(0.01)
        except Exception as e:
            logging.error(f"处理RTCM缓冲区时出错: {str(e)}")
            time.sleep(1)


def process_station_message(parsed_data, msg_type):
    """处理站点消息（1005/1006），提取 ECEF 并换算成经纬度。"""
    try:
        if msg_type in (1005, 1006):
            ecef_x = getattr(parsed_data, 'DF025', 0)
            ecef_y = getattr(parsed_data, 'DF026', 0)
            ecef_z = getattr(parsed_data, 'DF027', 0)
            lat, lon, height = ecef_to_latlon(ecef_x, ecef_y, ecef_z)

            state.station_info.update({
                "station_id": getattr(parsed_data, 'DF003', "未知"),
                "ecef_x": ecef_x,
                "ecef_y": ecef_y,
                "ecef_z": ecef_z,
                "latitude": lat,
                "longitude": lon,
                "height": height
            })

            state.broadcast({
                "station_info": state.station_info,
                "msg_types": sorted(list(state.msg_types_seen)),
                "msg_type_frequency": frequency_tracker.get_all_frequencies()
            })
    except Exception as e:
        logging.error(f"处理1005/1006消息时出错: {e}")


def process_1033_message(parsed_data):
    """处理1033消息（天线/接收机描述）。"""
    try:
        antenna_descriptor_count = getattr(parsed_data, "DF029", 0)
        antenna_name = ""
        for i in range(1, antenna_descriptor_count + 1):
            antenna_name += getattr(parsed_data, f"DF030_{i:02d}", "")

        antenna_info = {
            "station_id": getattr(parsed_data, "DF003", "未知"),
            "antenna_name": antenna_name,
            "antenna_setup_id": getattr(parsed_data, "DF031", "未知"),
            "serial_number": getattr(parsed_data, "DF032", "未知"),
            "message_type": "1033"
        }

        receiver_count = getattr(parsed_data, "DF227", 0)
        receiver_model = ""
        for i in range(1, receiver_count + 1):
            receiver_model += getattr(parsed_data, f"DF228_{i:02d}", "")

        firmware_count = getattr(parsed_data, "DF229", 0)
        firmware_version = ""
        for i in range(1, firmware_count + 1):
            firmware_version += getattr(parsed_data, f"DF230_{i:02d}", "")

        device_info = {
            "station_id": getattr(parsed_data, "DF003", "未知"),
            "receiver_model": receiver_model,
            "firmware_version": firmware_version,
            "receiver_serial": getattr(parsed_data, "DF231", "未知"),
            "message_type": "1033"
        }

        return antenna_info, device_info
    except Exception as e:
        logging.error(f"处理1033消息时出错: {e}")
        return {"error": "天线信息提取失败"}, {"error": "设备信息提取失败"}
