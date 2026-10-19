"""Tests for following Plejd room moves into HA areas."""

from __future__ import annotations

import types

from plejd_cloud import area_sync
from plejd_cloud.area_sync import (
    async_listen_area_changes,
    async_reset_areas,
    async_sync_areas,
    get_device_rooms,
    match_area,
)


def _area(area_id, name, aliases=()):
    return types.SimpleNamespace(id=area_id, name=name, aliases=set(aliases))


AREAS = [_area("vardagsrum", "Vardagsrum"), _area("garage", "Garage"), _area("hall", "Hall", aliases=["Entré"])]


class _Devices:
    def __init__(self, devices):
        self.devices = {d.id: d for d in devices}

    def async_get_device(self, identifiers):
        return next((d for d in self.devices.values() if d.identifiers & identifiers), None)

    def async_update_device(self, device_id, area_id):
        self.devices[device_id].area_id = area_id


def _device(device_id, plejd_id, area_id):
    return types.SimpleNamespace(id=device_id, name=plejd_id, identifiers={("plejd_cloud", plejd_id)}, area_id=area_id)


class _Bus:
    def __init__(self):
        self.listeners = []

    def async_listen(self, event, handler):
        self.listeners.append((event, handler))
        return lambda: None


def _hass(devices):
    return types.SimpleNamespace(
        data={},
        bus=_Bus(),
        device_registry=_Devices(devices),
        area_registry=types.SimpleNamespace(async_list_areas=lambda: AREAS),
    )


ENTRY_DATA = {
    "devices": [
        {"device_id": "TEKNIK", "output_index": 0, "room_id": "r-garage"},
        {"device_id": "FONSTER", "output_index": 0, "room_id": "r-vardag"},
        {"device_id": "FONSTER", "output_index": 1, "room_id": "r-garage"},
        {"device_id": "LOOSE", "output_index": 0, "room_id": None},
        {"device_id": "ODD", "output_index": 0, "room_id": "r-ovrigt"},
    ],
    "rooms": [
        {"room_id": "r-vardag", "name": "Vardagsrum / Allrum"},
        {"room_id": "r-garage", "name": "Garage"},
        {"room_id": "r-ovrigt", "name": "Övrigt"},
    ],
}


def _entry(data=ENTRY_DATA):
    return types.SimpleNamespace(entry_id="e1", data=data)


def test_match_area_matches_whole_name_part_or_alias_case_insensitively():
    assert match_area("garage", AREAS).id == "garage"
    assert match_area("Vardagsrum / Allrum", AREAS).id == "vardagsrum"
    assert match_area("Entré", AREAS).id == "hall"
    assert match_area("Övrigt", AREAS) is None


def test_get_device_rooms_uses_first_output_and_includes_room_devices():
    assert get_device_rooms(ENTRY_DATA) == {
        "TEKNIK": "r-garage",
        "FONSTER": "r-vardag",
        "ODD": "r-ovrigt",
        "room_r-vardag": "r-vardag",
        "room_r-garage": "r-garage",
        "room_r-ovrigt": "r-ovrigt",
    }


async def test_first_sync_moves_devices_to_their_plejd_room_area():
    hass = _hass(
        [_device("d1", "TEKNIK", "vardagsrum"), _device("d2", "FONSTER", "badrum"), _device("d3", "ODD", "kok")]
    )
    await async_sync_areas(hass, _entry())
    areas = {d.name: d.area_id for d in hass.device_registry.devices.values()}
    assert areas == {"TEKNIK": "garage", "FONSTER": "vardagsrum", "ODD": "kok"}  # Övrigt has no area: untouched


async def test_area_picked_in_ha_sticks_until_plejd_room_changes():
    hass = _hass([_device("d1", "TEKNIK", "garage")])
    await async_sync_areas(hass, _entry())
    hass.device_registry.devices["d1"].area_id = "hall"  # user's own choice in HA
    await async_sync_areas(hass, _entry())
    assert hass.device_registry.devices["d1"].area_id == "hall"

    moved = {**ENTRY_DATA, "devices": [{"device_id": "TEKNIK", "output_index": 0, "room_id": "r-vardag"}]}
    await async_sync_areas(hass, _entry(moved))
    assert hass.device_registry.devices["d1"].area_id == "vardagsrum"


async def test_unchanged_rooms_do_not_rewrite_the_store():
    hass = _hass([_device("d1", "TEKNIK", "garage")])
    await async_sync_areas(hass, _entry())
    key = ("store", f"{area_sync.STORE_KEY}.e1")
    saved = hass.data[key]
    assert saved == {"TEKNIK": {"room": "r-garage:Garage", "area": "garage"}}
    hass.data[key] = dict(saved)  # a different object with the same content
    marker = hass.data[key]
    await async_sync_areas(hass, _entry())
    assert hass.data[key] is marker


async def test_device_in_unresolved_room_is_moved_once_the_room_resolves():
    hass = _hass([_device("d1", "TEKNIK", "vardagsrum")])
    lights_free = {"devices": [{"device_id": "TEKNIK", "output_index": 0, "room_id": "r-garage"}], "rooms": []}
    await async_sync_areas(hass, _entry(lights_free))
    assert hass.device_registry.devices["d1"].area_id == "vardagsrum"  # room name unknown yet

    await async_sync_areas(hass, _entry())  # a light was added, so the room now has a name
    assert hass.device_registry.devices["d1"].area_id == "garage"


async def test_renamed_plejd_room_is_evaluated_again():
    hass = _hass([_device("d1", "ODD", "kok")])
    await async_sync_areas(hass, _entry())
    renamed = {**ENTRY_DATA, "rooms": [{"room_id": "r-ovrigt", "name": "Hall"}]}
    await async_sync_areas(hass, _entry(renamed))
    assert hass.device_registry.devices["d1"].area_id == "hall"


def test_match_area_prefers_an_exact_name_over_another_areas_alias():
    areas = [_area("pantry", "Pantry", aliases=["Kitchen"]), _area("kitchen", "Kitchen")]
    assert match_area("Kitchen", areas).id == "kitchen"


def test_device_rooms_does_not_fall_back_to_a_later_output():
    data = {
        "devices": [
            {"device_id": "DUO", "output_index": 0, "room_id": None},
            {"device_id": "DUO", "output_index": 1, "room_id": "r-garage"},
        ]
    }
    assert get_device_rooms(data) == {}


async def test_room_without_lights_resolves_through_the_full_room_name_catalog():
    hass = _hass([_device("d1", "PLUG", "vardagsrum")])
    data = {
        "devices": [{"device_id": "PLUG", "output_index": 0, "room_id": "r-g"}],
        "rooms": [],
        "room_names": {"r-g": "Garage"},
    }
    await async_sync_areas(hass, _entry(data))
    assert hass.device_registry.devices["d1"].area_id == "garage"


async def test_reset_makes_the_next_sync_align_a_hand_picked_area_again():
    hass = _hass([_device("d1", "TEKNIK", "garage")])
    await async_sync_areas(hass, _entry())
    hass.device_registry.devices["d1"].area_id = "hall"
    await async_reset_areas(hass, _entry())
    await async_sync_areas(hass, _entry())
    assert hass.device_registry.devices["d1"].area_id == "garage"


async def test_area_registry_change_re_runs_the_sync():
    hass = _hass([_device("d1", "ODD", "kok")])
    entry = _entry()
    async_listen_area_changes(hass, entry)
    [(event, handler)] = hass.bus.listeners
    AREAS.append(_area("ovrigt", "Övrigt"))
    try:
        await handler(types.SimpleNamespace(data={}))
    finally:
        AREAS.pop()
    assert event == "area_registry_updated"
    assert hass.device_registry.devices["d1"].area_id == "ovrigt"


async def test_area_change_handler_swallows_sync_errors(monkeypatch):
    hass = _hass([])

    async def _boom(hass, entry, **_kwargs):
        raise RuntimeError("registry gone")

    monkeypatch.setattr(area_sync, "async_sync_areas", _boom)
    async_listen_area_changes(hass, _entry())
    [(_, handler)] = hass.bus.listeners
    await handler(types.SimpleNamespace(data={}))  # must not raise


async def test_changed_area_match_retargets_an_automatically_assigned_device():
    areas_before = list(AREAS)
    AREAS[:] = [_area("pantry", "Pantry", aliases=["Garage"])]
    try:
        hass = _hass([_device("d1", "TEKNIK", None)])
        await async_sync_areas(hass, _entry())
        assert hass.device_registry.devices["d1"].area_id == "pantry"  # matched through the alias
        AREAS.append(_area("garage", "Garage"))
        await async_sync_areas(hass, _entry())
        assert hass.device_registry.devices["d1"].area_id == "garage"  # exact name now wins
    finally:
        AREAS[:] = areas_before


async def test_changed_area_match_keeps_a_hand_picked_area():
    areas_before = list(AREAS)
    AREAS[:] = [_area("pantry", "Pantry", aliases=["Garage"]), _area("hall", "Hall")]
    try:
        hass = _hass([_device("d1", "TEKNIK", None)])
        await async_sync_areas(hass, _entry())
        hass.device_registry.devices["d1"].area_id = "hall"
        AREAS.append(_area("garage", "Garage"))
        await async_sync_areas(hass, _entry())
        assert hass.device_registry.devices["d1"].area_id == "hall"
    finally:
        AREAS[:] = areas_before


async def test_reset_waits_for_an_in_flight_sync_instead_of_being_undone(monkeypatch):
    import asyncio

    release = asyncio.Event()
    real_store = area_sync.Store

    class _SlowStore(real_store):
        async def async_load(self):
            await release.wait()  # the sync is mid-I/O when the reset arrives
            return await super().async_load()

    monkeypatch.setattr(area_sync, "Store", _SlowStore)
    hass = _hass([_device("d1", "TEKNIK", "garage")])
    sync = asyncio.create_task(async_sync_areas(hass, _entry()))
    await asyncio.sleep(0)
    reset = asyncio.create_task(async_reset_areas(hass, _entry()))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(sync, reset)
    assert ("store", f"{area_sync.STORE_KEY}.e1") not in hass.data


async def test_area_event_sync_queued_behind_a_reset_does_not_recreate_the_store():
    hass = _hass([_device("d1", "TEKNIK", "garage")])
    async_listen_area_changes(hass, _entry())  # subscribed while syncing was on
    [(_, handler)] = hass.bus.listeners
    await async_reset_areas(hass, _entry())  # option turned off / entry removed
    await handler(types.SimpleNamespace(data={}))  # stale event delivered afterwards
    assert ("store", f"{area_sync.STORE_KEY}.e1") not in hass.data


async def test_hand_picked_area_survives_its_matching_area_disappearing_for_a_while():
    areas_before = list(AREAS)
    try:
        hass = _hass([_device("d1", "TEKNIK", "vardagsrum")])
        await async_sync_areas(hass, _entry())
        hass.device_registry.devices["d1"].area_id = "hall"  # moved by hand
        AREAS[:] = [a for a in AREAS if a.id != "garage"]  # matching area deleted
        await async_sync_areas(hass, _entry())
        AREAS[:] = areas_before  # and created again
        await async_sync_areas(hass, _entry())
        assert hass.device_registry.devices["d1"].area_id == "hall"
    finally:
        AREAS[:] = areas_before


def test_match_area_prefers_a_name_part_over_another_areas_alias_for_the_whole_name():
    areas = [_area("allrum", "Allrum", aliases=["Vardagsrum / Allrum"]), _area("vardagsrum", "Vardagsrum")]
    assert match_area("Vardagsrum / Allrum", areas).id == "vardagsrum"


def test_match_area_splits_only_on_the_spaced_separator():
    assert match_area("AC/DC", [_area("ac", "AC")]) is None


async def test_hand_picked_area_survives_two_consecutive_match_changes():
    areas_before = list(AREAS)
    AREAS[:] = [_area("pantry", "Pantry", aliases=["Garage"]), _area("hall", "Hall")]
    try:
        hass = _hass([_device("d1", "TEKNIK", None)])
        await async_sync_areas(hass, _entry())  # auto-placed in pantry via the alias
        hass.device_registry.devices["d1"].area_id = "hall"  # hand-picked
        AREAS[:] = [_area("pantry", "Pantry"), _area("hall", "Hall", aliases=["Garage"])]
        await async_sync_areas(hass, _entry())  # match changes to the hand-picked area itself
        AREAS.append(_area("garage", "Garage"))
        await async_sync_areas(hass, _entry())  # and changes again
        assert hass.device_registry.devices["d1"].area_id == "hall"
    finally:
        AREAS[:] = areas_before


async def test_uses_the_entry_scoped_device_lookup_when_ha_has_it():
    class _ScopedDevices(_Devices):
        def async_get_device(self, identifiers):
            raise AssertionError("deprecated lookup used")

        def async_get_device_by_identifier(self, identifier, config_entry_id):
            assert config_entry_id == "e1"
            return super().async_get_device({identifier})

    hass = _hass([])
    hass.device_registry = _ScopedDevices([_device("d1", "TEKNIK", "vardagsrum")])
    await async_sync_areas(hass, _entry())
    assert hass.device_registry.devices["d1"].area_id == "garage"


async def test_renamed_plejd_room_keeps_a_hand_picked_area():
    hass = _hass([_device("d1", "TEKNIK", "vardagsrum")])
    await async_sync_areas(hass, _entry())
    hass.device_registry.devices["d1"].area_id = "vardagsrum"  # hand-picked back
    renamed = {**ENTRY_DATA, "rooms": [{"room_id": "r-garage", "name": "Hall"}, *ENTRY_DATA["rooms"][:1]]}
    await async_sync_areas(hass, _entry(renamed))
    assert hass.device_registry.devices["d1"].area_id == "vardagsrum"


async def test_renamed_plejd_room_moves_an_automatically_placed_device():
    hass = _hass([_device("d1", "TEKNIK", "vardagsrum")])
    await async_sync_areas(hass, _entry())  # auto-placed in garage
    renamed = {**ENTRY_DATA, "rooms": [{"room_id": "r-garage", "name": "Hall"}, *ENTRY_DATA["rooms"][:1]]}
    await async_sync_areas(hass, _entry(renamed))
    assert hass.device_registry.devices["d1"].area_id == "hall"


async def test_deleted_and_recreated_area_restores_an_automatically_placed_device():
    areas_before = list(AREAS)
    try:
        hass = _hass([_device("d1", "TEKNIK", "vardagsrum")])
        entry = _entry()
        async_listen_area_changes(hass, entry)
        [(_, handler)] = hass.bus.listeners
        await async_sync_areas(hass, entry)  # auto-placed in garage
        AREAS[:] = [a for a in AREAS if a.id != "garage"]
        hass.device_registry.devices["d1"].area_id = None  # HA clears it before the event
        await handler(types.SimpleNamespace(data={"action": "remove", "area_id": "garage"}))
        AREAS[:] = areas_before
        await handler(types.SimpleNamespace(data={"action": "create", "area_id": "garage"}))
        assert hass.device_registry.devices["d1"].area_id == "garage"
    finally:
        AREAS[:] = areas_before


async def test_area_cleared_by_hand_stays_cleared():
    hass = _hass([_device("d1", "TEKNIK", "vardagsrum")])
    await async_sync_areas(hass, _entry())
    hass.device_registry.devices["d1"].area_id = None
    await async_sync_areas(hass, _entry())
    assert hass.device_registry.devices["d1"].area_id is None


async def test_area_cleared_by_hand_is_not_restored_after_that_area_is_deleted_and_recreated():
    areas_before = list(AREAS)
    try:
        hass = _hass([_device("d1", "TEKNIK", "vardagsrum")])
        entry = _entry()
        async_listen_area_changes(hass, entry)
        [(_, handler)] = hass.bus.listeners
        await async_sync_areas(hass, entry)  # auto-placed in garage
        hass.device_registry.devices["d1"].area_id = None  # cleared by hand
        await async_sync_areas(hass, entry)  # any later sync notices the manual choice
        AREAS[:] = [a for a in AREAS if a.id != "garage"]
        await handler(types.SimpleNamespace(data={"action": "remove", "area_id": "garage"}))
        AREAS[:] = areas_before
        await handler(types.SimpleNamespace(data={"action": "create", "area_id": "garage"}))
        assert hass.device_registry.devices["d1"].area_id is None
    finally:
        AREAS[:] = areas_before
