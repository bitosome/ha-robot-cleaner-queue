# Robot Cleaner Queue

A Home Assistant companion integration that runs an **ordered** room-cleaning sequence on a Roborock robot: it sends one routine at a time, verifies the robot's own cleaning record before moving on, and never retries blindly. The sequence lives in Home Assistant, so closing the dashboard or locking the phone does not stop it.

This is the backend half. It is driven by the [Robot Vacuum Cleaner Card](https://github.com/bitosome/robot-vacuum-cleaner-card), which supplies the tiles, presets and manual setup. Without the card you can call the services directly.

## Why a queue exists

Native Roborock area cleaning accepts several areas in one command but does not promise an order, so it cannot implement "Kitchen → Office → Bedroom". This integration submits one room at a time and only advances when the robot reports a **successful cleaning record** for the room just finished. Pauses, recharge breaks and mop washing stay part of the same job and never count as completion.

## Install

### HACS (recommended)

1. HACS → **Custom repositories**.
2. Repository: `https://github.com/bitosome/ha-robot-cleaner-queue`, category: **Integration**.
3. Download it, then restart Home Assistant.

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

Installation and restart send no robot command. Afterwards, `sensor.robot_cleaner_queue`, `script.robot_cleaner_queue_control` and the `robot_cleaner_queue.control` action exist.

## Requirements

- Home Assistant **2026.9.0** or later.
- The native Roborock integration with a V1-protocol robot.
- Room routines created in the Roborock app, one per room, each representing the whole room.

One robot and one queue per Home Assistant instance. Other robot platforms and Roborock protocols are not implicitly supported.

## Services

| Action | Purpose |
| --- | --- |
| `robot_cleaner_queue.control` | `start`, `start_manual`, `pause`, `resume`, `cancel`, `return_to_dock`, `stop`, `toggle`, `toggle_saved` |
| `robot_cleaner_queue.device_control` | Allowlisted dock and robot settings (mop washing, child lock, DND, volume, …) |
| `robot_cleaner_queue.save_preset` | Store one validated plan per vacuum |
| `robot_cleaner_queue.get_capabilities` | Read-only capabilities, manual options, and the zones-and-areas report |

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

Call the action directly rather than through `script.turn_on` so validation errors reach the caller. A successful return means the command was dispatched, not that the robot started: `pending_command` stays set until telemetry acknowledges it.

## Zones and areas

The Roborock app names every room and often splits them finer than Home Assistant areas do. Room cleaning in Home Assistant goes only through `vacuum.clean_area`, so the entity registry's area mapping is what connects the two. `get_capabilities` reports that relationship read-only:

- `robot_maps` — every floor the robot reports.
- `robot_rooms` — each robot room with the app's `name`, its `segment`, its `floor`, and the `area_id`/`area_name` claiming it, or `null` when none does.
- `unmapped_areas` — areas whose mapped rooms the robot no longer reports.
- `rooms_complete` — whether every floor was readable, so a single readable floor never makes another floor's areas look stale.

One area may cover several robot rooms: a `Kitchen` area mapped to two segments cleans both, and the report names them. Nothing in this report writes configuration, creates areas or switches maps.

## Documentation

- [Queue backend: contract, completion rules, recovery](docs/queue-backend.md)
- [Manual cleaning: areas, modes, settings, compatibility](docs/manual-cleaning.md)

## Tests

```sh
python3 -B test/backend_queue_test.py
python3 -B test/backend_manual_test.py
python3 -B test/backend_controls_test.py
python3 -B test/backend_device_test.py
```

These are offline event traces against the production transition code with simulated Home Assistant states. They send no robot commands and do not prove physical operation.

## License

MIT
