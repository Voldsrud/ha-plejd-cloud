"""Tests for the dashboard's activity log."""

from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime

import pytest
from homeassistant.helpers import entity_registry as er
from plejd_cloud import activity
from plejd_cloud.schedule_ws import DATA_ENTRY


class _Bus:
    def __init__(self):
        self.listeners = {}

    def async_listen(self, event_type, cb):
        self.listeners[event_type] = cb
        return lambda: self.listeners.pop(event_type)


class _Conn:
    def __init__(self):
        self.result = None
        self.error = None

    def send_result(self, msg_id, payload):
        self.result = (msg_id, payload)

    def send_error(self, msg_id, code, message):
        self.error = (msg_id, code, message)


class _Services:
    def __init__(self):
        self.calls = []

    def async_services(self):
        return {
            "notify": {"mobile_app_pixel": None, "family": None, "persistent_notification": None, "send_message": None}
        }

    async def async_call(self, domain, service, data):
        self.calls.append((domain, service, data))


def _ctx(id_="c1", user_id=None, parent_id=None):
    return types.SimpleNamespace(id=id_, user_id=user_id, parent_id=parent_id)


def _state(entity_id, state, ctx=None, **attrs):
    when = attrs.pop("when", datetime(2026, 10, 10, 19, 5, tzinfo=UTC))
    return types.SimpleNamespace(
        entity_id=entity_id,
        state=state,
        attributes={"friendly_name": entity_id.split(".")[1].title(), **attrs},
        context=ctx or _ctx(),
        last_changed=when,
        last_updated=when,
    )


def _change(old, new):
    return types.SimpleNamespace(event_type="state_changed", data={"old_state": old, "new_state": new})


def _hass(origin=None, mesh_after=None):
    # mesh_after: {call time: origin} - what toggle_origin(since=...) finds after an HA call
    mesh_after = mesh_after or {}

    def reg(uid, platform="plejd_cloud", **kw):
        defaults = {"config_entry_id": "e1", "device_id": None, "area_id": None, "labels": set()}
        return types.SimpleNamespace(unique_id=uid, platform=platform, **{**defaults, **kw})

    entries = {
        "light.kontor": reg("d1"),
        "light.room_kontor": reg("room_r1", device_id="dev_room"),
        "light.other_brand": reg("x1", platform="hue"),
        "switch.relay": reg("d2"),
        "cover.blind": reg("d1"),
    }
    for entity_id, entry in entries.items():
        entry.entity_id = entity_id
    registry = er.EntityRegistry(entries)
    coordinator = types.SimpleNamespace(
        devices=[
            types.SimpleNamespace(device_id="d1", output_index=0, address=42, category="light", room_id="r1"),
            types.SimpleNamespace(device_id="d2", output_index=0, address=None, category="light"),
            types.SimpleNamespace(device_id="d3", output_index=0, address=43, category="relay"),
        ],
        rooms=[types.SimpleNamespace(room_id="r1", name="Kontor-group", member_addresses=[42])],
        toggle_origin=lambda address, since=None, state=None: origin if since is None else mesh_after.get(since),
    )
    users = {"u1": types.SimpleNamespace(name="Jonathan")}

    async def _get_user(user_id):
        return users.get(user_id)

    group_states = {
        "switch.schedule_workday": types.SimpleNamespace(state="on"),
        "group.downstairs": types.SimpleNamespace(attributes={"entity_id": ["group.inner", "light.other"]}),
        "group.inner": types.SimpleNamespace(attributes={"entity_id": ["light.room_kontor", "group.downstairs"]}),
    }
    return types.SimpleNamespace(
        states=types.SimpleNamespace(get=group_states.get),
        data={
            DATA_ENTRY: types.SimpleNamespace(
                runtime_data=coordinator, entry_id="e1", data={"room_names": {"r1": "Kontor"}}, options={}
            )
        },
        services=_Services(),
        device_registry=types.SimpleNamespace(
            async_get=lambda device_id: (
                types.SimpleNamespace(area_id="kontor", labels={"dev-label"}) if device_id == "dev_room" else None
            )
        ),
        area_registry=types.SimpleNamespace(
            async_get_area=lambda area_id: (
                types.SimpleNamespace(floor_id="upstairs", labels={"area-label"}) if area_id == "kontor" else None
            )
        ),
        bus=_Bus(),
        entity_registry=registry,
        auth=types.SimpleNamespace(async_get_user=_get_user),
    )


async def _log(hass):
    log = activity.PlejdActivityLog(hass, hass.data[DATA_ENTRY])
    await log.async_load()
    log.async_start()
    hass.data[activity.DATA_ACTIVITY] = log
    return log


async def test_ha_user_change_is_logged_with_the_user_name():
    hass = await _log_hass()
    hass.bus.listeners["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx(user_id="u1"), brightness=128))
    )
    conn = _Conn()
    await activity.ws_list(hass, conn, {"id": 1, "limit": 10})
    assert conn.result[1]["entries"] == [
        {
            "t": "2026-10-10T19:05:00+00:00",
            "type": "state",
            "entity_id": "light.kontor",
            "name": "Kontor",
            "state": "on",
            "room": "Kontor",
            "brightness": 50,
            "source": {"kind": "user", "user_id": "u1", "name": "Jonathan"},
        }
    ]


async def _log_hass(origin=None, mesh_after=None):
    hass = _hass(origin, mesh_after)
    await _log(hass)
    return hass


async def test_automation_and_script_runs_are_named():
    hass = await _log_hass()
    fire = hass.bus.listeners
    fire["automation_triggered"](
        types.SimpleNamespace(
            event_type="automation_triggered",
            context=_ctx("run1"),
            data={"name": "Kvällsljus", "entity_id": "automation.kvall"},
        )
    )
    fire["script_started"](
        types.SimpleNamespace(event_type="script_started", context=_ctx("run2"), data={"entity_id": "script.natt"})
    )
    fire["state_changed"](_change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx("run1"))))
    fire["state_changed"](
        _change(_state("light.kontor", "on"), _state("light.kontor", "off", _ctx("x", parent_id="run2")))
    )
    log = hass.data[activity.DATA_ACTIVITY]
    assert [e["source"] for e in log.entries] == [
        {"kind": "automation", "name": "Kvällsljus", "entity_id": "automation.kvall"},
        {"kind": "script", "name": "script.natt", "entity_id": "script.natt"},
    ]


async def test_change_from_outside_ha_uses_the_mesh_origin_or_external():
    hass = await _log_hass(origin={"kind": "plejd_room", "name": "Kontor"})
    hass.bus.listeners["state_changed"](_change(_state("light.kontor", "off"), _state("light.kontor", "on")))
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["source"] == {"kind": "plejd_room", "name": "Kontor"}

    hass = await _log_hass(origin=None)
    hass.bus.listeners["state_changed"](_change(_state("light.kontor", "off"), _state("light.kontor", "on")))
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["source"] == {"kind": "external"}


async def test_alarm_change_names_who_changed_it_when_the_panel_knows():
    hass = await _log_hass()
    fire = hass.bus.listeners["state_changed"]
    alarm = "alarm_control_panel.verisure"
    fire(_change(_state(alarm, "disarmed"), _state(alarm, "armed_away", changed_by="Jonathan Haraldsson")))
    fire(_change(_state(alarm, "armed_away"), _state(alarm, "disarmed")))
    assert [(e["state"], e["source"]) for e in hass.data[activity.DATA_ACTIVITY].entries] == [
        ("armed_away", {"kind": "alarm", "name": "Jonathan Haraldsson"}),
        ("disarmed", {"kind": "external"}),
    ]


async def test_noise_and_unrelated_entities_are_not_logged():
    hass = await _log_hass()
    fire = hass.bus.listeners["state_changed"]
    fire(_change(None, _state("light.kontor", "on")))  # added
    fire(_change(_state("light.kontor", "on"), _state("light.kontor", "on", brightness=10)))  # attribute only
    fire(_change(_state("light.kontor", "unavailable"), _state("light.kontor", "on")))  # reconnect
    fire(_change(_state("light.room_kontor", "off"), _state("light.room_kontor", "on")))  # room group light
    fire(_change(_state("light.other_brand", "off"), _state("light.other_brand", "on")))  # not Plejd
    fire(_change(_state("switch.relay", "off"), _state("switch.relay", "on")))  # Plejd, but no address
    fire(_change(_state("sensor.temp", "1"), _state("sensor.temp", "2")))  # other domain
    fire(_change(_state("cover.blind", "open"), _state("cover.blind", "closed")))  # covers aren't logged
    fire(_change(_state("light.unregistered", "off"), _state("light.unregistered", "on")))
    assert hass.data[activity.DATA_ACTIVITY].entries == []


async def test_without_a_loaded_coordinator_plejd_entities_are_skipped():
    hass = _hass()
    hass.data[DATA_ENTRY].runtime_data = None
    log = await _log(hass)
    hass.bus.listeners["state_changed"](_change(_state("light.kontor", "off"), _state("light.kontor", "on")))
    assert log.entries == []


async def test_log_is_capped_persisted_and_reloaded(monkeypatch):
    monkeypatch.setattr(activity, "MAX_ENTRIES", 3)
    hass = await _log_hass()
    for i in range(5):
        old, new = ("off", "on") if i % 2 == 0 else ("on", "off")
        hass.bus.listeners["state_changed"](_change(_state("light.kontor", old), _state("light.kontor", new)))
    assert len(hass.data[activity.DATA_ACTIVITY].entries) == 3
    reloaded = activity.PlejdActivityLog(hass, hass.data[DATA_ENTRY])
    await reloaded.async_load()
    assert reloaded.entries == hass.data[activity.DATA_ACTIVITY].entries


async def test_run_contexts_are_kept_for_a_day_and_bounded(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(activity, "_MAX_CONTEXTS", 3)
    hass = await _log_hass()

    def run(cid):
        hass.bus.listeners["automation_triggered"](
            types.SimpleNamespace(event_type="automation_triggered", context=_ctx(cid), data={"entity_id": "a"})
        )

    run("old")
    clock[0] += 20 * 3600
    run("r1")  # a run that waits for hours still has its context...
    assert list(hass.data[activity.DATA_ACTIVITY]._runs) == ["old", "r1"]
    clock[0] += 5 * 3600
    run("r2")  # ...but one older than a day is dropped
    assert list(hass.data[activity.DATA_ACTIVITY]._runs) == ["r1", "r2"]
    run("r3")
    run("r4")  # and the count stays bounded
    assert list(hass.data[activity.DATA_ACTIVITY]._runs) == ["r2", "r3", "r4"]


async def test_failed_load_never_overwrites_the_stored_log(monkeypatch):
    hass = _hass()
    hass.data[("store", "plejd_cloud.activity.e1")] = {"entries": [{"kept": True}]}
    log = activity.PlejdActivityLog(hass, hass.data[DATA_ENTRY])

    async def _fail():
        raise ValueError("newer storage version")

    monkeypatch.setattr(log._store, "async_load", _fail)
    with pytest.raises(ValueError):  # setup catches this and starts the log anyway
        await log.async_load()
    log.async_start()
    hass.bus.listeners["state_changed"](_change(_state("light.kontor", "off"), _state("light.kontor", "on")))
    await log.async_stop()
    assert log.entries[-1]["state"] == "on"  # still logged in memory
    assert hass.data[("store", "plejd_cloud.activity.e1")] == {
        "entries": [{"kept": True}]
    }  # but the store is untouched


async def test_stop_removes_the_listeners_and_flushes_pending_entries():
    hass = await _log_hass()
    log = hass.data[activity.DATA_ACTIVITY]
    log._store.async_delay_save = lambda *a, **k: None  # a delayed save that hasn't run yet
    hass.bus.listeners["state_changed"](_change(_state("light.kontor", "off"), _state("light.kontor", "on")))
    await log.async_stop()
    assert hass.bus.listeners == {}
    reloaded = activity.PlejdActivityLog(hass, hass.data[DATA_ENTRY])  # what a reload's new log sees
    await reloaded.async_load()
    assert [e["state"] for e in reloaded.entries] == ["on"]


async def test_each_setup_has_its_own_log_and_removing_it_deletes_it():
    hass = await _log_hass()
    hass.bus.listeners["state_changed"](_change(_state("light.kontor", "off"), _state("light.kontor", "on")))
    other = activity.PlejdActivityLog(hass, types.SimpleNamespace(entry_id="e2"))
    await other.async_load()
    assert other.entries == []
    await activity.async_remove_store(hass, "e1")
    gone = activity.PlejdActivityLog(hass, hass.data[DATA_ENTRY])
    await gone.async_load()
    assert gone.entries == []


async def test_a_script_called_by_an_automation_keeps_the_automation_as_source():
    hass = await _log_hass()
    fire = hass.bus.listeners
    fire["automation_triggered"](
        types.SimpleNamespace(
            event_type="automation_triggered", context=_ctx("run"), data={"name": "Kväll", "entity_id": "automation.k"}
        )
    )
    fire["script_started"](
        types.SimpleNamespace(event_type="script_started", context=_ctx("run"), data={"entity_id": "script.s"})
    )
    fire["state_changed"](_change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx("run"))))
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["source"]["kind"] == "automation"


async def test_list_errors_when_not_loaded_and_keeps_unknown_user_ids():
    conn = _Conn()
    await activity.ws_list(types.SimpleNamespace(data={}), conn, {"id": 1, "limit": 5})
    assert conn.error == (1, "not_loaded", "Plejd is not loaded")

    hass = await _log_hass()
    for user in ("gone", "gone"):
        hass.bus.listeners["state_changed"](
            _change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx(user_id=user)))
        )
    await activity.ws_list(hass, conn, {"id": 2, "limit": 5})
    assert [e["source"]["name"] for e in conn.result[1]["entries"]] == ["gone", "gone"]


def test_async_register_registers_the_commands():
    hass = types.SimpleNamespace(data={})
    activity.async_register(hass)
    assert {activity.ws_list, activity.ws_alerts_get, activity.ws_alerts_set} <= set(hass.data["ws_commands"])


def _room_call(hass, ctx, service="turn_on", domain="light", **target):
    hass.bus.listeners["call_service"](
        types.SimpleNamespace(context=ctx, data={"domain": domain, "service": service, "service_data": target})
    )


def _member(hass, old, new):
    hass.bus.listeners["state_changed"](_change(_state("light.kontor", old), _state("light.kontor", new)))
    return hass.data[activity.DATA_ACTIVITY].entries[-1]["source"]


async def test_room_command_from_ha_is_credited_to_the_one_transition_it_caused(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    hass = await _log_hass()
    _room_call(hass, _ctx(user_id="u1"), entity_id="light.room_kontor")
    assert _member(hass, "off", "on") == {"kind": "user", "user_id": "u1"}
    assert _member(hass, "on", "off") == {"kind": "external"}  # consumed: a second change isn't credited

    _room_call(hass, _ctx(user_id="u1"), entity_id="light.room_kontor")  # turn_on to a member that is already on...
    assert _member(hass, "on", "off") == {"kind": "external"}  # ...is used up by the next (off) transition...
    assert _member(hass, "off", "on") == {"kind": "external"}  # ...so a later outside "on" isn't credited to it

    _room_call(hass, _ctx(user_id="u1"), service="turn_on", entity_id=["light.room_kontor"])
    assert _member(hass, "on", "off") == {"kind": "external"}  # wrong direction for turn_on
    clock[0] += 20
    assert _member(hass, "off", "on") == {"kind": "external"}  # outside the window

    _room_call(hass, _ctx(user_id="u1"), service="toggle", entity_id="light.room_kontor")
    assert _member(hass, "on", "off") == {"kind": "user", "user_id": "u1"}  # toggle: either direction


async def test_room_command_targeted_by_device_area_or_label_is_credited():
    hass = await _log_hass()
    reg = hass.entity_registry.async_get("light.room_kontor")
    reg.labels = {"evening"}
    for target in (
        {"entity_id": "all"},
        {"entity_id": "group.downstairs"},  # a nested (and self-referencing) legacy group
        {"device_id": "dev_room"},
        {"area_id": ["kontor"]},
        {"floor_id": "upstairs"},
        {"label_id": "evening"},  # on the entity
        {"label_id": "dev-label"},  # on its device
        {"label_id": "area-label"},  # on its area
    ):
        _room_call(hass, _ctx(user_id="u1"), **target)
        assert _member(hass, "off", "on") == {"kind": "user", "user_id": "u1"}, target
        _member(hass, "on", "off")
    reg.area_id = "office"  # an entity's own area wins over its device's
    _room_call(hass, _ctx(user_id="u1"), area_id="kontor")
    assert _member(hass, "off", "on") == {"kind": "external"}


async def test_room_start_dim_from_ha_is_credited_either_way():
    hass = await _log_hass()
    for old, new in (("off", "on"), ("on", "off")):
        _room_call(hass, _ctx(user_id="u1"), domain="plejd_cloud", service="start_dim", entity_id="light.room_kontor")
        assert _member(hass, old, new) == {"kind": "user", "user_id": "u1"}


async def test_room_calls_that_are_not_credited():
    hass = await _log_hass()
    _room_call(hass, _ctx(), entity_id="light.room_kontor")  # no HA source
    _room_call(hass, _ctx(user_id="u1"), entity_id=["light.kontor", "light.unregistered"])  # not a room
    _room_call(hass, _ctx(user_id="u1"), domain="switch", entity_id="light.room_kontor")  # not a light call
    _room_call(hass, _ctx(user_id="u1"), area_id="elsewhere")  # targets something else
    _room_call(hass, _ctx(user_id="u1"), domain="plejd_cloud", service="stop_dim", entity_id="light.room_kontor")
    assert _member(hass, "off", "on") == {"kind": "external"}
    hass.entity_registry._entities.pop("light.room_kontor")  # room light not registered
    _room_call(hass, _ctx(user_id="u1"), device_id="dev_room")
    assert _member(hass, "on", "off") == {"kind": "external"}


async def test_room_commands_are_ignored_without_a_loaded_coordinator():
    hass = await _log_hass()
    hass.data[DATA_ENTRY].runtime_data = None
    hass.bus.listeners["call_service"](
        types.SimpleNamespace(
            context=_ctx(user_id="u1"),
            data={"domain": "light", "service": "turn_on", "service_data": {"entity_id": "light.room_kontor"}},
        )
    )
    assert hass.data[activity.DATA_ACTIVITY]._room_commands == {}


async def test_all_off_service_is_credited_to_whoever_called_it():
    hass = await _log_hass()
    hass.bus.listeners["call_service"](
        types.SimpleNamespace(
            context=_ctx(user_id="u1"), data={"domain": "plejd_cloud", "service": "all_off", "service_data": {}}
        )
    )
    assert _member(hass, "on", "off") == {"kind": "user", "user_id": "u1"}
    hass.bus.listeners["call_service"](  # called from outside HA (no user or run): nothing to credit
        types.SimpleNamespace(context=_ctx(), data={"domain": "plejd_cloud", "service": "all_off", "service_data": {}})
    )
    assert _member(hass, "on", "off") == {"kind": "external"}


async def test_an_alarm_with_a_non_numeric_brightness_attribute_is_still_logged():
    hass = await _log_hass()
    alarm = "alarm_control_panel.v"
    hass.bus.listeners["state_changed"](
        _change(_state(alarm, "disarmed"), _state(alarm, "armed_home", brightness="high"))
    )
    entry = hass.data[activity.DATA_ACTIVITY].entries[-1]
    assert entry["state"] == "armed_home" and "brightness" not in entry


async def test_all_off_credits_only_light_outputs():
    hass = await _log_hass()
    hass.bus.listeners["call_service"](
        types.SimpleNamespace(
            context=_ctx(user_id="u1"), data={"domain": "plejd_cloud", "service": "all_off", "service_data": {}}
        )
    )
    assert set(hass.data[activity.DATA_ACTIVITY]._room_commands) == {42}  # not the relay (43)


async def test_a_room_command_is_used_up_even_by_a_transition_with_its_own_context(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    hass = await _log_hass()
    _room_call(hass, _ctx("room", user_id="u1"), entity_id="light.room_kontor")  # no-op: the member is on
    hass.bus.listeners["state_changed"](
        _change(_state("light.kontor", "on"), _state("light.kontor", "off", _ctx("direct", user_id="u2")))
    )
    assert _member(hass, "off", "on") == {"kind": "external"}  # not the stale room caller


async def test_a_mesh_command_after_an_ha_call_beats_the_reused_context(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    hass = await _log_hass(mesh_after={100.0: {"kind": "plejd_device"}})
    fire = hass.bus.listeners
    fire["call_service"](
        types.SimpleNamespace(
            context=_ctx("c1", user_id="u1"), data={"domain": "light", "service": "turn_on", "service_data": {}}
        )
    )
    fire["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx("c1", user_id="u1")))
    )
    # the same reused context, but the wall switch pressed after the call explains it
    entries = hass.data[activity.DATA_ACTIVITY].entries
    assert entries[-1]["source"] == {"kind": "plejd_device"}
    hass2 = await _log_hass()  # no mesh command after the call: it was the HA user
    hass2.bus.listeners["call_service"](
        types.SimpleNamespace(
            context=_ctx("c2", user_id="u1"), data={"domain": "light", "service": "turn_on", "service_data": {}}
        )
    )
    hass2.bus.listeners["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx("c2", user_id="u1")))
    )
    assert hass2.data[activity.DATA_ACTIVITY].entries[-1]["source"]["kind"] == "user"


async def test_recorded_calls_are_bounded(monkeypatch):
    monkeypatch.setattr(activity, "_MAX_CALLS", 2)
    hass = await _log_hass()
    for i in range(3):
        hass.bus.listeners["call_service"](
            types.SimpleNamespace(context=_ctx(f"c{i}"), data={"domain": "light", "service": "x", "service_data": {}})
        )
    assert list(hass.data[activity.DATA_ACTIVITY]._calls) == ["c1", "c2"]


async def test_a_waiting_automation_keeps_its_name_across_a_reload():
    hass = await _log_hass()
    hass.bus.listeners["automation_triggered"](
        types.SimpleNamespace(
            event_type="automation_triggered", context=_ctx("run"), data={"name": "Kväll", "entity_id": "a.k"}
        )
    )
    await hass.data[activity.DATA_ACTIVITY].async_stop()
    await _log(hass)  # the reload's new log
    hass.bus.listeners["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx("run")))
    )
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["source"]["name"] == "Kväll"


async def test_a_room_command_after_the_members_own_call_wins(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    hass = await _log_hass()
    fire = hass.bus.listeners
    fire["call_service"](  # a direct command to the member (no-op, but its context stays on the entity)...
        types.SimpleNamespace(
            context=_ctx("direct", user_id="u1"), data={"domain": "light", "service": "turn_off", "service_data": {}}
        )
    )
    clock[0] = 102.0
    _room_call(hass, _ctx("room", user_id="u2"), entity_id="light.room_kontor")  # ...then u2 switches the room
    fire["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx("direct", user_id="u1")))
    )
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["source"] == {"kind": "user", "user_id": "u2"}


# ── v2: room, dimming, retention, schedules, alerts, ranges ──────────────────


async def test_entries_name_their_plejd_room():
    hass = await _log_hass()
    hass.bus.listeners["state_changed"](_change(_state("light.kontor", "off"), _state("light.kontor", "on")))
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["room"] == "Kontor"


async def test_a_dim_is_one_entry_from_start_to_end_level(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    hass = await _log_hass()
    fire = hass.bus.listeners["state_changed"]

    def level(before, after, ctx=None):
        fire(
            _change(
                _state("light.kontor", "on", brightness=before), _state("light.kontor", "on", ctx, brightness=after)
            )
        )

    level(51, 102, _ctx(user_id="u1"))  # 20% -> 40%
    clock[0] += 1
    level(102, 204)  # still the same dim: steps within 3 s merge
    clock[0] += 1
    level(204, 204)  # no change in level: ignored
    clock[0] += 5
    level(204, 26)  # settled, so this is a new dim
    dims = [e for e in hass.data[activity.DATA_ACTIVITY].entries if e["type"] == "dim"]
    assert [(d["from"], d["to"], d["source"]["kind"], d["room"]) for d in dims] == [
        (20, 80, "user", "Kontor"),
        (80, 10, "external", "Kontor"),
    ]
    fire(_change(_state("light.kontor", "on", brightness=26), _state("light.kontor", "on")))  # brightness gone: ignored
    fire(_change(_state("light.kontor", "on"), _state("light.kontor", "on", color_temp=300)))  # not a dim
    assert len([e for e in hass.data[activity.DATA_ACTIVITY].entries if e["type"] == "dim"]) == 2


async def test_switching_off_ends_a_dim(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    hass = await _log_hass()
    fire = hass.bus.listeners["state_changed"]
    fire(_change(_state("light.kontor", "on", brightness=51), _state("light.kontor", "on", brightness=102)))
    fire(_change(_state("light.kontor", "on", brightness=102), _state("light.kontor", "off")))
    fire(_change(_state("light.kontor", "off"), _state("light.kontor", "on", brightness=102)))
    fire(_change(_state("light.kontor", "on", brightness=102), _state("light.kontor", "on", brightness=204)))
    assert [e["type"] for e in hass.data[activity.DATA_ACTIVITY].entries] == ["dim", "state", "state", "dim"]


async def test_entries_older_than_30_days_are_dropped():
    hass = await _log_hass()  # "now" in the test stub is 2026-05-31
    fire = hass.bus.listeners["state_changed"]
    old = datetime(2026, 4, 20, tzinfo=UTC)
    recent = datetime(2026, 5, 20, tzinfo=UTC)
    fire(_change(_state("light.kontor", "off"), _state("light.kontor", "on", when=old)))
    fire(_change(_state("light.kontor", "on"), _state("light.kontor", "off", when=recent)))
    assert [e["state"] for e in hass.data[activity.DATA_ACTIVITY].entries] == ["off"]


async def test_scene_at_a_schedules_time_is_credited_to_the_schedule():
    when = datetime(2026, 10, 12, 5, 31, tzinfo=UTC)  # a Monday, 07:31 local (+02:00)
    hass = await _log_hass(origin={"kind": "plejd_scene", "name": "Morgon", "index": 3})
    hass.data[DATA_ENTRY].options = {
        "schedules": [
            {"name": "Weekend", "days": [5, 6], "time": "07:30:00", "scene": 3},
            {"name": "Other scene", "days": [0], "time": "07:30:00", "scene": 9},
            {"name": "Workday", "days": [0, 1, 2, 3, 4], "time": "07:30:00", "scene": 3},
        ]
    }
    fire = hass.bus.listeners["state_changed"]
    fire(_change(_state("light.kontor", "off"), _state("light.kontor", "on", when=when)))
    fire(_change(_state("light.kontor", "on"), _state("light.kontor", "off", when=when.replace(hour=12))))
    assert [e["source"] for e in hass.data[activity.DATA_ACTIVITY].entries] == [
        {"kind": "plejd_schedule", "name": "Workday"},
        {"kind": "plejd_scene", "name": "Morgon"},  # not near any schedule's time: just the scene
    ]


async def test_automation_trigger_is_kept_with_its_source():
    hass = await _log_hass()
    hass.bus.listeners["automation_triggered"](
        types.SimpleNamespace(
            event_type="automation_triggered",
            context=_ctx("run"),
            data={"name": "Hall", "entity_id": "automation.hall", "source": "state of binary_sensor.hall_motion"},
        )
    )
    hass.bus.listeners["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx("run")))
    )
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["source"]["trigger"] == "state of binary_sensor.hall_motion"


async def _alert_hass(**cfg):
    hass = await _log_hass(origin={"kind": "plejd_room", "name": "Kontor"})
    await hass.data[activity.DATA_ACTIVITY].async_set_alerts(
        {"enabled": True, "start": "23:00", "end": "06:00", "targets": ["mobile_app_pixel"], **cfg}
    )
    tasks = []
    hass.async_create_task = tasks.append
    return hass, tasks


async def test_night_watch_notifies_about_a_light_going_on_in_the_window():
    hass, tasks = await _alert_hass()
    fire = hass.bus.listeners["state_changed"]
    night = datetime(2026, 10, 11, 0, 13, tzinfo=UTC)  # 02:13 local
    fire(_change(_state("light.kontor", "off"), _state("light.kontor", "on", when=night)))
    await asyncio.gather(*tasks)
    assert hass.services.calls == [
        (
            "notify",
            "mobile_app_pixel",
            {"title": "Plejd night watch", "message": "Kontor (Kontor) turned on at 02:13 — Plejd app (whole room)"},
        ),
        (
            "persistent_notification",
            "create",
            {"title": "Plejd night watch", "message": "Kontor (Kontor) turned on at 02:13 — Plejd app (whole room)"},
        ),
    ]


async def test_night_watch_stays_quiet_outside_its_rules():
    hass, tasks = await _alert_hass(persistent=False)
    fire = hass.bus.listeners["state_changed"]
    night = datetime(2026, 10, 11, 0, 13, tzinfo=UTC)
    day = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    fire(_change(_state("light.kontor", "off"), _state("light.kontor", "on", when=day)))  # outside the window
    fire(_change(_state("light.kontor", "on"), _state("light.kontor", "off", when=night)))  # off, not on
    fire(
        _change(_state("alarm_control_panel.v", "disarmed"), _state("alarm_control_panel.v", "armed_away", when=night))
    )
    hass.bus.listeners["automation_triggered"](
        types.SimpleNamespace(
            event_type="automation_triggered", context=_ctx("run"), data={"entity_id": "automation.n"}
        )
    )
    fire(_change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx("run"), when=night)))  # planned
    assert tasks == []
    await hass.data[activity.DATA_ACTIVITY].async_set_alerts({"enabled": False})
    fire(_change(_state("light.kontor", "off"), _state("light.kontor", "on", when=night)))
    assert tasks == []


async def test_night_watch_can_include_the_alarm():
    hass, tasks = await _alert_hass(alarm=True, targets=[])
    night = datetime(2026, 10, 11, 0, 13, tzinfo=UTC)
    alarm = "alarm_control_panel.verisure"
    hass.bus.listeners["state_changed"](
        _change(_state(alarm, "armed_away"), _state(alarm, "disarmed", when=night, changed_by="Sofia"))
    )
    await asyncio.gather(*tasks)
    assert hass.services.calls == [
        (
            "persistent_notification",
            "create",
            {"title": "Plejd night watch", "message": "Verisure alarm: disarmed at 02:13 — Changed by Sofia"},
        ),
    ]


def test_window_handles_midnight_and_same_day():
    assert activity._in_window("23:30", "23:00", "06:00") and activity._in_window("05:59", "23:00", "06:00")
    assert not activity._in_window("06:00", "23:00", "06:00")
    assert activity._in_window("13:00", "12:00", "14:00") and not activity._in_window("14:00", "12:00", "14:00")


def test_describe_source_covers_every_kind():
    assert (
        activity.describe_source({"kind": "plejd_motion", "name": "Garage"}) == "Plejd motion sensor: Garage (likely)"
    )
    assert activity.describe_source({"kind": "nope"}) == "Outside Home Assistant"


async def test_list_filters_by_time_range_and_reports_more():
    hass = await _log_hass()
    fire = hass.bus.listeners["state_changed"]
    for day in (20, 21, 22):
        when = datetime(2026, 5, day, 12, tzinfo=UTC)
        fire(_change(_state("light.kontor", "off"), _state("light.kontor", "on", when=when)))
    conn = _Conn()
    await activity.ws_list(
        hass, conn, {"id": 1, "start": "2026-05-21T00:00:00+00:00", "end": "2026-05-23T00:00:00+00:00", "limit": 1}
    )
    result = conn.result[1]
    assert [e["t"][:10] for e in result["entries"]] == ["2026-05-22"]
    assert result["more"] is True and result["oldest"].startswith("2026-05-20")
    await activity.ws_list(hass, conn, {"id": 2, "start": "yesterday", "limit": 5})
    assert conn.error == (2, "invalid_time", "start/end must be ISO times")


async def test_alert_settings_round_trip_and_reject_unknown_targets():
    hass = await _log_hass()
    conn = _Conn()
    await activity.ws_alerts_get(hass, conn, {"id": 1})
    assert conn.result[1]["alerts"] == activity.DEFAULT_ALERTS
    assert conn.result[1]["notify_services"] == ["family", "mobile_app_pixel"]
    cfg = {**activity.DEFAULT_ALERTS, "enabled": True, "targets": ["family"]}
    await activity.ws_alerts_set(hass, conn, {"id": 2, "alerts": cfg})
    assert conn.result == (2, {"alerts": cfg})
    reloaded = activity.PlejdActivityLog(hass, hass.data[DATA_ENTRY])
    await reloaded.async_load()
    assert reloaded.alerts == cfg
    await activity.ws_alerts_set(hass, conn, {"id": 3, "alerts": {**cfg, "targets": ["gone"]}})
    assert conn.error == (3, "unknown_target", "Unknown notify service: gone")
    hass.data[activity.DATA_ACTIVITY].alerts = {**cfg, "targets": ["family", "gone"]}  # "gone" was removed after saving
    await activity.ws_alerts_set(hass, conn, {"id": 4, "alerts": {**cfg, "targets": ["gone"]}})
    assert conn.result == (4, {"alerts": {**cfg, "targets": ["gone"]}})  # a stale target can be kept or unticked


async def test_list_drops_entries_that_aged_past_retention_while_idle(monkeypatch):
    hass = await _log_hass()
    hass.bus.listeners["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", when=datetime(2026, 5, 20, tzinfo=UTC)))
    )
    later = datetime(2026, 6, 25, 12, tzinfo=UTC)
    monkeypatch.setattr(activity.dt_util, "now", lambda: later)
    conn = _Conn()
    await activity.ws_list(hass, conn, {"id": 1, "limit": 5})
    assert conn.result[1] == {"entries": [], "more": False, "oldest": None}
    assert hass.data[activity.DATA_ACTIVITY].entries == []


async def test_alert_commands_error_when_not_loaded():
    conn = _Conn()
    empty = types.SimpleNamespace(data={})
    await activity.ws_alerts_get(empty, conn, {"id": 1})
    assert conn.error[1] == "not_loaded"
    await activity.ws_alerts_set(empty, conn, {"id": 2, "alerts": activity.DEFAULT_ALERTS})
    assert conn.error[1] == "not_loaded"


async def test_a_light_without_a_room_has_no_room_field():
    hass = await _log_hass()
    hass.data[DATA_ENTRY].runtime_data.devices[0].room_id = None
    hass.bus.listeners["state_changed"](_change(_state("light.kontor", "off"), _state("light.kontor", "on")))
    assert "room" not in hass.data[activity.DATA_ACTIVITY].entries[-1]


async def test_a_dim_from_plejd_gets_its_mesh_source(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    hass = await _log_hass(origin={"kind": "plejd_scene", "name": "Kväll", "index": 9})
    hass.bus.listeners["state_changed"](
        _change(_state("light.kontor", "on", brightness=51), _state("light.kontor", "on", brightness=204))
    )
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["source"] == {"kind": "plejd_scene", "name": "Kväll"}


async def test_night_watch_names_the_ha_user():
    hass, tasks = await _alert_hass(persistent=False)
    night = datetime(2026, 10, 11, 0, 13, tzinfo=UTC)
    hass.bus.listeners["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx(user_id="u1"), when=night))
    )
    hass.bus.listeners["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx(user_id="gone"), when=night))
    )
    await asyncio.gather(*tasks)
    assert [c[2]["message"] for c in hass.services.calls] == [
        "Kontor (Kontor) turned on at 02:13 — Jonathan via Home Assistant",
        "Kontor (Kontor) turned on at 02:13 — someone via Home Assistant",
    ]


async def test_night_watch_ignores_plejd_relays():
    hass, tasks = await _alert_hass()
    hass.entity_registry._entities["switch.pump"] = types.SimpleNamespace(
        unique_id="d3", platform="plejd_cloud", config_entry_id="e1", device_id=None, area_id=None, labels=set()
    )
    night = datetime(2026, 10, 11, 0, 13, tzinfo=UTC)
    hass.bus.listeners["state_changed"](_change(_state("switch.pump", "off"), _state("switch.pump", "on", when=night)))
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["entity_id"] == "switch.pump"  # logged...
    assert tasks == []  # ...but no "light turned on" alert


async def test_a_switched_off_schedule_is_not_credited():
    when = datetime(2026, 10, 12, 5, 31, tzinfo=UTC)  # Monday 07:31 local
    hass = await _log_hass(origin={"kind": "plejd_scene", "name": "Morgon", "index": 3})
    hass.data[DATA_ENTRY].runtime_data.site_id = "S1"
    hass.data[DATA_ENTRY].options = {
        "schedules": [{"id": 7, "name": "Workday", "days": [0], "time": "07:30", "scene": 3}]
    }
    hass.entity_registry._entities["switch.schedule_workday"] = types.SimpleNamespace(
        unique_id="S1_schedule_7",
        platform="plejd_cloud",
        config_entry_id="e1",
        device_id=None,
        area_id=None,
        labels=set(),
    )
    fire = hass.bus.listeners["state_changed"]
    fire(_change(_state("light.kontor", "off"), _state("light.kontor", "on", when=when)))
    hass.states.get("switch.schedule_workday").state = "off"
    fire(_change(_state("light.kontor", "on"), _state("light.kontor", "off", when=when)))
    assert [e["source"]["kind"] for e in hass.data[activity.DATA_ACTIVITY].entries] == ["plejd_schedule", "plejd_scene"]


async def test_entries_older_than_30_days_are_dropped_on_load():
    hass = _hass()
    hass.data[("store", "plejd_cloud.activity.e1")] = {
        "entries": [
            {
                "t": "2026-04-01T00:00:00+00:00",
                "type": "state",
                "entity_id": "light.kontor",
                "name": "K",
                "state": "on",
                "source": {},
            },
            {
                "t": "2026-05-30T00:00:00+00:00",
                "type": "state",
                "entity_id": "light.kontor",
                "name": "K",
                "state": "off",
                "source": {},
            },
        ]
    }
    log = await _log(hass)  # "now" in the test stub is 2026-05-31
    assert [e["state"] for e in log.entries] == ["off"]
    assert [e["state"] for e in hass.data[("store", "plejd_cloud.activity.e1")]["entries"]] == ["off"]  # and saved


async def test_a_plejd_scene_run_from_ha_is_credited_to_its_caller(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    origin = {"kind": "plejd_scene", "name": "Kväll", "index": 3, "at": 101.0}
    hass = await _log_hass(origin=origin)
    hass.data[DATA_ENTRY].runtime_data.scenes = [types.SimpleNamespace(scene_id="sc1", index=3)]
    reg = hass.entity_registry._entities
    scene_reg = {"config_entry_id": "e1", "device_id": None, "area_id": None, "labels": set()}
    reg["scene.kvall"] = types.SimpleNamespace(unique_id="scene_sc1", entity_id="scene.kvall", **scene_reg)
    reg["scene.other"] = types.SimpleNamespace(unique_id="x", entity_id="scene.other", **scene_reg)
    fire = hass.bus.listeners

    def run_scene(ctx, target):
        fire["call_service"](
            types.SimpleNamespace(
                context=ctx, data={"domain": "scene", "service": "turn_on", "service_data": {"entity_id": target}}
            )
        )

    run_scene(_ctx(user_id="u1"), "scene.kvall")
    clock[0] = 101.5
    assert _member(hass, "off", "on") == {"kind": "user", "user_id": "u1"}
    assert _member(hass, "on", "off") == {"kind": "user", "user_id": "u1"}  # another output of the same firing
    origin["at"] = 104.0  # a second firing within the window wasn't caused by that call
    assert _member(hass, "off", "on") == {"kind": "plejd_scene", "name": "Kväll"}
    run_scene(_ctx(), ["scene.kvall"])  # no HA source: the mesh's own scene label stays
    run_scene(_ctx(user_id="u1"), ["scene.other", "scene.missing"])  # not Plejd scenes
    origin["at"] = 99.0  # a firing from before the call
    assert _member(hass, "on", "off") == {"kind": "plejd_scene", "name": "Kväll"}
    reg["scene.kvall"].area_id = "vardagsrum"
    clock[0] = 200.0
    origin["at"] = 200.5
    hass.bus.listeners["call_service"](
        types.SimpleNamespace(
            context=_ctx(user_id="u1"),
            data={"domain": "scene", "service": "turn_on", "service_data": {"area_id": "vardagsrum"}},
        )
    )
    assert _member(hass, "off", "on") == {"kind": "user", "user_id": "u1"}  # targeted through its area


async def test_a_scene_after_an_ha_call_is_still_checked_against_schedules(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    hass = await _log_hass(mesh_after={100.0: {"kind": "plejd_scene", "name": "Morgon", "index": 3}})
    hass.data[DATA_ENTRY].runtime_data.site_id = "S1"
    hass.data[DATA_ENTRY].options = {
        "schedules": [{"id": 1, "name": "Workday", "days": [0], "time": "07:30", "scene": 3}]
    }
    hass.bus.listeners["call_service"](
        types.SimpleNamespace(
            context=_ctx("c1", user_id="u1"), data={"domain": "light", "service": "turn_off", "service_data": {}}
        )
    )
    when = datetime(2026, 10, 12, 5, 31, tzinfo=UTC)  # Monday 07:31 local
    hass.bus.listeners["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", _ctx("c1", user_id="u1"), when=when))
    )
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["source"] == {"kind": "plejd_schedule", "name": "Workday"}


async def test_schedules_match_across_midnight_and_use_the_update_time():
    hass = await _log_hass(origin={"kind": "plejd_scene", "name": "Natt", "index": 4})
    hass.data[DATA_ENTRY].options = {
        "schedules": [{"id": 2, "name": "Sunday night", "days": [6], "time": "23:59", "scene": 4}]
    }
    monday_0000 = datetime(2026, 10, 11, 22, 0, tzinfo=UTC)  # Monday 00:00 local, a minute after Sunday 23:59
    st = _state("light.kontor", "on", when=monday_0000)
    st.last_changed = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)  # a dim: last_changed is when it went on, long before
    hass.bus.listeners["state_changed"](_change(_state("light.kontor", "off"), st))
    assert hass.data[activity.DATA_ACTIVITY].entries[-1]["source"] == {"kind": "plejd_schedule", "name": "Sunday night"}


async def test_one_failing_notify_target_does_not_stop_the_others(caplog):
    hass, tasks = await _alert_hass(targets=["mobile_app_pixel", "family"])
    sent = []

    async def _call(domain, service, data):
        if service == "mobile_app_pixel":
            raise RuntimeError("service not found")  # a removed phone
        sent.append((domain, service))

    hass.services.async_call = _call
    night = datetime(2026, 10, 11, 0, 13, tzinfo=UTC)
    hass.bus.listeners["state_changed"](
        _change(_state("light.kontor", "off"), _state("light.kontor", "on", when=night))
    )
    await asyncio.gather(*tasks)
    assert sent == [("notify", "family"), ("persistent_notification", "create")]
    assert "could not deliver to one target (RuntimeError)" in caplog.text
    assert "pixel" not in caplog.text  # the phone's name stays out of the log
