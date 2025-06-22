# 2RTK NTRIP server 安装指南

本文档提供了在Linux/Armbian系统上安装、更新和卸载2RTK NTRIP server的详细说明。

## 安装步骤

1. 将所有程序文件和安装脚本放在同一目录下
2. 给安装脚本添加执行权限：
   ```bash
   chmod +x install.sh
   ```
3. 以root权限运行安装脚本：
   ```bash
   sudo ./install.sh
   ```

安装过程将自动完成以下操作：
- 安装必要的系统依赖
- 创建Python虚拟环境并安装依赖
- 配置系统服务
- 设置自启动
- 创建管理命令

## 管理命令

安装完成后，可以使用以下命令管理服务：

- 启动服务：`ntripserver start`
- 停止服务：`ntripserver stop`
- 重启服务：`ntripserver restart`
- 查看状态：`ntripserver status`

## 访问Web界面

安装完成后，可以通过以下地址访问Web界面：
- `http://[设备IP]:5757`

默认登录凭据：
- 用户名：admin
- 密码：admin

## 更新程序

当有新版本可用时，按照以下步骤更新：

1. 将更新文件和更新脚本放在同一目录下
2. 给更新脚本添加执行权限：
   ```bash
   chmod +x update.sh
   ```
3. 以root权限运行更新脚本：
   ```bash
   sudo ./update.sh
   ```

更新过程将自动完成以下操作：
- 备份现有配置
- 更新程序文件
- 更新Python依赖
- 重启服务

## 卸载程序

如需卸载2RTK NTRIP server，请按照以下步骤操作：

1. 给卸载脚本添加执行权限：
   ```bash
   chmod +x uninstall.sh
   ```
2. 以root权限运行卸载脚本：
   ```bash
   sudo ./uninstall.sh
   ```

卸载过程将自动完成以下操作：
- 停止并禁用服务
- 删除服务文件
- 删除管理脚本
- 删除安装目录

## 故障排除

### 依赖安装失败

如果在安装Python依赖时遇到问题，脚本会自动尝试使用以下命令强制安装：
```bash
PIP_BREAK_SYSTEM_PACKAGES=1 pip install -r requirements.txt
```

### 服务无法启动

如果服务无法启动，请检查日志：
```bash
journalctl -u ntripserver -n 50
```

### 无法访问Web界面

1. 确认服务正在运行：
   ```bash
   ntripserver status
   ```
2. 检查防火墙设置，确保端口5757已开放：
   ```bash
   sudo ufw status
   ```
   如需开放端口：
   ```bash
   sudo ufw allow 5757/tcp
   ```

## 系统要求

- Python 3.6+
- 支持的操作系统：Linux/Armbian
- 串口模式需要可用的串口设备
- 网络连接（用于中继模式和Web界面）