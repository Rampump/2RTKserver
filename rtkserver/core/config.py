# -*- coding: utf-8 -*-
"""配置读写与数据库初始化（SQLite KV 表）。

设计约定：
  1. 只有一张 settings(key, value) 表，新增配置项零迁移成本：
     init_db() 只补缺失的键，绝不动已有值。
  2. get_config() 每次开一个 SQLite 连接（timeout=10）。它是高频调用，
     拆分期**不新增**调用点；需要配置的底层模块一律「传入 cfg」。
"""
import contextlib
import logging
import sqlite3

from ..paths import CONFIG_DB
from ..security import hash_password
from .constants import SENSITIVE_KEY_HINTS

DEFAULT_CONFIG = {
    "mode": "none",
    "serial_port": "",
    "baud_rate": "",
    # 三个数据出口各自一个独立开关，默认全关：程序启动只做采集/监控，
    # 不连接任何外部服务。需要哪个出口由用户在配置页显式勾选。
    "caster_enabled": "0",
    "caster_host": "",
    "caster_port": "2101",
    "caster_mountpoint": "",
    "caster_password": "",
    "relay_host": "",
    "relay_port": "2101",
    "relay_mountpoint": "",
    "relay_user": "",
    "relay_password": "",
    "local_caster_enabled": "0",
    "local_caster_host": "0.0.0.0",
    "local_caster_port": "2102",
    "radio_enabled": "0",
    "radio_port": "",
    "radio_baud": "115200",
    "admin_user": "admin",
    "admin_password": "admin"
}


def init_db():
    """初始化数据库并写入默认配置（小写键名）"""
    with contextlib.closing(sqlite3.connect(CONFIG_DB, timeout=10)) as conn:
        # WAL 模式下读不阻塞写，多线程（Web + 后台推流）同时访问时不会互相卡住。
        # 该设置写进数据库文件本身，只需成功设置一次。
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        conn.commit()

        cursor = conn.execute("SELECT COUNT(*) FROM settings")
        if cursor.fetchone()[0] == 0:
            default_items = [(k, v) for k, v in DEFAULT_CONFIG.items()]
            conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", default_items)
            conn.commit()
            logging.info("数据库已创建并写入默认配置")
        else:
            # 老库升级：后来新增的配置项（例如本地 Caster）在老库里根本不存在，
            # 用 INSERT OR IGNORE 只补缺失的键，不动任何已有值。
            missing = [(k, v) for k, v in DEFAULT_CONFIG.items()
                       if conn.execute("SELECT 1 FROM settings WHERE key = ?", (k,)).fetchone() is None]
            if missing:
                conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", missing)
                conn.commit()
                logging.info(f"配置库补齐 {len(missing)} 个新增项: {', '.join(k for k, _ in missing)}")


def get_db():
    # timeout=10：并发写时等待锁释放，而不是立刻抛 "database is locked"
    return sqlite3.connect(CONFIG_DB, timeout=10)


# 读库失败只详细记一次：get_config() 是高频调用，每 2 秒的状态推送都会走到这里，
# 每次都打完整堆栈会把日志刷满，真正的故障反而看不见。
_db_error_logged = {'done': False}

_INT_FIELDS = ['caster_port', 'relay_port', 'baud_rate', 'local_caster_port', 'radio_baud']


def _normalize_int_fields(config):
    for field in _INT_FIELDS:
        raw_val = config.get(field)
        if raw_val is None or raw_val == '':
            config[field] = 0
        else:
            try:
                config[field] = int(raw_val)
            except (ValueError, TypeError):
                config[field] = 0
    return config


def get_config():
    # 这里原先每次都调 init_db()：等于每个 HTTP 请求都新建一次连接、
    # 执行一次 CREATE TABLE IF NOT EXISTS 和 COUNT(*)、再提交一次事务。
    # 建表只在 main() 启动时做一次就够了。
    config = {}
    logging.debug("开始从数据库读取配置信息")
    try:
        with contextlib.closing(get_db()) as conn:
            cursor = conn.execute("SELECT key, value FROM settings")
            for key, value in cursor.fetchall():
                config[key.lower()] = value
    except sqlite3.Error as e:
        # 库文件被写成 0 字节 / 表丢了 / 磁盘满时，不能让配置页直接 500。
        # 退回默认值：页面能打开、能看见问题，用户可以重新保存一次配置（save_config 会自愈建表）。
        if not _db_error_logged['done']:
            _db_error_logged['done'] = True
            logging.error(f"读取配置库失败，暂时按默认配置处理：{e}")
        config = dict(DEFAULT_CONFIG)
    # 会话密钥不能出现在配置接口/模板里，否则等于把密钥交给前端
    config.pop('secret_key', None)
    logging.debug("配置信息读取完成")
    return _normalize_int_fields(config)


def save_config(config: dict):
    """保存配置到数据库。表不存在时先自愈建表再重试一次。"""
    try:
        _save_config_once(config)
    except sqlite3.OperationalError as e:
        logging.warning(f"写配置库失败（{e}），尝试重建配置表后重试")
        init_db()
        _save_config_once(config)


def _save_config_once(config: dict):
    with contextlib.closing(get_db()) as conn:
        existing = {}
        cursor = conn.execute("SELECT key, value FROM settings")
        for key, value in cursor.fetchall():
            existing[key.lower()] = value

        for key, value in config.items():
            key = key.lower()
            if key in {'baud_rate', 'serial_port'} and value is None:
                value = ''
            else:
                value = str(value)

            old_value = existing.get(key)

            if old_value == value:
                continue

            if key in existing:
                conn.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
            else:
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))

        conn.commit()


def reset_config_to_default():
    defaults = DEFAULT_CONFIG.copy()
    # 重置也要写哈希，避免重置后数据库里又出现明文口令
    defaults['admin_password'] = hash_password(DEFAULT_CONFIG['admin_password'])
    save_config(defaults)
    logging.info("配置已重置为默认值")


def mask_config(cfg: dict) -> dict:
    """打日志前对口令类字段打码"""
    masked = {}
    for key, value in cfg.items():
        if any(hint in key.lower() for hint in SENSITIVE_KEY_HINTS) and value:
            masked[key] = '***'
        else:
            masked[key] = value
    return masked


def save_serial_config(port: str, baud: int):
    """保存串口配置到数据库"""
    try:
        with contextlib.closing(sqlite3.connect(CONFIG_DB)) as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('serial_port', ?)", (port,))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('baud_rate', ?)", (str(baud),))
            conn.commit()

        logging.info(f"已保存串口配置到数据库: {port} @ {baud}bps")
    except Exception as e:
        logging.error(f"保存串口配置失败: {str(e)}")


def migrate_admin_password():
    """把数据库里现存的明文管理员口令升级为哈希，避免明文落库。

    放在 config 层（而不是 security 层）是为了让依赖方向保持单向：
    config → security，security 不反向 import config。
    """
    try:
        cfg = get_config()
        stored = cfg.get('admin_password', '')
        if stored and not stored.startswith('pbkdf2_sha256$'):
            save_config({'admin_password': hash_password(stored)})
            logging.info("管理员口令已升级为哈希存储（原明文已覆盖）")
    except Exception as e:
        logging.error(f"管理员口令迁移失败: {e}")
