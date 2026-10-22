"""Hardware-friendly controls for durable shared cleaning plans."""
from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from . import DOMAIN


async def async_setup_platform(hass, _config, async_add_entities, discovery_info=None):
    manager = hass.data[DOMAIN]
    known = set()

    @callback
    def discover():
        entities = []
        for vacuum in manager.saved_presets:
            if vacuum not in known:
                known.add(vacuum)
                entities.append(SharedPlanButton(manager, vacuum))
        if entities:
            async_add_entities(entities)

    manager.subscribe(discover)
    discover()


class SharedPlanButton(ButtonEntity):
    """Use the same atomic start/cancel-and-dock decision as the card service."""
    _attr_icon = "mdi:robot-vacuum"
    _attr_should_poll = False

    def __init__(self, manager, vacuum):
        self.manager = manager
        self.vacuum = vacuum
        self._attr_unique_id = f"{DOMAIN}_{vacuum}_shared_plan"
        state = manager.hass.states.get(vacuum)
        name = state.attributes.get("friendly_name", vacuum.split(".", 1)[-1]) if state else vacuum.split(".", 1)[-1]
        self._attr_name = f"{name} cleaning plan"

    async def async_added_to_hass(self):
        self.async_on_remove(self.manager.subscribe(self.async_write_ha_state))

    @property
    def extra_state_attributes(self):
        return {"vacuum": self.vacuum, "action": "Run shared plan; press again to cancel and dock"}

    async def async_press(self):
        # Keep service validation, caller permissions, queue locking and recovery
        # semantics in the single controller. Never implement a second toggle here.
        await self.hass.services.async_call(
            DOMAIN, "control", {"command": "toggle_saved", "vacuum": self.vacuum},
            blocking=True, context=self._context,
        )
