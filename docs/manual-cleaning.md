# Manual cleaning

Manual setup is separate from saved Roborock app routines. Preset mode keeps the settings stored in the Roborock app. Manual cleaning uses the native Home Assistant vacuum and select actions; it never presses a preset button.

For example, if the Bedroom preset in the Roborock app is Vacuum then mop, **Preset → Bedroom** runs that routine. **Manual setup → Vacuum → Bedroom** vacuums the mapped Bedroom area using the settings chosen in Home Assistant. It does not invoke or edit the Bedroom routine in the app.

The companion discovers the modes and values actually exposed by the selected native Roborock V1 robot. Supported choices can include **Vacuum**, **Vacuum & mop**, **Mop**, and **Vacuum then mop**, with suction, water flow, route, and one or two cleaning runs. Unsupported or unavailable options are omitted. SmartPlan, custom room programs and app-only numeric water controls are not imitated.

## Areas and sequence

Manual tiles come from the vacuum's existing Home Assistant **Cleaning by area** mapping. They are not the saved preset tiles, and the two lists need not match. A Home Assistant area can contain several mapped Roborock rooms; those rooms remain one tile and are submitted together. Only areas whose entire mapping exists on the robot's current map are offered, and only when the entity advertises area cleaning: without that feature a plan can only clean the whole home. Settings are validated against the options the native select entities themselves expose, because those are the strings `select_option` accepts; a native trait that labels a setting by value or by display name is accepted either way. The companion does not switch maps or discover maps during capability lookup.

Select tiles in the desired sequence, or leave all areas unselected and press **Clean all rooms**. The queue starts one area at a time using `vacuum.clean_area`, retaining Home Assistant's existing area mapping. Empty areas use `vacuum.start` for the whole current map. The robot chooses the internal order of multiple segments grouped in one HA area; the queue guarantees order between the selected HA areas.

**Vacuum then mop** is two explicit, server-owned passes: vacuum all selected areas in selection order, then mop those areas in the same order. The second pass requires successful completion records for the first pass. It is not an assumed native Roborock preset or an unsupported raw command. Repeats are separate completed jobs per area; `2×` can involve docking or mop service between runs, unlike a robot-native in-job repeat. For whole-home cleaning, each repeat is another full job.

For example, two areas and two repeats with Vacuum then mop produce: kitchen vacuum twice, office vacuum twice, kitchen mop twice, office mop twice. Area sequence numbers stay stable across passes.

## Capability response

`robot_cleaner_queue.get_capabilities` requires a `vacuum` and returns a service response. It reads the existing native cache only and performs no robot action or refresh. It requires control permission on the vacuum and the native settings entities.

```yaml
action: robot_cleaner_queue.get_capabilities
data:
  vacuum: vacuum.robot
response_variable: capabilities
```

The response contains `supported`, `area_cleaning` (whether the vacuum entity advertises the `CLEAN_AREA` feature that `vacuum.clean_area` needs), `modes` (`value` and `label`), option arrays `suction`, `water`, `routes`, `routes_by_mode`, `repeats`, `room_targets` (`id`, `name`, optional area `icon`), and `defaults`. An unavailable response includes `error`.

It also returns a read-only zone report, because the Roborock app names every room while Home Assistant areas group them into real rooms:

- `robot_maps`: `flag` and map `name` for every floor the robot reports.
- `robot_rooms`: `id` (`map_segment`), `segment`, the app's `name` when it has one, `floor`, and the `area_id`/`area_name` that claims it, or `null` when no Home Assistant area does.
- `unmapped_areas`: areas whose mapped rooms the robot no longer reports, with their `segments`.
- `rooms_complete`: whether every floor was readable. When it is false only the current floor was seen, so `unmapped_areas` stays empty rather than calling the other floor's areas stale.

The report never writes configuration, never creates areas and never switches maps. One area may cover several robot rooms: a `Kitchen` area mapped to `0_12` and `0_13` cleans both, and the report shows which app rooms those are. Rooms the app names but no area claims are listed so they can be mapped deliberately in Home Assistant rather than silently disappearing from manual tiles. Defaults use an exposed current value where safe, otherwise an available balanced/medium/standard option.

The manual form excludes off, SmartPlan, custom room programs, and remembered custom water flow from fine controls. In particular, an exposed `custom_water_flow` selector does not reveal the numeric setting that the Roborock app remembers; the card cannot truthfully display or edit that number. Vacuum & mop exposes standard/fast routes when offered; deep routes are limited to the mop phase. Route is omitted for vacuum-only cleaning.

## Start action

```yaml
action: robot_cleaner_queue.control
data:
  command: start_manual
  vacuum: vacuum.robot
  rooms: [kitchen, office]
  setup:
    mode: vacuum_then_mop
    suction: max
    water: high
    route: standard
    repeat: 1
```

`rooms: []` means whole home. Mode values are `vacuum`, `vacuum_mop`, `mop` or `vacuum_then_mop`, only when returned by capabilities. Omit suction for mop-only; omit water and route for vacuum-only. The backend rejects unsupported values, irrelevant settings, duplicate/overlapping areas, areas on another map, and more than 32 areas. At most 128 stages can be generated.

The optional script wrapper accepts the same `rooms` and `setup` data; direct service calls return validation failures to the card. The ordinary `pause`, `resume`, `cancel`, and `return_to_dock` commands apply to both preset and manual queues. Cancellation clears future work and does not claim to stop an active robot operation.

## Execution and compatibility

The native high-level mode resets its detailed motor settings. Each stage therefore sets the native mode first, then suction, water and route in that order. The queue stays `preparing` until a successful native coordinator update after configuration began confirms all requested values. Setting-service success alone never starts cleaning. A busy robot, configuration failure, unavailable telemetry or a 60-second confirmation timeout leaves the queue requiring attention.

Before every later stage, the companion checks capabilities, selected map, original area-to-room mapping, native setting entity identities, and the initiating user's current control permissions. It persists each transition before issuing commands. External Home Assistant vacuum, routine, setting and map controls interrupt the queue. App changes cannot always be attributed to their caller; the completion-record and map/readback checks still apply. Restart never resumes an interrupted plan automatically. Settings are left at the most recently applied values; the companion does not silently restore old settings after completion or cancellation.

Reviewed compatibility: Home Assistant Core **2026.9.4**, python-roborock **7.4.2**. Exact native behavior is based on [HA area cleaning](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/vacuum/__init__.py), [Roborock area implementation](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/roborock/vacuum.py), [native setting selectors](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/roborock/select.py), [Roborock cleaning modes](https://github.com/Python-roborock/python-roborock/blob/v7.4.2/roborock/data/v1/v1_clean_modes.py), and [coordinator freshness](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/roborock/coordinator.py). Other protocols and model capabilities fail closed. Automated tests use simulated states and services; physical cleaning has not been validated by these tests.
