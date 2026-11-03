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

# 安装目录里除了程序，还有两个不能再生的资产：
#   2RTKserver.db    管理员口令 + 全部连接配置
#   calibration.db   测出来的基准站坐标（天线相位中心 ECEF）
# 后者尤其致命：坐标没了，所有下游流动站都得回现场重测一次。
# 服务和上面这些都已停掉，此时文件不再被写入，直接拷即可。
if [ -f "$INSTALL_DIR/2RTKserver.db" ] || [ -f "$INSTALL_DIR/calibration.db" ]; then
    echo -e "${YELLOW}注意：以下数据删除后无法恢复${NC}"
    [ -f "$INSTALL_DIR/2RTKserver.db" ] && \
        echo "  · 2RTKserver.db  —— 管理员口令与全部连接配置"
    [ -f "$INSTALL_DIR/calibration.db" ] && \
        echo "  · calibration.db —— 基准站坐标（标定结果，丢了要回现场重测）"
    read -p "是否先备份到 /root/ 下? (y/n): " backup_confirm
    if [[ $backup_confirm == [yY] ]]; then
        STAMP="$(date +%F-%H%M)"
        # 两个库都在 WAL 模式，最近的写入可能还在 -wal 里，必须连带 -wal/-shm 一起拷，
        # 只拷主库文件等于丢掉最后一次修改。
        if [ -f "$INSTALL_DIR/2RTKserver.db" ]; then
            BACKUP_FILE="/root/2RTKserver-config-$STAMP.db"
            cp "$INSTALL_DIR/2RTKserver.db" "$BACKUP_FILE" && echo "配置库已备份: $BACKUP_FILE"
            for suffix in -wal -shm; do
                [ -f "$INSTALL_DIR/2RTKserver.db$suffix" ] && \
                    cp "$INSTALL_DIR/2RTKserver.db$suffix" "$BACKUP_FILE$suffix"
            done
        fi
        if [ -f "$INSTALL_DIR/calibration.db" ]; then
            CALIB_FILE="/root/2RTKserver-calibration-$STAMP.db"
            cp "$INSTALL_DIR/calibration.db" "$CALIB_FILE" && echo "标定库已备份: $CALIB_FILE"
            for suffix in -wal -shm; do
                [ -f "$INSTALL_DIR/calibration.db$suffix" ] && \
                    cp "$INSTALL_DIR/calibration.db$suffix" "$CALIB_FILE$suffix"
            done
        fi
    else
        echo -e "${RED}已选择不备份：标定坐标将永久丢失${NC}"
    fi
fi

rm -rf $INSTALL_DIR

echo -e "${GREEN}=== 卸载完成! ===${NC}"
echo "2RTK NTRIP Server 已从系统中移除"

exit 0