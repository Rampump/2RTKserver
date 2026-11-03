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

    # 连标定记录一起清（危险，默认**不做**）
    sudo /opt/2RTKserver/venv/bin/python /opt/2RTKserver/reset_config.py --purge-calibration

为什么默认不清 calibration.db（方案 §12.1.1）：
    "重置配置"的语义是"把连接参数和管理员口令恢复默认"，而标定记录是
    **测量出来的资产**（天线相位中心的精确 ECEF）。配置重置之后重新填一遍就行，
    坐标没了必须扛着设备回到现场重测。两者混在一次操作里删掉，是那种
    "点完才发现"的不可逆事故，所以必须由人显式加参数再确认一次。
"""
import argparse
import os
import subprocess
import sys

SERVICE_NAME = '2rtkserver'
CALIBRATION_DB = 'calibration.db'
CALIBRATION_DIR = '.calibration'

# 二次确认要打的那个词。用大写单词而不是 y/n：这个操作没有撤销途径，
# 手指滑一下回车不应该等于"删掉站点坐标"。
PURGE_WORD = 'DELETE'


def _calibration_files(here):
    """返回标定数据的绝对路径清单：库（含 WAL/SHM）+ 运行目录里的状态文件。"""
    paths = []
    for name in (CALIBRATION_DB, CALIBRATION_DB + '-wal', CALIBRATION_DB + '-shm'):
        paths.append(os.path.join(here, name))
    for name in ('lease.json', 'state.json', 'backup.json'):
        paths.append(os.path.join(here, CALIBRATION_DIR, name))
    return paths


def purge_calibration(here, assume_yes=False):
    """删掉标定库与标定运行状态。**只有显式传 --purge-calibration 才会走到这里。**

    返回 0 表示已清（或本来就没有），1 表示用户放弃。
    """
    targets = [p for p in _calibration_files(here) if os.path.exists(p)]
    if not targets:
        print('[信息] 没有找到标定数据（calibration.db 不存在），无需清理。')
        return 0

    print()
    print('=' * 56)
    print(' 危险：即将删除标定数据（不可恢复）')
    print('=' * 56)
    for path in targets:
        size = 0
        try:
            size = os.path.getsize(path)
        except OSError:
            pass
        print(f'  - {path}  ({size} 字节)')
    print()
    print('  这些文件里是天线相位中心坐标（站点资产）。删除后：')
    print('    · 历史标定记录全部丢失；')
    print('    · 「还原标定前配置」将不可用；')
    print('    · 想恢复只能回到现场重新标定。')
    print()

    if not assume_yes:
        answer = input(f'确认删除请输入 {PURGE_WORD} 回车（其它任意输入取消）: ').strip()
        if answer != PURGE_WORD:
            print('已取消，标定数据未做任何修改。')
            return 1

    removed, failed = 0, []
    for path in targets:
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            failed.append(f'{path}: {e}')
    print(f'[完成] 已删除 {removed} 个标定文件' if not failed else
          f'[警告] 已删除 {removed} 个，失败 {len(failed)} 个：')
    for line in failed:
        print(f'       {line}')

    # 运行目录空了就一起收掉（非空说明还有别人的东西，别动）
    state_dir = os.path.join(here, CALIBRATION_DIR)
    try:
        if os.path.isdir(state_dir) and not os.listdir(state_dir):
            os.rmdir(state_dir)
    except OSError:
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description='恢复默认配置：管理员账号重置为 admin/admin，所有连接设置清空。'
    )
    parser.add_argument('-y', '--yes', action='store_true', help='跳过确认提示')
    parser.add_argument('--restart', action='store_true',
                        help=f'重置后执行 systemctl restart {SERVICE_NAME}')
    parser.add_argument('--purge-calibration', action='store_true',
                        help='同时删除标定库 calibration.db（含历史坐标，不可恢复）。'
                             '默认**不删**，这是站点资产而不是配置')
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
    if args.purge_calibration:
        print('  - 删除标定库 calibration.db 与 .calibration/（历史坐标，不可恢复）')
    else:
        print()
        print('  [保留] 标定记录（calibration.db）不会被删除——它是站点资产，')
        print('         不是配置。确实要清掉请加 --purge-calibration。')
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

    # 标定库：默认绝不动。显式加了 --purge-calibration 才会到这里，
    # 而且还要再过一道"输入 DELETE"的确认（`--yes` 视为已确认，便于脚本化）。
    if args.purge_calibration:
        if purge_calibration(here, assume_yes=args.yes) != 0:
            print()
            print('[信息] 配置已重置，标定数据保持原样。')
    else:
        print()
        print('[信息] 标定记录（calibration.db）未改动。')

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
