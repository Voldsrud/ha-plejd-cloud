"""Standalone Plejd device commissioning — runs without HA.

Commissions an unprovisioned Plejd device into the mesh directly from this
machine's own Bluetooth adapter: cloud registration (createPlejdDevice_V2),
then the full BLE pairing sequence (DH key exchange, access address, node
index) — the same steps custom_components/plejd_cloud/add_device.py's
async_add_device does for the Home Assistant "Add a device" wizard.

Useful when Home Assistant itself has no local Bluetooth adapter or ESPHome
Bluetooth proxy yet, so the wizard can't see anything to commission.

Usage:
    uv run python tools/commission_device.py <address> <name> [--room TITLE] [--site-id ID]

Reads PLEJD_USER / PLEJD_PASS from tools/.env (see tools/.env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
import types
from pathlib import Path

from aiohttp import ClientSession
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# Load the integration's transport-independent modules without executing the
# package __init__ (which imports Home Assistant). Same trick as ble_validate.py:
# register a bare `plejd` package pointing at the source dir, so relative
# imports inside commission.py/cloud.py resolve against it.
_PLEJD_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "plejd"
_pkg = types.ModuleType("plejd")
_pkg.__path__ = [str(_PLEJD_DIR)]
sys.modules["plejd"] = _pkg

cloud = importlib.import_module("plejd.cloud")
commission = importlib.import_module("plejd.commission")

PLEJD_SERVICE_UUID = "31ba0001-6085-4726-be45-040c957391b5"
PLEJD_BLE_COMPANY_ID = 887  # Plejd.Shared PlejdManufacturerId

_MFR_LOGIN_OFFSET = 0
_MFR_HW_OFFSET = 3
_MFR_BUILD_TIME_OFFSET = 10
_MFR_BUILD_TIME_LEN = 6
_FLAG_HAS_ACCESS_ADDRESS = 0x01
_FLAG_HAS_NODE_INDEX = 0x02
_FLAG_HAS_CRYPTO_KEY = 0x04
_FLAG_ON_DEFAULT_MESH = 0x08

_SCAN_TIMEOUT = 15.0


def _parse_mfr_data(manufacturer_data: dict[int, bytes]) -> dict | None:
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


def _load_env(path: Path) -> tuple[str, str]:
    user = pwd = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "PLEJD_USER":
            user = value.strip()
        elif key.strip() == "PLEJD_PASS":
            pwd = value.strip()
    if not user or not pwd:
        raise SystemExit(f"PLEJD_USER / PLEJD_PASS missing from {path}")
    return user, pwd


async def _find_device(address: str) -> tuple[BLEDevice, AdvertisementData] | None:
    print(f"Scanning up to {_SCAN_TIMEOUT:.0f}s for {address}...")
    found: dict[str, tuple[BLEDevice, AdvertisementData]] = {}

    def _callback(device: BLEDevice, adv: AdvertisementData) -> None:
        if device.address.upper() == address.upper():
            found[device.address] = (device, adv)

    async with BleakScanner(detection_callback=_callback):
        for _ in range(int(_SCAN_TIMEOUT)):
            if found:
                break
            await asyncio.sleep(1)
    return next(iter(found.values()), None)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("address", help="BLE MAC address of the unprovisioned device")
    parser.add_argument("name", help="Name for the new device")
    parser.add_argument("--room", default=None, help="Room title to create and place the device in")
    parser.add_argument("--site-id", default=None, help="Plejd site ID (auto-picked if you only have one)")
    parser.add_argument("--env", default=str(Path(__file__).parent / ".env"))
    args = parser.parse_args()

    email, password = _load_env(Path(args.env))

    found = await _find_device(args.address)
    if found is None:
        raise SystemExit(f"Device {args.address} not found nearby (is it powered and in range?)")
    ble_device, adv = found
    if PLEJD_SERVICE_UUID not in (adv.service_uuids or []):
        raise SystemExit(f"{args.address} doesn't advertise the Plejd service - not a Plejd device?")
    parsed = _parse_mfr_data(adv.manufacturer_data or {})
    if parsed is None:
        raise SystemExit(f"Could not decode manufacturer data from {args.address}")
    if not parsed["is_unprovisioned"]:
        raise SystemExit(f"{args.address} is already provisioned - nothing to commission")
    hardware_id = str(parsed["hardware_id"])
    firmware_build_time = parsed["firmware_build_time"]
    print(f"Found unprovisioned device: hardware_id={hardware_id}, firmware_build_time={firmware_build_time}")

    async with ClientSession() as session:
        try:
            token = await cloud.async_login(session, email, password)
            if args.site_id:
                site = await cloud.async_get_site(session, token, args.site_id)
            else:
                sites = await cloud.async_get_sites(session, token)
                if len(sites) != 1:
                    ids = [(s.get("site") or s)["siteId"] for s in sites]
                    raise SystemExit(f"Pass --site-id, found {len(sites)} sites: {ids}")
                site = await cloud.async_get_site(session, token, (sites[0].get("site") or sites[0])["siteId"])
        except cloud.PlejdCloudError as err:
            raise SystemExit(f"Plejd cloud error: {err}") from err

        print("Commissioning...")
        addresses = await commission.async_commission_device(
            session, token, site, ble_device, args.name, hardware_id, firmware_build_time, room_title=args.room
        )
        print(f"Done! Node index {addresses.device_address}, outputs {addresses.output_addresses}")
        print("Reload the Plejd config entry in Home Assistant to pick up the new device.")


asyncio.run(main())
