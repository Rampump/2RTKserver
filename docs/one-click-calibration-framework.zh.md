# 一键标定：框架与流程

> 版本：对应代码基线（`VERSION = 3.0.0` 之后，P0/P1 修复已合入）
> 范围：**框架与流程**，讲清"点一下开始按钮之后，程序到底按什么顺序做什么、在哪一层做、失败了回哪去"。
> 命令细节以 `docs/quectel-lg290p-calibration-commands.zh.md` 为准；设计取舍以 `docs/auto-calibration-design.zh.md` 为准；
> 四种方式 × 三个模块的逐行对照见 `docs/calibration-four-methods-per-module.zh.md`。
> 本文是那三份文档的**上游总览**：先看这里建立骨架，再按需下钻。

---

## 0. 一句话框架

**一条任务、一个状态机、一张串口租约、四张流程分支、一段公共尾链。**

```
用户点「开始」
   ↓  POST /api/calibration/start   {method: cors|survey_in|manual|external_rover, ...}
[前端]  能力位算锁 → 参数自检 → 展示步骤预览
   ↓
[api 层]  method 映射成三个入口之一 → service.start_*
[service] 能力门拦截 → 构造 WritePlan → _start_task 起工作线程
   ↓
[worker] PREPARE → HOLD(抢串口租约) → OPEN → DETECT(识别模块) → BACKUP
   ↓
[writer] 按 plan.method 四分支之一执行"测量/配置"段
   ↓
[writer] 公共尾链 MSG_SETUP → SAVE → RESTART → VERIFY_MODE → VERIFY_RTCM → INJECT
   ↓
[finally] 任何路径都归还租约 → 数据面恢复
```

不变式（**任何改动都不能破坏的四条**）：

| # | 不变式 | 破坏后的症状 |
|---|---|---|
| 1 | 串口同一时刻只有一个持有者 | 两个 reader 撕裂 RTCM 流，本地 Caster 与上传同时断流 |
| 2 | 租约在 `finally` 里归还 | 主程序永久卡在"数据面不启动"，只能重启进程 |
| 3 | 能力判断在**下发第一条命令之前** | 占住串口十几秒后才发现这块模块不支持 |
| 4 | 失败必有备份、必能还原 | 模块停在"切了基站但坐标是旧的"的中间态 |

---

## 1. 分层：每一层只负责一件事

```
┌──────────────────────────────────────────────────────────────────┐
│ 前端  templates/calibration.html + static/calibration.js         │
│  能力位 → 四张卡开关；参数自检；步骤预览；1s 轮询 status          │
└───────────────────────────┬──────────────────────────────────────┘
                            │ HTTP JSON（全部 @login_required）
┌───────────────────────────▼──────────────────────────────────────┐
│ 接口  rtkserver/web/api_calibration.py                           │
│  只做三件事：method 合法性 → 映射到 service 入口 → 翻译错误码      │
└───────────────────────────┬──────────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────────┐
│ 服务  rtkserver/calibration/service.py                           │
│  能力门（拒绝不可行的组合） → WritePlan.build（参数归一化）        │
│  → _start_task（单实例 + 线程 + 状态落盘） → _worker（租约与识别） │
└───────────────────────────┬──────────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────────┐
│ 流程  rtkserver/calibration/writer.py  CoordinateWriter           │
│  按 plan.method 四分支执行测量/配置段 + 公共尾链                   │
│  只认"给我一个 plan 和一份备份"，不知道 HTTP 也不知道页面          │
└───────────────────────────┬──────────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────────┐
│ 驱动  rtkserver/calibration/drivers/*.py                          │
│  能力表 CAPS + 命令表 CMD + 接口覆写；零流程、零型号分支            │
└──────────────────────────────────────────────────────────────────┘
```

**职责边界的判据**：如果一段逻辑需要知道"用户点了什么"，它属于 service；如果一段逻辑需要知道"这块模块的命令长什么样"，它属于驱动；其余全部属于 writer。

---

## 2. 前端：四张卡 + 三行提示 + 一个预览

### 2.1 方式选择

| 卡片 | `method` | 一句话 | 是否需要写 NVM |
|---|---|---|---|
| CORS 差分标定 | `cors` | 连 NTRIP 拿厘米级坐标，再写进模块 | ✅ |
| Survey-in 自收敛 | `survey_in` | 让模块自己跑收敛，亚米级 | ✅ |
| 已知坐标写入 | `manual` | 坐标来自用户或历史记录，直接写 | ✅ |
| 流动站模式测量 | `external_rover` | 只测不写，产出坐标供他处使用 | ❌ |

四张卡的可用性由 `computeLocks()` 按能力位实时计算，**与后端三处能力门逐条对齐**（见 §4.1）。不可用的卡片置灰并给出原因文案，而不是点下去之后报错。

### 2.2 步骤预览

`planSteps()` 在开跑之前就把**本次将要经过的阶段**列出来，其中"会真正改动模块 NVM"的步骤（`WRITE_COORD` / `SAVE` / `VERIFY_RTCM` / `COLLECT`）用强调色标出：

| 方式 | 预览阶段序列 |
|---|---|
| `manual` | `PREPARE OPEN DETECT BACKUP WRITE_COORD BASE_SETUP MSG_SETUP SAVE RESTART VERIFY_MODE VERIFY_RTCM INJECT` |
| `cors`（复用历史坐标） | 同上，额外多一条**划掉**的 `COLLECT` —— 如实画出"本次跳过测量" |
| `cors`（真实测量） | 前置 `ROVER_SETUP NMEA_SETUP GGA_WAIT CORS_CONNECT FIX_WAIT COLLECT CORS_CLOSE`，再接上面那条尾链 |
| `survey_in` | 前置 `SVIN_CFG SVIN_WAIT`，接 `WRITE_COORD … INJECT` |
| `external_rover` | 只有 `… ROVER_SETUP NMEA_SETUP GGA_WAIT CORS_CONNECT FIX_WAIT COLLECT CORS_CLOSE RESTART INJECT`，**没有** `WRITE_COORD/BASE_SETUP/MSG_SETUP/SAVE/VERIFY_*` |

> 预览不是装饰：它是用户与程序之间唯一的"事前契约"。预览里出现的阶段，`status` 里一定会有；预览里没有的，后端一定不会做。

### 2.3 参数自检

`validate(payload)` 在按钮按下时就拦掉明显错误（缺串口、坐标越界、CORS 缺字段、复用坐标却没选记录）。后端 `WritePlan.build` 会再判一次——**两次校验不是冗余**：前端为了体验，后端为了防绕过。

---

## 3. 接口层：三种方式 → 三个入口

`api_calibration.py` 里 `method` 到服务入口的映射只有一张表，是整条链路唯一的分流点：

```python
if method in ('cors', 'external_rover', 'measure'):
    calib.start_cors(..., measure_only=(method != 'cors'), reuse_arp=reuse_arp)
elif method in ('survey_in', 'svin'):
    calib.start_survey_in(..., profile=body.get('svin_profile') or 'standard', ...)
elif method in ('manual', 'known'):
    calib.start_write(..., method='manual', ...)
else:
    return 400, f'未知的标定方式：{method}'
```

要点：

- **四张卡 → 三个入口**。`cors` 与 `external_rover` 共用 `start_cors`，区别只在 `measure_only` 一个布尔。
- `measure` / `svin` / `known` 是别名，保证老页面、老脚本不会因为改了词就 400。
- `reuse_arp` **只对 `cors` 生效**（`method == 'cors'` 才取），避免"只测不写"被误传复用坐标。

### 3.1 接口清单

| 路由 | 方法 | 作用 |
|---|---|---|
| `/calibrate` | GET | 页面 |
| `/api/calibration/options` | GET | 下发模块清单 + 能力位 + 档位表 |
| `/api/calibration/status` | GET | 任务快照（前端 1s 轮询） |
| `/api/calibration/detect` | POST | 只读识别（不写任何配置） |
| `/api/calibration/start` | POST | 启动一次标定 |
| `/api/calibration/abort` | POST | 请求取消（软中断，见 §7） |
| `/api/calibration/apply-as-source` | POST | 把标定端口设为数据源并应用 |
| `/api/calibration/history` | GET | 历史坐标列表 |
| 还原 | POST `start(action=restore)` | 读备份回写，见 §8 |

---

## 4. 服务层：能力门是所有拒绝发生的地方

### 4.1 三套判据，各管一段

| 入口 | 判据 | 拒绝文案示例 |
|---|---|---|
| `start_write` | `_has_command_set()` + `caps['fixed']` | 「不支持写入固定坐标，请改用 Survey-in 或 CORS」 |
| `start_survey_in` | `_has_command_set()` + `caps['svin']` | 「不支持 Survey-in 自收敛」 |
| `start_cors(!measure_only)` | 复用：`fixed`+`set_mode`；新建：再加 `dual_mode` | 「不支持切换工作模式」 |
| `start_cors(measure_only)` | `_has_command_set()` + `not caps['dual_mode']` 时拒绝 | 「是单基准站模块，没有流动站模式」 |

`_has_command_set(info)` 是统一的"这块驱动到底能不能下发东西"判据：**命令表非空即通过**；命令表为空时（如 u-blox 全走 UBX 二进制、`CMD = {}`），只要驱动对 `fixed/svin/set_mode/dual_mode` 至少声明过一个明确布尔值，即认为它能写。这样 F9P 不再被"命令数为 0"误伤，而空壳驱动（能力位全是 `undefined`）仍然会被拦。

**未指定型号时一律放行**：`_module_caps()` 返回 `None` 表示"还不知道是哪块模块"，真正的判断留给 writer 拿到驱动之后。

### 4.2 参数归一化：`WritePlan`

`WritePlan` 是"本次要做什么"的唯一真相，所有校验都在这里做完，流程层不再重复判：

| 字段 | 含义 | 谁决定 |
|---|---|---|
| `method` | 四种方式之一 | 接口层 |
| `mode` | 目标模式（base/rover） | 方式固定 |
| `svin_mode` | `fixed` / `survey_in` / `disabled` 三态 | 方式 + 参数 |
| `apc` / `arp` | 采集坐标 / 实际写入坐标 | 用户输入或采集结果 |
| `accuracy_grade` | `centimeter` / `decimeter` / `sub_meter` | 采集结果 |
| `msm` / `coord_msg` / `rates` / `offsets` | 报文配置 | 档位 + 驱动能力 |
| `cors` | NTRIP 连接参数 | 用户输入 |
| `resume_svin` / `on_slow` | 续标 / 不收敛时怎么办 | 用户或默认值 |

`plan.arp` 是"**实际写进模块的那组数**"。本产品的场景是"天线固定 → 测出该点 → 钉死"，所以不做 APC→ARP 相位中心换算：现场没有对中杆，多一层偏移只会因为方向填错而把整站静默抬降一个天线高。

### 4.3 单实例与状态落盘

`_start_task()` 是三个入口共用的开机流程：

1. 已有任务在跑 → 直接返回「已有标定任务在进行」，**不排队、不抢占**；
2. 置初始状态（`stage=PREPARE`、进度、`task_id`）；
3. 起一条 daemon 线程 `calib-worker`；
4. 状态落盘到 `state.json`（断电重启后页面能看到"上次被中断"，见 §7.2）。

串口/波特率的解析也在这一层：留空 → 落该型号的出厂默认档（LG290P 460800 / LC29H(BS) 115200），并标记 `baud_explicit=False`，识别失败时允许逐档重试；用户手填 → 标 `True`，**不允许被悄悄改掉**。

---

## 5. 工作线程：前五步与模块无关

`_worker()` 是所有方式的公共前奏，**这一段与标定方式无关**：

| 阶段 | 做什么 | 失败语义 |
|---|---|---|
| `PREPARE` | 校验参数（端口、波特率已在上层做完） | `TaskFailed` |
| `HOLD` | **抢串口租约**：请求数据面让位，等 `calibration_hold` 生效 | 超时 → 失败并告知"数据源未让位" |
| `OPEN` | 独占打开串口、清残帧 | 打不开 → 失败 |
| `DETECT` | 识别模块（指定型号只问那一个；未指定才全量探测，含波特率逐档重试） | 识别不到 → 只有 `external_rover` 能继续（降级为 `PassiveDriver`，只读 NMEA） |
| `BACKUP` | 读模块身份（SN/固件/UART）+ 备份标定前配置 | 读不到身份仍继续，但记录里标注"缺 SN"；备份失败则终止 |

三个细节：

- **`DETECT` 的降级只给 `external_rover`**：它不需要命令集，只要能读到模块自己吐的 NMEA 就够了。其余三种方式没有命令就写不了，直接失败。
- **备份不是可选项**：`_run_write` 里备份在 writer 之前完成，因为备份要用到 `lease.port` 与 `task_id`（落盘文件要能对上任务）。`external_rover` 例外——它不写任何东西，备份只是浪费时间。
- **只读诊断（diagnose）走同一条线程**，只是 `action` 不同，不进 writer。

---

## 6. writer：四分支 + 公共尾链

### 6.1 分支判定

`CoordinateWriter.run()` 里只有一条 `if/elif` 链，判定字段是 `plan.method`：

```python
if plan.method == 'survey_in':
    if not self._step_svin_resume(plan):     # 断点续标命中就跳过三件事
        self._step_switch_mode(plan)         # 先切基站（SVINSTATUS 只在基站输出）
        self._step_svin_cfg(plan)            # 配收敛参数 + 打开状态输出
        self._step_svin_wait(plan)           # 等收敛（可能几十分钟）
    self._step_write_coord(plan)             # 均值写成固定坐标
elif plan.method == 'cors':
    if 复用历史坐标:  COLLECT(跳过，samples=0)
    else:            self._step_cors_collect(plan)   # 整条测量链
    self._step_write_coord(plan)
    self._step_switch_mode(plan)
elif plan.method == 'external_rover':
    try:     self._step_cors_collect(plan)
    finally: self._restore_memory_mode()     # 内存模式必须切回去
    self.result['verdict'] = 'skipped'
    self._step_inject(plan)
    return self.result                        # 早退，不进尾链
else:                                         # manual / 还原
    if plan.svin_mode == 'fixed': self._step_write_coord(plan)
    else:                          self._step_config_svin(plan)
    self._step_switch_mode(plan)
# ↓ 三条路汇合到这里
self._step_setup_messages(plan)
self._step_save(plan)
self._step_restart(plan)
self._step_verify_mode(plan)
self._step_verify_rtcm(plan)
self._step_inject(plan)
```

三个容易踩的点：

1. **`manual` 这支的代码名是 `else`**，因为还原路径复用同一个分支（靠 `plan.svin_mode` 再分 `fixed` ↔ 配置自收敛）。
2. **自收敛的顺序是 `SWITCH_MODE → SVIN_CFG → SVIN_WAIT → WRITE_COORD`**，不能把 `SVIN_CFG` 提到 `BASE_SETUP` 之前：流动站模式下 `PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1` 直接回 `ERROR,1`。
3. **`external_rover` 是唯一早退的**，`_readonly=True`，`_step_save` 里有硬守卫 `raise WriteFailed('SAVE', ...)`，一个字节都不写 NVM。

### 6.2 公共尾链：八步，每步都有回读判据

| 步 | 阶段名 | 做什么 | 判据 |
|---|---|---|---|
| 1 | `MSG_SETUP` | 报文配置：MSM 档位 → 1005/1006 → 逐星座 MSM → 1033 → 静音 | 回读比对，不符重发一次 |
| 2 | `SAVE` | 存 NVM | 保存应答；`persist_check` 能力位为真时 poll Flash 层 |
| 3 | `RESTART` | 重启生效 | `RESTART_REQUIRED=False` 的型号记 `skipped` 并说明原因 |
| 4 | `VERIFY_MODE` | 确认已是基站 | 回读模式；本次跳过重启时文案单独处理 |
| 5 | `VERIFY_RTCM` | 抓第一帧 1005，比对坐标 | 偏差 < 5cm 告警、> 50cm 判失败并重下发一轮 |
| 6 | `INJECT` | 落库 + 写事件 | 还原不产生新记录 |

**为什么每一步都要回读而不是看 OK 应答**：模块的 `OK` 只表示"命令格式合法"，不代表参数生效。真机上出现过"配置读回完全正常但实际输出没变"的情况。

### 6.3 采集链（`cors` 与 `external_rover` 共用）

```
ROVER_SETUP  →  NMEA_SETUP  →  GGA_WAIT  →  CORS_CONNECT  →  FIX_WAIT  →  COLLECT  →  CORS_CLOSE
```

| 阶段 | 关键动作 | 真机踩过的坑（已固化在代码里） |
|---|---|---|
| `ROVER_SETUP` | 切流动站，持久化 + 重启生效 | 单基准站型号在此被拦下（`base_only`） |
| `NMEA_SETUP` | 开 GGA/GSA，关 GSV/GLL/VTG/RMC/ZDA；10Hz 定位 | 钉 1000ms 会让 GGA 掉到 0.011Hz；全开会掉到 0.15Hz |
| `GGA_WAIT` | 等原生 GGA（8s），再等 RMC/GLL 兜底，最后要求**质量位 > 0** | 冷启动直接进 FIX_WAIT 会白耗 180s |
| `CORS_CONNECT` | NTRIP 建连 + 错误分类 | 200 响应可能只有 `ICY 200 OK\r\n` |
| `FIX_WAIT` | 等连续 30s 固定解；超时后按命中率降级浮动解 | 模式复核**只在 COLLECT 查**，在 FIX_WAIT 查会让 38s 的固定解变成 120s 仍浮动 |
| `COLLECT` | 1Hz 采样 → 3σ 剔除 → 卡尔曼（仅诊断）→ 收敛判定 | 必须先 `_drain_gga()` 再 `pump()`，反了 GGA 只发 1 条 |
| `CORS_CLOSE` | 断 CORS（`finally`） | 断连 ≤ 2s 视为抖动，不清零固定解累计 |

**写入值 = 3σ 剔除后的算术平均**，卡尔曼只作稳定诊断，不参与写入。收敛判据是"最近 60s 三轴 σ ≤ 0.02m"（浮动解 0.15m）。

---

## 7. 状态机与取消

### 7.1 状态与进度

```
IDLE → PREPARE(3) → HOLD(8) → OPEN(14) → DETECT(20) → BACKUP(26)
     → [分支段] → 尾链 → DONE(100)
                  ↘ FAILED / ABORTED / INTERRUPTED
```

分支段的进度点：`ROVER_SETUP 24 / NMEA_SETUP 28 / GGA_WAIT 32 / CORS_CONNECT 36 / FIX_WAIT 40 / COLLECT 60 / CORS_CLOSE 66`，`SVIN_CFG 30 / SVIN_WAIT 40`，`WRITE_COORD 36 / BASE_SETUP 46 / MSG_SETUP 56 / SAVE 64 / RESTART 72 / VERIFY_MODE 80 / VERIFY_RTCM 88 / INJECT 96`。

### 7.2 取消是**软中断**

`abort` 只置 `self._cancel`，工作线程在**下一个检查点**收尾，不做硬 kill。因为硬 kill 会留下"切了基站但坐标是旧的"中间态。

三个例外要记住：

- `SVIN_WAIT` 里的取消**被吞掉**：用当前均值继续写坐标（已经收敛了几十分钟的均值不该白白扔掉）；只有一次样本都没采到（`obs == 0`）才算失败。
- `TaskEvicted`（租约被强制回收）**不能被吞**：串口已经还给数据面了，再发命令就是抢字节。
- 断电/`kill -9` 之后，`on_startup()` 读 `lease.json` 的中间态，把页面标成 `INTERRUPTED`，并**同时把 lease 改成终态**——否则每次开机都重新报一次，提示会失去意义。

---

## 8. 失败语义与回滚

### 8.1 三条收尾路径

| 触发 | 处理 | NVM 状态 |
|---|---|---|
| `WriteFailed` | `_finish_failure()`：结果标记 + 按需回写备份 | 按已完成的 `SAVE` 如实标注 |
| 用户取消 / 驱动异常 / 心跳丢失 | `_abort_cleanup()`：能回写就回写，再原样抛出 | 同上 |
| 租约已被回收 | **不回写** | `NVM_NOT_WRITTEN` |

### 8.2 备份与还原

- 备份在 writer 之前完成，落在 `.calibration/backup.json`（独立文件，**不进 `settings` 表**——配置是配置，标定结果是资产）。
- 备份里同时存**归一化角色**（`rover`/`base`/`unknown`）和原始回读文本：`usable()`/`describe()` 走角色，回写时用驱动原生格式。
- 还原走 `start_restore` → `WritePlan.from_backup()` → 复用 `manual` 分支，`kind='restore'` 让它**不产生新的历史坐标记录**（否则用户下次选坐标会看到一个自己从没标定过的值）。

---

## 9. 四个确定性要求（落地检查清单）

改任何一处流程，先过这四条：

1. **能力判断必须在下发命令之前**。位置在 `service.start_*`，不是 `writer.run()`。放在 writer 里意味着已经占了串口十几秒。
2. **公共尾链不能有分支**。四种方式的差异只允许出现在 §6.1 那条 `if/elif` 链和采集段里，`MSG_SETUP → INJECT` 这八步对所有方式一视同仁。要加"某型号跳过某步"，用能力位在步骤内部判，不要在链上加 `if`。
3. **驱动里不许出现型号分支**，只允许有 `CAPS` 位、`CMD` 表和接口覆写。`writer.py` 里所有按型号分叉，最终只收敛到 9 个 `_caps()` 使用点。
4. **串口归还必须在 `finally`**。这是整条链路唯一不能靠"小心"保证的事，只能靠结构。

---

## 10. 一页速查

| 我想改… | 改哪里 | 别改哪里 |
|---|---|---|
| 加一种新的标定方式 | `api_calibration.py`（映射）+ `service.start_*`（能力门）+ `writer.run()`（新分支） | 公共尾链 |
| 支持一款新模块 | 新增驱动文件（`CAPS` + `CMD` + 覆写） | `writer.py` / `service.py` |
| 调整固定解准入 | `writer.py` 顶部常量 | 驱动 |
| 改某步的判定文案 | `writer._step_*` 内的 `_record_step` 参数 | 阶段名 |
| 加一个前端提示 | `static/i18n.js` 中英各一条 + 页面 | 后端拒绝文案（要与判据一一对应） |

**阶段名是契约**：`PREPARE / HOLD / OPEN / DETECT / BACKUP / ROVER_SETUP / NMEA_SETUP / GGA_WAIT / CORS_CONNECT / FIX_WAIT / COLLECT / CORS_CLOSE / SVIN_CFG / SVIN_WAIT / WRITE_COORD / BASE_SETUP / MSG_SETUP / SAVE / RESTART / VERIFY_MODE / VERIFY_RTCM / INJECT / OBSERVE / PLAN / DONE / FAILED / ABORTED / INTERRUPTED`。前端事件流、进度条、步骤预览三处都按字符串匹配，改名等于同时改三处。

---

*本文为框架梳理，不含业务代码。所有行号与常量以当前工作区代码为准。*
