"""Offline fault policy, stop and native device-control regression traces."""
import asyncio
from datetime import datetime, timezone, timedelta
import sys
from pathlib import Path
import time
import unittest
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).parent))
import backend_manual_test as m
from backend_queue_test import Queue, Snapshot, ready, cleaning, record, adapter


def stage(target, index=0, mode="vacuum"):
    return {"target": target, "mode": mode, "room_index": index, "pass_index": 0, "repeat_index": 0,
            "settings": {"mode": mode}, "map_id": 0, "segments": [str(index + 1)]}


class PolicyTests(unittest.TestCase):
    def test_water_fault_retains_identity_and_unknown_fault_does_not_gain_exception(self):
        _, coordinator, *_ = m.fixture()
        for code, expected in [(38,"water_empty"),(39,"error"),(999,"error"),(0,"ok")]:
            coordinator.data.status.dock_error_status=code
            self.assertEqual(adapter.snapshot(coordinator,"docked").dock_error,expected)

    def test_water_exception_allows_vacuum_only_work(self):
        for fault in ["water_empty","error","waste_water_tank_full","unknown"]:
            for mode in ["vacuum","mop","vacuum_mop","vacuum_then_mop"]:
                current=ready(); current.dock_error=fault
                self.assertEqual(current.ready_for(mode),fault=="water_empty" and mode == "vacuum")
                current.error="error"
                self.assertFalse(current.ready_for(mode))

    def test_water_empty_vacuum_continues_only_after_matching_completion(self):
        q=Queue(); current=ready(); current.dock_error="water_empty"
        stages=[{"target":x,"mode":"vacuum","settings":{"mode":"vacuum"}} for x in ["office","kitchen"]]
        q.start_manual("vacuum.robot",["office","kitchen"],{"mode":"vacuum"},stages,{},current,100,"run")
        current.settings={"mode":"vacuum"};current.observed_at=101
        self.assertEqual(q.observe(current,101),("manual","0"))
        active=cleaning();active.dock_error="water_empty";active.observed_at=105
        q.observe(active,105)
        current.record=record(101,200)
        q.observe(current,201)
        self.assertEqual(q.completed,1)
        self.assertEqual(q.observe(current,202),("configure","1"))
        current.dock_error="error"
        self.assertIsNone(q.observe(current,203));self.assertEqual(q.phase,"attention")

    def test_stop_clears_future_stages_and_requires_fresh_ack(self):
        q=Queue(mode="manual",phase="running",vacuum="vacuum.robot",targets=["0_1","0_2"],
                stages=[stage("0_1",0),stage("0_2",1)])
        active=cleaning();active.dock_error="water_empty"
        self.assertEqual(q.command("stop",active,100),("vacuum","stop"))
        stale=ready();stale.observed_at=99
        q.observe(stale,102);self.assertEqual(q.pending_command,"stop")
        stale.observed_at=103;q.observe(stale,103)
        self.assertEqual((q.phase,q.pending_command,q.completed),("cancelled","",0))
        self.assertIsNone(q.observe(stale,200))

    def test_stop_does_not_replace_uncertain_command(self):
        q=Queue(phase="starting",pending_command="start",command_at=100)
        with self.assertRaises(ValueError): q.command("stop",cleaning(),110)
        self.assertEqual(q.pending_command,"start")


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await m.ManagerTraceTests.asyncSetUp(self)
        for key,(prefix,domain) in {**m.device.CONTROLS,**m.device.MAINTENANCE}.items():
            eid=f"{domain}.robot_{key}"
            self.entries.append(NS(entity_id=eid,unique_id=prefix+"_robot1",device_id="dock",config_entry_id="entry",platform="roborock",domain=domain,disabled_by=None))
            self.states[eid]=NS(state="off" if domain=="switch" else "50" if domain=="number" else "100" if domain=="sensor" else "22:00:00" if domain=="time" else "max",attributes={"min":0,"max":100,"options":["smart","max"]},last_updated=datetime.now(timezone.utc))
            self.allowed.add(eid)
        self.registry.entities={e.entity_id:e for e in self.entries}
        self.states["vacuum.robot"].attributes["supported_features"]=512
        self.coordinator.data.status.dock_error_status=38
        async def call(domain,service,data,**kwargs):
            self.assertTrue(self.manager.lock.locked())
            self.calls.append((domain,service,data))
            if service=="locate": return
            self.assertEqual(self.manager.store.saved[-1]["pending_command"],"device")
            state=self.states[data["entity_id"]]
            if getattr(self,"inject_failure",False): raise RuntimeError("Secret payload")
            if not getattr(self,"stale",False):
                state.state="on" if service=="turn_on" else "off" if service=="turn_off" else str(data.get("value",data.get("time",data.get("option"))))
                state.last_updated=datetime.now(timezone.utc)
        self.hass.services.async_call=call

    async def send(self,key,value=""):
        await self.manager.device_control(NS(context=m.FakeContext("user"),data={"vacuum":"vacuum.robot","control":key,"value":value}))

    async def test_native_discovery_uses_identity_not_name_or_dock_device_id(self):
        found=self.manager.device_entities("vacuum.robot")
        self.assertEqual(found["mop_washing"],"switch.robot_mop_washing")
        e=next(e for e in self.entries if e.entity_id==found["mop_washing"])
        e.disabled_by="integration"
        self.assertNotIn("mop_washing",self.manager.device_entities("vacuum.robot"))
        e.disabled_by=None;e.unique_id="mop_washing_other_robot"
        self.assertNotIn("mop_washing",self.manager.device_entities("vacuum.robot"))

    async def test_named_map_requires_same_robot_device_and_entry(self):
        image=NS(entity_id="image.renamed_map",unique_id="robot1_map_Ground floor",device_id="device",config_entry_id="entry",platform="roborock",domain="image",disabled_by=None)
        self.registry.entities[image.entity_id]=image
        self.states[image.entity_id]=NS(state="2026-01-01",attributes={})
        self.assertIn(image.entity_id,self.manager.device_entities("vacuum.robot").values())
        image.device_id="different"
        self.assertNotIn(image.entity_id,self.manager.device_entities("vacuum.robot").values())
        image.device_id="device";image.config_entry_id="other"
        self.assertNotIn(image.entity_id,self.manager.device_entities("vacuum.robot").values())

    async def test_empty_water_allows_emptying_and_drying_but_not_washing(self):
        with self.assertRaises(m.ServiceError): await self.send("mop_washing","on")
        self.assertEqual(self.calls,[])
        for key in ["dust_emptying","mop_drying"]:
            await self.send(key,"on")
            self.assertEqual(self.manager.queue.phase,"controlling")
            await self.manager.tick()
            self.assertEqual(self.manager.queue.phase,"idle")
        self.assertEqual(len(self.calls),2)

    async def test_stop_washing_available_with_water_fault(self):
        self.states["switch.robot_mop_washing"].state="on"
        self.coordinator.data.status.state_name="washing_the_mop"
        await self.send("mop_washing","off")
        self.assertEqual(self.calls[-1][1],"turn_off")

    async def test_ongoing_queue_rejects_dock_without_mutation(self):
        self.manager.queue=Queue(phase="running",vacuum="vacuum.robot")
        before=self.manager.queue.dump()
        with self.assertRaises(m.ServiceError): await self.send("mop_drying","on")
        self.assertEqual(self.manager.queue.dump(),before);self.assertEqual(self.calls,[])

    async def test_concurrent_setting_cannot_interrupt_existing_reservation(self):
        self.stale=True
        await self.send("volume",65)
        before=self.manager.queue.dump()
        with self.assertRaises(m.ServiceError): await self.send("volume",70)
        self.assertEqual(self.manager.queue.dump(),before);self.assertEqual(len(self.calls),1)
        self.manager.queue.command_at=time.time()-61
        await self.manager.tick()
        self.assertEqual(self.manager.queue.phase,"attention")
        self.assertGreater(self.manager.queue.not_before,0)

    async def test_invalid_values_and_unknown_controls_never_dispatch(self):
        for key,value in [("volume",-1),("volume",101),("volume","nan"),("dnd_start","25:30"),("empty_mode","unknown"),("mop_drying","toggle"),("arbitrary","on")]:
            with self.assertRaises(m.ServiceError): await self.send(key,value)
        self.assertEqual(self.calls,[])

    async def test_settings_fresh_readback_and_find_without_queue_adoption(self):
        for key,value,service in [("volume",60,"set_value"),("dnd","on","turn_on"),("dnd_start","21:30","set_value"),("empty_mode","smart","select_option")]:
            await self.send(key,value);self.assertEqual(self.calls[-1][1],service)
            await self.manager.tick();self.assertEqual(self.manager.queue.phase,"idle")
        before=self.manager.queue.dump()
        await self.send("locate")
        self.assertEqual(self.calls[-1][1],"locate");self.assertEqual(self.manager.queue.dump(),before)

    async def test_failure_does_not_retry_or_leak_error(self):
        self.inject_failure=True
        with self.assertRaisesRegex(m.ServiceError,"device command failed"): await self.send("volume",60)
        self.assertEqual(self.manager.queue.phase,"attention")
        self.assertNotIn("Secret",self.manager.queue.error)
        await self.manager.tick();self.assertEqual(len(self.calls),1)

    async def test_permission_denial_never_dispatches(self):
        self.allowed.remove("switch.robot_mop_drying")
        class Denied(Exception):
            def __init__(self,**kwargs): pass
        self.manager.device_control.__func__.__globals__["Unauthorized"]=Denied
        with self.assertRaises(Denied): await self.send("mop_drying","on")
        self.assertEqual(self.calls,[])


class WaterManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_vacuum_water_exception_covers_settings_and_final_dispatch(self):
        await m.ManagerTraceTests.asyncSetUp(self)
        self.coordinator.data.status.dock_error_status=38
        await m.ManagerTraceTests.start(self,setup={"mode":"vacuum","suction":"max"})
        await self.manager.tick()
        self.assertEqual(self.calls[-1][1],"clean_area")
        self.assertEqual(self.calls[0][2]["option"],"vacuum")
        self.assertFalse(any(domain=="button" for domain,_,_ in self.calls))

    async def test_mopping_and_mixed_plans_never_write_settings_when_water_empty(self):
        for mode in ["mop","vacuum_mop","vacuum_then_mop"]:
            await m.ManagerTraceTests.asyncSetUp(self)
            self.coordinator.data.status.dock_error_status=38
            with self.assertRaises(m.ServiceError): await m.ManagerTraceTests.start(self,setup={"mode":mode})
            self.assertEqual(self.calls,[])


if __name__=="__main__": unittest.main(verbosity=2)
