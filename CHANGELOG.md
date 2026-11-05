# Changelog

## 0.18.0

- Add the `skip_dock_cleanup` control command. The owner can release owed bin emptying or mop washing, bound to the displayed `run_id` and `cleanup_revision`. It sends no robot or dock command, does not stop a native dock cycle and never marks care as done: the checkpoint becomes `skipped` with its actions left unconfirmed, the floor outcome (cancelled or completed) is kept, and new cleaning is allowed again. It waits only while a previous command still awaits confirmation.
- `get_capabilities` reports `dock_cleanup_skip: true` so the card shows the action only with a companion that supports it.

## 0.17.0

- Expose native dock activity and action-specific availability, including manual stop during automatic dock care.
- Preserve run history and conditional empty-bin / wash-mop obligations after actual floor work, cancellation, docking and Home Assistant restarts.
- Adopt verified native care; dispatch only missing care after settled docking. Require observed start and completion, never merely service acceptance.
- Keep incomplete or interrupted care visible and provide an explicit run/revision-guarded Resume dock care action.
- Preserve room plans through manual device settings, and prevent stale device reservations from trapping later queue ticks.
- Add offline lifecycle, permission, persistence, fault, concurrency and restart regressions to CI and release validation.
