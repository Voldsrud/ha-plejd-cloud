"""Tests for remote → light dim bindings (generic ramp + trigger-attach engine)."""

from __future__ import annotations

import asyncio
import types

import pytest
from homeassistant.helpers import entity_registry as er
from plejd_cloud import bindings as bindings_mod
from plejd_cloud.bindings import DimRamp, PlejdDimBindings


async def _raise_disk_full(_data):
    raise OSError("disk full")


class _Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))


def _hass(data=None):
    return types.SimpleNamespace(services=_Services(), data=data if data is not None else {})


def _hass_with_entities(entities):
    """A hass whose entity registry maps entity_id -> integration platform."""
    registry = er.EntityRegistry({eid: types.SimpleNamespace(platform=platform) for eid, platform in entities.items()})
    return types.SimpleNamespace(services=_Services(), data={}, entity_registry=registry)


async def _noop_sleep(_seconds):
    return None


def _fake_monotonic(step=1.0):
    """A deterministic monotonic clock that advances `step` per call."""
    now = [0.0]

    def _mono():
        value = now[0]
        now[0] += step
        return value

    return _mono


# ── DimRamp ───────────────────────────────────────────────────────────────────


async def test_ramp_up_steps_brightness_up(monkeypatch):
    monkeypatch.setattr(bindings_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(bindings_mod.time, "monotonic", _fake_monotonic(1))  # 0,1,2,3,…
    hass = _hass()
    ramp = DimRamp(hass, step_pct=5, interval=1, max_duration=4)  # deadline 4 → 3 ticks
    ramp.start("b1", {"entity_id": ["light.a"]}, "up")
    await ramp._tasks["b1"]
    await asyncio.sleep(0)
    assert len(hass.services.calls) == 3
    assert hass.services.calls[0] == ("light", "turn_on", {"entity_id": ["light.a"], "brightness_step_pct": 5})


async def test_ramp_down_uses_negative_step(monkeypatch):
    monkeypatch.setattr(bindings_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(bindings_mod.time, "monotonic", _fake_monotonic(1))
    hass = _hass()
    ramp = DimRamp(hass, step_pct=7, interval=1, max_duration=3)
    ramp.start("b1", {"area_id": ["kitchen"]}, "down")
    await ramp._tasks["b1"]
    await asyncio.sleep(0)
    assert hass.services.calls[0] == ("light", "turn_on", {"area_id": ["kitchen"], "brightness_step_pct": -7})


async def test_ramp_logs_and_forgets_when_service_call_fails(monkeypatch):
    monkeypatch.setattr(bindings_mod.time, "monotonic", _fake_monotonic(1))
    hass = _hass()

    async def _boom(domain, service, data, blocking=False):
        raise RuntimeError("light unavailable")

    hass.services.async_call = _boom
    logged = []
    monkeypatch.setattr(bindings_mod._LOGGER, "warning", lambda *a, **k: logged.append((a, k)))
    ramp = DimRamp(hass, interval=1, max_duration=4)
    ramp.start("b1", {"entity_id": ["light.a"]}, "up")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "b1" not in ramp._tasks  # forgotten, exception retrieved (no "never retrieved" warning)
    assert logged
    exc_info = logged[0][1]["exc_info"]
    assert isinstance(exc_info, tuple)
    assert exc_info[0] is RuntimeError
    assert isinstance(exc_info[1], RuntimeError)
    assert exc_info[1].args == ("light unavailable",)
    assert exc_info[2] is not None


async def test_ramp_empty_target_is_noop():
    hass = _hass()
    ramp = DimRamp(hass)
    ramp.start("b1", {}, "up")
    assert "b1" not in ramp._tasks and hass.services.calls == []


async def test_ramp_stop_cancels():
    hass = _hass()
    ramp = DimRamp(hass, interval=3600)  # parks at the sleep after the first call
    ramp.start("b1", {"entity_id": ["light.a"]}, "up")
    await asyncio.sleep(0)
    task = ramp._tasks["b1"]
    ramp.stop("b1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert task.cancelled()
    assert "b1" not in ramp._tasks
    assert len(hass.services.calls) == 1  # one step before parking


async def test_ramp_stop_unknown_is_noop():
    DimRamp(_hass()).stop("nope")  # must not raise


async def test_ramp_restart_replaces_task():
    hass = _hass()
    ramp = DimRamp(hass, interval=3600)
    ramp.start("b1", {"entity_id": ["light.a"]}, "up")
    await asyncio.sleep(0)
    first = ramp._tasks["b1"]
    ramp.start("b1", {"entity_id": ["light.a"]}, "down")
    await asyncio.sleep(0)
    assert first.cancelled() and ramp._tasks["b1"] is not first
    ramp.shutdown()


async def test_ramp_shutdown_cancels_all():
    hass = _hass()
    ramp = DimRamp(hass, interval=3600)
    ramp.start("b1", {"entity_id": ["light.a"]}, "up")
    ramp.start("b2", {"entity_id": ["light.b"]}, "up")
    await asyncio.sleep(0)
    tasks = [ramp._tasks["b1"], ramp._tasks["b2"]]
    ramp.shutdown()
    await asyncio.sleep(0)
    assert all(t.cancelled() for t in tasks) and ramp._tasks == {}


async def test_ramp_uses_hass_background_task_when_available():
    created = []

    def _create(coro, name):
        created.append(name)
        return asyncio.ensure_future(coro)

    hass = types.SimpleNamespace(services=_Services(), async_create_background_task=_create, interval=3600)
    ramp = DimRamp(hass, interval=3600)
    ramp.start("b1", {"entity_id": ["light.a"]}, "up")
    await asyncio.sleep(0)
    assert created == ["plejd-dim-binding"]
    ramp.shutdown()


# ── PlejdDimBindings ──────────────────────────────────────────────────────────


def _spy_triggers(recorder):
    async def _init(hass, configs, action, domain, name, log_cb, **kwargs):
        recorder.append((configs, action))
        return lambda: recorder.append(("unsub",))

    return _init


class _SpyRamp:
    def __init__(self):
        self.calls = []

    def start(self, key, target, direction):
        self.calls.append(("start", key, target, direction))

    def stop(self, key):
        self.calls.append(("stop", key))

    def shutdown(self):
        self.calls.append(("shutdown",))


async def test_load_attaches_up_down_stop_and_actions_drive_ramp(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    binding = {
        "id": "b1",
        "targets": {"entity_id": ["light.kok"], "area_id": [], "device_id": []},
        "up": {"platform": "device", "type": "brightness_move_up"},
        "down": {"platform": "device", "type": "brightness_move_down"},
        "stop": {"platform": "device", "type": "brightness_stop"},
    }
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [binding]
    pb = PlejdDimBindings(hass)
    pb._ramp = _SpyRamp()
    await pb.async_load()
    assert pb.bindings == [binding]
    assert len(captured) == 3  # up, down, stop each attached
    # invoke each captured action and check it drives the ramp with the resolved target
    target = {"entity_id": ["light.kok"]}
    await captured[0][1]()  # up
    await captured[1][1]()  # down
    await captured[2][1]()  # stop
    assert pb._ramp.calls == [
        ("start", "b1", target, "up"),
        ("start", "b1", target, "down"),
        ("stop", "b1"),
    ]


async def test_replace_persists_and_reattaches(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass()
    pb = PlejdDimBindings(hass)
    await pb.async_load()  # empty
    assert pb.bindings == []
    new = [{"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"x": 1}, "stop": {"s": 1}}]
    await pb.async_replace(new)
    assert pb.bindings == new
    assert hass.data[("store", bindings_mod.STORE_KEY)] == new  # persisted
    assert len(captured) == 2  # the "up" and "stop" triggers attached


async def test_replace_detaches_previous(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"x": 1}, "stop": {"s": 1}}
    ]
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    await pb.async_replace([])  # remove all
    assert ("unsub",) in captured  # the previous trigger was detached


async def test_load_survives_a_bad_binding(monkeypatch):
    calls = []

    async def _init(hass, configs, action, domain, name, log_cb, **kwargs):
        calls.append(configs)
        if configs == [{"bad": 1}]:
            raise ValueError("stale device")
        return lambda: None

    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _init)
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "bad", "targets": {"entity_id": ["light.a"]}, "up": {"bad": 1}, "stop": {"s": 1}},
        {"id": "good", "targets": {"entity_id": ["light.b"]}, "up": {"ok": 1}, "stop": {"s": 1}},
    ]
    pb = PlejdDimBindings(hass)
    await pb.async_load()  # must not raise
    assert [{"ok": 1}] in calls  # the good binding still attached


async def test_shutdown_detaches_and_stops_ramp(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"x": 1}, "stop": {"s": 1}}
    ]
    pb = PlejdDimBindings(hass)
    pb._ramp = _SpyRamp()
    await pb.async_load()
    pb.shutdown()
    assert ("unsub",) in captured and ("shutdown",) in pb._ramp.calls


async def test_attach_rolls_back_partial_binding_on_failure(monkeypatch):
    events = []

    async def _init(hass, configs, action, domain, name, log_cb, **kwargs):
        events.append(("attach", configs))
        if configs == [{"boom": 1}]:
            raise ValueError("stop trigger invalid")

        def _unsub():
            events.append(("unsub", configs))

        return _unsub

    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _init)
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"ok": 1}, "stop": {"boom": 1}},
    ]
    pb = PlejdDimBindings(hass)
    await pb.async_load()  # the per-binding guard logs the failure
    # the already-attached "up" trigger is rolled back, so no half-attached ramp lingers
    assert ("attach", [{"ok": 1}]) in events and ("unsub", [{"ok": 1}]) in events
    assert pb._unsubs == []


async def test_replace_keeps_old_bindings_when_save_fails(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [{"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"x": 1}}]
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    old = pb.bindings
    pb._store.async_save = _raise_disk_full
    with pytest.raises(OSError):
        await pb.async_replace([{"id": "b2", "targets": {"entity_id": ["light.b"]}, "up": {"y": 1}, "stop": {"s": 1}}])
    assert pb.bindings == old  # in-memory bindings untouched, old triggers never detached
    assert ("unsub",) not in captured


async def test_load_stays_empty_when_legacy_id_save_fails(monkeypatch):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers([]))
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [{"targets": {"entity_id": ["light.a"]}}]  # no id → forces a save
    pb = PlejdDimBindings(hass)
    pb._store.async_save = _raise_disk_full
    with pytest.raises(OSError):
        await pb.async_load()
    assert pb.bindings == []  # nothing committed → manager stays empty and consistent


async def test_attach_rolls_back_when_trigger_yields_no_unsub(monkeypatch):
    events = []

    async def _init(hass, configs, action, domain, name, log_cb, **kwargs):
        if configs == [{"nostop": 1}]:
            return None  # HA set up nothing for this (stale/invalid) trigger

        def _unsub():
            events.append(("unsub", configs))

        events.append(("attach", configs))
        return _unsub

    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _init)
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"ok": 1}, "stop": {"nostop": 1}},
    ]
    pb = PlejdDimBindings(hass)
    await pb.async_load()  # per-binding guard logs; the half-attached binding is rolled back
    assert ("unsub", [{"ok": 1}]) in events  # the good "up" trigger was undone
    assert pb._unsubs == []  # nothing left live — no start-without-stop binding


async def test_concurrent_replaces_leave_consistent_triggers(monkeypatch):
    async def _init(hass, configs, action, domain, name, log_cb, **kwargs):
        await asyncio.sleep(0)  # yield: without the lock the two saves would interleave here
        return lambda: None

    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _init)
    hass = _hass()
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    a = [{"id": "a", "targets": {"entity_id": ["light.a"]}, "up": {"x": 1}, "down": {"y": 1}, "stop": {"z": 1}}]
    b = [{"id": "b", "targets": {"entity_id": ["light.b"]}, "up": {"x": 1}, "stop": {"z": 1}}]
    await asyncio.gather(pb.async_replace(a), pb.async_replace(b))
    live = sum(1 for key in ("up", "down", "stop") if pb.bindings[0].get(key))
    assert len(pb._unsubs) == live  # exactly the winner's triggers remain — no stale leak


async def test_plejd_single_light_target_uses_native_dim(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass_with_entities({"light.kok": "plejd_cloud"})
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["light.kok"]}, "up": {"a": 1}, "down": {"b": 1}, "stop": {"c": 1}},
    ]
    pb = PlejdDimBindings(hass)
    pb._ramp = _SpyRamp()
    await pb.async_load()
    await captured[0][1]()  # up
    await captured[2][1]()  # stop
    # a lone Plejd light rides its native ramp (start_dim/stop_dim → active transport), not the generic ramp
    assert hass.services.calls == [
        ("plejd_cloud", "start_dim", {"entity_id": ["light.kok"], "direction": "up"}),
        ("plejd_cloud", "stop_dim", {"entity_id": ["light.kok"]}),
    ]
    assert pb._ramp.calls == []


async def test_plejd_light_target_as_string_uses_native_dim(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass_with_entities({"light.kok": "plejd_cloud"})
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": "light.kok"}, "up": {"a": 1}, "stop": {"c": 1}},  # stored as a string
    ]
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    await captured[0][1]()
    assert hass.services.calls == [("plejd_cloud", "start_dim", {"entity_id": ["light.kok"], "direction": "up"})]


async def test_non_plejd_light_target_uses_generic_ramp(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass_with_entities({"light.hue": "hue"})
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["light.hue"]}, "up": {"a": 1}, "stop": {"c": 1}},
    ]
    pb = PlejdDimBindings(hass)
    pb._ramp = _SpyRamp()
    await pb.async_load()
    await captured[0][1]()
    assert pb._ramp.calls == [("start", "b1", {"entity_id": ["light.hue"]}, "up")]
    assert hass.services.calls == []  # non-Plejd light → generic ramp, no plejd service


async def test_area_target_uses_generic_ramp(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass_with_entities({})
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"area_id": ["kitchen"]}, "up": {"a": 1}, "stop": {"c": 1}},
    ]
    pb = PlejdDimBindings(hass)
    pb._ramp = _SpyRamp()
    await pb.async_load()
    await captured[0][1]()
    assert pb._ramp.calls == [("start", "b1", {"area_id": ["kitchen"]}, "up")]  # area can't use the per-light ramp


async def test_non_light_plejd_entity_uses_generic_ramp(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass_with_entities({"switch.plejd_relay": "plejd_cloud"})
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["switch.plejd_relay"]}, "up": {"a": 1}, "stop": {"c": 1}},
    ]
    pb = PlejdDimBindings(hass)
    pb._ramp = _SpyRamp()
    await pb.async_load()
    await captured[0][1]()
    assert pb._ramp.calls == [("start", "b1", {"entity_id": ["switch.plejd_relay"]}, "up")]  # only lights dim


async def test_replace_rejects_stopless_binding_without_persisting(monkeypatch):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers([]))
    hass = _hass()
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    with pytest.raises(ValueError, match="stop trigger"):
        await pb.async_replace([{"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"x": 1}}])
    assert pb.bindings == []  # not committed in memory
    assert ("store", bindings_mod.STORE_KEY) not in hass.data  # and never persisted


async def test_replace_survives_malformed_previous_binding(monkeypatch):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers([]))
    hass = _hass_with_entities({})
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "old", "targets": {"entity_id": [None]}, "up": {"a": 1}, "stop": {"c": 1}},  # legacy/bad data
    ]
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    # replacing must not raise while inspecting the malformed previous binding for native ramps
    await pb.async_replace([{"id": "new", "targets": {"area_id": ["kitchen"]}, "up": {"x": 1}, "stop": {"s": 1}}])
    assert [b["id"] for b in pb.bindings] == ["new"]


async def test_replace_stops_native_plejd_ramp_of_removed_binding(monkeypatch):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers([]))
    hass = _hass_with_entities({"light.kok": "plejd_cloud"})
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["light.kok"]}, "up": {"a": 1}, "stop": {"c": 1}},
    ]
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    hass.services.calls.clear()
    await pb.async_replace([])  # remove the binding while its native ramp could be running
    assert ("plejd_cloud", "stop_dim", {"entity_id": ["light.kok"]}) in hass.services.calls


async def test_binding_with_start_but_no_stop_is_rejected(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"x": 1}},  # start, no release
    ]
    pb = PlejdDimBindings(hass)
    await pb.async_load()  # per-binding guard logs and skips it
    assert pb._unsubs == [] and captured == []  # no hold-without-release binding attached


async def test_start_action_noops_after_close(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"x": 1}, "stop": {"s": 1}},
    ]
    pb = PlejdDimBindings(hass)
    pb._ramp = _SpyRamp()
    await pb.async_load()
    start_action = captured[0][1]  # the "up" start action
    pb.shutdown()
    await start_action()
    assert not any(c[0] == "start" for c in pb._ramp.calls)  # a trigger firing after teardown starts no ramp


async def test_replace_cancels_live_ramps(monkeypatch):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers([]))
    hass = _hass()
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    pb._ramp.start("b1", {"entity_id": ["light.a"]}, "up")  # a ramp is mid-hold
    await asyncio.sleep(0)
    task = pb._ramp._tasks["b1"]
    await pb.async_replace([{"id": "b2", "targets": {"entity_id": ["light.b"]}, "up": {"x": 1}, "stop": {"s": 1}}])
    await asyncio.sleep(0)
    assert task.cancelled()  # old ramp stopped, so it can't keep stepping to DIM_MAX_DURATION


async def test_replace_after_shutdown_attaches_nothing(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    hass = _hass()
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    pb.shutdown()  # integration unloaded
    await pb.async_replace([{"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"x": 1}, "stop": {"s": 1}}])
    assert pb._unsubs == []  # a closed manager never re-attaches


async def test_replace_undoes_attach_when_shutdown_races(monkeypatch):
    holder = {}

    async def _init(hass, configs, action, domain, name, log_cb, **kwargs):
        holder["pb"].shutdown()  # unload fires while we're awaiting trigger setup
        return lambda: holder.setdefault("unsubbed", []).append(configs)

    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _init)
    hass = _hass()
    pb = PlejdDimBindings(hass)
    holder["pb"] = pb
    await pb.async_load()
    await pb.async_replace([{"id": "b1", "targets": {"entity_id": ["light.a"]}, "up": {"x": 1}, "stop": {"s": 1}}])
    assert pb._unsubs == []  # the attach that completed after shutdown was rolled back
    assert holder.get("unsubbed") == [[{"x": 1}], [{"s": 1}]]  # its stray triggers were unsubscribed


async def test_replace_assigns_missing_ids(monkeypatch):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers([]))
    hass = _hass()
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    await pb.async_replace([{"targets": {"entity_id": ["light.a"]}}])  # no id supplied
    assert pb.bindings[0]["id"]  # assigned
    assert hass.data[("store", bindings_mod.STORE_KEY)][0]["id"]  # and persisted


async def test_replace_reassigns_duplicate_ids(monkeypatch):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers([]))
    hass = _hass()
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    await pb.async_replace(
        [
            {"id": "dup", "targets": {"entity_id": ["light.a"]}},
            {"id": "dup", "targets": {"entity_id": ["light.b"]}},
        ]
    )
    assert pb.bindings[0]["id"] == "dup"
    assert pb.bindings[1]["id"] != "dup"
    assert pb.bindings[0]["id"] != pb.bindings[1]["id"]


async def test_load_persists_ids_assigned_to_legacy_data(monkeypatch):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers([]))
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [{"targets": {"entity_id": ["light.a"]}}]  # no id
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    assert pb.bindings[0]["id"]  # assigned
    assert hass.data[("store", bindings_mod.STORE_KEY)][0]["id"]  # and saved back, so it's stable


async def test_load_persists_reassigned_duplicate_ids(monkeypatch):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers([]))
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "dup", "targets": {"entity_id": ["light.a"]}},
        {"id": "dup", "targets": {"entity_id": ["light.b"]}},
    ]
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    assert pb.bindings[0]["id"] == "dup"
    assert pb.bindings[1]["id"] != "dup"
    assert hass.data[("store", bindings_mod.STORE_KEY)][1]["id"] == pb.bindings[1]["id"]


# ── press actions (any trigger → any action) ──────────────────────────────────


async def _load_one(monkeypatch, hass, binding):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured := []))
    hass.data[("store", bindings_mod.STORE_KEY)] = [binding]
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    return pb, captured


async def test_press_toggle_calls_homeassistant_toggle(monkeypatch):
    hass = _hass()
    _, captured = await _load_one(
        monkeypatch,
        hass,
        {
            "id": "b1",
            "targets": {"entity_id": ["light.a"]},
            "presses": [{"trigger": {"platform": "device", "x": 1}, "action": {"type": "toggle"}}],
        },
    )
    await captured[0][1]()  # fire the press trigger
    assert hass.services.calls == [("homeassistant", "toggle", {"entity_id": ["light.a"]})]


async def test_press_on_and_off_map_to_turn_on_off(monkeypatch):
    hass = _hass()
    _, captured = await _load_one(
        monkeypatch,
        hass,
        {
            "id": "b1",
            "targets": {"area_id": ["kitchen"]},
            "presses": [
                {"trigger": {"platform": "device", "a": 1}, "action": {"type": "on"}},
                {"trigger": {"platform": "device", "b": 1}, "action": {"type": "off"}},
            ],
        },
    )
    await captured[0][1]()
    await captured[1][1]()
    assert hass.services.calls == [
        ("homeassistant", "turn_on", {"area_id": ["kitchen"]}),
        ("homeassistant", "turn_off", {"area_id": ["kitchen"]}),
    ]


async def test_press_scene_activates_scene(monkeypatch):
    hass = _hass()
    _, captured = await _load_one(
        monkeypatch,
        hass,
        {
            "id": "b1",
            "targets": {},
            "presses": [
                {"trigger": {"platform": "device", "x": 1}, "action": {"type": "scene", "entity_id": "scene.kvall"}}
            ],
        },
    )
    await captured[0][1]()
    assert hass.services.calls == [("scene", "turn_on", {"entity_id": ["scene.kvall"]})]


async def test_press_service_merges_target_and_data(monkeypatch):
    hass = _hass()
    _, captured = await _load_one(
        monkeypatch,
        hass,
        {
            "id": "b1",
            "targets": {"entity_id": ["light.a"]},
            "presses": [
                {
                    "trigger": {"platform": "device", "x": 1},
                    "action": {
                        "type": "service",
                        "domain": "light",
                        "service": "turn_on",
                        "data": {"brightness_pct": 40},
                    },
                }
            ],
        },
    )
    await captured[0][1]()
    assert hass.services.calls == [("light", "turn_on", {"entity_id": ["light.a"], "brightness_pct": 40})]


async def test_press_toggle_on_empty_target_is_noop(monkeypatch):
    hass = _hass()
    _, captured = await _load_one(
        monkeypatch,
        hass,
        {
            "id": "b1",
            "targets": {},
            "presses": [{"trigger": {"platform": "device", "x": 1}, "action": {"type": "toggle"}}],
        },
    )
    await captured[0][1]()
    assert hass.services.calls == []  # nothing to toggle


async def test_press_action_noops_after_close(monkeypatch):
    hass = _hass()
    pb, captured = await _load_one(
        monkeypatch,
        hass,
        {
            "id": "b1",
            "targets": {"entity_id": ["light.a"]},
            "presses": [{"trigger": {"platform": "device", "x": 1}, "action": {"type": "toggle"}}],
        },
    )
    pb.shutdown()
    await captured[0][1]()
    assert hass.services.calls == []  # a trigger firing after teardown runs nothing


async def test_replace_rejects_malformed_presses(monkeypatch):
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers([]))
    pb = PlejdDimBindings(_hass())
    await pb.async_load()
    bad = [
        {"presses": [{"action": {"type": "toggle"}}]},  # no trigger
        {"presses": [{"trigger": {"platform": "device", "x": 1}, "action": {"type": "bogus"}}]},  # unknown type
        {"presses": [{"trigger": {"platform": "device", "x": 1}, "action": {"type": "scene"}}]},  # scene w/o entity_id
        {
            "presses": [{"trigger": {"platform": "device", "x": 1}, "action": {"type": "service", "domain": "light"}}]
        },  # no service
        {"presses": "not-a-list"},  # wrong type for presses
        {"presses": [{"trigger": {"platform": "device", "x": 1}, "action": "not-a-dict"}]},  # wrong type for action
        {"presses": ["not-a-dict"]},  # wrong type for a press entry
        {
            "presses": [
                {"trigger": {"platform": "device", "x": 1}, "action": {"type": "scene", "entity_id": ["scene.kvall"]}}
            ]
        },  # entity_id must be a single string, not a list
        {
            "presses": [
                {
                    "trigger": {"platform": "device", "x": 1},
                    "action": {"type": "service", "domain": "light", "service": "turn_on", "data": ["bad"]},
                }
            ]
        },  # data must be a mapping
        {"presses": [{"trigger": "bad", "action": {"type": "toggle"}}]},  # trigger must be a mapping
        {"presses": [{"trigger": ["bad"], "action": {"type": "toggle"}}]},  # or a list of mappings
        {
            "presses": [
                {"trigger": {"platform": "device", "x": 1}, "action": {"type": "scene", "entity_id": "light.kok"}}
            ]
        },  # scene entity_id must target a scene.* entity
        {
            "presses": [
                {
                    "trigger": {"platform": "device", "x": 1},
                    "action": {"type": "service", "domain": 1, "service": "turn_on"},
                }
            ]
        },  # service domain must be a string
        {
            "presses": [
                {
                    "trigger": {"platform": "device", "x": 1},
                    "action": {"type": "service", "domain": "light", "service": 1},
                }
            ]
        },  # service name must be a string
        {"presses": [{"trigger": [{}], "action": {"type": "toggle"}}]},  # empty trigger inside a list
        {
            "presses": [{"trigger": {"platform": "device", "x": 1}, "action": {"type": "scene", "entity_id": "scene."}}]
        },  # scene entity_id needs a non-empty object_id
        {
            "presses": [
                {"trigger": {"platform": "device", "x": 1}, "action": {"type": "scene", "entity_id": "scene.kvall bad"}}
            ]
        },  # scene entity_id must be a valid slug
        {
            "presses": [
                {
                    "trigger": {"platform": "device", "x": 1},
                    "action": {"type": "service", "domain": "light.turn_on", "service": "turn_on"},
                }
            ]
        },  # service domain must be a valid slug, not a dotted entity id
        {
            "presses": [
                {
                    "trigger": {"platform": "device", "x": 1},
                    "action": {"type": "service", "domain": "light", "service": "turn on"},
                }
            ]
        },  # service name must be a valid slug, no spaces
        {"presses": [{"trigger": {"x": 1}, "action": {"type": "toggle"}}]},  # trigger needs a platform key
        {"presses": [{"trigger": {"platform": "", "x": 1}, "action": {"type": "toggle"}}]},  # platform can't be empty
        {"presses": [{"trigger": {"platform": 1}, "action": {"type": "toggle"}}]},  # platform must be a string
        {
            "presses": [
                {"trigger": {"platform": "device", "x": 1}, "action": {"type": "scene", "entity_id": "scene._bad"}}
            ]
        },  # scene entity_id can't start with an underscore
        {
            "presses": [
                {"trigger": {"platform": "device", "x": 1}, "action": {"type": "scene", "entity_id": "scene.bad_"}}
            ]
        },  # scene entity_id can't end with an underscore
        {
            "presses": [
                {
                    "trigger": {"platform": "device", "x": 1},
                    "action": {"type": "service", "domain": "bad__domain", "service": "turn_on"},
                }
            ]
        },  # service domain can't have a double underscore
    ]
    for binding in bad:
        with pytest.raises(ValueError):
            await pb.async_replace([{"id": "b1", "targets": {"entity_id": ["light.a"]}, **binding}])


async def test_load_skips_binding_with_malformed_presses(monkeypatch):
    captured = []
    monkeypatch.setattr(bindings_mod, "async_initialize_triggers", _spy_triggers(captured))
    logged = []
    monkeypatch.setattr(
        bindings_mod._LOGGER, "warning", lambda *a, **k: logged.append(a[0] % a[1:] if len(a) > 1 else a[0])
    )
    hass = _hass()
    hass.data[("store", bindings_mod.STORE_KEY)] = [
        {"id": "b1", "targets": {"entity_id": ["light.a"]}, "presses": "not-a-list"},
        {"id": "b2", "targets": {"entity_id": ["light.b"]}, "up": {"x": 1}, "stop": {"s": 1}},
    ]
    pb = PlejdDimBindings(hass)
    await pb.async_load()
    # b1 has malformed presses — logged as a warning, no triggers attached for it
    assert any("b1" in w for w in logged)
    # b2 is well-formed and attached normally (up + stop = 2 triggers)
    assert len(captured) == 2
