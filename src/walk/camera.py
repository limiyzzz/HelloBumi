"""相机预览：把画面压成 JPEG 给网页看，默认自己直连 D435I：

- `direct`（默认）：pyrealsense2 直连 D435I；`with_depth=True` 时彩色与深度
  并进同一条 pipeline，同一个 frameset 同时出彩色和深度，两个画面天然同帧。
  不依赖厂商服务，也不用 sudo；
- `dds`（`--dds-cam`）：订阅厂商服务 `noetix-video-capture.service` 发布的
  `subscribe_internal_video_desensed`（640x480 YUYV422，约 25fps）。

**直连要求先停厂商服务**：`noetix-video-capture` 通过 usbfs 独占设备，还在跑时
直连会报 `failed to set power state`，并且会把深度流饿成 ~0.5fps（见 depth.py）。
`just cam-off` 就是 `sudo systemctl stop noetix-video-capture`。
直连失败时（多半是服务没停）自动回落到 DDS，至少还有画面。

相机是**可选模块**：拿不到画面时只记录原因，行走遥控照常可用。

线程约定：
    直连接收 / DDS 回调 —— 只把最新一帧挂到槽位，绝不做解码（会拖慢整条流）
    解码 / 压缩线程    —— YUYV→BGR / 取帧 + JPEG 编码，来不及就丢帧
同步约定：
    每帧编码完成后把 (时间戳, JPEG) 存进环形缓存 `_ring`；DDS 模式下深度线程用
    `select_color(ts)` 取与深度帧同刻的彩色帧（配对缓存）。
    直连模式不需要配对——同一 frameset 里两者本来就同时刻。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

JPEG_QUALITY = 70       # JPEG 质量，越高越占带宽
START_TIMEOUT = 8.0     # 等第一帧的秒数，超时就判定相机不可用
CAMERA_STALE = 2.0      # 超过这个秒数没有新帧就判定画面停滞（DDS 流断了）
RING_LEN = 48           # 配对用环形缓存帧数：25fps 下约 2 秒，深度帧总能在里面找到同刻画面


class Camera:
    """视频流 → JPEG 字节；source 为 "dds"（厂商服务）或 "direct"（直连 D435I）。"""

    def __init__(self, quality: int = JPEG_QUALITY, direct: bool = False,
                 with_depth: bool = False) -> None:
        self.quality = quality
        self.source = "dds"
        self.note = "画面来自 noetix-video-capture 的 DDS 视频流"
        self.error: str | None = None
        self.width = 0
        self.height = 0
        self.fps = 0.0
        self.stop = threading.Event()
        self._direct = direct                               # 强制直连（跳过 DDS）
        self.with_depth = with_depth                        # 直连 pipeline 是否同时开深度流
        self._depth_sink: Callable[[Any, float, bytes | None], None] | None = None
        self._depth_scale = 0.001                           # z16 → 米；start() 读真实值
        self._last_depth_no = -1                            # 去重：深度帧号没变就不重复分析
        self._pipeline: Any = None                          # 直连模式的 pyrealsense2 pipeline
        self._lock = threading.Lock()
        self._frame: bytes | None = None                    # 最新一帧 JPEG
        self._frame_at = 0.0                                # 最近成功解码时刻（判停滞）
        self._ring: deque[tuple[float, bytes]] = deque(maxlen=RING_LEN)  # 配对缓存
        self._pending: tuple[int, int, Any] | None = None   # 待解码的原始帧
        self._started = threading.Event()

    def set_depth_sink(self, sink: Callable[[Any, float, bytes | None], None]) -> None:
        """注册深度数据出口：直连同源模式下，每个含新深度帧的 frameset 都会调它，
        参数是（深度米制数组, 采集时刻, 同帧彩色 JPEG）。"""
        self._depth_sink = sink

    @property
    def ready(self) -> bool:
        """是否有画面可播。"""
        return self._frame is not None and self.error is None

    def start(self, wait: float = START_TIMEOUT) -> bool:
        """起视频流；失败只记原因、返回 False。

        默认直连 D435I；直连失败（多半是厂商服务还占着设备）才回落 DDS。
        `direct=False`（`--dds-cam`）只走 DDS，完全不碰设备。
        """
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            self.error = f"缺少依赖 {exc}"
            return False
        self._cv2, self._np = cv2, np

        if not self._direct:
            return self._start_dds(wait)
        if self._start_direct(wait):
            return True
        print(f"[warn] 直连 D435I 失败：{self.error}；改为订阅厂商 DDS 视频流"
              "（官方服务没停就 just cam-off 再重启）", flush=True)
        self.error = None
        return self._start_dds(wait)

    def _start_dds(self, wait: float) -> bool:
        """订阅厂商服务的 DDS 视频流；失败时 error 记原因。"""
        try:
            import mediacontrol_py
            controller = mediacontrol_py.MediaController.instance()
            if not controller.init():
                self.error = "DDS 视频不可用：MediaController 初始化失败"
                return False
            controller.subscribe_internal_video_desensed(self._on_frame)
        except Exception as exc:        # noqa: BLE001 —— SDK 没编译好 / 服务没跑
            self.error = f"DDS 视频不可用：{str(exc).splitlines()[0][:120]}"
            return False

        threading.Thread(target=self._decode_loop, daemon=True).start()
        if not self._started.wait(wait):
            self.error = f"DDS 视频 {wait:.0f} 秒内没有收到画面（noetix-video-capture 服务在跑吗？）"
            return False
        self.error = None
        self.source = "dds"
        self.note = "画面来自 noetix-video-capture 的 DDS 视频流"
        return True

    def _start_direct(self, wait: float) -> bool:
        """直连 D435I；with_depth 时彩色+深度同一条 pipeline（同帧同步）。

        厂商服务没停时设备被占，会在这里失败并回落到 DDS（见 start()）。
        """
        try:
            import pyrealsense2 as rs
        except ImportError as exc:
            self.error = f"直连相机不可用：缺少 pyrealsense2（{exc}）"
            return False
        try:
            cfg = rs.config()
            cfg.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
            if self.with_depth:
                cfg.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 15)
            pipeline = rs.pipeline()
            profile = pipeline.start(cfg)
            if self.with_depth:
                self._depth_scale = profile.get_device() \
                    .first_depth_sensor().get_depth_scale()
        except Exception as exc:        # noqa: BLE001 —— 设备被占 / 没插 / 权限
            self.error = f"直连相机不可用：{str(exc).splitlines()[0][:120]}"
            return False
        self._pipeline = pipeline
        self.source = "direct"
        self.note = ("画面来自 D435I 直连（彩色+深度同帧采集，不经过厂商服务）"
                     if self.with_depth else
                     "画面来自 D435I 直连彩色流（不经过厂商服务）")
        self.error = None
        threading.Thread(target=self._direct_loop, daemon=True,
                         name="camera-direct").start()
        if not self._started.wait(wait):
            self.error = f"直连相机 {wait:.0f} 秒内没有收到画面"
            return False
        return True

    def latest(self) -> bytes | None:
        """最近一帧 JPEG 字节。"""
        with self._lock:
            return self._frame

    def select_color(self, ts: float) -> tuple[bytes, float] | None:
        """从环形缓存里取时间上最接近 ts 的彩色帧（DDS 模式的深度配对缓存）。

        返回 (JPEG, 时间差秒)；缓存为空返回 None。
        """
        with self._lock:
            if not self._ring:
                return None
            best = min(self._ring, key=lambda item: abs(item[0] - ts))
        return best[1], best[0] - ts

    def info(self) -> dict[str, Any]:
        """给网页轮询的相机状态。"""
        return {
            "ready": self.ready,
            "error": self.error,
            "fps": round(self.fps, 1),
            "source": self.source,
            "note": self.note,
            "stale": (self.ready and self._frame_at > 0
                      and time.monotonic() - self._frame_at > CAMERA_STALE),
        }

    def close(self) -> None:
        """停止采集线程与画面流。"""
        self.stop.set()
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:           # noqa: BLE001 —— 线程可能正卡在 wait_for_frames
                pass

    # ---------------- 内部 ----------------

    def _on_frame(self, stream: Any) -> None:
        """DDS 回调：只登记最新帧，不做任何耗时操作。"""
        try:
            with self._lock:
                self._pending = (int(stream.width), int(stream.height), stream.video_data)
        except Exception:               # noqa: BLE001 —— 回调里绝不能抛
            return

    def _direct_loop(self) -> None:
        """直连采集线程：每个 frameset 取彩色（可选深度）→ JPEG → 槽位/缓存。"""
        stamp, count = time.monotonic(), 0
        while not self.stop.is_set():
            now = time.monotonic()
            if now - stamp >= 1.0:      # 没帧时也要归零，否则会一直显示旧 fps
                self.fps = count / (now - stamp)
                count, stamp = 0, now
            pipeline = self._pipeline
            if pipeline is None:
                break
            try:
                frames = pipeline.wait_for_frames(1000)
            except Exception:           # noqa: BLE001 —— close() 会 stop() 打断等待
                continue
            color = frames.get_color_frame()
            if not color:
                continue
            try:
                ok, buffer = self._cv2.imencode(
                    ".jpg", self._np.asanyarray(color.get_data()),
                    [self._cv2.IMWRITE_JPEG_QUALITY, self.quality])
            except Exception:           # noqa: BLE001 —— 坏帧直接跳过
                continue
            if not ok:
                continue
            with self._lock:
                self._frame = buffer.tobytes()
                self._frame_at = now
                self.width, self.height = color.get_width(), color.get_height()
                self._ring.append((now, self._frame))
            self._started.set()
            count += 1

            depth = frames.get_depth_frame() if self.with_depth else None
            sink = self._depth_sink
            if depth is None or sink is None:
                continue
            if depth.get_frame_number() == self._last_depth_no:
                continue                # 同一深度帧重复到达（彩色 30fps > 深度 15fps）
            self._last_depth_no = depth.get_frame_number()
            try:
                meters = self._np.asanyarray(
                    depth.get_data(), dtype=self._np.float32) * self._depth_scale
            except Exception:           # noqa: BLE001 —— 坏深度帧直接跳过
                continue
            sink(meters, now, self._frame)   # 与彩色帧同一 frameset：同帧配对

    def _decode_loop(self) -> None:
        """把最新原始帧解成 BGR 再压 JPEG；处理不过来就丢帧。"""
        stamp, count = time.monotonic(), 0
        while not self.stop.is_set():
            now = time.monotonic()
            if now - stamp >= 1.0:      # 没帧时也要归零，否则会一直显示旧 fps
                self.fps = count / (now - stamp)
                count, stamp = 0, now
            with self._lock:
                pending, self._pending = self._pending, None
            if pending is None:
                time.sleep(0.005)
                continue

            frame = self._encode(*pending)
            if frame is None:
                continue
            with self._lock:
                self._frame = frame
                self._frame_at = now
                self.width, self.height = pending[0], pending[1]
                self._ring.append((now, frame))     # 进配对缓存（DDS 模式的深度线程会来取）
            self._started.set()
            count += 1

    def _encode(self, width: int, height: int, data: Any) -> bytes | None:
        """YUYV422 / RGB → JPEG；坏帧返回 None。"""
        try:
            raw = bytes(data)           # 640x480 YUYV 约 614400 字节，约 8ms
            if len(raw) == width * height * 2:              # YUYV422
                image = self._cv2.cvtColor(
                    self._np.frombuffer(raw, dtype="uint8").reshape(height, width, 2),
                    self._cv2.COLOR_YUV2BGR_YUYV)
            elif len(raw) == width * height * 3:            # RGB
                image = self._np.frombuffer(raw, dtype="uint8").reshape(height, width, 3)
            else:
                return None
            ok, buffer = self._cv2.imencode(
                ".jpg", image, [self._cv2.IMWRITE_JPEG_QUALITY, self.quality])
        except Exception:               # noqa: BLE001 —— 坏帧直接跳过
            return None
        return buffer.tobytes() if ok else None
