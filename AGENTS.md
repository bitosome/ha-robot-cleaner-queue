# Robot Cleaner Queue

This repository owns the Home Assistant **companion integration** for the Robot Vacuum
Cleaner Card. The card itself, its design system and its frontend tests live in
[robot-vacuum-cleaner-card](https://github.com/bitosome/robot-vacuum-cleaner-card);
do not duplicate frontend code or design tokens here.

Queue execution belongs in Home Assistant, never in browser timers. Commands, cleaning
acknowledgement and completion are separate facts. Preserve Roborock routine settings by
invoking configured preset button entities. Fail closed on errors, lost state or
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
```

Source changes alone do not authorize production installation or an actual cleaning run.
