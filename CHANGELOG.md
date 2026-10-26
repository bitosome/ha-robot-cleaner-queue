# Changelog

## 0.17.0

- Expose native dock activity and action-specific availability, including manual stop during automatic dock care.
- Preserve run history and conditional empty-bin / wash-mop obligations after actual floor work, cancellation, docking and Home Assistant restarts.
- Adopt verified native care; dispatch only missing care after settled docking. Require observed start and completion, never merely service acceptance.
- Keep incomplete or interrupted care visible and provide an explicit run/revision-guarded Resume dock care action.
- Preserve room plans through manual device settings, and prevent stale device reservations from trapping later queue ticks.
- Add offline lifecycle, permission, persistence, fault, concurrency and restart regressions to CI and release validation.
