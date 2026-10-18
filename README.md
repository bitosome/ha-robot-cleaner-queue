# Robot Cleaner Queue

A Home Assistant companion integration that runs an **ordered** room-cleaning sequence on a Roborock robot: it cleans the robot's own rooms one at a time, each with its own mode and settings, verifies the robot's own cleaning record before moving on, and never retries blindly. The sequence lives in Home Assistant, so closing the dashboard or locking the phone does not stop it.

This is the backend half. It is driven by the [Robot Vacuum Cleaner Card](https://github.com/bitosome/robot-vacuum-cleaner-card), which supplies the room tiles and the per-room setup. Without the card you can call the services directly.

## Why a queue exists

Native Roborock area cleaning accepts several areas in one command but does not promise an order, so it cannot implement "Kitchen → Office → Bedroom". This integration submits one room at a time and only advances when the robot reports a **successful cleaning record** for the room just finished. Pauses, recharge breaks and mop washing stay part of the same job and never count as completion.

## Install

### HACS (recommended)

1. HACS → **Custom repositories**.
2. Repository: `https://github.com/bitosome/ha-robot-cleaner-queue`, category: **Integration**.
3. Download it, then add the package include from [Manual](#manual) step 2 to your `configuration.yaml`. Home Assistant only loads a YAML integration that appears in its configuration, so without this step the download alone installs nothing and raises no error.
4. Restart Home Assistant.

### Manual

1. Copy `custom_components/robot_cleaner_queue` into `/config/custom_components/`.
2. Copy `packages/robot_cleaner_queue.yaml` into your HA packages directory and include it through `homeassistant.packages`:

   ```yaml
   homeassistant:
     packages:
       robot_cleaner_queue: !include packages/robot_cleaner_queue.yaml
   ```

   Merge with any existing `homeassistant:`/`packages:` section rather than replacing it.
3. Check the configuration, then restart Home Assistant.

Installation starts no cleaning. After a restart, an existing active sequence reconciles its saved progress before continuing. Afterwards, `sensor.robot_cleaner_queue`, `script.robot_cleaner_queue_control` and the `robot_cleaner_queue.control` action exist.

## Requirements

**Required**

- **Home Assistant 2026.9.0 or later**, verified against Core 2026.9.4 with python-roborock 7.4.2.
- **The native Roborock integration** connected to a **V1-protocol** robot. The integration reads that coordinator's cached state only: no credentials, no cloud client, no extra polling and no modification of the native integration. An unfamiliar coordinator shape fails closed instead of guessing.
- **A robot with named rooms on its current map.** Rooms are read from the map the Roborock app maintains, so nothing has to be rebuilt in Home Assistant and app routines are not needed.
- **Mapped Home Assistant areas only for legacy area queues.** Manual cleaning calls `vacuum.clean_area`, so Home Assistant's own area mapping connects areas to robot rooms, and an area is offered only when every Roborock room it maps to exists on the robot's current map. Map areas to rooms in the vacuum entity's settings; `get_capabilities` reports what is mapped, which robot rooms no area covers, and which mapped areas the robot no longer reports.
- **Home Assistant permissions** to control the vacuum and its setting entities. The initiating user is checked at acceptance, before every later dispatch and before any dock or settings change.

**Optional but recommended**

- The [Robot Vacuum Cleaner Card](https://github.com/bitosome/robot-vacuum-cleaner-card) supplies the room tiles, per-room setup and the saved plan. Every capability is also reachable through the services below, so the card is not required.

**Limits**

- One robot and one queue per Home Assistant instance.
- Other robot platforms and Roborock protocols are not implicitly supported.
- A new sequence requires a fresh idle/docked robot with no unfinished job. An interrupted sequence keeps its progress and offers explicit continuation or retry of its unconfirmed pass; it never resumes automatically.

## Services

| Action | Purpose |
| --- | --- |
| `robot_cleaner_queue.control` | `start_manual`, `pause`, `resume`, `cancel`, `return_to_dock`, `stop`, `toggle`, `toggle_saved`, `continue_sequence`, `resume_now` |
| `robot_cleaner_queue.device_control` | Allowlisted dock and robot settings (mop washing, child lock, DND, volume, …) |
| `robot_cleaner_queue.save_preset` | Store one validated plan per vacuum |
| `robot_cleaner_queue.get_capabilities` | Read-only capabilities, manual options, native control discovery and zones-and-areas report |
| `robot_cleaner_queue.get_diagnostics` | Authorized robot’s queue and recent controller events |
| `robot_cleaner_queue.get_history` | Persisted last sequence and up to five previous sequences, with per-pass completion evidence |

```yaml
action: robot_cleaner_queue.control
data:
  command: start_manual
  vacuum: vacuum.robot
  rooms:
    - {id: "0_12", mode: vacuum_mop, suction: max, water: high, route: standard}
    - {id: "0_13", mode: mop, water: low, route: deep}
```

Each room is one entry from `get_capabilities.robot_rooms`; anything a room omits falls back to the request's `setup` defaults. A legacy plan may still pass Home Assistant area ids (`rooms: [kitchen]`) instead, and the whole home is `rooms: []`.

Call the action directly rather than through `script.turn_on` so validation errors reach the caller. A successful return means the command was dispatched, not that the robot started: `pending_command` stays set until telemetry acknowledges it.

## Zones and areas

The Roborock app names every room and often splits them finer than Home Assistant areas do. Rooms are cleaned through the robot's own client, so no Home Assistant area mapping is needed for a room plan. `get_capabilities` reports the relationship read-only:

- `robot_maps` — every floor the robot reports.
- `robot_rooms` — each robot room with the app's `name`, its `segment`, its `floor`, and the `area_id`/`area_name` claiming it, or `null` when none does.
- `unmapped_areas` — areas whose mapped rooms the robot no longer reports.
- `rooms_complete` — whether every floor was readable, so a single readable floor never makes another floor's areas look stale.

One Home Assistant area may cover several robot rooms: a `Kitchen` area mapped to two segments cleans both, and the report names them. A room plan does not depend on that mapping — it names the robot's rooms directly. Nothing in this report writes configuration, creates areas or switches maps.

## Documentation

- [Queue backend: contract, completion rules, recovery](docs/queue-backend.md)
- [Manual cleaning: areas, modes, settings, compatibility](docs/manual-cleaning.md)

## Diagnosing a stopped sequence

Set the `custom_components.robot_cleaner_queue` logger to `debug` for a step-by-step account of every observation, command, deferral and refusal. `robot_cleaner_queue.get_diagnostics` returns the persisted queue, the current robot observation and the last 100 recorded steps, so a stopped sequence can be explained without reading the log.

Every accepted cleaning plan also keeps an independent, shared activity record. Water faults,
pause/resume, cancellation, dock/settings controls and Home Assistant restarts do not erase
the assigned rooms or their verified results. Starting another plan retains the previous
sequence in a bounded history of five previous runs. History never restarts old work.

The record preserves each room's name and settings and every vacuum/mop pass, including
repeats. A successful native cleaning record supplies the actual start/end times; a separate
confirmation time shows when Home Assistant observed it. Older stored queues retain their
completed counts with unknown timestamps. Unstarted passes remain pending, and an interrupted
pass is never presented as successfully cleaned. Details are available from `get_history`;
the sensor publishes small summaries and a revision for frontend updates.

## Tests

```sh
python3 -m pip install voluptuous
python3 -B test/backend_schema_test.py
python3 -B test/backend_queue_test.py
python3 -B test/backend_manual_test.py
python3 -B test/backend_controls_test.py
python3 -B test/backend_device_test.py
python3 -B test/backend_history_test.py
python3 -B test/backend_recovery_test.py
python3 -B test/backend_restart_test.py
```

These are offline event traces against the production transition code with simulated Home Assistant states. They send no robot commands and do not prove physical operation.

The package regression test additionally uses Home Assistant itself (Python 3.14):

```sh
python3 -m pip install -r test/requirements-homeassistant.txt
python3 -B test/backend_package_test.py
```

It validates the wrapper after Home Assistant merges packages, including selector
types and continuation fields. Release CI requires this check. Package-only script
fixes can be applied by replacing the wrapper and reloading scripts; no robot action
or Home Assistant restart is needed.


## License

MIT

## Room-plan updates in 0.8.0

Room plans finish all passes of one room before advancing, preserving the actual room index across repeated and vacuum-then-mop stages. Inherited settings irrelevant to a room's mode are omitted; explicitly contradictory settings are rejected. Rejected starts cannot change the address mode of a running plan.

Temporarily unavailable dock setting entities can defer configuration. Only settings not yet sent resume when those entities return; failed/uncertain calls and physical starts are never automatically repeated. Raw native exceptions are excluded from diagnostics because they may contain private payloads.

Retired saved Roborock routine plans are rejected with instructions to save a new room plan. `toggle_saved` never guesses a whole-house fallback. The service name `save_preset` and existing storage key are retained for compatibility. No routine buttons are pressed.

Device discovery now includes map selection, optional off-peak charging/times, and read-only attachment, tank, drying and cleaning-history sensors. Disabled native entities remain disabled. Map changes are rejected during unfinished jobs. The frontend README contains the supported-feature matrix and explicit Roborock-app-only limitations.

## Integration icon

Home Assistant 2026.3 and later displays the bundled integration icon from
`custom_components/robot_cleaner_queue/brand/`. The editable artwork and export
instructions are in [branding/](branding/README.md). Earlier Home Assistant
versions retain their usual fallback icon.


## Continue after a water fault or charging break

The card keeps the same room sequence and all confirmed passes when a fault stops
queue progression. **Continue sequence** resumes a retained paused room job, or
rejoins observation if that same job is already cleaning. If the native job ended
without confirmed success, **Retry current pass** repeats only that unconfirmed
pass, then continues the remaining sequence. An available matching successful
native record is reconciled before deciding whether a retry is needed.

During an unfinished native charging break, the card shows **Charging to continue** and may
offer **Continue now**. This sends the explicit native segment/zone resume command through HA;
firmware still decides whether the battery is sufficient. It never starts a full
clean in place of the retained room job.

A water-empty warning can remain latched after a refill. Explicitly confirming
**I refilled the clean-water tank** acknowledges only that known warning for the
current interrupted pass. Other faults remain blocking, and the acknowledgement
ends on a healthy dock report, a pass transition, cancellation or HA restart.

## Surviving Home Assistant restarts

The complete sequence, frozen room settings and completed passes survive restart.
Startup first waits for a fresh native robot update and revalidates the saved map,
rooms, controls and initiating user's permissions. A robot already cleaning or
charging is observed without repeating its start command. The queue continues the
remaining passes after the interrupted pass has a matching successful native record.
The record's start must be within the original pre-restart ownership window; later
telemetry and repeated restarts cannot expand that window to include an unrelated job.

Between-pass waiting, final dock care and preparation resume from their checkpoints.
A deliberately paused sequence stays paused. If its retained native room job is
paused or charging, **Resume robot** asks for confirmation and sends only the exact
native room/zone resume command. That confirmation does not waive completion proof
or battery limits. Faults and unconfirmed commands retain their recovery controls;
no uncertain start or resume is repeated. If the robot no longer has a matching
successful record, **Retry current pass** keeps all confirmed passes and retries
only the unconfirmed work when the robot is ready. Archived/cancelled plans never
resume automatically.

After **Stop**, **Return to dock** remains available once the robot confirms that
it is idle and the stop command has completed.
