"""Tests for async_remove_device (the HA-facing device-removal orchestration)."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from plejd_cloud.cloud import (
    PlejdAuthError,
    PlejdCloudDevice,
    PlejdCloudError,
    PlejdCloudInput,
    PlejdCloudMotion,
    PlejdCloudSite,
)
from plejd_cloud.manage_device import async_remove_device

_KEY = bytes(range(16))


def _device(device_id="d1", name="Spottar", address=11) -> PlejdCloudDevice:
    return PlejdCloudDevice(
        device_id=device_id,
        name=name,
        address=address,
        output_index=0,
        outputs=[11],
        hardware_id=1,
        model="DIM-01",
        category="light",
        dimmable=True,
        traits=9,
        room_id="r1",
    )


def _site(devices=None, inputs=None, motion=None, gateways=None, resource_set_id=None, firmware_by_device=None):
    devices = devices or []
    inputs = inputs or []
    motion = motion or []
    device_addresses = {d.device_id: d.address for d in devices if d.address is not None}
    device_addresses.update({i.device_id: i.address for i in inputs})
    device_addresses.update({m.device_id: m.address for m in motion})
    return PlejdCloudSite(
        site_id="S1",
        title="Home",
        crypto_key=_KEY,
        mesh_key="01-02-03-04",
        devices=devices,
        inputs=inputs,
        motion=motion,
        scenes=[],
        gateways=gateways or [],
        resource_set_id=resource_set_id,
        device_addresses=device_addresses,
        firmware_by_device=firmware_by_device or {},
    )


def _hass():
    return types.SimpleNamespace(
        data={},
        config_entries=types.SimpleNamespace(
            async_update_entry=lambda entry, data, options=None: (
                setattr(entry, "data", data),
                setattr(entry, "options", options if options is not None else entry.options),
            ),
            async_reload=AsyncMock(),
        ),
    )


def _entry(data=None, options=None):
    return types.SimpleNamespace(
        entry_id="e1",
        data=data or {"email": "u@x.com", "password": "pw", "site_id": "S1"},
        options=options or {},
        async_start_reauth=lambda hass: None,
    )


async def test_remove_device_raises_if_device_not_found(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device.async_get_site", AsyncMock(return_value=_site([])))
    with pytest.raises(HomeAssistantError, match="not found"):
        await async_remove_device(_hass(), _entry(), device_id="missing")


async def test_remove_device_raises_on_login_failure(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(side_effect=PlejdCloudError("down")))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error"):
        await async_remove_device(_hass(), _entry(), device_id="d1")


async def test_remove_device_triggers_reauth_on_stale_credentials(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(side_effect=PlejdAuthError("bad creds")))
    hass = _hass()
    entry = _entry()
    entry.async_start_reauth = MagicMock()
    with pytest.raises(HomeAssistantError, match="reauthentication started"):
        await async_remove_device(hass, entry, device_id="d1")
    entry.async_start_reauth.assert_called_once_with(hass)


async def test_remove_device_raises_on_get_site_failure(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device.async_get_site", AsyncMock(side_effect=PlejdCloudError("down")))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error"):
        await async_remove_device(_hass(), _entry(), device_id="d1")


async def test_remove_device_raises_on_cloud_error_during_removal(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device.async_get_site", AsyncMock(return_value=_site([_device()])))
    monkeypatch.setattr(
        "plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(side_effect=PlejdCloudError("down"))
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error removing device"):
        await async_remove_device(_hass(), _entry(), device_id="d1")


async def test_remove_device_raises_when_cloud_rejects_removal(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device.async_get_site", AsyncMock(return_value=_site([_device()])))
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="rejected"):
        await async_remove_device(_hass(), _entry(), device_id="d1")


async def test_remove_device_raises_on_cloud_error_during_refresh(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device.async_get_site", AsyncMock(side_effect=[_site([_device()]), PlejdCloudError("down")])
    )
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=True))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error refreshing site"):
        await async_remove_device(_hass(), _entry(), device_id="d1")


async def test_remove_device_succeeds_and_reloads(monkeypatch):
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device.async_get_site", AsyncMock(side_effect=[_site([_device()]), _site([])])
    )
    remove_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", remove_mock)

    await async_remove_device(hass, entry, device_id="d1")

    remove_mock.assert_awaited_once_with(None, "tok", "S1", "d1")
    assert entry.data["devices"] == []
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


async def test_remove_device_does_not_raise_when_only_the_reload_fails(monkeypatch, caplog):
    # The device's cloud removal already happened (non-idempotent - retrying just gets
    # "not found") by the time the reload is attempted - raising here would report a
    # successful removal as a failed one, with no way to retry through this same path.
    hass = _hass()
    entry = _entry()
    hass.config_entries.async_reload = AsyncMock(return_value=False)
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device.async_get_site", AsyncMock(side_effect=[_site([_device()]), _site([])])
    )
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=True))

    await async_remove_device(hass, entry, device_id="d1")

    assert "entry failed to reload after removing a device" in caplog.text


async def test_remove_device_finds_input_devices(monkeypatch):
    input_dev = PlejdCloudInput(device_id="d2", name="Hallway button", address=22)
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device.async_get_site", AsyncMock(return_value=_site(inputs=[input_dev])))
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="Hallway button"):
        await async_remove_device(_hass(), _entry(), device_id="d2")


async def test_remove_device_finds_motion_sensors(monkeypatch):
    motion_dev = PlejdCloudMotion(device_id="d3", name="Hall motion", address=33)
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device.async_get_site", AsyncMock(return_value=_site(motion=[motion_dev])))
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="Hall motion"):
        await async_remove_device(_hass(), _entry(), device_id="d3")


async def test_remove_device_found_despite_missing_address_entry(monkeypatch):
    # A device with no (or malformed) cloud deviceAddress entry - PlejdCloudDevice.address
    # is None - is exactly the kind of stale/orphaned device this service exists to
    # decommission; it must not be rejected as "not found" for lacking an address.
    device = _device(device_id="d4", name="Orphaned dimmer", address=None)
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device.async_get_site", AsyncMock(return_value=_site([device])))
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="Orphaned dimmer"):
        await async_remove_device(_hass(), _entry(), device_id="d4")


async def test_remove_device_found_via_firmware_by_device_only(monkeypatch):
    # A physical device (e.g. a motion sensor) whose deviceAddress lookup also failed is
    # missing from devices/inputs/motion entirely, not just address-less within one of
    # them - but parse_site() still tracks it in firmware_by_device unconditionally.
    device = _device(device_id="d5", name="Spottar")  # any device present, so the site isn't fully empty
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device.async_get_site",
        AsyncMock(return_value=_site([device], firmware_by_device={"d6": object()})),
    )
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="d6"):  # no display name available - falls back to the id
        await async_remove_device(_hass(), _entry(), device_id="d6")


async def test_remove_device_resets_forced_transport_when_gateway_removed(monkeypatch):
    hass = _hass()
    entry = _entry(options={"transport": "gateway"})
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device.async_get_site",
        AsyncMock(
            side_effect=[
                _site([_device()], gateways=["GWY-01"], resource_set_id="rs1"),
                _site([], gateways=[], resource_set_id=None),  # gateway is gone after removal
            ]
        ),
    )
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=True))

    await async_remove_device(hass, entry, device_id="d1")

    assert entry.options["transport"] == "auto"


async def test_remove_device_keeps_forced_transport_when_gateway_remains(monkeypatch):
    hass = _hass()
    entry = _entry(options={"transport": "gateway"})
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device.async_get_site",
        AsyncMock(
            side_effect=[
                _site([_device()], gateways=["GWY-01"], resource_set_id="rs1"),
                _site([], gateways=["GWY-01"], resource_set_id="rs1"),  # gateway still present
            ]
        ),
    )
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=True))

    await async_remove_device(hass, entry, device_id="d1")

    assert entry.options["transport"] == "gateway"


async def test_remove_device_finds_gateway_by_id_with_no_display_name(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_device.async_get_site", AsyncMock(return_value=_site(gateways=["GWY-01"])))
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="GWY-01"):
        await async_remove_device(_hass(), _entry(), device_id="GWY-01")


async def test_remove_device_runs_a_follow_up_reload_for_a_concurrent_change(monkeypatch):
    from plejd_cloud import schedule_ws

    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_device.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_device.async_get_site", AsyncMock(side_effect=[_site([_device()]), _site([])])
    )
    monkeypatch.setattr("plejd_cloud.manage_device.async_cloud_remove_device", AsyncMock(return_value=True))

    calls: list[str] = []

    async def _reload_sets_pending(entry_id):
        calls.append(entry_id)
        if len(calls) == 1:  # only the first reload race-loses to the concurrent change
            hass.data[schedule_ws.DATA_RELOAD_PENDING] = entry_id
        return True

    hass.config_entries.async_reload = AsyncMock(side_effect=_reload_sets_pending)

    await async_remove_device(hass, entry, device_id="d1")

    assert hass.config_entries.async_reload.await_count == 2  # ours, then the follow-up
    assert schedule_ws.DATA_RELOAD_PENDING not in hass.data
