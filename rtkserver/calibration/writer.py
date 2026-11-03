# -*- coding: utf-8 -*-
"""写入链路（方案 §8）：写坐标 → 切模式 → 配报文 → 保存 → 重启 → 三级校验 → 落库。

为什么单独成文件而不是塞进 service.py：
    `service.py` 管的是"串口怎么借、阶段怎么落盘、失败怎么收尾"，
    本文件管的是"**这 8 步按什么顺序做、每步拿什么判据算通过**"。
    两者混在一起以后，任何一次"回读阈值要不要改"都得翻 1800 行流程代码。

三条判据的出处（改数字前先回去看方案，不要凭感觉调）：
    COORD_TOLERANCE = 1 mm   §8.2：ECEF 参数是 4 位小数，理论上完全相等；
                             整米级偏差不是舍入，是"没写进去"
    RTCM_WARN_LIMIT = 5 cm   §8.6：1005 量化误差 ≤0.1 mm，5 cm 是"真超差"
    RTCM_FAIL_LIMIT = 50 cm  §8.6：超过就是"写入未生效或读到旧配置"

失败语义（本文件的核心契约）：
    - 任何一步失败都抛 `WriteFailed`，带着"停在哪一步 + 人话原因 + nvm_state"。
    - **保存（SAVEPAR）之前失败**：模块内存已改、NVM 还是旧值 → 必须回写内存态，
      否则模块停在"半个基准站"上，比原来的移动站状态糟得多（§8.4）。
    - **保存之后失败**：NVM 已经是新值，此时回写内存只会造成"内存与 NVM 不一致"，
      所以**不回写**，只把事实告诉用户（一键还原是用户显式动作）。
    - 中断（用户取消/心跳丢失）不在本文件兜：`service` 决定要不要回写，
      因为"租约已被看门狗回收"时再发命令就是跟数据面抢串口。

自测（离线，不需要硬件 —— 用一个会持久化/会掉电丢配置的假模块）：
    python -m rtkserver.calibration.writer
"""
import logging
import math
import time

from .geo import ecef_to_geodetic, geodetic_to_ecef, max_axis_delta
from .proto.rtcm import decode_frame

# ==================== 阶段（方案 §9.1 / §0.5.5 S12~S15） ====================
STEPS = ('WRITE_COORD', 'BASE_SETUP', 'MSG_SETUP', 'SAVE',
         'RESTART', 'VERIFY_MODE', 'VERIFY_RTCM', 'INJECT')

# ==================== 判据（方案 §8.2 / §8.6） ====================
COORD_TOLERANCE = 0.001        # 回读坐标最大轴偏差，米
RTCM_WARN_LIMIT = 0.05         # 1005 偏差告警门限，米
# 注意：这两个门限判的是「1005 播发值 vs 本次写入值」的一致性，
# 不是坐标精度。1005 播发的是模块内部存的那份坐标，与写入值同源，
# 所以正常情况恒为 0.1 mm 量级（1005 的量化误差），超差只说明写入没生效/没持久化。
RTCM_FAIL_LIMIT = 0.5          # 1005 偏差判失败门限，米
# 1005 不符时的重下发轮数上限（标定流程要求「不符则重下发 + 保存 + 重启复核」）。
# **只给 1 轮**：两次都错说明不是偶发（NVM 写保护 / 被外部改回 / 供电不足），
# 继续重试只会反复写 NVM 磨损闪存，也不会碰巧变对。
RTCM_RETRY_LIMIT = 1

# NVM 四态（`saved`/`reverted`/`unknown` 是方案 §8.4 的三态；
# `not_written` 是"一条写命令都没发出去"，此时既没保存也无从回写，必须与三者区分）
NVM_SAVED = 'saved'
NVM_REVERTED = 'reverted'
NVM_UNKNOWN = 'unknown'
NVM_NOT_WRITTEN = 'not_written'

# 提示文案（方案 §8.4 的表格，一字不改地对应三种状态）
TEXT_REVERTED = '已恢复为标定前的配置（内存态与 NVM 均已回写），可直接重试标定'
TEXT_UNKNOWN = ('配置保存失败且未能恢复。断电重启后模块会回到标定前的配置，请重新标定')
TEXT_SAVED_ONLY = '配置已写入模块，但后续校验未通过；如需退回去请用「还原标定前配置」'

DEFAULT_RTCM_TIMEOUT = 20.0    # 等 1005 的秒数（§8.6 给的 10 s 是下限，基站首帧可能更慢）
DEFAULT_RTCM_SAMPLES = 3       # 采到几条 1005 才算"稳定播发"

# Survey-in 三档预设（方案 P3，值与官方默认值**不同**，务必显式下发）：
#   快速 600 次 / 3.0 m、标准 1800 次 / 1.5 m、高精度 3600 次 / 0.8 m。
# 键名即 CLI/页面用的档位名。`count` 是"最少定位次数"，基站模式 1 Hz 时数值 ≈ 秒。
SURVEY_PROFILES = {
    'fast':     {'count': 600,  'acc': 3.0, 'label': '快速（临时建站）',   'grade': 'sub_meter'},
    'standard': {'count': 1800, 'acc': 1.5, 'label': '标准（常规野外）',   'grade': 'sub_meter'},
    'high':     {'count': 3600, 'acc': 0.8, 'label': '高精度（长期固定）', 'grade': 'sub_meter'},
}

# Survey-in 收敛轮询的最小间隔（秒）。SVINSTATUS 是 1 Hz 主动输出，
# 没必要比它更快地 poll；轮太勤只会空转耗 CPU。
SVIN_POLL_INTERVAL = 0.5
# 断点续标时最多等多久读到"已收敛"状态。模块正常 1 Hz 输出 SVINSTATUS，
# 3 秒足够读到两条；给到 6 秒是为了容忍刚重启/刚打开状态输出的空档。
SVIN_RESUME_WAIT = 6.0
# 连续多少次"读到状态但 Valid 一直是 1"才警告"可能已卡住"（不做硬终止，交给超时兜底）
SVIN_STALL_WARN_AT = 20
# 精度等级标注：Survey-in 是**亚米级**，与 CORS 的厘米级必须区分开（方案 §0.5.8 / §0.5.9）
ACCURACY_GRADE_SUB_METER = 'sub_meter'
ACCURACY_GRADE_CENTIMETER = 'centimeter'
# CORS 采集的三种真实精度，必须严格区分（三者混用会让用户以为拿到了厘米级坐标）：
#   FIXED   —— 质量位 4，模糊度固定，典型 cm 级
#   FLOAT   —— 质量位 5，模糊度浮动，典型 dm 级（高程尤差）
#   NOVELTY —— 采集完但 σ 未收敛到门限，只作参考
ACCURACY_GRADE_DECIMETER = 'decimeter'

# ==================== CORS 差分标定（方案 P1 / §6 / §7） ====================
# 采集时长三档（§7.5）：3 / 5 / 10 分钟可选，默认 5 分钟。
CORS_DURATIONS = {'short': 180, 'standard': 300, 'long': 600}
CORS_DEFAULT_DURATION = 300
# 固定解准入（§7.2）：连续 30 秒同时满足 质量=4、卫星数≥12、PDOP≤2.0。
FIX_QUALITY_REQUIRED = 4
FIX_MIN_SATS = 12
FIX_MAX_PDOP = 2.0
FIX_HOLD_SECONDS = 30.0

# ---- 浮动解降级准入（天线环境受限时的保底路径）----
# 为什么需要：天顶被建筑/窗框遮挡、只剩地平线附近卫星时（巷子、天井、室内靠窗），
# 模块能稳定进浮动解（质量=5）但**永远进不了固定解**——这是物理条件决定的，
# 再等多久都不会变。老逻辑在 FIX_WAIT 里死等到超时，最后一个点都采不到。
#
# 2026-11-02 真机实测（天线在窗户外巷道，45 秒采样）：
#   可见 37 颗 / 参与解算 25~27 颗，HDOP 0.62~0.91，质量位 458 条全 =5
#   CN0 均值 BDS 33.9 / GLO 36.1 / GAL 35.9 / GPS 31.1
#   低仰角(<15°) 11 颗占 30%，天顶(>60°)仅 3 颗且 CN0 仅 28.3
#   → 多径与对流层误差被低仰角星放大，模糊度无法固定，但浮动解高程极差仅 0.57m。
#
# 对基准站而言浮动解完全够用：基准站初始化只需"米级以内"的近似坐标，
# 模块随后用 RTCM1005/1006 持续平滑输出 ECEF。所以降级采集不是妥协，是正确选择。
#
# 但必须**如实标注精度等级**，让用户和入库记录都知道这份坐标是浮动的：
# 固定解通常 cm 级，浮动解 dm 级，两者不能混为一谈。
FLOAT_QUALITY_ALLOWED = 5
FLOAT_MIN_SATS = 12
FLOAT_MAX_PDOP = 3.0
FLOAT_HOLD_SECONDS = 20.0
# 浮动解必须比固定解采更久：dm 级噪声下 180 秒均值已能压到 cm 级，
# 但仍不足以冒充固定解，所以时长下限单列。
FLOAT_MIN_COLLECT_SECONDS = 180.0
# 等待固定解超时后，判定「可降级为浮动解」的最低命中轮数。
# 计数器是每轮 0.2s 累加，180 秒窗口约 900 轮；要求 ≥50% 命中说明浮动解是
# **稳定的**，而不是偶发跳一下。低于这个比例就是链路问题，必须失败。
FLOAT_MIGRATE_MIN_HITS_RATIO = 0.5
# 精度等级标签（写进 result，前端与数据库按这个区分）
ACCURACY_FIXED = 'fixed'
ACCURACY_FLOAT = 'float'
# 等待固定解的总超时（秒）。天线被遮挡时可能永远进不了固定解，
# 不能无限占用串口；超时判失败并把最后观测值带出去（§7.2 的兜底）。
FIX_WAIT_TIMEOUT = 180.0
# CORS 网络抖动宽容窗口（秒，补充附录G）。断开时长 ≤ 这个值视为抖动：
# **不清空** `fix_hold_started`，连续固定解继续累计；超过就当真实断连清零重来。
# 为什么需要：移动网络/VRS 服务端轮换连接会产生 1~2 秒空窗，老逻辑每次抖动都
# 从零等 30 秒，现场表现是"明明一直是固定解却永远进不了采集"。
CORS_NET_JITTER_TOLERANCE_SEC = 2.0
# GGA_WAIT：只给原生 GGA 的等待窗口（秒）。rover 模式下 RMC 常比 GGA 早到，
# 一见 RMC 就退化成软件生成会把厘米级直接降成米级；先给原生 GGA 这个窗口。
GGA_NATIVE_WAIT = 8.0
# GGA_WAIT 总等待（秒）：连 RMC/GLL 都没有时，等这么久再判「拿不到位置」。
GGA_TOTAL_WAIT = 30.0
# 等模块自身定位（GGA 质量位 > 0）的超时（秒）。冷启动首次上电通常 30~60 秒，
# 给到 90 秒覆盖"天线刚接上、天空被部分遮挡"的较慢场景。
# 为什么必须等：质量位 0 时 GGA 里没有有效经纬度，上报给 VRS 毫无意义；
# 2026-11-02 真机就是因为冷启动阶段（质量=1、卫星=9）径直进了 FIX_WAIT，
# 白白耗掉 180 秒固定解等待窗口最后超时。
POSITION_FIX_TIMEOUT = 90.0
# 定位间隔（毫秒）。**不要改成 1000**：本固件（LG290P03AANR01A03S）在 1 Hz
# 定位节拍下 GGA 输出调度会卡死到 87 秒一条（实测 0.011 Hz），而
# `PQTMCFGMSGRATE,R,1,GGA` 读回依然是 1，配置"看起来完全正常"。
# 10 Hz 定位 + GGA Rate=1（每 N 次定位输出一次 → 这里配 1 表示每次定位都出，
# 由 GGA_MIN_INTERVAL 在上行侧限速）实测稳定 1.00 Hz。
FIXRATE_SAFE_MS = 100
# GGA 实测频率校验窗口（秒）与合格线（Hz）。1 Hz 设计值，允许时钟抖动。
GGA_RATE_WINDOW = 10.0
GGA_MIN_HZ = 0.8
# 标定期间要关掉的 NMEA 语句。
#
# GSV 是带宽杀手：每个星座每个频点一条，五星座双频一次性就能吐出几十条；
# GLL/VTG/RMC/ZDA 对本流程毫无用途（我们只读 GGA 的质量位/星数/高程，
# 和 GSA 的 PDOP）。留着它们不会让标定更准，只会和 RTCM 解析抢输出调度。
#
# 2026-11-03 真机 A/B 实测（注入 CORS 差分 785 B/s，前后各 40 秒）：
#   全开 → 注入差分后 GGA **6 条 / 40 秒（0.15 Hz）**
#   关掉 → 注入差分后 GGA **41 条 / 40 秒（1.03 Hz）**
# 之前三次标定全部失败的 FIX_WAIT，实际是在一条"GGA 只剩 0.15 Hz"的
# 链路上等的：每帧差分刚攒上就被下一轮调度挤掉，模糊度永远建立不起来。
#
# ⚠️ GSA **不能**放进来（2026-11-02 真机踩的坑）：协议 §8.1 说 GSA 固定 1 Hz、
# 与 FixInterval 无关，实测也是如此——注入差分时它反而抢走了 GGA 的输出时隙。
# A/B 实测（同一份差分流）：
#   留 GGA+GSA → 0.52 Hz
#   只留 GGA   → **5.15 Hz**（10 倍）
# 所以 GSA 是必开的（PDOP 准入要用），但必须按差分态的实测频率单独复核，
# 见 _step_cors_nmea_setup 里 GSA 那段说明。
QUIET_NMEA = ('GSV', 'GLL', 'VTG', 'RMC', 'ZDA')
# 注入差分后 GGA 频率的复核周期（秒）。
# 为什么固化成常量：这个数决定了"多久能发现 GGA 掉速"。太短会被 1 Hz 的
# 整数效应干扰（10 秒 10 条看着像 1.0），太长又会让一次失败白等几十秒。
FIX_RATE_PROBE_SEC = 15.0
# 采集期间复核工作模式的间隔（秒）。base 模式下注入的 RTCM 不会被用于解算，
# 不查就会一直跑到超时，把"模式错了"误判成"信号差、进不了固定解"。
MODE_RECHECK_SECONDS = 60.0
# PDOP 只能从 GSA 拿；模块不输出 GSA 时降级为 HDOP ≤ 1.5（界面标注，§7.2）。
FIX_MAX_HDOP_FALLBACK = 1.5
# 收敛判定（§7.5）：连续 60 秒三轴滤波后 σ 均 ≤ 2 cm。
CONVERGE_SIGMA = 0.02
# 浮动解的收敛门限：**不能用 2cm**。浮动解原始 σ 典型 10~50 cm，拿 2cm 判
# 永远不会收敛，采集会被拖到时长上限（白白多跑几分钟）。
# 浮动解的"收敛"含义是「σ 稳定下来不再漂」，取 15 cm——与它的真实噪声量级匹配。
CONVERGE_SIGMA_FLOAT = 0.15
CONVERGE_HOLD_SECONDS = 60.0
# 3σ 野值剔除（§7.3）：滑动窗口 N=30，σ 下限钳到 5 mm。
OUTLIER_WINDOW = 30
OUTLIER_SIGMA_FLOOR = 0.005
# 卡尔曼（§7.4）：常值模型，三轴独立。
KALMAN_Q = 1e-6        # 过程噪声 m²/s
KALMAN_R = 0.0004      # 观测噪声 m²（σ=2cm）
# 采集轮询间隔（秒）。1 Hz 采样，0.2s 轮询足够跟上，还能及时响应终止。
CORS_POLL_INTERVAL = 0.2
# 链路静默判定（方案 §14 #1 / #28）：**已经收到过字节**之后，连续这么久
# 一个字节都没有，只能是拔线/断电/驱动崩了。必须立刻失败，不能等超时——
# 等下去的结果是拿着断线前的旧位置算完、把一组看着正常的坐标写进模块。
LINK_SILENCE_LIMIT = 3.0
# 每个轮询周期里「读模块串口」的时间窗（秒）。模块每 1 秒把 GGA+GSA 一起吐出来，
# 只要在窗口内有一次 read 就全拿到了；窗口开太大会挤掉 socket 的收数时间。
GGA_DRAIN_WINDOW = 0.08
# 采集到 10 个有效样本后才允许开始收敛判定（§7.4 初值用前 10 个样本均值）。
COLLECT_MIN_SAMPLES = 10
# 采集最大时长兜底（§7.6）：超过 30 分钟仍不收敛，让用户决定（采用/延长/终止）。
COLLECT_MAX_DURATION = 1800.0

class WriteFailed(Exception):
    """写链路失败。消息直接给用户看，必须能指出"停在哪一步、下一步该做什么"。"""

    def __init__(self, step, message, detail=None):
        super().__init__(message)
        self.step = str(step or '')
        self.message = str(message or '')
        self.detail = detail or {}
        self.nvm_state = ''


# ==================== 写入意图 ====================
class WritePlan:
    """一次写入的完整意图。**所有校验都在这里做，流程里不再重复判。**"""

    __slots__ = ('apc', 'arp', 'conversion', 'mode', 'svin_mode',
                 'survey_in', 'survey_profile', 'accuracy_grade',
                 'msm', 'antenna', 'rates', 'offsets', 'rtcm_fields',
                 'coord_msg', 'resume_svin', 'on_slow', 'method', 'remark',
                 'verify_rtcm', 'module', 'warnings', 'mode_ack', 'kind', 'cors')

    def __init__(self):
        self.apc = None                 # 用户/采集给出的坐标（ECEF）
        self.arp = None                 # 实际写入模块的坐标（ECEF）
        self.conversion = {}
        self.mode = 'base'              # 目标工作模式
        self.svin_mode = 'fixed'        # fixed / survey_in / disabled
        self.survey_in = None           # {'count':..,'acc':..,'distance':..}
        self.survey_profile = None      # fast / standard / high（自收敛档位）
        self.accuracy_grade = None      # sub_meter（Survey-in）/ centimeter（CORS），标注精度等级
        self.msm = 4                    # MSM 档位；None = 保持模块默认，不下发
        # 坐标报文：'1005'（默认）/ '1006'（带天线高度）/ 'keep'（保持模块默认，不下发）
        self.coord_msg = '1005'
        # Survey-in 断点续标：模块已经收敛过就直接取坐标，不重跑几十分钟。
        # 关掉它（resume_svin=False）表示"我要按这次的档位重新收敛"。
        self.resume_svin = True
        # 采集到时长仍未收敛（σ > 2cm）时怎么办（§7.6 / §14 #14）：
        #   'adopt' —— 采用当前均值继续写入，记录里如实标注未收敛（默认）；
        #   'fail'  —— 判失败，一个坐标都不写。现场信号不稳、又不想让
        #              一个"看着成功"的坐标进模块时选这个。
        self.on_slow = 'adopt'
        self.antenna = None             # {'desc':..,'setup_id':..,'sn':..}
        self.rates = {}                 # 要下发的速率；写入时留空（由流程决定），还原时来自备份
        self.offsets = {}               # MSM 的 <Offset>（只有还原用得上）
        self.rtcm_fields = None         # 备份里读到的 RTCM 七字段（还原专用，原样回放）
        self.method = 'manual'
        self.remark = ''
        self.verify_rtcm = True
        self.module = {}
        self.warnings = []
        self.mode_ack = ''               # ack / deferred（§8.3 的降级判定）
        self.kind = 'write'              # write / restore（只影响文案与统计，不影响链路）
        self.cors = None                 # CORS 差分标定配置（§6）：{'host','port','user','password','mountpoint','duration'}

    # ---------- 构造 ----------
    @classmethod
    def build(cls, ecef=None, lat=None, lon=None, height=0.0,
              method='manual', mode='base', svin_mode='fixed', survey_in=None,
              msm=4, antenna=None, remark='', verify_rtcm=True, coord_msg='1005',
              arp=None):
        """从用户输入构造意图。ECEF 与经纬高二选一。

        用户给的就是**要写进模块的那组坐标**，原样写入，不做任何相位中心换算：
        本产品的场景是"天线固定 → 测量出该点 → 钉死"，现场没有 RTK 对中杆，
        多一层 APC→ARP 偏移只会因为方向填错把整站静默抬降一个天线高。

        `arp=` 是给「复用历史坐标」用的旁路：库里存的已经是当年写进去的值，
        直接复用即可。
        """
        plan = cls()
        plan.method = str(method or 'manual')
        plan.mode = str(mode or 'base').lower()
        plan.svin_mode = str(svin_mode or 'fixed').lower()
        plan.msm = _check_msm(msm)
        plan.coord_msg = _check_coord_msg(coord_msg)
        plan.antenna = dict(antenna) if antenna else None
        plan.remark = str(remark or '')
        plan.verify_rtcm = bool(verify_rtcm)

        if plan.mode not in ('base', 'rover'):
            raise ValueError(f'目标模式只能是 base / rover，收到 {plan.mode!r}')
        if plan.svin_mode not in ('fixed', 'survey_in', 'disabled'):
            raise ValueError(f'坐标模式只能是 fixed / survey_in / disabled，收到 {plan.svin_mode!r}')
        if plan.svin_mode == 'fixed':
            if arp is not None:
                # 历史坐标直通：这组数就是当年写进模块的值
                plan.apc = None
                plan.arp = _check_ecef(arp)
                plan.conversion = {
                    'apc': None,
                    'arp': dict(zip(('x', 'y', 'z'), plan.arp)),
                    'formula': '直接复用历史坐标',
                }
                return plan
            ecef = ecef if ecef is not None else _ecef_from_geodetic(lat, lon, height)
            if ecef is None:
                raise ValueError('固定坐标模式必须给 ECEF 或经纬高')
            plan.apc = _check_ecef(ecef)
            plan.arp = plan.apc
            plan.conversion = {
                'apc': dict(zip(('x', 'y', 'z'), plan.apc)),
                'arp': dict(zip(('x', 'y', 'z'), plan.arp)),
                'formula': '给定坐标即写入值',
            }
        elif plan.svin_mode == 'survey_in':
            if not survey_in:
                raise ValueError('Survey-in 模式必须给次数与精度门限')
            plan.survey_in = {'count': int(survey_in.get('count') or 0),
                              'acc': float(survey_in.get('acc') or 0.0),
                              'distance': survey_in.get('distance')}
        else:
            plan.svin_mode = 'disabled'
        return plan

    @classmethod
    def build_survey_in(cls, profile='standard', mode='base', msm=4, antenna=None,
                        remark='', distance=None, verify_rtcm=True, coord_msg='1005',
                        resume_svin=True):
        """构造"模块自收敛"意图（方案 P3）。

        与 `build()` 的区别：这里**没有用户给的坐标**，坐标是模块自己收敛出来的。
        `svin_mode='survey_in'` 只是标记，真正的收敛均值在 `SVIN_WAIT` 结束后才填进
        `apc/arp`，然后走 `WRITE_COORD` 把它写成固定坐标（后续校验链路完全复用）。
        精度等级固定标 `sub_meter`（亚米级），与 CORS 厘米级区分（§0.5.8）。
        """
        key = str(profile or 'standard').strip().lower()
        preset = SURVEY_PROFILES.get(key)
        if preset is None:
            raise ValueError(
                f'Surey-in 档位只能是 {"/".join(sorted(SURVEY_PROFILES))}，收到 {profile!r}')
        plan = cls()
        plan.method = 'survey_in'
        plan.mode = str(mode or 'base').lower()
        if plan.mode not in ('base', 'rover'):
            raise ValueError(f'目标模式只能是 base / rover，收到 {plan.mode!r}')
        plan.svin_mode = 'survey_in'
        plan.survey_profile = key
        plan.accuracy_grade = preset['grade']
        plan.survey_in = {'count': preset['count'], 'acc': preset['acc'],
                          'distance': distance}
        plan.msm = _check_msm(msm)
        plan.coord_msg = _check_coord_msg(coord_msg)
        plan.resume_svin = bool(resume_svin)
        plan.antenna = dict(antenna) if antenna else None
        plan.remark = str(remark or '')
        plan.verify_rtcm = bool(verify_rtcm)
        return plan

    @classmethod
    def build_cors(cls, host, port, mountpoint, user='', password='',
                   duration=CORS_DEFAULT_DURATION, msm=4, antenna=None, remark='',
                   verify_rtcm=True, coord_msg='1005', method='cors',
                   on_slow='adopt', reuse_arp=None):
        """构造「CORS 差分标定」意图（方案 P1）。

        与 `build_survey_in` 同理：这里**没有用户给的坐标**，坐标是 CORS 固定解
        采集 + 滤波收敛出来的。`svin_mode='fixed'` 是"最终写固定坐标"的标记，
        真实坐标在 `COLLECT` 结束后才填进 `apc/arp`，然后走与历史坐标完全相同的
        写入链路（写坐标→切基站→配报文→保存→重启→校验→落库）。
        精度等级固定标 `centimeter`（厘米级），与 Survey-in 的亚米级区分（§0.5.8）。

        `method='external_rover'` 复用同一条采集链路，但**只测不写**：
        模块可能是外接的流动站、也可能是没有命令集的未知模块，测出坐标就结束，
        不配报文、不保存、不重启。若模块当前是基准站模式，会做一次**仅内存**的
        临时切换（不下发 SAVEPAR、不写 NVM），结束后恢复原内存模式（附录D）。
        此时 `svin_mode` 标 `disabled`（明确表示"本次没有任何坐标写进行为"）。

        `reuse_arp=` 是标定流程里 **A1 的分支**：「库内已有该天线的历史坐标，
        且天线没动过 → 直接复用这组坐标，跳过整段 CORS 测量，只做设基准站」。
        给了它就完全跳过 CORS 连接与采集（不连网、不占 CORS 账号），
        直接走 WRITE_COORD → … → VERIFY_RTCM。用于"天线还是那根天线，
        只是换了模块/换了固件/重刷过参数，需要把坐标重新钉进去"。
        此时 `cors` 配置仍然保留（落库与页面展示需要），但网络不会被碰。
        """
        host = str(host or '').strip()
        mountpoint = str(mountpoint or '').strip()
        reuse = reuse_arp is not None
        # 复用坐标时不连 CORS，主机/挂载点允许为空——硬要求它们反而会逼用户
        # 为了一次纯写入去填一份自己根本不用的 CORS 账号。
        if not reuse:
            if not host:
                raise ValueError('CORS 服务器地址不能为空')
            if not mountpoint:
                raise ValueError('CORS 挂载点不能为空')
        port = int(port or 0) if reuse else int(port)
        if not reuse:
            if not (1 <= port <= 65535):
                raise ValueError(f'CORS 端口非法：{port}')
        else:
            port = port if 1 <= port <= 65535 else 0
        duration = int(duration)
        if duration <= 0:
            raise ValueError(f'采集时长必须为正整数秒，收到 {duration}')

        method = str(method or 'cors').strip().lower()
        if method not in ('cors', 'external_rover'):
            raise ValueError(f'采集方法只能是 cors / external_rover，收到 {method!r}')
        if reuse and method == 'external_rover':
            raise ValueError('「复用历史坐标跳过测量」与「只测不写」互相矛盾：'
                             '前者要写入 NVM，后者一个字节都不许写')

        plan = cls()
        plan.method = method
        if method == 'cors':
            plan.mode = 'base'           # 采集完最终切回基准站
            plan.svin_mode = 'fixed'     # 采集完写固定坐标
        else:
            plan.mode = 'rover'          # 只测不写：模块停在哪就是哪
            plan.svin_mode = 'disabled'  # 本次不写任何坐标
        plan.accuracy_grade = ACCURACY_GRADE_CENTIMETER
        plan.cors = {
            'host': host,
            'port': port,
            'mountpoint': mountpoint,
            'user': str(user or ''),
            'password': str(password or ''),
            'duration': duration,
            'reuse': bool(reuse),
        }
        if reuse:
            # 复用分支：坐标来自库里，精度等级沿用历史记录里的口径
            apc = _check_ecef(reuse_arp)
            plan.apc = apc
            plan.arp = apc
            plan.conversion = {
                'apc': dict(zip(('x', 'y', 'z'), apc)),
                'arp': dict(zip(('x', 'y', 'z'), apc)),
                'formula': '复用库内历史坐标（天线未移动，跳过 CORS 测量）',
            }
            plan.warnings.append(
                '本次跳过了 CORS 测量，直接复用库里的历史坐标。'
                '这**前提是天线绝对没有被移动过**（搬动、转动、换底座、修过支架都算）——'
                '位置一旦变过，写进去的就是错的坐标，而且 1005 校验查不出来')
        plan.msm = _check_msm(msm)
        plan.coord_msg = _check_coord_msg(coord_msg)
        plan.antenna = dict(antenna) if antenna else None
        plan.remark = str(remark or '')
        plan.verify_rtcm = bool(verify_rtcm)
        plan.on_slow = _check_on_slow(on_slow)
        return plan

    @classmethod
    def from_backup(cls, snapshot):
        """由 `backup.capture()` 的结果构造"还原"意图。

        还原也走同一条写链路：正因为链路一样，"还原"才不会是第二套没人测的代码。
        差别只有四处：目标模式来自备份、坐标取备份里的旧值、
        移动站模式下不做 1005 校验、报文配置按备份**原样回放**（不按"标定该配成什么"重配）。
        """
        mode = ((snapshot or {}).get('mode') or {}).get('text') or 'rover'
        svin = (snapshot or {}).get('svin') or {}
        svin_mode = svin.get('mode') or 'disabled'
        plan = cls()
        plan.kind = 'restore'
        plan.mode = mode
        plan.svin_mode = svin_mode if svin_mode in ('fixed', 'survey_in', 'disabled') else 'disabled'
        plan.method = 'manual'
        plan.remark = '还原标定前配置'
        plan.module = dict((snapshot or {}).get('module') or {})
        plan.verify_rtcm = (mode == 'base' and plan.svin_mode == 'fixed')
        if plan.svin_mode == 'fixed':
            ecef = (svin.get('ecef_x'), svin.get('ecef_y'), svin.get('ecef_z'))
            if None in ecef or max(abs(v) for v in ecef) <= 1.0:
                raise ValueError('备份里的固定坐标是空值/零值，无法还原')
            plan.apc = _check_ecef(ecef)
            plan.arp = plan.apc                  # 备份存的就是当年写进去的值，原样回放
            plan.conversion = {'apc': dict(zip(('x', 'y', 'z'), plan.apc)),
                               'arp': dict(zip(('x', 'y', 'z'), plan.arp)),
                               'formula': '还原：备份值即原写入值'}
        elif plan.svin_mode == 'survey_in':
            plan.survey_in = {'count': int(svin.get('count') or 0),
                              'acc': float(svin.get('acc_limit') or 0.0),
                              'distance': svin.get('distance')}

        # ---- 报文配置：一律"原样放回"，不按标定目标重配 ----
        rtcm = (snapshot or {}).get('rtcm') or {}
        plan.msm = rtcm.get('msm_type')
        fields = [str(item).strip() for item in (rtcm.get('fields') or [])]
        # 只有 7 个字段才敢原样回放；字段不全就退化成"只改 MSM 档位"
        plan.rtcm_fields = fields if len(fields) == 7 else None
        plan.rates = {name: rate for name, rate in ((snapshot or {}).get('rates') or {}).items()
                      if rate is not None}
        plan.offsets = {name: off for name, off in ((snapshot or {}).get('offsets') or {}).items()
                        if off is not None}
        antenna = (snapshot or {}).get('antenna') or {}
        if antenna.get('desc'):
            plan.antenna = {'desc': antenna.get('desc'),
                            'setup_id': antenna.get('setup_id') or 0,
                            'sn': antenna.get('sn') or ''}
        return plan

    # ---------- 对外回显 ----------
    def written(self):
        """实际写进模块的那三个数（ARP）。没有坐标时返回 None。"""
        if self.arp is None:
            return None
        return {'x': round(self.arp[0], 4), 'y': round(self.arp[1], 4),
                'z': round(self.arp[2], 4)}

    def summary(self):
        """给页面/日志看的"我要做什么"，含换算过程（用户要能核对）。"""
        info = {
            'kind': self.kind,
            'mode': self.mode,
            'svin_mode': self.svin_mode,
            'survey_profile': self.survey_profile,
            'accuracy_grade': self.accuracy_grade,
            'msm': self.msm,
            'rtcm_fields': list(self.rtcm_fields) if self.rtcm_fields else None,
            'rates': dict(self.rates or {}),
            'method': self.method,
            'on_slow': self.on_slow,
            'remark': self.remark,
            'verify_rtcm': self.verify_rtcm,
            'cors': (None if self.cors is None else
                     {'host': self.cors.get('host'), 'port': self.cors.get('port'),
                      'mountpoint': self.cors.get('mountpoint'),
                      'duration': self.cors.get('duration')}),
            'apc': None,
            'arp': self.written(),
            'geodetic': None,
            'survey_in': self.survey_in,
            'antenna': self.antenna,
            'warnings': list(self.warnings),
        }
        if self.apc is not None:
            info['apc'] = {'x': round(self.apc[0], 4), 'y': round(self.apc[1], 4),
                           'z': round(self.apc[2], 4)}
        # 经纬度按写入值（ARP）反算，与 `build_record` 同一口径：
        # 复用历史坐标时 plan.apc 是 None，按 apc 算会让页面上的经纬度变成空的。
        anchor = self.arp if self.arp is not None else self.apc
        if anchor is not None:
            try:
                lat, lon, height = ecef_to_geodetic(*anchor)
                info['geodetic'] = {'lat': round(lat, 9), 'lon': round(lon, 9),
                                    'height': round(height, 4)}
            except Exception:                    # noqa: BLE001 - 回显失败不该拦住写入
                pass
        return info


def _ecef_from_geodetic(lat, lon, height):
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        raise ValueError(f'经纬度必须是数字，收到 {lat!r} / {lon!r}')
    if not (-90.0 <= lat <= 90.0):
        raise ValueError(f'纬度必须在 -90~90 之间，收到 {lat}')
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f'经度必须在 -180~180 之间，收到 {lon}')
    return geodetic_to_ecef(lat, lon, float(height or 0.0))


def _check_msm(value):
    """MSM 档位：4/5/6/7 具体档位；None/'keep'/0 表示"沿用模块当前配置，不下发"。

    必须区分"没选"和"选了 0"：模块没有 MSM0，0 只能当"别动"。
    """
    if value is None or value == '' or value == 0:
        return None
    if isinstance(value, str) and value.strip().lower() in ('keep', 'default', 'auto'):
        return None
    try:
        msm = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'MSM 档位必须是 4/5/6/7，或 keep 表示沿用模块默认，收到 {value!r}')
    if msm not in (4, 5, 6, 7):
        raise ValueError(f'MSM 档位只能是 4/5/6/7，收到 {msm}')
    return msm


def _check_coord_msg(value):
    """坐标报文：1005（默认）/ 1006（带天线高度）/ keep（沿用模块默认，不下发）。"""
    text = str(value if value is not None else '1005').strip().lower()
    if text in ('', 'default', 'auto'):
        text = '1005'
    if text not in ('1005', '1006', 'keep'):
        raise ValueError(f'坐标报文只能是 1005 / 1006 / keep，收到 {value!r}')
    return text


def _check_on_slow(value):
    """未收敛策略：adopt（采用当前值，默认）/ fail（不写坐标直接失败）。"""
    text = str(value if value is not None else 'adopt').strip().lower()
    if text in ('', 'default', 'adopt', 'use', 'keep'):
        return 'adopt'
    if text in ('fail', 'abort', 'stop'):
        return 'fail'
    raise ValueError(f'未收敛策略只能是 adopt / fail，收到 {value!r}')


def _check_ecef(values):
    try:
        x, y, z = (float(values[0]), float(values[1]), float(values[2]))
    except (TypeError, ValueError, IndexError):
        raise ValueError(f'ECEF 必须是三个数字，收到 {values!r}')
    for axis, value in zip('xyz', (x, y, z)):
        if not (-7.0e6 <= value <= 7.0e6):
            raise ValueError(f'ECEF 的 {axis} = {value} 超出地球范围，'
                             f'疑似把经纬度当 ECEF 传进来了')
    # 只看单轴范围拦不住 (30.5, 114.3, 50) 这种"经纬度当 ECEF"的输入 ——
    # 它的三个分量都在合法区间内，但到地心的距离只有 118 m，在地核里。
    # 基准站必然贴着地表：6.0e6~6.6e6 m（含 ±200 km 高程与极区扁率）。
    radius = (x * x + y * y + z * z) ** 0.5
    if not (6.0e6 <= radius <= 6.6e6):
        raise ValueError(
            f'ECEF 到地心距离 {radius:.3f} m 不在 6000~6600 km 之间：'
            f'这三个数看起来不是 ECEF（是不是把经纬度填进来了？）')
    return (x, y, z)


# ==================== 写链路本体 ====================
class CoordinateWriter:
    """把 `WritePlan` 落到模块上，并逐级校验。

    与流程层的接口只有三个回调：
        on_stage(stage, message, progress)  阶段推进（service 据此落 state.json）
        checkpoint()                        中断检查（service 抛 TaskAborted/TaskEvicted）
        on_record(record_dict)              记录落库（service 调 store，返回主键）
    """

    def __init__(self, driver, channel=None, on_stage=None, checkpoint=None,
                 on_record=None, rtcm_timeout=DEFAULT_RTCM_TIMEOUT,
                 rtcm_samples=DEFAULT_RTCM_SAMPLES, task_id='', svin_timeout=None,
                 fix_wait_timeout=None):
        self.driver = driver
        self.channel = channel if channel is not None else getattr(driver, 'channel', None)
        self._on_stage = on_stage or (lambda *a, **k: None)
        self._checkpoint = checkpoint or (lambda: None)
        self._on_record = on_record
        self.rtcm_timeout = float(rtcm_timeout)
        self.rtcm_samples = max(1, int(rtcm_samples))
        self.task_id = str(task_id or '')
        self.svin_timeout = None if svin_timeout is None else float(svin_timeout)
        self.fix_wait_timeout = (None if fix_wait_timeout is None
                                 else float(fix_wait_timeout))

        self._plan = None
        self._backup = None
        self._saved = False          # SAVEPAR 是否已成功（决定失败时能否回写）
        self._touched = False        # 是否已经发过写命令（决定"要不要回写"）
        # 只测不写模式下，模块被临时切走前的**内存模式**（'base' / 'rover' / None）。
        # 非 None 表示"我们动过内存态，收尾必须切回去"（附录D）。
        self._mem_restore_mode = None
        self._step = ''
        self.result = {}

    # ---------- 主入口 ----------
    def run(self, plan, backup=None):
        """跑完整链路。成功返回结果字典；失败抛 `WriteFailed`（已做完收尾）。"""
        self._plan = plan
        self._backup = backup
        self._saved = False
        self._touched = False
        self._mem_restore_mode = None
        # 只测不写（external_rover）：全程不发写命令，失败时也不能"回写"
        # —— 没有备份可回，回写反而会朝一块不归我们管的设备发命令。
        self._readonly = plan.method == 'external_rover'
        self.result = {
            'task_id': self.task_id,
            'task_kind': plan.kind,
            'plan': plan.summary(),
            'steps': [],
            'nvm_state': NVM_NOT_WRITTEN,
            'mode_ack': '',
            'verify': {},
            'verdict': 'unknown',
            'broadcast': None,
            'delta': None,
            'record_id': None,
            'warnings': list(plan.warnings),
            'backup': _backup_summary(backup),
        }

        try:
            # Survey-in 自收敛（方案 P3）：先切基站模式（$PQTMSVINSTATUS 只在基站模式
            # 输出），再配收敛参数 + 等收敛，最后把收敛均值写成固定坐标；后续校验链路
            # （配报文→保存→重启→校验→落库）完全复用。
            # 注意顺序：SVIN_CFG/SVIN_WAIT 必须在 BASE_SETUP 之后 —— 真机实测 rover 模式下
            # `PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1` 直接回 ERROR,1（参数不合法）。
            if plan.method == 'survey_in':
                # 断点续标优先：模块已经收敛过就直接拿均值写固定坐标，
                # 不再重跑 1800~3600 次（1 Hz 下 30~60 分钟）。
                if not self._step_svin_resume(plan):
                    self._step_switch_mode(plan)
                    self._step_svin_cfg(plan)
                    self._step_svin_wait(plan)
                self._step_write_coord(plan)
            elif plan.method == 'cors':
                # CORS 差分标定（方案 P1）：切流动站 → 配 NMEA → 等 GGA → 连 CORS
                # → 等固定解 → 采集滤波 → 断 CORS。得到 ECEF 后走与历史坐标相同的
                # 写入链路（写坐标→切基站→配报文→保存→重启→校验→落库）。
                #
                # A1 分支（`plan.cors['reuse']`）：库里已有该天线的坐标且没搬过 →
                # **整段跳过测量**，不连 CORS、不占账号、不进流动站，直接设基准站。
                # 判断"要不要复用"是用户的事（页面上的历史坐标列表），这里只执行。
                if (plan.cors or {}).get('reuse') and plan.arp is not None:
                    self._stage('COLLECT',
                                '复用库内历史坐标，跳过 CORS 测量（天线未移动）')
                    self.result['cors'] = {
                        'reused': True,
                        'ecef': list(plan.arp),
                        'samples': 0,
                        'accuracy_grade': plan.accuracy_grade,
                        'note': '复用历史坐标，本次未连接 CORS、未采集',
                    }
                    self._record_step('COLLECT', True,
                                      '复用历史坐标（跳过 CORS 测量与采集）',
                                      self.result['cors'])
                else:
                    self._step_cors_collect(plan)
                self._step_write_coord(plan)
                self._step_switch_mode(plan)
            elif plan.method == 'external_rover':
                # 外接流动站 / 未知模块：只测不写（方案里的 external_rover）。
                # 走完同一条采集链路就落库结束，**不配报文、不写坐标、不保存 NVM**。
                # 唯一允许碰的是"仅内存临时切模式"（附录D）——而且必须切回去，
                # 所以放在 finally 里：成功、失败、用户中止三条路径都要恢复。
                try:
                    self._step_cors_collect(plan)
                finally:
                    self._restore_memory_mode()
                self.result['verdict'] = 'skipped'   # 没有写入，也就没有 1005 校验
                self._step_inject(plan)
                self.result['readonly'] = True
                return self.result
            else:
                if plan.svin_mode == 'fixed':
                    self._step_write_coord(plan)
                else:
                    self._step_config_svin(plan)
                self._step_switch_mode(plan)
            self._step_setup_messages(plan)
            self._step_save(plan)
            self._step_restart(plan)
            self._step_verify_mode(plan)
            self._step_verify_rtcm(plan)
            self._step_inject(plan)
        except WriteFailed as e:
            self._finish_failure(e)
            raise
        except BaseException as e:
            # 中断（用户取消 / 心跳丢失 / 驱动异常）：能回写就回写，再把异常原样抛出。
            # "租约已被回收"时**不能**回写：串口已经还给数据面了，再发命令就是抢字节。
            self._abort_cleanup(e)
            raise

        self.result['verdict'] = self.result.get('verdict') or 'match'
        return self.result

    # ---------- 失败收尾（方案 §8.4） ----------
    def _finish_failure(self, error):
        if getattr(self, '_readonly', False):
            # 只测不写：没有写配置、没有保存 NVM。可能动过内存模式（附录D），
            # 但那条已经被 _restore_memory_mode 还原了，这里如实说"没写进 NVM"，
            # 不能说"什么都没动"——用户会去核对模块配置，发现模式变过就说不清了。
            error.nvm_state = NVM_NOT_WRITTEN
            self.result['nvm_state'] = error.nvm_state
            self.result['error_step'] = error.step
            self.result['error'] = error.message
            error.message = f'{error.message}（本模式未向模块写入任何配置，NVM 未改动）'
            logging.error(f'只测不写失败于 {error.step}：{error.message}')
            self._record_step(error.step or 'FAILED', False, error.message,
                              {'nvm_state': error.nvm_state, 'readonly': True})
            return
        if self._saved:
            error.nvm_state = NVM_SAVED
            error.message = f'{error.message}；{TEXT_SAVED_ONLY}'
        elif not self._touched:
            error.nvm_state = NVM_NOT_WRITTEN
        else:
            state, note = self._revert()
            error.nvm_state = state
            error.message = f'{error.message}；{note}'
        self.result['nvm_state'] = error.nvm_state
        self.result['error_step'] = error.step
        self.result['error'] = error.message
        logging.error(f'写链路失败于 {error.step}（nvm={error.nvm_state}）：{error.message}')
        self._record_step(error.step or 'FAILED', False, error.message,
                          {'nvm_state': error.nvm_state})

    def _abort_cleanup(self, error):
        if getattr(error, 'evicted', False):
            self.result['nvm_state'] = ''
            self.result['error'] = f'租约已被回收，未做回写：{error}'
            return
        if getattr(self, '_readonly', False):
            self.result['nvm_state'] = NVM_NOT_WRITTEN
            self.result['error'] = (f'任务被中断（{type(error).__name__}）；'
                                    f'本模式未向模块写入任何配置，NVM 未改动')
            return
        if self._saved or not self._touched:
            self.result['nvm_state'] = NVM_SAVED if self._saved else NVM_NOT_WRITTEN
            return
        try:
            state, note = self._revert()
            self.result['nvm_state'] = state
            self.result['error'] = f'任务被中断（{type(error).__name__}）；{note}'
        except Exception as exc:                 # noqa: BLE001 - 收尾失败不能再掩盖原异常
            self.result['nvm_state'] = NVM_UNKNOWN
            self.result['error'] = f'任务被中断，回写也失败：{exc}'

    def _revert(self):
        """用备份的旧值回写**内存态**并回读确认（方案 §8.4 第 1~2 步）。

        只回写内存、不 SAVEPAR：NVM 里本来还是旧值，再存一次既没必要，
        还会把"这次保存失败"的事实掩盖掉（记录里就说不清到底存没存进去）。
        """
        snapshot = self._backup
        if not snapshot:
            return NVM_UNKNOWN, '没有备份可用于回写，模块当前处于临时状态（断电重启会回到旧配置）'

        notes = []
        svin = snapshot.get('svin') or {}
        svin_mode = svin.get('mode') or ''
        old_mode = ((snapshot.get('mode') or {}).get('text')) or ''
        try:
            # 顺序与写入一致：先把坐标改回去，再切模式（§8.1 的风险窗口最小化）
            if svin_mode == 'fixed':
                ok, message, _data = self.driver.set_fixed_ecef(
                    svin.get('ecef_x'), svin.get('ecef_y'), svin.get('ecef_z'),
                    persist=False, restart=False, verify=False)
                notes.append(f'坐标回写{"成功" if ok else "失败：" + message}')
            elif svin_mode == 'survey_in':
                ok, message, _data = self.driver.config_survey_in(
                    svin.get('count') or 0, svin.get('acc_limit') or 0.0,
                    svin.get('distance'), persist=False, restart=False, verify=False)
                notes.append(f'Survey-in 回写{"成功" if ok else "失败：" + message}')
            elif svin_mode == 'disabled':
                # 原来就"没有坐标配置"：必须显式停用，否则模块内存里还留着
                # 我们刚写进去的坐标（回写不彻底 = 留下一个看不见的不一致）
                ok, message, _data = self.driver.disable_survey_in(persist=False, restart=False)
                notes.append(f'停用坐标配置{"成功" if ok else "失败：" + message}')
            else:
                notes.append('备份里没有坐标配置（当时读取失败），未回写坐标')

            if old_mode in ('base', 'rover'):
                # **必须 persist=True**：切过去时 ROVER_SETUP 用的是 persist=True，
                # NVM 里已经是 rover 了。这里只写运行态的话，回滚后内存是 base、
                # NVM 是 rover —— 一断电重启，基准站就变回流动站。
                # （2026-11-02 真机发现：失败后回读内存是 base，但 NVM 未还原）
                ok, message, _data = self.driver.set_mode(
                    old_mode, persist=True, restart=False, verify=False)
                notes.append(f'模式回写{"成功" if ok else "失败：" + message}')
            else:
                notes.append('原模式不可识别，跳过模式回写')

            # 报文速率也必须回放：CORS 链路会把 GGA/GSA 从 0 打开到 1，
            # 只还坐标和模式的话，回滚后 base 模式下会白白多播一路 NMEA，
            # 占串口带宽、干扰主程序数据面。踩过一次（2026-11-02 真机）。
            restored = 0
            for name, raw in (snapshot.get('rates') or {}).items():
                rate = _to_int(raw)
                if rate is None or _is_msm_rate_name(name):
                    continue              # 备份里没读到 / MSM 不能置 0，一律不动
                okr, _mr, _dr = self.driver.set_message_rate(name, rate)
                if okr:
                    restored += 1
            if restored:
                notes.append(f'报文速率回放 {restored} 条')
            elif snapshot.get('rates'):
                notes.append('报文速率回放失败（回读不一致，请手动核对 NMEA 输出）')

            # 回读校验：`set_message_rate` 返回 OK 不等于值真的生效。
            # 2026-11-02 真机踩过：回放 6 条全报成功，但 RTCM3-1005 实际是 0，
            # 基准站不播发坐标了（下游移动站直接失效）。1005 是基准站的命脉，
            # 必须回读确认；GGA 是 CORS 上报的依据，一并查。
            mismatch = []
            for name in ('RTCM3-1005', 'GGA'):
                want = _to_int((snapshot.get('rates') or {}).get(name))
                if want is None:
                    continue
                okv, _mv, dv = self.driver.get_message_rate(name)
                got = None
                if okv:
                    fields = (dv or {}).get('fields') or []
                    got = _to_int(fields[-1]) if fields else None
                if got != want:
                    mismatch.append(f'{name}={got}(应为 {want})')
            if mismatch:
                notes.append('速率回读不一致：' + '、'.join(mismatch)
                             + '；请用「还原标定前配置」复核')
        except Exception as e:                   # noqa: BLE001 - 回写失败只能降级
            return NVM_UNKNOWN, f'回写过程异常（{type(e).__name__}: {e}），' + TEXT_UNKNOWN

        # 回读确认（§8.4 第 2 步）：内存态真的回去了才算 reverted
        try:
            ok, _message, readback = self.driver.get_svin_config()
            mode_ok, _message2, mode_data = self.driver.get_mode()
        except Exception as e:                   # noqa: BLE001
            return NVM_UNKNOWN, f'回写后回读异常（{e}），' + TEXT_UNKNOWN

        if not ok or not mode_ok:
            return NVM_UNKNOWN, '回写后回读失败，' + TEXT_UNKNOWN
        if old_mode in ('base', 'rover') and mode_data.get('mode') != old_mode:
            return NVM_UNKNOWN, (f'回写后模式仍是 {mode_data.get("mode")}（应为 {old_mode}），'
                                 + TEXT_UNKNOWN)
        if svin_mode == 'fixed':
            worst = max_axis_delta(readback.get('ecef_x') or 0.0, readback.get('ecef_y') or 0.0,
                                  readback.get('ecef_z') or 0.0,
                                  svin.get('ecef_x') or 0.0, svin.get('ecef_y') or 0.0,
                                  svin.get('ecef_z') or 0.0)
            if worst > COORD_TOLERANCE:
                return NVM_UNKNOWN, (f'回写后坐标仍差 {worst:.4f} m，' + TEXT_UNKNOWN)
        elif svin_mode and readback.get('mode') != svin_mode:
            return NVM_UNKNOWN, (f'回写后坐标模式是 {readback.get("mode")}（应为 {svin_mode}），'
                                 + TEXT_UNKNOWN)

        self.result['revert_notes'] = notes
        logging.warning(f'已按备份回写内存态：{"；".join(notes)}')
        return NVM_REVERTED, TEXT_REVERTED

    # ---------- 步骤 1：写坐标（§8.1 第 1 步 / §8.2 门槛） ----------
    def _step_write_coord(self, plan):
        x, y, z = plan.arp
        offset = plan.conversion.get('offset_norm') or 0.0
        hint = f'（已扣除天线偏移 {offset:.3f} m）' if offset > 1e-9 else '（无天线偏移）'
        self._stage('WRITE_COORD',
                    f'写入 ARP 坐标 X={x:.4f} Y={y:.4f} Z={z:.4f}{hint}')
        last_error = ''
        for attempt in range(2):                 # 重发同一坐标是幂等的，值得重试一次
            self._checkpoint()
            ok, message, data = self.driver.set_fixed_ecef(
                x, y, z, persist=False, restart=False, verify=False)
            if not ok:
                last_error = message
                # 明确拒绝（ERROR,n）重试没意义；只有超时才重试
                if (data or {}).get('error') != 'timeout':
                    raise WriteFailed('WRITE_COORD', f'下发固定坐标失败：{message}', data)
            else:
                self._touched = True
                ok2, message2, readback = self.driver.get_svin_config()
                if ok2:
                    verdict = _compare_fixed(readback, x, y, z)
                    if verdict['ok']:
                        self.result['write'] = {'written': plan.written(),
                                                'readback': verdict['readback'],
                                                'delta': verdict['delta'],
                                                'attempts': attempt + 1}
                        self._record_step('WRITE_COORD', True,
                                          f'写入并回读通过（最大轴差 '
                                          f'{verdict["delta"]["max_axis"] * 1000:.3f} mm）',
                                          verdict)
                        return
                    last_error = (f'回读不一致：模式={readback.get("mode")}，'
                                  f'最大轴差 {verdict["delta"]["max_axis"]:.4f} m')
                else:
                    last_error = f'回读坐标失败：{message2}'
            logging.warning(f'写坐标第 {attempt + 1} 次未通过：{last_error}')
            self._sleep(0.3)

        raise WriteFailed('WRITE_COORD',
                          f'写入坐标后回读未通过（{last_error}）。'
                          f'模块保持原状，未做任何保存', {'error': 'verify_mismatch'})

    # ---------- 步骤 0a：配置 Survey-in 收敛（方案 P3 第 1 步） ----------
    # ---------- 步骤 0a：断点续标（模块已收敛过就直接取坐标） ----------
    def _step_svin_resume(self, plan):
        """Survey-in 断点续标：模块已经收敛好了，就直接复用那组均值。

        为什么值得单独做：一轮 Survey-in 是 1800~3600 次定位，1 Hz 下就是 30~60 分钟。
        天线没动过、只是模块被重启过、或者想再写一遍坐标，没理由再等一轮。

        判断条件全部是**只读**的，任何一条不满足就返回 False 交回原流程重新收敛：
            1) 当前是基站模式（SVINSTATUS 只在基站模式输出）
            2) 坐标模式是 survey_in
            3) `<Valid> == 2`（已收敛）且均值非零

        宁可多收敛一轮，也不要在"看起来像收敛"的时候直接采信一个来路不明的坐标。
        """
        if not plan.resume_svin or not self._caps('svin'):
            return False
        self._stage('SVIN_WAIT', '检查模块是否已经收敛过（断点续标）')
        try:
            ok, _message, mode = self.driver.get_mode()
        except Exception as e:                    # noqa: BLE001
            logging.debug(f'断点续标读模式异常（按重新收敛处理）：{e}')
            return False
        if not ok or (mode or {}).get('mode') != 'base':
            return False

        ok, _message, cfg = self.driver.get_svin_config()
        if not ok or (cfg or {}).get('mode') != 'survey_in':
            return False

        # 状态输出可能在模块重启后关掉了，读之前先确认打开
        enabler = getattr(self.driver, 'enable_survey_status', None)
        if enabler is not None:
            try:
                enabler(True, 1)
            except Exception as e:                # noqa: BLE001
                logging.debug(f'打开 SVINSTATUS 输出失败（继续等）：{e}')

        status = None
        deadline = time.time() + SVIN_RESUME_WAIT
        reader = getattr(self.driver, 'survey_status', None)
        while time.time() < deadline:
            self._checkpoint()
            self._drain_svin(0.0)
            if reader is not None:
                ok2, _m2, data = reader()
                if ok2 and data:
                    status = data
                    if data.get('state') == 2:
                        break
            time.sleep(SVIN_POLL_INTERVAL)
        if not status or status.get('state') != 2:
            return False

        x, y, z = status.get('mean_x'), status.get('mean_y'), status.get('mean_z')
        if None in (x, y, z) or max(abs(x), abs(y), abs(z)) < 1.0:
            return False
        try:
            apc = _check_ecef((x, y, z))
        except ValueError as e:
            logging.warning(f'断点续标的均值不是合法 ECEF（{e}），改为重新收敛')
            return False

        plan.apc = apc
        plan.arp = apc
        plan.svin_mode = 'fixed'
        plan.conversion = {
            'apc': dict(zip(('x', 'y', 'z'), apc)),
            'arp': dict(zip(('x', 'y', 'z'), apc)),
            # 来源仍然是固件：断点续标读的也是固件自己算好并保存的均值，
            # 我们只是没让它再跑一遍（见 _step_svin_wait 里"坐标来源边界"的说明）。
            'formula': '复用模块已收敛的 Survey-in 均值（解算全部由模块固件完成）',
            'solved_by': 'firmware',
        }
        self.result['survey'] = {
            'resumed': True,
            'obs': status.get('obs'),
            'cfg_count': status.get('cfg_count'),
            'mean_acc': status.get('mean_acc'),
            'mean_x': x, 'mean_y': y, 'mean_z': z,
            'elapsed_seconds': 0,
        }
        obs = status.get('obs')
        cnt = status.get('cfg_count')
        acc = status.get('mean_acc')
        self._warn(f'复用模块已收敛的 Survey-in 结果（{obs}/{cnt} 次，'
                   f'3D 精度 {acc} m），本次不再重新收敛；'
                   f'如需按当前档位重新收敛，请先关闭模块的 Survey-in')
        self._record_step('SVIN_WAIT', True,
                          f'断点续标：复用已收敛结果（{obs}/{cnt} 次，{acc} m）', status)
        return True

    def _step_svin_cfg(self, plan):
        """让模块进入自收敛：配次数/精度门限 + 打开 `$PQTMSVINSTATUS` 1 Hz。

        与 `_step_config_svin`（还原路径用，只写不收敛）不同：这里配完之后要**等收敛**，
        所以必须把状态输出打开，SVIN_WAIT 才有的读。
        """
        self._checkpoint()
        # 基站模式切完后要重启才生效（协议规范 §4.1 生效条件"需要重启"），而
        # `$PQTMSVINSTATUS` 只在基站模式输出（真机实测 rover 下开状态输出直接 ERROR,1）。
        # 所以在配 survey-in 之前，先重启一次让基站模式真正起来。
        if plan.mode_ack in ('ack', 'deferred'):
            self._stage('SVIN_CFG', '重启让基站模式生效（SVINSTATUS 只在基站模式输出）')
            ok, message, data = self.driver.reboot()
            if not ok:
                raise WriteFailed('SVIN_CFG',
                                  f'切基站后重启失败：{message}。'
                                  f'基站模式未生效，无法开启状态输出', data)
            self.result['svin_preboot'] = data

        cfg = plan.survey_in or {}
        count = cfg.get('count') or 0
        acc = cfg.get('acc') or 0.0
        preset = SURVEY_PROFILES.get(plan.survey_profile or '') or {}
        self._stage('SVIN_CFG',
                    f'配置 Survey-in 自收敛：{count} 次 / 3D 精度门限 {acc:.1f} m'
                    f'（{preset.get("label", plan.survey_profile)}）')
        readback = self._svin_config_verified(count, acc, cfg.get('distance'), 'SVIN_CFG')

        # 打开 PQTMSVINSTATUS 1 Hz（收敛状态就靠它喂）。失败是致命的：没有状态输出，
        # SVIN_WAIT 无从判断收敛，只能干等超时。
        enabler = getattr(self.driver, 'enable_survey_status', None)
        if enabler is not None:
            ok, message, data = enabler(True, 1)
            if not ok:
                raise WriteFailed('SVIN_CFG',
                                  f'打开收敛状态输出失败：{message}。'
                                  f'没有状态输出就无法判断收敛，已中止', data)
        self._record_step('SVIN_CFG', True,
                          f'Survey-in 已配置（{count} 次 / {acc:.1f} m），状态输出已打开',
                          readback)

    # ---------- 步骤 0b：等收敛（方案 P3 第 2 步，唯一的长时间步骤） ----------
    def _step_svin_wait(self, plan):
        """轮询 `$PQTMSVINSTATUS` 直到收敛、用户终止、或超时。

        判据（§5.2 / P3）：`<Valid> == 2`（已收敛）且 `<Obs> >= <CfgDur>`。
        只认 `Valid==2`：文档示例里 59/60 时 `Valid` 仍是 1。

        用户中途终止（`_checkpoint` 抛 TaskAborted）：**用当前均值继续写入**，
        而不是丢弃 —— 方案 P3 明确"支持用户中途手动终止，采用当前结果继续写入"。
        终止时若还没有任何有效观测（obs=0），才判失败。
        """
        self._checkpoint()
        count = (plan.survey_in or {}).get('count') or 0
        self._stage('SVIN_WAIT', f'等待模块自收敛（目标 {count} 次，可随时手动终止）')

        stats = self.driver.survey_status if hasattr(self.driver, 'survey_status') else None
        started = time.time()
        last_obs = 0
        stall_ticks = 0
        terminated = False
        last = None
        while True:
            try:
                self._checkpoint()       # 用户取消 → TaskAborted
            except Exception as e:        # noqa: BLE001
                # 方案 P3：SVIN_WAIT 里的取消不是"放弃任务"，而是"终止等待、
                # 用当前均值继续写入"。这里把取消吞掉并标记，随后走统一收尾。
                # 但"进程退出 / 租约被回收"（TaskEvicted）不能吞——串口已不归我们了。
                if getattr(e, 'evicted', False):
                    raise
                terminated = True
                break
            # 必须主动读串口：`survey_status()` 读的是 channel.cache['svin']，
            # 而 cache 靠 read_line 消费 `$PQTMSVINSTATUS` 行来填充。
            self._drain_svin(time.time() - started)
            # 自收敛最长可能跑 7200 秒。中间模块断电时，不查链路就会
            # 一直等到超时（两小时），用户只会看到"没进展"。
            self._assert_link_alive('SVIN_WAIT')
            time.sleep(SVIN_POLL_INTERVAL)

            status = None
            if stats is not None:
                try:
                    ok, _message, status = stats()
                    if not ok:
                        status = None
                except Exception as e:    # noqa: BLE001
                    logging.debug(f'读 Survey-in 状态异常（忽略）：{e}')
                    status = None

            if status:
                last = status
                obs = status.get('obs') or 0
                progress = status.get('progress')
                if progress is None and count:
                    progress = min(1.0, obs / float(count))
                mean_acc = status.get('mean_acc')

                # 卡住告警：状态一直在出、Valid 一直是 1、Obs 不再增长 → 可能天线被遮
                if status.get('state') == 1 and obs == last_obs and count:
                    stall_ticks += 1
                else:
                    stall_ticks = 0
                last_obs = obs

                remaining = ''
                if obs and count and obs < count:
                    rate = obs / max(1e-6, time.time() - started)
                    if rate > 1e-6:
                        remaining = f'，预计还需 {(count - obs) / rate / 60:.0f} 分钟'
                pct = int(100 * (progress if progress is not None else 0))
                self._stage('SVIN_WAIT',
                            f'收敛中：{obs}/{count} 次（{pct}%）'
                            f'，当前 3D 精度 {mean_acc if mean_acc is not None else "?"} m'
                            f'{remaining}')
                if stall_ticks >= SVIN_STALL_WARN_AT:
                    self._warn('观测数已多轮未增长，天线可能被遮挡或收星条件差，仍在等待')

                if status.get('done') and obs >= count:
                    break
            else:
                # 还没收到任何状态：可能是刚配完、模块还没开始输出。
                self._stage('SVIN_WAIT',
                            f'等待状态输出…（已 {time.time() - started:.0f} 秒）')

            # 超时兜底：不能让一个信号差的模块把串口占住 60 分钟以上。
            if self.svin_timeout and (time.time() - started) >= self.svin_timeout:
                raise WriteFailed(
                    'SVIN_WAIT',
                    f'Survey-in 在 {self.svin_timeout:.0f} 秒内未收敛'
                    f'（已 {last_obs}/{count} 次）。信号条件可能不满足精度门限，'
                    f'可提高档位精度门限、更换开阔场地或改用历史坐标', {'obs': last_obs})

        # ---- 收敛（或被手动终止）后的坐标提取 ----
        if last is None or not (last.get('mean_x') is not None
                                and last.get('mean_y') is not None
                                and last.get('mean_z') is not None):
            raise WriteFailed('SVIN_WAIT',
                              '没有拿到任何有效收敛均值（观测数为 0），无法写入',
                              {'obs': last_obs})

        x, y, z = float(last['mean_x']), float(last['mean_y']), float(last['mean_z'])
        try:
            apc = _check_ecef((x, y, z))
        except ValueError as e:
            raise WriteFailed('SVIN_WAIT', f'模块收敛出的坐标不合法：{e}',
                              {'mean': [x, y, z]})

        # **坐标来源边界（务必看清，别误读成"我们在解算"）**：
        # 标定流程明确「Survey-in 不解析、不读取模块算出的坐标，完全交给固件」。
        # 这里的 MeanX/Y/Z **就是固件自己算的**，我们一个字节都没有参与解算 ——
        # 读它只是为了把它转成「固定坐标」配置写回模块，从而让后续
        # WRITE_COORD → VERIFY_MODE → VERIFY_RTCM 这条统一校验链路有值可比。
        # 换句话说：**观测与解算 100% 由固件完成，我们只做"把结果钉死 + 校验持久化"**。
        # 不读它的话，这条链路就无从校验，用户拿不到"配置真的存进去了"的证据。
        # 精度等级固定标 sub_meter（亚米级），绝不能标 centimeter。
        plan.apc = apc
        plan.arp = apc
        plan.svin_mode = 'fixed'         # 收敛完成后，接下来是"把均值写成固定坐标"
        plan.conversion = {
            'apc': dict(zip(('x', 'y', 'z'), apc)),
            'arp': dict(zip(('x', 'y', 'z'), apc)),
            'formula': 'Survey-in 固件收敛均值即写入值（解算全部由模块固件完成）',
            'solved_by': 'firmware',
        }

        self.result['survey'] = {
            'terminated': terminated,
            'obs': last_obs,
            'count': count,
            'mean_acc': last.get('mean_acc'),
            'mean': {'x': round(x, 4), 'y': round(y, 4), 'z': round(z, 4)},
            'elapsed_seconds': round(time.time() - started, 1),
            'accuracy_grade': ACCURACY_GRADE_SUB_METER,
        }
        note = '已手动终止，采用当前均值' if terminated else '已收敛'
        self._record_step('SVIN_WAIT', True,
                          f'{note}：{last_obs}/{count} 次，'
                          f'3D 精度 {last.get("mean_acc")} m',
                          self.result['survey'])

    # ==================== CORS 差分标定采集链路（方案 P1 / §6 / §7） ====================
    # 顺序：ROVER_SETUP → NMEA_SETUP → GGA_WAIT → CORS_CONNECT → FIX_WAIT
    #       → COLLECT → CORS_CLOSE，得到 ECEF 后走统一写链路（写坐标→切基站→…）。
    def _looks_like_base_stream(self):
        """只读判断：模块现在吐的是不是**基准站 RTCM 流**（而不是定位流）。

        判据刻意简单：先读一小段串口，能读到有效 GGA/RMC/GLL 就是流动站在定位；
        读不到 NMEA 却已经收到过 RTCM 帧，就是基准站在播差分。
        这里**不做任何推断性结论** —— 判断错了的代价是拒绝一个本来能跑的任务，
        而放行的代价是让用户白等十分钟，两害相权取"拒得明确"。
        """
        channel = self.channel
        if channel is None:
            return False
        self._drain_gga(2.0)
        for key in ('gga', 'rmc', 'gll'):
            item = channel.cache.get(key) or {}
            if item.get('valid') and item.get('lat') is not None:
                return False
        try:
            frames = int(channel.counters.get('frames', 0))
        except Exception:                        # noqa: BLE001
            frames = 0
        return frames > 0

    def _restore_memory_mode(self):
        """把"仅内存临时切换"的模式切回去（附录D 的收尾动作）。

        只改内存、**绝不 SAVEPAR**：NVM 里本来就没被改过，再存一次反而把
        "这次标定一个字节都没写进模块"这件事变成假的。
        任何异常都只记日志，不能让它盖住原始失败原因。
        """
        mode = self._mem_restore_mode
        if not mode:
            return
        self._mem_restore_mode = None
        switcher = getattr(self.driver, 'set_mode_memory_only', None)
        if switcher is None:
            self._warn(f'模块内存模式仍是流动站（原为 {mode}），该驱动不支持内存切换，'
                       f'请手动切回（NVM 未受影响，断电重启即恢复）')
            return
        try:
            ok, message, _data = switcher(mode)
        except Exception as e:                   # noqa: BLE001
            ok, message = False, str(e)
        if ok:
            logging.info('[EXTERNAL_ROVER-MEM-ONLY] 已恢复原始内存模式 %s（NVM 未改动）', mode)
            self.result['memory_mode_restored'] = mode
            self._record_step('ROVER_SETUP', True,
                              f'已恢复原始内存模式 {mode}（未写 NVM）', {'mode': mode})
        else:
            logging.warning('[EXTERNAL_ROVER-MEM-ONLY] 恢复内存模式 %s 失败：%s', mode, message)
            self._warn(f'恢复原始内存模式失败（{message}）。NVM 未被修改，'
                       f'断电重启后模块会回到 {mode}，也可以手动切回')

    def _step_cors_collect(self, plan):
        """CORS 差分标定主链路：切流动站、连 CORS、等固定解、采集滤波，产出 ECEF。

        全链路任何一步失败都抛 WriteFailed；用户取消抛 TaskAborted（由 run() 的
        BaseException 分支收尾）。CORS 的 RTCM 只注入模块串口，绝不 distribute（§6.4）。
        """
        from .cors import (CorsClient, CorsError, GgaManager, RECONNECT_BACKOFF,
                           CUMULATIVE_DISCONNECT_LIMIT)

        cfg = plan.cors or {}
        duration = int(cfg.get('duration') or CORS_DEFAULT_DURATION)

        # ---- ROVER_SETUP：切流动站（持久化 + 重启生效）----
        self._step_cors_rover_setup(plan)

        # ---- NMEA_SETUP：打开 GGA / GSA 输出（固定解准入要看质量位、星数、PDOP）----
        self._step_cors_nmea_setup(plan)

        # ---- GGA_WAIT：等 3 条有效 GGA（无则软件生成）----
        gga_manager = self._step_cors_gga_wait(plan)

        # ---- CORS_CONNECT：连 NTRIP Caster（分类错误）----
        client = CorsClient(
            cfg['host'], cfg['port'], cfg['mountpoint'],
            user=cfg.get('user') or '', password=cfg.get('password') or '',
            inject=self._inject_rtcm_to_serial,
            on_state=self._cors_state,
            checkpoint=self._checkpoint)
        try:
            client.connect()
        except CorsError as e:
            raise WriteFailed('CORS_CONNECT', e.message, {'kind': e.kind, **e.detail})

        # 第二道门禁：连上 Caster 后、真正开始喂 RTCM 前再确认一次模式。
        # 切模式（含重启+搜星）到连 CORS 之间可能隔了几十秒，期间模式可能被改。
        self._ensure_rover('CORS_CONNECT')

        # ---- FIX_WAIT + COLLECT：固定解准入 + 1Hz 采集 + 3σ + 卡尔曼 ----
        try:
            collected = self._step_cors_fix_and_collect(
                plan, client, gga_manager, duration,
                mode_guard=self._ensure_rover)
        finally:
            # ---- CORS_CLOSE：断开 CORS（任何路径都关 socket）----
            self._stage('CORS_CLOSE', '断开 CORS 连接，停止 GGA 上报')
            self._audit_cors_downlink(client)
            client.close()

        # 采集结果填进 plan，后续 _step_write_coord 复用
        x, y, z = collected['ecef']
        try:
            apc = _check_ecef((x, y, z))
        except ValueError as e:
            raise WriteFailed('COLLECT', f'滤波收敛出的坐标不合法：{e}', collected)
        # 固定解得到的就是该天线的坐标，直接写入，不做相位中心换算。
        plan.apc = apc
        plan.arp = apc
        plan.conversion = {'apc': dict(zip(('x', 'y', 'z'), apc)),
                           'arp': dict(zip(('x', 'y', 'z'), apc)),
                           'formula': 'CORS 固定解即写入值'}
        plan.svin_mode = 'fixed'
        self.result['cors'] = collected
        tail = collected.get('tail_sigma3d') or 0.0
        verdict = '已收敛' if collected.get('converged') else '未收敛（按当前值采用）'
        self._record_step('COLLECT', True,
                          f'采集完成：{collected["samples"]} 点，'
                          f'全段 3D σ {collected["sigma3d"]:.4f} m，'
                          f'末段 σ {tail:.4f} m（{verdict}）',
                          collected)

    def _audit_cors_downlink(self, client):
        """CORS_CLOSE 时打一条下行体检结论（播了哪些星座、缺不缺 1230）。

        为什么要在**每次**标定都打：2026-11-02 真机有一个挂载点播了 1084
        却不播 1230，模块整整 300 秒卡在浮动解，而当时所有日志都显示
        "链路正常、29 颗星、PDOP 1.8"——问题只能靠事后手写探针才发现。
        把统计变成常规输出，下次换挂载点就能立刻看出差别。
        """
        audit = getattr(client, 'audit', None)
        if audit is None:
            return
        started = getattr(client, '_audit_started_at', 0.0) or 0.0
        elapsed = max(0.0, time.time() - started) if started else 0.0
        try:
            summary = audit.summary(elapsed)
            problems = audit.warnings()
            msgs = dict(audit.counts)
        except Exception as e:                      # noqa: BLE001
            logging.debug(f'下行体检统计失败（忽略）：{e}')
            return

        detail = {'summary': summary, 'counts': msgs,
                  'msm4': audit.msm4, 'issues': list(problems)}
        self.result['cors_downlink'] = detail
        self._record_step('CORS_CLOSE', True, f'下行体检：{summary}', detail)
        logging.info('[CORS_DOWNLINK] %s', summary)

        for text in problems:
            self._warn(text)
            logging.warning('[CORS_DOWNLINK] %s', text)

    # ---- ROVER_SETUP（§9.1）：CORS 标定的第一道门禁 ----
    def _step_cors_rover_setup(self, plan):
        """**先读模式，再决定是否切换**——这是 CORS 标定的第一步，不能省。
        为什么必须先查：CORS 标定的本质是"把 RTCM 喂给模块、让模块自己解算"，
        这只在**流动站**模式下成立。base 模式下模块本身就是差分源头，
        收到的 RTCM 不会被用于解算（协议 §2.3.25：base 自动禁用 NMEA、改播 RTCM），
        盲目注入只会白跑 3~10 分钟，最后拿到一堆单点解样本。

        为什么已经 rover 就不再切：切换要 `SAVEPAR` + 重启，串口会断一次、
        模块重新搜星。无谓的重启既拖时间又消耗 NVM 写入寿命。
        """
        self._stage('ROVER_SETUP', '检查模块当前工作模式（CORS 要求流动站）')
        readonly = plan.method == 'external_rover'
        # 纯基准站型号（有命令集但不能切 rover）在这里就拦下（附录C.2）：
        # 不放行到"切模式失败"那一步，是因为那时的报错是模块回 ERROR,3，
        # 用户看到的是"命令不支持"，而真正的原因是"你选错了标定方式"。
        # 让错误在一开始就指向正确的操作，比让它晚三十秒指向硬件更有用。
        if (not readonly and not getattr(self.driver, 'PASSIVE', False)
                and self._caps('set_mode') and not self._caps('dual_mode')):
            raise WriteFailed(
                'ROVER_SETUP',
                '该模块仅基准站模式，无法运行 CORS 流动站标定。'
                '请改用「只写已知坐标」，用已有的 ECEF 直接固定；'
                '若还没有坐标，可先用一块流动站做「只测坐标」得到天线坐标，再回来写入',
                {'base_only': True})
        ok, message, data = self.driver.get_mode()
        if not ok:
            if readonly:
                # 只测不写时，读不到模式不是致命的：模块可能根本没有命令集。
                # 但只要它吐的是**基准站 RTCM 流**而不是定位流，这条路就走不通 ——
                # 基准站不输出 GGA，没有 GGA 就没有 VRS，采集会一直空转到超时。
                # 这种情况必须当场拒绝，不能让用户白等十分钟才看到"拿不到固定解"。
                if getattr(self.driver, 'PASSIVE', False) and self._looks_like_base_stream():
                    raise WriteFailed(
                        'ROVER_SETUP',
                        '这块模块没有命令集，当前输出的是基准站 RTCM 数据流（没有定位/GGA），'
                        '无法用来做 CORS 测量。请换一块流动站模块，'
                        '或先用厂商工具把它切到流动站模式', data)
                self._warn(f'读不到当前工作模式（{message}）；按流动站继续，'
                           f'若始终拿不到固定解请先手动把模块切到流动站模式')
                self.result['cors_mode'] = {'from': 'unknown', 'to': 'rover',
                                            'switched': False, 'assumed': True}
                self._record_step('ROVER_SETUP', True,
                                  '模式未知，按流动站继续（只测不写，不改模块）', {})
                return
            raise WriteFailed('ROVER_SETUP', f'读不到当前工作模式：{message}', data)
        from_mode = (data or {}).get('mode')

        if from_mode == 'rover':
            self._warn('模块已处于流动站模式，跳过切换（不重复写 NVM、不重启）')
            self.result['cors_mode'] = {'from': 'rover', 'to': 'rover', 'switched': False}
            self._record_step('ROVER_SETUP', True,
                              '当前已是流动站，无需切换（回读确认）',
                              {'from': 'rover', 'switched': False})
            return

        if readonly and from_mode == 'base':
            # 附录D：双模模块当前是基准站，而基准站不输出 NMEA-GGA，没法做 CORS 采集。
            # 解法是**仅内存**临时切到流动站：下发模式命令，但**不下发 SAVEPAR**。
            # 这样 NVM 里仍是原来的基准站配置，任务结束/异常/用户中止时把内存态切回去，
            # 即使直接拔电，模块重启后也会从 NVM 读到原来的基准站配置。
            # 只有"必须保存到 NVM 才能生效"的型号才做不到这件事，那种情况下直接失败。
            self._stage('ROVER_SETUP', '当前是基准站：临时切到流动站（仅内存，不写 NVM）')
            switcher = getattr(self.driver, 'set_mode_memory_only', None)
            if switcher is None:
                raise WriteFailed(
                    'ROVER_SETUP',
                    '这块模块当前是基准站模式，而本模式（只测不写）不改动模块配置。'
                    '该型号驱动不支持"仅内存切换模式"（必须保存到 NVM 才生效），'
                    '无法在不改配置的前提下测量。'
                    '请改选「CORS 差分标定」（会自动切流动站、测完切回基准站并写入坐标），'
                    '或先手动把模块切到流动站模式再测', data)
            ok, message, mdata = switcher('rover')
            if not ok:
                raise WriteFailed(
                    'ROVER_SETUP',
                    f'临时切到流动站失败：{message}。'
                    f'本模式不会写 NVM，若该型号必须保存才生效，请改用「CORS 差分标定」',
                    mdata)
            # 记下原始模式，收尾时必须切回去（见 _restore_memory_mode）
            self._mem_restore_mode = from_mode
            logging.info('[EXTERNAL_ROVER-MEM-ONLY] 临时内存切换 rover，未写入 NVM；'
                         '结束将恢复原始内存模式 %s', from_mode)
            self._warn('模块当前是基准站，已**临时**切到流动站（仅内存，未写 NVM）：'
                       '标定结束会自动切回基准站；即使中途断电，模块重启后也会回到基准站配置')
            self.result['cors_mode'] = {'from': from_mode, 'to': 'rover',
                                        'switched': True, 'memory_only': True}
            self._record_step('ROVER_SETUP', True,
                              f'已从 {from_mode} 临时切为流动站（仅内存，未写 NVM）', mdata)
            return

        if from_mode not in ('base', 'rover'):
            raise WriteFailed(
                'ROVER_SETUP',
                f'当前模式为 {from_mode}，无法用于 CORS 标定（必须切到流动站）', data)

        self._stage('ROVER_SETUP',
                    f'当前是 {from_mode}，切换为流动站（才能接收 CORS 差分）')
        ok, message, data = self.driver.set_mode('rover', persist=True, restart=True,
                                                 verify=True)
        if not ok:
            raise WriteFailed('ROVER_SETUP', f'切换流动站失败：{message}', data)
        self._touched = True

        # set_mode 内部已回读一次，这里再独立确认：模式不对就不该继续连 CORS
        ok2, message2, data2 = self.driver.get_mode()
        if not ok2 or (data2 or {}).get('mode') != 'rover':
            raise WriteFailed(
                'ROVER_SETUP',
                f'切换后回读模式是 {(data2 or {}).get("mode")}，不是 rover：'
                f'此时注入的 RTCM 不会被模块用于解算', data2)
        self.result['cors_mode'] = {'from': from_mode, 'to': 'rover', 'switched': True}
        self._record_step('ROVER_SETUP', True,
                          f'已从 {from_mode} 切为流动站（回读确认）', data2)

    def _ensure_rover(self, stage):
        """注入 RTCM 期间的硬门禁：模式必须仍是 rover。

        模式可能被外部改掉（用户手动操作、模块异常复位、断电重启回默认）。
        在 base 模式下继续注入 RTCM 不会产生任何差分，但采集循环会一直跑，
        最后得出"信号差、进不了固定解"的错误结论——比直接失败更糟。
        读模式失败只告警不失败：串口偶尔丢一帧不该终止整个标定。
        """
        if getattr(self.driver, 'PASSIVE', False):
            return                       # 未知模块没有命令集，模式无从复核
        ok, message, data = self.driver.get_mode()
        if not ok:
            self._warn(f'模式复核失败（{message}），沿用上次确认的结果继续')
            return
        mode = (data or {}).get('mode')
        if mode != 'rover':
            raise WriteFailed(
                stage,
                f'模块模式已变为 {mode}（只有流动站才接收差分），已中止注入 RTCM', data)

    # ---- NMEA_SETUP（§9.1）----
    def _step_cors_nmea_setup(self, plan):
        """配 GGA 1Hz（固定解准入要质量位/星数）+ GSA（PDOP）+ FIXRATE 1Hz。

        **前提**：这一步必须在 ROVER_SETUP 之后跑。协议 §2.3.25 规定基站模式
        自动禁用 NMEA、改播 RTCM3-1005 + MSM，所以在 base 模式下读到的
        `GGA rate=0` 是**设计行为**，不能当成"模块不支持/不输出 GGA"。
        """
        self._stage('NMEA_SETUP', '配置 GGA / GSA 输出（固定解准入判据用）')

        # 未知模块（只读驱动）没有命令集：一条 NMEA_SETUP 都发不出去。
        # 这时唯一能做的是"读它现在吐什么"——模块本来就在输出 GGA 才有得测。
        if getattr(self.driver, 'PASSIVE', False):
            self._warn('未知模块没有命令集：不配置 NMEA，直接读模块现有输出。'
                       '若迟迟拿不到 GGA，请先用上位机把模块的 GGA 输出打开')
            self.result['cors_nmea'] = {'applied': {}, 'before': {}, 'passive': True}
            self._record_step('NMEA_SETUP', True, '未知模块：跳过 NMEA 配置，直接读现有输出',
                              self.result['cors_nmea'])
            return

        # 只测不写：模块不归我们管，能不动就不动。
        # 已经在输出 GGA 就一个字都不发；必须打开才动，而且只改内存态（不保存），
        # 并明确告诉用户"重启后恢复原值"——否则用户会以为自己的设备被改了。
        if plan.method == 'external_rover':
            ready = []
            for name in ('GGA', 'GSA'):
                ok0, _m0, d0 = self.driver.get_message_rate(name)
                if ok0:
                    fields = (d0 or {}).get('fields') or []
                    ready.append(fields[-1] if fields else None)
                else:
                    ready.append(None)
            if ready and str(ready[0]) not in ('None', '0', ''):
                self.result['cors_nmea'] = {'applied': {}, 'before': {'GGA': ready[0]},
                                             'readonly': True}
                self._record_step('NMEA_SETUP', True,
                                  f'只测不写：GGA 已在输出（{ready[0]}），未做任何配置',
                                  self.result['cors_nmea'])
                return
            self._warn('只测不写：模块当前没有输出 GGA，正在临时打开（只改内存态、不保存，'
                       '模块重启后恢复原值）')

        # 先记录「切到 rover 后、配置前」的原始速率：这是"rover 模式默认发不发
        # GGA"的唯一实测证据（只读不写，失败不影响流程：未配置过的语句回 ERROR,1）。
        before = {}
        for name in ('GGA', 'GSA'):
            ok0, _m0, d0 = self.driver.get_message_rate(name)
            if ok0:
                fields = (d0 or {}).get('fields') or []
                before[name] = fields[-1] if fields else None
        # 只如实记录，不猜"模块默认值"：这个值受固件默认、上次标定残留、
        # 是否保存过 NVM 三重影响（真机两次实测：一次读到 1、一次读到 0，
        # 同样都是 base→rover）。代码不依赖它，一律显式配置。
        if before.get('GGA') in (None, '0'):
            self._warn(f'切换后 GGA 输出为 {before.get("GGA", "未读到")}，'
                       f'已显式配置为 1 Hz（不依赖模块默认值）')

        applied = {}
        for name in ('GGA', 'GSA'):
            ok, message, _data = self.driver.set_message_rate(name, 1)
            if ok:
                applied[name] = 1
            else:
                # GGA 配不上是致命的（拿不到固定解判据）；GSA 配不上可降级（§7.2）
                if name == 'GGA':
                    raise WriteFailed('NMEA_SETUP', f'GGA 输出配置失败：{message}', _data)
                self._warn(f'GSA 输出配置失败（{message}），固定解准入将降级为 HDOP 判定')

        # ---- 关掉"标定期间用不到"的 NMEA：这是 GGA 被饿死的根因 ----
        # 2026-11-03 真机 A/B 实测（注入 CORS 差分 785 B/s 前后各 40 秒）：
        #   全开 GSA/GSV/GLL/VTG/RMC → 注入差分后 GGA **6 条 / 40 秒（0.15 Hz）**
        #   只留 GGA+GSV/GSA 关掉   → 注入差分后 GGA **41 条 / 40 秒（1.03 Hz）**
        # 也就是说模块在解析海量 RTCM（五星座 × 多频点 × MSM4 + 1005/1230）
        # 的同时，NMEA 输出调度会被挤到几乎停摆。之前三次标定全部失败的
        # FIX_WAIT，一部分时间其实是在一个"GGA 只剩 0.15 Hz"的链路上等的：
        # 差分龄期刚攒到几十毫秒就又过期，模糊度永远建立不起来。
        # 这里只保留 GGA（固定解判据）+ GSA（PDOP 准入），其余全关。
        quiet = {}
        for name in QUIET_NMEA:
            ok, message, _data = self.driver.set_message_rate(name, 0)
            quiet[name] = 'off' if ok else f'failed:{message}'
        applied['QUIET'] = quiet

        # FIXRATE：`PQTMCFGMSGRATE` 的 Rate 语义是「每 N 次定位输出一次」，
        # 不是 Hz。只配 Rate=1 不够：定位 10 Hz 时 GGA 会跟着变 10 Hz，
        # 采集的 1 Hz 去重会和差分龄期对不齐，所以定位间隔必须一起钉住。
        #
        # ⚠️ 但**千万别钉到 1000 ms**。2026-11-02~03 真机实测证据：
        #   FixRate=100 (10 Hz)  → GGA 实测 1.00 Hz，间隔稳定 1.00s   ✅
        #   FixRate=1000 (1 Hz)  → GGA 实测 0.011 Hz，87 秒才 1 条    ❌
        # 两种情况下 `PQTMCFGMSGRATE,R,1,GGA` 读回都是 1，配置"看起来没问题"，
        # 但固定解始终进不去——VRS 拿 87 秒一条的 GGA 根本无法持续跟踪测站。
        # 本固件（LG290P03AANR01A03S）在 1 Hz 定位节拍下 GGA 调度会卡死。
        # 所以这里钉在 100 ms（10 Hz 定位），再用 GGA 1/10 抽稀出 1 Hz 输出。
        setter = getattr(self.driver, 'set_fix_rate', None)
        if setter is not None:
            ok, message, _data = setter(FIXRATE_SAFE_MS)
            if ok:
                applied['FIXRATE'] = FIXRATE_SAFE_MS
            else:
                self._warn(f'定位间隔配置失败（{message}），改用实测频率校验兜底')
        self.result['cors_nmea'] = {'applied': applied, 'before': before}

        # ---- 关键闭环：RTK 引擎必须靠 SAVEPAR + 重启才真正初始化 ----
        # 2026-11-02~03 真机四次标定全部卡在 FIX_WAIT，统一根因就是这个：
        # 上面这些命令一条条下发、每条回 `OK`、回读也全部正确，**但模块的 RTK
        # 解算引擎从未重新初始化**。表现是质量位恒为 2（DGPS），差分龄期字段恒空——
        # 模块根本没把收到的 RTCM 当成有效差分源，无论注入多久都不会变。
        #
        # 实测证据（同一硬件、同一天线位置、同一差分流 RTCM33_GRCEJ_FPSE）：
        #   只下发配置不重启 → 8 分钟 1516 条 GGA **全为质量位 2**
        #   加 SAVEPAR + PQTMSRR 重启 → 重连后 **质量位 5（浮动）**，
        #     hdop 1.96 → 0.80，高程极差 9.85 m → **0.57 m**
        #
        # 这正是文档坑 #13「看似生效但重启回退」的镜像面：配置存在内存里能回读，
        # 但只有落盘重启后引擎才会按新配置重建。所以这一步不能省、也不能移到
        # 最后——它必须在"配完 NMEA/RTK 参数"之后、"验频率/连 CORS"之前。
        self._reinit_rtk_engine(applied)

        self._record_step('NMEA_SETUP', True,
                          f'已配置 {len(applied)} 条 NMEA 输出并落盘重启'
                          f'（配置前 GGA={before.get("GGA", "?")}）',
                          self.result['cors_nmea'])

        # 配置"报成功"不等于"真的在出"。必须实测 GGA 输出频率，
        # 否则会带着 0.01 Hz 的 GGA 跑满整个固定解窗口，最后误判成"信号差"。
        # **必须放在重启之后**：重启前测到的是旧状态下的频率，重启后可能完全不同。
        self._verify_gga_rate()

    def _reinit_rtk_engine(self, applied):
        """落盘 + 重启 + 重连 + 回读确认：让 RTK 引擎按新配置真正重建。

        **这是 CORS 标定能不能拿到差分的关键一步**，不是可选的"保险动作"。
        模块的很多 RTK 相关配置（接收机模式、RTK 使能、定位间隔、消息调度）
        只写进运行时内存：命令回 OK、回读也能读到，但解算引擎仍跑在旧参数上。
        只有 `PQTMSAVEPAR` 落盘 + `PQTMSRR` 重启后，引擎才会重新初始化。

        重启会断串口，所以顺序必须是：落盘 → 重启 → 等启动 → 重开串口 → 回读确认。
        """
        self._stage('RTK_REINIT', '保存配置并重启模块，初始化 RTK 解算引擎')
        saver = getattr(self.driver, 'save', None)
        rebooter = getattr(self.driver, 'reboot', None)
        if saver is None or rebooter is None:
            self._warn('驱动不支持 SAVEPAR/重启，跳过 RTK 引擎重建；'
                       '若迟迟进不了固定解，请手动重启模块后再试')
            self.result['rtk_reinit'] = {'skipped': True}
            return

        ok, message, data = saver()
        applied['SAVEPAR'] = 'ok' if ok else f'failed:{message}'
        if not ok:
            # 部分固件在 rover 模式下 SAVEPAR 会回 ERROR，但重启依然有效。
            # 不直接判死，如实记录后继续 —— 真正的裁判是后面的质量位实测。
            self._warn(f'配置落盘返回异常（{message}），继续尝试重启')

        ok2, message2, data2 = rebooter()
        applied['RESTART'] = 'ok' if ok2 else f'failed:{message2}'
        if not ok2:
            self._warn(f'重启命令返回异常（{message2}），仍等待模块自恢复')

        # 重开串口：`reopen` 内部已按 REBOOT_REOPEN_DELAY 等待并多次重试，
        # 覆盖 USB CDC 重新枚举的"设备不存在"窗口。
        reconnected = False
        try:
            self.channel.reopen()
            reconnected = True
        except Exception as exc:                 # noqa: BLE001 - 串口异常种类极杂
            logging.warning('[RTK_REINIT] 重启后重开串口失败：%s', exc)

        self.result['rtk_reinit'] = {
            'save': applied.get('SAVEPAR'),
            'restart': applied.get('RESTART'),
            'reconnected': reconnected,
        }
        if not reconnected:
            raise WriteFailed(
                'RTK_REINIT',
                '模块重启后无法重新建立串口通信。请检查 USB 线与供电，'
                '或手动重启模块后重试', self.result['rtk_reinit'])

        # 回读确认：配置确实还在（NVM 落盘成功），否则后面白等固定解
        ok3, message3, mode_data = self.driver.get_mode()
        if not ok3 or (mode_data or {}).get('mode') != 'rover':
            raise WriteFailed(
                'RTK_REINIT',
                f'重启后工作模式是 {(mode_data or {}).get("mode")}（应为 rover）：'
                f'配置没有持久化成功，RTK 引擎不会按新参数重建', mode_data)

        self._record_step('RTK_REINIT', True,
                          '已落盘并重启，串口重连且回读确认仍为流动站',
                          self.result['rtk_reinit'])

    def _verify_gga_rate(self, stage='GGA_RATE_CHECK', label='实测 GGA 输出频率',
                         window=None, strict=True):
        """实测 GGA 输出频率；`strict=True` 时不达标直接失败（不是告警）。

        为什么必须硬失败：GGA 是 VRS 的"点单地址"。频率掉到 0.01 Hz 时，
        CORS 侧看到的是测站"消失"，不会报错、不会断流，只是不再生成对应的
        差分改正数——现象和"信号差、进不了固定解"完全一样，现场极难区分。
        2026-11-02 的 180 秒就是耗在这上面。宁可这一步失败重来。

        判据放宽到 `GGA_MIN_HZ`(0.8 Hz)：1 Hz 设计值，允许时钟抖动。

        **`strict=False` 的用途**：差分注入之后必须再测一次。同一个配置
        在无差分时是 1.03 Hz、注入 785 B/s 的 RTCM 后掉到 0.15 Hz——
        只测"注入前"就等于什么都没测。
        """
        self._stage(stage, label)
        window = float(window or GGA_RATE_WINDOW)
        seen = []
        deadline = time.time() + window
        while time.time() < deadline:
            line = None
            try:
                line = self.channel.read_line(0.1) if self.channel else None
            except Exception:                        # noqa: BLE001
                line = None
            if not line:
                continue
            text = line.decode('ascii', 'ignore') if isinstance(line, bytes) else str(line)
            for part in text.replace('\r', '\n').split('\n'):
                part = part.strip()
                if part.startswith(('$GNGGA', '$GPGGA')):
                    seen.append(part)
            try:
                if self.channel:
                    self.channel.absorb(line)
            except Exception:                        # noqa: BLE001
                pass

        hz = len(seen) / window if window else 0.0
        detail = {'window': window, 'count': len(seen), 'hz': round(hz, 3),
                  'min_hz': GGA_MIN_HZ}
        if seen:
            last = seen[-1]
            detail['last'] = last[:72]
        if hz < GGA_MIN_HZ:
            if not strict:
                self._warn(f'{label}：GGA 实测 {hz:.3f} Hz（{len(seen)} 条 / '
                           f'{window:.0f}s），低于 {GGA_MIN_HZ} Hz')
                self.result.setdefault('cors_gga_rate_after_diff', detail)
                self._record_step(stage, False,
                                  f'GGA 实测仅 {hz:.2f} Hz（{len(seen)} 条 / {window:.0f}s）',
                                  detail)
                return detail
            raise WriteFailed(
                stage,
                f'GGA 实际只输出 {len(seen)} 条 / {window:.0f} 秒 = {hz:.3f} Hz'
                f'（要求 ≥{GGA_MIN_HZ} Hz）。VRS 无法用这个频率持续跟踪测站，'
                f'继续跑只会把"配置假成功"误判成"信号差进不了固定解"。'
                f'常见原因：① 定位间隔被写成 1000 ms（本固件会让 GGA 调度卡到 87 秒一次）；'
                f'② 注入 CORS 差分后 NMEA 输出被 RTCM 解析挤占（关掉 GSV/GLL/VTG/RMC）。',
                detail)
        self.result.setdefault('cors_gga_rate', detail)
        self._record_step(stage, True,
                          f'GGA 实测 {hz:.2f} Hz（{len(seen)} 条 / {window:.0f}s）', detail)
        return detail

    # ---- GGA_WAIT（§9.1）：等 3 条有效 GGA，无则软件生成 ----
    def _step_cors_gga_wait(self, plan):
        """等 3 条**不同**的有效 GGA（原生）；等不到但模块有 RMC/GLL 则走软件生成。

        真机暴露过两个坑，这里都必须避开：
        1. **不能数同一行**：cache 里的 GGA 在没被新行覆盖前一直"有效"，
           按循环次数计数会把 1 条 GGA 数成 3 条。必须按原始行变化计数。
        2. **不能一见 RMC 就退化成软件生成**：真机 rover 模式下 RMC 先到、
           GGA 晚一两秒到，早退会把本来就有的原生 GGA 变成单点软件生成，
           精度从厘米级掉到米级。所以先给原生 GGA 一段专属等待窗口。
        """
        from .cors import GgaManager
        self._stage('GGA_WAIT', '等待模块输出定位（GGA）')
        channel = self.channel
        deadline = time.time() + GGA_TOTAL_WAIT            # 完全没位置时的总等待
        native_deadline = time.time() + GGA_NATIVE_WAIT    # 只给原生 GGA 的窗口
        gga_count = 0
        last_raw = None
        while time.time() < deadline:
            self._checkpoint()
            self._drain_gga(0.2)
            gga = channel.cache.get('gga') if channel else None
            if gga and gga.get('valid') and gga.get('lat') is not None:
                raw = gga.get('raw') or (gga.get('utc'), gga.get('lat'), gga.get('lon'))
                if raw != last_raw:          # 必须是新的一条，不是缓存里那条
                    last_raw = raw
                    gga_count += 1
                    if gga_count >= 3:
                        break
            # 原生 GGA 窗口过了仍没有，且有 RMC/GLL → 允许软件生成（精度受限）
            if gga_count == 0 and time.time() >= native_deadline:
                if channel and (channel.cache.get('rmc') or channel.cache.get('gll')):
                    break
        gga_manager = GgaManager(channel, on_hint=self._warn)
        if gga_count >= 3:
            self._record_step('GGA_WAIT', True, f'已收到 {gga_count} 条有效 GGA（原生透传）', {})
        else:
            # 判定能否软件生成
            has_pos = bool(channel and (channel.cache.get('rmc') or channel.cache.get('gll')))
            if has_pos:
                self._warn('模块未输出 GGA，改用 RMC/GLL 软件生成（精度受单点定位限制，约 1–3 米）')
                self._record_step('GGA_WAIT', True, '无原生 GGA，启用软件生成（精度受限）',
                                  {'gga_count': gga_count, 'software_gga': True})
            else:
                raise WriteFailed('GGA_WAIT',
                                  f'模块既无 GGA 也无 RMC/GLL 输出，拿不到稳定 NMEA-GGA'
                                  f'（{GGA_TOTAL_WAIT:g} 秒内一条有效语句都没有），'
                                  f'无法获取位置。按顺序查：'
                                  f'① 天线是否接好、上方是否被完全遮挡（无星则无定位语句）；'
                                  f'② 模块是否被切成基准站——基站模式会自动关闭 NMEA 改播 RTCM，'
                                  f'此时串口里是二进制流不是文本语句；'
                                  f'③ 串口是否接对（只接了 TX 不接 RX 时读不到回传的定位流）；'
                                  f'④ 模块是否有命令集需要先打开 GGA 输出（未知模块请先用厂商上位机打开）',
                                  {'gga_count': gga_count})
        self._await_position_fix(gga_manager)
        return gga_manager

    def _await_position_fix(self, gga_manager):
        """进 FIX_WAIT 前先确认模块**自身已经定位**（GGA 质量位 > 0）。

        为什么加这道门禁：2026-11-02 真机 18:12 那次，GGA 刚配置成功时模块还在
        冷启动，`质量=1，卫星=9，PDOP=3.06` 就径直进了 FIX_WAIT，等了 180 秒
        才超时。冷启动阶段星数还在爬升，差分注进去也没有多余的观测量可用，
        白白耗掉整个固定解等待窗口。

        质量位 0 表示"未定位"（GGA 里的经纬度是空的或全 0），此时上报给 VRS 的
        位置毫无意义——**必须等到 >0 才值得开始注差分**。
        注意这不等于"必须等到固定解"：质量 1/2/5 都可以进 FIX_WAIT，
        差分会把模块自己从单点/DGPS/浮动推向固定。
        """
        channel = self.channel
        if channel is None:
            return
        deadline = time.time() + POSITION_FIX_TIMEOUT
        last_note = 0.0
        first_q = None
        while time.time() < deadline:
            self._checkpoint()
            self._drain_gga(0.2)
            gga = channel.cache.get('gga') or {}
            quality = gga.get('quality')
            sats = gga.get('num_sats') or 0
            if first_q is None and quality is not None:
                first_q = quality
            if quality and quality > 0:
                if quality != 1 or first_q != quality:
                    logging.info('[GGA_WAIT] 模块已定位：质量位=%s，卫星=%s', quality, sats)
                self._record_step('GGA_WAIT', True,
                                  f'模块已定位（质量位={quality}，卫星={sats}），开始注入差分',
                                  {'quality': quality, 'num_sats': sats})
                return
            # 每 10 秒提示一次，让现场知道在等什么（而不是看起来卡住）
            if time.time() - last_note >= 10:
                last_note = time.time()
                self._warn(f'模块尚未定位（质量位={quality}，卫星={sats}），'
                           f'等待搜星中…已等 {POSITION_FIX_TIMEOUT - (deadline - time.time()):.0f} 秒')
                if gga_manager is not None:
                    gga_manager.source()      # 触发一次防抖提示（会说明"质量位为 0"）

        raise WriteFailed(
            'GGA_WAIT',
            f'模块 {POSITION_FIX_TIMEOUT:g} 秒内始终没有定位（GGA 质量位一直是 0），'
            f'无法向 CORS 上报有意义的概略位置，差分也就无从生成。按顺序查：'
            f'① 天线是否接好、是否在室外能见到天空；'
            f'② 模块是否处于冷启动（首次上电需要 30~60 秒搜星，之后热启动只要几秒）；'
            f'③ 天线馈线是否短路/断路（可以用模块的 GSV 语句看各星座可见星数）；'
            f'④ 是否被错误配置成只跟踪少数星座',
            {'first_quality': first_q})

    # ---- FIX_WAIT + COLLECT（§7.2 / §7.5）----
    def _step_cors_fix_and_collect(self, plan, client, gga_manager, duration,
                                   mode_guard=None):
        """等连续 30s 固定解 → 1Hz 采集 → 3σ 剔除 → 三轴卡尔曼 → 收敛判定。

        双终止条件（§7.5）：到时长 或 连续 60s 三轴 σ≤2cm。
        采集期间 CORS 可能断连：走 3 次退避重连，累计断连 > 5 分钟判失败（§6.5）。

        `mode_guard` 可选注入（外层传 `self._ensure_rover`）：采集期间周期性复核
        模块是否仍在流动站模式。不传则不检查（离线自测用假驱动，没有模式概念）。
        """
        from .cors import CorsError, RECONNECT_BACKOFF, CUMULATIVE_DISCONNECT_LIMIT

        fix_hold_started = None       # 连续固定解的起始时刻
        fix_wait_started = None       # 进入 FIX_WAIT 的时刻（超时兜底用）
        # 注入差分后的 GGA 频率复核游标（见循环里 FIX_RATE_PROBE_SEC 那段）
        gga_forwarded_at_probe = None
        gga_count_at_probe = 0
        # 浮动解降级判定用的计数器：整个 FIX_WAIT 窗口里有多少轮满足浮动解准入。
        # 用它区分「天线遮挡（浮动解稳但固定不了）」和「差分根本没进来（连浮动都没有）」。
        float_hits = 0
        float_probes = 0
        fix_wait_timeout = float(self.fix_wait_timeout or FIX_WAIT_TIMEOUT)
        fix_since = 0.0
        hdop_fallback = False
        collect_started = None        # 进入采集的起始时刻
        samples = []                  # [(x, y, z), ...] 原始样本
        kf = None                     # 三轴卡尔曼（三个 Kalman1D 实例）
        kf_fed_upto = 0               # 已喂进卡尔曼的 samples 下标（防重复观测）
        kf_history = []               # 滤波后状态（仅诊断：看滤波器输出是否稳）
        clean_history = []            # 3σ 剔除后的**原始样本**（收敛判据的唯一依据）
        rejected = 0
        last_gga_time = 0.0
        next_mode_check = time.time() + MODE_RECHECK_SECONDS

        from .geo import Kalman1D, Welford3, reject_outliers_3sigma_xyz

        while True:
            self._checkpoint()
            # 周期性复核模式：base 模式下注入的 RTCM 不产生差分，
            # 不查就会一直跑到超时，最后误判成"信号差"。
            # **只在采集阶段查**：FIX_WAIT 是模糊度搜索期，此时往串口里插
            # PQTM 查询会打断 RTCM 流的连续性，拖慢甚至阻断固定解收敛
            # （2026-11-02 真机：加了 FIX_WAIT 检查后 120 秒仍停在浮动解，
            # 去掉后 38 秒固定）。注入前那一次检查已经覆盖了"模式是否可注入"。
            if (mode_guard is not None and collect_started is not None
                    and time.time() >= next_mode_check):
                mode_guard('COLLECT')
                next_mode_check = time.time() + MODE_RECHECK_SECONDS
            channel = self.channel
            # **先 drain 再 pump**：顺序反了的话，pump 上报给 CORS 的是上一轮缓存里的
            # 旧位置（最多晚一个轮询周期），而 VRS 正是拿这条位置去生成虚拟观测。
            # 先把模块这 200ms 内新吐的 GGA/GSA 读进缓存，再让 pump 逐条转发，
            # 转发出去的永远是「刚刚这一条」。同时固定解判据也靠这次 drain 刷新。
            self._drain_gga(GGA_DRAIN_WINDOW)
            self._assert_link_alive('FIX_WAIT' if collect_started is None else 'COLLECT')

            # 防抖生效时每轮打一条 `[GGA_JUMP_DROP]` 心跳（内部按 5 秒限流）。
            # 这条日志是现场判断"上报给 VRS 的位置是不是被模块带跑"的第一手证据。
            gga_manager.note_guard_active(
                f'{len(samples)} 点' if collect_started is not None else '等待固定解')

            # 单步轮询：收 RTCM 注入 + 逐条转发最新 GGA（gga_source 内部判新/保底重发）
            try:
                client.pump(gga_manager.source)
            except CorsError as e:
                if e.kind == 'write':
                    raise WriteFailed('COLLECT', f'RTCM 注入模块串口失败：{e.message}',
                                      {'kind': e.kind})
                # 网络类断连：走退避重连，累计超限判失败（§6.5）
                ok, outage = self._cors_reconnect(client, gga_manager)
                if not ok:
                    raise WriteFailed('FIX_WAIT',
                                      'CORS 累计断连超过 5 分钟，终止标定（检查网络）')
                # 抖动宽容（补充附录G）：2 秒内的短暂断连不清零"连续固定解"计时器。
                # 移动网络/VRS 服务端主动轮换连接都会产生 1~2 秒的空窗，按老逻辑
                # 每抖一次就要从头再等 30 秒——晴天无风的现场也会永远进不了采集。
                # 断连超过门限仍是真断连，必须清零：这时的差分确实断了 30 秒以上。
                if outage <= CORS_NET_JITTER_TOLERANCE_SEC:
                    logging.info('CORS 抖动恢复（断开 %.1f 秒 ≤ %.1f 秒），'
                                 '连续固定解计时器继续累计',
                                 outage, CORS_NET_JITTER_TOLERANCE_SEC)
                else:
                    logging.info('CORS 长断连恢复（断开 %.1f 秒 > %.1f 秒），'
                                 '重置连续固定解计时器，重新累计 %.0f 秒',
                                 outage, CORS_NET_JITTER_TOLERANCE_SEC, FIX_HOLD_SECONDS)
                    fix_hold_started = None
                continue

            gga = channel.cache.get('gga') if channel else None
            gsa = channel.cache.get('gsa') if channel else None

            # 固定解准入判据（§7.2）
            quality = (gga or {}).get('quality')
            num_sats = (gga or {}).get('num_sats') or 0
            # PDOP 只从 GSA 拿；GSA 无则降级 HDOP（§7.2）
            pdop = (gsa or {}).get('pdop')
            if pdop is None:
                hdop = (gsa or {}).get('hdop')
                if hdop is None:
                    hdop = (gga or {}).get('hdop')
                pdop_ok = (hdop is not None and hdop <= FIX_MAX_HDOP_FALLBACK)
                if gsa is None or not gsa.get('valid'):
                    hdop_fallback = True
            else:
                pdop_ok = pdop <= FIX_MAX_PDOP

            fixed = (quality == FIX_QUALITY_REQUIRED
                     and num_sats >= FIX_MIN_SATS and pdop_ok)

            # 浮动解降级准入：天线环境受限（低仰角为主、天顶被挡）时永远进不了
            # 固定解，但浮动解足够给基准站定初值。**只有固定解彻底无望时才降级**——
            # 先给固定解完整窗口，超时后再看这段时间里浮动解是否稳定。
            float_ok = (quality == FLOAT_QUALITY_ALLOWED
                        and num_sats >= FLOAT_MIN_SATS
                        and (pdop if pdop is not None else 0.0) <= FLOAT_MAX_PDOP)
            # 是否已降级（一旦降级就不再回头等固定解，避免在超时边缘反复跳）
            allow_float = bool(self.result.get('allow_float_collect'))
            accepted = fixed or (allow_float and float_ok)
            hold_needed = FIX_HOLD_SECONDS if fixed else FLOAT_HOLD_SECONDS

            # 只在 FIX_WAIT（未进采集）阶段统计浮动解命中率，采集阶段不再需要
            if collect_started is None:
                float_probes += 1
                if float_ok:
                    float_hits += 1

            now = time.time()
            if accepted:
                if fix_hold_started is None:
                    fix_hold_started = now
                fix_since = now - fix_hold_started
            else:
                fix_hold_started = None
                fix_since = 0.0

            # 实时指标：Web 状态接口（1 秒轮询）的指标卡直接读这里，
            # 不用去解析阶段文案里那串中文（解析文案是脆的，改一个字就断）。
            live = {
                'phase': 'COLLECT' if collect_started is not None else 'FIX_WAIT',
                'quality': quality,
                'num_sats': num_sats,
                'pdop': pdop,
                'hdop_fallback': bool(hdop_fallback),
                'fixed': bool(fixed),
                'fix_seconds': round(fix_since, 1),
                'samples': len(samples),
                'rejected': rejected,
                'sigma': None,
                'elapsed': round(now - collect_started, 1) if collect_started else 0.0,
                'duration': duration,
                'converged': False,
                'gga_forwarded': getattr(gga_manager, 'forwarded', 0),
                # 防抖状态：前端据此常驻显示"天线严禁移动"，并在跳变时变红。
                # 不能只靠日志——日志在滚动区里，用户不会一直盯着看。
                'gga_guard': bool(getattr(gga_manager, 'guard', False)),
                'gga_guard_active': bool(getattr(gga_manager, 'guard_active', False)),
                'gga_software': bool(getattr(gga_manager, 'software_mode', False)),
                'gga_jump_dropped': int(getattr(gga_manager, 'jump_dropped', 0) or 0),
                'gga_jump_stopped': bool(getattr(gga_manager, 'jump_stopped', False)),
            }
            self.result['live'] = live

            # 尚未进入采集：只显示固定解进度
            if collect_started is None:
                if accepted and fix_since >= hold_needed:
                    collect_started = now
                    fix_hold_started = None
                    if fixed:
                        self.result['accuracy_grade'] = ACCURACY_FIXED
                        self._stage('COLLECT', '已满足固定解准入，开始 1 Hz 采集与滤波')
                    else:
                        self.result['accuracy_grade'] = ACCURACY_FLOAT
                        self._warn(
                            '浮动解准入（质量=5）已满足，开始采集。'
                            '本环境进不了固定解（通常是天顶被遮挡、只有低仰角星），'
                            '浮动解高程极差实测约 0.5m，对基准站定初值够用，'
                            '但入库坐标会标注为「浮动解」而非「固定解」')
                        self._stage('COLLECT',
                                    f'浮动解稳定 {FLOAT_HOLD_SECONDS:.0f} 秒，'
                                    f'开始采集（精度等级：浮动，非固定）')
                    continue
                # FIX_WAIT 超时兜底：天线被遮挡时可能永远进不了固定解，
                # 不能无限占串口。超时判失败，并把最近的真实观测值带出去，
                # 否则用户只知道"失败"，不知道是星数不够还是 PDOP 太大。
                if fix_wait_started is None:
                    fix_wait_started = now
                # ---- 注入差分后的 GGA 频率复核（每 FIX_RATE_PROBE_SEC 秒一次）----
                # 「配置成功 + 注入前实测 1 Hz」并不能保证注入后还在出。
                # 2026-11-03 实测：同一份配置，无差分时 1.03 Hz，注入 785 B/s 的
                # RTCM 后掉到 0.15 Hz（GSV/GLL/VTG/RMC 还在抢输出调度）。
                # 不在这里复核的话，现场看到的永远是那句"等待固定解超时"，
                # 而真正的原因（GGA 几乎不出）被埋在 180 秒的沉默里。
                if gga_forwarded_at_probe is None:
                    gga_forwarded_at_probe = now
                    gga_count_at_probe = int(getattr(gga_manager, 'forwarded', 0) or 0)
                elif now - gga_forwarded_at_probe >= FIX_RATE_PROBE_SEC:
                    span = now - gga_forwarded_at_probe
                    grown = (int(getattr(gga_manager, 'forwarded', 0) or 0)
                             - gga_count_at_probe)
                    hz_now = grown / span if span > 0 else 0.0
                    self.result.setdefault('live_gga_rate', []).append(
                        {'t': round(now - fix_wait_started, 1),
                         'hz': round(hz_now, 3)})
                    if hz_now < GGA_MIN_HZ:
                        self._warn(
                            f'注入差分后 GGA 掉到 {hz_now:.2f} Hz（{grown} 条 / '
                            f'{span:.0f}s）。VRS 靠这条位置跟踪测站，频率不足时'
                            f'模糊度永远建不起来——这不是信号问题')
                    gga_forwarded_at_probe = now
                    gga_count_at_probe = int(getattr(gga_manager, 'forwarded', 0) or 0)

                if now - fix_wait_started >= fix_wait_timeout:
                    # ---- 超时后的分岔：能从浮动解降级就降级，不能才失败 ----
                    # 关键区分：**"一直浮动解"和"连浮动解都没有"是两种完全不同的故障。**
                    #   后者是链路的错（RTCM 没进去 / 账号没差分权限 / 挂载点不对），
                    #   等再久也没用，必须失败并提示去查账号；
                    #   前者是天线环境的物理上限（天顶被挡、只剩低仰角星），
                    #   模块已尽最大努力，浮动解的高程极差实测 0.57m，
                    #   给基准站定初值完全够用——这时判失败等于白测一场。
                    # 所以先看这整段窗口里浮动解占比，稳定就降级继续采，不稳定才抛错。
                    stable_float = (float_ok and float_probes > 0
                                    and float_hits >= float_probes * FLOAT_MIGRATE_MIN_HITS_RATIO)
                    if not allow_float and stable_float:
                        self.result['allow_float_collect'] = True
                        self.result['float_downgraded_at'] = round(
                            now - fix_wait_started, 1)
                        logging.info(
                            '[FLOAT-DOWNGRADE] 等待固定解 %.0f 秒未果，但浮动解稳定'
                            '（%d/%d 轮满足准入），降级为浮动解采集；'
                            '本环境质量位恒为 %s，属天线遮挡而非链路故障',
                            fix_wait_timeout, float_hits, float_probes, quality)
                        self._warn(
                            f'等待固定解 {fix_wait_timeout:g} 秒未果。'
                            f'期间浮动解稳定（{float_hits}/{float_probes} 轮满足准入），'
                            f'判定为**天线遮挡**导致无法固定，不是 CORS 链路的错。'
                            f'自动降级为浮动解采集——坐标会标注为「浮动解」，'
                            f'精度约 dm 级（高程），可作基准站初值')
                        self._stage('FIX_WAIT',
                                    '固定解不可得，降级为浮动解采集（天线遮挡）')
                        # 重置窗口给降级后完整时长；计数器也清零，让下一轮统计
                        # 只反映"降级之后"的浮动解质量（全程累计值会把降级前的
                        # 命中算进来，掩埋降级后差分突然断掉的情况）。
                        fix_wait_started = now
                        float_hits = 0
                        float_probes = 0
                        continue
                    if allow_float:
                        # 已降级：不再按"总时长"判死，只在**浮动解持续不达标**时失败。
                        # 降级后的正常状态就是"质量位 5、等待连满 FLOAT_HOLD_SECONDS"，
                        # 拿 fix_wait_timeout 当死线会把正在正常累计的采集误杀。
                        if float_probes > 0 and \
                                float_hits < float_probes * FLOAT_MIGRATE_MIN_HITS_RATIO:
                            raise WriteFailed(
                                'FIX_WAIT',
                                f'已降级为浮动解采集，但浮动解也无法维持准入'
                                f'（{float_hits}/{float_probes} 轮满足）。'
                                f'最后观测：质量={quality}，卫星={num_sats}'
                                f'（需 ≥{FLOAT_MIN_SATS}），PDOP={pdop}'
                                f'（需 ≤{FLOAT_MAX_PDOP}）。'
                                f'差分链路可能已断：检查 CORS 连接是否还在、'
                                f'账号是否被限流（多端同时登录）、挂载点是否为本区域数据源',
                                {'quality': quality, 'num_sats': num_sats, 'pdop': pdop,
                                 'float_hits': float_hits, 'float_probes': float_probes})
                        # 浮动解仍在达标，只是还没连满时长：续窗口继续等
                        fix_wait_started = now
                        float_hits = 0
                        float_probes = 0
                        time.sleep(CORS_POLL_INTERVAL)
                        continue
                    raise WriteFailed(
                        'FIX_WAIT',
                        f'等待固定解超时（{fix_wait_timeout:g} 秒），'
                        f'且浮动解也不稳定（{float_hits}/{float_probes} 轮满足准入），'
                        f'无法降级采集。'
                        f'最后观测：质量={quality}（需 4），卫星={num_sats}（需 ≥{FIX_MIN_SATS}），'
                        f'{"PDOP" if not hdop_fallback else "HDOP"}={pdop if pdop is not None else "?"}'
                        f'（需 ≤{FIX_MAX_PDOP if not hdop_fallback else FIX_MAX_HDOP_FALLBACK}）。'
                        f'质量位一直停在 1/2 说明**差分根本没被模块采用**，按顺序查：'
                        f'① CORS 账号的差分类型/坐标系权限（能连上、能收流，'
                        f'但只给了伪距差分或只允许 CGCS2000 时，表现为永远浮动解）；'
                        f'② 挂载点是否本区域精确数据源（基站级/网络 RTK 精度不一样）；'
                        f'③ 端口与坐标系是否配对（8002=WGS84 / 8001=CGCS2000）。'
                        f'若质量位一直接近 4~5 只是固定不了，那是天线遮挡，'
                        f'不属于本条，需把天线移到天顶开阔处',
                        {'quality': quality, 'num_sats': num_sats, 'pdop': pdop,
                         'hdop_fallback': hdop_fallback,
                         'float_hits': float_hits, 'float_probes': float_probes,
                         'elapsed': round(now - fix_wait_started, 1)})
                self._stage('FIX_WAIT',
                            (f'等待固定解（质量={quality}，卫星={num_sats}，'
                             f'{"PDOP" if not hdop_fallback else "HDOP"}'
                             f'={"?" if pdop is None and (gga or {}).get("hdop") is None else (pdop if pdop is not None else (gga or {}).get("hdop") or (gsa or {}).get("hdop"))}）'
                             if not allow_float else
                             f'浮动解采集中（质量={quality}，卫星={num_sats}，'
                             f'PDOP={pdop if pdop is not None else "?"}）')
                            + (f'，已连续 {fix_since:.0f}/{hold_needed:.0f} 秒'
                               if accepted else '')
                            + f'，{now - fix_wait_started:.0f}/{fix_wait_timeout:g} 秒')
                time.sleep(CORS_POLL_INTERVAL)
                continue

            # ---- 采集阶段：1 Hz 采样 ----
            # 只有拿到"新的合格 GGA"才采一个点（严格 1 Hz，去重靠时间戳）。
            # ⚠️ 判据必须是 `accepted` 而不是 `fixed`：降级到浮动解时 `fixed`
            # 永远为 False，用它当门禁会让整个采集期一个点都采不到。
            # 2026-11-02 真机就踩了这个：降级成功、界面显示"采集中"，
            # 但 5 分钟里 `samples` 恒为 0，最后报"没有拿到任何固定解样本"——
            # 一句误导性的错误，真实原因却是采样门禁写死了 quality==4。
            if accepted and gga and gga.get('lat') is not None:
                ts = now
                if ts - last_gga_time >= 0.9:      # 1 Hz 去重
                    last_gga_time = ts
                    try:
                        from .geo import geodetic_to_ecef
                        h = (gga.get('height') or 0.0)
                        # 大地水准面差距：GGA 海拔 + Sep = 椭球高（§7.1）
                        sep = gga.get('geoid_sep')
                        if sep is None:
                            sep = 0.0
                            self._warn('GGA 无大地水准面差距（Sep），高程精度可能降低约 ±2m')
                        h_ell = h + sep
                        ecef = geodetic_to_ecef(gga['lat'], gga['lon'], h_ell)
                        samples.append(ecef)
                    except Exception as e:         # noqa: BLE001
                        logging.debug(f'坐标换算失败（忽略该样本）：{e}')

            # 3σ 剔除 + 卡尔曼（有足够样本才做）
            if len(samples) >= COLLECT_MIN_SAMPLES:
                kept, kept_idx = reject_outliers_3sigma_xyz(samples, min_samples=OUTLIER_WINDOW)
                rejected = len(samples) - len(kept)
                if kf is None:
                    # 初值 = 前 10 个有效样本均值（§7.4）
                    x0 = sum(s[0] for s in kept[:COLLECT_MIN_SAMPLES]) / COLLECT_MIN_SAMPLES
                    y0 = sum(s[1] for s in kept[:COLLECT_MIN_SAMPLES]) / COLLECT_MIN_SAMPLES
                    z0 = sum(s[2] for s in kept[:COLLECT_MIN_SAMPLES]) / COLLECT_MIN_SAMPLES
                    kf = (Kalman1D(q=KALMAN_Q, r=KALMAN_R, x0=x0, p0=1.0),
                          Kalman1D(q=KALMAN_Q, r=KALMAN_R, x0=y0, p0=1.0),
                          Kalman1D(q=KALMAN_Q, r=KALMAN_R, x0=z0, p0=1.0))
                # **只喂新增样本**（真机踩过）：卡尔曼每个观测只能更新一次。
                # 早前的写法每轮（0.2s）把整个 kept 列表重喂一遍，同一批点被
                # 反复观测几百次，P 被人为压到极小 → σ 假收敛。真机证据：
                # 只采 37 秒（32 点）就判"连续 60 秒收敛"，物理上不可能。
                fed_any = False
                for index in kept_idx:
                    if index < kf_fed_upto:
                        continue
                    sample = samples[index]
                    kf[0].update(sample[0])
                    kf[1].update(sample[1])
                    kf[2].update(sample[2])
                    kf_fed_upto = index + 1
                    fed_any = True
                    # 收敛判据只认**原始合格样本**（见 _cors_converged 的说明）
                    clean_history.append(sample)
                if fed_any:
                    kf_history.append((kf[0].x, kf[1].x, kf[2].x))

            # 收敛判定（§7.5）：连续 60 秒三轴 σ ≤ 2cm（σ 取原始样本，不取滤波输出）
            converged = self._cors_converged(clean_history)

            # 进度显示：σ 必须是原始样本的真实离散度。
            # 早前用 sqrt(Σ kf.variance) 显示，那是滤波器自报的 P，稳态恒为
            # 7.7 mm 左右、与输入噪声完全无关（仿真已证），用户看到的是一个假数字。
            sigma = self._rolling_sigma(clean_history)
            elapsed = now - collect_started
            # 浮动解模式下把采集时长下限抬到 FLOAT_MIN_COLLECT_SECONDS：
            # dm 级噪声要靠更长平均才能把均值压到 cm 级；固定解本来就快，不受影响。
            eff_duration = duration
            if self.result.get('accuracy_grade') == ACCURACY_FLOAT:
                eff_duration = max(duration, FLOAT_MIN_COLLECT_SECONDS)
            pct = int(100 * min(1.0, elapsed / eff_duration))
            live.update({'samples': len(samples), 'rejected': rejected,
                         'sigma': sigma, 'elapsed': round(elapsed, 1),
                         'converged': bool(converged),
                         'accuracy_grade': self.result.get('accuracy_grade'),
                         'duration': eff_duration})
            self._stage('COLLECT',
                        f'采集 {len(samples)} 点（剔除 {rejected}），'
                        f'3D σ {"?" if sigma is None else f"{sigma:.3f}m"}，'
                        f'{elapsed:.0f}/{eff_duration} 秒（{pct}%）'
                        + ('，浮动解' if self.result.get('accuracy_grade') == ACCURACY_FLOAT
                           else ''))

            if converged:
                break
            if elapsed >= eff_duration:
                break

        # ---- 采集结束：判定是否收敛 / 是否到时长 ----
        if not samples:
            grade = self.result.get('accuracy_grade')
            raise WriteFailed(
                'COLLECT',
                f'采集期间没有拿到任何合格样本（准入等级={grade or "固定解"}，'
                f'浮动解降级={bool(self.result.get("allow_float_collect"))}）。'
                f'最后观测：质量={quality}，卫星={num_sats}，PDOP={pdop}。'
                f'若质量位一直是 1/2，说明差分没被模块采用（查账号权限/挂载点/端口）；'
                f'若质量位是 5 但一直没点，检查天线是否移动、GGA 是否还在输出',
                {'quality': quality, 'num_sats': num_sats, 'pdop': pdop,
                 'accuracy_grade': grade})

        kept, _ = reject_outliers_3sigma_xyz(samples, min_samples=OUTLIER_WINDOW)
        stat = Welford3()
        for sx, sy, sz in kept:
            stat.add(sx, sy, sz)
        sigma3d = stat.sigma3d
        converged = self._cors_converged(clean_history)

        result = {
            'samples': len(samples),
            'kept': len(kept),
            'rejected': len(samples) - len(kept),
            'duration_seconds': round(time.time() - collect_started, 1),
            'sigma3d': round(sigma3d, 6),
            # 判定收敛用的 σ（最近 CONVERGE_HOLD_SECONDS 个合格样本）。
            # 与 `sigma3d`（全段）是两个口径：整定段还没收敛时全段 σ 会被前期
            # 抖动拉高，真机上出现过"全段 3.76 cm 但判定收敛（末段 1.94 cm）"，
            # 只看全段会以为判据放水。两个都记，文案里也两个都给。
            'tail_sigma3d': round(self._rolling_sigma(clean_history) or 0.0, 6),
            'converged': converged,
            'hdop_fallback': hdop_fallback,
            'gga_forwarded': getattr(gga_manager, 'forwarded', 0),
            'gga_keepalive': getattr(gga_manager, 'repeated', 0),
            # 精度等级必须按**实际质量位**标注，不能一律写厘米级：
            # 浮动解（质量位 5）的真实精度是 dm 级，写成厘米级等于骗用户，
            # 而且入库后无法区分哪些站坐标可信、哪些需要复测。
            'accuracy_grade': (ACCURACY_GRADE_CENTIMETER
                               if self.result.get('accuracy_grade') == ACCURACY_FIXED
                               else ACCURACY_GRADE_DECIMETER),
            'rtk_quality': self.result.get('accuracy_grade'),
            # GGA 防抖统计：事后判断"这次标定的 VRS 输入有没有被大幅跳变污染"
            # 只能靠这几个数（日志会被滚动走），所以要跟着记录一起落库。
            # 注意语义已经变了：以前是"冻结了几次"，现在是"丢掉了几条跳变行"。
            'gga_guard_used': bool(getattr(gga_manager, 'guard', False)),
            'gga_jump_dropped': int(getattr(gga_manager, 'jump_dropped', 0) or 0),
            'gga_software': bool(getattr(gga_manager, 'software_mode', False)),
            # 原始样本三轴标准差：精度评估的唯一可信口径（附录I 的 raw_sample_std_*）
            'raw_std': {'e': round(stat.x.std, 6), 'n': round(stat.y.std, 6),
                        'u': round(stat.z.std, 6)},
            # 写入值 = 3σ 剔除后**算术平均**（常值量的最优无偏估计）。
            # 卡尔曼只承担「稳定性判据」的角色，不参与取值：q=1e-6 的指数遗忘
            # 对静态点在毫米级上等价于均值，但它会引入模型假设；两者差值记在
            # `kf_ecef` 里，方便事后核对滤波器有没有跑偏。
            'ecef': [round(v, 4) for v in stat.mean],
        }
        if kf is not None:
            result['kf_ecef'] = [round(kf[0].x, 4), round(kf[1].x, 4), round(kf[2].x, 4)]
            result['kf_minus_mean_mm'] = round(
                max(abs(kf[0].x - stat.mean[0]), abs(kf[1].x - stat.mean[1]),
                    abs(kf[2].x - stat.mean[2])) * 1000, 2)
            # 滤波输出三轴标准差：**只做参考**。滤波器自报的 P 收敛后恒在毫米级，
            # 与输入噪声无关，拿它当精度会得到一个永远好看但没意义的数字。
            result['kalman_std'] = {
                'e': round(math.sqrt(max(0.0, kf[0].variance)), 6),
                'n': round(math.sqrt(max(0.0, kf[1].variance)), 6),
                'u': round(math.sqrt(max(0.0, kf[2].variance)), 6),
            }
        if kf_history:
            # 诊断用：滤波输出序列的 σ。恒小于原始样本 σ（平滑所致），
            # 两者的比值就是「拿滤波序列判收敛会把门限放宽多少」的证据。
            fstat = Welford3()
            for point in kf_history[-int(CONVERGE_HOLD_SECONDS):]:
                fstat.add(*point)
            result['filter_sigma3d'] = round(fstat.sigma3d, 6)
        if not converged:
            # 到时长未收敛（§7.6）：默认采用当前值，但必须在记录与提示里如实标注；
            # 用户选了 'fail' 就一个坐标都不写——"宁可不标"是合法诉求，
            # 尤其是他明知道当前环境差、只想拿一次干净结果的时候。
            sigma_text = f'{stat.sigma3d:.3f} m'
            limit = (CONVERGE_SIGMA_FLOAT
                     if self.result.get('accuracy_grade') == ACCURACY_FLOAT
                     else CONVERGE_SIGMA)
            if str(getattr(plan, 'on_slow', 'adopt') or 'adopt') == 'fail':
                raise WriteFailed(
                    'COLLECT',
                    f'采集 {duration} 秒后仍未收敛：3D σ={sigma_text}，'
                    f'门限 {limit:.2f} m。按你选择的「未收敛即失败」，'
                    f'本次没有写入任何坐标（模块已切回标定前的配置）。'
                    f'建议：换更开阔的位置、把采集时长改成 10 分钟，或检查 CORS 数据源精度',
                    {'sigma3d': round(stat.sigma3d, 6), 'samples': len(samples),
                     'duration': duration})
            self._warn(f'采集时长到，三轴 σ 未稳定收敛到 {limit:.2f} m 以内；'
                       f'当前值已采用，精度已在记录中如实标注')
        return result

    @staticmethod
    def _rolling_sigma(clean_history):
        """最近 CONVERGE_HOLD_SECONDS 个合格样本的 3D σ（米）；不足返回 None。"""
        need = int(CONVERGE_HOLD_SECONDS)
        if len(clean_history) < need:
            return None
        from .geo import Welford3
        stat = Welford3()
        for x, y, z in clean_history[-need:]:
            stat.add(x, y, z)
        return stat.sigma3d

    def _cors_converged(self, clean_history):
        """连续 60 秒三轴 σ ≤ 2cm（§7.5 收敛判定）。

        **σ 必须取原始观测样本，不能取滤波输出**（真机 + 仿真双重验证）：
        卡尔曼对白噪声有约 2.3 倍平滑（原始 σ=40 mm → 滤波序列 σ=17.4 mm），
        若拿滤波序列判 `≤2cm`，实际门槛被悄悄放宽到原始 σ≈4.6cm，
        与方案 §7.5 要求的「观测三轴 σ ≤ 2cm」不是一回事。
        另外 `kf.variance`（滤波器自报的不确定度）在 q=1e-6 / r=4e-4 下
        稳态恒为 7.7 mm 左右、与输入噪声完全无关，用它判收敛等于恒真。

        **浮动解必须用独立门限**：浮动解原始 σ 典型在 10~50 cm，拿 2cm 判
        永远不收敛，采集会被拖到超时上限。浮动解的收敛判据是「σ 稳定下来」，
        不是「σ 小到厘米级」——这两件事不能混用同一个数。
        """
        sigma = self._rolling_sigma(clean_history)
        if self.result.get('accuracy_grade') == ACCURACY_FLOAT:
            limit = CONVERGE_SIGMA_FLOAT
        else:
            limit = CONVERGE_SIGMA
        return sigma is not None and sigma <= limit

    def _cors_reconnect(self, client, gga_manager):
        """CORS 断连后的退避重连。返回 `(ok, 本次断连秒数)`。

        `ok=False` 表示累计断连超限（判失败）；`seconds` 是**刚刚结束的这次断连**
        持续了多久，调用方据此决定要不要清空"连续固定解"计时器
        （门限见 `CORS_NET_JITTER_TOLERANCE_SEC`）。

        累计断连（含重连失败）> 5 分钟判失败（§6.5）；每次重连成功都立即发一次 GGA。
        """
        from .cors import CorsError, RECONNECT_BACKOFF, CUMULATIVE_DISCONNECT_LIMIT
        for delay in RECONNECT_BACKOFF:
            self._checkpoint()
            if client.cumulative_disconnect() > CUMULATIVE_DISCONNECT_LIMIT:
                return False, 0.0
            self._stage('FIX_WAIT', f'CORS 断开，{delay:.0f} 秒后重连')
            time.sleep(delay)
            self._checkpoint()
            try:
                client.connect()
                seconds = client.settle_disconnect()
                # 重连后立即发一次 GGA（§6.5）
                gga_manager.reset_rhythm()
                try:
                    client.send_gga_now(gga_manager.force_source)
                except CorsError:
                    continue
                return True, float(seconds or 0.0)
            except CorsError as e:
                if e.kind in ('auth', 'mountpoint', 'sourcetable'):
                    raise WriteFailed('CORS_CONNECT', e.message, {'kind': e.kind})
                continue
        return False, 0.0

    def _cors_state(self, state, message):
        """CORS 客户端的连接状态回调，转成阶段显示。"""
        mapping = {'connected': 'CORS_CONNECT', 'retrying': 'CORS_CONNECT',
                   'waiting_data': 'FIX_WAIT'}
        self._stage(mapping.get(state, 'FIX_WAIT'), message)

    def _inject_rtcm_to_serial(self, data):
        """把 CORS 下发的 RTCM 写进模块串口。**只进串口，绝不 distribute（§6.4）。**

        写超时 0.5s：模块串口写阻塞说明它不收了，立即判失败（§6.4）。
        """
        from .cors import CorsError as _CE
        channel = self.channel
        if channel is None or not channel.is_open:
            raise _CE('write', '串口未打开，无法注入 RTCM', {'error': 'closed'})
        serial = getattr(channel, '_serial', None)
        if serial is None:
            raise _CE('write', '串口句柄缺失，无法注入 RTCM', {'error': 'no_serial'})
        try:
            serial.write(bytes(data))
            serial.flush()
        except Exception as e:                       # noqa: BLE001
            raise _CE('write', f'写模块串口失败：{e}', {'error': str(e)})
        return None

    def _drain_gga(self, seconds):
        """读串口一小段，把 GGA/GSA/RMC/GLL 喂进 channel.cache。"""
        channel = self.channel
        if channel is None:
            return
        # 一次读多行：模块 1 Hz 同时吐 GGA 和 GSA，只读一行会把另一行留在缓冲里
        # 越积越多；且固定解准入要同时看 GGA 的质量位/星数 和 GSA 的 PDOP，
        # 只吸 GGA 不吸 GSA 会让 PDOP 永远停在旧值。
        deadline = time.time() + max(0.0, float(seconds))
        try:
            while time.time() < deadline:
                line = channel.read_line(0.05)
                if not line:
                    continue
                channel.absorb(line)
        except Exception as e:                       # noqa: BLE001
            logging.debug(f'读 NMEA 失败（忽略）：{e}')

    def _assert_link_alive(self, step):
        """串口"读到一半突然没声了"必须立刻失败（方案 §14 #1 / #28）。

        判据刻意区分两种情况：
            从未收到字节 → 不判死（基准站模式本来就可能不输出 NMEA，
                           那种情况留给命令应答超时去兜）；
            收到过再断 3 秒 → 拔线/断电，立刻失败。

        为什么不能等超时：采集阶段是"拿最后一条 GGA 继续算"，断线后
        `channel.cache['gga']` 里还留着断线前那一帧。等满 5 分钟的结果是
        用一帧旧位置算出一组 σ=0 的"完美坐标"，然后写进模块——现场完全看不出问题。
        """
        channel = self.channel
        if channel is None or not hasattr(channel, 'link_lost'):
            return
        if channel.link_lost(LINK_SILENCE_LIMIT):
            detail = getattr(channel, 'link_error', '') or 'read 无异常，但没有任何字节'
            silent = channel.silent_seconds()
            self._warn(f'串口链路中断（{detail}）')
            raise WriteFailed(
                step,
                f'串口连接中断：已连续 {LINK_SILENCE_LIMIT:g} 秒没有收到任何数据'
                f'（最后一次收数据在 {silent:.0f} 秒前）。'
                f'请检查模块供电与 USB 接线后重新标定；'
                f'本次不会拿断线前的位置继续计算',
                {'link_error': str(detail), 'silent_seconds': round(silent or 0, 1)})

    # ---------- 步骤 1'：Survey-in / 停用（还原路径会用，只写不收敛） ----------
    def _svin_config_verified(self, count, acc, distance, step):
        """配 Survey-in 并回读比对；不一致就**重发一次配置再回读**（§14 #6）。

        为什么重试的是"重发配置"而不是"重启模块"：第一次配之前已经为基站模式
        重启过一次（`_step_svin_cfg`），此刻再等 6 秒重启，对"参数到底写进去没有"
        这个问题没有任何帮助 —— 参数在 RAM 里，读回来就知道。

        两次都不符才判失败，并且把差异写进错误详情：次数配错会让进度百分比
        全错（obs/错误的分母），这种错不能靠"看起来在收敛"糊过去。
        """
        last_error = ''
        for attempt in range(2):
            self._checkpoint()
            ok, message, data = self.driver.config_survey_in(
                count, acc, distance, persist=False, restart=False, verify=False)
            if not ok:
                last_error = message
                # 明确拒绝（ERROR,n）重发没意义；只有超时才值得再发一次
                if (data or {}).get('error') != 'timeout':
                    raise WriteFailed(step, f'配置 Survey-in 失败：{message}', data)
            else:
                self._touched = True
                ok2, message2, readback = self.driver.get_svin_config()
                if not ok2:
                    last_error = f'回读 Survey-in 配置失败：{message2}'
                elif readback.get('mode') != 'survey_in':
                    last_error = f'回读模式不是 survey_in（{readback.get("mode")}）'
                elif _to_int(readback.get('count')) != int(count):
                    last_error = (f'回读次数是 {readback.get("count")}，应为 {int(count)}')
                else:
                    return readback
            logging.warning(f'Survey-in 配置第 {attempt + 1} 次回读未通过：{last_error}')

        raise WriteFailed(
            step,
            f'Survey-in 配置没有生效：{last_error}'
            f'（已重发一次配置并再次回读，结果仍然不符）',
            {'error': 'verify_mismatch', 'attempts': 2, 'last_error': last_error})

    def _step_config_svin(self, plan):
        self._checkpoint()
        if plan.svin_mode == 'survey_in':
            cfg = plan.survey_in or {}
            self._stage('WRITE_COORD',
                        f'配置 Survey-in：{cfg.get("count")} 次 / 精度门限 {cfg.get("acc")} m')
            readback = self._svin_config_verified(
                cfg.get('count') or 0, cfg.get('acc') or 0.0,
                cfg.get('distance'), 'WRITE_COORD')
            self._record_step('WRITE_COORD', True, 'Survey-in 配置已写入并回读通过', readback)
            return

        # disabled：还原一个"原本就没有坐标配置"的模块
        self._stage('WRITE_COORD', '停用坐标自动收敛（还原为未配置状态）')
        ok, message, data = self.driver.disable_survey_in(persist=False, restart=False)
        if not ok:
            raise WriteFailed('WRITE_COORD', f'停用坐标配置失败：{message}', data)
        self._touched = True
        self._record_step('WRITE_COORD', True, '已停用坐标配置', data)

    # ---------- 步骤 2：切模式（§8.1 第 2 步 / §8.3 降级判定） ----------
    def _step_switch_mode(self, plan):
        self._checkpoint()
        self._stage('BASE_SETUP', f'切换工作模式为 {plan.mode}')
        # Survey-in 需要在切模式后立即重启让基站模式生效（SVINSTATUS 只在基站模式
        # 输出），而这次重启发生在统一的 _step_save 之前 —— 若不 persist，重启后
        # 内存态被 NVM 里的旧值（rover）覆盖，基站模式就丢了。所以 survey-in 的
        # 切模式必须落 NVM；其余路径仍只改内存态，把"存不存"留给 _step_save 统一决定。
        persist = plan.method == 'survey_in'
        ok, message, data = self.driver.set_mode(
            plan.mode, persist=persist, restart=False, verify=False)
        if ok:
            self._touched = True
            plan.mode_ack = 'ack'
            self.result['mode_ack'] = 'ack'
            self._record_step('BASE_SETUP', True, f'已切换为 {plan.mode}（收到应答）', data)
            return

        if (data or {}).get('error') == 'timeout':
            # §8.3：切到基站后模块会关掉 NMEA，应答是否还输出没有文档保证。
            # 这里**不判失败**，留到重启后（模块一定已在新模式下）二次确认。
            plan.mode_ack = 'deferred'
            self.result['mode_ack'] = 'deferred'
            self._warn('切模式命令没有应答，按方案 §8.3 延后到重启后二次确认')
            self._record_step('BASE_SETUP', True, '已下发，等待重启后确认（无应答）', data)
            return

        raise WriteFailed('BASE_SETUP', f'切换模式失败：{message}', data)

    # ---------- 步骤 3：报文配置（§8.1 第 3 步） ----------
    def _step_setup_messages(self, plan):
        self._checkpoint()
        if plan.kind == 'restore':
            self._restore_messages(plan)
            return
        applied = {}

        # 3.0 用户选了"全部沿用模块默认"：一个字节都不改。
        #     这时 VERIFY_RTCM 只能检查"有没有播坐标"，不能拿写入值去比 ——
        #     因为模块播的可能是它自己存的旧坐标，跟这次写入的不是一回事。
        if plan.coord_msg == 'keep' and plan.msm is None and not plan.antenna:
            self._warn('按选择沿用模块当前的 RTCM 配置（未下发任何报文配置命令）')
            self.result['messages'] = {'applied': applied, 'source': 'module_default'}
            self._record_step('MSG_SETUP', True, '沿用模块默认 RTCM 配置（未改动）',
                              {'source': 'module_default'})
            return

        # 3.1 RTCM 基本配置（MSM 档位）。失败按致命处理：MSM 配不上，
        #     下游流动站拿到的是"只有坐标没有观测值"的基站，等于没用。
        msm = plan.msm
        if msm and self._caps('rtcm_out', True):
            self._stage('MSG_SETUP', f'配置 RTCM 输出（MSM{msm}）')
            ok, message, data = self.driver.config_rtcm(msm_type=msm, persist=False)
            if not ok:
                raise WriteFailed('MSG_SETUP', f'RTCM 配置失败：{message}', data)
            self._touched = True
            applied['msm'] = msm
            # 回读确认 MSM 档位真的变了。真机踩过一次：`PQTMCFGRTCM` 的仰角写成 `-90.0`
            # 会被回 ERROR,1（参数不合法），而只在"应答是 OK"这一层是看不出"配置没生效"的。
            ok2, message2, readback = self.driver.read_rtcm_config()
            if not ok2:
                raise WriteFailed('MSG_SETUP', f'RTCM 配置回读失败：{message2}', readback)
            fields = readback.get('fields') or []
            if not fields or _to_int(fields[0]) != msm:
                raise WriteFailed(
                    'MSG_SETUP',
                    f'RTCM 回读的 MSM 档位是 {fields[0] if fields else "(空)"}，应为 {msm}',
                    readback)

        # 3.2 坐标报文：1005（默认）或 1006（多带一个天线高度）。
        #     1006 自带 1005 的全部字段，所以选 1006 时不必再开 1005；
        #     但也不主动关 1005 —— 关掉一条正在播的报文，比多播一条危险得多
        #     （下游可能只认 1005，关了就整片失锁）。
        if plan.coord_msg == 'keep':
            self._warn('坐标报文按选择沿用模块默认（未下发 1005/1006 速率）')
        else:
            coord_name = f'RTCM3-{plan.coord_msg}'
            self._stage('MSG_SETUP',
                        f'打开 {coord_name}（基准站坐标播发，'
                        f'{"含天线高度" if plan.coord_msg == "1006" else "标准 1005"}）')
            ok, message, data = self.driver.set_message_rate(coord_name, 1)
            if not ok:
                # 1006 在部分固件上不支持：明确告诉用户换 1005，而不是干巴巴一句失败
                hint = ''
                if plan.coord_msg == '1006':
                    hint = '；本型号可能不支持 1006，请改选 1005'
                raise WriteFailed('MSG_SETUP',
                                  f'{coord_name} 输出速率配置失败：{message}{hint}。'
                                  f'缺坐标报文时最终校验无法进行，已中止（未保存）', data)
            self._touched = True
            applied[coord_name] = 1

            got = self._read_rate(coord_name)
            if got != 1:
                raise WriteFailed('MSG_SETUP',
                                  f'{coord_name} 回读速率是 {got}（应为 1），配置没有生效',
                                  {'rate': got})
        self.result['coord_msg'] = plan.coord_msg

        # 3.3 MSM：**逐星座下发**（107X=GPS、108X=GLO、109X=GAL、111X=QZSS、112X=BDS）。
        #     协议规范备注 3 写的"全星座共用一档"容易读成"配一条就全开"，实际不是：
        #     实测只配 107X 时播发里只有 1074，108X~112X 全是 0 —— 下游只能吃 GPS
        #     差分，可用卫星数直接砍半、固定变慢。所以这里逐个打开。
        #     失败只记警告：基站模式默认就播 GPS MSM4，少开星座不影响"能不能用"，
        #     但会让精度变差，所以要在结果里能看出来。
        constellations = getattr(self.driver, 'MSM_CONSTELLATIONS', None) or ('RTCM3-107X',)
        if msm:
            opened, failed = [], []
            for name in constellations:
                ok, message, _data = self.driver.set_message_rate(name, 1)
                if ok:
                    applied[name] = 1
                    opened.append(name)
                else:
                    failed.append(f'{name}（{message}）')
            self.result['cors_msm'] = {'opened': opened, 'failed': failed}
            if failed:
                self._warn('部分星座 MSM 未打开：' + '、'.join(failed)
                           + '；下游可用卫星数会减少')
            # 不单独记一步 MSG_SETUP（阶段序列是契约），信息挂在结果与 applied 里

        # 3.4 天线信息（1033）。必须在 SAVEPAR 之前写，否则存不进去（§8.7）。
        if plan.antenna and self._caps('ant_info'):
            desc = str(plan.antenna.get('desc') or '')[:31]
            self._stage('MSG_SETUP', f'写入天线信息（1033）：{desc}')
            ok, message, _data = self.driver.set_antenna_info(
                desc, plan.antenna.get('setup_id') or 0,
                plan.antenna.get('sn') or '', persist=False)
            if ok:
                applied['RTCM3-1033'] = 1
                rate_ok, rate_message, _ = self.driver.set_message_rate('RTCM3-1033', 1)
                if not rate_ok:
                    self._warn(f'天线信息已写入但 1033 速率没打开：{rate_message}')
            else:
                self._warn(f'天线信息写入失败（不影响基准站功能）：{message}')
        elif plan.antenna:
            self._warn('本型号不支持写天线信息（1033），已跳过')

        self.result['messages'] = {'applied': applied}
        self._record_step('MSG_SETUP', True, f'已配置 {len(applied)} 条报文', self.result['messages'])

    def _restore_messages(self, plan):
        """还原分支：按备份**原样放回**报文配置，不按"标定该配成什么"重配。

        为什么不能复用写入分支（真机踩过）：
            写入分支会强制 `RTCM3-1005=1`、MSM 速率设 1、开 1033 —— 目标是
            "配成一个合格的基准站"。还原的目标恰好相反。第一次真机还原时，
            模块确实回到了移动站，但备份里本来是 `RTCM3-1005=0`，还原后变成了 1：
            界面显示"还原完成"，实际报文比标定前多播了一路。这种"看着成功、
            其实没放回原样"的问题不会报任何错，只能靠严格按备份回放来避免。
        """
        applied = {}
        skipped = []

        # ---- 1. RTCM 七字段原样回放（含星历模式/间隔，按档位重配会悄悄改掉它们）----
        fields = plan.rtcm_fields
        restorer = getattr(self.driver, 'restore_rtcm_config', None)
        if fields and restorer is not None:
            self._stage('MSG_SETUP', f'原样恢复 RTCM 配置：{",".join(fields)}')
            ok, message, data = restorer(fields)
            if ok:
                self._touched = True
                applied['rtcm'] = ','.join(fields)
            else:
                self._warn(f'RTCM 配置原样恢复失败（{message}）；该档配置未还原')
        elif plan.msm and self._caps('rtcm_out', True):
            # 旧备份或当时读不到 → 只剩 MSM 档位可用，退一步按档位重配
            self._stage('MSG_SETUP', f'恢复 RTCM 输出（MSM{plan.msm}；备份无完整字段）')
            ok, message, _data = self.driver.config_rtcm(msm_type=plan.msm, persist=False)
            if ok:
                self._touched = True
                applied['msm'] = plan.msm
            else:
                self._warn(f'RTCM 档位恢复失败（{message}）')
        else:
            skipped.append('RTCM')

        # ---- 2. 报文速率逐条回放 ----
        # 备份里读不到（None）的条目**一律不动**："没读到"不等于"当时是 0"，
        # 按 0 下发反而会把一条原本开着的语句关掉。
        for name, raw in (plan.rates or {}).items():
            rate = _to_int(raw)
            if rate is None:
                continue
            if _is_msm_rate_name(name):
                setter = getattr(self.driver, 'set_msm_rate', None)
                if setter is None:
                    skipped.append(name)
                    continue
                if rate <= 0:
                    # MSM 的 Rate 必须 ≥1，没有"关掉"的写法；保持模块当前值并告知
                    skipped.append(f'{name}(备份为 0)')
                    continue
                offset = _to_int((plan.offsets or {}).get(name)) or 0
                ok, message, _data = setter(rate, offset)
            else:
                ok, message, _data = self.driver.set_message_rate(name, rate)
            if ok:
                applied[name] = rate
            else:
                # 单条速率失败不判整个还原失败：其余配置照样放回去，
                # 这一条模块用自己的默认值，界面提示用户手动核对即可。
                self._warn(f'{name} 速率恢复失败（{message}）；该条保持模块当前值')

        # ---- 3. 天线信息（1033）按备份回放；备份里没有就不碰 ----
        if plan.antenna and self._caps('ant_info'):
            desc = str(plan.antenna.get('desc') or '')[:31]
            self._stage('MSG_SETUP', f'恢复天线信息（1033）：{desc}')
            ok, message, _data = self.driver.set_antenna_info(
                desc, plan.antenna.get('setup_id') or 0,
                plan.antenna.get('sn') or '', persist=False)
            if ok:
                applied['RTCM3-1033'] = 'ant'
            else:
                self._warn(f'天线信息恢复失败（{message}）')

        if skipped:
            self._warn('以下报文未按备份回放（备份里没读到或不可设置）：' + '、'.join(skipped))
        self.result['messages'] = {'applied': applied, 'skipped': skipped,
                                   'source': 'backup'}
        self._record_step('MSG_SETUP', True,
                          f'已按备份回放 {len(applied)} 条报文配置',
                          self.result['messages'])

    # ---------- 步骤 4：保存（§8.1 第 4 步 / §8.4） ----------
    def _step_save(self, plan, attempt=1):
        self._checkpoint()
        # 只测不写是"一个字节都不许进 NVM"的模式（附录D）。正常路径根本不会走到
        # 这里，但把守卫放在发命令之前，等于给未来所有重构上了一道闸：
        # 谁哪天顺手把 SAVE 加进那条分支，这里会立刻炸，而不是悄悄写坏别人的模块。
        if getattr(self, '_readonly', False):
            raise WriteFailed('SAVE', '只测不写模式禁止 SAVEPAR（会写入模块 NVM）')
        self._stage('SAVE', 'SAVEPAR 写入模块 NVM')
        ok, message, data = self.driver.save()
        if not ok:
            raise WriteFailed('SAVE', f'保存参数失败：{message}', data)
        self._saved = True
        self.result['nvm_state'] = NVM_SAVED

        # §8.4：保存后立刻读一次作为"内存态确认"，重启后再读一次作"NVM 态确认"
        ok, message, readback = self.driver.get_svin_config()
        if not ok:
            raise WriteFailed('SAVE', f'保存成功但回读配置失败：{message}', readback)
        if plan.svin_mode == 'fixed':
            verdict = _compare_fixed(readback, plan.arp[0], plan.arp[1], plan.arp[2])
            if not verdict['ok']:
                raise WriteFailed(
                    'SAVE',
                    f'保存后回读与写入值不一致（模式={readback.get("mode")}，'
                    f'最大轴差 {verdict["delta"]["max_axis"]:.4f} m）。'
                    f'配置可能没有真正写进 NVM', readback)
        self._record_step('SAVE', True, '已保存，内存态回读一致',
                          {'readback': readback, 'attempt': attempt})

    # ---------- 步骤 5：重启（§8.1 第 5 步 / §8.5） ----------
    def _step_restart(self, plan, attempt=1):
        self._checkpoint()
        self._stage('RESTART', '软重启（SRR），等模块重新上线')
        ok, message, data = self.driver.reboot()
        if not ok:
            raise WriteFailed('RESTART',
                              f'模块没有重启成功：{message}。配置已保存，'
                              f'但无法确认是否生效；请断电重启后重新校验', data)
        self.result['boot'] = data
        self._record_step('RESTART', True, message, {'data': data, 'attempt': attempt})

    # ---------- 步骤 6：重启后回读（§8.1 第 6 步） ----------
    def _step_verify_mode(self, plan):
        self._checkpoint()
        self._stage('VERIFY_MODE', '重启后回读模式与坐标（NVM 持久化确认）')
        ok, message, mode_data = self.driver.get_mode()
        if not ok:
            raise WriteFailed('VERIFY_MODE', f'重启后读不到工作模式：{message}', mode_data)
        actual = mode_data.get('mode')
        if actual != plan.mode:
            raise WriteFailed('VERIFY_MODE',
                              f'重启后模式是 {actual}，不是 {plan.mode}：'
                              f'配置没有持久化成功（请检查模块 NVM/是否被复位）', mode_data)
        if plan.mode_ack == 'deferred':
            self.result['mode_ack'] = 'confirmed_after_reboot'

        ok, message, readback = self.driver.get_svin_config()
        if not ok:
            raise WriteFailed('VERIFY_MODE', f'重启后读不到坐标配置：{message}', readback)
        if plan.svin_mode == 'fixed':
            verdict = _compare_fixed(readback, plan.arp[0], plan.arp[1], plan.arp[2])
            if not verdict['ok']:
                raise WriteFailed(
                    'VERIFY_MODE',
                    f'重启后坐标与写入值不一致（模式={readback.get("mode")}，'
                    f'最大轴差 {verdict["delta"]["max_axis"]:.4f} m）：'
                    f'配置没有持久化成功', readback)
            self.result['verify']['readback'] = verdict['readback']
            self.result['verify']['delta'] = verdict['delta']
        elif plan.svin_mode == 'survey_in' and readback.get('mode') != 'survey_in':
            raise WriteFailed('VERIFY_MODE',
                              f'重启后坐标模式是 {readback.get("mode")}，不是 survey_in', readback)
        self._record_step('VERIFY_MODE', True,
                          f'重启后模式 {actual}、坐标一致（NVM 持久化确认通过）', readback)

    # ---------- 步骤 7：1005 一致性校验（§8.6） ----------
    # **语义边界（极易被误读）**：1005 播发的就是模块内部存的那份坐标，
    # 「写入 → 重启 → 播发」全程是同一份数据，误差恒等于 1005 的量化误差（≤0.1 mm）。
    # 所以这一步只能证明「写入生效 + NVM 持久化成功 + 播发链路通」，
    # **不能证明坐标的绝对精度**。把它当精度证据属于循环验证。
    # 绝对精度只能靠独立观测：换 CORS 源交叉解算 / 已知控制点比对 / 静态 PPP 后处理。
    def _step_verify_rtcm(self, plan):
        """监听 1005 校验播发坐标；不符时按标定流程**重下发一轮**再复核。

        标定流程（文档「模式 A1 / 步骤 12」）要求：
            「重启后校验 RTCM 输出，不符则重下发 + 保存 + 重启复核」
        原来的实现只判失败不重试。真机上"第一次重启后 1005 还是旧值"最常见的
        两个成因都是**可自愈**的：
          ① 重启时机太早，模块 NVM 还没写完就断电/复位，读回的是上一条；
          ② 报文速率下发成功但重启后没生效（LG290P 上踩过：set_message_rate
             回 OK 而 1005 实际仍是 0）。
        这两种情况重新下发一轮就能好，直接判失败会让用户白等一次完整标定。

        **只重试一轮**（`RTCM_RETRY_LIMIT = 1`）：两次都错说明不是偶发，
        继续重试只会反复写 NVM 磨损闪存，也不会碰巧变对。
        """
        if not plan.verify_rtcm:
            self.result['verdict'] = 'skipped'
            self._record_step('VERIFY_RTCM', True, '跳过（目标不是播发 1005 的基站）', {})
            return

        outcome = self._check_rtcm_once(plan, attempt=1)
        if outcome['ok']:
            return
        if RTCM_RETRY_LIMIT <= 0:
            raise WriteFailed('VERIFY_RTCM', outcome['detail_text'] or outcome['reason'],
                              outcome.get('detail'))

        # ---- 不符：重下发（报文 + 坐标）→ 保存 → 重启 → 复核 ----
        self._warn(f'第 1 次 1005 校验未通过（{outcome["reason"]}），'
                   f'按标定流程重新下发配置并重启复核')
        self._stage('VERIFY_RTCM', '1005 不符：重新下发 → 保存 → 重启 → 复核')
        self._retry_rtcm(plan)
        outcome = self._check_rtcm_once(plan, attempt=2)
        if outcome['ok']:
            self.result['verify']['retried'] = True
            self._warn('重下发后 1005 校验通过。'
                       '注意：这次是**第二轮**才成功，说明第一次配置没有落进 NVM，'
                       '如果这块模块反复出现此现象，请怀疑 NVM 写保护或供电不稳')
            return

        raise WriteFailed(
            'VERIFY_RTCM',
            f'已重下发一轮并重启复核，1005 仍不符（{outcome["reason"]}）。'
            f'两次都失败说明不是偶发：'
            f'① 模块 NVM 可能写保护/损坏，配置存不住；'
            f'② 模块可能被外部工具/另一路连接改回了旧配置；'
            f'③ 供电不足导致重启没有真正发生。'
            f'请断电重启模块，用「还原标定前配置」恢复到标定前状态后重试。'
            f'{outcome["detail_text"]}',
            {'first': self.result['verify'].get('first_attempt'),
             'second': outcome.get('detail')})

    # ---- 1005 校验的单次执行（原 _step_verify_rtcm 的主体）----
    def _check_rtcm_once(self, plan, attempt=1):
        """跑一次「采集 1005 → 比对」。返回 `{ok, verdict, reason, detail_text, detail}`。

        不抛异常（除中断）：判据三档与原来一致 ——
            ≤ RTCM_WARN_LIMIT  → match
            ≤ RTCM_FAIL_LIMIT  → warning（坐标可用，但要核对）
            否则               → 不符，交给调用方决定重试还是失败
        """
        self._checkpoint()
        self._stage('VERIFY_RTCM',
                    f'监听 1005 做权威校验（第 {attempt} 次，最多 {self.rtcm_timeout:.0f} 秒）')
        frames, total = self._collect_1005()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        bucket = self.result['verify']
        bucket['frames_1005'] = len(frames)
        bucket['frames_total'] = total
        bucket['checked_at'] = now
        bucket['attempts'] = attempt

        if not frames:
            want = ('1005 或 1006' if plan.coord_msg == 'keep'
                    else f'RTCM3-{plan.coord_msg}')
            # 一条都没收到 → 重下发能救的是"速率没生效"，救不了"线没接好"。
            # 仍然走一轮重试：第一次重启后模块可能还没开始播发，速率确实可能是 0。
            return {
                'ok': False, 'verdict': 'unknown',
                'reason': f'{self.rtcm_timeout:.0f} 秒内没有收到任何 {want} 报文',
                'detail_text': (f'{self.rtcm_timeout:.0f} 秒内没有收到任何 {want} 报文'
                                f'（共收到 {total} 个 RTCM 帧）：无法确认播发坐标。'
                                f'若重试仍无报文，检查 {want} 速率是否为 1、'
                                f'串口是否只接了 TX'),
                'detail': {'frames': total, 'attempt': attempt},
            }

        last = frames[-1]
        src = f'RTCM3-{last.get("msg_type") or 1005}'
        broadcast = {'x': last['ecef_x'], 'y': last['ecef_y'], 'z': last['ecef_z'],
                     'station_id': last.get('station_id'), 'source': src,
                     'antenna_height': last.get('antenna_height')}
        self.result['broadcast'] = broadcast
        dx = broadcast['x'] - plan.arp[0]
        dy = broadcast['y'] - plan.arp[1]
        dz = broadcast['z'] - plan.arp[2]
        axis = max_axis_delta(broadcast['x'], broadcast['y'], broadcast['z'],
                              plan.arp[0], plan.arp[1], plan.arp[2])
        norm = (dx * dx + dy * dy + dz * dz) ** 0.5
        delta = {'dx': round(dx, 4), 'dy': round(dy, 4), 'dz': round(dz, 4),
                 'max_axis': round(axis, 4), 'norm': round(norm, 4)}
        self.result['delta'] = delta

        if attempt == 1:
            bucket['first_attempt'] = {'axis': round(axis, 4), 'frames': len(frames)}

        if axis <= RTCM_WARN_LIMIT:
            self.result['verdict'] = 'match'
            self._record_step('VERIFY_RTCM', True,
                              f'1005 播发坐标与写入值一致（最大轴差 {axis * 1000:.2f} mm；'
                              f'证明写入与持久化生效，不代表绝对精度）', delta)
            return {'ok': True, 'verdict': 'match', 'reason': '一致',
                    'detail_text': '', 'detail': delta}

        if axis <= RTCM_FAIL_LIMIT:
            self.result['verdict'] = 'warning'
            self._warn(f'1005 播发坐标与写入值差 {axis:.3f} m（5 cm ~ 50 cm）：'
                       f'模块可能做了内部平滑；坐标可用，但请核对')
            self._record_step('VERIFY_RTCM', True, f'有偏差 {axis:.3f} m（按警告处理）', delta)
            return {'ok': True, 'verdict': 'warning', 'reason': f'偏差 {axis:.3f} m',
                    'detail_text': '', 'detail': delta}

        self.result['verdict'] = 'unknown'
        return {
            'ok': False, 'verdict': 'unknown',
            'reason': f'1005 播发坐标与写入值差 {axis:.3f} m（超过 50 cm）',
            'detail_text': (f'写入 {plan.written()}，'
                            f'播发 {broadcast["x"]:.4f}/{broadcast["y"]:.4f}/'
                            f'{broadcast["z"]:.4f}'),
            'detail': delta,
        }

    def _retry_rtcm(self, plan):
        """1005 不符时的重下发：报文 → 坐标 → 保存 → 重启 → 回读模式。

        顺序刻意与主链路一致（除坐标放前面：坐标是幂等的，先写它没有副作用，
        而报文速率必须先于保存，否则 SAVEPAR 存下去的还是没有 1005 的配置）。

        不用 `apply_and_verify`：那套是"发一次命令 + 回读一次"，覆盖不了
        "SAVEPAR + 重启 + NVM 持久化"这条真正会出问题的长链路。
        """
        self._checkpoint()
        # 1) 报文重新下发（只重下发，不动坐标：坐标已由 WRITE_COORD 写进内存）
        self._step_setup_messages(plan)

        # 2) 坐标重写一遍：第一次重启后如果 NVM 存的是旧值，这里再写一次是幂等的
        ok, message, data = self.driver.set_fixed_ecef(
            plan.arp[0], plan.arp[1], plan.arp[2],
            persist=False, restart=False, verify=False)
        if not ok and (data or {}).get('error') != 'timeout':
            raise WriteFailed('VERIFY_RTCM',
                              f'重下发坐标失败：{message}。'
                              f'模块保持第一轮配置，请用「还原标定前配置」恢复', data)
        self._touched = True

        # 3) 保存 + 4) 重启：复用主链路的同名步骤，语义（含回读判据）完全一致。
        #    attempt=2 只影响 steps 记录里的标记，便于事后分辨"这是重试那一轮"。
        self._step_save(plan, attempt=2)
        self._step_restart(plan, attempt=2)
        # 5) 重启后必须重新确认模式 —— 只重跑 1005 不确认模式，可能出现
        #    "模式掉了但 1005 照播"的组合，那组坐标是废的。
        self._step_verify_mode(plan)

    # ---------- 步骤 8：落库 ----------
    def _step_inject(self, plan):
        self._checkpoint()
        record = self.build_record(plan)
        self.result['record'] = record

        if plan.kind == 'restore':
            # 还原**不是一次新的标定成果**，不能往 `calibration_records` 里加一行：
            # 历史坐标列表是给"从这里面挑一个坐标写进去"用的，混进一条
            # "标定前的老坐标"会让用户下次选坐标时拿到一个自己从没标定过的值。
            # 还原这件事本身记在 `calibration_events`（service 落 DONE 事件时写）。
            self._stage('INJECT', '还原不产生新的历史坐标记录（只记事件）')
            self.result['record_id'] = None
            self.result['record_skipped'] = 'restore'
            self._record_step('INJECT', True, '已汇总（还原不写入历史坐标）',
                              {'record_id': None, 'skipped': 'restore'})
            return

        self._stage('INJECT', '标定结果上链（落库 + 事件）')
        record_id = None
        if self._on_record is not None:
            record_id = self._on_record(record)
        self.result['record_id'] = record_id
        self._record_step('INJECT', True,
                          '已写入标定库' if record_id else '已汇总（未落库）',
                          {'record_id': record_id})

    def build_record(self, plan):
        """组装 `calibration_records` 的一行。APC 与 ARP 两套值都存（方案 §8.1.1）。"""
        geodetic = {}
        # 经纬度按**写入值（ARP）**反算：播发出去的就是这组数，页面显示、
        # NTRIP 源列表、下游流动站看到的都是它，按 APC 显示会差一个天线高。
        anchor = plan.arp if plan.arp is not None else plan.apc
        if anchor is not None:
            try:
                lat, lon, height = ecef_to_geodetic(*anchor)
                geodetic = {'lat': lat, 'lon': lon, 'height': height}
            except Exception:                    # noqa: BLE001
                geodetic = {}
        verified = {'match': 1, 'warning': 2, 'skipped': 0}.get(
            self.result.get('verdict') or 'unknown', 0)
        delta = (self.result.get('delta') or {}).get('max_axis')
        survey = self.result.get('survey') or {}
        cors = self.result.get('cors') or {}
        raw_std = cors.get('raw_std') or {}
        kalman_std = cors.get('kalman_std') or {}
        accuracy = None
        samples = 0
        duration = 0
        svin_valid = None
        svin_obs = None
        svin_acc = None
        if plan.method == 'survey_in':
            # Survey-in 的"精度"就是收敛时的 3D 精度门限实测值（亚米级），
            # 与 CORS 的厘米级靠 `method` + 这个数值区分（方案 §0.5.8）。
            accuracy = survey.get('mean_acc')
            samples = survey.get('obs') or 0
            duration = int(survey.get('elapsed_seconds') or 0)
            # 附录B/I：这三个字段只对 Survey-in 有意义，CORS 模式保持 NULL。
            # `valid` 记的是模块自己报的收敛状态（2=已收敛），事后判断
            # "这条坐标是模块自己收敛出来的，还是断点续标复用的旧结果"靠它。
            svin_valid = 2 if survey else None
            svin_obs = survey.get('obs')
            svin_acc = survey.get('mean_acc')
        elif plan.method in ('cors', 'external_rover'):
            # CORS 的"精度"是滤波收敛后的 3D σ（厘米级）。
            # external_rover（只测不写）用的是同一条采集链路，统计口径完全一致。
            accuracy = cors.get('sigma3d')
            samples = cors.get('samples') or 0
            duration = int(cors.get('duration_seconds') or 0)
        module = plan.module or {}
        mount = (plan.cors or {}).get('mountpoint') or ''
        return {
            'module_sn': module.get('sn') or '',
            'module_model': module.get('model_key') or '',
            'firmware': module.get('firmware') or '',
            'method': plan.method,
            'ecef_x': round(plan.arp[0], 4) if plan.arp else None,
            'ecef_y': round(plan.arp[1], 4) if plan.arp else None,
            'ecef_z': round(plan.arp[2], 4) if plan.arp else None,
            'apc_x': round(plan.apc[0], 4) if plan.apc else None,
            'apc_y': round(plan.apc[1], 4) if plan.apc else None,
            'apc_z': round(plan.apc[2], 4) if plan.apc else None,
            'arp_x': round(plan.arp[0], 4) if plan.arp else None,
            'arp_y': round(plan.arp[1], 4) if plan.arp else None,
            'arp_z': round(plan.arp[2], 4) if plan.arp else None,
            # GGA 防抖是否生效 + 丢弃了多少条跳变行：影响"VRS 是怎么漂的"
            'gga_guard_used': 1 if cors.get('gga_guard_used') else 0,
            'gga_jump_dropped': int(cors.get('gga_jump_dropped') or 0),
            # 原始样本三轴 σ：唯一可信的精度口径
            'raw_sample_std_e': raw_std.get('e'),
            'raw_sample_std_n': raw_std.get('n'),
            'raw_sample_std_u': raw_std.get('u'),
            # 滤波输出三轴 σ：只做参考（见 store.py 里 DDL v3 的说明）
            'kalman_std_e': kalman_std.get('e'),
            'kalman_std_n': kalman_std.get('n'),
            'kalman_std_u': kalman_std.get('u'),
            'svin_valid': svin_valid,
            'svin_obs_count': svin_obs,
            'svin_mean_acc': svin_acc,
            'cors_mountpoint_used': mount,
            'accuracy': accuracy,
            'lat': geodetic.get('lat'),
            'lon': geodetic.get('lon'),
            'height': geodetic.get('height'),
            'samples': samples,
            'duration': duration,
            'verified': verified,
            'verify_delta': delta,
            'nvm_state': self.result.get('nvm_state') or '',
            'task_id': self.result.get('task_id') or '',
            'remark': plan.remark,
        }

    # ---------- 内部小工具 ----------
    def _collect_1005(self):
        """读混合流直到收满 `rtcm_samples` 条 1005/1006 或超时。返回 `(frames, 总帧数)`。"""
        channel = self.channel
        if channel is None:
            return [], 0
        frames_seen = channel.counters.get('frames', 0)
        start = time.time()
        deadline = start + max(1.0, self.rtcm_timeout)
        frames = []
        while time.time() < deadline:
            self._checkpoint()
            channel.read_line(0.2)

            total = channel.counters.get('frames', 0)
            if total > frames_seen:
                pool = channel.cache.get('rtcm_frames') or []
                delta = min(total - frames_seen, len(pool))
                for raw in (pool[-delta:] if delta else []):
                    parsed = decode_frame(raw)
                    if parsed is not None:
                        frames.append(parsed)
                frames_seen = total

            elapsed = time.time() - start
            if frames and len(frames) >= self.rtcm_samples and elapsed >= 0.4:
                break
            self._on_stage('VERIFY_RTCM',
                           f'监听坐标报文（1005/1006）：{len(frames)} 条 / {elapsed:.0f} 秒',
                           88 + int(6 * min(1.0, elapsed / max(1.0, self.rtcm_timeout))))
        return frames, channel.counters.get('frames', 0)

    def _drain_svin(self, elapsed):
        """读串口一小段，把 `$PQTMSVINSTATUS` 喂进 `channel.cache['svin']`。

        `survey_status()` 读的是缓存不是实时串口，所以必须有人消费串口字节；
        `read_line` 的 `Channel` 会自动把 SVINSTATUS 行解析进 cache。
        """
        channel = self.channel
        if channel is None:
            return
        try:
            line = channel.read_line(SVIN_POLL_INTERVAL)
            # read_line 只切行返回，不解析旁路语句；必须手动 absorb，
            # 否则 `$PQTMSVINSTATUS` 永远进不了 `channel.cache['svin']`，
            # survey_status() 也就永远读不到收敛状态。
            if line:
                channel.absorb(line)
        except Exception as e:            # noqa: BLE001 - 读串口失败交给下一轮重试
            logging.debug(f'读 Survey-in 状态失败（忽略）：{e}')

    def _read_rate(self, name):
        try:
            ok, _message, data = self.driver.get_message_rate(name)
        except Exception:                        # noqa: BLE001
            return None
        if not ok:
            return None
        fields = data.get('fields') or []
        # 速率是第 2 个字段（MSM 回复多一个偏移时延，用 [-1] 会读成时延）
        try:
            return int(float(fields[1]))
        except (TypeError, ValueError, IndexError):
            return None

    def _caps(self, key, default=False):
        try:
            return bool(self.driver.caps(key, default))
        except Exception:                        # noqa: BLE001
            return default

    def _stage(self, name, message, progress=None):
        self._step = name
        try:
            self._on_stage(name, message, progress)
        except Exception as e:                   # noqa: BLE001 - 回显失败不该拦住写入
            logging.debug(f'阶段回显失败（忽略）：{e}')

    def _record_step(self, stage, ok, message, detail):
        self.result.setdefault('steps', []).append({
            'stage': stage, 'ok': bool(ok), 'message': str(message),
            'at': time.strftime('%H:%M:%S'),
            'detail': _jsonable(detail),
        })

    def _warn(self, message):
        logging.warning(f'写链路：{message}')
        self.result.setdefault('warnings', []).append(str(message))

    @staticmethod
    def _sleep(seconds):
        time.sleep(max(0.0, float(seconds)))


def _compare_fixed(readback, x, y, z):
    """把回读值与写入值比对，返回 `{ok, readback, delta}`（模式必须是 fixed）。"""
    info = {
        'mode': readback.get('mode'),
        'ecef_x': readback.get('ecef_x'),
        'ecef_y': readback.get('ecef_y'),
        'ecef_z': readback.get('ecef_z'),
    }
    if readback.get('mode') != 'fixed':
        return {'ok': False, 'readback': info, 'delta': {'max_axis': float('inf')}}
    delta = max_axis_delta(readback.get('ecef_x') or 0.0, readback.get('ecef_y') or 0.0,
                           readback.get('ecef_z') or 0.0, x, y, z)
    return {'ok': delta <= COORD_TOLERANCE, 'readback': info,
            'delta': {'dx': round((readback.get('ecef_x') or 0.0) - x, 4),
                      'dy': round((readback.get('ecef_y') or 0.0) - y, 4),
                      'dz': round((readback.get('ecef_z') or 0.0) - z, 4),
                      'max_axis': round(delta, 4)}}


def _to_int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _backup_summary(snapshot):
    if not snapshot:
        return None
    return {
        'created_at': snapshot.get('created_at'),
        'mode': (snapshot.get('mode') or {}).get('text'),
        'svin_mode': (snapshot.get('svin') or {}).get('mode'),
        'complete': bool(snapshot.get('ok', True)),
    }


def _jsonable(value):
    """把阶段详情收敛成可 JSON 序列化的形态（state.json 要写它）。"""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ==================== 离线自测：会"掉电丢配置"的假模块 ====================
class _FakeModule:
    """假 LG290P：**内存态与 NVM 态分开存**。

    这是本自测的全部价值所在：
        `mem` 是模块当前运行配置（写命令立刻改它）；
        `nvm` 是断电后能恢复的配置（只有 SAVEPAR 成功才会被 `mem` 覆盖）。
    重启 = `mem = copy(nvm)`。只有这个模型能证明两件事：
        SAVEPAR 失败 → 重启后配置真的会回到旧值（所以必须回写内存态）；
        标定流程真的在"重启后回读"里发现了它（而不是只看了眼 OK 应答）。
    """

    def __init__(self, firmware='LG290P03AANR01A03S'):
        self.firmware = firmware
        self.mem = self._defaults()
        self.nvm = _deep(self.mem)
        # ---- 故障注入口（每个用例只拨一个开关）----
        self.fail_save = 0               # 接下来 N 次 SAVEPAR 回 ERROR,2
        self.fake_save = False           # SAVEPAR 回 OK 但不写 NVM（"假保存"）
        self.ack_mode = True             # False = 模式命令不回应答（§8.3 的固件）
        self.reject_mode = False         # 模式命令回 ERROR,1
        self.fail_svin_write_after = None  # 第 N 次之后的坐标写入一律 ERROR,2
        self.svin_writes = 0
        self.quantize_cm = False         # 坐标按 cm 量化（模拟固件舍入过粗）
        self.broadcast_offset = 0.0      # 1005 播发值相对配置值的系统偏差
        self.publish_1005 = True         # False = 完全不播 1005
        self.ant_info = False            # PQTMCFGANTINF 是否支持（真机为 False）
        self.reboot_pending = False
        self.reboots = 0
        # ---- Survey-in 收敛模拟（阶段 4 自测用）----
        self.svin_obs = 0                # 已输出的观测次数
        self.svin_auto = True            # True = 每次读状态 obs 递增，到 count 变 Valid=2
        self.svin_final_mean = (-2519265.0514, 4849534.9045, 3277834.6432)  # 收敛均值
        self.svin_acc = 1.2              # 收敛时的 3D 精度（模拟）
        self.svin_stall_at = None        # 设成 N 后，obs 到了 N 就不再增长（模拟信号差）

    @staticmethod
    def _defaults():
        """出厂的移动站状态。**报文速率要给真实值，不能空着**：

        真机 `R,RTCM3-1005` 对一个从没标定过的模块回 `OK,RTCM3-1005,0`（不是 ERROR），
        所以"标定前 1005 是关着的"必须能在假模块里被读到 —— 否则还原用例测不出
        "把 1005 关回去"这件事，而它正是真机第一次还原时漏掉的。
        RTCM3-1033 故意不给（真机回 ERROR,1），用来覆盖"备份里没读到就不动"的分支。
        """
        return {
            'mode': 1,
            'svin': {'mode': 0, 'count': 0, 'acc': 0.0,
                     'x': 0.0, 'y': 0.0, 'z': 0.0, 'distance': None},
            'rates': {'GGA': 1, 'RMC': 1, 'GSA': 1, 'GLL': 1,
                      'RTCM3-1005': 0, 'RTCM3-1006': 0, 'RTCM3-107X': 1},
            'rtcm': None,
        }


def _deep(value):
    import copy
    return copy.deepcopy(value)


def _is_msm_rate_name(name):
    """这条语句是不是 MSM 观测值（107x~113x）。它们全星座共用一条命令、且必带 Offset。"""
    return str(name or '').strip().upper().startswith(
        ('RTCM3-107', 'RTCM3-108', 'RTCM3-109', 'RTCM3-111', 'RTCM3-112', 'RTCM3-113'))


def _msm_tail(name):
    """MSM 的查询应答比别的多一个 `<Offset>` 字段（真机：`OK,RTCM3-107X,1,0`）。"""
    return ',0' if _is_msm_rate_name(name) else ''


def _fake_serial_class(module):
    """造一个假串口类，绑定到给定的假模块实例（跨 open/close 存活）。"""
    from .drivers.base import frame as _frame
    from .proto.rtcm import _build_1005_frame

    class _FakeSerial:
        def __init__(self, port=None, baudrate=None, **kwargs):
            self.port = port
            self.baudrate = baudrate or 115200
            self.is_open = True
            self._out = b''
            self._last_rtcm = 0.0
            self._last_svin = 0.0
            self._boot()

        # ---------- 上电 ----------
        def _boot(self):
            if module.reboot_pending:
                # 重启 = 回到 NVM 里的配置（这才是"持久化"的真实语义）
                module.mem = _deep(module.nvm)
                module.reboot_pending = False
                module.reboots += 1
                self._push(_frame(f'PQTMVER,1,MODULE,{module.firmware},2024/05/22,18:09:06'))

        def _push(self, text):
            self._out += text.encode('ascii')

        # ---------- 写：按命令改状态并回一条应答 ----------
        def write(self, data):
            body = bytes(data).decode('ascii', 'replace').strip().lstrip('$').split('*')[0]
            parts = body.split(',')
            name = parts[0].upper()
            reply = self._handle(name, parts, body)
            if reply:
                self._push(_frame(reply))
            return len(data)

        def _handle(self, name, parts, body):
            mem = module.mem
            if name == 'PQTMVERNO':
                return f'PQTMVERNO,{module.firmware},2024/05/22,18:09:06'
            if name == 'PQTMSN':
                return 'PQTMSN,ERROR,3'
            if name == 'PQTMUNIQID':
                return 'PQTMUNIQID,OK,8,00006FE9326C3E41'
            if name == 'PQTMCFGANTINF':
                if not module.ant_info:
                    return 'PQTMCFGANTINF,ERROR,3'
                if len(parts) > 1 and parts[1].upper() == 'R':
                    return 'PQTMCFGANTINF,OK,2RTK-ANT,3,00006FE9326C3E41'
                mem['antenna'] = parts[2:]
                return 'PQTMCFGANTINF,OK'
            if name == 'PQTMCFGRCVRMODE':
                if len(parts) > 1 and parts[1].upper() == 'R':
                    return f'PQTMCFGRCVRMODE,OK,{mem["mode"]}'
                if module.reject_mode:
                    return 'PQTMCFGRCVRMODE,ERROR,1'
                mem['mode'] = int(float(parts[2]))
                return f'PQTMCFGRCVRMODE,OK,{mem["mode"]}' if module.ack_mode else None
            if name == 'PQTMCFGSVIN':
                if len(parts) > 1 and parts[1].upper() == 'R':
                    svin = mem['svin']
                    return ('PQTMCFGSVIN,OK,%d,%d,%.1f,%.4f,%.4f,%.4f' % (
                        svin['mode'], svin['count'], svin['acc'],
                        svin['x'], svin['y'], svin['z']))
                module.svin_writes += 1
                if (module.fail_svin_write_after is not None
                        and module.svin_writes > module.fail_svin_write_after):
                    return 'PQTMCFGSVIN,ERROR,2'
                svin = mem['svin']
                svin['mode'] = int(float(parts[2]))
                svin['count'] = int(float(parts[3]))
                svin['acc'] = float(parts[4])
                coords = [float(v) for v in parts[5:8]]
                if module.quantize_cm:
                    coords = [round(v, 2) for v in coords]
                svin['x'], svin['y'], svin['z'] = coords
                svin['distance'] = float(parts[8]) if len(parts) > 8 else None
                return 'PQTMCFGSVIN,OK'
            if name == 'PQTMCFGRTCM':
                if len(parts) > 1 and parts[1].upper() == 'R':
                    fields = mem.get('rtcm')
                    if not fields:
                        return 'PQTMCFGRTCM,ERROR,1'
                    return 'PQTMCFGRTCM,OK,' + ','.join(str(v) for v in fields)
                mem['rtcm'] = parts[2:]
                return 'PQTMCFGRTCM,OK'
            if name == 'PQTMCFGMSGRATE':
                rates = mem['rates']
                if len(parts) > 1 and parts[1].upper() == 'R':
                    target = parts[2]
                    if target not in rates:
                        return 'PQTMCFGMSGRATE,ERROR,1'
                    return f'PQTMCFGMSGRATE,OK,{target},{rates[target]}' + _msm_tail(target)
                target = parts[2]
                rates[target] = int(float(parts[3]))
                return f'PQTMCFGMSGRATE,OK,{target},{rates[target]}'
            if name == 'PQTMSAVEPAR':
                if module.fail_save > 0:
                    module.fail_save -= 1
                    return 'PQTMSAVEPAR,ERROR,2'
                if module.fake_save:
                    return 'PQTMSAVEPAR,OK'      # 回 OK 但不落 NVM
                module.nvm = _deep(mem)
                return 'PQTMSAVEPAR,OK'
            if name == 'PQTMSRR':
                module.reboot_pending = True     # 真正的 mem←nvm 发生在下一次 open
                return None
            return f'{name},ERROR,3'

        # ---------- 读：先吐应答，再按 50ms 播 1005 / SVINSTATUS ----------
        def read(self, size=256):
            if self._out:
                data, self._out = self._out, b''
                return data
            mem = module.mem
            # Survey-in 收敛状态推流：配了 survey-in 且打开了 PQTMSVINSTATUS 就 1 Hz 推
            if (mem['svin'].get('mode') == 1
                    and mem['rates'].get('PQTMSVINSTATUS') == 1):
                now = time.time()
                if now - self._last_svin >= 0.05:
                    self._last_svin = now
                    return self._svin_frame()
            if (module.publish_1005 and mem['mode'] == 2
                    and mem['rates'].get('RTCM3-1005') == 1):
                now = time.time()
                if now - self._last_rtcm >= 0.05:
                    self._last_rtcm = now
                    svin = mem['svin']
                    offset = module.broadcast_offset
                    return _build_1005_frame(svin['x'] + offset, svin['y'] + offset,
                                             svin['z'] + offset, station_id=4321)
            time.sleep(0.005)
            return b''

        def _svin_frame(self):
            """推一条 `$PQTMSVINSTATUS`，模拟模块自收敛过程。"""
            svin = module.mem['svin']
            count = svin.get('count') or 0
            if module.svin_auto and module.svin_obs < count:
                if module.svin_stall_at is None or module.svin_obs < module.svin_stall_at:
                    module.svin_obs += 1
            obs = module.svin_obs
            valid = 2 if (obs >= count and count > 0) else 1
            x, y, z = module.svin_final_mean
            acc = module.svin_acc
            body = (f'PQTMSVINSTATUS,1,{obs * 1000},{valid},,11,{obs},{count},'
                    f'{x:.4f},{y:.4f},{z:.4f},{acc:.4f}')
            return _frame(body).encode('ascii')

        def flush(self):
            pass

        def reset_input_buffer(self):
            self._out = b''

        @property
        def in_waiting(self):
            return len(self._out)

        def close(self):
            self.is_open = False

    return _FakeSerial


def _selftest():
    """离线跑完整写链路 + 七个故障场景（不需要硬件）。

    跑法：python -m rtkserver.calibration.writer
    """
    import os
    import sys
    import tempfile

    import serial as serial_module

    from . import backup as backup_mod
    from . import store, workspace
    from .drivers.base import Channel
    from .drivers.quectel_lg290p import LG290PDriver

    print('=== writer 自测（假模块：内存态 / NVM 态分离）===')
    original = (workspace.STATE_DIR, store.CALIBRATION_DB, serial_module.Serial)
    tmpdir = tempfile.mkdtemp(prefix='2rtk-writer-')
    workspace.STATE_DIR = tmpdir
    store.CALIBRATION_DB = os.path.join(tmpdir, 'calibration.db')
    store.init_db()

    X, Y, Z = -2519265.0514, 4849534.9045, 3277834.6432
    stages = []

    def run_case(module, plan, backup=None, timeout=3.0, svin_timeout=None):
        del stages[:]
        serial_module.Serial = _fake_serial_class(module)
        channel = Channel(port='FAKE0', baud=115200)
        channel.open()
        driver = LG290PDriver(channel)
        ok, message, info = driver.info()
        assert ok, message
        plan.module = {'sn': info['sn'], 'model_key': info['model_key'],
                       'firmware': info['firmware']}
        records = []
        writer = CoordinateWriter(
            driver, channel=channel,
            on_stage=lambda stage, message, progress=None: stages.append(stage),
            on_record=lambda record: records.append(store.insert_record(record)) or records[-1],
            rtcm_timeout=timeout, rtcm_samples=2, task_id='SELFTEST',
            svin_timeout=svin_timeout)
        try:
            return writer.run(plan, backup), records, driver, channel
        finally:
            channel.close()

    try:
        # ---------- 0. 意图校验：把经纬度当 ECEF、非法模式 ----------
        for bad, why in (
            ({'ecef': (30.5, 114.3, 50.0)}, '经纬度当 ECEF'),
            ({'ecef': (X, Y, Z), 'mode': 'survey'}, '非法模式'),
        ):
            try:
                WritePlan.build(**bad)
                raised = False
            except ValueError:
                raised = True
            assert raised, f'{why} 必须被拒绝'
        print('[0] 意图校验 OK  经纬度当 ECEF / 非法模式 全部拒绝')

        # ---------- 0.5 CORS 意图校验（阶段5）----
        for bad_cors, why_cors in (
            ({'host': '', 'port': 8002, 'mountpoint': 'MP'}, 'CORS 地址为空'),
            ({'host': 'h', 'port': 8002, 'mountpoint': ''}, '挂载点为空'),
            ({'host': 'h', 'port': 0, 'mountpoint': 'MP'}, '端口非法'),
            ({'host': 'h', 'port': 8002, 'mountpoint': 'MP', 'duration': 0}, '采集时长非法'),
        ):
            try:
                WritePlan.build_cors(**bad_cors)
                raised = False
            except ValueError:
                raised = True
            assert raised, f'{why_cors} 必须被拒绝'
        cors_plan = WritePlan.build_cors(host='120.253.239.161', port=8002,
                                         mountpoint='RTCM33_GRCEJ', user='u', password='p',
                                         duration=300)
        assert cors_plan.method == 'cors'
        assert cors_plan.accuracy_grade == ACCURACY_GRADE_CENTIMETER
        assert cors_plan.cors['duration'] == 300 and cors_plan.cors['mountpoint'] == 'RTCM33_GRCEJ'
        assert cors_plan.svin_mode == 'fixed' and cors_plan.mode == 'base'
        print('[0.5] CORS 意图校验 OK  空地址/空挂载点/非法端口/非法时长全部拒绝，厘米级标注')

        # ---------- 1. 正常路径 ----------
        module = _FakeModule()
        plan = WritePlan.build(ecef=(X, Y, Z), method='manual',
                               antenna=None, remark='自测-正常')
        serial_module.Serial = _fake_serial_class(module)
        channel = Channel(port='FAKE0', baud=115200)
        channel.open()
        driver = LG290PDriver(channel)
        driver.info()
        backup = backup_mod.capture(driver, task_id='SELFTEST', port='FAKE0')
        assert backup['ok'] and backup['mode']['code'] == 1, backup
        channel.close()

        result, records, driver, channel = run_case(module, plan, backup)
        assert result['verdict'] == 'match', result
        assert result['nvm_state'] == NVM_SAVED, result
        assert result['mode_ack'] in ('ack', 'confirmed_after_reboot'), result
        assert [step['stage'] for step in result['steps']] == list(STEPS), result['steps']
        assert all(step['ok'] for step in result['steps']), result['steps']
        assert abs(result['delta']['max_axis']) < 1e-4, result['delta']
        # 模块真的切到基站、真的存进 NVM、真的播的是本次写入的坐标
        assert module.mem['mode'] == 2 and module.nvm['mode'] == 2
        assert abs(module.nvm['svin']['x'] - plan.arp[0]) < 1e-4, module.nvm['svin']
        assert abs(module.nvm['svin']['z'] - Z) < 0.05, module.nvm['svin']
        assert records and records[0] > 0, records
        record = store.get_record(records[0])
        assert record['verified'] == 1 and record['nvm_state'] == 'saved', record
        assert abs(record['apc_z'] - Z) < 1e-4 and abs(record['ecef_z'] - plan.arp[2]) < 1e-4, record
        assert record['task_id'] == 'SELFTEST', record
        print(f'[1] 正常路径 OK  8 步全过，1005 偏差 {result["delta"]["max_axis"] * 1000:.3f} mm，'
              f'记录 #{records[0]}')

        # ---------- 2. SAVEPAR 失败 → 自动回写内存态 ----------
        module = _FakeModule()
        module.fail_save = 99
        plan2 = WritePlan.build(ecef=(X, Y, Z), method='manual')
        writer_failed = None
        try:
            run_case(module, plan2, backup)
        except WriteFailed as e:
            writer_failed = e
        assert writer_failed is not None and writer_failed.step == 'SAVE', writer_failed
        assert writer_failed.nvm_state == NVM_REVERTED, writer_failed.nvm_state
        assert TEXT_REVERTED in writer_failed.message, writer_failed.message
        # 关键：内存态回到 rover + 原坐标配置，NVM 从来没被动过
        assert module.mem['mode'] == 1, module.mem
        assert module.mem['svin']['mode'] == 0, module.mem['svin']
        assert module.nvm['mode'] == 1 and module.nvm['svin']['mode'] == 0
        print(f'[2] 保存失败自动回写 OK  nvm={writer_failed.nvm_state}，'
              f'内存已回 rover（{module.mem["svin"]["mode"]}）')

        # ---------- 3. 保存失败且回写也失败 → unknown ----------
        module = _FakeModule()
        module.fail_save = 99
        module.fail_svin_write_after = 1         # 第 2 次写坐标（也就是回写）失败
        try:
            run_case(module, WritePlan.build(ecef=(X, Y, Z)), backup)
            failed = None
        except WriteFailed as e:
            failed = e
        assert failed is not None and failed.nvm_state == NVM_UNKNOWN, failed
        assert TEXT_UNKNOWN in failed.message, failed.message
        print(f'[3] 回写失败 → unknown OK  文案含"断电重启后模块会回到标定前的配置"')

        # ---------- 4. 假保存（回 OK 但没落 NVM）→ 重启后配置回滚被抓住 ----------
        module = _FakeModule()
        module.fake_save = True
        try:
            run_case(module, WritePlan.build(ecef=(X, Y, Z)), backup)
            failed = None
        except WriteFailed as e:
            failed = e
        assert failed is not None, '假保存必须在重启后回读时被抓住'
        assert failed.step == 'VERIFY_MODE', failed.step
        assert failed.nvm_state == NVM_SAVED, failed.nvm_state
        assert '没有持久化' in failed.message, failed.message
        assert module.mem['mode'] == 1, '重启后应回到 NVM 的旧模式'
        print(f'[4] 重启后持久化校验 OK  停在 {failed.step}：{failed.message[:44]}…')

        # ---------- 5. 1005 偏 0.2 m → 警告但通过（verified=2）----------
        module = _FakeModule()
        module.broadcast_offset = 0.2
        result, records, _d, _c = run_case(module, WritePlan.build(ecef=(X, Y, Z)), backup)
        assert result['verdict'] == 'warning', result['verdict']
        assert abs(result['delta']['max_axis'] - 0.2) < 1e-3, result['delta']
        assert store.get_record(records[0])['verified'] == 2
        print(f'[5] 5~50cm 偏差按警告处理 OK  实测 {result["delta"]["max_axis"]:.3f} m，'
              f'verified=2')

        # ---------- 6. 1005 偏 2 m → 重下发一轮 → 复核仍不符 → 判失败 ----------
        # 标定流程要求「不符则重下发 + 保存 + 重启复核」，所以这里不会一次就死：
        # 断言文案里带"已重下发一轮"，证明重试链路真的跑过（不是把重试写成了空转）。
        module = _FakeModule()
        module.broadcast_offset = 2.0
        try:
            run_case(module, WritePlan.build(ecef=(X, Y, Z)), backup)
            failed = None
        except WriteFailed as e:
            failed = e
        assert failed is not None and failed.step == 'VERIFY_RTCM', failed
        assert '50 cm' in failed.message, failed.message
        assert '已重下发一轮' in failed.message, failed.message
        # 两次都错 → 明确指向"不是偶发"的三个真因，不能只丢一句"校验失败"
        for hint in ('NVM', '外部工具', '供电'):
            assert hint in failed.message, (hint, failed.message)
        print(f'[6] 超 50cm 重试一轮后判失败 OK  文案含 NVM/外部改动/供电三条排查')

        # ---------- 7. 切模式无应答 → §8.3 降级，重启后确认 ----------
        module = _FakeModule()
        module.ack_mode = False
        result, _records, _d, _c = run_case(module, WritePlan.build(ecef=(X, Y, Z)), backup)
        assert result['mode_ack'] == 'confirmed_after_reboot', result['mode_ack']
        assert result['verdict'] == 'match'
        print('[7] 切模式无应答降级 OK  重启后二次确认通过（未误判失败）')

        # ---------- 8. 坐标回读差 5 mm → 重试一次仍不过 → 中止 ----------
        module = _FakeModule()
        module.quantize_cm = True                 # 模块把坐标舍到 cm
        try:
            run_case(module, WritePlan.build(ecef=(X, Y, Z)), backup)
            failed = None
        except WriteFailed as e:
            failed = e
        assert failed is not None and failed.step == 'WRITE_COORD', failed
        assert failed.nvm_state == NVM_REVERTED, failed.nvm_state
        print(f'[8] 1mm 门槛生效 OK  {failed.message[:40]}…')

        # ---------- 9. 一条 1005 都没收到 → 重下发一轮 → 仍然没有 → 判失败 ----------
        # 这个场景专门覆盖「速率没生效」：第一次配的报文速率可能被模块吞掉，
        # 重下发一轮是唯一能救回来的机会。救不回来才判失败，
        # **绝不把"没校验"当成"校验通过"**。
        module = _FakeModule()
        module.publish_1005 = False
        try:
            run_case(module, WritePlan.build(ecef=(X, Y, Z)), backup, timeout=1.0)
            failed = None
        except WriteFailed as e:
            failed = e
        assert failed is not None and failed.step == 'VERIFY_RTCM', failed
        assert '没有收到任何 RTCM3-1005' in failed.message, failed.message
        assert '已重下发一轮' in failed.message, failed.message
        print('[9] 无 1005 重试一轮后判失败 OK（不会把"没校验"当成"校验通过"）')

        # ---------- 10. 一键还原：从基站退回移动站，且**不产生历史坐标记录** ----------
        module = _FakeModule()
        snapshot = {'created_at': '2026-11-01 12:00:00', 'ok': True,
                    'module': {'firmware': module.firmware},
                    'mode': {'code': 1, 'text': 'rover'},
                    'svin': {'mode': 'disabled', 'mode_code': 0, 'count': 0,
                             'acc_limit': 0.0, 'ecef_x': 0.0, 'ecef_y': 0.0, 'ecef_z': 0.0},
                    'rtcm': {'msm_type': None}, 'rates': {}}
        restore_plan = WritePlan.from_backup(snapshot)
        assert restore_plan.kind == 'restore' and restore_plan.mode == 'rover'
        result, records, _d, _c = run_case(module, restore_plan, snapshot)
        assert result['verdict'] == 'skipped', result['verdict']
        assert module.mem['mode'] == 1, module.mem
        assert module.nvm['mode'] == 1, '还原也必须保存进 NVM'
        assert 'WRITE_COORD' in [step['stage'] for step in result['steps']]
        assert records == [], f'还原不是新的标定成果，不该写 calibration_records：{records}'
        assert result['record_id'] is None, result['record_id']
        print('[10] 还原 OK  基站 → 移动站、停用坐标配置，未做 1005 校验，未新增历史记录')

        # ---------- 11. 还原保真：RTCM 七字段与报文速率原样放回 ----------
        # 真机暴露过的问题：还原分支照搬"写入分支"，把备份里 1005=0 改成了 1，
        # 界面显示"还原完成"，报文却比标定前多播一路。这一条专门盯住它。
        module = _FakeModule()
        module.mem['mode'] = 2                       # 先摆成"已标定成基站"的样子
        module.mem['rtcm'] = ['4', '0', '-90', '07', '06', '1', '0']
        module.mem['rates'] = {'GGA': 1, 'RTCM3-1005': 1, 'RTCM3-107X': 1}
        module.nvm = _deep(module.mem)
        snapshot = {'created_at': '2026-11-01 12:00:00', 'ok': True,
                    'module': {'firmware': module.firmware},
                    'mode': {'code': 1, 'text': 'rover'},
                    'svin': {'mode': 'disabled', 'mode_code': 0, 'count': 0,
                             'acc_limit': 0.0, 'ecef_x': 0.0, 'ecef_y': 0.0, 'ecef_z': 0.0},
                    'rtcm': {'fields': ['3', '0', '-90', '07', '06', '0', '3600'],
                             'msm_type': 3},
                    'rates': {'GGA': 1, 'RMC': 5, 'RTCM3-1005': 0, 'RTCM3-107X': 2},
                    'offsets': {'RTCM3-107X': 0}}
        restore_plan = WritePlan.from_backup(snapshot)
        assert restore_plan.rtcm_fields == ['3', '0', '-90', '07', '06', '0', '3600'], \
            restore_plan.rtcm_fields
        assert restore_plan.rates.get('RTCM3-1005') == 0, restore_plan.rates
        result, records, _d, _c = run_case(module, restore_plan, snapshot)
        assert records == [], records
        # 七字段原样放回：星历模式 0 / 间隔 3600 都保住了（config_rtcm 会写成 1/0）
        assert module.mem['rtcm'] == ['3', '0', '-90', '07', '06', '0', '3600'], module.mem['rtcm']
        # 1005 保持备份里的 0：还原不是"把模块配成一个合格基准站"
        assert module.mem['rates'].get('RTCM3-1005') == 0, module.mem['rates']
        assert module.mem['rates'].get('RMC') == 5, module.mem['rates']
        assert module.mem['rates'].get('RTCM3-107X') == 2, module.mem['rates']
        assert module.nvm['rates'].get('RTCM3-1005') == 0, '还原后的速率也要存进 NVM'
        print('[11] 还原保真 OK  RTCM 七字段原样放回，1005 保持 0（未被强制打开）')

        # ---------- 12. Survey-in 三档预设（方案 P3，值与官方默认值不同） ----------
        assert SURVEY_PROFILES['fast'] == {'count': 600, 'acc': 3.0,
                                           'label': '快速（临时建站）', 'grade': 'sub_meter'}
        assert SURVEY_PROFILES['standard']['count'] == 1800
        assert SURVEY_PROFILES['standard']['acc'] == 1.5
        assert SURVEY_PROFILES['high']['count'] == 3600
        assert SURVEY_PROFILES['high']['acc'] == 0.8
        for bad in ('ultra', 'HIGH-ACC', ' '):
            try:
                WritePlan.build_survey_in(profile=bad)
                raised = False
            except ValueError:
                raised = True
            assert raised, f'非法档位 {bad!r} 必须被拒绝'
        # 空值（None / 空串）回落到默认 standard 是合理行为
        assert WritePlan.build_survey_in(profile=None).survey_profile == 'standard'
        assert WritePlan.build_survey_in(profile='').survey_profile == 'standard'
        p = WritePlan.build_survey_in(profile='standard')
        assert p.method == 'survey_in' and p.svin_mode == 'survey_in'
        assert p.survey_in['count'] == 1800 and p.survey_in['acc'] == 1.5
        assert p.accuracy_grade == 'sub_meter', 'Survey-in 必须标亚米级'
        print('[12] Survey-in 三档预设 OK  600/3.0、1800/1.5、3600/0.8，亚米级标注')

        # ---------- 13. Survey-in 完整链路：收敛 → 自动写坐标 → 切基站 → 校验 ----------
        module = _FakeModule()
        # 用小 count 加速自测：三档预设已在 [12] 单独锁住，这里只验证链路复用
        plan = WritePlan.build_survey_in(profile='standard')
        plan.survey_in = {'count': 8, 'acc': 1.5, 'distance': None}  # 8 次≈0.4s
        module.svin_final_mean = (X, Y, Z)
        module.svin_acc = 1.2
        result, records, _d, _c = run_case(module, plan, backup)
        assert result['verdict'] == 'match', result
        assert result['nvm_state'] == NVM_SAVED, result
        # 步骤顺序（任务书 P3）：BASE_SETUP → SVIN_CFG → SVIN_WAIT → WRITE_COORD → …
        order = [s['stage'] for s in result['steps']]
        assert order[0] == 'BASE_SETUP' and order[1] == 'SVIN_CFG', order
        assert order[2] == 'SVIN_WAIT' and order[3] == 'WRITE_COORD', order
        assert order[-1] == 'INJECT', order
        assert all(s['ok'] for s in result['steps']), result['steps']
        # 收敛均值被写成固定坐标并播发一致
        sv = result.get('survey') or {}
        assert sv.get('obs') == 8 and sv.get('mean_acc') == 1.2, sv
        assert abs(module.nvm['svin']['x'] - X) < 1e-3, module.nvm['svin']
        assert module.mem['mode'] == 2 and module.nvm['mode'] == 2
        # 记录：method=survey_in、accuracy 存收敛精度、samples 存观测数
        assert records and records[0] > 0, records
        rec = store.get_record(records[0])
        assert rec['method'] == 'survey_in', rec
        assert rec['accuracy'] == 1.2 and rec['samples'] == 8, rec
        assert rec['verified'] == 1, rec
        print(f'[13] Survey-in 完整链路 OK  收敛 8 次 → 自动写入 → 切基站 → 1005 一致'
              f'（记录 method=survey_in）')

        # ---------- 14. Survey-in 中途手动终止 → 用当前均值继续写入 ----------
        module = _FakeModule()
        plan = WritePlan.build_survey_in(profile='standard')
        plan.survey_in = {'count': 1000, 'acc': 1.5, 'distance': None}  # 大 count，逼它停在半路
        module.svin_final_mean = (X, Y, Z)
        module.svin_stall_at = 5       # obs 到 5 就不再增长（永远不收敛）
        # 用一个 checkpoint 在读到 5 次后抛 TaskAborted，模拟用户手动终止
        class _FakeAbort(Exception):
            evicted = False
        seen = {'n': 0, 'aborted': False}
        def abort_checkpoint():
            seen['n'] += 1
            # 只在 SVIN_WAIT 里终止一次；终止后 SVIN_WAIT 会吞掉它并继续写入，
            # 后续步骤的 _checkpoint 不应再抛，否则"用当前均值继续写入"就断了。
            if seen['n'] >= 20 and not seen['aborted']:
                seen['aborted'] = True
                raise _FakeAbort('用户手动终止收敛')
        serial_module.Serial = _fake_serial_class(module)
        channel = Channel(port='FAKE0', baud=115200)
        channel.open()
        driver = LG290PDriver(channel)
        driver.info()
        plan.module = {'sn': 'SN', 'model_key': 'LG290P', 'firmware': module.firmware}
        records = []
        writer = CoordinateWriter(
            driver, channel=channel,
            on_stage=lambda *a, **k: None,
            checkpoint=abort_checkpoint,
            on_record=lambda r: records.append(store.insert_record(r)) or records[-1],
            rtcm_timeout=2.0, rtcm_samples=2, task_id='SELFTEST',
            svin_timeout=30.0)
        try:
            result = writer.run(plan, backup)
        finally:
            channel.close()
        sv = result.get('survey') or {}
        assert sv.get('terminated') is True, sv
        assert sv.get('obs') >= 1, '终止时至少要有 1 次观测'
        assert result['verdict'] == 'match', result
        # 用当前（部分）均值继续写入了固定坐标，且切了基站
        assert module.mem['mode'] == 2, module.mem
        print(f'[14] 手动终止取当前均值 OK  终止于 obs={sv.get("obs")}，'
              f'仍完成写入并切基站')

        # ---------- 15. Survey-in 超时兜底 → 判失败（信号差不能无限占串口） ----------
        module = _FakeModule()
        plan = WritePlan.build_survey_in(profile='standard')
        plan.survey_in = {'count': 10000, 'acc': 0.1, 'distance': None}  # 不可能收敛
        module.svin_stall_at = 1        # obs 停在 1，永远不收敛
        module.svin_final_mean = (X, Y, Z)
        try:
            run_case(module, plan, backup, svin_timeout=0.5)
            failed = None
        except WriteFailed as e:
            failed = e
        assert failed is not None and failed.step == 'SVIN_WAIT', failed
        assert '未收敛' in failed.message, failed.message
        print(f'[15] Survey-in 超时兜底 OK  {failed.message[:40]}…')

        # ---------- 16. CORS 采集滤波收敛（阶段5 离线，假 Caster 不碰网络） ----------
        # 目的：不连真网络，用假 CORS 客户端 + 假串口固定解数据，验证
        #   固定解准入（质量=4/星数≥12/PDOP≤2.0）→ 1Hz 采样 → 3σ 剔除 → 卡尔曼
        #   → 收敛判定（三轴 σ≤2cm）整条链路能收敛出一个合法 ECEF。
        from . import cors as cors_mod
        from .proto import nmea as nmea_mod

        # 真实 ECEF 对应的经纬高（geodetic_to_ecef 的反变换，用于造固定解 GGA）
        lat0, lon0, h0 = ecef_to_geodetic(X, Y, Z)
        geoid_sep = -9.5          # 大地水准面差距，让 h = h0 - sep（§7.1 里 h+sep 还原椭球高）
        h_msl = h0 - geoid_sep    # GGA 里的海拔（MSL）

        # 造一个会推 GGA/GSA 的假串口：1 Hz 节奏，位置带厘米级抖动。
        # quality/pdop 可调，用来覆盖「永远进不了固定解」的 FIX_WAIT 超时分支。
        class _FixSerial:
            def __init__(self, quality=4, pdop=1.0, sats=14):
                self._out = b''
                self._last = 0.0
                self.is_open = True
                self._rng = 0          # 伪随机种子（确定性抖动，避免测试偶发）
                self.quality = quality
                self.pdop = pdop
                self.sats = sats
            def write(self, data):
                return len(data)
            def flush(self):
                pass
            def reset_input_buffer(self):
                self._out = b''
            @property
            def in_waiting(self):
                return len(self._out)
            def _next(self):
                # 确定性的厘米级抖动：让 3σ 剔除 + 卡尔曼有真实噪声可滤
                self._rng = (self._rng * 1103515245 + 12345) & 0x7fffffff
                jitter = ((self._rng % 2000) - 1000) / 100000.0   # ±1 cm
                return jitter
            def read(self, size=256):
                if self._out:
                    data, self._out = self._out, b''
                    return data
                now = time.time()
                if now - self._last >= 0.05:
                    self._last = now
                    jx, jy, jz = self._next(), self._next(), self._next()
                    gga = nmea_mod.build_gga(
                        lat0, lon0, h_msl + jz, utc=None, quality=self.quality,
                        num_sats=self.sats, hdop=0.6, geoid_sep=geoid_sep)
                    # GSA：PDOP 必须 ≤2.0，12 颗卫星（GSA 最多 12 颗）且含 PDOP 字段
                    gsa = nmea_mod.frame(
                        'GPGSA,A,3,01,02,03,04,05,06,07,08,09,10,11,12,'
                        f'{self.pdop:.1f},0.6,1.2')
                    return (gga + '\r\n' + gsa + '\r\n').encode('ascii')
                time.sleep(0.002)
                return b''

        # 假 CorsClient：只满足 _step_cors_fix_and_collect 用到的接口，不碰 socket
        class _FakeCorsClient:
            def __init__(self):
                self.gga_sent = []
                self._gga_source = None
            def pump(self, gga_source):
                self._gga_source = gga_source
                has_data, payload = gga_source()
                if has_data:
                    self.gga_sent.append(payload)
                return True
            def connect(self):
                return True
            def close(self):
                pass
            def send_gga_now(self, gga_source):
                has_data, payload = gga_source()
                if has_data:
                    self.gga_sent.append(payload)
                return True
            def cumulative_disconnect(self):
                return 0.0
            def settle_disconnect(self):
                pass

        fix_serial = _FixSerial()
        ch = Channel(port='FIX0', baud=115200, serial_instance=fix_serial)
        driver = LG290PDriver(ch)
        fake_client = _FakeCorsClient()
        writer = CoordinateWriter(driver, channel=ch,
                                  on_stage=lambda *a, **k: None,
                                  on_record=lambda r: None,
                                  task_id='SELFTEST')

        # 缩短固定解准入时长 + 采集时长，让离线自测秒级跑完（不改判据本身）。
        # duration 给到 3 秒：固定解 0.5s 准入 + 2.5s 采样，能采到 3~4 个点，
        # 足够验证「采样→3σ→卡尔曼→σ≤2cm」；收敛(需 60 点)在 [17] 单独单测。
        saved_hold = FIX_HOLD_SECONDS
        _this_mod = sys.modules[__name__]
        _this_mod.FIX_HOLD_SECONDS = 0.5
        try:
            collected = writer._step_cors_fix_and_collect(
                WritePlan.build_cors(host='h', port=8002, mountpoint='MP'),
                fake_client, cors_mod.GgaManager(ch), duration=3.0)
        finally:
            _this_mod.FIX_HOLD_SECONDS = saved_hold

        assert collected['samples'] > 0, collected
        # 固定解准入走的是 GSA 的 PDOP（≤2.0），不是 HDOP 降级（§7.2 的正确路径）
        assert collected['hdop_fallback'] is False, collected
        # 滤波收敛出的 ECEF 必须落在真实值附近（厘米级）
        ex, ey, ez = collected['ecef']
        assert abs(ex - X) < 0.05 and abs(ey - Y) < 0.05 and abs(ez - Z) < 0.05, \
            (collected['ecef'], (X, Y, Z))
        # 3D σ 必须 ≤ 2cm（卡尔曼收敛的直接体现）
        assert collected['sigma3d'] <= CONVERGE_SIGMA, collected['sigma3d']
        # 固定解期间，GGA 上报节奏确实在跑（VRS 靠它认位置）
        assert fake_client.gga_sent, '采集期间应持续上报 GGA'
        print(f'[16] CORS 采集滤波 OK  {collected["samples"]} 点，PDOP 准入（非 HDOP 降级），'
              f'ECEf 偏差 {max(abs(ex-X), abs(ey-Y), abs(ez-Z))*100:.1f} cm，'
              f'3D σ={collected["sigma3d"]*100:.1f} cm')

        # ---------- 17. CORS 收敛判定单测（连续 60s 三轴 σ≤2cm） ----------
        # 判据只吃「3σ 剔除后的原始样本」：需要 ≥60 个样本、且窗口 3D σ≤2cm。
        w = writer
        flat = [(X, Y, Z)] * 60
        assert w._cors_converged(flat) is True, '60 个零散布样本应收敛'
        assert w._cors_converged(flat[:59]) is False, '59 个不够 60 秒'
        # 散布 > 2cm 的样本即使满 60 个也不该收敛
        scattered = [(X + (i % 7) * 0.01, Y, Z) for i in range(60)]
        assert w._cors_converged(scattered) is False, 'σ>2cm 不得判收敛'
        # 回归：滤波会把噪声压掉约 2.3 倍。以前拿「滤波序列 σ」判收敛，
        # 原始 σ=40mm 时滤波后只有 17.4mm，会被误判成「≤2cm 收敛」。
        # 现在判据只看原始样本，40mm 必须判不收敛。
        noisy = []
        for i in range(60):
            noisy.append((X + ((i % 6) - 2.5) * 0.016,
                          Y + ((i % 5) - 2.0) * 0.016,
                          Z + ((i % 7) - 3.0) * 0.016))
        raw_sigma = w._rolling_sigma(noisy)
        assert raw_sigma is not None and raw_sigma > CONVERGE_SIGMA, raw_sigma
        assert w._cors_converged(noisy) is False, \
            f'原始 σ={raw_sigma*1000:.1f}mm > 20mm，不得因滤波平滑而判收敛'
        print(f'[17] CORS 收敛判定 OK  60 点 σ≈0 收敛 / 59 点不足 / '
              f'原始 σ={raw_sigma*1000:.0f}mm 不收敛（不取滤波序列）')

        # ---------- 18. FIX_WAIT 超时兜底（天线被遮挡时不能无限占串口） ----------
        # 假串口只给单点解（质量=1），永远进不了固定解；fix_wait_timeout=0.5 必须
        # 判失败，并且把最后的真实观测值（质量/星数/PDOP）带进错误消息里，
        # 否则用户只知道"失败"，不知道是星数不够还是 PDOP 太大。
        never = _FixSerial(quality=1, pdop=1.0, sats=9)
        ch2 = Channel(port='FIX1', baud=115200, serial_instance=never)
        drv2 = LG290PDriver(ch2)
        w2 = CoordinateWriter(drv2, channel=ch2, on_stage=lambda *a, **k: None,
                              on_record=lambda r: None, task_id='SELFTEST',
                              fix_wait_timeout=0.5)
        try:
            w2._step_cors_fix_and_collect(
                WritePlan.build_cors(host='h', port=8002, mountpoint='MP'),
                _FakeCorsClient(), cors_mod.GgaManager(ch2), duration=1.0)
            failed = None
        except WriteFailed as e:
            failed = e
        assert failed is not None and failed.step == 'FIX_WAIT', failed
        assert '质量=1' in failed.message and '卫星=9' in failed.message, failed.message
        assert failed.detail.get('quality') == 1 and failed.detail.get('num_sats') == 9, \
            failed.detail
        print(f'[18] FIX_WAIT 超时兜底 OK  {failed.message[:52]}…')

        # ---------- 18.5 浮动解降级采集（天线遮挡环境的保底路径） ----------
        # 场景：模块稳定报质量位 5（浮动），永远进不了 4。
        # 这是天顶被建筑/窗框遮挡的典型表现——物理上限，等多久都不会固定。
        # 期望行为：FIX_WAIT 超时后自动降级、**真的采到点**、精度标为 dm 级。
        #
        # 回归背景（2026-11-02 真机）：降级判定生效了、界面显示"采集中"，
        # 但采样门禁还写着 `if fixed and ...`，浮动解下 fixed 永远 False，
        # 5 分钟一个点都没采到，最后报"没有拿到任何固定解样本"——
        # 一句把根因完全指错方向的错误。这一条专门盯住采样门禁。
        floaty = _FixSerial(quality=5, pdop=1.5, sats=26)
        ch5 = Channel(port='FIX5', baud=115200, serial_instance=floaty)
        drv5 = LG290PDriver(ch5)
        w5 = CoordinateWriter(drv5, channel=ch5, on_stage=lambda *a, **k: None,
                              on_record=lambda r: None, task_id='SELFTEST',
                              fix_wait_timeout=0.6)
        res5 = w5._step_cors_fix_and_collect(
            WritePlan.build_cors(host='h', port=8002, mountpoint='MP'),
            _FakeCorsClient(), cors_mod.GgaManager(ch5), duration=2.0)
        assert res5['samples'] > 0, f'浮动解降级后必须采到点，实际 {res5["samples"]}'
        assert res5['accuracy_grade'] == ACCURACY_GRADE_DECIMETER, \
            f'浮动解必须标为 dm 级，实际 {res5["accuracy_grade"]}'
        assert res5['rtk_quality'] == ACCURACY_FLOAT, res5['rtk_quality']
        # 收敛门限也要跟着换：浮动解 σ 常 >2cm，用 2cm 会永远不收敛
        assert CONVERGE_SIGMA_FLOAT > CONVERGE_SIGMA
        print(f'[18.5] 浮动解降级采集 OK  采到 {res5["samples"]} 点，'
              f'精度标为 {res5["accuracy_grade"]}（质量位 5）')

        # ---------- 19. GGA_WAIT：RMC 先到也不能退化成软件生成 ----------
        # 真机 rover 模式下 RMC 比 GGA 早 1~2 秒到。旧实现"一见 RMC 就 break"，
        # 把本来有的原生 GGA 判成没有，精度从厘米级掉到米级。这一条专门盯住它。
        class _RmcFirstSerial:
            """前 0.4 秒只给 RMC，之后给 GGA（模拟真机的到达顺序）。"""
            def __init__(self):
                self.is_open = True
                self.t0 = None
                self._last = 0.0
            def write(self, data):
                return len(data)
            def flush(self):
                pass
            def reset_input_buffer(self):
                pass
            @property
            def in_waiting(self):
                return 0
            def read(self, size=256):
                now = time.time()
                if self.t0 is None:
                    self.t0 = now
                if now - self._last < 0.05:
                    time.sleep(0.002)
                    return b''
                self._last = now
                rmc = nmea_mod.frame(
                    'GPRMC,025519.00,A,3112.5270,N,11727.0745,E,0.02,0.0,021126,,,A')
                if now - self.t0 < 0.4:
                    return (rmc + '\r\n').encode('ascii')
                gga = nmea_mod.build_gga(lat0, lon0, h_msl, quality=4, num_sats=14,
                                         hdop=0.6, geoid_sep=geoid_sep,
                                         utc=f'0255{int((now - self.t0) * 10) % 60:02d}.00')
                return (rmc + '\r\n' + gga + '\r\n').encode('ascii')

        ch3 = Channel(port='FIX2', baud=115200, serial_instance=_RmcFirstSerial())
        drv3 = LG290PDriver(ch3)
        w3 = CoordinateWriter(drv3, channel=ch3, on_stage=lambda *a, **k: None,
                              on_record=lambda r: None, task_id='SELFTEST')
        mgr3 = w3._step_cors_gga_wait(WritePlan.build_cors(host='h', port=8002,
                                                           mountpoint='MP'))
        has_data, payload = mgr3.source()
        assert has_data and payload, payload
        assert mgr3.software_mode is False, '有原生 GGA 时不得走软件生成'
        assert b',4,' in payload, payload          # 原生质量位 4 必须原样透传
        print(f'[19] GGA_WAIT 不误退化 OK  RMC 先到仍等到原生 GGA（质量位 4 透传）')

        # ---------- 20. 卡尔曼不得重复观测同一样本（防"假收敛"） ----------
        # 真机踩过：旧写法每轮把整个 kept 列表重喂一遍，32 个点被观测了几百次，
        # P 被压到极小，只采 37 秒就报"连续 60 秒收敛"。物理上不可能。
        # 这里用 5 秒采集：样本数 < 60，**必须**判不收敛。
        ch4 = Channel(port='FIX3', baud=115200, serial_instance=_FixSerial())
        drv4 = LG290PDriver(ch4)
        w4 = CoordinateWriter(drv4, channel=ch4, on_stage=lambda *a, **k: None,
                              on_record=lambda r: None, task_id='SELFTEST')
        _this_mod.FIX_HOLD_SECONDS = 0.3
        try:
            short_run = w4._step_cors_fix_and_collect(
                WritePlan.build_cors(host='h', port=8002, mountpoint='MP'),
                _FakeCorsClient(), cors_mod.GgaManager(ch4), duration=5.0)
        finally:
            _this_mod.FIX_HOLD_SECONDS = saved_hold
        assert short_run['samples'] < 60, short_run
        assert short_run['converged'] is False, \
            f'不足 60 个样本不可能"连续 60 秒收敛"：{short_run}'
        print(f'[20] 卡尔曼不重复观测 OK  {short_run["samples"]} 点/'
              f'{short_run["duration_seconds"]:.0f} 秒 → 判不收敛（未假收敛）')

        # ---------- 21. A1 复用历史坐标：整段跳过 CORS 测量 ----------
        # 标定流程 A1 的分支：库里已有该天线的坐标、天线又没搬过 →
        # 不该再连一次 CORS 去测一遍（白占账号、白等 5 分钟），
        # 直接拿历史坐标走 WRITE_COORD → … → VERIFY_RTCM。
        # 这条自测的核心是证明「真的没连网」：把 fake Serial 换成一个
        # **一旦被用来读流动站数据就炸**的实现是没必要的，
        # 直接断言 stages 里根本不出现任何 CORS 采集相关阶段即可。
        reuse_plan = WritePlan.build_cors(
            host='', port=0, mountpoint='',      # 复用分支允许空（不连网）
            reuse_arp=(X, Y, Z))
        assert reuse_plan.cors['reuse'] is True, reuse_plan.cors
        assert reuse_plan.arp == (X, Y, Z), reuse_plan.arp
        assert reuse_plan.method == 'cors' and reuse_plan.mode == 'base'
        assert reuse_plan.svin_mode == 'fixed'
        # 必须给用户一句明确的"前提"，否则他会以为复用坐标永远安全
        assert any('没有被移动过' in w for w in reuse_plan.warnings), reuse_plan.warnings

        module = _FakeModule()
        result, records, _drv, _ch = run_case(module, reuse_plan, backup)
        assert result['cors'].get('reused') is True, result['cors']
        assert result['cors']['samples'] == 0, result['cors']
        assert result['cors']['ecef'] == [X, Y, Z], result['cors']
        # 关键：这 8 步必须一步不少地跑完 —— 复用坐标只是省掉"测量"，
        # 写入/NVM/1005 校验一个都不能省，否则"复用"就成了"不校验"。
        # 注意 `STEPS` 前面会多一条 `COLLECT`（专门用来在页面上说明
        # "本次跳过测量"，让用户看得到真相而不是一片空白），所以这里按
        # **子序列** 比对：8 步必须原样、按序出现，一条不少。
        got = [step['stage'] for step in result['steps']]
        assert got[:1] == ['COLLECT'], got
        assert got[1:] == list(STEPS), got
        assert all(step['ok'] for step in result['steps']), result['steps']
        assert result['verdict'] == 'match', result['verdict']
        assert result['nvm_state'] == NVM_SAVED, result['nvm_state']
        # 采集相关的阶段一个都不许出现（不连 CORS、不切流动站、不等 GGA）
        for banned in ('ROVER_SETUP', 'NMEA_SETUP', 'GGA_WAIT',
                       'CORS_CONNECT', 'CORS_COLLECT', 'CORS_CLOSE', 'FIX_WAIT'):
            assert banned not in stages, f'复用分支不应出现阶段 {banned}：{stages}'
        # 坐标是真的写进模块 NVM 了，而且是历史值，不是现场测出来的
        assert module.nvm['mode'] == 2, module.nvm
        assert abs(module.nvm['svin']['x'] - X) < 1e-4, module.nvm['svin']
        assert abs(module.nvm['svin']['z'] - Z) < 1e-4, module.nvm['svin']
        assert records and store.get_record(records[0])['verified'] == 1
        print(f'[21] A1 复用历史坐标 OK  跳过 CORS 连接与采集（0 点），'
              f'8 步写入链路与 1005 校验照常，记录 #{records[0]}')

        print('=== writer 自测全部通过（22 项）===')
        return 0
    finally:
        workspace.STATE_DIR, store.CALIBRATION_DB, serial_module.Serial = original
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING,
                        format='%(levelname)-7s %(message)s')
    raise SystemExit(_selftest())
