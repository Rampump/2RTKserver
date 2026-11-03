# CORS 差分标定流程（独立文档）

> 本文只讲一件事：**把这台 LG290P 从「基准站」临时切成「流动站」，用 CORS 网络差分把自己
> 定到厘米级，再把结果写回基准站坐标**的完整流程。
> 与 `docs/auto-calibration-design.zh.md` §6/§7/§9 对应，但**以代码实际行为为准**——
> 方案里写的是"应该怎么做"，本文写的是"现在实际怎么做，以及为什么"。
>
> 代码：`rtkserver/calibration/{cors,writer,service}.py`　最后核对：2026-11-02

---

## 0. 一页结论

| 问题 | 结论 |
| --- | --- |
| 模块自己会发 GGA 吗 | **rover 模式下会**（实配 1 Hz）。但代码**不依赖**这个默认，每次都显式下发 |
| GGA 要不要持续转发 | **要，而且逐条转发**。模块出一条就转一条，不攒批、不抽样 |
| 转发多快合适 | 模块 1 Hz → 转 1 Hz。上限保护 5 Hz，下限靠 3 秒保底重发 |
| 转得越快精度越高吗 | **不会**。VRS 只用这条位置决定"在哪儿合成虚拟观测"，多转不改变观测质量 |
| 多久能进固定解 | 天线开阔 30~40 秒；本机天线在巷道（有遮挡）**38~120 秒**，波动大 |
| 采多久 | 最少 180 秒，收敛即停（连续 60 秒 σ≤2 cm）。不收敛就采满时长 |
| 取哪个值 | 3σ 剔除后**算术平均**（不是卡尔曼输出） |
| 1005 偏差能证明精度吗 | **不能**。那是"写进去的值 = 播出来的值"的循环验证，见 §9 |

---

## 0.5 先选对标定方式（四种，别选错）

页面上有四个选项，区别只有一件事：**这次要不要把坐标写进模块**。

| 方式 | 谁用 | 会改模块吗 | 结果 |
| --- | --- | --- | --- |
| **CORS 差分标定**（`cors`） | 双模模块（自己能当流动站） | **会**：切 rover → 测 → 写坐标 → 切回 base → 保存 → 重启 → 校验 | 模块直接变成可用的基准站 |
| **只测坐标**（`external_rover`） | 外接流动站、或没有命令集的未知模块 | **不会**：不切模式、不写坐标、不保存 | 只给出一组 ECEF/经纬度，由你手动填进基准站 |
| **Survey-in 自收敛**（`survey_in`） | 支持 Survey-in 的双模模块，且**不要求厘米级** | **会**：切 base → 收敛 → 写坐标 → 保存 | 亚米级（0.8~3.0 m），耗时 10~60 分钟 |
| **只写已知坐标**（`manual`） | 只支持基准站的模块，坐标来自已知点或历史记录 | **会**：写坐标 → 切 base → 保存 → 校验 | 精度由你给的坐标决定，流程不测量 |

三条硬边界：

1. **只测不写遇到"当前是基准站"会直接拒绝**，不会顺手把别人的基准站切成流动站——
   那会让下游所有流动站立刻失去差分，而现场只会看到"设备自己坏了"。
2. **未知模块只能走"只测坐标"**。认不出型号就没有命令集，任何写操作都无法下发；
   流程会自动切到只读驱动，只读串口、只转发 GGA、只注入 RTCM。
3. **Survey-in 会先问"是不是已经收敛过"**。已收敛（`<Valid>==2`）就直接复用那组均值，
   不再重跑 1800~3600 次；要强制重收敛，把 `svin_resume` 关掉。

### RTCM 输出怎么选

| 选项 | 什么时候用 |
| --- | --- |
| 坐标报文 **1005**（默认） | 绝大多数情况。下游只认 1005 时不要选 1006 |
| 坐标报文 **1006** | 需要把天线高度一并播出去（1006 = 1005 + 16 bit 天线高度） |
| 坐标报文 **沿用** | 模块本来就配好了，只是不想被流程改 |
| MSM **4**（默认） | 带宽优先。数传链路吃紧时必须用这个 |
| MSM **7** | 精度优先，带宽约 3~5 倍。只在有线/4G 充裕时用 |
| MSM **沿用** | 不改模块的 RTCM 档位配置 |

两项都选「沿用」时，`MSG_SETUP` 一步都不下发。此时收尾校验只能确认"确实在播坐标"，
无法比对你是不是刚写进去的那个值——因为模块可能还在播它自己存的旧坐标。

---

## 1. 行业标准流程（外部依据）

网络 RTK 的本质是一条**双向**链路。很多人只记得"收差分"，漏掉"报位置"这一半：

```
        ① NTRIP 握手（HTTP GET 挂载点 + Basic 认证）
  模块 ──────────────────────────────────────────────▶ Caster
        ② GGA 上报（"我在哪儿"，1 Hz 持续）
  模块 ──────────────────────────────────────────────▶ Caster
                                                      │
                                    ┌─────────────────┘
                                    │ VRS 在你的位置旁合成一个虚拟基准站
                                    ▼
        ③ RTCM 差分下行（1005 坐标 + 1074/1084/1094/1124 观测）
  模块 ◀────────────────────────────────────────────── Caster
        ④ 模块做 RTK 解算 → GGA 质量位从 1 变 4（固定解）
```

四个关键事实（都有公开资料支撑）：

1. **VRS 类挂载点在你上报有效 GGA 之前，一帧 RTCM 都不发。**
   所以"连上 200 但没数据"不是失败，是还没告诉它你在哪。
   → 对应代码：`NO_DATA_GRACE=5s` 内无数据只提示，不判失败。
2. **NEAR 类挂载点每次收到新 GGA 都会重新算距离**，必要时把你切到更近的基准源。
   这就是"持续转发"的实际价值——不是提精度，是**让服务端始终知道你在哪**。
3. **各家对 GGA 的频率要求是"下限"而不是"越高越好"**：

   | 来源 | 要求 |
   | --- | --- |
   | 多数资料/客户端默认 | **1 Hz** |
   | NovAtel 建议 | ≤ 5 秒一次 |
   | SmartNet | 30 秒无 GGA 直接踢连接 |
   | 服务器压力口径 | 2 Hz 以上属于无谓消耗 |

   → 结论：**跟随模块的 1 Hz 逐条转发**，既不漏也不刷。
4. **RTCM 是 1 Hz 的**（1005 约 10 秒一条，MSM 1 秒一条）。
   差分龄期 < 2 s 才算健康；> 2 s 位置会开始摆动。

---

## 2. 本项目的完整链路

### 2.1 阶段时序

```
 ① PREPARE        校验参数（host/port/mountpoint/duration）
 ② HOLD           主程序数据面让出串口（标定期间不能双开）
 ③ OPEN/DETECT    打开串口、识别模块型号与能力位
 ④ BACKUP         备份原配置 → .calibration/backup.json（失败即中止）
 ⑤ ROVER_SETUP    get_mode() → 非 rover 才切 → 回读确认   ← 模式门禁 #1
 ⑥ NMEA_SETUP     显式下发 GGA=1、GSA=1、FIXRATE=1000ms
 ⑦ GGA_WAIT       等 3 条**不同**的原生 GGA（最多 8 s），等不到才降级软件生成
 ⑧ CORS_CONNECT   裸 socket 握手 → 再确认一次 rover        ← 模式门禁 #2
 ⑨ FIX_WAIT       等连续 30 s 同时满足 质量=4 / 星数≥12 / PDOP≤2.0
 ⑩ COLLECT        1 Hz 采样 → 3σ 剔除 → 卡尔曼 → 收敛判定
 ⑪ CORS_CLOSE     关 socket、归还租约
 ⑫ WRITE_COORD ─ ⑬ BASE_SETUP ─ ⑭ MSG_SETUP ─ ⑮ SAVE ─ ⑯ RESTART
 ⑰ VERIFY_MODE    回读模式与坐标
 ⑱ VERIFY_RTCM    抓 1005，比对播发值（一致性，非精度）
 ⑲ INJECT         结果上链（写 calibration 记录）
```

⑫ 之后**完全复用**历史坐标写入链路，没有第二套代码——这是硬约束，
契约测试 `test_cors_collect_reuses_write_chain` 锁着。

### 2.2 每个阶段的失败处理

| 阶段 | 失败条件 | 处理 |
| --- | --- | --- |
| ROVER_SETUP | 切不过去 / 回读不是 rover | 立即失败，**不写 NVM**，回滚 |
| NMEA_SETUP | GGA 配不上 | 致命（拿不到固定解判据）→ 失败回滚 |
| NMEA_SETUP | GSA 配不上 | 降级 HDOP≤1.5 并界面提示（不中止） |
| GGA_WAIT | 30 s 内既无 GGA 也无 RMC/GLL | 失败（天线/串口问题） |
| CORS_CONNECT | DNS/401/404/SOURCETABLE | **确定性错误，不重试**，直接报具体原因 |
| CORS_CONNECT | 网络不通/超时 | 指数退避 1s/2s/4s，累计断连 > 5 min 终止 |
| FIX_WAIT | 超时（默认 180 s，可 `--fix-timeout` 调） | 失败，**带出最后观测值**（质量/星数/PDOP） |
| COLLECT | 一个样本都没采到 | 失败 |
| COLLECT | 断连 | 退避重连 + **清空连续固定解计数**（§6.5） |
| 任意 | 用户取消 / 异常 | 安全关 socket、归还租约、按备份回滚 |

---

## 3. GGA 转发规则（本轮重点改造）

### 3.1 三条来源规则

| 情况 | 做法 |
| --- | --- |
| 模块原生 GGA 可用（质量任意） | **原样透传原始行**，不重算、不改质量位 |
| 原生 GGA 质量 = 0（未定位） | 仍然转发（连接不会断），同时提示"暂不生效" |
| 模块不发 GGA 但有 RMC/GLL | 软件生成：Talker=`GN`、**质量位固定 1**、卫星数/HDOP 有则填、Sep 留空 |

> 软件生成的质量位只能是 1（单点）。谎报 4 会让服务端以为你已固定，
> 反而拿到错误的虚拟观测——这是合规红线，契约测试锁着。

### 3.2 节奏规则（本次改动）

旧实现：`pump()` 里 `if now - last >= 1.0` 才去缓存取一条。
问题有两个：

1. **先发后读**：采集循环是"先 pump 再 drain"，发出去的是上一轮缓存里的位置；
2. **按秒抽稀**：模块恰好 1 Hz，用"距上次 ≥1 s"去卡 1 Hz 信号，抖动会造成漏一条或重复发同一条。

新实现（`cors.py` / `writer.py`）：

```python
# GgaManager.source()：行变了就立刻发
if payload != self._last_forwarded:
    self._last_forwarded = payload
    return True, payload              # 新位置 → 立即转发
if now - self._last_sent_at >= GGA_KEEPALIVE:   # 3 秒
    return True, payload              # 模块不出新行 → 保底重发，防 Caster 踢人
return False, b''

# CorsClient.pump()：只留最小间隔保护
if now - self._last_gga_sent >= GGA_MIN_INTERVAL:   # 0.2 s → 上限 5 Hz
    ...
```

并且采集循环改成 **先 drain 再 pump**：

```
while True:
    self._drain_gga(0.08)              # ① 读模块这 80ms 内新吐的 GGA/GSA
    client.pump(gga_manager.source)    # ② 收 RTCM 注入 + 逐条转发刚读到那条
    ... 用同一份缓存判固定解、采样 ...
```

这样"上报给 CORS 的位置"和"拿来做判据的位置"**永远是同一条**，不会出现
上报的是旧位置、判据用的是新位置这种错位。

### 3.3 位置冻结：为什么静态标定不能如实上报位置

VRS 是拿你上报的那条 GGA **合成虚拟基准站**的。FIX_WAIT 阶段模块还在浮动解，
位置每秒分米~米级地跳；位置一跳，服务端就把虚拟站跟着挪，**上一轮已搜索到的
整周模糊度全部作废**，于是重新搜索 → 位置又跳 → 再作废，形成死循环。

天线不动，就报一个不动的位置。这是 RTKLIB 的 STRSVR、u-center 静态测试的通行做法。

```python
# cors.py
GGA_FREEZE_POSITION = True       # 静态标定默认冻结
```

只替换 `字段2~5`（纬度/N、经度/E），**UTC 时间、质量位、星数、HDOP 全部保持实时**——
部分服务会检查时间戳是否新鲜、质量位是否合理，整行伪造会被直接拒。

两条保险（`GGA_FREEZE_MIN_QUALITY` / `GGA_FREEZE_RADIUS`）：

| 保险 | 判据 | 为什么 |
| --- | --- | --- |
| 只认真定位 | 质量位 ≥ 1 才落锚 | 切模式瞬间模块可能吐质量位 0 的旧行，经纬度不是当前真实位置，拿它落锚比不冻结更糟 |
| 偏离重落锚 | 实测离锚点 > 100 m 时重新落锚 | 多径抖动是米级，不会触发（锚点照旧稳住虚拟站）；能触发的只有"锚点本来就是错的"这种量级（例如残留的上一个基准站坐标），那时跟着实测走才不会全盘解不出来 |

真机佐证（2026-11-02，同一天同一根天线）：

| 时间 | GGA 策略 | 结果 |
| --- | --- | --- |
| 11:29 | 逐条转发，**不冻结** | 300 秒始终浮动解（质量 5），失败 |
| 11:50 | 逐条转发 + **冻结** | **72 秒进入固定解**，连续 30 秒后开始采集 |
| 11:55 | 同上（复测） | **38 秒进入固定解** |

### 3.4 重连后

断连重连成功 → `send_gga_now(gga_manager.force_source)` **立刻强发一条**，
不等节奏（VRS 要尽快重新认出位置），然后 `fix_hold_started = None`
**清空连续固定解计数**——断连期间的"连续 30 秒"是不成立的。

---

## 4. 固定解准入与采集滤波

### 4.1 准入判据（三条同时满足，连续 30 秒）

| 判据 | 门限 | 来源 |
| --- | --- | --- |
| 质量位 | `= 4`（RTK 固定解） | GGA 第 6 字段 |
| 卫星数 | `≥ 12` | GGA 第 7 字段 |
| PDOP | `≤ 2.0` | **GSA 第 15 字段** |

PDOP 只能从 GSA 拿（GGA 里只有 HDOP）。模块不出 GSA 时降级为 `HDOP ≤ 1.5`，
并在界面/日志标注——因为 HDOP 不含高程方向，门限必须收紧才等价。

**为什么必须连续 30 秒**：刚进固定解的头几秒，模糊度虽然固定了但整周还未完全收敛，
这时采到的点会带着系统性偏移。30 秒是让解算稳下来的最短观察窗。

### 4.2 采集

- **1 Hz 采样**：只在拿到新的固定解 GGA 时采一个点（按时间戳去重，不按循环次数）。
- **3σ 剔除**：滑动窗口 N=30，σ 下限钳到 5 mm（防止样本太干净时把所有点都判成野值）。
- **三轴卡尔曼**：常值模型，`q=1e-6`、`r=0.0004`，E/N/U 三轴独立。
  **每个观测只喂一次**（真机踩过的坑：重复喂会让协方差被压到极小，37 秒就"假收敛"）。
- **取值**：3σ 剔除后的**算术平均**。卡尔曼只承担"稳定性度量"角色，不参与取值；
  两者差值输出到 `kf_ecef` / `kf_minus_mean_mm` 供事后核对。

### 4.3 双终止条件

| 条件 | 说明 |
| --- | --- |
| 到时长 | `short=180s` / `standard=300s` / `long=600s` |
| 收敛 | 连续 60 秒三轴 **原始样本** σ ≤ 2 cm |

> 收敛判据必须作用在**原始样本**上，不能作用在滤波输出上。
> 仿真证据：原始 σ=40 mm 时滤波序列 σ 只有 17.4 mm（约 2.3 倍平滑），
> 拿它判 2 cm 等于把门限放宽到原始 σ≈4.6 cm。

不收敛时不自动失败：CLI 采用当前值并在记录里如实标注 `converged=false`，
Web 层给"采用当前值 / 延长 10 分钟 / 终止"三选一（§7.6）。

---

## 5. 安全红线（不可妥协）

| 红线 | 为什么 | 代码位置 |
| --- | --- | --- |
| CORS 的 RTCM **只注入模块串口**，绝不 `distribute()` | 全局分发会把别人的差分当自身数据发给下游，下游会用错坐标 | `cors.pump → inject` |
| 注入前必须确认模块在 **rover** | base 模式下注入 RTCM 不产生差分，只会白等到超时 | `_ensure_rover` |
| 断连必须清空连续固定解计数 | 否则"连续 30 秒固定"跨了断连期，判据失效 | `fix_hold_started = None` |
| 任何异常都要关 socket、归还租约 | 半死连接会占着串口，主程序无法恢复数据面 | `finally` 分支 |
| 失败回滚必须写 NVM | 切 rover 时 `persist=True` 已落盘，只写内存态会导致断电重启后基准站变流动站 | `_revert` |
| 回滚后必须**回读校验** | 真机事故：6 条速率"回放成功"，实际 `1005=0`，基准站不播坐标 | `_revert` 回读 |

---

## 6. 真机操作手册

### 6.1 前置检查

```bash
# 1) 看挂载点（确认账号有效 + 拿到可用接入点）
python -m rtkserver.calibration.service --list-mountpoints \
    --cors-host 120.253.239.161 --cors-port 8002

# 2) 看当前备份（能不能安全回滚）
python -m rtkserver.calibration.service --show-backup

# 3) 看模块现状（模式/坐标/NMEA 速率）
python -m rtkserver.calibration.service --port COM3 --seconds 12
```

### 6.2 跑标定

```bash
python -m rtkserver.calibration.service --port COM3 \
    --cors \
    --cors-host 120.253.239.161 \
    --cors-port 8002 \
    --mountpoint RTCM33_GRCEJ \
    --cors-user <账号> --cors-password <密码> \
    --duration short \
    --fix-timeout 300 \
    --remark "现场标定"
```

参数口径：

| 参数 | 建议 | 说明 |
| --- | --- | --- |
| `--cors-port` | `8002`（WGS84）/ `8001`（CGCS2000） | 与基准站坐标系保持一致，别混 |
| `--mountpoint` | 五星十六频优先 | 星多固定快；三星八频作为备选 |
| `--duration` | 首跑 `short`，正式跑 `standard` | `short=180s` 够验证链路 |
| `--fix-timeout` | 天线有遮挡时给 `300` | 默认 180 s；巷道环境首跑建议 300 |

### 6.3 正常输出长什么样

```
[10:36:13] ROVER_SETUP   已切换为流动站模式（原 base → rover）
[10:36:20] NMEA_SETUP    已配置 3 条 NMEA 输出（配置前 GGA=1）
[10:36:23] GGA_WAIT      已收到 3 条有效 GGA（原生透传）
[10:36:28] CORS_CONNECT  已连接 RTCM33_GRCEJ，等待差分
[10:36:35] FIX_WAIT      等待固定解（质量=5，卫星=21，PDOP=1.4），38/300 秒
[10:37:15] COLLECT       采集 12 点（剔除 0），3D σ 0.031m，15/180 秒（8%）
[10:38:31] COLLECT       采集 69 点（剔除 2），3D σ 0.008m，91/180 秒（50%）
[10:38:36] COLLECT       连续 60 秒 σ≤2cm，判定收敛
[10:38:37] CORS_CLOSE    已断开 CORS
[10:38:52] VERIFY_RTCM   1005 播发坐标与写入值一致（最大轴差 0.10 mm）
[10:38:53] DONE          标定完成
```

GGA 位置冻结版（2026-11-02 11:50，同一根天线，巷道遮挡）：

```
11:50:40 ROVER_SETUP   已从 base 切为流动站（回读确认）
11:50:41 NMEA_SETUP    已配置 3 条 NMEA 输出（配置前 GGA=0）
11:50:47 GGA_WAIT      已收到 3 条有效 GGA（原生透传）
11:51:54 FIX_WAIT      质量 4，连续固定 30/30 秒 → 进入采集（第 72 秒拿到固定解）
11:53:54 COLLECT       采集完成：95 点，全段 3D σ 0.0222 m，末段 σ 0.0197 m（已收敛）
11:53:55 WRITE_COORD   写入并回读通过（最大轴差 0.000 mm）
11:53:55 BASE_SETUP    已切换为 base（收到应答）
11:53:57 SAVE          已保存，内存态回读一致
11:54:03 VERIFY_MODE   重启后模式 base、坐标一致（NVM 持久化确认通过）
11:54:06 VERIFY_RTCM   1005 播发坐标与写入值一致（最大轴差 0.10 mm）
```

整轮 3 分 39 秒，`gga_forwarded=186`（约 1 Hz，逐条转发）。

### 6.4 失败排查表

| 现象 | 最可能原因 | 怎么办 |
| --- | --- | --- |
| `ROVER_SETUP` 失败 | 串口被主程序占用 / 模块不回 PQTM | 先停主程序；`--seconds 12` 确认串口能通 |
| `GGA_WAIT` 超时 | 模块在 base 模式（NMEA 被禁用）| 检查 ROVER_SETUP 是否真的切成功 |
| `CORS_CONNECT` 401 | 账号/密码错 | 用 `--list-mountpoints` 验证同一套凭据 |
| `CORS_CONNECT` 404 | 挂载点名字错 | 挂载点大小写敏感，从 SOURCETABLE 里复制 |
| 连上但无 RTCM | VRS 还没收到有效 GGA | 看日志里是否有 GGA 上报计数；确认模块在 rover |
| `FIX_WAIT` 停在质量=5 | 浮动解，模糊度没固定 | **加大 `--fix-timeout`**；确认 `GGA_FREEZE_POSITION=True`（VRS 跟着浮动位置挪虚拟站会让整周反复作废）；别在等待期插串口查询 |
| `FIX_WAIT` 停在质量=1 | 完全没收到差分 | 模块不在 rover / RTCM 没注入 / 挂载点不匹配 |
| 采集 σ 一直 > 2 cm | 多径严重 / 天线在动 | 延长时长；确认天线没被碰；看 `rejected` 是否很大 |
| 标定成功但基站只播 1074 | MSM 星座没逐条打开 | 看 `result.cors_msm.opened`；必须 5 项齐全 |

---

## 7. 与方案原设计的差异（都已改代码，此处备案）

| 项 | 方案原文 | 实际实现 | 原因 |
| --- | --- | --- | --- |
| GGA 节奏 | "严格 1 Hz" | **逐条转发 + 3 s 保底** | 1 Hz 节流卡 1 Hz 信号会抖动；逐条转发等价于 1 Hz 且不漏 |
| GGA 发送顺序 | 未规定 | **先 drain 再 pump** | 反了会永久上报上一轮的位置 |
| GGA 内容 | "透传模块 GGA" | **静态标定冻结经纬度**（时间/质量/星数实时） | VRS 跟着浮动位置挪虚拟站 → 整周反复作废，永远停在浮动解 |
| FIX_WAIT 期模式复核 | 未提及 | **只在 COLLECT 期复核** | 真机实测：等待期插查询会让 120 s 仍停在浮动解 |
| 失败回滚 | "安全关闭" | **NVM + NMEA 速率都回滚并回读校验** | 真机事故：回放报成功但 1005=0 |
| 收敛判据输入 | "三轴 σ" | **原始样本 σ**（不是滤波输出） | 滤波输出 σ 恒偏小，等于放宽门限 |

---

## 8. 验收判据

| 项 | 判据 |
| --- | --- |
| ① 自动切流动站 | `ROVER_SETUP` 一次通过，回读确认 rover |
| ② 自动连 CORS | `CORS_CONNECT` 成功，25 秒内收到 >10 KB RTCM |
| ③ 进固定解并采集 | `COLLECT` 有样本，且 `hdop_fallback=false`（走 PDOP 正路） |
| ④ 收敛曲线正常 | σ 单调下降，`duration_seconds` ≥ 60（不可能更短） |
| ⑤ 写坐标切基站 | `VERIFY_MODE` 坐标回读一致，`NVM=saved` |
| ⑥ 1005 播发 | 抓到 1005 帧，轴差 ≤ 5 cm（一致性，见 §9） |

---

## 9. 一个必须说清楚的边界：1005 不证明精度

`VERIFY_RTCM` 比较的是：

```
broadcast(模块正在播发的 1005 坐标)  vs  plan.arp(本次刚写进去的坐标)
```

这两个值**同源**，所以偏差恒等于 1005 的量化误差（≤ 0.1 mm）。
它能证明的是"写入 → NVM → 重启 → 播发"这条链路生效了，
**不能证明坐标是对的**——你写错 1 米，它就播错 1 米，偏差照样是 0.00 mm。

真正的绝对精度验证方式（本阶段未做，属现场施工质量环节）：

1. **交叉解算**：用另一个已知点的接收机同时观测，做基线解算比对；
2. **换基准源**：同一位置用另一家 CORS / 另一挂载点重标，两次结果互差；
3. **已知点检核**：天线架在已知坐标的控制点上，直接比差；
4. **静态 PPP**：观测 2 小时以上做 PPP，作为独立第三方真值。

### 9.1 重复性实测（同一天、同一根天线、同一挂载点）

| 记录 | 时间 | 样本 | 全段 σ | ECEF（X / Y / Z） | 与 #7 的距离 |
| --- | --- | --- | --- | --- | --- |
| #5 | 10:38 | 69 | 7.6 mm | -2001277.4679 / 5416696.9897 / 2699549.3692 | **9 mm** |
| #6 | 11:54 | 95 | 22.2 mm | -2001277.4485 / 5416696.9475 / 2699549.3736 | **4.6 cm** |
| #7 | 11:59 | 152 | 37.6 mm | -2001277.4649 / 5416696.9845 / 2699549.3599 | — |

结论很直白：**这套环境（巷道多径、天线有遮挡）的重复性在 1~5 cm 量级**，
不是毫米级。#5 与 #7 互差 9 mm，#6 偏了 4.6 cm——而 #6 恰恰是"收敛判得最早"的那次
（95 点 / 101 秒）。所以：

- **早收敛不等于准**：`σ` 只是"这段时间抖得小"，不保证锚在真值上；
- 记录里的 `accuracy` 存的是**全段 σ**（不是判定用的末段 σ），是保守口径；
- 要毫米级结论，得走上面第 1~4 条独立检核，不能拿 `VERIFY_RTCM` 当精度证明。

---

## 10. 代码索引

| 功能 | 文件 | 关键符号 |
| --- | --- | --- |
| NTRIP 客户端 | `rtkserver/calibration/cors.py` | `CorsClient.connect/pump/close` |
| 错误分类 | 同上 | `ERR_DNS/ERR_NETWORK/ERR_AUTH/ERR_MOUNTPOINT/ERR_SOURCETABLE/ERR_NO_DATA` |
| 挂载点读取 | 同上 | `read_mountpoints`（复用 `sources/ntrip`） |
| GGA 管理 | 同上 | `GgaManager.source/force_source` |
| 链路状态机 | `rtkserver/calibration/writer.py` | `_step_cors_collect` 及各 `_step_cors_*` |
| 采集滤波 | 同上 | `_step_cors_fix_and_collect`、`_cors_converged`、`_rolling_sigma` |
| 模式门禁 | 同上 | `_ensure_rover`、`MODE_RECHECK_SECONDS` |
| 回滚 | 同上 | `_revert`（NVM + NMEA 速率 + 回读校验） |
| 只测不写 | 同上 | `build_cors(method='external_rover')`、`run()` 的 `external_rover` 分支（只 collect + inject 即返回） |
| RTCM 选配 | 同上 | `WritePlan.coord_msg` / `.msm`、`_check_coord_msg`、`_check_msm`、`_step_setup_messages`、`_step_verify_rtcm` |
| Survey-in 续标 | 同上 | `_step_svin_resume`、`WritePlan.resume_svin`、`SVIN_RESUME_WAIT` |
| 未知模块驱动 | `rtkserver/calibration/drivers/passive.py` | `PassiveDriver`（`PASSIVE=True`，17 个写方法全部拒绝；刻意不进 `_REGISTRY`） |
| 1005/1006 编解码 | `rtkserver/calibration/proto/rtcm.py` | `decode_station_coord`、`_build_1005_frame(antenna_height=...)`、`MSG_1006` |
| 服务/CLI | `rtkserver/calibration/service.py` | `start_cors(module=, measure_only=, coord_msg=, msm=)`、`--measure-only`、`--coord-msg` |
| Web API | `rtkserver/web/api_calibration.py` | `SVIN_PROFILES`（从 writer 取）、`_msm_value`、`_ant_enu`、`_snapshot_payload['method']` |
| 天线偏移 | `rtkserver/calibration/geo.py` | `apc_to_arp`、`arp_to_apc`、`enu_to_ecef`；流程侧 `writer._apply_antenna_offset` |
| 仅内存切模式 | `rtkserver/calibration/drivers/base.py` | `set_mode_memory_only`（默认拒绝；LG290P 用 `persist=False, restart=False` 实现） |
| CORS 抖动宽容 | `rtkserver/calibration/writer.py` | `CORS_NET_JITTER_TOLERANCE_SEC`、`_cors_reconnect` 回报断连时长 |
| 冻结日志/告警 | `rtkserver/calibration/cors.py` | `LOG_FREEZE_ACTIVE`、`LOG_GGA_GENERATED`、`note_freeze_active`、`freeze_active` |
| 自测 | — | `python -m rtkserver.calibration.cors`（12 项）、`... writer`（20 项）、`... proto.rtcm`（9 项，含 1006）、`... drivers.passive`（4 项） |
| 启动钩子 | `rtkserver/calibration/service.py` | `on_startup()`（幂等，不动硬件）、`mark_interrupted()`、`interrupted_notice()`；挂在 `web/api_calibration.py: register(app)` |
| 链路活性 | `rtkserver/calibration/drivers/base.py` | `Channel.last_rx_at/rx_bytes/link_error`、`silent_seconds()`、`link_lost(limit)`；流程侧 `writer.LINK_SILENCE_LIMIT`、`_assert_link_alive` |
| 回读重试 | 同上 / `drivers/quectel_lg290p.py` | `VERIFY_RETRY`、`apply_and_verify()`、`_reapply()`、`_check_mode/_check_fixed/_check_svin`；流程侧 `writer._svin_config_verified` |
| 升级兼容 | `rtkserver/upgrade/runner.py` | `KEEP_NAMES`、`_copy_calibration_db`、`_restore_calibration_db`、`migrate_calibration_db`、`_calibration_db_rows` |
| 清库工具 | `reset_config.py` | `--purge-calibration`、`purge_calibration()`、`PURGE_WORD` |
| 契约测试 | `tests/test_contract.py` | `TestCalibrationScenarios`（7 条，锁只测不写 / 报文选配 / 续标顺序 / 1006）、`TestCalibrationAppendix`（17 条，锁附录 A~J）、`TestCalibrationStage7`（22 条，锁 §14 兜底 / 启动钩子 / 升级卸载 / §15.1 四条证明） |

## 11. 补充附录 A~J 落地对照

评审版补充材料（附录A~J）逐条对照如下。"已落地"指代码里有实现且有测试钉住；
"仅架构"指链路能表达、但缺驱动/硬件实例。

| 附录 | 要求 | 落地位置 | 状态 |
| --- | --- | --- | --- |
| A 软件合成 GGA | RMC/GLL → GGA，质量位固定 1，复用冻结，`[GGA-GENERATED]` 标记 | `cors.py: _build_software_gga` / `_current_payload` / `_apply_freeze` | 已落地。**这里修掉了一个真 bug**：早前合成 GGA 明确跳过冻结（注释写"单点解冻结没意义"），但冻结的目的是稳住 VRS，与 GGA 来源无关；单点解每秒跳米级，VRS 跟着跳，模糊度照样解不出来 |
| A.4 数据流隔离 | 上报用冻结值，本地采样用原始值，合成 GGA 绝不进串口 | `writer.py: _step_cors_fix_and_collect` 采样读 `channel.cache['gga']`；`_apply_freeze` 不回写 cache | 已落地（契约测试 `test_freeze_survives_without_native_gga_and_local_sample_stays_raw`） |
| B Survey-in 全流程 | 备份 → base → SVIN 配置 → 监控 Valid → 报文 → 保存 → 重启 → 校验 → 落库 | `writer.py: run()` 的 `survey_in` 分支、`_step_svin_resume/cfg/wait` | 已落地。与附录的差异：本实现把"收敛均值"直接当 ARP 写入，**不扣天线偏移**——模块自己算的就是它自己的参考点，上位机再扣一次等于重复计算 |
| C 纯基准站模块 | 禁止任何切 rover 命令；选 CORS 直接拦截 | `writer.py: _step_cors_rover_setup` 开头的 `dual_mode` 判定 | **仅架构**：拦截逻辑与文案已就位，但注册表里目前只有 LG290P（双模），没有 `base_only` 驱动实例。要真正跑通需要一块这类模块的命令集 |
| D external_rover 仅内存切模式 | base 态临时切 rover、不 SAVEPAR、结束恢复；不支持就失败 | `writer.py: _step_cors_rover_setup`、`_restore_memory_mode`；`base.py: set_mode_memory_only` | 已落地。**这推翻了上一轮的实现**：上一轮遇到 base 是硬拒绝，本轮按附录D 改成"仅内存临时切换 + 收尾恢复"，恢复放在 `finally`，**成功/失败/用户中止三条路径都覆盖** |
| E APC→ARP | `ARP = APC − ENU→ECEF(Δ)`，日志三行，库里两套值 | `geo.py: apc_to_arp`；`writer.py: _apply_antenna_offset` | 已落地，并修掉一个真 bug：CORS 路径此前把收敛值直接当 ARP 写（`ant_enu` 硬编码 0），有偏移时整站会差一个天线高 |
| E.5 历史坐标复用 | 取库里的 ARP 直接写，不重复换算 | `writer.py: build(arp=...)`；前端 `payload.arp` | 已落地。前端此前把 `ecef_*` 当 APC 提交，一旦界面上能填偏移就会**扣两次** |
| F 冻结告警 | `[GGA_FREEZE_ACTIVE]` 循环日志、超 100 m 解冻按 WARNING、前端常驻提示 | `cors.py: note_freeze_active` / `_apply_freeze`；`calibration.js: renderMetrics` | 已落地。循环日志按 1 s 限流（0.2 s 一圈按圈打五分钟是 1500 行，会把有用信息淹掉） |
| G CORS 抖动宽容 | 断连 ≤2 s 不清空固定解计时器 | `writer.py: CORS_NET_JITTER_TOLERANCE_SEC`；`cors.py: settle_disconnect()` 回报时长 | 已落地 |
| H UI 文案 | 禁止"标定坐标验证正确"类措辞；端口坐标系风险常驻 | `service.py: _write_advice _summarize_write`；`calibration.html` + `i18n.js` | 已落地（契约测试 `test_no_absolute_accuracy_wording`） |
| I 数据库字段 | APC/ARP、冻结标记、原始/滤波 σ、SVIN、挂载点 | `store.py: _DDL_V3`（schema v3，增量迁移） | 已落地。`svin_*` 在 CORS 模式写 NULL 而不是 0——补 0 会被读成"跑过 Survey-in、观测 0 次" |
| J 契约测试 | 8 组要点 | `tests/test_contract.py: TestCalibrationAppendix`（17 条） | 已落地 |

### 11.1 两个必须说清楚的取舍

1. **Survey-in 不扣天线偏移。** 附录E 把 APC/ARP 换算写成通用规则，但 Survey-in 的均值是模块
   按它自己的天线配置算出来的；上位机再减一次 `ant_delta_u`，整站就低一个天线高。
   本实现只在 CORS / manual 两条路径做换算，Survey-in 原样写入，界面上也把这一栏隐藏掉。
2. **`ARP` 与 `APC` 都存，但 `ecef_*` 的含义没变。** 从 v1 起 `ecef_*` 就表示"实际写进模块的 ARP"，
   v3 只是把同一组数再显式存一份到 `arp_*`。这样老代码、老库、老页面的语义都不变，
   而"播发的是哪个值 / 写进去的是哪个值"从此能分开查。

### 11.2 已知边界（没做，不是因为没看到）

- **LC29HBS 驱动**：命令集在 `docs/quectel-lg290p-calibration-commands.zh.md` §8.2 有记录，
  但它与 LG290P 是两套指令（`$PAIR432/434` 配 RTCM、`PQTMCFGSVIN` 只有 7 个参数、
  错误码没有 3）。没有硬件实测前不猜帧格式——猜错的后果是标定"成功"但坐标根本没写进去。
- **真机验证**：附录A/D/G 三条都改了行为（合成 GGA 走冻结、内存切模式、抖动不清零），
  全部只在假模块上验过，需要在真机复验。

## 12. 阶段 7 落地对照（兜底、启动钩子、升级兼容）

方案 §14 的 32 种异常逐条过了一遍，补齐了原先缺的几处；§15.1 的四条"不干扰主程序"
证明也补了行为级用例，`tests/test_contract.py: TestCalibrationStage7` 共 22 条。

| 要求 | 落地位置 | 说明 |
| --- | --- | --- |
| §14 #1 / #28 链路静默 | `drivers/base.py: Channel.last_rx_at/rx_bytes/link_error`、`silent_seconds()`、`link_lost()`；`writer.py: _assert_link_alive` | 只用来判"**曾经有数据、现在突然没有**"。从未收到字节不判死（基准站可能本来不输出 NMEA），交给命令超时兜；收到过又静默 3 秒 → 立刻失败，不拿断线前的位置继续算 |
| §14 #3 / #25 波特率不对 | `service.py: DETECT_BAUD_CANDIDATES`、`_scan_bauds` | 顺序 460800 → 230400 → 921600 → 38400 → 9600，**只在用户没显式指定波特率时**启用；枚举失败必须把波特率恢复原值再交回通道，成功了同步 `lease.baud` |
| §14 #2 / #4 / #25 识别失败 | `service.py: _detect_module`、`_detect_failure_text` | 三种病因分开给动作：有字节无应答 → 流控 RTS/CTS 与 TX/RX；认得出别的型号 → 直接报"实际检测到 X"并提示改选；无字节/全噪声 → 供电/接线/波特率 |
| §14 #6 回读不一致 | `drivers/base.py: VERIFY_RETRY=1`、`apply_and_verify`；`writer.py: _svin_config_verified` | 重发**一次**配置再回读，两次都不符才失败；重试阶段不再重启（要修的是 RAM 里的值，重启帮不上）。`verify_attempts` 随记录落库，事后能看出"这条参数是靠重试才生效的" |
| §14 #14 采集不收敛 | `writer.py: WritePlan.on_slow` | `adopt`（默认，采用当前均值 + 记录标注未收敛）/ `fail`（不写任何坐标直接失败）。默认值必须可解释：现场信号差一点就什么都拿不到是更糟的结果 |
| §14 #20 / #30 进程被杀 | `service.py: on_startup`（挂在 `api_calibration.register()` 里） | 启动时读残留租约：非终态 → 租约落 `INTERRUPTED` 终态（`interrupted_stage` 保留原阶段）+ `calibration_events` 落事件 + 页面常驻横幅。**钩子幂等且不动硬件**（此刻串口还没开）；没有租约也没有库时不会凭空造一个空库 |
| §12.1.1 升级 / 卸载 | `upgrade/runner.py` | `KEEP_NAMES` 加 `calibration.db`(-wal/-shm)/`.calibration`；备份连 `-wal`/`-shm` 一起拷（checkpoint 不保证每条事务）；回滚先删旧 `-wal/-shm` 再还原；`migrate_calibration_db()` 放在"文件已换、服务未起"的窗口里按 `PRAGMA user_version` 增量迁移；卸载备份到 `/root/2RTKserver-calibration-<时间戳>.db` 并打印记录条数 |
| 复位工具 | `reset_config.py` | 默认**不动** `calibration.db`；`--purge-calibration` 才清，且要输 `DELETE` 二次确认，清前逐文件列字节数并说明后果 |
| §15.1 契约 | `TestCalibrationStage7` | 单向依赖（启动钩子落点不变）、流程层无命令字符串、热路径零改动（按 AST 判调用，不按字符串，免得把注释判成违规）、租约释放必达（行为级：让 `acquire()` 抛异常，断言 `release()` 恰好一次且 `stage=FAILED`） |

两条容易写错的：
1. **启动钩子不能动硬件。** 它跑在 Web 起来之后、`run_once()` 之前，此时串口还没打开，
   盲发任何命令都会写坏模块。契约测试直接扫函数体里有没有 `set_mode`/`reboot`/`channel.open`。
2. **CORS 数据不进分发。** 这条用 AST 遍历 `ast.Call` 判，拦 `distribute()` /
   `upload_via_serial()` / `rtcm_queue.put()`。按字符串判会误伤文档里那句
   "绝不进全局 distribute"——而那句话恰恰是要保留的说明。
