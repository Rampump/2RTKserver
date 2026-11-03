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

# 记录脚本所在目录（源码目录），避免依赖当前工作目录
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 更新程序文件
echo -e "${YELLOW}更新程序文件...${NC}"
# 检查源码目录中是否存在程序文件
if [ -f "$SRC_DIR/server.py" ]; then
    # 保留数据库文件
    if [ -f "$INSTALL_DIR/2RTKserver.db" ]; then
        mv $INSTALL_DIR/2RTKserver.db $INSTALL_DIR/2RTKserver.db.temp
    fi

    # 只复制运行所需的文件，排除 .git / .secret_key / 数据库 / 日志 / __pycache__ / venv，
    # 避免把开发机的数据带入，也避免覆盖安装目录里已经建好的 venv 虚拟环境。
    #
    # rtkserver/ 必须先删再复制：拆包后模块会增删改名，直接覆盖会让已删除的模块
    # 留下 .pyc，import 时指向不存在的源码，表现为"改了代码不生效"或随机
    # ModuleNotFoundError。
    rm -rf "$INSTALL_DIR/rtkserver"
    find "$INSTALL_DIR" -maxdepth 2 -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null
    for item in server.py rtkserver reset_config.py requirements.txt install.sh uninstall.sh update.sh \
                VERSION README.md INSTALL.md INSTALL.en.md LICENSE templates static docs; do
        if [ -e "$SRC_DIR/$item" ]; then
            cp -r "$SRC_DIR/$item" "$INSTALL_DIR/"
        fi
    done

    # 复制后必须确认关键文件都在：上面已经把 rtkserver/ 整个删过了，若磁盘满或
    # 权限异常，cp 会中途静默失败一半，留下一个"能启动但 import 就炸"的安装。
    # 这里把配置库先还原再退出，绝不能让它停在 2RTKserver.db.temp 上——
    # 那样服务下次启动会当成全新安装，用户的配置和口令全没了。
    if [ ! -f "$INSTALL_DIR/server.py" ] || [ ! -f "$INSTALL_DIR/rtkserver/__init__.py" ]; then
        echo -e "${RED}程序文件复制不完整（server.py 或 rtkserver/ 缺失）${NC}"
        echo "常见原因：磁盘空间不足、安装目录权限异常。"
        if [ -f "$INSTALL_DIR/2RTKserver.db.temp" ]; then
            mv "$INSTALL_DIR/2RTKserver.db.temp" "$INSTALL_DIR/2RTKserver.db"
            echo "配置库已还原，未丢失。"
        fi
        echo "请清理空间 / 修正权限后重新运行本脚本。"
        exit 1
    fi

    # 同步版本号，否则网页上的「当前版本 / 检查更新」会永远停在装机那一版
    # 允许调用方显式指定（升级执行器/离线包没有 .git 时用）
    APP_VER="$(head -n1 "$SRC_DIR/VERSION" 2>/dev/null | tr -d '[:space:]')"
    SRC_COMMIT="${SRC_COMMIT_OVERRIDE:-}"
    if [ -z "$SRC_COMMIT" ] && [ -d "$SRC_DIR/.git" ] && command -v git >/dev/null 2>&1; then
        SRC_COMMIT=$(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || true)
    fi
    printf '{"app":"%s","commit":"%s","date":"%s","installed_at":"%s","source":"update.sh","channel":"update"}\n' \
        "${APP_VER}" "${SRC_COMMIT}" "$(date '+%Y-%m-%d %H:%M:%S')" "$(date '+%Y-%m-%d %H:%M:%S')" \
        > "$INSTALL_DIR/.version"

    # 恢复数据库文件
    if [ -f "$INSTALL_DIR/2RTKserver.db.temp" ]; then
        mv $INSTALL_DIR/2RTKserver.db.temp $INSTALL_DIR/2RTKserver.db
    fi

    # 必须切到安装目录：下面 `pip install -r requirements.txt` 用的是相对路径，
    # 而本脚本可能在任意 cwd 下被调用（例如 systemd-run 或升级执行器），
    # 不切目录就会报 "Could not open requirements file"，依赖静默不更新。
    cd "$INSTALL_DIR" || exit 1
else
    echo -e "${RED}未找到程序文件: $SRC_DIR/server.py${NC}"
    echo "请确保在包含更新文件的目录中运行此脚本"
    systemctl start $SERVICE_NAME
    exit 1
fi

# 更新Python依赖
echo -e "${YELLOW}更新Python依赖...${NC}"
source $INSTALL_DIR/venv/bin/activate

# 老 pip 认不出新 wheel 标签时会退化成源码编译并失败，先自升级一轮（失败不致命）
pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true

# 尝试常规安装
if pip install -r requirements.txt; then
    echo -e "${GREEN}依赖更新成功${NC}"
else
    echo -e "${YELLOW}常规更新失败，尝试强制更新...${NC}"
    # 尝试强制安装（针对Armbian系统）
    if PIP_BREAK_SYSTEM_PACKAGES=1 pip install -r requirements.txt; then
        echo -e "${GREEN}依赖强制更新成功${NC}"
    else
        # 这里**不退出**：文件已经换好，服务起来后能在日志里看到真实报错，
        # 直接中止反而会停在一个"代码是新的、服务没起"的状态。
        echo -e "${RED}依赖更新失败${NC}"
        echo -e "${YELLOW}服务仍会尝试启动；若无法启动请手动执行：${NC}"
        echo "  $INSTALL_DIR/venv/bin/pip install -r $INSTALL_DIR/requirements.txt"
    fi
fi

# 依赖是否真的可用：pip 报成功不等于 import 得动（装错环境 / so 依赖缺失）
for mod in serial flask flask_sock pyrtcm; do
    if ! $INSTALL_DIR/venv/bin/python -c "import $mod" 2>/dev/null; then
        echo -e "${RED}依赖不可用: $mod —— 服务可能起不来${NC}"
    fi
done

# 设置权限
echo -e "${YELLOW}设置权限...${NC}"
chmod +x $INSTALL_DIR/server.py
[ -f "$INSTALL_DIR/reset_config.py" ] && chmod +x $INSTALL_DIR/reset_config.py

# 启动服务
echo -e "${YELLOW}启动服务...${NC}"
LOG_BASE=$( [ -f "$INSTALL_DIR/server.log" ] && wc -l < "$INSTALL_DIR/server.log" | tr -d ' ' || echo 0 )
systemctl start $SERVICE_NAME

# 固定地址（mDNS）是程序启动后自己广播的，名字还可能因为局域网里已有同名设备
# 而退让成 2rtk-2.local，所以不能在这里写死 2rtk.local —— 等它把实际地址
# 写进日志再读出来；读不到（例如系统缺 zeroconf）就退回默认名。
LOG_FILE="$INSTALL_DIR/server.log"
LOG_BASE=${LOG_BASE:-0}
echo -e "${YELLOW}读取固定地址（mDNS）...${NC}"
MDNS_URL=""
for _ in $(seq 1 30); do
    MDNS_URL=$(tail -n +$((LOG_BASE + 1)) "$LOG_FILE" 2>/dev/null \
        | grep -oE 'http://[A-Za-z0-9._-]+\.local:[0-9]+' | tail -1)
    [ -n "$MDNS_URL" ] && break
    sleep 0.5
done
[ -z "$MDNS_URL" ] && MDNS_URL="http://2rtk.local:5757"

IP_ADDRESS=$(hostname -I | awk '{print $1}')
echo -e "${GREEN}=== 更新完成! ===${NC}"
echo -e "2RTK NTRIP Server 已更新"
echo -e "  本机 IP    : ${GREEN}http://$IP_ADDRESS:5757${NC}"
echo -e "  固定地址   : ${GREEN}$MDNS_URL${NC}   （Windows / macOS / iPhone）"
echo -e "  ${YELLOW}安卓 / 鸿蒙手机不解析 .local，请用上面的 IP 地址访问${NC}"
echo -e "  ${YELLOW}固定地址打不开（浏览器报 502）时：把 *.local 加入代理直连，或临时关掉代理 / VPN${NC}"
echo -e "${YELLOW}服务已重启，请检查状态:${NC}"
systemctl status $SERVICE_NAME --no-pager

exit 0