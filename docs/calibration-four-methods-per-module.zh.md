# 四种标定方式 × 不同模块的实现与运行流程（代码事实版）

> 本文**只梳理现状，不改任何代码**。
>
> 梳理对象：`cors`（CORS 差分）、`survey_in`（自收敛）、`manual`（已知坐标）、`external_rover`（流动站模式测量 / 只测不写）四种方式，在当前代码里的**入口、能力门、执行链路、按模块的差异**。
>
> 代码基线：`rtkserver/calibration/{service.py, writer.py, drivers/*}` + `rtkserver/web/api_calibration.py` + `static/calibration.js`。
>
> 实测口径（以真机结论为准，本文按此口径组织）：
>
> | 模块 | CORS 差分 | 自收敛 | 已知坐标 | 流动站测量 |
> |---|---|---|---|---|
> | Quectel LG290P | ✅ 通 | ✅ 通 | ✅ 通 | ✅ 通 |
> | Quectel LC29H(BS) | ❌ 不支持 | ✅ 通 | ✅ 通 | ❌ 不支持 |
> | u-blox ZED-F9P | ✅ 通 | ✅ 通 | ✅ 通 | ✅ 通 |
> | Unicore UM 系列 | — 待实测，本文不涉及 — | | | |

---

## 0. 先看结论：四种方式在代码里到底是什么关系

一句话总结当前的架构：**前端按「方式」走四张卡；后端 `service` 做能力门；真正的分流发生在 `writer.py` 的 `run()` 里一条 `if/elif` 链，按 `plan.method` 分四支；每个模块的差异则下沉到驱动层（`CAPS` 能力位 + 各接口覆写）**。

```
前端四张卡                api_calibration.py             service.py                    writer.run()
──────────────────────────────────────────────────────────────────────────────────────────────────
「CORS 差分标定」  method=cors            start_cors(measure_only=False)   method='cors'
「自收敛」        method=survey_in       start_survey_in(...)             method='survey_in'
「已知坐标写入」   method=manual          start_write(...)                 method='manual'
「流动站模式测量」 method=external_rover  start_cors(measure_only=True)    method='external_rover'
```

**注意 `manual` 这一支并不叫 `manual`**：`run()` 里的第四个分支是 `else`，它靠 `plan.svin_mode` 再分一次（`fixed` 走写坐标，`survey_in` 走配收敛——后者是「还原」路径用的）。

四种方式共用两段公共尾链（后文详述）：

- **写链路尾链**：`MSG_SETUP → SAVE → RESTART → VERIFY_MODE → VERIFY_RTCM → INJECT`
- **采集链路**（CORS / 流动站测量共用）：`ROVER_SETUP → NMEA_SETUP → GGA_WAIT → CORS_CONNECT → FIX_WAIT+COLLECT → CORS_CLOSE`

`external_rover` 是唯一一个**跑完采集就 return** 的，永远进不到尾链。

---

## 1. 三种入口与能力门（后端第一道闸）

### 1.1 Web 接口怎么把四种方式映射到三个 `start_*`

`rtkserver/web/api_calibration.py:274-334`：

```python
if method in ('cors', 'external_rover', 'measure'):
    ... calib.start_cors(..., measure_only=(method != 'cors'), reuse_arp=reuse_arp)
elif method in ('survey_in', 'svin'):
    ... calib.start_survey_in(..., profile=body.get('svin_profile') or 'standard', ...)
elif method in ('manual', 'known'):
    ... calib.start_write(..., method='manual', ...)
else:
    return jsonify({'ok': False, 'error': f'未知的标定方式：{method}'}), 400
```

要点：

- **`cors` 与 `external_rover` 共用同一个 `start_cors()`**，靠 `measure_only` 布尔值区分。`'measure'` 是 `external_rover` 的别名。
- `reuse_arp`（复用历史坐标）**只对 `method == 'cors'` 生效**：`reuse_arp = body.get('reuse_arp') if method == 'cors' else None`。因为 `measure_only` 一个字节都不许写，两者互斥。
- `manual` 分支**不补高程默认值**（代码注释明确：补 0 等于替用户断言"天线在椭球高 0 米"）。

### 1.2 三个 `start_*` 各自的能力门

三个入口的能力门**不是一套统一规则**，是各写各的：

| 入口 | 能力门 | 代码位置 |
|---|---|---|
| `start_write`（manual） | `_reject_write_without_fixed(module)` → 先查 `command_count > 0`，再查 `caps['fixed']` | `service.py:359, 1662` |
| `start_survey_in`（自收敛） | `_reject_without_caps(module, 'svin')` → 先查 `command_count > 0`，**只**查 `svin`（**不查 `fixed`**） | `service.py:404` |
| `start_cors(measure_only=False)` | `need = ('fixed','set_mode') if reuse else ('fixed','set_mode','dual_mode')` | `service.py:478-482` |
| `start_cors(measure_only=True)` | 走另一套判据（下详） | `service.py:483-498` |

**`start_survey_in` 为什么不查 `fixed`**——代码注释给了理由：

```python
# 只查 `svin` 不查 `fixed`：走这条路本来就是为了让模块自己收敛出坐标，
# 只要它能跑 SVIN 就能把结果存进 NVM，这两件事在驱动里是同一个能力组。
```

**`measure_only=True` 的独立判据**（`service.py:491-498`）：

```python
info = _module_caps(module)
if info and info['command_count'] > 0 and not info['caps'].get('dual_mode'):
    return False, (f'{info["name"]} 是单基准站模块，没有流动站模式，'
                   f'无法接收 CORS 差分数据测量天线坐标。请改用「Survey-in 自收敛」'
                   f'或「写入已知坐标」；若要用 CORS 测量，请换一块支持流动站模式的模块'
                   f'（如 LG290P），或用「只测坐标」配一块已处于流动站模式的未知模块')
```

这里**不看 `fixed`、不看 `set_mode`**——因为只测不写，既不写坐标也不切模式。只看 `dual_mode`。且**未指定型号时放行**（`info is None`）：未知模块很可能就是一台现成的流动站。

### 1.3 `_module_caps()` 的 `None` 语义

```python
def _module_caps(module):
    key = _clean_module(module)
    if not key:
        return None
    ...
```

`None` = "不知道是哪块模块，放行到运行时再说"。所以两个 `_reject_*` 函数遇到 `None` 一律 `return None`（放行），真正的判断落到 `writer.py` 拿到驱动之后（`_caps()`、`PASSIVE`、`ROVER_SETUP` 里的 `base_only` 检查）。

### 1.4 按实测口径，四种方式在当前模块上的**实际**通行情况

| 方式 | LG290P | LC29H(BS) | ZED-F9P | 拦截发生在哪一层 |
|---|---|---|---|---|
| `manual` | ✅ | ✅ | ✅ | `command_count>0` + `caps.fixed` 全过 |
| `survey_in` | ✅ | ✅ | ✅ | `command_count>0` + `caps.svin` 全过 |
| `cors` | ✅ | ❌ | ✅ | `_reject_without_caps(...,'set_mode')` → LC29H(BS) 报「不支持「切换工作模式」」 |
| `external_rover` | ✅ | ❌ | ✅ | `not caps['dual_mode']` → 报「是单基准站模块，没有流动站模式」 |

**LC29H(BS) 的 `dual_mode=False`、`set_mode=False` 是这两条路被挡住的原因**，两个方式分别命中不同的判据（一个是 `_reject_without_caps` 的能力位拼串，一个是 `measure_only` 分支的专门文案）。

### 1.5 前端是**怎么**跟着锁的（与后端逐条对齐）

`static/calibration.js:345-412` 的 `computeLocks()` 明确写了「逐条对着 `api_calibration.start_*` 写」：

```js
//   start_write     → 先命令集，再 fixed（`_reject_write_without_fixed`）
//   start_survey_in → 先命令集，再**只**看 svin（不查 fixed）
//   start_cors      → 复用分支 fixed+set_mode；正常分支再加 dual_mode
```

```js
function canWrite() {
  var mod = selectedModule();
  if (!mod) return true;                      // 自动识别 → 放开，后端再拦
  return Number(mod.command_count) > 0;
}
```

- `cors`：`!writable` → `caps.fixed === false` → `caps.set_mode !== true` → `!mod.dual_mode`，四级依次判。
- `survey_in`：`!writable` → `caps.svin !== true`（注释明确"**不能**顺手把 fixed 也加上"）。
- `manual`：`!writable` → `caps.fixed === false`。
- `external_rover`：`writable && mod && !mod.dual_mode` → 锁；**未选型号一律放开**。

也就是说：**前端对 LC29H(BS) 会把「CORS 差分标定」和「流动站模式测量」两张卡灰掉，而后端还有第二道同样的判断**。这是双保险，不是重复设计缺陷。

> ⚠️ 已知偏差（本文档只记录，不修）：`canWrite()` 用 `command_count > 0` 作为总闸，而 `ZEDF9PDriver.CMD = {}`（全走 UBX `_valset/_valget`），所以**在网页上选了 F9P，四张卡全灰**。真机跑通 F9P 走的是绕过该 API 的临时脚本（`.flows_f9p.py`）。这条与"前端流程判断没错"的结论冲突点就在这里——**判据用错了字段（命令条数），不是流程顺序错了**。

---

## 2. 方式一：CORS 差分标定（`method='cors'`）

厘米级。核心思想：让模块当**流动站**，把 CORS 的 RTCM 喂进它的串口，由模块自己解出固定解，采 3~10 分钟求平均，然后把它**切成基准站**并把这份坐标写死。

### 2.1 入口 → plan

```python
plan = WritePlan.build_cors(
    host=host, port=cors_port, mountpoint=mountpoint, user=user,
    password=password, duration=seconds,      # CORS_DURATIONS = {'short':180,'standard':300,'long':600}
    msm=msm, antenna=antenna, remark=remark, verify_rtcm=True,
    coord_msg=coord_msg,
    method='external_rover' if measure_only else 'cors',
    on_slow=on_slow, reuse_arp=reuse_arp)
```

`on_slow` 决定「到时长仍未收敛」怎么办：`adopt`（采用当前值并标注）或 `fail`。

### 2.2 完整阶段链

```
[CORS 复用分支 A1]                    [正常分支]
  COLLECT(跳过测量)                     ROVER_SETUP   切流动站
  WRITE_COORD  写坐标                   NMEA_SETUP    配 GGA/GSA + 静音 + FIXRATE + RTK REINIT
  BASE_SETUP   切基站                   GGA_WAIT      等 3 条有效 GGA
      │                                 CORS_CONNECT  连 NTRIP Caster（连上后再确认一次 rover）
      │                                 FIX_WAIT      等连续 30s 固定解（超时 180s 后可降级浮动解）
      │                                 COLLECT       1Hz 采集 + 3σ 剔除 + 卡尔曼 + 收敛判定
      │                                 CORS_CLOSE    断连 + 下行体检
      │                                 WRITE_COORD   写坐标（= 采集均值）
      │                                 BASE_SETUP    切基站
      └──────────────┬────────────────────────┘
                     ▼
   MSG_SETUP → SAVE → RESTART → VERIFY_MODE → VERIFY_RTCM → INJECT
```

`run()` 里对应（`writer.py:720-744`）：

```python
elif plan.method == 'cors':
    if (plan.cors or {}).get('reuse') and plan.arp is not None:
        ... # 记录 cors={'reused': True, 'samples': 0}
    else:
        self._step_cors_collect(plan)
    self._step_write_coord(plan)
    self._step_switch_mode(plan)
# 之后落进公共尾链
self._step_setup_messages(plan); self._step_save(plan); self._step_restart(plan)
self._step_verify_mode(plan);    self._step_verify_rtcm(plan); self._step_inject(plan)
```

### 2.3 `_step_cors_collect` 的七个阶段逐个说明

#### ① `ROVER_SETUP`（`writer.py:1427`）— 第一道硬门禁

先**读**模式再决定切不切：

```python
ok, message, data = self.driver.get_mode()
from_mode = (data or {}).get('mode')
if from_mode == 'rover':
    # 已在流动站：不重复写 NVM、不重启
    return
...
self.driver.set_mode('rover', persist=True, restart=True, verify=True)
# 再独立 get_mode() 确认真的变成 rover，否则拒绝继续连 CORS
```

开头还有一条**纯基准站型号的提前拦截**：

```python
if (not readonly and not getattr(self.driver, 'PASSIVE', False)
        and self._caps('set_mode') and not self._caps('dual_mode')):
    raise WriteFailed('ROVER_SETUP',
        '该模块仅基准站模式，无法运行 CORS 流动站标定。...', {'base_only': True})
```

> 对 LC29H(BS)：`set_mode=False`，所以 `_caps('set_mode')` 为假，**这条不会命中**——它早在 `service.start_cors` 的 `_reject_without_caps` 就被挡了。这条兜的是"有 `set_mode` 但只有单模"的型号。

#### ② `NMEA_SETUP`（`writer.py:1562`）— 最容易踩坑的一步

依次做五件事：

1. **记录配前的 GGA/GSA 速率**（只读，失败忽略）。注释明确"不猜模块默认值"（真机两次实测一次读到 1、一次读到 0）。
2. **显式配 GGA 1Hz、GSA 1Hz**。GGA 失败 = 致命；GSA 失败 = 降级为 HDOP 判据。
3. **静音多余 NMEA**：`QUIET_NMEA = ('GSV','GLL','VTG','RMC','ZDA')` 全置 0。
   真机 A/B 证据（注释里写死）：全开时注入差分后 GGA 只有 **0.15 Hz**；关掉后 **1.03 Hz**。
4. **钉住定位间隔**：`set_fix_rate(FIXRATE_SAFE_MS=100)`（10 Hz）。
   注释给了反面证据：`FixRate=1000`（1 Hz）时 GGA 实测 **0.011 Hz**，且 `PQTMCFGMSGRATE` 读回仍是 1，配置"看起来没问题"但永远进不了固定解。
5. **`_reinit_rtk_engine(applied)`**（`writer.py:1690`）：`SAVEPAR → reboot → channel.reopen() → 回读确认 rover`。
   注释写死了根因：只下发配置不重启 → 8 分钟 1516 条 GGA **全为质量位 2**；加 SAVEPAR+重启 → 质量位 5、高程极差 9.85m → 0.57m。
6. 最后 **`_verify_gga_rate()`** 实测频率（`GGA_MIN_HZ=0.8`）。必须在重启之后——重启前测的是旧状态。

#### ③ `GGA_WAIT`（`writer.py:1846`）
等 3 条有效 GGA；模块不吐则由 `GgaManager` 软件生成。

#### ④ `CORS_CONNECT`
`CorsClient.connect()`，`CorsError` 分类转成 `WriteFailed`。连上后**再跑一次 `_ensure_rover('CORS_CONNECT')`**——因为切模式（含重启搜星）到连 CORS 之间可能隔了几十秒。

#### ⑤ `FIX_WAIT` + `COLLECT`（`writer.py:1955`）— 唯一的长循环

固定解准入（三个条件同时满足，连续 30 秒）：

```python
FIX_QUALITY_REQUIRED = 4      # 质量位 4 = RTK 固定
FIX_MIN_SATS = 12
FIX_MAX_PDOP = 2.0
FIX_HOLD_SECONDS = 30.0
```

PDOP 只从 GSA 拿；无 GSA 时降级判 HDOP（`FIX_MAX_HDOP_FALLBACK`），并置 `hdop_fallback=True`。

**浮动解降级路径**（天线环境受限时的保底）：
- 固定解窗口给满 `FIX_WAIT_TIMEOUT=180s`；
- 超时后看这期间浮动解命中率是否够（`FLOAT_MIGRATE_MIN_HITS_RATIO`）；
- 够则降级：`FLOAT_QUALITY_ALLOWED=5`、`FLOAT_MIN_SATS=12`、`FLOAT_MAX_PDOP=3.0`、`FLOAT_HOLD_SECONDS=20`；
- 一旦降级不再回头等固定解。

**采集算法**（同一个循环里）：
1. 1 Hz 采样 → `samples`
2. `reject_outliers_3sigma_xyz` 剔 3σ 离群 → `clean_history`
3. `Kalman1D`×3 **只作稳定判据**（`kf_history` 仅诊断）
4. **写入值 = 3σ 剔除后的算术平均**（不是卡尔曼输出）
5. 收敛判据 `_cors_converged(clean_history)`：最近 `CONVERGE_HOLD_SECONDS=60` 秒三轴 σ ≤ `CONVERGE_SIGMA=0.02`（浮动解阈值 `CONVERGE_SIGMA_FLOAT=0.15`）

**期间的两种守护**：
- `mode_guard('COLLECT')` 每 `MODE_RECHECK_SECONDS=60` 秒复核一次模式。**只在采集阶段查，不在 FIX_WAIT 查**——注释给了真机证据：FIX_WAIT 插 PQTM 查询会打断 RTCM 流，加检查后 120 秒仍停浮动解，去掉后 38 秒固定。
- 断连：`_cors_reconnect` 退避重连；≤ `CORS_NET_JITTER_TOLERANCE_SEC` 的抖动不清零固定解计时器；累计断连超 `CUMULATIVE_DISCONNECT_LIMIT`（5 分钟）判失败。

**采样顺序**：先 `_drain_gga(GGA_DRAIN_WINDOW)` 再 `client.pump(...)`——顺序反了会让上报给 VRS 的是上一轮缓存里的旧位置。

#### ⑥ `CORS_CLOSE`
`finally` 里无条件执行：`_audit_cors_downlink(client)`（下行体检：播了哪些星座、缺不缺 1230）+ `client.close()`。

体检做成常规输出的理由写在注释里：2026-11-02 真机有个挂载点播 1084 却不播 **1230**，模块 300 秒卡在浮动解，而当时日志全显示"链路正常、29 颗星、PDOP 1.8"。

#### ⑦ 采集结果落到 plan

```python
plan.apc = apc; plan.arp = apc            # 固定解即该天线坐标，不做相位中心换算
plan.conversion = {..., 'formula': 'CORS 固定解即写入值'}
plan.svin_mode = 'fixed'                  # 让后续 WRITE_COORD 走"写固定坐标"
```

### 2.4 各模块在 CORS 链路上的实现差异

| 环节 | LG290P | ZED-F9P | LC29H(BS) |
|---|---|---|---|
| 能否进入 | ✅ | ✅ | ❌（`service` 挡住） |
| 协议层 | 文本 `$PQTM*`，`CMD` 32 条 | **纯 UBX**，`CMD = {}`，全走 `_valset/_valget` | — |
| `set_mode('rover')` | `PQTMCFGRCVRMODE,W,1` | `CFG-NAVHPG`/内部 VALSET | ❌ 直接返回失败（单基站） |
| GGA/GSA 速率 | `PQTMCFGMSGRATE,...` | `MSGOUT_*` / `MSGOUT_NMEA_ID_GGA_<PORT>` | — |
| 静音多余输出 | `silence` cap 未置位 → 不走 | ✅ `silence=True`，`silence_outputs(keep=('NAV_SVIN',))` 关 NAV/NMEA/RXM | — |
| 定位间隔 | `set_fix_rate(100)` | `set_fix_rate` 未覆写（继承基类/无此接口） | — |
| RTK 引擎重启 | 必须 SAVEPAR+reboot | `_reinit_rtk_engine` 同样执行 | — |
| 重启 | `RESTART_REQUIRED=True`，真重启 | `RESTART_REQUIRED=False` → `_step_restart` 记 `{'skipped':True,'reason':'no_reboot_command'}` | — |
| MSM 星座 | `MSM_CONSTELLATIONS` 五条 `107X/108X/109X/111X/112X`，逐星座下发 | 四条 MSM4：`1074/1084/1094/1124`；`REQUIRED_RTCM=(1005,1230)` | `MSM_CONSTELLATIONS=()`（空元组 = 只有一个全局开关） |
| 天线信息 1033 | `ant_info=True`，`PQTMCFGANTINFO` | `ant_info=False` → 打「本型号不支持写天线信息（1033），已跳过」 | `ant_info=False` |
| `nmea_needs_fix` | 未置位 | ✅ `True` | 未置位 |
| 波特率默认 | 见驱动 | `38400` | `115200` |

**F9P 为什么不需要"静音"以外的特殊处理**：`CAPS['silence']=True` 是 F9P 独有的一项。注释解释了必要性——Quectel 两家切 base 后会**自动**停 NMEA 改播 RTCM，u-blox **没有这个自动行为**，NAV-PVT/NAV-SAT/NAV-ODO 照旧 1Hz 吐，38400 下这几路已吃约 5% 带宽。

`silence_outputs(keep=('NAV_SVIN',), persist=False)`：留 `NAV_SVIN`（Survey-in 状态要读），RTCM 不走 MSGOUT 三组键所以不受影响。

---

## 3. 方式二：自收敛（`method='survey_in'`）

亚米级。核心思想：**观测与解算 100% 由模块固件完成**，我们只做「读结果 + 钉死成固定坐标 + 校验持久化」。

### 3.1 入口 → plan

```python
plan = WritePlan.build_survey_in(profile=key, mode='base', msm=msm,
                                 antenna=antenna, remark=remark,
                                 distance=distance, verify_rtcm=True,
                                 coord_msg=coord_msg, resume_svin=resume)
```

档位表（`writer.py:68`）：

```python
SURVEY_PROFILES = {
    'fast':     {'count': 600,  'acc': 3.0, 'label': '快速（临时建站）',   'grade': 'sub_meter'},
    'standard': {'count': 1800, 'acc': 1.5, 'label': '标准（常规野外）',   'grade': 'sub_meter'},
    'high':     {'count': 3600, 'acc': 0.8, 'label': '高精度（长期固定）', 'grade': 'sub_meter'},
}
```

三档**一律 `sub_meter`**，绝不能标 `centimeter`。

`resume=True`（默认）= 允许断点续标。超时 `svin_timeout=None` = 不限时（一直等收敛或手动终止）。

### 3.2 完整阶段链

```
SVIN_RESUME(只读探测；命中则跳过后面三步)
   ├─ 未命中：
   │   BASE_SETUP   切基站（persist=True，因为下一步会重启）
   │   SVIN_CFG     配次数/精度门限 + 打开状态输出（必要时先 reboot）
   │   SVIN_WAIT    轮询等收敛（唯一的长步骤）
   └─ 命中/收敛后：
       WRITE_COORD  把均值写成固定坐标
   MSG_SETUP → SAVE → RESTART → VERIFY_MODE → VERIFY_RTCM → INJECT
```

`run()` 里：

```python
if plan.method == 'survey_in':
    if not self._step_svin_resume(plan):
        self._step_switch_mode(plan); self._step_svin_cfg(plan); self._step_svin_wait(plan)
    self._step_write_coord(plan)
```

### 3.3 四个步骤

#### ① `SVIN_RESUME`（`writer.py:989`）— 断点续标，三条只读条件

```python
if not plan.resume_svin or not self._caps('svin'):   return False
# 条件1：当前是 base 模式（SVINSTATUS 只在 base 输出）
# 条件2：get_svin_config()['mode'] == 'survey_in'
# 条件3：survey_status()['state'] == 2（已收敛）且均值非零
```

任一条不满足就 `return False`，交回原流程重新收敛。等待上限 `SVIN_RESUME_WAIT=6.0` 秒。

注释的取舍很明确："宁可多收敛一轮，也不要在'看起来像收敛'的时候直接采信一个来路不明的坐标。"

#### ② `SVIN_CFG`（`writer.py:1079`）

```python
if plan.mode_ack in ('ack','deferred') and getattr(self.driver,'RESTART_REQUIRED',True):
    self.driver.reboot()     # 让基站模式真正生效
```

为什么必须先重启再配 SVIN：注释给了真机证据——**`$PQTMSVINSTATUS` 只在基站模式输出**，rover 模式下 `PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1` 直接回 `ERROR,1`。

然后 `_svin_config_verified(count, acc, distance, 'SVIN_CFG')`（`writer.py:2577`）：配 + 回读比对，不符则**重发一次配置再回读**（只对 timeout 重试；`ERROR,n` 直接失败）。回读要查三样：`mode=='survey_in'`、`count` 相等。

最后打开状态输出：

```python
enabler = getattr(self.driver, 'enable_survey_status', None)
if enabler is not None:
    ok, message, data = enabler(True, 1)
    if not ok:
        raise WriteFailed('SVIN_CFG', '打开收敛状态输出失败：...没有状态输出就无法判断收敛，已中止', data)
```

**注意：不是所有驱动都有 `enable_survey_status`**。缺失时静默跳过（`getattr` 拿到 `None`），靠模块默认速率输出状态。

#### ③ `SVIN_WAIT`（`writer.py:1123`）— 唯一的长时间步骤

判据：`status['done'] and obs >= count`。**只认 `Valid==2`**（注释：文档示例里 59/60 时 `Valid` 仍是 1）。

循环里做的事：
- `_drain_svin()` **必须主动读串口**——`survey_status()` 读的是 `channel.cache['svin']`，而 cache 靠 `read_line` 消费 `$PQTMSVINSTATUS` 行来填充。
- `_assert_link_alive('SVIN_WAIT')`：自收敛最长可能跑 7200 秒，中途断电不查链路就会干等到超时。
- 进度回显：`obs/count`、`mean_acc`、预计剩余分钟。
- **卡住告警**：`state==1` 且 `obs` 不再增长连续 `SVIN_STALL_WARN_AT=20` 次 → 打「天线可能被遮挡」。
- 状态为 `None` 时只打「等待状态输出…（已 N 秒）」。

**用户取消的处理是特殊的**：`_checkpoint()` 抛 `TaskAborted` 时**吞掉**并置 `terminated=True`，用当前均值继续（`evicted=True` 的 `TaskEvicted` 不吞——串口已不归我们）。如果 `obs == 0` 才判失败。

收敛后提取坐标，注释里的边界写得很重：

```python
# **坐标来源边界（务必看清，别误读成"我们在解算"）**：
# MeanX/Y/Z 就是固件自己算的，我们一个字节都没有参与解算
plan.svin_mode = 'fixed'    # 接下来是"把均值写成固定坐标"
plan.conversion = {..., 'solved_by': 'firmware'}
```

`result['survey']` 落：`terminated / obs / count / mean_acc / mean / elapsed_seconds / accuracy_grade='sub_meter'`。

#### ④ `WRITE_COORD`（`writer.py:945`）
`set_fixed_ecef(persist=False, restart=False, verify=False)`，重试 2 次（只对 timeout），逐次 `get_svin_config()` + `_compare_fixed()` 回读比对。

### 3.4 各模块在自收敛链路上的实现差异（**这是三种模块差别最大的一条链**）

| 环节 | LG290P | LC29H(BS) | ZED-F9P |
|---|---|---|---|
| `config_survey_in` 命令 | `PQTMCFGSVIN,W,1,{count},{acc},0,0,0[,{distance}]`（8 字段） | `PQTMCFGSVIN`（**7 字段，无 Distance**） | `TMODE_MODE=MODE_SURVEY_IN` + `TMODE_SVIN_MIN_DUR` + `TMODE_SVIN_ACC_LIMIT` |
| `count` 语义 | **定位次数**（0~86400） | **定位次数**（0~86400） | **秒**（`MIN_DUR`） |
| `acc` 单位 | 米（0.0~1000.0，`0` = 不限，打 WARNING） | 米（`0` = 不限） | **毫米**（内部换算 0.1mm，`scale=0.1`） |
| `distance` | ✅ 支持（0.0~10.0） | ❌ 接受但不下发，返回 `data['ignored']` | ❌ 忽略，返回 `distance_ignored` |
| 状态输出开关 | `enable_survey_status(True,1)` → `PQTMCFGMSGRATE` | **方法不存在** → `_step_svin_cfg` 里 `enabler is None` **静默跳过** | `enable_survey_status` → 设 `NAVOUT_NAV_SVIN_<PORT>`，`port = detect_port()[0] or 'UART1'` |
| 状态解析 | `channel.cache['svin']`；拿不到时按 `cache['mode_code']==1` 提示「rover 模式不输出」 | `channel.cache['svin']`；没有则返回 `{}` | 解析 **NAV-SVIN 40 字节**，同时给 `mean_acc_mm` 与 `mean_acc`（米），并把 `x/y/z` 复制成 `mean_x/mean_y/mean_z` |
| 切基站 `set_mode('base')` | `PQTMCFGRCVRMODE,W,2` 真下发 | **故意空操作**（见下） | UBX VALSET 切 base |
| SVIN_CFG 前是否 reboot | ✅ `RESTART_REQUIRED=True` | ❌ **跳过**（`RESTART_REQUIRED=False`，配置即时生效） | ❌ **跳过**（`RESTART_REQUIRED=False`） |
| `count` 是否会被 writer 校验 | 会比 `readback['count']` | 同 | 会比 `readback['count']`（驱动返回的是 `min_dur_s`） |
| 精度无效值处理 | — | — | `svin_acc_meaningless_when_no_fix=True` → 未定位时 `meanAcc` 是溢出垃圾值（真机见过 94868.3264 m = `0xE8D4A510`×0.1mm），回显时加「未定位时该值无效」 |

**LC29H(BS) 的 `set_mode('base')` 为什么是空操作**（`quectel_lc29hbs.py:374-423`）：

```python
def set_mode(self, mode, persist=True, restart=True, verify=True):
    want = str(mode or '').strip().lower()
    if want in ('rover', 'mobile', '流动站', '移动站'):
        return False, ('LC29H(BS) 是单基站模块，不支持流动站模式'
                       '（文档里没有 PQTMCFGRCVRMODE 命令）'), {'error': 'unsupported mode'}
    ...
    data = {'noop': True, 'reason': 'no_mode_command',
            'hint': '基站身份由 PQTMCFGSVIN 的 Mode=2 + 有效坐标决定'}
    ok, message, cfg = self.get_svin_config()
    if cfg.get('mode') == 'fixed':
        return True, '当前已是基站（固定坐标模式，Mode=2）', {**data, 'svin': cfg}
    if verify:
        return False, ('当前不是基站（Mode=%s）：本型号没有模式命令，'
                       '必须先写入固定坐标（PQTMCFGSVIN,W,2,...）才会成为基站'
                       % cfg.get('mode')), {**data, 'svin': cfg, 'error': 'base_needs_coords'}
    return True, '本型号无模式命令：已确认可写坐标，写入固定坐标后即为基站', {**data, 'svin': cfg}
```

注释里明确写了**为什么不能照抄 LG290P**：

- 写 `Mode=1`（survey-in）会把模块从"播固定坐标"改成"重新自收敛"——回滚路径（先还原坐标、再 `set_mode('base')`）会因此把刚还原好的坐标配置改成测量模式，一次「还原」反而把基站变成正在测量的站，且 1005 播的是旧均值。
- 写 `Mode=0` 更糟：等于把坐标清掉。

**真正让 LC29H(BS) 成为基站的是流程层随后的 `set_fixed_ecef()`**。

> 副作用提示（本文档只记录）：因为 `set_mode('base')` 在 `verify=True` 且当前不是 base 时会返回 `False`，`_step_switch_mode` 会走 `raise WriteFailed('BASE_SETUP', ...)`。而在自收敛链上 `WRITE_COORD` 在 `BASE_SETUP` **之前**（`SVIN_WAIT → WRITE_COORD`），此前 `SVIN_CFG` 已经把 `Mode=1` 写进去了——所以这里必然报 `base_needs_coords`。LC29H(BS) 的真机自收敛跑通走的是「断点续标命中」或 `verify=False` 的路径。这条属于**已知行为差异**，不是本文档要修的东西。

---

## 4. 方式三：已知坐标写入（`method='manual'`）

精度完全取决于用户给的坐标。核心思想：把外部得到的 ECEF 直接钉进模块，然后跑完整校验链路。

### 4.1 入口 → plan

```python
plan = WritePlan.build(ecef=ecef, lat=lat, lon=lon, height=height,
                       method=method,      # 'manual'
                       msm=msm, antenna=antenna, remark=remark,
                       coord_msg=coord_msg, arp=arp)
```

`arp=` 是「复用历史坐标」：库里存的就是当年写进模块的值，直通写入。同样要写 NVM，所以按 `fixed` 能力判。

参数校验**同步做完**（ECEF 越界这类问题在下发任何命令之前就报出来，而不是等占住串口、切了基站才发现）。

### 4.2 `run()` 里的第四支：靠 `svin_mode` 二次分流

```python
else:                                            # manual / restore / 其它
    if plan.svin_mode == 'fixed':
        self._step_write_coord(plan)             # ← manual 走这条
    else:
        self._step_config_svin(plan)             # ← 「还原」路径走这条
    self._step_switch_mode(plan)
```

`_step_config_svin`（`writer.py:2616`）是**还原专用**，两态：
- `survey_in` → `_svin_config_verified()` 配回去
- `disabled` → `disable_survey_in(persist=False, restart=False)`

它**只写不收敛**，所以不需要打开状态输出——这是它与 `_step_svin_cfg` 的关键区别。

### 4.3 阶段链

```
WRITE_COORD → BASE_SETUP → MSG_SETUP → SAVE → RESTART → VERIFY_MODE → VERIFY_RTCM → INJECT
```

注意顺序：**先写坐标、再切基站**。注释：还原时同理（"先把坐标改回去，再切模式"，把风险窗口最小化）。

### 4.4 各模块差异

| 环节 | LG290P | LC29H(BS) | ZED-F9P |
|---|---|---|---|
| 写坐标命令 | `PQTMCFGSVIN,W,2,0,0,{x},{y},{z}` | `PQTMCFGSVIN,W,2,0,0,{x},{y},{z}` | `TMODE_MODE=2` + `TMODE_POS_TYPE=0` + `ECEF_X/Y/Z` + `ECEF_X_HP/Y_HP/Z_HP` |
| ECEF 精度处理 | 直接给米 | 直接给米 | 拆成整厘米 + **0.1mm 有符号余量**两段 |
| 固定精度参数 | — | — | `TMODE_FIXED_POS_ACC = 10`（10mm） |
| 回读容差 | `COORD_TOLERANCE` | 同 | **0.5 mm**（HP 段验证，比通用容差严得多） |
| 切基站 | `PQTMCFGRCVRMODE,W,2` | **空操作**（基站身份 = Mode2 + 有效坐标） | VALSET 切 base |
| 是否需要 `MSM_CONSTELLATIONS` | 五星座逐条 | `()`（空元组→只发一条全局） | 四条 MSM4 |
| RESTART | 真重启 | 跳过（`RESTART_REQUIRED=False`） | 跳过（`RESTART_REQUIRED=False`） |
| 1033 天线信息 | ✅ `PQTMCFGANTINFO` | ❌ | ❌ |

**F9P 的 ECEF 拆分**（`ublox_zed_f9p.py:868-914`）是这条链上唯一需要"算数"的地方：

```python
# TMODE_ECEF_X 单位 cm，TMODE_ECEF_X_HP 单位 0.1mm 且有符号
ECEF_X(cm)    = int(x * 100)
ECEF_X_HP     = round((x * 100 - ECEF_X) * 10000)   # 注意有符号，不是无符号补码
```

### 4.5 `_step_switch_mode` 的 `persist` 分支

```python
persist = plan.method == 'survey_in'    # writer.py:2644
ok, message, data = self.driver.set_mode(plan.mode, persist=persist, restart=False, verify=False)
```

只有 survey-in 的切模式要落 NVM——因为 `SVIN_CFG` 会先重启一次，若内存态不落盘，重启后会被 NVM 里的旧值（rover）覆盖，基站模式就丢了。其余路径（含 manual）只改内存态，把"存不存"留给 `_step_save` 统一决定。

`timeout` 的处理（§8.3）：模块切到基站后会关掉 NMEA，应答是否还输出没有文档保证，所以**不判失败**，置 `mode_ack='deferred'`，留到 `VERIFY_MODE` 二次确认。

---

## 5. 方式四：流动站模式测量 / 只测不写（`method='external_rover'`）

核心思想：**同一条 CORS 采集链路，但一个字节都不写进模块**。用于"这块模块是外接流动站，或没有命令集的未知模块，我只要一个天线坐标"。

### 5.1 入口

`service.start_cors(measure_only=True)` → `WritePlan.build_cors(..., method='external_rover')`。

能力门已在 §1.2 说明：只看 `dual_mode`，未指定型号放行。

### 5.2 `run()` 里的早退分支（`writer.py:745-757`）

```python
elif plan.method == 'external_rover':
    try:
        self._step_cors_collect(plan)
    finally:
        self._restore_memory_mode()
    self.result['verdict'] = 'skipped'   # 没有写入，也就没有 1005 校验
    self._step_inject(plan)
    self.result['readonly'] = True
    return self.result                    # ← 直接 return，不进公共尾链
```

三条硬约束：

1. **`self._readonly = plan.method == 'external_rover'`**（`run()` 开头就设）。
2. **`_step_save` 里有硬守卫**：
   ```python
   if getattr(self, '_readonly', False):
       raise WriteFailed('SAVE', '只测不写模式禁止 SAVEPAR（会写入模块 NVM）')
   ```
   注释："正常路径根本不会走到这里，但把守卫放在发命令之前，等于给未来所有重构上了一道闸"。
3. **失败收尾也不回写**（`_finish_failure`）：
   ```python
   if getattr(self, '_readonly', False):
       error.nvm_state = NVM_NOT_WRITTEN
       error.message = f'{error.message}（本模式未向模块写入任何配置，NVM 未改动）'
       return
   ```
   理由：没有备份可回，回写反而会朝一块不归我们管的设备发命令。

### 5.3 阶段链

```
ROVER_SETUP → NMEA_SETUP → GGA_WAIT → CORS_CONNECT → FIX_WAIT+COLLECT → CORS_CLOSE
                                                                              ↓
                                                                     INJECT（落库，不写 NVM）
```

### 5.4 与 `cors` 在**同一个函数**里的四处分流

`_step_cors_rover_setup` / `_step_cors_nmea_setup` 都靠 `readonly = plan.method == 'external_rover'` 分叉：

**(a) ROVER_SETUP**
- `get_mode()` 读不到时：`readonly` → 不判失败，打警告「按流动站继续，若始终拿不到固定解请先手动把模块切到流动站模式」，记 `{'from':'unknown','assumed':True}`。
  额外检查：若驱动是 `PASSIVE` 且 `_looks_like_base_stream()` 为真 → 当场拒绝（基准站不输出 GGA，采集只会空转到超时）。
- `from_mode == 'base'` 时：走**仅内存**切换
  ```python
  switcher = getattr(self.driver, 'set_mode_memory_only', None)
  if switcher is None:
      raise WriteFailed('ROVER_SETUP', '这块模块当前是基准站模式，而本模式（只测不写）不改动模块配置。'
                        '该型号驱动不支持"仅内存切换模式"...')
  ok, message, mdata = switcher('rover')
  self._mem_restore_mode = from_mode       # 记下原模式，收尾必须切回
  ```
- `from_mode == 'rover'`：跳过切换（与 `cors` 相同）。

**(b) NMEA_SETUP**
```python
if plan.method == 'external_rover':
    # 已在输出 GGA → 一个字都不发
    # 没输出 → 才临时打开，且只改内存态，并告知"重启后恢复原值"
```

**(c) 收尾** `_restore_memory_mode()`（`writer.py:1295`）：
- **只改内存、绝不 SAVEPAR**——NVM 本来就没被改过，再存一次反而把"一个字节都没写"变成假的。
- 任何异常只记日志，不能盖住原始失败原因。
- 驱动不支持内存切换时打警告：「NVM 未受影响，断电重启即恢复」。

**(d) 落库** `build_record`：`method='external_rover'` 与 `cors` **共用同一套统计口径**（`accuracy=cors['sigma3d']`、`samples`、`duration`）——因为走的是同一条采集链路。`verdict='skipped'`，`verified` 映射为 `0`。

### 5.5 各模块差异

| 环节 | LG290P | ZED-F9P | LC29H(BS) |
|---|---|---|---|
| 能否进入 | ✅ | ✅ | ❌（`service` 挡） |
| `set_mode_memory_only` | ✅ 有 | ✅ 有 | 有但 `set_mode('rover')` 直接返回失败 |
| base→rover 仅内存切换 | ✅ `PQTMCFGRCVRMODE` 不落盘 | ✅ UBX VALSET 只写 RAM 层 | ❌（本来也不允许进入） |

> 关键点：**这条链路是三个模块里唯一"驱动实现完全不需要改动"的**。因为它只用 `set_mode_memory_only`、`get_message_rate`/`set_message_rate`、`get_mode`、`sleep`——这些都是 `BaseDriver` 契约里已有的方法。模块差异完全由"能不能进这道门"决定，而不是由驱动内部流程决定。

---

## 6. 公共尾链（`manual` / `survey_in` / `cors` 三条路共用）

### 6.1 `MSG_SETUP`（`writer.py:2666`）— 五段，全部靠 cap 开关

| 段 | 条件 | 内容 | 失败后果 |
|---|---|---|---|
| 3.0 沿用默认 | `coord_msg=='keep' and msm is None and not antenna` | 一个字节都不改 | — |
| 3.1 MSM 档位 | `msm and _caps('rtcm_out', True)` | `config_rtcm(msm_type)` + `read_rtcm_config()` 回读 | **致命**（配不上等于"只有坐标没有观测值"） |
| 3.2 坐标报文 | `coord_msg != 'keep'` | 开 `RTCM3-1005` 或 `1006`（1Hz）+ 回读 | **致命** |
| 3.3 逐星座 MSM | `msm` 为真 | 遍历 `MSM_CONSTELLATIONS` 逐个开 | 只记警告 |
| 3.4 天线 1033 | `plan.antenna and _caps('ant_info')` | `set_antenna_info()` + 开 1033 速率 | 只记警告 |
| 3.5 静音 | `_caps('silence')` | `silence_outputs(keep=('NAV_SVIN',))` | 只记警告 |

**3.1 回读时先取 `readback.get('msm_type')`，再退回 `fields[0]`**：
> 不是每个型号都有"七字段"（LC29H(BS) 只有一个全局 PAIR432 开关，`read_rtcm_config` 明确返回空的 fields）。只看 `fields[0]` 的话，那台模块回读永远得到 None，一次都过不了这关。

**3.3 的 `or` 陷阱**（注释里标了重点）：
```python
constellations = getattr(self.driver, 'MSM_CONSTELLATIONS', None)
if constellations is None:
    constellations = ('RTCM3-107X',)
# ✅ 只能判 None，不能用 `or` —— 早先写成 `... or ('RTCM3-107X',)`，
#    空表会被兜成默认值，于是对只有一个全局 MSM 开关的型号照样逐条发，换来一串"不支持"的假警告
```

**3.5 为什么必须放最后**：先保证该播的播出来，再关不该播的。静音失败只记警告——"真出了关不掉的怪模块，也不能让用户连坐标都写不进去"。

### 6.2 `SAVE`（`writer.py:2904`）
- `_readonly` 硬守卫（§5.2）。
- `driver.save()` → `_saved=True`、`nvm_state=NVM_SAVED`。
- 保存后 `get_svin_config()` 回读（这是 **RAM 态**确认，不是 NVM）。
- `_verify_persisted_after_save()`：**只对 `CAPS['persist_check']` 的型号**（当前只有 F9P）再 poll 一次持久层。失败**不阻断**，只降级为告警——"真正的硬判据在后面的 VERIFY_MODE"。
- 能力不存在时返回 `{'checked': False, 'reason': 'unsupported'}`，**不假装查过了**。

### 6.3 `RESTART`（`writer.py:2983`）
```python
if not getattr(self.driver, 'RESTART_REQUIRED', True):
    self.result['restart'] = {'skipped': True, 'reason': 'no_reboot_command'}
```
**F9P 和 LC29H(BS) 都走这条**（`RESTART_REQUIRED=False`），LG290P 真重启。

### 6.4 `VERIFY_MODE`（`writer.py:3008`）
```python
restarted = not bool((self.result.get('restart') or {}).get('skipped'))
when = '重启后' if restarted else '当前（未重启）'
```
判据用**本次流程实际有没有跳过重启**，而不是再读一遍 `RESTART_REQUIRED`——后者回答的是"这个型号需不需要重启"，不是"这次到底重启了没有"。

校验三样：`get_mode()['mode'] == plan.mode`；`svin_mode=='fixed'` 时 `_compare_fixed()`；`svin_mode=='survey_in'` 时 `readback['mode']=='survey_in'`。

文案尾巴分裂：重启过 → `NVM 持久化确认通过`；没重启 → `配置已生效，持久化待断电复核`。

### 6.5 `VERIFY_RTCM`（`writer.py:3061`）
- `plan.verify_rtcm` 为假 → `verdict='skipped'`。
- 不符时**按标定流程重下发一轮**（报文 + 坐标 → 保存 → 重启 → 复核），`RTCM_RETRY_LIMIT = 1`。
- 只重试一轮的理由：两次都错说明不是偶发，继续重试只会反复写 NVM 磨损闪存。

**语义边界（注释里写得最重的一段）**：

> 1005 播发的就是模块内部存的那份坐标，「写入 → 重启 → 播发」全程是同一份数据，误差恒等于 1005 的量化误差（≤0.1mm）。所以这一步只能证明「写入生效 + NVM 持久化成功 + 播发链路通」，**不能证明坐标的绝对精度**。把它当精度证据属于循环验证。绝对精度只能靠独立观测：换 CORS 源交叉解算 / 已知控制点比对 / 静态 PPP 后处理。

### 6.6 `INJECT`（`writer.py:3225`）
- `kind == 'restore'` → **不落** `calibration_records`（只记事件）。理由：历史坐标列表是给"从这里挑一个坐标写进去"用的，混进一条"标定前的老坐标"会让用户下次选到从未标定过的值。
- 其余 → `build_record()` + `_on_record()` 落库。

`build_record` 按 `method` 填不同字段：

| method | `accuracy` | `samples` | `duration` | `svin_valid/obs/acc` |
|---|---|---|---|---|
| `survey_in` | `survey['mean_acc']` | `survey['obs']` | `survey['elapsed_seconds']` | ✅ `2 / obs / mean_acc` |
| `cors` | `cors['sigma3d']` | `cors['samples']` | `cors['duration_seconds']` | NULL |
| `external_rover` | 同 `cors` | 同 `cors` | 同 `cors` | NULL |

经纬度按 **ARP（写入值）** 反算——播发出去的就是这组数。

---

## 7. 能力位矩阵与使用点

### 7.1 三个模块的 `CAPS`（反射实测）

| 能力位 | LG290P | LC29H(BS) | ZED-F9P |
|---|---|---|---|
| `dual_mode` | True | **False** | True |
| `set_mode` | True | **False** | True |
| `svin` | True | True | True |
| `fixed` | True | True | True |
| `ant_info` | True | False | False |
| `ant_delta` | False | False | False |
| `msg_rate` | True | True | True |
| `rtcm_out` | True | True | True |
| `err3` | True | True | **False** |
| `valdel` | — | — | True |
| `factory_reset` | — | — | True |
| `persist_check` | — | — | True |
| `silence` | — | — | True |
| `nmea_needs_fix` | — | — | True |
| `svin_acc_meaningless_when_no_fix` | — | — | True |

### 7.2 其他关键类属性

| 属性 | LG290P | LC29H(BS) | ZED-F9P |
|---|---|---|---|
| `CMD` 条数 | 32 | 18 | **0**（全 UBX） |
| `DUAL_MODE` | True | False | True |
| `RESTART_REQUIRED` | True | **False** | **False** |
| `MSM_CONSTELLATIONS` | 5 条（107X~112X） | **`()`** | 4 条（1074/1084/1094/1124） |
| `DEFAULT_BAUD` | — | 115200 | 38400 |
| `RATE_TARGETS` | — | `('PQTMSVINSTATUS','PQTMEPE')` | — |
| `COORD_MSG` | — | `('RTCM3-1005',)` | `REQUIRED_RTCM=(1005,1230)` |

### 7.3 `_caps(key)` 的 9 个使用点（`writer.py:3403`）

```python
def _caps(self, key, default=False):
    try:
        return bool(self.driver.caps(key, default))   # 读 runtime_caps
    except Exception:
        return default
```

| 使用点 | key | 影响 |
|---|---|---|
| `_step_svin_resume` | `svin` | 不支持则不尝试续标 |
| `_step_svin_wait` | `svin_acc_meaningless_when_no_fix` | 未定位时给精度值加解释文案 |
| `_step_cors_rover_setup` | `set_mode` / `dual_mode` | 纯基准站型号提前拦截 |
| `_step_setup_messages` 3.1 | `rtcm_out` | MSM 档位配置 |
| `_step_setup_messages` 3.3 | （用 `MSM_CONSTELLATIONS`，非 cap） | 逐星座 |
| `_step_setup_messages` 3.4 | `ant_info` | 1033 |
| `_step_setup_messages` 3.5 | `silence` | 静音 |
| `_step_save` | `persist_check` | 保存后 poll 持久层 |

> 也就是说：**`writer.py` 里所有"按型号分叉"的判断，最终都收敛到这 9 个 boolean**。除了 `PASSIVE`、`RESTART_REQUIRED`、`MSM_CONSTELLATIONS`、`_mem_restore_mode` 这几个显式属性。

---

## 8. 接口覆写对比（三个驱动各自实现了什么）

| 接口 | LG290P | LC29H(BS) | ZED-F9P |
|---|---|---|---|
| `set_fixed_ecef` | ✅ | ✅ | ✅ |
| `config_survey_in` | ✅ | ✅ | ✅ |
| `get_svin_config` | ✅ | ✅ | ✅ |
| `survey_status` | ✅ | ✅ | ✅ |
| `enable_survey_status` | ✅ | **❌ 缺失** | ✅ |
| `set_mode` | ✅ | ✅（base 空操作 / rover 拒绝） | ✅ |
| `set_mode_memory_only` | ✅ | ✅ | ✅ |
| `disable_survey_in` | ✅ | ✅ | ✅ |
| `save` / `reboot` | ✅ | ✅ | ✅ |
| `set_message_rate` | ✅ | ✅（只认 2 条 + 1005/1006） | ✅ |
| `config_rtcm` | ✅ | ✅（只 MSM4/7） | ✅ |
| `read_rtcm_config` | ✅ | ✅（空 fields + `msm_type`） | ✅ |
| `set_fix_rate` | ✅ | **❌ 缺失** | **❌ 缺失** |
| `set_antenna_info` | ✅ | ✅（但 `CAPS.ant_info=False`） | ✅（但 `CAPS.ant_info=False`） |
| `silence_outputs` | **❌ 缺失** | **❌ 缺失** | ✅ |
| `verify_persisted` | **❌ 缺失** | **❌ 缺失** | ✅ |

**"缺失"是安全的**：`writer.py` 全部用 `getattr(..., None)` 或 `_caps()` 前置判断，缺了就走降级路径，不会 `AttributeError`。

---

## 9. 关键常量速查（`writer.py`）

```python
# --- Survey-in ---
SURVEY_PROFILES       fast=600/3.0m  standard=1800/1.5m  high=3600/0.8m
SVIN_POLL_INTERVAL    0.5 s
SVIN_RESUME_WAIT      6.0 s
SVIN_STALL_WARN_AT    20 次

# --- CORS ---
CORS_DURATIONS        {'short':180, 'standard':300, 'long':600}
CORS_DEFAULT_DURATION 300
FIX_QUALITY_REQUIRED  4
FIX_MIN_SATS          12
FIX_MAX_PDOP          2.0
FIX_HOLD_SECONDS      30.0
FLOAT_QUALITY_ALLOWED 5
FLOAT_MIN_SATS        12
FLOAT_MAX_PDOP        3.0
FLOAT_HOLD_SECONDS    20.0
FIX_WAIT_TIMEOUT      180.0
FIXRATE_SAFE_MS       100          # 10 Hz 定位，GGA 抽稀到 1 Hz
GGA_MIN_HZ            0.8
QUIET_NMEA            ('GSV','GLL','VTG','RMC','ZDA')
MODE_RECHECK_SECONDS  60.0

# --- 采集滤波 ---
CONVERGE_SIGMA        0.02 m       # 固定解
CONVERGE_SIGMA_FLOAT  0.15 m
CONVERGE_HOLD_SECONDS 60.0
COLLECT_MIN_SAMPLES   10

# --- 尾链 ---
RTCM_RETRY_LIMIT      1
DEFAULT_RTCM_TIMEOUT  20.0
DEFAULT_RTCM_SAMPLES  3
```

---

## 10. 阶段名契约（事件流里能看到的字符串）

```
SVIN_WAIT  → SVIN_CFG   → WRITE_COORD → BASE_SETUP → MSG_SETUP → SAVE → RESTART
           → VERIFY_MODE → VERIFY_RTCM → INJECT

ROVER_SETUP → NMEA_SETUP → GGA_WAIT → CORS_CONNECT → FIX_WAIT → COLLECT
           → CORS_CLOSE → WRITE_COORD → BASE_SETUP → MSG_SETUP → SAVE → ...
```

阶段序列是**契约**，注释里明确：「不单独记一步 MSG_SETUP（阶段序列是契约）」。新增/改名阶段要先确认前端与事件表能接。

---

## 11. 附：四种方式一页对照

| 维度 | `cors` | `survey_in` | `manual` | `external_rover` |
|---|---|---|---|---|
| 精度等级 | `centimeter`（固定解）/ `decimeter`（浮动降级） | `sub_meter` | 取决于输入 | 同 `cors` |
| 谁解算 | 模块（RTK，吃 CORS 差分） | **模块固件**（SVIN） | 外部（用户） | 模块（RTK） |
| 典型时长 | 3~10 分钟 | 10~60 分钟 | 秒级 | 3~10 分钟 |
| 写 NVM | ✅ | ✅ | ✅ | **❌ 一个字节都不写** |
| 需要 CORS 账号 | ✅（复用分支跳过） | ❌ | ❌ | ✅ |
| 需要 `dual_mode` | ✅ | ❌ | ❌ | ✅ |
| 需要 `set_mode` | ✅ | ❌（`set_mode('base')` 允许空操作） | ❌ | ❌ |
| 需要 `svin` | ❌ | ✅ | ❌ | ❌ |
| 需要 `fixed` | ✅ | ❌ | ✅ | ❌ |
| 进公共尾链 | ✅ | ✅ | ✅ | ❌（采集完 return） |
| `verdict` 值 | `match`/`warning` | `match`/`warning` | `match`/`warning` | **`skipped`** |
| 有断点续标 | ✅（reuse_arp） | ✅（SVIN_RESUME） | ✅（arp=） | ❌ |
| 可手动中止并采用当前值 | ✅（`on_slow='adopt'`） | ✅（吞 TaskAborted） | ❌ | ✅ |

---

## 12. 本文档记录的已知偏差与修复状态

梳理过程中发现的、与"四种方式按模块正确分流"这个目标相关的偏差。**分级修复，改完划掉**：

| # | 位置 | 现象 | 状态 |
|---|---|---|---|
| 1 | `static/calibration.js` `canWrite()` + `service.py` 三处能力门 | 用 `command_count > 0` 当总闸，而 `ZED_F9P.CMD = {}` → 网页上选 F9P 四张卡全灰，且直接 POST 也会 400 | ✅ **已修**（见 §12.1） |
| 2 | LC29H(BS) `set_mode('base')` 在非 base 态返回 `base_needs_coords` | 自收敛链 `SWITCH_MODE` 在 `SVIN_CFG` 之前，此刻 Mode 必为 disabled → 全新模块第一步就失败 | ✅ **已修**（见 §12.2，按"单基准站无需切换"处理） |
| 3 | `backup.capture()` 的 `mode` 字段 | 字段名是 `mode`，装的却是**角色**；与 `svin['mode']`（坐标模式 `fixed`/`survey_in`/`disabled`）同名不同义，易混用 | ✅ **已修**（见 §12.3，加归一化 `role`） |
| 4 | `_step_svin_wait` | 对无状态输出的型号必然死等到超时 | ⏸ 未触发（三个驱动都有 `survey_status`） |
| 5 | `service.py` 多处读 `svin['mode']` | 复核结论：**这几处本来就是坐标模式，取值正确**，不是"归一化遗漏" | ❌ 撤销原判定（见 §12.4） |

### 12.1 偏差 1：F9P 被 `command_count` 误判

**关键点：错误不只在 `canWrite()` 一处** —— 后端 `_reject_write_without_fixed` 和 `_reject_without_caps` 里也有 `command_count == 0` 直接拒绝，只改前端等于按钮能点、请求照样 400。

统一判据抽成 `service._has_command_set(info)`：

```python
def _has_command_set(info):
    if (info or {}).get('command_count', 0) > 0:
        return True
    caps = (info or {}).get('caps') or {}
    return any(isinstance(caps.get(key), bool)
               for key in ('fixed', 'svin', 'set_mode', 'dual_mode'))
```

判据含义：**命令表非空 → 可写；否则看能力位是否被驱动真正声明过**（有明确布尔值即说明该驱动实现了能力探测，走的是 UBX 之类二进制路径）。能力位全 `undefined` 的空壳驱动才算"没法写"。

修复后各模块的四卡锁状态（实测枚举）：

| 模块 | cmd | cors | survey_in | manual | external_rover |
|---|---|---|---|---|---|
| `lg290p` | 32 | 开 | 开 | 开 | 开 |
| `zed_f9p` | **0** | **开** | **开** | **开** | **开** |
| `lc29hbs` | 18 | 🔒 锁 | 开 | 开 | 🔒 锁 |
| `um960/960l/980` | 27~33 | 开 | 🔒 锁 | 开 | 开 |

与 §0 的实测口径完全一致。

### 12.2 偏差 2：LC29H(BS) 自收敛第一步失败

**修正一个易错点**：实际链路是 `SWITCH_MODE → SVIN_CFG → SVIN_WAIT → WRITE_COORD`（不是 `BASE_SETUP → SVIN_CFG → … → WRITE_COORD`），且 `_step_switch_mode` 调 `set_mode` 时传 `verify=False`，所以**不会**在 `BASE_SETUP` 抛 `base_needs_coords`。真正的问题是驱动在 `verify=False` 下虽然返回成功，但语义含糊、未标记"无需切换"。

按"LC29H 本来就是单基准站模块，不需要切换/保存模式"处理，`set_mode('base')` 一律返回成功并带 `already_base_side=True`：

```python
return True, (f'本型号是单基准站模块，无需切换模式'
              f'（当前 SVIN Mode={cfg.get("mode")}，'
              f'由 PQTMCFGSVIN 决定，写入坐标后即为基站）'), \
             {**data, 'svin': cfg, 'already_base_side': True}
```

`_step_switch_mode` 据此记成 `mode_ack='not_needed'`，**且不置 `_touched`**（空操作没改模块，标成已改动会让失败文案谎称"正在回写"）。

配套验证：`_step_svin_cfg` 的 preboot 判断是 `mode_ack in ('ack','deferred')`，`not_needed` 不会误触发重启 —— 对 `RESTART_REQUIRED=False` 的 LC29H 正好正确。

### 12.3 偏差 3：备份的 `mode` 语义混用

`backup['mode']` 拆成三字段，`text` 保留驱动原值可追溯，新增 `role` 归一化：

```python
backup['mode'] = {
    'code': code,
    'text': data.get('mode') or MODE_TEXT.get(code, 'unknown'),
    'role': _role_of(code, data.get('mode')),   # 只返回 base / rover / unknown
}
```

`_role_of(code, text)` **code 优先**（1=rover、2=base 是协议数字，三家一致），code 缺失才看 text；`'fixed'`/`'survey_in'` 这类坐标模式值一律判 `unknown`，防止被误当角色。

`usable()` / `describe()` 改走 `role`，并兼容没有 `role` 字段的老备份（按 code 现算），不会因为老文件缺字段就判成不可还原。

### 12.4 偏差 5 撤销说明

原判"`service.py:1202/1356/1941/1943`、`backup.py:229` 读 `mode` 而非 `mode_norm`"**不成立**：这些位置读的都是 `get_svin_config()` 返回的 `svin['mode']`，即坐标模式（`fixed`/`survey_in`/`disabled`），本身就是归一化后的字符串，用在这些判断里是**正确的**。全局替换反而会引入错误分支。真正的语义混用只在 `backup['mode']`，已在 §12.3 处理。

---

*文档基于当前代码梳理。真机实测口径由用户提供（LG290P 四模式全通 / LC29H(BS) 仅自收敛+已知坐标 / ZED-F9P 四模式全通）。*
*§12.1 ~ §12.3 已落地修复；回归：`tests/test_contract.py` 291 项、`tests/test_ublox_f9p.py` 74 项。*