"""Deterministic queue transitions. No Home Assistant or device I/O."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

ACTIVE = {"preparing", "starting", "running", "paused", "controlling", "finishing"}
READY_STATUS = {"idle", "charging", "charging_complete"}
CLEANING_STATUS = {
    "cleaning", "spot_cleaning", "segment_cleaning", "zoned_cleaning",
    "robot_status_mopping", "clean_mop_cleaning", "clean_mop_mopping",
    "segment_mopping", "segment_clean_mop_cleaning", "segment_clean_mop_mopping",
    "zoned_mopping", "zoned_clean_mop_cleaning", "zoned_clean_mop_mopping",
}
START_STATUS = CLEANING_STATUS | {
    "starting", "charger_disconnected", "going_to_target", "washing_the_mop", "washing_the_mop_2",
    "going_to_wash_the_mop", "back_to_dock_washing_duster", "attaching_the_mop",
    "detaching_the_mop",
}
# Post-clean care may begin just after a transient charging observation.
DOCK_CARE_STATUS = {"emptying_the_bin", "washing_the_mop", "washing_the_mop_2", "attaching_the_mop",
                    "detaching_the_mop", "air_drying_stopping"}
SUCCESS_REASONS = {52, 54, 55, 56, 57}
ACK_SECONDS = 60
FINISH_SECONDS = 180
# Docking can briefly report charging before dust emptying/mop care starts. Require
# both elapsed ready time and a later native observation, never repeated cached ticks.
READY_SETTLE_SECONDS = 15
DOCK_FINISH_SECONDS = 1800
# A dispatched routine can take minutes to become visible. Immediately after a
# completed room the robot may still be washing or drying its mop, emptying dust or
# topping up its battery, and it ignores a routine until that servicing ends. A real
# run pressed the next room's routine one second after docking and the robot was first
# observed cleaning 677 seconds later, by which time the flat 60-second window had
# stopped the whole sequence even though cleaning then proceeded normally.
# Waiting is still fail-closed: the command is never re-sent and a fault, a competing
# job or lost telemetry stops the queue at once.
START_SECONDS = 900
# Preparation is measured from the dispatch, and a slow start may consume the whole
# start window before the job itself turns on.
PREPARE_SECONDS = START_SECONDS + 600
# Writing settings is not motion, and a servicing dock can hide the native setting
# entities for minutes, so the readback is awaited patiently rather than abandoned.
CONFIGURE_SECONDS = 600


@dataclass
class Snapshot:
    vacuum: str = "unavailable"
    status: str = "unavailable"
    job: str = "unavailable"
    error: str = "unavailable"
    dock_error: str = "ok"
    connected: bool = False
    record: dict[str, Any] | None = None
    observed_at: float = 0
    settings: dict[str, str] = field(default_factory=dict)
    dock_drying: bool | None = None

    @property
    def robot_healthy(self) -> bool:
        return (
            self.connected
            and self.vacuum not in {"unknown", "unavailable", "error"}
            and self.status not in {"unknown", "unavailable", "device_offline", "error", "charging_problem"}
            and self.job in {"on", "off"}
            and self.error == "none"
        )

    def healthy_for(self, mode: str = "vacuum") -> bool:
        """A water-empty dock blocks anything that mops and permits vacuum-only work."""
        return self.robot_healthy and (self.dock_error in {"ok", "none"} or
                                      self.dock_error == "water_empty" and mode == "vacuum")

    @property
    def healthy(self) -> bool:
        return self.healthy_for()

    def ready_for(self, mode: str = "vacuum") -> bool:
        return self.healthy_for(mode) and self.vacuum in {"docked", "idle"} and self.status in READY_STATUS and self.job == "off"

    def servicing_for(self, mode: str = "vacuum") -> bool:
        """Known dock care without a competing job; safe to wait, never to dispatch."""
        return (self.healthy_for(mode) and self.vacuum == "docked"
                and self.job == "off" and self.status in DOCK_CARE_STATUS)

    @property
    def ready(self) -> bool:
        return self.healthy and self.vacuum in {"docked", "idle"} and self.status in READY_STATUS and self.job == "off"


@dataclass
class Queue:
    phase: str = "idle"
    vacuum: str = ""
    mode: str = ""
    targets: list[str] = field(default_factory=list)
    setup: dict[str, Any] = field(default_factory=dict)
    stages: list[dict[str, Any]] = field(default_factory=list)
    control_entities: dict[str, str] = field(default_factory=dict)
    current_index: int = 0
    completed: int = 0
    error: str = ""
    pending_command: str = ""
    command_at: float = 0
    started_at: float = 0
    baseline_end: float = 0
    seen_job: bool = False
    finish_wait_at: float = 0
    next_pending: bool = False
    run_id: str = ""
    not_before: float = 0
    barrier_window: float = 0
    settings_sent_at: float = 0
    ready_since: float = 0
    ready_observed_at: float = 0
    dock_finish_at: float = 0
    command_failure: dict[str, Any] = field(default_factory=dict)
    start_uncertain: bool = False
    decision: str = ""
    address: str = ""
    owner_user_id: str | None = None

    def dump(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def restore(cls, data: dict[str, Any] | None) -> Queue:
        queue = cls(**{k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__})
        if queue.phase in ACTIVE or queue.pending_command:
            queue.attention("Home Assistant restarted. The saved queue was interrupted; clear it and select a new sequence when the robot is idle.")
        # v0.8.1 used the ten-minute setup deadline as a settings-only barrier.
        # No motion command uses that duration; shorten only that legacy case.
        if queue.barrier_window == CONFIGURE_SECONDS:
            queue.not_before = min(queue.not_before, queue.command_at + ACK_SECONDS)
            queue.barrier_window = ACK_SECONDS
        return queue

    def attention(self, message: str) -> None:
        self._preserve_command_barrier()
        self.phase = "attention"
        self.error = message
        self.pending_command = ""
        self.next_pending = False
        self.start_uncertain = False
        self._reset_ready()

    def confirmed(self) -> None:
        """The outstanding command was observed, so nothing about it is uncertain."""
        self.pending_command = ""
        self.not_before = 0
        self.barrier_window = 0
        self.start_uncertain = False

    def _reset_ready(self) -> None:
        self.ready_since = self.ready_observed_at = 0

    def _ready_settled(self, snapshot: Snapshot, now: float, after: float = 0) -> bool:
        """Only fresh observations spanning the settle window can authorize a step."""
        if not snapshot.ready_for(self.cleaning_mode):
            self._reset_ready()
            return False
        if after > self.ready_since:
            self._reset_ready()
        if snapshot.observed_at <= 0 or snapshot.observed_at < after:
            return False
        if not self.ready_since:
            self.ready_since = now
            self.ready_observed_at = snapshot.observed_at
            return False
        return (now >= self.ready_since + READY_SETTLE_SECONDS
                and snapshot.observed_at >= self.ready_since + READY_SETTLE_SECONDS
                and snapshot.observed_at > self.ready_observed_at)

    def ack_window(self) -> float:
        """How long a dispatched command may take to become observable."""
        if self.pending_command in {"start", "resume"}:
            return START_SECONDS
        if self.pending_command == "configure":
            # Settings cannot launch cleaning. Allow one native update window for
            # a late write, independently of the longer preparation deadline.
            return ACK_SECONDS
        return ACK_SECONDS

    def ack_timeout_message(self) -> str:
        if self.pending_command in {"start", "resume"}:
            return ("The robot did not start the room within %d minutes. No retry was sent."
                    % (START_SECONDS // 60))
        return "The robot did not acknowledge the command within 60 seconds. No retry was sent."

    def _preserve_command_barrier(self) -> None:
        if self.pending_command in {"start", "configure", "pause", "resume", "return_to_dock", "stop", "device"}:
            # A cloud command may have been accepted before telemetry catches up.
            # Clearing the UI must not permit another start on that stale state, so the
            # barrier lasts as long as that command may still be acknowledged.
            window = self.ack_window()
            sent_at = max(self.command_at, self.settings_sent_at) if self.pending_command == "configure" else self.command_at
            if sent_at + window > self.not_before:
                self.not_before, self.barrier_window = sent_at + window, window

    def _validate_start(self, snapshot: Snapshot, now: float, mode: str = "vacuum") -> None:
        if self.phase in ACTIVE or self.phase == "attention" or self.pending_command:
            raise ValueError("A queue is active or needs attention. Clear it before starting another sequence.")
        self._validate_command_barrier(snapshot, now)
        if not snapshot.ready_for(mode):
            raise ValueError("The robot must be available, idle or docked, and have no unfinished cleaning job.")

    def _validate_command_barrier(self, snapshot: Snapshot, now: float) -> None:
        if self.not_before and (now < self.not_before or snapshot.observed_at < self.not_before):
            window = self.barrier_window or ACK_SECONDS
            wait = ("%d minutes" % (window // 60)) if window >= 120 else ("%d seconds" % window)
            raise ValueError("A previous command is still uncertain. Wait %s for a fresh robot update after its acknowledgement window, then check the robot." % wait)

    def should_finish(self, snapshot: Snapshot) -> bool:
        """Whether a wall-switch toggle should end the current job instead of starting one."""
        return (self.phase in ACTIVE or bool(self.pending_command) or
                snapshot.job == "on" or snapshot.vacuum in {"cleaning", "paused", "returning"} or
                snapshot.status in START_STATUS - {"charger_disconnected"})

    def finish(self, vacuum: str, snapshot: Snapshot, now: float, run_id: str):
        """Cancel all stages, then let native docking perform post-clean care."""
        if self.mode == "finish" and self.phase == "controlling":
            return None  # Repeated holds cannot restart cleaning or duplicate docking.
        # A pending start/pause/stop keeps its own barrier, which defers the home
        # command below. A device reservation is different: finishing throws away the
        # readback it is waiting for, so refuse instead of losing that uncertainty.
        if self.mode == "device" and self.phase == "controlling":
            raise ValueError("A dock or settings command is still being confirmed. Wait for it before finishing.")
        self._preserve_command_barrier()
        self.mode, self.phase = "finish", "controlling"
        self.vacuum, self.run_id = vacuum, run_id
        self.targets, self.stages = [], []
        self.setup, self.control_entities = {}, {}
        self.current_index = self.completed = 0
        self.next_pending = self.seen_job = False
        self.pending_command, self.error = "", ""
        self.start_uncertain = False
        self._reset_ready()
        self.started_at = now
        return self._observe_finish(snapshot, now)

    def _observe_finish(self, snapshot: Snapshot, now: float):
        if self.phase != "controlling":
            return None
        if not snapshot.robot_healthy:
            self.attention("Cleaning sequence cancelled. The robot is unavailable or has a robot fault; check it before docking.")
            return None
        if now - self.started_at >= 1800:
            self.attention("Cleaning sequence cancelled, but docking did not finish within 30 minutes. Check the robot; no retry was sent.")
            return None
        if self.not_before and (now < self.not_before or snapshot.observed_at < self.not_before):
            return None  # An accepted start may still arrive; never race it with home.
        if snapshot.observed_at < self.started_at:
            return None
        if self.pending_command:
            confirmed = snapshot.observed_at >= self.command_at and (
                self.pending_command == "stop" and snapshot.job == "off" or
                self.pending_command == "return_to_dock" and snapshot.vacuum in {"returning", "docked"})
            if confirmed:
                self.confirmed()
            elif now - self.command_at >= ACK_SECONDS:
                self.attention("Cleaning sequence cancelled, but the robot did not confirm finishing. Check it; no retry was sent.")
            return None
        servicing = snapshot.status in DOCK_CARE_STATUS
        if servicing or snapshot.vacuum == "returning":
            return None  # Do not interrupt dock care or issue duplicate home commands.
        if snapshot.vacuum == "docked" and snapshot.job == "off":
            self.phase = "cancelled"
            return None
        if snapshot.vacuum == "docked" and snapshot.job == "on":
            command, service = "stop", "stop"  # End a recharge break before it can resume.
        elif snapshot.status in CLEANING_STATUS | {"paused", "idle", "charger_disconnected"}:
            command, service = "return_to_dock", "return_to_base"
        else:
            return None
        if self.setup.get(command):
            self.attention("The robot resumed or stopped after the finish command. The sequence is cancelled; check the robot before retrying.")
            return None
        self.setup[command] = True
        self.pending_command, self.command_at = command, now
        return "vacuum", service

    def external_control(self, command: str, vacuum: str, snapshot: Snapshot,
                         now: float, run_id: str) -> tuple[str, str] | None:
        """Control an existing app-started job without adopting it as a queue."""
        if command not in {"pause", "resume", "return_to_dock", "stop"}:
            raise ValueError("Only pause, resume, stop and return to dock can control an existing job.")
        if not vacuum:
            raise ValueError("Choose the vacuum to control.")
        if self.phase in ACTIVE or self.phase == "attention" or self.pending_command:
            raise ValueError("A queue or command is active or needs attention. Resolve it before controlling another job.")
        self._validate_command_barrier(snapshot, now)
        if not self.validate_control_state(command, snapshot):
            return None
        # A terminal sequence is historical, not a job to resume. Discard all
        # stage bookkeeping so no observation can advance its old stages.
        self.mode, self.phase = "external", "controlling"
        self.vacuum, self.run_id = vacuum, run_id
        self.targets, self.stages = [], []
        self.setup, self.control_entities = {}, {}
        self.current_index = self.completed = 0
        self.started_at = self.baseline_end = self.finish_wait_at = 0
        self.seen_job = self.next_pending = False
        self.not_before = self.barrier_window = 0
        self.error = ""
        self.command_failure = {}
        self.start_uncertain = False
        self._reset_ready()
        self.pending_command, self.command_at = command, now
        return "vacuum", {"pause": "pause", "resume": "start", "return_to_dock": "return_to_base", "stop": "stop"}[command]

    @staticmethod
    def validate_control_state(command: str, snapshot: Snapshot) -> bool:
        """Recheck state both at acceptance and immediately before dispatch."""
        if command == "stop":
            if not snapshot.connected or snapshot.vacuum in {"unknown", "unavailable"} or snapshot.job not in {"on", "off"}:
                raise ValueError("The robot must be available before stopping.")
            return snapshot.job == "on" or snapshot.vacuum in {"cleaning", "paused", "returning"}
        allowed = snapshot.healthy_for(snapshot.settings.get("mode", "vacuum")) if command == "resume" else snapshot.robot_healthy
        if not allowed:
            raise ValueError("The robot is unavailable or has a fault that prevents this action.")
        if command == "pause" and snapshot.status not in CLEANING_STATUS | {"returning_home", "docking"}:
            raise ValueError("Pause is available while cleaning or returning; wait for mop servicing to finish.")
        if command == "resume" and not (snapshot.vacuum == snapshot.status == "paused" and snapshot.job == "on"):
            raise ValueError("The robot must confirm a paused, unfinished cleaning job before resuming.")
        if command == "return_to_dock":
            if snapshot.vacuum in {"docked", "returning"}:
                return False
            if snapshot.status not in CLEANING_STATUS | {"paused", "idle", "charger_disconnected"}:
                raise ValueError("Return to dock is unavailable while the robot is servicing.")
        return True

    def _observe_external(self, snapshot: Snapshot, now: float) -> None:
        if not self.pending_command:
            return
        if (not snapshot.connected or snapshot.vacuum in {"unknown", "unavailable"} or
                self.pending_command == "resume" and not snapshot.healthy_for(snapshot.settings.get("mode", "vacuum"))):
            self.attention("The robot has a fault, or telemetry is unavailable. Check the robot before another command.")
            return
        confirmed = snapshot.observed_at >= self.command_at and (
            (self.pending_command == "pause" and snapshot.vacuum == snapshot.status == "paused") or
            (self.pending_command == "resume" and snapshot.job == "on" and snapshot.status in START_STATUS) or
            (self.pending_command == "return_to_dock" and snapshot.vacuum in {"docked", "returning"}) or
            (self.pending_command == "stop" and snapshot.job == "off" and snapshot.vacuum in {"docked", "idle"})
        )
        if confirmed:
            self.phase, self.error = "idle", ""
            self.confirmed()
        elif now - self.command_at >= ACK_SECONDS:
            self.attention("The robot did not acknowledge the command within 60 seconds. No retry was sent.")

    def start_manual(self, vacuum: str, targets: list[str], setup: dict, stages: list[dict],
                     control_entities: dict[str, str], snapshot: Snapshot, now: float, run_id: str) -> tuple[str, str]:
        if not stages or len(stages) > 128:
            raise ValueError("The cleaning plan has no supported stages or exceeds 128 stages.")
        # A plan that names a mopping mode is blocked by an empty tank as a whole; a
        # room plan without one is judged by the room it starts with.
        plan_mode = setup.get("mode") or stages[0].get("mode") or "vacuum"
        self._validate_start(snapshot, now, plan_mode)
        self.mode = "manual"
        self.vacuum, self.run_id = vacuum, run_id
        self.targets, self.setup = list(targets), dict(setup)
        self.stages = [dict(stage) for stage in stages]
        self.control_entities = dict(control_entities)
        self.current_index = self.completed = 0
        self.error = ""
        self.not_before = self.barrier_window = self.dock_finish_at = 0
        return self._dispatch(snapshot, now)

    @property
    def cleaning_mode(self) -> str:
        """The mode of the job being dispatched, not the plan's first setting."""
        if self.mode != "manual":
            return "vacuum"
        if self.stages and self.completed == len(self.stages):
            return self.stages[-1].get("mode") or "vacuum"
        return self.stage.get("mode") or self.setup.get("mode") or "vacuum"

    @property
    def stage(self) -> dict:
        return self.stages[self.current_index] if self.mode == "manual" and 0 <= self.current_index < len(self.stages) else {}

    def _dispatch(self, snapshot: Snapshot, now: float) -> tuple[str, str]:
        """Every plan applies its stage settings first and then starts that room."""
        self.phase = "preparing"
        self.pending_command = "configure"
        self.command_at = now
        self.settings_sent_at = 0
        self.next_pending = False
        self.command_failure = {}
        self.start_uncertain = False
        self._reset_ready()
        return "configure", str(self.current_index)

    def _start_job(self, snapshot: Snapshot, now: float) -> tuple[str, str]:
        self.decision = "dispatching room %d" % (self.current_index + 1)
        self.phase = "starting"
        self.pending_command = "start"
        self.command_at = self.started_at = now
        self.baseline_end = float((snapshot.record or {}).get("end") or 0)
        self.seen_job = False
        self.finish_wait_at = 0
        self.next_pending = False
        return "manual", str(self.current_index)

    def command(self, command: str, snapshot: Snapshot, now: float) -> tuple[str, str] | None:
        if command == "stop":
            self._validate_command_barrier(snapshot, now)
            if self.pending_command:
                raise ValueError("Wait for the previous command to be confirmed before stopping.")
            physical = self.validate_control_state(command, snapshot)
            self.command("cancel", snapshot, now)
            if not physical:
                return None
            self.pending_command, self.command_at = "stop", now
            return "vacuum", "stop"
        if command == "cancel":
            self._preserve_command_barrier()
            self.start_uncertain = False
            self._reset_ready()
            self.phase = "cancelled"
            self.pending_command = ""
            self.next_pending = False
            self.error = ""
            return None  # Cancelling a queue never claims to stop the robot.
        if command == "return_to_dock":
            had_pending_command = bool(self.pending_command)
            self.command("cancel", snapshot, now)
            if snapshot.vacuum in {"docked", "returning"}:
                return None
            # Cancelling future stages must not permit a second cloud command
            # while the earlier start/pause/resume/home result is uncertain.
            try:
                self._validate_command_barrier(snapshot, now)
                if had_pending_command:
                    raise ValueError("A previous command was still awaiting acknowledgement.")
            except ValueError:
                self.attention("Queue cleared. A previous command is still uncertain; wait for a fresh robot update after its acknowledgement window before returning to dock.")
                return None
            if not snapshot.robot_healthy or not (snapshot.status in CLEANING_STATUS | {"paused", "idle"}):
                self.attention("Queue cleared. Return to dock is unavailable while the robot is servicing or its state is uncertain.")
                return None
            self.pending_command = "return_to_dock"
            self.command_at = now
            return "vacuum", "return_to_base"
        if command == "pause":
            if self.phase not in {"starting", "running"} or self.pending_command:
                raise ValueError("The queue is not ready to pause.")
            if self.next_pending:
                self.phase = "paused"
                self._reset_ready()
                return None
            if not snapshot.robot_healthy or snapshot.status not in CLEANING_STATUS | {"returning_home", "docking"}:
                raise ValueError("Pause is available while cleaning or returning; wait for mop servicing to finish.")
            self.phase = "paused"
            self.pending_command = "pause"
            self.command_at = now
            return "vacuum", "pause"
        if command == "resume":
            if self.phase != "paused" or self.pending_command:
                raise ValueError("The queue is not paused.")
            if self.next_pending:
                if not snapshot.ready_for(self.cleaning_mode):
                    raise ValueError("The robot is not ready for the next room.")
                self.phase = "running"
                self._reset_ready()
                return None  # Fresh settled readiness is checked by observe before dispatch.
            if not snapshot.healthy_for(self.cleaning_mode) or snapshot.vacuum != "paused" or snapshot.status != "paused" or snapshot.job != "on":
                raise ValueError("The robot must confirm a paused, unfinished cleaning job before resuming.")
            self.phase = "starting"
            self.pending_command = "resume"
            self.command_at = now
            return "vacuum", "start"
        raise ValueError("Unknown queue command.")

    def _observe_dock_completion(self, snapshot: Snapshot, now: float) -> None:
        """Observe native return/care after the last floor pass; never send motion."""
        if now - self.dock_finish_at >= DOCK_FINISH_SECONDS:
            self.attention("Floor cleaning finished, but return and dock care were not confirmed within 30 minutes. Check the robot; no command was sent.")
            return
        if snapshot.job == "on" or snapshot.status in CLEANING_STATUS:
            self.attention("Another cleaning job appeared after the final pass. Check the robot; no further command was sent.")
            return
        if snapshot.status == "paused" or snapshot.vacuum == "paused":
            self.attention("The robot paused before final docking was confirmed. Check the robot; no further command was sent.")
            return
        if snapshot.vacuum == "docked" and self._ready_settled(snapshot, now, self.dock_finish_at):
            self.phase = "completed"
            self.decision = "floor cleaning and final docking confirmed"
            return
        if snapshot.vacuum != "docked" or not snapshot.ready_for(self.cleaning_mode):
            self._reset_ready()
        self.decision = ("floor cleaning finished; waiting for final return and dock care "
                         "(vacuum=%s status=%s job=%s)" % (snapshot.vacuum, snapshot.status, snapshot.job))

    def observe(self, snapshot: Snapshot, now: float) -> tuple[str, str] | None:
        if self.mode == "finish":
            return self._observe_finish(snapshot, now)
        if self.mode == "external":
            self._observe_external(snapshot, now)
            return None
        if self.pending_command == "stop":
            self._observe_external(snapshot, now)
            if not self.pending_command and self.phase == "idle":
                self.phase = "cancelled"
            return None
        if self.pending_command == "return_to_dock":
            fresh = snapshot.observed_at == 0 or snapshot.observed_at >= self.command_at
            if fresh and snapshot.vacuum in {"docked", "returning"}:
                self.confirmed()
                self.decision = "return to dock confirmed"
            else:
                self.decision = "waiting for the dock command to be confirmed (%ds)" % int(now - self.command_at)
                if not snapshot.robot_healthy or now - self.command_at >= ACK_SECONDS:
                    self.attention("Return to dock was not confirmed. The remaining queue has been cleared; check the robot.")
            return None
        if self.phase not in ACTIVE:
            return None
        if not snapshot.healthy_for(self.cleaning_mode):
            self.attention("The robot or dock has a fault, or telemetry is unavailable. The queue is stopped for review.")
            self.decision = "stopped: robot, dock or telemetry unhealthy (vacuum=%s status=%s job=%s error=%s dock=%s connected=%s)" % (
                snapshot.vacuum, snapshot.status, snapshot.job, snapshot.error, snapshot.dock_error, snapshot.connected)
            return None
        if self.phase == "finishing":
            self._observe_dock_completion(snapshot, now)
            return None
        if self.pending_command == "configure":
            if snapshot.servicing_for(self.cleaning_mode):
                self._reset_ready()
                self.decision = "waiting for dock care before applying manual settings: %s" % snapshot.status
                if now - self.command_at >= CONFIGURE_SECONDS:
                    self.attention("Dock care did not finish within %d minutes while preparing the next pass. No cleaning was started."
                                   % (CONFIGURE_SECONDS // 60))
            elif not snapshot.ready_for(self.cleaning_mode):
                self.attention("The robot became busy while manual settings were being applied. No cleaning was started.")
                self.decision = "stopped: robot left the ready state while applying settings (vacuum=%s status=%s job=%s)" % (
                    snapshot.vacuum, snapshot.status, snapshot.job)
            elif snapshot.observed_at >= max(self.command_at, self.settings_sent_at) and all(
                snapshot.settings.get(key) == value for key, value in self.stage.get("settings", {}).items()
            ):
                if self._ready_settled(snapshot, now, max(self.command_at, self.settings_sent_at)):
                    return self._start_job(snapshot, now)
                self.decision = "manual settings confirmed; waiting for settled readiness and a fresh robot update"
                if now - self.command_at >= CONFIGURE_SECONDS:
                    self.attention("The robot did not remain ready with fresh settings within 10 minutes. No cleaning was started.")
            else:
                self._reset_ready()
                missing = sorted(key for key, value in self.stage.get("settings", {}).items()
                                 if snapshot.settings.get(key) != value)
                self.decision = "waiting for manual settings readback: %s (observed settings %s)" % (
                    ", ".join("%s=%s" % (k, self.stage["settings"][k]) for k in missing), snapshot.settings)
                if now - self.command_at >= CONFIGURE_SECONDS:
                    self.attention("The robot did not confirm the manual settings within %d minutes. No cleaning was started."
                                   % (CONFIGURE_SECONDS // 60))
            return None
        if self.pending_command:
            # Confirming against telemetry older than the command would let a state the
            # robot already had before we asked stand in for our command landing. A zero
            # observation time means the adapter could not date it; connectivity and
            # freshness are enforced separately in that case.
            fresh = snapshot.observed_at == 0 or snapshot.observed_at >= self.command_at
            ack = fresh and (
                (self.pending_command == "pause" and snapshot.vacuum == snapshot.status == "paused") or (
                    self.pending_command in {"start", "resume"} and snapshot.status in START_STATUS
                )
            )
            if ack:
                if self.pending_command != "pause":
                    self.phase = "running"
                    self.seen_job = self.seen_job or snapshot.job == "on"
                self.decision = "%s acknowledged by the robot" % self.pending_command
                self.confirmed()
            else:
                self.decision = "waiting for the %s to be acknowledged (%ds of %ds; vacuum=%s status=%s job=%s)" % (
                    self.pending_command, int(now - self.command_at), int(self.ack_window()),
                    snapshot.vacuum, snapshot.status, snapshot.job)
                if now - self.command_at >= self.ack_window():
                    self.attention(self.ack_timeout_message())
            return None
        if self.phase == "paused":
            # App/manual resume does not silently restart an unattended queue.
            self.decision = "paused"
            if snapshot.vacuum != "paused" and not self.next_pending:
                self.attention("The robot changed state outside this queue while paused. Review its current job.")
                self.decision = "stopped: the robot left the paused state outside this queue (vacuum=%s status=%s)" % (
                    snapshot.vacuum, snapshot.status)
            return None
        if snapshot.vacuum == "paused" or snapshot.status == "paused":
            self.phase = "paused"
            return None
        if self.next_pending:
            if now - self.finish_wait_at >= DOCK_FINISH_SECONDS:
                self.attention("The robot did not confirm settled readiness for the next pass within 30 minutes. No further cleaning was started.")
            elif snapshot.job == "on":
                self.attention("Another job started before the next queued room. The queue was stopped.")
                self.decision = "stopped: another job started before the next room (status=%s)" % snapshot.status
            elif self._ready_settled(snapshot, now, self.finish_wait_at):
                return self._dispatch(snapshot, now)
            else:
                self.decision = "waiting for settled readiness before the next pass (vacuum=%s status=%s job=%s dock=%s)" % (
                    snapshot.vacuum, snapshot.status, snapshot.job, snapshot.dock_error)
            return None
        if snapshot.job == "on":
            self.seen_job = True
            self.finish_wait_at = 0
            self.decision = "cleaning (job active, status=%s)" % snapshot.status
            return None  # Includes low-battery breaks and mop washing.
        if not self.seen_job:
            # A job can acknowledge by washing its mops before in_cleaning turns on.
            # Preparation is not completion, and never advances rooms.
            if snapshot.status in START_STATUS and now - self.started_at < PREPARE_SECONDS:
                self.decision = "waiting for the job to start (status=%s, %ds of %ds)" % (
                    snapshot.status, int(now - self.started_at), int(PREPARE_SECONDS))
                return None
            self.attention("No active cleaning job was observed after preparation. Completion cannot be confirmed.")
            self.decision = "stopped: no active job was ever observed after preparation (status=%s after %ds)" % (
                snapshot.status, int(now - self.started_at))
            return None
        record = snapshot.record or {}
        end = float(record.get("end") or 0)
        begin = float(record.get("begin") or 0)
        fresh = end > self.baseline_end and begin >= self.started_at - 3 and end >= begin
        if not fresh:
            self.decision = "waiting for a completion record (record begin=%s end=%s complete=%s error=%s; baseline end=%s, started=%s)" % (
                record.get("begin"), record.get("end"), record.get("complete"), record.get("error"),
                self.baseline_end, self.started_at)
            if not self.finish_wait_at:
                self.finish_wait_at = now
            elif now - self.finish_wait_at >= FINISH_SECONDS:
                self.attention("The job ended without a matching completion record. No next room was started.")
            return None
        complete, error, reason = record.get("complete"), record.get("error"), record.get("finish_reason")
        if complete != 1 or error != 0 or (reason is not None and reason not in SUCCESS_REASONS):
            self.attention("The cleaning job was interrupted, failed, or did not report successful completion. No next room was started.")
            return None
        self.completed += 1
        self.current_index += 1
        self._reset_ready()
        if self.completed == len(self.stages):
            self.phase = "finishing"
            self.dock_finish_at = now
            self.decision = "floor cleaning finished; waiting for final return and dock care"
            return None
        self.finish_wait_at = now
        self.next_pending = True
        # Deliberately wait for the next observation and for dock/idle readiness.
        return None
