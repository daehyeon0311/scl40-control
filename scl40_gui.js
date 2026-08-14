"use strict";

/* SCL-40 LCP jet console.
   Three views share one persistent frame: the rail (measurements), the command
   bar (manual control) and the alarm bar are always on screen, while setup and
   history move out of the way of the running experiment. */

const $ = (id) => document.getElementById(id);

const HISTORY_LIMIT = 4000;
// Kept short on purpose: the log is for what just happened, not an archive.
// The server-side log file keeps the full record.
const LOG_LINES = 200;
const RECENT_LOG_LINES = 8;
const TIME_FORMAT = { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" };

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

// Run events that must be acknowledged by an operator, not just logged.
const RUN_EVENT_ALARM = {
  limit: "압력 상한",
  timeout: "시간 초과",
  stop_failed: "정지 실패",
  flow_switch_failed: "유량 전환 실패",
  pump_off: "외부 정지",
};

// A jump of this much within the look-back window blinks the readout.
const ALERT = { windowMs: 6000, flashMs: 2500, pressureRise: 0.2, flowRise: 0.005 };

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
  previousStage: "idle",
  alarmedRun: null,
  seenRunEvents: new Set(),
  runActiveAtFailure: false,
};

/* ---------- helpers ---------- */

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

const mpa = (value) => (value === null || value === undefined ? null : `${Number(value).toFixed(2)} MPa`);

function logLine(level, message, at = null) {
  for (const [containerId, limit] of [["activity", LOG_LINES], ["recentLog", RECENT_LOG_LINES]]) {
    const container = $(containerId);
    if (!container) continue;
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
    container.prepend(row);
    while (container.children.length > limit) container.lastElementChild.remove();
  }
}

function setStatusMessage(message, alert = false) {
  const node = $("statusMessage");
  node.textContent = message;
  node.classList.toggle("alert", alert);
}

/* ---------- alarms ---------- */

/* Blinking alone is not enough: anything an operator must know about while
   away from the screen stays on the alarm bar until it is acknowledged. */
const alarms = new Map();

function raiseAlarm(id, level, kind, message, sticky = true) {
  if (alarms.has(id)) return;
  alarms.set(id, { id, level, kind, message, at: new Date(), sticky });
  renderAlarms();
}

function clearAlarm(id, onlyIfTransient = false) {
  const alarm = alarms.get(id);
  if (!alarm) return;
  if (onlyIfTransient && alarm.sticky) return;
  alarms.delete(id);
  renderAlarms();
}

function renderAlarms() {
  const bar = $("alarmBar");
  bar.replaceChildren();
  bar.hidden = alarms.size === 0;

  for (const alarm of [...alarms.values()].reverse()) {
    const row = document.createElement("div");
    row.className = "alarm-row";
    row.dataset.level = alarm.level;

    const time = document.createElement("time");
    time.textContent = clock(alarm.at);
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = alarm.kind;
    const message = document.createElement("span");
    message.className = "msg";
    message.textContent = alarm.message;
    const ack = document.createElement("button");
    ack.type = "button";
    ack.textContent = "확인";
    ack.addEventListener("click", () => {
      alarms.delete(alarm.id);
      renderAlarms();
    });

    row.append(time, kind, message, ack);
    bar.append(row);
  }
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
    fill: "#ffffff", stroke: "#c9cbcf", "stroke-width": 1,
  }));

  // Institute wordmark, watermarked behind the grid and the trace.
  const markWidth = Math.min((box.right - box.left) * 0.24, 230);
  const markHeight = markWidth * (392 / 520);
  svg.append(svgEl("image", {
    href: "ibs_watermark.png",
    x: (box.left + box.right) / 2 - markWidth / 2,
    y: (box.top + box.bottom) / 2 - markHeight / 2,
    width: markWidth, height: markHeight,
    opacity: 0.08, preserveAspectRatio: "xMidYMid meet",
  }));

  for (let i = 0; i <= 4; i += 1) {
    const y = box.top + ((box.bottom - box.top) / 4) * i;
    if (i > 0 && i < 4) {
      svg.append(svgEl("line", {
        x1: box.left, y1: y, x2: box.right, y2: y, stroke: "#e9eaec", "stroke-width": 1,
      }));
    }
    svg.append(svgEl("text", {
      x: box.right + 8, y: y + 4,
      "font-family": "Consolas, monospace", "font-size": 11, fill: "#4c5054",
    }, (maxPressure - (maxPressure / 4) * i).toFixed(1)));
  }

  const tickMinutes = state.rangeMin === 5 ? 1 : state.rangeMin === 15 ? 5 : 15;
  const tickMs = tickMinutes * 60000;
  for (let t = Math.ceil(from / tickMs) * tickMs; t <= now; t += tickMs) {
    const x = xOf(t);
    svg.append(svgEl("line", {
      x1: x, y1: box.top, x2: x, y2: box.bottom, stroke: "#e9eaec", "stroke-width": 1,
    }));
    if (x <= box.right - 18) {
      svg.append(svgEl("text", {
        x, y: box.bottom + 17, "text-anchor": "middle",
        "font-family": "Consolas, monospace", "font-size": 10, fill: "#83878c",
      }, new Date(t).toLocaleTimeString("ko-KR", { hour12: false, hour: "2-digit", minute: "2-digit" })));
    }
  }

  svg.append(svgEl("text", {
    x: box.right + 8, y: box.top - 9,
    "font-family": "Consolas, monospace", "font-size": 10, fill: "#4c5054",
  }, "MPa"));

  // Flow is an operator setpoint, not a measured trace, so it is not plotted.
  // Only the moments it changed are marked, to place the fill/run switch.
  let previousTarget = null;
  let lastLabelX = -Infinity;
  for (const point of points) {
    if (point.target === null || point.target === undefined) continue;
    if (previousTarget !== null && Math.abs(point.target - previousTarget) > 1e-6) {
      const x = xOf(point.t);
      svg.append(svgEl("line", {
        x1: x, y1: box.top, x2: x, y2: box.bottom,
        stroke: "#83878c", "stroke-width": 1, "stroke-dasharray": "3 3",
      }));
      if (x - lastLabelX > 96) {
        const nearEdge = x > box.right - 96;
        svg.append(svgEl("text", {
          x: nearEdge ? x - 4 : x + 4, y: box.top + 12,
          "text-anchor": nearEdge ? "end" : "start",
          "font-family": "Consolas, monospace", "font-size": 10, fill: "#83878c",
        }, `${point.target.toFixed(4)} mL/min`));
        lastLabelX = x;
      }
    }
    previousTarget = point.target;
  }

  // The pressure that ends the current stage.
  const run = state.run || {};
  if (run.active && run.threshold !== null && run.threshold !== undefined && run.threshold <= maxPressure) {
    const y = yOfPressure(run.threshold);
    svg.append(svgEl("line", {
      x1: box.left, y1: y, x2: box.right, y2: y,
      stroke: "#c81000", "stroke-width": 1, "stroke-dasharray": "6 4",
    }));
    svg.append(svgEl("text", {
      x: box.left + 6, y: y - 5,
      "font-family": "Consolas, monospace", "font-size": 10, fill: "#c81000",
    }, `판단 압력 ${run.threshold.toFixed(2)} MPa`));
  }

  const maxGap = Math.max(state.pollMs, 2000) * 3;
  for (const d of buildSegments(points, (p) => p.p, xOf, yOfPressure, maxGap)) {
    svg.append(svgEl("path", {
      d, fill: "none", stroke: "#22262b", "stroke-width": 1.6,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
  }

  const last = points[points.length - 1];
  if (last && last.p !== null) {
    svg.append(svgEl("circle", { cx: xOf(last.t), cy: yOfPressure(last.p), r: 2.5, fill: "#22262b" }));
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

const flashTimers = {};

function flashCard(node, key) {
  if (!node || flashTimers[key]) return;
  node.classList.add("alert-flash");
  flashTimers[key] = setTimeout(() => {
    node.classList.remove("alert-flash");
    flashTimers[key] = null;
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
  if (pressureRise !== null && pressureRise >= ALERT.pressureRise) {
    flashCard($("pressureCard"), "pressure");
  }

  const flowRise = rise((point) => point.f);
  const targets = recent.map((point) => point.target).filter((v) => v !== null && v !== undefined);
  const targetChanged = targets.length > 1 && Math.abs(targets[targets.length - 1] - targets[0]) > 1e-6;
  if ((flowRise !== null && flowRise >= ALERT.flowRise) || targetChanged) {
    flashCard(document.querySelector('.readout[data-channel="flow"]'), "flow");
  }
}

/* ---------- flow schematic ---------- */

function renderSchematic(monitor, run, method) {
  const running = monitor.pump_on === true;
  const stage = run.stage || "idle";
  const pressure = monitor.pressure === null || monitor.pressure === undefined ? null : Number(monitor.pressure);

  for (const id of ["pipeA", "pipeB", "pipeC", "pipeD"]) {
    $(id).classList.toggle("active", running);
  }
  $("nodePump").classList.toggle("active", running);
  $("nodeJet").classList.toggle("active", running && stage !== "fill");

  // Highlight the part of the cartridge the current stage is acting on.
  $("nodeReservoir").classList.toggle("stage", stage === "fill");
  $("nodeCartridge").classList.toggle("stage", stage === "run");
  $("nodeCartridge").classList.toggle("active", running);

  const near = run.active && run.threshold !== null && run.threshold !== undefined
    && pressure !== null && pressure >= run.threshold * 0.85;
  $("nodeGauge").classList.toggle("alarm", !!near);

  setText("schFlow", num(monitor.flow ?? method.flow, 4) ? `${num(monitor.flow ?? method.flow, 4)} mL/min` : null);
  setText("schPressure", pressure === null ? null : `${pressure.toFixed(2)} MPa`);

  const notes = {
    fill: "물 채우는 중 — 저장조를 채우고 있습니다",
    run: "시료 압출 중",
  };
  setText("schematicNote", notes[stage] || (running ? "펌프 운전 중" : "정지"), "정지");
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
    for (const item of Object.values(result.applied).filter((entry) => entry.changed)) {
      logLine("ok", `${item.label} ${item.previous} → ${item.value} ${item.unit} (재확인 완료)`);
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

function renderRun(run, method) {
  state.run = run;
  const stage = run.stage || "idle";
  const active = !!run.active;

  $("runStageCard").dataset.stage = stage;
  $("runStageChip").dataset.stage = stage;
  $("runStageChip").textContent = run.stage_label || "대기";
  setText("runStageText", run.stage_label, "대기");
  setText("runStageNote", run.detail, "런이 실행 중이 아닙니다.");
  setText("runDetail", run.detail, "시작 전");
  setText("statusRun", `런 ${run.stage_label || "대기"}`);
  setText("runModeNow", run.mode === "lcp_jet" ? "LCP 젯" : run.mode === "watch" ? "감시만" : null);

  const waiting = active && !run.watching;
  $("runSettleBar").hidden = !waiting;
  if (waiting) $("runSettleFill").style.width = "100%";

  setText("runThreshold", mpa(run.threshold));
  setText("runPressureNow", mpa(run.pressure));
  setText("runPeak", mpa(run.peak));
  setText("runElapsed", active ? `${Math.round(run.elapsed_seconds)} 초` : null);

  const ready = state.controlEnabled && state.loggedIn;
  $("runStartBtn").disabled = !ready || active;
  $("runAbortBtn").disabled = !ready || !active;
  for (const id of Object.values(RUN_FIELDS)) $(id).disabled = active;

  // Run events come from the server; fold them into the one event log and
  // promote the ones an operator has to acknowledge.
  for (const event of run.events || []) {
    const key = `${event.timestamp}|${event.message}`;
    if (state.seenRunEvents.has(key)) continue;
    state.seenRunEvents.add(key);
    logLine(RUN_EVENT_LEVEL[event.kind] || "info", event.message, event.timestamp);
    if (RUN_EVENT_ALARM[event.kind]) {
      raiseAlarm(`run:${key}`, "alarm", RUN_EVENT_ALARM[event.kind], event.message);
      state.alarmedRun = run.started_at;
    }
  }

  if (stage !== state.previousStage) {
    if (stage === "finished") {
      raiseAlarm(`done:${run.started_at}`, "done", "런 완료", `${run.detail} · 시작 ${run.started_at || ""}`);
    } else if ((stage === "aborted" || stage === "error") && state.alarmedRun !== run.started_at) {
      // The cause already raised its own alarm; do not report it twice.
      raiseAlarm(`stop:${run.started_at}:${stage}`, "alarm", "런 중단", run.detail || stage);
    }
    state.previousStage = stage;
  }

  const last = run.last_run;
  setText("lastRunStage", last ? (last.stage === "finished" ? "정상 완료" : "중단됨") : "기록 없음");
  setText("lastRunMode", last ? (last.mode === "lcp_jet" ? "LCP 젯" : "감시만") : null);
  setText("lastRunStarted", last ? last.started_at : null);
  setText("lastRunFinished", last ? last.finished_at : null);
  if (last && last.started_at && last.finished_at) {
    const seconds = Math.round((new Date(last.finished_at) - new Date(last.started_at)) / 1000);
    setText("lastRunDuration", `${Math.floor(seconds / 60)}분 ${seconds % 60}초`);
  } else {
    setText("lastRunDuration", null);
  }

  applyRunDefaults(run.config);
  updateRunMode();
  renderGauge(run, method);
}

function renderGauge(run, method) {
  const pressure = Number(state.run.pressure ?? NaN);
  const limit = Number(run.config?.pressure_limit) || Number(method.pmax) || 10;
  const fill = Number.isFinite(pressure) ? Math.max(0, Math.min(1, pressure / limit)) : 0;
  $("gaugeFill").style.width = `${(fill * 100).toFixed(1)}%`;

  const mark = $("gaugeMark");
  const threshold = run.active ? run.threshold : null;
  if (threshold === null || threshold === undefined) {
    mark.hidden = true;
  } else {
    mark.hidden = false;
    mark.style.left = `${Math.max(0, Math.min(100, (threshold / limit) * 100)).toFixed(1)}%`;
  }
  setText("gaugeNote", run.active && run.config?.pressure_limit
    ? `상한 ${Number(run.config.pressure_limit).toFixed(1)} MPa`
    : `Pmax ${num(method.pmax, 1) || "—"}`);
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
    logLine("warn", `LCP 젯 런 시작 · ${result.run.stage_label}`);
    showView("run");
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
  $("linkState").querySelector("b").textContent = "연결됨";
  setText("latency", `${data.latency_ms} ms`);
  setText("sessionUser", state.loggedIn ? auth.user_id : "없음", "없음");

  setText("controllerModel", controller.model, "SCL-40");
  setText("pumpModel", pump.model, "펌프 없음");
  setText("pumpUnit", pump.unit_id ? `Unit ${pump.unit_id}` : "", "");

  const monitorLive = monitor.available !== false;
  const pressure = num(monitor.pressure, 2);
  const flow = num(monitor.flow, 4);
  const target = num(monitor.target_flow ?? method.flow, 4);

  setText("pressure", pressure, "–.––");
  setText("flow", flow, "–.––––");
  setText("targetFlow", target, "–.––––");
  setText("pressureSource", monitorLive ? "Monitor" : "세션 없음");

  $("pressureCard").classList.toggle("stale", pressure === null);
  document.querySelector('.readout[data-channel="flow"]').classList.toggle("stale", flow === null);
  document.querySelector('.readout[data-channel="target"]').classList.toggle("stale", target === null);

  const pumpRunning = monitor.pump_on;
  $("pumpState").dataset.state = pumpRunning === true ? "running" : pumpRunning === false ? "stopped" : "unknown";
  setText("pumpStateText", pumpRunning === true ? "운전 중" : pumpRunning === false ? "정지" : "확인 중");
  setText("pumpStateNote", `OpState ${monitor.op_state_code || "—"}`);

  setText("methodAlias", method.alias ? `${method.alias} · No ${method.number ?? "—"}` : `No ${method.number ?? "—"}`);
  setText("methodFlow", num(method.flow, 4));
  setText("methodTflow", num(method.tflow, 4));
  setText("methodPmax", num(method.pmax, 1));
  setText("methodPmin", num(method.pmin, 1));

  setText("controllerDetail", `${controller.model || "—"} · fw ${controller.version || "—"} · addr ${controller.address || "—"}`);
  setText("pumpDetail", pump.model ? `${pump.model} · Unit ${pump.unit_id || "—"} · fw ${pump.version || "—"} · addr ${pump.address || "—"}` : "검출되지 않음");
  setText("hostName", summary.host_name);
  setText("systemState", summary.system_state_code);
  setText("loginState", state.loggedIn ? `웹 세션 · ${auth.user_id}` : `장비 코드 ${summary.login_state_code || "—"}`);
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
    ? { text: "시뮬레이터", key: "sim" }
    : !state.controlEnabled
      ? { text: "읽기 전용", key: "readonly" }
      : state.loggedIn
        ? { text: "제어 가능", key: "ready" }
        : { text: "로그인 필요", key: "control" };
  $("modeBadge").textContent = mode.text;
  $("modeBadge").dataset.mode = mode.key;
  setText("statusMode", mode.text);

  $("simBanner").hidden = !state.simulated;
  setText("statusHost", data.host);
  setText("statusCommand", activity.action
    ? `${activity.action} · ${activity.busy ? "처리 중" : (activity.result || "")} · ${activity.timestamp ? clock(new Date(activity.timestamp)) : "—"}`
    : "보낸 명령 없음");
  setText("lastUpdated", `갱신 ${clock(new Date(data.timestamp))}`);

  setText("railHost", data.host);
  setText("railPoll", `${(state.pollMs / 1000).toFixed(0)}초`);
  setText("railSamples", String(state.history.length));

  state.limits = data.limits || state.limits;
  const hint = Object.entries(state.limits)
    .map(([, spec]) => `${spec.label} ${spec.min}–${spec.max} ${spec.unit}`)
    .join(" · ");
  setText("paramHint", hint || "—");
  syncParamInputs(method);

  renderRun(data.run || { stage: "idle", active: false }, method);
  renderSchematic(monitor, state.run, method);
  updateParamButtons();
  checkSuddenRise();
  renderChart();

  if ($("flowInput").value === "" && method.flow) $("flowInput").value = num(method.flow, 4);
}

function renderOffline(message) {
  $("linkState").dataset.state = "offline";
  $("linkState").querySelector("b").textContent = "연결 끊김";
  setText("latency", "—");
  $("pressureCard").classList.add("stale");
  document.querySelectorAll(".readout").forEach((node) => node.classList.add("stale"));
  setStatusMessage(message, true);
  // A blind watchdog during a run has to be acknowledged, not just logged.
  raiseAlarm("offline", "alarm", "통신 끊김", message, state.run.active === true);
  state.runActiveAtFailure = state.run.active === true;
}

/* ---------- views ---------- */

const VIEWS = ["run", "setup", "log"];

function showView(name, updateHash = true) {
  if (!VIEWS.includes(name)) name = "run";
  for (const tab of document.querySelectorAll(".tab")) tab.classList.toggle("on", tab.dataset.view === name);
  for (const view of document.querySelectorAll(".view")) view.classList.toggle("on", view.dataset.view === name);
  // Keep the view in the address bar so a reload comes back to the same place.
  if (updateHash && location.hash.slice(1) !== name) history.replaceState(null, "", `#${name}`);
  if (name === "run") renderChart();
}

/* ---------- polling ---------- */

async function refresh(manual = false) {
  try {
    const data = await api("/api/snapshot");
    await fetchTrend();
    render(data);
    clearAlarm("offline", true);
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

/* ---------- manual commands ---------- */

async function login() {
  const userId = $("userId").value.trim();
  const password = $("password").value;
  if (!userId) {
    logLine("error", "장비 사용자 ID를 입력하세요.");
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
    logLine("ok", `장비 로그인 성공 · ${userId}`);
  } catch (error) {
    $("loginBtn").disabled = false;
    logLine("error", error.message);
  }
  await refresh(false);
}

async function logout() {
  try {
    await api("/api/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    logLine("ok", "장비 로그아웃 완료");
  } catch (error) {
    logLine("error", error.message);
  }
  await refresh(false);
}

async function setFlow() {
  const value = Number($("flowInput").value);
  if (!Number.isFinite(value) || value < 0 || value > 1) {
    logLine("error", "Flow는 0.0000~1.0000 mL/min 범위로 입력하세요.");
    return;
  }
  $("setFlowBtn").disabled = true;
  try {
    const result = await api("/api/control/method", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ params: { flow: value.toFixed(4) }, confirmation: "SET_METHOD" }),
    });
    const applied = result.applied.flow;
    logLine("ok", `Flow 변경 확인: ${applied.previous} → ${applied.value} mL/min`);
  } catch (error) {
    logLine("error", error.message);
  }
  await refresh(false);
}

async function command(action) {
  if (action === "start" && !confirm("현재 Method의 Flow로 펌프를 START 합니다. 계속할까요?")) return;
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

$("tabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (tab) showView(tab.dataset.view);
});

$("refreshBtn").addEventListener("click", () => refresh(true));
$("loginBtn").addEventListener("click", login);
$("logoutBtn").addEventListener("click", logout);
$("setFlowBtn").addEventListener("click", setFlow);
$("startBtn").addEventListener("click", () => command("start"));
$("stopBtn").addEventListener("click", () => command("stop"));
$("applyBtn").addEventListener("click", applyParams);
$("revertBtn").addEventListener("click", revertParams);
$("runStartBtn").addEventListener("click", startRun);
$("runAbortBtn").addEventListener("click", abortRun);
$("runMode").addEventListener("change", updateRunMode);

$("userId").addEventListener("keydown", (event) => { if (event.key === "Enter") $("password").focus(); });
$("password").addEventListener("keydown", (event) => { if (event.key === "Enter") login(); });
$("flowInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !$("setFlowBtn").disabled) setFlow();
});

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
  setText("railWindow", `${state.rangeMin}분`);
  renderChart();
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderChart, 120);
});

window.addEventListener("hashchange", () => showView(location.hash.slice(1), false));

setInterval(() => { $("clock").textContent = clock(new Date()); }, 1000);
$("clock").textContent = clock(new Date());
showView(location.hash.slice(1) || "run", false);
applyRunDefaults(null);
updateRunMode();
logLine("info", "콘솔 시작 · 장비 상태 조회");
renderChart();
refresh(true);
