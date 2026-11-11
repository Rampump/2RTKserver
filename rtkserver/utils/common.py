# -*- coding: utf-8 -*-
"""无状态通用工具：可中断 sleep、重试装饰器、格式化、坐标换算、清屏。

全部保持纯函数/纯装饰器特性，只通过 core.state 读取退出信号，
不持有任何自己的全局状态（重试计数也在闭包里）。
"""
import math
import os
import platform
import sys
import time
from functools import wraps

from ..core import state


class ThreadExitRequested(Exception):
    """收到退出/重启信号时，用于中断阻塞中的重试循环"""


def _dataplane_exit_requested() -> bool:
    """数据面是否收到了"这一轮该退出"的信号。

    标定线程与数据面**同进程**，共用 `state.thread_exit_event`。数据面每次
    「保存并重启」或"上一轮线程未退"都会置位这个事件，但那是**数据面自己的
    重启**，标定任务完全不该受影响——不然标定会在最不该失败的地方
    （`CORS_CONNECT` 建连）莫名抛出空消息的 `ThreadExitRequested()`。
    详见 `state.calibration_owns_serial` 的注释。
    """
    if state.calibration_owns_serial:
        return False
    return state.thread_exit_event.is_set()


def interruptible_sleep(seconds: float, event=None) -> bool:
    """可被退出信号打断的 sleep。返回 True 表示期间收到了退出信号。

    **进入时已置位的事件不算"期间收到"**：返回值只看这段 sleep 里事件有没有
    从"未置位"变成"置位"。原来的写法在末尾直接返回 `event.is_set()`，
    于是调用时事件已经是置位状态，就会立刻返回 True —— 任何"先置位、后
    sleep"的路径都会被误判成"被打断"，重试循环因此少跑一轮。
    标定线程复用同进程的 `thread_exit_event` 时这个 bug 会直接让重试失效。
    """
    event = event if event is not None else state.thread_exit_event
    # 标定独占串口期间，数据面的退出信号与自己无关（见 calibration_owns_serial）
    if event is state.thread_exit_event and state.calibration_owns_serial:
        end_time = time.time() + max(0.0, seconds)
        while time.time() < end_time:
            if state.shutdown_event.is_set():
                return False
            time.sleep(min(0.2, max(0.0, end_time - time.time())))
        return False
    if event.is_set():
        # 进来就已经是置位态：这是"先前就有的信号"，不是这次 sleep 收到的
        return False
    end_time = time.time() + max(0.0, seconds)
    while time.time() < end_time:
        if event.is_set():
            return True
        time.sleep(min(0.2, max(0.0, end_time - time.time())))
    return event.is_set()


def retry(max_retries: int, delay: int = 5, respect_exit: bool = True):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                # 重试循环必须能被重启指令打断，否则一次重连会阻塞数分钟，
                # 用户在页面上点"保存重启"后要等很久才生效。
                # 但标定线程持有串口期间不算：那时 `thread_exit_event` 只代表
                # 数据面自己在重启，标定不该被打断（见 `_dataplane_exit_requested`）。
                if respect_exit and _dataplane_exit_requested():
                    raise ThreadExitRequested()
                try:
                    return func(*args, **kwargs)
                except ThreadExitRequested:
                    raise
                except Exception as e:
                    if attempt < max_retries - 1:
                        if interruptible_sleep(delay):
                            raise ThreadExitRequested()
                    else:
                        raise
        return wrapper
    return decorator


def format_bytes(num_bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f}PB"


def format_duration(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}h {m}m {s}s"


def ecef_to_latlon(x, y, z):
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = 2 * f - f * f

    r = math.sqrt(x*x + y*y)
    z_val = z
    for _ in range(100):
        z_old = z_val
        sinp = z_val / math.sqrt(r*r + z_val*z_val)
        v = a / math.sqrt(1 - e2 * sinp*sinp)
        z_val = z + e2 * v * sinp
        if abs(z_val - z_old) < 1e-4:
            break
    lat = math.atan2(z_val, r) if r > 1e-12 else (math.pi/2 if z > 0 else -math.pi/2)
    lon = math.atan2(y, x)
    height = math.sqrt(r*r + z_val*z_val) - a / math.sqrt(1 - e2 * math.sin(lat)**2)

    return math.degrees(lat), math.degrees(lon), height


def clear_screen():
    # 在 systemd 下 stdout 不是 TTY，clear 只会把 ANSI 转义序列原样写进 journald
    # （日志里出现大量乱码），而且每 100 秒多 fork 一个进程。非交互环境直接跳过。
    try:
        if not sys.stdout.isatty():
            return
    except Exception:
        return
    system = platform.system()
    if system == "Windows":
        os.system('cls')
    elif system in ["Linux", "Darwin"]:
        os.system('clear')
    else:
        print("不支持的操作系统，无法清理屏幕。")


__all__ = [
    'ThreadExitRequested', 'interruptible_sleep', 'retry',
    'format_bytes', 'format_duration', 'ecef_to_latlon', 'clear_screen',
]
