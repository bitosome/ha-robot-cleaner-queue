"""Offline shared-controller checks for jobs started outside a queue."""
import asyncio
from pathlib import Path
import sys
import time
import types
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import backend_manual_test as manual_tests
import backend_queue_test as queue_tests

Queue, Snapshot = queue_tests.Queue, queue_tests.Snapshot
NS = types.SimpleNamespace


def state(vacuum="cleaning", status="segment_cleaning", job="on", observed_at=100):
    return Snapshot(vacuum, status, job, "none", "ok", True, observed_at=observed_at)


def stage(target, index=0, mode="vacuum"):
    return {"target": target, "mode": mode, "room_index": index, "pass_index": 0, "repeat_index": 0,
            "settings": {"mode": mode}, "map_id": 0, "segments": [str(index + 1)]}


ONE_STAGE = [stage("0_1")]
TWO_STAGES = [stage("0_1", 0), stage("0_2", 1)]


class StandaloneEngineTests(unittest.TestCase):
    def test_each_action_requires_fresh_confirmation_then_becomes_idle(self):
        cases = [
            ("pause", state(), state("paused", "paused"), "pause"),
            ("resume", state("paused", "paused"), state(), "start"),
            ("return_to_dock", state(), state("returning", "returning_home"), "return_to_base"),
        ]
        for command, before, after, service in cases:
            with self.subTest(command=command):
                queue = Queue()
                self.assertEqual(queue.external_control(command, "vacuum.robot", before, 100, "request"), ("vacuum", service))
                self.assertEqual((queue.mode, queue.phase, queue.pending_command), ("external", "controlling", command))
                after.observed_at = 99
                queue.observe(after, 101)
                self.assertEqual(queue.pending_command, command)
                after.observed_at = 102
                queue.observe(after, 102)
                self.assertEqual((queue.phase, queue.pending_command, queue.completed), ("idle", "", 0))

    def test_terminal_manual_history_never_adopted_or_advanced(self):
        queue = Queue(mode="manual", phase="cancelled", vacuum="vacuum.robot",
                      targets=["kitchen"], stages=[{"target": "kitchen"}], setup={"mode": "mop"},
                      control_entities={"mode": "select.mode"}, completed=1, current_index=1, seen_job=True, next_pending=True)
        queue.external_control("resume", "vacuum.robot", state("paused", "paused"), 100, "request")
        self.assertEqual((queue.targets, queue.stages, queue.setup, queue.control_entities), ([], [], {}, {}))
        self.assertEqual((queue.current_index, queue.completed, queue.seen_job, queue.next_pending), (0, 0, False, False))
        queue.observe(state(observed_at=101), 101)
        if True:
            done = state("docked", "charging", "off", observed_at=200)
            done.record = queue_tests.record()
            for now in [200, 300, 500]:
                self.assertIsNone(queue.observe(done, now))
            self.assertEqual((queue.phase, queue.completed), ("idle", 0))

    def test_active_and_pending_plans_reject_without_mutation(self):
        for queue in [Queue(phase="running"), Queue(phase="idle", pending_command="pause"), Queue(phase="controlling", mode="external")]:
            before = queue.dump()
            with self.assertRaises(ValueError):
                queue.external_control("pause", "vacuum.robot", state(), 100, "request")
            self.assertEqual(queue.dump(), before)

    def test_invalid_fault_and_servicing_states_never_dispatch(self):
        for command, current in [
            ("pause", state("docked", "washing_the_mop", "on")),
            ("resume", state("paused", "paused", "off")),
            ("resume", state()), ("return_to_dock", state("idle", "attaching_the_mop")),
            ("pause", Snapshot()),
        ]:
            queue = Queue()
            before = queue.dump()
            with self.assertRaises(ValueError):
                queue.external_control(command, "vacuum.robot", current, 100, "request")
            self.assertEqual(queue.dump(), before)

    def test_already_docked_or_returning_is_noop(self):
        for current in [state("docked", "charging", "off"), state("returning", "returning_home")]:
            queue = Queue()
            self.assertIsNone(queue.external_control("return_to_dock", "vacuum.robot", current, 100, "request"))
            self.assertEqual(queue.dump(), Queue().dump())

    def test_timeout_cancel_restart_and_fault_keep_barrier(self):
        for reason in ["timeout", "cancel", "restart", "fault"]:
            queue = Queue()
            queue.external_control("pause", "vacuum.robot", state(), 100, "request")
            if reason == "timeout":
                queue.observe(state(observed_at=160), 161)
            elif reason == "cancel":
                queue.command("cancel", Snapshot(), 110)
            elif reason == "restart":
                queue = Queue.restore(queue.dump())
            else:
                queue.observe(Snapshot(), 110)
            self.assertEqual(queue.not_before, 160)
            queue.command("cancel", Snapshot(), 165)
            with self.assertRaises(ValueError):
                queue.external_control("pause", "vacuum.robot", state(observed_at=159), 165, "new")
            self.assertEqual(queue.external_control("pause", "vacuum.robot", state(observed_at=166), 166, "new"), ("vacuum", "pause"))

    def test_stale_telemetry_cannot_confirm_a_command(self):
        """A snapshot older than the command cannot stand in for it landing."""
        queue = Queue(phase="running", vacuum="vacuum.robot", mode="manual", targets=["0_1"], stages=ONE_STAGE, started_at=100)
        self.assertEqual(queue.command("pause", state(), 200), ("vacuum", "pause"))
        # The robot already looked paused at 190, before the command was sent at 200.
        self.assertIsNone(queue.observe(state("paused", "paused", "on", observed_at=190), 201))
        self.assertEqual(queue.pending_command, "pause")
        self.assertIsNone(queue.observe(state("paused", "paused", "on", observed_at=205), 206))
        self.assertEqual((queue.phase, queue.pending_command), ("paused", ""))

    def test_barrier_reports_the_window_it_actually_guards(self):
        queue = Queue(phase="starting", vacuum="vacuum.robot", pending_command="start", command_at=100)
        queue.command("cancel", Snapshot(), 110)
        self.assertEqual(queue.not_before, 1000)
        with self.assertRaises(ValueError) as caught:
            queue.start_manual("vacuum.robot", ["0_1"], {}, ONE_STAGE, {},
                               Snapshot("docked", "charging", "off", "none", "ok", True, observed_at=200), 200, "new")
        self.assertIn("15 minutes", str(caught.exception))

    def test_finishing_refuses_to_discard_a_device_reservation(self):
        queue = Queue(mode="device", phase="controlling", vacuum="vacuum.robot",
                      pending_command="device", command_at=100, setup={"control": "mop_washing"},
                      control_entities={"device": "switch.robot_mop_washing"})
        with self.assertRaisesRegex(ValueError, "still being confirmed"):
            queue.finish("vacuum.robot", state("docked", "charging", "off", observed_at=101), 101, "finish")
        self.assertEqual((queue.mode, queue.pending_command), ("device", "device"))

    def test_confirmed_command_releases_its_barrier(self):
        """A stop the robot has confirmed must not lock docking behind a window."""
        queue = Queue(phase="running", vacuum="vacuum.robot", mode="manual", targets=["0_1"], stages=ONE_STAGE, started_at=100)
        self.assertEqual(queue.command("stop", state(), 100), ("vacuum", "stop"))
        # The robot reports the stop: idle with no active job.
        queue.observe(state("idle", "idle", "off", observed_at=104), 104)
        self.assertEqual((queue.phase, queue.pending_command, queue.not_before), ("cancelled", "", 0))
        self.assertEqual(queue.external_control("return_to_dock", "vacuum.robot",
                                                state("idle", "idle", "off", observed_at=105), 105, "dock"),
                         ("vacuum", "return_to_base"))

    def test_unconfirmed_command_still_keeps_its_barrier(self):
        queue = Queue(phase="running", vacuum="vacuum.robot", mode="manual", targets=["0_1"], stages=ONE_STAGE, started_at=100)
        queue.command("stop", state(), 100)
        queue.observe(state("cleaning", "segment_cleaning", "on", observed_at=101), 101)
        self.assertEqual(queue.pending_command, "stop")
        self.assertEqual(queue.not_before, 0)
        with self.assertRaises(ValueError):
            queue.external_control("return_to_dock", "vacuum.robot", state(observed_at=150), 150, "dock")

    def test_uncertain_resume_blocks_new_preset_until_fresh_idle(self):
        queue = Queue()
        queue.external_control("resume", "vacuum.robot", state("paused", "paused"), 100, "request")
        queue.command("cancel", Snapshot(), 110)
        idle = state("docked", "charging", "off", observed_at=110)
        with self.assertRaises(ValueError):
            queue.start_manual("vacuum.other", ["0_1"], {}, ONE_STAGE, {}, idle, 120, "new")
        idle.observed_at = 1005
        self.assertEqual(queue.start_manual("vacuum.robot", ["0_1"], {}, ONE_STAGE, {}, idle, 1005, "new"), ("configure", "0"))


    def test_owned_dock_cancels_future_stages_but_respects_existing_barrier(self):
        queue = Queue(phase="attention", vacuum="vacuum.robot", mode="manual", targets=["0_1", "0_2"], stages=TWO_STAGES, not_before=160)
        paused = state("paused", "paused", observed_at=110)
        for now in [120, 165]:
            self.assertIsNone(queue.command("return_to_dock", paused, now))
            self.assertEqual((queue.phase, queue.pending_command, queue.next_pending), ("attention", "", False))
            self.assertEqual(queue.not_before, 160)
        paused.observed_at = 166
        self.assertEqual(queue.command("return_to_dock", paused, 166), ("vacuum", "return_to_base"))

    def test_owned_pending_commands_cannot_be_replaced_with_dock(self):
        for pending in ["start", "pause", "resume", "return_to_dock"]:
            queue = Queue(phase="running", vacuum="vacuum.robot", mode="manual", targets=["0_1", "0_2"], stages=TWO_STAGES,
                          pending_command=pending, command_at=100, next_pending=True)
            self.assertIsNone(queue.command("return_to_dock", state(observed_at=110), 110))
            barrier = 1000 if pending in {"start", "resume"} else 160
            self.assertEqual((queue.phase, queue.pending_command, queue.not_before, queue.next_pending), ("attention", "", barrier, False))
            self.assertIsNone(queue.command("return_to_dock", state(observed_at=111), 111))
            self.assertIsNone(queue.observe(state(observed_at=170), 170))

    def test_owned_resume_requires_unfinished_job_but_preserves_queued_next_stage(self):
        queue = Queue(phase="paused", vacuum="vacuum.robot", mode="manual", targets=["0_1", "0_2"], stages=TWO_STAGES, started_at=100)
        before = queue.dump()
        with self.assertRaisesRegex(ValueError, "unfinished"):
            queue.command("resume", state("paused", "paused", "off"), 150)
        self.assertEqual(queue.dump(), before)
        queue.next_pending = True
        queue.current_index = queue.completed = 1
        self.assertIsNone(queue.command("resume", state("docked", "charging", "off"), 150))
        self.assertIsNone(queue.observe(state("docked", "charging", "off", observed_at=150), 150))
        self.assertEqual(queue.observe(state("docked", "charging", "off", observed_at=166), 166), ("configure", "1"))


class RecoveryEngineTests(unittest.TestCase):
    def stopped(self):
        return Queue(phase="attention", mode="manual", vacuum="vacuum.robot",
                     targets=["0_1", "0_2"], stages=TWO_STAGES,
                     completed=1, current_index=1, command_at=100,
                     settings_sent_at=95, started_at=100, baseline_end=80,
                     seen_job=True, finish_wait_at=70, not_before=1000,
                     barrier_window=900, error="The previous start failed.",
                     command_failure={"category": "unknown"}, decision="stopped old run",
                     run_id="old")

    def test_explicit_new_plan_replaces_resolved_attention_without_replaying_history(self):
        q = self.stopped()
        idle = state("docked", "charging", "off", observed_at=1990)
        self.assertFalse(q.should_finish(idle), "A wall-switch hold should choose a new saved plan")
        self.assertEqual(q.start_manual("vacuum.robot", ["0_1"], {}, ONE_STAGE, {}, idle, 2000, "new"),
                         ("configure", "0"))
        self.assertEqual((q.phase, q.current_index, q.completed, q.run_id), ("preparing", 0, 0, "new"))
        self.assertEqual(q.stages, ONE_STAGE)
        self.assertEqual((q.started_at, q.baseline_end, q.finish_wait_at, q.seen_job), (0, 0, 0, False))
        self.assertEqual((q.error, q.command_failure, q.not_before), ("", {}, 0))
        self.assertNotIn("old", q.decision)
        idle.settings = {"mode": "vacuum"}
        idle.observed_at = 2001
        self.assertIsNone(q.observe(idle, 2001))
        idle.observed_at = 2017
        self.assertEqual(q.observe(idle, 2017), ("manual", "0"))
        self.assertEqual(q.targets, ["0_1"])

    def test_attention_never_automatically_restarts_on_fresh_idle(self):
        q = self.stopped()
        before = q.dump()
        idle = state("docked", "charging", "off", observed_at=2000)
        for now in [2000, 2016, 3000]:
            idle.observed_at = now
            self.assertIsNone(q.observe(idle, now))
        self.assertEqual(q.dump(), before)

    def test_recovery_is_read_only_and_rejects_uncertainty_stale_or_unfinished_state(self):
        for case in ["active", "pending", "deadline", "before_deadline", "undated", "stale", "before_command",
                     "busy", "unfinished", "washing", "offline", "robot_fault", "water_fault"]:
            with self.subTest(case=case):
                q = self.stopped()
                current = state("docked", "charging", "off", observed_at=2000)
                if case == "active": q.phase = "finishing"
                elif case == "pending": q.pending_command = "device"
                elif case == "deadline": q.not_before = 2001
                elif case == "before_deadline": current.observed_at = 999
                elif case == "undated": current.observed_at = 0
                elif case == "stale": current.observed_at = 1909
                elif case == "before_command": q.command_at = 2001
                elif case == "busy": current.vacuum, current.status, current.job = "cleaning", "segment_cleaning", "on"
                elif case == "unfinished": current.job = "on"
                elif case == "washing": current.status = "washing_the_mop"
                elif case == "offline": current.connected = False
                elif case == "robot_fault": current.error = "error"
                else: current.dock_error = "water_empty"
                before = q.dump()
                with self.assertRaises(ValueError):
                    q.validate_recovery(current, 2000, "mop" if case == "water_fault" else "vacuum")
                self.assertEqual(q.dump(), before)
                with self.assertRaises(ValueError):
                    q.start_manual("vacuum.robot", ["0_1"], {"mode": "mop" if case == "water_fault" else "vacuum"},
                                   ONE_STAGE, {}, current, 2000, "new")
                self.assertEqual(q.dump(), before)

    def test_recovery_allows_vacuum_only_with_water_empty_and_does_not_mutate(self):
        q = self.stopped()
        current = state("docked", "charging", "off", observed_at=1910)
        current.dock_error = "water_empty"
        before = q.dump()
        self.assertIsNone(q.validate_recovery(current, 2000, "vacuum"))
        self.assertEqual(q.dump(), before)

    def test_other_vacuum_cannot_discard_stopped_queue(self):
        q = self.stopped()
        before = q.dump()
        with self.assertRaisesRegex(ValueError, "different vacuum"):
            q.start_manual("vacuum.other", ["0_1"], {}, ONE_STAGE, {},
                           state("docked", "charging", "off", observed_at=2000), 2000, "new")
        self.assertEqual(q.dump(), before)

    def test_restart_attention_can_be_replaced_only_after_original_start_barrier(self):
        q = Queue(phase="starting", mode="manual", vacuum="vacuum.robot", targets=["0_1"],
                  stages=ONE_STAGE, pending_command="start", command_at=100, run_id="old")
        q = Queue.restore(q.dump())
        self.assertEqual((q.phase, q.not_before), ("attention", 1000))
        idle = state("docked", "charging", "off", observed_at=999)
        with self.assertRaises(ValueError):
            q.start_manual("vacuum.robot", ["0_1"], {}, ONE_STAGE, {}, idle, 1001, "new")
        idle.observed_at = 1001
        self.assertEqual(q.start_manual("vacuum.robot", ["0_1"], {}, ONE_STAGE, {}, idle, 1001, "new"),
                         ("configure", "0"))

    def test_native_job_can_be_controlled_from_stopped_history_without_resuming_old_stages(self):
        cases = [
            ("pause", state(), state("paused", "paused"), "pause"),
            ("resume", state("paused", "paused"), state(), "start"),
            ("stop", state(), state("idle", "idle", "off"), "stop"),
            ("return_to_dock", state(), state("returning", "returning_home"), "return_to_base"),
        ]
        for command, before, after, service in cases:
            with self.subTest(command=command):
                q = self.stopped()
                before.observed_at = 2000
                self.assertEqual(q.external_control(command, "vacuum.robot", before, 2000, "native"),
                                 ("vacuum", service))
                self.assertEqual((q.mode, q.phase, q.pending_command), ("external", "controlling", command))
                self.assertEqual((q.stages, q.targets, q.setup, q.control_entities), ([], [], {}, {}))
                self.assertEqual((q.current_index, q.completed, q.seen_job, q.next_pending), (0, 0, False, False))
                self.assertEqual((q.error, q.command_failure, q.run_id), ("", {}, "native"))
                after.observed_at = 2001
                self.assertIsNone(q.observe(after, 2001))
                self.assertEqual((q.phase, q.pending_command), ("idle", ""))
                idle = state("docked", "charging", "off", observed_at=2100)
                idle.record = queue_tests.record(begin=2000, end=2100)
                self.assertIsNone(q.observe(idle, 2100))
                self.assertEqual(q.completed, 0)

    def test_external_recovery_rejects_unknown_stale_or_uncertain_native_state_without_mutation(self):
        for case in ["deadline", "old_observation", "undated", "stale", "offline", "unknown", "busy_resume", "other_robot"]:
            with self.subTest(case=case):
                q = self.stopped()
                current = state(observed_at=2000)
                command, vacuum = "pause", "vacuum.robot"
                if case == "deadline": q.not_before = 2001
                elif case == "old_observation": current.observed_at = 999
                elif case == "undated": current.observed_at = 0
                elif case == "stale": current.observed_at = 1909
                elif case == "offline": current.connected = False
                elif case == "unknown": current.status, command = "unknown", "stop"
                elif case == "busy_resume": command = "resume"
                else: vacuum = "vacuum.other"
                before = q.dump()
                with self.assertRaises(ValueError):
                    q.external_control(command, vacuum, current, 2000, "new")
                self.assertEqual(q.dump(), before)

    def test_stopped_history_does_not_prevent_stop_for_a_known_robot_fault(self):
        q = self.stopped()
        current = state("error", "error", "on", observed_at=2000)
        current.error = "error"
        self.assertEqual(q.external_control("stop", "vacuum.robot", current, 2000, "stop"), ("vacuum", "stop"))
        self.assertEqual(q.stages, [])

    def test_clear_is_always_queue_only_and_dismisses_error_without_dropping_barrier(self):
        q = self.stopped()
        self.assertIsNone(q.command("cancel", Snapshot(), 500))
        self.assertEqual((q.phase, q.error, q.command_failure, q.decision), ("cancelled", "", {}, "sequence cleared"))
        self.assertEqual(q.not_before, 1000)
        self.assertIsNone(q.observe(state(), 501))


class FinishEngineTests(unittest.TestCase):
    def test_finish_clears_every_stage_and_docks_once_despite_water_fault(self):
        q = Queue(phase="running", mode="manual", targets=["0_1", "0_2"], stages=TWO_STAGES, next_pending=True)
        current = state(); current.dock_error = "water_empty"
        self.assertTrue(q.should_finish(current))
        self.assertEqual(q.finish("vacuum.robot", current, 100, "finish"), ("vacuum", "return_to_base"))
        self.assertEqual((q.stages, q.next_pending), ([], False))
        self.assertIsNone(q.finish("vacuum.robot", current, 101, "repeat"))
        q.observe(state("returning", "returning_home", observed_at=102), 102)
        q.observe(state("docked", "washing_the_mop", "off", 103), 103)
        self.assertEqual(q.phase, "controlling")
        q.observe(state("docked", "charging", "off", 104), 104)
        self.assertEqual(q.phase, "cancelled")
        self.assertIsNone(q.observe(state(), 200))

    def test_pending_start_waits_for_barrier_and_fresh_state(self):
        q = Queue(phase="starting", pending_command="start", command_at=100)
        self.assertIsNone(q.finish("vacuum.robot", state(observed_at=110), 110, "finish"))
        self.assertIsNone(q.observe(state(observed_at=999), 999))
        self.assertEqual(q.observe(state(observed_at=1005), 1005), ("vacuum", "return_to_base"))

    def test_mid_job_wash_is_not_completion_and_resumed_cleaning_is_sent_home(self):
        q = Queue()
        self.assertIsNone(q.finish("vacuum.robot", state("docked", "washing_the_mop", "on"), 100, "finish"))
        self.assertEqual(q.phase, "controlling")
        self.assertEqual(q.observe(state(observed_at=110), 110), ("vacuum", "return_to_base"))

    def test_recharge_break_stops_unfinished_native_job(self):
        q = Queue()
        self.assertEqual(q.finish("vacuum.robot", state("docked", "charging", "on"), 100, "finish"), ("vacuum", "stop"))
        q.observe(state("docked", "charging", "off", 101), 101)
        q.observe(state("docked", "charging", "off", 102), 102)
        self.assertEqual(q.phase, "cancelled")

    def test_returning_and_care_never_send_duplicate_dock_commands(self):
        q = Queue()
        for i, status in enumerate(["returning_home", "washing_the_mop", "emptying_the_bin"]):
            current = state("returning" if i == 0 else "docked", status, "off", 100+i)
            if i == 0: self.assertIsNone(q.finish("vacuum.robot", current, 100, "finish"))
            else: self.assertIsNone(q.observe(current, 100+i))
        self.assertEqual(q.setup, {})

    def test_restart_timeout_fault_and_unexpected_resume_never_retry(self):
        q = Queue(); q.finish("vacuum.robot", state(), 100, "finish")
        restored = Queue.restore(q.dump())
        self.assertEqual(restored.phase, "attention")
        self.assertIsNone(restored.observe(state(), 170))
        q.observe(state(observed_at=161), 161)
        self.assertEqual(q.phase, "attention")
        q = Queue(); q.finish("vacuum.robot", state(), 100, "finish")
        q.observe(state("returning", "returning_home", observed_at=101), 101)
        self.assertIsNone(q.observe(state(observed_at=102), 102))
        self.assertEqual(q.phase, "attention")
        q = Queue(); q.finish("vacuum.robot", Snapshot(), 100, "finish")
        self.assertEqual(q.phase, "attention")

    def test_idle_toggle_can_start_but_paused_and_preparing_must_finish(self):
        self.assertFalse(Queue().should_finish(state("docked", "charging", "off")))
        self.assertTrue(Queue().should_finish(state("paused", "paused")))
        self.assertTrue(Queue(phase="preparing").should_finish(state("docked", "charging", "off")))


class StandaloneManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.auth_count = 0
        self.allowed = {"vacuum.robot", "vacuum.other"}
        self.current = state(observed_at=time.time())
        async def get_user(user_id):
            self.auth_count += 1
            if self.auth_count == 2 and getattr(self, "revoke_before_dispatch", False):
                self.allowed.clear()
            if self.auth_count == 2 and getattr(self, "finish_before_dispatch", False):
                self.current = state("docked", "charging", "off", observed_at=time.time())
            return NS(is_active=True, is_admin=False, permissions=NS(check_entity=lambda entity, _policy: entity in self.allowed))
        async def call(domain, service, data, **kwargs):
            self.assertTrue(self.manager.lock.locked(), "Every physical command must remain under the shared lock")
            self.assertEqual(self.manager.store.saved[-1]["pending_command"], self.manager.queue.pending_command)
            self.assertIn(self.manager.store.saved[-1]["mode"], {"external", "finish"})
            self.calls.append((domain, service, data, kwargs["context"]))
            if getattr(self, "service_failure", False):
                raise RuntimeError("Sensitive payload")
        hass = NS(auth=NS(async_get_user=get_user), services=NS(async_call=call), async_create_task=asyncio.create_task)
        self.manager = manual_tests.manager_class()(hass)
        self.manager.current_snapshot = lambda vacuum: self.current
        class Unauthorized(Exception):
            def __init__(self, **kwargs):
                super().__init__("Unauthorized")
        self.manager.control.__func__.__globals__["Unauthorized"] = Unauthorized
        self.unauthorized = Unauthorized

    async def control(self, command, vacuum="vacuum.robot"):
        context = manual_tests.FakeContext("user")
        await self.manager.control(NS(context=context, data={"command": command, "vacuum": vacuum, "presets": [], "rooms": []}))
        return context

    async def test_toggle_cancels_under_lock_and_repeated_holds_send_home_once(self):
        await self.control("toggle")
        self.assertEqual(self.manager.queue.mode, "finish")
        self.current.observed_at = time.time()
        await self.manager.tick()
        self.assertEqual([c[:2] for c in self.calls], [("vacuum", "return_to_base")])
        await self.control("toggle")
        self.assertEqual(len(self.calls), 1)
        self.current = state("docked", "charging", "off", time.time())
        await self.manager.tick()
        await self.manager.tick()
        self.assertEqual(self.manager.queue.phase, "cancelled")

    async def test_toggle_permission_revocation_prevents_deferred_docking(self):
        await self.control("toggle")
        self.allowed.clear()
        self.current.observed_at = time.time()
        await self.manager.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.queue.phase, "attention")

    async def test_toggle_idle_needs_a_saved_plan_but_finish_does_not(self):
        self.current = state("docked", "charging", "off", time.time())
        with self.assertRaisesRegex(manual_tests.ServiceError, "No cleaning plan is saved"):
            await self.control("toggle")
        self.assertEqual(self.calls, [])
        self.current = state(observed_at=time.time())
        await self.control("toggle")
        self.assertEqual(self.manager.queue.mode, "finish")

    async def test_external_pause_uses_native_service_and_original_user_context(self):
        parent = await self.control("pause")
        self.assertEqual(self.calls[0][:3], ("vacuum", "pause", {"entity_id": "vacuum.robot"}))
        context = self.calls[0][3]
        self.assertEqual((context.user_id, context.parent_id), ("user", parent.id))
        self.assertIn(context.id, self.manager.contexts)
        self.current = state("paused", "paused", observed_at=time.time())
        await self.manager.tick()
        self.assertEqual((self.manager.queue.mode, self.manager.queue.phase), ("external", "idle"))

    async def test_explicit_vacuum_required_even_with_terminal_history(self):
        self.manager.queue = Queue(phase="completed", vacuum="vacuum.robot", mode="manual", targets=["0_1"], stages=ONE_STAGE)
        with self.assertRaisesRegex(manual_tests.ServiceError, "Specify a vacuum"):
            await self.control("pause", "")
        self.assertEqual(self.calls, [])

    async def test_concurrent_controls_only_dispatch_once(self):
        results = await asyncio.gather(self.control("pause"), self.control("return_to_dock"), return_exceptions=True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(isinstance(result, manual_tests.ServiceError) for result in results), 1)
        self.assertEqual(self.manager.queue.pending_command, "pause")

    async def test_different_vacuum_cannot_bypass_active_attention_or_pending(self):
        for phase, mode, pending in [("running", "manual", ""), ("attention", "manual", ""), ("controlling", "external", "resume"), ("attention", "external", "")]:
            self.manager.queue = Queue(phase=phase, mode=mode, vacuum="vacuum.robot", pending_command=pending, targets=["0_1"], stages=ONE_STAGE)
            before = self.manager.queue.dump()
            with self.assertRaisesRegex(manual_tests.ServiceError, "different vacuum"):
                await self.control("pause", "vacuum.other")
            self.assertEqual(self.manager.queue.dump(), before)
        self.assertEqual(self.calls, [])

    async def test_old_attention_does_not_block_native_job_pause_or_resume_with_obsolete_settings(self):
        for command, current in [("pause", state()), ("resume", state("paused", "paused"))]:
            with self.subTest(command=command):
                await self.asyncSetUp()
                now = time.time()
                current.observed_at = now
                self.current = current
                self.manager.queue = Queue(phase="attention", mode="manual", vacuum="vacuum.robot",
                                           targets=["0_1"], stages=ONE_STAGE,
                                           control_entities={"mode": "select.removed_setting"},
                                           error="Old failure", command_at=now - 1000, not_before=now - 100)
                await self.control(command)
                self.assertEqual(self.calls[0][:2], ("vacuum", "pause" if command == "pause" else "start"))
                self.assertEqual((self.manager.queue.mode, self.manager.queue.pending_command), ("external", command))
                self.assertEqual((self.manager.queue.stages, self.manager.queue.control_entities), ([], {}))

    async def test_attention_native_control_cannot_bypass_an_uncertain_start(self):
        now = time.time()
        self.manager.queue = Queue(phase="attention", mode="manual", vacuum="vacuum.robot", targets=["0_1"],
                                   stages=ONE_STAGE, command_at=now - 10, not_before=now + 890, barrier_window=900)
        before = self.manager.queue.dump()
        with self.assertRaisesRegex(manual_tests.ServiceError, "uncertain"):
            await self.control("pause")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.queue.dump(), before)

    async def test_terminal_plan_permissions_do_not_leak_into_new_controls(self):
        self.manager.queue = Queue(phase="completed", vacuum="vacuum.old", mode="manual", targets=["0_1"], stages=ONE_STAGE, control_entities={"mode": "select.old"})
        await self.control("pause")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.manager.queue.stages, [])          # the old plan is discarded

    async def test_unauthorized_and_revoked_permissions_never_dispatch(self):
        self.allowed.clear()
        with self.assertRaises(self.unauthorized):
            await self.control("pause")
        self.assertEqual(self.manager.queue.phase, "idle")
        await self.asyncSetUp()
        self.revoke_before_dispatch = True
        await self.control("pause")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.queue.phase, "attention")

    async def test_native_job_ending_during_authorization_prevents_resume(self):
        self.current = state("paused", "paused", observed_at=time.time())
        self.finish_before_dispatch = True
        await self.control("resume")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.queue.phase, "attention")

    async def test_service_failure_retains_uncertainty_and_hides_private_error(self):
        self.service_failure = True
        await self.control("pause")
        self.assertEqual(self.manager.queue.phase, "attention")
        self.assertGreater(self.manager.queue.not_before, time.time())
        self.assertNotIn("Sensitive", self.manager.queue.error)
        with self.assertRaises(manual_tests.ServiceError):
            await self.control("pause")
        self.assertEqual(len(self.calls), 1)

    async def test_external_competing_control_interrupts_pending_without_retry(self):
        await self.control("pause")
        event = NS(context=manual_tests.FakeContext("other"), data={"domain": "vacuum", "service": "start", "service_data": {"entity_id": "vacuum.robot"}})
        self.manager.external_command(event)
        await asyncio.sleep(0)
        self.assertEqual(self.manager.queue.phase, "attention")
        self.assertGreater(self.manager.queue.not_before, time.time())
        await self.manager.tick()
        self.assertEqual(len(self.calls), 1)

    async def test_owned_queue_pause_still_preserves_sequence_and_start_time(self):
        self.allowed.update({"button.kitchen", "button.office"})
        self.manager.queue = Queue(phase="running", vacuum="vacuum.robot", mode="manual", targets=["0_1", "0_2"], stages=TWO_STAGES, started_at=100, owner_user_id="user")
        # This assertion applies only to standalone calls; record owned dispatch separately.
        async def call(domain, service, data, **kwargs):
            self.calls.append((domain, service, data, kwargs["context"]))
        self.manager.hass.services.async_call = call
        await self.control("pause")
        self.assertEqual((self.manager.queue.mode, self.manager.queue.started_at), ("manual", 100))
        self.assertEqual(self.manager.queue.stages, TWO_STAGES)
        self.assertEqual(self.calls[0][1], "pause")


    async def test_owned_resume_rechecks_unfinished_job_after_authorization(self):
        self.allowed.add("button.kitchen")
        self.manager.queue = Queue(phase="paused", vacuum="vacuum.robot", mode="manual", targets=["0_1"], stages=ONE_STAGE, owner_user_id="user")
        self.current = state("paused", "paused", observed_at=time.time())
        self.finish_before_dispatch = True
        await self.control("resume")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.queue.phase, "attention")
        self.assertEqual(self.manager.queue.completed, 0)

    async def test_owned_pause_rechecks_native_state_after_authorization(self):
        self.allowed.add("button.kitchen")
        self.manager.queue = Queue(phase="running", vacuum="vacuum.robot", mode="manual", targets=["0_1"], stages=ONE_STAGE, owner_user_id="user")
        self.finish_before_dispatch = True
        await self.control("pause")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.queue.phase, "attention")

    async def test_repeated_owned_dock_does_not_send_second_command_and_persists_barrier(self):
        self.allowed.add("button.kitchen")
        self.manager.queue = Queue(phase="running", vacuum="vacuum.robot", mode="manual", targets=["0_1"], stages=ONE_STAGE, owner_user_id="user")
        async def call(domain, service, data, **kwargs):
            self.calls.append((domain, service, data, kwargs["context"]))
        self.manager.hass.services.async_call = call
        await self.control("return_to_dock")
        await self.control("return_to_dock")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "return_to_base")
        self.assertEqual(self.manager.queue.phase, "attention")
        self.assertGreater(self.manager.queue.not_before, time.time())
        self.assertEqual(self.manager.store.saved[-1], self.manager.queue.dump())


if __name__ == "__main__":
    unittest.main(verbosity=2)
