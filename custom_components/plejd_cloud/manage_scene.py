"""Create, rename/reorder/redefine, and remove Plejd scenes.

The HA-facing orchestration around cloud.py's scene functions: mutate on the
cloud, then refresh and reload the config entry so the change is reflected.
Shared by the create_scene, update_scene, and remove_scene services.

Cloud-only, by design: create_scene/update_scene save a scene's steps to the
Plejd cloud, but do NOT program the per-device mesh configuration a local
CMD_SCENE (0x0021) broadcast relies on to know what a scene index actually
does - that's a separate, still-unconfirmed opcode (docs/reverse_engineering.md
calls it out as 0x0022 but its wire payload has never been captured). A scene
created or edited this way needs the Plejd app opened once (while it can reach
the mesh) to push that configuration before local/BLE-only execution reflects
it; a gateway-connected site may sync sooner via the cloud's own path to the
gateway, but that isn't confirmed either. Execute a scene through this
integration only after confirming (e.g. via the app) that it's been synced to
the physical devices.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import schedule_ws
from .cloud import PlejdAuthError, PlejdCloudError, async_get_site, async_login
from .cloud import async_create_scene as async_cloud_create_scene
from .cloud import async_remove_scene as async_cloud_remove_scene
from .cloud import async_update_scene as async_cloud_update_scene
from .const import (
    CONF_CLOUD_SCHEDULES,
    CONF_SCHEDULES,
    CONF_SITE_ID,
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


def _cloud_schedule_owning_scene(entry: ConfigEntry, scene_id: str) -> dict | None:
    """Find the cloud schedule (if any) whose on-scene or night-reduction scene is scene_id."""
    return next(
        (
            s
            for s in entry.data.get(CONF_CLOUD_SCHEDULES, [])
            if s["scene_id"] == scene_id or (s["night_reduction"] or {}).get("scene_id") == scene_id
        ),
        None,
    )


async def _async_refresh_and_reload(
    hass: HomeAssistant, entry: ConfigEntry, http_session, token, *, mutation: str | None = None
) -> None:
    try:
        fresh_site = await async_get_site(http_session, token, entry.data[CONF_SITE_ID])
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error refreshing site: {err}") from err
    await schedule_ws.async_reload_entry_with_lock(
        hass,
        entry,
        lambda e: schedule_ws.site_data_overlay(e, fresh_site),
        # A create/remove's cloud mutation has already happened and isn't safely retryable
        # by the time this runs - raising on a reload failure here would make the whole
        # service call look failed: retrying a create makes a duplicate scene, retrying a
        # remove just gets "not found". Only the reload itself needs a manual retry (e.g. a
        # later options-flow reload, or the next successful management operation's own
        # reload picking up the same data). A plain update is safe to retry as-is.
        raise_on_reload_failure=mutation is None,
        error_context=f"a scene {mutation}" if mutation else "a scene update",
    )


async def async_create_scene(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    title: str,
    scene_steps: list[dict],
    order: int = 0,
    hidden_from_scene_list: bool = False,
) -> None:
    """Create a new scene, then refresh + reload the config entry."""
    if not scene_steps:
        raise HomeAssistantError("create_scene needs at least one scene step")
    http_session, token, site = await _async_login_and_get_site(hass, entry)
    try:
        await async_cloud_create_scene(
            http_session,
            token,
            site.site_id,
            title,
            scene_steps,
            order=order,
            hidden_from_scene_list=hidden_from_scene_list,
        )
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error creating scene: {err}") from err
    await _async_refresh_and_reload(hass, entry, http_session, token, mutation="create")


async def async_update_scene(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    scene_id: str,
    title: str | None = None,
    order: int | None = None,
    scene_steps: list[dict] | None = None,
    hidden_from_scene_list: bool | None = None,
) -> None:
    """Rename, reorder, and/or replace a scene's steps, then refresh + reload the config entry.

    Refuses to edit a scene that's a cloud schedule's on-scene or night-reduction scene -
    editing it here would change the cloud's scene content without the matching TimeEvent
    dirtyDevices update or CONF_CLOUD_SCHEDULES cache update that update_schedule performs,
    leaving the schedule silently unsynced until it's next edited through update_schedule.
    """
    if title is None and order is None and scene_steps is None and hidden_from_scene_list is None:
        raise HomeAssistantError(
            "update_scene needs at least one of title, order, scene_steps, or hidden_from_scene_list"
        )
    if scene_steps is not None and not scene_steps:
        raise HomeAssistantError(
            "update_scene's scene_steps replaces every step - pass at least one, not an empty list"
        )
    http_session, token, site = await _async_login_and_get_site(hass, entry)
    scene = next((s for s in site.all_scenes if s.scene_id == scene_id), None)
    if scene is None:
        raise HomeAssistantError(f"Plejd scene {scene_id} not found on this site")
    cloud_schedule = _cloud_schedule_owning_scene(entry, scene_id)
    if cloud_schedule is not None:
        raise HomeAssistantError(
            f"Plejd scene '{scene.name}' is managed by cloud schedule '{cloud_schedule['title']}'; "
            "update it via update_schedule instead"
        )
    try:
        ok = await async_cloud_update_scene(
            http_session,
            token,
            site.site_id,
            scene_id,
            title=title,
            order=order,
            scene_steps=scene_steps,
            hidden_from_scene_list=hidden_from_scene_list,
        )
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error updating scene: {err}") from err
    if not ok:
        raise HomeAssistantError(f"Plejd cloud rejected the scene update for {scene_id}")
    await _async_refresh_and_reload(hass, entry, http_session, token)


async def async_remove_scene(hass: HomeAssistant, entry: ConfigEntry, *, scene_id: str) -> None:
    """Remove a scene, then refresh + reload the config entry.

    Refuses to remove a scene that's still referenced by an on-device schedule
    (entry.options[CONF_SCHEDULES], keyed by mesh index) or by a cloud schedule
    (entry.data[CONF_CLOUD_SCHEDULES], as its on-scene or night-reduction scene) -
    in both cases, freeing the id would leave the schedule silently pointing at
    nothing (or a later scene reusing the freed cloud id) instead of erroring.
    """
    http_session, token, site = await _async_login_and_get_site(hass, entry)
    scene = next((s for s in site.all_scenes if s.scene_id == scene_id), None)
    if scene is None:
        raise HomeAssistantError(f"Plejd scene {scene_id} not found on this site")
    indexed = next((s for s in site.scenes if s.scene_id == scene_id), None)
    if indexed is not None:
        referencing = [s["name"] for s in entry.options.get(CONF_SCHEDULES, []) if s.get("scene") == indexed.index]
        if referencing:
            raise HomeAssistantError(
                f"Plejd scene '{scene.name}' is used by schedule(s) {', '.join(referencing)}; "
                "remove or update them first"
            )
    cloud_schedule = _cloud_schedule_owning_scene(entry, scene_id)
    if cloud_schedule is not None:
        raise HomeAssistantError(
            f"Plejd scene '{scene.name}' is used by cloud schedule '{cloud_schedule['title']}'; "
            "remove or update it first"
        )
    try:
        ok = await async_cloud_remove_scene(http_session, token, site.site_id, scene_id)
    except PlejdCloudError as err:
        raise HomeAssistantError(f"Plejd cloud error removing scene: {err}") from err
    if not ok:
        raise HomeAssistantError(f"Plejd cloud rejected removing scene '{scene.name}'")
    await _async_refresh_and_reload(hass, entry, http_session, token, mutation="remove")
