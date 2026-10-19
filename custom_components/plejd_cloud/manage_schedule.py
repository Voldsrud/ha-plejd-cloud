"""Create, update, and remove Plejd cloud schedules (astro triggers + night reduction).

The real Plejd app's "Schemaläggning" feature - confirmed via a live capture to be
entirely cloud-side: a TimeEvent Parse object (the sunset/sunrise-relative trigger, plus
an optional night-reduction quiet-hours window) and one or two ordinary (hidden) scenes
it runs, tied together by a CreatedById embedded in each scene's own settings JSON. Not
the on-device weekly schedules schedule_ws.py manages (entry.options[CONF_SCHEDULES],
CMD_TIME_EVENT_* mesh commands) - that's a separate, BLE-only mechanism; like
manage_scene.py, this one never touches the mesh directly.

Create and update are confirmed to be genuinely separate Parse functions
(createTimeEvent_V3 / updateTimeEvent_V3) - creation gets a server-generated id back
(the response's eventId), so a scene's CreatedById tag can only be set in a follow-up
updateScene call once that id is known; the scenes themselves are created untagged.
Removal is a whole-state updateTimeEvent_V3 resend with dirtyRemove=true, followed by
the dedicated removeTimeEvent_V3 call - see async_remove_schedule's docstring for what's
simplified relative to the captured sequence.

getSiteById's own response for existing cloud schedules hasn't been captured/confirmed,
so this only tracks schedules created through this integration itself
(entry.data[CONF_CLOUD_SCHEDULES]) - async_update_schedule/async_remove_schedule can only
target one of those, not a schedule created through the app. Only "astro" mode (the only
one captured) is supported; a fixed-clock-time trigger mode's wire shape is unconfirmed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import schedule_ws
from .cloud import PlejdAuthError, PlejdCloudError, async_get_site, async_login
from .cloud import async_create_scene as async_cloud_create_scene
from .cloud import async_create_time_event as async_cloud_create_time_event
from .cloud import async_remove_scene as async_cloud_remove_scene
from .cloud import async_remove_time_event as async_cloud_remove_time_event
from .cloud import async_update_scene as async_cloud_update_scene
from .cloud import async_update_time_event as async_cloud_update_time_event
from .const import (
    CONF_CLOUD_SCHEDULES,
    CONF_SITE_ID,
    DOMAIN,
    SCHEDULE_ASTRO_EVENTS,
    SCHEDULE_OFFSET_MAX,
    SCHEDULE_OFFSET_MIN,
)

_LOGGER = logging.getLogger(__name__)

_DATA_LOCKS = "plejd_cloud_schedule_locks"


def _async_get_lock(hass: HomeAssistant, entry: ConfigEntry) -> asyncio.Lock:
    """One lock per config entry, so concurrent create/update calls can't race on CONF_CLOUD_SCHEDULES.

    Unlike CONF_DEVICES/CONF_SCENES/etc, always rebuilt wholesale from a fresh getSiteById,
    this list is the only place a cloud schedule's ids live (getSiteById's own response for
    existing schedules isn't confirmed) - a lost update here permanently orphans the schedule
    from this integration's point of view, not just until the next refresh.
    """
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(_DATA_LOCKS, {})
    return locks.setdefault(entry.entry_id, asyncio.Lock())


async def _async_login_and_get_site(hass: HomeAssistant, entry: ConfigEntry):
    http_session = async_get_clientsession(hass)
    try:
        token = await async_login(http_session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    except PlejdAuthError as err:
        entry.async_start_reauth(hass)
        raise HomeAssistantError("Plejd cloud credentials rejected; reauthentication started") from err
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error: {err}") from err
    try:
        site = await async_get_site(http_session, token, entry.data[CONF_SITE_ID])
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error: {err}") from err
    return http_session, token, site


async def _async_refresh_and_reload(
    hass: HomeAssistant,
    entry: ConfigEntry,
    http_session,
    token,
    *,
    cloud_schedules: list[dict],
    mutation: str | None = None,
) -> None:
    try:
        fresh_site = await async_get_site(http_session, token, entry.data[CONF_SITE_ID])
    except PlejdCloudError as err:
        # Even if refreshing the rest of the site fails, don't lose track of a cloud schedule
        # that was already created/updated on the cloud - entry.data[CONF_CLOUD_SCHEDULES] is
        # the only place this integration remembers it (getSiteById's own listing of existing
        # schedules isn't confirmed), so losing this write would orphan it permanently.
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CLOUD_SCHEDULES: cloud_schedules})
        raise HomeAssistantError(f"Plejd cloud error refreshing site: {err}") from err
    await schedule_ws.async_reload_entry_with_lock(
        hass,
        entry,
        lambda e: schedule_ws.site_data_overlay(e, fresh_site, {CONF_CLOUD_SCHEDULES: cloud_schedules}),
        # A create/remove's cloud mutation has already happened and isn't safely retryable
        # by the time this runs - raising on a reload failure here would make the whole
        # service call look failed: retrying a create makes a duplicate schedule, retrying
        # a remove just gets "not found". Only the reload itself needs a manual retry. A
        # plain update is safe to retry as-is.
        raise_on_reload_failure=mutation is None,
        error_context=f"a schedule {mutation}" if mutation else "a schedule update",
    )


async def _sync_cloud_schedules_cache(hass: HomeAssistant, entry: ConfigEntry, cloud_schedules: list[dict]) -> None:
    """Write entry.data[CONF_CLOUD_SCHEDULES] immediately, without triggering a reload.

    Called right before firing plejd_schedule_created: a listener reacting to that event
    synchronously (e.g. calling update_schedule right away) would otherwise race the much
    later entry.data write inside _async_refresh_and_reload and see the new schedule as
    "not tracked". The full refresh (fresh devices/scenes/etc, and the real reload) still
    follows immediately after this.

    async_update_entry schedules the entry's update listener as a new task rather than
    running it inline, so it hasn't necessarily run by the time async_update_entry returns -
    yield one loop iteration so it (having no awaits of its own on the "lock held" path) has
    a chance to run while this lock is still held, instead of running unguarded after it's
    released below. Deliberately not hass.async_block_till_done(): that drains EVERY pending
    HA task, including any other management operation already waiting to acquire this same
    lock - which would deadlock (it can't finish without the lock we're still holding).
    A genuinely concurrent change detected during that window still gets its own follow-up
    reload here (not discarded): the immediately-following _async_refresh_and_reload usually
    covers it too, but its PlejdCloudError path can persist this cache write and raise
    without ever reloading, which would otherwise silently drop the concurrent change's
    needed reload.
    """
    lock = schedule_ws.async_get_reload_lock(hass, entry.entry_id)
    try:
        async with lock:
            schedule_ws.async_mark_expecting_self_reload(hass, entry.entry_id)
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CLOUD_SCHEDULES: cloud_schedules})
            await asyncio.sleep(0)
    finally:
        # Defensive: clears it even if the listener never ran within this session (e.g. a
        # genuinely no-op write), so it can't leak into a later, unrelated one.
        schedule_ws.async_consume_expected_self_reload(hass, entry.entry_id)
    if schedule_ws.async_take_reload_pending(hass, entry.entry_id):
        async with lock:
            try:
                await hass.config_entries.async_reload(entry.entry_id)
            except Exception:  # noqa: BLE001 - best-effort follow-up for someone else's change
                _LOGGER.warning("Plejd: follow-up reload for a concurrent change failed")


async def _async_cleanup_orphaned_scenes(http_session, token: str, site_id: str, scene_ids: list[str]) -> None:
    """Best-effort removal of scene(s) already created before a later step in the same call failed.

    Deliberately swallows PlejdCloudError: the caller is already raising the real failure, and an
    orphaned hidden scene (worst case) is a lesser problem than masking that failure with this one.
    """
    for scene_id in scene_ids:
        try:
            ok = await async_cloud_remove_scene(http_session, token, site_id, scene_id)
        except PlejdCloudError:
            # Best-effort: the caller is already raising the real failure, so this must not
            # replace or mask it - but a silently-abandoned hidden scene needs a trail for
            # support to find later, so log it rather than dropping it entirely.
            _LOGGER.warning(
                "Plejd: could not clean up orphaned scene %s after a schedule create/update failure",
                scene_id,
                exc_info=True,
            )
            continue
        if not ok:
            _LOGGER.warning(
                "Plejd: cloud rejected cleanup of orphaned scene %s after a schedule create/update failure",
                scene_id,
            )


def _validate_trigger(event: str, offset: int, label: str) -> None:
    if event not in SCHEDULE_ASTRO_EVENTS:
        raise HomeAssistantError(f"{label}_event must be one of {SCHEDULE_ASTRO_EVENTS}")
    if not SCHEDULE_OFFSET_MIN <= offset <= SCHEDULE_OFFSET_MAX:
        raise HomeAssistantError(
            f"{label}_offset must be between {SCHEDULE_OFFSET_MIN} and {SCHEDULE_OFFSET_MAX} minutes"
        )


def _validate_days(scheduled_days: list[int] | None) -> list[int]:
    """None means "not specified" (defaults to every day); an explicit empty list is rejected outright.

    scheduled_days is falsy-but-meaningful input, so `if scheduled_days` would silently treat an
    explicitly empty list the same as "not specified" and default it to every day - the opposite of
    what a caller passing [] almost certainly intends. Use activated=False to pause a schedule
    instead of trying to express "no days" here.
    """
    if scheduled_days is None:
        return list(range(7))
    days = sorted(set(scheduled_days))
    if not days:
        raise HomeAssistantError("scheduled_days must include at least one day (0-6); use activated=False to pause")
    # bool is a subclass of int in Python (True == 1), so an explicit isinstance(d, bool)
    # exclusion is needed or a caller passing [True, False] would silently serialize as
    # JSON true/false instead of being rejected as not a weekday number.
    if not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
        raise HomeAssistantError("scheduled_days must be integers 0-6 (Monday=0)")
    return days


_TIME_HH_MM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_time(value: str, label: str) -> None:
    if not _TIME_HH_MM.match(value):
        raise HomeAssistantError(f'night_reduction\'s {label} must be a 24-hour "HH:MM" time, got {value!r}')


def _validate_night_reduction(night_reduction: dict) -> None:
    if not night_reduction.get("scene_steps"):
        raise HomeAssistantError("night_reduction needs at least one scene step")
    if not night_reduction.get("start_time") or not night_reduction.get("end_time"):
        raise HomeAssistantError("night_reduction needs start_time and end_time")
    _validate_time(night_reduction["start_time"], "start_time")
    _validate_time(night_reduction["end_time"], "end_time")
    has_weekend_start = night_reduction.get("weekend_start_time") is not None
    has_weekend_end = night_reduction.get("weekend_end_time") is not None
    if has_weekend_start != has_weekend_end:
        raise HomeAssistantError("night_reduction's weekend_start_time and weekend_end_time must be given together")
    if has_weekend_start:
        _validate_time(night_reduction["weekend_start_time"], "weekend_start_time")
        _validate_time(night_reduction["weekend_end_time"], "weekend_end_time")


def _astro_event_settings(schedule_id: str | None = None) -> str:
    """The on-scene's settings JSON. schedule_id is only known after createTimeEvent_V3 returns
    its server-generated eventId, so the scene is created without CreatedById and backfilled."""
    payload: dict[str, object] = {"SceneType": "AstroEventScene"}
    if schedule_id is not None:
        payload["CreatedById"] = schedule_id
    return json.dumps(payload)


def _night_reduction_settings(schedule_id: str | None = None) -> str:
    payload: dict[str, object] = {"SceneType": "NightReductionScene"}
    if schedule_id is not None:
        payload["CreatedById"] = schedule_id
    return json.dumps(payload)


def _night_reduction_result(scene_id: str, night_reduction: dict) -> dict:
    return {
        "scene_id": scene_id,
        "device_ids": sorted({s["device_id"] for s in night_reduction["scene_steps"]}),
        "start_time": night_reduction["start_time"],
        "end_time": night_reduction["end_time"],
        "weekend_start_time": night_reduction.get("weekend_start_time"),
        "weekend_end_time": night_reduction.get("weekend_end_time"),
    }


def _all_device_ids(schedule: dict) -> set[str]:
    """The union of devices a schedule's on-scene and (if any) night-reduction scene target.

    Used for dirtyDevices/dirtyRemovedDevices - a device referenced only by the night-reduction
    scene still needs its local cache marked dirty on the cloud, same as an on-scene device.
    """
    night_reduction = schedule.get("night_reduction") or {}
    return set(schedule.get("device_ids") or []) | set(night_reduction.get("device_ids") or [])


async def async_create_schedule(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    title: str,
    scene_steps: list[dict],
    start_event: str,
    start_offset: int,
    end_event: str,
    end_offset: int,
    scheduled_days: list[int] | None = None,
    fade_time: int = 0,
    night_reduction: dict | None = None,
) -> str:
    """Create a cloud schedule: an on-scene, an optional night-reduction scene, and the trigger linking them.

    Returns the generated schedule_id - the only place a caller can otherwise learn it is by
    reading entry.data[CONF_CLOUD_SCHEDULES] directly, since getSiteById can't be used to
    rediscover it (see the module docstring); the schedule_created service handler fires it
    as an event so it's visible from the Services UI too.
    """
    if not scene_steps:
        raise HomeAssistantError("create_schedule needs at least one scene step")
    _validate_trigger(start_event, start_offset, "start")
    _validate_trigger(end_event, end_offset, "end")
    days = _validate_days(scheduled_days)
    if night_reduction is not None:
        _validate_night_reduction(night_reduction)

    http_session, token, site = await _async_login_and_get_site(hass, entry)
    device_ids = sorted({s["device_id"] for s in scene_steps})

    # Held for the whole cloud+cache sequence, not just the final write: this integration is
    # the only place a cloud schedule's ids are remembered, so even the cloud-call ordering
    # (not just the local list) must be serialized against a concurrent create/update on the
    # same entry.
    async with _async_get_lock(hass, entry):
        created_scene_ids: list[str] = []
        try:
            on_scene_id = await async_cloud_create_scene(
                http_session,
                token,
                site.site_id,
                title,
                scene_steps,
                hidden_from_scene_list=True,
                settings=_astro_event_settings(),
            )
            created_scene_ids.append(on_scene_id)
            night_scene_id: str | None = None
            night_reduction_result: dict | None = None
            if night_reduction is not None:
                night_scene_id = await async_cloud_create_scene(
                    http_session,
                    token,
                    site.site_id,
                    f"{title} Nattläge",
                    night_reduction["scene_steps"],
                    hidden_from_scene_list=True,
                    settings=_night_reduction_settings(),
                )
                created_scene_ids.append(night_scene_id)
                night_reduction_result = _night_reduction_result(night_scene_id, night_reduction)
            dirty_devices = sorted(
                set(device_ids) | (set(night_reduction_result["device_ids"]) if night_reduction_result else set())
            )
            result = await async_cloud_create_time_event(
                http_session,
                token,
                site.site_id,
                on_scene_id,
                scheduled_days=days,
                fade_time=fade_time,
                activated=True,
                start_event=start_event,
                start_offset=start_offset,
                end_event=end_event,
                end_offset=end_offset,
                dirty_devices=dirty_devices,
                night_reduction=night_reduction_result,
            )
        except PlejdCloudError as err:
            await _async_cleanup_orphaned_scenes(http_session, token, site.site_id, created_scene_ids)
            raise HomeAssistantError(f"Plejd cloud error creating schedule: {err}") from err
        if result is None or not result.get("eventId"):
            await _async_cleanup_orphaned_scenes(http_session, token, site.site_id, created_scene_ids)
            raise HomeAssistantError("Plejd cloud rejected the schedule creation")
        schedule_id = result["eventId"]

        # The scene(s) exist and the trigger is live at this point - the schedule works even
        # if this backfill fails, so a failure here is logged, not raised (matches the
        # orphaned-scene cleanup's own "don't mask what already succeeded" philosophy).
        try:
            if not await async_cloud_update_scene(
                http_session, token, site.site_id, on_scene_id, settings=_astro_event_settings(schedule_id)
            ):
                _LOGGER.warning("Plejd: cloud rejected tagging schedule scene %s with its CreatedById", on_scene_id)
            if night_scene_id is not None and not await async_cloud_update_scene(
                http_session, token, site.site_id, night_scene_id, settings=_night_reduction_settings(schedule_id)
            ):
                _LOGGER.warning(
                    "Plejd: cloud rejected tagging night-reduction scene %s with its CreatedById", night_scene_id
                )
        except PlejdCloudError:
            _LOGGER.warning("Plejd: could not tag schedule %s's scene(s) with CreatedById", schedule_id, exc_info=True)

        schedule = {
            "schedule_id": schedule_id,
            "scene_id": on_scene_id,
            "title": title,
            "device_ids": device_ids,
            "scheduled_days": days,
            "fade_time": fade_time,
            "activated": True,
            "start_event": start_event,
            "start_offset": start_offset,
            "end_event": end_event,
            "end_offset": end_offset,
            "night_reduction": night_reduction_result,
        }
        cloud_schedules = [*entry.data.get(CONF_CLOUD_SCHEDULES, []), schedule]
        # Sync entry.data before firing the event, and fire before the full refresh: a
        # listener reacting to the event synchronously must already see the schedule as
        # tracked, and a later refresh failure must not also swallow the only way the user
        # can learn its id (this integration can't rediscover it from getSiteById).
        await _sync_cloud_schedules_cache(hass, entry, cloud_schedules)
        hass.bus.async_fire(f"{DOMAIN}_schedule_created", {"schedule_id": schedule_id})
        await _async_refresh_and_reload(
            hass, entry, http_session, token, cloud_schedules=cloud_schedules, mutation="create"
        )
        return schedule_id


async def async_update_schedule(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    schedule_id: str,
    title: str | None = None,
    scene_steps: list[dict] | None = None,
    start_event: str | None = None,
    start_offset: int | None = None,
    end_event: str | None = None,
    end_offset: int | None = None,
    scheduled_days: list[int] | None = None,
    fade_time: int | None = None,
    activated: bool | None = None,
    night_reduction: dict | None = None,
) -> None:
    """Update a cloud schedule created via async_create_schedule, then refresh + reload.

    updateTimeEvent_V3 is a whole-state call (confirmed via a live capture resending the
    same complete payload on every edit) - fields left unset here are resent unchanged
    from what this integration last knew about the schedule. night_reduction, if given,
    replaces the whole night-reduction block (creating its scene on first use).

    The cache is updated field-by-field as each underlying cloud call actually succeeds
    (not all at once at the end), so a failure partway through - the scene rename lands
    but the trigger update is then rejected, say - doesn't leave entry.data claiming
    nothing happened when part of it did. device_ids specifically is deferred until the
    trigger update itself succeeds (not just the scene edit): it doubles as "what the cloud
    was last told is dirty", and a scene edit alone doesn't tell the cloud anything - only
    the TimeEvent call's dirtyDevices/dirtyRemovedDevices does.

    Held for the whole read-build-send-persist sequence (not just the final cache write):
    two updates to the same schedule racing on the cloud calls could otherwise resend a
    stale whole-state payload and stomp each other, not just corrupt the local cache.
    """
    if (
        title is None
        and scene_steps is None
        and start_event is None
        and start_offset is None
        and end_event is None
        and end_offset is None
        and scheduled_days is None
        and fade_time is None
        and activated is None
        and night_reduction is None
    ):
        # A no-op call would still resend the cached whole state to updateTimeEvent_V3 -
        # harmless if the cache is fresh, but this integration can't tell if the schedule
        # was since edited in the Plejd app, so a no-op "update" would silently overwrite
        # whatever changed there instead of erroring loudly.
        raise HomeAssistantError("update_schedule needs at least one field to change")

    async with _async_get_lock(hass, entry):
        cached = next((s for s in entry.data.get(CONF_CLOUD_SCHEDULES, []) if s["schedule_id"] == schedule_id), None)
        if cached is None:
            raise HomeAssistantError(
                f"Plejd schedule {schedule_id} isn't tracked by this integration "
                "(only schedules created via create_schedule can be updated)"
            )
        effective_start_event = start_event if start_event is not None else cached["start_event"]
        effective_start_offset = start_offset if start_offset is not None else cached["start_offset"]
        effective_end_event = end_event if end_event is not None else cached["end_event"]
        effective_end_offset = end_offset if end_offset is not None else cached["end_offset"]
        _validate_trigger(effective_start_event, effective_start_offset, "start")
        _validate_trigger(effective_end_event, effective_end_offset, "end")
        days = _validate_days(scheduled_days) if scheduled_days is not None else cached["scheduled_days"]
        if scene_steps is not None and not scene_steps:
            raise HomeAssistantError(
                "update_schedule's scene_steps replaces every step - pass at least one, not an empty list"
            )
        if night_reduction is not None:
            _validate_night_reduction(night_reduction)

        http_session, token, site = await _async_login_and_get_site(hass, entry)
        if not any(s.scene_id == cached["scene_id"] for s in site.all_scenes):
            raise HomeAssistantError(f"Plejd scene {cached['scene_id']} not found on this site")
        cached_night_reduction = cached["night_reduction"]
        if cached_night_reduction is not None and not any(
            s.scene_id == cached_night_reduction["scene_id"] for s in site.all_scenes
        ):
            raise HomeAssistantError(f"Plejd scene {cached_night_reduction['scene_id']} not found on this site")

        updated = dict(cached)
        created_scene_ids: list[str] = []
        new_device_ids: list[str] | None = None
        pending_error: HomeAssistantError | None = None
        try:
            if title is not None or scene_steps is not None:
                ok = await async_cloud_update_scene(
                    http_session, token, site.site_id, cached["scene_id"], title=title, scene_steps=scene_steps
                )
                if not ok:
                    raise HomeAssistantError("Plejd cloud rejected the schedule's scene update")
                if title is not None:
                    updated["title"] = title
                if scene_steps is not None:
                    new_device_ids = sorted({s["device_id"] for s in scene_steps})

            night_reduction_result = cached_night_reduction
            if night_reduction is not None:
                effective_title = updated["title"]
                if cached_night_reduction is None:
                    night_scene_id = await async_cloud_create_scene(
                        http_session,
                        token,
                        site.site_id,
                        f"{effective_title} Nattläge",
                        night_reduction["scene_steps"],
                        hidden_from_scene_list=True,
                        settings=_night_reduction_settings(schedule_id),
                    )
                    created_scene_ids.append(night_scene_id)
                else:
                    night_scene_id = cached_night_reduction["scene_id"]
                    ok = await async_cloud_update_scene(
                        http_session, token, site.site_id, night_scene_id, scene_steps=night_reduction["scene_steps"]
                    )
                    if not ok:
                        raise HomeAssistantError("Plejd cloud rejected the night-reduction scene update")
                night_reduction_result = _night_reduction_result(night_scene_id, night_reduction)

            before_devices = _all_device_ids(cached)
            after_primary = set(new_device_ids) if new_device_ids is not None else set(cached.get("device_ids") or [])
            after_devices = after_primary | set((night_reduction_result or {}).get("device_ids") or [])

            needs_time_event_update = (
                scheduled_days is not None
                or fade_time is not None
                or activated is not None
                or start_event is not None
                or start_offset is not None
                or end_event is not None
                or end_offset is not None
                or night_reduction is not None
                or scene_steps is not None
                or after_devices != before_devices
            )
            if needs_time_event_update:
                result = await async_cloud_update_time_event(
                    http_session,
                    token,
                    site.site_id,
                    schedule_id,
                    cached["scene_id"],
                    scheduled_days=days,
                    fade_time=fade_time if fade_time is not None else cached["fade_time"],
                    activated=activated if activated is not None else cached["activated"],
                    start_event=effective_start_event,
                    start_offset=effective_start_offset,
                    end_event=effective_end_event,
                    end_offset=effective_end_offset,
                    dirty_devices=sorted(after_devices),
                    dirty_removed_devices=sorted(before_devices - after_devices),
                    night_reduction=night_reduction_result,
                )
                if result is None:
                    raise HomeAssistantError(f"Plejd cloud rejected the schedule update for {schedule_id}")
                if new_device_ids is not None:
                    updated["device_ids"] = new_device_ids
                updated["night_reduction"] = night_reduction_result
                updated["scheduled_days"] = days
                updated["fade_time"] = fade_time if fade_time is not None else cached["fade_time"]
                updated["activated"] = activated if activated is not None else cached["activated"]
                updated["start_event"] = effective_start_event
                updated["start_offset"] = effective_start_offset
                updated["end_event"] = effective_end_event
                updated["end_offset"] = effective_end_offset
        except PlejdCloudError as err:
            await _async_cleanup_orphaned_scenes(http_session, token, site.site_id, created_scene_ids)
            pending_error = HomeAssistantError(f"Plejd cloud error updating schedule: {err}")
        except HomeAssistantError as err:
            await _async_cleanup_orphaned_scenes(http_session, token, site.site_id, created_scene_ids)
            pending_error = err

        cloud_schedules = [
            updated if s["schedule_id"] == schedule_id else s for s in entry.data.get(CONF_CLOUD_SCHEDULES, [])
        ]
        try:
            await _async_refresh_and_reload(hass, entry, http_session, token, cloud_schedules=cloud_schedules)
        except HomeAssistantError as refresh_err:
            # Don't let a refresh failure replace an already-pending update failure - the
            # update error is what the user actually needs to see and act on.
            if pending_error is not None:
                _LOGGER.warning(
                    "Plejd: also failed to refresh after a schedule update error: %s", refresh_err, exc_info=True
                )
            else:
                pending_error = refresh_err

        if pending_error is not None:
            raise pending_error


async def async_remove_schedule(hass: HomeAssistant, entry: ConfigEntry, *, schedule_id: str) -> None:
    """Remove a cloud schedule created via async_create_schedule, then refresh + reload.

    Mirrors the confirmed capture: a whole-state updateTimeEvent_V3 resend with
    dirtyRemove=true (dirtyDevices/dirtyRemovedDevices both empty, matching the capture),
    then the dedicated removeTimeEvent_V3 call. The capture also showed an updateScene
    resending the on-scene's individual steps with dirtyRemoved=true before emptying them -
    skipped here since this integration only caches device_ids, not the original per-step
    output/state/value needed to resend that call faithfully; going straight from
    dirtyRemove to removeTimeEvent_V3 is the minimal confirmed-safe path (steps are still
    emptied via updateScene right before removeScene, matching the capture, since that
    doesn't need per-step data).

    Once removeTimeEvent_V3 confirms the trigger is gone, the schedule itself no longer
    exists - a failure cleaning up its scene(s) after that point is logged, not raised.
    """
    async with _async_get_lock(hass, entry):
        cached = next((s for s in entry.data.get(CONF_CLOUD_SCHEDULES, []) if s["schedule_id"] == schedule_id), None)
        if cached is None:
            raise HomeAssistantError(
                f"Plejd schedule {schedule_id} isn't tracked by this integration "
                "(only schedules created via create_schedule can be removed)"
            )
        http_session, token, site = await _async_login_and_get_site(hass, entry)

        try:
            result = await async_cloud_update_time_event(
                http_session,
                token,
                site.site_id,
                schedule_id,
                cached["scene_id"],
                scheduled_days=cached["scheduled_days"],
                fade_time=cached["fade_time"],
                activated=cached["activated"],
                start_event=cached["start_event"],
                start_offset=cached["start_offset"],
                end_event=cached["end_event"],
                end_offset=cached["end_offset"],
                dirty_devices=[],
                dirty_removed_devices=[],
                dirty_remove=True,
                night_reduction=cached["night_reduction"],
            )
        except PlejdCloudError as err:
            raise HomeAssistantError(f"Plejd cloud error removing schedule: {err}") from err
        if result is None:
            raise HomeAssistantError(f"Plejd cloud rejected removing schedule {schedule_id}")

        try:
            removed = await async_cloud_remove_time_event(
                http_session, token, site.site_id, schedule_id, device_ids=sorted(_all_device_ids(cached))
            )
        except PlejdCloudError as err:
            raise HomeAssistantError(f"Plejd cloud error removing schedule: {err}") from err
        if not removed:
            raise HomeAssistantError(f"Plejd cloud rejected removing schedule {schedule_id}")

        night_reduction = cached["night_reduction"]
        scene_ids = [cached["scene_id"], *([night_reduction["scene_id"]] if night_reduction else [])]
        for scene_id in scene_ids:
            try:
                await async_cloud_update_scene(http_session, token, site.site_id, scene_id, scene_steps=[])
                if not await async_cloud_remove_scene(http_session, token, site.site_id, scene_id):
                    _LOGGER.warning(
                        "Plejd: cloud rejected removing schedule scene %s after removeTimeEvent_V3", scene_id
                    )
            except PlejdCloudError:
                _LOGGER.warning(
                    "Plejd: could not clean up schedule scene %s after removeTimeEvent_V3", scene_id, exc_info=True
                )

        cloud_schedules = [s for s in entry.data.get(CONF_CLOUD_SCHEDULES, []) if s["schedule_id"] != schedule_id]
        await _async_refresh_and_reload(
            hass, entry, http_session, token, cloud_schedules=cloud_schedules, mutation="remove"
        )
