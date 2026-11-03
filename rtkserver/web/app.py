# -*- coding: utf-8 -*-
"""Flask 应用工厂。

两个拆包必踩的坑，这里显式处理：
  1. Flask(__name__) 在包内会让 root_path 指向 rtkserver/web/，
     templates/ 与 static/ 全部 404 —— 必须显式指定绝对路径。
  2. 路由必须用显式 endpoint 名注册，保证 url_for('login') 这些既有引用不变。
"""
import logging

from flask import Flask
from flask_sock import Sock

from .. import paths
from ..core.constants import (LOCAL_CASTER_MOUNTPOINT, LOCAL_CASTER_PASSWORD,
                              LOCAL_CASTER_USER, PUBLIC_STATION_COORDS)
from ..security import get_or_create_secret_key

app = None
sock = None


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


def create_app() -> Flask:
    """创建并配置 Flask 应用（含 Sock），返回同一个单例。

    只允许调用一次：flask-sock 的路由和 session 密钥都不适合重复初始化。
    """
    global app, sock
    if app is not None:
        return app

    app = Flask(__name__, template_folder=paths.TEMPLATES_DIR, static_folder=paths.STATIC_DIR)
    # 原先是硬编码常量，任何人拿到源码就能自签 session cookie 冒充管理员。
    # 改为：环境变量 SECRET_KEY 优先，否则首次启动随机生成并持久化到独立文件。
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
        public_station_coords=PUBLIC_STATION_COORDS,
    )

    app.after_request(_set_security_headers)

    from . import api, api_upgrade, routes, ws
    routes.register(app)
    api.register(app)
    api_upgrade.register(app)
    ws.register(sock)

    # 标定 Web 层（阶段 6）：唯一一处 web → calibration 的依赖，且**必须是可选的**。
    # 契约要求"删掉标定包主程序照跑"，所以这里用 try/except 兜住 ImportError：
    # 没有标定包时只是不注册 /calibrate 与 /api/calibration/*，其它功能不受影响。
    calibration_available = False
    try:
        from . import api_calibration
        api_calibration.register(app)
        calibration_available = True
    except ImportError as e:                    # pragma: no cover - 仅裁剪包时触发
        logging.info('标定模块不可用，跳过 /calibrate 与 /api/calibration/*：%s', e)
    app.jinja_env.globals.update(calibration_available=calibration_available)

    logging.info("Web 应用已创建（模板 %s，静态 %s）", paths.TEMPLATES_DIR, paths.STATIC_DIR)
    return app


def get_app() -> Flask:
    """懒获取：未创建则创建，供测试与 reset_config 等入口使用。"""
    return create_app()
