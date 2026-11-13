# -*- coding: utf-8 -*-
"""一键标定的 JSON 接口 + 页面路由（方案 §11）。

三条硬约束（改代码前先读）：

1. **全部 `@login_required`**。标定过程里有 CORS 口令、精确 ECEF、模块 SN，
   这些一样都不能进公开的 `/ws` 通道（那条通道未登录也能连，只做打码广播）。
   所以状态接口走**登录态 HTTP 轮询**（1 秒一次），不走 WebSocket。
2. **并发任务拦在 409**。串口是独占资源，第二个任务进来必须被挡回，
   而不是排队等着把彼此的字节读乱。
3. **口令永不回显**。下发的密码只进任务参数；`status`/`history` 返回前统一打码，
   日志尾同理。
"""
import logging
import re

from flask import jsonify, render_template, request
from ..calibration import service as calib
from ..calibration import store
from ..calibration.drivers import driver_manager
from ..calibration.drivers.base import _normalize_method
from ..calibration.writer import SURVEY_PROFILES as _SURVEY_PROFILES
from ..core import state
from ..core.config import get_config, save_config
from ..security import login_required

# 规范方式键 → 界面文案。错误信息与日志用它，四个键只在这里写一次。
_METHOD_LABEL = {
    'cors': 'CORS 一键固定',
    'survey_in': '自收敛（Survey-in）',
    'manual': '写入已知坐标',
    'external_rover': '仅测量坐标',
}

# 需要打码的键名（与 core.config.mask_config 同款规则，这里独立一份是为了
# 也能处理"自由文本里的 password=xxx"）
_SECRET_KEY_RE = re.compile(r'(pass(word|wd)?|secret|token)\s*[=:]\s*(\S+)', re.I)

# 采集时长三档（与 writer.CORS_DURATIONS 对应，这里只给前端做下拉）
DURATION_CHOICES = [
    {'value': 'short', 'seconds': 180},
    {'value': 'standard', 'seconds': 300},
    {'value': 'long', 'seconds': 600},
]

# Survey-in 档位**直接从 writer 取**，不再手抄一份。
# 手抄过一次，抄成了 fast/standard/precise，而 writer 里是 fast/standard/high，
# 结果页面上选"精确"必然被后端拒（400），且三档的文案数值全对不上。
SVIN_PROFILES = list(_SURVEY_PROFILES.keys())


def _baud_param(raw):
    """把前端波特率框的值规整成 service 能识别的形态。

    前端留空时发的是 `null`（自动），但也可能发空串 / 'auto'；如果原样
    传给 `int()` 就会报"波特率不合法"，而这恰恰是**默认路径**。
    这里只做"留空 → None"，**不做档位白名单校验**——厂商文档之外的
    非标档位（LC29H 的 3000000、UM980 的 19200，或现场改过的值）
    必须能填能发，是否合法交给 service 统一判断。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw) or None
    text = str(raw).strip()
    if not text or text.lower() == 'auto':
        return None
    return text


def _msm_value(raw):
    """MSM 档位：数字字符串 → int；'keep'/0/空 → None（沿用模块默认）。"""
    if raw is None or raw == '':
        return 4
    text = str(raw).strip().lower()
    if text in ('keep', 'default', 'auto', '0'):
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return 4


def _mask(text):
    """把自由文本里的口令打码。日志尾、错误信息都要过一遍。"""
    if not text:
        return text
    return _SECRET_KEY_RE.sub(lambda m: f'{m.group(1)}=******', str(text))


def _mask_obj(obj):
    """递归打码：字典里键名含 password/passwd/secret 的值一律替换。"""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if re.search(r'pass(word|wd)?|secret|token', str(key), re.I):
                out[key] = '******' if value else value
            else:
                out[key] = _mask_obj(value)
        return out
    if isinstance(obj, list):
        return [_mask_obj(item) for item in obj]
    if isinstance(obj, str):
        return _mask(obj)
    return obj


def _snapshot_payload(full=False):
    """状态快照 + 实时指标 + 日志尾。`full=False` 时裁掉大字段，省带宽。"""
    snap = calib.status()
    result = snap.get('result') or {}
    live = result.get('live') if isinstance(result, dict) else None
    plan_info = (result.get('plan') or {}) if isinstance(result, dict) else {}
    payload = {
        'ok': True,
        'task_id': snap.get('task_id'),
        'action': snap.get('action'),
        # 标定方式（cors / survey_in / manual / external_rover）：
        # 前端据此决定"完成后要不要显示设为数据源"——只测不写时不该出现。
        'method': plan_info.get('method') or '',
        'stage': snap.get('stage'),
        'stage_text': snap.get('stage_text'),
        'progress': snap.get('progress'),
        'message': _mask(snap.get('message')),
        'error': _mask(snap.get('error')),
        'running': snap.get('running'),
        'port': snap.get('port'),
        'baud': snap.get('baud'),
        # False = 波特率框留空（自动）。前端据此区分"用户指定了档位"与
        # "让后端按型号官方档位逐档试"，否则结果里"已自动改为 X"无处解释。
        'baud_explicit': bool(snap.get('baud_explicit')),
        'started_at': snap.get('started_at'),
        'stage_at': snap.get('stage_at'),
        'finished_at': snap.get('finished_at'),
        'lease': snap.get('lease'),
        # 上次被硬中断的详情：页面据此显示警告横幅（方案 §14 #20）。
        # 与 `stage == 'INTERRUPTED'` 冗余，但前端判断"要不要画横幅"只该有一个依据，
        # 这里给对象本身，避免前端再去 `result` 里翻。
        'interrupted': _mask_obj(snap.get('interrupted')),
        'live': live,
        'has_result': bool(result),
    }
    if full:
        payload['result'] = _mask_obj(result)
        payload['logs'] = _log_tail(snap.get('task_id'))
        payload['summary'] = _mask_obj(result.get('summary') if isinstance(result, dict) else None)
    return payload


def _log_tail(task_id, limit=40):
    """最近若干条事件（阶段流水），供状态区下面的日志尾展示。"""
    if not task_id:
        return []
    try:
        events = store.list_events(task_id, limit=limit)
    except Exception as e:                      # noqa: BLE001 - 日志尾缺了不影响主流程
        logging.debug(f'读取事件失败（忽略）：{e}')
        return []
    return [{'at': e.get('created_at') or e.get('at') or '',
             'stage': e.get('stage'),
             'level': e.get('level'),
             'message': _mask(e.get('message'))} for e in events]


# ==================== 页面 ====================
@login_required
def calibration_page():
    return render_template('calibration.html')


# ==================== 接口 ====================
@login_required
def api_calibration_options():
    """一次拉全：型号分组 + 能力矩阵 + 历史坐标 + 默认参数 + 运行态。

    前端据此渲染下拉、按能力灰化、填默认值 —— 型号列表不要在 JS 里硬编码，
    否则加了新驱动前端就看不到。
    """
    modules = driver_manager.list_supported_modules()
    # 把"这块型号上次回读到的串口参数"贴到型号条目上：页面据此提示
    # "留空则先试该型号自己的默认档（LG290P 460800 / LC29H(BS) 115200）"，
    # 而不是让用户面对一个空输入框猜。
    # 放在 API 层合并、不放进 driver_manager：驱动注册表不该反过来依赖标定库。
    try:
        profiles = {item.get('model_key'): item for item in store.list_profiles()}
    except Exception as e:                      # noqa: BLE001
        profiles = {}
        logging.warning(f'读取型号波特率记忆失败（忽略）：{e}')
    for item in modules.get('all') or []:
        prof = profiles.get(item.get('id')) or {}
        try:
            item['remembered_baud'] = int(prof.get('uart_baud') or 0)
        except (TypeError, ValueError):
            item['remembered_baud'] = 0
        item['uart'] = prof.get('uart') or {}

    latest = []
    try:
        latest = store.list_records(limit=3)
    except Exception as e:                      # noqa: BLE001
        logging.warning(f'读取历史标定记录失败（忽略）：{e}')
    # 「复用坐标」取全库最近一条，**不按模块 SN 过滤**。
    # 这里曾经按当前模块 SN 过滤，真机上就卡住了：先用 LG290P 测出天线坐标，
    # 换成 LC29H(BS) 想复用时，页面判定"本机还没有这块模块的历史坐标"直接把
    # 复用按钮锁死 —— 明明同一根天线、同一个安装位置，只是换了块模块。
    # 坐标描述的是**天线相位中心**这个物理点，跟谁去测、谁去播没关系。
    # 页面把模块型号/SN/标定时间一并显示出来，让用户自己核对"是不是同一根天线"，
    # 这比程序替他判定更可靠：程序根本不知道天线有没有被挪过。
    #
    # 只挑**带完整 ECEF** 的那条：记录可以只有经纬度（按约定不再反算 ECEF），
    # 或者来自一次中断的运行。把这种行当默认复用项，用户点完会收到
    # "请填写 ECEF 或经纬高" —— 库里明明有能用的记录。
    reuse = store.get_latest_with_coord()
    snap = calib.status()
    return jsonify({
        'ok': True,
        'modules': modules,
        'durations': DURATION_CHOICES,
        'svin_profiles': [
            {'value': key, 'count': item['count'], 'acc': item['acc'],
             'grade': item['grade'], 'label': item['label']}
            for key, item in _SURVEY_PROFILES.items()
        ],
        'history': [_mask_obj(item) for item in latest],
        # 供「复用坐标」用：全库最近一条（任意模块、任意 SN）。空 = 库里还没有坐标
        'reuse_history': [_mask_obj(reuse)] if reuse else [],
        'backup': calib.backup_info(),
        'defaults': {
            # 只是"没选型号时"的兜底展示值。**真正的首试档由型号决定**：
            # 输入框留空且选了型号时，后端走 `_default_baud_for(module)`
            # （LG290P 460800 / LC29H(BS) 115200），不读这个字段。
            'baud': 115200,
            # 上次回读到的波特率（任意型号）。前端只在用户留空且选了具体型号时
            # 才拿它当提示，不会覆盖用户的显式输入。
            'remembered_baud': store.remembered_baud(),
            'duration': 'standard',
            # 2101 是通用 NTRIP 端口；国内移动 CORS 的 VRS 实测走 8002（WGS84）/
            # 8001（CGCS2000），2101 直接超时。默认给 8002，另在页面留候选。
            'cors_port': 8002,
            'svin_profile': 'standard',
            'method': 'cors',
        },
        'running': bool(snap.get('running')),
        'stage': snap.get('stage'),
        'serial_port': get_config().get('serial_port') or '',
        'local_caster': {
            'enabled': bool(get_config().get('local_caster_enabled')),
            'port': get_config().get('local_caster_port'),
        },
    })


@login_required
def api_calibration_scan():
    """扫描串口并判定是不是 GNSS 模块（**只判定，不决定**）。

    POST body：
        ports   [str]   要扫的口；留空 = 扫全部
        bauds   [int]   要试的波特率；留空 = 从高到低扫全集
        active  bool    被动读不到时要不要主动发查询（默认 true）
        wait    bool    同步等待模式（CLI/脚本用），默认 false

    结果**不过滤**：每个口都返回，带 `family`（gnss / unknown / noise / busy）、
    `class`（rtk / gnss，非 GNSS 口为空）、`vendor`（认得出厂牌才有）、
    `vendor_confidence`（high / medium / low）、`vendor_evidence`（判定依据，
    给前端做 tooltip）、`model`（型号，可能为空）、`features`
    （rtcm / nmea / ubx / sbf / sirf / ashtech / novatel / trimble /
    nmea_priv / unicore / pq）与 `note`。前端据此显示，
    用户自己决定点哪个口、要不要当成模块 —— 这里不做任何自动选择。

    厂牌判定的证据链（强 → 弱）：
        RTCM-1033 的 `RECEIVER TYPE`  /  `#VERSION` ASCII 日志头 /
        UBX·SBF·TSIP·SiRF 专属二进制  /  `$PQTM` 等私有命令族  /
        私有 NMEA talker（`$PUBX` `$PSTM` …）  /  文本横幅里的厂名
    认不出就返回空串，**不猜** —— 编一个厂牌比空白更糟。基准站只播 RTCM
    且没开 1033 时，扫描还会补发一次只读版本查询（`$PQTMVERNO` 等）来认。

    `class` 与 `family` 的分工：`family` 回答"是不是 GNSS 设备"，
    `class` 回答"能不能差分"。判据只有一条硬证据 —— CRC-24Q 校验通过的
    RTCM3 帧；只有 NMEA / UBX / 私有语句的口一律标 `gnss`，
    那是"知道自己在哪"，不是"厘米级"。
    """
    body = request.get_json(silent=True) or {}
    ports = body.get('ports')
    if isinstance(ports, str):
        ports = [item.strip() for item in ports.split(',') if item.strip()]
    if ports is not None and not isinstance(ports, list):
        return jsonify({'ok': False, 'error': 'ports 必须是字符串数组'}), 400

    bauds = body.get('bauds')
    if isinstance(bauds, (str, int)):
        bauds = [bauds]
    clean_bauds = []
    for item in (bauds or []):
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            clean_bauds.append(value)

    ok, message, payload = calib.scan_ports(
        ports=ports or None, bauds=clean_bauds or None,
        active=bool(body.get('active', True)))
    if not ok:
        busy = '在进行' in str(message)
        return jsonify({'ok': False, 'error': _mask(message),
                        'scan': _mask_obj(payload)}), (409 if busy else 400)
    return jsonify({'ok': True, 'message': _mask(message),
                    'scan': _mask_obj(payload)}), 202


@login_required
def api_calibration_scan_status():
    """扫描进度与结果（1 秒轮询）。附带当前系统串口清单，页面首屏用它填下拉。"""
    resp = jsonify({'ok': True, 'scan': _mask_obj(calib.scan_status())})
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp


@login_required
def api_calibration_status():
    """1 秒轮询：阶段 / 进度 / 实时指标 / 日志尾。

    带 `?full=1` 时额外返回完整 result 与日志（任务结束时前端拉一次即可）。
    """
    full = str(request.args.get('full') or '').lower() in ('1', 'true', 'yes')
    resp = jsonify(_snapshot_payload(full=full))
    # 状态里含精确坐标与口令掩码，任何中间缓存都不该留存
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp


@login_required
def api_calibration_detect():
    """只识别不标定：3~15 秒，走同一套租约。"""
    body = request.get_json(silent=True) or {}
    ok, message, payload = calib.start_diagnose(
        port=body.get('port'), baud=_baud_param(body.get('baud')),
        module=body.get('module'), force=bool(body.get('force')))
    if not ok:
        busy = '已有标定任务' in str(message)
        return jsonify({'ok': False, 'error': _mask(message),
                        'status': payload if not busy else None}), (409 if busy else 400)
    return jsonify({'ok': True, 'message': _mask(message), 'status': _snapshot_payload()}), 202


@login_required
def api_calibration_start():
    """下发 plan 启动任务。202=已接受，409=已有任务在跑。

    **方式的合法性只有一处判据**：`driver.methods()`（由驱动声明的 ROLE 派生）。
    以前这里写的是 `if method in ('cors', ...)` 一张字符串白名单，而驱动那边
    另有一套 CAPS 判断 —— 两边一旦不一致，页面能点、后端 400，或者反过来
    后端放行、执行到一半才在切模式那步失败。现在先按型号收紧，再分流。
    """
    body = request.get_json(silent=True) or {}
    # 别名收口：'measure' / 'svin' / 'known' 都是老写法，统一成规范键。
    # 漏传 method 时**不再默认成 cors** —— 老代码 `body.get('method') or 'cors'`
    # 让"忘了传"变成"用 CORS 跑"，而 CORS 是最重的一条路（要连网、要账号、
    # 要切流动站）。现在直接 400 报清楚。
    raw_method = body.get('method')
    if not raw_method:
        return jsonify({'ok': False, 'error': '缺少 method（cors / survey_in / manual / external_rover）'}), 400
    method = _normalize_method(raw_method)
    port = body.get('port')
    baud = _baud_param(body.get('baud'))
    module = body.get('module')

    # 先挡"键本身不认识"：归一化后仍是原样（且不等于任何规范键）说明前端
    # 传了一个我们没定义的方法名。这时候报"型号不支持"会误导用户去换模块，
    # 真正的问题是调用方拼错了键。
    if method not in _METHOD_LABEL:
        return jsonify({
            'ok': False,
            'error': (f'未知的标定方式：{raw_method}'
                      f'（可用：{" / ".join(_METHOD_LABEL)}）'),
            'methods': list(_METHOD_LABEL),
        }), 400

    # 型号能力闸门。**只在用户亲手指定了型号时生效。**
    #
    # `module` 为空有两种情况，语义完全不同，不能混为一谈：
    #   1. 用户选了「自动识别」—— 型号得占住串口读出来才知道，**此刻就是未知**；
    #   2. 用户指定了一个我们没注册的型号 —— 那确实该按最保守处理。
    # 原实现一律按 role='rover' 拒掉 cors，真机上就直接踩了：页面留着默认的
    # 「自动识别」，点「读取模块信息」明明认出了 `Quectel LG290P`，接着点
    # 「CORS 一键固定」却被 400 顶回来「未知模块是「流动站（仅测量）」」。
    #
    # 与 `_reject_without_caps` / `_reject_write_without_fixed` 保持同一约定：
    # **未指定型号时放行，运行时拿到真实驱动再判一次**。那两道闸门本来就这么写
    # （见 service.py 里 "未指定型号时放行（运行时再判）" 的注释），只有这里漏了。
    # 前端也在 `computeLocks()` 里同步改成"真正未知才全放开"。
    if module:
        allowed = driver_manager.methods_of(module)
        if method not in allowed:
            driver = driver_manager.get_driver_by_id(module)
            name = driver.DISPLAY_NAME if driver else '未知模块'
            role = driver.role_label() if driver else '流动站（仅测量）'
            return jsonify({
                'ok': False,
                'error': (f'{name} 是「{role}」，不支持「{_METHOD_LABEL.get(method, method)}」。'
                          f'可用的方式：{" / ".join(_METHOD_LABEL.get(m, m) for m in allowed)}'),
                'role': driver_manager.role_of(module),
                'methods': list(allowed),
            }), 400

    if method in ('cors', 'external_rover'):
        cors = body.get('cors') or {}
        # `reuse_arp` 走标定流程 A1 的复用分支：库内已有该天线坐标且未移动 →
        # 跳过整段 CORS 测量，不连网、不占账号，直接设基准站。
        # 只对 method='cors' 生效（'external_rover' 是只测不写，两者互斥）。
        reuse_arp = body.get('reuse_arp') if method == 'cors' else None
        ok, message, payload = calib.start_cors(
            port=port, baud=baud,
            host=cors.get('host'), cors_port=cors.get('port'),
            mountpoint=cors.get('mountpoint'),
            user=cors.get('user'), password=cors.get('password'),
            duration=body.get('duration') or 'standard',
            msm=_msm_value(body.get('msm')),
            antenna=body.get('antenna'),
            remark=body.get('remark') or '',
            fix_wait_timeout=body.get('fix_wait_timeout'),
            module=module, coord_msg=body.get('coord_msg') or '1005',
            measure_only=(method != 'cors'),
            on_slow=body.get('on_slow') or 'adopt',
            reuse_arp=reuse_arp)
    elif method == 'survey_in':
        ok, message, payload = calib.start_survey_in(
            port=port, baud=baud,
            profile=body.get('svin_profile') or 'standard',
            msm=_msm_value(body.get('msm')),
            antenna=body.get('antenna'),
            remark=body.get('remark') or '',
            svin_timeout=body.get('svin_timeout'),
            module=module, coord_msg=body.get('coord_msg') or '1005',
            resume=body.get('svin_resume', True))
    elif method == 'manual':
        # `arp` = 复用历史坐标：库里存的就是当年写进模块的值，直通写入。
        ok, message, payload = calib.start_write(
            port=port, baud=baud,
            ecef=body.get('ecef'), lat=body.get('lat'), lon=body.get('lon'),
            # 高程**不要**在这里补 0：缺了就让它缺，由 WritePlan 报"必须给高程"。
            # 补 0 等于替用户断言"天线在椭球高 0 米"，而这个断言在页面上看不出来，
            # 后果是整站静默低几十米（校验链路只核对"写入 == 播发"，两个都错就查不出）。
            height=body.get('height'),
            arp=body.get('arp'),
            method='manual', msm=_msm_value(body.get('msm')),
            antenna=body.get('antenna'),
            remark=body.get('remark') or '',
            module=module, coord_msg=body.get('coord_msg') or '1005')
        # 已知坐标写入**不测量**，所以"没信号"这件事不该拦它。
        # 历史坐标 / 手填 ECEF 都是现场测量过的值，直接写就行。
    else:
        # 归一化后还有第四种以外的值，说明前端传了个我们不认识的键。
        # 这里不猜、不打补丁，直接报清楚 —— 猜错一次就是"点 A 跑了 B"。
        return jsonify({'ok': False, 'error': f'未知的标定方式：{raw_method}'}), 400

    if not ok:
        busy = '已有标定任务' in str(message)
        return jsonify({'ok': False, 'error': _mask(message),
                        'status': _snapshot_payload() if busy else None}), (409 if busy else 400)
    return jsonify({'ok': True, 'message': _mask(message), 'status': _snapshot_payload()}), 202


@login_required
def api_calibration_abort():
    """终止。在 SAVE/RESTART 阶段只做标记（方案 §9.2），不会硬中断写 NVM。

    `service.abort()` 的契约是 `(ok, message)` 两元组（另外两处调用点也是这么解包的），
    这里曾按三元组解包，真机上点「中止」必然 500——取消其实已经生效，但页面
    只能看到"服务器错误"，用户会以为没停下来而反复点。快照另行取一次即可。
    """
    ok, message = calib.abort()
    return jsonify({'ok': bool(ok), 'message': _mask(message),
                    'status': _snapshot_payload()}), (200 if ok else 409)


@login_required
def api_calibration_apply_as_source():
    """把该口设为串口数据源并重启数据面（写 settings 必须用户显式点）。"""
    body = request.get_json(silent=True) or {}
    port = str(body.get('port') or '').strip()
    if not port:
        return jsonify({'ok': False, 'error': '缺少串口'}), 400
    baud = _baud_param(body.get('baud'))
    update = {'serial_port': port, 'mode': 'serial'}
    # 前端留空 = 自动探测。此时**必须**把本次任务实际协商到的波特率写进配置：
    # 数据面是按 settings 里的 baud_rate 打开串口的，留空就落回默认 115200，
    # 而模块可能刚被识别成 460800 —— 用户点"设为数据源"后数据面反而读不到字节。
    if baud:
        update['baud_rate'] = int(baud)
    else:
        negotiated = (calib.status() or {}).get('baud')
        if negotiated:
            update['baud_rate'] = int(negotiated)
    save_config(update)
    state.restart_event.set()
    logging.info(f'已把 {port} 设为串口数据源并请求重启数据面')
    return jsonify({'ok': True, 'message': f'已设为数据源：{port}（数据面重启中）'})


@login_required
def api_calibration_history():
    """历史记录分页（按 SN / 方式过滤，默认按时间倒序）。"""
    try:
        limit = max(1, min(int(request.args.get('limit') or 20), 100))
        offset = max(0, int(request.args.get('offset') or 0))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'limit/offset 必须是整数'}), 400
    module_sn = str(request.args.get('sn') or '').strip() or None
    method = (request.args.get('method') or '').strip() or None
    items = store.list_records(limit=limit, offset=offset,
                               module_sn=module_sn, method=method)
    total = store.count_records(module_sn=module_sn)
    return jsonify({'ok': True, 'total': total, 'limit': limit, 'offset': offset,
                    'items': [_mask_obj(item) for item in items]})


def api_calibration_modules():
    """模块台账（按 SN）：这块板子上次插在哪个口、固件是哪一版、标定用的什么方式。

    和 `history` 的分工：history 是"每次标定一行"的流水，只有标定成功才有行；
    modules 是"每块板子一行"的状态快照，**读一次信息就更新一次**，
    所以没标定过的模块也在这里，且带着上次的端口与固件 —— 这正是
    "把这块板子插回来，上次是怎么配的" 这类问题的答案。
    """
    try:
        limit = max(1, min(int(request.args.get('limit') or 50), 200))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'limit 必须是整数'}), 400
    items = store.list_module_info(limit=limit)
    return jsonify({'ok': True, 'limit': limit, 'count': len(items), 'items': items})


def _install_json_error_page(app):
    """让标定接口的未捕获异常返回 **JSON**，而不是 Flask 的 HTML 错误页。

    真机实测：`POST /api/calibration/start` 里 `_check_ecef` 抛 `KeyError`，
    Flask 兜底渲染了 `<!doctype html>...500 Internal Server Error...`。
    而前端 `calibration.js` 有十几处都是 `res.json()` 直取 —— 拿到 HTML 就
    `SyntaxError: Unexpected token '<'`，页面只能显示"网络错误"，
    **真实原因（坐标格式不对）一个字都传不到用户眼前**，用户会以为是服务没起来
    而反复重试，每次重试都重新占一遍串口。

    所以这里挂一层**只作用于标定 API 前缀**的兜底：
      * `/api/calibration/*` 出错 → 500 + `{'ok': false, 'error': ...}`；
      * 页面 `/calibrate` 保持 HTML 行为；
      * 其它路由（数据面、登录）完全不受影响。

    为什么不用 `@app.errorhandler(500)`：那是**全局**的，会把主程序数据面接口的
    错误页也换成 JSON，属于干扰别的模块。这里用 `after_request` 只改写
    **已经发生的 5xx 且仍是 HTML** 的响应，命中面严格限定在前缀内。
    """
    from flask import jsonify as _jsonify

    @app.after_request
    def _jsonify_calib_errors(response):
        """把标定 API 的 HTML 5xx 换成 JSON。"""
        if not request.path.startswith('/api/calibration/'):
            return response
        if response.status_code < 500 or response.is_json:
            return response
        # Flask 的 HTML 错误页里带着异常类名，尽量抠出来给前端一个线索
        detail = ''
        try:
            body = response.get_data(as_text=True)
            match = re.search(r'<p>(.*?)</p>', body, re.S)
            if match:
                detail = re.sub(r'<[^>]+>', '', match.group(1)).strip()
        except Exception:                              # noqa: BLE001 - 抠不出就算了
            detail = ''
        text = f'标定服务内部错误：{detail}' if detail else '标定服务内部错误（详见服务端日志）'
        payload = _jsonify({'ok': False, 'error': _mask(text), 'detail': _mask(detail)})
        payload.status_code = response.status_code
        return payload


def register(app):
    """挂到 app 上。endpoint 名统一 `api_calibration_*`，页面用 `calibration_page`。

    这里顺带跑一次**进程启动钩子**（方案 §14 #20 / #30）。放这里而不是
    `core/lifecycle.py`，是因为契约要求 core 层不得依赖标定包（删掉
    `rtkserver/calibration/` 主程序照跑）；而 web/app.py 调本函数时正好
    在 Web 线程起来之前、`run_once()` 之前，是"进程刚起来"这个语义点。
    钩子内部自带幂等与异常兜底，失败只记日志，不影响任何其它功能。
    """
    _install_json_error_page(app)
    app.add_url_rule('/calibrate', 'calibration_page', calibration_page, methods=['GET'])
    app.add_url_rule('/api/calibration/options', 'api_calibration_options',
                     api_calibration_options, methods=['GET'])
    app.add_url_rule('/api/calibration/status', 'api_calibration_status',
                     api_calibration_status, methods=['GET'])
    app.add_url_rule('/api/calibration/detect', 'api_calibration_detect',
                     api_calibration_detect, methods=['POST'])
    # 串口扫描：只判定、不决定。和 detect 是两件事 ——
    # detect 是"我已经选定了这个口和型号，读一下它的信息"（要走租约、占串口）；
    # scan 是"电脑上都有哪些口、哪些在吐 GNSS 数据"（独立跑，不占租约）。
    app.add_url_rule('/api/calibration/scan', 'api_calibration_scan',
                     api_calibration_scan, methods=['POST'])
    app.add_url_rule('/api/calibration/scan/status', 'api_calibration_scan_status',
                     api_calibration_scan_status, methods=['GET'])
    app.add_url_rule('/api/calibration/start', 'api_calibration_start',
                     api_calibration_start, methods=['POST'])
    app.add_url_rule('/api/calibration/abort', 'api_calibration_abort',
                     api_calibration_abort, methods=['POST'])
    app.add_url_rule('/api/calibration/apply-as-source', 'api_calibration_apply_as_source',
                     api_calibration_apply_as_source, methods=['POST'])
    app.add_url_rule('/api/calibration/history', 'api_calibration_history',
                     api_calibration_history, methods=['GET'])
    app.add_url_rule('/api/calibration/modules', 'api_calibration_modules',
                     api_calibration_modules, methods=['GET'])

    try:
        calib.on_startup()
    except Exception as e:                           # noqa: BLE001 - 钩子失败不能拦住启动
        logging.warning(f'标定启动钩子执行失败（已忽略）：{e}')
