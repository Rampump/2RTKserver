#!/bin/bash

# 安装脚本 - 2RTK NTRIP Server
# 用于Linux/Armbian系统的自动安装

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# 安装目录
INSTALL_DIR="/opt/2RTKserver"
SERVICE_NAME="2rtkserver"

echo -e "${GREEN}=== 2RTK NTRIP Server 安装程序 ===${NC}"
echo "此脚本将安装2RTK NTRIP Server及其依赖"

# 检查是否以root权限运行
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}请以root权限运行此脚本${NC}"
  echo "使用: sudo bash install.sh"
  exit 1
fi

# 检查系统
echo -e "${YELLOW}检查系统...${NC}"
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$NAME
    VER=$VERSION_ID
    echo -e "检测到操作系统: ${GREEN}$OS $VER${NC}"
else
    echo -e "${RED}无法确定操作系统类型${NC}"
    echo "继续安装，但可能会遇到兼容性问题"
fi

# 安装系统依赖
echo -e "${YELLOW}安装系统依赖...${NC}"
apt update
apt install -y python3 python3-pip python3-venv git systemd

# 检查 Python 版本（代码用到了 f-string、typing 等 3.8+ 特性）
PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
if [ -z "$PY_VER" ]; then
    echo -e "${RED}未检测到 python3，安装已中止${NC}"
    exit 1
fi
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; }; then
    echo -e "${RED}Python 版本过低（当前 $PY_VER），需要 3.8 或更高${NC}"
    exit 1
fi
echo -e "Python 版本: ${GREEN}$PY_VER${NC}"

# 记录脚本所在目录（也就是源码目录）。
# 原先是先 cd "$INSTALL_DIR" 再检查 ./server.py，导致全新安装时
# 检查的是空的目标目录，必然报"未找到程序文件"退出。
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 创建安装目录
echo -e "${YELLOW}创建安装目录...${NC}"
mkdir -p $INSTALL_DIR

# 下载或复制程序文件
echo -e "${YELLOW}复制程序文件...${NC}"
# 检查源码目录中是否存在程序文件
if [ -f "$SRC_DIR/server.py" ]; then
    # 只复制运行所需的文件，排除 .git / .secret_key / 数据库 / 日志 / __pycache__ 等
    # 开发机或本机运行产生的数据，不应带入干净的目标安装目录。
    # docs 是 README 里引用的截图目录，漏掉会让安装后的 README 图片全变成裂图。
    for item in server.py reset_config.py requirements.txt install.sh uninstall.sh update.sh \
                README.md INSTALL.md INSTALL.en.md LICENSE templates static docs; do
        if [ -e "$SRC_DIR/$item" ]; then
            cp -r "$SRC_DIR/$item" "$INSTALL_DIR/"
        fi
    done
else
    echo -e "${RED}未找到程序文件: $SRC_DIR/server.py${NC}"
    echo "请确保在包含 server.py 的目录中运行此脚本"
    exit 1
fi

# 切换到安装目录，后续操作都在这里进行
cd $INSTALL_DIR

# 创建Python虚拟环境
echo -e "${YELLOW}创建Python虚拟环境...${NC}"
python3 -m venv $INSTALL_DIR/venv
source $INSTALL_DIR/venv/bin/activate

# 安装Python依赖
echo -e "${YELLOW}安装Python依赖...${NC}"
# 尝试常规安装
if pip install -r requirements.txt; then
    echo -e "${GREEN}依赖安装成功${NC}"
else
    echo -e "${YELLOW}常规安装失败，尝试强制安装...${NC}"
    # 尝试强制安装（针对Armbian系统）
    if PIP_BREAK_SYSTEM_PACKAGES=1 pip install -r requirements.txt; then
        echo -e "${GREEN}依赖强制安装成功${NC}"
    else
        echo -e "${RED}依赖安装失败${NC}"
        exit 1
    fi
fi

# 创建systemd服务文件
echo -e "${YELLOW}创建系统服务...${NC}"
cat > /etc/systemd/system/$SERVICE_NAME.service << EOF
[Unit]
Description=2RTK NTRIP Server
# network-online：开机时先等网络就绪，避免上传/中继在没网时白重连几轮
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/server.py
# 关掉 Python 输出缓冲，journalctl 里能实时看到日志（否则只能看到一大块延迟输出）
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
# 停止时给足时间让线程退出、释放端口（本地 Caster / 数传串口）
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF

# 设置权限
echo -e "${YELLOW}设置权限...${NC}"
chmod +x $INSTALL_DIR/server.py
# 命令行恢复默认配置脚本（网页不再提供重置入口，忘记密码时用它）
[ -f "$INSTALL_DIR/reset_config.py" ] && chmod +x $INSTALL_DIR/reset_config.py
chmod 644 /etc/systemd/system/$SERVICE_NAME.service

# 启用并启动服务
echo -e "${YELLOW}启用并启动服务...${NC}"
systemctl daemon-reload
systemctl enable $SERVICE_NAME
systemctl start $SERVICE_NAME

# 创建管理脚本
echo -e "${YELLOW}创建管理脚本...${NC}"
cat > /usr/local/bin/2rtkserver << EOF
#!/bin/bash
case "\$1" in
    start)
        systemctl start $SERVICE_NAME
        ;;
    stop)
        systemctl stop $SERVICE_NAME
        ;;
    restart)
        systemctl restart $SERVICE_NAME
        ;;
    status)
        systemctl status $SERVICE_NAME
        ;;
    *)
        echo "用法: 2rtkserver {start|stop|restart|status}"
        exit 1
        ;;
esac
exit 0
EOF

chmod +x /usr/local/bin/2rtkserver

# 获取IP地址
IP_ADDRESS=$(hostname -I | awk '{print $1}')

echo -e "${GREEN}=== 安装完成! ===${NC}"
echo -e "2RTK NTRIP Server 已安装在 ${GREEN}$INSTALL_DIR${NC}"
echo -e "服务名称: ${GREEN}$SERVICE_NAME${NC}"
echo -e "访问Web界面: ${GREEN}http://$IP_ADDRESS:5757${NC}"
echo -e "管理命令: ${GREEN}2rtkserver {start|stop|restart|status}${NC}"
echo ""
echo -e "${YELLOW}服务已启动，请检查状态:${NC}"
systemctl status $SERVICE_NAME --no-pager

exit 0