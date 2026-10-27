# 2RTK NTRIP Server

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Armbian-orange.svg)](#)

> 一个 RTK 基准站模块 + 一台小主机 + 家里的宽带 = 你自己的 GNSS 基准站。
> One RTK base module + one small host + your home broadband = your own GNSS base station.

2RTK NTRIP Server 是一个用于 RTK（实时动态差分）定位的 NTRIP 服务器。它可以从 USB 串口上的 RTK 模块 / GNSS 接收机读取 RTCM 数据并上传到 NTRIP Caster，也可以从其他 Caster 拉取数据再转发，实现**单基准站自建 CORS**。

2RTK NTRIP Server is an NTRIP server for RTK positioning. It reads RTCM data from an RTK module / GNSS receiver on a serial port and uploads it to an NTRIP Caster, or pulls from another caster and re-forwards — enabling a **self-built CORS with a single base station**.

适用于 Linux / Armbian，已在玩客云等 armbian 设备上测试通过，也可用于树莓派等其他 Linux 小主机。

Works on Linux / Armbian; tested on OneCloud and compatible with Raspberry Pi and other Linux boards.

---

## 功能特点 · Features

- 支持 NTRIP 协议，兼容大多数 RTK 模块与串口 RTK 接收机。
  NTRIP protocol support, compatible with most RTK modules and serial receivers.
- 数据源：串口、NTRIP 客户端。
  Data sources: serial port, NTRIP client.
- 内置 Web 管理界面，实时修改运行模式与配置、监控状态。
  Built-in web UI for live configuration and monitoring.
- **内置本地 NTRIP Caster**：把正在转发的同一路 RTCM 数据额外向内网发布，移动站 / 手簿直连本机取差分。默认监听 `0.0.0.0:2101`，挂载点 `2RTK`，账号密码均为 `2RTK`。
  **Built-in local NTRIP Caster**: publishes the same stream on the LAN so rovers connect directly. Defaults to `0.0.0.0:2101`, mount point `2RTK`, user/password `2RTK`.
- Web 端实时监控收星数量、信号质量，以图表展示。
  Live satellite count & signal quality charts.
- 自动获取基站经纬度并标记在地图上（国内高德、其他地区 OpenStreetMap），并显示 20KM / 50KM 覆盖圈。
  Auto-plots the base location on a map (AMap in China, OSM elsewhere) with 20 km / 50 km coverage circles.
- 自动重连：网络波动或连接失败时自动重连并认证。
  Auto-reconnect & re-auth on network drops.
- 前端支持中文 / 英文切换。
  Frontend supports Chinese / English.

> 单基准站 RTK 受电离层 / 对流层 / 卫星轨道 / 多路径误差影响，有效覆盖通常约 20KM；双频 / 多频接收机有时可达 50KM，但一般不建议超过 50KM。
> A single-base RTK is affected by ionospheric / tropospheric / orbit / multipath errors; effective range is typically ~20 km, up to 50 km with dual/multi-frequency receivers.

---

## ⚠️ 使用前必读：先定位天线相位中心点 · Locate the Antenna Phase Center First

基准站差分改正数是**相对天线相位中心（APC）**计算的。如果这个坐标没配准，所有移动站的定位都会整体偏移，精度无从谈起。

RTK corrections are computed relative to the **Antenna Phase Center (APC)**. If this coordinate is wrong, every rover position is shifted by the same error.

**使用前请先通过静态测量 / 已知控制点等方式，把天线相位中心的精确坐标写入模块**（详见 [INSTALL.md](INSTALL.md) 或 [INSTALL.en.md](INSTALL.en.md) 的「定位天线相位中心点」章节）。

**Before use, write the accurate phase-center coordinate into the module** (see the "Locate the Antenna Phase Center First" section in [INSTALL.md](INSTALL.md) / [INSTALL.en.md](INSTALL.en.md)).

---

## 快速开始 · Quick Start

```bash
git clone https://github.com/Rampump/2RTKserver.git
cd 2RTKserver
chmod +x install.sh
sudo ./install.sh
```

访问 Web 界面 · Open the web UI: `http://[设备IP]:5757`

默认登录 · Default login: `admin` / `admin`（登录后请立即改密码 · change it immediately）。

详细步骤见 [INSTALL.md](INSTALL.md)（中文）/ [INSTALL.en.md](INSTALL.en.md)（English）。

---

## 工作模式 · Modes

- **串口模式 serial**：从 USB 串口读取 RTK 模块的 RTCM 数据，上传到上游 Caster。串口名留空自动扫描。
  Read RTCM from the module's serial port and upload to an upstream caster.
- **中继模式 relay**：连接其他 NTRIP 服务器拉取数据再转发（自建 CORS）。可点「获取挂载点」读取对端源列表。
  Pull from another NTRIP caster and re-forward. "Fetch Mount Points" reads the peer's source table.
- **本地 Caster（可选叠加）**：把同一路数据额外向内网发布，移动站直连本机。
  Local caster (optional overlay): publish the same stream on the LAN.

---

## 管理命令 · Management

```bash
2rtkserver start | stop | restart | status
```

---

## 技术特点 · Technical

基于单基站 RTK：基站观测卫星信号计算改正数发送给移动站。结构简单、成本低，但有效范围有限（一般 20~50KM）。与千寻 CORS、移动 CORS 等多基站 RTK 相比覆盖较小，但适合预算有限的用户，或用无人机等设备自建 CORS。

Single-base RTK: the base observes satellites and computes corrections for rovers. Simple and low-cost, but limited range (typically 20–50 km).

---

## 文档 · Docs

- [INSTALL.md](INSTALL.md) — 中文安装指南（含天线相位中心定位说明）
- [INSTALL.en.md](INSTALL.en.md) — English installation guide (incl. antenna phase center)

---

## 许可证 · License

本项目采用 [GNU GPLv3](LICENSE)。GPL 要求任何分发本软件的修改版本也必须以相同许可证开源。

Licensed under [GNU GPLv3](LICENSE).

---

## 联系方式 · Contact

作者 · Author：文七 (Wen Qi)  
邮箱 · Email：i@jia.by

欢迎提交 issue、功能请求和 PR。Welcome issues, feature requests and pull requests.
