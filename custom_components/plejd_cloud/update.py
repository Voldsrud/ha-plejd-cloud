"""Plejd update platform — surfaces device firmware status (read-only).

This entity reports the installed firmware and whether the Plejd cloud offers a
newer build, so Home Assistant shows an "update available" badge and notification.
It deliberately does NOT expose an install action: flashing a Plejd device is a
bricking risk and is best done from the Plejd app, which validates compatibility.
"""

from __future__ import annotations

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, HARDWARE_GWY_01, HARDWARE_TYPES, HARDWARE_WMS_01
from .coordinator import PlejdCoordinator

UPDATE_WARNING = (
    "Update firmware from the official Plejd app. The app verifies device "
    "compatibility before flashing; updating outside it risks bricking the device. "
    "Home Assistant shows the available version here but does not install it."
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up one firmware-status entity per physical Plejd device (outputs + sensors)."""
    coordinator: PlejdCoordinator = entry.runtime_data
    seen: set[str] = set()
    entities: list[PlejdFirmwareUpdate] = []
    for device in coordinator.devices:
        if device.device_id not in seen:
            seen.add(device.device_id)
            entities.append(PlejdFirmwareUpdate(coordinator, device.device_id, device.name, device.model))
    for sensor in coordinator.motion:
        if sensor.device_id not in seen:
            seen.add(sensor.device_id)
            entities.append(
                PlejdFirmwareUpdate(coordinator, sensor.device_id, sensor.name, HARDWARE_TYPES[HARDWARE_WMS_01])
            )
    for gateway_id in coordinator.gateways:
        if gateway_id not in seen:
            seen.add(gateway_id)
            entities.append(PlejdFirmwareUpdate(coordinator, gateway_id, "Gateway", HARDWARE_TYPES[HARDWARE_GWY_01]))
    async_add_entities(entities)


class PlejdFirmwareUpdate(UpdateEntity):
    """Reports a Plejd device's installed firmware and the latest the cloud offers."""

    _attr_has_entity_name = True
    _attr_translation_key = "firmware"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_release_summary = UPDATE_WARNING

    def __init__(self, coordinator: PlejdCoordinator, device_id: str, name: str, model: str) -> None:
        self._coordinator = coordinator
        self._device_id = device_id
        self._attr_unique_id = f"firmware_{device_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=name,
            manufacturer="Plejd",
            model=model,
        )

    @property
    def installed_version(self) -> str | None:
        status = self._coordinator.firmware.get(self._device_id)
        return status.installed_version if status else None

    @property
    def latest_version(self) -> str | None:
        # Only diverge from installed when the cloud genuinely has a newer build, so
        # the "update available" state tracks the buildTime, not version-string quirks.
        status = self._coordinator.firmware.get(self._device_id)
        if status is None:
            return None
        if status.update_available:
            return status.latest_version
        return status.installed_version

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self.async_write_ha_state))
