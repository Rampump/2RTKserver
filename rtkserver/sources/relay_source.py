# -*- coding: utf-8 -*-
"""数据源②：中继（作为 NTRIP 客户端从上游 Caster 拉流）。

与串口模式的唯一区别就是数据从哪来；拿到数据之后的分发路径完全一致。
同样不持有 caster 连接 —— 上传由 CasterUploader 的独立线程负责。
"""
import logging
import socket
import time

from ..core import state
from ..core.constants import ART_LOGO, RELAY_IDLE_RECONNECT, RELAY_READ_TIMEOUT
from ..core.distributor import distribute
from ..core.status import describe_targets, push_run_status
from ..utils.common import (ThreadExitRequested, clear_screen, format_bytes,
                            format_duration, interruptible_sleep)
from .ntrip import connect_to_host, handshake_ntrip_client


def open_relay(cfg):
    sock = connect_to_host(cfg['relay_host'], cfg['relay_port'])
    if not handshake_ntrip_client(sock, cfg):
        sock.close()
        raise RuntimeError('中继认证失败')
    # 读超时交给 forward_relay 解释为"本轮没有数据"，而不是致命错误
    sock.settimeout(RELAY_READ_TIMEOUT)
    return sock


def forward_relay(relay, cfg, thread_exit_event):
    """中继数据源：只负责「从上游拉 RTCM → 交给 distribute()」。"""
    program_start = time.time()
    last_clear = program_start
    last_recv = program_start
    last_status_update = program_start
    total_bytes = 0

    state.run_status["mode"] = "中继模式"
    state.run_status["data_source"] = f"{cfg['relay_host']}:{cfg['relay_port']}/{cfg['relay_mountpoint']}"
    state.run_status["target_caster"] = describe_targets(cfg)
    push_run_status()

    while not thread_exit_event.is_set():
        try:
            timed_out = False
            eof = False
            try:
                data = relay.recv(4096)
                # recv 返回空字节 = 对端已经关闭连接（不是超时）
                eof = not data
            except socket.timeout:
                # 读超时只代表"这一轮没有数据"，不代表连接坏了。
                # 原先没有单独捕获，socket.timeout 会掉进下面的 except，
                # 被当成致命错误把 relay 和 caster 两条连接一起断掉重连。
                data = b""
                timed_out = True

            if data:
                last_recv = time.time()
                total_bytes += len(data)
                # 与串口模式完全相同的统一分发路径
                distribute(data)
            elif eof:
                # 对端主动断开：立刻重连，不必等满 120 秒
                logging.warning("中继连接已被对端关闭，立即重新连接")
                try:
                    relay.close()
                except Exception:
                    pass
                relay = open_relay(cfg)
                last_recv = time.time()
            elif timed_out:
                # 读超时后走到这里，120 秒空闲重连规则现在才真正生效
                if time.time() - last_recv > RELAY_IDLE_RECONNECT:
                    logging.warning(f"中继连接连续 {RELAY_IDLE_RECONNECT} 秒无数据，尝试重新连接")
                    try:
                        relay.close()
                    except Exception:
                        pass
                    relay = open_relay(cfg)
                    last_recv = time.time()
                else:
                    time.sleep(0.1)

            current_time = time.time()
            if current_time - last_clear > 100:
                clear_screen()
                print(ART_LOGO)
                print(f"中继模式: {cfg['relay_host']}:{cfg['relay_port']}/{cfg['relay_mountpoint']}")
                print(f"数据出口: {describe_targets(cfg)}")
                print(f"运行时间: {format_duration(current_time - program_start)}")
                print(f"接收数据量: {format_bytes(total_bytes)}")
                last_clear = current_time

            if current_time - last_status_update > 2:
                state.run_status["data_sent"] = format_bytes(total_bytes)
                state.run_status["run_time"] = format_duration(current_time - program_start)
                push_run_status()
                last_status_update = current_time

        except ThreadExitRequested:
            break
        except Exception as e:
            logging.error(f"中继模式错误: {str(e)}")
            # 只重连数据源本身。上传出口有自己的重连循环，互不牵连。
            try:
                if relay is not None:
                    relay.close()
            except Exception:
                pass
            relay = None
            while relay is None and not thread_exit_event.is_set():
                try:
                    relay = open_relay(cfg)
                    logging.info("中继数据源重新连接成功")
                except ThreadExitRequested:
                    break
                except Exception as e2:
                    logging.error(f"中继数据源重新连接失败: {e2}")
                    if interruptible_sleep(15):
                        break

    # 退出时只关自己持有的中继连接
    if relay is not None:
        try:
            relay.close()
        except Exception:
            pass
