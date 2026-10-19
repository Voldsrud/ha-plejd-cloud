"""Tests for the room-card WebSocket API (Plejd rooms + lamp styles)."""

from __future__ import annotations

import types

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from plejd_cloud import room_ws
from plejd_cloud.cloud import PlejdCloudRoom
from plejd_cloud.schedule_ws import DATA_ENTRY


class _Conn:
    def __init__(self):
        self.result = None
        self.error = None

    def send_result(self, msg_id, payload):
        self.result = (msg_id, payload)

    def send_error(self, msg_id, code, message):
        self.error = (msg_id, code, message)


def _reg(unique_id, entity_id):
    return types.SimpleNamespace(unique_id=unique_id, entity_id=entity_id, config_entry_id="e1")


def _device(device_id, room_id, output_index=0, category="light"):
    return types.SimpleNamespace(device_id=device_id, room_id=room_id, output_index=output_index, category=category)


def _hass(coordinator=None, room_names=None, entry=True):
    data = {"room_names": room_names} if room_names is not None else {}
    registry = er.EntityRegistry(
        {
            e.entity_id: e
            for e in [
                _reg("room_r1", "light.kok"),
                _reg("d1", "light.kok_tak"),
                _reg("d2_1", "light.kok_spot"),
                _reg("d3", "light.hall"),
                _reg("d6", "light.garage"),
                _reg("room_r2", "light.tom"),
                _reg("d1", "switch.kok_tak"),  # stale relay entry for the same output: ignored
            ]
        }
    )
    hass = types.SimpleNamespace(data={}, entity_registry=registry)
    if entry:
        hass.data[DATA_ENTRY] = types.SimpleNamespace(entry_id="e1", runtime_data=coordinator, data=data)
    return hass


async def test_rooms_maps_each_plejd_room_to_its_group_light_and_members():
    coordinator = types.SimpleNamespace(
        rooms=[
            PlejdCloudRoom("r1", "Kök", 1, [], True, []),
            PlejdCloudRoom("r2", "Tomt", 2, [], False, []),
        ],
        devices=[
            _device("d1", "r1"),
            _device("d2", "r1", output_index=1),
            _device("d3", None),
            _device("d4", "r1", category="relay"),  # not a light
            _device("d5", "r1"),  # no entity registered
            _device("d6", "r3"),  # room with a relay: no safe group light, but still its own room
        ],
    )
    conn = _Conn()
    await room_ws.ws_rooms(_hass(coordinator, room_names={"r1": "Kök (renamed)", "r3": "Garage"}), conn, {"id": 1})
    assert conn.result == (
        1,
        {
            "rooms": [
                # stored room_names win over the cached group room's name
                {
                    "room_id": "r1",
                    "name": "Kök (renamed)",
                    "entity_id": "light.kok",
                    "lights": ["light.kok_tak", "light.kok_spot"],
                },
                {"room_id": "r2", "name": "Tomt", "entity_id": "light.tom", "lights": []},
                {"room_id": "r3", "name": "Garage", "entity_id": None, "lights": ["light.garage"]},
            ]
        },
    )


async def test_rooms_without_stored_names_fall_back_to_group_rooms():
    coordinator = types.SimpleNamespace(
        rooms=[PlejdCloudRoom("r1", "Kök", 1, [], True, [])], devices=[_device("d1", "r1")]
    )
    conn = _Conn()
    await room_ws.ws_rooms(_hass(coordinator), conn, {"id": 1})
    assert conn.result[1]["rooms"] == [
        {"room_id": "r1", "name": "Kök", "entity_id": "light.kok", "lights": ["light.kok_tak"]}
    ]


async def test_rooms_errors_when_not_loaded():
    conn = _Conn()
    await room_ws.ws_rooms(_hass(None), conn, {"id": 2})
    assert conn.error == (2, "not_loaded", "Plejd is not loaded")


async def test_styles_default_to_empty_and_list_the_lamp_types():
    conn = _Conn()
    await room_ws.ws_styles_get(_hass(), conn, {"id": 3})
    assert conn.result == (3, {"styles": {}, "available": list(room_ws.LIGHT_STYLES)})


@pytest.mark.parametrize("style", ["pendant", "spot"])
async def test_style_set_persists_and_clear_removes(style):
    hass = _hass()
    conn = _Conn()
    await room_ws.ws_styles_set(hass, conn, {"id": 4, "entity_id": "light.kok_tak", "style": style})
    await room_ws.ws_styles_set(hass, conn, {"id": 5, "entity_id": "light.hall", "style": "wall"})
    await room_ws.ws_styles_get(hass, conn, {"id": 6})
    assert conn.result[1]["styles"] == {"light.kok_tak": style, "light.hall": "wall"}
    await room_ws.ws_styles_set(hass, conn, {"id": 7, "entity_id": "light.hall", "style": None})
    assert conn.result == (7, {"styles": {"light.kok_tak": style}})


async def test_style_follows_an_entity_id_rename():
    hass = _hass()
    conn = _Conn()
    await room_ws.ws_styles_set(hass, conn, {"id": 1, "entity_id": "light.hall", "style": "wall"})
    registry = hass.entity_registry._entities
    registry["light.hallway"] = registry.pop("light.hall")
    registry["light.hallway"].entity_id = "light.hallway"
    await room_ws.ws_styles_get(hass, conn, {"id": 2})
    assert conn.result[1]["styles"] == {"light.hallway": "wall"}


async def test_style_set_rejects_an_unknown_light():
    conn = _Conn()
    await room_ws.ws_styles_set(_hass(), conn, {"id": 1, "entity_id": "light.nope", "style": "wall"})
    assert conn.error == (1, "not_found", "Unknown light")


async def test_styles_are_empty_when_plejd_is_not_loaded():
    hass = _hass(entry=False)
    hass.data[("store", "plejd_cloud.light_styles")] = {"d3": "wall"}
    conn = _Conn()
    await room_ws.ws_styles_get(hass, conn, {"id": 1})
    assert conn.result[1]["styles"] == {}


def test_async_register_registers_all_commands():
    hass = types.SimpleNamespace(data={})
    room_ws.async_register(hass)
    assert {
        room_ws.ws_rooms,
        room_ws.ws_styles_get,
        room_ws.ws_styles_set,
        room_ws.ws_layout_get,
        room_ws.ws_layout_set,
        room_ws.ws_rename_light,
    } <= set(hass.data["ws_commands"])


async def test_layout_defaults_to_empty_then_round_trips():
    hass = _hass()
    conn = _Conn()
    await room_ws.ws_layout_get(hass, conn, {"id": 1})
    assert conn.result == (1, {"order": [], "sizes": {}})
    await room_ws.ws_layout_set(hass, conn, {"id": 2, "order": ["r2", "", "r1"], "sizes": {"r1": 3, "": 1}})
    await room_ws.ws_layout_get(hass, conn, {"id": 3})
    assert conn.result == (3, {"order": ["r2", "", "r1"], "sizes": {"r1": 3, "": 1}})


# ── lights/rename ────────────────────────────────────────────────────────────


class _Registry(er.EntityRegistry):
    def __init__(self, entities):
        super().__init__(entities)
        self.entity_names = {}

    def async_update_entity(self, entity_id, *, name):
        self.entity_names[entity_id] = name


class _Devices:
    def __init__(self):
        self.names = {}

    def async_update_device(self, device_id, *, name_by_user):
        self.names[device_id] = name_by_user


def _rename_hass(rename_error=None):
    def light(uid, entity_id, device_id):
        return types.SimpleNamespace(
            unique_id=uid, entity_id=entity_id, device_id=device_id, config_entry_id="e1", name=None
        )

    renamed = []
    skipped = []

    async def _rename(device_id, name, output_index=None):
        if rename_error:
            raise rename_error
        renamed.append((device_id, name, output_index))

    coordinator = types.SimpleNamespace(
        devices=[
            _device("d1", "r1"),
            _device("d2", "r1"),
            _device("d2", "r1", output_index=1),
            _device("d2", "r1", output_index=2, category="relay"),
            _device("d3", "r1"),
            _device("d3", "r1", output_index=1, category="relay"),
        ],
        async_rename_device=_rename,
        skip_next_mirror=lambda device_id, name: skipped.append((device_id, name)),
    )
    reauth = []
    entry = types.SimpleNamespace(
        entry_id="e1", runtime_data=coordinator, data={}, async_start_reauth=lambda h: reauth.append(h)
    )
    registry = _Registry(
        {
            e.entity_id: e
            for e in [
                light("d1", "light.single", "dev1"),
                light("d2", "light.dual_a", "dev2"),
                light("d2_1", "light.dual_b", "dev2"),
                light("x", "switch.other", "devx"),
                light("d3", "light.mixed", "dev3"),
            ]
        }
    )
    hass = types.SimpleNamespace(data={DATA_ENTRY: entry}, entity_registry=registry, device_registry=_Devices())
    hass.skipped = skipped
    return hass, renamed, reauth


async def test_rename_single_output_light_renames_the_device_after_plejd_accepts():
    hass, renamed, _ = _rename_hass()
    conn = _Conn()
    await room_ws.ws_rename_light(hass, conn, {"id": 1, "entity_id": "light.single", "name": " Taklampa "})
    assert renamed == [("d1", "Taklampa", 0)]
    assert hass.device_registry.names == {"dev1": "Taklampa"}
    assert conn.result == (1, {"name": "Taklampa"})
    assert hass.skipped == [("d1", "Taklampa")]  # the registry update mustn't mirror it to Plejd again


async def test_rename_clears_an_entity_name_override_that_would_hide_the_new_device_name():
    hass, _, _ = _rename_hass()
    hass.entity_registry.async_get("light.single").name = "Old override"
    conn = _Conn()
    await room_ws.ws_rename_light(hass, conn, {"id": 1, "entity_id": "light.single", "name": "Taklampa"})
    assert hass.device_registry.names == {"dev1": "Taklampa"}
    assert hass.entity_registry.entity_names == {"light.single": None}


async def test_rename_light_sharing_a_device_with_a_relay_names_only_the_entity():
    hass, renamed, _ = _rename_hass()
    conn = _Conn()
    await room_ws.ws_rename_light(hass, conn, {"id": 1, "entity_id": "light.mixed", "name": "Spot"})
    assert renamed == [("d3", "Spot", 0)]
    assert hass.device_registry.names == {} and hass.skipped == []
    assert hass.entity_registry.entity_names == {"light.mixed": "Spot"}


async def test_rename_one_output_of_a_shared_device_names_only_that_entity():
    # two lights share device d2 (the relay output doesn't count): renaming the device would rename both
    hass, renamed, _ = _rename_hass()
    conn = _Conn()
    await room_ws.ws_rename_light(hass, conn, {"id": 1, "entity_id": "light.dual_b", "name": "Spot"})
    assert renamed == [("d2", "Spot", 1)]
    assert hass.device_registry.names == {}
    assert hass.entity_registry.entity_names == {"light.dual_b": "Spot"}


@pytest.mark.parametrize(
    ("error", "code"),
    [(HomeAssistantError("cloud said no"), "rename_failed"), (RuntimeError("boom"), "rename_failed")],
)
async def test_rename_reports_a_cloud_failure_and_leaves_ha_untouched(error, code):
    hass, _, _ = _rename_hass(rename_error=error)
    conn = _Conn()
    await room_ws.ws_rename_light(hass, conn, {"id": 1, "entity_id": "light.single", "name": "X"})
    assert conn.error[1] == code
    assert hass.device_registry.names == {} and hass.entity_registry.entity_names == {}


async def test_rename_rejected_credentials_start_reauth():
    from plejd_cloud.cloud import PlejdAuthError

    hass, _, reauth = _rename_hass(rename_error=PlejdAuthError("bad"))
    conn = _Conn()
    await room_ws.ws_rename_light(hass, conn, {"id": 1, "entity_id": "light.single", "name": "X"})
    assert conn.error[1] == "auth_failed" and reauth == [hass]


@pytest.mark.parametrize(
    ("msg", "code"),
    [
        ({"entity_id": "light.single", "name": "  "}, "name_required"),
        ({"entity_id": "switch.other", "name": "X"}, "not_found"),
        ({"entity_id": "light.nope", "name": "X"}, "not_found"),
    ],
)
async def test_rename_rejects_bad_input(msg, code):
    hass, renamed, _ = _rename_hass()
    conn = _Conn()
    await room_ws.ws_rename_light(hass, conn, {"id": 1, **msg})
    assert conn.error[1] == code and renamed == []


async def test_rename_errors_when_not_loaded():
    conn = _Conn()
    await room_ws.ws_rename_light(types.SimpleNamespace(data={}), conn, {"id": 1, "entity_id": "light.x", "name": "X"})
    assert conn.error == (1, "not_loaded", "Plejd is not loaded")
