# -*- coding: utf-8 -*-
"""未知模块的"只读驱动"：没有命令集，只会读模块自己吐出来的 NMEA。

为什么需要它
------------
用户明确要求"未知模块全部按照流动站模块看待"。可是 `match_driver()` 认不出型号时
返回 None，流程就没法启动——因为绝大多数步骤都是"发命令"。

`external_rover`（只测不写）恰好只需要三件事：读串口、转发 GGA、注入 RTCM。
这三件都不需要命令集。所以给这条路径配一个"什么都不发"的驱动：
    detect/info/get_mode 全部本地返回，不碰串口；
    任何写操作直接返回失败，杜绝误改别人的设备。

刻意**不注册**到 `driver_manager._REGISTRY`：
    一旦注册，`match_driver` 会把它排在最后当兜底，所有认不出的模块都会被它"认领"，
    于是本该报"型号不符"的场景变成"能跑但每步都失败"，现场更难定位。
    它只在 `external_rover` 且没有驱动认领时被显式构造。
"""
import logging

from .base import BaseDriver


class PassiveDriver(BaseDriver):
    """只读驱动：不发送任何命令，仅依赖串口已有的 NMEA 输出。"""

    MODEL_KEY = 'passive'
    DISPLAY_NAME = '未知模块（仅读 NMEA）'
    MATCH_PREFIX = ''
    DUAL_MODE = False
    CAPS = {}                 # 所有能力位为 False，前端据此灰掉所有需要写操作的功能
    CMD = {}                  # 没有命令集：body() 一被调用就抛 DriverBug，这是有意的
    PASSIVE = True            # 流程层用它判断"能不能发命令"

    def detect(self):
        """本地判断：缓存里有没有定位语句。不发任何命令。"""
        cache = getattr(self.channel, 'cache', None) or {}
        if cache.get('gga') or cache.get('rmc') or cache.get('gll'):
            return True, '未知型号，但已有 NMEA 定位输出', {'model_key': self.MODEL_KEY}
        return False, '未知型号，且串口上还没有 NMEA 定位输出', {'model_key': self.MODEL_KEY}

    def info(self):
        cache = getattr(self.channel, 'cache', None) or {}
        gga = cache.get('gga') or {}
        firmware = '未知（模块未提供 VER 语句或型号不匹配任何驱动）'
        return True, '未知模块（无命令集）', {
            'model_key': self.MODEL_KEY,
            'display_name': self.DISPLAY_NAME,
            'firmware': firmware,
            'build_date': '',
            'sn': '',
            'sn_source': '',
            'uniq_id': '',
            'mode': 'unknown',
            'caps': {},
            # 给页面一个"它确实在输出定位"的证据，比空表更有用
            'num_sats': gga.get('num_sats'),
            'quality': gga.get('quality'),
        }

    def get_mode(self):
        """读不到：没有命令集。调用方（external_rover 路径）会按流动站继续。"""
        return False, '未知模块没有命令集，读不到工作模式', {
            'mode': 'unknown', 'error': 'unsupported'}

    def probe_caps(self):
        """没有能力可探测。留空以兼容调用方。"""
        return {}

    # ---------- 所有写操作一律拒绝 ----------
    def _refuse(self, what):
        logging.info(f'未知模块（只读驱动）拒绝执行：{what}')
        return False, f'未知模块没有命令集，无法{what}', {'error': 'unsupported'}

    def set_mode(self, mode, persist=True, restart=True, verify=True):
        return self._refuse(f'切换工作模式为 {mode}')

    def get_fixed_ecef(self):
        return self._refuse('读取固定坐标配置')

    def set_fixed_ecef(self, x, y, z, persist=True, restart=True, verify=True):
        return self._refuse('写入固定坐标')

    def get_svin_config(self):
        return self._refuse('读取坐标配置')

    def config_survey_in(self, count, acc_limit, distance=None, persist=True, restart=True):
        return self._refuse('配置 Survey-in')

    def disable_survey_in(self, persist=True, restart=True):
        return self._refuse('停用坐标配置')

    def survey_status(self):
        return self._refuse('读取 Survey-in 状态')

    def set_message_rate(self, name, rate, offset=0):
        return self._refuse(f'配置 {name} 输出速率')

    def get_message_rate(self, name):
        return self._refuse(f'读取 {name} 输出速率')

    def config_rtcm(self, msm_type=4, persist=True):
        return self._refuse('配置 RTCM 输出')

    def read_rtcm_config(self):
        return self._refuse('读取 RTCM 配置')

    def restore_rtcm_config(self, fields):
        return self._refuse('恢复 RTCM 配置')

    def set_antenna_info(self, desc, setup_id=0, sn='', persist=True):
        return self._refuse('写入天线信息')

    def save(self):
        return self._refuse('保存配置到 NVM')

    def reboot(self):
        return self._refuse('重启模块')

    def set_fix_rate(self, interval_ms):
        return self._refuse('配置定位输出间隔')


# ==================== 离线自测（不需要硬件） ====================
if __name__ == '__main__':
    from .base import Channel

    class _MemChannel:
        def __init__(self):
            self.cache = {}
            self.counters = {'lines': 0, 'frames': 0, 'noise': 0}

    ch = _MemChannel()
    drv = PassiveDriver(ch)

    ok, msg, _ = drv.detect()
    assert not ok, '没有 NMEA 时不该认领'
    ch.cache['gga'] = {'valid': True, 'lat': 30.5, 'lon': 114.3,
                       'quality': 4, 'num_sats': 22}
    ok, msg, data = drv.detect()
    assert ok, '有 GGA 就该能跑只测不写'
    print('[1] 认领条件 OK  只认 NMEA 输出，不看型号')

    ok, msg, info = drv.info()
    assert ok and info['num_sats'] == 22 and info['mode'] == 'unknown'
    print('[2] 身份信息 OK  无 SN/固件时不伪造，带上卫星数作为"确实在定位"的证据')

    ok, msg, _ = drv.get_mode()
    assert not ok and '命令集' in msg
    print('[3] 读模式 OK  明确返回失败（调用方按流动站继续）')

    writes = [
        ('set_mode', (('rover',), {})),
        ('set_fixed_ecef', ((1.0, 2.0, 3.0), {})),
        ('set_message_rate', (('GGA', 1), {})),
        ('save', ((), {})),
        ('reboot', ((), {})),
    ]
    for name, (args, kwargs) in writes:
        ok, msg, _ = getattr(drv, name)(*args, **kwargs)
        assert not ok, f'{name} 必须拒绝'
    print('[4] 写操作全拒绝 OK  未知模块绝不能被这条路径改动')

    print('=== passive 驱动自测通过 ===')
