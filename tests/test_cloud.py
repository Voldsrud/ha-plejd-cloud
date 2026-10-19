"""Tests for the Plejd cloud (Parse) client."""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses
from plejd_cloud.cloud import (
    NewDeviceInfo,
    PlejdAuthError,
    PlejdCloudError,
    _parse_new_device_addresses,
    async_create_device,
    async_create_room,
    async_create_scene,
    async_create_time_event,
    async_get_available_firmware,
    async_get_needed_output_count,
    async_get_site,
    async_get_sites,
    async_login,
    async_remove_device,
    async_remove_room,
    async_remove_scene,
    async_remove_time_event,
    async_set_device_title,
    async_set_input_setting,
    async_update_room,
    async_update_scene,
    async_update_time_event,
    parse_site,
)
from plejd_cloud.const import (
    PLEJD_FN_COMPATIBLE_DEVICES,
    PLEJD_FN_CREATE_DEVICE,
    PLEJD_FN_CREATE_ROOM,
    PLEJD_FN_CREATE_SCENE,
    PLEJD_FN_CREATE_TIME_EVENT,
    PLEJD_FN_REMOVE_DEVICE,
    PLEJD_FN_REMOVE_ROOM,
    PLEJD_FN_REMOVE_SCENE,
    PLEJD_FN_REMOVE_TIME_EVENT,
    PLEJD_FN_SET_INPUT,
    PLEJD_FN_UPDATE_ROOM,
    PLEJD_FN_UPDATE_SCENE,
    PLEJD_FN_UPDATE_TIME_EVENT,
    PLEJD_PARSE_URL,
)

_LOGIN = PLEJD_PARSE_URL + "login"
_SITE_LIST = PLEJD_PARSE_URL + "functions/getSiteList"
_SITE_BY_ID = PLEJD_PARSE_URL + "functions/getSiteById"
_FIRMWARE = PLEJD_PARSE_URL + "functions/getFirmwaresByHardwareId"
_CREATE_DEVICE = PLEJD_PARSE_URL + PLEJD_FN_CREATE_DEVICE
_CREATE_ROOM = PLEJD_PARSE_URL + PLEJD_FN_CREATE_ROOM
_UPDATE_ROOM = PLEJD_PARSE_URL + PLEJD_FN_UPDATE_ROOM
_REMOVE_ROOM = PLEJD_PARSE_URL + PLEJD_FN_REMOVE_ROOM
_CREATE_SCENE = PLEJD_PARSE_URL + PLEJD_FN_CREATE_SCENE
_UPDATE_SCENE = PLEJD_PARSE_URL + PLEJD_FN_UPDATE_SCENE
_REMOVE_SCENE = PLEJD_PARSE_URL + PLEJD_FN_REMOVE_SCENE
_REMOVE_DEVICE = PLEJD_PARSE_URL + PLEJD_FN_REMOVE_DEVICE
_SET_INPUT = PLEJD_PARSE_URL + PLEJD_FN_SET_INPUT
_CREATE_TIME_EVENT = PLEJD_PARSE_URL + PLEJD_FN_CREATE_TIME_EVENT
_UPDATE_TIME_EVENT = PLEJD_PARSE_URL + PLEJD_FN_UPDATE_TIME_EVENT
_REMOVE_TIME_EVENT = PLEJD_PARSE_URL + PLEJD_FN_REMOVE_TIME_EVENT

_SITE = {
    "siteId": "S1",
    "title": "Home",
    "plejdMesh": {"cryptoKey": "00112233445566778899aabbccddeeff"},
    "deviceAddress": {"d1": 1, "d2": 2, "d3": 3, "w1": 33},
    "outputAddress": {"d1": {"0": 11}, "d2": {"0": 21, "1": 22}},
    "inputAddress": {"d1": {"0": 11, "1": 11}, "d3": {"0": 31}},
    "plejdDevices": [
        {"deviceId": "d1", "hardwareId": "1"},
        {"deviceId": "d2", "hardwareId": "18"},
        {"deviceId": "w1", "hardwareId": "70"},
        {"deviceId": "d3", "hardwareId": "16"},
    ],
    "devices": [
        {"deviceId": "d1", "title": "Kitchen", "roomId": "r1", "outputType": "LIGHT"},
        {"deviceId": "d2", "title": "Pump", "roomId": "r1", "outputType": "RELAY"},
        {"deviceId": "d3", "title": "Blind", "roomId": "r2", "outputType": "COVERABLE"},
    ],
    "scenes": [{"sceneId": "sc1", "title": "Movie"}, {"sceneId": "sc2", "title": "NoIndex"}],
    "sceneIndex": {"sc1": 3},
}


async def _session():
    return aiohttp.ClientSession()


async def test_login_returns_session_token():
    with aioresponses() as m:
        m.post(_LOGIN, payload={"sessionToken": "r:abc"})
        async with aiohttp.ClientSession() as s:
            assert await async_login(s, "User@Example.com", "pw") == "r:abc"


async def test_login_bad_credentials_raises_auth_error():
    with aioresponses() as m:
        m.post(_LOGIN, status=404, payload={"error": "invalid login", "code": 101})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdAuthError, match="invalid login"):
                await async_login(s, "u@x.se", "bad")


async def test_login_without_token_is_transient_not_an_auth_failure():
    # A 200 carrying no sessionToken is a malformed response, not the server saying these
    # credentials are wrong (Parse signals that with a 4xx + error code) - so it must not
    # start reauth for a password that was never the problem.
    with aioresponses() as m:
        m.post(_LOGIN, payload={"objectId": "u1"})  # 200 but no token
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdCloudError) as excinfo:
                await async_login(s, "u@x.se", "pw")
    assert not isinstance(excinfo.value, PlejdAuthError)


async def test_get_sites_returns_list():
    with aioresponses() as m:
        m.post(_SITE_LIST, payload={"result": [{"siteId": "S1", "title": "Home"}]})
        async with aiohttp.ClientSession() as s:
            sites = await async_get_sites(s, "tok")
    assert sites == [{"siteId": "S1", "title": "Home"}]


async def test_get_sites_non_list_result_is_empty():
    with aioresponses() as m:
        m.post(_SITE_LIST, payload={"result": None})
        async with aiohttp.ClientSession() as s:
            assert await async_get_sites(s, "tok") == []


async def test_motion_sensor_room_comes_from_its_device_entry_or_its_own_record():
    import copy

    site_raw = copy.deepcopy(_SITE)
    site_raw["plejdDevices"][2]["roomId"] = "r9"  # standalone WMS-01: only plejdDevices knows its room
    with aioresponses() as m:
        m.post(_SITE_BY_ID, payload={"result": [site_raw]})
        async with aiohttp.ClientSession() as s:
            assert [m.room_id for m in (await async_get_site(s, "tok", "S1")).motion] == ["r9"]
    site_raw["devices"].append({"deviceId": "w1", "title": "Hall motion", "roomId": "r2", "outputType": "SENSOR"})
    with aioresponses() as m:
        m.post(_SITE_BY_ID, payload={"result": [site_raw]})
        async with aiohttp.ClientSession() as s:
            site = await async_get_site(s, "tok", "S1")
            assert [(m.room_id, m.name) for m in site.motion] == [("r2", "Hall motion")]  # its title, not generic
    site_raw["devices"][-1]["roomId"] = None  # its device entry lacks a room: its own record still has one
    with aioresponses() as m:
        m.post(_SITE_BY_ID, payload={"result": [site_raw]})
        async with aiohttp.ClientSession() as s:
            assert [m.room_id for m in (await async_get_site(s, "tok", "S1")).motion] == ["r9"]


def test_parse_site_scene_outputs_come_from_scene_steps():
    def outputs(steps):
        return [s.output_addresses for s in parse_site({**_SITE, "sceneSteps": steps}).scenes]

    step = {"sceneId": "sc1", "deviceId": "d1", "output": 0, "dirty": False, "dirtyRemoved": False}
    removed = {**step, "deviceId": "d2", "dirtyRemoved": True}  # a removal the mesh already has
    assert outputs([step, dict(step), removed]) == [[11]]  # duplicates collapse
    assert outputs([]) == [[]]  # a scene with no steps sets nothing
    # Membership is unknown rather than incomplete when it can't be trusted:
    assert outputs([step, {**removed, "dirty": True}]) == [None]  # an edit the mesh hasn't got yet
    assert outputs([step, {**step, "deviceId": "gone"}]) == [None]  # an output we can't map
    for bad in ({"dirty": 1}, {"dirtyRemoved": "true"}, {"deviceId": ["d1"]}, {"output": {"0": 1}}, {"output": True}):
        assert outputs([step, {**step, **bad}]) == [None], bad  # flags or keys of the wrong type
    assert outputs([step, {"sceneId": "sc2", "deviceId": "gone"}]) == [[11]]  # ...only for its own scene
    assert outputs([step, "not-a-dict"]) == [None]
    assert outputs([step, {**step, "sceneId": None}]) == [None]
    assert outputs({"x": 1}) == [None]
    assert [s.output_addresses for s in parse_site(_SITE).scenes] == [None]


async def test_call_function_error_status_raises():
    with aioresponses() as m:
        m.post(_SITE_LIST, status=500, payload={"error": "boom"})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdCloudError, match="boom"):
                await async_get_sites(s, "tok")


async def test_get_site_parses_devices():
    with aioresponses() as m:
        m.post(_SITE_BY_ID, payload={"result": [_SITE]})
        async with aiohttp.ClientSession() as s:
            site = await async_get_site(s, "tok", "S1")
    assert site.crypto_key == bytes.fromhex("00112233445566778899aabbccddeeff")
    by_id = {d.device_id: d for d in site.devices}
    assert set(by_id) == {"d1", "d2", "d3"}
    assert by_id["d1"].category == "light" and by_id["d1"].dimmable is True
    assert by_id["d1"].model == "DIM-01" and by_id["d1"].outputs == [11]
    assert by_id["d2"].category == "switch" and by_id["d2"].dimmable is False
    assert by_id["d2"].outputs == [21, 22] and by_id["d2"].address == 21
    assert by_id["d3"].category == "cover" and by_id["d3"].model == "JAL-01"
    # scenes: only those with a sceneIndex entry are kept
    assert [(s.name, s.index) for s in site.scenes] == [("Movie", 3)]
    # inputs: deduped by address; named from devices[]; input_index kept for later writes
    assert sorted((i.address, i.name, i.input_index) for i in site.inputs) == [(11, "Kitchen", 0), (31, "Blind", 0)]
    # physical device addresses (for fault polling), keyed by device_id, incl. sensor w1
    assert site.device_addresses == {"d1": 1, "d2": 2, "d3": 3, "w1": 33}
    assert [(m.address, m.name) for m in site.motion] == [(33, "Motion sensor")]


def test_parse_site_keeps_input_index_for_two_input_device():
    # A real two-input device (distinct addresses per input, unlike d1's fixture above
    # where both indices share one address and get deduped) must keep each input's own
    # index, since async_set_input_setting needs it to target the right physical input.
    site = {**_SITE, "inputAddress": {"d1": {"0": 11, "1": 12}}}
    parsed = parse_site(site)
    assert sorted((i.address, i.input_index) for i in parsed.inputs) == [(11, 0), (12, 1)]


def test_parse_site_skips_malformed_input_address_entries():
    # A non-numeric index or address must not abort the whole site parse - just that entry.
    site = {**_SITE, "inputAddress": {"d1": {"bad": 11, "1": "also-bad", "2": 13}}}
    parsed = parse_site(site)
    assert [(i.address, i.input_index) for i in parsed.inputs] == [(13, 2)]


async def test_get_site_accepts_dict_result():
    with aioresponses() as m:
        m.post(_SITE_BY_ID, payload={"result": _SITE})  # not wrapped in a list
        async with aiohttp.ClientSession() as s:
            site = await async_get_site(s, "tok", "S1")
    assert site.site_id == "S1"


async def test_get_site_empty_result_raises():
    with aioresponses() as m:
        m.post(_SITE_BY_ID, payload={"result": []})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdCloudError, match="not found"):
                await async_get_site(s, "tok", "S1")


async def test_get_site_malformed_result_raises():
    with aioresponses() as m:
        m.post(_SITE_BY_ID, payload={"result": "nope"})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdCloudError, match="malformed"):
                await async_get_site(s, "tok", "S1")


def test_parse_site_requires_crypto_key():
    with pytest.raises(PlejdCloudError, match="cryptoKey"):
        parse_site({"siteId": "S1", "plejdMesh": {}})


def test_parse_site_rejects_a_crypto_key_of_the_wrong_length():
    with pytest.raises(PlejdCloudError, match="16 bytes"):
        parse_site({"siteId": "S1", "plejdMesh": {"cryptoKey": "00" * 15}})


def test_dimmable_follows_traits_when_present():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "plejdDevices": [{"deviceId": "a", "hardwareId": "1"}, {"deviceId": "b", "hardwareId": "1"}],
            "devices": [
                {"deviceId": "a", "outputType": "LIGHT", "traits": 0x01},  # Powerable only -> on/off light
                {"deviceId": "b", "outputType": "LIGHT", "traits": 0x03},  # Powerable|Dimmable
            ],
        }
    )
    by_id = {d.device_id: d for d in site.devices}
    assert by_id["a"].category == "light" and by_id["a"].dimmable is False
    assert by_id["b"].dimmable is True and by_id["b"].traits == 0x03


def test_parse_site_extracts_gateway_and_resource_set():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "devices": [],
            "gateways": [{"deviceId": "gw1", "hardwareId": "4", "resourceSetId": "rsABC"}],
            "resourceSets": [{"objectId": "rsXYZ"}],
        }
    )
    assert site.gateways == ["gw1"]
    assert site.resource_set_id == "rsABC"  # the gateway's own resourceSetId wins


def test_parse_site_resource_set_falls_back_to_resource_sets():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "devices": [],
            "gateways": [{"deviceId": "gw1"}],  # no resourceSetId on the gateway
            "resourceSets": [{"objectId": "rsXYZ"}],
        }
    )
    assert site.gateways == ["gw1"] and site.resource_set_id == "rsXYZ"


def test_parse_site_ambiguous_resource_sets_yields_none():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "devices": [],
            "gateways": [{"deviceId": "gw1"}],  # no resourceSetId on the gateway
            "resourceSets": [{"objectId": "rsA"}, {"objectId": "rsB"}],  # ambiguous
        }
    )
    assert site.gateways == ["gw1"] and site.resource_set_id is None


def test_parse_site_no_gateway():
    site = parse_site({"plejdMesh": {"cryptoKey": "00" * 16}, "devices": []})
    assert site.gateways == [] and site.resource_set_id is None


def test_parse_site_handles_missing_address_and_hardware():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "devices": [{"deviceId": "x", "outputType": "Unknown"}],
        }
    )
    dev = site.devices[0]
    assert dev.address is None and dev.outputs == []
    assert dev.hardware_id == 0 and dev.category == "none"
    assert site.title == "Plejd"  # default


def test_parse_site_captures_output_settings():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "devices": [
                {"deviceId": "a", "outputType": "LIGHT", "outputSettings": {"minDim": 100, "dimCurve": 1}},
                {"deviceId": "b", "outputType": "LIGHT"},  # no outputSettings
            ],
        }
    )
    by_id = {d.device_id: d for d in site.devices}
    assert by_id["a"].output_settings == {"minDim": 100, "dimCurve": 1}
    assert by_id["b"].output_settings is None


def test_parse_site_ignores_non_dict_output_settings():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "devices": [{"deviceId": "a", "outputType": "LIGHT", "outputSettings": "bad"}],
        }
    )
    assert site.devices[0].output_settings is None


def test_parse_site_firmware_by_device_covers_all_physical_devices():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "plejdDevices": [
                {
                    "deviceId": "d1",
                    "hardwareId": "1",
                    "faceplateId": 7,
                    "firmware": {"version": "6.43.3", "buildTime": 20260324155701},
                },
                {"deviceId": "w1", "hardwareId": "70", "firmware": {"version": "4.41.3", "buildTime": 20240910153670}},
            ],
            # A GWY-01 lives in gateways[], not plejdDevices, and carries its firmware
            # dict under `firmwareObject` (its `firmware` is a bare buildTime int).
            "gateways": [
                {
                    "deviceId": "gw1",
                    "hardwareId": "4",
                    "firmware": 20230207104904,
                    "firmwareObject": {"version": "2.3.1", "buildTime": 20230207104904},
                }
            ],
            "devices": [{"deviceId": "d1", "outputType": "LIGHT"}],  # only d1 is a controllable output
        }
    )
    fw = site.firmware_by_device
    assert set(fw) == {"d1", "w1", "gw1"}  # outputs + sensors + gateway, not just controllable outputs
    assert fw["d1"].version == "6.43.3" and fw["d1"].build_time == 20260324155701
    assert fw["d1"].hardware_id == 1 and fw["d1"].faceplate_id == "7"
    assert fw["w1"].version == "4.41.3"
    # gateway firmware comes from firmwareObject, not the bare int `firmware`
    assert fw["gw1"].version == "2.3.1" and fw["gw1"].build_time == 20230207104904 and fw["gw1"].hardware_id == 4


def test_parse_site_firmware_tolerates_missing_or_garbage():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "plejdDevices": [
                {"deviceId": "a", "hardwareId": "1"},  # no firmware/faceplate at all
                {"deviceId": "b", "hardwareId": "1", "firmware": {"version": 5, "buildTime": "nope"}},
                {"hardwareId": "1"},  # no deviceId -> skipped
            ],
            "devices": [],
        }
    )
    fw = site.firmware_by_device
    assert set(fw) == {"a", "b"}
    assert fw["a"].version is None and fw["a"].build_time is None and fw["a"].faceplate_id is None
    assert fw["b"].version is None  # non-str version dropped
    assert fw["b"].build_time is None  # non-numeric buildTime dropped


def test_parse_site_device_addresses_drops_garbage_values():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "deviceAddress": {"d1": 5, "d2": "not-a-number", "d3": None, "d4": 300, "d5": -1, "d6": 0, "d7": True},
            "devices": [],
        }
    )
    assert site.device_addresses == {"d1": 5}


async def test_available_firmware_returns_newest_offered():
    with aioresponses() as m:
        m.post(
            _FIRMWARE,
            payload={
                "result": [
                    {"version": "6.40.0", "buildTime": 20251201000000},
                    {"version": "6.43.3", "buildTime": 20260324155701},
                ]
            },
        )
        async with aiohttp.ClientSession() as s:
            latest = await async_get_available_firmware(s, "tok", 1, "0")
    assert latest == ("6.43.3", 20260324155701)


async def test_available_firmware_empty_list_means_up_to_date():
    with aioresponses() as m:
        m.post(_FIRMWARE, payload={"result": []})
        async with aiohttp.ClientSession() as s:
            assert await async_get_available_firmware(s, "tok", 1, None) is None


async def test_available_firmware_skips_malformed_entries():
    with aioresponses() as m:
        m.post(
            _FIRMWARE,
            payload={"result": ["junk", {"version": "x"}, {"buildTime": 1}, {"version": "6.1.0", "buildTime": 42}]},
        )
        async with aiohttp.ClientSession() as s:
            assert await async_get_available_firmware(s, "tok", 9, None) == ("6.1.0", 42)


_COMPATIBLE_DEVICES = PLEJD_PARSE_URL + PLEJD_FN_COMPATIBLE_DEVICES


async def test_needed_output_count_reads_needed_addresses():
    with aioresponses() as m:
        m.post(_COMPATIBLE_DEVICES, payload={"result": {"compatible": [{"24": {"neededAddresses": 2}}]}})
        async with aiohttp.ClientSession() as s:
            assert await async_get_needed_output_count(s, "tok", "24", 20240101000000) == 2


async def test_needed_output_count_zero_for_wall_controller():
    with aioresponses() as m:
        m.post(_COMPATIBLE_DEVICES, payload={"result": {"compatible": [{"10": {"neededAddresses": 0}}]}})
        async with aiohttp.ClientSession() as s:
            assert await async_get_needed_output_count(s, "tok", "10", 20240101000000) == 0


async def test_needed_output_count_falls_back_to_one_when_not_confirmed():
    with aioresponses() as m:
        m.post(_COMPATIBLE_DEVICES, payload={"result": {"compatible": [], "incompatible": ["22"]}})
        async with aiohttp.ClientSession() as s:
            assert await async_get_needed_output_count(s, "tok", "22", 20240101000000) == 1


async def test_needed_output_count_sends_hardware_and_build_time():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": {"compatible": [{"22": {"neededAddresses": 1}}]}})

    with aioresponses() as m:
        m.post(_COMPATIBLE_DEVICES, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_get_needed_output_count(s, "tok", "22", 20240701133622)
    assert captured["devices"] == [
        {"buildTime": 20240701133622, "firmwareNumber": None, "hardwareId": "22", "faceplateId": "0", "variant": None}
    ]


_UPDATE_DEVICE = PLEJD_PARSE_URL + "functions/updateDevice_V2"


def test_parse_site_extracts_output_object_id():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16},
            "plejdDevices": [{"deviceId": "F161F68198AF", "hardwareId": "1"}],
            "devices": [{"deviceId": "F161F68198AF", "outputType": "LIGHT", "objectId": "7MK7dlrcfz"}],
        }
    )
    assert site.devices[0].object_id == "7MK7dlrcfz"


async def test_set_device_title_success():
    with aioresponses() as m:
        m.post(_UPDATE_DEVICE, payload={"result": True})
        async with aiohttp.ClientSession() as s:
            ok = await async_set_device_title(s, "tok", "site-1", "F161F68198AF", "7MK7dlrcfz", "Vardagsrum")
    assert ok is True


async def test_set_device_title_false_when_cloud_rejects():
    with aioresponses() as m:
        m.post(_UPDATE_DEVICE, payload={"result": False})
        async with aiohttp.ClientSession() as s:
            assert await async_set_device_title(s, "tok", "site-1", "d1", "p1", "X") is False


def test_parse_site_extracts_mesh_key():
    site = parse_site(
        {
            "plejdMesh": {"cryptoKey": "00" * 16, "meshKey": "AB-CD-EF-01"},
            "devices": [],
        }
    )
    assert site.mesh_key == "AB-CD-EF-01"


def test_parse_site_mesh_key_defaults_to_empty_string():
    site = parse_site({"plejdMesh": {"cryptoKey": "00" * 16}, "devices": []})
    assert site.mesh_key == ""


# ---- createPlejdDevice_V2 ----


def test_parse_new_device_addresses_full_response():
    result = {
        "deviceAddress": 5,
        "outputAddress": {"0": 50, "1": 51},
    }
    addrs = _parse_new_device_addresses(result)
    assert addrs.device_address == 5
    assert addrs.output_addresses == {0: 50, 1: 51}


def test_parse_new_device_addresses_non_dict_returns_empty():
    addrs = _parse_new_device_addresses(None)
    assert addrs.device_address is None
    assert addrs.output_addresses == {}


def test_parse_new_device_addresses_skips_invalid_output_keys():
    result = {"deviceAddress": 7, "outputAddress": {"0": 70, "bad": "skip", "1": None}}
    addrs = _parse_new_device_addresses(result)
    assert addrs.device_address == 7
    assert addrs.output_addresses == {0: 70}


async def test_async_create_device_sends_required_fields():
    with aioresponses() as m:
        m.post(_CREATE_DEVICE, payload={"result": {"deviceAddress": 10, "outputAddress": {"0": 100}}})
        async with aiohttp.ClientSession() as s:
            addrs = await async_create_device(s, "tok", "site1", "aabbccddeeff", "1", 20241101000000)
    assert addrs.device_address == 10
    assert addrs.output_addresses == {0: 100}


async def test_async_create_device_always_sends_variant_and_installation_location():
    """The cloud function rejects the request if these keys are missing entirely (even as null)."""
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": {"deviceAddress": 1}})

    with aioresponses() as m:
        m.post(_CREATE_DEVICE, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_create_device(s, "tok", "site1", "aabbccddeeff", "1", 0)
    assert "variant" in captured and captured["variant"] is None
    assert "installationLocation" in captured and captured["installationLocation"] == ""


async def test_async_create_device_sends_optional_fields():
    with aioresponses() as m:
        m.post(_CREATE_DEVICE, payload={"result": {"deviceAddress": 3}})
        async with aiohttp.ClientSession() as s:
            addrs = await async_create_device(
                s,
                "tok",
                "site1",
                "aabbccddeeff",
                "1",
                20241101000000,
                device_infos=[NewDeviceInfo(title="Kitchen", output_index=0, room_id="r1")],
                faceplate_id="fp1",
                variant="v2",
                installation_location="ceiling",
            )
    assert addrs.device_address == 3


async def test_async_create_device_error_raises():
    with aioresponses() as m:
        m.post(_CREATE_DEVICE, status=400, payload={"error": "bad request"})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdCloudError, match="bad request"):
                await async_create_device(s, "tok", "site1", "aabbccddeeff", "1", 0)


async def test_create_room_posts_correct_payload_and_returns_uuid():
    with aioresponses() as m:
        m.post(_CREATE_ROOM, payload={"result": 1})
        async with aiohttp.ClientSession() as s:
            room_id = await async_create_room(s, "tok", "site1", "Bibliotek")
    # room_id must be a UUID string
    import uuid as _uuid

    _uuid.UUID(room_id)  # raises if not a valid UUID


async def test_create_room_uses_supplied_category():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": 1})

    with aioresponses() as m:
        m.post(_CREATE_ROOM, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_create_room(s, "tok", "site1", "Garage", category="Garage")
    assert captured["title"] == "Garage"
    assert captured["category"] == "Garage"


async def test_update_room_sends_only_provided_fields():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": True})

    with aioresponses() as m:
        m.post(_UPDATE_ROOM, callback=_capture)
        async with aiohttp.ClientSession() as s:
            ok = await async_update_room(s, "tok", "site1", "room1", title="Vardagsrum")
    assert ok is True
    assert captured == {"siteId": "site1", "roomId": "room1", "title": "Vardagsrum"}


async def test_update_room_sends_all_provided_fields():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": True})

    with aioresponses() as m:
        m.post(_UPDATE_ROOM, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_update_room(s, "tok", "site1", "room1", title="Kök", order=2, category="Kitchen")
    assert captured == {"siteId": "site1", "roomId": "room1", "title": "Kök", "order": 2, "category": "Kitchen"}


async def test_update_room_rejects_malformed_truthy_result():
    # A malformed but truthy `result` (not the literal True the real API sends on success)
    # must not be treated as a successful update - reject it strictly, like device rename.
    with aioresponses() as m:
        m.post(_UPDATE_ROOM, payload={"result": "yes"})
        async with aiohttp.ClientSession() as s:
            ok = await async_update_room(s, "tok", "site1", "room1", title="X")
    assert ok is False


async def test_remove_room_posts_correct_payload():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": True})

    with aioresponses() as m:
        m.post(_REMOVE_ROOM, callback=_capture)
        async with aiohttp.ClientSession() as s:
            ok = await async_remove_room(s, "tok", "site1", "room1")
    assert ok is True
    assert captured == {"siteId": "site1", "roomId": "room1"}


async def test_remove_room_rejects_malformed_truthy_result():
    with aioresponses() as m:
        m.post(_REMOVE_ROOM, payload={"result": {}})
        async with aiohttp.ClientSession() as s:
            ok = await async_remove_room(s, "tok", "site1", "room1")
    assert ok is False


_STEP = {"device_id": "d1", "output": 0, "state": "On", "value": 255}


async def test_create_scene_posts_correct_payload_and_returns_uuid():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": 1})

    with aioresponses() as m:
        m.post(_CREATE_SCENE, callback=_capture)
        async with aiohttp.ClientSession() as s:
            scene_id = await async_create_scene(s, "tok", "site1", "Movie Night", [_STEP])
    import uuid as _uuid

    _uuid.UUID(scene_id)  # raises if not a valid UUID
    assert captured == {
        "siteId": "site1",
        "sceneId": scene_id,
        "title": "Movie Night",
        "order": 0,
        "sceneSteps": [
            {"deviceId": "d1", "output": 0, "dirty": True, "dirtyRemoved": False, "state": "On", "value": 255}
        ],
        "hiddenFromSceneList": False,
        "settings": "",
    }


async def test_create_scene_forwards_optional_step_fields():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": 1})

    step = {**_STEP, "color_temperature": 3000, "coverable_tilt": 50, "climate_boost_time": 30}
    with aioresponses() as m:
        m.post(_CREATE_SCENE, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_create_scene(s, "tok", "site1", "X", [step], order=2, hidden_from_scene_list=True)
    assert captured["order"] == 2
    assert captured["hiddenFromSceneList"] is True
    assert captured["sceneSteps"][0]["colorTemperature"] == 3000
    assert captured["sceneSteps"][0]["coverableTilt"] == 50
    assert captured["sceneSteps"][0]["climateBoostTime"] == 30


async def test_update_scene_sends_only_provided_fields():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": True})

    with aioresponses() as m:
        m.post(_UPDATE_SCENE, callback=_capture)
        async with aiohttp.ClientSession() as s:
            ok = await async_update_scene(s, "tok", "site1", "scene1", title="Renamed")
    assert ok is True
    assert captured == {"siteId": "site1", "sceneId": "scene1", "title": "Renamed"}


async def test_update_scene_sends_all_provided_fields():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": True})

    with aioresponses() as m:
        m.post(_UPDATE_SCENE, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_update_scene(
                s,
                "tok",
                "site1",
                "scene1",
                title="X",
                order=3,
                scene_steps=[_STEP],
                hidden_from_scene_list=True,
                settings="{}",
            )
    assert captured["title"] == "X"
    assert captured["order"] == 3
    assert captured["hiddenFromSceneList"] is True
    assert captured["settings"] == "{}"
    assert captured["sceneSteps"] == [
        {"deviceId": "d1", "output": 0, "dirty": True, "dirtyRemoved": False, "state": "On", "value": 255}
    ]


async def test_update_scene_rejects_malformed_truthy_result():
    with aioresponses() as m:
        m.post(_UPDATE_SCENE, payload={"result": "yes"})
        async with aiohttp.ClientSession() as s:
            ok = await async_update_scene(s, "tok", "site1", "scene1", title="X")
    assert ok is False


async def test_remove_scene_posts_correct_payload():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": True})

    with aioresponses() as m:
        m.post(_REMOVE_SCENE, callback=_capture)
        async with aiohttp.ClientSession() as s:
            ok = await async_remove_scene(s, "tok", "site1", "scene1")
    assert ok is True
    assert captured == {"siteId": "site1", "sceneId": "scene1"}


async def test_remove_scene_rejects_malformed_truthy_result():
    with aioresponses() as m:
        m.post(_REMOVE_SCENE, payload={"result": {}})
        async with aiohttp.ClientSession() as s:
            ok = await async_remove_scene(s, "tok", "site1", "scene1")
    assert ok is False


async def test_remove_device_posts_correct_payload():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": True})

    with aioresponses() as m:
        m.post(_REMOVE_DEVICE, callback=_capture)
        async with aiohttp.ClientSession() as s:
            ok = await async_remove_device(s, "tok", "site1", "d1")
    assert ok is True
    assert captured == {"siteId": "site1", "deviceId": "d1"}


async def test_remove_device_rejects_malformed_truthy_result():
    with aioresponses() as m:
        m.post(_REMOVE_DEVICE, payload={"result": {}})
        async with aiohttp.ClientSession() as s:
            ok = await async_remove_device(s, "tok", "site1", "d1")
    assert ok is False


async def test_set_input_setting_posts_toggle():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": True})

    with aioresponses() as m:
        m.post(_SET_INPUT, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_set_input_setting(s, "tok", "site1", "aabbccddeeff", 0, "Toggle")
    assert captured["deviceId"] == "aabbccddeeff"
    assert captured["input"] == 0
    assert captured["buttonType"] == "Toggle"
    assert captured["doubleSidedDirectionButton"] is False


def test_parse_site_parses_rooms_with_group_addresses():
    site = {
        **_SITE,
        "rooms": [
            {"roomId": "r1", "title": "Kitchen"},
            {"roomId": "r3", "title": "Empty room"},
        ],
        # r2 is absent from rooms[] -> name falls back to "Room"; r4's address is non-int
        # -> skipped; r5's is a bool (int subclass) -> also skipped, not silently coerced.
        "roomAddress": {"r1": 14, "r2": 16, "r3": 99, "r4": "bad", "r5": True},
        # d1 (a LIGHT) belongs to both groups; both groups have only light members.
        "outputGroups": {"d1": {"0": [14, 16]}},
    }
    rooms = parse_site(site).rooms
    by_addr = {r.address: r for r in rooms}
    assert set(by_addr) == {14, 16}  # r3 (no members) and r4 (bad address) are dropped
    assert by_addr[14].name == "Kitchen"
    assert by_addr[14].member_addresses == [11]
    assert by_addr[14].dimmable is True
    assert by_addr[16].name == "Room"  # fallback when the room has no title entry
    assert by_addr[16].member_addresses == [11]  # d1.out0 also belongs to group 16


def test_parse_site_all_rooms_includes_empty_and_non_light_rooms():
    # all_rooms (for room management) must not share `rooms`'s light-grouping filtering:
    # an empty room (r3, no group members) and a room with only a non-light member would
    # both be silently dropped from `rooms`, which is exactly wrong for "does this room
    # exist" / "is it safe to delete" (see PR #114 review).
    site = {
        **_SITE,
        "rooms": [
            {"roomId": "r1", "title": "Kitchen"},
            {"roomId": "r3", "title": "Empty room"},
        ],
        "roomAddress": {"r1": 14, "r3": 99},
        "outputGroups": {"d1": {"0": [14]}},
    }
    parsed = parse_site(site)
    assert [r.address for r in parsed.rooms] == [14]  # r3 still excluded from the light-group list
    by_id = {r.room_id: r for r in parsed.all_rooms}
    assert set(by_id) == {"r1", "r3"}
    assert by_id["r1"].name == "Kitchen"
    assert by_id["r1"].has_devices is True  # d1/d2 (from _SITE's devices[]) have roomId "r1"
    assert by_id["r1"].address == 14
    assert by_id["r3"].name == "Empty room"
    assert by_id["r3"].has_devices is False
    # r3 has no light-group members so it's excluded from `rooms`, but move_device_to_room
    # still needs its address to target the room even though it has no light entity.
    assert by_id["r3"].address == 99


def test_parse_site_all_rooms_address_is_none_for_a_malformed_room_address():
    site = {
        **_SITE,
        "rooms": [{"roomId": "r1", "title": "Kitchen"}],
        "roomAddress": {"r1": "not-a-number"},
    }
    all_rooms = parse_site(site).all_rooms
    assert all_rooms[0].address is None


def test_parse_site_all_rooms_skips_malformed_room_entries():
    site = {**_SITE, "rooms": [{"roomId": "r1", "title": "Kitchen"}, "not-a-dict", {"title": "no id"}, None]}
    all_rooms = parse_site(site).all_rooms
    assert [r.room_id for r in all_rooms] == ["r1"]


def test_parse_site_all_rooms_includes_a_room_with_no_matching_rooms_entry():
    # roomAddress/outputGroups can carry a room_id absent from rooms[] itself (e.g. after
    # a partial deletion) - all_rooms's whole purpose is "every room on the site", so it
    # must include that room too, not silently omit it.
    site = {
        **_SITE,
        "rooms": [{"roomId": "r1", "title": "Kitchen"}],
        "roomAddress": {"r1": 14, "r99": 16},  # r99 has no entry in rooms[] at all
    }
    all_rooms = parse_site(site).all_rooms
    by_id = {r.room_id: r for r in all_rooms}
    assert set(by_id) == {"r1", "r99"}
    assert by_id["r99"].name == "Room"
    assert by_id["r99"].address == 16
    assert by_id["r99"].has_devices is False


def test_parse_site_all_scenes_includes_scenes_missing_from_scene_index():
    # all_scenes (for scene management) must not share `scenes`'s mesh-index filtering:
    # _SITE's sc2 has no sceneIndex entry, so it's excluded from `scenes` (used for
    # execution - a broadcast needs a real index), which is exactly wrong for "does this
    # scene exist" (same issue as PlejdCloudRoom vs all_rooms, #114).
    parsed = parse_site(_SITE)
    assert [s.scene_id for s in parsed.scenes] == ["sc1"]  # sc2 still excluded (no index)
    by_id = {s.scene_id: s for s in parsed.all_scenes}
    assert set(by_id) == {"sc1", "sc2"}
    assert by_id["sc1"].name == "Movie"
    assert by_id["sc2"].name == "NoIndex"


def test_parse_site_all_scenes_skips_malformed_scene_entries():
    site = {**_SITE, "scenes": [{"sceneId": "sc1", "title": "Movie"}, "not-a-dict", {"title": "no id"}, None]}
    all_scenes = parse_site(site).all_scenes
    assert [s.scene_id for s in all_scenes] == ["sc1"]


def test_parse_site_excludes_room_with_non_light_member():
    site = {
        **_SITE,
        "roomAddress": {"r1": 14},
        # d1 (a LIGHT) and d2 (a RELAY) both belong to group 14 - a 0x0098 group command
        # would also toggle the relay, so the whole room is excluded rather than silently
        # dropping the non-light member from an otherwise-created room.
        "outputGroups": {"d1": {"0": [14]}, "d2": {"0": [14]}},
    }
    assert parse_site(site).rooms == []


def test_parse_site_rejects_out_of_range_group_address():
    site = {
        **_SITE,
        # mesh addresses are single-byte (encode_command masks with & 0xFF); 0, negative,
        # and >255 group addresses must not silently wrap onto a real device's address.
        "roomAddress": {"r1": 0, "r2": -1, "r3": 256, "r4": 14},
        "outputGroups": {"d1": {"0": [0, -1, 256, 14]}},
    }
    rooms = parse_site(site).rooms
    assert [r.room_id for r in rooms] == ["r4"]


def test_parse_site_skips_malformed_output_group_entries():
    site = {
        **_SITE,
        "roomAddress": {"r1": 14},
        # d1's own group list has one malformed (non-int) group id alongside the valid one;
        # d5 (not in devices[], so its own outputAddress cast can't crash devices-parsing)
        # has a malformed (non-int) output address for an otherwise-valid group. Neither
        # entry may abort the whole site parse - both are skipped like a bad roomAddress.
        "outputGroups": {"d1": {"0": [14, "bad-group"]}, "d5": {"0": [14]}},
        "outputAddress": {**_SITE["outputAddress"], "d5": {"0": "bad-address"}},
    }
    rooms = parse_site(site).rooms
    assert len(rooms) == 1
    assert rooms[0].member_addresses == [11]  # only d1.out0 survives


def test_parse_site_skips_non_list_output_group_value():
    site = {
        **_SITE,
        "roomAddress": {"r1": 14},
        # d1's own group membership is a scalar, not a list of group addresses -> must be
        # skipped instead of raising (an int isn't iterable; a dict would silently iterate
        # its keys instead of the intended group addresses).
        "outputGroups": {"d1": {"0": 14}},
    }
    rooms = parse_site(site).rooms
    assert rooms == []  # the malformed membership yields no members -> room dropped


def test_parse_site_tolerates_non_dict_room_and_group_fields():
    site = {**_SITE, "roomAddress": ["not", "a", "dict"], "outputGroups": ["also", "not", "a", "dict"]}
    assert parse_site(site).rooms == []  # both malformed top-level fields are treated as absent


def test_parse_site_skips_non_dict_room_entry():
    site = {
        **_SITE,
        # a stray non-dict entry alongside a valid one must not abort parsing
        "rooms": [{"roomId": "r1", "title": "Kitchen"}, "not-a-dict", None],
        "roomAddress": {"r1": 14},
        "outputGroups": {"d1": {"0": [14]}},
    }
    rooms = parse_site(site).rooms
    assert len(rooms) == 1 and rooms[0].name == "Kitchen"


def test_parse_site_tolerates_non_list_rooms_field():
    site = {
        **_SITE,
        # rooms is a scalar (untrusted cloud data), not a list -> treated as absent
        # instead of raising when building room_titles.
        "rooms": "not-a-list",
        "roomAddress": {"r1": 14},
        "outputGroups": {"d1": {"0": [14]}},
    }
    rooms = parse_site(site).rooms
    assert len(rooms) == 1 and rooms[0].name == "Room"  # falls back like a missing title does


def test_parse_site_skips_non_dict_output_group_membership_map():
    site = {
        **_SITE,
        "roomAddress": {"r1": 14},
        # d1's whole membership map is a scalar, not {outputIdx: [groups]} -> skipped
        # entirely instead of raising on a bad .items() call.
        "outputGroups": {"d1": "not-a-dict"},
    }
    assert parse_site(site).rooms == []


def test_parse_site_ignores_non_string_room_title():
    site = {
        **_SITE,
        # a non-string title must not crash the .strip() call -> falls back like a
        # missing title does ("Room")
        "rooms": [{"roomId": "r1", "title": 123}],
        "roomAddress": {"r1": 14},
        "outputGroups": {"d1": {"0": [14]}},
    }
    rooms = parse_site(site).rooms
    assert len(rooms) == 1 and rooms[0].name == "Room"


def test_parse_site_tolerates_non_dict_output_address_fields():
    site = {
        **_SITE,
        "roomAddress": {"r1": 14},
        "outputGroups": {"d1": {"0": [14]}},
        # the whole outputAddress map is malformed (untrusted cloud data) -> treated as
        # empty rather than raising when the room-membership loop calls .get() on it;
        # d1 is still resolved via the deviceAddress fallback (mirrors the device
        # parser's own fallback for a single-output light)
        "outputAddress": ["not", "a", "dict"],
    }
    rooms = parse_site(site).rooms
    assert len(rooms) == 1
    assert rooms[0].member_addresses == [1]  # d1's deviceAddress (1), not its missing outputAddress


def test_parse_site_skips_non_dict_per_device_output_address():
    site = {
        **_SITE,
        "roomAddress": {"r1": 14},
        # d5 doesn't appear in devices[] (so its malformed entry can't crash the
        # unrelated device-parsing loop); its own outputAddress value is a scalar, not
        # {outputIdx: address} -> its membership must be skipped, not raise.
        "outputGroups": {"d5": {"0": [14]}},
        "outputAddress": {**_SITE["outputAddress"], "d5": "not-a-dict"},
    }
    assert parse_site(site).rooms == []


def test_parse_site_room_membership_falls_back_to_device_address():
    site = {
        **_SITE,
        "roomAddress": {"r1": 14},
        "outputGroups": {"d1": {"0": [14]}},
        # d1 has no entry in outputAddress at all (a valid dict, just missing this key)
        # -> falls back to deviceAddress, mirroring the device parser's own fallback
        # for a single-output light whose outputAddress the cloud omits.
        "outputAddress": {"d2": _SITE["outputAddress"]["d2"]},
    }
    rooms = parse_site(site).rooms
    assert len(rooms) == 1
    assert rooms[0].member_addresses == [1]  # d1's deviceAddress


def test_parse_site_room_not_dimmable_when_no_member_is_dimmable():
    site = {
        **_SITE,
        "roomAddress": {"r1": 14},
        "outputGroups": {"d1": {"0": [14]}},
        # d1 is a LIGHT but explicitly lacks the Dimmable trait -> on/off only.
        "devices": [
            {"deviceId": "d1", "title": "Kitchen", "roomId": "r1", "outputType": "LIGHT", "traits": 0},
        ],
    }
    rooms = parse_site(site).rooms
    assert len(rooms) == 1
    assert rooms[0].dimmable is False


def test_parse_site_room_dimmable_addresses_excludes_on_off_only_members():
    site = {
        **_SITE,
        "roomAddress": {"r1": 14},
        "outputGroups": {"d1": {"0": [14]}, "d6": {"0": [14]}},
        "outputAddress": {**_SITE["outputAddress"], "d6": {"0": 61}},
        "plejdDevices": [*_SITE["plejdDevices"], {"deviceId": "d6", "hardwareId": "1"}],
        "devices": [
            *_SITE["devices"],
            {"deviceId": "d6", "title": "Lamp", "roomId": "r1", "outputType": "LIGHT", "traits": 0},
        ],
    }
    rooms = parse_site(site).rooms
    assert len(rooms) == 1
    room = rooms[0]
    assert set(room.member_addresses) == {11, 61}
    assert room.dimmable_addresses == [11]  # d1 is dimmable; d6 (traits=0) is on/off only


def test_parse_site_marks_nothing_malformed_for_a_well_formed_empty_site():
    # A genuinely empty-but-well-formed site (every collection key present as its correct
    # empty type) must not be flagged - only an absent/wrong-type field is a parse concern,
    # not a caller-level "is this data trustworthy" one.
    site = {
        "siteId": "S1",
        "title": "Empty",
        "plejdMesh": {"cryptoKey": "00" * 16},
        "devices": [],
        "deviceAddress": {},
        "outputAddress": {},
        "inputAddress": {},
        "plejdDevices": [],
        "scenes": [],
        "sceneIndex": {},
        "rooms": [],
        "roomAddress": {},
        "outputGroups": {},
        "gateways": [],
    }
    assert parse_site(site).malformed == frozenset()


@pytest.mark.parametrize(
    ("key", "bad_value", "label"),
    [
        ("devices", {"d1": {}}, "devices"),
        ("inputAddress", [], "inputs"),
        ("plejdDevices", {}, "motion"),
        ("scenes", "not-a-list", "scenes"),
        ("rooms", "not-a-list", "rooms"),
        ("gateways", "not-a-list", "gateways"),
    ],
)
def test_parse_site_marks_a_collection_malformed_when_its_source_field_is_the_wrong_type(key, bad_value, label):
    site = {**_SITE, key: bad_value}
    assert label in parse_site(site).malformed


def test_parse_site_marks_rooms_malformed_when_room_address_is_the_wrong_type():
    site = {**_SITE, "rooms": [], "roomAddress": "not-a-dict"}
    assert "rooms" in parse_site(site).malformed


@pytest.mark.parametrize(
    ("key", "bad_value", "label"),
    [
        ("devices", {"d1": {"deviceId": "d1"}}, "devices"),  # object instead of list
        ("inputAddress", [{"0": 11}], "inputs"),  # list instead of object
        ("plejdDevices", {"d1": {"hardwareId": "1"}}, "motion"),
        ("scenes", {"sc1": {"sceneId": "sc1"}}, "scenes"),
        ("rooms", {"r1": {"roomId": "r1"}}, "rooms"),
        ("roomAddress", [14], "rooms"),
        ("outputGroups", [{"d1": {"0": [14]}}], "rooms"),
    ],
)
def test_parse_site_survives_a_wrong_typed_but_non_empty_collection(key, bad_value, label):
    # A wrong-typed value that is also non-empty must still parse to a flagged-but-usable
    # site rather than raising mid-parse (AttributeError/TypeError) - the caller can only
    # skip a malformed snapshot if `malformed` actually reaches it.
    site = parse_site({**_SITE, key: bad_value})
    assert label in site.malformed


@pytest.mark.parametrize(
    ("key", "labels"),
    [("outputAddress", {"devices"}), ("deviceAddress", {"devices", "motion"})],
)
def test_parse_site_marks_devices_malformed_when_an_address_map_is_the_wrong_type(key, labels):
    # outputAddress is what control commands target and deviceAddress is the physical
    # fallback (also the motion sensors' own address source) - a present-but-corrupt map
    # parses into entities with address=None that can never be commanded, so it must not
    # look like a valid snapshot worth caching over the working one.
    assert labels <= parse_site({**_SITE, key: "not-a-dict"}).malformed


async def test_login_rate_limit_is_transient_not_an_auth_failure():
    # A 429 (the daily poll's own retries can provoke one) is not the server rejecting
    # these credentials - mapping it to PlejdAuthError would start reauth for a password
    # that is still perfectly valid.
    with aioresponses() as m:
        m.post(_LOGIN, status=429, payload={"error": "too many requests"})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdCloudError, match="too many requests") as excinfo:
                await async_login(s, "u@x.se", "pw")
    assert not isinstance(excinfo.value, PlejdAuthError)


@pytest.mark.parametrize("key", ["sceneIndex", "roomAddress", "outputGroups"])
def test_parse_site_marks_a_collection_malformed_when_a_companion_map_is_corrupt(key):
    # A partial response can keep the primary list while corrupting the map it is parsed
    # against - scenes with an unusable sceneIndex parses to zero executable scenes, rooms
    # with an unusable roomAddress/outputGroups to zero room entities. Both look like a
    # genuine deletion unless flagged, so the poll would destroy those entities.
    site = {**_SITE, "rooms": [{"roomId": "r1", "title": "Kok"}], "roomAddress": {"r1": 14}, "outputGroups": {}}
    site[key] = "not-a-dict"
    expected = "scenes" if key == "sceneIndex" else "rooms"
    assert expected in parse_site(site).malformed


def test_parse_site_skips_non_object_entries_inside_the_device_collections():
    # The collections are the right type but carry a stray non-object element - those
    # individual entries are skipped rather than crashing the whole parse (which would
    # take the site's every other, perfectly good device with it).
    site = parse_site(
        {
            **_SITE,
            "devices": [*_SITE["devices"], "not-an-object"],
            "plejdDevices": [*_SITE["plejdDevices"], 42],
            "gateways": ["not-an-object"],
        }
    )
    assert {d.device_id for d in site.devices} == {"d1", "d2", "d3"}  # the good ones survive
    assert [(m.address, m.name) for m in site.motion] == [(33, "Motion sensor")]
    assert site.gateways == []


@pytest.mark.parametrize(
    ("key", "labels"),
    [
        ("devices", {"devices"}),
        ("plejdDevices", {"devices", "motion"}),
        ("scenes", {"scenes"}),
        ("rooms", {"rooms"}),
        ("gateways", {"gateways"}),
    ],
)
def test_parse_site_marks_a_collection_malformed_for_a_stray_non_object_entry(key, labels):
    # Surviving the bad element (above) is not enough for a caching, diffing caller: the
    # skipped entry is a device/scene/room missing from the snapshot, which the cloud poll
    # would read as a deliberate deletion and persist. It must be flagged instead.
    site = parse_site({**_SITE, key: [*(_SITE.get(key) or []), "not-an-object"]})
    assert labels <= site.malformed


@pytest.mark.parametrize(
    ("key", "record", "labels"),
    [
        ("devices", {"title": "ghost"}, {"devices"}),  # no deviceId
        ("plejdDevices", {"hardwareId": "1"}, {"devices", "motion"}),
        ("scenes", {"title": "nameless"}, {"scenes"}),
        ("rooms", {"title": "nameless"}, {"rooms"}),
        ("gateways", {"resourceSetId": "rs1"}, {"gateways"}),
    ],
)
def test_parse_site_marks_a_collection_malformed_for_a_record_missing_its_id(key, record, labels):
    # A record present but missing the id everything keys off is truncated data, and the
    # parser can only skip it - which for the diffing cloud poll reads as a deletion. Flag
    # the collection so the snapshot is rejected rather than silently applied short a device.
    site = parse_site({**_SITE, key: [*(_SITE.get(key) or []), record]})
    assert labels <= site.malformed


def test_parse_site_still_skips_a_device_record_missing_its_id():
    # Flagging the collection must not stop the parse producing the other, good devices -
    # a caller that only needs to read the site (not cache it) is unaffected.
    site = parse_site({**_SITE, "devices": [*_SITE["devices"], {"title": "ghost"}]})
    assert {d.device_id for d in site.devices} == {"d1", "d2", "d3"}


@pytest.mark.parametrize(
    ("missing", "labels"),
    [
        ("devices", {"devices"}),
        ("plejdDevices", {"devices", "motion"}),
        ("deviceAddress", {"devices", "motion"}),
    ],
)
def test_parse_site_flags_a_required_collection_that_is_absent(missing, labels):
    # These cannot legitimately be omitted by a site that has devices: omission means the
    # response is truncated, and normalizing it to empty is exactly what would persist
    # CONF_DEVICES=[] and wipe every entity on the next poll.
    site = {**_SITE}
    del site[missing]
    assert labels <= parse_site(site).malformed


def test_parse_site_does_not_flag_a_devices_free_site_missing_its_companions():
    # The companion requirement is conditional on there being devices at all - a site with
    # none has nothing to describe, so omitting plejdDevices/deviceAddress is consistent
    # rather than truncated, and must not block setup.
    site = {"siteId": "S1", "plejdMesh": {"cryptoKey": "00" * 16}, "devices": []}
    assert parse_site(site).malformed == frozenset()


def test_parse_site_does_not_flag_a_site_that_simply_has_no_rooms_or_gateway():
    # Omitting a collection is how the API says the site has none: a BLE-only site has no
    # gateways, a site with no rooms has no rooms/roomAddress/outputGroups. Flagging those
    # would refuse setup, refuse reconfigure and stop the poll ever syncing for such a site,
    # which is far worse than the destructive sync the flag exists to prevent.
    assert "rooms" not in _SITE and "gateways" not in _SITE  # the representative fixture
    assert parse_site(_SITE).malformed == frozenset()


def test_parse_site_picks_the_lowest_input_index_for_a_shared_address():
    # Several inputs can share one mesh address and only the first survives dedup. The winner
    # must be the lowest index, not whichever the cloud's JSON key order happened to emit
    # first - otherwise the cached input_index flips between polls, reloading an unchanged
    # integration and making a later per-input settings write hit a different physical input.
    forward = parse_site({**_SITE, "inputAddress": {"d1": {"0": 11, "1": 11}}})
    reversed_keys = parse_site({**_SITE, "inputAddress": {"d1": {"1": 11, "0": 11}}})
    assert [(i.address, i.input_index) for i in forward.inputs] == [(11, 0)]
    assert [(i.address, i.input_index) for i in reversed_keys.inputs] == [(11, 0)]


@pytest.mark.parametrize(
    ("key", "record", "labels"),
    [
        ("devices", {"deviceId": 7, "title": "int id"}, {"devices"}),
        ("scenes", {"sceneId": 7, "title": "int id"}, {"scenes"}),
        ("gateways", {"deviceId": 7}, {"gateways"}),
    ],
)
def test_parse_site_flags_rather_than_crashes_on_a_non_string_id(key, record, labels):
    # Mixed identifier types (one string id, one int) used to blow up the canonical sort with
    # a TypeError, so untrusted input aborted the parse instead of returning something the
    # caller could reject. The record is skipped and the collection flagged instead.
    site = parse_site({**_SITE, key: [*(_SITE.get(key) or []), record]})
    assert labels <= site.malformed


@pytest.mark.parametrize(
    ("mutation", "why"),
    [
        # d3 is single-output with no outputAddress entry, so it relies on deviceAddress
        ({"deviceAddress": {"d1": 1, "d2": 2, "w1": 33}}, "no address resolves for d3"),
        ({"plejdDevices": [{"deviceId": "d2", "hardwareId": "18"}]}, "no hardware record for d1/d3"),
    ],
)
def test_parse_site_flags_devices_when_one_does_not_fully_resolve(mutation, why):
    # An outcome check rather than per-map coverage checks: a device only works if it resolved
    # to BOTH an address and a hardware record, and a truncated response can break either.
    # A device that came out address-less or hardware-unknown is what the light/switch
    # platforms silently drop, so it must never be cached over a working snapshot.
    site = parse_site({**_SITE, **mutation})
    assert "devices" in site.malformed, why


def test_parse_site_flags_inputs_for_a_non_string_device_key():
    # Mixed key types would also make the candidate sort compare int with str and raise,
    # aborting the parse instead of returning a site the caller can reject.
    site = parse_site({**_SITE, "inputAddress": {7: {"0": 41}, "d3": {"0": 31}}})
    assert "inputs" in site.malformed
    assert [i.device_id for i in site.inputs] == ["d3"]


@pytest.mark.parametrize(
    "groups",
    [
        {"d1": {"0": "truncated"}},  # per-output value is not a list
        {"d1": {"0": ["not-a-number"]}},  # a group entry that is not numeric
    ],
)
def test_parse_site_flags_rooms_for_a_malformed_per_output_group_list(groups):
    # Each of these silently loses a group membership, shrinking site.rooms into something the
    # unattended poll would persist and use to remove the affected room entity.
    site = parse_site({**_SITE, "roomAddress": {"r1": 14}, "outputGroups": groups})
    assert "rooms" in site.malformed


def test_parse_site_flags_gateways_for_a_non_string_resource_set_id():
    # The value goes straight into the WebSocket's Resource-Set-ID header, and persisting a
    # truthy non-string over a working cached id would fail every connection while matching
    # the cached snapshot, so later polls would see no change and never repair it.
    site = parse_site({**_SITE, "gateways": [{"deviceId": "gw1", "resourceSetId": 123}]})
    assert "gateways" in site.malformed
    assert site.resource_set_id is None


def test_parse_site_flags_rooms_for_a_malformed_per_device_output_group_map():
    # Skipping the bad per-device entry keeps the parse alive but drops that device's group
    # memberships, shrinking or emptying the room list - which the diffing poll would read as
    # a real change and use to remove those room entities.
    site = parse_site({**_SITE, "roomAddress": {"r1": 14}, "outputGroups": {"d1": "truncated"}})
    assert "rooms" in site.malformed


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"d1": "not-a-dict"},  # the whole per-device map is corrupt
        {"d1": {"nope": 11}},  # non-numeric input index
        {"d1": {"0": "nope"}},  # non-numeric address
    ],
)
def test_parse_site_flags_inputs_for_a_malformed_input_entry(bad_entry):
    # The bad entry is skipped so the parse survives, but skipping silently drops real
    # buttons - which the diffing poll would read as a deletion and persist, removing those
    # event entities. The collection must be flagged so the snapshot is rejected instead.
    site = parse_site({**_SITE, "inputAddress": {**bad_entry, "d3": {"0": 31}}})
    assert [(i.device_id, i.address) for i in site.inputs] == [("d3", 31)]  # the good one survives
    assert "inputs" in site.malformed


def test_parse_site_skips_a_motion_sensor_with_a_non_string_id():
    # The sensor is dropped rather than crashing the canonical sort, and plejdDevices is
    # flagged so a caching caller rejects the snapshot instead of losing the sensor quietly.
    site = parse_site({**_SITE, "plejdDevices": [*_SITE["plejdDevices"], {"deviceId": 7, "hardwareId": "70"}]})
    assert [m.device_id for m in site.motion] == ["w1"]
    assert "motion" in site.malformed


def test_parse_site_returns_collections_in_a_canonical_order():
    # parse_site is the single place ordering is normalized, so every caller that stores a
    # snapshot (config flow setup/reconfigure, the cloud poll) writes the same order for the
    # same site - otherwise the first poll after setup diffs unequal and reloads for nothing.
    shuffled = {
        **_SITE,
        "devices": list(reversed(_SITE["devices"])),
        "inputAddress": {"d3": {"0": 31}, "d1": {"1": 11, "0": 12}},
        "scenes": list(reversed(_SITE["scenes"])),
        "sceneIndex": {"sc1": 3, "sc2": 4},
    }
    site = parse_site(shuffled)
    assert [(d.device_id, d.output_index) for d in site.devices] == sorted(
        (d.device_id, d.output_index) for d in site.devices
    )
    assert [(i.device_id, i.input_index) for i in site.inputs] == sorted(
        (i.device_id, i.input_index) for i in site.inputs
    )
    assert [s.scene_id for s in site.scenes] == sorted(s.scene_id for s in site.scenes)
    assert [s.scene_id for s in site.all_scenes] == sorted(s.scene_id for s in site.all_scenes)


async def test_login_request_timeout_is_transient_not_an_auth_failure():
    # A 408 is the server giving up on the request, not a verdict on the credentials.
    with aioresponses() as m:
        m.post(_LOGIN, status=408, payload={"error": "request timeout"})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdCloudError, match="request timeout") as excinfo:
                await async_login(s, "u@x.se", "pw")
    assert not isinstance(excinfo.value, PlejdAuthError)


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (403, {"error": "forbidden"}),  # e.g. a WAF in front of the API
        (404, {"error": "not found"}),  # e.g. an endpoint rollout, no Parse code
        (400, {"error": "bad request"}),
    ],
)
async def test_login_unrecognized_4xx_without_a_code_is_transient(status, payload):
    # Parse answers a rejected username/password with code 101, so a 4xx carrying no code did
    # not establish that the stored password is wrong - treating it as auth would prompt the
    # user to re-enter a perfectly valid password (and in the daily poll, do so unattended).
    with aioresponses() as m:
        m.post(_LOGIN, status=status, payload=payload)
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdCloudError) as excinfo:
                await async_login(s, "u@x.se", "pw")
    assert not isinstance(excinfo.value, PlejdAuthError)


async def test_login_parse_invalid_login_code_is_an_auth_failure():
    # Parse's own credential-rejection signal (code 101) must still reach reauth, whatever
    # status it arrives with - that is the one case where prompting for the password is right.
    with aioresponses() as m:
        m.post(_LOGIN, status=404, payload={"code": 101, "error": "invalid login parameters"})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdAuthError, match="invalid login parameters"):
                await async_login(s, "u@x.se", "pw")


def test_parse_site_sorts_device_outputs_regardless_of_output_address_key_order():
    # outputAddress is a JSON object, so its key order is not semantically meaningful and
    # can wobble between requests. `outputs` must be canonicalized, or coordinator.py's
    # cloud-poll diff sees an unchanged site as changed and reloads for nothing.
    forward = parse_site({**_SITE, "outputAddress": {"d2": {"0": 21, "1": 22}}})
    reversed_keys = parse_site({**_SITE, "outputAddress": {"d2": {"1": 22, "0": 21}}})
    d2_forward = next(d for d in forward.devices if d.device_id == "d2")
    d2_reversed = next(d for d in reversed_keys.devices if d.device_id == "d2")
    assert d2_forward.outputs == d2_reversed.outputs == [21, 22]


async def test_login_server_error_is_transient_not_an_auth_failure():
    # A 5xx means the Plejd cloud is broken, not that the stored password is wrong -
    # callers start HA's reauth flow on PlejdAuthError, which would wrongly prompt the
    # user to re-enter perfectly valid credentials during an outage.
    with aioresponses() as m:
        m.post(_LOGIN, status=503, payload={"error": "service unavailable"})
        async with aiohttp.ClientSession() as s:
            with pytest.raises(PlejdCloudError, match="service unavailable") as excinfo:
                await async_login(s, "u@x.se", "pw")
    assert not isinstance(excinfo.value, PlejdAuthError)


async def test_update_time_event_sends_minimal_payload_without_night_reduction():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": {"targetDevices": [{"deviceId": "d1", "index": 0}], "eventId": "te1"}})

    with aioresponses() as m:
        m.post(_UPDATE_TIME_EVENT, callback=_capture)
        async with aiohttp.ClientSession() as s:
            result = await async_update_time_event(
                s,
                "tok",
                "site1",
                "te1",
                "scene1",
                scheduled_days=[0, 1, 2, 3, 4, 5, 6],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
            )
    assert result == {"targetDevices": [{"deviceId": "d1", "index": 0}], "eventId": "te1"}
    assert captured == {
        "siteId": "site1",
        "timeEventId": "te1",
        "sceneId": "scene1",
        "scheduledDays": [0, 1, 2, 3, 4, 5, 6],
        "fadeTime": 0,
        "activated": True,
        "dirtyDevices": [],
        "dirtyRemovedDevices": [],
        "dirtyRemove": False,
        "mode": "astro",
        "version": 2,
        "start": {"event": "sunset", "offset": 15},
        "end": {"event": "sunrise", "offset": 0},
        "pauseStart": "00:00",
        "pauseEnd": "00:00",
    }


async def test_update_time_event_sends_dirty_devices_and_night_reduction():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": {"targetDevices": [], "eventId": "te1"}})

    with aioresponses() as m:
        m.post(_UPDATE_TIME_EVENT, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_update_time_event(
                s,
                "tok",
                "site1",
                "te1",
                "scene1",
                scheduled_days=[0],
                fade_time=2,
                activated=False,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
                dirty_devices=["d1"],
                night_reduction={
                    "scene_id": "night1",
                    "start_time": "23:15",
                    "end_time": "05:30",
                    "weekend_start_time": "23:59",
                    "weekend_end_time": "06:50",
                },
            )
    assert captured["dirtyDevices"] == ["d1"]
    assert captured["version"] == 3  # night_reduction present -> version 3, not 2
    assert "pauseStart" not in captured and "pauseEnd" not in captured
    assert captured["nightReduction"] == {
        "startTime": "23:15",
        "endTime": "05:30",
        "sceneId": "night1",
        "weekendDeviation": {"startTime": "23:59", "endTime": "06:50"},
    }


async def test_update_time_event_night_reduction_without_weekend_deviation():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": {"targetDevices": [], "eventId": "te1"}})

    with aioresponses() as m:
        m.post(_UPDATE_TIME_EVENT, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_update_time_event(
                s,
                "tok",
                "site1",
                "te1",
                "scene1",
                scheduled_days=[0],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
                night_reduction={"scene_id": "night1", "start_time": "23:15", "end_time": "05:30"},
            )
    assert captured["nightReduction"] == {"startTime": "23:15", "endTime": "05:30", "sceneId": "night1"}
    assert "weekendDeviation" not in captured["nightReduction"]


async def test_update_time_event_rejects_malformed_result():
    with aioresponses() as m:
        m.post(_UPDATE_TIME_EVENT, payload={"result": True})
        async with aiohttp.ClientSession() as s:
            result = await async_update_time_event(
                s,
                "tok",
                "site1",
                "te1",
                "scene1",
                scheduled_days=[0],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
            )
    assert result is None


async def test_update_time_event_rejects_missing_event_id():
    with aioresponses() as m:
        m.post(_UPDATE_TIME_EVENT, payload={"result": {"targetDevices": []}})
        async with aiohttp.ClientSession() as s:
            result = await async_update_time_event(
                s,
                "tok",
                "site1",
                "te1",
                "scene1",
                scheduled_days=[0],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
            )
    assert result is None


async def test_update_time_event_rejects_mismatched_event_id():
    # A response confirming a DIFFERENT TimeEvent than the one asked for must not be
    # mistaken for success - the caller's requested update was not actually applied.
    with aioresponses() as m:
        m.post(_UPDATE_TIME_EVENT, payload={"result": {"targetDevices": [], "eventId": "other-id"}})
        async with aiohttp.ClientSession() as s:
            result = await async_update_time_event(
                s,
                "tok",
                "site1",
                "te1",
                "scene1",
                scheduled_days=[0],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
            )
    assert result is None


async def test_update_time_event_sends_dirty_remove():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": {"targetDevices": [], "eventId": "te1"}})

    with aioresponses() as m:
        m.post(_UPDATE_TIME_EVENT, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_update_time_event(
                s,
                "tok",
                "site1",
                "te1",
                "scene1",
                scheduled_days=[0],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
                dirty_remove=True,
            )
    assert captured["dirtyRemove"] is True


async def test_create_time_event_sends_null_time_event_id_and_target_devices():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": {"targetDevices": [{"deviceId": "d1", "index": 0}], "eventId": "te1"}})

    with aioresponses() as m:
        m.post(_CREATE_TIME_EVENT, callback=_capture)
        async with aiohttp.ClientSession() as s:
            result = await async_create_time_event(
                s,
                "tok",
                "site1",
                "scene1",
                scheduled_days=[0, 1, 2, 3, 4, 5, 6],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
                dirty_devices=["d1"],
            )
    assert result == {"targetDevices": [{"deviceId": "d1", "index": 0}], "eventId": "te1"}
    assert captured == {
        "siteId": "site1",
        "timeEventId": None,
        "sceneId": "scene1",
        "scheduledDays": [0, 1, 2, 3, 4, 5, 6],
        "fadeTime": 0,
        "activated": True,
        "dirtyDevices": ["d1"],
        "dirtyRemovedDevices": [],
        "dirtyRemove": False,
        "mode": "astro",
        "version": 2,
        "start": {"event": "sunset", "offset": 15},
        "end": {"event": "sunrise", "offset": 0},
        "pauseStart": "00:00",
        "pauseEnd": "00:00",
        "targetDevices": ["d1"],
    }


async def test_create_time_event_with_night_reduction_uses_version_3():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": {"targetDevices": [], "eventId": "te1"}})

    with aioresponses() as m:
        m.post(_CREATE_TIME_EVENT, callback=_capture)
        async with aiohttp.ClientSession() as s:
            await async_create_time_event(
                s,
                "tok",
                "site1",
                "scene1",
                scheduled_days=[0],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
                night_reduction={"scene_id": "night1", "start_time": "23:15", "end_time": "05:30"},
            )
    assert captured["version"] == 3
    assert "pauseStart" not in captured and "pauseEnd" not in captured
    assert captured["nightReduction"] == {"startTime": "23:15", "endTime": "05:30", "sceneId": "night1"}


async def test_create_time_event_rejects_malformed_result():
    with aioresponses() as m:
        m.post(_CREATE_TIME_EVENT, payload={"result": True})
        async with aiohttp.ClientSession() as s:
            result = await async_create_time_event(
                s,
                "tok",
                "site1",
                "scene1",
                scheduled_days=[0],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
            )
    assert result is None


async def test_create_time_event_rejects_missing_event_id():
    with aioresponses() as m:
        m.post(_CREATE_TIME_EVENT, payload={"result": {"targetDevices": []}})
        async with aiohttp.ClientSession() as s:
            result = await async_create_time_event(
                s,
                "tok",
                "site1",
                "scene1",
                scheduled_days=[0],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
            )
    assert result is None


async def test_create_time_event_rejects_non_string_event_id():
    with aioresponses() as m:
        m.post(_CREATE_TIME_EVENT, payload={"result": {"targetDevices": [], "eventId": 12345}})
        async with aiohttp.ClientSession() as s:
            result = await async_create_time_event(
                s,
                "tok",
                "site1",
                "scene1",
                scheduled_days=[0],
                fade_time=0,
                activated=True,
                start_event="sunset",
                start_offset=15,
                end_event="sunrise",
                end_offset=0,
            )
    assert result is None


async def test_remove_time_event_posts_correct_payload():
    from aioresponses import CallbackResult

    captured: dict = {}

    def _capture(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return CallbackResult(payload={"result": True})

    with aioresponses() as m:
        m.post(_REMOVE_TIME_EVENT, callback=_capture)
        async with aiohttp.ClientSession() as s:
            ok = await async_remove_time_event(s, "tok", "site1", "te1", device_ids=["d1"])
    assert ok is True
    assert captured == {"siteId": "site1", "timeEventId": "te1", "mode": "astro", "deviceIds": ["d1"]}


async def test_remove_time_event_rejects_malformed_truthy_result():
    with aioresponses() as m:
        m.post(_REMOVE_TIME_EVENT, payload={"result": "yes"})
        async with aiohttp.ClientSession() as s:
            ok = await async_remove_time_event(s, "tok", "site1", "te1", device_ids=["d1"])
    assert ok is False
