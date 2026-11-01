# -*- coding: utf-8 -*-
"""NTRIP 协议客户端：TCP 建连、SOURCE 握手、中继客户端握手、源列表拉取。

被 caster_uploader（上传出口）、relay_source（中继数据源）、web.api（源列表接口）共用。
本模块不 import 上述调用方，依赖方向单向。
"""
import base64
import logging
import socket

from ..core import state
from ..core.constants import (CASTER_WRITE_TIMEOUT, NTRIP_HANDSHAKE_TIMEOUT,
                              SOURCETABLE_MAX_BYTES, SOURCETABLE_TIMEOUT)
from ..utils.common import (ThreadExitRequested, interruptible_sleep, retry)


@retry(max_retries=3, delay=5)
def connect_to_host(host, port, timeout=10):
    logging.info(f"正在向{host}:{port}发送连接")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        # 连接建立后必须清除 connect 用的超时，否则这个 10 秒超时会一直挂在 socket 上：
        # 中继端 recv 只要 10 秒没数据就抛 socket.timeout，被上层当成致命错误，
        # 导致 relay 和 caster 两条连接被一起断掉重连。
        s.settimeout(None)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # 让内核尽早探测对端消失（Linux 专有选项，失败不影响功能）
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
        except (AttributeError, OSError):
            pass
        logging.info(f"成功连接到{host}:{port}")
        return s
    except Exception as e:
        logging.error(f"连接到{host}:{port} 失败: {e}，请检查网络连接和配置")
        try:
            s.close()
        except Exception:
            pass
        raise


def build_client_request(cfg):
    auth = ''
    if cfg.get('relay_user') and cfg.get('relay_password'):
        cred = f"{cfg['relay_user']}:{cfg['relay_password']}"
        auth = base64.b64encode(cred.encode()).decode()
    req = f"GET /{cfg['relay_mountpoint']} HTTP/1.1\r\n"
    req += f"Host: {cfg['relay_host']}\r\n"
    req += "Ntrip-Version: Ntrip/2.0\r\n"
    req += "User-Agent: NTRIP 2RTKserver\r\n"
    if auth:
        req += f"Authorization: Basic {auth}\r\n"
    req += "Accept: */*\r\nConnection: close\r\n\r\n"
    return req


@retry(max_retries=3, delay=5)
def handshake_ntrip_client(srv_sock, cfg):
    hdr = build_client_request(cfg)
    logging.info("发送realy 连接请求")
    # 握手阶段必须有超时，否则服务端不说话就会永久阻塞
    srv_sock.settimeout(NTRIP_HANDSHAKE_TIMEOUT)
    try:
        srv_sock.sendall(hdr.encode())
        resp = srv_sock.recv(2048).decode(errors='ignore')
    finally:
        srv_sock.settimeout(None)
    logging.info(f"收到realy caster服务器响应: {resp.splitlines()[:5]}")
    if 'ICY 200 OK' in resp or '200 OK' in resp:
        logging.info('Relay NTRIP server authentication passed, receiving RTCM data')
        return True
    logging.error(f"Relay NTRIP server response: {resp.splitlines()[:5]}")
    return False


@retry(max_retries=2, delay=3)
class _NullCaster:
    """没有配置上传 Caster 时的占位对象。

    场景：只想让本机作为本地 Caster 对内网发布差分，不需要往任何外部 Caster 推流。
    这样 upload_via_serial / forward_relay 里那些 caster.sendall() 调用不用处处判空，
    也不会因为"发送失败"反复触发重连逻辑。
    """
    def sendall(self, data):
        return None

    def close(self):
        return None

    def settimeout(self, timeout):
        return None


# ==================== 以下为已废弃（deprecated）的上传连接实现 ====================
# 上传出口现已由 CasterUploader（独立线程 + 队列）接管，运行路径不再调用
# open_caster / _NullCaster / reconnect_caster。保留仅为参考与平滑过渡，
# 后续版本可安全删除。判定"是否启用上传"一律看 caster_enabled 独立开关。
def open_caster(cfg):
    # 延迟导入：upload 状态函数住在 outlets.caster_uploader，
    # 而它在模块顶层 import 本模块的 connect_to_host。函数内导入打破这个环。
    from ..outlets import caster_uploader as up

    # 判定只看独立开关，不看"地址是否为空"：
    # 地址是用户填过的历史配置，不代表他这次想启用上传。未勾选就绝不去连。
    if str(cfg.get('caster_enabled', '0')) != '1':
        logging.info("未勾选「上传到云端 Caster」，数据只走本地出口")
        up.mark_upload_state('idle', cfg, error='')
        return _NullCaster()
    logging.info(f">>>向上传端NTRIP Caster 申请连接{cfg['caster_host']}:{cfg['caster_port']}")
    up.mark_upload_state('connecting', cfg)
    try:
        sock = connect_to_host(cfg['caster_host'], int(cfg['caster_port']))
    except Exception as e:
        up.mark_upload_state('retrying', cfg, error=str(e))
        raise
    hdr = (
        f"SOURCE {cfg['caster_password']} {cfg['caster_mountpoint']}\r\n"
        "Source-Agent: NTRIP 2RTKserver\r\n"
        "STR: \r\n\r\n"
    )
    # 握手阶段用短超时，成功后换成写超时，用于尽早发现死连接
    sock.settimeout(NTRIP_HANDSHAKE_TIMEOUT)
    try:
        sock.sendall(hdr.encode())
        resp = sock.recv(1024).decode(errors='ignore')
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise
    logging.info(">>> 接收到上传端NTRIP Caster 响应 ：")
    logging.info(resp)

    if 'ICY 200 OK' not in resp and '200 OK' not in resp:
        logging.error(f"NTRIP Caster response: {resp.splitlines()[:3]}")
        try:
            sock.close()
        except Exception:
            pass
        up.mark_upload_state('error', cfg, error=f"认证失败: {resp.splitlines()[:1]}")
        raise RuntimeError('上传端 NTRIP Caster 认证失败，请检查密码及配置信息')
    sock.settimeout(CASTER_WRITE_TIMEOUT)
    logging.info('上传端NTRIP Caster 认证通过开始发送RTCM数据')
    up._reset_upload_stats()
    up.mark_upload_state('authed', cfg, error='')
    return sock


def reconnect_caster(cfg):
    """彻底重新连接Caster，而不仅仅是尝试连接

    原先这里是 5 次循环 × open_caster 的 @retry(3) × connect_to_host 的 @retry(3)，
    最多 45 次连接尝试、每次失败还要 sleep(15)，一次重连可能阻塞十分钟以上，
    期间"重启"指令完全没反应。改成单层指数退避，并且随时可被退出信号打断。
    """
    backoff = [5, 10, 20, 30, 30]
    last_error = None
    for attempt, delay in enumerate(backoff, start=1):
        if state.thread_exit_event.is_set():
            raise ThreadExitRequested()
        try:
            # 完全重新建立连接，包括认证过程
            return open_caster(cfg)
        except ThreadExitRequested:
            raise
        except Exception as e:
            last_error = e
            logging.error(f"Caster重连尝试 {attempt}/{len(backoff)} 失败: {e}")
            if attempt < len(backoff):
                if interruptible_sleep(delay):
                    raise ThreadExitRequested()

    # 如果达到最大重试次数仍失败，抛出异常
    raise RuntimeError(f"达到最大重试次数({len(backoff)})，Caster重连失败: {last_error}")


# ==================== 中继挂载点列表（NTRIP SOURCETABLE） ====================
# 为什么这件事必须由后端做、前端 JS 做不了：
#   浏览器只允许 HTTP(S) / WebSocket，不能对任意主机开一条裸 TCP 连接。
#   NTRIP Caster 的 2101 端口是"纯 TCP + 自定义文本协议"：
#       客户端发 "GET / HTTP/1.0" → 服务端回 "SOURCETABLE 200 OK" + 若干 STR; 行
#   它不是 HTTP 服务（没有正常的状态行/分段语义），fetch() / XHR 连不上去，
#   WebSocket 也不行（WS 握手本身要走 HTTP Upgrade，Caster 不会应答）。
#   所以只能让设备上的 Python 代连一次，把解析好的列表以 JSON 交给页面。
#
# 安全说明：这个接口相当于"让设备替浏览器访问任意 host:port"（SSRF 面）。
#   本设备是内网部署，且中继地址本来就可能是内网 Caster（例如 192.168.x.x），
#   因此不做网段限制，只做三件事：必须已登录、单次强制超时、响应大小上限。
def fetch_sourcetable(host: str, port: int, user: str = '', password: str = '') -> str:
    """向 NTRIP Caster 请求源列表原始文本。连接/读取失败会抛异常。"""
    req = "GET / HTTP/1.0\r\n"
    req += f"Host: {host}\r\n"
    req += "Ntrip-Version: Ntrip/2.0\r\n"
    req += "User-Agent: NTRIP 2RTKserver/sourcetable\r\n"
    req += "Accept: */*\r\n"
    if user and password:
        cred = base64.b64encode(f"{user}:{password}".encode()).decode()
        req += f"Authorization: Basic {cred}\r\n"
    req += "\r\n"

    sock = socket.create_connection((host, port), timeout=SOURCETABLE_TIMEOUT)
    try:
        sock.settimeout(SOURCETABLE_TIMEOUT)
        sock.sendall(req.encode())
        chunks = []
        total = 0
        while total < SOURCETABLE_MAX_BYTES:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            # 结尾标记可能被切在两个包中间，只看尾部一小段即可
            if b'ENDSOURCETABLE' in b''.join(chunks[-2:]):
                break
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return b''.join(chunks).decode('utf-8', errors='replace')


def parse_sourcetable(text: str):
    """把 SOURCETABLE 文本解析成挂载点列表。

    标准一行（STR）字段顺序：
      STR;挂载点;标识;格式;格式细节;载波;导航系统;网络;国家;纬度;经度;需要NMEA;
      解状态;生成器;压缩加密;认证;费用;比特率;其他
    """
    lines = text.splitlines()
    header = lines[0].strip() if lines else ''
    items = []
    seen = set()
    for line in lines:
        if not line.startswith('STR;'):
            continue
        f = line.split(';')
        if len(f) < 12:
            continue
        mountpoint = f[1].strip()
        if not mountpoint or mountpoint in seen:
            continue
        seen.add(mountpoint)
        items.append({
            'mountpoint': mountpoint,
            'id': f[2].strip(),
            'format': f[3].strip(),
            'nav': f[6].strip() if len(f) > 6 else '',
            'country': f[8].strip() if len(f) > 8 else '',
            'auth': f[15].strip() if len(f) > 15 else '',
        })
    items.sort(key=lambda x: x['mountpoint'].lower())
    return header, items
