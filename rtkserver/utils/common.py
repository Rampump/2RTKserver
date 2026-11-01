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


def interruptible_sleep(seconds: float, event=None) -> bool:
    """可被退出信号打断的 sleep。返回 True 表示期间收到了退出信号。"""
    event = event if event is not None else state.thread_exit_event
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
                if respect_exit and state.thread_exit_event.is_set():
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
