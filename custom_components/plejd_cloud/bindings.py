"""Remote → light dim bindings, managed by the integration.

A binding maps a remote's hold/release actions (any HA device trigger — IKEA, Hue, ZHA,
Zigbee2MQTT, …) to smooth hold-to-dim of a target: any Home Assistant light or a whole
area, Plejd or not. Bindings are stored here and their triggers attached via HA's generic
trigger machinery. When a trigger fires, a target that is a single Plejd light rides
Plejd's native ramp (`start_dim`/`stop_dim` over the site's chosen transport, with local
level tracking); every other target — an area, a device, multiple or non-Plejd entities —
uses a generic `light.turn_on` brightness-step ramp so it works for every light.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.trigger import async_initialize_triggers

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.dim_bindings"

# Plejd's own hold-to-dim entity services (light.py). A binding that targets a single
# Plejd light is driven through these so it rides the site's chosen transport
# (gateway/BLE, per CONF_TRANSPORT) with local level tracking, instead of the generic
# state-read-back ramp — which can plateau on stale BLE state for gateway-less installs.
SERVICE_START_DIM = "start_dim"
SERVICE_STOP_DIM = "stop_dim"

# Brightness delta per tick (percent), tick spacing, and a safety cap so a missed
# release event can't ramp forever.
DIM_STEP_PCT = 5
DIM_INTERVAL = 0.12
DIM_MAX_DURATION = 8.0

_TARGET_KEYS = ("entity_id", "device_id", "area_id")

_ERR_STOPLESS_BINDING = "dim binding has a start trigger but no stop trigger"


class InvalidDimBinding(ValueError):
    """A dim binding is structurally invalid (e.g. a start trigger with no stop trigger).

    A distinct type so the WebSocket layer can surface these as client-input errors
    without also catching unrelated ValueErrors from deeper in the save path.
    """


def _target(binding: dict) -> dict[str, Any]:
    """The HA service target from a binding (entity/device/area), non-empty keys only."""
    targets = binding.get("targets") or {}
    return {key: targets[key] for key in _TARGET_KEYS if targets.get(key)}


def _is_stopless(binding: dict) -> bool:
    """True if the binding has a start trigger (up/down) but no stop trigger to release it."""
    return bool((binding.get("up") or binding.get("down")) and not binding.get("stop"))


# A "press" maps one remote trigger (any button, any press type) to an instantaneous action
# on the binding's target — the general counterpart to the hold-to-dim up/down/stop triggers.
PRESS_ACTIONS = ("toggle", "on", "off", "scene", "service")

# HA's actual slug convention: lowercase alphanumeric segments joined by single
# underscores — no leading/trailing/double underscore (matches HA's own entity-id and
# service-name validators). A stricter check here catches typos that would otherwise
# only surface as a failed hass.services.async_call when the remote fires.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_SCENE_ENTITY_RE = re.compile(r"^scene\.[a-z0-9]+(?:_[a-z0-9]+)*$")


def _validate_presses(binding: dict) -> None:
    """Reject a binding whose press mappings are malformed.

    Raised from both the save path (async_replace, before persisting) and the load path
    (_attach, as a safety net for legacy/hand-edited storage).
    """
    presses = binding.get("presses")
    if presses is None:
        return
    if not isinstance(presses, list):
        raise InvalidDimBinding("binding presses must be a list")
    for press in presses:
        if not isinstance(press, dict):
            raise InvalidDimBinding("each press entry must be a mapping")
        trigger = press.get("trigger")
        if not trigger:
            raise InvalidDimBinding("binding press has no trigger")
        trigger_configs = trigger if isinstance(trigger, list) else [trigger]
        if not all(isinstance(t, dict) and t for t in trigger_configs):
            raise InvalidDimBinding("binding press trigger must be a non-empty mapping (or a list of them)")
        if not all(isinstance(t.get("platform"), str) and t.get("platform") for t in trigger_configs):
            # HA's trigger helper reads CONF_PLATFORM to dispatch to the right trigger
            # integration; without it, async_initialize_triggers fails and _async_attach
            # only logs and skips the binding — save-time is where this should be caught.
            raise InvalidDimBinding("binding press trigger must include a platform")
        action = press.get("action")
        if action is not None and not isinstance(action, dict):
            raise InvalidDimBinding("press action must be a mapping")
        action = action or {}
        atype = action.get("type")
        if atype not in PRESS_ACTIONS:
            raise InvalidDimBinding(f"unknown press action type: {atype!r}")
        if atype == "scene":
            entity_id = action.get("entity_id")
            if not isinstance(entity_id, str) or not _SCENE_ENTITY_RE.match(entity_id):
                raise InvalidDimBinding("scene press action needs a valid scene.* entity_id")
        if atype == "service":
            domain, service = action.get("domain"), action.get("service")
            if not (isinstance(domain, str) and _SLUG_RE.match(domain)):
                raise InvalidDimBinding("service press action needs a valid domain")
            if not (isinstance(service, str) and _SLUG_RE.match(service)):
                raise InvalidDimBinding("service press action needs a valid service name")
            data = action.get("data")
            if data is not None and not isinstance(data, dict):
                raise InvalidDimBinding("service press action data must be a mapping")


def _ensure_ids(bindings: list[dict]) -> bool:
    """Give every binding a stable unique id (so ramps never collide under a shared key).

    Mutates in place; returns True if any id was newly assigned or de-duplicated.
    """
    assigned = False
    seen: set[str] = set()
    for binding in bindings:
        if (bid := binding.get("id")) and bid not in seen:
            seen.add(bid)
            continue
        bid = uuid4().hex  # unique by construction, so it also breaks any duplicate
        binding["id"] = bid
        seen.add(bid)
        assigned = True
    return assigned


class DimRamp:
    """Generic hold-to-dim: steps a target's brightness via `light.turn_on` while held.

    Keyed so each binding gets its own cancellable ramp. Works on any light because it
    hands the whole target (entity/device/area) to `light.turn_on`, which expands an
    area to its lights. Runs as a system action (a remote trigger fired it), so no user
    context or per-user permission check applies — like any automation-driven call.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        step_pct: int = DIM_STEP_PCT,
        interval: float = DIM_INTERVAL,
        max_duration: float = DIM_MAX_DURATION,
    ) -> None:
        self._hass = hass
        self._step_pct = step_pct
        self._interval = interval
        self._max_duration = max_duration
        self._tasks: dict[str, asyncio.Task] = {}

    def start(self, key: str, target: dict[str, Any], direction: str) -> None:
        """Begin ramping `target` up/down while held (restarts if already running)."""
        self.stop(key)
        if not target:
            return  # nothing to dim
        task = self._spawn(self._run(target, direction))
        self._tasks[key] = task
        task.add_done_callback(lambda finished, k=key: self._forget(k, finished))

    def stop(self, key: str) -> None:
        """Stop ramping for this binding (no-op if not running)."""
        task = self._tasks.get(key)
        if task is not None:
            task.cancel()

    def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()

    def _forget(self, key: str, task: asyncio.Task) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
        # Retrieve the result so a failing ramp doesn't surface as "Task exception was
        # never retrieved"; a cancel (the normal stop path) isn't an error.
        if not task.cancelled() and (exc := task.exception()) is not None:
            _LOGGER.warning(
                "Plejd: dim ramp failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _spawn(self, coro) -> asyncio.Task:
        create = getattr(self._hass, "async_create_background_task", None)
        if create is not None:
            return create(coro, name="plejd-dim-binding")
        return asyncio.ensure_future(coro)

    async def _run(self, target: dict[str, Any], direction: str) -> None:
        step = self._step_pct if direction == "up" else -self._step_pct
        # Wall-clock deadline: each tick also awaits a (possibly slow) service call, so
        # counting intervals would under-measure and let the cap drift under load.
        deadline = time.monotonic() + self._max_duration
        while time.monotonic() < deadline:
            await self._hass.services.async_call(
                "light", "turn_on", {**target, "brightness_step_pct": step}, blocking=True
            )
            await asyncio.sleep(self._interval)


class PlejdDimBindings:
    """Loads/persists dim bindings and attaches their triggers to the ramp."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self._bindings: list[dict] = []
        self._unsubs: list = []
        self._ramp = DimRamp(hass)
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def bindings(self) -> list[dict]:
        return self._bindings

    async def async_load(self) -> None:
        # Runs once at setup, before shutdown is wired up, so no teardown race to guard here.
        # Serialize with replace (shared lock) so triggers are never attached twice; commit
        # in-memory state only after the read/save, so a failed load leaves an empty,
        # consistent manager (no triggers attached, nothing listed).
        async with self._lock:
            bindings = await self._store.async_load() or []
            if _ensure_ids(bindings):
                await self._store.async_save(bindings)  # persist ids assigned to legacy data
            self._bindings = bindings
            await self._async_attach()

    async def async_replace(self, bindings: list[dict]) -> None:
        """Persist the full set of bindings and re-attach their triggers."""
        for binding in bindings:
            if _is_stopless(binding):
                # Reject before persisting: a hold with no release is invalid, and
                # _async_attach would otherwise swallow the per-binding error and still
                # report a saved-but-non-functional binding to the client.
                raise InvalidDimBinding(_ERR_STOPLESS_BINDING)
            _validate_presses(binding)
        _ensure_ids(bindings)
        # Serialize the whole save→swap→re-attach so two overlapping saves can't interleave
        # and leave both trigger sets live. Save first so the in-memory list and the live
        # triggers always agree; durability is best-effort — HA's Store logs write failures
        # rather than raising, so we can't promise the on-disk copy beyond that.
        async with self._lock:
            await self._store.async_save(bindings)
            previous = self._bindings
            self._bindings = bindings
            self._detach()
            self._ramp.shutdown()  # cancel generic live ramps whose stop trigger may be gone
            await self._stop_native_ramps(previous)  # and the coordinator-owned Plejd ramps
            if self._closed:
                return
            await self._async_attach()
            if self._closed:  # unload raced this save → don't leave triggers live past teardown
                self._detach()

    async def _stop_native_ramps(self, bindings: list[dict]) -> None:
        """Stop any in-flight Plejd-native ramp for these bindings' single-light targets.

        Those ramps are owned by the coordinator (via plejd.start_dim), not the generic
        DimRamp, so a replace must stop them explicitly or a held light rides to its bound.
        """
        for binding in bindings:
            try:
                light = self._plejd_light(binding)
                if light is not None:
                    await self._hass.services.async_call(
                        DOMAIN, SERVICE_STOP_DIM, {"entity_id": [light]}, blocking=True
                    )
            except Exception:  # noqa: BLE001 - best-effort cleanup; a bad/old record mustn't abort the replace
                _LOGGER.warning("Plejd: could not stop native ramp for a replaced binding", exc_info=True)

    async def _async_attach(self) -> None:
        for binding in self._bindings:
            try:
                await self._attach(binding)
            except Exception:  # noqa: BLE001 - a stale binding (e.g. removed remote) mustn't break the rest
                _LOGGER.warning("Plejd: could not attach dim binding %s", binding.get("id"), exc_info=True)

    async def _attach(self, binding: dict) -> None:
        bid = binding["id"]  # guaranteed by _ensure_ids
        target = _target(binding)
        if _is_stopless(binding):
            # Load-path safety net for legacy/hand-edited storage (async_replace rejects
            # these before saving): a hold with no release would ramp to DIM_MAX_DURATION.
            raise InvalidDimBinding(_ERR_STOPLESS_BINDING)
        _validate_presses(binding)  # load-path guard: malformed presses log + skip this binding
        plejd_light = self._plejd_light(binding)
        # Attach atomically: if any trigger fails, roll back the ones already attached for
        # this binding, so we never leave a ramp that can start but not stop.
        attached: list = []
        try:
            for key, direction in (("up", "up"), ("down", "down")):
                trigger = binding.get(key)
                if trigger:
                    action = self._start_action(bid, target, direction, plejd_light)
                    await self._attach_trigger(attached, trigger, action)
            stop = binding.get("stop")
            if stop:
                await self._attach_trigger(attached, stop, self._stop_action(bid, plejd_light))
            for press in binding.get("presses") or []:
                trigger = press.get("trigger")
                if trigger:
                    await self._attach_trigger(attached, trigger, self._press_action(target, press.get("action") or {}))
        except Exception:
            for unsub in attached:
                unsub()
            raise
        self._unsubs.extend(attached)

    def _plejd_light(self, binding: dict) -> str | None:
        """The entity_id if the binding targets exactly one Plejd light, else None.

        Those ride Plejd's native ramp (over the chosen transport); everything else —
        areas, devices, multiple or non-Plejd entities — uses the generic ramp.
        """
        target = _target(binding)
        entities = target.get("entity_id")
        if isinstance(entities, str):
            entities = [entities]
        if list(target) != ["entity_id"] or not isinstance(entities, list) or len(entities) != 1:
            return None
        entity_id = entities[0]
        if not entity_id.startswith("light."):
            return None
        entry = er.async_get(self._hass).async_get(entity_id)
        return entity_id if entry is not None and entry.platform == DOMAIN else None

    async def _attach_trigger(self, attached: list, trigger, action) -> None:
        # Attach each config on its own so a partial failure in a multi-trigger slot is
        # visible: async_initialize_triggers returns None when a config sets up nothing
        # (stale/invalid). Treat that as a failure so _attach rolls the binding back —
        # a binding that can start a ramp but never stop it would run to DIM_MAX_DURATION.
        configs = trigger if isinstance(trigger, list) else [trigger]
        for config in configs:
            unsub = await async_initialize_triggers(self._hass, [config], action, DOMAIN, "plejd-dim", _LOGGER.log)
            if unsub is None:
                raise RuntimeError("dim binding trigger failed to initialize")
            attached.append(unsub)

    def _start_action(self, bid, target, direction, plejd_light):
        async def _action(run_variables=None, context=None):
            if self._closed:  # a trigger firing during unload-race cleanup mustn't start a ramp
                return
            if plejd_light is not None:
                await self._hass.services.async_call(
                    DOMAIN,
                    SERVICE_START_DIM,
                    {"entity_id": [plejd_light], "direction": direction},
                    blocking=True,
                )
            else:
                self._ramp.start(bid, target, direction)

        return _action

    def _stop_action(self, bid, plejd_light):
        async def _action(run_variables=None, context=None):
            if plejd_light is not None:
                await self._hass.services.async_call(
                    DOMAIN, SERVICE_STOP_DIM, {"entity_id": [plejd_light]}, blocking=True
                )
            else:
                self._ramp.stop(bid)

        return _action

    def _press_action(self, target, action):
        async def _action(run_variables=None, context=None):
            if self._closed:  # unload raced an attach; don't run after teardown
                return
            await self._run_press(target, action)

        return _action

    async def _run_press(self, target: dict[str, Any], action: dict) -> None:
        """Run one press action on the target (any remote trigger → any HA action)."""
        atype = action.get("type")
        if atype in ("toggle", "on", "off"):
            if not target:
                return  # nothing to act on
            service = {"toggle": "toggle", "on": "turn_on", "off": "turn_off"}[atype]
            await self._hass.services.async_call("homeassistant", service, dict(target), blocking=True)
        elif atype == "scene":
            await self._hass.services.async_call(
                "scene", "turn_on", {"entity_id": [action["entity_id"]]}, blocking=True
            )
        elif atype == "service":
            # Escape hatch: call any service. The binding target is merged in so a service
            # like light.turn_on applies to it; explicit data wins on conflict.
            data = {**target, **(action.get("data") or {})}
            await self._hass.services.async_call(action["domain"], action["service"], data, blocking=True)

    def _detach(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    def shutdown(self) -> None:
        # Sync teardown (runs outside _lock): flag so an in-flight save re-checks and
        # doesn't attach triggers after unload.
        self._closed = True
        self._detach()
        self._ramp.shutdown()
