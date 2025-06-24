# 2RTK NTRIP Server

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

2RTK NTRIP Server是一个用于RTK（Real-Time Kinematic）定位系统的NTRIP server。
它可以通过从串口上的RTK模块读取数据发送到NTRIPcaster服务器，也可以从其他NTRIPcaster服务器接收数据并转发到自有的NTRIPcaster。达到单基准站自建CORS的目的.

  适用于Linux/Armbian环境，已在玩客云等armbian系统上测试通过.也可以用于树莓派等其他Linux/Armbian系统.

## 功能特点

- 支持NTRIP协议，兼容大多数RTK模块.
- 支持多种数据源：串口、NTRIP客户端等
- 内置Web管理界面，可以通过web前端实时修改程序运行模式和配置，方便配置和监控.
- web端会实时监控模块或基准站的实时状态，收星数量、收星质量、RTK状态等.并以图片的形式在前端显示.
- web端在标记RTK基站时，会自动获取基站的经纬度信息，然后标记在地图上.
- 在中国可以使用高德地图标记RTK基站位置，其它地区可以使用开源OpenStreetMap地图进行基准站标记.
- 使用地图标记基准站时，会同时标记两个覆盖半径分别为20KM和50KM的圆形覆盖范围.
- PS：在单基准站的RTK系统中，受（电离层误差；对流层误差；卫星轨道误差；多路径误差）影响，有效覆盖范围通常为20KM. 使用双频或者多频RTK接收机时，NTRIP服务有时可实现50公里的覆盖范围且精度良好，但是一般不推荐使用超过50KM的RTK单基准站.
- 支持数据流转发和中继.
- 支持Linux/Armbian系统自动安装和管理.
- 自动重试：当遇到网络波动、服务器连接失败等情况时，程序会自动重连并认证，确保稳定运行。
 
![server.png](https://raw.gitcode.com/user-images/assets/5308990/85709034-dba9-42f0-bc24-d2d1a95efe71/server.png 'server.png')

## 工作模式

- **串口模式**：可自动扫描USB串口，也可指定串口读取RTK模块的RTCM数据，然后转发到本地NTRIP Caster。
               不知道串口名称时可以留空，程序会自动扫描串口.建议填写正确的波特率，程序会优先扫描RTK模块常用的波特率115200。
![serial.png](https://raw.gitcode.com/user-images/assets/5308990/a8620db8-799e-49b8-9269-0746e3f01b5e/serial.png 'serial.png')
- **中继模式**：作为NTRIP客户端，连接其他NTRIP服务器获取RTCM数据，然后转发到本地NTRIP Caster。
![relay.png](https://raw.gitcode.com/user-images/assets/5308990/c1fdff2d-4098-4e6c-a7e0-a62ba0dffc50/relay.png 'relay.png')
- **Caster模式**：#####暂未添加该功能####  作为本地内NTRIP caster，读取串口RTK模块的RTCM数据，然后提供RTCM数据流给移动站等NTRIP客户端.
## 安装指南
 #### 部分Armbian系统因为精简等原因.会导致依赖安装失败，推荐使用手动安装依赖.
### 自动安装（Linux/Armbian）

1. 克隆仓库：
   ```bash
   git clone https://gitcode.com/rampump/NTRIPserver.git 
   cd NTRIPserve
   ```
   备用地址：
   ```
   git clone https://github.com/Rampump/2RTKserver.git
   cd NTRIPserve
   ```
2. 运行安装脚本：
   ```bash
   chmod +x install.sh
   sudo ./install.sh
   ```

3. 安装完成后，可通过以下地址访问Web界面：
   ```
   http://[设备IP]:5757
   ```
4. 默认登录
   ```
   - 用户名：admin  
   - 密码：admin
   访问配置页面需使用管理员账户`admin`和默认密码`admin`登录.配置程序后请修改默认密码.
  ```

### 手动安装



1. 克隆仓库：
   ```bash
   git clone https://gitcode.com/rampump/NTRIPserver.git 

   cd NTRIPserve
   ```
   备用地址：
   ```
   git clone https://github.com/Rampump/2RTKserver.git

   cd NTRIPserve
   ```
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
   
   注意：在某些Armbian系统上，可能需要使用以下命令安装依赖：
   ```bash
   sudo PIP_BREAK_SYSTEM_PACKAGES=1 pip install -r requirements.txt
   ```

3. 运行服务器：
   ```bash
   python server.py
   ```

## 使用方法

### 管理命令

安装完成后，可以使用以下命令管理服务：

- 启动服务：`2rtkserver start`
- 停止服务：`2rtkserver stop`
- 重启服务：`2rtkserver restart`
- 查看状态：`2rtkserver status`

### Web界面

通过Web界面可以进行以下操作：

1. 配置数据源（串口、网络等）
2. 管理数据源
3. 监控数据流状态和连接情况
4. 查看统计信息


### 更新程序

当有新版本可用时：

1. 克隆或下载最新代码
2. 运行更新脚本：
   ```bash
   chmod +x update.sh
   sudo ./update.sh
   ```

### 卸载程序

如需卸载：

```bash
chmod +x uninstall.sh
sudo ./uninstall.sh
```

## 配置RTK基准站模块

 如使用RTK模块从串口转发数据，请将RTK模块配置为基准站模式，并进入固定状态~ RTCM数据精准度由模块决定，精度越高，数据越准确。

1. 服务器地址：ntrip caster地址
2. 端口：5757
3. 挂载点：在Web界面中配置的挂载点名称
4. 用户名/密码：在Web界面中创建的用户凭据

## 技术特点

本程序基于单基站RTK技术。基站观测卫星信号计算出校正数，发送给移动站，帮助移动站精确计算位置。其优点是结构简单、成本低。但有效范围有限，一般在基站周围20-50公里范围内。随着距离增加，误差会逐渐增大。与千寻CORS、移动CORS等多基站RTK技术相比，覆盖范围较小。但它可以在小范围内提供精确定位，适合预算有限的用户，或者自己搭建CORS使用无人机等设备。

## 部署与使用

目前主要用于玩客云的Armbian系统上。只需将RTK基站模块插入玩客云的USB口，程序即可读取数据并转发到Caster。部署操作简单。此外，它可以适配CORS，为数据源和传输提供更多选择。

## 故障排除

常见问题及解决方法请参考 [INSTALL.md](INSTALL.md) 文档中的故障排除部分。

## 贡献指南

欢迎提交问题报告、功能请求和代码贡献。请遵循以下步骤：

1. Fork 仓库
2. 创建功能分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add some amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建Pull Request

## 许可证

本项目采用 GNU通用公共许可证v3 (GPLv3) - 详情请参阅 [LICENSE](LICENSE) 文件

GPL许可证要求任何分发此软件的修改版本也必须以相同的许可证开源。

## 联系方式

作者：文七  
邮箱：i@jia.by  
项目链接：[https://gitcode.com/rampump/NTRIPserver](https://gitcode.com/rampump/NTRIPserver)

## 致谢

感谢所有为本项目做出贡献的开发者和用户。