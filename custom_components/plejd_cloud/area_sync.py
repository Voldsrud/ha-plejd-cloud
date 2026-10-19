"""Keep each Plejd device's HA area in step with its room in the Plejd app."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .const import CONF_DEVICES, CONF_ROOM_NAMES, CONF_ROOMS, DOMAIN, ROOM_DEVICE_ID_PREFIX

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.area_sync"
DATA_LOCKS = f"{DOMAIN}_area_sync_locks"
DATA_GENERATIONS = f"{DOMAIN}_area_sync_generations"


def _lock(hass: HomeAssistant, entry: ConfigEntry) -> asyncio.Lock:
    # One per entry, shared by sync and reset, so a reset never interleaves with an in-flight sync.
    return hass.data.setdefault(DATA_LOCKS, {}).setdefault(entry.entry_id, asyncio.Lock())


def _generation(hass: HomeAssistant, entry: ConfigEntry) -> int:
    return hass.data.get(DATA_GENERATIONS, {}).get(entry.entry_id, 0)


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(hass, STORE_VERSION, f"{STORE_KEY}.{entry.entry_id}")


def _get_device(devices: Any, plejd_id: str, entry_id: str) -> Any | None:
    # NOTE: async_get_device is deprecated from HA 2026.10 (breaks in 2027.8); its replacement doesn't exist before.
    if hasattr(devices, "async_get_device_by_identifier"):
        return devices.async_get_device_by_identifier((DOMAIN, plejd_id), entry_id)
    return devices.async_get_device(identifiers={(DOMAIN, plejd_id)})


def _fold(name: str) -> str:
    return name.strip().casefold()


def match_area(room_name: str, areas: list[Any]) -> Any | None:
    """Return the area named like the room or one " / " part of it, then the same by alias."""
    candidates = [_fold(c) for c in (room_name, *room_name.split(" / "))]
    names = {_fold(area.name): area for area in reversed(areas)}
    aliases = {_fold(alias): area for area in reversed(areas) for alias in getattr(area, "aliases", None) or ()}
    for lookup in (names, aliases):  # every canonical candidate before any alias
        for candidate in candidates:
            if candidate in lookup:
                return lookup[candidate]
    return None


def get_device_rooms(entry_data: dict[str, Any]) -> dict[str, str]:
    """Map each Plejd HA-device identifier to its room_id; a multi-output device follows its first output."""
    rooms: dict[str, str] = {}
    seen: set[str] = set()
    for device in entry_data.get(CONF_DEVICES, []):
        if device["device_id"] in seen:
            continue
        seen.add(device["device_id"])
        if device.get("room_id"):
            rooms[device["device_id"]] = device["room_id"]
    for room in entry_data.get(CONF_ROOMS, []):
        rooms[f"{ROOM_DEVICE_ID_PREFIX}{room['room_id']}"] = room["room_id"]
    return rooms


async def async_sync_areas(
    hass: HomeAssistant, entry: ConfigEntry, *, generation: int | None = None, removed_area: str | None = None
) -> None:
    """Move devices whose Plejd room changed since the last sync to the matching HA area."""
    # NOTE: record a device only once its room resolved to an area, so later room/area changes re-evaluate it;
    # remembering the area we assigned lets a changed match retarget it without overriding a hand-picked one.
    async with _lock(hass, entry):
        if generation is not None and generation != _generation(hass, entry):
            return  # a reset ran since this sync was requested; saving now would undo it
        room_names = {
            **{room["room_id"]: room["name"] for room in entry.data.get(CONF_ROOMS, [])},
            **(entry.data.get(CONF_ROOM_NAMES) or {}),
        }
        store = _store(hass, entry)
        synced: dict[str, dict[str, str]] = await store.async_load() or {}
        devices = dr.async_get(hass)
        areas = list(ar.async_get(hass).async_list_areas())

        resolved: dict[str, dict[str, str]] = {}
        for plejd_id, room_id in get_device_rooms(entry.data).items():
            room_key = f"{room_id}:{room_names[room_id]}" if room_id in room_names else None
            device = _get_device(devices, plejd_id, entry.entry_id)
            area = match_area(room_names[room_id], areas) if room_key else None
            previous = synced.get(plejd_id) or {}
            if device is None or area is None:
                if device is not None and previous.get("room", "").split(":", 1)[0] == room_id:
                    resolved[plejd_id] = previous  # same room, no area right now: keep its history
                    deleted = removed_area and previous.get("area") == removed_area and not previous.get("manual")
                    if deleted and device.area_id is None:
                        # HA cleared the area because it was deleted, not the user: restore it once it's back.
                        resolved[plejd_id] = {**previous, "area": None}
                continue
            # Only a move to another Plejd room overrides a hand-picked area; a rename acts like a match change.
            room_changed = previous.get("room", "").split(":", 1)[0] != room_id
            auto_placed = device.area_id == previous.get("area")
            if room_changed or auto_placed:
                if device.area_id != area.id:
                    _LOGGER.info("Plejd: moving %s to area %s (Plejd room changed)", device.name, area.name)
                    devices.async_update_device(device.id, area_id=area.id)
                resolved[plejd_id] = {"room": room_key, "area": area.id}
            else:
                resolved[plejd_id] = {**previous, "manual": True}  # hand-picked (or cleared): keep our marker
        if resolved != synced:
            await store.async_save(resolved)


async def async_reset_areas(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget what was synced; run while the option is off and on entry removal."""
    async with _lock(hass, entry):
        generations = hass.data.setdefault(DATA_GENERATIONS, {})
        generations[entry.entry_id] = generations.get(entry.entry_id, 0) + 1
        await _store(hass, entry).async_remove()


def async_listen_area_changes(hass: HomeAssistant, entry: ConfigEntry) -> Callable[[], None]:
    """Re-run the sync when an area is added or renamed, so a newly matching room takes effect."""

    generation = _generation(hass, entry)

    async def _on_area_change(event: Event) -> None:
        try:
            removed = event.data.get("area_id") if event.data.get("action") == "remove" else None
            await async_sync_areas(hass, entry, generation=generation, removed_area=removed)
        except Exception:  # noqa: BLE001 - optional; never let a registry event handler raise
            _LOGGER.warning("Plejd: could not sync device areas after an area change", exc_info=True)

    return hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, _on_area_change)
