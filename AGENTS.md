# Robot Cleaner Queue

<!-- workspace-pointer -->
## Shared workspace rules

Part of the bitosome Home Assistant workspace. Shared rules, access (SSH, ha-mcp) and processes
are in `../AGENTS.md` (the workspace entry point) and `../home-assistant-knowledge/`; they take
precedence over this file. Build, test, release and known gotchas for this repo:
[repo catalog](../home-assistant-knowledge/docs/repo-catalog.md#ha-robot-cleaner-queue).

- Edit and release from this checkout; never patch production directly.
- Push, tag/release, HACS install, production changes, reloads and restarts need the owner's explicit approval.
- After a release or install, update `docs/current-state.md` and `docs/changelog.md` in the knowledge repo.

This repository owns the Home Assistant **companion integration** for the Robot Vacuum
Cleaner Card. The card itself, its design system and its frontend tests live in
[robot-vacuum-cleaner-card](https://github.com/bitosome/robot-vacuum-cleaner-card);
do not duplicate frontend code or design tokens here.

Queue execution belongs in Home Assistant, never in browser timers. Commands, cleaning
acknowledgement and completion are separate facts. Use native rooms/areas and explicit per-room settings; never reintroduce Roborock routine-button execution. Fail closed on errors, lost state or
uncertain completion.

Keep physical robot actions out of tests: use mock Home Assistant state and service
fixtures, and never require a real robot or network access. The remote is public — never
commit production dashboards, household identifiers, maps, credentials, raw
registry/config entries or API dumps. Examples use generic entities.

Run every backend test before publishing:

```sh
python3 -B test/backend_queue_test.py
python3 -B test/backend_manual_test.py
python3 -B test/backend_controls_test.py
python3 -B test/backend_device_test.py
python3 -B test/backend_dock_test.py
python3 -B test/backend_history_test.py
python3 -B test/backend_recovery_test.py
python3 -B test/backend_restart_test.py
python3 -B test/backend_editing_test.py
```

Source changes alone do not authorize production installation or an actual cleaning run.

Validate package changes with actual Home Assistant package merging and script validation:
`python3 -B test/backend_package_test.py` and `python3 -B test/backend_storage_test.py` (Python 3.14; dependencies in
`test/requirements-homeassistant.txt`). The shipped package only enables the integration; do not add script wrappers, compatibility automations or input helpers. Verify the queue sensor, service and shared-plan button after reload.

## Publishing

Commit and push every change. A change a user can see - card behaviour, integration
code, entity or service contracts - also gets a version bump and an annotated
`v<version>` tag, because HACS installs from releases: do not wait to be asked for a
release. Docs-only changes are pushed without a release. After pushing a tag, confirm
the release workflow succeeded and that the asset is attached.
