"""Tests for holiday mode (presence simulation)."""

from __future__ import annotations

import asyncio
import random
import types
from datetime import datetime, time, timedelta, timezone

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from plejd_cloud import holiday_mode as hm
from plejd_cloud.const import CONF_HOLIDAY_LIGHTS, CONF_HOLIDAY_WINDOW_END, CONF_HOLIDAY_WINDOW_START
from plejd_cloud.holiday_mode import STORE_KEY, PlejdHolidayMode, _in_window, _parse_hhmm


class _FakeRandom:
    """A deterministic stand-in for random.Random, injected at the manager's rng boundary."""

    def __init__(self, sample_result, uniform_value=15.0):
        self._sample_result = sample_result
        self._uniform_value = uniform_value
        self.sample_calls: list[tuple[list[str], int]] = []
        self.uniform_calls: list[tuple[float, float]] = []

    def sample(self, population, k):
        self.sample_calls.append((list(population), k))
        return list(self._sample_result[:k])

    def uniform(self, a, b):
        self.uniform_calls.append((a, b))
        return self._uniform_value


class _Services:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))


class _FlakyServices:
    """Raises once for (service, entity_id) pairs in `fail_first`, then succeeds."""

    def __init__(self, fail_first=None):
        self.calls: list[tuple[str, str, dict]] = []
        self._fail_first = set(fail_first or ())

    async def async_call(self, domain, service, data, blocking=False):
        key = (service, data.get("entity_id"))
        if key in self._fail_first:
            self._fail_first.discard(key)
            raise RuntimeError("simulated transient failure")
        self.calls.append((domain, service, data))


class _AlwaysFailingServices:
    """Every call raises (simulates a permanently unreachable light)."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain, service, data, blocking=False):
        raise RuntimeError("permanently unavailable")


class _States:
    """Minimal hass.states stand-in: reports "on" for a fixed set of entities."""

    def __init__(self, on_entities=None):
        self._on = set(on_entities or [])

    def get(self, entity_id):
        return types.SimpleNamespace(state="on") if entity_id in self._on else None


def _hass(entity_registry=None, states_on=None):
    h = types.SimpleNamespace(services=_Services(), states=_States(states_on), data={})
    if entity_registry is not None:
        h.entity_registry = entity_registry
    return h


def _entry(options=None, entry_id="e1"):
    return types.SimpleNamespace(options=options or {}, entry_id=entry_id)


def _registry(entity_to_entry: dict[str, str], disabled: set[str] | None = None):
    disabled = disabled or set()
    entities = {
        eid: types.SimpleNamespace(
            entity_id=eid, config_entry_id=cid, disabled_by=("user" if eid in disabled else None)
        )
        for eid, cid in entity_to_entry.items()
    }
    return er.EntityRegistry(entities)


def _local(hour, minute):
    return datetime(2026, 7, 19, hour, minute, tzinfo=timezone(timedelta(hours=2)))


def _manager(hass, entry, *, rng=None):
    """A PlejdHolidayMode marked as already running (as if async_start() had been called).

    In production, _async_tick/_async_apply only ever run via the registered interval,
    so `_unsub` is always set by the time they fire. Most tests below exercise that tick
    behavior directly, without going through async_start() first.
    """
    manager = PlejdHolidayMode(hass, entry, rng=rng)
    manager._unsub = lambda: None
    return manager


# ── _in_window / _parse_hhmm (pure helpers) ────────────────────────────────────


def test_in_window_normal_range():
    assert _in_window(time(19, 0), time(18, 0), time(23, 0)) is True
    assert _in_window(time(12, 0), time(18, 0), time(23, 0)) is False
    assert _in_window(time(18, 0), time(18, 0), time(23, 0)) is True  # inclusive start
    assert _in_window(time(23, 0), time(18, 0), time(23, 0)) is False  # exclusive end


def test_in_window_crosses_midnight():
    assert _in_window(time(23, 30), time(22, 0), time(2, 0)) is True
    assert _in_window(time(1, 0), time(22, 0), time(2, 0)) is True
    assert _in_window(time(12, 0), time(22, 0), time(2, 0)) is False


def test_parse_hhmm_ignores_seconds():
    assert _parse_hhmm("18:30:15") == time(18, 30)
    assert _parse_hhmm("07:05") == time(7, 5)


# ── target light resolution ─────────────────────────────────────────────────────


def test_configured_lights_take_priority_over_fallback():
    manager = PlejdHolidayMode(_hass(), _entry(options={CONF_HOLIDAY_LIGHTS: ["light.x", "light.y"]}))
    assert manager._target_lights() == ["light.x", "light.y"]


def test_falls_back_to_all_plejd_lights_when_none_configured():
    registry = _registry(
        {
            "light.kitchen": "e1",
            "light.hall": "e1",
            "switch.pump": "e1",  # non-light domain, excluded
            "light.other_entry": "e2",  # different config entry, excluded
        }
    )
    manager = PlejdHolidayMode(_hass(entity_registry=registry), _entry(options={}, entry_id="e1"))
    assert sorted(manager._target_lights()) == ["light.hall", "light.kitchen"]


def test_falls_back_excludes_disabled_light_entities():
    # A disabled entity has no live state to control and shouldn't count toward the quota.
    registry = _registry({"light.kitchen": "e1", "light.disabled": "e1"}, disabled={"light.disabled"})
    manager = PlejdHolidayMode(_hass(entity_registry=registry), _entry(options={}, entry_id="e1"))
    assert manager._target_lights() == ["light.kitchen"]


def test_window_uses_defaults_when_unset():
    manager = PlejdHolidayMode(_hass(), _entry(options={}))
    assert manager._window() == (time(18, 0), time(23, 0))


def test_window_reads_configured_bounds():
    manager = PlejdHolidayMode(
        _hass(), _entry(options={CONF_HOLIDAY_WINDOW_START: "22:00", CONF_HOLIDAY_WINDOW_END: "02:00"})
    )
    assert manager._window() == (time(22, 0), time(2, 0))


# ── async_start / async_stop ────────────────────────────────────────────────────


async def test_start_registers_recurring_timer_and_is_idempotent(monkeypatch):
    registrations = []

    def _fake_track(hass, action, interval):
        registrations.append((hass, action, interval))
        return lambda: registrations.append("unsub")

    monkeypatch.setattr(hm, "async_track_time_interval", _fake_track)
    manager = PlejdHolidayMode(_hass(), _entry())
    await manager.async_start()
    await manager.async_start()  # idempotent: must not register a second timer
    assert len([r for r in registrations if r != "unsub"]) == 1
    assert manager.is_running is True


async def test_stop_cancels_timer_turns_off_tracked_lights_and_clears_pending_state(monkeypatch):
    unsubbed = []

    def _fake_track(hass, action, interval):
        return lambda: unsubbed.append(True)

    monkeypatch.setattr(hm, "async_track_time_interval", _fake_track)
    hass = _hass()
    manager = PlejdHolidayMode(hass, _entry())
    await manager.async_start()
    manager._on_until["light.a"] = _local(20, 0)

    result = await manager.async_stop()

    assert result is True  # everything it owned was turned off
    assert unsubbed == [True]  # the timer's own unsub was invoked -> no leaked timer
    assert manager.is_running is False
    assert manager._on_until == {}
    assert hass.services.calls == [("light", "turn_off", {"entity_id": "light.a"})]
    assert hass.data[("store", STORE_KEY)] == {}  # cleared deadline persisted too


async def test_stop_without_start_is_a_noop():
    hass = _hass()
    manager = PlejdHolidayMode(hass, _entry())
    assert await manager.async_stop() is True  # must not raise
    assert manager.is_running is False
    assert hass.data[("store", STORE_KEY)] == {}  # persists even with nothing to turn off


def test_default_rng_is_a_real_random_instance():
    manager = PlejdHolidayMode(_hass(), _entry())
    assert isinstance(manager._rng, random.Random)


# ── persistence across a restart ────────────────────────────────────────────────


async def test_start_restores_deadlines_persisted_before_a_restart(monkeypatch):
    monkeypatch.setattr(hm, "async_track_time_interval", lambda hass, action, interval: lambda: None)
    hass = _hass()
    deadline = _local(21, 0)
    hass.data[("store", STORE_KEY)] = {"light.a": deadline.isoformat()}

    manager = PlejdHolidayMode(hass, _entry())
    await manager.async_start()

    assert manager._on_until == {"light.a": deadline}


async def test_restored_deadline_still_expires_and_turns_the_light_off(monkeypatch):
    monkeypatch.setattr(hm, "async_track_time_interval", lambda hass, action, interval: lambda: None)
    hass = _hass()
    deadline = _local(21, 0)
    hass.data[("store", STORE_KEY)] = {"light.a": deadline.isoformat()}
    manager = PlejdHolidayMode(hass, _entry(options={CONF_HOLIDAY_LIGHTS: ["light.a"]}), rng=_FakeRandom([]))
    await manager.async_start()

    await manager._async_apply(deadline + timedelta(minutes=1))

    assert hass.services.calls == [("light", "turn_off", {"entity_id": "light.a"})]


async def test_apply_persists_deadlines_for_the_next_restart():
    hass = _hass()
    options = {CONF_HOLIDAY_LIGHTS: ["light.a"]}
    manager = _manager(hass, _entry(options=options), rng=_FakeRandom(["light.a"], uniform_value=20.0))
    now = _local(20, 0)

    await manager._async_apply(now)

    assert hass.data[("store", STORE_KEY)] == {"light.a": (now + timedelta(minutes=20.0)).isoformat()}


async def test_start_drops_a_malformed_persisted_deadline_without_leaking_the_timer(monkeypatch):
    unsubbed = []
    monkeypatch.setattr(hm, "async_track_time_interval", lambda hass, action, interval: lambda: unsubbed.append(True))
    hass = _hass()
    hass.data[("store", STORE_KEY)] = {"light.a": "not-a-valid-timestamp", "light.b": _local(21, 0).isoformat()}
    manager = PlejdHolidayMode(hass, _entry())

    await manager.async_start()  # must not raise, and must not abandon the timer it just registered

    assert manager._on_until == {"light.b": _local(21, 0)}  # the malformed entry is dropped, not fatal
    assert manager.is_running is True  # the timer is still correctly owned, not leaked


async def test_start_drops_a_timezone_naive_persisted_deadline_without_leaking_the_timer(monkeypatch):
    unsubbed = []
    monkeypatch.setattr(hm, "async_track_time_interval", lambda hass, action, interval: lambda: unsubbed.append(True))
    hass = _hass()
    naive_deadline = datetime(2026, 7, 19, 21, 0)  # no tzinfo, e.g. from a hand-edited store
    hass.data[("store", STORE_KEY)] = {"light.a": naive_deadline.isoformat(), "light.b": _local(21, 0).isoformat()}
    manager = PlejdHolidayMode(hass, _entry())

    await manager.async_start()  # must not raise, and must not abandon the timer it just registered

    assert manager._on_until == {"light.b": _local(21, 0)}  # the naive entry is dropped, not fatal
    assert manager.is_running is True  # the timer is still correctly owned, not leaked


async def test_start_recovers_from_a_load_that_raises():
    hass = _hass()
    manager = PlejdHolidayMode(hass, _entry())

    async def _boom():
        raise OSError("disk full")

    manager._store.async_load = _boom

    await manager.async_start()  # must not raise, and must not abandon the timer it just registered

    assert manager._on_until == {}
    assert manager.is_running is True


async def test_start_recovers_from_a_non_dict_payload():
    hass = _hass()
    hass.data[("store", STORE_KEY)] = ["not", "a", "dict"]
    manager = PlejdHolidayMode(hass, _entry())

    await manager.async_start()  # must not raise

    assert manager._on_until == {}
    assert manager.is_running is True


# ── _async_tick: active-window gating ─────────────────────────────────────────


async def test_tick_outside_active_window_does_nothing(monkeypatch):
    hass = _hass()
    manager = PlejdHolidayMode(hass, _entry(options={CONF_HOLIDAY_LIGHTS: ["light.a"]}), rng=_FakeRandom(["light.a"]))
    monkeypatch.setattr(dt_util, "now", lambda: _local(12, 0))  # noon, outside default 18:00-23:00
    await manager._async_tick(None)
    assert hass.services.calls == []


async def test_tick_inside_active_window_applies(monkeypatch):
    hass = _hass()
    manager = _manager(hass, _entry(options={CONF_HOLIDAY_LIGHTS: ["light.a"]}), rng=_FakeRandom(["light.a"]))
    monkeypatch.setattr(dt_util, "now", lambda: _local(20, 0))
    await manager._async_tick(None)
    assert hass.services.calls == [("light", "turn_on", {"entity_id": "light.a"})]


async def test_tick_respects_configured_window_crossing_midnight(monkeypatch):
    hass = _hass()
    options = {CONF_HOLIDAY_LIGHTS: ["light.a"], CONF_HOLIDAY_WINDOW_START: "22:00", CONF_HOLIDAY_WINDOW_END: "02:00"}
    # uniform_value stays at the default 15.0-minute on-duration, so a light picked at
    # 01:00 (deadline 01:15) is long expired by the next, out-of-window check at noon.
    manager = _manager(hass, _entry(options=options), rng=_FakeRandom(["light.a"]))

    monkeypatch.setattr(dt_util, "now", lambda: _local(1, 0))  # 01:00, inside 22:00-02:00
    await manager._async_tick(None)
    assert hass.services.calls == [("light", "turn_on", {"entity_id": "light.a"})]

    hass.services.calls.clear()
    monkeypatch.setattr(dt_util, "now", lambda: _local(1, 10))  # still inside the window, not yet expired
    await manager._async_tick(None)
    assert hass.services.calls == []

    hass.services.calls.clear()
    monkeypatch.setattr(dt_util, "now", lambda: _local(12, 0))  # noon: outside the window, past the deadline
    await manager._async_tick(None)
    assert hass.services.calls == [("light", "turn_off", {"entity_id": "light.a"})]  # expiry still runs


# ── _async_apply: randomized on/off behavior ──────────────────────────────────


async def test_no_target_lights_does_nothing():
    hass = _hass()
    manager = PlejdHolidayMode(hass, _entry(options={}), rng=_FakeRandom([]))
    await manager._async_apply(_local(20, 0))
    assert hass.services.calls == []


async def test_seeded_rng_drives_which_lights_turn_on_and_for_how_long():
    hass = _hass()
    options = {CONF_HOLIDAY_LIGHTS: ["light.a", "light.b", "light.c"]}
    fake_rng = _FakeRandom(sample_result=["light.b"], uniform_value=20.0)
    manager = _manager(hass, _entry(options=options), rng=fake_rng)
    now = _local(20, 0)

    await manager._async_apply(now)

    assert hass.services.calls == [("light", "turn_on", {"entity_id": "light.b"})]
    assert fake_rng.sample_calls == [(["light.a", "light.b", "light.c"], 1)]  # round(3 * 0.4) -> 1
    assert manager._on_until == {"light.b": now + timedelta(minutes=20.0)}


async def test_randomization_is_reproducible_with_the_same_seed():
    options = {CONF_HOLIDAY_LIGHTS: ["light.a", "light.b", "light.c", "light.d"]}
    manager1 = _manager(_hass(), _entry(options=options), rng=random.Random(42))
    manager2 = _manager(_hass(), _entry(options=options), rng=random.Random(42))
    now = _local(20, 0)

    await manager1._async_apply(now)
    await manager2._async_apply(now)

    assert manager1._hass.services.calls == manager2._hass.services.calls
    assert manager1._on_until == manager2._on_until


async def test_already_on_lights_are_not_picked_again():
    hass = _hass()
    options = {CONF_HOLIDAY_LIGHTS: ["light.a"]}
    manager = _manager(hass, _entry(options=options), rng=_FakeRandom(["light.a"]))
    now = _local(20, 0)
    await manager._async_apply(now)
    hass.services.calls.clear()

    await manager._async_apply(now + timedelta(minutes=1))  # still within its on-duration

    assert hass.services.calls == []  # nothing left to turn on, nothing expired yet


async def test_turns_off_expired_lights_on_a_later_tick():
    hass = _hass()
    options = {CONF_HOLIDAY_LIGHTS: ["light.a"]}
    manager = _manager(hass, _entry(options=options), rng=_FakeRandom(["light.a"], uniform_value=10.0))
    t0 = _local(20, 0)

    await manager._async_apply(t0)
    assert hass.services.calls == [("light", "turn_on", {"entity_id": "light.a"})]
    hass.services.calls.clear()

    await manager._async_apply(t0 + timedelta(minutes=5))  # still within the 10-minute on-duration
    assert hass.services.calls == []

    await manager._async_apply(t0 + timedelta(minutes=11))  # past the on-duration
    # Expired -> turned off, then immediately eligible again -> picked back on with a fresh deadline.
    assert hass.services.calls == [
        ("light", "turn_off", {"entity_id": "light.a"}),
        ("light", "turn_on", {"entity_id": "light.a"}),
    ]
    assert manager._on_until["light.a"] == t0 + timedelta(minutes=21.0)


async def test_skips_lights_already_on_that_holiday_mode_did_not_turn_on():
    # A light already on (by the user, another automation, ...) must not be adopted as
    # "ours" — else a later expiry would turn off a light holiday mode never turned on.
    hass = _hass(states_on=["light.a"])
    options = {CONF_HOLIDAY_LIGHTS: ["light.a", "light.b"]}
    fake_rng = _FakeRandom(["light.b"])
    manager = _manager(hass, _entry(options=options), rng=fake_rng)

    await manager._async_apply(_local(20, 0))

    assert fake_rng.sample_calls == [(["light.b"], 1)]  # light.a excluded from the candidate pool
    assert hass.services.calls == [("light", "turn_on", {"entity_id": "light.b"})]
    assert "light.a" not in manager._on_until


async def test_turns_off_expired_lights_even_outside_the_active_window(monkeypatch):
    # A light turned on near the window's end can have a deadline past it; expiry must
    # still run on every tick, not only while inside the active window.
    hass = _hass()
    options = {CONF_HOLIDAY_LIGHTS: ["light.a"]}
    manager = _manager(hass, _entry(options=options), rng=_FakeRandom(["light.a"], uniform_value=45.0))

    monkeypatch.setattr(dt_util, "now", lambda: _local(22, 50))  # inside 18:00-23:00
    await manager._async_tick(None)  # on-duration deadline: 22:50 + 45min = 23:35
    assert hass.services.calls == [("light", "turn_on", {"entity_id": "light.a"})]
    hass.services.calls.clear()

    monkeypatch.setattr(dt_util, "now", lambda: _local(23, 30))  # outside the window, not yet expired
    await manager._async_tick(None)
    assert hass.services.calls == []

    monkeypatch.setattr(dt_util, "now", lambda: _local(23, 40))  # outside the window, past the deadline
    await manager._async_tick(None)
    assert hass.services.calls == [("light", "turn_off", {"entity_id": "light.a"})]


# ── retry-safety: a failing service call must not corrupt tracking ─────────────


async def test_turn_off_failure_keeps_the_light_tracked_for_retry():
    hass = _hass()
    options = {CONF_HOLIDAY_LIGHTS: ["light.a"]}
    fake_rng = _FakeRandom(["light.a"], uniform_value=10.0)
    manager = _manager(hass, _entry(options=options), rng=fake_rng)
    t0 = _local(20, 0)
    await manager._async_apply(t0)  # turns on light.a, deadline t0+10min
    hass.services.calls.clear()

    hass.services = _FlakyServices(fail_first={("turn_off", "light.a")})
    fake_rng._sample_result = []  # isolate the expiry retry from picking a new light this tick
    await manager._async_apply(t0 + timedelta(minutes=11))  # turn_off raises the first time
    assert hass.services.calls == []
    assert manager._on_until == {"light.a": t0 + timedelta(minutes=10)}  # still tracked, not lost

    await manager._async_apply(t0 + timedelta(minutes=12))  # retried on the next tick, now succeeds
    assert hass.services.calls == [("light", "turn_off", {"entity_id": "light.a"})]
    assert manager._on_until == {}


async def test_turn_on_failure_does_not_mark_the_light_as_owned():
    hass = _hass()
    hass.services = _FlakyServices(fail_first={("turn_on", "light.a")})
    options = {CONF_HOLIDAY_LIGHTS: ["light.a"]}
    manager = _manager(hass, _entry(options=options), rng=_FakeRandom(["light.a"], uniform_value=10.0))
    t0 = _local(20, 0)

    await manager._async_apply(t0)  # turn_on raises -> must not be tracked
    assert hass.services.calls == []
    assert manager._on_until == {}

    await manager._async_apply(t0 + timedelta(minutes=1))  # retried on the next tick, now succeeds
    assert hass.services.calls == [("light", "turn_on", {"entity_id": "light.a"})]
    assert manager._on_until == {"light.a": (t0 + timedelta(minutes=1)) + timedelta(minutes=10)}


# ── async_stop(): bounded retry on cleanup failure ──────────────────────────────


async def test_stop_retries_a_failed_turn_off_once():
    hass = _hass()
    hass.services = _FlakyServices(fail_first={("turn_off", "light.a")})
    manager = PlejdHolidayMode(hass, _entry())
    manager._on_until["light.a"] = _local(20, 0)

    result = await manager.async_stop()

    assert result is True  # the retry succeeded, so nothing is left stranded
    assert hass.services.calls == [("light", "turn_off", {"entity_id": "light.a"})]  # succeeded on retry
    assert manager._on_until == {}


async def test_stop_gives_up_after_exhausting_retries_but_keeps_tracking():
    hass = _hass()
    hass.services = _AlwaysFailingServices()
    manager = PlejdHolidayMode(hass, _entry())
    manager._on_until["light.a"] = _local(20, 0)

    result = await manager.async_stop()

    assert result is False  # a caller acting on this must not report success (#89 review)
    assert manager._on_until == {"light.a": _local(20, 0)}  # not silently lost
    assert manager.is_running is False


async def test_stop_treats_a_persist_failure_as_best_effort():
    class _BrokenStore:
        async def async_save(self, data):
            raise OSError("simulated disk-full error")

    hass = _hass()
    manager = PlejdHolidayMode(hass, _entry())
    manager._store = _BrokenStore()
    manager._on_until["light.a"] = _local(20, 0)

    result = await manager.async_stop()  # must not raise despite the failed persist (#89 review)

    assert result is True  # the light was still turned off; only the save failed
    assert manager._on_until == {}
    assert hass.services.calls == [("light", "turn_off", {"entity_id": "light.a"})]


async def test_tick_is_a_noop_once_unsub_has_been_cleared_by_stop(monkeypatch):
    # Models a tick invocation that was already queued (fired before async_stop()
    # cancelled the timer) but only gets to acquire the lock after stop already ran
    # to completion (#89 review) — it must not turn any light back on with no timer
    # left to ever expire it.
    monkeypatch.setattr(dt_util, "now", lambda: _local(20, 0))  # inside the default window
    options = {CONF_HOLIDAY_LIGHTS: ["light.a"]}
    manager = _manager(_hass(), _entry(options=options), rng=_FakeRandom(["light.a"]))
    manager._unsub = None  # as if async_stop() had already run

    await manager._async_tick(None)

    assert manager._hass.services.calls == []
    assert manager._on_until == {}


# ── race conditions: stop-while-in-flight, overlapping starts ──────────────────


async def test_concurrent_starts_register_only_one_timer(monkeypatch):
    registrations = []

    def _fake_track(hass, action, interval):
        registrations.append(True)
        return lambda: None

    monkeypatch.setattr(hm, "async_track_time_interval", _fake_track)

    class _SlowStore:
        async def async_load(self):
            await asyncio.sleep(0)  # yield control, simulating a slow disk read
            return {}

        async def async_save(self, data):
            return None

    manager = PlejdHolidayMode(_hass(), _entry())
    manager._store = _SlowStore()

    await asyncio.gather(manager.async_start(), manager.async_start())

    assert registrations == [True]  # the lock serialized them: the second saw _unsub already set


async def test_a_concurrent_stop_waits_for_an_in_flight_tick_to_fully_finish(monkeypatch):
    # async_start()/async_stop()/each tick's apply share one lock, so they can never
    # interleave — this is what makes the whole earlier family of races (a tick
    # persisting after stop already cleared the store, a light adopted right as stop
    # cancels the timer, ...) structurally impossible, instead of needing a bespoke
    # check for each one.
    monkeypatch.setattr(dt_util, "now", lambda: _local(20, 0))  # inside the default 18:00-23:00 window
    options = {CONF_HOLIDAY_LIGHTS: ["light.a"]}
    manager = _manager(_hass(), _entry(options=options), rng=_FakeRandom(["light.a"], uniform_value=10.0))
    release_turn_on = asyncio.Event()

    class _BlockingServices:
        def __init__(self):
            self.calls: list[tuple[str, str, dict]] = []

        async def async_call(self, domain, service, data, blocking=False):
            self.calls.append((domain, service, data))
            if service == "turn_on":
                await release_turn_on.wait()  # held open until the test explicitly releases it

    manager._hass.services = _BlockingServices()

    tick_task = asyncio.ensure_future(manager._async_tick(None))
    await asyncio.sleep(0)  # let the tick acquire the lock and reach the blocked turn_on call

    stop_task = asyncio.ensure_future(manager.async_stop())
    await asyncio.sleep(0)
    assert not stop_task.done()  # blocked: waiting for the tick to release the lock

    release_turn_on.set()  # let the tick's turn_on call return, and the tick finish
    await tick_task
    await stop_task

    assert manager._hass.services.calls[-1] == ("light", "turn_off", {"entity_id": "light.a"})
    assert manager._on_until == {}  # stop cleaned up the light the tick had just turned on
