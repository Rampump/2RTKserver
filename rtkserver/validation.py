# -*- coding: utf-8 -*-
"""配置有效性校验：只判定「能不能干活」，不负责保存拦截。

两条硬约束：
  1. 返回 False 只表示"还没配好"，主程序会停在等待配置，网页仍可用；
  2. 本函数在 mode=none 时每 10 秒被调用一次，内部日志必须节流，
     否则一天写满 1MB×4 的轮转额度，真正的故障日志会被冲掉。
"""
import logging
import time

from .core import state
from .core.constants import (LOCAL_CASTER_HOST, LOCAL_CASTER_PORT,
                             SERIAL_COMMON_BAUDS)
from .utils.network import check_self_loop, port_conflict_message, probe_tcp_port

_validate_log_state = {'last': 0.0, 'msg': None}


def _log_validate_problem(level: str, message: str):
    """配置校验失败的提示，按"同一问题 60 秒一次"节流。"""
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
        ours = (state.local_caster is not None and state.local_caster.port == lc_port)
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
                if baud not in SERIAL_COMMON_BAUDS:
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
