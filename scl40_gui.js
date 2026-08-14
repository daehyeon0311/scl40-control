const $ = (id) => document.getElementById(id);
let host = "192.168.200.99";
let controlEnabled = false;
let loggedIn = false;
let accessPin = sessionStorage.getItem("scl40AccessPin") || "";

function logLine(level, message) {
  const row = document.createElement("div");
  row.innerHTML = `<span>${new Date().toLocaleTimeString("ko-KR", {hour12:false})}</span><b class="${level}">${level.toUpperCase()}</b><span></span>`;
  row.lastElementChild.textContent = message;
  $("activity").prepend(row);
  while ($("activity").children.length > 30) $("activity").lastElementChild.remove();
}

function setText(id, value, fallback = "—") { $(id).textContent = value ?? fallback; }

async function api(path, options={}, allowPinPrompt=true) {
  const headers = {...(options.headers || {})};
  if (accessPin) headers["X-SCL40-PIN"] = accessPin;
  const response = await fetch(path, {cache:"no-store", ...options, headers});
  const data = await response.json();
  if (response.status === 401 && data.pin_required && allowPinPrompt) {
    const entered = prompt("SCL-40 대시보드 접속 PIN을 입력하세요.");
    if (entered === null) throw new Error("대시보드 PIN 입력이 취소되었습니다.");
    accessPin = entered.trim();
    sessionStorage.setItem("scl40AccessPin", accessPin);
    return api(path, options, false);
  }
  if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function render(data) {
  host = data.host;
  controlEnabled = data.control_enabled;
  const ctrl = data.config?.controller || {};
  const pump = data.config?.pumps?.[0] || {};
  const status = data.summary || {};
  const monitor = data.monitor || {};
  const auth = data.auth || {};
  const activity = data.control_activity || {};
  loggedIn = !!auth.logged_in;
  $("connectionOrb").classList.replace("offline", "online");
  setText("connectionText", "ONLINE"); setText("latency", `${data.latency_ms} ms`);
  setText("controllerModel", ctrl.model); setText("pumpModel", pump.model || "NO PUMP");
  setText("subtitle", `${host} · ${status.group_name || "Shimadzu HPLC"}`);
  setText("pumpDetail", `${pump.model || "Not detected"} · Unit ${pump.unit_id || "—"}`);
  setText("pumpVersion", `Version ${pump.version || "—"}`);
  setText("controllerDetail", `${ctrl.model || "—"} / v${ctrl.version || "—"}`);
  setText("pumpAddress", pump.address); setText("hostName", status.host_name);
  setText("serverType", status.server_type); setText("systemState", status.system_state_code);
  setText("loginState", loggedIn ? `LOGGED IN · ${auth.user_id}` : `DEVICE ${status.login_state_code || "—"}`);
  setText("lastOperator", activity.operator_ip || "—");
  setText("lastCommand", activity.action ? `${activity.action} · ${activity.busy ? "PROCESSING" : activity.result.toUpperCase()}` : "—");
  $("startBtn").disabled = !(controlEnabled && loggedIn) || !!activity.busy;
  $("stopBtn").disabled = !(controlEnabled && loggedIn) || !!activity.busy;
  $("setFlowBtn").disabled = !(controlEnabled && loggedIn) || !!activity.busy;
  setText("pressure", monitor.pressure); setText("flow", monitor.flow); setText("targetFlow", monitor.target_flow);
  setText("pumpState", monitor.pump_on === true ? "RUNNING" : monitor.pump_on === false ? "STOPPED" : "UNKNOWN");
  $("pumpState").className = `state ${monitor.pump_on === true ? "running" : monitor.pump_on === false ? "stopped" : "unknown"}`;
  $("lastUpdated").textContent = `Updated ${new Date(data.timestamp).toLocaleTimeString("ko-KR")}`;
  $("modeBadge").textContent = controlEnabled ? (loggedIn ? "CONTROL READY" : "LOGIN REQUIRED") : "READ ONLY";
  $("modeBadge").className = `badge ${controlEnabled ? "control" : "neutral"}`;
  $("loginBtn").disabled = loggedIn;
  $("logoutBtn").disabled = !loggedIn;
  $("readonlyNotice").innerHTML = loggedIn
    ? "<b>제어 준비</b><span>START/STOP 버튼을 바로 사용할 수 있습니다.</span>"
    : "<b>로그인 필요</b><span>장비가 명령을 승인하려면 SCL-40 사용자 계정이 필요합니다.</span>";
  if (data.method?.flow && !$(`flowInput`).value) $("flowInput").value = data.method.flow;
}

async function refresh(manual=false) {
  try {
    const data = await api("/api/snapshot");
    render(data);
    if (manual) logLine("ok", `SCL-40 응답 수신 (${data.latency_ms} ms)`);
  } catch (error) {
    $("connectionOrb").classList.replace("online", "offline");
    setText("connectionText", "OFFLINE"); setText("latency", "— ms");
    logLine("error", error.message);
  }
}

async function login() {
  const user_id = $("userId").value.trim();
  const password = $("password").value;
  if (!user_id) { logLine("error", "SCL-40 사용자 ID를 입력하세요."); return; }
  $("loginBtn").disabled = true;
  try {
    await api("/api/login", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({user_id, password})
    });
    $("password").value = "";
    logLine("ok", `SCL-40 로그인 성공 · ${user_id}`);
    await refresh(true);
  } catch (error) {
    $("loginBtn").disabled = false;
    logLine("error", error.message);
  }
}

async function logout() {
  try {
    await api("/api/logout", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    loggedIn = false;
    logLine("ok", "SCL-40 로그아웃 완료");
    await refresh(true);
  } catch (error) { logLine("error", error.message); }
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
    const result = await api("/api/control/set-flow", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({flow:formatted, confirmation:"SET_FLOW"})
    });
    logLine("ok", `유량 변경 및 재조회 확인: ${result.previous_flow} → ${result.flow} mL/min`);
    await refresh(true);
  } catch (error) { logLine("error", error.message); }
  finally { $("setFlowBtn").disabled = !loggedIn; }
}

async function command(action) {
  if (action === "start" && !confirm("현재 표시된 목표 유량으로 펌프를 START합니다. 계속할까요?")) return;
  try {
    const data = await api(`/api/control/${action}`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({confirmation:action.toUpperCase()})
    });
    logLine("ok", `${data.action} 명령 수신 확인: ${data.response_root}`);
  } catch (error) { logLine("error", error.message); }
  finally { setTimeout(() => refresh(true), 500); }
}

$("refreshBtn").addEventListener("click", () => refresh(true));
$("loginBtn").addEventListener("click", login);
$("logoutBtn").addEventListener("click", logout);
$("setFlowBtn").addEventListener("click", setFlow);
$("password").addEventListener("keydown", (event) => { if (event.key === "Enter") login(); });
$("startBtn").addEventListener("click", () => command("start"));
$("stopBtn").addEventListener("click", () => command("stop"));
setInterval(() => $("clock").textContent = new Date().toLocaleTimeString("ko-KR", {hour12:false}), 1000);
setInterval(() => refresh(false), 5000);
logLine("ok", "로컬 대시보드 시작 · SCL-40 상태 조회");
refresh(true);
