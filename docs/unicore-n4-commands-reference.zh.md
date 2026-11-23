# Unicore N4 系列接收机指令参考（2RTKserver 对接版）

> 依据：《Unicore Reference Commands Manual For N4 High Precision Products_V2_EN_R1.4》（349 页，2024-07）。
> 项目对应驱动：`rtkserver/calibration/drivers/unicore_n4.py`。
>
> **标注约定**（对齐 `quectel-lg290p-calibration-commands.zh.md` 体例）：
>
> | 标记 | 含义 |
> | --- | --- |
> | `[原文]` | 手册直接给出的命令、参数、数值或示例，可直接使用 |
> | `[驱动]` | `unicore_n4.py` 当前已封装下发/解析的命令 |
> | `[未覆盖]` | 手册有此命令，但驱动当前未封装；是否需要接入按现场需求评估 |
> | `[部分驱动]` | 同一小节里部分命令已封装、部分刻意未做（逐条注明理由） |
> | `[未规定]` | 手册明确没有给出，不臆造 |
>
> **版本与章节号提示**：驱动注释早期基于旧版 `V2_CH_R1.1`，现已按 **R1.4** 校正
>（`DOC_TITLE` 同步改为 `V2_EN_R1.4`）。已修正的漂移：天线高 `CONFIG ANTENNADELTAHEN`
> 是 **§4.25**（旧注释写 §4.22）；R1.4 的 **§4.26 是 `CONFIG AUTHCODE`**，不是天线型号
>（`BASEANTENNAMODEL` 在手册里只出现在 §8 的 CONFIG 汇总表，没有单列小节号）。
> 上机改命令前仍以本文 / R1.4 目录核对。

---

## 1. 范围

### 1.1 适用型号

手册覆盖：**UM960、UM960L、UM980、UM982、UB9A0**。每条命令都带 "Applicable to" 标注，本文在对应条目下标明；驱动按型号拆分类别，命令集不跨型号套用。

> 同门老型号（UB4B0/UM4B0/UM480/UM440/UM482 等）同样响应 `VERSION`，但命令集不同，驱动 `LEGACY_PRODUCTS` 显式排除，避免认错板子。

### 1.2 命令体系（手册 §2）

| 类别 | 命令头 | 作用 |
| --- | --- | --- |
| 工作模式 | `MODE` | 设/查基站、流动站、Heading2 模式 |
| 功能/接口配置 | `CONFIG` | 串口、网络、RTK、RTCM 输出、信号组、天线高等 |
| 卫星屏蔽 | `MASK` / `UNMASK` | 屏蔽/解禁卫星系统、频率、PRN、截止高度角 |
| 辅助定位 | `AGNSS` | 输入辅助位置/时间（UM982/UM980/UB9A0） |
| 数据输出 | 各 LOG 命令 | 位置、定向、观测值、星历、状态等报文 |
| 其他 | `UNLOG` / `FRESET` / `RESET` / `SAVECONFIG` | 停输出、复位、保存 |

---

## 2. 通用规则（先读这一节）

### 2.1 两种命令输入格式 `[原文]`

- **缩写 ASCII（默认）**：无 `$`、无校验和，直接发命令头 + 参数，参数用**空格**分隔。例：`RESET`、`mode base time 60`、`rtcm1074 com2 1`。
- **ASCII 带校验**：`$<CMD>[,<参数>...]*<XOR><CR><LF>`。需先 `CONFIG CMDFORMAT 1` 开启校验模式，模块才认带校验命令；默认只认无校验命令。例：`$RESET*55`。
- 校验和：`$` 与 `*` 之间全部字符（含 `,`）逐字节 XOR，两位十六进制。
- `CMDFORMAT` 仅 UM982（§4.41）。

> ⚠️ 与 Quectel `$PQTM...*XX` 体系不能混用：N4 命令是裸 ASCII、空格分隔；按 `$...*XX` 发出，模块不认也不回错，现场表现就是"发命令没反应"。

### 2.2 应答与输出日志的校验 `[原文]`

- 输入命令走 XOR 校验；**输出日志**（ASCII/二进制 LOG）末尾带的是 **32-bit CRC**（附录 1），两者不是一回事。
- 应答头 `#` 与 `$` 混排：`#VERSIONA,...`、`#MODE,...`、`$CONFIG,...*65` 都可能出现在同一串口流里。

### 2.3 配置生效模型 `[原文]`

- 命令只改运行态；`SAVECONFIG` 才把当前配置（LOG 报文、端口配置等）写入 NVM 持久化。
- `FRESET` 清空 NVM 配置、星历、位置，并把波特率复位到 **115200**，强制重启。
- 推荐流程：下发配置 → 核对回读 → `SAVECONFIG` 保存。

### 2.4 串口 `[原文]`（§4.2）

- 物理口：`COM1`、`COM2`、`COM3`，功能相同、各自独立，可互相配置；**建议 COM1 留作升级口**。
- 波特率（三档口完全一致）：9600 / 19200 / 38400 / 57600 / 115200 / 230400 / 460800 / 921600。
- 帧格式：数据位仅 8、校验仅 N、停止位仅 1（当前产品不支持 7 数据位 / E·O 校验 / 2 停止位）。
- 语法：`CONFIG COM1 115200` 或 `CONFIG COM1 115200 8 n 1`。
- `[驱动]` 波特率与帧格式已封装（`set_baud` / `set_baud_frame`），`BAUDS`、`FRAME_FIXED` 与手册一致。

---

## 3. 工作模式 `MODE`（§3）

### 3.1 查询当前模式 `[原文][驱动]`

- 命令：`MODE`
- 应答示例：`#MODE,81,GPS,FINE,2230,547967000,0,0,18,518;MODE ROVER SURVEY,*1B`
- 模式取值：`MODE ROVER`（含 UAV / AUTOMOTIVE / SURVEY 细分）、`MODE BASE`、`MODE BASE TIME`、`MODE HEADING2`。

### 3.2 固定坐标基站 `[原文][驱动]`

语法：`MODE BASE [ID] <param1> <param2> <param3>`

| 坐标系 | param1 | param2 | param3 |
| --- | --- | --- | --- |
| 大地坐标（param1 在 ±90 内） | 纬度 lat（度，11 位有效位） | 经度 lon（度，±180，11 位有效位） | 高程 h（米，±30000，6 位有效位） |
| ECEF（param1 超出 ±90） | X（米，4 位有效位） | Y（米） | Z（米） |

示例：
```
MODE BASE 40.45628476579 116.2859754968 58.0984
MODE BASE -2160489.0276 4383620.1006 4084738.1110
MODE BASE 1 40.07898324818 116.23660197714 60.4265
```
- 基站 ID：0–4095 整数，可省略（ID 限定 RTCM3.2）。
- 切固定坐标后，GPGGA 始终显示该坐标。
- 适用：UM960 / UM960L / UM980 / UB9A0 / UM982。

### 3.3 自收敛基站 `[原文][驱动]`

语法：`MODE BASE [ID] TIME <T> [Distance]`

```
MODE BASE TIME 60
MODE BASE TIME 60 5
MODE BASE 1 TIME 60
```

- `T`：计算平均位置的最长时长（秒，非负，≤3600）；收敛计时从**定位质量良好**那一刻起算，不是从首帧定位起算。
- `Distance`：0–10 米。重启后若新收敛坐标与 Flash 保存坐标偏差小于该值，则沿用 Flash 坐标；=0 表示每次都用新收敛结果。
- 适用：UM960 / UM960L / UM980 / UB9A0 / UM982。
- **注意**：N4 没有独立的 Survey-in 进度语句，也没有"已收敛"标志；收敛只能靠 `MODE` 查询 + 坐标稳定性间接判断（驱动 `svin_has_status=False`）。

### 3.4 缺省参数基站 `[原文][驱动]`

- 命令：`MODE BASE`（不带参数）
- 行为：默认 60 秒定位平均；收敛门限水平误差 2.5 m、垂直 3.5 m。

### 3.5 仅设置基站 ID `[原文][驱动]`

- 命令：`MODE BASE <ID>`，ID 0–4095。

### 3.6 流动站 `[原文][驱动]`

语法：`MODE ROVER [profile] [variant]`；`[variant]` 为空取该大类默认。

| profile | variant | 说明 |
| --- | --- | --- |
| UAV | DEFAULT / HIGHDYN | 无人机动态；DEFAULT 普通 UAV，HIGHDYN 高动态（编队灯光秀/FPV） |
| SURVEY | DEFAULT / MOW | 测量测绘；DEFAULT 精密测量，MOW 割草模式 |
| AUTOMOTIVE | DEFAULT | 车载动态 |

示例：`MODE ROVER UAV`、`MODE ROVER SURVEY`、`MODE ROVER SURVEY MOW`。
- 适用：UM960 / UM980 / UB9A0 / UM982（**UM960L 不支持**）。
- 出厂默认：UM980=SURVEY DEFAULT；UM982=UAV DEFAULT；UM960=SURVEY MOW。

### 3.7 Heading2 定向 `[原文][驱动]`

- 命令：`MODE HEADING2 [FIXLENGTH|VARIABLELENGTH|STATIC|LOWDYNAMIC]`
- 示例：`MODE HEADING2`、`MODE HEADING2 FIXLENGTH`
- 适用：UM960 / UM980 / UB9A0 / UM982（UM960L 不支持）。
- 双天线 Heading 引擎配置 `CONFIG HEADING ...` **仅 UM982**（§4.8），驱动已单独封装在 `HEADING_CMD`。

---

## 4. 网络配置

### 4.1 以太网口 ETH1 `[原文][未覆盖]`（§4.32，适用 UB9A0）

```
CONFIG ETH1 DHCP
CONFIG ETH1 192.168.0.100 192.168.0.1 255.255.255.0 192.168.0.1
```
- 静态：本地 IP、网关、掩码、DNS，空格分隔。
- 接收机仅一个网口 ETH1。
- `[未覆盖]` **不做**：手册把 ETH1 标为适用 UB9A0，而本驱动负责的四个型号
  （UM960/960L/980/982）没有这个网口。给不支持的型号封装网络配置只会制造误配。

### 4.2 网络串口 ICOM `[原文][未覆盖]`（§4.33，适用 UB9A0）

- 三个网络串口：`icom1` / `icom2` / `icom3`，功能相同、各自独立。
- 语法：`CONFIG [port] [protocol] [IP] <port>`
```
CONFIG ICOM1 TCP 30001
CONFIG ICOM1 TCP 192.168.0.2 30001
```
- 协议 TCP/UDP（默认 TCP）；默认 SERVER 模式，本地端口用于 SERVER，否则填对端服务端口。
- 端口范围 1–65534；**40000 保留给内部系统编程**，建议用 30001/30002/30003，避免 32768 以上。
- `[未覆盖]` **不做**：同 §4.1，ICOM 也标为适用 UB9A0，本项目四型号无此能力。
  流动站要联网取差分请用 §5.2 的 `NCOM20`（内置 NTRIP Client，四型号命令体已在驱动里）。

---

## 5. 内置 NTRIP（§4.35 / §4.36）

> ⚠️ **仅支持 NTRIP 协议 V1**。本节命令**已封装**（`set_ntrip_server` / `set_ntrip_client` /
> `disable_ntrip`），但 **`CAPS['ntrip']` 保持 `False`**：手册把 NCOM 小节标为适用 UB9A0，
> N4 四个型号厂商没有逐一承诺（见 §12），所以"命令能发"不等于"这块板子一定有"。
> 现场实测通过后再把能力位打开；不改就代表厂商没承诺过，上层不会误以为板子自带 NTRIP
> 而去掉外部转发链路。
> 2RTKserver 侧若要 NTRIP2（账号 + 密码）或需要**上传用户名**，仍须走串口把 RTCM 流
> 交给外部程序转发 —— 内置上传端只有密码，没有用户名。

### 5.1 Ntrip Server（上传到 Caster）`[原文][驱动]`

- 设备：`NCOM1` / `NCOM2` / `NCOM3`，三路独立。
- 语法：`CONFIG NCOM<1|2|3> <Caster IP> <port> <mountpoint> <upload password>`
- 示例：`CONFIG NCOM1 10.0.100.2 9000 UB9A0_RTCM32 SERV_PASSWORD`
- 凭据模型：**只有上传密码，无用户名**（NTRIP1 风格）。
- `Disable` 关闭该路 Ntrip server。
- `[驱动]` `set_ntrip_server(host, port, mount, password, slot=1|2|3)`；`disable_ntrip(slot)` 关单路，
  不传 `slot` 则三路上传 + 一路下载一起关。端口 40000 会被驱动拒绝（§4.2 保留号）。

### 5.2 Ntrip Client（从 Caster 下载）`[原文][驱动]`

- 设备：仅 `NCOM20` 一路。
- 语法：`CONFIG NCOM20 <Caster IP 或域名> <port> <mountpoint> <username> <password>`
- 示例：`CONFIG NCOM20 10.0.100.2 9000 UB9A0_RTCM32 UNAME CLI_PASSWORD`
- 凭据：用户名 + 密码。
- `[驱动]` `set_ntrip_client(host, port, mount, user, password)`。返回值里带
  `protocol='NTRIP1'` 与 `verified_on_this_model`（后者取 `CAPS['ntrip']`，默认 `False`）。
- ⚠️ 这是**流动站最省事的一条路**（模块直连 caster，不必外挂转发程序），
  但也意味着没有 GGA 交互 —— 需要 VRS（发 GGA 换差分）的挂载点用不了它。

---

## 6. RTCM 差分输出（§1.1 + 附录 2）

### 6.1 命令语法 `[原文][驱动]`

```
RTCM<消息号> <口> <间隔>
```
- 口：`COM1` / `COM2` / `COM3`（命令里小写 `com2`）。
- 间隔语义是**秒**：1006/1033 写 10 = 每 10 秒一次；MSM 写 1 = 1 Hz。
- 间隔档位（手册 NMEA 侧口径）：1 / 0.5 / 0.2 / 0.1 / 0.05 / 0.02，对应 1 / 2 / 5 / 10 / 20 / 50 Hz（50 Hz 仅限特定产品与固件）。
- 驱动：`rtcm_msg = 'RTCM{msg} {port} {interval}'`。

### 6.2 手册支持的 RTCM V3 电文清单 `[原文]`

**观测值**
- GPS：1001、1002、1003、1004；MSM4 1074、MSM5 1075
- GLONASS：1009、1010、1011、1012；MSM4 1084、MSM5 1085
- BDS：1104（行业自定义 RTK 观测值，勿与 SBAS 等定义混淆）；MSM3 1123、MSM4 1124、MSM5 1125、MSM6 1126、MSM7 1127
- （Galileo 观测值 MSM 见手册；常用示例使用 1094）

**基站坐标**：1005（ARP 坐标）、1006（ARP 坐标 + 天线高）

**天线描述**：1007（当前仅编码支持）、1033（接收机与天线描述）

**辅助/星历**：1019 GPS 星历、1020 GLO 星历、1042 BDS 星历、1045 GAL F/NAV、1046 GAL I/NAV；63 BDS 星历（测试电文）；1105 定向信息（Unicore 自定义，定向接收机发流动站）。

### 6.3 基准站常用组合（手册 §1.1 示例）`[原文]`

```
rtcm1006 com2 10      // 基站 ARP 坐标（含天线高），10s
rtcm1033 com2 10      // 接收机/天线描述，10s
rtcm1074 com2 1       // GPS MSM4，1Hz
rtcm1124 com2 1       // BDS MSM4，1Hz
rtcm1084 com2 1       // GLONASS MSM4，1Hz
rtcm1094 com2 1       // Galileo MSM4，1Hz
saveconfig
```

### 6.4 其他 RTCM 相关开关 `[原文][部分驱动]`

- `CONFIG RTCMB1CB2a ENABLE|DISABLE`：RTCM 是否编入 BDS B1C/B2a 信号，默认 ENABLE（UM980/UB9A0/UM982，§4.16）。
  `[驱动]` `set_rtcm_b1cb2a(enabled)`，仅 UM980 / UM982 有该命令键（UM960 不在适用表）。
- `CONFIG RTCMDECAUTO ENABLE`：基站同时发 RTCM3.0 与 3.2 时，解码 3.2 并滤掉 3.0（UM980/UM982，§4.37）。
  `[驱动]` `set_rtcm_decauto(enabled)`，同样仅 UM980 / UM982。
- `CONFIG RTCMPHASERATE`、`CONFIG RTCMCLOCKOFFSET`、`CONFIG ALLEPHRTCM`：相位距率/钟差补偿/星历输出（§4.18/4.19/4.38）。
  `[未覆盖]` **刻意不做**：手册只写了作用，没给参数取值与格式；简化 ASCII 下发不认识的
  参数既不报错也不生效，盲写等于制造"看起来配了"的假象。需要时先按手册拿到确切语法再补。

---

## 7. NMEA 输出（§7.1）

### 7.1 常用语句与速率 `[原文][驱动]`

语法：`<语句名> <口> <间隔>`；N4 只认小写无前缀形式。

```
GPGGA COM2 1      // 1Hz GGA
GPRMC COM2 1
GPZDA COM2 1
GPVTG COM2 1
GPTHS COM2 1      // 当前时刻真航向
```
驱动别名表：GGA/RMC/ZDA/VTG/THS/GLL/GSA/GSV/GNS 等（`NMEA_ALIASES`）。

### 7.2 GGA 定位质量位（qual）`[原文]`

| qual | 含义 |
| --- | --- |
| 0 | 无定位/无效 |
| 1 | 单点定位（SPP） |
| 2 | 差分定位 |
| 3 | GPS PPS 模式 |
| 4 | RTK 固定解（RTK Int） |
| 5 | RTK 浮点解（Float） |
| 6 | 航位推算 |
| 7 | 人工输入模式 |
| 8 | 模拟器模式 |

GGA 示例：`$GNGGA,023634.00,4004.73871635,N,11614.19729418,E,1,28,0.7,61.0988,M,-8.4923,M,,*58`

### 7.3 NMEA 版本 `[原文][驱动]`（§4.15）

- `CONFIG NMEA0183 V410`（默认，扩展支持 BDS）/ `CONFIG NMEA0183 V411`。
- `[驱动]` `set_nmea_version('V410'|'V411')`，只认这两个枚举值（写 `410` 或 `V4.10` 模块静默不认）。

---

## 8. 其他常用 CONFIG

| 命令 | 作用 | 适用型号 | 驱动状态 |
| --- | --- | --- | --- |
| `CONFIG RTK TIMEOUT <秒>` | RTK 数据最大龄期（0 关闭 RTK；1–1800）。UM982 默认 600s，UM980 默认 120s | 全系列 | `[驱动]` `set_rtk_timeout` §4.6 |
| `CONFIG RTK RELIABILITY <引擎> <ADR>` | RTK/ADR 可靠性阈值 1–4 | 全系列 | `[驱动]` `set_rtk_reliability` |
| `CONFIG RTK RESET / DISABLE` | 复位 RTK 解 / 停止 RTK（含浮点、固定） | 全系列 | `[驱动]` `reset_rtk` / `disable_rtk` |
| `CONFIG DGPS TIMEOUT <秒>` | 差分数据最大龄期（0 关闭 DGPS；1–1800，默认 300） | 全系列 | `[驱动]` `set_dgps_timeout` §4.5 |
| `CONFIG STANDALONE ENABLE <lat> <lon> <h>` / `DISABLE` | 无差分时段保持一段时间厘米级定位 | UM960/UM980/UB9A0/UM982 | `[驱动]` `set_standalone` / `unset_standalone` §4.7（**UM960L 无此键**） |
| `CONFIG ANTENNADELTAHEN <Height> <East> <North>` | 天线高与平面偏移，影响 1006 描述；高 0–6.5535 m，东/北偏移 0–100 m，默认 0 | 全系列 | `[驱动]` `set_ant_delta`（默认不下发） |
| `CONFIG BASEANTENNAMODEL "<名>" <SN> <setup_id> <type>` | 基站天线型号，影响 1005/1006/1007/1033 | — | `[驱动]` `set_ant_model` |
| `CONFIG MMP <mode>` | 多路径抑制 | 仅 UM980 | `[驱动]` `set_mmp` |
| `CONFIG PSRVELDRPOS <mode>` | Doppler 位置预测 | 仅 UM980 | `[驱动]` `set_psrveldrpos` |
| `CONFIG SIGNALGROUP <type>` | 信号组（主/从天线 TypeNum，决定可用信号与更新率） | 见手册表 4-32 | `[未覆盖]` §4.24 —— 需要表 4-32 的完整 TypeNum 清单才能校验，手册未给全表 |
| `CONFIG AUTHCODE <string>` | 授权码；正确后自动保存并重启，**固件升级/FRESET 都无法清除**；错误码会导致工作异常 | UM980/UB9A0/UM982 | `[未覆盖]` §4.26 —— 写错会永久锁死功能，且无撤销路径，不适合放进通用配置流程 |
| `CONFIG ALGRESET <RTK1|RTK2|HEADING>` | 复位主/从天线 RTK 或航向算法 | UM980/UB9A0/UM982/UM960 | `[未覆盖]` §4.27 |
| `CONFIG UNDULATION AUTO|<分离值>` | 大地水准面模型；默认内置网格，分离值 -1000～+1000 m | — | `[未覆盖]` §4.4 |
| `CONFIG PPS ...` | PPS 周期/脉宽/延迟补偿 | — | `[未覆盖]` §4.3 |

### 8.1 卫星系统/频率屏蔽 `[原文][驱动]`（§5）

- 查询：`MASK`
- 屏蔽：`MASK GPS|BDS|GLO|GAL|QZSS|IRNSS`；频率如 `MASK B1`、`MASK E5a`；单星 `MASK GPS PRN 10`；截止高度角 `MASK 10`（-90°～90°，默认 5°）。
- 解禁：`UNMASK <系统|频率|PRN>`。
- 注意：按系统屏蔽与按 PRN 屏蔽**不能混用**；屏蔽系统后再解禁单颗 PRN 不生效。
- 驱动：`get_mask` / `set_elevation_mask`（高度角，**合法区间 -90~90**，默认 5）；
  按系统/频率屏蔽用 `mask_target` / `unmask`，单星用 `mask_prn` / `unmask_prn`。
- ⚠️ `mask_target` 会**拒绝纯数字**：`MASK` 后面跟数字是高度角、跟名字才是屏蔽对象，
  把 `MASK 10` 当"屏蔽 10 号星"发下去会静默改掉高度角设置。

---

## 9. 状态与信息日志（§7.3）

| 日志 | 内容 | 消息 ID | 驱动状态 |
| --- | --- | --- | --- |
| `VERSIONA` / `VERSIONB` | 产品名、授权、PN/SN、硬/固件版本、授权日期 | 37 | `[驱动]` 已解析产品名/版本/SN 做型号识别 |
| `BASEINFO` | 基站位置、ID、健康状态，支持 ONCHANGED | 176 | `[未覆盖]` §7.3.7 |
| `BASEPOS` | 固定基站模式下实时基站位置，用于监测 | — | `[未覆盖]` §7.3.44 |
| `RTKSTATUSA/B` | 解状态、差分数据状态等 | 509 | `[未覆盖]` §7.3.51 |
| `RTCMSTATUSA/B` | RTCM 数据状态，仅 ONCHANGED 触发 | 2125 | `[未覆盖]` §7.3.56 |
| `OBSVM` / `OBSVH` / `OBSVMCMP` / `OBSVHCMP` | 主/从天线观测值（含压缩版） | — | `[未覆盖]` |
| `BESTNAV` / `ADRNAV` / `PPPNAV` / `SPPNAV` 及各 DOP | 位置速度与精度 | — | `[未覆盖]` |
| `SATSINFO` / `BESTSAT` / `SATELLITE` | 卫星使用/可见信息 | — | `[未覆盖]` |

> 完整字段结构（头、偏移、字节、32-bit CRC）见手册 §7.3 各小节；本文不逐字段抄录。

---

## 10. 维护命令（§8）

| 命令 | 作用 | 驱动状态 |
| --- | --- | --- |
| `UNLOG [port] [message]` | 停输出；省略口=当前口，省略报文=停当前口全部输出。例：`UNLOG GPGGA`、`UNLOG COM2 GPGGA` | `[驱动]` |
| `FRESET` | 清 NVM 配置/星历/位置，波特率回 115200，强制重启 | `[驱动]` `factory_reset` |
| `RESET [EPHEM|ALMANAC|IONUTC|POSITION|XOPARAM|ALL]` | 重启并按需清星历/历书/电离层 UTC/位置/晶振参数；`RESET ALL` 清上述除 XOPARAM 外全部 | `[驱动]` `reset()`（裸 RESET）/ `reset_data(kind)` |
| `SAVECONFIG` | 当前配置写入 NVM（ONCE 触发的报文除外） | `[驱动]` `save` |

> 信号从模拟器切回真实场景时，建议清星历、历书、电离层/UTC、位置，否则可能不定位或定位异常。

---

## 11. 与 2RTKserver 对接要点

1. **接入路径**：本项目基准站接入走串口（`serial_source`）读 RTCM 流，或由上游 caster 中继。N4 内置 NCOM NTRIP 仅 V1、上传无用户名，与 NTRIP2 模型不匹配；需要 NTRIP2 时以串口/ICOM 转发为准。
2. **命令裸发**：下发用缩写 ASCII、空格分隔，不带 `$`/校验；应答同时有 `$` 与 `#` 头，解析需两种头兼容。
3. **无数字错误码**：简化 ASCII 不回错误码，命令不被认时现场表现为"无响应"；排查优先核对型号适用性、端口名、参数顺序与波特率。
4. **坐标顺序**：`MODE BASE <lat> <lon> <h>`；流程层若持 ECEF，需先转经纬高再下发，避免把 ECEF 分量塞进纬度位置。
5. **型号裁剪**：流动站/Heading2 不含 UM960L；双天线 Heading 引擎仅 UM982；MMP/PSRVELDRPOS 仅 UM980。下发前按 `MODEL_KEY` 裁剪命令集。
6. **持久化**：流程改完运行态后需 `SAVECONFIG` 落 NVM；`FRESET` 后串口回到 115200，需重新枚举波特率再识别。
7. **驱动当前未封装但手册可用**：ETH1/ICOM 网络（仅 UB9A0，本项目不支持该型号）、
   SIGNALGROUP、AUTHCODE、ALGRESET、UNDULATION、PPS，以及 BASEINFO/BASEPOS/RTKSTATUS/
   RTCMSTATUS 等状态日志。前几项在 §8 表里逐条注明了不做的理由；状态日志不做是因为
   手册正文只给字段清单、**没给 ASCII 版的逐字段偏移与 32-bit CRC 算法**（§7.3 只说
   "见各小节"），盲写解析必然错位。是否接入按现场需求评估。

## 12. 未规定 / 待核对

- 手册未给出各网络命令在 UM960/960L/980/982 上的逐一适用性（ETH1/ICOM/NCOM 小节标注适用 UB9A0，其余型号上机前须实测）。
- 50 Hz 输出率、部分高动态模式受具体产品与固件 Build 限制，手册已注明，不在此泛化承诺。
- 本文未覆盖二进制 LOG 的逐字节偏移与 32-bit CRC 计算；需要解析二进制日志时再回手册 §7.3 与附录 1。
