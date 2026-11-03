# -*- coding: utf-8 -*-
"""CORS 链路（方案 §6）：NTRIP 客户端 + GGA 管理器 + RTCM 注入。

用在哪：标定链路里的「CORS 差分标定」分支（P1）。模块切到流动站后，
这里负责：
    1. 用裸 socket 连 NTRIP Caster（复用 `sources/ntrip.connect_to_host`），
       但**不复用** `handshake_ntrip_client`（它读的是 `cfg['relay_*']`，语义不同）；
    2. 完整分类连接错误（DNS / 网络 / 401 / 404 / SOURCETABLE / 200 无数据）；
    3. 把模块原生 GGA 透传出去（质量位/时间戳/高程一个字都不改），无原生 GGA 时
       才按 §6.2 软件生成；上报前过一道**防抖**（不是冻结，见下）；
    4. 把 Caster 下发的 RTCM 注入模块串口（**绝不进全局 distribute**，§6.4）。

标定阶段为什么**不冻结**上报位置（2026-11 修正）：
    VRS 是"拿你上行的 GGA 当概略位置，在那个点附近虚拟一台基准站"。标定的目标
    就是让模块靠 VRS 差分收敛出自己天线的真实坐标 —— 此时坐标本来就未知，没有
    任何可靠的固定值可以填。曾经这里默认把上报位置钉在第一条 GGA 上（冻结），
    两条后果叠加起来正好把固定解拖死：
        · 模块的真实 GGA 还在飘（单点/浮点），而 VRS 拿到的是一个和真实位置
          不符的虚拟站位置 → 电离层/对流层/距离模型残差变大，模糊度更难固定；
        · 冻结行里的时间戳是实时的、位置却是旧的，服务端算出的"流动站在动"与
          "位置没变"互相矛盾，部分 Caster 会直接拒绝或只给 DGPS。
    现在默认原样透传，只做**防抖**：单条大跳变（默认 >500 m）丢弃一次、复用
    上一条正常 GGA 兜底，连续多次跳变就停发并告警。缓慢漂移（正常单点漂移）
    完全跟随模块 —— 这才是 VRS 期望的输入。

标定完成后的业务转发（基准站播 RTCM）是另一回事：坐标已经确定，上行 GGA 应当
固定为基站真实坐标。那是分发链路（relay）的事，不在本模块的职责里。

为什么单线程超时轮询（§6.3）而不是两条线程：
    两条线程共享 socket 时，sendall 与 recv 的错误处理（谁关 fd、谁重连）会变成竞态。
    单线程 0.2 秒轮询的代价是 GGA 最多晚 200ms 发出，对 1Hz 的 VRS 完全无感。

自测（不需要硬件，用一个会回 RTCM / SOURCETABLE / 401 的假 Caster）：
    python -m rtkserver.calibration.cors
"""
import base64
import logging
import math
import socket
import time

from ..core.constants import NTRIP_HANDSHAKE_TIMEOUT
from ..sources.ntrip import connect_to_host
from ..sources.rtcm_parser import extract_rtcm_frames
from .proto import nmea

# ==================== 错误分类（方案 §6.1） ====================
# 每种错误对应一个稳定 token，流程层据此决定"要不要重试、给用户什么文案"。
# DNS / 401 / 404 / SOURCETABLE 都是**确定性错误**，重试无意义；
# 网络不通 / 超时 / 200 无数据才走指数退避重连。
ERR_DNS = 'dns'
ERR_NETWORK = 'network'
ERR_AUTH = 'auth'              # 401
ERR_MOUNTPOINT = 'mountpoint'  # 404
ERR_SOURCETABLE = 'sourcetable'
ERR_NO_DATA = 'no_data'        # 200 后无数据（VRS 先发 GGA 再等）
ERR_WRITE = 'write'            # 写模块串口失败（半死连接）

# 判定「200 后无数据」的观察窗口（秒）。VRS 在你上报有效 GGA 前不会下发 RTCM，
# 所以这个窗口内没数据**不能**判失败，而是先发 GGA 再等（§6.1）。
NO_DATA_GRACE = 5.0

# 断连重连的指数退避（秒）。单次断连 3 次退避，累计断连 > 5 分钟判失败（§6.5）。
RECONNECT_BACKOFF = (1.0, 2.0, 4.0)
CUMULATIVE_DISCONNECT_LIMIT = 300.0   # 5 分钟

# 全双工轮询的 socket 超时（秒）。§6.3：0.2 秒轮询，GGA 最多晚 200ms。
POLL_TIMEOUT = 0.2

# ---------- GGA 上报节奏（对齐行业标准做法） ----------
# 行业实测口径（2026-11 复核）：
#   · 各家 CORS 对 GGA 的**下限**要求是 1 Hz 最常见，最宽松的 SmartNet 是 30 秒无 GGA 才踢；
#     NovAtel 建议 5 秒，多数资料给出的典型值是 1 Hz。
#   · 各家对**上限**也有限制：2 Hz 以上对 VRS 服务器是无谓压力，多数客户端就按模块
#     自身的 1 Hz 输出逐条转发（u-center / AgOpenGPS / STRSVR 都是这个行为）。
#   · VRS 类挂载点在收到第一条有效 GGA 之前**一帧 RTCM 都不发**；NEAR 类挂载点则每次
#     收到新 GGA 都会重新算距离、必要时切到更近的基准源。
# 因此本实现的策略是「**逐条转发 + 保底重发**」：
#   1. 模块每输出一条新的 GGA，就立刻原样转发一条（不攒、不抽样、不改写）；
#   2. 最小间隔 GGA_MIN_INTERVAL 兜底，避免模块被配成 10 Hz 时把 Caster 打爆；
#   3. 若模块超过 GGA_KEEPALIVE 秒没有新 GGA（掉星/串口卡住），把最后一条按 1 Hz 重发，
#      防止 Caster 因「长时间无 GGA」判定客户端离线而断流。
GGA_MIN_INTERVAL = 0.2      # 两条 GGA 之间的最小间隔（秒）→ 上限 5 Hz
GGA_KEEPALIVE = 3.0         # 无新 GGA 时的保底重发间隔（秒）

# ---------- GGA 防抖（标定阶段唯一允许的"改写"，且只在异常时才改写） ----------
# 目标是：**位置完全跟随模块真实输出**，但单条离谱的 GGA 不要污染 VRS。
GGA_GUARD_ENABLED = True
# 允许位置抖动的门限（米）。VRS 只要求概略位置在几十公里级正确，但**不能
# 几分钟内来回跳几百米到几公里** —— 那会让服务端反复更换虚拟基准站，上一轮
# 固好的整周模糊度全部作废。500 m 比任何正常单点漂移（几十米量级）都宽得多，
# 又远小于"跳到另一处站点坐标"这种错误（公里级）。
GGA_JUMP_LIMIT = 500.0
# 连续丢弃几条后停发：不无限循环补发旧 GGA（那样 VRS 会被钉在一个早已不对的
# 位置，比断流更糟）。停发期间 Caster 若因无 GGA 断流，重连逻辑会接手。
GGA_JUMP_MAX_DROP = 3
# 长时间没有新的合法 GGA 时，旧行最多还能复用多久（秒）。超过就停发，
# 避免"模块早就没定位了，我们还在替它报一个看似正常的位置"。
GGA_STALE_LIMIT = 10.0

# `[GGA_JUMP_DROP]` 的打印节奏（秒）：连续跳变时每 0.2 s 一条会刷屏。
GGA_GUARD_LOG_INTERVAL = 5.0

# 日志前缀：这几条是现场排查时最先 grep 的东西，必须稳定不变。
LOG_GGA_JUMP = '[GGA_JUMP_DROP]'
LOG_GGA_STALE = '[GGA_STALE_STOP]'
LOG_GGA_GENERATED = '[GGA-GENERATED]'
# 「防抖已建立锚点」的**健康心跳**前缀。必须和 LOG_GGA_JUMP 分开：
# 2026-11-03 第一次标定时心跳沿用了 `[GGA_JUMP_DROP]`，180 秒刷了 35 条，
# 看日志的人第一反应是"一直在丢 GGA"，而实际 jump_dropped=0、一条都没丢。
# 排查时 grep 错前缀会直接把人带进沟里。
LOG_GGA_GUARD = '[GGA_GUARD_ACTIVE]'

# 注入模块串口的写超时（秒）。写阻塞说明模块不收了（流控/波特率错），不能无限等（§6.4）。
INJECT_WRITE_TIMEOUT = 0.5


class CorsError(Exception):
    """CORS 连接/运行错误。`kind` 是 ERR_* 之一，`message` 是给人看的中文。"""

    def __init__(self, kind, message, detail=None):
        super().__init__(message)
        self.kind = str(kind or '')
        self.message = str(message or '')
        self.detail = detail or {}


def build_ntrip_request(host, port, mountpoint, user='', password=''):
    """组一条 NTRIP 取流请求（`GET /<mountpoint>`）。

    刻意不复用 `sources/ntrip.build_client_request`：那个读 `cfg['relay_*']`，
    语义是「中继客户端」，字段来源完全不同。这里从显式参数组装，保证标定
    的 CORS 账号/挂载点与主程序的中继配置零耦合。
    """
    req = f'GET /{mountpoint} HTTP/1.1\r\n'
    req += f'Host: {host}:{port}\r\n'
    req += 'Ntrip-Version: Ntrip/2.0\r\n'
    req += 'User-Agent: NTRIP 2RTKserver/calibration\r\n'
    if user and password:
        cred = base64.b64encode(f'{user}:{password}'.encode()).decode()
        req += f'Authorization: Basic {cred}\r\n'
    req += 'Accept: */*\r\nConnection: close\r\n\r\n'
    return req


def read_mountpoints(host, port, user='', password=''):
    """读取 Caster 的挂载点列表（§6.1「读取挂载点」）。

    **复用** `sources/ntrip.py` 的 `fetch_sourcetable` + `parse_sourcetable`：
    那套实现已经在主程序里跑过（含大小上限、ENDSOURCETABLE 判尾、去重排序），
    标定再写一份只会多一份没人测的代码。

    返回 `(header, items)`，`items` 每项 `{mountpoint, id, format, nav, country, auth}`。
    失败抛 `CorsError`：DNS 单独分类（地址写错），其余归网络类。
    """
    # 先单独做 DNS 解析：把「域名/地址写错」和「网络不通」分开（§6.1 第一条）
    try:
        socket.getaddrinfo(host, int(port or 0))
    except socket.gaierror as e:
        raise CorsError(ERR_DNS, f'域名解析失败，请检查服务器地址 {host}',
                        {'error': str(e)})

    from ..sources.ntrip import fetch_sourcetable, parse_sourcetable
    try:
        text = fetch_sourcetable(host, int(port), user or '', password or '')
    except socket.timeout as e:
        raise CorsError(ERR_NETWORK,
                        f'读取挂载点超时（{host}:{port}），请检查网络与端口',
                        {'error': str(e)})
    except OSError as e:
        raise CorsError(ERR_NETWORK,
                        f'无法读取挂载点（{host}:{port}），请检查网络与端口',
                        {'error': str(e)})
    except Exception as e:                       # noqa: BLE001 - 协议层异常种类杂
        raise CorsError(ERR_NETWORK, f'读取挂载点失败：{e}', {'error': str(e)})

    header, items = parse_sourcetable(text or '')
    if not items:
        # 有些 Caster 在未认证时回一张空表；这跟「地址对了但没挂载点」要区分开
        upper = (text or '')[:200].upper()
        if '401' in upper or 'UNAUTHORIZED' in upper:
            raise CorsError(ERR_AUTH, '用户名或密码错误（读取挂载点被拒绝）',
                            {'raw': (text or '')[:120]})
        raise CorsError(ERR_MOUNTPOINT,
                        '该地址没有返回任何挂载点，请确认端口是否正确',
                        {'raw': (text or '')[:120]})
    return header, items


def classify_response(resp_text):
    """把 NTRIP 握手响应归类。返回 (状态, 人话, kind)。

    只认状态行第一个 token，不整段匹配（不同 Caster 的 401/404 文案不同）。
    """
    line = (resp_text or '').splitlines()[0] if resp_text else ''
    upper = line.upper()
    # SOURCETABLE 必须最先判：它的状态行是 `SOURCETABLE 200 OK`，含 `200 OK`，
    # 若先按 200 命中就会把「请求到了列表接口」误当成「取流成功」（§6.1）。
    if 'SOURCETABLE' in upper:
        return 'sourcetable', '该地址返回的是源列表，请确认端口', ERR_SOURCETABLE
    if '401' in upper:
        return 'auth', '用户名或密码错误', ERR_AUTH
    if '404' in upper:
        return 'mountpoint', '挂载点不存在，请重新「读取挂载点」', ERR_MOUNTPOINT
    if 'ICY 200' in upper or '200 OK' in upper:
        return 'ok', '已连接', None
    return 'unknown', f'无法识别的响应：{line or "(空)"}', ERR_NETWORK


class DownlinkAudit:
    """统计 CORS 下行播了什么，用于「为什么固定不了」的现场自证。

    2026-11-02 真机踩过的坑：一个 RTCM33 挂载点播了 1084（GLONASS MSM4）
    却不播 1230（GLONASS 码相位偏差），模块跟踪得到 GLONASS 却无法把它纳入
    模糊度固定，整整 300 秒卡在浮动解。当时没有任何日志能看出这一点，
    只能靠事后手写探针去数消息号——所以把它变成标定的常规体检。

    这里只做事后统计，不参与控制流：统计结果在 `CORS_CLOSE` 时打进日志，
    并在缺 1230 时给出明确告警文案（不提前终止，因为仍然可能固定，
    只是成功率下降——要不要继续由现场决定）。
    """

    #: 主流 CORS 都播的 MSM4 消息号 → 星座名
    MSM4_CONSTELLATIONS = {
        1074: 'GPS', 1084: 'GLONASS', 1094: 'Galileo',
        1114: 'QZSS', 1124: 'BDS', 1134: 'NavIC',
    }
    MSG_STATION_COORD = 1005
    MSG_GLONASS_BIAS = 1230

    def __init__(self):
        self.counts = {}          # msg -> 帧数
        self._buf = b''
        self.bytes_rx = 0

    def feed(self, data):
        """喂一段下行字节；内部切帧并累加计数（跨调用保留半帧，不丢）。"""
        if not data:
            return
        self.bytes_rx += len(data)
        self._buf += data
        try:
            frames, self._buf = extract_rtcm_frames(self._buf)
        except Exception as exc:                  # noqa: BLE001
            # 统计出错绝不能影响主流程（差分注入才是命脉）
            logging.debug('下行统计切帧异常（忽略）：%s', exc)
            return
        for frame in frames:
            if len(frame) < 6:
                continue
            msg = (frame[3] << 4) | (frame[4] >> 4)
            self.counts[msg] = self.counts.get(msg, 0) + 1

    @property
    def msm4(self):
        """实际收到的 MSM4 星座名列表（按标准顺序）。"""
        return [name for msg, name in self.MSM4_CONSTELLATIONS.items()
                if self.counts.get(msg)]

    def summary(self, elapsed=None):
        """人话摘要，给日志和标定报告用。"""
        parts = []
        if elapsed and elapsed > 0:
            parts.append(f'{self.bytes_rx / elapsed:.0f} B/s')
        parts.append(f'MSM4={"+".join(self.msm4) if self.msm4 else "无"}')
        if self.counts.get(self.MSG_STATION_COORD):
            parts.append(f'1005={self.counts[self.MSG_STATION_COORD]}帧')
        if self.counts.get(self.MSG_GLONASS_BIAS):
            parts.append(f'1230={self.counts[self.MSG_GLONASS_BIAS]}帧')
        else:
            parts.append('1230=缺失')
        return '，'.join(parts)

    def warnings(self):
        """返回需要提示用户的问题清单（空列表表示没发现问题）。"""
        problems = []
        if not self.msm4:
            problems.append('下行没有任何 MSM4 观测数据，无法做 RTK')
            return problems
        # GLONASS 播了观测却不播偏差：最隐蔽的一类配置缺陷
        if self.counts.get(1084) and not self.counts.get(self.MSG_GLONASS_BIAS):
            problems.append(
                '该挂载点播发了 GLONASS 观测（1084）却没有播 MT1230 码相位偏差：'
                '模块虽然能跟踪 GLONASS，但通常无法把它们纳入模糊度固定，'
                '会明显降低固定成功率。若长时间停在浮动解，请优先排查这一项')
        if not self.counts.get(self.MSG_STATION_COORD):
            problems.append('下行没有 MT1005 基站坐标，部分模块会拒绝使用差分')
        return problems


class CorsClient:
    """一次 CORS 取流会话。把「建连、分类错误、收 RTCM、发 GGA」收敛在一处。

    流程层（writer）只跟这个对象打交道，不需要知道 socket 的细节：
        client.connect()         建连 + 分类错误（可能重试）
        client.pump(gga_source)  单步轮询：收 RTCM → 注入回调、到点发 GGA
        client.close()           安全关闭 socket

    RTCM 的注入通过 `inject` 回调交给调用方，**绝不**自己调 `distribute()`：
    数据面此刻已停，且把 CORS 的差分当自身数据转发出去会让下游用错坐标（§6.4）。
    """

    def __init__(self, host, port, mountpoint, user='', password='',
                 inject=None, on_state=None, checkpoint=None):
        self.host = str(host or '')
        self.port = int(port or 0)
        self.mountpoint = str(mountpoint or '')
        self.user = str(user or '')
        self.password = str(password or '')
        self._inject = inject or (lambda data: None)      # 只进串口，不进 distribute
        self._on_state = on_state or (lambda state, message: None)
        self._checkpoint = checkpoint or (lambda: None)

        self.sock = None
        self.connected = False
        self._last_gga_sent = 0.0
        self._gga_sent = 0                 # 已上报给 Caster 的 GGA 条数（诊断用）
        self._last_activity = 0.0          # 最近一次收到数据（判「200 无数据」用）
        self._bytes_rx = 0
        self._frames_rx = 0
        self._disconnected_at = None       # 进入断连态的起始时刻
        self._cumulative_disconnect = 0.0
        self._closed = False
        self._rx_buf = b''                 # 握手 recv 多读到的 RTCM 余量
        # 下行体检器：统计播了哪些星座/消息号。connect() 里会重置，
        # 这里先建一个空实例，保证 connect 之前访问 audit 不炸。
        self.audit = DownlinkAudit()
        self._audit_started_at = 0.0

    # ---------- 生命周期 ----------
    @property
    def is_open(self):
        return self.sock is not None and self.connected and not self._closed

    @property
    def gga_sent(self):
        """已上报给 Caster 的 GGA 条数（诊断/验收用）。"""
        return self._gga_sent

    def connect(self):
        """建连 + 握手 + 分类错误。确定性错误（DNS/401/404/SOURCETABLE）直接抛，
        网络类错误走 §6.5 的 3 次指数退避重连。

        返回 True 表示连接就绪（可能还没数据，VRS 要等 GGA）。
        """
        last_error = None
        # 先做一次 DNS 解析（独立于 connect），把「域名错」和「网络不通」分开（§6.1）。
        try:
            socket.getaddrinfo(self.host, self.port)
        except socket.gaierror as e:
            raise CorsError(ERR_DNS,
                            f'域名解析失败，请检查服务器地址 {self.host}', {'error': str(e)})

        for attempt, delay in enumerate(RECONNECT_BACKOFF, start=1):
            self._checkpoint()
            try:
                return self._connect_once()
            except CorsError as e:
                # 确定性错误：重试也是错，直接上抛
                if e.kind in (ERR_AUTH, ERR_MOUNTPOINT, ERR_SOURCETABLE):
                    raise
                last_error = e
                if attempt < len(RECONNECT_BACKOFF):
                    self._on_state('retrying', f'连接失败，{delay:.0f} 秒后重试（{attempt}/3）')
                    self._interruptible_sleep(delay)
        raise CorsError(last_error.kind if last_error else ERR_NETWORK,
                        (last_error.message if last_error
                         else f'无法连接 {self.host}:{self.port}，请检查网络与端口'),
                        (last_error.detail if last_error else {}))

    def _connect_once(self):
        """单次建连。成功返回 True；失败抛 CorsError（已关 socket）。"""
        try:
            sock = connect_to_host(self.host, self.port, timeout=10)
        except CorsError:
            raise
        except socket.gaierror as e:
            raise CorsError(ERR_DNS, f'域名解析失败：{e}', {'error': str(e)})
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            raise CorsError(ERR_NETWORK,
                            f'无法连接 {self.host}:{self.port}，请检查网络与端口',
                            {'error': str(e)})
        except Exception as e:                       # noqa: BLE001 - connect 异常种类杂
            raise CorsError(ERR_NETWORK, f'连接失败：{e}', {'error': str(e)})

        try:
            sock.settimeout(NTRIP_HANDSHAKE_TIMEOUT)
            sock.sendall(build_ntrip_request(
                self.host, self.port, self.mountpoint, self.user, self.password).encode())
            resp_raw = sock.recv(2048)
        except Exception as e:                       # noqa: BLE001
            self._safe_close_sock(sock)
            raise CorsError(ERR_NETWORK, f'握手失败：{e}', {'error': str(e)})

        # 握手 recv 可能把首段 RTCM 一起读进来（Caster 有时在 200 之后立刻推流）。
        # 响应头和 RTCM 之间以 `\r\n\r\n` 分隔；头之后的余量必须缓存起来，
        # 否则首段差分会被丢掉（对 VRS 来说就是丢了一帧，不影响功能但浪费）。
        resp, rtcm_remainder = self._split_header(resp_raw)
        status, message, kind = classify_response(resp)
        if status != 'ok':
            self._safe_close_sock(sock)
            raise CorsError(kind or ERR_NETWORK, message, {'raw': (resp or '').splitlines()[:1]})

        # 握手成功后换成轮询超时（§6.3 的 0.2s），并清 connect 用的大超时
        sock.settimeout(POLL_TIMEOUT)
        self.sock = sock
        self.connected = True
        self._closed = False
        self._rx_buf = rtcm_remainder
        self._last_activity = time.time()
        self._last_gga_sent = 0.0
        self._disconnected_at = None
        # 重连时重置统计：每次会话单独体检，避免旧数据掩盖当前挂载点的问题
        self.audit = DownlinkAudit()
        self._audit_started_at = time.time()
        if rtcm_remainder:
            self.audit.feed(rtcm_remainder)
        self._on_state('connected', f'已连接 {self.host}:{self.port}/{self.mountpoint}')
        return True

    @staticmethod
    def _split_header(raw):
        """把握手 recv 的字节拆成「响应头」和「头之后的 RTCM 余量」。

        NTRIP 响应头以 `\r\n\r\n` 结束；后面的都是 RTCM 二进制。找不到分隔符时
        把整段当响应头（余量为空）—— 部分 Caster 的 200 响应不带标准空行。
        """
        if not raw:
            return '', b''
        marker = b'\r\n\r\n'
        index = raw.find(marker)
        if index < 0:
            return raw.decode(errors='ignore'), b''
        return raw[:index + len(marker)].decode(errors='ignore'), raw[index + len(marker):]

    # ---------- 单步轮询（§6.3 全双工） ----------
    def pump(self, gga_source, inject_writer=None):
        """跑一个 0.2 秒轮询片：收 RTCM → 注入 → 到点发 GGA。

        参数：
            gga_source     可调用，返回 `(has_data, gga_bytes)`；
                           has_data=False 表示「此刻没有可上报的位置」。
            inject_writer  可调用，把 RTCM 写进模块串口；None 用构造时的 inject。

        返回 True 表示本轮一切正常；抛 CorsError(ERR_NO_DATA / ERR_WRITE / ERR_NETWORK)。
        """
        self._checkpoint()
        if not self.is_open:
            raise CorsError(ERR_NETWORK, '连接已断开')

        data = b''
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            data = b''
        except OSError as e:
            self._mark_disconnected()
            raise CorsError(ERR_NETWORK, f'连接断开：{e}', {'error': str(e)})

        # 先把握手阶段缓存下来的 RTCM 余量补上（首段差分不丢）
        if self._rx_buf:
            data = self._rx_buf + data
            self._rx_buf = b''

        if data:
            self._last_activity = time.time()
            self._bytes_rx += len(data)
            # 下行体检：统计播了哪些星座/消息号（缺 1230 这类问题靠它暴露）
            self.audit.feed(data)
            # RTCM 注入：只进模块串口，绝不 distribute（§6.4）
            writer = inject_writer or self._inject
            try:
                writer(data)
            except Exception as e:               # noqa: BLE001
                self._mark_disconnected()
                raise CorsError(ERR_WRITE, f'注入模块串口失败：{e}', {'error': str(e)})

        # 逐条转发 GGA：节奏由 gga_source 决定（新位置立即发 / 无新位置保底重发），
        # 这里只做最小间隔保护（避免模块高频输出时把 Caster 打爆）。
        now = time.time()
        if now - self._last_gga_sent >= GGA_MIN_INTERVAL:
            has_data, payload = gga_source()
            if has_data and payload:
                try:
                    self.sock.sendall(payload)
                except OSError as e:
                    self._mark_disconnected()
                    raise CorsError(ERR_NETWORK, f'发送 GGA 失败：{e}', {'error': str(e)})
                self._last_gga_sent = now
                self._gga_sent += 1

        # VRS 场景：200 后 NO_DATA_GRACE 秒无数据 → 提示先发 GGA 再等（不判失败，§6.1）
        if self._last_activity > 0 and now - self._last_activity > NO_DATA_GRACE:
            self._on_state('waiting_data',
                           '已连接但暂无差分数据（VRS 通常需先收到 GGA 才下发 RTCM）')
        return True

    def send_gga_now(self, gga_source):
        """重连后立即发一次 GGA（§6.5：不等 1 秒，让 VRS 尽快认出位置）。"""
        if not self.is_open:
            return False
        has_data, payload = gga_source()
        if has_data and payload:
            try:
                self.sock.sendall(payload)
                self._last_gga_sent = time.time()
                return True
            except OSError:
                self._mark_disconnected()
                raise
        return False

    def close(self):
        """安全关闭 socket（幂等）。任何异常都吞掉，绝不掩盖上层语义。"""
        self._closed = True
        self.connected = False
        self._safe_close_sock(self.sock)
        self.sock = None

    # ---------- 断连累计（§6.5） ----------
    def _mark_disconnected(self):
        self.connected = False
        if self._disconnected_at is None:
            self._disconnected_at = time.time()
        self._safe_close_sock(self.sock)
        self.sock = None

    def cumulative_disconnect(self):
        """返回累计断连秒数（含正在断的这一次）。"""
        if self._disconnected_at is not None:
            return self._cumulative_disconnect + (time.time() - self._disconnected_at)
        return self._cumulative_disconnect

    def settle_disconnect(self):
        """断连结束（重连成功）时把本次断连时长累进累计值，并**返回本次时长**。

        返回值的用途见 `writer.CORS_NET_JITTER_TOLERANCE_SEC`：两秒内的抖动不该
        把已经累计起来的"连续固定解"计时器清零，否则网络每抖一次就要从头等 30 秒。
        """
        if self._disconnected_at is not None:
            seconds = time.time() - self._disconnected_at
            self._cumulative_disconnect += seconds
            self._disconnected_at = None
            return seconds
        return 0.0

    # ---------- 内部 ----------
    @staticmethod
    def _safe_close_sock(sock):
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _interruptible_sleep(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._checkpoint()
            time.sleep(0.1)


# ==================== GGA 管理器（方案 §6.2） ====================
# 职责：自动检测模块原生 GGA（有则透传），无则从 RMC/GLL 软件生成。
# 软件生成严格按 §6.2 合规规则：Talker 统一 GN、质量位 1（单点）、缺失字段留空。
# 上报前过一道防抖（GGA_JUMP_LIMIT）：只丢异常跳变，正常漂移完全跟随模块。
class GgaManager:
    """决定「上报给 CORS 的那条 GGA」从哪来，并按模块节奏逐条转发。

    上报节奏（见文件头 GGA_MIN_INTERVAL / GGA_KEEPALIVE 的说明）：
        `source()` 每次被调用时判断——**行变了就立刻发**，行没变则按 GGA_KEEPALIVE
        保底重发。模块 1 Hz 输出就等于 1 Hz 转发，5 Hz 输出就 5 Hz 转发，
        既不攒批也不抽样；模块停止输出时也不会让 Caster 因超时断流。

    三条来源规则（§6.2）：
        1. 模块原生输出 GGA → 原样透传（时间戳/质量位/星数/高程一个字都不改）；
        2. 模块原生 GGA 但质量 =0 → 仍然转发（连接不会断），提示未定位；
        3. 模块不输出 GGA 但有 RMC/GLL → 软件生成（质量位 1，缺失字段留空）。

    防抖规则（标定阶段唯一的"改写"，且只在异常时触发）：
        · 与上一条**正常上报**的 GGA 距离 > `GGA_JUMP_LIMIT` → 本条丢弃，
          复用上一条兜底一次；
        · 连续丢弃 `GGA_JUMP_MAX_DROP` 条 → 停发 GGA 并告警（不无限补发旧位置）；
        · 超过 `GGA_STALE_LIMIT` 秒没有新的合法 GGA → 停发（不替模块报假位置）。
    为什么是"防抖"而不是"冻结"：见文件头的长说明。
    """

    def __init__(self, channel, on_hint=None, guard=None):
        self.channel = channel
        self._on_hint = on_hint or (lambda text: None)
        # 标定阶段默认只做防抖，不改写位置（见文件头"标定阶段为什么不冻结"）
        self.guard = GGA_GUARD_ENABLED if guard is None else bool(guard)
        self._anchor = None            # 上一条正常上报的 (lat, lon)，判跳变用
        self._anchor_at = 0.0          # 锚点时刻（判"过期"用）
        self._anchor_payload = None    # 上一条正常上报的原始行（兜底复用）
        self._drops_since_anchor = 0    # 距上次正常上报连续丢了几条
        self._last_raw = None          # 最近一次组好的 GGA 行（bytes，诊断用）
        self._last_forwarded = None    # 最近一次真正上报出去的那条（用于判「新」）
        self._last_sent_at = 0.0
        self._last_guard_log_at = 0.0
        self.software_mode = False     # True = 走软件生成（界面要标注精度受限）
        self.forwarded = 0             # 已上报条数（新位置）
        self.repeated = 0              # 保底重发条数（无新位置）
        # 防抖计数：界面与落库记录据此判断"这次标定的 VRS 输入是否被大幅跳变污染过"
        self.jump_dropped = 0          # 丢弃过的异常 GGA 条数
        self.jump_stopped = False      # 是否因连续跳变而停发
        self.last_jump_distance = None
        self.stale_stopped = 0         # 因长时间无新 GGA 而停发的条数

    @property
    def guard_active(self):
        """防抖是否已建立锚点（True=后续跳变会被拦住）。界面据此常驻提示。"""
        return self.guard and self._anchor is not None

    def note_guard_active(self, label=''):
        """状态机每轮调用一次：防抖生效时打一条 `[GGA_GUARD_ACTIVE]` 日志。

        按 `GGA_GUARD_LOG_INTERVAL` 限流（原因同 GGA_MIN_INTERVAL 的说明）。
        正常跟随模块时这条只是"链路活着"的心跳，跳变时才在 `_guard` 里另打日志。
        """
        if not self.guard_active:
            return
        now = time.time()
        if now - self._last_guard_log_at < GGA_GUARD_LOG_INTERVAL:
            return
        self._last_guard_log_at = now
        where = f'（{label}）' if label else ''
        logging.info('%s GGA 防抖已建立锚点%s：位置跟随模块实际输出，'
                     '仅跳变 > %.0f m 的异常行会被丢弃；标定期间天线严禁移动',
                     LOG_GGA_GUARD, where, GGA_JUMP_LIMIT)

    def source(self, force=False):
        """返回 `(has_data, payload_bytes)`，给 `CorsClient.pump` 用。

        force=False（默认，pump 路径）：只在「有一条新的 GGA」或「距上次上报
        超过 GGA_KEEPALIVE 秒」时返回数据，其余返回 `(False, b'')`。
        force=True（重连/首连路径）：无视节奏，立刻给当前最新的一条。
        """
        has_data, payload = self._current_payload()
        if not has_data:
            return False, b''
        now = time.time()
        if force:
            self._last_forwarded = payload
            self._last_sent_at = now
            return True, payload
        if payload != self._last_forwarded:
            # 模块出了新的一行 → 立刻转发（这就是「持续转发」，不攒批、不抽样）
            self._last_forwarded = payload
            self._last_sent_at = now
            self.forwarded += 1
            return True, payload
        # 行没变：模块这条 GGA 还是旧的。等够保底间隔重发一次，防止 Caster 断流。
        if now - self._last_sent_at >= GGA_KEEPALIVE:
            self._last_sent_at = now
            self.repeated += 1
            return True, payload
        return False, b''

    def force_source(self):
        """重连后立即要一条（§6.5：让 VRS 尽快重新认出位置）。"""
        return self.source(force=True)

    def _current_payload(self):
        """按 §6.2 组出「当前应该上报的那一条」：原生 GGA 优先，否则软件生成。"""
        channel = self.channel
        if channel is None:
            return False, b''

        gga = channel.cache.get('gga')
        if gga and gga.get('valid') and gga.get('lat') is not None:
            # 原生 GGA 可用（质量任意）：透传原始行，保持原始精度与质量位
            self.software_mode = False
            raw = gga.get('raw')
            if raw:
                if gga.get('quality') in (0, None):
                    self._on_hint('模块尚未定位（GGA 质量位为 0），差分可能暂不生效')
                payload = (raw if raw.endswith('\r\n') else raw + '\r\n').encode('ascii', 'replace')
                self._last_raw = payload          # 未改写的原始行，诊断用
                return self._guard(payload, gga.get('lat'), gga.get('lon'))

        # 无原生 GGA：尝试软件生成（§6.2）
        built = self._build_software_gga()
        if built is None:
            return False, b''
        payload, lat, lon = built
        if not self.software_mode:
            logging.info('%s 模块不输出 GGA，改用 RMC/GLL 合成（质量位固定 1，'
                         '精度为单点级，不具备厘米级绝对精度）', LOG_GGA_GENERATED)
        self.software_mode = True
        self._last_raw = payload
        # 合成 GGA 同样过防抖：它虽然只是单点解，但一旦某个 RMC 突然跳到另一个
        # 城市（缓存串了、模块切到基准站播了 1005 的坐标），VRS 会被带跑。
        return self._guard(payload, lat, lon)

    def _guard(self, payload, lat, lon):
        """防抖：异常跳变丢弃并复用上一条；连续跳变则停发。**绝不改写经纬度。**

        判据只有三条，都是"宁可偶尔丢一条，也不要把离谱位置塞给 VRS"：
            1. 与上一条正常上报距离 > GGA_JUMP_LIMIT → 丢本条、复用上一条；
            2. 连续丢弃 ≥ GGA_JUMP_MAX_DROP → 停发（不无限补发旧位置）；
            3. 距上一条正常上报 > GGA_STALE_LIMIT 秒 → 停发（模块没定位了）。
        正常单点漂移（几十米量级）永远在门限内，直接原样透传，一个字节都不动。
        """
        if not self.guard:
            return True, payload
        now = time.time()
        if lat is None or lon is None:
            return True, payload

        # 锚点过期：模块早就没有新的合法 GGA 了，不能再替它报旧位置
        if self._anchor is not None and now - self._anchor_at > GGA_STALE_LIMIT:
            self.stale_stopped += 1
            if self.stale_stopped % 50 == 1:
                logging.warning(
                    '%s 已 %.0f 秒没有新的合法 GGA，暂停上报给 CORS；'
                    '模块可能掉星或被切成基准站',
                    LOG_GGA_STALE, now - self._anchor_at)
            return False, b''

        if self._anchor is None:
            # 第一条：直接落锚（还没有参考，谈不上"跳变"）
            self._anchor = (lat, lon)
            self._anchor_at = now
            self._anchor_payload = payload
            self._drops_since_anchor = 0
            return True, payload

        distance = self._distance(self._anchor, (lat, lon))
        if distance > GGA_JUMP_LIMIT:
            self.jump_dropped += 1
            self._drops_since_anchor += 1
            self.last_jump_distance = distance
            # 连续跳变：说明模块位置真的在乱跳（或模块被切成了基准站播另一处坐标）。
            # 这时继续补发旧 GGA 等于骗 VRS，比断流更糟，直接停发等它自己恢复。
            if self._drops_since_anchor >= GGA_JUMP_MAX_DROP:
                if not self.jump_stopped:
                    self.jump_stopped = True
                    logging.error(
                        '%s 连续 %d 条 GGA 位置跳变超过 %.0f m（最近 %.0f m），'
                        '已停止上报；请检查天线是否被移动、模块是否被切成基准站',
                        LOG_GGA_JUMP, GGA_JUMP_MAX_DROP, GGA_JUMP_LIMIT, distance)
                    self._on_hint(
                        f'GGA 连续 {GGA_JUMP_MAX_DROP} 次位置跳变超过 '
                        f'{GGA_JUMP_LIMIT:.0f} 米，已暂停上报给 CORS：'
                        f'模块位置在乱跳，请检查天线与模块模式')
                return False, b''
            if now - self._last_guard_log_at >= GGA_GUARD_LOG_INTERVAL:
                self._last_guard_log_at = now
                logging.warning(
                    '%s 丢弃一条跳变 GGA：偏离上一条正常位置 %.0f m（门限 %.0f m），'
                    '本次复用上一条兜底', LOG_GGA_JUMP, distance, GGA_JUMP_LIMIT)
            # 复用上一条正常行兜底**一次**（Caster 不断流的短期保护）
            return (True, self._anchor_payload) if self._anchor_payload else (False, b'')

        # 正常范围：更新锚点，原行透传
        self._anchor = (lat, lon)
        self._anchor_at = now
        self._anchor_payload = payload
        self._drops_since_anchor = 0
        if self.jump_stopped:
            self.jump_stopped = False
            logging.info('%s GGA 位置恢复正常，恢复上报（累计丢弃 %d 条）',
                         LOG_GGA_JUMP, self.jump_dropped)
        return True, payload

    def _build_software_gga(self):
        """从 RMC/GLL 提取位置，按 §6.2 软件生成 $GNGGA（质量位 1，缺失留空）。

        返回 `(payload_bytes, lat, lon)`，拿不到位置返回 None。
        带上 lat/lon 是为了让防抖也能作用于合成 GGA —— 否则 RMC 突然跳到
        另一处，VRS 一样会被带跑。
        """
        channel = self.channel
        pos = None
        for key in ('rmc', 'gll'):
            item = channel.cache.get(key)
            if item and item.get('valid') and item.get('lat') is not None:
                pos = item
                break
        if pos is None:
            return None
        lat = pos.get('lat')
        lon = pos.get('lon')
        height = pos.get('height')
        if height is None:
            # RMC/GLL 没有海拔；GGA 缺海拔会让 ECEF 高差几米（§6.2 / §7.1）
            height = 0.0
        # 卫星数/HDOP：能从 GSA 取就取，取不到留空（NMEA 允许空字段，§6.2）
        gsa = channel.cache.get('gsa')
        num_sats = None
        hdop = None
        if gsa and gsa.get('valid'):
            num_sats = len(gsa.get('sats') or []) or None
            hdop = gsa.get('hdop')
        # 大地水准面差距：软件生成路线拿不到，留空（§7.1 会兜底）
        line = nmea.build_gga(lat, lon, height, quality=1, num_sats=num_sats,
                              hdop=hdop, geoid_sep=None)
        return line.encode('ascii', 'replace'), lat, lon

    @staticmethod
    def _nmea_deg(value):
        """NMEA 的 ddmm.mmmm / dddmm.mmmm 转十进制度（方向由 N/S、E/W 字段另给）。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        degrees = int(number // 100)
        return degrees + (number - degrees * 100) / 60.0

    @staticmethod
    def _distance(a, b):
        """两个十进制度点之间的近似距离（米）。100 米量级用等距圆柱近似足够。"""
        lat1, lon1 = a
        lat2, lon2 = b
        mean_lat = math.radians((lat1 + lat2) / 2.0)
        dx = (lon2 - lon1) * math.cos(mean_lat)
        dy = lat2 - lat1
        return math.hypot(dx, dy) * 111320.0

    def reset_rhythm(self):
        """重连后调用：把「已上报」状态清空，让下一次 source() 必定发一条（§6.5）。"""
        self._last_sent_at = 0.0
        self._last_forwarded = None


# ==================== 自测（假 Caster，不需要硬件） ====================
class _FakeCasterServer:
    """内存里的假 NTRIP Caster：能回 200+RTCM、401、404、SOURCETABLE、静默。"""

    def __init__(self, mode='ok', rtcm_payload=b''):
        import threading
        self.mode = mode
        self.rtcm_payload = rtcm_payload
        self.received = []          # 收到的字节（验证 GGA 是否发出）
        self._ready = threading.Event()
        self._stop = False
        self._sock = None
        self._listener = None
        self._thread = None

    def start(self):
        import threading
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen(4)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        # 循环 accept：connect_to_host 带 @retry(3)，一次会话里可能连多次
        while not self._stop:
            try:
                self._sock, _addr = self._listener.accept()
            except OSError:
                return
            self._sock.settimeout(0.2)
            try:
                self._sock.recv(4096)      # 吃掉 GET 请求
            except OSError:
                pass
            if self.mode == '401':
                self._sock.sendall(b'HTTP/1.1 401 Unauthorized\r\n\r\n')
                self._sock.close()
                continue
            if self.mode == '404':
                self._sock.sendall(b'HTTP/1.1 404 Not Found\r\n\r\n')
                self._sock.close()
                continue
            if self.mode == 'sourcetable':
                self._sock.sendall(b'SOURCETABLE 200 OK\r\nENDSOURCETABLE\r\n')
                self._sock.close()
                continue
            if self.mode == 'table':
                # 有内容的源列表（验证 read_mountpoints 的解析，而不是只验证"空表"）
                self._sock.sendall(
                    b'SOURCETABLE 200 OK\r\n'
                    b'STR;MP_ONE;ONE;RTCM 3.3;1005(1),1074(1);2;GPS;CHN;31;117;0;0;'
                    b'SNIP;X;NONE;B;0;0\r\n'
                    b'STR;MP_TWO;TWO;RTCM 3.3;1005(1),1074(1);2;BDS GPS;CHN;31;117;0;0;'
                    b'SNIP;X;NONE;B;0;0\r\n'
                    b'ENDSOURCETABLE\r\n')
                self._sock.close()
                continue
            if self.mode == 'silent':
                # 先回 200，再保持静默，触发「200 无数据」
                self._sock.sendall(b'ICY 200 OK\r\n\r\n')
                deadline = time.time() + 5.0
                while time.time() < deadline and not self._stop:
                    try:
                        chunk = self._sock.recv(4096)
                        if chunk:
                            self.received.append(chunk)
                    except socket.timeout:
                        pass
                self._sock.close()
                continue
            # ok：先回 200，再发一段 RTCM，然后循环读 GGA
            self._sock.sendall(b'ICY 200 OK\r\n\r\n' + self.rtcm_payload)
            deadline = time.time() + 5.0
            while time.time() < deadline and not self._stop:
                try:
                    chunk = self._sock.recv(4096)
                    if chunk:
                        self.received.append(chunk)
                    else:
                        break
                except socket.timeout:
                    pass
            self._sock.close()

    def stop(self):
        self._stop = True
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        try:
            self._listener.close()
        except OSError:
            pass


def _selftest():
    import threading

    from .drivers.base import Channel
    from .proto.rtcm import _build_1005_frame

    print('=== cors 自测（假 Caster，不需要硬件） ===')

    # 造一段合法 RTCM 1005（用 proto.rtcm 的组帧函数）
    rtcm = _build_1005_frame(-2519265.0, 4849534.0, 3277834.0)

    # 1) 请求组帧：Authorization 只在有账号时出现
    req = build_ntrip_request('host', 8002, 'MP', 'u', 'p')
    assert 'GET /MP HTTP/1.1' in req and 'Authorization: Basic' in req, req
    req2 = build_ntrip_request('host', 8002, 'MP')
    assert 'Authorization' not in req2
    print('[1] 请求组帧 OK（账号可选）')

    # 2) 响应分类
    assert classify_response('ICY 200 OK\r\n')[0] == 'ok'
    assert classify_response('HTTP/1.1 401 Unauthorized')[2] == ERR_AUTH
    assert classify_response('HTTP/1.1 404 Not Found')[2] == ERR_MOUNTPOINT
    assert classify_response('SOURCETABLE 200 OK')[2] == ERR_SOURCETABLE
    print('[2] 响应分类 OK  200/401/404/SOURCETABLE')

    # 3) 401 确定性错误：不重试，直接抛 ERR_AUTH
    srv = _FakeCasterServer(mode='401').start()
    client = CorsClient('127.0.0.1', srv.port, 'MP', 'u', 'p')
    try:
        client.connect()
        raised = None
    except CorsError as e:
        raised = e
    assert raised is not None and raised.kind == ERR_AUTH, raised
    srv.stop()
    print(f'[3] 401 不重试 OK  {raised.message}')

    # 4) 404 / SOURCETABLE 确定性错误
    for mode, kind in (('404', ERR_MOUNTPOINT), ('sourcetable', ERR_SOURCETABLE)):
        srv = _FakeCasterServer(mode=mode).start()
        client = CorsClient('127.0.0.1', srv.port, 'MP', 'u', 'p')
        try:
            client.connect()
            raised = None
        except CorsError as e:
            raised = e
        assert raised is not None and raised.kind == kind, (mode, raised)
        srv.stop()
    print('[4] 404 / SOURCETABLE 不重试 OK')

    # 5) DNS 失败 → ERR_DNS
    client = CorsClient('nonexistent.invalid', 8002, 'MP', 'u', 'p')
    try:
        client.connect()
        raised = None
    except CorsError as e:
        raised = e
    assert raised is not None and raised.kind == ERR_DNS, raised
    print(f'[5] DNS 失败分类 OK  {raised.message}')

    # 6) 正常连接 + RTCM 注入 + GGA 透传（原生 GGA）
    srv = _FakeCasterServer(mode='ok', rtcm_payload=rtcm).start()
    injected = []
    client = CorsClient('127.0.0.1', srv.port, 'MP', 'u', 'p',
                        inject=lambda data: injected.append(data))
    assert client.connect() is True
    # 造一个带原生 GGA 的 channel
    ch = Channel(port='FAKE', baud=115200)
    ch.cache['gga'] = {
        'valid': True, 'lat': 30.5, 'lon': 114.3, 'quality': 1,
        'raw': '$GNGGA,120000.00,3030.00000,N,11418.00000,E,1,12,0.8,50.000,M,-9.5,M,,*XX',
    }
    mgr = GgaManager(ch)
    # 跑几轮 pump，把 GGA 发出去、RTCM 收进来
    deadline = time.time() + 2.5
    while time.time() < deadline and not injected:
        client.pump(mgr.source)
        time.sleep(0.05)
    assert injected, '应该收到 RTCM 注入'
    # 验证注入的是 RTCM 帧（0xD3 开头），不是别的
    assert injected[0][0] == 0xD3, injected[0][:4]
    # 验证 GGA 发出去了（透传原始行）
    assert srv.received, 'Caster 应该收到 GGA'
    client.close()
    srv.stop()
    print(f'[6] 建连+RTCM注入+GGA透传 OK  注入 {len(injected)} 段，'
          f'上报 GGA {sum(len(c) for c in srv.received)} 字节')

    # 7) 软件生成 GGA（无原生 GGA，只有 RMC）
    srv = _FakeCasterServer(mode='ok', rtcm_payload=rtcm).start()
    client = CorsClient('127.0.0.1', srv.port, 'MP', 'u', 'p')
    client.connect()
    ch = Channel(port='FAKE', baud=115200)
    ch.cache['rmc'] = {'valid': True, 'lat': 30.5, 'lon': 114.3}
    ch.cache['gsa'] = {'valid': True, 'sats': ['1', '2', '3', '4'], 'hdop': 0.9}
    mgr = GgaManager(ch)
    has_data, payload = mgr.source()
    assert has_data and payload.startswith(b'$GNGGA'), payload
    assert mgr.software_mode is True
    # 质量位必须是 1（单点），不能谎报 4
    assert b',1,' in payload, payload
    client.close()
    srv.stop()
    print(f'[7] 软件生成 GGA OK  {payload.decode().strip()}（质量位=1）')

    # 8) 200 无数据：pump 不判失败，返回 True 且提示 waiting_data
    srv = _FakeCasterServer(mode='silent').start()
    client = CorsClient('127.0.0.1', srv.port, 'MP', 'u', 'p')
    client.connect()
    states = []
    client._on_state = lambda s, m: states.append(s)
    ok = True
    deadline = time.time() + 6.0
    while time.time() < deadline:
        client.pump(lambda: (False, b''))
        time.sleep(0.3)
    assert 'waiting_data' in states, states
    client.close()
    srv.stop()
    print('[8] 200 无数据不判失败 OK  进入 waiting_data 提示')

    # 9) 读取挂载点：复用主程序 SOURCETABLE 解析（复用而不是重写）
    srv = _FakeCasterServer(mode='table').start()
    header, items = read_mountpoints('127.0.0.1', srv.port, 'u', 'p')
    srv.stop()
    assert len(items) == 2, items
    assert [i['mountpoint'] for i in items] == ['MP_ONE', 'MP_TWO'], items
    assert items[0]['format'] == 'RTCM 3.3' and items[0]['nav'] == 'GPS', items[0]
    assert items[1]['nav'] == 'BDS GPS', items[1]
    print(f'[9] 读取挂载点 OK  {len(items)} 个：'
          + '、'.join(f'{i["mountpoint"]}({i["nav"]})' for i in items))

    # 10) 读取挂载点：DNS 失败要单独分类（地址写错 vs 网络不通）
    try:
        read_mountpoints('nonexistent.invalid', 8002)
        raised = None
    except CorsError as e:
        raised = e
    assert raised is not None and raised.kind == ERR_DNS, raised
    print(f'[10] 挂载点 DNS 失败分类 OK  {raised.message}')

    # ---------- 11. GGA 逐条转发节奏（新行立刻发 / 同一条不重发 / 超时保底重发）----------
    # 这是「持续转发」的核心：模块每吐一条新 GGA 就上报一条，不攒批、不抽样；
    # 行没变时不重复刷（省流量、不吵 Caster），但超过 GGA_KEEPALIVE 要保底重发，
    # 否则模块掉星后 Caster 会因为「长时间无 GGA」把连接踢掉。
    ch_g = Channel(port='SELFTEST')
    mgr_g = GgaManager(ch_g)
    line1 = nmea.frame('GNGGA,000001.00,3030.00000,N,11418.00000,E,4,16,0.6,30.0,M,-9.5,M,1.0,0000')
    line2 = nmea.frame('GNGGA,000002.00,3030.00000,N,11418.00000,E,4,16,0.6,30.0,M,-9.5,M,1.0,0000')
    ch_g.cache['gga'] = {'valid': True, 'lat': 30.5, 'lon': 114.3, 'quality': 4,
                         'num_sats': 16, 'hdop': 0.6, 'height': 30.0,
                         'geoid_sep': -9.5, 'raw': line1}
    assert mgr_g.source()[0] is True, '第一条新 GGA 必须上报'
    assert mgr_g.source()[0] is False, '同一条 GGA 不得重复上报'
    ch_g.cache['gga'] = dict(ch_g.cache['gga'], raw=line2)
    assert mgr_g.source()[0] is True, '换了一条新 GGA 必须立刻上报'
    # 保底重发：把上次上报时间往前拨，模拟「模块 3 秒没出新行」
    mgr_g._last_sent_at -= (GGA_KEEPALIVE + 0.5)
    has, payload = mgr_g.source()
    assert has is True and payload.rstrip() == line2.rstrip().encode('ascii'), \
        '超时未更新应保底重发最后一条'
    # force 路径（重连/首连）无视节奏，必须立刻给一条
    ch_g.cache['gga'] = dict(ch_g.cache['gga'], raw=line1)
    mgr_g.source()
    assert mgr_g.source()[0] is False, '同一条不该重复'
    assert mgr_g.force_source()[0] is True, 'force_source 必须无视节奏立刻给'
    print(f'[11] GGA 逐条转发节奏 OK  新行立即发 / 同条不重发 / 超 {GGA_KEEPALIVE:g}s 保底重发')

    # ---------- 12. GGA 防抖（标定阶段：位置跟随模块，只丢异常跳变）----------
    # 这是对"冻结"的替代。冻结曾经把上报位置钉在第一条 GGA 上，VRS 的虚拟站
    # 与模块真实位置长期不符，模糊度反而更难固定。现在默认原样透传，只拦
    # "几分钟内跳几百米到几公里"这种会让服务端反复换虚拟站的坏行。
    ch_f = Channel(port='SELFTEST2')
    mgr_f = GgaManager(ch_f)

    def _feed(mgr, line, quality, lat, lon, sats=16, hdop=0.7, height=30.0, at=None):
        ch_f.cache['gga'] = {'valid': True, 'lat': lat, 'lon': lon, 'quality': quality,
                             'num_sats': sats, 'hdop': hdop, 'height': height,
                             'geoid_sep': -9.5, 'raw': line}
        if at is not None:                      # 手动把锚点时间往前拨，模拟"过了一段时间"
            mgr._anchor_at = at
        has, payload = mgr.source()
        return has, payload.decode('ascii') if has else ''

    a = nmea.frame('GNGGA,010100.00,3030.00000,N,11418.00000,E,5,20,0.7,30.0,M,-9.5,M,1.0,0000')
    b = nmea.frame('GNGGA,010101.00,3030.00100,N,11418.00100,E,5,20,0.7,30.0,M,-9.5,M,1.0,0000')
    c = nmea.frame('GNGGA,010102.00,3030.00150,N,11418.00150,E,5,20,0.7,30.0,M,-9.5,M,1.0,0000')

    has, first = _feed(mgr_f, a, 4, 30.50000, 114.30000)          # 第一条：落锚，原样透传
    has2, second = _feed(mgr_f, b, 4, 30.500016, 114.300016)      # ~2 m 正常漂移：跟随
    assert has and first.split(',')[2] == '3030.00000', f'第一条必须原样透传：{first[:40]}'
    assert has2 and second.split(',')[2] == b.split(',')[2], \
        f'正常漂移必须原样透传（不许改写经纬度）：{second[:40]}'
    assert second.split(',')[1] != first.split(',')[1], '时间戳必须来自模块实时值'

    # 单条大跳变（约 11 km）：本条丢弃。`_guard` 给出"复用上一条"，但 `source()`
    # 会按"行是否变化"去重 —— 复用的就是上一行，所以这一拍**不发任何东西**，
    # 既没把离谱位置塞给 VRS，也没有往 Caster 灌重复行。
    jump = nmea.frame('GNGGA,010103.00,3040.00000,N,11430.00000,E,4,12,1.1,30.0,M,-9.5,M,1.0,0000')
    has3, third = _feed(mgr_f, jump, 4, 30.66667, 114.50000)
    assert has3 is False and third == '', f'跳变行不得上报：{third[:40]}'
    assert mgr_f.jump_dropped == 1, mgr_f.jump_dropped
    assert mgr_f.last_jump_distance > GGA_JUMP_LIMIT, mgr_f.last_jump_distance

    # 恢复正常：必须立刻跟上真实位置（不因为刚才丢过就一直用旧行）
    # 注意模块恢复后会吐**新的一行**（时间戳变），所以这里用新行而不是复述 c
    c2 = nmea.frame('GNGGA,010104.00,3030.00150,N,11418.00150,E,5,20,0.7,30.0,M,-9.5,M,1.0,0000')
    has4, fourth = _feed(mgr_f, c2, 4, 30.500025, 114.300025)
    assert has4 and fourth.split(',')[2] == c2.split(',')[2], \
        f'恢复后必须跟随模块真实位置：{fourth[:40]}'
    assert mgr_f.jump_stopped is False

    # 连续跳变 3 条：停发（不无限补发旧 GGA，那样等于骗 VRS）
    for i in range(GGA_JUMP_MAX_DROP):
        line = nmea.frame(f'GNGGA,01020{i}.00,3040.00000,N,11430.00000,E,4,12,1.1,'
                          f'30.0,M,-9.5,M,1.0,0000')
        has5, _ = _feed(mgr_f, line, 4, 30.66667, 114.50000)
    assert has5 is False and mgr_f.jump_stopped is True, \
        f'连续 {GGA_JUMP_MAX_DROP} 条跳变必须停发，实际 has={has5}'

    # 恢复后重新放行（同样用新行）
    c3 = nmea.frame('GNGGA,010105.00,3030.00150,N,11418.00150,E,5,20,0.7,30.0,M,-9.5,M,1.0,0000')
    has6, sixth = _feed(mgr_f, c3, 4, 30.500025, 114.300025)
    assert has6 and sixth.split(',')[2] == c3.split(',')[2] and mgr_f.jump_stopped is False, \
        f'恢复后必须重新上报：{sixth[:40]}'

    # 锚点过期：模块不再输出新的合法 GGA，不能替它报旧位置
    ch_f.cache['gga'] = dict(ch_f.cache['gga'], raw=c3)
    mgr_f._anchor_at = time.time() - (GGA_STALE_LIMIT + 5)
    has7, _ = mgr_f.source()
    assert has7 is False and mgr_f.stale_stopped >= 1, \
        f'超过 {GGA_STALE_LIMIT:g}s 无新 GGA 必须停发，实际 has={has7}'

    # 关掉防抖时要能回到「一条不改地转发」
    mgr_n = GgaManager(ch_f, guard=False)
    mgr_n._anchor_at = time.time()
    has8, fourth2 = _feed(mgr_n, a, 4, 30.50000, 114.30000)
    assert has8 and fourth2.split(',')[2] == a.split(',')[2], \
        f'guard=False 应原样转发：{fourth2[:40]}'
    print(f'[12] GGA 防抖 OK  正常漂移原样透传 / 跳变 >{GGA_JUMP_LIMIT:.0f}m 丢弃复用 / '
          f'连续 {GGA_JUMP_MAX_DROP} 条停发 / 过期停发')

    print('=== cors 自测全部通过 ===')
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(_selftest())
