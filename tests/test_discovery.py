"""Tests for discovering unprovisioned Plejd devices over BLE."""

from __future__ import annotations

import types

from plejd_cloud.const import PLEJD_SERVICE_UUID
from plejd_cloud.discovery import _parse_plejd_mfr_data, async_scan_unprovisioned


def _fake_service_info(address, mfr_data, service_uuids=None, rssi=-70, name=None):
    return types.SimpleNamespace(
        address=address,
        name=name,
        rssi=rssi,
        service_uuids=service_uuids or [PLEJD_SERVICE_UUID],
        manufacturer_data=mfr_data,
    )


def _hass(service_infos=()):
    return types.SimpleNamespace(service_infos=list(service_infos))


# ── _parse_plejd_mfr_data ──────────────────────────────────────────────────────


def test_parse_mfr_data_on_default_mesh():
    result = _parse_plejd_mfr_data({887: bytes([0x08, 0, 0, 2])})
    assert result == {"hardware_id": 2, "is_unprovisioned": True, "firmware_build_time": 0}


def test_parse_mfr_data_provisioned():
    result = _parse_plejd_mfr_data({887: bytes([0x07, 0, 0, 1])})
    assert result == {"hardware_id": 1, "is_unprovisioned": False, "firmware_build_time": 0}


def test_parse_mfr_data_includes_build_time():
    # 17 bytes: LoginByte + pad[2] + hw_id + mac[6] + buildTime[6 BE] + extra
    bt = (20240701133622).to_bytes(6, "big")
    data = bytes([0x08, 0, 0, 22]) + bytes(6) + bt + bytes([0x07])
    result = _parse_plejd_mfr_data({887: data})
    assert result is not None
    assert result["hardware_id"] == 22
    assert result["firmware_build_time"] == 20240701133622


def test_parse_mfr_data_empty_dict_returns_none():
    assert _parse_plejd_mfr_data({}) is None


def test_parse_mfr_data_too_short_returns_none():
    assert _parse_plejd_mfr_data({887: bytes([0x08, 0, 0])}) is None


def test_parse_mfr_data_ignores_other_company_ids():
    # Some other vendor's manufacturer data, not Plejd's 887 - must not be parsed.
    assert _parse_plejd_mfr_data({0x004C: bytes([0x08, 0, 0, 1, 0, 0, 0, 0])}) is None


# ── async_scan_unprovisioned ───────────────────────────────────────────────────


def test_scan_finds_unprovisioned_on_default_mesh():
    # LoginByte 0x08 = IsOnDefaultMesh, hardware_id byte at offset 3 = 1 (DIM-01)
    hass = _hass([_fake_service_info("AA:BB:CC:DD:EE:FF", {887: bytes([0x08, 0, 0, 1])})])
    devs = async_scan_unprovisioned(hass)
    assert len(devs) == 1
    assert devs[0]["address"] == "AA:BB:CC:DD:EE:FF"
    assert devs[0]["hardware_id"] == "1"
    assert devs[0]["model"] == "DIM-01"


def test_scan_finds_unclaimed_device():
    # LoginByte 0x00 = no flags set = unclaimed; hardware_id at offset 3 = 3 (CTR-01)
    hass = _hass([_fake_service_info("11:22:33:44:55:66", {887: bytes([0x00, 0, 0, 3])})])
    devs = async_scan_unprovisioned(hass)
    assert devs[0]["model"] == "CTR-01"


def test_scan_excludes_provisioned_device():
    # LoginByte 0x07 = all three provisioning bits set, not on default mesh -> provisioned
    hass = _hass([_fake_service_info("AA:BB:CC:DD:EE:FF", {887: bytes([0x07, 0, 0, 1])})])
    assert async_scan_unprovisioned(hass) == []


def test_scan_excludes_non_plejd_devices():
    # Different service UUID - not a Plejd device
    hass = _hass(
        [
            _fake_service_info(
                "AA:BB:CC:DD:EE:FF",
                {887: bytes([0x08, 0, 0, 1])},
                service_uuids=["0000180a-0000-1000-8000-00805f9b34fb"],
            )
        ]
    )
    assert async_scan_unprovisioned(hass) == []


def test_scan_returns_empty_list_when_no_devices():
    assert async_scan_unprovisioned(_hass([])) == []
