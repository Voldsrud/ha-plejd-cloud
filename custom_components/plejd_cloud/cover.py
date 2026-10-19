"""Plejd cover platform (JAL/MTR blinds).

Decoded from the app but **not hardware-validated** (no cover on the test mesh).
The position command is byte-exact; HA can't read the live position back yet, so
the entity uses assumed state.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import ATTR_POSITION, CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import PlejdCloudDevice
from .const import CATEGORY_COVER, DOMAIN
from .coordinator import PlejdCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Plejd covers for the config entry."""
    coordinator: PlejdCoordinator = entry.runtime_data
    async_add_entities(
        PlejdCover(coordinator, device)
        for device in coordinator.devices
        if device.category == CATEGORY_COVER and device.address is not None
    )


class PlejdCover(CoverEntity):
    """A Plejd JAL/MTR motor exposed as a cover."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_assumed_state = True
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: PlejdCoordinator, device: PlejdCloudDevice) -> None:
        self._coordinator = coordinator
        self._device = device
        self._attr_unique_id = (
            device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Plejd",
            model=device.model,
        )

    @property
    def is_closed(self) -> bool | None:
        # No reliable position read-back yet; assumed_state covers this.
        return None

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_cover_position(self._device.address, 100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_cover_position(self._device.address, 0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_cover_position(self._device.address, kwargs[ATTR_POSITION])

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._coordinator.async_cover_stop(self._device.address)
