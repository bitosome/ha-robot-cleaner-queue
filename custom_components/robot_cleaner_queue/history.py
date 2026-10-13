"""Bounded, durable cleaning evidence, independent of executable queue state.

These records are read-only history. They never authorize a robot command or restore
an interrupted queue. Only the engine's verified completion path adds successful
native-record timestamps; legacy counts deliberately retain unknown times.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite

VERSION = 1
MAX_PREVIOUS_RUNS = 5
MAX_STAGES = 128
MAX_INTERRUPTION_EVENTS = 12
STATES = {"pending", "preparing", "starting", "running", "paused", "attention", "completed", "cancelled", "finishing"}
SETTINGS = {"mode", "suction", "water", "route", "repeat"}
LIVE_STATES = {"preparing", "starting", "running", "paused", "finishing"}


def timestamp(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and 0 < value <= 8.64e12 else None


def _settings(value):
    return {k: v for k, v in value.items() if k in SETTINGS and isinstance(v, (str, int, float))} if isinstance(value, dict) else {}


def _stage(stage, index):
    return {"index": index, "target": stage.get("target", ""), "mode": stage.get("mode", "vacuum"),
            "room_index": stage.get("room_index", index), "pass_index": stage.get("pass_index", 0),
            "repeat_index": stage.get("repeat_index", 0), "settings": _settings(stage.get("settings")),
            "state": "pending", "started_at": None, "finished_at": None, "confirmed_at": None,
            "reason": "", "evidence_source": "queue"}


def from_queue(queue, assigned_at=None, room_names=None):
    """Create evidence from an existing queue without inventing historical times.

    This is also the migration/backfill helper. Recorder recovery may supply dated
    observations afterwards, with evidence_source='recorder'; never populate a native
    finished_at without the actual matched cleaning record.
    """
    if queue.mode != "manual" or not queue.stages:
        return None
    names = room_names or {}
    specs = {r["id"]: r for r in queue.setup.get("rooms", []) if isinstance(r, dict) and isinstance(r.get("id"), str)}
    stages = [_stage(stage, index) for index, stage in enumerate(queue.stages[:MAX_STAGES])]
    targets = list(dict.fromkeys(stage["target"] for stage in stages))
    rooms = [{"id": target, "name": names.get(target) or specs.get(target, {}).get("name") or target or "Whole home",
              "setup": _settings(specs.get(target, {}).get("setup", queue.setup))} for target in targets]
    for stage in stages[:max(0, queue.completed)]:
        stage.update(state="completed", evidence_source="legacy_queue")
    if 0 <= queue.current_index < len(stages) and not queue.next_pending:
        stage = stages[queue.current_index]
        if stage["state"] != "completed" and queue.phase in {"preparing", "starting", "running", "paused", "attention"}:
            stage["state"] = queue.phase
    return {"run_id": queue.run_id or "legacy", "vacuum": queue.vacuum, "state": queue.phase,
            "assigned_at": timestamp(assigned_at), "updated_at": timestamp(assigned_at), "finished_at": None,
            "rooms": rooms, "stages": stages, "interruptions": []}


def normalize(value):
    """Allowlist restored evidence. Damaged history must not break queue barriers."""
    if not isinstance(value, dict) or not isinstance(value.get("run_id"), str) or not value["run_id"]:
        return None
    raw_stages = value.get("stages")
    if (not isinstance(value.get("vacuum"), str) or not isinstance(raw_stages, list)
            or not 1 <= len(raw_stages) <= MAX_STAGES or not isinstance(value.get("state"), str) or value["state"] not in STATES):
        return None
    stages = []
    for index, raw in enumerate(raw_stages):
        if (not isinstance(raw, dict) or not isinstance(raw.get("target"), str)
                or not isinstance(raw.get("mode"), str) or not isinstance(raw.get("state"), str) or raw["state"] not in STATES):
            return None
        stage = _stage(raw, index)
        for key in ("room_index", "pass_index", "repeat_index"):
            stage[key] = raw.get(key) if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool) and raw[key] >= 0 else 0
        stage.update(state=raw["state"], reason=str(raw.get("reason") or "")[:600],
                     evidence_source=raw.get("evidence_source") if isinstance(raw.get("evidence_source"), str) and raw["evidence_source"] in {"queue", "native_record", "legacy_queue", "recorder"} else "queue")
        for key in ("started_at", "finished_at", "confirmed_at"):
            stage[key] = timestamp(raw.get(key))
        stages.append(stage)
    rooms = []
    for raw in (value.get("rooms") if isinstance(value.get("rooms"), list) else [])[:MAX_STAGES]:
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            rooms.append({"id": raw["id"], "name": str(raw.get("name") or raw["id"] or "Whole home")[:200], "setup": _settings(raw.get("setup"))})
    interruptions = [{"at": timestamp(event.get("at")), "reason": str(event.get("reason") or "")[:600]}
                     for event in (value.get("interruptions") if isinstance(value.get("interruptions"), list) else [])[-MAX_INTERRUPTION_EVENTS:]
                     if isinstance(event, dict)]
    return {"run_id": value["run_id"], "vacuum": value["vacuum"], "state": value["state"],
            **{key: timestamp(value.get(key)) for key in ("assigned_at", "updated_at", "finished_at")},
            "rooms": rooms, "stages": stages, "interruptions": interruptions}


def restore(queue):
    queue.last_run = normalize(queue.last_run)
    previous = queue.previous_runs if isinstance(queue.previous_runs, list) else []
    seen = {queue.last_run["run_id"]} if queue.last_run else set()
    queue.previous_runs = []
    for raw in previous:
        run = normalize(raw)
        if run and run["run_id"] not in seen:
            _historical(run)
            queue.previous_runs.append(run)
            seen.add(run["run_id"])
        if len(queue.previous_runs) == MAX_PREVIOUS_RUNS:
            break
    if not isinstance(queue.history_revision, int) or isinstance(queue.history_revision, bool) or queue.history_revision < 0:
        queue.history_revision = 0
    if queue.last_run and not _belongs(queue):
        _historical(queue.last_run)
        if queue.mode == "manual" and queue.stages:
            queue.previous_runs = [queue.last_run, *queue.previous_runs][:MAX_PREVIOUS_RUNS]
            queue.last_run = None
    ensure(queue)


def _historical(run):
    """An archive can never present an old execution as currently cleaning."""
    if run["state"] in LIVE_STATES:
        run["state"] = "attention"
    for stage in run["stages"]:
        if stage["state"] in LIVE_STATES:
            stage.update(state="attention", reason="This historical pass has no confirmed completion.")


def ensure(queue):
    if queue.last_run is None and queue.mode == "manual" and queue.stages:
        queue.last_run = from_queue(queue)
        queue.history_revision += 1


def _belongs(queue):
    return queue.mode == "manual" and queue.last_run and queue.last_run["run_id"] == (queue.run_id or "legacy")


def _changed(queue, before, now):
    if queue.last_run != before:
        queue.last_run["updated_at"] = timestamp(now) or queue.last_run.get("updated_at")
        queue.history_revision += 1


def begin(queue, now, room_names=None):
    """Called only after acceptance, after the previous run has been synchronized."""
    if queue.last_run:
        queue.previous_runs = [deepcopy(queue.last_run), *[run for run in queue.previous_runs if run["run_id"] != queue.last_run["run_id"]]][:MAX_PREVIOUS_RUNS]
    queue.last_run = from_queue(queue, now, room_names)
    queue.history_revision += 1


def sync(queue, now=None):
    ensure(queue)
    if not _belongs(queue):
        return
    run = queue.last_run
    before = deepcopy(run)
    run["state"] = queue.phase if queue.phase in STATES else run["state"]
    if queue.phase in {"cancelled", "completed"} and run["finished_at"] is None and before["state"] != queue.phase:
        run["finished_at"] = timestamp(now)
    if 0 <= queue.current_index < len(run["stages"]):
        stage = run["stages"][queue.current_index]
        if stage["state"] != "completed":
            if queue.next_pending:
                stage["state"] = "pending"
            elif queue.phase in {"preparing", "starting", "running", "paused"} or (queue.phase == "attention" and stage["state"] != "pending"):
                stage["state"] = queue.phase
            if queue.phase == "attention" and stage["state"] == "attention":
                stage["reason"] = queue.error
    if queue.phase in {"attention", "cancelled"}:
        reason = queue.error or "Sequence cancelled. Unconfirmed work is not marked as completed."
        if not run["interruptions"] or run["interruptions"][-1]["reason"] != reason or before["state"] != queue.phase:
            run["interruptions"].append({"at": timestamp(now), "reason": reason})
            run["interruptions"] = run["interruptions"][-MAX_INTERRUPTION_EVENTS:]
        elif run["interruptions"][-1]["at"] is None and timestamp(now):
            run["interruptions"][-1]["at"] = now
        # Future rooms remain pending. Cancellation is a sequence outcome, not
        # evidence that the robot attempted or failed every unstarted room.
        if queue.phase == "cancelled" and 0 <= queue.current_index < len(run["stages"]):
            stage = run["stages"][queue.current_index]
            if stage["state"] not in {"pending", "completed"}:
                stage.update(state="cancelled", reason=reason)
    _changed(queue, before, now)


def observe_running(queue, snapshot, now, cleaning_status):
    """A request/start acknowledgement is not proof of floor cleaning."""
    ensure(queue)
    if (not _belongs(queue) or queue.phase not in {"starting", "running"} or queue.next_pending
            or queue.pending_command not in {"", "start", "resume"}
            or snapshot.job != "on" or snapshot.status not in cleaning_status
            or not snapshot.robot_healthy or not queue.started_at <= snapshot.observed_at <= now):
        return
    if not 0 <= queue.current_index < len(queue.last_run["stages"]):
        return
    stage = queue.last_run["stages"][queue.current_index]
    if stage["started_at"] is None:
        before = deepcopy(queue.last_run)
        stage["started_at"] = timestamp(snapshot.observed_at)
        _changed(queue, before, now)


def complete_stage(queue, record, now):
    """Call only after the engine verifies a matching successful native record."""
    ensure(queue)
    if not _belongs(queue) or not 0 <= queue.current_index < len(queue.last_run["stages"]):
        return
    before = deepcopy(queue.last_run)
    queue.last_run["stages"][queue.current_index].update(
        state="completed", started_at=timestamp(record.get("begin")), finished_at=timestamp(record.get("end")),
        confirmed_at=timestamp(now), reason="", evidence_source="native_record")
    _changed(queue, before, now)


def cancel_before_detach(queue, now):
    """Finish/dock cancels execution but keeps the full assignment and evidence."""
    ensure(queue)
    if not _belongs(queue) or queue.phase in {"completed", "cancelled"}:
        return
    original_phase, original_error = queue.phase, queue.error
    queue.phase, queue.error = "cancelled", ""
    sync(queue, now)
    queue.phase, queue.error = original_phase, original_error


def summary(run):
    if not run:
        return None
    return {key: run.get(key) for key in ("run_id", "vacuum", "state", "assigned_at", "updated_at", "finished_at")} | {
        "stage_count": len(run["stages"]), "completed_count": sum(stage["state"] == "completed" for stage in run["stages"]),
        "room_count": len(run["rooms"])}


def response(queue, vacuum):
    """Detached copy: callers cannot mutate persistent evidence through a response."""
    return deepcopy({"run_history_version": VERSION, "history_revision": queue.history_revision,
                     "last_run": queue.last_run if queue.last_run and queue.last_run["vacuum"] == vacuum else None,
                     "previous_runs": [run for run in queue.previous_runs if run["vacuum"] == vacuum]})
