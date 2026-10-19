"""Tests for the Plejd diagnostics."""

from __future__ import annotations

import types

import pytest
from plejd_cloud import diagnostics
from plejd_cloud.cloud import PlejdAuthError, PlejdCloudDevice
from plejd_cloud.diagnostics import async_get_config_entry_diagnostics, site_shape

SITE = {
    "site": {"title": "Hemma", "siteId": "guid"},
    "plejdDevices": [
        {"deviceId": "d1", "firmware": {"version": "1"}},
        {"deviceId": "d2", "firmware": {"version": "2"}},
    ],
    "timeEvents": [{"mode": "astro", "offset": 15}, {"mode": "time", "time": "07:00"}],
    "deviceAddress": {"FFB5CB120267": 1, "aabbccddeeff": 2},
    "roomAddress": {"5b1e2a90-aaaa-bbbb": 14},
}


@pytest.fixture(autouse=True)
def _cloud(monkeypatch):
    async def login(session, email, password):
        return "token"

    async def raw(session, token, site_id):
        return SITE

    monkeypatch.setattr(diagnostics, "async_get_clientsession", lambda hass: None)
    monkeypatch.setattr(diagnostics, "async_login", login)
    monkeypatch.setattr(diagnostics, "async_get_site_raw", raw)


def _device(model="DIM-01"):
    return PlejdCloudDevice(
        device_id="d1",
        name="Vardagsrum",
        address=11,
        output_index=0,
        outputs=[11],
        hardware_id=1,
        model=model,
        category="light",
        dimmable=True,
        traits=3,
        room_id="r1",
    )


class _Coordinator:
    def __init__(self):
        self.devices = [_device("DIM-01"), _device("CTR-01")]
        self.scenes = [object()]
        self.inputs = []
        self.motion = []
        self.active_transport = "gateway"
        self.available = True


def _entry():
    return types.SimpleNamespace(
        data={
            "email": "user@example.com",
            "password": "secret",
            "crypto_key": "00112233445566778899aabbccddeeff",
            "site_id": "site-guid",
            "resource_set_id": "rs1",
            "installation_id": "inst-1",
            "discovered_address": "AA:BB:CC:DD:EE:FF",
            "gateways": ["E6C9AABBCCDD"],
            "device_addresses": {"d1": 11, "w1": 33},
            "devices": [
                {"device_id": "d1", "object_id": "7MK7dlrcfz", "address": 11, "name": "Vardagsrum", "model": "DIM-01"}
            ],
            "rooms": [
                {
                    "room_id": "r1",
                    "name": "Vardagsrum",
                    "address": 14,
                    "member_addresses": [11],
                    "dimmable": True,
                    "dimmable_addresses": [11],
                }
            ],
            "pending_room_moves": {
                "d1": {
                    "room_id": "r2",
                    "output_address": 11,
                    "is_light": True,
                    "dimmable": True,
                    "new_room_address": 34,
                }
            },
            "cloud_schedules": [
                {
                    "schedule_id": "te1",
                    "scene_id": "s1",
                    "title": "Garage",
                    "device_ids": ["d1"],
                    "scheduled_days": [0, 1, 2, 3, 4, 5, 6],
                    "fade_time": 0,
                    "activated": True,
                    "start_event": "sunset",
                    "start_offset": 15,
                    "end_event": "sunrise",
                    "end_offset": 0,
                    "night_reduction": None,
                }
            ],
        },
        options={
            "transport": "gateway",
            "schedules": [{"slot": 0, "name": "Wake", "days": [0, 1], "time": "07:00", "scene": 1, "fade": 0}],
        },
        runtime_data=_Coordinator(),
    )


async def test_diagnostics_redacts_secrets_and_pii():
    diag = await async_get_config_entry_diagnostics(None, _entry())
    data = diag["entry_data"]
    # secrets + PII redacted
    for key in (
        "email",
        "password",
        "crypto_key",
        "site_id",
        "resource_set_id",
        "installation_id",
        "discovered_address",
        "gateways",
        "device_addresses",  # mesh addresses keyed by device_id -- BLE/PII-adjacent
    ):
        assert data[key] == "**REDACTED**"
    dev = data["devices"][0]
    assert dev["device_id"] == "**REDACTED**" and dev["address"] == "**REDACTED**" and dev["name"] == "**REDACTED**"
    assert dev["object_id"] == "**REDACTED**"  # cloud Parse id must not leak in diagnostics
    # model is not sensitive and is kept
    assert dev["model"] == "DIM-01"
    room = data["rooms"][0]
    assert room["room_id"] == "**REDACTED**" and room["address"] == "**REDACTED**" and room["name"] == "**REDACTED**"
    assert room["member_addresses"] == "**REDACTED**"  # mesh output addresses -- BLE/PII-adjacent
    assert room["dimmable_addresses"] == "**REDACTED**"  # mesh output addresses -- BLE/PII-adjacent
    assert room["dimmable"] is True  # not sensitive and is kept
    # schedules (occupancy/routine) are redacted wholesale, transport pref kept
    assert diag["options"]["schedules"] == "**REDACTED**" and diag["options"]["transport"] == "gateway"
    # move_device_to_room's own not-yet-cloud-confirmed moves: redacted wholesale, since
    # it's keyed BY device_id (a dict key, invisible to a by-value redactor) and its values
    # carry mesh addresses too
    assert data["pending_room_moves"] == "**REDACTED**"
    # cloud schedules (astro triggers + night reduction) reveal occupancy/routine just like
    # the on-device ones above — redacted wholesale, count reported below
    assert data["cloud_schedules"] == "**REDACTED**"


async def test_diagnostics_reports_transport_and_counts():
    diag = await async_get_config_entry_diagnostics(None, _entry())
    assert diag["active_transport"] == "gateway" and diag["available"] is True
    assert diag["counts"] == {
        "devices": 2,
        "scenes": 1,
        "inputs": 0,
        "motion": 0,
        "gateways": 1,
        "schedules": 1,
        "cloud_schedules": 1,
    }
    assert diag["models"] == ["CTR-01", "DIM-01"]


async def test_diagnostics_transport_disconnected_label():
    entry = _entry()
    entry.runtime_data.active_transport = None
    diag = await async_get_config_entry_diagnostics(None, entry)
    assert diag["active_transport"] == "disconnected"


def test_site_shape_keeps_keys_and_types_but_no_values():
    shape = site_shape(SITE)
    assert shape["site"] == {"title": "str", "siteId": "str"}
    # identical items collapse to one shape; differing ones (the point of the dump) are kept
    assert shape["plejdDevices"] == {"<list>": 2, "items": [{"deviceId": "str", "firmware": {"version": "str"}}]}
    assert shape["timeEvents"]["items"] == [{"mode": "str", "offset": "int"}, {"mode": "str", "time": "str"}]
    assert "Hemma" not in repr(shape) and "07:00" not in repr(shape)


def test_site_shape_caps_variants_per_list():
    assert len(site_shape([{"k" * (i + 1): i} for i in range(10)])["items"]) == 4


async def test_diagnostics_include_site_structure():
    diag = await async_get_config_entry_diagnostics(None, _entry())
    assert diag["site_structure"]["timeEvents"]["<list>"] == 2


async def test_diagnostics_site_structure_reports_cloud_error(monkeypatch):
    async def fail(session, email, password):
        raise PlejdAuthError("bad")

    monkeypatch.setattr(diagnostics, "async_login", fail)
    diag = await async_get_config_entry_diagnostics(None, _entry())
    assert diag["site_structure"] == {"error": "PlejdAuthError"}


def test_site_shape_hides_identifier_keys():
    shape = site_shape(SITE)
    assert shape["deviceAddress"] == {"<map>": 2, "values": ["int"]}
    assert shape["roomAddress"] == {"<map>": 1, "values": ["int"]}
    assert "FFB5CB120267" not in repr(shape) and "aabbccddeeff" not in repr(shape) and "5b1e2a90" not in repr(shape)


async def test_diagnostics_site_structure_survives_a_network_error(monkeypatch):
    async def offline(session, email, password):
        raise TimeoutError

    monkeypatch.setattr(diagnostics, "async_login", offline)
    diag = await async_get_config_entry_diagnostics(None, _entry())
    assert diag["site_structure"] == {"error": "TimeoutError"} and diag["counts"]["devices"] == 2


async def test_diagnostics_site_structure_survives_invalid_json(monkeypatch):
    async def bad(session, token, site_id):
        raise ValueError("Expecting value")

    monkeypatch.setattr(diagnostics, "async_get_site_raw", bad)
    diag = await async_get_config_entry_diagnostics(None, _entry())
    assert diag["site_structure"] == {"error": "ValueError"}
