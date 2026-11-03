# -*- coding: utf-8 -*-
"""JSON 接口：/api/load_config 、/api/local_caster 、/api/serial_ports 、/api/sourcetable。"""
import logging
import re
import socket
import threading
import time

import serial.tools.list_ports
from flask import jsonify, request

from ..core.config import get_config
from ..core.constants import (SOURCETABLE_CACHE_TTL, SOURCETABLE_MAX_ITEMS,
                              SOURCETABLE_HOST_RE)
from ..outlets.local_caster import local_caster_snapshot
from ..security import login_required
from ..sources.ntrip import fetch_sourcetable, parse_sourcetable

_sourcetable_cache = {}                   # (host, port) -> (timestamp, payload)
_sourcetable_cache_lock = threading.Lock()

_SOURCETABLE_HOST_RE = re.compile(SOURCETABLE_HOST_RE)


@login_required
def api_load_config():
    cfg = get_config()
    # 口令哈希没有必要下发给前端
    cfg.pop('admin_password', None)
    return jsonify(cfg)


@login_required
def api_local_caster():
    """本地 Caster 的实时状态，供配置页展示（含当前接入客户端数）。"""
    info = local_caster_snapshot()
    info['mode'] = get_config().get('mode') or 'none'
    return jsonify(info)


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


@login_required
def api_sourcetable():
    """读取指定 NTRIP Caster 的挂载点列表，供中继模式下拉选择。"""
    payload = request.get_json(silent=True) or request.form
    # 不假设字段类型：JSON 里端口通常是数字（`8002`），表单里是字符串（`"8002"`）。
    # 之前直接对 `payload.get('port')` 调 `.strip()`，数字端口一来就 AttributeError 500，
    # 前端只要把端口当 number 传就必挂。统一走 str() 再判。
    host = str(payload.get('host') or '').strip()
    port_raw = str(payload.get('port') or '').strip()
    user = str(payload.get('user') or '').strip()
    password = payload.get('password') or ''
    # form 提交的密码也可能是非 str（少见但一律兜住）
    if not isinstance(password, str):
        password = str(password)

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


def register(app):
    """把接口挂到 app 上。endpoint 名与拆包前完全一致。"""
    app.add_url_rule('/api/load_config', 'api_load_config', api_load_config, methods=['GET'])
    app.add_url_rule('/api/local_caster', 'api_local_caster', api_local_caster, methods=['GET'])
    app.add_url_rule('/api/serial_ports', 'api_serial_ports', api_serial_ports, methods=['GET'])
    app.add_url_rule('/api/sourcetable', 'api_sourcetable', api_sourcetable, methods=['POST'])
