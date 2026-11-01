# -*- coding: utf-8 -*-
"""Web 侧的升级/卸载封装。

这里只做三件事：准备计划、把执行器拉起来、读执行器留下的状态。
真正的升级动作一行都不在本进程里做——否则 `systemctl stop` 会先把 Web 自己杀掉。
"""
import json
import logging
import os
import shutil
import subprocess
import time

from .. import __version__ as APP_VERSION
from .. import paths
from ..core.config import get_config
from ..core.constants import WEB_PORT
from . import sources

WORK_DIR = os.path.join(paths.BASE_DIR, '.upgrade')
STATE_FILE = os.path.join(WORK_DIR, 'state.json')
PLAN_FILE = os.path.join(WORK_DIR, 'plan.json')
LOG_FILE = os.path.join(WORK_DIR, 'upgrade.log')
VERSION_FILE = os.path.join(paths.BASE_DIR, '.version')
RUNNER_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runner.py')
RUNNER_DST = os.path.join(WORK_DIR, 'runner.py')
SERVICE_NAME = '2rtkserver'
UNIT_NAME = '2rtk-upgrader'

ACTIVE_STAGES = {
    'queued', 'fetching', 'verifying', 'backing_up', 'stopping', 'applying',
    'deps', 'starting', 'health', 'rolling_back', 'uninstalling',
}


# ==================== 版本 ====================
def current_version():
    """当前安装版本。.version 由 install.sh / update.sh / 升级执行器写入。

    app 是代码包自报的语义化版本（rtkserver.__version__），commit 是实际装的提交。
    两者都要给前端：只报 commit 时用户看不出「这版到底新不新」，
    只报版本号又无法确认装的是不是同一个提交。
    """
    info = {'app': APP_VERSION, 'commit': '', 'date': '', 'installed_at': '',
            'source': '', 'channel': ''}
    try:
        with open(VERSION_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in ('commit', 'date', 'installed_at', 'source', 'channel'):
                info[key] = data.get(key, '') or ''
    except (OSError, ValueError):
        pass
    return info


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


# ==================== 状态 ====================
def read_state():
    """读执行器留下的状态。文件不存在时返回 idle。"""
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {'stage': 'idle', 'stage_text': '空闲', 'progress': 0, 'message': ''}
    except (OSError, ValueError):
        return {'stage': 'idle', 'stage_text': '空闲', 'progress': 0, 'message': ''}
    data.setdefault('stage', 'idle')
    data['active'] = data.get('stage') in ACTIVE_STAGES and not data.get('finished_at')
    # 执行器是独立进程，崩掉时状态会永远停在中间态。
    # 用 pid 兜底判定，避免前端一直显示"升级中"、按钮永远点不动。
    if data['active'] and data.get('pid') and not _pid_alive(data['pid']):
        data['active'] = False
        data['stage'] = 'failed'
        data['stage_text'] = '升级中断'
        data['message'] = '升级进程已退出但未写入最终状态，请查看 .upgrade/upgrade.log'
    return data


def _tail_log(nbytes=6000):
    try:
        size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            if size > nbytes:
                f.seek(size - nbytes)
            return f.read()
    except OSError:
        return ''


# ==================== 端口 ====================
def managed_ports():
    """升级前必须确认释放的端口，Web 端口永远排第一。

    5757 是 Web 端口；本地 Caster 的端口是可配置的（默认 2102，有人会改成 2101）。
    升级时如果这些端口还被旧进程占着，新进程 bind 失败，页面就打不开了。

    顺序很重要：调用方会拿返回值的第一项做健康检查。
    这里**不能**用 sorted()，否则 {5757, 2102} 会排成 [2102, 5757]，
    健康检查就被发到本地 Caster 上——那边把 /login 当成不存在的挂载点直接断开，
    表现为「服务明明起来了，却判定升级失败并回滚」。
    """
    web_port = int(WEB_PORT)
    rest = set()
    try:
        cfg = get_config()
    except Exception:  # noqa: BLE001
        return [web_port]
    if str(cfg.get('local_caster_enabled', '0')) == '1':
        try:
            rest.add(int(cfg.get('local_caster_port') or 0))
        except (TypeError, ValueError):
            pass
    rest.discard(web_port)
    rest.discard(0)
    return [web_port] + sorted(rest)


# ==================== 远端检查 ====================
def _ls_remote(url, branch, timeout=25):
    """返回 (ok, commit, err)。只读操作，不改任何本地文件。"""
    try:
        p = subprocess.run(
            ['git', 'ls-remote', '--heads', url, branch],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
            env={**os.environ, 'LC_ALL': 'C', 'GIT_TERMINAL_PROMPT': '0'})
        out = p.stdout.decode('utf-8', 'replace').strip()
        if p.returncode != 0:
            return False, '', out.splitlines()[-1] if out else 'git 返回非零'
        if not out:
            return False, '', f'远端没有 {branch} 分支'
        return True, out.split()[0][:12], ''
    except subprocess.TimeoutExpired:
        return False, '', f'连接超时（{timeout}s）'
    except FileNotFoundError:
        return False, '', '系统没有安装 git'
    except Exception as e:  # noqa: BLE001
        return False, '', str(e)


def check_updates():
    """并发探测各升级源，返回每个源的最新提交与「是否有更新」。

    并发不是为了快，是为了别让用户干等：国内访问 GitHub 常常是"能连上但要等到超时"，
    串行探测会让「检查更新」按钮转上半分钟才出结果。
    """
    import concurrent.futures

    conf = sources.resolved()
    cur = current_version()
    cur_commit = cur.get('commit', '')
    items = conf['sources']

    def probe(item):
        key, label, url = item
        ok, commit, err = _ls_remote(url, conf['branch'])
        return {
            'key': key,
            'label': label,
            'url': url,
            'ok': ok,
            'commit': commit,
            'error': err,
            # 没有本地版本信息时不能断定"有更新"，宁可让用户自己决定
            'has_update': bool(ok and cur_commit and commit and commit != cur_commit),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(items))) as ex:
        results = list(ex.map(probe, items))
    reachable = [r for r in results if r['ok']]
    return {
        'current': cur,
        'branch': conf['branch'],
        'sources': results,
        'reachable': len(reachable) > 0,
        'has_update': any(r['has_update'] for r in reachable),
    }


# ==================== 启动执行器 ====================
def _write_plan(plan):
    os.makedirs(WORK_DIR, exist_ok=True)
    tmp = PLAN_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PLAN_FILE)


def _launch(plan, mode):
    """用 systemd-run 拉一个独立 transient unit。

    关键：绝不能用 subprocess.Popen 直接起。升级流程第一步就是
    `systemctl stop 2rtkserver`，而 Popen 出来的子进程和 Web 进程在同一个
    cgroup 里，会跟着一起被杀，升级中途夭折。
    """
    os.makedirs(WORK_DIR, exist_ok=True)
    shutil.copy2(RUNNER_SRC, RUNNER_DST)
    _write_plan(plan)

    python = os.path.join(paths.BASE_DIR, 'venv', 'bin', 'python3')
    if not os.path.exists(python):
        python = 'python3'

    # 上一轮的 unit 可能还留着失败的记录，先清掉，否则同名启动会报 already exists
    subprocess.run(['systemctl', 'reset-failed', UNIT_NAME],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)

    cmd = [
        'systemd-run',
        f'--unit={UNIT_NAME}',
        '--collect',
        '--property=Type=oneshot',
        '--property=Restart=no',
        '--property=StandardOutput=journal',
        '--property=StandardError=journal',
        '--property=WorkingDirectory=' + paths.BASE_DIR,
        python, RUNNER_DST, '--plan', PLAN_FILE,
    ]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        out = p.stdout.decode('utf-8', 'replace').strip()
        if p.returncode != 0:
            logging.error(f"拉起升级执行器失败: {out}")
            return False, out or 'systemd-run 返回非零'
        logging.info(f"升级执行器已拉起（{mode}）: {out}")
        return True, out
    except FileNotFoundError:
        return False, '系统缺少 systemd-run，无法在服务之外安全执行'
    except subprocess.TimeoutExpired:
        return False, 'systemd-run 超时'


def start_upgrade(source_key=None):
    """发起升级。返回 (ok, message)。真正结果由前端轮询状态获得。"""
    st = read_state()
    if st.get('active'):
        return False, '已有升级任务正在进行，请等待完成'

    conf = sources.resolved()
    chosen = sources.source_by_key(source_key)
    if chosen:
        src_list = [chosen]
    else:
        src_list = [url for _k, _l, url in conf['sources']]
    if not src_list:
        return False, '没有可用的升级源'

    cur = current_version()
    plan = {
        'mode': 'upgrade',
        'install_dir': paths.BASE_DIR,
        'service': SERVICE_NAME,
        'sources': src_list,
        'branch': conf['branch'],
        'ports': managed_ports(),
        'web_port': int(WEB_PORT),
        'from_version': cur.get('commit', ''),
        'from_commit': cur.get('commit', ''),
        'delay': 3,
    }
    ok, msg = _launch(plan, 'upgrade')
    if ok:
        # 立刻写一个"排队中"状态，避免前端第一次轮询读到上一轮的 success
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'mode': 'upgrade', 'stage': 'queued', 'stage_text': '排队中',
                    'progress': 2, 'message': '升级任务已受理，即将停止服务…',
                    'error': '', 'rolled_back': False, 'finished_at': '',
                    'from_version': cur.get('commit', ''),
                    'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                }, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
    return ok, msg


def start_uninstall(backup_db=True):
    st = read_state()
    if st.get('active'):
        return False, '有任务正在执行，请稍后再试'
    plan = {
        'mode': 'uninstall',
        'install_dir': paths.BASE_DIR,
        'service': SERVICE_NAME,
        'ports': managed_ports(),
        'web_port': int(WEB_PORT),
        'backup_db': bool(backup_db),
        'delay': 3,
    }
    return _launch(plan, 'uninstall')


def snapshot():
    """给前端的一揽子信息。"""
    return {
        'version': current_version(),
        'state': read_state(),
        'ports': managed_ports(),
        'sources': [
            {'key': k, 'label': label, 'url': url}
            for k, label, url in sources.resolved()['sources']
        ],
        'branch': sources.resolved()['branch'],
        'log_tail': _tail_log(),
    }
