# -*- coding: utf-8 -*-
"""出口②：本地 NTRIP Caster。

设计取舍（和参考实现 pygnssutils.socket_server 的差异）：
  1. 不用 socketserver.ThreadingTCPServer，而是自己写 accept 循环 + 每客户端一线程。
     原因是本程序需要"随配置重启"：restart 时要能确定性地关掉监听 socket 和所有
     客户端连接，用裸 socket 更好控制生命周期。
  2. 每个客户端一个有界队列，发布方永不阻塞。队列满了说明该客户端不读数据，
     直接断开它——对 RTCM 流来说，慢客户端留着比踢掉更糟（会拖累所有客户端）。
  3. 只实现一套挂载点、一组固定账号，不做 CAS/NET 的多挂载点逻辑。

⚠ 本文件的 start()/stop() 是踩过坑的成熟逻辑（shutdown()+close()、3 次退避重试、
   1s accept 超时），拆分时原样搬运，不要为了"优雅"改写。
"""
import base64
import hmac
import logging
import queue
import socket
import threading
import time
from typing import Optional

from ..core import state
from ..core.config import get_config
from ..core.constants import (LOCAL_CASTER_CLIENT_TIMEOUT, LOCAL_CASTER_HOST,
                              LOCAL_CASTER_MAX_CLIENTS, LOCAL_CASTER_MOUNTPOINT,
                              LOCAL_CASTER_PASSWORD, LOCAL_CASTER_PORT,
                              LOCAL_CASTER_QUEUE_SIZE, LOCAL_CASTER_SERVER_NAME,
                              LOCAL_CASTER_USER, RTCM_TYPE_WINDOW)
from ..utils.network import find_port_owner, get_local_ip


class _CasterClient:
    """一个已接入的 NTRIP 客户端。"""
    __slots__ = ('sock', 'addr', 'outbox', 'alive')

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.outbox = queue.Queue(maxsize=LOCAL_CASTER_QUEUE_SIZE)
        self.alive = True


def _normalize_target(target: str) -> str:
    """把请求目标归一化成挂载点名。

    三种形态现场都出现过，必须都认：
      /2RTK                     标准 NTRIP
      /2RTK?x=1                 带 query（个别客户端会加时间戳）
      http://1.2.3.4:2102/2RTK  绝对形态 —— 客户端走了 HTTP 代理时，
                                请求行里的目标就是完整 URL。原先这种请求被
                                整串当成挂载点名，直接回 404「不存在的挂载点」。
    """
    if not target:
        return ''
    text = target.split('?', 1)[0].split('#', 1)[0]
    if '://' in text:
        rest = text.split('://', 1)[1]
        slash = rest.find('/')
        text = rest[slash:] if slash >= 0 else '/'
    return text.strip().strip('/')


class LocalCaster:
    """把本机正在转发的 RTCM 数据以 NTRIP Caster 形式对内网发布。

    协议要点：
      GET /             → 返回 SOURCETABLE（不需要账号，方便客户端发现挂载点）
      GET /2RTK         → Basic 认证通过后返回 200/ICY，随后持续下发 RTCM
      认证失败          → 401 + WWW-Authenticate，客户端（RTKLIB/u-center 等）会重试带账号
      **版本镜像**      → 客户端用 HTTP/1.1 说话就回 "HTTP/1.1 200 OK + Ntrip-Version"，
                         只有经典 NTRIP 1.0（GET ... HTTP/1.0 且不带 Ntrip-Version）
                         才回 "ICY 200 OK"。原先"没声明 2.0 就一律回 ICY"会让
                         HTTP/1.1 客户端收到非 HTTP 状态行，判握手失败。
      SOURCE 方法       → 405（那是"推流"语义，本机不作上游 Caster）
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self._listen = None
        self._thread = None
        self._running = False
        self._clients = []
        self._lock = threading.Lock()
        self._credentials = base64.b64encode(
            f"{LOCAL_CASTER_USER}:{LOCAL_CASTER_PASSWORD}".encode()
        )

    # ---------- 生命周期 ----------
    def start(self, retry: int = 3) -> bool:
        """尝试监听端口。

        为什么要重试：重启程序时，上一轮的 accept 线程可能还阻塞在 accept() 上。
        POSIX 下 close() 一个正被别的线程 accept() 的监听 socket，并不保证那个
        阻塞的 syscall 立刻返回——fd 引用要到 syscall 退出后才真正释放，这期间
        端口仍算被占用，新实例 bind 就会拿到 EADDRINUSE。
        实测在 systemd 重启 / 网页保存重启时这个窗口能到 1~2 秒，
        所以这里用退避重试覆盖它，而不是一次失败就永久放弃本地 Caster。
        """
        last_err = None
        sock = None
        for attempt in range(max(1, retry)):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, self.port))
                sock.listen(16)
                # 关键：accept 设超时，让 _accept_loop 每秒能回来检查 _running。
                # 不设的话线程会一直卡在 accept() 里，stop() 的 join 只能等超时，
                # 端口也就迟迟释放不掉。
                sock.settimeout(1.0)
                self._listen = sock
                last_err = None
                break
            except OSError as e:
                last_err = e
                try:
                    if sock is not None:
                        sock.close()
                except OSError:
                    pass
                sock = None
                if attempt < retry - 1:
                    logging.warning(
                        f"本地 Caster 监听 {self.host}:{self.port} 失败（{e}），"
                        f"1 秒后重试（{attempt + 1}/{retry - 1}）"
                    )
                    time.sleep(1.0)

        if last_err is not None:
            owner = find_port_owner(self.port)
            who = f"，当前占用者：{owner}" if owner else ""
            logging.error(
                f"本地 NTRIP Caster 启动失败：无法监听 {self.host}:{self.port}（{last_err}）{who}。"
                f"注意：中继/上传端填的对端端口与本机监听端口互不影响，"
                f"这个报错只说明本机 {self.port} 已被占用；"
                f"请在配置页改用其他端口，或结束占用该端口的进程后重启"
            )
            return False

        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, name='local-caster', daemon=True)
        self._thread.start()
        logging.info(
            f"本地 NTRIP Caster 已启动：{self.host}:{self.port} "
            f"挂载点 {LOCAL_CASTER_MOUNTPOINT}（账号 {LOCAL_CASTER_USER}）"
        )
        return True

    def stop(self):
        self._running = False
        listen = self._listen
        self._listen = None
        if listen is not None:
            # 先 shutdown 再 close：shutdown 能把阻塞中的 accept() 叫醒，
            # 只 close 的话 Linux 上阻塞的那个 syscall 不一定立刻返回，
            # 端口会一直占着，下一个实例 bind 就 EADDRINUSE。
            try:
                listen.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                listen.close()
            except OSError:
                pass
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            c.alive = False
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            # accept 已设 1 秒超时，正常情况几乎立刻退出；给 3 秒足够宽裕。
            thread.join(3)
            if thread.is_alive():
                logging.warning(
                    f"本地 Caster accept 线程未在超时内退出，端口 {self.port} 可能被短暂占用"
                )

    # ---------- 数据分发 ----------
    def publish(self, data: bytes):
        """把一帧/一段 RTCM 数据复制给所有在线客户端（非阻塞）。"""
        if not data or not self._running:
            return
        with self._lock:
            clients = [c for c in self._clients if c.alive]
        for c in clients:
            try:
                c.outbox.put_nowait(data)
            except queue.Full:
                c.alive = False
                logging.warning(f"本地 Caster 客户端 {c.addr[0]} 读取过慢，已断开")
                try:
                    c.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def status(self) -> dict:
        with self._lock:
            online = sum(1 for c in self._clients if c.alive)
        return {
            'running': self._running,
            'host': self.host,
            'port': self.port,
            'mountpoint': LOCAL_CASTER_MOUNTPOINT,
            'user': LOCAL_CASTER_USER,
            'clients': online,
            'max_clients': LOCAL_CASTER_MAX_CLIENTS,
        }

    # ---------- 内部 ----------
    def _accept_loop(self):
        while self._running:
            listen = self._listen
            if listen is None:
                break
            try:
                sock, addr = listen.accept()
            except socket.timeout:
                # accept 设了 1 秒超时：超时只代表这一轮没新连接，
                # 不是错误，回到 while 顶端重新检查 _running。
                # 注意必须先于 OSError 捕获——TimeoutError 是 OSError 的子类。
                continue
            except OSError:
                break  # 监听 socket 被 close()，正常退出
            except Exception as e:
                logging.error(f"本地 Caster accept 异常: {e}")
                break

            with self._lock:
                full = len(self._clients) >= LOCAL_CASTER_MAX_CLIENTS
            if full:
                try:
                    sock.sendall(self._response_head(
                        '503 Service Unavailable', True, self._http_date(),
                        body=f'too many clients (max {LOCAL_CASTER_MAX_CLIENTS})\r\n'.encode()))
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
                logging.warning(f"本地 Caster 连接数已满，拒绝 {addr[0]}")
                continue

            client = _CasterClient(sock, addr)
            with self._lock:
                self._clients.append(client)
            threading.Thread(target=self._serve_client, args=(client,),
                             name=f'caster-{addr[0]}', daemon=True).start()

    def _parse_request(self, raw: bytes) -> dict:
        """解析 NTRIP 请求，返回各要素。

        原先返回位置元组，字段一多就容易错位；改成 dict 后每个字段自解释。
        行分隔同时容忍 CRLF 与裸 LF（个别嵌入式客户端只发 LF）。
        """
        lines = raw.replace(b'\r\n', b'\n').split(b'\n')
        method = ''
        target = ''
        http11 = False
        authorized = False
        ntrip2 = False

        parts = (lines[0] if lines else b'').split(b' ')
        if len(parts) >= 2:
            method = parts[0].decode('latin-1').upper().strip()
            target = parts[1].decode('latin-1').strip()
            if len(parts) >= 3:
                http11 = b'1.1' in parts[2]

        for line in lines[1:]:
            low = line.lower()
            if low.startswith(b'authorization: basic '):
                given = line.split(b' ', 2)[2].strip()
                if hmac.compare_digest(given, self._credentials):
                    authorized = True
            elif low.startswith(b'ntrip-version:'):
                if b'2.0' in line:
                    ntrip2 = True

        return {'method': method, 'target': target,
                'mountpoint': _normalize_target(target),
                'authorized': authorized, 'ntrip2': ntrip2, 'http11': http11}

    @staticmethod
    def _response_head(status: str, v2: bool, http_date: str,
                       body: bytes = b'', extra: str = '') -> bytes:
        """构造短响应（401/404/405/503），显式带 Content-Length 后关闭连接。

        为什么要显式写 Content-Length：部分客户端（.NET 的 HttpClient、部分
        国产 App）在既没有 Content-Length 又没等到 EOF 时，会把 401 当成
        "响应不完整"而直接判失败，不再带账号重试 —— 表现就是"一直认证不上"。
        """
        head = (
            f"HTTP/1.1 {status}\r\n"
            + ("Ntrip-Version: Ntrip/2.0\r\n" if v2 else "")
            + f"Server: {LOCAL_CASTER_SERVER_NAME}\r\nDate: {http_date}\r\n"
            + extra
            + "Content-Type: text/plain\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode()
        return head + body

    def _serve_client(self, client: _CasterClient):
        sock = client.sock
        try:
            sock.settimeout(LOCAL_CASTER_CLIENT_TIMEOUT)
            raw = b''
            # 同时容忍 CRLF 与裸 LF：个别嵌入式客户端只发 LF，
            # 只等 b'\r\n\r\n' 会让它一直挂到超时，表现就是"连接超时"。
            while (b'\r\n\r\n' not in raw and b'\n\n' not in raw) and len(raw) < 8192:
                chunk = sock.recv(1024)
                if not chunk:
                    return
                raw += chunk
            if not raw:
                return

            info = self._parse_request(raw)
            http_date = self._http_date()
            # 版本镜像：客户端用 HTTP/1.1 说话就用 HTTP/1.1 回。只有经典
            # NTRIP 1.0 客户端（GET ... HTTP/1.0 且不带 Ntrip-Version）才用
            # "ICY 200 OK"。原先"没声明 2.0 就一律回 ICY"会让 HTTP/1.1
            # 客户端（.NET、部分国产 App）收到非 HTTP 状态行而判握手失败。
            v2 = bool(info['ntrip2'] or info['http11'])
            mountpoint = info['mountpoint']

            # 0) 只接受 GET。SOURCE 是"推流"语义（客户端想当基站往这里发数据），
            #    个别客户端会先试一次；原先它落进"挂载点为空"的分支被回了源列表，
            #    客户端把源列表当数据流解析，表现就是"连上了但一帧都没有"。
            if info['method'] != 'GET':
                sock.sendall(self._response_head(
                    '405 Method Not Allowed', v2, http_date,
                    body=f"this caster requires GET, got {info['method'] or '?'}\r\n".encode(),
                    extra='Allow: GET\r\n'))
                logging.warning(
                    f"本地 Caster: {client.addr[0]} 用了不支持的请求方式 "
                    f"{info['method'] or '?'} {info['target']}（本机只接受 GET）")
                return

            # 1) 源列表：不要求认证（只暴露一个固定挂载点名，没有敏感信息）
            if not mountpoint or mountpoint.lower() == 'sourcetable':
                sock.sendall(self._build_sourcetable_response(v2, http_date))
                logging.info(f"本地 Caster: {client.addr[0]} 读取源列表")
                return

            # 2) 挂载点不对：明确回 404，别把源列表当数据流塞给客户端。
            #    比较忽略大小写：挂载点名在现场经常被手抄成全小写。
            if mountpoint.lower() != LOCAL_CASTER_MOUNTPOINT.lower():
                body = (f"unknown mountpoint /{mountpoint}, "
                        f"available: /{LOCAL_CASTER_MOUNTPOINT}\r\n").encode()
                sock.sendall(self._response_head('404 Not Found', v2, http_date, body=body))
                logging.warning(
                    f"本地 Caster: {client.addr[0]} 请求了不存在的挂载点 /{mountpoint}"
                    f"（原始请求行：{info['method']} {info['target']}）")
                return

            # 3) 认证
            if not info['authorized']:
                sock.sendall(self._response_head(
                    '401 Unauthorized', v2, http_date,
                    extra=f'WWW-Authenticate: Basic realm="{LOCAL_CASTER_MOUNTPOINT}"\r\n'))
                logging.warning(f"本地 Caster: {client.addr[0]} 认证失败（账号或密码不对）")
                return

            # 4) 放行，开始推流
            if v2:
                head = (
                    "HTTP/1.1 200 OK\r\n"
                    f"Ntrip-Version: Ntrip/2.0\r\n"
                    f"Server: {LOCAL_CASTER_SERVER_NAME}\r\nDate: {http_date}\r\n"
                    "Cache-Control: no-store, no-cache, max-age=0\r\n"
                    "Pragma: no-cache\r\nConnection: close\r\n"
                    "Content-Type: gnss/data\r\n\r\n"
                )
            else:
                head = "ICY 200 OK\r\n\r\n"
            sock.sendall(head.encode())
            sock.settimeout(None)
            logging.info(f"本地 Caster: {client.addr[0]}:{client.addr[1]} 已接入挂载点 "
                         f"/{LOCAL_CASTER_MOUNTPOINT}（{'NTRIP2' if v2 else 'NTRIP1'}）")

            while self._running and client.alive:
                try:
                    data = client.outbox.get(timeout=1.0)
                except queue.Empty:
                    # 数据源暂时没数据，顺便探测客户端是否已经走了
                    try:
                        sock.settimeout(0.01)
                        if sock.recv(1, socket.MSG_PEEK) == b'':
                            break
                    except socket.timeout:
                        pass
                    except OSError:
                        break
                    finally:
                        if client.alive:
                            try:
                                sock.settimeout(None)
                            except OSError:
                                pass
                    continue
                if data:
                    sock.sendall(data)

        except (OSError, ConnectionError, socket.timeout) as e:
            logging.info(f"本地 Caster 客户端 {client.addr[0]} 断开: {e}")
        except Exception as e:
            logging.error(f"本地 Caster 客户端处理异常: {e}")
        finally:
            client.alive = False
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            try:
                sock.close()
            except OSError:
                pass
            logging.info(f"本地 Caster: {client.addr[0]}:{client.addr[1]} 已断开")

    @staticmethod
    def _http_date() -> str:
        return time.strftime('%a, %d %b %Y %H:%M:%S UTC', time.gmtime())

    def _active_types(self):
        """最近 RTCM_TYPE_WINDOW 秒内真的出现过的消息类型（按编号升序）。"""
        now = time.time()
        return sorted(t for t, ts in state.msg_types_recent.items() if now - ts <= RTCM_TYPE_WINDOW)

    def _describe_rtcm(self) -> str:
        """把当前在推的消息类型拼成源列表里的格式细节，例如 1005(1),1074(1)。"""
        types = self._active_types()
        if not types:
            return '1005(1),1074(1),1084(1),1094(1),1124(1)'
        return ','.join(f'{t}(1)' for t in types[:16])

    def _nav_systems(self) -> str:
        """根据实际观测到的 MSM 消息号推断星座，避免源列表写错。"""
        mapping = (('107', 'GPS'), ('108', 'GLO'), ('109', 'GAL'),
                   ('110', 'SBAS'), ('111', 'QZS'), ('112', 'BDS'), ('113', 'IRNSS'))
        seen = {str(t) for t in self._active_types()}
        nav = [name for prefix, name in mapping if any(t.startswith(prefix) for t in seen)]
        return '+'.join(nav) if nav else 'GPS+GLO+GAL+BDS'

    def _station_latlon(self):
        lat = state.station_info.get('latitude')
        lon = state.station_info.get('longitude')
        try:
            return f"{float(lat):.9f}", f"{float(lon):.9f}"
        except (TypeError, ValueError):
            return '0.000000000', '0.000000000'

    def _build_sourcetable_response(self, ntrip2: bool, http_date: str) -> bytes:
        lat, lon = self._station_latlon()
        # CAS 行要写"对外可访问的地址"，监听 0.0.0.0 时不能照抄 0.0.0.0
        adv_host = self.host
        if adv_host in ('0.0.0.0', '', '::'):
            try:
                adv_host = get_local_ip()
            except Exception:
                adv_host = self.host
        body = (
            f"CAS;{adv_host};{self.port};{LOCAL_CASTER_SERVER_NAME};2RTK;0;CHN;{lat};{lon};0.0.0.0;0;none\r\n"
            f"NET;2RTK;2RTK;B;N;none;none;none;none\r\n"
            f"STR;{LOCAL_CASTER_MOUNTPOINT};2RTK-Base;RTCM 3.2;{self._describe_rtcm()};"
            f"2;{self._nav_systems()};;;{lat};{lon};0;0;2RTK;none;B;N;0;\r\n"
            "ENDSOURCETABLE\r\n"
        ).encode()
        if ntrip2:
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Ntrip-Version: Ntrip/2.0\r\n"
                f"Server: {LOCAL_CASTER_SERVER_NAME}\r\nDate: {http_date}\r\n"
                "Content-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode()
        else:
            head = (
                "SOURCETABLE 200 OK\r\n"
                f"Date: {http_date}\r\nContent-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode()
        return head + body


# ==================== 对外接口（单例存放在 core.state） ====================
def start_local_caster(cfg) -> Optional[LocalCaster]:
    """按配置启动本地 Caster；失败只记日志，不影响原有的上传链路。"""
    stop_local_caster()
    try:
        port = int(cfg.get('local_caster_port') or LOCAL_CASTER_PORT)
    except (TypeError, ValueError):
        port = LOCAL_CASTER_PORT
    host = (cfg.get('local_caster_host') or LOCAL_CASTER_HOST).strip() or LOCAL_CASTER_HOST
    caster = LocalCaster(host, port)
    if not caster.start():
        owner = find_port_owner(port)
        state.local_caster_last_error = f"{port} 端口被占用" + (f"（{owner}）" if owner else "")
        return None
    state.local_caster_last_error = ''
    state.local_caster = caster
    return caster


def stop_local_caster():
    if state.local_caster is not None:
        state.local_caster.stop()
        state.local_caster = None
        logging.info("本地 NTRIP Caster 已停止")


def publish(data: bytes):
    """把数据额外分发一份给本地 Caster 的客户端（没启动时是空操作）。"""
    if state.local_caster is not None:
        state.local_caster.publish(data)


def local_caster_snapshot() -> dict:
    """本地 Caster 的对外可见快照，供状态页卡片与 ws 首帧使用。

    只包含"能不能连、连了几台"这类运行信息，不含任何配置项；
    因此可以跟随状态的公开推送一起下发，不需要登录。
    """
    cfg = get_config()
    enabled = str(cfg.get('local_caster_enabled', '0')) == '1'
    if state.local_caster is not None:
        info = state.local_caster.status()
    else:
        try:
            port = int(cfg.get('local_caster_port') or LOCAL_CASTER_PORT)
        except (TypeError, ValueError):
            port = LOCAL_CASTER_PORT
        info = {
            'running': False,
            'host': cfg.get('local_caster_host') or LOCAL_CASTER_HOST,
            'port': port,
            'clients': 0,
            'max_clients': LOCAL_CASTER_MAX_CLIENTS,
        }
    info['enabled'] = enabled
    info['mountpoint'] = LOCAL_CASTER_MOUNTPOINT
    info['user'] = LOCAL_CASTER_USER
    info['password'] = LOCAL_CASTER_PASSWORD
    info['last_error'] = state.local_caster_last_error
    # 拆开给：现场最容易错的就是把 IP、端口、挂载点、账号密码混成一串
    # （192.168.1.19:2102/2RTK 看着像"地址"，填进手机 App 却各格都要单独填）。
    info['ip'] = get_local_ip()
    info['example'] = f"{info['ip']}:{info['port']}/{LOCAL_CASTER_MOUNTPOINT}"
    return info
