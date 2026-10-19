"""WebSocket API for admin-editable custom remote button-profile overrides.

Lets the dashboard save a per-device button layout that takes effect immediately —
no code change or release needed. Read by `dim_binding_ws.ws_device_triggers`, which
prefers a saved override over a built-in profile over the generic grouping.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .remote_profiles import InvalidRemoteProfile

_LOGGER = logging.getLogger(__name__)

DATA_REMOTE_PROFILES = f"{DOMAIN}_remote_profiles"


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/remote_profiles/list"})
@websocket_api.async_response
async def ws_list(hass: HomeAssistant, connection, msg) -> None:
    profiles = hass.data.get(DATA_REMOTE_PROFILES)
    if profiles is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    connection.send_result(msg["id"], {"profiles": profiles.profiles})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "plejd/remote_profiles/save", vol.Required("device_id"): str, vol.Required("profile"): dict}
)
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection, msg) -> None:
    profiles = hass.data.get(DATA_REMOTE_PROFILES)
    if profiles is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    try:
        await profiles.async_save(msg["device_id"], msg["profile"])
    except InvalidRemoteProfile as err:
        connection.send_error(msg["id"], "invalid_profile", str(err))
        return
    except Exception:  # noqa: BLE001 - log the detail server-side, return a stable generic message
        _LOGGER.exception("Plejd: failed to save remote profile")
        connection.send_error(msg["id"], "save_failed", "Could not save remote profile")
        return
    connection.send_result(msg["id"], {"profiles": profiles.profiles})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/remote_profiles/delete", vol.Required("device_id"): str})
@websocket_api.async_response
async def ws_delete(hass: HomeAssistant, connection, msg) -> None:
    profiles = hass.data.get(DATA_REMOTE_PROFILES)
    if profiles is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    try:
        await profiles.async_delete(msg["device_id"])
    except Exception:  # noqa: BLE001 - log the detail server-side, return a stable generic message
        _LOGGER.exception("Plejd: failed to delete remote profile")
        connection.send_error(msg["id"], "delete_failed", "Could not delete remote profile")
        return
    connection.send_result(msg["id"], {"profiles": profiles.profiles})


def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_save)
    websocket_api.async_register_command(hass, ws_delete)
