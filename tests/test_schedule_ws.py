"""Tests for the schedule WebSocket API."""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from plejd_cloud import schedule_ws
from plejd_cloud.schedule_ws import DATA_ENTRY, DATA_RELOAD_PENDING


class _Conn:
    def __init__(self):
        self.result = None
        self.error = None

    def send_result(self, msg_id, payload):
        self.result = (msg_id, payload)

    def send_error(self, msg_id, code, message):
        self.error = (msg_id, code, message)


class _ConfigEntries:
    def __init__(self):
        self.updated = None
        self.reloaded = None
        self.reload_calls: list[str] = []
        self.reload_fails = False
        self.reload_ok = True
        self.update_fails = False

    def async_update_entry(self, entry, *, options):
        if self.update_fails:
            raise RuntimeError("update failed")
        self.updated = options
        entry.options = options

    async def async_reload(self, entry_id):
        if self.reload_fails:
            raise RuntimeError("reload failed")
        self.reloaded = entry_id
        self.reload_calls.append(entry_id)
        return self.reload_ok


class _Coordinator:
    def __init__(self, fails=False):
        self.removed = []
        self.fails = fails

    async def async_remove_time_event(self, slot):
        if self.fails:
            raise RuntimeError("BLE link dropped")
        self.removed.append(slot)


def _entry(options=None, scenes=None, runtime_data=None):
    return types.SimpleNamespace(
        entry_id="e1",
        options=options or {},
        data={"scenes": scenes if scenes is not None else [{"index": 3, "name": "Movie"}]},
        runtime_data=runtime_data,
    )


def _hass(entry=None, **kw):
    hass = types.SimpleNamespace(data={}, config_entries=_ConfigEntries(), **kw)
    if entry is not None:
        hass.data[DATA_ENTRY] = entry
    return hass


_SCHEDULE = {"id": 0, "slot": 1, "name": "Evening", "days": [0, 6], "time": "18:30:00", "scene": 3, "fade": 0}


# ── list ─────────────────────────────────────────────────────────────────────


async def test_list_returns_schedules_and_scenes():
    entry = _entry(options={"schedules": [_SCHEDULE]})
    conn = _Conn()
    await schedule_ws.ws_list(_hass(entry), conn, {"id": 7})
    assert conn.result == (7, {"schedules": [_SCHEDULE], "scenes": [{"index": 3, "name": "Movie"}]})


async def test_list_errors_when_not_loaded():
    conn = _Conn()
    await schedule_ws.ws_list(_hass(), conn, {"id": 7})
    assert conn.error == (7, "not_loaded", "Plejd is not loaded")
    assert conn.result is None


# ── add ──────────────────────────────────────────────────────────────────────


async def test_add_assigns_slot_and_id_then_reloads():
    entry = _entry(options={})
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_add(
        hass, conn, {"id": 1, "name": "Evening", "days": [0, 6], "time": "18:30", "scene": 3, "fade": 5}
    )
    sched = conn.result[1]["schedules"][0]
    assert sched == {
        "id": 0,
        "slot": 0,
        "name": "Evening",
        "days": [0, 6],
        "time": "18:30:00",
        "scene": 3,
        "fade": 5,
    }
    assert entry.options["schedules"] == [sched]
    assert entry.options["next_schedule_id"] == 1
    assert hass.config_entries.reloaded == "e1"


async def test_add_uses_next_free_slot():
    existing = [{"id": 0, "slot": 0, "name": "A", "days": [0], "time": "06:00:00", "scene": 3, "fade": 0}]
    entry = _entry(options={"schedules": existing, "next_schedule_id": 1})
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "B", "days": [1], "time": "07:00", "scene": 3, "fade": 0})
    added = conn.result[1]["schedules"][1]
    assert added["slot"] == 1 and added["id"] == 1


async def test_add_defaults_fade_to_zero():
    entry = _entry(options={})
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert conn.result[1]["schedules"][0]["fade"] == 0


async def test_add_errors_when_not_loaded():
    conn = _Conn()
    await schedule_ws.ws_add(_hass(), conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert conn.error == (1, "not_loaded", "Plejd is not loaded")
    assert conn.result is None


async def test_add_rejects_blank_name():
    entry = _entry(options={})
    conn = _Conn()
    await schedule_ws.ws_add(
        _hass(entry), conn, {"id": 1, "name": "  ", "days": [0], "time": "06:00", "scene": 3, "fade": 0}
    )
    assert conn.error == (1, "name_required", "Name is required")


async def test_add_rejects_empty_days():
    entry = _entry(options={})
    conn = _Conn()
    await schedule_ws.ws_add(
        _hass(entry), conn, {"id": 1, "name": "X", "days": [], "time": "06:00", "scene": 3, "fade": 0}
    )
    assert conn.error[1] == "invalid_days"


async def test_add_rejects_out_of_range_day():
    entry = _entry(options={})
    conn = _Conn()
    await schedule_ws.ws_add(
        _hass(entry), conn, {"id": 1, "name": "X", "days": [7], "time": "06:00", "scene": 3, "fade": 0}
    )
    assert conn.error[1] == "invalid_days"


async def test_add_rejects_invalid_time():
    entry = _entry(options={})
    for bad in ("7", "25:00", "07:xx", ""):
        conn = _Conn()
        await schedule_ws.ws_add(
            _hass(entry), conn, {"id": 1, "name": "X", "days": [0], "time": bad, "scene": 3, "fade": 0}
        )
        assert conn.error[1] == "invalid_time", bad


async def test_add_rejects_unknown_scene():
    entry = _entry(options={})
    conn = _Conn()
    await schedule_ws.ws_add(
        _hass(entry), conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 99, "fade": 0}
    )
    assert conn.error[1] == "invalid_scene"


async def test_add_rejects_negative_fade():
    entry = _entry(options={})
    conn = _Conn()
    await schedule_ws.ws_add(
        _hass(entry), conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": -1}
    )
    assert conn.error[1] == "invalid_fade"


async def test_add_errors_when_no_free_slots():
    full = [
        {"id": i, "slot": i, "name": f"s{i}", "days": [0], "time": "07:00:00", "scene": 3, "fade": 0} for i in range(20)
    ]
    entry = _entry(options={"schedules": full})
    conn = _Conn()
    await schedule_ws.ws_add(
        _hass(entry), conn, {"id": 1, "name": "More", "days": [0], "time": "06:00", "scene": 3, "fade": 0}
    )
    assert conn.error == (1, "no_free_slots", "No free schedule slots")


async def test_add_returns_persisted_schedules_when_reload_raises():
    # async_reload() can raise instead of just returning False. Options are already
    # persisted at that point (issue #94 thread 2), so this must be treated the same as a
    # reported reload failure - not a generic error with no data.
    entry = _entry(options={})
    hass = _hass(entry)
    hass.config_entries.reload_fails = True
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert conn.error is None
    msg_id, payload = conn.result
    assert msg_id == 1
    assert payload["schedules"][0]["name"] == "X"
    assert entry.options["schedules"] == payload["schedules"]
    assert payload["reload_failed"] == "Saved, but Plejd failed to reload; try again"


async def test_add_returns_error_when_the_save_itself_fails():
    # Unlike a reload failure, nothing was persisted here - async_update_entry() itself
    # raised, before any reload was even attempted - so this must be a genuine error.
    entry = _entry(options={})
    hass = _hass(entry)
    hass.config_entries.update_fails = True
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert conn.error == (1, "save_failed", "Could not save")
    assert conn.result is None
    assert not schedule_ws.async_get_reload_lock(hass, entry.entry_id).locked()


async def test_add_returns_persisted_schedules_when_reload_reports_failure():
    # async_reload() can return False (e.g. setup entered retry) instead of raising. Options
    # are already persisted at that point, so the response must still carry them - not just an
    # error - or a dashboard retry would add a second, duplicate schedule (issue #94 thread 1).
    entry = _entry(options={})
    hass = _hass(entry)
    hass.config_entries.reload_ok = False
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert conn.error is None
    msg_id, payload = conn.result
    assert msg_id == 1
    assert payload["schedules"][0]["name"] == "X"
    assert entry.options["schedules"] == payload["schedules"]
    assert payload["reload_failed"] == "Saved, but Plejd failed to reload; try again"
    assert not schedule_ws.async_get_reload_lock(hass, entry.entry_id).locked()


async def test_add_does_not_double_reload_via_update_listener():
    # The reload lock must be released once persist finishes so the entry's update
    # listener (_async_reload_entry) reloads normally on the next unrelated options change.
    entry = _entry(options={})
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert not schedule_ws.async_get_reload_lock(hass, entry.entry_id).locked()


async def test_add_runs_follow_up_reload_when_a_listener_was_left_pending():
    # _async_reload_entry marks DATA_RELOAD_PENDING when it suppressed its own reload for a
    # concurrent, unrelated options change while this save's reload was in flight (issue #94
    # thread 2). That change must still get a reload once ours is done, not be dropped.
    entry = _entry(options={})
    hass = _hass(entry)
    hass.data[DATA_RELOAD_PENDING] = "e1"
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert conn.result[1]["schedules"][0]["name"] == "X"
    assert hass.config_entries.reload_calls == ["e1", "e1"]
    assert DATA_RELOAD_PENDING not in hass.data


async def test_add_ignores_pending_reload_marked_for_a_different_entry():
    entry = _entry(options={})
    hass = _hass(entry)
    hass.data[DATA_RELOAD_PENDING] = "some-other-entry"
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert hass.config_entries.reload_calls == ["e1"]
    assert hass.data[DATA_RELOAD_PENDING] == "some-other-entry"


# ── async_reload_entry_with_lock ────────────────────────────────────────────


def _lock_test_hass_entry():
    entry = types.SimpleNamespace(entry_id="e1", data={})
    hass = types.SimpleNamespace(
        data={},
        config_entries=types.SimpleNamespace(
            async_update_entry=lambda e, data: setattr(e, "data", data),
            async_reload=AsyncMock(),
        ),
    )
    return hass, entry


async def test_reload_entry_with_lock_raises_when_reload_raises():
    hass, entry = _lock_test_hass_entry()
    hass.config_entries.async_reload = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(HomeAssistantError, match="failed to reload after a test update"):
        await schedule_ws.async_reload_entry_with_lock(
            hass, entry, lambda e: {**e.data, "x": 1}, error_context="a test update"
        )


async def test_reload_entry_with_lock_logs_when_the_follow_up_reload_raises(caplog):
    hass, entry = _lock_test_hass_entry()
    hass.data[DATA_RELOAD_PENDING] = "e1"
    calls: list[str] = []

    async def _reload(entry_id):
        calls.append(entry_id)
        if len(calls) == 2:  # the follow-up reload
            raise RuntimeError("boom")
        return True

    hass.config_entries.async_reload = AsyncMock(side_effect=_reload)
    await schedule_ws.async_reload_entry_with_lock(
        hass, entry, lambda e: {**e.data, "x": 1}, error_context="a test update"
    )
    assert calls == ["e1", "e1"]
    assert "follow-up reload for a concurrent change" in caplog.text
    # a rejected/raised follow-up must stay pending rather than be dropped
    assert hass.data[DATA_RELOAD_PENDING] == "e1"


async def test_add_logs_and_continues_when_follow_up_reload_fails(caplog):
    class _FlakyFollowUp:
        def __init__(self):
            self.calls = 0

        def async_update_entry(self, entry, *, options):
            entry.options = options

        async def async_reload(self, entry_id):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("follow-up reload failed")
            return True

    entry = _entry(options={})
    hass = _hass(entry)
    hass.config_entries = _FlakyFollowUp()
    hass.data[DATA_RELOAD_PENDING] = "e1"
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert conn.result[1]["schedules"][0]["name"] == "X"
    assert hass.config_entries.calls == 2
    # A failed follow-up stays pending rather than being dropped: it belongs to a concurrent
    # change, so losing it would silently discard someone else's edit.
    assert hass.data[DATA_RELOAD_PENDING] == "e1"
    assert "follow-up reload" in caplog.text


async def test_add_resets_stale_gateway_transport_when_no_gateway():
    entry = _entry(options={"transport": "gateway"})  # entry.data has no gateways/resource_set_id
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert entry.options["transport"] == "auto"


async def test_add_preserves_transport_when_gateway_present():
    entry = _entry(options={"transport": "gateway"})
    entry.data["gateways"] = ["GWY-1"]
    entry.data["resource_set_id"] = "rs1"
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_add(hass, conn, {"id": 1, "name": "X", "days": [0], "time": "06:00", "scene": 3, "fade": 0})
    assert entry.options["transport"] == "gateway"


# ── delete ───────────────────────────────────────────────────────────────────


async def test_delete_clears_device_event_and_reloads():
    coordinator = _Coordinator()
    entry = _entry(options={"schedules": [_SCHEDULE]}, runtime_data=coordinator)
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_delete(hass, conn, {"id": 2, "schedule_id": 0})
    assert conn.result == (2, {"schedules": []})
    assert entry.options["schedules"] == []
    assert coordinator.removed == [1]
    assert hass.config_entries.reloaded == "e1"


async def test_delete_persists_even_if_mesh_write_fails():
    coordinator = _Coordinator(fails=True)
    entry = _entry(options={"schedules": [_SCHEDULE]}, runtime_data=coordinator)
    conn = _Conn()
    await schedule_ws.ws_delete(_hass(entry), conn, {"id": 2, "schedule_id": 0})
    assert conn.result == (2, {"schedules": []})
    assert entry.options["schedules"] == []


async def test_delete_is_best_effort_when_mesh_unavailable():
    # runtime_data None -> async_remove_time_event raises AttributeError, swallowed.
    entry = _entry(options={"schedules": [_SCHEDULE]})
    conn = _Conn()
    await schedule_ws.ws_delete(_hass(entry), conn, {"id": 2, "schedule_id": 0})
    assert conn.result == (2, {"schedules": []})


async def test_delete_errors_when_not_loaded():
    conn = _Conn()
    await schedule_ws.ws_delete(_hass(), conn, {"id": 2, "schedule_id": 0})
    assert conn.error == (2, "not_loaded", "Plejd is not loaded")
    assert conn.result is None


async def test_delete_errors_when_schedule_not_found():
    entry = _entry(options={"schedules": [_SCHEDULE]})
    conn = _Conn()
    await schedule_ws.ws_delete(_hass(entry), conn, {"id": 2, "schedule_id": 999})
    assert conn.error == (2, "not_found", "Schedule not found")
    assert conn.result is None


async def test_delete_returns_persisted_schedules_when_reload_raises():
    coordinator = _Coordinator()
    entry = _entry(options={"schedules": [_SCHEDULE]}, runtime_data=coordinator)
    hass = _hass(entry)
    hass.config_entries.reload_fails = True
    conn = _Conn()
    await schedule_ws.ws_delete(hass, conn, {"id": 2, "schedule_id": 0})
    assert conn.error is None
    assert conn.result == (
        2,
        {"schedules": [], "reload_failed": "Saved, but Plejd failed to reload; try again"},
    )


async def test_delete_returns_persisted_schedules_when_reload_reports_failure():
    coordinator = _Coordinator()
    entry = _entry(options={"schedules": [_SCHEDULE]}, runtime_data=coordinator)
    hass = _hass(entry)
    hass.config_entries.reload_ok = False
    conn = _Conn()
    await schedule_ws.ws_delete(hass, conn, {"id": 2, "schedule_id": 0})
    assert conn.error is None
    assert conn.result == (
        2,
        {"schedules": [], "reload_failed": "Saved, but Plejd failed to reload; try again"},
    )
    assert entry.options["schedules"] == []


async def test_delete_resets_stale_gateway_transport_when_no_gateway():
    coordinator = _Coordinator()
    entry = _entry(options={"schedules": [_SCHEDULE], "transport": "gateway"}, runtime_data=coordinator)
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_delete(hass, conn, {"id": 2, "schedule_id": 0})
    assert entry.options["transport"] == "auto"


async def test_delete_preserves_concurrent_edit_made_during_mesh_clear():
    # A schedule added by another WS call while we awaited async_remove_time_event() must
    # survive the delete's persist instead of being overwritten by the pre-await snapshot.
    other = {"id": 5, "slot": 2, "name": "Other", "days": [1], "time": "07:00:00", "scene": 3, "fade": 0}

    class _ConcurrentCoordinator:
        def __init__(self, entry):
            self.entry = entry

        async def async_remove_time_event(self, slot):
            # Simulate a concurrent ws_add completing (and persisting) mid-await.
            self.entry.options = {**self.entry.options, "schedules": [*self.entry.options["schedules"], other]}

    entry = _entry(options={"schedules": [_SCHEDULE]})
    entry.runtime_data = _ConcurrentCoordinator(entry)
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_delete(hass, conn, {"id": 2, "schedule_id": 0})
    assert entry.options["schedules"] == [other]
    assert conn.result == (2, {"schedules": [other]})


# ── registration ─────────────────────────────────────────────────────────────


def test_async_register_registers_all_commands():
    hass = _hass()
    schedule_ws.async_register(hass)
    registered = hass.data["ws_commands"]
    assert schedule_ws.ws_list in registered
    assert schedule_ws.ws_add in registered
    assert schedule_ws.ws_delete in registered
    assert schedule_ws.ws_settings_get in registered
    assert schedule_ws.ws_settings_set in registered
    assert schedule_ws.ws_devices_scan in registered


# ── #125: payloads are built under the lock, not from a pre-lock snapshot ─────


async def test_add_does_not_overwrite_a_schedule_added_while_it_waited_for_the_lock():
    # The headline #125 race: two adds arrive together. The second waits on the reload lock,
    # and if it built its payload before waiting it would write a list that never contained
    # the first one's schedule - AND hand out the same free slot and the same next id.
    entry = _entry(options={})
    hass = _hass(entry)
    conn = _Conn()

    lock = schedule_ws.async_get_reload_lock(hass, entry.entry_id)
    await lock.acquire()
    add = asyncio.ensure_future(
        schedule_ws.ws_add(hass, conn, {"id": 1, "name": "second", "days": [0], "time": "07:00", "scene": 3, "fade": 0})
    )
    await asyncio.sleep(0)  # let it reach the lock and block there
    # the "first" add completes while we hold the lock
    entry.options = {
        **entry.options,
        "schedules": [{"id": 0, "slot": 0, "name": "first", "days": [1], "time": "06:00", "scene": 3, "fade": 0}],
        "next_schedule_id": 1,
    }
    lock.release()
    await asyncio.gather(add)

    saved = entry.options["schedules"]
    assert [s["name"] for s in saved] == ["first", "second"]  # neither lost
    assert [s["slot"] for s in saved] == [0, 1]  # no slot reuse
    assert [s["id"] for s in saved] == [0, 1]  # no id reuse
    assert entry.options["next_schedule_id"] == 2


async def test_delete_does_not_resurrect_a_schedule_added_while_it_waited_for_the_lock():
    # Mirror case: a delete that built its `kept` list pre-lock would write back a list
    # missing the schedule another edit added in the meantime.
    keep_me = {"id": 5, "slot": 2, "name": "added meanwhile", "days": [0], "time": "09:00", "scene": 3, "fade": 0}
    entry = _entry(options={"schedules": [_SCHEDULE]})
    hass = _hass(entry)
    conn = _Conn()

    lock = schedule_ws.async_get_reload_lock(hass, entry.entry_id)
    await lock.acquire()
    delete = asyncio.ensure_future(schedule_ws.ws_delete(hass, conn, {"id": 1, "schedule_id": _SCHEDULE["id"]}))
    await asyncio.sleep(0)
    entry.options = {**entry.options, "schedules": [_SCHEDULE, keep_me]}
    lock.release()
    await asyncio.gather(delete)

    assert entry.options["schedules"] == [keep_me]  # target removed, the new one survives


# ── settings ─────────────────────────────────────────────────────────────────


def _gateway_entry(options=None):
    entry = _entry(options=options)
    entry.data.update(gateways=[{"deviceId": "gw"}], resource_set_id="rs")
    return entry


async def test_settings_get_returns_defaults():
    conn = _Conn()
    await schedule_ws.ws_settings_get(_hass(_entry()), conn, {"id": 1})
    assert conn.result == (
        1,
        {
            "transport": "auto",
            "has_gateway": False,
            "holiday_lights": [],
            "holiday_window_start": "18:00",
            "holiday_window_end": "23:00",
            "sync_areas": False,
        },
    )


async def test_settings_get_errors_when_not_loaded():
    conn = _Conn()
    await schedule_ws.ws_settings_get(_hass(), conn, {"id": 1})
    assert conn.error == (1, "not_loaded", "Plejd is not loaded")


async def test_settings_set_persists_holiday_transport_and_area_sync_then_reloads():
    entry = _gateway_entry(options={"schedules": [_SCHEDULE], "transport": "auto"})
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_settings_set(
        hass,
        conn,
        {
            "id": 2,
            "transport": "ble",
            "holiday_lights": ["light.hall"],
            "holiday_window_start": "19:30:00",
            "holiday_window_end": "01:00",
            "sync_areas": True,
        },
    )
    assert conn.result == (2, {})
    assert entry.options == {
        "schedules": [_SCHEDULE],
        "transport": "ble",
        "holiday_lights": ["light.hall"],
        "holiday_window_start": "19:30",
        "holiday_window_end": "01:00",
        "sync_areas": True,
    }
    assert hass.config_entries.reloaded == "e1"


async def test_settings_set_forces_auto_transport_without_gateway():
    entry = _entry(options={})
    conn = _Conn()
    await schedule_ws.ws_settings_set(_hass(entry), conn, {"id": 3, "transport": "gateway"})
    assert entry.options["transport"] == "auto"


async def test_settings_set_rejects_invalid_time_without_saving():
    entry = _entry(options={})
    hass = _hass(entry)
    conn = _Conn()
    await schedule_ws.ws_settings_set(hass, conn, {"id": 4, "holiday_window_end": "25:00"})
    assert conn.error == (4, "invalid_time", "Invalid time")
    assert hass.config_entries.updated is None


async def test_settings_set_errors_when_not_loaded():
    conn = _Conn()
    await schedule_ws.ws_settings_set(_hass(), conn, {"id": 5})
    assert conn.error == (5, "not_loaded", "Plejd is not loaded")


# ── device scan ──────────────────────────────────────────────────────────────


async def test_devices_scan_lists_unprovisioned(monkeypatch):
    found = [{"address": "AA", "model": "DIM-01", "rssi": -60}]
    monkeypatch.setattr(schedule_ws, "async_bluetooth_available", lambda hass: True)
    monkeypatch.setattr(schedule_ws, "async_scan_unprovisioned", lambda hass: found)
    conn = _Conn()
    await schedule_ws.ws_devices_scan(_hass(), conn, {"id": 6})
    assert conn.result[1]["bluetooth"] is True
    assert conn.result[1]["devices"] == found
    assert "Kitchen" in conn.result[1]["room_categories"]


async def test_devices_scan_without_bluetooth_returns_nothing(monkeypatch):
    monkeypatch.setattr(schedule_ws, "async_bluetooth_available", lambda hass: False)
    conn = _Conn()
    await schedule_ws.ws_devices_scan(_hass(), conn, {"id": 7})
    assert conn.result[1]["bluetooth"] is False
    assert conn.result[1]["devices"] == []
