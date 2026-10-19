"""Plejd cloud (Parse Server) client.

Logs in and fetches a site's crypto key + device list, decoded from the app's
`ParseClient` / `ImportSiteAsync`. Login is plain Parse (`/login`); the site comes
from the `getSiteById` cloud function. The JSON shape matches the app's
deserializers; the multi-output address mapping is worth confirming on a live
capture (#2). See docs/reverse_engineering.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from aiohttp import ClientSession

from .const import (
    CATEGORY_LIGHT,
    CATEGORY_NONE,
    DEFAULT_CATEGORY,
    HARDWARE_TYPES,
    HARDWARE_WMS_01,
    PLEJD_FN_COMPATIBLE_DEVICES,
    PLEJD_FN_CREATE_DEVICE,
    PLEJD_FN_CREATE_ROOM,
    PLEJD_FN_CREATE_SCENE,
    PLEJD_FN_CREATE_TIME_EVENT,
    PLEJD_FN_FIRMWARE_BY_HW,
    PLEJD_FN_REMOVE_DEVICE,
    PLEJD_FN_REMOVE_ROOM,
    PLEJD_FN_REMOVE_SCENE,
    PLEJD_FN_REMOVE_TIME_EVENT,
    PLEJD_FN_SET_INPUT,
    PLEJD_FN_SITE_BY_ID,
    PLEJD_FN_SITE_LIST,
    PLEJD_FN_UPDATE_DEVICE,
    PLEJD_FN_UPDATE_ROOM,
    PLEJD_FN_UPDATE_SCENE,
    PLEJD_FN_UPDATE_TIME_EVENT,
    PLEJD_PARSE_APP_ID,
    PLEJD_PARSE_LOGIN,
    PLEJD_PARSE_URL,
    TRAIT_DIMMABLE,
)

# Cloud outputType values are UPPERCASE (validated against the real API).
_OUTPUT_TYPE_CATEGORY = {
    "LIGHT": "light",
    "RELAY": "switch",
    "COVERABLE": "cover",
    "THERMOSTAT": "climate",
}


# Parse's error code for rejected login credentials (returned with a 404). The ONLY signal
# that a login failure is about the credentials rather than the request - see async_login.
PARSE_ERROR_INVALID_LOGIN = 101


class PlejdCloudError(Exception):
    """A Plejd cloud request failed."""


class PlejdAuthError(PlejdCloudError):
    """Login was rejected (bad credentials)."""


@dataclass
class PlejdCloudDevice:
    """One controllable output from the site, mapped toward an HA entity."""

    device_id: str
    name: str
    address: int | None
    output_index: int
    outputs: list[int]
    hardware_id: int
    model: str
    category: str
    dimmable: bool
    traits: int
    room_id: str | None
    object_id: str | None = None  # the output's Parse objectId (deviceParseId), needed to rename it
    output_settings: dict | None = None  # raw cloud outputSettings dict, if returned by the API


@dataclass
class PlejdDeviceFirmware:
    """The installed firmware of one physical device, keyed for an update lookup."""

    version: str | None  # e.g. "6.43.3"
    build_time: int | None  # e.g. 20260324155701; monotonic, used to compare builds
    hardware_id: int
    faceplate_id: str | None


@dataclass
class PlejdCloudInput:
    """A button input (fires press/release events)."""

    device_id: str
    name: str
    address: int
    input_index: int = 0  # which physical input on the device (Ingång 1/2); needed for async_set_input_setting


@dataclass
class PlejdCloudMotion:
    """A motion sensor (WMS-01): broadcasts motion + ambient light."""

    device_id: str
    name: str
    address: int
    room_id: str | None = None  # kept here: a standalone WMS-01 has no output entry to look its room up in


@dataclass
class PlejdCloudScene:
    """A Plejd scene: name + its mesh index (triggered as a broadcast)."""

    scene_id: str
    name: str
    index: int
    # Mesh addresses of the outputs the scene sets; None when unknown (cached before this was parsed).
    output_addresses: list[int] | None = None


@dataclass
class PlejdCloudSceneInfo:
    """Every scene on the site, for scene management (existence checks).

    Unlike PlejdCloudScene, not filtered to scenes with a confirmed mesh index -
    that list is built for scene execution (a broadcast needs a real index) and
    silently excludes a scene missing from sceneIndex, which is exactly the wrong
    lens for "does this scene exist" (same issue as PlejdCloudRoom vs all_rooms).
    """

    scene_id: str
    name: str


@dataclass
class PlejdCloudRoom:
    """A Plejd room: its group mesh address (one 0x0098 controls the whole room) plus member output addresses."""

    room_id: str
    name: str
    address: int  # the room's group mesh address — target of set_group_state_and_level
    member_addresses: list[int]
    dimmable: bool  # True if any member output supports brightness (vs. on/off only)
    dimmable_addresses: list[int]  # subset of member_addresses that support brightness


@dataclass
class PlejdCloudRoomInfo:
    """Every room on the site, for room management (existence + occupancy checks).

    Unlike PlejdCloudRoom, not filtered to light-only groups with a controllable member -
    that list is built for the room-group light entity feature and silently excludes an
    empty room (no group entity is worth creating for it) or a room whose only members
    aren't lights, which is exactly the wrong lens for "does this room exist" / "is it
    safe to delete" (see #114 review).
    """

    room_id: str
    name: str
    has_devices: bool  # any device's roomId matches this room, regardless of category
    address: int | None  # the room's mesh group address, for move_device_to_room (may be absent)


@dataclass
class PlejdCloudAstroEvent:
    """A sunset/sunrise schedule as getSiteById lists it (astroEvents), including ones made in the app."""

    schedule_id: str
    scene_id: str
    scheduled_days: list[int]
    fade_time: int
    activated: bool
    sunset_offset: int
    sunrise_offset: int
    targets: list[tuple[str, int | None]]  # (deviceId, output index); a multi-output device names one output
    night_reduction: dict[str, object] | None


@dataclass
class PlejdCloudSite:
    """A site: its crypto key, mesh key, devices, scenes, and any gateway."""

    site_id: str
    title: str
    crypto_key: bytes
    mesh_key: str  # dash-separated hex ("AA-BB-CC-DD") for SetAccessAddress during commissioning
    devices: list[PlejdCloudDevice]
    inputs: list[PlejdCloudInput]
    motion: list[PlejdCloudMotion]
    scenes: list[PlejdCloudScene]
    gateways: list[str]  # gateway (GWY-01) device ids; empty if none
    resource_set_id: str | None  # for the remote-control WebSocket (Resource-Set-ID)
    # every physical device's installed firmware (outputs, sensors, gateway)
    firmware_by_device: dict[str, PlejdDeviceFirmware] = field(default_factory=dict)
    # every physical device's own mesh address (outputs, sensors, gateway) — distinct from
    # an output's address; this is what NotifyEvents/fault polling must target.
    device_addresses: dict[str, int] = field(default_factory=dict)
    # rooms with a group address, so a whole room is controlled in one 0x0098 command.
    rooms: list[PlejdCloudRoom] = field(default_factory=list)
    # every room on the site, for room management - see PlejdCloudRoomInfo.
    all_rooms: list[PlejdCloudRoomInfo] = field(default_factory=list)
    # every scene on the site, for scene management - see PlejdCloudSceneInfo.
    all_scenes: list[PlejdCloudSceneInfo] = field(default_factory=list)
    # names of collections (devices/inputs/motion/scenes/rooms/gateways) whose raw source
    # field(s) were missing or the wrong type in this response, as opposed to present and
    # genuinely empty (e.g. the user deleted their last scene) - callers that cache and diff
    # this site (coordinator.py's cloud poll) must not treat a malformed field the same as a
    # real, intentional deletion.
    malformed: frozenset[str] = frozenset()


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {"X-Parse-Application-Id": PLEJD_PARSE_APP_ID, "Content-Type": "application/json"}
    if token is not None:
        headers["X-Parse-Session-Token"] = token
    return headers


async def async_login(session: ClientSession, email: str, password: str) -> str:
    """Log in to the Plejd cloud and return a Parse session token."""
    async with session.post(
        PLEJD_PARSE_URL + PLEJD_PARSE_LOGIN,
        headers=_headers(),
        json={"username": email.lower(), "password": password},
    ) as resp:
        data = await resp.json()
        token = data.get("sessionToken") if isinstance(data, dict) else None
        if resp.status != 200 or not token:
            message = str(data.get("error", "login failed")) if isinstance(data, dict) else "login failed"
            # Callers start HA's reauth flow on PlejdAuthError, so ONLY Parse's own
            # credential-rejection code may map to it. Parse answers a bad username/password
            # on this endpoint with code 101 (confirmed by our own capture), so anything
            # without that code did not establish that the stored password is wrong: a 403
            # from a WAF, a 404 during an endpoint rollout, a 200 carrying no sessionToken, a
            # timeout or a 5xx are all request failures. Treating those as auth failures
            # prompts the user to re-enter a password that was never the problem, and in the
            # unattended daily poll that prompt is pure noise.
            code = data.get("code") if isinstance(data, dict) else None
            if code == PARSE_ERROR_INVALID_LOGIN:
                raise PlejdAuthError(message)
            raise PlejdCloudError(message)
        return token


async def _call_function(session: ClientSession, token: str, function: str, body: dict) -> object:
    async with session.post(PLEJD_PARSE_URL + function, headers=_headers(token), json=body) as resp:
        data = await resp.json()
        if resp.status != 200:
            raise PlejdCloudError(str(data.get("error", f"{function} failed")) if isinstance(data, dict) else function)
        return data.get("result")


async def async_get_sites(session: ClientSession, token: str) -> list[dict]:
    """List the user's sites (each with a `siteId` and `title`)."""
    result = await _call_function(session, token, PLEJD_FN_SITE_LIST, {})
    return result if isinstance(result, list) else []


def _as_build_time(value: object) -> int | None:
    """A firmware buildTime (e.g. 20260324155701) as int, or None if absent/garbage."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def async_get_available_firmware(
    session: ClientSession, token: str, hardware_id: int, faceplate_id: str | None
) -> tuple[str, int] | None:
    """Latest published firmware (version, buildTime) for a hardware/faceplate, or None when up to date."""
    body: dict[str, str] = {"hardwareId": str(hardware_id)}
    if faceplate_id is not None:
        body["faceplateId"] = faceplate_id
    result = await _call_function(session, token, PLEJD_FN_FIRMWARE_BY_HW, body)
    best: tuple[str, int] | None = None
    for item in result if isinstance(result, list) else []:
        if not isinstance(item, dict):
            continue
        version = item.get("version")
        build_time = _as_build_time(item.get("buildTime"))
        if not isinstance(version, str) or build_time is None:
            continue
        if best is None or build_time > best[1]:
            best = (version, build_time)
    return best


async def async_get_needed_output_count(
    session: ClientSession, token: str, hardware_id: str, firmware_build_time: int, faceplate_id: str = "0"
) -> int:
    """How many output addresses createPlejdDevice_V2's deviceInfo needs for this hardware.

    Wall controllers (WPH-01, WRT-01, CTR-01, ...) need zero; multi-output hardware
    (DIM-02, REL-02, OUT-02, ...) need more than one. Falls back to 1 (the common
    single-output case) if the cloud doesn't confirm a compatible entry.
    """
    body = {
        "devices": [
            {
                "buildTime": firmware_build_time,
                "firmwareNumber": None,
                "hardwareId": hardware_id,
                "faceplateId": faceplate_id,
                "variant": None,
            }
        ],
        "appVersion": "7.3.0.1.65",
        "deviceType": "android",
    }
    result = await _call_function(session, token, PLEJD_FN_COMPATIBLE_DEVICES, body)
    compatible = result.get("compatible") if isinstance(result, dict) else None
    for entry in compatible if isinstance(compatible, list) else []:
        info = entry.get(hardware_id) if isinstance(entry, dict) else None
        if isinstance(info, dict) and isinstance(info.get("neededAddresses"), int):
            return info["neededAddresses"]
    return 1


async def async_set_device_title(
    session: ClientSession, token: str, site_id: str, device_id: str, device_parse_id: str, title: str
) -> bool:
    """Rename a device in the Plejd cloud (mirrors to the Plejd app). Returns the cloud's ok flag."""
    body = {"siteId": site_id, "deviceId": device_id, "deviceParseId": device_parse_id, "title": title}
    result = await _call_function(session, token, PLEJD_FN_UPDATE_DEVICE, body)
    return result is True


async def async_remove_device(session: ClientSession, token: str, site_id: str, device_id: str) -> bool:
    """Remove a device from the site."""
    result = await _call_function(session, token, PLEJD_FN_REMOVE_DEVICE, {"siteId": site_id, "deviceId": device_id})
    return result is True


async def async_get_site_raw(session: ClientSession, token: str, site_id: str) -> object:
    """getSiteById's unparsed response, for diagnostics."""
    return await _call_function(session, token, PLEJD_FN_SITE_BY_ID, {"siteId": site_id})


async def async_get_site(session: ClientSession, token: str, site_id: str) -> PlejdCloudSite:
    """Fetch one site (crypto key + devices) by id."""
    result = await async_get_site_raw(session, token, site_id)
    if isinstance(result, list):
        if not result:
            raise PlejdCloudError("site not found")
        result = result[0]
    if not isinstance(result, dict):
        raise PlejdCloudError("malformed site response")
    return parse_site(result)


@dataclass
class NewDeviceInfo:
    """Per-output metadata for a new device being registered to a site."""

    title: str
    output_index: int
    room_id: str | None = None
    # Powerable | Groupable (DeviceTrait bitmask) - the baseline the app sends at
    # creation time regardless of hardware type; the cloud fills in the rest
    # (e.g. Dimmable) from its own hardware config once the device is registered.
    traits: int = 9
    hidden_from_room_list: bool = False


@dataclass
class NewDeviceAddresses:
    """Addresses assigned to a newly registered device by the cloud."""

    device_address: int | None  # mesh node address for the device
    output_addresses: dict[int, int]  # output_index -> mesh address


async def async_create_device(
    session: ClientSession,
    token: str,
    site_id: str,
    device_id: str,
    hardware_id: str,
    firmware_build_time: int,
    device_infos: list[NewDeviceInfo] | None = None,
    faceplate_id: str = "0",
    variant: str | None = None,
    installation_location: str = "",
) -> NewDeviceAddresses:
    """Register a new device to a site via createPlejdDevice_V2.

    Returns the mesh addresses assigned by the cloud. Call this BEFORE BLE
    commissioning — the returned device_address is the node index used in SetNodeIndex.
    device_id is the BLE MAC address as a hex string (no separators, lowercase).
    """
    body: dict = {
        "siteId": site_id,
        "deviceId": device_id,
        "hardwareId": hardware_id,
        "firmwareBuildTime": firmware_build_time,
        # The app always sends these two keys - a captured real request had
        # variant=null (fine) but installationLocation as non-null free text;
        # omitting either key entirely gets a generic "Invalid request." rejection.
        "variant": variant,
        "installationLocation": installation_location,
        "faceplateId": faceplate_id,
    }
    if device_infos:
        body["deviceInfo"] = [
            {
                "title": di.title,
                "index": di.output_index,
                "roomId": di.room_id,
                "traits": di.traits,
                "hiddenFromRoomList": di.hidden_from_room_list,
            }
            for di in device_infos
        ]
    result = await _call_function(session, token, PLEJD_FN_CREATE_DEVICE, body)
    return _parse_new_device_addresses(result)


def _parse_new_device_addresses(result: object) -> NewDeviceAddresses:
    """Parse a createPlejdDevice_V2 response into mesh addresses."""
    if not isinstance(result, dict):
        return NewDeviceAddresses(device_address=None, output_addresses={})
    device_addr_raw = result.get("deviceAddress")
    device_address = int(device_addr_raw) if device_addr_raw is not None else None
    output_address_map = result.get("outputAddress") or {}
    output_addresses: dict[int, int] = {}
    for idx_str, addr in output_address_map.items():
        try:
            output_addresses[int(idx_str)] = int(addr)
        except (ValueError, TypeError):
            continue  # malformed entry from the cloud - skip it, not fatal
    return NewDeviceAddresses(device_address=device_address, output_addresses=output_addresses)


async def async_create_room(
    session: ClientSession,
    token: str,
    site_id: str,
    title: str,
    category: str = "Other",
) -> str:
    """Create a new room and return its ID (a UUID we generate and pass in)."""
    room_id = str(uuid.uuid4())
    await _call_function(
        session,
        token,
        PLEJD_FN_CREATE_ROOM,
        {"siteId": site_id, "roomId": room_id, "title": title, "category": category, "imageHash": 0},
    )
    return room_id


async def async_update_room(
    session: ClientSession,
    token: str,
    site_id: str,
    room_id: str,
    *,
    title: str | None = None,
    order: int | None = None,
    category: str | None = None,
) -> bool:
    """Rename, reorder, and/or recategorize a room. Only the supplied fields are sent."""
    params: dict[str, object] = {"siteId": site_id, "roomId": room_id}
    if title is not None:
        params["title"] = title
    if order is not None:
        params["order"] = order
    if category is not None:
        params["category"] = category
    result = await _call_function(session, token, PLEJD_FN_UPDATE_ROOM, params)
    return result is True


async def async_remove_room(session: ClientSession, token: str, site_id: str, room_id: str) -> bool:
    """Remove a room. The cloud rejects this if the room still has devices in it."""
    result = await _call_function(session, token, PLEJD_FN_REMOVE_ROOM, {"siteId": site_id, "roomId": room_id})
    return result is True


def _scene_step_to_payload(step: dict) -> dict[str, object]:
    """One scene step, in the app's own createScene/updateScene sceneSteps wire shape.

    A step passed to a create/update call is normally freshly pushed, so dirty/dirtyRemoved
    default to True/False - they track pending local edits in the app's own offline cache,
    which this integration has no equivalent of. A caller marking a step as no-longer-needed
    (see async_remove_schedule's confirmed dirtyRemoved:true resend before removeTimeEvent_V3)
    can override either via the step dict's own "dirty"/"dirty_removed" keys.
    """
    payload: dict[str, object] = {
        "deviceId": step["device_id"],
        "output": step["output"],
        "dirty": step.get("dirty", True),
        "dirtyRemoved": step.get("dirty_removed", False),
        "state": step["state"],
        "value": step["value"],
    }
    if "color_temperature" in step:
        payload["colorTemperature"] = step["color_temperature"]
    if "coverable_tilt" in step:
        payload["coverableTilt"] = step["coverable_tilt"]
    if "climate_boost_time" in step:
        payload["climateBoostTime"] = step["climate_boost_time"]
    return payload


async def async_create_scene(
    session: ClientSession,
    token: str,
    site_id: str,
    title: str,
    scene_steps: list[dict],
    *,
    order: int = 0,
    hidden_from_scene_list: bool = False,
    settings: str = "",
) -> str:
    """Create a new scene and return its ID (a UUID we generate and pass in)."""
    scene_id = str(uuid.uuid4())
    params = {
        "siteId": site_id,
        "sceneId": scene_id,
        "title": title,
        "order": order,
        "sceneSteps": [_scene_step_to_payload(s) for s in scene_steps],
        "hiddenFromSceneList": hidden_from_scene_list,
        "settings": settings,
    }
    await _call_function(session, token, PLEJD_FN_CREATE_SCENE, params)
    return scene_id


async def async_update_scene(
    session: ClientSession,
    token: str,
    site_id: str,
    scene_id: str,
    *,
    title: str | None = None,
    order: int | None = None,
    scene_steps: list[dict] | None = None,
    hidden_from_scene_list: bool | None = None,
    settings: str | None = None,
) -> bool:
    """Rename, reorder, and/or replace a scene's steps. Only the supplied fields are sent."""
    params: dict[str, object] = {"siteId": site_id, "sceneId": scene_id}
    if title is not None:
        params["title"] = title
    if order is not None:
        params["order"] = order
    if scene_steps is not None:
        params["sceneSteps"] = [_scene_step_to_payload(s) for s in scene_steps]
    if hidden_from_scene_list is not None:
        params["hiddenFromSceneList"] = hidden_from_scene_list
    if settings is not None:
        params["settings"] = settings
    result = await _call_function(session, token, PLEJD_FN_UPDATE_SCENE, params)
    return result is True


async def async_remove_scene(session: ClientSession, token: str, site_id: str, scene_id: str) -> bool:
    """Remove a scene."""
    result = await _call_function(session, token, PLEJD_FN_REMOVE_SCENE, {"siteId": site_id, "sceneId": scene_id})
    return result is True


def _time_event_common_payload(
    *,
    scheduled_days: list[int],
    fade_time: int,
    activated: bool,
    dirty_devices: list[str] | None,
    dirty_removed_devices: list[str] | None,
    dirty_remove: bool,
    start_event: str,
    start_offset: int,
    end_event: str,
    end_offset: int,
    night_reduction: dict | None,
) -> dict[str, object]:
    """Fields shared by createTimeEvent_V3 and updateTimeEvent_V3.

    version/pauseStart/pauseEnd vs nightReduction: two live captures each showed one
    internally-consistent combination - version 2 + pauseStart/pauseEnd ("00:00" placeholders)
    for a schedule with no night reduction, version 3 + a nightReduction object (no
    pauseStart/pauseEnd) for one that has it. Both individual shapes are confirmed-real
    successful calls; which field controls the switch is inferred, not independently
    re-verified in a single session - see #121.
    """
    payload: dict[str, object] = {
        "scheduledDays": scheduled_days,
        "fadeTime": fade_time,
        "activated": activated,
        "dirtyDevices": dirty_devices or [],
        "dirtyRemovedDevices": dirty_removed_devices or [],
        "dirtyRemove": dirty_remove,
        "mode": "astro",
        "start": {"event": start_event, "offset": start_offset},
        "end": {"event": end_event, "offset": end_offset},
    }
    if night_reduction is not None:
        payload["version"] = 3
        night_reduction_payload: dict[str, object] = {
            "startTime": night_reduction["start_time"],
            "endTime": night_reduction["end_time"],
            "sceneId": night_reduction["scene_id"],
        }
        if (
            night_reduction.get("weekend_start_time") is not None
            and night_reduction.get("weekend_end_time") is not None
        ):
            night_reduction_payload["weekendDeviation"] = {
                "startTime": night_reduction["weekend_start_time"],
                "endTime": night_reduction["weekend_end_time"],
            }
        payload["nightReduction"] = night_reduction_payload
    else:
        payload["version"] = 2
        payload["pauseStart"] = "00:00"
        payload["pauseEnd"] = "00:00"
    return payload


async def async_create_time_event(
    session: ClientSession,
    token: str,
    site_id: str,
    scene_id: str,
    *,
    scheduled_days: list[int],
    fade_time: int,
    activated: bool,
    start_event: str,
    start_offset: int,
    end_event: str,
    end_offset: int,
    dirty_devices: list[str] | None = None,
    night_reduction: dict | None = None,
) -> dict | None:
    """Create a cloud schedule's astro trigger (createTimeEvent_V3).

    Confirmed via a live capture to be a SEPARATE function from async_update_time_event -
    timeEventId is always null here (the server assigns the real id, returned as eventId);
    targetDevices is only present on create. Returns the cloud's {targetDevices, eventId}
    result, or None if rejected or missing a usable eventId (callers key everything else -
    updates, removal - off that id, so a malformed response must not be mistaken for success).
    """
    device_ids = dirty_devices or []
    params: dict[str, object] = {
        "siteId": site_id,
        "timeEventId": None,
        "sceneId": scene_id,
        **_time_event_common_payload(
            scheduled_days=scheduled_days,
            fade_time=fade_time,
            activated=activated,
            dirty_devices=device_ids,
            dirty_removed_devices=None,
            dirty_remove=False,
            start_event=start_event,
            start_offset=start_offset,
            end_event=end_event,
            end_offset=end_offset,
            night_reduction=night_reduction,
        ),
        "targetDevices": device_ids,
    }
    result = await _call_function(session, token, PLEJD_FN_CREATE_TIME_EVENT, params)
    if not isinstance(result, dict) or not isinstance(result.get("eventId"), str) or not result["eventId"]:
        return None
    return result


async def async_update_time_event(
    session: ClientSession,
    token: str,
    site_id: str,
    schedule_id: str,
    scene_id: str,
    *,
    scheduled_days: list[int],
    fade_time: int,
    activated: bool,
    start_event: str,
    start_offset: int,
    end_event: str,
    end_offset: int,
    dirty_devices: list[str] | None = None,
    dirty_removed_devices: list[str] | None = None,
    dirty_remove: bool = False,
    night_reduction: dict | None = None,
) -> dict | None:
    """Update (or, with dirty_remove=True, mark for removal) a cloud schedule's astro trigger.

    Confirmed to be a whole-state call: the caller must resend every field on each edit,
    not just what changed. night_reduction, if given: {"scene_id", "start_time", "end_time",
    "weekend_start_time"?, "weekend_end_time"?}. dirty_remove=True is the confirmed first
    step of removing a schedule (see async_remove_schedule), sent before the dedicated
    removeTimeEvent_V3 call. Returns the cloud's {targetDevices, eventId} result, or None
    if rejected, missing an eventId, or the returned eventId doesn't match schedule_id -
    a mismatched id would mean the cloud updated (or created) a different TimeEvent than
    the one asked for, which callers must not treat as their own update having landed.
    """
    params: dict[str, object] = {
        "siteId": site_id,
        "timeEventId": schedule_id,
        "sceneId": scene_id,
        **_time_event_common_payload(
            scheduled_days=scheduled_days,
            fade_time=fade_time,
            activated=activated,
            dirty_devices=dirty_devices,
            dirty_removed_devices=dirty_removed_devices,
            dirty_remove=dirty_remove,
            start_event=start_event,
            start_offset=start_offset,
            end_event=end_event,
            end_offset=end_offset,
            night_reduction=night_reduction,
        ),
    }
    result = await _call_function(session, token, PLEJD_FN_UPDATE_TIME_EVENT, params)
    if not isinstance(result, dict) or result.get("eventId") != schedule_id:
        return None
    return result


async def async_remove_time_event(
    session: ClientSession, token: str, site_id: str, schedule_id: str, *, device_ids: list[str]
) -> bool:
    """Remove a cloud schedule's TimeEvent (removeTimeEvent_V3), confirmed via a live capture."""
    result = await _call_function(
        session,
        token,
        PLEJD_FN_REMOVE_TIME_EVENT,
        {"siteId": site_id, "timeEventId": schedule_id, "mode": "astro", "deviceIds": device_ids},
    )
    return result is True


async def async_set_input_setting(
    session: ClientSession,
    token: str,
    site_id: str,
    device_id: str,
    input_index: int,
    button_type: str,
) -> None:
    """Set the button type for one input of a device (e.g. 'Toggle' or 'PushButton')."""
    await _call_function(
        session,
        token,
        PLEJD_FN_SET_INPUT,
        {
            "siteId": site_id,
            "deviceId": device_id,
            "input": input_index,
            "buttonType": button_type,
            "dimSpeed": 0,
            "singleClick": None,
            "doubleClick": None,
            "doubleSidedDirectionButton": False,
            "motionSensorData": None,
            "positiveEdgeScene": None,
            "negativeEdgeScene": None,
        },
    )


def _plain_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_astro_events(site: dict) -> list[PlejdCloudAstroEvent]:
    """getSiteById's astroEvents; a record being removed (dirtyRemove) or missing its ids is skipped."""
    events = []
    raw = site.get("astroEvents")
    for ev in raw if isinstance(raw, list) else []:
        if not isinstance(ev, dict) or ev.get("dirtyRemove") is True:
            continue
        schedule_id, scene_id = ev.get("astroEventId"), ev.get("sceneId")
        if not isinstance(schedule_id, str) or not isinstance(scene_id, str):
            continue
        days = ev.get("scheduledDays")
        targets = ev.get("targetDevices")
        night = ev.get("nightReduction")
        weekend = night.get("weekendDeviation") if isinstance(night, dict) else None
        events.append(
            PlejdCloudAstroEvent(
                schedule_id=schedule_id,
                scene_id=scene_id,
                scheduled_days=sorted(
                    {d for d in days if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6}
                )
                if isinstance(days, list)
                else [],
                fade_time=_plain_int(ev.get("fadeTime")),
                activated=ev.get("activated") is not False
                and not (isinstance(ev.get("activated"), (int, str)) and not ev.get("activated")),
                sunset_offset=_plain_int(ev.get("sunsetOffset")),
                sunrise_offset=_plain_int(ev.get("sunriseOffset")),
                targets=sorted(
                    {
                        (
                            t["deviceId"],
                            t["index"]
                            if isinstance(t.get("index"), int) and not isinstance(t["index"], bool)
                            else None,
                        )
                        for t in targets
                        if isinstance(t, dict) and isinstance(t.get("deviceId"), str)
                    },
                    key=lambda x: (x[0], x[1] if x[1] is not None else -1),
                )
                if isinstance(targets, list)
                else [],
                night_reduction={
                    "start_time": night.get("startTime"),
                    "end_time": night.get("endTime"),
                    "scene_id": night.get("sceneId"),
                    "weekend_start_time": weekend.get("startTime") if isinstance(weekend, dict) else None,
                    "weekend_end_time": weekend.get("endTime") if isinstance(weekend, dict) else None,
                }
                if isinstance(night, dict)
                else None,
            )
        )
    events.sort(key=lambda e: e.schedule_id)
    return events


def parse_site(site: dict) -> PlejdCloudSite:
    """Parse a getSiteById result into a PlejdCloudSite."""
    mesh = site.get("plejdMesh") or {}
    key_hex = mesh.get("cryptoKey")
    if not key_hex:
        raise PlejdCloudError("site has no cryptoKey")
    # cryptoKey is dash-separated hex ("XX-XX-..", 16 bytes). The BLE auth path requires
    # exactly 16 bytes; accepting any other length here would let a malformed cloud
    # response silently replace a known-good cached key with one that can never
    # authenticate, stranding the mesh until a human notices and re-triggers setup.
    crypto_key = bytes.fromhex(key_hex.replace("-", ""))
    if len(crypto_key) != 16:
        raise PlejdCloudError("site cryptoKey is not 16 bytes")
    # meshKey is a dash-separated hex string used as the BLE AccessAddress during commissioning.
    mesh_key: str = mesh.get("meshKey") or ""

    # A field's raw source key being absent or the wrong type means this response is
    # truncated/malformed, not that the user emptied that collection - the difference
    # matters to callers (coordinator.py's cloud poll) that must not treat "cloud sent
    # garbage" the same as "cloud correctly reports zero scenes now". An empty list/dict
    # of the right type is a genuine, trustworthy "there are none".
    # Each raw field is normalized to an empty container of the right type as it is checked,
    # and everything below parses those locals rather than re-reading site[...]: a wrong-typed
    # but non-empty value (e.g. devices as an object, inputAddress as a list) would otherwise
    # blow up mid-parse with AttributeError/TypeError before `malformed` could reach the
    # caller at all, turning a skippable bad response into an unhandled exception.
    # A collection is also malformed when a companion map it is parsed against is the wrong
    # type: scenes with a wrong-typed sceneIndex yields no executable scenes, and rooms with a
    # wrong-typed outputGroups yields no room entities - both parse "successfully" to an empty
    # list that would look like a genuine deletion and destroy those entities on the next poll.
    #
    # Whether an ABSENT key is malformed depends on the key, because omission means two very
    # different things depending on which collection it is:
    #
    #  - OPTIONAL (the default): omission is how the API says the site has none of them. A
    #    BLE-only site has no `gateways`; a site with no rooms has no
    #    `rooms`/`roomAddress`/`outputGroups`; a site of only lights/relays has no
    #    `inputAddress`; a site with no scenes has no `scenes`/`sceneIndex`. Flagging those
    #    would refuse setup, refuse reconfigure and stop the poll syncing for such a site -
    #    a far worse failure than the one the flag exists to prevent.
    #  - REQUIRED (`required=True`): the site cannot meaningfully exist without it, so
    #    omission means the response is truncated, not that the collection is empty.
    #    `devices` is the clearest case; `plejdDevices`/`deviceAddress` are required only
    #    once `devices` is non-empty, since those devices must have hardware entries and
    #    mesh addresses. Normalizing THOSE to empty is what would silently wipe every
    #    entity, which is exactly what the flag is for.
    malformed: set[str] = set()

    def _checked_list(key: str, *labels: str, id_field: str | None = None, required: bool = False) -> list:
        raw = site.get(key)
        if raw is None:
            if required:
                malformed.update(labels)
            return []  # see the required/optional note above
        if not isinstance(raw, list):
            malformed.update(labels)
            return []
        if id_field is not None and any(
            not isinstance(item, dict) or not isinstance(item.get(id_field), str) for item in raw
        ):
            # A stray non-object element, or a record whose id is missing or not a string,
            # means this array is itself truncated/corrupt. The loops below still skip such
            # an element so the parse survives, but that silently drops a real
            # device/scene/room from the snapshot - which a caching, diffing caller (the
            # cloud poll) would read as a deliberate deletion and persist, losing the entity.
            malformed.update(labels)
        return raw

    def _checked_dict(key: str, *labels: str, required: bool = False) -> dict:
        raw = site.get(key)
        if raw is None:
            if required:
                malformed.update(labels)
            return {}  # see the required/optional note above
        if isinstance(raw, dict):
            return raw
        malformed.update(labels)
        return {}

    # devices is required: a site without it is a truncated response, not an empty site, and
    # normalizing it away is what would wipe every entity the integration has.
    raw_devices = _checked_list("devices", "devices", id_field="deviceId", required=True)
    has_devices = bool(raw_devices)
    input_address = _checked_dict("inputAddress", "inputs")
    # plejdDevices feeds both the motion sensors and every device's hardware id/model, so a
    # corrupt element there makes both collections untrustworthy. Required once there are
    # devices at all, since each of those must have a hardware entry here.
    plejd_devices = _checked_list("plejdDevices", "motion", "devices", id_field="deviceId", required=has_devices)
    raw_scenes = _checked_list("scenes", "scenes", id_field="sceneId")
    raw_scene_steps = site.get("sceneSteps")
    scene_index = _checked_dict("sceneIndex", "scenes")
    # These two carry every mesh address there is: outputAddress is what control commands
    # target, deviceAddress is the physical-device fallback (and what fault polling and the
    # motion sensors use). Losing either silently yields devices with address=None - parsed
    # "successfully" into entities that can never be commanded - so they count as malformed
    # device (and, for deviceAddress, motion) data rather than an empty-but-valid map.
    # outputAddress stays optional: a single-output device legitimately falls back to its
    # deviceAddress, so its absence is survivable rather than proof of truncation.
    output_address = _checked_dict("outputAddress", "devices")
    device_address = _checked_dict("deviceAddress", "devices", "motion", required=has_devices)
    raw_rooms = _checked_list("rooms", "rooms", id_field="roomId")
    room_address = _checked_dict("roomAddress", "rooms")
    output_groups = _checked_dict("outputGroups", "rooms")
    gateways_raw = _checked_list("gateways", "gateways", id_field="deviceId")

    hardware_by_id = {d.get("deviceId"): d for d in plejd_devices if isinstance(d, dict)}

    devices: list[PlejdCloudDevice] = []
    seen_outputs: dict[str, int] = {}  # deviceId -> next output index (devices[] is one entry per output)
    for info in raw_devices:
        if not isinstance(info, dict):
            continue  # untrusted cloud data: a non-object entry in devices[]
        device_id = info.get("deviceId")
        if not isinstance(device_id, str):
            continue  # untrusted cloud data: a non-string id would also break the sort below
        output_index = seen_outputs.get(device_id, 0)
        seen_outputs[device_id] = output_index + 1
        hardware = hardware_by_id.get(device_id, {})
        hardware_id = int(hardware.get("hardwareId") or 0)
        model = HARDWARE_TYPES.get(hardware_id, "Unknown")
        output_type = (info.get("outputType") or "UNKNOWN").upper()
        category = _OUTPUT_TYPE_CATEGORY.get(output_type) or DEFAULT_CATEGORY.get(hardware_id, CATEGORY_NONE)
        out_map = output_address.get(device_id) or {}
        # Sorted, not in outputAddress's own key order: this list is only ever used as a set
        # of the device's output addresses (a multi-output check), and leaving it in the
        # cloud's arbitrary JSON object order would make an unchanged site compare unequal
        # to its cached snapshot and reload the integration for nothing (coordinator.py's
        # cloud poll diffs entry.data against this).
        outputs = sorted(int(a) for a in out_map.values())
        phys_address = device_address.get(device_id)
        # Control targets the output's own mesh address; fall back to the device address.
        address = out_map.get(str(output_index), phys_address)
        # Prefer the per-output Dimmable trait; fall back to category when the cloud
        # omits traits (a light-category output can still be on/off only).
        traits = int(info.get("traits") or 0)
        dimmable = bool(traits & TRAIT_DIMMABLE) if "traits" in info else category == CATEGORY_LIGHT
        raw_settings = info.get("outputSettings")
        devices.append(
            PlejdCloudDevice(
                device_id=device_id,
                name=info.get("title") or model,
                address=int(address) if address is not None else None,
                output_index=output_index,
                outputs=outputs,
                hardware_id=hardware_id,
                model=model,
                category=category,
                dimmable=dimmable,
                traits=traits,
                room_id=info.get("roomId"),
                object_id=info.get("objectId"),
                output_settings=raw_settings if isinstance(raw_settings, dict) else None,
            )
        )

    # Outcome check, deliberately instead of validating each source map's coverage separately:
    # a device only works if it resolved to BOTH a mesh address and a hardware record, and it
    # can fail to for many reasons (deviceAddress truncated, outputAddress missing this output,
    # plejdDevices missing this record, an out-of-range address...). Asserting the result
    # covers every one of those at once, including shapes not enumerated here - and a device
    # that came out address-less or hardware-unknown is exactly what the light/switch platforms
    # silently drop and what the settings entities lose, so it must not be cached over a
    # working snapshot.
    if any(d.address is None or d.device_id not in hardware_by_id for d in devices):
        malformed.add("devices")
    name_by_device = {info.get("deviceId"): info.get("title") for info in raw_devices if isinstance(info, dict)}
    # Several inputs can share one mesh address (the same physical button reported under more
    # than one index), and only the first is kept. Collect every candidate and order them
    # BEFORE deduplicating, so the winner is the lowest (device_id, input_index) rather than
    # whichever the cloud's arbitrary JSON key order happened to put first: otherwise the
    # cached input_index flips between polls, which both reloads an unchanged integration and
    # makes a later per-input settings write target a different physical input.
    input_candidates: list[tuple[str, int, int]] = []
    for device_id, idx_map in input_address.items():
        # Corrupt entries are skipped so the parse survives, but skipping silently drops real
        # buttons - which the diffing poll would read as a deletion and persist, removing
        # those event entities. Flag the collection so the snapshot is rejected instead.
        if not isinstance(device_id, str):
            # Mixed key types would also make the candidate sort below compare int with str
            # and raise, aborting the whole parse instead of returning a rejectable site.
            malformed.add("inputs")
            continue
        if not isinstance(idx_map, dict):
            malformed.add("inputs")
            continue  # untrusted cloud data: a malformed per-device input map
        for idx_str, addr in idx_map.items():
            try:
                input_candidates.append((device_id, int(idx_str), int(addr)))
            except (ValueError, TypeError):
                malformed.add("inputs")  # a non-numeric index/address is the same loss
    input_candidates.sort()
    inputs: list[PlejdCloudInput] = []
    seen_addr: set[int] = set()
    for device_id, input_index, address in input_candidates:
        if address in seen_addr:
            continue
        seen_addr.add(address)
        inputs.append(
            PlejdCloudInput(
                device_id=device_id,
                name=name_by_device.get(device_id) or "Button",
                address=address,
                input_index=input_index,
            )
        )

    motion: list[PlejdCloudMotion] = []
    for phys in plejd_devices:
        if not isinstance(phys, dict):
            continue  # untrusted cloud data: a non-object entry in plejdDevices[]
        if int(phys.get("hardwareId") or 0) == HARDWARE_WMS_01:
            device_id = phys.get("deviceId")
            if not isinstance(device_id, str):
                continue  # untrusted cloud data: a non-string id would also break the sort below
            addr = device_address.get(device_id)
            if addr is not None:
                room = next(
                    (
                        r
                        for i in raw_devices
                        if isinstance(i, dict)
                        and i.get("deviceId") == device_id
                        and isinstance(r := i.get("roomId"), str)
                    ),
                    phys.get("roomId"),
                )
                motion.append(
                    PlejdCloudMotion(
                        device_id=device_id,
                        name=name_by_device.get(device_id) or "Motion sensor",
                        address=int(addr),
                        room_id=room if isinstance(room, str) else None,
                    )
                )

    # Installed firmware for every physical device (outputs, sensors, gateway alike),
    # so the update platform can cover them all — not just controllable outputs.
    # Gateways live in gateways[], not plejdDevices, and carry the firmware dict under
    # `firmwareObject` (their `firmware` is a bare buildTime int), so prefer that.
    firmware_by_device: dict[str, PlejdDeviceFirmware] = {}
    for phys in plejd_devices + gateways_raw:
        if not isinstance(phys, dict):
            continue  # untrusted cloud data: a non-object entry in plejdDevices[]/gateways[]
        device_id = phys.get("deviceId")
        if device_id is None:
            continue
        firmware = next((v for v in (phys.get("firmwareObject"), phys.get("firmware")) if isinstance(v, dict)), {})
        faceplate = phys.get("faceplateId")
        firmware_by_device[device_id] = PlejdDeviceFirmware(
            version=firmware.get("version") if isinstance(firmware.get("version"), str) else None,
            build_time=_as_build_time(firmware.get("buildTime")),
            hardware_id=int(phys.get("hardwareId") or 0),
            faceplate_id=str(faceplate) if faceplate is not None else None,
        )

    # sceneSteps (top level, one per scene output) say which outputs each scene sets. Membership is left
    # unknown (None) wherever it can't be trusted, so a scene is never wrongly ruled out as a change's origin:
    # for every scene when the list is missing or has a record we can't attribute to a scene, and for one
    # scene when a step is still dirty (edited in the cloud, not yet synced to the mesh by the app) or names
    # an output we can't map.
    scene_outputs: dict[str, list[int]] | None = None
    unknown_scenes: set[str] = set()
    if isinstance(raw_scene_steps, list):
        address_by_output = {(d.device_id, d.output_index): d.address for d in devices if d.address is not None}
        scene_outputs = {}
        for step in raw_scene_steps:
            if not isinstance(step, dict) or not isinstance(step.get("sceneId"), str):
                scene_outputs = None
                break
            dirty, removed = step.get("dirty", False), step.get("dirtyRemoved", False)
            device_id, output = step.get("deviceId"), step.get("output")
            if (
                dirty is not False
                or not isinstance(removed, bool)
                or not isinstance(device_id, str)
                or type(output) is not int
            ):
                unknown_scenes.add(step["sceneId"])  # pending in the app, or fields we can't trust
            elif not removed:  # a removal the mesh already has drops out
                if (address := address_by_output.get((device_id, output))) is None:
                    unknown_scenes.add(step["sceneId"])
                else:
                    scene_outputs.setdefault(step["sceneId"], []).append(address)
    scenes = [
        PlejdCloudScene(
            scene_id=sc["sceneId"],
            name=sc.get("title") or sc["sceneId"],
            index=int(idx),
            output_addresses=None
            if scene_outputs is None or sc["sceneId"] in unknown_scenes
            else sorted(set(scene_outputs.get(sc["sceneId"], []))),
        )
        for sc in raw_scenes
        if isinstance(sc, dict)
        and isinstance(sc.get("sceneId"), str)
        and (idx := scene_index.get(sc.get("sceneId"))) is not None
    ]
    all_scenes = [
        PlejdCloudSceneInfo(scene_id=sc["sceneId"], name=sc.get("title") or sc["sceneId"])
        for sc in raw_scenes
        if isinstance(sc, dict) and isinstance(sc.get("sceneId"), str)
    ]

    # A gateway (GWY-01) has no controllable output, so it is absent from devices[];
    # it lives only in gateways[]. Its resourceSetId authorises the remote WebSocket.
    gateway_objs = gateways_raw
    gateways = [g["deviceId"] for g in gateway_objs if isinstance(g, dict) and isinstance(g.get("deviceId"), str)]
    # The gateway's own resourceSetId is authoritative. Only fall back to a site
    # resourceSet when there's exactly one (otherwise we can't tell which grants access).
    resource_sets = site.get("resourceSets") or []

    def _grant(value: object) -> str | None:
        # Must be a non-empty string: it goes straight into the WebSocket's Resource-Set-ID
        # header, so a truthy non-string (e.g. 123) would be persisted over a working cached
        # id and then fail every gateway connection - and, because the bad value now matches
        # the cached snapshot, later polls would see no change and never repair it.
        if isinstance(value, str) and value:
            return value
        if value is not None:
            malformed.add("gateways")
        return None

    resource_set_id = next(
        (grant for g in gateway_objs if isinstance(g, dict) and (grant := _grant(g.get("resourceSetId")))), None
    )
    if resource_set_id is None and len(resource_sets) == 1:
        resource_set_id = _grant(resource_sets[0].get("objectId")) if isinstance(resource_sets[0], dict) else None

    # Rooms carry a group mesh address (roomAddress: roomId -> address); outputGroups maps
    # each output to the group addresses it belongs to. Dimming a room = one 0x0098 to its
    # group address (every member responds at once), the way the app controls a room.
    room_titles = {
        r.get("roomId"): (r.get("title") if isinstance(r.get("title"), str) else "").strip()
        for r in raw_rooms
        if isinstance(r, dict)
    }
    category_by_address = {d.address: d.category for d in devices if d.address is not None}
    dimmable_by_address = {d.address: d.dimmable for d in devices if d.address is not None}
    members_by_group: dict[int, list[int]] = {}
    # Group addresses with at least one non-light member: a group command (0x0098) is
    # unconditional and would also toggle/dim a cover, switch, etc. sharing the address,
    # so a room with any non-light member is excluded from the aggregate light entity
    # entirely rather than risk commanding an output it isn't meant to control.
    non_light_groups: set[int] = set()
    for device_id, out_map in output_groups.items():
        if not isinstance(out_map, dict):
            # Skipping keeps the parse alive, but it drops this device's group memberships,
            # which shrinks or empties the room list - a caching caller would read that as a
            # real change and remove those room entities. Flag rather than lose them quietly.
            malformed.add("rooms")
            continue  # untrusted cloud data: skip a malformed per-device group-membership map
        dev_out_addr = output_address.get(device_id)
        dev_out_addr = dev_out_addr if isinstance(dev_out_addr, dict) else {}
        for out_idx, groups in out_map.items():
            # Every skip below loses a group membership, so each one flags too - see above.
            if not isinstance(groups, list):
                malformed.add("rooms")
                continue
            # Mirror the device parser's own fallback: a single-output light can be
            # missing from outputAddress and controlled via its deviceAddress instead.
            out_addr = dev_out_addr.get(str(out_idx), device_address.get(device_id))
            if out_addr is None:
                malformed.add("rooms")
                continue
            try:
                addr = int(out_addr)
            except (TypeError, ValueError):
                malformed.add("rooms")
                continue
            is_light = category_by_address.get(addr) == CATEGORY_LIGHT
            for group in groups:
                try:
                    group_addr = int(group)
                except (TypeError, ValueError):
                    malformed.add("rooms")
                    continue
                if is_light:
                    members_by_group.setdefault(group_addr, []).append(addr)
                else:
                    non_light_groups.add(group_addr)
    # Every room's own mesh group address, validated once and reused below by both the
    # light-group-filtered `rooms` and the unfiltered `all_rooms` (move_device_to_room
    # needs a target address for a room even when it has no light-group entity).
    room_group_address: dict[str, int] = {}
    for room_id, addr in room_address.items():
        if isinstance(addr, bool):
            continue  # bool is an int subclass - int(True) == 1 would silently pass as a real address
        try:
            group_addr = int(addr)
        except (TypeError, ValueError):
            continue
        if not 1 <= group_addr <= 255:
            continue  # mesh addresses are single-byte (encode_command masks with & 0xFF); 0 is broadcast-like
        room_group_address[room_id] = group_addr

    rooms: list[PlejdCloudRoom] = []
    for room_id, group_addr in room_group_address.items():
        if group_addr in non_light_groups:
            continue  # a group command would also hit a non-light member sharing this address
        members = sorted(set(members_by_group.get(group_addr, [])))
        if not members:
            continue  # a room with no controllable outputs isn't worth a group entity
        rooms.append(
            PlejdCloudRoom(
                room_id=room_id,
                name=room_titles.get(room_id) or "Room",
                address=group_addr,
                member_addresses=members,
                dimmable=any(dimmable_by_address.get(m) for m in members),
                dimmable_addresses=[m for m in members if dimmable_by_address.get(m)],
            )
        )

    device_room_ids = {d.room_id for d in devices if d.room_id}
    all_rooms = [
        PlejdCloudRoomInfo(
            room_id=r["roomId"],
            name=(r.get("title") if isinstance(r.get("title"), str) else "").strip() or "Room",
            has_devices=r["roomId"] in device_room_ids,
            address=room_group_address.get(r["roomId"]),
        )
        for r in raw_rooms
        if isinstance(r, dict) and isinstance(r.get("roomId"), str)
    ]
    # roomAddress/outputGroups can carry a room_id absent from rooms[] itself (the same
    # source `rooms` above already draws its own room_ids from) - all_rooms's whole
    # purpose is "every room on the site", so it must include those too, not just ones
    # with a matching rooms[] object.
    known_room_ids = {r.room_id for r in all_rooms}
    all_rooms += [
        PlejdCloudRoomInfo(room_id=room_id, name="Room", has_devices=room_id in device_room_ids, address=addr)
        for room_id, addr in room_group_address.items()
        if room_id not in known_room_ids
    ]

    meta = site.get("site") or site  # id/title are nested under "site" in the real payload
    device_addresses: dict[str, int] = {}
    for device_id, addr in device_address.items():
        if isinstance(addr, bool):
            continue  # bool is an int subclass - int(True) == 1 would silently pass as a real address
        try:
            addr_int = int(addr)
        except (TypeError, ValueError):
            continue
        if not 1 <= addr_int <= 255:
            continue  # mesh addresses are single-byte (encode_command masks with & 0xFF); 0 is broadcast-like
        device_addresses[device_id] = addr_int

    # Canonical ordering, applied once here so EVERY caller that stores a snapshot (the
    # config flow's setup and reconfigure steps, coordinator.py's cloud poll) writes the
    # same order for the same site. getSiteById is JSON-object-keyed for several of these
    # (inputAddress, roomAddress, per-output address maps, ...) and object key order is not
    # semantically meaningful, so the same site can otherwise produce differently ordered
    # lists across requests - and a stored snapshot in source order would make the very
    # first poll after setup/reconfigure diff unequal and reload the integration for
    # nothing. Sorting at the source keeps that impossible rather than relying on each
    # write site to remember.
    devices.sort(key=lambda d: (d.device_id, d.output_index))
    inputs.sort(key=lambda i: (i.device_id, i.input_index))
    motion.sort(key=lambda m: m.device_id)
    scenes.sort(key=lambda s: s.scene_id)
    rooms.sort(key=lambda r: r.room_id)
    all_rooms.sort(key=lambda r: r.room_id)
    all_scenes.sort(key=lambda s: s.scene_id)
    gateways.sort()

    return PlejdCloudSite(
        site_id=meta.get("siteId") or "",
        title=meta.get("title") or "Plejd",
        crypto_key=crypto_key,
        mesh_key=mesh_key,
        devices=devices,
        inputs=inputs,
        motion=motion,
        scenes=scenes,
        gateways=gateways,
        resource_set_id=resource_set_id,
        firmware_by_device=firmware_by_device,
        device_addresses=device_addresses,
        rooms=rooms,
        all_rooms=all_rooms,
        all_scenes=all_scenes,
        malformed=frozenset(malformed),
    )
