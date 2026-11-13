"""前方障碍物测距：RealSense D435I 深度流 → 左 / 中 / 右分区最近距离。

为什么默认不启用：DDS 的 `VideoStream` 只有彩色图，SDK 没有深度接口；而
`noetix-video-capture.service` 已把 D435I 的 UVC 接口挂在 usbfs 上独占
（见 camera.py / README「相机画面」）。pyrealsense2 再直连同一设备会争用控制
端点，实测会让相机每秒重枚举一次，彩色 DDS 流与深度流双双中断。
所以本模块只作为 `--depth` 显式开启的实验性旁路：要用它请先停掉厂商视频
服务（此时网页画面也会没有），或只做短时单独测试。

这是**旁路模块**：自己的守护线程、只处理最新帧、结果仅供网页显示，
不进 camera 的采集/解码线程，也不进 10ms 控制循环和 Remote 看门狗。
`pyrealsense2` 是可选依赖（aarch64 上通常来自用户级 site-packages），
延迟导入，缺失或设备打不开时只记原因，遥控照常可用。
连续拿不到帧会主动释放设备，避免持续和厂商服务抢 USB。

测距不是"逐物体"而是"分区最近距离"：不做目标检测时，把画面横向三等分，
每区切成若干小块、取块内深度中值，再取最小块 —— 这样既代表该方向最近的
障碍物，又不会被单点噪点、无效像素和地面带偏。
"""
from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

WIDTH, HEIGHT, FPS = 640, 480, 15
MIN_M = 0.35        # 更近的多半是机身/地面，忽略
MAX_M = 8.0         # 更远的当作"该方向无障碍物"
HZ = 5              # 分析频率；流是 15fps，这里只处理需要的最新帧
ROI_TOP = 0.20      # 裁掉上边（天花板 / 远景）
ROI_BOTTOM = 0.78   # 裁掉下边（地面）
BLOCKS_Y, BLOCKS_X = 6, 8   # 每区分块，块内取中值
MIN_VALID_RATIO = 0.5       # 块内有效像素不足一半就丢弃
FRAME_TIMEOUT_MS = 500      # wait_for_frames 超时；也保证 close() 能及时退出
RETRY_WAIT = 0.2            # 取帧异常后的退避，避免设备断开时忙循环烧 CPU
MAX_MISSES = 10             # 连续失败这么多次就释放设备，把 USB 让回给厂商服务
ZONES = ("left", "center", "right")


def _empty_result(msg: str) -> dict[str, Any]:
    """统一的"无有效结果"结构，前端不用区分是没启用还是坏了。"""
    return {"ok": False, "left": None, "center": None, "right": None,
            "nearest": None, "min_m": MIN_M, "max_m": MAX_M,
            "updated_at": None, "msg": msg}


def _nearest_block(zone: np.ndarray, valid: np.ndarray) -> float | None:
    """一个分区里最近的"可信块"：块内中值的最小值，抗单点噪声。"""
    h, w = zone.shape
    ys = np.linspace(0, h, BLOCKS_Y + 1).astype(int)
    xs = np.linspace(0, w, BLOCKS_X + 1).astype(int)
    best: float | None = None
    for y0, y1 in zip(ys[:-1], ys[1:]):
        for x0, x1 in zip(xs[:-1], xs[1:]):
            mask = valid[y0:y1, x0:x1]
            if mask.size == 0 or mask.mean() < MIN_VALID_RATIO:
                continue
            value = float(np.median(zone[y0:y1, x0:x1][mask]))
            if best is None or value < best:
                best = value
    return best


class DepthVision:
    """D435I 深度流 → 三分区最近距离（米）；失败只记原因，不影响遥控。"""

    def __init__(self, min_m: float = MIN_M, max_m: float = MAX_M, hz: float = HZ) -> None:
        self.min_m = min_m
        self.max_m = max_m
        self.period = 1.0 / hz
        self.stop = threading.Event()
        self.error: str | None = None
        self._lock = threading.Lock()
        self._pipe_lock = threading.Lock()      # close() 与取帧线程都会释放 pipeline
        self._result = _empty_result("深度视觉尚未启动")
        self._pipeline: Any = None
        self._scale = 0.001          # D435I z16 默认 1mm；start() 会读真实值
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """只开深度流（不碰厂商服务的彩色流），起守护线程。"""
        try:
            import pyrealsense2 as rs
        except ImportError as exc:      # 可选依赖：装不上也不能拖垮遥控
            self.error = f"缺少依赖 {exc}"
            self._set(_empty_result(self.error))
            return False

        try:
            cfg = rs.config()
            cfg.enable_stream(rs.stream.depth, WIDTH, HEIGHT, rs.format.z16, FPS)
            pipeline = rs.pipeline()
            profile = pipeline.start(cfg)
            self._scale = profile.get_device().first_depth_sensor().get_depth_scale()
        except Exception as exc:        # 设备被占 / 权限 / UVC 异常
            self.error = f"深度流不可用：{str(exc).splitlines()[0][:120]}"
            self._set(_empty_result(self.error))
            return False

        self._pipeline = pipeline
        self._thread = threading.Thread(target=self._loop, daemon=True, name="depth-vision")
        self._thread.start()
        return True

    def snapshot(self) -> dict[str, Any]:
        """给 HTTP 线程的只读快照。"""
        with self._lock:
            return dict(self._result)

    def close(self) -> None:
        """停线程并释放深度流；与 camera.close() 一样只是旁路收尾。"""
        self.stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._release()

    def _release(self) -> None:
        """幂等地停掉 pipeline：取帧线程连续失败时也会调，主动让出 USB 设备。"""
        with self._pipe_lock:
            pipeline, self._pipeline = self._pipeline, None
            if pipeline is not None:
                try:
                    pipeline.stop()
                except Exception:
                    pass

    # ---------------- 内部 ----------------

    def _set(self, result: dict[str, Any]) -> None:
        with self._lock:
            self._result = result

    def _loop(self) -> None:
        next_at, misses = 0.0, 0
        while not self.stop.is_set():
            try:
                frames = self._pipeline.wait_for_frames(FRAME_TIMEOUT_MS)
            except Exception as exc:
                misses += 1
                if misses >= 3:
                    self._set(_empty_result(f"深度帧中断：{str(exc).splitlines()[0][:100]}"))
                if misses >= MAX_MISSES:
                    self._set(_empty_result("深度流反复不可用，已释放设备（画面优先）"))
                    self._release()
                    return
                self.stop.wait(RETRY_WAIT)   # 退避：设备掉线时别空转烧 CPU
                continue
            misses = 0
            now = time.monotonic()
            if now < next_at:           # 只按 HZ 分析，多余的帧直接丢掉
                continue
            next_at = now + self.period
            frame = frames.get_depth_frame()
            if not frame:
                continue
            try:
                meters = np.asanyarray(frame.get_data(), dtype=np.float32) * self._scale
                self._set(self._measure(meters))
            except Exception as exc:
                self._set(_empty_result(f"深度帧解析失败：{exc}"))

    def _measure(self, meters: np.ndarray) -> dict[str, Any]:
        h, w = meters.shape
        roi = meters[int(h * ROI_TOP):int(h * ROI_BOTTOM), :]
        valid = (roi >= self.min_m) & (roi <= self.max_m)
        bounds = np.linspace(0, w, len(ZONES) + 1).astype(int)

        zones: dict[str, float | None] = {}
        for name, a, b in zip(ZONES, bounds[:-1], bounds[1:]):
            value = _nearest_block(roi[:, a:b], valid[:, a:b])
            zones[name] = round(value, 2) if value is not None else None
        found = [v for v in zones.values() if v is not None]
        return {"ok": True, **zones,
                "nearest": min(found) if found else None,
                "min_m": self.min_m, "max_m": self.max_m,
                "updated_at": time.time(), "msg": ""}
