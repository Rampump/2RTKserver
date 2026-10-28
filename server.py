#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2RTK NTRIP Server
Copyright (C) 2024 文七 (i@jia.by)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import socket
import serial
import serial.tools.list_ports
import os
import sys
import platform
import logging
import time
import base64
from io import BytesIO
from functools import wraps
from pyrtcm import RTCMReader, parse_msm
import threading
import json
import math
from collections import defaultdict, deque
from flask import Flask, render_template, jsonify, request, redirect, url_for, session
from flask_sock import Sock
import sqlite3
import contextlib
import hashlib
import hmac
import secrets
import re
import queue
from logging.handlers import RotatingFileHandler
from typing import Optional, Tuple
from threading import Event

# 程序自身所在目录。所有落盘文件（配置库、日志、会话密钥）都以它为基准，
# 不能依赖"当前工作目录"——否则从别的目录执行 python /opt/2RTKserver/server.py
# 会新造一个空库，表现为"配置全丢了、又要重新配一遍"。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ART_LOGO = """
    ██████╗ ██████╗ ████████╗██╗  ██╗
    ╚════██╗██╔══██╗╚══██╔══╝██║ ██╔╝
     █████╔╝██████╗╔   ██║   █████╔╝ 
    ██╔═══╝ ██╔══██╗   ██║   ██║  ██╗
    ███████╗██║  ██║   ██║   ██║  ██╗
    ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝
         2RTK Ntrip Server 2.1.8
"""

rtcm_buffer = bytearray()
buffer_lock = threading.Lock()
station_info = {}
clients = []              # list[WSClient]：每个监控连接一个待发队列
clients_lock = threading.Lock()  # 只保护 clients 列表本身，不再保护"发送"动作
msg_types_seen = set()
# 源列表里声明的消息类型必须"当前真的在推"。msg_types_seen 是进程级累加集合，
# 跑过中继（MSM7）再切回串口（MSM4）时不会清空，会把 1077/1087… 残留在 STR 行里误导移动站。
msg_types_recent = {}        # 消息类型 -> 最近一次观测时间
RTCM_TYPE_WINDOW = 60        # 秒：超过该窗口没再出现的类型不写入源列表
shutdown_event = Event()
restart_event = Event()
thread_exit_event = Event()
worker_thread = None
MAX_BUFFER_SIZE = 10 * 1024 * 1024 

# 安全与网络参数
PBKDF2_ITERATIONS = 100000          # ARM 小板上的折中值，登录一次约 0.2~0.5 秒
PASSWORD_SCHEME = 'pbkdf2_sha256'
SENSITIVE_KEY_HINTS = ('password', 'passwd', 'secret')
NTRIP_HANDSHAKE_TIMEOUT = 10        # NTRIP 请求/响应阶段超时
CASTER_WRITE_TIMEOUT = 15           # 上传端写超时，用来尽早发现死连接
RELAY_READ_TIMEOUT = 20             # 中继端读超时，超时视为"本轮无数据"
RELAY_IDLE_RECONNECT = 120          # 中继连续无数据多久后重连
# 设为 1 时，未登录的访问者也能看到基准站精确坐标（旧行为）
PUBLIC_STATION_COORDS = os.environ.get('PUBLIC_STATION_COORDS', '0') == '1'

# WebSocket 监控广播参数
MAX_WS_CLIENTS = 32                 # 监控连接上限：每个连接占一个线程，必须有上限
WS_OUTBOX_MAXLEN = 32               # 每连接待发帧上限，满了丢最旧（不阻塞生产者）
WS_RECEIVE_POLL = 0.2               # 收包轮询间隔，同时也是"把队列发空"的最小周期
WS_DROP_LIMIT = 100                 # 连续丢帧超过该值说明客户端长期不消费，主动断开

# ==================== 本地 NTRIP Caster 参数 ====================
# 作用：把本机正在转发的那一路 RTCM 数据，同时以 NTRIP Caster 的身份对内网发布，
#       移动站/手簿直接连本机取差分，不必再绕到外网 Caster。
# 注意：这里是"再发一份"，不改变原有的上传链路——原来往哪上传现在还往哪上传。
LOCAL_CASTER_HOST = '0.0.0.0'             # 默认监听所有网卡
LOCAL_CASTER_PORT = 2101
LOCAL_CASTER_MOUNTPOINT = '2RTK'          # 唯一挂载点，固定
LOCAL_CASTER_USER = '2RTK'                # 固定账号
LOCAL_CASTER_PASSWORD = '2RTK'            # 固定密码
LOCAL_CASTER_MAX_CLIENTS = 16             # 每个客户端一个线程，必须有上限
LOCAL_CASTER_QUEUE_SIZE = 256             # 每客户端待发帧上限，满了说明客户端不读数据
LOCAL_CASTER_CLIENT_TIMEOUT = 10          # 客户端握手阶段超时
LOCAL_CASTER_SERVER_NAME = '2RTK NTRIP Caster'


class ThreadExitRequested(Exception):
    """收到退出/重启信号时，用于中断阻塞中的重试循环"""


class WebSocketLogHandler(logging.Handler):
    """WebSocket日志处理器，不影响原有日志处理"""
    def __init__(self):
        super().__init__()
        self.formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        self.history = deque(maxlen=100)  # 保留最近100条日志

    def emit(self, record):
        try:
            msg = self.format(record)
            self.history.append(msg)
            # 通过现有send_to_clients函数推送，不干扰原有逻辑
            send_to_clients({"type": "log", "message": msg})
        except Exception:
            self.handleError(record)

class PrintCapture:
    """捕获print输出，同时保留原有控制台输出"""
    def __init__(self, original_print):
        self.original_print = original_print
        self.history = deque(maxlen=100)

    def __call__(self, *args, **kwargs):
        # 保留原有控制台输出
        self.original_print(*args, **kwargs)
        # 同时捕获输出并推送到WebSocket
        try:
            msg = " ".join(map(str, args))
            self.history.append(msg)
            send_to_clients({"type": "print", "message": msg})
        except Exception:
            pass


def setup_logging():
    
    handlers = [
        logging.StreamHandler(),
        # 嵌入式设备存储有限，改为轮转日志，避免 server.log 无限增长
        RotatingFileHandler(
            os.path.join(BASE_DIR, 'server.log'), maxBytes=1024 * 1024, backupCount=3, encoding='utf-8'
        )
    ]
    
    # 添加WebSocket日志处理器
    ws_handler = WebSocketLogHandler()
    handlers.append(ws_handler)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        # force=True 必需：模块导入阶段（如生成会话密钥）若已触发过 logging，
        # root 会先被装上默认 StreamHandler，此时 basicConfig 是空操作，
        # 文件日志和 WebSocket 日志推送会静默失效。
        force=True
    )
    
    
    # 同一个 PrintCapture 实例既接管内置 print，又作为历史记录来源。
    # 原先这里是 PrintCapture(print).history 与 PrintCapture(builtins.print) 两个不同对象，
    # 导致 log_history['print'] 永远是空的历史。
    import builtins
    print_capture = PrintCapture(print)
    global log_history
    log_history = {
        'log': ws_handler.history,
        'print': print_capture.history
    }
    builtins.print = print_capture


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "localhost"


# ==================== 端口占用诊断 ====================
#
# 这里有个非常常见的误判，先写在代码里备查：
#   「中继 / 上传端填的 2101」是**对端端口**。本机出站时由内核分配一个临时端口
#   （比如 54321），本地根本不会 bind 2101。所以「远端 Caster 用 2101」和
#   「本地 Caster 监听 2101」并不冲突，两者可以同时存在。
#
#   真会报 EADDRINUSE(errno 98) 的只有三种：
#     1) 上一个进程没退干净，还占着 2101（systemd 重启、网页保存重启时最常见）
#     2) 本机别的程序占了 2101（str2str / 另一个实例 / 其它 NTRIP 软件）
#     3) 上传目标写成了本机地址 + 同一个端口（自环，见 check_self_loop）
#
#   下面几个函数就是把这三件事在日志和网页里说清楚，而不是只丢一句 errno 98。


def find_port_owner(port):
    """尽力找出监听指定 TCP 端口的进程，返回 'pid 1234 (str2str)' 或 None。

    只在 Linux 有效（读 /proc/net/tcp{,6} 取 socket inode，再反查 /proc/*/fd
    找到持有它的进程）。只在启动失败、保存校验这类低频路径调用。
    """
    if not sys.platform.startswith('linux'):
        return None
    inodes = set()
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(path, 'r') as f:
                next(f, None)  # 跳过表头
                for line in f:
                    fields = line.split()
                    # 表结构：sl local_address rem_address st ... 第 10 列为 inode
                    # local_address 形如 00000000:08A1（08A1 就是端口）；st == '0A' 表示 LISTEN
                    if len(fields) < 10 or fields[3] != '0A':
                        continue
                    try:
                        if int(fields[1].rsplit(':', 1)[1], 16) != int(port):
                            continue
                    except (IndexError, ValueError):
                        continue
                    inodes.add('socket:[%s]' % fields[9])
        except OSError:
            continue
    if not inodes:
        return None
    try:
        pids = os.listdir('/proc')
    except OSError:
        return None
    for pid in pids:
        if not pid.isdigit():
            continue
        fd_dir = '/proc/%s/fd' % pid
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # 权限不足或进程已退出，跳过
        for fd in fds:
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if link in inodes:
                try:
                    with open('/proc/%s/comm' % pid, 'r') as f:
                        name = f.read().strip()
                except OSError:
                    name = '?'
                return 'pid %s (%s)' % (pid, name)
    return None


def probe_tcp_port(host, port):
    """试一下 host:port 现在能不能监听。返回 (可用?, 说明)。

    不可用时说明里尽量带上占用者，例如「被 pid 1234 (str2str) 占用」。

    两个平台用的手段不同：
      Linux/POSIX —— 直接 bind。加 SO_REUSEADDR 是为了不把 TIME_WAIT 里的旧连接
                     误判成占用；真正有程序在 listen 时 bind 仍会 EADDRINUSE。
      Windows     —— bind 探活不可靠（SO_REUSEADDR 语义允许抢端口，bind 照样成功），
                     所以改成"连一下"：连得上就说明有程序在监听。
    """
    port = int(port)
    host = host or LOCAL_CASTER_HOST

    if os.name == 'nt':
        target = '127.0.0.1' if host in ('0.0.0.0', '::', '*') else host
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.3)
        try:
            probe.connect((target, port))
            return False, '被本机某个程序监听（端口已被占用）'
        except OSError:
            return True, ''
        finally:
            try:
                probe.close()
            except OSError:
                pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True, ''
    except OSError as e:
        owner = find_port_owner(port)
        return False, ('被 %s 占用' % owner) if owner else str(e)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def local_addresses():
    """本机可能被写进配置的"自己"的地址集合，用于自环判断。"""
    ips = {'127.0.0.1', 'localhost', '0.0.0.0', '::1'}
    try:
        ip = get_local_ip()
        if ip:
            ips.add(ip)
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        ips.add(hostname.lower())
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return ips


def check_self_loop(cfg):
    """检查「出口指向自己」这类必然跑不通的配置，返回一句人话或 None。

    上传：caster_host 是本机 + 端口等于本地 Caster 端口
          → SOURCE 请求会打到自己刚开的 Caster 上（它只认 GET），现象就是
            「连上了但一直 timeout」，非常像对端 Caster 有问题，实际是自环。
    中继：relay_host 是本机 + 端口等于本地 Caster 端口
          → 从自己身上拉流，原地打转。
    """
    if str(cfg.get('local_caster_enabled', '0')) != '1':
        return None
    try:
        lc_port = int(cfg.get('local_caster_port') or LOCAL_CASTER_PORT)
    except (TypeError, ValueError):
        return None
    ips = local_addresses()

    if str(cfg.get('caster_enabled', '0')) == '1':
        host = (cfg.get('caster_host') or '').strip().lower()
        try:
            cport = int(cfg.get('caster_port') or 0)
        except (TypeError, ValueError):
            cport = 0
        if host and host in ips and cport == lc_port:
            return (f"上传地址 {host}:{cport} 就是本机，且与本地 Caster 端口相同，"
                    f"数据会推给自己（表现为连上后一直超时）。"
                    f"请把上传地址改成远端 Caster，或把本地 Caster 端口改成别的值。")

    if cfg.get('mode') == 'relay':
        host = (cfg.get('relay_host') or '').strip().lower()
        try:
            rport = int(cfg.get('relay_port') or 0)
        except (TypeError, ValueError):
            rport = 0
        if host and host in ips and rport == lc_port:
            return (f"中继地址 {host}:{rport} 指向本机自己的 Caster，"
                    f"等于从自己身上拉流，收不到新数据。")
    return None


def port_conflict_message(cfg):
    """本地 Caster 监听端口与中继/上传端口重复时，返回一句提示，否则 None。

    为什么要求错开：中继/上传填的是**对端端口**，本机出站用内核临时端口，
    技术上可以和本地监听端口同为 2101 而不冲突。但同一个配置页里重复出现
    2101，极容易被误读成「本地 Caster 抢了出口的端口」，也会在对端地址被写成
    127.0.0.1 时直接变成自环。所以统一要求把本机监听端口错开。
    """
    if str(cfg.get('local_caster_enabled', '0')) != '1':
        return None
    try:
        lc = int(cfg.get('local_caster_port') or LOCAL_CASTER_PORT)
    except (TypeError, ValueError):
        return None

    same = []
    if str(cfg.get('caster_enabled', '0')) == '1':
        try:
            if int(cfg.get('caster_port') or 0) == lc:
                same.append('「上传到云端 Caster」')
        except (TypeError, ValueError):
            pass
    if cfg.get('mode') == 'relay':
        try:
            if int(cfg.get('relay_port') or 0) == lc:
                same.append('「中继取流」')
        except (TypeError, ValueError):
            pass

    if not same:
        return None
    return (f"本地 Caster 监听端口 {lc} 与 {' / '.join(same)} 的端口相同。"
            f"本地 Caster 会在本机真实占用 {lc}，请改成 2102 等未被占用的端口。")


run_status = {
    "mode": "",
    "data_source": "",
    "target_caster": "",
    "data_sent": "0B",
    "run_time": "0h 0m 0s"
}


def push_run_status():
    """推送运行状态，并顺带带上本地 Caster 的实时快照。

    客户端数会随时变化，所以不能只在启动时算一次；放在这里每 2 秒更新，
    首页那张"本地 Caster"卡片就能跟着动。
    """
    try:
        run_status["local_caster"] = local_caster_snapshot()
    except Exception:
        # 状态快照失败绝不能连累主推送链路
        pass
    try:
        run_status["upload"] = upload_snapshot()
    except Exception:
        pass
    try:
        run_status["radio"] = serial_bridge_snapshot()
    except Exception:
        pass
    send_to_clients({"run_status": run_status})


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

UPLOAD_STATE_TEXT = {
    "idle": "未配置",
    "connecting": "连接中…",
    "authed": "已认证 · 正在推送",
    "retrying": "连接中断 · 重试中",
    "error": "认证失败",
}


def upload_snapshot(cfg=None) -> dict:
    """首页展示用的上传链路快照。口令类字段一律不下发。

    状态来源优先级：运行中的 CasterUploader 实例 > 全局 upload_status 兜底。
    这样状态页读到的是真实推流线程的统计，而不是另外维护的一份影子数据。
    """
    if caster_uploader is not None:
        return caster_uploader.status()
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


def mark_upload_state(state: str, cfg=None, error: str = '', reconnect: bool = False):
    """更新上传端链路状态。state 见 UPLOAD_STATE_TEXT。"""
    global _upload_session_start
    upload_status['state'] = state
    upload_status['state_text'] = UPLOAD_STATE_TEXT.get(state, state)
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
    if state == 'authed':
        _upload_session_start = time.time()
    elif state in ('idle', 'connecting', 'retrying', 'error'):
        _upload_session_start = 0.0
    try:
        run_status['upload'] = upload_snapshot(cfg)
    except Exception:
        pass

CONFIG_DB = os.path.join(BASE_DIR, '2RTKserver.db')
SECRET_KEY_FILE = os.path.join(BASE_DIR, '.secret_key')
DEFAULT_CONFIG = {
    "mode": "none",
    "serial_port": "",
    "baud_rate": "",
    # 三个数据出口各自一个独立开关，默认全关：程序启动只做采集/监控，
    # 不连接任何外部服务。需要哪个出口由用户在配置页显式勾选。
    "caster_enabled": "0",
    "caster_host": "",
    "caster_port": "2101",
    "caster_mountpoint": "",
    "caster_password": "",
    "relay_host": "",
    "relay_port": "2101",
    "relay_mountpoint": "",
    "relay_user": "",
    "relay_password": "",
    "local_caster_enabled": "0",
    "local_caster_host": "0.0.0.0",
    "local_caster_port": "2101",
    "radio_enabled": "0",
    "radio_port": "",
    "radio_baud": "115200",
    "admin_user": "admin",
    "admin_password": "admin"
}

def init_db():
    """初始化数据库并写入默认配置（小写键名）"""
    with contextlib.closing(sqlite3.connect(CONFIG_DB, timeout=10)) as conn:
        # WAL 模式下读不阻塞写，多线程（Web + 后台推流）同时访问时不会互相卡住。
        # 该设置写进数据库文件本身，只需成功设置一次。
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.commit()

        cursor = conn.execute("SELECT COUNT(*) FROM settings")
        if cursor.fetchone()[0] == 0:
            default_items = [(k, v) for k, v in DEFAULT_CONFIG.items()]
            conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", default_items)
            conn.commit()
            logging.info("数据库已创建并写入默认配置")
        else:
            # 老库升级：后来新增的配置项（例如本地 Caster）在老库里根本不存在，
            # 用 INSERT OR IGNORE 只补缺失的键，不动任何已有值。
            missing = [(k, v) for k, v in DEFAULT_CONFIG.items()
                       if conn.execute("SELECT 1 FROM settings WHERE key = ?", (k,)).fetchone() is None]
            if missing:
                conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", missing)
                conn.commit()
                logging.info(f"配置库补齐 {len(missing)} 个新增项: {', '.join(k for k, _ in missing)}")

def get_db():
    # timeout=10：并发写时等待锁释放，而不是立刻抛 "database is locked"
    return sqlite3.connect(CONFIG_DB, timeout=10)

# ==================== 口令与会话密钥 ====================
def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 加盐哈希，格式：pbkdf2_sha256$轮数$盐hex$哈希hex"""
    if password is None:
        password = ''
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return f"{PASSWORD_SCHEME}${PBKDF2_ITERATIONS}${salt}${digest.hex()}"

def verify_password(password: str, stored: str) -> bool:
    """校验口令。兼容历史明文存储，便于迁移期平滑升级。"""
    if not stored:
        return False
    if password is None:
        return False
    if stored.startswith(PASSWORD_SCHEME + '$'):
        try:
            _, iterations, salt, expected = stored.split('$', 3)
            digest = hashlib.pbkdf2_hmac(
                'sha256', password.encode('utf-8'), bytes.fromhex(salt), int(iterations)
            )
            return hmac.compare_digest(digest.hex(), expected)
        except (ValueError, TypeError):
            return False
    # 历史明文口令（仅在尚未迁移时命中）
    return hmac.compare_digest(str(password), str(stored))

def migrate_admin_password():
    """把数据库里现存的明文管理员口令升级为哈希，避免明文落库"""
    try:
        cfg = get_config()
        stored = cfg.get('admin_password', '')
        if stored and not stored.startswith(PASSWORD_SCHEME + '$'):
            save_config({'admin_password': hash_password(stored)})
            logging.info("管理员口令已升级为哈希存储（原明文已覆盖）")
    except Exception as e:
        logging.error(f"管理员口令迁移失败: {e}")

def get_or_create_secret_key() -> str:
    """session 签名密钥：环境变量优先，否则首次启动随机生成并写入独立文件。

    不写进 2RTKserver.db —— 那个文件是被 Git 跟踪的，
    密钥一旦跟着提交上去，就等于又把固定密钥公开了一遍。
    """
    env_key = os.environ.get('SECRET_KEY')
    if env_key and len(env_key) >= 16:
        logging.info("使用环境变量 SECRET_KEY 作为会话签名密钥")
        return env_key
    try:
        if os.path.exists(SECRET_KEY_FILE):
            with open(SECRET_KEY_FILE, 'r', encoding='utf-8') as f:
                key = f.read().strip()
            if len(key) >= 32:
                return key
        key = secrets.token_hex(32)
        with open(SECRET_KEY_FILE, 'w', encoding='utf-8') as f:
            f.write(key)
        try:
            os.chmod(SECRET_KEY_FILE, 0o600)
        except OSError:
            pass
        logging.info(f"已生成新的会话签名密钥并写入 {SECRET_KEY_FILE}（权限 600）")
        return key
    except Exception as e:
        # 文件不可写时退化为进程内随机密钥：重启后已登录的会话会失效，但不会留下固定密钥
        logging.error(f"读写会话密钥文件失败，使用临时密钥（重启后需重新登录）: {e}")
        return secrets.token_hex(32)

def mask_config(cfg: dict) -> dict:
    """打日志前对口令类字段打码"""
    masked = {}
    for key, value in cfg.items():
        if any(hint in key.lower() for hint in SENSITIVE_KEY_HINTS) and value:
            masked[key] = '***'
        else:
            masked[key] = value
    return masked

def get_config():
    # 这里原先每次都调 init_db()：等于每个 HTTP 请求都新建一次连接、
    # 执行一次 CREATE TABLE IF NOT EXISTS 和 COUNT(*)、再提交一次事务。
    # 建表只在 main() 启动时做一次就够了。
    config = {}
    logging.debug("开始从数据库读取配置信息")
    with contextlib.closing(get_db()) as conn:
        cursor = conn.execute("SELECT key, value FROM settings")
        for key, value in cursor.fetchall():
            config[key.lower()] = value
    # 会话密钥不能出现在配置接口/模板里，否则等于把密钥交给前端
    config.pop('secret_key', None)
    logging.debug("配置信息读取完成")
    int_fields = ['caster_port', 'relay_port', 'baud_rate', 'local_caster_port', 'radio_baud']
    for field in int_fields:
        raw_val = config.get(field)
        if raw_val is None or raw_val == '':
            config[field] = 0
        else:
            try:
                config[field] = int(raw_val)                
            except (ValueError, TypeError):
                config[field] = 0
    return config

def run_once():
    """一轮完整运行：先启动数据出口，再启动数据源；停机严格逆序。

    这是整个程序的调度中心，职责边界很清楚：
      - 出口是否启动，只看各自的 _enabled 独立开关（与本函数里的 mode 无关）；
      - 数据源是否启动，只看 mode；
      - 任何模块启动失败都只记日志，不阻断其他模块，也不中断整体运行。
    """
    global worker_thread

    # 上一轮的工作线程若仍未退出（例如卡在阻塞的 recv 里），
    # 绝不能再起一个新线程：否则两个线程会同时向同一个挂载点推流、抢同一个串口。
    if worker_thread is not None and worker_thread.is_alive():
        logging.error("上一轮工作线程尚未退出，为避免重复推流暂缓启动新实例")
        thread_exit_event.set()
        time.sleep(1)
        return

    # 退出信号只在新一轮开始时清除（原先是放在 finally 里，会把还没退出的老线程"复活"）
    thread_exit_event.clear()
    # 换数据源后，上一轮的消息类型不能带进来，否则源列表会描述已不存在的数据
    msg_types_seen.clear()
    msg_types_recent.clear()

    ser = None
    relay = None
    source_thread = None

    try:
        cfg = get_config()
        if not validate_config(cfg):
            logging.error("监测到配置信息无效，将于5秒后重新读取")
            time.sleep(5)
            return

        mode = cfg['mode']

        # ==================== 阶段 2：启动数据出口（先起，等着接数据）====================
        # 顺序：本地 Caster（开始 listen）→ 数传（开串口待命）→ 上传（建连 + 握手）
        # 三者各自独立线程；任何一个启动失败只记日志，绝不影响另外两个，也不影响数据源。

        if str(cfg.get('local_caster_enabled', '0')) == '1':
            start_local_caster(cfg)
        else:
            stop_local_caster()

        if str(cfg.get('radio_enabled', '0')) == '1':
            start_serial_bridge(cfg)
        else:
            stop_serial_bridge()

        # 上传出口是独立线程 + 队列：建连失败会在后台一直重试，
        # 绝不会把数据源堵在这里（这正是本次改造要根治的老问题）。
        if str(cfg.get('caster_enabled', '0')) == '1':
            start_caster_uploader(cfg)
        else:
            stop_caster_uploader()
            mark_upload_state('idle', cfg, error='')

        logging.info(f"数据出口就绪：{describe_targets(cfg)}")

        # ==================== 阶段 3：启动数据源（后起，一起来就有数据）====================
        if mode == 'serial':
            logging.info("进入串口模式")
            port, baud = detect_serial(cfg)
            if not (port and baud):
                logging.warning("未检测到有效串口，5秒后重试")
                time.sleep(5)
                return
            cfg['serial_port'], cfg['baud_rate'] = port, baud
            try:
                ser = serial.Serial(port=port, baudrate=baud, timeout=1)
            except Exception as e:
                logging.error(f"打开串口失败: {e}")
                time.sleep(5)
                return

            source_thread = threading.Thread(
                target=upload_via_serial,
                args=(ser, cfg, thread_exit_event),
                daemon=True
            )
            source_thread.start()
            worker_thread = source_thread

        elif mode == 'relay':
            logging.info("进入中继模式")
            try:
                relay = open_relay(cfg)
            except Exception as e:
                logging.error(f"打开中继连接失败: {e}")
                time.sleep(5)
                return

            source_thread = threading.Thread(
                target=forward_relay,
                args=(relay, cfg, thread_exit_event),
                daemon=True
            )
            source_thread.start()
            worker_thread = source_thread

        else:
            logging.error(f"未知模式: {mode}")
            time.sleep(5)
            return

        # ==================== 阶段 4：等待重启 / 退出信号 ====================
        while not restart_event.is_set() and not shutdown_event.is_set():
            time.sleep(1)

    except ThreadExitRequested:
        logging.info("检测到重启/退出信号，提前结束本轮运行")
    except Exception as e:
        logging.exception(f"运行主循环run_once时发生未捕获异常: {e}")
        time.sleep(5)

    finally:
        # ==================== 停机：严格按启动的逆序 ====================
        # 1) 先停数据源：不再产生新数据，避免"源还在推、出口已关"的报错
        thread_exit_event.set()
        if source_thread is not None and source_thread.is_alive():
            source_thread.join(5)
        if source_thread is None or not source_thread.is_alive():
            worker_thread = None
        else:
            # 关键：不再无条件清除退出信号。老线程会在下一次循环检查时看到信号并自行退出；
            # 只要它还活着，worker_thread 就保留，下一轮 run_once 会被守卫拦住。
            logging.warning("数据源线程未在超时时间内退出，将继续等待其自行结束")

        # 2) 再停出口：上传 → 数传 → 本地 Caster
        #    本地 Caster 最后停，让内网客户端尽量晚一刻断流。
        stop_caster_uploader()
        stop_serial_bridge()
        stop_local_caster()

        # 3) 关闭数据源自己持有的句柄
        if ser is not None:
            try:
                ser.close()
            except Exception as e:
                logging.error(f"关闭串口时出错: {e}")
        if relay is not None:
            try:
                relay.close()
            except Exception as e:
                logging.error(f"关闭中继连接时出错: {e}")

        if restart_event.is_set():
            logging.info("接收到重启指令，等待所有资源关闭后重新加载配置")
            restart_event.clear()


def save_config(config: dict):
    """保存配置到数据库"""
    with contextlib.closing(get_db()) as conn:
        existing = {}
        cursor = conn.execute("SELECT key, value FROM settings")
        for key, value in cursor.fetchall():
            existing[key.lower()] = value

        for key, value in config.items():
            key = key.lower()
            if key in {'baud_rate', 'serial_port'} and value is None:
                value = ''
            else:
                value = str(value)

            old_value = existing.get(key)

            if old_value == value:
                continue  

            if key in existing:
                conn.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
            else:
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

        conn.commit()   

def reset_config_to_default():
    defaults = DEFAULT_CONFIG.copy()
    # 重置也要写哈希，避免重置后数据库里又出现明文口令
    defaults['admin_password'] = hash_password(DEFAULT_CONFIG['admin_password'])
    save_config(defaults)
    logging.info("配置已重置为默认值")

# 工具函数
def interruptible_sleep(seconds: float, event=None) -> bool:
    """可被退出信号打断的 sleep。返回 True 表示期间收到了退出信号。"""
    event = event if event is not None else thread_exit_event
    end_time = time.time() + max(0.0, seconds)
    while time.time() < end_time:
        if event.is_set():
            return True
        time.sleep(min(0.2, max(0.0, end_time - time.time())))
    return event.is_set()

def retry(max_retries: int, delay: int = 5, respect_exit: bool = True):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                # 重试循环必须能被重启指令打断，否则一次重连会阻塞数分钟，
                # 用户在页面上点"保存重启"后要等很久才生效。
                if respect_exit and thread_exit_event.is_set():
                    raise ThreadExitRequested()
                try:
                    return func(*args, **kwargs)
                except ThreadExitRequested:
                    raise
                except Exception as e:
                    if attempt < max_retries - 1:
                        if interruptible_sleep(delay):
                            raise ThreadExitRequested()
                    else:
                        raise
        return wrapper
    return decorator

def format_bytes(num_bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f}PB"

def format_duration(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}h {m}m {s}s"

def ecef_to_latlon(x, y, z):
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = 2 * f - f * f

    r = math.sqrt(x*x + y*y)
    z_val = z
    for _ in range(100):
        z_old = z_val
        sinp = z_val / math.sqrt(r*r + z_val*z_val)
        v = a / math.sqrt(1 - e2 * sinp*sinp)
        z_val = z + e2 * v * sinp
        if abs(z_val - z_old) < 1e-4:
            break
    lat = math.atan2(z_val, r) if r > 1e-12 else (math.pi/2 if z > 0 else -math.pi/2)
    lon = math.atan2(y, x)
    height = math.sqrt(r*r + z_val*z_val) - a / math.sqrt(1 - e2 * math.sin(lat)**2)

    return math.degrees(lat), math.degrees(lon), height

def clear_screen():
    # 在 systemd 下 stdout 不是 TTY，clear 只会把 ANSI 转义序列原样写进 journald
    # （日志里出现大量乱码），而且每 100 秒多 fork 一个进程。非交互环境直接跳过。
    try:
        if not sys.stdout.isatty():
            return
    except Exception:
        return
    system = platform.system()
    if system == "Windows":
        os.system('cls')
    elif system in ["Linux", "Darwin"]: 
        os.system('clear')
    else:
        print("不支持的操作系统，无法清理屏幕。")

# ==================== CRC24Q（RTCM3 帧校验） ====================
CRC24Q_POLY = 0x1864CFB
CRC24Q_TABLE = []
for _i in range(256):
    _crc = _i << 16
    for _ in range(8):
        _crc <<= 1
        if _crc & 0x1000000:
            _crc ^= CRC24Q_POLY
    CRC24Q_TABLE.append(_crc & 0xFFFFFF)

def crc24q(data: bytes) -> int:
    """计算 RTCM3 / GPS 使用的 CRC-24Q"""
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ CRC24Q_TABLE[((crc >> 16) ^ byte) & 0xFF]
    return crc

# 从二进制数据中提取完整的RTCM帧
def extract_rtcm_frames(data: bytes):
    frames = []
    index = 0
    data_len = len(data)

    while index < data_len:
        if data[index] == 0xD3 and index + 2 < data_len:
            length = ((data[index+1] & 0x03) << 8) | data[index+2]
            frame_length = 3 + length + 3
            if index + frame_length <= data_len:
                frame = data[index:index+frame_length]
                # 校验 CRC24Q：载荷里偶然出现的 0xD3 会被误判成帧头，
                # 不校验就会把后续合法帧整段吞掉，并让缓冲区一直堆积。
                if crc24q(frame[:-3]) != int.from_bytes(frame[-3:], 'big'):
                    logging.debug("RTCM 帧 CRC 校验失败，跳过一个字节重新同步")
                    index += 1
                    continue
                frames.append(frame)
                index += frame_length
            else:
                break
        else:
            index += 1
    return frames, data[index:]

# 配置处理，包括日志设置、读取配置、验证配置等
_validate_log_state = {'last': 0.0, 'msg': None}

def _log_validate_problem(level: str, message: str):
    """配置校验失败的提示，按"同一问题 60 秒一次"节流。

    validate_config 在未配置状态下会被 main() 每 10 秒调用一次，
    每次都在函数内部直接 logging.error 的话，一天约 1.6MB，
    很快就把 1MB×4 的轮转额度写满，真正需要排查的日志反而被冲掉。
    """
    now = time.time()
    if message == _validate_log_state['msg'] and now - _validate_log_state['last'] < 60:
        return
    _validate_log_state['msg'] = message
    _validate_log_state['last'] = now
    if level == 'warning':
        logging.warning(message)
    else:
        logging.error(message)

def validate_config(cfg):
    # 这个函数每 10 秒会被调用一次，所以内部日志改走 60 秒节流的包装
    def err(message):
        _log_validate_problem('error', message)

    def warn(message):
        _log_validate_problem('warning', message)

    # ========== 第一层：数据源（三选一） ==========
    # 三个数据出口各自一个独立开关，只有「已勾选」的出口才校验其必填项；
    # 未勾选的字段即使数据库里残留着历史值，也一律不校验、不连接。
    mode = cfg.get('mode', 'none') or 'none'
    caster_on = str(cfg.get('caster_enabled', '0')) == '1'
    local_caster_on = str(cfg.get('local_caster_enabled', '0')) == '1'
    radio_on = str(cfg.get('radio_enabled', '0')) == '1'

    if mode == 'none':
        # 这不是错误：用户还没在配置页选数据源。程序保持运行（网页/改密仍可用），
        # 只是不往下启动任何数据模块，安静等待配置。
        _log_validate_problem('warning', '尚未选择数据源（串口 / 中继），等待在 Web 配置页完成配置')
        return False
    if mode not in ('serial', 'relay'):
        err(f"无效的运行模式: {mode}，请选择串口或中继模式")
        return False

    # ========== 第二层：数据出口（勾了谁才校验谁） ==========
    # 出口 1：上传到云端 NTRIP Caster
    if caster_on:
        if not (cfg.get('caster_host') or '').strip():
            err("已勾选「上传到云端 Caster」，请填写上传地址")
            return False
        if not (cfg.get('caster_mountpoint') or '').strip():
            err("已勾选「上传到云端 Caster」，请填写挂载点")
            return False
        try:
            p = int(cfg.get('caster_port') or 0)
            if p <= 0 or p > 65535:
                raise ValueError
        except (ValueError, TypeError):
            err("上传 Caster 端口号无效，必须是1-65535之间的整数")
            return False

    # 出口 2：本地 NTRIP Caster
    if local_caster_on:
        try:
            p = int(cfg.get('local_caster_port') or 0)
            if p <= 0 or p > 65535:
                raise ValueError
        except (ValueError, TypeError):
            err("本地 Caster 端口号无效，必须是1-65535之间的整数")
            return False
        if not (cfg.get('local_caster_host') or '').strip():
            err("本地 Caster 监听地址未配置，可填 0.0.0.0 表示所有网卡")
            return False
        # 端口被别的进程占着就提前说清楚是谁占的。这里只告警不拦截：
        # 重启时上一轮实例可能还在退出的尾巴上，硬拦会把可自愈的情况变成启动失败
        # （真正的绑定在 LocalCaster.start() 里带 3 次退避重试）。
        lc_host = (cfg.get('local_caster_host') or '').strip() or LOCAL_CASTER_HOST
        lc_port = int(cfg.get('local_caster_port'))
        ours = (local_caster is not None and local_caster.port == lc_port)
        if not ours:
            ok, detail = probe_tcp_port(lc_host, lc_port)
            if not ok:
                warn(f"本地 Caster 端口 {lc_port} 当前不可用（{detail}）；"
                     f"若监听失败，请改用其他端口或结束占用该端口的进程")

    # 出口 3：无线电数传透传
    if radio_on:
        if not (cfg.get('radio_port') or '').strip():
            err("已勾选「无线电数传」，请填写数传串口")
            return False
        try:
            if int(cfg.get('radio_baud') or 0) <= 0:
                raise ValueError
        except (ValueError, TypeError):
            err("数传波特率无效，必须是正整数")
            return False

    # 自环：出口指向本机自己，跑起来一定是「连上了但没数据」，必须明确拦住
    loop = check_self_loop(cfg)
    if loop:
        err(loop)
        return False

    # 端口重复只告警不拦截：老库里可能就是这种组合，硬拦会让程序直接停在
    # 「等待配置」不再干活。真正的保存拦截在配置页提交时做。
    dup = port_conflict_message(cfg)
    if dup:
        warn(dup + "（技术上不冲突，因为中继/上传用的是对端端口；但建议错开，避免混淆）")

    # 零出口：允许运行——数据仍会被解析并显示在监控页（看星数、星历等），只是不向外分发
    if not (caster_on or local_caster_on or radio_on):
        warn("未勾选任何数据出口，数据只用于本地监控显示，不会向外分发")

    # 3. 根据不同模式检查专属必填参数
    if mode == 'serial':
        # 串口模式：波特率可以自动检测，因此不强制检查，但建议提示
        if not cfg.get('serial_port'):
            warn("串口端口未配置，程序将尝试自动检测")
        # 波特率若配置则验证有效性
        if cfg.get('baud_rate'):
            try:
                baud = int(cfg.get('baud_rate'))
                valid_bauds = [115200, 460800, 230400, 57600, 38400, 19200, 9600]
                if baud not in valid_bauds:
                    warn(f"波特率{baud}不常见，可能导致连接问题")
            except (ValueError, TypeError):
                err("波特率必须是有效的整数")
                return False

    elif mode == 'relay':
        # 中继模式：必须检查中继服务器的配置
        if not cfg.get('relay_host'):
            err("中继服务器地址未配置，请补充")
            return False
        if not cfg.get('relay_mountpoint'):
            err("中继服务器挂载点未配置，请补充")
            return False
        try:
            if int(cfg.get('relay_port', 0)) <= 0 or int(cfg.get('relay_port', 0)) > 65535:
                err("中继服务器端口号无效，必须是1-65535之间的整数")
                return False
        except (ValueError, TypeError):
            err("中继服务器端口号必须是有效的整数")
            return False

    # 所有检查通过
    return True


def detect_serial(cfg: dict) -> Tuple[Optional[str], Optional[int]]:
    """检测可用的RTK串口设备，优先使用配置文件中的设置"""
    port = cfg.get('serial_port', '')
    baud = cfg.get('baud_rate', '')

    valid_bauds = [115200, 460800, 230400, 57600, 38400, 19200, 9600, 76800, 4800, 2400, 1200]

    if port and baud and baud > 0:
        logging.info(f"尝试配置的串口: {port} @ {baud}bps")
        if test_serial(port, baud, check_rtcm=True):
            logging.info(f"使用配置的串口: {port} @ {baud}bps")
            return port, baud

    logging.warning("开始自动扫描串口设备...")
    available_ports = [p.device for p in serial.tools.list_ports.comports()]
    if not available_ports:
        logging.error("未发现任何串口设备")
        return None, None

    logging.info(f"发现 {len(available_ports)} 个串口: {', '.join(available_ports)}")
    for current_port in available_ports:
        for current_baud in valid_bauds:
            # 扫描可能持续较久，收到重启/退出信号要立刻放弃
            if thread_exit_event.is_set():
                raise ThreadExitRequested()
            try:
                if test_serial(current_port, current_baud, check_rtcm=True):
                    logging.info(f"发现RTK模块: {current_port} @ {current_baud}bps")
                    save_serial_config(current_port, current_baud)
                    return current_port, current_baud
            except serial.SerialException as e:
                logging.debug(f"串口 {current_port} @ {current_baud}bps 不可用: {str(e)}")

    logging.error("未检测到RTK模块，请检查设备连接")
    return None, None

@retry(max_retries=2, delay=1)
def test_serial(port: str, baud: int, check_rtcm: bool = True) -> bool:
    """测试串口是否输出RTCM数据"""
    try:
        with serial.Serial(port, baud, timeout=2) as ser:
            ser.reset_input_buffer()
            start_time = time.time()
            rtcm_found = False
            
            while time.time() - start_time < 2:
                # 单档波特率探测从 5 秒收紧到 2 秒：
                # RTCM 一般 1Hz，2 秒足以判定；否则 4 个串口 × 11 档波特率
                # 最坏要扫 220 秒以上，启动时表现为"卡死"。
                if thread_exit_event.is_set():
                    raise ThreadExitRequested()
                data = ser.read(max(ser.in_waiting, 1024))
                if not data:
                    continue
                if b'\xd3' in data:
                    frames, _ = extract_rtcm_frames(data)
                    if frames:
                        rtcm_found = True
                        if check_rtcm:
                            logging.debug(f"在 {port} @ {baud}bps 检测到有效RTCM数据")
                            return True
                
                if not check_rtcm:
                    logging.debug(f"在 {port} @ {baud}bps 检测到有效数据")
                    return True
            
            return rtcm_found
    except serial.SerialException:
        return False

def save_serial_config(port: str, baud: int):
    """保存串口配置到数据库"""
    try:
        with contextlib.closing(sqlite3.connect(CONFIG_DB)) as conn:
           
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('serial_port', ?)", (port,))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('baud_rate', ?)", (str(baud),))
            conn.commit()
            
        logging.info(f"已保存串口配置到数据库: {port} @ {baud}bps")
    except Exception as e:
        logging.error(f"保存串口配置失败: {str(e)}")

# NTRIP通信，连接主机、构建请求头、进行握手等
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

# 服务器Caster连接，打开Caster连接@retry(max_retries=3, delay=5)
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
    # 判定只看独立开关，不看"地址是否为空"：
    # 地址是用户填过的历史配置，不代表他这次想启用上传。未勾选就绝不去连。
    if str(cfg.get('caster_enabled', '0')) != '1':
        logging.info("未勾选「上传到云端 Caster」，数据只走本地出口")
        mark_upload_state('idle', cfg, error='')
        return _NullCaster()
    logging.info(f">>>向上传端NTRIP Caster 申请连接{cfg['caster_host']}:{cfg['caster_port']}")
    mark_upload_state('connecting', cfg)
    try:
        sock = connect_to_host(cfg['caster_host'], int(cfg['caster_port']))
    except Exception as e:
        mark_upload_state('retrying', cfg, error=str(e))
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
        mark_upload_state('error', cfg, error=f"认证失败: {resp.splitlines()[:1]}")
        raise RuntimeError('上传端 NTRIP Caster 认证失败，请检查密码及配置信息')
    sock.settimeout(CASTER_WRITE_TIMEOUT)
    logging.info('上传端NTRIP Caster 认证通过开始发送RTCM数据')
    _reset_upload_stats()
    mark_upload_state('authed', cfg, error='')
    return sock

# 数据处理，跟踪消息频率
class FrequencyTracker:
    def __init__(self, window_size=180):
        self.window_size = window_size
        self.msg_timestamps = defaultdict(deque)

    def add_message(self, msg_type):
        current_time = time.time()
        self.msg_timestamps[msg_type].append(current_time)
        self._prune_old_timestamps(msg_type, current_time)

    def get_all_frequencies(self):
        current_time = time.time()
        frequencies = {}
        for msg_type, timestamps in self.msg_timestamps.items():
            self._prune_old_timestamps(msg_type, current_time)
            freq = len(timestamps) // self.window_size
            frequencies[msg_type] = max(1, freq)
        return frequencies

    def _prune_old_timestamps(self, msg_type, current_time):
        cutoff = current_time - self.window_size
        timestamps = self.msg_timestamps[msg_type]
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

frequency_tracker = FrequencyTracker(window_size=30)

# Web服务，创建Flask应用、处理路由和WebSocket连接
app = Flask(__name__)
# 原先是硬编码常量，任何人拿到源码就能自签 session cookie 冒充管理员。
# 改为：环境变量 SECRET_KEY 优先，否则首次启动随机生成并持久化到数据库。
app.secret_key = get_or_create_secret_key()
# 会话 Cookie 加固：HttpOnly 防脚本读取（Flask 默认已开），SameSite=Lax 让
# 跨站发起的 POST 带不上会话，等于给没有 CSRF token 的表单加了一道兜底。
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
sock = Sock(app)

# 本地 Caster 的固定挂载点/账号注入模板，避免模板里再硬编码一份、日后对不上
app.jinja_env.globals.update(
    local_caster_mountpoint=LOCAL_CASTER_MOUNTPOINT,
    local_caster_user=LOCAL_CASTER_USER,
    local_caster_password=LOCAL_CASTER_PASSWORD,
)

# 登录验证装饰器
def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            # 接口请求不能重定向到登录页，原因有两个：
            #   1. 前端 fetch 拿到的是登录页 HTML，JSON 解析必然失败，还多一次白跑；
            #   2. 更麻烦的是下面那句 session['next_url'] = request.path 会把
            #      /api/load_config 记下来，用户随后登录就被弹到一个裸 JSON 页面。
            # 首页在未登录时就会 fetch 这个接口，等于每次登录都会踩一遍。
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': '未登录或登录已过期'}), 401
            session['next_url'] = request.path
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    return wrapper


def _safe_next_url(next_url):
    """登录后的跳转目标必须是站内页面。

    挡两类：`//evil.com` 这种协议相对地址（开放重定向），以及 /api/* 接口路径
    （跳过去只会看到一串 JSON）。
    """
    if not next_url:
        return None
    if not next_url.startswith('/') or next_url.startswith('//'):
        return None
    if next_url.startswith('/api/'):
        return None
    return next_url

# 登录失败限速：原先可以无限次尝试，配合弱口令存在被爆破的可能。
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300

_login_failures = {}          # ip -> (连续失败次数, 最后一次失败时间)
_login_failures_lock = threading.Lock()

def _login_locked_seconds(ip: str) -> int:
    """返回还需等待的秒数，0 表示可以尝试登录。"""
    with _login_failures_lock:
        record = _login_failures.get(ip)
        if not record:
            return 0
        count, last_time = record
        if count < LOGIN_MAX_FAILURES:
            return 0
        remain = LOGIN_LOCKOUT_SECONDS - (time.time() - last_time)
        if remain <= 0:
            _login_failures.pop(ip, None)
            return 0
        return int(remain) + 1

def _record_login_failure(ip: str):
    with _login_failures_lock:
        # 简单的容量保护，避免被大量伪造源地址撑爆内存
        if len(_login_failures) > 1000:
            _login_failures.clear()
        count, _ = _login_failures.get(ip, (0, 0.0))
        _login_failures[ip] = (count + 1, time.time())

def _clear_login_failures(ip: str):
    with _login_failures_lock:
        _login_failures.pop(ip, None)

@app.after_request
def _set_security_headers(resp):
    """补几个基础安全响应头。没上 CSP：模板里有内联脚本和 CDN 样式，加严格的
    script-src 会直接把页面打挂，需要连模板一起重构才有意义。"""
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    # 注意：这里必须是 strict-origin-when-cross-origin，不能是 no-referrer。
    # OpenStreetMap 瓦片使用政策强制要求请求携带 Referer（用于识别来源站点），
    # 一旦页面声明 no-referrer，浏览器请求 tile.openstreetmap.org 时不发 Referer，
    # 瓦片会被直接拒绝（403 / "Referer is required"），地图整片空白。
    # strict-origin-when-cross-origin 只把"源"（协议+域名+端口）发给跨域站点，
    # 不泄露内网路径，同时满足 OSM 政策。
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    return resp

@app.route('/login', methods=['GET', 'POST'])
def login():
    # 检查用户是否已经登录
    if session.get('logged_in'):
        return redirect(url_for('config_page'))  

    if request.method == 'POST':
        client_ip = (request.remote_addr or 'unknown')
        locked = _login_locked_seconds(client_ip)
        if locked > 0:
            logging.warning(f"登录已被限速，来自 {client_ip} 的请求被拒绝（还需等待 {locked} 秒）")
            return render_template(
                'login.html',
                error=f'登录失败次数过多，请 {locked} 秒后再试。'
            ), 429

        cfg = get_config()
        username = request.form.get('username')
        password = request.form.get('password')
        # 口令以 PBKDF2 哈希存储；verify_password 内部用 compare_digest 做常量时间比较
        if username == cfg['admin_user'] and verify_password(password, cfg['admin_password']):
            _clear_login_failures(client_ip)
            session['logged_in'] = True
            session.permanent = False
            next_url = _safe_next_url(session.pop('next_url', None))
            if next_url:
                return redirect(next_url)
            return redirect(url_for('config_page'))  
        _record_login_failure(client_ip)
        return render_template('login.html', error='用户名或密码错误，请重试。')

    return render_template('login.html')

# 退出登录。原项目根本没有这个路由：登录后只能靠关浏览器或等 Session 过期，
# 共用设备时下一个人直接继承管理员权限。用 POST 而不是 GET，避免浏览器/杀毒软件
# 的链接预取把会话提前踢掉。
@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('index'))

# 配置页面
@app.route('/config', methods=['GET', 'POST'])
@login_required
def config_page():
    cfg = get_config()
    message = None
    message_type = None

    if request.method == 'POST':
        if 'save_config' in request.form or 'restart_program' in request.form:
            form_data = request.form.to_dict()
            mode = form_data.get('mode')

            # 本地 Caster 端口先校验，避免"保存成功但程序起不来"这种含糊状态
            if form_data.get('local_caster_enabled'):
                try:
                    lc_port = int((form_data.get('local_caster_port') or '').strip())
                    if not (1 <= lc_port <= 65535):
                        raise ValueError
                except (TypeError, ValueError):
                    return render_template(
                        'config.html', config=get_config(),
                        message='本地 Caster 端口无效，应为 1-65535 的整数，配置未保存。',
                        message_type='error'
                    )
                # 端口被别的进程占着，存进数据库也是白存：重启后必然 bind 失败，
                # 网页上只留一句"本地 Caster 启动失败"，很难定位。这里直接拦住，
                # 并且说明是谁占的（Linux 上能给出 pid 和进程名）。
                lc_host = (form_data.get('local_caster_host') or '').strip() or LOCAL_CASTER_HOST
                ours = (local_caster is not None and local_caster.port == lc_port)
                if not ours:
                    ok, detail = probe_tcp_port(lc_host, lc_port)
                    if not ok:
                        return render_template(
                            'config.html', config=get_config(),
                            message=f'本地 Caster 端口 {lc_port} 现在不可用（{detail}），'
                                    f'配置未保存。请改成其他端口，或先结束占用该端口的进程。'
                                    f'（中继/上传端填的 2101 是对端端口，不会占用本机 2101）',
                            message_type='error'
                        )

            new_cfg = {"mode": mode}

            # ===== 三个数据出口与运行模式完全解耦 =====
            # 任何模式下这三个开关都独立生效：勾了才启动，不勾就绝不连接。
            # 出口 1：上传到云端 NTRIP Caster
            new_cfg.update({
                "caster_enabled": "1" if form_data.get('caster_enabled') else "0",
                "caster_host": (form_data.get('caster_host') or '').strip(),
                "caster_port": (form_data.get('caster_port') or '').strip() or '2101',
                "caster_mountpoint": (form_data.get('caster_mountpoint') or '').strip(),
                "caster_password": form_data.get('caster_password', ''),
            })

            # 出口 2：本地 NTRIP Caster
            new_cfg.update({
                "local_caster_enabled": "1" if form_data.get('local_caster_enabled') else "0",
                "local_caster_host": (form_data.get('local_caster_host') or '').strip() or '0.0.0.0',
                "local_caster_port": (form_data.get('local_caster_port') or '').strip() or '2101',
            })

            # 出口 3：无线电数传透传
            radio_baud = (form_data.get('radio_baud') or '').strip() or '115200'
            new_cfg.update({
                "radio_enabled": "1" if form_data.get('radio_enabled') else "0",
                "radio_port": (form_data.get('radio_port') or '').strip(),
                "radio_baud": radio_baud,
            })

            # ===== 数据源：只有当前模式对应的字段才保存 =====
            if mode == 'serial':
                new_cfg.update({
                    "serial_port": (form_data.get('serial_port') or '').strip(),
                    "baud_rate": (form_data.get('baud_rate') or '').strip(),
                })
            elif mode == 'relay':
                new_cfg.update({
                    "relay_host": (form_data.get('relay_host') or '').strip(),
                    "relay_port": (form_data.get('relay_port') or '').strip() or '2101',
                    "relay_mountpoint": (form_data.get('relay_mountpoint') or '').strip(),
                    "relay_user": (form_data.get('relay_user') or '').strip(),
                    "relay_password": form_data.get('relay_password', ''),
                })

            # 前端已经拦过一次，这里再拦一次，防止直接构造 POST 绕过
            dup = port_conflict_message(new_cfg)
            if dup:
                return render_template(
                    'config.html', config=get_config(),
                    message=dup + '配置未保存。', message_type='error'
                )

            logging.info(f"保存的配置信息: {mask_config(new_cfg)}")
            save_config(new_cfg)

            message = "配置已保存成功！"
            message_type = "success"

            if 'restart_program' in request.form:
                restart_event.set()
                message = "配置已保存，程序将重新启动！"
                return redirect(url_for('index'))

    config = get_config()
    return render_template('config.html', config=config, message=message, message_type=message_type)

# ==================== 配置重置（不提供 HTTP 接口） ====================
# 历史沿革：
#   1. 原实现 /reset_config 完全没有鉴权，局域网内任何人都能把管理员重置成 admin/admin。
#   2. 曾改为「已登录 或 来自本机」放行，但 request.remote_addr 是 TCP 对端地址：
#      一旦按 README 建议在前面挂 Nginx，所有外网请求的对端都是 127.0.0.1，
#      "本机"判断恒为真，鉴权会被整体绕过。
#   3. 现在改为完全不提供 Web 重置入口 —— 因为本设备是内网部署，恢复操作走 SSH 更可靠，
#      而且没有"客户端地址判断"这层逻辑，就不存在代理绕过问题。
#      恢复方式：SSH 登录设备后执行
#          /opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py
# 说明：这里删除了 @app.route('/reset_config') 路由，唯一调用方是登录页的
#       resetDefaults()（已同步改为提示文案），仓库内无其他引用。

# 重启：必须有登录态。原先无鉴权，任何人都能远程反复重启造成拒绝服务。
@app.route('/restart_program', methods=['POST'])
@login_required
def restart_program():
    restart_event.set()
    return jsonify({"status": "ok", "message": "程序将重新启动！"}), 200

# 修改管理员密码页面
@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    error_message = None
    success_message = None
    if request.method == 'POST':
        cfg = get_config()
        old_password = request.form.get('old_password')
        new_password = request.form.get('new_password')

        if verify_password(old_password, cfg['admin_password']):
            if not new_password:
                error_message = "新密码不能为空。"
            else:
                if len(new_password) < 6:
                    logging.warning("管理员设置了短于 6 位的新密码，建议加长")
                # 只写密码字段，且写入的是哈希而非明文
                save_config({'admin_password': hash_password(new_password)})
                success_message = "密码修改成功，请使用新密码登录。"
                session.pop('logged_in', None)  # 注销用户
                return render_template('login.html', success_message=success_message)
        else:
            error_message = "旧密码输入错误，请重试。"

    return render_template('change_password.html', error_message=error_message, success_message=success_message)
@app.route('/api/load_config', methods=['GET'])
@login_required
def api_load_config():
    cfg = get_config()
    # 口令哈希没有必要下发给前端
    cfg.pop('admin_password', None)
    return jsonify(cfg)


@app.route('/api/local_caster', methods=['GET'])
@login_required
def api_local_caster():
    """本地 Caster 的实时状态，供配置页展示（含当前接入客户端数）。"""
    info = local_caster_snapshot()
    info['mode'] = get_config().get('mode') or 'none'
    return jsonify(info)


@app.route('/api/serial_ports', methods=['GET'])
@login_required
def api_serial_ports():
    """枚举系统当前可见的串口设备，供配置页下拉选择。

    返回每个串口的 device 与描述，并额外标注：
      - gnss_port：当前已配置为 GNSS 模块输入的串口（serial_port），
        前端据此把它在下拉里禁用，避免把数传电台选到 GNSS 模块那个口上。
      - radio_port：当前已配置为数传输出的串口（radio_port）。
    串口列表只反映"此刻插着哪些设备"，用户仍可手动填写列表外的路径。
    """
    try:
        ports = serial.tools.list_ports.comports()
        items = []
        for p in ports:
            items.append({
                'device': p.device,
                'description': (p.description or '').strip(),
                'hwid': getattr(p, 'hwid', '') or '',
            })
    except Exception as e:
        logging.error(f"枚举串口失败：{e}")
        items = []
    cfg = get_config()
    return jsonify({
        'ports': items,
        'gnss_port': (cfg.get('serial_port') or '').strip(),
        'radio_port': (cfg.get('radio_port') or '').strip(),
    })

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
SOURCETABLE_TIMEOUT = 8.0                 # 连接 + 读取的总超时（秒）
SOURCETABLE_MAX_BYTES = 4 * 1024 * 1024   # 防止对端无限吐数据把内存吃满
SOURCETABLE_CACHE_TTL = 300               # 同一 Caster 5 分钟内重复请求走缓存
SOURCETABLE_MAX_ITEMS = 5000              # 列表上限：极少数公共 Caster 会挂上万个点，
                                          # 全部塞进 <datalist> 会让页面卡顿
_sourcetable_cache = {}                   # (host, port) -> (timestamp, payload)
_sourcetable_cache_lock = threading.Lock()

# 主机名/域名/IPv4 校验：不允许斜杠、空格、冒号等会污染请求行的字符
_SOURCETABLE_HOST_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252})$')


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


@app.route('/api/sourcetable', methods=['POST'])
@login_required
def api_sourcetable():
    """读取指定 NTRIP Caster 的挂载点列表，供中继模式下拉选择。"""
    payload = request.get_json(silent=True) or request.form
    host = (payload.get('host') or '').strip()
    port_raw = (payload.get('port') or '').strip()
    user = (payload.get('user') or '').strip()
    password = payload.get('password') or ''

    if not _SOURCETABLE_HOST_RE.match(host):
        return jsonify({'ok': False, 'error': '中继地址格式不正确，请填写域名或 IP'}), 400
    try:
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': '中继端口无效，应为 1-65535 的整数'}), 400

    cache_key = (host.lower(), port)
    now = time.time()
    with _sourcetable_cache_lock:
        hit = _sourcetable_cache.get(cache_key)
    if hit and now - hit[0] < SOURCETABLE_CACHE_TTL:
        body = dict(hit[1])
        body['cached'] = True
        return jsonify(body)

    try:
        raw = fetch_sourcetable(host, port, user, password)
    except socket.timeout:
        logging.warning(f"获取挂载点超时: {host}:{port}")
        return jsonify({'ok': False, 'error': f'连接 {host}:{port} 超时，请检查地址、端口和网络'}), 502
    except OSError as e:
        logging.warning(f"获取挂载点失败: {host}:{port} - {e}")
        return jsonify({'ok': False, 'error': f'无法连接 {host}:{port}（{e.strerror or e}）'}), 502

    header, items = parse_sourcetable(raw)
    total = len(items)
    truncated = total > SOURCETABLE_MAX_ITEMS
    if truncated:
        items = items[:SOURCETABLE_MAX_ITEMS]
    if not items:
        snippet = raw.strip().splitlines()[:1]
        detail = snippet[0][:80] if snippet else '无响应内容'
        logging.warning(f"挂载点列表解析失败: {host}:{port} -> {detail}")
        if 'SOURCETABLE' not in raw.upper():
            msg = f'对方返回的不是 NTRIP 源列表（{detail}），请确认端口是否正确、是否需要账号密码'
        else:
            msg = '该 Caster 的源列表为空，请确认地址与端口'
        return jsonify({'ok': False, 'error': msg}), 502

    body = {
        'ok': True,
        'cached': False,
        'caster': header,
        'count': total,
        'truncated': truncated,
        'mountpoints': items,
    }
    with _sourcetable_cache_lock:
        _sourcetable_cache[cache_key] = (now, body)
        # 缓存顺手做个上限，避免长期运行后无限增长
        if len(_sourcetable_cache) > 16:
            oldest = min(_sourcetable_cache.items(), key=lambda kv: kv[1][0])[0]
            _sourcetable_cache.pop(oldest, None)

    logging.info(f"已获取 {total} 个挂载点{('（已截断列表）' if truncated else '')}: {host}:{port}")
    return jsonify(body)


# ==================== 本地 NTRIP Caster ====================
# 设计取舍（和参考实现 pygnssutils.socket_server 的差异）：
#   1. 不用 socketserver.ThreadingTCPServer，而是自己写 accept 循环 + 每客户端一线程。
#      原因是本程序需要"随配置重启"：restart 时要能确定性地关掉监听 socket 和所有
#      客户端连接，用裸 socket 更好控制生命周期。
#   2. 每个客户端一个有界队列，发布方永不阻塞。队列满了说明该客户端不读数据，
#      直接断开它——对 RTCM 流来说，慢客户端留着比踢掉更糟（会拖累所有客户端）。
#   3. 只实现一套挂载点、一组固定账号，不做 CAS/NET 的多挂载点逻辑。
class _CasterClient:
    """一个已接入的 NTRIP 客户端。"""
    __slots__ = ('sock', 'addr', 'outbox', 'alive')

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.outbox = queue.Queue(maxsize=LOCAL_CASTER_QUEUE_SIZE)
        self.alive = True


class LocalCaster:
    """把本机正在转发的 RTCM 数据以 NTRIP Caster 形式对内网发布。

    协议要点：
      GET /             → 返回 SOURCETABLE（不需要账号，方便客户端发现挂载点）
      GET /2RTK         → Basic 认证通过后返回 200/ICY，随后持续下发 RTCM
      认证失败          → 401 + WWW-Authenticate，客户端（RTKLIB/u-center 等）会重试带账号
      NTRIP 1.0 客户端  → 用 "ICY 200 OK"，2.0 客户端 → "HTTP/1.1 200 OK + Ntrip-Version"
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
                    sock.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
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

    def _parse_request(self, raw: bytes):
        """解析 NTRIP 请求，返回 (挂载点, 是否通过认证, 是否请求源列表, 是否NTRIP2)。"""
        mountpoint = ''
        authorized = False
        ntrip2 = False
        for line in raw.split(b'\r\n'):
            low = line.lower()
            if low.startswith(b'get '):
                parts = line.split(b' ')
                if len(parts) >= 2:
                    target = parts[1].decode('latin-1')
                    mountpoint = target.split('?')[0].strip('/')
            elif low.startswith(b'authorization: basic '):
                given = line.split(b' ', 2)[2].strip()
                if hmac.compare_digest(given, self._credentials):
                    authorized = True
            elif low.startswith(b'ntrip-version:'):
                ntrip2 = b'2.0' in line
        return mountpoint, authorized, mountpoint == '', ntrip2

    def _serve_client(self, client: _CasterClient):
        sock = client.sock
        try:
            sock.settimeout(LOCAL_CASTER_CLIENT_TIMEOUT)
            raw = b''
            while b'\r\n\r\n' not in raw and len(raw) < 8192:
                chunk = sock.recv(1024)
                if not chunk:
                    return
                raw += chunk
            if not raw:
                return

            mountpoint, authorized, wants_table, ntrip2 = self._parse_request(raw)
            http_date = self._http_date()

            # 1) 源列表：不要求认证（只暴露一个固定挂载点名，没有敏感信息）
            if wants_table:
                sock.sendall(self._build_sourcetable_response(ntrip2, http_date))
                logging.info(f"本地 Caster: {client.addr[0]} 读取源列表")
                return

            # 2) 挂载点不对：明确回 404，别把源列表当数据流塞给客户端
            if mountpoint != LOCAL_CASTER_MOUNTPOINT:
                body = f"unknown mountpoint, available: /{LOCAL_CASTER_MOUNTPOINT}\r\n".encode()
                head = (
                    "HTTP/1.1 404 Not Found\r\n"
                    + ("Ntrip-Version: Ntrip/2.0\r\n" if ntrip2 else "")
                    + f"Date: {http_date}\r\nContent-Type: text/plain\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode() + body
                sock.sendall(head)
                logging.warning(f"本地 Caster: {client.addr[0]} 请求了不存在的挂载点 /{mountpoint}")
                return

            # 3) 认证
            if not authorized:
                head = (
                    "HTTP/1.1 401 Unauthorized\r\n"
                    + ("Ntrip-Version: Ntrip/2.0\r\n" if ntrip2 else "")
                    + f"Server: {LOCAL_CASTER_SERVER_NAME}\r\nDate: {http_date}\r\n"
                    + f'WWW-Authenticate: Basic realm="{LOCAL_CASTER_MOUNTPOINT}"\r\n'
                    + "Connection: close\r\n\r\n"
                ).encode()
                sock.sendall(head)
                logging.warning(f"本地 Caster: {client.addr[0]} 认证失败（账号或密码不对）")
                return

            # 4) 放行，开始推流
            if ntrip2:
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
                         f"/{LOCAL_CASTER_MOUNTPOINT}（{'NTRIP2' if ntrip2 else 'NTRIP1'}）")

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
        return sorted(t for t, ts in msg_types_recent.items() if now - ts <= RTCM_TYPE_WINDOW)

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
        lat = station_info.get('latitude')
        lon = station_info.get('longitude')
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


local_caster: Optional['LocalCaster'] = None
# 上一次启动失败的原因，供状态页卡片显示（只在内存里，不落库）
local_caster_last_error = ''


def start_local_caster(cfg) -> Optional['LocalCaster']:
    """按配置启动本地 Caster；失败只记日志，不影响原有的上传链路。"""
    global local_caster, local_caster_last_error
    stop_local_caster()
    try:
        port = int(cfg.get('local_caster_port') or LOCAL_CASTER_PORT)
    except (TypeError, ValueError):
        port = LOCAL_CASTER_PORT
    host = (cfg.get('local_caster_host') or LOCAL_CASTER_HOST).strip() or LOCAL_CASTER_HOST
    caster = LocalCaster(host, port)
    if not caster.start():
        owner = find_port_owner(port)
        local_caster_last_error = f"{port} 端口被占用" + (f"（{owner}）" if owner else "")
        return None
    local_caster_last_error = ''
    local_caster = caster
    return caster


def stop_local_caster():
    global local_caster
    if local_caster is not None:
        local_caster.stop()
        local_caster = None
        logging.info("本地 NTRIP Caster 已停止")


def publish_to_local_caster(data: bytes):
    """把数据额外分发一份给本地 Caster 的客户端（没启动时是空操作）。"""
    if local_caster is not None:
        local_caster.publish(data)


# ==================== 无线电数传透传（Radio Serial Bridge） ====================
# 设计目标：把本机正在转发的同一路 RTCM 数据，额外以"串口透传"的方式送给一个
# USB 数传电台（发送端），让附近无网络的流动站通过配套数传电台直接收差分，
# 无需 NTRIP 账号密码。
#
# 关键取舍（与本地 Caster 相同的思路，但更强调"单向、只发不收、绝不影响主链路"）：
#   1. 独立开关：radio_enabled 与 local_caster_enabled、运行模式三者完全解耦，
#      关掉数传不影响本地 Caster，反之亦然；两者可同时开、也可独立开。
#   2. 独立线程 + 有界队列：串口写入在专用线程里做，publish 永远是非阻塞 put_nowait，
#      串口卡死/写慢只会丢弃数传这一路的数据，绝不拖累串口/中继主数据流。
#   3. 只发不收（单向广播）：数传场景 base 只发、流动站只收，串口以纯写打开，
#      不读任何回传字节，避免与 GNSS 模块串口产生任何读冲突。
#   4. 断线重连：串口被拔/写失败时自动关串口、定时重试重开，期间静默丢弃数据，
#      不向上抛异常、不影响主链路。
RADIO_QUEUE_SIZE = 512             # 待发帧上限，满了丢弃最旧（数传串口写慢时保护）
RADIO_RECONNECT_INTERVAL = 3.0     # 串口断线后的重连间隔（秒）
RADIO_VALID_BAUDS = [115200, 921600, 460800, 230400, 57600, 38400, 19200, 9600]


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


serial_bridge: Optional['SerialBridge'] = None


def start_serial_bridge(cfg) -> Optional['SerialBridge']:
    """按配置启动数传透传；失败只记日志，不影响主链路。"""
    global serial_bridge
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
    serial_bridge = bridge
    return bridge


def stop_serial_bridge():
    global serial_bridge
    if serial_bridge is not None:
        serial_bridge.stop()
        serial_bridge = None
        logging.info("数传透传已停止")


def publish_to_serial_bridge(data: bytes):
    """把数据额外透传一份给数传串口（没启用时是空操作，永不影响主链路）。"""
    if serial_bridge is not None:
        serial_bridge.publish(data)


def serial_bridge_snapshot() -> dict:
    """数传透传的对外可见快照，供状态页卡片使用（不含任何口令，可公开推送）。"""
    cfg = get_config()
    enabled = str(cfg.get('radio_enabled', '0')) == '1'
    if serial_bridge is not None:
        info = serial_bridge.status()
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


# ==================== 上传出口：云端 NTRIP Caster（Caster Uploader） ====================
# 历史问题（本次修复的核心）：原先 caster.sendall() 直接写在数据源循环里，
# 上传端一旦超时/断线，重连的 while 循环就把整个数据源循环卡住——
# 表现为"上传挂了，本地 Caster 和数传同时断流"。
#
# 现在的结构：独立线程 + 有界队列。数据源只做一次非阻塞 put_nowait，
# TCP 建连、NTRIP SOURCE 握手、推流、断线重连全部在本线程内完成。
# 上传端怎么超时重连，都不影响数据源读取和其他两个出口。
CASTER_QUEUE_SIZE = 512            # 待发帧上限，满了丢最旧（上传慢时保护内存）
CASTER_RECONNECT_INTERVAL = 5.0    # 断线重连基础间隔（秒）


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
    def _set_state(self, state: str, error: str = None):
        with self._lock:
            self._state = state
            self._state_text = UPLOAD_STATE_TEXT.get(state, state)
            if error is not None:
                self._last_error = error
            if state == 'authed':
                self._session_start = time.time()
            elif state in ('idle', 'connecting', 'retrying', 'error'):
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


caster_uploader: Optional['CasterUploader'] = None


def start_caster_uploader(cfg) -> Optional['CasterUploader']:
    """按独立开关启动上传出口；未勾选则什么都不做。"""
    global caster_uploader
    stop_caster_uploader()
    if str(cfg.get('caster_enabled', '0')) != '1':
        logging.info("未勾选「上传到云端 Caster」，跳过启动")
        return None
    if not (cfg.get('caster_host') or '').strip():
        logging.warning("已勾选上传出口但未填地址，跳过启动")
        return None
    up = CasterUploader(cfg)
    up.start()
    caster_uploader = up
    return up


def stop_caster_uploader():
    global caster_uploader
    if caster_uploader is not None:
        caster_uploader.stop()
        caster_uploader = None
        logging.info("上传出口已停止")


def feed_caster_uploader(data: bytes):
    """把数据投递给上传出口（未启用时是空操作）。"""
    if caster_uploader is not None:
        caster_uploader.feed(data)


# ==================== M0 统一数据分发器 ====================
def distribute(data: bytes):
    """把一路 RTCM 数据扇出给所有「已启用」的出口 + 解析线程。

    设计约束（这是整个架构改造的核心）：
      1. 永不阻塞：所有出口都是"队列投递"或"内存扇出"，本函数不做任何网络/串口 IO。
      2. 永不抛错：每个出口单独 try/except，一个出口异常绝不影响其他出口。
      3. 顺序固定：本地 Caster 与数传在前（本地优先，外网断了本地仍可用），
         上传出口在后（远端可能慢），解析缓冲最后（纯内存，最不可能失败）。

    数据源（串口 / 中继）只调这一个函数，不再关心有哪几个出口、谁先谁后。
    """
    if not data:
        return

    # 出口 1：本地 NTRIP Caster —— 纯内存扇出
    try:
        publish_to_local_caster(data)
    except Exception as e:
        logging.error(f"分发到本地 Caster 失败：{e}")

    # 出口 2：无线电数传 —— 入有界队列，满则丢最旧
    try:
        publish_to_serial_bridge(data)
    except Exception as e:
        logging.error(f"分发到数传出口失败：{e}")

    # 出口 3：上传云端 Caster —— 仅入队，所有 IO 在独立线程完成
    try:
        feed_caster_uploader(data)
    except Exception as e:
        logging.error(f"分发到上传出口失败：{e}")

    # 分支 4：RTCM 解析 —— 仅写内存缓冲，由常驻解析线程消费
    try:
        upload_data(data)
    except Exception as e:
        logging.error(f"分发到解析缓冲失败：{e}")


def describe_targets(cfg) -> str:
    """把「已启用」的出口拼成一句话，用于状态页和控制台显示。

    未勾选的出口不会出现在这里——即使数据库里存着历史地址。
    """
    parts = []
    if str(cfg.get('caster_enabled', '0')) == '1' and cfg.get('caster_host'):
        parts.append(f"上传 {cfg.get('caster_host')}:{cfg.get('caster_port')}"
                     f"/{cfg.get('caster_mountpoint')}")
    if str(cfg.get('local_caster_enabled', '0')) == '1':
        # 只有真正监听成功才会有对象；bind 失败时这里是 None。
        # 之前只看配置项，导致"启动失败"仍打印成"就绪"，状态页也显示正常，容易误判。
        if local_caster is not None:
            parts.append(f"本地 Caster {cfg.get('local_caster_host')}:{cfg.get('local_caster_port')}")
        else:
            parts.append(f"本地 Caster 启动失败（端口 {cfg.get('local_caster_port')} 不可用）")
    if str(cfg.get('radio_enabled', '0')) == '1':
        if serial_bridge is not None:
            parts.append(f"数传 {cfg.get('radio_port')}")
        else:
            parts.append("数传 未配置串口")
    return " + ".join(parts) if parts else "仅本地监控（未启用任何出口）"


def local_caster_snapshot() -> dict:
    """本地 Caster 的对外可见快照，供状态页卡片与 ws 首帧使用。

    只包含"能不能连、连了几台"这类运行信息，不含任何配置项；
    因此可以跟随状态的公开推送一起下发，不需要登录。
    """
    cfg = get_config()
    enabled = str(cfg.get('local_caster_enabled', '0')) == '1'
    if local_caster is not None:
        info = local_caster.status()
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
    info['last_error'] = local_caster_last_error
    info['example'] = f"{get_local_ip()}:{info['port']}/{LOCAL_CASTER_MOUNTPOINT}"
    return info


class WSClient:
    """一个监控连接 = 一个待发队列。

    这样改的原因（原来 send_to_clients 直接调 ws.send）：
      1. ws.send 是同步阻塞调用，simple_websocket 内部是单次 sock.send，没有超时。
         一个不读数据的客户端会把"调用 send 的那个线程"挂住 —— 而调用方可能是
         RTCM 解析线程或推流线程，等于监控拖累了主业务。
      2. simple_websocket 内部还有一个读线程在用同一个 wsproto 连接做解析，
         多个线程同时 send 会让 wsproto 状态机竞争，帧可能交错。
      3. 现在生产方只往队列里 append（deque 满自动丢最旧，永不阻塞），
         发送全部收敛到该连接自己的处理线程里（见 websocket()），一个连接只有一个发送者。
    """
    __slots__ = ('ws', 'authenticated', 'outbox', 'alive', 'dropped')

    def __init__(self, ws, authenticated: bool):
        self.ws = ws
        self.authenticated = authenticated
        self.outbox = deque(maxlen=WS_OUTBOX_MAXLEN)
        self.alive = True
        self.dropped = 0

    def enqueue(self, payload: str):
        if not self.alive:
            return
        if len(self.outbox) >= WS_OUTBOX_MAXLEN:
            self.dropped += 1
            if self.dropped > WS_DROP_LIMIT:
                # 队列持续满 = 客户端长期不消费，主动断开，把连接名额让出来
                self.alive = False
                return
        else:
            self.dropped = 0
        self.outbox.append(payload)

    def flush(self) -> bool:
        """把队列里的帧发完。返回 False 表示连接应终止。"""
        while self.outbox:
            if not self.alive:
                return False
            try:
                self.ws.send(self.outbox.popleft())
            except Exception:
                self.alive = False
                return False
        return True


def register_client(ws) -> Optional[WSClient]:
    """登记一个监控连接；超过上限返回 None。"""
    with clients_lock:
        if len(clients) >= MAX_WS_CLIENTS:
            return None
        client = WSClient(ws, bool(session.get('logged_in')))
        clients.append(client)
        return client

def unregister_client(client: WSClient):
    with clients_lock:
        try:
            clients.remove(client)
        except ValueError:
            pass

@app.route("/")
def index():
    return render_template('index.html')

@sock.route("/ws")
def websocket(ws):
    # 状态页本身是公开的，所以这里不做拦截，只记录"这个连接是否已登录"，
    # 由 send_to_clients 决定是否下发基准站精确坐标（见 _redact_for_public）。
    client = register_client(ws)
    if client is None:
        logging.warning(f"监控连接数已达上限 {MAX_WS_CLIENTS}，拒绝新连接")
        try:
            ws.send(json.dumps({
                "type": "error",
                "message": f"监控连接数已达上限（{MAX_WS_CLIENTS}），请稍后再试"
            }))
            ws.close()
        except Exception:
            pass
        return

    try:
        # 首帧历史也走队列，保证这个连接自始至终只有一个线程在 send
        all_history = list(log_history['log']) + list(log_history['print'])
        client.enqueue(json.dumps({
            "type": "history",
            "messages": all_history
        }))
        # 首帧补一条本地 Caster 状态：正常情况下 run_status 每 2 秒刷一次就够了，
        # 但 mode=none 时根本没有数据循环在推，首页会一直空着，所以连接时先给一次。
        client.enqueue(json.dumps({
            "type": "local_caster",
            "local_caster": local_caster_snapshot()
        }))
        # 数传透传状态同样补一条首帧兜底（mode=none 时靠它显示）
        client.enqueue(json.dumps({
            "type": "radio",
            "radio": serial_bridge_snapshot()
        }))

        # 单线程同时负责"发"和"收"：
        #   flush() 把队列发空 -> receive() 阻塞等待客户端消息/心跳，超时即回到循环顶部再发。
        # 这样每条连接只有一个发送者，彻底避免多线程写同一 wsproto。
        while client.alive:
            if not client.flush():
                break
            msg = ws.receive(timeout=WS_RECEIVE_POLL)
            if msg is None:
                continue   # 超时：正常，回到顶部继续发送
    except Exception:
        pass
    finally:
        client.alive = False
        unregister_client(client)


def _redact_for_public(message: dict) -> dict:
    """给未登录访问者的脱敏副本。

    状态页是公开的（方便随手看一眼信号质量），但基准站精确坐标、
    ECEF、设备序列号属于敏感资产信息，不应该匿名可见。
    把环境变量 PUBLIC_STATION_COORDS 设为 1 可恢复"完全公开"的旧行为。
    """
    if not isinstance(message, dict):
        return message
    if 'station_info' in message and isinstance(message.get('station_info'), dict):
        info = message['station_info']
        message = dict(message)
        message['station_info'] = {
            'station_id': info.get('station_id'),
            'masked': True
        }
    for key in ('antenna_info', 'device_info'):
        if isinstance(message.get(key), dict):
            item = dict(message[key])
            item.pop('serial_number', None)
            item.pop('receiver_serial', None)
            message = dict(message)
            message[key] = item
    return message


def send_to_clients(message):
    """广播一条消息：只做序列化 + 入队，不做任何 socket I/O。

    因为不碰 socket，即使客户端卡死也不会阻塞调用方
    （调用方可能是 RTCM 解析线程或推流线程）。
    """
    with clients_lock:
        snapshot = list(clients)
    if not snapshot:
        return

    authed_json = None
    public_json = None

    for client in snapshot:
        if client.authenticated or PUBLIC_STATION_COORDS:
            if authed_json is None:
                authed_json = json.dumps(message)
            client.enqueue(authed_json)
        else:
            if public_json is None:
                public_json = json.dumps(_redact_for_public(message))
            client.enqueue(public_json)

# 监控广播：降频 + 精简字段
MSM_BROADCAST_INTERVAL = 1.0        # 每个星座每秒最多推一次（原先每条 MSM 都推）
MSM_CELL_FIELDS = ('CELLPRN', 'CELLSIG', 'DF403', 'DF405', 'DF408')

_msm_last_sent = {}                 # gnss -> 上次广播时间，仅 process_rtcm_buffer 线程写

def _broadcast_msm(gnss: str, header: dict, cells) -> bool:
    """把一条 MSM 结果推给监控页面，但每个星座每秒最多一次。

    两处优化：
      1. 降频：多星座多信号同时播发时，原来每条 MSM 都广播一次全量 cell 数据
         （约 10 条/秒）。实测单条 22.5KB，等于每个监控页面 200KB/s 以上的流量，
         而页面本来就是 1Hz 刷新，多推的都被前端覆盖掉了。
      2. 裁剪：前端只读 CELLPRN / CELLSIG / DF403 / DF405 / DF408，
         DF400/401/402/404/406/407/420 发过去没人用，去掉能再省约 40% 流量。
    返回 True 表示本次确实发出了一条。
    """
    now = time.time()
    if now - _msm_last_sent.get(gnss, 0.0) < MSM_BROADCAST_INTERVAL:
        return False
    _msm_last_sent[gnss] = now

    slim_cells = [{k: c[k] for k in MSM_CELL_FIELDS if k in c} for c in cells]
    send_to_clients({
        "identity": header.get("identity"),
        "gnss": gnss,
        "station": header.get("station"),
        "sats": header.get("sats"),
        "cell_data": slim_cells,
        "station_info": station_info,
        "msg_types": sorted(list(msg_types_seen)),
        "msg_type_frequency": frequency_tracker.get_all_frequencies()
    })
    return True

# 统一接口，将收到的RTCM数据追加到全局缓冲区并限制大小
def upload_data(data):
    global rtcm_buffer
    with buffer_lock:
        overflow = len(rtcm_buffer) + len(data) - MAX_BUFFER_SIZE
        if overflow > 0:
            # 原先是整体 clear()，一次溢出会把还没解析的合法数据全丢掉；
            # 改为只丢最旧的部分，保留最新的数据继续解析。
            logging.warning(f"RTCM缓冲区溢出，丢弃最旧的 {overflow} 字节")
            del rtcm_buffer[:overflow]
        rtcm_buffer.extend(data)
    # 注意：这里不再做"上传统计"。原先把"进入解析缓冲的字节"记成"已上传字节"，
    # 语义是错的（没开上传出口时也会累加）。真正的发送量由 CasterUploader 线程统计。


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

# 处理RTCM缓冲区中的数据
def process_rtcm_buffer():
    while True:
        try:
            current_buffer = b""
            with buffer_lock:
                if rtcm_buffer:
                    current_buffer = bytes(rtcm_buffer)
                    rtcm_buffer.clear()
            
            if not current_buffer:
                time.sleep(0.1)
                continue
                
            frames, remaining = extract_rtcm_frames(current_buffer)
            
            if remaining:
                with buffer_lock:
                    rtcm_buffer.extend(remaining)
            
            for frame in frames:
                try:
                    with BytesIO(frame) as frame_io:
                        rtr = RTCMReader(frame_io)
                        
                        for (raw_data, parsed_data) in rtr:
                            if not parsed_data:
                                continue

                            try:
                                msg_type = parsed_data.DF002
                            except AttributeError:
                                continue
                            frequency_tracker.add_message(msg_type)
                            msg_types_seen.add(msg_type)
                            msg_types_recent[msg_type] = time.time()

                            if msg_type in (1005, 1006):
                                process_station_message(parsed_data, msg_type)
                            
                            if msg_type == 1033:
                                antenna_info, device_info = process_1033_message(parsed_data)
                                
                                if antenna_info:
                                    send_to_clients({
                                        "antenna_info": antenna_info,
                                        "msg_types": sorted(list(msg_types_seen)),
                                        "msg_type_frequency": frequency_tracker.get_all_frequencies()
                                    })
                                
                                if device_info:
                                    send_to_clients({
                                        "device_info": device_info,
                                        "msg_types": sorted(list(msg_types_seen)),
                                        "msg_type_frequency": frequency_tracker.get_all_frequencies()
                                    })

                            msm_array = parse_msm(parsed_data)
                            if msm_array:
                                header, sats, cells = msm_array
                                gnss = header.get("gnss", "UNKNOWN").upper()

                                # 降频 + 精简字段（详见下方 _broadcast_msm 的说明）
                                _broadcast_msm(gnss, header, cells)
                except Exception as e:
                    logging.error(f"处理RTCM帧时出错: {str(e)}")
                    continue

            time.sleep(0.01)
        except Exception as e:
            logging.error(f"处理RTCM缓冲区时出错: {str(e)}")
            time.sleep(1)

# 处理站点消息
def process_station_message(parsed_data, msg_type):
    try:
        if msg_type in (1005, 1006):
            ecef_x = getattr(parsed_data, 'DF025', 0)
            ecef_y = getattr(parsed_data, 'DF026', 0)
            ecef_z = getattr(parsed_data, 'DF027', 0)
            lat, lon, height = ecef_to_latlon(ecef_x, ecef_y, ecef_z)
            
            station_info.update({
                "station_id": getattr(parsed_data, 'DF003', "未知"),
                "ecef_x": ecef_x,
                "ecef_y": ecef_y,
                "ecef_z": ecef_z,
                "latitude": lat,
                "longitude": lon,
                "height": height
            })
            
            send_to_clients({
                "station_info": station_info,
                "msg_types": sorted(list(msg_types_seen)),
                "msg_type_frequency": frequency_tracker.get_all_frequencies()
            })
    except Exception as e:
        logging.error(f"处理1005/1006消息时出错: {e}")

# 处理1033消息
def process_1033_message(parsed_data):
    try:
        antenna_descriptor_count = getattr(parsed_data, "DF029", 0)
        antenna_name = ""
        for i in range(1, antenna_descriptor_count + 1):
            antenna_name += getattr(parsed_data, f"DF030_{i:02d}", "")
        
        antenna_info = {
            "station_id": getattr(parsed_data, "DF003", "未知"),
            "antenna_name": antenna_name,
            "antenna_setup_id": getattr(parsed_data, "DF031", "未知"),
            "serial_number": getattr(parsed_data, "DF032", "未知"),
            "message_type": "1033"
        }
        
        receiver_count = getattr(parsed_data, "DF227", 0)
        receiver_model = ""
        for i in range(1, receiver_count + 1):
            receiver_model += getattr(parsed_data, f"DF228_{i:02d}", "")
        
        firmware_count = getattr(parsed_data, "DF229", 0)
        firmware_version = ""
        for i in range(1, firmware_count + 1):
            firmware_version += getattr(parsed_data, f"DF230_{i:02d}", "")
        
        device_info = {
            "station_id": getattr(parsed_data, "DF003", "未知"),
            "receiver_model": receiver_model,
            "firmware_version": firmware_version,
            "receiver_serial": getattr(parsed_data, "DF231", "未知"),
            "message_type": "1033"
        }
        
        return antenna_info, device_info
    except Exception as e:
        logging.error(f"处理1033消息时出错: {e}")
        return {"error": "天线信息提取失败"}, {"error": "设备信息提取失败"}

# 连接管理，打开中继连接、重新连接Caster
def open_relay(cfg):
    sock = connect_to_host(cfg['relay_host'], cfg['relay_port'])
    if not handshake_ntrip_client(sock, cfg):
        sock.close()
        raise RuntimeError('中继认证失败')
    # 读超时交给 forward_relay 解释为"本轮没有数据"，而不是致命错误
    sock.settimeout(RELAY_READ_TIMEOUT)
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
        if thread_exit_event.is_set():
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
    

# 通过串口上传数据
def upload_via_serial(ser, cfg, thread_exit_event):
    """串口数据源：只负责「读串口 → 交给 distribute()」。

    注意这里不再有任何 caster.sendall / 重连逻辑——那正是历史上
    "上传端一卡住，串口读取停摆、本地 Caster 和数传一起断流"的根因。
    推流、握手、断线重连全部由 CasterUploader 在自己的线程里完成。
    """
    program_start = time.time()
    last_clear = program_start
    last_status_update = program_start
    total_bytes = 0

    # 增量更新，不整体替换全局 dict：避免覆盖其他模块写入的 radio / local_caster 字段
    run_status["mode"] = "串口模式"
    run_status["data_source"] = f"{cfg['serial_port']} @ {cfg['baud_rate']}bps"
    run_status["target_caster"] = describe_targets(cfg)
    push_run_status()

    while not thread_exit_event.is_set():
        try:
            if ser is None:
                logging.warning("串口对象为 None，尝试重新识别串口...")
                port, baud = detect_serial(cfg)
                if port and baud:
                    try:
                        ser = serial.Serial(port, baud, timeout=1)
                        cfg['serial_port'], cfg['baud_rate'] = port, baud
                        logging.info(f"串口重连成功: {port} @ {baud}")
                    except Exception as e:
                        logging.error(f"串口重建失败: {e}")
                        time.sleep(5)
                        continue
                else:
                    time.sleep(5)
                    continue

            data = ser.read(ser.in_waiting or 1024)

            if not data:
                time.sleep(0.2)
                continue

            # 统一分发：本地 Caster / 数传 / 上传 / 解析，四路互相独立且全部非阻塞
            distribute(data)

            total_bytes += len(data)

            now = time.time()
            if now - last_clear > 100:
                clear_screen()
                print(ART_LOGO)
                print(f"串口模式: {cfg['serial_port']} @ {cfg['baud_rate']}bps")
                print(f"数据出口: {describe_targets(cfg)}")
                print(f"运行时间: {format_duration(now - program_start)}")
                print(f"读取数据量: {format_bytes(total_bytes)}")
                last_clear = now

            if now - last_status_update > 2:
                run_status["data_sent"] = format_bytes(total_bytes)
                run_status["run_time"] = format_duration(now - program_start)
                push_run_status()
                last_status_update = now

        except Exception as e:
            logging.error(f"串口读取失败: {e}")
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(3)

    # 退出时只关自己持有的串口；上传连接由 CasterUploader 自己关闭
    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass


def forward_relay(relay, cfg, thread_exit_event):
    """中继数据源：只负责「从上游拉 RTCM → 交给 distribute()」。

    与串口模式的唯一区别就是数据从哪来；拿到数据之后的分发路径完全一致。
    同样不再持有 caster 连接 —— 上传由 CasterUploader 的独立线程负责。
    """
    program_start = time.time()
    last_clear = program_start
    last_recv = program_start
    last_status_update = program_start
    total_bytes = 0

    run_status["mode"] = "中继模式"
    run_status["data_source"] = f"{cfg['relay_host']}:{cfg['relay_port']}/{cfg['relay_mountpoint']}"
    run_status["target_caster"] = describe_targets(cfg)
    push_run_status()

    while not thread_exit_event.is_set():
        try:
            timed_out = False
            eof = False
            try:
                data = relay.recv(4096)
                # recv 返回空字节 = 对端已经关闭连接（不是超时）
                eof = not data
            except socket.timeout:
                # 读超时只代表"这一轮没有数据"，不代表连接坏了。
                # 原先没有单独捕获，socket.timeout 会掉进下面的 except，
                # 被当成致命错误把 relay 和 caster 两条连接一起断掉重连。
                data = b""
                timed_out = True

            if data:
                last_recv = time.time()
                total_bytes += len(data)
                # 与串口模式完全相同的统一分发路径
                distribute(data)
            elif eof:
                # 对端主动断开：立刻重连，不必等满 120 秒
                logging.warning("中继连接已被对端关闭，立即重新连接")
                try:
                    relay.close()
                except Exception:
                    pass
                relay = open_relay(cfg)
                last_recv = time.time()
            elif timed_out:
                # 读超时后走到这里，120 秒空闲重连规则现在才真正生效
                if time.time() - last_recv > RELAY_IDLE_RECONNECT:
                    logging.warning(f"中继连接连续 {RELAY_IDLE_RECONNECT} 秒无数据，尝试重新连接")
                    try:
                        relay.close()
                    except Exception:
                        pass
                    relay = open_relay(cfg)
                    last_recv = time.time()
                else:
                    time.sleep(0.1)

            current_time = time.time()
            if current_time - last_clear > 100:
                clear_screen()
                print(ART_LOGO)
                print(f"中继模式: {cfg['relay_host']}:{cfg['relay_port']}/{cfg['relay_mountpoint']}")
                print(f"数据出口: {describe_targets(cfg)}")
                print(f"运行时间: {format_duration(current_time - program_start)}")
                print(f"接收数据量: {format_bytes(total_bytes)}")
                last_clear = current_time

            if current_time - last_status_update > 2:
                run_status["data_sent"] = format_bytes(total_bytes)
                run_status["run_time"] = format_duration(current_time - program_start)
                push_run_status()
                last_status_update = current_time

        except ThreadExitRequested:
            break
        except Exception as e:
            logging.error(f"中继模式错误: {str(e)}")
            # 只重连数据源本身。上传出口有自己的重连循环，互不牵连。
            try:
                if relay is not None:
                    relay.close()
            except Exception:
                pass
            relay = None
            while relay is None and not thread_exit_event.is_set():
                try:
                    relay = open_relay(cfg)
                    logging.info("中继数据源重新连接成功")
                except ThreadExitRequested:
                    break
                except Exception as e2:
                    logging.error(f"中继数据源重新连接失败: {e2}")
                    if interruptible_sleep(15):
                        break

    # 退出时只关自己持有的中继连接
    if relay is not None:
        try:
            relay.close()
        except Exception:
            pass

def main():
    setup_logging()
    print("2RTK Server 启动")
    print(ART_LOGO)
    
    init_db()
    # 把历史遗留的明文管理员口令升级为哈希
    migrate_admin_password()
    
    # 启动辅助线程（保持不变）
    rtcm_processor = threading.Thread(target=process_rtcm_buffer, daemon=True)
    rtcm_processor.start()
    logging.info("RTCM数据处理线程已启动")

    # 注意：这里不能换成 waitress/gunicorn 普通的 HTTP worker。
    # flask-sock 需要服务器支持 WebSocket 协议升级，waitress 不支持，
    # 换上去会直接让监控面板的 /ws 无法连接。
    # 要上生产级 WSGI，需要 gunicorn + gevent worker，故此处保持内置服务器并显式开启多线程。
    web_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5757, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True
    )
    web_thread.start()
    
    host_ip = get_local_ip()  
    logging.info(f"Web服务已启动，访问地址: http://{host_ip}:5757")
    logging.info(f"请先在Web配置页面完成设置：http://{host_ip}:5757/config")

    # 重构：前置配置检查，仅在配置有效时执行主流程
    while not shutdown_event.is_set():
        # 1. 先检查配置是否有效
        cfg = get_config()
        if not validate_config(cfg):
            # 配置还没配好就安静等待：网页、登录、改密这些功能都照常可用，
            # 只是不往下启动任何数据模块。validate_config 内部已按 60 秒节流
            # 打出具体缺哪一项，这里不重复记日志。
            if (cfg.get('mode') or 'none') == 'none':
                # 让状态页能明确显示"等待配置"，而不是一片空白
                run_status['mode'] = '等待配置'
                run_status['data_source'] = '尚未选择数据源（串口 / 中继）'
                run_status['target_caster'] = '尚未选择数据出口'
            time.sleep(10)
            continue  # 跳过后续逻辑，直接进入下一次循环
        
        # 2. 配置有效，执行主流程（run_once不变）
        logging.info("配置信息有效，启动主程序...")
        run_once()
        
        # 3. 处理重启指令（保持不变）
        if restart_event.is_set():
            logging.info("接收到重启指令，重新检查配置...")
            restart_event.clear()
            continue
        
        # 4. 主流程正常结束后短暂等待（保持不变）
        time.sleep(1)


if __name__ == "__main__":

    main()