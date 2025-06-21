# 2RTK NTRIP Server

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

2RTK NTRIP Server是一个用于RTK（Real-Time Kinematic）定位系统的NTRIP server。它允许用户通过网络分发RTCM校正数据，支持多种数据源，包括串口接收机和网络数据流。
   它可以通过从串口上的RTK模块读取数据发送到NTRIPcaster服务器，也可以从其他NTRIPcaster服务器接收数据并转发到自有的NTRIPcaster。
适用于Linux/Armbian环境，已在玩客云等armbian系统上测试通过.也可以用于树莓派等其他armbian系统.

## 功能特点

- 支持NTRIP协议，兼容大多数RTK模块
- 支持多种数据源：串口、NTRIP客户端等
- 内置Web管理界面，可以通过web前端实时修改程序运行模式和配置，方便配置和监控.
  web端会实时监控模块或基准站的实时状态，收星数量、收星质量、RTK状态等.并以图片的形式在前端显示.
  web端在国内使用高德地图标记RTK基站位置，国际使用开源OpenStreetMap地图进行基准站标记.
- 支持数据流转发和中继.
- 支持Linux/Armbian系统自动安装和管理.
![server1.png](https://raw.gitcode.com/user-images/assets/5308990/ad85831d-f013-46cb-b53f-ee34ca620104/server1.png 'server1.png')
![ser3.png](https://raw.gitcode.com/user-images/assets/5308990/bb228481-89e3-4336-a676-92befe6c7cba/ser3.png 'ser3.png')![ser2.png](https://raw.gitcode.com/user-images/assets/5308990/8b60b43d-1a24-4f56-9f5e-b7dd720fc8df/ser2.png 'ser2.png')![ser4.png](https://raw.gitcode.com/user-images/assets/5308990/3ed15432-0f89-47f8-af54-c1d489dd72fb/ser4.png 'ser4.png')
## 安装指南

### 自动安装（Linux/Armbian）

1. 克隆仓库：
   ```bash
   git clone https://gitcode.com/rampump/NTRIPserve.git
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
   git clone https://gitcode.com/rampump/NTRIPserve.git
   cd NTRIPserve
   ```

2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
   
   注意：在某些Armbian系统上，可能需要使用以下命令：
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

## 配置RTK模块

 如使用RTK模块从串口转发数据，请将RTK模块配置为基准站模式，并进入固定状态~ RTCM数据精准度由模块决定，精度越高，数据越准确。

1. 服务器地址：ntrip caster地址
2. 端口：5757
3. 挂载点：在Web界面中配置的挂载点名称
4. 用户名/密码：在Web界面中创建的用户凭据

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
项目链接：[https://gitcode.com/rampump/NTRIPserve](https://gitcode.com/rampump/NTRIPserve)

## 致谢

感谢所有为本项目做出贡献的开发者和用户。
