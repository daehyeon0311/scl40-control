"""HTTP/XML client checked end to end against the offline simulator.

The real request builders and response parsers run here; only the instrument
at the other end is simulated. Write paths are never pointed at hardware.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from scl40_gui import CommandBroker, CommandBusy, SCL40Client, SCL40Error, TrendRecorder
from scl40_sim import start_simulator
from scl40_store import AuditStore


class ClientAgainstSimulator(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.device = start_simulator(port=0, bind="127.0.0.1")
        host, port = cls.server.server_address
        cls.host = f"{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.device.shutdown()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self.device.reset_cartridge()
        self.device.session_id = ""
        self.device.pump_on = False
        self.device.touch_screen_logged_in = False
        self.device.target_flow = Decimal("0.0460")
        self.device.pmax = Decimal("10.0")
        self.device.pmin = Decimal("0.0")
        self.client = SCL40Client(self.host)

    def login(self) -> None:
        self.client.login("Admin", "Admin")

    # ---- reads ---------------------------------------------------------

    def test_config_reports_the_controller_and_every_pump(self) -> None:
        config = self.client.get_config()
        self.assertEqual(config["controller"]["model"], "SCL-40")
        self.assertEqual([pump["unit_id"] for pump in config["pumps"]], ["A", "B"])

    def test_method_read_exposes_every_writable_field(self) -> None:
        method = self.client.get_method("A")
        for key in ("flow", "tflow", "pmax", "pmin"):
            self.assertTrue(method[key], f"{key} missing from the Method response")

    def test_summary_reports_the_system_state(self) -> None:
        self.assertEqual(self.client.get_summary()["system_state_code"], "401")

    # ---- session -------------------------------------------------------

    def test_login_and_logout(self) -> None:
        self.login()
        self.assertTrue(self.client.session_id)
        self.client.logout()
        self.assertEqual(self.client.session_id, "")

    def test_wrong_password_is_rejected(self) -> None:
        with self.assertRaises(SCL40Error) as caught:
            self.client.login("Admin", "nope")
        self.assertIn("비밀번호", str(caught.exception))

    def test_touch_screen_session_blocks_web_login(self) -> None:
        """Result 13 is the one the operator meets most often."""
        self.device.touch_screen_logged_in = True
        with self.assertRaises(SCL40Error) as caught:
            self.client.login("Admin", "Admin")
        self.assertIn("13", str(caught.exception))

    def test_monitor_needs_a_session(self) -> None:
        self.assertFalse(self.client.get_monitor("A")["available"])

    # ---- writes --------------------------------------------------------

    def test_flow_write_is_verified_by_readback(self) -> None:
        self.login()
        result = self.client.set_method_params({"flow": "0.2500"}, "A")
        self.assertEqual(result["applied"]["flow"]["value"], "0.2500")
        self.assertEqual(self.client.get_method("A")["flow"], "0.2500")

    def test_pressure_limits_are_writable(self) -> None:
        self.login()
        self.client.set_method_params({"pmax": "8.0", "pmin": "0.5"}, "A")
        method = self.client.get_method("A")
        self.assertEqual(method["pmax"], "8.0")
        self.assertEqual(method["pmin"], "0.5")

    def test_write_keeps_the_confirmed_document_shape(self) -> None:
        """Flow and Tflow always travel together, as the instrument expects."""
        self.login()
        request = self.client.set_method_params({"pmax": "7.0"}, "A")["request"]
        self.assertIn("<UnitID>A</UnitID>", request)
        self.assertIn("<Flow>", request)
        self.assertIn("<Tflow>", request)
        self.assertIn("<Pmax>7.0</Pmax>", request)

    def test_preview_builds_the_request_without_sending_it(self) -> None:
        self.login()
        before = self.client.get_method("A")["flow"]
        preview = self.client.preview_method_params({"flow": "0.7000"}, "A")
        self.assertIn("<Flow>0.7000</Flow>", preview["request"])
        self.assertEqual(self.client.get_method("A")["flow"], before)

    def test_writes_require_a_session(self) -> None:
        with self.assertRaises(SCL40Error):
            self.client.set_method_params({"flow": "0.1000"}, "A")

    def test_flow_above_the_hard_limit_is_refused(self) -> None:
        self.login()
        with self.assertRaises(SCL40Error):
            self.client.set_method_params({"flow": "6.0"}, "A")
        self.assertEqual(self.client.get_method("A")["flow"], "0.0460")

    def test_non_numeric_values_are_refused(self) -> None:
        self.login()
        with self.assertRaises(SCL40Error):
            self.client.set_method_params({"flow": "빠르게"}, "A")

    def test_pressure_floor_must_stay_below_the_ceiling(self) -> None:
        self.login()
        with self.assertRaises(SCL40Error) as caught:
            self.client.set_method_params({"pmax": "2.0", "pmin": "3.0"}, "A")
        self.assertIn("하한", str(caught.exception))

    def test_pressure_ceiling_option_caps_what_may_be_written(self) -> None:
        client = SCL40Client(self.host, pressure_ceiling=Decimal("5.0"))
        client.login("Admin", "Admin")
        with self.assertRaises(SCL40Error):
            client.set_method_params({"pmax": "9.0"}, "A")

    def test_a_field_the_instrument_ignores_fails_the_readback(self) -> None:
        """A silent no-op is the dangerous case, so it must raise."""
        self.login()
        original = self.device.method_write
        self.device.method_write = lambda request: self.device.method_response()
        try:
            with self.assertRaises(SCL40Error) as caught:
                self.client.set_method_params({"flow": "0.9000"}, "A")
        finally:
            self.device.method_write = original
        self.assertIn("불일치", str(caught.exception))

    # ---- pump ----------------------------------------------------------

    def test_pump_start_and_stop(self) -> None:
        self.login()
        self.client.send_pump(start=True)
        self.assertTrue(self.device.pump_on)
        self.client.send_pump(start=False)
        self.assertFalse(self.device.pump_on)

    def test_pump_commands_require_a_session(self) -> None:
        with self.assertRaises(SCL40Error):
            self.client.send_pump(start=True)

    def test_snapshot_gathers_every_unit(self) -> None:
        self.login()
        snapshot = self.client.snapshot()
        units = [pump["unit_id"] for pump in snapshot["methods"]["pumps"]]
        self.assertEqual(units, ["A", "B"])


class CommandBrokerTests(unittest.TestCase):
    """Only one instrument command may be in flight at a time."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = AuditStore(Path(directory.name) / "audit.db")
        # Close the database before the directory goes away: Windows refuses to
        # delete a file that still has an open handle.
        self.addCleanup(store.close)
        self.broker = CommandBroker(store=store, user_getter=lambda: "tester")

    def test_result_is_passed_through(self) -> None:
        self.assertEqual(self.broker.execute("READ", "1.2.3.4", lambda: "done"), "done")

    def test_activity_records_the_operator_and_outcome(self) -> None:
        self.broker.execute("SET METHOD", "10.0.0.9", lambda: None)
        activity = self.broker.activity()

        self.assertEqual(activity["action"], "SET METHOD")
        self.assertEqual(activity["operator_ip"], "10.0.0.9")
        self.assertEqual(activity["result"], "success")
        self.assertFalse(activity["busy"])

    def test_a_failed_command_is_recorded_and_re_raised(self) -> None:
        def boom():
            raise RuntimeError("장비 응답 없음")

        with self.assertRaises(RuntimeError):
            self.broker.execute("START", "127.0.0.1", boom)
        self.assertEqual(self.broker.activity()["result"], "failed")

    def test_a_second_command_is_refused_while_one_runs(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(2)

        worker = threading.Thread(target=lambda: self.broker.execute("START", "a", slow))
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(release.set)
        started.wait(2)

        with self.assertRaises(CommandBusy):
            self.broker.execute("STOP", "b", lambda: None)

    def test_the_lock_is_released_after_a_failure(self) -> None:
        with self.assertRaises(RuntimeError):
            self.broker.execute("START", "a", lambda: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(self.broker.execute("STOP", "b", lambda: "ok"), "ok")


class TrendRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = TrendRecorder(capacity=5, min_interval=0)

    def test_samples_are_returned_oldest_first(self) -> None:
        self.recorder.record("1.0", "0.05", "0.05")
        self.recorder.record("2.0", "0.05", "0.05")
        samples, until = self.recorder.series()

        self.assertEqual([sample[1] for sample in samples], [1.0, 2.0])
        self.assertEqual(until, samples[-1][0])

    def test_an_incremental_fetch_never_skips_a_sample(self) -> None:
        """Samples inside one clock tick must still get distinct stamps."""
        self.recorder.record("1.0", "0.05", "0.05")
        _, until = self.recorder.series()
        self.recorder.record("2.0", "0.05", "0.05")

        samples, _ = self.recorder.series(since=until)
        self.assertEqual([sample[1] for sample in samples], [2.0])

    def test_capacity_drops_the_oldest_samples(self) -> None:
        for value in range(8):
            self.recorder.record(str(value), "0.05", "0.05")
        samples, _ = self.recorder.series()

        self.assertEqual(len(samples), 5)
        self.assertEqual(samples[0][1], 3.0)

    def test_unreadable_values_are_not_recorded(self) -> None:
        self.recorder.record(None, None, None)
        self.recorder.record("", "", "")
        self.assertEqual(self.recorder.series()[0], [])

    def test_rapid_samples_are_thinned(self) -> None:
        recorder = TrendRecorder(min_interval=60)
        recorder.record("1.0", "0.05", "0.05")
        recorder.record("2.0", "0.05", "0.05")
        self.assertEqual(len(recorder.series()[0]), 1)

    def test_thinning_is_tracked_per_unit(self) -> None:
        recorder = TrendRecorder(min_interval=60)
        recorder.record("1.0", "0.05", "0.05", unit_id="A")
        recorder.record("2.0", "0.05", "0.05", unit_id="B")
        self.assertEqual(len(recorder.series(unit_id="A")[0]), 1)
        self.assertEqual(len(recorder.series(unit_id="B")[0]), 1)


if __name__ == "__main__":
    unittest.main()
