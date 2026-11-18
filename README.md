# Bumi 机器人板载程序

所有程序都在机器人（Jetson）本机上运行，**不再需要 Windows 电脑中转**：没有 IP 搜索、没有 SSH 上传、没有 TCP 桥接。
原来的 Windows 端（`pc_control/`、`src/host.py`）已删除；`pixi.toml` + `justfile` 现在只用于管理本机依赖与命令。

## 文件分工

整个项目就是「一个入口 + 一个包」，越往下越靠近 SDK：

| 文件 | 作用 | 用到的 SDK |
| --- | --- | --- |
| `src/walk_web.py` | 命令行入口：只解析参数和返回退出码 | —— |
| `src/walk/app.py` | 装配与生命周期：SDK → 状态 → 视觉 → HTTP → 控制循环 | HighController |
| `src/walk/server.py` | HTTP 接口（Flask + Werkzeug）：页面 + 静态资源 + 四个接口 + 命令分发表 | —— |
| `src/walk/remote.py` | 网页意图的共享状态：速度、行走开关、看门狗、使能防呆 | —— |
| `src/walk/vision.py` | D435I 直连：一条 pipeline 出彩色 JPEG + 深度测距/伪彩图（`--depth` 才开深度） | pyrealsense2, cv2 |
| `vendor/noetix_sdk_bumi/` | 厂商 SDK 的 git submodule：头文件 / pybind wrapper / 静态库 / DDS 库与头文件 / `config/dds.xml`；3 个 `.so` 是本地编译产物，不提交 | —— |
| `scripts/sdk/` | 精简版 `CMakeLists.txt` + `build.sh`：给 submodule 编译出 3 个 `.so` 并放到 SDK 根目录 | —— |
| `src/web/index.html` | 遥控网页：纯结构（`web/` 必须和 `walk_web.py` 同级） | —— |
| `src/web/app.css` | 页面样式（由 `/static/` 提供） | —— |
| `src/web/app.js` | 页面逻辑（ES module，零 CDN 依赖） | —— |
| `src/web/vendor/` | 本地 vendor 的前端库 + 许可证 + `vendor.json` 校验清单 | —— |

调用方向单一，没有互相 import：

```text
walk_web.py → app.WalkApp → (remote, vision, server) → vendor SDK 的 .so（只用于运动控制）
                                      └→ D435I 直连：彩色 + 可选深度（纯旁路，不参与控制）
```

## 开发环境：pixi + just

Python 依赖统一写在项目根的 `pixi.toml`（`pixi.lock` 建议提交），命令统一写在 `justfile`：

| 命令 | 作用 |
| --- | --- |
| `just install` | 创建/更新环境（`pixi install`） |
| `just doctor` | 检查 Python / numpy / cv2 / 厂商 SDK 能否导入 |
| `just sdk-build` | 用 `scripts/sdk/` 的精简构建文件编译 vendor SDK 的 3 个 `.so` |
| `just check` | Python + JS 语法检查 + vendor 文件 sha256 完整性 |
| `just serve` | 起网页遥控：D435I 直连彩色流（`--port 8080`） |
| `just serve-nocam` | 同上，不开视觉（连相机也不碰） |
| `just serve-depth` | 同上，彩色+深度同一条 pipeline，出距离和伪彩深度图（`--depth`） |
| `just restart` | 幂等重启：停旧实例、等设备释放、再直连启动（彩色+深度） |
| `just serve-bg` / `just kill` | 后台运行（日志 `logs/walk_web.log`）/ 停止 |
| `just wifi-status` / `wifi-scan` / `wifi-saved` | 查看 WiFi 状态 / 扫描 / 已保存配置 |
| `just wifi-connect <SSID> [密码]` | 连接 WiFi |
| `just wifi` | 交互式配网向导 |

`pixi.toml` 里的内容：

- `python = "3.10.*"`：厂商 SDK 只提供 `*.cpython-310-aarch64-linux-gnu.so`，**不能升 3.11+**；
- `numpy` / `opencv-python`：`walk/vision.py` 直连取帧、JPEG 编码与伪彩着色的运行时依赖；
- `flask`（含 `werkzeug`）：`walk/server.py` 的 HTTP 服务，WSGI + threaded；
- `psutil`：`walk/server.py` 枚举本机网卡 IPv4（`net_if_addrs()`），替掉手写的 `fcntl.ioctl`；
- `typer` / `questionary` / `tenacity` / `rich`：`wifi_setup.py` 的 CLI、交互向导、轮询、表格渲染；
- `esprima`：**仅开发期**用于 `just check` 校验 `app.js` 语法，网页运行时不加载；
- `just`：命令运行器本身（从 conda-forge 装，通过 `~/.local/bin/just` 可直接调用）。

HTTP 层选 **Flask 3 + Werkzeug**（WSGI、threaded），没用 FastAPI / ASGI：控制循环是同步的
10ms 线程，Werkzeug 的后台线程与它同模型，`server.shutdown()` 能直接放进 `app.close()`；
MJPEG 用同步生成器逐帧推，不需要 asyncio 事件循环，也少了 pydantic-core 这类编译扩展。
`create_app(remote, camera)` 是应用工厂，依赖走闭包注入，不再是 `Handler.xxx = ...` 类属性补丁。
启动提示里那些「浏览器打开：http://... (网卡名)」由 `local_urls()` 用 `psutil.net_if_addrs()` 生成：
一次拿到「网卡名 → 地址列表」，不再自己写 `SIOCGIFADDR` ioctl、结构体偏移和 15 字节截断。

> 系统 `python3` 里只有 numpy/cv2，**没有 flask / typer / psutil 等**，两个入口都跑不起来：
> `walk_web.py`、`wifi_setup.py` 一律用 `just` 或 `pixi run python`，避免环境漂移。

## 网页前端：本地 vendor，零 CDN 依赖

`index.html` 只放结构，样式和逻辑分别是 `app.css` / `app.js`（ES module），由 `/static/` 提供。
`server.py` 的 `/static/<path:name>` 路由（`_static_target()` + Flask `send_from_directory()`）
只放行 `web/` 下的 `.css` / `.js`，以及 `web/vendor/` 下的**一级** `.js`，
拒绝更深的子目录、隐藏文件与目录穿越。

### 本地 vendor 的库（都在 `src/web/vendor/`，MIT，附许可证原文）

| 库 | 版本 | 解决的实际问题 | 大小 |
| --- | --- | --- | --- |
| `nipplejs` | 0.10.2 | 手机端**模拟摇杆**：把 4 个离散方向键换成连续的前进/转向量，速度滑块照样生效 | 20 KB |

版本、来源 URL 与 sha256 记录在 `vendor/vendor.json`，`just check` 会逐个校验文件完整性。
这个库用 `<script defer>` 从本机加载（全局 `nipplejs`），**页面运行时不访问外网**。

### 为什么不用 jQuery / Alpine / htmx

- 机器人和手机常在**没有外网**的局域网里，CDN 一挂整个遥控页面就废；
- jQuery 解决的是 2009 年的问题（浏览器差异、Ajax 封装、事件绑定）。这里用的 `fetch`、`classList`、
  `dataset`、`closest`、`setPointerCapture` 全是浏览器原生，代码更短也更直接；
- 这个页面没有列表渲染、动画或组件树。Alpine / htmx / React 只会多一层运行时；
  控制机器人的页面宁可少一层依赖。**值得引的是「能力型」库（模拟量输入），不是「写法型」库**；
- 原来单文件 522 行里大半是内联 CSS 和一个大 IIFE。拆成三个文件，把 DOM 引用、状态、
  命令表（`COMMANDS`）和事件委托集中起来，才是真正的「干净」。

以后要加库，**下载到 `src/web/vendor/`、补 `vendor.json` 的 sha256、写进 `_static_target` 白名单**，
不要写 CDN 的 `<script src="https://...">`。

> 代码已从旧的 `src/bumi/` 全部收拢到 `src/walk/`；只读状态采集
> （`core.py` / `types.py` / `tools.py`）和 SDK 查找/预检层 `sdk.py` 都已删除。

> **同一时刻只能有一个控制进程**：`walk_web.py` 独占 SDK 的 HighController 通道
> 和 D435I。程序启动时会主动检测旧实例，发现就直接报 PID 并拒绝启动（不再让你
> 看到一堆 `Device or resource busy`）。切换用 `just restart`，它会停旧实例、等
> 2 秒设备释放、再启动；手动切换先 `pkill -f walk_web.py` 并等 2 秒。

## 位置

程序就在机器人本机上，**不需要往别处拷、也不用 SSH 中转**：

```bash
cd /home/noetix/projects/hello-bumi/src
ls          # 应看到 walk/  walk_web.py  web/
```

> `web/` 必须和 `walk_web.py` 在同一级目录，否则服务会报「找不到网页文件」。

厂商 SDK 以 `.gitmodules` 中登记的 submodule 形式放在 `vendor/noetix_sdk_bumi/`：
头文件、pybind wrapper、静态库、DDS 库/头文件与 `config/dds.xml` 由上游仓库跟踪，
不再依赖 `/home/noetix/noetix_sdk_bumi/` 的外部路径；原先项目根目录下指向它的旧软连接目录 `noetix_sdk_bumi/` 也已删除。
`highcontrol_py` / `lowcontrol_py` / `mediacontrol_py` 三个 `.so` 是**本地编译产物**，
由 `pybind/*.cpp` 生成，和 `build/` 一样不提交（`submodule` 内已 ignore）。
`walk/__init__.py` 只在导入包时做两件事：把这个目录加进 `sys.path`、把 `CYCLONEDDS_URI`
指向 `vendor/noetix_sdk_bumi/config/dds.xml`（里面写了运控板的 Peer 地址，不设就发现不了它）。
之后 `app.py` 直接 `import highcontrol_py`——**没有 `find_sdk` / 目录搜索 / XML 预检，
也没有 `--sdk-root` 参数**。视觉已完全用 `pyrealsense2` 直连实现，不再经过
`mediacontrol_py` / 厂商视频服务。SDK 缺失会直接报 `ModuleNotFoundError`。

> 换机器后先 `git submodule update --init --recursive` 拉取源码，再跑 `just sdk-build`
> 生成 `.so`（需要 cmake / g++ / pybind11 / Eigen3）。项目的 `scripts/sdk/` 保存了精简版
> `CMakeLists.txt` + `build.sh`：不编译 examples、不依赖 onnxruntime/yaml-cpp，用 `$ORIGIN`
> 定位 DDS 库，并把产物拷到 SDK 根目录。`just sdk-build` 临时覆盖 submodule 的这两个文件，
> 构建完自动还原，因此 submodule 保持干净；改了 pybind wrapper 后也用它重新编译。
>
> `lowcontrol_py` 也在 SDK 里，但**别在同一进程里 import 它**：厂商把
> `RobotBmsData` 等 pybind11 类型在 `highcontrol_py` 和 `lowcontrol_py` 里各注册了一次，
> 同时导入会报 `ImportError: generic_type: type "RobotBmsData" is already registered!`。
> 本项目只用到 `highcontrol_py`（控制）；`mediacontrol_py` 保留但已不再使用。

当前环境：`aarch64` / `Linux 5.15.185-tegra` / `Python 3.10.12`。

## 网页遥控行走

```bash
cd /home/noetix/projects/hello-bumi
just serve                                  # 推荐：依赖由 pixi.toml 提供

# 或直接（必须走 pixi 环境，系统 python3 缺 flask）
pixi run python -u src/walk_web.py --port 8080
```

启动后会打印**每块网卡**的可访问地址（带接口名，方便区分该用哪个）：

```text
HighController 已就绪，等待机器人状态 ...
  浏览器打开：http://10.42.0.211:8080    (enP1p1s0)
  浏览器打开：http://192.168.55.101:8080 (enP8p1s0)
  浏览器打开：http://192.168.5.42:8080   (wlx90de802fc5eb)
```

**手机 / 平板 / 电脑只要和机器人同一个局域网**，挑对应那张网卡的地址打开即可（不需要装任何客户端）。

只想看某一张网卡（比如外接的 USB 无线网卡）就加 `--iface`：

```bash
pixi run python -u src/walk_web.py --port 8080 --iface wlx90de802fc5eb
# → 浏览器打开：http://192.168.5.42:8080    (wlx90de802fc5eb)
```

想断开终端也继续跑：

```bash
cd /home/noetix/projects/hello-bumi/src
nohup python3 -u walk_web.py --port 8080 > walk_web.log 2>&1 &
tail -f walk_web.log      # 看日志
pkill -f walk_web.py      # 停止
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--port` | `8080` | 网页端口；被占用时换 `8081` |
| `--iface` | 全部网卡 | 只显示指定网卡的访问地址，如 `wlx90de802fc5eb` |
| `--bind` | `0.0.0.0` | 监听地址，改成 `127.0.0.1` 就只有本机能访问 |
| `--no-camera` | 关 | 不启动视觉（连相机都不碰，只遥控） |
| `--depth` | 关 | 同一条 pipeline 加开深度：分区距离 + 伪彩深度图 |

### 相机画面

视觉卡片里是 D435I 的实时画面（MJPEG，约 30 帧/秒），右上角显示实测帧率；
加 `--depth` 时画面下方还会多一张伪彩深度图（见「前方测距」）。
视觉是**独立可选模块**：拿不到画面时页面只显示「不可用 + 原因」，行走遥控照常可用。

彩色和深度**都用同一套方案**：`pyrealsense2` 直连 D435I，一条 pipeline：

```text
walk_web.py → Vision（src/walk/vision.py）→ pyrealsense2 pipeline
        ├─ color bgr8 640x480@30 → JPEG → MJPEG 25fps → 浏览器 <img>
        └─ depth z16 640x480@15（--depth 才开）
              → 投递最新一帧 → 独立线程 5Hz 测距 + 伪彩 → MJPEG 5fps
```

不依赖厂商视频服务（官方文档里它叫 `noetix-video-capture.service`，只发布彩色
YUYV 流、没有深度）。该服务运行时会独占 D435I，本项目直接连不上，需要先停掉并
禁用开机自启（见「视觉」一节的命令）。

**实现要点：**

- 采集线程只做两件事：彩色 `imencode`（约 4ms）+ 把深度米制数组投递到槽位；
- 测距和着色在分析线程做（约 14ms/帧、5Hz），**只处理最新帧**，来不及就丢，
  绝不阻塞取帧——这是之前「越用越卡」的根治办法；
- 两条 MJPEG 各推各的：彩色 25fps、深度图 5fps，不锁步；
- 浏览器端断流后用 1×1 透明 GIF 断开连接（不用 `removeAttribute`，避免 error
  递归）、2 秒重连节流；后端重启时 `/api/status` 的 `instance` 字段变了，
  网页自动 reload，丢掉旧连接。

启动日志：

```text
D435I 直连已启动：640x480（彩色）
# 或
D435I 直连已启动：640x480（彩色 + 深度）
# 失败
视觉不可用：D435I 直连失败：...（网页仍可遥控）
```

### 界面操作（半吊起、脚着地的场景）

```text
按住 W/S 前后、A/D 转向，松开即停（也可以用方向键或点界面按钮）
空格 停车    红色「急停」 停车 + 请求失能
R 起身       I 恢复使能（仅 workmode=30 时有效）
O 进入/退出行走    C 收纳    Q 退出服务
```

「移动」卡片右上角可切 **方向键 / 摇杆**：摇杆给的是连续量，前后=前进后退、左右=转向，
选择会记在浏览器 `localStorage` 里。切模式、急停、收纳、退出、失焦都会把摇杆归零。

「动作」区是**一次性指令**：点一下下发一条，运控板自己把整套动作播完，不用按住。
转发的是 SDK `ControlCmd` 里对应的枚举：

| 按钮 | 指令 | 说明 |
| --- | --- | --- |
| 挥手 | `SWING` | 手臂挥动 |
| 握手 | `SHAKE` | 伸手握手 |
| 欢呼 | `CHEER` | 欢呼动作 |
| 舞蹈 1 | `DANCE1` | 舞蹈套路一 |
| 舞蹈 2 | `DANCE2` | 舞蹈套路二 |

动作只在**非行走**状态下生效：行走中点击会提示「请先停止行走」，避免边走边跳。
想加别的动作（`DANCE`、`RUN`、`TEAR` 等）只要在 `web/index.html` 里照抄一个
`<button class="btn" data-act="枚举名">名称</button>`，后端会自动放行。

按「先直立 → 使能 → 行走」的顺序：

1. 让机器人先直立：点「起身」或按 `r`（发 `FALLTOSTAND`）；若仍倒着，用官方 APP 让它站稳。
2. 适当放松吊架，让脚真正着地承重，不要完全吊空。
3. 使能：`workmode=30` 时点「恢复使能」或按 `i`（发 `START`）；成功后显示 `workmode 0`。
4. 行走：点「行走」或按 `o`（持续发 `WALK`），应显示 `workmode 2`。再按一次退出并停车。
5. 遥控移动：按住 `W`/`S`/`A`/`D`，速度用滑块调，**先小后大**（吊起实验建议 0.1~0.3）。
6. 收尾：「收纳」停车进收纳姿态；「急停」停车并请求失能；「退出」让机器人侧停车并结束服务。

### 安全机制

- 网页每 0.1 秒发一次当前速度，服务端 **0.5 秒收不到命令就自动把速度归零**（保持站立，不退出行走模式）。
  所以关掉浏览器、断网、手机锁屏都不会让机器人一直走。
- 浏览器切到后台或窗口失焦时，网页会立刻发一次停车。
- 程序退出（Ctrl+C 或网页「退出」）前会连发 20 帧零速度，避免最后一条速度残留。
  启动阶段任何一步失败（含端口占用）也走同一套收尾，不会留下相机线程或未清零的速度。
  收尾期间会忽略重复的 Ctrl+C：`pixi run` 是父进程，Ctrl+C 时终端把 SIGINT 发给整个
  前台进程组，它还会再给 python 转发一次，第二个信号原本会把收尾打断。
  想让 `pixi` 完全不报 `interrupted by SIGINT`，用网页「退出 (q)」让进程正常返回即可。
- HTTP 服务在后台线程启动，主线程最多等 2 秒：启动卡住也**不会挡住控制循环**（看门狗只有在循环里才生效）。
  超时后服务若最终失败，后台会补一条 `[warn]` 日志，不静默吞异常；退出时 `close()` 先补零速度，
  再等启动线程收尾（最多 1 秒）后关服务。
- 视觉（`vision.py`）是**纯旁路**：彩色默认开、深度要 `--depth` 才开。深度结果只并入
  `/api/status` 给网页显示；不进 10ms 控制循环、不进看门狗、不参与任何运动决策。
  深度分析在独立线程、只处理最新帧，超过 2 秒没更新网页就清空距离；连续取帧失败会
  主动释放设备，不持续占用 USB。
- `START` 只在 `workmode=30` 时允许下发，其余状态会被服务端拒绝，防止误失能摔倒。
- 真实的 HTTP 路由：`GET /`（页面）、`GET /static/<css|js>` 与 `/static/vendor/<js>`（本地静态资源）、
  `GET /api/status`（状态 + 相机 + 前方距离）、`GET /api/camera.mjpg`（画面流）、
  `GET /api/depth.mjpg`（伪彩深度图流，仅 `--depth` 时可用）、`POST /api/cmd`（命令），
  没有鉴权，**只在可信局域网里用**；需要更严格时用 `--bind 127.0.0.1` 配合 SSH 端口转发。

## 前方测距（`src/walk/vision.py`）

**默认关闭，打开方式是 `just serve-depth`（等价于在启动命令上加 `--depth`）。**

深度数据来自 D435I 自己的 `depth z16` 流。官方视频服务 `noetix-video-capture.service`
只发布彩色 YUYV（`VideoStream.video_data` 恰好 614400 字节 = 640×480 一帧），SDK 里
没有深度或点云接口；而且它运行时独占 D435I、把深度流饿到 ~0.5fps。所以本项目
**不再走官方视频服务**，彩色和深度都自己直连（停服务与禁自启命令见「视觉」一节）。

停掉厂商服务后直连实测：深度 15fps、有效像素 82–90%、中位距离 1.44 m；
彩色走同一条 pipeline 约 30fps，两条流都稳定。用法：

```bash
just serve-depth
```

开启后视觉卡片里依次是彩色画面、伪彩深度图、以及 **左 / 中 / 右** 三个方向最近的障碍物距离
（< 0.8 m 标红）；**只供人参考，不参与任何运动决策**。

- 数据来源：`depth z16` 流（640×480@15），按设备 `depth_scale`（实测 0.001）换算成米。
- 量程 0.35–8 m。裁掉画面上 20% 与下 22%（天花板 / 地面），每区切成 6×8 小块、取块内深度
  中值，再取最小块 —— 避免单个噪点、无效像素或地面把「最近距离」带偏。
- `pyrealsense2` 是可选依赖（本机 2.58.4，aarch64 上来自用户级 site-packages），
  **没有**写进 `pixi.toml`。缺失或设备打不开时只打印原因，遥控照常可用；`just doctor` 会提示。
- 结果超过 2 秒没更新（`VISION_STALE`）就判过期，网页清空距离并显示原因。取帧超时阈值
  1000ms、连续 `REPORT_MISSES` 次才提示中断（启动预热期不再误报），累计 `MAX_MISSES`
  次后主动 `pipeline.stop()` 释放设备。
- 画面停滞也有检测：超过 `CAMERA_STALE` 秒没有新帧，`/api/status.camera.stale` 置真，
  页面明确提示「画面已停滞」，不会静默停留在最后一帧。

### 伪彩深度图（`/api/depth.mjpg`）

同一路深度帧顺带编码成 JPEG，视觉卡片里彩色画面下方就是深度图（MJPEG，5 帧/秒）。

- **固定量程着色**：可视化量程 `0.30–5 m`（`VIEW_MIN_M` / `VIEW_MAX_M`，比测距的
  0.35–8 m 窄），线性映射到 `COLORMAP_JET`，近处偏红、远处偏蓝；按 8 m 归一化会把
  室内场景压成黄绿色、几乎看不到蓝。量程固定，帧与帧之间颜色可比。
- **无回波区画暗蓝** `(60, 0, 0)`：D435I 输出 0 的像素（室内常占 40–45%）按「最远」
  处理，整幅图都是蓝色系，不会像深灰那样一片黑，也不会造出假的近处障碍。
- JPEG 质量 80（`DEPTH_JPEG_QUALITY`），640×480 单帧约 35–45 KB；着色与编码约 14ms，
  按 `COLOR_HZ=5` 限频。之前试过中值 + 金字塔填洞（31ms + OpenCV 多线程抢 CPU），
  实际更卡，已回退。
- 中断处理：深度结果一变成 `ok=False`，后端就把缓存的 JPEG 置空（不会让网页停在旧图上）；
  深度图 `<img>` 的 `error` 事件触发「深度图中断，正在重试 ...」，由下一轮 `/api/status`
  轮询自动重连，不做立即重试，避免断流时反复建连接。
- 未启用时 `/api/depth.mjpg` 直接返回 `503` + 原因，网页显示「深度图未启用（just serve-depth 开启）」。

## WiFi 配网（`src/wifi_setup.py`）

换机器人无线网卡连的 WiFi 用 `src/wifi_setup.py`，**在机器人本机上跑**
（PC 用 VSCode 的 SSH 远程连上后，在终端里直接执行即可）。推荐用 `just`，它会自动带上 pixi 环境：

```bash
just wifi-status                                       # 当前连的哪个 WiFi、IP 多少
just wifi-scan                                         # 扫描周围 WiFi（自动切 CN 地区码）
just wifi-connect CMCC 12345678                        # 换到新 WiFi
just wifi-connect-static CMCC 192.168.1.50/24 192.168.1.1   # 静态 IP
just wifi-forget CMCC                                  # 删掉已保存的 WiFi
just wifi                                              # 交互式向导（推荐）
```

等价的原始命令（在项目根执行）：

```bash
pixi run python src/wifi_setup.py status
pixi run python src/wifi_setup.py scan
pixi run python src/wifi_setup.py connect CMCC -p 12345678
pixi run python src/wifi_setup.py connect CMCC --forget-old
pixi run python src/wifi_setup.py forget CMCC
```

> 脚本现在用 `typer`（CLI）、`questionary`（交互向导）、`tenacity`（等 DHCP）、`rich`（表格），
> 依赖全在 `pixi.toml`；`nmcli` 调用改成 argv 列表，不再拼 shell 字符串，`nmcli -t` 用标准库 `csv` 解析。

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--sudo-password` | 无 | sudo 口令（改连接要权限）；不写则交互输入，也可用环境变量 `ROBOT_SUDO_PASSWORD` |
| `--no-sudo` | 关 | 不加 sudo（`sudo python3 wifi_setup.py` 或免密 sudo 时用） |
| `--iface` | 自动找 | 指定无线网卡，如 `wlx90de802fc5eb` |
| `--timeout` | `45` | 连 WiFi 的等待秒数 |

`connect` 的常用选项：`-p/--password`、`--hidden`（隐藏网络）、`--priority N`（越大越优先）、
`--ip 192.168.1.50/24 --gw 192.168.1.1 --dns 8.8.8.8`（静态 IP，默认 DHCP）、
`--forget-old`（连上后清掉其它已保存的 WiFi）。密码不写且是加密网络时会提示输入。

要点：

- 改连接走 `nmcli`，需要 sudo，所以第一次会问一次机器人口令；
- 换到新网络并拿到 IP 后，**旧的 WiFi 配置自动删除**（不再询问）；
- **每次都输密码**：不复用已保存配置里的密码，换网络时一律重建连接；
  （密码仍保存在机器人的连接配置里 —— 机器人上没人能交互输密码，NM 必须持有密钥才能维持
  连接和断线重连；把 psk 改成「外部提供」会让连接立刻掉线，已验证）
- **扫描也要权限**：NetworkManager 的主动扫描（`nmcli dev wifi rescan`）默认是 `auth_admin`，
  不给权限时只能看到缓存结果（往往只有当前连着的那个 WiFi）。脚本会用 sudo 触发扫描，
  想每次都不输口令，一次性放行（写完后无需重启）：
  ```bash
  sudo tee /etc/polkit-1/rules.d/50-nm-wifi-scan.rules >/dev/null <<'EOF'
  polkit.addRule(function(action, subject) {
      if (action.id == "org.freedesktop.NetworkManager.wifi.scan" &&
          subject.user == "noetix") {
          return polkit.Result.YES;
      }
  });
  EOF
  ```
- **地区码**：机器现在是 `country US`，只有 1–11 信道；国内路由若落在 **12/13 信道就扫不到**，
  先切地区码再扫：`python3 wifi_setup.py --reg CN scan`（等价 `sudo iw reg set CN`，重启失效）；
- 同名旧配置会先删再建，避免「改了密码却不生效」；
- 切换成功后无线网卡 IP 会变，`status` 打印出来的新 IP 就是网页遥控地址。

## 视觉

彩色和深度都由 `src/walk/vision.py` 用一条 `pyrealsense2` pipeline 直连 D435I，
**不依赖厂商视频服务**（见上文「相机画面」），和遥控互不干扰。

### 官方视频服务（已停并禁用）

厂商随机器人装了 `noetix-video-capture.service`（1.0.3，`enabled` + `Restart=always`），
运行 `/home/noetix/work/noetix_video_capture/demo video_external_capture 640 480 30`。
它是**官方提供给我们订阅**的彩色视频源：用 librealsense2 开 `COLOR/YUYV` 一条流，
通过 DDS 发到 `/noetix/bumi/agent/video/stream/internal_capture`，SDK 的
`mediacontrol_py.subscribe_internal_video_desensed()` 就是订阅这个话题。
但它**没有深度**：SDK 的 `VideoStream` 只有 `video_data`（彩色像素），没有深度字段或话题。

它运行时会独占 D435I 并让深度流饿到 ~0.5fps（伴随每秒一次的 UVC 重探测），
所以本项目改成自己直连。既然已决定不用它，停掉并禁止开机自启：

```bash
sudo systemctl stop noetix-video-capture
sudo systemctl disable noetix-video-capture
systemctl is-active noetix-video-capture    # inactive
systemctl is-enabled noetix-video-capture   # disabled
```

> 官方以后若重装/升级跑 `install.sh`，它会重新 `enable` 这个服务，届时要再 disable 一次。

## 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| `ModuleNotFoundError: flask / typer / psutil` | 用系统 `python3` 直接跑了；改用 `just serve`，或先 `just install` |
| `ModuleNotFoundError: highcontrol_py` | `vendor/noetix_sdk_bumi/` 没拉下来或 `.so` 没编译：先 `git submodule update --init --recursive`，再跑 `just sdk-build` |
| `启动失败：HighController 初始化失败` | 上一个 `walk_web.py` 还占着通道，先 `pkill -f walk_web.py` |
| `core dumped` | 同上，两个控制进程同时加载了 SDK |
| `启动失败：已有 walk_web.py 在运行（PID ...）` | 旧实例还占着 SDK / D435I（常见于 Ctrl+C 后以为退出了、或之前用 `nohup` 起的后台服务）。用 `just restart` 一步到位；手动就 `pkill -f walk_web.py` 后等 2 秒 |
| `xioctl(VIDIOC_S_FMT) failed ... Device or resource busy` | 设备被别的进程占：先 `pgrep -af walk_web.py`；没有则看厂商服务是否又在跑（`systemctl is-active noetix-video-capture`，是就 stop + disable） |
| `启动失败：... Software caused connection abort` / 端口占用 | 换个端口 `--port 8081` |
| `[warn] HTTP 服务 2s 内没启动完` | 控制循环已照常运行，网页可能稍后才起来；若后面还有 `[warn] ... 最终启动失败`，用 `ss -ltnp` 查端口并换 `--port` |
| 打开网页是「未连接」 | 服务没起、IP 不对、或不在同一网段；看启动时打印的地址 |
| 网页能开但机器人不动 | 没进 `workmode 2`（先「起身」再「使能」再「行走」）；或速度滑块太小 |
| `启动失败：找不到网页文件` | `src/web/index.html` 没和 `src/` 里的代码一起拷过去 |
| `workmode` 一直是 `-` | DDS 没连上运控板，检查 `vendor/noetix_sdk_bumi/config/dds.xml` 里的 Peer 地址、网线 |
| 画面卡住只有一帧 / 提示「画面已停滞」 | 检查 USB：`journalctl -k` 是否每秒刷 `Found UVC 1.50 device` / `GET_CUR ... -32`，是则重新插拔 D435I 或断电重启；也确认厂商服务确实已停（`systemctl is-active noetix-video-capture`） |
| 网页距离一直 `--` / 提示「前方测距未启用」 | 深度默认关闭，`just serve-depth` 或 `--depth` 开启 |
| 深度流只有 ~0.5fps / 「深度数据已过期」 | 厂商服务可能又跑起来了（重装/升级后 `install.sh` 会重新 enable）：stop + disable 后重启本项目 |
| 网页相机显示「不可用」 | 看画面里的原因：设备被占 / 没插好 / 缺 pyrealsense2；遥控照常可用 |
| 启动时短暂提示「深度帧中断」 | 深度流预热期偶发，几秒内会自愈；超时阈值 1000ms、连续 5 次才提示。若持续不恢复，按「画面停滞」条目检查 USB |
| 深度图有大片偏暗的蓝色 | 正常：那是无回波区（室内约 40–45%），已按邻近深度着色并压暗，表示「推测，不是实测」 |
| 深度图颜色整体偏蓝或偏红 | 正常：固定 0.3–5 m 量程，近距离偏红、4 m 外偏蓝；不做逐帧归一化是本意（颜色才可比） |
| 深度图中断，正在重试 ... | 深度流掉帧超过阈值：后端已清空旧图，前端每 2 秒最多重连一次 `/api/depth.mjpg`（避免反复建连接），恢复后自动继续；持续不恢复看是否已「释放设备」 |
| `scan` 只看到当前连着的那个 WiFi | 主动扫描没权限（只拿到缓存）：输 sudo 口令，或加上面那条 polkit 规则 |
| 连上后显示「已连接：None IP：-」 | 多半是激活完又在重新协商 / 等 DHCP：脚本现在会等拿到 IP 再打印；仍为空就再跑 `status`，或检查密码 |
| `Secrets were required, but not provided` | 那条已保存配置没把密码存在系统里（只在用户钥匙串中），root 下取不到：重新输一次密码连（脚本会自动转过去），之后就能直接重连 |
| 目标 WiFi 扫不到（路由在 12/13 信道） | 地区码是 US，只有 1–11 信道：`python3 wifi_setup.py --reg CN scan` |
| 相机画面很卡 | 彩色上限 25fps、深度图 5fps，两路已解耦；刷新网页让新 `app.js` 生效（后端重启会自动 reload）。嫌占资源用 `--no-camera` |
