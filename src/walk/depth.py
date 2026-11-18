"""前方障碍物测距：RealSense D435I 深度流 → 左 / 中 / 右分区最近距离。

为什么单独开一路：DDS 的 `VideoStream` 只有彩色图（640x480 YUYV422），SDK
没有深度或点云接口；深度只能由 D435I 自己的 `depth z16` 流提供。

**前提：先停厂商 `noetix-video-capture.service`（`just cam-off`）。** 2026-11-15
实测推翻了 11-13 的「可共存」结论：厂商服务运行时内核以约 55 次/分钟重探测
UVC 接口，并以 usbfs 独占设备，深度流会被饿成 ~0.5fps（网页显示「深度数据已过期」）。
停掉厂商服务后直连实测：深度 15fps、有效像素 82–90%、中位距离 1.44 m。

默认由 camera.py 的直连 pipeline 同时开彩色+深度（`external=True`），
本模块不开流、只消费同一 frameset 里的深度帧，两个画面天然同帧。
只有 `--no-camera --depth` 或相机直连失败回落到 DDS 时，才自己开一路深度流。

这是**旁路模块**：自己的守护线程、只处理最新帧、结果仅供网页显示，
不进 10ms 控制循环和 Remote 看门狗。
`pyrealsense2` 是可选依赖（aarch64 上通常来自用户级 site-packages），
延迟导入，缺失或设备打不开时只记原因，遥控照常可用。
连续拿不到帧会主动释放设备，避免持续占用 USB。

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
FRAME_TIMEOUT_MS = 1000     # 启动预热 / 控制循环+JPEG 高负载时，单帧可能超过 500ms
REPORT_MISSES = 5           # 连续失败这么多次才提示中断，滤掉启动期抖动
RETRY_WAIT = 0.2            # 取帧异常后的退避，避免设备断开时忙循环烧 CPU
MAX_MISSES = 15             # 连续失败这么多次就释放设备，避免持续占用 USB
DEPTH_JPEG_QUALITY = 80     # 伪彩色深度图的 JPEG 质量
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
    """D435I 深度流 → 三分区最近距离（米）；失败只记原因，不影响遥控。

    两种数据来源：
    - `external=True`：深度流由 camera.py 的直连 pipeline 提供（与彩色同一
      frameset，天然同帧同步），本模块不开流，通过 `ingest()` 收数据；
    - 默认：自己开一条只含 depth 的 pipeline，并借助相机帧缓存
      （`attach_camera`）做时间戳配对，让网页上彩色与深度显示同一时刻。
    """

    def __init__(self, min_m: float = MIN_M, max_m: float = MAX_M, hz: float = HZ,
                 external: bool = False) -> None:
        self.min_m = min_m
        self.max_m = max_m
        self.period = 1.0 / hz
        self.stop = threading.Event()
        self.error: str | None = None
        self._external = external
        self._camera: Any = None                    # DDS 模式配对用的相机引用
        self._lock = threading.Condition(threading.Lock())   # 同时当配对事件用：commit 后 notify
        self._pair_seq = 0                          # 配对提交序号：两个 MJPEG 生成器按它锁步推送
        self._pipe_lock = threading.Lock()          # close() 与取帧线程都会释放 pipeline
        self._result = _empty_result("深度视觉尚未启动")
        self._depth_jpeg: bytes | None = None       # 最新伪彩深度图（给网页 MJPEG 用）
        self._pair_color: bytes | None = None       # 与最新深度图同刻的彩色帧（配对结果）
        self._pipeline: Any = None
        self._cv2: Any = None        # 延迟导入，缺 cv2 也不影响遥控
        self._scale = 0.001          # D435I z16 默认 1mm；start() 会读真实值
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """external 模式不开流（数据来自 camera 的直连 pipeline）；否则自己开深度流。"""
        try:
            import cv2
        except ImportError as exc:      # 可选依赖：装不上也不能拖垮遥控
            self.error = f"缺少依赖 {exc}"
            self._set(_empty_result(self.error))
            return False
        self._cv2 = cv2
        if self._external:
            return True

        try:
            import pyrealsense2 as rs
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

    def ingest(self, meters: Any, ts: float, color: bytes | None = None) -> None:
        """直连同源模式入口：camera 线程把同一 frameset 里的深度（米）和彩色 JPEG 送进来。

        两者来自同一个 frameset，commit 成一对后两个 MJPEG 生成器按序号
        锁步推送，网页上两个画面就是同一时刻。
        """
        if self.stop.is_set():
            return
        try:
            jpeg = self._colorize(meters)
            result = self._measure(meters)
        except Exception as exc:        # noqa: BLE001 —— 旁路模块不能拖垮相机线程
            self._set(_empty_result(f"深度帧解析失败：{exc}"))
            return
        self._commit_pair(color, jpeg)
        self._set(result)

    def attach_camera(self, camera: Any) -> None:
        """DDS 模式：记录相机引用，用它的帧缓存做时间戳配对（缓存即同步手段）。"""
        self._camera = camera

    def paired_active(self) -> bool:
        """配对推送是否已生效（第一帧配对提交后才切换，避免深度起不来时卡住彩色画面）。"""
        with self._lock:
            return (self._external or self._camera is not None) and self._pair_seq > 0

    def next_pair(self, last_seq: int, timeout: float, color: bool) -> tuple[int, bytes | None]:
        """等比 last_seq 新的一帧配对数据；返回 (序号, JPEG)。

        JPEG 可能为 None：深度中断时配对槽位会被清空，调用方跳过推送即可。
        超时未等到也返回当前序号，调用方继续循环（期间可响应 stop / 重试）。
        """
        with self._lock:
            if self._pair_seq == last_seq:
                self._lock.wait(timeout)
            return self._pair_seq, (self._pair_color if color else self._depth_jpeg)

    def _commit_pair(self, color: bytes | None, depth_jpeg: bytes | None) -> None:
        """登记一对同刻的（彩色, 深度）并广播序号，唤醒两个推送生成器。"""
        with self._lock:
            if depth_jpeg is not None:
                self._depth_jpeg = depth_jpeg
            self._pair_color = color
            self._pair_seq += 1
            self._lock.notify_all()

    def snapshot(self) -> dict[str, Any]:
        """给 HTTP 线程的只读快照。"""
        with self._lock:
            return dict(self._result)

    def depth_jpeg(self) -> bytes | None:
        """最新伪彩色深度图；未启用或取帧中断时是 None。"""
        with self._lock:
            return self._depth_jpeg

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
            if not result.get("ok"):
                self._depth_jpeg = None     # 中断时不给网页看旧图
                self._pair_color = None     # 配对彩色帧一起作废，画面回退到最新帧
            self._lock.notify_all()         # 唤醒正在等配对的 MJPEG 生成器（序号不变也会醒）

    def _loop(self) -> None:
        next_at, misses = 0.0, 0
        while not self.stop.is_set():
            try:
                frames = self._pipeline.wait_for_frames(FRAME_TIMEOUT_MS)
            except Exception as exc:
                misses += 1
                if misses >= REPORT_MISSES:
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
                jpeg = self._colorize(meters)
                if jpeg is not None:
                    with self._lock:
                        self._depth_jpeg = jpeg
                self._set(self._measure(meters))
            except Exception as exc:
                self._set(_empty_result(f"深度帧解析失败：{exc}"))
                continue
            # 时间戳配对：从相机缓存里取与本帧深度同刻的彩色帧，提交成一对
            if jpeg is not None and self._camera is not None:
                match = self._camera.select_color(time.monotonic())
                self._commit_pair(match[0] if match else None, jpeg)

    def _colorize(self, meters: np.ndarray) -> bytes | None:
        """z16 米制数组 → 伪彩色 JPEG：固定量程着色，无效像素画深灰。

        近处偏红、远处偏蓝（与 <0.8 m 红色警示一致）。按固定 0.35–8 m 映射，
        不随每帧 min/max 漂移，不同帧的颜色才可比；D435I 的红外投射器让白墙、
        暗处也有有效深度，深灰区域才是真正没有数据的地方。
        """
        if self._cv2 is None:
            return None
        try:
            valid = (meters >= self.min_m) & (meters <= self.max_m)
            span = self.max_m - self.min_m
            near = np.clip((self.max_m - meters) / span, 0.0, 1.0)
            gray = (near * 255).astype(np.uint8)
            colored = self._cv2.applyColorMap(gray, self._cv2.COLORMAP_JET)
            colored[~valid] = (40, 40, 40)   # BGR 深灰 = 无深度数据
            ok, buf = self._cv2.imencode(
                ".jpg", colored, [self._cv2.IMWRITE_JPEG_QUALITY, DEPTH_JPEG_QUALITY])
        except Exception:
            return None
        return buf.tobytes() if ok else None

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
