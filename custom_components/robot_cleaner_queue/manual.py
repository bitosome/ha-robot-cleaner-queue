"""Allowlisted cleaning settings, robot-room plans and legacy area plans from the native cache.

No discovery, polling, raw commands, map switching, credentials, or preset presses.
Compatibility: Home Assistant 2026.9.4 / python-roborock 7.4.2.
"""
from __future__ import annotations
from typing import Any

MODES = {"vacuum": "vacuum", "vacuum_mop": "vac_and_mop", "mop": "mop"}
MODE_KEYS = {native: key for key, native in MODES.items()}
LABELS = {"vacuum": "Vacuum", "vacuum_mop": "Vacuum & mop", "mop": "Mop", "vacuum_then_mop": "Vacuum then mop"}
SELECT_KEYS = {"mode": "cleaning_mode", "water": "water_box_mode", "route": "mop_mode"}
# homeassistant.components.vacuum.VacuumEntityFeature.CLEAN_AREA
AREA_FEATURE = 16384
EXCLUDE = {"off", "off_raise_main_brush", "gentle", "smart_mode", "custom", "custom_water_flow"}


def cached_settings(coordinator: Any) -> dict[str, str]:
    status = getattr(getattr(coordinator, "properties_api", None), "status", None)
    properties = {"mode": "current_cleaning_mode_name", "suction": "fan_speed_name", "water": "water_mode_name", "route": "mop_route_name"}
    return {key: value for key, prop in properties.items() if isinstance(value := getattr(status, prop, None), str)}


def controls(entries, vacuum_entry, coordinator, states) -> dict[str, str]:
    """Resolve exact native select identities, including user-renamed entity IDs."""
    result = {}
    for entry in entries:
        if (entry.platform != "roborock" or entry.domain != "select" or entry.disabled_by
                or entry.device_id != vacuum_entry.device_id or entry.config_entry_id != vacuum_entry.config_entry_id):
            continue
        for key, native in SELECT_KEYS.items():
            state = states.get(entry.entity_id)
            if entry.unique_id == f"{native}_{coordinator.duid_slug}" and state and state.state not in {"unknown", "unavailable"}:
                result[key] = entry.entity_id
    return result


def unavailable_controls(entries, vacuum_entry, coordinator, states) -> list[str]:
    """Native setting entities that exist but cannot be read or written right now.

    The Roborock integration marks setting entities unavailable while the dock
    services. The plan itself is unchanged, so a queue should wait for them instead
    of treating an unreadable select as an unsupported setting.
    """
    keys = set()
    for entry in entries:
        if (entry.platform != "roborock" or entry.domain != "select" or getattr(entry, "disabled_by", None)
                or entry.device_id != vacuum_entry.device_id
                or entry.config_entry_id != vacuum_entry.config_entry_id):
            continue
        for key, native in SELECT_KEYS.items():
            if entry.unique_id == f"{native}_{coordinator.duid_slug}":
                state = states.get(entry.entity_id)
                if state is None or state.state in {"unknown", "unavailable"}:
                    keys.add(key)
    return sorted(keys)


def current_map(coordinator) -> tuple[int | None, set[str]]:
    api = getattr(coordinator, "properties_api", None)
    flag = getattr(getattr(api, "maps", None), "current_map", None)
    info = getattr(getattr(api, "home", None), "current_map_data", None)
    if not isinstance(flag, int) or isinstance(flag, bool) or info is None or getattr(info, "map_flag", None) != flag:
        return None, set()
    segments = {f"{flag}_{r.segment_id}" for r in getattr(info, "rooms", []) if isinstance(getattr(r, "segment_id", None), int)}
    return flag, segments


def area_mapping(vacuum_entry) -> dict:
    """Home Assistant stores the vacuum's area mapping in the entity registry options."""
    options = getattr(vacuum_entry, "options", None)
    vacuum_options = options.get("vacuum") if isinstance(options, dict) else None
    mapping = vacuum_options.get("area_mapping") if isinstance(vacuum_options, dict) else None
    return mapping if isinstance(mapping, dict) else {}


def _rooms(map_info, flag) -> list[dict]:
    result = []
    for room in getattr(map_info, "rooms", None) or []:
        segment = getattr(room, "segment_id", None)
        if not isinstance(segment, int) or isinstance(segment, bool):
            continue
        label = getattr(room, "name", None)
        result.append({"id": f"{flag}_{segment}", "segment": segment,
                       "name": label.strip() if isinstance(label, str) and label.strip() else None})
    return result


def home_maps(coordinator) -> tuple[list[dict], bool]:
    """Every map the robot reports, with the room names owned by the Roborock app.

    `home_map_info` carries all maps; older cached structures expose only the current
    map, which is still reported so a mapping can be reviewed without inventing rooms.
    The flag says whether every map was readable, because only then can an area be
    called stale.
    """
    home = getattr(getattr(coordinator, "properties_api", None), "home", None)
    info = getattr(home, "home_map_info", None)
    if isinstance(info, dict) and info:
        maps = []
        for flag, map_info in info.items():
            if not isinstance(flag, int) or isinstance(flag, bool):
                continue
            label = getattr(map_info, "name", None)
            maps.append({"flag": flag, "name": label.strip() if isinstance(label, str) and label.strip() else None,
                         "rooms": _rooms(map_info, flag)})
        return maps, bool(maps)
    info = getattr(home, "current_map_data", None)
    flag = getattr(info, "map_flag", None)
    if not isinstance(flag, int) or isinstance(flag, bool):
        return [], False
    return [{"flag": flag, "name": None, "rooms": _rooms(info, flag)}], False


def room_report(vacuum_entry, coordinator, area_registry) -> dict:
    """Read-only view of the robot's rooms and the areas that claim them.

    Reports the Roborock app's own names next to the Home Assistant areas, so an
    operator can see how the robot's finer segmentation is grouped into real rooms,
    which robot rooms no area covers, and which areas point at rooms the robot no
    longer reports. It never writes configuration.
    """
    maps, complete = home_maps(coordinator)
    known = {room["id"] for entry in maps for room in entry["rooms"]}
    # Areas that are selectable right now must never be called stale.
    known |= current_map(coordinator)[1]
    mapping = area_mapping(vacuum_entry)
    owners: dict[str, str] = {}
    for area_id, segments in mapping.items():
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if isinstance(segment, str) and segment not in owners:
                owners[segment] = area_id
    rooms = []
    for entry in maps:
        for room in entry["rooms"]:
            area_id = owners.get(room["id"])
            area = area_registry.async_get_area(area_id) if area_id else None
            rooms.append({**room, "floor": entry["name"],
                          "area_id": area_id if area is not None else None,
                          "area_name": area.name if area is not None else None})
    areas = []
    for area_id, segments in mapping.items():
        if not complete or not isinstance(segments, list) or not segments:
            continue
        area = area_registry.async_get_area(area_id)
        listed = [segment for segment in segments if isinstance(segment, str)]
        if area is not None and listed and not set(listed) <= known:
            areas.append({"id": area_id, "name": area.name, "segments": listed})
    return {"maps": [{"flag": e["flag"], "name": e["name"]} for e in maps], "rooms": rooms,
            "unmapped_areas": areas, "complete": complete}


def room_targets(vacuum_entry, coordinator, area_registry) -> dict[str, dict]:
    _, known = current_map(coordinator)
    mapping = area_mapping(vacuum_entry)
    result = {}
    for area_id, segments in mapping.items():
        area = area_registry.async_get_area(area_id)
        if area is None or not isinstance(segments, list) or not segments or not all(isinstance(s, str) and s in known for s in segments):
            continue
        if len(set(segments)) != len(segments):
            continue
        result[area_id] = {"id": area_id, "name": area.name, "segments": list(segments)}
        if isinstance(icon := getattr(area, "icon", None), str) and icon.strip():
            result[area_id]["icon"] = icon
    return result


def capabilities(vacuum_entry, coordinator, entries, states, area_registry) -> tuple[dict, dict[str, str], dict]:
    selected = controls(entries, vacuum_entry, coordinator, states)
    current = cached_settings(coordinator)
    status = getattr(getattr(coordinator, "properties_api", None), "status", None)
    def native_options(prop):
        # Only the select entity's own options are accepted by select_option, and the
        # native trait may label a setting by value or by display name. Accept both
        # rather than assuming one family, so a model that names them differently still
        # offers every setting it really exposes.
        names = set()
        for item in (getattr(status, prop, None) or []):
            for candidate in (getattr(item, "value", item), getattr(item, "display_name", None)):
                if isinstance(candidate, str):
                    names.add(candidate)
        return names
    def options(key, native_prop):
        state = states.get(selected.get(key, ""))
        exposed = state.attributes.get("options", []) if state else []
        native = native_options(native_prop)
        return [v for v in exposed if isinstance(v, str) and v in native and v not in EXCLUDE]
    water = options("water", "water_mode_options")
    routes = options("route", "mop_route_options")
    mode_state = states.get(selected.get("mode", ""))
    # The select may expose a native value or a display name, so map each exposed
    # option back to the mode it means instead of comparing literals.
    mode_keys = {}
    for item in (getattr(status, "cleaning_mode_options", None) or []):
        value = getattr(item, "value", item)
        key = MODE_KEYS.get(value) if isinstance(value, str) else None
        if key is None:
            continue
        for candidate in (value, getattr(item, "display_name", None)):
            if isinstance(candidate, str):
                mode_keys.setdefault(candidate, key)
    exposed_modes = [v for v in (mode_state.attributes.get("options", []) if mode_state else []) if isinstance(v, str)]
    modes = {mode_keys[option] for option in exposed_modes if option in mode_keys}
    vac_state = states.get(vacuum_entry.entity_id)
    native_fan = native_options("fan_speed_options")
    suction = [v for v in (vac_state.attributes.get("fan_speed_list", []) if vac_state else []) if isinstance(v, str) and v in native_fan and v not in EXCLUDE]
    offered = [key for key in MODES if key in modes and (key == "mop" or suction) and (key == "vacuum" or water)]
    if "vacuum" in offered and "mop" in offered:
        offered.append("vacuum_then_mop")
    features = int(getattr(vac_state, "attributes", {}).get("supported_features", 0) or 0) if vac_state is not None else 0
    area_cleaning = bool(features & AREA_FEATURE)
    targets = room_targets(vacuum_entry, coordinator, area_registry) if area_cleaning else {}
    flag, _ = current_map(coordinator)
    healthy = bool(getattr(coordinator, "last_update_success", False)) and vac_state is not None and vac_state.state not in {"unknown", "unavailable"}
    supported = bool(offered and healthy and flag is not None)
    def default(key, values, preferred):
        return current.get(key) if current.get(key) in values else preferred if preferred in values else values[0] if values else None
    current_mode = next((k for k, v in MODES.items() if v == current.get("mode")), None)
    defaults = {"mode": current_mode if current_mode in offered else offered[0] if offered else None, "repeat": 1}
    for key, vals, preferred in [("suction", suction, "balanced"), ("water", water, "medium"), ("route", routes, "standard")]:
        if (value := default(key, vals, preferred)) is not None:
            defaults[key] = value
    def is_fast_route(value):
        return value.strip().lower() in {"standard", "fast"}
    routes_by_mode = {"vacuum": [], "vacuum_mop": [r for r in routes if is_fast_route(r)], "mop": routes, "vacuum_then_mop": routes}
    valid_routes = routes_by_mode.get(defaults["mode"], [])
    if defaults.get("route") not in valid_routes:
        defaults.pop("route", None)
        if valid_routes:
            standard = next((r for r in valid_routes if r.strip().lower() == "standard"), None)
            defaults["route"] = standard if standard is not None else valid_routes[0]
    unavailable = unavailable_controls(entries, vacuum_entry, coordinator, states)
    result = {"supported": supported, "unavailable_controls": unavailable,
              "modes": [{"value": v, "label": LABELS[v]} for v in offered],
              "suction": suction, "water": water, "routes": routes, "routes_by_mode": routes_by_mode,
              "repeats": [1, 2], "area_cleaning": area_cleaning, "room_targets": [{k: t[k] for k in ("id", "name", "icon") if k in t} for t in targets.values()], "defaults": defaults}
    report = room_report(vacuum_entry, coordinator, area_registry)
    result["robot_maps"] = report["maps"]
    result["robot_rooms"] = report["rooms"]
    result["unmapped_areas"] = report["unmapped_areas"]
    result["rooms_complete"] = report["complete"]
    if not supported:
        result["error"] = "Manual cleaning requires an available native Roborock robot, supported cleaning-mode controls, and a known current map."
    return result, selected, targets


def build_plan(rooms: list[str], setup: dict, caps: dict, targets: dict, map_id: int) -> tuple[dict, list[dict]]:
    """Validate caller input and freeze settings, map and area mappings for each job."""
    if not caps.get("supported"):
        raise ValueError(caps.get("error", "Manual cleaning is not supported by this robot."))
    if not isinstance(rooms, list) or len(rooms) > 32 or any(not isinstance(r, str) for r in rooms) or len(set(rooms)) != len(rooms):
        raise ValueError("Select at most 32 distinct mapped areas.")
    if any(r not in targets for r in rooms):
        raise ValueError("Every selected area must be mapped to existing rooms on the robot's current map.")
    segments = [s for room in rooms for s in targets[room]["segments"]]
    if len(set(segments)) != len(segments):
        raise ValueError("Selected areas overlap on the robot map. Select each physical room only once.")
    if not isinstance(setup, dict) or set(setup) - {"mode", "suction", "water", "route", "repeat"}:
        raise ValueError("Unknown manual cleaning settings.")
    mode = setup.get("mode")
    if mode not in {m["value"] for m in caps["modes"]}:
        raise ValueError("Choose a supported manual cleaning mode.")
    repeat = setup.get("repeat", 1)
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat not in caps["repeats"]:
        raise ValueError("Choose one or two cleaning runs per area.")
    normalized = {"mode": mode, "repeat": repeat}
    for key, relevant in [("suction", mode != "mop"), ("water", mode != "vacuum"), ("route", mode != "vacuum")]:
        values = caps["routes_by_mode"][mode] if key == "route" else caps[key]
        if not relevant:
            if key in setup:
                raise ValueError(f"{key.title()} is not a setting for the selected cleaning mode.")
            continue
        value = setup.get(key, caps["defaults"].get(key))
        if not values:
            if key in setup:
                raise ValueError(f"The robot does not expose {key} for this mode.")
            continue
        if key not in setup and value not in values:
            value = "standard" if key == "route" and "standard" in values else values[0]
        if value not in values:
            raise ValueError(f"Choose a supported {key} option for this cleaning mode.")
        normalized[key] = value
    phases = ["vacuum", "mop"] if mode == "vacuum_then_mop" else [mode]
    stages = []
    for pass_index, phase in enumerate(phases):
        for room_index, target in enumerate(rooms or [""]):
            for repeat_index in range(repeat):
                settings = {"mode": MODES[phase]}
                if phase != "mop" and "suction" in normalized:
                    settings["suction"] = normalized["suction"]
                if phase != "vacuum":
                    settings.update({k: normalized[k] for k in ("water", "route") if k in normalized})
                stages.append({"target": target, "mode": phase, "room_index": room_index, "pass_index": pass_index,
                               "repeat_index": repeat_index, "settings": settings, "map_id": map_id,
                               "segments": list(targets[target]["segments"]) if target else []})
    return normalized, stages


def robot_targets(coordinator, map_id: int | None) -> dict[str, dict]:
    """The robot's own rooms on one map, shaped like area targets.

    Each room is a target whose single segment is that room, so the existing plan
    builder, validator and stage freeze are reused unchanged while addressing the
    robot's rooms directly instead of Home Assistant areas.
    """
    if map_id is None:
        return {}
    result = {}
    for entry in home_maps(coordinator)[0]:
        if entry["flag"] != map_id:
            continue
        for room in entry["rooms"]:
            segment = room.get("segment")
            if not isinstance(segment, int):
                continue
            result[room["id"]] = {"id": room["id"], "name": room["name"] or room["id"],
                                  "segments": [str(segment)], "segment": segment}
    return result


def build_room_plan(requests: list, defaults: dict, caps: dict, targets: dict,
                    map_id: int) -> tuple[list[dict], list[dict]]:
    """Freeze an ordered room plan where every room carries its own settings.

    A request is {"id": <room id>, **settings}; anything it omits falls back to the
    plan defaults, and each room is validated exactly like a single-room plan.
    """
    if not isinstance(requests, list) or not 1 <= len(requests) <= 32:
        raise ValueError("Select between 1 and 32 rooms.")
    if not isinstance(defaults, dict) or set(defaults) - {"mode", "suction", "water", "route", "repeat"}:
        raise ValueError("Unknown cleaning settings.")
    plan_rooms, stages, seen = [], [], set()
    for room_index, request in enumerate(requests):
        if not isinstance(request, dict) or not isinstance(request.get("id"), str):
            raise ValueError("Every room needs an id from get_capabilities.")
        room_id = request["id"]
        spec = {key: value for key, value in request.items() if key != "id"}
        if set(spec) - {"mode", "suction", "water", "route", "repeat"}:
            raise ValueError("Unknown cleaning settings.")
        if room_id in seen:
            raise ValueError("Select each room only once.")
        seen.add(room_id)
        if room_id not in targets:
            raise ValueError("Every selected room must exist on the robot's current map.")
        merged = {**defaults, **spec}
        # Discard only inherited settings made irrelevant by an explicit room mode.
        # Explicitly contradictory settings are still rejected by build_plan.
        irrelevant = {"water", "route"} if merged.get("mode") == "vacuum" else {"suction"} if merged.get("mode") == "mop" else set()
        for key in irrelevant - spec.keys():
            merged.pop(key, None)
        normalized, room_stages = build_plan([room_id], merged, caps, targets, map_id)
        for stage in room_stages:
            stage["room_index"] = room_index
        if len(stages) + len(room_stages) > 128:
            raise ValueError("The cleaning plan exceeds 128 stages.")
        plan_rooms.append({"id": room_id, "name": targets[room_id]["name"], "setup": normalized})
        stages.extend(room_stages)
    return plan_rooms, stages


def validate_stage(stage: dict, caps: dict, targets: dict, map_id: int | None) -> None:
    """Never silently clean a changed map or a different area mapping."""
    if not caps.get("supported") or map_id is None or map_id != stage.get("map_id"):
        raise ValueError("The robot map or manual-cleaning capabilities changed. Review the cleaning plan.")
    target = stage["target"]
    if target and (target not in targets or targets[target]["segments"] != stage.get("segments")):
        raise ValueError("A selected area's room mapping changed. Review the cleaning plan.")
    mode = stage["mode"]
    if mode not in {m["value"] for m in caps["modes"]}:
        raise ValueError("The planned cleaning mode is no longer available.")
    for key, value in stage["settings"].items():
        options = [MODES[mode]] if key == "mode" else caps["routes_by_mode"][mode] if key == "route" else caps[key]
        if value not in options:
            raise ValueError("A planned manual setting is no longer available.")
