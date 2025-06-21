#!/bin/bash

# 卸载脚本 - 2RTK NTRIP Server

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# 安装目录和服务名称
INSTALL_DIR="/opt/2RTKserver"
SERVICE_NAME="2rtkserver"

echo -e "${RED}=== 2RTK NTRIP Server 卸载程序 ===${NC}"
echo "此脚本将卸载2RTK NTRIP Server及其服务"

# 检查是否以root权限运行
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}请以root权限运行此脚本${NC}"
  echo "使用: sudo bash uninstall.sh"
  exit 1
fi

# 确认卸载
read -p "确定要卸载2RTK NTRIP Server吗? (y/n): " confirm
if [[ $confirm != [yY] ]]; then
    echo "卸载已取消"
    exit 0
fi

# 停止并禁用服务
echo -e "${YELLOW}停止并禁用服务...${NC}"
systemctl stop $SERVICE_NAME
systemctl disable $SERVICE_NAME

# 删除服务文件
echo -e "${YELLOW}删除服务文件...${NC}"
rm -f /etc/systemd/system/$SERVICE_NAME.service
systemctl daemon-reload

# 删除管理脚本
echo -e "${YELLOW}删除管理脚本...${NC}"
rm -f /usr/local/bin/2rtkserver

# 删除安装目录
echo -e "${YELLOW}删除安装目录...${NC}"
rm -rf $INSTALL_DIR

echo -e "${GREEN}=== 卸载完成! ===${NC}"
echo "2RTK NTRIP Server 已从系统中移除"

exit 0