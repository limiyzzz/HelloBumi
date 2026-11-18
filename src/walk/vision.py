"""D435I 视觉：一条 pyrealsense2 pipeline 同时出彩色预览和深度测距。

设计要点
--------
* 只用 RealSense 直连，不依赖厂商 noetix-video-capture / DDS 视频服务。
  官方服务只发布彩色 YUYV（见 README「视觉」），没有深度；而它运行时会独占
  USB 并让深度流饿到 ~0.5fps，所以本项目彩色和深度都自己收。
* 一条 pipeline 同时开 color bgr8@30 和（可选）depth z16@15，同一个
  frameset 里取两路数据。彩色和深度各自线程处理，互不阻塞：
      _capture 线程  wait_for_frames → 彩色 JPEG → _frame 槽位
                               └→ 深度米制数组 → _pending（只覆盖，不积压）
      _analyze 线程  _pending → 分区测距 + 伪彩 JPEG → _result / _depth_jpeg
* 深度分析约 14ms，按 COLOR_HZ=5 限频、只处理最新帧；来不及就丢，
  绝不让取帧线程等它。
* 依赖（pyrealsense2 / cv2 / numpy）在模块顶层静态导入，缺了就启动即报错，
  不搞运行时延迟导入；失败只记 error 并继续跑遥控，模块是纯旁路，
  不进控制循环和看门狗。

对外接口（给 server.py / app.py）：
    start() / close() / ready / error / stop / info()
    latest()      最近一帧彩色 JPEG（字节）
    depth_jpeg()  最近一帧伪彩深度 JPEG，未启用为 None
    snapshot()    分区测距结果（字典）
"""
from __future__ import annotations

import threading
import time
from typing import Any

import cv2
import numpy as np
import pyrealsense2 as rs

# ---- 采集 ----------------------------------------------------------------
WIDTH, HEIGHT = 640, 480
COLOR_FPS, DEPTH_FPS = 30, 15
JPEG_QUALITY = 70           # 彩色 JPEG 质量，越高越占带宽
START_TIMEOUT = 8.0         # 等第一帧的秒数，超时就判相机不可用
CAMERA_STALE = 2.0          # 超过这个秒数没有新帧就判画面停滞
FRAME_TIMEOUT_MS = 1000     # 单帧等待上限，高负载时可能超过 500ms
RETRY_WAIT = 0.2            # 取帧异常后的退避，避免设备断开时忙循环

# ---- 深度 ----------------------------------------------------------------
MIN_M = 0.35                # 更近的多半是机身/地面，忽略
MAX_M = 8.0                 # 更远的当作"该方向无障碍物"
DEPTH_HZ = 5                # 测距分析频率（流是 15fps，只处理需要的最新帧）
ROI_TOP = 0.20              # 裁掉上边（天花板 / 远景）
ROI_BOTTOM = 0.78           # 裁掉下边（地面）
BLOCKS_Y, BLOCKS_X = 6, 8   # 每区分块，块内取中值
MIN_VALID_RATIO = 0.5       # 块内有效像素不足一半就丢弃
REPORT_MISSES = 5           # 连续失败这么多次才提示中断
MAX_MISSES = 15             # 连续失败这么多次就释放设备
VIEW_MIN_M = 0.30           # 伪彩图可视化量程（比测距窄，室内才看得到蓝）
VIEW_MAX_M = 5.0
COLOR_HZ = 5                # 伪彩图刷新率；着色+编码约 14ms，15fps 全算太吃 CPU
DEPTH_JPEG_QUALITY = 80
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


class Vision:
    """D435I 彩色预览 + 深度测距（深度可选），一条 pipeline 全搞定。"""

    def __init__(self, with_depth: bool = False) -> None:
        self.with_depth = with_depth
        self.error: str | None = None
        self.stop = threading.Event()
        self.width, self.height = 0, 0
        self.fps = 0.0
        self._pipeline: Any = None
        self._depth_scale = 0.001
        self._pipe_lock = threading.Lock()

        self._lock = threading.Condition(threading.Lock())
        self._frame: bytes | None = None        # 最新彩色 JPEG
        self._frame_at = 0.0
        self._pending: tuple[np.ndarray, float] | None = None   # 待分析的深度帧（只留最新）
        self._started = threading.Event()
        self._result = _empty_result("深度视觉尚未启动")
        self._depth_jpeg: bytes | None = None
        self._last_color_at = 0.0
        self._last_depth_no = -1
        self._capture_thread: threading.Thread | None = None
        self._analyze_thread: threading.Thread | None = None

    @property
    def ready(self) -> bool:
        return self._frame is not None and self.error is None

    def start(self, wait: float = START_TIMEOUT) -> bool:
        """开 pipeline（彩色 + 可选深度）并起处理线程；失败只记原因。"""
        try:
            cfg = rs.config()
            cfg.enable_stream(rs.stream.color, WIDTH, HEIGHT, rs.format.bgr8, COLOR_FPS)
            if self.with_depth:
                cfg.enable_stream(rs.stream.depth, WIDTH, HEIGHT, rs.format.z16, DEPTH_FPS)
            pipeline = rs.pipeline()
            profile = pipeline.start(cfg)
            if self.with_depth:
                self._depth_scale = profile.get_device() \
                    .first_depth_sensor().get_depth_scale()
        except Exception as exc:        # noqa: BLE001 —— 设备被占 / 没插 / 权限
            self.error = f"D435I 直连失败：{str(exc).splitlines()[0][:120]}"
            self._set(_empty_result(self.error))
            return False

        self._pipeline = pipeline
        self.error = None
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="vision-capture")
        self._capture_thread.start()
        if not self._started.wait(wait):
            self.error = f"D435I 直连 {wait:.0f} 秒内没有收到画面"
            return False
        if self.with_depth:
            self._analyze_thread = threading.Thread(
                target=self._analyze_loop, daemon=True, name="vision-analyze")
            self._analyze_thread.start()
        return True

    def close(self) -> None:
        """停线程并释放设备；幂等。"""
        self.stop.set()
        for thread in (self._capture_thread, self._analyze_thread):
            if thread is not None:
                thread.join(timeout=1)
        with self._pipe_lock:
            pipeline, self._pipeline = self._pipeline, None
            if pipeline is not None:
                try:
                    pipeline.stop()
                except Exception:           # noqa: BLE001 —— 线程可能正卡在 wait_for_frames
                    pass

    # ---- 对外只读接口 ----------------------------------------------------

    def latest(self) -> bytes | None:
        """最近一帧彩色 JPEG。"""
        with self._lock:
            return self._frame

    def depth_jpeg(self) -> bytes | None:
        """最近一帧伪彩深度 JPEG；未启用或中断时为 None。"""
        with self._lock:
            return self._depth_jpeg

    def snapshot(self) -> dict[str, Any]:
        """分区测距的只读快照（HTTP 线程用）。"""
        with self._lock:
            return dict(self._result)

    def info(self) -> dict[str, Any]:
        """给网页轮询的相机状态。"""
        return {
            "ready": self.ready,
            "error": self.error,
            "fps": round(self.fps, 1),
            "source": "direct",      # 现在只有直连，没有别的来源
            "note": "画面来自 D435I 直连（不依赖厂商视频服务）",
            "stale": (self.ready and self._frame_at > 0
                      and time.monotonic() - self._frame_at > CAMERA_STALE),
        }

    # ---- 采集线程 --------------------------------------------------------

    def _capture_loop(self) -> None:
        """wait_for_frames → 彩色 JPEG → 槽位；深度帧只投递最新一份给分析线程。"""
        stamp, count = time.monotonic(), 0
        misses = 0
        while not self.stop.is_set():
            now = time.monotonic()
            if now - stamp >= 1.0:      # 没帧时也要归零，否则会一直显示旧 fps
                self.fps = count / (now - stamp)
                count, stamp = 0, now
            pipeline = self._pipeline
            if pipeline is None:
                break
            try:
                frames = pipeline.wait_for_frames(FRAME_TIMEOUT_MS)
            except Exception:           # noqa: BLE001 —— close() 会 stop() 打断等待
                misses += 1
                if misses >= MAX_MISSES:
                    self.error = "D435I 反复取帧失败，已释放设备"
                    with self._lock:
                        self._frame = None
                    self.close()
                    return
                self.stop.wait(RETRY_WAIT)
                continue
            misses = 0

            color = frames.get_color_frame()
            if color:
                try:
                    ok, buffer = cv2.imencode(
                        ".jpg", np.asanyarray(color.get_data()),
                        [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                except Exception:       # noqa: BLE001 —— 坏帧直接跳过
                    ok = False
                if ok:
                    with self._lock:
                        self._frame = buffer.tobytes()
                        self._frame_at = now
                    self.width, self.height = color.get_width(), color.get_height()
                    self._started.set()
                    count += 1

            if not self.with_depth:
                continue
            depth = frames.get_depth_frame()
            if not depth or depth.get_frame_number() == self._last_depth_no:
                continue                # 同一深度帧重复到达（彩色 30fps > 深度 15fps）
            self._last_depth_no = depth.get_frame_number()
            try:
                meters = np.asanyarray(
                    depth.get_data(), dtype=np.float32) * self._depth_scale
            except Exception:           # noqa: BLE001 —— 坏深度帧直接跳过
                continue
            with self._lock:
                self._pending = (meters, now)   # 只覆盖，绝不积压
                self._lock.notify()

    # ---- 深度分析线程 ----------------------------------------------------

    def _analyze_loop(self) -> None:
        """按 DEPTH_HZ 消费最新深度帧；测距与着色都在这里做，不阻塞取帧。"""
        next_at = 0.0
        while not self.stop.is_set():
            with self._lock:
                if self._pending is None:
                    self._lock.wait(0.2)
                    continue
                meters, ts = self._pending
                self._pending = None
            now = time.monotonic()
            if now < next_at:           # 限频：多余的帧丢掉
                continue
            next_at = now + 1.0 / DEPTH_HZ
            try:
                jpeg = self._colorize(meters) if ts - self._last_color_at >= 1.0 / COLOR_HZ else None
                if jpeg is not None:
                    self._last_color_at = ts
                result = self._measure(meters)
            except Exception as exc:    # noqa: BLE001 —— 旁路模块不能拖垮取帧
                self._set(_empty_result(f"深度帧解析失败：{exc}"))
                continue
            if jpeg is not None:
                with self._lock:
                    self._depth_jpeg = jpeg
            self._set(result)

    def _set(self, result: dict[str, Any]) -> None:
        with self._lock:
            self._result = result
            if not result.get("ok"):
                self._depth_jpeg = None  # 中断时不给网页看旧图

    def _colorize(self, meters: np.ndarray) -> bytes | None:
        """z16 米制数组 → 伪彩 JPEG：固定量程着色，无回波区画暗蓝。"""
        try:
            valid = (meters >= VIEW_MIN_M) & (meters <= VIEW_MAX_M)
            span = VIEW_MAX_M - VIEW_MIN_M
            near = np.clip((VIEW_MAX_M - meters) / span, 0.0, 1.0).astype(np.float32)
            near[~valid] = 0.0          # 无回波 = 最远 = 深蓝，别当"最近"画成红色
            gray = (near * 255.0).astype(np.uint8)
            colored = cv2.applyColorMap(gray, cv2.COLORMAP_JET)
            if (~valid).any():
                colored[~valid] = (60, 0, 0)    # BGR 暗蓝：无回波/推测区
            ok, buf = cv2.imencode(
                ".jpg", colored, [cv2.IMWRITE_JPEG_QUALITY, DEPTH_JPEG_QUALITY])
        except Exception:
            return None
        return buf.tobytes() if ok else None

    def _measure(self, meters: np.ndarray) -> dict[str, Any]:
        """左/中/右分区最近距离；中值与最小块组合抗噪。"""
        h, w = meters.shape
        roi = meters[int(h * ROI_TOP):int(h * ROI_BOTTOM), :]
        valid = (roi >= MIN_M) & (roi <= MAX_M)
        bounds = np.linspace(0, w, len(ZONES) + 1).astype(int)
        zones: dict[str, float | None] = {}
        for name, a, b in zip(ZONES, bounds[:-1], bounds[1:]):
            value = _nearest_block(roi[:, a:b], valid[:, a:b])
            zones[name] = round(value, 2) if value is not None else None
        found = [v for v in zones.values() if v is not None]
        return {"ok": True, **zones,
                "nearest": min(found) if found else None,
                "min_m": MIN_M, "max_m": MAX_M,
                "updated_at": time.time(), "msg": ""}
