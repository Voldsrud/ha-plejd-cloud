"""Tests for the Plejd climate platform."""

from __future__ import annotations

import types

from homeassistant.components.climate import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE
from plejd_cloud.climate import PlejdClimate, async_setup_entry
from plejd_cloud.cloud import PlejdCloudDevice
from plejd_cloud.protocol import ThermostatState


def _device(category="climate", address=9, output_settings=None):
    return PlejdCloudDevice(
        device_id="t1",
        name="Floor",
        address=address,
        output_index=0,
        outputs=[address],
        hardware_id=100,
        model="TRM-01",
        category=category,
        dimmable=False,
        traits=0x20,
        room_id="r1",
        output_settings=output_settings,
    )


class _Coordinator:
    def __init__(self, devices):
        self.devices = devices
        self.setpoints: list = []
        self.modes: list = []
        self.listeners: list = []
        self.available = True
        self.climate: dict = {}

    def climate_state_for(self, address):
        return self.climate.get(address)

    def async_add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)

    async def async_set_climate_setpoint(self, address, celsius):
        self.setpoints.append((address, celsius))

    async def async_set_climate_mode(self, address, mode):
        self.modes.append((address, mode))


async def test_setup_creates_climate_only_for_climate_devices():
    coord = _Coordinator([_device(), _device(category="light"), _device(address=None)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 1


def test_climate_available_follows_coordinator():
    coord = _Coordinator([])
    c = PlejdClimate(coord, _device())
    assert c.available is True
    coord.available = False
    assert c.available is False


def test_attributes():
    c = PlejdClimate(_Coordinator([]), _device())
    assert c._attr_hvac_modes == [HVACMode.HEAT]
    assert c._attr_supported_features == (ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE)
    assert "boost" in c._attr_preset_modes and c._attr_preset_mode == "none"
    assert c._attr_unique_id == "t1" and c._attr_device_info["model"] == "TRM-01"


def test_target_temperature_has_a_default_before_any_setpoint_is_sent():
    # A null target on a fresh entity would disable a UI's +/- controls (target == null
    # reads as "no reading yet"), blocking the very first setpoint change entirely.
    c = PlejdClimate(_Coordinator([]), _device())
    assert c._attr_target_temperature == 21.0


async def test_set_temperature_sends_setpoint():
    coord = _Coordinator([])
    c = PlejdClimate(coord, _device())
    await c.async_set_temperature(**{ATTR_TEMPERATURE: 21.5})
    assert coord.setpoints == [(9, 21.5)]
    assert c._attr_target_temperature == 21.5


async def test_set_temperature_without_value_noops():
    coord = _Coordinator([])
    await PlejdClimate(coord, _device()).async_set_temperature()
    assert coord.setpoints == []


async def test_set_preset_maps_to_mode():
    coord = _Coordinator([])
    c = PlejdClimate(coord, _device())
    await c.async_set_preset_mode("boost")
    assert coord.modes == [(9, 3)]  # OperatingMode.Boost
    assert c._attr_preset_mode == "boost"


async def test_set_unknown_preset_falls_back_to_normal():
    coord = _Coordinator([])
    await PlejdClimate(coord, _device()).async_set_preset_mode("weird")
    assert coord.modes == [(9, 7)]  # OperatingMode.Normal


async def test_set_hvac_mode_noop():
    coord = _Coordinator([])
    await PlejdClimate(coord, _device()).async_set_hvac_mode(HVACMode.HEAT)
    assert coord.setpoints == [] and coord.modes == []


async def test_added_to_hass_subscribes():
    coord = _Coordinator([])
    await PlejdClimate(coord, _device()).async_added_to_hass()
    assert len(coord.listeners) == 1


def test_reads_target_current_mode_and_heating_from_the_report():
    coord = _Coordinator([])
    coord.climate[9] = ThermostatState(mode=3, target=22, current=19, heating=True)
    c = PlejdClimate(coord, _device())
    assert (c._attr_target_temperature, c._attr_current_temperature) == (22, 19)
    assert c._attr_preset_mode == "boost" and c._attr_hvac_action == HVACAction.HEATING


def test_keeps_the_preset_when_the_mode_is_unknown_and_heating_unreported():
    coord = _Coordinator([])
    coord.climate[9] = ThermostatState(mode=None, target=20, current=21, heating=None)
    c = PlejdClimate(coord, _device())
    assert c._attr_preset_mode == "none" and c._attr_current_temperature == 21
    assert getattr(c, "_attr_hvac_action", None) is None


def test_idle_when_not_heating():
    coord = _Coordinator([])
    coord.climate[9] = ThermostatState(mode=7, target=20, current=21, heating=False)
    assert PlejdClimate(coord, _device())._attr_hvac_action == HVACAction.IDLE


def test_update_from_coordinator_refreshes_the_reading():
    coord = _Coordinator([])
    c = PlejdClimate(coord, _device())
    coord.climate[9] = ThermostatState(mode=7, target=23, current=20, heating=None)
    c._handle_update()
    assert (c._attr_target_temperature, c._attr_current_temperature) == (23, 20)


def test_pwm_regulation_ignores_the_temperature_bits():
    coord = _Coordinator([])
    coord.climate[9] = ThermostatState(mode=7, target=40, current=0, heating=None)
    c = PlejdClimate(coord, _device(output_settings={"climateSettings": {"regulationMode": "PWM"}}))
    assert c._attr_target_temperature == 21.0 and getattr(c, "_attr_current_temperature", None) is None


def test_uses_the_user_input_limits_from_the_cloud():
    limits = {"minUserInputTemperature": 15, "maxUserInputTemperature": 28}
    c = PlejdClimate(_Coordinator([]), _device(output_settings={"climateSettings": {"temperatureLimits": limits}}))
    assert (c._attr_min_temp, c._attr_max_temp) == (15, 28)


def test_ignores_invalid_limits():
    limits = {"minUserInputTemperature": 30, "maxUserInputTemperature": 10}
    c = PlejdClimate(_Coordinator([]), _device(output_settings={"climateSettings": {"temperatureLimits": limits}}))
    assert (c._attr_min_temp, c._attr_max_temp) == (5, 35)
