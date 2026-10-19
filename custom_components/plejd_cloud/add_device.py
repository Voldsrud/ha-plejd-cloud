"""Add a new Plejd device to a Home Assistant config entry.

The HA-facing orchestration around commission.py's transport-independent BLE
commissioning: resolves the device via HA's Bluetooth integration, registers +
commissions it, then refreshes and reloads the config entry. Shared by the
add_device service and the "Add a device" dashboard wizard (which calls that service).
"""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import schedule_ws
from .cloud import (
    PlejdCloudError,
    async_get_site,
    async_login,
    async_set_input_setting,
)
from .commission import async_commission_device
from .const import (
    CONF_SITE_ID,
)
from .discovery import _parse_plejd_mfr_data, async_bluetooth_available

_LOGGER = logging.getLogger(__name__)


async def async_add_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    address: str,
    name: str,
    hardware_id: str = "0",
    room_id: str | None = None,
    room_title: str | None = None,
    room_category: str | None = None,
    firmware_build_time: int = 0,
    input_settings: list[dict] | None = None,
) -> None:
    """Add a new Plejd device end-to-end: cloud registration, BLE commissioning,
    input-button config, then refresh + reload the config entry.
    """
    for cfg in input_settings or []:
        if "input" not in cfg or "button_type" not in cfg:
            raise HomeAssistantError(f"Invalid input_settings entry (needs 'input' and 'button_type'): {cfg}")

    if not async_bluetooth_available(hass):
        raise HomeAssistantError(
            "Bluetooth is not available on this Home Assistant instance. Add a local "
            "Bluetooth adapter or an ESPHome Bluetooth proxy (in active mode), then try again."
        )

    # Validate the device is actually in range before touching the cloud, so a
    # typo'd address fails fast instead of creating an orphaned cloud room first.
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise HomeAssistantError(f"Plejd device {address} not found in Bluetooth range")

    # Confirm the address is actually advertising as unprovisioned before any cloud
    # mutation - the options-flow wizard only ever offers unprovisioned addresses,
    # but this service can be called directly (Developer Tools, automations) with
    # any address, including an already-commissioned device or an unrelated one.
    service_infos = bluetooth.async_discovered_service_info(hass, connectable=True)
    adv = next((si for si in service_infos if si.address == address), None)
    parsed_adv = _parse_plejd_mfr_data(adv.manufacturer_data or {}) if adv else None
    if parsed_adv is None or not parsed_adv["is_unprovisioned"]:
        raise HomeAssistantError(
            f"Plejd device {address} is not currently advertising as unprovisioned "
            "(it may already be commissioned, or isn't a Plejd device)"
        )
    if hardware_id == "0":
        hardware_id = str(parsed_adv["hardware_id"])
    if firmware_build_time == 0:
        firmware_build_time = parsed_adv["firmware_build_time"]

    http_session = async_get_clientsession(hass)
    try:
        token = await async_login(http_session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
        site = await async_get_site(http_session, token, entry.data[CONF_SITE_ID])
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error during device add: {err}") from err

    device_id = address.replace(":", "").lower()
    try:
        await async_commission_device(
            http_session,
            token,
            site,
            ble_device,
            name,
            hardware_id,
            firmware_build_time,
            room_id,
            room_title,
            room_category,
        )
    except Exception as err:
        raise HomeAssistantError(f"Plejd commissioning failed: {err}") from err

    # The device has joined the mesh at this point - input-setting failures are
    # reported but must not skip the refresh/reload below, or HA never learns
    # about a device that's already been commissioned.
    input_setting_errors: list[str] = []
    for cfg in input_settings or []:
        try:
            await async_set_input_setting(
                http_session, token, site.site_id, device_id, cfg["input"], cfg["button_type"]
            )
        except Exception as err:  # noqa: BLE001 - collected below, not fatal to the add
            _LOGGER.warning("Plejd add_device: failed to set input %s button type", cfg["input"], exc_info=True)
            input_setting_errors.append(str(err))

    # Refresh device list from cloud so the new device is present on reload.
    try:
        fresh_site = await async_get_site(http_session, token, entry.data[CONF_SITE_ID])
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error refreshing device list: {err}") from err
    await schedule_ws.async_reload_entry_with_lock(
        hass,
        entry,
        lambda e: schedule_ws.site_data_overlay(e, fresh_site),
        # Commissioning already happened and is non-idempotent by this point (the device is
        # no longer advertising as unprovisioned) - raising on a reload failure here would
        # report a successfully-added device as a failed add, with no way to retry through
        # this same path. Only the reload itself needs a manual retry.
        raise_on_reload_failure=False,
        error_context="adding a device",
    )

    if input_setting_errors:
        raise HomeAssistantError(f"Device added, but some input settings failed: {'; '.join(input_setting_errors)}")
