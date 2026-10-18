"""Actual Manager lifecycle/service traces using native-shaped offline fixtures."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
import unittest

import backend_manual_test as manual
from backend_queue_test import Queue, Snapshot, load, record
from backend_recovery_test import running, native

restart = load("restart")
recovery = load("recovery")


class RestartEngineTests(unittest.TestCase):
    def test_restart_freezes_owned_start_window_across_two_restarts(self):
        q = Queue.restore(running().dump())
        self.assertEqual(q.phase, "recovering")
        self.assertEqual(q.restart_record_before, 140)
        q.observe(native(vacuum="cleaning", status="segment_mopping", now=500), 500)
        self.assertEqual(q.phase, "running")
        q.observe(native(vacuum="cleaning", status="segment_mopping", now=510), 510)
        q = Queue.restore(q.dump())
        self.assertEqual(q.restart_record_before, 140)
        ended = native(job="off", now=1000)
        ended.record = record(550, 990)
        q.observe(ended, 1000)
        ended.observed_at = 1181
        q.observe(ended, 1181)
        self.assertEqual((q.completed, q.current_index, q.phase), (1, 1, "attention"))
        self.assertEqual(recovery.continuation_action(q, ended, 1181), "retry_stage")

    def test_matching_completion_during_outage_is_credited_once(self):
        q = Queue.restore(running().dump())
        ended = native(job="off", now=1000)
        ended.record = record(101, 800)
        q.observe(ended, 1000)
        self.assertEqual((q.phase, q.completed), ("finishing", 2))
        self.assertEqual(q.last_run["stages"][1]["finished_at"], 800)
        q.observe(ended, 1001)
        self.assertEqual(q.completed, 2)

    def test_record_visible_beside_later_job_is_durable_until_job_ends(self):
        q = Queue.restore(running().dump())
        active = native(now=1000)
        active.record = record(100, 800)
        q.observe(active, 1000)
        self.assertEqual(q.phase, "recovering")
        q = Queue.restore(q.dump())
        active.job, active.observed_at, active.record = "off", 1200, record(900, 1190)
        q.observe(active, 1200)
        self.assertEqual(q.completed, 2)
        self.assertEqual(q.last_run["stages"][1]["finished_at"], 800)

    def test_completed_floor_pass_is_kept_when_dock_water_fault_arrives(self):
        q = Queue.restore(running().dump())
        ended = native(job="off", water="water_empty", now=1000)
        ended.record = record(101, 800)
        q.observe(ended, 1000)
        self.assertEqual((q.completed, q.phase), (2, "attention"))
        self.assertEqual(q.last_run["stages"][1]["state"], "completed")

    def test_matching_record_after_rejoin_survives_later_record_replacement(self):
        q = Queue.restore(running().dump())
        active = native(vacuum="cleaning", status="segment_mopping", now=500)
        q.observe(active, 500)
        active.observed_at, active.record = 510, record(100, 505)
        q.observe(active, 510)
        q = Queue.restore(q.dump())
        active.job, active.observed_at, active.record = "off", 1200, record(900, 1190)
        q.observe(active, 1200)
        self.assertEqual(q.completed, 2)
        self.assertEqual(q.last_run["stages"][1]["finished_at"], 505)

    def test_user_pause_restores_without_motion_and_generic_resume_is_rejected(self):
        q = running()
        q.phase = "paused"
        q = Queue.restore(q.dump())
        paused = native(vacuum="paused", status="paused", now=1000)
        q.observe(paused, 1000)
        self.assertEqual(q.phase, "paused")
        with self.assertRaisesRegex(ValueError, "confirm"):
            q.command("resume", paused, 1000)
        self.assertTrue(restart.resume_token(q, paused, 1000))

    def test_cancel_stop_and_uncertain_start_never_become_auto_recoverable(self):
        for phase, command in [("cancelled", ""), ("cancelled", "stop"), ("starting", "start")]:
            q = running()
            q.phase, q.pending_command, q.command_at = phase, command, 145
            q = Queue.restore(q.dump())
            self.assertFalse(q.restart_checkpoint)
            self.assertIn(q.phase, {"cancelled", "attention"})
            if command:
                self.assertGreater(q.not_before, 145)

    def test_pending_pause_is_confirmed_after_restart_without_replaying_it(self):
        q = running()
        q.phase, q.pending_command, q.command_at = "paused", "pause", 145
        q = Queue.restore(q.dump())
        self.assertEqual(q.phase, "recovering")
        active = native(vacuum="cleaning", status="segment_mopping", now=150)
        q.observe(active, 150)
        self.assertEqual(q.phase, "recovering")
        active.vacuum, active.status, active.observed_at = "paused", "paused", 151
        q.observe(active, 151)
        self.assertEqual((q.phase, q.pending_command, q.not_before), ("paused", "", 0))
        self.assertTrue(restart.resume_token(q, active, 151))

    def test_uncertain_resume_preserves_bound_for_explicit_recovery_after_barrier(self):
        q = running()
        q.phase, q.pending_command, q.command_at = "starting", "resume", 145
        q = Queue.restore(q.dump())
        self.assertEqual((q.phase, q.restart_record_before), ("attention", 140))
        current = native(vacuum="paused", status="paused", now=1100)
        self.assertTrue(restart.resume_token(q, current, 1100))

    def test_malformed_checkpoint_preserves_plan_for_review(self):
        for mutate in [lambda q: setattr(q, "restart_checkpoint", {"junk": 1}),
                       lambda q: q.restart_checkpoint.update(matched_record={"begin": 100, "end": 200, "complete": 1, "error": 0, "finish_reason": []})]:
            q = Queue.restore(running().dump())
            mutate(q)
            restored = Queue.restore(q.dump())
            self.assertEqual(restored.phase, "attention")
            self.assertEqual(restored.run_id, "same-run")
            self.assertEqual(len(restored.stages), 2)

    def test_matching_completion_does_not_bypass_uncertain_resume_barrier(self):
        q = Queue.restore(running().dump())
        current = native(now=500)
        q.observe(current, 500)
        restart.request_resume(q, current, 500, restart.resume_token(q, current, 500))
        current.job, current.observed_at, current.record = "off", 510, record(100, 509)
        q.observe(current, 510)
        self.assertEqual((q.phase, q.pending_command, q.completed), ("starting", "resume_now", 1))

    def test_captured_completion_survives_later_fault_and_record_replacement(self):
        q = Queue.restore(running().dump())
        current = native(vacuum="cleaning", status="segment_mopping", now=500)
        q.observe(current, 500)
        current.observed_at, current.record, current.dock_error = 510, record(100, 509), "water_empty"
        q.observe(current, 510)
        self.assertEqual(q.phase, "attention")
        current.job, current.vacuum, current.status, current.observed_at = "off", "docked", "charging", 520
        current.record, current.dock_error = record(511, 519), "ok"
        recovery.observe_job(q, current, 520)
        self.assertEqual(recovery.continuation_action(q, current, 520), "continue_next")
        recovery.continue_sequence(q, current, 520)
        self.assertEqual(q.completed, 2)
        self.assertEqual(q.last_run["stages"][1]["finished_at"], 509)

    def test_refill_acknowledgement_allows_displayed_restart_resume_token(self):
        q = Queue.restore(running().dump())
        current = native(now=500, water="water_empty")
        q.observe(current, 500)
        self.assertEqual(q.phase, "attention")
        self.assertIsNone(recovery.continue_sequence(q, current, 500, water_refilled=True))
        state = recovery.state(q, current, 500)
        self.assertTrue(state["restart_resume_ready"])
        self.assertEqual(restart.request_resume(q, current, 500, state["restart_resume_token"]), ("vacuum", "start"))
        self.assertEqual(current.dock_error, "water_empty")

    def test_explicit_continue_uses_captured_proof_after_map_or_permission_interruption(self):
        q = Queue.restore(running().dump())
        current = native(job="off", now=500)
        current.record = record(101, 490)
        q.restart_matched_record = dict(current.record)
        q.attention("Map or permission changed")
        self.assertEqual(recovery.continuation_action(q, current, 500), "continue_next")
        recovery.continue_sequence(q, current, 500)
        self.assertEqual((q.completed, q.phase), (2, "finishing"))
        self.assertEqual(q.last_run["stages"][1]["finished_at"], 490)

    def test_app_resumed_job_can_explicitly_rejoin_without_motion_or_widening_proof(self):
        q = running()
        q.phase = "paused"
        q = Queue.restore(q.dump())
        current = native(vacuum="paused", status="paused", now=500)
        q.observe(current, 500)
        current.vacuum, current.status, current.observed_at = "cleaning", "segment_mopping", 510
        q.observe(current, 510)
        self.assertEqual(q.phase, "attention")
        self.assertEqual(recovery.continuation_action(q, current, 510), "resume_observation")
        self.assertIsNone(recovery.continue_sequence(q, current, 510))
        self.assertEqual((q.phase, q.restart_record_before, q.completed), ("running", 140, 1))

    def test_wall_switch_during_recovery_cancels_checkpoint_and_docks(self):
        q = Queue.restore(running().dump())
        current = native(vacuum="cleaning", status="segment_mopping", now=500)
        self.assertEqual(q.finish("vacuum.robot", current, 500, "finish"), ("vacuum", "return_to_base"))
        self.assertFalse(q.restart_checkpoint)
        current.vacuum, current.status, current.observed_at = "returning", "returning_home", 501
        q.observe(current, 501)
        self.assertEqual((q.mode, q.phase), ("finish", "controlling"))

    def test_old_attention_store_keeps_explicit_retry_escape(self):
        q = running()
        q.attention("Home Assistant stopped. The queue will not restart automatically.")
        q = Queue.restore(q.dump())
        idle = native(job="off", now=500)
        self.assertEqual(recovery.continuation_action(q, idle, 500), "retry_stage")

    def test_water_fault_after_rejoin_cannot_expand_completion_window(self):
        q = Queue.restore(running().dump())
        active = native(vacuum="cleaning", status="segment_mopping", now=500)
        q.observe(active, 500)
        active.observed_at, active.dock_error = 510, "water_empty"
        q.observe(active, 510)
        active.job, active.observed_at, active.dock_error, active.record = "off", 520, "ok", record(450, 519)
        active.vacuum, active.status = "docked", "charging"
        recovery.observe_job(q, active, 520)
        self.assertFalse(recovery.completed_after_fault(q, active))
        self.assertEqual(recovery.continuation_action(q, active, 520), "retry_stage")


class RestartLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = manual.ManagerTraceTests.asyncSetUp
    advance = manual.ManagerTraceTests.advance
    settle = manual.ManagerTraceTests.settle
    start = manual.ManagerTraceTests.start

    async def owned(self):
        self.coordinator.data.clean_summary.last_clean_record = NS(**record(self.clock - 200, self.clock - 100))
        await self.start(rooms=[{"id": "0_1", "mode": "vacuum", "suction": "max"},
                                {"id": "0_3", "mode": "mop", "water": "high", "route": "standard"}])
        await self.settle()
        status = self.coordinator.properties_api.status
        status.state_name, status.in_cleaning, status.in_returning, status.clean_time = "segment_cleaning", 3, 0, 30
        self.states["vacuum.robot"].state = "cleaning"
        await self.advance(1)
        await self.advance(1)
        self.assertEqual(self.manager.queue.phase, "running")
        return status

    async def reboot(self, *, graceful=True, delay=600):
        old = self.manager
        if graceful:
            await old.shutdown(None)
        loaded = deepcopy(old.store.saved[-1])
        self.clock += delay
        self.manager = type(old)(self.hass)
        self.manager.resolve = lambda vacuum: (self.registry, self.vacuum, self.coordinator)
        async def load_queue(): return deepcopy(loaded)
        async def empty(): return None
        self.manager.store.async_load = load_queue
        self.manager.preferences_store.async_load = empty
        self.manager.preset_store.async_load = empty
        env = self.manager.setup.__func__.__globals__
        env.update(async_track_time_interval=lambda *_: lambda: None, timedelta=timedelta,
                   EVENT_CALL_SERVICE="call_service", EVENT_HOMEASSISTANT_STOP="stop")
        self.hass.bus = NS(async_listen=lambda *_: lambda: None, async_listen_once=lambda *_: lambda: None)
        await self.manager.setup()
        return loaded

    def call(self, command, **extra):
        return NS(context=manual.FakeContext("user"), data={"command": command, "vacuum": "vacuum.robot",
                  "run_id": self.manager.queue.run_id, "current_index": self.manager.queue.current_index, **extra})

    async def test_shutdown_setup_stale_startup_then_native_completion_progresses_remaining_plan(self):
        status = await self.owned()
        run_id, start = self.manager.queue.run_id, self.manager.queue.started_at
        calls = deepcopy(self.calls)
        loaded = await self.reboot()
        self.assertEqual(loaded["phase"], "running", "Shutdown must not cancel execution intent")
        await self.manager.tick()
        self.assertEqual(self.manager.queue.phase, "recovering")
        self.assertEqual(self.calls, calls)
        await self.advance(1)
        self.assertEqual(self.manager.queue.phase, "running")
        self.assertEqual(self.calls, calls)
        status.in_cleaning, status.state_name = 0, "charging"
        self.states["vacuum.robot"].state = "docked"
        self.coordinator.data.clean_summary.last_clean_record = NS(**record(start, self.clock))
        await self.advance(1)
        self.assertEqual((self.manager.queue.completed, self.manager.queue.current_index, self.manager.queue.run_id), (1, 1, run_id))
        await self.settle()
        await self.settle()
        self.assertEqual(self.segment_calls, [["0_1"], ["0_3"]])
        self.assertEqual(self.manager.queue.last_run["stages"][0]["state"], "completed")

    async def test_completion_during_downtime_and_second_restart_credit_once(self):
        status = await self.owned()
        start = self.manager.queue.started_at
        await self.reboot()
        status.in_cleaning, status.state_name = 0, "charging"
        self.states["vacuum.robot"].state = "docked"
        self.coordinator.data.clean_summary.last_clean_record = NS(**record(start, self.clock - 20))
        await self.advance(1)
        self.assertEqual(self.manager.queue.completed, 1)
        await self.reboot()
        await self.advance(1)
        self.assertEqual(self.manager.queue.completed, 1)
        await self.settle()
        await self.settle()
        self.assertEqual(self.segment_calls, [["0_1"], ["0_3"]])

    async def test_unavailable_robot_and_startup_selects_wait_without_losing_checkpoint(self):
        await self.owned()
        await self.reboot()
        cp = deepcopy(self.manager.queue.restart_checkpoint)
        self.coordinator.last_update_success = False
        await self.advance(1)
        self.assertEqual(self.manager.queue.restart_checkpoint, cp)
        self.coordinator.last_update_success = True
        self.states["select.renamed_mode"].state = "unavailable"
        await self.advance(1)
        self.assertEqual(self.manager.queue.phase, "recovering")
        self.states["select.renamed_mode"].state = "vacuum"
        await self.advance(1)
        self.assertEqual(self.manager.queue.phase, "running")
        self.assertEqual(self.segment_calls, [["0_1"]])

    async def test_changed_map_or_removed_permissions_prevent_restart_progress(self):
        for issue in ("map", "permissions"):
            await self.asyncSetUp()
            await self.owned()
            await self.reboot()
            if issue == "map":
                self.coordinator.properties_api.maps.current_map = 1
                self.coordinator.properties_api.home.current_map_data.map_flag = 1
            else: self.allowed.clear()
            await self.advance(1)
            self.assertEqual(self.manager.queue.phase, "attention")
            self.assertEqual(self.segment_calls, [["0_1"]])
            self.assertEqual(self.manager.queue.completed, 0)

    async def test_preparing_restart_reapplies_settings_only_after_settled_idle(self):
        self.coordinator.data.clean_summary.last_clean_record = NS(**record(self.clock - 200, self.clock - 100))
        await self.start(rooms=[{"id": "0_1", "mode": "vacuum", "suction": "max"}])
        await self.reboot()
        await self.advance(1)
        self.assertEqual(self.segment_calls, [])
        await self.settle()
        await self.settle()
        self.assertEqual(self.segment_calls, [["0_1"]])

    async def test_unconfirmed_dispatched_start_is_not_replayed_on_crash(self):
        await self.start(rooms=[{"id": "0_1", "mode": "vacuum", "suction": "max"}])
        await self.settle()
        self.assertEqual(self.manager.queue.pending_command, "start")
        await self.reboot(graceful=False)
        await self.advance(1)
        self.assertEqual(self.manager.queue.phase, "attention")
        self.assertEqual(self.segment_calls, [["0_1"]])

    async def test_paused_or_charging_job_requires_fresh_explicit_token_and_exact_command(self):
        for paused in (False, True):
            await self.asyncSetUp()
            status = await self.owned()
            status.state_name = "paused" if paused else "charging"
            self.states["vacuum.robot"].state = "paused" if paused else "docked"
            if paused:
                self.manager.queue.phase = "paused"
            await self.reboot(delay=10)
            self.assertFalse(self.manager.continuation_state(self.manager.current_snapshot("vacuum.robot"))["restart_resume_ready"])
            await self.advance(1)
            self.assertFalse(any(service == "send_command" for _, service, _ in self.calls))
            state = self.manager.continuation_state(self.manager.current_snapshot("vacuum.robot"))
            self.assertTrue(state["restart_resume_ready"])
            with self.assertRaises(manual.ServiceError):
                await self.manager.control(self.call("resume"))
            before = self.manager.queue.restart_record_before
            await self.manager.control(self.call("resume_now", recovery_token=state["restart_resume_token"]))
            self.assertEqual(self.calls[-1], ("vacuum", "send_command", {"entity_id": "vacuum.robot", "command": "resume_segment_clean"}))
            self.assertEqual(self.manager.queue.restart_record_before, before)
            with self.assertRaises(manual.ServiceError):
                await self.manager.control(self.call("resume_now", recovery_token=state["restart_resume_token"]))
            status.state_name = "segment_cleaning"
            self.states["vacuum.robot"].state = "cleaning"
            await self.advance(1)
            self.assertEqual((self.manager.queue.phase, self.manager.queue.pending_command), ("running", ""))

    async def test_stale_resume_token_and_pre_dispatch_state_race_never_send_motion(self):
        status = await self.owned()
        status.state_name, self.states["vacuum.robot"].state = "charging", "docked"
        await self.reboot()
        await self.advance(1)
        state = self.manager.continuation_state(self.manager.current_snapshot("vacuum.robot"))
        status.clean_time += 1
        with self.assertRaises(manual.ServiceError):
            await self.manager.control(self.call("resume_now", recovery_token=state["restart_resume_token"]))
        state = self.manager.continuation_state(self.manager.current_snapshot("vacuum.robot"))
        original = self.hass.auth.async_get_user
        count = 0
        async def race(user):
            nonlocal count
            count += 1
            if count == 3: status.in_returning = 1
            return await original(user)
        self.hass.auth.async_get_user = race
        await self.manager.control(self.call("resume_now", recovery_token=state["restart_resume_token"]))
        self.assertFalse(any(service == "send_command" for _, service, _ in self.calls))
        self.assertEqual(self.manager.queue.phase, "attention")

    async def test_storage_failure_before_explicit_resume_never_sends_motion(self):
        status = await self.owned()
        status.state_name, self.states["vacuum.robot"].state = "charging", "docked"
        await self.reboot()
        await self.advance(1)
        state = self.manager.continuation_state(self.manager.current_snapshot("vacuum.robot"))
        async def fail(_): raise OSError("disk unavailable")
        self.manager.store.async_save = fail
        await self.manager.control(self.call("resume_now", recovery_token=state["restart_resume_token"]))
        self.assertFalse(any(service == "send_command" for _, service, _ in self.calls))

    async def test_confirmed_user_stop_still_accepts_return_to_dock(self):
        status = await self.owned()
        await self.manager.control(self.call("stop"))
        status.state_name, status.in_cleaning = "idle", 0
        self.states["vacuum.robot"].state = "idle"
        await self.advance(1)
        self.assertEqual(self.manager.queue.phase, "cancelled")
        await self.manager.control(self.call("return_to_dock"))
        self.assertEqual(self.calls[-1], ("vacuum", "return_to_base", {"entity_id": "vacuum.robot"}))
        self.assertEqual(self.manager.queue.completed, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
