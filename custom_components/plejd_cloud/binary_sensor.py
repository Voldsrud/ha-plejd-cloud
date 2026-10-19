"""Plejd binary_sensor platform (WMS-01 motion)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .cloud import PlejdCloudMotion
from .const import DOMAIN, HARDWARE_GWY_01, HARDWARE_TYPES, HARDWARE_WMS_01
from .coordinator import PlejdCoordinator
from .protocol import MotionEvent

MOTION_OFF_DELAY = 60  # seconds with no broadcast before clearing motion


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Plejd motion sensors and per-device health (problem) sensors."""
    coordinator: PlejdCoordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [PlejdMotionBinarySensor(coordinator, sensor) for sensor in coordinator.motion]
    # Fault entities target the physical device's own mesh address (where NotifyEvents
    # is polled/stored) — not an output's address, and covering devices with no output
    # at all (motion sensors, gateways), not just coordinator.devices.
    seen: set[str] = set()
    for device in coordinator.devices:
        if device.device_id in seen:
            continue
        address = coordinator.device_address_for(device.device_id)
        if address is not None:
            seen.add(device.device_id)
            entities.append(PlejdProblemBinarySensor(coordinator, device.device_id, address, device.name, device.model))
    for sensor in coordinator.motion:
        if sensor.device_id not in seen:
            seen.add(sensor.device_id)
            entities.append(
                PlejdProblemBinarySensor(
                    coordinator, sensor.device_id, sensor.address, sensor.name, HARDWARE_TYPES[HARDWARE_WMS_01]
                )
            )
    for button in coordinator.inputs:
        if button.device_id in seen:
            continue
        address = coordinator.device_address_for(button.device_id)
        if address is not None:
            seen.add(button.device_id)
            entities.append(PlejdProblemBinarySensor(coordinator, button.device_id, address, button.name, None))
    for gateway_id in coordinator.gateways:
        if gateway_id in seen:
            continue
        address = coordinator.device_address_for(gateway_id)
        if address is not None:
            seen.add(gateway_id)
            entities.append(
                PlejdProblemBinarySensor(coordinator, gateway_id, address, "Gateway", HARDWARE_TYPES[HARDWARE_GWY_01])
            )
    async_add_entities(entities)


class PlejdProblemBinarySensor(BinarySensorEntity):
    """A device-health sensor: on when the device reports any NotifyEvents fault."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "device_fault"

    def __init__(
        self, coordinator: PlejdCoordinator, device_id: str, address: int, name: str, model: str | None
    ) -> None:
        self._coordinator = coordinator
        self._address = address
        self._attr_unique_id = f"fault_{device_id}"
        info: dict[str, object] = {"identifiers": {(DOMAIN, device_id)}, "name": name, "manufacturer": "Plejd"}
        if model is not None:
            info["model"] = model
        self._attr_device_info = DeviceInfo(**info)

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.faults_for(self._address))

    @property
    def extra_state_attributes(self) -> dict[str, list[str]]:
        return {"active_faults": sorted(self._coordinator.faults_for(self._address))}

    @callback
    def _handle(self, address: int, _faults: frozenset[str]) -> None:
        if address == self._address:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_fault_listener(self._handle))


class PlejdMotionBinarySensor(BinarySensorEntity):
    """A WMS-01 motion sensor (momentary; auto-clears after a delay)."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_is_on = False

    def __init__(self, coordinator: PlejdCoordinator, sensor: PlejdCloudMotion) -> None:
        self._coordinator = coordinator
        self._sensor = sensor
        self._attr_unique_id = f"motion_{sensor.device_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sensor.device_id)},
            name=sensor.name,
            manufacturer="Plejd",
            model="WMS-01",
        )
        self._cancel = None

    @callback
    def _handle(self, event: MotionEvent) -> None:
        if event.address != self._sensor.address or not event.motion:
            return
        self._attr_is_on = True
        if self._cancel is not None:
            self._cancel()
        self._cancel = async_call_later(self.hass, MOTION_OFF_DELAY, self._clear)
        self.async_write_ha_state()

    @callback
    def _clear(self, _now: object) -> None:
        self._attr_is_on = False
        self._cancel = None
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_motion_listener(self._handle))
