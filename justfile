set shell := ["bash", "-c"]

PORT := "8080"

# 列出所有可用命令
default:
    @just --list

# ---------------- 环境 ----------------

install:
    pixi install
    @echo "依赖就绪；用 just doctor 检查 SDK 是否能在 pixi 环境里导入"

doctor:
    pixi run python -c "import sys, numpy, cv2, psutil; print('python', sys.version.split()[0]); print('numpy', numpy.__version__); print('cv2', cv2.__version__); print('psutil', psutil.__version__)"
    pixi run python -c "import sys, os; sys.path.insert(0, 'src'); import walk, highcontrol_py, mediacontrol_py; print('厂商 SDK 导入 OK', walk.SDK_DIR); print('CYCLONEDDS_URI', os.environ['CYCLONEDDS_URI'])"
    @pixi run python -c "import importlib.util as u; print('pyrealsense2', 'OK' if u.find_spec('pyrealsense2') else '未安装（仅 --depth 实验性测距需要）')"

check:
    pixi run python -m compileall -q src/walk src/walk_web.py src/wifi_setup.py
    pixi run python -c "import esprima; esprima.parseScript(open('src/web/app.js', encoding='utf-8').read()); print('app.js 语法 OK')"
    pixi run python -c "import json,hashlib,pathlib; p=pathlib.Path('src/web/vendor'); m=json.load(open(p/'vendor.json')); assert all(hashlib.sha256((p/x['file']).read_bytes()).hexdigest()==x['sha256'] for x in m['libraries']); print('vendor 校验 OK')"
    @echo "Python + JS 语法 + vendor 完整性检查通过"

# ---------------- 相机 ----------------

# 停厂商视频服务，把 D435I 让给 walk_web.py（直连彩色+深度前必须先执行）
cam-off:
    sudo systemctl stop noetix-video-capture
    @systemctl is-active noetix-video-capture || true

# 恢复厂商视频服务（不用本项目直连时再开回来）
cam-on:
    sudo systemctl start noetix-video-capture
    @systemctl is-active noetix-video-capture || true

cam-status:
    @systemctl status noetix-video-capture --no-pager -n 5 || true

# ---------------- 网页遥控（默认直连 D435I，需先 just cam-off） ----------------

serve port=PORT:
    pixi run python -u src/walk_web.py --port {{port}}

serve-nocam port=PORT:
    pixi run python -u src/walk_web.py --port {{port}} --no-camera

# 彩色 + 深度同一条 pipeline（同一 frameset，画面天然同帧）；需先 just cam-off
serve-depth port=PORT:
    pixi run python -u src/walk_web.py --port {{port}} --depth

# 兼容旧命令：--direct-cam 现在已是默认，等价于 just serve
serve-direct port=PORT:
    pixi run python -u src/walk_web.py --port {{port}} --direct-cam

# 改回厂商 DDS 视频流（不碰设备，画面只有彩色、没有深度图）
serve-dds port=PORT:
    pixi run python -u src/walk_web.py --port {{port}} --dds-cam

serve-bg port=PORT:
    #!/usr/bin/env bash
    set -e
    mkdir -p logs
    nohup pixi run python -u src/walk_web.py --port {{port}} > logs/walk_web.log 2>&1 &
    echo "已后台启动（端口 {{port}}），日志：logs/walk_web.log"

kill:
    -pkill -f '[w]alk_web.py'
    @echo "已停止（没找到进程时会忽略报错）"

# 幂等重启：停旧实例 → 等设备释放 → 直连彩色+深度启动（避免残留实例抢 D435I）
restart port=PORT:
    #!/usr/bin/env bash
    set -e
    pkill -f '[w]alk_web.py' || true
    sleep 3
    exec pixi run python -u src/walk_web.py --port {{port}} --depth

logs:
    tail -n 60 -f logs/walk_web.log

urls:
    @echo "本机 IP：$(hostname -I)"
    @echo "网页：http://<对应网卡 IP>:{{PORT}}"

# ---------------- WiFi 配网 ----------------

wifi:
    pixi run python src/wifi_setup.py

wifi-status:
    pixi run python src/wifi_setup.py status

wifi-scan:
    pixi run python src/wifi_setup.py --reg CN scan

wifi-saved:
    pixi run python src/wifi_setup.py saved

wifi-connect ssid psk="":
    #!/usr/bin/env bash
    set -e
    if [ -n "{{psk}}" ]; then
      pixi run python src/wifi_setup.py connect "{{ssid}}" -p "{{psk}}"
    else
      pixi run python src/wifi_setup.py connect "{{ssid}}"
    fi

wifi-connect-static ssid ip gw="" dns="":
    #!/usr/bin/env bash
    set -e
    args=(--ip "{{ip}}")
    if [ -n "{{gw}}" ]; then args+=(--gw "{{gw}}"); fi
    if [ -n "{{dns}}" ]; then args+=(--dns "{{dns}}"); fi
    pixi run python src/wifi_setup.py connect "{{ssid}}" "${args[@]}"

wifi-forget name:
    pixi run python src/wifi_setup.py forget "{{name}}"

wifi-reg code="CN":
    pixi run python src/wifi_setup.py --reg {{code}} status
