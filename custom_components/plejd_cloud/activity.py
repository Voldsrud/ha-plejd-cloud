"""Activity log for the dashboard: on/off and dimming of Plejd lights/switches and alarm changes, with their source.

HA's own context says when a change came from Home Assistant (a user, an automation or a script). For
changes from outside HA, the mesh shows only part of the story - see PlejdCoordinator.toggle_origin - and
an alarm panel may name who changed it (Verisure's ``changed_by``). Kept in its own Store so the log
survives restarts and recorder purges.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CATEGORY_LIGHT, CONF_ROOM_NAMES, CONF_SCHEDULES, DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_ACTIVITY = f"{DOMAIN}_activity"
RETENTION = timedelta(days=30)
MAX_ENTRIES = 50000  # safety cap on top of the 30 days
# A dim is finished once the level has stayed put this long; until then its steps merge into one entry.
DIM_SETTLE = 3.0
# A scene run this close to an on-device schedule's time is credited to that schedule.
_SCHEDULE_SLACK = timedelta(minutes=2)
_ALERT_STORE_KEY = f"{DOMAIN}.activity_alerts"
DEFAULT_ALERTS: dict[str, Any] = {
    "enabled": False,
    "start": "23:00",
    "end": "06:00",
    "targets": [],  # notify services, e.g. "mobile_app_pixel"
    "persistent": True,  # also a notification in Home Assistant itself
    "ignore": ["automation", "script", "plejd_schedule"],  # planned changes don't alert
    "alarm": False,  # also alert on alarm changes in the window
}
_STORE_KEY = f"{DOMAIN}.activity"  # + ".<entry_id>": one log per Plejd setup
# Covers aren't logged: Plejd covers have no state read-back, so they never change state in HA.
_TRACKED_PLEJD_DOMAINS = ("light", "switch")
_IGNORED_STATES = ("unavailable", "unknown")
# Automation/script runs remembered for attribution: long enough for runs with delays/waits, bounded for memory.
_RUN_TTL = 24 * 3600
_MAX_CONTEXTS = 5000
_DATA_RUNS = f"{DOMAIN}_activity_runs"
_DATA_CALLS = f"{DOMAIN}_activity_calls"
_MAX_CALLS = 500
# How long after an HA command to a Plejd room its member lights' changes are credited to it.
_ROOM_COMMAND_WINDOW = 10.0


class PlejdActivityLog:
    """Listens to state changes and keeps the newest MAX_ENTRIES of them."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        # Its own reference to the entry (and so the coordinator), not hass.data: older HA runs on_unload
        # callbacks even when an unload is refused, which would drop that lookup from a still-running setup.
        self._entry = entry
        self._store: Store = Store(hass, 1, f"{_STORE_KEY}.{entry.entry_id}")
        self.entries: list[dict[str, Any]] = []  # oldest first
        # context id -> (started, run). Kept in hass.data, not on this instance, so an automation that is still
        # waiting while the integration reloads keeps its name when it acts afterwards.
        self._runs: OrderedDict[str, tuple[float, dict[str, str]]] = hass.data.setdefault(_DATA_RUNS, OrderedDict())
        # context id -> when HA sent a service call with it, to tell a fresh outside action from an old context
        # HA keeps reusing on the entity for a few seconds after a call.
        self._calls: OrderedDict[str, float] = hass.data.setdefault(_DATA_CALLS, OrderedDict())
        # Saving is off until the stored log has been read: after a failed read, saving would replace it.
        self._can_save = False
        # member mesh address -> (when, source) of an HA command sent to its Plejd room light. The room light
        # isn't logged, and the member changes it causes carry no context of their own.
        self._room_commands: dict[int, tuple[float, dict[str, Any], str | None]] = {}  # + the expected new state
        self._dims: dict[str, tuple[float, dict[str, Any]]] = {}  # entity_id -> (last step, its open dim entry)
        # Plejd scene index -> [when called, HA source, when the scene it caused fired]
        self._scene_calls: dict[int, list[Any]] = {}
        self._alert_store: Store = Store(hass, 1, f"{_ALERT_STORE_KEY}.{entry.entry_id}")
        self.alerts: dict[str, Any] = dict(DEFAULT_ALERTS)
        self._unsubs: list = []

    async def async_load(self) -> None:
        self.entries = list((await self._store.async_load() or {}).get("entries", []))[-MAX_ENTRIES:]
        self._can_save = True
        cutoff = _utc((dt_util.now() - RETENTION).isoformat())
        if self.entries and _utc(self.entries[0]["t"]) < cutoff:
            self.entries = [e for e in self.entries if _utc(e["t"]) >= cutoff]
            self._save()
        self.alerts = {**DEFAULT_ALERTS, **(await self._alert_store.async_load() or {})}

    async def async_set_alerts(self, alerts: dict[str, Any]) -> None:
        self.alerts = {**DEFAULT_ALERTS, **alerts}
        await self._alert_store.async_save(self.alerts)

    @callback
    def async_start(self) -> None:
        bus = self.hass.bus
        self._unsubs = [
            bus.async_listen("state_changed", self._on_state_changed),
            bus.async_listen("automation_triggered", self._on_run),
            bus.async_listen("script_started", self._on_run),
            bus.async_listen("call_service", self._on_call_service),
        ]

    async def async_stop(self) -> None:
        """Stop listening and write out what's pending, so a reload's new log loads everything."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._can_save:
            await self._store.async_save({"entries": self.entries})

    @callback
    def _on_run(self, event) -> None:
        kind = "automation" if event.event_type == "automation_triggered" else "script"
        if event.context.id in self._runs:
            return  # a script called by an automation runs in its context: the automation stays the source
        now = time.monotonic()
        self._runs[event.context.id] = (
            now,
            {
                "kind": kind,
                "name": event.data.get("name") or event.data.get("entity_id", ""),
                "entity_id": event.data.get("entity_id", ""),
                **(
                    {"trigger": event.data["source"]} if event.data.get("source") else {}
                ),  # e.g. "state of binary_sensor.x"
            },
        )
        while self._runs and (len(self._runs) > _MAX_CONTEXTS or now - next(iter(self._runs.values()))[0] > _RUN_TTL):
            self._runs.popitem(last=False)

    @callback
    def _on_call_service(self, event) -> None:
        self._calls[event.context.id] = time.monotonic()
        self._calls.move_to_end(event.context.id)
        while len(self._calls) > _MAX_CALLS:
            self._calls.popitem(last=False)
        if event.data.get("domain") == "scene" and event.data.get("service") == "turn_on":
            # A Plejd scene run from HA: its outputs change without the call's context, and the mesh echo would
            # otherwise be credited to "Plejd scene" instead of the person or automation that ran it.
            if (source := self._ha_source(event.context)) is not None:
                for index in self._plejd_scene_indexes(event.data.get("service_data") or {}):
                    self._scene_calls[index] = [time.monotonic(), source, None]
            return
        if event.data.get("domain") == DOMAIN and event.data.get("service") == "all_off":
            # plejd.all_off switches the outputs directly, so their changes carry no context of their own.
            if (source := self._ha_source(event.context)) is not None:
                coordinator = getattr(self._entry, "runtime_data", None)
                now = time.monotonic()
                for device in getattr(coordinator, "devices", []):
                    if device.address is not None and device.category == CATEGORY_LIGHT:  # all_off's own filter
                        self._room_commands[device.address] = (now, source, "off")
            return
        # Only these switch or ramp a light: turn_on/turn_off say which way; toggle and plejd.start_dim (a ramp
        # that dims an on room or brings an off one up) can go either way. stop_dim and the rest change nothing.
        service = (event.data.get("domain"), event.data.get("service"))
        if service not in (("light", "turn_on"), ("light", "turn_off"), ("light", "toggle"), (DOMAIN, "start_dim")):
            return
        source = self._ha_source(event.context)
        if source is None:
            return
        service = service[1]
        expected = {"turn_on": "on", "turn_off": "off"}.get(service)
        now = time.monotonic()
        for address in self._targeted_room_members(event.data.get("service_data") or {}):
            self._room_commands[address] = (now, source, expected)

    @callback
    def _on_state_changed(self, event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None:
            return  # added or removed
        if old.state in _IGNORED_STATES or new.state in _IGNORED_STATES:
            return  # connection drops and restarts, not someone switching anything
        entity_id = new.entity_id
        domain = entity_id.split(".")[0]
        output = None
        if domain != "alarm_control_panel":
            output = self._plejd_output(entity_id, domain)
            if output is None:
                return
        if old.state == new.state:
            if new.state == "on" and output is not None:
                self._dim_step(new, old, output)
            return  # any other attribute-only change isn't activity
        self._dims.pop(entity_id, None)  # switching on/off ends a dim in progress
        entry: dict[str, Any] = {
            "t": new.last_changed.isoformat(),
            "type": "alarm" if output is None else "state",
            "entity_id": entity_id,
            "name": new.attributes.get("friendly_name", entity_id),
            "state": new.state,
            "source": self._source(new, output.address if output else None),
        }
        if output is not None and (room := self._room_name(output)):
            entry["room"] = room
        if output is not None and (pct := _pct(new)) is not None:  # brightness means nothing on an alarm panel
            entry["brightness"] = pct
        self._add(entry)
        self._maybe_alert(entry)

    def _dim_step(self, new, old, output) -> None:
        """A brightness change while on: merged into one dim entry until the level settles."""
        before, after = _pct(old), _pct(new)
        if before is None or after is None or before == after:
            return
        now = time.monotonic()
        open_dim = self._dims.get(new.entity_id)
        if open_dim is not None and now - open_dim[0] <= DIM_SETTLE:
            open_dim[1]["to"] = after
            open_dim[1]["t_end"] = new.last_updated.isoformat()
            self._dims[new.entity_id] = (now, open_dim[1])
            self._save()
            return
        entry: dict[str, Any] = {
            "t": new.last_updated.isoformat(),
            "type": "dim",
            "entity_id": new.entity_id,
            "name": new.attributes.get("friendly_name", new.entity_id),
            "state": "on",
            "from": before,
            "to": after,
            "source": self._source(new, output.address),
        }
        if room := self._room_name(output):
            entry["room"] = room
        self._dims[new.entity_id] = (now, entry)
        self._add(entry)

    def _add(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)
        self.prune()
        self._save()

    def prune(self) -> None:
        """Drop entries past the retention window or over the size cap."""
        cutoff = (dt_util.now() - RETENTION).isoformat()
        drop = 0
        while drop < len(self.entries) and _utc(self.entries[drop]["t"]) < _utc(cutoff):
            drop += 1
        if drop := max(drop, len(self.entries) - MAX_ENTRIES):
            del self.entries[:drop]
            self._save()

    def _save(self) -> None:
        if self._can_save:
            self._store.async_delay_save(lambda: {"entries": self.entries}, 10)

    def _room_name(self, output) -> str | None:
        """The Plejd room an output is in, by name (every room's title is kept in the entry's room_names)."""
        room_id = getattr(output, "room_id", None)
        entry = self._entry
        if not room_id:
            return None
        names = {r.room_id: r.name for r in getattr(entry.runtime_data, "rooms", [])}
        names.update((getattr(entry, "data", None) or {}).get(CONF_ROOM_NAMES) or {})
        return names.get(room_id)

    def _maybe_alert(self, entry: dict[str, Any]) -> None:
        """Night watch: notify about a light going on (or an alarm change) inside the configured window."""
        cfg = self.alerts
        if not cfg.get("enabled"):
            return
        if entry["type"] == "alarm" and not cfg.get("alarm"):
            return
        if entry["type"] == "state" and (entry["state"] != "on" or not entry["entity_id"].startswith("light.")):
            return  # a light going on; a Plejd relay (switch.*) may drive anything
        if entry["source"]["kind"] in (cfg.get("ignore") or []):
            return
        local = dt_util.as_local(datetime.fromisoformat(entry["t"]))
        if not _in_window(local.strftime("%H:%M"), cfg.get("start", "23:00"), cfg.get("end", "06:00")):
            return
        self.hass.async_create_task(self._async_send_alert(entry, local, cfg))

    async def _async_send_alert(self, entry: dict[str, Any], local: datetime, cfg: dict[str, Any]) -> None:
        source = entry["source"]
        if user_id := source.get("user_id"):  # stored as an id; the message needs the person's name
            user = await self.hass.auth.async_get_user(user_id)
            source = {**source, "name": user.name if user else "someone"}
        room = f" ({entry['room']})" if entry.get("room") else ""
        what = "turned on" if entry["type"] == "state" else f"alarm: {entry['state'].replace('_', ' ')}"
        message = f"{entry['name']}{room} {what} at {local:%H:%M} — {describe_source(source)}"
        title = "Plejd night watch"
        calls = [("notify", target) for target in cfg.get("targets") or []]
        if cfg.get("persistent"):
            calls.append(("persistent_notification", "create"))
        for domain, service in calls:
            try:  # one removed phone mustn't stop the alert reaching the others
                await self.hass.services.async_call(domain, service, {"title": title, "message": message})
            except Exception as err:  # noqa: BLE001 - report and carry on with the next target
                # The service name and the exception text usually carry a person's and phone's name.
                _LOGGER.warning("Plejd night watch: could not deliver to one target (%s)", type(err).__name__)

    def _plejd_output(self, entity_id: str, domain: str):
        """The Plejd output (cloud device) behind an entity; None for anything that isn't one."""
        if domain not in _TRACKED_PLEJD_DOMAINS:
            return None
        reg = er.async_get(self.hass).async_get(entity_id)
        coordinator = getattr(self._entry, "runtime_data", None)
        if reg is None or reg.platform != DOMAIN or coordinator is None:
            return None
        for device in coordinator.devices:
            uid = device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
            if uid == reg.unique_id and device.address is not None:
                return device
        return None  # e.g. a room-group light, which only mirrors its member lights

    def _targeted_room_members(self, service_data: dict[str, Any]) -> list[int]:
        """Member addresses of every Plejd room light a light service call targets."""
        coordinator = getattr(self._entry, "runtime_data", None)
        if coordinator is None:
            return []
        is_targeted = self._target_matcher(service_data)
        registry = er.async_get(self.hass)
        by_unique_id = {e.unique_id: e for e in er.async_entries_for_config_entry(registry, self._entry.entry_id)}
        members: list[int] = []
        for room in coordinator.rooms:
            if (reg := by_unique_id.get(f"room_{room.room_id}")) is not None and is_targeted(reg):
                members.extend(room.member_addresses)
        return members

    def _target_matcher(self, service_data: dict[str, Any]):
        """Whether a registry entry is among a service call's entity/device/area/floor/label targets.

        Matches entries itself rather than through HA's target helper, whose signature differs across the
        supported HA versions.
        """

        def ids(key: str) -> set[str]:
            value = service_data.get(key) or []
            return {value} if isinstance(value, str) else set(value)

        def expand_groups(targets: set[str]) -> set[str]:
            """Members of any (nested) legacy group.* target, like HA's own expansion."""
            out, todo = set(), list(targets)
            while todo:
                entity_id = todo.pop()
                if entity_id in out:
                    continue
                out.add(entity_id)
                if entity_id.startswith("group.") and (group := self.hass.states.get(entity_id)):
                    todo.extend(group.attributes.get("entity_id") or [])
            return out

        entity_ids, device_ids, area_ids, floor_ids, label_ids = (
            expand_groups(ids("entity_id")),
            ids("device_id"),
            ids("area_id"),
            ids("floor_id"),
            ids("label_id"),
        )

        def is_targeted(reg) -> bool:
            # Mirrors HA's target expansion: the entity's own area, else its device's; labels on the entity,
            # its device or its area; and the floor of that area.
            device = dr.async_get(self.hass).async_get(reg.device_id) if reg.device_id else None
            area_id = reg.area_id or getattr(device, "area_id", None)
            area = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
            labels = (
                set(reg.labels or ())
                | set(getattr(device, "labels", ()) or ())
                | set(getattr(area, "labels", ()) or ())
            )
            return bool(
                "all" in entity_ids  # entity_id: all targets every entity of the domain
                or reg.entity_id in entity_ids
                or (reg.device_id and reg.device_id in device_ids)
                or (area_id and area_id in area_ids)
                or (getattr(area, "floor_id", None) in floor_ids)
                or labels & label_ids
            )

        return is_targeted

    def _ha_source(self, ctx) -> dict[str, Any] | None:
        """The automation/script run or user behind an HA context; None when it didn't come from HA."""
        run = self._runs.get(ctx.id) or (self._runs.get(ctx.parent_id) if ctx.parent_id else None)
        if run:
            return dict(run[1])
        if ctx.user_id:
            return {"kind": "user", "user_id": ctx.user_id}
        return None

    def _source(self, state, address: int | None) -> dict[str, Any]:
        # A room command is used up by the member's first transition after it, whichever way that goes (and
        # whatever else explains it): it credits that transition only if it's the one the command asked for.
        cmd = self._room_commands.pop(address, None) if address is not None else None
        room_cmd = (
            cmd if cmd and time.monotonic() - cmd[0] <= _ROOM_COMMAND_WINDOW and cmd[2] in (None, state.state) else None
        )
        ha = self._ha_source(state.context)
        called = self._calls.get(state.context.id)
        if room_cmd and ha and called is not None and room_cmd[0] > called:
            return dict(room_cmd[1])  # a room command sent after the call whose context the member still wears
        if ha:
            # HA keeps a call's context on the entity for a few seconds and reuses it for later writes, so a
            # wall switch or app pressed just after an HA command would wear that command's context. A mesh
            # command seen after the HA call explains this transition better.
            coordinator = getattr(self._entry, "runtime_data", None)
            if address is not None and coordinator is not None and called is not None:
                if origin := coordinator.toggle_origin(address, since=called, state=state.state):
                    return self._named_origin(origin, state)
            return ha
        if room_cmd:
            return dict(room_cmd[1])
        if address is None:  # an alarm panel: its own integration may know who changed it
            changed_by = state.attributes.get("changed_by")
            return {"kind": "alarm", "name": changed_by} if changed_by else {"kind": "external"}
        coordinator = getattr(self._entry, "runtime_data", None)
        origin = coordinator.toggle_origin(address, state=state.state) if coordinator is not None else None
        return self._named_origin(origin, state) if origin else {"kind": "external"}

    def _plejd_scene_indexes(self, service_data: dict[str, Any]) -> list[int]:
        """Mesh indexes of the Plejd scenes a scene.turn_on call targets."""
        coordinator = getattr(self._entry, "runtime_data", None)
        is_targeted = self._target_matcher(service_data)
        by_unique_id = {
            e.unique_id: e
            for e in er.async_entries_for_config_entry(er.async_get(self.hass), self._entry.entry_id)
            if e.entity_id.startswith("scene.")
        }
        return [
            scene.index
            for scene in getattr(coordinator, "scenes", [])
            if (reg := by_unique_id.get(f"scene_{scene.scene_id}")) is not None and is_targeted(reg)
        ]

    def _named_origin(self, origin: dict[str, Any], state) -> dict[str, Any]:
        """A mesh origin as logged: a scene run from HA or at a schedule's time gets that source instead."""
        if origin["kind"] == "plejd_scene":
            call = self._scene_calls.get(origin["index"])
            if call and call[2] is None and call[0] <= origin["at"] <= call[0] + _ROOM_COMMAND_WINDOW:
                call[2] = origin["at"]  # the first firing after the call is the one it caused
            if call and call[2] == origin["at"]:
                return dict(call[1])
            if schedule := self._schedule_for(origin["index"], state):
                return {"kind": "plejd_schedule", "name": schedule}
        return {k: v for k, v in origin.items() if k not in ("index", "at")}

    def _schedule_enabled(self, schedule: dict[str, Any]) -> bool:
        """Whether a schedule's switch is on (switch.py's PlejdScheduleSwitch; unknown counts as on)."""
        site_id = getattr(getattr(self._entry, "runtime_data", None), "site_id", None)
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "switch", DOMAIN, f"{site_id}_schedule_{schedule.get('id')}"
        )
        state = self.hass.states.get(entity_id) if entity_id else None
        return state is None or state.state != "off"

    def _schedule_for(self, scene_index: int, state) -> str | None:
        """The on-device schedule that runs this scene at about this time, if any."""
        entry = self._entry
        # last_updated: a dim keeps last_changed from when the light went on
        local = dt_util.as_local(state.last_updated)
        for schedule in (getattr(entry, "options", None) or {}).get(CONF_SCHEDULES) or []:
            if schedule.get("scene") != scene_index or not self._schedule_enabled(schedule):
                continue  # another scene, or switched off (its time event is gone from the devices)
            hour, minute, *_ = (int(p) for p in str(schedule.get("time", "")).split(":") + ["0"])
            # Yesterday's and tomorrow's runs too: 23:59 on Sunday can land at 00:00 on Monday.
            for day in (-1, 0, 1):
                planned = (local + timedelta(days=day)).replace(hour=hour, minute=minute, second=0, microsecond=0)
                if planned.weekday() in schedule.get("days", []) and abs(local - planned) <= _SCHEDULE_SLACK:
                    return schedule.get("name")
        return None


def _pct(state) -> int | None:
    brightness = state.attributes.get("brightness")
    return round(brightness / 255 * 100) if isinstance(brightness, int | float) else None


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(UTC)


def _in_window(hhmm: str, start: str, end: str) -> bool:
    """Whether a time of day falls in [start, end); a window like 23:00-06:00 crosses midnight."""
    return start <= hhmm < end if start <= end else (hhmm >= start or hhmm < end)


_SOURCE_TEXT = {
    "user": "{name} via Home Assistant",
    "automation": "Automation: {name}",
    "script": "Script: {name}",
    "plejd_room": "Plejd app (whole room)",
    "plejd_device": "Plejd app, Google Home or the light's own switch",
    "plejd_input": "Switch or remote: {name} (likely)",
    "plejd_scene": "Plejd scene: {name}",
    "plejd_schedule": "Plejd schedule: {name}",
    "plejd_motion": "Plejd motion sensor: {name} (likely)",
    "alarm": "Changed by {name}",
}


def describe_source(source: dict[str, Any]) -> str:
    """A source as plain English (notifications; the dashboard has its own translations)."""
    return _SOURCE_TEXT.get(source.get("kind", ""), "Outside Home Assistant").format(name=source.get("name", ""))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "plejd/activity/list",
        vol.Optional("start"): str,  # ISO times; omitted = the whole kept log
        vol.Optional("end"): str,
        vol.Optional("limit", default=5000): vol.All(int, vol.Range(min=1, max=MAX_ENTRIES)),
    }
)
@websocket_api.async_response
async def ws_list(hass: HomeAssistant, connection, msg) -> None:
    log: PlejdActivityLog | None = hass.data.get(DATA_ACTIVITY)
    if log is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    try:
        start = _utc(msg["start"]) if msg.get("start") else None
        end = _utc(msg["end"]) if msg.get("end") else None
    except ValueError:
        connection.send_error(msg["id"], "invalid_time", "start/end must be ISO times")
        return
    log.prune()  # an idle install gets no new entries to prune on
    picked = [
        e
        for e in reversed(log.entries)
        if (start is None or _utc(e["t"]) >= start) and (end is None or _utc(e["t"]) < end)
    ]
    entries = picked[: msg["limit"]]  # newest first
    names: dict[str, str] = {}
    for entry in entries:
        user_id = entry["source"].get("user_id")
        if user_id and user_id not in names:
            user = await hass.auth.async_get_user(user_id)
            names[user_id] = user.name if user else user_id
    connection.send_result(
        msg["id"],
        {
            "entries": [
                {**e, "source": {**e["source"], "name": names[e["source"]["user_id"]]}}
                if e["source"].get("user_id")
                else e
                for e in entries
            ],
            "more": len(picked) > len(entries),
            "oldest": log.entries[0]["t"] if log.entries else None,
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "plejd/activity/alerts/get"})
@websocket_api.async_response
async def ws_alerts_get(hass: HomeAssistant, connection, msg) -> None:
    log: PlejdActivityLog | None = hass.data.get(DATA_ACTIVITY)
    if log is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    # notify.persistent_notification duplicates the "persistent" option, and notify.send_message needs a
    # notify entity to target, so neither works as a plain target here.
    unusable = {"persistent_notification", "send_message"}
    services = sorted(svc for svc in hass.services.async_services().get("notify", {}) if svc not in unusable)
    connection.send_result(msg["id"], {"alerts": log.alerts, "notify_services": services})


_HHMM = vol.Match(r"^([01]\d|2[0-3]):[0-5]\d$")


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "plejd/activity/alerts/set",
        vol.Required("alerts"): {
            vol.Required("enabled"): bool,
            vol.Required("start"): _HHMM,
            vol.Required("end"): _HHMM,
            vol.Required("targets"): [str],
            vol.Required("persistent"): bool,
            vol.Required("ignore"): [str],
            vol.Required("alarm"): bool,
        },
    }
)
@websocket_api.async_response
async def ws_alerts_set(hass: HomeAssistant, connection, msg) -> None:
    log: PlejdActivityLog | None = hass.data.get(DATA_ACTIVITY)
    if log is None:
        connection.send_error(msg["id"], "not_loaded", "Plejd is not loaded")
        return
    # A target already saved stays allowed after its service disappears, so the rest can still be edited.
    known = set(hass.services.async_services().get("notify", {})) | set(log.alerts["targets"])
    if unknown := [t for t in msg["alerts"]["targets"] if t not in known]:
        connection.send_error(msg["id"], "unknown_target", f"Unknown notify service: {', '.join(unknown)}")
        return
    await log.async_set_alerts(msg["alerts"])
    connection.send_result(msg["id"], {"alerts": log.alerts})


def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_alerts_get)
    websocket_api.async_register_command(hass, ws_alerts_set)


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    """Delete a removed Plejd setup's log and alert settings, so a later setup doesn't inherit them."""
    await Store(hass, 1, f"{_STORE_KEY}.{entry_id}").async_remove()
    await Store(hass, 1, f"{_ALERT_STORE_KEY}.{entry_id}").async_remove()
