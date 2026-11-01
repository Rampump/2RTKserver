# -*- coding: utf-8 -*-
"""局域网固定地址：mDNS / DNS-SD 广播（唯一新增能力，零业务依赖）。

这个文件刻意不 import 任何项目内模块，只用标准库 + zeroconf。
对外只有四个方法：start / set_ntrip / snapshot / stop，由 core.lifecycle 调用。

能解决什么：
    同一内网里，Windows / macOS / iPhone / iPad / Linux(avahi) 用
    http://2rtk.local:5757 就能打开配置页，不需要知道本机 IP。

不能解决什么（协议层事实，不是实现不到位）：
    **Android 系统级不支持 mDNS 解析**，安卓浏览器基本打不开 .local。
    安卓请用首页「运行状态」里显示的本机 IP 访问。

关键实现说明：
  1. `2rtk.local:<任意端口>` 只靠 A 记录就能用 —— 注册 _http._tcp 时带上
     server='2rtk.local.' 与全部非回环 IPv4，zeroconf 会一并应答 A 查询。
     `_ntrip._tcp` 只是让人/工具能看到端口号，注册失败也不影响连通性。
  2. 只开 IPv4：ARM 板的 IPv6 链路本地地址会把 SRV 记录搞坏。
  3. 30 秒看门狗：换 Wi-Fi / DHCP 续租导致 IP 变化时自动 update_service，
     名字不变；同时回调 on_address_change，让首页 IP 跟着刷新。
  4. 名字冲突：同级网段里已有一台 2rtk 时，zeroconf 自动退为 2rtk-2.local，
     只记一条 WARNING，不占 UI、不加配置项。
"""
import logging
import socket
import threading
import time

DEFAULT_HOSTNAME = '2rtk'
DEFAULT_WEB_PORT = 5757

MDNS_HTTP_TYPE = '_http._tcp.local.'
MDNS_NTRIP_TYPE = '_ntrip._tcp.local.'   # 非标准类型，仅作端口展示
WATCH_INTERVAL = 30                      # 秒：IP 变化检查周期

logger = logging.getLogger(__name__)


def _local_ipv4_addresses():
    """本机全部非回环 IPv4（默认路由地址优先）。

    不 import 项目内 utils，保持本文件可独立拷走使用。
    """
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith('127.') and ip != 'localhost':
            found.append(ip)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith('127.') and ip not in found:
                found.append(ip)
    except Exception:
        pass
    return found


class ZeroconfAnnouncer:
    """把本机以 `2rtk.local` 的名字广播到局域网。"""

    def __init__(self, hostname=DEFAULT_HOSTNAME, web_port=DEFAULT_WEB_PORT,
                 on_address_change=None):
        self.hostname = (hostname or DEFAULT_HOSTNAME).strip().rstrip('.')
        self.web_port = int(web_port or DEFAULT_WEB_PORT)
        self.on_address_change = on_address_change

        self._zc = None
        self._http_info = None
        self._ntrip_info = None
        self._addresses = []
        self._registered_name = ''
        self._state = 'stopped'      # stopped / running / unavailable
        self._last_error = ''
        self._lock = threading.Lock()
        self._watch_thread = None
        self._watch_stop = threading.Event()

    # ---------- 对外 ----------
    def start(self) -> bool:
        """注册 _http._tcp 服务（自带 A 记录）。失败只返回 False，不抛异常。"""
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
        except ImportError as e:
            # 老设备只更新代码、没更新依赖时走这里：其它功能完全不受影响
            self._state = 'unavailable'
            self._last_error = f'未安装 zeroconf（{e}）'
            logger.warning("mDNS 固定地址不可用：未安装 zeroconf 模块，"
                           "请执行 pip install 'zeroconf>=0.132.2,<0.140'；"
                           "不影响其它功能，可继续用本机 IP 访问")
            return False

        try:
            try:
                self._zc = Zeroconf(ip_version=IPVersion.V4Only)
            except TypeError:
                # 极老版本不接受 ip_version 关键字
                self._zc = Zeroconf()

            addresses = _local_ipv4_addresses()
            if not addresses:
                self._state = 'unavailable'
                self._last_error = '未找到可用的 IPv4 地址'
                logger.warning("mDNS 固定地址不可用：本机没有非回环 IPv4 地址（网络未连接？）")
                self._close_zc()
                return False

            info = ServiceInfo(
                MDNS_HTTP_TYPE,
                f"{self.hostname}.{MDNS_HTTP_TYPE}",
                addresses=[socket.inet_aton(ip) for ip in addresses],
                port=self.web_port,
                properties={
                    'path': '/',
                    'product': '2RTK NTRIP Server',
                    'version': '2.1.8',
                },
                server=f"{self.hostname}.local.",
            )
            # allow_name_change：同网段已有同名设备时自动退为 2rtk-2.local
            registered = self._zc.register_service(info, allow_name_change=True)
            if registered is not None:
                info = registered

            with self._lock:
                self._http_info = info
                self._addresses = list(addresses)
                self._registered_name = (info.name or '').split('.')[0]
                self._state = 'running'
                self._last_error = ''

            self._start_watchdog()
            self._log_started(info)
            return True

        except Exception as e:
            self._state = 'unavailable'
            self._last_error = str(e)
            logger.warning(f"mDNS 固定地址启动失败（已忽略，不影响其它功能）：{e}")
            self._close_zc()
            return False

    def set_ntrip(self, port, mountpoint='2RTK'):
        """广播本地 Caster 的端口（仅作展示）。port 为 None/0 时取消广播。"""
        if self._zc is None:
            return False
        try:
            from zeroconf import ServiceInfo
        except ImportError:
            return False

        lock = self._lock
        with lock:
            old = self._ntrip_info
            self._ntrip_info = None

        if old is not None:
            try:
                self._zc.unregister_service(old)
            except Exception:
                pass

        try:
            port = int(port or 0)
        except (TypeError, ValueError):
            port = 0
        if port <= 0:
            return True

        try:
            info = ServiceInfo(
                MDNS_NTRIP_TYPE,
                f"{self.hostname}.{MDNS_NTRIP_TYPE}",
                addresses=[socket.inet_aton(ip) for ip in (self._addresses or _local_ipv4_addresses())],
                port=port,
                properties={
                    'mountpoint': mountpoint or '2RTK',
                    'user': '2RTK',
                    'note': 'NTRIP Caster on this device',
                },
                server=f"{self.hostname}.local.",
            )
            registered = self._zc.register_service(info, allow_name_change=True)
            if registered is not None:
                info = registered
            with lock:
                self._ntrip_info = info
            logger.info(f"mDNS 已广播本地 Caster 端口：{port}（{mountpoint}）")
            return True
        except Exception as e:
            # 这一路只是"锦上添花"，失败不影响 2rtk.local:2102 可用（靠 A 记录）
            logger.warning(f"mDNS 广播本地 Caster 端口失败（不影响取流）：{e}")
            return False

    def snapshot(self) -> dict:
        with self._lock:
            name = self._registered_name or self.hostname
            ntrip_port = None
            if self._ntrip_info is not None:
                try:
                    ntrip_port = self._ntrip_info.port
                except Exception:
                    ntrip_port = None
            return {
                'state': self._state,
                'name': f'{name}.local',
                'hostname': self.hostname,
                'web_url': f'http://{name}.local:{self.web_port}',
                'ntrip_url': (f"{name}.local:{ntrip_port}/2RTK" if ntrip_port else ''),
                'addresses': list(self._addresses),
                'last_error': self._last_error,
            }

    def stop(self):
        """注销服务（发 TTL=0 道别包）并关闭。"""
        self._watch_stop.set()
        thread = self._watch_thread
        self._watch_thread = None
        if thread is not None and thread.is_alive():
            thread.join(2)

        zc = self._zc
        infos = []
        with self._lock:
            if self._http_info is not None:
                infos.append(self._http_info)
            if self._ntrip_info is not None:
                infos.append(self._ntrip_info)
            self._http_info = None
            self._ntrip_info = None
            self._state = 'stopped'

        if zc is not None:
            for info in infos:
                try:
                    # unregister_service 会发送 TTL=0 的 goodbye 包，
                    # 让局域网内的缓存立刻失效，而不是等 120 秒自然过期。
                    zc.unregister_service(info)
                except Exception:
                    pass
            try:
                zc.close()
            except Exception:
                pass
        self._zc = None
        logger.info("mDNS 广播已停止（已发送道别包）")

    # ---------- 内部 ----------
    def _close_zc(self):
        zc = self._zc
        self._zc = None
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass

    def _log_started(self, info):
        name = (info.name or '').split('.')[0] or self.hostname
        if name != self.hostname:
            logger.warning(f"局域网内已存在同名设备，本机固定地址退为 http://{name}.local:{self.web_port}"
                           f"（原名字 {self.hostname}.local 被占用）")
        else:
            logger.info(f"固定地址已启用：http://{name}.local:{self.web_port}"
                        f"（地址 {', '.join(self._addresses)}）")
        logger.info("提示：Windows / macOS / iPhone 可用固定地址访问；安卓系统不支持 .local，请用本机 IP")

    def _start_watchdog(self):
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(target=self._watch_loop, name='mdns-watch', daemon=True)
        self._watch_thread.start()

    def _watch_loop(self):
        while not self._watch_stop.wait(WATCH_INTERVAL):
            try:
                current = _local_ipv4_addresses()
                if not current:
                    continue
                with self._lock:
                    changed = current != self._addresses
                    http_info = self._http_info
                if not changed or http_info is None or self._zc is None:
                    continue
                self._update_addresses(current, http_info)
            except Exception as e:
                logger.debug(f"mDNS 地址看门狗异常（忽略）：{e}")

    def _update_addresses(self, addresses, info):
        """IP 变了就更新 A 记录，名字保持不变。"""
        try:
            from zeroconf import ServiceInfo
            new_info = ServiceInfo(
                MDNS_HTTP_TYPE,
                info.name,
                addresses=[socket.inet_aton(ip) for ip in addresses],
                port=info.port,
                properties=info.properties,
                server=f"{self._registered_name or self.hostname}.local.",
            )
            self._zc.update_service(new_info)
            with self._lock:
                self._http_info = new_info
                self._addresses = list(addresses)
            logger.info(f"本机 IP 已变化，mDNS 记录已刷新：{', '.join(addresses)}")
            if self.on_address_change is not None:
                try:
                    self.on_address_change(addresses)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"刷新 mDNS 地址失败（将在下个周期重试）：{e}")


__all__ = ['ZeroconfAnnouncer', 'DEFAULT_HOSTNAME', 'DEFAULT_WEB_PORT',
           'MDNS_HTTP_TYPE', 'MDNS_NTRIP_TYPE', 'WATCH_INTERVAL']
