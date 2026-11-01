# -*- coding: utf-8 -*-
"""拆包契约测试（stdlib unittest，零新增依赖）。

这份测试的作用不是"覆盖业务"，而是**锁死拆分前后的等价性**：
一旦改动让路由表、默认配置、校验语义或 RTCM 解帧行为发生变化，这里会立刻红。
跑法：

    python -m unittest discover -s tests -v
"""
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
    """路由表必须与拆包前完全一致（11 条业务路由 + Flask 自带 static）。"""

    EXPECTED = {
        ('/', 'GET'),
        ('/login', 'GET,POST'),
        ('/logout', 'POST'),
        ('/config', 'GET,POST'),
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
                     'api_serial_ports', 'api_sourcetable', 'websocket'):
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
