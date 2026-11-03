# -*- coding: utf-8 -*-
"""拆包契约测试（stdlib unittest，零新增依赖）。

这份测试的作用不是"覆盖业务"，而是**锁死拆分前后的等价性**：
一旦改动让路由表、默认配置、校验语义或 RTCM 解帧行为发生变化，这里会立刻红。
跑法：

    python -m unittest discover -s tests -v
"""
import ast
import io
import os
import random
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==================== 独立参考实现（用于交叉验证） ====================
def crc24q_reference(data: bytes) -> int:
    """逐位实现的 CRC-24Q，与被测的表驱动实现互为验证。

    刻意不复用被测代码，否则等于自己验证自己。
    """
    poly = 0x1864CFB
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= poly
    return crc & 0xFFFFFF


class TestPaths(unittest.TestCase):
    """修正 ③：BASE_DIR 漂移会让用户以为"配置全丢了"。"""

    def test_base_dir_is_repo_root(self):
        from rtkserver import paths
        self.assertEqual(os.path.realpath(paths.BASE_DIR), os.path.realpath(REPO_ROOT))

    def test_data_files_live_in_root(self):
        from rtkserver import paths
        self.assertEqual(os.path.dirname(paths.CONFIG_DB), os.path.realpath(REPO_ROOT))
        self.assertEqual(os.path.dirname(paths.SECRET_KEY_FILE), os.path.realpath(REPO_ROOT))
        self.assertEqual(os.path.dirname(paths.LOG_FILE), os.path.realpath(REPO_ROOT))

    def test_templates_and_static_exist(self):
        from rtkserver import paths
        self.assertTrue(os.path.isdir(paths.TEMPLATES_DIR), paths.TEMPLATES_DIR)
        self.assertTrue(os.path.isdir(paths.STATIC_DIR), paths.STATIC_DIR)


class TestRoutes(unittest.TestCase):
    """路由表必须稳定：新增/删除路由要在这里显式登记，避免悄悄多出一个入口。"""

    EXPECTED = {
        ('/', 'GET'),
        ('/login', 'GET,POST'),
        ('/logout', 'POST'),
        ('/config', 'GET,POST'),
        # 检查更新单独成页：升级会停服务换文件，不能和"改配置"混在一起。
        # 卸载接口保留但已固定返回 410（网页入口移除，只走 uninstall.sh）。
        ('/upgrade', 'GET'),
        ('/change_password', 'GET,POST'),
        ('/restart_program', 'POST'),
        ('/api/load_config', 'GET'),
        ('/api/local_caster', 'GET'),
        ('/api/serial_ports', 'GET'),
        ('/api/sourcetable', 'POST'),
        ('/api/upgrade/status', 'GET'),
        ('/api/upgrade/check', 'POST'),
        ('/api/upgrade/start', 'POST'),
        ('/api/upgrade/uninstall', 'POST'),
        ('/ws', 'GET'),
        # ---- 阶段 6：一键标定 Web 层（方案 §11.2）----
        ('/calibrate', 'GET'),
        ('/api/calibration/options', 'GET'),
        ('/api/calibration/status', 'GET'),
        ('/api/calibration/detect', 'POST'),
        ('/api/calibration/start', 'POST'),
        ('/api/calibration/abort', 'POST'),
        ('/api/calibration/apply-as-source', 'POST'),
        ('/api/calibration/history', 'GET'),
    }

    @classmethod
    def setUpClass(cls):
        from rtkserver.web.app import create_app
        cls.app = create_app()

    def test_business_rules_unchanged(self):
        rules = set()
        for r in self.app.url_map.iter_rules():
            if r.rule.startswith('/static/'):
                continue
            methods = ','.join(sorted(r.methods - {'HEAD', 'OPTIONS'}))
            rules.add((r.rule, methods))
        self.assertEqual(rules, self.EXPECTED)

    def test_endpoint_names_unchanged(self):
        # 模板与代码里用 url_for('login' / 'index' / 'config_page')，名字不能变
        endpoints = {r.endpoint for r in self.app.url_map.iter_rules()}
        for name in ('index', 'login', 'logout', 'config_page', 'restart_program',
                     'change_password', 'api_load_config', 'api_local_caster',
                     'api_serial_ports', 'api_sourcetable', 'websocket',
                     'calibration_page', 'api_calibration_options', 'api_calibration_status',
                     'api_calibration_start', 'api_calibration_abort'):
            self.assertIn(name, endpoints, f'endpoint 丢了: {name}')

    def test_index_renders(self):
        client = self.app.test_client()
        resp = client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'<html', resp.data.lower())

    def test_config_requires_login(self):
        client = self.app.test_client()
        resp = client.get('/config')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers.get('Location', ''))

    def test_api_returns_401_not_redirect(self):
        # 接口未登录必须回 401 JSON，不能 302 到登录页（否则前端 JSON 解析必然失败）
        client = self.app.test_client()
        resp = client.get('/api/load_config')
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.is_json and resp.get_json().get('ok'))

    def test_security_headers_present(self):
        client = self.app.test_client()
        resp = client.get('/')
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(resp.headers.get('X-Frame-Options'), 'SAMEORIGIN')
        # OSM 瓦片政策要求：不能是 no-referrer
        self.assertEqual(resp.headers.get('Referrer-Policy'), 'strict-origin-when-cross-origin')


class TestDefaultConfig(unittest.TestCase):
    """老设备升级时，缺键补齐的集合不能变。"""

    EXPECTED_KEYS = {
        'mode', 'serial_port', 'baud_rate',
        'caster_enabled', 'caster_host', 'caster_port', 'caster_mountpoint', 'caster_password',
        'relay_host', 'relay_port', 'relay_mountpoint', 'relay_user', 'relay_password',
        'local_caster_enabled', 'local_caster_host', 'local_caster_port',
        'radio_enabled', 'radio_port', 'radio_baud',
        'admin_user', 'admin_password',
    }

    def test_key_set(self):
        from rtkserver.core.config import DEFAULT_CONFIG
        self.assertEqual(set(DEFAULT_CONFIG), self.EXPECTED_KEYS)

    def test_key_defaults(self):
        from rtkserver.core.config import DEFAULT_CONFIG
        self.assertEqual(DEFAULT_CONFIG['mode'], 'none')
        self.assertEqual(DEFAULT_CONFIG['local_caster_port'], '2102')
        self.assertEqual(DEFAULT_CONFIG['caster_port'], '2101')
        self.assertEqual(DEFAULT_CONFIG['relay_port'], '2101')
        self.assertEqual(DEFAULT_CONFIG['local_caster_host'], '0.0.0.0')
        # 三个出口默认全关：装上不应该自动连任何外部服务
        for key in ('caster_enabled', 'local_caster_enabled', 'radio_enabled'):
            self.assertEqual(DEFAULT_CONFIG[key], '0', key)

    def test_no_new_config_keys_for_mdns(self):
        """mDNS 主机名固定为常量，不得变成配置项。"""
        from rtkserver.core.config import DEFAULT_CONFIG
        for key in DEFAULT_CONFIG:
            self.assertNotIn('mdns', key.lower())
            self.assertNotIn('zeroconf', key.lower())
            self.assertNotIn('hostname', key.lower())


class TestValidateConfig(unittest.TestCase):
    """validate_config 决定了程序是否"敢干活"，语义不能漂。"""

    def _base(self, **over):
        from rtkserver.core.config import DEFAULT_CONFIG
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(over)
        return cfg

    def test_mode_none_returns_false(self):
        from rtkserver.validation import validate_config
        self.assertFalse(validate_config(self._base()))

    def test_invalid_mode_returns_false(self):
        from rtkserver.validation import validate_config
        self.assertFalse(validate_config(self._base(mode='whatever')))

    def test_serial_without_outlets_returns_true(self):
        from rtkserver.validation import validate_config
        self.assertTrue(validate_config(self._base(mode='serial')))

    def test_caster_enabled_without_host_returns_false(self):
        from rtkserver.validation import validate_config
        cfg = self._base(mode='serial', caster_enabled='1', caster_mountpoint='M')
        self.assertFalse(validate_config(cfg))

    def test_relay_missing_host_returns_false(self):
        from rtkserver.validation import validate_config
        cfg = self._base(mode='relay', relay_port='2101', relay_mountpoint='M')
        self.assertFalse(validate_config(cfg))

    def test_relay_valid_returns_true(self):
        from rtkserver.validation import validate_config
        cfg = self._base(mode='relay', relay_host='caster.example.com',
                         relay_port='2101', relay_mountpoint='M')
        self.assertTrue(validate_config(cfg))

    def test_invalid_local_caster_port_returns_false(self):
        from rtkserver.validation import validate_config
        cfg = self._base(mode='serial', local_caster_enabled='1',
                         local_caster_port='70000')
        self.assertFalse(validate_config(cfg))

    def test_self_loop_is_rejected(self):
        from rtkserver.validation import validate_config
        cfg = self._base(mode='serial', local_caster_enabled='1',
                         local_caster_port='65533',
                         caster_enabled='1', caster_host='127.0.0.1',
                         caster_port='65533', caster_mountpoint='M')
        self.assertFalse(validate_config(cfg))

    def test_radio_without_port_returns_false(self):
        from rtkserver.validation import validate_config
        cfg = self._base(mode='serial', radio_enabled='1', radio_port='')
        self.assertFalse(validate_config(cfg))


class TestRtcmFrames(unittest.TestCase):
    """CRC24Q 与解帧是数据面唯一的"纯函数"，必须逐字节等价。"""

    def test_crc_matches_reference(self):
        from rtkserver.sources.rtcm_parser import crc24q
        rnd = random.Random(20261101)
        for _ in range(200):
            n = rnd.randint(0, 64)
            data = bytes(rnd.randrange(256) for _ in range(n))
            self.assertEqual(crc24q(data), crc24q_reference(data), data.hex())

    def test_known_frame_roundtrip(self):
        from rtkserver.sources.rtcm_parser import crc24q, extract_rtcm_frames
        payload = b'\x3e\xd0\x00\x03\x8aX\xd9I<\x87/4\x10\x9d\x07\xd6\xafH Z'
        header = bytes([0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF])
        crc = crc24q(header + payload).to_bytes(3, 'big')
        frame = header + payload + crc

        frames, remaining = extract_rtcm_frames(frame)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], frame)
        self.assertEqual(remaining, b'')

    def test_bad_crc_is_skipped(self):
        from rtkserver.sources.rtcm_parser import crc24q, extract_rtcm_frames
        payload = b'\x01\x02\x03\x04'
        header = bytes([0xD3, 0x00, len(payload)])
        bad = header + payload + b'\x00\x00\x00'
        good_crc = crc24q(header + payload).to_bytes(3, 'big')
        good = header + payload + good_crc

        frames, remaining = extract_rtcm_frames(bad + good)
        # 坏帧必须被跳过，后面的好帧仍要能取出来
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], good)
        self.assertEqual(remaining, b'')

    def test_partial_frame_kept_as_remainder(self):
        from rtkserver.sources.rtcm_parser import crc24q, extract_rtcm_frames
        payload = b'\x0a\x0b\x0c'
        header = bytes([0xD3, 0x00, len(payload)])
        frame = header + payload + crc24q(header + payload).to_bytes(3, 'big')

        frames, remaining = extract_rtcm_frames(frame[:-2])
        self.assertEqual(frames, [])
        self.assertEqual(remaining, frame[:-2])

    def test_garbage_before_frame_is_dropped(self):
        from rtkserver.sources.rtcm_parser import crc24q, extract_rtcm_frames
        payload = b'\x11\x22'
        header = bytes([0xD3, 0x00, len(payload)])
        frame = header + payload + crc24q(header + payload).to_bytes(3, 'big')

        frames, remaining = extract_rtcm_frames(b'\x00\xff\xff' + frame)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], frame)
        self.assertEqual(remaining, b'')


class TestOutletRegistry(unittest.TestCase):
    """分发顺序与拆包前一致：本地 Caster → 数传 → 上传 → 解析。"""

    def test_registry_order(self):
        from rtkserver.core import state
        from rtkserver.outlets import register_all
        register_all()
        self.assertEqual(
            list(state.outlets),
            ['local_caster', 'radio', 'caster', 'parser'],
        )
        for name, item in state.outlets.items():
            self.assertTrue(callable(item['publish']), name)
            self.assertIn('error_label', item)

    def test_distribute_is_noop_for_empty_data(self):
        from rtkserver.core.distributor import distribute
        # 空数据不得触发任何出口（否则会往队列里塞空帧）
        distribute(b'')


class TestMdnsAnnouncer(unittest.TestCase):
    """mDNS 是旁路能力：依赖缺失时必须安静降级，绝不能抛异常。"""

    def test_snapshot_without_start(self):
        from rtkserver.discovery.zeroconf_announcer import ZeroconfAnnouncer
        ann = ZeroconfAnnouncer()
        snap = ann.snapshot()
        self.assertEqual(snap['state'], 'stopped')
        self.assertEqual(snap['name'], '2rtk.local')
        self.assertEqual(snap['web_url'], 'http://2rtk.local:5757')

    def test_constant_hostname(self):
        from rtkserver.discovery.zeroconf_announcer import DEFAULT_HOSTNAME
        self.assertEqual(DEFAULT_HOSTNAME, '2rtk')

    def test_start_and_stop_never_raise(self):
        """装了 zeroconf 就真广播一次；没装则返回 False。两种情况都不许抛异常。"""
        from rtkserver.discovery.zeroconf_announcer import ZeroconfAnnouncer
        ann = ZeroconfAnnouncer(web_port=5757)
        try:
            ok = ann.start()
            self.assertIsInstance(ok, bool)
            if ok:
                self.assertEqual(ann.snapshot()['state'], 'running')
                self.assertTrue(ann.snapshot()['addresses'])
        finally:
            ann.stop()

    def test_stop_is_idempotent(self):
        from rtkserver.discovery.zeroconf_announcer import ZeroconfAnnouncer
        ann = ZeroconfAnnouncer()
        ann.stop()
        ann.stop()

    def test_snapshot_follows_actual_server_after_rename(self):
        """名字冲突退让后，对外显示/打印的地址必须是**实际广播**的那个名字。

        踩过的坑：zeroconf 的 allow_name_change 只改服务实例名，A 记录用的
        server 不变。当初用实例名生成 URL，日志会写"退为 2rtk-2.local"，
        实际仍在广播 2rtk.local —— 两台设备抢同一个名字，客户端 50% 概率
        连错机器。这里锁住：显示名一律来自 self._server。
        """
        from rtkserver.discovery.zeroconf_announcer import ZeroconfAnnouncer
        ann = ZeroconfAnnouncer()
        ann._server = '2rtk-2.local.'
        snap = ann.snapshot()
        self.assertEqual(snap['name'], '2rtk-2.local')
        self.assertEqual(snap['web_url'], 'http://2rtk-2.local:5757')
        self.assertEqual(snap['hostname'], '2rtk')      # 配置的本名不变
        self.assertEqual(ann._host_label(), '2rtk-2')

    def test_host_label_tolerates_bare_name(self):
        from rtkserver.discovery.zeroconf_announcer import ZeroconfAnnouncer
        ann = ZeroconfAnnouncer()
        ann._server = ''
        self.assertEqual(ann._host_label(), '2rtk')


class TestRunStatusIp(unittest.TestCase):
    """首次使用（没配数据源）也要能看见本机 IP —— 安卓只能靠它访问。"""

    def test_refresh_network_info_fills_ip(self):
        from rtkserver.core import state
        from rtkserver.core.status import refresh_network_info
        refresh_network_info()
        self.assertIn('local_ip', state.run_status)
        self.assertIn('local_ips', state.run_status)
        self.assertIn('mdns_url', state.run_status)
        self.assertTrue(state.run_status['mdns_url'].startswith('http://2rtk.local'))
        self.assertIsInstance(state.run_status['local_ips'], list)


class TestVirtualAddressFilter(unittest.TestCase):
    """容器 / 网桥地址不能进 mDNS 的 A 记录。

    现场设备常装 Docker：docker0 的 172.17.0.1 会出现在 getaddrinfo(hostname)
    里。若写进 A 记录，客户端解析 2rtk.local 时可能拿到这个局域网不可达的
    地址，表现为"时好时坏"，极难排查。
    """

    def setUp(self):
        from rtkserver.utils.network import drop_virtual_addresses
        self.drop = drop_virtual_addresses

    def test_docker_and_overlay_dropped(self):
        mapping = {'192.168.1.71': 'eth0', '172.31.0.1': 'docker0',
                   '10.8.0.5': 'ztabc', '192.168.1.72': 'wlan0'}
        got = self.drop(['192.168.1.71', '172.31.0.1', '10.8.0.5', '192.168.1.72'], mapping)
        self.assertEqual(got, ['192.168.1.71', '192.168.1.72'])

    def test_bridge_and_veth_dropped(self):
        mapping = {'10.1.0.1': 'br-3f2a', '10.1.0.2': 'veth1a2b', '192.168.1.71': 'eth0'}
        got = self.drop(['192.168.1.71', '10.1.0.1', '10.1.0.2'], mapping)
        self.assertEqual(got, ['192.168.1.71'])

    def test_first_address_kept_even_if_virtual(self):
        """整机只有虚拟网卡时不能剔空，否则状态页无 IP 可显示。"""
        mapping = {'172.31.0.1': 'docker0'}
        self.assertEqual(self.drop(['172.31.0.1'], mapping), ['172.31.0.1'])

    def test_no_mapping_is_passthrough(self):
        """非 Linux（拿不到网卡名）时保持原行为，不误伤。"""
        ips = ['192.168.1.71', '172.31.0.1']
        self.assertEqual(self.drop(ips, {}), ips)
        self.assertEqual(self.drop([], {}), [])

    def test_unmapped_address_kept(self):
        """映射里没有的地址说明不是本地虚拟网卡，必须保留。"""
        mapping = {'192.168.1.71': 'eth0'}
        self.assertEqual(self.drop(['192.168.1.71', '192.168.1.200'], mapping),
                         ['192.168.1.71', '192.168.1.200'])

    def test_announcer_uses_same_rule(self):
        """announcer 自带一份实现（可独立拷走），规则必须与 utils 一致。"""
        from rtkserver.discovery import zeroconf_announcer as ann
        self.assertTrue(hasattr(ann, '_interface_ipv4_map'))
        self.assertEqual(ann._VIRTUAL_IF_PREFIXES,
                         __import__('rtkserver.utils.network', fromlist=['x']).VIRTUAL_IF_PREFIXES)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestUpgrade(unittest.TestCase):
    """升级/卸载执行器的硬约束。

    runner 必须能在「rtkserver/ 已被替换」之后继续正常工作，所以它不能
    import 任何项目内模块——这条一旦破了，升级会死在半路，且只在真机上才暴露。
    """

    def setUp(self):
        from rtkserver.upgrade import sources, service
        self.sources = sources
        self.service = service

    def test_runner_only_uses_stdlib(self):
        import re
        import sys
        from rtkserver.upgrade import service as svc
        with open(svc.RUNNER_SRC, 'r', encoding='utf-8') as f:
            src = f.read()
        mods = set()
        for line in src.splitlines():
            m = re.match(r'\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)', line)
            if m:
                mods.add(m.group(1))
        third_party = {m for m in mods if m not in sys.stdlib_module_names}
        self.assertEqual(third_party, set(), f'执行器引入了非标准库依赖: {third_party}')

    def test_runner_does_not_import_project(self):
        from rtkserver.upgrade import service as svc
        with open(svc.RUNNER_SRC, 'r', encoding='utf-8') as f:
            src = f.read()
        self.assertNotIn('from rtkserver', src)
        self.assertNotIn('import rtkserver', src)
        self.assertNotIn('from ..', src)

    def test_sources_resolved(self):
        conf = self.sources.resolved()
        self.assertIn('branch', conf)
        self.assertTrue(conf['sources'], '至少要有 github 一个源')
        for key, label, url in conf['sources']:
            self.assertTrue(url.startswith('http'), url)
            self.assertTrue(label)

    def test_source_by_key(self):
        self.assertIsNone(self.sources.source_by_key('auto'))
        self.assertIsNone(self.sources.source_by_key(''))
        first_key = self.sources.resolved()['sources'][0][0]
        self.assertTrue(self.sources.source_by_key(first_key).startswith('http'))

    def test_managed_ports_web_port_first(self):
        # Web 端口必须排第一：健康检查取的是 managed_ports()[0]，
        # 一旦顺序变成 [2102, 5757]，健康检查就会打到本地 Caster 上，
        # 表现为「服务已起来却判定升级失败」。这条断言就是那次事故的护栏。
        from rtkserver.core.constants import WEB_PORT
        ports = self.service.managed_ports()
        self.assertEqual(ports[0], WEB_PORT)
        self.assertEqual(len(ports), len(set(ports)))

    def test_state_shape_when_missing(self):
        st = self.service.read_state()
        self.assertIn('stage', st)
        self.assertIn('progress', st)



class TestRadioLogThrottle(unittest.TestCase):
    """数传串口打不开时的重试日志必须收敛。

    真机实测：模块拔掉后每 3 秒重试一次，每次都打 ERROR，几个晚上就能把
    server.log 刷到几百 MB，真正的故障反而被淹没。这里锁死"首次 ERROR、
    之后抑制"的行为，避免以后被改回无脑打日志。
    """

    def setUp(self):
        try:
            from rtkserver.outlets import radio_bridge
        except Exception as exc:  # 没装 pyserial 的环境跳过，不算失败
            self.skipTest(f'跳过（无法导入 radio_bridge：{exc}）')
        self.mod = radio_bridge

    def test_failures_logged_once(self):
        import logging

        records = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Collect()
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            bridge = self.mod.SerialBridge('/dev/definitely-not-a-port', 57600)
            for _ in range(5):
                self.assertFalse(bridge._open())
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)

        errors = [r for r in records if r.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 1, f'连续 5 次失败应只打 1 条 ERROR，实际 {len(errors)} 条')
        self.assertIn('数传串口打开失败', errors[0].getMessage())
        # 状态里仍要能看到失败信息，抑制的只是日志而不是状态
        st = bridge.status()
        self.assertFalse(st['connected'])
        self.assertTrue(st['last_error'])


class TestVersionSingleSource(unittest.TestCase):
    """版本号只能有一个来源：安装根目录的 VERSION。

    背景：版本号原先散落在 server.py、rtkserver/__init__.py、控制台 LOGO 和
    mDNS TXT 记录四处，发版漏改一处就会出现「网页 2.2.1、mDNS 2.1.8」这种
    自相矛盾的信息。这组断言把"只改一个文件"锁死，谁再写死版本号会立刻红。
    """

    def setUp(self):
        from rtkserver import version
        self.version = version

    def test_version_file_semver(self):
        with open(self.version.VERSION_FILE, 'r', encoding='utf-8') as f:
            text = f.read().strip()
        self.assertRegex(text, r'^\d+\.\d+\.\d+$', 'VERSION 必须是 主.次.修 三段式')

    def test_package_version_matches_file(self):
        import rtkserver
        with open(self.version.VERSION_FILE, 'r', encoding='utf-8') as f:
            text = f.read().strip()
        self.assertEqual(rtkserver.__version__, text)

    def test_server_module_exports_same_version(self):
        import server
        import rtkserver
        self.assertEqual(server.__version__, rtkserver.__version__)

    def test_missing_file_falls_back_to_placeholder(self):
        # 打包漏了 VERSION 时不能崩，也不能报一个"看起来像真版本"的号
        self.assertEqual(self.version.read_version('/nonexistent/VERSION'),
                         self.version.UNKNOWN_VERSION)

    def test_bad_content_falls_back(self):
        import tempfile
        for bad in ('', 'abc', '2.2', '2.2.2.1'):
            with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                             encoding='utf-8') as f:
                f.write(bad)
                path = f.name
            try:
                self.assertEqual(self.version.read_version(path),
                                 self.version.UNKNOWN_VERSION, bad)
            finally:
                os.unlink(path)

    def test_version_tuple(self):
        self.assertEqual(self.version.version_tuple('2.2.2'), (2, 2, 2))
        self.assertEqual(self.version.version_tuple('2.2.2-rc1'), (2, 2, 2))
        self.assertEqual(self.version.version_tuple('abc'), (0, 0, 0))

    def test_no_semver_literals_in_code(self):
        """代码里不许再出现 'x.y.z' 形式的硬编码版本号。

        rtkserver/version.py 豁免：它是唯一允许出现版本字面量的地方（占位值
        0.0.0），它的内容由 test_package_version_matches_file 单独核对。
        """
        import re
        pattern = re.compile(r"""(['"])\d+\.\d+\.\d+\1""")
        targets = [os.path.join(REPO_ROOT, 'server.py')]
        for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, 'rtkserver')):
            if '__pycache__' in root:
                continue
            targets += [os.path.join(root, f) for f in files
                        if f.endswith('.py') and f != 'version.py']
        for path in targets:
            with open(path, 'r', encoding='utf-8') as f:
                for lineno, line in enumerate(f, 1):
                    self.assertIsNone(
                        pattern.search(line),
                        f'{os.path.relpath(path, REPO_ROOT)}:{lineno} 写死了版本号: {line.strip()}')

    def test_logo_and_announcer_use_shared_version(self):
        from rtkserver import __version__
        from rtkserver.core.constants import ART_LOGO
        from rtkserver.discovery.zeroconf_announcer import ZeroconfAnnouncer
        self.assertIn(__version__, ART_LOGO)
        self.assertEqual(ZeroconfAnnouncer(version=__version__).version, __version__)
        # 默认不猜版本：没有传入时宁可不写这条 TXT 记录
        self.assertEqual(ZeroconfAnnouncer().version, '')

    def test_installers_ship_version_file(self):
        """安装/更新脚本必须把 VERSION 一起复制过去，否则装完版本号就是 0.0.0。"""
        for name in ('install.sh', 'update.sh'):
            with open(os.path.join(REPO_ROOT, name), 'r', encoding='utf-8') as f:
                text = f.read()
            self.assertIn('VERSION', text, f'{name} 的复制清单漏了 VERSION')
            self.assertIn('"app"', text, f'{name} 写 .version 时没记录 app 版本')

    def test_upgrade_runner_syncs_version_file(self):
        from rtkserver.upgrade import service as svc
        with open(svc.RUNNER_SRC, 'r', encoding='utf-8') as f:
            src = f.read()
        self.assertIn("'VERSION'", src, '升级执行器的 SYNC_ITEMS 漏了 VERSION')
        self.assertIn("'app':", src, '升级执行器写 .version 时没记录 app 版本')


class TestConfigDbResilience(unittest.TestCase):
    """配置库被写成 0 字节 / 表丢了，不能让配置页直接 500。

    .19 那台设备上真实出现过"多处 0 字节的空壳文件"，配置库就是其中之一：
    当时的状态是网页能打开、一进配置页就崩，用户完全不知道该删哪个文件。
    """

    def setUp(self):
        import tempfile
        from rtkserver.core import config as cfgmod
        self.cfgmod = cfgmod
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, '2RTKserver.db')
        open(self.path, 'wb').close()          # 0 字节，等价于"表不存在"
        self.old = cfgmod.CONFIG_DB
        cfgmod.CONFIG_DB = self.path
        cfgmod._db_error_logged['done'] = False

    def tearDown(self):
        import shutil
        self.cfgmod.CONFIG_DB = self.old
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_falls_back_to_defaults(self):
        from rtkserver.core.config import DEFAULT_CONFIG
        cfg = self.cfgmod.get_config()
        self.assertEqual(cfg['mode'], 'none')
        self.assertEqual(cfg['caster_port'], 2101)      # 仍然做了 int 归一
        # 退回的是完整默认值，不是一张空表
        self.assertEqual(set(cfg) - {'secret_key'}, set(DEFAULT_CONFIG))

    def test_save_self_heals_the_table(self):
        self.cfgmod.save_config({'mode': 'relay', 'relay_host': 'caster.example.com'})
        cfg = self.cfgmod.get_config()
        self.assertEqual(cfg['mode'], 'relay')
        self.assertEqual(cfg['relay_host'], 'caster.example.com')
        # 自愈时会把缺失的默认键一并补齐，不留下半张表
        self.assertEqual(cfg['local_caster_port'], 2102)


class TestMdnsSelfHeal(unittest.TestCase):
    """开机时网卡还没拿到地址，固定地址不能就此永久失效。

    现场形态：设备上电早于 DHCP，`_local_ipv4_addresses()` 返回空，原实现直接
    return False，于是整个进程生命周期内都不会再广播 —— 用户重启一次服务就好了，
    于是这个问题一直被当成"偶发"。现在交给维护循环每个周期重试一次。
    """

    def test_pending_is_false_before_start(self):
        from rtkserver.discovery.zeroconf_announcer import ZeroconfAnnouncer
        self.assertFalse(ZeroconfAnnouncer().pending)

    def test_pending_true_when_zc_open_but_not_running(self):
        from rtkserver.discovery.zeroconf_announcer import ZeroconfAnnouncer
        ann = ZeroconfAnnouncer()
        ann._zc = object()          # 只看"有 zc 且没 running"，不碰真实网络
        try:
            self.assertTrue(ann.pending)
        finally:
            ann._zc = None

    def test_no_address_keeps_watchdog_alive(self):
        """没有 IPv4 时：start() 返回 False，但维护线程已经起来（pending=True）。"""
        try:
            import zeroconf  # noqa: F401
        except ImportError:
            self.skipTest('本机未安装 zeroconf')
        from unittest.mock import patch
        from rtkserver.discovery import zeroconf_announcer as mod
        ann = mod.ZeroconfAnnouncer(web_port=5757)
        try:
            with patch.object(mod, '_local_ipv4_addresses', return_value=[]):
                self.assertFalse(ann.start())
                self.assertEqual(ann.snapshot()['state'], 'unavailable')
                self.assertTrue(ann.pending, '没有地址时也必须保留维护循环，否则网络就绪后无人重试')
                self.assertIn('重试', ann.snapshot()['last_error'])
        finally:
            ann.stop()
        self.assertFalse(ann.pending)

    def test_watchdog_retries_registration(self):
        """维护循环必须真的调用 _try_register，而不只是看着地址变化。"""
        from unittest.mock import patch
        from rtkserver.discovery import zeroconf_announcer as mod
        ann = mod.ZeroconfAnnouncer(web_port=5757)
        calls = []

        def fake_try():
            calls.append(1)
            return True

        ann._zc = object()
        ann._state = 'unavailable'
        # 让 wait() 第一次返回 False（=周期到），第二次返回 True（=退出）
        seq = iter([False, True])
        with patch.object(ann._watch_stop, 'wait', side_effect=lambda _t: next(seq)), \
             patch.object(ann, '_try_register', side_effect=fake_try):
            ann._watch_loop()
        ann._zc = None
        self.assertEqual(len(calls), 1, '未广播成功时，每个周期都要重试一次')


class TestProxyHint(unittest.TestCase):
    """「固定地址打不开但 IP 正常」= 客户端代理拦了 .local（实测 502）。

    这条排查结论写过一次就丢，新人/用户还是会当成程序坏了，所以锁进文档、
    安装输出和前端提示里。
    """

    def test_frontend_hint_paired(self):
        with open(os.path.join(REPO_ROOT, 'static', 'i18n.js'), 'r', encoding='utf-8') as f:
            text = f.read()
        self.assertEqual(text.count("'rs.mdns.hint'"), 2, 'rs.mdns.hint 必须中英文各一条')

    def test_status_page_renders_hint(self):
        with open(os.path.join(REPO_ROOT, 'templates', 'index.html'), 'r', encoding='utf-8') as f:
            html = f.read()
        self.assertIn('data-i18n="rs.mdns.hint"', html)

    def test_installers_print_proxy_hint(self):
        for name in ('install.sh', 'update.sh'):
            with open(os.path.join(REPO_ROOT, name), 'r', encoding='utf-8') as f:
                text = f.read()
            self.assertIn('502', text, f'{name} 的访问提示没有说明 502 是代理导致的')

    def test_announcer_logs_proxy_hint(self):
        from rtkserver.discovery import zeroconf_announcer as mod
        with open(mod.__file__, 'r', encoding='utf-8') as f:
            text = f.read()
        self.assertIn('代理', text.split('def _log_started')[1][:600])


# ==================== 标定模块（calibration）隔离契约 ====================
class TestCalibrationIsolation(unittest.TestCase):
    """标定模块的四条铁律，改动一破就红。

    这组测试锁的不是业务，而是"删掉 rtkserver/calibration/ 主程序照跑"：
    一旦 core/outlets/sources 里出现 calibration，整个模块就成了主程序的
    硬依赖，租约出问题时连采集都起不来。
    """

    CALIBRATION_DIR = os.path.join(REPO_ROOT, 'rtkserver', 'calibration')

    def _py_files(self, subdir):
        base = os.path.join(REPO_ROOT, 'rtkserver', subdir)
        result = []
        for root, _dirs, files in os.walk(base):
            for name in files:
                if name.endswith('.py'):
                    result.append(os.path.join(root, name))
        return result

    def test_core_never_imports_calibration(self):
        """单向依赖：calibration → core/utils/sources 可以，反过来不行。

        这里用 AST 判真实 import，而不是 `'calibration' in text`：
        阶段 2 起 `state.py` 必须有 `calibration_hold` 这个**变量名**（gate 的门闩），
        用子串判会把这种合法用法一起判死，最后逼着人删测试而不是修代码。
        真正要拦的是"core 层去 import 标定包"，那才是硬依赖。
        """
        offenders = []
        for subdir in ('core', 'outlets', 'sources', 'web', 'utils', 'upgrade', 'discovery'):
            for path in self._py_files(subdir):
                with open(path, 'r', encoding='utf-8') as handle:
                    source = handle.read()
                relative = os.path.relpath(path, REPO_ROOT)
                # 阶段 6 的唯一例外：Web 层要驱动标定，但依赖必须是**可选**的
                # （app.py 用 try/except ImportError 包住注册，见 test_web_calibration_import_is_optional）。
                if relative.replace('\\', '/').endswith('web/api_calibration.py'):
                    continue
                try:
                    tree = ast.parse(source)
                except SyntaxError as e:
                    offenders.append(f'{relative}（无法解析：{e}）')
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if 'calibration' in alias.name.split('.'):
                                offenders.append(f'{relative} → import {alias.name}')
                    elif isinstance(node, ast.ImportFrom):
                        parts = (node.module or '').split('.')
                        if 'calibration' in parts:
                            offenders.append(f'{relative} → from {node.module} import …')
                        for alias in node.names:
                            if alias.name == 'calibration':
                                offenders.append(f'{relative} → from … import calibration')
                    elif isinstance(node, ast.Call):
                        func = node.func
                        called = getattr(func, 'attr', None) or getattr(func, 'id', '')
                        if called in ('import_module', '__import__'):
                            for arg in node.args:
                                if (isinstance(arg, ast.Constant)
                                        and isinstance(arg.value, str)
                                        and 'calibration' in arg.value):
                                    offenders.append(f'{relative} → {called}({arg.value!r})')
        self.assertEqual(offenders, [], f'这些文件反向依赖了标定模块：{offenders}')

    def test_web_calibration_import_is_optional(self):
        """web 唯一允许引用标定的地方必须被 try/except ImportError 包住。

        否则「删掉标定包，主程序照跑」这条就断了：app.py 一 import 就整体起不来。
        """
        path = os.path.join(REPO_ROOT, 'rtkserver', 'web', 'app.py')
        with open(path, 'r', encoding='utf-8') as handle:
            source = handle.read()
        index = source.index('from . import api_calibration')
        window = source[max(0, index - 400):index + 300]
        self.assertIn('try:', window)
        self.assertIn('except ImportError', window)
        self.assertIn('calibration_available', source)

    def test_core_touches_only_the_gate_slots(self):
        """core 层与标定的唯一接触面是两个 gate 槽位 + 一个看门狗函数。

        允许清单之外任何 `calibration*` 符号出现在 core 里都要红：
        例如有人图省事在 `main()` 里直接 `service.start_diagnose()`，
        那就把"删掉标定包主程序照跑"这条彻底破坏了。
        """
        allowed = {'calibration_hold', 'calibration_beat',
                   '_calibration_gate', 'CALIBRATION_BEAT_TIMEOUT'}
        offenders = []
        for subdir in ('core', 'outlets', 'sources', 'utils'):
            for path in self._py_files(subdir):
                with open(path, 'r', encoding='utf-8') as handle:
                    tree = ast.parse(handle.read())
                names = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name):
                        names.add(node.id)
                    elif isinstance(node, ast.Attribute):
                        names.add(node.attr)
                    elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                        names.add(node.name)
                    elif isinstance(node, ast.arg):
                        names.add(node.arg)
                bad = {name for name in names
                       if 'calibration' in name.lower() and name not in allowed}
                for name in bad:
                    offenders.append(f'{os.path.relpath(path, REPO_ROOT)} → {name}')
        self.assertEqual(offenders, [], f'core 层出现了允许清单之外的标定符号：{offenders}')

    def test_gate_slots_defined_in_state(self):
        """gate 的两个槽位必须在 state.py，且初值正确（心跳 0.0 是"没有标定"的哨兵）。"""
        from rtkserver.core import state
        self.assertIsInstance(state.calibration_hold, type(__import__('threading').Event()))
        self.assertFalse(state.calibration_hold.is_set(), '门闩初始必须是"开"的')
        self.assertEqual(state.calibration_beat, 0.0)

    def test_entry_shell_does_not_import_calibration(self):
        with open(os.path.join(REPO_ROOT, 'server.py'), 'r', encoding='utf-8') as handle:
            self.assertNotIn('calibration', handle.read())

    def test_package_import_has_no_side_effect(self):
        """导入包不能建库/开串口/起线程——否则"删掉本包"就不是零成本了。"""
        import importlib
        db_path = os.path.join(REPO_ROOT, 'calibration.db')
        existed = os.path.exists(db_path)
        importlib.import_module('rtkserver.calibration')
        importlib.import_module('rtkserver.calibration.store')
        importlib.import_module('rtkserver.calibration.geo')
        importlib.import_module('rtkserver.calibration.proto.nmea')
        importlib.import_module('rtkserver.calibration.proto.rtcm')
        if not existed:
            self.assertFalse(os.path.exists(db_path), 'import 阶段不允许创建 calibration.db')

    def test_schema_matches_design(self):
        """三张表 + 两个索引必须在，字段名必须与方案 §12.2 一致。"""
        import shutil
        import sqlite3
        import tempfile
        from rtkserver.calibration import store

        original = store.CALIBRATION_DB
        tmpdir = tempfile.mkdtemp(prefix='2rtk-contract-')
        store.CALIBRATION_DB = os.path.join(tmpdir, 'calibration.db')
        try:
            store.init_db()
            conn = sqlite3.connect(store.CALIBRATION_DB)
            try:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for name in ('calibration_records', 'module_profiles', 'calibration_events'):
                    self.assertIn(name, tables)
                columns = {row[1] for row in conn.execute(
                    'PRAGMA table_info(calibration_records)')}
                for field in ('module_sn', 'module_model', 'firmware', 'method',
                              'ecef_x', 'ecef_y', 'ecef_z', 'accuracy',
                              'lat', 'lon', 'height', 'samples', 'duration',
                              'verified', 'verify_delta', 'created_at', 'remark'):
                    self.assertIn(field, columns, f'calibration_records 缺字段 {field}')
                indexes = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertIn('idx_records_sn_time', indexes)
                self.assertIn('idx_records_time', indexes)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],
                                 store.SCHEMA_VERSION)
            finally:
                # 必须显式 close：Windows 上连接没关时 -wal/-shm 删不掉，rmdir 会报"目录不是空的"
                conn.close()
        finally:
            store.CALIBRATION_DB = original
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_calibration_db_is_git_ignored(self):
        """标定记录是资产也是隐私，绝不能进版本库。"""
        with open(os.path.join(REPO_ROOT, '.gitignore'), 'r', encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn('calibration.db', text)


class TestCalibrationWeb(unittest.TestCase):
    """阶段 6：一键标定 Web 层（方案 §11）。"""

    ENDPOINTS = [
        ('/calibrate', 'GET'),
        ('/api/calibration/options', 'GET'),
        ('/api/calibration/status', 'GET'),
        ('/api/calibration/detect', 'POST'),
        ('/api/calibration/start', 'POST'),
        ('/api/calibration/abort', 'POST'),
        ('/api/calibration/apply-as-source', 'POST'),
        ('/api/calibration/history', 'GET'),
    ]

    @classmethod
    def setUpClass(cls):
        from rtkserver.web.app import create_app
        cls.app = create_app()

    def _client(self, logged_in=True):
        client = self.app.test_client()
        if logged_in:
            with client.session_transaction() as session:
                session['logged_in'] = True
        return client

    def test_all_endpoints_require_login(self):
        """一条都不能漏鉴权：标定过程里有口令、精确 ECEF、模块 SN。"""
        client = self._client(logged_in=False)
        for path, method in self.ENDPOINTS:
            # 未登录时 login_required 会跳登录页；API 语义上是 302，不是 200
            resp = getattr(client, method.lower())(path)
            self.assertIn(resp.status_code, (301, 302, 401),
                          f'{method} {path} 未鉴权就能访问（{resp.status_code}）')

    def test_status_is_not_cacheable(self):
        """状态里含精确坐标与阶段口令掩码，必须禁止中间缓存。"""
        client = self._client()
        resp = client.get('/api/calibration/status')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('no-store', (resp.headers.get('Cache-Control') or ''))

    def test_busy_task_returns_409(self):
        """并发任务必须 409，不能排队去抢同一个串口。"""
        from rtkserver.web import api_calibration as web
        original = web.calib.start_cors
        web.calib.start_cors = lambda **kwargs: (False, '已有标定任务在进行，请等它结束或先取消', None)
        try:
            client = self._client()
            resp = client.post('/api/calibration/start',
                               json={'method': 'cors', 'port': 'COM9',
                                     'cors': {'host': 'h', 'port': 2101,
                                              'mountpoint': 'MP'}})
            self.assertEqual(resp.status_code, 409)
            self.assertIn('已有标定任务', resp.get_json()['error'])
        finally:
            web.calib.start_cors = original

    def test_password_never_echoed(self):
        """口令掩码：字典键名与自由文本两条路都要打码。

        用假口令做夹具，不要把现场账号写进测试——测试文件是入库的。
        """
        from rtkserver.web.api_calibration import _mask, _mask_obj
        text = '连接失败 password=Secr3t!Test user=testuser'
        masked = _mask(text)
        self.assertNotIn('Secr3t!Test', masked)
        self.assertIn('******', masked)
        obj = _mask_obj({'password': 'Secr3t!Test', 'user': 'u',
                         'nested': [{'cors_password': 'x'}]})
        self.assertEqual(obj['password'], '******')
        self.assertEqual(obj['nested'][0]['cors_password'], '******')
        self.assertEqual(obj['user'], 'u')

    def test_options_returns_grouped_modules(self):
        """型号必须由后端分组下发，前端不硬编码（加驱动就自动出现）。"""
        client = self._client()
        body = client.get('/api/calibration/options').get_json()
        self.assertTrue(body['ok'])
        for key in ('dual_mode', 'single_mode', 'all'):
            self.assertIn(key, body['modules'])
        self.assertTrue(body['modules']['all'], '至少应有一个已注册驱动')
        self.assertIn('history', body)
        self.assertIn('defaults', body)

    def test_svin_profile_options_match_backend(self):
        """页面上的 Survey-in 档位值必须都在后端的 SURVEY_PROFILES 里。

        这条漂移过一次：页面写 fast/standard/precise，后端只有 fast/standard/high，
        结果用户选「精确」必然 400，而且三档的文案数值（60 秒 / 2 m 之类）
        跟实现（600 次 / 3.0 m）完全对不上——页面看着正常，点了就报错。
        """
        import io
        import os
        import re

        from rtkserver.calibration.writer import SURVEY_PROFILES

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'templates', 'calibration.html')
        html = io.open(path, encoding='utf-8').read()
        block = html[html.index('id="cal-svin-profile"'):]
        block = block[:block.index('</select>')]
        values = re.findall(r'<option value="([^"]+)"', block)
        self.assertTrue(values, '页面上应有 Survey-in 档位选项')
        unknown = [v for v in values if v not in SURVEY_PROFILES]
        self.assertEqual(unknown, [],
                         f'页面档位 {unknown} 在后端不存在（后端只有 {sorted(SURVEY_PROFILES)}）')

        body = self._client().get('/api/calibration/options').get_json()
        self.assertEqual(sorted(item['value'] for item in body['svin_profiles']),
                         sorted(SURVEY_PROFILES),
                         'options 下发的档位必须与 writer.SURVEY_PROFILES 完全一致')

    def test_selected_module_reaches_driver_matching(self):
        """前端选的型号必须真的参与识别，否则下拉框只是个装饰。

        之前 `_worker` 调 `match_driver(channel)` 没传 `only`，前端也不提交 module，
        选什么型号都照旧全量探测——下拉框完全没效果。
        现在识别被抽到 `_detect_module`（加了型号不符/波特率兜底），所以两处都要看。
        """
        import inspect

        from rtkserver.calibration import service as calib

        worker_src = inspect.getsource(calib.CalibrationService._worker)
        self.assertIn('self._detect_module(', worker_src,
                      '_worker 必须通过 _detect_module 识别')
        self.assertIn('lease, only', worker_src,
                      '_worker 必须把所选型号传进识别函数')
        detect_src = inspect.getsource(calib.CalibrationService._detect_module)
        self.assertIn('only=only', detect_src,
                      '_detect_module 必须把 only 传给 match_driver(only=...)')
        self.assertIn("task.get('module')", worker_src,
                      '_worker 必须从任务参数里取 module')

        for name in ('start_cors', 'start_survey_in', 'start_write'):
            sig = inspect.signature(getattr(calib.CalibrationService, name))
            self.assertIn('module', sig.parameters,
                          f'{name} 必须接收 module 参数，否则 Web 层传不进来')


class TestCalibrationScenarios(unittest.TestCase):
    """四类标定场景的边界（用户口径）。

    这组测试锁的是"哪种场景该动模块、哪种一根手指都不许碰"，
    以及 RTCM 输出选配（1005/1006、MSM4/MSM7、沿用默认）的基本契约。
    全部是源码/纯逻辑断言，不碰硬件。
    """

    @classmethod
    def setUpClass(cls):
        cls._root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _source(self, *parts):
        path = os.path.join(self._root, *parts)
        with io.open(path, encoding='utf-8') as handle:
            return handle.read()

    def test_external_rover_only_measures(self):
        """只测不写：采集完只落库，绝不能出现任何写模块的动作。

        这块模块可能是别人正在用的流动站，也可能是没有命令集的未知模块。
        一旦这里混进 write_coord / switch_mode / save / restart，
        就等于"标定一个未知设备"把人家改坏了，而且没有任何报错。
        """
        from rtkserver.calibration.writer import WritePlan

        plan = WritePlan.build_cors(host='h', port=8002, mountpoint='MP',
                                    method='external_rover')
        self.assertEqual(plan.method, 'external_rover')
        self.assertEqual(plan.mode, 'rover', '只测不写时不许切基准站')
        self.assertEqual(plan.svin_mode, 'disabled', '只测不写时不许写坐标')

        source = self._source('rtkserver', 'calibration', 'writer.py')
        body = source[source.index("elif plan.method == 'external_rover':"):]
        body = body[:body.index('else:', 10)]
        for forbidden in ('_step_write_coord', '_step_switch_mode', '_step_setup_messages',
                          '_step_save', '_step_restart', '_step_verify_rtcm'):
            self.assertNotIn(forbidden, body,
                             f'只测不写分支里出现了 {forbidden}，会改动模块')
        self.assertIn('_step_cors_collect', body)
        self.assertIn('_step_inject', body)

    def test_external_rover_switches_mode_in_memory_only(self):
        """只测不写遇到基准站：**仅内存**临时切流动站，绝不 SAVEPAR（附录D）。

        这条契约有三层，少一层就会在现场出事：
          1) base 态必须临时切走 —— 否则基准站不输出 GGA，压根没法做 CORS 采集，
             用户会白等十分钟才看到"进不了固定解"；
          2) 切的时候不许写 NVM —— 写下去就成了"把别人的基准站改成了流动站"，
             而且要等下次标定才会被发现；
          3) 结束时必须切回去 —— 内存态残留同样让下游失去差分。
        所以这里同时断言"走了 set_mode_memory_only"和"没有走 persist=True 的 set_mode"。
        """
        source = self._source('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_rover_setup(self, plan):'):]
        body = body[:body.index('    def _ensure_rover(self, stage):')]
        self.assertIn("readonly and from_mode == 'base'", body,
                      '必须单独拦"只测不写 + 基准站模式"这一条')
        self.assertIn('set_mode_memory_only', body, '必须走仅内存切换')
        self.assertIn('_mem_restore_mode = from_mode', body, '必须记下原模式以便收尾恢复')

        # 只测不写分支里不许出现带 persist=True 的 set_mode（那就是写 NVM 了）
        branch = source[source.index("elif plan.method == 'external_rover':"):]
        branch = branch[:branch.index('else:', 10)]
        self.assertNotIn('persist=True', branch)
        # 收尾必须放在 finally：成功、失败、用户中止三条路径都要恢复
        self.assertIn('_restore_memory_mode()', branch)
        self.assertIn('finally:', branch)

    def test_readonly_mode_blocks_savepar(self):
        """即使有人把 SAVE 加进只测不写分支，也必须在发命令之前炸掉（附录D.3）。"""
        from rtkserver.calibration.writer import CoordinateWriter, WriteFailed, WritePlan

        plan = WritePlan.build_cors(host='h', port=8002, mountpoint='MP',
                                    method='external_rover')
        writer = CoordinateWriter(driver=None, channel=None)
        writer._plan = plan
        writer._readonly = True
        with self.assertRaises(WriteFailed) as ctx:
            writer._step_save(plan)
        self.assertIn('禁止 SAVEPAR', str(ctx.exception))
        self.assertFalse(writer._saved, '被拦下时不能把状态标成"已保存"')

    def test_memory_mode_restore_never_saves(self):
        """恢复内存模式时绝不能顺手 SAVEPAR。

        `_restore_memory_mode` 只调 `set_mode_memory_only`；如果哪天有人图省事
        写成 `set_mode(mode, persist=True)`，NVM 里就会留下"标定中途那个模式"，
        用户下次断电重启才发现模块状态不对。
        """
        source = self._source('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _restore_memory_mode(self):'):]
        body = body[:body.index('    def _step_cors_collect(self, plan):')]
        self.assertIn('set_mode_memory_only', body)
        self.assertNotIn('self.driver.save(', body)
        self.assertNotIn('persist=True', body)

    def test_coord_message_selection(self):
        """坐标报文可选 1005 / 1006 / keep，其它值必须拒绝。"""
        from rtkserver.calibration.writer import WritePlan

        base = dict(ecef=(-2148744.1, 4426641.5, 4044655.9))
        for value, expected in (('1005', '1005'), ('1006', '1006'),
                                ('keep', 'keep'), (None, '1005')):
            plan = WritePlan.build(coord_msg=value, **base)
            self.assertEqual(plan.coord_msg, expected)
        with self.assertRaises(ValueError):
            WritePlan.build(coord_msg='1004', **base)
        with self.assertRaises(ValueError):
            WritePlan.build(coord_msg='1005，1006', **base)

    def test_msm_keep_is_distinct_from_msm4(self):
        """'沿用模块默认' 必须解析成 None，不能悄悄变成 MSM4。

        两者差别很实：MSM4 会真的下发 PQTMCFGRTCM 改档位，
        keep 则一条报文配置命令都不发——用户选 keep 就是不想让流程碰报文。
        """
        from rtkserver.calibration.writer import WritePlan

        base = dict(ecef=(-2148744.1, 4426641.5, 4044655.9))
        self.assertIsNone(WritePlan.build(msm='keep', **base).msm)
        self.assertIsNone(WritePlan.build(msm=None, **base).msm)
        self.assertEqual(WritePlan.build(msm=4, **base).msm, 4)
        self.assertEqual(WritePlan.build(msm='7', **base).msm, 7)
        with self.assertRaises(ValueError):
            WritePlan.build(msm=8, **base)

    def test_both_keep_skips_message_setup_entirely(self):
        """两项都选"沿用"时，MSG_SETUP 直接返回，不下发任何报文命令。"""
        source = self._source('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_setup_messages(self, plan):'):]
        body = body[:body.index('    def _restore_messages(self, plan):')]
        self.assertIn("plan.coord_msg == 'keep' and plan.msm is None", body,
                      '必须能识别"两项都沿用"并提前返回')
        keep_at = body.index("plan.coord_msg == 'keep' and plan.msm is None")
        first_write = body.index('self.driver.')
        self.assertLess(keep_at, first_write, '提前返回的判断必须在任何写命令之前')

    def test_svin_resume_runs_before_resurvey(self):
        """Survey-in 断点续标：先尝试复用已收敛结果，再考虑重新收敛。"""
        source = self._source('rtkserver', 'calibration', 'writer.py')
        body = source[source.index("if plan.method == 'survey_in':"):]
        body = body[:body.index("elif plan.method == 'cors':")]
        self.assertIn('_step_svin_resume', body)
        self.assertLess(body.index('_step_svin_resume'), body.index('_step_svin_cfg'),
                        '必须"先问能不能复用"，再决定重新收敛')

    def test_rtcm_decodes_1006(self):
        """1006 必须能被解出来（带天线高度），否则选 1006 就没法做收尾校验。"""
        from rtkserver.calibration.proto import rtcm

        frame = rtcm._build_1005_frame(-2148744.1234, 4426641.5678, 4044655.9012,
                                       station_id=9, antenna_height=1.5)
        info = rtcm.decode_frame(frame)
        self.assertIsNotNone(info)
        self.assertEqual(info['msg_type'], 1006)
        self.assertEqual(info['station_id'], 9)
        self.assertAlmostEqual(info['antenna_height'], 1.5, places=4)
        self.assertAlmostEqual(info['ecef_x'], -2148744.1234, places=3)


class TestCalibrationDrivers(unittest.TestCase):
    """驱动层契约：命令必须外置成数据、校验和必须算出来、键名必须是语义键。

    这三条一旦破，方案 §5.1 的"流程层不出现命令字符串"就成了口号：
    命令一旦散进流程代码，换型号就必须改流程，多机型维护直接崩盘。
    """

    @classmethod
    def setUpClass(cls):
        from rtkserver.calibration.drivers import quectel_lg290p
        cls.driver_class = quectel_lg290p.LG290PDriver

    def test_command_table_matches_documented_checksums(self):
        """表里记的校验和必须与实时 XOR 结果一致（方案 §4.1）。"""
        from rtkserver.calibration.drivers.base import checksum
        cmd = self.driver_class.CMD
        expect = self.driver_class.CS_EXPECT
        self.assertTrue(expect, '至少要有一条带校验和常量的命令')
        mismatched = []
        for key, expected in expect.items():
            actual = checksum(cmd[key])
            if actual != expected.upper():
                mismatched.append(f'{key}: 表={expected} 实算={actual}')
        self.assertEqual(mismatched, [])

    def test_command_table_has_no_framing(self):
        """命令表只存命令体：带 `$` 或 `*` 说明有人把校验和抄进来了。"""
        for key, body in self.driver_class.CMD.items():
            self.assertNotIn('$', body, f'{key} 的命令体不该带 $')
            self.assertNotIn('*', body, f'{key} 的命令体不该带校验和')
            self.assertNotIn('\r', body, f'{key} 的命令体不该带 CRLF')
            self.assertTrue(body.isascii(), f'{key} 的命令体含全角字符：{body!r}')

    def test_semantic_keys_not_vendor_names(self):
        """键名是语义键（set_base），不是命令名（PQTMCFGRCVRMODE,W,2）。"""
        keys = list(self.driver_class.CMD)
        self.assertIn('set_base', keys)
        self.assertIn('get_svin', keys)
        self.assertIn('save', keys)
        for key in keys:
            self.assertNotIn('PQTM', key.upper(), f'键名 {key} 混进了厂家命令名')

    def test_caps_declared(self):
        from rtkserver.calibration.drivers.quectel_lg290p import CAPS
        for key in ('dual_mode', 'set_mode', 'svin', 'fixed', 'ant_info', 'ant_delta'):
            self.assertIn(key, CAPS)
        # LG290P 不支持 PQTMCFGANTDELTA（指令全集 §6.5 原文），能力位必须是 False
        self.assertFalse(CAPS['ant_delta'])

    def test_msm_configured_per_constellation(self):
        """MSM 必须按星座分别下发/查询，不能统一别名成 107X。

        真机（LG290P03AANR01A03S）实测：
            R,RTCM3-107X → OK,RTCM3-107X,1,0    GPS 开着
            R,RTCM3-108X → OK,RTCM3-108X,0,0    GLONASS 关着
            R,RTCM3-109X → OK,RTCM3-109X,0,0    Galileo 关着
            R,RTCM3-112X → OK,RTCM3-112X,0,0    BDS 关着
        驱动过去把任意 10xX 都路由成 `RTCM3-107X`，结果是「查 108X 返回的是 107X
        的值」，看着全开、实际只有 GPS 在播 —— 下游只能吃 GPS 差分，卫星数砍半。
        """
        from rtkserver.calibration.drivers import quectel_lg290p as drv
        # 命令模板必须带 {name}
        self.assertIn('{name}', drv.CMD['msg_rate_msm'])
        self.assertIn('{name}', drv.CMD['get_msg_rate_msm'])
        # 方法签名必须能接星座名
        import inspect
        self.assertIn('name', inspect.signature(drv.LG290PDriver.set_msm_rate).parameters)
        source = inspect.getsource(drv.LG290PDriver.set_message_rate)
        self.assertIn('self.set_msm_rate(rate, 0, name)', source,
                      'set_message_rate 必须把星座名透传给 set_msm_rate')
        source = inspect.getsource(drv.LG290PDriver.get_message_rate)
        self.assertIn("get_msg_rate_msm', name=name", source,
                      '查询必须带星座名，不能只查 107X')

    def test_constellation_list_reachable_from_instance(self):
        """星座清单必须能**从实例上**取到，不能只定义在模块级。

        流程层的写法是 `getattr(self.driver, 'MSM_CONSTELLATIONS')`——
        常量只挂在模块上时这里取到 None，然后静默退化成"只开 107X"，
        代码看着完全正确，结果基站只播 1074（2026-11-02 真机踩到：标定成功，
        但 `cors_msm.opened` 只有 `RTCM3-107X` 一项）。
        """
        from rtkserver.calibration.drivers import base
        from rtkserver.calibration.drivers import quectel_lg290p as drv
        self.assertTrue(hasattr(base.BaseDriver, 'MSM_CONSTELLATIONS'),
                        '基类要给默认值，否则 getattr 的 fallback 会散落在流程里')
        names = getattr(drv.LG290PDriver, 'MSM_CONSTELLATIONS', None)
        self.assertTrue(names, '驱动类上必须能取到星座清单')
        for name in ('RTCM3-107X', 'RTCM3-108X', 'RTCM3-109X',
                     'RTCM3-111X', 'RTCM3-112X'):
            self.assertIn(name, names)

    def test_backup_covers_all_constellations(self):
        """备份要覆盖 107X~112X，否则还原会把多星座悄悄关掉。"""
        from rtkserver.calibration import backup
        for name in ('RTCM3-107X', 'RTCM3-108X', 'RTCM3-109X',
                     'RTCM3-111X', 'RTCM3-112X'):
            self.assertIn(name, backup.RATE_TARGETS)

    def test_registry_and_grouping(self):
        from rtkserver.calibration.drivers import driver_manager
        modules = driver_manager.list_supported_modules()
        ids = [item['id'] for item in modules['all']]
        self.assertIn('lg290p', ids)
        lg = [item for item in modules['dual_mode'] if item['id'] == 'lg290p']
        self.assertEqual(len(lg), 1, 'lg290p 应归入双模分组')
        self.assertTrue(lg[0]['caps']['svin'])
        self.assertEqual(driver_manager.get_driver_by_id('lg290p').MODEL_KEY, 'lg290p')
        self.assertIsNone(driver_manager.get_driver_by_id('nonexistent'))


class TestCalibrationCommandFidelity(unittest.TestCase):
    """命令表与官方文档的逐字一致性（2026-11 三份官方 PDF 复核后锁定）。

    下面每条期望值都抄自官方原文（GNSS 协议规范 V1.2 / 基站模式应用指导 V1.1），
    连校验和一起抄。谁改了模板里的逗号、空格、小数位，这里立刻红——
    因为参数格式一变校验和就变，模块那边只会回 ERROR,1，现场根本看不出是格式错。
    """

    # 官方文档原文示例：(语义键, 参数, 官方完整命令串)
    DOC_EXAMPLES = (
        # 基站模式应用指导 §2.2.3 / 协议规范 §2.3.22
        ('set_svin', dict(count=3600, acc='1.2'), '$PQTMCFGSVIN,W,1,3600,1.2,0,0,0*0A'),
        ('set_svin', dict(count=60, acc='15.0'), '$PQTMCFGSVIN,W,1,60,15.0,0,0,0*3E'),
        ('set_svin_hold', dict(count=3600, acc='1.2', distance='2.5'),
         '$PQTMCFGSVIN,W,1,3600,1.2,0,0,0,2.5*0F'),
        ('set_svin_hold', dict(count=3600, acc='1.2', distance='1.5'),
         '$PQTMCFGSVIN,W,1,3600,1.2,0,0,0,1.5*0C'),
        ('set_fixed', dict(x='-2519265.0514', y='4849534.9045', z='3277834.6432'),
         '$PQTMCFGSVIN,W,2,0,0,-2519265.0514,4849534.9045,3277834.6432*2A'),
        # 协议规范 §2.3.39 / 基站指导 §2.2.2。
        # **有意偏离官方示例**：文档把仰角写成 `-90.0`，真机（LG290P03AANR01A03S /
        # 2024-05-22）对该写法回 `ERROR,1`，只有整数 `-90` 才通过。命令表因此固定写
        # `-90`；偏差记录在 docs/quectel-lg290p-calibration-commands.zh.md §6.2。
        ('set_rtcm', dict(msm=4, mode=0, eph_mode=1, eph_interval=0),
         '$PQTMCFGRTCM,W,4,0,-90,07,06,1,0*25'),
        # 协议规范 §2.3.15 全部示例
        ('msg_rate', dict(name='GGA', rate=1), '$PQTMCFGMSGRATE,W,GGA,1*0A'),
        ('msg_rate', dict(name='RTCM3-1005', rate=1), '$PQTMCFGMSGRATE,W,RTCM3-1005,1*59'),
        ('msg_rate_ver', dict(name='RTCM3-107X', rate=1, ver=0),
         '$PQTMCFGMSGRATE,W,RTCM3-107X,1,0*2F'),
        ('msg_rate_ver', dict(name='PQTMSVINSTATUS', rate=1, ver=1),
         '$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1*58'),
        ('msg_rate_ver', dict(name='PQTMSVINSTATUS', rate=0, ver=1),
         '$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,0,1*59'),
        ('msg_rate_ver', dict(name='PQTMEPE', rate=1, ver=2),
         '$PQTMCFGMSGRATE,W,PQTMEPE,1,2*1D'),
        ('get_msg_rate', dict(name='GGA'), '$PQTMCFGMSGRATE,R,GGA*12'),
        ('get_msg_rate', dict(name='RTCM3-1005'), '$PQTMCFGMSGRATE,R,RTCM3-1005*41'),
        ('get_msg_rate_ver', dict(name='PQTMEPE', ver=2), '$PQTMCFGMSGRATE,R,PQTMEPE,2*05'),
        # 协议规范 §2.3.46
        ('set_ant_info', dict(desc='', setup_id=0, sn=''), '$PQTMCFGANTINF,W,,0,*27'),
    )

    @classmethod
    def setUpClass(cls):
        from rtkserver.calibration.drivers import quectel_lg290p
        cls.driver = quectel_lg290p

    def test_render_matches_official_examples(self):
        from rtkserver.calibration.drivers.base import checksum
        cmd = self.driver.LG290PDriver.CMD
        for key, kwargs, expected in self.DOC_EXAMPLES:
            body = expected.lstrip('$').split('*')[0]
            rendered = cmd[key].format(**kwargs)
            self.assertEqual(rendered, body, f'{key} 与官方原文不一致：{rendered}')
            self.assertEqual('$' + body + '*' + checksum(body), expected,
                             f'{key} 校验和与官方原文不一致')

    def test_queried_pqtm_statements_carry_msg_ver(self):
        """PQTM 语句查询必须带 <MsgVer>，否则模块回 ERROR,1（§2.3.15 注意 1）。"""
        from rtkserver.calibration.drivers.quectel_lg290p import msg_ver_for
        self.assertEqual(msg_ver_for('PQTMSVINSTATUS'), 1)
        self.assertEqual(msg_ver_for('PQTMEPE'), 2, 'PQTMEPE 的语句版本是 2，不是 1')
        self.assertIsNone(msg_ver_for('GGA'), 'NMEA 语句必须省略 MsgVer')
        self.assertIsNone(msg_ver_for('RTCM3-1005'))
        self.assertIn('get_msg_rate_ver', self.driver.LG290PDriver.CMD)

    def test_rate_is_per_fix_not_hz(self):
        """<Rate> 的语义是"每 N 次定位输出一次"，范围按语句类型分档（表 6）。

        下限必须是 0：`<Rate>` 的定义是"0 或 N，0 = 不输出"（表 6 参数表第一行），
        真机对一个从未配置过的模块读 1005 就回 `OK,RTCM3-1005,0`。还原要用到它 ——
        标定把 1005 从 0 打开到 1，还原得能关回 0。
        """
        from rtkserver.calibration.drivers.quectel_lg290p import rate_range
        self.assertEqual(rate_range('GGA'), (0, 255))
        self.assertEqual(rate_range('RMC'), (0, 255))
        self.assertEqual(rate_range('RTCM3-1005'), (0, 1200))
        self.assertEqual(rate_range('RTCM3-107X'), (0, 1200))
        self.assertEqual(rate_range('RTCM3-1019'), (0, 1))
        self.assertEqual(rate_range('RAW-QZSSL6'), (0, 1))


class TestCalibrationParsers(unittest.TestCase):
    """解析层：官方示例必须能被还原成正确数值，且 <Valid> 不能被布尔量覆盖。"""

    def test_svin_status_official_examples(self):
        from rtkserver.calibration.drivers.base import parse_line

        def one(text):
            parsed = parse_line(text)
            self.assertEqual(parsed['type'], 'SVINSTATUS')
            self.assertTrue(parsed['valid'], '解析成功标志')
            return parsed

        # 基站模式应用指导 §3.2.2 原文：59/60 仍是"收敛中"，60/60 才"已收敛"
        mid = one('$PQTMSVINSTATUS,1,291322000,1,,11,59,60,'
                  '-2005559.8530,5411823.2016,2706139.4032,1.7830*31')
        self.assertEqual(mid['state'], 1)
        self.assertFalse(mid['done'])
        self.assertEqual((mid['obs'], mid['cfg_count']), (59, 60))
        self.assertAlmostEqual(mid['progress'], 59 / 60, places=9)
        self.assertEqual(mid['mean_x'], -2005559.8530)
        self.assertEqual(mid['mean_acc'], 1.7830)

        done = one('$PQTMSVINSTATUS,1,291323000,2,,11,60,60,'
                   '-2005559.8481,5411823.1873,2706139.3995,1.8075*3F')
        self.assertEqual(done['state'], 2)
        self.assertTrue(done['done'])
        self.assertEqual(done['progress'], 1.0)

        # 协议规范 §2.3.23 原文示例（Res0 为空）
        empty = one('$PQTMSVINSTATUS,1,1000,1,,01,20,100,'
                    '-2484434.3645,4875976.9741,3266161.3412,1.2415*3C')
        self.assertEqual((empty['obs'], empty['cfg_count']), (20, 100))
        self.assertAlmostEqual(empty['progress'], 0.2, places=9)

    def test_boot_frame_differs_from_query_reply(self):
        """`$PQTMVER`（开机，6 字段）与 `$PQTMVERNO`（查询应答，5 字段）字段错位一位。"""
        from rtkserver.calibration.drivers.base import parse_line
        boot = parse_line('$PQTMVER,1,MODULE,LG290P03AANR01A03S,2024/05/22,18:09:06*36')
        self.assertEqual(boot['type'], 'VER')
        self.assertEqual(boot['msg_ver'], '1')
        self.assertEqual(boot['ver_name'], 'MODULE')
        self.assertEqual(boot['ver_str'], 'LG290P03AANR01A03S')
        self.assertEqual(boot['build_date'], '2024/05/22')
        query = parse_line('$PQTMVERNO,LG290P03AANR01A03S,2024/05/22,18:09:06*1C')
        self.assertEqual(query['type'], 'VERNO')
        self.assertEqual(query['fields'][1], 'LG290P03AANR01A03S')

    def test_detect_rejects_boot_frame_like_reply(self):
        """识别绝不能把开机帧的 MsgVer `1` 当成固件串。"""
        from rtkserver.calibration.drivers import driver_manager
        channel = driver_manager._fake_channel()
        channel._serial._reply = lambda name, body: (
            'PQTMVERNO,1,MODULE,LG290P03AANR01A03S'
            if name == 'PQTMVERNO' else f'{name},OK')
        driver, message, _attempts = driver_manager.match_driver(channel)
        self.assertIsNone(driver, f'应拒绝字段错位的识别应答，实际认领为 {message}')

    def test_policy_classifies_parameterized_queries(self):
        from rtkserver.calibration.drivers.base import policy_for
        self.assertEqual(policy_for('PQTMCFGSVIN,R'), 'query')
        self.assertEqual(policy_for('PQTMCFGMSGRATE,R,GGA'), 'query')
        self.assertEqual(policy_for('PQTMCFGMSGRATE,R,PQTMEPE,2'), 'query')
        self.assertEqual(policy_for('PQTMCFGMSGRATE,W,GGA,1'), 'config')
        self.assertEqual(policy_for('PQTMSAVEPAR'), 'save')
        self.assertEqual(policy_for('PQTMSRR'), 'config')


# ==================== 标定租约 / 门闸契约（方案 §15.1） ====================
class TestCalibrationGateContract(unittest.TestCase):
    """锁死"让位 → 独占 → 归还"里最容易写反的四处顺序。

    这些都不是业务逻辑，而是顺序问题：顺序错了程序照样跑，只是要么数据面
    永久不启动（网页能开、设备不转发、日志没报错），要么起来就退出无限抖动。
    真机上这两种故障都极难复现，所以只能在源码层锁死。
    """

    def _read(self, *parts):
        path = os.path.join(REPO_ROOT, *parts)
        with open(path, 'r', encoding='utf-8') as handle:
            return handle.read()

    def test_gate_precedes_run_once_in_main(self):
        """门闸必须出现在 main() 的循环顶部、且早于 run_once()。"""
        source = self._read('rtkserver', 'core', 'lifecycle.py')
        main_body = source[source.index('def main('):]
        gate_at = main_body.index('if _calibration_gate():')
        run_at = main_body.index('run_once()')
        self.assertLess(gate_at, run_at, '门闸必须在 run_once() 之前，否则串口会被抢回去')

    def test_watchdog_clears_hold_without_setting_restart(self):
        """看门狗强制解除时必须"清 hold + 清 restart"。

        若这里写 `restart_event.set()`：下一轮 run_once 一起来就看到重启信号，
        立刻拆自己，形成"起来—退出"抖动，比不恢复还糟。
        """
        source = self._read('rtkserver', 'core', 'lifecycle.py')
        gate_fn = source[source.index('def _calibration_gate('):]
        gate_fn = gate_fn[:gate_fn.index('\ndef main(')]
        self.assertIn('state.calibration_hold.clear()', gate_fn)
        self.assertIn('state.restart_event.clear()', gate_fn)
        self.assertNotIn('state.restart_event.set()', gate_fn,
                         '看门狗不能置 restart_event')

    def test_release_clears_hold_before_waking_main(self):
        """release() 必须"先清 hold 再置 restart"，顺序反了数据面会抖动。"""
        source = self._read('rtkserver', 'calibration', 'lease.py')
        body = source[source.index('    def release('):]
        clear_at = body.index('state.calibration_hold.clear()')
        wake_at = body.index('state.restart_event.set()')
        self.assertLess(clear_at, wake_at,
                        '必须先 hold.clear() 再 restart_event.set()')

    def test_release_is_idempotent(self):
        """release() 必须自带幂等闸，否则 finally 里重复调用会改写终态。"""
        source = self._read('rtkserver', 'calibration', 'lease.py')
        body = source[source.index('    def release('):]
        self.assertIn('self._released', body)
        self.assertIn('if self._released:', body)

    def test_worker_always_releases_in_finally(self):
        """工作线程的 finally 必须无条件归还租约 —— 这是整个功能的安全带。"""
        source = self._read('rtkserver', 'calibration', 'service.py')
        worker = source[source.index('    def _worker('):]
        finally_at = worker.index('finally:')
        self.assertIn('lease.release(', worker[finally_at:])

    @staticmethod
    def _production_strings(source):
        """取出**生产代码**里的字符串常量：跳过 docstring 与自测夹具。

        为什么要跳过自测夹具：`_selftest` 里必须写假模块的应答（那本来就是
        "假装自己是 LG290P"），把它算违规等于逼着人删掉自测。
        真正要拦的是流程代码里冒出可直接下发的命令串。
        """
        tree = ast.parse(source)
        skip = set()
        fixtures = {'_selftest', '_fake_serial_class'}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, 'body', [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    skip.add(id(body[0].value))          # docstring
        for node in tree.body:
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in fixtures):
                skip.update(id(child) for child in ast.walk(node))
        return [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skip]

    def test_service_layer_has_no_vendor_commands(self):
        """流程层不许出现可下发的厂家命令（方案 §5.1.1）。

        判据有两条：
            `$PQTM…`  —— 完整的帧，能直接写到串口去，必须只能来自驱动命令表；
            `PQTM…,W,…` —— 写命令，哪怕只拼了个片段也不许出现在流程层。
        只读的命令名当"来源标注"（例如记录里写 `source=PQTMCFGSVIN`）是允许的：
        它不构成一次下发，驱动依然是唯一出口。
        """
        for name in ('service.py', 'lease.py'):
            source = self._read('rtkserver', 'calibration', name)
            offenders = []
            for text in self._production_strings(source):
                if '$PQTM' in text or ('PQTM' in text and ',W,' in text):
                    offenders.append(text)
            self.assertEqual(offenders, [],
                             f'{name} 的流程代码里出现了厂家命令字面量：{offenders}')

    def test_heartbeat_written_with_gate(self):
        """置 hold 的同一段代码里必须写心跳初值，否则 1 秒内被看门狗误杀。"""
        source = self._read('rtkserver', 'calibration', 'lease.py')
        body = source[source.index('    def acquire('):]
        body = body[:body.index('def _precheck(')]
        hold_at = body.index('state.calibration_hold.set()')
        beat_at = body.index('state.calibration_beat = time.time()')
        self.assertLess(beat_at, hold_at, '心跳必须与 gate 同时（甚至先）写入')


class TestCalibrationWriteChain(unittest.TestCase):
    """阶段3 写入链路的四条硬约束（方案 §8）。

    这一组锁的全是"顺序"和"什么时候不许动手"：
    顺序错了程序照样跑，但会把模块留在最糟的状态上（基准站 + 零坐标），
    或者留下一个"内存与 NVM 不一致"的隐形炸弹 —— 真机上都要几天后才炸。
    """

    def _read(self, *parts):
        path = os.path.join(REPO_ROOT, *parts)
        with open(path, 'r', encoding='utf-8') as handle:
            return handle.read()

    def test_step_order_matches_design(self):
        """必须是 写坐标 → 切模式 → 配报文 → 保存 → 重启 → 回读 → 1005 → 落库。

        §8.1 的"为什么先写坐标再切模式"：反过来一旦写坐标失败，模块已经停在
        "基准站 + 坐标 0,0,0"，播出去的是几内亚湾的基准站，所有流动站被带偏几千公里。
        """
        from rtkserver.calibration import writer
        self.assertEqual(writer.STEPS,
                         ('WRITE_COORD', 'BASE_SETUP', 'MSG_SETUP', 'SAVE',
                          'RESTART', 'VERIFY_MODE', 'VERIFY_RTCM', 'INJECT'))

        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def run(self, plan, backup=None):'):]
        body = body[:body.index('    # ---------- 失败收尾')]
        # 阶段名与实现函数名不是一一对应（切模式的阶段叫 BASE_SETUP，实现叫 _step_switch_mode），
        # 所以这里显式列映射：漏一个、顺序错一个都红。
        #
        # 注意：Survey-in（P3）是**前置分支**，顺序是
        #   BASE_SETUP → SVIN_CFG → SVIN_WAIT → WRITE_COORD → …（必须先切基站才能收敛）
        # 而固定坐标主链路（§8.1）是 WRITE_COORD → BASE_SETUP → …（先写坐标再切模式，
        # 防止写坐标失败时模块停在"基准站 + 0,0,0"）。
        # 两条链路后半段（配报文→保存→重启→校验→落库）完全复用。这里锁的是复用段
        # 与"先写坐标再切模式"的 §8.1 主链路；survey-in 的先后顺序由 test_survey_in_order 锁。
        methods = {
            'WRITE_COORD': ('self._step_write_coord(', 'self._step_config_svin('),
            'BASE_SETUP': ('self._step_switch_mode(',),
            'MSG_SETUP': ('self._step_setup_messages(',),
            'SAVE': ('self._step_save(',),
            'RESTART': ('self._step_restart(',),
            'VERIFY_MODE': ('self._step_verify_mode(',),
            'VERIFY_RTCM': ('self._step_verify_rtcm(',),
            'INJECT': ('self._step_inject(',),
        }
        self.assertEqual(sorted(methods), sorted(writer.STEPS), '映射漏了阶段')
        # 主链路（固定坐标 / 还原）在 run() 的 else 分支里，后面那段是复用的公共尾段。
        # survey-in 分支里 `_step_write_coord` 出现在 `_step_switch_mode` 之后（先切基站
        # 才能收敛），用整个 body 的 index 会把它误当成主链路的 WRITE_COORD。
        # 所以这里把"写坐标→切模式"这一段限定在 else 分支内验证，公共尾段单独验证。
        else_branch = body[body.index('            else:'):
                           body.index('            self._step_setup_messages(plan)')]
        positions = []
        for step in writer.STEPS:
            if step in ('WRITE_COORD', 'BASE_SETUP'):
                found = [else_branch.index(name) for name in methods[step]
                         if name in else_branch]
                self.assertTrue(found, f'else 分支里没有 {step} 对应的实现')
                positions.append(min(found))
            else:
                # 公共尾段：配报文→保存→重启→校验→落库，在 else 分支结束之后。
                tail = body[body.index('            self._step_setup_messages(plan)'):]
                name = methods[step][0]
                self.assertIn(name, tail, f'公共尾段里没有 {step} 对应的实现')
                positions.append(tail.index(name) + 10_000)  # 保证尾段都排在 WRITE_COORD/BASE_SETUP 之后
        self.assertEqual(positions, sorted(positions),
                         '写链路的执行顺序必须与 STEPS 一致')
        # 保存必须早于重启：先重启会把没保存的配置丢掉（§8.4）
        self.assertLess(body.index('self._step_save(plan)'),
                        body.index('self._step_restart(plan)'),
                        'SAVEPAR 必须早于 SRR')

    def test_survey_in_order(self):
        """Survey-in 分支的顺序：BASE_SETUP → SVIN_CFG → SVIN_WAIT → WRITE_COORD。

        与主链路相反（先切模式再写坐标），因为 $PQTMSVINSTATUS 只在基站模式输出，
        必须先切基站并生效，模块才开始收敛。这是任务书 P3 明确规定的顺序。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def run(self, plan, backup=None):'):]
        body = body[:body.index('    # ---------- 失败收尾')]
        branch = body[body.index("if plan.method == 'survey_in':"):
                      body.index('            else:')]
        order = ['self._step_switch_mode(plan)',
                 'self._step_svin_cfg(plan)',
                 'self._step_svin_wait(plan)',
                 'self._step_write_coord(plan)']
        pos = [branch.index(name) for name in order]
        self.assertEqual(pos, sorted(pos), 'Survey-in 分支顺序必须 BASE_SETUP → SVIN_CFG → SVIN_WAIT → WRITE_COORD')

    def test_thresholds_match_design(self):
        """三个判据的数值是方案定死的，改动等于改了验收标准。"""
        from rtkserver.calibration import writer
        self.assertEqual(writer.COORD_TOLERANCE, 0.001, '§8.2 回读门槛 1 mm')
        self.assertEqual(writer.RTCM_WARN_LIMIT, 0.05, '§8.6 1005 告警 5 cm')
        self.assertEqual(writer.RTCM_FAIL_LIMIT, 0.5, '§8.6 1005 判失败 50 cm')
        self.assertLess(writer.RTCM_WARN_LIMIT, writer.RTCM_FAIL_LIMIT)

    def test_revert_only_before_save(self):
        """保存成功后绝不再回写内存（会造成内存与 NVM 不一致）。"""
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _finish_failure(self, error):'):]
        body = body[:body.index('    def _abort_cleanup(')]
        saved_branch = body.index('if self._saved:')
        revert_at = body.index('self._revert()')
        self.assertLess(saved_branch, revert_at)
        # 保存成功那条分支里不许出现回写调用
        saved_block = body[saved_branch:body.index('elif not self._touched:')]
        self.assertNotIn('self._revert()', saved_block,
                         '已保存进 NVM 的配置不能靠回写内存来"撤销"')

    def test_no_revert_when_lease_evicted(self):
        """租约被看门狗回收时禁止回写：串口已经还给数据面，再发命令就是抢字节。"""
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _abort_cleanup(self, error):'):]
        body = body[:body.index('    def _revert(self):')]
        evicted_at = body.index("getattr(error, 'evicted', False)")
        revert_at = body.index('self._revert()')
        self.assertLess(evicted_at, revert_at, '必须先判 evicted 再决定要不要回写')

    def test_evicted_flag_declared_on_exceptions(self):
        """`TaskEvicted.evicted=True` / `TaskAborted.evicted=False` 是 writer 的判据来源。"""
        from rtkserver.calibration import service
        self.assertTrue(service.TaskEvicted.evicted)
        self.assertFalse(service.TaskAborted.evicted)
        self.assertTrue(issubclass(service.TaskEvicted, Exception))

    def test_service_passes_checkpoint_to_writer(self):
        """写链路必须能被打断：不传 checkpoint，用户点取消要等 1005 超时 20 秒。"""
        source = self._read('rtkserver', 'calibration', 'service.py')
        body = source[source.index('    def _run_write(self, driver, lease, task):'):]
        body = body[:body.index('    def _writer_stage(')]
        self.assertIn('CoordinateWriter(', body)
        self.assertIn('checkpoint=self._checkpoint', body)
        self.assertIn('on_record=self._save_record', body)

    def test_failed_write_not_stored_as_history(self):
        """失败的写入不落 calibration_records —— 历史坐标里不能有"从没生效过"的值。"""
        source = self._read('rtkserver', 'calibration', 'service.py')
        body = source[source.index('    def _run_write(self, driver, lease, task):'):]
        body = body[:body.index('    def _writer_stage(')]
        except_at = body.index('except WriteFailed as e:')
        tail = body[except_at:except_at + 900]
        self.assertNotIn('self._save_record(', tail,
                         '失败路径只能记 events，不能往 records 里塞未生效的坐标')
        self.assertIn("self._record_events('FAILED'", tail)

    def test_restore_reuses_the_same_pipeline(self):
        """还原必须复用写链路，不能另写一套没人测的代码。"""
        from rtkserver.calibration import writer
        snapshot = {'mode': {'code': 1, 'text': 'rover'}, 'ok': True,
                    'svin': {'mode': 'fixed', 'ecef_x': -2519265.0514,
                             'ecef_y': 4849534.9045, 'ecef_z': 3277834.6432},
                    'rtcm': {'msm_type': 4}, 'rates': {}}
        plan = writer.WritePlan.from_backup(snapshot)
        self.assertEqual(plan.kind, 'restore')
        self.assertEqual(plan.mode, 'rover')
        self.assertFalse(plan.verify_rtcm, '还原到移动站时不该去等 1005')
        self.assertEqual(plan.written()['x'], round(-2519265.0514, 4))

    def test_restore_replays_backup_instead_of_rebuilding(self):
        """还原必须"原样放回"，不能照搬"把模块配成合格基准站"的那套动作。

        真机第一次还原暴露的就是这个：链路把备份里 `RTCM3-1005=0` 改成 1，
        界面显示"还原完成"，报文却比标定前多播一路。这类错误不报任何异常，
        只能靠"还原分支里不许出现 1005=1 这种目标值"来锁住。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _restore_messages(self, plan):'):]
        # `_step_save` 现在带 `attempt` 参数（1005 不符时会走"重下发一轮"），
        # 所以按签名前缀切，不要写死 `def _step_save(self, plan):`。
        body = body[:body.index('    def _step_save(self, plan')]
        self.assertIn('restore_rtcm_config', body, '必须原样回放 RTCM 七字段')
        self.assertIn('plan.rates', body, '必须按备份逐条回放报文速率')
        self.assertNotIn("set_message_rate('RTCM3-1005', 1)", body,
                         '还原不许强制打开 1005（那是标定目标，不是还原目标）')
        # 分派必须在写入动作之前：先判 kind，再决定走哪条
        step = source[source.index('    def _step_setup_messages(self, plan):'):]
        step = step[:step.index('    def _restore_messages(self, plan):')]
        self.assertIn("plan.kind == 'restore'", step)
        # 坐标报文由用户选（1005 / 1006），变量名是 coord_name；
        # 这条断言只关心"restore 分支要在打开坐标报文之前就返回"
        self.assertLess(step.index("plan.kind == 'restore'"),
                        step.index('set_message_rate(coord_name, 1)'))

    def test_restore_does_not_add_history_record(self):
        """还原不是新的标定成果：不许往 calibration_records 里加一行。

        历史坐标列表是给"从里面挑一个写进去"用的。混进一条"标定前的老坐标"，
        用户下次选坐标就会拿到一个自己从没标定过的值。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_inject(self, plan):'):]
        body = body[:body.index('    def build_record(self, plan):')]
        start = body.index("if plan.kind == 'restore':")
        marker = body.index("self._stage('INJECT', '标定结果上链")
        head = body[start:marker]
        self.assertNotIn('_on_record(', head, '还原分支不能调落库回调')
        self.assertIn('return', head)
        self.assertIn('_on_record(record)', body[marker:], '正常写入仍然要落库')

    def test_restore_rtcm_fields_are_whitelisted(self):
        """`restore_rtcm_config` 只放行"7 个纯数字字段"，其余一律拒绝。

        这些值来自我们自己读回来的应答，但 `backup.json` 是磁盘上的普通 JSON，
        可以被手改；拼进命令前不校验，就等于把串口交给一个文本文件。
        """
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        class Probe(LG290PDriver):
            def __init__(self):
                self.sent = []

            def request(self, name, **kwargs):
                self.sent.append((name, kwargs))
                return True, 'OK', {}

        probe = Probe()
        ok, _message, _data = probe.restore_rtcm_config(
            ['4', '0', '-90', '07', '06', '1', '0'])
        self.assertTrue(ok)
        self.assertEqual(probe.sent,
                         [('set_rtcm_fields', {'fields': '4,0,-90,07,06,1,0'})],
                         '七字段必须原样拼回去，不能重排或补默认值')

        probe = Probe()
        self.assertFalse(probe.restore_rtcm_config(['4', '0', '-90'])[0],
                         '字段不足 7 个必须拒绝')
        self.assertEqual(probe.sent, [])
        for bad in (['4', '0', '-90', '07', '06', '1', '0;reboot'],
                    ['4', '0', '-90', '07', '06', '1', ''],
                    ['4', '0', '-90', '07', '06', '1', 'x']):
            probe = Probe()
            ok, message, _data = probe.restore_rtcm_config(bad)
            self.assertFalse(ok, f'非法字段必须拒绝：{bad}')
            self.assertEqual(probe.sent, [], f'拒绝时不许下发命令：{bad}')

    def test_plan_from_backup_keeps_fidelity_inputs(self):
        """备份里的七字段 / 速率 / 偏移 / 天线信息都要进意图，供还原分支使用。"""
        from rtkserver.calibration import writer
        plan = writer.WritePlan.from_backup({
            'mode': {'code': 1, 'text': 'rover'}, 'ok': True,
            'svin': {'mode': 'disabled'},
            'rtcm': {'fields': ['3', '0', '-90', '07', '06', '0', '3600'], 'msm_type': 3},
            'rates': {'RTCM3-1005': 0, 'GGA': 1, 'RTCM3-1033': None},
            'offsets': {'RTCM3-107X': 0},
            'antenna': {'desc': '2RTK-X', 'setup_id': 3, 'sn': 'SN1'},
        })
        self.assertEqual(plan.rtcm_fields, ['3', '0', '-90', '07', '06', '0', '3600'])
        self.assertEqual(plan.rates, {'RTCM3-1005': 0, 'GGA': 1},
                         'rate=0 是"关闭"，必须保留；None 是"没读到"，必须丢弃')
        self.assertEqual(plan.offsets, {'RTCM3-107X': 0})
        self.assertEqual(plan.antenna['desc'], '2RTK-X')
        self.assertIsNone(plan.survey_in)
        summary = plan.summary()
        self.assertEqual(summary['kind'], 'restore')
        self.assertEqual(summary['rates'].get('RTCM3-1005'), 0)

    def test_given_coordinates_written_as_is(self):
        """用户给的坐标就是写进模块的值，不做任何相位中心换算。

        本产品的场景是"天线固定 → 测量出该点 → 钉死"，现场没有 RTK 对中杆，
        多一层 APC→ARP 偏移只会因为方向填错把整站静默抬降一个天线高。
        """
        from rtkserver.calibration import writer
        x, y, z = -2519265.0514, 4849534.9045, 3277834.6432
        plan = writer.WritePlan.build(ecef=(x, y, z))
        self.assertEqual(plan.arp, plan.apc, '写入值必须等于给定值')
        self.assertEqual(plan.arp, (x, y, z))
        # 记录里 apc_* 与 arp_* 两套值都要在，且都等于给定值
        record = writer.CoordinateWriter(None, channel=None).build_record(plan)
        self.assertAlmostEqual(record['apc_z'], z, places=4)
        self.assertAlmostEqual(record['ecef_z'], z, places=4)
        self.assertNotIn('ant_delta_u', record, '记录里不该再有天线偏移列')
        # 意图里不应存在 ant_enu 槽位
        self.assertFalse(hasattr(plan, 'ant_enu'))

    def test_record_carries_nvm_state(self):
        """`nvm_state` 必须能落库（不然"坐标自己变回去了"永远查不出来）。"""
        from rtkserver.calibration import store
        for field in ('apc_x', 'apc_y', 'apc_z', 'nvm_state', 'task_id'):
            self.assertIn(field, store.RECORD_FIELDS, f'calibration_records 缺 {field}')
        self.assertIn('saved', store.NVM_STATES)
        self.assertIn('reverted', store.NVM_STATES)
        self.assertIn('unknown', store.NVM_STATES)

    def test_no_vendor_commands_in_write_layer(self):
        """写链路与备份层同样不许出现可下发的厂家命令（与 service/lease 同一铁律）。"""
        for name in ('writer.py', 'backup.py'):
            source = self._read('rtkserver', 'calibration', name)
            offenders = []
            for text in TestCalibrationGateContract._production_strings(source):
                if '$PQTM' in text or ('PQTM' in text and ',W,' in text):
                    offenders.append(text)
            self.assertEqual(offenders, [],
                             f'{name} 里出现了厂家命令字面量：{offenders}')

    def test_backup_captures_what_restore_needs(self):
        """备份必须覆盖"还原时要用到的全部东西"：模式、坐标、速率。"""
        from rtkserver.calibration import backup

        class Driver:
            def caps(self, key, default=False):
                return True

            def get_mode(self):
                return True, 'ok', {'mode': 'rover', 'mode_code': 1}

            def get_svin_config(self):
                return True, 'ok', {'mode': 'fixed', 'mode_code': 2, 'count': 0,
                                    'acc_limit': 0.0, 'ecef_x': 1.0, 'ecef_y': 2.0,
                                    'ecef_z': 3.0}

            def read_rtcm_config(self):
                return True, 'ok', {'fields': ['4'], 'raw': ''}

            def get_message_rate(self, name):
                return True, 'ok', {'fields': [name, '1']}

            def read_antenna_info(self):
                return True, 'ok', {'desc': 'X', 'setup_id': 1, 'sn': 'S'}

        snapshot = backup.capture(Driver(), task_id='T', port='P')
        self.assertTrue(snapshot['ok'])
        self.assertEqual(snapshot['mode']['text'], 'rover')
        self.assertEqual(snapshot['svin']['ecef_z'], 3.0)
        self.assertEqual(snapshot['rtcm']['msm_type'], 4)
        for name in ('GGA', 'RTCM3-1005', 'RTCM3-1033'):
            self.assertEqual(snapshot['rates'][name], 1)
        self.assertTrue(backup.usable(snapshot)[0])

    def test_reboot_tolerates_missing_boot_frame(self):
        """§8.5 降级：等不到开机首帧但串口在出数据，也算重启完成。

        基站模式下模块只播 RTCM，部分固件不打 `$PQTMVER`；若死等首帧，
        "重启成功"会被判成"模块未重启"，而重试又要多等 6 秒。
        """
        source = self._read('rtkserver', 'calibration', 'drivers', 'base.py')
        body = source[source.index('    def _reboot_sequence(self, body):'):]
        body = body[:body.index('    # ---------- 必须由子类实现的能力')]
        self.assertIn('silent', body)
        self.assertIn("counters.get('lines'", body,
                      '必须用"串口是否还有数据"来区分"没重启"和"没打开机帧"')


class TestCalibrationCors(unittest.TestCase):
    """阶段5 CORS 差分标定的硬约束（方案 §6 / §7 / P1）。

    锁的全是"顺序""安全红线""准入/收敛阈值"：
    顺序错了会把模块留在"流动站连不上 CORS 又回不到基准站"的中间态；
    安全红线（RTCM 只进串口不进 distribute）破了下游会用别人的差分算自己的位置。
    """

    def _read(self, *parts):
        path = os.path.join(REPO_ROOT, *parts)
        with open(path, 'r', encoding='utf-8') as handle:
            return handle.read()

    def test_cors_branch_order(self):
        """CORS 分支顺序：切流动站 → 配NMEA → 等GGA → 连CORS → 等固定解 → 采集 → 断CORS。

        之后走与历史坐标相同的写入链路（写坐标 → 切基站 → 配报文 → 保存 → 重启 → 校验）。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_collect(self, plan):'):]
        body = body[:body.index('    # ---- ROVER_SETUP（§9.1）：CORS 标定的第一道门禁 ----')]
        order = ['self._step_cors_rover_setup(plan)',
                 'self._step_cors_nmea_setup(plan)',
                 'self._step_cors_gga_wait(plan)',
                 'CorsClient(',
                 'client.connect()',
                 'self._ensure_rover(\'CORS_CONNECT\')',
                 'self._step_cors_fix_and_collect(']
        pos = [body.index(name) for name in order]
        self.assertEqual(pos, sorted(pos),
                         'CORS 链路必须 ROVER_SETUP → NMEA_SETUP → GGA_WAIT '
                         '→ CORS_CONNECT → 模式复核 → FIX_WAIT/COLLECT')

    def test_cors_reads_mode_before_switching(self):
        """CORS 标定第一步必须先读模式，只有 rover 才允许注入 RTCM。

        CORS 的本质是"把 RTCM 喂给模块、让模块自己解算"，只有流动站模式成立。
        base 模式下模块自己是差分源头，收到的 RTCM 不参与解算（协议 §2.3.25
        还规定 base 自动禁用 NMEA），盲目注入只会白跑几分钟拿到一堆单点解。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_rover_setup'):]
        body = body[:body.index('    def _ensure_rover')]
        # 必须先 get_mode 再考虑 set_mode
        self.assertIn('self.driver.get_mode()', body, '必须先读当前模式')
        self.assertLess(body.index('self.driver.get_mode()'),
                        body.index("set_mode('rover'"),
                        '读模式必须发生在切模式之前')
        # 已经是 rover 就不要再切（避免无谓写 NVM + 重启）
        self.assertIn("if from_mode == 'rover'", body)
        self.assertIn('switched', body, '必须记录是否真的切换过')
        # 切完必须回读确认
        self.assertGreaterEqual(body.count('self.driver.get_mode()'), 2,
                                '切换后必须回读确认模式')
        # 注入前后都要有门禁
        guard = source[source.index('    def _ensure_rover'):]
        guard = guard[:guard.index('    # ---- NMEA_SETUP')]
        self.assertIn("if mode != 'rover'", guard)
        self.assertIn('raise WriteFailed(', guard)
        outer = source[source.index('    def _step_cors_collect'):]
        outer = outer[:outer.index('    # ---- ROVER_SETUP（§9.1）：CORS 标定的第一道门禁 ----')]
        self.assertIn("self._ensure_rover('CORS_CONNECT')", outer,
                      '连上 Caster 后、注入前必须再确认一次模式')
        self.assertIn('mode_guard=self._ensure_rover', outer,
                      '采集期间必须周期性复核模式')

    def test_cors_branch_is_wired_in_run(self):
        """run() 里 CORS 分支必须在 survey_in 分支之后、公共尾段之前，且写坐标先于切模式。"""
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def run(self, plan, backup=None):'):]
        body = body[:body.index('    # ---------- 失败收尾')]
        cors_at = body.index("elif plan.method == 'cors':")
        survey_at = body.index("if plan.method == 'survey_in':")
        write_at = body.index('self._step_write_coord(plan)', cors_at)
        switch_at = body.index('self._step_switch_mode(plan)', cors_at)
        self.assertLess(survey_at, cors_at)
        self.assertLess(write_at, switch_at,
                        'CORS 采集完后必须先写坐标再切模式（§8.1）')

    def test_cors_rtcm_only_injected_to_serial(self):
        """安全红线（§6.4）：CORS 下发的 RTCM 只注入串口，绝不进全局 distribute。"""
        import ast
        source = self._read('rtkserver', 'calibration', 'cors.py')
        tree = ast.parse(source)
        # 只查"实际调用 distribute(...)"，不查注释/docstring 里的说明文字
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, 'id', None) or getattr(func, 'attr', '')
                self.assertNotEqual(name, 'distribute',
                                    'cors.py 绝不能调用 distribute 分发')
        writer = self._read('rtkserver', 'calibration', 'writer.py')
        inject = writer[writer.index('    def _inject_rtcm_to_serial(self, data):'):]
        inject = inject[:inject.index('    def _drain_gga')]
        self.assertNotIn('distribute(', inject,
                         'RTCM 注入路径绝不能调用 distribute')

    def test_cors_error_tokens(self):
        """错误分类 token 必须齐全（§6.1）：dns/network/auth/mountpoint/sourcetable/no_data。"""
        from rtkserver.calibration import cors
        for token in ('ERR_DNS', 'ERR_NETWORK', 'ERR_AUTH', 'ERR_MOUNTPOINT',
                      'ERR_SOURCETABLE', 'ERR_NO_DATA'):
            self.assertTrue(hasattr(cors, token), f'cors 缺 {token}')
        # 确定性错误（auth/mountpoint/sourcetable）重连时必须直接上抛不重试
        source = self._read('rtkserver', 'calibration', 'cors.py')
        connect = source[source.index('    def connect(self):'):]
        connect = connect[:connect.index('    def _connect_once')]
        self.assertIn('ERR_AUTH, ERR_MOUNTPOINT, ERR_SOURCETABLE', connect,
                      '确定性错误不得退避重试')

    def test_cors_gga_software_quality_is_single_point(self):
        """软件生成 GGA 的质量位必须 =1（单点），不能谎报 4（§6.2）。"""
        source = self._read('rtkserver', 'calibration', 'cors.py')
        build = source[source.index('    def _build_software_gga(self):'):]
        self.assertIn('quality=1', build, '软件生成 GGA 质量位必须是 1（单点）')

    def test_cors_fix_and_converge_thresholds(self):
        """固定解准入与收敛阈值是方案定死的（§7.2 / §7.5）。"""
        from rtkserver.calibration import writer
        self.assertEqual(writer.FIX_QUALITY_REQUIRED, 4, '质量位 =4（RTK 固定解）')
        self.assertEqual(writer.FIX_MIN_SATS, 12, '卫星数 ≥12')
        self.assertEqual(writer.FIX_MAX_PDOP, 2.0, 'PDOP ≤2.0')
        self.assertEqual(writer.FIX_HOLD_SECONDS, 30.0, '连续 30 秒')
        self.assertEqual(writer.FIX_MAX_HDOP_FALLBACK, 1.5, '无 GSA 降级 HDOP≤1.5')
        self.assertEqual(writer.CONVERGE_SIGMA, 0.02, '三轴 σ ≤2cm')
        self.assertEqual(writer.CONVERGE_HOLD_SECONDS, 60.0, '连续 60 秒')

    def test_cors_pdop_from_gsa_only(self):
        """PDOP 只能从 GSA 取，不能从 GGA 取（§7.2）。GGA 只有 HDOP。"""
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_fix_and_collect'):]
        body = body[:body.index('    def _cors_converged')]
        self.assertIn("(gsa or {}).get('pdop')", body, 'PDOP 必须来自 GSA，不是 GGA')

    def test_cors_collect_reuses_write_chain(self):
        """采集出的 ECEF 必须复用现有写链路（写坐标→切基站→…），不另写一套。"""
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index("elif plan.method == 'cors':"):]
        # 结束锚点用下一个并列分支：cors 分支内部自己有 `if reuse / else`，
        # 拿 `            else:` 当锚点会被内层 else 提前截断（A1 复用分支加的）。
        body = body[:body.index("elif plan.method == 'external_rover':")]
        self.assertIn('self._step_write_coord(plan)', body)
        self.assertIn('self._step_switch_mode(plan)', body)
        # A1 复用分支（库里已有坐标且天线未移动）也必须走同一条写链路：
        # 它省的只是"测量"，写入/NVM/1005 校验一步都不许省。
        self.assertIn("(plan.cors or {}).get('reuse')", body,
                      'A1 复用分支必须留在 cors 分支内，复用同一条写链路')

    def test_cors_collect_drains_serial_each_loop(self):
        """采集循环每轮必须 drain 串口（阶段5 真机 bug）。

        不 drain，`channel.cache['gga']` 就停在 GGA_WAIT 阶段的残留值，
        固定解质量位/星数/PDOP 永远不更新，真机永远采不到样本。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_fix_and_collect'):]
        body = body[:body.index('    def _cors_converged')]
        drain_at = body.index('self._drain_gga(')
        read_at = body.index("gga = channel.cache.get('gga')")
        self.assertLess(drain_at, read_at,
                        '必须先 drain 串口再读 cache，否则读到的是旧 GGA')

    def test_cors_gga_wait_gives_native_gga_a_window(self):
        """GGA_WAIT 不能一见 RMC 就退化成软件生成（阶段5 真机 bug）。

        真机 rover 模式下 RMC 比 GGA 早 1~2 秒到，早退会把厘米级的原生 GGA
        降成米级的软件生成。必须有独立的原生等待窗口。
        """
        from rtkserver.calibration import writer
        self.assertGreater(writer.GGA_NATIVE_WAIT, 0,
                           '必须给原生 GGA 一个专属等待窗口')
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_gga_wait'):]
        body = body[:body.index('    def _step_cors_fix_and_collect')]
        # 软件生成的分支必须被"窗口已过"这个条件挡住
        self.assertIn('time.time() >= native_deadline', body,
                      'RMC 早到不得立即退化成软件生成')
        # 计数必须按"新的一行"来，不能按循环次数（缓存里的同一行会被数多次）
        self.assertIn('raw != last_raw', body,
                      'GGA 计数必须按原始行变化，不能按循环次数')

    def test_cors_kalman_feeds_each_sample_once(self):
        """卡尔曼每个样本只能喂一次（阶段5 真机 bug，防假收敛）。

        旧写法每轮把整个 kept 列表重喂，同一批点被观测几百次，P 被压到极小，
        真机只采 37 秒就报"连续 60 秒收敛"——物理上不可能。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_fix_and_collect'):]
        body = body[:body.index('    def _cors_converged')]
        self.assertIn('kf_fed_upto', body, '必须记录已喂到的样本下标')
        self.assertIn('if index < kf_fed_upto', body,
                      '只允许喂新增样本，禁止整批重喂')
        self.assertIn('kept_idx', body, '必须用 3σ 返回的下标做增量喂入')

    def test_cors_reuse_branch_skips_survey_but_keeps_verify(self):
        """A1 复用分支只许省"测量"，写入 / NVM / 1005 校验一步都不许省。

        标定流程 A1 的分支：库里已有该天线的坐标且天线没搬过 → 跳过 CORS 测量。
        危险写法有两种，都必须被这条测试拦住：
          ① 复用分支自己拼一套"只写坐标就收工"，绕过 SAVE/RESTART/VERIFY_RTCM
             → 用户拿到"标定完成"，但坐标到底进没进 NVM 没人验过；
          ② 复用分支照旧去连 CORS → 白占账号、白等 5 分钟，还可能因为
             现场没网直接失败，用户完全不知道为什么"复用个坐标还要联网"。
        """
        from rtkserver.calibration import writer
        # 复用时不连网：主机/挂载点必须允许为空
        plan = writer.WritePlan.build_cors(host='', port=0, mountpoint='',
                                           reuse_arp=(-2519265.0514, 4849534.9045,
                                                      3277834.6432))
        self.assertTrue(plan.cors['reuse'])
        self.assertEqual(plan.method, 'cors', '复用仍是 CORS 标定，不能降级成 manual')
        self.assertEqual(plan.mode, 'base')
        self.assertEqual(plan.svin_mode, 'fixed')
        self.assertEqual(plan.accuracy_grade, writer.ACCURACY_GRADE_CENTIMETER,
                         '复用历史坐标仍属厘米级口径')
        self.assertTrue(any('没有被移动过' in w for w in plan.warnings),
                        '必须提醒用户"复用前提是天线没动过"')

        # 复用 + 只测不写 互相矛盾：一个要写 NVM，一个一个字节都不许写
        with self.assertRaises(ValueError):
            writer.WritePlan.build_cors(host='', port=0, mountpoint='',
                                        method='external_rover',
                                        reuse_arp=(1.0, 2.0, 3.0))

        # 非复用分支仍然硬要求 CORS 地址与挂载点（不能因为加了复用就把校验放松）
        with self.assertRaises(ValueError):
            writer.WritePlan.build_cors(host='', port=8002, mountpoint='MP')

        # 结构约束：复用分支必须落在 cors 分支内部，且之后仍走统一写链路
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index("elif plan.method == 'cors':"):]
        body = body[:body.index("elif plan.method == 'external_rover':")]
        self.assertIn("(plan.cors or {}).get('reuse')", body)
        self.assertNotIn('_step_cors_collect(plan)', body.split("(plan.cors or {}).get('reuse')")[0],
                         '复用判断必须在采集之前，不能先采集再决定复用')
        # 复用分支里不许自己调 SAVE / RESTART / VERIFY（那是外层统一链路的事），
        # 否则两处实现会漂移，还原/手动等其它分支就享受不到同一份判据
        reuse_at = body.index("(plan.cors or {}).get('reuse')")
        reuse_block = body[reuse_at:]
        for banned in ('_step_save(', '_step_restart(', '_step_verify_rtcm('):
            self.assertNotIn(banned, reuse_block,
                             f'复用分支不得自己调 {banned}，必须交回统一写链路')

    def test_cors_reuse_rejected_with_measure_only(self):
        """复用坐标 + 只测不写 必须在服务层就被拒（§14 危险组合）。"""
        from rtkserver.calibration import service
        src = self._read('rtkserver', 'calibration', 'service.py')
        start = src[src.index('    def start_cors('):]
        start = start[:start.index('    def start_restore(')]
        self.assertIn('reuse', start, '服务层必须显式处理 reuse_arp')
        self.assertIn('measure_only', start)
        self.assertIn('互相矛盾', start,
                      '复用 + 只测不写必须给出明确拒绝理由，不能静默取其一')

    def test_cors_reuse_source_is_bound_to_own_module_sn(self):
        """复用坐标只能来自**本机 SN** 的历史记录（A1 分支最危险的坑）。

        页面初次加载时 `list_records(limit=3)` 拿的是全库最近几条，
        它们可能是另一块模块、另一根天线的坐标。直接拿去复用，写进 NVM 后
        1005 校验会**照常通过**（它只核对"写入值 == 播发值"），
        现场表现是"基准站一直在播别人家的位置"，而且没有任何报错。
        所以后端必须单独给一份按 SN 过滤的结果，前端只用那一份。
        """
        source = self._read('rtkserver', 'web', 'api_calibration.py')
        body = source[source.index('def api_calibration_options():'):]
        body = body[:body.index('@login_required')]
        self.assertIn('own_history', body, '必须单独返回本机 SN 的历史坐标')
        self.assertIn('module_sn=', body, '必须按 module_sn 过滤')
        self.assertIn("request.args.get('sn')", body, 'SN 必须由前端传入')

        js = self._read('static', 'calibration.js')
        self.assertIn('fillOwnHistory', js, '前端必须区分"本机记录"与"全库记录"')
        self.assertIn('own_history', js)
        # 复用来源必须取 ownHistory，不能是 history
        build = js[js.index('function buildPayload()'):]
        build = build[:build.index('function validate(')]
        self.assertIn('STATE.history', build,
                      '复用值来自 STATE.history，但 STATE.history 必须已被本机记录覆盖')
        self.assertIn('payload.reuse_arp', build, 'CORS 方式下必须发 reuse_arp')

    def test_cors_reuse_keeps_rtcm_verification(self):
        """复用分支同样要做 1005 校验（跳过的是测量，不是校验）。

        标定流程 A1 的复用分支语义是"坐标已知，只做设基准站"。
        但"坐标写进 NVM 并真的播出去"这一步与是否测量无关 —— 少了它，
        用户会以为"复用一下就好了"，实际坐标可能根本没落盘。
        """
        from rtkserver.calibration import writer
        plan = writer.WritePlan.build_cors(host='', port=0, mountpoint='',
                                           reuse_arp=(-2519265.0514, 4849534.9045,
                                                      3277834.6432))
        self.assertTrue(plan.verify_rtcm, '复用分支默认必须校验 1005')
        self.assertEqual(plan.mode, 'base')
        # 复用分支不能把 written() 变成空：落库要写这组坐标
        self.assertEqual(plan.written()['x'], round(-2519265.0514, 4))

    def test_cors_fix_wait_timeout_mentions_account_permission(self):
        """FIX_WAIT 超时排查必须包含"CORS 账号权限"这一项（流程文档要求）。

        真实现场最常见的"永远浮动解"不是天线遮挡，而是账号只给了伪距差分，
        或只允许 CGCS2000 而挂载点是 WGS84。文案少了这一条，用户会把
        整条链路拆了重装也找不到原因。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_fix_and_collect'):]
        body = body[:body.index('    def _cors_converged')]
        self.assertIn('账号', body, '超时文案必须提到 CORS 账号')
        self.assertIn('权限', body, '超时文案必须提到权限/差分等级')
        self.assertIn('坐标系', body, '超时文案必须提到坐标系匹配')

    def test_cors_fix_wait_has_timeout(self):
        """FIX_WAIT 必须有超时兜底（阶段5 真机 bug）。

        天线被遮挡时永远进不了固定解，没有超时就会无限占用串口，
        主程序数据面一直让不出去。
        """
        from rtkserver.calibration import writer
        self.assertGreater(writer.FIX_WAIT_TIMEOUT, 0, 'FIX_WAIT 必须有总超时')
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_fix_and_collect'):]
        body = body[:body.index('    def _cors_converged')]
        self.assertIn('fix_wait_started', body)
        self.assertIn("raise WriteFailed(\n                        'FIX_WAIT'", body,
                      '超时必须抛 WriteFailed(FIX_WAIT) 而不是静默继续')
        # 超时消息要带上真实观测值，否则用户不知道卡在哪一条判据
        self.assertIn("'quality': quality", body)
        self.assertIn("'num_sats': num_sats", body)

    def test_cors_converge_uses_raw_samples_not_filtered(self):
        """收敛判据必须吃原始观测样本，不能吃滤波输出。

        卡尔曼对白噪声有约 2.3 倍平滑（原始 σ=40 mm → 滤波序列 σ=17.4 mm），
        拿滤波序列判 `≤2cm` 等于把方案 §7.5 的门限悄悄放宽到原始 σ≈4.6cm。
        另外 `kf.variance` 在 q=1e-6 / r=4e-4 下稳态恒为 7.7 mm、
        与输入噪声完全无关，用它判收敛等于恒真。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _cors_converged'):]
        body = body[:body.index('    def _cors_reconnect')]
        self.assertIn('_rolling_sigma', body, '收敛 σ 必须走 _rolling_sigma')
        self.assertIn('clean_history', body, '判据必须基于 3σ 剔除后的原始样本')
        self.assertNotIn('kf_history', body,
                         '收敛判据不得使用滤波输出序列（会把门限放宽 2.3 倍）')
        self.assertNotIn('kf[0].variance', body,
                         '收敛判据不得使用 kf.variance（稳态恒真，与数据无关）')
        # 采集循环里只允许往 clean_history 塞原始样本
        loop = source[source.index('    def _step_cors_fix_and_collect'):]
        loop = loop[:loop.index('    def _cors_converged')]
        self.assertIn('clean_history.append(sample)', loop)
        self.assertIn('self._cors_converged(clean_history)', loop)

    def test_verify_rtcm_is_consistency_not_accuracy(self):
        """1005 校验只证明写入链路生效，**不能**当绝对精度的证据。

        1005 播发的是模块内部存的那份坐标，写入→重启→播发同源，
        偏差恒等于 1005 的量化误差（≤0.1 mm）。把它当精度证据是循环验证。
        源码必须把这个边界写清楚，否则报告里很容易被当成"精度 0.1 mm"。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        self.assertIn('不能证明坐标的绝对精度', source,
                      '1005 校验必须标注语义边界（不得当精度证据）')
        self.assertIn('不代表绝对精度', source,
                      '成功文案里也要带上限定，避免被误读')
        # 反面对照：模式/坐标回读那一步本来就是"持久化确认"，语义是对的
        self.assertIn('NVM 持久化确认', source)

    def test_cors_mode_guard_skips_fix_wait(self):
        """模式周期复核只能在采集阶段做，FIX_WAIT 期必须让开（真机实测）。

        2026-11-02 两跑对照：带 FIX_WAIT 期查询 → 120 秒一直停在浮动解（质量=5）；
        去掉后 → 120 秒末进入固定解（质量=4）。原因是往串口插 PQTM 查询会打断
        RTCM 注入的连续性，而 FIX_WAIT 正是模糊度搜索期，最怕这种中断。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_fix_and_collect'):]
        body = body[:body.index('    def _cors_converged')]
        self.assertIn('collect_started is not None', body,
                      '模式复核必须以「已进入采集」为前提，FIX_WAIT 期不得查询')
        self.assertIn('MODE_RECHECK_SECONDS', body)
        # 注入前那一次检查仍然保留（这是"能不能注入"的关键门禁）
        outer = source[source.index('    def _step_cors_collect'):]
        outer = outer[:outer.index('    # ---- ROVER_SETUP（§9.1）：CORS 标定的第一道门禁 ----')]
        self.assertIn("self._ensure_rover('CORS_CONNECT')", outer)

    def test_revert_restores_nvm_and_nmea(self):
        """失败回滚必须把 NVM 和 NMEA 速率一起还原（真机实测的两个坑）。

        1. 切 rover 用的是 `persist=True`，NVM 里已经是 rover。回滚若只写运行态，
           内存是 base、NVM 是 rover —— 一断电重启基准站就变流动站。
        2. CORS 链路会把 GGA/GSA 从 0 打开到 1。只还坐标和模式的话，
           base 模式下会白白多播 NMEA，占串口带宽。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _revert'):]
        body = body[:body.index('    # ---------- 步骤 1：写坐标')]
        self.assertIn("set_mode(\n                    old_mode, persist=True", body,
                      '模式回写必须写进 NVM，否则断电后模式会错')
        self.assertIn('set_message_rate(name, rate)', body,
                      '回滚必须按备份回放报文速率')
        self.assertIn('报文速率回放', body)

    def test_cors_read_mountpoints_reuses_sourcetable(self):
        """读取挂载点必须复用主程序的 SOURCETABLE 实现（任务书第 1 条）。"""
        source = self._read('rtkserver', 'calibration', 'cors.py')
        self.assertIn('def read_mountpoints(', source)
        self.assertIn('from ..sources.ntrip import fetch_sourcetable, parse_sourcetable',
                      source, '必须复用 sources/ntrip 的解析，不另写一套')

    def test_cors_gga_forwarded_per_line(self):
        """GGA 必须「模块出一条就转发一条」，不是按固定 1 秒抽一次。

        行业口径（2026-11 复核）：CORS 对 GGA 的**下限**要求通常是 1 Hz（最宽 30 秒），
        但 VRS 是拿这条位置合成虚拟观测的，NEAR 类挂载点还会按每次新 GGA 重选基准源。
        所以正确做法是逐条透传 + 保底重发，而不是「每 1 秒取缓存里最新那条」——
        后者在模块 1 Hz 输出时会因为节流抖动漏掉或重复，转发出去的位置也不是刚刚这条。
        """
        source = self._read('rtkserver', 'calibration', 'cors.py')
        self.assertIn('GGA_MIN_INTERVAL', source, '必须保留高频保护下限')
        self.assertIn('GGA_KEEPALIVE', source, '必须有「模块不出新行时保底重发」')
        # 判新用的是「原始行对比」，不是时间戳节流
        self.assertIn('if payload != self._last_forwarded:', source)
        self.assertIn('def force_source(self):', source,
                      '重连后必须能立刻强发一条（不等节奏）')
        # pump 里不能再用固定 1 秒节流
        pump = source[source.index('    def pump(self, gga_source'):]
        pump = pump[:pump.index('    def send_gga_now(')]
        self.assertNotIn('>= 1.0', pump, 'pump 不得再按 1 秒抽稀，节奏交给 GgaManager')
        self.assertIn('GGA_MIN_INTERVAL', pump)

    def test_cors_drain_before_pump(self):
        """采集循环必须「先读串口再上报」：反了就永远上报上一轮的位置。

        VRS 用这条 GGA 决定在哪合成虚拟观测，位置旧一个周期（≈0.3 s）在静态标定里
        影响不大，但如果是「先 pump 再 drain」，转发出去的始终是缓存里的旧行；
        而且固定解准入的判据也正是靠同一次 drain 刷新，两者必须同源。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_fix_and_collect('):]
        body = body[:body.index('    def _cors_converged(')]
        drain_at = body.index('self._drain_gga(GGA_DRAIN_WINDOW)')
        pump_at = body.index('client.pump(gga_manager.source)')
        self.assertLess(drain_at, pump_at, '必须先 drain 再 pump，否则上报的是旧位置')
        # 重连后要强发一条（用 force_source，而不是被节奏挡住）；
        # 这一段在 _cors_reconnect 里，不在主循环体内，所以查整个文件。
        self.assertIn('client.send_gga_now(gga_manager.force_source)', source)




class TestCalibrationAppendix(unittest.TestCase):
    """补充附录 A~J 的契约。锁的都是"错了现场看不出来"的那几条。

    这些断言的共同点：失败时不会有人报错，只会得到一组看起来正常的错坐标。
    正因为没有任何外部信号能发现问题，才必须在这里用测试钉住。
    """

    @classmethod
    def setUpClass(cls):
        cls._root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _source(self, *parts):
        path = os.path.join(self._root, *parts)
        with io.open(path, encoding='utf-8') as handle:
            return handle.read()

    # ---------- 附录A：软件合成 GGA ----------
    @staticmethod
    def _manager(gga=None, rmc=None, guard=None):
        from rtkserver.calibration.cors import GgaManager

        cache = {}
        if gga is not None:
            cache['gga'] = gga
        if rmc is not None:
            cache['rmc'] = rmc

        class _Channel:
            def __init__(self):
                self.cache = cache

        return GgaManager(_Channel(), guard=guard)

    @staticmethod
    def _gga_line(lat='3030.00000', lon='11418.00000', quality=4, height='30.000'):
        body = f'GNGGA,052803.00,{lat},N,{lon},E,{quality},12,1.1,{height},M,,M,,'
        from rtkserver.calibration.proto import nmea
        return nmea.frame(body)

    def test_software_gga_quality_is_never_4(self):
        """软件合成的 GGA 质量位只能是 1（单点），绝不能写成 4（固定解）。

        写成 4 的后果：这条报文就是喂 VRS 的，服务端会以为流动站已经厘米级固定，
        直接下发"最匹配"的差分；而实际位置是米级单点解，得到的坐标看起来
        像厘米级结果，实际差着几十米。整个系统没有一处会报错。
        """
        mgr = self._manager(gga=None, rmc={'valid': True, 'lat': 30.5, 'lon': 114.3,
                                           'height': 30.0})
        has_data, payload = mgr._current_payload()
        self.assertTrue(has_data, '只有 RMC 时必须合成出 GGA')
        text = payload.decode('ascii')
        quality = int(text.split(',')[6])
        self.assertEqual(quality, 1, f'合成 GGA 质量位必须是 1，实际 {quality}：{text}')
        self.assertNotEqual(quality, 4)

    def test_software_gga_is_guarded_too(self):
        """合成 GGA 也要过防抖：护栏看的是"位置能不能用"，与 GGA 从哪来无关。

        但防抖**不钉位置**：5 米的单点抖动必须原样透传。锁死位置会让 VRS 生成的
        虚拟参考站停在旧点上，电离层/对流层残差随真实位置偏离一起放大。
        """
        rmc = {'valid': True, 'lat': 30.5, 'lon': 114.3, 'height': 30.0}
        mgr = self._manager(gga=None, rmc=rmc)
        mgr._current_payload()                 # 第一帧落锚
        first = mgr._current_payload()[1].decode('ascii')
        rmc['lat'] = 30.50005                  # 单点解下一秒抖了约 5 米
        rmc['lon'] = 114.30005
        second = mgr._current_payload()[1].decode('ascii')
        self.assertNotEqual(first.split(',')[2], second.split(',')[2],
                            '正常漂移必须原样透传，不能把纬度钉住')
        self.assertNotEqual(first.split(',')[4], second.split(',')[4],
                            '正常漂移必须原样透传，不能把经度钉住')
        self.assertEqual(mgr.jump_dropped, 0, '5 米的漂移不该被当成跳变')

    def test_guard_never_writes_back_to_channel_cache(self):
        """防护栏只作用在上行报文的副本上，绝不回写 `channel.cache`（附录A.4 数据流隔离）。

        回写的后果：本地采样拿同一个位置采上千次，σ 变成 0、判定"完美收敛"，
        得到的是一组自己骗自己的坐标。
        """
        mgr = self._manager(gga={'valid': True, 'lat': 30.5, 'lon': 114.3,
                                 'quality': 4, 'raw': self._gga_line()})
        before = dict(mgr.channel.cache['gga'])
        mgr._current_payload()
        mgr._current_payload()
        after = mgr.channel.cache['gga']
        self.assertEqual(before, after, '防抖绝不能回写 channel.cache')

    def test_jump_over_limit_is_dropped_and_reused_once(self):
        """单条跳变 > GGA_JUMP_LIMIT：丢弃该条、复用上一条兜底，但不能连续复用到死。"""
        from rtkserver.calibration.cors import GGA_JUMP_LIMIT

        self.assertEqual(GGA_JUMP_LIMIT, 500.0)
        mgr = self._manager(gga={'valid': True, 'lat': 30.5, 'lon': 114.3,
                                 'quality': 4, 'raw': self._gga_line()})
        mgr._current_payload()
        self.assertTrue(mgr.guard_active, '第一条合法 GGA 之后护栏应已建立锚点')
        # 天线被搬动：纬度跳 0.01° ≈ 1.1 km，远超 500 m 门限
        mgr.channel.cache['gga'] = {'valid': True, 'lat': 30.51, 'lon': 114.3,
                                    'quality': 4, 'raw': self._gga_line(lat='3030.60000')}
        has, payload = mgr._current_payload()
        self.assertEqual(mgr.jump_dropped, 1, '超门限必须丢弃一条')
        self.assertGreater(mgr.last_jump_distance, GGA_JUMP_LIMIT)
        # 复用上一条 = 内容与原行相同 → source() 去重后不会重复发，这是设计如此
        # （见 GgaManager.source 的"行没变就等 keepalive"逻辑），不能当成 bug。
        self.assertIn(b'3030.00000', payload, '兜底内容必须是上一条正常 GGA')
        self.assertNotIn(b'3030.60000', payload, '跳变行绝不能上报给 CORS')

    def test_guard_stops_after_consecutive_jumps(self):
        """连续跳变达到 GGA_JUMP_MAX_DROP 必须停发，而不是无限补发旧位置。

        无限补发的后果：Caster 会一直收到一个"看起来正常但已经不真实"的位置，
        VRS 生成的改正数全部基于一个不存在的点，用户那边表现为"永远不固定"，
        而且日志里一条错误都没有。
        """
        from rtkserver.calibration.cors import GGA_JUMP_MAX_DROP, GGA_JUMP_LIMIT

        mgr = self._manager(gga={'valid': True, 'lat': 30.5, 'lon': 114.3,
                                 'quality': 4, 'raw': self._gga_line()})
        mgr._current_payload()
        for i in range(GGA_JUMP_MAX_DROP):
            lat = f'3030.{60000 + i * 100:05d}'
            mgr.channel.cache['gga'] = {'valid': True, 'lat': 30.51 + i * 0.001,
                                        'lon': 114.3, 'quality': 4,
                                        'raw': self._gga_line(lat=lat)}
            mgr._current_payload()
        self.assertEqual(mgr.jump_dropped, GGA_JUMP_MAX_DROP)
        self.assertTrue(mgr.jump_stopped, f'连续 {GGA_JUMP_MAX_DROP} 条跳变必须停发')
        has, payload = mgr._current_payload()
        self.assertFalse(has, '停发状态下不能再返回任何 GGA')
        self.assertEqual(payload, b'')

    def test_guard_stops_when_anchor_goes_stale(self):
        """长时间没有新的合法 GGA → 停发，绝不替模块编一个位置报上去。"""
        import time

        from rtkserver.calibration.cors import GGA_STALE_LIMIT

        mgr = self._manager(gga={'valid': True, 'lat': 30.5, 'lon': 114.3,
                                 'quality': 4, 'raw': self._gga_line()})
        mgr._current_payload()
        mgr._anchor_at = time.time() - (GGA_STALE_LIMIT + 5)
        has, _payload = mgr._current_payload()
        self.assertFalse(has, f'超过 {GGA_STALE_LIMIT:g}s 无新 GGA 必须停发')
        self.assertGreater(mgr.stale_stopped, 0)

    def test_guard_disabled_passes_raw_through(self):
        """`guard=False` 时必须完全透传（留给"确认模块稳定"的诊断场景）。"""
        mgr = self._manager(gga={'valid': True, 'lat': 30.5, 'lon': 114.3,
                                 'quality': 4, 'raw': self._gga_line()}, guard=False)
        mgr._current_payload()
        self.assertFalse(mgr.guard_active, 'guard=False 时不该建立锚点')
        mgr.channel.cache['gga'] = {'valid': True, 'lat': 31.5, 'lon': 114.3,
                                    'quality': 4, 'raw': self._gga_line(lat='3130.00000')}
        has, payload = mgr._current_payload()
        self.assertTrue(has)
        self.assertEqual(mgr.jump_dropped, 0, 'guard=False 时不做任何过滤')
        self.assertIn(b'3130.00000', payload)

    def test_gga_freeze_is_gone(self):
        """回归护栏：冻结逻辑（把位置钉死在第一条 GGA 上）必须彻底消失。

        为什么留这条测试：冻结是"改上报内容"，防抖是"过滤上报内容"，两者很容易在
        后续重构里被混回一起。VRS 靠上行 GGA 里的概略位置定位虚拟参考站，一旦位置
        被钉住，虚拟参考站就停在旧点上，电离层/对流层残差随真实位置偏离一起放大，
        表现是"怎么都固定不了"——而且没有任何报错。
        """
        source = self._source('rtkserver', 'calibration', 'cors.py')
        for gone in ('GGA_FREEZE_POSITION', 'GGA_FREEZE_RADIUS', 'GGA_FREEZE_HEIGHT',
                     'GGA_FREEZE_MIN_QUALITY', '_apply_freeze'):
            self.assertNotIn(gone, source, f'冻结残留：{gone}')
        self.assertIn('GGA_JUMP_LIMIT', source, '防抖门限必须存在')

    def test_generated_gga_never_injected_to_serial(self):
        """合成 GGA 只用于上行 CORS，绝不写进模块串口。"""
        source = self._source('rtkserver', 'calibration', 'cors.py')
        body = source[source.index('    def _build_software_gga(self):'):]
        body = body[:body.index('    def reset_rhythm(self):')]
        for forbidden in ('self.channel.write', 'self.channel.send', 'serial.write'):
            self.assertNotIn(forbidden, body,
                             f'合成 GGA 段落里出现了 {forbidden}，会把报文灌回模块')

    # ---------- 坐标链路：不做天线偏移 ----------
    def test_no_antenna_offset_in_geo(self):
        """geo 模块不该再提供 APC→ARP 换算。

        本产品不做 RTK 测量工程那套相位中心/参考点换算：天线装好就是那个点，
        测出来直接写，多一层偏移只会引入填错方向的风险。
        """
        from rtkserver.calibration import geo
        self.assertFalse(hasattr(geo, 'apc_to_arp'), 'geo 不该再有 apc_to_arp')
        self.assertFalse(hasattr(geo, 'arp_to_apc'), 'geo 不该再有 arp_to_apc')

    def test_record_keeps_apc_and_arp_separately(self):
        """记录里 apc_* 与 arp_* 各存一份，且 ecef_* 表示写入值。"""
        from rtkserver.calibration.geo import geodetic_to_ecef
        from rtkserver.calibration.writer import CoordinateWriter, WritePlan

        given = geodetic_to_ecef(30.5, 114.3, 50.0)
        plan = WritePlan.build(ecef=given)
        writer = CoordinateWriter(driver=None, channel=None)
        record = writer.build_record(plan)
        self.assertAlmostEqual(record['apc_z'], round(given[2], 4), places=4)
        self.assertAlmostEqual(record['arp_z'], record['apc_z'], places=6,
                               msg='无偏移时 ARP 必须等于 APC')
        self.assertAlmostEqual(record['ecef_z'], record['arp_z'], places=6,
                               msg='ecef_* 必须等于写入值')

    def test_history_reuse_writes_value_as_is(self):
        """复用历史坐标走 arp 直通，原样写入。"""
        from rtkserver.calibration.geo import geodetic_to_ecef
        from rtkserver.calibration.writer import WritePlan

        arp = geodetic_to_ecef(30.5, 114.3, 50.0)
        plan = WritePlan.build(arp=arp)
        for got, want in zip(plan.arp, arp):
            self.assertAlmostEqual(got, want, places=9,
                                   msg='历史坐标被改动了')
        self.assertIsNone(plan.apc, '复用历史坐标时不该假装知道原值')

    # ---------- 附录C / D：只支持基准站 & 仅内存切换 ----------
    def test_base_driver_refuses_memory_mode_switch_by_default(self):
        """基类默认不支持仅内存切换：做不到就必须明确失败，不能偷偷写 NVM。"""
        from rtkserver.calibration.drivers.base import BaseDriver

        class _Dummy(BaseDriver):
            MODEL_KEY = 'dummy'

        ok, message, data = _Dummy(None).set_mode_memory_only('rover')
        self.assertFalse(ok)
        self.assertEqual(data.get('error'), 'unsupported_mem_mode')
        self.assertIn('仅内存', message)

    def test_base_only_module_is_blocked_before_switching(self):
        """纯基准站型号选 CORS 标定要在第一步就拦下，并指向正确操作（附录C.2）。"""
        source = self._source('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('    def _step_cors_rover_setup(self, plan):'):]
        body = body[:body.index('    def _ensure_rover(self, stage):')]
        self.assertIn("not self._caps('dual_mode')", body)
        self.assertIn('该模块仅基准站模式', body)
        self.assertIn('只写已知坐标', body)
        # 拦下之后不能再去切模式
        guard = body[:body.index("self._caps('dual_mode')")]
        self.assertNotIn("set_mode('rover'", guard)

    # ---------- 附录G：CORS 抖动宽容 ----------
    def test_cors_jitter_tolerance(self):
        """2 秒内的断连不清空连续固定解计时器，超过就重置。"""
        from rtkserver.calibration import writer as W

        self.assertEqual(W.CORS_NET_JITTER_TOLERANCE_SEC, 2.0)
        source = self._source('rtkserver', 'calibration', 'writer.py')
        body = source[source.index('                ok, outage = self._cors_reconnect('):]
        body = body[:body.index('            gga = channel.cache.get')]
        self.assertIn('outage <= CORS_NET_JITTER_TOLERANCE_SEC', body)
        # 宽容分支里不许清计时器；重置只能出现在 else 里
        tolerant = body[:body.index('else:')]
        self.assertNotIn('fix_hold_started = None', tolerant,
                         '抖动恢复不该清空连续固定解计时器')
        self.assertIn('fix_hold_started = None', body,
                      '长断连必须重置计时器')

    def test_settle_disconnect_returns_duration(self):
        """`settle_disconnect()` 必须回报本次断连时长，抖动判定全靠它。"""
        from rtkserver.calibration.cors import CorsClient

        client = CorsClient('127.0.0.1', 2101, 'MP')
        self.assertEqual(client.settle_disconnect(), 0.0, '没断过应返回 0')
        client._disconnected_at = __import__('time').time() - 1.5
        seconds = client.settle_disconnect()
        self.assertTrue(1.4 < seconds < 1.7, f'应回报约 1.5 秒，实际 {seconds}')

    # ---------- 附录B / I：Survey-in 与落库字段 ----------
    def test_record_has_appendix_i_fields(self):
        """补充附录I 的字段必须真的能落库（含 CORS 模式下的 NULL 语义）。"""
        from rtkserver.calibration import store

        for field in ('arp_x', 'arp_y', 'arp_z', 'gga_guard_used', 'gga_jump_dropped',
                      'raw_sample_std_e', 'raw_sample_std_n', 'raw_sample_std_u',
                      'kalman_std_e', 'kalman_std_n', 'kalman_std_u',
                      'svin_valid', 'svin_obs_count', 'svin_mean_acc',
                      'cors_mountpoint_used'):
            self.assertIn(field, store.RECORD_FIELDS, f'缺少字段 {field}')
        self.assertGreaterEqual(store.SCHEMA_VERSION, 4)

        row = store._normalize_record({'method': 'cors', 'svin_obs_count': None})
        self.assertIsNone(row['svin_obs_count'],
                          'CORS 模式没跑 Survey-in，观测数必须留 NULL 而不是 0')
        self.assertEqual(row['gga_guard_used'], 0)
        self.assertEqual(row['gga_jump_dropped'], 0)

    def test_freeze_column_no_longer_written(self):
        """`gga_freeze_used` 只保留给历史记录读，新代码绝不能再往里写。

        冻结=1 与"护栏=1"语义完全相反（一个是改上报位置，一个是原样透传 + 过滤），
        混在同一列里事后无法区分，等于把审计线索毁了。
        """
        from rtkserver.calibration import store

        self.assertNotIn('gga_freeze_used', store.RECORD_FIELDS)
        self.assertNotIn('gga_freeze_used', store._INT_FIELDS)
        writer = self._source('rtkserver', 'calibration', 'writer.py')
        self.assertNotIn("'gga_freeze_used'", writer,
                         'writer 里还残留 gga_freeze_used 的写入')

    def test_survey_in_writes_svin_fields(self):
        """Survey-in 记录要带 svin_valid / obs / mean_acc，否则事后无法区分来源。"""
        from rtkserver.calibration.writer import CoordinateWriter, WritePlan

        plan = WritePlan.build_survey_in(profile='fast')
        writer = CoordinateWriter(driver=None, channel=None)
        writer.result['survey'] = {'obs': 600, 'mean_acc': 0.42, 'mean_x': 1.0,
                                   'mean_y': 2.0, 'mean_z': 3.0}
        record = writer.build_record(plan)
        self.assertEqual(record['svin_valid'], 2, '已收敛应记 Valid=2')
        self.assertEqual(record['svin_obs_count'], 600)
        self.assertAlmostEqual(record['svin_mean_acc'], 0.42)

    # ---------- 附录H：文案红线 ----------
    def test_no_absolute_accuracy_wording(self):
        """1005 校验只能说"链路一致"，不许说"坐标验证正确"（附录H.1）。

        只查**代码行里的字符串**，不查注释：注释里出现这几个字往往正是
        "为什么不能这么说"的说明（本仓库就有这么一条），误判成违规没有意义。
        """
        banned = ('标定坐标验证正确', '坐标验证正确', '坐标已通过验证')
        for name in ('service.py', 'writer.py'):
            text = self._source('rtkserver', 'calibration', name)
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith('#'):
                    continue
                for phrase in banned:
                    self.assertNotIn(phrase, line,
                                     f'{name}:{lineno} 出现了被禁止的绝对精度措辞：{phrase}')
        service = self._source('rtkserver', 'calibration', 'service.py')
        self.assertIn('不能代表天线绝对地理精度', service,
                      '成功文案必须声明 1005 不代表绝对精度')

    def test_port_coordinate_system_warning_present(self):
        """CORS 端口坐标系风险必须常驻可见（附录H.2）。"""
        page = self._source('templates', 'calibration.html')
        self.assertIn('cal-cors-port', page)
        self.assertIn('8001', page)
        self.assertIn('8002', page)
        i18n = self._source('static', 'i18n.js')
        self.assertIn('cal.cors.port_hint', i18n)
        self.assertIn('CGCS2000', i18n)


class TestCalibrationStage7(unittest.TestCase):
    """阶段7：异常兜底（方案 §14）、启动钩子、升级兼容（§12.1.1）、§15.1 契约。

    这里锁的都是"错了不会有人报错"的那一类：
        · 热路径被掺进 CORS 数据 → 下游坐标整体漂走，日志全绿；
        · 保存失败还重启 → 模块丢掉刚写的配置，现场表现像"模块自己变回去了"；
        · 进程被杀后没有启动钩子 → 模块停在中间态，用户直接拿它当基准站；
        · 升级把 calibration.db 覆盖/漏备份 → 站点坐标没了，只能回现场重测。
    """

    @staticmethod
    def _read(*parts):
        with io.open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as handle:
            return handle.read()

    # ==================== §15.1 契约 ====================
    def test_hot_path_functions_untouched(self):
        """distribute / upload_via_serial / process_rtcm_buffer 不得掺入标定逻辑。

        "与基线一致"没法直接断言（基线本身会随合法改动变化），所以锁三件可判定的事：
        签名不变、体内不出现 calibration、标定包不 import 它们。
        签名一改，标定代码就能顺手把 CORS 字节塞进数据面 —— 那是致命红线。
        """
        import inspect

        from rtkserver.core.distributor import distribute
        from rtkserver.sources.rtcm_parser import process_rtcm_buffer
        from rtkserver.sources.serial_source import upload_via_serial

        self.assertEqual(list(inspect.signature(distribute).parameters), ['data'])
        self.assertEqual(list(inspect.signature(upload_via_serial).parameters),
                         ['ser', 'cfg', 'thread_exit_event'])
        self.assertEqual(list(inspect.signature(process_rtcm_buffer).parameters), [])
        for fn in (distribute, upload_via_serial, process_rtcm_buffer):
            body = inspect.getsource(fn).lower()
            self.assertNotIn('calibration', body,
                             f'{fn.__name__} 被掺进了标定逻辑（热路径必须零改动）')

    def test_cors_bytes_never_reach_distributor(self):
        """CORS 收到的 RTCM 只能注入模块串口，绝不允许进分发链路（高危点 4）。

        进了 distribute 就等于把"别人基准站"的差分当成自己的数据源播给下游：
        位置会整体漂走，而且没有任何报错——比不转发严重得多。

        这里按 **AST 调用**判定，不按字符串：文档里写"绝不进全局 distribute（§6.4）"
        是必须保留的说明，按字符串判会把注释也判成违规。
        """
        for name in ('writer.py', 'cors.py', 'service.py'):
            source = self._read('rtkserver', 'calibration', name)
            tree = ast.parse(source)
            banned_calls = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = getattr(func, 'attr', None) or getattr(func, 'id', '')
                if called in ('distribute', 'upload_via_serial'):
                    banned_calls.append(f'{called}()')
                # state.rtcm_queue.put(...) 等价于进分发，也要拦
                if called == 'put':
                    base = getattr(func, 'value', None)
                    if getattr(base, 'attr', '') == 'rtcm_queue':
                        banned_calls.append('rtcm_queue.put()')
            self.assertEqual(banned_calls, [],
                             f'{name} 把 CORS 数据送进了分发链路：{banned_calls}')

    def test_lease_release_reachable_on_failure_path(self):
        """行为级：工作线程在任意失败路径都必须归还租约（高危点 5）。

        静态看 `finally` 只能证明"写了这个关键字"，这里真的让 acquire 抛异常，
        确认 release 被调用且只调用一次。
        """
        import shutil
        import tempfile

        from rtkserver.calibration import service as calib
        from rtkserver.calibration import store, workspace

        tmp = tempfile.mkdtemp(prefix='2rtk-release-')
        old_dir, old_db = workspace.STATE_DIR, store.CALIBRATION_DB
        old_lease = calib.SerialLease
        released = []

        class _BoomLease:
            """租约在"申请"阶段就炸：模拟串口被拔/数据面不让位。"""

            def __init__(self, port=None, baud=None, task_id=''):
                self.stage = 'IDLE'
                self.port = port
                self.channel = None
                self.task_id = task_id
                self.is_held = False

            def snapshot(self):
                return {}

            def acquire(self):
                raise OSError('could not open port /dev/ttyUSB0：设备已消失')

            def open(self):
                return True, ''

            def beat(self, force=False):
                pass

            def release(self, stage='DONE', message=''):
                released.append((stage, message))

        workspace.STATE_DIR = tmp
        store.CALIBRATION_DB = os.path.join(tmp, 'calibration.db')
        calib.SerialLease = _BoomLease
        try:
            svc = calib.CalibrationService()
            svc._task = {'port': 'FAKE0', 'baud': 115200, 'action': 'write'}
            svc._task_id = 'T-REL'
            svc._worker()
        finally:
            calib.SerialLease = old_lease
            workspace.STATE_DIR = old_dir
            store.CALIBRATION_DB = old_db
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(len(released), 1, '失败路径必须归还租约，且只归还一次')
        self.assertEqual(released[0][0], 'FAILED')
        self.assertIn('设备已消失', released[0][1])

    # ==================== 启动钩子（§14 #20 / #30） ====================
    def test_startup_hook_marks_interrupted(self):
        """非终态租约 → 标记 INTERRUPTED、租约落终态、页面状态可读、事件入库。"""
        import shutil
        import tempfile

        from rtkserver.calibration import service as calib
        from rtkserver.calibration import store, workspace

        tmp = tempfile.mkdtemp(prefix='2rtk-startup-')
        old_dir, old_db = workspace.STATE_DIR, store.CALIBRATION_DB
        old_service = calib._SERVICE
        old_checked, old_notice = calib._STARTUP['checked'], calib._STARTUP['notice']
        workspace.STATE_DIR = tmp
        store.CALIBRATION_DB = os.path.join(tmp, 'calibration.db')
        calib._SERVICE = None
        calib._STARTUP['checked'] = False
        calib._STARTUP['notice'] = None
        try:
            workspace.write_json(workspace.lease_path(), {
                'task_id': 'T-INT', 'port': 'COM9', 'baud': 115200, 'stage': 'SAVE',
                'held': True, 'pid': 999999, 'started_at': '2026-11-02 10:00:00',
                'module': {'model': 'Quectel LG290P', 'sn': 'SN123'},
            })
            notice = calib.on_startup()
            self.assertIsNotNone(notice, '非终态 + 持有者已死必须判为"上次被中断"')
            self.assertEqual(notice['stage'], 'SAVE')
            self.assertEqual(notice['port'], 'COM9')
            self.assertEqual(notice['sn'], 'SN123')

            lease = workspace.read_json(workspace.lease_path())
            self.assertEqual(lease['stage'], 'INTERRUPTED', '租约必须落终态，否则每次开机都报')
            self.assertFalse(lease['held'])
            self.assertEqual(lease.get('interrupted_stage'), 'SAVE')

            snap = calib.status()
            self.assertEqual(snap['stage'], 'INTERRUPTED')
            self.assertTrue(snap.get('interrupted'), '页面要能拿到横幅数据')
            self.assertEqual(snap['interrupted']['stage'], 'SAVE')

            events = store.list_events('T-INT', limit=10)
            self.assertTrue(any(e.get('stage') == 'INTERRUPTED' for e in events),
                            '中断事件要进 calibration_events，事后才能查')

            # 幂等：同一进程再调一次不重复报（否则横幅天天弹，用户学会无视）
            self.assertIs(calib.on_startup(), notice)
        finally:
            calib._SERVICE = old_service
            calib._STARTUP['checked'] = old_checked
            calib._STARTUP['notice'] = old_notice
            workspace.STATE_DIR = old_dir
            store.CALIBRATION_DB = old_db
            shutil.rmtree(tmp, ignore_errors=True)

    def test_startup_hook_runs_before_dataplane(self):
        """钩子必须挂在"Web 起来之前、run_once 之前"，且**不改硬件**（§14 #20）。

        - 挂在 `api_calibration.register()` 里：core 层不能依赖标定包；
        - 位置在 `_start_web_thread()` 之后、`run_once()` 之前：进程刚起来那一刻；
        - 钩子体内不许出现任何驱动/串口动作：此刻串口还没打开，盲操作会写坏模块。
        """
        import inspect

        from rtkserver.calibration import service as calib

        api = self._read('rtkserver', 'web', 'api_calibration.py')
        register = api[api.index('def register(app):'):]
        self.assertIn('calib.on_startup()', register, '启动钩子必须真的被调用')

        app_src = self._read('rtkserver', 'web', 'app.py')
        self.assertIn('api_calibration.register(app)', app_src)

        life = self._read('rtkserver', 'core', 'lifecycle.py')
        main_body = life[life.index('def main('):]
        self.assertLess(main_body.index('_start_web_thread()'),
                        main_body.index('run_once()'),
                        '钩子必须早于数据面启动，否则读到的状态已经被覆盖')

        hook = inspect.getsource(calib.on_startup)
        for banned in ('set_mode', 'driver.reboot', 'driver.save', 'SAVEPAR,', 'channel.open'):
            self.assertNotIn(banned, hook, f'on_startup 里出现了 {banned}：钩子绝不允许动硬件')

    def test_startup_hook_skips_database_creation_when_absent(self):
        """没有标定库、也没有残留租约时，启动钩子不该凭空造一个空库。"""
        import inspect

        from rtkserver.calibration import service as calib

        hook = inspect.getsource(calib.on_startup)
        self.assertIn('if lease or os.path.exists(store.CALIBRATION_DB)', hook)

    # ==================== 异常矩阵补充（§14） ====================
    def test_link_silence_fails_fast(self):
        """拔线/断电必须在 3 秒级判死，不能等超时（§14 #1 / #28）。"""
        import time as _time

        from rtkserver.calibration import writer as W
        from rtkserver.calibration.drivers.base import Channel

        self.assertEqual(W.LINK_SILENCE_LIMIT, 3.0)
        channel = Channel(port='FAKE')
        self.assertFalse(channel.link_lost(3.0), '从未收到数据不算断线（可能本来就不输出）')
        channel.last_rx_at = _time.time()
        self.assertFalse(channel.link_lost(3.0))
        channel.last_rx_at = _time.time() - 5.0
        self.assertTrue(channel.link_lost(3.0), '收到过再静默 5 秒必须判死')
        channel.last_rx_at = _time.time()
        channel.link_error = 'SerialException: device unplugged'
        self.assertTrue(channel.link_lost(3.0), 'read 抛异常要立刻判死')

        source = self._read('rtkserver', 'calibration', 'writer.py')
        collect = source[source.index('    def _step_cors_fix_and_collect('):]
        collect = collect[:collect.index('    def _cors_converged(')]
        self.assertIn('self._assert_link_alive(', collect, '采集循环必须每轮检查链路')
        svin = source[source.index('    def _step_svin_wait(self, plan):'):]
        svin = svin[:svin.index('    def _step_svin_resume(') if
                   '    def _step_svin_resume(' in svin else len(svin)]
        self.assertIn('self._assert_link_alive(', svin,
                      'Survey-in 最长等两小时，链路断了必须立刻失败')

    def test_baud_scan_only_when_user_does_not_pin_it(self):
        """波特率枚举只在用户没显式指定时启用（§14 #3 / #25）。"""
        import inspect

        from rtkserver.calibration import service as calib

        self.assertGreaterEqual(len(calib.DETECT_BAUD_CANDIDATES), 4)
        worker = inspect.getsource(calib.CalibrationService._worker)
        self.assertIn("allow_baud_scan=not task.get('baud_explicit')", worker)
        scan = inspect.getsource(calib.CalibrationService._scan_bauds)
        self.assertIn('channel.baud = int(original)', scan,
                      '枚举失败必须把波特率恢复原值再交回通道')
        self.assertIn('lease.baud = int(baud)', scan,
                      '换上正确波特率后要同步到租约，否则重启模块会用错档位')

    def test_detect_failure_explains_the_real_cause(self):
        """识别失败的三种病因要分别给动作（§14 #2 / #25）。"""
        from rtkserver.calibration import service as calib

        class _Ch:
            rx_bytes = 128
            counters = {'noise': 0}
            baud = 115200

        text = calib.CalibrationService._detect_failure_text(_Ch(), '无应答', [])
        self.assertIn('流控', text, '有字节但无应答要指向 RTS/CTS 与 TX/RX')
        _Ch.rx_bytes, _Ch.counters = 0, {'noise': 900}
        self.assertIn('波特率', calib.CalibrationService._detect_failure_text(_Ch(), '无应答', []))
        _Ch.counters = {'noise': 0}
        self.assertIn('供电', calib.CalibrationService._detect_failure_text(_Ch(), '无应答', []))

    def test_model_mismatch_reports_detected_model(self):
        """选了错误型号要报出"实际检测到什么"（§14 #4）。"""
        import inspect

        from rtkserver.calibration import service as calib

        detect = inspect.getsource(calib.CalibrationService._detect_module)
        self.assertIn('实际检测到', detect)
        self.assertIn('detected_module', detect, '页面要能拿到检测结果，便于一键改选')

    def test_save_failure_never_reboots(self):
        """SAVEPAR 失败绝不重启模块（高危点 3 / §14 #16）。

        带着"没保存的配置"重启，等于让模块把内存态丢掉、回到旧 NVM：
        现场看到的是"刚标完，断电一次坐标就变回去了"，而且没有任何日志线索。
        """
        source = self._read('rtkserver', 'calibration', 'writer.py')
        # 签名带 `attempt`（1005 重下发轮次标记），按前缀切，别写死完整签名
        body = source[source.index('    def _step_save(self, plan'):]
        body = body[:body.index('    def _step_restart(self, plan')]
        self.assertIn('raise WriteFailed', body)
        for banned in ('_step_restart(', 'driver.reboot(', 'self._reboot'):
            self.assertNotIn(banned, body, f'保存步骤里出现了 {banned}：保存失败绝不能重启')

    # ==================== 升级 / 卸载 / 重置（§12.1.1） ====================
    def test_upgrade_keeps_and_backs_up_calibration_db(self):
        """升级：不进覆盖名单、备份带上 -wal/-shm、失败可还原。"""
        import shutil
        import tempfile

        from rtkserver.upgrade.runner import KEEP_NAMES, Runner

        self.assertIn('calibration.db', KEEP_NAMES, '升级绝不能覆盖标定库')
        self.assertIn('.calibration', KEEP_NAMES)

        tmp = tempfile.mkdtemp(prefix='2rtk-upgrade-')
        install = os.path.join(tmp, 'install')
        os.makedirs(install)

        class _Dummy:
            install_dir = install

            def log(self, *args, **kwargs):
                pass

        try:
            with open(os.path.join(install, 'calibration.db'), 'wb') as handle:
                handle.write(b'main-db')
            with open(os.path.join(install, 'calibration.db-wal'), 'wb') as handle:
                handle.write(b'wal-not-checkpointed')

            dst = os.path.join(tmp, 'calibration-20260101-0000.db')
            self.assertEqual(Runner._copy_calibration_db(_Dummy(), dst), dst)
            self.assertTrue(os.path.exists(dst))
            self.assertTrue(os.path.exists(dst + '-wal'),
                            '-wal 必须一起备份，否则最近一次标定记录会丢')

            # 回滚：主库被删掉也要能还原回来
            os.remove(os.path.join(install, 'calibration.db'))
            self.assertTrue(Runner._restore_calibration_db(_Dummy(), dst))
            self.assertTrue(os.path.exists(os.path.join(install, 'calibration.db')))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_uninstall_backs_up_calibration_db(self):
        """卸载：标定库要备份到 /root 并列出条数，且默认保留。"""
        source = self._read('rtkserver', 'upgrade', 'runner.py')
        self.assertIn('2RTKserver-calibration-', source)
        self.assertIn('calibration_backup', source)
        self.assertIn('_calibration_db_rows()', source)
        self.assertIn('已备份到', source)

    def test_reset_config_keeps_calibration_by_default(self):
        """reset_config 默认不动 calibration.db；清库必须显式加参数 + 输入 DELETE。"""
        import shutil
        import tempfile

        import reset_config

        source = self._read('reset_config.py')
        self.assertIn('--purge-calibration', source)
        self.assertEqual(reset_config.PURGE_WORD, 'DELETE')
        self.assertIn('if args.purge_calibration:', source,
                      '清理必须被显式参数包住，不能无条件执行')
        self.assertIn('标定记录（calibration.db）未改动', source)

        tmp = tempfile.mkdtemp(prefix='2rtk-reset-')
        try:
            for name in ('calibration.db', 'calibration.db-wal'):
                with open(os.path.join(tmp, name), 'wb') as handle:
                    handle.write(b'x')
            # 这段会往 stdout 打"危险操作"清单，测试里静音掉，别把结果刷满
            import contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reset_config.purge_calibration(tmp, assume_yes=True), 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, 'calibration.db')))
            self.assertFalse(os.path.exists(os.path.join(tmp, 'calibration.db-wal')))
            # 没有库时也不能报错（脚本要能在任何环境跑）
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reset_config.purge_calibration(tmp, assume_yes=True), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reset_config_lists_what_it_would_delete(self):
        """清理前列清单：文件名 + 字节数，附带"会丢什么"的后果说明。"""
        import reset_config

        source = self._read('reset_config.py')
        self.assertIn('不可恢复', source)
        self.assertIn('历史标定记录全部丢失', source)
        self.assertIn('还原标定前配置', source)
        self.assertIn("CALIBRATION_DIR", source)
        names = [os.path.basename(p) for p in reset_config._calibration_files('/tmp/x')]
        self.assertIn('calibration.db', names)
        self.assertIn('lease.json', names)

    def test_unconverged_policy_is_explicit(self):
        """未收敛策略必须显式可选，且默认仍是"采用并标注"（§14 #14）。

        这一条锁两种误用：
            · 把 `fail` 当默认 → 现场信号稍差就什么都拿不到；
            · 把 `adopt` 做成"静默成功" → 用户以为拿到的是一组厘米级坐标。
        """
        from rtkserver.calibration import writer as W

        plan = W.WritePlan.build_cors(host='cors.example.com', port=8002,
                                      mountpoint='RTCM32', duration=300)
        self.assertEqual(plan.on_slow, 'adopt', '默认必须采用，不能一上来就判失败')
        plan = W.WritePlan.build_cors(host='cors.example.com', port=8002,
                                      mountpoint='RTCM32', duration=300, on_slow='fail')
        self.assertEqual(plan.on_slow, 'fail')
        self.assertEqual(plan.summary()['on_slow'], 'fail', '结论里要能看出这次的选择')
        with self.assertRaises(ValueError):
            W.WritePlan.build_cors(host='cors.example.com', port=8002,
                                   mountpoint='RTCM32', on_slow='whatever')

        source = self._read('rtkserver', 'calibration', 'writer.py')
        body = source[source.index("        if not converged:"):]
        body = body[:body.index('    @staticmethod\n    def _rolling_sigma')]
        self.assertIn("getattr(plan, 'on_slow'", body)
        self.assertIn('本次没有写入任何坐标', body)

    # ==================== §14 #6 回读不一致 ====================
    @staticmethod
    def _stub_driver(replies):
        """造一个只回放预设应答的驱动，用来做"回读不一致"的行为级测试。

        用真驱动 + 假串口，而不是 mock 掉 `apply_and_verify`：要验的正是
        "驱动到底调了几次配置命令"，把被测对象 mock 掉就什么都没验到。
        """
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        class _StubChannel:
            def __init__(self):
                self.replies = list(replies)
                self.sent = []
                self.cache = {}
                self.counters = {'lines': 0, 'frames': 0, 'noise': 0}
                self.parser = None

            def send(self, body, timeout=None, retries=None, interval=None):
                self.sent.append(body)
                if not self.replies:
                    return 'timeout', ''
                status, line = self.replies.pop(0)
                self.counters['lines'] += 1
                return status, line

            def count(self, body):
                return self.sent.count(body)

        channel = _StubChannel()
        return LG290PDriver(channel), channel

    def test_verify_mismatch_retries_exactly_once(self):
        """回读不一致要**重发一次**再回读，对上就算成功（§14 #6）。

        为什么必须重试而不是直接失败：模块刚重启完参数还没加载、串口里残留着
        上一条应答，这两种情况都会让第一次回读读到旧值。不重试的话，现场看到的是
        "偶尔标定失败一次，重来就好"——没人会当 bug 报，只会当成玄学。
        """
        from rtkserver.calibration.drivers.base import VERIFY_RETRY

        self.assertEqual(VERIFY_RETRY, 1, '重试次数必须是 1：多了只是把失败拖长')

        applied = ('ok', '$PQTMCFGRCVRMODE,OK')
        stale = ('ok', '$PQTMCFGRCVRMODE,OK,1')     # 回读还是 rover
        base = ('ok', '$PQTMCFGRCVRMODE,OK,2')
        saved = ('ok', '$PQTMSAVEPAR,OK')

        driver, channel = self._stub_driver([applied, saved, stale,
                                             applied, saved, base])
        ok, message, data = driver.set_mode('base', persist=True,
                                            restart=False, verify=True)
        self.assertTrue(ok, message)
        self.assertEqual(data['verify_attempts'], 2, '要记下"回读了几次才对上"')
        self.assertEqual(data['mode'], 'base')
        self.assertEqual(channel.count('PQTMCFGRCVRMODE,W,2'), 2,
                         '配置命令必须恰好重发一次')

    def test_verify_mismatch_gives_up_after_one_retry(self):
        """两次都不符才判失败，且必须说清楚"重发过还是不对"（§14 #6）。

        不写这句话，用户看到的就是和第一次一模一样的报错，会以为重试没发生过、
        反复点"开始标定"；实际上模块是真的没接受这条配置，该去查固件/权限了。
        """
        applied = ('ok', '$PQTMCFGRCVRMODE,OK')
        stale = ('ok', '$PQTMCFGRCVRMODE,OK,1')
        saved = ('ok', '$PQTMSAVEPAR,OK')

        driver, channel = self._stub_driver([applied, saved, stale,
                                             applied, saved, stale])
        ok, message, data = driver.set_mode('base', persist=True,
                                            restart=False, verify=True)
        self.assertFalse(ok)
        self.assertIn('仍然不符', message, '失败文案要说明"已经重发并再次回读过"')
        self.assertTrue(data.get('retried'), '结论里要能看出这次重试过')
        self.assertEqual(data['verify_attempts'], 2)
        self.assertEqual(channel.count('PQTMCFGRCVRMODE,W,2'), 2,
                         '只能重试一次，不能变成死循环重发')

    def test_verify_success_without_retry_counts_one_attempt(self):
        """一次就对上的正常路径也要带 `verify_attempts=1`，否则统计口径不齐。"""
        applied = ('ok', '$PQTMCFGRCVRMODE,OK')
        base = ('ok', '$PQTMCFGRCVRMODE,OK,2')
        saved = ('ok', '$PQTMSAVEPAR,OK')

        driver, channel = self._stub_driver([applied, saved, base])
        ok, _message, data = driver.set_mode('base', persist=True,
                                            restart=False, verify=True)
        self.assertTrue(ok)
        self.assertEqual(data['verify_attempts'], 1)
        self.assertEqual(channel.count('PQTMCFGRCVRMODE,W,2'), 1)

    def test_every_readback_setter_goes_through_the_retry_helper(self):
        """三条带回读的配置（模式/固定坐标/Survey-in）都要走同一套重试。

        各写一遍循环，必然有一处忘了重试，而漏掉的那处平时完全看不出来。
        """
        import inspect

        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        source = inspect.getsource(LG290PDriver)
        self.assertEqual(source.count('self.apply_and_verify('), 3,
                         '模式/固定坐标/Survey-in 三处都必须走重试助手')
        for name in ('set_mode', 'set_fixed_ecef', 'config_survey_in'):
            body = inspect.getsource(getattr(LG290PDriver, name))
            self.assertIn('self.apply_and_verify(', body, f'{name} 漏了回读重试')

        reapply = inspect.getsource(LG290PDriver._reapply)
        for banned in ('self.reboot(', "request('reboot'"):
            self.assertNotIn(banned, reapply,
                             '重试阶段不再重启：重启是为了让 NVM 生效，'
                             '而这里要修的是 RAM 里的值没读回来')

    def _svin_writer_case(self, always_stale=False):
        """真流程 + 假模块：跑 `_svin_config_verified`，返回 (异常, 调用次数, writer)。

        用假模块而不是 mock 掉 `config_survey_in`：要验的是"到底重发了几次配置"，
        把被测的东西 mock 掉就什么都没验到。
        """
        import serial as serial_module

        from rtkserver.calibration import writer as W
        from rtkserver.calibration.drivers.base import Channel
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        module = W._FakeModule()
        old_serial = serial_module.Serial
        serial_module.Serial = W._fake_serial_class(module)
        channel = Channel(port='FAKE0', baud=115200)
        channel.open()
        writer = None
        calls = []
        try:
            driver = LG290PDriver(channel)
            real_config = driver.config_survey_in
            real_read = driver.get_svin_config

            def counting_config(count, acc, distance=None, **kwargs):
                calls.append(int(count))
                return real_config(count, acc, distance, **kwargs)

            reads = {'n': 0}

            def stale_first():
                reads['n'] += 1
                if always_stale or reads['n'] == 1:
                    # 模拟"模块还在用旧值"：回读读到的是停用状态
                    return True, 'stale', {'mode': 'disabled', 'mode_code': 0, 'count': 0}
                return real_read()

            driver.config_survey_in = counting_config
            driver.get_svin_config = stale_first
            writer = W.CoordinateWriter(driver, channel=channel)
            writer._svin_config_verified(60, 1.5, None, 'SVIN_CFG')
            error = None
        except W.WriteFailed as exc:
            error = exc
        finally:
            channel.close()
            serial_module.Serial = old_serial
        return error, calls, module

    def test_svin_readback_mismatch_retries_once_then_succeeds(self):
        """Survey-in 配置回读不一致也要重发一次（§14 #6），流程层同样适用。

        写坐标那步早就有"重发同一坐标"的重试，而配 Survey-in 没有 ——
        漏在这里的后果是：模块刚重启完、参数还没加载，用户看到"配置未生效"，
        重跑一次又好了，和写坐标那条的症状一模一样，原因却完全查不到。
        """
        error, calls, module = self._svin_writer_case(always_stale=False)
        self.assertIsNone(error, f'重试一次就该成功，实际失败：{error}')
        self.assertEqual(calls, [60, 60], '配置命令必须恰好重发一次')
        self.assertEqual(module.mem['svin']['mode'], 1, '模块最终要真的进 survey_in')

    def test_svin_readback_mismatch_gives_up_after_one_retry(self):
        """两次都对不上才判失败，并且报出"重发过还是不符"。"""
        from rtkserver.calibration import writer as W

        error, calls, _module = self._svin_writer_case(always_stale=True)
        self.assertIsNotNone(error)
        self.assertIsInstance(error, W.WriteFailed)
        self.assertEqual(error.step, 'SVIN_CFG')
        self.assertIn('仍然不符', error.message)
        self.assertEqual(calls, [60, 60], '只能重试一次，不能变成死循环重发')
        self.assertEqual(error.detail.get('attempts'), 2)

    def test_module_level_facade_matches_service_signatures(self):
        """`service.py` 模块级转发函数的参数表必须与 `CalibrationService` 方法一致。

        这里的漂移**不会在 import 时报错**，只会在 Web 接口真正调用时抛
        `TypeError: unexpected keyword argument` —— 表现是 500，且标定主入口整个
        不可用（历史上 `start_cors` 就漏了 `on_slow`，`start_write` 漏了 `arp`）。
        所以必须用测试钉死：谁改了方法参数忘了同步转发函数，这里会立刻红。
        """
        import inspect

        from rtkserver.calibration import service as facade
        from rtkserver.calibration.service import CalibrationService as S

        # task_id 是内部排队用的，不对外开放
        internal = {'self', 'task_id'}
        for name in ('start_cors', 'start_write', 'start_survey_in',
                     'start_diagnose', 'start_restore'):
            func = getattr(facade, name, None)
            self.assertIsNotNone(func, f'service.py 缺少转发函数 {name}')
            method = getattr(S, name, None)
            self.assertIsNotNone(method, f'CalibrationService 缺少方法 {name}')
            func_params = set(inspect.signature(func).parameters)
            method_params = set(inspect.signature(method).parameters) - internal
            self.assertEqual(
                method_params - func_params, set(),
                f'{name} 的转发函数漏了参数：{sorted(method_params - func_params)}，'
                f'Web 接口透传这些参数时会 TypeError 500')
            self.assertEqual(
                func_params - method_params, set(),
                f'{name} 的转发函数多出方法不接受的参数：{sorted(func_params - method_params)}')
