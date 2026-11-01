# -*- coding: utf-8 -*-
"""出口①：上传到云端 NTRIP Caster（Caster Uploader）。

历史问题（上一轮改造修复的核心）：原先 caster.sendall() 直接写在数据源循环里，
上传端一旦超时/断线，重连的 while 循环就把整个数据源循环卡住——
表现为"上传挂了，本地 Caster 和数传同时断流"。

现在的结构：独立线程 + 有界队列。数据源只做一次非阻塞 put_nowait，
TCP 建连、NTRIP SOURCE 握手、推流、断线重连全部在本线程内完成。
上传端怎么超时重连，都不影响数据源读取和其他两个出口。
"""
import logging
import queue
import threading
import time
from typing import Optional

from ..core import state
from ..core.constants import (CASTER_QUEUE_SIZE, CASTER_RECONNECT_INTERVAL,
                              CASTER_WRITE_TIMEOUT, NTRIP_HANDSHAKE_TIMEOUT,
                              UPLOAD_STATE_TEXT)
from ..sources.ntrip import connect_to_host
from ..utils.common import format_duration

# ==================== 上传端（远端 Caster）链路状态 ====================
# 只描述"往哪推 / 认证是否通过 / 推了多少"，绝不含口令。
upload_status = {
    "enabled": False,
    "state": "idle",            # idle / connecting / authed / retrying / error
    "state_text": "未配置",
    "host": "",
    "port": "",
    "mountpoint": "",
    "target": "不上传（仅本地 Caster）",
    "sent_bytes": 0,
    "sent_frames": 0,
    "rate_bps": 0,              # 实测字节/秒
    "reconnects": 0,
    "last_error": "",
    "session_time": "0h 0m 0s",
}
_upload_rate_last = {"time": 0.0, "bytes": 0}
_upload_session_start = 0.0


def upload_snapshot(cfg=None) -> dict:
    """首页展示用的上传链路快照。口令类字段一律不下发。

    状态来源优先级：运行中的 CasterUploader 实例 > 全局 upload_status 兜底。
    这样状态页读到的是真实推流线程的统计，而不是另外维护的一份影子数据。
    """
    if state.caster_uploader is not None:
        return state.caster_uploader.status()
    info = dict(upload_status)
    info.pop('secret_key', None)
    if cfg is not None:
        # 只看独立开关。未启用时把地址一起清掉，避免把历史残留值显示成"正在上传"。
        enabled = str(cfg.get('caster_enabled', '0')) == '1'
        info['enabled'] = enabled
        if enabled:
            info['target'] = (f"{cfg.get('caster_host', '')}:{cfg.get('caster_port', '')}"
                              f"/{cfg.get('caster_mountpoint', '')}")
        else:
            info['target'] = "未启用"
            info['host'] = ''
            info['port'] = ''
            info['mountpoint'] = ''
    return info


def mark_upload_state(state_name: str, cfg=None, error: str = '', reconnect: bool = False):
    """更新上传端链路状态。state_name 见 UPLOAD_STATE_TEXT。"""
    global _upload_session_start
    upload_status['state'] = state_name
    upload_status['state_text'] = UPLOAD_STATE_TEXT.get(state_name, state_name)
    if cfg is not None:
        enabled = str(cfg.get('caster_enabled', '0')) == '1'
        upload_status['enabled'] = enabled
        if enabled:
            upload_status['host'] = cfg.get('caster_host') or ''
            upload_status['port'] = str(cfg.get('caster_port') or '')
            upload_status['mountpoint'] = cfg.get('caster_mountpoint') or ''
            upload_status['target'] = (f"{upload_status['host']}:{upload_status['port']}"
                                       f"/{upload_status['mountpoint']}")
        else:
            # 未启用：清空地址与挂载点，状态页显示"未启用"而不是残留的历史配置
            upload_status['host'] = ''
            upload_status['port'] = ''
            upload_status['mountpoint'] = ''
            upload_status['target'] = '未启用'
    if error:
        upload_status['last_error'] = error
    if reconnect:
        upload_status['reconnects'] = int(upload_status.get('reconnects', 0)) + 1
    if state_name == 'authed':
        _upload_session_start = time.time()
    elif state_name in ('idle', 'connecting', 'retrying', 'error'):
        _upload_session_start = 0.0
    try:
        state.run_status['upload'] = upload_snapshot(cfg)
    except Exception:
        pass


def _tick_upload_stats(byte_count: int):
    """累加上传字节/帧数，并按滑动窗口估算实测速率。"""
    global _upload_rate_last
    try:
        upload_status['sent_bytes'] = int(upload_status.get('sent_bytes', 0)) + byte_count
        upload_status['sent_frames'] = int(upload_status.get('sent_frames', 0)) + 1

        now = time.time()
        last = _upload_rate_last
        if last['time'] == 0.0:
            last['time'] = now
            last['bytes'] = int(upload_status['sent_bytes'])
        elif now - last['time'] >= 1.0:
            dt = now - last['time']
            db = int(upload_status['sent_bytes']) - last['bytes']
            upload_status['rate_bps'] = int(db / dt) if dt > 0 else 0
            last['time'] = now
            last['bytes'] = int(upload_status['sent_bytes'])
    except Exception:
        pass


def _reset_upload_stats():
    """新一轮会话开始（认证通过）时清零累计统计。"""
    global _upload_rate_last
    upload_status['sent_bytes'] = 0
    upload_status['sent_frames'] = 0
    upload_status['rate_bps'] = 0
    upload_status['reconnects'] = 0
    upload_status['last_error'] = ''
    _upload_rate_last = {"time": 0.0, "bytes": 0}


class CasterUploader:
    """把 RTCM 字节流推送到上游 NTRIP Caster（NTRIP 1.0 / 2.0 的 SOURCE 协议）。"""

    def __init__(self, cfg):
        self._host = (cfg.get('caster_host') or '').strip()
        self._port = str(cfg.get('caster_port') or '2101')
        self._mountpoint = (cfg.get('caster_mountpoint') or '').strip()
        self._password = cfg.get('caster_password') or ''
        self._sock = None
        self._thread = None
        self._running = False
        self._outbox = queue.Queue(maxsize=CASTER_QUEUE_SIZE)
        self._authed = False
        self._state = 'idle'
        self._state_text = UPLOAD_STATE_TEXT.get('idle', '未配置')
        self._last_error = ''
        self._sent_bytes = 0
        self._sent_frames = 0
        self._reconnects = 0
        self._session_start = 0.0
        self._rate_last_time = 0.0
        self._rate_last_bytes = 0
        self._rate_bps = 0
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        self._running = True
        self._thread = threading.Thread(target=self._loop, name='caster-upload', daemon=True)
        self._thread.start()
        logging.info(f"上传出口已启动：{self.target()}（独立线程，断线自动重连）")
        return True

    def stop(self):
        self._running = False
        self._close()
        # 清空待发队列，避免下次启动把旧数据灌给上游
        try:
            while True:
                self._outbox.get_nowait()
        except queue.Empty:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(3)
        self._thread = None

    # ---------- 数据入口（非阻塞，永不拖慢调用方） ----------
    def feed(self, data: bytes):
        if not data or not self._running:
            return
        try:
            self._outbox.put_nowait(data)
        except queue.Full:
            # 上传链路太慢导致积压：丢最旧，保证送出去的是最新差分
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                pass
            try:
                self._outbox.put_nowait(data)
            except queue.Full:
                pass

    # ---------- 对外状态 ----------
    def target(self) -> str:
        return f"{self._host}:{self._port}/{self._mountpoint}"

    def status(self) -> dict:
        with self._lock:
            session = (time.time() - self._session_start) if self._session_start else 0.0
            return {
                'enabled': True,
                'state': self._state,
                'state_text': self._state_text,
                'host': self._host,
                'port': self._port,
                'mountpoint': self._mountpoint,
                'target': self.target(),
                'sent_bytes': self._sent_bytes,
                'sent_frames': self._sent_frames,
                'rate_bps': self._rate_bps,
                'reconnects': self._reconnects,
                'last_error': self._last_error,
                'session_time': format_duration(session),
                'queue_size': self._outbox.qsize(),
            }

    # ---------- 内部 ----------
    def _set_state(self, state_name: str, error: str = None):
        with self._lock:
            self._state = state_name
            self._state_text = UPLOAD_STATE_TEXT.get(state_name, state_name)
            if error is not None:
                self._last_error = error
            if state_name == 'authed':
                self._session_start = time.time()
            elif state_name in ('idle', 'connecting', 'retrying', 'error'):
                self._session_start = 0.0

    def _close(self):
        with self._lock:
            sock = self._sock
            self._sock = None
            self._authed = False
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _sleep(self, seconds: float):
        """可被 stop() 快速打断的 sleep（不依赖全局事件，本模块独立生命周期）。"""
        end = time.time() + max(0.0, seconds)
        while self._running and time.time() < end:
            time.sleep(min(0.2, max(0.0, end - time.time())))

    def _connect(self) -> bool:
        """TCP 建连 + NTRIP SOURCE 握手。成功返回 True，失败不抛异常。"""
        self._set_state('connecting')
        try:
            sock = connect_to_host(self._host, int(self._port))
        except Exception as e:
            self._set_state('retrying', error=str(e))
            logging.error(f"上传出口连接失败：{e}")
            return False

        hdr = (
            f"SOURCE {self._password} {self._mountpoint}\r\n"
            "Source-Agent: NTRIP 2RTKserver\r\n"
            "STR: \r\n\r\n"
        )
        try:
            sock.settimeout(NTRIP_HANDSHAKE_TIMEOUT)
            sock.sendall(hdr.encode())
            resp = sock.recv(1024).decode(errors='ignore')
        except Exception as e:
            self._set_state('retrying', error=str(e))
            logging.error(f"上传出口握手失败：{e}")
            try:
                sock.close()
            except Exception:
                pass
            return False

        if 'ICY 200 OK' not in resp and '200 OK' not in resp:
            head = (resp.splitlines() or ['无响应'])[0]
            self._set_state('error', error=f"认证失败：{head}")
            logging.error(f"上传 Caster 认证失败，响应：{resp.splitlines()[:3]}")
            try:
                sock.close()
            except Exception:
                pass
            return False

        try:
            sock.settimeout(CASTER_WRITE_TIMEOUT)
        except Exception:
            pass

        with self._lock:
            self._sock = sock
            self._authed = True
            self._sent_bytes = 0
            self._sent_frames = 0
            self._rate_last_time = time.time()
            self._rate_last_bytes = 0
            self._rate_bps = 0
        self._set_state('authed')
        logging.info(f"上传出口认证通过，开始推送：{self.target()}")
        return True

    def _retry_connect(self):
        while self._running and self._sock is None:
            if self._connect():
                return
            self._sleep(CASTER_RECONNECT_INTERVAL)

    def _loop(self):
        # 首次建连：失败就退避重试，直到成功或收到停止信号
        while self._running and self._sock is None:
            if self._connect():
                break
            self._sleep(CASTER_RECONNECT_INTERVAL)

        while self._running:
            try:
                data = self._outbox.get(timeout=0.5)
            except queue.Empty:
                if self._sock is None and self._running:
                    self._retry_connect()
                continue

            # 锁内取快照，避免与 stop()/重连并发把 self._sock 置 None 后仍去 sendall
            with self._lock:
                sock = self._sock
            if sock is None:
                # 连接不可用：直接丢弃这一帧，后台继续重连
                continue

            try:
                sock.sendall(data)
                with self._lock:
                    self._sent_bytes += len(data)
                    self._sent_frames += 1
                self._tick_rate()
            except Exception as e:
                logging.error(f"上传出口发送失败：{e}")
                with self._lock:
                    self._reconnects += 1
                self._set_state('retrying', error=str(e))
                self._close()
                self._retry_connect()
        self._close()

    def _tick_rate(self):
        now = time.time()
        with self._lock:
            if now - self._rate_last_time >= 2.0:
                elapsed = now - self._rate_last_time
                if elapsed > 0:
                    self._rate_bps = int((self._sent_bytes - self._rate_last_bytes) / elapsed)
                self._rate_last_time = now
                self._rate_last_bytes = self._sent_bytes


# ==================== 对外接口（单例存放在 core.state） ====================
def start_caster_uploader(cfg) -> Optional[CasterUploader]:
    """按独立开关启动上传出口；未勾选则什么都不做。"""
    stop_caster_uploader()
    if str(cfg.get('caster_enabled', '0')) != '1':
        logging.info("未勾选「上传到云端 Caster」，跳过启动")
        return None
    if not (cfg.get('caster_host') or '').strip():
        logging.warning("已勾选上传出口但未填地址，跳过启动")
        return None
    up = CasterUploader(cfg)
    up.start()
    state.caster_uploader = up
    return up


def stop_caster_uploader():
    if state.caster_uploader is not None:
        state.caster_uploader.stop()
        state.caster_uploader = None
        logging.info("上传出口已停止")


def feed(data: bytes):
    """把数据投递给上传出口（未启用时是空操作）。"""
    if state.caster_uploader is not None:
        state.caster_uploader.feed(data)
