"""The Plejd integration."""

from __future__ import annotations

import logging
from functools import partial

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry

from . import activity, area_sync, dim_binding_ws, panel, remote_profile_ws, room_ws, schedule_ws
from .add_device import async_add_device
from .bindings import PlejdDimBindings
from .const import (
    CONF_SHOW_PANEL,
    CONF_SYNC_AREAS,
    DOMAIN,
    ROOM_CATEGORIES,
    SCHEDULE_ASTRO_EVENTS,
    SCHEDULE_OFFSET_MAX,
    SCHEDULE_OFFSET_MIN,
)
from .coordinator import PlejdCoordinator, async_clear_malformed_site_issue, async_reset_self_heal_cooldown
from .discovery import async_bluetooth_available, async_scan_unprovisioned
from .holiday_mode import DATA_HOLIDAY_MODE, PlejdHolidayMode
from .manage_device import async_remove_device
from .manage_device_room import async_move_device_to_room
from .manage_room import async_remove_room, async_update_room
from .manage_scene import async_create_scene, async_remove_scene, async_update_scene
from .manage_schedule import async_create_schedule, async_remove_schedule, async_update_schedule
from .remote_profiles import PlejdRemoteProfiles

_WS_REGISTERED = f"{DOMAIN}_ws_registered"
# The config entry the permanently-registered service handlers below should act on -
# refreshed on every async_setup_entry call (including a retry or a later reinstall), so
# the handlers stay bound to whichever entry is actually current instead of the first one
# ever seen. Deliberately never cleared on unload: see the registration comment below.
_DATA_CURRENT_ENTRY = f"{DOMAIN}_current_entry"

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.SCENE,
    Platform.CLIMATE,
    Platform.EVENT,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.COVER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.UPDATE,
]

SERVICE_ADD_DEVICE = "add_device"
SERVICE_SCAN_DEVICES = "scan_new_devices"
SERVICE_ALL_OFF = "all_off"
SERVICE_UPDATE_ROOM = "update_room"
SERVICE_REMOVE_ROOM = "remove_room"
SERVICE_CREATE_SCENE = "create_scene"
SERVICE_UPDATE_SCENE = "update_scene"
SERVICE_REMOVE_SCENE = "remove_scene"
SERVICE_REMOVE_DEVICE = "remove_device"
SERVICE_MOVE_DEVICE_TO_ROOM = "move_device_to_room"
SERVICE_CREATE_SCHEDULE = "create_schedule"
SERVICE_UPDATE_SCHEDULE = "update_schedule"
SERVICE_REMOVE_SCHEDULE = "remove_schedule"

_INPUT_SETTING_SCHEMA = vol.Schema({vol.Required("input"): int, vol.Required("button_type"): str})

_SCENE_STEP_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): str,
        vol.Required("output"): int,
        vol.Required("state"): vol.In(["On", "Off", "Temperature", "PWM", "Boost", "Frost"]),
        vol.Required("value"): int,
        vol.Optional("color_temperature"): int,
        vol.Optional("coverable_tilt"): int,
        vol.Optional("climate_boost_time"): int,
    }
)

_ADD_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("device_address"): str,
        vol.Required("name"): str,
        vol.Optional("hardware_id", default="0"): str,
        vol.Optional("room_id"): str,
        vol.Optional("room_title"): str,
        vol.Optional("room_category"): vol.In(ROOM_CATEGORIES),
        vol.Optional("firmware_build_time", default=0): int,
        vol.Optional("input_settings", default=[]): [_INPUT_SETTING_SCHEMA],
    }
)

_UPDATE_ROOM_SCHEMA = vol.Schema(
    {
        vol.Required("room_id"): str,
        vol.Optional("title"): str,
        vol.Optional("order"): vol.All(int, vol.Range(min=0)),
        vol.Optional("category"): vol.In(ROOM_CATEGORIES),
    }
)

_REMOVE_ROOM_SCHEMA = vol.Schema({vol.Required("room_id"): str})

_CREATE_SCENE_SCHEMA = vol.Schema(
    {
        vol.Required("title"): str,
        vol.Required("scene_steps"): [_SCENE_STEP_SCHEMA],
        vol.Optional("order", default=0): vol.All(int, vol.Range(min=0)),
        vol.Optional("hidden_from_scene_list", default=False): bool,
    }
)

_UPDATE_SCENE_SCHEMA = vol.Schema(
    {
        vol.Required("scene_id"): str,
        vol.Optional("title"): str,
        vol.Optional("order"): vol.All(int, vol.Range(min=0)),
        vol.Optional("scene_steps"): [_SCENE_STEP_SCHEMA],
        vol.Optional("hidden_from_scene_list"): bool,
    }
)

_REMOVE_SCENE_SCHEMA = vol.Schema({vol.Required("scene_id"): str})

_REMOVE_DEVICE_SCHEMA = vol.Schema({vol.Required("device_id"): str})

_MOVE_DEVICE_TO_ROOM_SCHEMA = vol.Schema({vol.Required("device_id"): str, vol.Required("room_id"): str})

_SCHEDULE_OFFSET_VALIDATOR = vol.All(int, vol.Range(min=SCHEDULE_OFFSET_MIN, max=SCHEDULE_OFFSET_MAX))

_NIGHT_REDUCTION_SCHEMA = vol.Schema(
    {
        vol.Required("scene_steps"): [_SCENE_STEP_SCHEMA],
        vol.Required("start_time"): str,
        vol.Required("end_time"): str,
        vol.Optional("weekend_start_time"): str,
        vol.Optional("weekend_end_time"): str,
    }
)

_CREATE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("title"): str,
        vol.Required("scene_steps"): [_SCENE_STEP_SCHEMA],
        vol.Required("start_event"): vol.In(SCHEDULE_ASTRO_EVENTS),
        vol.Required("start_offset"): _SCHEDULE_OFFSET_VALIDATOR,
        vol.Required("end_event"): vol.In(SCHEDULE_ASTRO_EVENTS),
        vol.Required("end_offset"): _SCHEDULE_OFFSET_VALIDATOR,
        vol.Optional("scheduled_days"): [vol.All(int, vol.Range(min=0, max=6))],
        vol.Optional("fade_time", default=0): vol.All(int, vol.Range(min=0)),
        vol.Optional("night_reduction"): _NIGHT_REDUCTION_SCHEMA,
    }
)

_UPDATE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("schedule_id"): str,
        vol.Optional("title"): str,
        vol.Optional("scene_steps"): [_SCENE_STEP_SCHEMA],
        vol.Optional("start_event"): vol.In(SCHEDULE_ASTRO_EVENTS),
        vol.Optional("start_offset"): _SCHEDULE_OFFSET_VALIDATOR,
        vol.Optional("end_event"): vol.In(SCHEDULE_ASTRO_EVENTS),
        vol.Optional("end_offset"): _SCHEDULE_OFFSET_VALIDATOR,
        vol.Optional("scheduled_days"): [vol.All(int, vol.Range(min=0, max=6))],
        vol.Optional("fade_time"): vol.All(int, vol.Range(min=0)),
        vol.Optional("activated"): bool,
        vol.Optional("night_reduction"): _NIGHT_REDUCTION_SCHEMA,
    }
)

_REMOVE_SCHEDULE_SCHEMA = vol.Schema({vol.Required("schedule_id"): str})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = PlejdCoordinator(hass, entry)
    # Assign before connecting so diagnostics work even while setup is still failing.
    entry.runtime_data = coordinator
    # Refreshed on every attempt (including a retry, or a later remove+reinstall) so the
    # permanently-registered handlers below always act on whichever entry is current -
    # see _DATA_CURRENT_ENTRY.
    hass.data[_DATA_CURRENT_ENTRY] = entry

    async def _async_handle_add_device(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        await async_add_device(
            hass,
            current_entry,
            address=call.data["device_address"],
            name=call.data["name"],
            hardware_id=call.data.get("hardware_id", "0"),
            room_id=call.data.get("room_id"),
            room_title=call.data.get("room_title"),
            room_category=call.data.get("room_category"),
            firmware_build_time=int(call.data.get("firmware_build_time", 0)),
            input_settings=call.data.get("input_settings", []),
        )

    async def _async_handle_scan_devices(call) -> None:
        if not async_bluetooth_available(hass):
            raise HomeAssistantError(
                "Bluetooth is not available on this Home Assistant instance. Add a local "
                "Bluetooth adapter or an ESPHome Bluetooth proxy, then try again."
            )
        new_devices = async_scan_unprovisioned(hass)
        _LOGGER.info("Plejd scan found %d unprovisioned device(s)", len(new_devices))
        _LOGGER.debug("Plejd scan found unprovisioned device(s): %s", [d["address"] for d in new_devices])
        hass.bus.async_fire(f"{DOMAIN}_new_devices_found", {"devices": new_devices})

    async def _async_handle_all_off(call) -> None:
        # entry.runtime_data of the CURRENT entry, not a closed-over coordinator: this
        # handler is only ever (re-)registered on the first setup attempt (see the
        # registration guard below), so a later retry's or reinstall's coordinator must
        # be looked up fresh.
        await hass.data[_DATA_CURRENT_ENTRY].runtime_data.async_all_off()

    async def _async_handle_update_room(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        await async_update_room(
            hass,
            current_entry,
            room_id=call.data["room_id"],
            title=call.data.get("title"),
            order=call.data.get("order"),
            category=call.data.get("category"),
        )

    async def _async_handle_remove_room(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        await async_remove_room(hass, current_entry, room_id=call.data["room_id"])

    async def _async_handle_create_scene(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        await async_create_scene(
            hass,
            current_entry,
            title=call.data["title"],
            scene_steps=call.data["scene_steps"],
            order=call.data["order"],
            hidden_from_scene_list=call.data["hidden_from_scene_list"],
        )

    async def _async_handle_update_scene(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        await async_update_scene(
            hass,
            current_entry,
            scene_id=call.data["scene_id"],
            title=call.data.get("title"),
            order=call.data.get("order"),
            scene_steps=call.data.get("scene_steps"),
            hidden_from_scene_list=call.data.get("hidden_from_scene_list"),
        )

    async def _async_handle_remove_scene(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        await async_remove_scene(hass, current_entry, scene_id=call.data["scene_id"])

    async def _async_handle_remove_device(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        await async_remove_device(hass, current_entry, device_id=call.data["device_id"])

    async def _async_handle_move_device_to_room(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        await async_move_device_to_room(
            hass, current_entry, device_id=call.data["device_id"], room_id=call.data["room_id"]
        )

    async def _async_handle_create_schedule(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        # async_create_schedule fires plejd_schedule_created itself (before its own
        # post-create refresh, which can fail independently of the create having worked) -
        # nothing left to do here with its returned schedule_id.
        await async_create_schedule(
            hass,
            current_entry,
            title=call.data["title"],
            scene_steps=call.data["scene_steps"],
            start_event=call.data["start_event"],
            start_offset=call.data["start_offset"],
            end_event=call.data["end_event"],
            end_offset=call.data["end_offset"],
            scheduled_days=call.data.get("scheduled_days"),
            fade_time=call.data["fade_time"],
            night_reduction=call.data.get("night_reduction"),
        )

    async def _async_handle_update_schedule(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        await async_update_schedule(
            hass,
            current_entry,
            schedule_id=call.data["schedule_id"],
            title=call.data.get("title"),
            scene_steps=call.data.get("scene_steps"),
            start_event=call.data.get("start_event"),
            start_offset=call.data.get("start_offset"),
            end_event=call.data.get("end_event"),
            end_offset=call.data.get("end_offset"),
            scheduled_days=call.data.get("scheduled_days"),
            fade_time=call.data.get("fade_time"),
            activated=call.data.get("activated"),
            night_reduction=call.data.get("night_reduction"),
        )

    async def _async_handle_remove_schedule(call) -> None:
        current_entry = hass.data[_DATA_CURRENT_ENTRY]
        await async_remove_schedule(hass, current_entry, schedule_id=call.data["schedule_id"])

    # Registered once per hass lifetime, before coordinator.async_start(), and NOT torn
    # down by entry.async_on_unload: HA runs every already-registered on_unload callback
    # as cleanup when async_setup_entry raises ConfigEntryNotReady (same path a failed
    # mesh connection takes), which would otherwise unregister these services again right
    # after registering them - defeating the point of registering early (e.g. being able
    # to remove_device the one stale device that's keeping the mesh from connecting).
    # Same permanent-registration pattern already used below for the WS commands; safe
    # here too since this integration is single_config_entry (only one entry ever exists)
    # and every handler above reads hass.data[_DATA_CURRENT_ENTRY] fresh rather than
    # closing over this specific attempt's entry, so they rebind correctly even after a
    # full remove+reinstall (not just a setup retry) without needing to re-register.
    # Checked per-service (not one all-or-nothing flag): a service added by a later
    # integration update would otherwise stay unregistered on a reload of an already-
    # loaded HA process, needing a full HA restart just to appear.
    def _register_once(service: str, handler, schema=None) -> None:
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, handler, schema=schema)

    _register_once(SERVICE_ADD_DEVICE, _async_handle_add_device, _ADD_DEVICE_SCHEMA)
    _register_once(SERVICE_SCAN_DEVICES, _async_handle_scan_devices)
    _register_once(SERVICE_ALL_OFF, _async_handle_all_off)
    _register_once(SERVICE_UPDATE_ROOM, _async_handle_update_room, _UPDATE_ROOM_SCHEMA)
    _register_once(SERVICE_REMOVE_ROOM, _async_handle_remove_room, _REMOVE_ROOM_SCHEMA)
    _register_once(SERVICE_CREATE_SCENE, _async_handle_create_scene, _CREATE_SCENE_SCHEMA)
    _register_once(SERVICE_UPDATE_SCENE, _async_handle_update_scene, _UPDATE_SCENE_SCHEMA)
    _register_once(SERVICE_REMOVE_SCENE, _async_handle_remove_scene, _REMOVE_SCENE_SCHEMA)
    _register_once(SERVICE_REMOVE_DEVICE, _async_handle_remove_device, _REMOVE_DEVICE_SCHEMA)
    _register_once(SERVICE_MOVE_DEVICE_TO_ROOM, _async_handle_move_device_to_room, _MOVE_DEVICE_TO_ROOM_SCHEMA)
    _register_once(SERVICE_CREATE_SCHEDULE, _async_handle_create_schedule, _CREATE_SCHEDULE_SCHEMA)
    _register_once(SERVICE_UPDATE_SCHEDULE, _async_handle_update_schedule, _UPDATE_SCHEDULE_SCHEMA)
    _register_once(SERVICE_REMOVE_SCHEDULE, _async_handle_remove_schedule, _REMOVE_SCHEDULE_SCHEMA)

    # Register the sidebar dashboard before starting the coordinator. It's optional, so a
    # failure (e.g. another panel already owns the `plejd` url path) must not abort setup —
    # the mesh/lights still work, and options remain reachable to hide it. HA also runs
    # this on_unload if a later setup step fails. Toggle via the options.
    if entry.options.get(CONF_SHOW_PANEL, True):
        try:
            await panel.async_register_panel(hass)
        except Exception:  # noqa: BLE001 - the dashboard is optional; never fail setup over it
            _LOGGER.warning("Plejd dashboard panel could not be registered; continuing without it", exc_info=True)
    entry.async_on_unload(lambda: panel.async_unregister_panel(hass))
    await coordinator.async_start()

    # Holiday mode (presence simulation) — constructed before platform forwarding so the
    # switch platform can look it up; the switch entity itself controls start/stop.
    hass.data[DATA_HOLIDAY_MODE] = PlejdHolidayMode(hass, entry)

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Don't leak the BLE connection, or a running holiday-mode timer, if platform setup
        # fails partway — a platform forwarded before the failure (e.g. a restored-on
        # holiday switch) may have already started it.
        holiday_mode = hass.data.pop(DATA_HOLIDAY_MODE, None)
        if holiday_mode is not None:
            # A platform earlier than switch may not have run yet, so this manager's
            # persisted deadlines were never loaded; start() loads them (idempotent if
            # a restored switch already did) so stop()'s persist can't wipe the store
            # with an empty, never-loaded in-memory state.
            await holiday_mode.async_start()
            await holiday_mode.async_stop()
        await coordinator.async_shutdown()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    # Follow Plejd room moves into HA areas (opt-in; the daily cloud poll reloads on change).
    if entry.options.get(CONF_SYNC_AREAS, False):
        # Subscribe first, so an area change while the initial sync runs is queued behind it, not lost.
        entry.async_on_unload(area_sync.async_listen_area_changes(hass, entry))
        try:
            await area_sync.async_sync_areas(hass, entry)
        except Exception:  # noqa: BLE001 - optional; a registry/storage error must not fail setup
            _LOGGER.warning("Plejd: could not sync device areas with Plejd rooms", exc_info=True)
    else:
        # Cleared on the reload that turns syncing off, so turning it on again aligns every device.
        try:
            await area_sync.async_reset_areas(hass, entry)
        except Exception:  # noqa: BLE001 - optional; a storage error must not fail setup
            _LOGGER.warning("Plejd: could not clear the area sync state", exc_info=True)
    # Mirror HA device renames back to the Plejd app (cloud title update).
    entry.async_on_unload(
        hass.bus.async_listen(
            device_registry.EVENT_DEVICE_REGISTRY_UPDATED, coordinator.async_handle_device_registry_update
        )
    )

    # Remote → light dim bindings (managed from the dashboard via the WebSocket API).
    # Optional, like the panel: a storage error must not stop the mesh/lights loading.
    dim_bindings = PlejdDimBindings(hass)
    try:
        await dim_bindings.async_load()
    except Exception:  # noqa: BLE001 - bindings are optional; continue with an empty manager
        _LOGGER.warning("Plejd: could not load dim bindings; continuing without them", exc_info=True)
    hass.data[dim_binding_ws.DATA_BINDINGS] = dim_bindings
    entry.async_on_unload(partial(_drop, hass, dim_binding_ws.DATA_BINDINGS))
    entry.async_on_unload(dim_bindings.shutdown)

    # Schedules (managed from the dashboard via the WebSocket API too, mirroring bindings above).
    hass.data[schedule_ws.DATA_ENTRY] = entry
    entry.async_on_unload(partial(_drop, hass, schedule_ws.DATA_ENTRY))

    # Custom remote button-profile overrides (see remote_profiles.py). Same optional,
    # storage-backed pattern as the dim bindings above.
    remote_profiles = PlejdRemoteProfiles(hass)
    try:
        await remote_profiles.async_load()
    except Exception:  # noqa: BLE001 - optional; continue with an empty manager
        _LOGGER.warning("Plejd: could not load remote profiles; continuing without them", exc_info=True)
    hass.data[remote_profile_ws.DATA_REMOTE_PROFILES] = remote_profiles
    entry.async_on_unload(partial(_drop, hass, remote_profile_ws.DATA_REMOTE_PROFILES))

    if not hass.data.get(_WS_REGISTERED):
        dim_binding_ws.async_register(hass)  # hass-global commands; register once
        schedule_ws.async_register(hass)
        remote_profile_ws.async_register(hass)
        room_ws.async_register(hass)
        activity.async_register(hass)
        hass.data[_WS_REGISTERED] = True
    # Activity log for the dashboard's Log tab, started last so a setup that fails earlier never starts it.
    # Optional like the bindings: a storage error mustn't stop setup. Stopped in async_unload_entry, and only
    # when the unload succeeds: older HA runs on_unload callbacks even after a refused unload, which would
    # leave a still-running integration without its log.
    activity_log = activity.PlejdActivityLog(hass, entry)
    try:
        await activity_log.async_load()
    except Exception:  # noqa: BLE001 - optional; start with an empty log
        _LOGGER.warning("Plejd: could not load the activity log; starting empty", exc_info=True)
    activity_log.async_start()
    hass.data[activity.DATA_ACTIVITY] = activity_log

    # Room renames/moves and site syncs all reload the entry without touching entity ids; tell an
    # open dashboard to fetch the rooms again.
    hass.bus.async_fire(room_ws.EVENT_ROOMS_CHANGED, {})
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Schedules live in the entry options; reload so added/removed switches take effect.
    # Skip when a management operation (schedule WS save, add_device, room/scene/schedule
    # service, ...) already owns this entry's reload lock - it's already reloading (or about
    # to) for its own change, so a second reload here would race it.
    if schedule_ws.async_get_reload_lock(hass, entry.entry_id).locked():
        if schedule_ws.async_consume_expected_self_reload(hass, entry.entry_id):
            # The lock holder's own async_update_entry() triggered this - it already
            # covers this change with its own upcoming reload, so no follow-up is needed.
            return
        # Not anticipated by the current lock holder, so it's a genuinely different,
        # concurrent change that landed while the lock was held - mark it pending instead
        # of assuming it's already covered; the lock holder checks this once it's done.
        schedule_ws.async_mark_reload_pending(hass, entry.entry_id)
        return
    # Captured BEFORE the reload: this reload can only be said to cover a change already
    # pending when it started. A marker set while it runs belongs to a change that landed
    # after the entry was read, so consuming it would skip a follow-up that is still needed.
    pending_token = schedule_ws.async_reload_pending_token(hass, entry.entry_id)
    if await hass.config_entries.async_reload(entry.entry_id):
        # This reload applied whatever the entry held when it started, which includes any
        # change still marked pending from an earlier follow-up that failed. Leaving that
        # marker set would make the next management operation reload a second time for a
        # change already live - a needless teardown and BLE/gateway reconnect.
        schedule_ws.async_take_reload_pending_token(hass, entry.entry_id, pending_token)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Stop holiday mode (and turn off any lights it owns) before the light platform and
    # the mesh connection go away, or the cleanup calls would target entities that no
    # longer exist / a mesh that can no longer reach them. Unlike the coordinator below,
    # this runs unconditionally (not gated on unload_ok): its own timer/state is this
    # integration's alone to own, independent of whether some other platform's unload fails.
    # Only *remove* it from hass.data once the unload actually succeeds, though — if a
    # platform refuses to unload, the entry (and its holiday switch, still reporting on)
    # stays loaded, so resume it instead of leaving presence simulation silently stopped —
    # but only if it was actually running before this stop, or a switch left off would
    # come back with a hidden timer behind an entity that still reads off (#89 review).
    # A later retry must still be able to find it either way (async_stop() is idempotent,
    # so re-running it then is a no-op).
    holiday_mode = hass.data.get(DATA_HOLIDAY_MODE)
    was_holiday_mode_running = holiday_mode is not None and holiday_mode.is_running
    if holiday_mode is not None:
        await holiday_mode.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DATA_HOLIDAY_MODE, None)
        if (activity_log := hass.data.pop(activity.DATA_ACTIVITY, None)) is not None:
            try:
                await activity_log.async_stop()
            except Exception:  # noqa: BLE001 - the log is optional; never leave the mesh connection running over it
                _LOGGER.warning("Plejd: could not save the activity log on unload", exc_info=True)
        await entry.runtime_data.async_shutdown()
    elif was_holiday_mode_running:
        await holiday_mode.async_start()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up state that outlives the entry itself."""
    try:
        await activity.async_remove_store(hass, entry.entry_id)
    except Exception:  # noqa: BLE001 - optional; the cleanup below must still run
        _LOGGER.warning("Plejd: could not delete the activity log", exc_info=True)
    # The malformed-cloud repair issue is persistent, so nothing else would ever delete it
    # once the entry is gone: its only other clear paths are a healthy poll or a successful
    # reconfigure, neither of which can happen after removal. Without this the user is left
    # with an orphaned warning about an integration they no longer have, surviving restarts.
    async_clear_malformed_site_issue(hass, entry.entry_id)
    async_reset_self_heal_cooldown(hass, entry.entry_id)
    await area_sync.async_reset_areas(hass, entry)


def _drop(hass: HomeAssistant, key: str) -> None:
    # Returns None on purpose: HA schedules a non-None on_unload return value as a task,
    # and a bare `lambda: hass.data.pop(...)` returns the popped object, failing the unload.
    hass.data.pop(key, None)
