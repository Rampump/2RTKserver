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
from ..core.constants import ART_LOGO, SERIAL_EMPTY_WATCHDOG, SERIAL_SCAN_BAUDS
from ..core.distributor import distribute
from ..core.status import describe_target_parts, describe_targets, push_run_status
from ..utils.common import (ThreadExitRequested, clear_screen, format_bytes,
                            format_duration, interruptible_sleep)
from .rtcm_parser import extract_rtcm_frames


def _looks_like_nmea(data: bytes) -> bool:
    """粗判这段字节像不像 NMEA 语句（GNSS 模块活着，但可能还没输出 RTCM）。

    为什么要这么判：只出 NMEA 的模块（没配成输出 RTCM、或刚上电还在吐提示）
    是"源活着但暂时不能上传"。旧逻辑只认 RTCM，会把它判成"没有模块"，
    数据面一次都起不来——现场看到的就是"插了模块但三路全停"。

    只认 `$G`/`$B`/`$P`/`$I` 开头且带逗号，**不认任意字节**：
    波特率不匹配时读到的是乱码，同样是"有字节"，认了就会选错波特率。
    """
    for head in (b'$G', b'$B', b'$P', b'$I'):
        idx = data.find(head)
        if idx >= 0 and b',' in data[idx:idx + 96]:
            return True
    return False


def _probe_serial(port: str, baud: int, window: float = 2.0):
    """探测一个 (串口, 波特率)，返回 'rtcm' / 'nmea' / None。

    为什么要把两种判据分开返回，而不是直接给 True/False：
    **RTCM 是数据源独有的特征，NMEA 不是**——数传电台同样会收到流动站回传的 GGA。
    所以扫描只能是"RTCM 优先、NMEA 兜底"，否则会把数传口认成数据源，现场表现是
    "首页一切正常、本地 Caster 在推 GGA、云端永远不上线（一个 RTCM 都没有）"。

    窗口固定 2 秒：RTCM 一般 1 Hz，2 秒足够；解出 RTCM 立刻返回，没解出就等满窗口，
    保证"先出 NMEA、1 秒后才出 RTCM"的模块不会被 NMEA 抢先判定。
    """
    try:
        with serial.Serial(port, baud, timeout=2) as ser:
            ser.reset_input_buffer()
            start_time = time.time()
            saw_nmea = False
            while time.time() - start_time < window:
                # 单档波特率探测 2 秒：4 个串口 × 11 档波特率，再长启动就像卡死
                if state.thread_exit_event.is_set():
                    raise ThreadExitRequested()
                data = ser.read(max(ser.in_waiting, 1024))
                if not data:
                    continue
                if b'\xd3' in data:
                    frames, _ = extract_rtcm_frames(data)
                    if frames:
                        return 'rtcm'
                if not saw_nmea and _looks_like_nmea(data):
                    saw_nmea = True
            return 'nmea' if saw_nmea else None
    except ThreadExitRequested:
        raise
    except Exception:
        # 探测失败就是"这个口/这档波特率上没有数据源"：
        # 不限于 SerialException——非法波特率、权限不足、驱动报错都归为这一类，
        # 否则一个坏口会让整轮扫描抛出去，看起来像"程序崩了"。
        return None


def detect_serial(cfg: dict) -> Tuple[Optional[str], Optional[int]]:
    """找出"哪个串口上有活的 GNSS 模块"。

    判据分两档，**RTCM 优先**：
      1. 能解出并校验 RTCM 帧的口 —— 这才是真正的数据源，命中即返回，
         连数传口也照收（现场真有人把模块和数传的线互换，硬跳过数传口就永远找不到）；
      2. 一路 RTCM 都没有时，才退到 NMEA 兜底（只出 NMEA 的模块也能起本地两路），
         此时**优先选不是数传口的那个**，因为数传口上的 NMEA 几乎都是流动站回传的 GGA。
    能不能上传不在这里判断：上传端按 last_rtcm_ts 自己决定。
    """
    port = cfg.get('serial_port', '')
    baud = cfg.get('baud_rate', '')
    valid_bauds = SERIAL_SCAN_BAUDS

    radio_port = ''
    if str(cfg.get('radio_enabled', '0')) == '1':
        radio_port = (cfg.get('radio_port') or '').strip()

    # NMEA 候选：(串口, 波特率, 是不是数传口)。配置口先入列，保证用户显式选择优先。
    nmea_candidates = []

    def _remember(verdict, p, b):
        if verdict == 'nmea':
            nmea_candidates.append((p, b, bool(radio_port) and p == radio_port))

    if port and baud and baud > 0:
        logging.info(f"尝试配置的串口: {port} @ {baud}bps")
        verdict = _probe_serial(port, baud)
        if verdict == 'rtcm':
            logging.info(f"使用配置的串口: {port} @ {baud}bps（已解出 RTCM）")
            return port, baud
        _remember(verdict, port, baud)

    logging.warning("开始自动扫描串口设备...")
    available_ports = [p.device for p in serial.tools.list_ports.comports()]
    if not available_ports:
        logging.error("未发现任何串口设备")
        return None, None

    logging.info(f"发现 {len(available_ports)} 个串口: {', '.join(available_ports)}")
    for current_port in available_ports:
        if port and current_port == port:
            continue                      # 配置口刚探过，别白花 2 秒
        for current_baud in valid_bauds:
            # 扫描可能持续较久，收到重启/退出信号要立刻放弃
            if state.thread_exit_event.is_set():
                raise ThreadExitRequested()
            try:
                verdict = _probe_serial(current_port, current_baud)
            except serial.SerialException as e:
                logging.debug(f"串口 {current_port} @ {current_baud}bps 不可用: {str(e)}")
                continue
            if verdict == 'rtcm':
                logging.info(f"发现串口数据源: {current_port} @ {current_baud}bps（已解出 RTCM）")
                save_serial_config(current_port, current_baud)
                return current_port, current_baud
            _remember(verdict, current_port, current_baud)

    # 一路 RTCM 都没有：退到 NMEA 判据。优先非数传口，全在数传口上才用它并告警。
    if nmea_candidates:
        preferred = [c for c in nmea_candidates if not c[2]] or nmea_candidates
        chosen_port, chosen_baud, is_radio = preferred[0]
        if is_radio:
            logging.warning(
                f"只有数传口 {chosen_port} @ {chosen_baud}bps 有数据"
                f"（模块与数传的线可能插反了），暂按数据源使用；"
                f"若模块确实在这个口上，建议把配置里的数据源/数传串口对调")
        else:
            logging.warning(
                f"在 {chosen_port} @ {chosen_baud}bps 检测到 NMEA 但没有 RTCM："
                f"按可用数据源继续，云端上传会等 RTCM 帧出现后再认证")
        # 别把数传口写进"数据源串口"配置：只临时用它起数据面，配置留给用户自己改。
        # 否则线换回去之后，配置里已经钉死了数传口，下一次又要靠扫描兜回来。
        if not is_radio and (chosen_port != port or chosen_baud != baud):
            save_serial_config(chosen_port, chosen_baud)
        return chosen_port, chosen_baud

    logging.error("未检测到任何串口数据（模块未插入或未输出），请检查设备连接")
    return None, None


def test_serial(port: str, baud: int, check_rtcm: bool = True,
                accept_nmea: bool = False) -> bool:
    """兼容包装：返回 bool。新代码请直接用 `_probe_serial()` 拿三态结果。

    check_rtcm=True  —— 必须解析出至少一个 RTCM 帧才算通过
    accept_nmea=True —— 退一步：像 NMEA（`$G`/`$B`/`$P`/`$I` + 逗号）也算通过。
                        这只用来"找到端口"，不能用来判断"能不能上传"。

    不再套 `@retry`：一次探测本身就是 2 秒窗口，重试 3 次就是一档 8 秒，
    11 档波特率扫下来要 88 秒，启动时表现为"卡死"。重复探测由 `detect_serial()`
    的多轮调用兜住，不需要在单次探测里叠。
    """
    verdict = _probe_serial(port, baud)
    if verdict == 'rtcm':
        return True
    if not check_rtcm:
        return verdict is not None
    return bool(accept_nmea and verdict == 'nmea')


def upload_via_serial(ser, cfg, thread_exit_event):
    """串口数据源：只负责「读串口 → 交给 distribute()」。

    注意这里不再有任何 caster.sendall / 重连逻辑——那正是历史上
    "上传端一卡住，串口读取停摆、本地 Caster 和数传一起断流"的根因。
    推流、握手、断线重连全部由 CasterUploader 在自己的线程里完成。
    """
    program_start = time.time()
    last_clear = program_start
    last_status_update = program_start
    last_data_ts = program_start
    total_bytes = 0

    # 增量更新，不整体替换全局 dict：避免覆盖其他模块写入的 radio / local_caster 字段
    # mode 给人看（日志/控制台），mode_key 给前端查 i18n 表——否则英文界面会漏出中文
    state.run_status["mode"] = "串口模式"
    state.run_status["mode_key"] = "serial"
    state.run_status["data_source"] = f"{cfg['serial_port']} @ {cfg['baud_rate']}bps"
    state.run_status["target_caster"] = describe_targets(cfg)
    # 结构化版本：英文界面靠它重新拼，避免直接显示中文
    state.run_status["target_parts"] = describe_target_parts(cfg)
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
                        last_data_ts = time.time()
                        logging.info(f"串口重连成功: {port} @ {baud}")
                    except Exception as e:
                        logging.error(f"串口重建失败: {e}")
                        if interruptible_sleep(5, thread_exit_event):
                            break
                        continue
                else:
                    if interruptible_sleep(5, thread_exit_event):
                        break
                    continue

            data = ser.read(ser.in_waiting or 1024)

            if not data:
                # 空读看门狗：部分 USB 转串口驱动在设备被拔掉后不抛异常，
                # 只是 read() 一直返回空字节。没有这条，程序会永远睡在 0.2 秒
                # 的空转里，模块插回来也发现不了，现场只能重启进程。
                if time.time() - last_data_ts > SERIAL_EMPTY_WATCHDOG:
                    raise serial.SerialException(
                        f"串口连续 {SERIAL_EMPTY_WATCHDOG:.0f} 秒没有任何数据，主动关闭重建")
                time.sleep(0.2)
                continue

            last_data_ts = time.time()

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

        except ThreadExitRequested:
            # detect_serial() 扫描途中收到重启/退出信号时抛的是它。
            # 必须放在 `except Exception` 前面，否则会被当成"串口读取失败: "
            # （异常不带消息，冒号后空着）吞掉，白等一轮退避。
            break
        except Exception as e:
            logging.error(f"串口读取失败: {e}")
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            ser = None
            if interruptible_sleep(3, thread_exit_event):
                break

    # 退出时只关自己持有的串口；上传连接由 CasterUploader 自己关闭
    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass
