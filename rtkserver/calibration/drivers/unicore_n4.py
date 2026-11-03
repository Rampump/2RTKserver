# -*- coding: utf-8 -*-
"""Unicore（和芯星通）N4 系列驱动（UM960 / UM960L / UM980 / UM982）。

数据来源：《Unicore Reference Commands Manual For N4 High Precision Products》
（V2_CH_R1.1，下称"手册"）。每个方法都标了章节，改一行前先去文档核对，不要凭记忆改。

## 为什么一个手册要拆成四个驱动类

手册封面上写"高精度接收机"，正文里却是**一块板子一套命令集**。逐条命令都有
"适用产品"标注，实测统计（全书 110 处标注）：

    62 处  四型号通用（UM960、UM960L、UM980、UM982）
    25 处  仅 UM982
    19 处  UM980+UM982（无 UM960）
     3 处  仅 UM980
     1 处  仅 UM960L

差异不是"多了几条可选语句"这么轻，而是**功能大类的有无**：

    MODE ROVER [UAV|SURVEY|AUTOMOTIVE]   UM960/UM980/UM982 有，**UM960L 没有**（§3.6）
    MODE HEADING2 + GPTHS2               UM960/UM980/UM982 有（§3.7）
    CONFIG HEADING …（双天线引擎）        **仅 UM982**（§4.8）
    CONFIG ANTENNADELTAHEN（天线高）      四型号都有（§4.22）
    CONFIG MMP / PSRVELDRPOS             **仅 UM980**（§4.13 / §4.17）

把四型号压成同一个 `MODEL_KEY` 的后果是具体的：用户在页面上选了 UM960L，
程序却按 UM982 的命令集下发 `MODE ROVER SURVEY`，模块静默不响应 ——
简化 ASCII 协议**不回错误码**，现场表现就是"发了命令没反应"，无从查起。

所以：**一个型号一个类、一个 `MODEL_KEY`、一张自己的 `CMD` 表**。
四条协议差异（简化 ASCII / 双语句头 / 无 Survey-in / 坐标是经纬高）由
`UnicoreN4Base` 承载，子类只声明"我叫什么、我会哪些命令"。

## 与 Quectel 系（`$PQTM...*XX`）的本质差异 —— 两套命令集不能互相套用

1. **命令是"简化 ASCII"**：`freset` / `version` / `mode base ...` / `rtcm1074 com2 1`
   既没有 `$` 也没有校验和，参数用**空格**分隔而不是逗号（手册 §1 开头）。
   按 `$...*XX` 发出去，模块不认也不回错，现场表现就是"发命令永远没反应"。
   → 靠 `FRAME_COMMANDS = False` + `write_line` 的裸发分支解决。

2. **应答仍是 NMEA**：`#VERSIONA,...*e195b254`、`$CONFIG,COM1,...*65`、`#MODE,...*1B`。
   而且 `#` 和 `$` 两种头混排，二进制 LOG 也以 0x23 出现在同一条流里。
   → 靠 `LINE_HEADS = ('$', '#')` 解决；`message_name()` 已统一剥头。

3. **没有 Survey-in 命令**：N4 的"自主优化基站"就是 `MODE BASE TIME <秒> [距离]`（§3.3），
   没有 `$PQTMSVINSTATUS` 那样的进度语句，也没有"已收敛"标志。
   所以 `svin` 能力为 **False**，收敛进度只能靠 `MODE` 查询 + 坐标是否稳定来间接判断。

4. **坐标单位是（纬度, 经度, 高程）**：`MODE BASE <lat> <lon> <h>`，ECEF 由模块自己判
   （`param1` 超出 ±90 就当 ECEF 的 X，§3.2 表 3-4）。流程层一律给 ECEF 时，
   必须在这里转成经纬高再下发 —— 直接把 ECEF 塞进 lat/lon 位置会被当大地坐标，
   下发"纬度 -2160489°"这种值，模块静默忽略或回错误，几乎不可能定位到原因。

5. **没有开机首帧**：`BOOT_FRAME = ''`。手册全文没有"每次启动必先输出"的语句，
   `#VERSIONA` 要靠 `VERSION` 命令问才回。写死 Quectel 的 `PQTMVER` 会让每块 N4
   白等 20 秒再被判"未重启"—— 所以基类的 `_reboot_sequence()` 改为读这个属性。

一件事刻意没做：**没给 `detect()` 写"前缀匹配"**。N4 的版本应答里没有像
`LG290P03AANR01A03S` 那样的固件串，型号藏在 `#VERSIONA` 的 `"UM982"` 字段和
二进制头里的 Type 枚举（17=UM982 18=UM980 19=UM960 20=UM980i 21=UM980A
22=UM960A 24=UM960L）。所以 `MATCH_PREFIX` 留空，改由 `detect()` 查型号名，
并且**显式排除**同门的老型号（UM4B0/UB4B0/UM482…），它们也接 `version` 命令，
不排除会互相冒领。

## 新增型号怎么加（只改这一个文件 + 一行注册）

1. 在 `_PRODUCT_TO_CLASS` 里把 `#VERSIONA` 的产品名映射到新类（`detect()` 靠它分派）；
2. 新类继承 `UnicoreN4Base`，声明 `MODEL_KEY` / `DISPLAY_NAME` / `PRODUCT_MODEL` /
   `REQUIRES` 或 `EXCLUDES`（按手册的"适用产品"裁剪 `CMD`）/ `CAPS`；
3. 加 `@register`，并把 `MODEL_KEY` 追加到 `driver_manager._BUILTIN_MODULES`
   —— 不，是追加到本文件末尾的 `REGISTERED_KEYS`（`_BUILTIN_MODULES` 只认模块名）。

命令的统一语义键（`set_mode` / `set_message_rate` / `save` …）由 `CMD` 表提供，
流程层只认这些键，**任何地方都不出现具体命令串**。
"""
import logging
import time

from .base import BaseDriver, error_info, message_name
from .driver_manager import _REGISTRY, register

# ==================== 命令表（语义键 → 命令体） ====================
# 全部是**简化 ASCII**（裸文本、空格分隔、无校验和），所以这里出现的模板里
# 一个逗号都没有。改模板时务必保持这一点，否则会被当成 Quectel 风格发出去。
#
# 这张表是**四型号都能用**的公共集（手册里标"四型号通用"的那些）。
# 只在部分型号上存在的命令由子类的 REQUIRES / EXCLUDES 裁剪，见各类的注释。
CMD = {
    # ---- 一、识别与信息查询（§7.3.1 VERSION / §1 表 1-1）----
    'identify':        'VERSION',
    'version':         'VERSION',

    # ---- 二、工作模式（§3）----
    'get_mode':        'MODE',
    # 精确坐标基站（§3.2）：MODE BASE [ID] <lat> <lon> <height>
    'set_base_fixed':  'MODE BASE {lat} {lon} {h}',
    'set_base_fixed_id': 'MODE BASE {id} {lat} {lon} {h}',
    # 自主优化基站（§3.3）：MODE BASE [ID] TIME <T> [Distance]
    'set_base_time':   'MODE BASE TIME {seconds}',
    'set_base_time_dist': 'MODE BASE TIME {seconds} {distance}',
    'set_base_time_id': 'MODE BASE {id} TIME {seconds}',
    # 只设基站 ID（§3.5）
    'set_base_id':     'MODE BASE {id}',
    # 缺省参数基站（§3.4）：当前定位 60 秒均值
    'set_base_default': 'MODE BASE',

    # ---- 三、串口（§4.2）----
    'get_config':      'CONFIG',
    'set_baud':        'CONFIG {port} {baud}',
    'set_baud_frame':  'CONFIG {port} {baud} {databits} {parity} {stopbits}',
    'get_baud':        'CONFIG',

    # ---- 四、天线（§4.22 / §4.26）----
    # 天线高：影响 RTCM1006 里"地面标识点 → 天线参考点"的描述（§4.22 表 4-30）
    'set_ant_delta':   'CONFIG ANTENNADELTAHEN {h} {east} {north}',
    'set_ant_model':   'CONFIG BASEANTENNAMODEL "{name}" {sn} {setup_id} {type}',

    # ---- 五、截止高度角（§5.2 MASK）----
    'get_mask':        'MASK',
    'set_elev_mask':   'MASK {degree}',

    # ---- 六、报文输出（§1 表 1-1 + 附录 2）----
    # rtcm<消息号> com<口> <间隔秒>；gpgga/gprmc/gpzda/gpvth/gpths com<口> <间隔秒>
    # 间隔语义是**秒**：1006/1033 写 10 = 每 10 秒一次；MSM 写 1 = 1 Hz（§1.1 表 1-2）。
    'rtcm_msg':        'RTCM{msg} {port} {interval}',
    'nmea_msg':        '{name} {port} {interval}',
    'unlog':           'UNLOG',
    'unlog_msg':       'UNLOG {name}',
    'unlog_port':      'UNLOG {port}',
    'unlog_port_msg':  'UNLOG {port} {name}',

    # ---- 七、工具（§8）----
    'save':            'SAVECONFIG',
    'factory_reset':   'FRESET',
    'reset':           'RESET',
}

# 流动站（§3.6）**不属于**公共集：手册明写"适用产品：UM960、UM980、UM982"，
# UM960L 不在其中。所以单独抽出来给那三个型号合并进自己的 CMD。
ROVER_CMD = {
    'set_rover':       'MODE ROVER',
    'set_rover_type':  'MODE ROVER {profile}',
    'set_rover_sub':   'MODE ROVER {profile} {variant}',
}

# Heading2 定向（§3.7）：适用 UM960、UM980、UM982
HEADING2_CMD = {
    'set_heading2':    'MODE HEADING2',
}

# 双天线 Heading 引擎（§4.8）：**仅 UM982**（手册 §4.8 表 4-12）
HEADING_CMD = {
    'set_heading_engine':   'CONFIG HEADING {mode}',
    'set_heading_offset':   'CONFIG HEADING OFFSET {yaw} {pitch}',
    'set_heading_length':   'CONFIG HEADING LENGTH {length} {mode}',
}

# 仅 UM980 的功能开关（§4.13 MMP / §4.17 PSRVELDRPOS）
UM980_ONLY_CMD = {
    'set_mmp':         'CONFIG MMP {mode}',
    'set_psrveldrpos': 'CONFIG PSRVELDRPOS {mode}',
}

CAPS = {
    'dual_mode': True,        # 可切移动站/基站（§3）
    'set_mode': True,
    # §3.3 的 `MODE BASE TIME` 是"自主优化 60 秒取均值"，语义上确实等于 Survey-in，
    # 但**没有** PQTM 那套"最少定位次数 / 3D 精度门限 / 收敛状态语句"。
    # 标成 True 会让流程层去等一个永远不来的 SVINSTATUS，然后超时失败，
    # 所以这里保持 False，由 writer 走"自主优化基站"分支而不是"Survey-in"分支。
    'svin': False,
    'fixed': True,            # 精确坐标（§3.2）
    'ant_info': True,         # CONFIG BASEANTENNAMODEL（§4.26，影响 1005/1006/1007/1033）
    # 天线高（§4.22 ANTENNADELTAHEN，影响 RTCM1006）。四型号都有这条命令，
    # 但"天线偏移/天线高"整个功能在产品层面已定案不做（见 MEMORY：不参与 RTK 测量
    # 工程就不需要 APC→ARP），所以默认 None = 不下发。留着命令体是为了
    # 「现场确实需要时一个开关就能打开」，而不是重新写一遍协议。
    'ant_delta': None,
    'msg_rate': True,         # rtcmXXXX / gpgga 逐条配间隔
    'rtcm_out': True,         # 同上
    'heading2': False,        # 双天线 Heading2 定向，子类按手册打开
    'err3': False,            # 简化 ASCII 不返回数字错误码，只有"不响应"或 `$command,response:...`
}

# 基站播发的 MSM 星座清单。N4 是**逐条消息号**独立开关（`rtcm1074 com2 1`），
# 不像 Quectel 那样全星座共用一档。名字沿用 Quectel 的 `RTCM3-107X` 口径，
# 由 `set_message_rate()` 翻译成 `RTCM1 074` 这样的命令。
MSM_CONSTELLATIONS = ('RTCM3-107X',   # GPS      → rtcm1074
                      'RTCM3-108X',   # GLONASS  → rtcm1084
                      'RTCM3-109X',   # Galileo  → rtcm1094
                      'RTCM3-111X',   # QZSS     → rtcm1114
                      'RTCM3-112X')   # BDS      → rtcm1124

# 官方波特率（§4.2 表 4-4）：COM1/COM2/COM3 三档完全一致。FRESET 后回 115200。
BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
DEFAULT_BAUD = 115200
BAUD_CMD_KEY = 'set_baud'

# 串口名：命令里写 com1/com2/com3，应答里写成 COM1（§4.1 消息输出示例）。
PORTS = ('COM1', 'COM2', 'COM3')
# 串口帧格式只有这一种可选（§4.2 表 4-3：数据位仅 8、校验仅 N、停止位仅 1）
FRAME_FIXED = (8, 'N', 1)

# 型号名 → 内部型号键。`#VERSIONA` 第 1 个字段就是这个字符串（带引号）。
# 手册只写了四个主力型号；UM960A/UM980I/UM980A 是同一套命令集的衍生款，
# 按"同族同命令集"归到对应型号上，`detect()` 会如实报出真实产品名。
PRODUCT_MODELS = {
    'UM960': 'um960',
    'UM960L': 'um960l',
    'UM960A': 'um960',
    'UM980': 'um980',
    'UM980I': 'um980',
    'UM980A': 'um980',
    'UM982': 'um982',
}
# 同门但**不是** N4 系列的老型号（VERSION 的 Type 枚举 1~16、23）。
# 它们同样接 `version`，不显式排除就会互相冒领 —— 而它们的命令集和 N4 不一样，
# 认错型号比认不出来严重得多（会往一块不认识的板子上写基站配置）。
LEGACY_PRODUCTS = ('UB4B0', 'UM4B0', 'UM480', 'UM440', 'UM482', 'UM442', 'UB482',
                   'UT4B0', 'UT900', 'UB362L', 'UB4B0M', 'UB4B0J', 'UM482L',
                   'UM4B0L', 'UT910', 'CLAP-B', 'CLAP-C')

# 流动站工作模式（§3.6 表 3-8）。参数 2 留空 = 该大类默认配置。
ROVER_PROFILES = ('UAV', 'SURVEY', 'AUTOMOTIVE')
ROVER_VARIANTS = {
    'UAV': ('DEFAULT', 'FORMATION'),
    'SURVEY': ('DEFAULT', 'MOW'),
    'AUTOMOTIVE': ('DEFAULT',),
}
# 各型号出厂默认流动站模式（§3.6 表 3-9）。用于"恢复出厂后该按哪档配"。
DEFAULT_ROVER = {
    'um980': ('SURVEY', 'DEFAULT'),
    'um982': ('UAV', 'DEFAULT'),
    'um960': ('SURVEY', 'MOW'),
}

# NMEA 语句名 → 命令里的写法。N4 只认小写无前缀形式（§1 表 1-1）。
NMEA_ALIASES = {
    'GGA': 'GPGGA', 'RMC': 'GPRMC', 'ZDA': 'GPZDA',
    'VTG': 'GPVTG', 'THS': 'GPTHS', 'GLL': 'GPGLL',
    'GSA': 'GPGSA', 'GSV': 'GPGSV', 'GNS': 'GPGNS',
}

# 这些语句名不是"报告速率"而是"报告状态/信息"，不能配间隔（§4.1 / §5.1）。
# 写进 `set_message_rate` 会拼出 `CONFIG XYZ 1` 这种模块不认的命令。
_NON_RATE_NAMES = ('CONFIG', 'MASK', 'VERSION', 'MODE')

RATE_RANGE = (0, 3600)      # 间隔（秒）：0 = 关闭，上限按 1006 类慢播报留足余量


# ==================== 公共基类：四条协议差异的唯一定义处 ====================
class UnicoreN4Base(BaseDriver):
    """N4 系列的**协议公共层**（简化 ASCII / 双语句头 / 无 Survey-in / 经纬高坐标）。

    刻意不 `@register`：它没有独立的型号身份，注册进去会让 `list_supported_modules()`
    多出一个"不存在的型号"，前端下拉里就出现一个选了必然失败的选项。
    真正的型号身份在下面四个子类上。

    子类只声明三样东西：`MODEL_KEY`、`DISPLAY_NAME`、`PRODUCT_MODEL`（`#VERSIONA`
    里那个字符串），外加自己的 `CMD` / `CAPS`。所有命令的**发送方式**都在这里，
    所以"新加一个型号"绝不会碰协议层，"协议改一个字"也不必改四遍。
    """

    # 数据来源标注（前端"关于/型号详情"要显示，别让用户以为是我们编的）
    DOC_TITLE = 'Unicore Reference Commands Manual For N4 High Precision Products V2_CH_R1.1'

    MATCH_PREFIX = ''          # 见文件头"一件事刻意没做"
    DUAL_MODE = True
    CAPS = CAPS
    CMD = CMD
    CS_EXPECT = {}             # 没有任何一条命令自带校验和
    MSM_CONSTELLATIONS = MSM_CONSTELLATIONS

    BAUDS = BAUDS
    DEFAULT_BAUD = DEFAULT_BAUD
    BAUD_CMD_KEY = BAUD_CMD_KEY

    # 简化 ASCII：裸文本发出去，应答认 `$` 和 `#` 两种头（见文件头 "1." 与 "2."）
    FRAME_COMMANDS = False
    LINE_HEADS = ('$', '#')
    # 手册全文没有"每次启动必先输出"的语句，`#VERSIONA` 要问才回 ——
    # 声明空串，`_reboot_sequence()` 就不会去死等一个不存在的开机帧。
    BOOT_FRAME = ''
    # 命令名与应答名不完全相同的几组（N4 特有）。不写这张表，
    # `VERSION` 发出去后模块回 `#VERSIONA`，`classify()` 认不出，
    # 就会一直等到超时 —— 现场表现是"模块明明回了还是报无应答"。
    REPLY_ALIASES = {
        'VERSION': ('VERSIONA',),
        'MODE': ('MODEA',),
        'CONFIG': ('CONFIGA',),
        'MASK': ('MASKA',),
    }

    # 默认的语句输出口。N4 每个 `LOG` 指令都要带端口名（`rtcm1074 com2 1`），
    # 而流程层调 `set_message_rate()` 时不传端口 —— 需要有默认值。
    # 取 COM2 而不是 COM1：COM1 一般是命令/调试口，COM2 才是对外播差分与 NMEA 的，
    # 这也是手册 §1.1 基准站示例 (`rtcm1074 com2 1`) 的用法。
    DEFAULT_OUT_PORT = 'COM2'

    # 该型号在 `#VERSIONA` 里的产品名（子类必须覆盖）。`detect()` 靠它认领。
    PRODUCT_MODEL = ''

    # ==================== 零、裸命令出口 ====================
    def _raw(self, body, timeout=None, retries=None):
        """发一条**不在 CMD 表里**的裸命令（`UNLOG` / `SAVECONFIG` / `RESET` / `FRESET`）。

        这些命令要么无参数、要么参数组合太多（`UNLOG [port] [msg]` 的四种形态），
        放进 CMD 表反而要靠一堆可选模板拼，不如就地拼字符串。
        `send()` 在 Channel 上而不在 BaseDriver 上，所以这里不能写 `self.send()`。
        """
        status, line = self.channel.send(body, timeout=timeout, retries=retries)
        if status == 'timeout':
            return False, f'{body} 无应答（已重试）', {'error': 'timeout', 'body': body}
        if status == 'error':
            code = self._parse_error_code(line)
            info = error_info(code)
            return False, f'{info["error_text"]}（错误码 {code}）；{info["advice"]}', info
        return True, line.strip(), {
            'fields': line.strip().lstrip(''.join(self.channel.heads or ('$',)))
                              .split('*')[0].split(',')[1:],
            'raw': line.strip()}

    # ==================== 一、识别与信息 ====================
    def detect(self):
        """§7.3.1：`VERSION` 的应答里读产品名，再判断"是不是我"。

        应答形态（手册原样）：
            `#VERSIONA,94,GPS,FINE,2190,117325000,0,0,18,160;`
            `"UM982","R4.10Build5251","HRPT00-S10C-P","-","ffff48ffff0fffff","2021/11/26"*e195b254`

        与 Quectel 完全不同：这里**不是纯逗号分隔**，二进制头之后用 `;` 分段，
        每个字段是带引号的 JSON 风格字符串。所以不能用 `request()` 的 `fields[0]`，
        必须自己切 —— 这也是本驱动覆盖 `info()` 而不用基类字段映射的原因。

        **返回的 `model_key` 是"实际识别到的那个型号"，不是 `self.MODEL_KEY`**：
        自动探测时 `match_driver()` 用的是临时实例，型号反过来由识别结果决定。
        若这里返回自身的 key，用户选了 UM960L 却插着 UM982 也会"识别成功"，
        然后按 UM960L 的命令集去配一块 UM982 —— 静默错配，比认不出来严重得多。
        """
        ok, message, data = self.request('identify')
        if not ok:
            return False, message, data

        parsed = self.parse_version(data.get('raw') or message)
        product = (parsed.get('product') or '').upper()
        if not product:
            return False, f'VERSION 应答里没有产品名（原文：{(data.get("raw") or "")[:80]}）', {
                'error': 'no_product', 'raw': data.get('raw')}
        if product in LEGACY_PRODUCTS:
            return False, f'这是 {product}，不属于 N4 系列（命令集不同，不能套用本驱动）', {
                'product': product, 'error': 'legacy_product'}
        if product not in PRODUCT_MODELS:
            return False, f'未收录的 N4 型号 {product}（已知：{"/".join(sorted(PRODUCT_MODELS))}）', {
                'product': product, 'error': 'unknown_product'}

        model_key = PRODUCT_MODELS[product]
        if model_key != self.MODEL_KEY:
            # 自动探测阶段（临时实例的 key 是基类的 ''）会走到这里，是正常的；
            # 用户手选了型号时则说明型号不符，必须报出来而不是"将错就错"。
            if self.MODEL_KEY:
                return False, (f'这是 {product}（型号键 {model_key}），'
                               f'不是本驱动负责的 {self.DISPLAY_NAME}'), {
                    'product': product, 'model_key': model_key,
                    'error': 'model_mismatch'}

        return True, f'识别到 {product}', {
            'product': product,
            'model_key': model_key,
            'firmware': parsed.get('firmware') or '',
            'build_date': parsed.get('build_date') or '',
            'sn': parsed.get('psn') or '',
            'auth': parsed.get('auth') or '',
            'hw_version': parsed.get('hw_version') or '',
            'efuse_id': parsed.get('efuse_id') or '',
            'raw': parsed.get('raw'),
        }

    @staticmethod
    def parse_version(raw):
        """把 `#VERSIONA,...;"UM982","R4.10Build5251",...*XXXXXXXX` 拆成字典。

        切法：先按 `;` 分成"二进制头"和"载荷"两段，载荷按 `","` 切再剥引号。
        为什么不用 csv：字段里可能出现 `-` / 空格（PN 号、固件串），
        而 `"` 是唯一明确的边界符，按引号切不会切错。

        字段顺序（表 7-50）：Type(产品) / sw version / Auth / Psn / efuse ID / comp time。
        手册里第 1 个字段是 Type **枚举**，但 ASCII 输出给的是**字符串**产品名，
        所以直接按字符串用，不需要再维护一份枚举映射。
        """
        text = (raw or '').strip()
        head, _, payload = text.partition(';')
        # 载荷末尾的 `*XXXX` 是 32 位 CRC（手册表 7-50 第 8 项），不是 NMEA 8 位校验和
        payload = payload.split('*')[0].strip()
        values = []
        for chunk in payload.split('","'):
            values.append(chunk.strip().strip('"'))
        result = {
            'raw': text,
            'payload': payload,
            'product': values[0] if values else '',
            'firmware': values[1] if len(values) > 1 else '',
            'auth': values[2] if len(values) > 2 else '',
            'psn': values[3] if len(values) > 3 else '',
            'efuse_id': values[4] if len(values) > 4 else '',
            'build_date': values[5] if len(values) > 5 else '',
            'head': head,
        }
        # PN 与 SN 在同一个字段里，用 `-` 分隔（表 7-50 第 5 项：前 13 位 PN、后 15 位 SN）
        psn = result['psn']
        if '-' in psn:
            result['pn'], _, result['sn'] = psn.partition('-')
        else:
            result['pn'], result['sn'] = psn, ''
        return result

    def read_version(self):
        """单独读版本（识别之后流程里还要再读一次用于落库）。"""
        ok, message, data = self.request('version')
        if not ok:
            return False, message, data
        return True, message, self.parse_version(data.get('raw') or message)

    def info(self):
        """型号 / 固件 / SN / 能力标记。

        与 Quectel 的 info() 结构保持一致（键名相同），流程层和前端不用分支。
        区别只有两条：N4 的 SN 要和 PN 一起查（同一个字段），且**没有**唯一 ID 命令 ——
        所以 `uniq_id` 留空，`sn` 直接取自 VERSION 的 Psn 字段（本来就能唯一标识一块板）。
        """
        ok, message, data = self.detect()
        if not ok:
            return False, message, data

        result = dict(data)
        result['display_name'] = self.DISPLAY_NAME
        # detect() 给出的才是"实际是哪块板"；self.MODEL_KEY 只表示"谁认领的"
        result['model_key'] = data.get('model_key') or self.MODEL_KEY
        result['model_name'] = self.DISPLAY_NAME
        # N4 没有独立的 SN 命令，VERSION 里的 Psn 就是唯一标识
        result['uniq_id'] = result.get('sn') or ''
        result['sn_source'] = 'version_psn' if result.get('sn') else ''

        mode_ok, mode_message, mode_data = self.get_mode()
        result['mode'] = mode_data.get('mode', 'unknown') if mode_ok else 'unknown'
        if mode_ok:
            result['mode_raw'] = mode_data.get('raw_mode') or ''
        else:
            result['mode_error'] = mode_message

        # 串口波特率：VERSION/CONFIG 查询顺带读回来，落库时能对上"当时按哪一档通的"
        config_ok, _config_message, config_data = self.read_port_config()
        if config_ok:
            result['baud'] = config_data.get('COM1', {}).get('baud')

        result['caps'] = dict(self.runtime_caps)
        return True, f'{result["product"]} / {result["firmware"]} / SN {result["sn"] or "(未读到)"}', result

    def probe_caps(self):
        """运行时探测可选命令。

        N4 不返回数字错误码（`err3` 为 False），"不支持"的表现是**静默不响应**。
        所以这里不能用 Quectel 那套"ERROR,3 → 关能力位"的逻辑：
        超时既可能是"不支持"，也可能是"这条命令这条固件改了语法的写法"，
        贸然关掉能力位会让本来能用的功能变灰。因此本方法只做记录，不改能力集。
        """
        return {}

    # ==================== 二、模式 ====================
    def get_mode(self):
        """§3.1 查询：`MODE` → `#MODE,...,18,518;MODE ROVER SURVEY,*1B`。

        应答里 `;` 之后才是模式描述（`MODE ROVER SURVEY` / `MODE BASE` /
        `MODE BASE TIME` / `MODE HEADING2`）。三种基站的应答都是 `MODE BASE` 开头，
        所以判断顺序必须"先精确后前缀"，否则 `MODE BASE TIME` 会被当成普通基站。
        """
        ok, message, data = self.request('get_mode')
        if not ok:
            return False, message, data

        raw_mode = self._mode_text(data.get('raw') or message)
        mode, variant = self._classify_mode(raw_mode)
        if self.channel is not None:
            # 缓存最近一次模式：上层要区分"没开 NMEA 输出"和"还在 rover 模式"
            self.channel.cache['mode_text'] = raw_mode
        return True, f'当前模式：{raw_mode or mode}', {
            'mode': mode, 'variant': variant, 'raw_mode': raw_mode}

    @staticmethod
    def _mode_text(raw):
        """从 `#MODE,...;MODE ROVER SURVEY,*1B` 里取出 `MODE ROVER SURVEY`。

        两种应答形态都要处理（实测同一固件在不同命令下会给不同的形态）：
            1) `#MODE,1,2,3;MODE ROVER SURVEY,*1B`  —— 带 `;`，`;` 之后是模式文本
            2) `#MODE,95,BASE*1B`                   —— 不带 `;`，模式名是最后一个逗号段

        形态 2 是 `MODE BASE` 之类**简写命令**的应答，此时 `95` 是头字节而非模式名。

        **不能在这两种形态里都用"找 MODE 关键字"的办法**：形态 2 的头字段本身
        就叫 `MODE`，`index('MODE')` 会命中它，返回 `MODE,95,BASE`，
        `_classify_mode` 吃到这个串判成 unknown —— 早期版本正是这个 bug，
        现场症状是"写了坐标却回读不到 base"。
        所以：先处理带 `;` 的形态；不带 `;` 的形态只在关键字**前面是空格**时
        （真正的 `MODE xxx` 文本）才按关键字切。
        """
        text = (raw or '').strip()
        if ';' in text:
            # 形态 1：`;` 之后是模式描述，关键字一定在段首
            text = text.split(';', 1)[1].split('*')[0].strip().strip(',')
            upper = text.upper()
            for marker in ('HEADINGMODE', 'MODE'):
                if marker in upper:
                    return text[upper.index(marker):].strip().strip(',')
            return text

        text = text.split('*')[0].strip().strip(',')
        upper = text.upper()
        # 形态 2：头字段 `MODE` 后面紧跟 `,`，不能当成模式文本。
        # 只在关键字前有空格（或整串以关键字+空格开头）时才按关键字切。
        for marker in ('HEADINGMODE', 'MODE'):
            start = 0
            while True:
                index = upper.find(marker, start)
                if index < 0:
                    break
                before = upper[:index]
                after = upper[index + len(marker):]
                # `… ;MODE BASE…` 已在上面处理；这里要求关键字不是头字段
                if not before.rstrip() and after.startswith(' '):
                    return text[index:].strip().strip(',')
                start = index + len(marker)

        # 没有可用的关键字：最后一个逗号段是模式名（BASE / ROVER / HEADING2 / ...）
        pieces = [p.strip() for p in text.split(',') if p.strip()]
        if pieces and pieces[-1].upper() in ('BASE', 'ROVER', 'HEADING2', 'HEADING', 'NONE'):
            return f'MODE {pieces[-1].upper()}'
        return text.strip().strip(',')

    @staticmethod
    def _classify_mode(raw_mode):
        """`MODE BASE TIME` → ('base', 'time')；`MODE ROVER SURVEY` → ('rover', 'survey')。

        **必须"先精确后前缀"**：三种基站的应答都以 `MODE BASE` 开头
        （§3.2 精确坐标 / §3.3 自主优化 / §3.4 缺省参数），
        用 `startswith('MODE BASE')` 一概判成 base 的话，
        `set_mode('base')` 的回读永远成功，而用户其实要的是"自主优化 60 秒"。
        返回的第二个值是 `variant`，让流程层能区分三种基站。
        """
        text = (raw_mode or '').strip().upper().removeprefix('MODE').strip()
        if not text:
            return 'unknown', ''
        if text.startswith('BASE'):
            rest = text[len('BASE'):].strip()
            if not rest:
                return 'base', 'default'
            first = rest.split()[0]
            if first == 'TIME':
                return 'base', 'time'
            # `MODE BASE 1234 <lat> <lon> <h>`：剩下的都是坐标，变体是 fixed
            if first.lstrip('-').replace('.', '').isdigit():
                return 'base', 'fixed'
            return 'base', first.lower()
        if text.startswith('ROVER'):
            rest = text[len('ROVER'):].strip()
            return 'rover', (rest.split()[0].lower() if rest else '')
        if text.startswith('HEADING2'):
            return 'heading2', ''
        if text.startswith('HEADING'):
            return 'heading', ''
        return 'unknown', ''

    def set_mode(self, mode, persist=True, restart=True, verify=True, **kwargs):
        """切工作模式（§3）：`MODE BASE` / `MODE ROVER [profile [variant]]` / `MODE HEADING2`。

        **不写死任何一条命令**：语义键 `set_base_default` / `set_base_time` /
        `set_base_fixed` / `set_rover` / `set_heading2` 都由 `CMD` 表提供，
        子类没声明哪个键，这里就报"本型号不支持"。
        UM960L 没有 `set_heading2`（手册 §3.7 的适用产品不含它），
        所以对它调 `set_mode('heading2')` 会在这里被拦下 —— 而不是发一条模块不认的命令。

        `base` 模式有三档，靠关键字区分（手册 §3.2/3.3/3.4 是三条不同命令）：
            - `variant='time'`   + `seconds`  → `MODE BASE TIME <n>`     自主优化
            - `variant='fixed'`  + 坐标参数   → `MODE BASE <id> <lat> <lon> <h>` 精确坐标
            - 不传 variant                    → `MODE BASE`               缺省参数

        基站三档**必须分开**：都用 `MODE BASE` 一概而论的后果是
        用户要"自主优化 60 秒"，模块却按缺省参数立刻播发一个未经收敛的坐标。
        """
        mode = (mode or '').lower()
        variant = (kwargs.pop('variant', '') or '').lower()

        if mode == 'base':
            if variant in ('time', 'survey_in'):
                if 'set_base_time' not in self.CMD:
                    return False, f'{self.DISPLAY_NAME} 没有自主优化模式', {
                        'error': 'unsupported'}
                seconds = kwargs.pop('seconds', kwargs.pop('count', 60))
                key, params = 'set_base_time', {'seconds': seconds}
            elif variant == 'fixed':
                if 'set_base_fixed' not in self.CMD:
                    return False, f'{self.DISPLAY_NAME} 没有精确坐标模式', {
                        'error': 'unsupported'}
                key, params = 'set_base_fixed', dict(kwargs)
            elif variant in ('', 'default'):
                key, params = 'set_base_default', {}
            else:
                return False, (f'基站的 variant 只能是 time / fixed / default，'
                               f'收到 {variant!r}'), {'error': 'bad_variant'}
        elif mode == 'rover':
            if not self.caps('dual_mode'):
                return False, '本型号不是双模，不能切移动站', {'error': 'no_dual_mode'}
            if 'set_rover' not in self.CMD:
                return False, (f'{self.DISPLAY_NAME} 没有流动站模式'
                               f'（手册 §3.6 适用产品不含本型号）'), {'error': 'unsupported'}
            # 细分档位交给 set_rover()：它是"这家厂商才有"的附加能力，
            # 塞进基类约定的 set_mode 签名里会让别的厂商驱动无从实现。
            return self.set_rover(
                profile=kwargs.get('profile'), variant=kwargs.get('variant'),
                persist=persist, restart=restart, verify=verify)
        elif mode == 'heading2':
            if not self.caps('heading2'):
                return False, (f'{self.DISPLAY_NAME} 不支持 Heading2 定向'
                               f'（手册 §3.7 适用产品不含本型号）'), {'error': 'unsupported'}
            key, params = 'set_heading2', {}
        else:
            return False, f'不支持的模式：{mode}（可选 base / rover / heading2）', {
                'error': 'bad_mode'}

        ok, message, data = self.request(key, **params)
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
            want = 'time' if variant in ('time', 'survey_in') else (
                'fixed' if variant == 'fixed' else '')
            return self.apply_and_verify(
                lambda: self._reapply(key, persist, **params),
                lambda: self._check_mode(mode, want))
        return True, f'已下发切换到 {mode} 模式', {'mode': mode, 'variant': variant}

    def set_rover(self, profile=None, variant=None, persist=True, restart=True, verify=True):
        """§3.6 流动站细分：`MODE ROVER [UAV|SURVEY|AUTOMOTIVE] [DEFAULT|FORMATION|MOW]`。

        单独一个方法（而不是塞进 `set_mode(..., profile=)`）是因为
        `set_mode` 是**基类约定的接口**，签名不能按厂商需要随意加参数 ——
        流程层只调 `set_mode('rover')`，细分档位是"这家厂商才有"的附加能力，
        由上层按需调用，没有就自然消失。
        """
        if 'set_rover' not in self.CMD:
            return False, (f'{self.DISPLAY_NAME} 没有流动站模式'
                           f'（手册 §3.6 适用产品不含本型号）'), {'error': 'unsupported'}

        profile = (profile or '').upper()
        variant = (variant or '').upper()
        if not profile:
            # 不传就取出厂默认（表 3-9），而不是"随便挑一个" ——
            # UM982 的出厂默认是 UAV，UM980 是 SURVEY，选错了对精度有影响。
            profile, default_variant = DEFAULT_ROVER.get(self.MODEL_KEY, ('SURVEY', 'DEFAULT'))
            variant = variant or default_variant
        if profile not in ROVER_PROFILES:
            return False, f'未知流动站大类：{profile}（可选 {"/".join(ROVER_PROFILES)}）', {
                'error': 'bad_profile'}
        if variant and variant not in ROVER_VARIANTS.get(profile, ()):
            return False, (f'{profile} 没有 {variant} 这一档'
                           f'（可选 {"/".join(ROVER_VARIANTS.get(profile, ())) or "无"}）'), {
                'error': 'bad_variant'}

        if variant:
            key, params = 'set_rover_sub', {'profile': profile, 'variant': variant}
        else:
            key, params = 'set_rover_type', {'profile': profile}

        ok, message, data = self.request(key, **params)
        if not ok:
            return False, message, data

        if persist:
            saved, save_message, save_data = self.save()
            if not saved:
                return False, f'切流动站成功但保存失败：{save_message}', save_data
        if restart:
            rebooted, reboot_message, reboot_data = self.reboot()
            if not rebooted:
                return False, f'流动站已保存但重启失败：{reboot_message}', reboot_data
        if verify:
            return self.apply_and_verify(
                lambda: self._reapply(key, persist, **params),
                lambda: self._check_mode('rover'))
        return True, f'已切换到流动站 {profile} {variant or ""}'.strip(), {
            'mode': 'rover', 'profile': profile, 'variant': variant}

    def set_mode_memory_only(self, mode):
        """只改运行态切模式：不 SAVECONFIG、不重启、不碰 NVM。

        N4 的 `MODE ...` 本身就是写当前运行态，`SAVECONFIG` 才落 Flash（§8.1）。
        所以"仅内存切换"是天然的：下发后立刻回读，回读一致就算成功。
        回读不一致就如实报失败，**不要**为了"看起来成功"偷偷存一次 Flash ——
        那会把用户正在用的基准站配置改掉，而这正是本方法要避免的事。
        """
        return self.set_mode(mode, persist=False, restart=False, verify=True)

    # ---------- 回读比对（方案 §14 #6 的 check 侧） ----------
    def _reapply(self, key, persist, **kwargs):
        """重发一次配置命令；`persist` 为真时再存一次 NVM（**不再重启**）。

        重试阶段故意不 RESET：重启是为了让 NVM 生效，而这里要修的是
        "刚下发到 RAM 的值没读回来"。再等一次重启既慢，又可能把串口等到超时，
        对"参数到底生效没有"这个问题没有任何帮助。
        """
        ok, message, data = self.request(key, **kwargs)
        if not ok:
            return False, message, data
        if persist:
            saved, save_message, save_data = self.save()
            if not saved:
                return False, f'重发配置成功但保存失败：{save_message}', save_data
        return True, '已重发配置', {'retried': True}

    def _check_mode(self, mode, variant=''):
        """回读模式；`variant` 非空时连基站档位一起核对。

        基站三档（缺省参数 / 自主优化 / 精确坐标）在 `#MODE` 应答里都**以 `MODE BASE` 开头**，
        只比 `mode` 的话，用户要"自主优化 60 秒"、模块实际停在缺省参数上也判通过 ——
        而且下一条命令 `_check` 永远 True，重试机制完全失效。
        """
        ok, message, data = self.get_mode()
        if not ok:
            return False, f'回读模式失败：{message}', data
        if data.get('mode') != mode:
            return False, f'回读不一致：期望 {mode}，实际 {data.get("mode")}（原文 {data.get("raw_mode")}）', data
        if variant and data.get('variant') != variant:
            return False, (f'回读不一致：期望基站 {variant} 档，'
                           f'实际 {data.get("variant")}（原文 {data.get("raw_mode")}）'), {
                'error': 'verify_mismatch', 'readback': data}
        return True, f'已切换到 {mode} 模式', data

    def _check_fixed(self, lat, lon, height, tolerance=0.01):
        """回读固定坐标并与写入值比对。

        容差 1 cm 是按"下发时保留 9 位小数"定的：更严会让浮点打印误差误判，
        更松就区分不出"写进去了"和"写的是上一个坐标"。
        """
        ok, message, data = self.get_fixed_ecef()
        if not ok:
            return False, f'回读坐标失败：{message}', data
        got = data.get('position') or {}
        try:
            got_lat = float(got.get('lat', 0.0))
            got_lon = float(got.get('lon', 0.0))
            got_h = float(got.get('height', 0.0))
        except (TypeError, ValueError):
            return False, f'回读坐标不可解析：{got}', {'error': 'verify_mismatch', 'readback': data}
        # 经纬度的单位是**度**，1 cm 约 9e-8 度，所以先按 111320 m/度换成米再比 ——
        # 直接拿"度差"和 0.01 比，等于要求精度到 1 米，永远判"已写入"。
        worst_m = max(abs(got_lat - lat) * 111320.0,
                      abs(got_lon - lon) * 111320.0,
                      abs(got_h - height))
        if worst_m > tolerance:
            return False, f'回读坐标与写入值差 {worst_m:.4f} m（超过 {tolerance} m），判定未生效', {
                'error': 'verify_mismatch', 'delta': worst_m, 'readback': data}
        data['verify_delta'] = worst_m
        data['written'] = {'lat': lat, 'lon': lon, 'height': height}
        return True, f'固定坐标已写入（回读偏差 {worst_m:.4f} m）', data

    def _check_svin(self, seconds, distance=None):
        ok, message, data = self.get_svin_config()
        if not ok:
            return False, f'回读自主优化基站配置失败：{message}', data
        if data.get('mode') != 'base' or data.get('variant') != 'time':
            return False, f'回读不一致：期望自主优化基站，实际 {data.get("raw_mode")}', {
                'error': 'verify_mismatch', 'readback': data}
        if data.get('seconds') is not None and int(data['seconds']) != int(seconds):
            return False, (f'回读不一致：期望 {int(seconds)} 秒，'
                           f'实际 {data["seconds"]} 秒'), {'error': 'verify_mismatch', 'readback': data}
        return True, f'自主优化基站已配置：{int(seconds)} 秒', data

    # ==================== 三、坐标与"自主优化基站" ====================
    def get_svin_config(self):
        """读基站配置，**结构对齐 Quectel 的 `PQTMCFGSVIN` 应答**。

        N4 没有 Survey-in 配置语句（见文件头 "3."），能读出来的只有当前模式。
        所以这里把 `MODE` 的三种基站形态翻译成同义的 `mode` 字段：
            `MODE BASE`            → mode='base', variant='default'（缺省参数基站）
            `MODE BASE TIME n`     → mode='base', variant='time'   （自主优化 = Survey-in 等价物）
            `MODE BASE id lat …`   → mode='base', variant='fixed'  （精确坐标）

        流程层只认 `mode` 的三态（disabled / survey_in / fixed），
        所以这里额外给出 `mode_norm`：`time` → `survey_in`、`fixed` → `fixed`、
        其余 → `disabled`。**不要在流程层写"如果是 N4 就看 variant"** ——
        各家方言的翻译必须停在驱动里，这是"只写流程不写命令"的前提。

        坐标字段 `ecef_x/y/z` 在 N4 上读不到（`MODE` 应答不带坐标），
        如实给 `None` 而不是给 0：给 0 会让上层以为"模块配了 0,0,0 坐标"，
        而 `set_fixed_ecef` 的校验逻辑会拿 0 去比对，直接误判。
        """
        ok, message, data = self.get_mode()
        if not ok:
            return False, message, data

        mode = data.get('mode')
        variant = data.get('variant') or ''
        raw_mode = data.get('raw_mode') or ''
        norm = {'fixed': 'fixed', 'time': 'survey_in'}.get(variant, 'disabled') \
            if mode == 'base' else 'disabled'

        result = {
            'mode': mode,                 # 'base' / 'rover' / 'heading2' / 'unknown'
            'variant': variant,           # 'default' / 'time' / 'fixed' / '' 
            'mode_norm': norm,            # 归一化后的三态，给流程层用
            'raw_mode': raw_mode,
            'count': None,                # N4 没有"最少定位次数"，只有秒
            'acc_limit': None,            # N4 没有 3D 精度门限
            'seconds': None,              # 自主优化的时长（§3.3）
            'distance': None,             # §3.3 的保持距离（可选）
            'ecef_x': None, 'ecef_y': None, 'ecef_z': None,
        }
        if variant == 'time':
            seconds, distance = self._parse_base_time(raw_mode)
            result['seconds'] = seconds
            result['distance'] = distance
        return True, f'当前模式：{raw_mode or mode}', result

    @staticmethod
    def _parse_base_time(raw_mode):
        """从 `MODE BASE [id] TIME <秒> [距离]` 里取秒数与距离。

        `MODE` 的**查询**应答（§3.1）只回模式名，不带参数；能拿到参数的
        只有 `MODE BASE TIME` 命令的回显。所以这两个值常常是 None —— 
        那是事实，不要编一个默认 60 出来。
        """
        text = (raw_mode or '').upper()
        if 'TIME' not in text:
            return None, None
        pieces = text.split('TIME')[1].split()
        seconds = _to_int(pieces[0]) if pieces else None
        distance = _to_float(pieces[1]) if len(pieces) > 1 else None
        return seconds, distance

    def get_fixed_ecef(self):
        """读当前基站坐标，**统一返回 ECEF**（与 Quectel 驱动的键名一致）。

        N4 的 `MODE` 应答不带坐标，所以这里只能说明"当前是不是固定坐标模式"。
        要真正读到坐标值，得从模块播发的 `1005/1006` 里取 —— 那是 `Channel`
        缓存的事（`rtcm_frames`），不属于驱动查询。

        返回 `position` 里同时给经纬高（N4 原生口径）和换算出的 ECEF，
        这样流程层不必知道"这家厂商是经纬高、那家是 ECEF"。
        """
        ok, message, data = self.get_svin_config()
        if not ok:
            return False, message, data
        if data.get('mode_norm') != 'fixed':
            return True, (f'当前不是固定坐标模式（{data.get("raw_mode") or data.get("mode")}），'
                          f'坐标以模块实时播发为准'), data
        return True, message, data

    def set_fixed_ecef(self, x, y, z, persist=True, restart=True, verify=True):
        """写精确坐标基站（§3.2）：**流程层给 ECEF，这里转成经纬高再下发**。

        `MODE BASE [ID] <lat> <lon> <h>` 的参数是**大地坐标**（纬度、经度、椭球高），
        而项目里流程层全用 ECEF（Quectel 是 ECEF 口径，统一在接口上更省事）。
        直接把 ECEF 塞进 lat/lon 位置，模块会收到"纬度 -2160489°"这种值：
        简化 ASCII **不回错误码**，表现就是"发了没反应、回读还是旧坐标"，
        根本查不到原因。所以转换必须在这里做，而且要有范围校验。
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

        lat, lon, height = ecef_to_geodetic(x, y, z)
        return self.set_fixed_geodetic(lat, lon, height, persist=persist,
                                       restart=restart, verify=verify)

    def set_fixed_geodetic(self, lat, lon, height, base_id=None,
                           persist=True, restart=True, verify=True):
        """按**经纬高**写精确坐标基站（N4 原生口径）。

        纬度/经度用 9 位小数：1e-9 度约 0.1 mm，远超 RTK 的厘米级需求，
        又不会像 15 位那样把命令拉长到可能超串口缓冲。
        """
        lat = _to_float(lat)
        lon = _to_float(lon)
        height = _to_float(height)
        if lat is None or not (-90.0 <= lat <= 90.0):
            return False, f'纬度超出 ±90°：{lat}', {'error': 'bad_lat'}
        if lon is None or not (-180.0 <= lon <= 180.0):
            return False, f'经度超出 ±180°：{lon}', {'error': 'bad_lon'}
        if height is None or not (-1000.0 <= height <= 20000.0):
            return False, f'椭球高超出 -1000~20000 m：{height}', {'error': 'bad_height'}

        params = {'lat': _fmt_deg(lat, 9), 'lon': _fmt_deg(lon, 9), 'h': f'{height:.4f}'}
        key = 'set_base_fixed'
        if base_id is not None:
            base_id = _to_int(base_id)
            if base_id is None or not (0 <= base_id <= 4095):
                # §3.5：ID 范围 0~4095（RTCM 1005/1006 的 Station ID 字段位数决定）
                return False, f'基站 ID 必须在 0~4095 之间，收到 {base_id}', {
                    'error': 'bad_id'}
            params['id'] = int(base_id)
            key = 'set_base_fixed_id'

        ok, message, data = self.request(key, **params)
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
                lambda: self._reapply(key, persist, **params),
                lambda: self._check_fixed(lat, lon, height))
        return True, '固定坐标已下发', {'written': {'lat': lat, 'lon': lon, 'height': height,
                                              'ecef': geodetic_to_ecef(lat, lon, height)}}

    def config_survey_in(self, count, acc_limit=None, distance=None,
                         persist=True, restart=True, verify=True):
        """"自主优化基站"（§3.3）：`MODE BASE [ID] TIME <秒> [距离]`。

        **这是 N4 上 Survey-in 的等价物，但不是同一个东西**：
        它按时间取均值，没有"最少定位次数"也没有"3D 精度门限"，
        更没有 `$PQTMSVINSTATUS` 那种进度语句（见文件头 "3."）。
        所以 `count` 在这里的含义是**秒数**：Quectel 的 60 次 @1 Hz 恰好也是 60 秒，
        数值上是通的，这一点必须在文档里写清楚，否则以后有人以为
        "填 120 就是要在 120 次定位内收敛"。

        `acc_limit` 收下但不使用（N4 没有这个参数），记进返回里让调用方知道
        "这个要求本型号无法保证"，而不是假装配了。
        """
        seconds = _to_int(count)
        if seconds is None or not (1 <= seconds <= 86400):
            return False, f'自主优化时长必须在 1~86400 秒之间，收到 {count}', {
                'error': 'bad_count'}

        params = {'seconds': int(seconds)}
        key = 'set_base_time'
        if distance is not None:
            distance = _to_float(distance)
            if distance is None or not (0.0 <= distance <= 10.0):
                return False, f'保持距离必须在 0.0~10.0 米之间，收到 {distance}', {
                    'error': 'bad_distance'}
            params['distance'] = _fmt_num(distance, 1)
            key = 'set_base_time_dist'

        ok, message, data = self.request(key, **params)
        if not ok:
            return False, message, data

        if persist:
            saved, save_message, save_data = self.save()
            if not saved:
                return False, f'自主优化配置成功但保存失败：{save_message}', save_data
        if restart:
            rebooted, reboot_message, reboot_data = self.reboot()
            if not rebooted:
                return False, f'配置已保存但重启失败：{reboot_message}', reboot_data
        if verify:
            verified, verify_message, verify_data = self.apply_and_verify(
                lambda: self._reapply(key, persist, **params),
                lambda: self._check_svin(seconds, distance))
            if isinstance(verify_data, dict):
                verify_data.setdefault('acc_limit_ignored', acc_limit)
            return verified, verify_message, verify_data

        result = {'mode': 'base', 'variant': 'time', 'seconds': int(seconds)}
        if acc_limit is not None:
            result['acc_limit_ignored'] = acc_limit
        return True, f'自主优化基站已下发：{int(seconds)} 秒', result

    def disable_survey_in(self, persist=True, restart=False):
        """停用自动收敛：把基站从"自主优化"改成"缺省参数基站"（§3.4）。

        §3.4 的 `MODE BASE`（不带参数）取**当前定位 60 秒均值**作为坐标，
        下发即完成、不再持续收敛，这正是"停掉上一轮的 Survey-in"要的效果。
        注意它**不是**"关闭基站"：N4 没有"基站但不用坐标"这种状态。

        默认 `restart=False`：停用本身立刻生效，没必要为它多等一次重启；
        调用方要一次彻底生效，把 `restart=True` 与后续步骤合并即可。
        """
        ok, message, data = self.request('set_base_default')
        if not ok:
            return False, message, data
        if persist:
            saved, save_message, save_data = self.save()
            if not saved:
                return False, f'停用成功但保存失败：{save_message}', save_data
        if restart:
            rebooted, reboot_message, reboot_data = self.reboot()
            if not rebooted:
                return False, f'停用已保存但重启失败：{reboot_message}', reboot_data
        return True, '已停用自动收敛（改为缺省参数基站）', {
            'mode': 'base', 'variant': 'default', 'mode_norm': 'disabled'}

    def survey_status(self):
        """读收敛进度。**N4 没有进度语句**，只能靠"坐标稳定 + 时间过去"间接判断。

        返回 `error='no_status'` 并说明原因，而不是编一个 `valid=1` 出来 ——
        上层拿到 `valid=2`（已收敛）就会立刻去读坐标，而此刻模块可能还在取均值，
        读到的坐标是浮动的半成品，会被当成"标定结果"落库。

        能给出的证据：当前是不是 `MODE BASE TIME`（收敛会自己结束并转成固定解）、
        GGA 的定位质量位、以及最近一次 RTK 差分龄期。这三个够上层做
        "再等等 / 已经稳了"的判断，但**不足以**给出"已收敛"的确定结论。
        """
        ok, message, data = self.get_svin_config()
        if not ok:
            return False, message, data

        gga = (self.channel.cache.get('gga') if self.channel else None) or {}
        evidence = {
            'mode_norm': data.get('mode_norm'),
            'variant': data.get('variant'),
            'seconds': data.get('seconds'),
            'fix_quality': gga.get('quality'),
            'fix_text': gga.get('quality_text'),
            'satellites': gga.get('satellites'),
            'raw_mode': data.get('raw_mode'),
        }
        if data.get('mode_norm') == 'survey_in':
            return False, ('N4 没有收敛进度语句（手册无对应章节），'
                           '只能按自主优化时长和 GGA 定位质量判断；'
                           f'当前质量 {gga.get("quality_text") or "未知"}，'
                           f'卫星 {gga.get("satellites") or "?"} 颗'), {
                'error': 'no_status', 'reason': 'vendor_has_no_status', **evidence}
        return False, ('当前不是自主优化基站模式'
                       f'（{data.get("raw_mode") or data.get("mode")}），无收敛进度可言'), {
            'error': 'no_status', 'reason': 'not_survey_in', **evidence}

    def enable_survey_status(self, enabled=True, rate=1):
        """N4 没有收敛状态语句，所以这个开关**无事可做**。

        保留方法是为了接口齐全：流程层会统一调它，本型号明确返回"不支持"，
        让它走"没有状态输出"的分支，而不是去 `cache['svin']` 里等一个永不为真的值。
        """
        return False, (f'{self.DISPLAY_NAME} 没有收敛状态语句，无法开启状态输出'), {
            'error': 'unsupported', 'reason': 'vendor_has_no_status'}

    # ==================== 四、串口 ====================
    def read_port_config(self):
        """§4.1 查询三个串口的配置：`CONFIG` → `$CONFIG,COM1,115200,8,N,1,...`。

        应答是**每个串口一行**（COM1/COM2/COM3 各回一条），
        而 `request()` 只等一条 —— 所以这里自己读：发一次命令，把接下来的
        几条 `CONFIG` 应答全部收进字典，直到三个口都到齐或超时。
        """
        status, line = self.channel.send('CONFIG', timeout=1.5, retries=2)
        if status == 'timeout':
            return False, 'CONFIG 无应答（已重试）', {'error': 'timeout'}
        if status == 'error':
            code = self._parse_error_code(line)
            return False, f'{error_info(code)["error_text"]}（错误码 {code}）', error_info(code)

        ports = {}
        self._absorb_config_line(line, ports)
        # 继续收剩余端口：每口一行，三行最多等 1.5 秒
        deadline = time.time() + 1.5
        while len(ports) < len(PORTS) and time.time() < deadline:
            extra = self.channel.read_line(min(0.4, max(0.05, deadline - time.time())))
            if not extra:
                continue
            if message_name(extra) in ('CONFIG', 'CONFIGA'):
                self._absorb_config_line(extra, ports)
        if not ports:
            return False, f'CONFIG 应答不可解析：{line.strip()[:80]}', {'error': 'bad_reply',
                                                                      'raw': line}
        return True, f'读到 {len(ports)} 个串口配置', ports

    @staticmethod
    def _absorb_config_line(line, ports):
        """`$CONFIG,COM1,115200,8,N,1,0,0` → {'COM1': {'baud': 115200, ...}}。

        字段顺序（手册 §4.1 的 CONFIG 应答）：
            语句名, 端口, 波特率, 数据位, 校验, 停止位, [保留...]
        解析失败的端口**不放进结果**，而不是放一个默认 115200 的假值 ——
        假值会让"读波特率"看起来永远成功，实际读到的是我们编的。
        """
        payload = (line or '').strip().lstrip('$#').split('*')[0]
        fields = [item.strip() for item in payload.split(',')]
        if len(fields) < 3:
            return
        port = fields[1].upper()
        if port not in PORTS:
            return
        baud = _to_int(fields[2])
        if not baud:
            return
        ports[port] = {
            'port': port,
            'baud': baud,
            'databits': _to_int(fields[3]) if len(fields) > 3 else None,
            'parity': fields[4] if len(fields) > 4 else '',
            'stopbits': _to_int(fields[5]) if len(fields) > 5 else None,
        }

    def get_baud(self, port=None):
        """读某个串口的波特率；不指定就取 COM1（命令口）。"""
        ok, message, data = self.read_port_config()
        if not ok:
            return False, message, data
        port = (port or 'COM1').upper()
        entry = data.get(port)
        if not entry:
            return False, f'没有读到 {port} 的配置（读到 {sorted(data)}）', {
                'error': 'port_missing', 'ports': data}
        return True, f'{port} 当前 {entry["baud"]}', entry

    def set_baud(self, baud, port=None, databits=None, parity=None, stopbits=None):
        """§4.2 改串口波特率：`CONFIG <port> <baud>` （帧格式可选）。

        `port` 留空 = 改 COM1 —— 但要注意 N4 的命令里**必须带端口名**，
        与 Quectel 的 `PQTMCFGPORT`（省略就作用于当前口）不同。
        这也是"两套命令集不能互相套用"的具体例子。

        返回值带 `reboot_required`：改完当前会话还是旧速率，
        上层必须知道"下一步要重开串口"，否则继续发命令全是乱码。
        """
        try:
            value = int(baud)
        except (TypeError, ValueError):
            return False, f'波特率不合法：{baud}', {'error': 'bad_baud'}
        if value <= 0:
            return False, f'波特率不合法：{baud}', {'error': 'bad_baud'}

        declared = self.bauds()
        if declared and value not in declared:
            # 不拦，只把事实说清楚：有些固件在非标档位也能通，拦下来反而是错
            logging.warning(f'{self.DISPLAY_NAME} 官方档位 {declared} 不含 {value}，'
                            f'仍按用户要求下发')

        port = _port_name(port) or 'COM1'
        if databits is not None or parity is not None or stopbits is not None:
            # §4.2 表 4-3：只有 8-N-1 一种合法组合，别的一个都不认
            frame = (int(databits or FRAME_FIXED[0]),
                     str(parity or FRAME_FIXED[1]).upper(),
                     int(stopbits or FRAME_FIXED[2]))
            if frame != FRAME_FIXED:
                return False, (f'N4 只支持 {FRAME_FIXED[0]}{FRAME_FIXED[1]}{FRAME_FIXED[2]} '
                               f'（8 数据位 / 无校验 / 1 停止位），收到 {frame}'), {
                    'error': 'bad_frame'}
            ok, message, data = self.request(
                'set_baud_frame', port=port, baud=value,
                databits=frame[0], parity=frame[1], stopbits=frame[2])
        else:
            ok, message, data = self.request('set_baud', port=port, baud=value)

        if ok:
            data = dict(data or {})
            data['port'] = port
            data['baud'] = value
            data['reboot_required'] = True
            return True, f'{port} 波特率已设为 {value}（重启后生效）', data
        return False, message, data

    # ==================== 五、报文输出（§1 表 1-1 / 附录 2） ====================
    def set_message_rate(self, name, rate, port=None):
        """给某条语句配输出间隔：`rtcm<消息号> com<n> <秒>` / `gpgga com<n> <秒>`。

        `rate` 的语义是**秒**（间隔），不是"每 N 次定位一次"：
        `rtcm1074 com2 1` = 每 1 秒播一次；`0` = 关闭。
        这个差异很要命 —— Quectel 的 `Rate=1` 在 10 Hz 下是 10 Hz 输出，
        而这里 `1` 恒等于 1 Hz，与定位频率无关。

        `name` 接受三种写法，方便流程层沿用 Quectel 的口径：
            `RTCM3-1074` / `1074` / `RTCM1074`  → `rtcm1074`
            `GGA` / `$GPGGA` / `GPGGA`          → `gpgga`
        """
        name = (name or '').strip()
        if not name:
            return False, '语句名不能为空', {'error': 'bad_name'}
        rate = _to_int(rate)
        if rate is None:
            return False, '间隔必须是整数秒', {'error': 'bad_rate'}
        if not (RATE_RANGE[0] <= rate <= RATE_RANGE[1]):
            return False, f'间隔必须在 {RATE_RANGE[0]}~{RATE_RANGE[1]} 秒之间（0 表示关闭）', {
                'error': 'bad_rate', 'range': list(RATE_RANGE)}

        port = _port_name(port) or self.DEFAULT_OUT_PORT
        message_no = _rtcm_message_number(name)
        if message_no is not None:
            key, params = 'rtcm_msg', {'msg': f'{message_no:04d}', 'port': port, 'interval': rate}
        else:
            token = self._nmea_token(name)
            if not token:
                return False, (f'无法把 {name!r} 翻译成 N4 的语句名'
                               f'（RTCM 用 1005/1074 这类消息号，NMEA 用 GGA/RMC/…）'), {
                    'error': 'bad_name'}
            if token in _NON_RATE_NAMES:
                # `CONFIG`/`MASK`/`VERSION`/`MODE` 是"报告配置"不是"按间隔播报"，
                # 拼成 `CONFIG com2 1` 模块不认，而且会把真正的 CONFIG 命令挡住。
                return False, f'{token} 不是按间隔播报的语句，不能配输出间隔', {
                    'error': 'not_rate_msg'}
            key, params = 'nmea_msg', {'name': token.lower(), 'port': port, 'interval': rate}
        return self.request(key, **params)

    @staticmethod
    def _nmea_token(name):
        """把各种写法的 NMEA 名统一成 N4 命令里的小写无前缀形式（`gpgga`）。

        剥的顺序：`$` → `GP` 前缀 → 查 `NMEA_ALIASES`。
        为什么要查别名表：`THS` 在 N4 里只能配 `gpths`，
        而 `GPGGAH`（仅 UM982 的 Heading 版 GGA）不能剥成 `GGAH` ——
        别名表里没有的一律原样保留，让模块自己去拒绝，比我们瞎猜安全。
        """
        text = (name or '').strip().lstrip('$').upper()
        if text.startswith('GP') and len(text) > 2:
            text = text[2:]
        return NMEA_ALIASES.get(text, text).upper()

    def get_message_rate(self, name, port=None):
        """查某条语句当前的间隔。

        N4 **没有**"查询单条语句输出配置"的命令（`CONFIG` 只回串口配置），
        所以这里如实返回"查不到"。上层拿到 `error='unsupported'` 就走
        "配置已下发、不做回读校验"的分支，而不是把它当致命错误。
        """
        return False, (f'{self.DISPLAY_NAME} 没有查询单条语句输出间隔的命令'
                       f'（手册 §4.1 的 CONFIG 只回串口参数）'), {
            'error': 'unsupported', 'name': name}

    def config_messages(self, rates=None, persist=True, port=None, **kwargs):
        """批量配间隔：`{'GGA': 1, 'RTCM3-1005': 10, 'RTCM3-1074': 1}`。

        N4 是**逐条消息号独立开关**（`rtcm1074 com2 1`），不像 Quectel 的
        MSM 那样全星座共用一档 —— 所以这里老老实实一条一条下发，
        每条失败单独记账，最后一起报。
        """
        merged = dict(rates or {})
        merged.update(kwargs)
        if not merged:
            return False, '没有要配置的语句', {'error': 'empty'}

        applied, failed = {}, {}
        for name, rate in merged.items():
            ok, message, _data = self.set_message_rate(name, rate, port=port)
            if ok:
                applied[name] = int(rate)
            else:
                failed[name] = message
        if not applied:
            return False, f'全部间隔配置失败：{failed}', {'error': 'all_failed', 'failed': failed}

        saved = False
        if persist:
            saved, save_message, _save_data = self.save()
            if not saved:
                return False, f'间隔已下发但保存失败：{save_message}', {'applied': applied}
        if failed:
            return False, f'部分成功：{list(applied)} 已配，失败 {failed}', {
                'applied': applied, 'failed': failed, 'saved': saved}
        return True, f'已配置 {len(applied)} 条语句间隔', {'applied': applied, 'saved': saved}

    def set_rtcm_output(self, enabled=True, port=None, msm_rate=1,
                        base_station_rate=10, persist=True, names=None):
        """一键配基准站播发：MSM + 1005/1006/1033。

        为什么 1005 用 10 秒而 MSM 用 1 秒：1005/1006 是**静态的基站坐标描述**，
        每个历元重播一遍纯属占带宽；MSM 是观测值，必须跟着历元走（§1.1 表 1-2）。
        间隔的单位都是**秒**，所以这里的 10 就是"每 10 秒一次"，不是"每 10 个历元"。
        """
        port = _port_name(port) or self.DEFAULT_OUT_PORT
        rate_on, rate_off = (1 if enabled else 0), 0
        targets = {}
        for constellation in (names or self.MSM_CONSTELLATIONS):
            msg = _rtcm_name_number(constellation)
            if msg is not None:
                targets[f'RTCM3-{msg}'] = msm_rate if enabled else rate_off
        for slow in ('1005', '1033'):
            targets[f'RTCM3-{slow}'] = base_station_rate if enabled else rate_off
        return self.config_messages(targets, persist=persist, port=port)

    def config_rtcm(self, msm_type=4, eph_mode=1, eph_interval=0, persist=True):
        """N4 **没有**"全局 RTCM 配置"命令：MSM 档位由消息号决定（1074 = MSM4）。

        保留方法是接口齐全。想从 MSM4 换成 MSM7，得改播发消息号
        （`rtcm1077` 而不是 `rtcm1074`），那是 `set_rtcm_output()` 的事。
        """
        msm_type = _to_int(msm_type)
        if msm_type is None or not (3 <= msm_type <= 7):
            return False, 'MSM 类型必须在 3~7 之间', {'error': 'bad_msm'}
        return False, (f'{self.DISPLAY_NAME} 没有全局 RTCM 配置命令；'
                       f'MSM 档位由播发的消息号决定（MSM{msm_type} = 107{msm_type}），'
                       f'请改用 set_rtcm_output / set_message_rate'), {
            'error': 'unsupported', 'hint': 'use_set_message_rate', 'msm_type': msm_type}

    def read_rtcm_config(self):
        """N4 没有 RTCM 配置查询命令 —— 播发项就是当前 `LOG` 列表，读不回来。"""
        return False, (f'{self.DISPLAY_NAME} 没有查询 RTCM 播发配置的命令'), {
            'error': 'unsupported'}

    def restore_rtcm_config(self, fields):
        """还原播发配置：N4 的"配置"就是一串 `LOG`，没有可原样放回的字段。

        真正的还原应该重发备份里记下的 `applied` 字典（键是语句名、值是间隔），
        由 `set_rtcm_output` / `config_messages` 按型号自己的命令重新下发 ——
        这正是"备份记语义、不记命令"的好处：换型号也能还原。
        """
        return False, (f'{self.DISPLAY_NAME} 的播发配置没有可回写的字段结构；'
                       f'请用备份里的语句名清单重新下发'), {
            'error': 'unsupported', 'hint': 'replay_applied_rates'}

    def unlog(self, port=None, name=None):
        """§5.3 关闭输出：`UNLOG [port] [语句名]`，四种形态。

        - `UNLOG`              关掉**全部**输出
        - `UNLOG <port>`       关掉这个口的全部输出
        - `UNLOG <name>`       关掉这条语句（所有口）
        - `UNLOG <port> <name>` 关掉这个口的这条语句
        用 `_raw()` 而不是 CMD 表：四种形态要靠"参数有没有"拼，
        放进模板表会变成四行 `{}` 占位，反而更易错。
        """
        port_name = _port_name(port)
        body = 'UNLOG'
        if port_name and name:
            body = f'UNLOG {port_name} {name}'
        elif port_name:
            body = f'UNLOG {port_name}'
        elif name:
            body = f'UNLOG {name}'
        return self._raw(body)

    def set_fix_rate(self, ms):
        """定位频率：N4 用 `CONFIG RATECFG`（§4.3）还是 `MODE`？

        **手册里没有单条的定位频率命令** —— 观测采样率跟播发间隔走
        （`rtcm1074 com2 1` 就是 1 Hz）。所以这里不下发任何东西，
        让上层知道"本型号的频率由报文间隔决定"。
        """
        return False, (f'{self.DISPLAY_NAME} 没有独立的定位频率命令；'
                       f'采样率随报文间隔走（把 MSM 间隔设为 1 秒即 1 Hz）'), {
            'error': 'unsupported', 'hint': 'rate_follows_message_interval'}

    # ==================== 六、天线 ====================
    def set_antenna_info(self, desc, setup_id=0, sn='', persist=True):
        """§4.26 `CONFIG BASEANTENNAMODEL "<名称>" <SN> <setup_id> <类型>`（进 RTCM3-1033）。

        三个硬限制（手册 §4.26）：
            - 名称最长 16 字符（**不是** Quectel 的 31，抄过来会静默截断）
            - 一律用双引号包住，名称里不能有引号
            - setup_id 0~255

        写完还要 `rtcm1033 com2 <间隔>` 打开 1033，否则模块不输出 ——
        这一步交给 `set_rtcm_output()`，因为"要不要 1033"是流程层的决定。
        """
        if not self.caps('ant_info'):
            return False, '本型号不支持写天线信息', {'error': 'unsupported'}
        desc = str(desc or '').strip()
        sn = str(sn or '').strip()
        if len(desc) > 16:
            return False, f'天线名称超过 16 字符上限（N4 口径，非 Quectel 的 31）：{desc}', {
                'error': 'too_long'}
        if '"' in desc or '"' in sn:
            return False, '天线名称 / SN 不能包含双引号（会破坏命令里的引号包裹）', {
                'error': 'bad_quote'}
        setup_id = _to_int(setup_id) or 0
        if not (0 <= setup_id <= 255):
            return False, f'setup_id 必须在 0~255 之间，收到 {setup_id}', {'error': 'bad_id'}

        ok, message, data = self.request('set_ant_model', name=desc, sn=sn or '-',
                                         setup_id=setup_id, type='ADVNULLANTENNA')
        if not ok:
            return False, message, data
        if persist:
            saved, save_message, _save_data = self.save()
            if not saved:
                return False, f'天线信息已写但保存失败：{save_message}', {}
        return True, '天线信息已写入（需再打开 RTCM3-1033 才会输出）', {
            'desc': desc, 'setup_id': setup_id, 'sn': sn}

    def set_elevation_mask(self, degree, persist=True):
        """§5.2 截止高度角：`MASK <度>`，范围 0~90。"""
        degree = _to_float(degree)
        if degree is None or not (0.0 <= degree <= 90.0):
            return False, f'截止高度角必须在 0~90 度之间，收到 {degree}', {'error': 'bad_mask'}
        ok, message, data = self.request('set_elev_mask', degree=_fmt_num(degree, 1))
        if not ok:
            return False, message, data
        if persist:
            self.save()
        return True, f'截止高度角已设为 {degree}°', {'degree': degree}

    def get_elevation_mask(self):
        """§5.2 查询：`MASK` → `$MASK,10*XX`。"""
        ok, message, data = self.request('get_mask')
        if not ok:
            return False, message, data
        fields = [f for f in (data.get('fields') or []) if str(f).strip()]
        if not fields:
            return False, f'MASK 应答里没有角度：{message}', {'error': 'bad_reply'}
        return True, message, {'degree': _to_float(fields[-1]), 'raw': data.get('raw')}

    def set_ant_delta(self, height, east=0.0, north=0.0, persist=True):
        """§4.22 `CONFIG ANTENNADELTAHEN <高> <东> <北>`（影响 RTCM1006）。

        **默认关着**（`CAPS['ant_delta'] = None`）：本产品不做 RTK 测量工程，
        天线相位中心到地面标识点的偏移没有意义。命令体保留，
        现场确实需要时把能力位打开即可，不必重写一遍协议。
        """
        if not self.caps('ant_delta'):
            return False, ('本产品不做天线偏移（APC→ARP）——那是 RTK 测量工程的工序；'
                           '如需启用请打开驱动的 ant_delta 能力位'), {'error': 'unsupported'}
        height = _to_float(height)
        if height is None or not (-10.0 <= height <= 10.0):
            return False, f'天线高必须在 -10~10 米之间，收到 {height}', {'error': 'bad_height'}
        ok, message, data = self.request(
            'set_ant_delta', h=_fmt_num(height, 4),
            east=_fmt_num(_to_float(east) or 0.0, 4),
            north=_fmt_num(_to_float(north) or 0.0, 4))
        if ok and persist:
            self.save()
        return ok, message, data

    # ==================== 七、持久化与工具 ====================
    def save(self):
        """§8.1 `SAVECONFIG`：把当前运行态参数写进 Flash。

        它保存的是**当前全部参数**，不是"给上一条命令保存" —— 
        所以任何 `set_*` 之后单独存一次都是安全的，不会只保存某一条。
        """
        ok, message, data = self.request('save')
        if ok:
            return True, '参数已保存到 Flash', data
        return False, message, data

    def reboot(self):
        """§8.2 `RESET`：软重启。没有 OK 应答，只能等串口恢复（见 base._reboot_sequence）。

        键名是 `'reset'`（`CMD` 表里的声明），**不是** `'reboot'` ——
        流程层的方法名和命令表的键名是两套命名，这里一旦写错，
        现场表现是"切完模式就崩在 DriverBug: 驱动没有命令键 'reboot'"。
        """
        if 'reset' not in self.CMD:
            return False, f'{self.DISPLAY_NAME} 没有重启命令', {'error': 'unsupported'}
        return self._reboot_sequence(self.body('reset'))

    def factory_reset(self):
        """§8.3 `FRESET`：恢复出厂。会清掉模式/坐标/速率 —— 基站上执行 = 直接失去基站身份。"""
        ok, message, data = self.fetch_raw('factory_reset')
        if ok:
            return True, '已下发恢复出厂，重启后生效', data
        return False, message, data

    def fetch_raw(self, key):
        """按键名取命令体后用 `_raw()` 发（用于无应答 / 非标准应答的命令）。"""
        body = self.CMD.get(key)
        if body is None:
            return False, f'{self.MODEL_KEY} 驱动没有命令键 {key!r}', {'error': 'no_key'}
        return self._raw(body)

    # ==================== 八、语句解析 ====================
    def parse_line(self, line):
        """N4 的语句解析：把基类的 NMEA 解析包一层，补上 `#VERSIONA` 与 `MODE`。

        基类 `proto.nmea.parse_line` 只认标准 NMEA（GGA/RMC/GSA）。N4 多两类：
            `#VERSIONA,...`  —— 版本（识别用）
            `#MODE,...`      —— 当前模式（`MODE` 查询的应答，也可能主动输出）
            `$CONFIG,...`    —— 串口配置（一个口一行）
        这三类不解析出来，`Channel.absorb()` 就没法把它们塞进缓存，
        `info()` / `survey_status()` 就得各自重发一遍命令。

        基类解析器返回 None 的语句在这里兜底，返回非 None 的**直接用基类结果**，
        保证 GGA 的字段名（`quality` / `satellites` / `lat` / `lon`）与 Quectel 完全一致 ——
        流程层拿 GGA 的时候不该知道插的是哪家的板子。
        """
        parsed = nmea_parse_shim(line)
        if parsed is not None:
            return parsed

        name = message_name(line)
        if name in ('VERSION', 'VERSIONA'):
            info = self.parse_version(line)
            info['type'] = 'VER'
            # `ver_str` / `build_date` 两个键名与 Quectel 的 `$PQTMVERNO` 对齐，
            # 基类 `_reboot_sequence` 取的是 `cache['ver']['ver_str']`
            info['ver_str'] = info.get('firmware') or ''
            return info
        if name in ('MODE', 'MODEA'):
            raw_mode = self._mode_text(line)
            mode, variant = self._classify_mode(raw_mode)
            return {'type': 'MODE', 'mode': mode, 'variant': variant,
                    'raw_mode': raw_mode, 'raw': line.strip()}
        if name in ('CONFIG', 'CONFIGA'):
            return {'type': 'CONFIG', 'fields': _split_nmea(line), 'raw': line.strip()}
        if name in ('BESTNAV', 'BESTNAVA'):
            return self._parse_bestnav(line)
        return None

    @staticmethod
    def _parse_bestnav(line):
        """`#BESTNAVA,...` 高精度定位结果（表 7-2x）。

        用途只有一个：基站模式下模块关掉 NMEA 后，GGA 不来了，
        但 BESTNAV 还能证明"定位还在、质量如何"。取前 6 个字段够用：
        解状态、卫星数、纬度、经度、高、速度。
        """
        fields = _split_nmea(line)
        return {
            'type': 'BESTNAV',
            'sol_status': _to_int(fields[0]) if fields else None,
            'num_sats': _to_int(fields[4]) if len(fields) > 4 else None,
            'lat': _to_float(fields[5]) if len(fields) > 5 else None,
            'lon': _to_float(fields[6]) if len(fields) > 6 else None,
            'height': _to_float(fields[7]) if len(fields) > 7 else None,
            'raw': line.strip(),
        }

    # ==================== 九、能力自检（供 --selftest） ====================
    @classmethod
    def command_keys(cls):
        """本型号 CMD 表里的全部语义键（自检用）。"""
        return tuple(sorted(cls.CMD))


# ==================== 四个型号：只声明"我是谁、我会哪些命令" ====================
# 每个类里的 `CMD = {**CMD, ...}` 是**唯一**的型号差异表达方式：
# 流程层永远只写 `request('set_heading2')`，能不能用由这里决定。
# 手册的"适用产品"标注是裁剪依据，改一行前先回手册核对章节号。


@register
class UnicoreUM960Driver(UnicoreN4Base):
    """Unicore UM960（含 UM960A）。手册 §3.6 有流动站、§3.7 有 Heading2。"""

    MODEL_KEY = 'um960'
    DISPLAY_NAME = 'Unicore UM960'
    PRODUCT_MODEL = 'UM960'
    DOC_TITLE = 'Unicore N4 手册 § UM960'
    # §3.6 流动站 + §3.7 Heading2 都在适用产品里；§4.8 双天线引擎不在
    CMD = {**CMD, **ROVER_CMD, **HEADING2_CMD}
    CAPS = {**CAPS, 'heading2': True}


@register
class UnicoreUM960LDriver(UnicoreN4Base):
    """Unicore UM960L —— **本手册里最"窄"的一块**。

    只有它没有 `MODE ROVER`（§3.6 适用产品不含 UM960L），
    所以它的 CMD 表就是四型号公共集本身，一条都不加。
    页面上若给它配"切移动站"，会在 `set_mode()` 里被明确拒绝，
    而不是发出 `MODE ROVER` 然后静默无响应（简化 ASCII 不回错误码）。
    """

    MODEL_KEY = 'um960l'
    DISPLAY_NAME = 'Unicore UM960L'
    PRODUCT_MODEL = 'UM960L'
    DOC_TITLE = 'Unicore N4 手册 § UM960L'
    CMD = dict(CMD)              # 无流动站、无 Heading2
    CAPS = dict(CAPS)


@register
class UnicoreUM980Driver(UnicoreN4Base):
    """Unicore UM980（含 UM980I / UM980A）。唯一的"功能全集"型号之一。

    比 UM960 多两条只有它有的开关：§4.13 `CONFIG MMP`、§4.17 `CONFIG PSRVELDRPOS`。
    """

    MODEL_KEY = 'um980'
    DISPLAY_NAME = 'Unicore UM980'
    PRODUCT_MODEL = 'UM980'
    DOC_TITLE = 'Unicore N4 手册 § UM980'
    CMD = {**CMD, **ROVER_CMD, **HEADING2_CMD, **UM980_ONLY_CMD}
    CAPS = {**CAPS, 'heading2': True, 'mmp': True, 'psrveldrpos': True}


@register
class UnicoreUM982Driver(UnicoreN4Base):
    """Unicore UM982 —— 双天线定向主板，功能最多的型号。

    **`HIDDEN = True`：协议完整支持，但不在前端下拉里出现。**
    理由是 UM982 是双天线定向板（两路天线口做 Heading 定向），装成 RTK 基准站
    要把两天线接在一根桅杆上定死基线，还得多配 `CONFIG HEADING`，现场不实用；
    基准站请用 UM960 / UM980 这类单天线板。驱动保留是为了：
        - 历史记录里型号键是 `um982` 的老数据仍能回填、仍能跑标定；
        - 后期定向类需求（测姿）直接用它，不必重写一份驱动。
    若要放开，把本行改成 `HIDDEN = False` 即可，前端下拉自动多出这一项。

    比 UM960/UM980 多一整套 Heading 引擎（§4.8）：
        `CONFIG HEADING <FIXLENGTH|VARIABLELENGTH|STATIC|LOWDYNAMIC>`
        `CONFIG HEADING OFFSET <yaw> <pitch>`
        `CONFIG HEADING LENGTH <length> <mode>`
    外加只有 UM982 才输出的 `GPGGAH` / `GPTHS` / `GPHPR` 等 Heading 版语句。
    少的是 §4.17 `CONFIG PSRVELDRPOS`（那是 UM980 专有）。
    """

    MODEL_KEY = 'um982'
    DISPLAY_NAME = 'Unicore UM982'
    PRODUCT_MODEL = 'UM982'
    DOC_TITLE = 'Unicore N4 手册 § UM982'
    CMD = {**CMD, **ROVER_CMD, **HEADING2_CMD, **HEADING_CMD}
    CAPS = {**CAPS, 'heading2': True, 'heading_engine': True}
    HIDDEN = True               # 双天线定向板，不适合做 RTK 基准站 → 前端不推


# 本文件注册的型号键。`driver_manager._BUILTIN_MODULES` 只列**文件名**，
# 一个文件里注册几个型号由这里声明的类决定 —— 加新型号不用改 `_BUILTIN_MODULES`。
REGISTERED_KEYS = ('um960', 'um960l', 'um980', 'um982')

# `#VERSIONA` 产品名 → 驱动类。`detect()` 只用来校验"是不是 N4"，
# 真正的分派由 `driver_manager.match_driver()` 遍历注册表完成：
# 每个子类的 `detect()` 都会回读产品名并比对自己的 `PRODUCT_MODEL`，
# 对不上就返回失败（`model_mismatch`），所以这里不需要额外的分派表。
PRODUCT_TO_CLASS = {
    'UM960': UnicoreUM960Driver,
    'UM960A': UnicoreUM960Driver,
    'UM960L': UnicoreUM960LDriver,
    'UM980': UnicoreUM980Driver,
    'UM980I': UnicoreUM980Driver,
    'UM980A': UnicoreUM980Driver,
    'UM982': UnicoreUM982Driver,
}


# ==================== 模块级工具函数 ====================
# 放在类外面是因为 `driver_manager._selftest()` 与其它驱动也会直接 import 它们。


def ecef_to_geodetic(x, y, z):
    """ECEF → (纬, 经, 高)，单位度/米。

    直接转给 `calibration/geo.py`，**不在这里重写一份**：
    标定写进模块的坐标和监控页显示的坐标必须出自同一套算法，
    否则"写进去的和看到的不一样"时根本没法判断是哪边错了。
    """
    from ..geo import ecef_to_geodetic as _impl
    return _impl(x, y, z)


def geodetic_to_ecef(lat_deg, lon_deg, height=0.0):
    """(纬, 经, 高) → ECEF，单位度/米。同上，复用 `geo.py`。"""
    from ..geo import geodetic_to_ecef as _impl
    return _impl(lat_deg, lon_deg, height)


def _split_nmea(line):
    """去掉语句头、CRC 与 `;` 之后的段，返回字段列表。

    N4 的应答有两种尾巴：
        标准 NMEA 的 `*XX`（8 位异或）
        `#VERSIONA` 的 `*XXXXXXXX`（32 位 CRC）
    还有一种中间形态 `#MODE,...,160;MODE ROVER SURVEY,*1B` —— `;` 之前是
    二进制头，之后才是给人看的模式文本。切字段前必须先按 `;` 断开，
    否则 `fields` 里会混进 `160;MODE ROVER SURVEY` 这种拼接串。
    """
    text = (line or '').strip().lstrip('$#')
    if ';' in text:
        text = text.split(';', 1)[1]
    text = text.split('*')[0]
    return [item.strip().strip('"') for item in text.split(',')]


def _quality_from_pos_type(pos_type):
    """N4 的 `sol_status`（表 7-2x）→ GGA 口径的定位质量位。

    为什么需要这个映射：基站模式下 GGA 不输出，`BESTNAV` 的数值是另一套编码，
    而流程层统一按 GGA 的 `quality` 判断"能不能开工"。不翻译的话，
    上层会拿 N4 的 `sol_status=56` 去比 GGA 的 4，永远判"没固定"。
    """
    mapping = {0: 0,    # SOL_NONE
               16: 4,   # SOL_FIXEDPOS / 差分固定
               34: 4,   # SOL_FIXEDHEIGHT
               48: 4,   # SOL_DOPPLERFIXED
               49: 4,   # SOL_SINS
               50: 5,   # SOL_PSRFIXED（浮点）
               51: 5,   # SOL_PSRFLOAT
               52: 5,   # SOL_NARROWINT
               53: 5,   # SOL_WIDELANE
               54: 5,   # SOL_NARROWLANE
               55: 5,   # SOL_L1FLOAT
               56: 4,   # SOL_L1INT（整数解）
               57: 5}   # SOL_INS
    return mapping.get(_to_int(pos_type), 0)


# MSM 基础消息号（附录 2）：GPS 1074 / GLONASS 1084 / Galileo 1094 /
# QZSS 1114 / BDS 1124 / NavIC 1134。**注意是每 10 一个星座**，
# 不是每 1 —— 写成 `base + level` 会得到 1078 这种不存在的报文号。
_MSM_BASES = {
    'GPS': 1074,
    'GLONASS': 1084,
    'GALILEO': 1094,
    'QZSS': 1114,
    'BDS': 1124,
    'BEIDOU': 1124,
    'NAVIC': 1134,
    'IRNSS': 1134,
}

# 星座代号 → `RTCM3-10xX` 的 x（1074 的 7 / 1084 的 8 …）
_MSM_CONSTELLATION_DIGIT = {
    'GPS': 7, 'GLONASS': 8, 'GALILEO': 9, 'QZSS': 1, 'BDS': 2, 'NAVIC': 3,
}


def _rtcm_name_number(name):
    """`RTCM3-107X` / `RTCM3-1074` / `1074` → 消息号整数；非 RTCM 名返回 None。

    `X` 表示"MSM 第 4 档"，默认取 MSM4（`107X` → 1074）。
    返回 None 表示"这不是 RTCM 语句名"，由调用方转去按 NMEA 处理 ——
    **不要**返回 0，0 会被当成合法消息号拼出 `rtcm0000`。
    """
    text = (name or '').strip().upper()
    if not text:
        return None
    if text.startswith('RTCM3-'):
        payload = text[len('RTCM3-'):]
    elif text.startswith('RTCM'):
        payload = text[len('RTCM'):]
    elif text.isdigit():
        payload = text
    else:
        return None

    payload = payload.strip()
    if payload.endswith('X'):
        # `107X` = `10` + 星座数字 `7` + 档位 `X`，X 默认取 4（MSM4，最通用的一档）。
        # 拼法是 `1070 + 4 = 1074`；**不是** `107 * 10 = 1070` ——
        # 1070 不是任何 MSM 报文号，发下去模块会静默忽略（现场表现：配了没输出）。
        head = payload[:-1]
        if not head.isdigit() or len(head) != 3:
            return None
        base = int(head) * 10          # `107` → 1070
        # 1070 是"星座前缀"，报文号是 1074~1077（档位 4~7），所以要比对 1074 那一组
        if (base + 4) not in _MSM_BASES.values():
            return None
        return base + 4
    if payload.isdigit() and len(payload) == 4:
        return int(payload)
    return None


def _rtcm_message_number(name):
    """把语句名翻成 4 位消息号；不是 RTCM 就返回 None（`set_message_rate` 用）。"""
    number = _rtcm_name_number(name)
    if number is None:
        return None
    return number


def _rtcm_type_of(message_no):
    """消息号 → 类型标记：`'msm'` / `'base'`(100x) / `'ant'`(1033) / `'other'`。

    备份与还原时要按类型决定"这条消息该不该重播"：
    MSM 是观测值（必须每个历元），1005/1006 是静态描述（10 秒一次够），
    1033 是天线信息（30 秒一次就够）。
    """
    number = _to_int(message_no)
    if number is None:
        return 'other'
    if any(base <= number <= base + 7 for base in _MSM_BASES.values()):
        return 'msm'
    if 1001 <= number <= 1012:
        return 'base'
    if number == 1033:
        return 'ant'
    return 'other'


def _port_name(port):
    """端口名归一化：`2` / `com2` / `COM2` → `com2`；空值返回 ''。

    N4 命令里写**小写** `com2`（`rtcm1074 com2 1`），
    而应答里写**大写** `COM2`。两个方向都要能认，否则"配了 COM2，读回来是 com2"
    会被当成两个不同的端口。
    """
    text = str(port or '').strip().lower().lstrip('com')
    if not text:
        return ''
    if not text.isdigit():
        return ''
    number = int(text)
    if number < 1:
        return ''
    return f'com{number}'


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


def _fmt_num(value, min_dp=1, max_dp=4):
    """按"至少 min_dp 位、至多 max_dp 位"输出十进制串。

    为什么不直接 `f'{v:.1f}'`：那会把用户填的 0.05 变成 0.1、1.25 变成 1.2 ——
    保持距离和截止高度角都是米/度级判据，被悄悄抹掉精度等于改了用户要求。
    """
    number = _to_float(value)
    if number is None:
        return '0.' + '0' * min_dp
    for dp in range(int(min_dp), int(max_dp) + 1):
        text = f'{number:.{dp}f}'
        if abs(float(text) - number) < 1e-9:
            return text
    return f'{number:.{max_dp}f}'


def _fmt_deg(value, digits=9):
    """经纬度专用格式化：固定 9 位小数（约 0.1 mm），不用"最短表示"。

    为什么不用 `_fmt_num`：纬度的 `25.3` 和 `25.300000000` 在模块眼里是同一个点，
    但固定位数能让命令长度可预测，偏于排查"为什么这条命令没被认"。
    """
    number = _to_float(value)
    if number is None:
        number = 0.0
    return f'{number:.{int(digits)}f}'


def _now():
    """当前时间字符串（秒级）。备份文件与落库记录都用这个格式。"""
    import datetime
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def nmea_parse_shim(line):
    """调基类的标准 NMEA 解析器；解析不了返回 None。

    单独包一层是为了让 `parse_line` 里的兜底顺序看得清楚：
    先给标准 NMEA 判，认了就返回，认不出才走 N4 专有语句。
    """
    from .base import parse_line as _base_parse
    try:
        return _base_parse(line)
    except Exception:
        return None


if __name__ == '__main__':      # pragma: no cover - 手工自测
    print(__doc__)
    # 型号键 → 类：直接用注册表反查，不额外维护一张映射表（维护两张必然不同步）
    for _key in REGISTERED_KEYS:
        _cls = _REGISTRY.get(_key)
        print(f'{_key:10s} {(getattr(_cls, "DISPLAY_NAME", "(未注册)") if _cls else "(未注册)")}')
