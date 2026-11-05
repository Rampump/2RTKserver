# -*- coding: utf-8 -*-
"""2RTK NTRIP Server 包。

对外只暴露版本号与包路径常量；具体能力都在子包里，
server.py 只是入口壳，保证 systemd 的 ExecStart 不用改。

版本号唯一来源是根目录的 VERSION 文件（见 rtkserver/version.py），
这里**不要**再写死任何版本字符串。

为什么包名叫 `rtkserver` 而不是 `2rtkserver`
--------------------------------------------
Python 的模块名必须是合法标识符，**不能以数字开头**：
写 `from 2rtkserver.core.config import ...` 在语法解析阶段就会
`SyntaxError: invalid decimal literal`。`importlib.import_module('2rtkserver')`
虽然能加载，但全项目每一处 import 都得改成动态导入，
IDE 跳转、静态检查、`python -m` 全部失效，得不偿失。

所以目录名与包名去掉开头的数字。安装目录仍是 `/opt/2RTKserver`、
systemd 服务名仍是 `2rtkserver`，这些都不受影响。
"""

from .version import __version__

__all__ = ['__version__']
