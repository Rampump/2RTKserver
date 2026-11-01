# -*- coding: utf-8 -*-
"""2RTK NTRIP Server 包。

对外只暴露版本号与包路径常量；具体能力都在子包里，
server.py 只是入口壳，保证 systemd 的 ExecStart 不用改。
"""

__version__ = '2.2.1'
