"""Plejd event platform (wall-switch button presses).

Validated live: a button broadcasts opcode 0x0097 on its input address with
data=[01 pressed / 00 released].
"""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import PlejdCloudInput
from .const import DOMAIN
from .coordinator import PlejdCoordinator

EVENT_PRESS = "press"
EVENT_RELEASE = "release"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Plejd button events for the config entry."""
    coordinator: PlejdCoordinator = entry.runtime_data
    async_add_entities(PlejdButtonEvent(coordinator, button) for button in coordinator.inputs)


class PlejdButtonEvent(EventEntity):
    """A Plejd wall-switch input exposed as an event entity."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_event_types = [EVENT_PRESS, EVENT_RELEASE]

    def __init__(self, coordinator: PlejdCoordinator, button: PlejdCloudInput) -> None:
        self._coordinator = coordinator
        self._button = button
        self._attr_unique_id = f"button_{button.device_id}_{button.address}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, button.device_id)},
            name=button.name,
            manufacturer="Plejd",
        )

    @callback
    def _handle(self, address: int, pressed: bool) -> None:
        if address == self._button.address:
            self._trigger_event(EVENT_PRESS if pressed else EVENT_RELEASE)
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_button_listener(self._handle))
