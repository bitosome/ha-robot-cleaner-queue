"""An HA-owned ordered routine queue, independent of any browser connection."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from uuid import uuid4

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Context, HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import area_registry as ar, config_validation as cv, entity_registry as er
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .adapter import is_competing_command, routine_matches, snapshot
from .engine import ACTIVE, ACK_SECONDS, Queue, Snapshot
from .device import CONTROLS, DOCK, device_entities, device_command
from .permissions import async_require_control
from .manual import build_plan, cached_settings, capabilities, current_map, validate_stage

DOMAIN = "robot_cleaner_queue"
_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)
SERVICE_SCHEMA = vol.Schema({
    vol.Required("command"): vol.In(["start", "start_manual", "pause", "resume", "cancel", "return_to_dock", "stop", "toggle", "toggle_saved"]),
    vol.Optional("presets", default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional("vacuum", default=""): str,
    vol.Optional("rooms", default=[]): vol.All(cv.ensure_list, [str]),
    vol.Optional("setup"): vol.Schema({
        vol.Required("mode"): vol.In(["vacuum", "mop", "vacuum_mop", "vacuum_then_mop"]),
        vol.Optional("suction"): str, vol.Optional("water"): str, vol.Optional("route"): str,
        vol.Optional("repeat", default=1): vol.All(int, vol.In([1, 2])),
    }),
    # Accepted for card configuration compatibility; safety comes from native data.
    vol.Optional("cleaning_entity", default=""): str,
    vol.Optional("status_entity", default=""): str,
    vol.Optional("error_entity", default=""): str,
    vol.Optional("last_clean_end_entity", default=""): str,
})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    manager = Manager(hass)
    hass.data[DOMAIN] = manager
    await manager.setup()
    hass.services.async_register(DOMAIN, "control", manager.control, schema=SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, "save_preset", manager.save_preset, schema=vol.Schema({
        vol.Required("vacuum"): cv.entity_id,
        vol.Required("source"): vol.In(["preset", "manual"]),
        vol.Optional("presets", default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Optional("rooms", default=[]): vol.All(cv.ensure_list, [str]),
        vol.Optional("setup", default={}): dict,
    }))
    hass.services.async_register(DOMAIN, "get_capabilities", manager.get_capabilities,
                                 schema=vol.Schema({vol.Required("vacuum"): cv.entity_id}),
                                 supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "device_control", manager.device_control,
        schema=vol.Schema({vol.Required("vacuum"): cv.entity_id,
                           vol.Required("control"): vol.In([*CONTROLS, "locate"]),
                           vol.Optional("value", default=""): vol.Any(str, int, float)}))
    await async_load_platform(hass, "sensor", DOMAIN, {}, config)
    return True


class Manager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.queue = Queue()
        self.store = Store(hass, 1, DOMAIN)
        self.preset_store = Store(hass, 1, DOMAIN + "_presets")
        self.saved_presets: dict = {}
        self.lock = asyncio.Lock()
        self.listeners = []
        self.unsubs = []
        self.contexts: set[str] = set()
        self.closing = False
        self.coordinator = None
        self.coordinator_unsub = None
        self.last_published = None

    async def setup(self) -> None:
        try:
            loaded_presets = await self.preset_store.async_load()
            self.saved_presets = loaded_presets if isinstance(loaded_presets, dict) else {}
        except Exception:  # noqa: BLE001 - a damaged store must never block setup
            _LOGGER.exception("Saved presets could not be read; starting without them")
            self.saved_presets = {}
        try:
            loaded_queue = await self.store.async_load()
            self.queue = Queue.restore(loaded_queue if isinstance(loaded_queue, dict) else None)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("The stored queue could not be read; starting from an empty queue")
            self.queue = Queue()
            self.queue.attention("The stored sequence could not be read. Check the robot before starting a new one.")
        await self.store.async_save(self.queue.dump())
        self.unsubs.extend([
            async_track_time_interval(self.hass, self.tick, timedelta(seconds=5)),
            self.hass.bus.async_listen(EVENT_CALL_SERVICE, self.external_command),
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.shutdown),
        ])

    @callback
    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    async def publish(self) -> None:
        data = self.queue.dump()
        if data == self.last_published:
            return
        # Persist before sending a new physical command or exposing the transition.
        try:
            await self.store.async_save(data)
        except Exception:  # noqa: BLE001 - never leave an undispatched command looking active
            _LOGGER.exception("Queue state could not be saved; stopping the queue for review")
            self.queue.attention("The queue state could not be saved. Check the robot before starting a new sequence.")
            self.last_published = self.queue.dump()
            for listener in list(self.listeners):
                listener()
            return
        self.last_published = data
        for listener in list(self.listeners):
            listener()

    def resolve(self, vacuum: str):
        registry = er.async_get(self.hass)
        entry = registry.async_get(vacuum)
        if entry is None or entry.platform != "roborock" or entry.domain != "vacuum":
            raise ValueError("Choose a vacuum from the native Roborock integration.")
        config_entry = self.hass.config_entries.async_get_entry(entry.config_entry_id)
        runtime = getattr(config_entry, "runtime_data", None)
        coordinators = runtime.values() if hasattr(runtime, "values") else []
        coordinator = next((c for c in coordinators if getattr(c, "duid_slug", None) == entry.unique_id), None)
        if coordinator is None or not hasattr(getattr(coordinator, "properties_api", None), "clean_summary"):
            raise ValueError("This Roborock model does not expose the cached cleaning records needed for safe sequencing.")
        return registry, entry, coordinator

    def manual_capabilities(self, vacuum: str):
        registry, entry, coordinator = self.resolve(vacuum)
        caps, controls, targets = capabilities(entry, coordinator, registry.entities.values(), self.hass.states, ar.async_get(self.hass))
        return caps, controls, targets, current_map(coordinator)[0]

    async def get_capabilities(self, call: ServiceCall) -> dict:
        try:
            await async_require_control(self.hass.auth, call.context.user_id, [call.data["vacuum"]], POLICY_CONTROL)
        except PermissionError as err:
            raise Unauthorized(context=call.context, permission=POLICY_CONTROL) from err
        try:
            caps, controls, _, _ = self.manual_capabilities(call.data["vacuum"])
            await async_require_control(self.hass.auth, call.context.user_id, list(controls.values()), POLICY_CONTROL)
            caps["control_version"] = 4
            caps["current_map"] = current_map(self.resolve(call.data["vacuum"])[2])[0]
            caps["saved_preset"] = await self.read_saved_preset(call.data["vacuum"], call.context.user_id)
            caps["device_entities"] = {}
            for key, entity_id in self.device_entities(call.data["vacuum"]).items():
                try:
                    await async_require_control(self.hass.auth, call.context.user_id, [entity_id],
                                                "read" if key not in CONTROLS else POLICY_CONTROL)
                    caps["device_entities"][key] = entity_id
                except PermissionError:
                    pass
            return caps
        except PermissionError as err:
            raise Unauthorized(context=call.context, permission=POLICY_CONTROL) from err
        except (ValueError, AttributeError, TypeError):
            return {"supported": False, "modes": [], "suction": [], "water": [], "routes": [],
                    "routes_by_mode": {"vacuum": [], "vacuum_mop": [], "mop": [], "vacuum_then_mop": []},
                    "repeats": [], "area_cleaning": False, "room_targets": [], "robot_maps": [], "robot_rooms": [],
                    "unmapped_areas": [], "rooms_complete": False, "defaults": {},
                    "error": "Manual cleaning is unavailable. Check the native Roborock integration and area mapping."}

    async def read_saved_preset(self, vacuum, user_id):
        plan = self.saved_presets.get(vacuum)
        if plan:
            await async_require_control(self.hass.auth, user_id, [vacuum, *plan.get("presets", [])], POLICY_CONTROL)
        return plan

    async def save_preset(self, call: ServiceCall) -> None:
        """Persist a reusable plan; never configure or actuate the robot."""
        async with self.lock:
            if self.closing:
                raise ServiceValidationError("Home Assistant is stopping.")
            vacuum, source = call.data["vacuum"], call.data["source"]
            try:
                await async_require_control(self.hass.auth, call.context.user_id, [vacuum], POLICY_CONTROL)
                plan = {"source": source, "presets": [], "rooms": [], "setup": {}}
                if source == "manual":
                    if call.data.get("presets"):
                        raise ValueError("A manual preset cannot contain Roborock routine buttons.")
                    caps, controls, targets, map_id = self.manual_capabilities(vacuum)
                    await async_require_control(self.hass.auth, call.context.user_id, list(controls.values()), POLICY_CONTROL)
                    setup, _ = build_plan(call.data.get("rooms", []), call.data.get("setup", {}), caps, targets, map_id)
                    plan.update(rooms=list(call.data.get("rooms", [])), setup=setup, map_id=map_id)
                else:
                    if call.data.get("rooms") or call.data.get("setup"):
                        raise ValueError("Roborock routines use their app settings, not manual settings.")
                    presets = list(call.data.get("presets", []))
                    if not 1 <= len(presets) <= 32 or len(set(presets)) != len(presets):
                        raise ValueError("Select 1–32 distinct room presets.")
                    await async_require_control(self.hass.auth, call.context.user_id, presets, POLICY_CONTROL)
                    registry, entry, coordinator = self.resolve(vacuum)
                    for preset in presets:
                        state = self.hass.states.get(preset)
                        if not routine_matches(registry.async_get(preset), entry, coordinator) or state is None or state.state == "unavailable":
                            raise ValueError("Every preset must be an available routine for this robot.")
                    plan["presets"] = presets
                updated = {**self.saved_presets, vacuum: plan}
                await self.preset_store.async_save(updated)
                self.saved_presets = updated  # Report success only after durable storage.
            except PermissionError as err:
                raise Unauthorized(context=call.context, permission=POLICY_CONTROL) from err
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err

    def device_entities(self, vacuum: str) -> dict:
        registry, entry, coordinator = self.resolve(vacuum)
        return device_entities(entry, coordinator, registry.entities.values(), self.hass.states)

    def validate_device(self, vacuum, key, value):
        current = self.current_snapshot(vacuum)
        self.queue._validate_command_barrier(current, time.time())
        if self.queue.phase in ACTIVE or self.queue.phase == "attention" or self.queue.pending_command:
            raise ValueError("Finish or clear the cleaning sequence before changing dock settings.")
        if not current.connected or current.vacuum in {"unknown", "unavailable"}:
            raise ValueError("The robot is unavailable.")
        if key in DOCK and value == "on":
            mode = "mop" if key == "mop_washing" else "vacuum"
            if current.vacuum != "docked" or not current.ready_for(mode):
                raise ValueError("The robot must be docked, without an unfinished job or a fault affecting this dock action.")
        return current

    async def device_control(self, call: ServiceCall) -> None:
        async with self.lock:
            if self.closing:
                raise ServiceValidationError("Home Assistant is stopping.")
            vacuum, key, value = call.data["vacuum"], call.data["control"], call.data.get("value", "")
            reservation = None
            try:
                await async_require_control(self.hass.auth, call.context.user_id, [vacuum], POLICY_CONTROL)
                if key == "locate":
                    state = self.hass.states.get(vacuum)
                    if state is None or state.state in {"unknown", "unavailable"} or not int(state.attributes.get("supported_features", 0)) & 512:
                        raise ValueError("Find robot is unavailable.")
                    await self.hass.services.async_call("vacuum", "locate", {"entity_id": vacuum}, blocking=True, context=call.context)
                    return  # A sound has no telemetry acknowledgement and never adopts a job.
                entity_id = self.device_entities(vacuum).get(key)
                if not entity_id or key not in CONTROLS:
                    raise ValueError("This native control is not available for this robot.")
                await async_require_control(self.hass.auth, call.context.user_id, [vacuum, entity_id], POLICY_CONTROL)
                self.validate_device(vacuum, key, value)
                domain, service, data, expected = device_command(key, value, entity_id, self.hass.states.get(entity_id))
                state = self.hass.states.get(entity_id)
                if self.device_value_matches(state.state, expected, domain):
                    return
                self.queue = Queue(mode="device", phase="controlling", vacuum=vacuum, run_id=uuid4().hex,
                    setup={"control": key, "value": expected, "domain": domain}, control_entities={"device": entity_id},
                    pending_command="device", command_at=time.time(), owner_user_id=call.context.user_id)
                reservation = self.queue.run_id
                await self.publish()
                # Persisted reservation blocks starts while native telemetry catches up.
                await async_require_control(self.hass.auth, call.context.user_id, [vacuum, entity_id], POLICY_CONTROL)
                if self.closing or self.queue.phase != "controlling" or self.queue.pending_command != "device":
                    raise ValueError("The device command was interrupted before dispatch.")
                if self.device_entities(vacuum).get(key) != entity_id:
                    raise ValueError("The native control changed before dispatch.")
                current = self.current_snapshot(vacuum)
                if key in DOCK and value == "on" and (current.vacuum != "docked" or not current.ready_for("mop" if key == "mop_washing" else "vacuum")):
                    raise ValueError("The robot became busy before the dock command.")
                context = Context(user_id=call.context.user_id, parent_id=call.context.id)
                self.contexts.add(context.id)
                await self.hass.services.async_call(domain, service, data, blocking=True, context=context)
            except PermissionError as err:
                if reservation and self.queue.run_id == reservation and self.queue.pending_command:
                    self.queue.attention("The initiating user can no longer control this device command.")
                    await self.publish()
                raise Unauthorized(context=call.context, permission=POLICY_CONTROL) from err
            except ValueError as err:
                if reservation and self.queue.run_id == reservation and self.queue.pending_command:
                    self.queue.attention("The device command could not be confirmed. Check the robot before retrying.")
                    await self.publish()
                raise ServiceValidationError(str(err)) from err
            except Exception as err:
                if reservation and self.queue.run_id == reservation and self.queue.pending_command:
                    self.queue.attention("The device command failed or is uncertain. No retry was sent.")
                    await self.publish()
                raise ServiceValidationError("The device command failed. Check the robot before retrying.") from err

    @staticmethod
    def device_value_matches(actual, expected, domain):
        try:
            return float(actual) == float(expected) if domain == "number" else actual == expected
        except (TypeError, ValueError):
            return False

    def observe_device(self):
        queue = self.queue
        state = self.hass.states.get(queue.control_entities.get("device", ""))
        updated = getattr(state, "last_updated", None)
        if (state and updated and updated.timestamp() >= queue.command_at and
                self.device_value_matches(state.state, queue.setup.get("value"), queue.setup.get("domain"))):
            queue.phase = "idle"
            queue.confirmed()
        elif time.time() - queue.command_at >= ACK_SECONDS:
            queue.attention("The device did not confirm this setting within 60 seconds. No retry was sent.")

    def current_snapshot(self, vacuum: str) -> Snapshot:
        _, _, coordinator = self.resolve(vacuum)
        if coordinator is not self.coordinator:
            if self.coordinator_unsub:
                self.coordinator_unsub()
            self.coordinator = coordinator
            self.coordinator_unsub = coordinator.async_add_listener(self.schedule_tick)
        state = self.hass.states.get(vacuum)
        current = snapshot(coordinator, state.state if state else "unavailable")
        current.settings = self.observed_settings(vacuum, state, coordinator)
        return current

    def observed_settings(self, vacuum: str, state, coordinator) -> dict:
        """Prefer the native select states: they hold exactly the strings select_option accepts."""
        settings = cached_settings(coordinator)
        for key in ("mode", "water", "route"):
            entity_id = self.queue.control_entities.get(key)
            select_state = self.hass.states.get(entity_id) if entity_id else None
            if select_state is not None and select_state.state not in {"unknown", "unavailable"}:
                settings[key] = select_state.state
        fan = getattr(getattr(state, "attributes", None), "get", lambda *_: None)("fan_speed")
        if isinstance(fan, str) and fan:
            settings["suction"] = fan
        return settings

    @callback
    def schedule_tick(self) -> None:
        if not self.closing:
            self.hass.async_create_task(self.tick())

    async def control(self, call: ServiceCall) -> None:
        async with self.lock:
            if self.closing:
                raise ServiceValidationError("Home Assistant is stopping.")
            data = dict(call.data)
            command = data["command"]
            vacuum = data.get("vacuum") or self.queue.vacuum
            saved_plan = None
            if command in {"toggle", "toggle_saved"}:
                use_saved = command == "toggle_saved"
                # Decide under the same lock as starts and stage advancement.
                if self.queue.vacuum and vacuum != self.queue.vacuum and (self.queue.phase in ACTIVE or self.queue.pending_command):
                    raise ServiceValidationError("Another vacuum has an active command.")
                try:
                    finished = self.queue.should_finish(self.current_snapshot(vacuum))
                except (ValueError, TypeError, AttributeError) as err:
                    raise ServiceValidationError(str(err)) from err
                command = "finish" if finished else "start"
                if command == "start" and use_saved:
                    if not isinstance(self.saved_presets, dict):
                        raise ServiceValidationError("The saved presets could not be read. Save the preset again.")
                    saved_plan = self.saved_presets.get(vacuum)
                    if saved_plan:
                        missing = [key for key in ("source", "presets", "rooms", "setup")
                                   if not isinstance(saved_plan, dict) or key not in saved_plan]
                        if missing:
                            raise ServiceValidationError("The saved preset is incomplete. Save it again from the card.")
                        data.update({key: saved_plan[key] for key in ("presets", "rooms", "setup")})
                        command = "start_manual" if saved_plan["source"] == "manual" else "start"
            bound = self.queue.phase in ACTIVE or self.queue.phase == "attention" or bool(self.queue.pending_command)
            owned_plan = bound and self.queue.mode in {"preset", "manual"}
            standalone = command in {"pause", "resume", "return_to_dock", "stop"} and not owned_plan
            if standalone and not data.get("vacuum"):
                raise ServiceValidationError("Specify a vacuum when controlling a job outside an active queue.")
            if command not in {"start", "start_manual"} and bound and self.queue.vacuum and vacuum != self.queue.vacuum:
                raise ServiceValidationError("This command targets a different vacuum than the current queue.")
            permission_entities = [vacuum, *(data["presets"] if command == "start" else [] if command == "start_manual" else self.queue.presets if owned_plan else []),
                                   *(self.queue.control_entities.values() if owned_plan and command not in {"start", "start_manual"} else [])]
            try:
                await async_require_control(self.hass.auth, call.context.user_id, permission_entities, POLICY_CONTROL)
            except PermissionError as err:
                raise Unauthorized(context=call.context, permission=POLICY_CONTROL) from err
            try:
                if command == "cancel":
                    effect = self.queue.command(command, Snapshot(), time.time())
                else:
                    current = self.current_snapshot(vacuum)
                    if command == "finish":
                        effect = self.queue.finish(vacuum, current, time.time(), uuid4().hex)
                        self.queue.owner_user_id = call.context.user_id
                    elif command == "start_manual":
                        if data["presets"]:
                            raise ValueError("Manual cleaning cannot run preset buttons.")
                        caps, controls, targets, map_id = self.manual_capabilities(vacuum)
                        if saved_plan is not None and saved_plan.get("map_id") != map_id:
                            raise ValueError("The saved preset belongs to another map. Select that map or save a new preset.")
                        setup, stages = build_plan(data["rooms"], data.get("setup", {}), caps, targets, map_id)
                        try:
                            await async_require_control(self.hass.auth, call.context.user_id, list(controls.values()), POLICY_CONTROL)
                        except PermissionError as err:
                            raise Unauthorized(context=call.context, permission=POLICY_CONTROL) from err
                        effect = self.queue.start_manual(vacuum, data["rooms"], setup, stages, controls, current, time.time(), uuid4().hex)
                        self.queue.owner_user_id = call.context.user_id
                    elif command == "start":
                        registry, entry, coordinator = self.resolve(vacuum)
                        presets = data["presets"]
                        for preset in presets:
                            state = self.hass.states.get(preset)
                            if not routine_matches(registry.async_get(preset), entry, coordinator) or state is None or state.state == "unavailable":
                                raise ValueError("Every room must be an available routine button belonging to the selected robot.")
                        effect = self.queue.start(vacuum, presets, current, time.time(), uuid4().hex)
                        self.queue.owner_user_id = call.context.user_id
                    elif standalone:
                        effect = self.queue.external_control(command, vacuum, current, time.time(), uuid4().hex)
                        if effect:
                            self.queue.owner_user_id = call.context.user_id
                    else:
                        effect = self.queue.command(command, current, time.time())
            except ValueError as err:
                # Rejected starts must not interrupt an existing valid queue.
                raise ServiceValidationError(str(err)) from err
            await self.publish()
            if effect:
                await self.execute(effect, call.context)

    def effect_valid(self, effect: tuple[str, str]) -> bool:
        kind, target = effect
        if self.closing:
            return False
        if kind == "configure":
            return self.queue.mode == "manual" and self.queue.phase == "preparing" and self.queue.pending_command == "configure" and target == str(self.queue.current_index)
        if kind in {"preset", "manual"}:
            return self.queue.phase == "starting" and self.queue.pending_command == "start" and (
                (kind == "manual" and self.queue.mode == "manual" and target == str(self.queue.current_index)) or
                (kind == "preset" and self.queue.mode == "preset" and target == self.queue.presets[self.queue.current_index]))
        return kind == "vacuum" and bool(self.queue.pending_command)

    async def execute(self, effect: tuple[str, str], parent: Context | None = None) -> None:
        kind, target = effect
        if not self.effect_valid(effect):
            return
        user_id = parent.user_id if parent is not None else self.queue.owner_user_id
        async def authorize():
            await async_require_control(self.hass.auth, user_id,
                [self.queue.vacuum, *self.queue.presets, *self.queue.control_entities.values()], POLICY_CONTROL)
        try:
            await authorize()
            if not self.effect_valid(effect):
                return
            context = Context(user_id=user_id, parent_id=parent.id if parent else None)
            self.contexts.add(context.id)
            if len(self.contexts) > 128:
                self.contexts = {context.id}
            if kind in {"manual", "configure"}:
                caps, controls, targets, map_id = self.manual_capabilities(self.queue.vacuum)
                validate_stage(self.queue.stage, caps, targets, map_id)
                if controls != self.queue.control_entities:
                    raise ValueError("The native manual control entities changed.")
                if not self.current_snapshot(self.queue.vacuum).ready_for(self.queue.cleaning_mode):
                    raise ValueError("The robot is no longer ready to start a manual job.")
                if kind == "configure":
                    # High-level mode resets lower-level settings: always set it first.
                    for key in ("mode", "suction", "water", "route"):
                        if key not in self.queue.stage["settings"]:
                            continue
                        await authorize()
                        if not self.effect_valid(effect):
                            return
                        # A native-app start can appear during an awaited settings
                        # refresh without a Home Assistant service event.
                        if not self.current_snapshot(self.queue.vacuum).ready_for(self.queue.cleaning_mode):
                            raise ValueError("The robot became busy while applying manual settings.")
                        latest_caps, latest_controls, latest_targets, latest_map = self.manual_capabilities(self.queue.vacuum)
                        validate_stage(self.queue.stage, latest_caps, latest_targets, latest_map)
                        if latest_controls != self.queue.control_entities:
                            raise ValueError("Manual control entities changed during setup.")
                        value = self.queue.stage["settings"][key]
                        if key == "suction":
                            await self.hass.services.async_call("vacuum", "set_fan_speed", {"entity_id": self.queue.vacuum, "fan_speed": value}, blocking=True, context=context)
                        else:
                            await self.hass.services.async_call("select", "select_option", {"entity_id": controls[key], "option": value}, blocking=True, context=context)
                    # Tick uses freshly observed settings before issuing any start.
                    return
                current = self.current_snapshot(self.queue.vacuum)
                if not all(current.settings.get(key) == value for key, value in self.queue.stage["settings"].items()):
                    raise ValueError("Manual settings changed before the start command.")
                area = self.queue.stage["target"]
                data = {"entity_id": self.queue.vacuum}
                if area:
                    data["cleaning_area_id"] = [area]
                await self.hass.services.async_call("vacuum", "clean_area" if area else "start", data, blocking=True, context=context)
            elif kind == "preset":
                if not self.current_snapshot(self.queue.vacuum).ready:
                    raise ValueError("The robot is no longer ready for a preset.")
                registry, entry, coordinator = self.resolve(self.queue.vacuum)
                state = self.hass.states.get(target)
                if not routine_matches(registry.async_get(target), entry, coordinator) or state is None or state.state == "unavailable":
                    raise ValueError("The next room preset is unavailable or no longer belongs to this robot.")
                await self.hass.services.async_call("button", "press", {"entity_id": target}, blocking=True, context=context)
            else:
                current = self.current_snapshot(self.queue.vacuum)
                self.queue._validate_command_barrier(current, time.time())
                if not self.queue.validate_control_state(self.queue.pending_command, current):
                    return
                await self.hass.services.async_call("vacuum", target, {"entity_id": self.queue.vacuum}, blocking=True, context=context)
        except PermissionError:
            self.queue.attention("The initiating user can no longer control this cleaning sequence. No further command was sent.")
            await self.publish()
        except Exception:
            # Raw integration errors can contain private payloads; never expose them.
            self.queue.attention("The command failed or could not be confirmed. Check the robot before retrying; no automatic retry was sent.")
            await self.publish()
            _LOGGER.warning("Robot queue command failed; queue stopped without retry")

    async def tick(self, _now=None) -> None:
        if self.closing or not self.queue.vacuum or self.queue.phase not in ACTIVE and not self.queue.pending_command:
            return
        async with self.lock:
            if self.queue.mode == "device":
                self.observe_device()
                await self.publish()
                return
            try:
                current = self.current_snapshot(self.queue.vacuum)
            except ValueError:
                current = Snapshot()
            effect = self.queue.observe(current, time.time())
            await self.publish()
            if effect:
                await self.execute(effect)

    @callback
    def external_command(self, event) -> None:
        if self.closing or self.queue.phase not in ACTIVE or event.context.id in self.contexts:
            return
        domain, service = event.data.get("domain"), event.data.get("service")
        data = event.data.get("service_data", {})
        def is_routine(entity_id):
            try:
                registry, entry, coordinator = self.resolve(self.queue.vacuum)
                return routine_matches(registry.async_get(entity_id), entry, coordinator)
            except ValueError:
                return False
        def is_setting(entity_id):
            try:
                registry, entry, _ = self.resolve(self.queue.vacuum)
                setting = registry.async_get(entity_id)
                return bool(setting and setting.platform == "roborock" and setting.domain in {"select", "switch"}
                            and setting.config_entry_id == entry.config_entry_id
                            and str(setting.unique_id).endswith("_" + entry.unique_id))
            except ValueError:
                return False
        affected = is_competing_command(domain, service, data, self.queue.vacuum, is_routine, is_setting)
        if affected:
            # Set synchronously before a waiting tick can start another preset.
            self.queue.attention("Another Home Assistant control changed the robot. The queue was stopped to avoid conflicting commands.")
            self.hass.async_create_task(self.persist_interruption())

    async def persist_interruption(self) -> None:
        async with self.lock:
            await self.publish()

    async def shutdown(self, _event) -> None:
        self.closing = True
        async with self.lock:
            for unsub in self.unsubs:
                unsub()
            if self.coordinator_unsub:
                self.coordinator_unsub()
            if self.queue.phase in ACTIVE or self.queue.pending_command:
                self.queue.attention("Home Assistant stopped. The queue will not restart automatically.")
            await self.publish()
