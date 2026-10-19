"""Holiday mode: presence simulation, the HA equivalent of the Plejd app's "Semesterläge".

While enabled and within a configured time-of-day window, periodically turns a
random subset of the target lights on for a randomized duration, so an empty home
looks lived-in while away. Drives plain `light.turn_on`/`light.turn_off` (not
Plejd-specific mesh commands), matching this integration's existing "generic ramp"
approach (bindings.py) so it composes with any light in the user's HA setup, not
only Plejd ones.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_HOLIDAY_LIGHTS,
    CONF_HOLIDAY_WINDOW_END,
    CONF_HOLIDAY_WINDOW_START,
    DOMAIN,
    HOLIDAY_WINDOW_END_DEFAULT,
    HOLIDAY_WINDOW_START_DEFAULT,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DATA_HOLIDAY_MODE = f"{DOMAIN}_holiday_mode"

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.holiday_mode"

CHECK_INTERVAL = timedelta(minutes=5)
TOGGLE_FRACTION = 0.4  # fraction of currently-off target lights turned on per tick
MIN_ON_MINUTES = 10
MAX_ON_MINUTES = 45


def _parse_hhmm(value: str) -> time:
    hour, minute = (int(p) for p in value.split(":")[:2])
    return time(hour, minute)


def _in_window(now: time, start: time, end: time) -> bool:
    """Whether `now` falls in [start, end); handles a window that crosses midnight."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


class PlejdHolidayMode:
    """Randomly varies target lights on a recurring schedule, only within an active window.

    async_start()/async_stop()/each tick's apply all run under a single lock, so they
    can never interleave — a start fully completes (or a stop fully completes) before
    the other begins. That removes an entire class of races (a tick persisting after
    stop already cleared the store, a light picked up right as stop cancels the timer,
    ...) rather than requiring a bespoke check for each one.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._rng = rng or random.Random()
        self._unsub: Callable[[], None] | None = None
        self._on_until: dict[str, datetime] = {}
        self._store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._unsub is not None

    async def async_start(self) -> None:
        """Begin the recurring schedule, restoring deadlines persisted before a restart (idempotent)."""
        async with self._lock:
            if self._unsub is not None:
                return
            self._on_until = await self._async_load_deadlines()
            self._unsub = async_track_time_interval(self._hass, self._async_tick, CHECK_INTERVAL)

    async def async_stop(self) -> bool:
        """Stop the recurring schedule and turn off any lights it turned on (idempotent).

        Awaited directly by callers (the switch entity, and __init__.py's unload, before
        the light platform is torn down) rather than fired-and-forgotten, so the cleanup
        reliably completes while the target entities/connection still exist. Always
        persists afterward (even with nothing to turn off), so it correctly clears the
        store when nothing was ever restored/tracked this session.

        Returns False if a light could not be turned off despite the retry, so a caller
        acting on an explicit user request (the switch) can surface that instead of
        reporting success while a holiday-owned light is still on.
        """
        async with self._lock:
            if self._unsub is not None:
                self._unsub()
                self._unsub = None
            all_off = True
            for entity_id in list(self._on_until):
                if await self._async_turn_off_with_retry(entity_id):
                    del self._on_until[entity_id]
                else:
                    all_off = False
            await self._async_persist()
            return all_off

    async def _async_load_deadlines(self) -> dict[str, datetime]:
        """Best-effort load of persisted deadlines.

        Must never raise: this runs before the timer is registered, so a failure here
        would otherwise abort async_start() and leave the caller unable to tell whether
        it's actually running. A corrupt/unreadable store, or one holding a payload of
        the wrong shape, means "start with none"; a single malformed deadline just drops
        that one entry rather than discarding everything else that parsed fine.
        """
        try:
            stored = await self._store.async_load() or {}
        except Exception:  # noqa: BLE001 - a corrupt/unreadable store must never block starting
            _LOGGER.warning("Plejd holiday mode: could not load persisted deadlines; starting with none", exc_info=True)
            return {}
        if not isinstance(stored, dict):
            _LOGGER.warning("Plejd holiday mode: persisted deadlines had an unexpected shape; starting with none")
            return {}
        parsed: dict[str, datetime] = {}
        for entity_id, deadline in stored.items():
            try:
                parsed_deadline = datetime.fromisoformat(deadline)
            except (TypeError, ValueError):
                _LOGGER.warning("Plejd holiday mode: dropping a malformed persisted deadline for %s", entity_id)
                continue
            if parsed_deadline.tzinfo is None:
                # _async_turn_off_expired() compares against dt_util.now() (aware); a naive
                # deadline would TypeError on every tick, silently breaking that light's
                # expiry forever (#89 review).
                _LOGGER.warning("Plejd holiday mode: dropping a timezone-naive persisted deadline for %s", entity_id)
                continue
            parsed[entity_id] = parsed_deadline
        return parsed

    async def _async_turn_off_with_retry(self, entity_id: str, attempts: int = 2) -> bool:
        """Turn off `entity_id`, retrying once — stopping cancels the timer, so a single
        transient failure here would otherwise leave the light with no later retry."""
        for _ in range(attempts):
            if await self._async_turn_off(entity_id):
                return True
        return False

    def _window(self) -> tuple[time, time]:
        options = self._entry.options
        start = _parse_hhmm(options.get(CONF_HOLIDAY_WINDOW_START, HOLIDAY_WINDOW_START_DEFAULT))
        end = _parse_hhmm(options.get(CONF_HOLIDAY_WINDOW_END, HOLIDAY_WINDOW_END_DEFAULT))
        return start, end

    def _target_lights(self) -> list[str]:
        """Configured target lights, or every Plejd light entity if none are configured."""
        configured = self._entry.options.get(CONF_HOLIDAY_LIGHTS)
        if configured:
            return list(configured)
        registry = er.async_get(self._hass)
        reg_entries = er.async_entries_for_config_entry(registry, self._entry.entry_id)
        return [
            reg_entry.entity_id
            for reg_entry in reg_entries
            if reg_entry.entity_id.startswith("light.") and reg_entry.disabled_by is None
        ]

    def _is_currently_on(self, entity_id: str) -> bool:
        """True if HA reports this light already on for a reason holiday mode didn't track."""
        state = self._hass.states.get(entity_id)
        return state is not None and state.state == "on"

    async def _async_persist(self) -> None:
        """Best-effort save: a full disk must not abort the stop/unload cleanup that called this."""
        try:
            await self._store.async_save({eid: deadline.isoformat() for eid, deadline in self._on_until.items()})
        except Exception:  # noqa: BLE001 - persistence is best-effort, never fatal to the caller
            _LOGGER.warning("Plejd holiday mode: could not persist deadlines", exc_info=True)

    async def _async_turn_on(self, entity_id: str) -> bool:
        try:
            await self._hass.services.async_call("light", "turn_on", {"entity_id": entity_id}, blocking=True)
        except Exception:  # noqa: BLE001 - one failed light must not block the rest, nor mark it as ours
            _LOGGER.warning("Plejd holiday mode: could not turn on %s", entity_id, exc_info=True)
            return False
        return True

    async def _async_turn_off(self, entity_id: str) -> bool:
        try:
            await self._hass.services.async_call("light", "turn_off", {"entity_id": entity_id}, blocking=True)
        except Exception:  # noqa: BLE001 - one failed light must not block the rest
            _LOGGER.warning("Plejd holiday mode: could not turn off %s", entity_id, exc_info=True)
            return False
        return True

    async def _async_tick(self, _now: object) -> None:
        async with self._lock:
            if self._unsub is None:
                return  # queued behind an async_stop() that already ran; nothing to do
            await self._async_apply(dt_util.now())

    async def _async_apply(self, now_local: datetime) -> None:
        # Expire our own on-lights on every tick, even outside the active window — a light
        # turned on near the window's end can have a deadline past it (#89 review).
        await self._async_turn_off_expired(now_local)
        if _in_window(now_local.time(), *self._window()):
            await self._async_turn_on_new(now_local)
        await self._async_persist()

    async def _async_turn_on_new(self, now_local: datetime) -> None:
        off_lights = [
            entity_id
            for entity_id in self._target_lights()
            if entity_id not in self._on_until and not self._is_currently_on(entity_id)
        ]
        if not off_lights:
            return
        count = min(len(off_lights), max(1, round(len(off_lights) * TOGGLE_FRACTION)))
        for entity_id in self._rng.sample(off_lights, count):
            if not await self._async_turn_on(entity_id):
                continue  # not adopted as ours: safe to retry on the next tick
            minutes = self._rng.uniform(MIN_ON_MINUTES, MAX_ON_MINUTES)
            self._on_until[entity_id] = now_local + timedelta(minutes=minutes)

    async def _async_turn_off_expired(self, now_local: datetime) -> None:
        expired = [entity_id for entity_id, deadline in self._on_until.items() if deadline <= now_local]
        for entity_id in expired:
            if await self._async_turn_off(entity_id):
                del self._on_until[entity_id]
