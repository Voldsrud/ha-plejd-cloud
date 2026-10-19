"""Smooth remote-driven dimming for Plejd lights over the fast gateway path.

A dimmer remote's "move while held, stop on release" actions (IKEA/Hue style) map to
:meth:`PlejdDimRamp.start` / :meth:`PlejdDimRamp.stop`: start walks the target output's
brightness one step per tick in the given direction until released or a bound is hit.
It rides the coordinator's gateway command path, which delivers each step reliably since
the ack fix (#77) — so the ramp is smooth instead of the chunky input_number workaround.
"""

from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant

# Per-tick brightness delta (0-255) and the pause between ticks. ~13/255 gives ~20
# steps across the full range; each tick also awaits the command's gateway round-trip,
# so a full sweep is roughly a couple of seconds of wall-clock — a comfortable
# hold-to-dim feel.
DIM_STEP = 13
DIM_INTERVAL = 0.1
# Holding "down" dims to a low floor, not fully off — releasing leaves the light on,
# like a physical dimmer.
DIM_MIN = 1
DIM_MAX = 255


class PlejdDimRamp:
    """Runs and cancels per-output brightness ramps, one background task per address."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator,  # PlejdCoordinator; duck-typed (state_for + async_set_output) to avoid an import cycle
        *,
        step: int = DIM_STEP,
        interval: float = DIM_INTERVAL,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._step = step
        self._interval = interval
        self._tasks: dict[int, asyncio.Task] = {}

    def start(
        self,
        address: int,
        direction: int,
        *,
        current: tuple[bool, int] | None = None,
        member_addresses: list[int] | None = None,
    ) -> None:
        """Begin ramping `address` up/down while held.

        `current` seeds (is_on, level) for a target with no state read-back of its own
        (a room's group address). `member_addresses`, for the same case, routes each
        tick through the coordinator's member-aware group write instead of a plain one.
        """
        self.stop(address)
        task = self._spawn(self._run(address, direction, current, member_addresses))
        self._tasks[address] = task
        # Drop the entry when the ramp ends on its own (bound reached), but only if it's
        # still the current task — a start() that replaced it must not be evicted.
        task.add_done_callback(lambda finished, addr=address: self._forget(addr, finished))

    def stop(self, address: int) -> None:
        """Stop ramping `address` (no-op if it isn't ramping)."""
        task = self._tasks.get(address)
        if task is not None:
            task.cancel()

    def shutdown(self) -> None:
        """Cancel every in-flight ramp (entry unload/reload)."""
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()

    def _forget(self, address: int, task: asyncio.Task) -> None:
        if self._tasks.get(address) is task:
            del self._tasks[address]

    def _spawn(self, coro) -> asyncio.Task:
        create = getattr(self._hass, "async_create_background_task", None)
        if create is not None:
            return create(coro, name="plejd-dim-ramp")
        return asyncio.ensure_future(coro)

    async def _run(
        self,
        address: int,
        direction: int,
        current: tuple[bool, int] | None = None,
        member_addresses: list[int] | None = None,
    ) -> None:
        if current is not None:
            is_on, level = current
        else:
            state = self._coordinator.state_for(address)
            is_on = state is not None and state.on
            level = state.level if state is not None else 0
        if direction < 0 and (not is_on or level <= DIM_MIN):
            return  # already off or at the floor — nothing to dim down
        if not is_on:
            level = 0  # ramping up from off starts at the bottom
        step = self._step if direction >= 0 else -self._step
        while True:
            level = max(DIM_MIN, min(DIM_MAX, level + step))
            if member_addresses is not None:
                await self._coordinator.async_set_group_output(address, True, level, member_addresses)
            else:
                await self._coordinator.async_set_output(address, True, level)
            if level <= DIM_MIN or level >= DIM_MAX:
                return  # reached a bound; hold there until released
            await asyncio.sleep(self._interval)
