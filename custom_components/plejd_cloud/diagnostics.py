"""Diagnostics for the Plejd integration.

Downloadable config-entry diagnostics for troubleshooting. All secrets and PII —
credentials, the site crypto key, session/resource/installation ids, BLE addresses,
and device/room names — are redacted; only non-identifying structure (transport,
counts, models) is exposed.
"""

from __future__ import annotations

import re
from typing import Any

import aiohttp
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloud import PlejdCloudError, async_get_site_raw, async_login
from .const import (
    CONF_CLOUD_SCHEDULES,
    CONF_CRYPTO_KEY,
    CONF_DEVICE_ADDRESSES,
    CONF_DISCOVERED_ADDRESS,
    CONF_GATEWAYS,
    CONF_INSTALLATION_ID,
    CONF_PENDING_ROOM_MOVES,
    CONF_RESOURCE_SET_ID,
    CONF_SCHEDULES,
    CONF_SITE_ID,
)
from .coordinator import PlejdCoordinator

# Distinct shapes kept per list: the same key holding a different structure (e.g. a schedule's
# astro vs fixed-time trigger) is exactly what the dump is for, so don't keep only the first.
_SHAPE_VARIANTS = 4

# Keys to redact wherever they appear (entry data is nested: devices/scenes/inputs/…).
TO_REDACT = {
    "email",
    "password",
    CONF_CRYPTO_KEY,
    CONF_SITE_ID,
    CONF_RESOURCE_SET_ID,
    CONF_INSTALLATION_ID,
    CONF_DISCOVERED_ADDRESS,
    CONF_GATEWAYS,
    CONF_DEVICE_ADDRESSES,
    "device_id",
    "deviceId",
    "object_id",
    "address",
    "outputs",
    "member_addresses",
    "dimmable_addresses",
    "name",
    "room_id",
    "scene_id",
    # Schedule timings reveal occupancy/routine — redact wholesale (count reported below).
    CONF_SCHEDULES,
    CONF_CLOUD_SCHEDULES,
    # move_device_to_room's own pending-moves cache is keyed BY device_id (a dict key,
    # which async_redact_data can't redact - only values under a matching key name) and
    # its values carry mesh addresses too - redact the whole structure wholesale rather
    # than trying to name every nested field individually.
    CONF_PENDING_ROOM_MOVES,
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator: PlejdCoordinator = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "active_transport": coordinator.active_transport or "disconnected",
        "available": coordinator.available,
        "counts": {
            "devices": len(coordinator.devices),
            "scenes": len(coordinator.scenes),
            "inputs": len(coordinator.inputs),
            "motion": len(coordinator.motion),
            "gateways": len(entry.data.get(CONF_GATEWAYS) or []),
            "schedules": len(entry.options.get(CONF_SCHEDULES) or []),
            "cloud_schedules": len(entry.data.get(CONF_CLOUD_SCHEDULES) or []),
        },
        "models": sorted({device.model for device in coordinator.devices}),
        "site_structure": await _site_structure(hass, entry),
    }


def site_shape(value: Any) -> Any:
    """Keys and value types only, never values: shows the cloud schema without any secret or PII."""
    if isinstance(value, dict):
        if any(_is_identifier(key) for key in value):
            # a map keyed by device/room/scene id (deviceAddress, outputAddress, ...): the keys are PII, keep shapes only
            variants = []
            for item in value.values():
                shape = site_shape(item)
                if shape not in variants and len(variants) < _SHAPE_VARIANTS:
                    variants.append(shape)
            return {"<map>": len(value), "values": variants}
        return {key: site_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        variants: list[Any] = []
        for item in value:
            shape = site_shape(item)
            if shape not in variants and len(variants) < _SHAPE_VARIANTS:
                variants.append(shape)
        return {"<list>": len(value), "items": variants}
    return type(value).__name__


def _is_identifier(key: object) -> bool:
    """Wire field names are plain camelCase; ids (MACs, GUIDs, Parse ids) carry digits or dashes."""
    return (
        not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_]+", key) or bool(re.fullmatch(r"[0-9A-Fa-f]{8,}", key))
    )


async def _site_structure(hass: HomeAssistant, entry: ConfigEntry) -> Any:
    session = async_get_clientsession(hass)
    try:
        token = await async_login(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
        return site_shape(await async_get_site_raw(session, token, entry.data[CONF_SITE_ID]))
    except (PlejdCloudError, aiohttp.ClientError, OSError, TimeoutError, ValueError) as err:
        return {"error": type(err).__name__}
