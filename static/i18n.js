/* ============================================================================
 * 2RTK NTRIP Server - 前端国际化（纯前端实现，后端无需改动）
 *
 * 用法：
 *   - HTML 静态文案：给元素加 data-i18n="key" 属性，页面加载后自动替换。
 *     例如：<span data-i18n="nav.status">状态信息</span>
 *     属性值里的占位符 {x} 会按 data-i18n-args 或元素自带内容替换（见 applyI18n）。
 *   - JS 动态文案：调用 t('key') 获取当前语言的字符串。
 *   - 语言切换：调用 setLang('zh' | 'en')，选择会持久化到 localStorage。
 *
 * 语言优先级：localStorage 的 lang 键 > 浏览器语言 > 默认中文。
 * ========================================================================== */

(function (global) {
  'use strict';

  var messages = {
    zh: {
      // ---- 通用 / 导航 ----
      'nav.status': '状态信息',
      'nav.config': '运行设置',
      'nav.password': '修改密码',
      'nav.logout': '退出登录',
      'nav.login': '登录',
      'nav.donate': '捐赠作者',
      'brand.sub.index': '基准站状态',
      'brand.sub.config': '设备管理',
      'header.logged_in': '管理员已登录',
      'header.logged_out': '未登录',
      'header.loading': '数据加载中…',
      'footer.name': '2RTK NTRIP server',
      'lang.toggle': 'EN',

      // ---- 登录页 ----
      'login.title': '管理员登录',
      'login.username': '用户名',
      'login.password': '密码',
      'login.submit': '登录',
      'login.wrong': '用户名或密码错误，请重试。',
      'login.forgot.summary': '忘记密码？查看重置方法',
      'login.forgot.text': '出于安全考虑，网页不提供重置入口。请 SSH 登录设备后执行（路径按实际安装目录调整）：',
      'login.forgot.after': '重置后管理员恢复为 admin / admin，所有连接设置会被清空，请随后立即修改密码。',

      // ---- 修改密码页 ----
      'pwd.title': '修改管理员密码',
      'pwd.sub': '修改后请使用新密码重新登录',
      'pwd.old': '当前密码',
      'pwd.new': '新密码',
      'pwd.submit': '保存新密码',
      'pwd.cancel': '取消',

      // ---- 首页卡片 ----
      'card.run_status': '运行状态',
      'card.satellites': '卫星星座',
      'card.station_info': '基准站信息',
      'card.upload': '上传端 NTRIP Caster',
      'card.local_caster': '本地 NTRIP Caster',
      'card.radio': '无线电数传透传',
      'card.log': '系统日志',
      'card.map': '基准站位置',
      'card.hint.upload': '本机向上游基站服务器推送差分',
      'card.hint.local_caster': '内网移动站直连本机取差分',
      'card.hint.radio': '单向广播 · 附近流动站免 NTRIP 验证直接收差分',
      'card.hint.log': '实时推送 · 最多保留 500 条',

      // 运行状态字段
      'rs.mode': '运行模式',
      'rs.source': '数据源 Source',
      'rs.target': '目标 Caster',
      'rs.sent': '发送数据量',
      'rs.runtime': '运行时间',
      'rs.waiting': '等待数据…',

      // 卫星星座字段
      'sat.sys_count': '观测星座数量',
      'sat.systems': '观测到的星座',
      'sat.sat_count': '观测卫星数量',
      'sat.strong': '优良信号卫星',
      'sat.no_data': '暂无数据',

      // 基准站信息字段
      'st.rtcm': 'RTCM 数据',
      'st.rtcm_wait': '等待数据加载…',
      'st.device': '设备型号',
      'st.firmware': '固件版本',
      'st.antenna': '天线状态',

      // 上传端字段
      'up.status': '上传状态',
      'up.target': '上传地址',
      'up.mountpoint': '挂载点',
      'up.sent': '已发送',
      'up.rate': '实测速率',
      'up.reconnects': '重连次数',
      'up.session': '本次会话时长',
      'up.reading': '读取中…',

      // 本地 Caster 字段
      'lc.status': '运行状态',
      'lc.address': '接入地址',
      'lc.mount': '挂载点 / 账号',
      'lc.clients': '在线客户端',
      'lc.reading': '读取中…',

      // 数传透传字段
      'radio.status': '运行状态',
      'radio.port': '数传串口',
      'radio.baud': '波特率',
      'radio.sent': '已发送',
      'radio.rate': '实测速率',
      'radio.reconnects': '重连次数',
      'radio.reading': '读取中…',

      // 日志
      'log.loading': '系统日志加载中…',

      // 地图
      'map.title': '基准站位置地图',
      'map.loading': '正在加载地图数据…',
      'map.amap': '高德地图',
      'map.osm': 'OpenStreetMap',
      'map.station_id': '基准站编号',
      'map.ecef_x': 'ECEF-X',
      'map.ecef_y': 'ECEF-Y',
      'map.ecef_z': 'ECEF-Z',
      'map.lon': '经度',
      'map.lat': '纬度',
      'map.height': '海拔',
      'map.marker': '基准站',

      // 信号强度
      'sig.loading': 'RTCM 数据已解码，观测到的频段信号强度将在这里显示',
      'sig.receiving': '正在接收 RTCM 数据…',
      'sig.band': '信号类型',
      'sig.freq_band': '频段',
      'sig.freq': '频率',
      'sig.strength': '信号强度',
      'sig.system': '所属系统',
      'sig.prn': '卫星PRN',
      'sig.count': '观测频段',
      'sig.unknown': '未知',

      // ---- 动态状态文案（JS 用）----
      'mode.serial': '串口模式',
      'mode.relay': '中继模式',
      'mode.none': '未配置',
      'mode.auto': '自动检测',

      'up.unconfigured': '未启用上传出口',
      'up.unconfigured_hint': '未勾选「上传到云端 Caster」，本机不会向任何上游服务器发送数据。需要上传请在配置页勾选该出口。',
      'up.connected_hint': '已连接 {target}，差分数据持续推送中。',
      'up.connecting_hint': '正在向上游服务器发起连接…',
      'up.retrying_hint': '连接中断，正在重试（最近错误：{error}）',
      'up.retrying_hint_noerr': '连接中断，正在重试。',
      'up.error_hint': '认证失败，请检查密码与挂载点配置。',
      'up.frames': '帧',

      'lc.disabled': '未启用',
      'lc.disabled_hint': '需要在「运行设置」里勾选启用本地 Caster 并保存。',
      'lc.running': '运行中',
      'lc.running_hint': '移动站填地址 {addr} ，挂载点 {mp}，账号 / 密码均为 {user}。',
      'lc.none': '未运行：当前没有数据源',
      'lc.none_hint': '运行模式为 none，本机没有数据可发布。',
      'lc.enabled_not_run': '已启用但未运行',
      'lc.enabled_not_run_hint': '端口可能被占用，或保存后尚未重启。',

      'radio.disabled': '未启用',
      'radio.disabled_hint': '需要在「运行设置」里勾选启用数传透传并保存。',
      'radio.no_port': '未配置串口',
      'radio.no_port_hint': '已启用但未填写数传串口，请在运行设置里指定串口路径。',
      'radio.sending': '发送中',
      'radio.sending_hint': '正通过 {port} 单向广播 RTCM 差分，附近数传电台可直接接收。',
      'radio.reconnecting': '串口断线 · 重连中',
      'radio.reconnecting_hint': '数传串口断线，正在重连（最近错误：{error}）。',
      'radio.reconnecting_hint_noerr': '数传串口断线，正在重连。',
      'radio.not_run': '已启用但未运行',
      'radio.not_run_hint': '串口可能被占用，或保存后尚未重启。',

      // ---- 日志 / websocket ----
      'ws.connected': 'WebSocket连接成功，开始接收数据',
      'ws.received': '收到数据',
      'ws.error': '连接错误',
      'ws.closed': '连接关闭 (代码: {code}): {reason}',
      'ws.data_error': '数据处理错误',
      'ws.disconnected': '连接断开，{sec}秒后尝试重连 ({cur}/{max})',
      'ws.disconnected_retry': '连接断开，{sec} 秒后重连（{cur}/{max}）',
      'ws.cannot_reconnect': '无法重新连接，请刷新页面',
      'ws.map_timeout': '地图加载超时，尝试OpenStreetMap',
      'ws.map_amap_ok': '使用高德地图初始化成功',
      'ws.map_amap_fail': '高德地图初始化失败',
      'ws.map_osm_ok': '地图已加载（瓦片源：{provider}）',
      'ws.map_tile_fail': '瓦片源「{from}」加载失败，切换到「{to}」',
      'ws.map_all_fail': '所有 OpenStreetMap 瓦片源均不可用，请检查设备的外网访问',
      'ws.map_error': '地图初始化失败',
      'ws.map_unavailable': '无法加载地图服务',
      'ws.amap_fallback': '高德瓦片连续加载失败，尝试切换到 OpenStreetMap',
      'ws.last_update': '最后更新',

      // ---- 配置页 ----
      'cfg.title': '连接与上传设置',
      'cfg.reload': '读取当前配置',
      'cfg.mode': '运行模式 Mode',
      'cfg.mode.none': 'none —— 仅采集不转发（默认）',
      'cfg.mode.serial': 'serial —— 串口读取后上传',
      'cfg.mode.relay': 'relay —— 从中继 Caster 拉取后上传',
      'cfg.serial_port': '串口名称 Serial port',
      'cfg.serial_placeholder': '例如 /dev/ttyUSB0 或 COM3',
      'cfg.baud': '波特率 Baud rate',
      'cfg.caster_host': '上传地址 Caster address',
      'cfg.caster_port': '上传端口 Caster port',
      'cfg.caster_mount': '挂载点 Mount point',
      'cfg.caster_password': '上传密码 Password',
      'cfg.out_title': '数据出口（可多选，互相独立）',
      'cfg.out_hint': '勾选哪个出口就启动哪个，可多选、也可全不选（全不选时只做本地监控，不向任何外部发送）。三个出口互不影响：其中一个断线或出错，其他照常工作。',
      'cfg.out_upload': '① 上传到云端 NTRIP Caster',
      'cfg.out_upload_hint': '以 NTRIP SOURCE 协议推送到上游 Caster。独立线程运行，断线自动重连，不影响其他出口。',
      'cfg.relay_host': '中继地址 Relay address',
      'cfg.relay_port': '中继端口 Relay port',
      'cfg.relay_mount': '中继挂载点 Mount point',
      'cfg.relay_user': '中继用户名 Username',
      'cfg.relay_password': '中继密码 Password',
      'cfg.relay_placeholder': '点右侧按钮拉取，或直接手动输入',
      'cfg.fetch': '获取挂载点',
      'cfg.fetching': '获取中…',
      'cfg.fetch_hint': '从上面填写的中继地址与端口读取挂载点列表，也可以不拉取、直接手动输入。',
      'cfg.local_title': '② 本地 NTRIP Caster',
      'cfg.local_hint': '把本机正在转发的同一路 RTCM 数据，额外以 NTRIP Caster 的形式向内网发布。移动站/手簿直接连本机即可取差分，不必再绕到外网 Caster。原有上传链路不受影响。',
      'cfg.local_enable': '启用本地 Caster',
      'cfg.local_host': '监听地址 Listen address',
      'cfg.local_port': '监听端口 Listen port',
      'cfg.local_port_hint': '「监听端口」是本机真实占用的端口，客户端连的就是它。中继 / 上传端填的端口是「对端端口」，本机只是出站去连对方（用内核临时端口），所以它们可以都用 2101、彼此不冲突；真正会冲突的只有本机已有程序（含上一个没退干净的进程）占着同一个端口。',
      'cfg.port_conflict_upload': '本地 Caster 监听端口 {port} 与「① 上传到云端 NTRIP Caster」的端口相同。',
      'cfg.port_conflict_relay': '本地 Caster 监听端口 {port} 与「中继取流」的端口相同。',
      'cfg.port_conflict_fix': '本机监听端口要真实占用，请把本地 Caster 改成 {suggest} 这类未被占用的端口再保存。',
      'cfg.lc_mount': '挂载点',
      'cfg.lc_user': '用户名 / 密码',
      'cfg.lc_status': '运行状态',
      'cfg.radio_title': '③ 无线电数传透传（数传电台）',
      'cfg.radio_hint': '把本机正在转发的同一路 RTCM 数据，额外透传到一个 USB 数传电台（发送端）。附近无网络的流动站用配套数传电台即可直接接收差分，无需 NTRIP 账号密码。本功能与本地 Caster 完全独立，可同时开、也可单独开。',
      'cfg.radio_enable': '启用数传透传',
      'cfg.radio_port': '数传串口 Radio port',
      'cfg.radio_port_hint': '可点击「扫描串口」从下拉里选，也可以直接手动输入。已用作 GNSS 模块的串口会自动排除。',
      'cfg.radio_baud': '波特率 Baud rate',
      'cfg.radio_dir': '传输方向',
      'cfg.radio_dir_val': '单向广播（仅发送，不接收）',
      'cfg.radio_status': '运行状态',
      'cfg.radio_reading': '读取中…',
      'cfg.scan_ports': '扫描串口',
      'cfg.scanning': '扫描中…',
      'cfg.serial_port_hint': '可点击「扫描串口」从下拉里选，也可以直接手动输入。',
      'cfg.port_gnss_tag': '（已用作 GNSS 模块）',
      'cfg.port_count': '扫描到 {n} 个串口，点输入框可从下拉选择，也可手动输入。',
      'cfg.port_none': '未扫描到串口设备，可手动输入串口路径。',
      'cfg.port_fail': '扫描失败：{msg}',
      'cfg.radio_unconfigured': '未配置（勾选上方开关并保存后生效）',
      'cfg.radio_configured': '已配置串口 {port}（保存后生效）',
      'cfg.radio_example_hint': '单向广播：基准站只发送、流动站只接收，无需任何账号密码。',
      'cfg.save': '保存并重启',
      'cfg.save_hint': '保存后服务会自动重启以应用新配置',
      'cfg.reset_title': '需要彻底恢复默认配置？',
      'cfg.reset_text': '网页不提供该入口——重置接口无法可靠区分本机与远程请求，容易成为攻击面。请 SSH 登录设备后执行：',
      'cfg.reset_after': '该操作会把管理员恢复为 admin / admin 并清空全部连接设置。路径请按实际安装目录调整。',

      // 配置页动态文案
      'cfg.mount_need': '请先填写上方的中继地址和端口',
      'cfg.mount_connecting': '正在连接 {host}:{port} 读取挂载点…',
      'cfg.mount_ok': '已读取 {count} 个挂载点{cache}{capped}。点输入框可从列表挑选，也可以继续手动输入。',
      'cfg.mount_cached': '（5 分钟内缓存，不重复请求）',
      'cfg.mount_capped': '，列表过大只显示前 {n} 个，其余请手动输入',
      'cfg.mount_fail': '获取失败：{msg}',
      'cfg.load_fail': '配置读取失败：{msg}',
      'cfg.lc_disabled': '未启用（勾选上方开关并保存后生效）',
      'cfg.lc_running': '运行中 · {clients}/{max} 个客户端在线',
      'cfg.lc_example': '移动站填地址 {addr} ，挂载点 {mp}，账号 / 密码均为 {user}',
      'cfg.lc_none': '未运行：当前运行模式是 none，没有数据源',
      'cfg.lc_none_hint': '请先在上方选择串口或中继模式并保存，本机才有数据可发布。',
      'cfg.lc_not_run': '已启用但未运行（端口被占用，或保存后尚未重启）',
      'cfg.lc_fail': '状态读取失败：{msg}',

      // ---- 捐赠面板 ----
      'donate.title': '支持 2RTK Server',
      'donate.sub': '扫码即可赞助，将用于设备与服务器维护',
      'donate.alipay': '支付宝',
      'donate.wechat': '微信',
      'donate.thanks': '感谢每一位支持者 🧡',
      'donate.missing': '收款码未配置'
    },

    en: {
      // ---- Common / Nav ----
      'nav.status': 'Status',
      'nav.config': 'Settings',
      'nav.password': 'Password',
      'nav.logout': 'Logout',
      'nav.login': 'Login',
      'nav.donate': 'Donate',
      'brand.sub.index': 'Base Station',
      'brand.sub.config': 'Device',
      'header.logged_in': 'Admin logged in',
      'header.logged_out': 'Not logged in',
      'header.loading': 'Loading…',
      'footer.name': '2RTK NTRIP server',
      'lang.toggle': '中文',

      // ---- Login ----
      'login.title': 'Administrator Login',
      'login.username': 'Username',
      'login.password': 'Password',
      'login.submit': 'Login',
      'login.wrong': 'Incorrect username or password. Please try again.',
      'login.forgot.summary': 'Forgot password? Reset instructions',
      'login.forgot.text': 'For security reasons, there is no web reset. SSH into the device and run (adjust the path to your install directory):',
      'login.forgot.after': 'After reset, the admin becomes admin / admin and all connection settings are cleared. Change the password immediately afterwards.',

      // ---- Change password ----
      'pwd.title': 'Change Admin Password',
      'pwd.sub': 'You will need to log in again with the new password',
      'pwd.old': 'Current password',
      'pwd.new': 'New password',
      'pwd.submit': 'Save new password',
      'pwd.cancel': 'Cancel',

      // ---- Home cards ----
      'card.run_status': 'Run Status',
      'card.satellites': 'Satellites',
      'card.station_info': 'Base Station Info',
      'card.upload': 'Upstream NTRIP Caster',
      'card.local_caster': 'Local NTRIP Caster',
      'card.radio': 'Radio Serial Passthrough',
      'card.log': 'System Log',
      'card.map': 'Base Station Location',
      'card.hint.upload': 'Push corrections to an upstream caster',
      'card.hint.local_caster': 'LAN rovers connect directly for corrections',
      'card.hint.radio': 'One-way broadcast · nearby rovers get corrections without NTRIP auth',
      'card.hint.log': 'Live feed · keeps up to 500 entries',

      // Run status fields
      'rs.mode': 'Mode',
      'rs.source': 'Data Source',
      'rs.target': 'Target Caster',
      'rs.sent': 'Data Sent',
      'rs.runtime': 'Uptime',
      'rs.waiting': 'Waiting for data…',

      // Satellites fields
      'sat.sys_count': 'Systems Observed',
      'sat.systems': 'Systems',
      'sat.sat_count': 'Satellites Observed',
      'sat.strong': 'Strong Signal Sats',
      'sat.no_data': 'No data',

      // Base station info fields
      'st.rtcm': 'RTCM Data',
      'st.rtcm_wait': 'Waiting for data…',
      'st.device': 'Device Model',
      'st.firmware': 'Firmware Version',
      'st.antenna': 'Antenna Status',

      // Upload fields
      'up.status': 'Upload Status',
      'up.target': 'Upload Address',
      'up.mountpoint': 'Mount Point',
      'up.sent': 'Sent',
      'up.rate': 'Measured Rate',
      'up.reconnects': 'Reconnects',
      'up.session': 'Session Uptime',
      'up.reading': 'Reading…',

      // Local Caster fields
      'lc.status': 'Status',
      'lc.address': 'Address',
      'lc.mount': 'Mount / Account',
      'lc.clients': 'Clients Online',
      'lc.reading': 'Reading…',

      // Radio passthrough fields
      'radio.status': 'Status',
      'radio.port': 'Radio Port',
      'radio.baud': 'Baud Rate',
      'radio.sent': 'Sent',
      'radio.rate': 'Measured Rate',
      'radio.reconnects': 'Reconnects',
      'radio.reading': 'Reading…',

      // Log
      'log.loading': 'Loading system log…',

      // Map
      'map.title': 'Base Station Map',
      'map.loading': 'Loading map data…',
      'map.amap': 'AMap',
      'map.osm': 'OpenStreetMap',
      'map.station_id': 'Station ID',
      'map.ecef_x': 'ECEF-X',
      'map.ecef_y': 'ECEF-Y',
      'map.ecef_z': 'ECEF-Z',
      'map.lon': 'Longitude',
      'map.lat': 'Latitude',
      'map.height': 'Altitude',
      'map.marker': 'Base Station',

      // Signal strength
      'sig.loading': 'RTCM decoded — observed signal strengths will appear here',
      'sig.receiving': 'Receiving RTCM data…',
      'sig.band': 'Signal Type',
      'sig.freq_band': 'Band',
      'sig.freq': 'Frequency',
      'sig.strength': 'Signal Strength',
      'sig.system': 'System',
      'sig.prn': 'Satellite PRN',
      'sig.count': 'Bands',
      'sig.unknown': 'Unknown',

      // ---- Dynamic status (JS) ----
      'mode.serial': 'Serial Mode',
      'mode.relay': 'Relay Mode',
      'mode.none': 'Not Configured',
      'mode.auto': 'Auto-detect',

      'up.unconfigured': 'Upload output disabled',
      'up.unconfigured_hint': 'The "Upload to upstream Caster" output is not ticked, so nothing is sent upstream. Enable it on the settings page if you need uploading.',
      'up.connected_hint': 'Connected to {target}, streaming corrections…',
      'up.connecting_hint': 'Connecting to upstream server…',
      'up.retrying_hint': 'Connection lost, retrying (last error: {error})',
      'up.retrying_hint_noerr': 'Connection lost, retrying.',
      'up.error_hint': 'Authentication failed. Check the password and mount point.',
      'up.frames': 'frames',

      'lc.disabled': 'Disabled',
      'lc.disabled_hint': 'Enable the local caster in Settings and save.',
      'lc.running': 'Running',
      'lc.running_hint': 'Rover: address {addr}, mount point {mp}, user/password both {user}.',
      'lc.none': 'Not running: no data source',
      'lc.none_hint': 'Mode is none; this device has no data to publish.',
      'lc.enabled_not_run': 'Enabled but not running',
      'lc.enabled_not_run_hint': 'Port may be occupied, or not restarted after saving.',

      'radio.disabled': 'Disabled',
      'radio.disabled_hint': 'Enable radio passthrough in Settings and save.',
      'radio.no_port': 'No serial port configured',
      'radio.no_port_hint': 'Enabled but no radio port set. Specify the serial path in Settings.',
      'radio.sending': 'Transmitting',
      'radio.sending_hint': 'Broadcasting RTCM corrections on {port}; nearby radios can receive directly.',
      'radio.reconnecting': 'Port lost · reconnecting',
      'radio.reconnecting_hint': 'Radio serial port disconnected, reconnecting (last error: {error}).',
      'radio.reconnecting_hint_noerr': 'Radio serial port disconnected, reconnecting.',
      'radio.not_run': 'Enabled but not running',
      'radio.not_run_hint': 'Port may be occupied, or not restarted after saving.',

      // ---- Logs / websocket ----
      'ws.connected': 'WebSocket connected, receiving data',
      'ws.received': 'Received data',
      'ws.error': 'Connection error',
      'ws.closed': 'Connection closed (code {code}): {reason}',
      'ws.data_error': 'Data processing error',
      'ws.disconnected': 'Disconnected, retrying in {sec}s ({cur}/{max})',
      'ws.disconnected_retry': 'Disconnected, retry in {sec}s ({cur}/{max})',
      'ws.cannot_reconnect': 'Cannot reconnect, please refresh the page',
      'ws.map_timeout': 'Map load timeout, trying OpenStreetMap',
      'ws.map_amap_ok': 'AMap initialized',
      'ws.map_amap_fail': 'AMap initialization failed',
      'ws.map_osm_ok': 'Map loaded (tile source: {provider})',
      'ws.map_tile_fail': 'Tile source "{from}" failed, switching to "{to}"',
      'ws.map_all_fail': 'All OpenStreetMap tile sources unavailable. Check the device internet access.',
      'ws.map_error': 'Map initialization failed',
      'ws.map_unavailable': 'Unable to load map service',
      'ws.amap_fallback': 'AMap tiles failing, switching to OpenStreetMap',
      'ws.last_update': 'Last update',

      // ---- Config page ----
      'cfg.title': 'Connection & Upload Settings',
      'cfg.reload': 'Reload config',
      'cfg.mode': 'Mode',
      'cfg.mode.none': 'none —— collect only, no forward (default)',
      'cfg.mode.serial': 'serial —— read from serial then upload',
      'cfg.mode.relay': 'relay —— pull from a relay caster then upload',
      'cfg.serial_port': 'Serial Port',
      'cfg.serial_placeholder': 'e.g. /dev/ttyUSB0 or COM3',
      'cfg.baud': 'Baud Rate',
      'cfg.caster_host': 'Caster Address',
      'cfg.caster_port': 'Caster Port',
      'cfg.caster_mount': 'Mount Point',
      'cfg.caster_password': 'Password',
      'cfg.out_title': 'Data outputs (multi-select, independent)',
      'cfg.out_hint': 'Tick whichever outputs you need — any combination, or none (monitor only, nothing sent externally). The three outputs never affect each other: one failing or reconnecting leaves the others running.',
      'cfg.out_upload': '① Upload to upstream NTRIP Caster',
      'cfg.out_upload_hint': 'Pushes to the upstream caster using NTRIP SOURCE. Runs in its own thread with automatic reconnect; never blocks the other outputs.',
      'cfg.relay_host': 'Relay Address',
      'cfg.relay_port': 'Relay Port',
      'cfg.relay_mount': 'Mount Point',
      'cfg.relay_user': 'Username',
      'cfg.relay_password': 'Password',
      'cfg.relay_placeholder': 'Click the button to fetch, or type manually',
      'cfg.fetch': 'Fetch Mount Points',
      'cfg.fetching': 'Fetching…',
      'cfg.fetch_hint': 'Read the mount point list from the relay address/port above. You may also type manually.',
      'cfg.local_title': '② Local NTRIP Caster',
      'cfg.local_hint': 'Publish the same RTCM stream additionally as a local NTRIP Caster on the LAN. Rovers/handhelds connect directly to this device for corrections, without going through an external caster. The existing upstream link is unaffected.',
      'cfg.local_enable': 'Enable local caster',
      'cfg.local_host': 'Listen Address',
      'cfg.local_port': 'Listen Port',
      'cfg.local_port_hint': 'The listen port is really bound on this device — that is what clients connect to. The ports filled in for relay / upload are remote endpoints: this device only dials out to them using an ephemeral local port, so they may all be 2101 without conflict. A real conflict only happens when another program on this device (including a previous instance that has not exited) already holds that port.',
      'cfg.port_conflict_upload': 'Local caster listen port {port} is the same as the "① Upload to remote NTRIP Caster" port.',
      'cfg.port_conflict_relay': 'Local caster listen port {port} is the same as the "Relay source" port.',
      'cfg.port_conflict_fix': 'The listen port is really bound on this device — change the local caster to an unused port such as {suggest} and save again.',
      'cfg.lc_mount': 'Mount Point',
      'cfg.lc_user': 'Username / Password',
      'cfg.lc_status': 'Status',
      'cfg.radio_title': '③ Radio Serial Passthrough',
      'cfg.radio_hint': 'Passthrough the same RTCM stream additionally to a USB data radio (transmitter). Nearby rovers without network can receive corrections directly via a paired radio, with no NTRIP account. This is fully independent from the local caster — enable together or separately.',
      'cfg.radio_enable': 'Enable radio passthrough',
      'cfg.radio_port': 'Radio Port',
      'cfg.radio_port_hint': 'Click "Scan ports" to pick from the list, or type manually. The port already used for the GNSS module is excluded automatically.',
      'cfg.radio_baud': 'Baud Rate',
      'cfg.radio_dir': 'Direction',
      'cfg.radio_dir_val': 'One-way broadcast (send only, no receive)',
      'cfg.radio_status': 'Status',
      'cfg.radio_reading': 'Reading…',
      'cfg.scan_ports': 'Scan ports',
      'cfg.scanning': 'Scanning…',
      'cfg.serial_port_hint': 'Click "Scan ports" to pick from the list, or type manually.',
      'cfg.port_gnss_tag': ' (used by GNSS module)',
      'cfg.port_count': 'Found {n} serial port(s). Click the input to pick, or type manually.',
      'cfg.port_none': 'No serial ports found. You may type the port path manually.',
      'cfg.port_fail': 'Scan failed: {msg}',
      'cfg.radio_unconfigured': 'Not configured (enable above and save to take effect)',
      'cfg.radio_configured': 'Port {port} configured (takes effect after save)',
      'cfg.radio_example_hint': 'One-way broadcast: base only sends, rover only receives, no account needed.',
      'cfg.save': 'Save & Restart',
      'cfg.save_hint': 'The service restarts automatically after saving',
      'cfg.reset_title': 'Need a full factory reset?',
      'cfg.reset_text': 'No web entry is provided — a reset endpoint cannot reliably distinguish local vs proxied remote requests. SSH into the device and run:',
      'cfg.reset_after': 'This resets the admin to admin / admin and clears all connection settings. Adjust the path to your install directory.',

      // Config dynamic
      'cfg.mount_need': 'Fill in the relay address and port above first',
      'cfg.mount_connecting': 'Connecting to {host}:{port} to read mount points…',
      'cfg.mount_ok': 'Loaded {count} mount points{cache}{capped}. Click the input to pick, or keep typing manually.',
      'cfg.mount_cached': ' (cached for 5 min)',
      'cfg.mount_capped': ', list too large, showing first {n} — type the rest manually',
      'cfg.mount_fail': 'Fetch failed: {msg}',
      'cfg.load_fail': 'Failed to load config: {msg}',
      'cfg.lc_disabled': 'Disabled (tick the switch above and save)',
      'cfg.lc_running': 'Running · {clients}/{max} clients online',
      'cfg.lc_example': 'Rover: address {addr}, mount point {mp}, user/password both {user}',
      'cfg.lc_none': 'Not running: mode is none, no data source',
      'cfg.lc_none_hint': 'Select serial or relay mode above and save first.',
      'cfg.lc_not_run': 'Enabled but not running (port occupied, or not restarted after save)',
      'cfg.lc_fail': 'Status read failed: {msg}',

      // ---- Donate ----
      'donate.title': 'Support 2RTK Server',
      'donate.sub': 'Scan to donate — for device & server upkeep',
      'donate.alipay': 'Alipay',
      'donate.wechat': 'WeChat',
      'donate.thanks': 'Thanks to every supporter 🧡',
      'donate.missing': 'QR code not configured'
    }
  };

  var currentLang = 'zh';

  // 读取持久化的语言选择，其次看浏览器语言
  function detectLang() {
    try {
      var saved = localStorage.getItem('2rtk_lang');
      if (saved === 'zh' || saved === 'en') return saved;
    } catch (e) { /* localStorage 不可用时忽略 */ }
    try {
      var nav = (navigator.language || '').toLowerCase();
      if (nav.indexOf('zh') === 0) return 'zh';
      if (nav.indexOf('en') === 0) return 'en';
    } catch (e) { /* ignore */ }
    return 'zh';
  }

  // 简单占位符替换：{name} -> value
  function fmt(tpl, args) {
    return String(tpl).replace(/\{(\w+)\}/g, function (_, k) {
      return (args && args[k] !== undefined) ? args[k] : '{' + k + '}';
    });
  }

  // 翻译函数（供 script.js 等调用）
  function t(key, args) {
    var dict = messages[currentLang] || messages.zh;
    var v = dict[key];
    if (v === undefined) v = (messages.zh[key] !== undefined ? messages.zh[key] : key);
    return args ? fmt(v, args) : v;
  }

  // 把当前语言应用到所有带 data-i18n 的元素
  function applyI18n() {
    var els = document.querySelectorAll('[data-i18n]');
    Array.prototype.forEach.call(els, function (el) {
      var key = el.getAttribute('data-i18n');
      var v = t(key);
      // 支持 data-i18n-args 传参，例如 data-i18n-args='{"name":"x"}'
      var argsAttr = el.getAttribute('data-i18n-args');
      if (argsAttr) {
        try { v = fmt(v, JSON.parse(argsAttr)); } catch (e) { /* ignore */ }
      }
      if (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) {
        el.value = v;
      } else {
        el.textContent = v;
      }
    });

    // 切换按钮文字显示成"目标语言"
    var btn = document.getElementById('lang-toggle');
    if (btn) btn.textContent = t('lang.toggle');

    // 更新 <html lang>
    document.documentElement.setAttribute('lang', currentLang === 'zh' ? 'zh-CN' : 'en');
  }

  function setLang(lang) {
    if (lang !== 'zh' && lang !== 'en') return;
    currentLang = lang;
    try { localStorage.setItem('2rtk_lang', lang); } catch (e) { /* ignore */ }
    applyI18n();
    // 通知页面重新应用动态文案（页面各自监听）
    if (typeof document.dispatchEvent === 'function') {
      document.dispatchEvent(new CustomEvent('langchange', { detail: { lang: lang } }));
    }
  }

  function toggleLang() {
    setLang(currentLang === 'zh' ? 'en' : 'zh');
  }

  function init() {
    currentLang = detectLang();
    applyI18n();

    var btn = document.getElementById('lang-toggle');
    if (btn) btn.addEventListener('click', toggleLang);
  }

  // 暴露接口
  global.I18N = {
    t: t,
    setLang: setLang,
    toggleLang: toggleLang,
    getLang: function () { return currentLang; },
    init: init
  };

  // DOM 就绪时初始化（也允许页面在末尾手动调用 I18N.init()）
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})(window);
