"""Plejd climate platform (TRM thermostats).

Setpoint/mode commands are byte-exact. Target, current temperature, mode and heating
are read back from the output-state report (see protocol.decode_thermostat_state),
the same bit layout live TRM-01 thermostats report correctly over BLE.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import PlejdCloudDevice
from .const import CATEGORY_CLIMATE, DOMAIN, TRM_MODE_NORMAL, TRM_PRESETS
from .coordinator import PlejdCoordinator

_PRESET_TO_MODE = {name: mode for mode, name in TRM_PRESETS.items()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Plejd thermostats for the config entry."""
    coordinator: PlejdCoordinator = entry.runtime_data
    async_add_entities(
        PlejdClimate(coordinator, device)
        for device in coordinator.devices
        if device.category == CATEGORY_CLIMATE and device.address is not None
    )


class PlejdClimate(ClimateEntity):
    """A Plejd TRM thermostat."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    _attr_preset_modes = list(TRM_PRESETS.values())
    _attr_target_temperature_step = 1
    _attr_min_temp = 5
    _attr_max_temp = 35

    def __init__(self, coordinator: PlejdCoordinator, device: PlejdCloudDevice) -> None:
        self._coordinator = coordinator
        self._device = device
        self._attr_unique_id = device.device_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Plejd",
            model=device.model,
        )
        self._attr_preset_mode = TRM_PRESETS[TRM_MODE_NORMAL]
        # The real setpoint isn't readable without a capture (see module docstring), but
        # leaving this unset until the first successful async_set_temperature makes the
        # entity report no target at all - callers (including our own panel) can't tell
        # "no reading yet" from "off", and a UI that disables its controls on a null target
        # can never send the very first setpoint. A reasonable room-temperature default
        # is a starting point to adjust from, not a claim about the device's real state.
        self._attr_target_temperature = 21.0
        climate_settings = (device.output_settings or {}).get("climateSettings") or {}
        # PWM regulation reports a duty cycle in the same bits, not temperatures.
        self._is_pwm = climate_settings.get("regulationMode") == "PWM"
        limits = climate_settings.get("temperatureLimits") or {}
        low, high = limits.get("minUserInputTemperature"), limits.get("maxUserInputTemperature")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low < high:
            self._attr_min_temp, self._attr_max_temp = low, high
        self._apply_state()

    @property
    def available(self) -> bool:
        return self._coordinator.available

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None:
            await self._coordinator.async_set_climate_setpoint(self._device.address, temperature)
            self._attr_target_temperature = temperature

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        mode = _PRESET_TO_MODE.get(preset_mode, TRM_MODE_NORMAL)
        await self._coordinator.async_set_climate_mode(self._device.address, mode)
        self._attr_preset_mode = preset_mode

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        # Plejd thermostats only heat; the mode is fixed.
        return

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self._apply_state()
        self.async_write_ha_state()

    def _apply_state(self) -> None:
        if self._is_pwm or self._device.address is None:
            return
        state = self._coordinator.climate_state_for(self._device.address)
        if state is None:
            return
        self._attr_current_temperature = state.current
        self._attr_target_temperature = state.target
        if state.mode is not None and state.mode in TRM_PRESETS:
            self._attr_preset_mode = TRM_PRESETS[state.mode]
        if state.heating is not None:
            self._attr_hvac_action = HVACAction.HEATING if state.heating else HVACAction.IDLE
