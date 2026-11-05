# ZED-F9P 一键设为基准站——完整流程文档

> 基于 `rtkserver/calibration/` 源码追踪：`service.py` → `writer.py`（状态机）→ `drivers/ublox_zed_f9p.py`（UBX 指令层）
>
> 入口：`service.start_survey_in()`（自收敛）或 `service.start_cors()`（CORS 标定），两条路在写完坐标后汇合。
>
> 本文以 **Survey-in 自收敛** 为主线（`method='survey_in'`），CORS 路径差异处单独标注。

---

## 总览：步骤序列

```
串口打开 → OUTPUT_PREP → [SVIN_RESUME?] → BASE_SETUP(切基站)
  → 重启(让基站生效) → SVIN_CFG(配收敛参数+开NAV-SVIN) → SVIN_WAIT(等收敛)
  → WRITE_COORD(把收敛均值写成固定坐标) → MSG_SETUP(配RTCM输出+静音多余输出)
  → SAVE(CFG-CFG落NVM) → RESTART(软重启) → VERIFY_MODE(回读模式+坐标)
  → VERIFY_RTCM(监听1005校验播发) → INJECT(落库)
```

驱动本身没有"一键设基站"单方法，以上由 `writer.py` 的 `run()` 方法逐步骤编排，每步一个 `_step_xxx()`。

---

## 步骤 0：打开标定所需输出（OUTPUT_PREP）

**函数**：`writer._step_prepare_outputs()`（第 2934 行）

### 做什么

开工前主动打开本次流程需要的输出报文，只改 RAM（`persist=False`），不动 Flash。

### 用了什么命令

- Survey-in 路径：打开 `NAV-PVT` 1 Hz（给前端位置快照）
- CORS 路径：打开 `GGA`、`RMC`、`NAV-PVT`（GGA 要上送 CORS）
- 手动写坐标路径：打开 `GGA`、`NAV-PVT`

具体走驱动 `enable_outputs(want, rate=1, persist=False)`，内部对每条报文调 `set_message_rate()` → 写 `CFG_MSGOUT_*_<端口>` 键。

### 目的

真机教训：模块可能被之前的调试配成"只剩 RTCM"（NAV/NMEA 全关），导致流程卡在等 GGA 或等状态报文。开工前主动开，避免事后补救白等几十秒。

### 失败反馈

- **失败不阻断**，只记警告 `_warn()`："开启标定所需输出失败，流程继续"。
- 模块本来就在播 RTCM，输出开不出来顶多某条校验降级。
- 如果模块本就全开着，`data.enabled` 为空，不标 `_touched`（不算"我们改过模块"）。

---

## 步骤 1：断点续标检查（SVIN_RESUME）

**函数**：`writer._step_svin_resume()`（第 1055 行）

### 做什么

只读检查：模块是否已经是 Survey-in 模式且已收敛（`Valid==2`）。如果是，直接拿已收敛的均值写固定坐标，跳过重新等 30~60 分钟。

### 判定条件

1. 当前是基站模式（`get_mode()` 返回 `base`）
2. 坐标模式是 `survey_in`
3. NAV-SVIN 里 `active==0`（收敛完成）且 `valid==1`
4. 均值非零

### 失败反馈

任何一条不满足就返回 `False`，走完整流程（切基站→配收敛→等收敛）。不报错。

---

## 步骤 2：切换为基准站模式（BASE_SETUP）

**函数**：`writer._step_switch_mode()`（第 3107 行）→ 驱动 `set_mode('base')`（第 833 行）

### 做什么

把 TMODE_MODE 从 0（流动站/disabled）切到对应基站模式值。

### 用了什么命令

驱动内部发 **CFG-VALSET**：

| 键名 | keyID | 值 | 含义 |
|---|---|---|---|
| `CFG_TMODE_MODE` | `0x20030001` | `1`（Survey-in）或 `2`（Fixed） | 0=流动站, 1=自收敛, 2=固定坐标 |

Survey-in 路径先写 `TMODE_MODE=1`（Survey-in），后续收敛完再改成 `2`（Fixed）。

- `persist=True`（survey-in 路径必须落 NVM，因为后面要重启）
- `restart=False`（writer 后面统一重启）
- `verify=False`（writer 后面统一回读）

### 目的

让模块从"流动站解算"切换到"基准站自定位"模式。

### 失败反馈与校验

- **ACK 超时**不判失败：切基站后模块可能关 NMEA 输出，应答可能不来。标记 `mode_ack='deferred'`，留到重启后二次确认。
- **NAK 或其他错误**：抛 `WriteFailed('BASE_SETUP', ...)`，流程中止。
- 单基准站模块（如 LC29H(BS)）：`set_mode()` 是空操作，返回 `already_base_side`，记 `not_needed`。

---

## 步骤 2b：重启让基站模式生效

**函数**：`writer._step_svin_cfg()` 开头（第 1155 行）→ 驱动 `reboot()`（第 2413 行）

### 做什么

切基站后重启一次，因为 `$PQTMSVINSTATUS` / NAV-SVIN 只在基站模式输出，流动站模式下开状态输出直接报错。

### 用了什么命令

发 **UBX-CFG-RST**（类 0x06，ID 0x04）：
- `navBbrMask` = `0x0000`（不清 BBR）
- `resetMode` = `0x02`（受控软复位）
- 等待模块重新上线（8 秒内串口出现数据）

> **已知问题**：CFG-RST 载荷只打包了 3 字节（`<HB`），pyubx2 定义是 4 字节（缺 `reserved0`）。模块通常容忍，但不严格符合协议。

### 失败反馈

重启失败抛 `WriteFailed('SVIN_CFG', '切基站后重启失败...')`。

---

## 步骤 3：配置 Survey-in 收敛参数（SVIN_CFG）

**函数**：`writer._step_svin_cfg()`（第 1144 行）→ 驱动 `config_survey_in()`（第 1020 行）

### 做什么

设置自收敛的时长和精度门限，并打开收敛状态报文输出。

### 用了什么命令

驱动发 **CFG-VALSET** 写三个键：

| 键名 | keyID | 说明 |
|---|---|---|
| `CFG_TMODE_SVIN_MIN_DUR` | — | 最小收敛时长（**秒**），fast=600s / standard=1800s / high=3600s |
| `CFG_TMODE_SVIN_ACC_LIMIT` | — | 精度门限（mm），fast=3000mm / standard=1500mm / high=800mm |
| `CFG_TMODE_MODE` | `0x20030001` | = `1`（Survey-in 模式） |

然后调 `enable_survey_status(True, rate=1)` 打开 **UBX-NAV-SVIN** 周期输出 1 Hz：
- 写 `CFG_MSGOUT_NAV_SVIN_USB/UART1` = 1

### 三档预设

| 档位 | 时长 | 精度门限 | 等级 |
|---|---|---|---|
| fast | 600 秒（10 分钟） | 3.0 m | 亚米 |
| standard | 1800 秒（30 分钟） | 1.5 m | 亚米 |
| high | 3600 秒（60 分钟） | 0.8 m | 亚米 |

### 失败反馈与校验

- `config_survey_in()` 后立即调 `get_svin_config()` 回读，对比写入的 count 和 acc：
  - 回读不一致 → `_svin_config_verified()` 抛 `WriteFailed`
- `enable_survey_status()` 失败 → 抛 `WriteFailed`（没有状态输出就无法判断收敛）

---

## 步骤 4：等待自收敛完成（SVIN_WAIT）

**函数**：`writer._step_svin_wait_by_status()`（第 1212 行）→ 驱动 `survey_status()`（第 1157 行）

### 做什么

轮询 NAV-SVIN 报文，直到收敛完成。

### 用了什么命令

持续读串口上的 **UBX-NAV-SVIN**（类 0x01，ID 0x36），解析关键字段：

| 字段 | 偏移 | 含义 |
|---|---|---|
| `iTOW` | 0-3 | GPS 时间戳 |
| `dur` | 4-7 | 当前收敛已用秒数 |
| `meanX` | 8-11 | 均值 X（cm） |
| `meanY` | 12-15 | 均值 Y（cm） |
| `meanZ` | 16-19 | 均值 Z（cm） |
| `meanXHP` | 20 | X 高精密分量（0.1mm，有符号） |
| `meanYHP` | 21 | Y 高精密分量 |
| `meanZHP` | 22 | Z 高精密分量 |
| `meanAcc` | 24-27 | 均值精度估计（mm） |
| `obs` | 28-29 | 已观测固定解次数 |
| `valid` | 30 bit0 | 1=坐标有效 |
| `active` | 30 bit1 | 1=Survey-in 进行中 |

### 收敛判定

```
active == 0  AND  valid == 1
```

即 Survey-in 已结束且坐标有效。同时要求 `obs >= dur`（观测次数达标）。

### 中途取消

用户手动取消时**不丢弃结果**，用当前均值继续写入（只要 obs > 0）。

### 失败反馈

- 超时（`svin_timeout`）：抛 `WriteFailed`
- 链路断开（模块掉线）：`_assert_link_alive()` 检测串口无数据，提前报错
- obs 长期不增长（stall 检测）：记警告

> **已知问题**：`survey_status()` 第 1157 行的 `_combine()` 公式 `(main + hp/10000)/100` 差了 100 倍，应为 `(main + hp/100)/100`。hp 单位是 0.1mm，1cm=100×0.1mm，不是 10000。这会导致收敛坐标的毫米级分量被错误缩小 100 倍。

---

## 步骤 5：写入固定坐标（WRITE_COORD）

**函数**：`writer._step_write_coord()`（第 1011 行）→ 驱动 `set_fixed_ecef()`（第 971 行）

### 做什么

把步骤 4 收敛出的 ECEF 坐标写成固定基准站坐标，TMODE 从 Survey-in 切到 Fixed。

### 用了什么命令

发 **CFG-VALSET** 写 8 个键：

| 键名 | 说明 |
|---|---|
| `CFG_TMODE_MODE` | = `2`（Fixed 固定模式） |
| `CFG_TMODE_POS_TYPE` | = `0`（ECEF 坐标，非经纬高） |
| `CFG_TMODE_ECEF_X` | X 坐标（cm 整数部分） |
| `CFG_TMODE_ECEF_X_HP` | X 高精密分量（0.1mm，有符号单字节） |
| `CFG_TMODE_ECEF_Y` | Y（cm） |
| `CFG_TMODE_ECEF_Y_HP` | Y HP |
| `CFG_TMODE_ECEF_Z` | Z（cm） |
| `CFG_TMODE_ECEF_Z_HP` | Z HP |
| `CFG_TMODE_FIXED_POS_ACC` | 坐标精度声明 = 10mm |

坐标拆分：`_split_ecef()` 把米值拆成厘米整数 + 0.1mm 余数。HP 字段是有符号单字节（-127~127）。

### 失败反馈与校验

- 写入后立即调 `get_svin_config()` 回读，对比三轴坐标：
  - 容差 0.5mm（`_compare_fixed()`）
  - 最大轴差超过容差 → 重试一次（共 2 次）
  - 仍不一致 → 抛 `WriteFailed('WRITE_COORD', ...)`，**不保存 NVM**，模块保持原状
- 超时类错误重试，明确 NAK 不重试

---

## 步骤 6：配置 RTCM 输出（MSG_SETUP）

**函数**：`writer._step_setup_messages()`（第 3148 行）→ 驱动 `config_rtcm()`（第 2030 行）

### 做什么

配置基准站向外播发的 RTCM3 差分报文，并静音多余输出。

### 用了什么命令

#### 6.1 RTCM 整体配置（config_rtcm）

发一条 **CFG-VALSET**，批量写 MSGOUT 键：

| RTCM 报文 | 含义 | MSM4 键 | MSM7 键 |
|---|---|---|---|
| 1005 | 基准站坐标（ARP） | `RTCM_1005_<port>` | 同左 |
| 1074/1077 | GPS 观测值 | `RTCM_1074_<port>` | `RTCM_1077_<port>` |
| 1084/1087 | GLONASS 观测值 | `RTCM_1084_<port>` | `RTCM_1087_<port>` |
| 1094/1097 | Galileo 观测值 | `RTCM_1094_<port>` | `RTCM_1097_<port>` |
| 1124/1127 | BDS 观测值 | `RTCM_1124_<port>` | `RTCM_1127_<port>` |
| 1230 | GLONASS 码偏差 | `RTCM_1230_<port>` | 同左 |

未选中的观测报文显式写 0（避免 MSM4/MSM7 混播）。

端口由 `detect_port()` 探测：USB CDC（COMxx/ttyACMx）写 `_USB` 后缀键，UART1 写 `_UART1` 后缀键。

#### 6.2 坐标报文速率（set_message_rate）

单独发 CFG-VALSET 确保 `RTCM3-1005` 速率 = 1。

#### 6.3 逐星座打开 MSM

对每个星座（GPS/GLO/GAL/BDS）分别调 `set_message_rate('RTCM3-107X', 1)` 等。

#### 6.4 天线信息（1033）

如果用户填了天线信息，写 `set_antenna_info()` → CFG-VALSET 写天线描述/Setup ID/序列号。

#### 6.5 静音多余输出（silence_outputs）

先 `snapshot_outputs()` 保存当前所有输出状态（RAM 层），再 `silence_outputs()` 把 NAV-PVT/NAV-SAT/NAV-ODO/GGA/GSA/GSV/RMC/VTG 等非 RTCM 输出关掉，只留 RTCM。

### 失败反馈与校验

- RTCM 配置失败 → **致命**，抛 `WriteFailed('MSG_SETUP')`（MSM 配不上等于基站没用）
- RTCM 配置后调 `read_rtcm_config()` 回读 MSM 档位：
  - 回读档位 ≠ 目标档位 → 抛 `WriteFailed`
- 1005 速率回读 ≠ 1 → 抛 `WriteFailed`
- 部分星座 MSM 打开失败 → 只记警告，不阻断
- 静音失败 → 只记警告（带宽优化，不是功能前提）

> **已知问题**：ubx_keys.py 中 MSM4 的 4 个 key ID（1074/1084/1094/1124）全部抄成了对应 MSM7 的 I2C 端口 ID，导致默认 MSM4 路径下 UART1/USB 口上不播观测报文。MSM7 的 4 个 ID 正确。

---

## 步骤 7：保存到 NVM（SAVE）

**函数**：`writer._step_save()`（第 3470 行）→ 驱动 `save()`（第 2271 行）

### 做什么

把 RAM 里的配置写入 Flash，断电不丢。

### 用了什么命令

发 **UBX-CFG-CFG**（类 0x06，ID 0x09）：
- `clearMask` = `0x00001F1F`（不清）
- `saveMask` = `0x00001F1F`（存 IO/MSG/INF/NAV/RINV）
- `loadMask` = `0`
- `deviceMask` = `0x03`（BBR + Flash）

载荷 13 字节：`<IIIB`。

### 失败反馈与校验

1. `save()` 本身失败 → 抛 `WriteFailed('SAVE')`
2. 保存后立即 `get_svin_config()` 回读 RAM 态：
   - fixed 模式下对比三轴坐标，最大轴差 > 阈值 → 抛 `WriteFailed`
3. 对声明了 `persist_check` 能力的型号，额外 `verify_persisted()` 读 **Flash 层**（layer=2）：
   - 只查 3 个键：`TMODE_MODE`、`RTCM_1005`、`RTCM_1230`
   - 不匹配 → **只警告不阻断**（Flash 偶发写入失败重启后可重试）

> **已知问题**：`PERSIST_CHECK_KEYS` 只查 3 个键，不查 TMODE 坐标（LAT/LON/HEIGHT 或 ECEF X/Y/Z）。坐标没落盘发现不了。

---

## 步骤 8：软重启（RESTART）

**函数**：`writer._step_restart()`（第 3550 行）→ 驱动 `reboot()`（第 2413 行）

### 做什么

重启模块，让它从 NVM 加载配置，确认掉电后配置不丢。

### 用了什么命令

同步骤 2b：发 **UBX-CFG-RST**（3 字节载荷，resetMode=0x02），等 8 秒内串口出现数据。

### 失败反馈

- 模块 8 秒没上线 → 抛 `WriteFailed('RESTART', '模块没有重启成功...')`
- 配置已保存但无法确认是否生效，提示用户断电重启后重新校验

> **已知问题**：`reboot()` 只验证"串口上有数据回来"，不调 `detect()` 确认模块身份和波特率。如果模块进了 safe boot（9600 波特率），host 在 38400 上读到乱码但仍报"重启成功"。

---

## 步骤 9：重启后回读校验（VERIFY_MODE）

**函数**：`writer._step_verify_mode()`（第 3575 行）

### 做什么

重启后从 RAM 读回实际配置，对比写入值。

### 用了什么命令

1. `get_mode()` → CFG-VALGET 读 `CFG_TMODE_MODE`：
   - 期望值 = `base`（对应 TMODE_MODE=2 fixed）
   - 实际 ≠ 期望值 → 抛 `WriteFailed('VERIFY_MODE', '配置没有持久化成功')`
2. `get_svin_config()` → CFG-VALGET 批量读 11 个 TMODE 键：
   - fixed 模式下对比 ECEF X/Y/Z（含 HP 分量），容差 0.5mm
   - 不一致 → 抛 `WriteFailed`

### 目的

这是 NVM 持久化的硬判据：重启后 RAM 里的值是从 Flash/BBR 加载的，如果这里对得上，说明真的存进去了。

### 失败反馈

- 模式不对 → 致命错误，提示检查 NVM
- 坐标不一致 → 致命错误，给出最大轴差

---

## 步骤 10：RTCM 播发校验（VERIFY_RTCM）

**函数**：`writer._step_verify_rtcm()`（第 3628 行）

### 做什么

监听串口上实际播发的 RTCM3-1005 报文，解析其中的坐标，与写入值对比。

### 用了什么命令

不发命令，**被动监听**串口流：
- 解析 RTCM3 帧同步码 `0xD3`
- 识别 1005 报文（消息类型 1005）
- 提取基准站 ECEF 坐标
- 与写入的 `plan.arp` 对比

### 失败反馈与重试

- 第 1 次不符 → **自动重下发一轮**（重新配报文 + 写坐标 + SAVE + RESTART）
- 第 2 次仍不符 → 抛 `WriteFailed('VERIFY_RTCM')`
- 只重试 1 次（避免反复擦写 Flash）

### 注意

这一步只能证明"写入生效 + NVM 持久化 + 播发链路通"，**不能证明坐标绝对精度**（1005 播的就是模块内部存的那份坐标，是循环验证）。绝对精度要靠独立观测。

---

## 步骤 11：落库（INJECT）

**函数**：`writer._step_inject()`（第 3792 行）

把标定结果（坐标、精度等级、方法、时间、天线信息）写入标定数据库，完成。

---

## 附：CORS 差分标定路径差异

CORS 路径（`method='cors'`）与 Survey-in 的区别在步骤 2~5：

| 步骤 | Survey-in | CORS |
|---|---|---|
| 模式 | 直接切基站 Survey-in | 先切**流动站**，开 GGA，连 NTRIP CORS |
| 坐标来源 | 模块自己收敛 30~60 分钟 | 收 CORS 差分数据，等 RTK 固定解，采集滤波得到厘米级坐标 |
| 收敛判定 | NAV-SVIN active==0 && valid==1 | NAV-PVT flags.diffSoln==1 && carrSoln==2（固定解），持续采集 N 秒 |
| 坐标精度 | 亚米级（0.8~3m） | 厘米级 |

两条路在拿到 ECEF 坐标后汇合：WRITE_COORD → MSG_SETUP → SAVE → RESTART → VERIFY。

---

## 附：流动站（Rover）侧配置对比

作为参考，F9P 当流动站时与基准站的关键差异：

| 项目 | 基准站 | 流动站 |
|---|---|---|
| `CFG_TMODE_MODE` | 1（Survey-in）或 2（Fixed） | **0**（流动站/disabled） |
| RTCM 输出 | 开启 1005/MSM/1230 | **关闭**（不需要播差分） |
| RTCM 输入 | 不需要 | 需要开 `CFG_*_INPROT_RTCM3X` = 1（从串口收差分） |
| 判定 | NAV-SVIN valid==1 | NAV-PVT flags.diffSoln==1 && carrSoln==2 |

> **已知问题**：当前驱动没有显式写 `CFG_*_INPROT_RTCM3X=1`。F9P 默认可能不接收 RTCM3 输入，流动站侧需要手动开。

---

## 已知问题汇总（按步骤标注）

| # | 位置 | 问题 | 影响步骤 |
|---|---|---|---|
| 1 | ubx_keys.py 173-180 | MSM4 的 1074/1084/1094/1124 key ID 抄错（写成 MSM7 的 I2C ID） | 步骤 6 |
| 2 | ublox_zed_f9p.py 1157 | `_combine()` hp 因子 100× 错误（/10000 应为 /100） | 步骤 4 |
| 3 | ubx_keys.py RTCM_OUT | RTCM 键硬编码 UART1，USB 链路不出 RTCM | 步骤 6 |
| 4 | ublox_zed_f9p.py 28/29 | MON-HW aStatus/aPower 偏移错（应为 20/21） | 天线诊断 |
| 5 | ublox_zed_f9p.py 0:8 | "唯一 ID"读的是 pinSel+pinBank 不是芯片 ID | info() |
| 6 | ublox_zed_f9p.py 2336 | CFG-RST 载荷少 reserved0（3 字节应为 4 字节） | 步骤 2b/8 |
| 7 | 驱动全局 | 无 `INPROT_RTCM3X=1` 流动站输入配置 | 流动站侧 |
| 8 | PERSIST_CHECK_KEYS | 只查 3 键，不查 TMODE 坐标 | 步骤 7 |
| 9 | reboot() | 不调 detect() 确认模块身份和波特率 | 步骤 8 |
