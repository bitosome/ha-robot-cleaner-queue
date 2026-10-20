"""No-motion replacement of untouched rooms; current/completed work is immutable."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from . import history

EDITABLE_PHASES = {"preparing", "starting", "running", "paused", "attention"}


def boundary(queue):
    """Return the immutable stage prefix, including every pass of a started room."""
    if not (queue.mode == "manual" and queue.address == "room" and queue.run_id
            and queue.stages and queue.completed == queue.current_index
            and 0 <= queue.current_index <= len(queue.stages)):
        return None
    specs = queue.setup.get("rooms", [])
    targets = [room.get("id") for room in specs if isinstance(room, dict)]
    stage_targets = list(dict.fromkeys(stage.get("target") for stage in queue.stages))
    if (not targets or targets != stage_targets or targets != queue.targets
            or len(set(targets)) != len(targets) or any(not target for target in targets)):
        return None
    # Native room plans are contiguous. Never edit a legacy all-vacuum/all-mop plan.
    closed, previous = set(), None
    for stage in queue.stages:
        target = stage.get("target")
        if target != previous:
            if target in closed:
                return None
            closed.add(target)
            previous = target
    protected = set(stage["target"] for stage in queue.stages[:queue.current_index])
    between = queue.next_pending or queue.interrupted_next_pending
    if queue.current_index < len(queue.stages) and (not between or queue.pending_command):
        protected.add(queue.stages[queue.current_index]["target"])
    end = max((index + 1 for index, stage in enumerate(queue.stages) if stage["target"] in protected), default=0)
    return end


def sync_revision(queue):
    """Revision covers edit eligibility and reservations, not every telemetry update."""
    if not isinstance(queue.queue_revision, int) or isinstance(queue.queue_revision, bool) or queue.queue_revision < 0:
        queue.queue_revision = 0
    value = [queue.run_id, queue.mode, queue.address, queue.phase, queue.pending_command,
             queue.current_index, queue.completed, queue.next_pending, queue.interrupted_next_pending,
             queue.targets, queue.setup, queue.stages]
    signature = sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if signature != queue.queue_edit_signature:
        queue.queue_revision += 1
        queue.queue_edit_signature = signature


def state(queue):
    sync_revision(queue)
    end = boundary(queue)
    reason = ""
    if end is None:
        reason = "Only a retained native room sequence can be edited."
    elif queue.phase not in EDITABLE_PHASES:
        reason = "Wait for sequence recovery to finish." if queue.phase == "recovering" else "This sequence is no longer accepting rooms."
    elif queue.completed == len(queue.stages):
        reason = "All assigned rooms have finished."
    protected = list(dict.fromkeys(stage["target"] for stage in queue.stages[:end])) if end is not None else []
    pending = list(dict.fromkeys(stage["target"] for stage in queue.stages[end:])) if end is not None else []
    return {"queue_edit_version": 1, "queue_revision": queue.queue_revision,
            "queue_editable": not reason, "queue_edit_reason": reason,
            "protected_room_ids": protected, "editable_room_ids": pending}


def validate_request(queue, vacuum, run_id, revision):
    status = state(queue)
    if type(revision) is not int or vacuum != queue.vacuum or run_id != queue.run_id or revision != queue.queue_revision:
        raise ValueError("The sequence changed. Reload its remaining rooms before applying your changes.")
    if not status["queue_editable"]:
        raise ValueError(status["queue_edit_reason"])
    return boundary(queue)


def replace_remaining(queue, rooms, stages, now):
    """Call only on a detached candidate after authorization and native validation."""
    previous_revision = queue.queue_revision
    end = boundary(queue)
    protected = list(dict.fromkeys(stage["target"] for stage in queue.stages[:end]))
    if any(room["id"] in protected for room in rooms):
        raise ValueError("A completed or started room cannot be added, removed or reordered.")
    if len(protected) + len(rooms) > 32 or end + len(stages) > 128:
        raise ValueError("The edited sequence exceeds 32 rooms or 128 passes.")
    prefix_rooms = deepcopy(queue.setup["rooms"][:len(protected)])
    replacement = deepcopy(stages)
    for stage in replacement:
        stage["room_index"] += len(protected)
    old_targets = list(queue.targets)
    old_rooms = deepcopy(queue.setup["rooms"])
    old_history = deepcopy(queue.last_run)
    queue.stages = deepcopy(queue.stages[:end]) + replacement
    queue.setup = {"rooms": prefix_rooms + deepcopy(rooms)}
    queue.targets = protected + [room["id"] for room in rooms]
    history.edit_remaining(queue, end, old_targets, old_rooms, old_history, now)
    if queue.current_index == len(queue.stages):
        # Removing final waiting rooms never sends a dock/start command. A user's
        # pause/fault remains explicit; final care is only observed later.
        if queue.phase in {"running", "preparing"}:
            queue.phase, queue.next_pending, queue.dock_finish_at = "finishing", False, now
            queue._reset_ready()
        queue.decision = "Remaining rooms removed; confirmed work retained for final dock care."
    history.sync(queue, now)
    sync_revision(queue)
    if queue.queue_revision == previous_revision:
        queue.queue_revision += 1  # Even an accepted no-op consumes its revision.


def frozen_remaining(queue, requests, defaults, targets, map_id):
    """Permit reorder/removal during temporary dock-control unavailability."""
    if not isinstance(defaults, dict) or set(defaults) - {"mode", "suction", "water", "route", "repeat"}:
        raise ValueError("Unknown cleaning settings.")
    specs = {room["id"]: room for room in queue.setup["rooms"]}
    rooms, stages, seen = [], [], set()
    for index, request in enumerate(requests):
        if not isinstance(request, dict) or not isinstance(request.get("id"), str):
            raise ValueError("Every remaining room needs its native id.")
        target = request["id"]
        if target in seen:
            raise ValueError("Select each room only once.")
        seen.add(target)
        room = specs.get(target)
        changed = {key: value for key, value in request.items() if key != "id"}
        if room is None or {**room["setup"], **defaults, **changed} != room["setup"]:
            raise ValueError("Wait for native cleaning controls before adding rooms or changing settings. Existing rooms can still be removed or reordered.")
        frozen = [deepcopy(stage) for stage in queue.stages if stage["target"] == target]
        for stage in frozen:
            validate_mapping(stage, targets, map_id)
            stage["room_index"] = index
        rooms.append(deepcopy(room))
        stages.extend(frozen)
    return rooms, stages


def validate_mapping(stage, targets, map_id):
    if (map_id != stage.get("map_id") or stage["target"] not in targets
            or targets[stage["target"]]["segments"] != stage.get("segments")):
        raise ValueError("A retained room's map or segment mapping changed. Review the sequence before editing.")
