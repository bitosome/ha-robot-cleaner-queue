"""Native V1 states omitted from HA's vacuum activity mapping.

Fixtures reflect python-roborock 7.4.2 RoborockStateCode values and HA 2026.9.4
vacuum.py STATE_CODE_TO_STATE omissions. In particular, code 25's display_name
is washing_the_mop, while the HA mapping contains code 23 only. These are cached
observations, never requests to a robot.
"""
from datetime import datetime, timezone
from enum import IntEnum
from types import SimpleNamespace as NS
import unittest

from backend_queue_test import adapter, Queue


class NativeState(IntEnum):
    washing_the_mop_2 = 25
    air_drying_stopping = 202
    robot_status_mopping = 6301
    clean_mop_cleaning = 6302
    clean_mop_mopping = 6303
    segment_mopping = 6304
    segment_clean_mop_cleaning = 6305
    segment_clean_mop_mopping = 6306
    zoned_mopping = 6307
    zoned_clean_mop_cleaning = 6308
    zoned_clean_mop_mopping = 6309
    back_to_dock_washing_duster = 6310

    @property
    def display_name(self):
        return "washing_the_mop" if self.value == 25 else self.name


class NativeStatus:
    def __init__(self, state, job=0, error=0):
        self.state = state
        self.in_cleaning = job
        self.error_code = error
        self.dock_error_status = 0
        self.dry_status = 0

    @property
    def state_name(self):
        return self.state.display_name


def cached_native(state, *, vacuum="unknown", connected=True, job=0, error=0):
    native = NativeStatus(state, job, error)
    coordinator = NS(
        data=NS(status=native, clean_summary=NS(last_clean_record=None)),
        last_update_success=connected,
        _last_update_success_time=datetime.fromtimestamp(120, timezone.utc),
    )
    return adapter.snapshot(coordinator, vacuum)


def manual_queue(phase):
    return Queue(
        phase=phase, vacuum="vacuum.robot", mode="manual", run_id="run",
        stages=[{"mode": "mop", "target": "0_1", "settings": {"mode": "mop"}}],
        command_at=100, started_at=100, seen_job=True,
    )


class NativeActivityTests(unittest.TestCase):
    def test_native_wash_alias_is_care_even_when_ha_activity_is_unknown(self):
        current = cached_native(NativeState.washing_the_mop_2)
        self.assertEqual(current.status, "washing_the_mop")
        self.assertEqual(current.vacuum, "docked")
        self.assertTrue(current.servicing_for("mop"))
        self.assertFalse(current.ready_for("mop"))
        queue = manual_queue("preparing")
        queue.pending_command = "configure"
        self.assertIsNone(queue.observe(current, 120))
        self.assertEqual(queue.phase, "preparing")
        self.assertIn("dock care", queue.decision)

    def test_final_washing_waits_without_false_telemetry_fault_or_completion(self):
        current = cached_native(NativeState.washing_the_mop_2)
        queue = manual_queue("finishing")
        queue.current_index = queue.completed = 1
        queue.dock_finish_at = 110
        self.assertIsNone(queue.observe(current, 120))
        self.assertEqual(queue.phase, "finishing")
        self.assertEqual(queue.error, "")

    def test_all_native_mop_activity_variants_remain_busy(self):
        for state in NativeState:
            if not 6301 <= state.value <= 6309:
                continue
            with self.subTest(state=state):
                current = cached_native(state, job=3)
                self.assertEqual(current.vacuum, "cleaning")
                self.assertTrue(current.healthy_for("mop"))
                self.assertFalse(current.ready_for("mop"))
                queue = manual_queue("running")
                self.assertIsNone(queue.observe(current, 120))
                self.assertEqual(queue.phase, "running")
                self.assertEqual(queue.error, "")

    def test_native_return_to_wash_stays_busy_until_docking(self):
        current = cached_native(NativeState.back_to_dock_washing_duster, job=3)
        self.assertEqual(current.vacuum, "returning")
        self.assertTrue(current.healthy_for("mop"))
        self.assertFalse(current.ready_for("mop"))
        queue = manual_queue("running")
        self.assertIsNone(queue.observe(current, 120))
        self.assertEqual(queue.phase, "running")

    def test_stopping_drying_is_dock_care_not_ready(self):
        current = cached_native(NativeState.air_drying_stopping)
        self.assertTrue(current.servicing_for("mop"))
        self.assertFalse(current.ready_for("mop"))

    def test_disconnected_unavailable_and_error_are_never_reclassified(self):
        for vacuum, connected in [("unavailable", True), ("error", True), ("unknown", False)]:
            with self.subTest(vacuum=vacuum, connected=connected):
                current = cached_native(NativeState.washing_the_mop_2, vacuum=vacuum, connected=connected)
                self.assertEqual(current.vacuum, vacuum)
                self.assertFalse(current.healthy_for("mop"))

    def test_robot_errors_are_still_errors_with_known_native_activity(self):
        current = cached_native(NativeState.washing_the_mop_2, error=42)
        self.assertEqual(current.vacuum, "docked")
        self.assertFalse(current.healthy_for("mop"))

    def test_existing_ha_activity_is_not_overridden(self):
        current = cached_native(NativeState.segment_mopping, vacuum="paused", job=3)
        self.assertEqual(current.vacuum, "paused")

    def test_unrecognized_and_missing_native_codes_fail_closed(self):
        for code in [0, 8, 9999, None, True, "25"]:
            with self.subTest(code=code):
                # A known display label alone must not normalize a new firmware code.
                state = NS(value=code, display_name="washing_the_mop")
                current = cached_native(state)
                self.assertEqual(current.vacuum, "unknown")
                self.assertFalse(current.healthy_for("mop"))


if __name__ == "__main__":
    unittest.main()
