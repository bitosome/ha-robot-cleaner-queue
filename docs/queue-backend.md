# Home Assistant queue companion

The card selects an ordered list of existing Roborock **routine buttons**, or a [manual cleaning plan](manual-cleaning.md) using native Home Assistant areas and robot settings. This companion executes it in Home Assistant, so closing the dashboard or locking the phone does not stop the sequence. Native Roborock area cleaning does not promise room order; submitting several areas in one command cannot implement an elevator-style queue.

This first version supports **one robot and one queue per Home Assistant instance**, using the native Roborock V1 integration. Other robot platforms and Roborock protocols are not implicitly supported. Room routines must each represent the room named on its tile. A routine may contain multiple internal steps; its final cleaning record must represent the entire routine before this companion can safely chain it. Set up and verify one-room routines in the Roborock app. Their suction, mopping and repetition settings remain owned by that app.

## Installation

### HACS

Add `https://github.com/bitosome/ha-robot-cleaner-queue` as a **Custom repository** of category **Integration**, download it, and restart Home Assistant. HACS then tracks updates, which still need a restart because integrations load at startup.

### Manual

1. Copy the `custom_components/robot_cleaner_queue` directory into Home Assistant's `/config/custom_components/` directory.
2. Copy `packages/robot_cleaner_queue.yaml` to your HA packages directory. Include it through your existing `homeassistant.packages` configuration. For example, installations without existing packages can add:

   ```yaml
   homeassistant:
     packages:
       robot_cleaner_queue: !include packages/robot_cleaner_queue.yaml
   ```

   Merge with existing configuration; do not replace another `homeassistant:` or `packages:` section.
3. Check HA configuration, then restart Home Assistant. Installation and restart do not send a robot command.
4. Confirm `sensor.robot_cleaner_queue`, `script.robot_cleaner_queue_control` and the `robot_cleaner_queue.control` action exist. Configure the card's `queue_entity` and room preset buttons.

The card is a separate HACS repository and does not install these Python files; add this repository as an integration as above. No Roborock credentials, tokens, new cloud client, extra polling, or native integration modification is required.

## Card/action contract

Prefer calling the fast custom action directly, so validation errors can be shown immediately:

```yaml
action: robot_cleaner_queue.control
data:
  command: start
  vacuum: vacuum.robot
  presets:
    - button.robot_kitchen
    - button.robot_office
    - button.robot_bedroom
```

The optional `script.robot_cleaner_queue_control` wrapper accepts the same fields. Calling it through `script.turn_on` does not propagate validation exceptions to its caller; direct `robot_cleaner_queue.control` is preferred. The service waits for the initial HA command dispatch, then returns. A successful service return does not mean the robot started: `pending_command` remains until physical telemetry acknowledges it.

Other commands:

| Command | Effect |
| --- | --- |
| `pause` | Pause the current cleaning operation when its state supports pausing. During a between-room wait, hold the sequence without sending a robot command. |
| `resume` | Resume a confirmed paused job, or dispatch the next selected room after a between-room pause. |
| `cancel` | Clear the remaining queue only. The current robot operation continues. |
| `return_to_dock` | Clear the remaining queue first, then request docking when the robot's state permits it. Mop servicing and uncertain states are not interrupted. |

Version 0.2.2 can pause/resume/dock an app-started job with an explicit vacuum. It stores `mode: external` without presets or stages; acknowledgement returns it to idle and cannot advance an old plan. Resume requires a paused, unfinished job. Active, attention and uncertain commands cannot be bypassed by controlling a different robot.

For non-start commands, pass the same `vacuum` to protect against a card configured for another robot. An `attention` queue must be cleared with `cancel` before a new queue can start. Clearing an unacknowledged start/resume preserves a safety barrier: another start requires that command's acknowledgement window to expire (15 minutes for a start or resume, 60 seconds for a control command) and a new native poll after that window confirming idle/job-off. The barrier lasts as long as the start it guards, because a late routine can still arrive. It is released as soon as the robot is observed confirming that command, so a stop the robot has already acknowledged never delays docking. Cancelling cannot reuse a stale docked state to send duplicate routines. Only an idle/docked robot with no unfinished job can start a new sequence. Starts cannot replace an existing queue or unfinished job.

The service accepts 1–32 distinct preset entity IDs. It verifies that each is an enabled, available native Roborock **routine** button on the selected vacuum's device and config entry. It rejects maintenance-reset buttons and arbitrary entities. An `unknown` routine button state is valid before its first press.

The optional `cleaning_entity`, `status_entity`, `error_entity` and `last_clean_end_entity` service fields are accepted for frontend configuration compatibility. Safety decisions use the matching native Roborock coordinator's coherent cached state, rather than trusting caller-supplied sensor mappings.

`sensor.robot_cleaner_queue` states:

- `idle`: no queue is running; also used after an external-job control is acknowledged.
- `controlling`: a pause/resume/dock command for an externally started job is awaiting fresh acknowledgement; new commands and starts are blocked.
- `preparing`: manual settings are being applied and awaiting fresh native readback.
- `starting`: a start/resume command is awaiting observed acknowledgement.
- `running`: the acknowledged routine is active or waiting to return to the dock before the next room.
- `paused`: the queue is paused; check `pending_command` for pause acknowledgement.
- `completed`: every selected routine reported successful completion.
- `cancelled`: remaining rooms were cleared; the robot may still be active.
- `attention`: execution stopped because a fault, restart, conflict or uncertain result requires review.

Manual queues also expose `mode: manual`, ordered `targets` (HA area IDs), `setup` (chosen settings), and `stages` (target, cleaning mode, zero-based room/pass/repeat indices). `current_index` and `completed` count stages, while `room_index + 1` preserves the displayed area sequence. Empty targets represent whole-home cleaning. Preset queues expose `mode: preset`.

Attributes: `vacuum`, ordered `presets`, zero-based `current_index`, `completed` count, `pending_command`, `error`, `run_id`, and `waiting_for_dock`, and `command_barrier_until` (UTC epoch seconds or null). The tile order shown to a person is `index + 1`. `completed` reports successful routine records; robot location alone never proves room coverage.

## Completion and interruption rules

A normal room transition requires all of these:

1. A preset was sent once and the robot acknowledged a preparing, washing or cleaning state within 15 minutes. A routine pressed immediately after a finished room can take minutes to appear: the robot still has to wash or dry its mop, empty dust or top up its battery, and it ignores a routine until that servicing ends. Production traces show a routine first observed 677 seconds after the press, so the shorter window abandoned a sequence that was in fact proceeding. The command is never re-sent while this window runs. An active job must subsequently be observed; initial mop preparation can take up to a further ten minutes without being mistaken for completion.
2. The job-active flag is now off. Pauses, mop washing and low-battery charging breaks remain part of the same job while that flag is on.
3. The native cached cleaning record is newer than the pre-command record and began no earlier than three seconds before the command (to allow whole-second timestamps).
4. The record explicitly says `complete == 1`, `error == 0`, and its finish reason is successful when present. Missing/unknown completion fields, a manual interruption, an unreachable area or a washing failure stop the queue.
5. Before another preset is dispatched, the robot is healthy and idle/docked rather than returning, washing, emptying or charging for an unfinished job.

The companion waits up to three minutes after job-off for the corresponding completion record. It never infers completion from the current-room sensor, a stale percentage, a generic docked state, or `last_clean_end` alone. The native end timestamp is also updated for unsuccessful records.

The initiating Home Assistant user's control permission is checked on the vacuum and every selected preset before the queue changes. The caller's user ID is retained in HA's private queue storage and propagated to native commands. Permissions are fetched again before each later dispatch, so deleting/deactivating a user or revoking entity access stops progression. This does not grant additional access; automations without a user context retain normal HA system behavior.

No automatic retries are sent. Faults and lost telemetry stop progression for review. HA commands from other controls stop this queue to avoid competing writers. App/device interruption is caught by the cleaning record's completion/finish reason; changes that produce indistinguishable successful records cannot be attributed to a particular caller. A single-room routine that internally ends and starts another independent job is unsuitable for automatic chaining and must be simplified in the app.

Pause and return-to-dock also require observed acknowledgement within 60 seconds; a start or resume gets the 15-minute window described above. Queued presets and position are persisted in HA's storage before dispatch. An HA restart or shutdown preserves the sequence for inspection and changes it to `attention`; it never resumes cleaning automatically. Clear and reselect the desired remaining rooms after checking the robot. Cancelling or docking clears progression before sending another robot action.

## Compatibility and validation

The read-only adapter was reviewed against **Home Assistant Core 2026.9.4** and **python-roborock 7.4.2**. It reads the existing coordinator's `data.clean_summary.last_clean_record` (`begin`, `end`, `complete`, `error`, `finish_reason`) because those completion fields are not exposed by standard HA entities. This is an internal compatibility boundary; check it when upgrading HA. An incompatible model or unavailable structure fails closed. A real device run has not been performed during development.

References: [HA Roborock documentation](https://www.home-assistant.io/integrations/roborock/), [HA sensor implementation](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/roborock/sensor.py), [HA coordinator](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/roborock/coordinator.py), [Roborock cleaning records](https://github.com/Python-roborock/python-roborock/blob/v7.4.2/roborock/data/v1/v1_containers.py), [finish reasons](https://github.com/Python-roborock/python-roborock/blob/v7.4.2/roborock/data/v1/v1_code_mappings.py).

Run the offline production-code trace tests without HA or a robot:

```sh
python3 -B test/backend_queue_test.py
python3 -B test/backend_manual_test.py
python3 -B test/backend_controls_test.py
```

These test ordered success, low-battery/wash breaks, interrupted records, faults, missing acknowledgements, stale records, pause/resume, cancellation, return-to-dock, concurrent starts, restart behavior and routine validation. They do not prove physical operation or native HA runtime compatibility.

To remove the companion, first clear its queue, remove the package include and custom component, then restart HA. Card resources and the native Roborock integration are independent. Removing this queue does not issue a robot command.

### Mode-specific dock faults and auxiliary controls

v0.3.0 retains the native dock fault identity. Roborock V1 `water_empty` (code 38, python-roborock 7.4.2) permits an explicit manual vacuum-only plan. Since v0.4.0 it also permits launching native app presets; Roborock decides whether their mop operations can proceed. The exception is checked at acceptance, every settings write, final start dispatch, observation and each subsequent stage. Explicit manual mopping plans remain blocked. Preset settings are never rewritten to vacuum-only. Other dock faults and missing robot telemetry fail closed.

`control: stop` differs from `cancel`: it cancels future stages and sends `vacuum.stop`, awaiting fresh idle/docked telemetry with `in_cleaning == 0`. It does not replace a command already awaiting acknowledgement. Pause/Home do not require a healthy water tank; Resume still checks the cleaning mode.

`robot_cleaner_queue.device_control` accepts `vacuum`, an allowlisted `control` and its native `value`. Registry unique IDs and config-entry ownership select the target; callers cannot pass arbitrary entity IDs. Settings/dock actions persist a `mode: device` reservation under the same queue lock, preserve the caller's permissions, and wait for fresh entity readback. Failure, timeout or restart requires review and retains the uncertainty barrier. A rejected concurrent command does not cancel an existing reservation. Read-only `get_capabilities` includes enabled/available, permission-filtered `device_entities` and `control_version: 3`.

Dock starts require a docked idle robot with no unfinished job. Washing requires a healthy dock; emptying/drying may proceed with the specific water-empty warning. Turning an already-running dock action off does not require water. Native robot/dock firmware remains authoritative. `locate` plays a sound and does not adopt or advance any cleaning plan. Map images and maintenance values are read-only.


## Persisted preset — v0.4.0

`save_preset` stores one validated plan per vacuum in the separate version-1 HA store `robot_cleaner_queue_presets`. Writes share the controller lock, and the new plan is published to memory only after durable save. Saving requires control permissions and never writes robot settings. `get_capabilities` exposes the authorized saved plan, current map and control_version 4. `toggle_saved` chooses finish/start atomically; only its idle branch reads the saved plan, then normal start validation and dispatch permissions apply. Manual plans retain map identity and are revalidated against current area mappings/settings. A supplied routine list is used only if no saved plan exists. Storage survives queue clearing and restarts without triggering a run.
