"""Explicit recovery of the retained plan, independent of the activity journal.

The V1 API has no documented current-job identifier. Continuity therefore needs
an observed owned segment/zone job, unchanged counters and completion baseline,
unchanged native settings/map, and no intervening external command or lost state.
An ended job can only be retried by explicit user action; it is never credited as
complete merely because the robot is docked.
"""
from __future__ import annotations

from dataclasses import replace
from math import isfinite

RECHARGE_STATUS = {"charging", "charging_complete"}
# python-roborock 7.4.2 RoborockCommand values, sent through HA's native service.
# Pin the operation: vacuum.start chooses its command again in the entity handler
# and could otherwise become APP_START or APP_CHARGE after an intervening update.
RESUME_COMMANDS = {2: "resume_zoned_clean", 3: "resume_segment_clean"}

def fault_window(queue):
    """Freeze the last owned observation, independently of later dock settings."""
    upper = queue.restart_record_before or queue.native_job_observed_at
    return upper if (queue.mode == "manual" and queue.stage and queue.seen_job
                     and queue.native_job_type in {2, 3}
                     and isinstance(upper, (int, float)) and not isinstance(upper, bool)
                     and isfinite(upper) and isinstance(queue.started_at, (int, float))
                     and not isinstance(queue.started_at, bool) and isfinite(queue.started_at)
                     and upper >= queue.started_at > 0) else 0


def restore_fault_window(queue):
    value = queue.fault_record_before
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not isfinite(value) or value < 0
            or value and (value > fault_window(queue) or value < queue.started_at)):
        queue.fault_record_before = 0
    # v0.14 could discard native-resume continuity when dock settings reset.
    # Migrate only its exact device-fault state; no cancellation/foreign controls.
    if (not queue.fault_record_before and queue.phase == "attention"
            and queue.error == "The robot or dock has a fault, or telemetry is unavailable. The sequence is retained for review."):
        queue.fault_record_before = fault_window(queue)


def reconcile_fault_completion(queue, snapshot, now):
    """Credit proven floor work without resuming another room or sending commands."""
    from . import history
    from .engine import MAX_OBSERVATION_AGE
    if (queue.phase != "attention" or queue.pending_command or not queue.fault_record_before
            or not snapshot.connected or not snapshot.observed_at
            or not 0 <= now - snapshot.observed_at <= MAX_OBSERVATION_AGE
            or not completed_after_fault(queue, snapshot)):
        return False
    from . import restart
    proven = restart.completion_record(queue, snapshot) if queue.restart_record_before else snapshot.record
    history.complete_stage(queue, proven, now)
    queue.completed += 1
    queue.current_index += 1
    queue.seen_job = False
    queue.started_at = 0
    queue.native_job_observed_at = 0
    queue.fault_record_before = 0
    queue.restart_record_before = 0
    queue.restart_matched_record = {}
    queue.water_refilled_index = -1
    queue._reset_ready()
    queue.interrupted_next_pending = queue.completed < len(queue.stages)
    queue.continuation_context = "completed_after_fault"
    queue.error = "The interrupted pass completed successfully. Continue when ready."
    if queue.completed == len(queue.stages) and snapshot.healthy_for(queue.cleaning_mode):
        queue.phase, queue.dock_finish_at = "finishing", now
        queue.error = ""
        queue.interrupted_next_pending = False
    queue.decision = "Confirmed the interrupted pass from its successful native record; no cleaning command sent."
    history.sync(queue, now)
    return True


def effective_snapshot(queue, snapshot):
    """A user's refill acknowledgement belongs only to this unfinished pass."""
    if (queue.water_refilled_index == queue.current_index and queue.mode == "manual" and queue.stage
            and snapshot.dock_error == "water_empty" and snapshot.robot_healthy):
        return replace(snapshot, dock_error="ok")
    return snapshot


def execution_status(queue, snapshot):
    from .engine import DOCK_CARE_STATUS, CLEANING_STATUS
    if queue.phase == "recovering":
        return "recovering"
    if queue.phase == "attention":
        return "attention"
    if queue.phase == "finishing":
        return "finishing"
    if queue.next_pending:
        return "waiting_for_dock"
    if snapshot.job == "on" and snapshot.vacuum == "docked" and snapshot.status in RECHARGE_STATUS:
        return "recharging"
    if snapshot.status in DOCK_CARE_STATUS:
        return "dock_care"
    if snapshot.vacuum == "returning":
        return "returning"
    if snapshot.vacuum == "paused" or queue.phase == "paused":
        return "paused"
    if snapshot.status in CLEANING_STATUS and snapshot.job == "on":
        return "cleaning"
    return queue.phase


def matches_owned_job(queue, snapshot, *, allow_missing_settings=False):
    from .engine import MAX_OBSERVATION_AGE
    end = (snapshot.record or {}).get("end")
    known_end = isinstance(end, (int, float)) and not isinstance(end, bool) and isfinite(end)
    return (queue.seen_job and queue.native_job_observed_at > 0
            and 0 <= snapshot.observed_at - queue.native_job_observed_at <= MAX_OBSERVATION_AGE
            and not queue.native_job_discontinuous and queue.native_job_type in {2, 3}
            and snapshot.job == "on" and snapshot.job_type == queue.native_job_type
            and queue.native_clean_time is not None and snapshot.clean_time is not None
            and snapshot.clean_time >= queue.native_clean_time
            and known_end and end == queue.baseline_end
            and all(snapshot.settings.get(key) == value or (allow_missing_settings and key not in snapshot.settings)
                    for key, value in queue.stage.get("settings", {}).items()))


def observe_job(queue, snapshot, now):
    """Keep narrow continuity evidence while observing an owned active pass."""
    from .engine import MAX_OBSERVATION_AGE
    from . import restart
    if queue.mode != "manual" or queue.next_pending or not queue.stage:
        return
    if snapshot.dock_error in {"ok", "none"}:
        queue.water_refilled_index = -1
    if queue.phase == "attention":
        if queue.continuation_context not in {"device_fault", "awaiting_completion", "completed_after_fault", "completion_pending_idle"}:
            return
        if queue.continuation_context in {"awaiting_completion", "completed_after_fault"} and snapshot.job == "on":
            queue.continuation_context = ""
            queue.fault_record_before = 0
            return  # The earlier owned job ended; this one cannot be adopted.
        if snapshot.connected and snapshot.job == "on" and successful_owned_record(queue, snapshot):
            queue.continuation_context = "completion_pending_idle"
            return  # Record and in_cleaning can refresh separately; freeze identity.
        if snapshot.connected and snapshot.job == "off" and snapshot.observed_at and now - snapshot.observed_at <= MAX_OBSERVATION_AGE:
            queue.continuation_context = "completed_after_fault" if completed_after_fault(queue, snapshot) else "awaiting_completion"
            return
        if (queue.restart_record_before and queue.continuation_context == "device_fault"
                and snapshot.connected and snapshot.observed_at >= queue.restart_loaded_at
                and 0 <= now - snapshot.observed_at <= MAX_OBSERVATION_AGE
                and restart.active_candidate(queue, snapshot)):
            return  # May explicitly rejoin observation, never automatic physical resume.
        if (not snapshot.connected or not snapshot.observed_at or now - snapshot.observed_at > MAX_OBSERVATION_AGE
                or not matches_owned_job(queue, snapshot, allow_missing_settings=True)):
            queue.continuation_context = ""
            return
        queue.native_clean_time = snapshot.clean_time
        queue.native_job_observed_at = snapshot.observed_at
        return
    if snapshot.observed_at and snapshot.observed_at < queue.native_job_observed_at:
        return
    if not snapshot.connected or not snapshot.observed_at or now - snapshot.observed_at > MAX_OBSERVATION_AGE:
        queue.native_job_discontinuous = True
        return
    if snapshot.observed_at == queue.native_job_observed_at:
        return
    if queue.native_job_discontinuous:
        return
    if queue.native_job_observed_at and snapshot.observed_at - queue.native_job_observed_at > MAX_OBSERVATION_AGE:
        queue.native_job_discontinuous = True
        return
    if (not snapshot.connected or not snapshot.observed_at or now - snapshot.observed_at > MAX_OBSERVATION_AGE
            or snapshot.job != "on" or snapshot.job_type not in {2, 3}
            or snapshot.clean_time is None):
        return
    if (queue.native_job_type is not None and (snapshot.job_type != queue.native_job_type
            or snapshot.clean_time < (queue.native_clean_time or 0))):
        queue.native_job_discontinuous = True
        return  # Eligibility evidence only; normal completion stays record-based.
    queue.native_job_type = snapshot.job_type
    queue.native_clean_time = snapshot.clean_time
    queue.native_job_observed_at = snapshot.observed_at


def successful_owned_record(queue, snapshot):
    """Match the retained pass's record independently of native trait update order."""
    from .engine import SUCCESS_REASONS
    from . import restart
    record = restart.completion_record(queue, snapshot) if queue.restart_record_before else snapshot.record or {}
    begin, end = float(record.get("begin") or 0), float(record.get("end") or 0)
    return ((queue.continuation_context in {"device_fault", "awaiting_completion", "completed_after_fault", "completion_pending_idle"}
             or queue.phase == "attention" and queue.fault_record_before > 0
             or queue.restart_record_before and restart.valid_record_data(queue, record))
            and queue.seen_job and queue.native_job_observed_at > 0
            and queue.started_at - 3 <= begin <= (queue.fault_record_before or queue.restart_record_before or queue.native_job_observed_at)
            and end >= begin and end > queue.baseline_end
            and record.get("complete") == 1 and record.get("error") == 0
            and (record.get("finish_reason") is None or record.get("finish_reason") in SUCCESS_REASONS))


def completed_after_fault(queue, snapshot):
    """Credit requires both the matching success record and an ended native job."""
    return snapshot.job == "off" and successful_owned_record(queue, snapshot)


def _validate_common(queue, snapshot, now):
    if queue.mode != "manual" or not queue.stages or not queue.run_id:
        raise ValueError("There is no retained cleaning sequence to continue.")
    if queue.pending_command:
        raise ValueError("Wait for the previous robot command to be confirmed.")
    queue._validate_command_barrier(snapshot, now)
    queue._validate_recovery_observation(snapshot, now)
    if (queue.restart_record_before or queue.restart_checkpoint) and snapshot.observed_at < queue.restart_loaded_at:
        raise ValueError("Wait for a fresh robot update after Home Assistant starts.")
    if queue.completed != queue.current_index or not 0 <= queue.current_index <= len(queue.stages):
        raise ValueError("The saved progress is inconsistent. Review the sequence before starting another plan.")


def validate_native_resume(queue, snapshot):
    if queue.restart_record_before or not matches_owned_job(queue, snapshot) or snapshot.in_returning != 0:
        raise ValueError("The unfinished job cannot be matched to this pass. Finish or stop the robot's current job before retrying the remaining pass.")
    if not snapshot.healthy_for(queue.cleaning_mode):
        raise ValueError("Resolve the robot or dock fault before continuing this pass.")
    paused = snapshot.vacuum == snapshot.status == "paused"
    recharging = snapshot.vacuum == "docked" and snapshot.status in RECHARGE_STATUS
    if not (paused or recharging):
        raise ValueError("Wait until the retained job is paused or charging before continuing.")


def continuation_action(queue, snapshot, now, *, water_refilled=False):
    """Read-only validation; the card gets exactly the action the service permits."""
    _validate_common(queue, snapshot, now)
    if queue.phase != "attention":
        raise ValueError("The sequence is not waiting for recovery.")
    if water_refilled and queue.stage and snapshot.dock_error == "water_empty" and snapshot.robot_healthy:
        snapshot = replace(snapshot, dock_error="ok")
    else:
        snapshot = effective_snapshot(queue, snapshot)
    if not snapshot.healthy_for(queue.cleaning_mode):
        raise ValueError("Refill the clean-water tank or resolve the robot/dock fault before continuing.")
    if queue.completed == len(queue.stages):
        if snapshot.job != "off":
            raise ValueError("An unfinished robot job must end before final docking can be confirmed.")
        return "finish_docking"
    if snapshot.job == "on":
        from . import restart
        if queue.restart_record_before and restart.active_candidate(queue, snapshot):
            return "resume_observation"  # Resuming actual motion needs separate native-job confirmation.
        if queue.continuation_context != "device_fault":
            raise ValueError("Job continuity was lost. Stop or finish the current robot job before retrying the remaining pass.")
        from .engine import CLEANING_STATUS, START_STATUS, DOCK_CARE_STATUS
        if (matches_owned_job(queue, snapshot)
                and snapshot.status in CLEANING_STATUS | START_STATUS | DOCK_CARE_STATUS | {"returning_home", "docking"}):
            return "resume_observation"
        validate_native_resume(queue, snapshot)
        return "resume_job"
    if not snapshot.ready_for(queue.cleaning_mode):
        raise ValueError("Wait for the robot to finish dock care and become ready before continuing.")
    return "continue_next" if queue.interrupted_next_pending or completed_after_fault(queue, snapshot) else "retry_stage"


def validate_resume_now(queue, snapshot, now):
    _validate_common(queue, snapshot, now)
    if queue.phase not in {"running", "paused"} or queue.next_pending or not queue.stage:
        raise ValueError("The sequence has no active pass waiting for charging.")
    snapshot = effective_snapshot(queue, snapshot)
    validate_native_resume(queue, snapshot)
    if snapshot.vacuum != "docked" or snapshot.status not in RECHARGE_STATUS:
        raise ValueError("Continue now is available only during a charging break.")


def continue_sequence(queue, snapshot, now, *, water_refilled=False):
    from . import history
    action = continuation_action(queue, snapshot, now, water_refilled=water_refilled)
    if water_refilled and snapshot.dock_error == "water_empty":
        queue.water_refilled_index = queue.current_index
    queue.error, queue.command_failure = "", {}
    queue.not_before = queue.barrier_window = 0
    queue.start_uncertain = False
    queue._reset_ready()
    if action == "finish_docking":
        queue.phase, queue.dock_finish_at = "finishing", now
        queue.decision = "continuing final docking observation; all floor passes are confirmed"
        effect = None
    elif action == "resume_observation":
        queue.phase, queue.pending_command, queue.finish_wait_at = "running", "", 0
        queue.decision = "rejoined the retained native room job without sending a robot command"
        effect = None
    elif action == "resume_job":
        queue.resume_owned_job = True
        queue.phase, queue.pending_command, queue.command_at = "starting", "resume", now
        queue.decision = "continuing the retained native room job"
        effect = ("vacuum", "start")
    elif action == "continue_next":
        if completed_after_fault(queue, snapshot):
            from . import restart
            proven_record = restart.completion_record(queue, snapshot) if queue.restart_record_before else snapshot.record
            history.complete_stage(queue, proven_record, now)
            queue.restart_record_before = 0
            queue.restart_matched_record = {}
            queue.current_index += 1
            queue.completed += 1
            queue.water_refilled_index = -1
        queue.phase, queue.next_pending, queue.finish_wait_at = "running", True, now
        if queue.completed == len(queue.stages):
            queue.phase, queue.next_pending, queue.dock_finish_at = "finishing", False, now
        queue.decision = "continuing the next unstarted pass after settled dock readiness"
        effect = None
    else:
        # Only the unconfirmed pass is retried. Preserve confirmed modes, room
        # order, original settings and the same run/activity record.
        queue.seen_job = False
        queue.started_at = queue.finish_wait_at = 0
        queue.native_job_discontinuous = False
        queue.native_job_type = queue.native_clean_time = None
        queue.native_job_observed_at = 0
        history.retry_stage(queue, now)
        effect = queue._dispatch(snapshot, now)
    queue.continuation_context = ""
    queue.fault_record_before = 0
    queue.interrupted_next_pending = False
    history.sync(queue, now)
    return effect


def state(queue, snapshot, now):
    from . import restart
    result = {"execution_status": execution_status(queue, snapshot),
              "restart_recovery": bool(queue.restart_checkpoint),
              "restart_record_pending": bool(queue.restart_record_before),
              "restart_recovery_reason": queue.decision if queue.restart_checkpoint else "",
              "restart_resume_ready": False, "restart_resume_reason": "", "restart_resume_token": "",
              "water_refill_ack_active": bool(queue.mode == "manual" and queue.stage
                    and queue.phase in {"preparing", "starting", "running", "paused"}
                    and queue.water_refilled_index == queue.current_index),
              "continuation_ready": False, "continuation_action": "", "continuation_reason": "",
              "continuation_requires_water_ack": False, "resume_now_ready": False, "resume_now_reason": ""}
    if queue.phase == "attention" and queue.mode == "manual" and queue.stages:
        needs_water = bool(queue.stage) and snapshot.dock_error == "water_empty" and queue.cleaning_mode != "vacuum"
        try:
            action = continuation_action(queue, snapshot, now, water_refilled=needs_water)
            result.update(continuation_ready=True, continuation_action=action,
                          continuation_requires_water_ack=needs_water)
        except ValueError as err:
            result["continuation_reason"] = str(err)
    if result["execution_status"] == "recharging":
        try:
            validate_resume_now(queue, snapshot, now)
            result["resume_now_ready"] = True
        except ValueError as err:
            result["resume_now_reason"] = str(err)
    if queue.restart_record_before:
        try:
            result.update(restart_resume_ready=True, restart_resume_token=restart.resume_token(queue, effective_snapshot(queue, snapshot), now))
        except ValueError as err:
            result["restart_resume_reason"] = str(err)
    return result
