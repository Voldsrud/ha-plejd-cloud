"""Tests for the remote hold-to-dim ramp engine."""

from __future__ import annotations

import asyncio
import types

from plejd_cloud.dim_ramp import DIM_MAX, DIM_MIN, PlejdDimRamp
from plejd_cloud.protocol import OutputState


class _FakeCoord:
    def __init__(self, states=None, devices=()):
        self._states = states or {}
        self.devices = list(devices)
        self.sets: list[tuple[int, bool, int]] = []
        self.group_sets: list[tuple[int, bool, int, list[int]]] = []

    def state_for(self, address):
        return self._states.get(address)

    async def async_set_output(self, address, on, level):
        self.sets.append((address, on, level))

    async def async_set_group_output(self, address, on, level, member_addresses):
        self.group_sets.append((address, on, level, list(member_addresses)))


def _ramp(coord, **kw):
    # No async_create_background_task on this fake hass → _spawn uses asyncio.ensure_future.
    return PlejdDimRamp(types.SimpleNamespace(), coord, **kw)


async def _drain():
    # let scheduled done-callbacks / cancellations run
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# Completion tests use interval=0 (steps back-to-back, no real delay); cancel/restart
# tests use a long interval so the ramp is parked mid-sleep when we cancel it. No
# monkeypatching of asyncio.sleep — patching it globally would also break the tests'
# own asyncio.sleep(0) scheduling yields.
PARKED = 3600


# ── ramp behaviour ────────────────────────────────────────────────────────────


async def test_ramp_up_from_off_reaches_max():
    coord = _FakeCoord()  # address 11 off/unknown
    ramp = _ramp(coord, step=100, interval=0)
    ramp.start(11, 1)
    await ramp._tasks[11]
    await _drain()
    assert coord.sets == [(11, True, 100), (11, True, 200), (11, True, DIM_MAX)]
    assert 11 not in ramp._tasks  # ended on its own → forgotten


async def test_ramp_down_from_on_reaches_floor():
    coord = _FakeCoord({11: OutputState(output=11, on=True, level=80)})
    ramp = _ramp(coord, step=100, interval=0)
    ramp.start(11, -1)
    await ramp._tasks[11]
    await _drain()
    assert coord.sets == [(11, True, DIM_MIN)]  # 80 - 100 clamped to the floor, still on


async def test_ramp_down_from_off_is_noop():
    coord = _FakeCoord()  # off
    ramp = _ramp(coord, step=100, interval=0)
    ramp.start(11, -1)
    await ramp._tasks[11]
    await _drain()
    assert coord.sets == []  # nothing to dim down on an off light


async def test_ramp_down_at_floor_is_noop():
    coord = _FakeCoord({11: OutputState(output=11, on=True, level=DIM_MIN)})
    ramp = _ramp(coord, step=100, interval=0)
    ramp.start(11, -1)
    await ramp._tasks[11]
    await _drain()
    assert coord.sets == []


async def test_ramp_up_walks_in_steps():
    coord = _FakeCoord({11: OutputState(output=11, on=True, level=200)})
    ramp = _ramp(coord, step=25, interval=0)
    ramp.start(11, 1)
    await ramp._tasks[11]
    await _drain()
    assert coord.sets == [(11, True, 225), (11, True, 250), (11, True, DIM_MAX)]


# ── `current` seed (targets with no state read-back of their own, e.g. a room group) ──


async def test_ramp_up_uses_seeded_current_instead_of_coordinator_state():
    coord = _FakeCoord()  # no per-output state at all for a group address
    ramp = _ramp(coord, step=25, interval=0)
    ramp.start(99, 1, current=(True, 200))
    await ramp._tasks[99]
    await _drain()
    assert coord.sets == [(99, True, 225), (99, True, 250), (99, True, DIM_MAX)]


async def test_ramp_down_seeded_off_is_noop():
    coord = _FakeCoord()
    ramp = _ramp(coord, step=25, interval=0)
    ramp.start(99, -1, current=(False, 0))
    await ramp._tasks[99]
    await _drain()
    assert coord.sets == []  # seeded as off -> nothing to dim down, coordinator state ignored


# ── `member_addresses` (a room's group address, so each tick also updates members) ──


async def test_ramp_with_member_addresses_routes_through_group_output():
    coord = _FakeCoord()
    ramp = _ramp(coord, step=25, interval=0)
    ramp.start(14, 1, current=(True, 200), member_addresses=[5, 6])
    await ramp._tasks[14]
    await _drain()
    assert coord.sets == []  # never the plain per-output write
    assert coord.group_sets == [
        (14, True, 225, [5, 6]),
        (14, True, 250, [5, 6]),
        (14, True, DIM_MAX, [5, 6]),
    ]


async def test_ramp_without_member_addresses_uses_plain_output():
    coord = _FakeCoord({11: OutputState(output=11, on=True, level=200)})
    ramp = _ramp(coord, step=25, interval=0)
    ramp.start(11, 1)  # no member_addresses -> a normal single-output ramp
    await ramp._tasks[11]
    await _drain()
    assert coord.group_sets == []


# ── stop / restart / shutdown ─────────────────────────────────────────────────


async def test_stop_cancels_in_flight_ramp():
    coord = _FakeCoord({11: OutputState(output=11, on=True, level=100)})
    ramp = _ramp(coord, step=10, interval=PARKED)
    ramp.start(11, 1)
    await asyncio.sleep(0)  # run first step, then park at the long sleep
    task = ramp._tasks[11]
    ramp.stop(11)
    await _drain()
    assert task.cancelled()
    assert 11 not in ramp._tasks  # done-callback cleaned it up
    assert coord.sets == [(11, True, 110)]  # exactly one step happened before parking


async def test_stop_unknown_address_is_noop():
    ramp = _ramp(_FakeCoord())
    ramp.stop(999)  # never started — must not raise


async def test_spawn_uses_hass_background_task_when_available():
    created = []

    def _create(coro, name):
        created.append(name)
        return asyncio.ensure_future(coro)

    hass = types.SimpleNamespace(async_create_background_task=_create)
    coord = _FakeCoord({11: OutputState(output=11, on=True, level=100)})
    ramp = PlejdDimRamp(hass, coord, step=100, interval=0)
    ramp.start(11, 1)
    await ramp._tasks[11]
    await _drain()
    assert created == ["plejd-dim-ramp"]  # HA-owned task (surfaces failures), not a bare future


async def test_restart_replaces_task_without_evicting_new_one():
    coord = _FakeCoord({11: OutputState(output=11, on=True, level=100)})
    ramp = _ramp(coord, step=10, interval=PARKED)
    ramp.start(11, 1)
    await asyncio.sleep(0)
    first = ramp._tasks[11]
    ramp.start(11, 1)  # restart: cancels `first`, installs a new task
    await asyncio.sleep(0)
    second = ramp._tasks[11]
    assert first.cancelled() and second is not first
    await _drain()
    # the cancelled first task's done-callback must NOT evict the live second task
    assert ramp._tasks.get(11) is second
    ramp.shutdown()


async def test_shutdown_cancels_all():
    coord = _FakeCoord({11: OutputState(output=11, on=True, level=100), 12: OutputState(output=12, on=True, level=100)})
    ramp = _ramp(coord, step=10, interval=PARKED)
    ramp.start(11, 1)
    ramp.start(12, -1)
    await asyncio.sleep(0)
    tasks = [ramp._tasks[11], ramp._tasks[12]]
    ramp.shutdown()
    await _drain()
    assert all(t.cancelled() for t in tasks)
    assert ramp._tasks == {}
