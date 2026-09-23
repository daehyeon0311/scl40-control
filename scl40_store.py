#!/usr/bin/env python3
"""Persistent telemetry, audit and alarm storage for the SCL-40 dashboard."""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class AuditStore:
    """Thread-safe SQLite store with a tamper-evident event hash chain."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    pressure REAL,
                    flow REAL,
                    setpoint REAL,
                    pump_on INTEGER,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
                CREATE INDEX IF NOT EXISTS idx_telemetry_unit_ts ON telemetry(unit_id, ts);

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    category TEXT NOT NULL,
                    action TEXT NOT NULL,
                    result TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    operator_ip TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);

                CREATE TABLE IF NOT EXISTS alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    first_ts REAL NOT NULL,
                    last_ts REAL NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    code TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    raw TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    acknowledged_by TEXT NOT NULL DEFAULT '',
                    acknowledged_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_alarm_active ON alarms(active, last_ts);
                """
            )
            self._db.commit()

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def record_sample(
        self,
        unit_id: str,
        pressure: Any,
        flow: Any,
        setpoint: Any,
        pump_on: bool | None = None,
        error_code: str = "",
        ts: float | None = None,
    ) -> None:
        ts = ts or time.time()
        with self._lock:
            self._db.execute(
                """INSERT INTO telemetry
                   (ts,timestamp,unit_id,pressure,flow,setpoint,pump_on,error_code)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    ts, datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds"),
                    unit_id, self._number(pressure), self._number(flow), self._number(setpoint),
                    None if pump_on is None else int(pump_on), str(error_code or ""),
                ),
            )
            self._db.commit()

    def record_event(
        self,
        category: str,
        action: str,
        result: str = "info",
        user_id: str = "",
        operator_ip: str = "",
        unit_id: str = "",
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> int:
        ts = time.time()
        timestamp = iso_now()
        details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            row = self._db.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous_hash = row[0] if row else ""
            canonical = "|".join((
                timestamp, category, action, result, user_id,
                operator_ip, unit_id, message, details_json, previous_hash,
            ))
            entry_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            cur = self._db.execute(
                """INSERT INTO audit_events
                   (ts,timestamp,category,action,result,user_id,operator_ip,unit_id,message,
                    details_json,previous_hash,entry_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts, timestamp, category, action, result, user_id, operator_ip, unit_id,
                    message, details_json, previous_hash, entry_hash,
                ),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def raise_alarm(
        self,
        code: str,
        message: str,
        severity: str = "warning",
        unit_id: str = "",
        raw: str = "",
    ) -> int:
        now = time.time()
        timestamp = iso_now()
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM alarms WHERE code=? AND unit_id=? AND active=1 ORDER BY id DESC LIMIT 1",
                (code, unit_id),
            ).fetchone()
            if row:
                alarm_id = int(row[0])
                self._db.execute(
                    """UPDATE alarms SET last_ts=?,last_seen=?,severity=?,message=?,raw=? WHERE id=?""",
                    (now, timestamp, severity, message, raw, alarm_id),
                )
            else:
                cur = self._db.execute(
                    """INSERT INTO alarms
                       (first_ts,last_ts,first_seen,last_seen,code,unit_id,severity,message,raw)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (now, now, timestamp, timestamp, code, unit_id, severity, message, raw),
                )
                alarm_id = int(cur.lastrowid)
            self._db.commit()
            return alarm_id

    def resolve_alarm(self, code: str, unit_id: str = "") -> None:
        with self._lock:
            self._db.execute(
                "UPDATE alarms SET active=0,last_ts=?,last_seen=? WHERE code=? AND unit_id=? AND active=1",
                (time.time(), iso_now(), code, unit_id),
            )
            self._db.commit()

    def acknowledge_alarm(self, alarm_id: int, user_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                """UPDATE alarms SET acknowledged=1,acknowledged_by=?,acknowledged_at=? WHERE id=?""",
                (user_id, iso_now(), alarm_id),
            )
            self._db.commit()
            return cur.rowcount == 1

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._db.execute(
                """SELECT id,timestamp,category,action,result,user_id,operator_ip,unit_id,
                          message,details_json,entry_hash
                   FROM audit_events ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json") or "{}")
            result.append(item)
        return result

    def recent_telemetry(self, unit_id: str, limit: int = 7200) -> list[list[Any]]:
        """Return oldest-to-newest points suitable for the dashboard chart."""
        limit = max(1, min(int(limit), 100000))
        with self._lock:
            rows = self._db.execute(
                """SELECT ts,pressure,flow,setpoint FROM (
                       SELECT ts,pressure,flow,setpoint FROM telemetry
                       WHERE unit_id=? ORDER BY id DESC LIMIT ?
                   ) ORDER BY ts""",
                (unit_id.strip().upper(), limit),
            ).fetchall()
        return [[row["ts"], row["pressure"], row["flow"], row["setpoint"]] for row in rows]

    def recent_alarms(self, limit: int = 100, active_only: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        where = "WHERE active=1" if active_only else ""
        with self._lock:
            rows = self._db.execute(
                f"""SELECT id,first_seen,last_seen,code,unit_id,severity,message,raw,active,
                           acknowledged,acknowledged_by,acknowledged_at
                    FROM alarms {where} ORDER BY active DESC,last_ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            sample_count = int(self._db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0])
            event_count = int(self._db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
            active_alarms = int(self._db.execute("SELECT COUNT(*) FROM alarms WHERE active=1").fetchone()[0])
            first = self._db.execute("SELECT timestamp FROM telemetry ORDER BY id LIMIT 1").fetchone()
            last = self._db.execute("SELECT timestamp FROM telemetry ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "database": self.path.name,
            "samples": sample_count,
            "events": event_count,
            "active_alarms": active_alarms,
            "first_sample": first[0] if first else "",
            "last_sample": last[0] if last else "",
        }

    def export_csv(self, kind: str = "telemetry") -> bytes:
        tables = {
            "telemetry": (
                "SELECT timestamp,unit_id,pressure,flow,setpoint,pump_on,error_code FROM telemetry ORDER BY id",
                ("timestamp", "unit_id", "pressure_MPa", "flow_mL_min", "setpoint_mL_min", "pump_on", "error_code"),
            ),
            "events": (
                """SELECT timestamp,category,action,result,user_id,operator_ip,unit_id,message,details_json,entry_hash
                   FROM audit_events ORDER BY id""",
                (
                    "timestamp", "category", "action", "result", "user_id",
                    "operator_ip", "unit_id", "message", "details_json", "entry_hash",
                ),
            ),
            "alarms": (
                """SELECT first_seen,last_seen,code,unit_id,severity,message,raw,active,acknowledged,
                          acknowledged_by,acknowledged_at FROM alarms ORDER BY id""",
                (
                    "first_seen", "last_seen", "code", "unit_id", "severity", "message",
                    "raw", "active", "acknowledged", "acknowledged_by", "acknowledged_at",
                ),
            ),
        }
        if kind not in tables:
            raise ValueError("지원하지 않는 CSV 종류입니다.")
        query, headers = tables[kind]
        with self._lock:
            rows = self._db.execute(query).fetchall()
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(tuple(row) for row in rows)
        return ("\ufeff" + stream.getvalue()).encode("utf-8")

    def close(self) -> None:
        with self._lock:
            self._db.close()


class CommunicationHealth:
    def __init__(self, store: AuditStore, stale_seconds: float = 12.0) -> None:
        self.store = store
        self.stale_seconds = stale_seconds
        self._lock = threading.Lock()
        self.last_success = ""
        self.last_failure = ""
        self.last_error = ""
        self.consecutive_failures = 0
        self.total_failures = 0
        self.last_latency_ms: int | None = None

    def success(self, latency_ms: Any = None) -> None:
        with self._lock:
            recovered = self.consecutive_failures > 0
            self.last_success = iso_now()
            self.last_error = ""
            self.consecutive_failures = 0
            with contextlib.suppress(TypeError, ValueError):
                self.last_latency_ms = int(latency_ms)
        if recovered:
            self.store.resolve_alarm("COMMUNICATION")
            self.store.record_event("communication", "RECOVERED", "success", message="SCL-40 통신 복구")

    def failure(self, error: Any) -> None:
        message = str(error)
        with self._lock:
            self.last_failure = iso_now()
            self.last_error = message
            self.consecutive_failures += 1
            self.total_failures += 1
            failures = self.consecutive_failures
        self.store.raise_alarm(
            "COMMUNICATION", f"SCL-40 통신 실패 {failures}회 연속: {message}",
            severity="critical" if failures >= 3 else "warning", raw=message,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            age = None
            if self.last_success:
                try:
                    age = max(0.0, time.time() - datetime.fromisoformat(self.last_success).timestamp())
                except ValueError:
                    age = None
            stale = self.consecutive_failures > 0 or (age is not None and age > self.stale_seconds)
            return {
                "state": "offline" if self.consecutive_failures >= 3 else "stale" if stale else "online",
                "last_success": self.last_success,
                "last_failure": self.last_failure,
                "last_error": self.last_error,
                "consecutive_failures": self.consecutive_failures,
                "total_failures": self.total_failures,
                "last_latency_ms": self.last_latency_ms,
                "stale_seconds": self.stale_seconds,
            }
