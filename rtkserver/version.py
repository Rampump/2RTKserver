# -*- coding: utf-8 -*-
"""程序版本号的唯一来源：安装根目录下的 ``VERSION`` 文件。

为什么不再在代码里写 ``__version__ = "主.次.修"``：
    版本号原先散落在四个地方（server.py、rtkserver/__init__.py、控制台 LOGO、
    mDNS 的 TXT 记录），发版时只要漏改一处，就会出现「网页显示 2.2.1、
    mDNS 写着 2.1.8」这种自相矛盾的信息，排查升级问题时极其误导。
    现在只改根目录的 VERSION 一个文件，其余地方一律从这里读。

本模块只用标准库（os / re），不 import 任何项目内模块，所以 constants、
security 这类底层模块可以安全导入，不会形成循环依赖。升级执行器 runner.py
是自包含单文件，不能 import 本模块，它直接读同一份 VERSION 文件。
"""
import os
import re

# <安装目录>/rtkserver/version.py -> <安装目录>
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(BASE_DIR, 'VERSION')

# VERSION 缺失或被写坏时的占位值。刻意选 0.0.0 这种"不可能是真实发版号"的值：
# 前端看到它就知道是打包漏了 VERSION，而不是真的装了个 1.x 的老版本。
UNKNOWN_VERSION = '0.0.0'

_SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$')


def read_version(path=None):
    """读 VERSION 并校验格式；任何异常都退化为 UNKNOWN_VERSION，绝不抛。"""
    path = path or VERSION_FILE
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read().strip()
    except OSError:
        return UNKNOWN_VERSION
    # 只取第一行：有人用编辑器保存时可能带上空行或 BOM 之外的杂物
    text = text.splitlines()[0].strip() if text else ''
    return text if _SEMVER_RE.match(text) else UNKNOWN_VERSION


def version_tuple(text=None):
    """返回 (major, minor, patch)，供比较大小用；非法版本当 (0, 0, 0)。"""
    text = text or __version__
    try:
        core = text.split('-')[0].split('+')[0]
        major, minor, patch = (int(x) for x in core.split('.')[:3])
        return major, minor, patch
    except (ValueError, AttributeError):
        return 0, 0, 0


__version__ = read_version()

__all__ = ['__version__', 'VERSION_FILE', 'UNKNOWN_VERSION', 'read_version',
           'version_tuple']
