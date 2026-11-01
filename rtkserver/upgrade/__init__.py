# -*- coding: utf-8 -*-
"""升级与卸载能力。

目录职责：
    runner.py    自包含的执行器单文件，由 systemd-run 以独立 transient unit 拉起，
                 不受 `systemctl stop 2rtkserver` 影响。只依赖标准库。
    service.py   Web 侧封装：准备计划、拉起执行器、读取状态与版本。
    sources.py   升级源地址（GitHub / GitCode），允许设备上单独覆盖。
"""
