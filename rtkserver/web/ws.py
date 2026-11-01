# -*- coding: utf-8 -*-
"""WebSocket 监控推送。

一个连接 = 一个待发队列。生产方只往队列里 append（deque 满自动丢最旧，永不阻塞），
发送全部收敛到该连接自己的处理线程里，一个连接只有一个发送者。

为什么不能直接 ws.send：
  1. ws.send 是同步阻塞调用，simple_websocket 内部是单次 sock.send，没有超时。
     一个不读数据的客户端会把"调用 send 的那个线程"挂住 —— 而调用方可能是
     RTCM 解析线程或推流线程，等于监控拖累了主业务。
  2. simple_websocket 内部还有一个读线程在用同一个 wsproto 连接做解析，
     多个线程同时 send 会让 wsproto 状态机竞争，帧可能交错。
"""
import json
import logging
from collections import deque
from typing import Optional

from flask import session

from ..core import state
from ..core.constants import (MAX_WS_CLIENTS, PUBLIC_STATION_COORDS,
                              WS_DROP_LIMIT, WS_OUTBOX_MAXLEN,
                              WS_RECEIVE_POLL)


class WSClient:
    """一个监控连接 = 一个待发队列。"""
    __slots__ = ('ws', 'authenticated', 'outbox', 'alive', 'dropped')

    def __init__(self, ws, authenticated: bool):
        self.ws = ws
        self.authenticated = authenticated
        self.outbox = deque(maxlen=WS_OUTBOX_MAXLEN)
        self.alive = True
        self.dropped = 0

    def enqueue(self, payload: str):
        if not self.alive:
            return
        if len(self.outbox) >= WS_OUTBOX_MAXLEN:
            self.dropped += 1
            if self.dropped > WS_DROP_LIMIT:
                # 队列持续满 = 客户端长期不消费，主动断开，把连接名额让出来
                self.alive = False
                return
        else:
            self.dropped = 0
        self.outbox.append(payload)

    def flush(self) -> bool:
        """把队列里的帧发完。返回 False 表示连接应终止。"""
        while self.outbox:
            if not self.alive:
                return False
            try:
                self.ws.send(self.outbox.popleft())
            except Exception:
                self.alive = False
                return False
        return True


def register_client(ws) -> Optional[WSClient]:
    """登记一个监控连接；超过上限返回 None。"""
    with state.clients_lock:
        if len(state.clients) >= MAX_WS_CLIENTS:
            return None
        client = WSClient(ws, bool(session.get('logged_in')))
        state.clients.append(client)
        return client


def unregister_client(client: WSClient):
    with state.clients_lock:
        try:
            state.clients.remove(client)
        except ValueError:
            pass


def _redact_for_public(message: dict) -> dict:
    """给未登录访问者的脱敏副本。

    状态页是公开的（方便随手看一眼信号质量），但基准站精确坐标、
    ECEF、设备序列号属于敏感资产信息，不应该匿名可见。
    把环境变量 PUBLIC_STATION_COORDS 设为 1 可恢复"完全公开"的旧行为。
    """
    if not isinstance(message, dict):
        return message
    if 'station_info' in message and isinstance(message.get('station_info'), dict):
        info = message['station_info']
        message = dict(message)
        message['station_info'] = {
            'station_id': info.get('station_id'),
            'masked': True
        }
    for key in ('antenna_info', 'device_info'):
        if isinstance(message.get(key), dict):
            item = dict(message[key])
            item.pop('serial_number', None)
            item.pop('receiver_serial', None)
            message = dict(message)
            message[key] = item
    return message


def send_to_clients(message):
    """广播一条消息：只做序列化 + 入队，不做任何 socket I/O。

    因为不碰 socket，即使客户端卡死也不会阻塞调用方
    （调用方可能是 RTCM 解析线程或推流线程）。
    """
    with state.clients_lock:
        snapshot = list(state.clients)
    if not snapshot:
        return

    authed_json = None
    public_json = None

    for client in snapshot:
        if client.authenticated or PUBLIC_STATION_COORDS:
            if authed_json is None:
                authed_json = json.dumps(message)
            client.enqueue(authed_json)
        else:
            if public_json is None:
                public_json = json.dumps(_redact_for_public(message))
            client.enqueue(public_json)


def bind_publish():
    """把 send_to_clients 挂到 state.publish，让底层（日志/解析）无需 import web。"""
    state.publish = send_to_clients


def websocket(ws):
    # 状态页本身是公开的，所以这里不做拦截，只记录"这个连接是否已登录"，
    # 由 send_to_clients 决定是否下发基准站精确坐标（见 _redact_for_public）。
    client = register_client(ws)
    if client is None:
        logging.warning(f"监控连接数已达上限 {MAX_WS_CLIENTS}，拒绝新连接")
        try:
            ws.send(json.dumps({
                "type": "error",
                "message": f"监控连接数已达上限（{MAX_WS_CLIENTS}），请稍后再试"
            }))
            ws.close()
        except Exception:
            pass
        return

    try:
        # 首帧历史也走队列，保证这个连接自始至终只有一个线程在 send
        all_history = list(state.log_history['log']) + list(state.log_history['print'])
        client.enqueue(json.dumps({
            "type": "history",
            "messages": all_history
        }))
        # 首帧补一条本地 Caster 状态：正常情况下 run_status 每 2 秒刷一次就够了，
        # 但 mode=none 时根本没有数据循环在推，首页会一直空着，所以连接时先给一次。
        from ..outlets.local_caster import local_caster_snapshot
        client.enqueue(json.dumps({
            "type": "local_caster",
            "local_caster": local_caster_snapshot()
        }))
        # 数传透传状态同样补一条首帧兜底（mode=none 时靠它显示）
        from ..outlets.radio_bridge import serial_bridge_snapshot
        client.enqueue(json.dumps({
            "type": "radio",
            "radio": serial_bridge_snapshot()
        }))
        # 运行状态首帧：首次使用（还没配置数据源）时没有任何循环在推状态，
        # 首页必须靠这一帧显示本机 IP 与固定访问地址。
        from ..core.status import initial_run_status
        client.enqueue(json.dumps({
            "run_status": initial_run_status()
        }))

        # 单线程同时负责"发"和"收"：
        #   flush() 把队列发空 -> receive() 阻塞等待客户端消息/心跳，超时即回到循环顶部再发。
        # 这样每条连接只有一个发送者，彻底避免多线程写同一 wsproto。
        while client.alive:
            if not client.flush():
                break
            msg = ws.receive(timeout=WS_RECEIVE_POLL)
            if msg is None:
                continue   # 超时：正常，回到顶部继续发送
    except Exception:
        pass
    finally:
        client.alive = False
        unregister_client(client)


def register(sock):
    """把 /ws 挂到 flask-sock 上。"""
    sock.route('/ws')(websocket)
    bind_publish()
