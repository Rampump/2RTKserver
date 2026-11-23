# -*- coding: utf-8 -*-
"""全局常量。全部是不可变值，任何模块只读。

注意：这里原则上不 import 任何项目内模块，避免出现「constants 依赖谁」的环。
唯一的例外是 rtkserver.version —— 它只用标准库、不 import 任何东西，
所以 constants → version 是单向的，不会成环。
"""
import os

from ..version import __version__ as APP_VERSION

# ==================== 程序标识 ====================
# 版本号不写死在 LOGO 里：以前这里硬编码 2.1.8，发版时极容易被漏掉，
# 结果控制台横幅、网页版本、mDNS 记录三者对不上。
ART_LOGO = """
    ██████╗ ██████╗ ████████╗██╗  ██╗
    ╚════██╗██╔══██╗╚══██╔══╝██║ ██╔╝
     █████╔╝██████╗╔   ██║   █████╔╝ 
    ██╔═══╝ ██╔══██╗   ██║   ██║  ██╗
    ███████╗██║  ██║   ██║   ██║  ██╗
    ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝
         2RTK Ntrip Server {version}
""".format(version=APP_VERSION)

# ==================== Web 服务 ====================
# 原先硬编码在 main() 的 app.run(...) 里，这里集中一处，便于测试与文档引用。
WEB_HOST = '0.0.0.0'
WEB_PORT = 5757

# ==================== 运行期缓冲 ====================
MAX_BUFFER_SIZE = 10 * 1024 * 1024
RTCM_TYPE_WINDOW = 60        # 秒：超过该窗口没再出现的类型不写入源列表

# ==================== 安全与网络参数 ====================
PBKDF2_ITERATIONS = 100000          # ARM 小板上的折中值，登录一次约 0.2~0.5 秒
PASSWORD_SCHEME = 'pbkdf2_sha256'
SENSITIVE_KEY_HINTS = ('password', 'passwd', 'secret')
NTRIP_HANDSHAKE_TIMEOUT = 10        # NTRIP 请求/响应阶段超时
CASTER_WRITE_TIMEOUT = 15           # 上传端写超时，用来尽早发现死连接
RELAY_READ_TIMEOUT = 20             # 中继端读超时，超时视为"本轮无数据"
# 注意：RELAY_IDLE_RECONNECT 与各退避表统一放在下面的「重连退避」一节

# ==================== 数据源门闸（三个出口共用） ====================
# 首帧门闸：拿不到一帧 RTCM 就绝不去连云端 / 不开本地监听 / 不占数传口。
# 10 秒的依据：RTCM 常见 1 Hz，慢流也 ≥0.2 Hz；比 detect_serial() 的单档 2 秒
# 宽松，但比云端常见的 30~60 秒空闲踢线短得多。
SOURCE_FIRST_DATA_TIMEOUT = 10.0
# 运行期断流：模块被拔 / 中继上游停流后，超过这个时间就主动断开上传，
# 避免"被云端踢 + 立刻重连"的抖动。取值必须明显小于云端空闲阈值。
SOURCE_STALL_TIMEOUT = 15.0

# 设为 1 时，未登录的访问者也能看到基准站精确坐标（旧行为）
PUBLIC_STATION_COORDS = os.environ.get('PUBLIC_STATION_COORDS', '0') == '1'

# ==================== WebSocket 监控广播参数 ====================
MAX_WS_CLIENTS = 32                 # 监控连接上限：每个连接占一个线程，必须有上限
WS_OUTBOX_MAXLEN = 32               # 每连接待发帧上限，满了丢最旧（不阻塞生产者）
WS_RECEIVE_POLL = 0.2               # 收包轮询间隔，同时也是"把队列发空"的最小周期
WS_DROP_LIMIT = 100                 # 连续丢帧超过该值说明客户端长期不消费，主动断开
MSM_BROADCAST_INTERVAL = 1.0        # 每个星座每秒最多推一次（原先每条 MSM 都推）
MSM_CELL_FIELDS = ('CELLPRN', 'CELLSIG', 'DF403', 'DF405', 'DF408')

# ==================== 本地 NTRIP Caster 参数 ====================
# 作用：把本机正在转发的那一路 RTCM 数据，同时以 NTRIP Caster 的身份对内网发布，
#       移动站/手簿直接连本机取差分，不必再绕到外网 Caster。
# 注意：这里是"再发一份"，不改变原有的上传链路——原来往哪上传现在还往哪上传。
LOCAL_CASTER_HOST = '0.0.0.0'             # 默认监听所有网卡
# 默认 2102 而不是 2101：2101 已经是中继/上传端口的默认值，而本地 Caster 是
# 「本机真实监听」，配置页要求它与出口端口错开，两个默认值一样会一开就撞。
LOCAL_CASTER_PORT = 2102
LOCAL_CASTER_MOUNTPOINT = '2RTK'          # 唯一挂载点，固定
LOCAL_CASTER_USER = '2RTK'                # 固定账号
LOCAL_CASTER_PASSWORD = '2RTK'            # 固定密码
LOCAL_CASTER_MAX_CLIENTS = 16             # 每个客户端一个线程，必须有上限
LOCAL_CASTER_QUEUE_SIZE = 256             # 每客户端待发帧上限，满了说明客户端不读数据
LOCAL_CASTER_CLIENT_TIMEOUT = 10          # 客户端握手阶段超时
LOCAL_CASTER_SERVER_NAME = '2RTK NTRIP Caster'

# ==================== 无线电数传透传 ====================
RADIO_QUEUE_SIZE = 512             # 待发帧上限，满了丢弃最旧（数传串口写慢时保护）
RADIO_RECONNECT_INTERVAL = 3.0     # 串口断线后的重连间隔（秒）
RADIO_FAIL_LOG_INTERVAL = 600      # 串口打不开时 ERROR 日志的最小间隔（秒）
RADIO_VALID_BAUDS = [115200, 921600, 460800, 230400, 57600, 38400, 19200, 9600]

# ==================== 重连退避（数据源与出口共用一套） ====================
# 为什么要有这张表：原先"重连间隔"是四五个常量各自为政，而且 `connect_to_host`
# 等函数上还挂着 `@retry(3)`，一次"失败"实际上被放大成 3 次敲门——想在调用方
# 调一个 5 秒根本调不准，最坏一轮能拖到 100 秒。2026-11-24 起统一为：
#   **底层建连只尝试一次，节奏全部由调用方的 Backoff 决定。**
#
# 网络类失败（连不上 / 握手超时 / RST）：从 2 秒起，逐档翻倍到 30 秒封顶。
# 首次给 2 秒而不是 0：对端刚重启完往往 1~2 秒就能接受连接，但也绝不允许
# 零延迟无限重连（那会把上游 caster 的"连接数限制"直接打满）。
NETWORK_BACKOFF = (2.0, 5.0, 10.0, 20.0, 30.0)
# 认证类失败（401 / 403 / 挂载点不存在 / User connection limit exceeded）：
# 重试再快也没用，只会刷日志、并在公共 caster 上触发封 IP。降频到分钟级。
AUTH_BACKOFF = (30.0, 60.0, 120.0, 300.0)
# 退避抖动比例：多台设备同时重启时错开敲门时刻，避免同步风暴。
BACKOFF_JITTER = 0.2

# 中继对端主动关闭（recv 返回空字节）的退避表。
# 这一档单独一张表，因为"接受连接后立刻关闭"是最常见的故障形态（同名源已在
# 线上、caster 内部报错），必须给它一个更长的最高档，否则就是毫秒级重连风暴。
RELAY_EOF_BACKOFF = (2.0, 5.0, 15.0, 30.0, 60.0)
# 中继连续多久没数据就判定这条连接已死（NTRIP 没有心跳，只能靠空闲兜底）
RELAY_IDLE_RECONNECT = 120

# 串口"读空"看门狗：某些 USB 转串口驱动在设备被拔掉后不会抛异常，只是
# read() 一直返回空字节。没有这个看门狗，程序会永远在 `sleep(0.2)` 里空转，
# 模块插回来也发现不了，现场只能重启进程。
SERIAL_EMPTY_WATCHDOG = 15.0

# ==================== 上传出口（云端 Caster） ====================
CASTER_QUEUE_SIZE = 512            # 待发帧上限，满了丢最旧（上传慢时保护内存）
CASTER_RECONNECT_INTERVAL = 5.0    # 兼容保留：旧版固定间隔，现由 NETWORK_BACKOFF 接管
# 上传会话最长存活时间：满这个时长主动断一次重连。
# 目的不是"修复"什么，而是规避半开连接——云端断电或 NAT 表老化时 TCP 不会发
# FIN/RST，而 RTCM 才 1~2 KB/s，内核发送缓冲能吞很久，sendall 迟迟不报错，
# 状态页会显示"正在推送"但云端一个字节都没收到。定期轮换是最便宜的解法。
CASTER_SESSION_MAX_AGE = 1800.0

UPLOAD_STATE_TEXT = {
    "idle": "未配置",
    "waiting_source": "等待数据源",
    "connecting": "连接中…",
    "authed": "已认证 · 正在推送",
    "retrying": "连接中断 · 重试中",
    "error": "认证失败",
}

# ==================== 中继挂载点列表（NTRIP SOURCETABLE） ====================
SOURCETABLE_TIMEOUT = 8.0                 # 连接 + 读取的总超时（秒）
SOURCETABLE_MAX_BYTES = 4 * 1024 * 1024   # 防止对端无限吐数据把内存吃满
SOURCETABLE_CACHE_TTL = 300               # 同一 Caster 5 分钟内重复请求走缓存
SOURCETABLE_MAX_ITEMS = 5000              # 列表上限：极少数公共 Caster 会挂上万个点，
                                          # 全部塞进 <datalist> 会让页面卡顿

# 主机名/域名/IPv4 校验：不允许斜杠、空格、冒号等会污染请求行的字符
SOURCETABLE_HOST_RE = r'^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252})$'

# ==================== 登录限速 ====================
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300

# ==================== Arduino/串口扫描 ====================
# 检测串口时依次尝试的波特率（顺序影响首次启动的扫描耗时）
SERIAL_SCAN_BAUDS = [115200, 460800, 230400, 57600, 38400, 19200, 9600, 76800, 4800, 2400, 1200]
# validate_config 里"不常见波特率"的告警基准
SERIAL_COMMON_BAUDS = [115200, 460800, 230400, 57600, 38400, 19200, 9600]
