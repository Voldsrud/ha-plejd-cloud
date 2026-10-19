"""Remove a Plejd device from the site.

The HA-facing orchestration around cloud.py's async_remove_device: mutate on
the cloud, then refresh and reload the config entry so the change is
reflected. Shared by the remove_device service.

Removing a device is decommissioning it - unlike removing a room or scene
(which still exist for other things to reference), a removed device's own
automations/dim-bindings naturally going stale afterward is expected, the
same as removing any other HA device, not a dependency to guard against.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import schedule_ws
from .cloud import PlejdAuthError, PlejdCloudError, async_get_site, async_login
from .cloud import async_remove_device as async_cloud_remove_device
from .const import (
    CONF_SITE_ID,
    CONF_TRANSPORT,
    TRANSPORT_AUTO,
)


async def _async_login_and_get_site(hass: HomeAssistant, entry: ConfigEntry):
    http_session = async_get_clientsession(hass)
    try:
        token = await async_login(http_session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    except PlejdAuthError as err:
        # Raising ConfigEntryAuthFailed only triggers reauth from async_setup_entry or a
        # coordinator's own update method - a service handler must start it explicitly.
        entry.async_start_reauth(hass)
        raise HomeAssistantError("Plejd cloud credentials rejected; reauthentication started") from err
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error: {err}") from err
    try:
        site = await async_get_site(http_session, token, entry.data[CONF_SITE_ID])
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error: {err}") from err
    return http_session, token, site


async def _async_refresh_and_reload(hass: HomeAssistant, entry: ConfigEntry, http_session, token) -> None:
    try:
        fresh_site = await async_get_site(http_session, token, entry.data[CONF_SITE_ID])
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error refreshing site: {err}") from err
    # Mirrors the config-flow/schedule_ws guard: drop a forced gateway-only
    # preference once there's no usable gateway left, or removing the gateway
    # device would leave the coordinator stuck raising ConfigEntryNotReady forever
    # on reload instead of falling back to BLE.
    has_gateway = bool(fresh_site.gateways and fresh_site.resource_set_id)
    new_transport = entry.options.get(CONF_TRANSPORT, TRANSPORT_AUTO) if has_gateway else TRANSPORT_AUTO
    await schedule_ws.async_reload_entry_with_lock(
        hass,
        entry,
        lambda e: schedule_ws.site_data_overlay(e, fresh_site),
        build_options=lambda e: {**e.options, CONF_TRANSPORT: new_transport},
        # The device's cloud mutation has already happened and isn't safely retryable by
        # the time this runs - raising on a reload failure here would make the whole
        # service call look failed, and retrying just gets "not found". Only the reload
        # itself needs a manual retry.
        raise_on_reload_failure=False,
        error_context="removing a device",
    )


def _device_name(site, device_id: str) -> str | None:
    """Display name for any physical device (output, input, motion sensor, or gateway)."""
    for candidates in (site.devices, site.inputs, site.motion):
        found = next((d for d in candidates if d.device_id == device_id), None)
        if found is not None:
            return found.name
    return None


async def async_remove_device(hass: HomeAssistant, entry: ConfigEntry, *, device_id: str) -> None:
    """Remove a device from the site, then refresh + reload the config entry."""
    http_session, token, site = await _async_login_and_get_site(hass, entry)
    # Checked directly against devices/inputs/motion/gateways/firmware_by_device, not
    # device_addresses - a device with a missing or malformed cloud address entry still
    # belongs here (that's exactly the kind of stale device this service exists to
    # decommission), and site.devices alone only holds controllable outputs, not
    # inputs/motion/gateways (see #114's identical all_rooms/rooms distinction).
    # firmware_by_device is unconditionally built from every plejdDevices/gateways entry
    # (cloud.py), so it still covers a physical device (e.g. a motion sensor) that's
    # missing from inputs/motion entirely because ITS address lookup failed too.
    name = _device_name(site, device_id)
    if name is None and device_id not in site.gateways and device_id not in site.firmware_by_device:
        raise HomeAssistantError(f"Plejd device {device_id} not found on this site")
    name = name or device_id
    try:
        ok = await async_cloud_remove_device(http_session, token, site.site_id, device_id)
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error removing device: {err}") from err
    if not ok:
        raise HomeAssistantError(f"Plejd cloud rejected removing device '{name}'")
    await _async_refresh_and_reload(hass, entry, http_session, token)
