"""Discover unprovisioned Plejd devices from BLE advertisements.

Shared by the scan_new_devices/add_device services and the dashboard's
"Add a device" wizard, so there's one place that knows how to decode a Plejd
advertisement's provisioning state.
"""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import HARDWARE_TYPES, PLEJD_BLE_COMPANY_ID, PLEJD_SERVICE_UUID

# Offsets in bleak's manufacturer_data value (i.e. raw byte offset minus 2 for company ID).
_MFR_LOGIN_OFFSET = 0  # provisioning status flags (raw byte 2)
_MFR_HW_OFFSET = 3  # hardware type ID (raw byte 5)
_MFR_BUILD_TIME_OFFSET = 10  # firmware build timestamp: 6 bytes big-endian (YYYYMMDDHHMMSS decimal)
_MFR_BUILD_TIME_LEN = 6
# LoginByte flags (from Plejd.Shared BleAdvertisementData constants).
_FLAG_HAS_ACCESS_ADDRESS = 0x01
_FLAG_HAS_NODE_INDEX = 0x02
_FLAG_HAS_CRYPTO_KEY = 0x04
_FLAG_ON_DEFAULT_MESH = 0x08


def _parse_plejd_mfr_data(manufacturer_data: dict[int, bytes]) -> dict | None:
    """Extract Plejd provisioning state from BLE manufacturer data.

    Returns None if no usable data is found. The 'is_unprovisioned' flag is True
    when the device is on the default mesh (bit 3) or has no provisioning at all.
    """
    data = manufacturer_data.get(PLEJD_BLE_COMPANY_ID)
    if data is None or len(data) < _MFR_HW_OFFSET + 1:
        return None
    login = data[_MFR_LOGIN_OFFSET]
    hardware_id = data[_MFR_HW_OFFSET]
    on_default_mesh = bool(login & _FLAG_ON_DEFAULT_MESH)
    unclaimed = not (login & (_FLAG_HAS_ACCESS_ADDRESS | _FLAG_HAS_NODE_INDEX | _FLAG_HAS_CRYPTO_KEY))
    end = _MFR_BUILD_TIME_OFFSET + _MFR_BUILD_TIME_LEN
    firmware_build_time = int.from_bytes(data[_MFR_BUILD_TIME_OFFSET:end], "big") if len(data) >= end else 0
    return {
        "hardware_id": hardware_id,
        "is_unprovisioned": on_default_mesh or unclaimed,
        "firmware_build_time": firmware_build_time,
    }


def async_bluetooth_available(hass: HomeAssistant) -> bool:
    """Whether HA has at least one connectable Bluetooth scanner right now.

    Commissioning needs a real BLE GATT connection to the new device, so a local
    adapter or an active-mode ESPHome Bluetooth proxy is required - passive-only
    coverage (or none at all) can't do it.
    """
    return bluetooth.async_scanner_count(hass, connectable=True) > 0


def async_scan_unprovisioned(hass: HomeAssistant) -> list[dict]:
    """Return every unprovisioned Plejd device currently visible over Bluetooth.

    Reads HA's Bluetooth discovery cache (already continuously updated in the
    background), so this returns immediately rather than running a fresh scan.
    """
    new_devices = []
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        if PLEJD_SERVICE_UUID not in (info.service_uuids or []):
            continue
        parsed = _parse_plejd_mfr_data(info.manufacturer_data or {})
        if not parsed or not parsed["is_unprovisioned"]:
            continue
        hw_id = parsed["hardware_id"]
        model = HARDWARE_TYPES.get(hw_id, "Unknown")
        new_devices.append(
            {
                "address": info.address,
                "name": info.name or model,
                "rssi": info.rssi,
                "hardware_id": str(hw_id),
                "model": model,
                "firmware_build_time": parsed["firmware_build_time"],
            }
        )
    return new_devices
