# 2RTK NTRIP Server

`GPLv3` · `Linux / Armbian` · `Python 3.8+`

[English](README.md) · **中文**

> 一个 RTK 基准站模块 + 一个机顶盒（Armbian）+ 宽带 WiFi = 你自己的 RTK 基准站。

一个支持 RTK 基准站的模块，通过 USB 或者 UART 接入一个可以刷 Armbian 的机顶盒、树莓派、玩客云、Armbian 开发板等。
程序会通过读取串口的 RTK 模块数据自建 RTKBASE（或者 relay 别的 NTRIP Caster 上挂载点的数据）：

1. 可以转发到你自建的 NTRIP Caster 或者 RTK2GO 等。
   自建 NTRIP Caster 可以用 <https://github.com/Rampump/NTRIPcaster>；
   不想自建可以用 NTRIP Caster 测试服务器 <https://2rtk.com/test-caster/>。
2. 也可同时开启本地内网 NTRIP Caster。
3. 还可以同时通过另外串口上的无线数传模块播发出去。

![实物参考](docs/images/RTKBASE.webp)

如上图接好硬件，室外固定好 RTK 天线。

## 一键安装

SSH 或直接登录 Armbian 设备，然后复制下面**这一整行**到设备终端里回车：

```bash
sudo apt-get install -y git && sudo rm -rf /tmp/2rtk-install && (sudo git clone --depth 1 https://gitcode.com/rampump/NTRIPserver.git /tmp/2rtk-install || (sudo rm -rf /tmp/2rtk-install && sudo git clone --depth 1 https://github.com/Rampump/2RTKserver.git /tmp/2rtk-install)) && sudo bash /tmp/2rtk-install/install.sh
```

缺 `git` 会先装 `git`；先试国内 GitCode 镜像，连不上再退到 GitHub。
装完浏览器打开 `http://设备IP:5757` 或 `http://2rtk.local:5757`（Windows / macOS / iOS / Linux）。
默认登录账号密码都是 **`admin`**。

完整说明见 [INSTALL.md](INSTALL.md) · [English](INSTALL.en.md)。

**完整使用教程（安装 → 标定 → 发布）：** [中文](docs/tutorial.zh.md) · [English](docs/tutorial.en.md)

## 功能

- **三个独立出口** —— 上传上游 Caster、局域网内置 Caster、USB 数传播发。
  任意勾选组合；某个出口挂了不会拖住另外两个。
- **两种数据源** —— 从模块经 USB 串口读 RTCM3，或 relay 别的 Caster
  （适合把上游流在本地再播一遍，或做备用基准站）。
- **网页端标定**（`/calibrate`）—— 测量天线位置并写进模块，不用厂家上位机。
  支持 CORS/VRS、Survey-in、手动输入；内置 Quectel LG290P、Quectel LC29H(BS)、
  Unicore N4 的型号档案。每条结果都标注 `fixed` / `float` / `sub_meter` / `decimeter`。
- **网页升级** —— 检查更新、一键升级、失败自动回滚，不用 SSH。
  卸载刻意只留 SSH，不给浏览器入口。

## 截图

**状态页** —— 数据源、三个出口、实时卫星与差分状态一目了然。

![状态页](docs/images/screenshot-status.png)

**配置页** —— 出口、数据源、各出口实时状态。

![配置页](docs/images/screenshot-settings.png)

**标定页**（`/calibrate`）—— 四条互斥的路径，选哪条，页面就只显示那条真正会用到的设置：

1. **CORS 差分标定** —— 模块临时切成流动站，连 CORS 取差分，稳定固定解后采集过滤，
   再写回基准站坐标。精度最高（厘米级），需要 CORS 账号，且模块要支持基准站/流动站双模切换。
2. **Survey-in 自收敛** —— 由模块固件自己长时间平均出坐标，不连 CORS、不占账号。
   耗时十几分钟到一小时，全程不能动天线，精度一般为亚米级，达不到厘米级。
3. **写入已知坐标** —— 不测量，把手上已有的坐标直接写进模块并设为基准站。
   可以是手动输入、复用本机历史记录，或先用「只测坐标」测出来再回来写。
4. **只测坐标（不改模块）** —— 连 CORS 测出天线坐标就结束，不写坐标、不保存 NVM。
   用于外接流动站，或没有命令集的未知模块：测完给你一组坐标，由你手动配进基准站。

![标定页](docs/images/screenshot-calibrate.png)

## 系统构成

```
   [ 楼顶大地型天线，稳固安装 ]
                 │  馈线
                 ▼
   [ RTK 模块：RTCM3 over USB 串口 ]
                 │  USB
                 ▼
   [ Armbian 主机：机顶盒 / 玩客云 / 树莓派 ]──▶ 局域网 ──▶ 流动站、手簿、无人机地面链路
                 │  USB（可选）
                 ▼
   [ USB 数传模块 ] ~~~ 空中 ~~~▶ [ 流动站数传 ]  （不需要任何网络）
```

**数据源**（二选一）：

- `serial` —— 从模块读。串口留空则自动扫描。
- `relay` —— 从别的 Caster 拉流（`host:port/mountpoint`，可选用户名密码）。

**出口**（可多选，完全独立）：

| 出口 | 作用 | 默认值 |
| --- | --- | --- |
| ① 上传上游 Caster | NTRIP `SOURCE` 推送，自动重连，永不阻塞数据源 | `host:2101/mountpoint` |
| ② 局域网 Caster | 把同一路流服务给局域网，任意 NTRIP 客户端可连 | `0.0.0.0:2102`，挂载点 `2RTK` |
| ③ 数传播发 | 单向透传到 USB 数传模块 | 自动扫描端口 @ 115200 |

## 硬件

> **关于真机测试范围**：手上目前只有 **LG290P** 和 **LC29HC** 两块模块，所以只对这两款做了真机测试。
> 其他型号（LC29HBS、N4 等）的驱动与命令集是按厂家手册写的，尚未经过实机验证 —— 用之前请留意，
> 遇到问题欢迎提 issue 反馈。

- **天线** —— 大地测量 / 测量型，稳固安装，**天线相位中心（APC）坐标要事先测好**。
  差分是相对相位中心算的；这个坐标错了，所有流动站都会整体偏移，软件救不回来。
  方法见 [INSTALL.md § 先定位天线相位中心点](INSTALL.md#重要先定位天线相位中心点)。
- **接收机** —— 任何能通过串口输出 RTCM3、能进入基准站模式的模块：
  u-blox ZED-F9P、Unicore UM980/UM982、Septentrio mosaik、Trimble BD9xx 等。
  **网页端标定**需要内置型号档案（LG290P / LC29HBS / N4）；其他模块照样能当数据源用，
  坐标用厂家工具写进去即可。
- **主机** —— 老机顶盒 / 玩客云刷 Armbian（已实测）、树莓派、香橙派，
  或任意 x86 小主机。512 MB 内存足够。
- **数传（可选）** —— 任意透明传输的 USB 数传模块（如 410–470 MHz），
  流动站端配一个对应接收机。

## 卸载

刻意不给浏览器入口 —— 它会停服务、删 systemd 单元、删掉整个安装目录，
在你只能远程访问的设备上，一个确认对话框不足以拦住误点。

```bash
sudo bash /opt/2RTKserver/uninstall.sh                       # 交互式
printf 'y\ny\n' | sudo bash /opt/2RTKserver/uninstall.sh     # 确认卸载 + 两个库都备份到 /root
```

`2RTKserver.db`（管理员口令 + 连接设置）和 `calibration.db`（你实测的基准站坐标，
含未合并的 `-wal`）都会在删除任何东西之前先拷到 `/root/`。

## 忘记管理员密码？

刻意没有网页重置接口。

```bash
sudo systemctl stop 2rtkserver
sudo /opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py --yes
sudo systemctl start 2rtkserver
```

## 文档

- [INSTALL.md](INSTALL.md) —— 安装、配置、天线相位中心、故障排除
- [INSTALL.en.md](INSTALL.en.md) —— English installation guide
- [CHANGELOG.md](CHANGELOG.md) —— 版本更新记录
- [`reset_config.py`](reset_config.py) —— 命令行重置管理员口令与连接设置

标定设计与现场记录：

- [docs/auto-calibration-design.zh.md](docs/auto-calibration-design.zh.md) ——
  完整设计：状态机、租约/看门狗、驱动、精度分级、库结构与迁移
- [docs/cors-calibration-flow.zh.md](docs/cors-calibration-flow.zh.md) ——
  逐步走 CORS 标定流程，标出每个阶段的确切串口命令
- [docs/quectel-lg290p-calibration-commands.zh.md](docs/quectel-lg290p-calibration-commands.zh.md)
  —— LG290P 命令参考（`PQTMCFG*` / `PQTMSVIN*`）与踩过的坑
- [docs/calibration-four-methods-per-module.zh.md](docs/calibration-four-methods-per-module.zh.md)
  —— 四种方式 × 三个模块逐行对照
- [docs/one-click-calibration-framework.zh.md](docs/one-click-calibration-framework.zh.md)
  —— 框架总览，上面三份文档的上游入口
- [docs/architecture-and-calibration-integration.zh.md](docs/architecture-and-calibration-integration.zh.md)
  —— 标定包如何与核心解耦
- **真机测试报告** —— 含失败、根因与修法：
  [全链路 2026-11-03](docs/lg290p-realdevice-fullchain-2026-11-03.zh.md)
  （[English](docs/lg290p-realdevice-fullchain-2026-11-03.en.md)）·
  [CORS 外场测试 2026-11-02](docs/lg290p-cors-field-test-2026-11-02.zh.md)

## 许可与致谢

以 [GNU GPLv3](LICENSE) 发布。任何分发出去的衍生作品必须保持 GPLv3。

- 作者：文七 — i@jia.by
- 2024 年起作为个人基准站工具开始，之后逐步演进出现在这套"数据源 / 出口"架构、
  数传出口、双语界面与各项加固，重构与审查过程中有 AI 协助。
- 感谢 RTKBase 项目展示了自建基准站该长什么样，
  感谢 RTKLIB / pyrtcm 社区提供的协议细节。

欢迎提 issue 和 PR —— 尤其是来自其他板子、其他接收机型号的现场反馈。
