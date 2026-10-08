# Home Assistant queue companion

The card selects an ordered list of the **robot's own rooms** (the rooms named in the Roborock app, addressed by segment), each with its own cleaning mode and settings. This companion executes the plan in Home Assistant, so closing the dashboard or locking the phone does not stop the sequence. Roborock app **routines are not used**: they cannot be introspected, cannot carry per-room settings, and a routine that internally ends one job and starts another cannot be chained safely. Cleaning rooms in a chosen order needs one job per room, which no single native command provides.

This version supports **one robot and one queue per Home Assistant instance**, using the native Roborock V1 integration. Other robot platforms and Roborock protocols are not implicitly supported. Rooms are read from the robot's current map, so a room can be cleaned on its own or together with a group, and the app keeps owning the map itself. Each room's job must produce its own cleaning record before the next room starts.

## Installation

### HACS

Add `https://github.com/bitosome/ha-robot-cleaner-queue` as a **Custom repository** of category **Integration**, download it, and restart Home Assistant. HACS tracks updates, which still need a restart because integrations load at startup. HACS installs the Python files only: Home Assistant will not load them until `robot_cleaner_queue:` exists in your configuration, which the package include below supplies. Without it nothing appears and no error is raised.

### Manual

1. Copy the `custom_components/robot_cleaner_queue` directory into Home Assistant's `/config/custom_components/` directory. Home Assistant only loads a YAML integration that appears in `configuration.yaml`, so this step is required by both install paths below: the package in step 2 supplies the `robot_cleaner_queue:` key.
2. Copy `packages/robot_cleaner_queue.yaml` to your HA packages directory. Include it through your existing `homeassistant.packages` configuration. For example, installations without existing packages can add:

   ```yaml
   homeassistant:
     packages:
       robot_cleaner_queue: !include packages/robot_cleaner_queue.yaml
   ```

   Merge with existing configuration; do not replace another `homeassistant:` or `packages:` section.
3. Check HA configuration, then restart Home Assistant. Installation and restart do not send a robot command.
4. Confirm `sensor.robot_cleaner_queue`, `script.robot_cleaner_queue_control` and the `robot_cleaner_queue.control` action exist. Configure the card's `queue_entity`.

The card is a separate HACS repository and does not install these Python files; add this repository as an integration as above. No Roborock credentials, tokens, new cloud client, extra polling, or native integration modification is required.

## Card/action contract

Prefer calling the fast custom action directly, so validation errors can be shown immediately:

```yaml
action: robot_cleaner_queue.control
data:
  command: start_manual
  vacuum: vacuum.robot
  rooms:
    - id: "0_12"          # a room id from get_capabilities.robot_rooms
      mode: vacuum_mop
      suction: max
      water: high
      route: standard
    - id: "0_13"
      mode: mop
      water: low
      route: deep
```

The optional `script.robot_cleaner_queue_control` wrapper accepts the same fields. Calling it through `script.turn_on` does not propagate validation exceptions to its caller; direct `robot_cleaner_queue.control` is preferred. The service waits for the initial HA command dispatch, then returns. A successful service return does not mean the robot started: `pending_command` remains until physical telemetry acknowledges it.

Other commands:

| Command | Effect |
| --- | --- |
| `pause` | Pause the current cleaning operation when its state supports pausing. During a between-room wait, hold the sequence without sending a robot command. |
| `resume` | Resume a confirmed paused job, or dispatch the next selected room after a between-room pause. |
| `cancel` | Clear the remaining queue only. The current robot operation continues. |
| `return_to_dock` | Clear the remaining queue first, then request docking when the robot's state permits it. Mop servicing and uncertain states are not interrupted. |

The card contract is `control_version: 5`. Version 0.2.2 can pause/resume/dock an app-started job with an explicit vacuum. It stores `mode: external` without targets or stages; acknowledgement returns it to idle and cannot advance an old plan. Resume requires a paused, unfinished job. Active, attention and uncertain commands cannot be bypassed by controlling a different robot.

For non-start commands, pass the same `vacuum` to protect against a card configured for another robot. With execution_version 3, an explicit new start can replace historical `attention` after fresh native readiness and uncertainty checks. It starts the requested plan from its beginning, never resumes old stages. `cancel` remains an optional queue-only dismissal, available even when the robot is offline. Clearing an unacknowledged command preserves a safety barrier: another start requires that command's window to expire — fifteen minutes for a start or resume, sixty seconds for settings or a control command — and a new native poll after that window confirming idle/job-off. The barrier is released as soon as the robot is observed confirming that command, so a stop the robot has already acknowledged never delays docking. Cancelling cannot reuse a stale docked state to send a duplicate job. Only an idle/docked robot with no unfinished job can start a new sequence. Starts cannot replace an existing queue or unfinished job.

`start_manual` accepts either a list of room requests (`{"id": "0_12", "mode": ..., "suction": ..., "water": ..., "route": ..., "repeat": ...}`) or the legacy list of Home Assistant area IDs (`["kitchen"]`). Room ids come from `get_capabilities.robot_rooms` and must exist on the robot's current map. Each room's settings fall back to the request's `setup` defaults, and a plan may hold 1–32 rooms and at most 128 stages. Rooms cannot repeat. Area-shaped requests keep working for plans saved before rooms existed.

The optional `cleaning_entity`, `status_entity`, `error_entity` and `last_clean_end_entity` service fields are accepted for frontend configuration compatibility. Safety decisions use the matching native Roborock coordinator's coherent cached state, rather than trusting caller-supplied sensor mappings.

`sensor.robot_cleaner_queue` states:

- `idle`: no queue is running; also used after an external-job control is acknowledged.
- `controlling`: a pause/resume/dock command for an externally started job is awaiting fresh acknowledgement; new commands and starts are blocked.
- `preparing`: manual settings are being applied and awaiting fresh native readback.
- `starting`: a start/resume command is awaiting observed acknowledgement.
- `running`: the acknowledged room job is active or waiting to return to the dock before the next room.
- `paused`: the queue is paused; check `pending_command` for pause acknowledgement.
- `finishing`: every floor pass succeeded; final return and active dock care are still being observed.
- `completed`: every stage succeeded and final stable docking was confirmed. Passive drying may continue.
- `cancelled`: remaining rooms were cleared; the robot may still be active.
- `attention`: execution stopped because a fault, restart, conflict or uncertain result requires review.

Every plan exposes `mode: manual`, ordered `targets` (room ids, or Home Assistant area ids for a legacy plan), `address` (`room` when the robot's own segments are cleaned, `area` when a Home Assistant area is), `setup`, and `stages` (target, cleaning mode, frozen settings, zero-based room/pass/repeat indices). `current_index` and `completed` count stages, while `room_index + 1` preserves the displayed room sequence. A room plan stores its per-room settings under `setup.rooms`. Empty targets represent whole-home cleaning.

Attributes: `vacuum`, `mode`, `address`, ordered `targets`, `setup`, `stages`, zero-based `current_index`, `completed` count, `pending_command`, `decision`, `error`, `run_id`, `waiting_for_dock`, `control_version` and `command_barrier_until` (UTC epoch seconds or null). The tile order shown to a person is `index + 1`. `completed` reports successful cleaning records; robot location alone never proves room coverage.

## Completion and interruption rules

A normal room transition requires all of these:

1. A room was dispatched once and the robot acknowledged a preparing, washing or cleaning state within 15 minutes. A job dispatched immediately after a finished room can take minutes to appear: the robot still has to wash or dry its mop, empty dust or top up its battery, and it ignores a new job until that servicing ends. Production traces show a job first observed 677 seconds after the dispatch, so the shorter window abandoned a sequence that was in fact proceeding. The command is never re-sent while this window runs. An active job must subsequently be observed; initial mop preparation can take up to a further ten minutes without being mistaken for completion.
2. The job-active flag is now off. Pauses, mop washing and low-battery charging breaks remain part of the same job while that flag is on.
3. The native cached cleaning record is newer than the pre-command record and began no earlier than three seconds before the command (to allow whole-second timestamps).
4. Every confirmation needs telemetry newer than the command that asked for it, so a state the robot already had cannot stand in for the command landing. A zero observation time means the adapter could not date the reading, and connectivity is checked separately in that case. The record explicitly says `complete == 1`, `error == 0`, and its finish reason is successful when present. Missing/unknown completion fields, a manual interruption, an unreachable area or a washing failure stop the queue.
5. Before another room is dispatched, the robot is healthy and idle/docked rather than returning, washing, emptying or charging for an unfinished job.

The companion waits up to three minutes after job-off for the corresponding completion record. It never infers completion from the current-room sensor, a stale percentage, a generic docked state, or `last_clean_end` alone. The native end timestamp is also updated for unsuccessful records.

The initiating Home Assistant user's control permission is checked on the vacuum and its setting entities before the queue changes. The caller's user ID is retained in HA's private queue storage and propagated to native commands. Permissions are fetched again before each later dispatch, so deleting/deactivating a user or revoking entity access stops progression. This does not grant additional access; automations without a user context retain normal HA system behavior.

No automatic retries are sent. Faults and lost telemetry stop progression for review. HA commands from other controls stop this queue to avoid competing writers, and so does a Roborock app routine or schedule that starts its own job. App/device interruption is caught by the cleaning record's completion/finish reason; changes that produce indistinguishable successful records cannot be attributed to a particular caller.

Pause and return-to-dock also require observed acknowledgement within 60 seconds; a start or resume gets the 15-minute window and settings preparation may wait ten minutes, but cancelling settings retains only a sixty-second update barrier. Queued rooms and position are persisted in HA's storage before dispatch. An HA restart or shutdown preserves the sequence for inspection and changes it to `attention`; it never resumes cleaning automatically. Clear and reselect the desired remaining rooms after checking the robot. Cancelling or docking clears progression before sending another robot action.

## Compatibility and validation

The read-only adapter was reviewed against **Home Assistant Core 2026.9.4** and **python-roborock 7.4.2**. It reads the existing coordinator's `data.clean_summary.last_clean_record` (`begin`, `end`, `complete`, `error`, `finish_reason`) because those completion fields are not exposed by standard HA entities. This is an internal compatibility boundary; check it when upgrading HA. An incompatible model or unavailable structure fails closed. A real device run has not been performed during development.

References: [HA Roborock documentation](https://www.home-assistant.io/integrations/roborock/), [HA sensor implementation](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/roborock/sensor.py), [HA coordinator](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/roborock/coordinator.py), [Roborock cleaning records](https://github.com/Python-roborock/python-roborock/blob/v7.4.2/roborock/data/v1/v1_containers.py), [finish reasons](https://github.com/Python-roborock/python-roborock/blob/v7.4.2/roborock/data/v1/v1_code_mappings.py).

Run the offline production-code trace tests without HA or a robot:

```sh
python3 -B test/backend_queue_test.py
python3 -B test/backend_manual_test.py
python3 -B test/backend_controls_test.py
```

These test ordered success with per-room settings, low-battery/wash breaks, interrupted records, faults, missing acknowledgements, stale records, pause/resume, cancellation, return-to-dock, concurrent starts, restart behavior and room plan validation. They do not prove physical operation or native HA runtime compatibility.

To remove the companion, first clear its queue, remove the package include and custom component, then restart HA. Card resources and the native Roborock integration are independent. Removing this queue does not issue a robot command.

### Mode-specific dock faults and auxiliary controls

v0.3.0 retains the native dock fault identity. Roborock V1 `water_empty` (code 38, python-roborock 7.4.2) permits vacuum-only work: a plan that names a mopping mode is refused, and a room plan is judged by the room it starts with. Every later stage is re-checked, so a mopping room waits for water instead of starting. The exception is checked at acceptance, every settings write, final dispatch, observation and each subsequent stage. Settings are never rewritten to vacuum-only. Other dock faults and missing robot telemetry fail closed.

`control: stop` differs from `cancel`: it cancels future stages and sends `vacuum.stop`, awaiting fresh idle/docked telemetry with `in_cleaning == 0`. It does not replace a command already awaiting acknowledgement, and `finish`/`toggle` refuse while a dock or settings reservation is still being confirmed instead of discarding its readback. Pause/Home do not require a healthy water tank; Resume still checks the cleaning mode.

`robot_cleaner_queue.device_control` accepts `vacuum`, an allowlisted `control` and its native `value`. Registry unique IDs and config-entry ownership select the target; callers cannot pass arbitrary entity IDs. Settings/dock actions persist a `mode: device` reservation under the same queue lock, preserve the caller's permissions, and wait for fresh entity readback. Failure, timeout or restart requires review and retains the uncertainty barrier. A rejected concurrent command does not cancel an existing reservation. Read-only `get_capabilities` includes enabled/available, permission-filtered `device_entities` and `control_version: 5`.

Dock starts require a docked idle robot with no unfinished job. Washing requires a healthy dock; emptying/drying may proceed with the specific water-empty warning. Turning an already-running dock action off does not require water. Native robot/dock firmware remains authoritative. `locate` plays a sound and does not adopt or advance any cleaning plan. Map images and maintenance values are read-only.


## Diagnosing a stopped sequence

Every step is logged under the `custom_components.robot_cleaner_queue` logger, and the queue keeps the last 100 steps in memory:

- **DEBUG** — each observation that differs from the previous one: robot state, status, job flag, fault, dock state, observation time, the cleaning record, and the engine's own reason for the current state (`decision`).
- **INFO** — every received command with its caller, the dispatch of each room, and deferrals such as *"native setting entities are unavailable: water; waiting"*.
- **WARNING** — a refusal or failed command, with allowlisted error classification. Raw native messages, payloads and tracebacks are excluded from command diagnostics.

```yaml
action: robot_cleaner_queue.get_diagnostics
data:
  vacuum: vacuum.robot
response_variable: diagnostics
```

The response contains the persisted `queue`, the `robot` observation, the saved plan keys, and up to 100 `events` with time, kind, detail, phase, decision, pending command, room index and completed count — enough to reconstruct what a stopped sequence was waiting for without reading the log.

A dock that is servicing hides the native setting entities for a while. That is not a plan change: a manual stage waits for them (logging the deferral) instead of stopping, and the settings readback window is 10 minutes because writing settings is not motion. A stopped sequence retains safe error classification; the original native message is deliberately not retained.

## Persisted plan — v0.4.0, rooms since v0.7.0

`save_preset` stores one validated plan per vacuum in the separate version-1 HA store `robot_cleaner_queue_presets`. Writes share the controller lock, and the new plan is published to memory only after durable save. Saving requires control permissions and never writes robot settings. `get_capabilities` exposes the authorized saved plan, current map and control_version 5.

A plan is `{"source": "rooms", "rooms": [...], "setup": {...}, "map_id": <flag>}`. The stored `rooms` are exactly what `start_manual` accepts: room requests, or Home Assistant area ids for a plan saved before rooms existed. `source: "manual"` is accepted as a legacy alias. The legacy `presets` field is ignored.

`toggle` and `toggle_saved` are the wall-switch entry points and behave identically: while a job is active or uncertain they cancel and dock instead, and when idle they start the saved plan for that vacuum, failing with a validation error when no plan is saved. Plans retain map identity and are revalidated against the current map and robot rooms before running. Storage survives queue clearing and restarts without triggering a run.


### Inline room-plan release (0.8.0)

The service metadata and bundled wrapper now match the room schema: no `start` routine command or routine selectors. `save_preset` accepts `rooms` and `manual` sources; old app-routine plans are rejected before execution. Rejected starts preserve the existing queue's address mode. Per-room stage indices remain room indices even when a room has multiple passes.

Configuration resumes only explicitly deferred, unsent setting writes. Successfully sent keys are not repeated; errors remain terminal and no uncertain start is retried. Native exception messages are omitted from diagnostic events. `get_diagnostics` refuses a different robot's bound queue.

The native device allowlist includes selected map and optional off-peak controls. Discovery exposes available tank/attachment/history telemetry read-only. Selecting a map while a robot has an unfinished native job is rejected even if this queue is idle.

## Dock care during preparation

Version 0.8.1 handles dock care beginning after a brief charging state between
passes. If dust emptying, mop washing or mop attachment begins while settings are
being applied, preparation waits with no further writes or cleaning command. Only
settings not yet successfully sent resume when the dock is ready, and fresh
readback must match before cleaning starts. This wait retains the ten-minute
configuration deadline. A competing job, unknown state, fault, cancellation or
uncertain service failure still stops the sequence; no command is retried.

## Shared room preferences — v0.9.0

`save_preferences` stores `{vacuum, revision, map_id, defaults, rooms}` separately
from the saved switch plan. `rooms` is a dictionary of current-map room IDs to
complete settings; omission removes an override on that floor only. Every write
validates native capabilities and the current map, checks the caller’s control
permissions, and compares the revision. A stale revision is rejected instead of
overwriting another user’s changes. Storage succeeds before publishing the new
revision; no robot commands are sent.

Profiles are keyed by vacuum, never by user, in Home Assistant’s
`.storage/robot_cleaner_queue_preferences`. Defaults and other-floor overrides
survive restarts. `get_capabilities.preferences` returns the shared profile and
`sensor.robot_cleaner_queue.preferences_revisions` notifies dashboards to refresh.
Saved switch plans and active queues remain frozen and independent of preferences.

Settings-only cancellation uses a 60-second barrier from the latest possible
settings write plus a fresh native poll. It no longer inherits the ten-minute
configuration deadline. Older stored ten-minute settings barriers migrate on
restart; fifteen-minute start/resume barriers are unchanged.


### Shared saved-plan revision contract

`get_capabilities.saved_plan_revision` is always present when the companion supports
revision-aware plan saves, including `0` before any plan exists or for a legacy plan
without a revision. Clients can detect this field without changing `control_version: 5`.
The saved plan also contains `saved_preset.revision`; successful saves increment it.
Send the revision read with the plan as the optional `save_preset.revision` argument
to reject an overwrite after another user saves. Older clients may omit the argument.

`sensor.robot_cleaner_queue.saved_plan_revisions` publishes the revision by vacuum
and notifies connected dashboards only after a durable save. Native room plans store
all effective settings, including defaults, so later robot setting changes cannot
alter the wall-switch plan. Area plans retain `source: manual`; native room plans use
`source: rooms`. Unreadable plan or preference storage is preserved and cannot be
overwritten by a subsequent save.


### Readiness, finishing and uncertain starts (v0.10.0)

After each floor pass, the next stage waits for healthy, job-off readiness to hold
for at least 15 seconds and be confirmed by a newer native observation. Settings
readback uses the same gate after the latest settings write. Repeated ticks over
one cached state cannot qualify. Observed dock care resets the gate; passive mop
drying does not block another clean. Dock readiness waits are bounded and do not
send a cleaning command while waiting.

The last successful floor record enters `finishing`, an active phase with all
stages complete. Native return, mop washing and dust emptying remain visible;
`completed` requires fresh settled docking/job-off readiness. This observes dock
care rather than forcing it or claiming that every possible dock action ran.
Drying remains visible independently after completion. A 30-minute failure to
confirm final docking requires attention. Restart never resumes a sequence.

`get_capabilities.execution_version: 2` and the queue sensor's matching attribute
identify this contract. Sensor attributes include `floor_cleaning_complete`,
`dock_status`, nullable `dock_drying`, `start_uncertain`, and `command_failure`.
Dock telemetry continues to update when the floor sequence is inactive without
persisting unchanged queue state on every native poll.

A failure after entering the native room-start API leaves the original start
under observation, without resending or extending its acknowledgement deadline.
A late native acknowledgement can continue the sequence; timeout still requires
attention. Native transports may fall back internally, and HA may fail while
refreshing after a successful send, so even a rejection code does not prove that
no earlier attempt was accepted. Validation or interruption before dispatch does
not create a motion uncertainty barrier.

Failure metadata survives restart in the queue store. It contains bounded,
allowlisted exception types, numeric error codes, translation keys, operation,
setting key where applicable, attempt flag and timestamp. Raw error messages,
service payloads, traceback contents, credentials and placeholders are excluded.
Categories are diagnostic only and never authorize a retry.


## Stopped-run recovery — v0.10.1

Historical failure does not own a new robot job. Explicit Start or the saved-plan
wall-switch action may replace attention once the robot has fresh, healthy,
idle/job-off telemetry and any uncertain command window has expired with a later
native update. Pause/resume/stop/dock can control a later native app job without
adopting the old plan. Clearing stopped history requires permission on the vacuum
only, not obsolete setting entities, and never sends a robot command.

The sensor and capability contract exposes execution_version 3. Dynamic attributes
recovery_ready and recovery_reason explain whether a new plan may replace history;
the actual requested mode is validated again at dispatch. All initial starts and
native dispatches require a known observation no older than 90 seconds. Queue-only
clearing and local plan editing remain available without fresh robot telemetry.

HA 2026.9.4 omits some known V1 mop/wash activities from its vacuum state mapping.
For connected native observations only, the adapter translates those verified numeric
states when HA reports unknown. It never overrides unavailable/error/disconnected
states or guesses an unrecognized code. robot_activity, robot_connected and
robot_observed_at let the card apply the same narrowly scoped display fallback.
No new polling, protocol commands or physical retries are introduced.
