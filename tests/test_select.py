"""Tests for the Plejd select platform (dimmer curve + phase-dim edge + boot state)."""

from __future__ import annotations

import types

from homeassistant.const import EntityCategory
from plejd_cloud.cloud import PlejdCloudDevice
from plejd_cloud.select import PlejdBootStateSelect, PlejdOutputSettingSelect, PlejdRelayPoleSelect, async_setup_entry


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
        self.curve_calls = []
        self.phase_calls = []
        self.boot_state_calls = []
        self.relay_pole_calls = []
        self._settings = settings
        self._listener = None

    def settings_for(self, address):
        return self._settings

    def async_add_listener(self, cb):
        self._listener = cb
        return lambda: None

    async def async_set_output_curve(self, address, output, curve):
        self.curve_calls.append((address, output, curve))

    async def async_set_output_phase_dim(self, address, output, phase):
        self.phase_calls.append((address, output, phase))

    async def async_set_output_boot_state(self, address, output, use_last):
        self.boot_state_calls.append((address, output, use_last))

    async def async_set_output_relay_config(self, address, output, config):
        self.relay_pole_calls.append((address, output, config))


async def test_setup_creates_selects_per_category():
    # dimmable light (hw 1, phase-cut): curve + phase + boot_state
    # switch: boot_state only
    # non-dimmable light: boot_state only
    # no-address: skipped
    coord = _Coordinator([_device(), _device(category="switch"), _device(dimmable=False), _device(address=None)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    keys = [e._attr_translation_key for e in added]
    assert keys.count("dim_curve") == 1
    assert keys.count("phase_dim") == 1
    assert keys.count("boot_state") == 3  # dimmable light + switch + non-dimmable light
    assert len(added) == 5


async def test_non_phase_dimmer_gets_curve_and_boot_state():
    # LED-10 (hardware 5) dims but isn't a phase-cut dimmer -> curve + boot_state, no phase-edge.
    coord = _Coordinator([_device(hardware_id=5)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert [e._attr_translation_key for e in added] == ["dim_curve", "boot_state"]


def test_options_and_unique_id():
    curve = PlejdOutputSettingSelect(_Coordinator([]), _device(), "curve")
    phase = PlejdOutputSettingSelect(_Coordinator([]), _device(output_index=2), "phase")
    assert curve._attr_entity_category == EntityCategory.CONFIG
    assert curve._attr_options == ["standard", "linear", "logarithmic", "partial"]
    assert curve._attr_unique_id == "d1_curve"
    assert phase._attr_options == ["trailing_edge", "leading_edge"]
    assert phase._attr_unique_id == "d1_2_phase"


async def test_select_option_sends_mapped_wire_value():
    coord = _Coordinator([])
    curve = PlejdOutputSettingSelect(coord, _device(), "curve")
    phase = PlejdOutputSettingSelect(coord, _device(), "phase")
    await curve.async_select_option("partial")  # -> 3
    await phase.async_select_option("leading_edge")  # -> 1
    assert coord.curve_calls == [(5, 0, 3)]
    assert coord.phase_calls == [(5, 0, 1)]
    assert curve._attr_current_option == "partial"


async def test_restores_last_known_option():
    entity = PlejdOutputSettingSelect(_Coordinator([]), _device(), "curve")

    async def _last():
        return types.SimpleNamespace(state="linear")

    entity.async_get_last_state = _last
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "linear"


async def test_ignores_unknown_restored_option():
    entity = PlejdOutputSettingSelect(_Coordinator([]), _device(), "phase")

    async def _last():
        return types.SimpleNamespace(state="bogus")

    entity.async_get_last_state = _last
    await entity.async_added_to_hass()
    assert getattr(entity, "_attr_current_option", None) is None


async def test_init_uses_coordinator_settings_curve():
    from plejd_cloud.protocol import OutputSettings

    settings = OutputSettings(curve=1)  # 1 = linear
    entity = PlejdOutputSettingSelect(_Coordinator([], settings=settings), _device(), "curve")
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "linear"


async def test_init_uses_coordinator_settings_phase():
    from plejd_cloud.protocol import OutputSettings

    settings = OutputSettings(phase_dim=1)  # 1 = leading_edge
    entity = PlejdOutputSettingSelect(_Coordinator([], settings=settings), _device(), "phase")
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "leading_edge"


async def test_init_falls_back_to_restore_when_settings_none():
    entity = PlejdOutputSettingSelect(_Coordinator([]), _device(), "curve")

    async def _last():
        return types.SimpleNamespace(state="partial")

    entity.async_get_last_state = _last
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "partial"


async def test_live_read_arriving_during_restore_wins_over_stale_state():
    """A read reply landing while awaiting restore must win — the listener is subscribed
    first, and the settings cache is re-checked after the await."""
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    entity = PlejdOutputSettingSelect(coord, _device(), "curve")

    async def _last():
        coord._settings = OutputSettings(curve=1)  # the "live" read arrives mid-await -> linear
        return types.SimpleNamespace(state="partial")  # stale restored value

    entity.async_get_last_state = _last
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "linear"  # live value wins, not the stale "partial"


async def test_listener_updates_curve_option():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    entity = PlejdOutputSettingSelect(coord, _device(), "curve")
    await entity.async_added_to_hass()
    coord._settings = OutputSettings(curve=3)  # partial
    coord._listener()
    assert entity._attr_current_option == "partial"


async def test_listener_no_update_when_settings_none():
    coord = _Coordinator([])
    entity = PlejdOutputSettingSelect(coord, _device(), "curve")
    await entity.async_added_to_hass()
    coord._listener()  # settings_for returns None -> early return, no crash
    assert getattr(entity, "_attr_current_option", None) is None


async def test_listener_no_update_when_unknown_raw_value():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([], settings=OutputSettings(curve=99))  # 99 not in CURVE_OPTIONS
    entity = PlejdOutputSettingSelect(coord, _device(), "curve")
    await entity.async_added_to_hass()
    # Should not crash and should not set an option
    assert getattr(entity, "_attr_current_option", None) is None


# ── PlejdBootStateSelect ──────────────────────────────────────────────────────


def test_boot_state_options_and_unique_id():
    entity = PlejdBootStateSelect(_Coordinator([]), _device())
    assert entity._attr_entity_category == EntityCategory.CONFIG
    assert entity._attr_options == ["previous_state", "off"]
    assert entity._attr_unique_id == "d1_boot_state"
    assert entity._attr_translation_key == "boot_state"


def test_boot_state_unique_id_with_output_index():
    entity = PlejdBootStateSelect(_Coordinator([]), _device(output_index=2))
    assert entity._attr_unique_id == "d1_2_boot_state"


async def test_boot_state_select_option_sends_bool():
    coord = _Coordinator([])
    entity = PlejdBootStateSelect(coord, _device())
    await entity.async_select_option("previous_state")
    assert coord.boot_state_calls == [(5, 0, True)]
    assert entity._attr_current_option == "previous_state"

    await entity.async_select_option("off")
    assert coord.boot_state_calls[-1] == (5, 0, False)
    assert entity._attr_current_option == "off"


async def test_boot_state_init_from_coordinator_settings_use_last():
    from plejd_cloud.protocol import OutputSettings

    settings = OutputSettings(boot_state=True)
    entity = PlejdBootStateSelect(_Coordinator([], settings=settings), _device())
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "previous_state"


async def test_boot_state_init_from_coordinator_settings_off():
    from plejd_cloud.protocol import OutputSettings

    settings = OutputSettings(boot_state=False)
    entity = PlejdBootStateSelect(_Coordinator([], settings=settings), _device())
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "off"


async def test_boot_state_falls_back_to_restore():
    entity = PlejdBootStateSelect(_Coordinator([]), _device())

    async def _last():
        return types.SimpleNamespace(state="off")

    entity.async_get_last_state = _last
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "off"


async def test_boot_state_live_read_during_restore_wins():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    entity = PlejdBootStateSelect(coord, _device())

    async def _last():
        coord._settings = OutputSettings(boot_state=True)
        return types.SimpleNamespace(state="off")

    entity.async_get_last_state = _last
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "previous_state"


async def test_boot_state_ignores_unknown_restored_state():
    entity = PlejdBootStateSelect(_Coordinator([]), _device())

    async def _last():
        return types.SimpleNamespace(state="bogus")

    entity.async_get_last_state = _last
    await entity.async_added_to_hass()
    assert getattr(entity, "_attr_current_option", None) is None


async def test_boot_state_listener_updates():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    entity = PlejdBootStateSelect(coord, _device())
    await entity.async_added_to_hass()
    coord._settings = OutputSettings(boot_state=True)
    coord._listener()
    assert entity._attr_current_option == "previous_state"


async def test_boot_state_listener_no_update_when_settings_none():
    coord = _Coordinator([])
    entity = PlejdBootStateSelect(coord, _device())
    await entity.async_added_to_hass()
    coord._listener()  # settings_for returns None -> early return
    assert getattr(entity, "_attr_current_option", None) is None


async def test_boot_state_listener_ignores_none_raw_value():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    entity = PlejdBootStateSelect(coord, _device())
    await entity.async_added_to_hass()
    coord._settings = OutputSettings(boot_state=None)
    coord._listener()
    assert getattr(entity, "_attr_current_option", None) is None


# ── PlejdRelayPoleSelect ──────────────────────────────────────────────────────


async def test_setup_creates_relay_pole_select_for_relay_config_hardware():
    # hardware_id=11 (DIM-01-2P) is in RELAY_CONFIG_HARDWARE -> relay_pole_config entity
    coord = _Coordinator([_device(hardware_id=11)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    keys = [e._attr_translation_key for e in added]
    assert "relay_pole_config" in keys


async def test_setup_does_not_create_relay_pole_select_for_other_hardware():
    coord = _Coordinator([_device(hardware_id=1)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert all(e._attr_translation_key != "relay_pole_config" for e in added)


def test_relay_pole_options_and_unique_id():
    entity = PlejdRelayPoleSelect(_Coordinator([]), _device(hardware_id=11))
    assert entity._attr_entity_category == EntityCategory.CONFIG
    assert entity._attr_options == ["two_pole", "one_pole"]
    assert entity._attr_unique_id == "d1_relay_pole_config"
    assert entity._attr_translation_key == "relay_pole_config"


def test_relay_pole_unique_id_with_output_index():
    entity = PlejdRelayPoleSelect(_Coordinator([]), _device(hardware_id=11, output_index=1))
    assert entity._attr_unique_id == "d1_1_relay_pole_config"


async def test_relay_pole_select_option_sends_wire_value():
    coord = _Coordinator([])
    entity = PlejdRelayPoleSelect(coord, _device(hardware_id=11))
    await entity.async_select_option("two_pole")
    assert coord.relay_pole_calls == [(5, 0, 0)]
    assert entity._attr_current_option == "two_pole"

    await entity.async_select_option("one_pole")
    assert coord.relay_pole_calls[-1] == (5, 0, 1)
    assert entity._attr_current_option == "one_pole"


async def test_relay_pole_init_from_coordinator_settings():
    from plejd_cloud.protocol import OutputSettings

    settings = OutputSettings(relay_pole_config=1)
    entity = PlejdRelayPoleSelect(_Coordinator([], settings=settings), _device(hardware_id=11))
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "one_pole"


async def test_relay_pole_falls_back_to_restore():
    entity = PlejdRelayPoleSelect(_Coordinator([]), _device(hardware_id=11))

    async def _last():
        return types.SimpleNamespace(state="two_pole")

    entity.async_get_last_state = _last
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "two_pole"


async def test_relay_pole_live_read_during_restore_wins():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    entity = PlejdRelayPoleSelect(coord, _device(hardware_id=11))

    async def _last():
        coord._settings = OutputSettings(relay_pole_config=1)
        return types.SimpleNamespace(state="two_pole")

    entity.async_get_last_state = _last
    await entity.async_added_to_hass()
    assert entity._attr_current_option == "one_pole"


async def test_relay_pole_ignores_unknown_restored_state():
    entity = PlejdRelayPoleSelect(_Coordinator([]), _device(hardware_id=11))

    async def _last():
        return types.SimpleNamespace(state="bogus")

    entity.async_get_last_state = _last
    await entity.async_added_to_hass()
    assert getattr(entity, "_attr_current_option", None) is None


async def test_relay_pole_listener_updates():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    entity = PlejdRelayPoleSelect(coord, _device(hardware_id=11))
    await entity.async_added_to_hass()
    coord._settings = OutputSettings(relay_pole_config=0)
    coord._listener()
    assert entity._attr_current_option == "two_pole"


async def test_relay_pole_listener_no_update_when_settings_none():
    coord = _Coordinator([])
    entity = PlejdRelayPoleSelect(coord, _device(hardware_id=11))
    await entity.async_added_to_hass()
    coord._listener()
    assert getattr(entity, "_attr_current_option", None) is None


async def test_relay_pole_listener_ignores_unknown_raw_value():
    from plejd_cloud.protocol import OutputSettings

    coord = _Coordinator([])
    entity = PlejdRelayPoleSelect(coord, _device(hardware_id=11))
    await entity.async_added_to_hass()
    coord._settings = OutputSettings(relay_pole_config=99)
    coord._listener()
    assert getattr(entity, "_attr_current_option", None) is None
