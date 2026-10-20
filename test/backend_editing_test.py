"""Offline live-queue edits: preservation, concurrency, durable history and recovery."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS
import unittest

import backend_manual_test as manual
from backend_queue_test import Queue, Snapshot, load, record

editing, history = load("editing"), load("history")


class EditingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = manual.ManagerTraceTests.asyncSetUp
    start = manual.ManagerTraceTests.start
    settle = manual.ManagerTraceTests.settle
    advance = manual.ManagerTraceTests.advance

    async def plan(self):
        await self.start(rooms=[{"id": "0_1", "mode": "vacuum_then_mop"},
                                {"id": "0_2", "mode": "mop"},
                                {"id": "0_3", "mode": "vacuum"}])
        self.calls.clear()
        self.segment_calls.clear()

    async def edit(self, rooms, **changes):
        q = self.manager.queue
        data = {"vacuum": q.vacuum, "run_id": q.run_id,
                "revision": editing.state(q)["queue_revision"], "rooms": rooms, **changes}
        await self.manager.update_queue(NS(context=manual.FakeContext("user"), data=data))

    def pending(self):
        return [{"id": "0_3", "mode": "vacuum", "suction": "max"},
                {"id": "0_2", "mode": "mop", "water": "high", "route": "standard"}]

    async def test_reorder_preserves_current_pass_and_settings_and_sends_no_commands(self):
        await self.plan()
        q = self.manager.queue
        frozen = deepcopy(q.stages[:2])
        before = q.dump()
        await self.edit(self.pending())
        q = self.manager.queue
        self.assertEqual(q.targets, ["0_1", "0_3", "0_2"])
        self.assertEqual(q.stages[:2], frozen)
        self.assertEqual([s["room_index"] for s in q.stages], [0, 0, 1, 2])
        for key in ("run_id", "current_index", "completed", "pending_command", "owner_user_id", "command_at", "started_at", "restart_checkpoint", "not_before"):
            self.assertEqual(q.dump()[key], before[key], key)
        self.assertGreater(q.queue_revision, before["queue_revision"])
        self.assertEqual((self.calls, self.segment_calls), ([], []))
        self.assertTrue(self.manager.store.saved[-1]["edit_pending"])
        await self.manager.publish()
        self.assertEqual(self.manager.store.saved[-1], q.dump())

    async def test_remove_add_and_readd_are_journaled_not_counted_as_completed(self):
        await self.plan()
        await self.edit(self.pending()[:1])
        self.assertEqual(self.manager.queue.completed, 0)
        event = self.manager.queue.last_run["edits"][-1]
        self.assertEqual(event["removed_rooms"][0]["id"], "0_2")
        self.assertEqual(event["removed_rooms"][0]["state"], "removed")
        await self.edit(self.pending())
        q = self.manager.queue
        self.assertEqual(q.last_run["edits"][-1]["added_room_ids"], ["0_2"])
        self.assertEqual(q.last_run["stages"][-1]["state"], "pending")
        self.assertIsNone(q.last_run["stages"][-1]["finished_at"])
        restored = Queue.restore(q.dump())
        self.assertEqual(restored.targets, q.targets)
        self.assertEqual(restored.last_run["edits"], q.last_run["edits"])
        self.assertEqual(restored.stages, q.stages)

    async def test_completed_vacuum_keeps_pending_mop_and_proven_timestamps(self):
        await self.plan()
        q = self.manager.queue
        history.complete_stage(q, record(100, 120), 125)
        q.completed = q.current_index = 1
        q.pending_command, q.phase, q.next_pending = "", "running", True
        proof = deepcopy(q.last_run["stages"][0])
        self.assertEqual(editing.state(q)["protected_room_ids"], ["0_1"])
        await self.edit([])
        q = self.manager.queue
        self.assertEqual(q.targets, ["0_1"])
        self.assertEqual(q.stage["mode"], "mop")
        self.assertEqual(q.last_run["stages"][0], proof)
        self.assertEqual(q.last_run["stages"][1]["state"], "pending")
        self.assertEqual((q.completed, q.current_index, q.phase), (1, 1, "running"))

    async def between(self, phase="running"):
        await self.plan()
        q = self.manager.queue
        for index in range(2):
            q.current_index = index
            history.complete_stage(q, record(100 + index * 30, 120 + index * 30), 155)
        q.current_index = q.completed = 2
        q.pending_command, q.phase, q.next_pending = "", phase, True
        q.interrupted_next_pending = phase == "attention"
        q.finish_wait_at = self.clock
        return q

    async def test_whole_unstarted_next_room_is_editable(self):
        q = await self.between()
        self.assertEqual(editing.state(q)["editable_room_ids"], ["0_2", "0_3"])
        await self.edit(self.pending()[:1])
        self.assertEqual(self.manager.queue.stage["target"], "0_3")
        self.assertEqual(self.manager.queue.current_index, 2)

    async def test_remove_all_between_rooms_enters_finishing_without_motion(self):
        await self.between()
        await self.edit([])
        q = self.manager.queue
        self.assertEqual((q.phase, q.current_index, q.completed), ("finishing", 2, 2))
        self.assertFalse(q.next_pending)
        self.assertFalse(editing.state(q)["queue_editable"])
        self.assertEqual((self.calls, self.segment_calls), ([], []))
        self.assertTrue(all(s["state"] == "completed" for s in q.last_run["stages"]))

    async def test_remove_all_between_rooms_retains_pause_until_explicit_resume(self):
        await self.between("paused")
        await self.edit([])
        q = self.manager.queue
        self.assertEqual(q.phase, "paused")
        current = Snapshot("docked", "charging", "off", "none", "ok", True, observed_at=self.clock)
        self.assertIsNone(q.command("resume", current, self.clock))
        self.assertEqual((q.phase, q.next_pending), ("finishing", False))
        self.assertIsNone(q.observe(current, self.clock))

    async def test_remove_all_during_attention_retains_fault_and_continuation(self):
        await self.between("attention")
        self.manager.queue.error = "Refill the tank"
        await self.edit([])
        q = self.manager.queue
        self.assertEqual((q.phase, q.error), ("attention", "Refill the tank"))
        current = Snapshot("docked", "charging", "off", "none", "ok", True, observed_at=self.clock)
        self.assertIsNone(load("recovery").continue_sequence(q, current, self.clock))
        self.assertEqual(q.phase, "finishing")

    async def test_started_completed_or_duplicate_room_cannot_be_added(self):
        await self.plan()
        for rooms in [[{"id": "0_1", "mode": "vacuum"}], self.pending() + self.pending(), [{"id": "0_99", "mode": "vacuum"}]]:
            before = self.manager.queue.dump()
            with self.assertRaises(manual.ServiceError):
                await self.edit(rooms)
            self.assertEqual(self.manager.queue.dump(), before)
        self.assertEqual((self.calls, self.segment_calls), ([], []))

    async def test_stale_revision_run_and_wrong_vacuum_rejected(self):
        await self.plan()
        q = self.manager.queue
        revision = editing.state(q)["queue_revision"]
        await self.edit(self.pending())
        before = self.manager.queue.dump()
        for changes in [{"revision": revision}, {"run_id": "other"}, {"vacuum": "vacuum.other"}]:
            with self.assertRaisesRegex(manual.ServiceError, "changed"):
                await self.edit([], **changes)
            self.assertEqual(self.manager.queue.dump(), before)

    async def test_stage_reservation_invalidates_draft_without_a_prior_sensor_read(self):
        await self.plan()
        q = self.manager.queue
        revision = editing.state(q)["queue_revision"]
        q.phase, q.pending_command = "starting", "start"
        with self.assertRaisesRegex(manual.ServiceError, "changed"):
            await self.edit([], revision=revision)

    async def test_two_editors_same_revision_only_one_wins(self):
        await self.plan()
        revision = editing.state(self.manager.queue)["queue_revision"]
        results = await asyncio.gather(self.edit(self.pending(), revision=revision),
                                       self.edit([], revision=revision), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, manual.ServiceError) for result in results), 1)
        self.assertEqual(self.manager.queue.targets, ["0_1", "0_3", "0_2"])

    async def test_save_failure_preserves_live_queue_and_commands(self):
        await self.plan()
        before = self.manager.queue.dump()
        async def fail(_data):
            raise OSError("disk unavailable")
        self.manager.store.async_save = fail
        with self.assertRaisesRegex(manual.ServiceError, "could not be saved"):
            await self.edit(self.pending())
        self.assertEqual(self.manager.queue.dump(), before)
        self.assertEqual((self.calls, self.segment_calls), ([], []))

    async def test_external_interruption_during_save_is_preserved_and_durably_rolled_back(self):
        await self.plan()
        save = self.manager.store.async_save
        interrupted = False
        async def interrupt(data):
            nonlocal interrupted
            await save(data)
            if not interrupted:
                interrupted = True
                self.manager.queue.attention("External robot control")
        self.manager.store.async_save = interrupt
        with self.assertRaisesRegex(manual.ServiceError, "changed while saving"):
            await self.edit(self.pending())
        q = self.manager.queue
        self.assertEqual((q.phase, q.error), ("attention", "External robot control"))
        self.assertEqual(q.targets, ["0_1", "0_2", "0_3"])
        self.assertEqual(self.manager.store.saved[-1], q.dump())
        self.assertEqual((self.calls, self.segment_calls), ([], []))

    async def test_shutdown_during_save_does_not_commit_candidate(self):
        await self.plan()
        save = self.manager.store.async_save
        async def stop(data):
            await save(data)
            self.manager.closing = True
        self.manager.store.async_save = stop
        with self.assertRaisesRegex(manual.ServiceError, "changed while saving"):
            await self.edit(self.pending())
        self.assertEqual(self.manager.queue.targets, ["0_1", "0_2", "0_3"])
        self.assertEqual(self.manager.store.saved[-1], self.manager.queue.dump())

    async def test_external_interruption_during_permission_check_is_not_overwritten(self):
        await self.plan()
        original = self.hass.auth.async_get_user
        async def interrupt(uid):
            self.manager.queue.attention("Competing command")
            return await original(uid)
        self.hass.auth.async_get_user = interrupt
        with self.assertRaisesRegex(manual.ServiceError, "checking permissions"):
            await self.edit(self.pending())
        self.assertEqual(self.manager.queue.error, "Competing command")
        self.assertEqual(self.manager.queue.targets, ["0_1", "0_2", "0_3"])

    async def test_revoked_setting_permission_rejects_without_mutating_plan(self):
        await self.plan()
        self.allowed.remove("select.renamed_mode")
        before = self.manager.queue.dump()
        with self.assertRaises(manual.ServiceError):
            await self.edit(self.pending())
        self.assertEqual(self.manager.queue.dump(), before)

    async def test_map_and_settings_revalidated_after_permission_await(self):
        await self.plan()
        original = self.hass.auth.async_get_user
        async def change_map(uid):
            self.coordinator.properties_api.maps.current_map = 4
            return await original(uid)
        self.hass.auth.async_get_user = change_map
        with self.assertRaises(manual.ServiceError):
            await self.edit(self.pending())
        self.assertEqual(self.manager.queue.targets, ["0_1", "0_2", "0_3"])

    async def test_recovering_and_terminal_queues_refuse_edits(self):
        await self.plan()
        for phase in ("recovering", "finishing", "completed", "cancelled"):
            self.manager.queue.phase = phase
            self.assertFalse(editing.state(self.manager.queue)["queue_editable"])
            with self.assertRaises(manual.ServiceError):
                await self.edit([])

    async def test_saved_plan_and_shared_defaults_are_unchanged(self):
        await self.plan()
        self.manager.saved_presets = {"vacuum.robot": {"revision": 8, "rooms": ["saved"]}}
        self.manager.preferences = {"vacuum.robot": {"revision": 4}}
        before = deepcopy((self.manager.saved_presets, self.manager.preferences))
        await self.edit(self.pending())
        self.assertEqual((self.manager.saved_presets, self.manager.preferences), before)

    async def test_immediate_restart_guard_retains_amended_plan_without_motion(self):
        await self.plan()
        await self.edit(self.pending())
        restored = Queue.restore(self.manager.store.saved[-1])
        self.assertEqual(restored.phase, "attention")
        self.assertIn("edit needs review", restored.error)
        self.assertEqual(restored.targets, ["0_1", "0_3", "0_2"])
        self.assertEqual(restored.stages, self.manager.queue.stages)
        self.assertFalse(restored.edit_pending)
        self.assertEqual((self.calls, self.segment_calls), ([], []))

    async def test_failed_interruption_rollback_keeps_disk_candidate_guarded(self):
        await self.plan()
        save = self.manager.store.async_save
        attempts = 0
        notifications = []
        self.manager.listeners.append(lambda: notifications.append(self.manager.queue.phase))
        async def interrupt_then_fail(data):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await save(data)
                self.manager.queue.attention("External command")
            else:
                raise OSError("rollback disk failure")
        self.manager.store.async_save = interrupt_then_fail
        with self.assertRaisesRegex(manual.ServiceError, "persisted plan may contain"):
            await self.edit(self.pending())
        self.assertEqual(self.manager.queue.phase, "attention")
        self.assertEqual(notifications, ["attention"])
        restored = Queue.restore(self.manager.store.saved[-1])
        self.assertEqual(restored.phase, "attention")
        self.assertEqual(restored.targets, ["0_1", "0_3", "0_2"])
        self.assertEqual((self.calls, self.segment_calls), ([], []))

    async def test_control_finalizes_guard_before_any_robot_command(self):
        await self.plan()
        await self.edit(self.pending())
        self.assertTrue(self.manager.store.saved[-1]["edit_pending"])
        await self.settle()
        self.assertFalse(self.manager.store.saved[-1]["edit_pending"])
        self.assertEqual(self.segment_calls, [["0_1"]])

    async def test_boolean_revision_is_rejected(self):
        await self.plan()
        with self.assertRaisesRegex(manual.ServiceError, "changed"):
            await self.edit([], revision=True)

    async def test_water_fault_allows_pending_edits_without_resuming_the_robot(self):
        await self.plan()
        q = self.manager.queue
        q.attention("Clean water empty", recoverable_fault=True)
        self.coordinator.properties_api.status.dock_error_status = 38
        await self.edit(self.pending())
        self.assertEqual((self.manager.queue.phase, self.manager.queue.error), ("attention", "Clean water empty"))
        self.assertEqual((self.calls, self.segment_calls), ([], []))

    async def test_dock_unavailable_controls_allow_unchanged_remove_reorder(self):
        await self.plan()
        q = self.manager.queue
        requests = [{"id": room["id"], **room["setup"]} for room in reversed(q.setup["rooms"][1:])]
        for key in ("mode", "water", "route"):
            self.states["select.renamed_" + key].state = "unavailable"
        await self.edit(requests)
        self.assertEqual(self.manager.queue.targets, ["0_1", "0_3", "0_2"])
        await self.edit([])
        self.assertEqual(self.manager.queue.targets, ["0_1"])
        with self.assertRaisesRegex(manual.ServiceError, "Wait for native"):
            await self.edit(requests)
        self.assertEqual((self.calls, self.segment_calls), ([], []))

    async def test_dock_unavailable_controls_do_not_allow_new_settings(self):
        await self.plan()
        self.states["select.renamed_water"].state = "unavailable"
        with self.assertRaisesRegex(manual.ServiceError, "Wait for native"):
            await self.edit([{"id": "0_2", "mode": "vacuum", "suction": "quiet"}])
        self.assertEqual(self.manager.queue.targets, ["0_1", "0_2", "0_3"])

    async def test_accepted_noop_consumes_revision(self):
        await self.plan()
        q = self.manager.queue
        revision = editing.state(q)["queue_revision"]
        same = [{"id": room["id"], **room["setup"]} for room in q.setup["rooms"][1:]]
        await self.edit(same)
        self.assertGreater(self.manager.queue.queue_revision, revision)
        with self.assertRaisesRegex(manual.ServiceError, "changed"):
            await self.edit(same, revision=revision)

    async def test_reordered_suffix_is_the_next_native_room_dispatched(self):
        await self.between()
        await self.edit(self.pending())
        self.assertEqual((self.calls, self.segment_calls), ([], []))
        await self.advance(1)
        await self.advance(16)
        await self.settle()
        self.assertEqual(self.segment_calls, [["0_3"]])
        self.assertEqual(self.manager.queue.current_index, 2)
        self.assertFalse(self.manager.store.saved[-1]["edit_pending"])

    async def test_other_editor_cannot_bypass_original_owner_permission_revocation(self):
        await self.plan()
        self.manager.queue.owner_user_id = "original-owner"
        get_user = self.hass.auth.async_get_user
        async def users(uid):
            if uid == "original-owner":
                return NS(is_active=False, is_admin=False)
            return await get_user(uid)
        self.hass.auth.async_get_user = users
        with self.assertRaises(manual.ServiceError):
            await self.edit(self.pending())
        self.assertEqual(self.manager.queue.targets, ["0_1", "0_2", "0_3"])
        self.assertEqual((self.calls, self.segment_calls), ([], []))

    async def test_partial_room_restart_keeps_current_job_evidence_and_edited_tail(self):
        await self.plan()
        q = self.manager.queue
        q.phase, q.pending_command = "running", ""
        q.seen_job, q.started_at, q.baseline_end = True, 100, 80
        q.native_job_observed_at, q.native_job_type, q.native_clean_time = 140, 3, 40
        q.checkpoint_observed_at, q.checkpoint_record_end = 140, 80
        await self.edit(self.pending())
        q = self.manager.queue
        restored = Queue.restore(q.dump())
        self.assertEqual(restored.phase, "recovering")
        self.assertEqual(restored.restart_checkpoint["owned_before"], 140)
        self.assertEqual(restored.stage, q.stage)
        self.assertEqual(restored.stages, q.stages)
        self.assertEqual(restored.targets, ["0_1", "0_3", "0_2"])
        self.assertEqual(restored.completed, 0)
        self.assertEqual(restored.last_run["edits"], q.last_run["edits"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
