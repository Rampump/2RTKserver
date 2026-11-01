# -*- coding: utf-8 -*-
"""出口注册：把「谁接收数据」收敛成一张表，distribute() 只遍历它。

顺序即分发顺序，与改造前完全一致：
    本地 Caster（纯内存扇出）→ 数传（有界队列）→ 上传（有界队列）→ 解析缓冲（纯内存）

为什么用注册表而不是在 distributor 里 import 三个出口：
    以后新增出口只改这张表，distribute() 一行不用动，也不会出现
    "分发器反向依赖所有业务模块"的耦合。
"""
from ..core import state
from . import caster_uploader, local_caster, radio_bridge


def register_all():
    """注册四个接收方（三个出口 + 解析缓冲）。重复调用是幂等的。"""
    from ..sources import rtcm_parser

    state.outlets.clear()
    state.outlets['local_caster'] = {
        'publish': local_caster.publish,
        'label': '本地 Caster',
        'error_label': '本地 Caster',
    }
    state.outlets['radio'] = {
        'publish': radio_bridge.publish,
        'label': '数传',
        'error_label': '数传出口',
    }
    state.outlets['caster'] = {
        'publish': caster_uploader.feed,
        'label': '上传',
        'error_label': '上传出口',
    }
    state.outlets['parser'] = {
        'publish': rtcm_parser.upload_data,
        'label': '解析',
        'error_label': '解析缓冲',
    }
    return state.outlets
