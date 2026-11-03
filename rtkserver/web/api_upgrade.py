# -*- coding: utf-8 -*-
"""升级接口。

设计约束：升级会把本进程停掉，所以这里所有接口都必须「立即返回」，
结果一律靠 /api/upgrade/status 轮询。前端据此在服务断开时不报错，
而是显示"正在重启"并继续等。

卸载**不再是**接口：它会删掉整个安装目录，误触不可逆，只保留命令行入口。
`/api/upgrade/uninstall` 现在固定返回 410，用来给旧书签一个明确的着落。
"""
import logging

from flask import jsonify, request

from ..security import login_required
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
    """已下线。

    卸载会停服务、删 systemd 单元与整个安装目录，误触不可逆。网页上一个
    "确认框 + 输 DELETE"挡不住这种操作——用户可能只是想关掉弹窗就点了确认。
    所以入口整体移除，只保留安装目录里的 `uninstall.sh`（SSH 登录后执行）。

    这里保留一个明确的 410 而不是让路由 404：404 会让人以为是前端调错了地址，
    410 + 这段说明才能告诉真正需要卸载的人"去哪执行"。也避免旧书签静默失败。
    """
    return jsonify({
        'ok': False,
        'error': '网页不再提供卸载入口。请 SSH 登录设备后执行 /opt/2RTKserver/uninstall.sh',
    }), 410


def register(app):
    app.add_url_rule('/api/upgrade/status', 'api_upgrade_status',
                     api_upgrade_status, methods=['GET'])
    app.add_url_rule('/api/upgrade/check', 'api_upgrade_check',
                     api_upgrade_check, methods=['POST'])
    app.add_url_rule('/api/upgrade/start', 'api_upgrade_start',
                     api_upgrade_start, methods=['POST'])
    app.add_url_rule('/api/upgrade/uninstall', 'api_upgrade_uninstall',
                     api_upgrade_uninstall, methods=['POST'])
