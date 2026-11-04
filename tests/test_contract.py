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
import sys
import time as time_module
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 直接 `python tests/test_contract.py` 时 sys.path[0] 是 tests/，import rtkserver 会失败
# （127 个 error）。discover 模式本来就把仓库根放进 path，这里补齐，两种跑法都要能用。
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class _SilentSerial:
    """只会"收下命令、永不回话"的串口替身。

    契约测试要用真实的 `Channel` 跑驱动的失败路径，但真串口打不开、
    假模块又要为每种型号写应答脚本。这里退一步：`read` 立刻返回空，
    让 `read_line()` 自然跑到 deadline 返回 None —— 于是每条驱动都
    老老实实走"命令无应答"分支，正好用来校验失败时的返回值形状。
    """

    def __init__(self, port='SILENT', baudrate=38400):
        self.port = port
        self.baudrate = baudrate
        self.is_open = True
        self.written = []

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def flush(self):
        return None

    def reset_input_buffer(self):
        return None

    def reset_output_buffer(self):
        return None

    def read(self, size=256):
        return b''

    @property
    def in_waiting(self):
        return 0

    def close(self):
        self.is_open = False



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
        # 串口扫描是**独立**的异步接口：不取串口租约、不受标定看门狗管辖，
        # 所以不和 /start 共用一条通道，也不会因为正在标定就 409。
        ('/api/calibration/scan', 'POST'),
        ('/api/calibration/scan/status', 'GET'),
        ('/api/calibration/detect', 'POST'),
        ('/api/calibration/start', 'POST'),
        ('/api/calibration/abort', 'POST'),
        ('/api/calibration/apply-as-source', 'POST'),
        ('/api/calibration/history', 'GET'),
        # SN 级台账：只读，与 history（每次标定一行的流水）是两种粒度 ——
        # 台账一块板子一行，读一次信息就更新，没标定过的模块也在里面。
        ('/api/calibration/modules', 'GET'),
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


# 这里原来有一份 `if __name__ == '__main__': unittest.main()`，位置在文件中间。
# 它会让 `python -m tests.test_contract` 跑到这里就退出，后面 160 多项契约测试
# （含全部标定契约、升级执行器契约）从来没被执行过，跑出来永远显示"41 项全过"。
# 统一只在文件末尾保留一个入口；日常用 `python -m unittest discover -s tests -t .`。


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

    def test_module_info_table(self):
        """SN 级台账：表在、键唯一、"空值不覆盖旧值"。"""
        import shutil
        import sqlite3
        import tempfile
        from rtkserver.calibration import store

        original = store.CALIBRATION_DB
        tmpdir = tempfile.mkdtemp(prefix='2rtk-contract-')
        store.CALIBRATION_DB = os.path.join(tmpdir, 'calibration.db')
        try:
            store.init_db()
            with store._connect() as conn:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn('module_info', tables, 'module_info 表必须存在（schema v6）')
                columns = {row[1] for row in conn.execute('PRAGMA table_info(module_info)')}
                for field in ('sn_key', 'module_sn', 'module_model', 'firmware',
                              'unique_id', 'role', 'last_port', 'last_baud',
                              'last_method', 'first_seen_at', 'last_seen_at', 'seen_count'):
                    self.assertIn(field, columns, f'module_info 缺字段 {field}')
                indexes = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='module_info'")}
                self.assertIn('idx_module_info_sn', indexes)

            # 第一次：完整信息
            key = store.touch_module(
                model_key='lg290p', display_name='Quectel LG290P',
                info={'sn': 'QT2024ABC123', 'firmware': 'V1.03',
                      'build_date': '2024/05/22', 'model': 'LG290P'},
                port='COM3', baud=115200, method='cors', role='dual')
            self.assertEqual(key, 'sn:QT2024ABC123')

            # 第二次：固件/日期读到空 —— 绝不能用空串把上次的值覆盖掉，
            # 否则"有些固件第二次回读缺字段"会把台账一路清空
            store.touch_module(
                model_key='lg290p', display_name='Quectel LG290P',
                info={'sn': 'QT2024ABC123', 'firmware': '', 'build_date': ''},
                port='COM7', baud=0, method='manual', role='dual')
            row = store.get_module_info(sn='QT2024ABC123')
            self.assertEqual(row['seen_count'], 2, '两次观测必须累加')
            self.assertEqual(row['firmware'], 'V1.03', '空固件不能覆盖旧值')
            self.assertEqual(row['last_port'], 'COM7', '端口应该更新为最近一次')
            self.assertEqual(row['last_method'], 'manual')
            self.assertEqual(row['last_baud'], 115200, 'baud=0 时保留旧值')

            # 无 SN 的降级链：uid → 型号:端口 → 空
            self.assertEqual(store.sn_key('', 'UID-1', 'um980', 'COM5'), 'uid:UID-1')
            self.assertEqual(store.sn_key('', '', 'um980', 'com5'), 'loc:um980:COM5')
            self.assertEqual(store.sn_key('', '', '', ''), '')
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
        ('/api/calibration/scan', 'POST'),
        ('/api/calibration/scan/status', 'GET'),
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
        """并发任务必须 409，不能排队去抢同一个串口。

        必须带 `module='lg290p'`：不带型号的端口被当作「未知模块 → 流动站」，
        在角色闸门那一步就 400 了（见 test_unknown_module_falls_back_to_rover），
        根本走不到 `start_cors`。这条测的是"两个任务抢同一个串口"，不是角色判定。
        """
        from rtkserver.web import api_calibration as web
        original = web.calib.start_cors
        web.calib.start_cors = lambda **kwargs: (False, '已有标定任务在进行，请等它结束或先取消', None)
        try:
            client = self._client()
            resp = client.post('/api/calibration/start',
                               json={'method': 'cors', 'port': 'COM9',
                                     'module': 'lg290p',
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

    def test_save_persist_check_marks_unsupported_honestly(self):
        """没声明 `persist_check` 的型号，报告里必须写"没查"，不能写成"查过了"。

        SAVE 之后读一次 RAM 是**证明不了落盘**的（RAM 里本来就是刚写的值）。
        F9P 能 poll Flash 层，所以多查一层；其它 ASCII 型号没有这套命令，
        这里必须显式标 `checked: False` —— 如果写成 `{'ok': True}`，
        看日志的人会以为持久化被验证过，而实际上根本没查。
        """
        from rtkserver.calibration.writer import CoordinateWriter, WritePlan

        plan = WritePlan.build_cors(host='h', port=8002, mountpoint='MP',
                                    method='external_rover')

        class _NoPersist:
            CAPS = {}

        writer = CoordinateWriter(driver=_NoPersist(), channel=None)
        result = writer._verify_persisted_after_save(plan)
        self.assertIs(result.get('checked'), False)
        self.assertEqual(result.get('reason'), 'unsupported')
        self.assertNotIn('ok', result, '没查过就不能出现 ok 字段')

    def test_save_persist_check_downgrades_mismatch_to_warning(self):
        """持久层不一致只告警、不抛 —— 硬判据留给重启后的 VERIFY_MODE。

        理由：Flash 写入偶发失败（模块忙、供电抖动）重启后还能重试，
        当场判死会让用户白丢一次已经收敛好的坐标。
        """
        from rtkserver.calibration.writer import CoordinateWriter, WritePlan

        plan = WritePlan.build_cors(host='h', port=8002, mountpoint='MP',
                                    method='external_rover')
        plan.mode = 'base'

        class _Driver:
            CAPS = {'persist_check': True}
            MODE_VALUE = {'disabled': 0, 'survey_in': 1, 'fixed': 2}

            def __init__(self):
                self.seen = None

            def verify_persisted(self, expected=None):
                self.seen = expected
                return False, 'Flash 层有 1 个键不符：TMODE_MODE 要 2 实际 0', {
                    'layer': 2, 'mismatch': {'TMODE_MODE': {'want': 2, 'got': 0}}}

        driver = _Driver()
        writer = CoordinateWriter(driver=driver, channel=None)
        warnings = []
        writer._warn = lambda message, *a, **k: warnings.append(message)

        result = writer._verify_persisted_after_save(plan)
        self.assertTrue(result['checked'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['mismatch']['TMODE_MODE']['got'], 0)
        self.assertEqual(len(warnings), 1, '不一致必须留下一条告警，不能静默')
        # base 要翻成 TMODE 的 fixed(2)，不能把 'base' 原样丢给驱动
        self.assertEqual(driver.seen, {'TMODE_MODE': 2})

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

    def test_backup_mode_carries_normalized_role(self):
        """备份里的 `mode` 装的是**角色**，必须额外存归一化的 `role`。

        三家驱动 `get_mode()` 的 `text` 取值来源不同（LG290P 读 RCVRMODE、
        LC29H(BS) 用 SVIN Mode 反推），而 `svin['mode']` 装的是坐标模式
        （fixed/survey_in/disabled）。两个 `mode` 字段同名不同义，
        还原/展示一律走 role，避免混用。
        """
        from rtkserver.calibration import backup

        self.assertEqual(backup._role_of(1, 'rover'), 'rover')
        self.assertEqual(backup._role_of(2, 'base'), 'base')
        # code 优先：text 给错也不能盖过协议里的数字
        self.assertEqual(backup._role_of(2, 'rover'), 'base')
        # 坐标模式值不是工作模式，必须判 unknown 而不是被当成 base
        self.assertEqual(backup._role_of(None, 'fixed'), 'unknown')
        self.assertEqual(backup._role_of(None, 'survey_in'), 'unknown')
        # 老备份没有 role 字段时按 code 现算，不能因此判成不可还原
        legacy = {'mode': {'code': 1, 'text': 'rover'}, 'svin': {}}
        self.assertTrue(backup.usable(legacy)[0])
        legacy_bad = {'mode': {'code': 0, 'text': 'unknown'}, 'svin': {}}
        self.assertFalse(backup.usable(legacy_bad)[0])

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

    def test_cors_reuse_source_is_latest_record_any_module(self):
        """复用坐标取**库内最近一条**，不按模块/SN 过滤。

        这条约定曾经是反的：早先强制按 `module_sn` 过滤，理由是"防止拿别的
        天线的坐标写进来"。真机上这个判据直接挡住了正确操作 —— 先用 LG290P
        测出天线坐标，换插 LC29H(BS) 后复用入口消失，而那是同一根天线、
        同一个安装位置，只是换了块模块。
        坐标描述的是**天线相位中心**这个物理点，与"谁去测、谁去播"无关。
        程序无从判断天线有没有被挪动（那才是真正的风险），所以把判据交回给人：
        页面把型号/SN/时间两组坐标都列出来，由用户核对。
        """
        source = self._read('rtkserver', 'web', 'api_calibration.py')
        body = source[source.index('def api_calibration_options():'):]
        body = body[:body.index('@login_required')]
        self.assertIn('reuse_history', body, '必须返回复用来源')
        self.assertNotIn('module_sn=', body, '复用来源不得按 module_sn 过滤')
        self.assertNotIn("request.args.get('sn')", body,
                         '不得再要求前端传 SN 才能拿到复用坐标')

        js = self._read('static', 'calibration.js')
        self.assertIn('reuse_history', js)
        self.assertIn('STATE.reuseHistory', js)
        self.assertNotIn('own_history', js)
        # 复用值必须取 reuseHistory，不能直接摸 STATE.history（那只是"最近三条"）
        build = js[js.index('function buildPayload()'):]
        build = build[:build.index('function validate(')]
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

            def __init__(self, port=None, baud=None, task_id='', action=''):
                self.stage = 'IDLE'
                self.port = port
                self.channel = None
                self.task_id = task_id
                self.action = action
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


# ==================== 标定接口的解包契约 ====================
class TestCalibrationApiUnpacking(unittest.TestCase):
    """Web 接口对 `service` 返回值的解包必须与实现一致。

    真机踩过：`service.abort()` 返回 `(ok, message)` 两元组（service.py 内另外
    两处调用点也是这么解的），而 `api_calibration_abort` 按三元组解包，
    结果点「中止」必定 `ValueError → 500`。取消动作其实已经生效，但页面只能
    看到"服务器错误"，用户会以为没停下来，反复点。
    """

    def _call(self, func_name, stub_returns):
        import types
        from flask import Flask, session
        from rtkserver.web import api_calibration as api

        app = Flask(__name__)
        app.secret_key = 'test-only'
        saved = {}
        # 把 service 门面换成桩，只保留被测接口用到的那一个函数
        fake = types.SimpleNamespace(**{func_name: lambda *a, **k: stub_returns})
        for attr in ('abort', 'start_diagnose', 'status'):
            if not hasattr(fake, attr):
                setattr(fake, attr, lambda *a, **k: (True, 'ok', {}))
        saved['calib'] = api.calib
        saved['snap'] = api._snapshot_payload
        api.calib = fake
        api._snapshot_payload = lambda full=False: {}
        try:
            with app.test_request_context('/api/calibration/abort', method='POST'):
                session['logged_in'] = True
                func = getattr(api, 'api_calibration_abort')
                return func.__wrapped__()
        finally:
            api.calib = saved['calib']
            api._snapshot_payload = saved['snap']

    def test_abort_accepts_two_tuple(self):
        """abort 返回 (ok, message) 时必须 200，不能 500。"""
        resp, code = self._call('abort', (True, '已请求取消，正在收尾'))
        self.assertEqual(code, 200, f'abort 两元组解包失败，返回 {code}')
        self.assertTrue(resp.get_json().get('ok'))

    def test_abort_idle_returns_409(self):
        """没有进行中的任务时返回 409 而不是 200。"""
        resp, code = self._call('abort', (False, '当前没有进行中的任务'))
        self.assertEqual(code, 409)


class TestBaudRecall(unittest.TestCase):
    """波特率必须"问模块"，而不是一档档盲试。

    现场原话：「官方文档说默认是个很高的档，测试时用的却是 115200，
    能不能用命令集查出来，把官方波特率记下来优先用，别一个一个递增试」。

    对应的实现有三块，缺一块这条链路就断：
      1. 驱动 `read_uart()` —— 真的发回读命令并解析模块的回答；
      2. `module_profiles.uart_baud` —— 把回读值按型号记住；
      3. `_baud_candidates(remembered=...)` —— 下次把它排在候选表最前面。
    """

    # ---------- ① 默认口令判断（登录页那条提示的开关） ----------
    def test_default_admin_detection(self):
        from rtkserver.security import hash_password, is_default_admin
        self.assertTrue(is_default_admin({'admin_user': 'admin', 'admin_password': 'admin'}),
                        '明文默认口令必须判为"仍是默认"')
        self.assertTrue(is_default_admin({'admin_user': 'admin',
                                          'admin_password': hash_password('admin')}),
                        '哈希后的默认口令同样要判出来（改密页写的就是哈希）')
        self.assertFalse(is_default_admin({'admin_user': 'admin',
                                           'admin_password': hash_password('other')}),
                         '改过口令后不能再提示默认账号')
        self.assertFalse(is_default_admin({'admin_user': 'root', 'admin_password': 'admin'}))
        self.assertFalse(is_default_admin({}))
        self.assertFalse(is_default_admin(None))

    def test_login_page_shows_default_hint_only_when_default(self):
        html = os.path.join(REPO_ROOT, 'templates', 'login.html')
        with open(html, 'r', encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn('{% if default_login %}', text,
                      '提示必须受 default_login 控制，否则改过密码还在教人用默认口令')
        self.assertIn('login.default.title', text)
        self.assertIn('login.default.body', text)
        with open(os.path.join(REPO_ROOT, 'rtkserver', 'web', 'routes.py'),
                  'r', encoding='utf-8') as handle:
            routes = handle.read()
        self.assertIn('is_default_admin(get_config())', routes,
                      '登录路由必须自己算出 default_login')

    def test_i18n_has_both_languages(self):
        with open(os.path.join(REPO_ROOT, 'static', 'i18n.js'),
                  'r', encoding='utf-8') as handle:
            text = handle.read()
        for key in ('login.default.title', 'login.default.body',
                    'cal.hw.uart', 'cal.baud.auto_remembered',
                    'cal.module.hint.auto', 'cal.module.hint.fixed'):
            self.assertEqual(text.count(f"'{key}'"), 2,
                             f'{key} 必须中英文各一条（实际 {text.count(key)}）')

    # ---------- ② 驱动回读 ----------
    def _driver(self, replies):
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        class _StubChannel:
            def __init__(self):
                self.replies = list(replies)
                self.sent = []
                self.cache = {}
                self.counters = {'lines': 0, 'frames': 0, 'noise': 0}
                self.parser = None
                self.heads = ('$',)
                self.frame_out = True
                self.reply_aliases = {}

            def send(self, body, timeout=None, retries=None, interval=None):
                self.sent.append(body)
                if not self.replies:
                    return 'timeout', ''
                return self.replies.pop(0)

        channel = _StubChannel()
        return LG290PDriver(channel), channel

    def test_lg290p_read_uart_parses_index_first(self):
        """`$PQTMCFGUART,OK,<Index>,<BaudRate>,...`：Index 在最前面。

        按"最后一个数字当波特率"的经验写法会拿到流控标志 0，
        现场表现是"模块说波特率是 0"——这条用例专门锁住字段顺序。
        """
        driver, channel = self._driver([('ok', '$PQTMCFGUART,OK,1,115200,8,0,1,0*5F')])
        ok, message, data = driver.read_uart()
        self.assertTrue(ok, message)
        self.assertEqual(data['baud'], 115200)
        self.assertEqual(data['port'], 1)
        self.assertEqual(data['databits'], 8)
        self.assertEqual(data['parity'], 'N')
        self.assertEqual(data['stopbits'], 1)
        self.assertEqual(data['flow'], 0)
        self.assertEqual(data['frame'], '8N1')
        self.assertIn('115200', message)

    def test_lg290p_read_uart_uses_documented_command(self):
        driver, channel = self._driver([('ok', '$PQTMCFGUART,OK,1,460800,8,0,1,0*3F')])
        driver.read_uart()
        self.assertEqual(channel.sent[0], 'PQTMCFGUART,R',
                         '当前口回读必须发 `PQTMCFGUART,R`（§6.8）')
        driver2, channel2 = self._driver([('ok', '$PQTMCFGUART,OK,2,9600,8,0,1,0*11')])
        driver2.read_uart(port=2)
        self.assertEqual(channel2.sent[0], 'PQTMCFGUART,R,2')

    def test_lg290p_read_uart_bare_ok_is_failure(self):
        """只回 `$PQTMCFGUART,OK` 时不许编一个波特率出来。"""
        driver, _ = self._driver([('ok', '$PQTMCFGUART,OK*10')])
        ok, message, data = driver.read_uart()
        self.assertFalse(ok)
        self.assertEqual(data.get('error'), 'bad_reply')

    def test_read_uart_keys_declared_per_driver(self):
        """有回读命令的型号要声明，没有的型号保持空串（别假装支持）。"""
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver
        from rtkserver.calibration.drivers.unicore_n4 import UnicoreUM980Driver
        self.assertEqual(LG290PDriver.BAUD_READ_KEY, 'get_baud')
        self.assertEqual(UnicoreUM980Driver.BAUD_READ_KEY, 'get_baud')
        # LC29HBS 只有 PAIR864 写命令，没有回读 —— 声明成空串，
        # read_uart() 就会老实返回"不支持"，而不是发一条必然超时的命令
        self.assertEqual(LC29HBSDriver.BAUD_READ_KEY, '')
        ok, message, _data = LC29HBSDriver(None).read_uart()
        self.assertFalse(ok)
        self.assertIn('不支持', message)

    # ---------- ③ 记忆 + 排序 ----------
    def test_default_baud_is_tried_first(self):
        """候选表必须把 `DEFAULT_BAUD` 排第一，而不是最高档。

        真机教训：纯降序让 921600 永远排第一，一块默认跑 460800 的 LG290P
        要错 2 档（每档 2 秒超时）才命中 —— 用户抱怨的"一档档试"从低往高
        变成了从高往低，总等待一点没少。`DEFAULT_BAUD` 之前只声明、不参与
        排序，等于白写。

        这里锁三件事：
          1. 第一位是 `DEFAULT_BAUD`（该型号自己的默认档）；
          2. 厂商档位一个都不能丢 —— 押错了还得靠它们兜；
          3. 每个型号的首选是它自己的常量，不是一张通用表
             （LG290P=460800 / LC29H(BS)=115200，见下一个测试）。
        """
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        order = LG290PDriver(None).bauds()
        self.assertEqual(order[0], LG290PDriver.DEFAULT_BAUD,
                         'DEFAULT_BAUD 必须排第一，否则首轮就在白等超时')
        self.assertEqual(set(order), set(LG290PDriver.BAUDS),
                         '重排不能丢掉任何厂商档位')
        self.assertEqual(len(order), len(set(order)), '不能有重复档位')

        # 其余部分仍是降序：押注落空之后，先试高档位（线材好的可能性更大）
        rest = list(order[1:])
        self.assertEqual(rest, sorted(rest, reverse=True))

    def test_detect_bauds_entry_uses_default_first(self):
        """`detect_bauds()` 是采集/识别真正走的入口，也得是同一套顺序。"""
        from rtkserver.calibration.drivers import driver_manager
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        self.assertEqual(driver_manager.detect_bauds('lg290p')[0],
                         LG290PDriver.DEFAULT_BAUD)

    def test_new_device_candidates_hit_default_without_scan(self):
        """全新设备（无手填、无记忆）第一次就该命中默认档，0 次超时。"""
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver
        from rtkserver.calibration.service import CalibrationService

        order = CalibrationService._baud_candidates(['lg290p'])
        self.assertEqual(order[0], LG290PDriver.DEFAULT_BAUD,
                         '全新模块的第一档必须是该型号自己的默认档，不能是 921600')

    def test_store_remembers_and_keeps_uart_baud(self):
        import shutil
        import sqlite3
        import tempfile
        from rtkserver.calibration import store

        original = store.CALIBRATION_DB
        tmpdir = tempfile.mkdtemp(prefix='2rtk-baud-')
        store.CALIBRATION_DB = os.path.join(tmpdir, 'calibration.db')
        try:
            store.init_db()
            conn = sqlite3.connect(store.CALIBRATION_DB)
            try:
                columns = {row[1] for row in conn.execute(
                    'PRAGMA table_info(module_profiles)')}
            finally:
                conn.close()
            self.assertIn('uart_baud', columns)
            self.assertIn('uart_info', columns)

            self.assertEqual(store.remembered_baud('lg290p'), 0, '新库不该凭空有值')
            store.save_profile('lg290p', 'Quectel LG290P', 'LG290P', {'svin': True},
                               uart={'port': 1, 'baud': 460800, 'frame': '8N1'})
            self.assertEqual(store.remembered_baud('lg290p'), 460800)
            self.assertEqual(store.get_profile('lg290p')['uart']['baud'], 460800)

            # 只同步 CAPS（不带 uart）不能把记住的值冲掉
            store.save_profile('lg290p', 'Quectel LG290P', 'LG290P', {'svin': True})
            self.assertEqual(store.remembered_baud('lg290p'), 460800,
                             '这次没回读，不许清空上次的记忆')
            # 回读成功但波特率解析为 0，同样不许覆盖
            store.save_profile('lg290p', 'Quectel LG290P', 'LG290P', None,
                               uart={'baud': 0})
            self.assertEqual(store.remembered_baud('lg290p'), 460800)
        finally:
            store.CALIBRATION_DB = original
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_baud_candidates_put_remembered_second(self):
        """`remembered` 要排在手填值之后、厂商档位表之前。

        没有这一条，每次连设备都要白等好几轮 2 秒超时：模块被刷过固件或改过
        串口配置之后，它跑的那一档未必还是厂商示例里那个值，而这是唯一一条
        "模块亲口说的"证据，必须是候选表里最靠前的推断。
        """
        from rtkserver.calibration.service import CalibrationService

        got = CalibrationService._baud_candidates(['lg290p'], current=None,
                                                 remembered=115200)
        self.assertEqual(got[0], 115200, got)
        self.assertEqual(len(got), len(set(got)), f'候选表有重复：{got}')

        got = CalibrationService._baud_candidates(['lg290p'], current=460800,
                                                 remembered=115200)
        self.assertEqual(got[:2], [460800, 115200],
                         f'手填值必须最优先，记忆值紧跟其后：{got}')

        # 没记忆时：该型号的 DEFAULT_BAUD 打头，其余降序
        # （LG290P 的默认档是 460800，不是 115200 —— 见驱动里那段注释）
        plain = CalibrationService._baud_candidates(['lg290p'])
        self.assertEqual(plain, [460800, 921600, 230400, 115200, 9600], plain)

        # 记忆值本来就在厂商档位里时不能出现两次
        got = CalibrationService._baud_candidates(['lg290p'], remembered=921600)
        self.assertEqual(got.count(921600), 1, got)


class TestDropdownAnchor(unittest.TestCase):
    """候选面板必须锚在输入框上（真机：扫描串口后列表满屏乱跑）。

    根因不是"样式没对齐"，而是原生 `<datalist>` 弹层会被祖先的 transform 影响：
    卡片上的 `.fade-in` 动画曾经在末帧留下 `transform: translateY(0)`，浏览器据此
    把卡片当成包含块，弹层坐标就算到别处。修法是自绘面板 + `position: fixed` 现算坐标。
    """

    def test_card_animation_must_not_touch_transform(self):
        """`@keyframes fade-in` 里不许出现 transform —— 这直接决定下拉会不会卡。

        实测（Chrome，同一台机器、同一页面，唯一变量是"点的时候动画有没有在跑"）：

           动画已结束再点 #cal-module（模块型号下拉）→  50 ~ 56 ms
           动画进行中（0.35s 窗口内）点同一个下拉   → 454 ~ 972 ms

        `transform` 动画会把卡片提升为合成层，而原生 `<select>` 的下拉列表由浏览器
        UI 线程在合成层里定位绘制 —— 位移每帧都变，列表就得跟着重算。几百毫秒的
        延迟到了手机或低配设备上，用户看到的就是"点一下浏览器卡死"。

        还有两个"看起来能修好、实际没用"的写法，别试：
          * 末帧写 `transform: none`：keyframe 里只要出现过 transform 就参与插值，
            `none` 按单位矩阵算，计算值仍是 matrix(1,0,0,1,0,0)；
          * 只把 `both` 换成 `backwards`：能消掉残留，但动画进行中照样是合成层，
            卡顿窗口只是缩小到 0.2 秒。
        唯一可靠的做法是不动画 transform。这条测试就是防止它被加回去。
        """
        with open(os.path.join(REPO_ROOT, 'static', 'theme.css'), 'r',
                  encoding='utf-8') as handle:
            css = handle.read()

        start = css.find('@keyframes fade-in')
        self.assertNotEqual(start, -1, 'theme.css 里找不到 @keyframes fade-in')
        block = css[start:css.find('}', css.find('to', start))]
        self.assertNotIn('transform', block,
                         '@keyframes fade-in 里又出现了 transform —— '
                         '卡片里的原生下拉会重新变卡，详见本测试的说明')

    def test_picker_script_exists_and_is_wired(self):
        path = os.path.join(REPO_ROOT, 'static', 'picker.js')
        self.assertTrue(os.path.exists(path), 'static/picker.js 不存在')
        for name in ('config.html', 'calibration.html'):
            with open(os.path.join(REPO_ROOT, 'templates', name), 'r',
                      encoding='utf-8') as handle:
                text = handle.read()
            self.assertIn('/static/picker.js', text, f'{name} 没有引入 picker.js')
            # 脚本必须在页面自己的脚本之前加载：页面脚本会在按钮回调里调 Picker.open
            self.assertLess(text.index('/static/picker.js'),
                            text.index('/static/calibration.js')
                            if name == 'calibration.html' else len(text))

    def test_picker_detaches_native_list(self):
        with open(os.path.join(REPO_ROOT, 'static', 'picker.js'), 'r',
                  encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn("removeAttribute('list')", text,
                      '必须摘掉 list 属性，否则原生弹层仍会另开一个')
        self.assertIn('document.body.appendChild(panel)', text,
                      '面板必须挂 body，不能留在有 transform 的卡片里')

    def test_picker_panel_css_is_fixed(self):
        with open(os.path.join(REPO_ROOT, 'static', 'theme.css'), 'r',
                  encoding='utf-8') as handle:
            css = handle.read()
        block = css[css.index('.picker-panel {'):]
        block = block[:block.index('}')]
        self.assertIn('position: fixed', block)
        self.assertIn('z-index', block)

    def test_outlet_cards_are_equal_height(self):
        """三个出口卡片要等高：按内容各自撑高会得到 7/4/6 行的参差框。"""
        with open(os.path.join(REPO_ROOT, 'static', 'theme.css'), 'r',
                  encoding='utf-8') as handle:
            css = handle.read()
        block = css[css.index('.grid-outlets {'):]
        block = block[:block.index('}')]
        self.assertIn('align-items: stretch', block,
                      'grid-outlets 不能再用 align-items: start')
        self.assertNotIn('align-items: start', block)
        self.assertIn('.grid-outlets > .card', css,
                      '卡片要变成纵向 flex，底部提示才能贴底对齐')
        self.assertIn('.grid-outlets > .card > .field-hint:last-child', css)



class TestDriverProtocolSurface(unittest.TestCase):
    """每个驱动的"流程层协议面"必须完整，缺一个方法就是一次 500。

    真机事故：换插 LC29H(BS) 后点「读取模块信息」直接
        AttributeError: 'LC29HBSDriver' object has no attribute 'read_rtcm_config'
    整个诊断接口 500。原因不是命令写错，而是**流程层会无条件调用的一组方法**
    在驱动里根本不存在 —— 诊断报告要读 RTCM 配置、备份要逐项回读，
    这些都按"型号有命令就实现、没有就返回不支持"的约定走，但没有强制。
    这条用例把约定变成契约：方法必须存在、必须返回三元组、不许抛 AttributeError。
    """

    PIPELINE_METHODS = (
        'get_svin_config', 'read_rtcm_config', 'restore_rtcm_config',
        'config_rtcm', 'get_message_rate', 'set_message_rate',
        'disable_survey_in', 'read_antenna_info',
        'get_mode', 'set_mode', 'set_mode_memory_only',
        'get_fixed_ecef', 'set_fixed_ecef', 'config_survey_in', 'survey_status',
        'config_messages', 'set_antenna_info', 'save', 'reboot',
    )

    def _driver_classes(self):
        from rtkserver.calibration.drivers import driver_manager
        classes = []
        for key in driver_manager.supported_driver_ids():
            driver = driver_manager.get_driver_by_id(key)
            if driver is not None:
                classes.append((key, driver.__class__))
        return classes

    def test_every_registered_driver_exposes_pipeline_methods(self):
        classes = self._driver_classes()
        self.assertGreater(len(classes), 2, '至少应有多个型号注册，否则这条用例没意义')
        for key, cls in classes:
            for name in self.PIPELINE_METHODS:
                self.assertTrue(
                    callable(getattr(cls, name, None)),
                    f'{cls.__name__}（{key}）缺少 {name}()：'
                    f'流程层会无条件调用它，缺了就是一次 500')

    def test_every_driver_get_mode_returns_triple(self):
        """**每个具体驱动的 `get_mode()` 都必须回三元组，第三项是 dict。**

        这条是补上的漏洞：ZED-F9P 曾经 `return 'base'`（裸字符串），
        而 `service.py` 写的是 `ok, message, mode = driver.get_mode()`
        再 `mode.get('mode')` —— 真机一走到"读模块信息"就 AttributeError。

        旧用例 `test_base_defaults_return_triples_not_exceptions` 只校验了
        基类默认实现，子类覆盖后返回什么完全没管，所以这个错漏到了真机。

        这里不连串口：给每条驱动挂一个"永远没数据"的假串口，
        让 `get_mode()` 真真正正走完"发命令 → 等应答 → 超时"的完整路径。
        这样校验的不是签名，而是**失败路径的返回值形状**——恰恰是
        真机上第一个被调用、也最容易被写成裸值的分支。
        """
        classes = self._driver_classes()
        self.assertGreater(len(classes), 2)
        from rtkserver.calibration.drivers.base import BaseDriver, Channel

        for key, cls in classes:
            if cls.get_mode is BaseDriver.get_mode:
                continue        # 没覆盖的走基类默认实现，已被上一条用例覆盖
            channel = Channel(serial_instance=_SilentSerial())
            driver = cls(channel)
            try:
                result = driver.get_mode()
            except Exception as err:            # noqa: BLE001
                # 缺件/表错这类 DriverBug 属于另一类问题，不在这里判；
                # 但 AttributeError 一定说明驱动与基类契约不一致（曾经的 `return 'base'`）。
                self.assertNotIsInstance(
                    err, AttributeError,
                    f'{cls.__name__}（{key}）.get_mode() 内部有 AttributeError：{err}')
                continue
            self.assertIsInstance(
                result, tuple,
                f'{cls.__name__}（{key}）.get_mode() 必须返回三元组，'
                f'实际返回 {type(result).__name__}：{result!r}')
            self.assertEqual(len(result), 3,
                             f'{cls.__name__}（{key}）.get_mode() 三元组长度不对')
            self.assertIsInstance(
                result[2], dict,
                f'{cls.__name__}（{key}）.get_mode() 第三项必须是 dict（流程层会 .get()）')

    def test_base_defaults_return_triples_not_exceptions(self):
        """基类默认实现必须返回 `(False, 原因, {...})`，不能抛异常。"""
        from rtkserver.calibration.drivers.base import BaseDriver

        class _Bare(BaseDriver):
            MODEL_KEY = 'bare'

        driver = _Bare(None)
        calls = (
            ('get_svin_config', (), {}),
            ('read_rtcm_config', (), {}),
            ('restore_rtcm_config', ([],), {}),
            ('config_rtcm', (), {}),
            ('get_message_rate', ('GGA',), {}),
            ('set_message_rate', ('GGA', 1), {}),
            ('disable_survey_in', (), {'persist': False}),
            ('read_antenna_info', (), {}),
            ('get_mode', (), {}),
            ('set_mode', ('base',), {}),
        )
        for name, args, kwargs in calls:
            result = getattr(driver, name)(*args, **kwargs)
            self.assertIsInstance(result, tuple, f'{name} 必须返回三元组')
            self.assertEqual(len(result), 3, f'{name} 必须返回三元组')
            self.assertFalse(result[0], f'{name} 默认实现必须返回失败')
            self.assertTrue(result[1], f'{name} 必须给出原因，不能空字符串')
            self.assertEqual(result[2].get('error'), 'unsupported',
                             f'{name} 的错误码必须是 unsupported')


class TestLc29hbsMissingCommands(unittest.TestCase):
    """LC29H(BS) 曾经缺失的 5 条命令，现在都必须是"真实现"而不是空壳。

    这一组不是"能返回就行"：每条都要检查**下发的命令体**，
    否则很容易写成"返回 True 但什么都没发"——那比 AttributeError 更难查。
    """

    def _driver(self, replies):
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver

        class _StubChannel:
            """按真实 `Channel` 的接口来：PAIR 设置命令走 write_line/read_line/absorb，
            走 `send()` 的只有基类那条通用路径。

            早先这个桩只实现了 `send()`，于是驱动里新加的 `$PAIR001` 应答路径
            在测试里直接 AttributeError —— 等于"新逻辑一条都没被测到"。
            """

            def __init__(self):
                self.queue = [line for status, line in replies
                              if status != 'timeout' and line]
                self.sent = []
                self.cache = {}
                self.counters = {'lines': 0, 'frames': 0, 'noise': 0}
                self.parser = None
                self.heads = ('$',)
                self.frame_out = True
                self.reply_aliases = {}

            def write_line(self, body):
                self.sent.append(body)

            def read_line(self, timeout=None):
                if not self.queue:
                    return None
                return self.queue.pop(0)

            def absorb(self, line):
                self.counters['lines'] += 1
                if self.parser is not None:
                    return self.parser(line)
                return None

            def send(self, body, timeout=None, retries=None, interval=None):
                self.sent.append(body)
                if not self.queue:
                    return 'timeout', ''
                line = self.queue.pop(0)
                return ('error' if ',ERROR' in line else 'ok'), line

        channel = _StubChannel()
        return LC29HBSDriver(channel), channel

    def test_read_rtcm_config_translates_mode_to_msm_type(self):
        """`PAIR433` 只有一个全局 Mode，必须翻译成上层的 `msm_type`。

        上层（诊断、备份、还原）统一按 MSM 档位判断，直接回 `{'mode': 0}`
        会让它们拿到 None，表现为"MSM 档位未知"，还原时把档位丢掉。
        """
        driver, channel = self._driver([
            ('ok', '$PAIR433,0*3E'), ('ok', '$PAIR435,1*38'), ('ok', '$PAIR437,0*3A')])
        ok, message, data = driver.read_rtcm_config()
        self.assertTrue(ok, message)
        self.assertEqual(data['msm_type'], 4)
        self.assertEqual(data['mode'], 0)
        self.assertEqual(data['ant_pnt'], 1)
        self.assertEqual(data['ephemeris'], 0)
        self.assertEqual(channel.sent, ['PAIR433', 'PAIR435', 'PAIR437'])

        driver, _ = self._driver([
            ('ok', '$PAIR433,1*3E'), ('ok', '$PAIR435,1*38'), ('ok', '$PAIR437,0*3A')])
        ok, _m, data = driver.read_rtcm_config()
        self.assertTrue(ok)
        self.assertEqual(data['msm_type'], 7, 'Mode=1 是 MSM7')

    def test_read_rtcm_config_reads_value_from_fields(self):
        """`mode` 必须从 `fields[0]` 取，不能读 `data['mode']`（那里永远是 None）。"""
        driver, _ = self._driver([
            ('ok', '$PAIR433,0*3E'), ('ok', '$PAIR435,0*38'), ('ok', '$PAIR437,1*3A')])
        ok, _m, data = driver.read_rtcm_config()
        self.assertTrue(ok)
        self.assertEqual(data['mode'], 0, 'PAIR433 的值在第一个字段里')
        self.assertEqual(data['ant_pnt'], 0)
        self.assertEqual(data['ephemeris'], 1)

    def test_config_rtcm_only_accepts_msm4_and_msm7(self):
        """PAIR432 只有 0/1 两个值，MSM5/6 必须明确拒绝而不是悄悄降级。"""
        driver, channel = self._driver([('ok', '$PAIR001,432,0*3E')])
        ok, message, _data = driver.config_rtcm(msm_type=7, persist=False)
        self.assertTrue(ok, message)
        self.assertEqual(channel.sent, ['PAIR432,1'])

        driver, channel = self._driver([])
        ok, message, data = driver.config_rtcm(msm_type=5, persist=False)
        self.assertFalse(ok)
        self.assertIn('MSM4', message)
        self.assertEqual(data['supported'], [4, 7])
        self.assertEqual(channel.sent, [], '不支持的档位不能发任何命令')

    def test_message_rate_whitelist_and_msg_ver(self):
        """只有两条语句可配速率，且 MsgVer 不能统一写 1（PQTMEPE 是 2）。"""
        driver, channel = self._driver([('ok', '$PQTMCFGMSGRATE,OK,1*00')])
        ok, message, _data = driver.set_message_rate('PQTMSVINSTATUS', 1)
        self.assertTrue(ok, message)
        self.assertEqual(channel.sent, ['PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1'])

        driver, channel = self._driver([('ok', '$PQTMCFGMSGRATE,OK,1*00')])
        ok, _m, _d = driver.set_message_rate('PQTMEPE', 1)
        self.assertTrue(ok)
        self.assertEqual(channel.sent, ['PQTMCFGMSGRATE,W,PQTMEPE,1,2'],
                         'PQTMEPE 的 MsgVer 是 2，写成 1 模块会回 ERROR,1')

        driver, channel = self._driver([])
        ok, message, data = driver.set_message_rate('GGA', 1)
        self.assertFalse(ok)
        self.assertIn('不支持', message)
        self.assertEqual(data['supported'], ['PQTMSVINSTATUS', 'PQTMEPE'])
        self.assertEqual(channel.sent, [], '不支持的语句不能发命令去撞超时')

        driver, channel = self._driver([])
        ok, message, _d = driver.set_message_rate('PQTMEPE', 99)
        self.assertFalse(ok)
        self.assertIn('1~20', message)

    def test_get_message_rate_parses_rate_after_name(self):
        driver, channel = self._driver([
            ('ok', '$PQTMCFGMSGRATE,OK,PQTMSVINSTATUS,5,1*00')])
        ok, _m, data = driver.get_message_rate('PQTMSVINSTATUS')
        self.assertTrue(ok)
        self.assertEqual(data['rate'], 5)
        self.assertEqual(channel.sent, ['PQTMCFGMSGRATE,R,PQTMSVINSTATUS,1'])

    def test_disable_survey_in_sends_mode_zero(self):
        """停用 SVIN 必须是 `PQTMCFGSVIN,W,0,...`（7 字段，与 LG290P 的 8 字段不同）。"""
        driver, channel = self._driver([('ok', '$PQTMCFGSVIN,OK,0,0,0,0,0,0*00')])
        ok, message, _data = driver.disable_survey_in(persist=False)
        self.assertTrue(ok, message)
        self.assertEqual(channel.sent, ['PQTMCFGSVIN,W,0,0,0,0,0,0'])


class TestLc29hbsRealDevice(unittest.TestCase):
    """LC29H(BS) 真机联调暴露的问题（2026-11-03，LC29HBSNR11A01S / CH340 / 115200）。

    这一组全部来自真机：文档命令体本身是对的（16 条示例校验和逐条验过），
    错的是驱动与流程层的接口、以及"设置类 PAIR 命令怎么收应答"。
    """

    def _driver(self, replies):
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver

        class _StubChannel:
            def __init__(self):
                self.queue = [line for status, line in replies
                              if status != 'timeout' and line]
                self.sent = []
                self.cache = {}
                self.counters = {'lines': 0, 'frames': 0, 'noise': 0}
                self.parser = None
                self.heads = ('$',)
                self.frame_out = True
                self.reply_aliases = {}

            def write_line(self, body):
                self.sent.append(body)

            def read_line(self, timeout=None):
                if not self.queue:
                    return None
                return self.queue.pop(0)

            def absorb(self, line):
                self.counters['lines'] += 1
                if self.parser is not None:
                    return self.parser(line)
                return None

            def send(self, body, timeout=None, retries=None, interval=None):
                self.sent.append(body)
                if not self.queue:
                    return 'timeout', ''
                line = self.queue.pop(0)
                return ('error' if ',ERROR' in line else 'ok'), line

        channel = _StubChannel()
        return LC29HBSDriver(channel), channel

    # ---------- ① 签名：流程层是按基类签名调的 ----------
    def test_flow_layer_keyword_calls_never_raise_type_error(self):
        """流程层按**基类签名**调驱动，任何型号都不许把参数收窄。

        真机症状：`TypeError: LC29HBSDriver.set_mode() got an unexpected
        keyword argument 'persist'` —— 写坐标和自收敛两条路都在 BASE_SETUP
        阶段就炸掉，一条命令都没发出去。这类错误在单元测试里完全看不见
        （没人按流程层的方式调），所以只能拿基类签名去套所有驱动。
        """
        import inspect
        from rtkserver.calibration.drivers.base import BaseDriver
        from rtkserver.calibration.drivers import driver_manager as dm

        # 流程层真正带关键字调用的方法（`writer.py` / `lease.py`）
        keyword_calls = {
            'set_mode': {'persist': True, 'restart': False, 'verify': False},
            'config_survey_in': {'count': 1, 'acc_limit': 1.0, 'distance': None,
                                 'persist': False, 'restart': False, 'verify': False},
            'set_fixed_ecef': {'x': 1.0, 'y': 2.0, 'z': 3.0,
                               'persist': False, 'restart': False, 'verify': False},
            'set_antenna_info': {'desc': 'ant', 'setup_id': 0, 'sn': '',
                                 'persist': False},
            'config_rtcm': {'msm_type': 4, 'persist': False},
            'disable_survey_in': {'persist': False, 'restart': False},
        }
        dm.load_builtin_drivers()
        self.assertTrue(dm._REGISTRY, '至少要有内置驱动')
        for key, cls in sorted(dm._REGISTRY.items()):
            for method, kwargs in keyword_calls.items():
                func = getattr(cls, method, None)
                base = getattr(BaseDriver, method)
                if func is None or func is base:
                    continue
                signature = inspect.signature(func)
                try:
                    # bind_partial：位置参数由流程层另行传，这里只验关键字能不能收
                    signature.bind_partial(object(), **kwargs)
                except TypeError as exc:
                    self.fail(f'{cls.__name__}.{method} 收不下流程层的调用 '
                              f'({", ".join(kwargs)})：{exc}；'
                              f'实际签名 {signature}')

    # ---------- ② 设置类 PAIR 命令的应答是 $PAIR001 ----------
    def test_pair_set_command_accepts_pair001_ack(self):
        """`PAIR432,1` 只回 `$PAIR001,432,0`，驱动必须认这条应答。

        真机实测：走通用 request() 时三条 PAIR 设置全部报"无应答（已重试）"，
        而模块其实已经执行 —— 用户看到的是"配置配不上"。
        """
        driver, channel = self._driver([('ok', '$PAIR001,432,0*3E')])
        ok, message, data = driver.config_rtcm(msm_type=7, persist=False)
        self.assertTrue(ok, message)
        self.assertEqual(channel.sent, ['PAIR432,1'], '一条命令只能发一次')
        self.assertEqual(data['mode'], 1)
        self.assertEqual(driver._last_pair['result'], 0)

    def test_pair_ack_result_code_is_translated(self):
        """结果码 4 = 参数错误，必须原样报出来，而不是"无应答"。"""
        driver, _channel = self._driver([('ok', '$PAIR001,432,4*3A')])
        ok, message, data = driver.config_rtcm(msm_type=4, persist=False)
        self.assertFalse(ok)
        self.assertIn('参数错误', message)
        self.assertEqual(data['pair_result'], 4)

    def test_pair_ack_with_other_command_id_is_ignored(self):
        """别的命令的 ACK 不能当成本次应答（查询 433 时先来的是 ACK，不是结果）。"""
        driver, channel = self._driver([('ok', '$PAIR001,433,0*3F')])
        ok, message, data = driver.set_rtcm_output(mode=0)
        self.assertFalse(ok)
        self.assertIn('无应答', message)
        self.assertEqual(channel.sent.count('PAIR432,0'), 1,
                         '同一条设置只能发一次，不能重发（会重复写 NVM）')

    def test_pair_set_uses_single_send(self):
        """成功时不能重发：同一条设置重发会重复写 NVM（闪存寿命）。"""
        driver, channel = self._driver([('ok', '$PAIR001,434,0*38')])
        ok, message, _data = driver.set_message_rate('RTCM3-1005', 1)
        self.assertTrue(ok, message)
        self.assertEqual(channel.sent, ['PAIR434,1'])

    # ---------- ③ 角色映射：SVIN 的 mode 是字符串 ----------
    def test_role_mapping_reads_string_mode(self):
        """`get_svin_config()` 返回的是 'fixed'/'survey_in'/'disabled'，不是 0/1/2。

        拿字符串和整数比较永远不成立，`get_mode()` 会恒为 'unknown'：
        真机上坐标明明已经写进模块，页面显示"模式未知"，VERIFY_MODE 必然判失败。
        """
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver

        self.assertEqual(LC29HBSDriver._role_from_svin({'mode': 'fixed'}), 'base')
        self.assertEqual(LC29HBSDriver._role_from_svin({'mode': 'survey_in'}), 'base',
                         '自收敛中也是基站：此刻正在按均值播 1005')
        self.assertEqual(LC29HBSDriver._role_from_svin({'mode': 'disabled'}), 'rover')
        self.assertEqual(LC29HBSDriver._role_from_svin({'mode': 2}), 'base',
                         '原始数字也要能认')

        driver, _channel = self._driver([
            ('ok', '$PQTMCFGSVIN,OK,2,0,0.0,-2001283.8896,5416711.8735,2699543.2568*5C')])
        ok, message, data = driver.get_mode()
        self.assertTrue(ok, message)
        self.assertEqual(data['mode'], 'base')
        self.assertEqual(data['svin_mode'], 'fixed', '原始 SVIN 模式要单独留一份')

    # ---------- ④ info() 取值 ----------
    def test_info_reports_rtcm_and_antenna_values(self):
        """`info()` 的 rtcm_mode/ant_pnt 必须从 fields 取，不能读 `data['mode']`。"""
        driver, _channel = self._driver([
            ('ok', '$PQTMVERNO,LC29HBSNR11A01S,2023/02/13,10:14:06*27'),
            ('ok', '$PQTMCFGSVIN,OK,2,0,0.0,-2001283.8896,5416711.8735,2699543.2568*5C'),
            ('ok', '$PAIR433,0*22'),
            ('ok', '$PAIR435,1*25'),
        ])
        ok, _message, data = driver.info()
        self.assertTrue(ok)
        self.assertEqual(data['rtcm_mode'], 0, 'PAIR433 的值在 fields[0]')
        self.assertEqual(data['ant_pnt'], 1)
        self.assertEqual(data['mode'], 'base')

    # ---------- ⑤ 1005 走 PAIR434，不是"逐报文速率" ----------
    def test_coord_msg_1005_maps_to_pair434(self):
        """流程层用 `set_message_rate('RTCM3-1005', 1)` 表达"播坐标"。

        本型号没有逐报文速率，只有 PAIR434 一个开关。驱动负责翻译，
        并且 `fields` 要按 `[名字, 速率]` 的形状回（备份/还原都取 fields[1]）。
        """
        driver, channel = self._driver([('ok', '$PAIR001,434,0*38')])
        ok, message, data = driver.set_message_rate('RTCM3-1005', 1)
        self.assertTrue(ok, message)
        self.assertEqual(channel.sent, ['PAIR434,1'])
        self.assertEqual(data['rate'], 1)

        driver, channel = self._driver([('ok', '$PAIR435,1*25')])
        ok, _message, data = driver.get_message_rate('RTCM3-1005')
        self.assertTrue(ok)
        self.assertEqual(data['fields'], ['RTCM3-1005', '1'])
        self.assertEqual(channel.sent, ['PAIR435'])

    def test_coord_msg_1006_is_rejected(self):
        """RTCM 输出表里只有 1005，1006 必须明确拒绝而不是静默不播。"""
        driver, channel = self._driver([])
        ok, message, _data = driver.set_message_rate('RTCM3-1006', 1)
        self.assertFalse(ok)
        self.assertIn('1005', message)
        self.assertEqual(channel.sent, [])

    # ---------- ⑥ 没有重启命令的型号 ----------
    def test_driver_declares_no_restart_needed(self):
        """文档里没有重启命令，而配置即时生效 —— 必须声明，否则标定必死。"""
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver

        self.assertFalse(LC29HBSDriver.RESTART_REQUIRED)
        self.assertIn('没有 PQTMSRR', LC29HBSDriver(None).reboot()[1])

    def test_restart_step_skips_when_not_required(self):
        """`RESTART_REQUIRED = False` 时流程层跳过软重启，而不是判整个标定失败。"""
        from rtkserver.calibration import writer as W

        class _Driver:
            RESTART_REQUIRED = False

            def reboot(self):
                raise AssertionError('不该调 reboot()')

        writer = W.CoordinateWriter(_Driver(), channel=None)
        writer._step_restart(None)            # 不抛异常 = 通过
        self.assertTrue(writer.result['restart']['skipped'])
        self.assertEqual(writer.result['steps'][-1]['stage'], 'RESTART')
        self.assertTrue(writer.result['steps'][-1]['ok'])

    def test_set_mode_base_is_a_noop_that_reports_truth(self):
        """本型号没有模式命令：`set_mode('base')` 不许改 SVIN，只能如实说明。

        照抄 LG290P 的"切基站 = 写 Mode=1"会把一台正在播固定坐标的模块
        改成重新自收敛 —— 回滚路径（先还原坐标、再切模式）会因此把刚还原好的
        坐标配置毁掉，而且播出去的 1005 还是旧均值。
        """
        fixed = ('ok', '$PQTMCFGSVIN,OK,2,0,0.0,-2001283.8896,5416711.8735,'
                        '2699543.2568*5C')
        driver, channel = self._driver([fixed])
        ok, message, data = driver.set_mode('base', persist=True, restart=True, verify=True)
        self.assertTrue(ok, message)
        self.assertEqual(channel.sent, ['PQTMCFGSVIN,R'], '只许查询，不许下发')
        self.assertTrue(data['noop'])

        # 还没有坐标时（SVIN Mode=0/1）：单基准站模块**不需要**"先切过去再写"，
        # 这里必须返回成功并标出 already_base_side，而不是报 base_needs_coords。
        # 理由：自收敛链上 SWITCH_MODE 在 SVIN_CFG 之前，此刻 Mode 必然是 disabled，
        # 按 verify 判失败会让全新模块走自收敛第一步就卡住；而这一步对 LC29H
        # 本来就没有可下发的命令，判失败毫无信息量。真正的动作在 set_fixed_ecef。
        driver, _channel = self._driver([
            ('ok', '$PQTMCFGSVIN,OK,0,0,0,0,0,0*00')])
        ok, message, data = driver.set_mode('base', persist=True, restart=False,
                                            verify=True)
        self.assertTrue(ok, message)
        self.assertTrue(data['already_base_side'])
        self.assertNotIn('error', data)

    def test_survey_in_accepts_distance_and_reports_it_ignored(self):
        """7 字段没有 `<Distance>`：接受但不下发，并如实标成 ignored。"""
        driver, channel = self._driver([
            ('ok', '$PQTMCFGSVIN,OK*70'),
            ('ok', '$PQTMCFGSVIN,OK,1,3600,15.0,0.0,0.0,0.0*00')])
        ok, message, data = driver.config_survey_in(3600, 15.0, 30.0,
                                                    persist=False, restart=False,
                                                    verify=True)
        self.assertTrue(ok, message)
        self.assertEqual(channel.sent, ['PQTMCFGSVIN,W,1,3600,15.0,0,0,0',
                                        'PQTMCFGSVIN,R'])
        self.assertEqual(data['ignored']['distance'], 30.0)


class TestCrossModelIsolation(unittest.TestCase):
    """一个型号的驱动改动，不许把另一个型号带歪。

    用户 2026-11-03 在真机上明确要求过：不同模块的命令集、默认档、生效方式
    都不一样，必须各自设计、不能混为一谈。这一组就是那条要求的机器可读版本：
    凡是"为了某个型号方便"而写进共用层的假设（默认档、字段形状、能力位、
    应答收取方式、是否重启），都会在这里报红。
    """

    # ---------- ① 波特率：每个型号一份，不许互相抄 ----------
    def test_default_baud_is_per_model(self):
        """LG290P=460800、LC29H(BS)=115200 —— 抄对方的值就白错一轮。"""
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        self.assertEqual(LG290PDriver.DEFAULT_BAUD, 460800,
                         'LG290P 出厂配在 460800，写成 115200 会让新模块先错一轮')
        self.assertEqual(LC29HBSDriver.DEFAULT_BAUD, 115200,
                         'LC29H(BS) 文档 §2.3.8 明写「默认波特率为 115200 bps」')
        # 真正被探测用的是 bauds()，不是常量本身，这里一并锁死
        self.assertEqual(LG290PDriver(None).bauds()[0], 460800)
        self.assertEqual(LC29HBSDriver(None).bauds()[0], 115200)

    def test_baud_tables_do_not_borrow_each_other(self):
        """各自的厂商档位表；兜底表必须同时覆盖两个派系。"""
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver
        from rtkserver.calibration.service import DETECT_BAUD_CANDIDATES

        # 4800 / 3000000 是 LC29H(BS) 的档，LG290P 探它们纯属白等
        self.assertNotIn(4800, LG290PDriver.BAUDS)
        self.assertNotIn(3000000, LG290PDriver.BAUDS)
        self.assertIn(3000000, LC29HBSDriver.BAUDS)

        # 兜底表（完全不知道型号时用）：少一个派系就等于整条派系探不到
        self.assertIn(115200, DETECT_BAUD_CANDIDATES)
        self.assertIn(460800, DETECT_BAUD_CANDIDATES)

    def test_probe_order_does_not_shrink_settable_set(self):
        """`PROBE_BAUDS` 只影响"去试哪些"，`BAUDS` 仍是"能设置哪些"的全集。"""
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver

        # 3 Mbps 是 UART2 的默认档，本机走 UART1，不该进探测序列
        self.assertIn(3000000, LC29HBSDriver.BAUDS)
        self.assertNotIn(3000000, LC29HBSDriver.PROBE_BAUDS)
        # 但它仍然是 `PAIR864` 的合法参数，写 UART2 时要用
        self.assertEqual(LC29HBSDriver(None).body('set_baud', baud=3000000),
                         'PAIR864,0,0,3000000',
                         '探测顺序收窄不许影响"能不能设置"')

    def test_auto_detect_keeps_both_camps_at_the_front(self):
        """不知道型号时，两派的默认档必须都在队首两位。

        整段拼接会让注册得早的型号独占队首：LG290P 的 460800 会被
        LC29H(BS) 的 3000000 / 921600 压在后面，插一块 LG290P 仍要错三轮。
        """
        from rtkserver.calibration.service import CalibrationService

        order = CalibrationService._baud_candidates(None)
        self.assertEqual(set(order[:2]), {115200, 460800}, order[:4])

    def test_blank_baud_follows_selected_module(self):
        """留空 + 选了型号 → 从**该型号的**默认档开始，而不是全局 115200。

        这是"默认档排第一"真正落地的地方：识别时首试档由 `_resolve_baud` 决定，
        它不看型号的话，选了 LG290P 仍会先白等一轮 115200 超时才轮到 460800。
        """
        from rtkserver.calibration.service import DEFAULT_BAUD, CalibrationService

        self.assertEqual(CalibrationService._resolve_baud(None, 'lg290p'),
                         (460800, False, ''))
        self.assertEqual(CalibrationService._resolve_baud('', 'lc29hbs'),
                         (115200, False, ''))
        # 没选型号 = 不押注，仍用全局兜底
        self.assertEqual(CalibrationService._resolve_baud(None)[0], int(DEFAULT_BAUD))
        # 手填值永远优先，且必须标成 explicit（否则失败后会按自动模式换档）
        self.assertEqual(CalibrationService._resolve_baud(115200, 'lg290p'),
                         (115200, True, ''))

    # ---------- ② 运行时状态：不共享对象 ----------
    def test_runtime_caps_are_per_instance(self):
        """`probe_caps()` 会按真实应答改写能力位，共享一份字典就是灾难。"""
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        lc, lg = LC29HBSDriver(None), LG290PDriver(None)
        self.assertIsNot(lc.runtime_caps, lg.runtime_caps)
        self.assertIsNot(LC29HBSDriver.CAPS, LG290PDriver.CAPS)

        before = dict(lg.runtime_caps)
        lc.runtime_caps['svin'] = not lc.runtime_caps.get('svin')
        lc.runtime_caps['ant_info'] = True
        self.assertEqual(lg.runtime_caps, before, '改 A 的能力位不许影响 B')
        self.assertTrue(LC29HBSDriver.CAPS.get('svin'),
                        '类上的 CAPS 也不许被实例改写（否则下次新建的实例就带错能力）')

    def test_pair_ack_override_stays_on_its_own_class(self):
        """LC29H(BS) 覆写 `request()` 收 `$PAIR001` —— 只对它自己生效。"""
        from rtkserver.calibration.drivers.base import BaseDriver
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        self.assertIsNot(LC29HBSDriver.request, BaseDriver.request)
        self.assertIs(LG290PDriver.request, BaseDriver.request,
                      'LG290P 的应答名就是命令名，不需要也不许套用 PAIR001 逻辑')

    # ---------- ③ 流程层：默认值兜底，别的型号行为不变 ----------
    def test_lg290p_still_goes_through_restart(self):
        """`RESTART_REQUIRED` 默认 True —— LG290P 必须照旧重启。"""
        from rtkserver.calibration import writer as W

        class _Driver:
            RESTART_REQUIRED = True

            def __init__(self):
                self.calls = 0

            def reboot(self):
                self.calls += 1
                return True, 'PGK 已发出', {'boot': 'PQTMVER'}

        driver = _Driver()
        writer = W.CoordinateWriter(driver, channel=None)
        writer._step_restart(None)
        self.assertEqual(driver.calls, 1,
                         'LG290P 不能因为 LC29H(BS) 不用重启就跟着跳过')
        self.assertNotIn('skipped', writer.result.get('restart') or {})
        self.assertEqual(writer.result['steps'][-1]['stage'], 'RESTART')

    def test_lg290p_rtcm_readback_translates_seven_fields(self):
        """LG290P 把七字段翻译成语义化名字，同时原样保留 `fields` 供还原回放。

        这条测试防的是跨型号串味：某个型号改字段形状时把另一个型号带歪。
        所以它固定的是三件事 ——
        1. `fields` 仍是 7 项、`fields[0]` 仍是 `'4'`（还原走 `len(fields)==7`）；
        2. 翻译出来的 `msm_type` 与 `fields[0]` 一致（不能各说各的）；
        3. 只查询不下发。
        """
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        class _Chan:
            def __init__(self):
                self.sent = []

            def send(self, body, timeout=None, retries=None, interval=None):
                self.sent.append(body)
                return 'ok', '$PQTMCFGRTCM,OK,4,0,-90,07,06,1,0*2B'

        channel = _Chan()
        driver = LG290PDriver(channel)
        ok, _message, data = driver.read_rtcm_config()
        self.assertTrue(ok)

        self.assertEqual(data['fields'], ['4', '0', '-90', '07', '06', '1', '0'],
                         '原始七字段必须完整保留，还原要原样回放')
        self.assertEqual(data['msm_type'], 4,
                         '翻译值必须与 fields[0] 一致')
        self.assertEqual(data['coord_bits'], 7)
        self.assertEqual(data['msm_bits'], 6)
        self.assertEqual(data['enabled'], ['1005', '1006', '1007'],
                         '07 是位图，0b111 表示 1005/1006/1007 三条全开')
        self.assertEqual(channel.sent, ['PQTMCFGRTCM,R'], '只查询，不下发')

    def test_lg290p_still_fans_out_constellations(self):
        """空星座表是 LC29H(BS) 的声明，不许变成 LG290P 的默认。"""
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        self.assertIsNotNone(LG290PDriver.MSM_CONSTELLATIONS,
                             'None 会退化成只开 107X（2026-11-02 真机踩过）')
        self.assertGreaterEqual(len(LG290PDriver.MSM_CONSTELLATIONS), 2,
                                'LG290P 要多星座逐条下发，不能只开一路')
        self.assertEqual(LC29HBSDriver.MSM_CONSTELLATIONS, (),
                         'LC29H(BS) 只有一个全局开关，空表是"不逐星座下发"')

    # ---------- ④ 命令集：两张表互不重叠 ----------
    def test_command_tables_are_separate(self):
        """谁的专有命令就只留在谁的表里。"""
        from rtkserver.calibration.drivers.quectel_lc29hbs import LC29HBSDriver
        from rtkserver.calibration.drivers.quectel_lg290p import LG290PDriver

        self.assertIsNot(LG290PDriver.CMD, LC29HBSDriver.CMD)

        lc_bodies = ' '.join(LC29HBSDriver.CMD.values())
        for name in ('PQTMCFGRCVRMODE', 'PQTMCFGRTCM', 'PQTMSRR',
                     'PQTMSN', 'PQTMCFGUART', 'PQTMCFGFIXRATE'):
            self.assertNotIn(name, lc_bodies,
                             f'LC29H(BS) 的文档里没有 {name}，不许从 LG290P 照抄')

        lg_bodies = ' '.join(LG290PDriver.CMD.values())
        for name in ('PAIR432', 'PAIR433', 'PAIR434', 'PAIR864'):
            self.assertNotIn(name, lg_bodies,
                             f'LG290P 用 PQTMCFGRTCM/PQTMCFGUART，不是 {name}')

    def test_resolve_baud_callers_actually_have_a_module(self):
        """`_resolve_baud(baud, module)` 的调用点，所在函数必须真有 `module` 变量。

        照着真踩的坑写的：批量把 `_resolve_baud(baud)` 改成 `(baud, module)` 时，
        `start_restore()` 根本没有 `module` 参数 —— 它只有在用户真的点「一键还原」
        并且波特率留空时才会 NameError，单元测试从来没走过那条路。
        用 AST 静态查，比"记得补测试"可靠。
        """
        import ast

        path = os.path.join(REPO_ROOT, 'rtkserver', 'calibration', 'service.py')
        with io.open(path, encoding='utf-8') as handle:
            tree = ast.parse(handle.read())

        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            params = {a.arg for a in node.args.args} | {
                a.arg for a in node.args.kwonlyargs}
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if not (isinstance(func, ast.Attribute)
                        and func.attr == '_resolve_baud'):
                    continue
                self.assertGreaterEqual(len(call.args), 2,
                                        f'{node.name}() 调用 _resolve_baud 没传型号')
                arg = call.args[1]
                if isinstance(arg, ast.Name):
                    checked += 1
                    self.assertIn(arg.id, params,
                                  f'{node.name}() 里用了 {arg.id}，但它不是参数')
        self.assertGreater(checked, 0, '一个调用点都没找到，这条断言已经失效了')

    def test_frontend_checks_against_settable_set(self):
        """前端"手填值合不合法"必须查 `set_bauds`（可设置全集），不是探测序列。

        探测序列里没有 LC29H(BS) 的 3000000（UART2 档），拿它做校验会把一个
        合法输入判成"不在官方档位"，用户只能被迫改成一个"看起来合法"的值。
        """
        with io.open(os.path.join(REPO_ROOT, 'static', 'calibration.js'),
                     encoding='utf-8') as handle:
            src = handle.read()

        self.assertIn('function currentModuleLegalBauds()', src)
        self.assertIn('set_bauds', src)
        # offlist 提示前那几行必须用全集表
        start = src.index('cal.baud.offlist')
        block = src[start - 300:start]
        self.assertIn('allowed', block,
                      'offlist 校验要用 currentModuleLegalBauds()，不能用探测序列')


class TestWriterUsesNormalizedContracts(unittest.TestCase):
    """流程层不许只看"某个型号才有"的字段（真机联调时在 MSM 回读处炸掉）。"""

    def _writer_source(self):
        with io.open(os.path.join(REPO_ROOT, 'rtkserver', 'calibration', 'writer.py'),
                     encoding='utf-8') as handle:
            return handle.read()

    def test_msm_readback_prefers_msm_type(self):
        """LC29H(BS) 的 read_rtcm_config 返回空 fields，只能靠 msm_type 比对。"""
        src = self._writer_source()
        start = src.index("ok2, message2, readback = self.driver.read_rtcm_config()")
        block = src[start:start + 1200]
        self.assertIn("readback.get('msm_type')", block,
                      '必须优先用驱动翻译好的 msm_type')
        self.assertNotIn("if not fields or _to_int(fields[0]) != msm", block,
                         '只看 fields[0] 会让"没有七字段"的型号永远校验不过')

    def test_empty_constellation_list_means_global_switch(self):
        """`MSM_CONSTELLATIONS = ()` 是"本型号只有一个全局开关"，不能用 `or` 兜掉。

        2026-11 改造后这里不再走 `getattr` 探测（基类已声明该常量），
        所以断言改看**取值方式**：必须显式判 `is None`，绝不能用
        `x or ('RTCM3-107X',)` —— 后者会把空元组（有意义的"全局开关"）
        和一个真没声明的驱动混为一谈。这里的 ``None` 只兜 `driver.X = None`
        这种手写覆盖，不属于探测。
        """
        src = self._writer_source()
        start = src.index('constellations = self.driver.MSM_CONSTELLATIONS')
        block = src[start:start + 260]
        self.assertIn('is None', block)
        self.assertNotIn("or ('RTCM3-107X',)", block)
        self.assertNotIn('getattr(self.driver', block,
                         '直接读类常量即可：基类已声明 MSM_CONSTELLATIONS，'
                         'getattr 探测会掩盖"驱动没声明"这个事实')


class TestMeasureOnlyRequiresRover(unittest.TestCase):
    """「只测坐标」必须要求模块能当流动站（真机反馈 #3）。

    单基准站模块（LC29H(BS)）收不到改正数，接上 CORS 也只能给单点解。
    让它跑满整档采集再吐一个米级坐标是最坏的一种"成功"：用户会以为
    标定成功了，然后把这个米级坐标写进基准站。
    """

    def _read(self, *parts):
        with io.open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as handle:
            return handle.read()

    def test_service_rejects_measure_only_on_base_only_module(self):
        from rtkserver.calibration import service
        ok, message, _status = service.start_cors(
            port='/dev/ttyUSB0', host='120.253.239.161', cors_port=8002,
            mountpoint='RTCM33_GRCEJ', user='u', password='p',
            module='lc29hbs', measure_only=True)
        self.assertFalse(ok, message)
        self.assertIn('LC29H', message)
        self.assertIn('流动站', message)

    def test_unknown_module_still_allowed(self):
        """未指定型号必须放行：未知模块完全可能就是一台现成的流动站。"""
        src = self._read('rtkserver', 'calibration', 'service.py')
        block = src[src.index('# 「只测坐标」靠 CORS/NTRIP'):]
        block = block[:block.index('plan = WritePlan.build_cors')]
        self.assertIn('_module_caps(module)', block)
        # 判据不能是裸的 command_count：UBX 驱动（ZED-F9P）CMD 为空但完全可写。
        self.assertIn('_has_command_set(info)', block,
                      '必须在"已知型号且能下发命令"时才判 dual_mode')
        self.assertIn("not info['caps'].get('dual_mode')", block)

    def test_frontend_locks_measure_for_base_only(self):
        """单模 / 流动站模块必须锁掉「仅测量」，理由按角色说。

        判定依据换成了 `methods_of()` 的允许表（`selectedMethods()`），而不是
        老代码里那个 `!mod.dual_mode` 布尔 —— 三档角色（dual/single/rover）
        塞不进一个布尔里，而且前后端共用一张表才能保证"能点就一定不会 400"。
        """
        js = self._read('static', 'calibration.js')
        block = js[js.index('function computeLocks()'):]
        block = block[:block.index('function applyCapabilities()')]
        self.assertIn('locks.external_rover', block,
                      '只测坐标必须在 computeLocks 里被锁')
        self.assertIn("allowed.indexOf('external_rover') < 0", block,
                      '锁的条件是"角色的允许方式表里没有仅测量"')
        self.assertIn('cal.lock.measure_role', block,
                      '锁文案要带上角色名，用户才知道该换什么模块')
        text = self._read('static', 'i18n.js')
        self.assertEqual(text.count("'cal.lock.measure_role'"), 2,
                         '中英文各一条')


class TestReuseHistoryAnyModule(unittest.TestCase):
    """「复用坐标」不按模块/SN 过滤（真机反馈 #4）。

    坐标描述的是天线相位中心这个物理点。真机上先用 LG290P 测出坐标，
    换插 LC29H(BS) 后想复用，旧逻辑按 module_sn 过滤 → 判定"本机没有历史坐标"
    → 复用入口直接消失。同一根天线、同一个安装位置，只是换了块模块。
    """

    def _read(self, *parts):
        with io.open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as handle:
            return handle.read()

    def test_options_returns_latest_record_without_sn_filter(self):
        source = self._read('rtkserver', 'web', 'api_calibration.py')
        body = source[source.index('def api_calibration_options():'):]
        body = body[:body.index('@login_required')]
        self.assertIn('reuse_history', body, '必须返回复用来源')
        self.assertNotIn('module_sn=', body, '复用来源不得按 module_sn 过滤')
        self.assertNotIn("request.args.get('sn')", body,
                         '不得再要求前端传 SN 才能拿到复用坐标')

    def test_frontend_uses_reuse_history(self):
        js = self._read('static', 'calibration.js')
        self.assertIn('reuse_history', js)
        self.assertIn('STATE.reuseHistory', js)
        self.assertNotIn('STATE.ownHistory', js)
        self.assertNotIn('own_history', js)
        build = js[js.index('function buildPayload()'):]
        build = build[:build.index('function validate(')]
        self.assertIn('payload.reuse_arp', build, 'CORS 方式下必须发 reuse_arp')

    def test_history_item_shows_both_coordinate_systems(self):
        """列表要同时给出经纬高与 ECEF —— 换模块后核对"是不是同一根天线"只能靠经纬度。"""
        js = self._read('static', 'calibration.js')
        block = js[js.index('function renderHistoryList()'):]
        block = block[:block.index('function fillReuseHistory')]
        self.assertIn('record.lat', block)
        self.assertIn('record.lon', block)
        self.assertIn('record.ecef_x', block)


class TestRecordGeoBothWays(unittest.TestCase):
    """坐标落库要"两边都有"（真机反馈 #5）。

    不同型号/不同来源给的坐标形态不一样：有的只给 ECEF（1005），
    有的只给经纬高（NMEA、CORS 成果表、厂家界面）。库里必须两组都存：
    ECEF 是真正写进模块、播发出去的那组，经纬高是人能核对位置的那组。
    """

    def setUp(self):
        import tempfile
        from rtkserver.calibration import store
        self.store = store
        self._orig = store.CALIBRATION_DB
        self._tmp = tempfile.mkdtemp(prefix='2rtk-geo-')
        store.CALIBRATION_DB = os.path.join(self._tmp, 'c.db')
        store.init_db()

    def tearDown(self):
        self.store.CALIBRATION_DB = self._orig

    def test_latlon_only_fills_ecef(self):
        self.store.insert_record({'module_sn': 'A', 'method': 'manual',
                                  'lat': 25.20352374, 'lon': 110.27748337,
                                  'height': 138.316})
        row = self.store.list_records(limit=1)[0]
        self.assertIsNotNone(row['ecef_x'], '只有经纬高时必须补出 ECEF')
        self.assertAlmostEqual(row['ecef_x'], -2001277.3805, delta=0.002)
        self.assertAlmostEqual(row['ecef_y'], 5416696.8658, delta=0.002)
        self.assertAlmostEqual(row['ecef_z'], 2699549.3066, delta=0.002)
        self.assertEqual(row['arp_x'], row['ecef_x'],
                         '这组数就是要写进模块的值，arp_* 也必须补齐')

    def test_ecef_only_fills_latlon(self):
        self.store.insert_record({'module_sn': 'B', 'method': 'manual',
                                  'ecef_x': -2001277.3805,
                                  'ecef_y': 5416696.8658,
                                  'ecef_z': 2699549.3072})
        row = self.store.list_records(limit=1)[0]
        self.assertAlmostEqual(row['lat'], 25.20352374, places=7)
        self.assertAlmostEqual(row['lon'], 110.27748337, places=7)
        self.assertAlmostEqual(row['height'], 138.316, delta=0.01)

    def test_roundtrip_error_is_sub_millimetre(self):
        """往返误差必须小到不影响 RTK：0.1 mm 级。

        量化到 4 位小数（0.1 mm）是刻意的：再细也播不出去
        （RTCM 1005 的分辨率就是 0.1 mm），反而让库里出现对不齐的尾数。
        """
        from rtkserver.calibration.geo import (ecef_to_geodetic,
                                               geodetic_to_ecef,
                                               max_axis_delta)
        lat, lon, h = 25.20352374, 110.27748337, 138.316
        x, y, z = geodetic_to_ecef(lat, lon, h)
        lat2, lon2, h2 = ecef_to_geodetic(round(x, 4), round(y, 4), round(z, 4))
        x2, y2, z2 = geodetic_to_ecef(lat2, lon2, h2)
        delta = max_axis_delta(round(x, 4), round(y, 4), round(z, 4), x2, y2, z2)
        self.assertLess(delta, 0.001, f'往返误差 {delta} m 超过 1 mm')

    def test_conflicting_pair_is_corrected_by_ecef(self):
        """两组对不上时以 ECEF 为准（它是真正播出去的那组），并留下告警。"""
        self.store.insert_record({'module_sn': 'C', 'method': 'manual',
                                  'ecef_x': -2001277.3805,
                                  'ecef_y': 5416696.8658,
                                  'ecef_z': 2699549.3072,
                                  'lat': 30.0, 'lon': 120.0})
        row = self.store.list_records(limit=1)[0]
        self.assertAlmostEqual(row['lat'], 25.20352374, places=7)
        self.assertAlmostEqual(row['lon'], 110.27748337, places=7)


class TestPortScanInlineFeedback(unittest.TestCase):
    """扫描串口的候选与结果必须就地显示（真机反馈 #1）。

    真机现象：换了 USB 口、换插 LC29H(BS) 后点「扫描串口」看起来"没反应"。
    原因是候选只写进了 datalist（要再点一次输入框才弹），而失败信息写在
    页面最底部的全局错误框里，离那根输入框隔着好几屏。
    """

    def _read(self, *parts):
        with io.open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as handle:
            return handle.read()

    def test_template_has_inline_chip_container(self):
        text = self._read('templates', 'calibration.html')
        self.assertIn('id="cal-port-chips"', text)
        self.assertIn('id="cal-port-hint"', text)

    def test_scan_renders_chips_and_hint(self):
        js = self._read('static', 'calibration.js')
        self.assertIn('function renderPortChips', js)
        self.assertIn('function setPortHint', js)
        block = js[js.index('function scanPorts()'):]
        block = block[:block.index('function readModuleInfo')]
        self.assertIn('renderPortChips(ports)', block,
                      '扫描结果必须就地渲染，不能只塞 datalist')
        self.assertIn('setPortHint(', block,
                      '结果/失败原因要写在输入框下面，不能只写全局框')
        self.assertIn("r.ok", block, 'HTTP 非 2xx 必须走失败分支，不能当成空列表')

    def test_single_port_is_filled_automatically(self):
        js = self._read('static', 'calibration.js')
        block = js[js.index('function scanPorts()'):]
        block = block[:block.index('function readModuleInfo')]
        self.assertIn('ports.length === 1', block,
                      '只有一个口时直接填上：现场换完 USB 口最想看到"它认到了"')

    def test_api_separates_enumeration_failure_from_empty(self):
        src = self._read('rtkserver', 'web', 'api.py')
        block = src[src.index('def api_serial_ports():'):]
        block = block[:block.index('@login_required')]
        self.assertIn("'ok': not error", block,
                      '枚举失败与"没有设备"必须能分开')
        self.assertIn("'count': len(items)", block)


class TestSchemaMigrationSelfHeal(unittest.TestCase):
    """库迁移必须能从中断处自愈。

    SQLite 里 DDL 不受 Python 隐式事务保护（sqlite3 只在 DML 前开事务，
    `ALTER TABLE` 是立即提交的）。所以"迁移跑到一半掉电"会留下
    「列已经加上、`PRAGMA user_version` 还停在旧值」的半成品库。
    如果迁移不是幂等的，下次启动重跑同一步就会撞 `duplicate column name`，
    `init_db()` 抛异常，这台设备的标定库**永久打不开** —— 数据还在，
    但页面上的标定、历史、备份全部失效，且没有任何界面能修。
    """

    def setUp(self):
        import sqlite3
        import tempfile
        from rtkserver.calibration import store
        self.store = store
        self.sqlite3 = sqlite3
        self._orig = store.CALIBRATION_DB
        self._tmp = tempfile.mkdtemp(prefix='2rtk-migrate-')
        self.db = os.path.join(self._tmp, 'calibration.db')
        store.CALIBRATION_DB = self.db

    def tearDown(self):
        self.store.CALIBRATION_DB = self._orig

    def _make_v1_db(self, records=1):
        """造一个真的 v1 库：只有基础表 + 历史记录，版本号停在 1。"""
        conn = self.sqlite3.connect(self.db)
        for ddl in self.store._DDL_V1:
            conn.execute(ddl)
        for i in range(records):
            conn.execute(
                'INSERT INTO calibration_records '
                '(module_sn, module_model, method, ecef_x, ecef_y, ecef_z, '
                ' lat, lon, height, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (f'SN{i:03d}', 'lg290p', 'cors', 1.0, 2.0, 3.0,
                 30.5, 114.3, 50.0, f'2026-11-0{i + 1} 10:00:00'))
        conn.execute('PRAGMA user_version = 1')
        conn.commit()
        conn.close()

    def _cols(self):
        conn = self.sqlite3.connect(self.db)
        try:
            return [r[1] for r in conn.execute('PRAGMA table_info(calibration_records)')]
        finally:
            conn.close()

    def _version(self):
        conn = self.sqlite3.connect(self.db)
        try:
            return conn.execute('PRAGMA user_version').fetchone()[0]
        finally:
            conn.close()

    def _count(self):
        conn = self.sqlite3.connect(self.db)
        try:
            return conn.execute('SELECT COUNT(*) FROM calibration_records').fetchone()[0]
        finally:
            conn.close()

    def test_clean_v1_upgrades_to_current(self):
        self._make_v1_db()
        self.store.init_db()
        self.assertEqual(self._version(), self.store.SCHEMA_VERSION)
        self.assertEqual(self._count(), 1, '升级不能丢掉历史记录')

    def test_half_applied_migration_recovers(self):
        """半成品库（v2 只加了 4 列、版本号没动）必须能自己走完。"""
        self._make_v1_db()
        conn = self.sqlite3.connect(self.db)
        for ddl in self.store._DDL_V2[:4]:
            conn.execute(ddl)          # 模拟掉电前已提交的那几条
        conn.commit()
        conn.close()

        self.store.init_db()           # 这里是原来的崩溃点
        self.assertEqual(self._version(), self.store.SCHEMA_VERSION)
        self.assertEqual(self._count(), 1, '自愈过程不能丢数据')
        cols = self._cols()
        for need in ('apc_x', 'nvm_state', 'task_id', 'arp_x',
                     'gga_guard_used', 'kalman_std_e', 'svin_valid'):
            self.assertIn(need, cols, f'自愈后仍缺列 {need}')

    def test_init_db_is_idempotent(self):
        self._make_v1_db()
        for _ in range(3):
            self.store.init_db()
        self.assertEqual(self._version(), self.store.SCHEMA_VERSION)
        self.assertEqual(self._count(), 1)

    def test_downgrade_does_not_destroy_data(self):
        """用户先跑新版本又回退：只告警，不重建、不删列。"""
        self._make_v1_db()
        self.store.init_db()
        conn = self.sqlite3.connect(self.db)
        conn.execute('PRAGMA user_version = 99')
        conn.commit()
        conn.close()

        self.store.init_db()                     # 不该抛
        self.assertEqual(self._version(), 99, '不能把新版本号改小')
        self.assertEqual(self._count(), 1, '回退场景下绝不能丢数据')

    def test_migration_statements_tolerate_existing_columns(self):
        """`_exec_ddl` 对"列已存在"和"表已存在"都必须静默跳过。"""
        conn = self.sqlite3.connect(self.db)
        try:
            conn.execute('CREATE TABLE t_demo (a INTEGER)')
            conn.commit()
            self.store._exec_ddl(conn, 'ALTER TABLE t_demo ADD COLUMN b INTEGER')
            self.store._exec_ddl(conn, 'ALTER TABLE t_demo ADD COLUMN b INTEGER')  # 再来一次
            self.store._exec_ddl(conn, 'CREATE TABLE IF NOT EXISTS t_demo (a INTEGER)')
            cols = [r[1] for r in conn.execute('PRAGMA table_info(t_demo)')]
            self.assertEqual(cols, ['a', 'b'])
        finally:
            conn.close()

    def test_real_errors_still_raise(self):
        """不能把所有 OperationalError 都吞掉，否则真错误会被静默掩盖。"""
        conn = self.sqlite3.connect(self.db)
        try:
            with self.assertRaises(self.sqlite3.OperationalError):
                self.store._exec_ddl(conn, 'ALTER TABLE no_such_table ADD COLUMN c INTEGER')
        finally:
            conn.close()


class TestManualHeightRequired(unittest.TestCase):
    """手动写坐标时高程是必填，不能静默按 0（海平面）处理。

    用户手上有经纬度、但不知道椭球高是很常见的（CORS 成果表常常只给平面位置）。
    旧行为是三处都补 0：前端 `parseFloat(...) || 0`、接口 `body.get('height') or 0.0`、
    换算 `float(height or 0.0)`。结果是写进模块的 ECEF 落在椭球面上，
    内陆站整体低几十米；而 1005 校验查不出来 —— 它只核对"写入 == 播发"，
    两个值错得一模一样。必须让用户补这个数。
    """

    def _read(self, *parts):
        with open(os.path.join(REPO_ROOT, *parts), encoding='utf-8') as handle:
            return handle.read()

    def test_conversion_rejects_missing_height(self):
        from rtkserver.calibration.writer import _ecef_from_geodetic
        for missing in (None, ''):
            with self.assertRaises(ValueError) as ctx:
                _ecef_from_geodetic(30.5, 114.3, missing)
            self.assertIn('高程', str(ctx.exception), '错误信息要说清楚缺什么')

    def test_conversion_rejects_nan_height(self):
        from rtkserver.calibration.writer import _ecef_from_geodetic
        with self.assertRaises(ValueError):
            _ecef_from_geodetic(30.5, 114.3, float('nan'))

    def test_conversion_rejects_non_numeric_height(self):
        from rtkserver.calibration.writer import _ecef_from_geodetic
        with self.assertRaises(ValueError) as ctx:
            _ecef_from_geodetic(30.5, 114.3, 'xyz')
        # 不能把 Python 的原始报错原样吐给用户
        self.assertNotIn('could not convert', str(ctx.exception))

    def test_explicit_zero_height_is_still_accepted(self):
        """显式写 0 是"我说了在海平面"，这个意图要尊重；区别在于"没说"。"""
        from rtkserver.calibration.writer import _ecef_from_geodetic
        x, y, z = _ecef_from_geodetic(30.5, 114.3, 0)
        self.assertAlmostEqual(x, -2263466.1599, delta=0.01)

    def test_plan_build_requires_height(self):
        from rtkserver.calibration.writer import WritePlan
        with self.assertRaises(ValueError):
            WritePlan.build(lat=30.5, lon=114.3)          # 不传 height
        plan = WritePlan.build(lat=30.5, lon=114.3, height=50)
        self.assertIsNotNone(plan.arp)

    def test_api_rejects_lat_lon_without_height(self):
        """经纬度给了、高程没给 → 400，且错误文案点明是高程。

        `module='lg290p'` 不能省：不带型号的口按「未知模块 → 流动站」处理，
        流动站不支持 `manual`，角色闸门会先返回"不支持写入已知坐标"，
        就测不到高程校验这一层了。
        """
        from rtkserver.web.app import create_app
        app = create_app()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['user'] = 'admin'
            sess['logged_in'] = True
        for body in ({'method': 'manual', 'port': '/dev/null',
                      'module': 'lg290p', 'lat': 30.5, 'lon': 114.3},
                     {'method': 'manual', 'port': '/dev/null',
                      'module': 'lg290p', 'lat': 30.5, 'lon': 114.3, 'height': None},
                     {'method': 'manual', 'port': '/dev/null',
                      'module': 'lg290p', 'lat': 30.5, 'lon': 114.3, 'height': ''}):
            r = client.post('/api/calibration/start', json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn('高程', r.get_json().get('error') or '', body)

    def test_store_does_not_fabricate_ecef_without_height(self):
        """落库时高度未知就不要造 ECEF —— 这条记录会被当成"可复用的权威坐标"。"""
        import tempfile
        from rtkserver.calibration import store
        orig = store.CALIBRATION_DB
        try:
            store.CALIBRATION_DB = os.path.join(
                tempfile.mkdtemp(prefix='2rtk-h-'), 'c.db')
            store.init_db()
            store.insert_record({'module_sn': 'H1', 'method': 'manual',
                                 'lat': 30.5, 'lon': 114.3})
            row = store.list_records(limit=1)[0]
            self.assertIsNone(row['ecef_x'],
                              '没高程就不能反算 ECEF（h=0 会让整站低几十米）')
            self.assertIsNone(row['height'])
        finally:
            store.CALIBRATION_DB = orig

    def test_consistent_pair_without_height_is_not_flagged(self):
        """经纬度与 ECEF 本来一致、只是没填高程时，不该报"不一致"。"""
        import logging
        import tempfile
        from rtkserver.calibration import store
        from rtkserver.calibration.geo import geodetic_to_ecef
        orig = store.CALIBRATION_DB
        try:
            store.CALIBRATION_DB = os.path.join(
                tempfile.mkdtemp(prefix='2rtk-h2-'), 'c.db')
            store.init_db()
            x, y, z = geodetic_to_ecef(30.5, 114.3, 50.0)

            caught = []

            class _Grab(logging.Handler):
                def emit(self, record):
                    caught.append(record.getMessage())

            handler = _Grab(level=logging.WARNING)
            root = logging.getLogger()
            root.addHandler(handler)
            try:
                store.insert_record({'module_sn': 'H2', 'method': 'manual',
                                     'lat': 30.5, 'lon': 114.3,
                                     'ecef_x': x, 'ecef_y': y, 'ecef_z': z})
            finally:
                root.removeHandler(handler)

            self.assertFalse([m for m in caught if '不一致' in m],
                             '一组一致的数据不该被判成冲突（h=0 反算会得出假偏差）')
            row = store.list_records(limit=1)[0]
            self.assertAlmostEqual(row['height'], 50.0, delta=0.01,
                                   msg='高度应由 ECEF 反算补上')
        finally:
            store.CALIBRATION_DB = orig

    def test_frontend_does_not_coalesce_height_to_zero(self):
        js = self._read('static', 'calibration.js')
        self.assertNotIn("parseFloat($('cal-h').value) || 0", js,
                         "空高程不能变成 0：0 是海平面，不是「没填」")
        self.assertIn('cal.err.need_height', js, '前端要在提交前就拦住')

    def test_error_message_is_translated(self):
        i18n = self._read('static', 'i18n.js')
        self.assertEqual(i18n.count("'cal.err.need_height'"), 2,
                         '中英文各要一条')

    def test_reuse_skips_records_without_coordinate(self):
        """「复用历史坐标」只能挑带完整 ECEF 的那条。

        现在允许落"只有经纬度、没有高程"的记录（不反算 ECEF）。如果复用
        直接拿最近一条，用户点完会收到"请填写 ECEF 或经纬高" —— 库里明明
        有别的可用记录，看起来就像按钮坏了。
        """
        import tempfile
        from rtkserver.calibration import store
        orig = store.CALIBRATION_DB
        try:
            store.CALIBRATION_DB = os.path.join(
                tempfile.mkdtemp(prefix='2rtk-reuse-'), 'c.db')
            store.init_db()
            # 先存一条完整的
            store.insert_record({'module_sn': 'R1', 'method': 'cors',
                                 'ecef_x': -2263466.16, 'ecef_y': 5013021.86,
                                 'ecef_z': 3218254.55,
                                 'created_at': '2026-11-01 10:00:00'})
            # 再存一条没有 ECEF 的（时间更新，会排在前面）
            store.insert_record({'module_sn': 'R2', 'method': 'manual',
                                 'lat': 30.5, 'lon': 114.3,
                                 'created_at': '2026-11-02 10:00:00'})
            self.assertEqual(store.list_records(limit=1)[0]['module_sn'], 'R2',
                             '前提：列表最近一条确实是没有坐标的那条')
            pick = store.get_latest_with_coord()
            self.assertIsNotNone(pick)
            self.assertEqual(pick['module_sn'], 'R1',
                             '复用必须跳过没有 ECEF 的记录')
        finally:
            store.CALIBRATION_DB = orig


#: 各型号的**期望**角色/能力，作为"模块标记"的唯一期望值表。
#:
#: 这张表存在的原因是 `ROLE` / `DUAL_MODE` / `CAPS['dual_mode']` 是三个
#: 独立字段，改了前两个不会自动改第三个，而运行时三处逻辑（service 的
#: "只测坐标"闸门、writer 的纯基准站拦截、writer 的兜底切模）**直接读
#: 后两个**、不从 `ROLE` 折算。UM960L 就是这么漏的：子类只覆盖了
#: `ROLE='single'`，继承来的 `DUAL_MODE=True` 留在原地，于是"页面上
#: 只给自收敛+写坐标，后端却放行只测坐标"，点下去必然撞 `MODE ROVER`
#: 不存在。所以凡是动角色，必须四个字段一起对，这张表就是那道校验。
_EXPECTED_ROLE_TABLE = {
    # id:       (role,     dual_mode, svin,  svin_has_status)
    'lg290p':   ('dual',   True,      True,  True),
    'lc29hbs':  ('single', False,     True,  True),
    'zed_f9p':  ('dual',   True,      True,  True),
    'um960':    ('dual',   True,      True,  False),
    'um960l':   ('single', False,     True,  False),
    'um980':    ('dual',   True,      True,  False),
    'um982':    ('dual',   True,      True,  False),
}


class TestModuleRoleMarking(unittest.TestCase):
    """模块标记核查（用户指令：LG290P 双模 / LC29HBS 单模 / ZEDF9P 双模 / UM 系列双模）。

    这里锁的是**四个字段的一致性**，不是某个具体值 —— 具体值放
    `_EXPECTED_ROLE_TABLE`，改型号能力时只改那一张表。
    """

    def _registry(self):
        from rtkserver.calibration.drivers import driver_manager
        driver_manager.load_builtin_drivers()
        return driver_manager._REGISTRY

    def test_role_and_dual_mode_agree(self):
        """`ROLE` 与 `DUAL_MODE` / `CAPS['dual_mode']` 必须同值。

        这是三个字段唯一的硬约束：`role == 'dual'` ⟺ `dual_mode is True`。
        （反向不成立 —— single 和 rover 的 `dual_mode` 都是 False，
        光看布尔分不出这两种，所以判定"是哪种"仍然只能看 `ROLE`。）
        """
        for key, cls in sorted(self._registry().items()):
            caps = getattr(cls, 'CAPS', {}) or {}
            role = getattr(cls, 'ROLE', None)
            dual_attr = getattr(cls, 'DUAL_MODE', None)
            dual_cap = caps.get('dual_mode')
            with self.subTest(module=key):
                self.assertIn(role, ('dual', 'single', 'rover'),
                              f'{key} 的 ROLE 必须是三档之一')
                self.assertEqual(
                    dual_attr, role == 'dual',
                    f'{key}: ROLE={role!r} 但 DUAL_MODE={dual_attr!r}。'
                    ' 运行时 service/writer 直接读 DUAL_MODE，不从 ROLE 折算。')
                self.assertEqual(
                    dual_cap, role == 'dual',
                    f"{key}: ROLE={role!r} 但 CAPS['dual_mode']={dual_cap!r}。"
                    ' 三处运行时逻辑读的是 CAPS 这一份。')

    def test_expected_roles(self):
        """逐型号核用户给定的期望值。"""
        reg = self._registry()
        for key, (role, dual, svin, has_status) in sorted(_EXPECTED_ROLE_TABLE.items()):
            with self.subTest(module=key):
                self.assertIn(key, reg, f'{key} 没在注册表里')
                cls = reg[key]
                caps = getattr(cls, 'CAPS', {}) or {}
                self.assertEqual(getattr(cls, 'ROLE', None), role, f'{key} 角色不对')
                self.assertEqual(getattr(cls, 'DUAL_MODE', None), dual, f'{key} DUAL_MODE 不对')
                self.assertEqual(caps.get('dual_mode'), dual, f'{key} CAPS dual_mode 不对')
                self.assertEqual(caps.get('svin'), svin, f'{key} svin 能力位不对')
                # 缺键按 True 处理（BaseDriver 语义），只有 N4 系列显式声明 False
                self.assertEqual(caps.get('svin_has_status', True), has_status,
                                 f'{key} svin_has_status 不对')

    def test_svin_cap_matches_methods(self):
        """`CAPS['svin']` 与「方式表里有 survey_in」必须一致。

        这是"按钮能亮、点了必 400"的根因：`service.start_survey_in()`
        第一句就是 `_reject_without_caps(module, 'svin')`，而前端放不放开
        看的是 `methods_of()`。两边不一致时，用户看到按钮是亮的，
        点下去收到 400 —— UM960/UM980/UM982 之前就卡在这里。
        """
        from rtkserver.calibration.drivers import driver_manager
        for key, cls in sorted(self._registry().items()):
            caps = getattr(cls, 'CAPS', {}) or {}
            methods = driver_manager.methods_of(key)
            with self.subTest(module=key):
                if 'survey_in' in methods:
                    self.assertIs(caps.get('svin'), True,
                                  f'{key}: 方式表里有 survey_in，但 CAPS["svin"] 不是 True。'
                                  ' 前端会放开"自收敛"按钮，后端 start_survey_in 会拒。')

    def test_no_status_models_route_to_time_branch(self):
        """没有收敛进度语句的型号必须在 writer 里走"按时间等"分支。

        `svin_has_status=False` 只是**声明**，真正生效要靠 `_step_svin_wait`
        的分流。这里把调度器和两个分支都锁住，防止有人后来把分流删了
        只留下能力位 —— 那种情况能力位变成纯装饰，N4 会回落去轮询一个
        永远不来的 `$PQTMSVINSTATUS`，一路跑到超时。
        """
        with io.open(os.path.join(REPO_ROOT, 'rtkserver', 'calibration', 'writer.py'),
                     encoding='utf-8') as handle:
            src = handle.read()
        self.assertIn('def _step_svin_wait_by_status(', src)
        self.assertIn('def _step_svin_wait_by_time(', src)
        self.assertIn('def _gga_ecef(', src, '按时间等的那条支路需要从 GGA 反算 ECEF')
        # 调度器必须真的按能力位分流，而不是无条件走某一条
        block = src[src.index('def _step_svin_wait('):]
        block = block[:block.index('def _step_svin_wait_by_status(')]
        self.assertIn('svin_has_status', block, '调度器要读能力位')
        self.assertIn('_step_svin_wait_by_time', block)
        self.assertIn('_step_svin_wait_by_status', block)


class TestSvinModeContract(unittest.TestCase):
    """`get_svin_config()['mode']` 必须与 Quectel 同口径（三态）。"""

    _THREE_STATES = ('disabled', 'survey_in', 'fixed')

    def _driver_source(self, filename):
        with io.open(os.path.join(REPO_ROOT, 'rtkserver', 'calibration',
                                  'drivers', filename), encoding='utf-8') as handle:
            return handle.read()

    def test_all_drivers_report_three_state_mode(self):
        """回读键名不统一会让 `writer._svin_config_verified()` 永远判失败。

        它只认 `readback.get('mode') == 'survey_in'`。UM 驱动早先回的是
        N4 原生口径 `'base'`，于是每次 Survey-in 配置回读都判"模式不是
        survey_in"，重发一轮仍不符就整单失败 —— UM960/UM980/UM982 的
        「自收敛」不是没实现，是键名对不上。

        这里做**静态**校验：两个驱动源码里，`mode` 键的取值都必须落在三态内。
        （跑不出真值是因为 `get_svin_config()` 要发命令回读，无通道时
        会直接 AttributeError —— 那是通道的问题，不是口径的问题。）
        """
        for filename, native_hint in (('quectel_lg290p.py', 'survey_in'),
                                      ('quectel_lc29hbs.py', 'survey_in'),
                                      ('unicore_n4.py', 'norm')):
            src = self._driver_source(filename)
            with self.subTest(driver=filename):
                body = src[src.index('def get_svin_config('):]
                # 截到下一个方法定义为止
                nxt = body.find('\n    def ', 1)
                if nxt > 0:
                    body = body[:nxt]
                self.assertRegex(body, r"'mode':",
                                 f'{filename} 的 get_svin_config 没给 mode 键')
                # 三态的字符串必须都出现过（映射表或归一化表达式）
                for state in self._THREE_STATES:
                    self.assertIn(state, body,
                                  f'{filename} 的 get_svin_config 缺 {state} 这一态')

    def test_quectel_mode_uses_verbatim_three_state(self):
        """Quectel 侧 `mode` 直接取三态码，不接受原生方言串。"""
        src = self._driver_source('quectel_lg290p.py')
        body = src[src.index('def get_svin_config('):]
        nxt = body.find('\n    def ', 1)
        if nxt > 0:
            body = body[:nxt]
        self.assertIn("{0: 'disabled', 1: 'survey_in', 2: 'fixed'}", body,
                      'PQTMCFGSVIN 的 Mode 码必须是 0/1/2 直译，别再加中转')

    def test_n4_normalises_and_keeps_native(self):
        """N4 侧 `mode` 必须是归一化后的三态，原生口径另存 `mode_raw`。

        不能反过来（`mode` 给 'base'、归一化值另放一个键）：流程层只读
        `mode`，它在 writer / backup 里被当成三态用了至少三处。
        """
        src = self._driver_source('unicore_n4.py')
        body = src[src.index('def get_svin_config('):]
        nxt = body.find('\n    def ', 1)
        if nxt > 0:
            body = body[:nxt]
        self.assertIn("'mode': norm", body,
                      "N4 的 mode 必须给归一化三态（norm），不能给原生 'base'")
        self.assertIn("'mode_raw'", body,
                      'N4 原生口径必须另存 mode_raw，写日志/排障要看')
        # 归一化的两个关键映射：time → survey_in
        self.assertIn("'time': 'survey_in'", body,
                      "variant='time'（MODE BASE TIME）必须归一化成 survey_in")


class TestEcefInputContract(unittest.TestCase):
    """`_check_ecef()` 的入参形态契约（真机实测 500 的回归防线）。

    背景：`POST /api/calibration/start` 传 `{'x':..,'y':..,'z':..}` 时，
    早先的 `_check_ecef` 按 `values[0]` 取值 → `KeyError: 0` → Flask 渲染
    HTML 500。前端 `calibration.js` 十几处都是 `res.json()` 直取，拿到 HTML
    只能显示"网络错误"，真实原因传不到用户眼前。

    契约三句话：
      1. 序列和字典**都要认**（两条都是真实通路）；
      2. 任何非法输入**只能是 `ValueError`**，不能漏 `KeyError`/`TypeError`；
      3. NaN / Inf 必须挡（它们能过 `float()`，却让所有范围比较失效）。
    """

    def _fn(self):
        from rtkserver.calibration.writer import _check_ecef
        return _check_ecef

    def test_accepts_sequence_and_dict(self):
        f = self._fn()
        want = (-2001277.2129, 5416697.0103, 2699549.4332)
        self.assertEqual(f(list(want)), want)
        self.assertEqual(f(tuple(want)), want)
        self.assertEqual(f({'x': want[0], 'y': want[1], 'z': want[2]}), want)

    def test_dict_missing_key_is_value_error(self):
        f = self._fn()
        for bad in ({'x': 1, 'y': 2}, {'x': 1, 'z': 3}, {}):
            with self.assertRaises(ValueError, msg=f'{bad} 应报 ValueError'):
                f(bad)

    def test_never_leaks_keyerror(self):
        """漏 KeyError 就是 500；这里钉死它只能 ValueError。"""
        f = self._fn()
        for bad in (None, 'abc', [1, 2], {'y': 1, 'z': 2}, 5):
            try:
                f(bad)
            except ValueError:
                pass
            except Exception as e:                   # noqa: BLE001
                self.fail(f'{bad!r} 漏出了 {type(e).__name__}: {e}（应为 ValueError）')
            else:
                self.fail(f'{bad!r} 没被拦住')

    def test_rejects_nan_and_inf(self):
        """NaN 能过 float()，但所有范围比较都是 False —— 必须单独挡。"""
        f = self._fn()
        for value in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(ValueError, msg=f'{value!r} 必须被拒'):
                f([value, 5416697.0, 2699549.0])

    def test_rejects_latlon_as_ecef(self):
        f = self._fn()
        with self.assertRaises(ValueError):
            f([110.27, 25.20, 164.5])


class TestSvinTimeoutFallback(unittest.TestCase):
    """自收敛必须有兜底上限，不能无限等（真机实测暴露）。

    背景：模块没天线时 `$PQTMSVINSTATUS` 照样 1 Hz 输出，但 `Obs` 恒为 0。
    早先 `_step_svin_wait_by_status` 只在 `svin_timeout` 非空时才判超时，
    于是任务**永不结束**、串口被占到进程重启；而同文件的
    `_step_svin_wait_by_time` 一直有兜底 —— 两条路不对称。
    """

    def test_constants_exist_and_are_sane(self):
        from rtkserver.calibration import writer
        self.assertGreater(writer.SVIN_TIMEOUT_PER_OBS, 1.0,
                           '系数至少 >1，否则"收星慢但确实在前进"会被误掐')
        self.assertGreaterEqual(writer.SVIN_DEFAULT_TIMEOUT_MIN, 300.0,
                                '下限不能太小，否则档位配错时正常收敛被掐掉')

    def test_status_branch_has_fallback(self):
        """源码里必须有兜底表达式，而不是只有 `if self.svin_timeout and ...`。"""
        import inspect
        from rtkserver.calibration.writer import CoordinateWriter
        body = inspect.getsource(CoordinateWriter._step_svin_wait_by_status)
        self.assertIn('SVIN_TIMEOUT_PER_OBS', body,
                      'by_status 分支必须用档位推算兜底上限')
        self.assertIn('SVIN_DEFAULT_TIMEOUT_MIN', body,
                      'by_status 分支必须用兜底下限')

    def test_all_profiles_get_bounded_limit(self):
        from rtkserver.calibration.writer import (
            SVIN_DEFAULT_TIMEOUT_MIN, SVIN_TIMEOUT_PER_OBS, SURVEY_PROFILES)
        for key, preset in SURVEY_PROFILES.items():
            limit = max(SVIN_DEFAULT_TIMEOUT_MIN,
                        preset['count'] * SVIN_TIMEOUT_PER_OBS)
            self.assertTrue(600 <= limit <= 4 * 3600,
                            f'{key} 档的兜底上限 {limit}s 不合理')


class TestCalibrationApiErrorShape(unittest.TestCase):
    """标定 API 的 5xx 必须是 JSON，不能是 HTML 错误页。

    前端有十几处 `res.json()` 直取；拿到 `<!doctype html>` 就是
    `SyntaxError`，用户只能看到"网络错误"，真实原因完全丢失。
    """

    def test_5xx_is_json_for_calibration_api(self):
        import rtkserver.web.api_calibration as mod
        app = mod.app if hasattr(mod, 'app') else None
        if app is None:
            # 本模块不持 app，直接读实现了兜底的那个函数
            src = open(mod.__file__, encoding='utf-8').read()
            self.assertIn('_install_json_error_page', src)
            self.assertIn('after_request', src,
                          '必须用 after_request 改写 5xx，且限定在标定前缀内')
            self.assertIn("/api/calibration/", src,
                          '兜底必须限定前缀，不能污染数据面接口')
            return
        client = app.test_client()
        # 故意触发一个 5xx：不存在的路径走不到这里，改为直接断言钩子存在
        self.assertTrue(any(getattr(f, '__name__', '') == '_jsonify_calib_errors'
                            for f in app.after_request_funcs.get(None, [])))


class TestUbxQueryVsConfigSemantics(unittest.TestCase):
    """UBX 的**查询类**与**配置类**报文必须走不同的等待方式。

    ## 这条契约是怎么来的（真机测试暴露的设计缺陷）

    `detect()` 原来调的是 `_send_ubx(*K.MSG_MON_VER, b'', retries=3)`。
    `_send_ubx` 等的是 `UBX-ACK-ACK`(05 01)，而 **MON-VER 是查询类报文，
    模块永远不回 05 01，只回一条带载荷的 0A 04**。于是每轮白等 1.5 秒、
    重试 3 次共约 5 秒，然后判 `timeout` —— 模块明明答了，驱动说没收到。

    这个错误最坏的地方不是慢，而是**它把排查方向带偏**：失败文案写
    "未收到 UBX-MON-VER 应答，不是 u-blox Gen9 模块"，现场会去查
    流控、TX/RX 接线、固件升级模式，而真正的原因只是"等错了报文类型"。
    同一次真机测试里扫描器（裸读字节）明明读到了 MON-VER，
    两个组件结论相反，就是这个 bug 的指纹。

    ## 三条断言锁住什么

    1. `detect()` 的源码里不能出现 `_send_ubx`（回到老路就红）；
    2. 只回内容、不回 ACK 的模块，`info()` 必须**认出来**且够快；
    3. 把配置类报文误传给 `_poll_ubx()` 必须**立刻抛 DriverBug**，
       而不是静默超时 1.5 秒再报一个指向硬件的错。
    """

    def test_detect_uses_poll_not_send(self):
        """只查**真实调用**，不看注释 —— 注释里解释"为什么不用 _send_ubx"是正当的。

        用 AST 取 `self.xxx(...)` 形式的调用名，字符串匹配会被大段解释性
        注释误伤（本项目的驱动注释写得很长，正是为了让后来人不再踩同一个坑）。
        """
        import inspect
        from rtkserver.calibration.drivers.ublox_zed_f9p import ZEDF9PDriver

        tree = ast.parse(inspect.getsource(ZEDF9PDriver.detect).replace(
            '    def detect', 'def detect', 1))
        called = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                value = func.value
                if isinstance(value, ast.Name) and value.id == 'self':
                    called.add(func.attr)

        self.assertIn('_poll_ubx', called, 'detect 必须用 _poll_ubx 等查询应答')
        self.assertNotIn('_send_ubx', called,
                         'detect 不能走等 ACK 的配置路径（MON-VER 不回 ACK）')

    def test_query_reply_without_ack_is_accepted(self):
        """模块只回 MON-VER 内容、一个 ACK 都不回 —— 这是真实 u-blox 行为。"""
        from rtkserver.calibration.drivers.base import Channel, ubx_frame
        from rtkserver.calibration.drivers.ublox_zed_f9p import ZEDF9PDriver

        class FakePort:
            """最小串口替身：只对查询类报文回内容，配置类一律不回。"""

            def __init__(self):
                self.port = 'FAKE'
                self.baudrate = 38400
                self.is_open = True
                self.rx = b''

            def write(self, data):
                if len(data) >= 4 and data[2] == 0x0A and data[3] == 0x04:
                    sw = b'EXT CORE 1.00 (61b2dd)'.ljust(30, b'\x00')
                    hw = b'00190000'.ljust(10, b'\x00')
                    ext = b'ROM BASE 0x118B2060'.ljust(30, b'\x00')
                    ext += b'FWVER=HPG 1.32'.ljust(30, b'\x00')
                    ext += b'MOD=ZED-F9P'.ljust(30, b'\x00')
                    self.rx += ubx_frame(0x0A, 0x04, sw + hw + ext)
                elif len(data) >= 4 and data[2] == 0x0A and data[3] == 0x09:
                    self.rx += ubx_frame(0x0A, 0x09, bytes(60))
                # 注意：**故意不回任何 05 01 / 05 00**
                return len(data)

            def flush(self):
                pass

            def read(self, n=256):
                out, self.rx = self.rx[:n], self.rx[n:]
                return out

            def reset_input_buffer(self):
                self.rx = b''

            def close(self):
                self.is_open = False

        channel = Channel(port='FAKE', serial_instance=FakePort())
        driver = ZEDF9PDriver(channel)
        started = time_module.time()
        ok, message, data = driver.info()
        elapsed = time_module.time() - started

        self.assertTrue(ok, f'只回内容的模块应该被认出来，实际：{message}')
        self.assertIn('ZED-F9P', message)
        self.assertEqual(data.get('model_key'), 'zed_f9p')
        # 旧实现要白等 4.5s+（3 次 × 1.5s）。现在两条查询各 1.5s 封顶，
        # 正常路径 1 秒内返回。给到 3 秒容忍 CI 抖动。
        self.assertLess(elapsed, 3.0, f'查询应答不该慢等 ACK，实际耗时 {elapsed:.2f}s')

    def test_config_message_rejected_by_poll(self):
        """配置类报文误传进 `_poll_ubx()` 必须立刻报错，不能静默超时。"""
        from rtkserver.calibration.drivers.base import Channel, DriverBug
        from rtkserver.calibration.drivers.ublox_zed_f9p import (
            ZEDF9PDriver, _CONFIG_CLASS_MESSAGES)

        self.assertIn((0x06, 0x8A), _CONFIG_CLASS_MESSAGES)   # CFG-VALSET
        self.assertNotIn((0x0A, 0x04), _CONFIG_CLASS_MESSAGES)  # MON-VER 是查询

        class Quiet:
            """什么都不回的串口：用来证明"报错是拦出来的，不是超时出来的"。"""

            port, baudrate, is_open = 'FAKE', 38400, True

            def write(self, data):
                return len(data)

            def flush(self):
                pass

            def read(self, n=256):
                return b''

            def reset_input_buffer(self):
                pass

            def close(self):
                pass

        driver = ZEDF9PDriver(Channel(port='FAKE', serial_instance=Quiet()))
        started = time_module.time()
        with self.assertRaises(DriverBug):
            driver._poll_ubx((0x06, 0x8A))


class TestOutputSnapshotRestore(unittest.TestCase):
    """`silence_outputs()` 必须是**可逆**的，否则它就是一台单向破坏机。

    ## 这条契约是怎么来的（Com12 真机，ZED-F9P / HPG 1.32）

    那块板子的输出状态：`RTCM_1005/1074/1084/1094/1124/1230` 全 =1，
    NAV/NMEA/RXM 那 69 个键里**只有 `NAVOUT_NAV_SVIN_USB` 非零**。
    也就是说它是一台"只剩 RTCM 的纯净基站"—— 用户之前调试时把别的语句关了。

    原先的链路是这样把它彻底弄坏的：

        1. `backup.capture()` 只记 `RATE_TARGETS` 那 12 条
           （GGA/RMC/GSA/GLL + 七条 RTCM），**一条 NAV-* 都没有**；
        2. `silence_outputs(keep=('NAV_SVIN',))` 把开着的 NAV/NMEA/RXM 全写 0；
        3. 还原时 `_restore_messages()` 只回放那 12 条，NAV 一条都放不回。

    结果：模块从此只吐 RTCM + NAV-SVIN，用户 u-center 里星图全空，
    而界面显示"已按备份回放"、**不报任何错**。这种"看着成功、
    实际更糟"的损坏比直接报错恶劣得多。

    ## 四条断言锁住什么

    1. 驱动必须**同时**具备快照 / 开启 / 还原三件套（少一个就是单向的）；
    2. `silence_outputs()` 关掉的键，`snapshot_outputs()` 必须都覆盖到——
       两者枚举**同一个集合**，不许各写一份；
    3. 快照要记**全量**（含 0 的）：还原时"本来是关的"也要写回去，
       否则会把用户原本关着的输出打开，等于反向破坏；
    4. 流程层必须先快照再静音，**快照失败就不许静音**。
    """

    def _driver(self):
        """用真驱动的枚举/键表，只把串口换成记录型替身。"""
        from rtkserver.calibration.drivers.base import Channel
        from rtkserver.calibration.drivers.ublox_zed_f9p import ZEDF9PDriver

        class Recorder:
            """记录每一次写入的键值，能让 VALGET 回读出"刚写进去的值"。"""
            port, baudrate, is_open = 'FAKE', 38400, True

            def __init__(self):
                self.rx = b''
                self.written = []

            def write(self, data):
                return len(data)

            def flush(self):
                pass

            def read(self, n=256):
                out, self.rx = self.rx[:n], self.rx[n:]
                return out

            def reset_input_buffer(self):
                self.rx = b''

            def close(self):
                pass

        port = Recorder()
        return ZEDF9PDriver(Channel(port='FAKE', serial_instance=port)), port

    def test_driver_exposes_all_three(self):
        """快照 / 开启 / 还原三件套必须都在，且 CAPS 如实声明。"""
        from rtkserver.calibration.drivers.ublox_zed_f9p import ZEDF9PDriver

        for name in ('snapshot_outputs', 'restore_outputs', 'enable_outputs',
                     'silence_outputs', '_output_keys'):
            self.assertTrue(callable(getattr(ZEDF9PDriver, name, None)),
                            f'ZEDF9PDriver 缺少 {name}')
        self.assertTrue(ZEDF9PDriver.CAPS.get('output_snapshot'),
                        'CAPS 要声明 output_snapshot，流程层才敢走"先快照再静音"')
        self.assertTrue(ZEDF9PDriver.CAPS.get('silence'),
                        'silence 不能让位给 output_snapshot，两条能力是互补的')

    def test_silence_and_snapshot_share_one_key_set(self):
        """静音枚举的键 ⊆ 快照枚举的键：关得掉的都必须能放回。"""
        from rtkserver.calibration.drivers import ubx_keys as K

        driver, _port = self._driver()
        ports = ('USB', 'UART1')

        snapshot_keys = set(driver._output_keys(ports))
        # `silence_outputs` 的 targets 就是 `_output_keys()` 减去 keep，
        # 所以"关得掉的"必然是 `_output_keys()` 的子集 —— 这里正着验一遍：
        # 两个方法都真的用同一个枚举（AST 取真实调用，注释里提到不算）。
        import inspect
        for func in (driver.silence_outputs, driver.snapshot_outputs):
            tree = ast.parse(inspect.getsource(func).replace(
                '    def ', 'def ', 1))
            called = {n.func.attr for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)
                      and isinstance(n.func.value, ast.Name)
                      and n.func.value.id == 'self'}
            self.assertIn('_output_keys', called,
                          f'{func.__name__} 必须复用 _output_keys()，'
                          f'否则"关掉的那批"和"能恢复的那批"会随时间漂移')

        # RTCM 不在这组枚举里 —— 它由 read_rtcm_config / restore_rtcm_config 管。
        # 这条断言防的是"哪天有人把 RTCM 也塞进 _output_keys"，
        # 那样 silence 会把 RTCM 一起关掉，基站直接哑掉。
        self.assertFalse([k for k in snapshot_keys if k.upper().startswith('RTCM')],
                         'RTCM 播发键不该进这组枚举：静音不能关基站的主播发')
        self.assertTrue(all(k in K.ALL_KEYS for k in snapshot_keys))

    def test_snapshot_records_zero_rates_too(self):
        """快照必须**全量**记录（含 0），否则还原会把用户关着的输出打开。"""
        driver, _port = self._driver()

        # 伪造一次 VALGET：一半开、一半关
        keys = driver._output_keys(('USB',))
        self.assertGreater(len(keys), 10, 'USB 口应该枚举出十几条以上的输出键')
        fake = {k: (1 if i % 2 == 0 else 0) for i, k in enumerate(keys)}

        original = driver._valget_batched
        driver._valget_batched = lambda ks, layer=0: (
            {k: fake[k] for k in ks if k in fake}, 0, [])
        try:
            ok, message, data = driver.snapshot_outputs(ports=('USB',))
        finally:
            driver._valget_batched = original

        self.assertTrue(ok, message)
        rates = data.get('rates') or {}
        self.assertEqual(set(rates), set(fake),
                         '快照要覆盖全部键，不能只记开着的')
        self.assertIn(0, rates.values(),
                      '必须把速率为 0 的也记下来：还原时"本来是关的"同样要写回')
        self.assertEqual(len(data.get('on') or {}), sum(1 for v in fake.values() if v),
                         'on 里只放开着的，但 rates 必须是全量')

    def _called_order(self, func):
        """按源码出现顺序返回**真实调用**的驱动方法名。

        必须走 AST 而不是字符串 find：本项目的注释极长，`_step_setup_messages`
        里解释"为什么静音前要快照"的那段话本身就提到了 `silence_outputs`，
        字符串匹配会把它当成调用，顺序就假红了 —— 这个坑刚踩过一次。

        三类都要收：
            · `self.xxx(...)`                        —— 流程内部方法
            · `self.driver.xxx(...)`                 —— 驱动方法直接调用
            · `getattr(self.driver, 'xxx', None)`    —— 驱动能力探测
        第三类是"能力可选"的统一写法（驱动不一定有这个方法），
        漏了它会把所有可选能力调用当不存在。
        """
        import inspect
        tree = ast.parse(inspect.getsource(func).replace('    def ', 'def ', 1))
        calls = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == 'self'):
                calls.append((node.lineno, node.func.attr))
                continue
            # self.driver.silence_outputs(...)
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Attribute)
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == 'self'
                    and node.func.value.attr == 'driver'):
                calls.append((node.lineno, node.func.attr))
                continue
            # getattr(self.driver, 'snapshot_outputs', None)
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == 'getattr'
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                calls.append((node.lineno, node.args[1].value))
        calls.sort()
        return [name for _l, name in calls]

    def test_writer_snapshots_before_silence(self):
        """流程层：先快照 → 再静音；**快照失败就跳过静音**。"""
        import inspect
        from rtkserver.calibration import writer as writer_mod

        order = self._called_order(
            writer_mod.CoordinateWriter._step_setup_messages)
        self.assertIn('snapshot_outputs', order,
                      '_step_setup_messages 必须先做输出快照')
        self.assertIn('silence_outputs', order)
        self.assertLess(order.index('snapshot_outputs'),
                        order.index('silence_outputs'),
                        '快照必须在静音之前 —— 静音后再快照，'
                        '记下的已经是关掉之后的状态，还原只会把"关"再回放一遍')

        src = inspect.getsource(writer_mod.CoordinateWriter._step_setup_messages)
        self.assertIn('已跳过静音', src,
                      '快照失败时要跳过静音：宁可多占带宽，也不能关完没法还原')

    def test_restore_replays_snapshot_after_backup(self):
        """还原：快照回放必须**排在备份速率回放之后**，让快照为准。

        为什么顺序不能反：备份表（`RATE_TARGETS`）只有 12 条、且不含 NAV；
        快照是静音前那一刻的**全量**真实状态。两者交集里那 4 条
        （GGA/RMC/GSA/GLL）会被写两遍，谁在后谁说话 —— 必须让快照说话。
        """
        import inspect
        from rtkserver.calibration import writer as writer_mod

        order = self._called_order(
            writer_mod.CoordinateWriter._restore_messages)
        self.assertIn('restore_outputs', order,
                      '_restore_messages 必须按快照放回输出配置')
        self.assertIn('set_message_rate', order,
                      '备份速率回放还在（不能因为加了快照就把它删掉）')
        self.assertLess(order.index('set_message_rate'),
                        order.index('restore_outputs'),
                        '快照回放要在备份速率回放之后：快照更全更准，'
                        '应当覆盖前者写下的值')

    def test_restore_never_writes_flash(self):
        """还原只碰 RAM：Flash 一个字节不动，出错也能靠重启回到原配置。"""
        import inspect
        from rtkserver.calibration import writer as writer_mod
        from rtkserver.calibration.drivers.ublox_zed_f9p import ZEDF9PDriver

        src = inspect.getsource(ZEDF9PDriver.restore_outputs)
        self.assertIn('K.LAYER_RAM', src)
        self.assertNotIn('K.LAYER_FLASH', src,
                         'restore_outputs 不许写 Flash：还原本身出错时，'
                         '用户至少还能靠重启回到原来那份落盘配置')
        self.assertIn('restore_outputs', inspect.getsource(
            writer_mod.CoordinateWriter._restore_messages))

    def test_readonly_mode_never_touches_outputs(self):
        """只测不写（external_rover）不许开输出 —— 那台设备不归我们管。"""
        import inspect
        from rtkserver.calibration import writer as writer_mod

        src = inspect.getsource(writer_mod.CoordinateWriter._step_prepare_outputs)
        self.assertIn('_readonly', src,
                      '开工前开输出必须先看 _readonly，只读模式一条写命令都不能发')
        self.assertIn('persist=False', src,
                      '开输出只改 RAM，不许动 Flash（擦写寿命 + 用户可能特意关的）')

    def test_output_prep_runs_before_all_branches(self):
        """`run()` 里开启输出的调用必须在所有方式分支之前。"""
        import inspect
        from rtkserver.calibration import writer as writer_mod

        src = inspect.getsource(writer_mod.CoordinateWriter.run)
        pos_prep = src.find('self._step_prepare_outputs')
        self.assertGreater(pos_prep, -1, 'run() 必须调用 _step_prepare_outputs')
        for later in ('_step_svin_resume', '_step_cors_collect',
                      '_step_ensure_base_for_write'):
            pos = src.find(later)
            if pos > -1:
                self.assertLess(pos_prep, pos,
                                f'开启输出要在 {later} 之前：那一步开始就要读模块输出了')

    def test_enable_outputs_is_idempotent(self):
        """已经是目标值的键不该重复写（Flash 寿命 + 无谓往返）。"""
        driver, _port = self._driver()
        keys = driver._output_keys(('USB',))
        target = [k for k in keys if k.endswith('NAV_PVT_USB')]
        self.assertTrue(target, '键表里应该有 NAV_PVT_USB')

        writes = []

        original_get = driver._valget_batched
        original_set = driver._valset

        def fake_get(ks, layer=0):
            # 全部已经是 1Hz
            return {k: 1 for k in ks}, 0, []

        def fake_set(items, layers=0):
            writes.extend(items)
            return True, 'ACK', {}

        driver._valget_batched = fake_get
        driver._valset = fake_set
        try:
            ok, message, data = driver.enable_outputs(
                ['NAV_PVT'], rate=1, ports=('USB',))
        finally:
            driver._valget_batched = original_get
            driver._valset = original_set

        self.assertTrue(ok, message)
        self.assertEqual(writes, [], '已经是目标值还写，等于白烧一次 Flash')
        self.assertTrue(data.get('already_on'), '应当如实报告"本来就开着"')
        self.assertEqual(data.get('enabled'), [],
                         '一条都没写，enabled 就该是空的（别把 already_on 算进去）')


class TestOutputSurfaceIsAbstracted(unittest.TestCase):
    """F9P 的输出面必须是**驱动抽象命令**，不许和标定流程绑死。

    ## 这条契约怎么来的

    用户原话：`他的驱动应该单独抽象成命令 不能和一键标定的代码架构绑定写死
    写死了会影响别的模块调用`。

    具体病症是流程层里的 `getattr(self.driver, 'snapshot_outputs', None)`
    这类**特征探测**。它看起来"更兼容"，实际干了两件坏事：

        1. 把"某个驱动有没有这个方法"推迟到运行期才知道 —— 漏实现时
           静默跳过，用户只看到"流程卡在等 GGA"，没有任何线索指向根因；
        2. 让 F9P 的私有机制（MSGOUT 一遍 69 个键、按端口分组、还得先
           detect_port 探链路）事实上升级成了流程层的**可选依赖**，
           而 LG290P / LC29H(BS) / N4 走的是完全不同的文本单键机制。

    正确形态：`BaseDriver` 声明三个抽象命令并给出**通用默认实现**
    （Quectel/Unicore 退化成逐条 `set_message_rate`，`snapshot_outputs`
    回 unsupported 让流程层安全跳过静音），F9P 在**自己内部**覆盖成
    VALGET/VALSET 整组操作。流程层永远按基类签名调用，不认型号。

    ## 断言锁住四件事

    1. 基类必须有这三个方法（否则"抽象"只是空话，别人也没法实现）；
    2. 流程层不许再用 `getattr` 探测这三个名字（AST 判，注释不算）；
    3. 三个方法的**前三个位置参数**两边一致（F9P 多出来的必须带默认值）；
    4. 键表自描述：`OUTPUT_FANOUT` 与 `_output_keys()` 枚举同一个集合，
       且端口全集从键表派生而不是驱动里再抄一份。
    """

    OUTPUT_API = ('snapshot_outputs', 'enable_outputs', 'restore_outputs')

    def test_base_driver_declares_three_commands(self):
        """基类必须有这三条命令，且真的可调用（不是 NotImplementedError 桩）。"""
        from rtkserver.calibration.drivers.base import BaseDriver

        for name in self.OUTPUT_API:
            self.assertTrue(callable(getattr(BaseDriver, name, None)),
                            f'BaseDriver 缺少抽象命令 {name}()：'
                            f'没有基类声明，流程层就只能靠 getattr 探测')

    def test_base_defaults_are_usable_not_stubs(self):
        """默认实现必须是**能用的退路**，不是抛异常。

        `snapshot_outputs` 默认回 unsupported（流程层据此跳过静音）；
        `enable_outputs` / `restore_outputs` 默认逐条走 `set_message_rate`。
        抛 NotImplementedError 会让 LG290P / LC29H(BS) / N4 在流程层直接崩。
        """
        import inspect
        from rtkserver.calibration.drivers.base import BaseDriver

        for name in self.OUTPUT_API:
            src = inspect.getsource(getattr(BaseDriver, name))
            self.assertNotIn('raise NotImplementedError', src,
                             f'{name}() 的基类默认实现不能抛 NotImplementedError：'
                             f'那是"每个驱动都必须自己写"，等于把可选能力变成硬依赖')
        self.assertIn('set_message_rate', inspect.getsource(
            BaseDriver.enable_outputs),
            'enable_outputs 的默认实现应当逐条 set_message_rate')
        self.assertIn('set_message_rate', inspect.getsource(
            BaseDriver.restore_outputs),
            'restore_outputs 的默认实现应当逐条 set_message_rate')

    def test_write_layer_has_no_feature_probing_on_output_api(self):
        """流程层不许对这三条命令做 `getattr` 探测 —— 只能是直接调用。"""
        import inspect
        from rtkserver.calibration import writer as writer_mod

        # 只查**真正会发命令**的那几个方法，不扫全文件：
        # `_restore_messages` 里 `getattr(self.driver, 'restore_rtcm_config')`
        # 属于 RTCM 面，是另一条待收口的线，不该让这里的断言假红。
        targets = [
            writer_mod.CoordinateWriter._step_prepare_outputs,
            writer_mod.CoordinateWriter._step_setup_messages,
            writer_mod.CoordinateWriter._restore_messages,
        ]
        for func in targets:
            tree = ast.parse(inspect.getsource(func).replace('    def ', 'def ', 1))
            probed = [
                node.args[1].value
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == 'getattr'
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in self.OUTPUT_API
            ]
            self.assertEqual(probed, [],
                             f'{func.__name__} 里还在探测 {probed}：'
                             f'改成直接 self.driver.xxx() —— '
                             f'基类已经给了默认实现，探测只会掩盖漏实现')

    def test_override_signatures_stay_call_compatible(self):
        """F9P 覆盖实现的前几个位置参数必须与基类一致，扩展位必须带默认值。

        流程层一律按基类签名调用，所以 `ports` / `strict` 这类 F9P 专有的
        参数只能作**带默认值的尾巴**，不能插到前面。
        """
        import inspect
        from rtkserver.calibration.drivers.base import BaseDriver
        from rtkserver.calibration.drivers.ublox_zed_f9p import ZEDF9PDriver

        for name in self.OUTPUT_API:
            base_ps = list(inspect.signature(
                getattr(BaseDriver, name)).parameters.values())[1:]   # 去掉 self
            sub_ps = list(inspect.signature(
                getattr(ZEDF9PDriver, name)).parameters.values())[1:]
            self.assertGreaterEqual(len(sub_ps), len(base_ps),
                                    f'{name}() 的参数比基类少，覆盖后流程层会 TypeError')
            for i, bp in enumerate(base_ps):
                self.assertEqual(sub_ps[i].name, bp.name,
                                 f'{name}() 第 {i + 1} 个位置参数应为 {bp.name!r}，'
                                 f'实际 {sub_ps[i].name!r}：流程层是按基类位置传的')
            for extra in sub_ps[len(base_ps):]:
                self.assertNotEqual(extra.default, inspect.Parameter.empty,
                                    f'{name}() 的扩展参数 {extra.name!r} 必须带默认值')

    def test_key_table_self_describes_ports(self):
        """`OUTPUT_FANOUT` 是 MSGOUT 键的唯一登记处，端口全集由它派生。"""
        from rtkserver.calibration.drivers import ubx_keys as K

        self.assertTrue(K.OUTPUT_FANOUT, 'ubx_keys 必须登记 OUTPUT_FANOUT')
        known = set(K.ALL_KEYS)
        self.assertEqual([k for k in K.OUTPUT_FANOUT if k not in known], [],
                         'OUTPUT_FANOUT 里的每个键都必须在 ALL_KEYS 里，'
                         '否则 VALGET 会拿一个固件不认识的键去问')
        self.assertEqual(set(K.OUTPUT_PORTS),
                         {'I2C', 'UART1', 'UART2', 'USB', 'SPI'},
                         '五个物理口都要覆盖；少一个会让那个口永远静不掉')

        # RTCM 播发键（`RTCM_1005` 这种纯数字形态）绝不许混进来。
        # `RXMOUT_RXM_RTCM_*` 是 RXM 族里名为 RTCM 的 UBX 报文，
        # 不是基站 1005 播发开关 —— 所以按"RTCM_数字"判，不按含 RTCM 判。
        import re as _re
        bad = [k for k in K.OUTPUT_FANOUT if _re.search(r'RTCM_\d', k)]
        self.assertEqual(bad, [], f'RTCM 播发键不该进 MSGOUT 枚举：{bad}')

    def test_output_keys_matches_fanout(self):
        """`_output_keys()` 结果必须与按端口过滤 `OUTPUT_FANOUT` **完全一致**。

        以前是靠 `f'{prefix}{name}_{port}'` 拼字符串，正确性依赖
        "这个口真的有这个键"和"UART1 后缀是不是空串"两条隐式约定。
        现在改成以键表为准，这条断言就是那个改动的锁。
        """
        from rtkserver.calibration.drivers.base import Channel
        from rtkserver.calibration.drivers.ublox_zed_f9p import ZEDF9PDriver
        from rtkserver.calibration.drivers import ubx_keys as K

        class _Null:
            port, baudrate, is_open = 'FAKE', 38400, True
            rx = b''

            def write(self, data):
                return len(data)

            def flush(self):
                pass

            def read(self, n=256):
                return b''

            def reset_input_buffer(self):
                pass

            def close(self):
                pass

        driver = ZEDF9PDriver(Channel(port='FAKE', serial_instance=_Null()))
        for ports in (('USB',), ('UART1',), ('USB', 'UART1')):
            got = set(driver._output_keys(ports))
            want = {k for k in K.OUTPUT_FANOUT if k.rsplit('_', 1)[-1] in ports}
            self.assertEqual(got, want, f'端口 {ports} 的枚举结果与键表不一致')

    def test_driver_ports_derive_from_key_table(self):
        """驱动里的 `PORTS` 不许再抄一份白名单 —— 两份会漂移。"""
        from rtkserver.calibration.drivers.ublox_zed_f9p import PORTS
        from rtkserver.calibration.drivers import ubx_keys as K

        self.assertEqual(set(PORTS), set(K.OUTPUT_PORTS),
                         'PORTS 要从 K.OUTPUT_PORTS 派生：手抄一份的话，'
                         '固件多曝一个口时驱动会先把合法的口判成非法并 raise DriverBug')

    def test_other_models_are_unaffected(self):
        """LG290P / LC29H(BS) / N4 / 未知模块 必须照常工作，不走 F9P 那套。"""
        from rtkserver.calibration.drivers.base import BaseDriver
        from rtkserver.calibration.drivers import driver_manager

        driver_manager.load_builtin_drivers()
        registry = driver_manager._REGISTRY
        self.assertGreaterEqual(len(registry), 4,
                                f'内置驱动没加载全，注册表只有 {sorted(registry)}')

        for name, cls in registry.items():
            for api in self.OUTPUT_API:
                self.assertTrue(callable(getattr(cls, api, None)),
                                f'{name} 缺 {api}()：基类改了但子类把它覆盖掉了')
            # 声称 F9P 专属能力的只能是 F9P 自己
            caps = getattr(cls, 'CAPS', None) or {}
            if caps.get('output_snapshot'):
                self.assertEqual(cls.__name__, 'ZEDF9PDriver',
                                 f'{name} 不该声明 output_snapshot：'
                                 f'只有 u-blox 那套 MSGOUT 位表需要"先快照再静音"')

        # 基类默认实现必须是"能用但降级"，不是抛异常。
        self.assertTrue(hasattr(BaseDriver, 'snapshot_outputs'))


class TestNoFeatureProbingOnDriver(unittest.TestCase):
    """流程层/服务层/备份层**一律不许**对驱动做 `getattr` 特征探测。

    ## 为什么这条要从"输出面三个方法"推广到全部

    上一类 `TestOutputSurfaceIsAbstracted` 只锁住了 `snapshot_outputs` /
    `enable_outputs` / `restore_outputs` 三个名字，因为当时的现实是"只有这三个
    引起了事故"。但同一个反模式在另外七处**照旧存在**：

        getattr(self.driver, 'set_mode_memory_only', None)
        getattr(self.driver, 'enable_survey_status', None)
        getattr(self.driver, 'set_msm_rate', None)
        getattr(self.driver, 'set_fix_rate', None)
        getattr(self.driver, 'ensure_nmea_output', None)
        getattr(self.driver, 'verify_persisted', None)
        getattr(self.driver, 'survey_status', None)
        getattr(self.driver, 'save'/'reboot'/'read_antenna_info'/'read_uart', None)

    每一处的 `if xxx is None: return / skip` 都是一条**静默分支**：
    漏实现的型号和执行成功的型号，在日志与界面上长得一样。这与
    `docs/one-click-calibration-framework.zh.md` §"驱动能力必须显式声明"
    的结论矛盾 —— 那份文档要求"能力不存在时给出可解释的失败"。

    ## 断言

    1. 上面这些方法**全部**在 `BaseDriver` 上有默认实现；
    2. `writer.py` / `service.py` / `backup.py` 的**函数体**里不再出现
       `getattr(<驱动对象>, '<方法名>')` 形态（AST 判，注释和 docstring 不算）；
    3. 类常量（`CAPS` / `PASSIVE` / `RESTART_REQUIRED` / `MODE_VALUE` /
       `MSM_CONSTELLATIONS`）在基类上都有声明，流程层直接读属性。
    """

    ABSTRACT_API = (
        # 已抽到基类、带默认实现（回 unsupported 或退化成通用做法）
        'snapshot_outputs', 'enable_outputs', 'restore_outputs',
        'enable_survey_status', 'set_msm_rate', 'set_fix_rate',
        'verify_persisted', 'survey_status', 'set_mode_memory_only',
        'restore_rtcm_config', 'ensure_nmea_output', 'read_antenna_info',
        'read_uart', 'save', 'reboot',
    )
    CONST_ATTRS = ('CAPS', 'PASSIVE', 'RESTART_REQUIRED',
                   'MODE_VALUE', 'MSM_CONSTELLATIONS')

    # 这三个模块是"按型号分支"的重灾区；drivers/ 里的探测是驱动自己的事，不在此限。
    SCANNED_MODULES = ('writer', 'service', 'backup')

    def _iter_functions(self, module):
        import inspect
        for name, obj in vars(module).items():
            if inspect.isfunction(obj) and obj.__module__ == module.__name__:
                yield name, obj
            elif inspect.isclass(obj) and obj.__module__ == module.__name__:
                for mname, meth in vars(obj).items():
                    if inspect.isfunction(meth):
                        yield f'{name}.{mname}', meth

    def _probed_attrs(self, func):
        """返回该函数体里 `getattr(x, '字面量')` 的全部字面量。"""
        import inspect
        try:
            src = inspect.getsource(func)
        except (OSError, TypeError):
            return []
        # 去掉首行缩进，否则嵌套函数/方法源码无法单独 parse
        lines = src.splitlines()
        indent = len(lines[0]) - len(lines[0].lstrip()) if lines else 0
        src = '\n'.join(line[indent:] if len(line) > indent else line.lstrip()
                        for line in lines)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return []
        out = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == 'getattr'
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                out.append(node.args[1].value)
        return out

    def test_base_declares_every_abstract_api(self):
        from rtkserver.calibration.drivers.base import BaseDriver

        missing = [n for n in self.ABSTRACT_API
                   if not callable(getattr(BaseDriver, n, None))]
        self.assertEqual(missing, [],
                         f'BaseDriver 缺这些抽象命令：{missing}。'
                         f'缺一个，流程层就会退回 getattr 探测，'
                         f'漏实现时静默跳过而不报错')

    def test_default_impls_never_raise_notimplemented(self):
        import inspect
        from rtkserver.calibration.drivers.base import BaseDriver

        bad = []
        for name in self.ABSTRACT_API:
            src = inspect.getsource(getattr(BaseDriver, name))
            if 'raise NotImplementedError' in src:
                bad.append(name)
        self.assertEqual(bad, [],
                         f'这些默认实现抛了 NotImplementedError：{bad}。'
                         f'那不是"抽象"，而是"每个驱动都必须自己写"，'
                         f'等于把可选能力变成硬依赖')

    def test_no_getattr_probing_in_business_layers(self):
        import importlib
        offenders = {}
        for mod_name in self.SCANNED_MODULES:
            module = importlib.import_module(f'rtkserver.calibration.{mod_name}')
            for qualname, func in self._iter_functions(module):
                hits = [a for a in self._probed_attrs(func) if a in self.ABSTRACT_API]
                if hits:
                    offenders[f'{mod_name}.{qualname}'] = hits
        self.assertEqual(offenders, {},
                         f'这些地方还在探测驱动方法是否存在：{offenders}。'
                         f'改成直接调用 —— 基类已有默认实现，'
                         f'探测只会把"漏实现"变成"什么都没发生"')

    def test_class_constants_declared_on_base(self):
        """类常量不许用 getattr 兜底：基类已经声明，直接用属性读。"""
        from rtkserver.calibration.drivers.base import BaseDriver

        missing = [a for a in self.CONST_ATTRS if not hasattr(BaseDriver, a)]
        self.assertEqual(missing, [],
                         f'BaseDriver 没声明这些类常量：{missing}。'
                         f'流程层用 getattr(driver, x, 默认值) 兜底，'
                         f'会让"驱动没声明"和"驱动声明了默认值"两种情况混在一起')

    def test_persist_check_cap_implies_real_impl(self):
        """声明了 `persist_check` 的型号必须真的实现 `verify_persisted`。

        以前这里有个 `if not callable(checker): return {'reason': 'no_method'}`
        分支 —— 它把"声明了能力但没实现"和"这型号本来没这能力"报成了同一种结果。
        现在要求两者一致：要么都声明、要么都不声明。
        """
        from rtkserver.calibration.drivers import driver_manager

        driver_manager.load_builtin_drivers()
        for name, cls in driver_manager._REGISTRY.items():
            caps = getattr(cls, 'CAPS', None) or {}
            if not caps.get('persist_check'):
                continue
            impl = cls.__dict__.get('verify_persisted')
            self.assertIsNotNone(
                impl,
                f'{name} 声明了 CAPS["persist_check"] 却没自己实现 '
                f'verify_persisted()：基类默认实现只回 unsupported，'
                f'这条能力位就成了空头承诺')


if __name__ == '__main__':
    unittest.main(verbosity=2)
