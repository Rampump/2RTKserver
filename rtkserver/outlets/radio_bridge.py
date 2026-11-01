# -*- coding: utf-8 -*-
"""出口③：无线电数传透传（Radio Serial Bridge）。

设计目标：把本机正在转发的同一路 RTCM 数据，额外以"串口透传"的方式送给一个
USB 数传电台（发送端），让附近无网络的流动站通过配套数传电台直接收差分，
无需 NTRIP 账号密码。

关键取舍（与本地 Caster 相同的思路，但更强调"单向、只发不收、绝不影响主链路"）：
  1. 独立开关：radio_enabled 与 local_caster_enabled、运行模式三者完全解耦，
     关掉数传不影响本地 Caster，反之亦然；两者可同时开、也可独立开。
  2. 独立线程 + 有界队列：串口写入在专用线程里做，publish 永远是非阻塞 put_nowait，
     串口卡死/写慢只会丢弃数传这一路的数据，绝不拖累串口/中继主数据流。
  3. 只发不收（单向广播）：数传场景 base 只发、流动站只收，串口以纯写打开，
     不读任何回传字节，避免与 GNSS 模块串口产生任何读冲突。
  4. 断线重连：串口被拔/写失败时自动关串口、定时重试重开，期间静默丢弃数据，
     不向上抛异常、不影响主链路。
"""
import logging
import queue
import threading
import time
from typing import Optional

import serial

from ..core import state
from ..core.config import get_config
from ..core.constants import RADIO_QUEUE_SIZE, RADIO_RECONNECT_INTERVAL


class SerialBridge:
    """把 RTCM 字节流透传给一个 USB 数传串口（单向发送）。"""

    def __init__(self, port: str, baud: int):
        self.port = port
        self.baud = baud
        self._ser = None
        self._thread = None
        self._running = False
        self._outbox = queue.Queue(maxsize=RADIO_QUEUE_SIZE)
        self._connected = False
        self._sent_bytes = 0
        self._reconnects = 0
        self._last_error = ''
        self._rate_last_time = 0.0
        self._rate_last_bytes = 0
        self._rate_bps = 0.0
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """启动发送线程。串口打不开也照常起线程，由线程内的重连逻辑兜底。

        返回 True 表示线程已启动（含"串口尚未打开、后台持续重试"的情况）。
        """
        self._running = True
        # 先同步打开一次串口，再起线程。
        # 原先是在线程 start() 之后立刻读 self._connected——那时 _send_loop 还没跑到 _open()，
        # _connected 必然是 False，于是串口明明正常也会打印"未打开，将持续重试"，误导排查。
        self._open()
        if self._connected:
            logging.info(f"数传透传已启动：{self.port} @ {self.baud}bps（单向广播，只发不收）")
        else:
            logging.warning(f"数传透传已启用但串口未打开，将持续重试：{self.port} @ {self.baud}")
        self._thread = threading.Thread(target=self._send_loop, name='radio-bridge', daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        self._close_serial()
        # 清空队列里残留的待发数据，避免下次启动后立刻把旧数据灌给电台
        try:
            while True:
                self._outbox.get_nowait()
        except queue.Empty:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(2)
        self._thread = None

    # ---------- 数据入口（非阻塞，永不影响调用方） ----------
    def publish(self, data: bytes):
        if not data or not self._running:
            return
        try:
            self._outbox.put_nowait(data)
        except queue.Full:
            # 数传串口写得太慢，队列积压：丢最旧，保证永远是最新的差分出去
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                pass
            try:
                self._outbox.put_nowait(data)
            except queue.Full:
                pass

    # ---------- 对外状态 ----------
    def status(self) -> dict:
        with self._lock:
            return {
                'running': self._running,
                'connected': self._connected,
                'port': self.port,
                'baud': self.baud,
                'sent_bytes': self._sent_bytes,
                'rate_bps': self._rate_bps,
                'reconnects': self._reconnects,
                'last_error': self._last_error,
                'queue_size': self._outbox.qsize(),
            }

    # ---------- 内部 ----------
    def _open(self) -> bool:
        try:
            new_ser = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                timeout=0.5,
                write_timeout=1.0,
            )
            new_ser.reset_output_buffer()
            with self._lock:
                self._ser = new_ser
                self._connected = True
                self._last_error = ''
            return True
        except (serial.SerialException, OSError, ValueError) as e:
            with self._lock:
                self._connected = False
                self._last_error = str(e)
            logging.error(f"数传串口打开失败 {self.port} @ {self.baud}: {e}")
            return False

    def _close_serial(self):
        with self._lock:
            ser = self._ser
            self._ser = None
            self._connected = False
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _send_loop(self):
        # 首次进入时若串口还没打开（配置保存时串口未插/打不开），先尝试打开一次
        if self._ser is None:
            self._open()
        while self._running:
            try:
                data = self._outbox.get(timeout=0.5)
            except queue.Empty:
                # 没数据时也要给"尚未打开串口"的情况一个重试打开的机会
                if self._ser is None and self._running:
                    self._open()
                    if self._ser is None:
                        time.sleep(RADIO_RECONNECT_INTERVAL)
                continue

            # 在锁内取串口对象的快照，避免 stop() 并发把 self._ser 置 None 后
            # 这里仍对 None 调用 write/flush 导致 AttributeError。
            with self._lock:
                ser = self._ser
            if ser is None:
                # 串口还没打开：丢弃这条数据，并尝试打开
                self._open()
                if self._ser is None:
                    time.sleep(RADIO_RECONNECT_INTERVAL)
                continue

            try:
                ser.write(data)
                ser.flush()
                with self._lock:
                    self._sent_bytes += len(data)
                self._tick_rate(len(data))
            except (serial.SerialException, OSError, serial.SerialTimeoutException) as e:
                logging.error(f"数传串口写入失败：{e}")
                with self._lock:
                    self._reconnects += 1
                    self._last_error = str(e)
                self._close_serial()
                # 断线重连：定时重试重开，期间继续消费队列丢弃数据，不阻塞主链路
                while self._running and self._ser is None:
                    if not self._open():
                        time.sleep(RADIO_RECONNECT_INTERVAL)
                    else:
                        logging.info(f"数传串口重连成功：{self.port} @ {self.baud}")
                        break
            except Exception as e:
                logging.error(f"数传串口发送异常：{e}")
                self._close_serial()
        self._close_serial()

    def _tick_rate(self, nbytes: int):
        now = time.time()
        with self._lock:
            if now - self._rate_last_time >= 2.0:
                elapsed = now - self._rate_last_time
                if elapsed > 0:
                    self._rate_bps = (self._sent_bytes - self._rate_last_bytes) / elapsed
                self._rate_last_time = now
                self._rate_last_bytes = self._sent_bytes


# ==================== 对外接口（单例存放在 core.state） ====================
def start_serial_bridge(cfg) -> Optional[SerialBridge]:
    """按配置启动数传透传；失败只记日志，不影响主链路。"""
    stop_serial_bridge()
    port = (cfg.get('radio_port') or '').strip()
    if not port:
        logging.info("数传透传未配置串口，跳过启动")
        return None
    try:
        baud = int(cfg.get('radio_baud') or 115200)
    except (TypeError, ValueError):
        baud = 115200
    bridge = SerialBridge(port, baud)
    bridge.start()
    # 串口暂时打不开也没关系：线程会在后台持续重试，状态页能看到"已启用但未连接"
    state.serial_bridge = bridge
    return bridge


def stop_serial_bridge():
    if state.serial_bridge is not None:
        state.serial_bridge.stop()
        state.serial_bridge = None
        logging.info("数传透传已停止")


def publish(data: bytes):
    """把数据额外透传一份给数传串口（没启用时是空操作，永不影响主链路）。"""
    if state.serial_bridge is not None:
        state.serial_bridge.publish(data)


def serial_bridge_snapshot() -> dict:
    """数传透传的对外可见快照，供状态页卡片使用（不含任何口令，可公开推送）。"""
    cfg = get_config()
    enabled = str(cfg.get('radio_enabled', '0')) == '1'
    if state.serial_bridge is not None:
        info = state.serial_bridge.status()
    else:
        try:
            baud = int(cfg.get('radio_baud') or 115200)
        except (TypeError, ValueError):
            baud = 115200
        info = {
            'running': False,
            'connected': False,
            'port': cfg.get('radio_port') or '',
            'baud': baud,
            'sent_bytes': 0,
            'rate_bps': 0.0,
            'reconnects': 0,
            'last_error': '',
            'queue_size': 0,
        }
    info['enabled'] = enabled
    return info
