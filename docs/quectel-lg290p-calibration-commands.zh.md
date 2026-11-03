# LG290P 与其他 LG 模块标定指令全集

> 依据：移远通信官方文档 7 份（见 §1.2）。所有命令、参数范围、默认值、返回格式均**照原文抄录**，
> 未做任何增删改写；文档之间的差异单列在 §8，不做"统一化"处理。
>
> **标注约定**（全文遵守）：
>
> | 标记 | 含义 |
> | --- | --- |
> | `[原文]` | 文档中直接给出的命令、数值或示例，可直接使用 |
> | `[推导]` | 文档给出格式但该具体取值没有示例，本文按格式拼装并**已按 XOR 复算校验和** |
> | `[建议]` | 文档未给数值，本文给出的工程建议值，**非官方参数**，上机前须实测 |
> | `[未规定]` | 文档明确没有说，不臆造 |
>
> **校验和可信度**：本文所有 `*XX` 均用 §7.1 的算法逐条复算过。凡文档自带示例的（如
> `$PQTMSAVEPAR*5A`、`$PQTMCFGRCVRMODE,W,2*29`、`$PAIR864,0,0,115200*1B`），复算结果与文档
> **完全一致**，说明复算函数与移远实现一致，`[推导]` 条目的校验和可直接使用。

---

## 1. 范围

### 1.1 适用模块

| 文档 | 覆盖型号 |
| --- | --- |
| GNSS 协议规范 V1.2-1 | LG290P(03)、LG291P(00)、LG291P(03)、LG580P(03)、LG581P(03)、LG680P(03)（LG580P(06) 仅在基站指导的适用表出现） |
| 基站模式应用指导 V1.1 | 同上（主线是 LG290P(03)） |
| RTK 应用指导 V1.1 | LG290P(03)、LG291P(03)、LG580P(03)、LG581P(03)、LG680P(03)（**不含 LG291P(00)**） |
| LC29H(BS) GNSS 协议规范 V1.0 | LC29H(BS) —— 另一套指令集，见 §8.2 |
| LC29H(AA,BS)/LC79H(AL) EASY&EPOC 差异说明 | AGNSS 预测功能，**与标定无关**，见 §8.3 |
| LC29H 系列 SPI 应用指导 | LC29H 系列 SPI 传输层，见 §8.4 |
| LG290P(03) 硬件设计手册 V1.2 | UART1~3 电气与波特率，见 §2.4 |

**本报告主体是 LG290P(03)（`$PQTM` 指令集）**，其他型号的差异集中在 §8 的矩阵里，不混写。

### 1.2 文档清单（引用编号）

| 编号 | 文档 | 本报告用它做什么 |
| --- | --- | --- |
| [1] | Quectel_LG29xPLG58xPLG680P系列_GNSS_协议规范_V1.2-1 | 命令格式、参数范围、默认值、错误码、返回示例（主依据） |
| [2] | Quectel_LG29xPLG58xPLG680P系列_基站模式_应用指导_V1.1 | 基站模式标准操作序列、1005 实测输出、FAQ |
| [3] | Quectel_LG29xPLG58xPLG680P系列_RTK_应用指导_V1.1 | RTCM 输入报文表、差分龄期上限、固定解排查 |
| [4] | Quectel_LC29HBS_GNSS_协议规范_V1.0 | LC29H(BS) 的 PQTM/PAIR 指令集 |
| [5] | Quectel_LC29HAABSLC79HAL_EASYEPOC_差异说明_V1.0 | EASY/EPOC 的 PAIR 指令（非标定） |
| [6] | Quectel_LC29H系列_SPI_应用指导_V1.0 | SPI 与 UART1 复用关系 |
| [7] | Quectel_LG290P03_硬件设计手册_V1.2 | UART 波特率档位、无硬件流控 |

---

## 2. 通用规则（先读这一节，否则单条命令会用错）

### 2.1 帧结构 `[原文]`

```
$<Address>[,<Data>...]*<Checksum><CR><LF>
```

- 起始 `$`（0x24），结束 `<CR><LF>`（0x0D 0x0A）——**两条都必须发**，只发 `\n` 模块不认。
- `<Checksum>`：`$` 与 `*` 之间**全部字符**（含所有 `,`）逐字节 XOR，取两位大写十六进制。
- 例：`$PQTMSAVEPAR*5A` → 参与计算的是 `PQTMSAVEPAR` 这 11 个字符。

### 2.2 错误码 `[原文]`

LG29xP/LG58xP/LG680P 系列（协议规范 表 5）：

| ErrCode | 含义 |
| --- | --- |
| 1 | 参数无效 |
| 2 | 执行失败 |
| 3 | **不支持该命令** |

LC29H(BS)（LC29HBS 协议规范 表 3）**只有 1、2 两个**，没有 3。上位机判错时不能只认 3。

### 2.3 配置生效模型（最容易踩的坑）`[原文]`

> "为避免一些不确定因素，通过设置命令配置参数后需要发送 `$PQTMSAVEPAR*5A` 保存配置，然后重启
> 模块，以确保所有配置生效。否则模块上电后会恢复默认值。"（协议规范 2.3 章）

即：**`W` 命令只改运行态；不 `SAVEPAR` + 重启，断电即丢**。

**重启等待时长 —— `[未规定]`**。文档只写"等待模块开机"，没有给秒数，本文不编造。
判定重启完成的可信依据是协议规范 2.3.1 备注：

> "模块每次成功启动后，均会先输出该语句（`$PQTMVER`）。"

所以工程上应**等 `$PQTMVER` 首帧**再发后续命令；`[建议]` 3~10 s 内收不到首帧即重发一次查询探测，
而不是死等固定秒数。

### 2.4 端口与速率 `[原文]`

- 命令走 UART1/UART2/UART3，均为 8N1、**不支持硬件流控**（硬件手册 4.1.1.x）。
- LG290P 支持的波特率：`9600` / `115200` / `230400` / `460800` / `921600`。
  **没有 4800、19200、38400、57600、3000000**（这点和 LC29H(BS) 的 `$PAIR864` 不同，见 §8.2）。
- 默认波特率：**文档没有给固定值**。硬件手册 UART1/2/3 三处备注均为：
  "接口默认设置可能因软件版本而异。详情请参考具体的软件版本。"
  → 所以上位机**不能假设 115200**，首连应做"多档轮询探测"（`[建议]`：9600 → 115200 → 460800）。

---

## 3. 一、识别与信息查询类

### 3.1 `PQTMVERNO` — 查询固件版本

**发送**

```
$PQTMVERNO*<CS><CR><LF>
```

```
$PQTMVERNO*58
```

**返回** `[原文]`

```
$PQTMVERNO,<VerStr>,<BuildDate>,<BuildTime>*<CS><CR><LF>      成功
$PQTMVERNO,ERROR,<ErrCode>*<CS><CR><LF>                        失败
```

实测示例：`$PQTMVERNO,LG290P03AANR01A03S,2024/04/30,10:53:07*18`

| 字段 | 格式 | 单位 | 说明 |
| --- | --- | --- | --- |
| `<VerStr>` | 字符串 | — | 固件版本 |
| `<BuildDate>` | YYYY/MM/DD | — | 固件创建日期 |
| `<BuildTime>` | hh:mm:ss | — | 固件创建时间 |

**生效条件**：查询类，无需保存、无需重启。

**注意**
- 与 `$PQTMVER`（§3.4）不同：`PQTMVER` 是**模块主动输出**的开机首帧，`PQTMVERNO` 是**你问它答**。
- 版本串里带型号位（`LG290P03`），可用它反查是不是插错了型号，比读 SN 快。

---

### 3.2 `PQTMSN` — 查询模块 SN

**发送**

```
$PQTMSN*<CS><CR><LF>
```

```
$PQTMSN*05
```

**返回** `[原文]`

```
$PQTMSN,OK,<Reversed>,<Length>,<SN>*<CS><CR><LF>
$PQTMSN,ERROR,<ErrCode>*<CS><CR><LF>
```

实测示例：`$PQTMSN,OK,1,16,ESG5241364AUIDE5*3C`

| 字段 | 格式 | 单位 | 说明 |
| --- | --- | --- | --- |
| `<Reversed>` | 十进制 | — | 固定为 1（原文如此，不是笔误修正项） |
| `<Length>` | 十进制 | 字节 | SN 长度，**最大 32**，按实际字符串长度输出 |
| `<SN>` | 字符串 | — | SN 字符串 |

**生效条件**：查询类，无需保存、无需重启。

**注意**
- `<Length>` 是"实际长度"，不要按固定 16 位去截断解析，SN 最长 32。
- 与 `PQTMUNIQID` 的区别：SN 是产线序列号（可读、可登记），UNIQID 是芯片唯一 ID（16 字节十六进制）。

---

### 3.3 `PQTMUNIQID` — 查询模块唯一 ID

**发送**

```
$PQTMUNIQID*<CS><CR><LF>
```

```
$PQTMUNIQID*16
```

**返回** `[原文]`

```
$PQTMUNIQID,OK,<Length>,<ID>*<CS><CR><LF>
$PQTMUNIQID,ERROR,<ErrCode>*<CS><CR><LF>
```

实测示例：`$PQTMUNIQID,OK,16,81D62010EE0AF375BDF5952CDC3757A1*3E`

| 字段 | 格式 | 单位 | 说明 |
| --- | --- | --- | --- |
| `<Length>` | 十进制 | 字节 | 唯一 ID 长度 |
| `<ID>` | 十六进制 | — | 模块唯一 ID |

**生效条件**：查询类，无需保存、无需重启。

**注意**
- 返回串里**没有 `OK` 后的固定位数**，`<Length>` 决定后面 ID 的字符数，解析时按 `<Length>` 取。
- 适合做"基站-账号"绑定：不可篡改，比 SN 更适合做授权锚点。

---

### 3.4 `PQTMVER` — 开机首帧（模块主动输出）

**发送**：无（模块自己发）。

**返回** `[原文]`

```
$PQTMVER,<MsgVer>,<VerName>,<VerStr>,<BuildDate>,<BuildTime>*<CS><CR><LF>
```

示例：`$PQTMVER,1,MODULE,LG290P03AANR01A03S,2024/04/30,10:53:07*32`

| 字段 | 格式 | 单位 | 说明 |
| --- | --- | --- | --- |
| `<MsgVer>` | 十进制 | — | 语句版本，总为 1 |
| `<VerName>` | 字符串 | — | 固定为 `MODULE` |
| `<VerStr>` | 字符串 | — | 主版本字符串 |
| `<BuildDate>` | yyyy/mm/dd | — | 固件构建日期 |
| `<BuildTime>` | hh:mm:ss | — | 固件构建时间 |

**生效条件**：不适用（输出类）。

**注意**
- **这是判断"重启完成"的唯一可靠信号**（`[原文]` 备注：每次成功启动都会先输出它）。
  上位机做"下发→保存→重启→回读"时，用它当同步点，不要 `sleep(5)`。
- `MsgVer` 恒为 1，别拿它当固件版本。

---

### 3.5 当前工作模式查询

与设置是**同一条命令** `PQTMCFGRCVRMODE`，完整字段见 §4.1。此处只给发送与返回。

**发送**

```
$PQTMCFGRCVRMODE,R*32
```

**返回** `[原文]`

```
$PQTMCFGRCVRMODE,OK,<Mode>*<CS><CR><LF>
$PQTMCFGRCVRMODE,ERROR,<ErrCode>*<CS><CR><LF>
```

示例：`$PQTMCFGRCVRMODE,OK,2*7A`（2 = 基站模式）

**生效条件**：查询类，无需保存、无需重启。

**注意**：`<Mode>` 返回 2 不代表基站**已经收敛**——收敛状态要看 `PQTMSVINSTATUS`（§5.2）。

---

## 4. 二、工作模式切换类

### 4.1 `PQTMCFGRCVRMODE` — 设置/查询接收机工作模式

**发送**

```
$PQTMCFGRCVRMODE,W,<Mode>*<CS><CR><LF>      设置
$PQTMCFGRCVRMODE,R*<CS><CR><LF>             查询
```

```
$PQTMCFGRCVRMODE,W,2*29     切基站模式   [原文]
$PQTMCFGRCVRMODE,W,1*2A     切移动站模式 [推导]
$PQTMCFGRCVRMODE,R*32       查询当前模式 [原文]
```

**返回** `[原文]`

```
$PQTMCFGRCVRMODE,OK*<CS><CR><LF>             设置成功
$PQTMCFGRCVRMODE,OK,<Mode>*<CS><CR><LF>      查询成功
$PQTMCFGRCVRMODE,ERROR,<ErrCode>*<CS><CR><LF> 失败
```

实测示例：`$PQTMCFGRCVRMODE,OK*64` / `$PQTMCFGRCVRMODE,OK,2*7A`

| 参数 | 格式 | 单位 | 取值 | 含义 |
| --- | --- | --- | --- | --- |
| `<Mode>` | 十进制 | — | 0 / 1 / 2 | 0 = 未知；**1 = 移动站模式**（切到该模式时接收机恢复默认 NMEA 输出状态）；**2 = 基站模式**（自动禁用 NMEA 输出，启用 RTCM MSM4 与 RTCM3-1005 输出） |

**生效条件**

| 项 | 结论 |
| --- | --- |
| 需要 `PQTMSAVEPAR` | 是 |
| 需要重启 | **是**（`[原文]`：不保存并重置，模块仍继续在原来的模式下工作） |
| 重启等待时长 | `[未规定]`，以 `$PQTMVER` 首帧为准（§2.3） |
| 例外 | **LG291P(00) 无需重启即可立即切换**；但若不保存配置，重启后恢复原模式 |

**注意**
- 切到基站模式后**NMEA 自动关闭**、**RTCM MSM4 + 1005 自动开启**、定位速率固定 1 Hz。
  想要 NMEA 就得自己用 `PQTMCFGMSGRATE` 逐条打开（§6.1），不是"切回来就好"。
- 切回移动站模式时"恢复默认 NMEA 输出状态"——**之前手动调过的语句速率会被默认值覆盖**，
  所以模式来回切之后必须重新下发输出配置。
- 基站模式下的定位速率固定 1 Hz **且不支持更改**（LG291P(00) 例外，见 §6.3）。
- 这一条必须先做，再做 §5（Survey-in）。文档明确：除 LG291P(00) 外，其他模块**必须处于基站模式**
  才能执行 `PQTMSVINSTATUS`。

---

## 5. 三、坐标与 Survey-in 类

两条路线二选一：

- **知道自己坐标** → 固定（Fixed）模式，直接写 ECEF；
- **不知道** → Survey-in 自收敛。

文档原话（基站指导 2.2.3）：

> "如果用户已知基站位置的坐标，可通过 PQTMCFGSVIN 将模块设置为固定模式……如果用户无法获取准确
> 坐标，可通过模块的 Survey-in 模式自动获取位置坐标。"

### 5.1 `PQTMCFGSVIN` — 设置/查询 Survey-in 与固定坐标

**发送**

```
$PQTMCFGSVIN,W,<Mode>,<CFG_CNT>,<3D_AccLimit>,<ECEF_X>,<ECEF_Y>,<ECEF_Z>[,<Distance>]*<CS><CR><LF>
$PQTMCFGSVIN,R*<CS><CR><LF>
```

三条都是 `[原文]` 示例，可直接下发：

```
$PQTMCFGSVIN,W,1,3600,1.2,0,0,0*0A                                     Survey-in，3600 次 / 1.2 m
$PQTMCFGSVIN,W,1,3600,1.2,0,0,0,2.5*0F                                 Survey-in + 保持距离 2.5 m
$PQTMCFGSVIN,W,2,0,0,-2519265.0514,4849534.9045,3277834.6432*2A        固定模式，写 ECEF
```

**返回** `[原文]`

```
$PQTMCFGSVIN,OK*<CS><CR><LF>                                            设置成功
$PQTMCFGSVIN,OK,<Mode>,<CFG_CNT>,<3D_AccLimit>,<ECEF_X>,<ECEF_Y>,<ECEF_Z>[,<Distance>]*<CS><CR><LF>   查询成功
$PQTMCFGSVIN,ERROR,<ErrCode>*<CS><CR><LF>                               失败
```

实测示例：

```
$PQTMCFGSVIN,R*26
$PQTMCFGSVIN,OK,1,3600,1.2,0.0000,0.0000,0.0000,0.0*75     Survey-in 生效中（ECEF 为 0 是正常的）
$PQTMCFGSVIN,OK,2,0,0.0,-2519265.0514,4849534.9045,3277834.6432,0.0*65   固定模式
$PQTMCFGSVIN,OK,0,0,0.0,0.0000,0.0000,0.0000*40           Survey-in 被禁用
```

**参数** `[原文]`

| 字段 | 格式 | 单位 | 取值 / 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `<Mode>` | 十进制 | — | 0 / 1 / 2 | 0 = 禁用；1 = Survey-in 模式；2 = 固定（Fixed）模式（ARP 位置采用 ECEF 坐标） |
| `<CFG_CNT>` | 十进制 | 次 | 范围 **0~86400** | Survey-in 模式下**最少定位次数** |
| `<3D_AccLimit>` | 十进制 | 米 | 范围 **[0.0, 1000.0]**，**默认 0.0** | Survey-in 模式下的 3D 位置精度门限；**该字段为 0 时表示无 3D 定位的位置精度限制** |
| `<ECEF_X>` | 十进制 | 米 | — | WGS84 ECEF X 坐标 |
| `<ECEF_Y>` | 十进制 | 米 | — | WGS84 ECEF Y 坐标 |
| `<ECEF_Z>` | 十进制 | 米 | — | WGS84 ECEF Z 坐标 |
| `<Distance>` | 十进制 | 米 | 范围 **[0.0, 10.0]**，默认 0.0 | 自收敛位置保持：当前收敛结果与上次结果的距离，大于设定值 → 输出本次结果；小于 → 沿用上次结果。**仅 `<Mode> = 1` 时有效**。0.0 = 始终使用最新的 Survey-in 结果 |

**生效条件**

| 项 | 结论 |
| --- | --- |
| 需要 `PQTMSAVEPAR` | 是（不保存则重新上电恢复默认值） |
| 需要重启 | **文档流程里是"保存 → 重启"**（基站指导 3.2.1 / 3.2.2 均为该顺序） |
| 重启等待时长 | `[未规定]`，等 `$PQTMVER` 首帧 |
| 特例 | **收敛后位置变了要重算**：文档说"若 Survey-in 模式配置未发生改变，则无需保存配置，重新执行 PQTMCFGSVIN 后，再重启模块即可" |

**注意（都是文档明写的，不是推测）**
1. `<3D_AccLimit>` **默认 0.0 = 无精度限制**。也就是说你用 `W,1,3600,0,...` 下发，3600 个点不管
   多烂都会进平均值——这是移植到其他型号时最容易出错的地方（LC29H(BS) 默认是 15.0，见 §8.2）。
2. `<Distance>` **只有 8 个参数时才存在**，7 参数写法下没有；两种写法文档都有示例（`*0A` / `*0F`）。
3. 固定模式下 `<CFG_CNT>` 和 `<3D_AccLimit>` 传 0 即可（文档示例 `W,2,0,0,X,Y,Z`）。
4. 查询返回的 ECEF 是**配置值**，Survey-in 模式下它就是 `0.0000`——**不是收敛结果**。
   要看真实收敛坐标，用 `PQTMSVINSTATUS`（§5.2）或抓 RTCM3-1005。
5. **协议规范 2.3.22 的"失败返回"写的是 `$PQTMGNSSSTOP,ERROR,<ErrCode>`，是文档笔误**，
   实际应为 `$PQTMCFGSVIN,ERROR,<ErrCode>`。本文照原文标注，实现时按 `$PQTMCFGSVIN,ERROR` 解析。
6. 基站位置误差**直接转化为移动站位置误差**（文档原话），所以固定模式写坐标前必须确认 ECEF 是
   **天线相位中心（ARP）**的坐标，不是地面标石坐标。

---

### 5.2 `PQTMSVINSTATUS` — Survey-in 状态输出（模块主动输出）

**开启方式**

```
$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1*58      每定位一次输出一次   [原文]
$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,0,1*59      关闭输出             [原文]
```

（`PQTM` 语句的速率配置**必须带 `<MsgVer>`**，否则报错——协议规范 2.3.15 备注 1。）

**返回** `[原文]`

```
$PQTMSVINSTATUS,<MsgVer>,<TOW>,<Valid>,<Res0>,<Res1>,<Obs>,<CfgDur>,<MeanX>,<MeanY>,<MeanZ>,<MeanAcc>*<CS><CR><LF>
```

实测示例：

```
$PQTMSVINSTATUS,1,291264000,1,,11,1,60,-2005560.2218,5411825.5447,2706139.7061,1.8691*0C     收敛中（1/60）
$PQTMSVINSTATUS,1,291323000,2,,11,60,60,-2005559.8481,5411823.1873,2706139.3995,1.8075*3F    收敛完成（60/60）
```

| 字段 | 格式 | 单位 | 说明 |
| --- | --- | --- | --- |
| `<MsgVer>` | 十进制 | — | 语句版本，总为 1 |
| `<TOW>` | 十进制 | 毫秒 | GPS 时间，周内秒 |
| `<Valid>` | 十进制 | — | **0 = 无效；1 = 正在进行；2 = 有效** |
| `<Res0>` | 十进制 | — | 预留，一直为空 |
| `<Res1>` | 十进制 | — | 预留 |
| `<Obs>` | 十进制 | 次 | Survey-in 期间使用的当前位置观测数 |
| `<CfgDur>` | 十进制 | 次 | 即 `PQTMCFGSVIN` 里配置的 `<CFG_CNT>` |
| `<MeanX>` / `<MeanY>` / `<MeanZ>` | 十进制 | 米 | 当前 Survey-in 的**平均位置** ECEF 坐标 |
| `<MeanAcc>` | 十进制 | 米 | 当前平均位置精度 |

**生效条件**：输出类。开启/关闭要 `SAVEPAR` + 重启才持久（基站指导示例里就是这么做的）。

**注意**
1. **`<Valid>` = 2 才是收敛完成**，别用 `<Obs> == <CfgDur>` 单独判断（文档示例 59/60 时仍是 1）。
2. 除 LG291P(00) 外，**必须在基站模式下**才能执行这条命令（协议规范 2.3.23 备注）。
3. 收敛计数不涨的官方解释（基站指导 FAQ 2）：
   > "只有在当前基站坐标满足设置的 3D 位置精度阈值时，收敛计数才会累加。上述情况可能是由于 3D
   > 位置精度阈值设置过低或是当前坐标精度不足，未能满足 3D 位置精度阈值的要求。"
   即阈值**数值设得过小 = 条件过严**，会让 `<Obs>` 卡住不动。
4. **别把这条语句透传给移动站**——基站指导 备注 2：移动站会回"不支持该命令"的错误码。
   所以收敛完成后要 `W,PQTMSVINSTATUS,0,1` 关掉，再 `SAVEPAR` + 重启。
5. 字段名与 LC29H(BS) 不同（那边叫 `<CurrentCNT>`），见 §8.2，正则别写死一套。

---

## 6. 四、报文输出配置类

### 6.1 `PQTMCFGMSGRATE` — 设置/查询语句输出速率

**发送（三种形态，别混）** `[原文]`

```
$PQTMCFGMSGRATE,W,<MsgName>,<Rate>[,<MsgVer/Offset>]*<CS><CR><LF>                     设置当前端口
$PQTMCFGMSGRATE,W,<PortType>,<PortID>,<MsgName/MsgID>,<Rate>[,<MsgVer/Offset>]*<CS><CR><LF>   设置指定端口
$PQTMCFGMSGRATE,R,<MsgName>[,<MsgVer/Offset>]*<CS><CR><LF>                            查询当前端口
$PQTMCFGMSGRATE,R,<PortType>,<PortID>,<MsgName/MsgID>[,<MsgVer/Offset>]*<CS><CR><LF>  查询指定端口
```

常用实例（未标注者为 `[原文]`；标 `推导` 的是文档只给格式、没给该具体取值，已按 §9.1 复算校验和）：

```
$PQTMCFGMSGRATE,W,GGA,1*0A                NMEA GGA 每定位一次输出一次
$PQTMCFGMSGRATE,W,RMC,1*17                NMEA RMC（推导）
$PQTMCFGMSGRATE,W,GSA,1*1E                NMEA GSA（推导）
$PQTMCFGMSGRATE,W,GSV,1*09                NMEA GSV（推导）
$PQTMCFGMSGRATE,W,RTCM3-1005,1*59         1005 每定位一次输出一次
$PQTMCFGMSGRATE,W,RTCM3-1006,1*5A         1006（推导）
$PQTMCFGMSGRATE,W,RTCM3-1033,1*5C         1033 天线信息（推导）
$PQTMCFGMSGRATE,W,RTCM3-107X,1,0*2F       107X（GPS MSM）每 1 次、偏移时延 0
$PQTMCFGMSGRATE,W,RTCM3-107X,5,0*2B       107X 每 5 次（推导）
$PQTMCFGMSGRATE,W,RTCM3-1019,1*54         GPS 星历（推导）
$PQTMCFGMSGRATE,W,1,1,GGA,1*0A            指定 UART1 的 GGA 速率
```

**返回** `[原文]`

```
$PQTMCFGMSGRATE,OK*<CS><CR><LF>                                                  设置成功
$PQTMCFGMSGRATE,OK,[<PortType>,<PortID>,]<MsgName>,<Rate>[,<MsgVer/Offset>]*<CS><CR><LF>   查询成功
$PQTMCFGMSGRATE,ERROR,<ErrCode>*<CS><CR><LF>                                     失败
```

实测示例：`$PQTMCFGMSGRATE,OK,GGA,1*59` / `$PQTMCFGMSGRATE,OK,1,1,GGA,1*59`

**参数** `[原文]`

| 字段 | 格式 | 单位 | 取值 | 说明 |
| --- | --- | --- | --- | --- |
| `<PortType>` | 十进制 | — | 1 | 端口类型，1 = UART |
| `<PortID>` | 十进制 | — | 1 / 2 / 3 | 1 = UART1，2 = UART2，3 = UART3 |
| `<MsgName/MsgID>` | 字符串/十六进制 | — | 见下表 | 语句名称 |
| `<Rate>` | 十进制 | — | **0** 或 **N** | 0 = 不输出；N = 每 N 次定位输出一次 |
| `<MsgVer/Offset>` | 十进制 | — | 见说明 | PQTM 语句 → 语句版本（**必填**）；RTCM MSM → 偏移时延，范围 0 ~ `<Rate>`−1；其他语句（NMEA 标准语句、1005/1006/1019 等）→ **省略** |

**支持的语句与速率范围**（协议规范 表 6）`[原文]`

| 语句 | 输出频率范围 N |
| --- | --- |
| RMC / GGA / GSV / GSA / VTG / GLL / GBS / GNS / GST / ZDA / HDT / THS | 1~255 |
| PQTMEPE / PQTMVEL / PQTMGEOFENCESTATUS / PQTMTXT / PQTMSVINSTATUS / PQTMPVT / PQTMDOP / PQTMPL / PQTMODO / PQTMTAR / PQTMNAV | 1~255 |
| RTCM3-1005 / 1006 / 1033 | 1~1200 |
| RTCM3-107X（GPS）/ 108X（GLONASS）/ 109X（Galileo）/ 111X（QZSS）/ 112X（BDS）/ 113X（NavIC） | 1~1200 |
| RTCM3-1019 / 1020 / 1041 / 1042 / 1044 / 1046（星历） | **0（关闭）/ 1（开启）** |
| RAW-PPPB2B(0AB2) / RAW-QZSSL6(0AB6) / RAW-HASE6(0AE6) | 0（关闭）/ 1（开启） |

**生效条件**

| 项 | 结论 |
| --- | --- |
| 需要 `PQTMSAVEPAR` | 是 |
| 需要重启 | 是（基站指导里每条速率配置后都跟 `SAVEPAR` + `SRR`；`[建议]` 生产前统一重下发一遍更稳） |
| 紧急关闭 | 不保存也能立即生效（运行态），只是断电回默认 |

**注意（前 3 条是文档备注原文）**
1. 配置 **PQTM 语句必须带 `<MsgVer>`**，否则返回错误。
2. 配置 NMEA 标准语句 / RTCM 语句（**不含** RTCM MSM）时，`<MsgVer>` 必须**省略**。
3. **所有 RTCM MSM 语句的 `<Rate>` 取值相同，且仅最后一次设置有效**。想 107X 每 1 次、112X 每 5 次
   是做不到的——MSM 全星座共用一档。
4. `<Offset>` 影响 MSM 输出时刻：`输出时间 = <FixInterval> × <Rate> + <FixInterval> × <Offset>`。
   若新 `<Rate>` 导致旧 `<Offset>` 越界（不在 0 ~ `<Rate>`−1），冲突项须归零，"该过程由软件自动完成"。
5. **RTCM 星历语句的输出速率与 `<FixInterval>` 无关**；`0/1` 是开关不是倍数。
6. **GSA 和 GSV 输出速率固定为 1 Hz，与 `<FixInterval>` 无关**（LG291P(00) 例外：默认 10，且与
   `<FixInterval>` 有关）。
7. 星历要真正出来，需先用 `PQTMCFGRTCM` 设 `<EPH_Mode>`，再用本命令打开对应语句
   （基站指导 2.2.2 备注）——两步缺一不可。
8. **基站模式默认只开 MSM4 + 1005**。想发 1033（天线信息）/1006（天线高）/星历，必须逐条打开。
9. 带宽不够导致丢数据时，官方给的解法就是**降速率或提波特率**（基站指导 FAQ 4）。

**实测补充（LG290P03AANR01A03S / 2024-05-22，与文档有偏差，以本节为准）**

| # | 文档写法 | 实测结果 | 驱动做法 |
| --- | --- | --- | --- |
| 1 | 按星座逐条配 `RTCM3-1074/1084/1094/1124` | 四条**全部** `ERROR,1` | 统一写 `RTCM3-107X`（全星座共用一档，与注意 3 呼应） |
| 2 | `<Offset>` 标"MSM 可带" | `W,RTCM3-107X,1 → ERROR,1`；`W,RTCM3-107X,1,0 → OK` | MSM **必须**带 `<Offset>`，且 `0 ≤ Offset < Rate` |
| 3 | N 的范围 1~1200 | 对从未配置过的模块读 `R,RTCM3-1005 → OK,RTCM3-1005,0` | 允许下发 `0`（= 不输出）：一键还原要靠它把标定打开的 1005 关回去 |
| 4 | MSM 查询的返回格式 | `R,RTCM3-107X → OK,RTCM3-107X,1,0`（比 NMEA 多一个字段） | 速率取**第 2 个**字段；用 `fields[-1]` 会把偏移时延当成速率 |
| 5 | GSA / GSV 可配速率 | 命令回 `OK`，实际速率不变（注意 6 已说明） | 写入时记警告，不当失败处理 |

---

### 6.2 `PQTMCFGRTCM` — RTCM 功能配置（MSM 类型 / 仰角 / 星历）

**发送**

```
$PQTMCFGRTCM,W,<MSM_Type>,<MSM_Mode>,<MSM_ElevThd>,<Reserved>,<Reserved>,<EPH_Mode>,<EPH_Interval>*<CS><CR><LF>
$PQTMCFGRTCM,R*<CS><CR><LF>
```

两条常用（`[原文]`）：

```
$PQTMCFGRTCM,W,4,0,-90.0,07,06,1,0*3B       MSM4，仰角不限，星历更新时输出
$PQTMCFGRTCM,W,4,0,-90,07,06,1,0*25         同上（基站指导写法，仰角无小数位）
$PQTMCFGRTCM,W,7,0,5,07,06,2,3600*01        MSM7，仰角≥5°，星历更新时 + 每 3600 s 输出一次
```

**返回** `[原文]`

```
$PQTMCFGRTCM,OK*<CS><CR><LF>
$PQTMCFGRTCM,OK,<MSM_Type>,<MSM_Mode>,<MSM_ElevThd>,<Reserved>,<Reserved>,<EPH_Mode>,<EPH_Interval>*<CS><CR><LF>
$PQTMCFGRTCM,ERROR,<ErrCode>*<CS><CR><LF>
```

实测示例：`$PQTMCFGRTCM,OK,4,0,-90.0,07,06,1,0*68`

| 字段 | 格式 | 单位 | 取值 / 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `<MSM_Type>` | 十进制 | — | 范围 **3~7**，**默认 4** | 对应 RTCM MSM3~MSM7 |
| `<MSM_Mode>` | 十进制 | — | **总为 0** | 0 = 未搜到星时不输出 RTCM MSM 语句 |
| `<MSM_ElevThd>` | 十进制 | 度 | 范围 **[-90.0, 90.0]**，**默认 -90.0** | 卫星仰角阈值，-90.0 表示无限制 |
| `<Reserved>` | 十进制 | — | **默认 07** | 预留（照抄，别改） |
| `<Reserved>` | 十进制 | — | **默认 06** | 预留（照抄，别改） |
| `<EPH_Mode>` | 十进制 | — | 0 / 1 / 2 / 3 | 0 = 禁用；1 = 更新时输出；2 = 更新时输出 + 按 `<EPH_Interval>` 间隔输出；3 = 每个历元输出 |
| `<EPH_Interval>` | 十进制 | 秒 | 范围 **0~7200**，**默认 0** | 星历输出间隔，**仅 `<EPH_Mode> = 2` 时有效** |

**生效条件**：`SAVEPAR` + 重启（同 §2.3）。

**注意**
- 两个 `<Reserved>` **必须原样带 `07,06`**，文档示例里从来不变。
- **`-90.0` 会被拒**：真机（LG290P03AANR01A03S / 2024-05-22）实测
  `W,4,0,-90.0,07,06,1,0 → ERROR,1`（参数不合法），而 `W,4,0,-90,07,06,1,0 → OK`，
  且 `R` 的回显是 `OK,4,0,-90,07,06,1,0`。文档 §6.2 示例里两种写法都出现过并注明
  "模块都接受"，**这块固件不接受带小数位的 `-90.0`**。驱动统一写整数 `-90`。
- `MSM_Type` 只在这里定一次，**全星座共用**（与 §6.1 备注 3 呼应）。
- **还原必须整条回放这 7 个字段**：只按 `<MSM_Type>` 重配会把 `<EPH_Mode>` /
  `<EPH_Interval>` 一并写回默认值 `1/0`——用户原来"每 3600 s 输出一次星历"就静默丢了。
  驱动为此提供 `restore_rtcm_config(fields)`：`R` 读到的 7 个字段原样拼回去，
  逐字段白名单（只放行数字/负号/小数点）后再拼命令。

---

### 6.3 `PQTMCFGFIXRATE` — 定位间隔

**发送**

```
$PQTMCFGFIXRATE,W,<FixInterval>*<CS><CR><LF>
$PQTMCFGFIXRATE,R*<CS><CR><LF>
```

```
$PQTMCFGFIXRATE,W,1000*59        1 Hz   [原文]
$PQTMCFGFIXRATE,R*71                    [原文]
```

**返回** `[原文]`

```
$PQTMCFGFIXRATE,OK*<CS><CR><LF>
$PQTMCFGFIXRATE,OK,<FixInterval>*<CS><CR><LF>
$PQTMCFGFIXRATE,ERROR,<ErrCode>*<CS><CR><LF>
```

示例：`$PQTMCFGFIXRATE,OK,1000*0A`

| 参数 | 格式 | 单位 | 说明 |
| --- | --- | --- | --- |
| `<FixInterval>` | 十进制 | 毫秒 | 定位间隔 |

**生效条件**：`SAVEPAR` + 重启。

**注意（关键，直接影响 Survey-in 时长估算）**
- **基站模式下定位速率固定 1 Hz 且不支持更改**（LG291P(00) 例外，可改）。
  → 所以 Survey-in 的 `<CFG_CNT>` 基本等于"秒数"：60 ≈ 1 分钟，3600 ≈ 1 小时。
- 移动站模式默认 10 Hz。
- 文档**没有给 `<FixInterval>` 的合法范围**，只给了 `1000` 的示例；其它值属 `[未规定]`，别乱试。

---

### 6.4 `PQTMCFGANTINF` — 天线/设备信息（进 RTCM3-1033）

**发送**

```
$PQTMCFGANTINF,W,<AntDsc>,<AntSetupID>,<AntSN>*<CS><CR><LF>
$PQTMCFGANTINF,R*<CS><CR><LF>
```

```
$PQTMCFGANTINF,W,,0,*27                          全空（[原文] 示例，注意末尾留一个逗号）
$PQTMCFGANTINF,W,2RTK-GNSS-01,0,SN0001*4C        带描述与 SN（推导）
$PQTMCFGANTINF,R*3E
```

**返回** `[原文]`

```
$PQTMCFGANTINF,OK*<CS><CR><LF>
$PQTMCFGANTINF,OK,<AntDsc>,<AntSetupID>,<AntSN>*<CS><CR><LF>
$PQTMCFGANTINF,ERROR,<ErrCode>*<CS><CR><LF>
```

示例：`$PQTMCFGANTINF,OK,,0,*74`

| 参数 | 格式 | 单位 | 取值 | 说明 |
| --- | --- | --- | --- | --- |
| `<AntDsc>` | 字符串 | — | **最多 31 字符**，默认空 | 天线描述，输出到 **RTCM3 DF030** |
| `<AntSetupID>` | 十进制 | — | **[0, 255]**，默认 0 | 天线设置 ID，输出到 **RTCM3 DF031** |
| `<AntSN>` | 字符串 | — | **最多 31 字符**，默认空 | 天线序列号，输出到 **RTCM3 DF033** |

**生效条件**：`SAVEPAR` + 重启。

**注意**
- 只写 `PQTMCFGANTINF` **不会**让 1033 出现——还要 `PQTMCFGMSGRATE,W,RTCM3-1033,N` 打开它
  （基站模式默认只开 MSM4 + 1005，见 §6.1 注意 8）。
- 31 字符是**上限**，超了会被拒或截断（文档只说"最多 31"）；含逗号会破坏参数分隔，**字符串内禁止逗号**。
- LG291P(00)：**该语句正在开发中**（协议规范原文），量产前须向移远确认。

---

### 6.5 `PQTMCFGANTDELTA` — 参考点与天线距离（进 RTCM3-1006 DF028）

> ⚠️ **LG290P 不支持这条命令。** 协议规范 2.3.47 备注原文：
> "仅 LG291P (00) 模块支持该命令，该命令在开发中。"
> 也就是说：**LG290P 上没法用命令写天线高**，1006 的 DF028 恒为默认值。
> 若必须对外声明天线高，只能在数据链路上改流，或换型号。

**发送**（仅供 LG291P(00)，`[原文]` 格式）

```
$PQTMCFGANTDELTA,W,<East>,<North>,<Height>*<CS><CR><LF>
$PQTMCFGANTDELTA,R*<CS><CR><LF>
```

```
$PQTMCFGANTDELTA,W,0.0000,0.0000,0.0000*10
$PQTMCFGANTDELTA,R*27
```

**返回** `[原文]`

```
$PQTMCFGANTDELTA,OK*<CS><CR><LF>
$PQTMCFGANTDELTA,OK,<East>,<North>,<Height>*<CS><CR><LF>
$PQTMCFGANTDELTA,ERROR,<ErrCode>*<CS><CR><LF>
```

示例：`$PQTMCFGANTDELTA,OK,0.0000,0.0000,0.0000*43`

| 参数 | 格式 | 单位 | 取值 / 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `<East>` | 十进制 | 米 | **[-200.0000, 200.0000]**，默认 0.0000 | 参考点到天线的东向偏差 |
| `<North>` | 十进制 | 米 | **[-200.0000, 200.0000]**，默认 0.0000 | 参考点到天线的北向偏差 |
| `<Height>` | 十进制 | 米 | **[0.0000, 6.5535]**，默认 0.0000 | 参考点到天线的高度偏差，输出到 **RTCM3-1006 DF028** |

**注意**
- `<Height>` **不能为负**（下限 0.0000），上限 6.5535 m 是 16 位定点带来的硬限制。
- 该命令的 East/North **不进 1006**，文档只指明 `<Height>` 进 DF028；East/North 的用途文档未说明，
  属 `[未规定]`，不要自行假设它参与 ARP 改正。

---

### 6.6 `PQTMCFGNMEATID` — NMEA 设备标识符（Talker ID）

**发送**

```
$PQTMCFGNMEATID,W,<Main_TalkerID>,<GSV_TalkerID>*<CS><CR><LF>
$PQTMCFGNMEATID,R*<CS><CR><LF>
```

```
$PQTMCFGNMEATID,W,GP,0*58      强制 GP 前缀      [原文]
$PQTMCFGNMEATID,W,00,0*4F      恢复自动（按星系） [原文]
$PQTMCFGNMEATID,R*7A
```

**返回** `[原文]`

```
$PQTMCFGNMEATID,OK*<CS><CR><LF>
$PQTMCFGNMEATID,OK,<Main_TalkerID>,<GSV_TalkerID>*<CS><CR><LF>
$PQTMCFGNMEATID,ERROR,<ErrCode>*<CS><CR><LF>
```

示例：`$PQTMCFGNMEATID,OK,GP,0*0B`

| 参数 | 格式 | 单位 | 取值 | 说明 |
| --- | --- | --- | --- | --- |
| `<Main_TalkerID>` | **字符** | — | 2 个字符 / `00` | 除 GSV 外所有标准 NMEA 语句的主标识符。`00` = 自主模式（由 GNSS 星系配置决定）；非 `00` 时用该两字符表示标识符 |
| `<GSV_TalkerID>` | 十进制 | — | 0 / 1 | 0 = 由 GNSS 星系配置决定；1 = 与 `<Main_TalkerID>` 同值 |

**生效条件**：`SAVEPAR` + 重启。

**注意**
- 老式 NTRIP/RTK 软件常只认 `$GPGGA`，把这里设成 `GP` 兼容性最好；但它是**字符型**参数，
  传 `GP` 而不是 `1`。
- GSV 是特殊项：`<GSV_TalkerID>=0` 时仍按星系给 `$GPGSV`/`$BDGSV`/`$GLGSV`……

---

### 6.7 `PQTMCFGNMEADP` — NMEA 小数位数

**发送**

```
$PQTMCFGNMEADP,W,<UTC_DP>,<POS_DP>,<ALT_DP>,<DOP_DP>,<SPD_DP>,<COG_DP>*<CS><CR><LF>
$PQTMCFGNMEADP,R*<CS><CR><LF>
```

```
$PQTMCFGNMEADP,W,3,8,3,2,3,2*39      出厂默认组合   [原文]
$PQTMCFGNMEADP,R*37
```

**返回** `[原文]`

```
$PQTMCFGNMEADP,OK*<CS><CR><LF>
$PQTMCFGNMEADP,OK,<UTC_DP>,<POS_DP>,<ALT_DP>,<DOP_DP>,<SPD_DP>,<COG_DP>*<CS><CR><LF>
$PQTMCFGNMEADP,ERROR,<ErrCode>*<CS><CR><LF>
```

示例：`$PQTMCFGNMEADP,OK,3,8,3,2,3,2*6A`

| 参数 | 范围 | 默认 | 说明 |
| --- | --- | --- | --- |
| `<UTC_DP>` | 0~3 | 3 | UTC 秒的小数位数，0 = 无小数部分 |
| `<POS_DP>` | **0~8** | 8 | 纬度/经度小数位数 |
| `<ALT_DP>` | 0~3 | 3 | 海拔高度与大地水准面间距小数位 |
| `<DOP_DP>` | 0~3 | 2 | DOP 小数位 |
| `<SPD_DP>` | 0~3 | 3 | 速度小数位 |
| `<COG_DP>` | 0~3 | 2 | COG 小数位 |

**生效条件**：`SAVEPAR` + 重启。

**注意**：6 个参数**全部必填**，不能只改一个；想只调经纬度，其余按默认值抄回去。

---

### 6.8 `PQTMCFGUART` — UART 接口参数

**发送**

```
$PQTMCFGUART,W,<BaudRate>[,<DataBit>,<Parity>,<StopBit>,<FlowCtrl>]*<CS><CR><LF>          当前端口
$PQTMCFGUART,W,<Index>,<BaudRate>[,<DataBit>,<Parity>,<StopBit>,<FlowCtrl>]*<CS><CR><LF>  指定端口
$PQTMCFGUART,R[,<Index>]*<CS><CR><LF>
```

```
$PQTMCFGUART,W,115200*18              当前接口设 115200     [原文]
$PQTMCFGUART,W,1,115200*05            UART1 设 115200       [原文]
$PQTMCFGUART,W,115200,8,0,1,0*11      当前接口全参数         [原文]
$PQTMCFGUART,R*36                      查询                  [原文]
```

**返回** `[原文]`

```
$PQTMCFGUART,OK*<CS><CR><LF>
$PQTMCFGUART,OK,<Index>,<BaudRate>,<DataBit>,<Parity>,<StopBit>,<FlowCtrl>*<CS><CR><LF>
$PQTMCFGUART,ERROR,<ErrCode>*<CS><CR><LF>
```

示例：`$PQTMCFGUART,OK,1,115200,8,0,1,0*5F`

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `<Index>` | 1 / 2 / 3 | UART1 / UART2 / UART3 |
| `<BaudRate>` | **9600、115200、230400、460800、921600** | 只有这 5 档 |
| `<DataBit>` | 8 | 8 位 |
| `<Parity>` | 0 / 1 / 2 / 3 / 4 | 0 无校验、1 奇、2 偶、3 1 校验、4 0 校验 |
| `<StopBit>` | 1 / 2 | 停止位 |
| `<FlowCtrl>` | 0 | 无流控（只支持这一种） |

**生效条件**：`SAVEPAR` + 重启（`[建议]` 立即按新波特率切换上位机，否则后续命令全部失联）。

**注意**
- **没有 4800 / 19200 / 38400 / 57600**。上位机端口下拉框要按这 5 档做，别列通用列表。
- 改完当前端口波特率后，**同一条口上的应答也变了**——发完立刻切，别等回包。

---

## 7. 五、工具类

### 7.1 `PQTMSAVEPAR` — 保存配置到 NVM

**发送**

```
$PQTMSAVEPAR*<CS><CR><LF>
```

```
$PQTMSAVEPAR*5A
```

**返回** `[原文]`

```
$PQTMSAVEPAR,OK*<CS><CR><LF>
$PQTMSAVEPAR,ERROR,<ErrCode>*<CS><CR><LF>
```

示例：`$PQTMSAVEPAR,OK*72`

**生效条件**：本身即刻完成写入；**所有 `W` 类配置都需要它 + 重启**才持久。

**注意**
- 它不是"给某一条命令保存"，而是**把当前全部运行态参数一次性写进 NVM**。
- 顺序必须是：`W...` → `SAVEPAR` → 重启。中途断电 = 前面全白做。
- `PQTMDEBUGON/OFF` 的调试状态也能被它保存（协议规范 2.3.26）。

---

### 7.2 `PQTMRESTOREPAR` — 恢复出厂参数

**发送**

```
$PQTMRESTOREPAR*<CS><CR><LF>
```

```
$PQTMRESTOREPAR*13
```

**返回** `[原文]`

```
$PQTMRESTOREPAR,OK*<CS><CR><LF>
$PQTMRESTOREPAR,ERROR,<ErrCode>*<CS><CR><LF>
```

示例：`$PQTMRESTOREPAR,OK*3B`

**生效条件**：**重启后生效**（`[原文]`："该命令重启后生效"）。

**注意**
- 会清掉**所有** PQTM 配置，包括工作模式、SVIN 坐标、语句速率。基站上执行 = 直接失去基站身份。
- 执行前先 `PQTMVERNO` / `PQTMSN` 记录设备身份，便于事后核对是不是同一台。

---

### 7.3 `PQTMSRR` — 系统复位（软重启）

**发送**

```
$PQTMSRR*<CS><CR><LF>
```

```
$PQTMSRR*4B
```

**返回**

```
（无）本命令文档未给出 OK / ERROR 应答，见下方"注意"
```

**生效条件**：立即复位，接收机重新启动。

**注意**
- 协议规范 2.3.5 对 `PQTMSRR` **只给了命令格式和示例，没有"结果"小节**（对比 `PQTMSAVEPAR`
  明确写了 `$PQTMSAVEPAR,OK*72`）。所以**不要等 `$PQTMSRR,OK`**，等 `$PQTMVER` 首帧（§3.4）。
- 基站指导原文："重启模块（通过发送相关命令或硬件 RESET_N 引脚重启均可，此处使用命令重启）"
  —— 命令复位和 RESET_N 引脚等效。
- 文档**没有**给重启耗时，见 §2.3。

---

### 7.4 `PQTMCOLD` / `PQTMWARM` / `PQTMHOT` — 冷/温/热启动

**发送** `[原文]`

```
$PQTMCOLD*1C      冷启动
$PQTMWARM*11      温启动
$PQTMHOT*4B       热启动
```

**返回**：文档各节均只给命令格式与示例，**未列 `OK/ERROR` 应答**。

**生效条件**：立即执行，不需保存、不需 `SAVEPAR`。

**注意**
- 这三条是**定位引擎启动方式**，不是配置复位：不清 NVM 配置，只影响星历/时间的使用程度。
- 与 `PQTMRESTOREPAR` 完全不是一回事（那条才清配置）。
- 基站上做标定时**不要**用冷启动——冷启动后重新搜星、重新收敛，Survey-in 会从头再来。

---

## 8. 不同模块的指令集差异

> 这一节只写**文档明确写了差异**的地方。文档没提的，一律标 `[未列]`，不推断成"支持"或"不支持"。
> 移远在每份协议规范首页都有同一句话：**"请使用本文档列出的命令控制或配置模块；对本文档未列出的
> 其他命令，移远通信不承担任何责任。"** —— 所以 `[未列]` 在工程上应按"不要用"处理。

### 8.1 LG29xP / LG58xP / LG680P 内部差异（`$PQTM` 指令集）

| 命令 | LG290P(03) | LG291P(00) | LG291P(03) / LG581P(03) | LG580P(03) / LG680P(03) |
| --- | --- | --- | --- | --- |
| `PQTMCFGRCVRMODE` | 支持，需保存+重启 | 支持，**立即生效**（不保存则重启回退） | 支持，需保存+重启 | 支持，需保存+重启 |
| `PQTMCFGSVIN`（8 参数含 `<Distance>`） | 支持 | 支持 | 支持 | 支持 |
| `PQTMSVINSTATUS` | **必须处于基站模式** | **不受此限制** | 必须处于基站模式 | 必须处于基站模式 |
| `PQTMCFGFIXRATE` | 基站模式固定 1 Hz，**不可改** | 基站模式可改 | 基站模式固定 1 Hz | 基站模式固定 1 Hz |
| `PQTMCFGRTCM` | 支持 | 支持 | 支持 | 支持 |
| `PQTMCFGMSGRATE` GSA/GSV | 固定 1 Hz，与 FixInterval 无关 | **默认 10，与 FixInterval 有关** | 同 LG290P | 同 LG290P |
| `PQTMCFGANTINF` | 支持 | **开发中** | 支持 | 支持 |
| `PQTMCFGANTDELTA` | **不支持**（原文：仅 LG291P(00) 支持） | 支持（**开发中**） | 不支持 | 不支持 |
| `PQTMCFGSBAS` 默认值 | **0x003F** | **0x0008**，仅 BDSBAS | **0x0008**，仅 BDSBAS | `[未列]`（文档只给 LG290P(03) 与 LG291P/LG581P 的默认值） |
| `PQTMCFGBLD`（双天线基线） | **不支持** | 不支持 | **LG581P(03) 支持** | **LG580P(03) 支持**；LG680P 未列 |
| `PQTMCFGRTK` | 支持 | **不支持** | 支持 | 支持 |
| `PQTMCFGRTKSRCTYPE` | 支持 | **不支持** | 支持 | 支持 |
| `PQTMSN` / `PQTMUNIQID` | 支持 | 支持 | 支持 | 支持 |
| 星系 | GPS/GLONASS/Galileo/BDS/QZSS/NavIC | **单 BDS** | **单 BDS** | 全星系 |

**只看这一张表就能避免的坑**：把 LG290P 的配置脚本直接烧到 LG291P(00) 上，`PQTMCFGRTK` 会回
`ERROR,3`（不支持），而 `PQTMCFGRCVRMODE` 因为"立即生效"，会让你误以为"配置已生效"，
实际**重启后全部回退**。

> 表格读法：**只有带 `[未列]` 或明确写"不支持/开发中"的单元格才是文档明文**；其余单元格表示协议规范
> **未针对该型号单列差异**（按同系列同行为处理），**不是逐型号实测结论**。量产前请按具体型号再核一遍
> 对应版本的协议规范——移远的型号批次（如 LG580P(03) 与 LG580P(06)）差异不会写在同一份表里。

### 8.2 LC29H(BS)：完全不同的指令集（`$PQTM` 7 条 + `$PAIR` 8 条）

文档 [4] 只列了 15 条消息，**没有** `PQTMCFGRCVRMODE`、`PQTMCFGRTCM`、`PQTMCFGSBAS`、
`PQTMSN`、`PQTMUNIQID`、`PQTMCFGANTINF`、`PQTMCFGUART`。按首页声明，这些命令在 LC29H(BS) 上不应使用。

**`$PQTM` 部分（7 条）**

| 命令 | 与 LG290P 的差异 |
| --- | --- |
| `PQTMCFGSVIN` | **只有 7 个参数，没有 `<Distance>`**；`<CFG_CNT>` 默认 **43200**（LG290P 无默认说明）；`<3D_AccLimit>` 默认 **15.0**（LG290P 是 **0.0 = 无限制**） |
| `PQTMSAVEPAR` | 同形（`$PQTMSAVEPAR*5A`） |
| `PQTMRESTOREPAR` | 同形 |
| `PQTMVERNO` | 同形，示例 `$PQTMVERNO,LC29HBSNR01A01S,2022/08/31,15:22:59*27` |
| `PQTMCFGMSGRATE` | **格式不同**：`W,<MsgName>,<Rate>,<MsgVer>` 的 `<MsgVer>` **必填且只能是 1 或 2**；仅支持 `$PQTMSVINSTATUS`（1~20）与 `$PQTMEPE`（1~20），**不支持 RTCM/NMEA 速率配置** |
| `PQTMEPE` | 输出类，`<MsgVer>` 恒为 2 |
| `PQTMSVINSTATUS` | 字段名不同：第 6 位是 **`<CurrentCNT>`**（LG290P 叫 `<Obs>`） |

**`$PAIR` 部分（8 条）—— LG290P 完全没有这套**

| 命令 | 作用 | 关键取值 |
| --- | --- | --- |
| `$PAIR001` | 所有 PAIR 命令的应答 | `<Result>`：0 成功、1 处理中、2 失败、3 不支持该 CommandID、4 参数错误（含校验和错）、5 MNL 忙 |
| `$PAIR432,<Mode>` | 设置 RTCM 输出模式 | **-1 = 禁用；0 = MSM4；1 = MSM7** |
| `$PAIR433` | 查询 RTCM 输出模式 | 回 `$PAIR433,<Mode>` + `$PAIR001,433,0` |
| `$PAIR434,<Enable>` | 启用/禁用 1005（静态天线参考点） | 0 禁用 / 1 启用 |
| `$PAIR435` | 查询 1005 输出状态 | 回 `$PAIR435,<Enable>` |
| `$PAIR436,<Enable>` | 启用/禁用 RTCM 星历输出 | 0 / 1 |
| `$PAIR437` | 查询星历输出状态 | 回 `$PAIR437,<Enable>` |
| `$PAIR864,<PortType>,<PortIndex>,<Baudrate>` | 设置 UART 波特率 | PortType 0=UART，PortIndex 0=UART1；波特率 **4800/9600/19200/38400/57600/115200/230400/460800/921600/3000000** |

```
$PAIR432,1*22        → $PAIR001,432,0*3E
$PAIR434,1*24        → $PAIR001,434,0*38
$PAIR864,0,0,115200*1B → $PAIR001,864,0*31     （以上均为 [原文] 示例）
```

**移植时的三个硬点**
1. LC29H(BS) **没有 MSM3~MSM7 连续选择**，只有 MSM4 / MSM7 两档（`$PAIR432`），也没有仰角阈值、
   星历间隔这些参数；LG290P 的 `PQTMCFGRTCM` 那套它一个都没有。
2. LC29H(BS) **错误码只有 1、2**，没有 3。上位机"命令不支持"的判定逻辑会失效。
3. 波特率档位不同：LC29H(BS) 的 `$PAIR864` 是**超集**——在 LG290P 那 5 档之外还多出
   `4800 / 19200 / 38400 / 57600 / 3000000`。反过来，LG290P 配不了这 5 档。
   **按型号分表下发**，别用一份通用列表：`[建议]` 严格按 §2.4 的 5 档做 LG290P 的下拉框。

### 8.3 LC29H(AA,BS) / LC79H(AL) 的 EASY & EPOC：与标定无关

文档 [5] 的 `$PAIR` 消息是 **AGNSS 星历预测**功能，不是基站标定，两者不要混：

| 功能 | EASY | EPOC |
| --- | --- | --- |
| 启用/禁用 | `$PAIR490` | `$PAIR496` |
| 配置星系 | — | `$PAIR498` |
| 清除 NVM 数据 | — | `$PAIR507` |
| 查询功能状态 | `$PAIR491` | `$PAIR508` |
| 查询预测状态 | `$PAIR491` | `$PAIR509` |
| 当前导航数据 RTC RAM → NVM | `$PAIR511` | `$PAIR511` |
| 清除 RTC RAM + NVM 导航数据 | `$PAIR512` | `$PAIR512` |
| 当前配置 RTC RAM → NVM | `$PAIR513` | `$PAIR513` |

差异要点（原文）：EASY 仅支持 GPS、可存 1/2/3 天；EPOC 支持 GPS+Galileo+BDS、只存 3 天、命令更多。

> **结论**：这三条 `$PAIR` 系列（432~437、864；490~513）与 LG290P 的 `$PQTM` 标定流程**没有任何交集**。
> 如果你的设备上跑的是 LG290P，这两个文档可以完全不看。

### 8.4 SPI 传输层（LC29H 系列，不适用于 LG290P 命令）

- 文档 [6]：SPI 是 **LC29H 系列**的接口，**与 I2C、UART1 引脚复用，不可与 I2C/UART1 同时使用**。
- 主机读写的是"消息"（NMEA/RTCM），**命令内容仍需按协议规范拼**，SPI 只是换了条物理通道。
- LG290P(03) 硬件手册列的通信接口是 **UART 和 I2C\***，**没有 SPI**。
  → 本报告所有命令对 LG290P 而言都是 **UART 上的 ASCII 文本**，不存在 SPI 帧头包装问题。

---

## 9. 附录 A：NMEA 校验和与组帧（Python）

### 9.1 校验和算法 `[原文]`

协议规范与 LC29HBS 规范的定义一致：

> "校验和是对语句中所有字符的 8 位（不包括起始和结束位）进行异或运算，所有字符是指在定界符
> `$` 与 `*` 之间，但不包括这些定界符的全部字符，包括 `,` 在内。"

```python
def nmea_checksum(body: str) -> str:
    """计算 NMEA 校验和。

    body: '$' 与 '*' 之间的全部字符（含所有逗号），不含 '$'、'*' 与 CRLF。
    返回两位大写十六进制字符串。
    """
    result = 0
    for ch in body.encode('ascii'):
        result ^= ch
    return f'{result:02X}'
```

自检（这些值就是文档里的原文示例，跑通说明实现正确）：

```python
assert nmea_checksum('PQTMSAVEPAR') == '5A'
assert nmea_checksum('PQTMCFGRCVRMODE,W,2') == '29'
assert nmea_checksum('PQTMCFGSVIN,W,1,3600,1.2,0,0,0') == '0A'
assert nmea_checksum('PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1') == '58'
assert nmea_checksum('PAIR864,0,0,115200') == '1B'
```

### 9.2 组帧与收帧（工程实现要点）

```python
def build(body: str) -> bytes:
    """body 不含 '$' 和 '*xx'。返回完整待发送字节（含 CRLF）。"""
    return f'${body}*{nmea_checksum(body)}\r\n'.encode('ascii')
```

```python
def parse(line: bytes):
    """返回 (body, checksum, ok)。ok=False 表示校验和错或帧不完整。"""
    if not line.startswith(b'$') or b'*' not in line:
        return None, None, False
    body, _, tail = line[1:].partition(b'*')
    text = body.decode('ascii', 'replace')
    want = tail[:2].decode('ascii', 'replace').upper()
    return text, want, (nmea_checksum(text) == want)
```

要点：

1. **必须以 `\r\n` 结尾发送**，只发 `\n` 不响应（§2.1）。
2. 收帧按 `\r\n` 切行，**不要按固定长度读**——`PQTMSVINSTATUS` 长度随数值位数变化。
3. **必须校验校验和**：基站模式下 UART 上同时有 RTCM 二进制流（`0xD3` 开头），文本行会与二进制
   交错，不校验会把二进制碎片当命令应答解析。
4. `[建议]` 命令与应答之间加超时（200~500 ms），超时重发 1 次；同一时刻**只允许一条命令在途**，
   `$PQTM` 应答不带序号，并发发送无法对应。

---

## 10. 附录 B：标准操作序列

以下两条序列**完全照抄**基站模式应用指导 3.2.1 / 3.2.2 的顺序，只补了"每步在等什么"。

### 10.1 序列一：固定坐标（已知 ECEF）

| # | 动作 | 报文 | 期望应答 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 切基站模式 | `$PQTMCFGRCVRMODE,W,2*29` | `$PQTMCFGRCVRMODE,OK*64` | 此步尚未生效 |
| 2 | 保存 | `$PQTMSAVEPAR*5A` | `$PQTMSAVEPAR,OK*72` | — |
| 3 | 重启 | `$PQTMSRR*4B` | 无 OK 应答 | **等 `$PQTMVER` 首帧** |
| 4 | 写 ECEF | `$PQTMCFGSVIN,W,2,0,0,<X>,<Y>,<Z>*<CS>` | `$PQTMCFGSVIN,OK*70` | 例中 `-2519265.0514,4849534.9045,3277834.6432` |
| 5 | （可选）开状态输出 | `$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1*58` | `$PQTMCFGMSGRATE,OK*29` | — |
| 6 | 保存 | `$PQTMSAVEPAR*5A` | `$PQTMSAVEPAR,OK*72` | — |
| 7 | 重启 | `$PQTMSRR*4B` | — | 等 `$PQTMVER` |
| 8 | **回读校验** | 抓 RTCM3-1005 二进制 | `D3 00 13 3E …` | 1005 在基站模式下**默认输出** |
| 9 | **回读校验** | `$PQTMSVINSTATUS` | `...,2,,00,0,0,<X>,<Y>,<Z>,0.0000*..` | `<Valid>=2`、`<Obs>=0`、`<MeanAcc>=0.0000` 是固定模式特征 |
| 10 | （可选）关状态输出 | `$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,0,1*59` | `$PQTMCFGMSGRATE,OK*29` | 文档建议看完关掉 |
| 11 | 保存 + 重启 | `$PQTMSAVEPAR*5A` → `$PQTMSRR*4B` | — | — |

### 10.2 序列二：Survey-in 自收敛

| # | 动作 | 报文 | 期望应答 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 切基站模式 | `$PQTMCFGRCVRMODE,W,2*29` | `$PQTMCFGRCVRMODE,OK*64` | — |
| 2 | 保存 | `$PQTMSAVEPAR*5A` | `$PQTMSAVEPAR,OK*72` | — |
| 3 | 重启 | `$PQTMSRR*4B` | — | 等 `$PQTMVER` |
| 4 | 配置收敛 | `$PQTMCFGSVIN,W,1,60,15.0,0,0,0*3E` | `$PQTMCFGSVIN,OK*70` | 文档示例：60 次 / 15.0 m |
| 5 | 开状态输出 | `$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1*58` | `$PQTMCFGMSGRATE,OK*29` | 可选但强烈建议 |
| 6 | 保存 | `$PQTMSAVEPAR*5A` | `$PQTMSAVEPAR,OK*72` | — |
| 7 | 重启 | `$PQTMSRR*4B` | — | 等 `$PQTMVER` |
| 8 | **等收敛** | 连续收 `$PQTMSVINSTATUS` | `...,1,,11,<Obs>,<CFG_CNT>,...` → `...,2,,11,<CFG_CNT>,<CFG_CNT>,...` | `<Valid>` 由 1 变 2 即完成 |
| 9 | **回读校验** | 抓 RTCM3-1005 | `D3 00 13 3E …` | 基站模式默认输出，即收敛后的平均 ECEF |
| 10 | 关状态输出 | `$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,0,1*59` | `$PQTMCFGMSGRATE,OK*29` | 文档：建议收敛后关闭 |
| 11 | 保存 + 重启 | `$PQTMSAVEPAR*5A` → `$PQTMSRR*4B` | — | — |

### 10.3 通用"下发 → 保存 → 重启 → 回读"模板

任何 `W` 类命令都可以套这个模板（`[建议]`，非文档原文，但顺序与文档一致）：

```
1. 下发 W 命令            → 必须收到 <CMD>,OK
2. 下发 SAVEPAR           → 必须收到 $PQTMSAVEPAR,OK
3. 下发 SRR               → 不等 OK，等 $PQTMVER 首帧
4. 下发同命令的 R 查询    → 回读值必须与下发值一致（浮点按原样位数比对）
5. 不一致 → 重下发一次；仍不一致 → 报错并中止，不要"看起来成功"就继续
```

第 4 步的价值：`PQTMCFGSVIN` 的 `-90` vs `-90.0`、`003F` vs `3F` 这类格式差异，只有回读才暴露。

---

## 11. 附录 C：Survey-in 参数档位

### 11.1 三个档位

文档只给了**两组**示例参数（60/15.0 与 3600/1.2）。第三档"高精度"是 `[建议]`，不是官方参数。

| 档位 | `PQTMCFGSVIN` 发送串 | `<CFG_CNT>` | `<3D_AccLimit>` | 预计时长（1 Hz） | 来源 |
| --- | --- | --- | --- | --- | --- |
| 快速 | `$PQTMCFGSVIN,W,1,60,15.0,0,0,0*3E` | 60 次 | 15.0 m | ≈ 60 s | `[原文]` 基站指导 3.2.2 |
| 标准 | `$PQTMCFGSVIN,W,1,3600,1.2,0,0,0*0A` | 3600 次 | 1.2 m | ≈ 60 min | `[原文]` 基站指导 2.2.3 |
| 高精度 | `$PQTMCFGSVIN,W,1,7200,0.5,0,0,0*0C` | 7200 次 | 0.5 m | ≈ 120 min | `[建议]` |
| 带保持距离 | `$PQTMCFGSVIN,W,1,3600,1.2,0,0,0,2.5*0F` | 3600 次 | 1.2 m | ≈ 60 min | `[原文]` 协议规范 2.3.22 |

**时长换算依据**：基站模式定位速率固定 1 Hz 且不可改（§6.3），所以 `<CFG_CNT>` 的次数 ≈ 秒数。
这是**下限**：任一历元的 3D 精度不满足 `<3D_AccLimit>` 时该点不计入，实际会更久。

### 11.2 选档建议 `[建议]`

| 场景 | 档位 | 理由 |
| --- | --- | --- |
| 现场调试、确认链路是否通 | 快速（60 / 15.0） | 1 分钟出结果，精度差但足够验证 1005 有没有输出 |
| 短期临时基站（几小时） | 标准（3600 / 1.2） | 文档默认示例，1 小时收敛，米级→分米级 |
| 长期固定基站 | 高精度（7200 / 0.5） | 2 小时；坐标会写进 NVM 长期使用 |
| 已知精确坐标 | **不用 Survey-in**，走固定模式 | 直接写 ECEF，零等待、零误差来源 |

### 11.3 `<3D_AccLimit>` 的取值陷阱 `[原文]`

- **填 0 = 无精度限制**（协议规范 2.3.22 参数表）。收敛最快，但**任何质量的点都进平均**，
  基站坐标可能偏差数米——对 RTK 是灾难级误差（基站误差直接叠加到移动站）。
- **填得过小**（比如 0.001）= 条件过严，`<Obs>` 可能**永远不增长**（基站指导 FAQ 2 原文）。
- 参考量级：LG290P 默认 0.0（无限制）、LC29H(BS) 默认 15.0。工程上 1.0~2.0 m 是常见选择区间。

### 11.4 收敛完成后改位置怎么办 `[原文]`

> "若基站坐标已收敛但基站位置发生改变……此时需要重新执行 PQTMCFGSVIN……若 Survey-in 模式配置
> 未发生改变，则无需保存配置，重新执行 PQTMCFGSVIN 后，再重启模块即可。"

即：**参数不变**时，重发一次 `PQTMCFGSVIN,W,...` + `SRR` 即可，不必再 `SAVEPAR`。

---

## 12. 附录 D：坑点清单（按踩到概率排序）

| # | 坑 | 后果 | 依据 |
| --- | --- | --- | --- |
| 1 | `W` 之后忘了 `SAVEPAR` + 重启 | 断电/重启后全部回默认，表现为"昨天配好的今天又不对" | 协议规范 2.3 章 |
| 2 | `<3D_AccLimit>` 填 0（默认） | 无精度门限，烂点也进平均，基站坐标偏几米 | 协议规范 2.3.22 |
| 3 | 想在移动站模式下查 `PQTMSVINSTATUS` | 无输出/报错；除 LG291P(00) 外必须先在基站模式 | 协议规范 2.3.23 备注 |
| 4 | 用 `PQTMCFGSVIN,R` 查收敛坐标 | 查到的是**配置值**，Survey-in 下 ECEF 显示 0.0000 | 协议规范 2.3.22 示例 |
| 5 | 给 PQTM 语句配速率时漏了 `<MsgVer>` | 直接 `ERROR,1` 参数无效 | 协议规范 2.3.15 备注 1 |
| 6 | 以为能给 107X 和 112X 分别设不同 `<Rate>` | 做不到，MSM 全星座共用最后一次设置 | 协议规范 2.3.15 备注 3 |
| 7 | 以为基站模式会输出 1033/1006 | 默认只有 MSM4 + 1005，其余要单独开 | 基站指导 表 2 |
| 8 | 在 LG290P 上想设天线高 | `PQTMCFGANTDELTA` 不支持，1006 的 DF028 写不进去 | 协议规范 2.3.47 备注 |
| 9 | 把 `PQTMSVINSTATUS` 透传给移动站 | 移动站回报"不支持该命令"错误码 | 基站指导 备注 2 |
| 10 | 等 `$PQTMSRR,OK` | 永远等不到，该命令文档未给 OK 应答 | 协议规范 2.3.5 |
| 11 | 按 `$PQTMGNSSSTOP,ERROR` 解析 CFGSVIN 失败 | 文档笔误，实际是 `$PQTMCFGSVIN,ERROR` | 协议规范 2.3.22 |
| 12 | 波特率下拉框列了 38400/57600 | LG290P 不支持，设了必失败 | 协议规范 2.3.10 / 硬件手册 |
| 13 | 把 LG290P 脚本原样烧到 LG291P(00) | `PQTMCFGRTK` 报 `ERROR,3`；模式"看似生效"但重启回退 | 协议规范 2.3.25 / 2.3.29 |
| 14 | 以为 LC29H(BS) 也有 `ERROR,3` | 该型号只有 1、2，判错逻辑失效 | LC29HBS 表 3 |
| 15 | 切回移动站模式后期望配置保留 | "恢复默认 NMEA 输出状态"，原速率被覆盖 | 协议规范 2.3.25 |
| 16 | 基站上执行 `PQTMRESTOREPAR` | 清掉全部 PQTM 配置，包括基站身份与坐标 | 协议规范 2.3.8 |
| 17 | 用冷启动"重置一下" | 星历丢失、重新收敛，Survey-in 从零开始 | 协议规范 2.3.2 |
| 18 | 差分龄期 ≥ 120 s 还找固定解 | 进不了固定解（GGA `<Quality>`=5） | RTK 指导 FAQ 3 |
| 19 | 收敛计数器不涨就以为模块坏了 | 多半是 `<3D_AccLimit>` 过严或天空视野差 | 基站指导 FAQ 2 |
| 20 | 字符串参数里带逗号（天线描述/SN） | 参数被劈开，`ERROR,1` | 协议规范 2.3.46 |
| 21 | 重启后立刻发下一条命令 | 模块还在启动，命令被吞 | `[建议]` 等 `$PQTMVER` 首帧 |
| 22 | 在 UART 上不校验校验和 | RTCM 二进制（0xD3…）被当文本解析 | §9.2 |

---

## 13. 附录 E：命令溯源表

| 命令 | 协议规范章节 | 基站指导 | 其它 |
| --- | --- | --- | --- |
| `PQTMVER` | 2.3.1 | — | — |
| `PQTMCOLD` / `PQTMWARM` / `PQTMHOT` | 2.3.2 / 2.3.3 / 2.3.4 | — | — |
| `PQTMSRR` | 2.3.5 | 3.2.1 / 3.2.2（"重启模块"） | — |
| `PQTMUNIQID` | 2.3.6 | — | — |
| `PQTMSAVEPAR` | 2.3.7 | 全文多处 | LC29HBS 2.2.2 |
| `PQTMRESTOREPAR` | 2.3.8 | — | LC29HBS 2.2.3 |
| `PQTMVERNO` | 2.3.9 | — | LC29HBS 2.2.4 |
| `PQTMCFGUART` | 2.3.10 | — | 硬件手册 4.1.1.1~4.1.1.3 |
| `PQTMCFGNMEADP` | 2.3.13 | — | — |
| `PQTMCFGMSGRATE` | 2.3.15 | 2.2.2 / 3.2.1 / 3.2.2 | LC29HBS 2.2.5（格式不同） |
| `PQTMCFGSVIN` | 2.3.22 | 2.2.3 / 3.2.1 / 3.2.2 | LC29HBS 2.2.1（少 `<Distance>`） |
| `PQTMSVINSTATUS` | 2.3.23 | 3.2.1 / 3.2.2 | LC29HBS 2.2.7（字段名不同） |
| `PQTMCFGRCVRMODE` | 2.3.25 | 2.2.1 / 3.2.1 / 3.2.2 | LC29HBS **无此命令** |
| `PQTMCFGFIXRATE` | 2.3.28 | — | — |
| `PQTMCFGRTK` | 2.3.29 | — | RTK 指导 FAQ 3（差分龄期） |
| `PQTMCFGRTCM` | 2.3.39 | 2.2.2 | LC29HBS 对应 `$PAIR432/433` |
| `PQTMCFGSBAS` | 2.3.40 | — | — |
| `PQTMCFGNMEATID` | 2.3.41 | — | — |
| `PQTMCFGBLD` | 2.3.43 | — | 仅 LG580P(03)/LG581P(03) |
| `PQTMCFGRTKSRCTYPE` | 2.3.44 | — | — |
| `PQTMSN` | 2.3.45 | — | — |
| `PQTMCFGANTINF` | 2.3.46 | — | — |
| `PQTMCFGANTDELTA` | 2.3.47 | — | 仅 LG291P(00)（开发中） |
| `$PAIR001` / `432~437` / `864` | — | — | LC29HBS 2.3.1~2.3.8 |
| `$PAIR490~513` | — | — | EASY&EPOC 差异说明 §2 表 3、§3 |

---

## 14. 一句话总结

**LG290P 标定只需 6 条命令**：`PQTMCFGRCVRMODE`（切基站）→ `PQTMCFGSVIN`（Survey-in 或固定坐标）
→ `PQTMCFGMSGRATE` / `PQTMCFGRTCM`（RTCM 输出）→ `PQTMSAVEPAR`（保存）→ `PQTMSRR`（重启），
外加 `PQTMSVINSTATUS` 观察收敛。**其余命令（SN/UNIQID/1033/差分源/星系）都是可选的增强项**。

最容易翻车的不是命令拼错，而是三件事：**忘了 SAVEPAR+重启**、**`<3D_AccLimit>` 默认 0 等于不设限**、
**拿 `PQTMCFGSVIN,R` 当"当前坐标"用**。这三条在 §12 排前三，不是巧合。

---

## 15. 真机实测校准（LG290P03AANR01A03S / 构建 2024-05-22）

本节全部为**真机复现**结果，用于修正前文按文档写下的假设。凡是与前面章节冲突的，
**以本节为准**——文档是"应该怎样"，实测是"这块固件实际怎样"。

| # | 前文说法 | 实测结果 | 影响 |
| --- | --- | --- | --- |
| 1 | §3.2 `PQTMSN` 可用于 LG290P | **`$PQTMSN,ERROR,3*6E`（该型号不支持）** | 只能用 `PQTMUNIQID` 当模块标识，`module_sn` 字段退化为 UNIQID |
| 2 | §6.4 `PQTMCFGANTINF` 可用于 LG290P | **`$PQTMCFGANTINF,ERROR,3*2B`** | 1033 天线信息写不进去，收尾环节必须跳过而不是报错 |
| 3 | §3.3 `<Length>` 未说明单位 | `PQTMUNIQID,OK,8,00006FE9326C3E41` → **Length 是字节数**（8 字节 = 16 个十六进制字符） | 按字符数截断会砍掉一半 ID |
| 4 | §6.2 建议统一写 `-90.0` | `PQTMCFGRTCM,R` 回读为 **`-90`**（无小数位） | "下发值==回读值"不能做字符串比较，必须按数值比 |
| 5 | §6.1 查询任意 `<MsgName>` | `PQTMCFGMSGRATE,R,RTCM3-1033` → **`ERROR,1`**（参数错误） | 未开启的语句查询会报错，不能据此判定"模块坏了" |
| 6 | §4.1 切基站后 NMEA 自动关闭 | 默认 rover 模式实测输出 **GGA/RMC/GLL/VTG**，时间戳间隔 100 ms = **10 Hz**（与基站指导 表 2 一致） | 标定链路可以直接从 GGA 取质量位与卫星数；但切基站后这些语句会消失，要 GGA 必须显式重开 |

**由此得出的工程结论**（已落到 `drivers/quectel_lg290p.py`）：

1. **能力集必须运行时探测，不能只信文档**。`error_code == 3` 就是"本固件没有这条命令"，
   对应的能力位要立刻置 False 并灰掉界面按钮（方案 §4.5）。
2. **模块标识优先用 UNIQID**：它是芯片唯一 ID、不可篡改，比产线 SN 更适合做绑定锚点；
   `PQTMSN` 支持与否反而要看固件。
3. **回读校验一律按数值比**：浮点格式（`-90` / `-90.0`）与补零（`003F` / `3F`）在回读时都可能变，
   字符串比较必然误报"下发失败"。

---

## 16. 官方三份文档复核（2026-11）

复核对象（均为用户提供的官方原件）：

| 文档 | 版本 | 页数 | 用途 |
| --- | --- | --- | --- |
| LG29xP&LG58xP&LG680P 系列 GNSS 协议规范 | V1.2 | 124 | 命令全集、参数表、应答、错误码 |
| LG29xP&LG58xP&LG680P 系列 基站模式应用指导 | V1.1（2025-07-28） | 33 | 基站标定的官方操作序列 |
| LG29xP&LG58xP&LG680P 系列 RTK 应用指导 | V1.1（2025-06-19） | 33 | GGA 质量位语义、1005/1006 定义 |

### 16.1 复核方法（可重复执行）

```python
# 1) 抽取 PDF 全文 → 2) 抓出形如 $PQTM...*HH 的语句 → 3) 逐条重算 XOR
#    协议规范里共 57 个 PQTM 命令名、222 条带校验和的语句
```

**结果**：222 条里 **221 条复算一致**，仅 1 条不符：

| 语句 | 文档写的 | 实际应为 | 判断 |
| --- | --- | --- | --- |
| `$PQTMEOE,1,190423.000,20241224,2346,212681000` | `*45` | `*65` | 厂家笔误，本项目不使用该语句，无需处理 |

这一条恰好说明"校验和必须运行时算"的价值：照抄文档就会发出一条永远得不到应答的命令。

### 16.2 命令表比对结果

| 项 | 数量 | 结论 |
| --- | --- | --- |
| 驱动命令表（`CMD`） | 23 条 | 命令名、参数个数、参数顺序**与协议规范逐字一致** |
| 带校验和常量（`CS_EXPECT`） | 12 条 | 表值 == 实时 XOR == 文档原文，**0 处不符** |
| 参数化模板实例化后比对 | 16 条 | 与文档示例命令串及其校验和**完全一致** |

参数化模板实例化对照（节选，已固化为契约测试 `TestCalibrationCommandFidelity`）：

| 语义键 | 实发命令体 | 实算 | 文档 |
| --- | --- | --- | --- |
| `set_svin(3600, "1.2")` | `PQTMCFGSVIN,W,1,3600,1.2,0,0,0` | `0A` | `0A` |
| `set_svin(60, "15.0")` | `PQTMCFGSVIN,W,1,60,15.0,0,0,0` | `3E` | `3E` |
| `set_svin_hold(3600,"1.2","1.5")` | `PQTMCFGSVIN,W,1,3600,1.2,0,0,0,1.5` | `0C` | `0C` |
| `set_fixed(-2519265.0514,…)` | `PQTMCFGSVIN,W,2,0,0,-2519265.0514,…` | `2A` | `2A` |
| `set_rtcm(4,0,1,0)` | `PQTMCFGRTCM,W,4,0,-90,07,06,1,0` | `25` | `25` |
| `msg_rate_ver(PQTMSVINSTATUS,1,1)` | `PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1` | `58` | `58` |
| `get_msg_rate_ver(PQTMEPE,2)` | `PQTMCFGMSGRATE,R,PQTMEPE,2` | `05` | `05` |
| `set_ant_info("",0,"")` | `PQTMCFGANTINF,W,,0,` | `27` | `27` |

### 16.3 复核查出的 10 处问题（已全部修正）

| # | 位置 | 问题 | 依据 | 修正 |
| --- | --- | --- | --- | --- |
| 1 | `parse_line` | `$PQTMSVINSTATUS` 的 `<Valid>`（0/1/2）解析后被"解析成功=True"**覆盖**，状态值丢失 | §2.3.23 | 状态改存 `state`，`valid` 只作解析成功标志；新增 `done` / `progress` |
| 2 | `get_message_rate` | 查 PQTM 语句只发 `R,<MsgName>`，必然 `ERROR,1` | §2.3.15 注意 1 | 新增 `get_msg_rate_ver`；`msg_ver_for()` 按语句取版本（PQTMEPE=2，其余=1） |
| 3 | `set_message_rate` | `<Rate>` 无上限校验，且 GSA/GSV 配了不生效却静默 | 表 6 + 注意 6 | 按语句类型分档校验（NMEA 0~255 / RTCM3 0~1200 / EPH·RAW 0~1）——下限是 **0**，`<Rate>` 的定义就是"0 或 N，0 = 不输出"，一键还原要靠它把 1005 关回去；GSA·GSV 记警告 |
| 4 | `policy_for` | `PQTMCFGMSGRATE,R,GGA` 末字段是语句名，被归成配置命令 | §6.1 | 改为看第 2 个字段是否为 `R` |
| 5 | `probe_caps` | 探测命令写成字符串常量，绕过 `verify_checksum`，破坏"命令外置" | §4.1 | 改用 `CMD['probe_ant']` + `verify_checksum` |
| 6 | `detect` | 若吃到开机帧，首字段 `1`（MsgVer）会被当固件串 | §2.3.1 | 数字开头的固件串直接判"疑似开机帧" |
| 7 | `config_survey_in` | `f'{acc:.1f}'` 把 0.05 抹成 0.1、1.25 抹成 1.2 | §2.3.22 | `_fmt_dec()` 至少 1 位、最多 4 位，保住用户精度 |
| 8 | `parse_line` | 未建模语句（如 `$PQTMVERNO`）的 `fields` 被丢弃 | — | 兜底分支保留 `fields` |
| 9 | `set_rtcm` | 按官方示例写 `-90.0`，真机回 `ERROR,1`（阶段 3 真机实测） | 基站指导 §2.2.2 + 实测 | 命令表固定写整数 `-90`；契约测试里这一条**有意偏离官方示例**并注明原因 |
| 10 | `set_message_rate`(MSM) | 按星座名 `RTCM3-1074/1084/1094/1124` 写，四条全 `ERROR,1`（阶段 3 真机实测） | §6.1 实测补充 1/2 | 新增 `set_msm_rate()`：统一写 `RTCM3-107X` 且必带 `<Offset>`；备份/还原同样用这一条 |

### 16.4 复核确认无问题的点

- `set_rtcm` 的两个 `<Reserved>` 必须写 `07,06`（协议规范 §2.3.39 参数表默认值），
  示例里从未变过 —— 模板中硬编码正确，且不允许流程层改。
- `PQTMCFGSVIN` 参数顺序 `<Mode>,<CFG_CNT>,<3D_AccLimit>,<ECEF_X>,<ECEF_Y>,<ECEF_Z>[,<Distance>]`
  与模板一致；`<3D_AccLimit>` 默认 0.0 = 无精度限制（不是"最高精度"）。
- `PQTMSN` 的 `<Length>` 是**字符数**，`PQTMUNIQID` 的 `<Length>` 是**字节数** ——
  两者单位不同，驱动分别处理（`read_serial` 校验字符长度，`read_unique_id` 只记字节数）。
- `PQTMCFGANTDELTA` 仅 LG291P(00) 支持（§2.3.47 备注），LG290P 能力位 `ant_delta=False` 正确。
- 基站模式下定位速率固定 1 Hz 且不可改（§2.3.28 备注），所以 `<CFG_CNT>` 的次数在数值上
  等于秒数 —— 但字段语义仍是"次数"，移动站 10 Hz 时就不再等价。

### 16.5 官方标定序列（基站指导 §3.2，驱动按此实现）

```
① $PQTMCFGRCVRMODE,W,2*29      切基站
② $PQTMSAVEPAR*5A              保存（不保存重启就回默认）
③ $PQTMSRR*4B                  重启，等 $PQTMVER 首帧
   ── 以下二选一 ──
   固定坐标：④ $PQTMCFGSVIN,W,2,0,0,<X>,<Y>,<Z>
   Survey-in：④ $PQTMCFGSVIN,W,1,<CFG_CNT>,<3D_AccLimit>,0,0,0
⑤ $PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1*58   打开收敛状态输出（可选）
⑥ $PQTMSAVEPAR*5A  →  ⑦ $PQTMSRR*4B        再次保存+重启
   ── 观察 ──
   $PQTMSVINSTATUS 的 <Valid> 变 2 = 收敛完成；或直接解 RTCM3-1005 的 ECEF
⑧ $PQTMCFGMSGRATE,W,PQTMSVINSTATUS,0,1*59   收敛后关掉状态输出
```

**两个容易踩的细节**：

1. **官方序列是两次"保存+重启"**，不是一次。第一次让模式生效，第二次让坐标配置生效。
   中间那次重启后模块已是基站模式，输出从 NMEA 变成 RTCM（1005 + MSM4），
   此时再想读 GGA 必须 `PQTMCFGMSGRATE,W,GGA,1` 显式打开。
2. **收敛完成后坐标自动存 NVM**（基站指导 §2.2.3 原文："模块将自动保存该平均坐标并输出。
   即使下次重启模块后，仍使用该坐标进行输出"）。所以收敛完成后的那次重启是可选的，
   但为了把 `PQTMSVINSTATUS` 的输出关掉，官方示例仍然重启了一次。
