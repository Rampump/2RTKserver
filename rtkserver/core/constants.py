# -*- coding: utf-8 -*-
"""全局常量。全部是不可变值，任何模块只读。

注意：这里不 import 任何项目内模块，避免出现「constants 依赖谁」的环。
"""
import os

# ==================== 程序标识 ====================
ART_LOGO = """
    ██████╗ ██████╗ ████████╗██╗  ██╗
    ╚════██╗██╔══██╗╚══██╔══╝██║ ██╔╝
     █████╔╝██████╗╔   ██║   █████╔╝ 
    ██╔═══╝ ██╔══██╗   ██║   ██║  ██╗
    ███████╗██║  ██║   ██║   ██║  ██╗
    ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝
         2RTK Ntrip Server 2.1.8
"""

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
RELAY_IDLE_RECONNECT = 120          # 中继连续无数据多久后重连

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
RADIO_VALID_BAUDS = [115200, 921600, 460800, 230400, 57600, 38400, 19200, 9600]

# ==================== 上传出口（云端 Caster） ====================
CASTER_QUEUE_SIZE = 512            # 待发帧上限，满了丢最旧（上传慢时保护内存）
CASTER_RECONNECT_INTERVAL = 5.0    # 断线重连基础间隔（秒）

UPLOAD_STATE_TEXT = {
    "idle": "未配置",
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
