# -*- coding: utf-8 -*-
"""数据源②：中继（作为 NTRIP 客户端从上游 Caster 拉流）。

与串口模式的唯一区别就是数据从哪来；拿到数据之后的分发路径完全一致。
同样不持有 caster 连接 —— 上传由 CasterUploader 的独立线程负责。
"""
import logging
import socket
import time

from ..core import state
from ..core.constants import (ART_LOGO, BACKOFF_JITTER, NETWORK_BACKOFF,
                              RELAY_EOF_BACKOFF, RELAY_IDLE_RECONNECT,
                              RELAY_READ_TIMEOUT)
from ..core.distributor import distribute
from ..core.status import describe_target_parts, describe_targets, push_run_status
from ..utils.common import (Backoff, ThreadExitRequested, clear_screen, format_bytes,
                            format_duration, interruptible_sleep)
from .ntrip import connect_to_host, handshake_ntrip_client
from .rtcm_parser import reset_stats


def _note_source_retry(kind: str, backoff: Backoff, detail: str = '', delay: float = 0.0):
    """把"正在重连、第几次、多久后重试"写进 run_status，前端据此显示提示条。

    原先中继重连对前端完全不可见：页面只是"数据源没数据"，用户分不清是
    上游断流、账号被占还是密码错。这里不新增轮询，只在重连的那一刻写一次字段。
    """
    state.run_status['source_retry'] = {
        'kind': kind,
        'count': int(backoff.step),
        'next_delay': float(delay if delay else backoff.peek_delay()),
        'detail': detail[:200],
    }


def _clear_source_retry():
    """一旦真的收到数据，把重连提示清掉（否则页面会一直挂着旧文案）。"""
    if state.run_status.get('source_retry'):
        state.run_status['source_retry'] = {}


def open_relay(cfg):
    sock = connect_to_host(cfg['relay_host'], cfg['relay_port'])
    if not handshake_ntrip_client(sock, cfg):
        sock.close()
        raise RuntimeError('中继认证失败')
    # 读超时交给 forward_relay 解释为"本轮没有数据"，而不是致命错误
    sock.settimeout(RELAY_READ_TIMEOUT)
    # 每次建连都是一条新会话：上一路的消息类型、频率、基站坐标必须作废。
    # 不清的话换了上游（EOF 重连、同名源被顶替）页面还挂着旧源的 RTCM 统计，
    # 用户看到的就是"换了源还是以前的数据"。三处重连都走这里，无需逐个加。
    reset_stats()
    return sock


def forward_relay(relay, cfg, thread_exit_event):
    """中继数据源：只负责「从上游拉 RTCM → 交给 distribute()」。"""
    program_start = time.time()
    last_clear = program_start
    last_recv = program_start
    last_status_update = program_start
    total_bytes = 0

    state.run_status["mode"] = "中继模式"
    state.run_status["mode_key"] = "relay"
    state.run_status["data_source"] = f"{cfg['relay_host']}:{cfg['relay_port']}/{cfg['relay_mountpoint']}"
    state.run_status["target_caster"] = describe_targets(cfg)
    # 结构化版本：英文界面靠它重新拼，避免直接显示中文
    state.run_status["target_parts"] = describe_target_parts(cfg)
    push_run_status()

    eof_backoff = Backoff(RELAY_EOF_BACKOFF, jitter=BACKOFF_JITTER, name='中继对端关闭')
    net_backoff = Backoff(NETWORK_BACKOFF, jitter=BACKOFF_JITTER, name='中继连接失败')

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
                # 真的收到数据才算恢复：退避退回最快档，前端提示也清掉。
                # 注意不能在"建连成功"时 reset —— 对端"接受后立刻关闭"的场景里，
                # 那样会变成零延迟无限重连，正是要防的风暴。
                eof_backoff.reset()
                net_backoff.reset()
                _clear_source_retry()
                # 与串口模式完全相同的统一分发路径
                distribute(data)
            elif eof:
                # 对端主动断开：等一个递增的间隔再重连，绝不留零延迟路径。
                # 原先这里立刻 open_relay，遇到"接受连接后马上关闭"的上游
                # （同名源已在线 / caster 内部报错）就是毫秒级重连风暴，
                # 会把自己 caster 的"每用户 3 条连接"直接打满。
                delay = eof_backoff.next_delay()
                logging.warning(
                    f"中继连接已被对端关闭，{delay:.0f} 秒后重连"
                    f"（{eof_backoff.describe()}）")
                _note_source_retry('relay_eof', eof_backoff,
                                   '上游 caster 主动关闭连接，疑似同名源已在线上',
                                   delay)
                try:
                    relay.close()
                except Exception:
                    pass
                if interruptible_sleep(delay, thread_exit_event):
                    break
                relay = open_relay(cfg)
                last_recv = time.time()
            elif timed_out:
                # 读超时后走到这里，120 秒空闲重连规则现在才真正生效
                if time.time() - last_recv > RELAY_IDLE_RECONNECT:
                    logging.warning(f"中继连接连续 {RELAY_IDLE_RECONNECT} 秒无数据，尝试重新连接")
                    _note_source_retry('relay_idle', eof_backoff,
                                       f'上游连续 {RELAY_IDLE_RECONNECT} 秒无数据', 2.0)
                    try:
                        relay.close()
                    except Exception:
                        pass
                    if interruptible_sleep(2.0, thread_exit_event):
                        break
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
                    # 网络类失败按 NETWORK_BACKOFF 走：2→5→10→20→30 秒封顶。
                    # 原先是固定 15 秒，连不上和认证被拒一个节奏，且没有上限保护。
                    delay = net_backoff.next_delay()
                    logging.error(f"中继数据源重新连接失败: {e2}"
                                  f"（{net_backoff.describe()}）")
                    _note_source_retry('relay_error', net_backoff, str(e2), delay)
                    if interruptible_sleep(delay, thread_exit_event):
                        break

    # 退出时只关自己持有的中继连接
    if relay is not None:
        try:
            relay.close()
        except Exception:
            pass
