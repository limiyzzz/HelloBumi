/* Bumi 网页遥控：零运行时依赖的 ES module。
 * 分层：常量 → DOM/状态 → 通信 → 界面 → 控制 → 事件绑定 → 启动。
 * 页面由 /static/ 提供，接口只有 /api/cmd 与 /api/status。
 */
"use strict";

// ------------------------------------------------------------------ 常量
// 方向定义：[前后 x, 转向 yaw]；x>0 前进，yaw>0 左转
const DIRS = Object.freeze({ w: [1, 0], s: [-1, 0], a: [0, 1], d: [0, -1] });
const ALIAS = Object.freeze({ arrowup: "w", arrowdown: "s", arrowleft: "a", arrowright: "d" });
const ACTIONS = Object.freeze({
  SWING: "挥手", SHAKE: "握手", CHEER: "欢呼", DANCE1: "舞蹈 1", DANCE2: "舞蹈 2",
});
const KEY_COMMANDS = Object.freeze({
  o: "walk", r: "stand", i: "enable", c: "collect", q: "quit",
});

const HEARTBEAT_MS = 100;   // 每周期发当前速度，保活服务端看门狗
const POLL_MS = 300;        // 状态轮询间隔
const MAX_INFLIGHT = 3;     // 网络慢时不要堆积请求
const STREAM_RETRY_MS = 2000;  // 两路 MJPEG 断流后重连间隔，避免重启时疯狂建连接
// 用 1×1 透明 GIF 断开 MJPEG：removeAttribute("src") 会再触发 error，
// 和 error 监听形成无限循环（每次 restart 断流都会点燃，越用越卡）
const BLANK_IMG = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";

// ------------------------------------------------------------------ DOM / 状态
const el = [
  "link", "mode", "walk", "speed", "speedVal", "walkBtn", "toast",
  "cam", "camHint", "camInfo", "camNote",
  "depthImg", "depthHint",
  "dpad", "stickZone",
  "distLeft", "distCenter", "distRight", "distNote",
].reduce((map, id) => {
  map[id] = document.getElementById(id);
  return map;
}, {});

function loadInputMode() {
  try { return localStorage.getItem("bumi.input") === "stick" ? "stick" : "dpad"; }
  catch (err) { return "dpad"; }
}

function saveInputMode(mode) {
  try { localStorage.setItem("bumi.input", mode); } catch (err) { /* 忽略隐私模式 */ }
}

const state = {
  pressed: new Set(),
  joystick: { x: 0, yaw: 0 },   // nipplejs 的模拟量
  inputMode: loadInputMode(),
  speed: 0.3,
  walking: false,
  workmode: null,
  inflight: 0,
  toastTimer: 0,
  camStreaming: false,
  depthStreaming: false,
  camRetryAt: 0,              // 相机下次允许重连的时刻（ms）
  depthRetryAt: 0,            // 深度图下次允许重连的时刻（ms）
  instanceId: null,           // 后端实例 ID；变了就整页重载，丢掉旧连接
};

let stickManager = null;

// ------------------------------------------------------------------ 通信
async function request(path, options) {
  const res = await fetch(path, options);
  return res.json();
}

async function post(msg) {
  if (state.inflight >= MAX_INFLIGHT) return null;
  state.inflight += 1;
  try {
    const data = await request("/api/cmd", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(msg),
    });
    if (!data.ok) toast(data.msg || "命令被拒绝");
    return data;
  } catch (err) {
    setLink(false);
    return null;
  } finally {
    state.inflight -= 1;
  }
}

// ------------------------------------------------------------------ 界面
function toast(text) {
  el.toast.textContent = text;
  el.toast.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => el.toast.classList.remove("show"), 2200);
}

function setLink(ok) {
  el.link.textContent = ok ? "已连接" : "未连接";
  el.link.className = "pill " + (ok ? "on" : "off");
}

function setMode(value) {
  state.workmode = value;
  const known = value !== null && value !== undefined;
  const tone = value === 30 ? " warn" : value === 0 ? " on" : value === 2 ? " hot" : "";
  el.mode.textContent = "workmode " + (known ? value : "-");
  el.mode.className = "pill" + (known ? tone : "");
}

function setWalking(on) {
  state.walking = on;
  el.walk.textContent = on ? "行走中" : "站立";
  el.walk.className = "pill" + (on ? " hot" : "");
  el.walkBtn.textContent = on ? "停止行走 (o)" : "行走 (o)";
}

function showCamProblem(hint, badge) {
  state.camStreaming = false;
  state.camRetryAt = Date.now() + STREAM_RETRY_MS;
  el.cam.src = BLANK_IMG;         // 断开 MJPEG，但不再触发 error 递归
  el.cam.style.display = "none";
  el.camHint.style.display = "block";
  el.camHint.textContent = hint;
  el.camInfo.textContent = badge;
  el.camInfo.className = "pill warn";
  el.camNote.textContent = "";
}

function setCamera(info) {
  if (!info || !info.ready) {
    showCamProblem((info && info.error) || "相机不可用", "不可用");
    return;
  }
  if (info.stale) {
    const how = info.source === "direct" ? "D435I 直连流" : "DDS 视频流";
    showCamProblem("画面已停滞：" + how + "没有新帧，检查 USB 连接与设备是否被占用", "停滞");
    return;
  }
  if (!state.camStreaming && Date.now() >= state.camRetryAt) {
    el.cam.src = "/api/camera.mjpg?t=" + Date.now();
    state.camStreaming = true;
  }
  el.cam.style.display = "block";
  el.camHint.style.display = "none";
  const fps = info.fps ? Math.round(info.fps) + " fps" : "在线";
  el.camInfo.textContent = (info.source === "direct" ? "直连" : "DDS") + " · " + fps;
  el.camInfo.className = "pill on";
  el.camNote.textContent = info.note || "";
}

function hideDepth(hint) {
  state.depthStreaming = false;
  state.depthRetryAt = Date.now() + STREAM_RETRY_MS;
  el.depthImg.src = BLANK_IMG;    // 同相机：别用 removeAttribute 触发 error 递归
  el.depthImg.style.display = "none";
  el.depthHint.style.display = "block";
  el.depthHint.textContent = hint;
}

function setVision(info) {
  const zones = ["Left", "Center", "Right"];
  if (!info || !info.ok) {
    for (const zone of zones) {
      el["dist" + zone].querySelector("strong").textContent = "--";
      el["dist" + zone].classList.remove("near");
    }
    el.distNote.textContent = (info && info.msg) || "深度视觉不可用";
    el.distNote.className = "dist-note warn";
    hideDepth((info && info.msg) || "深度图不可用");
    return;
  }
  for (const zone of zones) {
    const value = info[zone.toLowerCase()];
    const item = el["dist" + zone];
    const has = typeof value === "number" && isFinite(value);
    item.querySelector("strong").textContent = has ? value.toFixed(2) + " m" : "--";
    item.classList.toggle("near", has && value < 0.8);
  }
  el.distNote.textContent = "量程 " + info.min_m + "–" + info.max_m + " m · 仅供参考，不参与控制";
  el.distNote.className = "dist-note";
  if (!state.depthStreaming && Date.now() >= state.depthRetryAt) {
    el.depthImg.src = "/api/depth.mjpg?t=" + Date.now();
    state.depthStreaming = true;
  }
  el.depthImg.style.display = "block";
  el.depthHint.style.display = "none";
}

// ------------------------------------------------------------------ 控制
function velocity() {
  let x = state.joystick.x;
  let yaw = state.joystick.yaw;
  for (const key of state.pressed) {
    const dir = DIRS[key];
    if (dir) { x += dir[0]; yaw += dir[1]; }
  }
  const mag = Math.hypot(x, yaw);
  if (mag > 1) { x /= mag; yaw /= mag; }   // 斜向不加速
  return { x: +(x * state.speed).toFixed(3), yaw: +(yaw * state.speed).toFixed(3) };
}

function push() {
  const v = velocity();
  post({ type: "move", x: v.x, yaw: v.yaw });
}

function markDir(key, on) {
  const btn = document.querySelector('[data-dir="' + key + '"]');
  if (btn) btn.classList.toggle("active", on);
}

function press(key) {
  if (!DIRS[key] || state.pressed.has(key)) return;
  state.pressed.add(key);
  markDir(key, true);
  push();
}

function release(key) {
  if (!state.pressed.delete(key)) return;
  markDir(key, false);
  push();
}

function clearPressed() {
  for (const key of state.pressed) markDir(key, false);
  state.pressed.clear();
}

function clearMotion() {
  clearPressed();
  state.joystick = { x: 0, yaw: 0 };
}

function act(name) {
  return post({ type: "action", name: name });
}

function stopAll() {
  clearMotion();
  post({ type: "stop" });
}

// ------------------------------------------------------------------ 输入方式（方向键 / 摇杆）
function initJoystick() {
  if (stickManager || typeof nipplejs === "undefined") return;
  stickManager = nipplejs.create({
    zone: el.stickZone,
    mode: "static",
    position: { left: "50%", top: "50%" },
    color: "#3b82f6",
    size: 130,
  });
  // nipplejs 的 vector.y 向上为正、vector.x 向右为正；本项目 yaw>0 是左转
  stickManager.on("move", (evt, data) => {
    const mag = Math.min(data.force || 0, 1);
    state.joystick = { x: data.vector.y * mag, yaw: -data.vector.x * mag };
    push();
  });
  stickManager.on("end", () => {
    state.joystick = { x: 0, yaw: 0 };
    push();
  });
}

function setInputMode(mode, notify) {
  state.inputMode = mode === "stick" ? "stick" : "dpad";
  saveInputMode(state.inputMode);
  el.dpad.hidden = state.inputMode !== "dpad";
  el.stickZone.hidden = state.inputMode !== "stick";
  for (const btn of document.querySelectorAll("[data-input]")) {
    btn.classList.toggle("on", btn.dataset.input === state.inputMode);
  }
  state.joystick = { x: 0, yaw: 0 };
  if (state.inputMode === "stick") initJoystick();
  if (notify) push();
}

// 命令表：HTML 的 data-cmd 与键盘按键都映射到这里
const COMMANDS = Object.freeze({
  walk: () => act("WALK"),
  stand: () => act("FALLTOSTAND"),
  enable: () => {
    if (state.workmode !== 30) {
      toast("已使能中(workmode=" + state.workmode + ")，请勿重复使能");
      return;
    }
    act("START");
  },
  collect: () => { clearMotion(); act("STANDTOFALL"); },
  estop: () => {
    clearMotion();
    post({ type: "estop" });
    toast("急停：已停车并请求失能");
  },
  quit: () => {
    if (!confirm("确认退出机器人上的遥控服务？退出后网页就控制不了了。")) return;
    clearMotion();
    post({ type: "quit" });
  },
  stop: stopAll,
});

async function runAction(btn) {
  if (state.walking) { toast("行走中不能播放动作，请先停止行走"); return; }
  const name = btn.dataset.act;
  btn.classList.add("active");
  setTimeout(() => btn.classList.remove("active"), 240);
  const data = await act(name);
  if (data && data.ok) toast((ACTIONS[name] || name) + "：已下发");
}

// ------------------------------------------------------------------ 事件绑定
function bindPointer() {
  document.addEventListener("pointerdown", (ev) => {
    const btn = ev.target.closest && ev.target.closest("[data-dir]");
    if (!btn) return;
    ev.preventDefault();
    try { btn.setPointerCapture(ev.pointerId); } catch (err) { /* 忽略 */ }
    press(btn.dataset.dir);
  });

  const end = (ev) => {
    const btn = ev.target.closest && ev.target.closest("[data-dir]");
    if (!btn) return;
    ev.preventDefault();
    release(btn.dataset.dir);
  };
  document.addEventListener("pointerup", end);
  document.addEventListener("pointercancel", end);

  document.addEventListener("contextmenu", (ev) => {
    if (ev.target.closest && ev.target.closest("[data-dir]")) ev.preventDefault();
  });
}

function bindClicks() {
  document.addEventListener("click", (ev) => {
    const target = ev.target;
    const inputBtn = target.closest && target.closest("[data-input]");
    if (inputBtn) { setInputMode(inputBtn.dataset.input, true); return; }
    const cmdBtn = target.closest && target.closest("[data-cmd]");
    if (cmdBtn) {
      const run = COMMANDS[cmdBtn.dataset.cmd];
      if (run) run();
      return;
    }
    const actBtn = target.closest && target.closest("[data-act]");
    if (actBtn) runAction(actBtn);
  });
}

function bindKeyboard() {
  window.addEventListener("keydown", (ev) => {
    const raw = (ev.key || "").toLowerCase();
    const key = ALIAS[raw] || raw;
    if (ev.repeat) return;
    if (DIRS[key]) { ev.preventDefault(); press(key); return; }
    if (raw === " " || raw === "spacebar") { ev.preventDefault(); stopAll(); return; }
    const cmd = KEY_COMMANDS[key];
    if (cmd) { ev.preventDefault(); COMMANDS[cmd](); }
  });

  window.addEventListener("keyup", (ev) => {
    const raw = (ev.key || "").toLowerCase();
    const key = ALIAS[raw] || raw;
    if (DIRS[key]) { ev.preventDefault(); release(key); }
  });
}

function bindVisibility() {
  // 失焦 / 切到后台：立即停车，避免方向键或摇杆的「按住不放」残留
  const halt = () => {
    if (state.pressed.size || state.joystick.x || state.joystick.yaw) stopAll();
  };
  window.addEventListener("blur", halt);
  document.addEventListener("visibilitychange", () => { if (document.hidden) halt(); });
}

function bindControls() {
  el.speed.addEventListener("input", (ev) => {
    state.speed = parseFloat(ev.target.value);
    el.speedVal.textContent = state.speed.toFixed(2);
    push();
  });
  el.cam.addEventListener("error", () => {
    // 流被服务端断开时，等下一轮状态轮询再重试
    showCamProblem("画面中断，正在重试 ...", "重连中");
  });
  el.depthImg.addEventListener("error", () => {
    hideDepth("深度图中断，正在重试 ...");
  });
}

// ------------------------------------------------------------------ 轮询与启动
async function poll() {
  try {
    const data = await request("/api/status", { cache: "no-store" });
    // 后端重启过：旧页面的 MJPEG 连接和 JS 状态都是脏的，整页重载一次就干净了
    if (data.instance) {
      if (state.instanceId && data.instance !== state.instanceId) {
        location.reload();
        return;
      }
      state.instanceId = data.instance;
    }
    setLink(true);
    setMode(data.workmode);
    setWalking(!!data.walking);
    setCamera(data.camera);
    setVision(data.vision);
  } catch (err) {
    setLink(false);
  }
  setTimeout(poll, POLL_MS);
}

function init() {
  el.speedVal.textContent = state.speed.toFixed(2);
  setInputMode(state.inputMode, false);   // 恢复上次的输入方式，启动时不发指令
  bindPointer();
  bindClicks();
  bindKeyboard();
  bindVisibility();
  bindControls();
  setInterval(() => { if (!document.hidden) push(); }, HEARTBEAT_MS);
  poll();
}

init();
