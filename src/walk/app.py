"""把遥控所需的各部件装配成一个对象，并管好它的生命周期。

walk_web.py 只负责解析命令行参数，剩下的装配都在这里：

    app = WalkApp()                     # 加载 SDK、连上运控板
    app.use_camera()                    # 可选：订阅画面
    app.start_server("0.0.0.0", 8080)   # 起 HTTP 服务
    app.run()                           # 阻塞：跑控制循环
    app.close()                         # 收尾：补发零速度、停服务、关相机

异常约定（交给调用方打印）：
    ImportError / RuntimeError  SDK 软连接断了、SDK 初始化失败
    OSError                    端口被占用
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, wait
from types import ModuleType
from typing import Any

import highcontrol_py

from .camera import Camera
from .depth import DepthVision
from .server import HttpServer, serve as serve_http
from .remote import Remote

LOOP_DT = 0.01          # 控制周期；publish_cmd 调用间隔须 > 2ms，10ms 留足余量
ZERO_FRAMES = 20        # 退出前补发的零速度帧数
SERVER_BOOT_TIMEOUT = 2.0   # 等 HTTP 启动结果的最长时间；超时就先跑控制循环
SERVER_CLOSE_TIMEOUT = 1.0  # 退出时等后台启动线程收尾的上限；绝不拖住补零速度


class WalkApp:
    """网页遥控应用：DDS 控制通道 + 共享状态 + 相机 + 深度 + HTTP 服务。"""

    def __init__(self) -> None:
        self.controller = highcontrol_py.HighController.instance()
        if not self.controller.init():
            raise RuntimeError("HighController 初始化失败（多半是别的控制进程还占着通道）")

        self.commands = command_table(highcontrol_py)
        self.remote = Remote(set(self.commands))
        self.controller.subscribe_robot_hardware_status(
            lambda status: self.remote.set_workmode(read_workmode(status)))
        print("HighController 已就绪，等待机器人状态 ...", flush=True)
        self.camera: Camera | None = None
        self.vision: DepthVision | None = None
        self.httpd: HttpServer | None = None
        self._server_boot: Future[HttpServer] | None = None

    # ---------------- 可选部件 ----------------

    def use_camera(self, direct: bool = False, with_depth: bool = False) -> None:
        """起相机预览；DDS 拿不到画面会自动回落直连，再失败也只是网页显示原因。

        `with_depth=True` 时直连 pipeline 会同时开深度流（供 --depth 分析深度）。
        """
        camera = Camera(direct=direct, with_depth=with_depth)
        if camera.start():
            desc = "D435I 直连" if camera.source == "direct" else "DDS 视频流"
            print(f"相机已启动：{camera.width}x{camera.height}（{desc}）", flush=True)
        else:
            print(f"相机不可用：{camera.error}（网页仍可遥控，只是没有画面）", flush=True)
        self.camera = camera

    def use_depth(self) -> None:
        """开 D435I 深度流测距；拿不到也继续，只是网页显示不可用原因。

        视觉分析是纯旁路：结果只进 /api/status，绝不参与控制循环或看门狗。
        相机直连且开了深度流时，彩色和深度来自同一条 pipeline：相机线程只把
        最新深度帧投给 `ingest()`（微秒级），测距/着色在 depth 自己的线程里做，
        不会拖慢彩色取帧；两条 MJPEG 也各走各的节奏（彩色 25fps、深度 5fps）。
        厂商服务运行时深度会被饿成 ~0.5fps，要深度先 `just cam-off`
        （= `sudo systemctl stop noetix-video-capture`）。
        """
        synced = (self.camera is not None and self.camera.source == "direct"
                  and self.camera.with_depth)
        if synced:
            vision = DepthVision(external=True)
            if vision.start():
                self.camera.set_depth_sink(vision.ingest)
                print("深度视觉已启动：彩色+深度同源采集（分析在独立线程，不拖慢画面）",
                      flush=True)
            else:
                print(f"深度视觉不可用：{vision.error}（遥控照常可用）", flush=True)
            self.vision = vision
            return
        print("[info] --depth 直连 D435I 深度流；若画面 fps 只有 ~0.5，"
              "先停厂商视频服务（sudo systemctl stop noetix-video-capture）", flush=True)
        vision = DepthVision()
        if vision.start():
            print("深度视觉已启动：左/中/右分区测距（仅网页显示）", flush=True)
        else:
            print(f"深度视觉不可用：{vision.error}（遥控照常可用）", flush=True)
        self.vision = vision

    def start_server(self, host: str, port: int, iface: str | None = None) -> None:
        """起 HTTP 服务；启动过程卡住也不能挡住控制循环。

        `serve()` 已经把 `serve_forever()` 放进守护线程，但建服之前的
        `local_urls()` / `make_server()` 绑定 / 打印地址仍是同步的。
        后台线程只负责把结果填进一个 `Future`，主线程最多等 SERVER_BOOT_TIMEOUT：
        - 已完成 → `result()` 拿到服务，或把原始异常原样抛回 main；
        - 超时 → 打警告并返回，让 `run()` 立刻开始发指令（看门狗只有控制循环
          在跑才生效），迟到结果交给 `add_done_callback` 补日志 / 补 httpd。
        """
        boot: Future[HttpServer] = Future()

        def work() -> None:
            try:
                httpd = serve_http(self.remote, self.camera, host, port, iface,
                                   vision=self.vision)
            except Exception as exc:
                boot.set_exception(exc)
                return
            self.httpd = httpd              # 超时后没人接 result，也要落地给 close()
            boot.set_result(httpd)

        def on_late(future: Future[HttpServer]) -> None:
            err = future.exception()        # 回调时 Future 已完成，不会阻塞
            if err is not None:
                print(f"[warn] HTTP 服务最终启动失败：{err}", flush=True)

        self._server_boot = boot
        threading.Thread(target=work, daemon=True, name="http-boot").start()
        done, _ = wait([boot], timeout=SERVER_BOOT_TIMEOUT)
        if done:
            boot.result()                   # 快速失败原样抛回 main；httpd 已由 work 赋值
            return
        # Future 保证：已完成则立即回调，未完成则完成时回调，
        # 不用自己写 Event / Lock / 标志位去防竞态。
        boot.add_done_callback(on_late)
        print(f"[warn] HTTP 服务 {SERVER_BOOT_TIMEOUT:g}s 内没启动完，"
              "控制循环先跑起来；稍后检查端口占用与启动日志", flush=True)

    # ---------------- 运行 ----------------

    def run(self) -> None:
        """按固定周期把网页状态翻译成 publish_cmd，直到 remote.stop 被置位。"""
        default = self.commands.get("DEFAULT")
        walk = self.commands.get("WALK")
        while not self.remote.stop.is_set():
            command = self.remote.take()
            if command.walking and walk is not None:
                action = walk                   # 行走要持续发，才能稳定停在 workmode=2
            elif command.action is not None:
                action = self.commands.get(command.action, default)
            else:
                action = default
            self.controller.publish_cmd(command.x, 0.0, command.yaw, action, 0)
            time.sleep(LOOP_DT)

    def close(self) -> None:
        """停车收尾：先补零速度（安全关键），再停 HTTP、关相机和深度流。"""
        self.remote.stop.set()
        default = self.commands.get("DEFAULT")
        for _ in range(ZERO_FRAMES):            # 避免最后一条速度残留在运控板
            self.controller.publish_cmd(0.0, 0.0, 0.0, default, 0)
            time.sleep(LOOP_DT)

        # 启动线程可能还没结束：等一小会儿让它把 httpd 落地，有上限，不拖住退出
        if self._server_boot is not None:
            done, _ = wait([self._server_boot], timeout=SERVER_CLOSE_TIMEOUT)
            if not done:
                print("[warn] HTTP 启动线程仍未结束，进程退出时会强制关掉它", flush=True)
        if self.httpd is not None:
            self.httpd.shutdown()
        if self.camera is not None:
            self.camera.close()
        if self.vision is not None:
            self.vision.close()
        print("已停车，退出。", flush=True)


def command_table(module: ModuleType) -> dict[str, Any]:
    """从 SDK 反射命令表：ControlCmd 里的大写属性即命令。"""
    control_cmd = module.ControlCmd
    return {name: getattr(control_cmd, name) for name in dir(control_cmd) if name.isupper()}


def read_workmode(status: Any) -> int | None:
    """从硬件状态里取 workmode；取不到返回 None。"""
    try:
        return int(status.workmode)     # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None
