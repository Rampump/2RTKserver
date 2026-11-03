/* ============================================================================
 * 2RTK NTRIP server — 候选下拉（替代浏览器原生 datalist 弹层）
 *
 * 为什么不用原生 `<datalist>`：
 *   原生弹层的位置由浏览器自己算，在带 `transform` / `filter` 的祖先元素里会算错。
 *   本项目每张卡片都挂了 `.fade-in`（`animation: fade-in ... both`，末帧
 *   `transform: translateY(0)`），这个"末帧也是 transform"会把卡片变成一个新的
 *   包含块 —— 于是"扫描串口""读取挂载点"填完候选后，下拉列表出现在离输入框
 *   很远的地方（真机反馈：满屏乱跑）。这是浏览器行为，不是样式能改掉的。
 *
 * 做法：
 *   1. 把 `list="xxx"` 从 input 上摘掉（只留 `data-picker-src="xxx"`），
 *      原生弹层就彻底不出现了；
 *   2. 候选仍从原来的 `<datalist>` 里读 —— 后端/页面写入候选的代码一行都不用改，
 *      动态填进去的 option 也不会丢；
 *   3. 面板挂在 `document.body` 下、`position: fixed`，坐标由
 *      `getBoundingClientRect()` 现算，所以祖先有没有 transform / overflow 都无所谓。
 *
 * 用法：
 *   <input type="text" list="port-list">            <!-- 自动增强，无需改 HTML -->
 *   Picker.open(document.getElementById('cal-port')) // 扫描完主动弹出来
 * ========================================================================== */
(function (global) {
  'use strict';

  var PANEL_CLASS = 'picker-panel';
  var ITEM_CLASS = 'picker-item';
  var MAX_HEIGHT = 268;
  var MIN_WIDTH = 200;

  var panel = null;
  var activeInput = null;      // 当前打开面板的输入框
  var items = [];              // [{value, label}]
  var cursor = -1;
  var suppressInput = false;   // 由 commit() 写入值时，不要把 input 事件当成"用户在打字"

  // ---------------- 小工具 ----------------
  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (ch) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch];
    });
  }

  function sourceOf(input) {
    var id = input.getAttribute('data-picker-src');
    return id ? document.getElementById(id) : null;
  }

  function readItems(input) {
    var src = sourceOf(input);
    if (!src) return [];
    var out = [];
    var seen = {};
    for (var i = 0; i < src.options.length; i++) {
      var opt = src.options[i];
      var value = String(opt.value == null ? '' : opt.value).trim();
      if (!value || seen[value]) continue;
      seen[value] = true;
      // option 的 label 是"这条候选的说明"（例如挂载点的格式/导航系统），
      // 没有 label 就退回显示 value 本身，不要凭空造一段说明。
      var label = String(opt.label || '').trim();
      // disabled 的候选**保留显示**但不可选：配置页把"当前已用作 GNSS 模块的串口"
      // 标成不可选并写明了原因，直接过滤掉等于把这条解释一起藏了，
      // 用户只会觉得"扫描少了一个口"。
      out.push({
        value: value,
        label: label === value ? '' : label,
        disabled: !!opt.disabled
      });
    }
    return out;
  }

  // ---------------- 面板 ----------------
  function ensurePanel() {
    if (panel && document.body.contains(panel)) return panel;
    panel = document.createElement('div');
    panel.className = PANEL_CLASS;
    panel.id = 'rtk-picker-panel';
    panel.setAttribute('role', 'listbox');
    panel.hidden = true;
    document.body.appendChild(panel);

    // 用 mousedown 而不是 click：点击面板会让输入框先失焦，blur 一收面板，
    // click 就永远派发不到条目上（经典坑）。
    panel.addEventListener('mousedown', function (ev) {
      ev.preventDefault();                     // 保住输入框焦点与已输入内容
      var node = closestItem(ev.target);
      if (node) commit(Number(node.getAttribute('data-index')));
    });
    panel.addEventListener('mousemove', function (ev) {
      var node = closestItem(ev.target);
      if (node) setCursor(Number(node.getAttribute('data-index')), false);
    });
    return panel;
  }

  function closestItem(node) {
    while (node && node !== panel) {
      if (node.classList && node.classList.contains(ITEM_CLASS)) return node;
      node = node.parentNode;
    }
    return null;
  }

  function render() {
    var html = [];
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      html.push(
        '<div class="' + ITEM_CLASS + (it.disabled ? ' is-disabled' : '') +
        '" role="option" data-index="' + i + '"' +
        (it.disabled ? ' aria-disabled="true"' : '') + '>' +
        '<span class="picker-main">' + esc(it.value) + '</span>' +
        (it.label ? '<span class="picker-sub">' + esc(it.label) + '</span>' : '') +
        '</div>'
      );
    }
    panel.innerHTML = html.join('');
  }

  // 从 index 出发按 dir 走一格，跳过不可选项；走不到就停在原地
  function step(index, dir) {
    var count = items.length;
    if (!count) return -1;
    var next = index;
    for (var i = 0; i < count; i++) {
      next += dir;
      if (next < 0) next = count - 1;
      if (next >= count) next = 0;
      if (!items[next].disabled) return next;
    }
    return index;
  }

  function setCursor(index, scroll) {
    var nodes = panel.querySelectorAll('.' + ITEM_CLASS);
    if (!nodes.length) { cursor = -1; return; }
    if (index < 0) index = items.length - 1;
    if (index >= items.length) index = 0;
    if (items[index] && items[index].disabled) index = step(index, 1);
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].classList.toggle('is-active', i === index);
    }
    cursor = index;
    if (scroll !== false && nodes[index] && nodes[index].scrollIntoView) {
      nodes[index].scrollIntoView({ block: 'nearest' });
    }
  }

  // 面板坐标一律现算：`position: fixed` + 视口坐标，
  // 不受任何祖先的 transform / overflow 影响（这正是原生 datalist 出错的地方）。
  function place(input) {
    var rect = input.getBoundingClientRect();
    var vw = document.documentElement.clientWidth || window.innerWidth;
    var vh = document.documentElement.clientHeight || window.innerHeight;

    var width = Math.max(rect.width, MIN_WIDTH);
    width = Math.min(width, vw - 16);
    var left = Math.min(Math.max(8, rect.left), Math.max(8, vw - width - 8));

    panel.style.width = width + 'px';
    panel.style.left = Math.round(left) + 'px';
    panel.style.maxHeight = Math.min(MAX_HEIGHT, Math.max(120, vh - 24)) + 'px';
    panel.style.top = '0px';                   // 先落位再量高度，避免量到上一次的 top

    var h = panel.offsetHeight;
    var spaceBelow = vh - rect.bottom - 10;
    var spaceAbove = rect.top - 10;
    // 下方放不下就翻到上方（上方更宽裕时）；两边都放不下则贴着下方，面板内部滚动
    var top = (h <= spaceBelow || spaceAbove <= spaceBelow)
      ? rect.bottom + 4
      : rect.top - h - 4;
    top = Math.max(8, Math.min(top, vh - h - 8));
    panel.style.top = Math.round(top) + 'px';
  }

  function filterItems(all, text) {
    var needle = String(text || '').trim().toLowerCase();
    if (!needle) return all.slice();
    var starts = [], contains = [];
    for (var i = 0; i < all.length; i++) {
      var v = all[i].value.toLowerCase();
      var l = (all[i].label || '').toLowerCase();
      if (v.indexOf(needle) === 0 || l.indexOf(needle) === 0) starts.push(all[i]);
      else if (v.indexOf(needle) >= 0 || l.indexOf(needle) >= 0) contains.push(all[i]);
    }
    return starts.concat(contains);
  }

  function open(input, text) {
    if (!input || input.disabled || input.readOnly) return false;
    // 输入框被隐藏（所在分区没勾选/没展开）时不要开面板：
    // 它此刻的 rect 是 0×0，面板会被摆到页面左上角 —— 那正是"乱跑"的样子。
    var rect = input.getBoundingClientRect();
    if (!rect.width && !rect.height) return false;
    var all = readItems(input);
    if (!all.length) { close(); return false; }

    activeInput = input;
    items = filterItems(all, text);
    if (!items.length) { close(); return false; }

    ensurePanel();
    render();
    cursor = -1;
    panel.hidden = false;
    panel.classList.add('is-open');
    input.setAttribute('aria-expanded', 'true');
    place(input);
    return true;
  }

  function close() {
    if (activeInput) activeInput.setAttribute('aria-expanded', 'false');
    activeInput = null;
    items = [];
    cursor = -1;
    if (panel) {
      panel.hidden = true;
      panel.classList.remove('is-open');
      panel.innerHTML = '';
    }
  }

  function commit(index) {
    var it = items[index];
    if (!it || it.disabled || !activeInput) return;
    var input = activeInput;
    suppressInput = true;
    input.value = it.value;
    // 页面上的联动（波特率合法性检查、地址变更清空缓存）都挂在 input/change 上，
    // 这里必须补派发，否则"选了候选但提示没更新"。
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    suppressInput = false;
    close();
    input.focus();
  }

  // ---------------- 事件 ----------------
  function onFocus(input) {
    return function () {
      // 聚焦先不弹（和原生一致）：一进页面就冒出一堆候选很吵。
      // 真正弹出由"点击"或"按下方向键"触发。
    };
  }

  function onInput(input) {
    return function () {
      if (suppressInput) return;
      var text = input.value;
      var pos = input.selectionStart;
      open(input, text);
      // 重排候选不应该把光标顶到末尾（面板挂 body 上不会动 DOM，这里只是保险）
      try { if (pos != null) input.setSelectionRange(pos, pos); } catch (e) { /* number 输入框不支持 */ }
    };
  }

  function onKeyDown(input) {
    return function (ev) {
      var isOpen = !!(panel && !panel.hidden && activeInput === input);

      if (ev.key === 'ArrowDown') {
        ev.preventDefault();
        if (!isOpen) { open(input, ''); return; }
        setCursor(step(cursor, 1));
        return;
      }
      if (ev.key === 'ArrowUp') {
        if (!isOpen) return;
        ev.preventDefault();
        setCursor(step(cursor, -1));
        return;
      }
      if (ev.key === 'Enter') {
        if (!isOpen) return;
        if (cursor >= 0) {
          ev.preventDefault();               // 有高亮项时"回车=选中"，不要顺手提交表单
          commit(cursor);
        } else {
          close();                           // 没高亮：交给表单默认行为
        }
        return;
      }
      if (ev.key === 'Escape' || ev.key === 'Tab') {
        if (isOpen) close();
      }
    };
  }

  function attach(input) {
    if (!input || input.dataset.pickerBound === '1') return;
    var listId = input.getAttribute('list');
    if (!listId) return;

    // 摘掉 list：原生弹层不再出现，但 datalist 元素保留（候选仍从它读）
    input.setAttribute('data-picker-src', listId);
    input.removeAttribute('list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('autocomplete', 'off');
    input.dataset.pickerBound = '1';

    input.addEventListener('focus', onFocus(input));
    input.addEventListener('click', function () { open(input, ''); });
    input.addEventListener('input', onInput(input));
    input.addEventListener('keydown', onKeyDown(input));
    input.addEventListener('blur', function () {
      // 延迟收起：mousedown 在 panel 内已 preventDefault，正常不会走到这里；
      // 留这点延迟是为了容忍"先 blur 再 click"的老浏览器行为。
      setTimeout(function () { if (activeInput === input) close(); }, 120);
    });
  }

  function attachAll(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll ? scope.querySelectorAll('input[list]') : [];
    for (var i = 0; i < nodes.length; i++) attach(nodes[i]);
  }

  // 面板跟着输入框走：滚动/缩放时重新定位，而不是默默关掉
  function reposition() {
    if (panel && !panel.hidden && activeInput) place(activeInput);
  }

  document.addEventListener('mousedown', function (ev) {
    if (!activeInput) return;
    if (ev.target === activeInput || (panel && panel.contains(ev.target))) return;
    close();
  }, true);

  window.addEventListener('resize', reposition);
  window.addEventListener('scroll', reposition, true);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { attachAll(); });
  } else {
    attachAll();
  }

  global.Picker = {
    attach: attach,
    attachAll: attachAll,
    // 第二个参数是"先按这段文字过滤"：读取挂载点/扫描串口之后直接调它弹出来，
    // 用户不必再点一次输入框才知道候选已经拿到了。
    open: function (input, text) { return open(input || activeInput, text || ''); },
    close: close,
    isOpen: function () { return !!(panel && !panel.hidden); }
  };
})(window);
