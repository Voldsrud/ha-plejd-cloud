"""Tests for the Plejd coordinator."""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from plejd_cloud import connection as conn
from plejd_cloud import coordinator as coordinator_mod
from plejd_cloud.const import (
    CONF_CRYPTO_KEY,
    CONF_DEVICE_ADDRESSES,
    CONF_DEVICES,
    CONF_DISCOVERED_ADDRESS,
    CONF_GATEWAYS,
    CONF_INPUTS,
    CONF_INSTALLATION_ID,
    CONF_MOTION,
    CONF_RESOURCE_SET_ID,
    CONF_ROOM_NAMES,
    CONF_ROOMS,
    CONF_SCENES,
    CONF_SITE_ID,
    CONF_TRANSPORT,
    PLEJD_CHAR_DATA_UUID,
    PLEJD_SERVICE_UUID,
)
from plejd_cloud.coordinator import PlejdCoordinator

_KEY_HEX = "00112233445566778899aabbccddeeff"
_DEV = {
    "device_id": "d1",
    "name": "Kitchen",
    "address": 5,
    "output_index": 0,
    "outputs": [5],
    "hardware_id": 1,
    "model": "DIM-01",
    "category": "light",
    "dimmable": True,
    "traits": 3,
    "room_id": "r1",
    "object_id": None,
    "output_settings": None,
}


class _FakeClient:
    is_connected = True

    def __init__(self):
        self.writes = []
        self.notify_cb = None

    async def write_gatt_char(self, uuid, data, response=False):
        self.writes.append((uuid, bytes(data)))

    async def read_gatt_char(self, uuid):
        return bytes(range(16))

    async def start_notify(self, uuid, cb):
        self.notify_cb = cb

    async def disconnect(self):
        self.is_connected = False


def _entry(discovered="AA:BB:CC:DD:EE:01"):
    return types.SimpleNamespace(
        entry_id="e1",
        data={CONF_CRYPTO_KEY: _KEY_HEX, CONF_DEVICES: [_DEV], CONF_DISCOVERED_ADDRESS: discovered},
    )


def _info(address, rssi=-50):
    return types.SimpleNamespace(address=address, service_uuids=[PLEJD_SERVICE_UUID], rssi=rssi)


def _hass(infos=(), ble=None):
    return types.SimpleNamespace(
        data={},
        service_infos=list(infos),
        ble_devices=ble or {},
        config_entries=types.SimpleNamespace(
            async_update_entry=lambda entry, data: setattr(entry, "data", data),
            # async_start's ConfigEntryNotReady handler makes a best-effort cloud-poll
            # attempt; entry_id lookup misses here so it's a clean no-op for tests that
            # don't care about cloud-poll behavior specifically.
            async_get_entry=lambda eid: None,
        ),
    )


def _patch_connect(monkeypatch, client):
    async def _establish(cls, device, name, **kw):
        return client

    monkeypatch.setattr(conn, "establish_connection", _establish)


def test_devices_loaded_from_entry():
    c = PlejdCoordinator(_hass(), _entry())
    assert [d.device_id for d in c.devices] == ["d1"]


def test_devices_migrate_when_output_index_missing():
    legacy = {k: v for k, v in _DEV.items() if k != "output_index"}  # entry stored before the field
    entry = types.SimpleNamespace(
        entry_id="e1",
        data={CONF_CRYPTO_KEY: _KEY_HEX, CONF_DEVICES: [legacy], CONF_DISCOVERED_ADDRESS: None},
    )
    c = PlejdCoordinator(_hass(), entry)
    assert c.devices[0].output_index == 0


async def test_start_connects_to_a_plejd_device(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    assert client.notify_cb is not None  # connected + subscribed


async def test_start_raises_when_no_device_in_range():
    from homeassistant.exceptions import ConfigEntryNotReady

    c = PlejdCoordinator(_hass([]), _entry())
    with pytest.raises(ConfigEntryNotReady, match="no Plejd device"):
        await c.async_start()


async def test_start_raises_when_address_unresolvable():
    from homeassistant.exceptions import ConfigEntryNotReady

    hass = _hass([_info("01:02:03:04:05:a0")], {})  # discovered but not resolvable
    c = PlejdCoordinator(hass, _entry(discovered=None))
    with pytest.raises(ConfigEntryNotReady, match="could not resolve"):
        await c.async_start()


async def test_start_wraps_connect_failure(monkeypatch):
    from homeassistant.exceptions import ConfigEntryNotReady

    async def _boom(cls, device, name, **kw):
        raise OSError("ble down")

    monkeypatch.setattr(conn, "establish_connection", _boom)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    with pytest.raises(ConfigEntryNotReady, match="failed to connect"):
        await c.async_start()


def test_pick_device_prefers_discovered_then_rssi():
    hass = _hass([_info("X", rssi=-30), _info("AA:BB:CC:DD:EE:01", rssi=-80)])
    c = PlejdCoordinator(hass, _entry(discovered="AA:BB:CC:DD:EE:01"))
    assert c._pick_device().address == "AA:BB:CC:DD:EE:01"  # discovered wins despite weaker rssi


def test_pick_device_uses_rssi_without_preference():
    hass = _hass([_info("X", rssi=-80), _info("Y", rssi=-40)])
    c = PlejdCoordinator(hass, _entry(discovered=None))
    assert c._pick_device().address == "Y"


async def test_set_output_reflects_state_immediately(monkeypatch):
    """No notification replay needed - a second command sent right after must see this
    state, not stale state (a fast on-then-off from the panel must not read "still off"
    and send "on" again just because the real echo hasn't landed yet)."""
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_output(5, True, 120)
    assert c.state_for(5).on is True and c.state_for(5).level == 120


async def test_set_output_notifies_listeners(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    seen = []
    c.async_add_listener(lambda: seen.append(1))
    await c.async_set_output(5, True, 120)
    assert seen == [1]


async def test_set_output_off_preserves_remembered_brightness(monkeypatch):
    # Turning off must not zero out the remembered brightness - the protocol's off
    # payload (level=0) is not "the light is now dim to 0", and a later turn_on()
    # restore (PlejdLight.async_turn_on) relies on the prior level surviving.
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_output(5, True, 150)
    await c.async_set_output(5, False, 0)
    assert c.state_for(5).on is False and c.state_for(5).level == 150


async def test_set_output_off_falls_back_to_zero_for_unknown_prior_state(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_output(9, False, 0)  # output 9 has no prior state at all
    assert c.state_for(9).on is False and c.state_for(9).level == 0


async def test_set_output_notification_replay_still_confirms_state(monkeypatch):
    # A real device echo arriving after the optimistic update must not break anything -
    # replaying the same written command back through the notification path still
    # decodes to consistent state.
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_output(5, True, 120)
    _, payload = client.writes[-1]
    client.notify_cb(None, bytearray(payload))
    assert c.state_for(5).level == 120


async def test_set_output_without_connection_raises():
    from homeassistant.exceptions import HomeAssistantError

    c = PlejdCoordinator(_hass(), _entry())
    with pytest.raises(HomeAssistantError, match="not connected"):
        await c.async_set_output(5, True, 1)


async def test_all_off_turns_off_every_light_output(monkeypatch):
    from plejd_cloud.const import CMD_GROUP_STATE_AND_LEVEL
    from plejd_cloud.protocol import decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    dev2 = {**_DEV, "device_id": "d2", "address": 6}
    dev_switch = {**_DEV, "device_id": "d3", "address": 7, "category": "switch"}
    dev_no_address = {**_DEV, "device_id": "d4", "address": None}
    entry = _entry(discovered=None)
    entry.data[CONF_DEVICES] = [_DEV, dev2, dev_switch, dev_no_address]
    c = PlejdCoordinator(hass, entry)
    await c.async_start()
    client.writes.clear()  # drop connect-time reads; only the all_off writes matter here

    await c.async_all_off()

    commands = [decode_command(c._connection.mesh.decrypt(w[1])) for w in client.writes if w[0] == PLEJD_CHAR_DATA_UUID]
    off_cmds = [cmd for cmd in commands if cmd.command == CMD_GROUP_STATE_AND_LEVEL]
    assert sorted(cmd.address for cmd in off_cmds) == [5, 6]  # only the two light outputs
    assert all(cmd.data[0] == 0 for cmd in off_cmds)  # off, not on


async def test_all_off_one_output_failure_does_not_skip_the_rest(monkeypatch):
    """A write failure for one light output must not abort turning off the rest."""
    from homeassistant.exceptions import HomeAssistantError

    dev2 = {**_DEV, "device_id": "d2", "address": 6}
    entry = types.SimpleNamespace(
        entry_id="e1",
        data={CONF_CRYPTO_KEY: _KEY_HEX, CONF_DEVICES: [_DEV, dev2], CONF_DISCOVERED_ADDRESS: None},
    )
    c = PlejdCoordinator(_hass(), entry)
    attempted: list[int] = []

    async def _async_set_output(address, on, level):
        attempted.append(address)
        if address == 5:
            raise HomeAssistantError("not connected")

    monkeypatch.setattr(c, "async_set_output", _async_set_output)
    await c.async_all_off()  # no exception propagates
    assert sorted(attempted) == [5, 6]  # output 6 was still turned off despite output 5 failing


async def test_all_off_raises_when_every_output_fails(monkeypatch):
    """If no output was actually turned off, the caller must be told all_off failed."""
    from homeassistant.exceptions import HomeAssistantError

    dev2 = {**_DEV, "device_id": "d2", "address": 6}
    entry = types.SimpleNamespace(
        entry_id="e1",
        data={CONF_CRYPTO_KEY: _KEY_HEX, CONF_DEVICES: [_DEV, dev2], CONF_DISCOVERED_ADDRESS: None},
    )
    c = PlejdCoordinator(_hass(), entry)
    attempted: list[int] = []

    async def _async_set_output(address, on, level):
        attempted.append(address)
        raise HomeAssistantError("not connected")

    monkeypatch.setattr(c, "async_set_output", _async_set_output)
    with pytest.raises(HomeAssistantError, match="failed to turn off any output"):
        await c.async_all_off()
    assert sorted(attempted) == [5, 6]  # both outputs were still attempted


async def test_set_group_output_writes_group_command_and_reflects_member_state(monkeypatch):
    from plejd_cloud.protocol import decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    seen = []
    c.async_add_listener(lambda: seen.append(1))
    await c.async_set_group_output(14, True, 120, [5, 6])
    # the single command was written to the room's group address...
    cmd = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert cmd.address == 14
    # ...and each member's own state is reflected immediately, without waiting on its
    # own notification (a group command's echo is keyed by the group address, not them)
    assert c.state_for(5).on is True and c.state_for(5).level == 120 and c.state_for(5).output == 5
    assert c.state_for(6).on is True and c.state_for(6).level == 120 and c.state_for(6).output == 6
    assert seen == [1]  # listeners notified of the optimistic update


async def test_set_group_output_off_preserves_member_levels(monkeypatch):
    # Turning a room off must not zero out each member's remembered brightness - the
    # protocol's off payload (level=0) is not a "the light is now dim to 0" fact, and
    # a later turn_on() restore relies on the prior level surviving the off command.
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_group_output(14, True, 150, [5, 6])  # first turn on at 150...
    await c.async_set_group_output(14, False, 0, [5, 6])  # ...then off
    assert c.state_for(5).on is False and c.state_for(5).level == 150
    assert c.state_for(6).on is False and c.state_for(6).level == 150


async def test_set_group_output_off_falls_back_to_zero_for_unknown_member(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_group_output(14, False, 0, [9])  # member 9 has no prior state at all
    assert c.state_for(9).on is False and c.state_for(9).level == 0


async def test_execute_scene_broadcasts(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_execute_scene(2)
    # scene is a broadcast (address 0) of opcode 0x0021 with the index
    from plejd_cloud.protocol import CMD_SCENE, decode_command

    cmd = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert cmd.address == 0 and cmd.command == CMD_SCENE and cmd.data == bytes([2])


async def test_execute_scene_without_connection_raises():
    from homeassistant.exceptions import HomeAssistantError

    c = PlejdCoordinator(_hass(), _entry())
    with pytest.raises(HomeAssistantError, match="not connected"):
        await c.async_execute_scene(1)


async def test_leave_mesh_group_writes_expected_vector(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_leave_mesh_group(0x27, 0x0E)

    from plejd_cloud.protocol import CMD_MESH_GROUP_MEMBERSHIP, decode_command

    cmd = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert cmd.address == 0x27 and cmd.command == CMD_MESH_GROUP_MEMBERSHIP
    assert cmd.data == bytes([0x01, 0x0E])


async def test_join_mesh_group_writes_expected_vector(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_join_mesh_group(0x27, 0x22)

    from plejd_cloud.protocol import CMD_MESH_GROUP_MEMBERSHIP, decode_command

    cmd = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert cmd.address == 0x27 and cmd.command == CMD_MESH_GROUP_MEMBERSHIP
    assert cmd.data == bytes([0x01, 0x22, 0x01])


async def test_join_mesh_group_without_connection_raises():
    from homeassistant.exceptions import HomeAssistantError

    c = PlejdCoordinator(_hass(), _entry())
    with pytest.raises(HomeAssistantError, match="not connected"):
        await c.async_join_mesh_group(0x27, 0x22)


def test_pick_device_handles_missing_rssi():
    hass = _hass([_info("X", rssi=None), _info("Y", rssi=-40)])
    c = PlejdCoordinator(hass, _entry(discovered=None))
    assert c._pick_device().address == "Y"  # None rssi treated as weakest, no crash


def test_state_for_none_before_connect():
    assert PlejdCoordinator(_hass(), _entry()).state_for(5) is None


def test_notify_outputs_calls_listeners():
    c = PlejdCoordinator(_hass(), _entry())
    seen = []
    remove = c.async_add_listener(lambda: seen.append(1))
    c._notify_outputs()
    remove()
    c._notify_outputs()
    assert seen == [1]


def test_output_event_notifies_listeners():
    from plejd_cloud.const import CMD_GROUP_STATE_AND_LEVEL
    from plejd_cloud.protocol import Command

    c = PlejdCoordinator(_hass(), _entry())
    seen = []
    remove = c.async_add_listener(lambda: seen.append(1))
    state_cmd = Command(address=5, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([1, 0, 0]))
    c._on_event(state_cmd)
    remove()
    c._on_event(state_cmd)
    assert seen == [1]


def _entry_with_room(room=None):
    entry = _entry(discovered=None)
    entry.data[CONF_ROOMS] = [
        room
        or {
            "room_id": "r1",
            "name": "Kök",
            "address": 14,
            "member_addresses": [5, 6],
            "dimmable": True,
            "dimmable_addresses": [5, 6],
        }
    ]
    return entry


async def _connected_coordinator_with_room(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry_with_room())
    await c.async_start()
    return c


async def test_group_state_event_fans_out_to_room_members(monkeypatch):
    """A room-group broadcast from outside HA (e.g. the Plejd app) must update member state too."""
    from plejd_cloud.const import CMD_GROUP_STATE_AND_LEVEL
    from plejd_cloud.protocol import Command

    c = await _connected_coordinator_with_room(monkeypatch)
    state_cmd = Command(address=14, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([1, 0, 200]))
    c._on_event(state_cmd)
    assert c.state_for(5).on is True and c.state_for(5).level == 200
    assert c.state_for(6).on is True and c.state_for(6).level == 200


async def test_group_state_event_off_preserves_member_levels(monkeypatch):
    from plejd_cloud.const import CMD_GROUP_STATE_AND_LEVEL
    from plejd_cloud.protocol import Command

    c = await _connected_coordinator_with_room(monkeypatch)
    c._on_event(Command(address=14, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([1, 0, 200])))
    c._on_event(Command(address=14, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([0, 0, 0])))
    assert c.state_for(5).on is False and c.state_for(5).level == 200


async def test_group_state_event_for_unknown_group_address_is_ignored(monkeypatch):
    from plejd_cloud.const import CMD_GROUP_STATE_AND_LEVEL
    from plejd_cloud.protocol import Command

    c = await _connected_coordinator_with_room(monkeypatch)
    c._on_event(Command(address=99, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([1, 0, 200])))
    assert c.state_for(5) is None and c.state_for(6) is None


async def test_group_state_event_with_short_payload_is_ignored(monkeypatch):
    from plejd_cloud.const import CMD_GROUP_STATE_AND_LEVEL
    from plejd_cloud.protocol import Command

    c = await _connected_coordinator_with_room(monkeypatch)
    c._on_event(Command(address=14, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([1])))
    assert c.state_for(5) is None


def test_button_event_dispatches_press_and_release():
    from plejd_cloud.const import CMD_INPUT_BUTTON
    from plejd_cloud.protocol import Command

    c = PlejdCoordinator(_hass(), _entry())
    events = []
    remove = c.async_add_button_listener(lambda addr, pressed: events.append((addr, pressed)))
    c._on_event(Command(address=11, command_type=0x10, command=CMD_INPUT_BUTTON, data=bytes([1])))
    c._on_event(Command(address=11, command_type=0x10, command=CMD_INPUT_BUTTON, data=bytes([0])))
    remove()
    c._on_event(Command(address=11, command_type=0x10, command=CMD_INPUT_BUTTON, data=b""))
    assert events == [(11, True), (11, False)]


async def test_shutdown(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_shutdown()
    assert not client.is_connected


async def test_available_reflects_connection(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    assert c.available is False  # not connected yet
    await c.async_start()
    assert c.available is True


async def test_initial_state_read_one_per_address(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    dev_none = {**_DEV, "device_id": "d2", "address": None}
    dev_dup = {**_DEV, "device_id": "d3", "address": 5}
    entry = types.SimpleNamespace(
        entry_id="e1",
        data={CONF_CRYPTO_KEY: _KEY_HEX, CONF_DEVICES: [_DEV, dev_none, dev_dup], CONF_DISCOVERED_ADDRESS: None},
    )
    c = PlejdCoordinator(hass, entry)
    await c.async_start()
    # one output-state read per unique non-None address (addr 5); None address + duplicate skipped.
    # (Other DATA reads after connect — dimmable-settings, boot-state, the NotifyEvents health
    # poll — use different opcodes and are filtered out below.)
    from plejd_cloud.const import CMD_OUTPUT_STATE_AND_LEVEL
    from plejd_cloud.protocol import TYPE_READ, decode_command

    reads = [
        cmd
        for w in client.writes
        if w[0] == PLEJD_CHAR_DATA_UUID
        for cmd in [decode_command(c._connection.mesh.decrypt(w[1]))]
        if cmd.command_type == TYPE_READ and cmd.command == CMD_OUTPUT_STATE_AND_LEVEL
    ]
    assert len(reads) == 1  # exactly one state read per unique address


async def test_read_all_states_noop_without_mesh():
    c = PlejdCoordinator(_hass(), _entry())
    await c._async_read_all_states()  # mesh is None — returns without writing


async def test_read_all_settings_noop_without_mesh():
    c = PlejdCoordinator(_hass(), _entry())
    await c._async_read_all_settings()  # mesh is None — returns without writing


async def test_settings_short_reply_ignored_for_all_commands(monkeypatch):
    """val-is-None guard returns early for every settings command on short data."""
    from plejd_cloud.const import (
        CMD_OUTPUT_BOOT_STATE,
        CMD_OUTPUT_CURVE_TYPE,
        CMD_OUTPUT_MAX_LEVEL,
        CMD_OUTPUT_PHASE_DIM_TYPE,
        CMD_OUTPUT_RELAY_OFF_TIME,
        CMD_OUTPUT_SPEED,
    )
    from plejd_cloud.protocol import Command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()

    # 1-byte payload → decode_output_level_reply returns None → max-level guard line
    c._on_event(Command(5, 0x03, CMD_OUTPUT_MAX_LEVEL, bytes([0x01])))
    # 1-byte payload → decode_output_speed_reply returns None → speed guard line
    c._on_event(Command(5, 0x03, CMD_OUTPUT_SPEED, bytes([0x01])))
    # 0-byte payload → decode_output_curve_reply returns None → curve guard line
    c._on_event(Command(5, 0x03, CMD_OUTPUT_CURVE_TYPE, b""))
    # 0-byte payload → decode_output_phase_dim_reply returns None → phase guard line
    c._on_event(Command(5, 0x03, CMD_OUTPUT_PHASE_DIM_TYPE, b""))
    # 0-byte payload → decode_output_boot_state_reply returns None → boot-state guard line
    c._on_event(Command(5, 0x03, CMD_OUTPUT_BOOT_STATE, b""))
    # 1-byte payload → decode_output_relay_off_time_reply returns None → relay-off-time guard
    c._on_event(Command(5, 0x03, CMD_OUTPUT_RELAY_OFF_TIME, bytes([0x01])))

    # None of the short replies should be stored
    assert c.settings_for(5) is None


async def test_disconnect_marks_unavailable_and_reconnects(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()

    async def _fake_sleep(_delay):
        client.is_connected = True  # the device comes back into range

    monkeypatch.setattr(coordinator_mod.asyncio, "sleep", _fake_sleep)
    client.is_connected = False
    c._connection._handle_disconnect(client)  # bleak signals the drop
    assert c.available is False
    await c._reconnect_task
    assert c.available is True


def test_spawn_prefers_hass_background_task():
    c = PlejdCoordinator(_hass(), _entry())
    captured = {}
    c.hass.async_create_background_task = lambda co, name: captured.update(coro=co, name=name) or "TASK"
    coro = c._async_reconnect()
    assert c._spawn(coro) == "TASK"
    assert captured["name"] == "plejd-reconnect"
    captured["coro"].close()  # we never scheduled it


async def test_reconnect_guard_when_already_running():
    c = PlejdCoordinator(_hass(), _entry())
    c._reconnecting = True
    await c._async_reconnect()  # returns immediately, leaves the flag as-is
    assert c._reconnecting is True


async def test_reconnect_stops_when_closed(monkeypatch):
    c = PlejdCoordinator(_hass(), _entry())  # no device in range

    async def _fake_sleep(_delay):
        c._closed = True

    monkeypatch.setattr(coordinator_mod.asyncio, "sleep", _fake_sleep)
    await c._async_reconnect()
    assert c._reconnecting is False


async def test_reconnect_backs_off_on_failure(monkeypatch):
    c = PlejdCoordinator(_hass(), _entry())  # no device → each attempt raises ConfigEntryNotReady
    calls = {"n": 0}

    async def _fake_sleep(_delay):
        calls["n"] += 1
        if calls["n"] >= 2:
            c._closed = True

    monkeypatch.setattr(coordinator_mod.asyncio, "sleep", _fake_sleep)
    await c._async_reconnect()
    assert calls["n"] == 2  # retried after the first failure, then gave up on close


async def test_reconnect_stops_and_starts_reauth_on_auth_failure(monkeypatch):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    c = PlejdCoordinator(_hass(), _entry())
    started = []
    c._entry = types.SimpleNamespace(async_start_reauth=lambda h: started.append(h))

    async def _fake_sleep(_delay):
        pass

    async def _auth_fail():
        raise ConfigEntryAuthFailed("credentials rejected")

    monkeypatch.setattr(coordinator_mod.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(c, "_async_select_and_connect", _auth_fail)
    await c._async_reconnect()
    assert started == [c.hass]  # reauth started, loop didn't retry forever
    assert c._reconnecting is False


async def test_reconnect_retries_on_unexpected_exception(monkeypatch):
    c = PlejdCoordinator(_hass(), _entry())
    calls = {"n": 0}

    async def _fake_sleep(_delay):
        pass

    async def _flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        c._closed = True  # connected: nothing left to retry

    monkeypatch.setattr(coordinator_mod.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(c, "_async_select_and_connect", _flaky)
    await c._async_reconnect()
    assert calls["n"] == 3  # two unexpected failures were retried, not fatal


async def test_a_drop_during_a_reconnect_is_not_lost(monkeypatch):
    # Seen live: the new gateway socket closed before its receive loop started. The loop's exit
    # reported the drop while the reconnect was still finishing, the drop's own reconnect bailed
    # out on the running one, and the integration stayed unavailable until HA was restarted.
    c = PlejdCoordinator(_hass(), _entry())
    links = {"up": False}
    monkeypatch.setattr(c, "_active_transport", lambda: types.SimpleNamespace(connected=links["up"]))
    attempts = []

    async def _fake_sleep(_delay):
        pass

    async def _connect():
        attempts.append(len(attempts))
        links["up"] = True
        c._available = True
        if len(attempts) == 1:  # this socket dies at once and reports it mid-reconnect
            links["up"] = False
            c._handle_disconnect()

    monkeypatch.setattr(coordinator_mod.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(c, "_async_select_and_connect", _connect)
    monkeypatch.setattr(c, "_notify_outputs", lambda: None)
    spawned = []
    monkeypatch.setattr(c, "_spawn", lambda coro: spawned.append(coro) or "TASK")
    c._available = False
    c._reconnect_task = task = asyncio.ensure_future(c._async_reconnect())
    await asyncio.gather(task)
    assert attempts == [0, 1]  # connected again rather than giving up after the first attempt
    assert c.available is True
    assert c._reconnecting is False
    # The running loop handled the drop itself, so shutdown still cancels the task that did the work.
    assert spawned == [] and c._reconnect_task is task


async def test_a_drop_with_no_reconnect_running_starts_one(monkeypatch):
    c = PlejdCoordinator(_hass(), _entry())
    monkeypatch.setattr(c, "_notify_outputs", lambda: None)
    spawned = []
    monkeypatch.setattr(c, "_spawn", lambda coro: spawned.append(coro) or "TASK")
    c._handle_disconnect()
    assert c._reconnect_task == "TASK" and len(spawned) == 1
    spawned[0].close()


async def test_shutdown_cancels_reconnect_task(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()

    async def _sleeper():
        await asyncio.sleep(100)

    c._reconnect_task = asyncio.ensure_future(_sleeper())
    task = c._reconnect_task
    await c.async_shutdown()
    assert c._reconnect_task is None and not client.is_connected
    drained = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(drained[0], asyncio.CancelledError)  # shutdown cancelled the reconnect task


class _FakeGateway:
    def __init__(self, *args, **kwargs):
        self.connected = False
        self.writes: list[bytes] = []
        self.state: dict = {}
        self.fail = False
        self.disconnected = False
        self.on_event = kwargs.get("on_event")

    async def connect(self):
        if self.fail:
            raise OSError("gw down")
        self.connected = True

    async def write(self, vector):
        self.writes.append(vector)

    def state_for(self, address):
        return self.state.get(address)

    def set_state(self, address, state):
        self.state[address] = state

    async def disconnect(self):
        self.disconnected = True
        self.connected = False


def _gateway_entry():
    return types.SimpleNamespace(
        entry_id="e1",
        data={
            CONF_CRYPTO_KEY: _KEY_HEX,
            CONF_DEVICES: [_DEV],
            CONF_DISCOVERED_ADDRESS: None,
            CONF_GATEWAYS: ["gw1"],
            CONF_RESOURCE_SET_ID: "rs1",
            CONF_INSTALLATION_ID: "inst-1",
            CONF_SITE_ID: "S1",
            CONF_EMAIL: "u@x.se",
            CONF_PASSWORD: "pw",
            CONF_DEVICE_ADDRESSES: {"d1": 5},
        },
    )


async def test_gateway_is_preferred_and_routes_commands(monkeypatch):
    from plejd_cloud.protocol import OutputState, set_group_state_and_level

    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    hass = _hass()
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    await c.async_start()
    assert c._active == "gateway" and c.available is True
    c._gateway.state = {11: OutputState(output=11, on=True, level=80)}
    assert c.state_for(11).level == 80
    await c.async_set_output(11, True, 80)
    assert c._gateway.writes[-1] == set_group_state_and_level(11, True, 80)


async def test_gateway_off_preserves_prior_level_despite_ack_landing_before_write_returns(monkeypatch):
    # Over the real gateway transport, a published ack is decoded into state_for() before
    # write()'s own await returns - reading "prior level" only after the write would already
    # see this command's own echo (off, level 0), losing the real remembered brightness.
    from plejd_cloud.protocol import OutputState

    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    hass = _hass()
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    await c.async_start()
    c._gateway.state = {11: OutputState(output=11, on=True, level=150)}

    async def _write_with_early_ack(vector):
        c._gateway.writes.append(vector)
        # Simulate the ack's own state push landing before write() returns.
        c._gateway.state[11] = OutputState(output=11, on=False, level=0)

    monkeypatch.setattr(c._gateway, "write", _write_with_early_ack)
    await c.async_set_output(11, False, 0)

    assert c.state_for(11).on is False and c.state_for(11).level == 150


async def test_gateway_set_output_does_not_overwrite_a_real_push_that_arrived_mid_write(monkeypatch):
    # A physical switch (or another app instance) can change the same output while our own
    # write is still in flight; that push already lands in state_for() (and already notified
    # listeners via _on_event) before write() returns. Our own optimistic record must not
    # then stomp that real, newer value with the one we merely intended to command.
    from plejd_cloud.protocol import OutputState

    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    hass = _hass()
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    await c.async_start()
    c._gateway.state = {11: OutputState(output=11, on=True, level=150)}

    async def _write_with_concurrent_third_party_change(vector):
        c._gateway.writes.append(vector)
        # Someone else changed this output to a value we didn't command, while we were
        # writing our own (on, 80) command.
        c._gateway.state[11] = OutputState(output=11, on=True, level=30)

    monkeypatch.setattr(c._gateway, "write", _write_with_concurrent_third_party_change)
    await c.async_set_output(11, True, 80)

    assert c.state_for(11).on is True and c.state_for(11).level == 30


async def test_gateway_set_output_still_applies_its_own_state_when_nothing_else_changed(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    hass = _hass()
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    await c.async_start()

    await c.async_set_output(11, True, 80)

    assert c.state_for(11).on is True and c.state_for(11).level == 80


async def test_gateway_set_group_output_reflects_member_state(monkeypatch):
    from plejd_cloud.protocol import set_group_state_and_level

    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    hass = _hass()
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    await c.async_start()
    await c.async_set_group_output(14, True, 90, [5, 6])
    assert c._gateway.writes[-1] == set_group_state_and_level(14, True, 90)
    assert c.state_for(5).on is True and c.state_for(5).level == 90 and c.state_for(5).output == 5
    assert c.state_for(6).on is True and c.state_for(6).level == 90 and c.state_for(6).output == 6


async def test_gateway_connect_starts_fault_polling(monkeypatch):
    from plejd_cloud.const import CMD_NOTIFY_EVENTS
    from plejd_cloud.protocol import decode_command

    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    hass = _hass()
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    await c.async_start()
    assert c._faults_unsub is not None
    reads = [decode_command(w) for w in c._gateway.writes]
    assert any(r.command == CMD_NOTIFY_EVENTS for r in reads)


async def test_gateway_pushes_route_notify_events_to_faults(monkeypatch):
    from plejd_cloud.const import CMD_NOTIFY_EVENTS
    from plejd_cloud.protocol import Command

    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    hass = _hass()
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    await c.async_start()
    # The gateway forwards every decoded mesh.out command through on_event, same as BLE.
    c._gateway.on_event(
        Command(address=5, command_type=0x03, command=CMD_NOTIFY_EVENTS, data=(0x8).to_bytes(8, "little"))
    )
    assert c.faults_for(5) == frozenset({"overtemperature"})


async def test_gateway_failure_falls_back_to_ble(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    c._gateway.fail = True
    await c.async_start()
    assert c._active == "ble" and c.available is True


async def test_gateway_auth_failure_raises_reauth(monkeypatch):
    from homeassistant.exceptions import ConfigEntryAuthFailed
    from plejd_cloud.cloud import PlejdAuthError

    class _AuthFailGateway(_FakeGateway):
        async def connect(self):
            raise PlejdAuthError("bad creds")

    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _AuthFailGateway)
    # BLE device is in range, but auth failure must NOT silently fall back to it.
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    with pytest.raises(ConfigEntryAuthFailed):
        await c.async_start()
    assert c._active is None  # did not connect over BLE


async def test_transport_force_ble_skips_gateway(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    hass.session = object()
    entry = _gateway_entry()
    entry.options = {CONF_TRANSPORT: "ble"}
    c = PlejdCoordinator(hass, entry)
    await c.async_start()
    assert c._active == "ble" and c._gateway.connected is False  # gateway present but unused


async def test_transport_force_gateway(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    hass = _hass()
    hass.session = object()
    entry = _gateway_entry()
    entry.options = {CONF_TRANSPORT: "gateway"}
    c = PlejdCoordinator(hass, entry)
    await c.async_start()
    assert c._active == "gateway"


async def test_transport_force_gateway_without_gateway_raises():
    from homeassistant.exceptions import ConfigEntryNotReady

    entry = _entry()
    entry.options = {CONF_TRANSPORT: "gateway"}
    c = PlejdCoordinator(_hass(), entry)
    with pytest.raises(ConfigEntryNotReady, match="no gateway"):
        await c.async_start()


async def test_active_transport_reflects_connection(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    hass = _hass()
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    assert c.active_transport is None  # not connected yet
    await c.async_start()
    assert c.active_transport == "gateway"


async def test_gateway_get_token(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)

    async def _login(session, email, password):
        return "TOKEN"

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    hass = _hass()
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    assert await c._async_get_token() == "TOKEN"


async def test_shutdown_disconnects_gateway(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "PlejdGatewayConnection", _FakeGateway)
    hass = _hass()
    hass.session = object()
    c = PlejdCoordinator(hass, _gateway_entry())
    await c.async_start()
    await c.async_shutdown()
    assert c._gateway.disconnected is True


async def test_set_climate_commands(monkeypatch):
    from plejd_cloud.const import CMD_TRM_MODE, CMD_TRM_SETPOINT
    from plejd_cloud.protocol import decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_climate_setpoint(9, 22.0)
    assert decode_command(c._connection.mesh.decrypt(client.writes[-1][1])).command == CMD_TRM_SETPOINT
    await c.async_set_climate_mode(9, 3)
    assert decode_command(c._connection.mesh.decrypt(client.writes[-1][1])).command == CMD_TRM_MODE


def test_motion_event_dispatches_to_listeners():
    from plejd_cloud.const import CMD_OUTPUT_SET
    from plejd_cloud.protocol import Command

    # entry with a motion sensor at address 33
    entry = types.SimpleNamespace(
        entry_id="e1",
        data={
            CONF_CRYPTO_KEY: _KEY_HEX,
            CONF_DEVICES: [_DEV],
            CONF_DISCOVERED_ADDRESS: None,
            "motion": [{"device_id": "w1", "name": "Motion", "address": 33}],
        },
    )
    c = PlejdCoordinator(_hass(), entry)
    events = []
    remove = c.async_add_motion_listener(events.append)
    # a real WMS motion broadcast (Source=Motion + Lux=2)
    c._on_event(Command(33, 0x10, CMD_OUTPUT_SET, bytes.fromhex("03031f0700b00f08460602")))
    c._on_event(Command(99, 0x10, CMD_OUTPUT_SET, b"\x03\x03"))  # not a motion address -> ignored
    remove()
    assert len(events) == 1 and events[0].motion is True and events[0].lux == 2


async def test_cover_commands(monkeypatch):
    from plejd_cloud.const import CMD_OUTPUT_SET
    from plejd_cloud.protocol import decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_cover_position(7, 100)
    pos = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert pos.command == CMD_OUTPUT_SET and pos.data[:2] == bytes([0x03, 0x08])
    await c.async_cover_stop(7)
    stop = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert stop.data == bytes([0x03, 0x08, 0x07, 0x00])


async def test_dim_level_settings(monkeypatch):
    from plejd_cloud.const import (
        CMD_OUTPUT_MAX_LEVEL,
        CMD_OUTPUT_MIN_LEVEL,
        CMD_OUTPUT_SPEED,
        CMD_OUTPUT_START_LEVEL,
    )
    from plejd_cloud.protocol import decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_output_min_level(9, 1, 0.0)
    low = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert low.command == CMD_OUTPUT_MIN_LEVEL and low.data == bytes([1, 0x00, 0x00])
    await c.async_set_output_max_level(9, 1, 1.0)
    high = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert high.command == CMD_OUTPUT_MAX_LEVEL and high.data == bytes([1, 0xFF, 0xFF])
    await c.async_set_output_speed(9, 1, 1.0)
    speed = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert speed.command == CMD_OUTPUT_SPEED and speed.data == bytes([1, 0x8F, 0x82])
    await c.async_set_output_start_level(9, 1, 0.5)
    start = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert start.command == CMD_OUTPUT_START_LEVEL and start.data == bytes([1, 0x00, 0x80])
    # Local writes update the settings cache immediately, without waiting for a BLE read-back.
    settings = c.settings_for(9)
    assert settings.min_level == 0.0
    assert settings.max_level == 100.0
    assert settings.speed == 1.0


def _expected_clock_bytes():
    from homeassistant.util import dt as dt_util

    now = dt_util.now()
    epoch = int(now.timestamp() + now.utcoffset().total_seconds())
    return bytes([epoch & 0xFF, (epoch >> 8) & 0xFF, (epoch >> 16) & 0xFF, (epoch >> 24) & 0xFF, 0x00])


async def test_clock_synced_on_connect(monkeypatch):
    from plejd_cloud.const import CMD_SYSTEM_TIME
    from plejd_cloud.protocol import decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    clock = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert clock.address == 0 and clock.command == CMD_SYSTEM_TIME
    assert clock.data == _expected_clock_bytes()


async def test_periodic_sync_and_shutdown_cancels(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    cancelled = []
    c._clock_unsub = lambda: cancelled.append(True)
    before = len(client.writes)
    await c._async_periodic_clock_sync(None)
    assert len(client.writes) == before + 1  # periodic tick re-broadcasts the clock
    await c.async_shutdown()
    assert cancelled == [True] and c._clock_unsub is None


async def test_program_and_remove_time_event(monkeypatch):
    from plejd_cloud.const import CMD_TIME_EVENT_SCENE, CMD_TIME_EVENT_TIME, CMD_TIME_EVENT_TYPE
    from plejd_cloud.protocol import decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    start = len(client.writes)
    await c.async_program_time_event(2, 0x7F, 6, 0, 0, 4, 0)
    cmds = [decode_command(c._connection.mesh.decrypt(w[1])) for w in client.writes[start:]]
    assert [x.command for x in cmds] == [CMD_TIME_EVENT_TIME, CMD_TIME_EVENT_TYPE, CMD_TIME_EVENT_SCENE]
    assert cmds[2].data == bytes([2, 1, 4])
    await c.async_remove_time_event(2)
    rm = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert rm.command == CMD_TIME_EVENT_TIME and rm.data == bytes([2])


async def test_clock_sync_failures_are_logged_not_raised(monkeypatch):
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))

    async def _boom():
        raise RuntimeError("mesh dropped")

    monkeypatch.setattr(c, "async_sync_clock", _boom)
    await c.async_start()  # on-connect sync raises -> warning, setup still succeeds
    assert c._connection.mesh is not None
    await c._async_periodic_clock_sync(None)  # periodic sync raises -> warning, no exception


async def test_dimmer_tuning_settings(monkeypatch):
    from plejd_cloud.const import CMD_OUTPUT_CURVE_TYPE, CMD_OUTPUT_PHASE_DIM_TYPE
    from plejd_cloud.protocol import decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_output_curve(9, 1, 3)
    cv = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert cv.command == CMD_OUTPUT_CURVE_TYPE and cv.data == bytes([1, 3])
    await c.async_set_output_phase_dim(9, 1, 1)
    ph = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert ph.command == CMD_OUTPUT_PHASE_DIM_TYPE and ph.data == bytes([1, 1])
    settings = c.settings_for(9)
    assert settings.curve == 3
    assert settings.phase_dim == 1


async def test_local_write_cache_survives_unrelated_ble_notification(monkeypatch):
    """A stale BLE reply for a different field must not clobber a just-written setting."""
    from plejd_cloud.const import CMD_OUTPUT_MIN_LEVEL
    from plejd_cloud.protocol import Command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    await c.async_set_output_max_level(9, 1, 0.5)
    assert c.settings_for(9).max_level == 50.0
    # An unrelated settings read-back arrives for the same address (e.g. min level).
    c._on_event(Command(9, 0x03, CMD_OUTPUT_MIN_LEVEL, bytes([0x00, 0x00])))
    assert c.settings_for(9).max_level == 50.0  # untouched by the unrelated reply


def test_settings_for_returns_none_before_connect():
    c = PlejdCoordinator(_hass(), _entry())
    assert c.settings_for(5) is None


async def test_settings_read_on_ble_connect(monkeypatch):
    """Connect issues READ requests for each dimmable output's settings."""
    from plejd_cloud.const import CMD_OUTPUT_CURVE_TYPE, CMD_OUTPUT_MAX_LEVEL, CMD_OUTPUT_MIN_LEVEL, CMD_OUTPUT_SPEED
    from plejd_cloud.protocol import TYPE_READ, decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()

    # Only DATA characteristic writes are valid commands; filter and ignore decode errors.
    def _try_decode(payload):
        try:
            return decode_command(c._connection.mesh.decrypt(payload))
        except ValueError:
            return None

    cmds = [_try_decode(w[1]) for w in client.writes if w[0] == PLEJD_CHAR_DATA_UUID]
    read_cmds = [cmd for cmd in cmds if cmd is not None and cmd.command_type == TYPE_READ]
    # Expect state read + 5 settings reads (min, max, speed, curve, phase) for the dimmable device.
    settings_codes = {cmd.command for cmd in read_cmds}
    assert CMD_OUTPUT_MIN_LEVEL in settings_codes
    assert CMD_OUTPUT_MAX_LEVEL in settings_codes
    assert CMD_OUTPUT_SPEED in settings_codes
    assert CMD_OUTPUT_CURVE_TYPE in settings_codes


async def test_settings_stored_on_event_and_listener_notified(monkeypatch):
    """_on_event for a settings reply stores the value and fires listeners."""
    from plejd_cloud.const import CMD_OUTPUT_MIN_LEVEL
    from plejd_cloud.protocol import Command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    seen = []
    c.async_add_listener(lambda: seen.append(1))
    # Simulate a min-level reply for address 5: [0xFF, 0xFF] = 100%.
    # command_type=0x03 (TYPE_READ|TYPE_ACK) — replies carry the Ack bit set (docs/protocol.md).
    c._on_event(Command(address=5, command_type=0x03, command=CMD_OUTPUT_MIN_LEVEL, data=bytes([0xFF, 0xFF])))
    assert c.settings_for(5) is not None
    assert c.settings_for(5).min_level == 100.0
    assert seen == [1]


async def test_settings_ignores_write_echo_not_a_read_reply(monkeypatch):
    """A write (TYPE_DONT_RESPOND) echoed back on the same feed must not be decoded as a
    reply — it carries [output, value...], not the reply's value-only bytes, and would
    corrupt the cache with a bogus value."""
    from plejd_cloud.const import CMD_OUTPUT_MIN_LEVEL
    from plejd_cloud.protocol import TYPE_DONT_RESPOND, Command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    # Our own set_output_min_level(addr, output=1, 0.0) write payload: [output, lo, hi].
    c._on_event(Command(address=5, command_type=TYPE_DONT_RESPOND, command=CMD_OUTPUT_MIN_LEVEL, data=bytes([1, 0, 0])))
    assert c.settings_for(5) is None  # not cached as if it were a read reply


async def test_settings_all_commands_stored(monkeypatch):
    """All settings command codes are stored in _output_settings."""
    from plejd_cloud.const import (
        CMD_OUTPUT_BOOT_STATE,
        CMD_OUTPUT_CURVE_TYPE,
        CMD_OUTPUT_INRUSH_CURRENT,
        CMD_OUTPUT_MAX_LEVEL,
        CMD_OUTPUT_MIN_LEVEL,
        CMD_OUTPUT_PHASE_DIM_TYPE,
        CMD_OUTPUT_RELAY_CONFIG,
        CMD_OUTPUT_RELAY_OFF_TIME,
        CMD_OUTPUT_SPEED,
    )
    from plejd_cloud.protocol import Command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()

    # min: 50% = 0x7FFF
    c._on_event(Command(5, 0x03, CMD_OUTPUT_MIN_LEVEL, bytes([0xFF, 0x7F])))
    # max: 100%
    c._on_event(Command(5, 0x03, CMD_OUTPUT_MAX_LEVEL, bytes([0xFF, 0xFF])))
    # speed: instant
    c._on_event(Command(5, 0x03, CMD_OUTPUT_SPEED, bytes([0xFF, 0xFF])))
    # curve: logarithmic (1)
    c._on_event(Command(5, 0x03, CMD_OUTPUT_CURVE_TYPE, bytes([1])))
    # phase: leading_edge (1)
    c._on_event(Command(5, 0x03, CMD_OUTPUT_PHASE_DIM_TYPE, bytes([1])))
    # boot state: use_last (1-byte reply)
    c._on_event(Command(5, 0x03, CMD_OUTPUT_BOOT_STATE, bytes([0x00])))
    # relay off time: 2s = 200 centiseconds = [0xC8, 0x00]
    c._on_event(Command(5, 0x03, CMD_OUTPUT_RELAY_OFF_TIME, bytes([0xC8, 0x00])))
    # relay pole config: one_pole (wire byte 1)
    c._on_event(Command(5, 0x03, CMD_OUTPUT_RELAY_CONFIG, bytes([1])))
    # inrush current: 500ms = 50 centiseconds
    c._on_event(Command(5, 0x03, CMD_OUTPUT_INRUSH_CURRENT, bytes([50, 0])))

    s = c.settings_for(5)
    assert s.max_level == 100.0
    assert s.speed == 0.0
    assert s.curve == 1
    assert s.phase_dim == 1
    assert s.boot_state is True  # 1-byte reply → use_last
    assert s.relay_off_time == 2.0
    assert s.relay_pole_config == 1
    assert s.inrush_current_ms == 500


async def test_settings_short_reply_ignored(monkeypatch):
    """A too-short settings reply is silently dropped."""
    from plejd_cloud.const import CMD_OUTPUT_MIN_LEVEL
    from plejd_cloud.protocol import Command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    c._on_event(Command(5, 0x03, CMD_OUTPUT_MIN_LEVEL, bytes([0x01])))  # only 1 byte, too short
    assert c.settings_for(5) is None  # not stored


def test_settings_from_cloud_with_values():
    from plejd_cloud.coordinator import _settings_from_cloud

    s = _settings_from_cloud({"minDim": 32767, "maxDim": 65535, "dimCurve": 1})
    assert s is not None
    assert s.curve == 1
    assert s.min_level == pytest.approx(50.0, abs=0.1)
    assert s.max_level == 100.0


def test_settings_from_cloud_empty_returns_none():
    from plejd_cloud.coordinator import _settings_from_cloud

    assert _settings_from_cloud({}) is None


def test_coordinator_pre_populates_from_cloud_output_settings():
    """Cloud outputSettings pre-populate _output_settings on init."""
    dev = {**_DEV, "output_settings": {"minDim": 32767, "maxDim": 65535, "dimCurve": 1}}
    entry = types.SimpleNamespace(
        entry_id="e1",
        data={CONF_CRYPTO_KEY: _KEY_HEX, CONF_DEVICES: [dev], CONF_DISCOVERED_ADDRESS: None},
    )
    c = PlejdCoordinator(_hass(), entry)
    s = c.settings_for(_DEV["address"])
    assert s is not None
    assert s.curve == 1


async def test_relay_off_time_read_on_ble_connect(monkeypatch):
    """Devices in RELAY_HARDWARE get a relay-off-time READ issued during connect."""
    from plejd_cloud.const import CMD_OUTPUT_RELAY_OFF_TIME
    from plejd_cloud.protocol import TYPE_READ, decode_command

    relay_dev = {**_DEV, "hardware_id": 3, "model": "CTR-01", "dimmable": False}
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    entry = types.SimpleNamespace(
        entry_id="e1",
        data={CONF_CRYPTO_KEY: _KEY_HEX, CONF_DEVICES: [relay_dev], CONF_DISCOVERED_ADDRESS: None},
    )
    c = PlejdCoordinator(hass, entry)
    await c.async_start()

    cmds = [decode_command(c._connection.mesh.decrypt(w[1])) for w in client.writes if w[0] == PLEJD_CHAR_DATA_UUID]
    read_cmds = {cmd.command for cmd in cmds if cmd.command_type == TYPE_READ}
    assert CMD_OUTPUT_RELAY_OFF_TIME in read_cmds


async def test_boot_state_and_relay_off_time_setters(monkeypatch):
    """async_set_output_boot_state and async_set_output_relay_off_time write correct vectors."""
    from plejd_cloud.const import CMD_OUTPUT_BOOT_STATE, CMD_OUTPUT_RELAY_OFF_TIME
    from plejd_cloud.protocol import decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()

    await c.async_set_output_boot_state(9, 0, True)
    bs = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert bs.command == CMD_OUTPUT_BOOT_STATE and bs.data == bytes([0x00])

    await c.async_set_output_relay_off_time(9, 0, 2.0)
    rt = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert rt.command == CMD_OUTPUT_RELAY_OFF_TIME and rt.data == bytes([0x00, 0xC8, 0x00])

    settings = c.settings_for(9)
    assert settings.boot_state is True
    assert settings.relay_off_time == 2.0


async def test_relay_config_and_inrush_setters(monkeypatch):
    """async_set_output_relay_config and async_set_output_inrush_current write correct vectors."""
    from plejd_cloud.const import CMD_OUTPUT_INRUSH_CURRENT, CMD_OUTPUT_RELAY_CONFIG
    from plejd_cloud.protocol import decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()

    await c.async_set_output_relay_config(9, 0, 1)  # one_pole
    rc = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert rc.command == CMD_OUTPUT_RELAY_CONFIG and rc.data == bytes([0x00, 0x01])

    await c.async_set_output_inrush_current(9, 0, 500)  # 500ms = 50 cs
    ic = decode_command(c._connection.mesh.decrypt(client.writes[-1][1]))
    assert ic.command == CMD_OUTPUT_INRUSH_CURRENT and ic.data == bytes([0x00, 50, 0x00])

    settings = c.settings_for(9)
    assert settings.relay_pole_config == 1
    assert settings.inrush_current_ms == 500


async def test_relay_config_hardware_read_during_connect(monkeypatch):
    """Devices in RELAY_CONFIG_HARDWARE get a relay-config READ issued during connect."""
    from plejd_cloud.const import CMD_OUTPUT_RELAY_CONFIG
    from plejd_cloud.protocol import TYPE_READ, decode_command

    relay_config_dev = {**_DEV, "hardware_id": 11, "model": "DIM-01-2P"}
    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    entry = types.SimpleNamespace(
        entry_id="e1",
        data={CONF_CRYPTO_KEY: _KEY_HEX, CONF_DEVICES: [relay_config_dev], CONF_DISCOVERED_ADDRESS: None},
    )
    c = PlejdCoordinator(hass, entry)
    await c.async_start()

    cmds = [decode_command(c._connection.mesh.decrypt(w[1])) for w in client.writes if w[0] == PLEJD_CHAR_DATA_UUID]
    read_cmds = {cmd.command for cmd in cmds if cmd.command_type == TYPE_READ}
    assert CMD_OUTPUT_RELAY_CONFIG in read_cmds


async def test_relay_config_short_reply_ignored(monkeypatch):
    """An empty relay config reply is silently dropped."""
    from plejd_cloud.const import CMD_OUTPUT_RELAY_CONFIG
    from plejd_cloud.protocol import Command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    c._on_event(Command(5, 0x03, CMD_OUTPUT_RELAY_CONFIG, b""))
    assert c.settings_for(5) is None


async def test_inrush_current_short_reply_ignored(monkeypatch):
    """A too-short inrush current reply is silently dropped."""
    from plejd_cloud.const import CMD_OUTPUT_INRUSH_CURRENT
    from plejd_cloud.protocol import Command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    c = PlejdCoordinator(hass, _entry(discovered=None))
    await c.async_start()
    c._on_event(Command(5, 0x03, CMD_OUTPUT_INRUSH_CURRENT, bytes([50])))  # only 1 byte
    assert c.settings_for(5) is None


def test_fault_event_routes_to_listeners():
    from plejd_cloud.const import CMD_NOTIFY_EVENTS
    from plejd_cloud.protocol import Command

    c = PlejdCoordinator(_hass(), _entry())
    seen = []
    remove = c.async_add_fault_listener(lambda addr, faults: seen.append((addr, faults)))
    c._on_event(Command(address=5, command_type=0x03, command=CMD_NOTIFY_EVENTS, data=(0x8).to_bytes(8, "little")))
    assert c.faults_for(5) == frozenset({"overtemperature"})
    assert seen == [(5, frozenset({"overtemperature"}))]
    remove()
    assert c.faults_for(99) == frozenset()  # unknown address -> empty


# --- cloud poll tests ---


def _cloud_poll_entry():
    """Entry with all site-derived fields populated, used for cloud-poll tests."""
    return types.SimpleNamespace(
        entry_id="e1",
        options={},
        data={
            CONF_CRYPTO_KEY: _KEY_HEX,
            CONF_DEVICES: [_DEV],
            CONF_DISCOVERED_ADDRESS: None,
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_SITE_ID: "S1",
            CONF_INPUTS: [],
            CONF_MOTION: [],
            CONF_SCENES: [],
            CONF_ROOMS: [],
            CONF_ROOM_NAMES: {},
            CONF_GATEWAYS: [],
            CONF_RESOURCE_SET_ID: None,
            CONF_DEVICE_ADDRESSES: {},
        },
    )


def _fake_site(
    devices=None, gateways=None, resource_set_id=None, device_addresses=None, rooms=None, motion=None, malformed=None
):
    """A PlejdCloudSite-like object matching _DEV by default (no change)."""
    from plejd_cloud.cloud import PlejdCloudSite

    return PlejdCloudSite(
        site_id="S1",
        title="Villa",
        crypto_key=bytes.fromhex(_KEY_HEX),
        mesh_key="AA-BB-CC-DD",
        devices=devices
        if devices is not None
        else [__import__("plejd_cloud.cloud", fromlist=["PlejdCloudDevice"]).PlejdCloudDevice(**_DEV)],
        inputs=[],
        motion=motion or [],
        scenes=[],
        gateways=gateways or [],
        resource_set_id=resource_set_id,
        device_addresses=device_addresses or {},
        rooms=rooms or [],
        malformed=frozenset(malformed or ()),
    )


async def test_cloud_poll_no_change_does_nothing(monkeypatch):
    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site()

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    updated = {}
    reloaded = []
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: updated.update(data),
        async_reload=lambda eid: reloaded.append(eid),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)
    assert not reloaded  # no difference → no reload


async def test_cloud_poll_device_added_reloads(monkeypatch):
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    updated = {}
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: updated.update(data),
        async_reload=AsyncMock(return_value=True),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)
    assert len(updated[CONF_DEVICES]) == 2
    config_entries.async_reload.assert_awaited_once_with("e1")


async def test_cloud_poll_reverts_and_logs_when_reload_is_rejected(monkeypatch, caplog):
    # If the reload itself fails (e.g. a platform refused to unload), leaving entry.data
    # already matching the fresh site would make every later poll's comparison find no
    # difference and never retry, stranding the running coordinator (which never actually
    # got the new data live) stale indefinitely. Revert instead, so the next poll retries.
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})
    original_devices = list(_cloud_poll_entry().data[CONF_DEVICES])

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(return_value=False),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)  # must not raise
    assert "reload after a site change failed" in caplog.text
    assert entry.data[CONF_DEVICES] == original_devices


async def test_cloud_poll_reverts_and_logs_when_reload_raises(monkeypatch, caplog):
    # async_reload() can raise instead of just returning False - must be treated the same
    # as a rejected reload (revert + log), not let the exception skip the revert entirely.
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})
    original_devices = list(_cloud_poll_entry().data[CONF_DEVICES])

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(side_effect=RuntimeError("boom")),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)  # must not raise
    assert "reload after a site change raised" in caplog.text
    assert entry.data[CONF_DEVICES] == original_devices


@pytest.mark.parametrize("malformed_label", ["devices", "inputs", "motion", "scenes", "rooms", "gateways"])
async def test_cloud_poll_skips_a_malformed_site_response(monkeypatch, caplog, malformed_label):
    # parse_site() flags a collection as malformed when its raw source field was missing or
    # the wrong type - that response must be skipped like a missed poll (not "synced"
    # destructively), regardless of which collection was affected or whether the resulting
    # parsed list happens to be empty or not.
    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(malformed=[malformed_label])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: pytest.fail("must not persist a malformed snapshot"),
        async_reload=AsyncMock(),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)  # must not raise
    assert "site response is malformed" in caplog.text
    assert malformed_label in caplog.text
    config_entries.async_reload.assert_not_awaited()


async def test_cloud_poll_skips_a_wrong_typed_response_without_raising(monkeypatch, caplog):
    # End-to-end over the real parse_site (not _fake_site): a wrong-typed, non-empty
    # collection must reach the poll as a flagged site and be skipped, not blow up mid-parse
    # with an AttributeError the poll's narrow except clause would let escape.
    from plejd_cloud.cloud import parse_site

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return parse_site(
            {
                "siteId": "S1",
                "plejdMesh": {"cryptoKey": _KEY_HEX},
                "devices": {"d1": {"deviceId": "d1"}},  # object instead of list
            }
        )

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: pytest.fail("must not persist a malformed snapshot"),
        async_reload=AsyncMock(),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)  # must not raise

    assert "site response is malformed" in caplog.text
    config_entries.async_reload.assert_not_awaited()


async def test_cloud_poll_syncs_a_genuinely_empty_but_well_formed_collection(monkeypatch):
    # A well-formed response reporting zero scenes (e.g. the user deleted their last one)
    # must NOT be treated as suspicious/malformed - unlike the emptiness-based heuristic
    # this replaced, a real deletion has to sync like any other site change, or it would be
    # silently blocked forever (every later poll's diff would keep finding "still empty").
    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[])  # malformed defaults to empty - this is well-formed

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()  # cached CONF_DEVICES: [_DEV] (non-empty)
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(return_value=True),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    assert entry.data[CONF_DEVICES] == []
    config_entries.async_reload.assert_awaited_once_with("e1")


async def test_cloud_poll_resets_forced_transport_when_gateway_disappears(monkeypatch):
    # Mirrors manage_device.py's own device-removal refresh: a forced TRANSPORT_GATEWAY
    # preference must be dropped when the gateway disappears, or the coordinator gets stuck
    # raising ConfigEntryNotReady forever instead of falling back to BLE.
    from plejd_cloud.const import TRANSPORT_AUTO, TRANSPORT_GATEWAY

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(gateways=[])  # gateway removed in the app

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    entry.data[CONF_GATEWAYS] = ["gw1"]
    entry.data[CONF_RESOURCE_SET_ID] = "rs1"
    entry.data[CONF_INSTALLATION_ID] = "inst1"
    entry.options = {CONF_TRANSPORT: TRANSPORT_GATEWAY}
    updated_options = {}
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: updated_options.update(options or {}),
        async_reload=AsyncMock(return_value=True),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)
    assert updated_options[CONF_TRANSPORT] == TRANSPORT_AUTO


async def test_cloud_poll_merges_options_read_after_acquiring_the_lock(monkeypatch):
    # Another operation (e.g. a schedule save) can hold the reload lock while this poll
    # waits, persisting its own options change. The poll must merge onto entry.options as
    # they are AFTER acquiring the lock, or it writes back a pre-lock copy and permanently
    # discards that change.
    from plejd_cloud import schedule_ws
    from plejd_cloud.cloud import PlejdCloudDevice
    from plejd_cloud.const import TRANSPORT_AUTO

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    entry.options = {CONF_TRANSPORT: TRANSPORT_AUTO}
    hass = _hass()
    hass.session = object()
    persisted: dict = {}
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: persisted.update({"options": options}),
        async_reload=AsyncMock(return_value=True),
    )
    c = PlejdCoordinator(hass, entry)

    # hold the lock, land a concurrent options change, then let the poll through
    lock = schedule_ws.async_get_reload_lock(hass, entry.entry_id)
    await lock.acquire()
    poll = asyncio.ensure_future(c._async_poll_cloud(None))
    await asyncio.sleep(0)  # let the poll reach the lock and block there
    entry.options = {**entry.options, "schedules": [{"id": 1}]}  # the other operation's save
    lock.release()
    await asyncio.gather(poll)

    assert persisted["options"]["schedules"] == [{"id": 1}]  # not discarded
    assert persisted["options"][CONF_TRANSPORT] == TRANSPORT_AUTO


async def test_cloud_poll_skips_when_a_management_operation_landed_while_it_waited(monkeypatch):
    # A device/room/scene operation completing between this poll's fetch and its lock
    # acquisition writes a NEWER snapshot of the same keys. Applying the poll's older overlay
    # would revert what the user just did, so the poll skips and lets the next one re-fetch.
    from plejd_cloud import schedule_ws
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    persisted: list = []
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: persisted.append(data),
        async_reload=AsyncMock(return_value=True),
    )
    c = PlejdCoordinator(hass, entry)

    lock = schedule_ws.async_get_reload_lock(hass, entry.entry_id)
    await lock.acquire()
    poll = asyncio.ensure_future(c._async_poll_cloud(None))
    await asyncio.sleep(0)  # let the poll reach the lock and block there
    # the management operation's own newer result for a site-derived key
    entry.data = {**entry.data, CONF_DEVICES: [{**_DEV, "name": "Renamed by the user"}]}
    lock.release()
    await asyncio.gather(poll)

    assert persisted == []  # nothing written
    assert entry.data[CONF_DEVICES][0]["name"] == "Renamed by the user"  # their change stands
    hass.config_entries.async_reload.assert_not_awaited()


async def test_cloud_poll_does_not_roll_back_when_the_follow_up_reload_succeeded(monkeypatch):
    # The follow-up reload loads the entry with this poll's data already written, so a
    # success there means the sync IS live - rolling back on the first reload's failure would
    # undo a working state.
    from plejd_cloud import schedule_ws
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    calls: list[str] = []

    async def _reload(entry_id):
        calls.append(entry_id)
        if len(calls) == 1:
            hass.data[schedule_ws.DATA_RELOAD_PENDING] = entry_id
            return False  # our own reload is rejected...
        return True  # ...but the follow-up for the concurrent change succeeds

    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(side_effect=_reload),
    )
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    assert calls == ["e1", "e1"]  # ours, then the follow-up - and no third rollback reload
    assert [d["device_id"] for d in entry.data[CONF_DEVICES]] == ["d1", "d2"]  # sync kept


async def test_cloud_poll_reloads_the_reverted_snapshot_after_a_failed_setup(monkeypatch):
    # If async_reload unloaded the old entry but failed to set the new one up, this
    # coordinator (and its poll timer) is already gone - reverting the stored snapshot alone
    # would leave nothing loaded and nothing scheduled to ever retry. The revert must be
    # followed by a reload that brings the entry back on the known-good data.
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    original_devices = list(entry.data[CONF_DEVICES])
    hass = _hass()
    hass.session = object()
    reload_results = [False, True]  # the site-change reload fails, the revert's reload works
    data_at_reload: list = []

    async def _reload(entry_id):
        data_at_reload.append(list(entry.data[CONF_DEVICES]))
        return reload_results.pop(0)

    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(side_effect=_reload),
    )
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    assert len(data_at_reload) == 2  # the failed one, then the revert's own
    assert data_at_reload[1] == original_devices  # reloaded on the reverted (known-good) data
    assert entry.data[CONF_DEVICES] == original_devices


async def test_cloud_poll_revert_keeps_a_concurrent_change(monkeypatch):
    # The revert restores only the keys this poll wrote, onto entry.data as it is now - a
    # wholesale write-back of the pre-reload snapshot would also undo an unrelated change
    # that landed while the (failed) reload was in flight.
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    original_devices = list(entry.data[CONF_DEVICES])
    hass = _hass()
    hass.session = object()
    reload_results = [False, True]

    async def _reload(entry_id):
        if len(reload_results) == 2:  # during the failing site-change reload
            entry.data = {**entry.data, "pending_room_moves": {"d9": {"room_id": "r2"}}}
        return reload_results.pop(0)

    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(side_effect=_reload),
    )
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    assert entry.data[CONF_DEVICES] == original_devices  # this poll's own write reverted
    assert entry.data["pending_room_moves"] == {"d9": {"room_id": "r2"}}  # the other change kept


async def test_cloud_poll_revert_drops_a_key_it_introduced_and_restores_the_old_transport(monkeypatch):
    # A gateway newly appearing seeds CONF_INSTALLATION_ID, a key that did not exist before
    # this poll - reverting must remove it rather than leave a None behind, and must put back
    # the transport option's previous value (not just delete it).
    from plejd_cloud.const import TRANSPORT_AUTO

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(gateways=["gw1"], resource_set_id="rs1")  # gateway appears

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    entry.options = {CONF_TRANSPORT: TRANSPORT_AUTO}  # present, so the revert restores it
    assert CONF_INSTALLATION_ID not in entry.data
    hass = _hass()
    hass.session = object()
    reload_results = [False, True]  # the site-change reload fails, then the revert's reload
    persisted: dict = {}

    async def _reload(entry_id):
        return reload_results.pop(0)

    def _update(e, data, options=None):
        e.data = data
        e.options = options if options is not None else e.options
        persisted.update({"data": data, "options": e.options})

    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=_update,
        async_reload=AsyncMock(side_effect=_reload),
    )
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    assert CONF_INSTALLATION_ID not in entry.data  # the seeded key is gone again
    assert entry.options[CONF_TRANSPORT] == TRANSPORT_AUTO  # restored, not dropped
    assert entry.data[CONF_GATEWAYS] == []  # back to the pre-poll value


async def test_cloud_poll_skips_when_shut_down_while_waiting_for_the_lock(monkeypatch):
    # Waiting for the lock is an await point, so the holder's reload (or an independent
    # unload) can shut this coordinator down meanwhile - the interval unsubscribe cannot stop
    # an already-running callback. Writing then would reload the entry after our own teardown.
    from plejd_cloud import schedule_ws
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    persisted: list = []
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: persisted.append(data),
        async_reload=AsyncMock(return_value=True),
    )
    c = PlejdCoordinator(hass, entry)

    lock = schedule_ws.async_get_reload_lock(hass, entry.entry_id)
    await lock.acquire()
    poll = asyncio.ensure_future(c._async_poll_cloud(None))
    await asyncio.sleep(0)  # let the poll reach the lock and block there
    c._closed = True  # the lock holder's reload tore this coordinator down
    lock.release()
    await asyncio.gather(poll)

    assert persisted == []  # nothing written after our own shutdown
    hass.config_entries.async_reload.assert_not_awaited()


async def test_cloud_poll_revert_removes_a_transport_option_it_introduced(monkeypatch):
    # The entry had no CONF_TRANSPORT at all; this poll wrote one. Reverting must remove it
    # again rather than leave a value the user never chose.
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    entry.options = {}  # no transport preference stored at all
    hass = _hass()
    hass.session = object()
    reload_results = [False, True]

    def _update(e, data, options=None):
        e.data = data
        if options is not None:
            e.options = options

    async def _reload(entry_id):
        return reload_results.pop(0)

    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=_update,
        async_reload=AsyncMock(side_effect=_reload),
    )
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    assert CONF_TRANSPORT not in entry.options  # removed again, not left behind


async def test_cloud_poll_revert_leaves_a_newer_edit_alone(monkeypatch):
    # Reconfigure and the options flow do not take this integration-specific lock, so a newer
    # edit to a site-derived key can land after this poll wrote it. The rollback must restore
    # only keys whose current value is still what this poll wrote.
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    reload_results = [False, True]
    reconfigured = [{**_DEV, "name": "Set by reconfigure"}]

    async def _reload(entry_id):
        if len(reload_results) == 2:
            # a reconfigure lands on the same key while our reload is failing
            entry.data = {**entry.data, CONF_DEVICES: reconfigured}
        return reload_results.pop(0)

    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(side_effect=_reload),
    )
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    assert entry.data[CONF_DEVICES] == reconfigured  # the newer edit survives the rollback


async def test_cloud_poll_discards_the_cached_grant_when_the_gateway_is_replaced(monkeypatch):
    # A cached resourceSetId belongs to the gateway it was issued for. Copying it onto a
    # replacement gateway would keep has_gateway true and rebuild the connection with an
    # obsolete grant forever, since every later poll would see the same cached value.
    from plejd_cloud.const import TRANSPORT_AUTO, TRANSPORT_GATEWAY

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(gateways=["gw-new"], resource_set_id=None)  # swapped, no grant yet

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    entry.data[CONF_GATEWAYS] = ["gw-old"]
    entry.data[CONF_RESOURCE_SET_ID] = "rs-old"
    entry.data[CONF_INSTALLATION_ID] = "inst1"
    entry.options = {CONF_TRANSPORT: TRANSPORT_GATEWAY}
    persisted: dict = {}

    def _update(e, data, options=None):
        e.data = data
        e.options = options if options is not None else e.options
        persisted.update({"data": data, "options": e.options})

    hass = _hass()
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=_update,
        async_reload=AsyncMock(return_value=True),
    )
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    assert persisted["data"][CONF_RESOURCE_SET_ID] is None  # stale grant not carried over
    assert persisted["options"][CONF_TRANSPORT] == TRANSPORT_AUTO  # degrades to BLE-capable auto


async def test_cloud_poll_keeps_the_cached_resource_set_id_when_the_gateway_omits_it(monkeypatch):
    # A response that still lists the gateway but drops its resourceSetId must not be read
    # as "the gateway is gone" - overwriting the cached id with None would take a
    # gateway-only install offline for a whole poll interval (24h).
    from plejd_cloud.const import TRANSPORT_GATEWAY

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(gateways=["gw1"], resource_set_id=None)  # gateway present, no id

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    entry.data[CONF_GATEWAYS] = ["gw1"]
    entry.data[CONF_RESOURCE_SET_ID] = "rs1"
    entry.data[CONF_INSTALLATION_ID] = "inst1"
    entry.options = {CONF_TRANSPORT: TRANSPORT_GATEWAY}
    persisted: dict = {}
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: persisted.update({"data": data, "options": options}),
        async_reload=AsyncMock(return_value=True),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    # nothing changed once the cached id is kept, so there is nothing to persist or reload
    assert persisted == {}
    assert entry.data[CONF_RESOURCE_SET_ID] == "rs1"
    assert entry.options[CONF_TRANSPORT] == TRANSPORT_GATEWAY
    config_entries.async_reload.assert_not_awaited()


async def test_cloud_poll_preserves_an_explicit_ble_preference_without_a_gateway(monkeypatch):
    # Only a now-impossible gateway-only preference may be reset when there's no usable
    # gateway - an explicit BLE choice is still valid, and silently downgrading it to AUTO
    # would let a gateway added later start being used on its own.
    from plejd_cloud.const import TRANSPORT_BLE

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[], gateways=[])  # a real change, so the poll proceeds

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    entry.options = {CONF_TRANSPORT: TRANSPORT_BLE}
    persisted: dict = {}
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: persisted.update({"options": options}),
        async_reload=AsyncMock(return_value=True),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    assert persisted["options"][CONF_TRANSPORT] == TRANSPORT_BLE  # not downgraded to AUTO


async def test_cloud_poll_preserves_forced_transport_on_a_malformed_gateway_snapshot(monkeypatch):
    # Unlike a genuine gateway removal (see above), a malformed response (gateways/
    # resourceSetId missing/wrong type) must not reset a forced TRANSPORT_GATEWAY
    # preference - the whole poll is skipped, so the user's choice survives until a
    # well-formed response actually confirms the gateway is gone.
    from plejd_cloud.const import TRANSPORT_GATEWAY

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(gateways=[], malformed=["gateways"])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    entry.data[CONF_GATEWAYS] = ["gw1"]
    entry.data[CONF_RESOURCE_SET_ID] = "rs1"
    entry.data[CONF_INSTALLATION_ID] = "inst1"
    entry.options = {CONF_TRANSPORT: TRANSPORT_GATEWAY}
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: pytest.fail("must not persist a malformed snapshot"),
        async_reload=AsyncMock(),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)  # must not raise

    assert entry.options[CONF_TRANSPORT] == TRANSPORT_GATEWAY  # untouched
    config_entries.async_reload.assert_not_awaited()


async def test_start_self_heal_persists_without_reloading(monkeypatch):
    # The setup-time self-heal call must persist a repaired snapshot without reloading -
    # async_setup_entry (which triggered this) is still setting up THIS entry.
    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(gateways=["gw-new"], resource_set_id="rs-new")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    updated = {}
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: updated.update(data),
        async_reload=AsyncMock(),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None, reload=False)
    assert updated[CONF_GATEWAYS] == ["gw-new"]
    config_entries.async_reload.assert_not_awaited()


async def test_cloud_poll_runs_a_follow_up_reload_for_a_concurrent_change(monkeypatch):
    # _async_reload_entry marks DATA_RELOAD_PENDING when it suppressed its own reload for
    # a concurrent, unrelated change while this poll's reload was in flight - that change
    # must still get a reload once ours is done, not be dropped.
    from plejd_cloud import schedule_ws
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    calls: list[str] = []

    async def _reload(entry_id):
        calls.append(entry_id)
        if len(calls) == 1:  # only the first reload race-loses to the concurrent change
            hass.data[schedule_ws.DATA_RELOAD_PENDING] = entry_id
        return True

    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(side_effect=_reload),
    )
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)

    assert calls == ["e1", "e1"]  # ours, then the follow-up
    assert schedule_ws.DATA_RELOAD_PENDING not in hass.data


async def test_cloud_poll_logs_when_the_follow_up_reload_raises(monkeypatch, caplog):
    # Same race as above, but the follow-up reload itself raises - that must be logged
    # and swallowed (it's a best-effort reload for someone else's change), not propagated
    # out of this poll and not treated as this poll's own reload having failed. It also
    # must not be dropped for good: leaving it pending lets a later reload retry it.
    from plejd_cloud import schedule_ws
    from plejd_cloud.cloud import PlejdCloudDevice

    new_dev = PlejdCloudDevice(**{**_DEV, "device_id": "d2", "name": "Matbord", "address": 9})

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(devices=[PlejdCloudDevice(**_DEV), new_dev])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    calls: list[str] = []

    async def _reload(entry_id):
        calls.append(entry_id)
        if len(calls) == 1:
            hass.data[schedule_ws.DATA_RELOAD_PENDING] = entry_id
            return True
        raise RuntimeError("boom")

    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(side_effect=_reload),
    )
    c = PlejdCoordinator(hass, entry)
    with caplog.at_level("WARNING"):
        await c._async_poll_cloud(None)

    assert calls == ["e1", "e1"]
    assert hass.data[schedule_ws.DATA_RELOAD_PENDING] == "e1"
    assert "follow-up reload for a concurrent change" in caplog.text


async def test_cloud_poll_seeds_installation_id_for_new_gateway(monkeypatch):
    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(gateways=["gw1"], resource_set_id="rs1")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()  # no CONF_INSTALLATION_ID - predates the gateway feature
    updated = {}
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: updated.update(data),
        async_reload=AsyncMock(return_value=True),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)
    assert updated[CONF_INSTALLATION_ID]  # a fresh id was generated, not left missing


async def test_cloud_poll_persists_device_addresses(monkeypatch):
    """A new/remapped physical address must be persisted, or device_address_for() goes
    stale after reload and fault sensors/polling silently stop working for that device."""

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(device_addresses={"d1": 5, "d2": 9})

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()  # cached device_addresses: {}
    updated = {}
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: updated.update(data),
        async_reload=AsyncMock(return_value=True),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)
    assert updated[CONF_DEVICE_ADDRESSES] == {"d1": 5, "d2": 9}


async def test_cloud_poll_auth_error_starts_reauth(monkeypatch):
    from plejd_cloud.cloud import PlejdAuthError

    async def _login(session, email, password):
        raise PlejdAuthError("bad creds")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)

    entry = _cloud_poll_entry()
    reloaded = []
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: None,
        async_reload=lambda eid: reloaded.append(eid),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    started = []
    c._entry = types.SimpleNamespace(async_start_reauth=lambda h: started.append(h))
    await c._async_poll_cloud(None)  # must not raise
    assert not reloaded
    # BLE-only sites have no gateway reconnect path to trigger reauth, and Reconfigure
    # can't repair a rejected password (it reuses the stored one) - the poll must start
    # reauth itself or auto-sync stays silently broken forever.
    assert started == [hass]


async def test_cloud_poll_entry_gone_does_nothing(monkeypatch):
    """Poll is a no-op when the entry has already been removed."""
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: None,
    )
    hass = _hass()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, _cloud_poll_entry())
    await c._async_poll_cloud(None)  # must not raise


async def test_cloud_poll_cloud_error_skips_reload(monkeypatch):
    from plejd_cloud.cloud import PlejdCloudError

    async def _login(session, email, password):
        raise PlejdCloudError("unreachable")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)

    entry = _cloud_poll_entry()
    reloaded = []
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: None,
        async_reload=lambda eid: reloaded.append(eid),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)  # must not raise
    assert not reloaded


async def test_cloud_poll_transport_error_is_treated_as_a_missed_poll(monkeypatch):
    # DNS/socket/TLS/timeout failures raise plain OSError/aiohttp errors, not
    # PlejdCloudError - these must be swallowed the same way, not surface as an
    # unhandled error from this scheduled background callback.
    async def _login(session, email, password):
        raise OSError("Name or service not known")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: None,
    )
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)  # must not raise


async def test_start_attempts_cloud_self_heal_before_raising_not_ready(monkeypatch):
    # A stale gateway/crypto-key is exactly what the cloud poll exists to repair, but its
    # recurring timer never gets registered when connect fails during setup - without this,
    # a setup retry would keep reusing the same stale entry.data forever.
    from homeassistant.exceptions import ConfigEntryNotReady

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(gateways=["gw-new"], resource_set_id="rs-new")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()  # no CONF_GATEWAYS/BLE device in range - connect fails
    updated = {}
    hass = _hass()  # no service_infos/ble_devices -> BLE connect finds nothing
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: updated.update(data),
        async_reload=AsyncMock(return_value=True),
    )
    c = PlejdCoordinator(hass, entry)
    with pytest.raises(ConfigEntryNotReady):
        await c.async_start()
    # The next setup retry re-reads entry.data fresh, so this is the only chance to fix it.
    assert updated[CONF_GATEWAYS] == ["gw-new"]
    # async_setup_entry (which called this) is still setting up THIS entry - reloading it
    # reentrantly here could hang or be rejected instead of taking effect.
    hass.config_entries.async_reload.assert_not_awaited()


async def test_start_self_heal_is_throttled_across_setup_retries(monkeypatch):
    # HA retries a failed setup on its own schedule, and for a BLE-only site simply out of
    # range that can repeat indefinitely. Each retry builds a NEW coordinator, so the throttle
    # state has to live in hass.data - otherwise every attempt means another cloud login.
    from homeassistant.exceptions import ConfigEntryNotReady

    # Count getSiteById, not logins: the gateway-token path logs in too, so login count is
    # not a clean proxy for "the self-heal reached the cloud".
    fetches = 0

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        nonlocal fetches
        fetches += 1
        return _fake_site(gateways=["gw-new"], resource_set_id="rs-new")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    hass = _hass()  # nothing in range -> connect fails -> self-heal path
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(return_value=True),
    )

    for _ in range(3):  # three setup retries, each with its own fresh coordinator
        with pytest.raises(ConfigEntryNotReady):
            await PlejdCoordinator(hass, entry).async_start()

    assert fetches == 1  # only the first attempt reached the cloud


async def test_start_self_heal_retries_once_the_cooldown_has_elapsed(monkeypatch):
    # The throttle must not be permanent: a genuine repair still has to get through, just at
    # a sane cadence rather than on every rapid setup retry.
    from homeassistant.exceptions import ConfigEntryNotReady

    fetches = 0

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        nonlocal fetches
        fetches += 1
        return _fake_site(gateways=["gw-new"], resource_set_id="rs-new")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    clock = 1_000.0
    monkeypatch.setattr(coordinator_mod.time, "monotonic", lambda: clock)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(return_value=True),
    )

    with pytest.raises(ConfigEntryNotReady):
        await PlejdCoordinator(hass, entry).async_start()
    assert fetches == 1

    clock += coordinator_mod.SELF_HEAL_COOLDOWN_SECONDS + 1
    monkeypatch.setattr(coordinator_mod.time, "monotonic", lambda: clock)
    with pytest.raises(ConfigEntryNotReady):
        await PlejdCoordinator(hass, entry).async_start()
    assert fetches == 2  # allowed through again


async def test_start_preserves_the_original_not_ready_when_self_heal_raises(monkeypatch, caplog):
    # An unexpected failure from the best-effort self-heal attempt (e.g. a malformed cloud
    # response) must not replace the original ConfigEntryNotReady - HA's setup-retry path
    # is keyed on that exact exception type.
    from homeassistant.exceptions import ConfigEntryNotReady

    async def _login(session, email, password):
        raise ValueError("malformed response")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)

    entry = _cloud_poll_entry()
    hass = _hass()  # no service_infos/ble_devices -> BLE connect finds nothing
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(async_get_entry=lambda eid: entry)
    c = PlejdCoordinator(hass, entry)
    with pytest.raises(ConfigEntryNotReady, match="no Plejd device"):
        await c.async_start()
    assert "cloud self-heal attempt during setup failed" in caplog.text


async def test_cloud_poll_persists_rooms(monkeypatch):
    from dataclasses import asdict

    from plejd_cloud.cloud import PlejdCloudRoom

    room = PlejdCloudRoom(
        room_id="r1", name="Kok", address=100, member_addresses=[5], dimmable=True, dimmable_addresses=[5]
    )

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(rooms=[room])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    updated = {}
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: updated.update(data),
        async_reload=AsyncMock(return_value=True),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)
    # A room added/renamed/removed must be reflected too, not just devices/scenes -
    # otherwise the advertised automatic room sync never actually updates room entities.
    assert updated[CONF_ROOMS] == [asdict(room)]


async def test_cloud_poll_discards_result_after_shutdown(monkeypatch):
    from plejd_cloud.cloud import PlejdCloudDevice

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        # Shutdown begins while this cloud call is still in flight - the interval timer
        # is already unregistered by then, but this already-running call must still
        # notice and not act on a possibly-removed entry with a stale snapshot.
        c._closed = True
        return _fake_site(devices=[PlejdCloudDevice(**{**_DEV, "device_id": "d2"})])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    updated = {}
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: updated.update(data),
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    await c._async_poll_cloud(None)
    assert not updated


async def test_cloud_poll_auth_error_after_shutdown_does_not_start_reauth(monkeypatch):
    from plejd_cloud.cloud import PlejdAuthError

    async def _login(session, email, password):
        c._closed = True
        raise PlejdAuthError("bad creds")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)

    entry = _cloud_poll_entry()
    config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: None,
    )
    hass = _hass()
    hass.session = object()
    hass.config_entries = config_entries
    c = PlejdCoordinator(hass, entry)
    started = []
    c._entry = types.SimpleNamespace(async_start_reauth=lambda h: started.append(h))
    await c._async_poll_cloud(None)  # must not raise
    assert not started


def _fw_site(firmware_by_device):
    from plejd_cloud.cloud import PlejdDeviceFirmware

    fw = {
        device_id: PlejdDeviceFirmware(version=v, build_time=bt, hardware_id=hw, faceplate_id=face)
        for device_id, (v, bt, hw, face) in firmware_by_device.items()
    }
    return types.SimpleNamespace(firmware_by_device=fw)


def _cloud_entry():
    return types.SimpleNamespace(
        entry_id="e1",
        data={
            CONF_CRYPTO_KEY: _KEY_HEX,
            CONF_DEVICES: [_DEV],
            CONF_DISCOVERED_ADDRESS: None,
            CONF_SITE_ID: "S1",
            CONF_EMAIL: "u@x.se",
            CONF_PASSWORD: "pw",
        },
    )


async def test_refresh_firmware_populates_status(monkeypatch):
    c = PlejdCoordinator(_hass(), _cloud_entry())
    site = _fw_site({"d1": ("6.40.0", 20251201000000, 1, "0")})

    async def _login(*a):
        return "tok"

    async def _get_site(*a):
        return site

    async def _latest(session, token, hw, face):
        return ("6.43.3", 20260324155701)

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    monkeypatch.setattr(coordinator_mod, "async_get_available_firmware", _latest)

    await c.async_refresh_firmware()
    status = c.firmware["d1"]
    assert status.installed_version == "6.40.0" and status.installed_build_time == 20251201000000
    assert status.latest_version == "6.43.3" and status.update_available is True


async def test_refresh_firmware_caches_lookups_per_hardware(monkeypatch):
    c = PlejdCoordinator(_hass(), _cloud_entry())
    # two devices sharing one (hardware, faceplate) + a motion sensor on different hardware
    site = _fw_site(
        {
            "d1": ("6.40.0", 20251201000000, 1, "0"),
            "d2": ("6.40.0", 20251201000000, 1, "0"),
            "w1": ("4.41.3", 20240910153670, 70, None),
        }
    )
    calls = []

    async def _login(*a):
        return "tok"

    async def _get_site(*a):
        return site

    async def _latest(session, token, hw, face):
        calls.append((hw, face))
        return None  # up to date

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    monkeypatch.setattr(coordinator_mod, "async_get_available_firmware", _latest)

    await c.async_refresh_firmware()
    assert sorted(calls) == [(1, "0"), (70, None)]  # one lookup per distinct (hardware, faceplate)
    assert set(c.firmware) == {"d1", "d2", "w1"}  # sensors covered, not just outputs
    assert c.firmware["w1"].update_available is False and c.firmware["w1"].latest_version is None


async def test_refresh_firmware_tolerates_one_failed_lookup(monkeypatch):
    c = PlejdCoordinator(_hass(), _cloud_entry())
    site = _fw_site(
        {
            "d1": ("6.40.0", 20251201000000, 1, "0"),  # this hardware's lookup will raise
            "w1": ("4.41.3", 20240910153670, 70, None),  # this one succeeds
        }
    )

    async def _login(*a):
        return "tok"

    async def _get_site(*a):
        return site

    async def _latest(session, token, hw, face):
        if hw == 1:
            raise coordinator_mod.PlejdAuthError("boom")  # one flaky combo
        return ("4.42.0", 20260101000000)

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    monkeypatch.setattr(coordinator_mod, "async_get_available_firmware", _latest)

    await c.async_refresh_firmware()
    # the whole refresh survives: every device still gets a status
    assert set(c.firmware) == {"d1", "w1"}
    assert c.firmware["d1"].latest_version is None  # failed lookup -> treated as up to date
    assert c.firmware["w1"].update_available is True  # the successful one still resolves


async def test_refresh_firmware_no_credentials_is_noop(monkeypatch):
    c = PlejdCoordinator(_hass(), _entry())  # no CONF_EMAIL / CONF_PASSWORD

    async def _boom(*a):
        raise AssertionError("must not log in without credentials")

    monkeypatch.setattr(coordinator_mod, "async_login", _boom)
    await c.async_refresh_firmware()
    assert c.firmware == {}


async def test_firmware_check_swallows_errors(monkeypatch):
    c = PlejdCoordinator(_hass(), _cloud_entry())

    async def _boom(*a):
        raise RuntimeError("cloud down")

    monkeypatch.setattr(coordinator_mod, "async_login", _boom)
    await c._async_firmware_check(None)  # best-effort: no exception
    assert c.firmware == {}


def test_schedule_firmware_checks_registers_timers_once():
    c = PlejdCoordinator(_hass(), _cloud_entry())
    c._schedule_firmware_checks()
    daily, one_shot = c._firmware_unsub, c._firmware_now_unsub
    assert daily is not None and one_shot is not None
    c._schedule_firmware_checks()  # a second start must not re-register
    assert c._firmware_unsub is daily and c._firmware_now_unsub is one_shot


async def test_one_shot_handle_cleared_after_firing(monkeypatch):
    c = PlejdCoordinator(_hass(), _cloud_entry())
    c._firmware_now_unsub = lambda: None

    async def _noop(*a):
        return None

    monkeypatch.setattr(c, "async_refresh_firmware", _noop)
    await c._async_firmware_check(None)
    assert c._firmware_now_unsub is None  # cleared once the one-shot fires


async def test_shutdown_cancels_both_firmware_timers():
    c = PlejdCoordinator(_hass(), _cloud_entry())
    cancelled = []
    c._firmware_unsub = lambda: cancelled.append("daily")
    c._firmware_now_unsub = lambda: cancelled.append("one_shot")
    await c.async_shutdown()
    assert sorted(cancelled) == ["daily", "one_shot"]
    assert c._firmware_unsub is None and c._firmware_now_unsub is None


class _FakeDevice:
    def __init__(self, identifiers, name_by_user):
        self.identifiers = identifiers
        self.name_by_user = name_by_user


class _FakeRegistry:
    def __init__(self, device):
        self._device = device

    def async_get(self, device_id):
        return self._device


def _reg_event(action="update", changes=None, device_id="ha1"):
    return types.SimpleNamespace(
        data={"action": action, "changes": changes if changes is not None else {}, "device_id": device_id}
    )


def test_output_parse_id_picks_lowest_output_index():
    from plejd_cloud.cloud import PlejdCloudDevice
    from plejd_cloud.coordinator import PlejdCoordinator

    def _dev(output_index, object_id):
        return PlejdCloudDevice(
            device_id="d1",
            name="Dev",
            address=1,
            output_index=output_index,
            outputs=[1],
            hardware_id=1,
            model="DIM-02",
            category="light",
            dimmable=True,
            traits=0,
            room_id=None,
            object_id=object_id,
        )

    # output_index=1 comes first in the list but output_index=0 is the primary
    devices = [_dev(1, "parse-id-1"), _dev(0, "parse-id-0")]
    assert PlejdCoordinator._output_parse_id(devices, "d1") == "parse-id-0"


async def test_rename_device_calls_cloud(monkeypatch):
    c = PlejdCoordinator(_hass(), _cloud_entry())
    c.devices[0].object_id = "p1"  # device_id "d1"
    captured = {}

    async def _login(*a):
        return "tok"

    async def _set(session, token, site_id, device_id, parse_id, title):
        captured.update(site_id=site_id, device_id=device_id, parse_id=parse_id, title=title)
        return True

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_set_device_title", _set)
    await c.async_rename_device("d1", "New Name")
    assert captured == {"site_id": "S1", "device_id": "d1", "parse_id": "p1", "title": "New Name"}


async def test_rename_device_falls_back_to_cloud_lookup(monkeypatch):
    # entries cached before object_id existed (e.g. _DEV) resolve it from a fresh site fetch
    from plejd_cloud.cloud import PlejdCloudDevice

    c = PlejdCoordinator(_hass(), _cloud_entry())  # _DEV has no object_id
    fresh = PlejdCloudDevice(
        device_id="d1",
        name="Kitchen",
        address=5,
        output_index=0,
        outputs=[5],
        hardware_id=1,
        model="DIM-01",
        category="light",
        dimmable=True,
        traits=3,
        room_id="r1",
        object_id="p2",
    )
    captured = {}

    async def _login(*a):
        return "tok"

    async def _get_site(*a):
        return types.SimpleNamespace(devices=[fresh])

    async def _set(session, token, site_id, device_id, parse_id, title):
        captured.update(parse_id=parse_id, title=title)
        return True

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    monkeypatch.setattr(coordinator_mod, "async_set_device_title", _set)
    await c.async_rename_device("d1", "X")
    assert captured == {"parse_id": "p2", "title": "X"}


async def test_rename_device_skips_when_not_resolvable(monkeypatch):
    c = PlejdCoordinator(_hass(), _cloud_entry())  # _DEV has no object_id

    async def _login(*a):
        return "tok"

    async def _get_site(*a):
        return types.SimpleNamespace(devices=[])  # device not found in the cloud either

    async def _fail(*a):
        raise AssertionError("must not call updateDevice when no Parse id is resolvable")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    monkeypatch.setattr(coordinator_mod, "async_set_device_title", _fail)
    await c.async_rename_device("d1", "X")  # resolves to nothing -> skip, no write


async def test_rename_device_skips_without_credentials(monkeypatch):
    c = PlejdCoordinator(_hass(), _entry())  # no CONF_EMAIL / CONF_PASSWORD

    async def _boom(*a):
        raise AssertionError("must not log in without credentials")

    monkeypatch.setattr(coordinator_mod, "async_login", _boom)
    await c.async_rename_device("d1", "X")


async def test_device_rename_already_done_by_the_dashboard_is_not_mirrored_again(monkeypatch):
    from plejd_cloud.const import DOMAIN

    c = PlejdCoordinator(_hass(), _cloud_entry())
    device = types.SimpleNamespace(identifiers={(DOMAIN, "d1")}, name_by_user="Kitchen")
    c.hass.device_registry = types.SimpleNamespace(async_get=lambda device_id: device)
    calls = []

    async def _rename(device_id, title, output_index=None):
        calls.append(title)

    monkeypatch.setattr(c, "async_rename_device", _rename)
    event = types.SimpleNamespace(data={"action": "update", "device_id": "dev", "changes": {"name_by_user": "old"}})
    c.skip_next_mirror("d1", "Kitchen")
    await c.async_handle_device_registry_update(event)
    assert calls == []  # skipped once...
    await c.async_handle_device_registry_update(event)
    assert calls == ["Kitchen"]  # ...and only once: a later user rename in HA still mirrors


async def test_rename_named_output_targets_that_output(monkeypatch):
    # the dashboard renames one light of a multi-output device: its own Parse object, not the primary's
    import dataclasses

    c = PlejdCoordinator(_hass(), _cloud_entry())
    c.devices[0].object_id = "p0"
    c.devices.append(dataclasses.replace(c.devices[0], output_index=1, object_id="p1"))
    captured = {}

    async def _login(*a):
        return "tok"

    async def _set(session, token, site_id, device_id, parse_id, title):
        captured.update(parse_id=parse_id, title=title)
        return True

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_set_device_title", _set)
    await c.async_rename_device("d1", "Second", output_index=1)
    assert captured == {"parse_id": "p1", "title": "Second"}


async def test_rename_named_output_raises_instead_of_skipping(monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    no_creds = PlejdCoordinator(_hass(), _entry())
    with pytest.raises(HomeAssistantError):
        await no_creds.async_rename_device("d1", "X", output_index=0)

    c = PlejdCoordinator(_hass(), _cloud_entry())  # _DEV has no object_id, and the cloud lookup finds none

    async def _login(*a):
        return "tok"

    async def _get_site(*a):
        return types.SimpleNamespace(devices=[])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    with pytest.raises(HomeAssistantError, match="no cloud object"):
        await c.async_rename_device("d1", "X", output_index=0)


async def test_rename_device_raises_when_cloud_rejects(monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    c = PlejdCoordinator(_hass(), _cloud_entry())
    c.devices[0].object_id = "p1"

    async def _login(*a):
        return "tok"

    async def _set(*a):
        return False

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_set_device_title", _set)
    with pytest.raises(HomeAssistantError):
        await c.async_rename_device("d1", "X")


async def test_registry_update_mirrors_rename(monkeypatch):
    from plejd_cloud.const import DOMAIN

    hass = _hass()
    c = PlejdCoordinator(hass, _cloud_entry())
    hass.device_registry = _FakeRegistry(_FakeDevice({(DOMAIN, "F1AF")}, "New Name"))
    called = {}

    async def _rename(device_id, title):
        called.update(device_id=device_id, title=title)

    monkeypatch.setattr(c, "async_rename_device", _rename)
    await c.async_handle_device_registry_update(_reg_event(changes={"name_by_user": "Old"}))
    assert called == {"device_id": "F1AF", "title": "New Name"}


async def test_registry_update_ignores_non_rename_events(monkeypatch):
    c = PlejdCoordinator(_hass(), _cloud_entry())

    async def _fail(*a):
        raise AssertionError("rename must not be triggered")

    monkeypatch.setattr(c, "async_rename_device", _fail)
    await c.async_handle_device_registry_update(_reg_event(action="create", changes={"name_by_user": "x"}))
    await c.async_handle_device_registry_update(_reg_event(action="update", changes={"area_id": None}))


async def test_registry_update_ignores_missing_device_and_non_plejd(monkeypatch):
    from plejd_cloud.const import DOMAIN

    hass = _hass()
    c = PlejdCoordinator(hass, _cloud_entry())

    async def _fail(*a):
        raise AssertionError("rename must not be triggered")

    monkeypatch.setattr(c, "async_rename_device", _fail)
    hass.device_registry = _FakeRegistry(None)  # device gone
    await c.async_handle_device_registry_update(_reg_event(changes={"name_by_user": "x"}))
    hass.device_registry = _FakeRegistry(_FakeDevice({("other", "z")}, "Name"))  # not a Plejd device
    await c.async_handle_device_registry_update(_reg_event(changes={"name_by_user": "x"}))
    hass.device_registry = _FakeRegistry(_FakeDevice({(DOMAIN, "d1")}, None))  # name cleared
    await c.async_handle_device_registry_update(_reg_event(changes={"name_by_user": "x"}))
    # no registry set yet (early startup)
    del hass.device_registry
    await c.async_handle_device_registry_update(_reg_event(changes={"name_by_user": "x"}))
    # device_id missing from event data
    hass.device_registry = _FakeRegistry(None)
    await c.async_handle_device_registry_update(
        types.SimpleNamespace(data={"action": "update", "changes": {"name_by_user": "x"}})
    )


async def test_registry_update_ignores_room_pseudo_device(monkeypatch):
    from plejd_cloud.const import DOMAIN, ROOM_DEVICE_ID_PREFIX

    hass = _hass()
    c = PlejdCoordinator(hass, _cloud_entry())
    hass.device_registry = _FakeRegistry(_FakeDevice({(DOMAIN, f"{ROOM_DEVICE_ID_PREFIX}r1")}, "New Name"))

    async def _fail(*a):
        raise AssertionError("a room pseudo-device has no Parse cloud object to rename")

    monkeypatch.setattr(c, "async_rename_device", _fail)
    await c.async_handle_device_registry_update(_reg_event(changes={"name_by_user": "New Name"}))


async def test_registry_update_swallows_rename_errors(monkeypatch):
    from plejd_cloud.const import DOMAIN

    hass = _hass()
    c = PlejdCoordinator(hass, _cloud_entry())
    hass.device_registry = _FakeRegistry(_FakeDevice({(DOMAIN, "d1")}, "Name"))

    async def _boom(*a):
        raise RuntimeError("cloud down")

    monkeypatch.setattr(c, "async_rename_device", _boom)
    await c.async_handle_device_registry_update(_reg_event(changes={"name_by_user": "x"}))  # no exception


async def test_registry_update_triggers_reauth_on_auth_error(monkeypatch):
    from plejd_cloud.const import DOMAIN

    hass = _hass()
    c = PlejdCoordinator(hass, _cloud_entry())
    started = []
    c._entry = types.SimpleNamespace(async_start_reauth=lambda h: started.append(h))
    hass.device_registry = _FakeRegistry(_FakeDevice({(DOMAIN, "d1")}, "New"))

    async def _auth_fail(*a):
        raise coordinator_mod.PlejdAuthError("bad creds")

    monkeypatch.setattr(coordinator_mod, "async_login", _auth_fail)
    await c.async_handle_device_registry_update(_reg_event(changes={"name_by_user": "x"}))
    assert started == [hass]  # reauth flow prompted instead of silently swallowed


async def test_poll_faults_reads_each_device(monkeypatch):
    from plejd_cloud.const import CMD_NOTIFY_EVENTS
    from plejd_cloud.protocol import TYPE_READ, decode_command

    client = _FakeClient()
    _patch_connect(monkeypatch, client)
    ble = types.SimpleNamespace(address="01:02:03:04:05:a0")
    hass = _hass([_info("01:02:03:04:05:a0")], {"01:02:03:04:05:a0": ble})
    entry = _entry(discovered=None)
    entry.data[CONF_DEVICE_ADDRESSES] = {"d1": 5}  # physical device address, cached at setup
    c = PlejdCoordinator(hass, entry)
    await c.async_start()  # sets up the poll and does an initial fault read
    reads = [decode_command(c._connection.mesh.decrypt(w[1])) for w in client.writes if w[0] == PLEJD_CHAR_DATA_UUID]
    notify = [r for r in reads if r.command == CMD_NOTIFY_EVENTS]
    assert notify and notify[0].command_type == TYPE_READ and notify[0].address == 5


async def test_poll_faults_best_effort_when_not_connected():
    c = PlejdCoordinator(_hass(), _entry())  # never connected -> _write_vector raises
    await c._async_poll_faults(None)  # swallowed, no exception propagates
    assert c.faults_for(5) == frozenset()


async def test_poll_faults_one_device_failure_does_not_skip_the_rest(monkeypatch):
    """A write failure for one device must not abort polling the remaining devices."""
    from homeassistant.exceptions import HomeAssistantError

    dev2 = {**_DEV, "device_id": "d2", "address": 6}
    entry = types.SimpleNamespace(
        entry_id="e1",
        data={
            CONF_CRYPTO_KEY: _KEY_HEX,
            CONF_DEVICES: [_DEV, dev2],
            CONF_DISCOVERED_ADDRESS: None,
            CONF_DEVICE_ADDRESSES: {"d1": 5, "d2": 6},
        },
    )
    c = PlejdCoordinator(_hass(), entry)
    attempted: list[int] = []

    async def _write_vector(vector):
        attempted.append(vector[0])
        if vector[0] == 5:
            raise HomeAssistantError("not connected")

    monkeypatch.setattr(c, "_write_vector", _write_vector)
    await c._async_poll_faults(None)
    assert sorted(attempted) == [5, 6]  # device 6 was still polled despite device 5 failing


def test_device_address_for_resolves_cached_physical_addresses():
    entry = types.SimpleNamespace(
        entry_id="e1",
        data={
            CONF_CRYPTO_KEY: _KEY_HEX,
            CONF_DEVICES: [_DEV],
            CONF_DISCOVERED_ADDRESS: None,
            CONF_DEVICE_ADDRESSES: {"d1": 5, "w1": 33},
        },
    )
    c = PlejdCoordinator(_hass(), entry)
    assert c.device_address_for("d1") == 5
    assert c.device_address_for("w1") == 33
    assert c.device_address_for("unknown") is None


async def test_poll_faults_resolves_addresses_from_cloud_when_not_cached(monkeypatch):
    """Entries added before CONF_DEVICE_ADDRESSES existed resolve it via one cloud fetch."""
    c = PlejdCoordinator(_hass(), _cloud_entry())  # has credentials, no cached device_addresses
    site = types.SimpleNamespace(device_addresses={"d1": 5, "w1": 33}, rooms=[])

    async def _login(*a):
        return "tok"

    async def _get_site(*a):
        return site

    attempted: list[int] = []

    async def _write_vector(vector):
        attempted.append(vector[0])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    monkeypatch.setattr(c, "_write_vector", _write_vector)

    await c._async_poll_faults(None)
    assert sorted(attempted) == [5, 33]
    assert c._device_addresses == {"d1": 5, "w1": 33}  # cached — no repeat fetch on the next poll

    await c._async_poll_faults(None)
    assert sorted(attempted) == [5, 5, 33, 33]  # polled again, but from the cache


async def test_poll_faults_resolves_rooms_from_cloud_when_entry_predates_room_groups(monkeypatch):
    """Entries added before CONF_ROOMS existed (missing key, not an empty list) backfill it via a cloud fetch."""
    from dataclasses import asdict

    from plejd_cloud.cloud import PlejdCloudRoom

    entry = _cloud_entry()  # has credentials, CONF_ROOMS absent from entry.data
    c = PlejdCoordinator(_hass(), entry)
    assert c.rooms == [] and c._rooms_from_legacy_entry is True
    room = PlejdCloudRoom(
        room_id="r1", name="Kök", address=14, member_addresses=[11], dimmable=True, dimmable_addresses=[11]
    )
    site = types.SimpleNamespace(
        device_addresses={"d1": 1},
        rooms=[room],
        all_rooms=[types.SimpleNamespace(room_id="r1", name="Kök")],
        malformed=frozenset(),
    )
    fetches = []

    async def _login(*a):
        return "tok"

    async def _get_site(*a):
        fetches.append(1)
        return site

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    monkeypatch.setattr(c, "_write_vector", lambda vector: asyncio.sleep(0))

    await c._async_poll_faults(None)
    assert c.rooms == [room]
    assert c._rooms_from_legacy_entry is False
    # Persisted to entry.data, not just the in-memory coordinator, so the room light
    # entity survives a restart/reload even if the cloud is unreachable at that point.
    assert entry.data[CONF_ROOMS] == [asdict(room)]
    assert entry.data[CONF_ROOM_NAMES] == {"r1": "Kök"}

    await c._async_poll_faults(None)
    assert fetches == [1]  # cached — no repeat fetch once rooms are resolved


async def test_poll_faults_backfills_room_names_for_an_entry_that_predates_them(monkeypatch):
    # Rooms whose group holds a non-light output are only in all_rooms; without their names the
    # dashboard can't give their lights a card, so fetch them now instead of at the daily sync.
    entry = _cloud_entry()
    entry.data[CONF_ROOMS] = []
    entry.data.pop(CONF_ROOM_NAMES, None)
    c = PlejdCoordinator(_hass(), entry)
    c._device_addresses = {"d1": 1}
    site = types.SimpleNamespace(
        device_addresses={"d1": 1},
        rooms=[],
        all_rooms=[types.SimpleNamespace(room_id="r9", name="Garage")],
        malformed=frozenset(),
    )

    async def _login(*a):
        return "tok"

    async def _get_site(*a):
        return site

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    monkeypatch.setattr(c, "_write_vector", lambda vector: asyncio.sleep(0))
    await c._async_poll_faults(None)
    assert entry.data[CONF_ROOM_NAMES] == {"r9": "Garage"}
    assert entry.data[CONF_ROOMS] == []  # untouched: only the missing key is filled in


async def test_poll_faults_does_not_backfill_rooms_from_a_malformed_site(monkeypatch):
    entry = _cloud_entry()
    entry.data[CONF_ROOMS] = []
    entry.data.pop(CONF_ROOM_NAMES, None)
    c = PlejdCoordinator(_hass(), entry)
    c._device_addresses = {"d1": 1}
    site = types.SimpleNamespace(device_addresses={"d1": 1}, rooms=[], all_rooms=[], malformed=frozenset({"rooms"}))
    fetches = []

    async def _login(*a):
        return "tok"

    async def _get_site(*a):
        fetches.append(1)
        return site

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    monkeypatch.setattr(c, "_write_vector", lambda vector: asyncio.sleep(0))
    await c._async_poll_faults(None)
    assert CONF_ROOM_NAMES not in entry.data  # nothing persisted from the bad snapshot
    await c._async_poll_faults(None)
    assert fetches == [1, 1]  # so the next poll tries again


@pytest.mark.parametrize("closed", [False, True])
async def test_poll_faults_stops_backfilling_and_starts_reauth_on_rejected_credentials(monkeypatch, closed):
    from plejd_cloud.cloud import PlejdAuthError

    entry = _cloud_entry()
    entry.data[CONF_ROOMS] = []
    entry.data.pop(CONF_ROOM_NAMES, None)
    c = PlejdCoordinator(_hass(), entry)
    c._device_addresses = {"d1": 1}
    c._closed = closed
    started, logins = [], []
    entry.async_start_reauth = lambda h: started.append(h)

    async def _login(*a):
        logins.append(1)
        raise PlejdAuthError("bad password")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(c, "_write_vector", lambda vector: asyncio.sleep(0))
    await c._async_poll_faults(None)
    await c._async_poll_faults(None)
    assert logins == [1]  # no login retried every poll with credentials known to be bad
    assert len(started) == (0 if closed else 1)  # a shut-down coordinator doesn't prompt
    assert CONF_ROOM_NAMES not in entry.data


async def test_poll_faults_does_not_refetch_for_a_genuinely_room_less_site(monkeypatch):
    """CONF_ROOMS present but empty (a real site with no rooms) must not trigger a fetch every poll."""
    entry = _cloud_entry()
    entry.data[CONF_ROOMS] = []
    c = PlejdCoordinator(_hass(), entry)
    assert c._rooms_from_legacy_entry is False

    async def _fail(*a):
        raise AssertionError("must not fetch the cloud site for an already-resolved empty room list")

    monkeypatch.setattr(coordinator_mod, "async_login", _fail)
    monkeypatch.setattr(c, "_write_vector", lambda vector: asyncio.sleep(0))
    await c._async_poll_faults(None)


async def test_poll_faults_swallows_cloud_fetch_failure(monkeypatch):
    """A failed address-resolution fetch is best-effort: no crash, retried next interval."""
    c = PlejdCoordinator(_hass(), _cloud_entry())  # has credentials, no cached device_addresses

    async def _boom(*a):
        raise coordinator_mod.PlejdCloudError("cloud unreachable")  # transient; auth has its own test

    attempted: list[int] = []

    async def _write_vector(vector):
        attempted.append(vector[0])

    monkeypatch.setattr(coordinator_mod, "async_login", _boom)
    monkeypatch.setattr(c, "_write_vector", _write_vector)
    await c._async_poll_faults(None)  # no exception propagates
    assert attempted == [] and c._device_addresses == {}


async def test_poll_faults_without_credentials_or_cache_is_noop(monkeypatch):
    """No cached addresses and no credentials to fetch them -> nothing polled, no crash."""
    c = PlejdCoordinator(_hass(), _entry())  # no CONF_EMAIL/CONF_PASSWORD, no CONF_DEVICE_ADDRESSES

    async def _boom(*a):
        raise AssertionError("must not log in without credentials")

    attempted: list[int] = []

    async def _write_vector(vector):
        attempted.append(vector[0])

    monkeypatch.setattr(coordinator_mod, "async_login", _boom)
    monkeypatch.setattr(c, "_write_vector", _write_vector)
    await c._async_poll_faults(None)
    assert attempted == []


async def test_cloud_poll_raises_a_repair_issue_for_a_skipped_sync(monkeypatch):
    # Skipping a malformed snapshot is right but completely silent: a site whose daily sync
    # has been skipped looks identical to one that simply has not changed. Deliberately no
    # "wait for N in a row" threshold - that needed state a restart would erase, and with a
    # 24h interval one skip already means a full day without syncing.
    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(malformed=["devices"])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: pytest.fail("must not persist a malformed snapshot"),
        async_reload=AsyncMock(),
    )
    c = PlejdCoordinator(hass, entry)

    await c._async_poll_cloud(None)

    issue = hass.created_issues["malformed_cloud_site_e1"]
    assert issue["severity"] == "warning"
    assert issue["translation_key"] == "malformed_cloud_site"
    assert issue["translation_placeholders"]["collections"] == "devices"


async def test_cloud_poll_clears_the_repair_issue_once_the_cloud_recovers(monkeypatch):
    from plejd_cloud.cloud import PlejdCloudDevice

    responses = [_fake_site(malformed=["devices"]), _fake_site()]

    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return responses.pop(0)

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)
    assert PlejdCloudDevice  # the recovered response parses to the cached device, so no change

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(return_value=True),
    )
    c = PlejdCoordinator(hass, entry)

    await c._async_poll_cloud(None)
    assert "malformed_cloud_site_e1" in hass.created_issues

    await c._async_poll_cloud(None)  # a good response
    assert "malformed_cloud_site_e1" not in hass.created_issues
    assert "malformed_cloud_site_e1" in hass.deleted_issues


async def test_cloud_poll_clears_a_repair_issue_that_outlived_a_restart(monkeypatch):
    # The issue is persistent but the streak counter is not, so after a restart mid-incident
    # the issue is on screen with the counter back at zero. Recovery has to clear it anyway -
    # gating the delete on the counter would strand the warning on screen forever.
    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site()  # a good response, matching the cached snapshot

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(return_value=True),
    )
    # what a restart looks like: the persisted issue survived, hass.data did not
    hass.created_issues = {"malformed_cloud_site_e1": {"domain": "plejd_cloud"}}

    await PlejdCoordinator(hass, entry)._async_poll_cloud(None)

    assert "malformed_cloud_site_e1" not in hass.created_issues


async def test_cloud_poll_marks_the_malformed_repair_issue_persistent(monkeypatch):
    # Non-persistent is HA's default, and it would drop the warning on restart - leaving an
    # ongoing incident invisible for another two 24h polls.
    async def _login(session, email, password):
        return "TOKEN"

    async def _get_site(session, token, site_id):
        return _fake_site(malformed=["devices"])

    monkeypatch.setattr(coordinator_mod, "async_login", _login)
    monkeypatch.setattr(coordinator_mod, "async_get_site", _get_site)

    entry = _cloud_poll_entry()
    hass = _hass()
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: pytest.fail("must not persist a malformed snapshot"),
        async_reload=AsyncMock(),
    )
    await PlejdCoordinator(hass, entry)._async_poll_cloud(None)

    assert hass.created_issues["malformed_cloud_site_e1"]["is_persistent"] is True


async def test_self_heal_cooldown_is_held_through_an_auth_failure(monkeypatch):
    # Until the user actually completes reauth the credentials stay bad, so releasing the
    # cooldown here would let every setup retry call async_login again - exactly the stream
    # of cloud logins the cooldown exists to prevent. The successful reauth path clears it.
    from plejd_cloud.cloud import PlejdAuthError

    async def _login(session, email, password):
        raise PlejdAuthError("bad creds")

    monkeypatch.setattr(coordinator_mod, "async_login", _login)

    entry = _cloud_poll_entry()
    entry.async_start_reauth = lambda hass: None
    hass = _hass()
    hass.session = object()
    hass.config_entries = types.SimpleNamespace(
        async_get_entry=lambda eid: entry,
        async_update_entry=lambda e, data, options=None: setattr(e, "data", data),
        async_reload=AsyncMock(),
    )
    c = PlejdCoordinator(hass, entry)
    assert c._should_attempt_self_heal() is True  # records the attempt
    await c._async_poll_cloud(None, reload=False)

    assert c._should_attempt_self_heal() is False  # still throttled

    # ...until reauth succeeds, which is the point a fresh attempt could actually work
    coordinator_mod.async_reset_self_heal_cooldown(hass, entry.entry_id)
    assert c._should_attempt_self_heal() is True


async def test_self_heal_cooldown_survives_a_backwards_wall_clock_step(monkeypatch):
    # An NTP correction stepping the wall clock backwards would make a wall-clock gap
    # negative and suppress self-healing until real time caught up - far longer than the
    # cooldown intends. The throttle is monotonic, so a wall-clock step cannot affect it.
    entry = _cloud_poll_entry()
    hass = _hass()
    c = PlejdCoordinator(hass, entry)

    clock = 1_000.0
    monkeypatch.setattr(coordinator_mod.time, "monotonic", lambda: clock)
    assert c._should_attempt_self_heal() is True
    assert c._should_attempt_self_heal() is False  # inside the cooldown

    clock += coordinator_mod.SELF_HEAL_COOLDOWN_SECONDS + 1
    assert c._should_attempt_self_heal() is True  # released purely on elapsed monotonic time


# ── toggle_origin (activity-log attribution) ─────────────────────────────────


def _origin_coordinator(monkeypatch, clock):
    from plejd_cloud.cloud import PlejdCloudInput, PlejdCloudRoom
    from plejd_cloud.protocol import Command

    monkeypatch.setattr(coordinator_mod.time, "monotonic", lambda: clock[0])
    c = PlejdCoordinator(_hass(), _entry())  # _DEV: output address 5, device d1
    c.rooms = [PlejdCloudRoom("r1", "Kontor", 41, [5], True, [5])]
    c.inputs = [PlejdCloudInput("d1", "Built-in", 5), PlejdCloudInput("remote", "Hall switch", 60)]

    def toggle(address):
        c._on_event(Command(address=address, command_type=0x10, command=coordinator_mod.CMD_INPUT_BUTTON, data=b"\x01"))

    return c, toggle


def test_toggle_origin_room_group_command(monkeypatch):
    clock = [100.0]
    c, toggle = _origin_coordinator(monkeypatch, clock)
    toggle(41)
    clock[0] = 102.0
    assert c.toggle_origin(5) == {"kind": "plejd_room", "name": "Kontor"}


def test_toggle_origin_on_the_output_itself_is_ambiguous(monkeypatch):
    clock = [100.0]
    c, toggle = _origin_coordinator(monkeypatch, clock)
    toggle(5)
    assert c.toggle_origin(5) == {"kind": "plejd_device"}


def test_toggle_origin_separate_remote_is_a_best_guess(monkeypatch):
    clock = [100.0]
    c, toggle = _origin_coordinator(monkeypatch, clock)
    toggle(60)
    assert c.toggle_origin(5) == {"kind": "plejd_input", "name": "Hall switch"}


def test_toggle_origin_ignores_malformed_toggle_frames(monkeypatch):
    from plejd_cloud.protocol import Command

    clock = [100.0]
    c, _ = _origin_coordinator(monkeypatch, clock)
    for data in (b"", b"\x07"):
        c._on_event(Command(address=41, command_type=0x10, command=coordinator_mod.CMD_INPUT_BUTTON, data=data))
    assert c.toggle_origin(5) is None


def test_toggle_origin_since_ignores_commands_from_before(monkeypatch):
    clock = [100.0]
    c, toggle = _origin_coordinator(monkeypatch, clock)
    toggle(5)
    assert c.toggle_origin(5, since=100.0) is None  # not newer than the HA call
    clock[0] = 101.0
    toggle(5)
    assert c.toggle_origin(5, since=100.0) == {"kind": "plejd_device"}


def test_toggle_origin_only_counts_commands_for_the_resulting_state(monkeypatch):
    from plejd_cloud.protocol import Command

    clock = [100.0]
    c, _ = _origin_coordinator(monkeypatch, clock)

    def onoff(address, value):
        c._on_event(
            Command(address=address, command_type=0x10, command=coordinator_mod.CMD_INPUT_BUTTON, data=bytes([value]))
        )

    onoff(5, 0)  # an "off" straight to the light...
    assert c.toggle_origin(5, state="on") is None  # ...doesn't explain it turning on
    assert c.toggle_origin(5, state="off") == {"kind": "plejd_device"}
    onoff(60, 0)  # a separate remote's release still counts either way (press/release, not on/off)
    assert c.toggle_origin(5, state="on") == {"kind": "plejd_input", "name": "Hall switch"}


def test_toggle_origin_newest_command_wins(monkeypatch):
    clock = [100.0]
    c, toggle = _origin_coordinator(monkeypatch, clock)
    toggle(41)  # room command...
    clock[0] = 103.0
    toggle(5)  # ...then one straight to the light: that one explains the change
    assert c.toggle_origin(5) == {"kind": "plejd_device"}
    clock[0] = 104.0
    toggle(60)  # and a remote pressed after both
    assert c.toggle_origin(5) == {"kind": "plejd_input", "name": "Hall switch"}


def test_toggle_origin_ignores_old_commands_and_unknown_addresses(monkeypatch):
    clock = [100.0]
    c, toggle = _origin_coordinator(monkeypatch, clock)
    toggle(41)
    toggle(60)
    clock[0] = 106.0  # past the 5 s window
    assert c.toggle_origin(5) is None
    assert c.toggle_origin(99) is None


def test_toggle_origin_scene_and_motion(monkeypatch):
    import dataclasses

    from plejd_cloud.cloud import PlejdCloudMotion, PlejdCloudScene
    from plejd_cloud.protocol import Command

    clock = [100.0]
    c, _ = _origin_coordinator(monkeypatch, clock)
    c.scenes = [PlejdCloudScene("s1", "Kväll", 3)]
    c.motion = [PlejdCloudMotion("wms", "Hall motion", 33)]
    c._motion_addresses = {33}
    c.devices.append(
        dataclasses.replace(c.devices[0], device_id="wms", address=None, category="motion")
    )  # same room r1

    def event(cmd, address, data):
        c._on_event(Command(address=address, command_type=0x10, command=cmd, data=data))

    event(coordinator_mod.CMD_SCENE, 0, b"\x83")  # the power-off form of scene 3
    assert c.toggle_origin(5) == {"kind": "plejd_scene", "name": "Kväll", "index": 3, "at": 100.0}
    c.scenes = [PlejdCloudScene("s1", "Kväll", 3, output_addresses=[7])]
    assert c.toggle_origin(5) is None  # output 5 isn't in the scene: a manual change at the same moment
    assert c.toggle_origin(7)["name"] == "Kväll"
    event(coordinator_mod.CMD_SCENE, 0, b"\x09")  # an unknown scene index
    assert c.toggle_origin(5)["name"] == "#9"
    clock[0] = 110.0  # scene too old; motion now
    monkeypatch.setattr(
        coordinator_mod, "decode_motion", lambda cmd: types.SimpleNamespace(address=33, motion=True, lux=None)
    )
    event(coordinator_mod.CMD_OUTPUT_SET, 33, b"\x00")
    clock[0] = 150.0  # within the 60 s motion window
    assert c.toggle_origin(5) == {"kind": "plejd_motion", "name": "Hall motion"}
    assert c.toggle_origin(99) is None  # an address with no device/room
    clock[0] = 200.0
    assert c.toggle_origin(5) is None


def test_toggle_origin_since_also_applies_to_scenes_and_motion(monkeypatch):
    import dataclasses

    from plejd_cloud.cloud import PlejdCloudMotion, PlejdCloudScene
    from plejd_cloud.protocol import Command

    clock = [100.0]
    c, _ = _origin_coordinator(monkeypatch, clock)
    c.scenes = [PlejdCloudScene("s1", "Kväll", 3)]
    c.motion = [PlejdCloudMotion("wms", "Hall motion", 33)]
    c._motion_addresses = {33}
    c.devices.append(dataclasses.replace(c.devices[0], device_id="wms", address=None, category="motion"))
    monkeypatch.setattr(
        coordinator_mod, "decode_motion", lambda cmd: types.SimpleNamespace(address=33, motion=True, lux=None)
    )
    c._on_event(Command(address=0, command_type=0x10, command=coordinator_mod.CMD_SCENE, data=b"\x03"))
    c._on_event(Command(address=33, command_type=0x10, command=coordinator_mod.CMD_OUTPUT_SET, data=b"\x00"))
    assert c.toggle_origin(5, since=100.0) is None  # both happened at the HA call, not after it
    assert c.toggle_origin(5, since=99.0) is not None


def test_toggle_origin_uses_a_standalone_motion_sensors_own_room(monkeypatch):
    from plejd_cloud.cloud import PlejdCloudMotion

    clock = [100.0]
    c, _ = _origin_coordinator(monkeypatch, clock)
    c.motion = [PlejdCloudMotion("wms", "Hall motion", 33, room_id="r1")]  # no output entry for it at all
    c._recent_motion[33] = 99.0
    assert c.toggle_origin(5) == {"kind": "plejd_motion", "name": "Hall motion"}


def _climate_coordinator():
    entry = _entry()
    trm = {**_DEV, "device_id": "t1", "name": "Bad", "address": 9, "outputs": [9], "hardware_id": 100}
    entry.data[CONF_DEVICES] = [_DEV, {**trm, "model": "TRM-01", "category": "climate", "traits": 0x20}]
    return PlejdCoordinator(_hass(), entry)


def test_climate_state_for_decodes_the_output_state_report(monkeypatch):
    from plejd_cloud.protocol import OutputState

    c = _climate_coordinator()
    # word 0x1C821: mode 7 (normal), target 32-10 = 22 °C, current 33-10 = 23 °C
    monkeypatch.setattr(c, "state_for", lambda a: OutputState(output=a, on=True, level=0x21, raw_level=0x21C8))
    state = c.climate_state_for(9)
    assert (state.mode, state.target, state.current, state.heating) == (7, 22, 23, None)


def test_climate_state_for_is_none_without_a_raw_level(monkeypatch):
    from plejd_cloud.protocol import OutputState

    c = _climate_coordinator()
    assert c.climate_state_for(9) is None
    monkeypatch.setattr(c, "state_for", lambda a: OutputState(output=a, on=True, level=0))
    assert c.climate_state_for(9) is None


def test_thermostat_report_records_heating_only_for_thermostats(monkeypatch):
    from plejd_cloud.const import CMD_GROUP_STATE_AND_LEVEL
    from plejd_cloud.protocol import Command, OutputState

    c = _climate_coordinator()
    c._on_event(
        Command(address=9, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([1, 0xC8, 0x21, 0x80]))
    )
    c._on_event(Command(address=5, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([1, 0, 200, 0x80])))
    assert c._climate_heating == {9: True}
    monkeypatch.setattr(c, "state_for", lambda a: OutputState(output=a, on=True, level=0x21, raw_level=0x21C8))
    assert c.climate_state_for(9).heating is True
