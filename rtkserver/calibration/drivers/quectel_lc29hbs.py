# -*- coding: utf-8 -*-
"""Quectel LC29H(BS) 驱动 —— PQTM + PAIR 双命令族。

命令来源：《LC29H (BS) GNSS 协议规范》（Rev. LC29H(BS)_Protocol_Specification_V1.0，
2022-08-31），每处都标了章节号，改之前先去文档核对。

## 和 LG290P 的四个关键差异（写代码时最容易被"套用"坑到的地方）

1. **没有 `PQTMCFGRCVRMODE`**。
   LC29H(BS) 是**单基站模块**，工作模式由 `PQTMCFGSVIN` 的 `<Mode>` 字段决定：
   `0=禁用 / 1=测量(Survey-in) / 2=固定(手动坐标)`。
   照抄 LG290P 的 `set_rover` / `set_base` 会直接收到 `ERROR,3`（该命令不支持）。

2. **`PQTMCFGSVIN` 只有 7 个字段**，没有 `distance`（那一段是 LG290P 的
   `…,0,0,0,{distance}`）。多写一个字段 → `ERROR,1`。

3. **`PQTMCFGMSGRATE` 必须带 `<MsgVer>`**，而且**只支持两条语句**：
   `PQTMSVINSTATUS`(1~20) 与 `PQTMEPE`(1~20)。
   想配 GGA/RMC 的速率会 `ERROR,1` —— 这个型号的 NMEA 输出速率不能逐条改。

4. **RTCM 输出走 PAIR 族**，不是 `PQTMCFGRTCM`：
   `PAIR432` 开关 RTCM（-1 禁用 / 0 MSM4 / 1 MSM7）、`PAIR434` 开关 1005、
   `PAIR436` 开关星历、`PAIR864` 改波特率。
   所有 PAIR 设置命令的应答是统一的两条：`$PAIR001,<命令号>,<结果码>`。

## 结果码（§2.3.1 PAIR001）

    0 = 成功    1 = 处理中   2 = 失败
    3 = 不支持  4 = 参数错误 5 = MNL 忙

`3` 要单独识别：它意味着"这块固件没实现这条命令"，应把对应能力位关掉，
而不是当成"参数填错"反复重试。
"""
import logging

from .base import BaseDriver
from .driver_manager import register

# ==================== 命令表（语义键 → 命令体） ====================
CMD = {
    # ---- 识别（§2.2.4）----
    # 回 `$PQTMVERNO,<VerStr>,<BuildDate>,<BuildTime>`
    # VerStr 形如 `LC29HBSNR01A01S`，前 6 位 `LC29HB` 就是型号标识
    'identify':         'PQTMVERNO',

    # ---- 测量 / 固定坐标（§2.2.1 PQTMCFGSVIN，7 字段！）----
    'get_svin':         'PQTMCFGSVIN,R',
    # Mode=1 测量模式：CFG_CNT=最少定位次数，AccLimit=3D 精度上限，ECEF 三轴填 0
    'set_svin':         'PQTMCFGSVIN,W,1,{count},{acc},0,0,0',
    # Mode=2 固定模式：CFG_CNT 与 AccLimit 填 0，后三个是真坐标
    'set_fixed':        'PQTMCFGSVIN,W,2,0,0,{x},{y},{z}',
    'disable_svin':     'PQTMCFGSVIN,W,0,0,0,0,0,0',

    # ---- 输出速率（§2.2.5，仅 PQTMSVINSTATUS / PQTMEPE，必须带 MsgVer）----
    'msg_rate_ver':     'PQTMCFGMSGRATE,W,{name},{rate},{ver}',
    'get_msg_rate_ver': 'PQTMCFGMSGRATE,R,{name},{ver}',
    # 本型号支持的两条语句及其 MsgVer（文档 §2.2.5 表：都固定为 1）
    'rate_svin':        'PQTMCFGMSGRATE,W,PQTMSVINSTATUS,{rate},1',
    'rate_epe':         'PQTMCFGMSGRATE,W,PQTMEPE,{rate},2',

    # ---- RTCM 输出（§2.3.2 ~ §2.3.7，PAIR 族）----
    'rtcm_mode':        'PAIR432,{mode}',      # -1 禁用 / 0 MSM4 / 1 MSM7
    'get_rtcm_mode':    'PAIR433',
    'ant_pnt':          'PAIR434,{enable}',    # 1005 静态天线参考点 0/1
    'get_ant_pnt':      'PAIR435',
    'ephemeris':        'PAIR436,{enable}',    # 星历输出 0/1
    'get_ephemeris':    'PAIR437',

    # ---- 串口（§2.3.8）----
    # PortType 固定 0(UART)，PortIndex 固定 0(UART1)；第三段才是波特率
    'set_baud':         'PAIR864,0,0,{baud}',

    # ---- 工具（§2.2.2 / §2.2.3）----
    'save':             'PQTMSAVEPAR',
    'factory_reset':    'PQTMRESTOREPAR',
}

# 文档给过校验和的**无参数**命令（有参数的不填：参数一变校验和就变）
CS_EXPECT = {
    'identify':     '58',   # §2.2.4
    'get_svin':     '26',   # §2.2.1
    'get_rtcm_mode': '3E',  # §2.3.3
    'get_ant_pnt':  '38',   # §2.3.5
    'get_ephemeris': '3A',  # §2.3.7
    'save':         '5A',   # §2.2.2
    'factory_reset': '13',  # §2.2.3
}

# ==================== 能力位 ====================
# 逐条对着文档判断，不要"看起来像就有"：
#   dual_mode  False —— 单基站模块，没有流动站模式（连 RCVRMODE 命令都没有）
#   set_mode   False —— 同上，模式由 CFGSVIN 的 Mode 字段决定
#   svin       True  —— §2.2.1 Mode=1
#   fixed      True  —— §2.2.1 Mode=2
#   ant_info   False —— 文档没有 1033 接收机/天线说明命令
#   ant_delta  False —— 没有 1006 DF028 天线偏移（RTK 测量才用，本项目也不要）
#   msg_rate   True  —— 但只有 PQTMSVINSTATUS / PQTMEPE 两条，见 RATE_TARGETS
#   rtcm_out   True  —— 走 PAIR432/434/436
#   err3       True  —— PAIR001 会回结果码 3（不支持）
CAPS = {
    'dual_mode': False,
    'set_mode': False,
    'svin': True,
    'fixed': True,
    'ant_info': False,
    'ant_delta': False,
    'msg_rate': True,
    'rtcm_out': True,
    'err3': True,
}

# PAIR001 的结果码（§2.3.1）
PAIR_RESULT = {
    0: '成功',
    1: '处理中',
    2: '失败',
    3: '该命令不支持（固件未实现）',
    4: '参数错误',
    5: 'MNL 忙',
}

# 本型号能配速率的语句（§2.2.5 表 7），其它一律不支持
RATE_TARGETS = ('PQTMSVINSTATUS', 'PQTMEPE')
RATE_RANGE = (1, 20)          # §2.2.5：Rate 取值 1~20

# RTCM 输出模式（§2.3.2 <Mode>）
RTCM_MODE_LABEL = {-1: '禁用', 0: 'MSM4', 1: 'MSM7'}


@register
class LC29HBSDriver(BaseDriver):
    """LC29H(BS)：单基站 GNSS 模块，PQTM（配置）+ PAIR（RTCM/串口）双命令族。"""

    MODEL_KEY = 'lc29hbs'
    DISPLAY_NAME = 'Quectel LC29H(BS)'
    # 固件串 `LC29HBSNR01A01S` 的前 6 位。不能写 'LC29H'：
    # LC29H(AA) 是流动站/基站双模模块，命令集不一样，两者必须分开认。
    MATCH_PREFIX = 'LC29HB'
    DUAL_MODE = False
    CAPS = CAPS
    CMD = CMD
    CS_EXPECT = CS_EXPECT

    # 官方波特率（§2.3.8 PAIR864 的 <Baudrate> 取值范围，文档原样十档）。
    # 注意 4800 与 3000000 是 LG290P 根本没有的档位 —— 这就是"每型号存一份"
    # 的意义：拿 LG290P 的清单去探 LC29H(BS)，这两档永远试不到。
    BAUDS = (4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 3000000)
    DEFAULT_BAUD = 115200
    BAUD_CMD_KEY = 'set_baud'

    # 只有一个 MSM 开关（PAIR432 是全局开关，不分星座），
    # 所以星座清单故意留空：流程层看到空表就不逐星座下发速率。
    MSM_CONSTELLATIONS = ()

    def __init__(self, channel):
        super().__init__(channel)
        # PAIR 命令的应答是异步的（$PAIR001 在结果帧之后才到），
        # 这里缓存最近一次 PAIR001 的 (命令号, 结果码)，供 request 收尾时取用。
        self._last_pair = None

    # ==================== 一、识别与信息 ====================
    def detect(self):
        """§2.2.4：用 `PQTMVERNO` 的版本串前缀判断型号。

        为什么不查 SN：这个型号文档里**没有** PQTMSN 命令（LG290P 才有），
        版本串是最短、一定有应答的一条。
        """
        ok, message, data = self.request('identify')
        if not ok:
            return False, message, data

        fields = (data or {}).get('fields') or []
        firmware = (fields[0] if fields else '').strip()
        # 防御：`$PQTMVER` 开机帧和 `$PQTMVERNO` 只差三个字母，字段却整体错位
        # （开机帧第 1 个字段是 MsgVer="1"）。吃错了会得到"固件串 1"这种鬼结论。
        if not firmware or firmware[0].isdigit():
            return False, f'识别应答格式异常（首字段为 {firmware or "(空)"}），疑似收到开机帧', {
                'firmware': firmware, 'error': 'bad_reply'}

        upper = firmware.upper()
        if not upper.startswith(self.MATCH_PREFIX):
            return False, f'固件串 {firmware} 不属于 {self.DISPLAY_NAME}', data
        # 排除 LC29H(AA)：它也是 LC29H 开头，但命令集不同（双模模块，有 RCVRMODE）
        if upper.startswith('LC29HAA'):
            return False, f'固件串 {firmware} 是 LC29H(AA)，不是 LC29H(BS)', data

        return True, f'识别到 {firmware}', data

    def info(self):
        """版本 + 当前 SVIN 配置 + RTCM 输出状态，一次问全。

        这些查询各自独立：某一条不支持时只让对应字段为空，不整体失败 ——
        否则用户在页面上会看到"读不到模块信息"，其实只是 PAIR433 这种小项不通。
        """
        data = {}

        ok, message, ver = self.request('identify')
        if not ok:
            return False, message, data
        fields = (ver or {}).get('fields') or []
        firmware = (fields[0] if fields else '').strip()
        data['firmware'] = firmware
        data['version'] = firmware
        data['build_date'] = fields[1] if len(fields) > 1 else None
        data['build_time'] = fields[2] if len(fields) > 2 else None
        data['model'] = self.DISPLAY_NAME
        data['model_key'] = self.MODEL_KEY

        ok, _msg, svin = self.get_svin_config()
        if ok and svin:
            data['svin'] = svin
            data['mode'] = self._role_from_svin(svin)

        ok, _msg, rtcm = self.request('get_rtcm_mode')
        if ok and rtcm:
            data['rtcm_mode'] = rtcm.get('mode')

        ok, _msg, ant = self.request('get_ant_pnt')
        if ok and ant:
            data['ant_pnt'] = ant.get('enable')

        return True, firmware or '已读取', data

    # ==================== 二、工作模式 ====================
    def get_mode(self):
        """没有 RCVRMODE 命令，用 SVIN 的 `<Mode>` 反推当前角色。

        0/1 → 没在播固定坐标，对外就是"流动站/未固定"
        2   → 已经在播固定坐标，等同基站
        """
        ok, message, data = self.get_svin_config()
        if not ok:
            return False, message, {'mode': 'unknown'}
        mode = self._role_from_svin(data or {})
        data = dict(data or {})
        data['role'] = mode
        data['mode'] = mode
        return True, f'当前模式 {mode}', data

    def set_mode(self, mode):
        """切工作模式。

        `base` → 只是"进入基站身份"，坐标仍要靠 set_fixed_ecef 写；
                这里用 Mode=1（测量模式）让模块先动起来，与 LG290P 的流程对齐。
        `rover` → LC29H(BS) 是单基站模块，**没有流动站模式**，直接拒绝并说明原因。
        """
        want = str(mode or '').strip().lower()
        if want in ('rover', 'mobile', '流动站', '移动站'):
            return False, (
                f'{self.DISPLAY_NAME} 是单基站模块，不支持流动站模式'
                '（文档里没有 PQTMCFGRCVRMODE 命令）'), {'error': 'unsupported mode'}

        # 切基站 = 先把 SVIN 置为测量模式，具体坐标由后续 set_fixed 覆盖
        ok, message, data = self.request('set_svin', count=3600, acc=15.0)
        if not ok:
            return False, message, data
        return True, '已进入基站（测量模式，等待写入坐标）', data

    def set_mode_memory_only(self, mode):
        """只改配置不重启。LC29H(BS) 的 Mode 字段本身就即时生效，所以直接复用。"""
        return self.set_mode(mode)

    # ==================== 三、坐标与 Survey-in ====================
    def get_svin_config(self):
        """§2.2.1 查询：`$PQTMCFGSVIN,OK,<Mode>,<CFG_CNT>,<3D_AccLimit>,<X>,<Y>,<Z>`。

        **只有 7 个字段**（LG290P 是 8 个，多一个 `<Distance>`）。
        硬套 LG290P 的解析会给 `distance` 取到 None，不影响正确性，
        但为了不给后来人挖坑，这里按本文档的字段数单独实现。
        """
        ok, message, data = self.request('get_svin')
        if not ok:
            return False, message, data
        fields = (data or {}).get('fields') or []
        if len(fields) < 6:
            return False, f'PQTMCFGSVIN 应答字段不足：{message}', {'error': 'bad_reply'}

        mode_code = _to_int(fields[0])
        return True, message, {
            'mode_code': mode_code,
            # 注意：这里的 mode 是 SVIN 模式，不是"基站/流动站"角色
            'mode': {0: 'disabled', 1: 'survey_in', 2: 'fixed'}.get(mode_code, 'unknown'),
            'count': _to_int(fields[1]),
            'acc_limit': _to_float(fields[2]),
            'ecef_x': _to_float(fields[3]),
            'ecef_y': _to_float(fields[4]),
            'ecef_z': _to_float(fields[5]),
        }

    def get_fixed_ecef(self):
        """读当前配置的 ECEF 坐标（`<Mode>=2` 时才是真坐标）。"""
        ok, message, data = self.get_svin_config()
        if not ok:
            return False, message, data
        if data.get('mode') != 'fixed':
            return False, f'当前不是固定坐标模式（{data.get("mode")}）', data
        return True, '已读到固定坐标', data

    def set_fixed_ecef(self, x, y, z, persist=True, restart=True, verify=True):
        """§2.2.1 Mode=2：直接写死 ECEF，写完后模块立刻用它播 1005。

        参数必须是 **ARP（天线相位中心）** 的 ECEF，不是地面标石。
        """
        try:
            x = float(x)
            y = float(y)
            z = float(z)
        except (TypeError, ValueError):
            return False, 'ECEF 必须是数字', {'error': 'bad_ecef'}
        if abs(x) > 7.0e6 or abs(y) > 7.0e6 or abs(z) > 7.0e6:
            return False, 'ECEF 数值超出地球范围，疑似把经纬度当 ECEF 传进来了', {
                'error': 'ecef_out_of_range'}

        ok, message, data = self.request('set_fixed',
                                         x=f'{x:.4f}', y=f'{y:.4f}', z=f'{z:.4f}')
        if not ok:
            return False, message, data

        if persist:
            saved, save_message, save_data = self.save()
            if not saved:
                return False, f'坐标已写入但保存失败：{save_message}', save_data

        if verify:
            ok_read, read_message, read_data = self.get_fixed_ecef()
            if not ok_read:
                return False, f'写入后回读失败：{read_message}', read_data
            for axis, want in (('ecef_x', x), ('ecef_y', y), ('ecef_z', z)):
                got = read_data.get(axis)
                if got is None or abs(float(got) - want) > 0.01:
                    return False, (
                        f'回读不一致：{axis} 写入 {want:.4f}、读回 {got}'), {
                        'error': 'verify_failed', 'written': {'x': x, 'y': y, 'z': z},
                        'read': read_data}

        return True, f'固定坐标已写入（{x:.4f}, {y:.4f}, {z:.4f}）', data

    def config_survey_in(self, count, acc):
        """§2.2.1 Mode=1：Survey-in。

        `count` 是**最少定位次数**（不是秒）—— 1Hz 下 43200 次约等于 12 小时，
        这也是文档给的上限与默认值。`acc` 是 3D 精度上限（米），0 = 不限制。
        """
        try:
            n = int(count)
            a = float(acc)
        except (TypeError, ValueError):
            return False, f'Survey-in 参数不合法：count={count} acc={acc}', {'error': 'bad args'}
        if n <= 0:
            return False, f'定位次数必须大于 0（实际 {n}）', {'error': 'bad count'}
        if n > 86400:
            return False, f'定位次数上限 86400（实际 {n}）', {'error': 'bad count'}

        ok, message, data = self.request('set_svin', count=n, acc=f'{a:.1f}')
        if ok:
            return True, f'已启动 Survey-in（{n} 次 / 精度 {a:.1f} m）', data
        return False, message, data

    def survey_status(self):
        """读 `$PQTMSVINSTATUS` 缓存的最后一帧。

        语句是**输出型**（模块按 Rate 主动推），不是查询型，所以这里读的是
        `Channel` 已解析并缓存的状态（`cache['svin']`），不发命令。
        """
        cache = getattr(self.channel, 'cache', None) or {}
        status = cache.get('svin')
        if not status:
            return False, '还没有收到 PQTMSVINSTATUS（模块尚未开始测量或速率是 0）', {}
        return True, '已读到测量状态', status

    # ==================== 四、报文输出 ====================
    def config_messages(self, items):
        """配输出速率。

        `items` 里出现 `PQTMSVINSTATUS` / `PQTMEPE` 之外的语句一律跳过并记日志 ——
        这个型号不支持（文档 §2.2.5 只列了两条），硬发会 `ERROR,1`。
        """
        results = []
        for item in items or ():
            name = str(item.get('name') or '').strip().upper()
            rate = item.get('rate')
            if name not in RATE_TARGETS:
                logging.info(f'{self.DISPLAY_NAME} 不支持给 {name} 配速率，已跳过')
                results.append({'name': name, 'ok': False, 'reason': 'unsupported'})
                continue
            try:
                value = int(rate)
            except (TypeError, ValueError):
                results.append({'name': name, 'ok': False, 'reason': 'bad rate'})
                continue
            if not (RATE_RANGE[0] <= value <= RATE_RANGE[1]):
                results.append({'name': name, 'ok': False, 'reason': 'out of range'})
                continue

            key = 'rate_svin' if name == 'PQTMSVINSTATUS' else 'rate_epe'
            ok, message, _data = self.request(key, rate=value)
            results.append({'name': name, 'ok': ok, 'rate': value,
                            'message': '' if ok else message})

        failed = [r for r in results if not r.get('ok')]
        if failed:
            detail = '；'.join(f'{r["name"]}({r.get("reason") or r.get("message")})'
                               for r in failed)
            return False, f'{len(failed)} 条速率未生效：{detail}', {'results': results}
        return True, f'{len(results)} 条速率已配置', {'results': results}

    def set_rtcm_output(self, mode=0, ant_pnt=True, ephemeris=False):
        """一次配好 RTCM 输出三件套（§2.3.2 / §2.3.4 / §2.3.6）。

        `mode`  -1 关 / 0 MSM4 / 1 MSM7；`ant_pnt` 控制 1005；`ephemeris` 控制星历。
        三条命令各自独立，失败不中断 —— 否则 MSM7 不被支持时连 1005 都开不了。
        """
        results = []
        try:
            m = int(mode)
        except (TypeError, ValueError):
            return False, f'RTCM 模式不合法：{mode}', {'error': 'bad mode'}
        if m not in RTCM_MODE_LABEL:
            return False, f'RTCM 模式只能是 -1/0/1（实际 {m}）', {'error': 'bad mode'}

        ok, message, _d = self.request('rtcm_mode', mode=m)
        results.append({'item': 'rtcm', 'ok': ok, 'message': message})

        ok, message, _d = self.request('ant_pnt', enable=1 if ant_pnt else 0)
        results.append({'item': '1005', 'ok': ok, 'message': message})

        ok, message, _d = self.request('ephemeris', enable=1 if ephemeris else 0)
        results.append({'item': 'ephemeris', 'ok': ok, 'message': message})

        failed = [r for r in results if not r['ok']]
        label = RTCM_MODE_LABEL[m]
        if failed:
            detail = '；'.join(f'{r["item"]}: {r["message"]}' for r in failed)
            return False, f'RTCM 输出部分失败：{detail}', {'results': results}
        return True, f'RTCM 输出已设为 {label}', {'mode': m, 'results': results}

    def get_rtcm_output(self):
        """回读 RTCM 三件套状态，用于"下发值 == 回读值"自检。"""
        data = {}
        ok, message, r = self.request('get_rtcm_mode')
        if not ok:
            return False, message, data
        data['mode'] = (r or {}).get('mode')

        ok, _m, a = self.request('get_ant_pnt')
        if ok:
            data['ant_pnt'] = (a or {}).get('enable')

        ok, _m, e = self.request('get_ephemeris')
        if ok:
            data['ephemeris'] = (e or {}).get('enable')

        return True, f'RTCM 模式 {data.get("mode")}', data

    # ==================== 五、天线信息 ====================
    def set_antenna_info(self, desc, setup_id, sn):
        """LC29H(BS) 文档没有 1033 接收机/天线说明命令，明确拒绝而不是假装成功。"""
        return False, (
            f'{self.DISPLAY_NAME} 不支持写天线信息（文档无 1033 对应命令）；'
            '下游若不校验 1033 可忽略此项'), {'error': 'unsupported'}

    # ==================== 六、波特率 ====================
    def set_baud(self, baud, port=None):
        """§2.3.8 PAIR864 改 UART1 波特率。

        **必须重启才生效**（文档备注 1），所以返回里带 `reboot_required`。
        另外文档备注 2 提醒：输出波特率低于 115200 时模块可能丢消息，
        这里只提示不阻止（有些现场确实只能跑 9600）。
        """
        try:
            value = int(baud)
        except (TypeError, ValueError):
            return False, f'波特率不合法：{baud}', {'error': 'bad baud'}
        if value <= 0:
            return False, f'波特率不合法：{baud}', {'error': 'bad baud'}
        if value not in self.BAUDS:
            logging.warning(f'{self.DISPLAY_NAME} 官方档位不含 {value}，仍按用户要求下发')

        ok, message, data = self.request('set_baud', baud=value)
        if ok:
            data = dict(data or {})
            data['baud'] = value
            data['reboot_required'] = True
            warn = '；注意：低于 115200 可能丢消息' if value < 115200 else ''
            return True, f'波特率已设为 {value}（重启后生效）{warn}', data
        return False, message, data

    # ==================== 七、工具 ====================
    def save(self):
        ok, message, data = self.request('save')
        if ok:
            return True, '参数已保存到 NVM', data
        return False, message, data

    def reboot(self):
        """LC29H(BS) 没有 PQTMSRR（那是 LG290P 的），用热启动代替。

        `PQTMHOT` 会重启 GNSS 引擎但**不断串口**，对"让新配置生效"足够，
        而且不会让当前串口断流 —— 这一点比 SRR 更适合标定流程收尾。
        """
        return False, (
            f'{self.DISPLAY_NAME} 没有 PQTMSRR 软重启命令；'
            '配置已在 PQTMSAVEPAR 后持久化，重新上电或等模块自行复位即可生效'), \
            {'error': 'no reboot command'}

    def factory_reset(self):
        ok, message, data = self.request('factory_reset')
        if ok:
            return True, '已下发恢复出厂，重启后生效', data
        return False, message, data

    # ==================== 八、解析 ====================
    def parse_line(self, line):
        """把一行拆成 dict 并更新缓存。

        必须自己实现（基类只做通用 NMEA）。这里额外处理三类本型号特有帧：
          - `$PQTMVERNO,...`        版本
          - `$PQTMCFGSVIN,OK,...`   7 字段 SVIN 配置
          - `$PAIR001,<cmd>,<res>`  PAIR 统一应答（含结果码含义）
        其余交给 `super().parse_line()`，避免把 GGA/RMC 这些通用逻辑重写一遍。
        """
        text = (line or '').strip()
        if not text:
            return None

        # PAIR 统一应答：单独处理，因为它是异步的，要留住最近一次结果
        if 'PAIR001' in text:
            parts = self._split(text)
            if len(parts) >= 3:
                try:
                    cmd_id = int(parts[1])
                    code = int(parts[2])
                except (TypeError, ValueError):
                    return None
                self._last_pair = {'command': cmd_id, 'result': code,
                                   'text': PAIR_RESULT.get(code, f'未知({code})')}
                return {'type': 'PAIR001', 'command': cmd_id, 'result': code,
                        'message': self._last_pair['text']}
            return None

        # PAIR 的查询应答（如 $PAIR433,1）
        if text.startswith('$PAIR') and ',OK' not in text and ',ERROR' not in text:
            parts = self._split(text)
            head = parts[0].lstrip('$')
            if len(parts) >= 2:
                try:
                    value = int(parts[1])
                except (TypeError, ValueError):
                    value = parts[1]
                mapping = {'PAIR433': 'mode', 'PAIR435': 'enable', 'PAIR437': 'enable'}
                key = mapping.get(head)
                if key:
                    return {'type': head, key: value}
            return {'type': head}

        return super().parse_line(line)

    # ==================== 内部小工具 ====================
    @staticmethod
    def _split(text):
        """拆逗号分隔字段，去掉 `*XX` 校验和与 `$`。"""
        body = text.lstrip('$')
        if '*' in body:
            body = body.split('*', 1)[0]
        return [item.strip() for item in body.split(',')]

    @staticmethod
    def _role_from_svin(data):
        mode = (data or {}).get('mode')
        if mode == 2:
            return 'base'
        if mode in (0, 1):
            return 'rover'
        return 'unknown'


# ==================== 小工具 ====================
def _to_int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _to_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None

