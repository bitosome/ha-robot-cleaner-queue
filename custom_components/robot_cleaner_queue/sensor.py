"""Frontend-readable state of the single server-owned robot queue."""
from homeassistant.components.sensor import SensorEntity
from . import DOMAIN
from . import history


async def async_setup_platform(hass, _config, async_add_entities, discovery_info=None):
    async_add_entities([QueueSensor(hass.data[DOMAIN])])


class QueueSensor(SensorEntity):
    _attr_name = "Robot cleaner queue"
    _attr_unique_id = "robot_cleaner_queue"
    _attr_icon = "mdi:format-list-numbered"
    _attr_should_poll = False
    # The durable queue Store owns these large, frozen plan details. Keep them in
    # live state for existing cards without exceeding Recorder's 16 KiB limit.
    _unrecorded_attributes = frozenset({"setup", "stages"})

    def __init__(self, manager):
        self.manager = manager

    async def async_added_to_hass(self):
        self.async_on_remove(self.manager.subscribe(self.async_write_ha_state))

    @property
    def native_value(self):
        return self.manager.queue.phase

    @property
    def extra_state_attributes(self):
        queue = self.manager.queue
        return {
            "control_version": 5,
            "execution_version": 3,
            "run_history_version": history.VERSION,
            "history_revision": queue.history_revision,
            "last_run": history.summary(queue.last_run),
            "previous_runs": [history.summary(run) for run in queue.previous_runs],
            "floor_cleaning_complete": bool(queue.stages) and queue.completed == len(queue.stages),
            "start_uncertain": queue.start_uncertain,
            "command_failure": dict(queue.command_failure),
            **self.manager.robot_state,
            "preferences_revisions": {key: value["revision"] for key, value in self.manager.preferences.items()},
            "saved_plan_revisions": {key: value.get("revision", 0) for key, value in self.manager.saved_presets.items()},
            "vacuum": queue.vacuum,
            "mode": queue.mode,
            "targets": list(queue.targets),
            "setup": dict(queue.setup),
            "stages": [{k: stage.get(k) for k in ("target", "mode", "room_index", "pass_index", "repeat_index")}
                       for stage in queue.stages if isinstance(stage, dict)],
            "current_index": queue.current_index,
            "completed": queue.completed,
            "error": queue.error,
            "pending_command": queue.pending_command,
            "run_id": queue.run_id,
            "waiting_for_dock": queue.next_pending,
            "command_barrier_until": queue.not_before or None,
        }
