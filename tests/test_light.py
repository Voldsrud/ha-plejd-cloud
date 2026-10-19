"""Tests for the Plejd light platform."""

from __future__ import annotations

import types

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode
from plejd_cloud.cloud import PlejdCloudDevice, PlejdCloudRoom
from plejd_cloud.light import PlejdLight, PlejdRoomLight, async_setup_entry
from plejd_cloud.protocol import OutputState


def _device(category="light", dimmable=True, address=5):
    return PlejdCloudDevice(
        device_id="d1",
        output_index=0,
        name="Kitchen",
        address=address,
        outputs=[address],
        hardware_id=1,
        model="DIM-01",
        category=category,
        dimmable=dimmable,
        traits=3,
        room_id="r1",
    )


class _SpyRamp:
    def __init__(self):
        self.calls = []

    def start(self, address, direction, *, current=None, member_addresses=None):
        self.calls.append(("start", address, direction, current, member_addresses))

    def stop(self, address):
        self.calls.append(("stop", address))


class _Coordinator:
    def __init__(self, devices, state=None, rooms=None, states=None):
        self.devices = devices
        self._state = state
        self._states = states or {}  # address -> OutputState, for room aggregation
        self.rooms = rooms or []
        self.commands = []
        self.group_calls = []  # (address, on, level, member_addresses) via async_set_group_output
        self.listeners = []
        self.available = True
        self.dim_ramp = _SpyRamp()

    def state_for(self, address):
        if self._states:
            return self._states.get(address)
        return self._state

    def async_add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)

    async def async_set_output(self, address, on, level):
        self.commands.append((address, on, level))

    async def async_set_group_output(self, address, on, level, member_addresses):
        self.commands.append((address, on, level))
        self.group_calls.append((address, on, level, list(member_addresses)))


async def test_setup_entry_creates_lights_only_for_light_devices():
    coord = _Coordinator([_device(), _device(category="switch"), _device(category="light", address=None)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 1  # the switch and the address-less light are skipped


def test_light_available_follows_coordinator():
    coord = _Coordinator([])
    light = PlejdLight(coord, _device())
    assert light.available is True
    coord.available = False
    assert light.available is False


def test_dimmable_light_color_mode():
    light = PlejdLight(_Coordinator([]), _device(dimmable=True))
    assert light._attr_color_mode == ColorMode.BRIGHTNESS
    assert light._attr_supported_color_modes == {ColorMode.BRIGHTNESS}
    assert light._attr_unique_id == "d1"
    assert light._attr_device_info["model"] == "DIM-01"


def test_non_dimmable_light_is_onoff():
    light = PlejdLight(_Coordinator([]), _device(dimmable=False))
    assert light._attr_color_mode == ColorMode.ONOFF


def test_is_on_and_brightness_from_state():
    coord = _Coordinator([], state=OutputState(output=0, on=True, level=180))
    light = PlejdLight(coord, _device(dimmable=True))
    assert light.is_on is True
    assert light.brightness == 180


def test_unknown_state_is_none():
    light = PlejdLight(_Coordinator([], state=None), _device())
    assert light.is_on is None
    assert light.brightness is None


def test_non_dimmable_has_no_brightness():
    coord = _Coordinator([], state=OutputState(output=0, on=True, level=180))
    light = PlejdLight(coord, _device(dimmable=False))
    assert light.brightness is None


async def test_turn_on_with_brightness():
    coord = _Coordinator([])
    light = PlejdLight(coord, _device())
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 77})
    assert coord.commands == [(5, True, 77)]


async def test_turn_on_without_brightness_defaults_full():
    coord = _Coordinator([], state=None)
    light = PlejdLight(coord, _device())
    await light.async_turn_on()
    assert coord.commands == [(5, True, 255)]


async def test_turn_on_without_brightness_restores_last_level():
    coord = _Coordinator([], state=OutputState(output=0, on=False, level=140))
    light = PlejdLight(coord, _device())
    await light.async_turn_on()
    assert coord.commands == [(5, True, 140)]


async def test_turn_on_without_brightness_defaults_full_when_last_level_zero():
    # A level of 0 is never a real "on" position for a Plejd dimmer (ramps floor at
    # DIM_MIN=1) -> treated the same as unknown, not commanded as an invisible on-at-0.
    coord = _Coordinator([], state=OutputState(output=0, on=False, level=0))
    light = PlejdLight(coord, _device())
    await light.async_turn_on()
    assert coord.commands == [(5, True, 255)]


async def test_turn_off():
    coord = _Coordinator([])
    light = PlejdLight(coord, _device())
    await light.async_turn_off()
    assert coord.commands == [(5, False, 0)]


async def test_added_to_hass_subscribes():
    coord = _Coordinator([])
    light = PlejdLight(coord, _device())
    await light.async_added_to_hass()
    assert len(coord.listeners) == 1


# ── Remote hold-to-dim entity services ────────────────────────────────────────


async def test_setup_entry_registers_dim_entity_services(monkeypatch):
    import plejd_cloud.light as light_mod

    registered = []

    class _Platform:
        def async_register_entity_service(self, name, schema, func):
            registered.append((name, func))

    monkeypatch.setattr(light_mod.entity_platform, "async_get_current_platform", lambda: _Platform())
    coord = _Coordinator([_device()])
    entry = types.SimpleNamespace(runtime_data=coord)
    await async_setup_entry(None, entry, lambda entities: None)
    assert (light_mod.SERVICE_START_DIM, "async_start_dim") in registered
    assert (light_mod.SERVICE_STOP_DIM, "async_stop_dim") in registered


async def test_async_start_dim_ramps_dimmable_light():
    coord = _Coordinator([])
    light = PlejdLight(coord, _device(dimmable=True, address=7))
    await light.async_start_dim("up")
    await light.async_start_dim("down")
    assert coord.dim_ramp.calls == [("start", 7, 1, None, None), ("start", 7, -1, None, None)]


async def test_async_start_dim_noop_for_non_dimmable_light():
    coord = _Coordinator([])
    light = PlejdLight(coord, _device(dimmable=False, address=7))
    await light.async_start_dim("up")
    assert coord.dim_ramp.calls == []  # on/off outputs can't ramp


async def test_async_stop_dim_stops_ramp():
    coord = _Coordinator([])
    light = PlejdLight(coord, _device(address=7))
    await light.async_stop_dim()
    assert coord.dim_ramp.calls == [("stop", 7)]


# ── PlejdRoomLight (whole-room group control) ─────────────────────────────────


def _room(address=14, members=(5, 6), dimmable=True, dimmable_addresses=None):
    if dimmable_addresses is None:
        dimmable_addresses = list(members) if dimmable else []
    return PlejdCloudRoom(
        room_id="r1",
        name="Kök",
        address=address,
        member_addresses=list(members),
        dimmable=dimmable,
        dimmable_addresses=dimmable_addresses,
    )


async def test_setup_entry_creates_a_light_per_room():
    coord = _Coordinator([_device()], rooms=[_room(), _room(address=16, members=(7,))])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert sum(isinstance(e, PlejdRoomLight) for e in added) == 2


async def test_room_turn_on_sends_one_group_command():
    coord = _Coordinator([], rooms=[_room()])
    light = PlejdRoomLight(coord, _room(address=14))
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 120})
    assert coord.commands == [(14, True, 120)]  # a single 0x0098 to the room group address


async def test_room_turn_on_without_brightness_restores_or_full():
    coord = _Coordinator(
        [], states={5: OutputState(output=0, on=True, level=100), 6: OutputState(output=0, on=True, level=200)}
    )
    light = PlejdRoomLight(coord, _room(members=(5, 6)))
    await light.async_turn_on()  # no brightness → the room's current average (150)
    assert coord.commands == [(14, True, 150)]


async def test_room_turn_on_without_brightness_partially_lit_keeps_visible_brightness():
    # Only member 5 is on (level 200); member 6 is off but remembers a stale low level
    # (50). Turning "on" a partially-lit room must preserve the visible brightness (200),
    # not blend in the off member's remembered level the way the all-off restore does.
    coord = _Coordinator(
        [], states={5: OutputState(output=0, on=True, level=200), 6: OutputState(output=0, on=False, level=50)}
    )
    light = PlejdRoomLight(coord, _room(members=(5, 6)))
    await light.async_turn_on()
    assert coord.commands == [(14, True, 200)]


async def test_room_turn_on_without_brightness_restores_average_of_off_members():
    coord = _Coordinator(
        [], states={5: OutputState(output=0, on=False, level=100), 6: OutputState(output=0, on=False, level=200)}
    )
    light = PlejdRoomLight(coord, _room(members=(5, 6)))
    await light.async_turn_on()  # every member is off but remembers a level -> restore it, not full
    assert coord.commands == [(14, True, 150)]


async def test_room_turn_on_ignores_zero_levels_when_restoring_mixed_members():
    # A level of 0 is degenerate/unknown (see PlejdLight) - blending it into the average
    # would dilute a real remembered level instead of restoring it outright.
    coord = _Coordinator(
        [], states={5: OutputState(output=0, on=False, level=0), 6: OutputState(output=0, on=False, level=200)}
    )
    light = PlejdRoomLight(coord, _room(members=(5, 6)))
    await light.async_turn_on()
    assert coord.commands == [(14, True, 200)]  # restores the one known level, not (0+200)/2


async def test_room_turn_on_without_brightness_defaults_full_when_average_zero():
    coord = _Coordinator(
        [], states={5: OutputState(output=0, on=False, level=0), 6: OutputState(output=0, on=False, level=0)}
    )
    light = PlejdRoomLight(coord, _room(members=(5, 6)))
    await light.async_turn_on()  # an average of 0 is likewise treated as unknown -> full
    assert coord.commands == [(14, True, 255)]


async def test_room_turn_off_sends_group_off():
    coord = _Coordinator([], rooms=[_room()])
    light = PlejdRoomLight(coord, _room(address=16))
    await light.async_turn_off()
    assert coord.commands == [(16, False, 0)]


async def test_room_turn_on_without_brightness_defaults_full_when_not_dimmable():
    # An on/off-only room never gets a brightness kwarg from HA (ColorMode.ONOFF), and
    # must not "restore" a possibly-zero known level either -> always command full, like
    # PlejdLight does for a non-dimmable device.
    coord = _Coordinator(
        [], states={5: OutputState(output=0, on=False, level=0), 6: OutputState(output=0, on=False, level=0)}
    )
    light = PlejdRoomLight(coord, _room(members=(5, 6), dimmable=False))
    await light.async_turn_on()
    assert coord.commands == [(14, True, 255)]


async def test_room_turn_on_forwards_member_addresses_for_optimistic_update():
    coord = _Coordinator([], rooms=[_room()])
    light = PlejdRoomLight(coord, _room(address=14, members=(5, 6)))
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 120})
    assert coord.group_calls == [(14, True, 120, [5, 6])]


async def test_room_turn_off_forwards_member_addresses_for_optimistic_update():
    coord = _Coordinator([], rooms=[_room()])
    light = PlejdRoomLight(coord, _room(address=16, members=(7, 8)))
    await light.async_turn_off()
    assert coord.group_calls == [(16, False, 0, [7, 8])]


def test_room_is_on_true_if_any_member_on():
    coord = _Coordinator(
        [], states={5: OutputState(output=0, on=False, level=0), 6: OutputState(output=0, on=True, level=90)}
    )
    assert PlejdRoomLight(coord, _room(members=(5, 6))).is_on is True


def test_room_state_none_when_no_member_states():
    light = PlejdRoomLight(_Coordinator([], states={}), _room(members=(5, 6)))
    assert light.is_on is None and light.brightness is None


def test_room_brightness_averages_on_members():
    coord = _Coordinator(
        [],
        states={
            5: OutputState(output=0, on=True, level=100),
            6: OutputState(output=0, on=False, level=200),
            7: OutputState(output=0, on=True, level=200),
        },
    )
    assert PlejdRoomLight(coord, _room(members=(5, 6, 7))).brightness == 150  # (100+200)/2


def test_room_brightness_excludes_on_off_only_members():
    coord = _Coordinator(
        [],
        states={
            5: OutputState(output=0, on=True, level=50),  # dimmable
            6: OutputState(output=0, on=True, level=255),  # on/off-only, level is meaningless
        },
    )
    room = _room(members=(5, 6), dimmable=True, dimmable_addresses=[5])
    assert PlejdRoomLight(coord, room).brightness == 50  # only the dimmable member counts


def test_room_available_and_identity():
    coord = _Coordinator([], rooms=[_room()])
    light = PlejdRoomLight(coord, _room())
    assert light.available is True
    assert light._attr_unique_id == "room_r1"
    assert light._attr_device_info["model"] == "Room"
    assert light._attr_color_mode == ColorMode.BRIGHTNESS


async def test_room_added_to_hass_subscribes():
    coord = _Coordinator([], rooms=[_room()])
    light = PlejdRoomLight(coord, _room())
    await light.async_added_to_hass()
    assert len(coord.listeners) == 1


def test_room_color_mode_onoff_when_no_member_is_dimmable():
    light = PlejdRoomLight(_Coordinator([]), _room(dimmable=False))
    assert light._attr_color_mode == ColorMode.ONOFF
    assert light._attr_supported_color_modes == {ColorMode.ONOFF}


def test_room_brightness_none_when_not_dimmable():
    coord = _Coordinator([], states={5: OutputState(output=0, on=True, level=180)})
    light = PlejdRoomLight(coord, _room(members=(5,), dimmable=False))
    assert light.brightness is None


# ── PlejdRoomLight hold-to-dim (ramps the room's group address) ───────────────


async def test_room_async_start_dim_ramps_with_seeded_state():
    coord = _Coordinator(
        [], states={5: OutputState(output=0, on=True, level=100), 6: OutputState(output=0, on=False, level=50)}
    )
    light = PlejdRoomLight(coord, _room(address=14, members=(5, 6)))
    await light.async_start_dim("up")
    # dim_ramp has no state read-back for a group address, so the room seeds it from its
    # own visible brightness (on members only) - is_on = any member on (True); level =
    # the one on member's level (100), not blended with the off member's remembered 50.
    # member_addresses routes each ramp tick through the member-aware group write.
    assert coord.dim_ramp.calls == [("start", 14, 1, (True, 100), [5, 6])]


async def test_room_async_start_dim_seeds_from_visible_not_blended_average():
    # Regression test: averaging in an off member's remembered level would seed a "dim
    # down" from a level higher than what's actually showing, briefly brightening the
    # lit output (and turning the off member on) on the ramp's first tick.
    coord = _Coordinator(
        [], states={5: OutputState(output=0, on=True, level=20), 6: OutputState(output=0, on=False, level=200)}
    )
    light = PlejdRoomLight(coord, _room(address=14, members=(5, 6)))
    await light.async_start_dim("down")
    assert coord.dim_ramp.calls == [("start", 14, -1, (True, 20), [5, 6])]


async def test_room_async_start_dim_noop_when_not_dimmable():
    coord = _Coordinator([])
    light = PlejdRoomLight(coord, _room(dimmable=False))
    await light.async_start_dim("up")
    assert coord.dim_ramp.calls == []  # on/off-only rooms can't ramp


async def test_room_async_stop_dim_stops_ramp():
    coord = _Coordinator([], rooms=[_room()])
    light = PlejdRoomLight(coord, _room(address=16))
    await light.async_stop_dim()
    assert coord.dim_ramp.calls == [("stop", 16)]
