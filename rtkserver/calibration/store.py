# -*- coding: utf-8 -*-
"""标定独立库（calibration.db）：记录、型号能力、阶段事件。

为什么独立于 `2RTKserver.db`：
    `settings` 是"配置"，会被人为重置；标定记录是"资产"，
    重置配置不该把用户的救命坐标一起清掉。物理分开就不会误伤。

故障隔离约定（本模块的核心契约）：
    - **读**失败一律返回空值并记一次警告，绝不抛给调用方：
      标定库坏了，页面顶多少显示一条历史，不能连累配置页和主程序。
    - **写**失败抛 `StoreError`：状态机据此把"落库失败"降级成警告，
      标定结果本身（坐标已经写进模块）不受影响。

并发：每次操作新开连接（与 core/config.py 同一套路），WAL + timeout=10。
不用连接池：标定是低频操作（一次任务写 1~2 行），池子只会带来线程安全问题。

自测（不需要硬件，用临时库，不动真实 calibration.db）：
    python -m rtkserver.calibration.store
"""
import contextlib
import json
import logging
import os
import sqlite3
import time

from ..paths import BASE_DIR

# 与 2RTKserver.db 同级。放模块级而不是常量写死，是为了自测能整体改成临时文件。
CALIBRATION_DB = os.path.join(BASE_DIR, 'calibration.db')

SCHEMA_VERSION = 4

# 每 SN 保留条数 / 全库上限（方案 §12.4）
KEEP_PER_SN = 50
TOTAL_LIMIT = 5000

METHODS = ('cors', 'survey_in', 'manual', 'external_rover')
VERIFY_STATES = (0, 1, 2)          # 0=未校验 1=通过 2=通过但有警告
# NVM 三态（方案 §8.4）：这条记录到底有没有真的写进模块的持久存储。
# 事后排查"坐标用了几天自己变回去了"时，只有这个字段能给出结论。
NVM_STATES = ('', 'saved', 'reverted', 'unknown', 'not_written')

RECORD_FIELDS = (
    'module_sn', 'module_model', 'firmware', 'method',
    'ecef_x', 'ecef_y', 'ecef_z', 'accuracy',
    'lat', 'lon', 'height',
    'samples', 'duration',
    'apc_x', 'apc_y', 'apc_z',
    'arp_x', 'arp_y', 'arp_z',
    'gga_guard_used', 'gga_jump_dropped',
    'raw_sample_std_e', 'raw_sample_std_n', 'raw_sample_std_u',
    'kalman_std_e', 'kalman_std_n', 'kalman_std_u',
    'svin_valid', 'svin_obs_count', 'svin_mean_acc',
    'cors_mountpoint_used',
    'verified', 'verify_delta',
    'nvm_state', 'task_id',
    'created_at', 'remark',
)

_REAL_FIELDS = ('ecef_x', 'ecef_y', 'ecef_z', 'accuracy', 'lat', 'lon', 'height',
                'apc_x', 'apc_y', 'apc_z',
                'arp_x', 'arp_y', 'arp_z',
                'raw_sample_std_e', 'raw_sample_std_n', 'raw_sample_std_u',
                'kalman_std_e', 'kalman_std_n', 'kalman_std_u',
                'svin_mean_acc', 'verify_delta')
_INT_FIELDS = ('samples', 'duration', 'verified', 'gga_guard_used', 'gga_jump_dropped',
               'svin_valid', 'svin_obs_count')
_TEXT_FIELDS = ('module_sn', 'module_model', 'firmware', 'method', 'created_at', 'remark',
                'nvm_state', 'task_id', 'cors_mountpoint_used')


class StoreError(Exception):
    """写标定库失败。调用方必须捕获并降级为警告，不能让标定任务因此崩掉。"""


# ==================== 表结构 ====================
_DDL_V1 = (
    '''
    CREATE TABLE IF NOT EXISTS calibration_records (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        module_sn    TEXT    NOT NULL DEFAULT '',
        module_model TEXT    NOT NULL DEFAULT '',
        firmware     TEXT    NOT NULL DEFAULT '',
        method       TEXT    NOT NULL DEFAULT '',
        ecef_x       REAL,
        ecef_y       REAL,
        ecef_z       REAL,
        accuracy     REAL,
        lat          REAL,
        lon          REAL,
        height       REAL,
        samples      INTEGER NOT NULL DEFAULT 0,
        duration     INTEGER NOT NULL DEFAULT 0,
        verified     INTEGER NOT NULL DEFAULT 0,
        verify_delta REAL,
        created_at   TEXT    NOT NULL,
        remark       TEXT    NOT NULL DEFAULT ''
    )
    ''',
    'CREATE INDEX IF NOT EXISTS idx_records_sn_time ON calibration_records(module_sn, created_at DESC)',
    'CREATE INDEX IF NOT EXISTS idx_records_time ON calibration_records(created_at DESC)',
    '''
    CREATE TABLE IF NOT EXISTS module_profiles (
        model_key    TEXT PRIMARY KEY,
        display_name TEXT NOT NULL DEFAULT '',
        match_prefix TEXT NOT NULL DEFAULT '',
        caps         TEXT NOT NULL DEFAULT '{}',
        updated_at   TEXT NOT NULL
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS calibration_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id    TEXT    NOT NULL DEFAULT '',
        record_id  INTEGER,
        stage      TEXT    NOT NULL DEFAULT '',
        level      TEXT    NOT NULL DEFAULT 'info',
        message    TEXT    NOT NULL DEFAULT '',
        created_at TEXT    NOT NULL
    )
    ''',
    'CREATE INDEX IF NOT EXISTS idx_events_task ON calibration_events(task_id, created_at)',
)

# 版本号 -> 迁移函数。只做增量，永远不改已发布的那一步。
# v2（阶段3）：写入链路要记两件事，缺了就无法事后归因 ——
#   apc_x/y/z  用户给的坐标（本产品没有天线偏移，这一组就是实际写入值）
#   nvm_state  saved / reverted / unknown（方案 §8.4 的三态）
#   task_id    与 calibration_events 互查用（事件在记录之前就产生了，只能靠它关联）
_DDL_V2 = (
    'ALTER TABLE calibration_records ADD COLUMN apc_x REAL',
    'ALTER TABLE calibration_records ADD COLUMN apc_y REAL',
    'ALTER TABLE calibration_records ADD COLUMN apc_z REAL',
    "ALTER TABLE calibration_records ADD COLUMN nvm_state TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE calibration_records ADD COLUMN task_id TEXT NOT NULL DEFAULT ''",
)

# v3（补充附录 I）：把"事后能不能复现这一组坐标"所需的信息全部落库。
#   arp_x/y/z     真正写进模块的值（ecef_* 是同一组数的另一种排布，单独存一份便于
#                 对照"当时想写什么"与"实际写进了什么"）
#   gga_guard_used   GGA 防抖有没有生效 —— 标定阶段 GGA 是**原样透传**的（VRS 靠它
#                 定位虚拟参考站），只在单条跳变 >500 m 时丢弃该条并复用上一条兜底。
#                 这个字段只说明"护栏开着"，说明不了 VRS 锁在哪
#   gga_jump_dropped 本次标定丢掉了多少条跳变 GGA。>0 说明过程中 GGA 有异常漂移，
#                 事后复盘"为什么一直不固定"时，先看这个数
#   raw_sample_std_*  **原始样本**三轴标准差，是精度评估的唯一可信口径
#   kalman_std_*  滤波输出标准差，只做参考（滤波器自报的 P 与输入噪声无关，
#                 拿它当精度会得到一个恒为毫米级的假数字）
#   svin_*        Survey-in 的 Valid / 观测数 / 模块自报 3D 精度；CORS 模式为 NULL
#   cors_mountpoint_used  用的哪个挂载点，复现现场靠它（挂载点不同，数据源精度不同）
_DDL_V3 = (
    'ALTER TABLE calibration_records ADD COLUMN arp_x REAL',
    'ALTER TABLE calibration_records ADD COLUMN arp_y REAL',
    'ALTER TABLE calibration_records ADD COLUMN arp_z REAL',
    'ALTER TABLE calibration_records ADD COLUMN gga_freeze_used INTEGER',
    'ALTER TABLE calibration_records ADD COLUMN raw_sample_std_e REAL',
    'ALTER TABLE calibration_records ADD COLUMN raw_sample_std_n REAL',
    'ALTER TABLE calibration_records ADD COLUMN raw_sample_std_u REAL',
    'ALTER TABLE calibration_records ADD COLUMN kalman_std_e REAL',
    'ALTER TABLE calibration_records ADD COLUMN kalman_std_n REAL',
    'ALTER TABLE calibration_records ADD COLUMN kalman_std_u REAL',
    'ALTER TABLE calibration_records ADD COLUMN svin_valid INTEGER',
    'ALTER TABLE calibration_records ADD COLUMN svin_obs_count INTEGER',
    'ALTER TABLE calibration_records ADD COLUMN svin_mean_acc REAL',
    "ALTER TABLE calibration_records ADD COLUMN cors_mountpoint_used TEXT NOT NULL DEFAULT ''",
)

# v4（2026-11）：GGA 冻结 → GGA 防抖。
# v3 的 `gga_freeze_used` 描述的是"上报位置被钉死在第一个定位上"，这套逻辑已经删掉了：
# 标定阶段锁死 GGA 会让 VRS 生成的虚拟参考站远离真实天线，电离层/对流层残差放大，
# 反而更固定不了。新逻辑原样透传 GGA、只过滤跳变，所以语义完全变了，不能复用旧列——
# 复用会让历史数据（冻结=1）与新数据（护栏=1）混在一起，事后无法区分。
# 旧列 `gga_freeze_used` 保留不删（历史记录还要读），新数据一律写 NULL。
_DDL_V4 = (
    'ALTER TABLE calibration_records ADD COLUMN gga_guard_used INTEGER',
    'ALTER TABLE calibration_records ADD COLUMN gga_jump_dropped INTEGER',
)

_MIGRATIONS = {
    1: lambda conn: [conn.execute(stmt) for stmt in _DDL_V1],
    2: lambda conn: [conn.execute(stmt) for stmt in _DDL_V2],
    3: lambda conn: [conn.execute(stmt) for stmt in _DDL_V3],
    4: lambda conn: [conn.execute(stmt) for stmt in _DDL_V4],
}

# 初始型号表。**能力矩阵的权威来源是驱动（drivers/*.py 的 CAPS）**，
# 这里的行只是"页面没连硬件时也能列出型号"的种子，启动后由
# `save_profile()` 用驱动里的值覆盖，避免两处真相打架。
_SEED_PROFILES = (
    {
        'model_key': 'lg290p',
        'display_name': 'Quectel LG290P',
        'match_prefix': 'LG290P',
        # ant_delta=False：LG290P 不支持 PQTMCFGANTDELTA（只有 LG291P(00) 支持）
        'caps': {'set_mode': True, 'svin': True, 'ant_info': True,
                 'ant_delta': False, 'rtcm_out': True},
    },
    {
        'model_key': 'lc29hbs',
        'display_name': 'Quectel LC29HBS',
        'match_prefix': 'LC29H',
        'caps': {'set_mode': True, 'svin': True, 'ant_info': True,
                 'ant_delta': True, 'rtcm_out': True},
    },
    {
        'model_key': 'unknown',
        'display_name': '未知模块（只观测，不写配置）',
        'match_prefix': '',
        'caps': {'set_mode': False, 'svin': False, 'ant_info': False,
                 'ant_delta': False, 'rtcm_out': False},
    },
)


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


@contextlib.contextmanager
def _connect():
    """开一个连接。WAL 与 synchronous 只在 init_db 里设一次（它们写进库文件本身）。"""
    conn = sqlite3.connect(CALIBRATION_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def init_db():
    """建库/建表/增量迁移 + 补种子数据。可重复调用，幂等。

    阶段1会把它挂到标定启动钩子里（Web 起来之后、主循环之前）。
    现在也可以在任意入口先调一次，代价是一次 open + 几次 PRAGMA。
    """
    with _connect() as conn:
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
        except sqlite3.Error as e:
            # 只读介质/权限问题：不影响已存在的库继续用
            logging.warning(f'标定库设置 WAL 失败（忽略）：{e}')

        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version < SCHEMA_VERSION:
            # 老库（version=0 但表已存在）也走这里：DDL 全是 IF NOT EXISTS，幂等
            _migrate(conn, version)
            version = SCHEMA_VERSION
        elif version > SCHEMA_VERSION:
            # 用户先跑了新版本又回退：不重建、不删列，只提示。
            # 重建等于把资产删了，这比"少一列"危险得多。
            logging.warning(
                f'标定库表结构版本 v{version} 高于本程序支持的 v{SCHEMA_VERSION}，'
                f'按兼容模式读写（可能出现未知字段）')

        _seed_profiles(conn)
        conn.commit()
    logging.debug(f'标定库就绪：{CALIBRATION_DB}（schema v{SCHEMA_VERSION}）')


def _migrate(conn, from_version):
    for target in range(from_version + 1, SCHEMA_VERSION + 1):
        step = _MIGRATIONS.get(target)
        if step is None:
            raise StoreError(f'缺少 v{target} 的迁移步骤')
        step(conn)
        # PRAGMA 不支持参数绑定，target 来自本模块常量，不是外部输入
        conn.execute(f'PRAGMA user_version = {int(target)}')
        conn.commit()
        logging.info(f'标定库表结构已升级到 v{target}')


def _seed_profiles(conn):
    if not _table_exists(conn, 'module_profiles'):
        return
    now = _now()
    for item in _SEED_PROFILES:
        conn.execute(
            "INSERT INTO module_profiles (model_key, display_name, match_prefix, caps, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(model_key) DO NOTHING",
            (item['model_key'], item['display_name'], item['match_prefix'],
             json.dumps(item['caps'], ensure_ascii=False), now))


# ==================== 记录：写 ====================
def _coerce(value, kind):
    if value is None or value == '':
        return None if kind == 'real' else (0 if kind == 'int' else '')
    try:
        if kind == 'real':
            return float(value)
        if kind == 'int':
            return int(float(value))
    except (TypeError, ValueError):
        return None if kind == 'real' else 0
    return str(value)


def _normalize_record(record):
    """把调用方给的 dict 收敛成一条完整行：补默认值、反算经纬度、校验枚举。

    经纬度缺失但 ECEF 齐全时**自动反算**：写进模块的是 ECEF，
    而页面和 NTRIP 源列表要的是经纬度，两处各算一遍迟早不一致。
    """
    row = {}
    for field in RECORD_FIELDS:
        if field in _REAL_FIELDS:
            row[field] = _coerce(record.get(field), 'real')
        elif field in _INT_FIELDS:
            row[field] = _coerce(record.get(field), 'int')
        else:
            row[field] = _coerce(record.get(field), 'text')

    if row['samples'] is None:
        row['samples'] = 0
    if row['duration'] is None:
        row['duration'] = 0
    if row['verified'] is None:
        row['verified'] = 0
    if row['verified'] not in VERIFY_STATES:
        row['verified'] = 0
    if row['gga_guard_used'] is None:
        row['gga_guard_used'] = 0
    if row['gga_jump_dropped'] is None:
        row['gga_jump_dropped'] = 0
    # Survey-in 专属字段：CORS / manual 模式下没有这个概念，必须留 NULL 而不是 0。
    # `_coerce` 对整型缺省补 0，那是为 samples/duration 这类"确实是 0"的字段设计的；
    # 这里若补 0，事后看库会误以为"跑过 Survey-in，观测 0 次"。
    for field in ('svin_valid', 'svin_obs_count'):
        if record.get(field) in (None, ''):
            row[field] = None
    # `ecef_*` 从 v1 起就表示"实际写进模块的 ARP"。老调用方只填 ecef_*，
    # 这里补一份 arp_*：同一组数两个列名并存是冗余的，但语义完全不同——
    # 一个说"播发的是这个"，一个说"写进模块的是这个"，事后对不上时能立刻看出
    # 是"没写进去"还是"写错了"。
    if (row['arp_x'] is None and row['arp_y'] is None and row['arp_z'] is None
            and None not in (row['ecef_x'], row['ecef_y'], row['ecef_z'])):
        row['arp_x'], row['arp_y'], row['arp_z'] = (
            row['ecef_x'], row['ecef_y'], row['ecef_z'])
    if row['nvm_state'] and row['nvm_state'] not in NVM_STATES:
        # 不认识就留空而不是硬塞一个值：这个字段是事后归因的唯一依据，
        # 记成 '' 表示"当时没判定"，硬塞 'unknown' 会与真正的回写失败混淆。
        logging.warning(f'标定记录的 nvm_state 非约定值，按未判定处理：{row["nvm_state"]}')
        row['nvm_state'] = ''
    if row['method'] and row['method'] not in METHODS:
        logging.warning(f'标定记录里的 method 非约定值，按原样保存：{row["method"]}')
    if not row['created_at']:
        row['created_at'] = _now()

    if (row['lat'] is None or row['lon'] is None) \
            and None not in (row['ecef_x'], row['ecef_y'], row['ecef_z']):
        from .geo import ecef_to_geodetic
        try:
            lat, lon, height = ecef_to_geodetic(row['ecef_x'], row['ecef_y'], row['ecef_z'])
            row['lat'], row['lon'] = lat, lon
            if row['height'] is None:
                row['height'] = height
        except Exception as e:                   # 反算失败不该拦住落库
            logging.warning(f'标定记录反算经纬度失败（保留空值）：{e}')

    return row


def insert_record(record):
    """插入一条标定记录，返回主键。失败抛 StoreError。

    表被删/库被换成 0 字节时先自愈建表再重试一次（与 core/config.py 同策略）。
    """
    if not isinstance(record, dict):
        raise StoreError('标定记录必须是 dict')
    row = _normalize_record(record)
    try:
        return _insert_once(row)
    except sqlite3.OperationalError as e:
        logging.warning(f'写标定库失败（{e}），重建表结构后重试一次')
        try:
            init_db()
            return _insert_once(row)
        except sqlite3.Error as retry_error:
            raise StoreError(f'写入标定记录失败：{retry_error}') from retry_error
    except sqlite3.Error as e:
        raise StoreError(f'写入标定记录失败：{e}') from e


_INSERT_SQL = (
    "INSERT INTO calibration_records ({columns}) VALUES ({marks})".format(
        columns=', '.join(RECORD_FIELDS),
        marks=', '.join('?' for _ in RECORD_FIELDS))
)


def _insert_once(row):
    with _connect() as conn:
        cursor = conn.execute(_INSERT_SQL, tuple(row[f] for f in RECORD_FIELDS))
        new_id = cursor.lastrowid
        try:
            _prune_records(conn)
        except sqlite3.Error as e:
            # 清理失败绝不能让"记录已经写进去了"变成一次失败
            logging.warning(f'标定记录清理失败（忽略）：{e}')
        conn.commit()
        return new_id


def update_record(record_id, **fields):
    """按主键更新若干字段，返回是否真的改到了行。

    只允许改 RECORD_FIELDS 里的列（id / created_at 除外）：
    直接拼字段名会有注入风险，这里用白名单过滤后再拼。
    """
    payload = {k: v for k, v in fields.items() if k in RECORD_FIELDS and k != 'created_at'}
    if not payload:
        return False

    assignments = []
    values = []
    for key, value in payload.items():
        if key in _REAL_FIELDS:
            value = _coerce(value, 'real')
        elif key in _INT_FIELDS:
            value = _coerce(value, 'int')
        else:
            value = _coerce(value, 'text')
        assignments.append(f'{key} = ?')
        values.append(value)

    values.append(int(record_id))
    sql = f"UPDATE calibration_records SET {', '.join(assignments)} WHERE id = ?"
    try:
        with _connect() as conn:
            cursor = conn.execute(sql, tuple(values))
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        raise StoreError(f'更新标定记录失败：{e}') from e


# ==================== 记录：读 ====================
def _row_to_dict(row):
    return {key: row[key] for key in row.keys()} if row is not None else None


def get_record(record_id):
    try:
        with _connect() as conn:
            row = conn.execute(
                'SELECT * FROM calibration_records WHERE id = ?', (int(record_id),)).fetchone()
            return _row_to_dict(row)
    except sqlite3.Error as e:
        logging.warning(f'读取标定记录失败（已忽略）：{e}')
        return None


def get_latest_by_sn(module_sn):
    """按 SN 取最近一条记录。

    未知模块的 SN 是空串，这里**直接返回 None**：空 SN 匹配到的"最近一条"
    可能来自另一块没识别出来的模块，拿它当"这块模块的历史坐标"会写错坐标。
    """
    if not module_sn:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM calibration_records WHERE module_sn = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (str(module_sn),)).fetchone()
            return _row_to_dict(row)
    except sqlite3.Error as e:
        logging.warning(f'按 SN 读取标定记录失败（已忽略）：{e}')
        return None


def list_records(limit=50, offset=0, module_sn=None, method=None):
    """按时间倒序列表（页面历史表格直接用）。读失败返回空列表。"""
    sql = 'SELECT * FROM calibration_records'
    where = []
    params = []
    if module_sn is not None:
        where.append('module_sn = ?')
        params.append(str(module_sn))
    if method:
        where.append('method = ?')
        params.append(str(method))
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?'
    params.extend([max(1, int(limit)), max(0, int(offset))])

    try:
        with _connect() as conn:
            return [_row_to_dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error as e:
        logging.warning(f'列出标定记录失败（已忽略）：{e}')
        return []


def count_records(module_sn=None):
    sql = 'SELECT COUNT(*) FROM calibration_records'
    params = ()
    if module_sn is not None:
        sql += ' WHERE module_sn = ?'
        params = (str(module_sn),)
    try:
        with _connect() as conn:
            return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error as e:
        logging.warning(f'统计标定记录失败（已忽略）：{e}')
        return 0


# ==================== 保留策略（方案 §12.4） ====================
def _prune_records(conn):
    """每个 SN 保留最近 KEEP_PER_SN 条，全库不超过 TOTAL_LIMIT 条。

    红线：任何 SN 的**最后一条成功记录**永不删除——那是用户的救命坐标。
    只在内存里算一遍 id 集合再删，不用 SQL 窗口函数（老 SQLite 不支持）。
    """
    rows = conn.execute(
        'SELECT id, module_sn, verified FROM calibration_records '
        'ORDER BY created_at DESC, id DESC').fetchall()
    if not rows:
        return 0

    keep = []
    keep_set = set()
    per_sn = {}
    success_seen = set()

    for row in rows:
        sn = row['module_sn'] or ''
        used = per_sn.get(sn, 0)
        if used < KEEP_PER_SN:
            keep.append(row['id'])
            keep_set.add(row['id'])
            per_sn[sn] = used + 1
        if row['verified'] == 1 and sn not in success_seen:
            success_seen.add(sn)
            if row['id'] not in keep_set:
                keep.append(row['id'])
                keep_set.add(row['id'])

    # 总量上限：keep 已近似按时间倒序，超出的从尾部（最旧）砍，成功记录不砍
    if len(keep) > TOTAL_LIMIT:
        latest_success = set()
        for sn in success_seen:
            for row in rows:
                if (row['module_sn'] or '') == sn and row['verified'] == 1:
                    latest_success.add(row['id'])
                    break
        keep = [rid for index, rid in enumerate(keep)
                if index < TOTAL_LIMIT or rid in latest_success]
        keep_set = set(keep)

    doomed = [rid for rid in (row['id'] for row in rows) if rid not in keep_set]
    if not doomed:
        return 0
    # SQLite 单语句变量上限默认 999，分批删
    for start in range(0, len(doomed), 500):
        batch = doomed[start:start + 500]
        conn.execute(
            f'DELETE FROM calibration_records WHERE id IN ({",".join("?" * len(batch))})',
            tuple(batch))
    logging.info(f'标定库清理：删除 {len(doomed)} 条超期记录')
    return len(doomed)


def prune_records():
    """手动触发一次清理（Web 端"清理历史"按钮用）。"""
    try:
        with _connect() as conn:
            deleted = _prune_records(conn)
            conn.commit()
            return deleted
    except sqlite3.Error as e:
        logging.warning(f'清理标定记录失败（已忽略）：{e}')
        return 0


# ==================== 型号能力表 ====================
def list_profiles():
    """返回型号列表，caps 已解成 dict。"""
    try:
        with _connect() as conn:
            rows = conn.execute(
                'SELECT * FROM module_profiles ORDER BY model_key').fetchall()
    except sqlite3.Error as e:
        logging.warning(f'读取型号表失败（已忽略）：{e}')
        return []

    result = []
    for row in rows:
        item = _row_to_dict(row)
        try:
            item['caps'] = json.loads(item.get('caps') or '{}')
        except (TypeError, ValueError):
            logging.warning(f'型号 {item.get("model_key")} 的 caps 不是合法 JSON，按空能力处理')
            item['caps'] = {}
        result.append(item)
    return result


def get_profile(model_key):
    if not model_key:
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                'SELECT * FROM module_profiles WHERE model_key = ?', (str(model_key),)).fetchone()
    except sqlite3.Error as e:
        logging.warning(f'读取型号 {model_key} 失败（已忽略）：{e}')
        return None
    item = _row_to_dict(row)
    if item is None:
        return None
    try:
        item['caps'] = json.loads(item.get('caps') or '{}')
    except (TypeError, ValueError):
        item['caps'] = {}
    return item


def save_profile(model_key, display_name='', match_prefix='', caps=None):
    """新增/覆盖一个型号（阶段1由驱动在启动时同步自己的 CAPS，保证单一真相）。"""
    if not model_key:
        raise StoreError('model_key 不能为空')
    caps_json = json.dumps(caps or {}, ensure_ascii=False)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO module_profiles (model_key, display_name, match_prefix, caps, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(model_key) DO UPDATE SET "
                "display_name=excluded.display_name, match_prefix=excluded.match_prefix, "
                "caps=excluded.caps, updated_at=excluded.updated_at",
                (str(model_key), str(display_name), str(match_prefix), caps_json, _now()))
            conn.commit()
    except sqlite3.Error as e:
        raise StoreError(f'保存型号 {model_key} 失败：{e}') from e


# ==================== 阶段事件（P2 统计用，先落表不展示） ====================
def insert_event(task_id, stage, level='info', message='', record_id=None):
    """记一条阶段流水。失败不抛：事件是锦上添花，不能拖垮正在跑的标定。"""
    try:
        with _connect() as conn:
            cursor = conn.execute(
                "INSERT INTO calibration_events (task_id, record_id, stage, level, message, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(task_id or ''), record_id, str(stage or ''), str(level or 'info'),
                 str(message or ''), _now()))
            conn.commit()
            return cursor.lastrowid
    except sqlite3.Error as e:
        logging.debug(f'写入标定事件失败（忽略）：{e}')
        return None


def list_events(task_id=None, limit=200):
    sql = 'SELECT * FROM calibration_events'
    params = []
    if task_id:
        sql += ' WHERE task_id = ?'
        params.append(str(task_id))
    sql += ' ORDER BY created_at DESC, id DESC LIMIT ?'
    params.append(max(1, int(limit)))
    try:
        with _connect() as conn:
            return [_row_to_dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error as e:
        logging.warning(f'读取标定事件失败（已忽略）：{e}')
        return []


# ==================== 自测入口（临时库，不碰真实数据） ====================
def _selftest():
    """建库/迁移/增查改/保留策略/型号表/事件，全在临时目录里跑。

    跑法：python -m rtkserver.calibration.store
    """
    import tempfile

    global CALIBRATION_DB
    print('=== store 自测 ===')

    # 0) 字段分类清单必须覆盖 RECORD_FIELDS，否则新增列会静默按文本存
    classified = set(_REAL_FIELDS) | set(_INT_FIELDS) | set(_TEXT_FIELDS)
    assert classified == set(RECORD_FIELDS), classified ^ set(RECORD_FIELDS)
    print(f'[0] 字段清单自洽 OK  {len(RECORD_FIELDS)} 个字段')

    original = CALIBRATION_DB
    tmpdir = tempfile.mkdtemp(prefix='2rtk-calib-')
    CALIBRATION_DB = os.path.join(tmpdir, 'calibration.db')

    try:
        # 1) 建库 + schema 版本
        init_db()
        assert os.path.exists(CALIBRATION_DB)
        with _connect() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert version == SCHEMA_VERSION, version
        assert mode.lower() == 'wal', mode
        for name in ('calibration_records', 'module_profiles', 'calibration_events'):
            assert name in tables, (name, tables)
        print(f'[1] 建库 OK  schema=v{version}  journal={mode}')

        # 2) 幂等：再调一次不能报错、不能清数据
        init_db()
        print('[2] 幂等 OK')

        # 3) 插入 + 自动反算经纬度
        from .geo import geodetic_to_ecef
        x, y, z = geodetic_to_ecef(30.5, 114.3, 45.6)
        new_id = insert_record({
            'module_sn': 'SN20261101', 'module_model': 'lg290p', 'firmware': '1.05',
            'method': 'cors', 'ecef_x': x, 'ecef_y': y, 'ecef_z': z,
            'accuracy': 0.012, 'samples': 120, 'duration': 180,
            'verified': 1, 'verify_delta': 0.003, 'remark': '首次标定',
        })
        assert new_id > 0
        record = get_record(new_id)
        assert record['module_sn'] == 'SN20261101'
        assert abs(record['lat'] - 30.5) < 1e-6, record['lat']
        assert abs(record['lon'] - 114.3) < 1e-6
        assert abs(record['height'] - 45.6) < 1e-3
        print(f'[3] 插入+反算 OK  id={new_id} lat={record["lat"]:.6f} lon={record["lon"]:.6f}')

        # 4) 按 SN 取最新
        latest = get_latest_by_sn('SN20261101')
        assert latest['id'] == new_id
        assert get_latest_by_sn('NOPE') is None
        assert get_latest_by_sn('') is None
        print('[4] 取最新 OK')

        # 5) 更新
        assert update_record(new_id, verified=2, remark='复核：精度略差') is True
        updated = get_record(new_id)
        assert updated['verified'] == 2 and '复核' in updated['remark']
        assert update_record(new_id, id=999) is False       # 白名单外字段 → 不做任何事
        print('[5] 更新 OK')

        # 6) 时间倒序 + 分页 + 计数
        for index in range(3):
            insert_record({'module_sn': 'SN-B', 'method': 'survey_in',
                           'ecef_x': x + index, 'ecef_y': y, 'ecef_z': z,
                           'created_at': f'2026-11-0{index + 1} 10:00:00'})
        rows = list_records(limit=10)
        assert len(rows) == 4, len(rows)
        assert rows[0]['created_at'] >= rows[-1]['created_at']
        assert len(list_records(limit=2)) == 2
        assert len(list_records(module_sn='SN-B')) == 3
        assert count_records() == 4 and count_records('SN-B') == 3
        print('[6] 列表/分页/计数 OK')

        # 7) 约束：非法 verified 归 0、method 原样保留
        bad_id = insert_record({'module_sn': 'SN-C', 'verified': 99, 'method': 'weird'})
        bad = get_record(bad_id)
        assert bad['verified'] == 0 and bad['method'] == 'weird'
        assert bad['samples'] == 0 and bad['duration'] == 0
        print('[7] 字段收敛 OK')

        # 8) 保留策略：单 SN 超过 50 条时只留最近 50，最后一条成功记录必留
        global KEEP_PER_SN
        saved_keep = KEEP_PER_SN
        KEEP_PER_SN = 5
        try:
            for index in range(12):
                insert_record({'module_sn': 'SN-D', 'verified': 0,
                               'created_at': f'2026-11-01 10:{index:02d}:00',
                               'remark': f'r{index}'})
            assert count_records('SN-D') == 5, count_records('SN-D')
            kept = list_records(module_sn='SN-D')
            assert kept[0]['remark'] == 'r11', kept[0]['remark']
            # 中间插一条成功记录，它比最新 5 条都旧，也必须活着
            insert_record({'module_sn': 'SN-D', 'verified': 0,
                           'created_at': '2026-11-01 09:00:00', 'remark': 'old-fail'})
            success_id = insert_record({'module_sn': 'SN-D', 'verified': 1,
                                        'created_at': '2026-11-01 09:30:00', 'remark': 'old-ok'})
            remarks = [row['remark'] for row in list_records(module_sn='SN-D', limit=100)]
            assert 'old-ok' in remarks, remarks
            assert get_record(success_id) is not None
            print(f'[8] 保留策略 OK  保留 {len(remarks)} 条，旧成功记录未被删')
        finally:
            KEEP_PER_SN = saved_keep

        # 9) 型号表
        profiles = list_profiles()
        keys = {item['model_key'] for item in profiles}
        assert {'lg290p', 'lc29hbs', 'unknown'} <= keys, keys
        lg = get_profile('lg290p')
        assert lg['caps']['svin'] is True and lg['caps']['ant_delta'] is False
        save_profile('lg290p', 'Quectel LG290P', 'LG290P', {'svin': True, 'ant_delta': False})
        assert get_profile('lg290p')['caps']['svin'] is True
        assert get_profile('nope') is None
        print(f'[9] 型号表 OK  {sorted(keys)}')

        # 10) 事件表
        insert_event('task-1', 'COLLECT', 'info', '开始采集')
        insert_event('task-1', 'SAVE', 'error', 'SAVEPAR 超时')
        insert_event('task-2', 'OPEN', 'info', '打开串口')
        assert len(list_events('task-1')) == 2
        assert len(list_events()) == 3
        print('[10] 事件表 OK')

        # 11) v1 → v2 增量迁移：老库的资产必须原样保留，新列给默认值
        legacy = os.path.join(tmpdir, 'legacy.db')
        conn = sqlite3.connect(legacy)
        try:
            for stmt in _DDL_V1:
                conn.execute(stmt)
            conn.execute('PRAGMA user_version = 1')
            conn.execute(
                "INSERT INTO calibration_records (module_sn, ecef_x, ecef_y, ecef_z, created_at) "
                "VALUES ('LEGACY-1', 1.0, 2.0, 3.0, '2026-01-01 00:00:00')")
            conn.commit()
        finally:
            conn.close()

        CALIBRATION_DB = legacy
        init_db()
        with _connect() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            columns = {row[1] for row in conn.execute(
                'PRAGMA table_info(calibration_records)')}
        assert version == SCHEMA_VERSION, version
        for field in ('apc_x', 'apc_y', 'apc_z', 'nvm_state', 'task_id'):
            assert field in columns, (field, columns)
        old = get_latest_by_sn('LEGACY-1')
        assert old and old['ecef_z'] == 3.0 and old['nvm_state'] == '' and old['apc_x'] is None, old
        print(f'[11] v1→v2 迁移 OK  老记录保留，新列默认空（schema v{version}）')

        # 12) 库文件被破坏时不能把调用方带崩
        CALIBRATION_DB = os.path.join(tmpdir, 'broken.db')
        with open(CALIBRATION_DB, 'wb') as handle:
            handle.write(b'\x00' * 32)
        assert list_records() == []          # 读：降级为空
        assert count_records() == 0
        assert get_latest_by_sn('X') is None
        assert list_profiles() == []
        try:
            insert_record({'module_sn': 'X'})
            raised = False
        except StoreError:
            raised = True
        assert raised, '坏库写入必须抛 StoreError 供上层降级'
        print('[12] 坏库隔离 OK（读降级、写抛 StoreError）')

        print('=== store 自测全部通过 ===')
        return 0
    finally:
        CALIBRATION_DB = original
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)   # Windows 上 -wal/-shm 可能还占着


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(_selftest())
