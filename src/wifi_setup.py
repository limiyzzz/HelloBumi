#!/usr/bin/env python3
"""机器人 WiFi 配网：在机器人（Jetson）本机上改无线网卡连哪个 WiFi。

PC 用 VSCode 的 SSH 远程连到机器人后，在终端里直接跑这个脚本即可，
所有操作都是本机 `nmcli`（NetworkManager）。

```bash
cd /home/noetix/projects/bumi-main/src
python3 wifi_setup.py status                         # 当前连的哪个 WiFi、IP 多少
python3 wifi_setup.py scan                           # 扫描周围 WiFi
python3 wifi_setup.py connect CMCC -p 12345678       # 换到新 WiFi
python3 wifi_setup.py connect CMCC --forget-old      # 换过去并清掉旧配置
python3 wifi_setup.py forget CMCC                    # 删掉已保存的 WiFi
python3 wifi_setup.py                                # 交互式向导（推荐）
```

要点：
- 改连接要权限：默认用 sudo（会问一次机器人口令；也可以 `--sudo-password` 或环境变量
  `ROBOT_SUDO_PASSWORD` 给；以 root 跑或免密 sudo 时加 `--no-sudo`）；
- 换到新网络并拿到 IP 后，**旧的 WiFi 配置自动删除**（不再询问）；
- **每次都要输密码**：不复用已保存配置里的密码，换网络时一律重建连接；
  （密码本身仍保存在机器人的连接配置里：机器人上没人能交互输密码，NM 必须持有密钥
  才能维持连接和断线重连。把 psk 改成「外部提供」会让连接立刻掉线，已验证过）；
- 同名旧配置先删再建，避免「改了密码却不生效」；
- 切换成功后无线网卡 IP 会变，`status` 打印的新 IP 就是网页遥控地址。
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field


# ------------------------------------------------------------------ 小工具
def shq(text: str) -> str:
    """给字符串加 shell 单引号，安全地拼进命令。"""
    return shlex.quote(str(text))


def split_terse(line: str) -> list[str]:
    """切分 `nmcli -t` 的一行：只在「未被反斜杠转义」的冒号处切，再还原转义。"""
    parts = re.split(r"(?<!\\):", line)
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in parts]


@dataclass
class Result:
    """一条命令的结果。"""

    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def msg(self) -> str:
        """失败时优先给 stderr，没有就给 stdout。"""
        return (self.err or self.out).strip()


class Runner:
    """本机执行命令，统一处理 sudo 提权。"""

    def __init__(self, sudo_password: str | None = None, use_sudo: bool = True) -> None:
        self.use_sudo = use_sudo and os.geteuid() != 0      # 已经是 root 就不用 sudo
        self._sudo_password = sudo_password
        self._asked = False
        self._warned = False

    def sudo_password(self) -> str:
        """拿 sudo 口令：参数 → 环境变量 → 交互问一次（只问一次）。"""
        if self._sudo_password is None and not self._asked:
            self._asked = True
            self._sudo_password = os.environ.get("ROBOT_SUDO_PASSWORD")
            if not self._sudo_password and sys.stdin.isatty():
                self._sudo_password = getpass.getpass("sudo 口令：")
        return self._sudo_password or ""

    def wrap_sudo(self, cmd: str) -> tuple[str, str]:
        """包成 sudo，返回 (命令, 要写进 stdin 的口令)。"""
        pw = self.sudo_password()
        if pw:
            return f"sudo -S -p '' -- bash -c {shq(cmd)}", pw + "\n"
        if not self._warned:
            self._warned = True
            print("[提示] 没有 sudo 口令，走 sudo -n（需要免密 sudo 或以 root 跑，"
                  "否则加 --sudo-password）", file=sys.stderr)
        return f"sudo -n -- bash -c {shq(cmd)}", ""

    def run(self, cmd: str, timeout: float = 30, sudo: bool = False) -> Result:
        stdin_data = ""
        if sudo and self.use_sudo:
            cmd, stdin_data = self.wrap_sudo(cmd)
        try:
            proc = subprocess.run(["bash", "-lc", cmd], input=stdin_data,
                                  capture_output=True, text=True, timeout=timeout)
            return Result(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            return Result(124, "", f"命令超时（{timeout}s）")


# ------------------------------------------------------- 本机 nmcli 命令拼装
WIFI_DEV_DETECT = (
    "dev=$(nmcli -t -f DEVICE,TYPE device status 2>/dev/null "
    "| awk -F: '$2==\"wifi\"{print $1; exit}')"
)


def _detect(iface: str | None) -> str:
    return f"dev={shq(iface)}" if iface else WIFI_DEV_DETECT


def cmd_status(iface: str | None = None) -> str:
    """一次拿到：网卡名 + 网卡详情 + 所有连接 + 每条 WiFi 配置对应的 SSID。"""
    return "\n".join([
        _detect(iface),
        'if [ -z "$dev" ]; then echo "__NO_WIFI_DEV__"; exit 0; fi',
        'echo "__DEV__:$dev"',
        'nmcli -t device show "$dev" 2>/dev/null',
        'echo "__SEC_CON__"',
        "nmcli -t -f NAME,UUID,TYPE,DEVICE,AUTOCONNECT,AUTOCONNECT-PRIORITY con show 2>/dev/null",
        'echo "__SEC_WIFI__"',
        "nmcli -t -f UUID,TYPE con show 2>/dev/null "
        "| awk -F: '$2==\"802-11-wireless\"{print $1}' "
        "| while read -r u; do "
        "ssid=$(nmcli -t -f 802-11-wireless.ssid con show \"$u\" 2>/dev/null "
        "| sed -n 's/^802-11-wireless.ssid://p' | head -1); "
        'echo "__SAVED__|$u|$ssid"; done',
        'echo "__SEC_REG__"',
        "iw reg get 2>/dev/null | sed -n '2p'",   # 地区码：决定 12/13 信道能不能扫到
    ])


def cmd_rescan(iface: str | None = None) -> str:
    """触发一次主动扫描（这一步需要管理员权限，否则只会拿到缓存结果）。"""
    return "\n".join([
        _detect(iface),
        'if [ -z "$dev" ]; then echo "__NO_WIFI_DEV__"; exit 1; fi',
        'nmcli dev wifi rescan ifname "$dev" 2>&1 || nmcli dev wifi rescan 2>&1',
    ])


def cmd_wifi_list(iface: str | None = None, in_use: str = "IN-USE") -> str:
    """列出 NetworkManager 缓存里扫到的 WiFi（IN-USE 列名新旧 nmcli 不同）。"""
    return "\n".join([
        _detect(iface),
        'if [ -z "$dev" ]; then echo "__NO_WIFI_DEV__"; exit 1; fi',
        f'nmcli -t -f {in_use},SSID,SIGNAL,SECURITY,CHAN,BSSID dev wifi list ifname "$dev" 2>/dev/null '
        f'|| nmcli -t -f {in_use},SSID,SIGNAL,SECURITY,CHAN,BSSID dev wifi list 2>/dev/null',
    ])


def cmd_connect(ssid: str, password: str = "", iface: str | None = None,
                name: str | None = None, hidden: bool = False, wait: int = 45,
                priority: int | None = None, ip: str | None = None,
                gw: str | None = None, dns: str | None = None) -> str:
    """连到指定 WiFi：建配置 → 按需设优先级 / 静态 IP → 激活。

    密码留在连接配置里：机器人上没有人能交互输密码，NM 必须自己持有密钥
    才能维持连接和断线重连。脚本层面仍然是每次都要求重新输入密码。
    """
    parts = ["nmcli", "--wait", str(wait), "dev", "wifi", "connect", shq(ssid)]
    if password:
        parts += ["password", shq(password)]
    if iface:
        parts += ["ifname", shq(iface)]
    parts += ["name", shq(name or ssid)]
    if hidden:
        parts += ["hidden", "yes"]

    lines = [" ".join(parts)]
    con = shq(name or ssid)
    if priority is not None:
        lines.append(f"nmcli con mod {con} connection.autoconnect-priority {int(priority)}")
    if ip:
        lines.append(f"nmcli con mod {con} ipv4.method manual ipv4.addresses {shq(ip)}")
        if gw:
            lines.append(f"nmcli con mod {con} ipv4.gateway {shq(gw)}")
        if dns:
            lines.append(f"nmcli con mod {con} ipv4.dns {shq(dns)}")
        lines.append(f"nmcli con up {con}" + (f" ifname {shq(iface)}" if iface else ""))
    return " && ".join(lines)


def cmd_delete(uuid: str) -> str:
    return f"nmcli con delete {shq(uuid)}"


# ------------------------------------------------------------------ 结果解析
@dataclass
class Network:
    """扫描到的一个 WiFi。"""

    ssid: str
    signal: str = "?"
    security: str = ""
    chan: str = ""
    bssid: str = ""
    in_use: bool = False


@dataclass
class Profile:
    """已保存的一条 WiFi 配置。"""

    uuid: str
    name: str
    ssid: str
    device: str = ""
    autoconnect: str = ""
    priority: str = ""


@dataclass
class Status:
    """当前无线网卡状态。"""

    device: str | None = None
    state: str = ""
    connection: str | None = None
    ip: str = ""
    gateway: str = ""
    dns: list[str] = field(default_factory=list)
    profiles: list[Profile] = field(default_factory=list)
    reg: str = ""


def parse_status(text: str) -> Status:
    """解析 cmd_status 的输出。"""
    st = Status()
    if "__NO_WIFI_DEV__" in text:
        return st

    cons: list[list[str]] = []
    wifi_ssid: dict[str, str] = {}
    section = "device"
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("__DEV__:"):
            st.device = line.split(":", 1)[1].strip()
            continue
        if line == "__SEC_CON__":
            section = "con"
            continue
        if line == "__SEC_WIFI__":
            section = "wifi"
            continue
        if line == "__SEC_REG__":
            section = "reg"
            continue
        if section == "reg":
            st.reg = line.strip()
            continue
        if section == "device":
            key, _, val = line.partition(":")
            if key == "GENERAL.STATE":
                m = re.search(r"\(([^)]*)\)", val)
                st.state = (m.group(1) if m else val).strip()
            elif key == "GENERAL.CONNECTION":
                st.connection = val.strip() or None
            elif key == "IP4.ADDRESS[1]":
                st.ip = val.strip()
            elif key == "IP4.GATEWAY":
                st.gateway = val.strip()
            elif key.startswith("IP4.DNS") and val.strip():
                st.dns.append(val.strip())
        elif section == "con":
            cons.append(split_terse(line))
        elif section == "wifi" and line.startswith("__SAVED__|"):
            _, uuid, ssid = line.split("|", 2)
            wifi_ssid[uuid.strip()] = split_terse(ssid.strip())[0]

    for f in cons:
        if len(f) < 3 or f[2] != "802-11-wireless":
            continue
        st.profiles.append(Profile(uuid=f[1], name=f[0], ssid=wifi_ssid.get(f[1], f[0]),
                                   device=f[3] if len(f) > 3 else "",
                                   autoconnect=f[4] if len(f) > 4 else "",
                                   priority=f[5] if len(f) > 5 else ""))
    return st


def parse_scan(text: str) -> list[Network]:
    """解析 cmd_scan 的输出：去重后按信号强度排序。"""
    nets: dict[str, Network] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("__"):
            continue
        f = split_terse(line)
        if len(f) < 3:
            continue
        ssid = f[1].strip()
        if not ssid or ssid in nets:
            continue
        nets[ssid] = Network(ssid=ssid, signal=f[2].strip(),
                             security=f[3].strip() if len(f) > 3 else "",
                             chan=f[4].strip() if len(f) > 4 else "",
                             bssid=f[5].strip() if len(f) > 5 else "",
                             in_use=f[0].strip() in ("*", "yes"))

    def strength(n: Network) -> int:
        try:
            return int(n.signal)
        except ValueError:
            return -1

    return sorted(nets.values(), key=strength, reverse=True)


# ------------------------------------------------------------------ 动作封装
def get_status(runner: Runner, iface: str | None = None) -> Status:
    r = runner.run(cmd_status(iface), timeout=20)
    if not r.ok and "__NO_WIFI_DEV__" not in r.out:
        print(f"[warn] 读状态失败：{r.msg}", file=sys.stderr)
    return parse_status(r.out)


POLKIT_HINT = (
    "  NetworkManager 的主动扫描要管理员权限。想不用每次输口令，一次性放行：\n"
    "    sudo tee /etc/polkit-1/rules.d/50-nm-wifi-scan.rules >/dev/null <<'EOF'\n"
    "    polkit.addRule(function(action, subject) {\n"
    "        if (action.id == \"org.freedesktop.NetworkManager.wifi.scan\" &&\n"
    "            subject.user == \"noetix\") {\n"
    "            return polkit.Result.YES;\n"
    "        }\n"
    "    });\n"
    "    EOF"
)


def do_scan(runner: Runner, iface: str | None = None) -> list[Network]:
    """主动扫一次再列结果：rescan 走 sudo，list 不需要权限。"""
    rr = runner.run(cmd_rescan(iface), timeout=25, sudo=True)
    if not rr.ok:
        print(f"[warn] 触发扫描失败：{rr.msg}", file=sys.stderr)
        print("[warn] 下面只是 NetworkManager 的缓存结果，可能只剩当前连着的那个 WiFi。",
              file=sys.stderr)
        print(POLKIT_HINT, file=sys.stderr)
    time.sleep(2)                                  # 等扫描结果回到缓存

    r = runner.run(cmd_wifi_list(iface), timeout=20)
    nets = parse_scan(r.out)
    if not nets and r.ok:
        # 新版 nmcli 把 IN-USE 列名改成了 ACTIVE，换一次重试
        r2 = runner.run(cmd_wifi_list(iface, in_use="ACTIVE"), timeout=20)
        nets = parse_scan(r2.out)
        if not nets and not r2.ok:
            print(f"[warn] 读取扫描结果失败：{r2.msg}", file=sys.stderr)
    if not nets:
        print("没扫到任何 WiFi（无线网卡在吗？8812cu 驱动加载了吗？）", file=sys.stderr)
    return nets


def do_connect(runner: Runner, ssid: str, password: str = "", iface: str | None = None,
               hidden: bool = False, wait: int = 45, priority: int | None = None,
               ip: str | None = None, gw: str | None = None, dns: str | None = None,
               drop_same_ssid: bool = True) -> Result:
    """连 WiFi。返回 nmcli 的原始结果（rc=0 即激活成功）。"""
    if drop_same_ssid:
        # 同名旧配置留着会让「改密码」不生效，先清掉再建
        for p in get_status(runner, iface).profiles:
            if p.ssid == ssid:
                runner.run(cmd_delete(p.uuid), timeout=20, sudo=True)
    cmd = cmd_connect(ssid, password, iface, hidden=hidden, wait=wait,
                      priority=priority, ip=ip, gw=gw, dns=dns)
    return runner.run(cmd, timeout=wait + 30, sudo=True)


def wait_connected(runner: Runner, iface: str | None = None, timeout: int = 25) -> Status:
    """激活返回后 DHCP / 重新协商还要几秒，等拿到 IP 再返回状态。"""
    deadline = time.monotonic() + timeout
    st = get_status(runner, iface)
    while time.monotonic() < deadline:
        if st.state == "connected" and st.ip:
            break
        time.sleep(1)
        st = get_status(runner, iface)
    return st


def do_forget(runner: Runner, target: str, iface: str | None = None) -> bool:
    """按 SSID / 配置名 / UUID 删除已保存的 WiFi 配置。"""
    hit = [p for p in get_status(runner, iface).profiles if target in (p.ssid, p.name, p.uuid)]
    if not hit:
        print(f"没有找到名为 {target!r} 的 WiFi 配置")
        return False
    ok = True
    for p in hit:
        r = runner.run(cmd_delete(p.uuid), timeout=20, sudo=True)
        print(("已删除" if r.ok else "删除失败") + f"：{p.name}（{p.ssid}）"
              + ("" if r.ok else f" -> {r.msg}"))
        ok = ok and r.ok
    return ok


# ------------------------------------------------------------------ 打印
def print_status(st: Status) -> None:
    print("── 无线网卡 ──")
    if not st.device:
        print("  没找到无线网卡（nmcli device status 里没有 wifi 设备）")
        return
    print(f"  网卡        : {st.device}")
    print(f"  状态        : {st.state or '-'}")
    print(f"  当前 WiFi   : {st.connection or '（未连接）'}")
    print(f"  IPv4        : {st.ip or '-'}")
    print(f"  网关        : {st.gateway or '-'}")
    if st.dns:
        print(f"  DNS         : {', '.join(st.dns)}")
    print(f"  地区码      : {st.reg or '-'}")
    if st.reg and not st.reg.startswith("country CN"):
        print("      [提示] 非 CN 地区码时 12/13 信道的路由扫不到：sudo iw reg set CN")
    if st.profiles:
        print("  已保存 WiFi :")
        for p in st.profiles:
            flag = " ← 当前" if st.connection and p.name == st.connection else ""
            print(f"      - {p.ssid}（配置名 {p.name}，优先级 {p.priority or '0'}）{flag}")
    else:
        print("  已保存 WiFi :（无）")


def print_scan(nets: list[Network]) -> None:
    print(f"── 扫到 {len(nets)} 个 WiFi ──")
    print(f"{'#':>3}  {'信号':>4}  {'加密':<12} {'信道':>4}  SSID")
    for i, n in enumerate(nets, 1):
        print(f"{i:>3}{'*' if n.in_use else ' '} {n.signal:>4}  {n.security or '开放':<12} "
              f"{n.chan:>4}  {n.ssid}")


# ------------------------------------------------------------------ 交互向导
def wizard(runner: Runner, args: argparse.Namespace) -> int:
    st = get_status(runner, args.iface)
    print_status(st)
    if not st.device:
        return 1

    while True:
        print()
        nets = do_scan(runner, args.iface)
        if not nets:
            return 1
        print()
        print_scan(nets)
        choice = input("\n输入序号连接，或直接输 SSID（r 重扫 / q 退出）：").strip()
        if choice.lower() == "q":
            return 0
        if choice.lower() == "r":
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(nets):
            net = nets[int(choice) - 1]
        elif choice:
            net = Network(ssid=choice)
        else:
            continue
        break

    ssid = net.ssid
    # 不复用已保存配置：每次都重新输密码
    password = ""
    if net.security != "开放":
        password = getpass.getpass(f"{ssid} 密码（无密码直接回车）：")

    ip = gw = dns = None
    if input("设置静态 IP？直接回车=自动 DHCP：").strip():
        ip = input("  IP（如 192.168.1.50/24）：").strip()
        gw = input("  网关（可留空）：").strip() or None
        dns = input("  DNS（可留空）：").strip() or None

    print(f"\n正在连接 {ssid} ...")
    r = do_connect(runner, ssid, password, iface=args.iface, wait=args.timeout,
                   ip=ip, gw=gw, dns=dns)
    if not r.ok:
        print(f"连接失败：{r.msg}")
        return 1

    new = wait_connected(runner, args.iface)
    print(f"已连接：{new.connection or '（未连接）'}   IP：{new.ip or '-'}   （{new.device}）")
    if new.ip:
        print(f"网页遥控：http://{new.ip.split('/')[0]}:8080")
    else:
        print("[warn] 激活成功但还没拿到 IP（DHCP 慢 / 密码不对？再跑一次 status 看看）")
    # 换到新网络且确实拿到 IP 后，旧的 WiFi 配置直接删掉，不再询问
    if st.connection and st.connection != new.connection and new.ip:
        old = next((p for p in new.profiles if p.name == st.connection), None)
        if old:
            r2 = runner.run(cmd_delete(old.uuid), timeout=20, sudo=True)
            print(f"已删除旧 WiFi 配置 {st.connection}" if r2.ok
                  else f"删除旧配置失败：{r2.msg}")
    return 0


# ------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="机器人 WiFi 配网（在机器人本机上改无线网卡连的 WiFi）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sudo-password", default=os.environ.get("ROBOT_SUDO_PASSWORD"),
                   help="sudo 口令（不写则交互输入）")
    p.add_argument("--no-sudo", action="store_true", help="不加 sudo（以 root 跑或免密 sudo 时）")
    p.add_argument("--iface", default=None, help="指定无线网卡，如 wlx90de802fc5eb（默认自动找）")
    p.add_argument("--timeout", type=int, default=45, help="连接 WiFi 的等待秒数")
    p.add_argument("--reg", default=None, metavar="CODE",
                   help="先设地区码再操作，如 CN（国内路由常在 12/13 信道，US 码扫不到；重启失效）")

    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="查看当前 WiFi 状态")
    sub.add_parser("scan", help="扫描周围 WiFi")
    sub.add_parser("saved", help="列出已保存的 WiFi 配置")

    c = sub.add_parser("connect", help="连接指定 WiFi")
    c.add_argument("ssid")
    c.add_argument("-p", "--password", default=os.environ.get("WIFI_PSK"),
                   help="WiFi 密码（不写且是加密网络时会提示输入）")
    c.add_argument("--hidden", action="store_true", help="隐藏网络（不广播 SSID）")
    c.add_argument("--priority", type=int, default=None, help="自动连接优先级，越大越优先")
    c.add_argument("--forget-old", action="store_true", help="连上后删掉其它已保存的 WiFi 配置")
    c.add_argument("--ip", default=None, help="静态 IP，如 192.168.1.50/24（默认 DHCP）")
    c.add_argument("--gw", default=None, help="静态网关")
    c.add_argument("--dns", default=None, help="静态 DNS")

    f = sub.add_parser("forget", help="删除已保存的 WiFi 配置")
    f.add_argument("target", help="SSID / 配置名 / UUID")
    return p


def main() -> int:
    args = build_parser().parse_args()
    runner = Runner(sudo_password=args.sudo_password, use_sudo=not args.no_sudo)

    if args.reg:                                   # 地区码决定能看到哪些信道
        r = runner.run(f"iw reg set {shq(args.reg)}", timeout=10, sudo=True)
        print(f"地区码已设为 {args.reg}" if r.ok else f"设置地区码失败：{r.msg}")
        if r.ok:
            print("（重启后失效；要永久生效加内核参数 cfg80211.ieee80211_regdom=CN）")

    if args.cmd == "status":
        print_status(get_status(runner, args.iface))
        return 0
    if args.cmd == "scan":
        print_scan(do_scan(runner, args.iface))
        return 0
    if args.cmd == "saved":
        profiles = get_status(runner, args.iface).profiles
        if not profiles:
            print("没有已保存的 WiFi 配置")
            return 0
        for p in profiles:
            print(f"{p.ssid:<24} 配置名={p.name}  uuid={p.uuid}  优先级={p.priority or '0'}")
        return 0
    if args.cmd == "forget":
        return 0 if do_forget(runner, args.target, args.iface) else 1
    if args.cmd == "connect":
        password = args.password or ""
        if not password and sys.stdin.isatty():
            password = getpass.getpass(f"{args.ssid} 密码（无密码直接回车）：")
        r = do_connect(runner, args.ssid, password, iface=args.iface, hidden=args.hidden,
                       wait=args.timeout, priority=args.priority,
                       ip=args.ip, gw=args.gw, dns=args.dns)
        if not r.ok:
            print(f"连接失败：{r.msg}")
            if "Secrets were required" in r.msg or "passwd-file" in r.msg:
                print("提示：这条配置没存密码，用 -p <密码> 重新连一次即可")
            return 1
        st = wait_connected(runner, args.iface)
        print(f"已连接：{st.connection or '（未连接）'}   IP：{st.ip or '-'}   （{st.device}）")
        if st.ip:
            print(f"网页遥控：http://{st.ip.split('/')[0]}:8080")
        else:
            print("[warn] 激活成功但还没拿到 IP（DHCP 慢 / 密码不对？再跑一次 status 看看）")
        if args.forget_old:
            for p in st.profiles:
                if p.ssid != args.ssid:
                    runner.run(cmd_delete(p.uuid), timeout=20, sudo=True)
                    print(f"已删除旧配置：{p.ssid}")
        return 0
    return wizard(runner, args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        raise SystemExit(130)
