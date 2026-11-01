# -*- coding: utf-8 -*-
"""数据源①：串口（GNSS 模块）读取 + 自动扫描识别。

数据源只负责「读 → 交给 distribute()」，不持有任何出口连接。
这里不再有 caster.sendall / 重连逻辑——那正是历史上
"上传端一卡住，串口读取停摆、本地 Caster 和数传一起断流"的根因。
"""
import logging
import time
from typing import Optional, Tuple

import serial
import serial.tools.list_ports

from ..core import state
from ..core.config import save_serial_config
from ..core.constants import ART_LOGO, SERIAL_SCAN_BAUDS
from ..core.distributor import distribute
from ..core.status import describe_targets, push_run_status
from ..utils.common import (ThreadExitRequested, clear_screen, format_bytes,
                            format_duration, retry)
from .rtcm_parser import extract_rtcm_frames


def detect_serial(cfg: dict) -> Tuple[Optional[str], Optional[int]]:
    """检测可用的RTK串口设备，优先使用配置文件中的设置"""
    port = cfg.get('serial_port', '')
    baud = cfg.get('baud_rate', '')

    valid_bauds = SERIAL_SCAN_BAUDS

    if port and baud and baud > 0:
        logging.info(f"尝试配置的串口: {port} @ {baud}bps")
        if test_serial(port, baud, check_rtcm=True):
            logging.info(f"使用配置的串口: {port} @ {baud}bps")
            return port, baud

    logging.warning("开始自动扫描串口设备...")
    available_ports = [p.device for p in serial.tools.list_ports.comports()]
    if not available_ports:
        logging.error("未发现任何串口设备")
        return None, None

    logging.info(f"发现 {len(available_ports)} 个串口: {', '.join(available_ports)}")
    for current_port in available_ports:
        for current_baud in valid_bauds:
            # 扫描可能持续较久，收到重启/退出信号要立刻放弃
            if state.thread_exit_event.is_set():
                raise ThreadExitRequested()
            try:
                if test_serial(current_port, current_baud, check_rtcm=True):
                    logging.info(f"发现RTK模块: {current_port} @ {current_baud}bps")
                    save_serial_config(current_port, current_baud)
                    return current_port, current_baud
            except serial.SerialException as e:
                logging.debug(f"串口 {current_port} @ {current_baud}bps 不可用: {str(e)}")

    logging.error("未检测到RTK模块，请检查设备连接")
    return None, None


@retry(max_retries=2, delay=1)
def test_serial(port: str, baud: int, check_rtcm: bool = True) -> bool:
    """测试串口是否输出RTCM数据"""
    try:
        with serial.Serial(port, baud, timeout=2) as ser:
            ser.reset_input_buffer()
            start_time = time.time()
            rtcm_found = False

            while time.time() - start_time < 2:
                # 单档波特率探测从 5 秒收紧到 2 秒：
                # RTCM 一般 1Hz，2 秒足以判定；否则 4 个串口 × 11 档波特率
                # 最坏要扫 220 秒以上，启动时表现为"卡死"。
                if state.thread_exit_event.is_set():
                    raise ThreadExitRequested()
                data = ser.read(max(ser.in_waiting, 1024))
                if not data:
                    continue
                if b'\xd3' in data:
                    frames, _ = extract_rtcm_frames(data)
                    if frames:
                        rtcm_found = True
                        if check_rtcm:
                            logging.debug(f"在 {port} @ {baud}bps 检测到有效RTCM数据")
                            return True

                if not check_rtcm:
                    logging.debug(f"在 {port} @ {baud}bps 检测到有效数据")
                    return True

            return rtcm_found
    except serial.SerialException:
        return False


def upload_via_serial(ser, cfg, thread_exit_event):
    """串口数据源：只负责「读串口 → 交给 distribute()」。

    注意这里不再有任何 caster.sendall / 重连逻辑——那正是历史上
    "上传端一卡住，串口读取停摆、本地 Caster 和数传一起断流"的根因。
    推流、握手、断线重连全部由 CasterUploader 在自己的线程里完成。
    """
    program_start = time.time()
    last_clear = program_start
    last_status_update = program_start
    total_bytes = 0

    # 增量更新，不整体替换全局 dict：避免覆盖其他模块写入的 radio / local_caster 字段
    state.run_status["mode"] = "串口模式"
    state.run_status["data_source"] = f"{cfg['serial_port']} @ {cfg['baud_rate']}bps"
    state.run_status["target_caster"] = describe_targets(cfg)
    push_run_status()

    while not thread_exit_event.is_set():
        try:
            if ser is None:
                logging.warning("串口对象为 None，尝试重新识别串口...")
                port, baud = detect_serial(cfg)
                if port and baud:
                    try:
                        ser = serial.Serial(port, baud, timeout=1)
                        cfg['serial_port'], cfg['baud_rate'] = port, baud
                        logging.info(f"串口重连成功: {port} @ {baud}")
                    except Exception as e:
                        logging.error(f"串口重建失败: {e}")
                        time.sleep(5)
                        continue
                else:
                    time.sleep(5)
                    continue

            data = ser.read(ser.in_waiting or 1024)

            if not data:
                time.sleep(0.2)
                continue

            # 统一分发：本地 Caster / 数传 / 上传 / 解析，四路互相独立且全部非阻塞
            distribute(data)

            total_bytes += len(data)

            now = time.time()
            if now - last_clear > 100:
                clear_screen()
                print(ART_LOGO)
                print(f"串口模式: {cfg['serial_port']} @ {cfg['baud_rate']}bps")
                print(f"数据出口: {describe_targets(cfg)}")
                print(f"运行时间: {format_duration(now - program_start)}")
                print(f"读取数据量: {format_bytes(total_bytes)}")
                last_clear = now

            if now - last_status_update > 2:
                state.run_status["data_sent"] = format_bytes(total_bytes)
                state.run_status["run_time"] = format_duration(now - program_start)
                push_run_status()
                last_status_update = now

        except Exception as e:
            logging.error(f"串口读取失败: {e}")
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(3)

    # 退出时只关自己持有的串口；上传连接由 CasterUploader 自己关闭
    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass
