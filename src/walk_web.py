"""Bumi 网页遥控：浏览器里按方向键控制机器人行走。

在机器人本机起一个 HTTP 服务，手机/平板/电脑连同一个局域网，浏览器打开就是遥控界面；
命令仍用官方 `HighController.publish_cmd` 经 DDS 下发给运控板。

    浏览器 --HTTP/JSON--> walk_web.py --publish_cmd(DDS)--> 运控板

用法（在机器人本机）：
    cd /home/noetix/projects/bumi-main
    just serve
    浏览器打开  http://<机器人IP>:8080

本文件只解析命令行参数，装配和生命周期都在 walk/app.py。
安全约定见 README「安全机制」一节。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import traceback

from walk.app import WalkApp


def install_signal_handlers() -> None:
    """让 kill / pkill 也走优雅退出（补发零速度），而不是硬杀。

    两个背景：后台启动时 SIGINT 会被继承成 SIG_IGN，所以 Ctrl+C 那套
    在这里无效；SIGTERM 默认直接终止进程，会跳过 finally 里的收尾。
    这里显式接管两个信号，统一转成 KeyboardInterrupt。
    """
    def handler(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass        # 非主线程等特殊情况，忽略


def main() -> int:
    """入口。异常一律在这里收住：解释器正常退出会在 SDK 析构阶段段错误，
    必须让 `__main__` 的 `os._exit(code)` 执行；任何失败路径都要 close() 补零速度。"""
    args = build_parser().parse_args()
    install_signal_handlers()

    app: WalkApp | None = None
    code = 0
    try:
        app = WalkApp()
        if not args.no_camera:
            app.use_camera()
        if args.depth:
            app.use_depth()
        app.start_server(args.bind, args.port, args.iface)
        app.run()
    except KeyboardInterrupt:
        print("\nCtrl+C，停车退出", flush=True)
    except Exception as exc:                # 包括启动失败、端口占用、运行期异常
        print(f"启动失败：{exc}", flush=True)
        if not isinstance(exc, (ImportError, FileNotFoundError, RuntimeError, OSError)):
            traceback.print_exc()
        code = 1
    finally:
        if app is not None:
            # 终端 Ctrl+C 会把 SIGINT 发给整个前台进程组：`pixi run` 是父进程，
            # 还会再给 python 子进程转发一次。第二个信号往往正好落在收尾阶段，
            # 会把 close() 里的补零速度 / 停 HTTP / 关相机打断（旧代码只吞掉异常，
            # 收尾就半途而废了）。收尾只有几百毫秒，期间忽略重复信号。
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, signal.SIG_IGN)
            print("\n正在停车收尾（忽略重复 Ctrl+C）...", flush=True)
            try:
                app.close()
            except KeyboardInterrupt:       # 兜底：信号在置 SIG_IGN 之前就到达
                print("\n收尾时收到中断信号，继续退出", flush=True)
            except Exception as exc:        # 收尾抛错也不能挡住 os._exit
                print(f"收尾失败：{exc}", flush=True)
    return code


def build_parser() -> argparse.ArgumentParser:#为网页提供特殊启动方式
    """命令行参数。"""
    parser = argparse.ArgumentParser(description="Bumi 网页遥控（在机器人上运行）")
    parser.add_argument("--bind", default="0.0.0.0", help="监听地址，默认全部网卡")
    parser.add_argument("--port", type=int, default=8080, help="网页端口")
    parser.add_argument("--no-camera", action="store_true", help="不启动相机预览")
    parser.add_argument("--depth", action="store_true",
                        help="显式开启 D435I 直连深度测距（实验性：会与厂商视频服务争用 USB，默认关闭）")
    parser.add_argument("--no-depth", action="store_true",
                        help="兼容旧命令：默认已关闭直连深度，无需再加此参数")
    # TODO: 咱们不需要iface
    parser.add_argument("--iface", default=None, help="启动提示里只显示这张网卡的地址，例如 wlx90de802fc5eb")
    return parser


if __name__ == "__main__":
    code = main()
    # 厂商 SDK 没有 shutdown 接口，解释器正常退出时会在析构阶段段错误，
    # 所以刷完缓冲直接 os._exit，保证退出码干净。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
