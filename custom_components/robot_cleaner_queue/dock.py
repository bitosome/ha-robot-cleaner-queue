"""Durable, evidence-driven dock care; service acceptance is never completion.

The checkpoint belongs to a cleaning run, independently of its floor-work phase.
Only observed/verified floor work creates hygiene obligations. Native care can
satisfy them; missing care is requested once after settled docking. An uncertain
request waits for explicit, revision-guarded retry instead of being replayed.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite

VERSION = 1
ACTIONS = ("dust_emptying", "mop_washing")
ACTIVE_STATUS = {"dust_emptying": {"emptying_the_bin"},
                 "mop_washing": {"washing_the_mop", "washing_the_mop_2"}}
LABELS = {"dust_emptying": "Dustbin emptying", "mop_washing": "Mop washing"}


def timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0


def required(queue):
    cp = queue.dock_cleanup
    return bool(cp and cp.get("state") != "completed" and any(cp.get("required", {}).values()))


def state(queue):
    return deepcopy(queue.dock_cleanup)


def _touch(queue, before):
    cp = queue.dock_cleanup
    if cp != before:
        cp["revision"] = int(before.get("revision", 0)) + 1


def _ensure(queue):
    if not queue.dock_cleanup:
        queue.dock_cleanup = {"version": VERSION, "run_id": queue.run_id, "revision": 0,
            "state": "pending", "required": {key: False for key in ACTIONS}, "actions": {},
            "reason": "", "retry_allowed": False, "armed": False, "outcome": "cancelled",
            "begun_at": 0, "ready_since": 0, "ready_observed_at": 0}
    return queue.dock_cleanup


def used(queue, mode, at):
    """Record physical use, never planned settings or command acknowledgement."""
    if not timestamp(at) or at <= 0:
        return
    keys = (["dust_emptying"] if mode == "vacuum" else ["mop_washing"] if mode == "mop"
            else list(ACTIONS) if mode in {"vacuum_mop", "vac_and_mop", "vacuum_then_mop"} else [])
    if not keys:
        return
    cp = _ensure(queue)
    before = deepcopy(cp)
    for key in keys:
        action = cp["actions"].get(key, {})
        if at <= action.get("used_at", 0):
            continue
        cp["required"][key] = True
        if action.get("started_at") and action["started_at"] >= at:
            action["used_at"] = at
            continue  # A late completion record predates already observed care.
        cp["actions"][key] = {"state": "pending", "used_at": at, "started_at": None,
                              "completed_at": None, "command_at": None}
    cp["state"] = "pending"
    _touch(queue, before)


def observe_usage(queue, snapshot, now):
    from .engine import CLEANING_STATUS
    from . import recovery
    normal = queue.mode == "manual" and queue.phase in {"starting", "running", "paused"}
    interrupted_owned = (queue.mode == "manual" and queue.phase in {"attention", "cancelled"}
                         and recovery.matches_owned_job(queue, snapshot))
    delayed_start = (queue.mode == "finish" and queue.phase == "controlling"
                     and queue.not_before > 0 and now <= queue.not_before)
    if not (normal or interrupted_owned or delayed_start):
        return
    if (queue.mode in {"manual", "finish"} and queue.phase in {"starting", "running", "paused", "attention", "cancelled", "controlling"}
            and not queue.next_pending and queue.stage and snapshot.job == "on"
            and snapshot.status in CLEANING_STATUS and snapshot.robot_healthy
            and queue.started_at > 0 and queue.started_at <= snapshot.observed_at <= now
            and now - snapshot.observed_at <= 90):
        used(queue, queue.stage.get("mode"), snapshot.observed_at)


def restore(queue):
    """Validate persisted evidence; malformed checkpoints never authorize motion."""
    cp = queue.dock_cleanup
    if not cp:
        # Old versions already retain actual per-pass start/completion evidence.
        run = queue.last_run or {}
        if run.get("run_id") == queue.run_id and queue.mode == "manual":
            for stage in run.get("stages", []):
                at = stage.get("finished_at") or stage.get("started_at")
                if timestamp(at) and at > 0:
                    used(queue, stage.get("mode"), at)
        cp = queue.dock_cleanup
        if cp and queue.phase == "completed":
            # An upgrade cannot infer whether old, completed cleaning had dock care.
            # Preserve old terminal history; new runs use the new contract.
            queue.dock_cleanup = {}
            return
    if not cp:
        return
    valid = (isinstance(cp, dict) and cp.get("version") == VERSION and cp.get("run_id") == queue.run_id
             and isinstance(cp.get("revision"), int) and not isinstance(cp.get("revision"), bool)
             and cp["revision"] >= 0 and isinstance(cp.get("required"), dict)
             and isinstance(cp.get("actions"), dict) and set(cp["actions"]) <= set(ACTIONS)
             and all(timestamp(cp.get(key, 0)) for key in ("begun_at", "ready_since", "ready_observed_at"))
             and isinstance(cp.get("armed"), bool)
             and cp.get("outcome") in {"completed", "cancelled"}
             and cp.get("state") in {"pending", "returning", "waiting", "running", "attention", "completed"}
             and all(isinstance(cp["required"].get(key), bool) for key in ACTIONS))
    if valid:
        for key in ACTIONS:
            if not cp["required"][key] and key not in cp["actions"]:
                continue
            action = cp["actions"].get(key)
            valid = (isinstance(action, dict) and action.get("state") in {"pending", "commanded", "running", "completed"}
                     and timestamp(action.get("used_at")) and action["used_at"] > 0
                     and all(action.get(name) is None or timestamp(action[name])
                             for name in ("command_at", "started_at", "completed_at")))
            if not valid:
                break
    if not valid:
        # Retain a visible, non-executable checkpoint rather than silently losing it.
        queue.dock_cleanup = {"version": VERSION, "run_id": queue.run_id, "revision": 0,
            "state": "attention", "required": {key: True for key in ACTIONS}, "actions": {},
            "reason": "Saved dock-care evidence is invalid. Inspect the robot and dock before cleaning.",
            "retry_allowed": False, "armed": False, "outcome": "cancelled", "invalid": True}
        queue.attention(queue.dock_cleanup["reason"])
        return
    cp["ready_since"] = cp["ready_observed_at"] = 0
    for action in cp["actions"].values():
        if action["state"] == "running":
            action["restart_needs_active"] = True


def arm(queue, now, outcome):
    if not required(queue):
        return
    cp = queue.dock_cleanup
    before = deepcopy(cp)
    cp.update(armed=True, outcome=outcome, begun_at=cp.get("begun_at") or now,
              state="returning", reason="Waiting for the robot to dock before final care.", retry_allowed=False,
              ready_since=0, ready_observed_at=0)
    _touch(queue, before)


def interrupted(queue, message):
    if not required(queue):
        return
    cp = queue.dock_cleanup
    before = deepcopy(cp)
    for action in cp.get("actions", {}).values():
        if action.get("state") == "running":
            action.update(state="pending", started_at=None, completed_at=None, command_at=None)
    if cp.get("armed"):
        cp.update(state="attention", reason=message, retry_allowed=not cp.get("invalid", False))
    _touch(queue, before)


def _settled(cp, snapshot, now):
    from .engine import READY_SETTLE_SECONDS
    if not (snapshot.vacuum == "docked" and snapshot.ready_for("vacuum")):
        cp["ready_since"] = cp["ready_observed_at"] = 0
        return False
    if not cp.get("ready_since"):
        cp["ready_since"], cp["ready_observed_at"] = now, snapshot.observed_at
        return False
    return (now >= cp["ready_since"] + READY_SETTLE_SECONDS
            and snapshot.observed_at >= cp["ready_since"] + READY_SETTLE_SECONDS
            and snapshot.observed_at > cp["ready_observed_at"])


def observe(queue, snapshot, now, *, dispatch=False):
    """Observe native care at any time; dispatch missing care only when armed."""
    from .engine import ACK_SECONDS, DOCK_CARE_STATUS, DOCK_FINISH_SECONDS
    if not required(queue):
        return None
    cp = queue.dock_cleanup
    if cp.get("invalid") or cp.get("state") == "attention":
        return None
    before = deepcopy(cp)
    if snapshot.observed_at <= 0 or not 0 <= now - snapshot.observed_at <= 90:
        if dispatch:
            queue.attention("Dock care is waiting for a fresh robot update. No action was repeated.")
        return None
    # Native state proves service start even when a short-lived switch is unavailable.
    for key in ACTIONS:
        action = cp["actions"].get(key)
        if not action or action["state"] == "completed":
            continue
        active = snapshot.status in ACTIVE_STATUS[key]
        if active and snapshot.observed_at >= max(action["used_at"], action.get("command_at") or 0):
            action.pop("restart_needs_active", None)
            if action["state"] != "running":
                action.update(state="running", started_at=snapshot.observed_at)
                cp["ready_since"] = cp["ready_observed_at"] = 0
            if queue.pending_command == "dock_care" and cp.get("current_action") == key:
                queue.confirmed()
        elif action["state"] == "commanded" and now - (action.get("command_at") or now) >= ACK_SECONDS:
            queue.attention(LABELS[key] + " was requested but its start was not observed. Check the dock before explicitly retrying; no automatic retry was sent.")
            return None
    if not dispatch:
        # Observe intermediate native care without dispatching it. A later floor
        # pass invalidates only the corresponding completed hygiene obligation.
        if snapshot.job == "off" and snapshot.vacuum == "docked" and snapshot.status not in DOCK_CARE_STATUS:
            settled = _settled(cp, snapshot, now)
            for key, action in cp["actions"].items():
                if (settled and action["state"] == "running" and not action.get("restart_needs_active")
                        and snapshot.healthy_for("mop" if key == "mop_washing" else "vacuum")):
                    action.update(state="completed", completed_at=snapshot.observed_at)
        else:
            cp["ready_since"] = cp["ready_observed_at"] = 0
        _touch(queue, before)
        return None
    if not snapshot.robot_healthy:
        queue.attention("Dock care is incomplete because the robot has a fault or is unavailable. Resolve it, then resume dock care.")
        return None
    if cp.get("begun_at") and now - cp["begun_at"] >= DOCK_FINISH_SECONDS:
        queue.attention("Dock care was not confirmed within 30 minutes. Check the dock; no action was repeated.")
        return None
    if snapshot.dock_error not in {"ok", "none", "water_empty"}:
        queue.attention("Dock care is incomplete because the dock reports a fault. Resolve it, then resume dock care.")
        return None
    if snapshot.job == "on" and cp["outcome"] == "completed":
        queue.attention("Another cleaning job appeared before final dock care. Check the robot; no dock command was sent.")
        return None
    if snapshot.job == "on":
        cp.update(state="returning", reason="Waiting for the cleaning job to end before dock care.")
        _touch(queue, before)
        return None
    if snapshot.vacuum != "docked":
        cp.update(state="returning", reason="Waiting for the robot to dock before final care.")
        cp["ready_since"] = cp["ready_observed_at"] = 0
        _touch(queue, before)
        return None
    if snapshot.status in DOCK_CARE_STATUS or any(value == "on" for key, value in snapshot.dock_controls.items() if key in ACTIONS):
        cp.update(state="running", reason="Waiting for the dock to finish servicing the robot.")
        cp["ready_since"] = cp["ready_observed_at"] = 0
        _touch(queue, before)
        return None
    remaining = [key for key in ACTIONS if cp["actions"].get(key, {}).get("state") not in {None, "completed"}]
    if snapshot.dock_error == "water_empty" and remaining == ["mop_washing"]:
        queue.attention("Mop washing is still required but the clean-water tank is empty. Refill it, then resume dock care.")
        return None
    if not _settled(cp, snapshot, now):
        cp.update(state="waiting", reason="Waiting for settled dock readiness.")
        _touch(queue, before)
        return None
    # An observed start followed by fresh settled idle is completion, not merely
    # acceptance of the native service. Faults can never count as successful care.
    for key in ACTIONS:
        action = cp["actions"].get(key)
        if action and action["state"] == "running":
            if action.get("restart_needs_active"):
                queue.attention(LABELS[key] + " ended while Home Assistant was offline. Completion is unconfirmed; inspect the dock before explicitly retrying.")
                return None
            if not snapshot.healthy_for("mop" if key == "mop_washing" else "vacuum"):
                queue.attention(LABELS[key] + " was interrupted by a dock fault. Resolve it, then resume dock care.")
                return None
            action.update(state="completed", completed_at=snapshot.observed_at)
    for key in ACTIONS:
        action = cp["actions"].get(key)
        if not action or action["state"] == "completed":
            continue
        if action["state"] == "commanded":
            cp.update(state="waiting", reason="Waiting for " + LABELS[key].lower() + " to start.")
            _touch(queue, before)
            return None
        if not snapshot.healthy_for("mop" if key == "mop_washing" else "vacuum"):
            queue.attention(LABELS[key] + " is still required but a dock fault prevents it. Resolve it, then resume dock care.")
            return None
        if snapshot.dock_controls.get(key) != "off":
            queue.attention(LABELS[key] + " is still required, but its native control is unavailable. Restore the control, then resume dock care.")
            return None
        action.update(state="commanded", command_at=now)
        cp.update(state="waiting", reason="Starting " + LABELS[key].lower() + ".", current_action=key)
        cp["ready_since"] = cp["ready_observed_at"] = 0
        queue.pending_command, queue.command_at = "dock_care", now
        _touch(queue, before)
        return "dock", key
    cp.update(state="completed", reason="Required dock care confirmed.", retry_allowed=False,
              completed_at=snapshot.observed_at, current_action="")
    queue.phase, queue.error = cp["outcome"], ""
    queue.confirmed()
    queue.decision = "required dock care confirmed"
    _touch(queue, before)
    return None


def resume(queue, snapshot, now, run_id, revision):
    if not required(queue) or queue.dock_cleanup.get("invalid"):
        raise ValueError("There is no recoverable dock-care checkpoint.")
    cp = queue.dock_cleanup
    if run_id != cp["run_id"] or type(revision) is not int or revision != cp["revision"]:
        raise ValueError("Dock care changed. Refresh its current status before continuing.")
    if cp["state"] != "attention" or not cp.get("retry_allowed"):
        raise ValueError("Dock care is not waiting for a retry.")
    queue._validate_command_barrier(snapshot, now)
    queue.validate_fresh_observation(snapshot, now)
    returning = snapshot.vacuum != "docked"
    recharge = snapshot.vacuum == "docked" and snapshot.job == "on" and cp["outcome"] == "cancelled"
    if returning:
        queue.validate_control_state("return_to_dock", snapshot)
    elif recharge:
        queue.validate_control_state("stop", snapshot)
    elif not snapshot.ready_for("vacuum"):
        raise ValueError("Wait for active dock servicing to end before retrying dock care.")
    before = deepcopy(cp)
    for action in cp["actions"].values():
        if action["state"] != "completed":
            action.update(state="pending", command_at=None, started_at=None, completed_at=None)
    cp.update(state="waiting", reason="Checking dock readiness before continuing care.",
              retry_allowed=False, armed=True, begun_at=now, ready_since=0, ready_observed_at=0)
    queue.phase = "finishing" if queue.mode == "manual" and cp["outcome"] == "completed" and not returning and not recharge else "controlling"
    if queue.phase == "controlling":
        queue.mode = "finish"
        queue.dock_finish_at = now
        queue.setup["finish_commands"] = {}
    queue.error = ""
    queue.confirmed()
    _touch(queue, before)
    if returning or recharge:
        return queue._observe_finish(snapshot, now)
    return observe(queue, snapshot, now, dispatch=True)
