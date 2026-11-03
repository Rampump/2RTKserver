/* ============================================================================
 * 2RTK NTRIP Server — 检查更新 / 一键升级
 *
 * 这里是整个前端里唯一「必须假设后端会消失一段时间」的模块：
 * 升级流程第一步就是停掉 Web 服务，页面上的 fetch 会被拒绝连接。
 * 所以交互一律做成两段式：
 *     1) 发起任务 → 后端立刻返回（不等待结果）
 *     2) 每 2 秒轮询 /api/upgrade/status，直到读到终态
 * 轮询期间连接失败属于预期现象，不弹错误、不中断，只在界面上写"正在重启"。
 *
 * 卸载**不在**这里：它会删掉整个安装目录，误触不可逆，所以从网页上彻底移除，
 * 只保留命令行 `uninstall.sh`（见 README 与页面上的指路说明）。
 * ========================================================================== */
(function () {
  'use strict';

  const t = (k, a) => (window.I18N && I18N.t ? I18N.t(k, a) : k);
  const $ = (id) => document.getElementById(id);

  const POLL_INTERVAL = 2000;
  const POLL_MAX = 12 * 60 * 1000;      // 升级最长等 12 分钟
  const TERMINAL = new Set(['success', 'failed', 'rolled_back']);

  let pollTimer = null;
  let pollStart = 0;

  // ---------- 小工具 ----------
  async function jsonFetch(url, opts) {
    const res = await fetch(url, Object.assign({ cache: 'no-store' }, opts || {}));
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) {
      const msg = (data && (data.error || data.message)) || ('HTTP ' + res.status);
      throw new Error(msg);
    }
    return data || {};
  }

  function setText(id, text, cls) {
    const node = $(id);
    if (!node) return;
    node.textContent = text;
    if (cls !== undefined) node.className = cls;
  }

  // ---------- 概览渲染 ----------
  function renderVersion(info) {
    const v = info.version || {};
    const parts = [];
    if (v.app) parts.push('v' + v.app);
    parts.push(v.commit || t('maint.ver_unknown'));
    setText('maint-version', parts.join(' · '));
    setText('maint-installed', v.installed_at || '—');
    // 端口只在明确传了数组时才更新。以前这里从已经渲染出来的文本里 split 回来，
    // 元素一旦不存在就直接抛异常，把"检查更新"变成"检查失败"。
    if (Array.isArray(info.ports)) {
      setText('maint-ports', info.ports.join(' / ') || '—');
    }
  }

  function renderSources(info) {
    const sel = $('maint-source');
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = '';
    const auto = document.createElement('option');
    auto.value = 'auto';
    auto.textContent = t('maint.source_auto');
    sel.appendChild(auto);
    (info.sources || []).forEach((s) => {
      const op = document.createElement('option');
      op.value = s.key;
      op.textContent = s.label;
      op.title = s.url;
      sel.appendChild(op);
    });
    if (prev) sel.value = prev;
  }

  function renderCheckResult(data) {
    const box = $('maint-result');
    if (!box) return;
    box.className = 'field-hint maint-result-list';
    box.innerHTML = '';
    const head = document.createElement('div');
    head.className = 'row';
    if (!data.reachable) {
      head.innerHTML = '<span class="is-bad">' + t('maint.no_source') + '</span>';
      box.appendChild(head);
    } else if (data.has_update) {
      head.innerHTML = '<span class="is-warn">' + t('maint.has_update') + '</span>';
      box.appendChild(head);
    } else if (data.local_commit_known === false) {
      // 本机没记录安装提交：此时"没有更新"和"不知道"是两回事，不能骗用户说已是最新
      head.innerHTML = '<span class="is-warn">' + t('maint.commit_unknown') + '</span>';
      box.appendChild(head);
    } else {
      head.innerHTML = '<span class="is-ok">' + t('maint.up_to_date') + '</span>';
      box.appendChild(head);
    }
    (data.sources || []).forEach((s) => {
      const row = document.createElement('div');
      row.className = 'row';
      const mark = s.ok
        ? (s.has_update ? '<span class="is-warn">↑</span>' : '<span class="is-ok">✓</span>')
        : '<span class="is-bad">✕</span>';
      const detail = s.ok
        ? '<span class="mono">' + (s.commit || '') + '</span>'
        : '<span class="dim">' + (s.error || '') + '</span>';
      row.innerHTML = mark + '<span>' + s.label + '</span>' + detail;
      box.appendChild(row);
    });
  }

  // ---------- 遮罩 ----------
  function setOverlay(stage, progress, message, log, foot) {
    const overlay = $('maint-overlay');
    if (!overlay) return;
    overlay.classList.remove('hidden');
    if (stage) setText('maint-stage', stage);
    const bar = $('maint-bar');
    if (bar && typeof progress === 'number') {
      bar.style.width = Math.max(2, Math.min(100, progress)) + '%';
    }
    if (message) setText('maint-msg', message);
    const pre = $('maint-log');
    if (pre && typeof log === 'string' && log !== '') {
      pre.textContent = log;
      pre.scrollTop = pre.scrollHeight;
    }
    if (foot !== undefined) setText('maint-foot', foot);
  }

  function hideOverlay() {
    const overlay = $('maint-overlay');
    if (overlay) overlay.classList.add('hidden');
  }

  // ---------- 轮询 ----------
  function stopPoll() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function schedulePoll() {
    stopPoll();
    pollTimer = setTimeout(poll, POLL_INTERVAL);
  }

  async function poll() {
    if (Date.now() - pollStart > POLL_MAX) {
      showManualHint(t('maint.timed_out'), t('maint.timed_out_msg'), t('maint.foot_manual'));
      return;
    }
    try {
      const res = await fetch('/api/upgrade/status', { cache: 'no-store' });
      if (res.status === 401) {
        setOverlay(t('maint.session_lost'), 100, t('maint.session_lost_msg'), undefined, '');
        return;
      }
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const st = data.state || {};

      setOverlay(st.stage_text || st.stage || '', st.progress || 0,
        st.message || '', data.log_tail || '', buildFoot(st));

      if (TERMINAL.has(st.stage) || (st.finished_at && st.finished_at !== '')) {
        finish(st);
        return;
      }
      schedulePoll();
    } catch (e) {
      // 服务正在重启，连接被拒是预期内的：不报错，只把阶段写清楚继续等。
      setOverlay(t('maint.restarting'), null,
        t('maint.restarting_msg'), undefined,
        t('maint.foot_wait', { n: Math.round((Date.now() - pollStart) / 1000) }));
      schedulePoll();
    }
  }

  // 等待超时 / 需要人工介入：给一个能刷新的出口，否则遮罩永远关不掉
  function showManualHint(title, msg, foot) {
    stopPoll();
    setOverlay(title, 100, msg, undefined, foot);
    const panel = document.querySelector('.maint-panel');
    if (panel && !panel.querySelector('.maint-close')) {
      const btn = document.createElement('button');
      btn.className = 'btn btn-secondary maint-close';
      btn.style.marginTop = '12px';
      btn.textContent = t('maint.close');
      btn.onclick = () => window.location.reload();
      panel.appendChild(btn);
    }
  }

  function buildFoot(st) {
    if (!st.finished_at) {
      return t('maint.foot_wait', { n: Math.round((Date.now() - pollStart) / 1000) });
    }
    return t('maint.foot_done');
  }

  function finish(st) {
    stopPoll();
    const ok = st.stage === 'success';
    const rolled = st.stage === 'rolled_back';
    const title = ok ? t('maint.ok') : (rolled ? t('maint.rolled') : t('maint.failed'));
    const msg = st.message || '';
    const bar = $('maint-bar');
    if (bar) bar.style.width = '100%';

    // 转圈的图标到终态必须停下来，否则用户看着还在转，会以为没结束
    const icon = document.querySelector('.maint-title i');
    if (icon) {
      icon.classList.remove('fa-spin', 'fa-cog', 'fa-check', 'fa-times', 'fa-warning');
      icon.classList.add(ok ? 'fa-check' : 'fa-times', ok ? 'is-ok' : 'is-bad');
    }

    setOverlay(title, 100, msg, undefined, ok ? t('maint.reload_soon') : t('maint.can_close'));

    const panel = document.querySelector('.maint-panel');
    if (!ok && panel && !panel.querySelector('.maint-reason')) {
      // 失败原因必须单独列出来：只显示"升级失败"等于什么都没说，
      // 用户既不知道是网络问题还是版本本身的问题，也不知道现在跑的是哪一版。
      const box = document.createElement('div');
      box.className = 'maint-msg maint-reason is-bad';
      const reason = st.error || msg || t('maint.unknown_reason');
      const from = st.from_version || st.old_commit || '';
      let text = t('maint.reason', { msg: reason });
      if (rolled && from) text += ' ' + t('maint.rolled_to', { v: from });
      box.textContent = text;
      panel.insertBefore(box, panel.querySelector('.maint-close') || null);

      const btn = document.createElement('button');
      btn.className = 'btn btn-secondary maint-close';
      btn.style.marginTop = '12px';
      btn.textContent = t('maint.close');
      btn.onclick = () => window.location.reload();
      panel.appendChild(btn);
    } else if (ok) {
      setTimeout(() => window.location.reload(), 5000);
    }
  }

  // ---------- 动作 ----------
  async function doCheck() {
    const btn = $('maint-check');
    btn.disabled = true;
    const old = btn.querySelector('span');
    if (old) old.textContent = t('maint.checking');
    try {
      const data = await jsonFetch('/api/upgrade/check', { method: 'POST' });
      renderCheckResult(data);
      // 只更新版本，端口保持上一次渲染的结果（check 接口不下发端口列表）
      renderVersion({ version: data.current });
    } catch (e) {
      const box = $('maint-result');
      box.className = 'field-hint is-bad';
      box.textContent = t('maint.check_fail', { msg: e.message });
    } finally {
      btn.disabled = false;
      if (old) old.textContent = t('maint.check');
    }
  }

  async function doUpgrade() {
    if (!window.confirm(t('maint.confirm_upgrade'))) return;
    const btn = $('maint-upgrade');
    btn.disabled = true;
    try {
      const data = await jsonFetch('/api/upgrade/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: $('maint-source').value || 'auto' }),
      });
      pollStart = Date.now();
      setOverlay(t('maint.starting'), 3, data.message || t('maint.starting_msg'), '', '');
      schedulePoll();
    } catch (e) {
      btn.disabled = false;
      const box = $('maint-result');
      box.className = 'field-hint is-bad';
      box.textContent = t('maint.start_fail', { msg: e.message });
    }
  }

  // ---------- 初始化 ----------
  // 上一次升级失败了，但用户已经刷新/重连过页面：不能再靠遮罩（那是任务进行中才有的），
  // 必须把原因和"已退回原版本"再讲一遍，否则用户只看到一句"当前版本"，什么都不知道。
  // 只回溯 30 分钟内的结果，避免几个月前的失败一直挂在页面上。
  function renderLastResult(st) {
    if (st.active) return;
    if (st.stage !== 'failed' && st.stage !== 'rolled_back') return;
    const when = Date.parse(String(st.finished_at || '').replace(' ', 'T'));
    if (isNaN(when) || Date.now() - when > 30 * 60 * 1000) return;
    const box = $('maint-result');
    if (!box) return;
    const from = st.from_version || st.old_commit || '';
    let text = t('maint.reason', { msg: st.error || st.message || t('maint.unknown_reason') });
    if (st.stage === 'rolled_back' && from) text += ' ' + t('maint.rolled_to', { v: from });
    box.className = 'field-hint is-bad';
    box.textContent = text;
  }

  async function refreshStatus() {
    try {
      const data = await jsonFetch('/api/upgrade/status');
      renderVersion(data);
      renderSources(data);
      const st = data.state || {};
      if (st.active) {
        // 上一次升级没结束（比如刷新了页面），直接接管进度显示
        pollStart = Date.now();
        setOverlay(st.stage_text || '', st.progress || 0, st.message || '',
          data.log_tail || '', '');
        schedulePoll();
      } else {
        renderLastResult(st);
      }
    } catch (e) {
      setText('maint-version', t('maint.load_fail'), 'mono is-bad');
    }
  }

  // 给页头的「重新读取」按钮用：不刷新整页，只重拉一次状态，
  // 免得用户刚打开的升级进度遮罩被刷新冲掉。
  window.Maintenance = { refresh: refreshStatus };

  document.addEventListener('DOMContentLoaded', function () {
    if (!$('maint-card')) return;
    $('maint-check').addEventListener('click', doCheck);
    $('maint-upgrade').addEventListener('click', doUpgrade);
    document.addEventListener('langchange', refreshStatus);
    refreshStatus();
  });
})();
