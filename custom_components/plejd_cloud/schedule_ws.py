"""WebSocket API for the dashboard's schedule editor and settings.

Admin-only commands to list/add/delete on-device weekly time-event schedules, read/write
the entry's other options (transport, holiday mode), and list unprovisioned devices for
the dashboard's "Add a device" wizard. Options persist on the config entry and reload it
so e.g. a schedule's `switch` entity (switch.py) is (re)created, which is what actually
programs/clears the on-device event.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict

import aiohttp
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .cloud import (
    PlejdAuthError,
    PlejdCloudError,
    PlejdCloudSite,
    async_get_site_raw,
    async_login,
    parse_astro_events,
    parse_site,
)
from .const import (
    CONF_CLOUD_SCHEDULES,
    CONF_DEVICE_ADDRESSES,
    CONF_DEVICES,
    CONF_GATEWAYS,
    CONF_HOLIDAY_LIGHTS,
    CONF_HOLIDAY_WINDOW_END,
    CONF_HOLIDAY_WINDOW_START,
    CONF_INPUTS,
    CONF_MOTION,
    CONF_RESOURCE_SET_ID,
    CONF_ROOM_NAMES,
    CONF_ROOMS,
    CONF_SCENES,
    CONF_SCHEDULES,
    CONF_SITE_ID,
    CONF_SYNC_AREAS,
    CONF_TRANSPORT,
    DOMAIN,
    HOLIDAY_WINDOW_END_DEFAULT,
    HOLIDAY_WINDOW_START_DEFAULT,
    ROOM_CATEGORIES,
    TIME_EVENT_SLOTS,
    TRANSPORT_AUTO,
    TRANSPORT_BLE,
    TRANSPORT_GATEWAY,
)
from .discovery import async_bluetooth_available, async_scan_unprovisioned

_LOGGER = logging.getLogger(__name__)

DATA_ENTRY = f"{DOMAIN}_schedule_entry"

# Per-entry lock held across every management operation's own update_entry+reload
# sequence (schedule WS saves, add_device, room/scene/schedule services, ...) so they
# serialize against EACH OTHER and against the entry's update listener
# (_async_reload_entry in __init__.py), instead of racing a shared flag that any one
# of them could clear out from under another still-in-flight caller.
_DATA_RELOAD_LOCKS = f"{DOMAIN}_reload_locks"

# Set by _async_reload_entry when it finds async_get_reload_lock() already held - some
# other operation owns an update+reload cycle for this entry right now and will pick up
# this change; the current lock holder checks this after releasing and runs a follow-up
# reload for it, instead of silently dropping it. Like _DATA_EXPECTING_SELF_RELOAD below,
# this is a single hass.data slot holding one entry_id, not a per-entry structure - correct
# only because manifest.json declares single_config_entry (exactly one entry_id ever
# exists). Would need to become per-entry if that ever changes.
DATA_RELOAD_PENDING = f"{DOMAIN}_schedule_reload_pending"

# Set by a reload-lock holder right before its own single async_update_entry() call, so
# the listener invocation that call triggers is recognized as self-triggered - already
# covered by this session's own upcoming reload - rather than mistaken for a genuinely
# concurrent change made by someone else while the lock was held (which would otherwise
# queue a redundant extra reload on every single successful call). Consumed by whichever
# listener invocation sees it first; cleared unconditionally once the session ends so it
# can never leak into a later, unrelated session if the listener never got a chance to run.
_DATA_EXPECTING_SELF_RELOAD = f"{DOMAIN}_reload_expecting_self_reload"

# Bumped every time the pending marker is set, so a holder can tell "the marker I saw
# before my reload" from "a NEW marker set while my reload was running". Without it, a
# reload that started before a change landed would wrongly consume that change's marker
# and its required follow-up reload would never happen.
_DATA_RELOAD_PENDING_SEQ = f"{DOMAIN}_reload_pending_seq"

_NEXT_ID_KEY = "next_schedule_id"


def async_mark_reload_pending(hass: HomeAssistant, entry_id: str) -> None:
    """Record that `entry_id` has a change still needing a reload."""
    hass.data[_DATA_RELOAD_PENDING_SEQ] = hass.data.get(_DATA_RELOAD_PENDING_SEQ, 0) + 1
    hass.data[DATA_RELOAD_PENDING] = entry_id


def async_reload_pending_token(hass: HomeAssistant, entry_id: str) -> int | None:
    """Token identifying the current pending marker, or None if nothing is pending."""
    if hass.data.get(DATA_RELOAD_PENDING) != entry_id:
        return None
    return hass.data.get(_DATA_RELOAD_PENDING_SEQ, 0)


def async_take_reload_pending(hass: HomeAssistant, entry_id: str) -> bool:
    """Claim any pending marker for `entry_id`, returning whether there was one.

    For a caller that is about to reload with the entry's CURRENT contents, so whatever the
    marker stands for is covered either way.
    """
    if hass.data.get(DATA_RELOAD_PENDING) != entry_id:
        return False  # nothing pending, or it belongs to a different entry
    hass.data.pop(DATA_RELOAD_PENDING, None)
    return True


def async_take_reload_pending_token(hass: HomeAssistant, entry_id: str, token: int | None) -> None:
    """Claim the marker `token` came from, and only that one.

    Deliberately a separate function rather than an optional argument: a caller whose reload
    has already read the entry must not swallow a marker created after that point (the change
    it stands for is not in the reload being performed), and `token=None` means there was
    nothing pending to claim - which as an optional "no filter" argument reads exactly like
    "claim anything" and would do the opposite of what is wanted.
    """
    if token is None or hass.data.get(DATA_RELOAD_PENDING) != entry_id:
        return
    if hass.data.get(_DATA_RELOAD_PENDING_SEQ, 0) != token:
        return  # a newer change landed while the reload ran; its follow-up is still needed
    hass.data.pop(DATA_RELOAD_PENDING, None)


def async_get_reload_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """Return the per-entry lock serializing this integration's own reload cycles."""
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(_DATA_RELOAD_LOCKS, {})
    return locks.setdefault(entry_id, asyncio.Lock())


def async_mark_expecting_self_reload(hass: HomeAssistant, entry_id: str) -> None:
    """Call immediately before a reload-lock holder's own async_update_entry(), see above."""
    hass.data[_DATA_EXPECTING_SELF_RELOAD] = entry_id


def async_consume_expected_self_reload(hass: HomeAssistant, entry_id: str) -> bool:
    """True (and clears the marker) if a self-triggered listener invocation was expected."""
    if hass.data.get(_DATA_EXPECTING_SELF_RELOAD) == entry_id:
        hass.data.pop(_DATA_EXPECTING_SELF_RELOAD, None)
        return True
    return False


def site_data_overlay(entry: ConfigEntry, site: PlejdCloudSite, overrides: dict | None = None) -> dict:
    """The standard site-derived entry.data overlay, merged onto `entry.data` as passed in.

    Every management operation refreshes the same set of cloud-derived keys after its own
    mutation, so they share this rather than repeating the list six times and drifting.
    Callers hand it to async_reload_entry_with_lock as `lambda e: site_data_overlay(e, site)`
    so `e.data` is read inside the lock - see that function's note on staleness.
    """
    return {
        **entry.data,
        CONF_DEVICES: [asdict(d) for d in site.devices],
        CONF_INPUTS: [asdict(i) for i in site.inputs],
        CONF_MOTION: [asdict(m) for m in site.motion],
        CONF_SCENES: [asdict(s) for s in site.scenes],
        CONF_ROOMS: [asdict(r) for r in site.rooms],
        CONF_ROOM_NAMES: {r.room_id: r.name for r in site.all_rooms},
        CONF_GATEWAYS: site.gateways,
        CONF_RESOURCE_SET_ID: site.resource_set_id,
        CONF_DEVICE_ADDRESSES: site.device_addresses,
        **(overrides or {}),
    }


async def async_reload_entry_with_lock(
    hass: HomeAssistant,
    entry: ConfigEntry,
    build_data: Callable[[ConfigEntry], dict],
    *,
    build_options: Callable[[ConfigEntry], dict] | None = None,
    raise_on_reload_failure: bool = True,
    error_context: str,
) -> None:
    """Build the entry payload under the shared per-entry reload lock, write it, and reload.

    `build_data` (and `build_options`) are invoked AFTER the lock is acquired and must read
    `entry.data`/`entry.options` themselves rather than closing over a snapshot: acquiring
    the lock is an await point, so another management operation can complete in between and
    persist its own change, and a payload computed before that would silently overwrite it
    (#125).

    Raises HomeAssistantError if the entry's own reload reports failure, UNLESS
    raise_on_reload_failure=False (only logs instead) - for a caller whose cloud mutation is
    already done and non-idempotent (e.g. a scene/schedule create), raising here would make
    the whole operation look failed, inviting a retry that duplicates the already-created
    cloud object; only the reload itself needs a retry. A follow-up reload for a genuinely
    concurrent change the update listener detected meanwhile (see DATA_RELOAD_PENDING) is
    always only logged on failure, since it isn't this caller's own operation to fail loudly
    for either way.
    """
    lock = async_get_reload_lock(hass, entry.entry_id)
    reloaded = True
    try:
        async with lock:
            async_mark_expecting_self_reload(hass, entry.entry_id)
            data = build_data(entry)
            if build_options is not None:
                hass.config_entries.async_update_entry(entry, data=data, options=build_options(entry))
            else:
                hass.config_entries.async_update_entry(entry, data=data)
            try:
                reloaded = await hass.config_entries.async_reload(entry.entry_id)
            except Exception:  # noqa: BLE001 - surfaced below as the HomeAssistantError callers expect
                _LOGGER.exception("Plejd: failed to reload after %s", error_context)
                reloaded = False
    finally:
        hass.data.pop(_DATA_EXPECTING_SELF_RELOAD, None)
        if async_take_reload_pending(hass, entry.entry_id):
            # A concurrent change's own reload was suppressed by the lock above while ours
            # was in flight; give it a reload of its own instead of dropping it silently.
            async with lock:
                follow_up_ok = False
                try:
                    follow_up_ok = await hass.config_entries.async_reload(entry.entry_id)
                except Exception:  # noqa: BLE001 - best-effort follow-up for someone else's change
                    _LOGGER.exception("Plejd: follow-up reload for a concurrent change raised")
                if not follow_up_ok:
                    # async_reload can report failure by returning False without raising, and
                    # dropping that would lose someone else's change entirely - leave it
                    # pending so the next reload from any operation picks it up.
                    _LOGGER.warning("Plejd: follow-up reload for a concurrent change failed; leaving it pending")
                    async_mark_reload_pending(hass, entry.entry_id)
    if not reloaded:
        if not raise_on_reload_failure:
            _LOGGER.warning("Plejd: entry failed to reload after %s; the cloud change was still made", error_context)
            return
        raise HomeAssistantError(f"Plejd: entry failed to reload after {error_context}")


def _parse_time(value: str) -> tuple[int, int, int] | None:
    """Parse 'HH:MM' or 'HH:MM:SS' into (hour, minute, second), or None if invalid."""
    parts = value.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour, minute, second


def _has_gateway(entry) -> bool:
    return bool(entry.data.get(CONF_GATEWAYS) and entry.data.get(CONF_RESOURCE_SET_ID))


def _current_transport(entry) -> str:
    """Mirror the config-flow schedules step: drop a gateway-only preference once there's no usable gateway."""
    return entry.options.get(CONF_TRANSPORT, TRANSPORT_AUTO) if _has_gateway(entry) else TRANSPORT_AUTO


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/schedules/list"})
@websocket_api.async_response
async def ws_list(hass: HomeAssistant, connection, msg) -> None:
    entry = hass.data.get(DATA_ENTRY)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    scenes = [{"index": s["index"], "name": s["name"]} for s in entry.data.get(CONF_SCENES, [])]
    connection.send_result(msg["id"], {"schedules": entry.options.get(CONF_SCHEDULES, []), "scenes": scenes})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/schedules/cloud"})
@websocket_api.async_response
async def ws_cloud_list(hass: HomeAssistant, connection, msg) -> None:
    """The site's sunset/sunrise schedules read live from the cloud, so ones made in the Plejd app show too."""
    entry = hass.data.get(DATA_ENTRY)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    session = async_get_clientsession(hass)
    try:
        token = await async_login(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
        raw = await async_get_site_raw(session, token, entry.data[CONF_SITE_ID])
        site = raw[0] if isinstance(raw, list) and raw else raw
        if not isinstance(site, dict):
            raise PlejdCloudError("malformed site response")
        parsed = parse_site(site)
    except PlejdAuthError:
        entry.async_start_reauth(hass)
        connection.send_error(msg["id"], "auth_failed", "Plejd cloud credentials rejected; reauthentication started")
        return
    # Value/Attribute/TypeError: parse_site on malformed cloud fields (untrusted data, not a bug here)
    except (PlejdCloudError, aiohttp.ClientError, OSError, TimeoutError, ValueError, AttributeError, TypeError) as err:
        connection.send_error(msg["id"], "cloud_error", f"Plejd cloud error: {str(err) or type(err).__name__}")
        return
    outputs = {(d.device_id, d.output_index): d.name for d in parsed.devices}
    devices = {d.device_id: d.name for d in parsed.devices}
    scenes = {s.scene_id: s.name for s in parsed.all_scenes if isinstance(s.name, str)}
    tracked = {s.get("schedule_id") for s in entry.data.get(CONF_CLOUD_SCHEDULES, [])}
    connection.send_result(
        msg["id"],
        {
            "schedules": [
                {
                    **asdict(ev),
                    "scene_name": scenes.get(ev.scene_id),
                    "devices": [outputs.get((d, i)) or devices.get(d, d) for d, i in ev.targets],
                    "from_app": ev.schedule_id not in tracked,
                }
                for ev in parse_astro_events(site)
            ]
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "plejd/schedules/add",
        vol.Required("name"): str,
        vol.Required("days"): [int],
        vol.Required("time"): str,
        vol.Required("scene"): int,
        vol.Optional("fade", default=0): int,
    }
)
@websocket_api.async_response
async def ws_add(hass: HomeAssistant, connection, msg) -> None:
    entry = hass.data.get(DATA_ENTRY)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return

    name = msg["name"].strip()
    if not name:
        connection.send_error(msg["id"], "name_required", "Name is required")
        return
    days = msg["days"]
    if not isinstance(days, list) or not days or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
        connection.send_error(msg["id"], "invalid_days", "Pick at least one valid day")
        return
    parsed = _parse_time(msg["time"])
    if parsed is None:
        connection.send_error(msg["id"], "invalid_time", "Invalid time")
        return
    scene_indices = {s["index"] for s in entry.data.get(CONF_SCENES, [])}
    if msg["scene"] not in scene_indices:
        connection.send_error(msg["id"], "invalid_scene", "Unknown scene")
        return
    fade = msg["fade"]
    if not isinstance(fade, int) or fade < 0:
        connection.send_error(msg["id"], "invalid_fade", "Fade must be a non-negative number of seconds")
        return

    hour, minute, second = parsed

    def _build(current_entry: ConfigEntry) -> tuple[dict, dict]:
        # Slot and id are allocated HERE, under the lock: another add completing while this
        # one waited would otherwise be handed the same free slot and the same next id.
        schedules: list[dict] = list(current_entry.options.get(CONF_SCHEDULES, []))
        used_slots = {s["slot"] for s in schedules}
        slot = next((i for i in range(TIME_EVENT_SLOTS) if i not in used_slots), None)
        if slot is None:
            connection.send_error(msg["id"], "no_free_slots", "No free schedule slots")
            raise _NothingToPersist
        next_id: int = current_entry.options.get(_NEXT_ID_KEY, 0)
        schedules.append(
            {
                "id": next_id,
                "slot": slot,
                "name": name,
                "days": sorted(set(days)),
                "time": f"{hour:02d}:{minute:02d}:{second:02d}",
                "scene": msg["scene"],
                "fade": fade,
            }
        )
        options = {
            **current_entry.options,
            CONF_SCHEDULES: schedules,
            _NEXT_ID_KEY: next_id + 1,
            CONF_TRANSPORT: _current_transport(current_entry),
        }
        return options, {"schedules": schedules}

    await _async_persist(hass, connection, msg, entry, _build)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/schedules/delete", vol.Required("schedule_id"): int})
@websocket_api.async_response
async def ws_delete(hass: HomeAssistant, connection, msg) -> None:
    entry = hass.data.get(DATA_ENTRY)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return

    schedules: list[dict] = list(entry.options.get(CONF_SCHEDULES, []))
    target = next((s for s in schedules if s["id"] == msg["schedule_id"]), None)
    if target is None:
        connection.send_error(msg["id"], "not_found", "Schedule not found")
        return

    coordinator = getattr(entry, "runtime_data", None)
    try:
        await coordinator.async_remove_time_event(target["slot"])
    except Exception:  # noqa: BLE001 - best-effort; persist the deletion whatever the mesh does
        _LOGGER.warning("Plejd: could not clear schedule slot %s from the mesh", target["slot"])

    def _build(current_entry: ConfigEntry) -> tuple[dict, dict]:
        # Read HERE, under the lock: another schedule edit may have completed while this one
        # waited for the mesh call and the lock, and a pre-lock list would undo it.
        kept = [s for s in current_entry.options.get(CONF_SCHEDULES, []) if s["id"] != msg["schedule_id"]]
        options = {**current_entry.options, CONF_SCHEDULES: kept, CONF_TRANSPORT: _current_transport(current_entry)}
        return options, {"schedules": kept}

    await _async_persist(hass, connection, msg, entry, _build)


def _settings(entry: ConfigEntry) -> dict:
    options = entry.options
    return {
        "transport": _current_transport(entry),
        "has_gateway": _has_gateway(entry),
        "holiday_lights": options.get(CONF_HOLIDAY_LIGHTS, []),
        "holiday_window_start": options.get(CONF_HOLIDAY_WINDOW_START, HOLIDAY_WINDOW_START_DEFAULT),
        "holiday_window_end": options.get(CONF_HOLIDAY_WINDOW_END, HOLIDAY_WINDOW_END_DEFAULT),
        "sync_areas": options.get(CONF_SYNC_AREAS, False),
    }


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/settings/get"})
@websocket_api.async_response
async def ws_settings_get(hass: HomeAssistant, connection, msg) -> None:
    entry = hass.data.get(DATA_ENTRY)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    connection.send_result(msg["id"], _settings(entry))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "plejd/settings/set",
        vol.Optional("transport"): vol.In([TRANSPORT_AUTO, TRANSPORT_GATEWAY, TRANSPORT_BLE]),
        vol.Optional("holiday_lights"): [str],
        vol.Optional("holiday_window_start"): str,
        vol.Optional("holiday_window_end"): str,
        vol.Optional("sync_areas"): bool,
    }
)
@websocket_api.async_response
async def ws_settings_set(hass: HomeAssistant, connection, msg) -> None:
    entry = hass.data.get(DATA_ENTRY)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    updates: dict = {}
    for key, conf in (
        ("holiday_window_start", CONF_HOLIDAY_WINDOW_START),
        ("holiday_window_end", CONF_HOLIDAY_WINDOW_END),
    ):
        if key in msg:
            parsed = _parse_time(msg[key])
            if parsed is None:
                connection.send_error(msg["id"], "invalid_time", "Invalid time")
                return
            updates[conf] = f"{parsed[0]:02d}:{parsed[1]:02d}"
    if "holiday_lights" in msg:
        updates[CONF_HOLIDAY_LIGHTS] = msg["holiday_lights"]
    if "transport" in msg:
        updates[CONF_TRANSPORT] = msg["transport"]
    if "sync_areas" in msg:
        updates[CONF_SYNC_AREAS] = msg["sync_areas"]

    def _build(current_entry: ConfigEntry) -> tuple[dict, dict]:
        options = {**current_entry.options, **updates}
        if not _has_gateway(current_entry):
            options[CONF_TRANSPORT] = TRANSPORT_AUTO  # a gateway-only preference is meaningless without one
        return options, {}

    await _async_persist(hass, connection, msg, entry, _build)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/devices/scan"})
@websocket_api.async_response
async def ws_devices_scan(hass: HomeAssistant, connection, msg) -> None:
    bluetooth = async_bluetooth_available(hass)
    connection.send_result(
        msg["id"],
        {
            "bluetooth": bluetooth,
            "devices": async_scan_unprovisioned(hass) if bluetooth else [],
            "room_categories": list(ROOM_CATEGORIES),
        },
    )


class _NothingToPersist(Exception):
    """Raised by a persist builder to abort the write; it has sent its own WS error."""


async def _async_persist(
    hass: HomeAssistant, connection, msg, entry, build: Callable[[ConfigEntry], tuple[dict, dict]]
) -> None:
    """Build `(options, result)` under the reload lock, save, reload, and answer the WS msg.

    `build` runs INSIDE the lock and must read `entry.options` itself. Acquiring the lock is
    an await point, so another schedule edit can complete between this handler's validation
    and the lock being granted; a payload built before that would overwrite the other edit,
    and worse, could hand out a slot or id that edit just claimed (#125). It may raise
    _NothingToPersist to abort after sending its own error response.
    """
    # Hold the reload lock so the entry's update listener (_async_reload_entry) doesn't
    # also reload for this same options change - we need this reload's own success/failure.
    lock = async_get_reload_lock(hass, entry.entry_id)
    reloaded = True
    async with lock:
        try:
            try:
                options, result = build(entry)
            except _NothingToPersist:
                return
            async_mark_expecting_self_reload(hass, entry.entry_id)
            try:
                hass.config_entries.async_update_entry(entry, options=options)
            except Exception:  # noqa: BLE001 - nothing was persisted; a genuine save failure
                _LOGGER.exception("Plejd: failed to save options")
                save_failed = True
            else:
                save_failed = False
                try:
                    reloaded = await hass.config_entries.async_reload(entry.entry_id)
                except Exception:  # noqa: BLE001 - options are already persisted; treat like a failed reload below
                    _LOGGER.exception("Plejd: failed to reload after saving options")
                    reloaded = False
        finally:
            hass.data.pop(_DATA_EXPECTING_SELF_RELOAD, None)
    if async_take_reload_pending(hass, entry.entry_id):
        # A concurrent options change's own reload was suppressed by the lock above while
        # ours was in flight; it may have landed after we already read entry state, so give
        # it a reload of its own instead of dropping it silently (see _async_reload_entry).
        async with lock:
            follow_up_ok = False
            try:
                follow_up_ok = await hass.config_entries.async_reload(entry.entry_id)
            except Exception:  # noqa: BLE001 - best-effort follow-up; already logged if the underlying issue recurs
                _LOGGER.exception("Plejd: follow-up reload for a concurrent option change raised")
            if not follow_up_ok:
                # A rejected reload reports failure by returning False without raising;
                # dropping it would lose that concurrent change, so leave it pending.
                _LOGGER.warning("Plejd: follow-up reload for a concurrent option change failed; leaving it pending")
                async_mark_reload_pending(hass, entry.entry_id)
    if save_failed:
        connection.send_error(msg["id"], "save_failed", "Could not save")
        return
    if reloaded is False:
        # Send a result, not an error: `options` are already persisted above, and an error with
        # no data would leave the dashboard showing its old list, where a "try again" click adds
        # a second, duplicate schedule instead of seeing the one that already saved.
        connection.send_result(msg["id"], {**result, "reload_failed": "Saved, but Plejd failed to reload; try again"})
        return
    connection.send_result(msg["id"], result)


def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_cloud_list)
    websocket_api.async_register_command(hass, ws_add)
    websocket_api.async_register_command(hass, ws_delete)
    websocket_api.async_register_command(hass, ws_settings_get)
    websocket_api.async_register_command(hass, ws_settings_set)
    websocket_api.async_register_command(hass, ws_devices_scan)
