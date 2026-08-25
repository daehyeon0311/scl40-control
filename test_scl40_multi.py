"""Offline tests for the two-pump SCL-40 data model.

These tests only use ``scl40_sim``. They never contact the real instrument.
"""

from __future__ import annotations

import unittest

from scl40_gui import SCL40Client, TrendRecorder
from scl40_sim import start_simulator


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


if __name__ == "__main__":
    unittest.main()
