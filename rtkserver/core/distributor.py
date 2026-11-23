# -*- coding: utf-8 -*-
"""统一数据分发器：一路 RTCM 数据 → 所有已启用的接收方。

设计约束（整个架构改造的核心，任何改动都不能破坏）：
  1. 永不阻塞：所有出口都是"队列投递"或"内存扇出"，本函数不做任何网络/串口 IO。
  2. 永不抛错：每个出口单独 try/except，一个出口异常绝不影响其他出口。
  3. 顺序固定：本地 Caster 与数传在前（本地优先，外网断了本地仍可用），
     上传出口在后（远端可能慢），解析缓冲最后（纯内存，最不可能失败）。
  4. 不加锁：这条路径在每个数据包上都会跑一次，任何等待都是灾难。

数据源（串口 / 中继）只调这一个函数，不再关心有哪几个出口、谁先谁后。
"""
import logging
import time

from . import state


def _ensure_registered():
    if state.outlets:
        return
    # 懒注册：即使调用方忘了在启动流程里注册，分发也不会静默失效。
    from ..outlets import register_all
    register_all()


def _mark_source_data(data: bytes):
    """门闸打点：只认真正带 RTCM 帧头（0xD3）的数据。

    为什么不认"任意字节"：串口噪声、中继的 NTRIP 心跳、模块上电时吐的 ASCII
    日志都会先于 RTCM 到达，把它们当成"有数据"会让出口在真的没有差分数据时
    就抢先起连，问题原样复现。0xD3 是 RTCM3 的固定前导，与 detect_serial()
    的判据一致。
    """
    if b'\xd3' not in data:
        return
    state.last_data_ts = time.time()
    if state.source_state != 'ok':
        state.source_state = 'ok'
    if not state.first_data_event.is_set():
        state.first_data_event.set()


def distribute(data: bytes):
    """把一路 RTCM 数据扇出给所有「已启用」的出口 + 解析线程。"""
    if not data:
        return

    _mark_source_data(data)

    _ensure_registered()

    for name, item in list(state.outlets.items()):
        try:
            item['publish'](data)
        except Exception as e:
            logging.error(f"分发到{item.get('error_label', name)}失败：{e}")
