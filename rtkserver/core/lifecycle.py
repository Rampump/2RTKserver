# -*- coding: utf-8 -*-
"""生命周期：一次运行（run_once）与进程主循环（main）。

边界约定（改配置只重启数据面，Web 与全局资源不动）：
  - 出口是否启动，只看各自的 _enabled 独立开关（与本函数里的 mode 无关）；
  - 数据源是否启动，只看 mode；
  - 任何模块启动失败都只记日志，不阻断其他模块，也不中断整体运行。

退出语义（本次拆分新增，原代码缺失）：
  SIGTERM/SIGINT → shutdown_event → run_once 的 finally 停数据源与三个出口
                 → hooks['shutdown'] 让 mDNS 发 TTL=0 道别包 → 正常返回 0
  没有这一条时 systemd 直接砍进程，mDNS 记录会在局域网里残留 120 秒指向已停机设备。
"""
import logging
import signal
import sys
import threading
import time

import serial

from .. import paths
from ..utils.common import ThreadExitRequested, interruptible_sleep
from ..utils.logger import log_startup_banner, setup_logging
from ..utils.network import get_local_ip
from ..outlets import register_all
from ..outlets.caster_uploader import (mark_upload_state, start_caster_uploader,
                                       stop_caster_uploader)
from ..outlets.local_caster import start_local_caster, stop_local_caster
from ..outlets.radio_bridge import start_serial_bridge, stop_serial_bridge
from ..sources.relay_source import forward_relay, open_relay
from ..sources.rtcm_parser import process_rtcm_buffer
from ..sources.serial_source import detect_serial, upload_via_serial
from ..validation import validate_config
from . import state
from .config import get_config, init_db, migrate_admin_password
from .constants import ART_LOGO, WEB_HOST, WEB_PORT
from .status import describe_targets


def install_signal_handlers():
    """把 SIGTERM/SIGINT 翻译成 shutdown_event，让 finally 有机会跑完。

    systemd 停服默认发 SIGTERM；Python 不注册 handler 时进程会被直接终止，
    run_once 的 finally 与 mDNS 道别包全都执行不到。
    """
    def _handler(signum, _frame):
        if not state.shutdown_event.is_set():
            logging.info(f"收到退出信号 {signum}，开始按逆序停止（最多等待 5 秒）")
        state.shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # 非主线程或平台不支持时忽略：不影响主流程
            pass


def _run_startup_hooks():
    for fn in list(state.hooks['startup']):
        try:
            fn()
        except Exception as e:
            # 旁路模块（mDNS）失败绝不影响主程序启动
            logging.warning(f"启动钩子执行失败（已忽略）：{e}")


def _run_shutdown_hooks():
    for fn in list(state.hooks['shutdown']):
        try:
            fn()
        except Exception as e:
            logging.warning(f"退出钩子执行失败（已忽略）：{e}")


# ==================== 局域网固定地址（mDNS）挂载点 ====================
# 只在 main() 里挂一次。所有异常都被吞掉：mDNS 是旁路能力，
# 它坏掉绝不能让 Web 配置页和数据转发跟着坏。
def _register_discovery_hooks():
    from ..version import __version__ as APP_VERSION

    def startup():
        from ..discovery.zeroconf_announcer import ZeroconfAnnouncer
        ann = ZeroconfAnnouncer(web_port=WEB_PORT, on_address_change=_on_mdns_address_change,
                                version=APP_VERSION)
        if ann.start():
            state.announcer = ann

    def shutdown():
        ann = state.announcer
        state.announcer = None
        if ann is not None:
            ann.stop()

    state.hooks['startup'].append(startup)
    state.hooks['shutdown'].append(shutdown)


def _on_mdns_address_change(addresses):
    """IP 变了：立刻把新 IP 推给已打开的页面，安卓用户不用刷新也知道该用哪个地址。"""
    try:
        from .status import push_run_status
        push_run_status()
    except Exception:
        pass


def _sync_mdns_ntrip_port():
    """把本地 Caster 的实际端口同步到 mDNS（只用于展示，失败不影响取流）。"""
    ann = state.announcer
    if ann is None:
        return
    try:
        if state.local_caster is not None:
            from .constants import LOCAL_CASTER_MOUNTPOINT
            ann.set_ntrip(state.local_caster.port, LOCAL_CASTER_MOUNTPOINT)
        else:
            ann.set_ntrip(None)
    except Exception as e:
        logging.debug(f"同步 mDNS 的 NTRIP 端口失败（忽略）：{e}")


def run_once():
    """一轮完整运行：先启动数据出口，再启动数据源；停机严格逆序。"""
    # 上一轮的工作线程若仍未退出（例如卡在阻塞的 recv 里），
    # 绝不能再起一个新线程：否则两个线程会同时向同一个挂载点推流、抢同一个串口。
    if state.worker_thread is not None and state.worker_thread.is_alive():
        logging.error("上一轮工作线程尚未退出，为避免重复推流暂缓启动新实例")
        state.thread_exit_event.set()
        time.sleep(1)
        return

    # 退出信号只在新一轮开始时清除（原先是放在 finally 里，会把还没退出的老线程"复活"）
    state.thread_exit_event.clear()
    # 换数据源后，上一轮的消息类型不能带进来，否则源列表会描述已不存在的数据
    state.msg_types_seen.clear()
    state.msg_types_recent.clear()

    ser = None
    relay = None
    source_thread = None

    try:
        cfg = get_config()
        if not validate_config(cfg):
            logging.error("监测到配置信息无效，将于5秒后重新读取")
            time.sleep(5)
            return

        mode = cfg['mode']

        # ==================== 阶段 2：启动数据出口（先起，等着接数据）====================
        # 顺序：本地 Caster（开始 listen）→ 数传（开串口待命）→ 上传（建连 + 握手）
        # 三者各自独立线程；任何一个启动失败只记日志，绝不影响另外两个，也不影响数据源。

        if str(cfg.get('local_caster_enabled', '0')) == '1':
            start_local_caster(cfg)
            _sync_mdns_ntrip_port()
        else:
            stop_local_caster()
            _sync_mdns_ntrip_port()

        if str(cfg.get('radio_enabled', '0')) == '1':
            start_serial_bridge(cfg)
        else:
            stop_serial_bridge()

        # 上传出口是独立线程 + 队列：建连失败会在后台一直重试，
        # 绝不会把数据源堵在这里（这正是本次改造要根治的老问题）。
        if str(cfg.get('caster_enabled', '0')) == '1':
            start_caster_uploader(cfg)
        else:
            stop_caster_uploader()
            mark_upload_state('idle', cfg, error='')

        logging.info(f"数据出口就绪：{describe_targets(cfg)}")

        # ==================== 阶段 3：启动数据源（后起，一起来就有数据）====================
        if mode == 'serial':
            logging.info("进入串口模式")
            port, baud = detect_serial(cfg)
            if not (port and baud):
                logging.warning("未检测到有效串口，5秒后重试")
                time.sleep(5)
                return
            cfg['serial_port'], cfg['baud_rate'] = port, baud
            try:
                ser = serial.Serial(port=port, baudrate=baud, timeout=1)
            except Exception as e:
                logging.error(f"打开串口失败: {e}")
                time.sleep(5)
                return

            source_thread = threading.Thread(
                target=upload_via_serial,
                args=(ser, cfg, state.thread_exit_event),
                name='source-serial',
                daemon=True
            )
            source_thread.start()
            state.worker_thread = source_thread

        elif mode == 'relay':
            logging.info("进入中继模式")
            try:
                relay = open_relay(cfg)
            except Exception as e:
                logging.error(f"打开中继连接失败: {e}")
                time.sleep(5)
                return

            source_thread = threading.Thread(
                target=forward_relay,
                args=(relay, cfg, state.thread_exit_event),
                name='source-relay',
                daemon=True
            )
            source_thread.start()
            state.worker_thread = source_thread

        else:
            logging.error(f"未知模式: {mode}")
            time.sleep(5)
            return

        # ==================== 阶段 4：等待重启 / 退出信号 ====================
        while not state.restart_event.is_set() and not state.shutdown_event.is_set():
            time.sleep(1)

    except ThreadExitRequested:
        logging.info("检测到重启/退出信号，提前结束本轮运行")
    except Exception as e:
        logging.exception(f"运行主循环run_once时发生未捕获异常: {e}")
        time.sleep(5)

    finally:
        # ==================== 停机：严格按启动的逆序 ====================
        # 1) 先停数据源：不再产生新数据，避免"源还在推、出口已关"的报错
        state.thread_exit_event.set()
        if source_thread is not None and source_thread.is_alive():
            source_thread.join(5)
        if source_thread is None or not source_thread.is_alive():
            state.worker_thread = None
        else:
            # 关键：不再无条件清除退出信号。老线程会在下一次循环检查时看到信号并自行退出；
            # 只要它还活着，worker_thread 就保留，下一轮 run_once 会被守卫拦住。
            logging.warning("数据源线程未在超时时间内退出，将继续等待其自行结束")

        # 2) 再停出口：上传 → 数传 → 本地 Caster
        #    本地 Caster 最后停，让内网客户端尽量晚一刻断流。
        stop_caster_uploader()
        stop_serial_bridge()
        stop_local_caster()
        _sync_mdns_ntrip_port()

        # 3) 关闭数据源自己持有的句柄
        if ser is not None:
            try:
                ser.close()
            except Exception as e:
                logging.error(f"关闭串口时出错: {e}")
        if relay is not None:
            try:
                relay.close()
            except Exception as e:
                logging.error(f"关闭中继连接时出错: {e}")

        if state.restart_event.is_set():
            logging.info("接收到重启指令，等待所有资源关闭后重新加载配置")
            state.restart_event.clear()


def _start_web_thread():
    from ..web.app import create_app
    app = create_app()

    # 注意：这里不能换成 waitress/gunicorn 普通的 HTTP worker。
    # flask-sock 需要服务器支持 WebSocket 协议升级，waitress 不支持，
    # 换上去会直接让监控面板的 /ws 无法连接。
    # 要上生产级 WSGI，需要 gunicorn + gevent worker，故此处保持内置服务器并显式开启多线程。
    web_thread = threading.Thread(
        target=lambda: app.run(host=WEB_HOST, port=WEB_PORT, debug=False,
                               use_reloader=False, threaded=True),
        name='web',
        daemon=True
    )
    web_thread.start()
    return web_thread


def main():
    setup_logging()
    print("2RTK Server 启动")
    print(ART_LOGO)

    logging.info(f"程序目录: {paths.BASE_DIR}")

    install_signal_handlers()
    register_all()
    _register_discovery_hooks()

    init_db()
    # 把历史遗留的明文管理员口令升级为哈希
    migrate_admin_password()

    # 启动辅助线程（保持不变）
    rtcm_processor = threading.Thread(target=process_rtcm_buffer, name='rtcm-parser', daemon=True)
    rtcm_processor.start()
    logging.info("RTCM数据处理线程已启动")

    _start_web_thread()

    host_ip = get_local_ip()
    log_startup_banner(host_ip, WEB_PORT)

    # 旁路模块（mDNS 固定地址）：Web 起来之后再挂，保证网页已经可用
    _run_startup_hooks()

    # 重构：前置配置检查，仅在配置有效时执行主流程
    while not state.shutdown_event.is_set():
        # 1. 先检查配置是否有效
        cfg = get_config()
        if not validate_config(cfg):
            # 配置还没配好就安静等待：网页、登录、改密这些功能都照常可用，
            # 只是不往下启动任何数据模块。validate_config 内部已按 60 秒节流
            # 打出具体缺哪一项，这里不重复记日志。
            if (cfg.get('mode') or 'none') == 'none':
                # 让状态页能明确显示"等待配置"，而不是一片空白
                state.run_status['mode'] = '等待配置'
                state.run_status['data_source'] = '尚未选择数据源（串口 / 中继）'
                state.run_status['target_caster'] = '尚未选择数据出口'
                # 首次使用也必须能看到本机 IP（安卓只能靠它访问）
                try:
                    from .status import push_run_status
                    push_run_status()
                except Exception:
                    pass
            # 用可中断 sleep：收到 SIGTERM 时最多 0.2 秒就退出，而不是干等 10 秒
            interruptible_sleep(10, state.shutdown_event)
            continue  # 跳过后续逻辑，直接进入下一次循环

        # 2. 配置有效，执行主流程（run_once不变）
        logging.info("配置信息有效，启动主程序...")
        run_once()

        # 3. 处理重指令（保持不变）
        if state.restart_event.is_set():
            logging.info("接收到重启指令，重新检查配置...")
            state.restart_event.clear()
            continue

        # 4. 主流程正常结束后短暂等待（保持不变）
        time.sleep(1)

    # ==================== 进程退出 ====================
    _run_shutdown_hooks()
    logging.info("2RTK Server 已退出")
    return 0


if __name__ == '__main__':
    sys.exit(main())
