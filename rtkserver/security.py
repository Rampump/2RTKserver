# -*- coding: utf-8 -*-
"""口令哈希、会话密钥、登录态与登录限速。

只依赖标准库 + Flask，不 import 项目内的 core.config，
所以 core.config 可以安全地 import 本模块（单向依赖）。
"""
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from .paths import SECRET_KEY_FILE
from .core.constants import (LOGIN_LOCKOUT_SECONDS, LOGIN_MAX_FAILURES,
                             PASSWORD_SCHEME, PBKDF2_ITERATIONS)


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


# 出厂默认管理员。登录页要靠这两个常量判断"该不该提示默认口令"，
# 所以只能有一处定义 —— 写在模板里一份、写在 core/config.py 里一份，
# 早晚会出现"提示说默认、实际已改过"或反过来的情况。
DEFAULT_ADMIN_USER = 'admin'
DEFAULT_ADMIN_PASSWORD = 'admin'


def is_default_admin(cfg) -> bool:
    """管理员是否仍在用出厂默认的用户名+口令（只读判断，不改任何状态）。

    登录页据此决定要不要显示「默认账号 admin / admin」的提示条：
      - 还开着默认口令 → 显示，并提醒正式部署前必须改；
      - 已经改过       → 不显示，否则等于在登录页上贴一个错误的账号密码。
    口令可能以 PBKDF2 哈希或历史明文两种形式存放，一律交给 verify_password 判。
    """
    try:
        user = str((cfg or {}).get('admin_user') or '')
        stored = str((cfg or {}).get('admin_password') or '')
    except AttributeError:
        return False
    if user != DEFAULT_ADMIN_USER:
        return False
    return verify_password(DEFAULT_ADMIN_PASSWORD, stored)


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


# ==================== 登录态 ====================
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


def safe_next_url(next_url):
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


# ==================== 登录失败限速 ====================
_login_failures = {}          # ip -> (连续失败次数, 最后一次失败时间)
_login_failures_lock = threading.Lock()


def login_locked_seconds(ip: str) -> int:
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


def record_login_failure(ip: str):
    with _login_failures_lock:
        # 简单的容量保护，避免被大量伪造源地址撑爆内存
        if len(_login_failures) > 1000:
            _login_failures.clear()
        count, _ = _login_failures.get(ip, (0, 0.0))
        _login_failures[ip] = (count + 1, time.time())


def clear_login_failures(ip: str):
    with _login_failures_lock:
        _login_failures.pop(ip, None)
