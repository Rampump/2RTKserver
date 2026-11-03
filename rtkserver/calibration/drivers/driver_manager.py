# -*- coding: utf-8 -*-
"""驱动管理器：注册表、自动识别、能力清单元数据、真机探测入口。

三件事，只做这三件：
    match_driver()          未知模块 → 依次问"你是谁" → 返回第一个认领的驱动
    list_supported_modules() 前端要的型号清单（按双模/单模分组 + 能力矩阵）
    get_driver_by_id()      已知型号（例：从历史记录回填）→ 直接加载

注册表是**唯一真相**：新增驱动只要 import 到 `_BUILTIN_MODULES` 并在类上加
`@register`，别处不需要维护型号清单（方案 §5.1.1 的"一个文件一行注册"）。

真机探测（不需要主程序，也不占用 5757 端口）：
    python -m rtkserver.calibration.drivers.driver_manager --list
    python -m rtkserver.calibration.drivers.driver_manager --port COM3
    python -m rtkserver.calibration.drivers.driver_manager --port /dev/ttyUSB0 --baud 115200

注意：主程序（2rtkserver.service）正在读同一个串口时必须先停掉。
Linux 的 tty 不排他，两个 fd 同时 open 会随机瓜分字节，两边都收不全。
"""
import logging
import time

from .base import BaseDriver, Channel

# 内置驱动模块。**新增型号只改这一行 + 加一个文件**，不要另立清单。
# 注意顺序不是识别优先级（match_driver 按 key 排序后逐个试，各自用固件串前缀认领），
# 这里只是"要 import 哪些文件"。
_BUILTIN_MODULES = (
    'quectel_lg290p',
    'quectel_lc29hbs',
    'unicore_n4',
)

_REGISTRY = {}          # model_key -> 驱动类
_LOADED = {'done': False}


def register(driver_class):
    """把驱动类登记进注册表。既作装饰器用，也可以直接调用。"""
    if not issubclass(driver_class, BaseDriver):
        raise TypeError(f'{driver_class!r} 必须继承 BaseDriver')
    key = getattr(driver_class, 'MODEL_KEY', '')
    if not key:
        raise TypeError(f'{driver_class.__name__} 缺少 MODEL_KEY')
    if key in _REGISTRY and _REGISTRY[key] is not driver_class:
        logging.warning(f'型号键 {key} 被重复注册，后者覆盖前者')
    _REGISTRY[key] = driver_class
    return driver_class


def load_builtin_drivers():
    """导入内置驱动模块（触发 @register）。导入失败不抛，只记日志。

    单个驱动模块写坏（少依赖、语法错）不能让整个标定功能连"列出型号"都做不到。
    """
    if _LOADED['done']:
        return
    for module_name in _BUILTIN_MODULES:
        try:
            # 必须用 __package__ 拼绝对路径：`python -m ...driver_manager` 时
            # __name__ 是 '__main__'，靠 __name__ 推导会得到 '__main__.drivers.xxx' 直接导入失败。
            package = __package__ or 'rtkserver.calibration.drivers'
            __import__(f'{package}.{module_name}', fromlist=['*'])
        except Exception as e:
            logging.error(f'加载标定驱动 {module_name} 失败（已跳过）：{e}')
    _LOADED['done'] = True


def supported_driver_ids():
    load_builtin_drivers()
    return sorted(_REGISTRY)


def visible_driver_ids():
    """对前端可见的型号键（`HIDDEN = True` 的型号被剔掉）。"""
    load_builtin_drivers()
    return sorted(key for key, cls in _REGISTRY.items()
                  if not getattr(cls, 'HIDDEN', False))


def get_driver_by_id(model_key, channel=None):
    """按型号键取驱动实例。未知键返回 None（调用方决定是回退还是报错）。"""
    load_builtin_drivers()
    driver_class = _REGISTRY.get((model_key or '').strip().lower())
    if driver_class is None:
        return None
    return driver_class(channel)


def list_supported_modules():
    """返回按双模/单模分组的型号元数据，供前端渲染下拉框与能力灰化。

    结构：
        {
          'dual_mode':   [ {...}, ... ],
          'single_mode': [ {...}, ... ],
          'all':         [ ... ]
        }
    每项：id / name / match_prefix / dual_mode / caps / command_count /
          bauds / default_baud / baud_cmd
    `bauds` 是该型号厂商文档写明的波特率档位，前端据此给候选；
    用户仍可手动输入任意值，`bauds` 只影响自动探测顺序与非法值提示。
    """
    load_builtin_drivers()
    dual, single = [], []
    for model_key, driver_class in sorted(_REGISTRY.items()):
        # 隐藏型号不进前端下拉（HIDDEN=True，例：双天线定向板不适合做基准站）。
        # 注册表里仍保留，历史记录回填 / 手工指定型号照常能加载。
        if getattr(driver_class, 'HIDDEN', False):
            continue
        # 借一个空实例走 bauds()，保证"去重 + 降序"这套规则只有一处实现
        probe = driver_class(None)
        item = {
            'id': model_key,
            'name': getattr(driver_class, 'DISPLAY_NAME', model_key),
            'match_prefix': getattr(driver_class, 'MATCH_PREFIX', ''),
            'dual_mode': bool(getattr(driver_class, 'DUAL_MODE', False)),
            # 拷一份，防止前端拿到的 dict 被后续改动污染注册表
            'caps': dict(getattr(driver_class, 'CAPS', {}) or {}),
            'command_count': len(getattr(driver_class, 'CMD', {}) or {}),
            'bauds': list(probe.bauds()),
            'default_baud': int(getattr(driver_class, 'DEFAULT_BAUD', 115200)),
            'baud_cmd': getattr(driver_class, 'BAUD_CMD_KEY', ''),
        }
        (dual if item['dual_mode'] else single).append(item)
    return {'dual_mode': dual, 'single_mode': single, 'all': dual + single}


def match_driver(channel, only=None, timeout=None):
    """依次发识别命令，返回第一个认领的驱动。

    参数：
        only     只试这些型号键（用户已经手动选了型号时用，省掉 1~2 秒）
        timeout  预留给"整轮探测超时"的参数（目前未用）
    返回：
        (driver | None, message, attempts)
        attempts 是 [(model_key, 失败原因), ...]，失败时直接展示给用户，
        否则用户只会看到"没识别到模块"而不知道是"模块没回话"还是"型号不匹配"。
    """
    load_builtin_drivers()
    # 自动探测不试隐藏型号（HIDDEN=True），否则一块 UM982 会被识别成"可用型号"，
    # 而它在前端下拉里根本不存在，用户看到型号却找不到对应选项。
    # 用户手选型号时（only 非空）不受此限：手选即视为明确指定。
    candidates = [(key, cls) for key, cls in sorted(_REGISTRY.items())
                  if (key in only if only else not getattr(cls, 'HIDDEN', False))]
    if not candidates:
        return None, f'没有可用的驱动（已注册：{supported_driver_ids() or "无"}）', []

    if channel is None or not getattr(channel, 'is_open', False):
        return None, '串口未打开，无法识别模块', []

    attempts = []
    for model_key, driver_class in candidates:
        driver = driver_class(channel)
        try:
            ok, message, _data = driver.detect()
        except Exception as e:                     # 单个驱动炸了不能影响后面的候选
            ok, message = False, f'识别时异常：{e}'
        if ok:
            logging.info(f'识别到模块：{driver.DISPLAY_NAME}（{message}）')
            return driver, message, attempts
        attempts.append((model_key, message))
        logging.debug(f'{model_key} 不匹配：{message}')
        # 清掉上一个候选读到的残帧，避免影响下一个驱动的识别
        if getattr(channel, '_buffer', b''):
            channel.drain()

    detail = '；'.join(f'{key}: {message}' for key, message in attempts)
    return None, f'没有驱动认领这块模块（{detail}）', attempts


# ==================== 离线自测（假模块，不需要硬件） ====================
class _FakeSerial:
    """内存假模块：只说 LG290P 会说的那几句，用来离线验证驱动逻辑。

    刻意把"无关输出 / 回显 / ERROR / 沉默"四种情况都混进来：
    真实串口上就是这四种字节混在一起，只测"正常应答"的代码上线必翻车。
    """

    def __init__(self, port='FAKE', baud=115200, boot=True):
        self.port = port
        self.baudrate = baud
        self.is_open = True
        self.writes = []            # 收到的命令体（不含 $ 与 *CS）
        self.silent = set()         # 这些命令名不回任何东西（模拟超时）
        self.error = set()          # 这些命令名回 ERROR,1（模拟拒绝）
        self._inbox = b''
        if boot:
            self._inbox += _frame('PQTMVER,1,MODULE,LG290P03AANR01A03S,2024/04/30,10:53:07')

    # ---- 串口接口 ----
    def write(self, data):
        text = data.decode('ascii', 'replace').strip()
        body = text.lstrip('$').split('*')[0]
        self.writes.append(body)
        name = body.split(',')[0]

        # 每写一次都夹带无关输出，验证"无关输出不算应答"（方案 §4.2）
        self._inbox += _frame('GNGGA,120000.00,3000.00000,N,12000.00000,E,1,12,0.8,50.000,M,,,,')
        self._inbox += _frame('PQTMSVINSTATUS,1,291264000,1,,11,1,60,0.0000,0.0000,0.0000,0.0000')
        self._inbox += _frame(body)          # 回显

        if name in self.silent:
            return len(data)
        if name in self.error:
            self._inbox += _frame(f'{name},ERROR,1')
            return len(data)

        reply = self._reply(name, body)
        if reply:
            self._inbox += _frame(reply)
        return len(data)

    def _reply(self, name, body):
        table = {
            'PQTMVERNO': 'PQTMVERNO,LG290P03AANR01A03S,2024/04/30,10:53:07',
            'PQTMSN': 'PQTMSN,OK,1,16,ESG5241364AUIDE5',
            'PQTMUNIQID': 'PQTMUNIQID,OK,16,81D62010EE0AF375BDF5952CDC3757A1',
            'PQTMCFGRCVRMODE,R': 'PQTMCFGRCVRMODE,OK,2',
            'PQTMCFGSVIN,R': 'PQTMCFGSVIN,OK,1,3600,1.2,0.0000,0.0000,0.0000,0.0',
            'PQTMCFGANTINF,R': 'PQTMCFGANTINF,OK,,0,',
            'PQTMCFGRTCM,R': 'PQTMCFGRTCM,OK,4,0,-90.0,07,06,1,0',
            'PQTMSAVEPAR': 'PQTMSAVEPAR,OK',
        }
        if body in table:
            return table[body]
        if name in table:
            return table[name]
        return f'{name},OK'

    def read(self, size=256):
        chunk = self._inbox[:size]
        self._inbox = self._inbox[size:]
        return chunk

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def reset_input_buffer(self):
        self._inbox = b''


def _frame(body):
    from .base import frame as _make
    return _make(body).encode('ascii')


def _fake_channel():
    channel = Channel(port='FAKE', baud=115200)
    channel.serial_factory = lambda port, baud: _FakeSerial(port, baud)
    return channel.open()


def _selftest():
    """不接硬件跑通整条驱动链路：识别→信息→模式→坐标→重试边界→重启。

    跑法：python -m rtkserver.calibration.drivers.driver_manager --selftest
    """
    from .base import checksum

    # 0) 命令表校验和自检（方案 §4.1：算出来必须与表里记的一致）
    from .quectel_lg290p import LG290PDriver
    bad = []
    for key, expected in LG290PDriver.CS_EXPECT.items():
        actual = checksum(LG290PDriver.CMD[key])
        if actual != expected.upper():
            bad.append(f'{key}: 表={expected} 实算={actual}')
    assert not bad, bad
    print(f'[0] 命令表自检 OK  {len(LG290PDriver.CMD)} 条命令，'
          f'{len(LG290PDriver.CS_EXPECT)} 条带校验和常量')

    # 1) 自动识别
    channel = _fake_channel()
    driver, message, attempts = match_driver(channel)
    assert driver is not None, (message, attempts)
    assert driver.MODEL_KEY == 'lg290p'
    print(f'[1] 识别 OK  {driver.DISPLAY_NAME}（{message}）')

    # 2) info：版本/SN/唯一ID/模式，且无关输出没把应答挤掉
    ok, message, data = driver.info()
    assert ok, message
    assert data['firmware'].startswith('LG290P'), data
    assert data['sn'] == 'ESG5241364AUIDE5', data
    assert data['uniq_id'] == '81D62010EE0AF375BDF5952CDC3757A1', data
    assert data['mode'] == 'base', data
    print(f'[2] info OK  {data["firmware"]} / SN {data["sn"]} / 模式 {data["mode"]}')

    # 3) 模式查询
    ok, message, data = driver.get_mode()
    assert ok and data['mode'] == 'base' and data['mode_code'] == 2
    print(f'[3] get_mode OK  {data["mode"]}（{data["mode_code"]}）')

    # 4) 坐标配置回读（Survey-in 下 ECEF 为 0，必须能识别出来）
    ok, message, data = driver.get_svin_config()
    assert ok, message
    assert data['mode'] == 'survey_in' and data['count'] == 3600
    assert data['acc_limit'] == 1.2 and data['distance'] == 0.0
    assert data['ecef_x'] == 0.0
    ok2, message2, _ = driver.get_fixed_ecef()
    assert ok2 and '不是固定模式' in message2
    print(f'[4] get_svin_config OK  mode={data["mode"]} count={data["count"]} acc={data["acc_limit"]}')

    # 5) ERROR 不重试（方案 §4.3）：只允许发一次
    channel = _fake_channel()
    driver, _m, _a = match_driver(channel)
    channel._serial.error = {'PQTMCFGSVIN'}
    before = len([w for w in channel._serial.writes if w.startswith('PQTMCFGSVIN')])
    ok, message, data = driver.get_svin_config()
    after = len([w for w in channel._serial.writes if w.startswith('PQTMCFGSVIN')])
    assert not ok and data.get('err_code') == 1, (message, data)
    assert after - before == 1, f'ERROR 被重试了 {after - before} 次'
    print(f'[5] ERROR 不重试 OK  {message}')

    # 6) 超时才重试：识别类 3 次重试 = 共 4 次下发
    channel = _fake_channel()
    channel._serial.silent = {'PQTMVERNO'}
    channel.send('PQTMVERNO', timeout=0.05, retries=3, interval=0.01)
    sends = len([w for w in channel._serial.writes if w == 'PQTMVERNO'])
    assert sends == 4, sends
    print(f'[6] 超时重试 OK  1 次首发 + 3 次重试 = {sends} 次')

    # 7) 重启判定：SRR 无应答 → 关串口 → 重开 → 等 $PQTMVER 首帧
    channel = _fake_channel()
    driver, _m, _a = match_driver(channel)
    ok, message, data = driver.reboot()
    assert ok, message
    assert (data.get('ver') or {}).get('ver_str', '').startswith('LG290P'), data
    print(f'[7] 重启判定 OK  {message}  ver={data["ver"].get("ver_str")}')

    # 8) 型号不匹配必须拒绝（插错模块时不能"将错就错"往下配）
    #    用一个**任何驱动都不认**的固件串：LG290P 和 LC29H(BS) 都在注册表里，
    #    用 LC29HBS 的串会反而被 lc29hbs 认领（这是对的，所以不能拿它测"拒绝"）。
    channel = _fake_channel()
    channel._serial._reply = lambda name, body: (
        'PQTMVERNO,GNSSUNKNOWN01A01S,2023/01/01,00:00:00'
        if name == 'PQTMVERNO' else f'{name},OK')
    driver, message, _attempts = match_driver(channel)
    assert driver is None and '没有驱动认领' in message, message
    print(f'[8] 型号拒绝 OK  {message}')

    # 8b) LC29H(BS) 必须被 lc29hbs 认领，而不是被 lg290p 拒掉后整体失败
    channel = _fake_channel()
    channel._serial._reply = lambda name, body: (
        'PQTMVERNO,LC29HBSNR01A01S,2022/08/31,15:22:59'
        if name == 'PQTMVERNO' else f'{name},OK')
    driver, message, _attempts = match_driver(channel)
    assert driver is not None and driver.MODEL_KEY == 'lc29hbs', message
    print(f'[8b] LC29H(BS) 识别 OK  {message}')

    # 8c) LC29H(AA) 不能冒充 LC29H(BS)：命令集不一样
    channel = _fake_channel()
    channel._serial._reply = lambda name, body: (
        'PQTMVERNO,LC29HAANR11A01S,2022/08/31,15:22:59'
        if name == 'PQTMVERNO' else f'{name},OK')
    driver, _message, _attempts = match_driver(channel)
    assert driver is None, 'LC29H(AA) 被当成了 LC29H(BS)'
    print('[8c] LC29H(AA) 拒绝 OK  不冒充 BS 型号')

    # 8d) 隐藏型号（UM982）自动探测时要跳过，否则会"识别成功但下拉里没这个选项"
    from .unicore_n4 import UnicoreUM982Driver as _HID
    _hid_ch = _fake_channel()
    _hid_ch._serial._reply = lambda name, body: (
        '#VERSIONA,94,GPS,FINE,2190,1,0,0,19,160;'
        '"UM982","R4.10Build5251","HRPT00","PN","efuse","2021/11/26"*e195b254'
        if name == 'VERSION' else f'{name},OK')
    _hid_drv = _HID(_hid_ch)
    _ok, _msg, _ = _hid_drv.detect()
    assert _ok, f'UM982 驱动本身必须还能识别（只是不对前端暴露）：{_msg}'
    # 自动探测（only=None）的候选里不能有它
    auto_candidates = [k for k, c in _REGISTRY.items()
                       if not getattr(c, 'HIDDEN', False)]
    assert 'um982' not in auto_candidates, auto_candidates
    print('[8d] 隐藏型号 OK  UM982 驱动仍可识别（历史回填用），但不在可见清单')

    # 9) 元数据按双模/单模分组
    modules = list_supported_modules()
    assert modules['dual_mode'] and modules['single_mode']
    dual_ids = [item['id'] for item in modules['dual_mode']]
    single_ids = [item['id'] for item in modules['single_mode']]
    assert 'lg290p' in dual_ids and 'lc29hbs' in single_ids, (dual_ids, single_ids)
    # N4 那份手册里 4 个型号各不相同（命令集是包含关系，不是同一套），
    # 必须各自是独立型号 —— 合成一个 'unicore_n4' 的话，前端选型号时
    # 用户分不出 UM960/UM980，落库也只有一个键，后期接新型号无处可加。
    for key in ('um960', 'um960l', 'um980'):
        assert key in dual_ids, f'N4 型号 {key} 没注册成独立型号：{dual_ids}'
    # UM982 是双天线定向板，不适合做基准站 → HIDDEN，前端下拉里不能出现。
    # 但注册表里必须还在（历史记录型号键是 um982 的老数据要能回填）。
    assert 'um982' not in dual_ids and 'um982' not in [
        i['id'] for i in modules['all']], 'UM982 应从可见清单里隐藏'
    assert 'um982' in supported_driver_ids(), 'UM982 隐藏 ≠ 注销，注册表里要还在'
    assert 'um982' not in visible_driver_ids()
    # 显示名用英文厂商名，中文名容易和型号混在一起看不清
    for item in modules['all']:
        assert '和芯星通' not in item['name'], f'{item["id"]} 显示名还含中文厂商名：{item["name"]}'
    assert 'svin' in modules['dual_mode'][0]['caps']
    # 每个型号都要带自己的官方波特率清单（前端据此给候选）
    for item in modules['all']:
        assert item['bauds'], f'{item["id"]} 没有声明 BAUDS'
        assert item['bauds'] == sorted(item['bauds'], reverse=True), item['bauds']
        assert item['default_baud'] in item['bauds'], item
    lg = [i for i in modules['all'] if i['id'] == 'lg290p'][0]
    bs = [i for i in modules['all'] if i['id'] == 'lc29hbs'][0]
    n4 = [i for i in modules['all'] if i['id'] == 'um980'][0]
    # 清单必须各不相同 —— 否则"每型号存一份"就没意义了
    assert 3000000 in bs['bauds'] and 3000000 not in lg['bauds'], (lg['bauds'], bs['bauds'])
    assert 19200 in n4['bauds'] and 19200 not in lg['bauds'], (lg['bauds'], n4['bauds'])
    assert 4800 in bs['bauds'] and 4800 not in n4['bauds'], (n4['bauds'], bs['bauds'])
    # 同一份手册拆出来的几个型号共用波特率表（表 4-4 对 COM1/2/3 一致），
    # 但命令集不同 —— 下面 9h) 专门验这一点。
    n4_bauds = n4['bauds']
    for key in ('um960', 'um960l'):
        same = [i for i in modules['all'] if i['id'] == key][0]
        assert same['bauds'] == n4_bauds, (key, same['bauds'], n4_bauds)
    print(f'[9] 型号清单 OK  双模 {len(dual_ids)} 个 / 单模 {len(single_ids)} 个；'
          f'N4 三型号可见（UM982 已隐藏）；三份波特率清单互不相同')

    # 9b) N4 型号判定：#VERSIONA 里查产品名，同门老型号必须被拒
    #     `parse_version` 是纯静态解析，四个子类继承自同一个基类，取哪个都行。
    from .unicore_n4 import (LEGACY_PRODUCTS, PRODUCT_MODELS,
                             UnicoreUM982Driver)
    v982 = UnicoreUM982Driver.parse_version(
        '#VERSIONA,94,GPS,FINE,2190,117325000,0,0,18,160;'
        '"UM982","R4.10Build5251","HRPT00-S10C-P","-","ffff48ffff0fffff","2021/11/26"*e195b254')
    assert v982['product'] == 'UM982', v982
    assert v982['firmware'] == 'R4.10Build5251', v982
    assert v982['build_date'] == '2021/11/26', v982
    v960 = UnicoreUM982Driver.parse_version(
        '#VERSIONA,94,GPS,FINE,2190,1,0,0,19,160;'
        '"UM960","R4.10Build1","AB","XXXX","efuse","2024-01-01"*12')
    assert v960['product'] == 'UM960', v960
    # 老型号（UM4B0 等）不能冒充 N4
    legacy = UnicoreUM982Driver.parse_version(
        '#VERSIONA,1,GPS,FINE,1,1,0,0,1,1;"UM4B0","R1.0","A","B","C","2020/01/01"*12')
    assert legacy['product'] in LEGACY_PRODUCTS, legacy
    assert legacy['product'] not in PRODUCT_MODELS, legacy
    print(f'[9b] N4 型号判定 OK  识别 UM982/UM960，拒绝老型号 {legacy["product"]}')

    # 9c) N4 是"简化 ASCII"：裸发、无 $、空格分隔；应答带 $/# 两种头
    channel = _fake_channel()
    n4_driver = UnicoreUM982Driver(channel)
    assert n4_driver.FRAME_COMMANDS is False
    assert set(n4_driver.LINE_HEADS) == {'$', '#'}
    channel.write_line('MODE BASE TIME 60')
    assert channel._serial.writes[-1] == 'MODE BASE TIME 60', channel._serial.writes[-1]
    # 命令名提取必须能吃空格分隔
    from .base import command_name, message_name as _mname, policy_for
    assert command_name('MODE BASE TIME 60') == 'MODE', command_name('MODE BASE TIME 60')
    assert command_name('UNLOG COM2 GPGGA') == 'UNLOG'
    assert command_name('CONFIG COM1 115200') == 'CONFIG'
    assert policy_for('VERSION') == 'identify'
    assert policy_for('SAVECONFIG') == 'save'
    assert _mname('#VERSIONA,GPS,"UM982"*12') == 'VERSIONA'
    assert _mname('$CONFIG,COM1,CONFIG COM1 115200*65') == 'CONFIG'
    print('[9c] N4 简化 ASCII OK  裸发无校验和、空格分隔、$/# 双头都能识别')

    # 9d) N4 的 ECEF↔经纬高互转（流程层只给经纬高，模块要 ECEF 或反之）
    from .unicore_n4 import ecef_to_geodetic, geodetic_to_ecef
    x, y, z = -2005559.8530, 5411823.2016, 2706139.4032
    lat, lon, h = ecef_to_geodetic(x, y, z)
    assert abs(lat - 25.3) < 2, lat
    assert abs(lon - 110.3) < 2, lon
    bx, by, bz = geodetic_to_ecef(lat, lon, h)
    assert abs(bx - x) < 1e-3 and abs(by - y) < 1e-3 and abs(bz - z) < 1e-3, (bx, by, bz)
    print(f'[9d] N4 ECEF 互转 OK  ({lat:.6f}°, {lon:.6f}°, {h:.3f}m) 往返误差 <1mm')

    # 9e) 命令名与应答名不一致时的识别（VERSION → #VERSIONA）
    #     不加这张别名表，classify() 认不出应答，现场表现是"模块明明回了还报超时"。
    from .unicore_n4 import UnicoreUM982Driver as _N4
    _ch = _fake_channel()
    _d = _N4(_ch)
    _ver = ('#VERSIONA,94,GPS,FINE,2190,117325000,0,0,18,160;'
            '"UM982","R4.10Build5251","HRPT00-S10C-P","PN-SN","ffff48","2021/11/26"*e195b254')
    assert _d.channel.classify(_ver, 'VERSION', 'VERSION') == 'ok', 'VERSIONA 应答没被认领'
    assert _d.channel.classify('#MODE,95,BASE*1B', 'MODE', 'MODE') == 'ok'
    # 无关语句不能被误认（否则永远读到别人的应答）
    assert _d.channel.classify('#MODE,95,BASE*1B', 'VERSION', 'VERSION') == 'other'
    print('[9e] N4 应答别名 OK  VERSION→#VERSIONA 可识别，无关语句不误认')

    # 9f) N4 模式文本的两种应答形态（有无 `;`）都要解析对
    #     变体一律小写 —— 流程层拿它做 `== 'fixed'` 之类的比较，
    #     返回手册原文的 `DEFAULT` 会让所有变体判断静默失效。
    cases = [
        ('#MODE,95,BASE*1B', 'base', 'default'),
        ('#MODE,95,ROVER*1B', 'rover', ''),
        ('#MODE,1,2,3;MODE ROVER SURVEY,*1B', 'rover', 'survey'),
        ('#MODE,1,2,3;MODE BASE TIME 60,*1B', 'base', 'time'),
        ('#MODE,1,2,3;MODE HEADING2,*1B', 'heading2', ''),
        ('#MODE,1,2,3;MODE BASE 1234 25.3 110.3 50,*1B', 'base', 'fixed'),
    ]
    for raw, want_mode, want_variant in cases:
        got = _N4._classify_mode(_N4._mode_text(raw))
        assert got == (want_mode, want_variant), (raw, got, want_mode, want_variant)
    print('[9f] N4 模式解析 OK  带/不带 `;` 的两种应答、BASE/ROVER/HEADING2 全对')

    # 9h) 一个手册四个型号：命令集必须按型号裁剪，不能"四合一"共用一张表。
    #     这是本轮拆分的核心 —— 流程层只认语义键（set_rover / save / ...），
    #     哪个型号有哪个键由各自的 CMD 表决定；驱动里没有 `if 型号 ==` 分支。
    from .unicore_n4 import _REGISTRY as _n4_reg
    base_keys = set(_n4_reg['um960l'].CMD)          # UM960L 是手册里功能最少的
    for key in ('um960', 'um980', 'um982'):
        extra = set(_n4_reg[key].CMD) - base_keys
        assert not (base_keys - set(_n4_reg[key].CMD)), f'{key} 缺公共命令'
        assert extra, f'{key} 和 UM960L 命令集一模一样 —— 裁剪没生效'
    # 型号独占项：写错归属就会往不认识的板子发命令
    assert 'set_heading_engine' in _n4_reg['um982'].CMD, 'CONFIG HEADING 仅 UM982'
    for key in ('um960', 'um960l', 'um980'):
        assert 'set_heading_engine' not in _n4_reg[key].CMD, key
    assert 'set_mmp' in _n4_reg['um980'].CMD, 'CONFIG MMP 仅 UM980'
    assert 'set_psrveldrpos' in _n4_reg['um980'].CMD, 'CONFIG PSRVELDRPOS 仅 UM980'
    for key in ('um960', 'um960l', 'um982'):
        assert 'set_mmp' not in _n4_reg[key].CMD, key
        assert 'set_psrveldrpos' not in _n4_reg[key].CMD, key
    # UM960L 手册 §3.6/§3.7 的适用产品不含它
    assert 'set_rover' not in _n4_reg['um960l'].CMD, 'UM960L 没有流动站模式'
    assert 'set_heading2' not in _n4_reg['um960l'].CMD, 'UM960L 没有 Heading2'
    for key in ('um960', 'um980', 'um982'):
        assert 'set_rover' in _n4_reg[key].CMD and 'set_heading2' in _n4_reg[key].CMD, key
    # 命令串不能串厂家：N4 的简化 ASCII 里不该出现 Quectel 的 $PQTM
    for key in _n4_reg:
        for sem, body in _n4_reg[key].CMD.items():
            assert isinstance(body, str) and body, (key, sem)
            assert not body.startswith('$'), f'{key}.{sem} 带了 $（那是 Quectel 的写法）'
            assert '*' not in body, f'{key}.{sem} 带了校验和段（N4 无校验位）'
    print('[9h] N4 型号裁剪 OK  4 型号独立命令集；独占项不串型号；命令串无 $ / 校验和')

    # 9g) MSM 报文号：1074 + MSM7 必须是 1077，不能是 1078
    #     （`base + level` 的写法会得到 1078 —— 一个不存在的报文号，模块静默忽略）
    from .unicore_n4 import _MSM_BASES, _rtcm_name_number
    assert _MSM_BASES['GPS'] == 1074, _MSM_BASES
    for level, want in ((4, 1074), (7, 1077)):
        assert _MSM_BASES['GPS'] - _MSM_BASES['GPS'] % 10 + level == want
    assert _rtcm_name_number('RTCM3-107X') == 1074
    assert _rtcm_name_number('RTCM3-112X') == 1124
    assert _rtcm_name_number('RTCM3-1005') == 1005
    print('[9g] N4 RTCM 报文号 OK  107X→1074、1074/1077 档位不串号')

    # 10) $PQTMSVINSTATUS 解析：<Valid> 必须落到 state，不能被"解析成功"覆盖
    from .base import parse_line
    mid = parse_line('$PQTMSVINSTATUS,1,291322000,1,,11,59,60,'
                     '-2005559.8530,5411823.2016,2706139.4032,1.7830*31')
    assert mid['state'] == 1 and mid['valid'] is True, mid
    assert mid['obs'] == 59 and mid['cfg_count'] == 60, mid
    assert mid['done'] is False and abs(mid['progress'] - 59 / 60) < 1e-9, mid
    done = parse_line('$PQTMSVINSTATUS,1,291323000,2,,11,60,60,'
                      '-2005559.8481,5411823.1873,2706139.3995,1.8075*3F')
    assert done['state'] == 2 and done['done'] is True and done['progress'] == 1.0, done
    assert done['mean_acc'] == 1.8075, done
    print(f'[10] SVINSTATUS OK  59/60→{mid["valid_text"]}，60/60→{done["valid_text"]}')

    # 11) 开机帧 $PQTMVER(6 字段) 与查询应答 $PQTMVERNO(5 字段) 不能混
    #     真机原文：SRR 后 5.67 s 出现，`1` 是 MsgVer、`MODULE` 是 VerName
    boot = parse_line('$PQTMVER,1,MODULE,LG290P03AANR01A03S,2024/05/22,18:09:06*36')
    assert boot['type'] == 'VER' and boot['boot'] is True, boot
    assert boot['ver_str'].startswith('LG290P'), boot
    assert boot['msg_ver'] == '1' and boot['ver_name'] == 'MODULE', boot
    query = parse_line('$PQTMVERNO,LG290P03AANR01A03S,2024/05/22,18:09:06*1C')
    assert query['type'] == 'VERNO', query
    print('[11] 开机帧/查询应答区分 OK  $PQTMVER 6 字段，$PQTMVERNO 5 字段')

    # 12) 速率：<Rate> 是"每 N 次定位一次"，范围按语句类型取，PQTM 自动补 MsgVer
    from .quectel_lg290p import msg_ver_for, rate_range
    assert rate_range('GGA') == (0, 255), 'NMEA 速率下限是 0（0 = 不输出）'
    assert rate_range('RTCM3-1005') == (0, 1200), 'RTCM3 速率下限是 0（还原要能关掉 1005）'
    assert rate_range('RTCM3-1019') == (0, 1)
    assert msg_ver_for('PQTMSVINSTATUS') == 1 and msg_ver_for('PQTMEPE') == 2
    assert msg_ver_for('GGA') is None, 'NMEA 语句必须省略 MsgVer'
    channel = _fake_channel()
    driver, _m, _a = match_driver(channel)
    ok, message, _ = driver.set_message_rate('GGA', 300)
    assert not ok and '255' in message, message
    ok, message, _ = driver.set_message_rate('RTCM3-1019', 5)
    assert not ok and '0~1' in message, message
    driver.set_message_rate('PQTMSVINSTATUS', 1)
    last = channel._serial.writes[-1]
    assert last == 'PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1', last
    print('[12] 速率规则 OK  范围按类型，PQTM 自动补 MsgVer')

    # 13) 查询命令必须走 query 档（带参数的 R 命令别被当成配置命令）
    assert policy_for('PQTMCFGRTCM,R') == 'query'
    assert policy_for('PQTMCFGMSGRATE,R,GGA') == 'query'
    assert policy_for('PQTMCFGMSGRATE,R,PQTMEPE,2') == 'query'
    assert policy_for('PQTMCFGMSGRATE,W,GGA,1') == 'config'
    assert policy_for('PQTMSAVEPAR') == 'save'
    print('[13] 查询策略识别 OK  ,R 与 ,R,<参数> 都归 query')

    # 14) 波特率候选合流：手填值优先，型号官方档位兜底，自动探测取并集。
    #     这里是"驱动存官方档位、前端可手改"两条需求的交汇点，最容易两边各说各话：
    #     驱动改了个 BAUDS，前端提示换了，而采集端还在走旧表 —— 没人会发现。
    from ..service import CalibrationService
    got = CalibrationService._baud_candidates(['um982'], current=57600)
    assert got and got[0] == 57600, got
    assert 19200 in got and 3000000 not in got, 'N4 不该去试 LC29H 才有的 3000000'
    # 多型号并集：选中同门的 4 个型号时不能丢档位，也不能混进别家的
    got = CalibrationService._baud_candidates(
        ['um960', 'um960l', 'um980', 'um982'], current=None)
    assert 19200 in got and 3000000 not in got, got
    got = CalibrationService._baud_candidates(['lg290p'], current=115200)
    assert got[0] == 115200 and 4800 not in got, got
    got = CalibrationService._baud_candidates(['lc29hbs'], current=None)
    assert 3000000 in got and 4800 in got, 'LC29H(BS) 的 4800/3000000 全局限表里没有'
    got = CalibrationService._baud_candidates(None, current=19200)
    assert got[0] == 19200 and len(got) == len(set(got)), got
    print('[14] 波特率候选合流 OK  手填值置顶 + 型号官方档位 + 自动探测并集')

    # 15) `baud_explicit` 必须按"用户填没填"判定，不能按"值等不等于默认"。
    #     手填 115200（恰好等于默认）若被判成"没填"，识别失败时程序会去
    #     自行换档，把用户刚刚做过的排除动作整个推翻。
    r = CalibrationService._resolve_baud
    from ..service import DEFAULT_BAUD
    assert r(None) == (int(DEFAULT_BAUD), False, ''), r(None)
    assert r('') == (int(DEFAULT_BAUD), False, ''), r('')
    assert r('  ')[0] == int(DEFAULT_BAUD), r('  ')
    assert r(115200) == (115200, True, ''), r(115200)
    assert r('460800') == (460800, True, ''), r('460800')
    assert r(True)[0] is None, 'True 会被 int() 当成 1，必须拒掉'
    assert r(0)[0] is None and r('abc')[0] is None, '0 与非数字都必须拒掉'
    print('[15] 波特率显式标记 OK  留空=自动(可换档)，手填 115200 也算"用户指定"')

    print('=== 驱动层离线自测全部通过 ===')
    return 0


# ==================== 真机探测入口 ====================
# 全局兜底候选表：只在**还不知道是哪个型号**时用（各驱动自己的 BAUDS 优先）。
# 顺序有讲究：115200 是绝大多数模块的出厂默认，460800/921600 是工程上常见的提速档。
DEFAULT_BAUDS = (115200, 460800, 921600, 230400, 38400, 9600)


def detect_bauds(model_key=None):
    """采集/识别该用的波特率候选顺序。

    传了 model_key 且该驱动声明了 BAUDS → 用厂商文档里的清单（降序）；
    否则退回全局 DEFAULT_BAUDS。
    """
    if model_key:
        driver = get_driver_by_id(model_key)
        if driver is not None:
            table = driver.bauds()
            if table:
                return list(table)
    return list(DEFAULT_BAUDS)


def probe(port, baud=None, bauds=None, verbose=True, model_key=None):
    """打开串口 → 识别模块 → 读版本/SN/模式/坐标配置。返回进程退出码。

    这就是"插一块模块就能验证查询类命令"的那条路：
    本阶段的所有查询命令（VERNO/SN/UNIQID/RCVRMODE/CFGSVIN）都在这里跑一遍。
    已知型号时传 model_key，候选波特率就按该型号的官方清单来试。
    """
    candidates = [baud] if baud else list(bauds or detect_bauds(model_key))
    channel = Channel(port=port, baud=int(candidates[0]))

    driver = None
    for candidate in candidates:
        channel.baud = int(candidate)
        try:
            channel.open()
        except Exception as e:
            print(f'× 打开 {port} @ {candidate} 失败：{e}')
            return 2
        channel.drain()
        driver, message, _attempts = match_driver(channel, only=[model_key] if model_key else None)
        if driver is not None:
            print(f'✓ {port} @ {candidate}  {message}')
            break
        channel.close()
        if verbose:
            print(f'× {candidate} 未识别到已知模块：{message}')

    if driver is None:
        print(f'× 在 {port} 上试过 {candidates} 都没识别到模块。')
        print('  排查顺序：① 模块是否已上电 ② TX/RX 是否交叉 ③ 波特率 ④ 流控（部分模块默认开 RTS/CTS）')
        return 3

    def show(title, ok, message, data, keys):
        flag = '✓' if ok else '×'
        print(f'{flag} {title}: {message}')
        if ok and keys:
            for key in keys:
                if key in data:
                    print(f'    {key} = {data[key]}')

    echo = print if verbose else (lambda *a, **k: None)

    ok, message, data = driver.info()
    echo(f'\n--- 模块信息（{driver.DISPLAY_NAME}）---')
    if ok:
        for key in ('firmware', 'build_date', 'sn', 'uniq_id'):
            echo(f'  {key:10s} = {data.get(key, "")}')
        source = data.get('sn_source') or ''
        echo(f'  {"sn来源":8s} = {source or "(无)"}')
        echo(f'  {"mode":10s} = {data.get("mode")}')
        if data.get('sn_error'):
            echo(f'  提示：{data["sn_error"]}')
    else:
        echo(f'  × {message}')

    echo('\n--- 当前坐标配置 ---')
    ok, message, data = driver.get_svin_config()
    if ok:
        echo(f'  mode      = {data.get("mode")}（{data.get("mode_code")}）')
        echo(f'  count     = {data.get("count")}')
        echo(f'  acc_limit = {data.get("acc_limit")}')
        echo(f'  ECEF      = {data.get("ecef_x")}, {data.get("ecef_y")}, {data.get("ecef_z")}')
        if data.get('distance') is not None:
            echo(f'  distance  = {data.get("distance")}')
        if data.get('mode') == 'survey_in':
            echo('  提示：Survey-in 模式下 ECEF 恒为 0.0000，不是收敛结果，要看 1005 或 SVINSTATUS')
    else:
        echo(f'  × {message}')

    echo('\n--- 能力集（运行时探测结果，不是文档抄的）---')
    # 必须打 runtime_caps：类上的 CAPS 只是文档声明值，真实硬件可能推翻它
    echo('  ' + '  '.join(f'{k}={v}' for k, v in driver.runtime_caps.items()))

    echo(f'\n完成。本次共解析 {channel.counters["lines"]} 行 NMEA / '
         f'{channel.counters["frames"]} 个 RTCM 帧 / 丢弃 {channel.counters["noise"]} 字节噪声')
    channel.close()
    return 0


def main(argv=None):
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog='python -m rtkserver.calibration.drivers.driver_manager',
        description='标定驱动：列型号 / 真机探测（阶段1只做查询类命令，不写模块配置）')
    parser.add_argument('-l', '--list', action='store_true', help='列出已注册的驱动型号')
    parser.add_argument('--port', help='串口设备，例如 COM3 或 /dev/ttyUSB0')
    parser.add_argument('--baud', type=int,
                        help='波特率；不填则按型号官方清单逐档尝试（未指定型号时用全局兜底表）')
    parser.add_argument('--model', help='已知型号键（配合 --baud 省略时按该型号官方波特率清单逐档试）')
    parser.add_argument('--selftest', action='store_true', help='离线自测（假模块，不需要硬件）')
    args = parser.parse_args(argv)

    if args.selftest:
        logging.basicConfig(level=logging.ERROR)
        return _selftest()

    if args.list or not args.port:
        modules = list_supported_modules()
        print('已注册驱动：')
        for group, title in (('dual_mode', '双模（可切移动站/基站）'), ('single_mode', '单模')):
            print(f'\n[{title}]')
            if not modules[group]:
                print('  （暂无）')
            for item in modules[group]:
                caps = ' '.join(f'{k}' for k, v in item['caps'].items() if v)
                print(f'  {item["id"]:12s} {item["name"]:20s} 前缀={item["match_prefix"]:8s} '
                      f'命令={item["command_count"]:2d}  能力={caps}')
                if item['bauds']:
                    print(f'  {"":12s} 官方波特率 {" / ".join(str(b) for b in item["bauds"])}'
                          f'（默认 {item["default_baud"]}）')
        if not args.port:
            print('\n提示：加 --port COM3（或 /dev/ttyUSB0）做真机探测。')
        return 0

    logging.basicConfig(level=logging.WARNING)
    return probe(args.port, args.baud, model_key=args.model)


if __name__ == '__main__':
    # `python -m ...driver_manager` 会把本文件以 `__main__` 身份加载，于是
    # 包里的 rtkserver.calibration.drivers.driver_manager 成了**另一个模块对象**：
    # @register 登记的注册表在那边，这边查到的永远是空表。
    # 所以入口一律转发给规范模块，命令行与 import 走同一份注册表。
    from rtkserver.calibration.drivers import driver_manager as _canonical
    raise SystemExit(_canonical.main())
