#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2RTK NTRIP Server 升级 / 卸载执行器（独立进程，自包含单文件）。

为什么必须是一个「独立于服务的进程」：
    Web 进程自己执行升级，第一件事就是 `systemctl stop 2rtkserver`——把自己杀掉，
    后面的复制、启动、回滚全部不会发生，设备会停在一个半新半旧、服务已停的状态。
    所以升级动作必须交给服务之外的进程。由 Web 侧用 `systemd-run` 拉起一个
    transient unit（--collect），它不在 2rtkserver.service 的 cgroup 里，
    停服务不会波及它。

为什么是单文件、只用标准库：
    升级过程中 `rtkserver/` 整个目录会被替换。执行器若 import 项目内模块，
    替换后这些模块的源码已变，后续 import 会拿到新代码甚至 ModuleNotFoundError。
    本文件只依赖 Python 标准库，且由 Web 侧复制到 `<安装目录>/.upgrade/runner.py` 后执行，
    自身不参与替换，全程稳定。

状态协议：
    `<安装目录>/.upgrade/state.json` 是执行器与 Web 前端之间唯一的通道，
    原子写入（tmp + rename）。字段：
        stage     当前阶段（见 STAGE_TEXT）
        progress  0-100
        message   给用户看的一句话
        error     失败原因（成功时为空）
        rolled_back 是否已回退到升级前的版本
        from/to   版本信息
        finished_at 结束时间，非空即代表本次任务已终结
    服务在升级期间会停止，前端靠轮询这个文件（经 Web API）得知进度。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

# ==================== 常量 ====================
STAGE_TEXT = {
    'queued': '排队中',
    'fetching': '正在获取新版本',
    'verifying': '正在校验新版本',
    'backing_up': '正在备份当前版本',
    'stopping': '正在停止服务并释放端口',
    'applying': '正在替换程序文件',
    'deps': '正在更新依赖',
    'starting': '正在启动服务',
    'health': '正在等待服务就绪',
    'success': '升级完成',
    'failed': '升级失败',
    'rolling_back': '正在回退到升级前的版本',
    'rolled_back': '已回退到升级前的版本',
    'uninstalling': '正在卸载',
    'uninstalled': '卸载完成',
}

# 需要从新版本复制到安装目录的条目（与 update.sh 的白名单保持一致）
SYNC_ITEMS = [
    'server.py', 'rtkserver', 'reset_config.py', 'requirements.txt',
    'install.sh', 'uninstall.sh', 'update.sh',
    'README.md', 'INSTALL.md', 'INSTALL.en.md', 'LICENSE',
    'templates', 'static', 'docs', 'tests', 'VERSION',
]

# 升级时绝对不能被覆盖的东西：虚拟环境、配置库、会话密钥、升级工作目录、日志
KEEP_NAMES = {'venv', '2RTKserver.db', '2RTKserver.db.bak', '2RTKserver.db.temp',
              '.secret_key', '.upgrade', 'server.log', '__pycache__'}

BACKUP_KEEP = 3          # 最多保留几份备份
PROBE_TIMEOUT = 15       # 单个升级源探活超时（并发探测，取最先可用的源）
CLONE_TIMEOUT = 300      # git clone 超时（秒）
PORT_FREE_TIMEOUT = 30   # 停服后等端口释放的超时
HEALTH_TIMEOUT = 90      # 启动后等 Web 端口可访问的超时


class Runner:
    def __init__(self, plan_path):
        with open(plan_path, 'r', encoding='utf-8') as f:
            self.plan = json.load(f)
        self.install_dir = self.plan['install_dir'].rstrip('/')
        self.service = self.plan.get('service', '2rtkserver')
        self.work_dir = os.path.join(self.install_dir, '.upgrade')
        self.state_path = os.path.join(self.work_dir, 'state.json')
        self.log_path = os.path.join(self.work_dir, 'upgrade.log')
        # 健康检查固定打 Web 端口。不能拿 ports[0] 糊弄：
        # 端口列表里还有本地 Caster（2102），一旦顺序变了就会去敲错端口。
        self.web_port = int(self.plan.get('web_port') or 5757)
        self.staging = os.path.join(self.work_dir, 'staging')
        self.backup_dir = os.path.join(self.work_dir, 'backups')
        self.python = os.path.join(self.install_dir, 'venv', 'bin', 'python3')
        if not os.path.exists(self.python):
            self.python = sys.executable
        self.state = {}

    # ---------- 状态与日志 ----------
    def log(self, msg, level='INFO'):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
        print(line, flush=True)
        try:
            os.makedirs(self.work_dir, exist_ok=True)
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass

    def set_state(self, stage, progress=None, message=None, **extra):
        self.state.update({
            'stage': stage,
            'stage_text': STAGE_TEXT.get(stage, stage),
            'pid': os.getpid(),
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        })
        if progress is not None:
            self.state['progress'] = progress
        if message is not None:
            self.state['message'] = message
        self.state.update(extra)
        try:
            os.makedirs(self.work_dir, exist_ok=True)
            tmp = self.state_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except OSError as e:
            self.log(f"写状态文件失败: {e}", 'WARN')

    # ---------- 系统命令 ----------
    def run(self, cmd, timeout=60, cwd=None, env=None, check=False):
        """执行命令，返回 (rc, 输出)。异常一律转成 rc=-1，不向上抛。"""
        full_env = os.environ.copy()
        full_env.setdefault('LC_ALL', 'C')
        if env:
            full_env.update(env)
        self.log('执行: ' + ' '.join(cmd) if isinstance(cmd, list) else f'执行: {cmd}')
        try:
            p = subprocess.run(cmd, cwd=cwd, env=full_env, timeout=timeout,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out = p.stdout.decode('utf-8', 'replace').strip()
            if out:
                for line in out.splitlines()[-40:]:
                    self.log('  | ' + line)
            if check and p.returncode != 0:
                self.log(f"命令返回非零: {p.returncode}", 'ERROR')
            return p.returncode, out
        except subprocess.TimeoutExpired:
            self.log(f"命令超时({timeout}s): {cmd}", 'ERROR')
            return -1, 'timeout'
        except Exception as e:  # noqa: BLE001
            self.log(f"命令执行异常: {e}", 'ERROR')
            return -1, str(e)

    # ---------- 端口 ----------
    def port_owners(self, port):
        """返回监听该端口的 pid 列表。"""
        rc, out = self.run(['ss', '-H', '-ltnp', f'sport = :{int(port)}'], timeout=10)
        if rc != 0 or not out:
            return []
        return [int(m) for m in re.findall(r'pid=(\d+)', out)]

    def wait_ports_free(self, ports, timeout=PORT_FREE_TIMEOUT):
        """等端口真正释放——这是升级最容易翻车的地方。

        服务进程退出 ≠ 端口立刻可用：线程还在收尾、socket 还在 TIME_WAIT、
        或者旧进程根本没退干净。不确认就复制文件、再启动，就会在 bind 时拿到
        EADDRINUSE，表现为「升级完打不开网页，但 systemctl 显示 active」。
        """
        ports = [int(p) for p in ports if p]
        deadline = time.time() + timeout
        while time.time() < deadline:
            busy = {p: self.port_owners(p) for p in ports}
            busy = {p: pids for p, pids in busy.items() if pids}
            if not busy:
                self.log(f"端口已全部释放: {ports}")
                return True
            time.sleep(0.5)
        # 超时：说明有残留进程还占着，直接按 pid 强清
        self.log(f"端口仍被占用 {busy}，开始强制清理", 'WARN')
        for port, pids in busy.items():
            for pid in pids:
                self.run(['kill', '-TERM', str(pid)], timeout=10)
        time.sleep(3)
        busy2 = {p: self.port_owners(p) for p in ports}
        busy2 = {p: pids for p, pids in busy2.items() if pids}
        for port, pids in busy2.items():
            for pid in pids:
                self.run(['kill', '-9', str(pid)], timeout=10)
        time.sleep(1.5)
        left = {p: self.port_owners(p) for p in ports}
        left = {p: pids for p, pids in left.items() if pids}
        if left:
            self.log(f"强制清理后仍被占用: {left}", 'ERROR')
            return False
        self.log("强制清理后端口已释放")
        return True

    # ---------- 服务 ----------
    def stop_service(self):
        # stop 之后必须再确认一次进程真的没了：systemd 的 stop 在超时后
        # 会升级为 SIGKILL，但 20 秒的 TimeoutStopSec 内它仍返回成功。
        self.run(['systemctl', 'stop', self.service], timeout=90)
        for _ in range(40):
            rc, out = self.run(['systemctl', 'is-active', self.service], timeout=10)
            if out.strip() not in ('active', 'activating', 'deactivating'):
                self.log(f"服务状态: {out.strip()}")
                return True
            time.sleep(0.5)
        return False

    def start_service(self):
        rc, _ = self.run(['systemctl', 'start', self.service], timeout=90)
        return rc == 0

    def health_check(self, port, timeout=HEALTH_TIMEOUT):
        """等 Web 端口返回 HTTP 响应，才算真的起来了。"""
        deadline = time.time() + timeout
        url = f'http://127.0.0.1:{int(port)}/login'
        while time.time() < deadline:
            rc, out = self.run(['systemctl', 'is-active', self.service], timeout=10)
            if out.strip() == 'failed':
                self.log("服务处于 failed 状态", 'ERROR')
                return False
            try:
                with urllib.request.urlopen(url, timeout=4) as r:
                    if r.status in (200, 302):
                        self.log(f"健康检查通过（HTTP {r.status}）")
                        return True
            except urllib.error.HTTPError as e:
                if e.code in (200, 302, 401, 403):
                    self.log(f"健康检查通过（HTTP {e.code}）")
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
        self.log(f"健康检查超时（{timeout}s 内 Web 端口未就绪）", 'ERROR')
        return False

    # ---------- 获取新版本 ----------
    def probe_sources(self, sources, branch):
        """并发探测所有源，返回 {url: (ok, commit, err)}。

        必须并发：国内设备访问 GitHub 往往是"能连上但要等到超时"，
        串行探测会让用户白等 2-3 分钟才轮到可用的镜像。
        """
        import concurrent.futures

        def one(url):
            env = {**os.environ, 'LC_ALL': 'C', 'GIT_TERMINAL_PROMPT': '0'}
            try:
                p = subprocess.run(['git', 'ls-remote', '--heads', url, branch],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=PROBE_TIMEOUT, env=env)
                out = p.stdout.decode('utf-8', 'replace').strip()
                if p.returncode == 0 and out:
                    return url, True, out.split()[0][:12], ''
                err = p.stderr.decode('utf-8', 'replace').strip().splitlines()
                return url, False, '', (err[-1] if err else f'无 {branch} 分支')
            except subprocess.TimeoutExpired:
                return url, False, '', f'连接超时（{PROBE_TIMEOUT}s）'
            except FileNotFoundError:
                return url, False, '', '系统未安装 git'
            except Exception as e:  # noqa: BLE001
                return url, False, '', str(e)

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(sources))) as ex:
            for url, ok, commit, err in ex.map(one, sources):
                results[url] = (ok, commit, err)
                self.log(f"源探测 {url}: {'可用 ' + commit if ok else '不可用 ' + err}")
        return results

    def fetch(self):
        self.set_state('fetching', 10, '正在从远程仓库获取新版本…',
                       attempted_sources=self.plan.get('sources', []))
        sources = [s for s in self.plan.get('sources', []) if s]
        branch = self.plan.get('branch') or 'main'
        if os.path.isdir(self.staging):
            shutil.rmtree(self.staging, ignore_errors=True)
        os.makedirs(self.staging, exist_ok=True)

        probed = self.probe_sources(sources, branch)
        errors = []
        for url in sources:
            ok, commit, err = probed.get(url, (False, '', '未探测'))
            if not ok:
                errors.append(f'{url} → {err}')
                continue
            self.log(f"使用源: {url} @ {commit}")
            rc, _ = self.run(
                ['git', '-c', 'http.lowSpeedLimit=1000', '-c', 'http.lowSpeedTime=30',
                 'clone', '--depth', '1', '--branch', branch, url, self.staging],
                timeout=CLONE_TIMEOUT)
            if rc != 0:
                shutil.rmtree(self.staging, ignore_errors=True)
                os.makedirs(self.staging, exist_ok=True)
                errors.append(f'{url} → clone 失败')
                continue
            date = ''
            rc, out = self.run(['git', '-C', self.staging, 'log', '-1', '--format=%ci'], timeout=20)
            if rc == 0:
                date = out.strip()
            self.state['source'] = url
            self.state['new_commit'] = commit
            self.state['new_date'] = date
            self.log(f"获取成功: {url} @ {commit} ({date})")
            return True, url, commit, date
        self.state['fetch_errors'] = errors
        self.log('所有升级源均不可用: ' + '; '.join(errors), 'ERROR')
        return False, None, None, None

    # ---------- 校验 ----------
    def verify(self):
        self.set_state('verifying', 22, '正在校验新版本的完整性…')
        required = ['server.py', os.path.join('rtkserver', '__init__.py'),
                    'templates', 'static']
        for item in required:
            if not os.path.exists(os.path.join(self.staging, item)):
                self.log(f"新版本缺少必要文件: {item}", 'ERROR')
                return False, f'新版本缺少必要文件: {item}'

        # 1) 语法编译：整包编译一遍，语法错误在这里就拦住，绝不带上线
        py = self.python
        rc, out = self.run([py, '-m', 'compileall', '-q',
                            os.path.join(self.staging, 'rtkserver'),
                            os.path.join(self.staging, 'server.py')], timeout=180)
        if rc != 0:
            return False, '新版本存在语法错误'

        # 2) 冒烟测试：能 import 出 Flask app 工厂，说明依赖关系没断
        rc, out = self.run(
            [py, '-c', 'import server; print("ok")'],
            timeout=120, cwd=self.staging,
            env={'PYTHONPATH': self.staging})
        if rc != 0 or 'ok' not in out:
            return False, '新版本无法导入（依赖或包结构有问题）'

        # 3) 契约测试：仓库自带 41 条断言，覆盖路由表/默认配置/解帧等
        tests_dir = os.path.join(self.staging, 'tests')
        if os.path.isdir(tests_dir):
            rc, out = self.run(
                [py, '-m', 'unittest', 'discover', '-s', 'tests', '-t', '.', '-q'],
                timeout=300, cwd=self.staging,
                env={'PYTHONPATH': self.staging})
            if rc != 0:
                return False, '新版本未通过自带测试'
        return True, ''

    # ---------- 备份 ----------
    def backup(self):
        self.set_state('backing_up', 30, '正在备份当前版本…')
        os.makedirs(self.backup_dir, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        tar_path = os.path.join(self.backup_dir, f'backup-{stamp}.tar.gz')

        try:
            with tarfile.open(tar_path, 'w:gz') as tar:
                for name in sorted(os.listdir(self.install_dir)):
                    if name in KEEP_NAMES:
                        continue
                    tar.add(os.path.join(self.install_dir, name), arcname=name)
        except Exception as e:  # noqa: BLE001
            self.log(f"备份失败: {e}", 'ERROR')
            return False, str(e)

        # 配置库单独留一份明文备份：回滚时哪怕 tar 出问题也能把配置捞回来
        db = os.path.join(self.install_dir, '2RTKserver.db')
        db_bak = ''
        if os.path.exists(db):
            db_bak = os.path.join(self.backup_dir, f'db-{stamp}.db')
            try:
                shutil.copy2(db, db_bak)
            except OSError as e:
                self.log(f"配置库备份失败: {e}", 'WARN')
                db_bak = ''

        size = os.path.getsize(tar_path) if os.path.exists(tar_path) else 0
        self.log(f"备份完成: {tar_path} ({size} 字节)")
        self.state.update({'backup_tar': tar_path, 'backup_db': db_bak})
        self.save_state()

        # 只保留最近 N 份，避免反复升级把 1.9G 的根分区塞满
        backups = sorted(f for f in os.listdir(self.backup_dir) if f.startswith('backup-'))
        for old in backups[:-BACKUP_KEEP]:
            try:
                os.remove(os.path.join(self.backup_dir, old))
            except OSError:
                pass
        return True, tar_path

    def save_state(self):
        try:
            tmp = self.state_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ---------- 应用 ----------
    def apply(self):
        self.set_state('applying', 55, '正在替换程序文件…')
        # rtkserver/ 必须先删再复制：模块会增删改名，直接覆盖会留下指向已删除源码的
        # .pyc，表现为「改了代码不生效」或随机 ModuleNotFoundError
        for name in ('rtkserver',):
            p = os.path.join(self.install_dir, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
        for root, dirs, _files in os.walk(self.install_dir):
            if '__pycache__' in dirs:
                shutil.rmtree(os.path.join(root, '__pycache__'), ignore_errors=True)
                dirs.remove('__pycache__')

        copied = []
        for item in SYNC_ITEMS:
            src = os.path.join(self.staging, item)
            if not os.path.exists(src):
                continue
            dst = os.path.join(self.install_dir, item)
            try:
                if os.path.isdir(src):
                    if os.path.isdir(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                copied.append(item)
            except Exception as e:  # noqa: BLE001
                self.log(f"复制 {item} 失败: {e}", 'ERROR')
                return False, f'复制 {item} 失败: {e}'
        self.log(f"已替换 {len(copied)} 个条目")
        self.state['copied'] = copied
        return True, ''

    def write_version(self):
        ver = {
            'commit': self.state.get('new_commit', ''),
            'date': self.state.get('new_date', ''),
            'source': self.state.get('source', ''),
            'installed_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'channel': 'upgrade',
        }
        try:
            with open(os.path.join(self.install_dir, '.version'), 'w', encoding='utf-8') as f:
                json.dump(ver, f, ensure_ascii=False)
        except OSError as e:
            self.log(f"写版本文件失败: {e}", 'WARN')

    def install_deps(self):
        self.set_state('deps', 70, '正在更新 Python 依赖…')
        req = os.path.join(self.install_dir, 'requirements.txt')
        if not os.path.exists(req):
            return True, ''
        pip = os.path.join(self.install_dir, 'venv', 'bin', 'pip')
        if not os.path.exists(pip):
            self.log("未找到 venv 内的 pip，跳过依赖安装", 'WARN')
            return True, ''
        rc, _ = self.run([pip, 'install', '-r', req], timeout=600)
        if rc != 0:
            self.log("常规安装失败，尝试 PIP_BREAK_SYSTEM_PACKAGES=1", 'WARN')
            rc, _ = self.run([pip, 'install', '-r', req], timeout=600,
                             env={'PIP_BREAK_SYSTEM_PACKAGES': '1'})
        if rc != 0:
            return False, '依赖安装失败'
        return True, ''

    # ---------- 回滚 ----------
    def rollback(self, reason):
        self.set_state('rolling_back', 85, f'升级失败，正在回退：{reason}', error=reason)
        tar_path = self.state.get('backup_tar')
        ok = True
        if tar_path and os.path.exists(tar_path):
            try:
                with tarfile.open(tar_path, 'r:gz') as tar:
                    tar.extractall(self.install_dir)
                self.log("已从备份还原程序文件")
            except Exception as e:  # noqa: BLE001
                self.log(f"还原备份失败: {e}", 'ERROR')
                ok = False
        else:
            self.log("没有可用备份，无法回退", 'ERROR')
            ok = False

        db_bak = self.state.get('backup_db')
        db = os.path.join(self.install_dir, '2RTKserver.db')
        if db_bak and os.path.exists(db_bak):
            try:
                shutil.copy2(db_bak, db)
                self.log("已还原配置库")
            except OSError as e:
                self.log(f"还原配置库失败: {e}", 'WARN')

        self.install_deps()
        self.start_service()
        healthy = self.health_check(self.web_port, timeout=60)
        stage = 'rolled_back' if (ok and healthy) else 'failed'
        old = self.state.get('from_version') or self.state.get('old_commit') or '升级前的版本'
        if healthy:
            msg = f'升级失败，已自动退回 {old}，服务运行正常。'
        else:
            msg = f'升级失败，回退后服务仍未就绪（{old}），请 SSH 登录检查。'
        self.set_state(stage, 100, msg, rolled_back=ok,
                       finished_at=time.strftime('%Y-%m-%d %H:%M:%S'))
        self.log(f"回退结束: stage={stage}, healthy={healthy}")
        return ok and healthy

    # ---------- 主流程 ----------
    def do_upgrade(self):
        started = time.strftime('%Y-%m-%d %H:%M:%S')
        self.state = {
            'mode': 'upgrade', 'stage': 'queued', 'stage_text': STAGE_TEXT['queued'],
            'progress': 0, 'message': '任务已受理', 'error': '', 'rolled_back': False,
            'started_at': started, 'finished_at': '', 'pid': os.getpid(),
            'from_version': self.plan.get('from_version', ''),
            'old_commit': self.plan.get('from_commit', ''),
        }
        self.save_state()
        self.log("=== 升级任务开始 ===")
        self.log("计划: " + json.dumps({k: v for k, v in self.plan.items() if k != 'plan_path'},
                                       ensure_ascii=False))

        # 给 Web 一点时间把「已启动」的响应发回浏览器，再动手停服务
        time.sleep(int(self.plan.get('delay', 2)))

        ok, url, commit, date = self.fetch()
        if not ok:
            self.set_state('failed', 100, '所有升级源均不可用，未做任何改动',
                           error='; '.join(self.state.get('fetch_errors', [])),
                           finished_at=time.strftime('%Y-%m-%d %H:%M:%S'))
            return 1

        ok, err = self.verify()
        if not ok:
            self.set_state('failed', 100, f'{err}，已中止升级（当前版本未受影响，服务仍在运行）',
                           error=err, finished_at=time.strftime('%Y-%m-%d %H:%M:%S'))
            return 1

        ok, err = self.backup()
        if not ok:
            self.set_state('failed', 100, '备份失败，为安全起见已中止：' + err,
                           error=err, finished_at=time.strftime('%Y-%m-%d %H:%M:%S'))
            return 1

        self.set_state('stopping', 40, '正在停止服务并等待端口释放…')
        self.run(['systemctl', 'stop', self.service], timeout=120)
        ports = self.plan.get('ports') or [5757]
        if not self.wait_ports_free(ports, PORT_FREE_TIMEOUT):
            self.set_state('failed', 100,
                           f'端口 {ports} 在 {PORT_FREE_TIMEOUT}s 内未能释放，已中止（旧版本仍在）',
                           error='port busy', finished_at=time.strftime('%Y-%m-%d %H:%M:%S'))
            self.start_service()
            return 1

        ok, err = self.apply()
        if not ok:
            return 0 if self.rollback(err) else 1

        self.write_version()

        ok, err = self.install_deps()
        if not ok:
            return 0 if self.rollback(err) else 1

        self.set_state('starting', 85, '正在启动服务…')
        self.start_service()
        self.set_state('health', 92, '正在等待服务就绪…')
        if not self.health_check(self.web_port):
            return 0 if self.rollback('新版本启动后 Web 端口无响应') else 1

        self.set_state('success', 100, f'升级完成：{commit}',
                       finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                       new_version=commit)
        self.log("=== 升级成功 ===")
        return 0

    def do_uninstall(self):
        self.state = {
            'mode': 'uninstall', 'stage': 'uninstalling', 'stage_text': STAGE_TEXT['uninstalling'],
            'progress': 10, 'message': '正在卸载…', 'error': '', 'rolled_back': False,
            'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'finished_at': '',
            'pid': os.getpid(),
        }
        self.save_state()
        self.log("=== 卸载任务开始 ===")
        time.sleep(int(self.plan.get('delay', 2)))

        # 配置库先备份到 /root，卸载删掉的是一整套连接参数和管理员口令，不能悄悄丢弃
        db = os.path.join(self.install_dir, '2RTKserver.db')
        if os.path.exists(db) and self.plan.get('backup_db', True):
            dst = f"/root/2RTKserver-{time.strftime('%Y%m%d-%H%M')}.db"
            try:
                shutil.copy2(db, dst)
                self.log(f"配置库已备份到 {dst}")
                self.state['db_backup'] = dst
            except OSError as e:
                self.log(f"配置库备份失败: {e}", 'WARN')

        self.run(['systemctl', 'stop', self.service], timeout=120)
        self.run(['systemctl', 'disable', self.service], timeout=60)
        ports = self.plan.get('ports') or [5757]
        self.wait_ports_free(ports, PORT_FREE_TIMEOUT)

        self.state['progress'] = 55
        self.save_state()
        self.run(['rm', '-f', f'/etc/systemd/system/{self.service}.service'], timeout=20)
        self.run(['systemctl', 'daemon-reload'], timeout=60)
        self.run(['rm', '-f', '/usr/local/bin/2rtkserver'], timeout=20)

        # 删自己所在的目录：本进程在 <安装目录>/.upgrade/ 下运行，
        # 直接 rm -rf 会连带删掉正在执行的脚本。先把自身复制到 /tmp 再从这个副本干活，
        # 但此刻已经在运行了，Linux 允许删除正在执行的文件（inode 仍被引用），
        # 所以这里可以安全地整目录删除。
        self.state['progress'] = 80
        self.save_state()
        self.run(['rm', '-rf', self.install_dir], timeout=120)
        removed = not os.path.exists(self.install_dir)
        self.log(f"安装目录已删除: {removed}")
        # 状态文件随目录一起没了，这就是"最后一个状态"——前端看到 404 即代表卸载完成
        return 0 if removed else 1


def main():
    plan_path = None
    for i, a in enumerate(sys.argv):
        if a == '--plan' and i + 1 < len(sys.argv):
            plan_path = sys.argv[i + 1]
    if not plan_path or not os.path.exists(plan_path):
        print('用法: runner.py --plan <plan.json>', file=sys.stderr)
        return 2
    r = Runner(plan_path)
    mode = r.plan.get('mode', 'upgrade')
    try:
        if mode == 'uninstall':
            return r.do_uninstall()
        return r.do_upgrade()
    except Exception as e:  # noqa: BLE001
        r.log(f"执行器异常: {e}", 'ERROR')
        import traceback
        r.log(traceback.format_exc(), 'ERROR')
        try:
            r.set_state('failed', 100, f'执行器异常：{e}', error=str(e),
                        finished_at=time.strftime('%Y-%m-%d %H:%M:%S'))
            if mode != 'uninstall':
                r.start_service()
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
