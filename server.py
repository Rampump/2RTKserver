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
from flask import Flask, render_template, send_from_directory, jsonify, request, redirect, url_for, session
from flask_sock import Sock
import sqlite3
import contextlib
from typing import Optional, Tuple
from threading import Event

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
clients = set()
msg_types_seen = set()
shutdown_event = Event()
restart_event = Event()
thread_exit_event = Event()
MAX_BUFFER_SIZE = 10 * 1024 * 1024 

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('server.log', encoding='utf-8')
        ]
    )

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "localhost"

run_status = {
    "mode": "",
    "data_source": "",
    "target_caster": "",
    "data_sent": "0B",
    "run_time": "0h 0m 0s"
}

CONFIG_DB = '2RTKserver.db'
DEFAULT_CONFIG = {
    "mode": "none",
    "serial_port": "",
    "baud_rate": "",
    "caster_host": "",
    "caster_port": "2101",
    "caster_mountpoint": "",
    "caster_password": "",
    "relay_host": "",
    "relay_port": "2101",
    "relay_mountpoint": "",
    "relay_user": "",
    "relay_password": "",
    "admin_user": "admin",
    "admin_password": "admin"
}

def init_db():
    """初始化数据库并写入默认配置（小写键名）"""
    with contextlib.closing(sqlite3.connect(CONFIG_DB)) as conn:
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

def get_db():
    return sqlite3.connect(CONFIG_DB)

def get_config():
    init_db()
    config = {}
    logging.info("开始从数据库读取配置信息")
    with contextlib.closing(get_db()) as conn:
        cursor = conn.execute("SELECT key, value FROM settings")
        for key, value in cursor.fetchall():
            config[key.lower()] = value
    logging.info("配置信息读取完成")
    int_fields = ['caster_port', 'relay_port', 'baud_rate']
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
    try:
        cfg = get_config()
        if not validate_config(cfg):
            logging.error("监测到配置信息无效，将于5秒后重新读取")
            time.sleep(5)
            return

        mode = cfg['mode']
        ser = None
        relay = None
        caster = None
        upload_thread = None
        forward_thread = None

        if mode == 'serial':
            logging.info("进入串口模式")
            port, baud = detect_serial(cfg)
            if port and baud:
                cfg['serial_port'], cfg['baud_rate'] = port, baud
                try:
                    ser = serial.Serial(port=port, baudrate=baud, timeout=1)
                except Exception as e:
                    logging.error(f"打开串口失败: {e}")
                    time.sleep(5)
                    return
                try:
                    caster = open_caster(cfg)
                except Exception as e:
                    logging.error(f"打开Caster失败: {e}")
                    time.sleep(5)
                    return

                upload_thread = threading.Thread(
                    target=upload_via_serial,
                    args=(ser, caster, cfg, thread_exit_event),
                    daemon=True
                )
                upload_thread.start()

                while not restart_event.is_set() and not shutdown_event.is_set():
                    time.sleep(1)
            else:
                logging.warning("未检测到有效串口，5秒后重试")
                time.sleep(5)

        elif mode == 'relay':
            logging.info("进入中继模式")
            try:
                relay = open_relay(cfg)
            except Exception as e:
                logging.error(f"打开中继连接失败: {e}")
                time.sleep(5)
                return
            try:
                caster = open_caster(cfg)
            except Exception as e:
                logging.error(f"打开上传Caster失败: {e}")
                time.sleep(5)
                return

            forward_thread = threading.Thread(
                target=forward_relay,
                args=(relay, caster, cfg, thread_exit_event),
                daemon=True
            )
            forward_thread.start()

            while not restart_event.is_set() and not shutdown_event.is_set():
                time.sleep(1)

        else:
            logging.error(f"未知模式: {mode}")
            time.sleep(5)

    except Exception as e:
        logging.exception(f"运行主循环run_once时发生未捕获异常: {e}")
        time.sleep(5)

    finally:
        thread_exit_event.set()
        threads_to_join = []
        if upload_thread and upload_thread.is_alive():
            threads_to_join.append((upload_thread, "串口上传"))
        if forward_thread and forward_thread.is_alive():
            threads_to_join.append((forward_thread, "中继转发"))

        timeout = 5
        start_time = time.time()

        for thread, name in threads_to_join:
            remaining = max(0, timeout - (time.time() - start_time))
            if remaining > 0:
                thread.join(remaining)
                if thread.is_alive():
                    logging.warning(f"{name}线程未在超时时间内退出，强制终止")

        thread_exit_event.clear()

        if ser:
            try:
                ser.close()
            except Exception as e:
                logging.error(f"关闭串口时出错: {e}")
        if relay:
            try:
                relay.close()
            except Exception as e:
                logging.error(f"关闭中继连接时出错: {e}")
        if caster:
            try:
                caster.close()
            except Exception as e:
                logging.error(f"关闭Caster连接时出错: {e}")

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
    save_config(DEFAULT_CONFIG.copy())
    logging.info("配置已重置为默认值")

# 工具函数
def retry(max_retries: int, delay: int = 5):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(delay)
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
    system = platform.system()
    if system == "Windows":
        os.system('cls')
    elif system in ["Linux", "Darwin"]: 
        os.system('clear')
    else:
        print("不支持的操作系统，无法清理屏幕。")

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
                frames.append(frame)
                index += frame_length
            else:
                break
        else:
            index += 1
    return frames, data[index:]

# 配置处理，包括日志设置、读取配置、验证配置等
def validate_config(cfg):
    mode = cfg.get('mode', 'none')
    if mode == 'none':
        logging.error("请在Web页面配置程序运行模式为（串口/中继）及其他必要参数")
        return False
    if not cfg.get('caster_host') or not cfg.get('caster_mountpoint'):
        logging.error('NTRIP Caster HOST未配置,请在Web页面配置上传NTRIP Caster服务器信息')
        return False
    if mode == 'serial':
        return True
    if mode == 'relay':
        if not cfg.get('relay_host') or not cfg.get('relay_mountpoint'):
            logging.error('relay Caster服务器未配置，请在Web页面配置中继caster服务器信息')
            return False
        return True
    logging.error(f"未知运行模式: {mode}")
    return False

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
            
            while time.time() - start_time < 5:
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
        logging.info(f"成功连接到{host}:{port}")
        return s
    except Exception as e:
        logging.error(f"连接到{host}:{port} 失败: {e}，请检查网络连接和配置")
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
    srv_sock.sendall(hdr.encode())
    resp = srv_sock.recv(2048).decode(errors='ignore')
    logging.info(f"收到realy caster服务器响应: {resp.splitlines()[:5]}")
    if 'ICY 200 OK' in resp or '200 OK' in resp:
        logging.info('Relay NTRIP server authentication passed, receiving RTCM data')
        return True
    logging.error(f"Relay NTRIP server response: {resp.splitlines()[:5]}")
    return False

# 服务器Caster连接，打开Caster连接@retry(max_retries=3, delay=5)
@retry(max_retries=3, delay=5)
def open_caster(cfg):
    logging.info(f">>>向上传端NTRIP Caster 申请连接{cfg['caster_host']}:{cfg['caster_port']}")
    sock = connect_to_host(cfg['caster_host'], int(cfg['caster_port']))
    hdr = (
        f"SOURCE {cfg['caster_password']} {cfg['caster_mountpoint']}\r\n"
        "Source-Agent: NTRIP 2RTKserver\r\n"
        "STR: \r\n\r\n"
    )
    sock.sendall(hdr.encode())
    resp = sock.recv(1024).decode(errors='ignore')
    logging.info(">>> 接收到上传端NTRIP Caster 响应 ：")
    logging.info(resp)
    
    if 'ICY 200 OK' not in resp and '200 OK' not in resp:
        logging.error(f"NTRIP Caster response: {resp.splitlines()[:3]}")
        raise RuntimeError('上传端 NTRIP Caster 认证失败，请检查密码及配置信息')
    logging.info('上传端NTRIP Caster 认证通过开始发送RTCM数据')
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
app.secret_key = 'adfaeg354350-2=13s43st108-34053du12'
sock = Sock(app)

try:
    with open("index.html", "r", encoding="utf-8") as f:
        frontend_html = f.read()
except FileNotFoundError:
    frontend_html = "<h1>前端文件未找到</h1>"

# 登录验证装饰器
def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            session['next_url'] = request.path
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    return wrapper

@app.route('/login', methods=['GET', 'POST'])
def login():
    # 检查用户是否已经登录
    if session.get('logged_in'):
        return redirect(url_for('config_page'))  

    if request.method == 'POST':
        cfg = get_config()
        username = request.form.get('username')
        password = request.form.get('password')
        if username == cfg['admin_user'] and password == cfg['admin_password']:
            session['logged_in'] = True
            next_url = session.pop('next_url', None)
            if next_url:
                return redirect(next_url)
            return redirect(url_for('config_page'))  
        return render_template('login.html', error='用户名或密码错误，请重试。')

    return render_template('login.html')

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
            new_cfg = {"mode": mode}

            if mode == 'serial':
                new_cfg.update({
                    "serial_port": form_data.get('serial_port', ''),
                    "baud_rate": form_data.get('baud_rate', ''),
                    "caster_host": form_data.get('caster_host', ''),
                    "caster_port": form_data.get('caster_port', '2101'),
                    "caster_mountpoint": form_data.get('caster_mountpoint', ''),
                    "caster_password": form_data.get('caster_password', '')
                })
            elif mode == 'relay':
                new_cfg.update({
                    "relay_host": form_data.get('relay_host', ''),
                    "relay_port": form_data.get('relay_port', '2101'),
                    "relay_mountpoint": form_data.get('relay_mountpoint', ''),
                    "relay_user": form_data.get('relay_user', ''),
                    "relay_password": form_data.get('relay_password', ''),
                    "caster_host": form_data.get('caster_host', ''),
                    "caster_port": form_data.get('caster_port', '2101'),
                    "caster_mountpoint": form_data.get('caster_mountpoint', ''),
                    "caster_password": form_data.get('caster_password', '')
                })

            logging.info(f"保存的配置信息: {new_cfg}")
            save_config(new_cfg)

            message = "配置已保存成功！"
            message_type = "success"

            if 'restart_program' in request.form:
                restart_event.set()
                message = "配置已保存，程序将重新启动！"
                return redirect(url_for('index'))

    config = get_config()
    return render_template('config.html', config=config, message=message, message_type=message_type)

@app.route('/reset_config', methods=['POST'])
def reset_config():
    reset_config_to_default()
    return jsonify({"status": "ok", "message": "配置已恢复默认，请使用默认账号 admin 登录"}), 200
@app.route('/restart_program', methods=['POST'])
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

        if old_password == cfg['admin_password']:
            # 更新密码
            cfg['admin_password'] = new_password
            save_config(cfg)
            success_message = "密码修改成功，请使用新密码登录。"
            session.pop('logged_in', None)  # 注销用户
            return render_template('login.html', success_message=success_message)
        else:
            error_message = "旧密码输入错误，请重试。"

    return render_template('change_password.html', error_message=error_message, success_message=success_message)
@app.route('/api/load_config', methods=['GET'])
@login_required
def api_load_config():
    return jsonify(get_config())

@app.route("/")
def index():
    return render_template('index.html')

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

@sock.route("/ws")
def websocket(ws):
    clients.add(ws)
    try:
        while True:
            ws.receive(timeout=60)
    except Exception:
        clients.discard(ws)


def send_to_clients(message):
    if not clients:
        return
    message_json = json.dumps(message)
    dead_clients = set()
    for ws in clients:
        try:
            ws.send(message_json)
        except Exception:
            dead_clients.add(ws)
    for ws in dead_clients:
        clients.discard(ws)

# 统一接口，将收到的RTCM数据追加到全局缓冲区并限制大小
def upload_data(data):
    global rtcm_buffer
    with buffer_lock:
        if len(rtcm_buffer) + len(data) > MAX_BUFFER_SIZE:
            logging.warning("RTCM缓冲区溢出，清空缓冲区")
            rtcm_buffer.clear()
        rtcm_buffer.extend(data)

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

                                send_to_clients({
                                    "identity": header.get("identity"),
                                    "gnss": gnss,
                                    "station": header.get("station"),
                                    "sats": header.get("sats"),
                                    "cell_data": cells,
                                    "station_info": station_info,
                                    "msg_types": sorted(list(msg_types_seen)),
                                    "msg_type_frequency": frequency_tracker.get_all_frequencies()
                                })
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
    return sock

def reconnect_caster(cfg):
    try:
        return open_caster(cfg)
    except Exception:
        time.sleep(5)
        return open_caster(cfg)

# 通过串口上传数据
def upload_via_serial(ser, caster, cfg, thread_exit_event):
    program_start = time.time()
    last_clear = program_start
    last_status_update = program_start
    total_bytes = 0

    global run_status

    while not thread_exit_event.is_set():
        try:
            data = ser.read(ser.in_waiting or 1024)
        except Exception as e:
            logging.error(f"串口读取失败: {e}")
            ser.close()
            ser = None

        if not data:
            time.sleep(0.5)
            ser.close()
            logging.warning("串口无数据，尝试重新识别串口...")
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
            continue

        # 尝试发送到Caster
        try:
            caster.sendall(data)
        except Exception as e:
            logging.error(f"caster发送失败，尝试重连: {e}")
            try:
                caster.close()
            except:
                pass
            try:
                caster = reconnect_caster(cfg)
                logging.info("caster重连成功")
            except Exception as e2:
                logging.error(f"caster重连失败: {e2}")
                time.sleep(5)
                continue

        upload_data(data)
        total_bytes += len(data)

        now = time.time()
        if now - last_clear > 100:
            clear_screen()
            print(ART_LOGO)
            print(f"串口模式: {cfg['serial_port']} @ {cfg['baud_rate']}bps")
            print(f"上传到: {cfg['caster_host']}:{cfg['caster_port']}/{cfg['caster_mountpoint']}")
            print(f"运行时间: {format_duration(now - program_start)}")
            print(f"发送数据量: {format_bytes(total_bytes)}")
            last_clear = now

        if now - last_status_update > 2:
            run_status = {
                "mode": "串口模式",
                "data_source": f"{cfg['serial_port']} @ {cfg['baud_rate']}bps",
                "target_caster": f"{cfg['caster_host']}:{cfg['caster_port']}/{cfg['caster_mountpoint']}",
                "data_sent": format_bytes(total_bytes),
                "run_time": format_duration(now - program_start)
            }
            send_to_clients({"run_status": run_status})
            last_status_update = now

        time.sleep(0.05)


def forward_relay(relay, caster, cfg, thread_exit_event):
    program_start = time.time()
    last_clear = program_start
    last_recv = program_start
    last_status_update = program_start
    total_bytes = 0
    global run_status

    run_status = {
        "mode": "中继模式",
        "data_source": f"{cfg['relay_host']}:{cfg['relay_port']}/{cfg['relay_mountpoint']}",
        "target_caster": f"{cfg['caster_host']}:{cfg['caster_port']}/{cfg['caster_mountpoint']}",
        "data_sent": "0B",
        "run_time": "0h 0m 0s"
    }
    send_to_clients({"run_status": run_status})

    while not thread_exit_event.is_set():
        try:
            data = relay.recv(4096)
            if data:
                last_recv = time.time()
                total_bytes += len(data)
                try:
                    caster.sendall(data)
                except Exception as send_err:
                    logging.error(f"发送数据到 Caster 失败: {send_err}")
                    raise
                upload_data(data)
            else:
                if time.time() - last_recv > 120:
                    logging.warning("中继连接超时，尝试重新连接")
                    relay.close()
                    relay = open_relay(cfg)
                    last_recv = time.time()

            current_time = time.time()
            if current_time - last_clear > 100:
                clear_screen()
                print(ART_LOGO)
                print(f"模式: 中继relay 数据源:{cfg['relay_host']}:{cfg['relay_port']}/{cfg['relay_mountpoint']}")
                print(f"目的地: {cfg['caster_host']}:{cfg['caster_port']}/{cfg['caster_mountpoint']}")
                print(f"已发送时间: {format_duration(current_time - program_start)}")
                print(f"转发数据量: {format_bytes(total_bytes)}")
                last_clear = current_time

            if current_time - last_status_update > 2:
                run_status["data_sent"] = format_bytes(total_bytes)
                run_status["run_time"] = format_duration(current_time - program_start)
                send_to_clients({"run_status": run_status})
                last_status_update = current_time

            time.sleep(0.1)
        except Exception as e:
            logging.error(f"中继模式错误: {str(e)}")
            if "10054" in str(e) or "10053" in str(e):
                logging.warning("远程主机断开连接，尝试重新连接中继...")
                try:
                    relay.close()
                except:
                    pass
                try:
                    relay = open_relay(cfg)
                    last_recv = time.time()
                    logging.info("重新连接成功")
                except Exception as e2:
                    logging.error(f"重新连接失败: {e2}")
                    time.sleep(5)
            else:
                logging.error("未知错误，5秒后重试...")
                time.sleep(5)

def main():
    setup_logging()
    print("2RTK Server 启动")
    print(ART_LOGO)
    
    init_db()
    
    rtcm_processor = threading.Thread(target=process_rtcm_buffer, daemon=True)
    rtcm_processor.start()
    logging.info("RTCM数据处理线程已启动")

    web_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5757, debug=False, use_reloader=False),
        daemon=True
    )
    web_thread.start()
    
    host_ip = get_local_ip()  
    logging.info(f"Web服务已启动，访问地址: http://{host_ip}:5757")
    logging.info("Web配置页面: http://{host_ip}:5757/config")

    while not shutdown_event.is_set():
        run_once()
        if restart_event.is_set():
            logging.info("接收到重启指令，正在重新加载配置")
            restart_event.clear()
        time.sleep(1)

if __name__ == "__main__":

    main()