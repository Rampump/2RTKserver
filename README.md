# 2RTK NTRIP Server

一个功能强大的NTRIP服务器应用程序，支持RTK基准站数据的接收和转发。

## 主要特性

- **双模式支持**
  - 串口模式：直接从RTK接收机读取数据
  - 中继模式：从其他NTRIP Caster转发数据

- **Web管理界面**
  - 实时监控系统运行状态
  - 可视化显示卫星信息
  - 基准站位置地图展示
  - 支持高德地图和OpenStreetMap

- **RTCM数据处理**
  - 实时解析RTCM消息
  - 支持多种RTCM消息类型
  - 卫星信号强度监控
  - 自动重连机制

- **安全特性**
  - 用户认证系统
  - 密码保护的配置界面
  - 安全的数据传输

## 技术栈

- **后端**
  - Python
  - Flask Web框架
  - WebSocket实时通信
  - SQLite数据存储

- **前端**
  - HTML5 + TailwindCSS
  - JavaScript
  - OpenLayers地图库
  - 响应式设计

## 系统要求

- Python 3.6+
- 支持的操作系统：Windows/Linux/macOS
- 串口模式需要可用的串口设备
- 网络连接（用于中继模式和Web界面）

## 配置说明

### 串口模式配置
- 串口：自动检测或手动配置
- 波特率：支持多种标准波特率
- Caster设置：服务器地址、端口、挂载点

### 中继模式配置
- 源Caster：服务器地址、端口、挂载点、用户名、密码
- 目标Caster：服务器地址、端口、挂载点、密码

## 使用说明

1. 启动服务器：运行`server.py`
2. 访问Web界面：`http://localhost:5757`
3. 登录系统（默认用户名/密码：admin/admin）
4. 配置运行参数
5. 启动数据转发服务

## 主要功能

- 实时状态监控
- RTCM数据解析和转发
- 卫星信号质量分析
- 基准站位置显示
- 自动故障恢复
- 数据流量统计

## 开发说明

项目结构：
```
2RTKserver/
├── server.py          # 主程序
├── static/            # 静态资源
│   ├── favicon.ico
│   ├── freq_map.json
│   └── script.js
├── templates/         # HTML模板
│   ├── change_password.html
│   ├── config.html
│   ├── index.html
│   └── login.html
└── 2RTKserver.db     # 配置数据库
```