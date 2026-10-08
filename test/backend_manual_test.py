"""Offline manual-plan and HA-manager traces; no network or robot commands."""
import __future__
import ast
import asyncio
from collections import deque
from datetime import datetime, timezone
import logging
from pathlib import Path
import sys
import time
import types
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))
from backend_queue_test import load, Queue, Snapshot, ready, cleaning, record, adapter, permissions
manual = load("manual")
device = load("device")
NS = types.SimpleNamespace


def fixture():
    vacuum = NS(entity_id="vacuum.robot", unique_id="robot1", device_id="device", config_entry_id="entry", platform="roborock", domain="vacuum", disabled_by=None,
                options={"vacuum": {"area_mapping": {"kitchen": ["0_1", "0_2"], "office": ["0_3"], "upstairs": ["1_1"], "stale": ["0_99"]}}})
    entries = [vacuum]
    states = {vacuum.entity_id: NS(state="docked", attributes={"fan_speed_list": ["quiet", "balanced", "max", "off", "smart_mode", "custom"],
                                                              "supported_features": manual.AREA_FEATURE | 8192})}
    option_sets = {"mode": ["vacuum", "vac_and_mop", "mop", "smart_mode", "custom"],
                   "water": ["off", "low", "medium", "high", "custom_water_flow", "smart_mode", "custom"],
                   "route": ["standard", "deep", "deep_plus", "fast", "smart_mode", "custom"]}
    for key, native in manual.SELECT_KEYS.items():
        eid = "select.renamed_" + key
        entries.append(NS(entity_id=eid, unique_id=native+"_robot1", device_id="device", config_entry_id="entry", platform="roborock", domain="select", disabled_by=None))
        states[eid] = NS(state={"mode": "vac_and_mop", "water": "custom_water_flow", "route": "standard"}[key], attributes={"options": option_sets[key]})
    enum = lambda name: NS(value=name, display_name=name)
    trait = NS(cleaning_mode_options=list(map(enum, option_sets["mode"])), fan_speed_options=list(map(enum, states[vacuum.entity_id].attributes["fan_speed_list"])),
               water_mode_options=list(map(enum, option_sets["water"])), mop_route_options=list(map(enum, option_sets["route"])),
               current_cleaning_mode_name="vac_and_mop", fan_speed_name="max", water_mode_name="custom_water_flow", mop_route_name="standard",
               state_name="charging", in_cleaning=0, error_code=0, dock_error_status=0)
    segments = []
    async def clean_segments(requested):
        segments.append(list(requested))
    coordinator = NS(duid_slug="robot1", api=NS(vacuum=NS(clean_segments=clean_segments, segments=segments)),
                     last_update_success=True, _last_update_success_time=datetime.now(timezone.utc),
                     properties_api=NS(status=trait, maps=NS(current_map=0), home=NS(current_map_data=NS(map_flag=0, rooms=[NS(segment_id=n) for n in [1, 2, 3]]))),
                     data=NS(status=trait, clean_summary=NS(last_clean_record=None)), async_add_listener=lambda callback: lambda: None)
    areas = NS(async_get_area=lambda area_id: NS(name=area_id.title()))
    registry = NS(entities={e.entity_id: e for e in entries}, async_get=lambda eid: next((e for e in entries if e.entity_id == eid), None))
    return vacuum, coordinator, entries, states, areas, registry


class ManualPlanTests(unittest.TestCase):
    def setUp(self):
        self.vacuum, self.coordinator, self.entries, self.states, self.areas, _ = fixture()
        self.caps, self.controls, self.targets = manual.capabilities(self.vacuum, self.coordinator, self.entries, self.states, self.areas)

    def build(self, mode="vacuum_then_mop", rooms=None, **kwargs):
        return manual.build_plan(["kitchen", "office"] if rooms is None else rooms, {"mode": mode, **kwargs}, self.caps, self.targets, 0)

    def test_capabilities_only_native_options_and_current_map_areas(self):
        self.assertTrue(self.caps["supported"])
        self.assertEqual([t["id"] for t in self.caps["room_targets"]], ["kitchen", "office"])
        self.assertEqual(self.controls["mode"], "select.renamed_mode")
        self.assertEqual(self.caps["water"], ["low", "medium", "high"])
        self.assertEqual(self.caps["defaults"]["water"], "medium")
        self.assertEqual(self.caps["suction"], ["quiet", "balanced", "max"])
        self.assertEqual(self.caps["routes_by_mode"]["vacuum_mop"], ["standard", "fast"])
        self.assertNotIn("segments", str(self.caps["room_targets"]))

    def test_area_icons_are_optional_metadata_without_changing_cleaning_targets(self):
        self.areas.async_get_area = lambda area_id: NS(name=area_id.title(), icon="mdi:chair-rolling" if area_id == "office" else None)
        caps, _, targets = manual.capabilities(self.vacuum, self.coordinator, self.entries, self.states, self.areas)
        self.assertNotIn("icon", caps["room_targets"][0])
        self.assertEqual(caps["room_targets"][1]["icon"], "mdi:chair-rolling")
        self.assertEqual(targets["office"]["segments"], ["0_3"])

    def test_missing_wrong_device_disabled_select_cannot_provide_modes(self):
        for mutate in [lambda entry: setattr(entry, "disabled_by", "user"), lambda entry: setattr(entry, "device_id", "other"), lambda entry: setattr(entry, "unique_id", "unrelated")]:
            vacuum, coordinator, entries, states, areas, _ = fixture()
            mutate(entries[1])
            caps, _, _ = manual.capabilities(vacuum, coordinator, entries, states, areas)
            self.assertFalse(caps["supported"])
        self.coordinator.properties_api.maps.current_map = None
        self.assertFalse(manual.capabilities(self.vacuum, self.coordinator, self.entries, self.states, self.areas)[0]["supported"])

    def test_two_pass_order_repeats_and_mode_specific_settings(self):
        setup, stages = self.build(repeat=2, suction="max", water="high", route="deep")
        self.assertEqual([(s["target"], s["mode"], s["repeat_index"]) for s in stages],
                         [(r, m, n) for m in ["vacuum", "mop"] for r in ["kitchen", "office"] for n in [0, 1]])
        self.assertEqual(stages[0]["settings"], {"mode": "vacuum", "suction": "max"})
        self.assertEqual(stages[4]["settings"], {"mode": "mop", "water": "high", "route": "deep"})
        self.assertEqual(stages[0]["segments"], ["0_1", "0_2"])
        self.assertEqual(setup["repeat"], 2)

    def test_whole_home_and_parameter_validation(self):
        self.assertEqual(len(self.build(rooms=[], repeat=2)[1]), 4)
        invalid = [{"mode": "smart_mode"}, {"mode": "vacuum", "water": "high"}, {"mode": "mop", "suction": "max"},
                   {"mode": "vacuum_mop", "route": "deep"}, {"mode": "mop", "water": "custom_water_flow"},
                   {"mode": "vacuum", "suction": "off"}, {"mode": "vacuum", "repeat": True}, {"mode": "vacuum", "repeat": 3},
                   {"mode": "vacuum", "raw_command": "anything"}]
        for setup in invalid:
            with self.subTest(setup=setup), self.assertRaises(ValueError):
                manual.build_plan([], setup, self.caps, self.targets, 0)
        for rooms in [["upstairs"], ["stale"], ["kitchen", "kitchen"], ["missing"]]:
            with self.assertRaises(ValueError):
                self.build(rooms=rooms)

    def test_overlap_and_changed_mapping_or_options_fail_closed(self):
        self.targets["overlap"] = {"segments": ["0_1"]}
        with self.assertRaises(ValueError):
            self.build(rooms=["kitchen", "overlap"])
        _, stages = self.build()
        stage = stages[0]
        manual.validate_stage(stage, self.caps, self.targets, 0)
        for map_id in [1, None]:
            with self.assertRaises(ValueError):
                manual.validate_stage(stage, self.caps, self.targets, map_id)
        self.targets["kitchen"]["segments"] = ["0_1"]
        with self.assertRaises(ValueError):
            manual.validate_stage(stage, self.caps, self.targets, 0)

    def test_area_tiles_require_the_native_area_feature(self):
        """A robot without area cleaning keeps whole-home cleaning but offers no tiles."""
        self.states[self.vacuum.entity_id].attributes["supported_features"] = 8192
        caps = manual.capabilities(self.vacuum, self.coordinator, self.entries, self.states, self.areas)[0]
        self.assertTrue(caps["supported"])
        self.assertFalse(caps["area_cleaning"])
        self.assertEqual(caps["room_targets"], [])

    def test_native_only_vacuum_does_not_offer_synthetic_mopping(self):
        self.states["select.renamed_mode"].attributes["options"] = ["vacuum"]
        caps = manual.capabilities(self.vacuum, self.coordinator, self.entries, self.states, self.areas)[0]
        self.assertEqual([m["value"] for m in caps["modes"]], ["vacuum"])


def room(name=None, segment=1):
    return NS(segment_id=segment, name=name)


class RoomReportTests(unittest.TestCase):
    """The report is read-only: it explains the mapping and never writes configuration."""

    def fixture(self):
        vacuum, coordinator, entries, states, areas, _ = fixture()
        coordinator.properties_api.home = NS(
            current_map_data=NS(map_flag=0, rooms=[room(segment=n) for n in [1, 2, 3]]),
            home_map_info={
                0: NS(map_flag=0, name="Ground floor", rooms=[room("Kitchen", 1), room("Dining area", 2), room("Office", 3)]),
                1: NS(map_flag=1, name="Upstairs", rooms=[room("Bedroom", 1), room(None, 2)]),
            })
        return vacuum, coordinator, entries, states, areas

    def report(self):
        vacuum, coordinator, entries, states, areas = self.fixture()
        return manual.capabilities(vacuum, coordinator, entries, states, areas)[0]

    def test_app_names_are_reported_against_the_areas_that_claim_them(self):
        caps = self.report()
        self.assertTrue(caps["rooms_complete"])
        self.assertEqual([m["name"] for m in caps["robot_maps"]], ["Ground floor", "Upstairs"])
        rooms = {(r["floor"], r["name"]): (r["id"], r["area_id"], r["area_name"]) for r in caps["robot_rooms"]}
        # One Home Assistant area can cover several finer Roborock rooms.
        self.assertEqual(rooms[("Ground floor", "Kitchen")], ("0_1", "kitchen", "Kitchen"))
        self.assertEqual(rooms[("Ground floor", "Dining area")], ("0_2", "kitchen", "Kitchen"))
        self.assertEqual(rooms[("Ground floor", "Office")], ("0_3", "office", "Office"))
        self.assertEqual(rooms[("Upstairs", "Bedroom")], ("1_1", "upstairs", "Upstairs"))

    def test_robot_room_without_an_area_is_reported_unassigned(self):
        caps = self.report()
        unassigned = [r for r in caps["robot_rooms"] if r["area_id"] is None]
        self.assertEqual([(r["id"], r["name"]) for r in unassigned], [("1_2", None)])

    def test_area_pointing_at_a_room_the_robot_dropped_is_flagged(self):
        caps = self.report()
        self.assertEqual([a["id"] for a in caps["unmapped_areas"]], ["stale"])
        self.assertEqual(caps["unmapped_areas"][0]["segments"], ["0_99"])

    def test_area_removed_from_the_registry_is_not_claimed_as_coverage(self):
        vacuum, coordinator, entries, states, _ = self.fixture()
        missing = NS(async_get_area=lambda area_id: None if area_id == "kitchen" else NS(name=area_id.title()))
        caps = manual.capabilities(vacuum, coordinator, entries, states, missing)[0]
        kitchen = [r for r in caps["robot_rooms"] if r["id"] in {"0_1", "0_2"}]
        self.assertEqual([r["area_id"] for r in kitchen], [None, None])

    def test_single_map_cache_reports_rooms_but_never_calls_an_area_stale(self):
        vacuum, coordinator, entries, states, areas, _ = fixture()
        caps = manual.capabilities(vacuum, coordinator, entries, states, areas)[0]
        self.assertFalse(caps["rooms_complete"])
        self.assertEqual([r["id"] for r in caps["robot_rooms"]], ["0_1", "0_2", "0_3"])
        self.assertEqual([r["name"] for r in caps["robot_rooms"]], [None, None, None])
        self.assertEqual([r["area_id"] for r in caps["robot_rooms"]], ["kitchen", "kitchen", "office"])
        self.assertEqual(caps["unmapped_areas"], [])

    def test_room_plan_freezes_per_room_settings_and_order(self):
        vacuum, coordinator, entries, states, areas = self.fixture()
        caps, _, _ = manual.capabilities(vacuum, coordinator, entries, states, areas)
        targets = manual.robot_targets(coordinator, 0)
        self.assertEqual(sorted(targets), ["0_1", "0_2", "0_3"])
        self.assertEqual(targets["0_2"]["name"], "Dining area")
        rooms, stages = manual.build_room_plan(
            [{"id": "0_2", "mode": "mop", "water": "low", "route": "deep"},
             {"id": "0_1", "mode": "vacuum", "suction": "max"}],
            {"repeat": 1}, caps, targets, 0)
        self.assertEqual([room["id"] for room in rooms], ["0_2", "0_1"])
        self.assertEqual(rooms[0]["setup"]["mode"], "mop")
        self.assertEqual(rooms[1]["setup"]["mode"], "vacuum")
        self.assertEqual([stage["target"] for stage in stages], ["0_2", "0_1"])
        self.assertEqual(stages[0]["settings"]["mode"], "mop")
        self.assertNotIn("suction", stages[0]["settings"])          # mop needs no suction
        self.assertEqual(stages[1]["settings"], {"mode": "vacuum", "suction": "max"})
        self.assertEqual(stages[0]["segments"], ["2"])              # the robot's own segment

    def test_mixed_room_defaults_and_repeats_keep_room_indices(self):
        vacuum, coordinator, entries, states, areas = self.fixture()
        caps, _, _ = manual.capabilities(vacuum, coordinator, entries, states, areas)
        targets = manual.robot_targets(coordinator, 0)
        defaults = {"mode":"vacuum_mop", "suction":"max", "water":"high", "route":"standard", "repeat":2}
        rooms, stages = manual.build_room_plan([{ "id":"0_1", "mode":"vacuum_then_mop" }, {"id":"0_2", "mode":"vacuum"}], defaults, caps, targets, 0)
        self.assertEqual([stage["room_index"] for stage in stages], [0,0,0,0,1,1])
        self.assertNotIn("water", rooms[1]["setup"])
        with self.assertRaises(ValueError):
            manual.build_room_plan([{ "id":"0_1", "mode":"vacuum", "water":"high" }], defaults, caps, targets, 0)

    def test_room_plan_rejects_unknown_duplicate_and_excess_rooms(self):
        vacuum, coordinator, entries, states, areas = self.fixture()
        caps, _, _ = manual.capabilities(vacuum, coordinator, entries, states, areas)
        targets = manual.robot_targets(coordinator, 0)
        with self.assertRaisesRegex(ValueError, "current map"):
            manual.build_room_plan([{"id": "0_99", "mode": "vacuum"}], {}, caps, targets, 0)
        with self.assertRaisesRegex(ValueError, "only once"):
            manual.build_room_plan([{"id": "0_1", "mode": "vacuum"}, {"id": "0_1", "mode": "mop"}], {}, caps, targets, 0)
        with self.assertRaisesRegex(ValueError, "32 rooms"):
            manual.build_room_plan([{"id": "0_1", "mode": "vacuum"}] * 33, {}, caps, targets, 0)
        with self.assertRaisesRegex(ValueError, "supported.*cleaning mode"):
            manual.build_room_plan([{"id": "0_1", "mode": "teleport"}], {}, caps, targets, 0)

    def test_room_plan_repeats_and_two_pass_rooms_expand_to_stages(self):
        vacuum, coordinator, entries, states, areas = self.fixture()
        caps, _, _ = manual.capabilities(vacuum, coordinator, entries, states, areas)
        targets = manual.robot_targets(coordinator, 0)
        rooms, stages = manual.build_room_plan(
            [{"id": "0_1", "mode": "vacuum_then_mop"}, {"id": "0_3", "mode": "vacuum", "repeat": 2}],
            {}, caps, targets, 0)
        self.assertEqual(len(stages), 2 + 2)
        self.assertEqual([stage["pass_index"] for stage in stages[:2]], [0, 1])
        self.assertEqual([stage["target"] for stage in stages[2:]], ["0_3", "0_3"])
        self.assertEqual(len(rooms), 2)

    def test_unavailable_robot_reports_no_invented_rooms(self):
        vacuum, coordinator, entries, states, areas, _ = fixture()
        coordinator.properties_api.home = NS()
        caps = manual.capabilities(vacuum, coordinator, entries, states, areas)[0]
        self.assertEqual(caps["robot_rooms"], [])
        self.assertEqual(caps["robot_maps"], [])
        self.assertFalse(caps["rooms_complete"])


class CompetingCommandTests(unittest.TestCase):
    def test_native_spot_zone_and_go_to_stop_queue_but_read_only_services_do_not(self):
        for domain, service in [("vacuum", "clean_spot"), ("roborock", "set_vacuum_zoned_cleaning"), ("roborock", "set_vacuum_goto_position")]:
            check = lambda data: adapter.is_competing_command(domain, service, data, "vacuum.robot", lambda _eid: False)
            self.assertTrue(check({"entity_id": "vacuum.robot"}))
            self.assertTrue(check({"area_id": "kitchen"}))
            self.assertTrue(check({}))
            self.assertFalse(check({"entity_id": "vacuum.other"}))
        for service in ["get_maps", "get_vacuum_current_position"]:
            self.assertFalse(adapter.is_competing_command("roborock", service, {"entity_id": "vacuum.robot"}, "vacuum.robot", lambda _eid: False))


class ManualEngineTests(unittest.TestCase):
    def plan(self):
        vacuum, coordinator, entries, states, areas, _ = fixture()
        caps, controls, targets = manual.capabilities(vacuum, coordinator, entries, states, areas)
        setup, stages = manual.build_plan(["kitchen"], {"mode": "vacuum_then_mop"}, caps, targets, 0)
        queue = Queue()
        self.assertEqual(queue.start_manual(vacuum.entity_id, ["kitchen"], setup, stages, controls, ready(), 100, "manual1"), ("configure", "0"))
        return queue

    def configured(self, queue, now=110):
        current = ready()
        current.settings = dict(queue.stage["settings"])
        current.observed_at = now
        return current

    def test_no_start_before_fresh_readback_and_no_second_pass_without_success(self):
        queue = self.plan()
        stale = self.configured(queue, 99)
        self.assertIsNone(queue.observe(stale, 110))
        mismatch = self.configured(queue)
        mismatch.settings["suction"] = "quiet"
        self.assertIsNone(queue.observe(mismatch, 115))
        self.assertEqual(queue.observe(self.configured(queue), 120), ("manual", "0"))
        queue.observe(cleaning(), 125)
        queue.observe(ready(record(120, 200)), 210)
        self.assertEqual(queue.observe(ready(record(120, 200)), 220), ("configure", "1"))
        self.assertEqual(queue.stage["mode"], "mop")
        self.assertEqual(queue.observe(self.configured(queue, 225), 230), ("manual", "1"))
        queue.observe(cleaning(), 235)
        queue.observe(ready(record(230, 300)), 310)
        self.assertEqual((queue.phase, queue.completed), ("completed", 2))

    def test_prepare_timeout_busy_robot_cancel_restart_no_cleaning(self):
        for outcome in ["timeout", "busy", "cancel", "restart"]:
            queue = self.plan()
            if outcome == "timeout":
                queue.observe(ready(), 701)   # the settings readback window is 600s
            elif outcome == "busy":
                queue.observe(cleaning(), 110)
            elif outcome == "cancel":
                queue.command("cancel", ready(), 110)
            else:
                queue = Queue.restore(queue.dump())
            self.assertIn(queue.phase, {"attention", "cancelled"})
            self.assertIsNone(queue.observe(self.configured(queue, 200), 200))

    def test_failed_vacuum_pass_never_starts_mop(self):
        queue = self.plan()
        queue.observe(self.configured(queue), 110)
        queue.observe(cleaning(), 120)
        queue.observe(ready(record(110, 200, complete=0, finish_reason=21)), 210)
        self.assertEqual((queue.phase, queue.completed, queue.current_index), ("attention", 0, 0))

    def test_active_plan_cannot_be_replaced_by_another(self):
        queue = self.plan()
        before = queue.dump()
        stages = [{"target": "0_2", "mode": "vacuum", "room_index": 0, "pass_index": 0, "repeat_index": 0,
                   "settings": {"mode": "vacuum"}, "map_id": 0, "segments": ["2"]}]
        with self.assertRaises(ValueError):
            queue.start_manual("vacuum.robot", ["0_2"], {}, stages, {}, ready(), 102, "other")
        self.assertEqual(before, queue.dump())


class FakeStore:
    def __init__(self, *args):
        self.saved = []
    async def async_save(self, data):
        self.saved.append(data)

class FakeContext:
    def __init__(self, user_id=None, parent_id=None):
        self.user_id, self.parent_id, self.id = user_id, parent_id, uuid4().hex

class ServiceError(Exception):
    pass


def manager_class():
    """Run the actual Manager class with HA-shaped fakes, without installing HA."""
    source = Path(__file__).resolve().parents[1] / "custom_components/robot_cleaner_queue/__init__.py"
    tree = ast.parse(source.read_text())
    definition = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Manager")
    env = dict(asyncio=asyncio, time=time, logging=logging, deque=deque, uuid4=uuid4, Store=FakeStore,
               Queue=Queue, Snapshot=Snapshot, ACTIVE=load("engine").ACTIVE,
               ACK_SECONDS=60, CONTROLS=device.CONTROLS, DOCK=device.DOCK, device_entities=device.device_entities, device_command=device.device_command,
               DOMAIN="robot_cleaner_queue", HomeAssistant=object, ServiceCall=object, Context=FakeContext, callback=lambda f:f,
               ar=NS(async_get=lambda hass: hass.areas), ServiceValidationError=ServiceError, Unauthorized=ServiceError,
               POLICY_CONTROL="control", async_require_control=permissions.async_require_control, _LOGGER=logging.getLogger("test"),
               snapshot=adapter.snapshot, routine_matches=adapter.routine_matches, is_competing_command=adapter.is_competing_command,
               build_plan=manual.build_plan, build_room_plan=manual.build_room_plan, robot_targets=manual.robot_targets,
               cached_settings=manual.cached_settings, capabilities=manual.capabilities,
               current_map=manual.current_map, validate_stage=manual.validate_stage)
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), "exec", flags=__future__.annotations.compiler_flag), env)
    return env["Manager"]


class ManagerTraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.vacuum, self.coordinator, self.entries, self.states, self.areas, self.registry = fixture()
        self.calls = []
        self.allowed = {e.entity_id for e in self.entries}
        self.admin = False
        async def get_user(_user):
            return NS(is_active=True, is_admin=self.admin, permissions=NS(check_entity=lambda entity, policy: entity in self.allowed))
        async def call(domain, service, data, **kwargs):
            self.calls.append((domain, service, data))
            self.assertTrue(self.manager.store.saved, "State must be persisted before commands")
            if getattr(self, "fail_service", None) == service:
                raise RuntimeError("Sensitive native payload must not escape")
            # Native setting services refresh cached state after setting, and Home
            # Assistant also moves the select entity to the option it accepted.
            status = self.coordinator.properties_api.status
            if service == "select_option":
                self.states[data["entity_id"]].state = data["option"]
                key = data["entity_id"].removeprefix("select.renamed_")
                if key == "mode":
                    status.current_cleaning_mode_name = data["option"]
                    status.fan_speed_name = "off" if data["option"] == "mop" else "balanced"
                    status.water_mode_name = "off" if data["option"] == "vacuum" else "medium"
                    status.mop_route_name = "standard"
                else:
                    setattr(status, {"water": "water_mode_name", "route": "mop_route_name"}[key], data["option"])
            elif service == "set_fan_speed":
                status.fan_speed_name = data["fan_speed"]
                self.states["vacuum.robot"].attributes["fan_speed"] = data["fan_speed"]
            self.coordinator._last_update_success_time = datetime.now(timezone.utc)
            if getattr(self, "interrupt_after", None) == len(self.calls):
                self.manager.queue.attention("External command")
            if getattr(self, "revoke_after", None) == len(self.calls):
                self.allowed.clear()
            if getattr(self, "app_start_after", None) == len(self.calls):
                status.state_name, status.in_cleaning = "segment_cleaning", 1
                self.states["vacuum.robot"].state = "cleaning"
        self.segment_calls = []
        async def clean_segments(segment_ids, **kwargs):
            self.segment_calls.append(list(segment_ids))
        self.vacuum_entity = NS(async_clean_segments=clean_segments, async_get_segments=lambda: [])
        self.hass = NS(states=self.states, areas=self.areas, auth=NS(async_get_user=get_user),
                       services=NS(async_call=call), async_create_task=asyncio.create_task,
                       data={"vacuum": NS(get_entity=lambda entity_id: self.vacuum_entity)})
        self.manager = manager_class()(self.hass)
        self.manager.resolve = lambda vacuum: (self.registry, self.vacuum, self.coordinator)

    async def start(self, rooms=None, setup=None):
        await self.manager.control(NS(context=FakeContext("user"), data={"command": "start_manual", "vacuum": "vacuum.robot", "presets": [],
                                   "rooms": ["kitchen"] if rooms is None else rooms,
                                   "setup": setup or {"mode": "vacuum_mop", "suction": "max", "water": "high", "route": "fast"}}))

    async def save_manual(self, **overrides):
        data = {"vacuum":"vacuum.robot", "source":"manual", "presets":[], "rooms":["office", "kitchen"],
                "setup":{"mode":"vacuum", "suction":"max", "repeat":2}, **overrides}
        await self.manager.save_preset(NS(context=FakeContext("user"), data=data))

    async def test_rejected_area_start_never_readdresses_an_active_room_plan(self):
        await self.save_rooms()
        await self.manager.control(NS(context=FakeContext("user"), data={"command":"toggle_saved", "vacuum":"vacuum.robot"}))
        before = self.manager.queue.dump()
        with self.assertRaises(ServiceError):
            await self.start()
        self.assertEqual(self.manager.queue.dump(), before)
        await self.manager.tick()
        self.assertEqual(self.segment_calls, [["0_1"]])
        self.assertFalse(any(service == "clean_area" for _, service, _ in self.calls))

    async def test_retired_routine_plan_never_turns_into_whole_home_clean(self):
        self.manager.saved_presets = {"vacuum.robot": {"source":"preset", "rooms":[], "setup":{}, "presets":["button.old"]}}
        with self.assertRaisesRegex(ServiceError, "retired"):
            await self.manager.control(NS(context=FakeContext("user"), data={"command":"toggle_saved", "vacuum":"vacuum.robot"}))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.queue.phase, "idle")

    async def test_deferred_configuration_resumes_only_unsent_settings(self):
        original = self.hass.services.async_call
        async def pause_entities(domain, service, data, **kwargs):
            await original(domain, service, data, **kwargs)
            if len(self.calls) == 1:
                self.states["select.renamed_water"].state = "unavailable"
        self.hass.services.async_call = pause_entities
        await self.start()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.manager.queue.phase, "preparing")
        await self.manager.tick()
        self.assertEqual(len(self.calls), 1)
        self.states["select.renamed_water"].state = "medium"
        await self.manager.tick()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(sum(data.get("entity_id") == "select.renamed_mode" for _, _, data in self.calls), 1)
        await self.manager.tick()
        self.assertEqual(self.calls[-1][1], "clean_area")
        await self.manager.tick()
        self.assertEqual(sum(service == "clean_area" for _, service, _ in self.calls), 1)

    async def test_command_failure_never_leaks_payload_into_diagnostics(self):
        self.fail_service = "set_fan_speed"
        await self.start()
        result = await self.manager.get_diagnostics(NS(data={"vacuum":"vacuum.robot"},context=NS(user_id=None,id="diag")))
        self.assertNotIn("Sensitive", str(result))
        self.assertEqual(self.manager.queue.phase, "attention")

    async def test_saved_room_plan_dispatches_robot_segments_not_areas(self):
        await self.save_rooms()
        self.assertEqual(self.calls, [])
        self.coordinator.data.status.dock_error_status = 38      # water empty: vacuuming may proceed
        await self.manager.control(NS(context=FakeContext("user"), data={"command":"toggle_saved", "vacuum":"vacuum.robot"}))
        self.assertEqual(self.manager.queue.address, "room")
        await self.manager.tick()
        # The native entity is called with the room's own id: the V1 entity parses
        # "<map>_<room>" and ignores other maps, the Q-series take bare segment ids.
        self.assertEqual(self.segment_calls, [["0_1"]])
        self.assertFalse(any(service in {"clean_area", "start"} for _, service, _ in self.calls))
        self.assertFalse(any(domain == "button" for domain, _, _ in self.calls))

    async def test_save_is_durable_no_commands_and_retrievable_from_new_manager(self):
        await self.save_manual()
        self.assertEqual(self.calls, [])
        stored = self.manager.preset_store.saved[-1]
        self.assertEqual(stored["vacuum.robot"]["rooms"], ["office", "kitchen"])
        self.assertEqual(stored["vacuum.robot"]["map_id"], 0)
        fresh = manager_class()(self.hass)
        fresh.saved_presets = stored
        self.assertEqual(await fresh.read_saved_preset("vacuum.robot", "user"), stored["vacuum.robot"])
        caps = await self.manager.get_capabilities(NS(context=FakeContext("user"), data={"vacuum":"vacuum.robot"}))
        self.assertEqual(caps["saved_preset"], stored["vacuum.robot"])
        self.assertEqual(caps["control_version"], 5)

    async def test_startup_restores_preset_without_starting_cleaning(self):
        await self.save_manual()
        stored = self.manager.preset_store.saved[-1]
        fresh = manager_class()(self.hass)
        async def load_plan(): return stored
        async def load_queue(): return None
        fresh.preset_store.async_load = load_plan
        fresh.store.async_load = load_queue
        env = fresh.setup.__func__.__globals__
        env["async_track_time_interval"] = lambda *args: lambda: None
        env["timedelta"] = lambda **kwargs: None
        env["EVENT_CALL_SERVICE"], env["EVENT_HOMEASSISTANT_STOP"] = "service", "stop"
        self.hass.bus = NS(async_listen=lambda *args: lambda: None, async_listen_once=lambda *args: lambda: None)
        await fresh.setup()
        self.assertEqual(fresh.saved_presets, stored)
        self.assertEqual(fresh.queue.phase, "idle")
        self.assertEqual(self.calls, [])

    async def save_rooms(self, rooms=None, setup=None):
        await self.manager.save_preset(NS(context=FakeContext("user"), data={
            "vacuum": "vacuum.robot", "source": "rooms",
            "rooms": rooms if rooms is not None else [{"id": "0_1", "mode": "vacuum", "suction": "max"}],
            "setup": {} if setup is None else setup}))

    async def test_busy_saved_toggle_finishes_even_if_saved_plan_is_invalid(self):
        self.manager.saved_presets = {"vacuum.robot": {"source":"broken"}}
        self.coordinator.data.status.state_name = "segment_cleaning"
        self.coordinator.data.status.in_cleaning = 1
        self.states["vacuum.robot"].state = "cleaning"
        await self.manager.control(NS(context=FakeContext("user"), data={"command":"toggle_saved", "vacuum":"vacuum.robot", "presets":[], "rooms":[]}))
        self.assertEqual(self.manager.queue.mode, "finish")

    async def test_saved_toggle_uses_manual_order_and_settings_not_fallback_button(self):
        await self.save_manual()
        await self.manager.control(NS(context=FakeContext("user"), data={"command":"toggle_saved", "vacuum":"vacuum.robot", "presets":["button.unrelated"], "rooms":[]}))
        self.assertEqual(self.manager.queue.targets, ["office", "kitchen"])
        self.assertEqual(self.manager.queue.setup["suction"], "max")
        self.assertEqual(self.manager.queue.setup["repeat"], 2)
        self.assertEqual(self.manager.queue.mode, "manual")
        self.assertFalse(any(service=="press" for _,service,_ in self.calls))

    async def test_saved_map_change_rejects_before_any_command(self):
        await self.save_manual()
        self.coordinator.properties_api.maps.current_map = 1
        with self.assertRaisesRegex(ServiceError, "another map"):
            await self.manager.control(NS(context=FakeContext("user"), data={"command":"toggle_saved", "vacuum":"vacuum.robot", "presets":[], "rooms":[]}))
        self.assertEqual(self.calls, [])

    async def test_invalid_settings_and_storage_failure_preserve_previous_preset(self):
        await self.save_manual()
        previous = self.manager.saved_presets
        with self.assertRaises(ServiceError):
            await self.save_manual(setup={"mode":"vacuum", "suction":"made_up"})
        self.assertEqual(self.manager.saved_presets, previous)
        async def fail(data): raise RuntimeError("disk full")
        self.manager.preset_store.async_save = fail
        with self.assertRaises(RuntimeError): await self.save_manual(rooms=["kitchen"])
        self.assertEqual(self.manager.saved_presets, previous)
        self.assertEqual(self.calls, [])

    async def test_saving_requires_permission_and_cannot_change_active_queue(self):
        await self.start()
        queue = self.manager.queue.dump()
        count = len(self.calls)
        await self.save_manual()
        self.assertEqual(self.manager.queue.dump(), queue)
        self.assertEqual(len(self.calls), count)
        self.allowed.clear()
        with self.assertRaises(Exception): await self.save_manual()
        self.assertEqual(len(self.manager.preset_store.saved), 1)

    async def test_capabilities_do_not_dispatch_or_poll(self):
        caps = await self.manager.get_capabilities(NS(context=FakeContext("user"), data={"vacuum": "vacuum.robot"}))
        self.assertTrue(caps["supported"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.store.saved, [])

    async def test_settings_order_readback_and_native_area_payload(self):
        await self.start()
        self.assertEqual([(d,s) for d,s,_ in self.calls], [("select", "select_option"), ("vacuum", "set_fan_speed"), ("select", "select_option"), ("select", "select_option")])
        self.assertEqual(self.calls[0][2]["option"], "vac_and_mop")
        self.assertEqual(self.manager.queue.phase, "preparing")
        await self.manager.tick()
        self.assertEqual(self.calls[-1], ("vacuum", "clean_area", {"entity_id": "vacuum.robot", "cleaning_area_id": ["kitchen"]}))
        self.assertEqual(self.manager.queue.pending_command, "start")
        self.assertFalse(any(domain == "button" for domain, _, _ in self.calls))

    async def test_whole_home_uses_native_start_and_saved_settings(self):
        await self.start(rooms=[], setup={"mode": "vacuum", "suction": "max", "repeat": 2})
        await self.manager.tick()
        self.assertEqual(self.calls[-1], ("vacuum", "start", {"entity_id": "vacuum.robot"}))
        self.assertEqual(len(self.manager.queue.stages), 2)
        self.assertEqual(self.manager.store.saved[-1]["owner_user_id"], "user")

    async def test_service_failure_external_interrupt_or_revocation_stops_mid_configuration(self):
        for attr, value in [("fail_service", "set_fan_speed"), ("interrupt_after", 1), ("revoke_after", 1)]:
            await self.asyncSetUp()
            setattr(self, attr, value)
            await self.start()
            self.assertEqual(self.manager.queue.phase, "attention")
            self.assertFalse(any(service in {"start", "clean_area"} for _, service, _ in self.calls))
            self.assertNotIn("Sensitive", self.manager.queue.error)
            delattr(self, attr)

    async def test_native_app_start_during_settings_stops_remaining_writes(self):
        self.app_start_after = 1
        await self.start()
        self.assertEqual(self.manager.queue.phase, "attention")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "select_option")

    async def test_map_change_between_settings_and_start_never_starts(self):
        await self.start()
        self.coordinator.properties_api.maps.current_map = 1
        await self.manager.tick()
        self.assertEqual(self.manager.queue.phase, "attention")
        self.assertFalse(any(service in {"start", "clean_area"} for _, service, _ in self.calls))

    async def test_unavailable_native_controls_defer_instead_of_abandoning_the_plan(self):
        """The dock marks setting entities unavailable while it services; wait, do not stop."""
        await self.start()
        self.assertTrue(self.manager.queue.stage)          # a manual plan is configuring
        sent = len(self.calls)
        self.states["select.renamed_water"].state = "unavailable"
        await self.manager.tick()
        self.assertNotEqual(self.manager.queue.phase, "attention")
        self.assertEqual(len(self.calls), sent, "No setting may be written while an entity is unavailable")
        self.states["select.renamed_water"].state = "medium"
        await self.manager.tick()
        self.assertIn(("select", "select_option"), [(domain, service) for domain, service, _ in self.calls])

    async def test_diagnostics_explain_the_last_steps(self):
        await self.start()
        result = await self.manager.get_diagnostics(NS(data={"vacuum": "vacuum.robot"}, context=NS(user_id=None, id="diag")))
        self.assertIn("queue", result)
        self.assertTrue(result["events"], "Expected recorded events")
        self.assertTrue(any(event["kind"] == "command" for event in result["events"]))
        self.assertIn("status", result["robot"])

    async def test_external_setting_and_map_controls_interrupt_queue(self):
        await self.start()
        event = NS(context=FakeContext("other"), data={"domain": "select", "service": "select_option", "service_data": {"entity_id": "select.renamed_mode"}})
        self.manager.external_command(event)
        self.assertEqual(self.manager.queue.phase, "attention")
        await asyncio.sleep(0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
