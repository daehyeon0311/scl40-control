#!/usr/bin/env python3
"""Offline SCL-40 / LC-40i simulator for developing the dashboard without hardware.

This module answers the same HTTP/XML endpoints the real SCL-40 exposes, using
only response shapes that were already confirmed against the live instrument.
It never talks to a real device and must never be used to validate physical
behavior — it only unblocks GUI/API work when the instrument is unreachable.
"""

from __future__ import annotations

import argparse
import random
import threading
import time
import xml.etree.ElementTree as ET
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>'

# Back pressure at 1.0000 mL/min while the sample is being extruded, in MPa.
PRESSURE_PER_ML = 10.0
# Back pressure at 1.0000 mL/min while the water reservoir is still filling.
# Much lower: the plunger has not met the sample yet.
FILL_PRESSURE_PER_ML = 1.5
# Fraction of the remaining flow gap closed per physics tick.
RAMP_FACTOR = 0.22
TICK_SECONDS = 0.25

# Default LCP jet cartridge geometry, in mL.
RESERVOIR_VOLUME = 0.15
SAMPLE_VOLUME = 0.05
# Pressure climb once the sample is fully expelled, in MPa per second.
END_RISE_PER_SECOND = 0.35


def text_of(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    value = node.findtext(path)
    return value.strip() if value else default


class SimulatedSCL40:
    """Mutable state of the fake controller plus a tiny pump model."""

    def __init__(
        self,
        *,
        users: dict[str, str] | None = None,
        touch_screen_logged_in: bool = False,
        reservoir_volume: float = RESERVOIR_VOLUME,
        sample_volume: float = SAMPLE_VOLUME,
        end_rise: float = END_RISE_PER_SECOND,
    ) -> None:
        self.lock = threading.RLock()
        self.users = users or {"Admin": "Admin"}
        self.touch_screen_logged_in = touch_screen_logged_in
        self.session_id = ""
        self.session_user = ""
        self.pump_on = False
        self.target_flow = Decimal("0.0460")
        self.tflow = Decimal("1.0000")
        self.pmax = Decimal("10.0")
        self.pmin = Decimal("0.0")
        self.pump_b_target_flow = Decimal("0.200")
        self.pump_b_tflow = Decimal("0.000")
        self.pump_b_pmax = Decimal("10.0")
        self.pump_b_pmin = Decimal("0.0")
        self.actual_flow = 0.0
        self.pressure = 0.0
        self.overpressure_stops = 0

        # LCP jet cartridge model. Water first fills the reservoir behind the
        # plunger at low pressure; once full the plunger meets the sample and
        # the pressure steps up; once the sample is gone it climbs without
        # bound. Volumes are in mL, delivered by integrating the flow.
        self.reservoir_volume = reservoir_volume
        self.sample_volume = sample_volume
        self.end_rise = end_rise
        self.delivered = 0.0
        self.overrun_seconds = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_physics, daemon=True)
        self._thread.start()

    # ---- pump model -----------------------------------------------------

    def _run_physics(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            with self.lock:
                goal = float(self.target_flow) if self.pump_on else 0.0
                self.actual_flow += (goal - self.actual_flow) * RAMP_FACTOR
                if abs(self.actual_flow) < 1e-5:
                    self.actual_flow = 0.0

                if self.pump_on:
                    self.delivered += self.actual_flow / 60.0 * TICK_SECONDS

                noise = random.uniform(-0.004, 0.004) if self.actual_flow else 0.0
                self.pressure = max(0.0, self._back_pressure() + noise)

                # The real pump shuts itself down above the method pressure
                # limit; reproduce that so Pmax means something here too.
                if self.pump_on and self.pressure > float(self.pmax):
                    self.pump_on = False
                    self.overpressure_stops += 1

    def _back_pressure(self) -> float:
        """Pressure for the current cartridge phase at the current flow."""
        if self.delivered < self.reservoir_volume:
            return self.actual_flow * FILL_PRESSURE_PER_ML

        extruded = self.delivered - self.reservoir_volume
        steady = self.actual_flow * PRESSURE_PER_ML
        if extruded < self.sample_volume:
            self.overrun_seconds = 0.0
            return steady

        if self.pump_on:
            self.overrun_seconds += TICK_SECONDS
        return steady + self.overrun_seconds * self.end_rise

    def reset_cartridge(self) -> None:
        """Load a fresh cartridge: empty reservoir, full sample."""
        with self.lock:
            self.delivered = 0.0
            self.overrun_seconds = 0.0

    def shutdown(self) -> None:
        self._stop.set()

    # ---- endpoint handlers ---------------------------------------------

    def config_response(self) -> str:
        return (
            XML_HEADER
            + "<Config><Info>"
            + "<Ctrl><Model>SCL-40</Model><SubModel></SubModel>"
              "<Version>1.67</Version><Address>1</Address></Ctrl>"
            + "<Pumps><Pump><UnitID>A</UnitID><Model>LC-40i</Model>"
              "<SubModel></SubModel><Version>1.00</Version><Address>3</Address>"
              "<State>1</State></Pump>"
              "<Pump><UnitID>B</UnitID><Model>LC-20Ai</Model>"
              "<SubModel></SubModel><Version>1.01</Version><Address>4</Address>"
              "<State>1</State></Pump></Pumps>"
            + "</Info></Config>"
        )

    def status_response(self) -> str:
        with self.lock:
            login_state = "1" if self.session_id or self.touch_screen_logged_in else "0"
            analyst = self.session_user
        return (
            XML_HEADER
            + "<Summary><GroupName>SIMULATOR</GroupName>"
              "<HostName>SCL40-SIM</HostName><SysNum>1</SysNum>"
            + "<Systems><System><Name>Simulated LC-40i</Name>"
            + f"<LoginState>{login_state}</LoginState><ServerType>SIM</ServerType>"
            + f"<Status><SysState>401</SysState><Analyst>{analyst}</Analyst></Status>"
            + "</System></Systems></Summary>"
        )

    def method_response(self) -> str:
        with self.lock:
            flow, tflow, pmax, pmin = self.target_flow, self.tflow, self.pmax, self.pmin
            bflow, btflow, bpmax, bpmin = (
                self.pump_b_target_flow, self.pump_b_tflow, self.pump_b_pmax, self.pump_b_pmin,
            )
        return (
            XML_HEADER
            + "<Method><No>0</No><Alias>SIM METHOD</Alias><Pumps><Pump>"
            + "<UnitID>A</UnitID>"
            + f"<Usual><Flow>{flow:.4f}</Flow><Tflow>{tflow:.4f}</Tflow>"
              f"<Pmax>{pmax:.1f}</Pmax></Usual>"
            + f"<Detail><Pmin>{pmin:.1f}</Pmin></Detail>"
            + "</Pump><Pump><UnitID>B</UnitID>"
            + f"<Usual><Flow>{bflow:.3f}</Flow><Tflow>{btflow:.3f}</Tflow>"
              f"<Pmax>{bpmax:.1f}</Pmax></Usual>"
            + f"<Detail><Pmin>{bpmin:.1f}</Pmin></Detail>"
            + "</Pump></Pumps></Method>"
        )

    def method_write(self, request: ET.Element) -> str:
        for pump in request.findall("./Pumps/Pump"):
            unit_id = text_of(pump, "UnitID")
            if unit_id == "A":
                attributes = {
                    "./Usual/Flow": "target_flow", "./Usual/Tflow": "tflow",
                    "./Usual/Pmax": "pmax", "./Detail/Pmin": "pmin",
                }
            elif unit_id == "B":
                attributes = {
                    "./Usual/Flow": "pump_b_target_flow", "./Usual/Tflow": "pump_b_tflow",
                    "./Usual/Pmax": "pump_b_pmax", "./Detail/Pmin": "pump_b_pmin",
                }
            else:
                continue
            fields = (
                (path, attribute) for path, attribute in attributes.items()
            )
            with self.lock:
                for path, attribute in fields:
                    written = text_of(pump, path)
                    if written:
                        setattr(self, attribute, Decimal(written))
        return self.method_response()

    def login_response(self, request: ET.Element) -> str:
        mode = text_of(request, "Mode")
        if mode == "-1":
            with self.lock:
                self.session_id = ""
                self.session_user = ""
            return XML_HEADER + "<Login><Mode>-1</Mode></Login>"

        user_id = text_of(request, "./Certification/UserID")
        password = text_of(request, "./Certification/Password")
        with self.lock:
            if self.touch_screen_logged_in:
                result, session = "13", ""
            elif user_id not in self.users:
                result, session = "1", ""
            elif self.users[user_id] != password:
                result, session = "2", ""
            elif self.session_id:
                result, session = "5", ""
            else:
                session = f"SIM{random.randint(100000, 999999)}"
                self.session_id = session
                self.session_user = user_id
                result = "0"
        return (
            XML_HEADER
            + "<Login><Mode>0</Mode><Certification>"
            + f"<UserID>{user_id}</UserID><Password></Password>"
            + f"<SessionID>{session}</SessionID><Result>{result}</Result>"
            + "</Certification></Login>"
        )

    def monitor_response(self, session_id: str) -> str | None:
        with self.lock:
            if not self.session_id or session_id != self.session_id:
                return None
            flow, pressure, bflow = self.actual_flow, self.pressure, float(self.pump_b_target_flow)
            op_state = "1" if self.pump_on else "0"
        return (
            XML_HEADER
            + "<Monitor>"
            + "<SysMon><Method><Pumps><Pump><UnitID>A</UnitID>"
            + f"<Press>{pressure:.1f}</Press><PressUnit>0</PressUnit>"
            + f"<Flow>{flow:.4f}</Flow></Pump>"
            + f"<Pump><UnitID>B</UnitID><Press>{pressure:.1f}</Press><PressUnit>2</PressUnit>"
            + f"<Flow>{bflow:.3f}</Flow></Pump></Pumps></Method></SysMon>"
            + "<Config><Situation><Pumps><Pump><UnitID>A</UnitID>"
            + f"<OpState>{op_state}</OpState></Pump><Pump><UnitID>B</UnitID>"
            + f"<OpState>{op_state}</OpState></Pump></Pumps></Situation></Config>"
            + "<AnalyMon><Authority>1</Authority><SysState>401</SysState></AnalyMon>"
            + "</Monitor>"
        )

    def event_response(self, request: ET.Element) -> str:
        pump_bt = text_of(request, "./Method/PumpBT")
        if pump_bt in ("0", "1"):
            with self.lock:
                self.pump_on = pump_bt == "1"
        return (
            XML_HEADER
            + f"<Event><Method><PumpBT>{pump_bt}</PumpBT></Method></Event>"
        )


def make_sim_handler(device: SimulatedSCL40, latency: float = 0.0):
    class SimHandler(BaseHTTPRequestHandler):
        server_version = "Shimadzu-LC20A/1.00 (httpd-sim)"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep console quiet
            pass

        def _send_xml(self, body: str, status: int = 200) -> None:
            if latency:
                time.sleep(latency)
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/xml; charset=UTF-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                request = ET.fromstring(raw) if raw.strip() else ET.Element("Empty")
            except ET.ParseError:
                self._send_xml(XML_HEADER + "<Error><Code>400</Code></Error>", 400)
                return

            path = self.path.split("?", 1)[0]
            if path == "/cgi-bin/Config.cgi":
                self._send_xml(device.config_response())
            elif path == "/cgi-bin/Status.cgi":
                self._send_xml(device.status_response())
            elif path == "/cgi-bin/Method.cgi":
                if request.find("./Pumps/Pump") is not None:
                    self._send_xml(device.method_write(request))
                else:
                    self._send_xml(device.method_response())
            elif path == "/cgi-bin/Login.cgi":
                self._send_xml(device.login_response(request))
            elif path.startswith("/cgi-bin/Monitor.cgi/"):
                body = device.monitor_response(path.rsplit("/", 1)[-1])
                if body is None:
                    self._send_xml(XML_HEADER + "<Error><Code>13</Code></Error>", 200)
                else:
                    self._send_xml(body)
            elif path == "/cgi-bin/Event.cgi":
                self._send_xml(device.event_response(request))
            else:
                self.send_error(404)

        def do_GET(self) -> None:  # noqa: N802
            self.send_error(404)

    return SimHandler


def start_simulator(
    port: int = 9099,
    bind: str = "127.0.0.1",
    *,
    latency: float = 0.0,
    touch_screen_logged_in: bool = False,
    reservoir_volume: float = RESERVOIR_VOLUME,
    sample_volume: float = SAMPLE_VOLUME,
) -> tuple[ThreadingHTTPServer, SimulatedSCL40]:
    """Start the simulator on a background thread and return (server, device)."""
    device = SimulatedSCL40(
        touch_screen_logged_in=touch_screen_logged_in,
        reservoir_volume=reservoir_volume,
        sample_volume=sample_volume,
    )
    server = ThreadingHTTPServer((bind, port), make_sim_handler(device, latency))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, device


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline SCL-40 simulator")
    parser.add_argument("--port", type=int, default=9099)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--latency", type=float, default=0.0, help="added seconds per response")
    parser.add_argument(
        "--touch-logged-in",
        action="store_true",
        help="reject web login with result 13 (touch screen owns the session)",
    )
    parser.add_argument("--reservoir", type=float, default=RESERVOIR_VOLUME,
                        help=f"water reservoir volume in mL (default: {RESERVOIR_VOLUME})")
    parser.add_argument("--sample", type=float, default=SAMPLE_VOLUME,
                        help=f"sample volume in mL (default: {SAMPLE_VOLUME})")
    args = parser.parse_args()

    server, device = start_simulator(
        args.port,
        args.bind,
        latency=args.latency,
        touch_screen_logged_in=args.touch_logged_in,
        reservoir_volume=args.reservoir,
        sample_volume=args.sample,
    )
    print(f"SCL-40 simulator listening on http://{args.bind}:{args.port}")
    print("Point the dashboard at it:")
    print(f"  python scl40_gui.py {args.bind}:{args.port} --enable-control")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        device.shutdown()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
