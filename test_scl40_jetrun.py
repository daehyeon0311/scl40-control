"""LCP jet run state machine.

The controller is driven one tick at a time against a fake instrument and a
fake clock, so every transition is deterministic and nothing waits on wall
time. This covers the pressure-triggered fill/run/stop path that the
experiment actually depends on.
"""

from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from unittest.mock import patch

import scl40_jetrun
from scl40_jetrun import (
    PUMP_START_CONFIRM_SECONDS,
    STAGE_ABORTED,
    STAGE_ERROR,
    STAGE_FILL,
    STAGE_FINISHED,
    STAGE_RUN,
    JetRunController,
    RunError,
)

SETTLE = 15.0
CONFIG = {
    "mode": "lcp_jet",
    "fill_flow": "0.3000",
    "fill_pressure": "1.5",
    "run_flow": "0.0460",
    "run_pressure": "1.5",
    "pressure_limit": "9.0",
    "settle_seconds": SETTLE,
    "stage_timeout": 600,
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeInstrument:
    """Single-pump stand-in exposing only what the controller calls."""

    session_id = "test-session"

    def __init__(self, pressure: float = 0.4) -> None:
        self.pressure = pressure
        self.pump_on = True
        self.available = True
        self.flow_writes: list[str] = []
        self.pump_commands: list[bool] = []
        self.fail_flow_write = False

    @staticmethod
    def get_config() -> dict:
        return {"pumps": [{"unit_id": "A", "model": "LC-40i"}]}

    def get_monitor(self) -> dict:
        if not self.available:
            return {"available": False, "reason": "세션 없음"}
        return {
            "available": True,
            "pressure": f"{self.pressure:.2f}",
            "flow": "0.3000",
            "pump_on": self.pump_on,
        }

    def set_method_params(self, params: dict) -> dict:
        if self.fail_flow_write:
            raise RuntimeError("장비가 거부함")
        self.flow_writes.append(str(params["flow"]))
        return {}

    def send_pump(self, start: bool) -> dict:
        self.pump_on = bool(start)
        self.pump_commands.append(self.pump_on)
        return {}


def run_inline(action, operator, operation):
    """Stand-in for the command broker: run it now, on this thread."""
    return operation()


def silent_log():
    log = logging.getLogger("test.jetrun")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


class JetRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.instrument = FakeInstrument()
        # A very long interval plus an immediate shutdown keeps the worker
        # thread from ever ticking on its own.
        self.controller = JetRunController(
            self.instrument, run_inline, silent_log(), poll_seconds=3600,
        )
        self.controller.shutdown()
        self.addCleanup(self.controller.shutdown)
        patcher = patch.object(scl40_jetrun.time, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, **overrides) -> None:
        self.controller.start({**CONFIG, **overrides}, "tester")

    def tick(self, seconds: float = 1.0) -> dict:
        self.clock.advance(seconds)
        self.controller._tick()
        return self.controller.state()

    def arm(self) -> dict:
        """Clear the settle window with the pressure below the trigger."""
        return self.tick(SETTLE + 1)

    def kinds(self, state: dict) -> list[str]:
        return [event["kind"] for event in state["events"]]

    # ---- configuration -------------------------------------------------

    def test_fill_flow_must_exceed_run_flow(self) -> None:
        with self.assertRaises(RunError):
            self.start(fill_flow="0.0100")

    def test_threshold_must_stay_below_the_absolute_limit(self) -> None:
        with self.assertRaises(RunError):
            self.start(run_pressure="9.5")

    def test_start_requires_a_session(self) -> None:
        self.instrument.session_id = ""
        with self.assertRaises(RunError):
            self.start()

    def test_a_second_run_cannot_start_while_one_is_active(self) -> None:
        self.start()
        with self.assertRaises(RunError):
            self.start()

    def test_automation_is_locked_out_on_a_multi_pump_system(self) -> None:
        """SYSTEM START would move both pumps, so the run must refuse."""
        self.instrument.get_config = lambda: {
            "pumps": [
                {"unit_id": "A", "model": "LC-40i"},
                {"unit_id": "B", "model": "LC-20Ai"},
            ]
        }
        with self.assertRaises(RunError) as caught:
            self.start()
        self.assertIn("다중 펌프", str(caught.exception))

    # ---- fill stage ----------------------------------------------------

    def test_start_writes_the_fill_flow_and_starts_the_pump(self) -> None:
        self.start()
        self.assertEqual(self.instrument.flow_writes, ["0.3000"])
        self.assertEqual(self.instrument.pump_commands, [True])
        self.assertEqual(self.controller.state()["stage"], STAGE_FILL)

    def test_fill_reacts_to_the_first_sample_at_the_threshold(self) -> None:
        """Filling starts from zero pressure, so there is nothing to settle.

        Waiting here would keep pushing at the fill flow after the plunger has
        already met the sample, which wastes sample.
        """
        self.start()
        self.instrument.pressure = 5.0
        state = self.tick(1)

        self.assertEqual(state["stage"], STAGE_RUN)
        self.assertEqual(self.instrument.flow_writes, ["0.3000", "0.0460"])

    def test_fill_switches_to_the_run_flow_on_the_pressure_jump(self) -> None:
        self.start()
        self.arm()
        self.instrument.pressure = 3.0
        state = self.tick()

        self.assertEqual(state["stage"], STAGE_RUN)
        self.assertEqual(self.instrument.flow_writes, ["0.3000", "0.0460"])
        self.assertEqual(self.instrument.pump_commands, [True], "the pump must keep running")
        self.assertFalse(state["watching"], "the new stage settles before watching again")
        self.assertIn("fill_detected", self.kinds(state))

    def test_a_failed_flow_switch_stops_the_pump(self) -> None:
        self.start()
        self.arm()
        self.instrument.fail_flow_write = True
        self.instrument.pressure = 3.0
        state = self.tick()

        self.assertEqual(state["stage"], STAGE_ERROR)
        self.assertFalse(self.instrument.pump_on)

    # ---- run stage -----------------------------------------------------

    def reach_run_stage(self) -> None:
        self.start()
        self.arm()
        self.instrument.pressure = 3.0
        self.tick()
        self.instrument.pressure = 0.5
        self.arm()

    def test_run_stops_the_pump_on_the_end_of_sample_rise(self) -> None:
        self.reach_run_stage()
        self.instrument.pressure = 1.8
        state = self.tick()

        self.assertEqual(state["stage"], STAGE_FINISHED)
        self.assertEqual(self.instrument.pump_commands, [True, False])
        self.assertFalse(self.instrument.pump_on)
        self.assertIn("end_detected", self.kinds(state))

    def test_run_waits_for_the_pressure_to_fall_before_watching(self) -> None:
        """The fall after the flow change must never be read as a rise.

        Entering the run stage the pressure is still coming down from the fill
        spike and is therefore above the trigger. Watching immediately here
        would stop the pump within a second of starting the experiment.
        """
        self.start()
        self.arm()
        self.instrument.pressure = 3.0
        self.assertEqual(self.tick()["stage"], STAGE_RUN)

        # still high, well past the settle window: must not fire
        state = self.tick(SETTLE + 30)
        self.assertEqual(state["stage"], STAGE_RUN)
        self.assertFalse(state["watching"])
        self.assertEqual(self.instrument.pump_commands, [True])

        self.instrument.pressure = 0.5
        self.assertTrue(self.tick()["watching"])

        self.instrument.pressure = 1.8
        self.assertEqual(self.tick()["stage"], STAGE_FINISHED)

    def test_run_stage_ignores_the_trigger_during_the_settle_window(self) -> None:
        self.start()
        self.arm()
        self.instrument.pressure = 3.0
        self.tick()

        self.instrument.pressure = 0.5
        state = self.tick(SETTLE - 5)
        self.assertFalse(state["watching"])
        self.assertEqual(state["stage"], STAGE_RUN)

    def test_run_keeps_going_below_the_threshold(self) -> None:
        self.reach_run_stage()
        for _ in range(10):
            state = self.tick(5)
        self.assertEqual(state["stage"], STAGE_RUN)
        self.assertEqual(self.instrument.pump_commands, [True])

    def test_peak_pressure_is_tracked_per_stage(self) -> None:
        """The fill spike must not drown out the run stage reading."""
        self.reach_run_stage()
        self.instrument.pressure = 1.2
        self.tick()
        self.instrument.pressure = 0.5
        self.assertEqual(self.tick()["peak"], 1.2)

    # ---- safety --------------------------------------------------------

    def test_absolute_limit_aborts_from_any_stage(self) -> None:
        self.start()
        self.instrument.pressure = 9.4
        state = self.tick()

        self.assertEqual(state["stage"], STAGE_ABORTED)
        self.assertEqual(self.instrument.pump_commands, [True, False])
        self.assertIn("limit", self.kinds(state))

    def test_absolute_limit_beats_the_settle_window(self) -> None:
        self.start()
        self.instrument.pressure = 9.4
        self.assertEqual(self.tick(1)["stage"], STAGE_ABORTED)

    def test_stage_timeout_aborts(self) -> None:
        self.start(stage_timeout=60)
        state = self.tick(61)

        self.assertEqual(state["stage"], STAGE_ABORTED)
        self.assertIn("timeout", self.kinds(state))
        self.assertEqual(self.instrument.pump_commands, [True, False])

    def test_an_external_stop_ends_the_run(self) -> None:
        self.start()
        self.tick(PUMP_START_CONFIRM_SECONDS)  # the pump is seen running
        self.instrument.pump_on = False
        state = self.tick()

        self.assertEqual(state["stage"], STAGE_ABORTED)
        self.assertIn("pump_off", self.kinds(state))

    def test_a_lost_monitor_session_does_not_trigger_anything(self) -> None:
        self.start()
        self.instrument.available = False
        state = self.tick(60)

        self.assertEqual(state["stage"], STAGE_FILL)
        self.assertEqual(self.instrument.pump_commands, [True])

    def test_abort_stops_the_pump(self) -> None:
        self.start()
        state = self.controller.abort("사용자 중단", "tester")

        self.assertEqual(state["stage"], STAGE_ABORTED)
        self.assertEqual(self.instrument.pump_commands, [True, False])

    def test_manual_stop_ends_the_run_without_a_second_command(self) -> None:
        self.start()
        state = self.controller.abort("수동 STOP", "tester", stop_pump=False)

        self.assertEqual(state["stage"], STAGE_ABORTED)
        self.assertEqual(self.instrument.pump_commands, [True])

    # ---- watch-only mode -----------------------------------------------

    def test_watch_mode_touches_nothing_at_start(self) -> None:
        self.start(mode="watch")
        state = self.controller.state()

        self.assertEqual(state["stage"], STAGE_RUN)
        self.assertEqual(self.instrument.flow_writes, [])
        self.assertEqual(self.instrument.pump_commands, [])

    def test_watch_mode_stops_on_the_rise(self) -> None:
        self.start(mode="watch")
        self.instrument.pressure = 0.5
        self.arm()
        self.instrument.pressure = 2.0
        state = self.tick()

        self.assertEqual(state["stage"], STAGE_FINISHED)
        self.assertEqual(self.instrument.pump_commands, [False])

    # ---- reporting -----------------------------------------------------

    def test_a_finished_run_is_summarised(self) -> None:
        self.reach_run_stage()
        self.instrument.pressure = 1.8
        self.tick()

        last = self.controller.state()["last_run"]
        self.assertEqual(last["stage"], STAGE_FINISHED)
        self.assertEqual(last["mode"], "lcp_jet")
        kinds = [event["kind"] for event in last["events"]]
        self.assertEqual(kinds[0], "fill_started")
        self.assertIn("fill_detected", kinds)
        self.assertIn("end_detected", kinds)

    def test_samples_are_forwarded_to_the_trend_recorder(self) -> None:
        samples: list[tuple] = []
        controller = JetRunController(
            self.instrument, run_inline, silent_log(), poll_seconds=3600,
            on_sample=lambda pressure, flow, target: samples.append((pressure, target)),
        )
        controller.shutdown()
        self.addCleanup(controller.shutdown)
        controller.start(dict(CONFIG), "tester")
        controller._tick()

        self.assertEqual(samples[0][0], "0.40")
        self.assertEqual(samples[0][1], Decimal("0.3000"))


if __name__ == "__main__":
    unittest.main()
