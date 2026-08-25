#!/usr/bin/env python3
"""LCP jet run controller: pressure-triggered water fill, extrusion and stop.

What this automates
-------------------
1. FILL  - run at a high flow so the water reservoir behind the plunger fills
           quickly. While filling, the back pressure stays low. When the water
           is in and the plunger meets the sample, the pressure jumps.
2. RUN   - on that jump, switch straight to the experiment flow without
           stopping the pump. The sample now extrudes at a low steady pressure.
3. STOP  - when the sample is gone the pressure climbs again. Stop the pump
           before it climbs any further.

Both transitions fire on an absolute pressure the operator types in.

Why only the experiment stage waits before watching
---------------------------------------------------
The fill stage watches immediately: the first sample at or above the operator's
fill threshold switches directly to the experiment flow. After that flow change
the pressure is still coming down from the fill stage, so the experiment stage
starts watching only once both conditions hold: the minimum settle time has
passed, and the pressure has been seen below the end threshold at least once.

Other notes
-----------
- Runs inside the dashboard server, not the browser, so closing the page never
  stops it.
- Every flow change and the stop go through the shared command broker, so they
  can never interleave with an operator command.
- The instrument's own method Pmax stays as the hardware protection underneath.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


STAGE_IDLE = "idle"
STAGE_FILL = "fill"
STAGE_RUN = "run"
STAGE_FINISHED = "finished"
STAGE_ABORTED = "aborted"
STAGE_ERROR = "error"
ACTIVE_STAGES = (STAGE_FILL, STAGE_RUN)

STAGE_LABELS = {
    STAGE_IDLE: "대기",
    STAGE_FILL: "물 채우는 중",
    STAGE_RUN: "실험 중",
    STAGE_FINISHED: "완료",
    STAGE_ABORTED: "중단됨",
    STAGE_ERROR: "오류",
}

FIELD_LIMITS: dict[str, dict[str, Any]] = {
    "fill_flow": {"label": "물 채울 때 유량", "unit": "mL/min", "min": Decimal("0"), "max": Decimal("1.0")},
    "run_flow": {"label": "실험 유량", "unit": "mL/min", "min": Decimal("0"), "max": Decimal("1.0")},
    "fill_pressure": {"label": "실험 유량 전환 압력", "unit": "MPa", "min": Decimal("0.05"), "max": Decimal("10.0")},
    "run_pressure": {"label": "실험 끝 판단 압력", "unit": "MPa", "min": Decimal("0.05"), "max": Decimal("10.0")},
    "pressure_limit": {"label": "절대 압력 상한", "unit": "MPa", "min": Decimal("0.1"), "max": Decimal("10.0")},
}
SETTLE_MIN, SETTLE_MAX = 0.0, 300.0
TIMEOUT_MAX = 24 * 3600.0


class RunError(RuntimeError):
    """Invalid configuration or an illegal state transition."""


def parse_decimal(raw: Any, key: str, required: bool = True) -> Decimal | None:
    spec = FIELD_LIMITS[key]
    if raw is None or raw == "":
        if required:
            raise RunError(f"{spec['label']} 값을 입력하세요.")
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise RunError(f"{spec['label']} 값은 숫자로 입력하세요.") from exc
    if not spec["min"] <= value <= spec["max"]:
        raise RunError(f"{spec['label']} 허용 범위는 {spec['min']}~{spec['max']} {spec['unit']}입니다.")
    return value


def parse_seconds(raw: Any, label: str, low: float, high: float, default: float) -> float:
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RunError(f"{label}은(는) 숫자로 입력하세요.") from exc
    if not low <= value <= high:
        raise RunError(f"{label} 허용 범위는 {low:.0f}~{high:.0f}초입니다.")
    return value


class JetRunController:
    """State machine driving fill -> run -> stop from absolute pressures."""

    def __init__(
        self,
        client: Any,
        execute: Callable[..., Any],
        log: Any,
        poll_seconds: float = 1.0,
        on_sample: Callable[[Any, Any, Any], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._client = client
        self._execute = execute
        self._log = log
        self._poll_seconds = poll_seconds
        self._on_sample = on_sample
        self._on_event = on_event
        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        self.stage = STAGE_IDLE
        self.config: dict[str, Any] = {}
        self._stage_started = 0.0
        self._watching = False
        self._started_at = ""
        self._detail = "런이 실행 중이 아닙니다."
        self._pressure: float | None = None
        self._peak: float | None = None
        self._operator = ""
        self._events: list[dict[str, Any]] = []
        self._last_run: dict[str, Any] | None = None

        self._thread = threading.Thread(target=self._loop, name="lcp-jet-run", daemon=True)
        self._thread.start()

    # ---- configuration -------------------------------------------------

    def start(self, body: dict[str, Any], operator: str) -> dict[str, Any]:
        mode = str(body.get("mode") or "lcp_jet")
        if mode not in ("lcp_jet", "watch"):
            raise RunError("모드는 lcp_jet 또는 watch여야 합니다.")
        with self._lock:
            if self.stage in ACTIVE_STAGES:
                raise RunError("이미 진행 중인 런이 있습니다. 먼저 중단하세요.")
        if not self._client.session_id:
            raise RunError("런을 시작하려면 SCL-40 로그인이 필요합니다.")
        connected_pumps = self._client.get_config().get("pumps", [])
        if len(connected_pumps) > 1:
            units = ", ".join(f"{pump['unit_id']}({pump['model']})" for pump in connected_pumps)
            raise RunError(
                "다중 펌프에서는 SYSTEM START가 모든 펌프를 함께 켭니다. "
                f"현재 연결: {units}. 두 펌프의 자동운전 역할이 정의될 때까지 LCP JET RUN은 잠겨 있습니다."
            )

        config: dict[str, Any] = {
            "mode": mode,
            "run_flow": parse_decimal(body.get("run_flow"), "run_flow"),
            "run_pressure": parse_decimal(body.get("run_pressure"), "run_pressure"),
            "pressure_limit": parse_decimal(body.get("pressure_limit"), "pressure_limit", required=False),
            "settle_seconds": parse_seconds(body.get("settle_seconds"), "안정화 시간", SETTLE_MIN, SETTLE_MAX, 15.0),
            "stage_timeout": parse_seconds(body.get("stage_timeout"), "단계 제한 시간", 0.0, TIMEOUT_MAX, 1800.0),
            "fill_flow": None,
            "fill_pressure": None,
        }
        if mode == "lcp_jet":
            config["fill_flow"] = parse_decimal(body.get("fill_flow"), "fill_flow")
            config["fill_pressure"] = parse_decimal(body.get("fill_pressure"), "fill_pressure")
            if config["fill_flow"] <= config["run_flow"]:
                raise RunError("물 채울 때 유량이 실험 유량보다 커야 합니다.")

        limit = config["pressure_limit"]
        for key in ("fill_pressure", "run_pressure"):
            threshold = config[key]
            if limit is not None and threshold is not None and threshold >= limit:
                raise RunError(
                    f"{FIELD_LIMITS[key]['label']}({threshold})은 절대 압력 상한({limit})보다 낮아야 합니다."
                )

        with self._lock:
            self.config = config
            self._events = []
            self._started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self._operator = operator
            self._last_run = None
            self._peak = None

        if mode == "lcp_jet":
            self._begin_fill()
        else:
            self._begin_watch()
        return self.state()

    def _begin_fill(self) -> None:
        flow = self.config["fill_flow"]
        self._log.warning("LCP jet run: fill stage at %s mL/min requested by %s", flow, self._operator)
        self._execute("RUN FILL", self._operator, lambda: self._client.set_method_params({"flow": flow}))
        self._execute("RUN START", self._operator, lambda: self._client.send_pump(start=True))
        self._record("fill_started", f"물 채우기 시작 · {flow} mL/min")
        self._enter_stage(STAGE_FILL)

    def _begin_watch(self) -> None:
        """Watch only: the operator filled and started the pump themselves."""
        self._log.warning("LCP jet run: watch-only run requested by %s", self._operator)
        self._record("watch_started", "감시만 하는 모드로 시작")
        self._enter_stage(STAGE_RUN)

    def abort(self, reason: str, operator: str, stop_pump: bool = True) -> dict[str, Any]:
        with self._lock:
            active = self.stage in ACTIVE_STAGES
        if not active:
            with self._lock:
                self.stage = STAGE_IDLE
                self._detail = "런이 실행 중이 아닙니다."
            return self.state()

        error = ""
        if stop_pump:
            try:
                self._execute("RUN ABORT", operator, lambda: self._client.send_pump(start=False))
                self._record("aborted", f"{reason} · 펌프 정지함")
            except Exception as exc:  # noqa: BLE001 - abort must always finish
                error = str(exc)
                self._log.error("LCP jet run abort STOP failed: %s", exc)
                self._record("stop_failed", f"{reason} · 펌프 정지 실패: {exc}")
        else:
            self._record("aborted", reason)
        self._finish(STAGE_ABORTED, reason if not error else f"{reason} · 펌프 정지 실패: {error}")
        return self.state()

    def shutdown(self) -> None:
        self._stop_event.set()

    # ---- state ---------------------------------------------------------

    def _threshold(self, stage: str | None = None) -> Decimal | None:
        stage = stage or self.stage
        if stage == STAGE_FILL:
            return self.config.get("fill_pressure")
        if stage == STAGE_RUN:
            return self.config.get("run_pressure")
        return None

    def _enter_stage(self, stage: str) -> None:
        with self._lock:
            self.stage = stage
            self._stage_started = time.monotonic()
            self._watching = stage == STAGE_FILL
            self._peak = None
            threshold = self._threshold(stage)
            if stage == STAGE_FILL:
                self._detail = f"즉시 감시 중 · 실험 유량 전환 압력 {threshold} MPa"
            else:
                self._detail = f"안정화 후 압력이 {threshold} MPa 아래로 내려오면 감시를 시작합니다."

    def _finish(self, stage: str, detail: str) -> None:
        with self._lock:
            self.stage = stage
            self._detail = detail
            self._watching = False
            self._last_run = {
                "stage": stage,
                "started_at": self._started_at,
                "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "mode": self.config.get("mode"),
                "events": list(self._events),
            }

    def _record(self, kind: str, message: str) -> None:
        entry = {
            "kind": kind,
            "message": message,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with self._lock:
            self._events.append(entry)
            if len(self._events) > 40:
                del self._events[:-40]
        if self._on_event:
            try:
                self._on_event(dict(entry))
            except Exception as exc:  # audit failure must not stop the safety loop
                self._log.error("run audit callback failed: %s", exc)
        self._log.warning("LCP jet run: %s", message)

    def state(self) -> dict[str, Any]:
        with self._lock:
            active = self.stage in ACTIVE_STAGES
            threshold = self._threshold()
            return {
                "stage": self.stage,
                "stage_label": STAGE_LABELS.get(self.stage, self.stage),
                "active": active,
                "watching": self._watching,
                "detail": self._detail,
                "mode": self.config.get("mode", ""),
                "elapsed_seconds": round(time.monotonic() - self._stage_started, 1) if active else 0.0,
                "threshold": float(threshold) if threshold is not None else None,
                "pressure": round(self._pressure, 3) if self._pressure is not None else None,
                "peak": round(self._peak, 3) if self._peak is not None else None,
                "started_at": self._started_at,
                "operator": self._operator,
                "events": list(self._events),
                "last_run": self._last_run,
                "config": {
                    key: (str(value) if isinstance(value, Decimal) else value)
                    for key, value in self.config.items()
                },
                "poll_seconds": self._poll_seconds,
            }

    # ---- worker --------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.wait(self._poll_seconds):
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - the controller must not die
                self._log.error("LCP jet run tick failed: %s", exc)
                with self._lock:
                    self._detail = f"감시 오류: {exc}"

    def _tick(self) -> None:
        with self._lock:
            if self.stage not in ACTIVE_STAGES:
                return

        monitor = self._client.get_monitor()
        if not monitor.get("available"):
            with self._lock:
                self._detail = monitor.get("reason") or "모니터 세션 없음"
            return
        raw_pressure = monitor.get("pressure")
        if raw_pressure in (None, ""):
            with self._lock:
                self._detail = "압력 값을 읽지 못했습니다."
            return

        pressure = float(raw_pressure)
        if self._on_sample is not None:
            with self._lock:
                commanded = self.config.get("fill_flow" if self.stage == STAGE_FILL else "run_flow")
            self._on_sample(raw_pressure, monitor.get("flow"), commanded)

        with self._lock:
            self.stage_snapshot = self.stage
            stage = self.stage
            config = dict(self.config)
            elapsed = time.monotonic() - self._stage_started
            self._pressure = pressure
            self._peak = pressure if self._peak is None else max(self._peak, pressure)
            pump_off = monitor.get("pump_on") is False

        if pump_off:
            self._record("pump_off", "펌프가 외부에서 정지되었습니다 — 런 종료")
            self._finish(STAGE_ABORTED, "펌프가 외부에서 정지됨")
            return

        limit = config.get("pressure_limit")
        if limit is not None and pressure >= float(limit):
            self._record("limit", f"절대 압력 상한 도달 · {pressure:.2f} ≥ {float(limit):.2f} MPa")
            self._stop_now(STAGE_ABORTED, "절대 압력 상한 도달")
            return

        timeout = config.get("stage_timeout") or 0.0
        if timeout and elapsed > timeout:
            self._record("timeout", f"{STAGE_LABELS[stage]} 제한 시간 {timeout:.0f}초 초과")
            self._stop_now(STAGE_ABORTED, "단계 제한 시간 초과")
            return

        threshold = float(config["fill_pressure" if stage == STAGE_FILL else "run_pressure"])
        settle = config["settle_seconds"]

        with self._lock:
            if stage == STAGE_FILL:
                # Filling must react to the first sample at/above the threshold.
                # A fixed settle delay can miss a rapid pressure rise entirely.
                self._watching = True
                self._detail = f"즉시 감시 중 · {pressure:.2f} / {threshold:.2f} MPa"
            elif not self._watching:
                # Arm only after the pressure has settled below the trigger, so
                # the fall from the previous stage cannot fire it.
                if elapsed < settle:
                    self._detail = f"안정화 중 · {max(0, settle - elapsed):.0f}초 남음 (현재 {pressure:.2f} MPa)"
                    return
                if pressure >= threshold:
                    self._detail = f"압력이 {threshold:.2f} MPa 아래로 내려오길 기다리는 중 (현재 {pressure:.2f} MPa)"
                    return
                self._watching = True
                self._detail = f"감시 중 · {pressure:.2f} / {threshold:.2f} MPa"
            else:
                self._detail = f"감시 중 · {pressure:.2f} / {threshold:.2f} MPa"

            if not self._watching or pressure < threshold:
                return

        if stage == STAGE_FILL:
            self._advance_to_run(pressure, threshold)
        else:
            self._record("end_detected", f"실험 끝 · 압력 {pressure:.2f} MPa ≥ {threshold:.2f} MPa")
            self._stop_now(STAGE_FINISHED, "샘플 다 밀어냄 — 펌프 정지")

    def _advance_to_run(self, pressure: float, threshold: float) -> None:
        run_flow = self.config["run_flow"]
        self._record(
            "fill_detected",
            f"전환 압력 도달 · {pressure:.2f} MPa ≥ {threshold:.2f} MPa "
            f"→ 실험 유량 {run_flow} mL/min으로 전환",
        )
        try:
            self._execute("RUN FLOW", self._operator, lambda: self._client.set_method_params({"flow": run_flow}))
        except Exception as exc:  # noqa: BLE001 - a failed switch must stop the pump
            self._record("flow_switch_failed", f"실험 유량 전환 실패: {exc}")
            self._stop_now(STAGE_ERROR, f"유량 전환 실패: {exc}")
            return
        self._enter_stage(STAGE_RUN)

    def _stop_now(self, stage: str, detail: str) -> None:
        try:
            self._execute("RUN STOP", self._operator, lambda: self._client.send_pump(start=False))
            self._record("stopped", f"펌프 정지 완료 · {detail}")
            self._finish(stage, detail)
        except Exception as exc:  # noqa: BLE001 - report, never swallow
            self._record("stop_failed", f"펌프 정지 실패: {exc} — 즉시 수동 확인 필요")
            self._finish(STAGE_ERROR, f"펌프 정지 실패: {exc}")
