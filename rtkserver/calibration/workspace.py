# -*- coding: utf-8 -*-
"""标定运行目录（`<安装目录>/.calibration/`）与原子 JSON 读写。

为什么单独抽一层：
    `lease.json` 与 `state.json` 都是「随时可能被另一个线程/进程读到」的文件。
    直接 `open(path, 'w')` 写会有一段"文件已截断、内容还没写完"的窗口，
    页面此时读到的是半截 JSON —— 用户看到的提示是"标定状态损坏"，
    而真实原因只是写文件和读文件撞在了一起。

    这里统一走「同目录临时文件 + os.replace」：同一分区内的 replace 是原子的，
    读方要么看到旧内容，要么看到新内容，不存在中间态。

    与 `store.py` 同一个套路，`STATE_DIR` 是模块级变量而不是常量常量：
    自测可以整体改指临时目录，跑完不留痕。所以本模块 import 期不建目录，
    真正做到"删掉 rtkserver/calibration/ 主程序照跑"。
"""
import io
import json
import logging
import os
import threading
import time

from ..paths import BASE_DIR

# 标定运行目录。放模块级（非 const 的写法）是为了自测能整体改指临时目录。
STATE_DIR = os.path.join(BASE_DIR, '.calibration')

LEASE_NAME = 'lease.json'
STATE_NAME = 'state.json'

# 落盘保留期：终态租约留 24 小时供页面回显"上次标定停在哪"（方案 §3.3 第 3 步）
LEASE_KEEP_SECONDS = 24 * 3600


def lease_path():
    return os.path.join(STATE_DIR, LEASE_NAME)


def state_path():
    return os.path.join(STATE_DIR, STATE_NAME)


def ensure_dir():
    """确保运行目录存在。**只在写之前调用**，import 期一律不碰文件系统。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    return STATE_DIR


def write_json(path, data, durable=True):
    """原子写 JSON。失败抛 OSError，由调用方决定"降级成警告"还是"直接终止"。

    `durable=False` 跳过 fsync：心跳每几秒写一次，真刷盘对 SD 卡不友好，
    而且断电丢最后几秒心跳的后果只是"看门狗多等一轮"，不影响正确性。

    临时文件名带 pid + 线程号：状态由 Web 线程与标定线程**同时**写很正常
    （一个写进度、一个写租约），共用一个 `xxx.tmp` 在 Windows 上会直接
    `PermissionError: 另一个程序正在使用此文件` —— 真机上表现为"状态页偶尔不刷新"。
    """
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        with io.open(tmp, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        # Windows 的 os.replace 在目标文件被子进程/杀软短暂打开时会失败，
        # 重试三次基本都能过；POSIX 上第一次就成功，重试成本可以忽略。
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return path
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path


def read_json(path, default=None):
    """读 JSON。文件不存在/内容损坏/无权限一律返回 `default`，绝不抛给调用方。

    标定页面不能因为一个残留的坏文件而打不开 —— 这比"读不到历史"严重得多。
    """
    try:
        with io.open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        logging.debug(f'读取 {path} 失败（按空处理）：{e}')
        return default


def remove_file(path):
    """删文件，不存在也算成功。"""
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    except OSError as e:
        logging.debug(f'删除 {path} 失败（忽略）：{e}')
        return False
    return True
