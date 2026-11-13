"""相机预览：订阅官方视频服务的 DDS 流，压成 JPEG 给网页看。

为什么不直接开相机：RealSense 被厂商服务 `noetix-video-capture.service` 独占
（它把 UVC 接口挂在 usbfs 上），任何进程直连都会报 `failed to set power state`。
所以这里订阅它已经发布出来的画面 `subscribe_internal_video_desensed`
（640x480 YUYV422，约 25fps），不抢设备、不用改配置、不用 sudo。

相机是**可选模块**：拿不到画面时只记录原因，行走遥控照常可用。

线程约定：
    DDS 回调线程  —— 只把最新帧挂到槽位，绝不做解码（会把整条流拖慢）
    解码线程      —— bytes() + YUYV→BGR + JPEG 编码，来不及就丢帧
"""
from __future__ import annotations

import threading
import time
from typing import Any

JPEG_QUALITY = 70       # JPEG 质量，越高越占带宽
START_TIMEOUT = 8.0     # 等第一帧的秒数，超时就判定相机不可用
CAMERA_STALE = 2.0      # 超过这个秒数没有新帧就判定画面停滞（DDS 流断了）


class Camera:
    """DDS 视频流 → JPEG 字节。"""

    def __init__(self, quality: int = JPEG_QUALITY) -> None:
        self.quality = quality
        self.source = "dds"
        self.note = "画面来自 noetix-video-capture 的 DDS 视频流"
        self.error: str | None = None
        self.width = 0
        self.height = 0
        self.fps = 0.0
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._frame: bytes | None = None                    # 最新一帧 JPEG
        self._frame_at = 0.0                               # 最近成功解码时刻（判停滞）
        self._pending: tuple[int, int, Any] | None = None   # 待解码的原始帧
        self._started = threading.Event()

    @property
    def ready(self) -> bool:
        """是否有画面可播。"""
        return self._frame is not None and self.error is None

    def start(self, wait: float = START_TIMEOUT) -> bool:
        """订阅视频并起解码线程；失败只记原因、返回 False。"""
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            self.error = f"缺少依赖 {exc}"
            return False
        self._cv2, self._np = cv2, np

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
        return True

    def latest(self) -> bytes | None:
        """最近一帧 JPEG 字节。"""
        with self._lock:
            return self._frame

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
        """停止解码线程与画面流。"""
        self.stop.set()

    # ---------------- 内部 ----------------

    def _on_frame(self, stream: Any) -> None:
        """DDS 回调：只登记最新帧，不做任何耗时操作。"""
        try:
            with self._lock:
                self._pending = (int(stream.width), int(stream.height), stream.video_data)
        except Exception:               # noqa: BLE001 —— 回调里绝不能抛
            return

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
