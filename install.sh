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

# 创建安装目录
echo -e "${YELLOW}创建安装目录...${NC}"
mkdir -p $INSTALL_DIR
cd $INSTALL_DIR

# 下载或复制程序文件
echo -e "${YELLOW}复制程序文件...${NC}"
# 如果当前目录已包含程序文件，则复制
if [ -f "./server.py" ]; then
    cp -r ./* $INSTALL_DIR/
else
    # 否则从git仓库克隆（假设有git仓库）
    echo -e "${RED}未找到程序文件${NC}"
    echo "请确保在包含程序文件的目录中运行此脚本"
    exit 1
fi

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
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 设置权限
echo -e "${YELLOW}设置权限...${NC}"
chmod +x $INSTALL_DIR/server.py
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