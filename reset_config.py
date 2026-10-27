#!/usr/bin/env python3
"""命令行恢复默认配置（把管理员重置回 admin/admin，并清空所有 Caster/串口设置）。

为什么不做成网页按钮：
    网页重置接口无法可靠区分「设备本机」和「经由反向代理的远程请求」——
    TCP 对端地址在代理后面恒为 127.0.0.1，"本机"判断会失效，
    等于把无鉴权的重置接口重新敞开。因此这类破坏性操作只保留 SSH 命令行入口。

用法（在设备上 SSH 登录后执行）：
    # 建议先停服务，避免程序用旧的内存配置又写回数据库
    sudo systemctl stop 2rtkserver
    /opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py
    sudo systemctl start 2rtkserver

    # 非交互（脚本化）
    /opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py --yes

    # 重置并自动重启服务（需要在 systemd 环境下、以 root 执行）
    sudo /opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py --yes --restart
"""
import argparse
import os
import subprocess
import sys

SERVICE_NAME = '2rtkserver'


def main() -> int:
    parser = argparse.ArgumentParser(
        description='恢复默认配置：管理员账号重置为 admin/admin，所有连接设置清空。'
    )
    parser.add_argument('-y', '--yes', action='store_true', help='跳过确认提示')
    parser.add_argument('--restart', action='store_true',
                        help=f'重置后执行 systemctl restart {SERVICE_NAME}')
    args = parser.parse_args()

    # 关键：server.py 里 CONFIG_DB / SECRET_KEY_FILE 都是相对路径，
    # 必须先切到脚本所在目录，否则在别的目录执行会在那里新建一个空数据库。
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)

    db_path = os.path.join(here, '2RTKserver.db')

    print('=' * 56)
    print(' 2RTKserver 配置恢复')
    print('=' * 56)
    print(f'程序目录: {here}')
    print(f'数据库  : {db_path}')
    print()
    print('将要执行的操作：')
    print('  - 管理员账号重置为 admin / admin')
    print('  - 清空 caster / relay / 串口 等全部连接设置')
    print('  - 运行模式重置为「未配置」')
    print()

    if not os.path.exists(db_path):
        print(f'[错误] 未找到数据库文件: {db_path}')
        print('       请确认程序确实安装在该目录下。')
        return 1

    if not args.yes:
        answer = input('确认继续？输入 yes 回车: ').strip().lower()
        if answer not in ('yes', 'y'):
            print('已取消，未做任何修改。')
            return 1

    try:
        # 导入 server 只为了复用默认配置与口令哈希逻辑，保持单一数据来源。
        # 该模块在 import 阶段会创建 Flask app 和会话密钥，不会启动服务，可安全导入。
        import server
    except Exception as e:
        print(f'[错误] 无法导入 server.py: {e}')
        print('       请用程序自带的解释器执行，例如：')
        print(f'       {os.path.join(here, "venv", "bin", "python")} {os.path.abspath(__file__)}')
        return 1

    try:
        # 口令会以 PBKDF2 哈希写入，不会在数据库里留下明文
        server.reset_config_to_default()
    except Exception as e:
        print(f'[错误] 写入默认配置失败: {e}')
        return 1

    print()
    print('[完成] 配置已恢复默认：')
    print('       用户名: admin')
    print('       密码  : admin')
    print()
    print('  ⚠ 请立即在 Web 配置页修改默认密码。')

    if args.restart:
        print()
        print(f'[执行] systemctl restart {SERVICE_NAME} ...')
        try:
            subprocess.run(['systemctl', 'restart', SERVICE_NAME], check=True)
            print('[完成] 服务已重启，新配置已生效。')
        except Exception as e:
            print(f'[警告] 重启服务失败: {e}')
            print(f'       请手动执行: sudo systemctl restart {SERVICE_NAME}')
            return 1
    else:
        print()
        print('  ⚠ 如果服务正在运行，需要重启后新配置才会生效：')
        print(f'       sudo systemctl restart {SERVICE_NAME}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
