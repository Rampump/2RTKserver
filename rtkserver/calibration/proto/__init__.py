# -*- coding: utf-8 -*-
"""标定用的轻量协议层：NMEA 组帧/解析、RTCM 1005 解码。

刻意与 `rtkserver.sources.rtcm_parser` 分工：
    全局解析器属于"数据面"，此刻已被租约停掉，只做统计和 UI；
    本包只解析标定链路自己收到的字节（串口直读 / CORS 回包），
    绝不往 `state.rtcm_buffer` 里塞东西，避免污染监控面板。
CRC 与解帧这两件事复用全局实现，不重复造轮子。
"""
