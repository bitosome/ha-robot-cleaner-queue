"""Offline execution traces for persistent, shared cleaning activity evidence."""
import ast
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import backend_queue_test as qtests
import backend_manual_test as mtests
import backend_device_test as dtests

Queue, Snapshot = qtests.Queue, qtests.Snapshot
history = qtests.load("history")


def stage(target="0_1", mode="vacuum", room_index=0, pass_index=0, repeat_index=0):
    return {"target": target, "mode": mode, "room_index": room_index, "pass_index": pass_index,
            "repeat_index": repeat_index, "map_id": 0, "segments": ["1"],
            "settings": {"mode": mode, **({"water": "high"} if mode == "mop" else {"suction": "max"})}}


def start(queue=None, now=100, run_id="run1", stages=None):
    queue = queue or Queue()
    stages = stages or [stage(), stage("0_2", "mop", 1)]
    targets = list(dict.fromkeys(s["target"] for s in stages if s["target"]))
    queue.start_manual("vacuum.robot", targets, {}, stages, {}, qtests.ready(observed_at=now), now, run_id,
                       room_names={"0_1": "Office", "0_2": "Kitchen"})
    return queue


def dispatch_and_ack(queue, now=110):
    ready = qtests.ready(observed_at=now)
    ready.settings = deepcopy(queue.stage["settings"])
    queue.observe(ready, now)
    ready.observed_at = now + 15
    assert queue.observe(ready, now + 15) == ("manual", str(queue.current_index))
    active = qtests.cleaning()
    active.observed_at = now + 20
    queue.observe(active, now + 20)


def first_complete():
    queue = start()
    dispatch_and_ack(queue)
    queue.observe(qtests.ready(qtests.record(125, 200), observed_at=202), 202)
    return queue


class HistoryTests(unittest.TestCase):
    def test_assignment_has_all_named_rooms_modes_and_no_invented_start_times(self):
        queue = start()
        run = queue.last_run
        self.assertEqual((run["assigned_at"], len(run["rooms"]), len(run["stages"])), (100, 2, 2))
        self.assertEqual([r["name"] for r in run["rooms"]], ["Office", "Kitchen"])
        self.assertEqual([s["state"] for s in run["stages"]], ["preparing", "pending"])
        self.assertTrue(all(s["started_at"] is None and s["finished_at"] is None for s in run["stages"]))

    def test_success_uses_native_begin_end_and_retains_observation_confirmation(self):
        queue = first_complete()
        result = queue.last_run["stages"][0]
        self.assertEqual((result["state"], result["started_at"], result["finished_at"], result["confirmed_at"]),
                         ("completed", 125, 200, 202))
        self.assertEqual(result["evidence_source"], "native_record")
        self.assertEqual(queue.last_run["stages"][1]["state"], "pending")

    def test_water_fault_and_new_remaining_room_start_retains_original_sequence(self):
        queue = first_complete()
        result = deepcopy(queue.last_run["stages"][0])
        current = qtests.ready(qtests.record(125, 200), observed_at=210)
        current.dock_error = "water_empty"
        queue.observe(current, 210)
        self.assertEqual(queue.phase, "attention")
        self.assertEqual(queue.last_run["stages"][0], result)
        self.assertEqual(queue.last_run["stages"][1]["state"], "pending")
        self.assertEqual(queue.last_run["interruptions"][-1]["at"], 210)
        start(queue, 400, "remaining", [stage("0_2", "mop")])
        archived = queue.previous_runs[0]
        self.assertEqual((archived["run_id"], archived["state"], len(archived["rooms"])), ("run1", "attention", 2))
        self.assertEqual(archived["stages"][0], result)
        self.assertEqual(queue.last_run["run_id"], "remaining")
        restored = Queue.restore(queue.dump())
        self.assertEqual(restored.previous_runs, queue.previous_runs)
        self.assertEqual(restored.last_run["state"], "recovering")
        self.assertIsNone(restored.observe(qtests.ready(observed_at=500), 500))

    def test_current_incomplete_pass_keeps_start_observation_but_not_completion(self):
        queue = start()
        dispatch_and_ack(queue)
        queue.observe(Snapshot(), 150)
        result = queue.last_run["stages"][0]
        self.assertEqual((result["state"], result["started_at"], result["finished_at"]), ("attention", 130, None))
        self.assertEqual(queue.last_run["stages"][1]["state"], "pending")

    def test_pause_resume_does_not_replace_assignment_or_first_observed_start(self):
        queue = start()
        dispatch_and_ack(queue)
        current = qtests.cleaning(); current.observed_at = 140
        queue.command("pause", current, 140)
        paused = Snapshot("paused", "paused", "on", "none", "ok", True, observed_at=145)
        queue.observe(paused, 145)
        self.assertEqual(queue.last_run["stages"][0]["state"], "paused")
        queue.command("resume", paused, 150)
        current.observed_at = 155
        queue.observe(current, 155)
        self.assertEqual(queue.last_run["stages"][0]["started_at"], 130)
        self.assertEqual((queue.last_run["run_id"], len(queue.last_run["stages"])), ("run1", 2))

    def test_cancel_and_finish_keep_completed_evidence_and_unattempted_rooms(self):
        for command in ("cancel", "finish"):
            queue = first_complete()
            result = deepcopy(queue.last_run["stages"][0])
            if command == "cancel":
                queue.command(command, qtests.ready(), 215)
            else:
                queue.finish("vacuum.robot", qtests.ready(observed_at=215), 215, "finish")
            self.assertEqual(queue.last_run["state"], "cancelled")
            self.assertEqual(queue.last_run["finished_at"], 215)
            self.assertEqual(queue.last_run["stages"][0], result)
            self.assertEqual(queue.last_run["stages"][1]["state"], "pending")

    def test_external_resume_keeps_history_but_never_adopts_a_later_success_record(self):
        queue = first_complete()
        queue.attention("Water empty")
        paused = Snapshot("paused", "paused", "on", "none", "ok", True, observed_at=300)
        queue.external_control("resume", "vacuum.robot", paused, 300, "external")
        run = deepcopy(queue.last_run)
        active = qtests.cleaning(); active.observed_at = 310
        queue.observe(active, 310)
        queue.observe(qtests.ready(qtests.record(310, 500), observed_at=500), 500)
        self.assertEqual(queue.last_run, run)
        self.assertEqual((queue.stages, queue.completed), ([], 0))

    def test_legacy_migration_keeps_completed_count_with_unknown_times(self):
        data = dict(mode="manual", phase="attention", vacuum="vacuum.robot", run_id="legacy-run",
                    stages=[stage(), stage("0_2", "mop", 1)], completed=1, current_index=1,
                    command_at=180, not_before=240, error="Interrupted")
        queue = Queue.restore(data)
        result = queue.last_run["stages"][0]
        self.assertEqual((result["state"], result["evidence_source"]), ("completed", "legacy_queue"))
        self.assertTrue(all(result[k] is None for k in ("started_at", "finished_at", "confirmed_at")))
        self.assertIsNone(queue.last_run["assigned_at"])
        self.assertEqual(queue.not_before, 240)
        data.update(phase="completed", completed=2, current_index=2)
        completed = Queue.restore(data)
        history.sync(completed, 500)
        self.assertIsNone(completed.last_run["finished_at"], "Migration time is not the old completion time")

    def test_malformed_journal_does_not_destroy_execution_barrier_or_legacy_evidence(self):
        for malformed in ([], {"state": []}, {"run_id": "x", "vacuum": "vacuum.robot", "state": [], "stages": [stage()]}):
            data = dict(mode="manual", phase="starting", vacuum="vacuum.robot", run_id="run", stages=[stage()],
                        pending_command="start", command_at=100, last_run=malformed, previous_runs={}, history_revision=[])
            queue = Queue.restore(data)
            self.assertEqual((queue.phase, queue.not_before), ("attention", 1000))
            self.assertEqual(queue.last_run["run_id"], "run")
        run = first_complete().last_run
        run["stages"][0]["evidence_source"] = {}
        self.assertIsNotNone(history.normalize(run))
        run["stages"][0]["state"] = {}
        self.assertIsNone(history.normalize(run))

    def test_snapshots_dump_and_api_are_deep_copies(self):
        queue = start()
        api = history.response(queue, "vacuum.robot")
        api["last_run"]["stages"][0]["settings"]["suction"] = "wrong"
        dumped = queue.dump()
        dumped["last_run"]["rooms"][0]["name"] = "wrong"
        self.assertEqual(queue.last_run["stages"][0]["settings"]["suction"], "max")
        self.assertEqual(queue.last_run["rooms"][0]["name"], "Office")
        stages = [stage()]
        queue = start(stages=stages)
        stages[0]["settings"]["suction"] = "wrong"
        self.assertEqual(queue.stage["settings"]["suction"], "max")

    def test_archived_or_detached_active_snapshots_restore_as_unconfirmed(self):
        queue = start()
        archived = deepcopy(queue.last_run)
        archived["run_id"] = "old"
        archived["state"] = "running"
        archived["stages"][0]["state"] = "running"
        queue.previous_runs = [archived, deepcopy(archived), deepcopy(queue.last_run)]
        restored = Queue.restore(queue.dump())
        self.assertEqual(len(restored.previous_runs), 1)
        self.assertEqual(restored.previous_runs[0]["state"], "attention")
        self.assertEqual(restored.previous_runs[0]["stages"][0]["state"], "attention")
        self.assertIsNone(restored.previous_runs[0]["stages"][0]["finished_at"])
        detached = Queue.restore({"mode": "device", "phase": "idle", "last_run": archived})
        self.assertEqual(detached.last_run["state"], "attention")

    def test_stale_records_failed_records_and_preparation_never_claim_completed_work(self):
        for result in (qtests.record(10, 20), qtests.record(125, 200, complete=0),
                       qtests.record(125, 200, error=1), qtests.record(125, 200, finish_reason=21)):
            queue = start()
            dispatch_and_ack(queue)
            queue.observe(qtests.ready(result, observed_at=202), 202)
            self.assertNotEqual(queue.last_run["stages"][0]["state"], "completed")
            self.assertIsNone(queue.last_run["stages"][0]["finished_at"])
        queue = start()
        ready = qtests.ready(observed_at=110); ready.settings = deepcopy(queue.stage["settings"])
        queue.observe(ready, 110); ready.observed_at = 125; queue.observe(ready, 125)
        washing = Snapshot("docked", "washing_the_mop", "on", "none", "ok", True, observed_at=130)
        queue.observe(washing, 130)
        self.assertIsNone(queue.last_run["stages"][0]["started_at"])

    def test_rejected_start_and_invalid_external_control_do_not_touch_history(self):
        queue = start()
        before = queue.dump()
        with self.assertRaises(ValueError):
            start(queue, 101, "rejected")
        self.assertEqual(queue.dump(), before)
        with self.assertRaises(ValueError):
            queue.external_control("pause", "vacuum.robot", qtests.ready(), 101, "rejected")
        self.assertEqual(queue.dump(), before)

    def test_repeated_passes_whole_home_and_bounded_archives(self):
        stages = [stage("", mode, 0, p, r) for r in range(2) for p, mode in enumerate(["vacuum", "mop"])]
        queue = start(stages=stages)
        self.assertEqual(queue.last_run["rooms"][0]["name"], "Whole home")
        self.assertEqual([(s["mode"], s["repeat_index"]) for s in queue.last_run["stages"]],
                         [("vacuum", 0), ("mop", 0), ("vacuum", 1), ("mop", 1)])
        for index in range(1, 8):
            queue.command("cancel", qtests.ready(), 100 + index * 100)
            start(queue, 200 + index * 100, "run%d" % (index + 1))
        self.assertEqual([r["run_id"] for r in queue.previous_runs], ["run7", "run6", "run5", "run4", "run3"])
        self.assertEqual(len(history.response(queue, "vacuum.other")["previous_runs"]), 0)

    def test_unchanged_tick_and_publish_sync_do_not_duplicate_events_or_shift_times(self):
        queue = first_complete()
        queue.command("cancel", qtests.ready(), 210)
        before = queue.dump()
        for now in (300, 500, 600):
            self.assertIsNone(queue.observe(qtests.ready(observed_at=now), now))
            history.sync(queue, now)
        self.assertEqual(queue.dump(), before)

    def test_sensor_summaries_exclude_heavy_journal_and_recorded_attributes_fit_max_plan(self):
        stages = [stage("0_1234567890_%d" % i, mode, i, p, r)
                  for i in range(32) for r in range(2) for p, mode in enumerate(["vacuum", "mop"])]
        queue = start(stages=stages)
        queue.setup = {"rooms": [{"id": s["target"], "name": "Generic room " + str(i),
                                  "setup": {"mode": "vacuum_then_mop", "suction": "max_plus", "water": "custom_water_flow", "route": "deep_plus", "repeat": 2}}
                                 for i, s in enumerate(stages[::4])]}
        queue.previous_runs = [deepcopy(queue.last_run) for _ in range(5)]
        source = qtests.PACKAGE / "sensor.py"
        definition = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef))
        env = {"SensorEntity": object, "history": history, "editing": qtests.load("editing")}
        exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), "exec"), env)
        sensor = env["QueueSensor"](NS(queue=queue, robot_state={}, preferences={}, saved_presets={}))
        attrs = sensor.extra_state_attributes
        self.assertNotIn("stages", attrs["last_run"])
        self.assertNotIn("rooms", attrs["previous_runs"][0])
        recorded = {key: value for key, value in attrs.items() if key not in sensor._unrecorded_attributes}
        self.assertLess(len(json.dumps(recorded).encode()), 16384)


class HistoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await mtests.ManagerTraceTests.asyncSetUp(self)

    async def test_history_requires_permission_and_filters_other_robot(self):
        self.manager.queue = first_complete()
        call = NS(context=mtests.FakeContext("user"), data={"vacuum": "vacuum.robot"})
        response = await self.manager.get_history(call)
        self.assertEqual(response["last_run"]["stages"][0]["state"], "completed")
        self.assertFalse(self.calls)
        self.allowed.clear()
        with self.assertRaises(mtests.ServiceError):
            await self.manager.get_history(call)
        self.allowed.add("vacuum.other")
        call.data["vacuum"] = "vacuum.other"
        self.assertEqual((await self.manager.get_history(call))["last_run"], None)

    async def test_publish_persists_journal_and_repeat_publish_is_noop(self):
        self.manager.queue = first_complete()
        await self.manager.publish()
        saved = self.manager.store.saved[-1]
        self.assertEqual(saved["last_run"]["stages"][0]["finished_at"], 200)
        count = len(self.manager.store.saved)
        await self.manager.publish()
        self.assertEqual(len(self.manager.store.saved), count)

    async def test_transient_storage_failure_retries_evidence_without_robot_action(self):
        self.manager.queue = first_complete()
        saved = []
        async def save(data):
            if not getattr(self, "storage_recovered", False):
                raise OSError("test storage unavailable")
            saved.append(deepcopy(data))
        self.manager.store.async_save = save
        with self.assertLogs("test", level="ERROR"):
            await self.manager.publish()
        self.assertEqual(self.manager.queue.phase, "attention")
        self.assertIsNone(self.manager.last_published)
        self.storage_recovered = True
        await self.manager.publish()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["last_run"]["stages"][0]["finished_at"], 200)
        self.assertFalse(self.calls)

    async def test_history_response_remains_detached_and_other_vacuum_archive_is_filtered(self):
        queue = first_complete()
        previous = deepcopy(queue.last_run); previous["vacuum"] = "vacuum.other"; previous["run_id"] = "other"
        queue.previous_runs = [previous]
        self.manager.queue = queue
        call = NS(context=mtests.FakeContext("user"), data={"vacuum": "vacuum.robot"})
        response = await self.manager.get_history(call)
        self.assertEqual(response["previous_runs"], [])
        response["last_run"]["stages"].clear()
        self.assertEqual(len(queue.last_run["stages"]), 2)


class HistoryDeviceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await dtests.DeviceTests.asyncSetUp(self)

    async def test_device_reservation_retains_last_run_and_archive(self):
        queue = first_complete()
        queue.command("cancel", qtests.ready(), 250)
        self.manager.queue = queue
        before = deepcopy(queue.last_run)
        await dtests.DeviceTests.send(self, "child_lock", "on")
        self.assertEqual(self.manager.queue.mode, "device")
        self.assertEqual(self.manager.queue.last_run, before)
        self.assertEqual(self.manager.store.saved[-1]["last_run"], before)

    async def test_noop_device_command_keeps_journal_byte_for_byte(self):
        queue = first_complete()
        queue.command("cancel", qtests.ready(), 250)
        self.manager.queue = queue
        before = queue.dump()
        await dtests.DeviceTests.send(self, "child_lock", "off")
        self.assertEqual(self.manager.queue.dump(), before)
        self.assertFalse(self.calls)


class FaultJournalTests(unittest.TestCase):
    def test_fault_onset_dedup_resolution_and_restore(self):
        q=start(); q.phase="running"
        current=qtests.ready(observed_at=150); current.error="error"; current.error_detail="clear_water_box_hoare"
        history.observe_faults(q,current,150); history.observe_faults(q,current,151)
        self.assertEqual(len(q.last_run["faults"]),1)
        self.assertEqual(q.last_run["faults"][0]["message"],"Clean-water tank warning")
        current.error="none"; current.dock_error="water_empty"; current.observed_at=152
        history.observe_faults(q,current,152)
        self.assertEqual(q.last_run["faults"][0]["resolved_at"],152)
        self.assertEqual(len(q.last_run["faults"]),2)
        current.dock_error="ok"
        history.observe_faults(q,current,300)
        self.assertIsNone(q.last_run["faults"][1]["resolved_at"],"Stale status cannot resolve a fault")
        current.observed_at=301; history.observe_faults(q,current,301)
        self.assertEqual(q.last_run["faults"][1]["resolved_at"],301)
        restored=Queue.restore(q.dump())
        self.assertEqual(restored.last_run["faults"],q.last_run["faults"])

    def test_fault_journal_is_bounded_and_old_history_migrates(self):
        q=start();q.phase="running"
        for n in range(60):
            current=qtests.ready(observed_at=150+n);current.dock_error="water_empty" if n%2==0 else "ok"
            history.observe_faults(q,current,150+n)
        self.assertEqual(len(q.last_run["faults"]),24)
        old=deepcopy(q.last_run);old.pop("faults")
        self.assertEqual(history.normalize(old)["faults"],[])


if __name__ == "__main__":
    unittest.main(verbosity=2)
