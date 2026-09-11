"""Offline tests for the two-pump SCL-40 data model.

These tests only use ``scl40_sim``. They never contact the real instrument.
"""

from __future__ import annotations

import unittest
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from scl40_gui import RolePolicy, SCL40Client, SCL40Error, SnapshotCache, TrendRecorder
from scl40_jetrun import JetRunController, STAGE_ERROR, STAGE_FILL, STAGE_FINISHED, STAGE_RUN
from scl40_sim import start_simulator
from scl40_store import AuditStore, CommunicationHealth


class MultiPumpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.device = start_simulator(port=0)
        port = cls.server.server_address[1]
        cls.client = SCL40Client(f"127.0.0.1:{port}", timeout=3)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.client.session_id:
            cls.client.logout()
        cls.device.shutdown()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        if not self.client.session_id:
            self.client.login("Admin", "Admin")

    def test_detects_both_connected_pumps(self) -> None:
        config = self.client.get_config(force=True)
        self.assertEqual(
            [(pump["unit_id"], pump["model"]) for pump in config["pumps"]],
            [("A", "LC-40i"), ("B", "LC-20Ai")],
        )

    def test_method_and_monitor_are_kept_per_unit(self) -> None:
        snapshot = self.client.snapshot()
        self.assertEqual([pump["unit_id"] for pump in snapshot["pumps"]], ["A", "B"])
        self.assertEqual(snapshot["pumps"][0]["method"]["flow"], "0.0460")
        self.assertEqual(snapshot["pumps"][1]["method"]["flow"], "0.200")

    def test_unit_b_method_write_does_not_change_unit_a(self) -> None:
        before_a = self.client.get_method("A")["flow"]
        result = self.client.set_method_params({"flow": "0.350"}, "B")
        self.assertEqual(result["unit_id"], "B")
        self.assertEqual(result["method"]["flow"], "0.350")
        self.assertEqual(self.client.get_method("A")["flow"], before_a)

    def test_unit_b_preview_builds_request_without_writing(self) -> None:
        before = self.client.get_method("B")["flow"]
        result = self.client.preview_method_params({"flow": "0.275"}, "B")
        self.assertFalse("/cgi-bin/Method.cgi" in result["request"])
        self.assertIn("<UnitID>B</UnitID>", result["request"])
        self.assertIn("<Flow>0.275</Flow>", result["request"])
        self.assertEqual(self.client.get_method("B")["flow"], before)

    def test_tflow_is_not_user_writable(self) -> None:
        self.assertNotIn("tflow", self.client.limit_table())
        with self.assertRaisesRegex(SCL40Error, "변경할 수 없습니다"):
            self.client.set_method_params({"tflow": "1.0000"}, "A")

    def test_system_event_changes_both_operation_states(self) -> None:
        self.client.send_pump(True)
        self.assertTrue(all(pump["pump_on"] for pump in self.client.get_monitors()["pumps"]))
        self.client.send_pump(False)
        self.assertTrue(all(pump["pump_on"] is False for pump in self.client.get_monitors()["pumps"]))

    def test_trend_history_is_separate_per_unit(self) -> None:
        recorder = TrendRecorder(min_interval=0)
        recorder.record("1.2", "0.1", "0.1", "A")
        recorder.record("2.3", "0.2", "0.2", "B")
        a_samples, _ = recorder.series(unit_id="A")
        b_samples, _ = recorder.series(unit_id="B")
        self.assertEqual(a_samples[0][1:], [1.2, 0.1, 0.1])
        self.assertEqual(b_samples[0][1:], [2.3, 0.2, 0.2])

    def test_snapshot_cache_reuses_and_invalidates(self) -> None:
        cache = SnapshotCache(ttl=10)
        calls = []

        def produce():
            calls.append(1)
            return {"sequence": len(calls)}

        first, first_fresh = cache.get(produce)
        second, second_fresh = cache.get(produce)
        cache.invalidate()
        third, third_fresh = cache.get(produce)

        self.assertTrue(first_fresh)
        self.assertFalse(second_fresh)
        self.assertEqual(first, second)
        self.assertTrue(third_fresh)
        self.assertEqual(third["sequence"], 2)

    def test_role_policy_defaults_and_override(self) -> None:
        policy = RolePolicy({"guest": "viewer", "chemist": "operator"})
        self.assertEqual(policy.role_for("Admin"), "admin")
        self.assertTrue(policy.describe("chemist")["control"])
        self.assertFalse(policy.describe("chemist")["pressure_limits"])
        self.assertFalse(policy.describe("guest")["control"])

    def test_persistent_history_audit_csv_and_alarm_recovery(self) -> None:
        with TemporaryDirectory() as temp:
            store = AuditStore(Path(temp) / "history.sqlite3")
            try:
                recorder = TrendRecorder(store, min_interval=0)
                recorder.record("1.25", "0.20", "0.20", "B", True)
                self.assertEqual(store.recent_telemetry("B")[0][1:], [1.25, 0.2, 0.2])

                first = store.record_event("command", "TEST", "success", user_id="Admin")
                second = store.record_event("command", "TEST2", "success", user_id="Admin")
                events = store.recent_events()
                self.assertEqual([events[1]["id"], events[0]["id"]], [first, second])
                self.assertNotEqual(events[0]["entry_hash"], events[1]["entry_hash"])
                self.assertTrue(store.export_csv("telemetry").startswith(b"\xef\xbb\xbf"))

                health = CommunicationHealth(store)
                health.failure("timeout")
                self.assertEqual(health.snapshot()["state"], "stale")
                self.assertEqual(store.stats()["active_alarms"], 1)
                health.success(25)
                self.assertEqual(health.snapshot()["state"], "online")
                self.assertEqual(store.stats()["active_alarms"], 0)
            finally:
                store.close()

    def test_timed_run_sets_flow_and_stops_after_duration(self) -> None:
        class TimedClient:
            session_id = "test-session"

            def __init__(self) -> None:
                self.pump_on = False
                self.commands = []

            @staticmethod
            def get_config():
                return {"pumps": [{"unit_id": "A", "model": "LC-40i"}]}

            def set_method_params(self, params):
                self.commands.append(("flow", str(params["flow"])))
                return {}

            def send_pump(self, start):
                self.pump_on = bool(start)
                self.commands.append(("pump", self.pump_on))
                return {}

            def get_monitor(self):
                return {"available": True, "pressure": "0.20", "flow": "0.1000", "pump_on": self.pump_on}

        client = TimedClient()
        controller = JetRunController(
            client,
            lambda _action, _operator, operation: operation(),
            log=type("Log", (), {"warning": lambda *args: None, "error": lambda *args: None})(),
            poll_seconds=3600,
        )
        try:
            state = controller.start({
                "mode": "timed", "run_flow": "0.1000", "duration_seconds": "2",
                "pressure_limit": "9.0",
            }, "tester")
            self.assertEqual(state["stage"], STAGE_RUN)
            self.assertEqual(client.commands, [("flow", "0.1000"), ("pump", True)])

            controller._stage_started = time.monotonic() - 3
            controller._tick()
            state = controller.state()
            self.assertEqual(state["stage"], STAGE_FINISHED)
            self.assertFalse(client.pump_on)
            self.assertEqual(client.commands[-1], ("pump", False))
            self.assertTrue(any(event["kind"] == "duration_complete" for event in state["events"]))
        finally:
            controller.shutdown()

    def test_run_waits_for_delayed_pump_on_confirmation(self) -> None:
        class DelayedMonitorClient:
            session_id = "test-session"

            def __init__(self) -> None:
                self.commands = []
                self.monitor_on = False

            @staticmethod
            def get_config():
                return {"pumps": [{"unit_id": "A", "model": "LC-40i"}]}

            def set_method_params(self, params):
                self.commands.append(("flow", str(params["flow"])))
                return {}

            def send_pump(self, start):
                self.commands.append(("pump", bool(start)))
                return {}

            def get_monitor(self):
                return {
                    "available": True, "pressure": "0.20", "flow": "0.3000",
                    "pump_on": self.monitor_on,
                }

        client = DelayedMonitorClient()
        controller = JetRunController(
            client,
            lambda _action, _operator, operation: operation(),
            log=type("Log", (), {"warning": lambda *args: None, "error": lambda *args: None})(),
            poll_seconds=3600,
        )
        try:
            controller.start({
                "mode": "lcp_jet", "fill_flow": "0.3000", "fill_pressure": "2.0",
                "run_flow": "0.1000", "run_pressure": "3.0", "pressure_limit": "9.0",
                "settle_seconds": "0.5", "stage_timeout": "60",
            }, "tester")

            controller._tick()
            state = controller.state()
            self.assertEqual(state["stage"], STAGE_FILL)
            self.assertIn("펌프 시작 확인 중", state["detail"])
            self.assertNotIn(("pump", False), client.commands)

            client.monitor_on = True
            controller._tick()
            self.assertEqual(controller.state()["stage"], STAGE_FILL)

            client.monitor_on = False
            controller._tick()
            self.assertNotEqual(controller.state()["stage"], STAGE_FILL)
        finally:
            controller.shutdown()

    def test_unconfirmed_start_fails_safe_after_grace_period(self) -> None:
        class NeverOnClient:
            session_id = "test-session"

            def __init__(self) -> None:
                self.commands = []

            @staticmethod
            def get_config():
                return {"pumps": [{"unit_id": "A", "model": "LC-40i"}]}

            def set_method_params(self, _params):
                return {}

            def send_pump(self, start):
                self.commands.append(bool(start))
                return {}

            @staticmethod
            def get_monitor():
                return {"available": True, "pressure": "0.10", "flow": "0.0000", "pump_on": False}

        client = NeverOnClient()
        controller = JetRunController(
            client,
            lambda _action, _operator, operation: operation(),
            log=type("Log", (), {"warning": lambda *args: None, "error": lambda *args: None})(),
            poll_seconds=3600,
        )
        try:
            controller.start({
                "mode": "lcp_jet", "fill_flow": "0.3000", "fill_pressure": "2.0",
                "run_flow": "0.1000", "run_pressure": "3.0", "pressure_limit": "9.0",
                "settle_seconds": "0.5", "stage_timeout": "60",
            }, "tester")
            controller._stage_started = time.monotonic() - 4
            controller._tick()
            self.assertEqual(controller.state()["stage"], STAGE_ERROR)
            self.assertEqual(client.commands, [True, False])
        finally:
            controller.shutdown()


if __name__ == "__main__":
    unittest.main()
