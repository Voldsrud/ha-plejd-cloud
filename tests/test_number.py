"""Tests for the Plejd number platform (min/max dim level settings + relay off time)."""

from __future__ import annotations

import types

from homeassistant.const import EntityCategory
from plejd_cloud.cloud import PlejdCloudDevice
from plejd_cloud.number import (
    PlejdDimLevelNumber,
    PlejdInrushCurrentNumber,
    PlejdRelayOffTimeNumber,
    PlejdTransitionTimeNumber,
    async_setup_entry,
)


def _device(category="light", address=5, dimmable=True, output_index=0, hardware_id=1):
    return PlejdCloudDevice(
        device_id="d1",
        name="Lamp",
        address=address,
        output_index=output_index,
        outputs=[address],
        hardware_id=hardware_id,
        model="DIM-01",
        category=category,
        dimmable=dimmable,
        traits=0x03,
        room_id="r1",
    )


class _Coordinator:
    def __init__(self, devices, settings=None):
        self.devices = devices
        self.min_calls = []
        self.max_calls = []
        self.start_calls = []
        self.speed_calls = []
        self.relay_off_calls = []
        self.inrush_calls = []
        self._settings = settings
        self._listener = None

    def settings_for(self, address):
        return self._settings

    def async_add_listener(self, cb):
        self._listener = cb
        return lambda: None

    async def async_set_output_min_level(self, address, output, fraction):
        self.min_calls.append((address, output, fraction))

    async def async_set_output_max_level(self, address, output, fraction):
        self.max_calls.append((address, output, fraction))

    async def async_set_output_start_level(self, address, output, fraction):
        self.start_calls.append((address, output, fraction))

    async def async_set_output_speed(self, address, output, seconds):
        self.speed_calls.append((address, output, seconds))

    async def async_set_output_relay_off_time(self, address, output, seconds):
        self.relay_off_calls.append((address, output, seconds))

    async def async_set_output_inrush_current(self, address, output, time_ms):
        self.inrush_calls.append((address, output, time_ms))


async def test_setup_creates_settings_only_for_dimmable_lights():
    coord = _Coordinator(
        [
            _device(),
            _device(category="switch"),
            _device(dimmable=False),
            _device(address=None),
        ]
    )
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    # One dimmable light -> min + max + start brightness + transition-time + inrush-current
    # (hardware_id=1 is in PHASE_DIM_HARDWARE).
    assert len(added) == 5
    assert {e._attr_translation_key for e in added} == {
        "min_dim_level",
        "max_dim_level",
        "start_level",
        "transition_time",
        "inrush_current_time",
    }


async def test_setup_creates_relay_off_time_for_relay_hardware():
    # hardware_id=3 (CTR-01) is in RELAY_HARDWARE; dimmable=False so no dim entities.
    coord = _Coordinator([_device(hardware_id=3, dimmable=False)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 1
    assert added[0]._attr_translation_key == "relay_off_time"


def test_attributes_and_unique_id():
    minimum = PlejdDimLevelNumber(_Coordinator([]), _device(), "min")
    maximum = PlejdDimLevelNumber(_Coordinator([]), _device(output_index=2), "max")
    assert minimum._attr_entity_category == EntityCategory.CONFIG
    assert minimum._attr_unique_id == "d1_min_level"
    assert maximum._attr_unique_id == "d1_2_max_level"
    assert (minimum._attr_native_min_value, minimum._attr_native_max_value) == (0, 100)


async def test_set_value_routes_to_kind_setter_as_fraction():
    coord = _Coordinator([])
    minimum = PlejdDimLevelNumber(coord, _device(), "min")
    maximum = PlejdDimLevelNumber(coord, _device(), "max")
    start = PlejdDimLevelNumber(coord, _device(), "start")
    await minimum.async_set_native_value(20)
    await maximum.async_set_native_value(80)
    await start.async_set_native_value(40)
    assert coord.min_calls == [(5, 0, 0.2)]
    assert coord.max_calls == [(5, 0, 0.8)]
    assert coord.start_calls == [(5, 0, 0.4)]
    assert start._attr_translation_key == "start_level" and start._attr_unique_id == "d1_start_level"


async def test_restores_last_value_on_add():
    entity = PlejdDimLevelNumber(_Coordinator([]), _device(), "min")

    async def _last():
        return types.SimpleNamespace(native_value=35)

    entity.async_get_last_number_data = _last
    await entity.async_added_to_hass()
    assert entity._attr_native_value == 35


async def test_no_restore_when_no_prior_state():
    entity = PlejdDimLevelNumber(_Coordinator([]), _device(), "max")
    await entity.async_added_to_hass()
    assert getattr(entity, "_attr_native_value", None) is None


async def test_live_read_arriving_during_restore_wins_over_stale_state():
    """A read reply landing while awaiting restore must win — the listener is subscribed
    first, and the settings cache is re-checked after the await."""
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    entity = PlejdDimLevelNumber(coord, _device(), "min")

    async def _last():
        coord._settings = OutputSettings(min_level=15.0)  # the "live" read arrives mid-await
        return types.SimpleNamespace(native_value=35)  # stale restored value

    entity.async_get_last_number_data = _last
    await entity.async_added_to_hass()
    assert entity._attr_native_value == 15.0  # live value wins, not the stale 35


async def test_init_uses_coordinator_settings_min():
    from plejd_cloud.protocol import OutputSettings

    settings = OutputSettings(min_level=20.0, max_level=90.0)
    entity = PlejdDimLevelNumber(_Coordinator([], settings=settings), _device(), "min")
    await entity.async_added_to_hass()
    assert entity._attr_native_value == 20.0


async def test_init_uses_coordinator_settings_max():
    from plejd_cloud.protocol import OutputSettings

    settings = OutputSettings(min_level=20.0, max_level=90.0)
    entity = PlejdDimLevelNumber(_Coordinator([], settings=settings), _device(), "max")
    await entity.async_added_to_hass()
    assert entity._attr_native_value == 90.0


async def test_listener_updates_dim_level():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    entity = PlejdDimLevelNumber(coord, _device(), "min")
    await entity.async_added_to_hass()
    coord._settings = OutputSettings(min_level=15.0)
    coord._listener()
    assert entity._attr_native_value == 15.0


async def test_listener_no_update_when_settings_none():
    coord = _Coordinator([])
    entity = PlejdDimLevelNumber(coord, _device(), "min")
    await entity.async_added_to_hass()
    coord._listener()  # settings_for returns None -> early return, no crash
    assert getattr(entity, "_attr_native_value", None) is None


def test_transition_time_attributes():
    t = PlejdTransitionTimeNumber(_Coordinator([]), _device(output_index=2))
    assert t._attr_entity_category == EntityCategory.CONFIG
    assert t._attr_translation_key == "transition_time"
    assert t._attr_unique_id == "d1_2_transition_time"
    assert (t._attr_native_min_value, t._attr_native_max_value) == (0, 10)


async def test_transition_time_sets_seconds_and_restores():
    coord = _Coordinator([])
    t = PlejdTransitionTimeNumber(coord, _device())
    await t.async_set_native_value(2.5)
    assert coord.speed_calls == [(5, 0, 2.5)] and t._attr_native_value == 2.5

    async def _last():
        return types.SimpleNamespace(native_value=1.0)

    t.async_get_last_number_data = _last
    await t.async_added_to_hass()
    assert t._attr_native_value == 1.0


async def test_transition_time_init_uses_coordinator_settings():
    from plejd_cloud.protocol import OutputSettings

    settings = OutputSettings(speed=3.5)
    t = PlejdTransitionTimeNumber(_Coordinator([], settings=settings), _device())
    await t.async_added_to_hass()
    assert t._attr_native_value == 3.5


async def test_transition_time_listener_updates_speed():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    t = PlejdTransitionTimeNumber(coord, _device())
    await t.async_added_to_hass()
    coord._settings = OutputSettings(speed=2.0)
    coord._listener()
    assert t._attr_native_value == 2.0


async def test_transition_time_listener_no_update_when_settings_none():
    coord = _Coordinator([])
    t = PlejdTransitionTimeNumber(coord, _device())
    await t.async_added_to_hass()
    coord._listener()  # settings_for returns None -> early return, no crash
    assert getattr(t, "_attr_native_value", None) is None


async def test_transition_time_live_read_during_restore_wins():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    t = PlejdTransitionTimeNumber(coord, _device())

    async def _last():
        coord._settings = OutputSettings(speed=2.0)
        return types.SimpleNamespace(native_value=1.0)

    t.async_get_last_number_data = _last
    await t.async_added_to_hass()
    assert t._attr_native_value == 2.0


# ── PlejdRelayOffTimeNumber ───────────────────────────────────────────────────


def test_relay_off_time_attributes():
    t = PlejdRelayOffTimeNumber(_Coordinator([]), _device(hardware_id=3, output_index=2))
    assert t._attr_entity_category == EntityCategory.CONFIG
    assert t._attr_translation_key == "relay_off_time"
    assert t._attr_unique_id == "d1_2_relay_off_time"
    assert t._attr_native_min_value == 0.1
    assert t._attr_native_max_value == 10.0


async def test_relay_off_time_set_value():
    coord = _Coordinator([])
    t = PlejdRelayOffTimeNumber(coord, _device(hardware_id=3))
    await t.async_set_native_value(2.0)
    assert coord.relay_off_calls == [(5, 0, 2.0)]
    assert t._attr_native_value == 2.0


async def test_relay_off_time_init_from_coordinator_settings():
    from plejd_cloud.protocol import OutputSettings

    settings = OutputSettings(relay_off_time=1.5)
    t = PlejdRelayOffTimeNumber(_Coordinator([], settings=settings), _device(hardware_id=3))
    await t.async_added_to_hass()
    assert t._attr_native_value == 1.5


async def test_relay_off_time_falls_back_to_restore():
    t = PlejdRelayOffTimeNumber(_Coordinator([]), _device(hardware_id=3))

    async def _last():
        return types.SimpleNamespace(native_value=3.0)

    t.async_get_last_number_data = _last
    await t.async_added_to_hass()
    assert t._attr_native_value == 3.0


async def test_relay_off_time_no_restore_when_no_prior_state():
    t = PlejdRelayOffTimeNumber(_Coordinator([]), _device(hardware_id=3))
    await t.async_added_to_hass()
    assert getattr(t, "_attr_native_value", None) is None


async def test_relay_off_time_listener_updates():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    t = PlejdRelayOffTimeNumber(coord, _device(hardware_id=3))
    await t.async_added_to_hass()
    coord._settings = OutputSettings(relay_off_time=4.0)
    coord._listener()
    assert t._attr_native_value == 4.0


async def test_relay_off_time_listener_no_update_when_settings_none():
    coord = _Coordinator([])
    t = PlejdRelayOffTimeNumber(coord, _device(hardware_id=3))
    await t.async_added_to_hass()
    coord._listener()
    assert getattr(t, "_attr_native_value", None) is None


async def test_relay_off_time_live_read_during_restore_wins():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    t = PlejdRelayOffTimeNumber(coord, _device(hardware_id=3))

    async def _last():
        coord._settings = OutputSettings(relay_off_time=4.0)
        return types.SimpleNamespace(native_value=3.0)

    t.async_get_last_number_data = _last
    await t.async_added_to_hass()
    assert t._attr_native_value == 4.0


# ── PlejdInrushCurrentNumber ──────────────────────────────────────────────────


async def test_setup_creates_inrush_for_phase_dim_hardware():
    # hardware_id=1 (DIM-01) is in PHASE_DIM_HARDWARE and is dimmable -> inrush entity.
    coord = _Coordinator([_device(hardware_id=1)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert any(e._attr_translation_key == "inrush_current_time" for e in added)


async def test_setup_no_inrush_for_non_phase_dim_hardware():
    # hardware_id=5 (LED-10) is NOT in PHASE_DIM_HARDWARE -> no inrush entity.
    coord = _Coordinator([_device(hardware_id=5)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert all(e._attr_translation_key != "inrush_current_time" for e in added)


def test_inrush_attributes_and_unique_id():
    t = PlejdInrushCurrentNumber(_Coordinator([]), _device(hardware_id=1))
    assert t._attr_entity_category == EntityCategory.CONFIG
    assert t._attr_translation_key == "inrush_current_time"
    assert t._attr_unique_id == "d1_inrush_current_time"
    assert t._attr_native_min_value == 0
    assert t._attr_native_max_value == 5000
    assert t._attr_native_step == 100
    assert t._attr_native_unit_of_measurement == "ms"


def test_inrush_unique_id_with_output_index():
    t = PlejdInrushCurrentNumber(_Coordinator([]), _device(hardware_id=1, output_index=2))
    assert t._attr_unique_id == "d1_2_inrush_current_time"


async def test_inrush_set_value_sends_ms():
    coord = _Coordinator([])
    t = PlejdInrushCurrentNumber(coord, _device(hardware_id=1))
    await t.async_set_native_value(500)
    assert coord.inrush_calls == [(5, 0, 500)]
    assert t._attr_native_value == 500


async def test_inrush_init_from_coordinator_settings():
    from plejd_cloud.protocol import OutputSettings

    settings = OutputSettings(inrush_current_ms=300)
    t = PlejdInrushCurrentNumber(_Coordinator([], settings=settings), _device(hardware_id=1))
    await t.async_added_to_hass()
    assert t._attr_native_value == 300


async def test_inrush_falls_back_to_restore():
    t = PlejdInrushCurrentNumber(_Coordinator([]), _device(hardware_id=1))

    async def _last():
        return types.SimpleNamespace(native_value=200)

    t.async_get_last_number_data = _last
    await t.async_added_to_hass()
    assert t._attr_native_value == 200


async def test_inrush_no_restore_when_no_prior_state():
    t = PlejdInrushCurrentNumber(_Coordinator([]), _device(hardware_id=1))
    await t.async_added_to_hass()
    assert getattr(t, "_attr_native_value", None) is None


async def test_inrush_listener_updates():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    t = PlejdInrushCurrentNumber(coord, _device(hardware_id=1))
    await t.async_added_to_hass()
    coord._settings = OutputSettings(inrush_current_ms=400)
    coord._listener()
    assert t._attr_native_value == 400


async def test_inrush_listener_no_update_when_settings_none():
    coord = _Coordinator([])
    t = PlejdInrushCurrentNumber(coord, _device(hardware_id=1))
    await t.async_added_to_hass()
    coord._listener()
    assert getattr(t, "_attr_native_value", None) is None


async def test_inrush_live_read_during_restore_wins():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    t = PlejdInrushCurrentNumber(coord, _device(hardware_id=1))

    async def _last():
        coord._settings = OutputSettings(inrush_current_ms=400)
        return types.SimpleNamespace(native_value=200)

    t.async_get_last_number_data = _last
    await t.async_added_to_hass()
    assert t._attr_native_value == 400


async def test_inrush_disabled_value_zero_accepted():
    coord = _Coordinator([])
    t = PlejdInrushCurrentNumber(coord, _device(hardware_id=1))
    await t.async_set_native_value(0)
    assert coord.inrush_calls == [(5, 0, 0)]
