"use strict";

const $ = (id) => document.getElementById(id);

const HISTORY_LIMIT = 2000;

const RUN_DEFAULTS = {
  mode: "lcp_jet",
  fill_flow: "0.3000",
  fill_pressure: "2.0",
  run_flow: "0.0460",
  run_pressure: "1.5",
  pressure_limit: "9.0",
  settle_seconds: "15",
  stage_timeout: "1800",
  duration_seconds: "60",
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
  duration_seconds: "runDuration",
};

const RUN_EVENT_LEVEL = {
  fill_detected: "ok",
  end_detected: "ok",
  stopped: "ok",
  watching: "info",
  fill_started: "warn",
  watch_started: "warn",
  timed_started: "warn",
  duration_complete: "ok",
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
  historyByUnit: {},
  accessPin: sessionStorage.getItem("scl40AccessPin") || "",
  pollTimer: null,
  chartFrame: null,
  systemCommand: { action: null, phase: null },
  trendSinceByUnit: {},
  limits: {},
  pumps: [],
  selectedUnit: localStorage.getItem("scl40SelectedPump") || "A",
  lastData: null,
  run: { stage: "idle", active: false },
  seenRunEvents: new Set(),
  authorization: { role: "viewer", control: false, pressure_limits: false, acknowledge_alarms: false },
  chartMeta: null,
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

function shortTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : clock(date);
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

async function downloadCsv(kind) {
  try {
    const headers = {};
    if (state.accessPin) headers["X-SCL40-PIN"] = state.accessPin;
    const response = await fetch(`/api/export.csv?kind=${encodeURIComponent(kind)}`, { cache: "no-store", headers });
    if (!response.ok) throw new Error(`CSV 다운로드 실패 · HTTP ${response.status}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `scl40_${kind}_${new Date().toISOString().replace(/[:.]/g, "-")}.csv`;
    link.click();
    URL.revokeObjectURL(url);
    logLine("ok", `${kind.toUpperCase()} CSV 저장`);
  } catch (error) {
    logLine("error", error.message);
  }
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
  const series = state.pumps.map((pump, index) => ({
    index,
    unitId: pump.unit_id,
    model: pump.model,
    color: index === 0 ? "#0f5fa8" : index === 1 ? "#c26a13" : "#65717f",
    dashed: index === 1,
    points: (state.historyByUnit[pump.unit_id] || []).filter((point) => point.t >= from),
  }));
  const allPoints = series.flatMap((item) => item.points);

  $("chartEmpty").hidden = series.some((item) => item.points.length >= 2);
  svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
  svg.replaceChildren();

  const maxPressure = axisMax(Math.max(0, ...allPoints.map((point) => point.p ?? 0)), 2);

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
    opacity: 0.035, preserveAspectRatio: "xMidYMid meet",
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

  const maxGap = Math.max(state.pollMs, 2000) * 3;
  for (const item of series) {
    for (const d of buildSegments(item.points, (point) => point.p, xOf, yOfPressure, maxGap)) {
      const attrs = {
        d, fill: "none", stroke: item.color, "stroke-width": 2.4,
        "stroke-linejoin": "round", "stroke-linecap": "round",
      };
      if (item.dashed) attrs["stroke-dasharray"] = "7 4";
      svg.append(svgEl("path", attrs));
    }

    const last = item.points[item.points.length - 1];
    if (last && last.p !== null) {
      const cx = xOf(last.t);
      const cy = yOfPressure(last.p);
      svg.append(svgEl("circle", { cx, cy, r: 3, fill: item.color, stroke: "#fff", "stroke-width": 1 }));
      const chipX = box.right - 84;
      const chipY = box.top + 8 + item.index * 24;
      svg.append(svgEl("rect", {
        x: chipX, y: chipY, width: 76, height: 18, rx: 2,
        fill: "#ffffff", stroke: item.color, "stroke-width": 1,
      }));
      svg.append(svgEl("text", {
        x: chipX + 38, y: chipY + 12.5, "text-anchor": "middle",
        "font-family": "Consolas, monospace", "font-size": 10, "font-weight": 700, fill: item.color,
      }, `${item.unitId}  ${last.p.toFixed(2)}`));
    }
  }

  // The pressure that ends the current stage.
  const run = state.run || {};
  if (run.active && run.threshold !== null && run.threshold !== undefined && run.threshold <= maxPressure) {
    const y = yOfPressure(run.threshold);
    svg.append(svgEl("rect", {
      x: box.left, y: box.top, width: box.right - box.left, height: Math.max(0, y - box.top),
      fill: "#a92b26", opacity: 0.045,
    }));
    svg.append(svgEl("line", {
      x1: box.left, y1: y, x2: box.right, y2: y,
      stroke: "#a92b26", "stroke-width": 1, "stroke-dasharray": "6 4",
    }));
    svg.append(svgEl("text", {
      x: box.left + 6, y: y - 5,
      "font-family": "Consolas, monospace", "font-size": 10, fill: "#a92b26",
    }, `판단 압력 ${run.threshold.toFixed(2)} MPa`));
  }

  state.chartMeta = { box, from, spanMs, series };

}

function showChartTooltip(event) {
  const meta = state.chartMeta;
  const svg = $("chart");
  const tooltip = $("chartTooltip");
  if (!meta || !svg || !tooltip) return;
  const rect = svg.getBoundingClientRect();
  const x = ((event.clientX - rect.left) / rect.width) * meta.box.width;
  if (x < meta.box.left || x > meta.box.right) {
    tooltip.hidden = true;
    return;
  }
  const time = meta.from + ((x - meta.box.left) / (meta.box.right - meta.box.left)) * meta.spanMs;
  const rows = meta.series.map((item) => {
    const nearest = item.points.reduce((best, point) => !best || Math.abs(point.t - time) < Math.abs(best.t - time) ? point : best, null);
    return { unitId: item.unitId, color: item.color, point: nearest };
  }).filter((item) => item.point && Math.abs(item.point.t - time) <= Math.max(state.pollMs * 3, 10000));
  if (!rows.length) {
    tooltip.hidden = true;
    return;
  }
  tooltip.replaceChildren();
  const title = document.createElement("b");
  title.textContent = new Date(rows[0].point.t).toLocaleTimeString("ko-KR", TIME_FORMAT);
  tooltip.append(title);
  for (const item of rows) {
    const row = document.createElement("span");
    const label = document.createElement("em");
    label.style.color = item.color;
    label.textContent = `PUMP ${item.unitId}`;
    const value = document.createElement("strong");
    value.textContent = item.point.p === null ? "—" : `${item.point.p.toFixed(2)} MPa`;
    row.append(label, value);
    tooltip.append(row);
  }
  tooltip.hidden = false;
  tooltip.style.left = `${Math.min(event.offsetX + 16, rect.width - 170)}px`;
  tooltip.style.top = `${Math.max(8, event.offsetY - 20)}px`;
}

function scheduleChartRender() {
  if (state.chartFrame !== null) return;
  state.chartFrame = requestAnimationFrame(() => {
    state.chartFrame = null;
    renderChart();
  });
}

/* Trend history lives on the server, so it survives reloads and every client
   sees the same trace. Each poll pulls only the samples it has not seen. */
async function fetchTrend() {
  await Promise.all(state.pumps.map(async (pump) => {
    const unitId = pump.unit_id;
    const since = state.trendSinceByUnit[unitId] || 0;
    const data = await api(`/api/trend?unit=${encodeURIComponent(unitId)}&since=${since}`);
    const history = state.historyByUnit[unitId] || [];
    for (const [t, p, f, target] of data.samples || []) {
      history.push({ t: t * 1000, p, f, target });
    }
    state.trendSinceByUnit[unitId] = data.until || since;
    if (history.length > HISTORY_LIMIT) history.splice(0, history.length - HISTORY_LIMIT);
    state.historyByUnit[unitId] = history;
  }));
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
  const ready = state.controlEnabled && state.loggedIn && state.authorization.pressure_limits && !state.run.active;
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
      body: JSON.stringify({ unit_id: state.selectedUnit, params, confirmation: "SET_METHOD" }),
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

function applyRunDefaults(config, force = false) {
  for (const [key, id] of Object.entries(RUN_FIELDS)) {
    const node = $(id);
    const supplied = config && config[key] !== undefined && config[key] !== null ? String(config[key]) : "";
    if (force && supplied) node.value = supplied;
    else if (node.value === "") node.value = supplied || RUN_DEFAULTS[key];
  }
}

function updateRunMode() {
  const mode = $("runMode").value;
  for (const node of document.querySelectorAll("[data-modes]")) {
    node.hidden = !node.dataset.modes.split(/\s+/).includes(mode);
  }
  $("runFlowLabel").textContent = mode === "timed" ? "운전 유량" : "실험 유량";
  $("runThresholdLabel").textContent = mode === "timed" ? "남은 시간" : "판단 압력";
  $("runHint").textContent = mode === "timed"
    ? "설정한 유량으로 운전하고 시간이 끝나면 자동 정지합니다. 압력 상한과 ABORT는 항상 작동하며, 타이머는 브라우저를 닫아도 유지됩니다."
    : "물 채우기는 즉시 감시하고, 실험 단계만 압력이 판단값 아래로 내려온 뒤 감시합니다. 감시는 서버에서 돌기 때문에 브라우저를 닫아도 유지됩니다.";
  if (!state.run.active) setText("runThreshold", null);
}

function renderRun(run, monitorPressure) {
  state.run = run;
  const stage = run.stage || "idle";
  const active = !!run.active;
  $("runPanel").dataset.active = active ? "true" : "false";

  $("runHeaderStatus").dataset.stage = stage;
  $("runHeaderStatus").title = run.detail || "런이 실행 중이 아닙니다.";
  setText("runHeaderText", run.stage_label, "대기");
  $("runStageChip").dataset.stage = stage;
  $("runStageChip").textContent = run.stage_label || "대기";
  setText("runDetail", run.detail, "시작 전");
  setText("statusRun", `RUN ${stage}`);

  const mpa = (value) => (value === null || value === undefined ? null : `${value.toFixed(2)} MPa`);
  const timed = run.mode === "timed";
  $("runThresholdLabel").textContent = timed ? "남은 시간" : "판단 압력";
  const duration = Number(run.config?.duration_seconds);
  const remaining = timed && Number.isFinite(duration) ? Math.max(0, duration - Number(run.elapsed_seconds || 0)) : null;
  setText("runThreshold", timed ? (remaining === null ? null : `${Math.ceil(remaining)} s`) : mpa(run.threshold));
  setText("runPressureNow", mpa(run.pressure ?? monitorPressure));
  setText("runPeak", mpa(run.peak));
  setText("runElapsed", active ? `${Math.round(run.elapsed_seconds)} s` : null);

  const ready = state.controlEnabled && state.loggedIn && state.authorization.control;
  const multiPump = state.pumps.length > 1;
  $("runStartBtn").disabled = !ready || active || multiPump;
  $("runAbortBtn").disabled = !ready || !active;
  $("multiPumpWarning").hidden = !multiPump;
  for (const id of Object.values(RUN_FIELDS)) $(id).disabled = active;

  // Run events come from the server; fold them into the single event log.
  for (const event of run.events || []) {
    const key = `${event.timestamp}|${event.message}`;
    if (state.seenRunEvents.has(key)) continue;
    state.seenRunEvents.add(key);
    logLine(RUN_EVENT_LEVEL[event.kind] || "info", event.message, event.timestamp);
  }

  applyRunDefaults(run.config, active);
  updateRunMode();
}

async function startRun() {
  const values = runFieldValues();
  const lcp = values.mode === "lcp_jet";
  const timed = values.mode === "timed";
  const summary = timed
    ? `${values.run_flow} mL/min으로 펌프를 켜고 ${values.duration_seconds}초 동안 운전한 뒤 자동으로 정지합니다.`
    : lcp
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
    logLine("warn", `자동운전 시작 · ${result.run.stage_label}`);
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

function renderOperationalRecords(data) {
  const communication = data.communication || {};
  const commState = communication.state || "wait";
  $("commState").dataset.state = commState;
  $("commState").textContent = commState.toUpperCase();
  setText("commLast", communication.last_success || "—");
  setText("commLatency", communication.last_latency_ms === null || communication.last_latency_ms === undefined ? "—" : `${communication.last_latency_ms} ms`);
  setText("commFailures", `${communication.consecutive_failures || 0} consecutive · ${communication.total_failures || 0} total`);
  setText("roleName", (state.authorization.role || "viewer").toUpperCase());

  const stats = data.records || {};
  setText("recordSamples", stats.samples ?? 0);
  setText("recordEvents", stats.events ?? 0);
  setText("recordAlarms", stats.active_alarms ?? 0);

  const alarmList = $("alarmList");
  alarmList.replaceChildren();
  const alarms = data.alarms || [];
  if (!alarms.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "활성 알람이 없습니다.";
    alarmList.append(empty);
  }
  for (const alarm of alarms) {
    const row = document.createElement("div");
    row.className = `alarm-row${alarm.acknowledged ? " acknowledged" : ""}`;
    const code = document.createElement("strong");
    code.textContent = `${alarm.unit_id ? `${alarm.unit_id} · ` : ""}${alarm.code}`;
    const message = document.createElement("span");
    message.textContent = alarm.message;
    const action = document.createElement("button");
    action.type = "button";
    action.className = "btn ghost-btn";
    action.textContent = alarm.acknowledged ? "ACKED" : "ACK";
    action.disabled = !!alarm.acknowledged || !state.authorization.acknowledge_alarms;
    action.addEventListener("click", () => acknowledgeAlarm(alarm.id));
    row.append(code, message, action);
    alarmList.append(row);
  }

  const audit = $("auditList");
  audit.replaceChildren();
  const events = data.recent_events || [];
  if (!events.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "기록된 이벤트가 없습니다.";
    audit.append(empty);
  }
  for (const event of events) {
    const row = document.createElement("div");
    row.className = "audit-row";
    const at = document.createElement("time");
    at.textContent = shortTime(event.timestamp);
    const action = document.createElement("strong");
    action.textContent = event.action;
    const message = document.createElement("span");
    message.textContent = `${event.unit_id ? `Pump ${event.unit_id} · ` : ""}${event.message}`;
    row.append(at, action, message);
    audit.append(row);
  }
}

async function acknowledgeAlarm(alarmId) {
  try {
    await api("/api/alarms/ack", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alarm_id: alarmId }),
    });
    logLine("ok", `알람 #${alarmId} 확인 처리`);
    await refresh(false);
  } catch (error) {
    logLine("error", error.message);
  }
}

/* ---------- rendering ---------- */

function selectPump(unitId) {
  if (!unitId || unitId === state.selectedUnit) return;
  state.selectedUnit = unitId;
  localStorage.setItem("scl40SelectedPump", unitId);
  for (const input of paramInputs()) input.dataset.dirty = "0";
  if (state.lastData) render(state.lastData, { chart: false });
}

function pumpIllustration(model, unitId) {
  const series40 = /40/i.test(model || "");
  // LC-40i uses the more detailed keypad/dial face; LC-20Ai uses the compact face.
  const detailedFace = series40;
  const svg = svgEl("svg", {
    class: `pump-illustration ${series40 ? "series-40" : "series-20"}`,
    viewBox: "0 0 84 82", role: "img", "aria-label": `${model || "Pump"} 장비 그림`,
  });
  svg.append(svgEl("title", {}, `${model || "Pump"} · Unit ${unitId}`));
  svg.append(svgEl("rect", {
    x: detailedFace ? 8 : 11, y: detailedFace ? 8 : 4, width: detailedFace ? 68 : 62, height: detailedFace ? 68 : 73,
    rx: detailedFace ? 2 : 6, fill: detailedFace ? "#eef1f4" : "#f7fafc", stroke: detailedFace ? "#707b87" : "#8997a6",
  }));
  svg.append(svgEl("rect", {
    x: detailedFace ? 15 : 18, y: detailedFace ? 15 : 13, width: detailedFace ? 43 : 39, height: detailedFace ? 18 : 24,
    rx: detailedFace ? 1 : 3, fill: series40 ? "#dcecf8" : "#d8e7d2", stroke: series40 ? "#76a6c9" : "#78906e",
  }));
  svg.append(svgEl("text", {
    x: detailedFace ? 36.5 : 37.5, y: detailedFace ? 27 : 28, "text-anchor": "middle",
    "font-family": "Consolas, monospace", "font-size": series40 ? 7 : 6.5, "font-weight": 700,
    fill: series40 ? "#0f5fa8" : "#3d6438",
  }, series40 ? "LC-40i" : "LC-20Ai"));
  svg.append(svgEl("circle", { cx: 65, cy: detailedFace ? 17 : 19, r: 2.6, fill: "#2d8a54" }));
  if (detailedFace) {
    for (let row = 0; row < 2; row += 1) {
      for (let col = 0; col < 4; col += 1) {
        svg.append(svgEl("rect", { x: 16 + col * 9, y: 39 + row * 8, width: 5, height: 4, rx: 1, fill: "#aab2ba" }));
      }
    }
    svg.append(svgEl("circle", { cx: 63, cy: 46, r: 8, fill: "#d7dde2", stroke: "#7b8792" }));
    for (let y = 60; y <= 68; y += 4) svg.append(svgEl("line", { x1: 16, y1: y, x2: 65, y2: y, stroke: "#9aa5af" }));
  } else {
    svg.append(svgEl("rect", { x: 18, y: 45, width: 46, height: 21, rx: 3, fill: "#e8edf2", stroke: "#b3bec8" }));
    svg.append(svgEl("circle", { cx: 30, cy: 55.5, r: 6.5, fill: "#d0d8df", stroke: "#8794a1" }));
    svg.append(svgEl("circle", { cx: 52, cy: 55.5, r: 6.5, fill: "#d0d8df", stroke: "#8794a1" }));
  }
  svg.append(svgEl("text", {
    x: detailedFace ? 61 : 65, y: 73, "text-anchor": "middle", "font-family": "Consolas, monospace",
    "font-size": 8, "font-weight": 700, fill: "#596675",
  }, unitId));
  return svg;
}

function renderPumpDeck(pumps) {
  const deck = $("pumpDeck");
  deck.replaceChildren();
  for (const pump of pumps) {
    const monitor = pump.monitor || {};
    const card = document.createElement("button");
    card.type = "button";
    card.className = "pump-card";
    card.classList.toggle("selected", pump.unit_id === state.selectedUnit);
    card.dataset.state = monitor.error ? "error" : monitor.pump_on === true ? "running" : monitor.pump_on === false ? "stopped" : "unknown";

    const illustration = pumpIllustration(pump.model, pump.unit_id);
    const identity = document.createElement("span");
    identity.className = "pump-identity";
    const name = document.createElement("span");
    name.className = "pump-name";
    name.textContent = pump.model || "Unknown pump";
    const unit = document.createElement("span");
    unit.className = "pump-unit";
    unit.textContent = `UNIT ${pump.unit_id}`;
    identity.append(name, unit);
    const live = document.createElement("span");
    live.className = "pump-live";
    live.textContent = monitor.error ? "ERROR" : monitor.pump_on === true ? "RUNNING" : monitor.pump_on === false ? "STOPPED" : "UNKNOWN";
    card.append(illustration, identity, live);

    const metrics = document.createElement("dl");
    for (const [label, value] of [
      ["PRESSURE", `${num(monitor.pressure, 2) ?? "—"} MPa`],
      ["FLOW", `${num(monitor.flow, 4) ?? "—"} mL/min`],
    ]) {
      const box = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value;
      box.append(dt, dd);
      metrics.append(box);
    }
    card.append(metrics);
    card.addEventListener("click", () => selectPump(pump.unit_id));
    deck.append(card);
  }
}

function syncPumpSelector(pumps) {
  const select = $("flowPumpSelect");
  const signature = pumps.map((pump) => `${pump.unit_id}:${pump.model}`).join("|");
  if (select.dataset.signature !== signature) {
    select.replaceChildren();
    for (const pump of pumps) {
      const option = document.createElement("option");
      option.value = pump.unit_id;
      option.textContent = `Unit ${pump.unit_id} · ${pump.model}`;
      select.append(option);
    }
    select.dataset.signature = signature;
  }
  select.value = state.selectedUnit;
}

function render(data, { chart = true } = {}) {
  state.lastData = data;
  state.host = data.host;
  state.controlEnabled = !!data.control_enabled;
  state.simulated = !!data.simulated;
  // Poll faster while a run is in progress so the trend and stage stay live.
  state.pollMs = data.run?.active || state.simulated ? 2000 : 5000;

  const controller = data.config?.controller || {};
  state.pumps = data.pumps || [];
  if (!state.pumps.some((pump) => pump.unit_id === state.selectedUnit)) {
    state.selectedUnit = state.pumps[0]?.unit_id || "A";
  }
  const pump = state.pumps.find((item) => item.unit_id === state.selectedUnit) || {};
  const summary = data.summary || {};
  const method = pump.method || data.method || {};
  const monitor = pump.monitor || data.monitor || {};
  const auth = data.auth || {};
  const activity = data.control_activity || {};
  state.loggedIn = !!auth.logged_in;
  state.authorization = data.authorization || state.authorization;
  $("commandbar").dataset.auth = state.loggedIn ? "logged-in" : "logged-out";
  document.querySelector(".access-group").dataset.auth = state.loggedIn ? "logged-in" : "logged-out";
  $("accessSession").hidden = !state.loggedIn;
  $("accessSession").querySelector("b").textContent = state.loggedIn
    ? `${auth.user_id} · ${state.authorization.role.toUpperCase()}`
    : "NONE";

  $("linkState").dataset.state = "online";
  $("linkState").querySelector("b").textContent = "ONLINE";
  setText("latency", `${data.latency_ms} ms`);
  setText("chartLive", `LIVE · ${(state.pollMs / 1000).toFixed(0)} s`);
  setText("sessionUser", state.loggedIn ? `${auth.user_id} · ${state.authorization.role.toUpperCase()}` : "NONE", "NONE");

  setText("controllerModel", controller.model, "SCL-40");
  setText("pumpSummary", state.pumps.map((item) => `${item.model} · Unit ${item.unit_id}`).join("  |  "), "NO PUMP");
  renderPumpDeck(state.pumps);
  syncPumpSelector(state.pumps);
  const pumpA = state.pumps.find((item) => item.unit_id === "A");
  const pumpB = state.pumps.find((item) => item.unit_id === "B");
  setText("legendPumpA", pumpA ? `PUMP A · ${pumpA.model}` : "PUMP A");
  setText("legendPumpB", pumpB ? `PUMP B · ${pumpB.model}` : "PUMP B");
  setText("methodAlias", `PUMP ${state.selectedUnit} · ${method.alias ? `${method.alias} · ` : ""}No ${method.number ?? "—"}`);
  setText("methodFlow", num(method.flow, 4));
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
  setText("pumpDetail", state.pumps.map((item) => `${item.model} · Unit ${item.unit_id} · fw ${item.version || "—"} · addr ${item.address || "—"}`).join(" | "), "not detected");
  setText("hostName", summary.host_name);
  setText("systemState", summary.system_state_code);
  setText("loginState", state.loggedIn ? `web session · ${auth.user_id}` : `device code ${summary.login_state_code || "—"}`);
  setText("lastOperator", activity.operator_ip);

  const commandReady = state.controlEnabled && state.loggedIn && state.authorization.control && !activity.busy;
  const commandPending = state.systemCommand.phase === "pending";
  const runActive = !!data.run?.active;
  $("startBtn").disabled = !commandReady || commandPending || runActive || state.pumps.length === 0;
  $("stopBtn").disabled = !commandReady || commandPending;
  const pumpStates = state.pumps.map((pump) => pump.monitor?.pump_on);
  const allRunning = pumpStates.length > 0 && pumpStates.every((running) => running === true);
  const allStopped = pumpStates.length > 0 && pumpStates.every((running) => running === false);
  const pressedAction = commandPending
    ? state.systemCommand.action
    : allRunning
      ? "start"
      : allStopped
        ? "stop"
        : null;
  for (const name of ["start", "stop"]) {
    const pressed = name === pressedAction;
    $(`${name}Btn`).classList.toggle("command-pressed", pressed);
    $(`${name}Btn`).setAttribute("aria-pressed", pressed ? "true" : "false");
  }
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

  setText("acqHost", data.host);
  setText("acqPoll", `${(state.pollMs / 1000).toFixed(0)} s`);
  setText("acqSamples", state.pumps.map((item) => `${item.unit_id}:${(state.historyByUnit[item.unit_id] || []).length}`).join(" · "), "0");
  if (chart) scheduleChartRender();

  const flowMax = Number(state.limits.flow?.max ?? 5);
  $("flowInput").max = String(flowMax);
  setText("flowRangeLabel", `0.0000 – ${flowMax.toFixed(4)}`);
  if (document.activeElement !== $("flowInput")) $("flowInput").value = num(method.flow, 4) || "";
  renderOperationalRecords(data);
}

function renderOffline(message) {
  $("linkState").dataset.state = "offline";
  $("linkState").querySelector("b").textContent = "OFFLINE";
  setText("latency", "—");
  setStatusMessage(message, true);
}

/* ---------- polling ---------- */

async function refresh(manual = false) {
  try {
    const data = await api("/api/snapshot");
    // The first trend request also needs the newly discovered pump list.
    state.pumps = data.pumps || [];
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
  const flowMax = Number(state.limits.flow?.max ?? 5);
  if (!Number.isFinite(value) || value < 0 || value > flowMax) {
    logLine("error", `유량은 0.0000~${flowMax.toFixed(4)} mL/min 범위로 입력하세요.`);
    return;
  }
  const formatted = value.toFixed(4);
  $("setFlowBtn").disabled = true;
  try {
    const result = await api("/api/control/method", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ unit_id: state.selectedUnit, params: { flow: formatted }, confirmation: "SET_METHOD" }),
    });
    const applied = result.applied.flow;
    logLine("ok", `Pump ${result.unit_id} 유량 변경 확인 (readback): ${applied.previous} → ${applied.value} mL/min`);
  } catch (error) {
    logLine("error", error.message);
  }
  await refresh(false);
}

function showSystemCommandState(action, phase = null) {
  const command = state.systemCommand;
  command.action = phase ? action : null;
  command.phase = phase;

  for (const name of ["start", "stop"]) {
    const button = $(`${name}Btn`);
    const pending = name === action && phase === "pending";
    const pressed = name === action && phase && phase !== "error";
    button.classList.toggle("command-pending", pending);
    button.classList.toggle("command-pressed", pressed);
    button.setAttribute("aria-busy", pending ? "true" : "false");
    button.textContent = `${name.toUpperCase()} ALL`;
  }
}

function pumpCommandConfirmed(data, action) {
  const states = (data.pumps || []).map((pump) => pump.monitor?.pump_on);
  const expected = action === "start";
  return states.length > 0 && states.every((running) => running === expected);
}

async function waitForPumpCommand(action, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    const data = await api("/api/snapshot");
    state.pumps = data.pumps || [];
    render(data, { chart: false });
    if (pumpCommandConfirmed(data, action)) return true;
  }
  return false;
}

async function command(action) {
  if (state.systemCommand.phase === "pending") return;
  const targets = state.pumps.map((pump) => `Unit ${pump.unit_id} ${pump.model}: ${pump.method?.flow ?? "—"} mL/min`).join("\n");
  if (action === "start" && !confirm(`SYSTEM START는 연결된 모든 펌프를 함께 켭니다.\n\n${targets}\n\n계속할까요?`)) return;
  showSystemCommandState(action, "pending");
  $("startBtn").disabled = true;
  $("stopBtn").disabled = true;
  clearTimeout(state.pollTimer);
  try {
    const data = await api(`/api/control/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmation: action.toUpperCase() }),
    });
    logLine("warn", `SYSTEM ${data.action} 명령 전송 · 장비 응답 ${data.response_root}`);
    const confirmed = await waitForPumpCommand(action);
    if (confirmed) {
      logLine("ok", `SYSTEM ${data.action} 상태 전환 확인`);
    } else {
      logLine("warn", `SYSTEM ${data.action} 명령은 전송됐지만 5초 안에 모니터 상태가 바뀌지 않았습니다.`);
    }
  } catch (error) {
    logLine("error", error.message);
    showSystemCommandState(action, "error");
  }
  showSystemCommandState(action, null);
  await refresh(false);
}

/* ---------- wiring ---------- */

$("refreshBtn").addEventListener("click", () => refresh(true));
$("loginBtn").addEventListener("click", login);
$("logoutBtn").addEventListener("click", logout);
$("setFlowBtn").addEventListener("click", setFlow);
$("flowPumpSelect").addEventListener("change", (event) => selectPump(event.target.value));
$("startBtn").addEventListener("click", () => command("start"));
$("stopBtn").addEventListener("click", () => command("stop"));
$("userId").addEventListener("keydown", (event) => { if (event.key === "Enter") $("password").focus(); });
$("password").addEventListener("keydown", (event) => { if (event.key === "Enter") login(); });
$("flowInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !$("setFlowBtn").disabled) setFlow();
});
$("multiPumpWarning").addEventListener("click", () => {
  const expanded = $("multiPumpWarning").getAttribute("aria-expanded") === "true";
  $("multiPumpWarning").setAttribute("aria-expanded", expanded ? "false" : "true");
});
$("chart").addEventListener("pointermove", showChartTooltip);
$("chart").addEventListener("pointerleave", () => { $("chartTooltip").hidden = true; });
for (const button of document.querySelectorAll("button[data-export]")) {
  button.addEventListener("click", () => downloadCsv(button.dataset.export));
}

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
  setText("acqWindow", `${state.rangeMin} min`);
  scheduleChartRender();
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(scheduleChartRender, 120);
});

setInterval(() => { $("clock").textContent = clock(new Date()); }, 1000);
$("clock").textContent = clock(new Date());
applyRunDefaults(null);
updateRunMode();
logLine("info", "콘솔 시작 · SCL-40 상태 조회");
scheduleChartRender();
refresh(true);
