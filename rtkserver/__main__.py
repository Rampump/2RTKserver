# -*- coding: utf-8 -*-
"""支持 `python -m rtkserver` 启动（等价于 python server.py）。"""
import sys

from .core.lifecycle import main

if __name__ == '__main__':
    sys.exit(main())
