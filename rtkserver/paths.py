# -*- coding: utf-8 -*-
"""落盘路径的唯一来源。

BASE_DIR 取「包目录的上一层」，也就是安装根目录（server.py 所在目录）。
拆包前它是 os.path.dirname(server.py)，拆包后模块下移了一层，
所以这里必须 dirname 两次；写成 dirname(__file__) 会让 DB / 日志 /
会话密钥全部换到 rtkserver/ 子目录里，表现为「配置全丢了、要重新配一遍」。
"""
import os

# <安装目录>/rtkserver/paths.py -> <安装目录>
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_DB = os.path.join(BASE_DIR, '2RTKserver.db')
SECRET_KEY_FILE = os.path.join(BASE_DIR, '.secret_key')
LOG_FILE = os.path.join(BASE_DIR, 'server.log')

TEMPLATES_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
