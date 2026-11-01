# -*- coding: utf-8 -*-
"""页面路由：/ 、/config 、/login 、/logout 、/change_password 、/restart_program。

注册时显式写 endpoint 名，保证 url_for('login') 这类既有引用在拆包后依然有效。
"""
import logging

from flask import (jsonify, redirect, render_template, request, session,
                   url_for)

from ..core import state
from ..core.config import get_config, mask_config, save_config
from ..core.constants import LOCAL_CASTER_HOST, LOCAL_CASTER_PORT
from ..security import (clear_login_failures, hash_password, login_locked_seconds,
                        login_required, record_login_failure, safe_next_url,
                        verify_password)
from ..utils.network import port_conflict_message, probe_tcp_port


def index():
    return render_template('index.html')


def login():
    # 检查用户是否已经登录
    if session.get('logged_in'):
        return redirect(url_for('config_page'))

    if request.method == 'POST':
        client_ip = (request.remote_addr or 'unknown')
        locked = login_locked_seconds(client_ip)
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
            clear_login_failures(client_ip)
            session['logged_in'] = True
            session.permanent = False
            next_url = safe_next_url(session.pop('next_url', None))
            if next_url:
                return redirect(next_url)
            return redirect(url_for('config_page'))
        record_login_failure(client_ip)
        return render_template('login.html', error='用户名或密码错误，请重试。')

    return render_template('login.html')


# 退出登录。原项目根本没有这个路由：登录后只能靠关浏览器或等 Session 过期，
# 共用设备时下一个人直接继承管理员权限。用 POST 而不是 GET，避免浏览器/杀毒软件
# 的链接预取把会话提前踢掉。
def logout():
    session.clear()
    return redirect(url_for('index'))


# 配置页面
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
                ours = (state.local_caster is not None and state.local_caster.port == lc_port)
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
                "local_caster_port": (form_data.get('local_caster_port') or '').strip() or str(LOCAL_CASTER_PORT),
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
                state.restart_event.set()
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

# 重启：必须有登录态。原先无鉴权，任何人都能远程反复重启造成拒绝服务。
@login_required
def restart_program():
    state.restart_event.set()
    return jsonify({"status": "ok", "message": "程序将重新启动！"}), 200


# 修改管理员密码页面
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


def register(app):
    """把页面路由挂到 app 上。endpoint 名与拆包前完全一致。"""
    app.add_url_rule('/', 'index', index, methods=['GET'])
    app.add_url_rule('/login', 'login', login, methods=['GET', 'POST'])
    app.add_url_rule('/logout', 'logout', logout, methods=['POST'])
    app.add_url_rule('/config', 'config_page', config_page, methods=['GET', 'POST'])
    app.add_url_rule('/restart_program', 'restart_program', restart_program, methods=['POST'])
    app.add_url_rule('/change_password', 'change_password', change_password, methods=['GET', 'POST'])
