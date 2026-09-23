#!/usr/bin/env python3
"""Local/LAN dashboard for Shimadzu SCL-40 with gated control endpoints."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scl40_jetrun import JetRunController, RunError
from scl40_store import AuditStore, CommunicationHealth


SOURCE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SCL40-Control"
else:
    DATA_DIR = SOURCE_DIR
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

# Writable Method 0 fields for a selected pump. Element names and their position in the
# document come from the live Method.cgi read response, not from guesswork.
# `order` is the sequence the instrument itself uses inside each section.
PARAM_SPEC: dict[str, dict[str, Any]] = {
    "flow": {
        "section": "Usual", "tag": "Flow", "order": 0, "decimals": 4,
        "label": "유량", "unit": "mL/min",
        "min": Decimal("0"), "max": Decimal("5.0"),
    },
    "tflow": {
        "section": "Usual", "tag": "Tflow", "order": 1, "decimals": 4,
        "label": "총 유량", "unit": "mL/min",
        "min": Decimal("0"), "max": Decimal("5.0"),
    },
    "pmax": {
        "section": "Usual", "tag": "Pmax", "order": 2, "decimals": 1,
        "label": "압력 상한", "unit": "MPa",
        "min": Decimal("0"), "max": Decimal("10.0"),
    },
    "pmin": {
        "section": "Detail", "tag": "Pmin", "order": 0, "decimals": 1,
        "label": "압력 하한", "unit": "MPa",
        "min": Decimal("0"), "max": Decimal("10.0"),
    },
}

# Flow and Tflow always travel together: that pairing is the write request shape
# already confirmed against the instrument.
ALWAYS_SENT = ("flow", "tflow")
USER_WRITABLE_PARAMS = frozenset(("flow", "pmax", "pmin"))


class RolePolicy:
    """Map the active SCL account to dashboard permissions.

    SCL-40 has one instrument session, so this policy deliberately follows that
    session.  Admin is an administrator by default; other authenticated users
    are operators; a logged-out dashboard is view-only.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = {str(k).casefold(): str(v).lower() for k, v in (mapping or {}).items()}
        invalid = {role for role in self.mapping.values() if role not in {"admin", "operator", "viewer"}}
        if invalid:
            raise ValueError(f"invalid dashboard roles: {', '.join(sorted(invalid))}")

    def role_for(self, user_id: str) -> str:
        if not user_id:
            return "viewer"
        return self.mapping.get(user_id.casefold(), "admin" if user_id.casefold() == "admin" else "operator")

    def describe(self, user_id: str) -> dict[str, Any]:
        role = self.role_for(user_id)
        return {
            "role": role,
            "control": role in {"admin", "operator"},
            "pressure_limits": role == "admin",
            "acknowledge_alarms": role in {"admin", "operator"},
        }


class SCL40Error(RuntimeError):
    pass


def text_at(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    value = node.findtext(path)
    return value.strip() if value else default


class SCL40Client:
    def __init__(self, host: str, timeout: float = 3.0, pressure_ceiling: Decimal | None = None) -> None:
        self.host = host
        self.timeout = timeout
        self.base_url = f"http://{host}"
        self._config_cache: dict[str, Any] | None = None
        self._config_cache_time = 0.0
        self.session_id = ""
        self.user_id = ""
        self.limits = {key: dict(spec) for key, spec in PARAM_SPEC.items()}
        if pressure_ceiling is not None:
            for key in ("pmax", "pmin"):
                self.limits[key]["max"] = pressure_ceiling

    def limit_table(self) -> dict[str, dict[str, Any]]:
        """Serialisable copy of the write limits, shared with the browser."""
        return {
            key: {
                "label": spec["label"], "unit": spec["unit"], "decimals": spec["decimals"],
                "min": str(spec["min"]), "max": str(spec["max"]),
            }
            for key, spec in self.limits.items()
            if key in USER_WRITABLE_PARAMS
        }

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

    @staticmethod
    def _method_values(root: ET.Element, pump: ET.Element) -> dict[str, Any]:
        return {
            "unit_id": text_at(pump, "UnitID"),
            "number": text_at(root, "No"),
            "alias": text_at(root, "Alias"),
            "flow": text_at(pump, "./Usual/Flow"),
            "tflow": text_at(pump, "./Usual/Tflow"),
            "pmax": text_at(pump, "./Usual/Pmax"),
            "pmin": text_at(pump, "./Detail/Pmin"),
        }

    def get_methods(self) -> dict[str, Any]:
        root, _ = self._post_xml("/cgi-bin/Method.cgi", XML_HEADER + "<Method><No>0</No></Method>")
        if root.tag != "Method":
            raise SCL40Error(f"Method 응답 루트가 예상과 다름: {root.tag}")
        connected = {pump["unit_id"] for pump in self.get_config().get("pumps", [])}
        return {
            "number": text_at(root, "No"),
            "alias": text_at(root, "Alias"),
            "pumps": [
                self._method_values(root, pump)
                for pump in root.findall("./Pumps/Pump")
                if text_at(pump, "UnitID") in connected
            ],
        }

    def get_method(self, unit_id: str = "A") -> dict[str, Any]:
        unit_id = unit_id.strip().upper()
        methods = self.get_methods()
        method = next((pump for pump in methods["pumps"] if pump["unit_id"] == unit_id), None)
        if method is None:
            raise SCL40Error(f"연결된 Pump Unit {unit_id}를 Method에서 찾지 못했습니다.")
        return method

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

    def get_monitors(self) -> dict[str, Any]:
        if not self.session_id:
            return {"available": False, "reason": "SCL-40 로그인이 필요합니다.", "pumps": []}
        root, _ = self._post_xml(f"/cgi-bin/Monitor.cgi/{self.session_id}", XML_HEADER + "<Monitor/>")
        if root.tag != "Monitor":
            raise SCL40Error(f"Monitor 응답 루트가 예상과 다름: {root.tag}")
        sys_pumps = root.findall("./SysMon/Method/Pumps/Pump")
        sit_pumps = root.findall("./Config/Situation/Pumps/Pump")
        situations = {text_at(pump, "UnitID"): pump for pump in sit_pumps}
        connected = {pump["unit_id"] for pump in self.get_config().get("pumps", [])}
        pumps = []
        for pump in sys_pumps:
            unit_id = text_at(pump, "UnitID")
            if unit_id not in connected:
                continue
            op_state = text_at(situations.get(unit_id), "OpState")
            pumps.append({
                "unit_id": unit_id,
                "available": True,
                "reason": "",
                "pressure": text_at(pump, "Press") or None,
                "pressure_unit_code": text_at(pump, "PressUnit"),
                "flow": text_at(pump, "Flow") or None,
                "pump_on": op_state == "1" if op_state else None,
                "op_state_code": op_state,
                "error": None,
            })
        return {
            "available": True,
            "reason": "",
            "authority": text_at(root, "./AnalyMon/Authority") or text_at(root, ".//Authority"),
            "system_state_code": text_at(root, "./AnalyMon/SysState") or text_at(root, ".//SysState"),
            "error": None,
            "pumps": pumps,
        }

    def get_monitor(self, unit_id: str = "A") -> dict[str, Any]:
        monitors = self.get_monitors()
        if not monitors.get("available"):
            return monitors
        unit_id = unit_id.strip().upper()
        monitor = next((pump for pump in monitors["pumps"] if pump["unit_id"] == unit_id), None)
        if monitor is None:
            raise SCL40Error(f"연결된 Pump Unit {unit_id}를 Monitor에서 찾지 못했습니다.")
        return {
            **monitor,
            "authority": monitors.get("authority"),
            "system_state_code": monitors.get("system_state_code"),
        }

    def snapshot(self) -> dict[str, Any]:
        started = time.perf_counter()
        config = self.get_config()
        summary = self.get_summary()
        methods = self.get_methods()
        try:
            monitors = self.get_monitors()
        except SCL40Error as exc:
            monitors = {"available": False, "reason": str(exc), "pumps": []}
        method_map = {pump["unit_id"]: pump for pump in methods.get("pumps", [])}
        monitor_map = {pump["unit_id"]: pump for pump in monitors.get("pumps", [])}
        pumps = []
        for pump_config in config.get("pumps", []):
            unit_id = pump_config["unit_id"]
            method = method_map.get(unit_id, {"unit_id": unit_id, "number": methods.get("number"), "alias": methods.get("alias")})
            monitor = monitor_map.get(unit_id, {
                "unit_id": unit_id, "available": False,
                "reason": monitors.get("reason", "Monitor 응답에 펌프가 없습니다."),
                "pressure": None, "flow": None, "pump_on": None, "op_state_code": "", "error": None,
            })
            monitor = {
                **monitor,
                "target_flow": method.get("flow"),
                "authority": monitors.get("authority"),
                "system_state_code": monitors.get("system_state_code"),
            }
            pumps.append({**pump_config, "method": method, "monitor": monitor})
        primary = pumps[0] if pumps else {"method": {}, "monitor": {"available": False, "reason": "연결된 펌프가 없습니다."}}
        return {
            "ok": True,
            "host": self.host,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "config": config,
            "summary": summary,
            "methods": methods,
            "pumps": pumps,
            "method": primary["method"],
            "auth": {"logged_in": bool(self.session_id), "user_id": self.user_id},
            "monitor": primary["monitor"],
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

    def _format(self, key: str, value: Any, decimals: int | None = None) -> str:
        spec = self.limits[key]
        decimals = spec["decimals"] if decimals is None else decimals
        quantum = Decimal(1).scaleb(-decimals)
        return f"{Decimal(str(value)).quantize(quantum):.{decimals}f}"

    def _validate(self, key: str, raw: Any, decimals: int | None = None) -> str:
        spec = self.limits.get(key)
        if spec is None:
            raise SCL40Error(f"알 수 없는 파라미터: {key}")
        if key not in USER_WRITABLE_PARAMS:
            raise SCL40Error(f"{spec['label']} 파라미터는 대시보드에서 변경할 수 없습니다.")
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise SCL40Error(f"{spec['label']} 값은 숫자로 입력하세요.") from exc
        if not spec["min"] <= value <= spec["max"]:
            raise SCL40Error(
                f"{spec['label']} 허용 범위는 {spec['min']}~{spec['max']} {spec['unit']}입니다."
            )
        return self._format(key, value, decimals)

    def preview_method_params(self, updates: dict[str, Any], unit_id: str = "A") -> dict[str, Any]:
        """Build and validate the exact Method.cgi request without sending it."""
        if not self.session_id:
            raise SCL40Error("파라미터 확인 전에 SCL-40 로그인이 필요합니다.")
        if not updates:
            raise SCL40Error("확인할 파라미터가 없습니다.")

        unit_id = unit_id.strip().upper()
        current = self.get_method(unit_id)
        decimals = {}
        for key in ("flow", "tflow"):
            current_value = str(current.get(key) or "")
            decimals[key] = len(current_value.rsplit(".", 1)[1]) if "." in current_value else self.limits[key]["decimals"]
        values = {key: self._validate(key, raw, decimals.get(key)) for key, raw in updates.items()}
        for key in ALWAYS_SENT:
            if key not in values:
                values[key] = self._format(key, current.get(key) or 0, decimals.get(key))

        pmax = Decimal(values.get("pmax") or current.get("pmax") or "0")
        pmin = Decimal(values.get("pmin") or current.get("pmin") or "0")
        if pmax > 0 and pmin >= pmax:
            raise SCL40Error(f"압력 하한({pmin})은 상한({pmax})보다 작아야 합니다.")

        root = ET.Element("Method")
        ET.SubElement(root, "No").text = current.get("number") or "0"
        pumps = ET.SubElement(root, "Pumps")
        pump = ET.SubElement(pumps, "Pump")
        ET.SubElement(pump, "UnitID").text = unit_id
        for section in ("Usual", "Detail"):
            keys = [k for k in values if self.limits[k]["section"] == section]
            if keys:
                node = ET.SubElement(pump, section)
                for key in sorted(keys, key=lambda k: self.limits[k]["order"]):
                    ET.SubElement(node, self.limits[key]["tag"]).text = values[key]
        request = XML_HEADER + ET.tostring(root, encoding="unicode")
        return {"unit_id": unit_id, "before": current, "values": values, "request": request}

    def set_method_params(self, updates: dict[str, Any], unit_id: str = "A") -> dict[str, Any]:
        """Write Method 0 pump parameters and verify every field by readback.

        Only element names and positions observed in the instrument's own
        Method.cgi read response are emitted. A field the instrument silently
        ignores fails the readback check instead of passing unnoticed.
        """
        preview = self.preview_method_params(updates, unit_id)
        unit_id, current, values, body = (
            preview["unit_id"], preview["before"], preview["values"], preview["request"]
        )
        response, _ = self._post_xml("/cgi-bin/Method.cgi", body)
        if response.tag != "Method":
            raise SCL40Error(f"Method 응답 루트가 예상과 다름: {response.tag}")

        readback = self.get_method(unit_id)
        mismatched = []
        for key, sent in values.items():
            try:
                if Decimal(readback.get(key) or "nan") != Decimal(sent):
                    mismatched.append(f"{self.limits[key]['label']} 요청 {sent} / 장비 {readback.get(key) or 'unknown'}")
            except InvalidOperation:
                mismatched.append(f"{self.limits[key]['label']} 재조회 값 없음")
        if mismatched:
            raise SCL40Error("파라미터 재조회 불일치: " + "; ".join(mismatched))

        applied = {
            key: {
                "label": self.limits[key]["label"],
                "unit": self.limits[key]["unit"],
                "previous": current.get(key),
                "value": readback.get(key),
                "changed": key in updates,
            }
            for key in values
        }
        return {"unit_id": unit_id, "applied": applied, "request": body, "method": readback}


class TrendRecorder:
    """Server-side pressure/flow history shared by every browser.

    The run controller samples at 1 Hz while a run is active and each dashboard
    poll contributes a sample too, so the trace survives page reloads and is
    identical on every connected client.
    """

    def __init__(self, store: AuditStore | None = None, capacity: int = 7200, min_interval: float = 0.5) -> None:
        self._samples: dict[str, deque[tuple[float, float | None, float | None, float | None]]] = {}
        self._capacity = capacity
        self._lock = threading.Lock()
        self._min_interval = min_interval
        self._store = store
        self._last_clock: dict[str, float] = {}

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def record(
        self, pressure: Any, flow: Any, target: Any, unit_id: str = "A",
        pump_on: bool | None = None, error_code: str = "",
    ) -> None:
        values = (self._number(pressure), self._number(flow), self._number(target))
        if values[0] is None and values[1] is None:
            return
        now = round(time.time(), 3)
        with self._lock:
            samples = self._samples.setdefault(unit_id, deque(maxlen=self._capacity))
            # Thinning is decided on the real clock. Comparing it against the
            # stored stamp instead would drop samples whenever rounding put the
            # stored value ahead of the raw reading.
            if now - self._last_clock.get(unit_id, float("-inf")) < self._min_interval:
                return
            stamp = now
            if samples and stamp <= samples[-1][0]:
                # Stamps must stay strictly increasing: an incremental fetch
                # asks for everything after `since`, so two samples sharing a
                # stamp would lose one of them permanently.
                stamp = round(samples[-1][0] + 0.001, 3)
            self._last_clock[unit_id] = now
            samples.append((stamp, *values))
        if self._store:
            self._store.record_sample(unit_id, pressure, flow, target, pump_on, error_code, ts=now)

    def series(self, since: float = 0.0, unit_id: str = "A") -> tuple[list[list[Any]], float]:
        with self._lock:
            stored = self._samples.get(unit_id)
            if stored is None:
                persisted = self._store.recent_telemetry(unit_id, self._capacity) if self._store else []
                stored = deque((tuple(item) for item in persisted), maxlen=self._capacity)
                self._samples[unit_id] = stored
            samples = [list(item) for item in stored if item[0] > since]
            until = stored[-1][0] if stored else since
        return samples, until


class CommandBusy(RuntimeError):
    pass


class SnapshotCache:
    """Coalesce near-simultaneous dashboard polls into one instrument read."""

    def __init__(self, ttl: float = 0.8) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._value: dict[str, Any] | None = None
        self._stored_at = 0.0

    def get(self, producer: Any) -> tuple[dict[str, Any], bool]:
        with self._lock:
            now = time.monotonic()
            if self._value is not None and now - self._stored_at <= self._ttl:
                return self._value, False
            value = producer()
            self._value = value
            self._stored_at = time.monotonic()
            return value, True

    def invalidate(self) -> None:
        with self._lock:
            self._value = None
            self._stored_at = 0.0


class CommandBroker:
    """Serialises every instrument write, whoever issues it.

    The browser and the pressure watchdog both go through here, so a watchdog
    STOP can never interleave with an operator command.
    """

    def __init__(self, store: AuditStore, user_getter: Any) -> None:
        self._command_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._activity: dict[str, Any] = {
            "busy": False, "operator_ip": "", "action": "", "timestamp": "", "result": "",
        }
        self._store = store
        self._user_getter = user_getter

    def activity(self) -> dict[str, Any]:
        with self._activity_lock:
            return dict(self._activity)

    def execute(self, action: str, operator: str, work: Any) -> Any:
        if not self._command_lock.acquire(blocking=False):
            raise CommandBusy("다른 장비 명령을 처리 중입니다. 잠시 후 다시 시도하세요.")
        try:
            with self._activity_lock:
                self._activity.update(
                    busy=True, operator_ip=operator, action=action,
                    timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
                    result="processing",
                )
            result = work()
            with self._activity_lock:
                self._activity.update(busy=False, result="success")
            self._store.record_event(
                "command", action, "success", user_id=self._user_getter(), operator_ip=operator,
                message=f"{action} 완료",
            )
            return result
        except Exception as exc:
            with self._activity_lock:
                self._activity.update(busy=False, result="failed")
            self._store.record_event(
                "command", action, "failed", user_id=self._user_getter(), operator_ip=operator,
                message=str(exc),
            )
            raise
        finally:
            self._command_lock.release()


def make_handler(
    client: SCL40Client,
    broker: CommandBroker,
    jetrun: JetRunController,
    recorder: TrendRecorder,
    store: AuditStore,
    health: CommunicationHealth,
    roles: RolePolicy,
    control_enabled: bool,
    log: logging.Logger,
    access_pin: str = "",
    simulated: bool = False,
):
    snapshot_cache = SnapshotCache()

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
            path = RESOURCE_DIR / name
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

        def _bytes(self, status: int, data: bytes, content_type: str, filename: str = "") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict[str, Any]:
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 8192)
                value = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(value, dict):
                    raise SCL40Error("JSON 요청은 객체여야 합니다.")
                return value
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise SCL40Error("잘못된 JSON 요청") from exc

        def _api_authorized(self) -> bool:
            if not access_pin:
                return True
            if self.client_address[0] in ("127.0.0.1", "::1"):
                return True
            supplied = self.headers.get("X-SCL40-PIN", "")
            return secrets.compare_digest(supplied, access_pin)

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
            elif self.path == "/scl40_gui.js":
                self._file("scl40_gui.js", "text/javascript; charset=utf-8")
            elif self.path == "/ibs_watermark.png":
                self._file("ibs_watermark.png", "image/png")
            elif self.path.split("?", 1)[0] == "/api/trend":
                query = parse_qs(urlparse(self.path).query)
                try:
                    since = float(query.get("since", ["0"])[0])
                except (TypeError, ValueError):
                    since = 0.0
                unit_id = str(query.get("unit", ["A"])[0]).strip().upper()
                samples, until = recorder.series(since, unit_id)
                self._json(200, {"ok": True, "unit_id": unit_id, "samples": samples, "until": until})
            elif self.path.split("?", 1)[0] == "/api/export.csv":
                query = parse_qs(urlparse(self.path).query)
                kind = str(query.get("kind", ["telemetry"])[0])
                try:
                    data = store.export_csv(kind)
                except ValueError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                self._bytes(200, data, "text/csv; charset=utf-8", f"scl40_{kind}_{datetime.now():%Y%m%d_%H%M%S}.csv")
            elif self.path.split("?", 1)[0] == "/api/records":
                query = parse_qs(urlparse(self.path).query)
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except (TypeError, ValueError):
                    self._json(400, {"ok": False, "error": "limit은 정수여야 합니다."})
                    return
                self._json(200, {"ok": True, "events": store.recent_events(limit), "stats": store.stats()})
            elif self.path.split("?", 1)[0] == "/api/alarms":
                query = parse_qs(urlparse(self.path).query)
                active_only = query.get("active", ["0"])[0] == "1"
                self._json(200, {"ok": True, "alarms": store.recent_alarms(100, active_only)})
            elif self.path == "/api/capabilities":
                self._json(200, {"ok": True, **self._capabilities()})
            elif self.path == "/api/snapshot":
                try:
                    snapshot, fresh = snapshot_cache.get(client.snapshot)
                    if fresh:
                        health.success(snapshot.get("latency_ms"))
                        for pump in snapshot.get("pumps", []):
                            monitor = pump.get("monitor") or {}
                            if monitor.get("available"):
                                recorder.record(
                                    monitor.get("pressure"), monitor.get("flow"), monitor.get("target_flow"),
                                    pump.get("unit_id") or "A", monitor.get("pump_on"), monitor.get("error") or "",
                                )
                                store.resolve_alarm("MONITOR_UNAVAILABLE", pump.get("unit_id") or "")
                            elif snapshot.get("auth", {}).get("logged_in"):
                                store.raise_alarm(
                                    "MONITOR_UNAVAILABLE", monitor.get("reason") or "모니터 데이터 없음",
                                    unit_id=pump.get("unit_id") or "",
                                )
                    authorization = roles.describe(client.user_id)
                    self._json(200, {
                        **snapshot,
                        "snapshot_cached": not fresh,
                        "control_enabled": control_enabled,
                        "control_activity": broker.activity(),
                        "simulated": simulated,
                        "limits": client.limit_table(),
                        "run": jetrun.state(),
                        "communication": health.snapshot(),
                        "authorization": authorization,
                        "records": store.stats(),
                        "alarms": store.recent_alarms(8, active_only=True),
                        "recent_events": store.recent_events(8),
                        "capabilities": self._capabilities(),
                    })
                except SCL40Error as exc:
                    log.warning("snapshot failed: %s", exc)
                    health.failure(exc)
                    self._json(502, {
                        "ok": False, "error": str(exc), "host": client.host,
                        "control_enabled": control_enabled, "simulated": simulated,
                        "run": jetrun.state(),
                        "communication": health.snapshot(),
                        "alarms": store.recent_alarms(8, active_only=True),
                    })
            elif self.path == "/api/info":
                self._json(200, {
                    "host": client.host, "control_enabled": control_enabled,
                    "read_only": not control_enabled, "simulated": simulated,
                    "limits": client.limit_table(),
                    "authorization": roles.describe(client.user_id),
                    "capabilities": self._capabilities(),
                })
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            try:
                if self.path.startswith("/api/") and not self._require_api_access():
                    return
                body = self._body()
                if self.path == "/api/login":
                    result = client.login(str(body.get("user_id", "")), str(body.get("password", "")))
                    snapshot_cache.invalidate()
                    store.record_event(
                        "session", "LOGIN", "success", user_id=result["user_id"],
                        operator_ip=self.client_address[0], message="SCL-40 로그인",
                    )
                    log.warning("SCL-40 login succeeded for user %s", result["user_id"])
                    self._json(200, {"ok": True, **result})
                    return
                if self.path == "/api/logout":
                    previous_user = client.user_id
                    result = client.logout()
                    snapshot_cache.invalidate()
                    store.record_event(
                        "session", "LOGOUT", "success", user_id=previous_user,
                        operator_ip=self.client_address[0], message="SCL-40 로그아웃",
                    )
                    log.warning("SCL-40 logout completed")
                    self._json(200, {"ok": True, **result})
                    return

                if self.path == "/api/verification/preview":
                    if not self._role_allowed("control"):
                        return
                    params = body.get("params") or {}
                    unit_id = str(body.get("unit_id") or "A").strip().upper()
                    if not isinstance(params, dict):
                        self._json(400, {"ok": False, "error": "params는 객체여야 합니다."})
                        return
                    result = client.preview_method_params(params, unit_id)
                    store.record_event(
                        "verification", "PREVIEW_METHOD", "success", user_id=client.user_id,
                        operator_ip=self.client_address[0], unit_id=unit_id,
                        message="전송하지 않고 Method 요청 검증", details={"request": result["request"]},
                    )
                    self._json(200, {"ok": True, **result, "sent": False})
                    return

                if self.path == "/api/alarms/ack":
                    if not self._role_allowed("acknowledge_alarms"):
                        return
                    try:
                        alarm_id = int(body.get("alarm_id") or 0)
                    except (TypeError, ValueError):
                        self._json(400, {"ok": False, "error": "alarm_id는 정수여야 합니다."})
                        return
                    if not store.acknowledge_alarm(alarm_id, client.user_id):
                        self._json(404, {"ok": False, "error": "알람을 찾지 못했습니다."})
                        return
                    store.record_event(
                        "alarm", "ACKNOWLEDGE", "success", user_id=client.user_id,
                        operator_ip=self.client_address[0], message=f"알람 #{alarm_id} 확인",
                    )
                    self._json(200, {"ok": True, "alarm_id": alarm_id})
                    return

                if self.path in ("/api/control/start", "/api/control/stop"):
                    action = "START" if self.path.endswith("start") else "STOP"
                    if not self._control_allowed(body, action):
                        return
                    log.warning("sending confirmed pump command: %s from %s", action, self.client_address[0])
                    result = broker.execute(
                        action, self.client_address[0],
                        lambda: client.send_pump(start=(action == "START")),
                    )
                    snapshot_cache.invalidate()
                    if action == "STOP":
                        jetrun.abort("수동 STOP", self.client_address[0], stop_pump=False)
                    self._json(200, {"ok": True, "action": action, **result})
                    return

                if self.path == "/api/control/method":
                    if not self._control_allowed(body, "SET_METHOD"):
                        return
                    params = body.get("params") or {}
                    unit_id = str(body.get("unit_id") or "A").strip().upper()
                    if not isinstance(params, dict):
                        self._json(400, {"ok": False, "error": "params는 객체여야 합니다."})
                        return
                    if any(key in params for key in ("pmax", "pmin")) and not self._role_allowed("pressure_limits"):
                        return
                    result = broker.execute(
                        f"SET METHOD {unit_id}", self.client_address[0],
                        lambda: client.set_method_params(params, unit_id),
                    )
                    snapshot_cache.invalidate()
                    changes = ", ".join(
                        f"{item['label']} {item['previous']} -> {item['value']} {item['unit']}"
                        for item in result["applied"].values() if item["changed"]
                    )
                    log.warning("method write for Unit %s from %s: %s", unit_id, self.client_address[0], changes or "no change")
                    log.info("method write request: %s", result["request"])
                    self._json(200, {"ok": True, **result})
                    return

                if self.path == "/api/run/start":
                    if not self._control_allowed(body, "START_RUN"):
                        return
                    state = jetrun.start(body, self.client_address[0])
                    snapshot_cache.invalidate()
                    self._json(200, {"ok": True, "run": state})
                    return

                if self.path == "/api/run/abort":
                    if not control_enabled:
                        self._json(403, {"ok": False, "error": "서버가 읽기 전용 모드입니다."})
                        return
                    state = jetrun.abort(
                        str(body.get("reason") or "사용자 중단"),
                        self.client_address[0],
                    )
                    snapshot_cache.invalidate()
                    self._json(200, {"ok": True, "run": state})
                    return

                self.send_error(404)
            except CommandBusy as exc:
                self._json(409, {"ok": False, "error": str(exc)})
            except RunError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            except SCL40Error as exc:
                log.error("request failed: %s", exc)
                self._json(502, {"ok": False, "error": str(exc)})

        def _capabilities(self) -> dict[str, Any]:
            return {
                "global_start_stop": {
                    "confirmed": True,
                    "scope": "all_connected_pumps",
                    "endpoint": "/cgi-bin/Event.cgi",
                },
                "individual_start_stop": {
                    "confirmed": False,
                    "reason": "확인된 Event.cgi 요청에 UnitID가 없어 개별 START/STOP은 제공하지 않습니다.",
                },
                "per_pump_method": {
                    "confirmed": True,
                    "fields": ["flow", "pmax", "pmin"],
                    "verification": "readback",
                },
                "per_module_purge": {
                    "confirmed": False,
                    "discovered_read_only": True,
                    "evidence": "SCL web purge.js uses SelModuleNo/PurgeAct, but no purge write has been sent or validated.",
                    "enabled": False,
                },
            }

        def _role_allowed(self, capability: str) -> bool:
            authorization = roles.describe(client.user_id)
            if authorization.get(capability):
                return True
            self._json(403, {
                "ok": False,
                "error": f"현재 역할({authorization['role']})에는 {capability} 권한이 없습니다.",
            })
            return False

        def _control_allowed(self, body: dict[str, Any], token: str) -> bool:
            if not control_enabled:
                self._json(403, {"ok": False, "error": "서버가 읽기 전용 모드입니다."})
                return False
            if not self._role_allowed("control"):
                return False
            if body.get("confirmation") != token:
                self._json(400, {"ok": False, "error": f"{token} 확인이 필요합니다."})
                return False
            return True

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shimadzu SCL-40 local dashboard")
    parser.add_argument("host", nargs="?", default="192.168.200.99", help="SCL-40 IP address")
    parser.add_argument("--port", type=int, default=8765, help="local dashboard port")
    parser.add_argument("--bind", default="127.0.0.1", help="dashboard listen address")
    parser.add_argument("--access-pin-file", type=Path, help="optional PIN file for an extra non-local API access check")
    parser.add_argument("--roles-file", type=Path, help="optional JSON mapping of SCL users to admin/operator/viewer")
    parser.add_argument("--history-db", type=Path, default=DATA_DIR / "scl40_history.sqlite3", help="SQLite history database")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser automatically")
    parser.add_argument("--enable-control", action="store_true", help="enable START/STOP endpoints")
    parser.add_argument(
        "--simulator",
        action="store_true",
        help="run against a built-in offline SCL-40 simulator instead of real hardware",
    )
    parser.add_argument("--sim-port", type=int, default=9099, help="port for the built-in simulator")
    parser.add_argument("--sim-latency", type=float, default=0.0, help="simulated response delay in seconds")
    parser.add_argument("--sim-reservoir", type=float, default=0.15, help="simulated reservoir volume in mL")
    parser.add_argument("--sim-sample", type=float, default=0.05, help="simulated sample volume in mL")
    parser.add_argument(
        "--pressure-ceiling",
        type=Decimal,
        default=Decimal("10.0"),
        help="highest pressure limit (MPa) an operator may write to the method (default: 10.0)",
    )
    parser.add_argument(
        "--guard-interval",
        type=float,
        default=1.0,
        help="pressure watchdog polling interval in seconds (default: 1.0)",
    )
    args = parser.parse_args(argv)

    if args.pressure_ceiling <= 0:
        parser.error("--pressure-ceiling must be greater than zero")
    if not 0.2 <= args.guard_interval <= 10:
        parser.error("--guard-interval must be between 0.2 and 10 seconds")

    access_pin = ""
    if args.access_pin_file:
        try:
            access_pin = args.access_pin_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            parser.error(f"cannot read access PIN file: {exc}")
        if len(access_pin) < 6:
            parser.error("access PIN must contain at least 6 characters")

    role_mapping: dict[str, str] = {}
    if args.roles_file:
        try:
            loaded_roles = json.loads(args.roles_file.read_text(encoding="utf-8"))
            if not isinstance(loaded_roles, dict):
                raise ValueError("top-level value must be an object")
            role_mapping = {str(key): str(value) for key, value in loaded_roles.items()}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read roles file: {exc}")
    try:
        roles = RolePolicy(role_mapping)
    except ValueError as exc:
        parser.error(str(exc))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / f"scl40_gui_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    log = logging.getLogger("scl40_gui")

    sim_server = None
    sim_device = None
    target_host = args.host
    if args.simulator:
        from scl40_sim import start_simulator

        sim_server, sim_device = start_simulator(
            args.sim_port, "127.0.0.1", latency=args.sim_latency,
            reservoir_volume=args.sim_reservoir, sample_volume=args.sim_sample,
        )
        target_host = f"127.0.0.1:{args.sim_port}"
        log.warning("SIMULATOR MODE - no real instrument is connected")

    client = SCL40Client(target_host, pressure_ceiling=args.pressure_ceiling)
    store = AuditStore(args.history_db)
    health = CommunicationHealth(store)
    broker = CommandBroker(store, lambda: client.user_id)
    recorder = TrendRecorder(store)
    jetrun = JetRunController(
        client, broker.execute, log,
        poll_seconds=args.guard_interval, on_sample=recorder.record,
        on_event=lambda event: store.record_event(
            "run", str(event.get("kind") or "EVENT"), "info", user_id=client.user_id,
            operator_ip=str(event.get("operator") or ""), message=str(event.get("message") or ""),
            details=event,
        ),
    )
    server = ThreadingHTTPServer(
        (args.bind, args.port),
        make_handler(
            client, broker, jetrun, recorder, store, health, roles,
            args.enable_control, log, access_pin, simulated=args.simulator,
        ),
    )
    url = f"http://127.0.0.1:{args.port}/"
    log.info("SCL-40 target: %s", target_host)
    log.info("Dashboard: %s", url)
    log.info("Listen address: %s:%s", args.bind, args.port)
    log.info("Remote API PIN: %s", "enabled" if access_pin else "disabled")
    log.info("Mode: %s", "CONTROL" if args.enable_control else "READ ONLY")
    log.info("Pressure write ceiling: %s MPa", args.pressure_ceiling)
    log.info("LCP jet run controller: idle, %.1f s interval", args.guard_interval)
    log.info("Persistent history: %s", args.history_db)
    log.info("Log: %s", log_path)
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        jetrun.shutdown()
        if client.session_id:
            try:
                client.logout()
                log.warning("SCL-40 session released on shutdown")
            except SCL40Error as exc:
                log.error("logout on shutdown failed: %s", exc)
        server.server_close()
        if sim_device is not None:
            sim_device.shutdown()
        if sim_server is not None:
            sim_server.shutdown()
            sim_server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
