# -*- coding: utf-8 -*-
"""升级与卸载接口。

设计约束：升级会把本进程停掉，所以这里所有接口都必须「立即返回」，
结果一律靠 /api/upgrade/status 轮询。前端据此在服务断开时不报错，
而是显示"正在重启"并继续等。
"""
import logging

from flask import jsonify, request

from ..core.config import get_config
from ..security import login_required, verify_password
from ..upgrade import service


@login_required
def api_upgrade_status():
    """当前版本 + 最近一次升级任务的状态 + 日志尾巴。"""
    data = service.snapshot()
    return jsonify({'ok': True, **data})


@login_required
def api_upgrade_check():
    """探测各升级源是否可达、远端提交与本地是否一致。"""
    try:
        return jsonify({'ok': True, **service.check_updates()})
    except Exception as e:  # noqa: BLE001
        logging.error(f"检查更新失败: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@login_required
def api_upgrade_start():
    """发起一键升级。立即返回，真正的进度靠轮询 status。"""
    body = request.get_json(silent=True) or {}
    source_key = (body.get('source') or 'auto').strip()
    ok, msg = service.start_upgrade(source_key)
    if not ok:
        return jsonify({'ok': False, 'error': msg}), 409
    logging.warning(f"管理员发起了升级（源: {source_key}）")
    return jsonify({
        'ok': True,
        'message': '升级任务已启动，服务即将重启',
        'ports': service.managed_ports(),
    })


@login_required
def api_upgrade_uninstall():
    """一键卸载。危险操作：要求再次输入管理员口令。"""
    body = request.get_json(silent=True) or {}
    pwd = body.get('password') or ''
    if not body.get('confirm'):
        return jsonify({'ok': False, 'error': '缺少确认标记'}), 400
    cfg = get_config()
    if not verify_password(pwd, cfg.get('admin_password', '')):
        logging.warning("卸载请求口令校验失败")
        return jsonify({'ok': False, 'error': '管理员口令不正确'}), 403

    ok, msg = service.start_uninstall(backup_db=True)
    if not ok:
        return jsonify({'ok': False, 'error': msg}), 409
    logging.warning("管理员发起了卸载，程序将从本机移除")
    return jsonify({'ok': True, 'message': '卸载任务已启动，服务即将停止且不再启动'})


def register(app):
    app.add_url_rule('/api/upgrade/status', 'api_upgrade_status',
                     api_upgrade_status, methods=['GET'])
    app.add_url_rule('/api/upgrade/check', 'api_upgrade_check',
                     api_upgrade_check, methods=['POST'])
    app.add_url_rule('/api/upgrade/start', 'api_upgrade_start',
                     api_upgrade_start, methods=['POST'])
    app.add_url_rule('/api/upgrade/uninstall', 'api_upgrade_uninstall',
                     api_upgrade_uninstall, methods=['POST'])
