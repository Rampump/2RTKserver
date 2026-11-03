# -*- coding: utf-8 -*-
"""标定前配置备份 + 一键还原（方案 §0.5.5 S4 / §8.4）。

为什么必须有这一层：
    写坐标这一步一旦下发，模块的**内存态**就已经变了。后面任何一步失败
    （报文配不上、SAVEPAR 超时、重启后没生效），模块都停在"半个基准站"
    状态上 —— 对下游流动站来说，这比原来的移动站状态糟糕得多。
    有备份才能说"失败了就退回去"，而不是"请你手动改回来"。

存哪儿、存几份：
    `.calibration/backup.json`，**只留最近一次**。理由：还原是"最后一次写入
    覆盖"的操作，历史备份留着只会在"我到底还原到哪一版"上添乱；
    需要追溯历史的话，`calibration_records` 里每次写入的 apc/arp 都在。

什么时候写：
    在**任何写命令之前**（DETECT 之后、WRITE_COORD 之前）。写完立刻落盘 fsync，
    因为紧接着就是改模块 —— 中间断电的话，用户下次开机能从 lease.json
    看出"上次停在 WRITE_COORD"，再结合 backup.json 还原。

失败不阻断：
    备份读不全（某条查询 ERROR）只记警告。少一个报文速率不该让整个标定做不了，
    但界面必须知道"这次备份不完整，还原能力打折"（方案 §0.5.5 S4）。
"""
import logging
import os
import time

from . import workspace

BACKUP_NAME = 'backup.json'
BACKUP_VERSION = 1

# 需要备份/还原的报文速率。
# **MSM 必须逐星座存**（107X=GPS、108X=GLO、109X=GAL、111X=QZSS、112X=BDS）：
# 每个星座的开关是独立的，实测只配 107X 时播发里只有 1074，其余四个全缺。
# 曾经这里只存 `RTCM3-107X` 一行、注释还写着"全星座共用一档"，导致：
#   ① 备份不覆盖 108X~112X，回滚时无法还原；
#   ② 还原后基准站悄悄退回只播 GPS，下游可用卫星数直接砍一半。
# 注意写的名字是 `107X` 这种通配名，**不是** `1074`：具体消息号模块回 ERROR,1。
RATE_TARGETS = ('GGA', 'RMC', 'GSA', 'GLL',
                'RTCM3-1005', 'RTCM3-1006', 'RTCM3-1033',
                'RTCM3-107X', 'RTCM3-108X', 'RTCM3-109X',
                'RTCM3-111X', 'RTCM3-112X')

MODE_TEXT = {0: 'unknown', 1: 'rover', 2: 'base'}


def path():
    """备份文件路径。跟着 `workspace.STATE_DIR` 走（自测整体改指临时目录）。"""
    return os.path.join(workspace.STATE_DIR, BACKUP_NAME)


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


# ==================== 采集 ====================
def capture(driver, task_id='', port='', module=None):
    """读当前配置并打包成备份字典。**任何单项失败都只记警告，绝不抛。**

    返回的字典一定带 `ok` 字段：全部关键项都读到才是 True。
    `ok=False` 时上层仍可继续标定，只是要告诉用户"这次没法一键还原"。
    """
    backup = {
        'version': BACKUP_VERSION,
        'task_id': str(task_id or ''),
        'port': str(port or ''),
        'created_at': _now(),
        'module': dict(module or {}),
        'mode': {},
        'svin': {},
        'rtcm': {},
        'rates': {},
        'offsets': {},
        'antenna': {},
        'warnings': [],
        'ok': True,
    }
    warn = backup['warnings'].append

    # ---- 工作模式（还原的第一要件）----
    try:
        ok, message, data = driver.get_mode()
    except Exception as e:                       # noqa: BLE001 - 驱动异常不该让备份失败
        ok, message, data = False, f'{type(e).__name__}: {e}', {}
    if ok:
        code = data.get('mode_code')
        backup['mode'] = {'code': code, 'text': data.get('mode') or MODE_TEXT.get(code, 'unknown')}
    else:
        backup['ok'] = False
        warn(f'读取工作模式失败，无法还原模式：{message}')

    # ---- 坐标 / Survey-in 配置 ----
    try:
        ok, message, data = driver.get_svin_config()
    except Exception as e:                       # noqa: BLE001
        ok, message, data = False, f'{type(e).__name__}: {e}', {}
    if ok:
        backup['svin'] = {
            'mode': data.get('mode'),
            'mode_code': data.get('mode_code'),
            'count': data.get('count'),
            'acc_limit': data.get('acc_limit'),
            'ecef_x': data.get('ecef_x'),
            'ecef_y': data.get('ecef_y'),
            'ecef_z': data.get('ecef_z'),
            'distance': data.get('distance'),
        }
    else:
        backup['ok'] = False
        warn(f'读取坐标配置失败，无法还原坐标：{message}')

    # ---- RTCM 配置（MSM 类型等）----
    try:
        ok, message, data = driver.read_rtcm_config()
    except Exception as e:                       # noqa: BLE001
        ok, message, data = False, f'{type(e).__name__}: {e}', {}
    if ok:
        fields = data.get('fields') or []
        # MSM 档位优先取驱动翻译好的 `msm_type`：LG290P 的七字段里有它，
        # LC29H(BS) 没有七字段（只有一个全局 PAIR432 开关），由驱动翻译成同一语义。
        # 只看 `fields[0]` 的话，后者会得到 None，还原时把 MSM 档位丢掉。
        msm_type = data.get('msm_type')
        if msm_type is None and fields:
            msm_type = _to_int(fields[0])
        backup['rtcm'] = {
            'fields': fields,
            'raw': data.get('raw', ''),
            'msm_type': msm_type,
        }
    else:
        # rover 模式下部分固件回 ERROR,1（"从没配过"），不是故障，不降 ok
        backup['rtcm'] = {'fields': [], 'raw': '', 'msm_type': None, 'error': message}
        warn(f'读取 RTCM 配置失败（还原时将跳过 MSM 档位）：{message}')

    # ---- 关键报文速率 ----
    for name in RATE_TARGETS:
        rate, offset = _read_rate(driver, name)
        backup['rates'][name] = rate
        if offset is not None:
            # MSM 的 `<Offset>` 也存下来：还原时要和 Rate 一起放回，
            # 否则"原样还原"会把时延偏移悄悄改成 0。
            backup['offsets'][name] = offset
        if rate is None:
            warn(f'报文 {name} 的速率没读到（还原时该条保持模块当前值）')

    # ---- 天线信息（1033）----
    reader = getattr(driver, 'read_antenna_info', None)
    if reader is not None and driver.caps('ant_info'):
        try:
            ok, message, data = reader()
            if ok:
                backup['antenna'] = {'desc': data.get('desc', ''),
                                     'setup_id': data.get('setup_id'),
                                     'sn': data.get('sn', '')}
        except Exception as e:                   # noqa: BLE001
            warn(f'读取天线信息失败（还原时将跳过）：{e}')

    return backup


def _read_rate(driver, name):
    """读单条语句速率，返回 `(rate, offset)`；读不到返回 `(None, None)`（不抛）。

    `ERROR,1` 表示"这条语句从没配置过"，不是故障 —— 此时保持 None，
    还原时跳过它，模块就用默认值，与"还原前"的效果一致。

    `offset` 只有 MSM 才有（`OK,RTCM3-107X,1,0`），其余语句是 None。
    """
    try:
        ok, _message, data = driver.get_message_rate(name)
    except Exception as e:                       # noqa: BLE001
        logging.debug(f'读取 {name} 速率异常（忽略）：{e}')
        return None, None
    if not ok:
        return None, None
    fields = data.get('fields') or []
    # 速率永远是"去掉 OK 之后的第 2 个字段"：NMEA/RTCM 是 [名字, 速率]，
    # MSM 是 [名字, 速率, 偏移时延] —— 用 fields[-1] 会把 MSM 的时延当成速率。
    if len(fields) < 2:
        return None, None
    rate = _to_int(fields[1])
    offset = _to_int(fields[2]) if len(fields) >= 3 else None
    return rate, offset


def _get_rate(driver, name):
    """只要速率时的薄封装（保持老调用方/自测可用）。"""
    return _read_rate(driver, name)[0]


# ==================== 落盘 / 读取 ====================
def save(backup):
    """原子写 `.calibration/backup.json`。失败抛 OSError，由上层决定是否降级。"""
    return workspace.write_json(path(), backup, durable=True)


def load():
    """读备份；不存在或损坏返回 None（页面据此灰掉"还原"按钮）。"""
    data = workspace.read_json(path())
    if not isinstance(data, dict):
        return None
    if not data.get('mode') and not data.get('svin'):
        return None
    return data


def clear():
    return workspace.remove_file(path())


def usable(backup):
    """这份备份能不能拿来还原：至少要有一个明确的工作模式。"""
    if not backup:
        return False, '没有可用的备份（还没标定过，或备份文件已损坏）'
    code = (backup.get('mode') or {}).get('code')
    if code not in (1, 2):
        return False, f'备份里的工作模式不可识别（{code}），无法还原'
    return True, '备份可用'


# ==================== 人看的两行字 ====================
def describe(backup):
    """一句话摘要，给 CLI / 页面确认框用。"""
    if not backup:
        return '无备份'
    mode = (backup.get('mode') or {}).get('text') or '?'
    svin = backup.get('svin') or {}
    svin_mode = svin.get('mode') or '?'
    if svin_mode == 'fixed':
        coord = (f'ECEF {svin.get("ecef_x")}, {svin.get("ecef_y")}, {svin.get("ecef_z")}')
    elif svin_mode == 'survey_in':
        coord = f'Survey-in {svin.get("count")} 次 / {svin.get("acc_limit")} m'
    else:
        coord = '无固定坐标'
    rates = {name: rate for name, rate in (backup.get('rates') or {}).items()
             if rate is not None}
    module = backup.get('module') or {}
    return (f'{backup.get("created_at") or "?"} 于 {backup.get("port") or "?"}'
            f'（{module.get("firmware") or module.get("model") or "未知模块"}）：'
            f'模式 {mode}，{coord}，'
            f'速率 {rates or "未读到"}'
            f'{"；备份不完整" if not backup.get("ok", True) else ""}')


def _to_int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


# ==================== 自测（离线，不需要硬件） ====================
class _StubDriver:
    """只为验证 capture() 的容错：一半项成功、一半项失败。"""

    def __init__(self, fail=()):
        self.fail = set(fail)

    def caps(self, key, default=False):
        return True

    def _maybe(self, name, data):
        if name in self.fail:
            return False, f'{name} 故意失败', {'error': 'stub'}
        return True, 'ok', data

    def get_mode(self):
        return self._maybe('mode', {'mode': 'rover', 'mode_code': 1})

    def get_svin_config(self):
        return self._maybe('svin', {'mode': 'fixed', 'mode_code': 2, 'count': 0,
                                    'acc_limit': 0.0, 'ecef_x': 1.0, 'ecef_y': 2.0,
                                    'ecef_z': 3.0})

    def read_rtcm_config(self):
        return self._maybe('rtcm', {'fields': ['4', '0', '-90.0', '07', '06', '1', '0'],
                                    'raw': 'ok'})

    def get_message_rate(self, name):
        if name in self.fail:
            return False, 'ERROR,1', {'err_code': 1}
        if str(name).upper().startswith('RTCM3-107'):
            return True, 'ok', {'fields': [name, '1', '0']}     # MSM 多一个 Offset
        return True, 'ok', {'fields': [name, '1']}

    def read_antenna_info(self):
        return self._maybe('ant', {'desc': '2RTK-X', 'setup_id': 3, 'sn': 'SN1'})


def _selftest():
    import os
    import tempfile

    print('=== backup 自测 ===')
    original_dir = workspace.STATE_DIR
    tmpdir = tempfile.mkdtemp(prefix='2rtk-backup-')
    workspace.STATE_DIR = tmpdir
    try:
        # 1) 全项成功
        backup = capture(_StubDriver(), task_id='T1', port='FAKE0',
                         module={'firmware': 'LG290P03AANR01A03S'})
        assert backup['ok'] is True, backup['warnings']
        assert backup['mode'] == {'code': 1, 'text': 'rover'}, backup['mode']
        assert backup['svin']['ecef_x'] == 1.0
        assert backup['rates']['GGA'] == 1
        assert backup['offsets'].get('RTCM3-107X') == 0, backup['offsets']
        assert 'GGA' not in backup['offsets'], '非 MSM 语句不该有 Offset'
        assert usable(backup)[0] is True
        print(f'[1] 采集 OK  {describe(backup)[:60]}…')

        # 2) 落盘 / 读取 / 描述
        save(backup)
        assert os.path.exists(path())
        loaded = load()
        assert loaded['task_id'] == 'T1' and loaded['svin']['ecef_z'] == 3.0
        print('[2] 落盘/读取 OK')

        # 3) 部分失败：不抛、降级、usable 仍为 True（模式读到了）
        broken = capture(_StubDriver(fail=('rtcm', 'GGA')), task_id='T2', port='FAKE0')
        assert broken['ok'] is True, '单条报文失败不该把备份判废'
        assert broken['rtcm']['msm_type'] is None
        assert broken['rates']['GGA'] is None and broken['rates']['RMC'] == 1
        assert any('GGA' in item for item in broken['warnings'])
        print(f'[3] 部分失败降级 OK  {len(broken["warnings"])} 条警告')

        # 4) 模式读不到 → ok=False，且 usable 说不可还原
        no_mode = capture(_StubDriver(fail=('mode',)), task_id='T3')
        assert no_mode['ok'] is False
        assert usable(no_mode)[0] is False
        save(no_mode)
        assert load() is not None, '模式缺失时 load 仍应返回文件内容（供人看警告）'
        print(f'[4] 关键项失败 OK  usable={usable(no_mode)[1]}')

        # 5) 文件损坏/不存在 → load 返回 None，usable 解释原因
        clear()
        assert load() is None
        assert usable(load())[0] is False
        with open(path(), 'w', encoding='utf-8') as handle:
            handle.write('{not json')
        assert load() is None
        print(f'[5] 坏文件隔离 OK  {usable(load())[1]}')

        print('=== backup 自测全部通过 ===')
        return 0
    finally:
        workspace.STATE_DIR = original_dir
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(_selftest())
