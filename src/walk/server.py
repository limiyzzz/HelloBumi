"""给网页用的 HTTP 服务（Flask + Werkzeug）：页面 + 静态资源 + 四个接口。

    GET  /                 遥控页面（src/web/index.html）
    GET  /static/<name>    页面依赖（app.css / app.js / vendor/*.js，白名单后缀，防目录穿越）
    GET  /api/status       当前状态（workmode / walking / 速度 / 相机 / 前方距离）
    GET  /api/camera.mjpg  相机画面（MJPEG 长连接，生成器逐帧推）
    GET  /api/depth.mjpg   伪彩色深度图（MJPEG 长连接，仅 --depth 时可用）
    POST /api/cmd          命令：move / stop / action / estop / quit

选 Flask / WSGI 而不是 FastAPI / ASGI：控制循环是同步线程，Werkzeug 的 threaded
服务器与它同一个线程模型，`server.shutdown()` 可以直接塞进 app.close()；
MJPEG 用同步生成器即可，不需要 asyncio 事件循环。依赖注入走应用工厂闭包。
`local_urls()` 也留在本模块：`serve()` 起完服务自己打印可访问地址，app.py 不用碰网卡。

没有鉴权，只在可信局域网里用；要收紧就 `--bind 127.0.0.1` 配合 SSH 端口转发。
"""
from __future__ import annotations

import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

import psutil
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler, make_server

from .camera import Camera
from .depth import DepthVision
from .remote import Remote

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
INDEX = WEB_DIR / "index.html"
STATIC_TYPES = {        # 只放行页面真正用到的静态资源后缀
    ".css": "text/css",
    ".js": "text/javascript",
}
VENDOR_DIR = "vendor"   # /static/vendor/<file> 只允许这一级子目录
STREAM_FPS = 25         # 画面推送上限；相机直连约 30fps，推 25 更稳、手机解码也不吃力
DEPTH_STREAM_FPS = 5    # 深度图推送频率，与 depth.py 的 HZ 一致
VISION_STALE = 2.0      # 深度结果超过这个秒数就判为过期，网页不再显示旧距离
# 每次启动一个 ID：网页发现它变了就自动 reload，丢掉旧的 MJPEG 连接 / JS 状态。
# 服务端重启后旧页面若继续跑，断流 error + 重连会在浏览器里不断累积，越看越卡。
INSTANCE_ID = uuid.uuid4().hex

# 网页命令 → Remote 方法（模块级常量，不挂在类属性上）
COMMANDS: dict[str, Callable[[Remote, dict[str, Any]], tuple[bool, str]]] = {
    "move": lambda remote, msg: remote.move(msg.get("x", 0.0), msg.get("yaw", 0.0)),
    "stop": lambda remote, msg: remote.halt(),
    "action": lambda remote, msg: remote.act(str(msg.get("name", "DEFAULT"))),
    "estop": lambda remote, msg: remote.estop(),
    "quit": lambda remote, msg: remote.quit(),
}


class QuietHandler(WSGIRequestHandler):
    """静音每请求日志（原来手写 Handler 时也是静音的）。"""

    def log(self, *args: Any, **kwargs: Any) -> None:
        return


class HttpServer:
    """把 WSGI server + 后台线程包起来，只给 app.close() 暴露 shutdown()。

    这样 WalkApp 不需要知道底层是 Werkzeug 还是别的 WSGI 实现。
    """

    def __init__(self, server: ThreadedWSGIServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def serve(remote: Remote, camera: Camera | None, host: str, port: int,
          iface: str | None = None, vision: DepthVision | None = None) -> HttpServer:
    """起 HTTP 服务并打印可访问地址；缺网页、网卡名无效或端口占用时抛异常。

    地址打印放在这里（而不是 app.py）：服务自己知道端口和网卡，调用方不用碰
    local_urls / --iface，只传进来就行。`vision` 是可选的旁路数据源。
    """
    if not INDEX.is_file():
        raise FileNotFoundError(f"找不到网页文件：{INDEX}（web/ 要和 walk_web.py 放在一起）")
    urls = local_urls(port, iface)      # 先算地址：网卡名写错时还没占端口
    server = make_server(host, port, create_app(remote, camera, vision),
                         threaded=True, request_handler=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for url, name in urls:
        print(f"  浏览器打开：{url}    ({name})", flush=True)
    return HttpServer(server, thread)


# ------------------------------------------------------------------ 应用工厂
def create_app(remote: Remote, camera: Camera | None,
               vision: DepthVision | None = None) -> Flask:
    """依赖通过闭包注入，避免全局变量 / 类属性打补丁；也方便单独起服务做测试。"""
    app = Flask(__name__, static_folder=None)   # 关掉 Flask 自带的 /static，用下面白名单版本
    app.json.ensure_ascii = False

    @app.after_request
    def no_store(resp: Response) -> Response:
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(404)
    def not_found(err: Any) -> tuple[Response, int]:
        return jsonify(ok=False, msg="not found"), 404

    @app.get("/")
    @app.get("/index.html")
    def index() -> Response:
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static(name: str) -> Response:
        target = _static_target(name)
        if target is None:
            return jsonify(ok=False, msg="not found"), 404
        base, filename, ctype = target
        return send_from_directory(base, filename, mimetype=ctype)

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status=204)

    @app.get("/api/status")
    def status() -> Response:
        return jsonify({"ok": True, "instance": INSTANCE_ID,
                        **remote.status(),
                        "camera": _camera_info(camera),
                        "vision": _vision_info(vision)})

    @app.post("/api/cmd")
    def cmd() -> tuple[Response, int]:
        message = request.get_json(silent=True)
        if not isinstance(message, dict):
            return jsonify(ok=False, msg="请求不是有效的 JSON 对象"), 400
        kind = str(message.get("type", ""))
        command = COMMANDS.get(kind)
        if command is None:
            return jsonify(ok=False, msg=f"未知命令：{kind}"), 400

        ok, text = command(remote, message)
        if kind in ("action", "estop", "quit"):
            print(f"[web] {kind} {message.get('name', '')} -> {text}", flush=True)
        return jsonify(ok=ok, msg=text), (200 if ok else 409)

    @app.get("/api/camera.mjpg")
    def camera_stream() -> Response:
        cam = camera
        if cam is None or not cam.ready:
            reason = cam.error if cam is not None else "相机未启用"
            return jsonify(ok=False, msg=reason or "相机不可用"), 503

        def part(frame: bytes) -> bytes:
            return (b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode("ascii")
                    + b"\r\n\r\n" + frame + b"\r\n")

        def frames() -> Iterator[bytes]:
            # 浏览器关页面 → werkzeug 写失败 → 生成器被关闭，线程自然结束
            last: bytes | None = None
            while not cam.stop.is_set():
                frame = cam.latest()
                # 换帧就推：不再等深度帧的序号，彩色走自己的节奏
                if frame is not None and frame is not last:
                    last = frame
                    yield part(frame)
                time.sleep(1 / STREAM_FPS)

        return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.get("/api/depth.mjpg")
    def depth_stream() -> Response:
        if vision is None or vision.depth_jpeg() is None:
            reason = ("深度图未启用（加 --depth 开启）" if vision is None
                      else "深度图暂不可用")
            return jsonify(ok=False, msg=reason), 503

        def part(frame: bytes) -> bytes:
            return (b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode("ascii")
                    + b"\r\n\r\n" + frame + b"\r\n")

        def frames() -> Iterator[bytes]:
            # 浏览器关页面 → werkzeug 写失败 → 生成器被关闭，线程自然结束
            last: bytes | None = None
            while not vision.stop.is_set():
                frame = vision.depth_jpeg()
                if frame is not None and frame is not last:   # 5Hz 才换图，不重复推
                    last = frame
                    yield part(frame)
                time.sleep(1 / DEPTH_STREAM_FPS)

        return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

    return app


def _static_target(name: str) -> tuple[str, str, str] | None:
    """解析静态路径：只放行 web/ 下的一级文件与 web/vendor/ 下的一级 .js。"""
    parts = name.split("/")
    if len(parts) == 1:
        base, filename = WEB_DIR, parts[0]
    elif len(parts) == 2 and parts[0] == VENDOR_DIR:
        base, filename = WEB_DIR / VENDOR_DIR, parts[1]
    else:
        return None

    ctype = STATIC_TYPES.get(Path(filename).suffix)
    if (ctype is None or not filename or filename.startswith(".")
            or "\\" in filename or "/" in filename):
        return None
    target = (base / filename).resolve()
    if base.resolve() not in target.parents or not target.is_file():
        return None
    return str(base), filename, ctype


def _camera_info(camera: Camera | None) -> dict[str, Any]:
    if camera is not None:
        return camera.info()
    return {"ready": False, "error": "相机未启用（--no-camera）", "fps": 0.0,
            "source": "off", "note": "", "stale": False}


def _vision_info(vision: DepthVision | None) -> dict[str, Any]:
    """分区距离；超过 VISION_STALE 秒没更新就判过期，网页只显示原因不显示旧值。"""
    if vision is None:
        return {"ok": False, "left": None, "center": None, "right": None,
                "nearest": None, "updated_at": None,
                "msg": "前方测距未启用（加 --depth 开启；需先 just cam-off 停厂商服务）"}
    data = vision.snapshot()
    ts = data.get("updated_at")
    if ts is not None and time.time() - ts > VISION_STALE:
        data["ok"] = False
        data["msg"] = "深度数据已过期"
    return data


# ------------------------------------------------------------------ 网卡地址
def local_urls(port: int, only: str | None = None) -> list[tuple[str, str]]:
    """列出本机可被访问的地址，返回 [(URL, 网卡名)]。

    `psutil.net_if_addrs()` 直接给出「网卡名 → 地址列表」，不用再碰 Linux ioctl。
    显式枚举每块网卡而不是只取默认路由——机器人上常有多张网卡
    （有线 / USB 无线 / 对运控板的专线），默认路由那张不一定是手机连的那张。
    用 `only` 可以只看指定网卡。
    """
    found: list[tuple[str, str]] = []
    for name, addrs in psutil.net_if_addrs().items():
        if only and name != only:
            continue
        for addr in addrs:
            if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                found.append((f"http://{addr.address}:{port}", name))
                break                       # 一块网卡只取第一个 IPv4，与原实现一致

    if only and not found:
        raise RuntimeError(f"网卡 {only} 没有 IPv4 地址（用 `ip -4 addr show {only}` 看一下）")
    return found or [(f"http://127.0.0.1:{port}", "lo")]
