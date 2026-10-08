"""Allowlisted native controls discovered by registry identity, never entity names."""
from __future__ import annotations
import math
import re

# (native unique-id prefix, entity domain). Dock entities have their own device ID.
CONTROLS = {
    "dust_emptying": ("dust_emptying", "switch"), "mop_washing": ("mop_washing", "switch"),
    "mop_drying": ("mop_drying", "switch"), "child_lock": ("child_lock", "switch"),
    "dnd": ("dnd_switch", "switch"), "dnd_start": ("dnd_start_time", "time"),
    "dnd_end": ("dnd_end_time", "time"), "volume": ("volume", "number"),
    "empty_mode": ("dust_collection_mode", "select"),
    "selected_map": ("selected_map", "select"),
    "off_peak": ("off_peak_switch", "switch"),
    "off_peak_start": ("off_peak_start", "time"), "off_peak_end": ("off_peak_end", "time"),
}
MAINTENANCE = {key: (key, "sensor") for key in (
    "main_brush_time_left", "side_brush_time_left", "filter_time_left", "sensor_time_left",
    "strainer_time_left", "cleaning_brush_time_left")}
TELEMETRY = {
    "mop_attached": ("water_box_carriage_status", "binary_sensor"),
    "water_box_attached": ("water_box_status", "binary_sensor"),
    "clean_water_empty": ("clean_box_empty", "binary_sensor"),
    "dirty_water_full": ("dirty_box_full", "binary_sensor"),
    "drying_remaining": ("mop_clean_remaining", "sensor"),
    "last_clean_begin": ("last_clean_start", "sensor"), "last_clean_end": ("last_clean_end", "sensor"),
    **{key:(key,"sensor") for key in ("total_cleaning_time", "total_cleaning_area", "total_cleaning_count")},
}
DOCK = {"dust_emptying", "mop_washing", "mop_drying"}


def device_entities(vacuum_entry, coordinator, entries, states):
    result = {}
    slug = coordinator.duid_slug
    for entry in entries:
        if (entry.platform != "roborock" or entry.config_entry_id != vacuum_entry.config_entry_id
                or getattr(entry, "disabled_by", None)):
            continue
        state = states.get(entry.entity_id)
        if state is None or state.state in {"unavailable", "unknown"}:
            continue
        for key, (prefix, domain) in {**CONTROLS, **MAINTENANCE, **TELEMETRY}.items():
            if entry.domain == domain and entry.unique_id == f"{prefix}_{slug}":
                result[key] = entry.entity_id
        if (entry.domain == "image" and entry.device_id == vacuum_entry.device_id
                and entry.unique_id.startswith(slug + "_map_")
                and entry.unique_id != slug + "_map_"):
            # Include all enabled maps; no map-changing service is exposed.
            # HA V1 uses the map name (including spaces) in legacy unique IDs.
            result["map_" + entry.entity_id] = entry.entity_id
    return result


def device_command(key, value, entity_id, state):
    if key not in CONTROLS or state is None or state.state in {"unknown", "unavailable"}:
        raise ValueError("This native control is unavailable.")
    domain = CONTROLS[key][1]
    data = {"entity_id": entity_id}
    if domain == "switch":
        if value not in {"on", "off"}:
            raise ValueError("Choose on or off.")
        return domain, "turn_" + value, data, value
    if domain == "select":
        if value not in state.attributes.get("options", []) or value in {"unknown", "unavailable"}:
            raise ValueError("Choose an available option.")
        return domain, "select_option", {**data, "option": value}, value
    if domain == "time":
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?", value):
            raise ValueError("Choose a valid time.")
        value = value if len(value) == 8 else value + ":00"
        return domain, "set_value", {**data, "time": value}, value
    if isinstance(value, bool):
        raise ValueError("Choose a valid volume.")
    value = float(value)
    if not math.isfinite(value) or not state.attributes.get("min", 0) <= value <= state.attributes.get("max", 100):
        raise ValueError("Volume is outside the native range.")
    return domain, "set_value", {**data, "value": value}, str(value)
