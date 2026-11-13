"""网页意图的暂存区。

一边是 HTTP 线程（浏览器发命令），另一边是控制循环（按 10ms 节奏取走并下发），
两边通过这里交换数据。所有方法内部加锁，调用方不用关心锁。

命令语义：
    move    设置速度（前后 x / 转向 yaw）
    halt    速度归零，但保持当前行走模式（原地站立）
    act     一次性动作；WALK 是切换键，START 只有失能时才允许下发
    estop   停车 + 退出行走 + 请求失能
    quit    停车并结束程序
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

WATCHDOG = 0.5      # 网页静默超过这个秒数就当作停车（秒）
MAX_SPEED = 1.0     # 速度上限；网页传进来的已经是归一化比例
DISABLED = 30       # workmode=30 表示失能，只有这个状态才允许 START 使能


@dataclass(frozen=True)
class Command:
    """控制循环每周期要下发的内容（一帧快照）。"""

    x: float
    yaw: float
    walking: bool
    action: str | None      # 一次性动作名；None 表示这一帧没有


class Remote:
    """HTTP 线程写、控制循环读的共享状态。"""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed          # 允许下发的一次性动作名（来自 SDK 命令表）
        self.stop = threading.Event()   # 置位后控制循环结束
        self._lock = threading.Lock()
        self._x = 0.0
        self._yaw = 0.0
        self._walking = False
        self._once: str | None = None
        self._workmode: int | None = None
        self._last_cmd = time.monotonic()

    # ---------------- 网页调用 ----------------

    def move(self, x: Any, yaw: Any) -> tuple[bool, str]:
        """设置当前速度。"""
        try:
            fx, fy = float(x), float(yaw)
        except (TypeError, ValueError):
            return False, "速度参数不是数字"
        with self._lock:
            self._x = max(-MAX_SPEED, min(MAX_SPEED, fx))
            self._yaw = max(-MAX_SPEED, min(MAX_SPEED, fy))
            self._touch()
        return True, "ok"

    def halt(self) -> tuple[bool, str]:
        """速度清零，保持当前行走模式。"""
        with self._lock:
            self._stop_moving()
            self._touch()
        return True, "已停车"

    def act(self, name: str) -> tuple[bool, str]:
        """下发一次性动作；WALK 是切换键。"""
        if name not in self.allowed:
            return False, f"不支持的动作：{name}"
        with self._lock:
            if name == "WALK":
                self._walking = not self._walking
                if not self._walking:
                    self._stop_moving()
                self._touch()
                return True, "进入行走" if self._walking else "退出行走，已停车"
            if name == "START" and self._workmode != DISABLED:
                # START 是使能/失能切换键，站着的时候下发会直接摔倒
                return False, f"当前 workmode={self._workmode}，只有失能({DISABLED})才能恢复使能"
            self._once = name
            self._touch()
        return True, f"已下发 {name}"

    def estop(self) -> tuple[bool, str]:
        """急停：停车 + 退出行走 + 请求失能。"""
        with self._lock:
            self._stop_moving()
            self._walking = False
            self._once = "START" if "START" in self.allowed else None
            self._touch()
        return True, "急停：已停车并请求失能"

    def quit(self) -> tuple[bool, str]:
        """停车并结束程序。"""
        with self._lock:
            self._stop_moving()
            self._walking = False
            self._touch()
            self.stop.set()
        return True, "已退出，网页可以关闭了"

    # ---------------- 控制循环调用 ----------------

    def take(self) -> Command:
        """取走当前意图；超时未收到网页命令则按停车处理，取完即清空一次性动作。"""
        with self._lock:
            if time.monotonic() - self._last_cmd > WATCHDOG:
                x = yaw = 0.0           # 关页面、断网、锁屏都会走到这里
            else:
                x, yaw = self._x, self._yaw
            command = Command(x=x, yaw=yaw, walking=self._walking, action=self._once)
            self._once = None
            return command

    # ---------------- 状态回显 ----------------

    def set_workmode(self, workmode: int | None) -> None:
        """记录 SDK 回报的工作模式，供网页显示和使能防呆。"""
        with self._lock:
            self._workmode = workmode

    def status(self) -> dict[str, Any]:
        """给网页轮询的状态快照。"""
        with self._lock:
            return {
                "workmode": self._workmode,
                "walking": self._walking,
                "x": round(self._x, 3),
                "yaw": round(self._yaw, 3),
                "age": round(time.monotonic() - self._last_cmd, 2),
            }

    # ---------------- 内部 ----------------

    def _stop_moving(self) -> None:
        """速度归零（调用方须持有锁）。"""
        self._x = self._yaw = 0.0

    def _touch(self) -> None:
        """刷新看门狗时间戳（调用方须持有锁）。"""
        self._last_cmd = time.monotonic()
