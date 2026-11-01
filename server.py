#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2RTK NTRIP Server
Copyright (C) 2024 文七 (i@jia.by)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

--------------------------------------------------------------------------
入口壳（约 40 行）：真正的实现都在 rtkserver/ 包里。

之所以保留这个文件名与位置，是为了让 systemd 的
    ExecStart=/opt/2RTKserver/venv/bin/python3 /opt/2RTKserver/server.py
和 reset_config.py 的 `import server` 都不用改，
老设备升级时不需要重装、不需要改服务单元。
--------------------------------------------------------------------------
"""
import os
import sys

# 保证从任意工作目录执行都能找到包（并且优先用同目录下的这一份，
# 避免 venv 里恰好装了同名包时被抢走）。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rtkserver.core.lifecycle import main  # noqa: E402

# ---- 兼容导出 ----
# reset_config.py 依赖 server.reset_config_to_default()；
# 其它脚本/工具历史上也可能 import 过这些名字，一并保留。
from rtkserver.paths import BASE_DIR, CONFIG_DB, SECRET_KEY_FILE  # noqa: E402,F811
from rtkserver.core.config import (DEFAULT_CONFIG, get_config, init_db,  # noqa: E402
                                    reset_config_to_default, save_config)
from rtkserver.security import (get_or_create_secret_key, hash_password,  # noqa: E402
                                 verify_password)
from rtkserver.web.app import create_app  # noqa: E402
# 版本号只从 VERSION 文件读（见 rtkserver/version.py），这里不要再写死。
from rtkserver.version import __version__  # noqa: E402

__all__ = [
    'main', 'create_app', '__version__',
    'BASE_DIR', 'CONFIG_DB', 'SECRET_KEY_FILE', 'DEFAULT_CONFIG',
    'get_config', 'save_config', 'init_db', 'reset_config_to_default',
    'hash_password', 'verify_password', 'get_or_create_secret_key',
]

if __name__ == "__main__":
    sys.exit(main())
