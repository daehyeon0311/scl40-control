"use strict";

const $ = (id) => document.getElementById(id);

const HISTORY_LIMIT = 2000;
const DASH_NUM = "–.––––";

const RUN_DEFAULTS = {
  mode: "lcp_jet",
  fill_flow: "0.3000",
  fill_pressure: "2.0",
  run_flow: "0.0460",
  run_pressure: "1.5",
  pressure_limit: "9.0",
  settle_seconds: "15",
  stage_timeout: "1800",
};

const RUN_FIELDS = {
  mode: "runMode",
  fill_flow: "runFillFlow",
  fill_pressure: "runFillPressure",
  run_flow: "runFlow",
  run_pressure: "runPressure",
  pressure_limit: "runLimit",
  settle_seconds: "runSettle",
  stage_timeout: "runTimeout",
};

const RUN_EVENT_LEVEL = {
  fill_detected: "ok",
  end_detected: "ok",
  stopped: "ok",
  watching: "info",
  fill_started: "warn",
  watch_started: "warn",
  aborted: "warn",
  pump_off: "warn",
  limit: "error",
  timeout: "error",
  stop_failed: "error",
  flow_switch_failed: "error",
};

const state = {
  host: "—",
  controlEnabled: false,
  loggedIn: false,
  simulated: false,
  pollMs: 5000,
  rangeMin: 5,
  history: [],
  accessPin: sessionStorage.getItem("scl40AccessPin") || "",
  pollTimer: null,
  trendSince: 0,
  limits: {},
  run: { stage: "idle", active: false },
  seenRunEvents: new Set(),
};

/* ---------- helpers ---------- */

const TIME_FORMAT = { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" };
const clock = (date) => date.toLocaleTimeString("ko-KR", TIME_FORMAT);

function num(value, digits) {
  const parsed = Number(value);
  return value === null || value === undefined || value === "" || !Number.isFinite(parsed)
    ? null
    : parsed.toFixed(digits);
}

function setText(id, value, fallback = "—") {
  $(id).textContent = value === null || value === undefined || value === "" ? fallback : value;
}

function logLine(level, message, at = null) {
  const row = document.createElement("div");
  row.className = "log-line";

  const time = document.createElement("time");
  time.textContent = clock(at ? new Date(at) : new Date());
  const lvl = document.createElement("span");
  lvl.className = `lvl ${level}`;
  lvl.textContent = level.toUpperCase();
  const msg = document.createElement("span");
  msg.className = "msg";
  msg.textContent = message;

  row.append(time, lvl, msg);
  const log = $("activity");
  log.prepend(row);
  while (log.children.length > 200) log.lastElementChild.remove();
}

function setStatusMessage(message, alert = false) {
  const node = $("statusMessage");
  node.textContent = message;
  node.classList.toggle("alert", alert);
}

/* ---------- transport ---------- */

async function api(path, options = {}, allowPinPrompt = true) {
  const headers = { ...(options.headers || {}) };
  if (state.accessPin) headers["X-SCL40-PIN"] = state.accessPin;
  const response = await fetch(path, { cache: "no-store", ...options, headers });
  const data = await response.json();
  if (response.status === 401 && data.pin_required && allowPinPrompt) {
    const entered = prompt("대시보드 접속 PIN을 입력하세요.");
    if (entered === null) throw new Error("대시보드 PIN 입력이 취소되었습니다.");
    state.accessPin = entered.trim();
    sessionStorage.setItem("scl40AccessPin", state.accessPin);
    return api(path, options, false);
  }
  if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

/* ---------- trend chart ---------- */

/* Plot geometry follows the rendered size of the <svg>, so labels keep their
   pixel size instead of being stretched with the box. */
function chartBox(svg) {
  const width = Math.max(420, Math.round(svg.clientWidth || 960));
  const height = Math.max(170, Math.round(svg.clientHeight || 300));
  // Pressure scale sits on the right, so the left margin only needs padding.
  return { width, height, left: 14, right: width - 56, top: 26, bottom: height - 26 };
}

/* Pick a round grid step first, then take four of them as the axis maximum,
   so every gridline label stays a clean number. */
function axisMax(value, minimum) {
  const target = Math.max(value, minimum, Number.MIN_VALUE) / 4;
  const exponent = Math.pow(10, Math.floor(Math.log10(target)));
  const scaled = target / exponent;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5 : scaled <= 5 ? 5 : 10;
  return step * exponent * 4;
}

function svgEl(tag, attrs, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

function buildSegments(points, pick, xOf, yOf, maxGapMs) {
  const segments = [];
  let current = [];
  let previousTime = null;
  for (const point of points) {
    const value = pick(point);
    const broken = value === null || (previousTime !== null && point.t - previousTime > maxGapMs);
    if (broken) {
      if (current.length > 1) segments.push(current);
      current = [];
      if (value === null) {
        previousTime = null;
        continue;
      }
    }
    current.push(`${xOf(point.t).toFixed(1)},${yOf(value).toFixed(1)}`);
    previousTime = point.t;
  }
  if (current.length > 1) segments.push(current);
  return segments.map((seg) => `M${seg.join("L")}`);
}

function renderChart() {
  const svg = $("chart");
  const box = chartBox(svg);
  const now = Date.now();
  const spanMs = state.rangeMin * 60000;
  const from = now - spanMs;
  const points = state.history.filter((p) => p.t >= from);

  $("chartEmpty").hidden = points.length >= 2;
  svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
  svg.replaceChildren();

  const maxPressure = axisMax(Math.max(0, ...points.map((p) => p.p ?? 0)), 2);

  const xOf = (t) => box.left + ((t - from) / spanMs) * (box.right - box.left);
  const yOfPressure = (v) => box.bottom - (v / maxPressure) * (box.bottom - box.top);

  svg.append(svgEl("rect", {
    x: box.left, y: box.top,
    width: box.right - box.left, height: box.bottom - box.top,
    fill: "#ffffff", stroke: "#c8cfd8", "stroke-width": 1,
  }));

  // Institute wordmark, watermarked behind the grid and the trace.
  const markWidth = Math.min((box.right - box.left) * 0.26, 250);
  const markHeight = markWidth * (392 / 520);
  svg.append(svgEl("image", {
    href: "ibs_watermark.png",
    x: (box.left + box.right) / 2 - markWidth / 2,
    y: (box.top + box.bottom) / 2 - markHeight / 2,
    width: markWidth, height: markHeight,
    opacity: 0.09, preserveAspectRatio: "xMidYMid meet",
  }));

  for (let i = 0; i <= 4; i += 1) {
    const y = box.top + ((box.bottom - box.top) / 4) * i;
    if (i > 0 && i < 4) {
      svg.append(svgEl("line", {
        x1: box.left, y1: y, x2: box.right, y2: y, stroke: "#eaedf1", "stroke-width": 1,
      }));
    }
    svg.append(svgEl("text", {
      x: box.right + 8, y: y + 4,
      "font-family": "Consolas, monospace", "font-size": 11, fill: "#0f5fa8",
    }, (maxPressure - (maxPressure / 4) * i).toFixed(1)));
  }

  const tickMinutes = state.rangeMin === 5 ? 1 : state.rangeMin === 15 ? 5 : 15;
  const tickMs = tickMinutes * 60000;
  for (let t = Math.ceil(from / tickMs) * tickMs; t <= now; t += tickMs) {
    const x = xOf(t);
    svg.append(svgEl("line", {
      x1: x, y1: box.top, x2: x, y2: box.bottom, stroke: "#eaedf1", "stroke-width": 1,
    }));
    if (x <= box.right - 18) {
      svg.append(svgEl("text", {
        x, y: box.bottom + 17, "text-anchor": "middle",
        "font-family": "Consolas, monospace", "font-size": 10, fill: "#7f8a98",
      }, new Date(t).toLocaleTimeString("ko-KR", { hour12: false, hour: "2-digit", minute: "2-digit" })));
    }
  }

  svg.append(svgEl("text", {
    x: box.right + 8, y: box.top - 9,
    "font-family": "Consolas, monospace", "font-size": 10, fill: "#0f5fa8",
  }, "MPa"));

  // Flow is an operator setpoint, not a measured trace, so it is not plotted.
  // Only the moments it changed are marked, to place the prime/run switch.
  let previousTarget = null;
  let lastLabelX = -Infinity;
  for (const point of points) {
    if (point.target === null || point.target === undefined) continue;
    if (previousTarget !== null && Math.abs(point.target - previousTarget) > 1e-6) {
      const x = xOf(point.t);
      svg.append(svgEl("line", {
        x1: x, y1: box.top, x2: x, y2: box.bottom,
        stroke: "#b4690e", "stroke-width": 1, "stroke-dasharray": "3 3",
      }));
      if (x - lastLabelX > 96) {
        const nearEdge = x > box.right - 96;
        svg.append(svgEl("text", {
          x: nearEdge ? x - 4 : x + 4, y: box.top + 12,
          "text-anchor": nearEdge ? "end" : "start",
          "font-family": "Consolas, monospace", "font-size": 10, fill: "#b4690e",
        }, `${point.target.toFixed(4)} mL/min`));
        lastLabelX = x;
      }
    }
    previousTarget = point.target;
  }

  const maxGap = Math.max(state.pollMs, 2000) * 3;
  for (const d of buildSegments(points, (p) => p.p, xOf, yOfPressure, maxGap)) {
    svg.append(svgEl("path", {
      d, fill: "none", stroke: "#0f5fa8", "stroke-width": 1.6,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
  }

  // The pressure that ends the current stage.
  const run = state.run || {};
  if (run.active && run.threshold !== null && run.threshold !== undefined && run.threshold <= maxPressure) {
    const y = yOfPressure(run.threshold);
    svg.append(svgEl("line", {
      x1: box.left, y1: y, x2: box.right, y2: y,
      stroke: "#a92b26", "stroke-width": 1, "stroke-dasharray": "6 4",
    }));
    svg.append(svgEl("text", {
      x: box.left + 6, y: y - 5,
      "font-family": "Consolas, monospace", "font-size": 10, fill: "#a92b26",
    }, `판단 압력 ${run.threshold.toFixed(2)} MPa`));
  }

  const last = points[points.length - 1];
  if (last && last.p !== null) {
    svg.append(svgEl("circle", { cx: xOf(last.t), cy: yOfPressure(last.p), r: 2.5, fill: "#0f5fa8" }));
  }
}

/* Trend history lives on the server, so it survives reloads and every client
   sees the same trace. Each poll pulls only the samples it has not seen. */
async function fetchTrend() {
  const data = await api(`/api/trend?since=${state.trendSince}`);
  for (const [t, p, f, target] of data.samples || []) {
    state.history.push({ t: t * 1000, p, f, target });
  }
  state.trendSince = data.until || state.trendSince;
  if (state.history.length > HISTORY_LIMIT) {
    state.history.splice(0, state.history.length - HISTORY_LIMIT);
  }
}

/* ---------- sudden-change indicators ---------- */

// A jump of this much within the look-back window blinks the readout.
const ALERT = {
  windowMs: 6000,
  flashMs: 2500,
  pressureRise: 0.2,   // MPa
  flowRise: 0.005,     // mL/min
};

const flashTimers = {};

function flashReadout(channel) {
  if (flashTimers[channel]) return;
  const node = document.querySelector(`.readout[data-channel="${channel}"]`);
  if (!node) return;
  node.classList.add("alert-flash");
  flashTimers[channel] = setTimeout(() => {
    node.classList.remove("alert-flash");
    flashTimers[channel] = null;
  }, ALERT.flashMs);
}

function checkSuddenRise() {
  const now = Date.now();
  const recent = state.history.filter((point) => point.t >= now - ALERT.windowMs);
  if (recent.length < 2) return;

  const rise = (pick) => {
    const values = recent.map(pick).filter((v) => v !== null && v !== undefined);
    return values.length < 2 ? null : values[values.length - 1] - Math.min(...values);
  };

  const pressureRise = rise((point) => point.p);
  if (pressureRise !== null && pressureRise >= ALERT.pressureRise) flashReadout("pressure");

  const flowRise = rise((point) => point.f);
  const targets = recent.map((point) => point.target).filter((v) => v !== null && v !== undefined);
  const targetChanged = targets.length > 1 && Math.abs(targets[targets.length - 1] - targets[0]) > 1e-6;
  if ((flowRise !== null && flowRise >= ALERT.flowRise) || targetChanged) flashReadout("flow");
}

/* ---------- method parameter editor ---------- */

function paramInputs() {
  return Array.from(document.querySelectorAll(".param-input"));
}

function markDirty(input) {
  input.dataset.dirty = input.value !== "" && input.value !== input.dataset.device ? "1" : "0";
}

function syncParamInputs(method) {
  for (const input of paramInputs()) {
    const value = method[input.dataset.param];
    const device = value === null || value === undefined ? "" : String(value);
    input.dataset.device = device;
    if (input.dataset.dirty !== "1" && document.activeElement !== input) input.value = device;
  }
}

function collectParamChanges() {
  const changes = {};
  for (const input of paramInputs()) {
    if (input.dataset.dirty === "1") changes[input.dataset.param] = input.value;
  }
  return changes;
}

function revertParams() {
  for (const input of paramInputs()) {
    input.value = input.dataset.device || "";
    input.dataset.dirty = "0";
  }
  updateParamButtons();
}

function updateParamButtons() {
  const dirty = Object.keys(collectParamChanges()).length > 0;
  const ready = state.controlEnabled && state.loggedIn && !state.run.active;
  $("applyBtn").disabled = !dirty || !ready;
  $("revertBtn").disabled = !dirty;
  for (const input of paramInputs()) input.disabled = !ready;
}

async function applyParams() {
  const params = collectParamChanges();
  if (!Object.keys(params).length) return;
  $("applyBtn").disabled = true;
  try {
    const result = await api("/api/control/method", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params, confirmation: "SET_METHOD" }),
    });
    const changed = Object.values(result.applied).filter((item) => item.changed);
    for (const item of changed) {
      logLine("ok", `${item.label} ${item.previous} → ${item.value} ${item.unit} (재조회 확인)`);
    }
    for (const input of paramInputs()) input.dataset.dirty = "0";
  } catch (error) {
    logLine("error", error.message);
  }
  await refresh(false);
}

/* ---------- LCP jet run ---------- */

function runFieldValues() {
  const values = {};
  for (const [key, id] of Object.entries(RUN_FIELDS)) values[key] = $(id).value.trim();
  return values;
}

function applyRunDefaults(config) {
  for (const [key, id] of Object.entries(RUN_FIELDS)) {
    const node = $(id);
    const supplied = config && config[key] !== undefined && config[key] !== null ? String(config[key]) : "";
    if (node.value === "") node.value = supplied || RUN_DEFAULTS[key];
  }
}

function updateRunMode() {
  const lcp = $("runMode").value === "lcp_jet";
  for (const node of document.querySelectorAll('[data-mode="lcp_jet"]')) node.hidden = !lcp;
}

function renderRun(run, monitorPressure) {
  state.run = run;
  const stage = run.stage || "idle";
  const active = !!run.active;

  $("runStageCard").dataset.stage = stage;
  $("runStageChip").dataset.stage = stage;
  $("runStageChip").textContent = run.stage_label || "대기";
  setText("runStageText", run.stage_label, "대기");
  setText("runStageNote", run.detail, "런이 실행 중이 아닙니다.");
  setText("runDetail", run.detail, "시작 전");
  setText("statusRun", `RUN ${stage}`);

  const waiting = active && !run.watching;
  $("runSettleBar").hidden = !waiting;
  if (waiting) $("runSettleFill").style.width = "100%";

  const mpa = (value) => (value === null || value === undefined ? null : `${value.toFixed(2)} MPa`);
  setText("runThreshold", mpa(run.threshold));
  setText("runPressureNow", mpa(run.pressure ?? monitorPressure));
  setText("runPeak", mpa(run.peak));
  setText("runElapsed", active ? `${Math.round(run.elapsed_seconds)} s` : null);

  const ready = state.controlEnabled && state.loggedIn;
  $("runStartBtn").disabled = !ready || active;
  $("runAbortBtn").disabled = !ready || !active;
  for (const id of Object.values(RUN_FIELDS)) $(id).disabled = active;

  // Run events come from the server; fold them into the single event log.
  for (const event of run.events || []) {
    const key = `${event.timestamp}|${event.message}`;
    if (state.seenRunEvents.has(key)) continue;
    state.seenRunEvents.add(key);
    logLine(RUN_EVENT_LEVEL[event.kind] || "info", event.message, event.timestamp);
  }

  applyRunDefaults(run.config);
  updateRunMode();
}

async function startRun() {
  const values = runFieldValues();
  const lcp = values.mode === "lcp_jet";
  const summary = lcp
    ? `${values.fill_flow} mL/min으로 펌프를 켜고 물을 채웁니다.\n`
      + `압력이 ${values.fill_pressure} MPa 이상이 되면 ${values.run_flow} mL/min으로 바꾸고,\n`
      + `다시 ${values.run_pressure} MPa 이상이 되면 펌프를 정지합니다.`
    : `현재 운전 상태를 감시하다가 압력이 ${values.run_pressure} MPa 이상이 되면 펌프를 정지합니다.`;
  if (!confirm(`${summary}\n\n계속할까요?`)) return;

  $("runStartBtn").disabled = true;
  try {
    const result = await api("/api/run/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...values, confirmation: "START_RUN" }),
    });
    logLine("warn", `LCP JET RUN 시작 · ${result.run.stage_label}`);
  } catch (error) {
    logLine("error", error.message);
  }
  await refresh(false);
}

async function abortRun() {
  if (!confirm("진행 중인 런을 중단하고 펌프를 정지합니다. 계속할까요?")) return;
  try {
    const result = await api("/api/run/abort", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "사용자 중단" }),
    });
    logLine("warn", `런 중단 · ${result.run.stage_label}`);
  } catch (error) {
    logLine("error", error.message);
  }
  await refresh(false);
}

/* ---------- rendering ---------- */

function render(data) {
  state.host = data.host;
  state.controlEnabled = !!data.control_enabled;
  state.simulated = !!data.simulated;
  // Poll faster while a run is in progress so the trend and stage stay live.
  state.pollMs = data.run?.active || state.simulated ? 2000 : 5000;

  const controller = data.config?.controller || {};
  const pump = data.config?.pumps?.[0] || {};
  const summary = data.summary || {};
  const method = data.method || {};
  const monitor = data.monitor || {};
  const auth = data.auth || {};
  const activity = data.control_activity || {};
  state.loggedIn = !!auth.logged_in;

  $("linkState").dataset.state = "online";
  $("linkState").querySelector("b").textContent = "ONLINE";
  setText("latency", `${data.latency_ms} ms`);
  setText("sessionUser", state.loggedIn ? auth.user_id : "NONE", "NONE");

  setText("controllerModel", controller.model, "SCL-40");
  setText("pumpModel", pump.model, "NO PUMP");
  setText("pumpUnit", pump.unit_id ? `Unit ${pump.unit_id}` : "", "");

  const monitorLive = monitor.available !== false;
  const pressure = num(monitor.pressure, 2);
  const flow = num(monitor.flow, 4);
  const target = num(monitor.target_flow ?? method.flow, 4);

  setText("pressure", pressure, "–.––");
  setText("flow", flow, DASH_NUM);
  setText("targetFlow", target, DASH_NUM);
  setText("pressureNote", method.pmax ? `limit ${num(method.pmax, 1)} MPa` : "limit —");
  setText("flowNote", monitorLive ? "measured" : "no monitor session");
  setText("targetNote", `method ${method.number ?? "—"}`);

  document.querySelector('.readout[data-channel="pressure"]').classList.toggle("stale", pressure === null);
  document.querySelector('.readout[data-channel="flow"]').classList.toggle("stale", flow === null);
  document.querySelector('.readout[data-channel="target"]').classList.toggle("stale", target === null);

  const pumpRunning = monitor.pump_on;
  const pumpNode = $("pumpState");
  pumpNode.dataset.state = pumpRunning === true ? "running" : pumpRunning === false ? "stopped" : "unknown";
  setText("pumpStateText", pumpRunning === true ? "RUNNING" : pumpRunning === false ? "STOPPED" : "UNKNOWN");
  setText("pumpStateNote", `OpState ${monitor.op_state_code || "—"}`);

  setText("methodAlias", method.alias ? `${method.alias} · No ${method.number ?? "—"}` : `No ${method.number ?? "—"}`);
  setText("methodFlow", num(method.flow, 4));
  setText("methodTflow", num(method.tflow, 4));
  setText("methodPmax", num(method.pmax, 1));
  setText("methodPmin", num(method.pmin, 1));

  state.limits = data.limits || state.limits;
  const hint = Object.entries(state.limits)
    .map(([, spec]) => `${spec.label} ${spec.min}–${spec.max} ${spec.unit}`)
    .join(" · ");
  setText("paramHint", hint || "—");
  syncParamInputs(method);
  renderRun(data.run || { stage: "idle", active: false }, monitor.pressure === null ? null : Number(monitor.pressure));
  updateParamButtons();

  setText("controllerDetail", `${controller.model || "—"} · fw ${controller.version || "—"} · addr ${controller.address || "—"}`);
  setText("pumpDetail", pump.model ? `${pump.model} · Unit ${pump.unit_id || "—"} · fw ${pump.version || "—"} · addr ${pump.address || "—"}` : "not detected");
  setText("hostName", summary.host_name);
  setText("systemState", summary.system_state_code);
  setText("loginState", state.loggedIn ? `web session · ${auth.user_id}` : `device code ${summary.login_state_code || "—"}`);
  setText("lastOperator", activity.operator_ip);

  const commandReady = state.controlEnabled && state.loggedIn && !activity.busy;
  const runActive = !!data.run?.active;
  $("startBtn").disabled = !commandReady || runActive;
  $("stopBtn").disabled = !commandReady;
  $("setFlowBtn").disabled = !commandReady || runActive;
  $("loginBtn").disabled = state.loggedIn;
  $("logoutBtn").disabled = !state.loggedIn;
  $("userId").disabled = state.loggedIn;
  $("password").disabled = state.loggedIn;

  const mode = state.simulated
    ? { text: "SIMULATOR", key: "sim" }
    : !state.controlEnabled
      ? { text: "READ ONLY", key: "readonly" }
      : state.loggedIn
        ? { text: "CONTROL READY", key: "ready" }
        : { text: "LOGIN REQUIRED", key: "control" };
  $("modeBadge").textContent = mode.text;
  $("modeBadge").dataset.mode = mode.key;
  setText("statusMode", mode.text);

  $("simBanner").hidden = !state.simulated;
  setText("statusHost", `${data.host}`);
  setText("statusCommand", activity.action
    ? `${activity.action} · ${activity.busy ? "BUSY" : (activity.result || "").toUpperCase()} · ${activity.timestamp ? clock(new Date(activity.timestamp)) : "—"}`
    : "no command issued");
  setText("lastUpdated", `updated ${clock(new Date(data.timestamp))}`);

  setText("railHost", data.host);
  setText("railPoll", `${(state.pollMs / 1000).toFixed(0)} s`);
  setText("railSamples", String(state.history.length));
  checkSuddenRise();
  renderChart();

  if ($("flowInput").value === "" && method.flow) $("flowInput").value = num(method.flow, 4);
}

function renderOffline(message) {
  $("linkState").dataset.state = "offline";
  $("linkState").querySelector("b").textContent = "OFFLINE";
  setText("latency", "—");
  document.querySelectorAll(".readout").forEach((node) => node.classList.add("stale"));
  setStatusMessage(message, true);
}

/* ---------- polling ---------- */

async function refresh(manual = false) {
  try {
    const data = await api("/api/snapshot");
    await fetchTrend();
    render(data);
    setStatusMessage(data.monitor?.available === false ? (data.monitor.reason || "") : "");
    if (manual) logLine("ok", `상태 갱신 완료 (${data.latency_ms} ms)`);
  } catch (error) {
    renderOffline(error.message);
    logLine("error", error.message);
  } finally {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(() => refresh(false), state.pollMs);
  }
}

/* ---------- commands ---------- */

async function login() {
  const userId = $("userId").value.trim();
  const password = $("password").value;
  if (!userId) {
    logLine("error", "SCL-40 사용자 ID를 입력하세요.");
    return;
  }
  $("loginBtn").disabled = true;
  try {
    await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, password }),
    });
    $("password").value = "";
    logLine("ok", `SCL-40 로그인 성공 · ${userId}`);
    await refresh(false);
  } catch (error) {
    $("loginBtn").disabled = false;
    logLine("error", error.message);
  }
}

async function logout() {
  try {
    await api("/api/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    logLine("ok", "SCL-40 로그아웃 완료");
  } catch (error) {
    logLine("error", error.message);
  }
  await refresh(false);
}

async function setFlow() {
  const value = Number($("flowInput").value);
  if (!Number.isFinite(value) || value < 0 || value > 1) {
    logLine("error", "유량은 0.0000~1.0000 mL/min 범위로 입력하세요.");
    return;
  }
  const formatted = value.toFixed(4);
  $("setFlowBtn").disabled = true;
  try {
    const result = await api("/api/control/method", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params: { flow: formatted }, confirmation: "SET_METHOD" }),
    });
    const applied = result.applied.flow;
    logLine("ok", `유량 변경 확인 (readback): ${applied.previous} → ${applied.value} mL/min`);
  } catch (error) {
    logLine("error", error.message);
  }
  await refresh(false);
}

async function command(action) {
  if (action === "start" && !confirm("현재 메소드의 목표 유량으로 펌프를 START 합니다. 계속할까요?")) return;
  try {
    const data = await api(`/api/control/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmation: action.toUpperCase() }),
    });
    logLine("warn", `${data.action} 명령 전송 · 장비 응답 ${data.response_root}`);
  } catch (error) {
    logLine("error", error.message);
  }
  setTimeout(() => refresh(false), 400);
}

/* ---------- wiring ---------- */

$("refreshBtn").addEventListener("click", () => refresh(true));
$("loginBtn").addEventListener("click", login);
$("logoutBtn").addEventListener("click", logout);
$("setFlowBtn").addEventListener("click", setFlow);
$("startBtn").addEventListener("click", () => command("start"));
$("stopBtn").addEventListener("click", () => command("stop"));
$("userId").addEventListener("keydown", (event) => { if (event.key === "Enter") $("password").focus(); });
$("password").addEventListener("keydown", (event) => { if (event.key === "Enter") login(); });
$("flowInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !$("setFlowBtn").disabled) setFlow();
});

$("applyBtn").addEventListener("click", applyParams);
$("revertBtn").addEventListener("click", revertParams);
$("runStartBtn").addEventListener("click", startRun);
$("runAbortBtn").addEventListener("click", abortRun);
$("runMode").addEventListener("change", updateRunMode);

for (const input of paramInputs()) {
  input.addEventListener("input", () => {
    markDirty(input);
    updateParamButtons();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !$("applyBtn").disabled) applyParams();
    if (event.key === "Escape") revertParams();
  });
}

$("rangeGroup").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-min]");
  if (!button) return;
  state.rangeMin = Number(button.dataset.min);
  for (const node of $("rangeGroup").children) node.classList.toggle("on", node === button);
  setText("railWindow", `${state.rangeMin} min`);
  renderChart();
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderChart, 120);
});

setInterval(() => { $("clock").textContent = clock(new Date()); }, 1000);
$("clock").textContent = clock(new Date());
applyRunDefaults(null);
updateRunMode();
logLine("info", "콘솔 시작 · SCL-40 상태 조회");
renderChart();
refresh(true);
