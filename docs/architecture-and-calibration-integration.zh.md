# 2RTKserver 项目架构与对接点分析报告

> 版本基线：`VERSION = 2.2.3`，提交 `50e576b`
> 阅读范围：`server.py` + `rtkserver/` 全部 38 个 `.py`（8 个包初始化、29 个实体模块）+ 4 个模板 + 4 个前端资源（script/maintenance/i18n/theme）+ `tests/test_contract.py`
> 规模：Python 6530 行 / 前端 5027 行 / 测试 765 行
> 本文只做结构分析与集成设计，不含业务实现代码。

---

## 1. 分层结构

### 1.1 目录与职责

```
server.py                 入口壳（60 行）：插 sys.path → 调 lifecycle.main()
                          + 兼容导出（reset_config.py 等历史引用）
rtkserver/
├─ paths.py               落盘路径唯一来源（BASE_DIR = 包目录上一层）
├─ version.py             版本唯一来源（读根目录 VERSION，semver 校验，缺失回落 0.0.0）
├─ security.py            PBKDF2 口令、会话密钥、login_required、登录限速
├─ validation.py          配置有效性（"能不能干活"），日志 60 秒节流
├─ core/
│   ├─ constants.py       全部不可变常量（按功能域分组），唯一导入 version
│   ├─ state.py           全局可变状态池 + 三张注册表（硬规则：只允许 import 本模块）
│   ├─ config.py          SQLite 单表 KV 读写、默认值补齐、自愈、口令迁移
│   ├─ distributor.py     一路数据 → 所有出口（非阻塞、不抛错、固定顺序、不加锁）
│   ├─ lifecycle.py       main() / run_once() / 信号处理 / mDNS 挂载点
│   └─ status.py          出口快照聚合 → run_status → WS 推送
├─ sources/
│   ├─ serial_source.py   数据源①：串口自动识别 + 读循环
│   ├─ relay_source.py    数据源②：NTRIP 中继拉流
│   ├─ ntrip.py           NTRIP 协议（建连/SOURCE 握手/客户端握手/SOURCETABLE）
│   └─ rtcm_parser.py     RTCM 解帧、CRC24Q、pyrtcm 解析、MSM 降频广播
├─ outlets/
│   ├─ __init__.py        出口注册表（顺序即分发顺序）
│   ├─ local_caster.py    出口②：本地 NTRIP Caster（accept + 每客户端线程）
│   ├─ caster_uploader.py 出口①：上传到云端 Caster（独立线程 + 队列）
│   └─ radio_bridge.py    出口③：无线电数传透传（单向写串口）
├─ discovery/
│   └─ zeroconf_announcer.py  mDNS 广播（自包含，仅标准库 + zeroconf）
├─ upgrade/               网页一键升级/卸载（runner 为独立进程执行器）
├─ web/
│   ├─ app.py             Flask 工厂（模板/静态绝对路径、安全头、Sock）
│   ├─ routes.py          页面路由（/、/config、/login、/logout、/change_password）
│   ├─ api.py             JSON 接口（load_config、local_caster、serial_ports、sourcetable）
│   ├─ api_upgrade.py     升级/卸载接口（status/check/start/uninstall）
│   └─ ws.py              WebSocket 推送（单连接单队列单发送者）
└─ utils/
    ├─ common.py          retry、可中断 sleep、格式化、ECEF→经纬高、清屏
    ├─ network.py         本机 IP、虚拟网卡剔除、端口占用诊断、自环检测
    └─ logger.py          全项目唯一日志初始化 + WS 日志/print 捕获
templates/  static/       4 页面 + script.js(1386) / maintenance.js(413) / i18n.js(778) / theme.css(1295)
tests/test_contract.py    15 个 TestCase / 68 条契约测试
```

### 1.2 依赖方向（分层铁律）

| 规则 | 体现 | 破坏后果 |
| --- | --- | --- |
| 底层不 import 上层 | `distribute()` 不知道有 Web；日志/解析只调 `state.broadcast()` | core→web 成环，拆包时必然 ImportError |
| 可变单例只在 `state` 存一份，且必须 `from ..core import state` | 模块注释明确禁止 `from ..core.state import local_caster` | `from X import y` 在导入瞬间绑死 None，赋值后永远读不到 |
| `constants` 只 import `version` | 版本号四处硬编码曾被这个例外收掉 | 版本号再次散落，网页与控制台对不上 |
| `paths` 是落盘路径唯一来源 | 拆包时 `dirname` 少一层会让库/日志/密钥全部"消失" | 用户表现为"配置全丢了" |
| 出口通过注册表接入 | `outlets.register_all()` 填 `state.outlets` | 新增出口要改分发器，违反开闭 |

### 1.3 启动序列（main）

```
setup_logging()（force=True，装 3 个 handler）
→ print(ART_LOGO)
→ install_signal_handlers()（SIGTERM/SIGINT → shutdown_event）
→ outlets.register_all()
→ _register_discovery_hooks()（挂 startup/shutdown 钩子）
→ init_db() + migrate_admin_password()
→ 起 rtcm-parser 线程
→ _start_web_thread()（Flask 内置服务器，threaded=True）
→ log_startup_banner()
→ _run_startup_hooks()（mDNS，Web 起来之后才挂）
→ while not shutdown_event：get_config → validate_config
     失败：mode=none 时补 run_status 并 interruptible_sleep(10)
     成功：run_once()（阻塞到重启/退出）
→ _run_shutdown_hooks()（mDNS 发 TTL=0 道别包）
```

关键设计：**重启只重启数据面**（`restart_event`），Web、日志、mDNS 均不重建；`run_once()` 在 `while not restart and not shutdown: sleep(1)` 上等待。

---

## 2. 线程模型

### 2.1 线程清单

| 线程名 | 创建点 | 数量 | 职责 | 退出方式 |
| --- | --- | --- | --- | --- |
| MainThread | `lifecycle.main` | 1 | 配置循环 + `run_once` 生命周期 | `shutdown_event` |
| `web` | `_start_web_thread` | 1（daemon） | Flask 内置服务器，`threaded=True` | 进程退出 |
| 请求线程 | werkzeug | 每请求一个 | 页面/接口 | 请求结束 |
| WS 连接线程 | flask-sock 处理器 | ≤ `MAX_WS_CLIENTS`(32) | flush 队列 + `receive` 轮询 | `client.alive=False` |
| `rtcm-parser` | `main` | 1 | 从 `state.rtcm_buffer` 解帧/解析/广播 | 进程退出 |
| `source-serial` | `run_once` | 1 | 读串口 → `distribute()` | `thread_exit_event` |
| `source-relay` | `run_once` | 1 | 收 TCP → `distribute()` | `thread_exit_event` |
| `caster-upload` | `CasterUploader.start` | 1 | 建连/握手/推流/重连 | `_running` + `_sleep` 可打断 |
| `radio-bridge` | `SerialBridge.start` | 1 | 写数传串口/重连 | `_running` |
| `local-caster` | `LocalCaster.start` | 1 | accept 循环（1s 超时） | `_running` + shutdown() |
| `caster-<ip>` | `_accept_loop` | ≤ `LOCAL_CASTER_MAX_CLIENTS`(16) | 服务单个 NTRIP 客户端 | `client.alive` / socket 错 |
| `mdns-watch` | `ZeroconfAnnouncer._start_watchdog` | 1 | 30s 巡检 IP 变化 / 重试注册 | `_watch_stop` |

稳态上限：约 `1 + 1 + 32 + 1 + 1 + 1 + 1 + 1 + 16 + 1 ≈ 56` 个线程（ARM 板 1GB 内存可接受）。**新增后台功能必须沿用"独立线程 + 有界队列"，不得在 HTTP 线程里做长任务，也不得在热路径里等待。**

### 2.2 三个事件对象（`core/state.py`）

| 事件 | 语义 | 谁置位 | 谁轮询 |
| --- | --- | --- | --- |
| `thread_exit_event` | 本轮运行退出（数据源/出口） | `run_once` 的 finally、重启 | 串口/中继/retry/带外扫描 |
| `shutdown_event` | 进程退出 | 信号处理 | `main`、`interruptible_sleep` |
| `restart_event` | 只重启数据面 | 配置页保存/`/restart_program` | `run_once` 等待循环 |

`ThreadExitRequested` 用于"从阻塞深处一次性退栈"，`interruptible_sleep` 每 0.2 秒检查一次——这两者是全项目"点保存立刻生效"的基础。

### 2.3 队列与背压（全部"满了丢最旧/踢客户端"，绝不阻塞生产者）

| 队列 | 容量 | 满时策略 | 好处 |
| --- | --- | --- | --- |
| `CasterUploader._outbox` | `CASTER_QUEUE_SIZE`(512) | 丢最旧再入队 | 上游慢不拖累数据源 |
| `SerialBridge._outbox` | `RADIO_QUEUE_SIZE`(512) | 丢最旧再入队 | 数传串口慢不影响主链路 |
| `_CasterClient.outbox` | `LOCAL_CASTER_QUEUE_SIZE`(256) | 断开该客户端 | 慢客户端会拖累所有客户端 |
| `rtcm_buffer` | `MAX_BUFFER_SIZE`(10MB) | 丢最旧字节 | 解析慢时不丢最新差分 |
| `WSClient.outbox` | `WS_OUTBOX_MAXLEN`(32) | 连续丢 100 帧则断开 | 监控不拖累解析线程 |

---

## 3. 数据流与转发核心

### 3.1 一条数据的旅程

```
[串口 GNSS]  ser.read()  ─┐
                          ├→ distribute(data) ─┬→ local_caster.publish  → 每客户端队列 → caster-<ip> 线程
[中继 Caster] recv()    ─┘                    ├→ radio_bridge.publish  → 数传队列 → radio-bridge 线程
                                              ├→ caster_uploader.feed → 上传队列 → caster-upload 线程
                                              └→ rtcm_parser.upload_data → rtcm_buffer
                                                                              ↓ rtcm-parser 线程
                                                          解帧 → pyrtcm → 1005/1006/1033/MSM
                                                                              ↓
                                                         state.broadcast(dict) → WS 各连接队列
```

- 数据源**只调 `distribute()`**，不持有任何出口连接；
- `distribute()` 四条硬约束：永不阻塞、永不抛错、顺序固定（本地优先）、不加锁；
- 出口注册顺序即分发顺序：`local_caster → radio → caster → parser`。

### 3.2 数据源与出口的独立开关

`mode`（serial/relay）只决定"数据从哪来"；三个出口各有独立开关（`caster_enabled` / `local_caster_enabled` / `radio_enabled`），与 mode 完全解耦，可同时开、也可全关（全关时数据只用于本地监控显示）。

---

## 4. 数据源接口与 RTCM 解析

### 4.1 串口数据源（`sources/serial_source.py`）

| 接口 | 语义 | 说明 |
| --- | --- | --- |
| `detect_serial(cfg)` | 优先用配置，失败则全量扫描 | 扫描 `SERIAL_SCAN_BAUDS`(11 档) × 端口；命中后 `save_serial_config()` 回写库 |
| `test_serial(port, baud, check_rtcm)` | 2 秒窗口内是否收到有效 RTCM | 内部用 `extract_rtcm_frames` 真解帧，不是"看到 0xD3 就算" |
| `upload_via_serial(ser, cfg, event)` | 读循环（唯一的串口持有者） | `ser.read(in_waiting or 1024)` → `distribute()`；异常则关串口、3 秒后重开 |

启停语义：

- **启动**：`run_once` 里 `detect_serial` → `serial.Serial(port, baud, timeout=1)` → 起线程、记 `state.worker_thread`；
- **停止**：置 `thread_exit_event` → `join(5)`；未退出则保留 `worker_thread`，下一轮 `run_once` 被守卫拦住（防同一挂载点双推流）；
- **重启**：`restart_event` → `run_once` finally 逆序停出口与数据源 → 回到配置循环。

> **关键事实（对后续任何"要写串口"的功能都成立）**：`upload_via_serial` 是**唯一**持有 `ser` 句柄的线程，读写都必须在这一条线程里串行完成；另开进程/线程 open 同一个 `/dev/ttyUSB*` 会把 RTCM 流撕成两半（Linux 上不默认独占，两个 reader 会各拿一部分字节），表现为"两边都在收、两边都不完整"。

### 4.2 中继数据源（`sources/relay_source.py`）

`open_relay()`（建连 + NTRIP 客户端握手）→ `forward_relay()` 循环：`recv` 超时视为"本轮无数据"（`RELAY_READ_TIMEOUT=20`），空字节视为对端关闭立刻重连，连续 `RELAY_IDLE_RECONNECT=120` 秒无数据才重连。

### 4.3 RTCM 解析（`sources/rtcm_parser.py`）

| 环节 | 实现 | 要点 |
| --- | --- | --- |
| 解帧 | `extract_rtcm_frames(data) → (frames, remaining)` | 找 `0xD3`，长度取 `((b1 & 0x03) << 8) \| b2`，**CRC24Q 不通过就 index+1 重新同步**（否则载荷里的 0xD3 会吞掉后续合法帧） |
| 校验 | `crc24q()` 查表实现 | 纯 Python 查表，ARM 上可接受（只对候选帧算一次） |
| 缓冲 | `upload_data()` 追加 + 溢出丢最旧 | `MAX_BUFFER_SIZE=10MB` |
| 解析线程 | `process_rtcm_buffer()` | 每 0.1s 取一次缓冲；逐帧 `pyrtcm.RTCMReader`；`DF002` 取消息号 |
| 站点信息 | `process_station_message()`（1005/1006） | ECEF → `ecef_to_latlon()` 迭代解算 → 写 `state.station_info` |
| 天线/设备 | `process_1033_message()` | 天线名、接收机型号、固件、序列号 |
| MSM | `parse_msm()` + `_broadcast_msm()` | 每星座每秒最多 1 次；只保留 `CELLPRN/CELLSIG/DF403/DF405/DF408`（源码注释：仅裁剪字段一项约省 40% 流量） |
| 类型跟踪 | `frequency_tracker` + `state.msg_types_seen` + `msg_types_recent` | `msg_types_recent` 是"最近 60 秒真的在推"的集合，被 SOURCETABLE 直接使用 |

**限制（重要）**：`extract_rtcm_frames` 会**丢弃所有非 RTCM 字节**（UBX 二进制、NMEA 语句在解帧时就被跳过了）。也就是说，接收机的 UBX-ACK / NMEA-GGA / NAV-SVIN 这类"回包"当前在数据面上是**不可见的**。

---

## 5. 数据库机制

| 维度 | 现状 |
| --- | --- |
| 结构 | 单表 `settings(key TEXT PRIMARY KEY, value TEXT)`，KV 模型，新增配置项零迁移 |
| 模式 | `PRAGMA journal_mode=WAL`（读写不互斥）；每次连接 `timeout=10` |
| 初始化 | `init_db()` 只在 `main()` 调一次；老库用"只补缺失键"的方式追加新项，绝不动已有值 |
| 读 | `get_config()` 每次新建连接，`_normalize_int_fields()` 把 5 个端口/波特率字段归一为 int；失败（库 0 字节/表丢失）退回 `DEFAULT_CONFIG`（只详细记一次日志） |
| 写 | `save_config()` → 失败则 `init_db()` 重建表后重试一次；只 UPDATE 有变化的键 |
| 口令 | `admin_password` 存 `pbkdf2_sha256$100000$salt$hash`；`migrate_admin_password()` 启动时把历史明文升级为哈希 |
| 日志脱敏 | `mask_config()` 按 `SENSITIVE_KEY_HINTS`（password/passwd/secret）打码 |
| 串口配置 | `save_serial_config(port, baud)` 用裸 `INSERT OR REPLACE`（自动识别命中时回写） |

**性能注意**：`get_config()` 无缓存。`push_run_status()` 每 2 秒被数据源调用，其中 `local_caster_snapshot()` 与 `serial_bridge_snapshot()` 各自再取一次配置 —— 稳态约 **2~3 次 SQLite 连接/2 秒**。新增功能**不要**在这条链路上再加 `get_config()`（标定模块应持有自己的一份配置快照，或通过参数传入 cfg）。

---

## 6. WebSocket 推送机制

### 6.1 结构：一连接 = 一队列 = 一发送者

```
解析线程/日志 Handler/状态推送
        ↓ state.broadcast(dict)
send_to_clients()  ← 只做 json.dumps + 入队，不碰 socket
        ↓ 每个连接 deque(maxlen=32)
websocket() 处理器线程：flush() 发空 → ws.receive(timeout=0.2) → 再 flush
```

为什么要这样：`simple_websocket.send` 是同步阻塞且无超时，且内部有读线程共用 wsproto 状态机 —— 直接 send 会让"不读数据的客户端"挂住解析线程，或产生帧交错。

### 6.2 消息协议（前端按**字段存在性**分发，不按 `type`）

| 字段 | 生产方 | 前端动作 |
| --- | --- | --- |
| `station_info` | `process_station_message` | 站点号/ECEF/经纬高 + 地图标记与 20/50km 圈 |
| `msg_types` / `msg_type_frequency` | 解析线程 | RTCM 类型列表 |
| `cell_data` + `gnss` | `_broadcast_msm` | 星座卡片与信噪比条（唯一的"大包"，已降频+裁剪） |
| `antenna_info` / `device_info` | 1033 处理 | 天线型号、接收机型号/固件 |
| `run_status` | `status.push_run_status` | 首页状态卡（mode/数据源/出口/IP/固定地址） |
| `local_caster` / `radio` | 出口快照 | 两张出口卡片 |
| `type: log/print` | 日志 Handler / print 捕获 | 日志面板 |
| `type: history` | WS 首帧 | 回放最近 100 条日志 + 100 条 print |

**约束**：`type` 只用于 `log/print/error/history/local_caster/radio/run_status` 这些"整帧语义"，业务数据靠字段名。新增功能必须用一个**不会与现有键冲突的顶层字段名**（建议 `calibration`），否则会被现有分支误处理。

### 6.3 安全与限流

- 状态页**公开可看**，但未登录客户端收到的是 `_redact_for_public()` 处理过的副本：`station_info` 只留 `station_id` + `masked`，`antenna_info/device_info` 去掉序列号；
- `PUBLIC_STATION_COORDS=1` 环境变量可恢复完全公开；
- 连接数上限 32，超了直接回 `{"type":"error"}` 并关闭；
- 首帧额外补 `local_caster`/`radio`/`run_status`：因为 `mode=none` 时没有任何循环在推状态，否则首页一片空白。

---

## 7. 现有代码规范

### 7.1 命名

| 对象 | 约定 | 例子 |
| --- | --- | --- |
| 包/模块 | 小写下划线，按"角色"分目录 | `sources/`、`outlets/`、`web/` |
| 类 | PascalCase，语义化名词 | `CasterUploader`、`SerialBridge`、`ZeroconfAnnouncer`、`_CasterClient`(私有) |
| 函数 | 动词开头 | `detect_*` / `probe_*` / `fetch_*` / `parse_*` / `extract_*` / `describe_*` / `push_*` / `mask_*` / `start_*` / `stop_*` / `*_snapshot` |
| 数据入口命名 | 出口用 `publish`/`feed`；注册表用 `register_*` | `local_caster.publish`、`caster_uploader.feed`、`register_all` |
| 常量 | 全大写 + 功能域前缀，集中在 `core/constants.py`，用分隔线分组 | `LOCAL_CASTER_PORT`、`RADIO_QUEUE_SIZE` |
| 单例 | 一律放 `state`，用 `state.xxx` 访问 | `state.local_caster`、`state.serial_bridge` |
| HTML id | kebab-case | `maint-card`、`serial-port-hint` |
| i18n key | `域.子项` | `rs.mdns.hint`、`cfg.port_conflict_fix`、`maint.failed` |
| CSS | 组件类 + 状态修饰类 | `.card`、`.kv`、`.maint-*`、`.is-ok/.is-bad/.is-warn` |

### 7.2 日志

- **唯一初始化**：`utils/logger.setup_logging()`，`force=True`，三个 handler（控制台 / `RotatingFileHandler` 1MB×3 / WS Handler）。其它模块**禁止** `basicConfig` 或添加 handler。
- **用法不统一**：多数模块直接用 root `logging.info(...)`；只有 `utils/network.py`、`discovery/zeroconf_announcer.py` 用 `logging.getLogger(__name__)`。新模块建议沿用 root 风格以保持日志格式一致。
- **级别语义**：
  - `INFO` 生命周期与状态跃迁（"本地 Caster 已启动"、"上传出口认证通过"）
  - `WARNING` 可自愈降级（端口占用告警、mDNS 不可用、客户端过慢被踢、配置未完成）
  - `ERROR` 需人工干预（配置无效、认证失败、串口读失败、写库失败）
  - `DEBUG` 高频细节（CRC 校验失败、单档波特率探测失败）
- **节流三件套**（新增高频日志必须照抄其一）：
  - `_log_validate_problem()`：同一问题 60 秒一次（配置每 10 秒校验一次）
  - `RADIO_FAIL_LOG_INTERVAL=600`：首次 ERROR、之后抑制、恢复时 INFO 汇报"此前连续失败 N 次"
  - `_db_error_logged`：读库失败只详细打一次
- **文案风格**：中文 + 带处置建议。典型样本：本地 Caster 启动失败那段，把"中继/上传填 2101 是对端端口"讲清楚，避免误判。
- **脱敏**：口令类字段打码（`mask_config`），状态快照里不含 `caster_password`/`relay_password`。

### 7.3 错误处理

| 风格 | 用法 | 典型位置 |
| --- | --- | --- |
| 旁路不抛 | 吞掉异常只记日志，主链路不受影响 | `state.broadcast`、`_run_startup_hooks`、`distribute`、各 `*_snapshot` |
| 可中断重试 | `@retry(max_retries, delay)` + `ThreadExitRequested` + `interruptible_sleep` | `connect_to_host`、`open_caster`、`test_serial` |
| 自愈优先 | 失败先修环境再重试，而不是直接失败 | `save_config` 重建表、`LocalCaster.start` 3 次退避、mDNS 30s 重试、`CasterUploader` 无限重连 |
| 状态机兜底 | 外部进程/单元可能没起来时，用 pid/超时判定"卡死" | `upgrade.service.read_state` |
| 用户可读 | errno/英文异常翻译成中文 + 处置建议 | 端口占用、配置校验、串口打不开 |

已知反模式（可接受但值得注意）：`except Exception: pass` 出现较多（socket 关闭、WS 发送、钩子调用），会削弱可观测性；建议新代码至少 `logging.debug`。

### 7.4 前后端契约

- 路由 **endpoint 名固定**（`index`/`login`/`config_page`/…），有测试锁死，不允许随意改名；
- i18n 必须中英成对（`static/i18n.js` 两份对象），有测试校验 key 数量；
- 前端不硬编码业务文案，一律 `data-i18n` + `I18N.t()`；
- 页面脚本分工：`script.js`（状态页，含 WS）、`maintenance.js`（配置页维护卡片，轮询）、`config.html` 内联（表单显隐/端口冲突/串口扫描）。

### 7.5 测试范式（`tests/test_contract.py`）

契约测试，不依赖硬件与外网：路由表、默认配置、校验规则、解帧、出口注册顺序、mDNS 快照、版本单一来源、配置库自愈，以及**源码文本断言**（如"runner 不得 import 项目内模块"、"install.sh 必须带 VERSION"）。新增功能应至少补：状态机终态、路由鉴权、以及"不改热路径语义"的回归断言。

---

## 8. 「基准站一键标定」集成方案

### 8.1 功能边界（先定义清楚，才能谈侵入点）

一次"一键标定"实际要做四件事：

1. **探明接收机**：型号/协议族（u-blox UBX、Unicore、Septentrio、或只吐 RTCM 的"哑"设备），决定能否写入；
2. **取得参考坐标**：两种模式 —— ①接收机自主 `survey-in`（等收敛，几十秒到几分钟）；②固定已知坐标（用户输入）；
3. **写入接收机**：base 模式 + 参考坐标（ECEF/经纬高）+ 天线高 + 1005/1006 等电文输出；
4. **验证**：从 RTCM 流里重新解析出 1005，坐标与本机记录一致 → 成功；失败可回退到写入前的配置。

### 8.2 硬约束（决定了方案取舍）

| 约束 | 事实 | 影响 |
| --- | --- | --- |
| 串口独占 | `upload_via_serial` 是唯一持 `ser` 的线程，读写须在同一线程串行 | 标定**不能**自己 open 串口 |
| 解析器丢弃非 RTCM | `extract_rtcm_frames` 跳过 UBX/NMEA | 现状看不到接收机 ACK / survey-in 进度 |
| 数据面不能停 | 停源 = 本地 Caster 全部客户端断流 + 上传出口重连 | 长时间断流不可接受（除非用户显式选择"离线标定"） |
| 配置库不是结果容器 | `settings` 是"配置"语义，标定结果是运行数据 | 不写 settings，避免 `init_db` 补齐逻辑被滥用 |
| WS 公开广播会脱敏 | 未登录客户端看不到精确坐标 | 标定接口必须 `login_required`，结果不进公开广播 |

### 8.3 候选方案对比

| 方案 | 做法 | 侵入度 | 断流 | 结论 |
| --- | --- | --- | --- | --- |
| A. 独立进程直接开串口 | 标定进程自己 `open(/dev/ttyUSB0)` | 零代码侵入 | 无 | ✗ 否决：两个 reader 撕裂 RTCM 流，两边都不完整 |
| B. 停数据面独占串口 | 置 `restart_event`，标定进程独占 | 中（要改 lifecycle 协调） | 30~180 秒 | △ 仅作为"离线标定"降级路径 |
| **C. 旁路通道（推荐）** | 在数据源线程内加"只读旁路 tap + 只写命令队列" | 极小（热路径 ~15 行） | 无 | ✓ 推荐 |

### 8.4 推荐架构：两个缝 + 一个旁路包

```
                    ┌──────────────── 新增 calibration/（旁路包，不 import web） ───────────────┐
                    │ service.py  状态机线程：idle→probing→surveying→computing→applying→verifying │
                    │ commands.py 厂商命令适配（u-blox UBX / Unicore / NMEA），纯字节编码        │
                    │ survey.py   Welford 在线均值/方差、精度门槛、样本数门槛                    │
                    └───────▲──────────────────────────────┬──────────────────────────────────┘
            只读旁路 tap     │                              │  只写命令队列（有界，丢最旧）
   （UBX/NMEA/RTCM 原始字节）│                              ▼
   serial_source 读循环：  data = ser.read()  →  tap(data)  →  drain(cmd_queue) → ser.write()
                                          └→ distribute(data)（下游一行不改）
```

- **tap（只读）**：`serial_source` 在 `distribute(data)` 之前把原始字节交给一个可选的观察者（`state.raw_tap`，默认 `None`）。标定模块据此解析 UBX-ACK / NAV-SVIN / NMEA-GGA，用于进度与质量判断；不注册时零开销。
- **cmd queue（只写）**：`state.receiver_cmd_queue`（`queue.Queue(maxsize=8)`）。`serial_source` 每轮循环顶部 `get_nowait()` 取一条命令写入串口；队列有界 + 超时写，保证写不拖慢读取。
- 两者都在**同一线程**内完成，天然串行，不破坏"单持有者"约束。

### 8.5 最小侵入清单（预估）

| 文件 | 改动 | 预估行数 | 性质 |
| --- | --- | --- | --- |
| `rtkserver/core/state.py` | 新增 `raw_tap = None`、`receiver_cmd_queue = queue.Queue(8)` 两个槽位 + 注释 | ~8 | 加槽位，不改语义 |
| `rtkserver/sources/serial_source.py` | 循环顶部 drain 命令；`distribute` 前调用 tap（都用 `if ... is not None` 包住，异常只记 debug） | ~15 | **唯一进入热路径的改动** |
| `rtkserver/web/app.py` | `from . import api_calibration` + `api_calibration.register(app)` | 2 | 注册 |
| `rtkserver/web/api_calibration.py` | 新文件：`status/start/abort/apply/restore`，全部 `@login_required` | ~130 | 新增 |
| `rtkserver/calibration/` | 新包（service/commands/survey） | ~400 | 新增 |
| `templates/config.html` | 一张卡片（直接复用 `maint-card` 的 DOM/CSS 结构，放维护卡片上方） | ~45 | 新增区块 |
| `static/calibration.js` | 轮询 + 进度条 + 结果展示（照 `maintenance.js` 的 poll/overlay 模式） | ~200 | 新增 |
| `static/i18n.js` | `cal.*` 中英各一套 | ~40 | 新增 |
| `static/theme.css` | 复用 `.maint-*`，最多补 3~5 条 | ~10 | 可选 |
| `tests/test_contract.py` | 新 TestCase：状态机终态、接口鉴权、热路径不阻塞（用假 ser 断言 tap 异常不影响 distribute） | ~60 | 新增 |

**不修改**：`distributor.py`、`lifecycle.py`、`outlets/*`、`rtcm_parser.py`、`config.py`、`status.py`、数据库结构。这就是"旁路集成"的落点：功能全部住在 `calibration/` 里，只借一条读旁路和一条写通道。

### 8.6 状态机与前端交互

```
idle → probing（问型号/能力，1~3s）
     → surveying（进度 0~100%，可中止）
     → computing（均值/离散度/精度门槛判定）
     → applying（写 base 模式与坐标，含天线高）
     → verifying（等第一条 1005，比对坐标误差）
     → done | failed(原因) | aborted
```

- 全程**不停数据面**，本地 Caster / 上传 / 数传照常；
- 配置页每秒轮询 `/api/calibration/status`，进度条复用维护模块的遮罩样式但**不遮挡整页**（标定需要用户先选串口）；
- 结果写回 `state.station_info` 后，首页地图、本地 Caster 的 SOURCETABLE（经纬度字段）、移动站看到的源列表会**自动跟着正确**（它们都读这一个源头）—— 这是最大的"免费复用"。
- 中止/超时：`abort` 只清队列与状态，不写接收机；`applying` 阶段的超时必须有上限（写命令 ≤ 1s，验证 ≤ 10s）。

### 8.7 失败与安全

- 写入前把接收机原 TMODE 配置落到 `.calib/prev.json`（**不要**写 `settings` 表），提供"恢复上次"按钮；
- 坐标写入失败不回滚、不猜：只报"第几步失败 + 接收机原始响应码"；
- 精度门槛（水平/垂直 RMS、样本数、离散度）不达标时**不自动写入**，只展示结果并让用户决定"仍然写入"；
- 权限：所有接口 `@login_required`；未登录连状态都不给（避免泄露精确坐标）；
- 串口忙（数据源未跑/未识别到串口）时直接拒绝启动标定，不尝试抢端口。

---

## 9. 可复用的现有能力

| 能力 | 现成实现 | 位置 | 复用方式 |
| --- | --- | --- | --- |
| 串口枚举 | `GET /api/serial_ports`（已鉴权，返回 device/description/gnss_port/radio_port） | `web/api.py` | 标定页下拉框直接调用，**不要**新写枚举接口 |
| 串口识别/波特率扫描 | `detect_serial()` / `test_serial()`（真解帧判定） | `sources/serial_source.py` | tap 收到首个 RTCM 帧即可判定"已连上"，无需重复扫描 |
| RTCM 解帧 + CRC | `extract_rtcm_frames()` / `crc24q()` | `sources/rtcm_parser.py` | 标定验证阶段复用同一份实现，避免两套 CRC 逻辑 |
| 电文解析 | `pyrtcm.RTCMReader` + `parse_msm` | 同上 | 1005/1006 校验参考坐标 |
| ECEF→经纬高 | `ecef_to_latlon()` | `utils/common.py` | 写入前把经纬高换算成 ECEF（反向公式需新增，但同一数学域） |
| 站点信息单一源头 | `state.station_info` | `core/state.py` | 标定成功直接更新它 → 首页/地图/SOURCETABLE/mDNS 全部同步 |
| 鉴权 | `@login_required` + 登录限速 | `security.py` | 所有标定接口套用 |
| 会话/CSRF 兜底 | `SESSION_COOKIE_SAMESITE=Lax` | `web/app.py` | 表单式接口天然受保护 |
| WS 推送 | `state.broadcast(dict)` + `WSClient` 背压 | `core/state.py` / `web/ws.py` | 若要实时进度可用；配置页轮询更简单 |
| 后台任务范式 | 独立线程 + 有界队列 + `snapshot()` + 幂等 `start/stop` + 节流日志 | `outlets/caster_uploader.py`、`radio_bridge.py` | **直接照抄结构**，不要发明新范式 |
| 前端卡片/进度范式 | `maintenance.js`（poll/overlay/失败回显）+ `.maint-*` 样式 | `static/` | 标定卡片与进度条复用 |
| 双语 | `i18n.js` + `data-i18n` | `static/` | `cal.*` 成对新增 |
| 契约测试范式 | 源码文本断言 + 纯逻辑断言 | `tests/test_contract.py` | 锁住"热路径不被标定阻塞" |
| mDNS 公告钩子 | `hooks['startup'/'shutdown']`、`on_address_change` | `core/lifecycle.py` | 标定不需要，但可参考"旁路模块如何挂载" |

---

## 10. 风险与待决问题

1. **接收机差异是最大不确定性**：u-blox（UBX 二进制 + `CFG-TMODE3`/`NAV-SVIN`）、Unicore（文本指令）、Septentrio（HTTP/文本）、只有 RTCM 输出的设备（不可写）。适配层必须能"探测失败 → 明确告知不支持写入"，并保留"只做坐标统计 + 展示"的降级模式。
2. **写命令会短暂占用读循环**：`ser.write` 有 `write_timeout`，命令编码必须短（≤ 64 字节），否则那一轮 `read` 被推迟，上游可能出现丢帧（RTCM 1Hz，容忍 ~200ms）。
3. **波特率切换**：多数接收机要在 115200 下接收配置命令，而数据流可能是 460800。若需切速，属于方案 B（短暂停源）的范畴，应作为显式选项而不是默认行为。
4. **survey-in 进度来源**：若只有 RTCM，没有 UBX/NMEA 回包，则进度只能靠"1005 坐标序列的收敛度"估算（方差随样本数下降），UI 文案要说清"估算"。
5. **天线高**：RTCM 1006 带天线高字段，1005 不带；标定表单必须让用户输入并说明"斜高/直高"。
6. **`snapshot_providers` / `iter_snapshots` 是死代码**（`core/state.py` 定义，无任何调用点）。标定可复用它做状态聚合，但需要有人在 `push_run_status` 里调用它；若不用，建议后续删除，避免"看起来有扩展点其实没接上"的误导。
7. **默认值双份硬编码**：`constants.LOCAL_CASTER_PORT=2102` 与 `DEFAULT_CONFIG['local_caster_port']='2102'`、`routes.py` 的 `or str(LOCAL_CASTER_PORT)` 各存一份；标定若引入新默认值（如默认波特率），应只在一处定义。
8. **WS 顶层字段名冲突**：所有消息按字段存在性分发，标定若走 WS 必须用 `calibration` 这个唯一键，并考虑未登录脱敏（进度不含坐标）。
9. **升级/卸载语义**：升级执行器是独立 transient unit（会停服务）。标定运行中若用户点升级，应先拒绝（`read_state().active` 已有现成判断可参考）。
10. **ARM 资源**：`.19` 那台是 armv7l、981MB。标定线程必须常驻但空闲时零 CPU（事件驱动或 5s 轮询），取样统计用 Welford O(1) 内存。

---

## 11. 建议的落地顺序

1. 先在 `state.py` 加两个槽位，在 `serial_source` 加 tap + drain（各 15 行以内），并补一条"tap 抛异常不影响 distribute / 队列满不阻塞"的契约测试；
2. 写 `calibration/survey.py`（纯函数：均值、方差、精度门槛）+ 单测，不碰 IO；
3. 写 `commands.py` 的 u-blox 适配（最小可用：读版本、survey-in 开关、写固定坐标、查 SVIN），其余厂商留接口；
4. 写 `service.py` 状态机（事件驱动 + 5s 心跳），跑通"只读不写"的探测与统计；
5. 接 `api_calibration.py`（全部鉴权）+ 前端卡片，先只展示结果，不写接收机；
6. 打开写入路径 + `verifying`（等 1005 比对），最后补 `.calib/prev.json` 回退。

每步都能独立上线、独立回滚，且第 1 步做完之后所有后续工作都只发生在 `calibration/` 与 `web/` 里，不再触碰数据面。
