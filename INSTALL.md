# 2RTK NTRIP Server 安装指南

> 面向 Armbian / Debian / Ubuntu 等 Linux 小主机（玩客云、树莓派、香橙派等）。
> 一个 RTK 基准站模块 + 一台小主机 + 家里的宽带，即可搭建你自己的 GNSS 基准站，
> 供移动站 / 手簿 / 无人机在 20~50 公里内获取厘米级差分改正。

---

## 目录

1. [准备条件](#准备条件)
2. [重要：先定位天线相位中心点](#重要先定位天线相位中心点)
3. [自动安装](#自动安装)
4. [手动安装](#手动安装)
5. [配置基准站](#配置基准站)
6. [管理命令](#管理命令)
7. [更新与卸载](#更新与卸载)
8. [配置保存在哪里](#配置保存在哪里)
9. [故障排除](#故障排除)

---

## 准备条件

- **硬件**：一台 Linux/Armbian 小主机（x86_64 / arm64 / armhf 均可），一个 RTK/GNSS 基准站模块（支持串口输出 RTCM3），一根 USB 转串口线。
- **软件**：Python 3.8+（推荐 3.10+，Armbian/Debian 11、Ubuntu 22.04 自带即可）。
- **网络**：局域网即可运行；中继模式、地图瓦片、前端图标需要能访问外网。
- **建议**：给设备固定一个内网 IP，方便移动站/手簿稳定连接。

---

## 重要：先定位天线相位中心点

> ⚠️ **这一步直接决定你的基准站数据是否精准，务必在首次使用前完成。**

RTK 基准站输出的差分改正数，是**相对天线相位中心点（Antenna Phase Center, APC）**计算的。
如果这个坐标错了，所有移动站解算出来的位置都会跟着整体偏移，精度无从谈起。

### 什么是天线相位中心

- 卫星信号并不是在天线的**物理几何中心**被接收的，而是在一个叫做"**相位中心**"的点上。
- 对于测量型天线，相位中心通常在天线顶部的某个位置，**不一定等于天线的几何中心或螺丝孔位置**。
- 天线厂商会在说明书里标注"相位中心偏移量"（Phase Center Offset, PCO），
  即相位中心相对天线参考点（Antenna Reference Point, ARP，一般是天线底部螺丝孔）的三维偏移。

### 如何获得准确的坐标

按精度从高到低，任选其一：

1. **（推荐）静态测量标定**：把天线架设在已知点上，用另一台高精度接收机做长时间静态观测，
   解算出天线的精确坐标。这是最准确的方式。
2. **已知控制点**：如果你有已知坐标的控制点（如测绘部门的 CORS 点、已知基准点），
   把天线架设在该点上，输入该点的坐标即可。
3. **长时间单点平均**：让模块以基准站模式连续运行数小时，取单点定位坐标的平均值
   作为近似坐标。精度约米级，仅适合对精度要求不高的场景。
4. **查地图估算**：在 Google Earth / 奥维地图等工具上点出天线位置，读取经纬度。
   精度受地图精度影响，仅作临时测试。

### 把坐标写进模块

拿到准确坐标后，需要通过模块的配置工具（通常是厂商提供的上位机软件或串口命令）
把**天线相位中心的经纬度和椭球高**写入模块，模块才会在输出的 RTCM 1005/1006 报文中
携带正确坐标。本程序会从这些报文中解析坐标并用于地图显示和 NTRIP 源列表（STR）。

> 如何确认坐标已生效：安装并运行本程序后，打开 Web 首页的"基准站位置"卡片，
> 看地图上的标记是否落在你预期的位置；或看"本地 NTRIP Caster"的源列表（STR 行）
> 里的经纬度是否与你写入的一致。如果不一致，说明模块里的坐标还没配好。

---

## 自动安装

### 一键安装（推荐）

复制下面**这一整行**到设备终端回车，全程自动（缺 git 会先装 git，先试国内 GitCode 镜像、
连不上自动退到 GitHub）：

```bash
sudo apt-get install -y git && sudo rm -rf /tmp/2rtk-install && (sudo git clone --depth 1 https://gitcode.com/rampump/NTRIPserver.git /tmp/2rtk-install || (sudo rm -rf /tmp/2rtk-install && sudo git clone --depth 1 https://github.com/Rampump/2RTKserver.git /tmp/2rtk-install)) && sudo bash /tmp/2rtk-install/install.sh
```

> **从 Windows 复制注意**
> - 命令是纯 ASCII 单行，直接粘贴即可，不要手打、不要中途切中文输入法（全角标点会报语法错）。
> - 不要经记事本 / Word 中转保存成 `.sh`：Windows 会写入 `CR LF`，Armbian 上 bash 报
>   `$'\r': command not found`，脚本看起来"没执行就退出"。必须用文件时先
>   `sed -i 's/\r$//' 文件名`。
> - 浏览器窗口窄会把命令折行显示，复制时确认整行完整、末尾没有被截断。

### 分步安装

1. 克隆仓库：

   ```bash
   git clone https://github.com/Rampump/2RTKserver.git
   cd 2RTKserver
   ```

   （备用地址：`https://gitcode.com/rampump/NTRIPserver.git`）

2. 运行安装脚本：

   ```bash
   chmod +x install.sh
   sudo ./install.sh
   ```

   脚本会自动完成：安装系统依赖 → 创建 Python 虚拟环境 → 安装依赖 → 配置 systemd 服务 → 设置开机自启 → 创建管理命令。

3. 访问 Web 界面 —— 安装结束时会同时打印两个地址：

   ```
   访问地址：
     本机 IP    : http://192.168.1.71:5757
     固定地址   : http://2rtk.local:5757   （Windows / macOS / iPhone，换网络也不用改）
     安卓 / 鸿蒙手机不解析 .local，请用上面的 IP 地址访问
   ```

   固定地址是从运行中的服务日志里读出来的实际值：如果局域网里已经有别的设备占了
   `2rtk`，这里会直接显示退让后的 `http://2rtk-2.local:5757`，不会显示错的。

4. 默认登录（**登录后请立即修改密码**）：

   ```
   用户名：admin
   密码  ：admin
   ```

---

## 手动安装

部分精简版 Armbian 系统的包管理器可能无法自动装依赖，可手动安装：

```bash
git clone https://github.com/Rampump/2RTKserver.git
cd 2RTKserver

# 安装依赖（若提示 externally-managed-environment，加 PIP_BREAK_SYSTEM_PACKAGES=1）
pip install -r requirements.txt
# 或
sudo PIP_BREAK_SYSTEM_PACKAGES=1 pip install -r requirements.txt

# 运行
python3 server.py
```

启动后，同一内网里可以直接访问固定地址 `http://2rtk.local:5757`（Windows / macOS /
iPhone / iPad / Linux+avahi 都支持）。**安卓手机 / 平板不支持 `.local`，请改用
`http://设备IP:5757`，设备 IP 在首页「运行状态」卡里能看到。**

固定地址依赖 `requirements.txt` 里的 `zeroconf`。没装上也不影响其它功能，
只是少了这个别名；装好后重启服务即可生效。

同网段里已经有一台设备叫 `2rtk` 时，本机会自动退为 `2rtk-2.local`，日志里有一行
WARNING，首页「固定地址」那行显示的也是实际名字。

> **升级请用 `update.sh`**：`install.sh` 结尾只执行 `systemctl start`，对已在运行的服务
> 是空操作 —— 文件换了，进程还是旧代码。`update.sh` 会先停服务、备份数据库、清掉旧
> `rtkserver/` 再复制，最后启动。

---

## 配置基准站

1. 登录 Web 界面，进入「运行设置」。
2. 选择运行模式：
   - **serial 串口模式**：从 USB 串口读取 RTK 模块的 RTCM 数据，再上传到上游 Caster。
     串口名留空会自动扫描；建议填对波特率（RTK 模块常用 115200）。
   - **relay 中继模式**：从另一个 NTRIP Caster 拉取数据再转发（用于自建 CORS）。
   - **none**：仅采集不转发。
3. 填入上传端 Caster 的地址 / 端口 / 挂载点 / 密码。
4. （可选）勾选「启用本地 Caster」，让内网移动站直连本机取差分：
   - 默认监听 `0.0.0.0:2102`，挂载点 `2RTK`，账号密码均为 `2RTK`。
   - 移动站/手簿填 `设备IP:2102/2RTK` 即可。
   - **端口说明**：本地 Caster 的监听端口会在本机真实 `bind`，而中继/上传端填的端口是**对端端口**，
     本机只是出站连接（用内核临时端口），所以它们可以都用 2101 而不冲突。默认把本地 Caster 设成
     2102 只是为了与出口默认端口错开；配置页会检查本机端口是否真被占用，并禁止两者填同一个值。
5. （可选）勾选「启用无线电数传透传」，把同一路 RTCM 数据透传到 USB 数传电台：
   - 在 Armbian 上插入一个 USB 数传电台（发送端），附近无网络的流动站用配对电台（接收端）即可直接收差分，**无需 NTRIP 验证**。
   - **单向广播，只发不收**：程序只往串口写 RTCM 字节流，不解析、不加任何协议。数传电台是串口透传设备，收发端频点/信道/波特率需保持一致。
   - 串口路径可手填，也可点击输入框自动扫描/枚举；枚举时会**自动禁用已用作 GNSS 模块的串口**，避免误选冲突。
   - 波特率默认 115200（可改），须 ≥ 数传电台空口速率，否则数据积压导致流动站掉固定。
   - 数传开关、本地 Caster 开关、运行模式三者**完全独立**，可同时开、也可单独开，互不影响。
6. 点「保存并重启」。

---

## 管理命令

安装完成后：

```bash
2rtkserver start     # 启动
2rtkserver stop      # 停止
2rtkserver restart   # 重启
2rtkserver status    # 查看状态
```

---

## 更新与卸载

命令行方式：

```bash
chmod +x update.sh
sudo ./update.sh     # 自动备份配置、更新文件、重启服务
```

```bash
chmod +x uninstall.sh
sudo ./uninstall.sh  # 停止并删除服务与安装目录
```

源码目录已经删掉、只想清干净系统时，用这一行（两个 `y` 分别是"确认卸载"和"先把配置库备份到 /root"）：

```bash
printf 'y\ny\n' | sudo bash /opt/2RTKserver/uninstall.sh
```

网页方式（不用 SSH，登录后进 **运行设置 → 系统维护**）：

| 按钮 | 行为 |
| --- | --- |
| 检查更新 | 同时探测两个仓库，列出各自的提交号与是否有更新 |
| 一键升级 | 拉取新版本 → 校验（语法编译 + 导入冒烟 + 仓库自带测试）→ 备份 → 停服并等端口真正释放 → 替换文件 → 装依赖 → 启动 → 健康检查；任何一步失败自动回退到原版本 |
| 卸载程序 | 需再次输入管理员口令并输入 `DELETE`，配置库先备份到 `/root/2RTKserver-<时间>.db` |

升级期间 Web 服务会停止几秒到几十秒，页面上的连接会断——这是正常的，页面会自己
继续轮询并在结束后自动刷新；失败时会显示具体原因，并明确告知已退回升级前的版本。

### 版本号只有一个来源

版本号写在程序根目录的 `VERSION` 文件里（一行 `主.次.修`，例如 `2.2.2`）：

- 网页「系统维护」显示 `v<版本> · <提交号>`：版本读 `VERSION`，提交号读 `.version`；
- 控制台横幅、mDNS 广播的版本字段也都从这一份读，**代码里不再出现任何硬编码版本号**
  （`tests/test_contract.py::TestVersionSingleSource` 会拦住写死的版本串）；
- `install.sh` / `update.sh` / 升级执行器都会把 `VERSION` 一起复制，并把它记进 `.version`；
- 发新版本时**只改 `VERSION` 这一行**再提交即可。万一打包漏了 `VERSION`，界面会显示
  `v0.0.0`——看到它就是提醒"这包不完整"，而不是真有个上古版本。

两个仓库地址（**仓库名不同，属正常**，代码里按地址区分，不要求同名）：

- GitCode 镜像：`https://gitcode.com/rampump/NTRIPserver.git`（国内可达，优先）
- GitHub 官方：`https://github.com/Rampump/2RTKserver.git`

升级时两个源**并发探测**，哪个先通就用哪个，所以访问不了 GitHub 的国内设备也能升级。
如需换成自己的镜像，写 `/opt/2RTKserver/.upgrade/sources.json` 即可，不必改代码：

```json
{
  "gitcode": "https://gitcode.com/your-name/your-repo.git",
  "github":  "https://github.com/your-name/your-repo.git",
  "branch":  "main",
  "order":   ["gitcode", "github"]
}
```

升级备份放在 `/opt/2RTKserver/.upgrade/backups/`（保留最近 3 份），升级日志在
`/opt/2RTKserver/.upgrade/upgrade.log`，执行器本身的日志用
`journalctl -u 2rtk-upgrader -n 100` 查看。

---

## 配置保存在哪里

- 所有配置存在**程序目录下的 `2RTKserver.db`**（SQLite），管理员口令以 PBKDF2 哈希存储。
- 重启、断电、服务崩溃被 systemd 拉起，配置和密码都原样保留。
- 数据库、日志 `server.log`、会话密钥 `.secret_key` 都以**程序文件所在目录**为基准，
  在任意目录执行 `python3 /opt/2RTKserver/server.py` 也能读到同一份配置。

备份（推荐定期做）：

```bash
sudo cp /opt/2RTKserver/2RTKserver.db /root/2RTKserver-$(date +%F).db
```

恢复：停服务 → 覆盖该文件 → 启动服务。

---

## 故障排除

### 依赖安装失败

脚本会自动回退到强制安装：

```bash
PIP_BREAK_SYSTEM_PACKAGES=1 pip install -r requirements.txt
```

### 服务无法启动

```bash
journalctl -u 2rtkserver -n 50
```

### 本地 Caster 启动失败 / 提示端口被占用

日志里出现 `无法监听 0.0.0.0:2102（[Errno 98] Address already in use）`，说明**本机**这个端口
已经有别的程序在监听（最常见是上一轮进程还没退干净，或另装了一套 NTRIP 软件）。

```bash
ss -lntp | grep :2102
sudo lsof -i :2102
```

按占用者处理：旧实例用 `sudo 2rtkserver restart` 或 `pkill` 清掉；是别的服务就在配置页把本地
Caster 换一个端口（如 2103）再保存。配置页现在会在保存前试绑一次，并直接把占用进程的 pid 和
名字显示出来。

> 注意：中继/上传端填的 2101 是**对端端口**，本机出站连接不会占用本机 2101，所以它和本地 Caster
> 的端口本来就不冲突。

### 无法访问 Web 界面

1. 确认服务运行：`2rtkserver status`
2. 检查防火墙放行 5757 端口：

   ```bash
   sudo ufw allow 5757/tcp
   ```

### 忘记管理员密码

网页不提供重置入口（安全考虑）。SSH 登录设备后执行：

```bash
sudo systemctl stop 2rtkserver
sudo /opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py --yes
sudo systemctl start 2rtkserver
```

重置后管理员恢复为 `admin / admin`，全部连接设置清空，请立即改密码。

### 地图不显示 / 瓦片加载失败

首页地图默认用高德（国内），失败会自动切换 OpenStreetMap 多镜像。若全部失败，
通常是设备外网不通，不影响差分数据，仅影响地图显示。

---

## 安全提示

- Web 管理端口 5757 默认监听 `0.0.0.0`，**仅适合内网/局域网**，不要直接暴露公网。
- 确需远程访问时，请加 Nginx + HTTPS 并配置访问控制，且先改默认密码。
- 登录连续失败 5 次锁定 5 分钟。
- 未登录访问者能看到信号统计，但看不到基准站精确坐标；如需公开坐标，设环境变量
  `PUBLIC_STATION_COORDS=1`（不建议）。
- `2RTKserver.db` 内含明文上传密码，备份文件请妥善保管，勿放到公开位置。
