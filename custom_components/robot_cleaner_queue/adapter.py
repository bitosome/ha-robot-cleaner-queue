"""Read cached Roborock V1 data; no credential access or additional device polling.

Compatibility boundary: Home Assistant 2026.9.4 / python-roborock 7.4.2.
If the native integration changes this structure, the queue fails closed.
"""
from __future__ import annotations
from typing import Any
from .engine import Snapshot

# The native library recognizes these V1 states, but HA 2026.9.4's vacuum
# activity table does not. HA therefore exposes "unknown" during real mop work.
# Translate only these verified numeric omissions; unknown firmware codes and
# unavailable/disconnected entities remain unavailable to the queue.
# python-roborock 7.4.2: data/v1/v1_code_mappings.py, RoborockStateCode.
NATIVE_ACTIVITY_FALLBACK = {
    25: "docked",  # washing_the_mop_2; state_name is "washing_the_mop"
    202: "docked",  # air_drying_stopping
    6301: "cleaning",  # robot_status_mopping
    6302: "cleaning",  # clean_mop_cleaning
    6303: "cleaning",  # clean_mop_mopping
    6304: "cleaning",  # segment_mopping
    6305: "cleaning",  # segment_clean_mop_cleaning
    6306: "cleaning",  # segment_clean_mop_mopping
    6307: "cleaning",  # zoned_mopping
    6308: "cleaning",  # zoned_clean_mop_cleaning
    6309: "cleaning",  # zoned_clean_mop_mopping
    6310: "returning",  # back_to_dock_washing_duster
}


def number(value: Any) -> int | float | None:
    value = getattr(value, "value", value)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def clean_record(record: Any) -> dict[str, Any] | None:
    if record is None:
        return None
    return {key: number(getattr(record, key, None)) for key in ("begin", "end", "complete", "error", "finish_reason")}


def snapshot(coordinator: Any, vacuum_state: str) -> Snapshot:
    data = getattr(coordinator, "data", None)
    status = getattr(data, "status", None)
    if status is None:
        return Snapshot()
    job = number(getattr(status, "in_cleaning", None))
    error = number(getattr(status, "error_code", None))
    dock_error = number(getattr(status, "dock_error_status", None))
    connected = bool(getattr(coordinator, "last_update_success", False))
    if vacuum_state == "unknown" and connected:
        vacuum_state = NATIVE_ACTIVITY_FALLBACK.get(number(getattr(status, "state", None)), vacuum_state)
    return Snapshot(
        vacuum=vacuum_state,
        status=getattr(status, "state_name", None) or "unavailable",
        job="unavailable" if job is None else "on" if job else "off",
        error="none" if error == 0 else "unavailable" if error is None else "error",
        # Native RoborockDockErrorCode.water_empty == 38 (python-roborock 7.4.2).
        # Only this known water-only fault is eligible for vacuum-only cleaning.
        dock_error="ok" if dock_error in (None, 0) else "water_empty" if dock_error == 38 else
                   (getattr(getattr(status, "dock_error_status", None), "name", None) or "error"),
        connected=connected,
        dock_drying=(None if getattr(status, "dry_status", None) is None
                     else bool(getattr(status, "dry_status"))),
        record=clean_record(getattr(getattr(data, "clean_summary", None), "last_clean_record", None)),
        observed_at=(getattr(coordinator, "_last_update_success_time", None).timestamp()
                     if getattr(coordinator, "_last_update_success_time", None) is not None else 0),
    )


def routine_matches(registry_entry: Any, vacuum_entry: Any, coordinator: Any) -> bool:
    """Native routine unique IDs are numeric scene IDs plus the robot's slug.

    This excludes arbitrary buttons, other robots, and maintenance-reset buttons.
    An unknown button state is valid before the first press.
    """
    if registry_entry is None or vacuum_entry is None:
        return False
    suffix = "_" + str(getattr(coordinator, "duid_slug", ""))
    unique_id = str(getattr(registry_entry, "unique_id", ""))
    prefix = unique_id[:-len(suffix)] if suffix != "_" and unique_id.endswith(suffix) else ""
    return bool(
        getattr(registry_entry, "platform", None) == "roborock"
        and getattr(registry_entry, "domain", None) == "button"
        and getattr(registry_entry, "device_id", None) == getattr(vacuum_entry, "device_id", None)
        and getattr(registry_entry, "config_entry_id", None) == getattr(vacuum_entry, "config_entry_id", None)
        and not getattr(registry_entry, "disabled_by", None)
        and prefix.isdecimal()
    )


def is_competing_command(domain: str, service: str, data: dict, vacuum: str, is_routine, is_setting=lambda _entity: False) -> bool:
    """call_service fires before HA expands device/area/label/floor targets."""
    targets = data.get("entity_id", [])
    targets = [targets] if isinstance(targets, str) else (targets or [])
    ambiguous = not targets or "all" in targets or any(
        key in data for key in ("device_id", "area_id", "label_id", "floor_id")
    )
    if domain == "vacuum" and service in {"start", "stop", "pause", "return_to_base", "clean_area", "clean_spot", "send_command", "set_fan_speed"}:
        return ambiguous or vacuum in targets
    if domain == "roborock" and service in {"set_vacuum_zoned_cleaning", "set_vacuum_goto_position"}:
        return ambiguous or vacuum in targets
    if domain == "select" and service in {"select_option", "select_next", "select_previous", "select_first", "select_last"}:
        return ambiguous or any(is_setting(eid) for eid in targets)
    if domain == "switch" and service in {"turn_on", "turn_off", "toggle"}:
        return ambiguous or any(is_setting(eid) for eid in targets)
    if domain == "button" and service == "press":
        return ambiguous or any(is_routine(eid) for eid in targets)
    return False
