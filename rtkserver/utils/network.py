# -*- coding: utf-8 -*-
"""网络与端口工具：本机 IP、端口占用诊断、自环判断。

端口占用诊断里有个非常常见的误判，先写在代码里备查：
  「中继 / 上传端填的 2101」是**对端端口**。本机出站时由内核分配一个临时端口
  （比如 54321），本地根本不会 bind 2101。所以「远端 Caster 用 2101」和
  「本地 Caster 监听 2101」并不冲突，两者可以同时存在。

  真会报 EADDRINUSE(errno 98) 的只有三种：
    1) 上一个进程没退干净，还占着 2101（systemd 重启、网页保存重启时最常见）
    2) 本机别的程序占了 2101（str2str / 另一个实例 / 其它 NTRIP 软件）
    3) 上传目标写成了本机地址 + 同一个端口（自环，见 check_self_loop）
"""
import logging
import os
import socket
import sys

from ..core.constants import LOCAL_CASTER_HOST, LOCAL_CASTER_PORT

logger = logging.getLogger(__name__)


def get_local_ip():
    """取本机对外通信用的 IPv4 地址（UDP connect 不产生实际流量）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def local_ipv4_addresses():
    """本机全部非回环 IPv4 地址（按"最可能是主网卡"的顺序返回）。

    mDNS 的 A 记录要一次带上所有地址：设备可能同时连有线和 Wi-Fi，
    只报一个的话，接入另一张网卡的客户端解析到的是不可达地址。
    顺序规则：默认路由地址优先，其余按 getaddrinfo 顺序。
    """
    primary = None
    try:
        primary = get_local_ip()
    except Exception:
        primary = None

    found = []
    for ip in (primary,):
        if ip and ip != 'localhost' and not ip.startswith('127.') and ip not in found:
            found.append(ip)

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith('127.') and ip not in found:
                found.append(ip)
    except Exception:
        pass

    # 上面两步可能都失败（例如离线设备），最后兜底枚举一遍本机网卡地址
    if not found:
        try:
            for info in socket.getaddrinfo(None, 0, socket.AF_INET, socket.SOCK_DGRAM):
                ip = info[4][0]
                if ip and not ip.startswith('127.') and ip not in found:
                    found.append(ip)
        except Exception:
            pass

    return found


def find_port_owner(port):
    """尽力找出监听指定 TCP 端口的进程，返回 'pid 1234 (str2str)' 或 None。

    只在 Linux 有效（读 /proc/net/tcp{,6} 取 socket inode，再反查 /proc/*/fd
    找到持有它的进程）。只在启动失败、保存校验这类低频路径调用。
    """
    if not sys.platform.startswith('linux'):
        return None
    inodes = set()
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(path, 'r') as f:
                next(f, None)  # 跳过表头
                for line in f:
                    fields = line.split()
                    # 表结构：sl local_address rem_address st ... 第 10 列为 inode
                    # local_address 形如 00000000:08A1（08A1 就是端口）；st == '0A' 表示 LISTEN
                    if len(fields) < 10 or fields[3] != '0A':
                        continue
                    try:
                        if int(fields[1].rsplit(':', 1)[1], 16) != int(port):
                            continue
                    except (IndexError, ValueError):
                        continue
                    inodes.add('socket:[%s]' % fields[9])
        except OSError:
            continue
    if not inodes:
        return None
    try:
        pids = os.listdir('/proc')
    except OSError:
        return None
    for pid in pids:
        if not pid.isdigit():
            continue
        fd_dir = '/proc/%s/fd' % pid
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # 权限不足或进程已退出，跳过
        for fd in fds:
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if link in inodes:
                try:
                    with open('/proc/%s/comm' % pid, 'r') as f:
                        name = f.read().strip()
                except OSError:
                    name = '?'
                return 'pid %s (%s)' % (pid, name)
    return None


def probe_tcp_port(host, port):
    """试一下 host:port 现在能不能监听。返回 (可用?, 说明)。

    不可用时说明里尽量带上占用者，例如「被 pid 1234 (str2str) 占用」。

    两个平台用的手段不同：
      Linux/POSIX —— 直接 bind。加 SO_REUSEADDR 是为了不把 TIME_WAIT 里的旧连接
                     误判成占用；真正有程序在 listen 时 bind 仍会 EADDRINUSE。
      Windows     —— bind 探活不可靠（SO_REUSEADDR 语义允许抢端口，bind 照样成功），
                     所以改成"连一下"：连得上就说明有程序在监听。
    """
    port = int(port)
    host = host or LOCAL_CASTER_HOST

    if os.name == 'nt':
        target = '127.0.0.1' if host in ('0.0.0.0', '::', '*') else host
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.3)
        try:
            probe.connect((target, port))
            return False, '被本机某个程序监听（端口已被占用）'
        except OSError:
            return True, ''
        finally:
            try:
                probe.close()
            except OSError:
                pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True, ''
    except OSError as e:
        owner = find_port_owner(port)
        return False, ('被 %s 占用' % owner) if owner else str(e)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def local_addresses():
    """本机可能被写进配置的"自己"的地址集合，用于自环判断。"""
    ips = {'127.0.0.1', 'localhost', '0.0.0.0', '::1'}
    try:
        ip = get_local_ip()
        if ip:
            ips.add(ip)
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        ips.add(hostname.lower())
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return ips


def check_self_loop(cfg):
    """检查「出口指向自己」这类必然跑不通的配置，返回一句人话或 None。

    上传：caster_host 是本机 + 端口等于本地 Caster 端口
          → SOURCE 请求会打到自己刚开的 Caster 上（它只认 GET），现象就是
            「连上了但一直 timeout」，非常像对端 Caster 有问题，实际是自环。
    中继：relay_host 是本机 + 端口等于本地 Caster 端口
          → 从自己身上拉流，原地打转。
    """
    if str(cfg.get('local_caster_enabled', '0')) != '1':
        return None
    try:
        lc_port = int(cfg.get('local_caster_port') or LOCAL_CASTER_PORT)
    except (TypeError, ValueError):
        return None
    ips = local_addresses()

    if str(cfg.get('caster_enabled', '0')) == '1':
        host = (cfg.get('caster_host') or '').strip().lower()
        try:
            cport = int(cfg.get('caster_port') or 0)
        except (TypeError, ValueError):
            cport = 0
        if host and host in ips and cport == lc_port:
            return (f"上传地址 {host}:{cport} 就是本机，且与本地 Caster 端口相同，"
                    f"数据会推给自己（表现为连上后一直超时）。"
                    f"请把上传地址改成远端 Caster，或把本地 Caster 端口改成别的值。")

    if cfg.get('mode') == 'relay':
        host = (cfg.get('relay_host') or '').strip().lower()
        try:
            rport = int(cfg.get('relay_port') or 0)
        except (TypeError, ValueError):
            rport = 0
        if host and host in ips and rport == lc_port:
            return (f"中继地址 {host}:{rport} 指向本机自己的 Caster，"
                    f"等于从自己身上拉流，收不到新数据。")
    return None


def port_conflict_message(cfg):
    """本地 Caster 监听端口与中继/上传端口重复时，返回一句提示，否则 None。

    为什么要求错开：中继/上传填的是**对端端口**，本机出站用内核临时端口，
    技术上可以和本地监听端口同为 2101 而不冲突。但同一个配置页里重复出现
    2101，极容易被误读成「本地 Caster 抢了出口的端口」，也会在对端地址被写成
    127.0.0.1 时直接变成自环。所以统一要求把本机监听端口错开。
    """
    if str(cfg.get('local_caster_enabled', '0')) != '1':
        return None
    try:
        lc = int(cfg.get('local_caster_port') or LOCAL_CASTER_PORT)
    except (TypeError, ValueError):
        return None

    same = []
    if str(cfg.get('caster_enabled', '0')) == '1':
        try:
            if int(cfg.get('caster_port') or 0) == lc:
                same.append('「上传到云端 Caster」')
        except (TypeError, ValueError):
            pass
    if cfg.get('mode') == 'relay':
        try:
            if int(cfg.get('relay_port') or 0) == lc:
                same.append('「中继取流」')
        except (TypeError, ValueError):
            pass

    if not same:
        return None
    return (f"本地 Caster 监听端口 {lc} 与 {' / '.join(same)} 的端口相同。"
            f"本地 Caster 会在本机真实占用 {lc}，请改成 2102 等未被占用的端口。")
