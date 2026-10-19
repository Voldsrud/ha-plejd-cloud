"""Plejd coordinator — owns the active mesh transport and pushes state to entities.

Control runs over one of two transports, chosen gateway-first: the remote
gateway/cloud WebSocket (gateway_transport.py) when the site has a Plejd Gateway,
otherwise the local BLE mesh (connection.py), falling back to BLE if the gateway is
unreachable. The coordinator connects (with reconnect/backoff), exposes the cloud
device list + live output state to platforms, and routes entity commands — building
plaintext command vectors that the BLE path encrypts and the gateway path relays.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from aiohttp import ClientError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry, issue_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import protocol, schedule_ws
from .cloud import (
    PlejdAuthError,
    PlejdCloudDevice,
    PlejdCloudError,
    PlejdCloudInput,
    PlejdCloudMotion,
    PlejdCloudRoom,
    PlejdCloudScene,
    async_get_available_firmware,
    async_get_site,
    async_login,
    async_set_device_title,
)
from .connection import PlejdConnection
from .const import (
    CATEGORY_CLIMATE,
    CATEGORY_LIGHT,
    CATEGORY_SWITCH,
    CMD_GROUP_STATE_AND_LEVEL,
    CMD_INPUT_BUTTON,
    CMD_NOTIFY_EVENTS,
    CMD_OUTPUT_BOOT_STATE,
    CMD_OUTPUT_CURVE_TYPE,
    CMD_OUTPUT_INRUSH_CURRENT,
    CMD_OUTPUT_MAX_LEVEL,
    CMD_OUTPUT_MIN_LEVEL,
    CMD_OUTPUT_PHASE_DIM_TYPE,
    CMD_OUTPUT_RELAY_CONFIG,
    CMD_OUTPUT_RELAY_OFF_TIME,
    CMD_OUTPUT_SET,
    CMD_OUTPUT_SPEED,
    CMD_OUTPUT_STATE_AND_LEVEL,
    CMD_SCENE,
    CONF_CRYPTO_KEY,
    CONF_DEVICE_ADDRESSES,
    CONF_DEVICES,
    CONF_DISCOVERED_ADDRESS,
    CONF_GATEWAYS,
    CONF_INPUTS,
    CONF_INSTALLATION_ID,
    CONF_MOTION,
    CONF_RESOURCE_SET_ID,
    CONF_ROOM_NAMES,
    CONF_ROOMS,
    CONF_SCENES,
    CONF_SITE_ID,
    CONF_TRANSPORT,
    DOMAIN,
    PHASE_DIM_HARDWARE,
    PLEJD_SERVICE_UUID,
    RELAY_CONFIG_HARDWARE,
    RELAY_HARDWARE,
    ROOM_DEVICE_ID_PREFIX,
    TIME_EVENT_REP_FOREVER,
    TIME_EVENT_RESULT_SCENE,
    TRANSPORT_AUTO,
    TRANSPORT_BLE,
    TRANSPORT_GATEWAY,
)
from .dim_ramp import PlejdDimRamp
from .gateway_transport import PlejdGatewayConnection
from .protocol import (
    Command,
    MotionEvent,
    OutputSettings,
    OutputState,
    ThermostatState,
    decode_motion,
    decode_notify_events,
    decode_thermostat_state,
)

_LOGGER = logging.getLogger(__name__)

RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0
NOTIFY_POLL_INTERVAL = timedelta(minutes=10)  # device-health (NotifyEvents) poll cadence
CLOUD_POLL_INTERVAL = timedelta(hours=24)  # how often to check for site changes (added/renamed devices)
# Minimum gap between setup-time self-heal attempts (see async_start). Much shorter than
# CLOUD_POLL_INTERVAL because a real repair should take effect on the next setup retry rather
# than a day later, but long enough that HA's rapid setup-retry backoff cannot turn a
# persistently out-of-range BLE mesh into a stream of cloud logins.
SELF_HEAL_COOLDOWN_SECONDS = timedelta(minutes=15).total_seconds()
# Last self-heal attempt as a MONOTONIC timestamp, keyed by entry_id. Lives in hass.data,
# not on the coordinator: every setup retry constructs a brand new coordinator, so instance
# state would never survive to throttle anything. Monotonic rather than wall clock because
# this measures elapsed time within one process - an NTP correction stepping the wall clock
# backwards would otherwise make the gap negative and suppress self-healing until real time
# caught back up, far longer than the cooldown intends.
DATA_LAST_SELF_HEAL = f"{DOMAIN}_last_self_heal"
# Deliberately no streak counter. Counting consecutive skips before warning needed state
# that survives restarts (hass.data does not), and a system restarted between daily polls
# could then skip malformed snapshots forever without ever warning. With a 24h interval a
# single skipped poll already means a full day without syncing, which is worth saying out
# loud - and the issue clears itself on the next usable response, so it cannot nag.


def async_reset_self_heal_cooldown(hass: HomeAssistant, entry_id: str) -> None:
    """Allow an immediate setup self-heal again for `entry_id`.

    Called when reauth succeeds: the cooldown is held through an auth failure so repeated
    setup retries cannot hammer the cloud with logins that cannot work, but reauth only
    updates the password - the retry right afterwards still needs a self-heal to fetch the
    crypto key/gateway data, and making it wait out the cooldown would strand setup for up
    to 15 more minutes at the exact moment it could finally succeed.
    """
    hass.data.get(DATA_LAST_SELF_HEAL, {}).pop(entry_id, None)


def async_clear_malformed_site_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the malformed-cloud repair issue (and its streak) for `entry_id`.

    Exposed because the repair tells the user to try Reconfigure, and that path proves the
    cloud is healthy again without going through the poll - the replacement coordinator
    would otherwise leave the warning up until its first scheduled poll, a day later.
    """
    issue_registry.async_delete_issue(hass, DOMAIN, f"malformed_cloud_site_{entry_id}")


FIRMWARE_REFRESH_INTERVAL = timedelta(days=1)


@dataclass
class PlejdFirmwareStatus:
    """A device's installed firmware vs. the latest the Plejd cloud offers for it."""

    installed_version: str | None
    installed_build_time: int | None
    latest_version: str | None
    latest_build_time: int | None

    @property
    def update_available(self) -> bool:
        return (
            self.latest_build_time is not None
            and self.installed_build_time is not None
            and self.latest_build_time > self.installed_build_time
        )


_SETTINGS_CMDS = frozenset(
    {
        CMD_OUTPUT_MIN_LEVEL,
        CMD_OUTPUT_MAX_LEVEL,
        CMD_OUTPUT_SPEED,
        CMD_OUTPUT_CURVE_TYPE,
        CMD_OUTPUT_PHASE_DIM_TYPE,
        CMD_OUTPUT_BOOT_STATE,
        CMD_OUTPUT_RELAY_OFF_TIME,
        CMD_OUTPUT_RELAY_CONFIG,
        CMD_OUTPUT_INRUSH_CURRENT,
    }
)


def _settings_from_cloud(raw: dict) -> OutputSettings | None:
    """Best-effort decode of a cloud outputSettings dict.

    NOTE(8408323): field names are speculative pending a capture that includes outputSettings (#73).
    """
    min_dim = raw.get("minDim")
    max_dim = raw.get("maxDim")
    curve = raw.get("dimCurve")
    s = OutputSettings(
        min_level=round(int(min_dim) / 0xFFFF * 100, 1) if isinstance(min_dim, (int, float)) else None,
        max_level=round(int(max_dim) / 0xFFFF * 100, 1) if isinstance(max_dim, (int, float)) else None,
        curve=int(curve) if isinstance(curve, (int, float)) else None,
    )
    if s.min_level is None and s.max_level is None and s.curve is None:
        return None
    return s


class PlejdCoordinator:
    """Holds the BLE connection and the site's devices; notifies HA entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry  # for runtime reauth (e.g. a rename hitting expired credentials)
        # Remote hold-to-dim ramps (light.start_dim/stop_dim entity services drive this).
        self.dim_ramp = PlejdDimRamp(hass, self)
        # Tolerate entries stored before a field existed (e.g. output_index).
        self.devices = [PlejdCloudDevice(**{"output_index": 0, **device}) for device in entry.data[CONF_DEVICES]]
        self._climate_addresses = {d.address for d in self.devices if d.category == CATEGORY_CLIMATE}
        self.scenes = [PlejdCloudScene(**scene) for scene in entry.data.get(CONF_SCENES, [])]
        self.rooms = [PlejdCloudRoom(**room) for room in entry.data.get(CONF_ROOMS, [])]
        # CONF_ROOMS is absent (not just empty) on entries added before room-groups existed;
        # only those need a backfill fetch (see _async_poll_faults) - a site with genuinely
        # no rooms stores an explicit [] and must not be re-fetched every poll interval.
        self._rooms_from_legacy_entry = CONF_ROOMS not in entry.data
        # Set when the fault poll's cloud backfill was refused for bad credentials: stop logging in
        # every poll; the reauth it starts reloads the entry with a fresh coordinator.
        self._backfill_auth_failed = False
        # device_id -> name just written to the Plejd cloud by an awaited rename; the device-registry
        # update that follows must not mirror it again (a late duplicate could undo a newer rename).
        self._already_mirrored: dict[str, str] = {}
        self.inputs = [PlejdCloudInput(**i) for i in entry.data.get(CONF_INPUTS, [])]
        self.motion = [PlejdCloudMotion(**m) for m in entry.data.get(CONF_MOTION, [])]
        self._motion_addresses = {m.address for m in self.motion}
        # device_id -> physical mesh address, for fault polling; absent on entries added
        # before this field existed (resolved via a one-time cloud fetch, see _async_poll_faults).
        self._device_addresses: dict[str, int] = dict(entry.data.get(CONF_DEVICE_ADDRESSES) or {})
        self.site_id = entry.data.get(CONF_SITE_ID, entry.entry_id)
        self._preferred = entry.data.get(CONF_DISCOVERED_ADDRESS)
        self._transport_pref = (getattr(entry, "options", None) or {}).get(CONF_TRANSPORT, TRANSPORT_AUTO)
        # Cloud credentials are used for the gateway token, the firmware-update check, and the cloud poll.
        self._email = entry.data.get(CONF_EMAIL, "")
        self._password = entry.data.get(CONF_PASSWORD, "")
        self._site_id = entry.data.get(CONF_SITE_ID, "")
        self._entry_id = entry.entry_id
        self._connection = PlejdConnection(
            bytes.fromhex(entry.data[CONF_CRYPTO_KEY]), self._on_event, self._handle_disconnect
        )
        # Gateway (remote/cloud) transport, when the site has one - preferred over BLE.
        self._gateway: PlejdGatewayConnection | None = None
        self.gateways = entry.data.get(CONF_GATEWAYS) or []  # GWY-01 device ids (no controllable output)
        gateways = self.gateways
        resource_set_id = entry.data.get(CONF_RESOURCE_SET_ID)
        if gateways and resource_set_id:
            self._gateway = PlejdGatewayConnection(
                async_get_clientsession(hass),
                entry.data[CONF_SITE_ID],
                resource_set_id,
                entry.data[CONF_INSTALLATION_ID],
                self._async_get_token,
                self._notify_outputs,
                self._handle_disconnect,
                on_event=self._on_event,
            )
        self._listeners: list[Callable[[], None]] = []
        self._button_listeners: list[Callable[[int, bool], None]] = []
        # mesh address -> (monotonic time, on/off value) of the last 0x0097 command seen for it, for the activity log
        self._recent_toggles: dict[int, tuple[float, bool]] = {}
        # Other recent mesh events that can explain a change, for the activity log:
        # last scene run (monotonic time, scene index) and last motion seen per motion-sensor address.
        self._recent_scene: tuple[float, int] | None = None
        self._recent_motion: dict[int, float] = {}
        self._motion_listeners: list[Callable[[MotionEvent], None]] = []
        self._fault_listeners: list[Callable[[int, frozenset[str]], None]] = []
        self._faults: dict[int, frozenset[str]] = {}
        self._climate_heating: dict[int, bool] = {}
        self.firmware: dict[str, PlejdFirmwareStatus] = {}  # device_id -> firmware status
        self._clock_unsub: Callable[[], None] | None = None
        self._faults_unsub: Callable[[], None] | None = None
        self._cloud_poll_unsub: Callable[[], None] | None = None
        self._firmware_unsub: Callable[[], None] | None = None
        self._firmware_now_unsub: Callable[[], None] | None = None
        self._available = False
        self._active: str | None = None  # "gateway" | "ble"
        self._closed = False
        self._reconnecting = False
        self._reconnect_task: asyncio.Task | None = None
        self._output_settings: dict[int, OutputSettings] = {}
        # Pre-populate from cloud so entities have a value before BLE reads return.
        for _device in self.devices:
            if _device.address is not None and _device.output_settings:
                _cloud_s = _settings_from_cloud(_device.output_settings)
                if _cloud_s is not None:
                    self._output_settings[_device.address] = _cloud_s

    def _active_transport(self) -> PlejdConnection | PlejdGatewayConnection | None:
        if self._active == "gateway":
            return self._gateway
        if self._active == "ble":
            return self._connection
        return None

    @property
    def available(self) -> bool:
        """Whether the active transport (gateway or BLE) is currently connected."""
        transport = self._active_transport()
        return self._available and transport is not None and transport.connected

    @property
    def active_transport(self) -> str | None:
        """The connected transport ('gateway' or 'ble'), or None when not connected."""
        return self._active if self.available else None

    @callback
    def _notify_outputs(self) -> None:
        for update in list(self._listeners):
            update()

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        """Register an output-state update callback; returns an unsubscribe function."""
        self._listeners.append(update)

        def _remove() -> None:
            self._listeners.remove(update)

        return _remove

    @callback
    def async_add_button_listener(self, cb: Callable[[int, bool], None]) -> Callable[[], None]:
        """Register a button callback cb(address, pressed); returns an unsubscribe."""
        self._button_listeners.append(cb)

        def _remove() -> None:
            self._button_listeners.remove(cb)

        return _remove

    @callback
    def async_add_motion_listener(self, cb: Callable[[MotionEvent], None]) -> Callable[[], None]:
        """Register a motion callback cb(MotionEvent); returns an unsubscribe."""
        self._motion_listeners.append(cb)

        def _remove() -> None:
            self._motion_listeners.remove(cb)

        return _remove

    @callback
    def async_add_fault_listener(self, cb: Callable[[int, frozenset[str]], None]) -> Callable[[], None]:
        """Register a device-health callback cb(address, fault_names); returns an unsubscribe."""
        self._fault_listeners.append(cb)

        def _remove() -> None:
            self._fault_listeners.remove(cb)

        return _remove

    def toggle_origin(
        self,
        output_address: int,
        window: float = 5.0,
        motion_window: float = 60.0,
        since: float | None = None,
        state: str | None = None,
    ) -> dict | None:
        """Where a just-seen on/off of `output_address` came from, as far as the mesh shows it.

        The mesh carries no sender: an on/off (0x0097) addressed to a room's group came from a whole-room
        command (in practice the Plejd app); one on a separate button input is that remote or wall
        switch; one on the output itself could be the Plejd app, a voice assistant through Plejd's
        cloud, or a switch wired to the device's own input - they look identical. None: nothing recent.
        """
        now = time.monotonic()

        def seen(address: int, match_state: bool = True) -> float | None:
            hit = self._recent_toggles.get(address)
            if hit is None:
                return None
            at, value = hit
            if now - at > window or (since is not None and at <= since):
                return None  # too old, or (with since) not newer than the command it would compete with
            # An on/off sent to a room or to the output carries the new state: one asking for the other state
            # can't explain this change. (A separate remote's press/release doesn't, so it isn't checked.)
            if match_state and state in ("on", "off") and value != (state == "on"):
                return None
            return at

        # Every command that could explain this change, newest first: a later command overrides an earlier one.
        candidates: list[tuple[float, dict]] = []
        for room in self.rooms:
            if output_address in room.member_addresses and (at := seen(room.address)) is not None:
                candidates.append((at, {"kind": "plejd_room", "name": room.name}))
        if (at := seen(output_address)) is not None:
            candidates.append((at, {"kind": "plejd_device"}))
        # A separate remote/wall switch pressed just before. Plejd doesn't say which outputs a button
        # drives, so this is a best guess (and it only wins when it is the newest command).
        own_device = next((d for d in self.devices if d.address == output_address), None)
        for button in self.inputs:
            if (own_device is None or button.device_id != own_device.device_id) and (
                at := seen(button.address, match_state=False)
            ) is not None:
                candidates.append((at, {"kind": "plejd_input", "name": button.name}))
        # A scene run on the mesh (Plejd app, a Plejd schedule, or a button bound to the scene).
        if (
            self._recent_scene is not None
            and now - self._recent_scene[0] <= window
            and (since is None or self._recent_scene[0] > since)
        ):
            index = self._recent_scene[1]
            scene = next((s for s in self.scenes if s.index == index), None)
            name = scene.name if scene else f"#{index}"
            # An output the scene doesn't set changed for another reason; unknown membership keeps the scene.
            if scene is None or scene.output_addresses is None or output_address in scene.output_addresses:
                candidates.append(
                    (
                        self._recent_scene[0],
                        {"kind": "plejd_scene", "name": name, "index": index, "at": self._recent_scene[0]},
                    )
                )
        # A Plejd motion sensor in the same room that saw motion shortly before (its own rule switched the light).
        room_id = own_device.room_id if own_device else None
        for sensor in self.motion:
            at = self._recent_motion.get(sensor.address)
            sensor_room = sensor.room_id or next(
                (d.room_id for d in self.devices if d.device_id == sensor.device_id), None
            )
            if (
                at is not None
                and now - at <= motion_window
                and (since is None or at > since)
                and room_id
                and sensor_room == room_id
            ):
                candidates.append((at, {"kind": "plejd_motion", "name": sensor.name}))
        return max(candidates, key=lambda c: c[0])[1] if candidates else None

    def faults_for(self, address: int) -> frozenset[str]:
        """Active fault-flag names last reported by a device (empty if none/unknown)."""
        return self._faults.get(address, frozenset())

    def device_address_for(self, device_id: str) -> int | None:
        """A physical device's own mesh address (for fault entities), or None if unresolved."""
        return self._device_addresses.get(device_id)

    @callback
    def _on_event(self, command: Command) -> None:
        # Every incoming mesh command's kind and target (never the payload), for tracing where a change came from.
        _LOGGER.debug("rx addr=%d cmd=0x%04x len=%d", command.address, command.command, len(command.data))
        if command.command in (CMD_GROUP_STATE_AND_LEVEL, CMD_OUTPUT_STATE_AND_LEVEL):
            if command.command == CMD_GROUP_STATE_AND_LEVEL:
                self._fan_group_state_to_members(command)
            if command.address in self._climate_addresses and len(command.data) > 3:
                self._climate_heating[command.address] = bool(command.data[3] & 0x80)
            for update in list(self._listeners):
                update()
        elif command.command == CMD_INPUT_BUTTON:
            if command.data[:1] in (b"\x00", b"\x01"):  # a malformed frame mustn't be credited with a change
                self._recent_toggles[command.address] = (time.monotonic(), command.data[0] == 1)
            pressed = bool(command.data and command.data[0])
            for cb in list(self._button_listeners):
                cb(command.address, pressed)
        elif command.command == CMD_SCENE and command.data[:1]:
            self._recent_scene = (time.monotonic(), command.data[0] & 0x7F)  # +0x80 marks a power-off scene
        elif command.command == CMD_OUTPUT_SET and command.address in self._motion_addresses:
            event = decode_motion(command)
            if event is not None:
                if event.motion:
                    self._recent_motion[event.address] = time.monotonic()
                for motion_cb in list(self._motion_listeners):
                    motion_cb(event)
        elif command.command in _SETTINGS_CMDS and command.command_type & protocol.TYPE_ACK:
            # Replies carry the Ack bit set (command_type = TYPE_READ|TYPE_ACK = 0x03, per
            # docs/protocol.md). Our own writes use TYPE_WRITE/TYPE_DONT_RESPOND (Ack unset)
            # and echo back on the same feed with a different payload shape ([output,
            # value...] vs. the reply's value-only bytes) — only a genuine reply is cacheable.
            self._update_output_settings(command)
        elif command.command == CMD_NOTIFY_EVENTS:
            faults = decode_notify_events(command)
            if faults is not None:
                self._faults[command.address] = faults
                for fault_cb in list(self._fault_listeners):
                    fault_cb(command.address, faults)

    def _update_output_settings(self, command: Command) -> None:
        """Store a settings read-reply and notify listeners."""
        cmd = command.command
        addr = command.address
        s = self._output_settings.get(addr) or OutputSettings()
        if cmd == CMD_OUTPUT_MIN_LEVEL:
            val = protocol.decode_output_level_reply(command)
            if val is None:
                return
            s.min_level = val
        elif cmd == CMD_OUTPUT_MAX_LEVEL:
            val = protocol.decode_output_level_reply(command)
            if val is None:
                return
            s.max_level = val
        elif cmd == CMD_OUTPUT_SPEED:
            val = protocol.decode_output_speed_reply(command)
            if val is None:
                return
            s.speed = val
        elif cmd == CMD_OUTPUT_CURVE_TYPE:
            val = protocol.decode_output_curve_reply(command)
            if val is None:
                return
            s.curve = val
        elif cmd == CMD_OUTPUT_PHASE_DIM_TYPE:
            val = protocol.decode_output_phase_dim_reply(command)
            if val is None:
                return
            s.phase_dim = val
        elif cmd == CMD_OUTPUT_BOOT_STATE:
            val = protocol.decode_output_boot_state_reply(command)
            if val is None:
                return
            s.boot_state = val
        elif cmd == CMD_OUTPUT_RELAY_OFF_TIME:
            val = protocol.decode_output_relay_off_time_reply(command)
            if val is None:
                return
            s.relay_off_time = val
        elif cmd == CMD_OUTPUT_RELAY_CONFIG:
            val = protocol.decode_output_relay_config_reply(command)
            if val is None:
                return
            s.relay_pole_config = val
        else:  # CMD_OUTPUT_INRUSH_CURRENT
            val = protocol.decode_output_inrush_current_reply(command)
            if val is None:
                return
            s.inrush_current_ms = val
        self._output_settings[addr] = s
        self._notify_outputs()

    def settings_for(self, address: int) -> OutputSettings | None:
        """Last-known per-output settings for a mesh address, if read."""
        return self._output_settings.get(address)

    def _cache_output_setting(self, address: int, **fields: object) -> None:
        """Optimistically update the settings cache after a local write.

        Without this, a BLE notification for an unrelated field arriving right after
        a local write would re-read the old cached value and overwrite the entity's
        new state until the next full settings read.
        """
        s = self._output_settings.get(address) or OutputSettings()
        for name, value in fields.items():
            setattr(s, name, value)
        self._output_settings[address] = s
        self._notify_outputs()

    def climate_state_for(self, address: int) -> ThermostatState | None:
        """Last-known thermostat state, decoded from the output-state report."""
        state = self.state_for(address)
        if state is None or state.raw_level is None:
            return None
        decoded = decode_thermostat_state(state.on, state.raw_level)
        heating = self._climate_heating.get(address)
        if heating is None:
            return decoded
        return ThermostatState(mode=decoded.mode, target=decoded.target, current=decoded.current, heating=heating)

    def state_for(self, address: int) -> OutputState | None:
        """Last-known output state for a mesh address, if seen."""
        if self._active == "gateway" and self._gateway is not None:
            return self._gateway.state_for(address)
        if self._connection.mesh is None:
            return None
        return self._connection.mesh.state.get(address)

    def _pick_device(self) -> bluetooth.BluetoothServiceInfoBleak | None:
        candidates = [
            info
            for info in bluetooth.async_discovered_service_info(self.hass, connectable=True)
            if PLEJD_SERVICE_UUID in info.service_uuids
        ]
        if not candidates:
            return None
        # Prefer the device the config flow discovered, else the strongest signal.
        # rssi can be None for adverts without a reported signal — treat as weakest.
        candidates.sort(key=lambda info: (info.address != self._preferred, -(info.rssi or -127)))
        return candidates[0]

    async def async_start(self) -> None:
        """Connect to the mesh — gateway-first when one exists, else BLE."""
        try:
            await self._async_select_and_connect()
        except ConfigEntryNotReady:
            # A stale gateway/crypto-key/etc is exactly what the cloud poll below exists to
            # repair, but its recurring timer never gets registered when connect fails here,
            # and a setup retry would otherwise keep reusing the same stale entry data
            # forever - make one best-effort attempt to refresh it before giving up this
            # attempt, so the next retry (which re-reads entry.data fresh) has a shot at it.
            # An unexpected failure from that attempt (e.g. a malformed cloud response) must
            # not replace this ConfigEntryNotReady - HA's setup-retry path is keyed on this
            # exact exception type, and the cached site may still work once BLE is back in
            # range even if the repair attempt itself failed.
            # reload=False: async_setup_entry (which called this) is still in progress for
            # THIS entry - reloading it reentrantly here could hang or be rejected instead of
            # taking effect. Persist the repaired snapshot only; the re-raise below triggers
            # HA's own setup retry, which re-reads entry.data fresh on its next attempt.
            if self._should_attempt_self_heal():
                try:
                    await self._async_poll_cloud(None, reload=False)
                except Exception:  # noqa: BLE001 - best-effort; the original ConfigEntryNotReady below still applies
                    _LOGGER.warning("Plejd: cloud self-heal attempt during setup failed", exc_info=True)
            raise
        self._cloud_poll_unsub = async_track_time_interval(self.hass, self._async_poll_cloud, CLOUD_POLL_INTERVAL)
        self._schedule_firmware_checks()

    def _note_malformed_poll(self, collections: list[str]) -> None:
        """Raise a repair issue for a skipped sync.

        Skipping a malformed snapshot is the right call, but it is also completely silent:
        without this, a site whose daily sync has been skipped looks identical to one that
        simply has not changed.
        """
        issue_registry.async_create_issue(
            self.hass,
            DOMAIN,
            f"malformed_cloud_site_{self._entry_id}",
            is_fixable=False,
            # Repair issues are dropped on restart unless persisted, and the streak counter
            # lives in hass.data so it resets too - between them, restarting mid-incident
            # would hide the warning and then need two more 24h polls to earn it back.
            is_persistent=True,
            severity=issue_registry.IssueSeverity.WARNING,
            translation_key="malformed_cloud_site",
            translation_placeholders={"collections": ", ".join(collections)},
        )

    def _clear_malformed_polls(self) -> None:
        """Drop the repair issue once the cloud serves a usable snapshot again."""
        async_clear_malformed_site_issue(self.hass, self._entry_id)

    def _should_attempt_self_heal(self) -> bool:
        """True at most once per SELF_HEAL_COOLDOWN, recording the attempt.

        HA retries a failed setup on its own schedule, and for a BLE-only site whose mesh is
        simply out of range that retry can repeat indefinitely - without this, every attempt
        would mean a fresh login + getSiteById against the Plejd cloud.
        """
        last: float | None = self.hass.data.get(DATA_LAST_SELF_HEAL, {}).get(self._entry_id)
        now = time.monotonic()
        if last is not None and now - last < SELF_HEAL_COOLDOWN_SECONDS:
            _LOGGER.debug(
                "Plejd: skipping the setup self-heal, last attempt was %.0fs ago (cooldown %.0fs)",
                now - last,
                SELF_HEAL_COOLDOWN_SECONDS,
            )
            return False
        self.hass.data.setdefault(DATA_LAST_SELF_HEAL, {})[self._entry_id] = now
        return True

    async def _async_poll_cloud(self, _now: object, *, reload: bool = True) -> None:
        """Detect site changes and reload the integration if anything differs.

        reload=False persists a repaired snapshot without reloading - see async_start's
        setup-time self-heal call, which must not reload the entry it's still setting up.
        """
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return
        session = async_get_clientsession(self.hass)
        try:
            token = await async_login(session, self._email, self._password)
            site = await async_get_site(session, token, self._site_id)
        except PlejdAuthError:
            # No gateway connect path exists in BLE-only setups, so prompt reauth here -
            # otherwise a rejected password leaves the daily poll silently broken forever
            # (Reconfigure alone can't fix it: it reuses the stored, now-invalid password).
            if self._closed:
                return
            _LOGGER.warning("Plejd cloud poll: credentials rejected — starting reauth")
            # The cooldown is deliberately NOT released here. Until the user actually
            # completes reauth the credentials stay bad, so every setup retry would call
            # async_login again - exactly the stream of cloud logins the cooldown exists to
            # stop. The successful reauth path clears it instead (see
            # async_reset_self_heal_cooldown), which is the point at which a fresh attempt
            # can actually succeed.
            self._entry.async_start_reauth(self.hass)
            return
        except (PlejdCloudError, ClientError, OSError):
            # Transport/cloud-API failure (DNS/socket/TLS/timeout/non-JSON 5xx) is a missed
            # poll, not a bug; retry at the next interval. An unexpected parse/programming
            # error is NOT caught here and propagates, per error-handling.md.
            _LOGGER.debug("Plejd cloud poll: cloud unreachable, will retry at next interval", exc_info=True)
            return
        if self._closed:
            # Shutdown began while the two cloud calls above were in flight; the interval
            # timer is already unregistered, but this already-running call must not act on
            # a possibly-removed entry (start reauth, persist a stale snapshot) after that.
            return
        # NOTE(8408323): if a move_device_to_room move is still pending (cloud not yet
        # converged), this snapshot's device room_id/room membership can overwrite that
        # move's local correction before it converges - the same class of "we can only
        # protect what we ourselves touch" tradeoff manage_device_room.py's own module
        # docstring already documents and accepts for every other feature's refresh cycle,
        # now also triggered by this poll. Not solved here for the same reason: doing so
        # would need pending-move awareness plumbed through this poll too.
        # No sorting needed here: parse_site() already returns every collection in a canonical
        # order, so the config flow's stored snapshot and this diff agree by construction.
        devices = [asdict(d) for d in site.devices]
        inputs = [asdict(i) for i in site.inputs]
        motion = [asdict(m) for m in site.motion]
        scenes = [asdict(s) for s in site.scenes]
        rooms = [asdict(r) for r in site.rooms]
        gateways = list(site.gateways)
        # parse_site() distinguishes "this collection's raw source field was absent or the
        # wrong type" (site.malformed) from "the cloud correctly reports zero of these now"
        # (a real, empty list) - only the former is untrustworthy. Treating emptiness alone
        # as suspicious would also block a legitimate last-scene/last-room deletion (and any
        # other unrelated site change) from ever syncing, since every later poll's diff
        # would keep finding the same "still empty" non-difference forever.
        if site.malformed:
            _LOGGER.warning(
                "Plejd cloud poll: site response is malformed (%s) - skipping this poll",
                ", ".join(sorted(site.malformed)),
            )
            self._note_malformed_poll(sorted(site.malformed))
            return
        self._clear_malformed_polls()
        # A response that still lists the gateway but transiently drops its resourceSetId
        # (and the resourceSets fallback) must not be read as "the gateway is gone": keeping
        # the cached id lets the gateway transport carry on, whereas overwriting it with None
        # takes a gateway-only install offline until the next poll a whole day later. If
        # access really was revoked the transport just fails and falls back to BLE, which is
        # the far cheaper wrong guess. A genuinely removed gateway (empty gateways) still
        # clears it. Deliberately not treated as a malformed response: a gateway with no
        # resourceSetId is a representable state the rest of the integration already handles
        # as "no usable gateway" (see config_flow's own has_gateway check), so flagging it
        # would block every future poll for such a site - the exact trap the malformed
        # checks above were added to avoid.
        # Only reusable while the gateway set is UNCHANGED: a cached grant belongs to the
        # gateway it was issued for, so copying it onto a replacement gateway (gw-old swapped
        # for gw-new) would keep has_gateway true and rebuild the connection with an obsolete
        # resource set forever - every later poll would see the same cached value and never
        # repair it. A changed set falls through to None, which correctly degrades to BLE.
        resource_set_id = site.resource_set_id
        if gateways and resource_set_id is None and gateways == (entry.data.get(CONF_GATEWAYS) or []):
            resource_set_id = entry.data.get(CONF_RESOURCE_SET_ID)
        # Mirrors manage_device.py's own device-removal refresh: drop a forced gateway-only
        # preference once there's no usable gateway left, or a gateway disappearing here
        # (removed in the app) would leave the coordinator stuck raising ConfigEntryNotReady
        # forever on reload instead of falling back to BLE.
        has_gateway = bool(gateways and resource_set_id)

        def _transport_for(current: str) -> str:
            # Only a gateway-only preference actually becomes impossible without a gateway. An
            # explicit BLE choice stays valid and must survive, or an unrelated site change
            # would silently downgrade it to AUTO and start using a gateway added later on
            # its own. Deferred into a callable because the answer depends on the preference
            # as it is when we actually write, which may be newer than what we read here.
            return TRANSPORT_AUTO if not has_gateway and current == TRANSPORT_GATEWAY else current

        new_transport = _transport_for(entry.options.get(CONF_TRANSPORT, TRANSPORT_AUTO))
        new_data = {
            CONF_CRYPTO_KEY: site.crypto_key.hex(),
            CONF_DEVICES: devices,
            CONF_INPUTS: inputs,
            CONF_MOTION: motion,
            CONF_SCENES: scenes,
            CONF_ROOMS: rooms,
            CONF_ROOM_NAMES: {r.room_id: r.name for r in site.all_rooms},
            CONF_GATEWAYS: gateways,
            CONF_RESOURCE_SET_ID: resource_set_id,
            CONF_DEVICE_ADDRESSES: site.device_addresses,
        }
        # A gateway newly appearing on an entry that predates CONF_INSTALLATION_ID (or
        # never had one) must seed it now - the gateway transport requires it and the
        # reload below would otherwise crash with a KeyError instead of applying this.
        if gateways and not entry.data.get(CONF_INSTALLATION_ID):
            new_data[CONF_INSTALLATION_ID] = str(uuid4())
        changed = [k for k, v in new_data.items() if entry.data.get(k) != v]
        new_options = {**entry.options, CONF_TRANSPORT: new_transport}
        transport_changed = new_options[CONF_TRANSPORT] != entry.options.get(CONF_TRANSPORT, TRANSPORT_AUTO)
        if not changed and not transport_changed:
            return
        if not reload:
            # Setup-time self-heal (see async_start): persist only, no reload - the caller's
            # own re-raised ConfigEntryNotReady triggers HA's setup retry instead.
            _LOGGER.info(
                "Plejd site changed (%s) — persisting for the next setup retry", ", ".join(changed) or "transport"
            )
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, **new_data}, options=new_options)
            return
        _LOGGER.info("Plejd site changed (%s) — reloading to apply updates", ", ".join(changed) or "transport")
        # Reload explicitly (rather than relying on the entry's update listener alone) so a
        # rejected reload (e.g. a platform refused to unload) is actually noticed - otherwise
        # entry.data already matches the fresh site and the running coordinator never
        # reflects it. Guarded by the shared per-entry reload lock so the listener doesn't
        # also fire a second, racing reload for this same change.
        # Populated inside the lock, immediately before the write, so the rollback restores
        # exactly what this poll overwrote - a snapshot taken out here would instead restore
        # values from before any concurrent operation that landed while we waited for the lock.
        overwritten_data: dict[str, Any] = {}
        overwritten_transport: tuple[str, ...] = ()
        written_transport: str | None = None  # what we actually wrote, to detect a newer edit
        # What the site-derived keys looked like when the diff above was computed. If they
        # differ once we hold the lock, a management operation (device/room/scene/schedule)
        # landed in between and its result is NEWER than this poll's fetch - applying our
        # overlay would revert the thing the user just did.
        pre_lock_values = {key: entry.data.get(key) for key in new_data}
        reload_ok = True
        lock = schedule_ws.async_get_reload_lock(self.hass, entry.entry_id)
        try:
            async with lock:
                if self._closed:
                    # Waiting for the lock is an await point, so the operation holding it (or an
                    # independent unload) can have shut this coordinator down meanwhile - the
                    # interval unsubscribe cannot stop an already-running callback. Writing now
                    # would reload the entry after our own teardown, racing entry removal or
                    # immediately reloading the coordinator that replaced us.
                    _LOGGER.debug("Plejd cloud poll: coordinator was shut down while waiting for the lock")
                elif any(entry.data.get(key) != value for key, value in pre_lock_values.items()):
                    # Skip rather than overwrite: the next interval re-fetches, and a
                    # management operation's own reload has already applied its change.
                    _LOGGER.info(
                        "Plejd cloud poll: the entry changed while waiting for the reload lock — "
                        "skipping this poll so the newer change stands"
                    )
                else:
                    schedule_ws.async_mark_expecting_self_reload(self.hass, entry.entry_id)
                    # Everything is read HERE, inside the lock, not from the pre-lock snapshot
                    # the diff above was computed from: if another operation held the lock
                    # while we waited, writing back anything captured before that would
                    # discard its change permanently.
                    overwritten_data = {key: entry.data[key] for key in new_data if key in entry.data}
                    overwritten_transport = (entry.options[CONF_TRANSPORT],) if CONF_TRANSPORT in entry.options else ()
                    written_transport = _transport_for(entry.options.get(CONF_TRANSPORT, TRANSPORT_AUTO))
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, **new_data},
                        options={**entry.options, CONF_TRANSPORT: written_transport},
                    )
                    try:
                        reload_ok = await self.hass.config_entries.async_reload(entry.entry_id)
                    except Exception:  # noqa: BLE001 - treated like a rejected reload below
                        _LOGGER.exception("Plejd cloud poll: reload after a site change raised")
                        reload_ok = False
        finally:
            schedule_ws.async_consume_expected_self_reload(self.hass, entry.entry_id)
        follow_up_ok = False
        if schedule_ws.async_take_reload_pending(self.hass, entry.entry_id):
            async with lock:
                try:
                    follow_up_ok = await self.hass.config_entries.async_reload(entry.entry_id)
                except Exception:  # noqa: BLE001 - treated like a rejected follow-up reload below
                    _LOGGER.exception("Plejd cloud poll: follow-up reload for a concurrent change raised")
                    follow_up_ok = False
            if not follow_up_ok:
                # A rejected (not raised) reload is silently dropping someone else's change
                # otherwise - re-mark it pending so the next successful reload (from this
                # poll or any other operation) picks it up, instead of leaving the running
                # coordinator stale until an unrelated site/option change happens to retry it.
                _LOGGER.warning("Plejd cloud poll: follow-up reload for a concurrent change failed; leaving it pending")
                schedule_ws.async_mark_reload_pending(self.hass, entry.entry_id)
        if not reload_ok and not follow_up_ok:
            # Revert the cached snapshot rather than just logging: leaving entry.data
            # already matching the fresh site would make every later poll's comparison
            # find no difference and never retry, stranding the running coordinator (which
            # never actually got the new data live) stale until an unrelated cloud change
            # or an HA restart. Reverting means the next poll's diff naturally retries this.
            #
            # A follow-up reload that succeeded is deliberately treated as this poll having
            # landed after all: it reloaded the entry with our data already written, so the
            # sync IS live and rolling back would undo a working state.
            _LOGGER.warning(
                "Plejd cloud poll: reload after a site change failed; reverting the cached "
                "snapshot so the next poll retries it"
            )
            async with lock:
                # Restore a key only if its CURRENT value is still the one this poll wrote.
                # Reconfigure and the options flow do not take this integration-specific lock,
                # so a newer edit to one of these keys is reachable even here - and undoing it
                # would be the very destruction this rollback exists to avoid.
                reverted_data = {**entry.data}
                for key, written in new_data.items():
                    if entry.data.get(key) != written:
                        continue  # someone edited it after us; theirs is newer, leave it alone
                    if key in overwritten_data:
                        reverted_data[key] = overwritten_data[key]
                    else:
                        reverted_data.pop(key, None)  # a key this poll introduced (e.g. installation id)
                reverted_options = {**entry.options}
                if entry.options.get(CONF_TRANSPORT) == written_transport:
                    if overwritten_transport:
                        reverted_options[CONF_TRANSPORT] = overwritten_transport[0]
                    else:
                        reverted_options.pop(CONF_TRANSPORT, None)
                schedule_ws.async_mark_expecting_self_reload(self.hass, entry.entry_id)
                try:
                    self.hass.config_entries.async_update_entry(entry, data=reverted_data, options=reverted_options)
                    # A reload that unloaded the old entry but then failed to set the new one
                    # up has already torn down this coordinator - including the poll timer
                    # that the revert above is counting on for the retry. Bring the entry back
                    # up on the known-good snapshot instead of leaving nothing loaded and
                    # nothing scheduled, which would strand it until an HA restart.
                    try:
                        if not await self.hass.config_entries.async_reload(entry.entry_id):
                            _LOGGER.warning("Plejd cloud poll: reload of the reverted snapshot was rejected")
                    except Exception:  # noqa: BLE001 - best-effort recovery, already logging a failure
                        _LOGGER.exception("Plejd cloud poll: reload of the reverted snapshot raised")
                finally:
                    schedule_ws.async_consume_expected_self_reload(self.hass, entry.entry_id)

    def _schedule_firmware_checks(self) -> None:
        """Check firmware shortly after start, then daily; both are best-effort."""
        if self._firmware_unsub is not None:
            return  # already scheduled (e.g. a second async_start)
        self._firmware_now_unsub = async_call_later(self.hass, 0, self._async_firmware_check)
        self._firmware_unsub = async_track_time_interval(
            self.hass, self._async_firmware_check, FIRMWARE_REFRESH_INTERVAL
        )

    async def _async_firmware_check(self, _now: object) -> None:
        self._firmware_now_unsub = None  # the one-shot has fired
        try:
            await self.async_refresh_firmware()
        except Exception:  # noqa: BLE001 - auxiliary + cloud-dependent; warn (e.g. lapsed credentials) but never disrupt
            _LOGGER.warning("Plejd firmware check failed", exc_info=True)

    async def async_refresh_firmware(self) -> None:
        """Refresh installed vs. latest firmware for every physical device from the cloud."""
        if not self._email or not self._password:
            return
        session = async_get_clientsession(self.hass)
        token = await async_login(session, self._email, self._password)
        site = await async_get_site(session, token, self.site_id)
        latest_by_hw: dict[tuple[int, str | None], tuple[str, int] | None] = {}
        status: dict[str, PlejdFirmwareStatus] = {}
        for device_id, firmware in site.firmware_by_device.items():
            key = (firmware.hardware_id, firmware.faceplate_id)
            if key not in latest_by_hw:
                try:
                    latest_by_hw[key] = await async_get_available_firmware(
                        session, token, firmware.hardware_id, firmware.faceplate_id
                    )
                except Exception:  # noqa: BLE001 - one flaky lookup must not discard the whole refresh
                    _LOGGER.debug("Plejd firmware lookup failed for hardware %s", firmware.hardware_id, exc_info=True)
                    latest_by_hw[key] = None
            latest = latest_by_hw[key]
            status[device_id] = PlejdFirmwareStatus(
                installed_version=firmware.version,
                installed_build_time=firmware.build_time,
                latest_version=latest[0] if latest else None,
                latest_build_time=latest[1] if latest else None,
            )
        self.firmware = status
        self._notify_outputs()

    @staticmethod
    def _output_parse_id(
        devices: list[PlejdCloudDevice], device_id: str, output_index: int | None = None
    ) -> str | None:
        # The title lives on the output; a device rename targets the primary output's Parse id.
        # Primary = lowest output_index (consistent with the unique_id base convention).
        matching = sorted(
            (
                d
                for d in devices
                if d.device_id == device_id and d.object_id and (output_index is None or d.output_index == output_index)
            ),
            key=lambda d: d.output_index,
        )
        return matching[0].object_id if matching else None

    def skip_next_mirror(self, device_id: str, name: str) -> None:
        """The next device-registry rename of `device_id` to `name` is already in the Plejd cloud."""
        self._already_mirrored[device_id] = name

    async def async_rename_device(self, device_id: str, title: str, output_index: int | None = None) -> None:
        """Rename a device's output in the Plejd cloud (so the Plejd app shows it too).

        With no output_index this mirrors an HA device rename onto the primary output and skips
        quietly when that can't be done; naming one output explicitly (the dashboard) raises instead.
        """
        explicit = output_index is not None
        if not self._email or not self._password:
            if explicit:
                raise HomeAssistantError("Plejd has no cloud credentials to rename with")
            return
        session = async_get_clientsession(self.hass)
        token = await async_login(session, self._email, self._password)
        parse_id = self._output_parse_id(self.devices, device_id, output_index)
        if parse_id is None:
            # Entries cached before object_id existed lack it — resolve from a fresh site fetch.
            site = await async_get_site(session, token, self.site_id)
            parse_id = self._output_parse_id(site.devices, device_id, output_index)
        if parse_id is None:
            if explicit:
                raise HomeAssistantError(f"Plejd has no cloud object for output {output_index} of device {device_id}")
            _LOGGER.debug("Plejd rename skipped: no Parse id for device %s", device_id)
            return
        if not await async_set_device_title(session, token, self.site_id, device_id, parse_id, title):
            raise HomeAssistantError(f"Plejd rejected the rename of device {device_id}")

    async def async_handle_device_registry_update(self, event: object) -> None:
        """When the user renames one of our devices in HA, push the new name to Plejd."""
        data = getattr(event, "data", {}) or {}
        if data.get("action") != "update" or "name_by_user" not in (data.get("changes") or {}):
            return
        device_id = data.get("device_id")
        if not device_id:
            return
        registry = device_registry.async_get(self.hass)
        if registry is None:
            return
        device = registry.async_get(device_id)
        if device is None:
            return
        plejd_id = next((ident for (domain, ident) in device.identifiers if domain == DOMAIN), None)
        name = device.name_by_user
        if plejd_id is None or not name:
            return
        if plejd_id.startswith(ROOM_DEVICE_ID_PREFIX):
            return  # a room pseudo-device has no Parse cloud object to rename
        if self._already_mirrored.pop(plejd_id, None) == name:
            return  # the dashboard already renamed it in Plejd and waited for the result
        try:
            await self.async_rename_device(plejd_id, name)
        except PlejdAuthError:
            # No gateway connect path exists in BLE-only setups, so prompt reauth here.
            self._entry.async_start_reauth(self.hass)
        except Exception:  # noqa: BLE001 - mirroring a rename is auxiliary and must never disrupt HA
            _LOGGER.warning("Plejd: could not mirror the device rename to the Plejd app", exc_info=True)

    async def _async_select_and_connect(self) -> None:
        """Connect over the chosen transport.

        Honour the user's forced preference; otherwise prefer the gateway and fall
        back to BLE when it's absent or unreachable.
        """
        if self._transport_pref == TRANSPORT_BLE:
            await self._async_connect_ble()
            return
        if self._transport_pref == TRANSPORT_GATEWAY:
            if self._gateway is None:
                raise ConfigEntryNotReady("Gateway transport selected, but this site has no gateway")
            await self._async_connect_gateway()
            return
        if self._gateway is not None:
            try:
                await self._async_connect_gateway()
                return
            except ConfigEntryNotReady as err:
                _LOGGER.warning("Plejd gateway connect failed, falling back to BLE: %s", err)
        await self._async_connect_ble()

    async def _async_connect_gateway(self) -> None:
        try:
            await self._gateway.connect()
        except PlejdAuthError as err:
            # Bad/expired cloud credentials: trigger reauth, don't mask as a transient outage.
            raise ConfigEntryAuthFailed("Plejd cloud credentials rejected") from err
        except Exception as err:  # noqa: BLE001 - transient gateway failure: fall back / retry
            raise ConfigEntryNotReady(f"gateway connect failed: {err}") from err
        self._active = "gateway"
        self._available = True
        self._notify_outputs()
        await self._start_fault_polling()

    async def _async_connect_ble(self) -> None:
        info = self._pick_device()
        if info is None:
            raise ConfigEntryNotReady("no Plejd device in range")
        device = bluetooth.async_ble_device_from_address(self.hass, info.address, connectable=True)
        if device is None:
            raise ConfigEntryNotReady(f"could not resolve {info.address}")
        _LOGGER.debug("connecting to Plejd mesh via %s", info.address)
        try:
            await self._connection.connect(device)
        except Exception as err:  # noqa: BLE001 - surface any BLE failure as a setup retry
            raise ConfigEntryNotReady(f"failed to connect: {err}") from err
        await self._async_read_all_states()
        await self._async_read_all_settings()
        self._active = "ble"
        self._available = True
        self._notify_outputs()
        await self._start_fault_polling()
        try:
            await self.async_sync_clock()
        except Exception:  # noqa: BLE001 - clock sync is auxiliary, never fail setup over it
            _LOGGER.warning("Plejd clock sync after connect failed", exc_info=True)
        # Keep device RTCs aligned (they drive on-device time/astro events) across drift + DST.
        if self._clock_unsub is not None:
            self._clock_unsub()
        self._clock_unsub = async_track_time_interval(self.hass, self._async_periodic_clock_sync, timedelta(days=1))

    async def _async_periodic_clock_sync(self, _now: object) -> None:
        try:
            await self.async_sync_clock()
        except Exception:  # noqa: BLE001 - best-effort; a missed daily sync is not fatal
            _LOGGER.warning("Plejd periodic clock sync failed", exc_info=True)

    async def _start_fault_polling(self) -> None:
        """Poll device health (NotifyEvents) periodically; replies arrive push-side."""
        if self._faults_unsub is not None:
            self._faults_unsub()
        self._faults_unsub = async_track_time_interval(self.hass, self._async_poll_faults, NOTIFY_POLL_INTERVAL)
        await self._async_poll_faults(None)

    async def _async_poll_faults(self, _now: object) -> None:
        # NotifyEvents belongs to the physical device, not any one output — poll the
        # device's own mesh address (may differ from an output's outputAddress).
        need_device_addresses = not self._device_addresses
        need_rooms = self._rooms_from_legacy_entry
        # Entries from before CONF_ROOM_NAMES: fill it in now rather than wait for the daily sync,
        # or lights in rooms without a group light sit under "Other lights" until then.
        need_room_names = CONF_ROOM_NAMES not in self._entry.data
        needs_cloud = need_device_addresses or need_rooms or need_room_names
        if needs_cloud and self._email and self._password and not self._backfill_auth_failed:
            try:
                session = async_get_clientsession(self.hass)
                token = await async_login(session, self._email, self._password)
                site = await async_get_site(session, token, self.site_id)
                if need_device_addresses:
                    self._device_addresses = dict(site.device_addresses)
                data_updates: dict = {}
                # Like the daily poll: never persist a snapshot the cloud sent malformed. Leaving
                # the keys unset keeps this backfill retrying on the next poll.
                if site.malformed:
                    _LOGGER.debug(
                        "Plejd fault poll: malformed site (%s), not backfilling rooms", sorted(site.malformed)
                    )
                    need_rooms = need_room_names = False
                if need_rooms:
                    self.rooms = site.rooms
                    self._rooms_from_legacy_entry = False
                    # Persist to entry.data, not just the in-memory coordinator: the light
                    # platform only builds PlejdRoomLight entities from CONF_ROOMS at setup,
                    # so an unpersisted backfill would lose room entities on the next
                    # restart/reload if the cloud is unreachable then (#86 review).
                    data_updates[CONF_ROOMS] = [asdict(r) for r in site.rooms]
                if need_room_names:
                    data_updates[CONF_ROOM_NAMES] = {r.room_id: r.name for r in site.all_rooms}
                if data_updates:
                    self.hass.config_entries.async_update_entry(self._entry, data={**self._entry.data, **data_updates})
            except PlejdAuthError:
                # Retrying can't succeed with these credentials; ask for new ones instead.
                self._backfill_auth_failed = True
                if not self._closed:
                    _LOGGER.warning("Plejd fault poll: credentials rejected — starting reauth")
                    self._entry.async_start_reauth(self.hass)
            except Exception:  # noqa: BLE001 - fault polling is best-effort; retry next interval
                _LOGGER.debug("Plejd fault poll: could not resolve device addresses", exc_info=True)
        for address in set(self._device_addresses.values()):
            try:
                await self._write_vector(protocol.request_notify_events(address))
            except Exception:  # noqa: BLE001 - best-effort per device; one failure shouldn't skip the rest
                _LOGGER.debug("Plejd fault poll failed for device %s", address, exc_info=True)
                continue

    async def _async_get_token(self) -> str:
        """Fetch a fresh Parse session token for the gateway WebSocket (login each time)."""
        return await async_login(async_get_clientsession(self.hass), self._email, self._password)

    async def _async_read_all_states(self) -> None:
        """Ask each output for its current state so entities populate on connect."""
        mesh = self._connection.mesh
        if mesh is None:
            return
        seen: set[int] = set()
        for device in self.devices:
            if device.address is None or device.address in seen:
                continue
            seen.add(device.address)
            await self._connection.write(mesh.request_output(device.address, device.output_index))

    async def _async_read_all_settings(self) -> None:
        """Read per-output settings from each addressable device after BLE connect."""
        mesh = self._connection.mesh
        if mesh is None:
            return
        seen: set[int] = set()
        for device in self.devices:
            if device.address is None or device.address in seen:
                continue
            seen.add(device.address)
            addr, out = device.address, device.output_index
            if device.dimmable:
                await self._connection.write(mesh.encrypt(protocol.request_output_min_level(addr, out)))
                await self._connection.write(mesh.encrypt(protocol.request_output_max_level(addr, out)))
                await self._connection.write(mesh.encrypt(protocol.request_output_speed(addr, out)))
                await self._connection.write(mesh.encrypt(protocol.request_output_curve(addr, out)))
                await self._connection.write(mesh.encrypt(protocol.request_output_phase_dim(addr, out)))
                if device.hardware_id in PHASE_DIM_HARDWARE:
                    await self._connection.write(mesh.encrypt(protocol.request_output_inrush_current(addr, out)))
            if device.category in (CATEGORY_LIGHT, CATEGORY_SWITCH):
                await self._connection.write(mesh.encrypt(protocol.request_output_boot_state(addr, out)))
            if device.hardware_id in RELAY_HARDWARE:
                await self._connection.write(mesh.encrypt(protocol.request_output_relay_off_time(addr, out)))
            if device.hardware_id in RELAY_CONFIG_HARDWARE:
                await self._connection.write(mesh.encrypt(protocol.request_output_relay_config(addr, out)))

    @callback
    def _handle_disconnect(self) -> None:
        # The link dropped: entities go unavailable until a reconnect succeeds.
        self._available = False
        self._notify_outputs()
        # A running reconnect loop re-checks `available` after its attempt, so it picks this drop up itself;
        # replacing it here would leave shutdown cancelling a finished task while that loop kept going.
        if not self._closed and not self._reconnecting:
            self._reconnect_task = self._spawn(self._async_reconnect())

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task:
        # Prefer HA's owned background task; fall back to a bare task outside HA (tests).
        create = getattr(self.hass, "async_create_background_task", None)
        if create is not None:
            return create(coro, name="plejd-reconnect")
        return asyncio.ensure_future(coro)

    async def _async_reconnect(self) -> None:
        """Reconnect (gateway or BLE) with exponential backoff until it succeeds or we close.

        Any failure to (re)connect must keep this loop alive: a single uncaught
        exception here silently kills the background task and the transport
        never reconnects again on its own, even if the underlying issue clears.
        """
        if self._reconnecting:
            return
        self._reconnecting = True
        delay = RECONNECT_INITIAL_DELAY
        try:
            while not self._closed and not self.available:
                await asyncio.sleep(delay)
                if self._closed:
                    return
                try:
                    await self._async_select_and_connect()
                    # No return: a drop reported while this attempt was connecting (a socket that closed
                    # before its receive loop started) only reaches _handle_disconnect, whose new loop
                    # bails out on _reconnecting. Re-checking `available` keeps that drop from being lost.
                    _LOGGER.debug("reconnected to Plejd over %s", self._active)
                except ConfigEntryAuthFailed:
                    # Retrying with the same rejected credentials can't succeed; prompt
                    # reauth and stop until the entry reloads with fresh ones.
                    self._entry.async_start_reauth(self.hass)
                    _LOGGER.warning("Plejd reconnect stopped: cloud credentials rejected, reauth requested")
                    return
                except ConfigEntryNotReady as err:
                    # Expected transient condition (device out of range, gateway
                    # unreachable, ...) — quiet, keeps retrying.
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)
                    _LOGGER.debug("Plejd reconnect failed (retry in %ss): %s", delay, err)
                except Exception:  # noqa: BLE001 - unexpected bug: keep retrying, but make it visible
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)
                    _LOGGER.warning("Plejd reconnect hit an unexpected error (retry in %ss)", delay, exc_info=True)
        finally:
            self._reconnecting = False

    async def _write_vector(self, vector: bytes) -> None:
        """Send a plaintext command vector over the active transport.

        The gateway relays plaintext (it holds the crypto key); BLE encrypts first.
        """
        if self._active == "gateway" and self._gateway is not None and self._gateway.connected:
            await self._gateway.write(vector)
            return
        mesh = self._connection.mesh
        if mesh is None:
            raise HomeAssistantError("Plejd mesh is not connected")
        await self._connection.write(mesh.encrypt(vector))

    async def async_sync_clock(self) -> None:
        """Broadcast the current local wall-clock to every mesh device (0x001B)."""
        now = dt_util.now()
        epoch = int(now.timestamp() + now.utcoffset().total_seconds())
        await self._write_vector(protocol.set_timestamp(epoch))

    async def async_program_time_event(
        self, slot: int, mask: int, hour: int, minute: int, second: int, scene: int, fade: int
    ) -> None:
        """Program an on-device weekly time event that runs a scene (3-step config)."""
        await self._write_vector(protocol.set_time_event_time(slot, mask, hour, minute, second, TIME_EVENT_REP_FOREVER))
        await self._write_vector(protocol.set_time_event_type(slot, TIME_EVENT_RESULT_SCENE))
        await self._write_vector(protocol.set_time_event_scene(slot, scene, fade))

    async def async_remove_time_event(self, slot: int) -> None:
        """Delete an on-device time event."""
        await self._write_vector(protocol.remove_time_event(slot))

    async def async_set_output(self, address: int, on: bool, level: int, *, notify: bool = True) -> None:
        """Send an on/off + level command for an output.

        Uses 0x0098 (`set_group_state_and_level`): the per-output cloud address alone
        identifies the target output, with no separate output byte — 0x00C8 with the
        per-output address broke every output past the first on a multi-output
        device (#71).

        `notify=False` lets a caller that still has more state to record (e.g.
        async_set_group_output's member states) defer the listener notification
        until everything is consistent, instead of notifying mid-update.
        """
        # Captured before the write, not after: over the gateway transport, a normal
        # published ack is decoded into state_for() before _write_vector()'s own await
        # returns, so reading "prior" afterward would already see this command's own
        # echo (off, level 0) instead of the real previous level.
        prior = self.state_for(address)
        await self._write_vector(protocol.set_group_state_and_level(address, on, level))
        current = self.state_for(address)
        # What our OWN command's echo literally looks like on the wire - level=0 for an
        # off command regardless of the remembered brightness (the enriched value below
        # is our own bookkeeping, not something the protocol's echo itself carries).
        raw_echo = OutputState(output=address, on=on, level=level)
        if current is not None and current != prior and current != raw_echo:
            # Something else (a physical switch, the Plejd app, another output on this
            # device) changed this output to a value we didn't command while our own
            # write was in flight. Its own push already notified listeners with the
            # real state (_on_event) - don't stomp that with our stale optimistic guess.
            return
        # Reflect the change immediately rather than waiting for the mesh's own echo:
        # BLE writes are never acked, and even the gateway's ack isn't guaranteed to
        # land before this returns. Without this, a command sent right after another
        # (e.g. a fast on-then-off) reads stale state and computes the wrong direction.
        # Turning off doesn't erase the remembered brightness - a real device keeps
        # reporting its last dim position while off, so the off case preserves the
        # prior level instead of the protocol's own off payload (always 0).
        record_level = level if on else (prior.level if prior is not None else level)
        self._record_output_state(address, OutputState(output=address, on=on, level=record_level))
        if notify:
            self._notify_outputs()

    async def async_all_off(self) -> None:
        """Turn off every light output in the site (mirrors the Plejd app's "all off")."""
        seen: set[int] = set()
        attempted = 0
        succeeded = 0
        for device in self.devices:
            if device.category != CATEGORY_LIGHT or device.address is None or device.address in seen:
                continue
            seen.add(device.address)
            attempted += 1
            try:
                await self.async_set_output(device.address, False, 0)
            except Exception:  # noqa: BLE001 - one output failing must not abort turning off the rest
                _LOGGER.warning("Plejd all_off: failed to turn off output %s", device.address, exc_info=True)
                continue
            succeeded += 1
        if attempted and not succeeded:
            raise HomeAssistantError("Plejd all_off: failed to turn off any output")

    async def async_set_group_output(self, address: int, on: bool, level: int, member_addresses: list[int]) -> None:
        """Send a group command (a Plejd room), then reflect it in each member's own state.

        A group command's own ack/echo is keyed by the group address, not by any
        member's address, so member outputs would otherwise show stale state until
        each one separately reports its own change over the mesh/gateway.
        """
        await self.async_set_output(address, on, level, notify=False)
        self._record_group_member_states(on, level, member_addresses)
        self._notify_outputs()

    def _fan_group_state_to_members(self, command: Command) -> None:
        """Reflect an externally-initiated room broadcast (e.g. the Plejd app) in each member.

        Like our own group commands (see async_set_group_output), a group broadcast is
        keyed by the room's own group address, not by any member's address, so
        PlejdRoomLight (which reads member state) would otherwise stay stale until each
        member separately reports its own change.
        """
        room = next((r for r in self.rooms if r.address == command.address), None)
        if room is None:
            return
        state = protocol.decode_output_state(command)
        if state is None:
            return
        self._record_group_member_states(state.on, state.level, room.member_addresses)

    def _record_group_member_states(self, on: bool, level: int, member_addresses: list[int]) -> None:
        for member in member_addresses:
            if on:
                member_level = level
            else:
                # Turning off doesn't erase a member's remembered brightness (the level
                # param here is just the protocol's off payload) - a real device keeps
                # reporting its own last dim position while off, and the room's restore
                # should too.
                prior = self.state_for(member)
                member_level = prior.level if prior is not None else level
            self._record_output_state(member, OutputState(output=member, on=on, level=member_level))

    def _record_output_state(self, address: int, state: OutputState) -> None:
        if self._active == "gateway" and self._gateway is not None:
            self._gateway.set_state(address, state)
        elif self._connection.mesh is not None:
            self._connection.mesh.set_state(address, state)

    async def async_set_output_min_level(self, address: int, output: int, fraction: float) -> None:
        """Set an output's minimum dim level (0-1 fraction)."""
        await self._write_vector(protocol.set_output_min_level(address, output, fraction))
        self._cache_output_setting(address, min_level=fraction * 100)

    async def async_set_output_max_level(self, address: int, output: int, fraction: float) -> None:
        """Set an output's maximum dim level (0-1 fraction)."""
        await self._write_vector(protocol.set_output_max_level(address, output, fraction))
        self._cache_output_setting(address, max_level=fraction * 100)

    async def async_set_output_start_level(self, address: int, output: int, fraction: float) -> None:
        """Set an output's start level (0-1 fraction)."""
        await self._write_vector(protocol.set_output_start_level(address, output, fraction))

    async def async_set_output_speed(self, address: int, output: int, seconds: float) -> None:
        """Set an output's dim transition time (seconds; 0 = instant)."""
        await self._write_vector(protocol.set_output_speed(address, output, seconds))
        self._cache_output_setting(address, speed=seconds)

    async def async_set_output_curve(self, address: int, output: int, curve: int) -> None:
        """Set an output's dimming curve (LoadCurve byte)."""
        await self._write_vector(protocol.set_output_curve(address, output, curve))
        self._cache_output_setting(address, curve=curve)

    async def async_set_output_phase_dim(self, address: int, output: int, phase: int) -> None:
        """Set an output's phase-dim edge (PhaseOutputType byte)."""
        await self._write_vector(protocol.set_output_phase_dim(address, output, phase))
        self._cache_output_setting(address, phase_dim=phase)

    async def async_set_output_boot_state(self, address: int, output: int, use_last: bool) -> None:
        """Set an output's after-power-outage boot state (True=restore, False=off)."""
        await self._write_vector(protocol.set_output_boot_state(address, output, use_last))
        self._cache_output_setting(address, boot_state=use_last)

    async def async_set_output_relay_off_time(self, address: int, output: int, seconds: float) -> None:
        """Set minimum relay off time in seconds (relay devices only)."""
        await self._write_vector(protocol.set_output_relay_off_time(address, output, seconds))
        self._cache_output_setting(address, relay_off_time=seconds)

    async def async_set_output_relay_config(self, address: int, output: int, config: int) -> None:
        """Set relay pole configuration (0=TwoPole, 1=OnePole)."""
        await self._write_vector(protocol.set_output_relay_config(address, output, config))
        self._cache_output_setting(address, relay_pole_config=config)

    async def async_set_output_inrush_current(self, address: int, output: int, time_ms: int) -> None:
        """Set inrush current protection time in milliseconds (0=disabled)."""
        await self._write_vector(protocol.set_output_inrush_current(address, output, time_ms))
        self._cache_output_setting(address, inrush_current_ms=time_ms)

    async def async_execute_scene(self, index: int) -> None:
        """Trigger a Plejd scene (broadcast to address 0)."""
        await self._write_vector(protocol.execute_scene(0, index))

    async def async_leave_mesh_group(self, address: int, room_address: int) -> None:
        """Remove a device from a room's mesh group, as part of moving it to another room."""
        await self._write_vector(protocol.leave_mesh_group(address, room_address))

    async def async_join_mesh_group(self, address: int, room_address: int) -> None:
        """Add a device to a room's mesh group, as part of moving it to another room."""
        await self._write_vector(protocol.join_mesh_group(address, room_address))

    async def async_set_climate_setpoint(self, address: int, celsius: float) -> None:
        """Set a thermostat target temperature."""
        await self._write_vector(protocol.set_climate_setpoint(address, celsius))

    async def async_set_climate_mode(self, address: int, mode: int) -> None:
        """Set a thermostat operating mode."""
        await self._write_vector(protocol.set_climate_mode(address, mode))

    async def async_set_cover_position(self, address: int, position: int) -> None:
        """Move a cover to a position (0-100)."""
        await self._write_vector(protocol.set_cover_position(address, position))

    async def async_cover_stop(self, address: int) -> None:
        """Halt a cover."""
        await self._write_vector(protocol.cover_stop(address))

    async def async_shutdown(self) -> None:
        self._closed = True
        if self._cloud_poll_unsub is not None:
            self._cloud_poll_unsub()
            self._cloud_poll_unsub = None
        self.dim_ramp.shutdown()
        if self._clock_unsub is not None:
            self._clock_unsub()
            self._clock_unsub = None
        if self._faults_unsub is not None:
            self._faults_unsub()
            self._faults_unsub = None
        if self._firmware_unsub is not None:
            self._firmware_unsub()
            self._firmware_unsub = None
        if self._firmware_now_unsub is not None:
            self._firmware_now_unsub()
            self._firmware_now_unsub = None
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._gateway is not None:
            await self._gateway.disconnect()
        await self._connection.disconnect()
