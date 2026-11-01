# -*- coding: utf-8 -*-
"""2RTK NTRIP Server 包。

对外只暴露版本号与包路径常量；具体能力都在子包里，
server.py 只是入口壳，保证 systemd 的 ExecStart 不用改。

版本号唯一来源是根目录的 VERSION 文件（见 rtkserver/version.py），
这里**不要**再写死任何版本字符串。
"""

from .version import __version__

__all__ = ['__version__']
