"""Frontend-readable state of the single server-owned robot queue."""
from homeassistant.components.sensor import SensorEntity
from . import DOMAIN


async def async_setup_platform(hass, _config, async_add_entities, discovery_info=None):
    async_add_entities([QueueSensor(hass.data[DOMAIN])])


class QueueSensor(SensorEntity):
    _attr_name = "Robot cleaner queue"
    _attr_unique_id = "robot_cleaner_queue"
    _attr_icon = "mdi:format-list-numbered"
    _attr_should_poll = False

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
            "control_version": 4,
            "vacuum": queue.vacuum,
            "presets": list(queue.presets),
            "mode": queue.mode,
            "targets": list(queue.targets),
            "setup": dict(queue.setup),
            "stages": [{k: stage[k] for k in ("target", "mode", "room_index", "pass_index", "repeat_index")} for stage in queue.stages],
            "current_index": queue.current_index,
            "completed": queue.completed,
            "error": queue.error,
            "pending_command": queue.pending_command,
            "run_id": queue.run_id,
            "waiting_for_dock": queue.next_pending,
            "command_barrier_until": queue.not_before or None,
        }
