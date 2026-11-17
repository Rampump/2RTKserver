/* ============================================================================
 * 一键标定页面（/calibrate）
 *
 * 页面结构按「标定方式」分区：选了哪种方式，就只有那一条路径真正会用到的
 * 设置块会显示出来。四种方式是互斥的四条后端链路，各自需要的输入完全不同：
 *
 *   cors            双模模块 + CORS 账号 → 厘米级；有复用分支（reuse_arp）
 *   survey_in       固件自己收敛 → 亚米级；只要档位 + 断点续标
 *   manual          拿已有坐标直接写；坐标来源三选一（手输 / 历史 / 先测再写）
 *   external_rover  只测不写；走 CORS 采集链路但不落一字节 NVM
 *
 * 几条刻意的选择：
 *   1. 状态走**登录态 HTTP 轮询**（1 秒一次），不用 /ws。那条通道未登录也能连，
 *      只做打码广播；标定过程里有 CORS 口令、精确 ECEF、模块 SN，不能进公开广播。
 *   2. 型号从 `/api/calibration/options` 动态拉，分组（双模/单模）与能力位都由后端给，
 *      前端**不硬编码任何型号名**，否则加驱动就得改 JS。
 *   3. 页面关掉任务照跑（服务端线程）；刷新后靠 status 的 stage 恢复"运行中"形态。
 *   4. **能力锁与后端拦截规则逐条对齐**。前端把按钮放开、后端再 400，
 *      用户只会以为程序坏了。两处尤其容易写错：
 *        - survey_in 只查 caps.svin，**不**要求 caps.fixed
 *        - cors 的复用分支不切流动站，所以**不**要求 dual_mode
 * ========================================================================== */
(function () {
  'use strict';

  var t = function (key, args) {
    try { return window.I18N ? window.I18N.t(key, args) : key; } catch (e) { return key; }
  };

  // i18n 缺键时 t() 会原样返回键名 —— 拿它当"有没有这条文案"的判据
  function hasKey(key) {
    var text = t(key);
    return !!text && text !== key;
  }

  var METHOD_LABEL = {
    cors: 'cal.method.cors',
    survey_in: 'cal.method.svin',
    manual: 'cal.method.manual',
    external_rover: 'cal.method.measure'
  };

  // 模块下拉框里代表「自动识别」的那一项（fillModules() 里也是这个值）
  var AUTO_ID = '__auto__';

  // 会写模块 NVM 的方式。external_rover 是唯一一个"一个字节都不写"的，
  // 它决定了 RTCM 配置块要不要出现、完成后要不要显示「设为数据源」。
  function writesModule(method) {
    return method !== 'external_rover';
  }

  var STATE = {
    options: null,
    timer: null,
    running: false,
    lastStage: '',
    // 阶段流水最后一行的文本节点：同一阶段内的进度消息直接改写这一行，
    // 不再每秒新增一行（见 appendTimeline）。
    lastRowText: null,
    lastTaskId: '',
    method: 'cors',
    // 「读取模块信息」识别出来的型号 id（driver id，如 'lg290p'）。
    // 下拉框停在「自动识别」时，它就是"按什么型号办事"的依据；
    // 手动改型号或切换串口时清空，避免拿着上一块模块的身份去跑标定。
    detectedModuleId: '',
    // cors 方式下的坐标来源：'measure'（正常走 CORS 测）或 'history'（复用）
    corsSource: 'measure',
    // manual 方式下的坐标来源：typing / history / measure_first
    coordSource: 'typing',
    ready: false,
    applied: false,
    hwTaskId: '',
    bannerClosed: false,
    lastSnap: null,
    // 「复用坐标」的来源：库内最近一条标定记录，**不按模块/SN 过滤**。
    // 坐标描述的是天线相位中心这个物理点，换模块、换固件都不影响它 ——
    // 真机上就是因为按 SN 过滤，换成 LC29H(BS) 之后复用按钮被锁死，
    // 而同一根天线半小时前刚测过。页面把型号/SN/时间一并显示，由用户核对
    // "是不是同一根天线"：程序并不知道天线有没有被挪过，替用户判定才是错的。
    reuseHistory: null,
    // 最近一次扫描到的串口候选（供 chip 重绘，避免每次重扫才刷新选中态）
    ports: [],
    // 协议嗅探状态（与标定任务完全独立，见 sniffPorts）
    scan: null,
    history: null
  };

  var TERMINAL = { 'DONE': 1, 'FAILED': 1, 'ABORTED': 1, 'INTERRUPTED': 1 };

  function $(id) { return document.getElementById(id); }
  function on(el, ev, fn) { if (el) el.addEventListener(ev, fn); }

  function toggle(id, visible) {
    var el = $(id);
    if (el) el.classList.toggle('is-hidden', !visible);
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/[<>&"]/g, function (c) {
        return { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c];
      });
  }

  // ---------------- 小工具 ----------------
  function fmtNum(value, digits, suffix) {
    if (value === null || value === undefined || value === '') return '—';
    var n = Number(value);
    if (!isFinite(n)) return '—';
    return n.toFixed(digits) + (suffix || '');
  }

  function fmtElapsed(seconds) {
    if (seconds === null || seconds === undefined) return '';
    var s = Math.max(0, Math.round(Number(seconds)));
    var m = Math.floor(s / 60);
    return m > 0 ? (m + ' 分 ' + (s % 60) + ' 秒') : (s + ' 秒');
  }

  // 采集时长档位 → 秒。**以后端下发的 durations 为准**
  // （`DURATION_CHOICES` ↔ `writer.CORS_DURATIONS`）。前端以前自己写了一份
  // 180/300/600，改档位时两边就会静默对不上：页面说 5 分钟、实际采 3 分钟。
  function durationSeconds(value) {
    var list = (STATE.options || {}).durations || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].value === value) return Number(list[i].seconds) || 0;
    }
    return 300;
  }

  // 取值前的静置观察时长（秒），由后端 `writer.SETTLE_SECONDS` 透传。
  // 老版本 options 没有这个字段时返回 0：估算会偏小，但不会把假数字写进提示。
  function settleSeconds() {
    var d = (STATE.options || {}).defaults || {};
    var n = Number(d.settle_seconds);
    return (isFinite(n) && n > 0) ? Math.round(n) : 0;
  }

  // 主簇不足时程序自动延长的那一段（后端 `EXTEND_SECONDS`）。
  // 以前页面估算停服时间时没算它，长档会比真实耗时少报两分钟。
  function extendSeconds() {
    var d = (STATE.options || {}).defaults || {};
    var n = Number(d.extend_seconds);
    return (isFinite(n) && n > 0) ? Math.round(n) : 0;
  }

  // 停服估算里的固定收尾余量（后端 `CORS_COMPLETION_RESERVE_SECONDS`）。
  // 120 只作为老版本 options 的兼容回退，当前值始终以后端下发为准。
  function reserveSeconds() {
    var d = (STATE.options || {}).defaults || {};
    var n = Number(d.completion_reserve_seconds);
    return (isFinite(n) && n > 0) ? Math.round(n) : 120;
  }

  // 采集档位由 `/api/calibration/options` 生成。模板里不再保留一份静态的
  // “3/5/10 分钟”，否则后端新增 xlong 后，页面会继续显示过期档位。
  function fillDurationChoices(options) {
    var select = $('cal-duration');
    var list = (options && options.durations) || [];
    if (!select || !list.length) return;
    var previous = select.value;
    var defaultKey = (options.defaults && options.defaults.duration) || 'standard';
    select.innerHTML = '';
    list.forEach(function (item) {
      if (!item || !item.value) return;
      var option = document.createElement('option');
      var key = 'cal.duration.' + item.value;
      option.value = item.value;
      option.setAttribute('data-i18n', key);
      option.textContent = hasKey(key)
        ? t(key)
        : (Math.round((Number(item.seconds) || 0) / 60) + ' min');
      select.appendChild(option);
    });
    var wanted = previous || defaultKey;
    var exists = list.some(function (item) { return item && item.value === wanted; });
    select.value = exists ? wanted : defaultKey;
  }

  function showError(message) {
    var box = $('cal-global-error');
    if (!box) return;
    if (!message) { box.classList.add('is-hidden'); box.textContent = ''; return; }
    box.textContent = message;
    box.classList.remove('is-hidden');
  }

  function stageLabel(stage, fallback) {
    if (!stage) return t('cal.stage.idle');
    // 翻译表里的阶段键是**大写**（`cal.stage.FIX_WAIT`），后端枚举也是大写，
    // 所以先按原样查。只有 `idle` 那种小写键才需要二次尝试 —— 之前这里
    // 一律转小写去查，结果 25 个阶段只命中 IDLE 一个，其余 24 个全部
    // 回退到后端的 `stage_text`（中文）。英文界面下状态行一直是中文，
    // 根因就在这一行。
    var raw = 'cal.stage.' + stage;
    var text = t(raw);
    if (text && text !== raw) return text;
    var lower = 'cal.stage.' + String(stage).toLowerCase();
    var alt = t(lower);
    if (alt && alt !== lower) return alt;
    return fallback || stage;
  }

  // 波特率：留空/'auto' → null（后端按型号官方档位逐档探测）。
  // 填了具体数值就是**用户明确指定**，后端不会再擅自改它、也不做枚举。
  // 允许任意正整数：厂商文档里没有的档位（LC29H 的 3000000、UM980 的 19200）
  // 也必须能填，所以这里不做"只能选候选表"的限制，只在明显非法时给提示。
  function baudValue() {
    var el = $('cal-baud');
    var raw = el ? String(el.value || '').trim() : '';
    if (!raw || raw.toLowerCase() === 'auto') return null;
    var n = Number(raw);
    return (isFinite(n) && n > 0 && Math.floor(n) === n) ? n : null;
  }

  // 波特率输入框的即时反馈：合法但不在该型号官方档位里时提醒一句（不拦截）。
  // 为什么只提醒不阻止：某些固件能跑非标波特率，拦下来反而是错。
  function checkBaud() {
    var el = $('cal-baud');
    var hint = $('cal-baud-hint');
    if (!el || !hint) return;
    var raw = String(el.value || '').trim();
    var legal = baudValue();
    var bauds = currentModuleBauds();

    if (!raw || raw.toLowerCase() === 'auto') {
      var remembered = currentModuleRememberedBaud();
      if (remembered) {
        // 这条提示是本轮真机测试的直接产物：用户抱怨"为什么要一档档试"。
        // 模块自己报过的值就在库里，先说清楚"优先用它"，再列官方档位兜底。
        hint.textContent = t('cal.baud.auto_remembered', {
          baud: remembered,
          list: bauds.length ? bauds.join(' / ') : t('cal.baud.auto_tip_plain')
        });
      } else {
        hint.textContent = bauds.length
          ? t('cal.baud.auto_tip', { top: bauds[0], list: bauds.join(' / ') })
          : t('cal.baud.auto_tip_plain');
      }
      hint.classList.remove('is-warn');
      return;
    }
    if (legal === null) {
      hint.textContent = t('cal.baud.invalid');
      hint.classList.add('is-warn');
      return;
    }
    var allowed = currentModuleLegalBauds();
    if (allowed.length && allowed.indexOf(legal) < 0) {
      hint.textContent = t('cal.baud.offlist', { value: legal, list: allowed.join(' / ') });
      hint.classList.add('is-warn');
      return;
    }
    hint.textContent = t('cal.baud.fixed', { value: legal });
    hint.classList.remove('is-warn');
  }

  // 型号选择的即时解释：这一格到底改变什么流程。
  // 后端两条路差别很大（只问一个 vs 挨个问），页面上只给一个下拉框，
  // 用户根本看不出自己选不选会影响什么 —— 真机测试时就有人问"一键标定好像没有
  // 选择模块型号"，其实是有的，只是没有任何文字说明它存在。
  function refreshModuleHint() {
    var box = $('cal-module-hint');
    if (!box) return;
    var mod = selectedModule();
    if (!mod) {
      box.textContent = t('cal.module.hint.auto');
    } else {
      box.textContent = t('cal.module.hint.fixed', { name: mod.name });
    }
  }

  // 当前所选型号的官方波特率档位（自动探测时返回空数组 = 不限制）。
  // 这是**探测顺序**：首项是该型号自己的默认档（LG290P 460800 / LC29H(BS) 115200），
  // 押错之后依次往下试。提示文案里的"优先试 X"取的就是首项。
  function currentModuleBauds() {
    var mod = selectedModule();
    return (mod && mod.bauds) ? mod.bauds : [];
  }

  // 当前型号**可以设置**的全部档位（降序，含不进探测序列的那些）。
  // 校验"手填值合不合法"必须用这张表：LC29H(BS) 的 3000000 是 UART2 的档，
  // 不会出现在探测序列里，但手填 3000000 依然合法，不能判成"不在官方档位"。
  function currentModuleLegalBauds() {
    var mod = selectedModule();
    if (mod && mod.set_bauds && mod.set_bauds.length) return mod.set_bauds;
    return currentModuleBauds();
  }

  // 这块型号上次"模块自报"的波特率（0 = 没有记录，别拿 0 当有效值）。
  // 选「自动探测」时用全库最近一次回读的值，提示里仍然标明它是"上次回读"。
  function currentModuleRememberedBaud() {
    var mod = selectedModule();
    if (mod && Number(mod.remembered_baud) > 0) return Number(mod.remembered_baud);
    var d = (STATE.options && STATE.options.defaults) || {};
    return Number(d.remembered_baud) > 0 ? Number(d.remembered_baud) : 0;
  }

  // 型号切换时刷新 datalist 候选与提示文案
  function refreshBaudOptions() {
    var list = $('cal-baud-list');
    // 候选给的是**可设置全集**（不是探测顺序）：用户手动填的时候要能看到
    // 3000000 这种"不进探测序列但合法"的档位。
    var bauds = currentModuleLegalBauds();
    if (!bauds.length) {
      // 自动探测：把已注册型号的档位并起来给个参考（去重、降序）
      var set = [];
      (((STATE.options || {}).modules || {}).all || []).forEach(function (m) {
        var table = (m.set_bauds && m.set_bauds.length) ? m.set_bauds : (m.bauds || []);
        table.forEach(function (b) { if (set.indexOf(b) < 0) set.push(b); });
      });
      bauds = set.sort(function (a, b) { return b - a; });
    }
    if (list) {
      list.innerHTML = bauds.map(function (b) {
        return '<option value="' + b + '"></option>';
      }).join('');
    }
    checkBaud();
  }

  // ---------------- 启动钩子横幅 ----------------
  // 上次进程被 kill -9 / 断电时，模块可能停在"切了基准站但坐标还是旧的"。
  // 这条横幅是用户动手前的最后一道提醒，所以**不自动消失**，只能手动关闭；
  // 关掉之后本次页面不再弹（点开新任务也不需要再看）。
  function renderBanner(snap) {
    var box = $('cal-banner');
    if (!box) return;
    var info = snap && snap.interrupted;
    if (!info || STATE.bannerClosed) { box.classList.add('is-hidden'); return; }
    var body = $('cal-banner-body');
    if (body) {
      body.textContent = t('cal.interrupted.body', {
        stage: info.stage_text || info.stage || '?',
        port: info.port || '—',
        module: info.module ? (info.module + (info.sn ? ' / ' + info.sn : '')) : '—'
      });
    }
    box.classList.remove('is-hidden');
  }

  // ---------------- 加载选项 ----------------
  function loadOptions() {
    return fetch('/api/calibration/options', { credentials: 'same-origin' })
      .then(function (res) {
        if (res.status === 401) { window.location.href = '/login'; throw new Error('unauth'); }
        return res.json();
      })
      .then(function (data) {
        if (!data || !data.ok) throw new Error((data && data.error) || 'options failed');
        STATE.options = data;
        fillDurationChoices(data);
        fillModules(data.modules);
        // 先定「可复用坐标」再渲染列表：列表渲染读的就是这条记录
        fillReuseHistory(data.reuse_history);
        fillHistory(data.history);
        if (data.defaults && data.defaults.cors_port && $('cal-cors-port')) {
          $('cal-cors-port').value = data.defaults.cors_port;
        }
        if (data.serial_port && $('cal-port') && !$('cal-port').value) {
          $('cal-port').value = data.serial_port;
        }
        STATE.ready = true;
        applyCapabilities();
        refreshBaudOptions();
        refreshModuleHint();
        if (data.running) startPolling();
        // 没有在跑也要先探一次：上一轮可能已经结束，而后端还留着它的
        // result / summary / logs。不探的话页面就是一片空白，用户只能
        // 靠记忆回想"上次标到底成没成"。poll() 拿到终态会自己补摘要，
        // 拿到"从没跑过"会安静地什么都不做。
        else if (!STATE.timer) poll();
        return data;
      })
      .catch(function (err) {
        if (err && err.message === 'unauth') return;
        showError(t('cal.err.options') + ': ' + err.message);
      });
  }

  // 型号下拉按**硬件角色**分三组：双模 / 单模 / 流动站。
  // 分组不是为了好看，是为了让用户一眼看出"这块模块能干什么"：
  // 单模型号下面不会出现 CORS 一键固定，流动站型号下面不会出现任何"固定"。
  //
  // `role` 与 `methods` 由后端驱动类声明（BaseDriver.ROLE + METHODS_BY_ROLE），
  // 前端**只读不猜**：后端说支持哪些方式，就放开哪些卡。这样"页面能点"
  // 与"后端会接受"永远同源，不会再出现"按钮能点但点了 400"。
  function fillModules(modules) {
    var select = $('cal-module');
    if (!select) return;
    var previous = select.value;
    select.innerHTML = '';
    var auto = document.createElement('option');
    auto.value = '__auto__';
    auto.textContent = t('cal.module.auto');
    select.appendChild(auto);

    var groups = [
      { label: t('cal.module.dual'), items: (modules && modules.dual_mode) || [] },
      { label: t('cal.module.single'), items: (modules && modules.single_mode) || [] },
      { label: t('cal.module.rover'), items: (modules && modules.rover_mode) || [] }
    ];
    groups.forEach(function (group) {
      if (!group.items.length) return;
      var og = document.createElement('optgroup');
      og.label = group.label;
      group.items.forEach(function (item) {
        var opt = document.createElement('option');
        opt.value = item.id;
        opt.textContent = item.name;
        opt.dataset.dual = item.dual_mode ? '1' : '0';
        opt.dataset.role = item.role || 'rover';
        // 方式表整份留在 DOM 上：型号切换后 computeLocks() 只按它判。
        opt.dataset.methods = (item.methods || []).join(',');
        // 能力表整份留在 DOM 上：applyCapabilities() 要按它决定"哪些块根本不该出现"。
        try { opt.dataset.caps = JSON.stringify(item.caps || {}); }
        catch (e) { opt.dataset.caps = '{}'; }
        og.appendChild(opt);
      });
      select.appendChild(og);
    });
    select.value = previous || '__auto__';
  }

  // 下拉框选「自动识别」时返回这块型号的元数据，前提是「读取模块信息」
  // 已经把真实型号认出来了（STATE.detectedModuleId）。
  //
  // 这里曾经直接 `return null`，真机上踩得很惨：用户留着默认的「自动识别」，
  // 点了「读取模块信息」，页面明明显示出 `Quectel LG290P`，接着选「CORS 一键固定」
  // 点开始 —— 后端 400「未知模块是「流动站（仅测量）」，不支持 CORS 一键固定」。
  // 原因是 buildPayload() 发出的是 `module: null`，后端按最保守的 role='rover'
  // 处理，直接把 cors 拒了。**识别结果只拿去渲染了，没有回写给请求用。**
  //
  // 自动识别的语义是"别让我选，你自己认"，那认出来之后就该按认出来的型号办事。
  function selectedModule() {
    var el = $('cal-module');
    var id = el ? el.value : AUTO_ID;
    if (id === AUTO_ID && STATE.detectedModuleId) id = STATE.detectedModuleId;
    var all = (STATE.options && STATE.options.modules && STATE.options.modules.all) || [];
    for (var i = 0; i < all.length; i++) { if (all[i].id === id) return all[i]; }
    return null;
  }

  // 下拉框当前**真实**选中的值（'__auto__' 或具体型号 id）。
  // 请求体里要发的是这个，不是 selectedModule()：自动识别时后者已经是
  // 识别出来的型号，把它当"用户手选的型号"发出去，就丢掉了"按实测再认一次"
  // 这层意思 —— 换了模块但没重读信息时，反而会用旧型号去卡新一轮识别。
  function pickedModuleId() {
    var el = $('cal-module');
    var id = el ? el.value : AUTO_ID;
    return id === AUTO_ID ? null : id;
  }

  // 当前型号的能力表。自动识别（__auto__）时返回 null，
  // 表示"还不知道是什么模块"——此时一律按最保守处理。
  function selectedCaps() {
    var mod = selectedModule();
    return mod ? (mod.caps || {}) : null;
  }

  // 自动识别时 mod 为 null，这里用"先放开"的策略：识别不出模块不该等于
  // "什么都不能做"，真要跑起来后端会按实际识别到的型号再拦一次。
  //
  // 判据不能用 `command_count`：那是"PQTM/PAIR 文本命令条数"，只有 Quectel 系
  // 驱动才有值。u-blox ZED-F9P 全走 UBX 二进制（`_valset/_valget`），`CMD = {}`，
  // 于是 command_count 恒为 0 —— 一旦拿它当总闸，F9P 在页面上四张卡全部置灰，
  // 用户根本点不下去（真机上只能靠绕过页面的脚本跑）。
  //
  // 改为**看能力位**：只要能算出"这条路需要的能力有没有"，就跟协议是文本还是
  // 二进制无关。下面四个判据与后端 `_reject_without_caps` / `_reject_write_*`
  // 逐条对齐（见 computeLocks）。command_count 只在"能力位全是 undefined 的
  // 空壳驱动"上还留着一层兜底价值，这里用 hasAnyCap 表达同一件事。
  function canWrite() {
    var mod = selectedModule();
    if (!mod) return true;
    var caps = mod.caps || {};
    // 能力表有明确置位（true/false 都算）→ 说明驱动真的声明了能力，可写。
    if (caps.fixed === true || caps.svin === true || caps.set_mode === true) return true;
    // 能力表是空壳（三者都 undefined）：退回命令集判据，避免把 unknown 驱动放开。
    return Number(mod.command_count) > 0;
  }

  function setLock(lockId, text) {
    var box = $(lockId);
    if (!box) return;
    if (text) {
      box.textContent = text;
      box.classList.remove('is-hidden');
    } else {
      box.textContent = '';
      box.classList.add('is-hidden');
    }
  }

  // 选中型号支持的方式集合。返回 null 表示"自动识别"（还不知道是什么模块）。
  //
  // 这是页面灰化的**唯一真相源**，与后端 `driver_manager.methods_of()` 同源：
  // 后端 `api_calibration_start()` 用同一张表拦请求，所以这里放开的一定能跑，
  // 这里锁掉的连点都点不下去。
  //
  // 三个角色对应的方式表（BaseDriver.METHODS_BY_ROLE）：
  //   dual   → cors / survey_in / manual / external_rover
  //   single → survey_in / manual
  //   rover  → external_rover
  // 未知模块（后端 role_of → 'rover'）**不是** dual —— 否则页面会亮出
  // "一键固定"，点下去必然卡在"切基准站"那一步。
  function selectedMethods() {
    var mod = selectedModule();
    if (!mod) return null;
    // 后端老版本可能没带 methods，退回按角色推一遍，避免整页四张卡全灰。
    if (Array.isArray(mod.methods)) return mod.methods.slice();
    var role = mod.role || (mod.dual_mode ? 'dual' : 'single');
    if (role === 'dual') return ['cors', 'survey_in', 'manual', 'external_rover'];
    if (role === 'single') return ['survey_in', 'manual'];
    return ['external_rover'];
  }

  // ---------------- 能力锁 ----------------
  // 每个方式卡片按"后端到底会拦什么"来决定锁不锁。锁定比后端严 → 用户被白白挡住；
  // 锁定比后端松 → 点下去才 400。这里逐条对着 api_calibration.start_* 写。
  //
  // **第一层判据是角色方式表**（selectedMethods），不是能力位：
  //   - 角色表说这块模块不支持 cors，就直接锁，理由写清楚"它是单模/流动站"；
  //   - 角色表说支持，再往下查能力位兜底（同一颗芯片的不同固件版本能力可能不同）。
  // 这个顺序很重要：先讲"你是单模模块"，比先讲"你缺 set_mode 位"对用户有用得多。
  function computeLocks() {
    var mod = selectedModule();
    var caps = selectedCaps() || {};
    var writable = canWrite();
    var locks = { cors: '', survey_in: '', manual: '', external_rover: '' };
    // 真的不知道是什么模块（自动识别 + 还没点过「读取模块信息」）：全部放开，
    // 后端按实际识别到的型号再拦一次。识别出来了就走下面完整的角色/能力闸门 ——
    // 否则「自动识别 + CORS」这条路前端一片绿灯、后端点下去就 400。
    if (!mod) return locks;

    var name = mod.name || mod.id;
    var allowed = selectedMethods() || [];
    var role = mod.role || (mod.dual_mode ? 'dual' : 'single');
    var roleLabel = mod.role_label || '';

    // 0) 角色闸门：型号能力表里没有这个方式 → 直接锁，理由用角色说。
    //    这与后端 start_* 开头的 `if method not in methods_of(module)` 一一对应。
    if (allowed.indexOf('cors') < 0) {
      locks.cors = role === 'single'
        ? t('cal.lock.cors_single', { name: name })
        : t('cal.lock.cors_role', { name: name, role: roleLabel || t('cal.role.rover') });
    }
    if (allowed.indexOf('survey_in') < 0) {
      locks.survey_in = t('cal.lock.svin_role', { name: name, role: roleLabel || t('cal.role.rover') });
    }
    if (allowed.indexOf('manual') < 0) {
      locks.manual = t('cal.lock.manual_role', { name: name, role: roleLabel || t('cal.role.rover') });
    }
    if (allowed.indexOf('external_rover') < 0) {
      locks.external_rover = t('cal.lock.measure_role', { name: name, role: roleLabel || t('cal.role.single') });
    }

    // 1) 命令集闸门：没命令集就什么都发不出去（只测不写除外，它不写 NVM）。
    //    能力位全是 undefined 的空壳驱动（如 passive）会被当成支持 —— 正是
    //    "按钮能点但点了报错"的来源，这层不能省。
    if (!writable) {
      locks.cors = locks.cors || t('cal.lock.manual', { name: name });
      locks.survey_in = locks.survey_in || t('cal.lock.manual', { name: name });
      locks.manual = locks.manual || t('cal.lock.manual', { name: name });
    }

    // 2) 能力位闸门：逐条对齐后端。
    //   start_write     → 先命令集，再 fixed（`_reject_write_without_fixed`）
    //   start_survey_in → 先命令集，再**只**看 svin（不查 fixed）
    //   start_cors      → 复用分支 fixed+set_mode；正常分支再加 dual_mode
    if (!locks.cors) {
      if (caps.fixed === false) locks.cors = t('cal.lock.cors_fixed', { name: name });
      else if (caps.set_mode !== true) locks.cors = t('cal.lock.cors_set_mode', { name: name });
    }
    // 自收敛不能顺手把 fixed 也加上，否则"只能自收敛、不能立即写固定坐标"的
    // 型号会被前端误锁（后端 start_survey_in 只查 svin）。
    if (!locks.survey_in && caps.svin !== true) {
      locks.survey_in = t('cal.lock.svin', { name: name });
    }
    if (!locks.manual && caps.fixed === false) {
      locks.manual = t('cal.lock.manual_caps', { name: name });
    }
    return locks;
  }

  function applyCapabilities() {
    var locks = computeLocks();
    var radios = document.querySelectorAll('input[name="cal-method"]');
    Array.prototype.forEach.call(radios, function (radio) {
      var method = radio.value;
      var locked = !!locks[method];
      radio.dataset.capDisabled = locked ? '1' : '0';
      radio.disabled = locked || STATE.running;
      var card = radio.closest('.cal-method');
      if (card) card.classList.toggle('is-disabled', locked);
      setLock('m-' + card_key(method) + '-lock', locks[method]);
    });
    // 当前选中的方式被锁住了 → 落到第一个可用的方式上。
    // 先执行回退再判断"是否有选中"，否则被锁的那张卡的 checked 还留在 DOM 上，
    // 加上新选中的那张就成了两个 checked —— 后续 currentMethod() 取到的是
    // 文档顺序里靠前的那个，也就是被锁的那一个。
    var checked = document.querySelector('input[name="cal-method"]:checked');
    if (!checked || checked.disabled) {
      var order = ['manual', 'survey_in', 'cors', 'external_rover'];
      for (var i = 0; i < order.length; i++) {
        var target = document.querySelector(
          'input[name="cal-method"][value="' + order[i] + '"]');
        if (target && !target.disabled) {
          // 单选取值：先把同组全部取消，再点亮目标，不依赖浏览器的默认互斥行为
          Array.prototype.forEach.call(radios, function (r) { r.checked = false; });
          target.checked = true;
          break;
        }
      }
    }
    syncMethod();
  }

  // 方式值 → DOM 卡片 id 后缀。survey_in 的卡片叫 m-svin，
  // external_rover 的卡片叫 m-measure：DOM 里用短名是为了可读，这里做一次映射。
  function card_key(method) {
    if (method === 'survey_in') return 'svin';
    if (method === 'external_rover') return 'measure';
    return method;
  }

  // 方式值 → i18n 键。同一个映射，因为字典里用的是短名（cal.method.svin）。
  // 散落在两处容易漏改，所以统一走这个函数。
  function methodLabelKey(method) {
    if (method === 'survey_in') return 'cal.method.svin';
    if (method === 'external_rover') return 'cal.method.measure';
    return 'cal.method.' + method;
  }

  // ---------------- 方式切换 ----------------
  function currentMethod() {
    var checked = document.querySelector('input[name="cal-method"]:checked');
    return checked ? checked.value : 'cors';
  }

  function currentCoordSource() {
    var checked = document.querySelector('input[name="cal-coord-src"]:checked');
    return checked ? checked.value : 'typing';
  }

  // 本次是否走「复用历史坐标」分支。只有 cors / manual 两个方式才有这条分支：
  //   cors   + 复用 = A1 复用分支（不连 CORS，但仍是 cm 级、仍要 1005 校验）
  //   manual + 复用 = 把旧值当用户输入回放（arp 直通）
  // survey_in 的坐标由固件算，external_rover 根本不写，两者都没有复用的语义。
  // 注意：manual 下"选了历史"必须有记录才算数 —— 否则页面上写着"复用"，
  // buildPayload 却拿不到坐标，点开始就报"请填写坐标"。
  function reuseActive() {
    if (STATE.method === 'cors') return STATE.corsSource === 'history' && !!STATE.reuseHistory;
    if (STATE.method === 'manual') {
      return STATE.coordSource === 'history' && !!STATE.reuseHistory;
    }
    return false;
  }

  function syncMethod() {
    STATE.method = currentMethod();
    STATE.coordSource = currentCoordSource();
    if (STATE.method !== 'cors') STATE.corsSource = 'measure';

    var method = STATE.method;
    var caps = selectedCaps() || {};
    var mod = selectedModule() || {};
    var reuse = reuseActive();

    // --- 方式卡片高亮 ---
    Array.prototype.forEach.call(document.querySelectorAll('.cal-method[data-method]'),
      function (card) {
        var radio = card.querySelector('input[name="cal-method"]');
        card.classList.toggle('is-active', !!(radio && radio.checked));
      });

    // --- 坐标来源卡片高亮（只在该块可见时有意义） ---
    Array.prototype.forEach.call(document.querySelectorAll('.cal-method[data-src]'),
      function (card) {
        var radio = card.querySelector('input[name="cal-coord-src"]');
        card.classList.toggle('is-active', !!(radio && radio.checked));
      });

    // --- ① CORS 接入：cors（非复用）与 external_rover 都要 ---
    // 复用分支不连网 → 整块藏掉，免得用户以为账号还得填。
    var showCors = (method === 'cors' && !reuse) || method === 'external_rover';
    toggle('cal-cors-card', showCors);

    // --- ② Survey-in 设置 ---
    toggle('cal-svin-card', method === 'survey_in');

    // --- ③ 坐标来源：manual 全都要；cors 也保留历史列表（选"复用"时才展开） ---
    // cors 方式下这块只用于"从历史里挑一条来复用"，所以整块只在
    // 用户真选了复用、或本来就是 manual 时出现。
    var showCoord = (method === 'manual') || (method === 'cors' && reuse);
    toggle('cal-coord-card', showCoord);

    // manual 的方式选择器：三个来源都列出来，让用户看到"还能复用/先测"这回事
    var srcPicker = document.querySelector('.cal-coord-src');
    if (srcPicker) srcPicker.classList.toggle('is-hidden', method !== 'manual');
    // cors+复用时页面上不该再让他改来源（改了就退出复用了），
    // 想跳出复用走下面那个「不复用，重新跑一次测量」的按钮。
    if (method === 'cors' && reuse) {
      var srcRadio = document.querySelector('input[name="cal-coord-src"][value="history"]');
      if (srcRadio) srcRadio.checked = true;
      STATE.coordSource = 'history';
      if (srcPicker) srcPicker.classList.add('is-hidden');
    }

    toggle('cal-coord-typing', method === 'manual' && STATE.coordSource === 'typing');
    toggle('cal-coord-history', reuse);
    toggle('cal-coord-measure', method === 'manual' && STATE.coordSource === 'measure_first');
    // 退出复用的回头路：只有真的处在复用分支里才需要
    toggle('cal-exit-reuse', reuse);

    // --- ④ RTCM 输出：会写模块 且 模块能配报文 ---
    // 未知模块（自动识别）先放开；识别出来没有 rtcm_out 就整块不出。
    var canRtcm = writesModule(method) && (mod.id ? !!caps.rtcm_out : true);
    toggle('cal-rtcm-card', canRtcm);

    // --- ⑤ 历史来源可用性 ---
    var hasHistory = !!STATE.reuseHistory;
    var srcHist = $('src-history');
    if (srcHist) {
      var radio = srcHist.querySelector('input[name="cal-coord-src"]');
      if (radio) { radio.disabled = !hasHistory; }
      srcHist.classList.toggle('is-disabled', !hasHistory);
    }
    setLock('src-history-lock', hasHistory ? '' : t('cal.lock.no_history'));

    renderPlan();
    renderHistoryList();
    renderLockHints();
    renderRunPreview();
  }

  // 复用分支的一句"为什么这次不用连网"，贴在 CORS 方式卡下面
  function renderLockHints() {
    var hintBox = $('m-cors-hint');
    if (!hintBox) return;
    var reuse = reuseActive();
    var hints = [];
    if (STATE.method === 'cors' && STATE.corsSource === 'history') {
      hints.push(reuse ? t('cal.hint.reuse_active') : t('cal.hint.reuse_no_history'));
    }
    if (hints.length) {
      hintBox.innerHTML = hints.map(function (text) {
        return '<div class="m-warn">' + escapeHtml(text) + '</div>';
      }).join('');
      hintBox.classList.remove('is-hidden');
    } else {
      hintBox.innerHTML = '';
      hintBox.classList.add('is-hidden');
    }
  }

  // ---------------- 计划步骤条 ----------------
  // 把后端真正要跑的阶段提前摆出来：用户选完方式就能看到"这次会不会动我的模块"。
  // 阶段名与 service/writer 里的 STEPS 完全一致，文案直接复用 cal.stage.*。
  function planSteps() {
    var method = STATE.method;
    var reuse = reuseActive();
    var steps = [];
    if (method === 'external_rover') {
      // HOLD 是等数据面让出串口（后端 worker 里真实存在的一步），PREFLIGHT 是
      // 「检查模块输出」——两步以前没画出来，用户看到的步骤条比实际跑的少两条，
      // 卡在任一步时就对不上号。
      // **结尾没有 RESTART**：只测不写这条路不进写入链路，切流动站走的是
      // 「仅内存、不写 NVM」，收尾只把内存模式切回去，模块一次都不重启。
      // 以前这里画了 RESTART，等于告诉用户"这块模块会被重启"，与「不改模块」矛盾。
      steps = ['PREPARE', 'HOLD', 'OPEN', 'DETECT', 'BACKUP', 'PREFLIGHT',
        'ROVER_SETUP', 'NMEA_SETUP',
        'GGA_WAIT', 'CORS_CONNECT', 'FIX_WAIT', 'SETTLE', 'COLLECT', 'CORS_CLOSE',
        'INJECT'];
      return { steps: steps, skip: {} };
    }
    if (method === 'survey_in') {
      steps = ['PREPARE', 'HOLD', 'OPEN', 'DETECT', 'BACKUP', 'SVIN_CFG', 'SVIN_WAIT',
        'WRITE_COORD', 'BASE_SETUP', 'MSG_SETUP', 'SAVE', 'RESTART',
        'VERIFY_MODE', 'VERIFY_RTCM', 'INJECT'];
      return { steps: steps, skip: {} };
    }
    if (method === 'cors' && !reuse) {
      steps = ['PREPARE', 'HOLD', 'OPEN', 'DETECT', 'BACKUP', 'PREFLIGHT',
        'ROVER_SETUP', 'NMEA_SETUP',
        'GGA_WAIT', 'CORS_CONNECT', 'FIX_WAIT', 'SETTLE', 'COLLECT', 'CORS_CLOSE',
        'WRITE_COORD', 'BASE_SETUP', 'MSG_SETUP', 'SAVE', 'RESTART',
        'VERIFY_MODE', 'VERIFY_RTCM', 'INJECT'];
      return { steps: steps, skip: {} };
    }
    // cors 复用 / manual：一上来就写坐标，没有采集段，因此也不进 PREFLIGHT
    // （那是 CORS 采集链路的开场检查）。
    // 复用分支后端**仍会**推一条 COLLECT 阶段（samples=0），页面上划掉它
    // 正好把"本次跳过测量"这个事实画出来，而不是让用户以为没测就是没跑。
    steps = ['PREPARE', 'HOLD', 'OPEN', 'DETECT', 'BACKUP'];
    var skip = {};
    if (reuse) { steps.push('COLLECT'); skip['COLLECT'] = 1; }
    steps = steps.concat(['WRITE_COORD', 'BASE_SETUP', 'MSG_SETUP', 'SAVE', 'RESTART',
      'VERIFY_MODE', 'VERIFY_RTCM', 'INJECT']);
    return { steps: steps, skip: skip };
  }

  function renderPlan() {
    var box = $('cal-plan');
    if (!box) return;
    var info = planSteps();
    var method = STATE.method;
    var reuse = reuseActive();
    // 只有真正决定结果的那几步用强调色（写入 / 保存 / 1005 校验 / 采集本身），
    // 其余留灰 —— 不是"会不会动 NVM"：采集一个字节都不写，但它是整件事的核心。
    var keySteps = { 'WRITE_COORD': 1, 'SAVE': 1, 'VERIFY_RTCM': 1, 'COLLECT': 1 };
    var parts = [];
    if (!writesModule(method)) {
      parts.push('<span class="is-key">' + escapeHtml(t('cal.plan.readonly')) + '</span>');
    } else if (reuse) {
      parts.push('<span class="is-key">' + escapeHtml(t('cal.plan.reuse')) + '</span>');
    }
    info.steps.forEach(function (stage) {
      var cls = [];
      if (info.skip[stage]) cls.push('is-skip');
      else if (keySteps[stage]) cls.push('is-key');
      parts.push('<span class="' + cls.join(' ') + '">' +
        escapeHtml(stageLabel(stage)) + '</span>');
    });
    box.innerHTML = parts.join('');
  }

  // ---------------- 历史坐标列表 ----------------
  function fillHistory(items) {
    STATE.history = (items && items.length) ? items[0] : null;
    renderHistoryList();
  }

  function renderHistoryList() {
    var list = $('cal-hist-list');
    if (!list) return;
    var record = STATE.reuseHistory;
    if (!record) {
      list.innerHTML = '<div class="cal-muted">' + escapeHtml(t('cal.history.none')) +
        '<br>' + escapeHtml(t('cal.history.none_hint')) + '</div>';
      var hintBox = $('cal-hist-hint');
      if (hintBox) hintBox.textContent = '';
      return;
    }
    var accuracy = record.accuracy ? (Number(record.accuracy) * 100).toFixed(1) + ' cm' : '—';
    // 精度指标必须分开显示（§17 ⑤）：`raw_rms_*` 是**参与取值的那批样本**的
    // 离散度（= 这条坐标有多准），`source_spread*` 是整个数据源的（= 源在跳多少）。
    // 以前只露一个 `accuracy`，用户看到 2 cm 就以为"测得不准"，
    // 其实那是两簇相距 4 cm 撑出来的，主簇自己可能只有 4 mm。
    var quality = [];
    var mmText = function (v) {
      var n = Number(v);
      return isFinite(n) ? (n * 1000).toFixed(1) + ' mm' : '—';
    };
    if (record.raw_rms_h != null) {
      quality.push(t('cal.history.h') + ' ' + mmText(record.raw_rms_h));
    }
    if (record.raw_rms_v != null) {
      quality.push(t('cal.history.v') + ' ' + mmText(record.raw_rms_v));
    }
    if (record.cluster_ratio != null) {
      quality.push(t('cal.history.cluster') + ' ' +
        Math.round(Number(record.cluster_ratio) * 100) + '%');
    }
    if (record.source_spread != null &&
        (record.raw_rms_h == null || Number(record.source_spread) > Number(record.raw_rms_h) * 1.5)) {
      quality.push(t('cal.history.source') + ' ' + mmText(record.source_spread));
    }
    var confKey = 'cal.conf.' + String(record.coord_confidence || '');
    var confText = record.coord_confidence
      ? (hasKey(confKey) ? t(confKey) : record.coord_confidence) : '';
    if (confText) quality.push(t('cal.history.conf') + ' ' + confText);
    if (record.need_resurvey) quality.push(t('cal.history.resurvey'));
    var methodKey = 'cal.method.short.' + String(record.method || '');
    var methodText = hasKey(methodKey) ? t(methodKey) : (record.method || '—');
    // 两组坐标都显示：ECEF 是真正写进模块、真正播发出去的那组数，
    // 经纬高是人看坐标时唯一能核对的东西（"是不是这根天线"只能靠它）。
    // 库里两组同时保存（见 store._normalize_record 的双向换算），这里不必现算。
    var geo = (record.lat != null && record.lon != null)
      ? (Number(record.lat).toFixed(9) + ', ' + Number(record.lon).toFixed(9) +
         (record.height != null ? ' · ' + Number(record.height).toFixed(3) + ' m' : ''))
      : '';
    list.innerHTML =
      '<label class="cal-hist-item is-active" data-id="' + escapeHtml(record.id || '') + '">' +
        '<div class="h-main">' +
          '<div class="h-when">' + escapeHtml(record.created_at || '—') + '</div>' +
          '<div class="h-meta">' +
            escapeHtml(t('cal.history.method')) + '：' + escapeHtml(methodText) +
            ' · ' + escapeHtml(t('cal.history.accuracy')) + '：' + escapeHtml(accuracy) +
            ' · ' + escapeHtml(record.module_model || '—') +
          '</div>' +
          (geo ? '<div class="h-sn">' + escapeHtml(geo) + '</div>' : '') +
          (quality.length ? '<div class="h-sn">' + escapeHtml(quality.join(' · ')) + '</div>' : '') +
          '<div class="h-sn">' +
            'X ' + escapeHtml(fmtNum(record.ecef_x, 4, '')) +
            '  Y ' + escapeHtml(fmtNum(record.ecef_y, 4, '')) +
            '  Z ' + escapeHtml(fmtNum(record.ecef_z, 4, '')) +
          '</div>' +
          (record.module_sn ? '<div class="h-sn">SN ' + escapeHtml(record.module_sn) + '</div>' : '') +
        '</div>' +
      '</label>';
    var hint = $('cal-hist-hint');
    if (hint) {
      // 复用一句话的后果在 cors / manual 下完全不同，文案必须分开。
      var key = (STATE.method === 'cors') ? 'cal.history.hint.cors' : 'cal.history.hint.manual';
      hint.textContent = hasKey(key) ? t(key) : t('cal.history.hint');
    }
  }

  // 「可复用」的历史坐标：库内最近一条，不区分模块型号/SN（见 STATE.reuseHistory）。
  function fillReuseHistory(items) {
    STATE.reuseHistory = (items && items.length) ? items[0] : null;
    if (!STATE.reuseHistory && STATE.method === 'cors' && STATE.corsSource === 'history') {
      STATE.corsSource = 'measure';
    }
    if (!STATE.reuseHistory && STATE.coordSource === 'history') {
      STATE.coordSource = 'typing';
      var radio = document.querySelector('input[name="cal-coord-src"][value="typing"]');
      if (radio) radio.checked = true;
    }
    var histRadio = document.querySelector('input[name="cal-coord-src"][value="history"]');
    if (histRadio) histRadio.checked = !!STATE.reuseHistory && histRadio.checked;
    syncMethod();
  }

  // ---------------- 串口扫描 ----------------
  // 结果就地反馈：提示写在串口输入框下面的 field-hint，候选渲染成 chip 贴在旁边。
  // 全程不依赖浮层 —— 真机上"点了扫描没反应"就是浮层没弹、而错误又写在页面底部
  // 全局框里的结果。浮层仍然会顺手打开（键盘操作更快），但不再是唯一出口。
  function setPortHint(text, kind) {
    var hint = $('cal-port-hint');
    if (!hint) return;
    hint.textContent = text || '';
    hint.classList.remove('is-info', 'is-ok', 'is-error');
    if (kind) hint.classList.add(kind);
  }

  // **必须先 blur 再赋值**：`#cal-port` 挂着 `list="cal-port-list"`，浏览器会把
  // 这个输入框当成"正在输入"，赋值后自动弹出 datalist 候选浮层 —— 浮层正好盖住
  // 刚填进去的串口名，现场看到的就是"只弹了个列表、并没有选中"。
  // 波特率框也有 list，但它的候选（cal-baud-list）通常为空/不匹配，不弹，
  // 于是表现成"波特率填上了、串口没填"的不对称，正是这条 bug 的由来。
  // blur 让输入框退出编辑态，浮层不会弹；再赋值，值就是最终值。
  // 这两个接口的端口名**字段名不一样**，是个真坑：
  //   GET /api/serial_ports          → ports[].device
  //   GET /api/calibration/scan/status → ports[].port
  // 前端原先一律按 `device` 读，于是扫描结果表里端口名一列全是空白、
  // 点行时 `pickPort(undefined)` 把输入框写成了空字符串 —— 表现就是
  // "波特率填上了、串口名没填"，再点「读取模块信息」自然报"请先选择串口"。
  // 统一在这里取值，两个名字都认，后端以后改哪个都不会再断。
  function portName(p) {
    if (!p) return '';
    return String(p.device || p.port || '').trim();
  }

  // **换口必须重置"上一次的识别结果"**。串口的型号选择是"人告诉程序这个口上是什么"，
  // 它只对某一个口成立 —— 换了口，上一口的选择就变成了一条错误的断言。
  // 实测：先点 COM3（LG290P）→ 读取成功，再点 COM12 直接点「读取模块信息」，
  // 下拉框仍停在「Quectel LG290P」，后端按所选型号去问，于是报
  // "模块型号与所选不符：你选的是 lg290p，实际检测到 ZED-F9P" —— 用户没有任何
  // 操作失误，是页面把旧答案带过去了。
  // 所以 portChanged 时把型号退回「自动探测」，并清掉上一口的硬件信息卡。
  function resetModuleForPort() {
    var sel = $('cal-module');
    if (sel && sel.value && sel.value !== '__auto__') {
      sel.value = '__auto__';
      // 能力位、波特率候选、提示文案都挂在 change 上，必须补派发。
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      // 已经是自动探测也要刷一遍：提示文案里带着"上次回读的波特率"，
      // 那是上一个口的数据，不能留在新口上。
      applyCapabilities();
      refreshBaudOptions();
      refreshModuleHint();
    }
    // 硬件信息卡（固件 / SN / UART）描述的是上一个口上的模块，留着就是误导。
    var hw = $('cal-hw');
    if (hw) hw.innerHTML = '';
    STATE.hwTaskId = null;
    // 识别出的型号同理：新口上接的很可能是另一块板子，
    // 拿着旧型号去卡能力锁 / 拼请求体，比"不知道"更糟。
    STATE.detectedModuleId = '';
  }

  // 内部调用（扫描预填、点击结果行）时 `changed` 由这里自己判：值真变了才重置，
  // 重复点同一行不会把用户已经选好的型号清掉。
  function pickPort(device, opts) {
    var input = $('cal-port');
    if (!input) return;
    var prev = String(input.value || '').trim();
    var next = String(device || '').trim();
    var changed = prev !== next;
    if (document.activeElement === input) input.blur();
    input.value = next;
    if (changed && !(opts && opts.keepModule)) resetModuleForPort();
    // 联动都挂在 input/change 上（波特率提示、型号能力重算），必须补派发。
    // 这里派发的是合成事件，不会重新打开 datalist —— 只有真实键盘输入才会。
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    // 赋值是"看不见的操作"：值变了但用户的眼睛还停在下面那张结果表上。
    // 给输入框闪一下强调色，让"填进去了"这件事有视觉落点，而不是靠猜。
    flashFilled(input);
    renderPortChips();
  }

  // 让某个输入框闪一下（500ms 的边框高亮）。只动 class，不改值、不抢焦点。
  function flashFilled(el) {
    if (!el || !el.classList) return;
    el.classList.remove('is-filled-flash');
    // 强制回流，否则连续两次点击同一个框时动画不会重播
    void el.offsetWidth;
    el.classList.add('is-filled-flash');
    window.setTimeout(function () { el.classList.remove('is-filled-flash'); }, 700);
  }

  // 扫描结果的候选**不再画成一排 chip**。原先扫描结束会同时出现两处：
  // 输入框下面那排 COM1 / COM3 / COM12 小圆块，以及下面的扫描结果表 ——
  // 同一份数据的两种画法，现场反馈"重复、看着乱"。
  // 现在只留结果表：它每行都带家族判定、命中波特率与数据特征，点行即选用，
  // 信息量严格覆盖那排小块。函数保留原名与"填 datalist"的职责（契约测试
  // test_contract 断言了这个函数名与调用点），只是不再渲染 chip。
  function renderPortChips(items) {
    if (items) STATE.ports = items;
    var box = $('cal-port-chips');
    if (box) {
      box.innerHTML = '';
      box.classList.add('is-hidden');
    }
  }

  // ---------------- 串口扫描与协议嗅探 ----------------
  // 两件事分开：
  //   1) 先列系统串口（/api/serial_ports）—— 快，立刻能填满输入框；
  //   2) 再起协议嗅探（/api/calibration/scan）—— 慢，逐口 x 逐波特率试读，
  //      判出这块板子到底是不是 GNSS、以及它在哪个波特率上说话。
  //
  // 嗅探是**异步任务**：POST 返回 202 后靠轮询 /scan/status 推进。
  // 与标定任务完全隔离（独立线程、独立锁、不占串口租约、不受 30 秒看门狗管辖），
  // 所以扫描期间标定状态机不会动，扫描也不会被标定心跳超时误杀。
  function scanPorts() {
    if (otherOpBusy('scan')) { 
      // 两条线互斥：读取模块信息正占着串口时不能再扫（反之亦然）。
      // 文案写在端口输入框下面就地提示，而不是页面底部的全局错误框 ——
      // 后者离按钮隔着好几屏，现场看就是"点了没反应"。
      setPortHint(t('cal.err.op_busy'), 'is-error');
      showError(t('cal.err.op_busy'));
      return;
    }
    listSerialPorts().then(function (ok) {
      if (ok) sniffPorts();
    });
  }

  function listSerialPorts() {
    return fetch('/api/serial_ports', { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        var ports = (data && data.ports) || [];
        var list = $('cal-port-list');
        if (list) {
          list.innerHTML = '';
          ports.forEach(function (p) {
            var opt = document.createElement('option');
            opt.value = portName(p);
            opt.label = p.description || portName(p);
            list.appendChild(opt);
          });
        }
        renderPortChips(ports);
        var input = $('cal-port');
        if (!ports.length) {
          setPortHint(t('cal.err.no_ports'), 'is-error');
          return false;
        }
        // 只有一个口就直接填上：现场换完 USB 口最想看到的就是"它认到了"。
        if (input && ports.length === 1 && !input.value.trim()) pickPort(portName(ports[0]));
        return true;
      })
      .catch(function (err) {
        setPortHint(t('cal.err.ports') + ': ' + err.message, 'is-error');
        return false;
      });
  }

  // 已扫到的结果按端口名索引，重扫时整体替换而不是追加。
  function sniffPorts() {
    STATE.scan = { running: true, ports: [], progress: [], summary: null, error: '' };
    // 进度与「读取模块信息」共用同一套 UI（见 progEls/startProg）：
    // 转圈 + 已用时间 + 一条按比例推进的进度条。真机上扫描 3 个口全无应答要
    // 40 秒以上，而这边原先只有一行会变的文字 —— 用户以为点空了。
    SCAN_UI.started = 0;
    SCAN_UI.failed = false;
    // 有手填波特率 → 只试那一档，总时长除以档数；否则按 9 档估。
    // 口数这时还不知道（要 POST 回来），用 1 个口起算，后端首帧到达后再按实测修正。
    SCAN_UI.budget = (($('cal-baud') && /^\d+$/.test($('cal-baud').value.trim())) ? 6 : 30);
    startProg('scan', t('cal.scan.started'));
    // **不要在这里单独禁用按钮**：`STATE.scan.running` 已经是"扫描在跑"的唯一
    // 真值，两个按钮的可用状态统一由 `syncOpButtons()` 推导。
    // 原先这里直接禁用按钮、收尾只放开另一个（`setOpBusy('scan', false)`），
    // 扫描按钮就再也没被放开 —— 而唯一会放开它的 `setFormLocked()` 只在状态
    // 轮询里跑，没有标定任务时轮询早就停了。现场表现就是"扫完必须刷新页面"。
    syncOpButtons();
    renderScanPanel();
    setPortHint(t('cal.scan.started'), 'is-info');

    var body = {
      // 端口留空 = 让后端把系统里所有串口都试一遍。
      // 波特率留空 = 统一按高到低扫全集（后端 SCAN_BAUDS）。
      // 用户手填了波特率就只试那一档 —— 现场明确知道速率时能省掉大半时间。
      ports: [],
      active: true
    };
    var typed = ($('cal-baud') && $('cal-baud').value.trim()) || '';
    if (typed && /^\d+$/.test(typed)) body.bauds = [parseInt(typed, 10)];

    return fetch('/api/calibration/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body)
    })
      .then(function (res) {
        if (res.status === 401) { window.location.href = '/login'; throw new Error('unauth'); }
        return res.json().then(function (data) { return { status: res.status, data: data }; });
      })
      .then(function (r) {
        if (!r.data || !r.data.ok) {
          var msg = (r.data && r.data.error) || ('HTTP ' + r.status);
          throw new Error(msg);
        }
        // 202 里已经带了第一帧状态，先画出来再开始轮询 —— 否则用户会盯着
        // 一个空面板等 1 秒，看起来像"点了没反应"。
        if (r.data.scan) {
          maskScanPayload(r.data.scan);
          renderScanPanel();
        }
        pollScan();
      })
      .catch(function (err) {
        if (err && err.message === 'unauth') return;
        STATE.scan.running = false;
        STATE.scan.error = err.message;
        SCAN_UI.lastElapsed = progElapsed('scan');
        failProg('scan', err.message);
        SCAN_UI.started = 0;
        syncOpButtons();
        renderScanPanel();
        setPortHint(t('cal.scan.failed') + ': ' + err.message, 'is-error');
      });
  }

  function pollScan() {
    if (!STATE.scan || !STATE.scan.running) return;
    fetch('/api/calibration/scan/status', { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (res) {
        if (!res || !res.ok) return;
        // 后端把扫描状态包在 `scan` 里（与 /scan 的 202 响应同构），
        // 顶层留给 ok / error 这类通用字段。
        var data = res.scan || {};
        maskScanPayload(data);
        renderScanPanel();
        if (data.running) {
          // 第一帧里带了系统串口清单，可以按"口数 × 档数"重新估总时长 ——
          // 1 个口 30 秒和 5 个口 150 秒，进度条用同一个分母会一直贴边。
          var available = (data.available || []).length || 1;
          var perPort = (SCAN_UI.budget <= 10) ? 6 : 30;
          SCAN_UI.budget = Math.max(6, available * perPort);
          // 1 秒一轮：一轮扫描最长也就 (口数 x 档数 x 1.5s)，逐轮刷新足够，
          // 再密就是无意义地刷 DOM。
          window.setTimeout(pollScan, 1000);
          return;
        }
        STATE.scan.running = false;
        onScanDone(data);
      })
      .catch(function (err) {
        STATE.scan.running = false;
        STATE.scan.error = err.message;
        SCAN_UI.lastElapsed = progElapsed('scan');
        failProg('scan', err.message);
        SCAN_UI.started = 0;
        syncOpButtons();
        renderScanPanel();
      });
  }

  function maskScanPayload(data) {
    if (!STATE.scan) return;
    STATE.scan.running = !!data.running;
    STATE.scan.ports = data.ports || [];
    STATE.scan.progress = data.progress || [];
    STATE.scan.summary = data.summary || null;
    // `available` 是系统串口清单，首屏也能用（不用等扫描）。
    if (data.available && data.available.length) renderPortChips(data.available);
  }

  // 扫描结束：把结果摊开，同时把"最像 GNSS 的那个口"预填进去。
  // 预填规则刻意保守 —— 只在**当前输入框是空**的时候填，绝不覆盖用户手填的值。
  // 波特率同理，而且多加一条：**端口没有波特率概念时不预填**（USB 直连 CDC），
  // 填进去只会让人以为"这就是它的波特率"。
  function onScanDone(data) {
    // **必须先把耗时取出来，再动 `SCAN_UI.started`**。`finishProg` 内部是靠
    // `progElapsed()` 算耗时的，而 `progElapsed()` 读的就是 `started`；
    // 原先这里先 `started = 0` 再调 `finishProg`，于是无论扫了 3 秒还是 33 秒，
    // 收尾文案永远是"耗时 0.0 秒"、计时器也被写成 '0.0s'（实测 33.5 秒的扫描
    // 显示成 0.0 秒，正是这条）。failProg 那条路径用的 `SCAN_UI.lastElapsed`
    // 就是这个道理的现成先例。
    var elapsed = progElapsed('scan');
    renderScanPanel();
    var ports = data.ports || [];
    var gnss = ports.filter(function (p) { return p.family === 'gnss'; });
    // 预填时**优先挑读到 RTCM 的口**：同样都判定为 GNSS，能差分的那个
    // 才可能是这次要标的模块。只在输入框为空时预填，用户随时能改 ——
    // 这是排序建议，不是替他决定。
    var target = gnss.filter(function (p) { return p.class === 'rtk'; })[0] || gnss[0];
    var input = $('cal-port');
    if (input && !input.value.trim() && target) {
      pickPort(portName(target));
      if (target.baud && target.baud_fixed !== false
          && $('cal-baud') && !$('cal-baud').value.trim()) {
        $('cal-baud').value = String(target.baud);
        $('cal-baud').dispatchEvent(new Event('input', { bubbles: true }));
      }
    }
    // 先放开互斥，再写结果提示 —— 顺序反了的话，「读取模块信息」会在
    // 最后一行提示写完之前就变回可点，用户正好卡在这个缝里看到两个都可点。
    SCAN_UI.started = 0;
    STATE.scan.running = false;
    syncOpButtons();
    finishProg('scan', t('cal.scan.done_ok', { elapsed: elapsed.toFixed(1) }), 1800, elapsed);
    if (!ports.length) {
      setPortHint(t('cal.scan.empty'), 'is-info');
      return;
    }
    var n = gnss.length;
    // RTK 的口单独数：`class` 是后端按"CRC 通过的 RTCM 帧"判的，
    // 跟 GNSS 不是一回事 —— 只发 NMEA 的廉价模块也算 GNSS，但它不能差分。
    var rtk = ports.filter(function (p) { return p.class === 'rtk'; }).length;
    var verify = (data.summary && data.summary.verify) || [];
    var rateFree = (data.summary && data.summary.rate_free) || [];
    var hint = t('cal.scan.done', { total: ports.length, gnss: n, rtk: rtk });
    if (verify.length) hint += ' ' + t('cal.scan.done.verify', { ports: verify.join('、') });
    if (rateFree.length) hint += ' ' + t('cal.scan.done.ratefree', { ports: rateFree.join('、') });
    // 认出厂牌就报一下：这行是"扫完看结果"的第一眼，比让用户自己
    // 逐行去读「厂家」列要快。
    var vendorNames = (data.summary && data.summary.vendor_names) || [];
    if (vendorNames.length) {
      hint += ' ' + t('cal.scan.done.vendors', { vendors: vendorNames.join('、') });
    }
    // 耗时也留在常驻提示里：进度条 1.8 秒后收起，用户回头看就只剩这一行。
    // 扫描慢是常态（3 个口全无应答 30 秒+），把数字留着才好判断"要不要换个口再扫"。
    hint += ' ' + t('cal.scan.done.elapsed', { elapsed: elapsed.toFixed(1) });
    setPortHint(hint, n ? 'is-ok' : 'is-info');
    // 扫描结果是判定「读取模块信息」能否点的唯一依据，扫完立刻重算
    refreshDetectGate();
  }

  function renderScanPanel() {
    var panel = $('cal-scan-panel');
    if (!panel || !STATE.scan) return;
    var scan = STATE.scan;

    var title = $('cal-scan-title');
    if (title) {
      title.textContent = scan.running
        ? t('cal.scan.running')
        : t('cal.scan.result', { n: (scan.ports || []).length });
    }

    // 进度文字由共用进度条（SCAN_UI）负责显示与计时，这里只在扫描中
    // 把最新一条事件推进去。停止/失败时不写 —— 已经由 failProg 写了终态文案，
    // 再覆盖成最后一条 `passive @ 9600` 会让"失败了"看起来像"还在扫"。
    if (scan.running) {
      var last = (scan.progress || []).slice(-1)[0];
      setProgMessage('scan', formatScanEvent(last));
    }

    var wrap = $('cal-scan-table');
    if (wrap) wrap.innerHTML = scanTableHtml(scan.ports || []);
    panel.classList.remove('is-hidden');
  }

  // 家族 → 显示名。后端只给 `family` 键，文案在前端；
  // 后端新加家族时这里兜底显示原始键，不至于渲染成空白行。
  function familyLabel(family) {
    var key = 'cal.scan.fam.' + family;
    var text = t(key);
    return (text === key) ? (family || '—') : text;
  }

  // 判定列：优先用后端给的 `class`（rtk / gnss），没有才退回 family。
  //
  // 为什么要分两层：`family='gnss'` 把"接 RTCM 差分的基准站"和"九块九的
  // 单点定位 GPS 模块"混成同一档，用户照着挑，挑到便宜货要等标定跑到
  // `FIX_WAIT` 才发现它不会差分。`class` 的判据只认一条硬证据 ——
  // CRC 通过后的 RTCM 帧。没有差分数据就老实说"GNSS 模块"，不吹成 RTK。
  //
  // 但反过来也不能踩坑：2026-11-13 真机接入一块**流动站**（$QXATT/$QXDRS，
  // 4 星座 10 Hz，单点解 19 星）时它一帧 RTCM 都不播，扫描器判 `gnss`。
  // 曾经这里的 tooltip 写"可能只支持单点定位"，等于把一台流动站说成廉价
  // 单点模块 —— `class` 描述的是"这个口此刻在播什么"，不是"这块硬件能不能
  // 差分"。tooltip 已改成点明这层区别。
  function verdictLabel(p) {
    var cls = p && p.class;
    if (cls) {
      var key = 'cal.scan.class.' + cls;
      var text = t(key);
      if (text !== key) return text;
    }
    return familyLabel(p && p.family);
  }

  // 判定列的 tooltip：把"凭什么这么判"讲清楚。
  // 用户最常问的就是"你凭什么说它是 RTK"——答案只有一条：CRC 通过的 RTCM 帧。
  function verdictTip(p) {
    var cls = p && p.class;
    if (!cls) return '';
    var key = 'cal.scan.class.' + cls + '.tip';
    var text = t(key);
    return (text === key) ? '' : text;
  }

  // 当前输入框里的端口在扫描结果中的家族判定。
  // 返回 null 表示"扫描结果里没有这个口"（用户手填/还没扫过）——
  // 这种情况不能拦：手填 COM12 而扫描只覆盖了 COM1~11 是常态。
  function portFamily(port) {
    var name = String(port || '').trim();
    if (!name || !STATE.scan || !STATE.scan.ports) return null;
    var ports = STATE.scan.ports;
    for (var i = 0; i < ports.length; i++) {
      if (portName(ports[i]) === name) return ports[i].family || 'unknown';
    }
    return null;
  }

  // 「读取模块信息」的准入闸门。
  // 为什么必须挡：未知/噪声口上识别一次要"自动探测 + 8 档波特率 × 6 个驱动"，
  // 真机实测 45 秒起步，而结果必然是"模块无应答"。后端已经在诊断任务里
  // 关掉了默认枚举，这里再把按钮禁掉，用户就不会白点。
  // 逃生口：勾「仍要强制识别」= 显式告诉后端 force=true，允许全档枚举。
  function refreshDetectGate() {
    var btn = $('cal-detect');
    if (!btn) return;
    var port = ($('cal-port') && $('cal-port').value.trim()) || '';
    var fam = portFamily(port);
    var blocked = !!fam && fam !== 'gnss';
    var box = $('cal-force-detect');
    var wrap = $('cal-force-wrap');
    if (wrap) wrap.hidden = !blocked;
    if (!blocked) {
      if (box) box.checked = false;
    }
    var force = !!(blocked && box && box.checked);
    var lock = blocked && !force;
    btn.dataset.famBlocked = lock ? '1' : '0';
    if (lock) {
      btn.disabled = true;
      // 原因只放 title 与旁边的强制开关上，不抢 `cal-port-hint` ——
      // 那行要留给扫描汇总（口数/耗时），否则用户扫完看不到结果。
      btn.title = t('cal.err.detect_not_gnss', { fam: familyLabel(fam) });
    } else {
      // 家族闸门只是"这一格该不该点"的一半，另一半是串口占用：
      // 扫描/识别任一条线在跑，这个按钮都必须锁住。以前这里只判 `STATE.running`
      // （标定任务），于是"扫描在跑时它还是亮的"要靠 `setOpBusy()` 补一刀 ——
      // 两处各写一半，谁先谁后就决定了按钮亮不亮。现在统一从这里推导。
      btn.disabled = opLocked();
      btn.title = '';
    }
  }

  // 端口类型 → 显示名。`usb-cdc` 是"这口没有波特率"的那一类，
  // 必须让人看见 —— 否则用户会照着它给的占位数字去填，然后一辈子对不上。
  function kindLabel(kind) {
    var key = 'cal.scan.kind.' + kind;
    var text = t(key);
    return (text === key) ? '' : text;
  }

  // 波特率那一格的文案。三种情况分开：
  //   没有波特率概念（USB CDC） → 显示"直连"，不显示数字
  //   扫到了                     → 数字 + 置信度标记（? 表示建议复核）
  //   没扫到                     → 破折号
  function baudCellHtml(port) {
    if (port.baud_fixed === false) {
      return '<span class="scan-ratefree">' + escapeHtml(t('cal.scan.baud.ratefree')) + '</span>';
    }
    if (!port.baud) return '—';
    var html = escapeHtml(String(port.baud));
    if ((port.confidence || 'low') !== 'high') {
      html += ' <span class="scan-warn" title="'
        + escapeHtml(t('cal.scan.confirm.tip')) + '">?</span>';
    }
    return html;
  }

  // 进度事件 → 一行人话。后端事件形如
  //   {port:'COM3', baud:115200, phase:'passive'|'active'|'start'|'fallback'}
  // phase 的语义：
  //   start    → 开始处理这个口
  //   passive  → 正在被动试读某一档（不发任何字节）
  //   fallback → 被动没读到，改走主动询问
  //   active   → 正在主动询问某一档
  function formatScanEvent(ev) {
    if (!ev) return '';
    if (typeof ev === 'string') return ev;
    var port = ev.port || '';
    var baud = ev.baud ? String(ev.baud) : '';
    if (ev.phase === 'start') return t('cal.scan.ev.start', { port: port });
    if (ev.phase === 'fallback') return t('cal.scan.ev.fallback', { port: port });
    if (ev.phase === 'active') return t('cal.scan.ev.active', { port: port, baud: baud });
    return t('cal.scan.ev.passive', { port: port, baud: baud });
  }

  // 厂牌那一格。三种情况分开：
  //   认出了厂牌 → 厂牌名（带证据 tooltip），能带出型号就再挂一行小字
  //   认不出     → 破折号 + 说明"为什么认不出"，别让人以为是界面没做
  //   USB 口     → 同上，厂牌只能从数据里认，VID 只能说明转接芯片是谁家的
  function vendorCellHtml(port) {
    var vendor = port.vendor || '';
    if (!vendor) {
      return '<span class="scan-vendor is-unknown" title="'
        + escapeHtml(t('cal.scan.vendor.unknown.tip')) + '">—</span>';
    }
    var html = '<span class="scan-vendor" title="' + escapeHtml(vendorTip(port)) + '">'
      + escapeHtml(vendor) + '</span>';
    var model = port.model || '';
    if (model) {
      // 型号常常直接包含厂名（RTCM-1033 的 `Quectel  LG290P`），
      // 已有厂牌在上头时就把重复的前缀去掉，避免一行里写成
      // "Quectel / Quectel LG290P"。
      var short = model.replace(new RegExp('^' + vendor.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*', 'i'), '');
      if (short && short !== model) model = short;
      html += '<div class="scan-model">' + escapeHtml(model) + '</div>';
    }
    return html;
  }

  // 厂牌证据：后端把"凭什么认为是这家的"逐条列好了，原样显示。
  // 加一句置信度说明 —— low 的那条是"文本里出现了厂名"，可能是集成商写的，
  // 不标注的话用户会拿它当定论。
  function vendorTip(port) {
    var lines = (port.vendor_evidence || []).slice();
    if (!lines.length) lines = [t('cal.scan.vendor.novidence')];
    var conf = port.vendor_confidence || '';
    if (conf === 'low') lines.push(t('cal.scan.vendor.low'));
    else if (conf === 'medium') lines.push(t('cal.scan.vendor.medium'));
    return lines.join('\n');
  }

  function scanTableHtml(ports) {
    if (!ports.length) {
      return '<p class="scan-note">' + escapeHtml(t('cal.scan.empty_row')) + '</p>';
    }
    var current = ($('cal-port') && $('cal-port').value.trim()) || '';
    var rows = ports.map(function (p, idx) {
      // 家族判定、波特率、数据特征都在行上；点击行为由 bindScanTable 委托。
      // 已在输入框里的那个口高亮成 is-current，扫完预填时用户能对得上。
      var feats = (p.features || []).map(function (f, i) {
        var key = 'cal.scan.feature.' + f;
        var label = t(key);
        if (label === key) label = f;
        return '<span class="scan-feat' + (i === 0 ? ' is-primary' : '') + '">'
          + escapeHtml(label) + '</span>';
      }).join('');
      var detail = (p.description || '');
      if (p.vid && p.pid) detail += (detail ? ' · ' : '') + p.vid + ':' + p.pid;
      var kind = kindLabel(p.kind);
      if (kind) detail += (detail ? ' · ' : '') + kind;
      // 厂牌在上一列单独显示，这里不再前缀一遍（否则一行里出现两次）。
      var rowClass = 'scan-row scan-fam-' + escapeHtml(p.class || p.family || 'unknown');
      if ((p.confidence || 'low') !== 'high' && p.family === 'gnss') rowClass += ' is-unverified';
      if (portName(p) === current) rowClass += ' is-current';
      return ''
        + '<tr class="' + rowClass + '" data-idx="' + idx + '" title="'
        + escapeHtml(t('cal.scan.row.tip')) + '">'
        + '<td><span class="scan-dev"><span class="scan-dot"></span>'
        + escapeHtml(portName(p)) + '</span></td>'
        + '<td class="scan-baud">' + baudCellHtml(p) + '</td>'
        + '<td class="scan-verdict" title="' + escapeHtml(verdictTip(p)) + '">'
        + escapeHtml(verdictLabel(p)) + '</td>'
        + '<td class="scan-vendor-cell">' + vendorCellHtml(p) + '</td>'
        + '<td><div class="scan-feats">' + (feats || '<span class="scan-note">—</span>') + '</div></td>'
        + '<td class="scan-note">' + escapeHtml(p.note || '') + '</td>'
        + '<td class="scan-desc">' + escapeHtml(detail) + '</td>'
        + '</tr>';
    }).join('');

    var html = '<table class="scan-table"><thead><tr>'
      + '<th>' + escapeHtml(t('cal.scan.col.port')) + '</th>'
      + '<th>' + escapeHtml(t('cal.scan.col.baud')) + '</th>'
      + '<th>' + escapeHtml(t('cal.scan.col.family')) + '</th>'
      + '<th>' + escapeHtml(t('cal.scan.col.vendor')) + '</th>'
      + '<th>' + escapeHtml(t('cal.scan.col.features')) + '</th>'
      + '<th>' + escapeHtml(t('cal.scan.col.note')) + '</th>'
      + '<th>' + escapeHtml(t('cal.scan.col.desc')) + '</th>'
      + '</tr></thead><tbody>' + rows + '</tbody></table>';
    return html;
  }

  // 表格是 innerHTML 拼的，点击用事件委托绑在容器上（重渲染后不用重绑）。
  function bindScanTable() {
    var wrap = $('cal-scan-table');
    if (!wrap) return;
    on(wrap, 'click', function (ev) {
      var tr = ev.target.closest ? ev.target.closest('.scan-row') : null;
      if (!tr) return;
      var idx = parseInt(tr.dataset.idx, 10);
      var p = (STATE.scan && STATE.scan.ports || [])[idx];
      if (!p) return;
      // 点行 = 用这个口，串口名与波特率**一并**写进上面的输入框，
      // 省掉"点完行再去手打一遍 115200"。
      //
      // 波特率必须"跟行保持一致"，包括**清空**：
      //   行有波特率      → 填上（用户点行就是要用这个口）
      //   行没扫到波特率  → 清空。不能留着上一个口的 115200 ——
      //                     实测里先点 COM3（115200）再点 COM1（空口），
      //                     输入框停在 115200，「读取模块信息」就拿着
      //                     一对错配的 串口+波特率 发给了后端。
      //   baud_fixed=false（USB CDC 直连）→ 清空。这类口没有波特率概念，
      //                     留着数字只会让人误以为"这就是它的波特率"。
      pickPort(portName(p));
      var baudEl = $('cal-baud');
      var hasBaud = !!(p.baud && p.baud_fixed !== false);
      if (baudEl) {
        if (document.activeElement === baudEl) baudEl.blur();
        baudEl.value = hasBaud ? String(p.baud) : '';
        // 联动都挂在 input/change 上（波特率提示、型号能力重算），必须补派发。
        // 清空时同样要派发：checkBaud() 会据此把提示切回"留空=自动探测"。
        baudEl.dispatchEvent(new Event('input', { bubbles: true }));
        baudEl.dispatchEvent(new Event('change', { bubbles: true }));
        flashFilled(baudEl);
      }
      Array.prototype.forEach.call(wrap.querySelectorAll('.scan-row'), function (el) {
        el.classList.toggle('is-current', el === tr);
      });
      // 提示写清"带过去了什么"，尤其是没带波特率的时候 ——
      // 否则用户点了行、波特率没变，会以为点了没反应。
      setPortHint(hasBaud
        ? t('cal.scan.picked', { port: portName(p), baud: p.baud })
        : t('cal.scan.picked.nobaud', { port: portName(p) }), 'is-ok');
    });
  }

  // ---------------- 读取模块信息（只读诊断，不写任何配置） ----------------
  // 后端是异步任务：POST 返回 202 后由轮询推进；任务 DONE 时再拉一次 full
  // 取回 report.module / report.mode。不能在这里等同步返回。
  //
  // **为什么按钮旁边要挂一条进度**：一次识别要逐档试波特率（自动探测时最多
  // 9 档 × 2~3 秒），全部落空接近 30 秒，期间页面除了按钮变灰什么都没变 ——
  // 真机反馈就是"点了像卡死"。这里在按钮下方给转圈 + 已用时间 + 当前档位，
  // 并且**从点击那一刻就开始计时**，不等 POST 返回：
  // POST 自己也可能因为等服务端让出串口而慢上几秒，那几秒同样不能是黑屏。
  //
  // **两条进度条共用这套实现**：`DETECT_UI` 服务「读取模块信息」，
  // `SCAN_UI` 服务「扫描串口」。两边都是"点一下、跑几十秒、期间只有一行文字在变"，
  // 各写一份迟早会漂。差别只在 DOM 元素与判定用的总时长 —— 由 `progEls(name)`
  // 和 `progBudget(name)` 决定，其余逻辑完全一致。
  //
  // **为什么计时口径是"预留总时长 + 走表"而不是进度事件**：后端两边的阶段粒度
  // 都太粗（扫描只有 `port/baud/phase` 三条，完全没有总数），拿事件数当分子会
  // 出现"第 1 档卡 20 秒、进度条纹丝不动"。所以只用走表推进、用事件改文字。
  var DETECT_UI = { started: 0, timer: null, plan: null, failed: false, text: '',
                    doneText: '', doneKeep: 2500 };
  var SCAN_UI = { started: 0, timer: null, budget: 0, failed: false, text: '',
                  doneText: '', doneKeep: 1800 };

  var PROG_ELEMENTS = {
    detect: { box: 'cal-detect-progress', text: 'cal-detect-text',
              timer: 'cal-detect-timer', bar: 'cal-detect-bar' },
    scan: { box: 'cal-scan-progress', text: 'cal-scan-text',
            timer: 'cal-scan-timer', bar: 'cal-scan-bar' }
  };

  function progState(name) { return name === 'scan' ? SCAN_UI : DETECT_UI; }

  function progEls(name) {
    var ids = PROG_ELEMENTS[name] || PROG_ELEMENTS.detect;
    return {
      box: $(ids.box), text: $(ids.text),
      timer: $(ids.timer), bar: $(ids.bar)
    };
  }

  // 总时长预估。**只用于把进度条推到 90%**，不用于任何"还剩几秒"的文案 ——
  // 扫描是"口数 × 档数"的乘积，后端首轮才知道口数，写死一个数字迟早脱节。
  // detect 走扫描档位预估时是 20 秒档，scan 取 30 秒：3 个口全无应答实测 40 秒+，
  // 取小了进度条会一直贴着 90% 不动；取大了前半程又慢得看不出在动。
  function progBudget(name) {
    if (name === 'scan') return (SCAN_UI.budget || 0) || 30;
    return (DETECT_UI.plan && DETECT_UI.plan.worst) || 30;
  }

  function detectEls() { return progEls('detect'); }

  function clearProgTimer(name) {
    var st = progState(name);
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
  }

  function clearDetectProgress() { clearProgTimer('detect'); }

  // 已经跑了多少秒（一位小数）。用 Date.now() 而不是自增计数器：
  // 标签页被切到后台时 setInterval 会被节流到 1 秒以上，
  // 自增会越跑越慢，显示的时间和用户真实感受对不上。
  function progElapsed(name) {
    var st = progState(name);
    if (!st.started) return 0;
    return Math.max(0, (Date.now() - st.started) / 1000);
  }

  function detectElapsed() { return progElapsed('detect'); }

  // 把进度条按"最坏总时长"推进。**到 90% 就停住**：
  // 真实结束时间由模块应答决定，进度条冲到 100% 却还在转圈比停在 90% 更让人困惑。
  function tickProg(name) {
    var els = progEls(name);
    var st = progState(name);
    if (els.timer) els.timer.textContent = progElapsed(name).toFixed(1) + 's';
    if (!els.bar || st.failed || !st.started) return;
    var pct = Math.min(90, (progElapsed(name) / progBudget(name)) * 100);
    els.bar.style.width = pct + '%';
  }

  function tickDetectProgress() { tickProg('detect'); }

  function startProg(name, message) {
    var els = progEls(name);
    var st = progState(name);
    clearProgTimer(name);
    st.started = Date.now();
    st.failed = false;
    st.text = message || '';
    st.doneText = '';
    if (!els.box) return;
    els.box.classList.remove('is-hidden', 'is-fail', 'is-done');
    if (els.text) els.text.textContent = st.text;
    if (els.bar) els.bar.style.width = '2%';
    if (els.timer) els.timer.textContent = '0.0s';
    st.timer = setInterval(function () { tickProg(name); }, 200);
  }

  function startDetectProgress(message) {
    var st = DETECT_UI;
    startProg('detect', message || t('cal.detect.prepare'));
    if (st.timer) { clearInterval(st.timer); st.timer = setInterval(tickDetectProgress, 200); }
  }

  // 阶段变化时只换文字，不重置计时 —— 计时是"用户从点击等到现在"的总时长。
  function setProgMessage(name, message) {
    var st = progState(name);
    if (!message) return;
    if (st.text === message) return;          // 同一句话反复写会打断选中/无障碍播报
    st.text = message;
    var els = progEls(name);
    if (els.text) els.text.textContent = message;
  }

  function setDetectMessage(message) { setProgMessage('detect', message); }  function failProg(name, message, detail) {
    var els = progEls(name);
    var st = progState(name);
    var elapsed = (typeof st.lastElapsed === 'number') ? st.lastElapsed : progElapsed(name);
    clearProgTimer(name);
    st.failed = true;
    st.started = 0;
    if (!els.box) return;
    els.box.classList.remove('is-hidden', 'is-done');
    els.box.classList.add('is-fail');
    if (els.text) {
      els.text.textContent = t(name === 'scan' ? 'cal.scan.failed' : 'cal.detect.fail',
                               { elapsed: elapsed.toFixed(1) }) +
        (message ? '：' + message : '') + (detail ? '（' + detail + '）' : '');
    }
    if (els.bar) els.bar.style.width = '100%';
    if (els.timer) els.timer.textContent = elapsed.toFixed(1) + 's';
  }

  function failDetectProgress(message, detail) {
    DETECT_UI.lastElapsed = (typeof DETECT_UI.lastElapsed === 'number')
      ? DETECT_UI.lastElapsed : progElapsed('detect');
    failProg('detect', message, detail);
  }

  // 成功提示停留 `doneKeep` 毫秒后收起 —— 结果本身已经在别处展示，
  // 这条只负责回答"刚才那几十秒到底算完了没"。
  //
  // `elapsedOverride`：调用方可能**已经**把 `started` 清零了（比如 `onScanDone`
  // 要先放开互斥按钮）。那种情况下这里再读 `progElapsed()` 拿到的必然是 0，
  // 计时器就被写成 '0.0s'。传进来就用传进来的，别自己再算一遍。
  function finishProg(name, doneText, keepMs, elapsedOverride) {
    var els = progEls(name);
    var st = progState(name);
    var elapsed = (typeof elapsedOverride === 'number')
      ? elapsedOverride : progElapsed(name);
    clearProgTimer(name);
    if (!els.box) return;
    els.box.classList.add('is-done');
    els.box.classList.remove('is-fail');
    if (els.text) els.text.textContent = doneText || '';
    if (els.bar) els.bar.style.width = '100%';
    if (els.timer) els.timer.textContent = elapsed.toFixed(1) + 's';
    var at = Date.now();
    setTimeout(function () {
      // 期间又点了同名的按钮 → 新一轮已经开始，绝不能把它收起来
      if (st.started || st.timer || !els.box) return;
      if (Date.now() - at < (keepMs || 2000) - 50) return;
      els.box.classList.add('is-hidden');
    }, keepMs || 2000);
  }

  function finishDetectProgress() {
    finishProg('detect', t('cal.detect.ok', { elapsed: progElapsed('detect').toFixed(1) }), 2500);
  }

  // 按当前状态机阶段刷新按钮旁的文字。后端给的是阶段名（英文），
  // 这里翻成人话 —— 直接显示 `DETECT` 等于没告诉用户任何事。
  function syncDetectStage(snap) {
    var stage = (snap && snap.stage) || '';
    var plan = snap && snap.detect_plan;
    if (plan) DETECT_UI.plan = plan;
    var baud = snap && snap.baud;
    var seconds = (snap && snap.result && snap.result.observations
      && snap.result.observations.seconds) || '';
    if (stage === 'PREPARE') setDetectMessage(t('cal.detect.prepare'));
    else if (stage === 'HOLD') setDetectMessage(t('cal.detect.hold'));
    else if (stage === 'OPEN') setDetectMessage(t('cal.detect.open', { port: (snap && snap.port) || '' }));
    else if (stage === 'DETECT') {
      // 自动探测（非显式波特率）才提示"最多试 N 档"：手填了档位就只试那一档，
      // 说"最多试 9 档"会让用户以为程序要把他填的值改掉。
      setDetectMessage(snap && snap.baud_explicit
        ? t('cal.detect.detect', { baud: baud || '?' })
        : t('cal.detect.detect_auto', {
          baud: baud || '?',
          steps: (DETECT_UI.plan && DETECT_UI.plan.steps) || 1
        }));
    } else if (stage === 'OBSERVE') {
      setDetectMessage(t('cal.detect.observe', { seconds: seconds || '…' }));
    } else if (stage === 'PLAN') setDetectMessage(t('cal.detect.plan'));
  }

  // ---------------- 「扫描串口」与「读取模块信息」互斥 ----------------
  // 两者抢的是同一批物理串口，但走的是两套机制：scan 不占租约、detect 占租约，
  // 所以**后端不会互相拦住**（scan 只挡标定任务，detect 只挡标定任务）。
  // 后端那层只能兜底，真正的互斥必须在点上按钮的那一刻就生效 ——
  // 让用户点了才弹"请先等它结束"，是最容易被当成"程序卡了"的交互。
  //
  // **两个按钮的可用状态只有一个真源 `syncOpButtons()`**，它从三个状态量推导：
  //   ① 标定任务在跑（`STATE.running`）
  //   ② 扫描在跑（`STATE.scan.running`，由 POST 与轮询共同维护）
  //   ③ 识别在跑（`DETECT_UI.started`，本页点过才有计时起点）
  // 以前是"哪条线开始就 `setOpBusy(name, true)`、结束再 `setOpBusy(name, false)`"
  // 的成对开关，而 `BUSY_IDS` 只列出**对方**、不含自己：扫描开始时手动
  // `btn.disabled = true` 禁掉自己，收尾却只放开对方 —— 扫描按钮永久变灰，
  // 而唯一会重置它的 `setFormLocked()` 只在状态轮询里跑，没有标定任务时
  // 轮询早就停了。现场表现就是"扫完必须刷新页面才能再点"。
  // 成对开关的错一个方向就是死锁，改成"每次从状态重算"就没有这个方向问题。
  function opLocked() {
    return !!STATE.running || scanRunning() || detectRunning();
  }

  function scanRunning() {
    return !!(STATE.scan && STATE.scan.running);
  }

  function detectRunning() {
    // 本页点过按钮才有本地计时起点；页面刷新后后端仍在跑 DETECT，
    // 但那时用户没有"我刚点的"这个上下文，靠 `STATE.running` 兜底。
    return !!DETECT_UI.started;
  }

  // 另一条线是否占着串口。用于按钮点下去之前的**同步兜底**：
  // 万一状态没同步上（比如在另一个标签页里点的），也不要发出这个请求。
  function otherOpBusy(name) {
    if (name === 'scan') return detectRunning() || STATE.running;
    return scanRunning() || STATE.running;
  }

  // 把两个按钮的可用状态刷成当前真值。**任何一条线的开始/结束都要调它**，
  // 而不是各自去改 `disabled` —— 改一半、漏一半就是这一串真机 bug 的来源。
  // 「开始标定」不在这里管：它由状态轮询里的 `setFormLocked()` 统一接管。
  function syncOpButtons() {
    var scanBtn = $('cal-scan');
    if (scanBtn) scanBtn.disabled = opLocked();
    // `cal-detect` 还要过端口家族闸门（未知/噪声口点了必然白等 45 秒），
    // 那个判断只在 refreshDetectGate 里，这里直接复用，不重写一遍。
    refreshDetectGate();
  }

  function readModuleInfo() {
    if (otherOpBusy('detect')) { showError(t('cal.err.op_busy')); return; }
    var port = $('cal-port').value.trim();
    if (!port) { showError(t('cal.err.need_port')); return; }
    // 第二道闸：按钮可能被键盘/脚本绕过 disabled，这里再判一次。
    var fam = portFamily(port);
    var forceBox = $('cal-force-detect');
    var force = !!(forceBox && forceBox.checked);
    if (fam && fam !== 'gnss' && !force) {
      showError(t('cal.err.detect_not_gnss', { fam: familyLabel(fam) }));
      refreshDetectGate();
      return;
    }
    var baud = baudValue();
    showError('');

    // 先按"还不知道型号"的候选表起个提示，POST 回来后再用后端算的 plan 修正。
    // 这样即在后端慢半拍时，用户也能立刻看到东西在动。
    // 顺序有讲究：**先占位（`startDetectProgress` 里写 `DETECT_UI.started`）
    // 再刷按钮**，否则 `detectRunning()` 还是 false，「扫描串口」按钮不会被锁，
    // 用户正好在这几毫秒里点了它。
    DETECT_UI.plan = null;
    DETECT_UI.lastElapsed = 0;
    startDetectProgress(t('cal.detect.prepare'));
    syncOpButtons();

    fetch('/api/calibration/detect', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        port: port, baud: baud,
        // 同理只发手选型号：自动识别就让后端自己去认
        module: pickedModuleId(),
        // force 只在用户勾了「仍要强制识别」时为 true：
        // 后端据它决定要不要对"自动探测 + 未填波特率"做全档枚举（约 45 秒）。
        force: force
      })
    })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok || !res.body.ok) {
          DETECT_UI.lastElapsed = detectElapsed();
          failDetectProgress(res.body.error || t('cal.err.detect'));
          showError(res.body.error || t('cal.err.detect'));
          DETECT_UI.started = 0;
          syncOpButtons();
          return;
        }
        // 后端返回的预估：型号不同候选表不同，由后端算，前端不硬编码。
        var snap = res.body.status || res.body;
        if (snap && snap.detect_plan) DETECT_UI.plan = snap.detect_plan;
        syncDetectStage(snap);
        startPolling();
      })
      .catch(function (err) {
        DETECT_UI.lastElapsed = detectElapsed();
        failDetectProgress(t('cal.err.detect'), err.message);
        showError(t('cal.err.detect') + ': ' + err.message);
        DETECT_UI.started = 0;
        syncOpButtons();
      });
  }

  function renderHw(report) {
    var box = $('cal-hw');
    if (!box) return;
    var mod = (report && report.module) || {};
    // 把识别到的型号记下来，供 selectedModule() 在「自动识别」分支回退使用。
    // 没有 `model_key` 说明这次没读通（模块不应答），保留上一次的值不动 ——
    // 别因为一次超时就把"我是谁"抹掉，接着点开始反而又被后端按 rover 拒。
    if (mod.model_key) {
      STATE.detectedModuleId = String(mod.model_key);
      applyCapabilities();
    }
    var mode = (report && (report.mode || {}).text) || 'unknown';
    var modeLabel = t('cal.hw.mode.' + mode);
    if (modeLabel === 'cal.hw.mode.' + mode) modeLabel = String(mode);
    // 模块自报的串口参数（LG290P 是 PQTMCFGUART,R，N4 是 CONFIG）。
    // 拿不到就显示「—」而不是空着：空着会像"页面漏了一行"。
    var uart = mod.uart || {};
    var uartText = '—';
    if (uart.baud) {
      uartText = (uart.port ? ('UART' + uart.port + ' ') : '') +
        uart.baud + (uart.frame ? (' ' + uart.frame) : '');
    }
    var rows = [
      ['cal.hw.firmware', mod.firmware || '—'],
      ['cal.hw.sn', mod.sn || '—'],
      ['cal.hw.mode', modeLabel],
      ['cal.hw.uart', uartText],
      ['cal.hw.uniq', mod.uniq_id || '—']
    ];
    box.innerHTML = rows.map(function (row) {
      return '<div><div class="k">' + escapeHtml(t(row[0])) + '</div><div class="v">' +
        escapeHtml(row[1]) + '</div></div>';
    }).join('');
    box.classList.remove('is-hidden');

    // 读模块信息后**不再**按 SN 重新拉历史：复用来源是全库最近一条，
    // 与当前插的是哪块模块无关（同一根天线换模块是最常见的场景）。
    // 页面上已经把"这条坐标是哪块模块、什么时候测的"显示出来，由用户核对。
    renderHistoryList();
  }

  // ---------------- 挂载点 ----------------
  function loadMountpoints() {
    var host = $('cal-cors-host').value.trim();
    var port = $('cal-cors-port').value.trim();
    if (!host || !port) { showError(t('cal.err.need_host')); return; }
    var btn = $('cal-load-mounts');
    btn.disabled = true;
    fetch('/api/sourcetable', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host: host, port: port,
        user: $('cal-cors-user').value.trim(),
        password: $('cal-cors-password').value
      })
    })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        var input = $('cal-cors-mount');
        var list = $('cal-cors-mount-list');
        // 用户可能已经手打了一个挂载点：点了「读取挂载点」不能把他填的冲掉
        var typed = (input.value || '').trim();
        list.innerHTML = '';
        if (!res.ok || !res.body.ok) {
          showError((res.body && res.body.error) || t('cal.err.mounts'));
          return;
        }
        showError('');
        var items = res.body.mountpoints || [];
        items.forEach(function (mp) {
          var opt = document.createElement('option');
          opt.value = mp.mountpoint;
          opt.label = mp.mountpoint + ' · ' + (mp.format || '') + ' · ' + (mp.nav || '');
          list.appendChild(opt);
        });
        if (typed) {
          // 手填优先，不覆盖它 —— 但仍把候选摊开：用户填的可能只是个近似值，
          // 让他一眼看到服务器上真实存在的挂载点，比默默什么都不做强。
          if (window.Picker) Picker.open(input, typed);
          return;
        }
        if (!items.length) { showError(t('cal.err.mounts_empty')); return; }
        input.value = items[0].mountpoint || '';
        if (window.Picker) Picker.open(input);
      })
      .catch(function (err) { showError(t('cal.err.mounts') + ': ' + err.message); })
      .then(function () { btn.disabled = false; });
  }

  // ---------------- 轮询 ----------------
  function startPolling() {
    if (STATE.timer) return;
    STATE.timer = setInterval(poll, 1000);
    poll();
  }

  function stopPolling() {
    if (STATE.timer) { clearInterval(STATE.timer); STATE.timer = null; }
  }

  function poll() {
    fetch('/api/calibration/status', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (res) {
        if (res.status === 401) {
          // 会话过期：标定还在服务端跑，提示重新登录，不要偷偷停轮询
          stopPolling();
          showError(t('cal.err.session'));
          return null;
        }
        return res.json();
      })
      .then(function (data) { if (data && data.ok) render(data); })
      .catch(function () { /* 网络抖动忽略，下一轮再试 */ })
      .then(function () {
        // 首轮拿到"任务已结束"的快照时，也要把结果摘要拉回来。
        // render() 里只在 running 由真变假那一刻调 fetchSummary()，那是给
        // "本次会话从头看到尾"准备的；用户关掉页面再打开、或者直接刷新，
        // 首轮快照的 wasRunning 就是 false，摘要区会一直是空的 —— 而后端
        // 明明还留着上一轮完整的 result 和 summary。
        if (!STATE.running && STATE.lastSnap && STATE.lastSnap.task_id
            && TERMINAL[STATE.lastSnap.stage]) {
          fetchSummary(STATE.lastSnap.task_id);
        }
      });
  }

  function render(snap) {
    var wasRunning = STATE.running;
    STATE.lastSnap = snap;
    STATE.running = !!snap.running;
    setFormLocked(STATE.running);
    // 页面刷新后本地的 DETECT_UI 是空的，但后端可能仍在跑 diagnose。
    // 这时「扫描串口」必须同样锁住 —— 两边抢的是同一个串口，与是不是本页点的无关。
    // 注意：这里先刷一次是给"任务在跑"那一段用的；本轮末尾还会再刷一次，
    // 因为上面那段可能刚好把 `DETECT_UI.started` 清零（任务刚结束），
    // 那时按钮必须立刻放开，而不是等下一轮 —— 而下一轮在有结果之后就不再来了
    // （`stopPolling()`），等下去就只能是"刷新页面"。
    syncOpButtons();

    var badge = $('cal-run-badge');
    if (badge) {
      badge.textContent = STATE.running ? t('cal.state.running')
        : (TERMINAL[snap.stage] ? t('cal.state.' + snap.stage.toLowerCase()) : t('cal.state.idle'));
    }

    var bar = $('cal-progress-bar');
    if (bar) bar.style.width = Math.max(0, Math.min(100, Number(snap.progress) || 0)) + '%';
    var stageEl = $('cal-stage');
    if (stageEl) stageEl.textContent = stageLabel(snap.stage, snap.stage_text);
    var msgEl = $('cal-stage-msg');
    if (msgEl) msgEl.textContent = snap.message || '';
    var elapsedEl = $('cal-elapsed');
    if (elapsedEl) {
      elapsedEl.textContent = snap.started_at
        ? (snap.started_at + (snap.finished_at ? ' → ' + snap.finished_at : '')) : '';
    }

    renderMetrics(snap.live);
    renderBanner(snap);
    appendTimeline(snap);
    renderLogs(snap.logs);

    // 「读取模块信息」的行内进度：只在任务在跑、且这条提示还开着时更新。
    // 判据用 `DETECT_UI.started`（本页点过按钮才为真），不要用 stage ——
    // 页面刷新后 status 里同样有 DETECT 阶段，但用户没有本轮的计时起点。
    if (DETECT_UI.started) {
      if (snap.running) {
        syncDetectStage(snap);
      } else if (snap.stage === 'DONE' || snap.stage === 'FAILED'
                 || snap.stage === 'CANCELLED') {
        DETECT_UI.lastElapsed = detectElapsed();
        if (snap.stage === 'DONE') finishDetectProgress();
        else failDetectProgress(snap.error || '');
        DETECT_UI.started = 0;
      }
    }

    if (STATE.running) {
      $('cal-abort').disabled = false;
      if (!STATE.timer) startPolling();
    } else {
      $('cal-abort').disabled = true;
      stopPolling();
      // 任务刚结束（running 由真变假）：拉一次 full 取 result，填结果摘要。
      // 每轮都拉会白白搬运一份完整 result + 日志尾。
      if (wasRunning && snap.task_id) {
        fetchSummary(snap.task_id);
      }
      if (snap.stage === 'DONE' && snap.method && snap.method !== 'external_rover') {
        $('cal-apply').classList.remove('is-hidden');
      } else if (snap.stage && TERMINAL[snap.stage]) {
        $('cal-apply').classList.add('is-hidden');
      }
      if (snap.stage === 'FAILED' && snap.error) showError(snap.error);
      // 只读诊断结束后，把固件/SN/模式取回来填进硬件信息区（每次任务只取一次）
      if (snap.stage === 'DONE' && snap.action === 'diagnose'
          && snap.task_id && snap.task_id !== STATE.hwTaskId) {
        STATE.hwTaskId = snap.task_id;
        fetch('/api/calibration/status?full=1',
          { credentials: 'same-origin', cache: 'no-store' })
          .then(function (r) { return r.json(); })
          .then(function (data) { if (data && data.ok) renderHw(data.result); })
          .catch(function () { /* 取不到就留空，不影响标定 */ });
      }
    }
    // 本轮最后一次刷按钮：这时 `STATE.running` 与 `DETECT_UI.started` 都是终值，
    // 算出来的就是"接下来用户能点什么"。放在 `stopPolling()` 之后也没关系 ——
    // 这条路径本来就不依赖轮询继续跑。
    syncOpButtons();
  }

  // ---------------- 结果摘要 ----------------
  // 任务结束后"这次到底做了什么、模块现在是什么状态、下一步干什么"——
  // 用户在页面上唯一会认真读的就是这一段。
  function fetchSummary(taskId) {
    if (!taskId || STATE.summaryTaskId === taskId) return;
    STATE.summaryTaskId = taskId;
    fetch('/api/calibration/status?full=1',
      { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.ok) return;
        renderSummary(data.summary, data.result, data.method, data.stage);
        if (data.result && data.result.module) renderHw(data.result);
      })
      .catch(function () { /* 摘要取不到不影响主流程 */ });
  }

  function renderSummary(summary, result, method, stage) {
    var box = $('cal-summary');
    if (!box) return;
    if (!summary && !result) { box.classList.add('is-hidden'); return; }

    var failed = stage === 'FAILED' || stage === 'ABORTED';
    var verdict = (summary && summary.verdict) || '';
    var tone = 'is-ok';
    if (failed) tone = 'is-bad';
    else if (verdict === 'warning' || verdict === 'no_broadcast' || verdict === 'skipped') {
      tone = 'is-warn';
    } else if (verdict === 'mismatch' || verdict === 'no_config') {
      tone = 'is-bad';
    } else if (verdict === 'unknown' || verdict === 'dry_run') {
      tone = 'is-warn';
    }
    box.className = 'cal-summary ' + tone;

    var hd = $('cal-summary-hd');
    if (hd) hd.textContent = (summary && summary.headline) || t('cal.result.title');

    var lines = [];
    var plan = (result && result.plan) || {};
    // 写了哪组坐标 —— 复用分支要特别标出来，否则用户会以为这次是新测的
    var arp = (summary && summary.arp) || plan.arp;
    var reused = !!(result && (result.cors || {}).reused);
    if (arp && arp.x !== undefined) {
      lines.push(t('cal.result.written') +
        '：X ' + fmtNum(arp.x, 4) + '  Y ' + fmtNum(arp.y, 4) + '  Z ' + fmtNum(arp.z, 4) +
        (reused ? '（' + t('cal.result.reused_tag') + '）' : ''));
    }
    var geo = (summary && summary.geodetic) || plan.geodetic;
    if (geo && geo.lat !== undefined && geo.lat !== null) {
      lines.push(t('cal.result.geodetic') +
        '：' + fmtNum(geo.lat, 8) + '°, ' + fmtNum(geo.lon, 8) + '°, ' +
        fmtNum(geo.height, 3) + ' m');
    }
    // 1005 播发值 vs 写入值的偏差：这条是"链路通不通"的唯一硬证据。
    // 后端 `delta` 是一个对象 {dx,dy,dz,max_axis,norm}（单位 m），不是标量。
    // 这里曾经直接 `Number(delta) * 100`，Number({...}) 恒为 NaN，页面上就显示
    // 「1005 播发偏差：NaN cm」——明明结论是"一致"，却配一个 NaN，等于把唯一的
    // 硬证据变成了噪声。取 max_axis（最大轴差），并在取不到时显示占位符。
    var broadcast = (summary && summary.broadcast) || (result && result.broadcast);
    var delta = (summary && summary.delta) || (result && result.delta);
    if (broadcast && broadcast.x !== undefined) {
      var axis = (delta && typeof delta === 'object') ? delta.max_axis : delta;
      var axisNum = (axis === null || axis === undefined) ? NaN : Number(axis);
      var deltaText = isFinite(axisNum) ? (axisNum * 100).toFixed(2) + ' cm' : '—';
      lines.push(t('cal.result.broadcast') + '：' + deltaText);
    }
    // 模块持久化的状态：用户据此判断"要不要断电重来"
    var nvm = (summary && summary.nvm_state) || '';
    if (nvm) {
      var nvmKey = 'cal.result.nvm.' + nvm;
      lines.push(t('cal.result.nvm') + '：' + (hasKey(nvmKey) ? t(nvmKey) : nvm));
    }
    var warnings = (summary && summary.warnings) || (result && result.warnings) || [];
    if (warnings.length) {
      lines.push(t('cal.result.warnings') + '：' + warnings.slice(0, 3).join('；'));
    }
    var advice = (summary && summary.advice) || '';
    if (advice) lines.push('<b>' + escapeHtml(t('cal.result.advice')) + '</b>：' + escapeHtml(advice));

    var body = $('cal-summary-body');
    if (body) {
      body.innerHTML = lines.map(function (text) {
        return '<div>' + (/^<b>/.test(text) ? text : escapeHtml(text)) + '</div>';
      }).join('');
    }

    // 复制 ECEF：只在真有坐标时出现。手工拼字符串，不依赖 result 的深层结构。
    var copyBtn = $('cal-copy-ecef');
    if (copyBtn) {
      if (arp && arp.x !== undefined) {
        copyBtn.dataset.ecef = [arp.x, arp.y, arp.z].join(', ');
        copyBtn.classList.remove('is-hidden');
      } else {
        copyBtn.dataset.ecef = '';
        copyBtn.classList.add('is-hidden');
      }
    }
    box.classList.remove('is-hidden');
  }

  function copyEcef() {
    var btn = $('cal-copy-ecef');
    var text = btn ? (btn.dataset.ecef || '') : '';
    if (!text) return;
    var done = function () { showError(''); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () {
        window.prompt(t('cal.result.copy_ecef'), text);
      });
    } else {
      window.prompt(t('cal.result.copy_ecef'), text);
    }
  }

  function renderMetrics(live) {
    var guardBox = $('cal-freeze');
    var metricsBox = $('cal-metrics');
    if (!live) {
      ['m-sats', 'm-pdop', 'm-fix', 'm-samples', 'm-sigma', 'm-gga'].forEach(function (id) {
        var el = $(id); if (el) { el.textContent = '—'; el.className = 'v'; }
      });
      if (guardBox) guardBox.classList.add('is-hidden');
      return;
    }
    // 指标卡在模板里带 `is-hidden`，而全文件只有这里能把它打开 ——
    // 少了这一行，六张卡就永远是 display:none，标定跑完也看不到
    // 卫星数 / PDOP / 固定状态。有 live 快照就一定该显示。
    if (metricsBox) metricsBox.classList.remove('is-hidden');
    // 防抖护栏：标定阶段 GGA 原样透传，这里只告诉用户"护栏开着"以及"拦下过几条"。
    // 一旦有跳变被丢弃，说明过程中位置出现过异常（天线被碰、模块被切成基准站、
    // 掉星后单点解乱跳），文案必须跟着变——否则用户会以为一切正常。
    if (guardBox) {
      var dropped = Number(live.gga_jump_dropped || 0);
      var stopped = !!live.gga_jump_stopped;
      if (live.gga_guard_active || dropped > 0 || stopped) {
        guardBox.classList.remove('is-hidden');
        var span = guardBox.querySelector('[data-i18n]');
        if (span) {
          var key = stopped ? 'cal.guard.stopped'
            : (dropped > 0 ? 'cal.guard.dropped' : 'cal.guard.active');
          span.setAttribute('data-i18n', key);
          span.textContent = t(key, { n: dropped });
        }
        guardBox.className = 'cal-freeze'
          + (stopped ? ' is-warn is-stop' : (dropped > 0 ? ' is-warn' : ''));
      } else {
        guardBox.classList.add('is-hidden');
      }
    }
    var sats = $('m-sats');
    if (sats) {
      sats.textContent = live.num_sats ? String(live.num_sats) : '—';
      sats.className = 'v ' + (live.num_sats >= 12 ? 'is-ok' : 'is-warn');
    }

    var pdop = $('m-pdop');
    var pdopValue = live.pdop;
    if (pdop) {
      pdop.textContent = (pdopValue === null || pdopValue === undefined)
        ? '—' : Number(pdopValue).toFixed(2);
      pdop.className = 'v ' + ((pdopValue !== null && pdopValue <= 2.0) ? 'is-ok' : 'is-warn');
    }

    var fix = $('m-fix');
    var quality = live.quality;
    var fixText = '—';
    if (quality === 4) fixText = t('cal.fix.fixed');
    else if (quality === 5) fixText = t('cal.fix.float');
    else if (quality === 1) fixText = t('cal.fix.single');
    else if (quality === 2) fixText = t('cal.fix.dgps');
    else if (quality === 0 || quality === null || quality === undefined) fixText = t('cal.fix.none');
    else fixText = 'Q' + quality;
    if (fix) {
      fix.textContent = fixText;
      fix.className = 'v ' + (quality === 4 ? 'is-ok' : (quality === 5 ? 'is-warn' : 'is-bad'));
    }

    var samples = $('m-samples');
    if (samples) samples.textContent = live.samples ? String(live.samples) : '0';
    var sigmaEl = $('m-sigma');
    if (sigmaEl) {
      if (live.sigma === null || live.sigma === undefined) {
        sigmaEl.textContent = '—'; sigmaEl.className = 'v';
      } else {
        sigmaEl.textContent = (Number(live.sigma) * 100).toFixed(1) + ' cm';
        sigmaEl.className = 'v ' + (Number(live.sigma) <= 0.02 ? 'is-ok' : 'is-warn');
      }
    }
    var gga = $('m-gga');
    if (gga) gga.textContent = live.gga_forwarded ? String(live.gga_forwarded) : '0';
  }

  // 阶段流水：**一步一行**，行内文字跟着当前阶段的实时消息走。
  //
  // 原来是按"阶段 + 完整消息"去重，而采集/静置观察的消息里带秒数和毫米值，
  // 每秒都在变 —— 一次 5 分钟采集会往这个 240px 高的滚动区里灌 300 行
  // 同一阶段的进度快照，前面真正有用的几步（切流动站、连 CORS）被顶出视口。
  // 加了静置观察之后又多了 120 行，所以这里改成按"阶段 + 进入时刻"去重建行，
  // 同一阶段内的消息变化只**改写最后一行**，不再新增行。实时进度另有
  // `#cal-stage-msg` 一直显示着，信息不会丢。
  function appendTimeline(snap) {
    if (!snap.stage) return;
    var rowKey = snap.task_id + '|' + snap.stage + '|' + (snap.stage_at || '');
    var text = stageLabel(snap.stage, snap.stage_text)
      + (snap.message ? ' · ' + snap.message : '');

    var list = $('cal-timeline');
    if (!list) return;
    if (snap.task_id && snap.task_id !== STATE.lastTaskId) {
      STATE.lastTaskId = snap.task_id;
      list.innerHTML = '';
      STATE.applied = false;
      STATE.lastStage = '';
      STATE.lastRowText = null;
    }

    // 同一阶段再次进入（例如 FIX_WAIT → SETTLE 未达标又回到 FIX_WAIT）时
    // stage_at 会变，rowKey 也就变了，仍然会新起一行。
    if (rowKey === STATE.lastStage && STATE.lastRowText) {
      if (STATE.lastRowText.nodeValue !== text) STATE.lastRowText.nodeValue = text;
      return;
    }
    STATE.lastStage = rowKey;

    var li = document.createElement('li');
    var isFail = snap.stage === 'FAILED' || snap.stage === 'ABORTED';
    li.className = isFail ? 'is-fail' : (snap.stage === 'DONE' ? 'is-ok' : '');
    var time = document.createElement('time');
    // 用**本阶段的进入时刻**，不是整个任务的开机时刻。取 started_at 时，
    // 一次 2 分半的标定里 13 行流水全写着同一个 16:04:21，用户根本看不出
    // 卡在哪一步、每步花了多久——而这正是这个列表唯一的用途。
    time.textContent = ((snap.stage_at || snap.started_at) || '').split(' ').pop() || '';
    li.appendChild(time);
    var node = document.createTextNode(text);
    li.appendChild(node);
    list.appendChild(li);
    STATE.lastRowText = node;
    list.scrollTop = list.scrollHeight;
  }

  function renderLogs(logs) {
    var box = $('cal-log');
    if (!box || !logs || !logs.length) return;
    box.textContent = '';
    logs.slice(-40).forEach(function (item) {
      var line = document.createElement('div');
      if (item.level === 'error') line.className = 'lv-error';
      line.textContent = '[' + (item.at || '').toString().slice(-8) + '] ' +
        (item.stage || '') + ' ' + (item.message || '');
      box.appendChild(line);
    });
    box.classList.remove('is-hidden');
    box.scrollTop = box.scrollHeight;
  }

  function setFormLocked(locked) {
    var ids = ['cal-port', 'cal-baud', 'cal-module', 'cal-cors-host', 'cal-cors-port',
      'cal-cors-user', 'cal-cors-password', 'cal-cors-mount', 'cal-duration',
      'cal-svin-profile', 'cal-fix-timeout', 'cal-load-mounts', 'cal-on-slow',
      'cal-coord-msg', 'cal-msm', 'cal-svin-resume', 'cal-scan',
      'cal-detect', 'cal-start', 'cal-goto-measure'];
    ids.forEach(function (id) {
      var el = $(id);
      if (el) el.disabled = !!locked;
    });
    ['cal-x', 'cal-y', 'cal-z', 'cal-lat', 'cal-lon', 'cal-h'].forEach(function (id) {
      var el = $(id);
      if (el) el.disabled = !!locked;
    });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="cal-method"]'),
      function (radio) { radio.disabled = !!locked || radio.dataset.capDisabled === '1'; });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="cal-coord-src"]'),
      function (radio) { radio.disabled = !!locked; });
    // 解锁之后还要再过一遍端口家族闸门：任务开始时这里把 cal-detect 放开，
    // 但那个口可能根本是 noise，不能让它跟着变回可点。
    refreshDetectGate();
  }

  // ---------------- 开始前预览 ----------------
  // 一句话说清"这次要做什么"，比让用户自己把前面几个卡片拼起来可靠。
  function renderRunPreview() {
    var box = $('cal-run-preview');
    var label = $('cal-start-label');
    if (!box) return;
    var method = STATE.method;
    var reuse = reuseActive();
    var methodText = hasKey(METHOD_LABEL[method]) ? t(METHOD_LABEL[method]) : method;
    var parts = [methodText];
    if (reuse) parts.push(t('cal.plan.reuse'));
    else if (!writesModule(method)) parts.push(t('cal.plan.readonly'));
    box.textContent = parts.join(' · ');
    if (label) {
      var startKey = 'cal.start.' + method + (reuse ? '_reuse' : '');
      if (!hasKey(startKey)) startKey = 'cal.start.' + method;
      if (!hasKey(startKey)) startKey = 'cal.start';
      label.setAttribute('data-i18n', startKey);
      label.textContent = t(startKey);
    }
  }

  // ---------------- payload 组装 ----------------
  // 复用坐标的来源：STATE.reuseHistory（库内最近一条）。
  // 复用前请确认那条记录确实是**同一根天线、同一个安装位置**——
  // 页面把型号/SN/时间/坐标都列出来了，这是唯一可靠的判据，程序替不了。
  function historyCoord() {
    var r = STATE.reuseHistory;
    if (!r) return null;
    var x = Number(r.ecef_x), y = Number(r.ecef_y), z = Number(r.ecef_z);
    if (!isFinite(x) || !isFinite(y) || !isFinite(z)) return null;
    return [x, y, z];
  }

  function buildPayload() {
    var method = STATE.method;
    var reuse = reuseActive();
    var caps = selectedCaps() || {};
    var mod = selectedModule() || {};
    var payload = {
      port: $('cal-port').value.trim(),
      baud: baudValue(),
      method: method,
      // 只发**用户手选**的型号；「自动识别」发 null，让后端按实测再认一遍。
      // 这里不能发 `selectedModule().id`：自动识别时它已被识别结果填充，
      // 发出去等于把"按实测认"降级成"按上次认的结果认"，换了模块就认错。
      module: pickedModuleId(),
      remark: t('cal.remark.auto')
    };

    // RTCM 输出只在"会写模块 且 模块能配报文"时才下发。
    // 块被藏起来时不能顺手把 DOM 里那份值发出去 —— 那等于用户没做选择却被写了一份配置。
    // `mod.id` 现在是"当前按哪块型号办事"（含自动识别出的型号），为空才是真的未知。
    var wantsRtcm = writesModule(method) && (mod.id ? !!caps.rtcm_out : true);
    if (wantsRtcm) {
      payload.msm = $('cal-msm').value;
      payload.coord_msg = $('cal-coord-msg').value;
    }

    if (method === 'cors' || method === 'external_rover') {
      // 复用分支不连网：整段 cors 都不发，账号字段留着也没意义
      if (!reuse) {
        payload.cors = {
          host: $('cal-cors-host').value.trim(),
          port: Number($('cal-cors-port').value),
          user: $('cal-cors-user').value.trim(),
          password: $('cal-cors-password').value,
          mountpoint: $('cal-cors-mount').value
        };
        payload.duration = $('cal-duration').value;
        var fixTimeout = Number($('cal-fix-timeout').value);
        payload.fix_wait_timeout = isFinite(fixTimeout) && fixTimeout > 0 ? fixTimeout : null;
        // 未收敛策略只对"会写坐标"的 CORS 标定有意义（只测不写不落 NVM）
        if (method === 'cors') payload.on_slow = $('cal-on-slow').value;
      } else {
        // A1 复用分支：后端整段跳过测量，直接把这组历史坐标写进 NVM。
        // 历史记录的 ecef_* 就是当年**写进模块的那组坐标**，原样传，不做换算。
        // 值来自 STATE.reuseHistory（库内最近一条，任意模块）
        if (!STATE.reuseHistory) return payload;
        var coord = historyCoord();
        if (coord) payload.reuse_arp = coord;
      }
    } else if (method === 'survey_in') {
      payload.svin_profile = $('cal-svin-profile').value;
      payload.svin_resume = !!($('cal-svin-resume') && $('cal-svin-resume').checked);
    } else {
      // manual：坐标来源三选一
      if (reuse) {
        var reusedCoord = historyCoord();
        if (reusedCoord) payload.arp = reusedCoord;
      } else if (STATE.coordSource === 'typing') {
        var x = parseFloat($('cal-x').value);
        var y = parseFloat($('cal-y').value);
        var z = parseFloat($('cal-z').value);
        if (isFinite(x) && isFinite(y) && isFinite(z)) {
          payload.ecef = [x, y, z];
        } else {
          payload.lat = parseFloat($('cal-lat').value);
          payload.lon = parseFloat($('cal-lon').value);
          // 高程留空就传 null，**不要** `|| 0`。
          // `parseFloat('') || 0` 会把"没填"变成"椭球高 0 米"，而 0 是海平面 ——
          // 内陆站因此静默低几十米，且校验链路查不出来（写入与播发错得一样）。
          var rawH = ($('cal-h').value || '').trim();
          payload.height = rawH === '' ? null : parseFloat(rawH);
        }
      }
    }
    return payload;
  }

  function validate(payload) {
    if (!payload.port) return t('cal.err.need_port');
    var method = payload.method;
    // 复用历史坐标的 CORS 标定不连网，所以不校验 CORS 地址/端口/挂载点
    if ((method === 'cors' || method === 'external_rover') && !payload.reuse_arp) {
      if (!payload.cors || !payload.cors.host) return t('cal.err.need_host');
      if (!payload.cors.port) return t('cal.err.need_cors_port');
      if (!payload.cors.mountpoint) return t('cal.err.need_mount');
    }
    if (method === 'manual') {
      // 复用分支下发的是 arp，手输下发的是 ecef / lat，两条通路都要认
      var hasCoord = payload.ecef || payload.arp || isFinite(payload.lat);
      if (!hasCoord) return t('cal.err.need_coord');
      // 走经纬度这条路时高程是必填：少了它算不出要写进模块的 ECEF。
      // 之前这里放过去、后端再补 0，等于把整站钉在海平面上。
      if (!payload.ecef && !payload.arp && isFinite(payload.lat)
          && (payload.height === null || payload.height === undefined
              || !isFinite(payload.height))) {
        return t('cal.err.need_height');
      }
    }
    // 能力互斥：后端第一步也会拦，但让用户点按钮之前就知道，
    // 比跑起来再报错省一次等待（而且省掉一次串口占用）。
    // 判据顺序与后端保持一致：**先角色方式表**（methods_of 闸门），再命令集，再能力位。
    var mod = selectedModule() || {};
    if (mod.id) {
      var caps = mod.caps || {};
      var allowed = selectedMethods() || [];
      if (allowed.indexOf(method) < 0) {
        var roleLabel = mod.role_label || '';
        return t('cal.err.role_unsupported', {
          name: mod.name || mod.id,
          role: roleLabel,
          method: t(methodLabelKey(method))
        });
      }
      var writable = writesModule(method);
      if (writable && !canWrite()) return t('cal.lock.manual', { name: mod.name || mod.id });
      if (method === 'cors' && caps.fixed === false) return t('cal.err.cors_fixed');
      if (method === 'manual' && caps.fixed === false) return t('cal.err.manual_fixed');
      if (method === 'survey_in' && caps.svin !== true) return t('cal.err.svin');
    }
    return '';
  }

  function confirmThenStart() {
    var payload = buildPayload();
    var problem = validate(payload);
    if (problem) { showError(problem); return; }
    showError('');

    var lines = [];
    var method = payload.method;
    // 停服估算 = 采集时长 + 预热观察 + 后端下发的收尾余量 + 可能发生的一次延长。
    // xlong 档当前最坏为 900 + 60 + 120 + 120 = 1200 秒。
    var settle = settleSeconds();
    var stopSeconds = durationSeconds(payload.duration) + settle + reserveSeconds()
                      + (extendSeconds() || 120);
    if (method === 'external_rover') {
      lines.push(t('cal.confirm.measure_only'));
      lines.push(t('cal.confirm.stop', { seconds: stopSeconds, settle: settle }));
    } else if (method === 'cors' && !payload.reuse_arp) {
      lines.push(t('cal.confirm.stop', { seconds: stopSeconds, settle: settle }));
    } else if (method === 'cors' && payload.reuse_arp) {
      lines.push(t('cal.confirm.reuse'));
    } else if (method === 'survey_in') {
      lines.push(t('cal.confirm.svin'));
    } else {
      lines.push(t('cal.confirm.write'));
    }
    if (payload.coord_msg === 'keep' && payload.msm === 'keep') {
      lines.push(t('cal.confirm.rtcm_keep'));
    }

    // 有在线客户端时补充一句：标定期间会断
    fetch('/api/local_caster', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (info) {
        var clients = info && (info.clients || info.client_count || 0);
        if (clients) lines.push(t('cal.confirm.clients', { n: clients }));
      })
      .catch(function () { /* 读不到就算了，不阻断 */ })
      .then(function () {
        var list = $('cal-confirm-list');
        list.innerHTML = '';
        lines.forEach(function (text) {
          var li = document.createElement('li');
          li.textContent = text;
          list.appendChild(li);
        });
        var title = $('cal-confirm-title');
        if (title) {
          var titleKey = 'cal.confirm.title.' + method + (payload.reuse_arp ? '_reuse' : '');
          title.textContent = hasKey(titleKey) ? t(titleKey) : t('cal.confirm.title');
        }
        $('cal-confirm-mask').classList.add('is-open');
        STATE.pendingPayload = payload;
      });
  }

  function doStart() {
    var payload = STATE.pendingPayload;
    $('cal-confirm-mask').classList.remove('is-open');
    if (!payload) return;
    $('cal-start').disabled = true;
    fetch('/api/calibration/start', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (res) {
        return res.json().then(function (b) { return { status: res.status, body: b }; });
      })
      .then(function (res) {
        if (res.status === 409) {
          showError(t('cal.err.busy'));
        } else if (!res.body.ok) {
          showError(res.body.error || t('cal.err.start'));
        } else {
          showError('');
          var timeline = $('cal-timeline');
          if (timeline) timeline.innerHTML = '';
          STATE.lastStage = '';
          // 清掉"最后一行"的引用：上面已经把列表清空了，留着旧节点会让
          // 第一轮进度消息改写一个不在文档里的节点，页面上就少了一行。
          STATE.lastRowText = null;
          STATE.summaryTaskId = '';
          $('cal-summary').classList.add('is-hidden');
          $('cal-apply').classList.add('is-hidden');
          startPolling();
        }
      })
      .catch(function (err) { showError(t('cal.err.start') + ': ' + err.message); })
      .then(function () { $('cal-start').disabled = STATE.running; });
  }

  function doAbort() {
    if (!window.confirm(t('cal.confirm.abort'))) return;
    fetch('/api/calibration/abort', { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) showError(data.message || t('cal.err.abort'));
        else startPolling();
      })
      .catch(function (err) { showError(t('cal.err.abort') + ': ' + err.message); });
  }

  function doApplyAsSource() {
    var port = $('cal-port').value.trim();
    if (!port) { showError(t('cal.err.need_port')); return; }
    fetch('/api/calibration/apply-as-source', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port: port, baud: baudValue() })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) showError(data.error || t('cal.err.apply'));
        else { showError(''); alert(data.message || t('cal.ok.apply')); }
      })
      .catch(function (err) { showError(t('cal.err.apply') + ': ' + err.message); });
  }

  // 「直接切到只测坐标」：把方式换成 external_rover，让用户当场就能测。
  // 不做静默跳转 —— 用户点了这个按钮就是要"现在去测"，跳过去还得自己再选一次。
  function gotoMeasure() {
    var radio = document.querySelector('input[name="cal-method"][value="external_rover"]');
    if (!radio || radio.disabled) return;
    radio.checked = true;
    STATE.corsSource = 'measure';
    STATE.coordSource = 'typing';
    syncMethod();
    var target = $('cal-cors-card');
    if (target && target.scrollIntoView) {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  // 「不复用，重新跑一次测量」：退出 A1 复用分支，回到正常测量路径。
  // 退出后 cors 方式重新显示 CORS 接入卡，manual 方式回到手写坐标。
  function exitReuse() {
    STATE.corsSource = 'measure';
    STATE.coordSource = 'typing';
    var srcRadio = document.querySelector('input[name="cal-coord-src"][value="typing"]');
    if (srcRadio) srcRadio.checked = true;
    syncMethod();
  }

  // ---------------- 启动 ----------------
  function boot() {
    on($('cal-scan'), 'click', scanPorts);
    on($('cal-scan-close'), 'click', function () {
      var panel = $('cal-scan-panel');
      if (panel) panel.classList.add('is-hidden');
    });
    bindScanTable();
    on($('cal-load-mounts'), 'click', loadMountpoints);
    on($('cal-module'), 'change', function () {
      // 用户亲手选了型号 → 以他的选择为准，识别结果让位（不再回退到识别值）。
      // 选回「自动识别」时识别结果照旧可用，所以只在选了具体型号时清。
      var picked = $('cal-module') ? $('cal-module').value : AUTO_ID;
      if (picked && picked !== AUTO_ID) STATE.detectedModuleId = '';
      applyCapabilities();
      refreshBaudOptions();
      refreshModuleHint();
    });
    on($('cal-baud'), 'input', checkBaud);
    on($('cal-baud'), 'change', checkBaud);
    // 手打串口名同样算"换了口"：型号只对某一个口成立，换了就得回到自动探测。
    // 挂在 change 而不是 input 上 —— input 每敲一个字母都会触发，
    // 会把用户正在选的型号一路清掉，必须等他敲完（失焦/回车）再判。
    on($('cal-port'), 'change', function () {
      resetModuleForPort();
      // 换口 = 换判定：只有扫描到 gnss 的口才允许直接点「读取模块信息」
      refreshDetectGate();
    });
    var forceBox = $('cal-force-detect');
    if (forceBox) on(forceBox, 'change', refreshDetectGate);
    refreshDetectGate();
    on($('cal-detect'), 'click', readModuleInfo);
    on($('cal-start'), 'click', confirmThenStart);
    on($('cal-abort'), 'click', doAbort);
    on($('cal-apply'), 'click', doApplyAsSource);
    on($('cal-copy-ecef'), 'click', copyEcef);
    on($('cal-goto-measure'), 'click', gotoMeasure);
    on($('cal-exit-reuse'), 'click', exitReuse);
    on($('cal-confirm-cancel'), 'click', function () {
      $('cal-confirm-mask').classList.remove('is-open');
      STATE.pendingPayload = null;
    });
    on($('cal-confirm-ok'), 'click', doStart);
    on($('cal-banner-close'), 'click', function () {
      // 只关掉本次页面：状态里那条 INTERRUPTED 仍然在，
      // 换个人打开页面照样能看到（这是安全信息，不该被一个人"消掉"）。
      STATE.bannerClosed = true;
      var box = $('cal-banner');
      if (box) box.classList.add('is-hidden');
    });

    // 方式卡片：整块可点，radio 本身被 .cal-method input 样式藏起来了
    Array.prototype.forEach.call(document.querySelectorAll('input[name="cal-method"]'),
      function (radio) {
        radio.addEventListener('change', function () {
          // 从 cors 切进"复用"是用户在方式卡里选不到的动作，
          // 所以离开 cors 时把 corsSource 复位，回来时默认还是正常测量。
          if (radio.value !== 'cors') STATE.corsSource = 'measure';
          syncMethod();
        });
      });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="cal-coord-src"]'),
      function (radio) {
        radio.addEventListener('change', function () {
          if (radio.value === 'history') {
            if (reuseActive()) {
              // 已经是复用分支了，切换来源就是在改 reuse 的开关
              STATE.coordSource = currentCoordSource();
            } else {
              STATE.coordSource = 'history';
              // cors 方式下选"复用历史"= 进入 A1 复用分支
              if (STATE.method === 'cors') STATE.corsSource = 'history';
            }
          } else {
            STATE.coordSource = radio.value;
          }
          syncMethod();
        });
      });

    document.addEventListener('langchange', function () {
      // 语言切换后重新渲染：型号分组名、能力提示、当前阶段都要跟着变
      if (STATE.options) fillModules(STATE.options.modules);
      applyCapabilities();
      syncMethod();
      if (STATE.lastSnap) render(STATE.lastSnap);
    });

    applyCapabilities();
    loadOptions();
    // 先拉一次状态：页面刚打开时任务通常是空闲的，但"上次被硬中断"的横幅
    // 必须在这一刻就能看到（用户下一步就要拿这块模块当基准站用）。
    poll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
