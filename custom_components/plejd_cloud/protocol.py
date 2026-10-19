"""Plejd mesh command codec.

Reverse-engineered from the Plejd app (`BleCommands` / `CommandType`). A command
written to the Datavector characteristic (before XOR-encryption) is::

    [address] [0x01] [command_type] [opcode_hi] [opcode_lo] [payload...]

`address` is the recipient's mesh node index (0x00 = broadcast/own); the opcode is
2-byte **big-endian**. The same framing comes back on LastChangedDatavector for
state changes. See docs/reverse_engineering.md.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .const import (
    CMD_DEVICE_TYPE,
    CMD_GROUP_STATE_AND_LEVEL,
    CMD_MESH_GROUP_MEMBERSHIP,
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
    CMD_OUTPUT_START_LEVEL,
    CMD_OUTPUT_STATE_AND_LEVEL,
    CMD_SCENE,
    CMD_SYSTEM_TIME,
    CMD_TIME_EVENT_SCENE,
    CMD_TIME_EVENT_TIME,
    CMD_TIME_EVENT_TYPE,
    CMD_TRM_MODE,
    CMD_TRM_SETPOINT,
    NOTIFY_EVENT_FLAGS,
    SOURCE_APP,
    SOURCE_MOTION,
    SUBPKG_LUX,
    SUBPKG_SOURCE,
    SUBPKG_WINDOW,
    WINDOW_LEVEL,
    WINDOW_STOP,
)

# CommandType byte (from the app's CommandType enum).
TYPE_WRITE = 0x00
TYPE_ACK = 0x01
TYPE_READ = 0x02
TYPE_DONT_RESPOND = 0x10

_MARKER = 0x01  # constant second byte of every command vector


def encode_command(address: int, command: int, data: bytes = b"", command_type: int = TYPE_WRITE) -> bytes:
    """Build a Datavector command vector (plaintext, pre-encryption)."""
    return bytes([address & 0xFF, _MARKER, command_type & 0xFF, (command >> 8) & 0xFF, command & 0xFF]) + data


@dataclass(frozen=True)
class Command:
    """A decoded Datavector vector."""

    address: int
    command_type: int
    command: int
    data: bytes


def decode_command(vector: bytes) -> Command:
    """Parse a decrypted Datavector vector into its fields.

    Rejects vectors whose marker byte isn't 0x01: the payload cipher has no MAC, so
    data decrypted with the wrong address/key (or corrupted) would otherwise be
    mis-parsed as a real command and drive bogus state.
    """
    if len(vector) < 5:
        raise ValueError(f"command vector too short: {len(vector)} bytes")
    if vector[1] != _MARKER:
        raise ValueError(f"bad command marker {vector[1]:#04x} (expected {_MARKER:#04x})")
    address = vector[0]
    command_type = vector[2]
    command = (vector[3] << 8) | vector[4]
    return Command(address=address, command_type=command_type, command=command, data=vector[5:])


def set_output_state_and_level(
    address: int, output: int, on: bool, level: int, command_type: int = TYPE_WRITE
) -> bytes:
    """Set an output on/off and dim level (0x00C8). `level` is the 8-bit dim value.

    Defaults to the app's Write `StateAndLevelCommand`; callers that don't need an
    ack (live control relies on the LastChangedDatavector broadcast) pass
    `TYPE_DONT_RESPOND`.
    """
    lvl = level & 0xFF
    payload = bytes([output & 0xFF, 1 if on else 0, lvl, lvl])
    return encode_command(address, CMD_OUTPUT_STATE_AND_LEVEL, payload, command_type=command_type)


def set_group_state_and_level(address: int, on: bool, level: int, command_type: int = TYPE_DONT_RESPOND) -> bytes:
    """Set an output on/off and dim level (0x0098). `level` is the 8-bit dim value.

    This is the command the app actually sends for live control (confirmed on a live
    gateway capture): the per-output cloud address alone identifies the target — unlike
    0x00C8, there's no separate output byte, matching this opcode's own decode side
    (`decode_output_state`, which reads `output=cmd.address`). Using 0x00C8 with the
    per-output address instead of a device-level address broke every output past the
    first on a multi-output device (#71).
    """
    lvl = level & 0xFF
    payload = bytes([1 if on else 0, lvl, lvl])
    return encode_command(address, CMD_GROUP_STATE_AND_LEVEL, payload, command_type=command_type)


def request_output_state_and_level(address: int, output: int) -> bytes:
    """Read an output's current state and level (0x00C8, Read)."""
    return encode_command(address, CMD_OUTPUT_STATE_AND_LEVEL, bytes([output & 0xFF]), command_type=TYPE_READ)


def request_notify_events(address: int) -> bytes:
    """Read a device's NotifyEvents fault bitfield (0x002B, Read). Reply lands on LastChanged."""
    return encode_command(address, CMD_NOTIFY_EVENTS, b"", command_type=TYPE_READ)


def decode_notify_events(cmd: Command) -> frozenset[str] | None:
    """Decode a NotifyEvents report (0x002B) into the set of active fault-flag names.

    Data is a little-endian uint64 bitfield (validated as 0 against healthy hardware);
    returns None for non-0x002B commands or a payload shorter than 8 bytes (e.g. an
    echoed 0-byte read request, or a truncated/malformed notification) - treating
    that as an empty bitfield would incorrectly clear an active fault.
    """
    if cmd.command != CMD_NOTIFY_EVENTS or len(cmd.data) < 8:
        return None
    bits = int.from_bytes(cmd.data[:8], "little")
    return frozenset(name for mask, name in NOTIFY_EVENT_FLAGS.items() if bits & mask)


def _level_fraction_to_u16le(fraction: float) -> bytes:
    """Encode a 0-1 dim-level fraction as the app's u16 little-endian (round(f*65535))."""
    val = max(0, min(0xFFFF, round(fraction * 0xFFFF)))
    return bytes([val & 0xFF, (val >> 8) & 0xFF])


def set_output_min_level(address: int, output: int, fraction: float) -> bytes:
    """Set an output's minimum dim level (0x00C9): [output, u16le of fraction*65535]."""
    payload = bytes([output & 0xFF]) + _level_fraction_to_u16le(fraction)
    return encode_command(address, CMD_OUTPUT_MIN_LEVEL, payload, command_type=TYPE_DONT_RESPOND)


def set_output_max_level(address: int, output: int, fraction: float) -> bytes:
    """Set an output's maximum dim level (0x00CA): [output, u16le of fraction*65535]."""
    payload = bytes([output & 0xFF]) + _level_fraction_to_u16le(fraction)
    return encode_command(address, CMD_OUTPUT_MAX_LEVEL, payload, command_type=TYPE_DONT_RESPOND)


def set_output_start_level(address: int, output: int, fraction: float) -> bytes:
    """Set an output's start level (0x00CF): [output, u16le of fraction*65535]."""
    payload = bytes([output & 0xFF]) + _level_fraction_to_u16le(fraction)
    return encode_command(address, CMD_OUTPUT_START_LEVEL, payload, command_type=TYPE_DONT_RESPOND)


def set_output_speed(address: int, output: int, seconds: float) -> bytes:
    """Set an output's dim transition time (0x00CB): [output, u16le steps] (app's ConvertSpeedToPayload).

    steps = round(65535/seconds/100); 0s is the instant sentinel 0xFFFF; bit 7 of the
    high byte flags times over 0.5s. Byte-exact from the app.
    """
    if seconds <= 0:
        lo, hi = 0xFF, 0xFF
    else:
        steps = max(0, min(0xFFFF, round(0xFFFF / seconds / 100)))
        lo, hi = steps & 0xFF, (steps >> 8) & 0xFF
        if seconds > 0.5:
            hi = (hi & 0x7F) | 0x80  # bit 7 is the >0.5s flag, not part of the step count
    return encode_command(address, CMD_OUTPUT_SPEED, bytes([output & 0xFF, lo, hi]), command_type=TYPE_DONT_RESPOND)


def weekday_mask(days: list[int]) -> int:
    """Pack weekday indices (Monday=0..Sunday=6) into the app's 1<<day bitmask."""
    mask = 0
    for day in days:
        mask |= 1 << (day & 7)
    return mask


def _fade_steps(seconds: int) -> bytes:
    """Encode a fade time as the app's u16le 65535//seconds."""
    steps = 0xFFFF // seconds
    return bytes([steps & 0xFF, (steps >> 8) & 0xFF])


def set_time_event_time(slot: int, mask: int, hour: int, minute: int, second: int, repetition: int) -> bytes:
    """Set a time event's weekly schedule (0x0258): [slot,1,mask,h,m,s,u32le rep]."""
    rep = repetition & 0xFFFFFFFF
    payload = bytes([slot & 0xFF, 1, mask & 0xFF, hour & 0xFF, minute & 0xFF, second & 0xFF])
    payload += bytes([rep & 0xFF, (rep >> 8) & 0xFF, (rep >> 16) & 0xFF, (rep >> 24) & 0xFF])
    return encode_command(0, CMD_TIME_EVENT_TIME, payload, command_type=TYPE_DONT_RESPOND)


def set_time_event_type(slot: int, result: int) -> bytes:
    """Set what a time event does (0x0259): [slot, TimeEventResult]."""
    return encode_command(0, CMD_TIME_EVENT_TYPE, bytes([slot & 0xFF, result & 0xFF]), command_type=TYPE_DONT_RESPOND)


def set_time_event_scene(slot: int, scene: int, fade: int = 0) -> bytes:
    """Set the scene a time event runs (0x025A): [slot,1,scene](+u16le fade if >0)."""
    payload = bytes([slot & 0xFF, 1, scene & 0xFF])
    if fade > 0:
        payload += _fade_steps(fade)
    return encode_command(0, CMD_TIME_EVENT_SCENE, payload, command_type=TYPE_DONT_RESPOND)


def remove_time_event(slot: int) -> bytes:
    """Delete a time event (0x0258): [slot]."""
    return encode_command(0, CMD_TIME_EVENT_TIME, bytes([slot & 0xFF]), command_type=TYPE_DONT_RESPOND)


def set_timestamp(epoch: int) -> bytes:
    """Broadcast a clock sync (0x001B): [u32le local-time epoch][0x00].

    The app sends local wall-clock seconds since 1970 (not UTC) so the device RTC
    reads wall-clock; broadcast to address 0, fire-and-forget.
    """
    ts = epoch & 0xFFFFFFFF
    payload = bytes([ts & 0xFF, (ts >> 8) & 0xFF, (ts >> 16) & 0xFF, (ts >> 24) & 0xFF, 0x00])
    return encode_command(0, CMD_SYSTEM_TIME, payload, command_type=TYPE_DONT_RESPOND)


def set_output_curve(address: int, output: int, curve: int) -> bytes:
    """Set an output's dimming curve (0x00CC): [output, LoadCurve byte]."""
    payload = bytes([output & 0xFF, curve & 0xFF])
    return encode_command(address, CMD_OUTPUT_CURVE_TYPE, payload, command_type=TYPE_DONT_RESPOND)


def set_output_phase_dim(address: int, output: int, phase: int) -> bytes:
    """Set an output's phase-dim edge (0x00CE): [output, PhaseOutputType byte]."""
    payload = bytes([output & 0xFF, phase & 0xFF])
    return encode_command(address, CMD_OUTPUT_PHASE_DIM_TYPE, payload, command_type=TYPE_DONT_RESPOND)


def execute_scene(address: int, scene: int) -> bytes:
    """Trigger a scene by index (0x0021).

    The app's ExecuteScene sends only the 1-byte index with the DontRespond type;
    the slewrate/level fields in the schema belong to scene *configuration* (0x0022).
    """
    return encode_command(address, CMD_SCENE, bytes([scene & 0xFF]), command_type=TYPE_DONT_RESPOND)


def leave_mesh_group(address: int, room_address: int) -> bytes:
    """Remove a device from a room's mesh group (0x0008): stops it responding to that
    room's group broadcasts. Confirmed via a live capture of the app's own "move device
    to room" action - `address` is the device's own mesh address (not its output
    address), matched against the room's group address via a live cloud fetch."""
    return encode_command(address, CMD_MESH_GROUP_MEMBERSHIP, bytes([0x01, room_address & 0xFF]))


def join_mesh_group(address: int, room_address: int) -> bytes:
    """Add a device to a room's mesh group (0x0008): same payload as leave_mesh_group
    with a trailing join-flag byte, confirmed on the same live capture."""
    return encode_command(address, CMD_MESH_GROUP_MEMBERSHIP, bytes([0x01, room_address & 0xFF, 0x01]))


def _mini_header(flag: int, size: int) -> int:
    """Single-byte mini-package header: low nibble = flag, bits 4-6 = size-1."""
    return (flag & 0x0F) | (((size - 1) & 7) << 4)


def _source_app() -> bytes:
    return bytes([_mini_header(SUBPKG_SOURCE, 1), SOURCE_APP])


def set_cover_position(address: int, position: int) -> bytes:
    """Set a cover position 0-100 (0x0420 WindowControl mini-package).

    Plejd stores an 8-bit level inverted (255-level); HA 0 (closed) -> level 0.
    Decoded from the app, not hardware-validated (no cover on the test mesh).
    """
    level = max(0, min(255, round(position / 100 * 255)))
    inverted = 255 - level
    payload = _source_app() + bytes([_mini_header(SUBPKG_WINDOW, 3), WINDOW_LEVEL, inverted, inverted])
    return encode_command(address, CMD_OUTPUT_SET, payload, command_type=TYPE_DONT_RESPOND)


def cover_stop(address: int) -> bytes:
    """Halt a cover (0x0420 WindowControl Stop)."""
    payload = _source_app() + bytes([_mini_header(SUBPKG_WINDOW, 1), WINDOW_STOP])
    return encode_command(address, CMD_OUTPUT_SET, payload, command_type=TYPE_DONT_RESPOND)


def set_climate_setpoint(address: int, celsius: float) -> bytes:
    """Set a thermostat target temperature (0x045C): u16 little-endian of round(C*10)."""
    val = max(0, min(0xFFFF, round(celsius * 10)))
    return encode_command(
        address, CMD_TRM_SETPOINT, bytes([val & 0xFF, (val >> 8) & 0xFF]), command_type=TYPE_DONT_RESPOND
    )


def set_climate_mode(address: int, mode: int) -> bytes:
    """Set a thermostat operating mode (0x045F, OperatingMode enum)."""
    return encode_command(address, CMD_TRM_MODE, bytes([mode & 0xFF]), command_type=TYPE_DONT_RESPOND)


def decode_temperature(data: bytes) -> float | None:
    """Decode a temperature value (u16 little-endian of C*10), e.g. a 0x045B reply."""
    if len(data) >= 2:
        return ((data[1] << 8) | data[0]) / 10
    return None


def parse_mini_package(data: bytes) -> list[tuple[int, bytes]]:
    """Split an output_set (0x0420) mini-package into (type, payload) sub-packages.

    Header (from the app's MiniPkgHeader): low nibble = type (15 = escape, then the
    next byte + 15 is the real type); bits 4-6 = payload_size - 1; bit 7 = a flag.
    Validated against a real WMS-01 motion broadcast.
    """
    packages: list[tuple[int, bytes]] = []
    i = 0
    n = len(data)
    while i < n:
        b0 = data[i]
        ptype = b0 & 0x0F
        header = 1
        if ptype == 15 and i + 1 < n:
            ptype = data[i + 1] + 15
            header = 2
        size = ((b0 >> 4) & 7) + 1
        start = i + header
        packages.append((ptype, data[start : start + size]))
        i = start + size
    return packages


@dataclass(frozen=True)
class MotionEvent:
    """A WMS motion broadcast: motion detected + (best-effort) ambient light."""

    address: int
    motion: bool
    lux: int | None


def decode_motion(cmd: Command) -> MotionEvent | None:
    """Decode a WMS output_set (0x0420) broadcast into motion + ambient light.

    Returns None for non-0x0420 commands. ``motion`` is true when a Source
    sub-package reports Motion; ``lux`` is the Lux sub-package value if present.
    """
    if cmd.command != CMD_OUTPUT_SET:
        return None
    motion = False
    lux: int | None = None
    for ptype, payload in parse_mini_package(cmd.data):
        if ptype == SUBPKG_SOURCE and payload:
            motion = payload[0] == SOURCE_MOTION
        elif ptype == SUBPKG_LUX and payload:
            lux = payload[0]
    return MotionEvent(address=cmd.address, motion=motion, lux=lux)


@dataclass(frozen=True)
class OutputState:
    """Decoded on/off + level for one output."""

    output: int
    on: bool
    level: int


@dataclass
class OutputSettings:
    """Per-output dimmer settings from BLE read-back or cloud pre-load."""

    min_level: float | None = None  # 0-100 percent
    max_level: float | None = None  # 0-100 percent
    speed: float | None = None  # seconds (0 = instant)
    curve: int | None = None  # LoadCurve byte
    phase_dim: int | None = None  # PhaseOutputType byte
    boot_state: bool | None = None  # True=use_last, False=off (0x00D7)
    relay_off_time: float | None = None  # seconds, relay devices only (0x00D4)
    relay_pole_config: int | None = None  # RelayConfig wire byte (0=TwoPole, 1=OnePole); 0x022A
    inrush_current_ms: int | None = None  # milliseconds (0=disabled, wire=centiseconds); 0x00A2


def request_output_min_level(address: int, output: int) -> bytes:
    """Read an output's minimum dim level (0x00C9, Read)."""
    return encode_command(address, CMD_OUTPUT_MIN_LEVEL, bytes([output & 0xFF]), command_type=TYPE_READ)


def request_output_max_level(address: int, output: int) -> bytes:
    """Read an output's maximum dim level (0x00CA, Read)."""
    return encode_command(address, CMD_OUTPUT_MAX_LEVEL, bytes([output & 0xFF]), command_type=TYPE_READ)


def request_output_speed(address: int, output: int) -> bytes:
    """Read an output's dim transition time (0x00CB, Read)."""
    return encode_command(address, CMD_OUTPUT_SPEED, bytes([output & 0xFF]), command_type=TYPE_READ)


def request_output_curve(address: int, output: int) -> bytes:
    """Read an output's dimming curve (0x00CC, Read)."""
    return encode_command(address, CMD_OUTPUT_CURVE_TYPE, bytes([output & 0xFF]), command_type=TYPE_READ)


def request_output_phase_dim(address: int, output: int) -> bytes:
    """Read an output's phase-dim edge (0x00CE, Read)."""
    return encode_command(address, CMD_OUTPUT_PHASE_DIM_TYPE, bytes([output & 0xFF]), command_type=TYPE_READ)


def decode_output_level_reply(cmd: Command) -> float | None:
    """Decode a min/max level read reply: [lo, hi] → 0-100 percent."""
    if len(cmd.data) < 2:
        return None
    return round((cmd.data[0] | (cmd.data[1] << 8)) / 0xFFFF * 100, 1)


def decode_output_speed_reply(cmd: Command) -> float | None:
    """Decode a speed read reply: [lo, hi] → seconds (inverse of set_output_speed)."""
    if len(cmd.data) < 2:
        return None
    lo, raw_hi = cmd.data[0], cmd.data[1]
    if lo == 0xFF and raw_hi == 0xFF:  # instant sentinel (see set_output_speed)
        return 0.0
    steps = lo | ((raw_hi & 0x7F) << 8)  # strip the >0.5s flag from the high byte
    return round(0xFFFF / steps / 100, 2) if steps else 0.0


def decode_output_curve_reply(cmd: Command) -> int | None:
    """Decode a curve type read reply: [curve_byte] → LoadCurve int."""
    return int(cmd.data[0]) if cmd.data else None


def decode_output_phase_dim_reply(cmd: Command) -> int | None:
    """Decode a phase-dim edge read reply: [phase_byte] → PhaseOutputType int."""
    return int(cmd.data[0]) if cmd.data else None


def set_output_boot_state(address: int, output: int, use_last: bool) -> bytes:
    """Set after-power-outage boot state (0x00D7).

    use_last=True → [output] (restore last state); False → [output, 0x00] (boot off).
    """
    payload = bytes([output & 0xFF]) + (b"" if use_last else b"\x00")
    return encode_command(address, CMD_OUTPUT_BOOT_STATE, payload, command_type=TYPE_DONT_RESPOND)


def request_output_boot_state(address: int, output: int) -> bytes:
    """Read an output's boot state (0x00D7, Read)."""
    return encode_command(address, CMD_OUTPUT_BOOT_STATE, bytes([output & 0xFF]), command_type=TYPE_READ)


def decode_output_boot_state_reply(cmd: Command) -> bool | None:
    """Decode a boot-state reply → True=use_last, False=off, None=unrecognised format."""
    if not cmd.data:
        return None
    if len(cmd.data) == 1:
        return True
    if cmd.data[1] == 0:
        return False
    return None


def set_output_relay_off_time(address: int, output: int, seconds: float) -> bytes:
    """Set minimum relay off time (0x00D4): [output, u16le centiseconds]."""
    cs = max(0, min(0xFFFF, round(seconds * 100)))
    return encode_command(
        address,
        CMD_OUTPUT_RELAY_OFF_TIME,
        bytes([output & 0xFF, cs & 0xFF, (cs >> 8) & 0xFF]),
        command_type=TYPE_DONT_RESPOND,
    )


def request_output_relay_off_time(address: int, output: int) -> bytes:
    """Read an output's minimum relay off time (0x00D4, Read)."""
    return encode_command(address, CMD_OUTPUT_RELAY_OFF_TIME, bytes([output & 0xFF]), command_type=TYPE_READ)


def decode_output_relay_off_time_reply(cmd: Command) -> float | None:
    """Decode a relay off-time reply: [u16le centiseconds] → seconds."""
    if len(cmd.data) < 2:
        return None
    return round((cmd.data[0] | (cmd.data[1] << 8)) / 100, 2)


def set_output_relay_config(address: int, output: int, config: int) -> bytes:
    """Set relay pole configuration (0x022A): [output, RelayConfig wire byte].

    Wire byte 0=TwoPole, 1=OnePole (from app GetBytes; NOT the raw enum index).
    """
    payload = bytes([output & 0xFF, config & 0xFF])
    return encode_command(address, CMD_OUTPUT_RELAY_CONFIG, payload, command_type=TYPE_DONT_RESPOND)


def request_output_relay_config(address: int, output: int) -> bytes:
    """Read an output's relay pole configuration (0x022A, Read)."""
    return encode_command(address, CMD_OUTPUT_RELAY_CONFIG, bytes([output & 0xFF]), command_type=TYPE_READ)


def decode_output_relay_config_reply(cmd: Command) -> int | None:
    """Decode a relay config reply: [config_byte] → RelayConfig wire int (0=TwoPole, 1=OnePole)."""
    return int(cmd.data[0]) if cmd.data else None


def set_output_inrush_current(address: int, output: int, time_ms: int) -> bytes:
    """Set inrush current protection time (0x00A2): [output, u16le centiseconds].

    time_ms=0 disables protection; the wire unit is centiseconds (ms // 10).
    """
    cs = max(0, min(0xFFFF, time_ms // 10))
    return encode_command(
        address,
        CMD_OUTPUT_INRUSH_CURRENT,
        bytes([output & 0xFF, cs & 0xFF, (cs >> 8) & 0xFF]),
        command_type=TYPE_DONT_RESPOND,
    )


def request_output_inrush_current(address: int, output: int) -> bytes:
    """Read an output's inrush current protection time (0x00A2, Read)."""
    return encode_command(address, CMD_OUTPUT_INRUSH_CURRENT, bytes([output & 0xFF]), command_type=TYPE_READ)


def decode_output_inrush_current_reply(cmd: Command) -> int | None:
    """Decode an inrush current reply: [u16le centiseconds] → milliseconds."""
    if len(cmd.data) < 2:
        return None
    return (cmd.data[0] | (cmd.data[1] << 8)) * 10


def decode_output_state(cmd: Command) -> OutputState | None:
    """Decode an on/off + level state report (validated against real devices).

    Devices broadcast state changes on **0x0098** keyed by the output's mesh address:
    ``[state, level_lo, level_hi, ...]`` — on = state, brightness (0-255) = the level
    high byte (level is 16-bit on the wire). ``output`` is the broadcasting address.

    A 0x00C8 read-reply (``[output, level_lo, level_hi]``) is also accepted.
    """
    if cmd.command == CMD_GROUP_STATE_AND_LEVEL and len(cmd.data) >= 3:
        return OutputState(output=cmd.address, on=cmd.data[0] != 0, level=cmd.data[2])
    if cmd.command == CMD_OUTPUT_STATE_AND_LEVEL and len(cmd.data) >= 3:
        level = cmd.data[2]
        return OutputState(output=cmd.data[0], on=level > 0 or cmd.data[1] != 0, level=level)
    return None


# ── Commissioning GATT helpers ──────────────────────────────────────────────
# These produce raw bytes for direct GATT characteristic writes (not mesh commands).
# They are used during device provisioning, which writes to individual GATT chars
# on the new device (not to the DataVector characteristic).


def public_key_bytes(public_key: int) -> bytes:
    """Encode a DH public key (uint64) as 8 bytes little-endian for CryptoKeyID write."""

    return struct.pack("<Q", public_key & 0xFFFFFFFFFFFFFFFF)


def access_address_bytes(mesh_key: str) -> bytes:
    """Convert a mesh key (dash-separated hex "AA-BB-CC-DD") to raw bytes for AccessAddressID write."""
    return bytes(int(h, 16) for h in mesh_key.split("-") if h)


def node_index_bytes(index: int) -> bytes:
    """Build the NodeIndex payload for SetNodeIndex: a single byte for the node address."""
    return bytes([index & 0xFF])


def replace_last_mesh_command(node_index: int) -> bytes:
    """Build the GetDeviceTypeNumber (cmd 0x0000 DontRespond) Datavector vector.

    ReplaceLastMeshCommand sends this command twice (100 ms apart) to the mesh to
    replace whatever the last pending command was for the newly added device's address.
    """
    return encode_command(node_index, CMD_DEVICE_TYPE, b"", command_type=TYPE_DONT_RESPOND)
