# -*- coding: utf-8 -*-
"""共享状态池：全项目唯一的可变单例与三张注册表。

为什么需要这一层：
    拆包前所有模块共享 server.py 的模块级全局变量；拆包后如果各模块
    各自维护副本，就会出现「出口明明开了，状态页永远显示未运行」这类
    极难定位的问题。所以可变单例只在本模块存一份。

硬规则（务必遵守）：
    禁止 `from ..core.state import local_caster`。
    `from ... import` 在导入瞬间就把当时的 None 绑死在自己的模块命名空间里，
    之后主程序给 state.local_caster 赋值，对那个名字毫无影响。
    一律 `from ..core import state` 再用 `state.local_caster`。

本模块不 import 任何项目内模块，保证可以被任何人安全导入。
"""
import threading
from collections import deque

# ==================== 跨线程事件（必须全局单例） ====================
thread_exit_event = threading.Event()   # 单轮运行的数据源/出口退出信号
shutdown_event = threading.Event()      # 进程级退出信号（由 SIGTERM/SIGINT 置位）
restart_event = threading.Event()       # 「保存并重启」信号，只重启数据面

worker_thread = None                    # 当前数据源线程，用于防止重复推流

# ==================== RTCM 缓冲与解析结果 ====================
rtcm_buffer = bytearray()
buffer_lock = threading.Lock()
station_info = {}
msg_types_seen = set()
# 源列表里声明的消息类型必须"当前真的在推"。msg_types_seen 是进程级累加集合，
# 跑过中继（MSM7）再切回串口（MSM4）时不会清空，会把 1077/1087… 残留在 STR 行里误导移动站。
msg_types_recent = {}        # 消息类型 -> 最近一次观测时间

# ==================== WebSocket 监控连接 ====================
clients = []                 # list[WSClient]：每个监控连接一个待发队列
clients_lock = threading.Lock()  # 只保护 clients 列表本身，不再保护"发送"动作

# ==================== 出口单例 ====================
local_caster = None
local_caster_last_error = ''
serial_bridge = None
caster_uploader = None

# ==================== 旁路模块单例 ====================
announcer = None             # ZeroconfAnnouncer，未启用/依赖缺失时为 None

# ==================== 运行状态（首页卡片） ====================
run_status = {
    "mode": "",
    "data_source": "",
    "target_caster": "",
    "data_sent": "0B",
    "run_time": "0h 0m 0s"
}
run_status_lock = threading.Lock()

# 日志/print 历史环形缓冲，WS 首帧要回放给新连接
log_history = {
    'log': deque(maxlen=100),
    'print': deque(maxlen=100),
}

# ==================== 三张注册表（向下通知，不反向 import） ====================
# outlets: name -> {'publish': callable(bytes), 'snapshot': callable() -> dict}
# 由 outlets 包在导入时自注册；distribute() 只遍历这张表，不 import 具体出口模块。
outlets = {}

# snapshot_providers: name -> callable() -> dict
# 状态提供者注册表，Web 层只读这里，不直接 import 业务模块。
snapshot_providers = {}

# hooks: 旁路模块（mDNS 等）挂载点，由 lifecycle 在启动/退出时统一调用
hooks = {'startup': [], 'shutdown': []}

# publish: 日志/状态 → WS 的出口。由 web.app.create_app() 赋值为 ws.send_to_clients。
# 拆包前是直接调 send_to_clients；现在反向：底层只往这里丢，不知道 WS 存在。
publish = None


def broadcast(message):
    """把一条消息推给所有监控连接；未初始化或出错都不抛异常。

    底层（解析线程、日志 Handler）只调这个函数，不 import web 层。
    """
    fn = publish
    if fn is None:
        return
    try:
        fn(message)
    except Exception:
        pass


def reset_runtime_state():
    """清空单轮运行期的可变状态（重启数据面时用，不动事件对象与单例）。"""
    msg_types_seen.clear()
    msg_types_recent.clear()
    with buffer_lock:
        rtcm_buffer.clear()
    station_info.clear()


def iter_snapshots():
    """返回所有已注册状态提供者的快照 {name: dict}，单个失败不影响其它。"""
    result = {}
    for name, provider in list(snapshot_providers.items()):
        try:
            result[name] = provider()
        except Exception:
            result[name] = {}
    return result
