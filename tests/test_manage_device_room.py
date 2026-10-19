"""Tests for async_move_device_to_room (the HA-facing move-device orchestration)."""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from plejd_cloud.cloud import (
    PlejdAuthError,
    PlejdCloudDevice,
    PlejdCloudError,
    PlejdCloudRoom,
    PlejdCloudRoomInfo,
    PlejdCloudSite,
)
from plejd_cloud.const import CONF_PENDING_ROOM_MOVES
from plejd_cloud.manage_device_room import DATA_PENDING_ROOM_MOVES, _async_persist_pending, async_move_device_to_room

_KEY = bytes(range(16))


def _device(
    device_id="d1", room_id="r1", address=39, outputs=None, dimmable=True, category="light"
) -> PlejdCloudDevice:
    return PlejdCloudDevice(
        device_id=device_id,
        name="Test Light",
        address=address,
        output_index=0,
        outputs=outputs if outputs is not None else [address],
        hardware_id=1,
        model="DIM-02",
        category=category,
        dimmable=dimmable,
        traits=9,
        room_id=room_id,
    )


def _room(room_id, name, address) -> PlejdCloudRoomInfo:
    return PlejdCloudRoomInfo(room_id=room_id, name=name, has_devices=True, address=address)


def _site(devices=None, all_rooms=None, device_addresses=None, rooms=None) -> PlejdCloudSite:
    return PlejdCloudSite(
        site_id="S1",
        title="Home",
        crypto_key=_KEY,
        mesh_key="01-02-03-04",
        devices=devices or [],
        inputs=[],
        motion=[],
        scenes=[],
        gateways=[],
        resource_set_id=None,
        device_addresses=device_addresses or {},
        all_rooms=all_rooms or [],
        rooms=rooms or [],
    )


def _coordinator():
    return types.SimpleNamespace(
        async_leave_mesh_group=AsyncMock(),
        async_join_mesh_group=AsyncMock(),
    )


def _hass():
    return types.SimpleNamespace(
        data={},
        async_block_till_done=AsyncMock(),
        config_entries=types.SimpleNamespace(
            async_update_entry=lambda entry, data: setattr(entry, "data", data),
            async_reload=AsyncMock(),
        ),
    )


def _entry(data=None, runtime_data=None):
    return types.SimpleNamespace(
        entry_id="e1",
        data=data or {"email": "u@x.com", "password": "pw", "site_id": "S1"},
        runtime_data=runtime_data or _coordinator(),
        async_start_reauth=lambda hass: None,
    )


async def test_move_device_raises_if_device_not_found(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=_site()))
    with pytest.raises(HomeAssistantError, match="not found"):
        await async_move_device_to_room(_hass(), _entry(), device_id="missing", room_id="r2")


async def test_move_device_raises_on_login_failure(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(side_effect=PlejdCloudError("down")))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error"):
        await async_move_device_to_room(_hass(), _entry(), device_id="d1", room_id="r2")


async def test_move_device_triggers_reauth_on_stale_credentials(monkeypatch):
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_login", AsyncMock(side_effect=PlejdAuthError("bad creds"))
    )
    hass = _hass()
    entry = _entry()
    entry.async_start_reauth = MagicMock()
    with pytest.raises(HomeAssistantError, match="reauthentication started"):
        await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")
    entry.async_start_reauth.assert_called_once_with(hass)


async def test_move_device_raises_on_get_site_failure(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=PlejdCloudError("down")))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error"):
        await async_move_device_to_room(_hass(), _entry(), device_id="d1", room_id="r2")


async def test_move_device_raises_for_a_multi_output_device(monkeypatch):
    site = _site(
        devices=[_device(device_id="d1", room_id="r1"), _device(device_id="d1", room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=site))
    with pytest.raises(HomeAssistantError, match="multiple outputs"):
        await async_move_device_to_room(_hass(), _entry(), device_id="d1", room_id="r2")


async def test_move_device_raises_for_a_single_record_with_multiple_output_addresses(monkeypatch):
    # A single site.devices record can itself list every output address for the device
    # (PlejdCloudDevice.outputs, built from outputAddress) - the multi-output rejection
    # must catch this shape too, not just multiple separate records.
    site = _site(
        devices=[_device(device_id="d1", room_id="r1", outputs=[39, 40])],
        device_addresses={"d1": 39},
        all_rooms=[_room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=site))
    with pytest.raises(HomeAssistantError, match="multiple outputs"):
        await async_move_device_to_room(_hass(), _entry(), device_id="d1", room_id="r2")


async def test_move_device_raises_for_a_device_with_no_controllable_output(monkeypatch):
    # A real physical device (a motion sensor, an input-only device, a gateway) has an
    # entry in device_addresses but none in site.devices.
    site = _site(devices=[], device_addresses={"d1": 39})
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=site))
    with pytest.raises(HomeAssistantError, match="no controllable output"):
        await async_move_device_to_room(_hass(), _entry(), device_id="d1", room_id="r2")


async def test_move_device_raises_if_room_not_found(monkeypatch):
    site = _site(devices=[_device()], device_addresses={"d1": 39})
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=site))
    with pytest.raises(HomeAssistantError, match="not found"):
        await async_move_device_to_room(_hass(), _entry(), device_id="d1", room_id="missing")


async def test_move_device_raises_if_destination_room_has_no_address(monkeypatch):
    site = _site(
        devices=[_device()],
        device_addresses={"d1": 39},
        all_rooms=[_room("r2", "Room B", address=None)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=site))
    with pytest.raises(HomeAssistantError, match="no mesh group address"):
        await async_move_device_to_room(_hass(), _entry(), device_id="d1", room_id="r2")


async def test_move_device_raises_if_already_in_destination_room(monkeypatch):
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=site))
    with pytest.raises(HomeAssistantError, match="already in room"):
        await async_move_device_to_room(_hass(), _entry(), device_id="d1", room_id="r1")


async def test_move_device_leaves_old_room_and_joins_new(monkeypatch):
    hass = _hass()
    coordinator = _coordinator()
    entry = _entry(runtime_data=coordinator)
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    coordinator.async_leave_mesh_group.assert_awaited_once_with(39, 14)
    coordinator.async_join_mesh_group.assert_awaited_once_with(39, 34)
    hass.config_entries.async_reload.assert_awaited_once_with("e1")
    moved = next(d for d in entry.data["devices"] if d["device_id"] == "d1")
    assert moved["room_id"] == "r2"


async def test_move_device_does_not_double_reload_across_its_two_persists(monkeypatch):
    # A successful move does two separate async_update_entry calls (the early pending-only
    # persist, then the full one), each its own separate reload-lock acquisition. The real
    # update listener (not an injected flag) must recognize both as self-triggered and
    # never fire its own competing reload for either.
    from plejd_cloud import _async_reload_entry

    hass = _hass()
    entry = _entry()
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    listener_tasks: list[asyncio.Task] = []

    def _update_entry(e, data):
        e.data = data
        # Real HA schedules the update listener as a new task rather than running it
        # inline - mirror that so it actually races each reload below, not just a flag.
        listener_tasks.append(asyncio.ensure_future(_async_reload_entry(hass, e)))

    hass.config_entries.async_update_entry = _update_entry

    real_reload = AsyncMock(return_value=True)

    async def _reload(entry_id):
        await asyncio.sleep(0)  # let any just-scheduled listener task run while still locked
        return await real_reload(entry_id)

    hass.config_entries.async_reload = AsyncMock(side_effect=_reload)

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")
    await asyncio.gather(*listener_tasks)

    # _async_persist_pending's own write needs no reload of its own (no pending change was
    # detected during its lock session) - only _async_refresh_and_reload's real reload runs.
    # The listener deferred to both writes' lock sessions instead of racing a reload of its own.
    real_reload.assert_awaited_once_with("e1")


async def test_move_device_joins_via_the_coordinator_current_at_join_time(monkeypatch):
    # entry.runtime_data must be read fresh for EACH mesh write, not cached once for both -
    # simulates an unrelated reload (e.g. an options edit) swapping in a new coordinator
    # in the gap between the leave and the join, and confirms the join goes to the NEW one.
    hass = _hass()
    old_coordinator = _coordinator()
    new_coordinator = _coordinator()
    entry = _entry(runtime_data=old_coordinator)

    async def _leave_then_an_unrelated_reload_swaps_the_coordinator(*_args, **_kwargs):
        entry.runtime_data = new_coordinator

    old_coordinator.async_leave_mesh_group.side_effect = _leave_then_an_unrelated_reload_swaps_the_coordinator

    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    old_coordinator.async_leave_mesh_group.assert_awaited_once_with(39, 14)
    old_coordinator.async_join_mesh_group.assert_not_awaited()  # torn down by the time join runs
    new_coordinator.async_join_mesh_group.assert_awaited_once_with(39, 34)


async def test_move_device_patches_room_membership_when_both_rooms_already_exist(monkeypatch):
    hass = _hass()
    entry = _entry()
    old_room = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39, 41], dimmable=True, dimmable_addresses=[39, 41]
    )
    new_room = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[50], dimmable=True, dimmable_addresses=[50]
    )
    unrelated_room = PlejdCloudRoom(
        room_id="r3", name="Garage", address=17, member_addresses=[60], dimmable=True, dimmable_addresses=[60]
    )
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
        rooms=[old_room, new_room, unrelated_room],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert rooms_by_id["r1"]["member_addresses"] == [41]
    assert rooms_by_id["r1"]["dimmable_addresses"] == [41]
    assert rooms_by_id["r2"]["member_addresses"] == [39, 50]
    assert rooms_by_id["r2"]["dimmable_addresses"] == [39, 50]
    assert rooms_by_id["r3"]["member_addresses"] == [60]  # unrelated room passed through unchanged


async def test_move_device_leaves_room_membership_untouched_for_a_non_light_device(monkeypatch):
    # PlejdCloudRoom/PlejdRoomLight is a light-only aggregate - parse_site() excludes a
    # group entirely if it has any non-light member, since a group command would hit it
    # too. Adding a non-light device's output to member_addresses here would recreate
    # exactly that unsafe case on reload.
    hass = _hass()
    entry = _entry()
    old_room = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39, 41], dimmable=True, dimmable_addresses=[41]
    )
    site = _site(
        devices=[_device(room_id="r1", category="switch", dimmable=False)],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
        rooms=[old_room],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert rooms_by_id == {"r1": {**vars(old_room)}}  # untouched: still lists 39, no r2 entry added


async def test_move_device_drops_an_existing_destination_room_light_for_a_non_light_move(monkeypatch):
    # Unlike the sibling test above (no r2 entry existed yet), here the destination
    # already has a cached light-group entity with OTHER light members. The mesh group
    # now genuinely contains a non-light member too - the stale "light-only" cache must
    # be dropped, not left in place, or its room light would keep broadcasting to it.
    hass = _hass()
    entry = _entry()
    old_room = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39], dimmable=False, dimmable_addresses=[]
    )
    destination_room = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[50], dimmable=True, dimmable_addresses=[50]
    )
    site = _site(
        devices=[_device(room_id="r1", category="switch", dimmable=False)],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
        rooms=[old_room, destination_room],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    room_ids = {r["room_id"] for r in entry.data["rooms"]}
    assert room_ids == {"r1"}  # r2's now-unsafe light-group entity is dropped entirely


async def test_move_device_prunes_a_non_light_pending_move_on_room_id_convergence_alone(monkeypatch):
    # A non-light move has no room-group membership to converge (the patch only ever
    # drops a stale cached entry for it) - room_id convergence alone must be enough to
    # prune its pending entry, without waiting on a membership signal that will never come.
    hass = _hass()
    entry = _entry()
    site = _site(
        devices=[_device(room_id="r1", category="switch", dimmable=False)],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")
    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]

    # A later, unrelated move: the cloud now reports "d1" already in "r2".
    converged_site = _site(
        devices=[
            _device(device_id="d1", room_id="r2", category="switch", dimmable=False),
            _device(device_id="e", room_id="r1", address=99),
        ],
        device_addresses={"d1": 39, "e": 99},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[converged_site, converged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="e", room_id="r2")

    assert "d1" not in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # pruned: room_id alone was enough


async def test_move_device_prunes_a_pending_move_for_a_device_removed_from_the_site(monkeypatch):
    # A device removed from the site entirely (e.g. via remove_device) before its pending
    # move converges never appears in fresh_site.devices at all - the pruning loop (which
    # only iterates that list) would never visit it, so without an explicit check it would
    # sit in `pending` forever, with its membership patch still being re-applied for a
    # device that no longer exists.
    hass = _hass()
    entry = _entry()
    r1 = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
        rooms=[r1],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")
    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]

    # A later, unrelated move: d1 has since been removed from the site entirely - it's
    # absent from fresh_site.devices, but the cloud's stale room aggregate still lists it.
    removed_site = _site(
        devices=[_device(device_id="e", room_id="r1", address=99)],
        device_addresses={"e": 99},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
        rooms=[r1],
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[removed_site, removed_site])
    )
    await async_move_device_to_room(hass, entry, device_id="e", room_id="r2")

    assert "d1" not in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # pruned: no longer on the site at all
    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert rooms_by_id["r1"]["member_addresses"] == [39]  # untouched: d1's stale entry isn't ours to patch anymore


async def test_move_device_leaves_room_membership_untouched_when_output_address_unresolved(monkeypatch):
    # A device whose own mesh address is known (own_address, from deviceAddress) but whose
    # SPECIFIC output has no resolvable address (e.g. an explicit null in outputAddress)
    # can't be safely keyed into member_addresses at all.
    hass = _hass()
    entry = _entry()
    old_room = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[41], dimmable=True, dimmable_addresses=[41]
    )
    site = _site(
        devices=[_device(room_id="r1", address=None, outputs=[])],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
        rooms=[old_room],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert rooms_by_id == {"r1": {**vars(old_room)}}  # left untouched, nothing resolvable to key off


async def test_move_device_drops_destination_room_for_a_non_light_device_with_unresolved_output_address(monkeypatch):
    # Even with no resolvable output_address, the mesh join command still targets the
    # device's own mesh address, so a non-light device genuinely joins the destination's
    # mesh group - the existing (now unsafe) destination room-light entity must still be
    # dropped, the same as when the output address IS resolvable. This exercises the fix
    # for hitting the output_address-is-None early-return before the non-light branch ran.
    hass = _hass()
    entry = _entry()
    old_room = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[41], dimmable=True, dimmable_addresses=[41]
    )
    destination_room = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[50], dimmable=True, dimmable_addresses=[50]
    )
    site = _site(
        devices=[_device(room_id="r1", address=None, outputs=[], category="switch", dimmable=False)],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
        rooms=[old_room, destination_room],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    room_ids = {r["room_id"] for r in entry.data["rooms"]}
    assert room_ids == {"r1"}  # r2's now-unsafe light-group entity is dropped, r1 untouched


async def test_move_device_prunes_pending_move_with_unresolved_output_address_on_room_id_alone(monkeypatch):
    # With no output address to key membership off, there's nothing for
    # _room_membership_converged to check beyond room_id - it must not block pruning
    # forever waiting on a membership signal that can never arrive for this device.
    hass = _hass()
    entry = _entry()
    site = _site(
        devices=[_device(room_id="r1", address=None, outputs=[])],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")
    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]

    # A later, unrelated move: the cloud now reports "d1" already in "r2".
    converged_site = _site(
        devices=[
            _device(device_id="d1", room_id="r2", address=None, outputs=[]),
            _device(device_id="e", room_id="r1", address=99),
        ],
        device_addresses={"d1": 39, "e": 99},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[converged_site, converged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="e", room_id="r2")

    assert "d1" not in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # pruned: room_id alone was enough


async def test_move_device_keeps_a_non_light_pending_move_with_unresolved_output_address_until_room_entity_drops(
    monkeypatch,
):
    # Unlike the light case above, a non-light move with an unresolved output_address is
    # NOT trivially converged just because room_id matches - _patch_room_membership keeps
    # dropping a stale destination room-light entity regardless of output_address (the join
    # always targets the device's own address), so convergence must wait for that entity to
    # actually disappear, or the drop stops being re-applied and the stale entity reappears.
    hass = _hass()
    entry = _entry()
    site = _site(
        devices=[_device(room_id="r1", address=None, outputs=[], category="switch", dimmable=False)],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")
    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]

    # A later, unrelated move: room_id has converged, but r2's light-group entity (with some
    # OTHER member, not d1's non-light/unresolved output) still exists in the fresh fetch.
    destination_room = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[50], dimmable=True, dimmable_addresses=[50]
    )
    converged_site = _site(
        devices=[
            _device(device_id="d1", room_id="r2", address=None, outputs=[], category="switch", dimmable=False),
            _device(device_id="e", room_id="r1", address=99),
        ],
        device_addresses={"d1": 39, "e": 99},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
        rooms=[destination_room],
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[converged_site, converged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="e", room_id="r2")

    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # destination entity still exists: not converged
    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert "r2" not in rooms_by_id  # drop still re-applied: r2's stale light-group entity removed again


async def test_move_device_does_not_synthesize_a_room_not_already_present(monkeypatch):
    # parse_site() can exclude a room from `rooms` for a real reason (no other light
    # member, or a non-light member sharing its group address) that this function has no
    # way to distinguish from "just empty" - it must never invent a new entry.
    hass = _hass()
    entry = _entry()
    old_room = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
        rooms=[old_room],  # r2 (the destination) has no light-group entity yet
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    room_ids = {r["room_id"] for r in entry.data["rooms"]}
    # r1 is dropped too (its last member just left - matches parse_site()'s own "no
    # controllable outputs" exclusion), and no r2 entry is synthesized in its place.
    assert room_ids == set()


async def test_move_device_forces_local_room_id_when_cloud_has_not_converged(monkeypatch):
    # A BLE-only site (no gateway) has no path for the cloud to learn about a mesh-only
    # room change - simulate the refresh fetch still reporting the device's OLD room, and
    # confirm the persisted data is corrected to the intended destination regardless.
    hass = _hass()
    entry = _entry()
    initial_site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    stale_refresh_site = _site(
        devices=[_device(room_id="r1")],  # still "r1" - the cloud hasn't converged yet
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[initial_site, stale_refresh_site])
    )

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    moved = next(d for d in entry.data["devices"] if d["device_id"] == "d1")
    assert moved["room_id"] == "r2"


async def test_move_device_uses_cloud_room_for_a_device_it_has_never_moved(monkeypatch):
    # A device this integration has no pending move for must trust the fresh cloud
    # fetch's room_id directly, not whatever stale entry.data happens to already say -
    # e.g. if the device was instead moved via the app itself since the last reload, the
    # cloud (not our own state) is the authoritative source for its current room.
    hass = _hass()
    coordinator = _coordinator()
    entry = _entry(
        runtime_data=coordinator,
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "devices": [{"device_id": "d1", "room_id": "r1"}],  # stale: app already moved it to r3
        },
    )
    site = _site(
        devices=[_device(room_id="r3")],  # the cloud correctly reports the device's real current room
        device_addresses={"d1": 39},
        all_rooms=[
            _room("r1", "Room A", address=14),
            _room("r2", "Room B", address=34),
            _room("r3", "Sovrum", address=46),
        ],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    coordinator.async_leave_mesh_group.assert_awaited_once_with(39, 46)  # r3's address, not stale r1's
    coordinator.async_join_mesh_group.assert_awaited_once_with(39, 34)


async def test_move_device_restores_a_pending_move_from_entry_data_after_a_restart(monkeypatch):
    # Simulates an HA restart: hass.data has no in-memory pending cache at all (a brand
    # new dict, never touched by this entry_id), but entry.data still holds a move this
    # integration made and persisted (via CONF_PENDING_ROOM_MOVES) before the restart. A
    # second move must still leave the room the FIRST move actually corrected it to (r2),
    # not whatever the (not-yet-converged) fresh cloud fetch still reports (r1).
    hass = _hass()
    coordinator = _coordinator()
    entry = _entry(
        runtime_data=coordinator,
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            CONF_PENDING_ROOM_MOVES: {
                "d1": {
                    "room_id": "r2",
                    "output_address": 39,
                    "is_light": True,
                    "dimmable": True,
                    "new_room_address": 34,
                    "from_room_id": "r1",
                }
            },
        },
    )
    site = _site(
        devices=[_device(room_id="r1", address=39)],  # cloud hasn't converged yet: still says r1
        device_addresses={"d1": 39},
        all_rooms=[
            _room("r1", "Room A", address=14),
            _room("r2", "Room B", address=34),
            _room("r3", "Sovrum", address=46),
        ],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r3")

    coordinator.async_leave_mesh_group.assert_awaited_once_with(39, 34)  # r2 (persisted), not stale r1
    coordinator.async_join_mesh_group.assert_awaited_once_with(39, 46)


async def test_move_device_preserves_an_earlier_pending_move_for_another_device(monkeypatch):
    # On a BLE-only site, refreshing for a second device's move must not revert an
    # earlier, still-unconverged move's own correction back to the stale cloud value.
    hass = _hass()
    entry = _entry()
    rooms_by_id = {
        "r1": _room("r1", "Room A", address=14),
        "r2": _room("r2", "Room B", address=34),
        "r3": _room("r3", "Sovrum", address=46),
    }

    device_a_site = _site(
        devices=[_device(device_id="a", room_id="r1", address=39)],
        device_addresses={"a": 39},
        all_rooms=list(rooms_by_id.values()),
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[device_a_site, device_a_site])
    )
    await async_move_device_to_room(hass, entry, device_id="a", room_id="r2")
    moved_a = next(d for d in entry.data["devices"] if d["device_id"] == "a")
    assert moved_a["room_id"] == "r2"

    # Now move a second device ("b") - the cloud fetch still (stale) reports "a" in "r1".
    both_devices_stale_site = _site(
        devices=[
            _device(device_id="a", room_id="r1", address=39),  # still stale from the cloud's perspective
            _device(device_id="b", room_id="r1", address=51),
        ],
        device_addresses={"a": 39, "b": 51},
        all_rooms=list(rooms_by_id.values()),
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site",
        AsyncMock(side_effect=[both_devices_stale_site, both_devices_stale_site]),
    )
    await async_move_device_to_room(hass, entry, device_id="b", room_id="r3")

    devices_by_id = {d["device_id"]: d for d in entry.data["devices"]}
    assert devices_by_id["a"]["room_id"] == "r2"  # not reverted to the stale cloud's "r1"
    assert devices_by_id["b"]["room_id"] == "r3"
    assert "a" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # still unconverged

    # A third move, of an untouched device "d" - the cloud has now finally converged for
    # "a" (matches its pending override on BOTH room_id and room-group membership) and
    # correctly reports an unrelated device "d" that was never moved at all.
    converged_site = _site(
        devices=[
            _device(device_id="a", room_id="r2", address=39),  # now matches pending - converged
            _device(device_id="b", room_id="r1", address=51),
            _device(device_id="d", room_id="r1", address=42),  # never moved; no pending entry
        ],
        device_addresses={"a": 39, "b": 51, "d": 42},
        all_rooms=list(rooms_by_id.values()),
        rooms=[
            PlejdCloudRoom(
                room_id="r2",
                name="Room B",
                address=34,
                member_addresses=[39],
                dimmable=True,
                dimmable_addresses=[39],
            ),
        ],
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[converged_site, converged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="b", room_id="r1")

    assert "a" not in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # pruned: cloud converged
    devices_by_id = {d["device_id"]: d for d in entry.data["devices"]}
    assert devices_by_id["a"]["room_id"] == "r2"
    assert devices_by_id["d"]["room_id"] == "r1"  # passed through untouched, no override existed


async def test_move_device_reapplies_room_membership_for_every_pending_device(monkeypatch):
    # This always starts fresh from the cloud's own rooms snapshot each call - refreshing
    # for one device's move must re-apply an EARLIER, still-unconverged device's own
    # room-membership patch too, not just persist its roomId (see the device_id test
    # above) - or the earlier move's membership correction gets silently wiped out.
    hass = _hass()
    entry = _entry()
    all_rooms = [
        _room("r1", "Room A", address=14),
        _room("r2", "Room B", address=34),
        _room("r3", "Sovrum", address=46),
    ]
    r1 = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39, 51], dimmable=True, dimmable_addresses=[39, 51]
    )
    r2 = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[60], dimmable=True, dimmable_addresses=[60]
    )
    stale_site = _site(
        devices=[
            _device(device_id="a", room_id="r1", address=39),
            _device(device_id="b", room_id="r1", address=51),
        ],
        device_addresses={"a": 39, "b": 51},
        all_rooms=all_rooms,
        rooms=[r1, r2],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[stale_site, stale_site])
    )
    await async_move_device_to_room(hass, entry, device_id="a", room_id="r2")

    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert rooms_by_id["r1"]["member_addresses"] == [51]
    assert rooms_by_id["r2"]["member_addresses"] == [39, 60]

    # Move "b" too - the cloud fetch is the SAME stale snapshot, with no idea "a" ever
    # moved (still shows r1=[39, 51], r2=[60]).
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[stale_site, stale_site])
    )
    await async_move_device_to_room(hass, entry, device_id="b", room_id="r3")

    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert "r1" not in rooms_by_id  # both a and b have left - dropped, matching parse_site()
    assert rooms_by_id["r2"]["member_addresses"] == [39, 60]  # a's earlier move still applied
    assert "r3" not in rooms_by_id  # no pre-existing entry to patch - declined synthesis


async def test_move_device_records_pending_before_a_refresh_can_fail(monkeypatch):
    # The mesh writes (leave + join) have already physically happened by the time the
    # refresh runs - if the refresh then fails, the pending cache must still reflect the
    # move, or a follow-up move to a THIRD room would derive "current room" from the stale
    # pre-move cloud data and never send a leave for the room the device is actually
    # already in, leaving it joined to both that room and the new one.
    hass = _hass()
    coordinator = _coordinator()
    entry = _entry(runtime_data=coordinator)
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[
            _room("r1", "Room A", address=14),
            _room("r2", "Room B", address=34),
            _room("r3", "Sovrum", address=46),
        ],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, PlejdCloudError("down")])
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error refreshing site"):
        await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    # The mesh write already succeeded despite the refresh failing - pending must reflect it,
    # in memory AND persisted to entry.data (so a restart before another move doesn't lose it).
    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]
    assert "d1" in entry.data[CONF_PENDING_ROOM_MOVES]
    coordinator.async_leave_mesh_group.assert_awaited_once_with(39, 14)
    coordinator.async_join_mesh_group.assert_awaited_once_with(39, 34)

    # An exact retry of the same move now correctly reports "already in room" - the device
    # really is already there, physically, even though entry.data's own cached view (never
    # updated, since the refresh failed) still shows it in r1.
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))
    with pytest.raises(HomeAssistantError, match="already in room"):
        await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    # A move to a THIRD room, however, must leave r2 (where the device actually now is),
    # not stale r1 - the fresh site fetch below still (incorrectly) reports r1, exactly as
    # it would if the failed refresh above had simply never run.
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r3")

    coordinator.async_leave_mesh_group.assert_awaited_with(39, 34)  # r2, not stale r1
    coordinator.async_join_mesh_group.assert_awaited_with(39, 46)


async def test_move_device_preserves_the_original_stale_room_across_a_chain_of_moves(monkeypatch):
    # Moving the SAME device three times before the cloud converges even the first hop: the
    # membership patch must still target the ORIGINAL room (what the stale cloud snapshot
    # actually shows), not any intermediate hop, or the original room's stale entry would
    # never get cleaned up - and the staleness-detection revalidation must not mistake a
    # fetch still stuck on the ORIGINAL room for proof of an external change partway
    # through the chain (from_room_id must track the chain's true start, not the previous
    # hop's own target).
    hass = _hass()
    coordinator = _coordinator()
    entry = _entry(runtime_data=coordinator)
    all_rooms = [
        _room("r1", "Room A", address=14),
        _room("r2", "Room B", address=34),
        _room("r3", "Sovrum", address=46),
        _room("r4", "Kontor", address=50),
    ]
    r1 = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    stale_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
        rooms=[r1],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[stale_site, stale_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    # Move again, r2 -> r3, before the cloud has any idea about EITHER move - it still
    # shows the device in the ORIGINAL r1, never having converged even the first hop.
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[stale_site, stale_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r3")

    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert rooms_by_id == {}  # r1 correctly stripped despite the cloud never showing the move

    # A THIRD hop, r3 -> r4, with the cloud STILL stuck on the very original r1: this fetch
    # matches neither r3 (this pending's own target) nor r2 (the second move's own
    # immediate predecessor) - only the chain's TRUE origin, r1, proves it's still the same
    # ordinary non-convergence, not an external change.
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[stale_site, stale_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r4")

    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # not wrongly treated as externally stale
    coordinator.async_leave_mesh_group.assert_awaited_with(39, 46)  # r3 - the actual current room, not stale r1
    coordinator.async_join_mesh_group.assert_awaited_with(39, 50)


async def test_move_device_prunes_a_pending_move_that_has_actually_converged(monkeypatch):
    # r1 -> r2 converges (both room_id and membership) before this device is ever moved
    # again - the pending entry must be recognized as confirmed and dropped right at the
    # start of the next call, using the fresh fetch already in hand, rather than trusting
    # it blindly forever (nothing else in the integration revisits it on its own).
    hass = _hass()
    entry = _entry()
    all_rooms = [
        _room("r1", "Room A", address=14),
        _room("r2", "Room B", address=34),
        _room("r3", "Sovrum", address=46),
    ]
    unconverged_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[unconverged_site, unconverged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")
    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]

    # Time passes; the cloud fully converges to r2 (room_id AND membership) with no other
    # move ever happening in between. The next move (r2 -> r3) must leave r2 - the room the
    # fresh fetch now genuinely agrees the device is in - proving it wasn't left stuck
    # treating "r1" as though it were still the current room.
    r2 = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    converged_site = _site(
        devices=[_device(room_id="r2", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
        rooms=[r2],
    )
    coordinator = entry.runtime_data
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[converged_site, converged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r3")

    coordinator.async_leave_mesh_group.assert_awaited_with(39, 34)  # r2 - the real, converged current room
    coordinator.async_join_mesh_group.assert_awaited_with(39, 46)


async def test_move_device_persists_a_prune_before_an_already_in_room_exit(monkeypatch):
    # r1 -> r2 converges, then a redundant request to move the SAME device to r2 again -
    # the pending entry gets pruned (converged) right before this raises "already in room",
    # an early return that never reaches _async_refresh_and_reload's own persist. Without
    # persisting the prune immediately, a restart before any other successful move would
    # reseed entry.data's stale copy of the pruned entry and undo it.
    hass = _hass()
    entry = _entry()
    all_rooms = [_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)]
    unconverged_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[unconverged_site, unconverged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")
    assert "d1" in entry.data[CONF_PENDING_ROOM_MOVES]

    # The cloud has since fully converged to r2; a redundant request to r2 again finds the
    # pending entry confirmed-converged, prunes it, then raises "already in room" - all
    # within the SAME call, before _async_refresh_and_reload ever runs.
    r2 = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    converged_site = _site(
        devices=[_device(room_id="r2", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
        rooms=[r2],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=converged_site))
    with pytest.raises(HomeAssistantError, match="already in room"):
        await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    assert "d1" not in entry.data[CONF_PENDING_ROOM_MOVES]  # pruned and persisted despite the raise
    assert "d1" not in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]


async def test_move_device_runs_a_follow_up_reload_after_persisting_a_prune(monkeypatch):
    # A concurrent options change racing the guarded persist-only update (from the prune
    # above) must not be silently dropped - its own reload is deferred while our guard is
    # up, then run as a follow-up once we're done, the same as the main refresh cycle does.
    from plejd_cloud import schedule_ws

    hass = _hass()
    entry = _entry()
    all_rooms = [_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)]
    unconverged_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[unconverged_site, unconverged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    r2 = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    converged_site = _site(
        devices=[_device(room_id="r2", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
        rooms=[r2],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=converged_site))

    original_update_entry = hass.config_entries.async_update_entry

    def _update_then_mark_concurrent_reload_pending(entry, data):
        original_update_entry(entry, data)
        hass.data[schedule_ws.DATA_RELOAD_PENDING] = entry.entry_id

    hass.config_entries.async_update_entry = _update_then_mark_concurrent_reload_pending
    hass.config_entries.async_reload.reset_mock()  # ignore the first (successful) move's own reload

    with pytest.raises(HomeAssistantError, match="already in room"):
        await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    hass.config_entries.async_reload.assert_awaited_once_with("e1")
    assert schedule_ws.DATA_RELOAD_PENDING not in hass.data


async def test_move_device_detects_a_pending_move_made_stale_by_a_later_app_move(monkeypatch):
    # Codex-reported scenario: r1 -> r2 converges, then the Plejd app itself moves the
    # device again (r2 -> r3) before this integration ever revisits it. The pending entry
    # (still "r2") no longer matches EITHER the room it started from (r1) or the room the
    # fresh cloud now reports (r3) - that mismatch is the signal that something else moved
    # it since, and the fresh fetch must be trusted instead of the stale pending value.
    hass = _hass()
    entry = _entry()
    all_rooms = [
        _room("r1", "Room A", address=14),
        _room("r2", "Room B", address=34),
        _room("r3", "Sovrum", address=46),
        _room("r4", "Kontor", address=50),
    ]
    unconverged_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[unconverged_site, unconverged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    # The app moved it again, to r3, after our own r1 -> r2 move fully converged. Neither
    # r1 (from_room_id) nor r2 (pending's own target) match this fresh room_id.
    coordinator = entry.runtime_data
    app_moved_site = _site(
        devices=[_device(room_id="r3", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[app_moved_site, app_moved_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r4")

    coordinator.async_leave_mesh_group.assert_awaited_with(39, 46)  # r3 - the real current room, not stale r2
    coordinator.async_join_mesh_group.assert_awaited_with(39, 50)


async def test_move_device_keeps_pending_when_room_id_converged_but_membership_has_not(monkeypatch):
    # room_id reaching the target while membership is still stale is ORDINARY partial
    # convergence (already exercised elsewhere for a DIFFERENT device's refresh), not proof
    # of an external change - a repeat call for the SAME device in that window must not
    # drop the pending entry, or the membership patch that's still needed stops being
    # re-applied on every later refresh (Codex's exact reported scenario).
    hass = _hass()
    entry = _entry()
    all_rooms = [_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)]
    unconverged_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[unconverged_site, unconverged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    # A redundant repeat request to the SAME destination: room_id has converged to r2, but
    # r2's own room-group membership hasn't caught up yet (no r2 entry in `rooms` at all).
    room_id_converged_site = _site(
        devices=[_device(room_id="r2", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=room_id_converged_site))
    with pytest.raises(HomeAssistantError, match="already in room"):
        await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    # The pending entry must survive this - dropping it here would lose the only state that
    # keeps r1's stale membership patch (and r2's eventual add) re-applied on later refreshes.
    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]


async def test_move_device_strips_the_room_actually_left_when_membership_outpaces_room_id(monkeypatch):
    # The opposite ordering from the test above: r1 -> r2's room-group MEMBERSHIP has
    # already converged (the cloud's rooms snapshot now lists the device under r2) even
    # though roomId hasn't yet. A second move (r2 -> r3) must strip the device from r2
    # (where the cloud now ACTUALLY shows it), not from the original r1 - a fixed "old
    # room" would incorrectly leave it cached as still belonging to r2.
    hass = _hass()
    entry = _entry()
    all_rooms = [
        _room("r1", "Room A", address=14),
        _room("r2", "Room B", address=34),
        _room("r3", "Sovrum", address=46),
    ]
    r1 = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    initial_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
        rooms=[r1],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[initial_site, initial_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    # Membership converged for the first move (r2 now lists 39) but roomId didn't (the
    # device's own cloud record still says "r1").
    r2_converged = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    partially_converged_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
        rooms=[r2_converged],  # r1 no longer lists it at all - only r2 does
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site",
        AsyncMock(side_effect=[partially_converged_site, partially_converged_site]),
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r3")

    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert "r2" not in rooms_by_id  # correctly stripped from where the cloud NOW shows it


async def test_move_device_keeps_pending_when_only_room_id_has_converged(monkeypatch):
    # A light move's room_id can converge slightly before its room-group membership does -
    # pruning the pending entry on room_id alone would stop re-patching a still-stale room.
    hass = _hass()
    entry = _entry()
    all_rooms = [_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)]
    r1 = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    initial_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
        rooms=[r1],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[initial_site, initial_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    # A later, unrelated move: room_id has converged (the cloud now shows "d1" in "r2")
    # but room-group membership hasn't caught up yet - r1 still lists its output (39).
    partially_converged_site = _site(
        devices=[_device(device_id="d1", room_id="r2", address=39), _device(device_id="e", room_id="r1", address=99)],
        device_addresses={"d1": 39, "e": 99},
        all_rooms=all_rooms,
        rooms=[r1],
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site",
        AsyncMock(side_effect=[partially_converged_site, partially_converged_site]),
    )
    await async_move_device_to_room(hass, entry, device_id="e", room_id="r2")

    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # membership still unconverged
    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert "r1" not in rooms_by_id  # patch still re-applied: 39 removed, emptying and dropping it


async def test_move_device_keeps_pending_when_still_listed_in_another_room(monkeypatch):
    # Membership converging means "added to the new room AND removed from every other
    # one" - being added to r2 while still (also) listed in r1 is only half-converged and
    # must not be pruned yet, or the r1 patch (dropping the now-stale member) would stop
    # being re-applied.
    hass = _hass()
    entry = _entry()
    all_rooms = [_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)]
    r1 = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    initial_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
        rooms=[r1],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[initial_site, initial_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    # A later, unrelated move: the cloud now shows 39 as a member of BOTH r1 and r2 - only
    # half-converged, since a device only ever belongs to one room by the app's convention.
    r1_still_listing_it = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    r2_now_listing_it = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    partially_converged_site = _site(
        devices=[_device(device_id="d1", room_id="r2", address=39), _device(device_id="e", room_id="r1", address=99)],
        device_addresses={"d1": 39, "e": 99},
        all_rooms=all_rooms,
        rooms=[r1_still_listing_it, r2_now_listing_it],
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site",
        AsyncMock(side_effect=[partially_converged_site, partially_converged_site]),
    )
    await async_move_device_to_room(hass, entry, device_id="e", room_id="r2")

    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # still listed elsewhere: not converged
    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert "r1" not in rooms_by_id  # patch still re-applied: 39 stripped from r1 again


async def test_move_device_prunes_a_light_pending_move_into_a_permanently_mixed_room(monkeypatch):
    # A light moved into a room that already has a non-light member is a PERMANENT edge
    # case, not "hasn't converged yet": parse_site() excludes any group with a non-light
    # member from `rooms` entirely, so the destination can never appear there no matter how
    # long we wait. Convergence must be decided from the raw device list instead (a
    # non-light device already reporting that room_id) combined with the light being
    # absent from every other room - or this pending entry would be stuck forever.
    hass = _hass()
    entry = _entry()
    all_rooms = [_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)]
    r1 = PlejdCloudRoom(
        room_id="r1", name="Room A", address=14, member_addresses=[39], dimmable=True, dimmable_addresses=[39]
    )
    initial_site = _site(
        devices=[_device(room_id="r1", address=39)],
        device_addresses={"d1": 39},
        all_rooms=all_rooms,
        rooms=[r1],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[initial_site, initial_site])
    )
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    # A later, unrelated move: room_id has converged, r2 now genuinely has a non-light
    # member (a switch) - so parse_site() excludes it from `rooms` entirely (permanently,
    # not just "not yet"), and the light (39) is no longer listed in any OTHER room either.
    switch_in_r2 = _device(device_id="s", room_id="r2", address=51, category="switch", dimmable=False)
    converged_site = _site(
        devices=[
            _device(device_id="d1", room_id="r2", address=39),
            switch_in_r2,
            _device(device_id="e", room_id="r1", address=99),
        ],
        device_addresses={"d1": 39, "s": 51, "e": 99},
        all_rooms=all_rooms,
        rooms=[],  # r2 excluded entirely: mixed light + non-light membership
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[converged_site, converged_site])
    )
    await async_move_device_to_room(hass, entry, device_id="e", room_id="r2")

    assert "d1" not in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # pruned: permanently excluded, not stuck


async def test_move_device_keeps_a_non_light_pending_move_while_destination_room_entity_exists(monkeypatch):
    # A non-light move's patch only ever DROPS a stale room-light entity at the destination
    # (never adds one) - so convergence means the cloud's own fresh fetch shows NO room
    # entity there at all, not merely "our device isn't a member of it". A destination
    # entity that still exists (e.g. with some other light member) means the cloud hasn't
    # caught up yet, and pruning here would stop the drop from being re-applied.
    hass = _hass()
    entry = _entry()
    site = _site(
        devices=[_device(room_id="r1", category="switch", dimmable=False)],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))
    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")
    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]

    # A later, unrelated move: room_id has converged, but r2's light-group entity (with some
    # OTHER member, not d1's non-light output) still exists in the cloud's fresh fetch.
    destination_room = PlejdCloudRoom(
        room_id="r2", name="Room B", address=34, member_addresses=[50], dimmable=True, dimmable_addresses=[50]
    )
    partially_converged_site = _site(
        devices=[
            _device(device_id="d1", room_id="r2", category="switch", dimmable=False),
            _device(device_id="e", room_id="r1", address=99),
        ],
        device_addresses={"d1": 39, "e": 99},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
        rooms=[destination_room],
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site",
        AsyncMock(side_effect=[partially_converged_site, partially_converged_site]),
    )
    await async_move_device_to_room(hass, entry, device_id="e", room_id="r2")

    assert "d1" in hass.data[DATA_PENDING_ROOM_MOVES][entry.entry_id]  # destination entity still exists: not converged
    rooms_by_id = {r["room_id"]: r for r in entry.data["rooms"]}
    assert "r2" not in rooms_by_id  # drop still re-applied: r2's stale light-group entity removed again


async def test_move_device_skips_leave_when_device_has_no_current_room(monkeypatch):
    hass = _hass()
    coordinator = _coordinator()
    entry = _entry(runtime_data=coordinator)
    site = _site(
        devices=[_device(room_id=None)],
        device_addresses={"d1": 39},
        all_rooms=[_room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    coordinator.async_leave_mesh_group.assert_not_awaited()
    coordinator.async_join_mesh_group.assert_awaited_once_with(39, 34)


async def test_move_device_raises_when_old_room_has_no_address(monkeypatch):
    # Silently skipping the leave and only joining would leave the device subscribed to
    # BOTH the old and new room's mesh groups - must fail before sending any mesh command.
    hass = _hass()
    coordinator = _coordinator()
    entry = _entry(runtime_data=coordinator)
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=None), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=site))

    with pytest.raises(HomeAssistantError, match="no resolvable mesh group address"):
        await async_move_device_to_room(hass, entry, device_id="d1", room_id="r2")

    coordinator.async_leave_mesh_group.assert_not_awaited()
    coordinator.async_join_mesh_group.assert_not_awaited()


async def test_move_device_raises_on_cloud_error_during_refresh(monkeypatch):
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, PlejdCloudError("down")])
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error refreshing site"):
        await async_move_device_to_room(_hass(), _entry(), device_id="d1", room_id="r2")


async def test_move_device_raises_when_reload_fails(monkeypatch):
    hass = _hass()
    hass.config_entries.async_reload = AsyncMock(return_value=False)
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    with pytest.raises(HomeAssistantError, match="failed to reload after moving a device to a room"):
        await async_move_device_to_room(hass, _entry(), device_id="d1", room_id="r2")


async def test_move_device_runs_a_follow_up_reload_for_a_concurrent_change(monkeypatch):
    from plejd_cloud import schedule_ws

    hass = _hass()
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(side_effect=[site, site]))

    calls: list[str] = []

    async def _reload_sets_pending(entry_id):
        calls.append(entry_id)
        if len(calls) == 1:  # only the first reload race-loses to the concurrent change
            hass.data[schedule_ws.DATA_RELOAD_PENDING] = entry_id
        return True

    hass.config_entries.async_reload = AsyncMock(side_effect=_reload_sets_pending)

    await async_move_device_to_room(hass, _entry(), device_id="d1", room_id="r2")

    assert hass.config_entries.async_reload.await_count == 2  # ours, then the follow-up
    assert schedule_ws.DATA_RELOAD_PENDING not in hass.data


async def test_move_device_serializes_concurrent_calls_for_the_same_device(monkeypatch):
    # Two overlapping calls for the SAME device must not both read the same "current
    # room" before either records its own move - or the device could end up joined to
    # both destinations. Simulated by pausing the first call's login until the second
    # call has had a chance to run and confirming it hasn't started its own login yet.
    hass = _hass()
    coordinator = _coordinator()
    entry = _entry(runtime_data=coordinator)
    site = _site(
        devices=[_device(room_id="r1")],
        device_addresses={"d1": 39},
        all_rooms=[
            _room("r1", "Room A", address=14),
            _room("r2", "Room B", address=34),
            _room("r3", "Sovrum", address=46),
        ],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=site))

    call_order: list[str] = []
    release_first = asyncio.Event()

    async def _login_pauses_the_first_call(*_args, **_kwargs):
        call_order.append("login_start")
        if len(call_order) == 1:
            await release_first.wait()
        call_order.append("login_done")
        return "tok"

    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", _login_pauses_the_first_call)

    first = asyncio.ensure_future(async_move_device_to_room(hass, entry, device_id="d1", room_id="r2"))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(async_move_device_to_room(hass, entry, device_id="d1", room_id="r3"))
    await asyncio.sleep(0)

    assert call_order == ["login_start"]  # the second call is still blocked on the lock

    release_first.set()
    await asyncio.gather(first, second)

    assert call_order == ["login_start", "login_done", "login_start", "login_done"]
    coordinator.async_join_mesh_group.assert_any_await(39, 34)  # r2, from the first call
    coordinator.async_join_mesh_group.assert_any_await(39, 46)  # r3, from the second call


async def test_move_device_serializes_concurrent_calls_for_different_devices(monkeypatch):
    # The lock is per-entry, not per-device: every successful move ends in an
    # async_reload that tears down and rebuilds entry.runtime_data (the coordinator) - if
    # two DIFFERENT devices' moves were allowed to overlap, the first call's reload could
    # swap the coordinator out from under the second call's still-in-flight mesh writes.
    # Simulated the same way as the same-device test above: the first call's login is
    # paused, and the second call (for a DIFFERENT device) must still not start until the
    # first has fully finished (including its reload).
    hass = _hass()
    coordinator = _coordinator()
    entry = _entry(runtime_data=coordinator)
    site = _site(
        devices=[_device(device_id="d1", room_id="r1", address=39), _device(device_id="d2", room_id="r1", address=51)],
        device_addresses={"d1": 39, "d2": 51},
        all_rooms=[_room("r1", "Room A", address=14), _room("r2", "Room B", address=34)],
    )
    monkeypatch.setattr("plejd_cloud.manage_device_room.async_get_site", AsyncMock(return_value=site))

    call_order: list[str] = []
    release_first = asyncio.Event()

    async def _login_pauses_the_first_call(*_args, **_kwargs):
        call_order.append("login_start")
        if len(call_order) == 1:
            await release_first.wait()
        call_order.append("login_done")
        return "tok"

    monkeypatch.setattr("plejd_cloud.manage_device_room.async_login", _login_pauses_the_first_call)

    first = asyncio.ensure_future(async_move_device_to_room(hass, entry, device_id="d1", room_id="r2"))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(async_move_device_to_room(hass, entry, device_id="d2", room_id="r2"))
    await asyncio.sleep(0)

    assert call_order == ["login_start"]  # the second call (a different device) still blocked on the lock

    release_first.set()
    await asyncio.gather(first, second)

    assert call_order == ["login_start", "login_done", "login_start", "login_done"]
    coordinator.async_join_mesh_group.assert_any_await(39, 34)
    coordinator.async_join_mesh_group.assert_any_await(51, 34)


async def test_persist_pending_logs_when_the_follow_up_reload_raises(caplog):
    from plejd_cloud import schedule_ws

    hass = _hass()
    entry = _entry()
    hass.data[schedule_ws.DATA_RELOAD_PENDING] = "e1"
    hass.config_entries.async_reload = AsyncMock(side_effect=RuntimeError("boom"))

    await _async_persist_pending(hass, entry, {})

    assert "follow-up reload for a concurrent change failed" in caplog.text
