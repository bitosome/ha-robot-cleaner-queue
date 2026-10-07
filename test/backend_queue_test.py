"""Offline event traces exercise production transition code, not a duplicate model."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components/robot_cleaner_queue"
namespace = types.ModuleType("queue_test_package")
namespace.__path__ = [str(PACKAGE)]
sys.modules[namespace.__name__] = namespace

def load(name):
    spec = importlib.util.spec_from_file_location("queue_test_package." + name, PACKAGE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

engine = load("engine")
adapter = load("adapter")
permissions = load("permissions")
Queue, Snapshot = engine.Queue, engine.Snapshot

def ready(record=None):
    return Snapshot("docked", "charging", "off", "none", "ok", True, record)

def cleaning(record=None):
    return Snapshot("cleaning", "segment_cleaning", "on", "none", "ok", True, record)

def record(begin=100, end=200, complete=1, error=0, finish_reason=52):
    return dict(begin=begin, end=end, complete=complete, error=error, finish_reason=finish_reason)

class QueueTests(unittest.TestCase):
    def start(self, presets=None):
        queue = Queue()
        effect = queue.start("vacuum.robot", presets or ["button.kitchen", "button.office"], ready(record(10, 20)), 100, "run1")
        self.assertEqual(effect, ("preset", (presets or ["button.kitchen"])[0]))
        return queue

    def acknowledged(self, presets=None):
        queue = self.start(presets)
        self.assertIsNone(queue.observe(cleaning(), 105))
        self.assertEqual(queue.phase, "running")
        self.assertEqual(queue.pending_command, "")
        return queue

    def test_order_two_rooms_only_after_success_and_ready(self):
        q = self.acknowledged()
        q.observe(Snapshot("returning", "returning_home", "off", "none", "ok", True, record()), 202)
        self.assertEqual((q.completed, q.current_index), (1, 1))
        self.assertIsNone(q.observe(Snapshot("docked", "washing_the_mop", "off", "none", "ok", True, record()), 205))
        self.assertEqual(q.observe(ready(record()), 220), ("preset", "button.office"))
        self.assertEqual(q.pending_command, "start")
        q.observe(cleaning(record()), 225)
        q.observe(ready(record(220, 300)), 305)
        self.assertEqual((q.phase, q.completed), ("completed", 2))
        self.assertIsNone(q.observe(ready(record(220, 300)), 400))

    def test_initial_mop_wash_acknowledges_without_claiming_active_job(self):
        q = self.start()
        wash = Snapshot("docked", "washing_the_mop", "off", "none", "ok", True)
        q.observe(wash, 105)
        self.assertEqual((q.phase, q.pending_command, q.seen_job), ("running", "", False))
        q.observe(wash, 180)
        self.assertEqual(q.completed, 0)
        q.observe(cleaning(), 240)
        self.assertTrue(q.seen_job)
        q.observe(ready(record(100, 300)), 310)
        self.assertEqual(q.completed, 1)

    def test_initial_wash_never_starting_job_stops_after_timeout(self):
        q = self.start()
        wash = Snapshot("docked", "washing_the_mop", "off", "none", "ok", True)
        q.observe(wash, 105)
        q.observe(wash, 1601)
        self.assertEqual(q.phase, "attention")
        self.assertEqual(q.completed, 0)

    def test_low_battery_and_wash_break_do_not_advance(self):
        q = self.acknowledged()
        for status in ["washing_the_mop", "going_to_wash_the_mop", "charging"]:
            self.assertIsNone(q.observe(Snapshot("docked", status, "on", "none", "ok", True, record()), 200))
            self.assertEqual((q.phase, q.current_index, q.completed), ("running", 0, 0))

    def test_app_cancel_record_never_starts_next_room(self):
        for complete, reason in [(0, 21), (1, 21), (0, 52), (None, None)]:
            q = self.acknowledged()
            q.observe(ready(record(complete=complete, finish_reason=reason)), 210)
            self.assertEqual(q.phase, "attention")
            self.assertEqual(q.completed, 0)
            self.assertIsNone(q.observe(ready(record()), 240))

    def test_failure_then_clear_error_does_not_resume_queue(self):
        q = self.acknowledged()
        q.observe(Snapshot("error", "error", "on", "stuck", "ok", True), 150)
        self.assertEqual(q.phase, "attention")
        self.assertIsNone(q.observe(ready(record()), 300))

    def test_dock_fault_and_lost_telemetry_stop_queue(self):
        for s in [Snapshot(), Snapshot("docked", "charging", "off", "none", "error", True)]:
            q = self.acknowledged()
            q.observe(s, 150)
            self.assertEqual(q.phase, "attention")

    def test_old_cached_end_cannot_complete_new_room(self):
        q = self.acknowledged()
        q.observe(ready(record(10, 20)), 200)
        self.assertEqual(q.completed, 0)
        q.observe(ready(record(10, 20)), 381)
        self.assertEqual(q.phase, "attention")

    def test_begin_before_request_rejects_unrelated_record(self):
        q = self.acknowledged()
        q.observe(ready(record(50, 200)), 210)
        self.assertEqual(q.completed, 0)
        q.observe(ready(record(50, 200)), 391)
        self.assertEqual(q.phase, "attention")

    def test_missed_ack_never_retries(self):
        q = self.start()
        # Docked and charging is not an acknowledgement: the dock may still be servicing.
        self.assertIsNone(q.observe(ready(record()), 300))
        self.assertEqual((q.phase, q.pending_command), ("starting", "start"))
        # The start window ends without a retry ever being sent.
        self.assertIsNone(q.observe(ready(record()), 1001))
        self.assertEqual(q.phase, "attention")
        self.assertEqual(q.completed, 0)

    def test_slow_start_after_dock_servicing_is_awaited(self):
        """A routine can first appear minutes after the press (production: 677 s)."""
        q = self.acknowledged()
        q.observe(Snapshot("returning", "returning_home", "off", "none", "ok", True, record()), 202)
        self.assertEqual((q.completed, q.current_index), (1, 1))
        self.assertEqual(q.observe(ready(record()), 205), ("preset", "button.office"))
        # The dock services the mop while the robot sits on it, charging.
        for timestamp in (260, 400, 700):
            self.assertIsNone(q.observe(Snapshot("docked", "charging", "off", "none", "ok", True, record()), timestamp))
            self.assertEqual((q.phase, q.pending_command), ("starting", "start"))
        # 677 seconds after the press the robot begins cleaning, and the queue continues.
        self.assertIsNone(q.observe(Snapshot("cleaning", "segment_cleaning", "on", "none", "ok", True, record()), 882))
        self.assertEqual((q.phase, q.pending_command), ("running", ""))
        q.observe(ready(record(205, 900)), 905)
        self.assertEqual((q.phase, q.completed), ("completed", 2))

    def test_busy_robot_or_unfinished_docked_job_reject_start(self):
        for s in [cleaning(), Snapshot("docked", "charging", "on", "none", "ok", True), Snapshot()]:
            with self.assertRaises(ValueError):
                Queue().start("vacuum.robot", ["button.office"], s, 100, "run")

    def test_concurrent_start_rejected_preserving_queue(self):
        q = self.acknowledged()
        saved = q.dump()
        with self.assertRaises(ValueError):
            q.start("vacuum.robot", ["button.bedroom"], ready(), 120, "run2")
        self.assertEqual(q.dump(), saved)

    def test_pause_resume_wait_for_ack_and_preserve_start(self):
        q = self.acknowledged()
        self.assertEqual(q.command("pause", cleaning(), 120), ("vacuum", "pause"))
        q.observe(cleaning(), 125)
        self.assertEqual(q.pending_command, "pause")
        paused = Snapshot("paused", "paused", "on", "none", "ok", True)
        q.observe(paused, 130)
        self.assertEqual((q.phase, q.pending_command), ("paused", ""))
        self.assertEqual(q.command("resume", paused, 140), ("vacuum", "start"))
        self.assertEqual(q.started_at, 100)
        q.observe(cleaning(), 150)
        self.assertEqual(q.phase, "running")

    def test_pause_not_allowed_during_servicing(self):
        q = self.acknowledged()
        with self.assertRaises(ValueError):
            q.command("pause", Snapshot("docked", "washing_the_mop", "on", "none", "ok", True), 150)

    def test_cancel_does_not_stop_current_robot_or_advance(self):
        q = self.acknowledged()
        self.assertIsNone(q.command("cancel", cleaning(), 150))
        self.assertEqual(q.phase, "cancelled")
        self.assertIsNone(q.observe(ready(record()), 220))
        self.assertEqual(q.completed, 0)

    def test_return_cancels_before_action_and_needs_ack(self):
        q = self.acknowledged()
        self.assertEqual(q.command("return_to_dock", cleaning(), 150), ("vacuum", "return_to_base"))
        self.assertEqual(q.phase, "cancelled")
        q.observe(Snapshot("returning", "returning_home", "on", "none", "ok", True), 155)
        self.assertEqual(q.pending_command, "")
        self.assertIsNone(q.observe(ready(record()), 220))

    def test_return_during_servicing_clears_queue_without_interrupt(self):
        q = self.acknowledged()
        s = Snapshot("cleaning", "washing_the_mop", "on", "none", "ok", True)
        self.assertIsNone(q.command("return_to_dock", s, 150))
        self.assertEqual(q.phase, "attention")

    def test_restart_never_sends_anything(self):
        for q in [self.start(), self.acknowledged()]:
            restored = Queue.restore(q.dump())
            self.assertEqual(restored.phase, "attention")
            self.assertEqual(restored.presets, q.presets)
            self.assertIsNone(restored.observe(ready(record()), 1000))

    def test_cancel_or_dock_pending_start_keeps_uncertainty_barrier(self):
        for command in ["cancel", "return_to_dock"]:
            q = self.start()
            q.command(command, ready(), 101)
            self.assertEqual(q.phase, "cancelled")
            self.assertEqual(q.not_before, 1000)
            for timestamp in [102, 1001]:
                with self.assertRaises(ValueError):
                    q.start("vacuum.robot", ["button.bedroom"], ready(), timestamp, "new")
            # A late acknowledgement cannot cause the next routine to be sent.
            self.assertIsNone(q.observe(cleaning(), 110))
            fresh_but_busy = cleaning()
            fresh_but_busy.observed_at = 1005
            with self.assertRaises(ValueError):
                q.start("vacuum.robot", ["button.bedroom"], fresh_but_busy, 1010, "new")
            fresh_ready = ready()
            fresh_ready.observed_at = 1005
            self.assertEqual(q.start("vacuum.robot", ["button.bedroom"], fresh_ready, 1010, "new"), ("preset", "button.bedroom"))

    def test_clear_after_start_timeout_cannot_bypass_stale_snapshot(self):
        q = self.start()
        q.observe(ready(), 1001)
        self.assertEqual(q.phase, "attention")
        q.command("cancel", ready(), 1002)
        with self.assertRaises(ValueError):
            q.start("vacuum.robot", ["button.bedroom"], ready(), 1003, "new")
        restored = Queue.restore(q.dump())
        self.assertEqual(restored.not_before, 1000)
        with self.assertRaises(ValueError):
            restored.start("vacuum.robot", ["button.bedroom"], ready(), 1010, "new")

    def test_empty_duplicate_and_excessive_sequences_rejected(self):
        for presets in [[], ["button.a", "button.a"], ["button.a%d" % n for n in range(33)]]:
            with self.assertRaises(ValueError):
                Queue().start("vacuum.robot", presets, ready(), 100, "run")

    def test_external_pause_is_visible_and_external_resume_interrupts(self):
        q = self.acknowledged()
        q.observe(Snapshot("paused", "paused", "on", "none", "ok", True), 130)
        self.assertEqual(q.phase, "paused")
        q.observe(cleaning(), 140)
        self.assertEqual(q.phase, "attention")

    def test_unknown_finish_reason_and_record_error_fail_closed(self):
        for r in [record(finish_reason=999), record(error=1), record(error=None)]:
            q = self.acknowledged()
            q.observe(ready(r), 220)
            self.assertEqual(q.phase, "attention")

    def test_other_job_before_next_dispatch_stops_queue(self):
        q = self.acknowledged()
        q.observe(ready(record()), 220)
        q.observe(cleaning(), 230)
        self.assertEqual(q.phase, "attention")

class AdapterTests(unittest.TestCase):
    def entry(self, **kwargs):
        return types.SimpleNamespace(**dict(dict(platform="roborock", domain="button", device_id="robot1", config_entry_id="entry1", disabled_by=None, unique_id="1234_robot1"), **kwargs))

    def test_only_same_device_numeric_routine_ids_allowed(self):
        vacuum = self.entry(domain="vacuum")
        coordinator = types.SimpleNamespace(duid_slug="robot1")
        self.assertTrue(adapter.routine_matches(self.entry(), vacuum, coordinator))
        for entry in [None, self.entry(unique_id="reset_sensor_consumable_robot1"), self.entry(device_id="robot2"), self.entry(config_entry_id="entry2"), self.entry(disabled_by="user"), self.entry(platform="template"), self.entry(unique_id="1234_robot2")]:
            self.assertFalse(adapter.routine_matches(entry, vacuum, coordinator))

    def test_missing_cached_data_and_unknown_error_fail_closed(self):
        self.assertFalse(adapter.snapshot(types.SimpleNamespace(), "docked").healthy)
        status = types.SimpleNamespace(state_name="charging", in_cleaning=0, error_code=None, dock_error_status=0)
        coordinator = types.SimpleNamespace(last_update_success=True, data=types.SimpleNamespace(status=status, clean_summary=types.SimpleNamespace(last_clean_record=None)))
        self.assertFalse(adapter.snapshot(coordinator, "docked").healthy)
        status.error_code = 0
        self.assertTrue(adapter.snapshot(coordinator, "docked").ready)

    def test_external_commands_handle_explicit_and_expanded_targets(self):
        check = lambda domain, service, data: adapter.is_competing_command(domain, service, data, "vacuum.robot", lambda eid: eid == "button.robot_kitchen")
        self.assertTrue(check("button", "press", {"entity_id": "button.robot_kitchen"}))
        self.assertFalse(check("button", "press", {"entity_id": "button.other_robot"}))
        self.assertFalse(check("vacuum", "pause", {"entity_id": "vacuum.other"}))
        self.assertTrue(check("vacuum", "pause", {"entity_id": "vacuum.robot"}))
        for target in [{}, {"entity_id": "all"}, {"device_id": "robot"}, {"area_id": "kitchen"}, {"label_id": "floor"}, {"floor_id": "first"}, {"entity_id": "button.other", "device_id": "robot"}]:
            self.assertTrue(check("button", "press", target))
            self.assertTrue(check("vacuum", "start", target))
        self.assertFalse(check("script", "turn_on", {"entity_id": "script.queue"}))

    def test_completion_adapter_exports_only_allowlisted_numbers(self):
        rec = types.SimpleNamespace(**record(), private_data="never export")
        self.assertEqual(adapter.clean_record(rec), record())
        self.assertNotIn("private_data", adapter.clean_record(rec))

class PermissionTests(unittest.IsolatedAsyncioTestCase):
    def auth(self, user):
        async def get_user(user_id):
            return user
        return types.SimpleNamespace(async_get_user=get_user)

    def user(self, allowed=(), admin=False, active=True):
        allowed = set(allowed)
        checks = []
        def check(entity, policy):
            checks.append((entity, policy))
            return entity in allowed
        return types.SimpleNamespace(is_active=active, is_admin=admin, permissions=types.SimpleNamespace(check_entity=check), checks=checks, allowed=allowed)

    async def test_read_only_user_and_partial_access_cannot_start(self):
        for allowed in [[], ["vacuum.robot"], ["button.kitchen"]]:
            with self.assertRaises(PermissionError):
                await permissions.async_require_control(self.auth(self.user(allowed)), "user", ["vacuum.robot", "button.kitchen"], "control")

    async def test_authorized_user_checks_every_entity_with_control_policy(self):
        user = self.user(["vacuum.robot", "button.kitchen", "button.office"])
        await permissions.async_require_control(self.auth(user), "user", ["vacuum.robot", "button.kitchen", "button.office"], "control")
        self.assertEqual(set(user.checks), {("vacuum.robot", "control"), ("button.kitchen", "control"), ("button.office", "control")})

    async def test_revoked_permissions_are_rechecked_before_continuation(self):
        user = self.user(["vacuum.robot", "button.kitchen"])
        auth = self.auth(user)
        await permissions.async_require_control(auth, "user", ["vacuum.robot", "button.kitchen"], "control")
        user.allowed.remove("button.kitchen")
        with self.assertRaises(PermissionError):
            await permissions.async_require_control(auth, "user", ["vacuum.robot", "button.kitchen"], "control")

    async def test_deleted_inactive_admin_and_automation_contexts(self):
        for user in [None, self.user(admin=True, active=False)]:
            with self.assertRaises(PermissionError):
                await permissions.async_require_control(self.auth(user), "user", ["vacuum.robot"], "control")
        await permissions.async_require_control(self.auth(self.user(admin=True)), "admin", ["vacuum.robot"], "control")
        await permissions.async_require_control(self.auth(None), None, ["vacuum.robot"], "control")

    def test_owner_survives_restore_without_exposing_it_on_sensor(self):
        queue = Queue(owner_user_id="user123")
        self.assertEqual(Queue.restore(queue.dump()).owner_user_id, "user123")
        sensor_source = (PACKAGE / "sensor.py").read_text()
        self.assertNotIn("owner_user_id", sensor_source)

if __name__ == "__main__":
    unittest.main(verbosity=2)
