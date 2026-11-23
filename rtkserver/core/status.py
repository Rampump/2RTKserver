# -*- coding: utf-8 -*-
"""运行状态聚合：把「三个出口 + 网络信息」拼成首页卡片需要的 run_status。

这是唯一知道所有出口快照函数的地方；出口模块只提供 snapshot，不反向依赖这里。
"""
import logging

from ..core import state
from ..core.config import get_config
from ..outlets import caster_uploader, local_caster, radio_bridge
from ..utils.network import local_ipv4_addresses

# 局域网固定访问地址：由 mDNS 广播保证（见 discovery/zeroconf_announcer.py）
MDNS_HOSTNAME = '2rtk'
MDNS_URL = f'http://{MDNS_HOSTNAME}.local'


def describe_targets(cfg) -> str:
    """把「已启用」的出口拼成一句话，用于状态页和控制台显示。

    未勾选的出口不会出现在这里——即使数据库里存着历史地址。
    """
    parts = [p['text'] for p in describe_target_parts(cfg)]
    return " + ".join(parts) if parts else "仅本地监控（未启用任何出口）"



def describe_target_parts(cfg) -> list:
    """describe_targets 的结构化版本：返回 [{kind, text, ...}]。

    kind 是给前端查 i18n 用的机器可读标识，text 保留原中文串。
    这样英文界面可以重新拼一遍，而不是把中文直接塞进界面。
    """
    parts = []
    if str(cfg.get('caster_enabled', '0')) == '1' and cfg.get('caster_host'):
        parts.append({
            'kind': 'caster',
            'host': cfg.get('caster_host'),
            'port': cfg.get('caster_port'),
            'mountpoint': cfg.get('caster_mountpoint'),
            'text': f"上传 {cfg.get('caster_host')}:{cfg.get('caster_port')}"
                    f"/{cfg.get('caster_mountpoint')}",
        })
    if str(cfg.get('local_caster_enabled', '0')) == '1':
        # 只有真正监听成功才会有对象；bind 失败时这里是 None。
        # 之前只看配置项，导致"启动失败"仍打印成"就绪"，状态页也显示正常，容易误判。
        if state.local_caster is not None:
            parts.append({
                'kind': 'local_caster',
                'host': cfg.get('local_caster_host'),
                'port': cfg.get('local_caster_port'),
                'text': f"本地 Caster {cfg.get('local_caster_host')}:{cfg.get('local_caster_port')}",
            })
        else:
            parts.append({
                'kind': 'local_caster_failed',
                'port': cfg.get('local_caster_port'),
                'text': f"本地 Caster 启动失败（端口 {cfg.get('local_caster_port')} 不可用）",
            })
    if str(cfg.get('radio_enabled', '0')) == '1':
        if state.serial_bridge is not None:
            parts.append({
                'kind': 'radio',
                'port': cfg.get('radio_port'),
                'text': f"数传 {cfg.get('radio_port')}",
            })
        else:
            parts.append({
                'kind': 'radio_unconfigured',
                'text': "数传 未配置串口",
            })
    return parts


def refresh_network_info():
    """把本机内网 IP 与固定访问地址写进 run_status。

    首次使用（mode=none）也必须能看见 IP —— 安卓只能靠它访问，
    所以这个函数由 /ws 首帧和 push_run_status 两条路径都会调用。
    """
    try:
        ips = local_ipv4_addresses()
    except Exception:
        ips = []
    state.run_status['local_ip'] = ips[0] if ips else ''
    state.run_status['local_ips'] = ips

    ann = state.announcer
    if ann is not None:
        try:
            snap = ann.snapshot()
        except Exception:
            snap = {}
        state.run_status['mdns_state'] = snap.get('state', 'unavailable')
        state.run_status['mdns_host'] = snap.get('name') or f'{MDNS_HOSTNAME}.local'
        state.run_status['mdns_url'] = snap.get('web_url') or f"{MDNS_URL}:{_web_port()}"
        state.run_status['mdns_ntrip_url'] = snap.get('ntrip_url') or ''
    else:
        # 还没挂载（或 zeroconf 缺失）：仍然给出建议地址，但标明状态
        state.run_status['mdns_state'] = 'unavailable'
        state.run_status['mdns_host'] = f'{MDNS_HOSTNAME}.local'
        state.run_status['mdns_url'] = f"{MDNS_URL}:{_web_port()}"
        state.run_status['mdns_ntrip_url'] = ''


def _web_port() -> int:
    from ..core.constants import WEB_PORT
    return WEB_PORT


def refresh_source_gate():
    """把「数据源有没有数据」算成前端能直接用的两个字段。

    source_state 只有四种：
      unknown  —— 还没开始（未配置 / 刚启动）
      waiting  —— 本轮一帧 RTCM 都没收到（门闸拦住，三个出口都没开）
      ok       —— 最近 SOURCE_STALL_TIMEOUT 秒内有数据
      stalled  —— 有数据过，但现在断了（模块被拔 / 中继上游停流）
    判定只依赖 state.last_data_ts（distributor 唯一写点），不另存一份状态，
    避免"上传端以为有数据、状态页以为没有"这种两套事实。
    """
    import time as _time

    from ..core.constants import SOURCE_STALL_TIMEOUT

    ts = state.last_data_ts
    if not ts:
        st = 'waiting' if state.source_state == 'waiting' else 'unknown'
        idle = 0
    else:
        idle = int(max(0.0, _time.time() - ts))
        st = 'stalled' if idle > SOURCE_STALL_TIMEOUT else 'ok'
    state.run_status['source_state'] = st
    state.run_status['source_idle_seconds'] = idle
    # 提示文案由后端给出中文兜底；英文界面按 source_state 查 i18n
    if st == 'stalled':
        state.run_status['source_hint'] = (
            f"数据源已 {idle} 秒没有 RTCM 数据（模块掉线或上游无流），"
            "云端上传已临时断开，数据恢复后自动重连")
    elif st == 'ok':
        state.run_status['source_hint'] = ''
    # waiting 的文案由 lifecycle 的 _mark_source_waiting() 写入，这里不覆盖
    return st


def push_run_status():
    """推送运行状态，并顺带带上各出口的实时快照。

    客户端数会随时变化，所以不能只在启动时算一次；放在这里每 2 秒更新，
    首页那几张卡片就能跟着动。
    """
    try:
        refresh_source_gate()
    except Exception:
        pass
    try:
        state.run_status["local_caster"] = local_caster.local_caster_snapshot()
    except Exception:
        # 状态快照失败绝不能连累主推送链路
        pass
    try:
        cfg = get_config()
        state.run_status["upload"] = caster_uploader.upload_snapshot(cfg)
    except Exception:
        pass
    try:
        state.run_status["radio"] = radio_bridge.serial_bridge_snapshot()
    except Exception:
        pass
    refresh_network_info()
    state.broadcast({"run_status": state.run_status})


def initial_run_status() -> dict:
    """WS 首帧专用：给一份完整快照（含 mode=none 时的 IP 展示）。"""
    refresh_network_info()
    try:
        refresh_source_gate()
    except Exception:
        pass
    try:
        state.run_status["local_caster"] = local_caster.local_caster_snapshot()
    except Exception:
        pass
    try:
        cfg = get_config()
        state.run_status["upload"] = caster_uploader.upload_snapshot(cfg)
    except Exception:
        pass
    try:
        state.run_status["radio"] = radio_bridge.serial_bridge_snapshot()
    except Exception:
        pass
    return state.run_status


def log_targets(cfg):
    logging.info(f"数据出口就绪：{describe_targets(cfg)}")
