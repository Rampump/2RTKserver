# -*- coding: utf-8 -*-
"""升级源地址。

默认值来自程序自己的 git 仓库（origin: https://github.com/Rampump/2RTKserver.git）。
国内设备访问 GitHub 经常不通（实测在 Armbian 上 curl github.com 返回 000），
所以这里同时给出 GitCode 镜像地址，并按「GitCode 优先、GitHub 兜底」的顺序尝试。

覆盖方式（不改代码）：
    <安装目录>/.upgrade/sources.json
    {
      "gitcode": "https://gitcode.com/<用户名>/2RTKserver.git",
      "github":  "https://github.com/Rampump/2RTKserver.git",
      "branch":  "main",
      "order":   ["gitcode", "github"]
    }
设备上没有这个文件时用下面的默认值。
"""
import json
import os

from .. import paths

# 两个仓库的仓库名确实不同：GitHub 叫 2RTKserver，GitCode 叫 NTRIPserver。
# 不强行要求同名——镜像站点常被重建，名字对不上是常态，代码里按地址区分即可，
# 用户也可以在 .upgrade/sources.json 里改成自己的镜像地址。
GITHUB_URL = 'https://github.com/Rampump/2RTKserver.git'
GITCODE_URL = 'https://gitcode.com/rampump/NTRIPserver.git'
DEFAULT_BRANCH = 'main'

OVERLAY_FILE = os.path.join(paths.BASE_DIR, '.upgrade', 'sources.json')

LABELS = {
    'gitcode': 'GitCode 镜像（国内可达）',
    'github': 'GitHub 官方源',
}


def _overlay():
    try:
        with open(OVERLAY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolved():
    """返回解析后的源配置：{'branch': str, 'sources': [(key, label, url), ...]}"""
    ov = _overlay()
    urls = {
        'gitcode': (ov.get('gitcode') or GITCODE_URL).strip(),
        'github': (ov.get('github') or GITHUB_URL).strip(),
    }
    order = ov.get('order') or ['gitcode', 'github']
    out = []
    for key in order:
        if key in urls and urls[key]:
            out.append((key, LABELS.get(key, key), urls[key]))
    return {'branch': (ov.get('branch') or DEFAULT_BRANCH).strip(), 'sources': out}


def source_by_key(key):
    """按前端选择的源返回 URL；未指定时返回 None（表示按顺序全试）。"""
    if not key or key == 'auto':
        return None
    for k, _label, url in resolved()['sources']:
        if k == key:
            return url
    return None
