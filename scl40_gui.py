#!/usr/bin/env python3
"""Local/LAN dashboard for Shimadzu SCL-40 with gated control endpoints."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import logging
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>'
CONFIG_REQUEST = XML_HEADER + '<Config/>'
STATUS_REQUEST = (
    XML_HEADER
    + '<Summary><GroupName/><HostName/><SysNum/><OtherNum/><NoRegNum/>'
      '<DateForm/><MainteCollect/><SysCollect/><Systems/><OtherGroups/>'
      '<NoRegSystems/></Summary>'
)
PUMP_START_REQUEST = XML_HEADER + '<Event><Method><PumpBT>1</PumpBT></Method></Event>'
PUMP_STOP_REQUEST = XML_HEADER + '<Event><Method><PumpBT>0</PumpBT></Method></Event>'


class SCL40Error(RuntimeError):
    pass


def text_at(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    value = node.findtext(path)
    return value.strip() if value else default


class SCL40Client:
    def __init__(self, host: str, timeout: float = 3.0) -> None:
        self.host = host
        self.timeout = timeout
        self.base_url = f"http://{host}"
        self._config_cache: dict[str, Any] | None = None
        self._config_cache_time = 0.0
        self.session_id = ""
        self.user_id = ""

    def _post_xml(self, endpoint: str, body: str) -> tuple[ET.Element, str]:
        url = self.base_url + endpoint
        request = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=UTF-8", "User-Agent": "SCL40-Local-GUI/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if response.status != 200:
                    raise SCL40Error(f"{endpoint}: HTTP {response.status}")
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise SCL40Error(f"{endpoint} 연결 실패: {exc}") from exc
        try:
            return ET.fromstring(raw), raw
        except ET.ParseError as exc:
            raise SCL40Error(f"{endpoint}: XML이 아닌 응답") from exc

    def get_config(self, force: bool = False) -> dict[str, Any]:
        if not force and self._config_cache and time.monotonic() - self._config_cache_time < 30:
            return self._config_cache
        root, _ = self._post_xml("/cgi-bin/Config.cgi", CONFIG_REQUEST)
        if root.tag != "Config":
            raise SCL40Error(f"Config 응답 루트가 예상과 다름: {root.tag}")
        controller = root.find("./Info/Ctrl")
        pumps: list[dict[str, str]] = []
        for pump in root.findall("./Info/Pumps/Pump"):
            model = text_at(pump, "Model")
            if model:
                pumps.append({
                    "unit_id": text_at(pump, "UnitID"),
                    "model": model,
                    "sub_model": text_at(pump, "SubModel"),
                    "version": text_at(pump, "Version"),
                    "address": text_at(pump, "Address"),
                    "state_code": text_at(pump, "State"),
                })
        result: dict[str, Any] = {
            "controller": {
                "model": text_at(controller, "Model"),
                "sub_model": text_at(controller, "SubModel"),
                "version": text_at(controller, "Version"),
                "address": text_at(controller, "Address"),
            },
            "pumps": pumps,
        }
        self._config_cache = result
        self._config_cache_time = time.monotonic()
        return result

    def get_summary(self) -> dict[str, Any]:
        root, _ = self._post_xml("/cgi-bin/Status.cgi", STATUS_REQUEST)
        if root.tag != "Summary":
            raise SCL40Error(f"Status 응답 루트가 예상과 다름: {root.tag}")
        system = root.find("./Systems/System")
        return {
            "group_name": text_at(root, "GroupName"),
            "host_name": text_at(root, "HostName"),
            "system_name": text_at(system, "Name"),
            "login_state_code": text_at(system, "LoginState"),
            "server_type": text_at(system, "ServerType"),
            "system_state_code": text_at(system, "./Status/SysState"),
            "analyst": text_at(system, "./Status/Analyst"),
        }

    def get_method(self) -> dict[str, Any]:
        root, _ = self._post_xml("/cgi-bin/Method.cgi", XML_HEADER + "<Method><No>0</No></Method>")
        if root.tag != "Method":
            raise SCL40Error(f"Method 응답 루트가 예상과 다름: {root.tag}")
        pump = next((p for p in root.findall("./Pumps/Pump") if text_at(p, "UnitID") == "A"), None)
        return {
            "number": text_at(root, "No"),
            "alias": text_at(root, "Alias"),
            "flow": text_at(pump, "./Usual/Flow"),
            "tflow": text_at(pump, "./Usual/Tflow"),
            "pmax": text_at(pump, "./Usual/Pmax"),
            "pmin": text_at(pump, "./Detail/Pmin"),
        }

    def login(self, user_id: str, password: str) -> dict[str, Any]:
        if not user_id:
            raise SCL40Error("사용자 ID를 입력하세요.")
        root = ET.Element("Login")
        ET.SubElement(root, "Mode").text = "0"
        cert = ET.SubElement(root, "Certification")
        ET.SubElement(cert, "UserID").text = user_id
        ET.SubElement(cert, "Password").text = password
        ET.SubElement(cert, "SessionID")
        ET.SubElement(cert, "Result")
        body = XML_HEADER + ET.tostring(root, encoding="unicode")
        response, _ = self._post_xml("/cgi-bin/Login.cgi", body)
        result = text_at(response, "./Certification/Result")
        session_id = text_at(response, "./Certification/SessionID")
        if result != "0" or not session_id:
            messages = {
                "1": "사용자 ID 오류",
                "2": "비밀번호 오류",
                "4": "이미 로그인됨",
                "5": "다른 웹 세션이 사용 중",
                "6": "분석 실행 중이라 로그인할 수 없음",
                "7": "사용자 권한 부족",
                "9": "동일 애플리케이션이 이미 실행 중",
                "10": "시스템 잠김",
                "12": "LC 워크스테이션 연결 중",
                "13": "SCL-40 터치스크린에 사용자가 로그인되어 있음 — 터치스크린에서 먼저 로그아웃하세요",
            }
            raise SCL40Error(f"SCL-40 로그인 실패 ({result or 'unknown'}): {messages.get(result, '장비 응답 확인 필요')}")
        self.session_id = session_id
        self.user_id = user_id
        return {"logged_in": True, "user_id": user_id}

    def logout(self) -> dict[str, Any]:
        if not self.session_id:
            return {"logged_in": False}
        try:
            root, _ = self._post_xml("/cgi-bin/Login.cgi", XML_HEADER + "<Login><Mode>-1</Mode></Login>")
            if root.tag != "Login":
                raise SCL40Error(f"Logout 응답 루트가 예상과 다름: {root.tag}")
        finally:
            self.session_id = ""
            self.user_id = ""
        return {"logged_in": False}

    def get_monitor(self) -> dict[str, Any]:
        if not self.session_id:
            return {"available": False, "reason": "SCL-40 로그인이 필요합니다."}
        root, _ = self._post_xml(f"/cgi-bin/Monitor.cgi/{self.session_id}", XML_HEADER + "<Monitor/>")
        if root.tag != "Monitor":
            raise SCL40Error(f"Monitor 응답 루트가 예상과 다름: {root.tag}")
        sys_pumps = root.findall("./SysMon/Method/Pumps/Pump")
        pump = next((p for p in sys_pumps if text_at(p, "UnitID") == "A"), sys_pumps[0] if sys_pumps else None)
        sit_pumps = root.findall("./Config/Situation/Pumps/Pump")
        situation = next((p for p in sit_pumps if text_at(p, "UnitID") == "A"), sit_pumps[0] if sit_pumps else None)
        op_state = text_at(situation, "OpState")
        return {
            "available": True,
            "reason": "",
            "pressure": text_at(pump, "Press") or None,
            "pressure_unit_code": text_at(pump, "PressUnit"),
            "flow": text_at(pump, "Flow") or None,
            "pump_on": op_state == "1" if op_state else None,
            "op_state_code": op_state,
            "authority": text_at(root, "./AnalyMon/Authority") or text_at(root, ".//Authority"),
            "system_state_code": text_at(root, "./AnalyMon/SysState") or text_at(root, ".//SysState"),
            "error": None,
        }

    def snapshot(self) -> dict[str, Any]:
        started = time.perf_counter()
        config = self.get_config()
        summary = self.get_summary()
        method = self.get_method()
        try:
            monitor = self.get_monitor()
        except SCL40Error as exc:
            monitor = {"available": False, "reason": str(exc)}
        monitor.setdefault("pressure", None)
        monitor.setdefault("flow", None)
        monitor["target_flow"] = method.get("flow")
        monitor.setdefault("pump_on", None)
        monitor.setdefault("error", None)
        return {
            "ok": True,
            "host": self.host,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "config": config,
            "summary": summary,
            "method": method,
            "auth": {"logged_in": bool(self.session_id), "user_id": self.user_id},
            "monitor": monitor,
        }

    def send_pump(self, start: bool) -> dict[str, Any]:
        if not self.session_id:
            raise SCL40Error("START/STOP 전에 SCL-40 로그인이 필요합니다.")
        body = PUMP_START_REQUEST if start else PUMP_STOP_REQUEST
        root, raw = self._post_xml("/cgi-bin/Event.cgi", body)
        if root.tag != "Event":
            raise SCL40Error(f"Event 응답 루트가 예상과 다름: {root.tag}")
        echoed = text_at(root, "./Method/PumpBT")
        expected = "1" if start else "0"
        if echoed and echoed != expected:
            raise SCL40Error(f"Event 응답값 불일치: {echoed}")
        return {"response_root": root.tag, "response": raw[:2000]}

    def set_flow(self, value: Any) -> dict[str, Any]:
        if not self.session_id:
            raise SCL40Error("유량 변경 전에 SCL-40 로그인이 필요합니다.")
        try:
            flow = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise SCL40Error("유량은 숫자로 입력하세요.") from exc
        if not Decimal("0") <= flow <= Decimal("1.0"):
            raise SCL40Error("허용 유량 범위는 0.0000~1.0000 mL/min입니다.")
        flow_text = f"{flow.quantize(Decimal('0.0001')):.4f}"
        current = self.get_method()
        tflow = current.get("tflow") or "0.0000"
        root = ET.Element("Method")
        ET.SubElement(root, "No").text = current.get("number") or "0"
        pumps = ET.SubElement(root, "Pumps")
        pump = ET.SubElement(pumps, "Pump")
        ET.SubElement(pump, "UnitID").text = "A"
        usual = ET.SubElement(pump, "Usual")
        ET.SubElement(usual, "Flow").text = flow_text
        ET.SubElement(usual, "Tflow").text = tflow
        body = XML_HEADER + ET.tostring(root, encoding="unicode")
        response, _ = self._post_xml("/cgi-bin/Method.cgi", body)
        if response.tag != "Method":
            raise SCL40Error(f"Method 응답 루트가 예상과 다름: {response.tag}")
        readback = self.get_method()
        try:
            matched = Decimal(readback.get("flow") or "-1") == Decimal(flow_text)
        except InvalidOperation:
            matched = False
        if not matched:
            raise SCL40Error(f"유량 재조회 불일치: 요청 {flow_text}, 장비 {readback.get('flow') or 'unknown'}")
        return {"flow": readback["flow"], "previous_flow": current.get("flow"), "tflow": readback.get("tflow")}


def make_handler(client: SCL40Client, control_enabled: bool, log: logging.Logger, access_pin: str = ""):
    command_lock = threading.Lock()
    activity_lock = threading.Lock()
    control_activity: dict[str, Any] = {
        "busy": False,
        "operator_ip": "",
        "action": "",
        "timestamp": "",
        "result": "",
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "SCL40LocalGUI/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("browser %s - %s", self.address_string(), fmt % args)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _file(self, name: str, content_type: str) -> None:
            path = APP_DIR / name
            try:
                data = path.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 8192)
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise SCL40Error("잘못된 JSON 요청") from exc

        def _api_authorized(self) -> bool:
            if self.client_address[0] in ("127.0.0.1", "::1"):
                return True
            supplied = self.headers.get("X-SCL40-PIN", "")
            return bool(access_pin) and secrets.compare_digest(supplied, access_pin)

        def _require_api_access(self) -> bool:
            if self._api_authorized():
                return True
            self._json(401, {"ok": False, "error": "다른 PC에서 접속하려면 대시보드 PIN이 필요합니다.", "pin_required": True})
            return False

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/") and not self._require_api_access():
                return
            if self.path == "/":
                self._file("scl40_gui.html", "text/html; charset=utf-8")
            elif self.path == "/scl40_gui.css":
                self._file("scl40_gui.css", "text/css; charset=utf-8")
            elif self.path == "/scl40_gui_controls.css":
                self._file("scl40_gui_controls.css", "text/css; charset=utf-8")
            elif self.path == "/scl40_gui.js":
                self._file("scl40_gui.js", "text/javascript; charset=utf-8")
            elif self.path == "/api/snapshot":
                try:
                    with activity_lock:
                        activity = dict(control_activity)
                    self._json(200, {**client.snapshot(), "control_enabled": control_enabled, "control_activity": activity})
                except SCL40Error as exc:
                    log.warning("snapshot failed: %s", exc)
                    self._json(502, {"ok": False, "error": str(exc), "host": client.host, "control_enabled": control_enabled})
            elif self.path == "/api/info":
                self._json(200, {"host": client.host, "control_enabled": control_enabled, "read_only": not control_enabled})
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            try:
                if self.path.startswith("/api/") and not self._require_api_access():
                    return
                body = self._body()
                if self.path == "/api/login":
                    result = client.login(str(body.get("user_id", "")), str(body.get("password", "")))
                    log.warning("SCL-40 login succeeded for user %s", result["user_id"])
                    self._json(200, {"ok": True, **result})
                    return
                if self.path == "/api/logout":
                    result = client.logout()
                    log.warning("SCL-40 logout completed")
                    self._json(200, {"ok": True, **result})
                    return

                if self.path in ("/api/control/start", "/api/control/stop"):
                    action = "START" if self.path.endswith("start") else "STOP"
                    if not control_enabled:
                        self._json(403, {"ok": False, "error": "서버가 읽기 전용 모드입니다."})
                        return
                    if body.get("confirmation") != action:
                        self._json(400, {"ok": False, "error": f"{action} 확인이 필요합니다."})
                        return
                    if not command_lock.acquire(blocking=False):
                        self._json(409, {"ok": False, "error": "다른 PC의 장비 명령을 처리 중입니다. 잠시 후 다시 시도하세요."})
                        return
                    try:
                        with activity_lock:
                            control_activity.update(busy=True, operator_ip=self.client_address[0], action=action, timestamp=datetime.now().astimezone().isoformat(timespec="seconds"), result="processing")
                        log.warning("sending confirmed pump command: %s from %s", action, self.client_address[0])
                        result = client.send_pump(start=(action == "START"))
                        with activity_lock:
                            control_activity.update(busy=False, result="success")
                        self._json(200, {"ok": True, "action": action, **result})
                    except Exception:
                        with activity_lock:
                            control_activity.update(busy=False, result="failed")
                        raise
                    finally:
                        command_lock.release()
                    return
                if self.path == "/api/control/set-flow":
                    if not control_enabled:
                        self._json(403, {"ok": False, "error": "서버가 읽기 전용 모드입니다."})
                        return
                    if body.get("confirmation") != "SET_FLOW":
                        self._json(400, {"ok": False, "error": "SET_FLOW 확인이 필요합니다."})
                        return
                    if not command_lock.acquire(blocking=False):
                        self._json(409, {"ok": False, "error": "다른 PC의 장비 명령을 처리 중입니다. 잠시 후 다시 시도하세요."})
                        return
                    try:
                        with activity_lock:
                            control_activity.update(busy=True, operator_ip=self.client_address[0], action="SET FLOW", timestamp=datetime.now().astimezone().isoformat(timespec="seconds"), result="processing")
                        result = client.set_flow(body.get("flow"))
                        log.warning("confirmed flow change from %s: %s -> %s mL/min", self.client_address[0], result.get("previous_flow"), result["flow"])
                        with activity_lock:
                            control_activity.update(busy=False, result="success")
                        self._json(200, {"ok": True, **result})
                    except Exception:
                        with activity_lock:
                            control_activity.update(busy=False, result="failed")
                        raise
                    finally:
                        command_lock.release()
                    return
                self.send_error(404)
            except SCL40Error as exc:
                log.error("request failed: %s", exc)
                self._json(502, {"ok": False, "error": str(exc)})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Shimadzu SCL-40 local dashboard")
    parser.add_argument("host", nargs="?", default="192.168.200.99", help="SCL-40 IP address")
    parser.add_argument("--port", type=int, default=8765, help="local dashboard port")
    parser.add_argument("--bind", default="127.0.0.1", help="dashboard listen address")
    parser.add_argument("--access-pin-file", type=Path, help="PIN file required for non-local API access")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser automatically")
    parser.add_argument("--enable-control", action="store_true", help="enable START/STOP endpoints")
    args = parser.parse_args()

    access_pin = ""
    if args.access_pin_file:
        try:
            access_pin = args.access_pin_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            parser.error(f"cannot read access PIN file: {exc}")
        if len(access_pin) < 6:
            parser.error("access PIN must contain at least 6 characters")

    log_path = APP_DIR / f"scl40_gui_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    log = logging.getLogger("scl40_gui")
    client = SCL40Client(args.host)
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(client, args.enable_control, log, access_pin))
    url = f"http://127.0.0.1:{args.port}/"
    log.info("SCL-40 target: %s", args.host)
    log.info("Dashboard: %s", url)
    log.info("Listen address: %s:%s", args.bind, args.port)
    log.info("Remote API PIN: %s", "enabled" if access_pin else "disabled")
    log.info("Mode: %s", "CONTROL" if args.enable_control else "READ ONLY")
    log.info("Log: %s", log_path)
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
