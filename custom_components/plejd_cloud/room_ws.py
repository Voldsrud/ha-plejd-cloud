"""WebSocket API for the dashboard's room cards: Plejd rooms and per-light display styles.

`plejd/rooms` maps each Plejd room to its member light entities and, where the room has a safe
light-only group, its room-group light entity (one mesh command for the whole room), so the panel
groups lights the way the app does. Light styles are purely cosmetic (which lamp the panel draws),
live in their own Store and are keyed by unique_id.
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .cloud import PlejdAuthError
from .const import CATEGORY_LIGHT, CONF_ROOM_NAMES, DOMAIN, ROOM_DEVICE_ID_PREFIX
from .schedule_ws import DATA_ENTRY

_LOGGER = logging.getLogger(__name__)

# Mirrors the lamp models in frontend/src/lamps.ts.
LIGHT_STYLES = (
    "bulb", "pendant", "spot", "ceiling", "strip", "table", "floor", "wall",
    "downlight", "chandelier", "lantern", "ground", "post",
)  # fmt: skip
ROOM_SIZES = (1, 2, 3)  # grid columns a room card spans

EVENT_ROOMS_CHANGED = f"{DOMAIN}_rooms_changed"

_STORE_KEY = f"{DOMAIN}.light_styles"
_LAYOUT_STORE_KEY = f"{DOMAIN}.room_layout"
_DATA_STYLES_LOCK = f"{DOMAIN}_light_styles_lock"


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/rooms"})
@websocket_api.async_response
async def ws_rooms(hass: HomeAssistant, connection, msg) -> None:
    entry = hass.data.get(DATA_ENTRY)
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    by_unique_id = _entity_ids(hass, entry)
    # Every room, not just coordinator.rooms: that list leaves out rooms whose group would also
    # switch a non-light output, and their lights still belong in their own room card. Such rooms
    # get no entity_id, so the panel switches their lights individually instead of by group.
    # Stored names come from the latest full-site fetch; the cached group rooms' names are a fallback.
    names: dict[str, str] = {
        **{r.room_id: r.name for r in coordinator.rooms},
        **(entry.data.get(CONF_ROOM_NAMES) or {}),
    }
    rooms = {
        room_id: {
            "room_id": room_id,
            "name": name,
            "entity_id": by_unique_id.get(f"{ROOM_DEVICE_ID_PREFIX}{room_id}"),
            "lights": [],
        }
        for room_id, name in names.items()
    }
    for d in coordinator.devices:
        uid = _unique_id(d)
        if d.category == CATEGORY_LIGHT and d.room_id in rooms and uid in by_unique_id:
            rooms[d.room_id]["lights"].append(by_unique_id[uid])
    connection.send_result(msg["id"], {"rooms": list(rooms.values())})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/light_styles/get"})
@websocket_api.async_response
async def ws_styles_get(hass: HomeAssistant, connection, msg) -> None:
    stored = await Store(hass, 1, _STORE_KEY).async_load() or {}
    connection.send_result(msg["id"], {"styles": _by_entity_id(hass, stored), "available": list(LIGHT_STYLES)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "plejd/light_styles/set",
        vol.Required("entity_id"): str,
        # None clears the override, back to the default lamp
        vol.Required("style"): vol.Any(None, vol.In(LIGHT_STYLES)),
    }
)
@websocket_api.async_response
async def ws_styles_set(hass: HomeAssistant, connection, msg) -> None:
    # Stored by the entity's unique_id, so renaming the entity_id keeps its lamp type.
    reg_entry = er.async_get(hass).async_get(msg["entity_id"])
    if reg_entry is None:
        connection.send_error(msg["id"], "not_found", "Unknown light")
        return
    store = Store(hass, 1, _STORE_KEY)
    # Load-modify-save under a lock, so two quick edits can't drop each other's change.
    async with hass.data.setdefault(_DATA_STYLES_LOCK, asyncio.Lock()):
        stored = dict(await store.async_load() or {})
        if msg["style"] is None:
            stored.pop(reg_entry.unique_id, None)
        else:
            stored[reg_entry.unique_id] = msg["style"]
        await store.async_save(stored)
    connection.send_result(msg["id"], {"styles": _by_entity_id(hass, stored)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "plejd/lights/rename", vol.Required("entity_id"): str, vol.Required("name"): str}
)
@websocket_api.async_response
async def ws_rename_light(hass: HomeAssistant, connection, msg) -> None:
    """Rename one Plejd light: its own output in the Plejd cloud first (awaited), then in HA."""
    entry = hass.data.get(DATA_ENTRY)
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    name = msg["name"].strip()
    if not name:
        connection.send_error(msg["id"], "name_required", "Name is required")
        return
    reg_entry = er.async_get(hass).async_get(msg["entity_id"])
    output = next(
        (
            d
            for d in coordinator.devices
            if reg_entry is not None and _unique_id(d) == reg_entry.unique_id and d.category == CATEGORY_LIGHT
        ),
        None,
    )
    if output is None:
        connection.send_error(msg["id"], "not_found", "Not a Plejd light")
        return
    try:
        await coordinator.async_rename_device(output.device_id, name, output.output_index)
    except PlejdAuthError:
        entry.async_start_reauth(hass)
        connection.send_error(msg["id"], "auth_failed", "Plejd rejected the account credentials")
        return
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "rename_failed", str(err))
        return
    except Exception:  # noqa: BLE001 - log the detail server-side, return a stable message
        _LOGGER.exception("Plejd: renaming %s failed", msg["entity_id"])
        connection.send_error(msg["id"], "rename_failed", "Could not rename the light in Plejd")
        return
    # Plejd now has the name. A device with this one output is that light, so name the device (what HA
    # shows everywhere); any other output (light, relay, cover…) shares the device, so name only this entity.
    outputs = [d for d in coordinator.devices if d.device_id == output.device_id]
    if len(outputs) == 1 and reg_entry.device_id:
        coordinator.skip_next_mirror(output.device_id, name)  # already renamed in Plejd above
        dr.async_get(hass).async_update_device(reg_entry.device_id, name_by_user=name)
        if reg_entry.name:
            # An entity-name override would keep showing instead of the new device name.
            er.async_get(hass).async_update_entity(msg["entity_id"], name=None)
    else:
        er.async_get(hass).async_update_entity(msg["entity_id"], name=name)
    connection.send_result(msg["id"], {"name": name})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/room_layout/get"})
@websocket_api.async_response
async def ws_layout_get(hass: HomeAssistant, connection, msg) -> None:
    layout = await Store(hass, 1, _LAYOUT_STORE_KEY).async_load() or {}
    connection.send_result(msg["id"], {"order": layout.get("order", []), "sizes": layout.get("sizes", {})})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "plejd/room_layout/set",
        # Room ids in display order; "" is the "Other lights" card.
        vol.Required("order"): [str],
        vol.Required("sizes"): {str: vol.In(ROOM_SIZES)},
    }
)
@websocket_api.async_response
async def ws_layout_set(hass: HomeAssistant, connection, msg) -> None:
    layout = {"order": msg["order"], "sizes": msg["sizes"]}
    await Store(hass, 1, _LAYOUT_STORE_KEY).async_save(layout)
    connection.send_result(msg["id"], layout)


def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_rooms)
    websocket_api.async_register_command(hass, ws_styles_get)
    websocket_api.async_register_command(hass, ws_styles_set)
    websocket_api.async_register_command(hass, ws_layout_get)
    websocket_api.async_register_command(hass, ws_layout_set)
    websocket_api.async_register_command(hass, ws_rename_light)


def _entity_ids(hass: HomeAssistant, entry) -> dict[str, str]:
    """This entry's light entities, unique_id -> current entity_id."""
    registry = er.async_get(hass)
    # Lights only: an output reconfigured light -> relay -> light leaves a switch.* entry with the
    # same unique_id behind, which must not shadow the light.
    return {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.entity_id.startswith("light.")
    }


def _by_entity_id(hass: HomeAssistant, stored: dict[str, str]) -> dict[str, str]:
    entry = hass.data.get(DATA_ENTRY)
    if entry is None:
        return {}
    ids = _entity_ids(hass, entry)
    return {ids[uid]: style for uid, style in stored.items() if uid in ids}


def _unique_id(device) -> str:
    """A Plejd output's light unique_id (mirrors PlejdLight in light.py)."""
    return device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
