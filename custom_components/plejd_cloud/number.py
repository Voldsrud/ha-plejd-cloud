"""Plejd number platform — per-output min/max dim level config.

These are device *settings* (the floor/ceiling a dimmer scales to), not live state.
Settings are read back over BLE after connect; entities fall back to restore-state
when the read hasn't arrived yet. The wire encoding (0-1 fraction as u16 little-endian)
is byte-exact, decoded from the app.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import PlejdCloudDevice
from .const import CATEGORY_LIGHT, DOMAIN, PHASE_DIM_HARDWARE, RELAY_HARDWARE
from .coordinator import PlejdCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Plejd dimmer settings (min + max brightness + transition time + relay off time + inrush current)."""
    coordinator: PlejdCoordinator = entry.runtime_data
    entities: list[RestoreNumber] = []
    for device in coordinator.devices:
        if device.address is None:
            continue
        if device.category == CATEGORY_LIGHT and device.dimmable:
            entities.append(PlejdDimLevelNumber(coordinator, device, "min"))
            entities.append(PlejdDimLevelNumber(coordinator, device, "max"))
            entities.append(PlejdDimLevelNumber(coordinator, device, "start"))
            entities.append(PlejdTransitionTimeNumber(coordinator, device))
            if device.hardware_id in PHASE_DIM_HARDWARE:
                entities.append(PlejdInrushCurrentNumber(coordinator, device))
        if device.hardware_id in RELAY_HARDWARE:
            entities.append(PlejdRelayOffTimeNumber(coordinator, device))
    async_add_entities(entities)


class PlejdDimLevelNumber(RestoreNumber):
    """A dimmer's minimum, maximum, or start brightness, as a 0-100% setting."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: PlejdCoordinator, device: PlejdCloudDevice, kind: str) -> None:
        self._coordinator = coordinator
        self._device = device
        setters: dict[str, Callable[[int, int, float], Awaitable[None]]] = {
            "min": coordinator.async_set_output_min_level,
            "max": coordinator.async_set_output_max_level,
            "start": coordinator.async_set_output_start_level,
        }
        self._setter = setters[kind]
        self._kind = kind
        self._attr_translation_key = {"min": "min_dim_level", "max": "max_dim_level", "start": "start_level"}[kind]
        base = device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
        self._attr_unique_id = f"{base}_{kind}_level"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Plejd",
            model=device.model,
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._setter(self._device.address, self._device.output_index, value / 100)
        self._attr_native_value = value
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Subscribe before the restore await: a read reply arriving during it must not be
        # missed, and must win over the (possibly stale) restored value once it lands.
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_coordinator_update))
        settings = self._coordinator.settings_for(self._device.address)
        val = getattr(settings, f"{self._kind}_level", None) if settings else None
        if val is None:
            last = await self.async_get_last_number_data()
            if last is not None and last.native_value is not None:
                self._attr_native_value = last.native_value
            settings = self._coordinator.settings_for(self._device.address)
            val = getattr(settings, f"{self._kind}_level", None) if settings else None
        if val is not None:
            self._attr_native_value = val

    @callback
    def _handle_coordinator_update(self) -> None:
        settings = self._coordinator.settings_for(self._device.address)
        if settings is None:
            return
        val = getattr(settings, f"{self._kind}_level", None)
        if val is not None and val != getattr(self, "_attr_native_value", None):
            self._attr_native_value = val
            self.async_write_ha_state()


class PlejdTransitionTimeNumber(RestoreNumber):
    """A dimmer's fade/transition time in seconds (0 = instant)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "transition_time"
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: PlejdCoordinator, device: PlejdCloudDevice) -> None:
        self._coordinator = coordinator
        self._device = device
        base = device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
        self._attr_unique_id = f"{base}_transition_time"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Plejd",
            model=device.model,
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_output_speed(self._device.address, self._device.output_index, value)
        self._attr_native_value = value
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_coordinator_update))
        settings = self._coordinator.settings_for(self._device.address)
        if settings is None or settings.speed is None:
            last = await self.async_get_last_number_data()
            if last is not None and last.native_value is not None:
                self._attr_native_value = last.native_value
            settings = self._coordinator.settings_for(self._device.address)
        if settings is not None and settings.speed is not None:
            self._attr_native_value = settings.speed

    @callback
    def _handle_coordinator_update(self) -> None:
        settings = self._coordinator.settings_for(self._device.address)
        if settings is None:
            return
        if settings.speed is not None and settings.speed != getattr(self, "_attr_native_value", None):
            self._attr_native_value = settings.speed
            self.async_write_ha_state()


class PlejdRelayOffTimeNumber(RestoreNumber):
    """Minimum relay off time (relay protection) in seconds for a Plejd relay output."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "relay_off_time"
    _attr_native_min_value = 0.1
    _attr_native_max_value = 10.0
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: PlejdCoordinator, device: PlejdCloudDevice) -> None:
        self._coordinator = coordinator
        self._device = device
        base = device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
        self._attr_unique_id = f"{base}_relay_off_time"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Plejd",
            model=device.model,
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_output_relay_off_time(self._device.address, self._device.output_index, value)
        self._attr_native_value = value
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_coordinator_update))
        settings = self._coordinator.settings_for(self._device.address)
        if settings is None or settings.relay_off_time is None:
            last = await self.async_get_last_number_data()
            if last is not None and last.native_value is not None:
                self._attr_native_value = last.native_value
            settings = self._coordinator.settings_for(self._device.address)
        if settings is not None and settings.relay_off_time is not None:
            self._attr_native_value = settings.relay_off_time

    @callback
    def _handle_coordinator_update(self) -> None:
        settings = self._coordinator.settings_for(self._device.address)
        if settings is None:
            return
        if settings.relay_off_time is not None and settings.relay_off_time != getattr(self, "_attr_native_value", None):
            self._attr_native_value = settings.relay_off_time
            self.async_write_ha_state()


class PlejdInrushCurrentNumber(RestoreNumber):
    """Inrush current protection time in milliseconds for a phase-cut dimmer (0=disabled)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "inrush_current_time"
    _attr_native_min_value = 0
    _attr_native_max_value = 5000
    _attr_native_step = 100
    _attr_native_unit_of_measurement = "ms"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: PlejdCoordinator, device: PlejdCloudDevice) -> None:
        self._coordinator = coordinator
        self._device = device
        base = device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
        self._attr_unique_id = f"{base}_inrush_current_time"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Plejd",
            model=device.model,
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_output_inrush_current(
            self._device.address, self._device.output_index, int(value)
        )
        self._attr_native_value = value
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_coordinator_update))
        settings = self._coordinator.settings_for(self._device.address)
        if settings is None or settings.inrush_current_ms is None:
            last = await self.async_get_last_number_data()
            if last is not None and last.native_value is not None:
                self._attr_native_value = last.native_value
            settings = self._coordinator.settings_for(self._device.address)
        if settings is not None and settings.inrush_current_ms is not None:
            self._attr_native_value = settings.inrush_current_ms

    @callback
    def _handle_coordinator_update(self) -> None:
        settings = self._coordinator.settings_for(self._device.address)
        if settings is None:
            return
        if settings.inrush_current_ms is not None and settings.inrush_current_ms != getattr(
            self, "_attr_native_value", None
        ):
            self._attr_native_value = settings.inrush_current_ms
            self.async_write_ha_state()
