"""Bumi 网页遥控：一个项目 = 一个入口 `walk_web.py` + 一个包 `walk/`。

分层（越往下越靠近 SDK）：

    walk_web.py  命令行参数、退出码
    app.py       装配与生命周期：SDK → 状态 → 相机 → HTTP → 控制循环
    server.py    HTTP 接口（Flask + Werkzeug：页面 / 状态 / 画面 / 命令）
    remote.py    网页意图的共享状态（速度、动作、看门狗）
    camera.py    DDS 视频流 → JPEG
    depth.py     D435I 深度流 → 左/中/右分区距离（纯旁路）

厂商 SDK 的 `.so` 软连接直接放在项目根 `noetix_sdk_bumi/`，不再查找、不再预检。
导入本包时只做两件必须的事，之后各模块就直接 `import highcontrol_py`：
1. 把软连接目录加进 sys.path（运行 `src/walk_web.py` 时默认路径里只有 `src/`）；
2. 设置 `CYCLONEDDS_URI`：dds.xml 里写了运控板的 Peer 地址，不设就发现不了它。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SDK_DIR = Path(__file__).resolve().parents[2] / "noetix_sdk_bumi"

if str(SDK_DIR) not in sys.path:
    sys.path.insert(0, str(SDK_DIR))
os.environ.setdefault("CYCLONEDDS_URI", (SDK_DIR / "dds.xml").as_uri())

from .app import WalkApp, command_table  # noqa: E402
from .camera import Camera  # noqa: E402
from .depth import DepthVision  # noqa: E402
from .remote import Command, Remote  # noqa: E402
from .server import create_app, local_urls, serve  # noqa: E402

__all__ = [
    "Camera",
    "Command",
    "DepthVision",
    "Remote",
    "SDK_DIR",
    "WalkApp",
    "command_table",
    "create_app",
    "local_urls",
    "serve",
]
