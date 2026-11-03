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
from ..calibration.writer import SURVEY_PROFILES as _SURVEY_PROFILES
from ..core import state
from ..core.config import get_config, save_config
from ..security import login_required

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
    latest = []
    try:
        latest = store.list_records(limit=3)
    except Exception as e:                      # noqa: BLE001
        logging.warning(f'读取历史标定记录失败（忽略）：{e}')
    # 历史坐标能不能被「复用」取决于它是不是**这台设备**的记录。
    # 上面只取了全库最近 3 条（页面要展示），但复用时必须知道当前模块 SN 下
    # 有没有记录 —— 否则会拿另一块模块/另一根天线的坐标往这台机器里写，
    # 1005 校验照样"通过"（它只核对写入值与播发值一致），现场表现为
    # "基准站播的是别人家的位置"，而且查不出任何异常。
    # 这里把当前模块（页面选中的型号）的 SN 对应记录单独给一份，
    # 前端只用这一份做复用，`history` 仍作展示用途。
    module_sn = str(request.args.get('sn') or '').strip()
    own_records = []
    if module_sn:
        try:
            own_records = store.list_records(limit=1, module_sn=module_sn)
        except Exception as e:                  # noqa: BLE001
            logging.warning(f'读取本机历史坐标失败（忽略）：{e}')
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
        # 仅供「复用坐标」用：当前模块 SN 的最近一条记录。空列表 = 这台设备没标定过
        'own_history': [_mask_obj(item) for item in own_records],
        'backup': calib.backup_info(),
        'defaults': {
            'baud': 115200,
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
        module=body.get('module'))
    if not ok:
        busy = '已有标定任务' in str(message)
        return jsonify({'ok': False, 'error': _mask(message),
                        'status': payload if not busy else None}), (409 if busy else 400)
    return jsonify({'ok': True, 'message': _mask(message), 'status': _snapshot_payload()}), 202


@login_required
def api_calibration_start():
    """下发 plan 启动任务。202=已接受，409=已有任务在跑。"""
    body = request.get_json(silent=True) or {}
    method = str(body.get('method') or 'cors').strip().lower()
    port = body.get('port')
    baud = _baud_param(body.get('baud'))
    module = body.get('module')

    if method in ('cors', 'external_rover', 'measure'):
        cors = body.get('cors') or {}
        # `reuse_arp` 走标定流程 A1 的复用分支：库内已有该天线坐标且未移动 →
        # 跳过整段 CORS 测量，不连网、不占账号，直接设基准站。
        # 只对 method='cors' 生效（'measure' 是只测不写，两者互斥）。
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
    elif method in ('survey_in', 'svin'):
        ok, message, payload = calib.start_survey_in(
            port=port, baud=baud,
            profile=body.get('svin_profile') or 'standard',
            msm=_msm_value(body.get('msm')),
            antenna=body.get('antenna'),
            remark=body.get('remark') or '',
            svin_timeout=body.get('svin_timeout'),
            module=module, coord_msg=body.get('coord_msg') or '1005',
            resume=body.get('svin_resume', True))
    elif method in ('manual', 'known'):
        # `arp` = 复用历史坐标：库里存的就是当年写进模块的值，直通写入。
        ok, message, payload = calib.start_write(
            port=port, baud=baud,
            ecef=body.get('ecef'), lat=body.get('lat'), lon=body.get('lon'),
            height=body.get('height') or 0.0,
            arp=body.get('arp'),
            method='manual', msm=_msm_value(body.get('msm')),
            antenna=body.get('antenna'),
            remark=body.get('remark') or '',
            module=module, coord_msg=body.get('coord_msg') or '1005')
    else:
        return jsonify({'ok': False, 'error': f'未知的标定方式：{method}'}), 400

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


def register(app):
    """挂到 app 上。endpoint 名统一 `api_calibration_*`，页面用 `calibration_page`。

    这里顺带跑一次**进程启动钩子**（方案 §14 #20 / #30）。放这里而不是
    `core/lifecycle.py`，是因为契约要求 core 层不得依赖标定包（删掉
    `rtkserver/calibration/` 主程序照跑）；而 web/app.py 调本函数时正好
    在 Web 线程起来之前、`run_once()` 之前，是"进程刚起来"这个语义点。
    钩子内部自带幂等与异常兜底，失败只记日志，不影响任何其它功能。
    """
    app.add_url_rule('/calibrate', 'calibration_page', calibration_page, methods=['GET'])
    app.add_url_rule('/api/calibration/options', 'api_calibration_options',
                     api_calibration_options, methods=['GET'])
    app.add_url_rule('/api/calibration/status', 'api_calibration_status',
                     api_calibration_status, methods=['GET'])
    app.add_url_rule('/api/calibration/detect', 'api_calibration_detect',
                     api_calibration_detect, methods=['POST'])
    app.add_url_rule('/api/calibration/start', 'api_calibration_start',
                     api_calibration_start, methods=['POST'])
    app.add_url_rule('/api/calibration/abort', 'api_calibration_abort',
                     api_calibration_abort, methods=['POST'])
    app.add_url_rule('/api/calibration/apply-as-source', 'api_calibration_apply_as_source',
                     api_calibration_apply_as_source, methods=['POST'])
    app.add_url_rule('/api/calibration/history', 'api_calibration_history',
                     api_calibration_history, methods=['GET'])

    try:
        calib.on_startup()
    except Exception as e:                           # noqa: BLE001 - 钩子失败不能拦住启动
        logging.warning(f'标定启动钩子执行失败（已忽略）：{e}')
