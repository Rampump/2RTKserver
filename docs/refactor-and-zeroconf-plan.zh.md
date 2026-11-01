# 2RTKserver 重构 + 固定地址访问（方案 v3 · 待开工确认）

> 合并稿：以你认可的"5 阶段 / core·utils·sources·outlets·web"分层为准，叠加 v2 里已经查证过的代码事实与 6 处必须修正的点。**代码未改动。**
>
> 与前两版的关系：v1（LLMNR + 配置项）作废；v2（19 模块细分）被本版替代；本版是唯一执行依据。

---

## 1. 采纳与修正一览

| 你的要点 | 本版处理 |
| --- | --- |
| 先旁路后核心、渐进式、每步可回滚 | **全盘采纳**，成为 P0-P7 阶段表的主线 |
| 阶段 1 常量 + 工具先拆 | 采纳；额外补 `paths.py`（见修正 ①） |
| 阶段 2 Web 层 | 采纳，但**前面必须先插一个 `core/state.py`**（见修正 ②） |
| 阶段 3 三出口，各自队列策略不动 | 采纳；**不做 `base.py` 抽象基类**（你自己也说了避免过度设计，且三者启停语义不同：listen / connect+重连 / 串口写） |
| 阶段 4 数据源 + RTCM 解析 | 采纳；解析器放 `sources/rtcm_parser.py`（见修正 ③ 的说明） |
| 阶段 5 分发 + 主循环收拢 | 采纳；`distribute()` 只做遍历入队，异常计数节流 |
| 生命周期钩子 / 状态注册 / 配置自动扩展 | 采纳，落到 `core/state.py` 三个注册表 |
| "install.sh / update.sh 基本不用改" | **必须改**（见修正 ①，不改就开机 ModuleNotFoundError） |
| 目录结构 | 采纳，外层加一个包壳 `rtkserver/`（见 §2 理由） |

---

## 2. 目标结构

```
2RTKserver/
├── server.py                    # 40 行入口壳：systemd ExecStart 不变
├── rtkserver/                  # 包壳（新增一层，理由见下）
│   ├── __init__.py              # __version__
│   ├── __main__.py              # python -m rtkserver
│   ├── paths.py                 # BASE_DIR / DB / 日志 / 密钥 / 模板 / 静态
│   ├── core/
│   │   ├── __init__.py
│   │   ├── constants.py         # WEB_PORT、队列长度、超时、协议常量、版本
│   │   ├── config.py            # DEFAULT_CONFIG / init_db / get_config / save / 重置
│   │   ├── state.py             # ★ 共享状态池 + 单例 + 钩子 + 注册表（新增节点）
│   │   ├── distributor.py       # distribute() + describe_targets()
│   │   └── lifecycle.py         # main() / run_once() / 事件 / 信号 / 启停顺序
│   ├── utils/
│   │   ├── network.py           # get_local_ip / local_ipv4_addresses / 端口探活 / 自环
│   │   ├── logger.py            # 日志初始化（只允许 main 调一次）
│   │   └── common.py            # format_bytes / duration / 可中断 sleep / retry / ecef
│   ├── security.py              # 口令哈希 / 会话密钥 / 登录锁定 / login_required
│   ├── validation.py            # validate_config（只判定"能不能干活"）
│   ├── sources/
│   │   ├── serial_source.py
│   │   ├── relay_source.py
│   │   ├── ntrip.py             # 连接/握手/源列表（被 relay 与 serial 共用）
│   │   └── rtcm_parser.py       # RTCM 统计解析（旁路线程）
│   ├── outlets/
│   │   ├── local_caster.py
│   │   ├── caster_uploader.py
│   │   └── radio_bridge.py
│   ├── web/
│   │   ├── app.py               # create_app()：Flask + Sock + jinja globals + 安全头
│   │   ├── routes.py            # / 、/config 、/login、/logout、/change_password、/restart_program
│   │   ├── api.py               # /api/*
│   │   └── ws.py                # WS 连接池、send_to_clients、/ws、首帧
│   └── discovery/
│       └── zeroconf_announcer.py   # 唯一新增能力，零业务依赖
├── templates/  static/          # 位置不变
├── tests/                       # 契约测试（待你批准进仓库）
├── reset_config.py              # 不改，靠入口壳 re-export 兼容
├── install.sh / update.sh       # 复制清单要改（修正 ①）
└── requirements.txt             # + zeroconf
```

**为什么多包一层 `rtkserver/`**（对你给的树唯一的结构性偏离，3 个理由）：
1. 顶层的 `utils`、`web`、`core` 是通用名，venv 里一旦装了同名包就是随机踩雷；包壳后是 `rtkserver.utils`，绝对唯一。
2. `install.sh`/`update.sh` 是**白名单复制**，复制一个目录比复制五个目录少一处漏改。
3. 可以用 `python -m rtkserver`，也方便以后写测试直接 import。

---

## 3. 六处必须修正（照抄贴文会出事的地方）

### ① `install.sh` / `update.sh` **必须改**，否则升级即宕机

两个脚本都是白名单：

```bash
for item in server.py reset_config.py requirements.txt install.sh uninstall.sh update.sh \
            README.md INSTALL.md INSTALL.en.md LICENSE templates static docs; do
```

不把包目录加进去，`systemctl start` 立刻 `ModuleNotFoundError: No module named 'rtkserver'`。
本版改法：

```bash
rm -rf "$INSTALL_DIR/rtkserver/__pycache__"     # 先清旧字节码，避免残留 .pyc 指向已删模块
for item in server.py rtkserver reset_config.py requirements.txt ... ; do
```

顺带：`chmod +x server.py` 保留；`reset_config.py` 不改（`server.py` 里 `from rtkserver.core.config import reset_config_to_default` 就够）。

### ② 阶段 2 之前必须先立 `core/state.py`，否则拆 Web 等于白拆

`logging` 要把日志推给 WS、`distribute` 要调三个出口、`push_run_status` 要调各出口快照。若先拆 Web，`web/ws.py` 就成了所有模块的依赖中心，只能反向 import 主模块 —— 这不是拆分，是搬家。

`core/state.py` 提供三张表 + 一个钩子（把贴文里"状态注册""生命周期钩子"落到具体接口）：

```python
# 可变单例：只能通过 state.xxx 访问
thread_exit_event, shutdown_event, restart_event
rtcm_buffer, buffer_lock, station_info, clients, clients_lock
msg_types_seen, msg_types_recent
local_caster, serial_bridge, caster_uploader, announcer

# 三张注册表（向下通知，不反向 import）
outlets = {}              # name -> publish(bytes)          出口注册；distribute 遍历
snapshot_providers = {}   # name -> () -> dict              状态快照；Web 只读这里
hooks = {'startup': [], 'shutdown': []}                    旁路模块（mDNS）挂载点

# 一条钩子：日志 → WS
publish = None            # web.app.create_app() 里赋值为 ws.send_to_clients
```

**硬规则**：任何模块都不许 `from ..core.state import local_caster` —— `from ... import` 在导入瞬间把 `None` 绑死，之后主程序赋值对该名字无效，症状是"出口开了但状态页永远显示未运行"，极难定位。一律 `from ..core import state` → `state.local_caster`。

### ③ `BASE_DIR` 的位置会漂移，用户会以为"配置全丢了"

现在：`BASE_DIR = dirname(abspath(server.py))`，DB/日志/`.secret_key` 都落在安装根目录。拆包后模块在 `rtkserver/` 里，若照抄 `dirname(__file__)` 就变成 `<安装目录>/rtkserver/2RTKserver.db` —— 新库、空配置、重新配一遍。这正是源码 L50-52 注释警告过的坑。

```python
# rtkserver/paths.py
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
```

必须单列一条验收：升级前后 `ls -la` 三个文件位置不变。

### ④ `Flask(__name__)` 拆走后模板会 404

现在是 `app = Flask(__name__)`，`__name__='server'` 是模块，root_path 就是安装根目录，`templates/`、`static/` 自动找到。搬到 `web/app.py` 后 `__name__='rtkserver.web.app'`，root_path 变成包内目录，模板 404、静态资源 404。

```python
app = Flask(__name__, template_folder=paths.TEMPLATES_DIR, static_folder=paths.STATIC_DIR)
```

### ⑤ "不能改变原有退出语义"在现状下等于"什么都不清理"

贴文写"优雅退出顺序：停数据源→停分发→停出口→停 Web→mDNS 道别，不要改变原有退出语义"。但现状是：`shutdown_event` 从头到尾**没有任何地方 set**，`main()` 靠 systemd SIGTERM 直接被杀，进程内的 `finally` 根本跑不到。所以这不是"保持语义"，而是**新增一条语义**，必须显式设计，否则 mDNS 道别包永远发不出去，局域网里 `2rtk.local` 会残留 120s 指向已停机的设备。

本版新增（`lifecycle.py`）：

```
SIGTERM/SIGINT → shutdown_event.set() → 主循环退出 → run_once 的 finally 停三个出口
              → announcer.stop()（发 TTL=0）→ 进程正常返回 0
```

systemd 侧已有 `TimeoutStopSec=20`，足够。

### ⑥ `get_config()` 不能因为拆分而新增读取点

现有 `get_config()` 每次开一个 SQLite 连接（`timeout=10`）。拆分后若各模块图方便到处调，连接数会上去（Web 一个请求里 `config_page` 已调 4 次）。

本版约束：**拆分期不新增 `get_config()` 调用点**，新代码走"传入 cfg"（现有 `upload_snapshot(cfg=None)` / `validate_config(cfg)` 已是这个模式）。等拆分稳定后再单独优化成请求级缓存，属于独立议题。

---

## 4. 阶段表（每阶段一个 commit，独立可回退）

| 阶段 | 内容 | 验收（必须全绿才进下一阶段） |
| --- | --- | --- |
| **P0 基线** | `git tag pre-split`；录基线：启动日志、11 条路由表、`/ws` 首帧 JSON、CRC 测试向量 | 基线存档；能复现当前行为 |
| **P0.5 契约测试** | `tests/test_contract.py`（**stdlib unittest，零新依赖**）：路由表 / DEFAULT_CONFIG 键集合 / `validate_config` 5 组输入 / CRC 与解帧向量 | `python -m unittest` 全绿 |
| **P1 常量 + 工具** | `paths.py`、`utils/logger.py`、`utils/network.py`、`utils/common.py`、`core/constants.py`；日志初始化收敛为唯一入口，其余模块只 `getLogger(__name__)` | 启动日志内容与基线逐行一致；`server.log` 仍落在安装根；端口探活/自环文案一致 |
| **P2 共享状态池** | `core/state.py`（三张表 + 钩子 + 单例） | 启动后 `state` 里三个 Event 与主循环用的是同一对象（打印 id 比对）；重复初始化日志会重复，验证没有 |
| **P3 Web 层** | `web/app.py`、`routes.py`、`api.py`、`ws.py`；`send_to_clients` 挂到 `state.publish` | 三页可登录/可保存；`/ws` 日志帧、状态帧、首帧与基线一致；**路由表 11 条不变** |
| **P4 三出口** | `outlets/*`，逐个原样搬（含 Caster 的 `shutdown()+close()`、3 次退避、1s accept 超时） | 三个出口各自单独开关跑一遍；单出口断开不影响另两个；状态页数字随动 |
| **P5 数据源 + 解析** | `sources/*`；解析线程独立、只读副本 | 串口/中继各跑一遍；仪表盘星数/消息类型正常；**解析卡顿不阻塞分发**（人为 sleep 验证） |
| **P6 分发 + 主循环** | `core/distributor.py`、`core/lifecycle.py`、`server.py` 入口壳 | 改配置"保存并重启"只重启数据源+出口，Web 不断线；SIGTERM 后 5s 内 `ping 2rtk.local` 失败 |
| **P7 固定地址 + IP 展示** | `discovery/zeroconf_announcer.py`；`run_status` 加 IP；首页两行；配置页/README 文案 | `2rtk.local:5757` 可开；首页首屏见 IP；`2rtk.local:2102` 能取流；安卓按提示用 IP |

**顺序上的关键调整**：贴文把"状态注册/钩子"放在最后的兼容预留里，本版把 `core/state.py` 提到 **P2**（Web 之前）。这是整条链路能不能拆干净的分水岭。

---

## 5. 符号搬迁映射（按新结构）

| 新位置 | 原 `server.py` 行段 |
| --- | --- |
| `paths.py` | 50-53、158-160、529-530 |
| `core/constants.py` | 82-112、79（`MAX_BUFFER_SIZE` 等）、5757 端口 |
| `core/config.py` | 531-592、664-697、852-884 |
| `core/state.py` | 65-79、411-417、444-468 + 新增注册表/钩子 |
| `core/distributor.py` | 2874-2935 |
| `core/lifecycle.py` | 699-851、3581-3637 + 新增信号处理 |
| `security.py` | 595-662、1424-1487 |
| `validation.py` | 1013-1160 |
| `utils/logger.py` | 119-189 |
| `utils/network.py` | 191-409 + 新增 `local_ipv4_addresses()` |
| `utils/common.py` | 887-966 |
| `sources/ntrip.py` | 1247-1377、1762-1836 |
| `sources/serial_source.py` | 1162-1246、3396-3475 |
| `sources/relay_source.py` | 3477-3579 |
| `sources/rtcm_parser.py` | 968-1014、1378-1403、3142-3357 |
| `outlets/local_caster.py` | 1916-2362、2937-2964、3116-3136 |
| `outlets/caster_uploader.py` | 2615-2912 |
| `outlets/radio_bridge.py` | 2364-2613 |
| `web/app.py` | 1406-1421、1489-1503 |
| `web/routes.py` | 1504-1545、1546-1650、1665-1696、3030-3032 |
| `web/api.py` | 1697-1760、1837-1914 |
| `web/ws.py` | 2967-3141 |
| `discovery/zeroconf_announcer.py` | **新增** |

> 解析器虽放在 `sources/rtcm_parser.py`（沿用你的结构），但它**不 import 任何数据源**，只被解析线程调用；若你希望语义更准，可改 `sinks/rtcm_parser.py`，一行 import 的事。

---

## 6. `discovery/zeroconf_announcer.py` 规格（独立单文件，零业务依赖）

```python
DEFAULT_HOSTNAME = '2rtk'
MDNS_HTTP_TYPE   = '_http._tcp.local.'
MDNS_NTRIP_TYPE  = '_ntrip._tcp.local.'   # 非标准类型，仅作端口展示
WATCH_INTERVAL   = 30

class ZeroconfAnnouncer:
    def __init__(self, hostname=DEFAULT_HOSTNAME, web_port=5757, on_address_change=None): ...
    def start(self) -> bool                     # 注册 _http._tcp（自带主机 A 记录）
    def set_ntrip(self, port, mountpoint='2RTK')  # port=None 取消；读配置页的 local_caster_port
    def snapshot(self) -> dict                  # {state, name, web_url, ntrip_url, addresses}
    def stop(self)                              # 发 TTL=0 道别包 + close
```

- **名字固定 `2rtk`，不加任何配置项**；同网段撞名时自动退 `2rtk-2.local`，只记 WARNING（不占 UI）。
- **`2rtk.local:<任意端口>` 之所以能用，只靠 A 记录**（`server='2rtk.local.'` + `parsed_addresses=本机全部非回环 IPv4`）；`_ntrip._tcp` 只是让人/工具能看到端口，失败也不影响连通。
- 只开 IPv4（ARM 板 IPv6 链路本地会把 SRV 搞坏）。
- 30s 看门狗：IP 变了就 `update_service()`，名字不变，回调 `on_address_change` 给 lifecycle 刷新首页 IP。
- `ImportError` → `start()` 返回 False + 一条 WARNING，其它功能零影响。
- 由 `core/lifecycle.py` 通过 `state.hooks['startup']` 挂载、`state.hooks['shutdown']` 收尾（钩子内异常单独捕获，绝不阻断启动）。

依赖：`requirements.txt` 加 `zeroconf>=0.132.2,<0.140`（0.132.2 是最后一个支持 Python 3.8 的稳定线；0.140 要 3.9、0.153 要 3.11，而项目声明 3.8+）。

---

## 7. IP 展示与文案（首次使用体验）

链路：`utils/network.local_ipv4_addresses()` → `run_status['local_ip'/'local_ips'/'mdns_url']` → `push_run_status()`（2s）+ **`/ws` 首帧补一条 `run_status`**。

> 首帧这条是必须的：`mode=none`（**首次使用、还没配置**）时没有任何数据循环在推状态，首页会空着——而"第一次打开就看见 IP"正是这次的需求。

- `templates/index.html` 运行状态卡加两行：`本机 IP`、`固定地址 2rtk.local:5757`。
- `static/script.js` 的 `updateRunStatus()` 加两行赋值（空值不覆盖）。
- `static/i18n.js` 加 `rs.local_ip` / `rs.mdns`（zh/en 成对，漏一个就漏字）。
- `templates/config.html` 本地 Caster 区块插一条 `field-hint`（无表单控件、无新配置项）：
  > 局域网固定地址：电脑/苹果设备用 `http://2rtk.local:5757`，NTRIP 客户端用 `2rtk.local:<端口>/2RTK`。
  > **安卓手机/平板不支持 .local，必须用首页「运行状态」里的本机 IP。**
- `README.md` 新增「用固定地址访问」章节，**安卓只能用 IP 单独一行加粗**；INSTALL 两版各补一句。

---

## 8. 验证与回滚

| 类别 | 项 |
| --- | --- |
| 契约 | P0.5 测试全绿（路由/默认配置/校验/CRC） |
| 冒烟（每阶段） | 启动 → 本地 Caster 端口通 → Web 三页可用 → 保存并重启不崩 → 停止后端口释放 |
| 边界 | 频繁改配置重启、断流重连、端口被占、16 客户端并发连本地 Caster |
| 稳定性 | 24h：RSS、线程数、fd 数平稳（拆分前后对比基线值） |
| 回滚 | 每阶段一个 commit，可单步 revert；数据文件位置不变（修正 ③），回滚无需迁移 |

已知不可控项（写进验收说明，不算缺陷）：安卓无法解析 `.local`；组播被隔离的访客网络；同网段重名退为 `2rtk-2.local`。

---

## 9. 开工前只差 3 个确认

1. **包壳** `rtkserver/`（把 `core/utils/sources/outlets/web` 装进去）是否接受？不接受就按你原树的顶层目录，我把 `sys.path` 处理和脚本复制清单相应调整。
2. **`tests/` 是否允许进仓库**（stdlib unittest，零新依赖）？这是我敢动 3640 行的前提。
3. 本地 Caster 的 **`_ntrip._tcp` 广播**保留还是删掉？（删掉不影响 `2rtk.local:2102` 可用）

确认后我从 **P1** 开工，每阶段给你：改了什么 → 验收跑了哪几条 → 结果。P0/P0.5 会先做，先让测试跑绿再动第一刀。

---

## 10. 实施记录（已完成）

### 10.1 一处无法按字面实现的约束：包名 `2rtkserver` → `rtkserver`

Python 的模块名必须是合法标识符，**不能以数字开头**。写 `from 2rtkserver.core.config import ...`
在语法解析阶段就报错：

```
SyntaxError: invalid decimal literal
```

`importlib.import_module('2rtkserver')` 虽然能加载，但全项目每一处 import 都要改成动态导入，
IDE 跳转、静态检查、`python -m` 全部失效，得不偿失。

因此包名定为 **`rtkserver`**；目录结构、分层、模块划分、模块职责与本文档完全一致，
只改了这一个字符串。安装目录仍是 `/opt/2RTKserver`，systemd 服务名仍是 `2rtkserver`，都不受影响。

### 10.2 实际交付文件

| 路径 | 说明 |
| --- | --- |
| `server.py` | 40 行入口壳，保留 `reset_config_to_default` 等兼容导出 |
| `rtkserver/paths.py` | `BASE_DIR = dirname(dirname(__file__))`，DB/日志/密钥全部落在安装根 |
| `rtkserver/security.py` | 口令哈希、会话密钥、登录态、登录限速 |
| `rtkserver/validation.py` | `validate_config`（60 秒日志节流原样保留） |
| `rtkserver/core/constants.py` | 全部常量，含新增 `WEB_HOST/WEB_PORT` |
| `rtkserver/core/config.py` | `DEFAULT_CONFIG` / `init_db` / `get_config` / `save_config` / 口令迁移 |
| `rtkserver/core/state.py` | 共享状态池 + 出口注册表 + 启动/退出钩子 + `broadcast` |
| `rtkserver/core/status.py` | `run_status` 聚合、IP 与固定地址、`push_run_status` |
| `rtkserver/core/distributor.py` | `distribute()`，遍历注册表，逐出口 try/except，不加锁 |
| `rtkserver/core/lifecycle.py` | `run_once` / `main` / SIGTERM 处理 / mDNS 钩子 |
| `rtkserver/utils/{network,logger,common}.py` | 网络与端口诊断、日志唯一初始化入口、通用函数 |
| `rtkserver/sources/{ntrip,serial_source,relay_source,rtcm_parser}.py` | 数据源与解析 |
| `rtkserver/outlets/{local_caster,radio_bridge,caster_uploader}.py` | 三个出口，启停逻辑原样搬运 |
| `rtkserver/web/{app,routes,api,ws}.py` | Flask + Sock，显式 endpoint 注册 |
| `rtkserver/discovery/zeroconf_announcer.py` | 唯一新增能力，零业务依赖 |
| `tests/test_contract.py` | 33 条契约测试（stdlib unittest） |
| `install.sh` / `update.sh` | 复制清单加 `rtkserver`，更新时先删旧包与 `__pycache__` |
| `requirements.txt` | 新增 `zeroconf>=0.132.2,<0.140` |
| `templates/`、`static/`、`README.md`、`INSTALL*.md` | IP 展示、安卓提示、固定地址文档 |

`core/status.py` 是方案 §5 映射表之外新增的一个小模块：`push_run_status` 与
`describe_targets` 原来夹在 411-439 行，既不属于分发器也不属于 Web，单独成文件后
`distributor.py` 才能保持"只做扇出"。

### 10.3 验收结果（本机实测）

| 项 | 结果 |
| --- | --- |
| 语法与导入 | `python -m compileall` 全通过；`import server` 正常 |
| 契约测试 | `python -m unittest discover -s tests` → **33 passed** |
| 路由表 | 11 条业务路由 + `/static`，与拆包前逐条一致；endpoint 名不变 |
| 登录与保存 | 未登录 `/config` → 302；登录 → `/config` 200；保存 → "配置已保存成功" |
| 接口 | `/api/load_config` 不含 `admin_password`；`/api/local_caster` 返回端口与挂载点 |
| 安全头 | `nosniff` / `SAMEORIGIN` / `strict-origin-when-cross-origin` 均在 |
| mDNS 注册 | `2rtk._http._tcp.local.` → 5757、`2rtk._ntrip._tcp.local.` → 2102，地址 `192.168.1.8` |
| **A 记录解析** | `socket.getaddrinfo('2rtk.local', 5757)` → `192.168.1.8` ✅ |
| 无 `zeroconf` 时 | `start()` 返回 False，一条 WARNING，其它功能零影响 |
| 首帧 `run_status` | 含 `local_ip` / `local_ips` / `mdns_url` / `mdns_state` / 三个出口快照 |
| 双语文案 | 中英各 236 条，新增 6 条全部成对 |

### 10.4 仍需在真机（Linux / systemd）上确认

本机是 Windows，以下四项只能上设备验：

1. `systemctl stop 2rtkserver` 后 **5 秒内** `ping 2rtk.local` 失败（SIGTERM → 道别包）。
2. 升级前后 `2RTKserver.db` / `server.log` / `.secret_key` **位置不变**（修正 ③）。
3. `systemctl status` 无 `ModuleNotFoundError`（修正 ①）。
4. 本地 Caster 端口通、改配置"保存并重启"Web 不断线。
