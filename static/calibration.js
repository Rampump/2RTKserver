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
    lastTaskId: '',
    method: 'cors',
    // cors 方式下的坐标来源：'measure'（正常走 CORS 测）或 'history'（复用）
    corsSource: 'measure',
    // manual 方式下的坐标来源：typing / history / measure_first
    coordSource: 'typing',
    ready: false,
    applied: false,
    hwTaskId: '',
    bannerClosed: false,
    lastSnap: null,
    // 本机（当前模块 SN）最近一次标定记录 —— 「复用坐标」只认这一条。
    // 全库最近几条只用于展示，不能当复用来源：拿别的设备的坐标写进来，
    // 1005 校验会照常通过，现场却播的是别人家的位置。
    ownSn: '',
    ownHistory: null,
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

  function showError(message) {
    var box = $('cal-global-error');
    if (!box) return;
    if (!message) { box.classList.add('is-hidden'); box.textContent = ''; return; }
    box.textContent = message;
    box.classList.remove('is-hidden');
  }

  function stageLabel(stage, fallback) {
    if (!stage) return t('cal.stage.idle');
    var key = 'cal.stage.' + stage;
    var text = t(key);
    // I18N.t 找不到 key 时会原样返回 key，这里据此回退到后端文案
    return (text && text !== key) ? text : (fallback || stage);
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
      hint.textContent = bauds.length
        ? t('cal.baud.auto_tip', { list: bauds.join(' / ') })
        : t('cal.baud.auto_tip_plain');
      hint.classList.remove('is-warn');
      return;
    }
    if (legal === null) {
      hint.textContent = t('cal.baud.invalid');
      hint.classList.add('is-warn');
      return;
    }
    if (bauds.length && bauds.indexOf(legal) < 0) {
      hint.textContent = t('cal.baud.offlist', { value: legal, list: bauds.join(' / ') });
      hint.classList.add('is-warn');
      return;
    }
    hint.textContent = t('cal.baud.fixed', { value: legal });
    hint.classList.remove('is-warn');
  }

  // 当前所选型号的官方波特率档位（自动探测时返回空数组 = 不限制）。
  function currentModuleBauds() {
    var mod = selectedModule();
    return (mod && mod.bauds) ? mod.bauds : [];
  }

  // 型号切换时刷新 datalist 候选与提示文案
  function refreshBaudOptions() {
    var list = $('cal-baud-list');
    var bauds = currentModuleBauds();
    if (!bauds.length) {
      // 自动探测：把已注册型号的档位并起来给个参考（去重、降序）
      var set = [];
      (((STATE.options || {}).modules || {}).all || []).forEach(function (m) {
        (m.bauds || []).forEach(function (b) { if (set.indexOf(b) < 0) set.push(b); });
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
        fillModules(data.modules);
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
        if (data.running) startPolling();
        return data;
      })
      .catch(function (err) {
        if (err && err.message === 'unauth') return;
        showError(t('cal.err.options') + ': ' + err.message);
      });
  }

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
      { label: t('cal.module.single'), items: (modules && modules.single_mode) || [] }
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
        // 能力表整份留在 DOM 上：applyCapabilities() 要按它决定"哪些块根本不该出现"。
        try { opt.dataset.caps = JSON.stringify(item.caps || {}); }
        catch (e) { opt.dataset.caps = '{}'; }
        og.appendChild(opt);
      });
      select.appendChild(og);
    });
    select.value = previous || '__auto__';
  }

  function selectedModule() {
    var el = $('cal-module');
    var id = el ? el.value : '__auto__';
    var all = (STATE.options && STATE.options.modules && STATE.options.modules.all) || [];
    for (var i = 0; i < all.length; i++) { if (all[i].id === id) return all[i]; }
    return null;
  }

  // 当前型号的能力表。自动识别（__auto__）时返回 null，
  // 表示"还不知道是什么模块"——此时一律按最保守处理。
  function selectedCaps() {
    var mod = selectedModule();
    return mod ? (mod.caps || {}) : null;
  }

  // 自动识别时 mod 为 null，这里用"先放开"的策略：识别不出模块不该等于
  // "什么都不能做"，真要跑起来后端会按实际识别到的型号再拦一次。
  function canWrite() {
    var mod = selectedModule();
    if (!mod) return true;
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

  // ---------------- 能力锁 ----------------
  // 每个方式卡片按"后端到底会拦什么"来决定锁不锁。锁定比后端严 → 用户被白白挡住；
  // 锁定比后端松 → 点下去才 400。这里逐条对着 api_calibration.start_* 写。
  function computeLocks() {
    var mod = selectedModule();
    var caps = selectedCaps() || {};
    var writable = canWrite();
    var locks = { cors: '', survey_in: '', manual: '', external_rover: '' };
    if (!mod) return locks;   // 自动识别：全部放开

    var name = mod.name || mod.id;
    // 三条写路径后端各有一处判据，**不是一套统一规则**，这里逐条对齐：
    //   start_write     → 先命令集，再 fixed（`_reject_write_without_fixed`）
    //   start_survey_in → 先命令集，再**只**看 svin（不查 fixed）
    //   start_cors      → 复用分支 fixed+set_mode；正常分支再加 dual_mode
    //                     （`_reject_without_caps`：先命令集，再看能力位）
    // 顺序不能省「先看命令集」这层：能力位全是 undefined 的空壳驱动（如 passive）
    // 会被当成支持，页面放开、后端 400 —— 正是"按钮能点但点了报错"的来源。
    // 复用分支不切流动站，所以不查 dual_mode；但同样要切回基准站并写 NVM，
    // 所以 fixed / set_mode 照查。
    if (!writable) {
      locks.cors = t('cal.lock.manual', { name: name });
    } else if (caps.fixed === false) {
      locks.cors = t('cal.lock.cors_fixed', { name: name });
    } else if (caps.set_mode !== true) {
      locks.cors = t('cal.lock.cors_set_mode', { name: name });
    } else if (!mod.dual_mode) {
      locks.cors = t('cal.lock.cors_dual', { name: name });
    }
    // 后端 start_survey_in 只查 caps.svin；这里**不能**顺手把 fixed 也加上，
    // 否则某些只能自收敛、不能立即写固定坐标的型号会被前端误锁。
    // 但「没有命令集」这条要先拦：SVIN 是靠下发命令跑的。
    if (!writable) {
      locks.survey_in = t('cal.lock.manual', { name: name });
    } else if (caps.svin !== true) {
      locks.survey_in = t('cal.lock.svin', { name: name });
    }
    if (!writable) {
      locks.manual = t('cal.lock.manual', { name: name });
    } else if (caps.fixed === false) {
      locks.manual = t('cal.lock.manual_caps', { name: name });
    }
    // 只测不写**对任何模块都开放**：它不写 NVM、不需要命令集，
    // 无命令集的第三方模块正是它的主要用途（后端 start_cors(measure_only=True)
    // 也不查 command_count / caps）。所以这里永远不加锁，别把它和写路径捆在一起。
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
    if (STATE.method === 'cors') return STATE.corsSource === 'history' && !!STATE.ownHistory;
    if (STATE.method === 'manual') {
      return STATE.coordSource === 'history' && !!STATE.ownHistory;
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
    var hasHistory = !!STATE.ownHistory;
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
      steps = ['PREPARE', 'OPEN', 'DETECT', 'BACKUP', 'ROVER_SETUP', 'NMEA_SETUP',
        'GGA_WAIT', 'CORS_CONNECT', 'FIX_WAIT', 'COLLECT', 'CORS_CLOSE',
        'RESTART', 'INJECT'];
      return { steps: steps, skip: {} };
    }
    if (method === 'survey_in') {
      steps = ['PREPARE', 'OPEN', 'DETECT', 'BACKUP', 'SVIN_CFG', 'SVIN_WAIT',
        'WRITE_COORD', 'BASE_SETUP', 'MSG_SETUP', 'SAVE', 'RESTART',
        'VERIFY_MODE', 'VERIFY_RTCM', 'INJECT'];
      return { steps: steps, skip: {} };
    }
    if (method === 'cors' && !reuse) {
      steps = ['PREPARE', 'OPEN', 'DETECT', 'BACKUP', 'ROVER_SETUP', 'NMEA_SETUP',
        'GGA_WAIT', 'CORS_CONNECT', 'FIX_WAIT', 'COLLECT', 'CORS_CLOSE',
        'WRITE_COORD', 'BASE_SETUP', 'MSG_SETUP', 'SAVE', 'RESTART',
        'VERIFY_MODE', 'VERIFY_RTCM', 'INJECT'];
      return { steps: steps, skip: {} };
    }
    // cors 复用 / manual：一上来就写坐标，没有采集段。
    // 复用分支后端**仍会**推一条 COLLECT 阶段（samples=0），页面上划掉它
    // 正好把"本次跳过测量"这个事实画出来，而不是让用户以为没测就是没跑。
    steps = ['PREPARE', 'OPEN', 'DETECT', 'BACKUP'];
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
    // 会"实际改动模块 NVM"的几步用强调色，其余留灰
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
    // 全库最近几条只在"还没读到本机 SN"时兜底展示；能复用的一律以 ownHistory 为准
    if (!STATE.ownHistory && STATE.history) STATE.ownHistoryFallback = STATE.history;
    renderHistoryList();
  }

  function renderHistoryList() {
    var list = $('cal-hist-list');
    if (!list) return;
    var record = STATE.ownHistory;
    if (!record) {
      list.innerHTML = '<div class="cal-muted">' + escapeHtml(t('cal.history.none')) +
        '<br>' + escapeHtml(t('cal.history.none_hint')) + '</div>';
      var hintBox = $('cal-hist-hint');
      if (hintBox) hintBox.textContent = '';
      return;
    }
    var accuracy = record.accuracy ? (Number(record.accuracy) * 100).toFixed(1) + ' cm' : '—';
    var methodKey = 'cal.method.short.' + String(record.method || '');
    var methodText = hasKey(methodKey) ? t(methodKey) : (record.method || '—');
    list.innerHTML =
      '<label class="cal-hist-item is-active" data-id="' + escapeHtml(record.id || '') + '">' +
        '<div class="h-main">' +
          '<div class="h-when">' + escapeHtml(record.created_at || '—') + '</div>' +
          '<div class="h-meta">' +
            escapeHtml(t('cal.history.method')) + '：' + escapeHtml(methodText) +
            ' · ' + escapeHtml(t('cal.history.accuracy')) + '：' + escapeHtml(accuracy) +
          '</div>' +
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

  // 「可复用」的历史坐标只认本机 SN 的记录。
  // 全库最近几条只是展示用 —— 拿另一块模块的坐标写进这台机器，
  // 1005 校验会照常通过（它只核对"写入=播发"），现场却播的是别人家的位置。
  function fillOwnHistory(items) {
    STATE.ownHistory = (items && items.length) ? items[0] : null;
    // 本机没记录时不能拿全库记录顶上：那可能是别的天线/别的模块的坐标
    if (!STATE.ownHistory && STATE.method === 'cors' && STATE.corsSource === 'history') {
      STATE.corsSource = 'measure';
    }
    if (!STATE.ownHistory && STATE.coordSource === 'history') {
      STATE.coordSource = 'typing';
      var radio = document.querySelector('input[name="cal-coord-src"][value="typing"]');
      if (radio) radio.checked = true;
    }
    var histRadio = document.querySelector('input[name="cal-coord-src"][value="history"]');
    if (histRadio) histRadio.checked = !!STATE.ownHistory && histRadio.checked;
    syncMethod();
  }

  // ---------------- 串口扫描 ----------------
  function scanPorts() {
    fetch('/api/serial_ports', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var list = $('cal-port-list');
        if (!list) return;
        list.innerHTML = '';
        (data.ports || data || []).forEach(function (p) {
          var opt = document.createElement('option');
          opt.value = p.device;
          opt.label = p.description || p.device;
          list.appendChild(opt);
        });
        if (!list.childElementCount) showError(t('cal.err.no_ports'));
        else showError('');
      })
      .catch(function (err) { showError(t('cal.err.ports') + ': ' + err.message); });
  }

  // ---------------- 读取模块信息（只读诊断，不写任何配置） ----------------
  // 后端是异步任务：POST 返回 202 后由轮询推进；任务 DONE 时再拉一次 full
  // 取回 report.module / report.mode。不能在这里等同步返回。
  function readModuleInfo() {
    var port = $('cal-port').value.trim();
    if (!port) { showError(t('cal.err.need_port')); return; }
    var baud = baudValue();
    var btn = $('cal-detect');
    if (btn) btn.disabled = true;
    showError('');
    fetch('/api/calibration/detect', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        port: port, baud: baud,
        module: (selectedModule() || {}).id || null
      })
    })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok || !res.body.ok) {
          showError(res.body.error || t('cal.err.detect'));
          if (btn) btn.disabled = false;
          return;
        }
        startPolling();
      })
      .catch(function (err) {
        showError(t('cal.err.detect') + ': ' + err.message);
        if (btn) btn.disabled = false;
      });
  }

  function renderHw(report) {
    var box = $('cal-hw');
    if (!box) return;
    var mod = (report && report.module) || {};
    var mode = (report && (report.mode || {}).text) || 'unknown';
    // i18n 里没有这个键时 t() 会原样返回键名，这时退回模块报的原始模式串
    var modeLabel = t('cal.hw.mode.' + mode);
    if (modeLabel === 'cal.hw.mode.' + mode) modeLabel = String(mode);
    var rows = [
      ['cal.hw.firmware', mod.firmware || '—'],
      ['cal.hw.sn', mod.sn || '—'],
      ['cal.hw.mode', modeLabel],
      ['cal.hw.uniq', mod.uniq_id || '—']
    ];
    box.innerHTML = rows.map(function (row) {
      return '<div><div class="k">' + escapeHtml(t(row[0])) + '</div><div class="v">' +
        escapeHtml(row[1]) + '</div></div>';
    }).join('');
    box.classList.remove('is-hidden');

    // 拿到 SN 才能判断"这台设备自己标定过没有"。
    // 页面初次加载时还没读模块，`own_history` 是空的；
    // 这里补一次带 SN 的请求，把「复用坐标」的来源限定在本机记录上。
    if (mod.sn && mod.sn !== STATE.ownSn) {
      STATE.ownSn = mod.sn;
      fetch('/api/calibration/options?sn=' + encodeURIComponent(mod.sn),
        { credentials: 'same-origin', cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data || !data.ok) return;
          fillOwnHistory(data.own_history);
        })
        .catch(function () { /* 取不到就当本机没记录：宁可不复用，也不能拿错坐标 */ });
    }
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
        if (typed) return;                       // 手填优先，不动它
        if (!items.length) { showError(t('cal.err.mounts_empty')); return; }
        input.value = items[0].mountpoint || '';
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
      .catch(function () { /* 网络抖动忽略，下一轮再试 */ });
  }

  function render(snap) {
    var wasRunning = STATE.running;
    STATE.lastSnap = snap;
    STATE.running = !!snap.running;
    setFormLocked(STATE.running);

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
    // 1005 播发值 vs 写入值的偏差：这条是"链路通不通"的唯一硬证据
    var broadcast = (summary && summary.broadcast) || (result && result.broadcast);
    var delta = (summary && summary.delta) || (result && result.delta);
    if (broadcast && broadcast.x !== undefined) {
      var deltaText = (delta === null || delta === undefined) ? '—'
        : (Number(delta) * 100).toFixed(2) + ' cm';
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
    if (!live) {
      ['m-sats', 'm-pdop', 'm-fix', 'm-samples', 'm-sigma', 'm-gga'].forEach(function (id) {
        var el = $(id); if (el) { el.textContent = '—'; el.className = 'v'; }
      });
      if (guardBox) guardBox.classList.add('is-hidden');
      return;
    }
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

  function appendTimeline(snap) {
    if (!snap.stage) return;
    var key = snap.task_id + '|' + snap.stage + '|' + (snap.message || '');
    if (key === STATE.lastStage) return;
    STATE.lastStage = key;

    var list = $('cal-timeline');
    if (!list) return;
    if (snap.task_id && snap.task_id !== STATE.lastTaskId) {
      STATE.lastTaskId = snap.task_id;
      list.innerHTML = '';
      STATE.applied = false;
    }

    var li = document.createElement('li');
    var isFail = snap.stage === 'FAILED' || snap.stage === 'ABORTED';
    li.className = isFail ? 'is-fail' : (snap.stage === 'DONE' ? 'is-ok' : '');
    var time = document.createElement('time');
    time.textContent = (snap.started_at || '').split(' ').pop() || '';
    li.appendChild(time);
    li.appendChild(document.createTextNode(
      stageLabel(snap.stage, snap.stage_text) + (snap.message ? ' · ' + snap.message : '')));
    list.appendChild(li);
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
      'cal-coord-msg', 'cal-msm', 'cal-svin-resume', 'cal-scan', 'cal-rescan',
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
  // 复用坐标的**唯一**来源：STATE.ownHistory（按本机 SN 过滤后的那一条）。
  // STATE.history 是全库最近几条，可能是别的模块、别的天线的坐标 —— 绝不能用，
  // 否则写进 NVM 后 1005 校验照常通过（它只核对"写入值 == 播发值"），
  // 现场表现是"基准站一直在播别人家的位置"，而且不报任何错。
  function historyCoord() {
    var r = STATE.ownHistory;
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
      // 选了型号就只让后端问这一个驱动：省 1~2 秒，选错型号也能立刻报出来
      module: mod.id || null,
      remark: t('cal.remark.auto')
    };

    // RTCM 输出只在"会写模块 且 模块能配报文"时才下发。
    // 块被藏起来时不能顺手把 DOM 里那份值发出去 —— 那等于用户没做选择却被写了一份配置。
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
        // 值只能来自 STATE.ownHistory —— 不能是 STATE.history（全库最近几条）。
        if (!STATE.ownHistory) return payload;
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
          payload.height = parseFloat($('cal-h').value) || 0;
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
    }
    // 能力互斥：后端第一步也会拦，但让用户点按钮之前就知道，
    // 比跑起来再报错省一次等待（而且省掉一次串口占用）。
    // 判据顺序与后端 `_reject_without_caps()` 保持一致：先命令集，再能力位。
    var mod = selectedModule() || {};
    if (mod.id) {
      var caps = mod.caps || {};
      // 复用分支不切流动站，所以不查 dual_mode
      if (method === 'cors' && !payload.reuse_arp) {
        if (mod.dual_mode === false) return t('cal.err.base_only');
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
    if (method === 'external_rover') {
      lines.push(t('cal.confirm.measure_only'));
      var secondsM = payload.duration === 'long' ? 600
        : (payload.duration === 'short' ? 180 : 300);
      lines.push(t('cal.confirm.stop', { seconds: secondsM + 120 }));
    } else if (method === 'cors' && !payload.reuse_arp) {
      var seconds = payload.duration === 'long' ? 600
        : (payload.duration === 'short' ? 180 : 300);
      lines.push(t('cal.confirm.stop', { seconds: seconds + 120 }));
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
    var target = $('cal-cors-card') || $('cal-sec-method');
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
    on($('cal-rescan'), 'click', function () { loadOptions(); });
    on($('cal-load-mounts'), 'click', loadMountpoints);
    on($('cal-module'), 'change', function () {
      applyCapabilities();
      refreshBaudOptions();
    });
    on($('cal-baud'), 'input', checkBaud);
    on($('cal-baud'), 'change', checkBaud);
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