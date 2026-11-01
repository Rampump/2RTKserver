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
import sys
import threading
import time

DEFAULT_HOSTNAME = '2rtk'
DEFAULT_WEB_PORT = 5757

MDNS_HTTP_TYPE = '_http._tcp.local.'
MDNS_NTRIP_TYPE = '_ntrip._tcp.local.'   # 非标准类型，仅作端口展示
WATCH_INTERVAL = 30                      # 秒：IP 变化检查周期
CONFLICT_PROBE_SECONDS = 1.5             # 秒：启动时探测同名设备的观察窗口
MAX_NAME_SUFFIX = 5                      # 名字冲突时最多退到 -6

# 容器 / 虚拟机 / 网桥的虚拟网卡前缀：这些地址在局域网里不可达，
# 不能写进 A 记录（装了 Docker 的设备上 docker0 的 172.17.0.1 必然出现）。
_VIRTUAL_IF_PREFIXES = ('docker', 'br-', 'veth', 'virbr', 'lxc', 'lxd',
                        'cni', 'flannel', 'kube', 'vmnet', 'vboxnet', 'zt')

logger = logging.getLogger(__name__)


def _interface_ipv4_map():
    """{IPv4: 网卡名}，仅 Linux 有效；其它平台返回 {}。

    与 utils/network.py 中的同名逻辑刻意重复：本文件承诺"只依赖标准库 + zeroconf，
    可单独拷走使用"，为此不 import 项目内任何模块。
    """
    if not sys.platform.startswith('linux'):
        return {}
    try:
        import fcntl
        import struct
    except ImportError:
        return {}
    mapping = {}
    for _, name in socket.if_nameindex():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            req = struct.pack('256s', name[:15].encode('utf-8', 'replace'))
            addr = fcntl.ioctl(sock.fileno(), 0x8915, req)[20:24]
            mapping[socket.inet_ntoa(addr)] = name
        except OSError:
            pass
        finally:
            sock.close()
    return mapping


def _local_ipv4_addresses():
    """本机全部非回环 IPv4（默认路由地址优先）。

    不 import 项目内 utils，保持本文件可独立拷走使用。
    容器 / 网桥网卡的地址会被剔除：它们的地址在局域网里不可达，
    写进 A 记录会让客户端解析到 http://2rtk.local 时随到随挂。
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

    mapping = _interface_ipv4_map()
    if mapping and found:
        kept = [ip for ip in found
                if not mapping.get(ip, '').lower().startswith(_VIRTUAL_IF_PREFIXES)]
        if kept:
            if found[0] not in kept:
                kept.insert(0, found[0])
            found = kept
    return found


class ZeroconfAnnouncer:
    """把本机以 `2rtk.local` 的名字广播到局域网。"""

    def __init__(self, hostname=DEFAULT_HOSTNAME, web_port=DEFAULT_WEB_PORT,
                 on_address_change=None, version=''):
        self.hostname = (hostname or DEFAULT_HOSTNAME).strip().rstrip('.')
        self.web_port = int(web_port or DEFAULT_WEB_PORT)
        self.on_address_change = on_address_change
        # 版本号由调用方传入（lifecycle 从 rtkserver.version 取），
        # 本文件坚持「不 import 任何项目内模块」，不自己去读。
        self.version = (version or '').strip()

        self._zc = None
        self._http_info = None
        self._ntrip_info = None
        self._addresses = []
        # 实际对外的主机名（含 .local.）。名字冲突退让后与 self.hostname 不同，
        # 所有对外展示与 _ntrip 记录的 server 都必须用它，否则客户端会被指向别人。
        self._server = f'{self.hostname}.local.'
        self._state = 'stopped'      # stopped / running / unavailable
        self._last_error = ''
        self._lock = threading.Lock()
        self._watch_thread = None
        self._watch_stop = threading.Event()

    # ---------- 对外 ----------
    def start(self) -> bool:
        """注册 _http._tcp 服务（自带 A 记录）。失败只返回 False，不抛异常。"""
        try:
            from zeroconf import IPVersion, Zeroconf
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

            ok = self._try_register()
            # 不管这次成没成，只要 Zeroconf 对象建起来了就进入维护循环。
            # 开机时网卡往往还没拿到 DHCP 地址（Wi-Fi 重连、交换机慢），
            # 原先直接 return False 的后果是「整个进程生命周期内固定地址都不可用」，
            # 用户只能重启服务才能恢复 —— 而重启恰好也是"网络已就绪"的时刻，
            # 于是这个 bug 表现为"重启一下就好了"，极难归因。
            self._start_watchdog()
            return ok

        except Exception as e:
            self._state = 'unavailable'
            self._last_error = str(e)
            logger.warning(f"mDNS 固定地址启动失败（已忽略，不影响其它功能）：{e}")
            self._close_zc()
            return False

    @property
    def pending(self) -> bool:
        """已进入维护循环、但还没广播成功（等网络就绪，稍后自动补上）。

        lifecycle 靠它决定"要不要把这个对象留下来"：只要还有自愈的可能，
        就得留着，否则退出时也没人发道别包。
        """
        return self._zc is not None and self._state != 'running'

    def _try_register(self) -> bool:
        """真正干活的注册动作。失败不抛异常（唯一例外是名字耗尽）。"""
        from zeroconf import NonUniqueNameException

        addresses = _local_ipv4_addresses()
        if not addresses:
            with self._lock:
                self._state = 'unavailable'
                self._last_error = '未找到可用的 IPv4 地址（网络未就绪，稍后自动重试）'
            logger.warning("mDNS 固定地址暂不可用：本机还没有非回环 IPv4 地址，"
                           f"{WATCH_INTERVAL} 秒后自动重试（不影响用 IP 访问）")
            return False

        host = self._resolve_hostname(addresses)
        info = None
        # allow_name_change 只改「服务实例名」，A 记录用的 server 不会跟着改，
        # 所以冲突必须由 _resolve_hostname 提前规避（它看的是真实的 server），
        # 这里再用 allow_name_change=False 兜住"探测与注册之间"的竞态。
        for suffix in range(0, MAX_NAME_SUFFIX + 1):
            candidate_host = host if suffix == 0 else f'{self.hostname}-{suffix + 1}'
            candidate = self._build_http_info(candidate_host, addresses)
            try:
                self._zc.register_service(candidate, allow_name_change=False)
            except NonUniqueNameException:
                logger.warning(f"mDNS 名称 {candidate_host}.local 注册时被占用，换下一个后缀重试")
                continue
            info = candidate
            self._server = f'{candidate_host}.local.'
            break
        if info is None:
            raise RuntimeError('局域网内同名设备过多，未能取得可用的 mDNS 主机名')

        with self._lock:
            self._http_info = info
            self._addresses = list(addresses)
            self._state = 'running'
            self._last_error = ''
        self._log_started(info)
        return True

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
                f"{self._host_label()}.{MDNS_NTRIP_TYPE}",
                addresses=[socket.inet_aton(ip) for ip in (self._addresses or _local_ipv4_addresses())],
                port=port,
                properties={
                    'mountpoint': mountpoint or '2RTK',
                    'user': '2RTK',
                    'note': 'NTRIP Caster on this device',
                },
                server=self._server,
            )
            self._zc.register_service(info, allow_name_change=False)
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
            name = self._host_label()
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
    def _host_label(self):
        """实际广播的主机名（不含 .local），冲突退让后可能是 2rtk-2。"""
        server = (self._server or '').rstrip('.')
        return server[:-len('.local')] if server.endswith('.local') else (server or self.hostname)

    def _build_http_info(self, host, addresses):
        from zeroconf import ServiceInfo
        properties = {
            'path': '/',
            'product': '2RTK NTRIP Server',
        }
        if self.version:
            properties['version'] = self.version
        return ServiceInfo(
            MDNS_HTTP_TYPE,
            f"{host}.{MDNS_HTTP_TYPE}",
            addresses=[socket.inet_aton(ip) for ip in addresses],
            port=self.web_port,
            properties=properties,
            server=f"{host}.local.",
        )

    def _resolve_hostname(self, our_addresses):
        """挑一个局域网里没人用的主机名（默认 2rtk，被占则 2rtk-2、2rtk-3…）。

        为什么要自己探测：zeroconf 的 allow_name_change 只改「服务实例名」，
        A 记录用的 server（2rtk.local）原样不动。两台设备都叫 2rtk 时，
        双方都会应答 2rtk.local 的 A 查询，客户端随机拿到一台 —— 表现为
        "有时打得开有时打不开"，比干脆不改名更难排查（已实测复现）。

        探测方式：浏览 _http._tcp，只看实例名以 2rtk 开头的（正常 0～2 条），
        取其 server 与自己的地址比对。地址不同才是别人；地址相同说明是
        本机上次没来得及发道别包留下的缓存，不能当成冲突。
        """
        names = []
        ours = set(our_addresses)
        prefix = self.hostname.lower()

        class _Listener:
            def add_service(self_, zc_, type_, name):
                if name.split('.')[0].lower().startswith(prefix):
                    names.append(name)

            def update_service(self_, *args):
                pass

            def remove_service(self_, *args):
                pass

        browser = None
        try:
            from zeroconf import ServiceBrowser
            browser = ServiceBrowser(self._zc, MDNS_HTTP_TYPE, _Listener())
            deadline = time.monotonic() + CONFLICT_PROBE_SECONDS
            while time.monotonic() < deadline and not names:
                time.sleep(0.05)
        except Exception as e:
            logger.debug(f"mDNS 冲突探测不可用（按无冲突处理）：{e}")
            return self.hostname

        claimed = set()
        try:
            for name in list(names):
                try:
                    info = self._zc.get_service_info(MDNS_HTTP_TYPE, name, timeout=800)
                except Exception:
                    info = None
                if not info or not info.server:
                    continue
                addrs = {socket.inet_ntoa(a) for a in info.addresses}
                if addrs and addrs != ours:
                    claimed.add(info.server.rstrip('.').lower())
        finally:
            if browser is not None:
                try:
                    browser.cancel()
                except Exception:
                    pass

        host = self.hostname
        suffix = 2
        while f'{host}.local' in claimed:
            if suffix > MAX_NAME_SUFFIX:
                logger.warning(f"局域网内 {self.hostname} 名字被占满（已试到 -{MAX_NAME_SUFFIX}），"
                               "仍使用原名广播")
                return self.hostname
            host = f'{self.hostname}-{suffix}'
            suffix += 1
        if host != self.hostname:
            logger.warning(f"局域网内已存在同名设备（{self.hostname}.local 被占用），"
                           f"本机固定地址退为 http://{host}.local:{self.web_port}")
        return host

    def _close_zc(self):
        zc = self._zc
        self._zc = None
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass

    def _log_started(self, info):
        logger.info(f"固定地址已启用：http://{self._host_label()}.local:{self.web_port}"
                    f"（地址 {', '.join(self._addresses)}）")
        logger.info("提示：Windows / macOS / iPhone 可用固定地址访问；安卓系统不支持 .local，请用本机 IP")
        # 现场最高频的"打不开"不是 mDNS 坏了，而是客户端装了代理/VPN：
        # Chrome 把 2rtk.local:5757 交给代理，代理解析不了 .local 就回 502。
        # 这条日志让用户第一眼就能排除掉"程序没起来"这种误判。
        logger.info("提示：浏览器打不开固定地址但用 IP 正常时，多为代理/VPN 拦了 .local，"
                    "把 *.local 加入直连（绕过代理）即可")

    def _start_watchdog(self):
        self._watch_stop.clear()
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        self._watch_thread = threading.Thread(target=self._watch_loop, name='mdns-watch', daemon=True)
        self._watch_thread.start()

    def _watch_loop(self):
        while not self._watch_stop.wait(WATCH_INTERVAL):
            try:
                with self._lock:
                    running = self._state == 'running'
                if not running:
                    # 还没广播成功（启动时无地址 / 名字全被占）：这里补一次。
                    # 只要网卡后来拿到地址，固定地址就能自己恢复，不必重启服务。
                    if self._zc is not None:
                        try:
                            self._try_register()
                        except Exception as e:
                            logger.debug(f"mDNS 重试注册未成功（下个周期再试）：{e}")
                    continue

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
                server=self._server,
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
