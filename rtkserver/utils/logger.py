# -*- coding: utf-8 -*-
"""日志初始化：全项目唯一入口，只允许 lifecycle.main() 调用一次。

其它模块一律 `logging.getLogger(__name__)`，绝不调用 basicConfig、
也不添加 Handler —— 否则会出现日志重复、文件句柄泄漏。
"""
import builtins
import logging
import os
from collections import deque
from logging.handlers import RotatingFileHandler

from ..core import state
from ..paths import LOG_FILE


class WebSocketLogHandler(logging.Handler):
    """WebSocket日志处理器，不影响原有日志处理。

    只往 state.publish 里丢，不 import web 层，避免 core → web 的反向依赖。
    """

    def __init__(self):
        super().__init__()
        self.formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        self.history = deque(maxlen=100)  # 保留最近100条日志

    def emit(self, record):
        try:
            msg = self.format(record)
            self.history.append(msg)
            state.broadcast({"type": "log", "message": msg})
        except Exception:
            self.handleError(record)


class PrintCapture:
    """捕获print输出，同时保留原有控制台输出"""

    def __init__(self, original_print):
        self.original_print = original_print
        self.history = deque(maxlen=100)

    def __call__(self, *args, **kwargs):
        # 保留原有控制台输出
        self.original_print(*args, **kwargs)
        # 同时捕获输出并推送到WebSocket
        try:
            msg = " ".join(map(str, args))
            self.history.append(msg)
            state.broadcast({"type": "print", "message": msg})
        except Exception:
            pass


def setup_logging():
    handlers = [
        logging.StreamHandler(),
        # 嵌入式设备存储有限，改为轮转日志，避免 server.log 无限增长
        RotatingFileHandler(
            LOG_FILE, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8'
        )
    ]

    # 添加WebSocket日志处理器
    ws_handler = WebSocketLogHandler()
    handlers.append(ws_handler)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        # force=True 必需：模块导入阶段（如生成会话密钥）若已触发过 logging，
        # root 会先被装上默认 StreamHandler，此时 basicConfig 是空操作，
        # 文件日志和 WebSocket 日志推送会静默失效。
        force=True
    )

    # 同一个 PrintCapture 实例既接管内置 print，又作为历史记录来源。
    # 原先这里是 PrintCapture(print).history 与 PrintCapture(builtins.print) 两个不同对象，
    # 导致 log_history['print'] 永远是空的历史。
    print_capture = PrintCapture(print)
    # 原地更新已有 dict，保证 ws 首帧读到的始终是同一个容器对象
    state.log_history['log'] = ws_handler.history
    state.log_history['print'] = print_capture.history
    builtins.print = print_capture


def log_startup_banner(host_ip: str, port: int):
    """启动横幅与访问地址提示，单独抽出来便于测试断言内容不变。"""
    logging.info(f"Web服务已启动，访问地址: http://{host_ip}:{port}")
    logging.info(f"请先在Web配置页面完成设置：http://{host_ip}:{port}/config")


__all__ = ['setup_logging', 'WebSocketLogHandler', 'PrintCapture', 'log_startup_banner']
