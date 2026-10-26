"""Offline interrupted-room recovery and retained-job recharge traces."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import unittest

import backend_manual_test as manual_tests
from backend_queue_test import Queue, Snapshot, load, record

recovery = load("recovery")
history = load("history")


def stage(target, mode="mop"):
    return {"target": target, "mode": mode, "room_index": 0 if target == "0_1" else 1,
            "pass_index": 0, "repeat_index": 0, "map_id": 0, "segments": [target[-1]],
            "settings": {"mode": mode}}


def running():
    q = Queue(phase="running", mode="manual", vacuum="vacuum.robot", run_id="same-run",
              targets=["0_1", "0_2"], stages=[stage("0_1"), stage("0_2")],
              setup={"rooms": [{"id": "0_1"}, {"id": "0_2"}]}, completed=1, current_index=1,
              started_at=100, command_at=100, baseline_end=80, seen_job=True,
              native_job_type=3, native_clean_time=30, native_job_observed_at=140)
    history.ensure(q)
    return q


def native(*, vacuum="docked", status="charging", job="on", now=150, water="ok"):
    return Snapshot(vacuum, status, job, "none", water, True, record(10, 80), now,
                    settings={"mode": "mop"}, job_type=3 if job == "on" else 0,
                    in_returning=0, battery=29, clean_time=30)


class ContinuationEngineTests(unittest.TestCase):
    def fault(self, q=None, **kw):
        q = q or running()
        current = native(water="water_empty", **kw)
        q.observe(current, current.observed_at)
        self.assertEqual(q.phase, "attention")
        return q

    def test_water_fault_explicit_native_resume_preserves_same_plan_and_completion(self):
        q = self.fault()
        before_plan = deepcopy((q.stages, q.setup, q.last_run["stages"][0]))
        q.observe(native(now=151), 151)
        self.assertEqual(q.phase, "attention", "No automatic continuation after refill")
        self.assertEqual(recovery.continuation_action(q, native(now=152), 152), "resume_job")
        self.assertEqual(recovery.continue_sequence(q, native(now=152), 152), ("vacuum", "start"))
        self.assertEqual((q.run_id, q.completed, q.current_index, q.started_at, q.baseline_end),
                         ("same-run", 1, 1, 100, 80))
        self.assertEqual((q.stages, q.setup, q.last_run["stages"][0]), before_plan)
        q.observe(native(vacuum="cleaning", status="segment_mopping", now=153), 153)
        self.assertEqual((q.phase, q.pending_command), ("running", ""))
        self.assertEqual(q.last_run["stages"][1]["reason"], "")

    def test_recharge_is_not_floor_cleaning_or_completion_and_resume_keeps_original_start(self):
        q, current = running(), native()
        q.observe(current, 150)
        self.assertEqual((q.completed, q.current_index), (1, 1))
        self.assertEqual(recovery.state(q, current, 150)["execution_status"], "recharging")
        self.assertTrue(recovery.state(q, current, 150)["resume_now_ready"])
        self.assertEqual(q.command("resume_now", current, 150), ("vacuum", "start"))
        self.assertEqual((q.pending_command, q.started_at, q.baseline_end), ("resume_now", 100, 80))
        with self.assertRaises(ValueError):
            q.command("resume_now", current, 150)

    def test_native_resume_never_sends_full_clean_or_return_home_for_wrong_flags(self):
        for key, value in [("job_type", 1), ("job_type", 4), ("job_type", None),
                           ("in_returning", 1), ("in_returning", None), ("job", "off"),
                           ("observed_at", 0), ("observed_at", 50), ("clean_time", 29),
                           ("record", None), ("record", record(100, 145)),
                           ("settings", {"mode": "vacuum"})]:
            with self.subTest(key=key, value=value):
                q, current = running(), native()
                setattr(current, key, value)
                before = q.dump()
                with self.assertRaises(ValueError):
                    q.command("resume_now", current, 150)
                self.assertEqual(q.dump(), before)

    def test_already_running_owned_job_can_rejoin_without_motion_or_restarting_pass(self):
        q = self.fault()
        current = native(vacuum="cleaning", status="segment_mopping", now=160)
        recovery.observe_job(q, current, 160)
        self.assertEqual(recovery.continuation_action(q, current, 160), "resume_observation")
        self.assertIsNone(recovery.continue_sequence(q, current, 160))
        self.assertEqual((q.phase, q.run_id, q.started_at, q.completed, q.current_index),
                         ("running", "same-run", 100, 1, 1))

    def test_return_to_mop_wash_and_unavailable_settings_preserve_identity_but_never_send_resume(self):
        q = self.fault()
        current = native(vacuum="returning", status="going_to_wash_the_mop", now=155)
        current.in_returning, current.settings = 1, {}
        recovery.observe_job(q, current, 155)
        self.assertEqual(q.continuation_context, "device_fault")
        with self.assertRaises(ValueError):
            recovery.continue_sequence(q, current, 155)
        current.settings = {"mode": "mop"}
        self.assertEqual(recovery.continuation_action(q, current, 155), "resume_observation")
        self.assertIsNone(recovery.continue_sequence(q, current, 155))
        self.assertEqual(q.phase, "running")

    def test_end_of_native_job_retries_only_unconfirmed_pass(self):
        q = self.fault()
        ended = native(job="off", now=155)
        ended.record = record(100, 154, complete=0, error=38)
        recovery.observe_job(q, ended, 155)
        self.assertEqual(recovery.continuation_action(q, ended, 155), "retry_stage")
        prior = deepcopy(q.last_run["stages"][0])
        self.assertEqual(recovery.continue_sequence(q, ended, 155), ("configure", "1"))
        self.assertEqual((q.run_id, q.current_index, q.completed), ("same-run", 1, 1))
        self.assertEqual(q.last_run["stages"][0], prior)
        self.assertIn("retried", q.last_run["interruptions"][-1]["reason"])

    def test_completed_native_record_after_fault_reconciles_on_explicit_continue_only(self):
        q = self.fault()
        ended = native(job="off", now=160)
        ended.record = record(100, 159)
        recovery.observe_job(q, ended, 160)
        self.assertEqual(q.completed, 1)
        self.assertEqual(recovery.continuation_action(q, ended, 160), "continue_next")
        self.assertIsNone(recovery.continue_sequence(q, ended, 160))
        self.assertEqual((q.phase, q.completed, q.current_index), ("finishing", 2, 2))
        self.assertEqual(q.last_run["stages"][1]["finished_at"], 159)

    def test_matching_completion_record_can_arrive_before_job_off_without_losing_evidence(self):
        q = self.fault()
        current = native(now=160)
        current.record = record(100, 159)
        original_observation = q.native_job_observed_at
        recovery.observe_job(q, current, 160)
        self.assertEqual(q.continuation_context, "completion_pending_idle")
        self.assertEqual(q.native_job_observed_at, original_observation)
        self.assertFalse(recovery.state(q, current, 160)["continuation_ready"])
        self.assertEqual(q.completed, 1)
        current.job, current.observed_at = "off", 161
        recovery.observe_job(q, current, 161)
        self.assertEqual(recovery.continuation_action(q, current, 161), "continue_next")
        self.assertIsNone(recovery.continue_sequence(q, current, 161))
        self.assertEqual((q.completed, q.current_index, q.phase), (2, 2, "finishing"))

    def test_unrelated_later_record_and_failed_record_never_receive_credit(self):
        for completed_record in [record(151, 159), record(100, 159, error=1), record(100, 159, complete=0)]:
            q = self.fault()
            ended = native(job="off", now=160)
            ended.record = completed_record
            recovery.observe_job(q, ended, 160)
            self.assertEqual(recovery.continuation_action(q, ended, 160), "retry_stage")
            self.assertEqual(q.completed, 1)

    def test_fault_between_passes_continues_next_without_repeating_completed_work(self):
        q = running()
        q.next_pending = True
        q = self.fault(q, job="off")
        self.assertTrue(q.interrupted_next_pending)
        self.assertEqual(recovery.continuation_action(q, native(job="off", now=160), 160), "continue_next")
        self.assertIsNone(recovery.continue_sequence(q, native(job="off", now=160), 160))
        self.assertEqual((q.phase, q.next_pending, q.current_index, q.completed), ("running", True, 1, 1))

    def test_fault_after_all_passes_only_restores_dock_observation(self):
        q = running()
        q.phase, q.completed, q.current_index, q.dock_finish_at = "finishing", 2, 2, 145
        q = self.fault(q, job="off")
        self.assertEqual(recovery.continuation_action(q, native(job="off", now=160), 160), "finish_docking")
        self.assertIsNone(recovery.continue_sequence(q, native(job="off", now=160), 160))
        self.assertEqual((q.phase, q.current_index), ("finishing", 2))

    def test_latched_water_acknowledgement_only_applies_to_this_existing_pass(self):
        q = self.fault()
        current = native(now=160, water="water_empty")
        with self.assertRaises(ValueError):
            recovery.continue_sequence(q, current, 160)
        self.assertTrue(recovery.state(q, current, 160)["continuation_requires_water_ack"])
        self.assertEqual(recovery.continue_sequence(q, current, 160, water_refilled=True), ("vacuum", "start"))
        q.observe(native(vacuum="cleaning", status="segment_mopping", now=161, water="water_empty"), 161)
        self.assertEqual(q.phase, "running")
        self.assertEqual(q.water_refilled_index, 1)
        q.observe(native(vacuum="cleaning", status="segment_mopping", now=162), 162)
        self.assertEqual(q.water_refilled_index, -1)
        q.observe(native(now=163, water="water_empty"), 163)
        self.assertEqual(q.phase, "attention", "A new water fault is never waived")

    def test_water_ack_never_overrides_other_faults_or_robot_errors(self):
        for key, value in [("dock_error", "unknown"), ("error", "error"), ("connected", False)]:
            q, current = self.fault(), native(now=160, water="water_empty")
            setattr(current, key, value)
            before = q.dump()
            with self.assertRaises(ValueError):
                recovery.continue_sequence(q, current, 160, water_refilled=True)
            self.assertEqual(q.dump(), before)

    def test_resume_barrier_cancel_restart_and_telemetry_loss_never_adopt_active_job(self):
        for cause in ["barrier", "restart", "lost", "external"]:
            q = self.fault()
            if cause == "barrier": q.not_before = 170
            elif cause == "restart": q = Queue.restore(q.dump())
            elif cause == "lost": recovery.observe_job(q, Snapshot(), 160)
            else: q.attention("External control changed the job")
            with self.assertRaises(ValueError):
                recovery.continue_sequence(q, native(now=160), 160)
            self.assertEqual(q.completed, 1)

    def test_counter_regression_only_revokes_resume_eligibility_not_normal_execution(self):
        q, current = running(), native(now=160)
        current.clean_time = 10
        q.observe(current, 160)
        self.assertEqual(q.phase, "running")
        self.assertTrue(q.native_job_discontinuous)
        self.assertFalse(recovery.state(q, current, 160)["resume_now_ready"])

    def test_older_observation_does_not_revoke_resume_evidence(self):
        q, current = running(), native(now=139)
        current.clean_time = 10
        recovery.observe_job(q, current, 150)
        self.assertFalse(q.native_job_discontinuous)
        self.assertEqual(q.native_clean_time, 30)

    def test_ended_job_followed_by_another_job_cannot_reacquire_continuity(self):
        q = self.fault()
        recovery.observe_job(q, native(job="off", now=155), 155)
        self.assertEqual(q.continuation_context, "awaiting_completion")
        replacement = native(now=160)
        replacement.clean_time = 40
        with self.assertRaises(ValueError):
            recovery.continuation_action(q, replacement, 160)
        self.assertFalse(recovery.state(q, replacement, 160)["continuation_ready"])
        recovery.observe_job(q, replacement, 160)
        self.assertEqual(q.continuation_context, "")
        with self.assertRaises(ValueError):
            recovery.continue_sequence(q, replacement, 160)
        replacement.job = "off"
        replacement.record = record(100, 165)
        recovery.observe_job(q, replacement, 166)
        self.assertFalse(recovery.completed_after_fault(q, replacement))

    def test_no_completion_baseline_or_native_gap_never_enables_charge_resume(self):
        q, current = running(), native()
        q.baseline_end, current.record = 0, None
        self.assertFalse(recovery.state(q, current, 150)["resume_now_ready"])
        q, current = running(), native(now=140)
        recovery.observe_job(q, current, 240)
        self.assertTrue(q.native_job_discontinuous)
        self.assertFalse(recovery.state(q, native(now=240), 240)["resume_now_ready"])

    def test_fresh_current_snapshot_after_long_native_gap_does_not_reacquire_resume(self):
        q = running()
        current = native(now=450)
        current.clean_time = 60
        self.assertFalse(recovery.state(q, current, 450)["resume_now_ready"])
        recovery.observe_job(q, current, 450)
        self.assertTrue(q.native_job_discontinuous)
        self.assertFalse(recovery.state(q, native(now=465), 465)["resume_now_ready"])
        q = self.fault()
        self.assertFalse(recovery.state(q, current, 450)["continuation_ready"])
        recovery.observe_job(q, current, 450)
        self.assertEqual(q.continuation_context, "")

    def test_inconsistent_restored_progress_is_rejected_without_mutation(self):
        q = self.fault()
        q.current_index = 0
        before = q.dump()
        with self.assertRaises(ValueError):
            recovery.continue_sequence(q, native(job="off", now=160), 160)
        self.assertEqual(q.dump(), before)


class ContinuationManagerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = manual_tests.ManagerTraceTests.asyncSetUp

    def prepare(self, phase="running"):
        caps, controls, targets, map_id = self.manager.manual_capabilities("vacuum.robot")
        room_targets = manual_tests.manual.robot_targets(self.coordinator, map_id)
        rooms, stages = manual_tests.manual.build_room_plan(
            [{"id": "0_1", "mode": "vacuum", "suction": "max"},
             {"id": "0_3", "mode": "vacuum", "suction": "max"}], {}, caps, room_targets, map_id)
        q = self.manager.queue = running()
        q.phase, q.address, q.stages, q.control_entities = phase, "room", stages, controls
        q.targets, q.setup = [r["id"] for r in rooms], {"rooms": rooms}
        q.command_at = q.started_at = self.clock - 100
        q.native_job_observed_at = self.clock - 5
        q.last_run = None
        history.ensure(q)
        status = self.coordinator.properties_api.status
        status.in_cleaning, status.in_returning, status.clean_time, status.battery = 3, 0, 30, 29
        status.current_cleaning_mode_name = "vacuum"
        self.states["select.renamed_mode"].state = "vacuum"
        self.states["vacuum.robot"].attributes["fan_speed"] = "max"
        self.coordinator.data.clean_summary.last_clean_record = NS(**record(10, 80))
        if phase == "attention":
            q.continuation_context = "device_fault"
        return q, status

    def call(self, command, **extra):
        return NS(context=manual_tests.FakeContext("user"), data={"command": command,
                  "vacuum": "vacuum.robot", "run_id": "same-run", "current_index": 1, **extra})

    async def test_tick_reconciles_late_final_success_and_persists_without_motion(self):
        q,status=self.prepare()
        q.attention("The robot or dock has a fault, or telemetry is unavailable. The sequence is retained for review.",recoverable_fault=True)
        q.continuation_context=""  # Dock settings reset before clean-summary refresh.
        begin=q.started_at
        status.in_cleaning=0;status.state_name="charging"
        self.states["vacuum.robot"].state="docked"
        self.coordinator.data.clean_summary.last_clean_record=NS(**record(begin,self.clock-1,finish_reason=56))
        await self.manager.tick()
        self.assertEqual((q.phase,q.completed),("finishing",2))
        self.assertEqual(self.manager.store.saved[-1]["completed"],2)
        self.assertEqual(self.calls,[])
        self.clock+=1
        self.coordinator._last_update_success_time=datetime.fromtimestamp(self.clock,timezone.utc)
        await self.manager.tick()
        self.clock+=16
        self.coordinator._last_update_success_time=datetime.fromtimestamp(self.clock,timezone.utc)
        await self.manager.tick()
        self.assertEqual(q.phase,"attention")
        self.assertIn("native control is unavailable",q.error)
        self.assertEqual(q.completed,2)
        self.assertEqual(self.calls,[])

    async def test_charging_resume_dispatches_explicit_native_resume_once_and_persists_original_progress(self):
        q, _ = self.prepare()
        await asyncio.gather(self.manager.control(self.call("resume_now")),
                             self.manager.control(self.call("resume_now")), return_exceptions=True)
        self.assertEqual(self.calls, [("vacuum", "send_command", {"entity_id": "vacuum.robot", "command": "resume_segment_clean"})])
        self.assertEqual((q.completed, q.current_index, q.run_id), (1, 1, "same-run"))
        self.assertEqual(q.owner_user_id, "user")
        self.assertTrue(self.manager.store.saved)

    async def test_native_state_race_cannot_change_resume_into_whole_home_start_or_docking(self):
        for job_type, command in [(3, "resume_segment_clean"), (2, "resume_zoned_clean")]:
            with self.subTest(job_type=job_type):
                q, status = self.prepare()
                self.calls.clear()
                q.native_job_type = status.in_cleaning = job_type
                original = self.hass.services.async_call
                async def race(domain, service, data, **kwargs):
                    # Happens after our final snapshot, before the HA entity handles
                    # this call. Generic vacuum.start would choose APP_CHARGE now.
                    status.in_cleaning, status.in_returning = 0, 1
                    await original(domain, service, data, **kwargs)
                self.hass.services.async_call = race
                await self.manager.control(self.call("resume_now"))
                self.assertEqual(self.calls, [("vacuum", "send_command", {"entity_id": "vacuum.robot", "command": command})])
                self.hass.services.async_call = original
                self.assertEqual((q.completed, q.current_index, q.run_id), (1, 1, "same-run"))

    async def test_stale_run_or_pass_rejected_before_any_effect(self):
        for extra in [{"run_id": "older"}, {"current_index": 0}]:
            q, _ = self.prepare()
            before = q.dump()
            with self.assertRaises(manual_tests.ServiceError):
                await self.manager.control(self.call("resume_now", **extra))
            self.assertEqual(q.dump(), before)
        self.assertEqual(self.calls, [])

    async def test_resume_rechecks_native_returning_flag_after_authorization(self):
        q, status = self.prepare()
        original = self.hass.auth.async_get_user
        count = 0
        async def race(user):
            nonlocal count
            count += 1
            if count >= 3:
                status.in_returning = 1
            return await original(user)
        self.hass.auth.async_get_user = race
        await self.manager.control(self.call("resume_now"))
        self.assertEqual(self.calls, [])
        self.assertEqual(q.phase, "attention")
        self.assertEqual(q.not_before, 0, "An unsent resume cannot lock recovery for 15 minutes")

    async def test_uncertain_resume_observes_matching_native_job_without_retry(self):
        q, status = self.prepare()
        self.fail_service = "send_command"
        await self.manager.control(self.call("resume_now"))
        self.assertEqual((q.phase, q.pending_command, q.start_uncertain), ("starting", "resume_now", True))
        status.state_name = "segment_cleaning"
        self.states["vacuum.robot"].state = "cleaning"
        self.clock += 1
        self.coordinator._last_update_success_time = datetime.fromtimestamp(self.clock, timezone.utc)
        await self.manager.tick()
        self.assertEqual((q.phase, q.pending_command, q.start_uncertain), ("running", "", False))
        self.assertEqual((q.completed, q.current_index, q.run_id), (1, 1, "same-run"))
        self.assertEqual(self.calls, [("vacuum", "send_command", {"entity_id": "vacuum.robot", "command": "resume_segment_clean"})])

    async def test_uncertain_resume_does_not_accept_unrelated_job_ack(self):
        q, status = self.prepare()
        self.fail_service = "send_command"
        await self.manager.control(self.call("resume_now"))
        status.state_name, status.in_cleaning = "cleaning", 1
        self.states["vacuum.robot"].state = "cleaning"
        self.clock += 1
        self.coordinator._last_update_success_time = datetime.fromtimestamp(self.clock, timezone.utc)
        await self.manager.tick()
        self.assertEqual((q.phase, q.pending_command), ("starting", "resume_now"))
        self.clock += 901
        self.coordinator._last_update_success_time = datetime.fromtimestamp(self.clock, timezone.utc)
        await self.manager.tick()
        self.assertEqual(q.phase, "attention")
        self.assertGreater(q.not_before, 0)
        self.assertEqual(self.calls, [("vacuum", "send_command", {"entity_id": "vacuum.robot", "command": "resume_segment_clean"})])

    async def test_changed_map_or_controls_cannot_continue(self):
        q, _ = self.prepare("attention")
        self.coordinator.properties_api.maps.current_map = 1
        with self.assertRaises(manual_tests.ServiceError):
            await self.manager.control(self.call("continue_sequence"))
        self.assertEqual(q.phase, "attention")
        self.assertEqual(self.calls, [])

    async def test_water_ack_resume_uses_native_command_and_observes_same_latched_fault(self):
        q, status = self.prepare("attention")
        q.stages[1]["mode"] = "mop"
        q.stages[1]["settings"] = {"mode": "mop", "water": "high", "route": "standard"}
        status.current_cleaning_mode_name, status.water_mode_name = "mop", "high"
        status.dock_error_status = 38
        self.states["select.renamed_mode"].state = "mop"
        self.states["select.renamed_water"].state = "high"
        self.assertTrue(self.manager.continuation_state(self.manager.current_snapshot("vacuum.robot"))["continuation_requires_water_ack"])
        await self.manager.control(self.call("continue_sequence", water_refilled=True))
        self.assertEqual(self.calls, [("vacuum", "send_command", {"entity_id": "vacuum.robot", "command": "resume_segment_clean"})])
        self.assertEqual(q.water_refilled_index, 1)
        status.state_name = "segment_mopping"
        self.states["vacuum.robot"].state = "cleaning"
        self.clock += 1
        self.coordinator._last_update_success_time = datetime.fromtimestamp(self.clock, timezone.utc)
        await self.manager.tick()
        self.assertEqual(q.phase, "running")
        self.assertTrue(self.manager.robot_state["water_refill_ack_active"])
        self.assertEqual((q.completed, q.current_index), (1, 1))

    async def test_retry_ended_pass_only_starts_that_remaining_room(self):
        q, status = self.prepare("attention")
        status.in_cleaning = 0
        q.continuation_context = ""
        completed_evidence = deepcopy(q.last_run["stages"][0])
        await self.manager.control(self.call("continue_sequence"))
        self.assertEqual(q.phase, "preparing")
        self.assertEqual(self.segment_calls, [])
        await self.manager.tick()
        self.clock += 16
        self.coordinator._last_update_success_time = datetime.fromtimestamp(self.clock, timezone.utc)
        await self.manager.tick()
        self.assertEqual(self.segment_calls, [["0_3"]])
        self.assertEqual(q.last_run["stages"][0], completed_evidence)
        self.assertEqual((q.completed, q.current_index, q.run_id), (1, 1, "same-run"))

    async def test_rejoin_observation_rechecks_job_after_permission_await(self):
        q, status = self.prepare("attention")
        status.state_name = "segment_cleaning"
        self.states["vacuum.robot"].state = "cleaning"
        original = self.hass.auth.async_get_user
        count = 0
        async def race(user):
            nonlocal count
            count += 1
            if count >= 2:
                status.in_cleaning = 1
            return await original(user)
        self.hass.auth.async_get_user = race
        with self.assertRaises(manual_tests.ServiceError):
            await self.manager.control(self.call("continue_sequence"))
        self.assertEqual(q.phase, "attention")
        self.assertEqual(self.calls, [])

    async def test_storage_failure_never_dispatches_resume(self):
        q, _ = self.prepare()
        async def fail(_data): raise OSError("offline storage")
        self.manager.store.async_save = fail
        await self.manager.control(self.call("resume_now"))
        self.assertEqual(q.phase, "attention")
        self.assertEqual(self.calls, [])

    async def test_suspended_ticks_invalidate_lost_job_continuity_without_motion(self):
        q, status = self.prepare("attention")
        status.clean_time = 5
        await self.manager.tick()
        self.assertEqual(q.continuation_context, "")
        self.assertFalse(self.manager.robot_state["continuation_ready"])
        self.assertEqual(self.calls, [])

    async def test_external_command_invalidates_suspended_continuity(self):
        q, _ = self.prepare("attention")
        self.manager.external_command(NS(context=manual_tests.FakeContext("other"),
            data={"domain": "vacuum", "service": "start", "service_data": {"entity_id": "vacuum.robot"}}))
        await asyncio.sleep(0)
        self.assertEqual(q.continuation_context, "")
        self.assertEqual(q.phase, "attention")
        self.assertEqual(self.calls, [])


class FaultCompletionTests(unittest.TestCase):
    def interrupted(self):
        q = running()
        q.observe(native(water="water_empty"), 150)
        rolled = native(now=151)
        rolled.settings = {"mode": "vac_and_mop"}
        recovery.observe_job(q, rolled, 151)
        self.assertEqual(q.continuation_context, "")
        return q

    def completed(self, begin=100):
        current = native(job="off", now=160)
        current.record = record(begin, 159, finish_reason=56)
        current.settings = {"mode": "vac_and_mop"}
        return current

    def test_late_success_after_dock_settings_reset_credits_final_pass_without_motion(self):
        q = self.interrupted()
        completed_before = deepcopy(q.last_run["stages"][0])
        self.assertTrue(recovery.reconcile_fault_completion(q, self.completed(), 160))
        self.assertEqual((q.phase, q.completed, q.current_index, q.pending_command), ("finishing", 2, 2, ""))
        self.assertEqual(q.last_run["stages"][0], completed_before)
        self.assertEqual(q.last_run["stages"][1]["finished_at"], 159)
        self.assertFalse(recovery.reconcile_fault_completion(q, self.completed(), 161))

    def test_remaining_room_requires_explicit_continue_and_is_not_double_credited(self):
        q = self.interrupted()
        q.stages.append(stage("0_3")); q.targets.append("0_3")
        q.last_run = None; history.ensure(q)
        self.assertTrue(recovery.reconcile_fault_completion(q, self.completed(), 160))
        self.assertEqual((q.phase, q.completed, q.interrupted_next_pending), ("attention", 2, True))
        self.assertEqual(recovery.continuation_action(q, self.completed(), 160), "continue_next")
        self.assertIsNone(recovery.continue_sequence(q, self.completed(), 160))
        self.assertEqual((q.completed, q.current_index), (2, 2))

    def test_unrelated_failed_stale_or_active_record_never_credits(self):
        for key, value in [("record",record(151,159)), ("record",record(100,159,error=1)),
                           ("record",record(100,159,complete=0)), ("record",record(100,159,finish_reason=99)),
                           ("job","on"), ("connected",False), ("observed_at",1)]:
            with self.subTest(key=key,value=value):
                q, current = self.interrupted(), self.completed()
                setattr(current,key,value)
                self.assertFalse(recovery.reconcile_fault_completion(q,current,160))
                self.assertEqual(q.completed,1)

    def test_legacy_fault_migration_and_new_checkpoint_survive_restart(self):
        for legacy in [False, True]:
            q = self.interrupted(); saved=q.dump()
            if legacy: saved.pop("fault_record_before")
            restored=Queue.restore(saved)
            self.assertTrue(recovery.reconcile_fault_completion(restored,self.completed(),160))
            self.assertEqual(restored.completed,2)

    def test_non_fault_attention_cannot_reuse_completion_window(self):
        q=self.interrupted()
        q.attention("Another control interrupted this sequence.")
        self.assertFalse(recovery.reconcile_fault_completion(q,self.completed(),160))
        self.assertEqual(q.fault_record_before,0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
