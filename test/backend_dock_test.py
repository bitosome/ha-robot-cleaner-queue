"""Offline dock lifecycle evidence, cancellation, restart and real Manager dispatch."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).parent))
import backend_manual_test as m
from backend_queue_test import Queue, Snapshot, load
import backend_device_test as device_tests

dock = load("dock")


def snap(at, status="charging", *, job="off", fault="ok", controls=None):
    vacuum = "cleaning" if status == "segment_cleaning" else "returning" if status == "returning_home" else "docked"
    return Snapshot(vacuum, status, job, "none", fault, True, observed_at=at,
        dock_controls=controls if controls is not None else {"dust_emptying":"off", "mop_washing":"off", "mop_drying":"on"})


def plan(mode="vacuum_mop"):
    stage={"target":"0_1", "mode":mode, "room_index":0, "pass_index":0, "repeat_index":0, "settings":{"mode":mode}}
    q=Queue(mode="manual",phase="running",vacuum="vacuum.robot",run_id="run",stages=[stage],targets=["0_1"],started_at=100)
    q.observe(snap(110,"segment_cleaning",job="on"),110)
    return q


def finished(mode="vacuum_mop"):
    q=plan(mode)
    q.finish(q.vacuum,snap(120,"segment_cleaning",job="on"),120,"unused")
    q.observe(snap(121,"returning_home"),121)
    return q


def native_care(q, key, at):
    status="emptying_the_bin" if key=="dust_emptying" else "washing_the_mop"
    q.observe(snap(at,status),at)
    q.observe(snap(at+5),at+5)
    q.observe(snap(at+21),at+21)


class DockTests(unittest.TestCase):
    def test_only_actual_work_creates_obligations(self):
        q=Queue(mode="manual",phase="preparing",run_id="run",stages=[{"mode":"vacuum_mop"}])
        q.finish("vacuum.robot",snap(100),100,"unused")
        self.assertFalse(q.dock_cleanup)
        for mode, expected in [("vacuum",{"dust_emptying":True,"mop_washing":False}),
                               ("mop",{"dust_emptying":False,"mop_washing":True}),
                               ("vacuum_mop",{"dust_emptying":True,"mop_washing":True})]:
            self.assertEqual(plan(mode).dock_cleanup["required"],expected)

    def test_cancel_preserves_plan_and_used_modes(self):
        q=plan(); stages=deepcopy(q.stages)
        q.command("cancel",snap(120,"segment_cleaning",job="on"),120)
        self.assertEqual(q.phase,"cancelled")
        self.assertEqual(q.stages,stages)
        self.assertTrue(dock.required(q))
        self.assertIsNone(q.observe(snap(121,"segment_cleaning",job="on"),121))
        self.assertEqual(q.phase,"cancelled")
        q.command("return_to_dock",snap(122,"segment_cleaning",job="on"),122)
        self.assertEqual(q.mode,"finish")
        self.assertEqual(q.run_id,"run")
        self.assertEqual(q.stages,stages)

    def test_missing_care_is_dispatched_once_and_ack_is_not_completion(self):
        q=finished("vacuum")
        self.assertIsNone(q.observe(snap(130),130))
        self.assertEqual(q.observe(snap(146),146),("dock","dust_emptying"))
        self.assertEqual(q.dock_cleanup["actions"]["dust_emptying"]["state"],"commanded")
        self.assertIsNone(q.observe(snap(147),147))
        self.assertNotEqual(q.phase,"cancelled")
        native_care(q,"dust_emptying",150)
        self.assertEqual(q.phase,"cancelled")
        self.assertEqual(q.dock_cleanup["state"],"completed")

    def test_native_care_is_adopted_without_duplicates(self):
        q=finished()
        native_care(q,"dust_emptying",130)
        # Native wash may already start before our next command; observe it once.
        native_care(q,"mop_washing",153)
        self.assertEqual(q.dock_cleanup["state"],"completed")
        self.assertEqual(q.phase,"cancelled")

    def test_stale_switch_on_cannot_prove_native_care(self):
        q=finished("vacuum")
        q.observe(snap(130,controls={"dust_emptying":"on","mop_washing":"off"}),130)
        self.assertEqual(q.dock_cleanup["actions"]["dust_emptying"]["state"],"pending")

    def test_timeout_never_replays_unknown_request_and_retry_is_revision_bound(self):
        q=finished("vacuum")
        q.observe(snap(130),130);q.observe(snap(146),146)
        q.observe(snap(207),207)
        self.assertEqual(q.phase,"attention")
        self.assertTrue(q.dock_cleanup["retry_allowed"])
        before=q.dump()
        self.assertIsNone(q.observe(snap(300),300))
        self.assertEqual(q.dump(),before)
        with self.assertRaises(ValueError):dock.resume(q,snap(300),300,"run",-1)
        self.assertIsNone(dock.resume(q,snap(300),300,"run",q.dock_cleanup["revision"]))
        self.assertEqual(q.observe(snap(316),316),("dock","dust_emptying"))

    def test_restart_unknown_command_is_observed_not_resent(self):
        q=finished("vacuum");q.observe(snap(130),130);q.observe(snap(146),146)
        q=Queue.restore(q.dump())
        self.assertIsNone(q.observe(snap(150),150))
        self.assertEqual(q.pending_command,"dock_care")
        native_care(q,"dust_emptying",151)
        self.assertEqual(q.dock_cleanup["state"],"completed")

    def test_restart_gap_cannot_prove_care_completed(self):
        q=finished("mop");q.observe(snap(130,"washing_the_mop"),130)
        q=Queue.restore(q.dump())
        q.observe(snap(150),150);q.observe(snap(166),166)
        self.assertEqual(q.phase,"attention")
        self.assertIn("offline",q.error)
        self.assertNotEqual(q.dock_cleanup["actions"]["mop_washing"]["state"],"completed")

    def test_restart_active_care_can_be_reobserved_then_completed(self):
        q=finished("mop");q.observe(snap(130,"washing_the_mop"),130)
        q=Queue.restore(q.dump());native_care(q,"mop_washing",150)
        self.assertEqual(q.dock_cleanup["state"],"completed")

    def test_late_record_before_observed_wash_preserves_evidence(self):
        q=plan("mop");q.observe(snap(130,"washing_the_mop"),130)
        dock.used(q,"mop",125)
        self.assertEqual(q.dock_cleanup["actions"]["mop_washing"]["started_at"],130)
        dock.used(q,"mop",140)
        self.assertEqual(q.dock_cleanup["actions"]["mop_washing"]["state"],"pending")

    def test_delayed_floor_start_after_cancel_is_tracked(self):
        q=Queue(mode="manual",phase="starting",vacuum="vacuum.robot",run_id="run",stages=[{"mode":"mop"}],
                started_at=100,pending_command="start",command_at=100)
        q.finish(q.vacuum,snap(110,"washing_the_mop",job="on"),110,"unused")
        q.observe(snap(120,"segment_cleaning",job="on"),120)
        self.assertTrue(q.dock_cleanup["required"]["mop_washing"])
        self.assertFalse(q.dock_cleanup["required"]["dust_emptying"])

    def test_empty_bin_can_run_before_water_fault_blocks_required_washing(self):
        q=finished()
        q.observe(snap(130,fault="water_empty"),130)
        self.assertEqual(q.observe(snap(146,fault="water_empty"),146),("dock","dust_emptying"))
        q.observe(snap(150,"emptying_the_bin",fault="water_empty"),150)
        q.observe(snap(155,fault="water_empty"),155)
        q.observe(snap(171,fault="water_empty"),171)
        self.assertEqual(q.phase,"attention")
        self.assertEqual(q.dock_cleanup["actions"]["dust_emptying"]["state"],"completed")
        self.assertEqual(q.dock_cleanup["actions"]["mop_washing"]["state"],"pending")

    def test_unsupported_control_is_reported_not_skipped(self):
        q=finished("mop");q.observe(snap(130,controls={}),130);q.observe(snap(146,controls={}),146)
        self.assertEqual(q.phase,"attention")
        self.assertIn("unavailable",q.error)

    def test_normal_completion_uses_same_cleanup_contract(self):
        q=plan("mop");q.completed=q.current_index=1;q.phase="finishing";q.dock_finish_at=120
        q.observe(snap(130),130)
        self.assertEqual(q.observe(snap(146),146),("dock","mop_washing"))
        native_care(q,"mop_washing",150)
        self.assertEqual(q.phase,"completed")

    def test_cancel_and_new_start_cannot_erase_armed_care(self):
        q=finished("mop")
        before=q.dump()
        with self.assertRaises(ValueError):q.command("cancel",snap(130),130)
        with self.assertRaises(ValueError):q.start_manual(q.vacuum,["0_2"],{},[{"mode":"vacuum"}],{},snap(130),130,"new")
        self.assertEqual(q.dump(),before)

    def test_generic_continue_cannot_credit_manually_stopped_dock_care(self):
        q=plan("mop");q.completed=q.current_index=1;q.phase="finishing";q.dock_finish_at=120
        q.observe(snap(130,"washing_the_mop"),130)
        q.attention("Dock care stop requested by the user.")
        before=q.dump()
        with self.assertRaisesRegex(ValueError,"Resume dock care"):
            load("recovery").continue_sequence(q,snap(150),150)
        q.observe(snap(170),170)
        self.assertEqual(q.dump(),before)

    def test_unrelated_job_after_cancel_does_not_change_used_modes(self):
        q=plan("vacuum");q.command("cancel",snap(120),120)
        before=deepcopy(q.dock_cleanup)
        q.stage["mode"]="mop"
        q.observe(snap(400,"segment_cleaning",job="on"),400)
        self.assertEqual(q.dock_cleanup,before)

    def test_unarmed_interrupted_care_is_never_credited_as_success(self):
        q=plan("vacuum");q.observe(snap(120,"emptying_the_bin"),120)
        self.assertEqual(q.dock_cleanup["actions"]["dust_emptying"]["state"],"running")
        q.attention("Dock care interrupted")
        dock.used(q,"mop",200)
        dock.arm(q,300,"completed")
        self.assertEqual(q.dock_cleanup["actions"]["dust_emptying"]["state"],"pending")
        dock.observe(q,snap(310),310,dispatch=True)
        self.assertEqual(dock.observe(q,snap(330),330,dispatch=True),("dock","dust_emptying"))

    def test_malformed_checkpoint_retains_plan_and_stops_for_review(self):
        for corrupt in [lambda cp:cp["actions"].update(extra={}),
                        lambda cp:cp.update(begun_at="invalid"),
                        lambda cp:cp["actions"].update(mop_washing=[]),
                        lambda cp:cp.update(required=[])]:
            q=finished("mop");stages=deepcopy(q.stages);corrupt(q.dock_cleanup)
            restored=Queue.restore(q.dump())
            self.assertEqual(restored.phase,"attention")
            self.assertEqual(restored.stages,stages)
            self.assertFalse(restored.dock_cleanup["retry_allowed"])
            self.assertIsNone(restored.observe(snap(400),400))

    def test_explicit_care_retry_can_recover_failed_return_without_floor_start(self):
        q=finished("mop")
        fault=snap(130,"returning_home");fault.error="error"
        q.observe(fault,130)
        self.assertEqual(q.phase,"attention")
        repaired=Snapshot("idle","idle","off","none","ok",True,observed_at=200,
                          dock_controls={"dust_emptying":"off","mop_washing":"off"})
        effect=dock.resume(q,repaired,200,q.run_id,q.dock_cleanup["revision"])
        self.assertEqual(effect,("vacuum","return_to_base"))
        self.assertEqual(q.mode,"finish")
        self.assertEqual(q.completed,0)
        self.assertEqual(q.stages[0]["mode"],"mop")
        q.observe(snap(201),201)
        q.observe(snap(202),202)
        self.assertEqual(q.observe(snap(218),218),("dock","mop_washing"))


class ManagerDockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await device_tests.DeviceTests.asyncSetUp(self)
        self.coordinator.data.status.dock_error_status=0
        self.manager.queue=plan("vacuum_mop")
        self.manager.queue.owner_user_id="user"
        self.manager.queue.started_at=self.clock-10
        self.manager.queue.dock_cleanup={}
        dock.used(self.manager.queue,"vacuum_mop",self.clock-5)
        self.manager.queue.finish("vacuum.robot",snap(self.clock,"segment_cleaning",job="on"),self.clock,"unused")
        self.manager.queue.confirmed()
        async def call(domain,service,data,**kwargs):
            self.assertTrue(self.manager.lock.locked())
            self.assertEqual(self.manager.store.saved[-1],self.manager.queue.dump())
            self.assertEqual(kwargs["context"].user_id,"user")
            self.calls.append((domain,service,data))
            if getattr(self,"inject_dock_failure",False):raise RuntimeError("SECRET")
        self.hass.services.async_call=call

    async def advance(self, seconds=16,status="charging",job=0):
        self.clock+=seconds
        self.coordinator.data.status.state_name=status
        self.coordinator.data.status.in_cleaning=job
        self.coordinator._last_update_success_time=datetime.fromtimestamp(self.clock,timezone.utc)
        await self.manager.tick()

    async def test_reservation_persisted_and_authorized_before_dock_service(self):
        await self.advance(1);await self.advance()
        self.assertEqual(self.calls,[("switch","turn_on",{"entity_id":"switch.robot_dust_emptying"})])
        await self.advance(1)
        self.assertEqual(len(self.calls),1)
        await self.advance(1,"emptying_the_bin")
        self.assertEqual(self.manager.queue.dock_cleanup["actions"]["dust_emptying"]["state"],"running")
        await self.advance(1);await self.advance()
        self.assertEqual(self.calls[-1],("switch","turn_on",{"entity_id":"switch.robot_mop_washing"}))

    async def test_permission_revoked_before_dock_dispatch_prevents_command(self):
        await self.advance(1)
        self.allowed.remove("switch.robot_dust_emptying")
        await self.advance()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.manager.queue.phase,"attention")

    async def test_native_service_failure_keeps_checkpoint_without_retry(self):
        self.inject_dock_failure=True
        await self.advance(1);await self.advance();await self.advance(100)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.manager.queue.phase,"attention")
        self.assertNotIn("SECRET",self.manager.queue.error)
        self.assertTrue(self.manager.queue.dock_cleanup["retry_allowed"])

    async def test_user_stop_remains_available_during_owned_care(self):
        await self.advance(1);await self.advance();await self.advance(1,"emptying_the_bin")
        self.states["switch.robot_dust_emptying"].state="on"
        before=deepcopy(self.manager.queue.stages)
        await self.manager.device_control(NS(context=m.FakeContext("user"),data={"vacuum":"vacuum.robot","control":"dust_emptying","value":"off"}))
        self.assertEqual(self.calls[-1],("switch","turn_off",{"entity_id":"switch.robot_dust_emptying"}))
        self.assertEqual(self.manager.queue.phase,"attention")
        self.assertEqual(self.manager.queue.stages,before)
        self.assertEqual(self.manager.queue.run_id,"run")
        self.assertTrue(dock.required(self.manager.queue))

    async def test_capability_keeps_unavailable_native_control_visible(self):
        self.states["switch.robot_mop_washing"].state="unavailable"
        current=self.manager.current_snapshot("vacuum.robot")
        info=self.manager.dock_controls("vacuum.robot",current)["mop_washing"]
        self.assertTrue(info["supported"])
        self.assertFalse(info["available"])
        self.assertFalse(info["can_start"])

    async def test_device_request_restart_does_not_intercept_future_plan_ticks(self):
        q=Queue(mode="manual",phase="completed",vacuum="vacuum.robot",run_id="past",
                stages=[{"mode":"vacuum","target":"0_1"}],current_index=1,completed=1)
        q.device_request={"control":"mop_drying","entity_id":"switch.robot_mop_drying","value":"on", "domain":"switch", "previous_phase":"completed"}
        q.phase,q.pending_command,q.command_at="controlling","device",self.clock
        restored=Queue.restore(q.dump())
        self.assertEqual(restored.phase,"attention")
        self.assertFalse(restored.device_request)
        self.assertEqual(restored.stages,q.stages)
        self.assertGreater(restored.not_before,self.clock)

    async def test_care_retry_rechecks_revision_under_manager_lock(self):
        await self.advance(1);await self.advance();await self.advance(61)
        cp=self.manager.queue.dock_cleanup
        self.assertEqual(cp["state"],"attention")
        calls=len(self.calls);before=self.manager.queue.dump()
        data={"vacuum":"vacuum.robot","command":"resume_dock_cleanup","run_id":"run","cleanup_revision":cp["revision"]-1}
        with self.assertRaisesRegex(m.ServiceError,"changed"):
            await self.manager.control(NS(context=m.FakeContext("user"),data=data))
        self.assertEqual(self.manager.queue.dump(),before)
        self.assertEqual(len(self.calls),calls)
        data["cleanup_revision"]=cp["revision"]
        await self.manager.control(NS(context=m.FakeContext("user"),data=data))
        self.assertEqual(len(self.calls),calls)
        await self.advance()
        self.assertEqual(len(self.calls),calls+1)

    async def test_native_state_race_before_dispatch_never_starts_care(self):
        await self.advance(1)
        original=self.hass.auth.async_get_user
        async def race(user_id):
            user=await original(user_id)
            self.coordinator.data.status.state_name="segment_cleaning"
            self.coordinator.data.status.in_cleaning=1
            self.states["vacuum.robot"].state="cleaning"
            return user
        self.hass.auth.async_get_user=race
        await self.advance()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.manager.queue.phase,"attention")
        self.assertTrue(dock.required(self.manager.queue))


if __name__=="__main__":unittest.main(verbosity=2)
