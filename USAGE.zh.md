# 2RTK NTRIP Server 使用手册

> 适用版本：**3.1.3**（以仓库根目录 `VERSION` 文件为准）
> 本文只讲"怎么用"：装完之后每个页面点哪里、每个字段填什么、流动站怎么连、服务怎么管。
> 原理与设计取舍见 [README.zh.md](README.zh.md)，安装细节与天线相位中心见 [INSTALL.md](INSTALL.md)。

---

## 目录

1. [30 秒速览](#1-30-秒速览)
2. [首次登录](#2-首次登录)
3. [页面导览](#3-页面导览)
4. [三分钟跑通：配置三步走](#4-三分钟跑通配置三步走)
5. [配置字段全表](#5-配置字段全表)
6. [三种出口怎么用](#6-三种出口怎么用)
7. [网页标定基准站坐标](#7-网页标定基准站坐标)
8. [流动站 / 手簿怎么连](#8-流动站--手簿怎么连)
9. [服务管理命令](#9-服务管理命令)
10. [升级、卸载、重置密码](#10-升级卸载重置密码)
11. [端口与账号速查](#11-端口与账号速查)
12. [故障排查速查](#12-故障排查速查)
13. [环境变量与调试](#13-环境变量与调试)

---

## 1. 30 秒速览

```
一个 RTK 模块 + 一台小主机 + 宽带 = 你自己的 GNSS 基准站
```

- 程序从**串口 RTK 模块**（或别的 NTRIP Caster）读 RTCM3 差分数据；
- 同时分发到**三个互相独立的出口**：上传到上游 Caster / 局域网内开一个 Caster / 通过 USB 数传电台广播；
- 所有配置、状态、标定、升级都在 **Web 界面**完成，端口 **5757**；
- 配置存在 SQLite 里（`2RTKserver.db` + `calibration.db`），不依赖任何云服务。

```
访问地址：  http://<设备IP>:5757     或   http://2rtk.local:5757
默认账号：  admin / admin   （登录后立刻改密码）
```

---

## 2. 首次登录

| 项 | 值 |
|---|---|
| Web 地址 | `http://<设备IP>:5757` |
| 固定地址（mDNS） | `http://2rtk.local:5757`，名字被占用时自动退成 `http://2rtk-2.local:5757`（首页会显示实际名字） |
| 默认用户名 | `admin` |
| 默认密码 | `admin` |
| 登录失败限速 | 同一 IP 连续失败 **5 次**后锁定 **300 秒** |

**注意：**
- 安卓 / 鸿蒙手机**不解析 `.local`**，请用 IP 访问。
- 浏览器打开 `2rtk.local` 报 502 但 IP 能开，是本机代理 / VPN（Clash、v2rayN、公司 VPN）把 `.local` 劫走了，把 `*.local` 加进代理直连列表即可。
- 首页 `/` 是**公开**的：未登录也能看信号质量、星图，但看不到基准站精确坐标和设备序列号。

---

## 3. 页面导览

登录后左上角 / 导航栏可访问以下页面：

| 路径 | 页面 | 用途 | 是否需要登录 |
|---|---|---|---|
| `/` | **状态首页** | 实时运行状态：数据源、三个出口、星图、RTCM 电文统计、日志、本机 IP、固定地址 | 公开（坐标脱敏） |
| `/login` | 登录页 | 账号密码登录 | — |
| `/config` | **运行设置** | 选数据源、填出口参数、改配置、重启数据面 | 是 |
| `/calibrate` | **基站标定** | 在浏览器里测天线坐标并写进模块（CORS / Survey-in / 手动） | 是 |
| `/change_password` | 修改密码 | 改管理员口令 | 是 |
| `/upgrade` | **系统维护** | 检查更新、一键升级（失败自动回滚） | 是 |
| `/logout` | 退出登录 | POST 请求，清 Session | — |

> 状态首页所有实时数据通过 WebSocket `/ws` 推送，约 2 秒一帧；未登录连接只收脱敏帧。

---

## 4. 三分钟跑通：配置三步走

进入 `/config`（运行设置）：

### 第 1 步：选数据源（二选一）

- **serial（串口模式）**——RTK 模块通过 USB 接到小主机：
  - `串口`：留空**自动扫描**；也可从下拉框选 `/dev/ttyUSB0` 之类。
  - `波特率`：留空让程序自己从模块**回读**（推荐）；常用值 115200 / 460800。
- **relay（中继模式）**——从另一个 NTRIP Caster 拉流再转发：
  - 填 `中继地址`（域名或 IP）、`端口`（默认 2101）、`挂载点`，可选账号密码。
  - 点"获取挂载点列表"可自动拉取对方 SOURCETABLE 供下拉选择（结果缓存 5 分钟）。

### 第 2 步：勾选需要的出口（可多选，互不影响）

在页面上同时看到三个开关，**想勾哪个勾哪个**：

- ☑ **上传到上游 Caster**——把这一路流以 NTRIP SOURCE 推到云端（如 2rtk.cn、RTK2go、自建 Caster）。
- ☑ **启用本地 Caster**——在本机 `bind` 一个 NTRIP 服务，供局域网内流动站直连。
- ☑ **启用无线电数传**——把数据单向透传到 USB 数传电台，给没有网络的流动站用。

一个都不勾也能保存：程序只解析数据、在状态页显示星图，不往外发。

### 第 3 步：保存

- 点 **保存**：配置写库，**不重启**（下次数据面重启时生效）。
- 点 **保存并重启**：写库后立即重启数据面（断串口、重连出口），Web 不中断。

保存时程序会自动拦截：本地 Caster 端口被别的进程占用、上传地址指回本机自环、端口填写冲突等，配置不会脏写进库。

---

## 5. 配置字段全表

配置保存在 `2RTKserver.db` 的 `settings` 表里，所有字段都能在 `/config` 页面改。

### 数据源

| 字段 | 说明 | 默认值 |
|---|---|---|
| `mode` | `serial` 串口 / `relay` 中继 / `none` 未配置 | `none` |
| `serial_port` | GNSS 模块串口设备节点，留空自动扫描 | 空 |
| `baud_rate` | 波特率，留空自动从模块回读 | 空 |
| `relay_host` / `relay_port` | 上游 Caster 地址 / 端口 | 空 / `2101` |
| `relay_mountpoint` | 中继挂载点名 | 空 |
| `relay_user` / `relay_password` | 中继账号密码（对方要求时填） | 空 |

### 出口 ① 上传上游 Caster

| 字段 | 说明 | 默认值 |
|---|---|---|
| `caster_enabled` | 是否开启上传 | `0` |
| `caster_host` / `caster_port` | 上游 Caster 地址 / 端口 | 空 / `2101` |
| `caster_mountpoint` | 你在对方那里申请的挂载点名 | 空 |
| `caster_password` | 对方要求的推送密码 | 空 |

### 出口 ② 本地 Caster

| 字段 | 说明 | 默认值 |
|---|---|---|
| `local_caster_enabled` | 是否开启本地 Caster | `0` |
| `local_caster_host` | 监听地址，`0.0.0.0` 表示所有网卡 | `0.0.0.0` |
| `local_caster_port` | 监听端口（**本机真 bind**） | `2102` |
| 挂载点（固定） | 写死，不可改 | `2RTK` |
| 账号 / 密码（固定） | 写死，不可改 | `2RTK` / `2RTK` |
| 最大并发客户端 | 写死 | `16` |

### 出口 ③ USB 数传透传

| 字段 | 说明 | 默认值 |
|---|---|---|
| `radio_enabled` | 是否开启数传 | `0` |
| `radio_port` | 数传电台的串口设备节点 | 空 |
| `radio_baud` | 数传波特率，必须 ≥ 电台空口速率 | `115200` |

可选波特率：`115200 / 921600 / 460800 / 230400 / 57600 / 38400 / 19200 / 9600`。

### 账号

| 字段 | 说明 | 默认值 |
|---|---|---|
| `admin_user` | Web 登录用户名 | `admin` |
| `admin_password` | Web 登录密码（PBKDF2-SHA256 哈希存储） | `admin` |

---

## 6. 三种出口怎么用

### ① 上传到上游 Caster

适用场景：你在 2rtk.cn / RTK2go / 自建 Caster 那里申请了一个挂载点，想把自己基准站的差分推上去，让外网流动站通过它取数。

填好 `caster_host / port / mountpoint / password` 后保存。程序会：

- 主动 TCP 连到对方，发 NTRIP `SOURCE` 握手；
- 连接失败自动每 5 秒重连，**不会阻塞串口数据读取**；
- 状态首页会显示 `未配置 / 连接中 / 已认证 · 正在推送 / 连接中断 · 重试中 / 认证失败`。

> ⚠ 这个端口（默认 2101）是**对端**的端口，不占用本机端口；你本机同时开本地 Caster 用 2102 不会冲突。

### ② 局域网内置 Caster

适用场景：手簿、平板、无人机地面站和这台小主机在同一个 Wi-Fi / 局域网里，直接连本机取差分，不用走外网。

打开开关后，任意标准 NTRIP 客户端连：

```
地址：  <设备IP>     （或 2rtk.local）
端口：  2102
挂载点：2RTK
用户名：2RTK
密码：  2RTK
```

- 访问 `http://<设备IP>:5757/` 不需要登录也能看到当前在线客户端数。
- 最多同时 16 个客户端；某个客户端不收数据，队列满了会被自动踢掉，不影响其他人。
- 不设 CAS / NET 计费，就一个挂载点。

### ③ USB 数传电台透传

适用场景：野外没有手机网络，流动站靠 433 MHz / 数传电台收差分。

把**发射端**数传电台插到小主机 USB 上，选对串口和波特率，保存即可。程序做的事非常单纯：

- 从数据队列里拿 RTCM 字节流，**原样写进串口**；
- 不解析、不封协议、单向只发不收；
- 断线 3 秒自动重连。

流动站那边配对一个**同频点、同波特率**的接收电台，在 RTK 软件里把数据口设成对应 COM 口即可，**不需要 NTRIP 账号密码**。

> 数传波特率必须 ≥ 电台空口速率，否则数据在串口里积压，流动站会反复掉固定解。

---

## 7. 网页标定基准站坐标

打开 `/calibrate`（前提：接了串口模块）。

### 支持的模块

内置了型号档案、能全自动标定的：

- u-blox ZED-F9P
- Quectel LG290P
- Quectel LC29H(BS)
- Unicore N4 系列

其他模块（UM980 / UM982 / Septentrio 等）能当数据源用，但**标定页不支持自动写坐标**，需要用厂家上位机写好 APC 坐标，本程序会自动从 RTCM 1005/1006 读出来显示在地图上。

### 三种标定方式

| 方式 | 你需要准备什么 | 能拿到的精度 |
|---|---|---|
| **CORS / VRS** | 一个可用的 CORS 账号（服务器、端口、挂载点、用户名、密码） | 厘米级（固定解） |
| **Survey-in 自测绘** | 什么都不用，但天空必须开阔 | 亚米级 / 分米级 |
| **手动输入** | 已经用静态测量 / 控制点得到了天线 APC 的经纬度 + 椭球高 | 取决于你输入的值 |

### 操作流程

1. 页面顶部点 **读取模块信息**——自动识别型号、波特率（在下拉框里手动指定型号可跳过扫描）。
2. 选标定方式，填参数。
3. 点 **开始标定**。期间：
   - 数据面会被暂时挂起（标定独占串口）；
   - 页面实时显示当前阶段、已用时间、卫星数、解算状态（`fixed / float / sub_meter / decimeter`）；
   - 天线被遮挡时不会"假装成功"，会诚实给一个较低的等级。
4. 标定完成后程序自动：把坐标写进模块 → **回读 RTCM 1005/1006 逐项比对** → 不一致判失败。
5. 结果存入 `calibration.db`，历史列表里可对同一根天线**复用**之前的坐标。

> 标定中途异常退出（断电、杀进程）也不会把主程序卡死：标定租约每 2 秒喂一次心跳，**30 秒无心跳自动放行**，数据面恢复。

---

## 8. 流动站 / 手簿怎么连

分三种情况：

### 情况 A：流动站和主机在同一局域网 / Wi-Fi

在 RTK 软件（Surpad、EZSurvey、实习助手、TopSpark、RTKGO 等）里新建数据链：

```
协议类型：  NTRIP
Caster IP： <小主机 IP>
端口：      2102
挂载点：    2RTK
用户名：    2RTK
密码：      2RTK
```

如果是 Windows / macOS / iPhone，IP 处也可以直接填 `2rtk.local`。

### 情况 B：流动站走 4G 公网

在 RTK 软件里填你在**上游 Caster**（2rtk.cn 等）申请的信息——本程序已经在出口 ① 把流推上去了，流动站连的是上游，不是小主机本身。

### 情况 C：野外数传电台

小主机那端插发射电台，流动站这端插接收电台，RTK 软件里把差分源设成"内置电台 / 串口"，选对应 COM 口即可。两端电台**频点、信道、空中波特率必须一致**。

---

## 9. 服务管理命令

安装后系统里有一个 `2rtkserver` 命令（其实是 systemd 的封装）：

```bash
sudo 2rtkserver start        # 启动
sudo 2rtkserver stop         # 停止
sudo 2rtkserver restart      # 重启
sudo 2rtkserver status       # 看状态
```

日志：

```bash
journalctl -u 2rtkserver -f              # 跟随 systemd 日志（推荐）
tail -f /opt/2RTKserver/server.log       # 或看文件日志（1 MB × 3 轮转）
```

程序安装位置：`/opt/2RTKserver/`，Python 虚拟环境在 `/opt/2RTKserver/venv/`。

> 注意：Web 页面上点"保存并重启"只会**重启数据面**（串口、出口线程），Web 服务本身不重启，刷新页面不影响。真正重启整个进程用 `sudo 2rtkserver restart` 或 systemd。

---

## 10. 升级、卸载、重置密码

### 升级

**永远不要**用 `install.sh` 升级——它结尾的 `systemctl start` 对已在跑的服务是空操作，文件换了旧代码还在内存里。

两种正确方式：

1. **网页升级（推荐）**：登录 → `/upgrade`（系统维护）→ 检查更新 → 立即升级。
   自动拉代码 → 语法编译 + 导入冒烟 + 契约测试 → 停服务 → 换文件 → 装依赖 → 起服务 → 等 5757 端口应答。**任何一步失败自动回滚**。
2. **命令行升级**：

   ```bash
   sudo rm -rf /tmp/2rtk-update && \
   (sudo git clone --depth 1 https://gitcode.com/rampump/NTRIPserver.git /tmp/2rtk-update || \
    (sudo rm -rf /tmp/2rtk-update && sudo git clone --depth 1 https://github.com/Rampump/2RTKserver.git /tmp/2rtk-update)) && \
   sudo bash /tmp/2rtk-update/update.sh
   ```

升级会保留 `2RTKserver.db`、`calibration.db`、`.calibration/` 和 `venv`。

### 卸载

**网页不提供卸载按钮**（误点不可逆）。只能 SSH：

```bash
# 交互式
sudo bash /opt/2RTKserver/uninstall.sh

# 全自动（y = 确认卸载，y = 两个数据库都备份到 /root）
printf 'y\ny\n' | sudo bash /opt/2RTKserver/uninstall.sh
```

卸载前会把 `2RTKserver.db`（账号 + 连接设置）和 `calibration.db`（你测出来的基站坐标）先拷到 `/root/`。

### 忘记管理员密码

网页没有重置入口（刻意不做，避免代理后被绕过）。SSH 上去：

```bash
sudo systemctl stop 2rtkserver
sudo /opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py --yes --restart
```

这会把管理员重置回 `admin / admin`，**标定坐标保留**。如果连坐标也想清掉（危险）：

```bash
sudo /opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py --yes --restart --purge-calibration
```

---

## 11. 端口与账号速查

| 端口 | 谁的 | 占不占本机 | 备注 |
|---|---|---|---|
| **5757** | 本程序 Web 界面 | 占（`0.0.0.0`） | 浏览器访问 |
| **2102** | 本地 Caster | 占（`0.0.0.0`） | 流动站 NTRIP 接入，挂载点 `2RTK`，账号 `2RTK/2RTK` |
| **2101** | 上传 / 中继对端 Caster | **不占** | 只是出站连接的目标端口 |
| mDNS 5353 | `2rtk.local` 广播 | 系统服务 | 安卓不解析 |

| 账号体系 | 用户名 | 密码 | 用在哪 |
|---|---|---|---|
| Web 管理员 | `admin` | `admin`（首次登录后请改） | 浏览器登录 |
| 本地 Caster | `2RTK` | `2RTK` | 流动站 NTRIP 接入 |

---

## 12. 故障排查速查

| 现象 | 怎么办 |
|---|---|
| 浏览器打不开 `2rtk.local`，但 IP 能开 | 代理 / VPN 把 `.local` 劫了，把 `*.local` 加进直连；或直接用 IP |
| 本地 Caster 启动报 `Address already in use` | `ss -lntp \| grep :2102` 看谁占了，改端口或停掉那个进程 |
| 上传状态一直"连接中"或"认证失败" | 检查上游 Caster 地址 / 端口 / 挂载点 / 密码；别把上传指到本机 `127.0.0.1:2102`（自环会被直接拒绝） |
| `Permission denied: /dev/ttyUSB0` | 把服务用户加 `dialout` 组；安装脚本是 root 跑服务，手动跑时才会遇到 |
| 状态页有数据但没卫星 | 模块没在发 MSM 观测电文，检查模块是否切到了**基准站 / Rover-base** 模式 |
| 地图瓦片空白 | 只是外网图床被墙，不影响差分数据本身 |
| "读取模块信息"转很久 | 在逐档扫波特率；在下拉框直接指定型号可跳过扫描 |
| 登录被锁 300 秒 | 等 5 分钟；或 `sudo systemctl restart 2rtkserver` |
| 想完全重置 | 见上文"忘记管理员密码" |

---

## 13. 环境变量与调试

| 变量 | 默认 | 作用 |
|---|---|---|
| `PUBLIC_STATION_COORDS` | `0` | 设为 `1` 时，未登录访问状态页也能看到精确基站坐标和设备序列号（默认脱敏） |
| `SECRET_KEY` | （随机生成并写入 `.secret_key`） | Flask Session 密钥；不设则首次启动自动生成持久化 |

**调试建议：**

- 想看实时发生什么：`journalctl -u 2rtkserver -f`。
- 想看具体某段数据：状态页下方有实时日志和 RTCM 电文统计。
- 命令行手动跑（不通过 systemd）：

  ```bash
  cd /opt/2RTKserver
  sudo ./venv/bin/python server.py
  ```

- 标定相关设计与字段细节见 `docs/` 目录下的 `*.zh.md`（特别是 `auto-calibration-design.zh.md` 和 `cors-calibration-flow.zh.md`）。

---

## 附：目录里有什么

```
/opt/2RTKserver/
├── server.py             # 入口壳（真正逻辑在 rtkserver/）
├── reset_config.py       # 命令行重置管理员 / 配置
├── install.sh            # 安装（首次用）
├── update.sh             # 升级（日常用这个）
├── uninstall.sh          # 卸载（仅 SSH）
├── requirements.txt
├── rtkserver/            # Python 源码包
├── templates/  static/   # Web 页面
├── docs/                 # 设计文档与截图
├── venv/                 # Python 虚拟环境
├── 2RTKserver.db         # 配置 + 管理员账号（SQLite）
├── calibration.db        # 标定坐标记录（SQLite，站点资产）
├── .calibration/         # 标定运行状态、备份
├── .secret_key           # Flask Session 密钥
├── .version              # 当前安装版本与 commit
└── server.log            # 文件日志（1 MB × 3 轮转）
```
