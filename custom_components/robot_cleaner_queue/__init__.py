"""An HA-owned ordered room queue, independent of any browser connection."""
from __future__ import annotations

import asyncio
from collections import deque
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
from .errors import command_failure_metadata
from .permissions import async_require_control
from .manual import build_plan, build_room_plan, cached_settings, capabilities, current_map, robot_targets, validate_stage

DOMAIN = "robot_cleaner_queue"
_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)
# A plan is an ordered list of the robot's own rooms; the legacy form is a list of
# Home Assistant area ids. Per-room settings are optional and fall back to `setup`.
ROOM_SCHEMA = vol.Schema({
    vol.Required("id"): str,
    vol.Optional("mode"): vol.In(["vacuum", "mop", "vacuum_mop", "vacuum_then_mop"]),
    vol.Optional("suction"): str,
    vol.Optional("water"): str,
    vol.Optional("route"): str,
    vol.Optional("repeat"): vol.All(int, vol.In([1, 2])),
})
PLAN_ROOMS = vol.All(cv.ensure_list, [vol.Any(str, ROOM_SCHEMA)])
SERVICE_SCHEMA = vol.Schema({
    vol.Required("command"): vol.In(["start_manual", "pause", "resume", "cancel", "return_to_dock", "stop", "toggle", "toggle_saved"]),
    vol.Optional("vacuum", default=""): str,
    vol.Optional("rooms", default=[]): PLAN_ROOMS,
    # Plan defaults; each room may override any of them, and a room plan without
    # defaults is valid because every room can carry its own mode.
    vol.Optional("setup", default={}): vol.Schema({
        vol.Optional("mode"): vol.In(["vacuum", "mop", "vacuum_mop", "vacuum_then_mop"]),
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
        vol.Required("source"): vol.In(["rooms", "manual"]),
        vol.Optional("revision"): vol.All(int, vol.Range(min=0)),
        # Accepted from the released card and ignored: routines are no longer a plan.
        vol.Optional("presets", default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Optional("rooms", default=[]): PLAN_ROOMS,
        vol.Optional("setup", default={}): dict,
    }))
    hass.services.async_register(DOMAIN, "get_capabilities", manager.get_capabilities,
                                 schema=vol.Schema({vol.Required("vacuum"): cv.entity_id}),
                                 supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "get_diagnostics", manager.get_diagnostics,
                                 schema=vol.Schema({vol.Required("vacuum"): cv.entity_id}),
                                 supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "device_control", manager.device_control,
        schema=vol.Schema({vol.Required("vacuum"): cv.entity_id,
                           vol.Required("control"): vol.In([*CONTROLS, "locate"]),
                           vol.Optional("value", default=""): vol.Any(str, int, float)}))
    hass.services.async_register(DOMAIN, "save_preferences", manager.save_preferences,
        schema=vol.Schema({vol.Required("vacuum"): cv.entity_id,
                           vol.Required("revision"): vol.All(int, vol.Range(min=0)),
                           vol.Required("map_id"): int,
                           vol.Required("defaults"): dict,
                           vol.Required("rooms"): dict}))
    await async_load_platform(hass, "sensor", DOMAIN, {}, config)
    return True


class Manager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.queue = Queue()
        self.store = Store(hass, 1, DOMAIN)
        self.preset_store = Store(hass, 1, DOMAIN + "_presets")
        self.saved_presets: dict = {}
        self.presets_load_failed = False
        self.preferences_store = Store(hass, 1, DOMAIN + "_preferences")
        self.preferences: dict = {}
        self.preferences_load_failed = False
        self.lock = asyncio.Lock()
        self.listeners = []
        self.unsubs = []
        self.contexts: set[str] = set()
        self.closing = False
        self.coordinator = None
        self.coordinator_unsub = None
        self.last_published = None
        self.robot_state: dict = {}
        self.last_robot_published: dict = {}
        self.events: deque = deque(maxlen=100)
        self.last_observation: dict = {}
        # Only resume configuration that was explicitly deferred before dispatch.
        # Never retry a start or an uncertain service call; restarts require review.
        self.deferred_configuration = None
        self.configuration_token = None
        self.configured_keys: set[str] = set()
        self.start_dispatch_token = None

    async def setup(self) -> None:
        await self.load_preferences()
        await self.load_saved_presets()
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

    def control_state(self, controls: dict) -> str:
        """Whether the frozen native controls are intact, changed, or merely unavailable.

        The Roborock integration marks setting entities unavailable while the dock
        services. That is temporary, so the queue waits for them instead of abandoning
        a plan the user selected.
        """
        frozen = self.queue.control_entities
        if controls == frozen:
            return "ok"
        if any(key in controls and controls[key] != frozen.get(key) for key in frozen):
            return "changed"
        missing = sorted(set(frozen) - set(controls))
        if missing:
            self.event("settings-deferred",
                       "native controls temporarily unavailable: %s" % ", ".join(missing), logging.INFO)
            return "waiting"
        return "changed"

    def event(self, kind: str, detail: str, level: int = logging.DEBUG) -> None:
        """Record and log what the queue is doing, so a failure can be explained."""
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "detail": detail,
                 "phase": self.queue.phase, "decision": self.queue.decision,
                 "pending": self.queue.pending_command, "index": self.queue.current_index,
                 "completed": self.queue.completed}
        self.events.append(entry)
        _LOGGER.log(level, "%s | phase=%s pending=%s room=%s/%s decision=%s | %s",
                    kind, entry["phase"], entry["pending"] or "-", entry["index"] + 1,
                    len(self.queue.stages),
                    entry["decision"] or "-", detail)

    def observe_log(self, current) -> None:
        """Log every observation that differs from the previous one."""
        record = current.record or {}
        observation = {
            "vacuum": current.vacuum, "status": current.status, "job": current.job,
            "error": current.error, "dock": current.dock_error, "connected": current.connected,
            "observed_at": round(current.observed_at or 0), "phase": self.queue.phase,
            "decision": self.queue.decision, "pending": self.queue.pending_command,
            "index": self.queue.current_index, "completed": self.queue.completed,
            "record": "%s-%s complete=%s error=%s reason=%s" % (
                record.get("begin"), record.get("end"), record.get("complete"),
                record.get("error"), record.get("finish_reason")),
        }
        if observation == self.last_observation:
            return
        changed = [key for key, value in observation.items() if self.last_observation.get(key) != value]
        self.last_observation = observation
        self.event("observation", "changed: %s" % ", ".join(
            "%s=%s" % (key, observation[key]) for key in changed), logging.DEBUG)

    async def get_diagnostics(self, call: ServiceCall) -> dict:
        """Read-only explanation of the current queue and the last 100 steps."""
        vacuum = call.data["vacuum"]
        try:
            await async_require_control(self.hass.auth, call.context.user_id, [vacuum], POLICY_CONTROL)
        except PermissionError as err:
            raise Unauthorized(context=call.context, permission=POLICY_CONTROL) from err
        if self.queue.vacuum and self.queue.vacuum != vacuum:
            raise ServiceValidationError("Diagnostics belong to a different robot.")
        current = self.current_snapshot(vacuum)
        return {
            "queue": self.queue.dump(),
            "events": list(self.events),
            "robot": {"vacuum": current.vacuum, "status": current.status, "job": current.job,
                      "error": current.error, "dock_error": current.dock_error,
                      "connected": current.connected, "observed_at": current.observed_at,
                      "settings": dict(current.settings), "record": dict(current.record or {}),
                      "dock_drying": current.dock_drying},
            "has_saved_plan": isinstance(self.saved_presets, dict) and vacuum in self.saved_presets,
        }

    @callback
    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    async def publish(self) -> None:
        data = self.queue.dump()
        if data == self.last_published:
            if self.robot_state != self.last_robot_published:
                self.last_robot_published = dict(self.robot_state)
                for listener in list(self.listeners):
                    listener()
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
        self.last_robot_published = dict(self.robot_state)
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
            caps["control_version"] = 5
            caps["execution_version"] = 3
            caps["current_map"] = current_map(self.resolve(call.data["vacuum"])[2])[0]
            caps["saved_preset"] = await self.read_saved_preset(call.data["vacuum"], call.context.user_id)
            caps["saved_plan_revision"] = (caps["saved_preset"] or {}).get("revision", 0)
            caps["preferences"] = self.preference_profile(call.data["vacuum"])
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

    async def load_preferences(self):
        try:
            data = await self.preferences_store.async_load()
            if data is not None and (not isinstance(data, dict) or any(
                not isinstance(key, str) or not isinstance(value, dict)
                or type(value.get("revision")) is not int or value["revision"] < 0
                or not self.valid_stored_settings(value.get("defaults"))
                or not isinstance(value.get("rooms"), dict)
                or any(not isinstance(room, str) or not self.valid_stored_settings(settings)
                       for room, settings in value["rooms"].items())
                for key, value in data.items())):
                raise ValueError("Invalid preference storage")
            self.preferences = data or {}
            self.preferences_load_failed = False
        except Exception:
            self.preferences_load_failed = True
            _LOGGER.error("Room preference storage could not be read; saving is disabled to preserve it")

    def preference_profile(self, vacuum):
        if self.preferences_load_failed:
            raise ServiceValidationError("Room settings could not be read. Restore preference storage before saving.")
        return self.preferences.get(vacuum, {"revision": 0, "defaults": {}, "rooms": {}})

    async def save_preferences(self, call):
        """Shared durable preferences, separate from the frozen wall-switch plan."""
        async with self.lock:
            if self.closing:
                raise ServiceValidationError("Home Assistant is stopping.")
            vacuum = call.data["vacuum"]
            try:
                await async_require_control(self.hass.auth, call.context.user_id, [vacuum], POLICY_CONTROL)
                current = self.preference_profile(vacuum)
                if call.data["revision"] != current["revision"]:
                    raise ValueError("Another user saved room settings. Reload shared settings before saving your changes.")
                caps, controls, _, map_id = self.manual_capabilities(vacuum)
                await async_require_control(self.hass.auth, call.context.user_id, list(controls.values()), POLICY_CONTROL)
                if map_id is None or call.data["map_id"] != map_id:
                    raise ValueError("The robot map changed. Reload shared settings before saving.")
                defaults, _ = build_plan([], call.data["defaults"], caps, {}, map_id)
                targets = robot_targets(self.resolve(vacuum)[2], map_id)
                requests = call.data["rooms"]
                if len(requests) > 128 or any(key not in targets for key in requests):
                    raise ValueError("Room settings must belong to existing rooms on the current map.")
                # Replace this floor only; retain preferences for other floors.
                rooms = {key: value for key, value in current["rooms"].items()
                         if not key.startswith(str(map_id) + "_")}
                for key, setup in requests.items():
                    rooms[key], _ = build_plan([], setup, caps, {}, map_id)
                profile = {"revision": current["revision"] + 1, "defaults": defaults, "rooms": rooms}
                updated = {**self.preferences, vacuum: profile}
                await self.preferences_store.async_save(updated)
                self.preferences = updated
                for listener in list(self.listeners):
                    listener()
            except PermissionError as err:
                raise Unauthorized(context=call.context, permission=POLICY_CONTROL) from err
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err

    @staticmethod
    def valid_stored_settings(value):
        """Validate storage shape without depending on a robot being online at startup."""
        if not isinstance(value, dict) or set(value) - {"mode", "suction", "water", "route", "repeat"}:
            return False
        return all(type(setting) is int and setting in {1, 2} if key == "repeat"
                   else isinstance(setting, str) for key, setting in value.items())

    async def load_saved_presets(self):
        """Keep unreadable storage intact instead of letting the next save replace it."""
        try:
            data = await self.preset_store.async_load()
            if data is not None and not isinstance(data, dict):
                raise ValueError("Invalid saved plan storage")
            for vacuum, plan in (data or {}).items():
                if (not isinstance(vacuum, str) or not isinstance(plan, dict)
                        or type(plan.get("revision", 0)) is not int or plan.get("revision", 0) < 0
                        or plan.get("source") not in {"rooms", "manual", "preset"}
                        or not isinstance(plan.get("rooms"), list)
                        or not self.valid_stored_settings(plan.get("setup"))):
                    raise ValueError("Invalid saved plan storage")
                for room in plan["rooms"]:
                    if not isinstance(room, str) and not (isinstance(room, dict)
                            and isinstance(room.get("id"), str)
                            and self.valid_stored_settings({key: value for key, value in room.items() if key != "id"})):
                        raise ValueError("Invalid saved room storage")
            self.saved_presets = data or {}
            self.presets_load_failed = False
        except Exception:  # noqa: BLE001 - do not overwrite the unreadable store
            self.presets_load_failed = True
            _LOGGER.error("Saved cleaning plans could not be read; saving is disabled to preserve them")

    async def read_saved_preset(self, vacuum, user_id):
        if self.presets_load_failed:
            raise ServiceValidationError("Saved plans could not be read. Restore plan storage before saving.")
        plan = self.saved_presets.get(vacuum)
        if plan:
            await async_require_control(self.hass.auth, user_id, [vacuum], POLICY_CONTROL)
        return plan

    async def save_preset(self, call: ServiceCall) -> None:
        """Persist a reusable plan; never configure or actuate the robot."""
        async with self.lock:
            if self.closing:
                raise ServiceValidationError("Home Assistant is stopping.")
            vacuum = call.data["vacuum"]
            try:
                await async_require_control(self.hass.auth, call.context.user_id, [vacuum], POLICY_CONTROL)
                current = await self.read_saved_preset(vacuum, call.context.user_id)
                revision = (current or {}).get("revision", 0)
                if "revision" in call.data and call.data["revision"] != revision:
                    raise ValueError("Another user saved the cleaning plan. Reload the saved plan before saving your changes.")
                plan = {"source": "rooms", "rooms": [], "setup": {}, "revision": revision + 1}
                caps, controls, targets, map_id = self.manual_capabilities(vacuum)
                await async_require_control(self.hass.auth, call.context.user_id, list(controls.values()), POLICY_CONTROL)
                requests = list(call.data.get("rooms", []))
                if requests and all(isinstance(item, dict) for item in requests):
                    rooms_map = robot_targets(self.resolve(vacuum)[2], map_id)
                    frozen, _ = build_room_plan(requests, call.data.get("setup", {}), caps, rooms_map, map_id)
                    plan.update(rooms=[{"id": room["id"], **room["setup"]} for room in frozen], map_id=map_id)
                else:
                    setup, _ = build_plan(requests, call.data.get("setup", {}), caps, targets, map_id)
                    plan.update(source="manual", rooms=requests, setup=setup, map_id=map_id)
                updated = {**self.saved_presets, vacuum: plan}
                await self.preset_store.async_save(updated)
                self.saved_presets = updated  # Report success only after durable storage.
                for listener in list(self.listeners):
                    listener()
            except PermissionError as err:
                raise Unauthorized(context=call.context, permission=POLICY_CONTROL) from err
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err

    def device_entities(self, vacuum: str) -> dict:
        registry, entry, coordinator = self.resolve(vacuum)
        return device_entities(entry, coordinator, registry.entities.values(), self.hass.states)

    def validate_device(self, vacuum, key, value):
        current = self.current_snapshot(vacuum)
        self.queue.validate_fresh_observation(current, time.time())
        self.queue._validate_command_barrier(current, time.time())
        if self.queue.phase in ACTIVE or self.queue.pending_command:
            raise ValueError("Wait for the active sequence or command before changing robot settings.")
        if self.queue.phase == "attention":
            if self.queue.vacuum and self.queue.vacuum != vacuum:
                raise ValueError("Clear the stopped sequence for the other robot first.")
            self.queue.validate_recovery(current, time.time())
        if not current.connected or current.vacuum in {"unknown", "unavailable"}:
            raise ValueError("The robot is unavailable.")
        if key == "selected_map" and not current.ready_for("vacuum"):
            raise ValueError("Finish the current job before changing maps.")
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
                self.queue.validate_fresh_observation(current, time.time())
                if key == "selected_map" and not current.ready_for("vacuum"):
                    raise ValueError("The robot became busy before the map change.")
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

    def room_entity(self, vacuum: str):
        """The native vacuum entity that can clean individual rooms."""
        data = getattr(self.hass, "data", None)
        component = data.get("vacuum") if isinstance(data, dict) else None
        entity = component.get_entity(vacuum) if hasattr(component, "get_entity") else None
        if entity is None or not hasattr(entity, "async_clean_segments"):
            raise ValueError("This robot's integration cannot clean individual rooms.")
        return entity

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
        self.robot_state = {"dock_status": current.status, "dock_drying": current.dock_drying,
                            "robot_observed_at": current.observed_at, "robot_activity": current.vacuum,
                            "robot_connected": current.connected, **self.recovery_state(current)}
        return current

    def recovery_state(self, current: Snapshot) -> dict:
        """Expose the same read-only recovery checks that authorize a new plan."""
        try:
            self.queue.validate_recovery(current, time.time())
        except ValueError as err:
            return {"recovery_ready": False, "recovery_reason": str(err)}
        return {"recovery_ready": True, "recovery_reason": ""}

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
            self.event("command", "received command=%s vacuum=%s user=%s rooms=%s" % (
                data.get("command"), data.get("vacuum") or self.queue.vacuum,
                call.context.user_id or "system", data.get("rooms") or []), logging.INFO)
            command = data["command"]
            vacuum = data.get("vacuum") or self.queue.vacuum
            saved_plan = None
            if command in {"toggle", "toggle_saved"}:
                # Decide under the same lock as starts and stage advancement.
                if self.queue.vacuum and vacuum != self.queue.vacuum and (self.queue.phase in ACTIVE or self.queue.pending_command):
                    raise ServiceValidationError("Another vacuum has an active command.")
                try:
                    finished = self.queue.should_finish(self.current_snapshot(vacuum))
                except (ValueError, TypeError, AttributeError) as err:
                    raise ServiceValidationError(str(err)) from err
                if finished:
                    command = "finish"
                else:
                    command = "start_manual"
                    if self.presets_load_failed or not isinstance(self.saved_presets, dict):
                        raise ServiceValidationError("Saved plans could not be read. Restore plan storage before starting.")
                    saved_plan = self.saved_presets.get(vacuum)
                    if not saved_plan:
                        raise ServiceValidationError("No cleaning plan is saved for this robot. Save one from the card first.")
                    if not isinstance(saved_plan, dict) or saved_plan.get("source") not in {"rooms", "manual"}:
                        raise ServiceValidationError("The saved plan uses retired Roborock routines. Select rooms and save a new plan from the card.")
                    missing = [key for key in ("rooms", "setup")
                               if not isinstance(saved_plan, dict) or key not in saved_plan]
                    if missing:
                        raise ServiceValidationError("The saved plan is incomplete. Save it again from the card.")
                    data.update(rooms=saved_plan["rooms"], setup=saved_plan["setup"])
            bound = self.queue.phase in ACTIVE or self.queue.phase == "attention" or bool(self.queue.pending_command)
            owned_plan = (self.queue.phase in ACTIVE or bool(self.queue.pending_command)) and self.queue.mode == "manual"
            standalone = command in {"pause", "resume", "return_to_dock", "stop"} and not owned_plan
            if standalone and not data.get("vacuum"):
                raise ServiceValidationError("Specify a vacuum when controlling a job outside an active queue.")
            if command != "start_manual" and bound and self.queue.vacuum and vacuum != self.queue.vacuum:
                raise ServiceValidationError("This command targets a different vacuum than the current queue.")
            # Queue dismissal and direct robot controls do not require permission on
            # obsolete setting entities from the stopped plan. Future starts/settings
            # validate their current entities independently before dispatch.
            permission_entities = [vacuum]
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
                        caps, controls, targets, map_id = self.manual_capabilities(vacuum)
                        if saved_plan is not None and saved_plan.get("map_id") != map_id:
                            raise ValueError("The saved plan belongs to another map. Select that map or save a new plan.")
                        requests = data["rooms"]
                        if requests and all(isinstance(item, dict) for item in requests):
                            rooms_map = robot_targets(self.resolve(vacuum)[2], map_id)
                            plan_rooms, stages = build_room_plan(requests, data.get("setup", {}), caps, rooms_map, map_id)
                            targets = {room["id"]: rooms_map[room["id"]] for room in plan_rooms}
                            data["rooms"] = [room["id"] for room in plan_rooms]
                            setup = {"rooms": plan_rooms}
                        else:
                            setup, stages = build_plan(requests, data.get("setup", {}), caps, targets, map_id)
                        try:
                            await async_require_control(self.hass.auth, call.context.user_id, list(controls.values()), POLICY_CONTROL)
                        except PermissionError as err:
                            raise Unauthorized(context=call.context, permission=POLICY_CONTROL) from err
                        address = "room" if requests and all(isinstance(item, dict) for item in requests) else "area"
                        effect = self.queue.start_manual(vacuum, data["rooms"], setup, stages, controls, current, time.time(), uuid4().hex)
                        self.queue.address = address
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
        if kind == "manual":
            return (self.queue.phase == "starting" and self.queue.pending_command == "start"
                    and self.queue.mode == "manual" and target == str(self.queue.current_index))
        return kind == "vacuum" and bool(self.queue.pending_command)

    async def clear_unsent_start(self, token) -> None:
        """An interrupted reservation is not an uncertain physical start."""
        current = (self.queue.run_id, self.queue.current_index, self.queue.command_at)
        if (token == current and token != self.start_dispatch_token
                and self.queue.phase in {"attention", "cancelled"}
                and not self.queue.pending_command and self.queue.not_before):
            self.queue.confirmed()
            await self.publish()

    async def execute(self, effect: tuple[str, str], parent: Context | None = None) -> None:
        kind, target = effect
        start_token = (self.queue.run_id, self.queue.current_index, self.queue.command_at)
        if not self.effect_valid(effect):
            if kind == "manual":
                await self.clear_unsent_start(start_token)
            return
        if kind == "manual" and start_token == self.start_dispatch_token:
            return  # A native start is never retried, including after an exception.
        user_id = parent.user_id if parent is not None else self.queue.owner_user_id
        attempted = False
        setting = None
        async def authorize():
            await async_require_control(self.hass.auth, user_id,
                [self.queue.vacuum, *(self.queue.control_entities.values()
                                     if kind in {"configure", "manual"} else [])], POLICY_CONTROL)
        try:
            await authorize()
            if not self.effect_valid(effect):
                return
            context = Context(user_id=user_id, parent_id=parent.id if parent else None)
            self.contexts.add(context.id)
            if len(self.contexts) > 128:
                self.contexts = {context.id}
            if kind == "configure":
                token = (self.queue.run_id, self.queue.current_index, self.queue.command_at)
                if token != self.configuration_token:
                    self.configuration_token = token
                    self.configured_keys = set()
                self.deferred_configuration = None
            if kind in {"manual", "configure"}:
                if kind == "configure" and self.current_snapshot(self.queue.vacuum).servicing_for(self.queue.cleaning_mode):
                    self.deferred_configuration = (token, effect)
                    return
                caps, controls, targets, map_id = self.manual_capabilities(self.queue.vacuum)
                if self.queue.address == "room":
                    targets = robot_targets(self.resolve(self.queue.vacuum)[2], map_id)
                if caps.get("unavailable_controls"):
                    self.event("settings-deferred", "native setting entities are unavailable: %s; waiting"
                               % ", ".join(caps["unavailable_controls"]), logging.INFO)
                    if kind == "configure":
                        self.deferred_configuration = (token, effect)
                        return
                    raise ValueError("Native controls are temporarily unavailable. No start was sent.")
                validate_stage(self.queue.stage, caps, targets, map_id)
                if self.control_state(controls) == "waiting":
                    if kind == "configure":
                        self.deferred_configuration = (token, effect)
                        return
                    raise ValueError("Native controls are temporarily unavailable. No start was sent.")
                if controls != self.queue.control_entities:
                    raise ValueError("The native manual control entities changed.")
                current = self.current_snapshot(self.queue.vacuum)
                self.queue.validate_fresh_observation(current, time.time())
                if not current.ready_for(self.queue.cleaning_mode):
                    raise ValueError("The robot is no longer ready to start a manual job.")
                if kind == "configure":
                    # High-level mode resets lower-level settings: always set it first.
                    for key in ("mode", "suction", "water", "route"):
                        if key not in self.queue.stage["settings"] or key in self.configured_keys:
                            continue
                        await authorize()
                        if not self.effect_valid(effect):
                            return
                        # A native-app start can appear during an awaited settings
                        # refresh without a Home Assistant service event.
                        current = self.current_snapshot(self.queue.vacuum)
                        if current.servicing_for(self.queue.cleaning_mode):
                            # A successful earlier write is retained. Resume only the
                            # unsent settings after normal post-clean dock care ends.
                            self.deferred_configuration = (token, effect)
                            self.event("settings-deferred", "waiting for dock care: %s" % current.status, logging.INFO)
                            return
                        if not current.ready_for(self.queue.cleaning_mode):
                            raise ValueError("The robot became busy while applying manual settings.")
                        latest_caps, latest_controls, latest_targets, latest_map = self.manual_capabilities(self.queue.vacuum)
                        if self.queue.address == "room":
                            latest_targets = robot_targets(self.resolve(self.queue.vacuum)[2], latest_map)
                        if latest_caps.get("unavailable_controls"):
                            self.deferred_configuration = (token, effect)
                            return
                        validate_stage(self.queue.stage, latest_caps, latest_targets, latest_map)
                        if self.control_state(latest_controls) == "waiting":
                            self.deferred_configuration = (token, effect)
                            return
                        if latest_controls != self.queue.control_entities:
                            raise ValueError("Manual control entities changed during setup.")
                        value = self.queue.stage["settings"][key]
                        self.queue.settings_sent_at = time.time()
                        await self.publish()  # Retain the latest possible write time across restart.
                        if not self.effect_valid(effect):
                            return
                        current = self.current_snapshot(self.queue.vacuum)
                        if current.servicing_for(self.queue.cleaning_mode):
                            self.deferred_configuration = (token, effect)
                            return
                        self.queue.validate_fresh_observation(current, time.time())
                        if not current.ready_for(self.queue.cleaning_mode):
                            raise ValueError("The robot became busy before applying manual settings.")
                        attempted = True
                        setting = key
                        if key == "suction":
                            await self.hass.services.async_call("vacuum", "set_fan_speed", {"entity_id": self.queue.vacuum, "fan_speed": value}, blocking=True, context=context)
                        else:
                            await self.hass.services.async_call("select", "select_option", {"entity_id": controls[key], "option": value}, blocking=True, context=context)
                        self.configured_keys.add(key)
                    # Tick uses freshly observed settings before issuing any start.
                    return
                current = self.current_snapshot(self.queue.vacuum)
                self.queue.validate_fresh_observation(current, time.time())
                if not all(current.settings.get(key) == value for key, value in self.queue.stage["settings"].items()):
                    raise ValueError("Manual settings changed before the start command.")
                area = self.queue.stage["target"]
                if self.queue.address == "room" and area:
                    entity = self.room_entity(self.queue.vacuum)
                    self.event("dispatch", "cleaning room %s" % area, logging.INFO)
                    # The native entity's own call is the only protocol-safe one: the V1
                    # entity takes "<map>_<room>" ids and ignores other maps, while the
                    # Q-series entities take bare segment ids. `coordinator.api` exists
                    # only for the Q-series classes.
                    attempted = True
                    self.start_dispatch_token = start_token
                    await entity.async_clean_segments([area])
                else:
                    data = {"entity_id": self.queue.vacuum}
                    if area:
                        data["cleaning_area_id"] = [area]
                    attempted = True
                    self.start_dispatch_token = start_token
                    await self.hass.services.async_call("vacuum", "clean_area" if area else "start", data, blocking=True, context=context)
            else:
                current = self.current_snapshot(self.queue.vacuum)
                self.queue.validate_fresh_observation(current, time.time())
                self.queue._validate_command_barrier(current, time.time())
                if not self.queue.validate_control_state(self.queue.pending_command, current):
                    return
                attempted = True
                await self.hass.services.async_call("vacuum", target, {"entity_id": self.queue.vacuum}, blocking=True, context=context)
        except PermissionError:
            if kind == "manual" and not attempted:
                self.queue.confirmed()  # A reserved start that never reached the native API.
            self.queue.attention("The initiating user can no longer control this cleaning sequence. No further command was sent.")
            await self.publish()
        except Exception as err:  # noqa: BLE001 - observe uncertain starts, never resend
            # HA wraps native errors, and its transport may already have fallen back
            # between local/cloud. Even a rejection cannot prove no earlier attempt
            # was accepted. Preserve safe cause metadata, never messages/payloads.
            failure = command_failure_metadata(err, kind if kind != "vacuum" else self.queue.pending_command)
            failure["attempted"] = attempted
            failure["time"] = time.time()
            if setting is not None:
                failure["setting"] = setting
            self.queue.command_failure = failure
            self.event("command-failed", str(failure), logging.WARNING)
            if kind == "manual" and attempted and self.effect_valid(effect):
                self.queue.start_uncertain = True
                self.queue.decision = "The start response is uncertain; watching native status without resending."
            else:
                if kind == "manual" and not attempted:
                    self.queue.confirmed()
                self.queue.attention("The %s command %s (%s). Check the robot; no automatic retry was sent." % (
                    "room start" if kind == "manual" else "settings" if kind == "configure" else "robot",
                    "failed or could not be confirmed" if attempted else "was not sent",
                    failure["category"]))
            await self.publish()
        finally:
            if kind == "manual" and not attempted:
                await self.clear_unsent_start(start_token)

    async def tick(self, _now=None) -> None:
        if self.closing or not self.queue.vacuum:
            return
        async with self.lock:
            try:
                current = self.current_snapshot(self.queue.vacuum)
            except ValueError:
                current = Snapshot()
                self.robot_state = {"dock_status": "unavailable", "dock_drying": None,
                                    "robot_observed_at": 0, "robot_activity": "unavailable", "robot_connected": False,
                                    "recovery_ready": False,
                                    "recovery_reason": "The robot is unavailable."}
            if self.queue.mode == "device":
                if self.queue.pending_command:
                    self.observe_device()
                await self.publish()
                return
            if self.queue.phase not in ACTIVE and not self.queue.pending_command:
                await self.publish()  # Keep native dock care visible after floor completion.
                return
            self.observe_log(current)
            effect = self.queue.observe(current, time.time())
            await self.publish()
            if effect:
                await self.execute(effect)
            elif self.deferred_configuration:
                token, deferred = self.deferred_configuration
                current_token = (self.queue.run_id, self.queue.current_index, self.queue.command_at)
                if token == current_token and self.effect_valid(deferred):
                    await self.execute(deferred)
                else:
                    self.deferred_configuration = None

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
            # Set synchronously before a waiting tick can start another room.
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
