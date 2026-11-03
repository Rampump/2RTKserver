# -*- coding: utf-8 -*-
"""基准站一键标定（calibration）包。

依赖方向铁律（由 tests/test_contract.py 锁死）：
    calibration → core / utils / sources   只读复用，单向
    core / outlets / sources → calibration 禁止，一行都不许 import

这条规则的实际意义：把 `rtkserver/calibration/` 整个目录删掉，主程序
（采集、分发、出口、配置页、升级）必须照常运行。所以包内任何模块
都不允许在 import 期做副作用动作（建库、开串口、起线程）。

子模块分工（阶段 0~2 已落地，标 * 的是阶段 3 才写的）：
    store.py          calibration.db 读写（独立库，不碰 settings 表）
    geo.py            坐标换算、3σ 野值剔除、Welford、卡尔曼
    proto/nmea.py     NMEA 组帧与解析
    proto/rtcm.py     RTCM 1005 轻量解码（只做标定校验）
    workspace.py      `.calibration/` 运行目录与原子 JSON 读写
    lease.py          串口租约：让位 / 独占 / 归还 / 心跳
    service.py        状态机、单工作线程、P0 只读诊断
    drivers/          各型号命令表与命令通道
    cors.py *         NTRIP 客户端 + GGA 上报
    api.py *          Web 接口

本文件刻意不 import 任何子模块：`import rtkserver.calibration` 必须零成本、
零副作用，否则"删掉本包主程序照跑"这条就只剩口号。
"""
