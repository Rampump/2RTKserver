#!/bin/bash

# 更新脚本 - 2RTK NTRIP Server

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# 安装目录和服务名称
INSTALL_DIR="/opt/2RTKserver"
SERVICE_NAME="2rtkserver"

echo -e "${GREEN}=== 2RTK NTRIP Server 更新程序 ===${NC}"

# 检查是否以root权限运行
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}请以root权限运行此脚本${NC}"
  echo "使用: sudo bash update.sh"
  exit 1
fi

# 检查安装目录是否存在
if [ ! -d "$INSTALL_DIR" ]; then
    echo -e "${RED}安装目录不存在: $INSTALL_DIR${NC}"
    echo "请先安装2RTK NTRIP Server"
    exit 1
fi

# 停止服务
echo -e "${YELLOW}停止服务...${NC}"
systemctl stop $SERVICE_NAME

# 备份配置
echo -e "${YELLOW}备份配置...${NC}"
if [ -f "$INSTALL_DIR/2RTKserver.db" ]; then
    cp $INSTALL_DIR/2RTKserver.db $INSTALL_DIR/2RTKserver.db.bak
    echo "数据库已备份为: $INSTALL_DIR/2RTKserver.db.bak"
fi

# 更新程序文件
echo -e "${YELLOW}更新程序文件...${NC}"
# 如果当前目录已包含程序文件，则复制
if [ -f "./server.py" ]; then
    # 保留数据库文件
    if [ -f "$INSTALL_DIR/2RTKserver.db" ]; then
        mv $INSTALL_DIR/2RTKserver.db $INSTALL_DIR/2RTKserver.db.temp
    fi
    
    # 复制新文件
    cp -r ./* $INSTALL_DIR/
    
    # 恢复数据库文件
    if [ -f "$INSTALL_DIR/2RTKserver.db.temp" ]; then
        mv $INSTALL_DIR/2RTKserver.db.temp $INSTALL_DIR/2RTKserver.db
    fi
else
    echo -e "${RED}未找到程序文件${NC}"
    echo "请确保在包含更新文件的目录中运行此脚本"
    systemctl start $SERVICE_NAME
    exit 1
fi

# 更新Python依赖
echo -e "${YELLOW}更新Python依赖...${NC}"
source $INSTALL_DIR/venv/bin/activate

# 尝试常规安装
if pip install -r requirements.txt; then
    echo -e "${GREEN}依赖更新成功${NC}"
else
    echo -e "${YELLOW}常规更新失败，尝试强制更新...${NC}"
    # 尝试强制安装（针对Armbian系统）
    if PIP_BREAK_SYSTEM_PACKAGES=1 pip install -r requirements.txt; then
        echo -e "${GREEN}依赖强制更新成功${NC}"
    else
        echo -e "${RED}依赖更新失败${NC}"
    fi
fi

# 设置权限
echo -e "${YELLOW}设置权限...${NC}"
chmod +x $INSTALL_DIR/server.py

# 启动服务
echo -e "${YELLOW}启动服务...${NC}"
systemctl start $SERVICE_NAME

echo -e "${GREEN}=== 更新完成! ===${NC}"
echo -e "2RTK NTRIP Server 已更新"
echo -e "${YELLOW}服务已重启，请检查状态:${NC}"
systemctl status $SERVICE_NAME --no-pager

exit 0