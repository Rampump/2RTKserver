# -*- coding: utf-8 -*-
"""Quectel LG290P 系列驱动（LG290P / LG290P(03) / LG291P）。

命令来源：docs/quectel-lg290p-calibration-commands.zh.md，
每个方法都标了对应章节，改一行前先去文档核对，不要凭记忆改。

三条纪律：
 1. **命令外置**：所有指令集中在 `CMD` 表，流程层只按语义键调用。
 2. **校验和算出来**：表里只存命令体，`*XX` 在发帧时实时 XOR；
    文档给过校验和的固定命令额外在 `CS_EXPECT` 里记一份用于自检（方案 §4.1）。
 3. **参数格式跟文档一致**：`-90.0` 不写 `-90`、`1.2` 不写 `1.20`，
    否则"下发值 == 回读值"的自检永远不相等（指令全集 §6.2 注意 2）。
"""
import logging

from .base import BaseDriver
from .driver_manager import register

# ==================== 命令表（语义键 → 命令体） ====================
CMD = {
    # ---- 识别与信息查询（§3.1 ~ §3.3）----
    'identify':        'PQTMVERNO',
    'serial_no':       'PQTMSN',
    'uniq_id':         'PQTMUNIQID',
    # 能力探测专用：与正式命令同源，避免 probe 里再抄一遍字符串（命令外置铁律）
    'probe_ant':       'PQTMCFGANTINF,R',

    # ---- 二、工作模式（§4.1）----
    'get_mode':        'PQTMCFGRCVRMODE,R',
    'set_rover':       'PQTMCFGRCVRMODE,W,1',
    'set_base':        'PQTMCFGRCVRMODE,W,2',

    # ---- 三、坐标与 Survey-in（§5.1）----
    'get_svin':        'PQTMCFGSVIN,R',
    'set_svin':        'PQTMCFGSVIN,W,1,{count},{acc},0,0,0',
    'set_svin_hold':   'PQTMCFGSVIN,W,1,{count},{acc},0,0,0,{distance}',
    'set_fixed':       'PQTMCFGSVIN,W,2,0,0,{x},{y},{z}',
    'disable_svin':    'PQTMCFGSVIN,W,0,0,0,0,0,0',

    # ---- 四、报文输出（§6.1 / §6.2）----
    'msg_rate':        'PQTMCFGMSGRATE,W,{name},{rate}',
    'msg_rate_ver':    'PQTMCFGMSGRATE,W,{name},{rate},{ver}',
    # MSM 是特例（§6.1 实测）：语句名固定 `RTCM3-107X`（全星座共用一档），
    # 且**必须带 `<Offset>`**（范围 0 ~ Rate-1），少写这个字段直接回 ERROR,1。
    # 按星座名写（RTCM3-1074 / 1084 …）也会回 ERROR,1 —— 真机验证过，别再试了。
    'msg_rate_msm':    'PQTMCFGMSGRATE,W,{name},{rate},{offset}',
    'get_msg_rate':    'PQTMCFGMSGRATE,R,{name}',
    'get_msg_rate_ver': 'PQTMCFGMSGRATE,R,{name},{ver}',
    'get_msg_rate_msm': 'PQTMCFGMSGRATE,R,{name}',
    # 定位频率（方案 P1：CORS 采集要「配 GGA 1Hz + FIXRATE 1000ms」）。
    # 单位是毫秒，不是 Hz：`PQTMCFGFIXRATE,W,1000` = 1 Hz。
    'fix_rate':        'PQTMCFGFIXRATE,W,{ms}',
    'get_fix_rate':    'PQTMCFGFIXRATE,R',
    'get_rtcm':        'PQTMCFGRTCM,R',
    # 仰角阈值实测必须写整数 `-90`：真机（LG290P03AANR01A03S / 2024-05-22）对
    # `-90.0` 直接回 `ERROR,1`（参数不合法），而模块自己的回显也是 `-90`。
    # 文档 §6.2 说"两种写法都出现过，模块都接受"—— 这块固件不接受，以实测为准。
    'set_rtcm':        'PQTMCFGRTCM,W,{msm},{mode},-90,07,06,{eph_mode},{eph_interval}',
    # 还原专用：把备份里读到的 7 个字段原样回放（含星历模式/间隔，逐字段重配会丢信息）
    'set_rtcm_fields': 'PQTMCFGRTCM,W,{fields}',

    # ---- 天线信息（§6.4）----
    # 注意：LG290P **不支持** PQTMCFGANTDELTA（那是 LG291P(00) 的，见 §6.5）
    'get_ant_info':    'PQTMCFGANTINF,R',
    'set_ant_info':    'PQTMCFGANTINF,W,{desc},{setup_id},{sn}',

    # ---- 四、串口（§2.3.10 PQTMCFGUART）----
    # 完整形式：PQTMCFGUART,W,<BaudRate>[,<DataBit>,<Parity>,<StopBit>,<FlowCtrl>]
    # 多串口形式：PQTMCFGUART,W,<Index>,<BaudRate>[,...]，Index: 1=UART1 2=UART2 3=UART3
    # 只写波特率时后面的数据位/校验/停止位/流控保持原值，不需要跟着填。
    'set_baud':        'PQTMCFGUART,W,{baud}',
    'set_baud_port':   'PQTMCFGUART,W,{port},{baud}',
    'get_baud':        'PQTMCFGUART,R',
    'get_baud_port':   'PQTMCFGUART,R,{port}',

    # ---- 五、工具（§7.1 / §7.2 / §7.3）----
    'save':            'PQTMSAVEPAR',
    'reboot':          'PQTMSRR',
    'factory_reset':   'PQTMRESTOREPAR',
}

# 文档原文给过校验和的**无参数**命令，用于下发前自检（有参数的不填，参数一变校验和就变）
CS_EXPECT = {
    'identify':      '58',   # §3.1
    'serial_no':     '05',   # §3.2
    'uniq_id':       '16',   # §3.3
    'get_mode':      '32',   # §4.1
    'set_rover':     '2A',   # §4.1
    'set_base':      '29',   # §4.1
    'get_svin':      '26',   # §5.1
    'get_ant_info':  '3E',   # §6.4
    'probe_ant':     '3E',   # 与 get_ant_info 同一条命令，仅用于能力探测
    'save':          '5A',   # §7.1
    'reboot':        '4B',   # §7.3
    'factory_reset': '13',   # §7.2
}

CAPS = {
    'dual_mode': True,        # 可切移动站/基站（§4.1）
    'set_mode': True,
    'svin': True,             # Survey-in（§5.1）
    'fixed': True,            # 固定坐标（§5.1）
    'ant_info': True,         # PQTMCFGANTINF（§6.4）
    'ant_delta': False,       # §6.5 原文：仅 LG291P(00) 支持，LG290P 不支持
    'msg_rate': True,         # §6.1
    'rtcm_out': True,         # §6.2
    'err3': True,             # 会返回错误码 3（§2.2）
}

# 基站播发的 MSM 星座清单（每条独立开关，必须全部下发才算"多星座"）。
# 只配 RTCM3-107X 时实测只有 1074（GPS）在播，其余四个星座_rate 默认是 0。
MSM_CONSTELLATIONS = ('RTCM3-107X',   # GPS      → 1074
                      'RTCM3-108X',   # GLONASS  → 1084
                      'RTCM3-109X',   # Galileo  → 1094
                      'RTCM3-111X',   # QZSS     → 1114
                      'RTCM3-112X')   # BDS      → 1124

MODE_TO_CODE = {'rover': 1, 'base': 2}
CODE_TO_MODE = {0: 'unknown', 1: 'rover', 2: 'base'}

# PQTM 私有语句配速率**必须带 <MsgVer>**，否则 ERROR,1（协议规范 §2.3.15 注意 1）。
# 版本号不是常量 1：PQTMEPE 是 2，PQTMSVINSTATUS 是 1。真机已验证：
#   R,PQTMEPE,2      → OK,PQTMEPE,0,2
#   R,PQTMSVINSTATUS,1 → ERROR,1（rover 模式下该语句不可用，与版本号无关）
MSG_VER = {
    'PQTMEPE': 2,
    'PQTMSVINSTATUS': 1,
}
DEFAULT_MSG_VER = 1

# 语句速率上限（协议规范 表 6）。<Rate> 的语义是"每 N 次定位输出一次"，**不是 Hz**。
# 下限之所以是 0 而不是 1：`<Rate>` 的定义是"**0 或 N**，0 = 不输出"（协议规范
# 表 6 参数表第一行）。这一条不是理论值 —— 真机 `R,RTCM3-1005` 对一个从未配置过的
# 模块就回 `OK,RTCM3-1005,0`，说明 0 是模块能持有的合法速率。还原功能必须能写 0：
# 标定把 1005 从 0 打开到 1，还原就得能把它关回 0，否则"一键还原"永远关不掉 1005。
RATE_RANGE_NMEA = (0, 255)           # 1~255 为有效输出间隔，0 = 关闭
RATE_RANGE_RTCM = (0, 1200)          # 1005/1006/1033 与各星座 MSM 都是 1~1200，0 = 关闭
RATE_RANGE_SWITCH = (0, 1)           # EPH / RAW-* 类只支持 0（关）/ 1（开）
_RATE_SWITCH_PREFIX = ('RTCM3-1019', 'RTCM3-1020', 'RTCM3-1041', 'RTCM3-1042',
                       'RTCM3-1044', 'RTCM3-1046', 'RAW-')
# 曾以为这两类语句的输出速率由固件固定为 1 Hz、配了也不生效（协议规范 表 6 注意 6）。
# **真机实测（LG290P03AANR01A03S）已推翻这个理解**：对 GSA/GSV 写 rate=0
# 是生效的（`$GNGSA` 直接消失、GSV 全无），写回 1 也立刻恢复。
# 保留这个元组只为"记录历史判断"，不再据此打警告——否则现场会被一条
# 不成立的 WARNING 误导，以为"GSA 关不掉"，进而怀疑整条 NMEA 配置链路坏了。
_RATE_FIXED = ('GSA', 'GSV')

# MSM 语句名前缀：107=GPS 108=GLONASS 109=Galileo 111=QZSS 112=BDS 113=NavIC。
# 它们在模块内部共用**一档**速率，且命令名只认 `RTCM3-107X`（真机验证：
# 写 `RTCM3-1074` 或漏掉 `<Offset>` 都回 ERROR,1）。
_MSM_PREFIX = ('RTCM3-107', 'RTCM3-108', 'RTCM3-109', 'RTCM3-111', 'RTCM3-112', 'RTCM3-113')


def is_msm_name(name):
    """是不是 MSM 语句名（`RTCM3-107X` / `RTCM3-1074` 都算）。"""
    return (name or '').strip().upper().startswith(_MSM_PREFIX)


def rate_range(name):
    """返回 (下限, 上限)，给 set_message_rate 做范围校验。"""
    upper = (name or '').strip().upper()
    if upper.startswith(_RATE_SWITCH_PREFIX):
        return RATE_RANGE_SWITCH
    if upper.startswith('RTCM3-'):
        return RATE_RANGE_RTCM
    return RATE_RANGE_NMEA


def msg_ver_for(name):
    """PQTM 语句该带哪个版本号；非 PQTM 语句返回 None（必须省略该字段）。"""
    upper = (name or '').strip().upper()
    if not upper.startswith('PQTM'):
        return None
    return MSG_VER.get(upper, DEFAULT_MSG_VER)


@register
class LG290PDriver(BaseDriver):
    """LG290P / LG290P(03) / LG291P 共用一套 `$PQTM` 指令集（指令全集 §8.1）。"""

    MODEL_KEY = 'lg290p'
    DISPLAY_NAME = 'Quectel LG290P'
    MATCH_PREFIX = 'LG290P'
    DUAL_MODE = True
    CAPS = CAPS
    CMD = CMD
    CS_EXPECT = CS_EXPECT

    # 官方波特率（LG29xP 协议规范 §2.3.10 PQTMCFGUART 的 <BaudRate> 取值范围）。
    # 文档明确只列这五档；4800 / 3000000 是 LC29H 才有的，这块不支持。
    #
    # 三个容易记错的事实，这里一次写清：
    #   1. 手册**没有**承诺出厂默认是哪一档。参数表里只列取值，正文 NOTE 明说
    #      「默认值没给的，用对应的 Get 命令查」—— 所以默认值不是文档里的知识，
    #      只能问模块。下面的 `DEFAULT_BAUD` 是"押注最可能命中的一档"（依据是
    #      手册全部示例 + 本机实测），`bauds()` 会把它排到候选表第一位；
    #      它不是"出厂值"的承诺，押错就照旧往下试，代价为零。
    #   2. 手册所有示例都写 115200，但那是示例，不是出厂值；刷过固件或改过
    #      CONFIG COM1 的模块跑在别的档上很常见（这一档正是用户手填值要排最前的原因）。
    #   3. 另一版 LG290P(03)/LG580P(03) 规范把范围写成 9600~4000000 连续区间，
    #      也就是说存在 5 档之外的合法非标值 —— 枚举永远覆盖不全，
    #      所以真正的答案来自下面这条回读命令，而不是把候选表越堆越长。
    BAUDS = (9600, 115200, 230400, 460800, 921600)
    DEFAULT_BAUD = 115200
    BAUD_CMD_KEY = 'set_baud'
    # §2.3.10 的回读命令 `$PQTMCFGUART,R` —— 有它就优先问模块，别逐档盲试。
    BAUD_READ_KEY = 'get_baud'

    # 必须挂在**类**上：流程层写的是 `getattr(driver_instance, 'MSM_CONSTELLATIONS')`。
    # 只在模块级定义过一次，取到 None 后静默退化成"只开 107X"，
    # 结果基站只播 1074、其余星座全关（2026-11-02 真机踩到，见契约测试）。
    MSM_CONSTELLATIONS = MSM_CONSTELLATIONS

    # ==================== 一、识别与信息 ====================
    def detect(self):
        """§3.1：用 `PQTMVERNO` 的版本串判断型号。

        为什么不用 SN：版本串里带型号位（`LG290P03...`），一眼就能看出插的是哪块，
        而且它是最短的一条查询命令，识别阶段的 2 秒超时只花一次。
        """
        ok, message, data = self.request('identify')
        if not ok:
            return False, message, data

        fields = data.get('fields') or []
        firmware = (fields[0] if fields else '').strip()
        # 防御：`$PQTMVER` 开机帧和 `$PQTMVERNO` 只差三个字母，字段却整体错位
        # （开机帧第 1 个字段是 MsgVer="1"）。真吃错了会得到"固件串 1"这种鬼结论，
        # 所以宁可显式拒绝，也不要拿数字开头的串去判型号。
        if not firmware or firmware[0].isdigit():
            return False, f'识别应答格式异常（首字段为 {firmware or "(空)"}），疑似收到开机帧', {
                'firmware': firmware, 'error': 'bad_reply'}
        if not firmware.upper().startswith(self.MATCH_PREFIX):
            return False, f'固件串 {firmware or "(空)"} 不属于 {self.DISPLAY_NAME}', {
                'firmware': firmware, 'error': 'prefix_mismatch'}

        return True, f'识别到 {firmware}', {
            'firmware': firmware,
            'build_date': fields[1] if len(fields) > 1 else '',
            'build_time': fields[2] if len(fields) > 2 else '',
            'model_key': self.MODEL_KEY,
        }

    def read_firmware(self):
        """§3.1 单独读固件版本（识别之后流程里还要再读一次用于落库）。"""
        ok, message, data = self.request('identify')
        if not ok:
            return False, message, data
        fields = data.get('fields') or []
        return True, message, {
            'firmware': fields[0] if fields else '',
            'build_date': fields[1] if len(fields) > 1 else '',
            'build_time': fields[2] if len(fields) > 2 else '',
        }

    def read_serial(self):
        """§3.2：`$PQTMSN,OK,<Reversed>,<Length>,<SN>`。Length 是实际长度，别按 16 位截断。

        实测（LG290P03AANR01A03S / 2024-05-22）这条命令回 `ERROR,3`（该型号不支持），
        虽然文档 §3.2 把它列在 LG290P 名下。所以本方法先看能力位，不支持就快速返回，
        由 info() 退回到 PQTMUNIQID 当模块标识。
        """
        if not self.caps('serial_no'):
            return False, '本固件不支持 PQTMSN（ERROR,3），请用 UNIQID 作模块标识', {
                'error': 'unsupported'}
        ok, message, data = self.request('serial_no')
        if not ok:
            return False, message, data
        fields = data.get('fields') or []
        if len(fields) < 3:
            return False, f'PQTMSN 应答字段不足：{message}', {'error': 'bad_reply', 'raw': message}
        try:
            length = int(fields[1])
        except ValueError:
            length = -1
        sn = fields[2].strip()
        if length > 0 and len(sn) != length:
            # 长度不符不判失败：固件偶有 Length 与字符串不等长，记录但不阻断
            logging.warning(f'PQTMSN 的 Length={length} 与实际长度 {len(sn)} 不一致')
        return True, sn, {'sn': sn, 'length': length}

    def read_unique_id(self):
        """§3.3：`$PQTMUNIQID,OK,<Length>,<ID>`。

        实测 `<Length>` 是**字节数**，不是字符数：`OK,8,00006FE9326C3E41`
        表示 8 字节 = 16 个十六进制字符。按字符数去截断会砍掉一半 ID。
        """
        ok, message, data = self.request('uniq_id')
        if not ok:
            return False, message, data
        fields = data.get('fields') or []
        if len(fields) < 2:
            return False, f'PQTMUNIQID 应答字段不足：{message}', {'error': 'bad_reply'}
        uniq_id = fields[1].strip()
        return True, uniq_id, {'uniq_id': uniq_id, 'length_bytes': _to_int(fields[0])}

    def probe_caps(self):
        """运行时探测可选命令，用真实应答修正能力集（方案 §4.5）。

        错误码 3 = "该型号不支持此命令"，**不要重试**，直接把能力位关掉。
        这比"按文档写死 CAPS"可靠：同一型号不同固件的命令集是会变的，
        实测 2024-05-22 版 LG290P 就没有 PQTMSN 和 PQTMCFGANTINF。

        探测用的命令体一律从 `CMD` 表取，不在函数里写字符串 ——
        否则"流程层不出现命令字符串"就有了后门，换型号时这里最先被漏掉。
        """
        probes = (
            ('serial_no', 'serial_no'),
            ('ant_info', 'probe_ant'),
        )
        result = {}
        for cap, key in probes:
            body = self.body(key)
            self.verify_checksum(key, body)
            status, line = self.channel.send(body, timeout=1.5, retries=0)
            if status == 'ok':
                result[cap] = True
            elif status == 'error' and self._parse_error_code(line) == 3:
                result[cap] = False
            else:
                # 超时/其它错误：不下结论，保持文档声明的值
                result[cap] = self.caps(cap)
            self.set_cap(cap, result[cap])
        return result

    def info(self):
        """§5.1 的 info()：型号/固件/SN/唯一ID/能力标记。

        单项失败不整体失败：SN 读不到仍然可以标定，只是记录里少个 SN。
        """
        ok, message, data = self.detect()
        if not ok:
            return False, message, data

        # 先探测能力，再决定 SN 走哪条命令 —— 否则会对着不支持的 PQTMSN 白等 2 秒
        self.probe_caps()

        result = dict(data)
        result['model_key'] = self.MODEL_KEY
        result['display_name'] = self.DISPLAY_NAME

        sn_ok, sn_message, sn_data = self.read_serial()
        result['sn'] = sn_data.get('sn', '') if sn_ok else ''
        result['sn_source'] = 'pqtmsn' if sn_ok else ''
        if not sn_ok:
            result['sn_error'] = sn_message

        uid_ok, _uid_message, uid_data = self.read_unique_id()
        result['uniq_id'] = uid_data.get('uniq_id', '') if uid_ok else ''
        # 没有 SN 时用 UNIQID 兜底：写进 calibration.db 的 module_sn 必须能唯一定位一块模块
        if not result['sn'] and result['uniq_id']:
            result['sn'] = result['uniq_id']
            result['sn_source'] = 'uniqid'

        mode_ok, mode_message, mode_data = self.get_mode()
        result['mode'] = mode_data.get('mode', 'unknown') if mode_ok else 'unknown'
        if not mode_ok:
            result['mode_error'] = mode_message

        result['caps'] = dict(self.runtime_caps)
        return True, f'{result["firmware"]} / SN {result["sn"] or "(未读到)"}', result

    # ==================== 二、模式 ====================
    def get_mode(self):
        """§4.1 查询：`$PQTMCFGRCVRMODE,OK,<Mode>`，0=未知 1=移动站 2=基站。

        注意：返回 2 只代表"配成了基站"，**不代表已收敛**，收敛看 Survey-in 状态（§5.2）。
        """
        ok, message, data = self.request('get_mode')
        if not ok:
            return False, message, data
        fields = data.get('fields') or []
        code = _to_int(fields[0]) if fields else None
        mode = CODE_TO_MODE.get(code, 'unknown')
        # 缓存最近一次模式：survey_status() 要靠它区分"没开状态输出"和"还在 rover 模式"
        if self.channel is not None:
            self.channel.cache['mode_code'] = code
        return True, f'当前模式：{mode}', {'mode': mode, 'mode_code': code}

    def set_mode(self, mode, persist=True, restart=True, verify=True):
        """§4.1 设置 + 基站指导 §3.2 序列：切模式 → SAVEPAR → SRR → 回读。

        `persist=False` 只改运行态（断电回默认），用于临时试验；正式标定必须 True。

        **切到基站后 NMEA 会消失**（协议规范 §2.3.25：基站模式自动禁用 NMEA 语句输出，
        改为输出 RTCM MSM4 + RTCM3-1005）。所以标定链路上要 GGA 就得用
        `ensure_nmea_output()` 显式打开，不能假设"刚才有 GGA，现在还有"。
        """
        mode = (mode or '').lower()
        if mode not in MODE_TO_CODE:
            return False, f'不支持的模式：{mode}', {'error': 'bad_mode'}
        if mode == 'rover' and not self.caps('dual_mode'):
            return False, '本型号不是双模，不能切移动站', {'error': 'no_dual_mode'}

        key = 'set_base' if mode == 'base' else 'set_rover'
        ok, message, data = self.request(key)
        if not ok:
            return False, message, data

        if persist:
            saved, save_message, save_data = self.save()
            if not saved:
                return False, f'切模式成功但保存失败：{save_message}', save_data
        if restart:
            rebooted, reboot_message, reboot_data = self.reboot()
            if not rebooted:
                return False, f'切模式已保存但重启失败：{reboot_message}', reboot_data
        if verify:
            return self.apply_and_verify(
                lambda: self._reapply(key, persist),
                lambda: self._check_mode(mode))
        return True, f'已下发切换到 {mode} 模式', {'mode': mode}

    def set_mode_memory_only(self, mode):
        """只改运行态切模式：不 SAVEPAR、不重启、不碰 NVM（附录D）。

        `$PQTMCFG...` 类命令写的是 RAM 里的当前配置，`$PQTMSAVEPAR` 才落到 NVM。
        所以这里下发模式命令后**立刻回读**，确认 RAM 里真的变了：
        回读不一致就如实返回失败，让上层提示用户"该型号必须保存才生效"，
        而不是为了"看起来成功"偷偷存一次 NVM（那会把用户的基准站配置改掉）。
        """
        mode = (mode or '').lower()
        if mode not in MODE_TO_CODE:
            return False, f'不支持的模式：{mode}', {'error': 'bad_mode'}
        return self.set_mode(mode, persist=False, restart=False, verify=True)

    def ensure_nmea_output(self, enabled=True, names=('GGA',), persist=True):
        """基站模式下把 GGA/RMC/GLL 重新打开（协议规范 §2.3.25 + 基站指导 表 2）。

        基站模式默认只输出 `1005 + MSM4`，NMEA 全关。标定流程如果还想从 GGA 读
        卫星数/质量位/PDOP 旁证，就必须显式打开；默认只开 GGA，够用且不占带宽。
        """
        targets = tuple(names or ('GGA',))
        rate = 1 if enabled else 0
        applied, failed = {}, {}
        for name in targets:
            ok, message, _data = self.set_message_rate(name, rate)
            if ok:
                applied[name] = rate
            else:
                failed[name] = message

        saved = False
        if applied and persist:
            saved, save_message, _data = self.save()
            if not saved:
                return False, f'NMEA 输出已改但保存失败：{save_message}', {'applied': applied}
        if failed:
            return False, f'部分失败：{failed}', {'applied': applied, 'failed': failed,
                                               'saved': saved}
        action = '开启' if enabled else '关闭'
        return True, f'已{action} NMEA 输出：{sorted(applied)}', {
            'applied': applied, 'saved': saved}

    # ==================== 三、坐标与 Survey-in ====================
    def get_svin_config(self):
        """§5.1 查询：`$PQTMCFGSVIN,OK,<Mode>,<CFG_CNT>,<3D_AccLimit>,<X>,<Y>,<Z>[,<Distance>]`。

        陷阱（§5.1 注意 4）：Survey-in 模式下这里的 ECEF 是配置值，恒为 `0.0000`，
        **不是收敛结果**。要看真实坐标用 `survey_status()` 或抓 1005。
        """
        ok, message, data = self.request('get_svin')
        if not ok:
            return False, message, data
        fields = data.get('fields') or []
        if len(fields) < 6:
            return False, f'PQTMCFGSVIN 应答字段不足：{message}', {'error': 'bad_reply'}

        mode_code = _to_int(fields[0])
        return True, message, {
            'mode_code': mode_code,
            'mode': {0: 'disabled', 1: 'survey_in', 2: 'fixed'}.get(mode_code, 'unknown'),
            'count': _to_int(fields[1]),
            'acc_limit': _to_float(fields[2]),
            'ecef_x': _to_float(fields[3]),
            'ecef_y': _to_float(fields[4]),
            'ecef_z': _to_float(fields[5]),
            'distance': _to_float(fields[6]) if len(fields) > 6 else None,
        }

    def get_fixed_ecef(self):
        """§5.1 读固定坐标。Survey-in 模式下会返回 0 值，调用方必须看 `mode`。"""
        ok, message, data = self.get_svin_config()
        if not ok:
            return False, message, data
        if data.get('mode') != 'fixed':
            return True, f'当前不是固定模式（{data.get("mode")}），ECEF 为配置值不代表真实坐标', data
        return True, message, data

    def set_fixed_ecef(self, x, y, z, persist=True, restart=True, verify=True):
        """§5.1 + 附录 B 序列一：写 ECEF → SAVEPAR → SRR → 回读比对。

        参数必须是 **ARP（天线相位中心）** 的 ECEF（§5.1 注意 6），不是地面标石。
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

        ok, message, data = self.request('set_fixed', x=f'{x:.4f}', y=f'{y:.4f}', z=f'{z:.4f}')
        if not ok:
            return False, message, data

        if persist:
            saved, save_message, save_data = self.save()
            if not saved:
                return False, f'写坐标成功但保存失败：{save_message}', save_data
        if restart:
            rebooted, reboot_message, reboot_data = self.reboot()
            if not rebooted:
                return False, f'坐标已保存但重启失败：{reboot_message}', reboot_data
        if verify:
            return self.apply_and_verify(
                lambda: self._reapply('set_fixed', persist,
                                      x=f'{x:.4f}', y=f'{y:.4f}', z=f'{z:.4f}'),
                lambda: self._check_fixed(x, y, z))
        return True, '固定坐标已下发', {'written': {'x': x, 'y': y, 'z': z}}

    def config_survey_in(self, count, acc_limit, distance=None,
                         persist=True, restart=True, verify=True):
        """§5.1 Survey-in 配置 + 附录 B 序列二：配收敛 → 保存 → 重启 → 回读。

        参数范围（§5.1）：count 0~86400，acc_limit 0.0~1000.0，distance 0.0~10.0。
        `acc_limit=0` 是"无精度限制"（默认值），烂点也会进平均 —— 别拿 0 当"最高精度"。
        """
        count = _to_int(count)
        acc_limit = _to_float(acc_limit)
        if count is None or not (0 <= count <= 86400):
            return False, 'Survey-in 次数必须在 0~86400 之间', {'error': 'bad_count'}
        if acc_limit is None or not (0.0 <= acc_limit <= 1000.0):
            return False, '3D 精度门限必须在 0.0~1000.0 米之间', {'error': 'bad_acc'}
        if acc_limit == 0:
            logging.warning('3D 精度门限为 0 = 无精度限制，任何质量的点都会进平均')

        kwargs = {'count': int(count), 'acc': _fmt_dec(acc_limit, 1)}
        key = 'set_svin'
        if distance is not None:
            distance = _to_float(distance)
            if distance is None or not (0.0 <= distance <= 10.0):
                return False, '保持距离必须在 0.0~10.0 米之间', {'error': 'bad_distance'}
            kwargs['distance'] = _fmt_dec(distance, 1)
            key = 'set_svin_hold'

        ok, message, data = self.request(key, **kwargs)
        if not ok:
            return False, message, data

        if persist:
            saved, save_message, save_data = self.save()
            if not saved:
                return False, f'Survey-in 配置成功但保存失败：{save_message}', save_data
        if restart:
            rebooted, reboot_message, reboot_data = self.reboot()
            if not rebooted:
                return False, f'Survey-in 已保存但重启失败：{reboot_message}', reboot_data
        if verify:
            return self.apply_and_verify(
                lambda: self._reapply(key, persist, **kwargs),
                lambda: self._check_svin(count, acc_limit))
        return True, f'Survey-in 已下发：{int(count)} 次 / {acc_limit:.1f} m', kwargs

    # ---------- 回读比对（§14 #6 的 check 侧） ----------
    def _reapply(self, key, persist, **kwargs):
        """重发一次配置命令；`persist` 为真时再存一次 NVM（**不再重启**）。

        重试阶段故意不 SRR：重启是为了让 NVM 生效，而这里要修的是"刚下发到 RAM
        的值没读回来"。再等一次 6 秒重启既慢，又可能把串口等到超时，
        对"参数到底生效没有"这个问题的答案没有任何帮助。
        """
        ok, message, data = self.request(key, **kwargs)
        if not ok:
            return False, message, data
        if persist:
            saved, save_message, save_data = self.save()
            if not saved:
                return False, f'重发配置成功但保存失败：{save_message}', save_data
        return True, '已重发配置', {'retried': True}

    def _check_mode(self, mode):
        ok, message, data = self.get_mode()
        if not ok:
            return False, f'回读模式失败：{message}', data
        if data.get('mode') != mode:
            return False, f'回读不一致：期望 {mode}，实际 {data.get("mode")}', data
        if mode == 'base':
            # 基站模式自动关 NMEA（协议规范 §2.3.25），记下来让上层知道要显式打开
            data['nmea_auto_off'] = True
        return True, f'已切换到 {mode} 模式', data

    def _check_fixed(self, x, y, z):
        ok, message, data = self.get_svin_config()
        if not ok:
            return False, f'回读坐标失败：{message}', data
        if data.get('mode') != 'fixed':
            return False, f'回读模式不是 fixed：{data.get("mode")}', data
        worst = max(abs((data.get('ecef_x') or 0.0) - x),
                    abs((data.get('ecef_y') or 0.0) - y),
                    abs((data.get('ecef_z') or 0.0) - z))
        if worst > 0.01:
            return False, f'回读坐标与写入值差 {worst:.4f} m（超过 1 cm），判定未生效', {
                'error': 'verify_mismatch', 'delta': worst, 'readback': data}
        data['verify_delta'] = worst
        data['written'] = {'x': x, 'y': y, 'z': z}
        return True, f'固定坐标已写入（回读偏差 {worst:.4f} m）', data

    def _check_svin(self, count, acc_limit):
        ok, message, data = self.get_svin_config()
        if not ok:
            return False, f'回读 Survey-in 配置失败：{message}', data
        if data.get('mode') != 'survey_in' or data.get('count') != int(count):
            return False, f'回读不一致：{data}', {'error': 'verify_mismatch', 'readback': data}
        return True, f'Survey-in 已配置：{int(count)} 次 / {acc_limit:.1f} m', data

    def disable_survey_in(self, persist=True, restart=False):
        """§5.1 `PQTMCFGSVIN,W,0,...`：停用 Survey-in / 固定坐标。

        用途是"标定前先把模块置成不自动收敛的状态"，避免上一次的 Survey-in 配置
        在标定过程中继续收敛，把旧均值当成新结果。

        默认 `restart=False`：停用本身不改变运行态定位，没必要为它多等一次 6 秒重启；
        调用方要一次彻底生效，传入 `restart=True` 与后续步骤合并重启即可。
        """
        ok, message, data = self.request('disable_svin')
        if not ok:
            return False, message, data
        if persist:
            saved, save_message, save_data = self.save()
            if not saved:
                return False, f'停用 Survey-in 成功但保存失败：{save_message}', save_data
        if restart:
            rebooted, reboot_message, reboot_data = self.reboot()
            if not rebooted:
                return False, f'停用已保存但重启失败：{reboot_message}', reboot_data
        return True, 'Survey-in 已停用', {'mode': 'disabled'}

    def survey_status(self):
        """§5.2 读最近一条 `$PQTMSVINSTATUS`（模块主动输出，没有查询命令）。

        `<Valid>`（本驱动里的 `state`）：0=无效 1=收敛中 **2=已收敛**。
        别用 `<Obs> == <CfgDur>` 判断完成：官方示例 59/60 时仍是 1，60/60 才变 2。
        `<CfgDur>` 是 **<CFG_CNT>（最少定位次数）**，不是秒；基站模式定位速率固定 1 Hz，
        所以次数在数值上恰好等于秒（协议规范 §2.3.28 备注），换模式就不再等价。

        拿不到时返回 `error='no_status'`，两种原因要分开提示：
        没开状态输出，或者模块还在 rover 模式（该语句只有基站模式才输出）。
        """
        status = self.channel.cache.get('svin') if self.channel else None
        if not status:
            mode = (self.channel.cache.get('mode_code') if self.channel else None)
            hint = '需先用 config_survey_in 并把 PQTMSVINSTATUS 输出打开'
            if mode == 1:
                hint = '模块当前是移动站(rover)模式，$PQTMSVINSTATUS 只在基站模式输出'
            return False, f'尚未收到 $PQTMSVINSTATUS（{hint}）', {
                'error': 'no_status', 'mode_code': mode}
        text = (f'Survey-in {status.get("valid_text")}：'
                f'{status.get("obs")}/{status.get("cfg_count")} 次，'
                f'3D 精度 {status.get("mean_acc")} m')
        return True, text, status

    def enable_survey_status(self, enabled=True, rate=1):
        """§5.2 开关：`PQTMCFGMSGRATE,W,PQTMSVINSTATUS,<Rate>,<MsgVer>`。

        PQTM 语句配速率**必须带 MsgVer**，否则 ERROR,1（§6.1 注意 1）。
        收敛完成后建议关掉：移动站会回"不支持该命令"的错误码（§5.2 注意 4）。
        """
        return self.request('msg_rate_ver', name='PQTMSVINSTATUS',
                            rate=int(rate) if enabled else 0, ver=1)

    # ==================== 四、报文输出 ====================
    def set_message_rate(self, name, rate, msg_ver=None):
        """§6.1 单条语句速率。PQTM 语句自动补 `<MsgVer>`，NMEA/RTCM 语句必须省略。

        `<Rate>` 的语义（协议规范 §2.3.15）：**每 N 次定位输出一次**，0 = 不输出。
        不是 Hz —— 基站模式 1 Hz 时两者数值相等，移动站 10 Hz 时 `Rate=1` 是 10 Hz。
        范围按语句类型取（表 6）：NMEA 0~255、RTCM3 0~1200、EPH/RAW 只能 0 或 1。
        0 是"关闭"，**必须能下发**：还原需要把标定打开的 1005 关回去。
        """
        name = (name or '').strip()
        if not name:
            return False, '语句名不能为空', {'error': 'bad_name'}
        rate = _to_int(rate)
        if rate is None:
            return False, '速率必须是整数', {'error': 'bad_rate'}
        if is_msm_name(name):
            # MSM 走专用命令：必须带 Offset（§6.1 实测），且**按星座名分别下发**。
            # `RTCM3-107X` 只管 GPS；GLONASS/Galileo/QZSS/BDS 分别是
            # 108X/109X/111X/112X，不显式打开就一直是 0
            # （真机实测：只设 107X 时播发里只有 1074，其余四个星座全缺）。
            return self.set_msm_rate(rate, 0, name)

        low, high = rate_range(name)
        if not (low <= rate <= high):
            return False, f'{name} 的速率必须在 {low}~{high} 之间（0 表示关闭）', {
                'error': 'bad_rate', 'range': [low, high]}
        if name.upper() in _RATE_FIXED:
            # 只记 debug：这条"固件固定 1 Hz"的说法已被真机推翻（详见常量注释），
            # 再打 WARNING 只会让现场误判 NMEA 配置链路失效。
            logging.debug(f'{name} 属于历史标注的固定速率语句，实测 rate={rate} 可生效')

        if msg_ver is not None:
            return self.request('msg_rate_ver', name=name, rate=rate, ver=int(msg_ver))
        ver = msg_ver_for(name)
        if ver is not None:
            # 文档：PQTM 语句必须带 MsgVer，否则参数无效（ERROR,1）
            return self.request('msg_rate_ver', name=name, rate=rate, ver=ver)
        return self.request('msg_rate', name=name, rate=rate)

    def set_fix_rate(self, ms):
        """§4.1 定位频率。`ms` 是毫秒（1000 = 1 Hz）。

        CORS 采集按 1 Hz 采样，若模块默认是 10 Hz，`Rate=1` 的 GGA 会变成 10 Hz，
        去重逻辑虽然能挡住，但差分龄期与采样时刻会对不齐，所以显式下发。
        """
        ms = _to_int(ms)
        if ms is None or not (100 <= ms <= 1000):
            # 协议允许 100~1000 ms（1~10 Hz）
            return False, f'定位频率必须在 100~1000 ms 之间，收到 {ms}', {
                'error': 'bad_rate'}
        return self.request('fix_rate', ms=ms)

    def get_fix_rate(self):
        """查询定位频率（毫秒）。失败返回 None（不当作致命错误）。"""
        ok, message, data = self.request('get_fix_rate')
        if not ok:
            return None, message, data
        return True, message, data

    def get_message_rate(self, name, msg_ver=None):
        """§6.1 查询某条语句的速率（回读校验用）。

        查询同样受"PQTM 语句必须带 MsgVer"约束（文档 §2.3.15 注意 1）。
        实测 **未启用过的语句会回 ERROR,1**，那是"没配过"不是"不支持"：
          R,GGA                → OK,GGA,1
          R,RTCM3-1005         → OK,RTCM3-1005,0     （0 = 当前关闭）
          R,RTCM3-1033         → ERROR,1             （从未配置）
          R,PQTMEPE,2          → OK,PQTMEPE,0,2
          R,PQTMSVINSTATUS,1   → ERROR,1             （rover 模式下该语句不可用）
          R,RTCM3-107X         → OK,RTCM3-107X,1,0   （MSM 多一个 <Offset> 字段）
        所以调用方不要把这里的失败直接当致命错误。
        """
        name = (name or '').strip()
        if not name:
            return False, '语句名不能为空', {'error': 'bad_name'}
        if is_msm_name(name):
            # MSM 查询必须带星座名（`RTCM3-108X` 等），且不带 MsgVer。
            # 曾经这里硬写成查 107X，导致「查 108X 得到的是 107X 的值」——
            # 每个星座的开关其实独立，误当别名就会以为全星座都开着。
            return self.request('get_msg_rate_msm', name=name)
        ver = msg_ver if msg_ver is not None else msg_ver_for(name)
        if ver is None:
            return self.request('get_msg_rate', name=name)
        return self.request('get_msg_rate_ver', name=name, ver=int(ver))

    def set_msm_rate(self, rate=1, offset=0, name='RTCM3-107X'):
        """§6.1 MSM 速率。`name` 按星座区分：107X=GPS、108X=GLO、109X=GAL、
        111X=QZSS、112X=BDS、113X=NavIC。

        与 `set_message_rate` 分开是因为参数形态不同：MSM 必须带 `<Offset>`
        （0 ~ Rate-1），而 NMEA / 1005 之类必须**省略**它。
        真机实测（LG290P03AANR01A03S / 2024-05-22）：
            W,RTCM3-107X,1     → ERROR,1      （MSM 少写 Offset 直接不合法）
            W,RTCM3-107X,1,0   → OK
            W,RTCM3-1074,1     → ERROR,1      （具体消息号不认，只认 10xX）
            R,RTCM3-108X       → OK,RTCM3-108X,0,0

        **每个星座独立开关**：只配 107X 时播发里只有 1074，108X/109X/111X/112X
        默认全为 0。协议规范备注 3 说的"全星座共用一档、仅最后一次生效"指的是
        `<Rate>` 的**取值**需要一致（否则模块行为不确定），不是"配一条就全开"。
        """
        rate = _to_int(rate)
        if rate is None or not (1 <= rate <= 1200):
            return False, f'MSM 速率必须在 1~1200 之间，收到 {rate}', {'error': 'bad_rate'}
        offset = _to_int(offset) or 0
        if not (0 <= offset < rate):
            # Offset 必须小于 Rate（文档 §6.1 注意 4），否则模块回 ERROR,1
            return False, f'MSM 偏移时延必须在 0~{rate - 1} 之间，收到 {offset}', {
                'error': 'bad_offset'}
        return self.request('msg_rate_msm', name=name, rate=rate, offset=offset)

    def config_messages(self, rates=None, persist=True, **kwargs):
        """§6.1 批量配速率。`rates` 形如 `{'GGA': 1, 'RTCM3-1005': 1}`。

        注意 MSM 全星座共用一档速率，且只有最后一次设置生效（§6.1 注意 3）：
        传 `RTCM3-107X` 和 `RTCM3-112X` 不同值，最终按最后一条生效。
        """
        merged = dict(rates or {})
        merged.update(kwargs)
        if not merged:
            return False, '没有要配置的语句', {'error': 'empty'}

        applied = {}
        failed = {}
        for name, rate in merged.items():
            ok, message, _data = self.set_message_rate(name, rate)
            if ok:
                applied[name] = int(rate)
            else:
                failed[name] = message

        if not applied:
            return False, f'全部速率配置失败：{failed}', {'error': 'all_failed', 'failed': failed}

        if persist:
            saved, save_message, _save_data = self.save()
            if not saved:
                return False, f'速率已下发但保存失败：{save_message}', {'applied': applied}

        if failed:
            return False, f'部分成功：{list(applied)} 已配，失败 {failed}', {
                'applied': applied, 'failed': failed}
        return True, f'已配置 {len(applied)} 条语句速率', {'applied': applied}

    def config_rtcm(self, msm_type=4, eph_mode=1, eph_interval=0, persist=True):
        """§6.2 `PQTMCFGRTCM`：MSM 类型 / 仰角 / 星历。

        两个 `<Reserved>` 必须是 `07,06`（文档示例里从来不变），所以不做成参数，
        免得有人"顺手改成 0"把模块配坏。
        """
        msm_type = _to_int(msm_type)
        if msm_type is None or not (3 <= msm_type <= 7):
            return False, 'MSM 类型必须在 3~7 之间', {'error': 'bad_msm'}
        eph_mode = _to_int(eph_mode)
        if eph_mode is None or eph_mode not in (0, 1, 2, 3):
            return False, '星历模式必须是 0~3', {'error': 'bad_eph_mode'}
        eph_interval = _to_int(eph_interval) or 0
        if not (0 <= eph_interval <= 7200):
            return False, '星历间隔必须在 0~7200 秒之间', {'error': 'bad_eph_interval'}

        ok, message, data = self.request('set_rtcm', msm=msm_type, mode=0,
                                         eph_mode=eph_mode, eph_interval=eph_interval)
        if not ok:
            return False, message, data
        if persist:
            saved, save_message, _save_data = self.save()
            if not saved:
                return False, f'RTCM 配置成功但保存失败：{save_message}', {}
        return True, f'RTCM 已配置：MSM{msm_type}，星历模式 {eph_mode}', {
            'msm_type': msm_type, 'eph_mode': eph_mode, 'eph_interval': eph_interval}

    def read_rtcm_config(self):
        """§6.2 查询 RTCM 配置。"""
        return self.request('get_rtcm')

    def restore_rtcm_config(self, fields):
        """把备份里读到的 7 个字段原样写回（还原专用）。

        为什么不复用 `config_rtcm`：那个只接受 MSM 档位，会把星历模式/间隔
        一律写成默认值（1 / 0）。用户原来的模块可能是"每 3600 s 输出一次星历"，
        按档位重配就等于偷偷改了配置 —— 还原必须是"原样放回"。
        """
        values = [str(item).strip() for item in (fields or [])]
        if len(values) != 7:
            return False, f'RTCM 配置需要 7 个字段，收到 {len(values)} 个', {
                'error': 'bad_fields', 'fields': values}
        for item in values:
            # 只允许数字/负号/小数点：这些值来自我们自己读回来的应答，
            # 但备份文件是磁盘上的普通 JSON，理论上可被手改，不能不校验就拼进命令。
            if not item or any(ch not in '0123456789.-' for ch in item):
                return False, f'RTCM 字段里有非法字符：{item!r}', {
                    'error': 'bad_fields', 'fields': values}
        return self.request('set_rtcm_fields', fields=','.join(values))

    # ==================== 天线信息 ====================
    def set_antenna_info(self, desc, setup_id=0, sn='', persist=True):
        """§6.4 `PQTMCFGANTINF`（进 RTCM3-1033）。

        两个硬限制：字符串**最多 31 字符**、**禁止逗号**（逗号会破坏参数分隔）。
        写完还要 `PQTMCFGMSGRATE,W,RTCM3-1033,1` 打开 1033，否则模块不输出（§6.4 注意 1）。
        """
        if not self.caps('ant_info'):
            return False, '本型号不支持写天线信息', {'error': 'unsupported'}
        desc = str(desc or '')
        sn = str(sn or '')
        for label, value in (('天线描述', desc), ('天线 SN', sn)):
            if len(value) > 31:
                return False, f'{label}超过 31 字符上限', {'error': 'too_long'}
            if ',' in value:
                return False, f'{label}不能包含逗号', {'error': 'comma'}

        ok, message, data = self.request('set_ant_info', desc=desc,
                                         setup_id=int(setup_id or 0), sn=sn)
        if not ok:
            return False, message, data
        if persist:
            saved, save_message, _save_data = self.save()
            if not saved:
                return False, f'天线信息已写但保存失败：{save_message}', {}
        return True, '天线信息已写入（需再打开 RTCM3-1033 才会输出）', {
            'desc': desc, 'setup_id': int(setup_id or 0), 'sn': sn}

    def read_antenna_info(self):
        """§6.4 查询天线信息。"""
        ok, message, data = self.request('get_ant_info')
        if not ok:
            return False, message, data
        fields = data.get('fields') or []
        return True, message, {
            'desc': fields[0] if fields else '',
            'setup_id': _to_int(fields[1]) if len(fields) > 1 else None,
            'sn': fields[2] if len(fields) > 2 else '',
        }

    # ==================== 五、工具 ====================
    def save(self):
        """§7.1 写 NVM。它保存的是**当前全部运行态参数**，不是"给上一条命令保存"。"""
        ok, message, data = self.request('save')
        if ok:
            return True, '参数已保存到 NVM', data
        return False, message, data

    def reboot(self):
        """§7.3 软重启。SRR 没有 OK 应答，只能等 `$PQTMVER` 首帧（见 base._reboot_sequence）。"""
        return self.request('reboot')

    def factory_reset(self):
        """§7.2 恢复出厂。会清掉模式/坐标/速率 —— 基站上执行 = 直接失去基站身份。"""
        ok, message, data = self.request('factory_reset')
        if ok:
            return True, '已下发恢复出厂，重启后生效', data
        return False, message, data

    # ---------- 波特率 ----------
    # §6.8 <Parity> 的数字取值 → 常规帧格式写法，只用于展示（配置里照抄原文回写）
    PARITY_TEXT = {0: 'N', 1: 'O', 2: 'E', 3: 'M', 4: 'S'}

    def read_uart(self, port=None):
        """§6.8 `$PQTMCFGUART,R[,<Index>]` → 模块自报的串口参数。

        应答：`$PQTMCFGUART,OK,<Index>,<BaudRate>,<DataBit>,<Parity>,<StopBit>,<FlowCtrl>`
        （示例 `$PQTMCFGUART,OK,1,115200,8,0,1,0*5F`）。

        注意字段顺序：**Index 在最前面**，不在最后。按"最后一个数字当波特率"
        之类的经验写法取，会拿到流控标志 0，表现为"模块说波特率是 0"。

        不指定 port 时问的是当前这条口 —— 上位机正插着的那条，正是要问的答案。
        """
        key = 'get_baud_port' if port else 'get_baud'
        kwargs = {'port': int(port)} if port else {}
        ok, message, data = self.request(key, **kwargs)
        if not ok:
            return False, message, data

        fields = data.get('fields') or []
        if len(fields) < 2:
            # 只回 `$PQTMCFGUART,OK` 不带参数时拿不到任何有用信息，按失败处理：
            # 宁可让上层退回"按候选表试"，也不要编一个波特率出来记住。
            return False, f'PQTMCFGUART 应答缺少参数：{data.get("raw", "")}', {
                'error': 'bad_reply', 'raw': data.get('raw', '')}

        index = _to_int(fields[0])
        baud = _to_int(fields[1])
        if not baud:
            return False, f'PQTMCFGUART 应答里的波特率无法解析：{data.get("raw", "")}', {
                'error': 'bad_reply', 'raw': data.get('raw', '')}

        databits = _to_int(fields[2]) if len(fields) > 2 else None
        parity_code = _to_int(fields[3]) if len(fields) > 3 else None
        stopbits = _to_int(fields[4]) if len(fields) > 4 else None
        flow = _to_int(fields[5]) if len(fields) > 5 else None
        parity = self.PARITY_TEXT.get(parity_code, '') if parity_code is not None else ''
        info = {
            'port': index,
            'baud': baud,
            'databits': databits,
            'parity': parity,
            'parity_code': parity_code,
            'stopbits': stopbits,
            'flow': flow,
            'frame': f'{databits or "?"}{parity or "?"}{stopbits or "?"}',
            'raw': data.get('raw', ''),
        }
        label = f'UART{index}' if index else '当前串口'
        return True, f'{label} 当前 {baud} {info["frame"]}', info

    def set_baud(self, baud, port=None):
        """§2.3.10 改串口波特率。

        `port` 留空 = 只发 `<BaudRate>`，作用于**当前串口**（也就是我们正在说话的
        这个口），这正是"用户填了什么就按什么连"要的行为。
        指定 `port`（1/2/3 = UART1/2/3）时走多串口形式。

        返回里带 `reboot_required`：改波特率后当前会话就以旧速率跑了，
        必须让上层知道"下一步要重开串口"，否则继续发命令全是乱码。
        """
        try:
            value = int(baud)
        except (TypeError, ValueError):
            return False, f'波特率不合法：{baud}', {'error': 'bad baud'}
        if value <= 0:
            return False, f'波特率不合法：{baud}', {'error': 'bad baud'}

        declared = self.bauds()
        if declared and value not in declared:
            # 不拦，只把事实说清楚：有些固件在非标档位也能通，拦下来反而是错
            logging.warning(f'{self.DISPLAY_NAME} 官方档位 {declared} 不含 {value}，仍按用户要求下发')

        if port:
            ok, message, data = self.request('set_baud_port', port=int(port), baud=value)
        else:
            ok, message, data = self.request('set_baud', baud=value)
        if ok:
            data = dict(data or {})
            data['baud'] = value
            data['reboot_required'] = True
            return True, f'波特率已设为 {value}（重启后生效）', data
        return False, message, data


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


def _fmt_dec(value, min_dp=1, max_dp=4):
    """按"至少 min_dp 位、至多 max_dp 位"输出十进制串。

    为什么不直接 `f'{v:.1f}'`：那会把用户填的 0.05 变成 0.1、1.25 变成 1.2 ——
    3D 精度门限和保持距离都是米级判据，被悄悄抹掉精度等于改了用户要求。
    官方示例都是 1 位小数（`1.2` / `15.0` / `2.5`），所以默认下限 1 位；
    多填的小数位原样保留到 4 位（协议里 ECEF 用 4 位）。
    """
    number = _to_float(value)
    if number is None:
        return '0.' + '0' * min_dp
    for dp in range(int(min_dp), int(max_dp) + 1):
        text = f'{number:.{dp}f}'
        if abs(float(text) - number) < 1e-9:
            return text
    return f'{number:.{max_dp}f}'
