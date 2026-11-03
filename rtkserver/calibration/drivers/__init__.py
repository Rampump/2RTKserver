# -*- coding: utf-8 -*-
"""标定驱动包。

新增型号的成本 = 一个文件 + 一张 `CMD` 表 + 一个 `parse_line` 覆盖，
`service.py` 一行都不用改（方案 §5.1.1）。注册靠 `driver_manager.register()`
装饰器，不要在别处维护第二份型号清单。
"""
