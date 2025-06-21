#!/usr/bin/env python3
import socket
import serial
import serial.tools.list_ports
import os
import logging
import time
import configparser
import base64
from functools import wraps

ART_LOGO = """
    ██████╗ ██████╗ ████████╗██╗  ██╗
    ╚════██╗██╔══██╗╚══██╔══╝██║ ██╔╝
     █████╔╝██████╔╝   ██║   █████╔╝
    ██╔═══╝ ██╔══██╗   ██║   ██║  ██╗
    ███████╗██║  ██║   ██║   ██║  ██╗
    ╚══════╝╚═╝  ╔═╝   ╚═╝   ╚═╝  ╚═╝
         2RTK Ntrip Server 1.8.6
"""

# ------------------- Retry Decorator -------------------
def retry(max_retries: int, delay: int = 5):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt < max_retries - 1:
                        logging.info(f"Retry attempt {attempt+1}/{max_retries}, waiting {delay} seconds...")
                        time.sleep(delay)
                    else:
                        raise
        return wrapper
    return decorator

# ------------------- Byte Formatting -------------------
def format_bytes(num_bytes):
    for unit in ['B','KB','MB','GB','TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f}PB"

# ------------------- Format Time to Hours, Minutes, and Seconds -------------------
def format_duration(seconds):
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}h {m}m {s}s"

# ------------------- Basic Functions -------------------
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()]
    )

def get_config():
    cfg = configparser.ConfigParser()
    cfg.read('config.ini')
    baud_raw = cfg.get('Serial', 'BAUD_RATE', fallback='').strip()
    baud_val = int(baud_raw) if baud_raw.isdigit() else 0
    return {
        'MODE': cfg.get('General', 'MODE', fallback='serial').lower(),
        'SERIAL_PORT': cfg.get('Serial', 'PORT', fallback='').strip(),
        'BAUD_RATE': baud_val,
        'HOST': cfg.get('NTRIP', 'HOST', fallback='').strip(),
        'PORT': cfg.getint('NTRIP', 'PORT', fallback=2101),
        'MOUNTPOINT': cfg.get('NTRIP', 'MOUNTPOINT', fallback='').strip(),
        'PASSWORD': cfg.get('NTRIP', 'PASSWORD', fallback='').strip(),
        'RELAY_HOST': cfg.get('Relay', 'HOST', fallback='').strip(),
        'RELAY_PORT': cfg.getint('Relay', 'PORT', fallback=2101),
        'RELAY_MOUNTPOINT': cfg.get('Relay', 'MOUNTPOINT', fallback='').strip(),
        'RELAY_USER': cfg.get('Relay', 'USER', fallback='').strip(),
        'RELAY_PASSWORD': cfg.get('Relay', 'PASSWORD', fallback='').strip(),
    }

def validate_config(cfg):
    mode = cfg['MODE']
    if not cfg['HOST'] or not cfg['MOUNTPOINT']:
        logging.error('Local Caster HOST or MOUNTPOINT is not configured')
        return False
    if mode == 'serial':
        return True
    if mode == 'relay':
        if not cfg['RELAY_HOST'] or not cfg['RELAY_MOUNTPOINT']:
            logging.error('Relay server HOST or MOUNTPOINT is not configured')
            return False
        return True
    logging.error(f"Unknown mode: {mode}")
    return False

# ------------------- Serial Port Scanning and Detection -------------------
def detect_serial(cfg):
    port = cfg['SERIAL_PORT']
    baud = cfg['BAUD_RATE']
    if port and baud and test_serial(port, baud):
        logging.info(f"Using configured serial port {port} @ {baud}bps")
        return port, baud
    if port and baud:
        logging.warning(f"Configured serial port {port} @ {baud}bps has no data, switching to automatic scanning")
    ports = [p.device for p in serial.tools.list_ports.comports()]
    for p in ports:
        for b in [115200, 9600, 57600, 230400]:
            if test_serial(p, b):
                logging.info(f"Automatically detected serial port {p} @ {b}bps, starting to read RTCM data")
                return p, b
    return None, None

@retry(max_retries=2, delay=2)
def test_serial(port, baud, threshold=50):
    try:
        ser = serial.Serial(port=port, baudrate=baud, timeout=0.5)
        ser.reset_input_buffer()
        total = 0
        start = time.time()
        while time.time() - start < 2:
            total += len(ser.read(ser.in_waiting or 1))
            if total >= threshold:
                ser.close()
                return True
        ser.close()
        return False
    except Exception:
        return False

# ------------------- NTRIP Connection -------------------
@retry(max_retries=3, delay=5)
def connect_to_host(host, port, timeout=10):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))
    return s

# ------------------- NTRIP Client Request -------------------
def build_client_request(cfg):
    auth = ''
    if cfg['RELAY_USER'] and cfg['RELAY_PASSWORD']:
        cred = f"{cfg['RELAY_USER']}:{cfg['RELAY_PASSWORD']}"
        auth = base64.b64encode(cred.encode()).decode()
    req = f"GET /{cfg['RELAY_MOUNTPOINT']} HTTP/1.1\r\n"
    req += f"Host: {cfg['RELAY_HOST']}\r\n"
    req += "Ntrip-Version: Ntrip/2.0\r\n"
    req += "User-Agent: NTRIP 2RTKClient/1.0\r\n"
    if auth:
        req += f"Authorization: Basic {auth}\r\n"
    req += "Accept: */*\r\nConnection: close\r\n\r\n"
    return req

@retry(max_retries=3, delay=5)
def handshake_ntrip_client(srv_sock, cfg):
    hdr = build_client_request(cfg)
    srv_sock.sendall(hdr.encode())
    resp = srv_sock.recv(2048).decode(errors='ignore')
    if 'ICY 200 OK' in resp or '200 OK' in resp:
        logging.info('Relay NTRIP server authentication passed, receiving RTCM data')
        return True
    logging.error(f"Relay NTRIP server response: {resp.splitlines()[:5]}")
    return False

# ------------------- Server Caster Connection -------------------
@retry(max_retries=3, delay=5)
def open_caster(cfg):
    sock = connect_to_host(cfg['HOST'], cfg['PORT'])
    hdr = (
        f"SOURCE {cfg['PASSWORD']} {cfg['MOUNTPOINT']}\r\n"
        "Source-Agent: NTRIP 2RTKServer/1.8.6\r\n"
        "STR: \r\n\r\n"
    )
    sock.sendall(hdr.encode())
    resp = sock.recv(1024).decode(errors='ignore')
    if 'ICY 200 OK' not in resp and '200 OK' not in resp:
        logging.error(f"NTRIP Caster response: {resp.splitlines()[:3]}")
        raise RuntimeError('Local Caster authentication failed')
    logging.info('NTRIP Caster authentication passed, starting to send RTCM data')
    return sock

# ------------------- Serial Port Data Upload -------------------
def upload_via_serial(ser, caster, cfg):
    program_start = time.time()
    last_clear = program_start
    last_recv = program_start
    total_bytes = 0
    while True:
        data = ser.read(ser.in_waiting or 1)
        if data:
            last_recv = time.time()
            total_bytes += len(data)
            try:
                caster.sendall(data)
            except Exception:
                caster = reconnect_caster(cfg)
        if time.time() - last_recv > 120:
            logging.error('Serial port data timeout, reconnecting serial port')
            ser.close()
            port, baud = detect_serial(cfg)
            cfg['SERIAL_PORT'], cfg['BAUD_RATE'] = port, baud
            ser = serial.Serial(port=port, baudrate=baud, timeout=1)
            last_recv = time.time()
        if time.time() - last_clear > 100:
            os.system('clear')
            print(ART_LOGO)
            print('Mode: Serial RTCM forwarding')
            print(f"From Serial Port: {cfg['SERIAL_PORT']} @ {cfg['BAUD_RATE']}bps")
            print(f"To   Caster: {cfg['HOST']}:{cfg['PORT']} /{cfg['MOUNTPOINT']}")
            elapsed_total = int(time.time() - program_start)
            print(f"Total running time: {format_duration(elapsed_total)}  Total forwarded: {format_bytes(total_bytes)}")
            last_clear = time.time()
        time.sleep(0.1)

# ------------------- Relay Data Forwarding -------------------
def forward_relay(relay, caster, cfg):
    program_start = time.time()
    last_clear = program_start
    last_recv = program_start
    total_bytes = 0
    while True:
        try:
            data = relay.recv(4096)
        except Exception:
            logging.error('Relay reception failed, reconnecting relay server')
            relay = open_relay(cfg)
            last_recv = time.time()
            continue
        if data:
            last_recv = time.time()
            total_bytes += len(data)
            try:
                caster.sendall(data)
            except Exception:
                caster = reconnect_caster(cfg)
        if time.time() - last_recv > 120:
            logging.error('Relay RTCM data timeout, reconnecting relay server')
            relay.close()
            relay = open_relay(cfg)
            last_recv = time.time()
        if time.time() - last_clear > 100:
            os.system('clear')
            print(ART_LOGO)
            print('Mode: Ntrip Caster relay')
            print(f"From Caster: {cfg['RELAY_HOST']}:{cfg['RELAY_PORT']} /{cfg['RELAY_MOUNTPOINT']}")
            print(f"To   Caster: {cfg['HOST']}:{cfg['PORT']} /{cfg['MOUNTPOINT']}")
            elapsed_total = int(time.time() - program_start)
            print(f"Total running time: {format_duration(elapsed_total)}  Total forwarded: {format_bytes(total_bytes)}")
            last_clear = time.time()
        time.sleep(0.1)

# ------------------- Reconnection Helper -------------------
def open_relay(cfg):
    sock = connect_to_host(cfg['RELAY_HOST'], cfg['RELAY_PORT'])
    if not handshake_ntrip_client(sock, cfg):
        sock.close()
        raise RuntimeError('Relay NTRIP client authentication failed')
    return sock

def reconnect_caster(cfg):
    logging.info('Reconnecting local Caster')
    try:
        return open_caster(cfg)
    except Exception:
        time.sleep(5)
        return open_caster(cfg)

# ------------------- Main Process -------------------
def main():
    setup_logging()
    while True:
        os.system('clear')
        print(ART_LOGO)
        cfg = get_config()
        if not validate_config(cfg):
            time.sleep(5)
            continue
        mode = cfg['MODE']
        try:
            if mode == 'serial':
                port, baud = detect_serial(cfg)
                cfg['SERIAL_PORT'], cfg['BAUD_RATE'] = port, baud
                ser = serial.Serial(port=port, baudrate=baud, timeout=1)
                caster = open_caster(cfg)
                upload_via_serial(ser, caster, cfg)
            elif mode == 'relay':
                relay = open_relay(cfg)
                caster = open_caster(cfg)
                forward_relay(relay, caster, cfg)
            else:
                logging.error(f"Unknown MODE: {mode}")
                time.sleep(5)
        except Exception as e:
            logging.error(f"Runtime exception: {e}, retrying in 5 seconds")
            time.sleep(5)

if __name__ == '__main__':
    main()
