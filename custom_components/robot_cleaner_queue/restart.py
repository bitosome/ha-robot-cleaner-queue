"""Durable no-motion reconciliation after a Home Assistant process gap.

Roborock has no stable active-job id. An apparent active job may be observed, but
only a successful record whose start lies inside the frozen pre-restart ownership
window may finish that pass. Neither later telemetry nor another restart widens it.
"""
from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def remember(queue, snapshot):
    if snapshot.connected and number(snapshot.observed_at) and snapshot.observed_at > queue.checkpoint_observed_at:
        queue.checkpoint_observed_at = snapshot.observed_at
        end = (snapshot.record or {}).get("end")
        queue.checkpoint_record_end = end if number(end) else None


def restore(queue):
    from .engine import ACTIVE
    from . import dock
    if dock.required(queue) and queue.dock_cleanup.get("armed") and not queue.dock_cleanup.get("invalid"):
        # Persisted care commands are observed, never re-issued after restart.
        # Their original acknowledgement deadlines remain in the checkpoint.
        queue._reset_ready()
        return
    queue.water_refilled_index = -1
    queue.resume_owned_job = False
    queue.restart_resume_guard = ""
    if (not number(queue.restart_record_before) or queue.restart_record_before < 0
            or not isinstance(queue.restart_matched_record, dict)):
        queue.restart_record_before = 0
        queue.attention("The saved restart evidence is invalid. The sequence is retained for review.")
        return
    if queue.restart_checkpoint and queue.phase == "recovering":
        cp = queue.restart_checkpoint
        valid = (isinstance(cp, dict) and cp.get("phase") in {"running", "paused", "preparing", "finishing", "attention"}
                 and number(cp.get("observed_at")) and cp["observed_at"] > 0
                 and isinstance(cp.get("next_pending"), bool)
                 and "record_end" in cp and (cp["record_end"] is None or number(cp["record_end"]))
                 and queue.mode == "manual" and queue.run_id and queue.stages
                 and queue.completed == queue.current_index and 0 <= queue.current_index <= len(queue.stages))
        if not valid:
            queue.attention("The saved restart checkpoint is invalid. The sequence is retained; review it before continuing.")
            return
        matched = cp.get("matched_record")
        if matched and not valid_record_data(queue, matched):
            queue.attention("The saved completion evidence is invalid. The sequence is retained for review.")
            return
        # Another restart while waiting for native setup must retain the first gap.
        queue._reset_ready()
        return
    queue.restart_checkpoint = {}
    eligible = (queue.mode == "manual" and queue.run_id and queue.stages
                and queue.completed == queue.current_index
                and 0 <= queue.current_index <= len(queue.stages)
                and (queue.phase in {"running", "preparing", "paused", "finishing"}
                     or queue.phase == "attention" and queue.continuation_context in {"device_fault", "awaiting_completion", "completed_after_fault", "completion_pending_idle"})
                and queue.pending_command in {"", "configure", "pause"})
    if eligible:
        phase = queue.phase
        fault_context = queue.continuation_context
        upper = queue.restart_record_before or queue.native_job_observed_at
        owned = (queue.seen_job and number(upper) and upper >= queue.started_at > 0
                 and queue.native_job_type in {1, 2, 3} and number(queue.native_clean_time))
        between = queue.next_pending or queue.interrupted_next_pending or phase in {"preparing", "finishing"}
        observed = queue.checkpoint_observed_at or queue.native_job_observed_at
        if number(observed) and observed > 0 and (between or owned):
            queue.restart_checkpoint = {
                "phase": phase, "next_pending": queue.next_pending or queue.interrupted_next_pending,
                "fault_context": fault_context,
                "pending": queue.pending_command, "observed_at": observed,
                "record_end": queue.checkpoint_record_end,
                "owned_before": upper if owned and not between else 0,
                "native_type": queue.native_job_type, "clean_time": queue.native_clean_time,
                "matched_record": dict(queue.restart_matched_record),
            }
            if owned and not between:
                queue.restart_record_before = upper
            queue._preserve_command_barrier()
            queue.phase = "recovering"
            queue.pending_command = ""
            queue.next_pending = False
            queue.continuation_context = ""
            queue._reset_ready()
            queue.decision = "Restoring the saved sequence; waiting for a fresh robot update."
            return
    queue.continuation_context = ""
    if (queue.mode == "manual" and queue.seen_job and queue.pending_command in {"pause", "resume", "resume_now"}
            and number(queue.native_job_observed_at) and queue.native_job_observed_at >= queue.started_at > 0):
        # An uncertain control is not replayed, but its existing owned-job window
        # remains available to explicit confirmed recovery once the barrier ends.
        queue.restart_record_before = queue.restart_record_before or queue.native_job_observed_at
    if queue.phase in ACTIVE or queue.pending_command:
        queue.attention("Home Assistant restarted while a command was unconfirmed. No command was repeated. Continue sequence can retry the unconfirmed pass once the robot is idle.")


def valid_record(queue, snapshot):
    return valid_record_data(queue, snapshot.record)


def valid_record_data(queue, record):
    from .engine import SUCCESS_REASONS
    record = record if isinstance(record, dict) else {}
    begin, end, reason = record.get("begin"), record.get("end"), record.get("finish_reason")
    return (number(begin) and number(end) and number(queue.restart_record_before) and queue.restart_record_before > 0
            and queue.started_at - 3 <= begin <= queue.restart_record_before
            and end >= begin and end > queue.baseline_end
            and record.get("complete") == 1 and record.get("error") == 0
            and (reason is None or number(reason) and reason in SUCCESS_REASONS))


def completion_record(queue, snapshot):
    """Return only matching, allowlisted proof; the newest robot record may be unrelated."""
    if valid_record(queue, snapshot):
        return dict(snapshot.record)
    cached = queue.restart_matched_record
    if isinstance(cached, dict) and valid_record_data(queue, cached):
        return dict(cached)
    return {}


def active_candidate(queue, snapshot):
    """A plausible retained job, not permission to credit work or send motion."""
    end = (snapshot.record or {}).get("end")
    return (queue.restart_record_before > 0 and snapshot.job == "on"
            and snapshot.job_type == queue.native_job_type
            and snapshot.job_type in {1, 2, 3}
            and number(snapshot.clean_time) and number(queue.native_clean_time)
            and snapshot.clean_time >= queue.native_clean_time
            and number(end) and end == queue.baseline_end
            and all(snapshot.settings.get(key) == value for key, value in queue.stage.get("settings", {}).items()))


def _attention(queue, message):
    if queue.restart_checkpoint.get("next_pending"):
        queue.interrupted_next_pending = True
    queue.attention(message)
    queue.decision = message


def reconcile(queue, snapshot, now):
    from . import history
    from .engine import FINISH_SECONDS, MAX_OBSERVATION_AGE
    cp = queue.restart_checkpoint
    if not snapshot.connected or not snapshot.observed_at or not 0 <= now - snapshot.observed_at <= MAX_OBSERVATION_AGE:
        queue.decision = "Restoring the saved sequence; waiting for the robot to reconnect."
        return None
    if snapshot.observed_at <= cp["observed_at"]:
        queue.decision = "Restoring the saved sequence; waiting for a new robot update."
        return None
    phase = cp["phase"]
    proven = completion_record(queue, snapshot)
    if proven:
        cp["matched_record"] = proven
    matched = cp.get("matched_record")
    if matched and not valid_record_data(queue, matched):
        _attention(queue, "The saved completion evidence is invalid. Review the retained sequence.")
        return None
    if matched and snapshot.job == "off":
        # Proven floor work remains completed even if subsequent dock care faults.
        history.complete_stage(queue, matched, now)
        queue.completed += 1
        queue.current_index += 1
        queue.restart_checkpoint = {}
        queue.restart_record_before = 0
        queue.restart_matched_record = {}
        queue.native_job_discontinuous = True
        queue._reset_ready()
        queue.pending_command = ""
        queue.finish_wait_at = now
        queue.phase, queue.next_pending = "running", True
        if queue.completed == len(queue.stages):
            queue.phase, queue.next_pending, queue.dock_finish_at = "finishing", False, now
        remember(queue, snapshot)
        if phase in {"paused", "attention"} or not snapshot.healthy_for(queue.cleaning_mode):
            queue.attention("The interrupted pass completed. The remaining sequence is retained; continue when ready.")
        else:
            queue.decision = "Restored pass completion from the robot's successful cleaning record."
        return None
    if matched:
        queue.decision = "The interrupted pass has a matching completion record; waiting for the robot's current job to end."
        return None
    if not snapshot.healthy_for(queue.cleaning_mode):
        # Keep ownership bounds across the fault so explicit continuation can
        # still credit a matching record, never automatically start another pass.
        _attention(queue, "The saved sequence is retained. Resolve the robot or dock fault, then continue the sequence.")
        queue.continuation_context = "device_fault"
        return None
    if cp.get("pending") == "pause":
        if active_candidate(queue, snapshot) and snapshot.vacuum == snapshot.status == "paused":
            queue.confirmed()
        else:
            if now < queue.not_before or snapshot.observed_at < queue.not_before:
                queue.decision = "Restoring the saved sequence; waiting for the earlier pause command to be confirmed."
            else:
                _attention(queue, "The pause command was not confirmed after restart. Review the native job before continuing.")
            return None
    if phase in {"preparing", "finishing"} or cp["next_pending"]:
        if queue.not_before and (now < queue.not_before or snapshot.observed_at < queue.not_before):
            queue.decision = "Restoring the saved sequence; waiting for the earlier settings command to settle."
            return None
        expected_end = cp["record_end"]
        end = (snapshot.record or {}).get("end")
        if not number(expected_end) or not number(end):
            queue.decision = "Restoring the saved sequence; waiting for the previous cleaning record."
            return None
        if end != expected_end or snapshot.job == "on":
            _attention(queue, "The robot changed jobs while Home Assistant was offline. The sequence is retained; finish the current job before continuing.")
            return None
        queue.restart_checkpoint = {}
        queue.pending_command = ""
        queue.finish_wait_at = queue.dock_finish_at = now
        queue._reset_ready()
        if phase == "finishing":
            queue.phase = "finishing"
        elif phase in {"paused", "attention"}:
            queue.phase, queue.next_pending = "paused", True
            if phase == "attention":
                queue.attention("The saved sequence is retained. Continue when ready.")
        else:
            # Settings are idempotent, but restart them only after fresh settled idle.
            queue.phase, queue.next_pending = "running", True
        queue.decision = "Sequence restored; waiting for settled dock readiness."
        return None
    if snapshot.job == "on":
        if active_candidate(queue, snapshot):
            queue.restart_checkpoint = {}
            queue.native_job_discontinuous = True  # A process gap is not proof for automatic native resume.
            queue.pending_command = ""
            queue.phase = "attention" if phase == "attention" else "paused" if phase == "paused" or snapshot.vacuum == "paused" else "running"
            if queue.phase == "attention":
                queue.continuation_context = "device_fault"
            queue.finish_wait_at = 0
            queue.decision = "Sequence restored; observing the native job and waiting for its matching completion record."
            remember(queue, snapshot)
            return None
        queue.decision = "The saved sequence is retained; waiting for a matching completion record or for the current robot job to end."
        return None
    if not queue.finish_wait_at or queue.finish_wait_at < cp["observed_at"]:
        queue.finish_wait_at = now
    if now - queue.finish_wait_at >= FINISH_SECONDS:
        _attention(queue, "The interrupted pass has no matching successful record. Continue sequence can retry this pass; completed passes are kept.")
    else:
        queue.decision = "Restoring the saved sequence; waiting for the interrupted pass's completion record."
    return None


def resume_token(queue, snapshot, now):
    """Explicit consent to resume this displayed native job, without claiming ownership."""
    from .engine import MAX_OBSERVATION_AGE
    from .recovery import RECHARGE_STATUS, effective_snapshot
    snapshot = effective_snapshot(queue, snapshot)
    if not queue.restart_record_before or queue.mode != "manual" or not queue.stage:
        raise ValueError("There is no interrupted pass to resume after restart.")
    if queue.phase not in {"recovering", "running", "paused", "attention"} or queue.pending_command:
        raise ValueError("Wait for the previous command to finish.")
    queue._validate_command_barrier(snapshot, now)
    if (not snapshot.connected or not snapshot.observed_at
            or snapshot.observed_at < queue.restart_loaded_at
            or not 0 <= now - snapshot.observed_at <= MAX_OBSERVATION_AGE):
        raise ValueError("Wait for a fresh robot update.")
    if not snapshot.healthy_for(queue.cleaning_mode):
        raise ValueError("Resolve the robot or dock fault before continuing.")
    if not active_candidate(queue, snapshot) or snapshot.job_type not in {2, 3} or snapshot.in_returning != 0:
        raise ValueError("The native room job cannot be resumed with these saved settings.")
    if not (snapshot.vacuum == snapshot.status == "paused"
            or snapshot.vacuum == "docked" and snapshot.status in RECHARGE_STATUS):
        raise ValueError("Wait until the robot is paused or charging.")
    payload = [queue.run_id, queue.current_index, queue.started_at, queue.restart_record_before,
               queue.baseline_end, queue.stage, snapshot.vacuum, snapshot.status,
               snapshot.job_type, snapshot.in_returning, snapshot.clean_time, snapshot.record, snapshot.settings]
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_resume(queue, snapshot, now, token):
    if not isinstance(token, str) or token != resume_token(queue, snapshot, now):
        raise ValueError("The robot job changed. Review it again before continuing.")
    queue.restart_checkpoint = {}
    queue.restart_resume_guard = token
    queue.resume_owned_job = True
    queue.phase, queue.pending_command, queue.command_at = "starting", "resume_now", now
    queue.error = ""
    queue.decision = "Continuing the confirmed native room job; completion still requires the saved pass's matching record."
    return "vacuum", "start"


def validate_dispatch(queue, snapshot, now):
    # Temporarily evaluate the same read-only consent fingerprint after reservation.
    phase, pending = queue.phase, queue.pending_command
    try:
        queue.phase, queue.pending_command = "running", ""
        token = resume_token(queue, snapshot, now)
    finally:
        queue.phase, queue.pending_command = phase, pending
    if token != queue.restart_resume_guard:
        raise ValueError("The robot job changed before its resume command. Review it again.")
