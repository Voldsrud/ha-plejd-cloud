"""Tests for the Plejd mesh command codec."""

from __future__ import annotations

import pytest
from plejd_cloud.const import (
    CMD_GROUP_STATE_AND_LEVEL,
    CMD_MESH_GROUP_MEMBERSHIP,
    CMD_OUTPUT_STATE_AND_LEVEL,
    CMD_SCENE,
)
from plejd_cloud.protocol import (
    TYPE_DONT_RESPOND,
    TYPE_READ,
    TYPE_WRITE,
    Command,
    decode_command,
    decode_output_state,
    encode_command,
    execute_scene,
    join_mesh_group,
    leave_mesh_group,
    request_output_state_and_level,
    set_group_state_and_level,
    set_output_state_and_level,
)


def test_encode_command_layout():
    v = encode_command(0x05, CMD_OUTPUT_STATE_AND_LEVEL, bytes([0x00, 0x01]), command_type=TYPE_WRITE)
    # [address, 0x01, type, op_hi, op_lo, data...] with big-endian opcode 0x00C8
    assert v == bytes([0x05, 0x01, 0x00, 0x00, 0xC8, 0x00, 0x01])


def test_set_output_state_and_level_bytes():
    v = set_output_state_and_level(0x02, output=3, on=True, level=200)
    assert v == bytes([0x02, 0x01, 0x00, 0x00, 0xC8, 0x03, 0x01, 200, 200])


def test_set_output_off_zeroes_state_byte():
    v = set_output_state_and_level(0, output=1, on=False, level=0)
    assert v[5:] == bytes([0x01, 0x00, 0x00, 0x00])


def test_set_group_state_and_level_bytes():
    # confirmed on a live gateway capture: opcode 0x0098, no output byte, level mirrored twice.
    v = set_group_state_and_level(40, on=True, level=200)
    assert v == bytes([40, 0x01, TYPE_DONT_RESPOND, 0x00, 0x98, 0x01, 200, 200])


def test_set_group_state_and_level_defaults_to_dont_respond():
    v = set_group_state_and_level(40, on=True, level=200)
    assert v[2] == TYPE_DONT_RESPOND


def test_set_group_state_off_zeroes_state_byte():
    v = set_group_state_and_level(0, on=False, level=0)
    assert v[5:] == bytes([0x00, 0x00, 0x00])


def test_set_group_state_and_level_decodes_with_address_as_output():
    cmd = decode_command(set_group_state_and_level(40, on=True, level=77))
    state = decode_output_state(cmd)
    assert (state.output, state.on, state.level) == (40, True, 77)


def test_request_uses_read_type_and_single_output_byte():
    v = request_output_state_and_level(0x07, output=4)
    assert v[2] == TYPE_READ
    assert v[5:] == bytes([0x04])


def test_execute_scene_opcode():
    v = execute_scene(0x01, scene=9)
    assert (v[3] << 8) | v[4] == CMD_SCENE
    assert v[2] == TYPE_DONT_RESPOND  # app sends ExecuteScene with DontRespond
    assert v[5:] == bytes([9])


def test_leave_mesh_group_bytes():
    # Confirmed via a live BLE capture of the app's own "move device to room" action:
    # decrypting a captured write for a device leaving a room's mesh group yielded exactly
    # this vector shape. Address/group values below are illustrative, not the real capture's.
    v = leave_mesh_group(0x27, 0x0E)
    assert v == bytes([0x27, 0x01, TYPE_WRITE, 0x00, 0x08, 0x01, 0x0E])


def test_join_mesh_group_bytes():
    # Same capture, joining a different room's mesh group - identical shape to
    # leave_mesh_group plus a trailing join-flag byte.
    v = join_mesh_group(0x27, 0x22)
    assert v == bytes([0x27, 0x01, TYPE_WRITE, 0x00, 0x08, 0x01, 0x22, 0x01])


def test_mesh_group_commands_share_the_same_opcode():
    leave = decode_command(leave_mesh_group(0x27, 0x0E))
    join = decode_command(join_mesh_group(0x27, 0x22))
    assert leave.command == CMD_MESH_GROUP_MEMBERSHIP
    assert join.command == CMD_MESH_GROUP_MEMBERSHIP
    assert leave.data == bytes([0x01, 0x0E])
    assert join.data == bytes([0x01, 0x22, 0x01])


def test_decode_command_rejects_bad_marker():
    v = bytearray(set_output_state_and_level(0x09, output=2, on=True, level=1))
    v[1] = 0x02  # corrupt the marker (e.g. wrong-key decryption)
    with pytest.raises(ValueError, match="bad command marker"):
        decode_command(bytes(v))


def test_decode_command_round_trip():
    v = set_output_state_and_level(0x09, output=2, on=True, level=128)
    cmd = decode_command(v)
    assert cmd.address == 0x09
    assert cmd.command == CMD_OUTPUT_STATE_AND_LEVEL
    assert cmd.data == bytes([0x02, 0x01, 128, 128])


def test_decode_command_rejects_short_vector():
    with pytest.raises(ValueError, match="too short"):
        decode_command(b"\x00\x01\x00")


def test_decode_output_state_on_and_level():
    cmd = decode_command(set_output_state_and_level(0, output=5, on=True, level=77))
    st = decode_output_state(cmd)
    assert st is not None
    assert (st.output, st.on, st.level) == (5, True, 77)


def test_decode_output_state_off():
    cmd = decode_command(set_output_state_and_level(0, output=5, on=False, level=0))
    st = decode_output_state(cmd)
    assert st is not None and st.on is False


def test_decode_state_report_0x0098_keyed_by_address():
    # Real devices broadcast state on 0x0098: [state, level_lo, level_hi, ...]; the
    # vector address identifies the output, brightness = the level high byte.
    cmd = Command(
        address=11, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([0x01, 0xC8, 0xC8, 0x00])
    )
    st = decode_output_state(cmd)
    assert st == st.__class__(output=11, on=True, level=0xC8, raw_level=0xC8C8)


def test_decode_state_report_off():
    cmd = Command(
        address=11, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([0x00, 0x00, 0x00, 0x00])
    )
    assert decode_output_state(cmd).on is False


def test_decode_output_state_ignores_other_opcodes_and_short_data():
    assert decode_output_state(Command(0, 0, CMD_SCENE, b"\x01")) is None
    assert decode_output_state(Command(0, 0, CMD_OUTPUT_STATE_AND_LEVEL, b"")) is None
    assert decode_output_state(Command(11, 0, CMD_GROUP_STATE_AND_LEVEL, b"\x01")) is None


# ── relay pole config (0x022A) ────────────────────────────────────────────────


def test_set_output_relay_config_bytes():
    from plejd_cloud.const import CMD_OUTPUT_RELAY_CONFIG
    from plejd_cloud.protocol import set_output_relay_config

    v = set_output_relay_config(0x05, output=0, config=0)  # two_pole
    assert v[2] == TYPE_DONT_RESPOND
    assert (v[3] << 8) | v[4] == CMD_OUTPUT_RELAY_CONFIG
    assert v[5:] == bytes([0x00, 0x00])

    v2 = set_output_relay_config(0x05, output=1, config=1)  # one_pole
    assert v2[5:] == bytes([0x01, 0x01])


def test_request_output_relay_config_uses_read_type():
    from plejd_cloud.protocol import request_output_relay_config

    v = request_output_relay_config(0x05, output=0)
    assert v[2] == TYPE_READ
    assert v[5:] == bytes([0x00])


def test_decode_output_relay_config_reply():
    from plejd_cloud.protocol import decode_output_relay_config_reply

    assert decode_output_relay_config_reply(Command(5, 0, 0x022A, bytes([0]))) == 0
    assert decode_output_relay_config_reply(Command(5, 0, 0x022A, bytes([1]))) == 1
    assert decode_output_relay_config_reply(Command(5, 0, 0x022A, b"")) is None


# ── inrush current protection time (0x00A2) ───────────────────────────────────


def test_set_output_inrush_current_bytes():
    from plejd_cloud.const import CMD_OUTPUT_INRUSH_CURRENT
    from plejd_cloud.protocol import set_output_inrush_current

    # 500 ms → 50 centiseconds (0x32, 0x00)
    v = set_output_inrush_current(0x05, output=0, time_ms=500)
    assert v[2] == TYPE_DONT_RESPOND
    assert (v[3] << 8) | v[4] == CMD_OUTPUT_INRUSH_CURRENT
    assert v[5:] == bytes([0x00, 50, 0x00])

    # 0 ms → disabled (0x00, 0x00)
    v2 = set_output_inrush_current(0x05, output=0, time_ms=0)
    assert v2[5:] == bytes([0x00, 0x00, 0x00])

    # 2560 ms → 256 centiseconds (u16le: 0x00, 0x01)
    v3 = set_output_inrush_current(0x05, output=0, time_ms=2560)
    assert v3[5:] == bytes([0x00, 0x00, 0x01])


def test_request_output_inrush_current_uses_read_type():
    from plejd_cloud.protocol import request_output_inrush_current

    v = request_output_inrush_current(0x05, output=0)
    assert v[2] == TYPE_READ
    assert v[5:] == bytes([0x00])


def test_decode_output_inrush_current_reply():
    from plejd_cloud.protocol import decode_output_inrush_current_reply

    # 50 cs → 500 ms
    assert decode_output_inrush_current_reply(Command(5, 0, 0x00A2, bytes([50, 0]))) == 500
    # 0 → disabled (0 ms)
    assert decode_output_inrush_current_reply(Command(5, 0, 0x00A2, bytes([0, 0]))) == 0
    # too short
    assert decode_output_inrush_current_reply(Command(5, 0, 0x00A2, bytes([50]))) is None


def test_set_climate_setpoint_bytes():
    from plejd_cloud.const import CMD_TRM_SETPOINT
    from plejd_cloud.protocol import set_climate_setpoint

    v = set_climate_setpoint(9, 21.5)  # 215 = 0x00D7 little-endian
    assert (v[3] << 8) | v[4] == CMD_TRM_SETPOINT
    assert v[5:] == bytes([0xD7, 0x00])


def test_set_climate_mode_bytes():
    from plejd_cloud.const import CMD_TRM_MODE
    from plejd_cloud.protocol import set_climate_mode

    v = set_climate_mode(9, 3)
    assert (v[3] << 8) | v[4] == CMD_TRM_MODE and v[5:] == bytes([3])


def test_decode_temperature():
    from plejd_cloud.protocol import decode_temperature

    assert decode_temperature(bytes([0xD7, 0x00])) == 21.5
    assert decode_temperature(b"\x00") is None


def test_parse_mini_package_real_wms_capture():
    from plejd_cloud.protocol import parse_mini_package

    # captured live from a WMS-01 motion broadcast
    data = bytes.fromhex("0303 1f07 00b0 0f08 46 0602".replace(" ", ""))
    pkgs = parse_mini_package(data)
    assert pkgs[0] == (3, bytes([0x03]))  # Source = Motion
    assert (22, bytes([0x00, 0xB0])) in pkgs  # escaped type 15+7
    assert (6, bytes([0x02])) in pkgs  # Lux


def test_decode_motion_from_capture():
    from plejd_cloud.const import CMD_OUTPUT_SET
    from plejd_cloud.protocol import Command, decode_motion

    cmd = Command(address=33, command_type=0x10, command=CMD_OUTPUT_SET, data=bytes.fromhex("03031f0700b00f08460602"))
    m = decode_motion(cmd)
    assert m.address == 33 and m.motion is True and m.lux == 2


def test_decode_motion_ignores_other_opcodes():
    from plejd_cloud.const import CMD_SCENE
    from plejd_cloud.protocol import Command, decode_motion

    assert decode_motion(Command(0, 0, CMD_SCENE, b"\x01")) is None


def test_set_cover_position_bytes():
    from plejd_cloud.const import CMD_OUTPUT_SET
    from plejd_cloud.protocol import set_cover_position

    open_cmd = set_cover_position(5, 100)  # level 255 -> inverted 0
    assert (open_cmd[3] << 8) | open_cmd[4] == CMD_OUTPUT_SET
    assert open_cmd[5:] == bytes([0x03, 0x08, 0x27, 0x01, 0x00, 0x00])
    close_cmd = set_cover_position(5, 0)  # level 0 -> inverted 255
    assert close_cmd[5:] == bytes([0x03, 0x08, 0x27, 0x01, 0xFF, 0xFF])


def test_cover_stop_bytes():
    from plejd_cloud.protocol import cover_stop

    assert cover_stop(5)[5:] == bytes([0x03, 0x08, 0x07, 0x00])


def test_dim_level_setting_bytes():
    from plejd_cloud.const import CMD_OUTPUT_MAX_LEVEL, CMD_OUTPUT_MIN_LEVEL
    from plejd_cloud.protocol import TYPE_DONT_RESPOND, set_output_max_level, set_output_min_level

    minimum = set_output_min_level(9, 1, 0.5)  # 0.5 * 65535 = 32768 (0x8000) le
    assert (minimum[3] << 8) | minimum[4] == CMD_OUTPUT_MIN_LEVEL
    assert minimum[2] == TYPE_DONT_RESPOND
    assert minimum[5:] == bytes([0x01, 0x00, 0x80])
    full = set_output_max_level(9, 0, 1.0)  # 65535 -> 0xFFFF
    assert (full[3] << 8) | full[4] == CMD_OUTPUT_MAX_LEVEL
    assert full[5:] == bytes([0x00, 0xFF, 0xFF])
    zero = set_output_min_level(9, 0, 0.0)
    assert zero[5:] == bytes([0x00, 0x00, 0x00])


def test_set_output_start_level_bytes():
    from plejd_cloud.const import CMD_OUTPUT_START_LEVEL
    from plejd_cloud.protocol import set_output_start_level

    start = set_output_start_level(9, 1, 0.5)  # 0.5*65535 = 32768 = 0x8000 le
    assert (start[3] << 8) | start[4] == CMD_OUTPUT_START_LEVEL
    assert start[5:] == bytes([0x01, 0x00, 0x80])


def test_set_output_speed_bytes():
    from plejd_cloud.const import CMD_OUTPUT_SPEED
    from plejd_cloud.protocol import set_output_speed

    one_s = set_output_speed(9, 1, 1.0)  # steps round(65535/1/100)=655=0x028F; >0.5s sets bit7 of hi
    assert (one_s[3] << 8) | one_s[4] == CMD_OUTPUT_SPEED
    assert one_s[5:] == bytes([0x01, 0x8F, 0x82])
    half = set_output_speed(9, 0, 0.5)  # steps round(65535/0.5/100)=1311=0x051F; not >0.5 → no flag
    assert half[5:] == bytes([0x00, 0x1F, 0x05])
    instant = set_output_speed(9, 0, 0)  # 0s sentinel
    assert instant[5:] == bytes([0x00, 0xFF, 0xFF])


def test_weekday_mask():
    from plejd_cloud.protocol import weekday_mask

    assert weekday_mask([0, 6]) == 0x41  # Monday + Sunday
    assert weekday_mask([0, 1, 2, 3, 4, 5, 6]) == 0x7F
    assert weekday_mask([]) == 0


def test_time_event_bytes():
    from plejd_cloud.const import CMD_TIME_EVENT_SCENE, CMD_TIME_EVENT_TIME, CMD_TIME_EVENT_TYPE
    from plejd_cloud.protocol import (
        remove_time_event,
        set_time_event_scene,
        set_time_event_time,
        set_time_event_type,
    )

    t = set_time_event_time(3, 0x7F, 7, 30, 0, 0xFFFFFFFF)
    assert t[0] == 0x00 and (t[3] << 8) | t[4] == CMD_TIME_EVENT_TIME
    assert t[5:] == bytes([3, 1, 0x7F, 7, 30, 0, 0xFF, 0xFF, 0xFF, 0xFF])
    ty = set_time_event_type(3, 0)
    assert (ty[3] << 8) | ty[4] == CMD_TIME_EVENT_TYPE and ty[5:] == bytes([3, 0])
    s = set_time_event_scene(3, 5)
    assert (s[3] << 8) | s[4] == CMD_TIME_EVENT_SCENE and s[5:] == bytes([3, 1, 5])
    faded = set_time_event_scene(3, 5, 10)  # 65535//10 = 6553 = 0x1999 le
    assert faded[5:] == bytes([3, 1, 5, 0x99, 0x19])
    rm = remove_time_event(3)
    assert (rm[3] << 8) | rm[4] == CMD_TIME_EVENT_TIME and rm[5:] == bytes([3])


def test_set_timestamp_bytes():
    from plejd_cloud.const import CMD_SYSTEM_TIME
    from plejd_cloud.protocol import TYPE_DONT_RESPOND, set_timestamp

    cmd = set_timestamp(0x01020304)
    assert cmd[0] == 0x00  # broadcast address
    assert cmd[2] == TYPE_DONT_RESPOND
    assert (cmd[3] << 8) | cmd[4] == CMD_SYSTEM_TIME
    assert cmd[5:] == bytes([0x04, 0x03, 0x02, 0x01, 0x00])  # u32le epoch + trailing zero


def test_dimmer_tuning_setting_bytes():
    from plejd_cloud.const import CMD_OUTPUT_CURVE_TYPE, CMD_OUTPUT_PHASE_DIM_TYPE
    from plejd_cloud.protocol import set_output_curve, set_output_phase_dim

    curve = set_output_curve(9, 1, 3)  # antilogarithmic
    assert (curve[3] << 8) | curve[4] == CMD_OUTPUT_CURVE_TYPE
    assert curve[5:] == bytes([0x01, 0x03])
    phase = set_output_phase_dim(9, 0, 1)  # leading edge
    assert (phase[3] << 8) | phase[4] == CMD_OUTPUT_PHASE_DIM_TYPE
    assert phase[5:] == bytes([0x00, 0x01])


# ── Commissioning helpers ─────────────────────────────────────────────────────


def test_public_key_bytes_packs_little_endian():
    from plejd_cloud.protocol import public_key_bytes

    key = 0x0102030405060708
    b = public_key_bytes(key)
    assert len(b) == 8
    assert b == bytes([0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01])


def test_public_key_bytes_truncates_to_uint64():
    from plejd_cloud.protocol import public_key_bytes

    # High bits beyond 64 should be masked out.
    assert public_key_bytes((1 << 64) | 1) == public_key_bytes(1)


def test_access_address_bytes_parses_dash_hex():
    from plejd_cloud.protocol import access_address_bytes

    assert access_address_bytes("AA-BB-CC-DD") == bytes([0xAA, 0xBB, 0xCC, 0xDD])


def test_access_address_bytes_empty_string():
    from plejd_cloud.protocol import access_address_bytes

    assert access_address_bytes("") == b""


def test_node_index_bytes_single_byte():
    from plejd_cloud.protocol import node_index_bytes

    assert node_index_bytes(5) == bytes([5])
    assert node_index_bytes(255) == bytes([255])


def test_node_index_bytes_masks_to_one_byte():
    from plejd_cloud.protocol import node_index_bytes

    assert node_index_bytes(256) == bytes([0])
    assert node_index_bytes(257) == bytes([1])


def test_replace_last_mesh_command_uses_dont_respond_and_cmd_device_type():
    from plejd_cloud.const import CMD_DEVICE_TYPE
    from plejd_cloud.protocol import TYPE_DONT_RESPOND, replace_last_mesh_command

    v = replace_last_mesh_command(7)
    # Byte 0: address low byte
    assert v[0] == 7
    # Byte 2: command_type
    assert v[2] == TYPE_DONT_RESPOND
    # Bytes 3-4: command opcode big-endian
    assert (v[3] << 8) | v[4] == CMD_DEVICE_TYPE
    # No payload beyond the header (DontRespond, no data)
    assert len(v) == 5


def test_settings_read_requests_use_read_type():
    from plejd_cloud.const import (
        CMD_OUTPUT_CURVE_TYPE,
        CMD_OUTPUT_MAX_LEVEL,
        CMD_OUTPUT_MIN_LEVEL,
        CMD_OUTPUT_PHASE_DIM_TYPE,
        CMD_OUTPUT_SPEED,
    )
    from plejd_cloud.protocol import (
        request_output_curve,
        request_output_max_level,
        request_output_min_level,
        request_output_phase_dim,
        request_output_speed,
    )

    for req_fn, expected_cmd in [
        (request_output_min_level, CMD_OUTPUT_MIN_LEVEL),
        (request_output_max_level, CMD_OUTPUT_MAX_LEVEL),
        (request_output_speed, CMD_OUTPUT_SPEED),
        (request_output_curve, CMD_OUTPUT_CURVE_TYPE),
        (request_output_phase_dim, CMD_OUTPUT_PHASE_DIM_TYPE),
    ]:
        v = req_fn(0x05, 1)
        assert v[2] == TYPE_READ
        assert (v[3] << 8) | v[4] == expected_cmd
        assert v[5:] == bytes([0x01])  # output index


def test_decode_output_level_reply_round_trips():
    from plejd_cloud.protocol import Command, decode_output_level_reply

    # 50% = round(32767.5 / 65535 * 100) = 50.0
    half_u16 = 0x7FFF
    cmd = Command(address=5, command_type=0x02, command=0x00C9, data=bytes([half_u16 & 0xFF, half_u16 >> 8]))
    assert decode_output_level_reply(cmd) == 50.0

    # full range
    cmd_full = Command(address=5, command_type=0x02, command=0x00C9, data=bytes([0xFF, 0xFF]))
    assert decode_output_level_reply(cmd_full) == 100.0

    # too short
    cmd_short = Command(address=5, command_type=0x02, command=0x00C9, data=bytes([0xFF]))
    assert decode_output_level_reply(cmd_short) is None


def test_decode_output_speed_reply_round_trips():
    from plejd_cloud.protocol import Command, decode_output_speed_reply

    # instant sentinel: [0xFF, 0xFF] -> 0.0s
    instant = Command(address=5, command_type=0x02, command=0x00CB, data=bytes([0xFF, 0xFF]))
    assert decode_output_speed_reply(instant) == 0.0

    # 1-second fade: encode -> steps=655 -> [0x8F, 0x82] (bit7 of hi = >0.5s flag)
    one_sec = Command(address=5, command_type=0x02, command=0x00CB, data=bytes([0x8F, 0x82]))
    assert decode_output_speed_reply(one_sec) == pytest.approx(1.0, rel=0.02)

    # too short
    short = Command(address=5, command_type=0x02, command=0x00CB, data=bytes([0x01]))
    assert decode_output_speed_reply(short) is None


def test_decode_output_curve_and_phase_dim_replies():
    from plejd_cloud.protocol import Command, decode_output_curve_reply, decode_output_phase_dim_reply

    curve_cmd = Command(address=5, command_type=0x02, command=0x00CC, data=bytes([1]))  # logarithmic
    assert decode_output_curve_reply(curve_cmd) == 1

    phase_cmd = Command(address=5, command_type=0x02, command=0x00CE, data=bytes([0]))  # trailing_edge
    assert decode_output_phase_dim_reply(phase_cmd) == 0

    # empty data -> None
    assert decode_output_curve_reply(Command(5, 0x02, 0x00CC, b"")) is None
    assert decode_output_phase_dim_reply(Command(5, 0x02, 0x00CE, b"")) is None


def test_set_output_boot_state_bytes():
    from plejd_cloud.const import CMD_OUTPUT_BOOT_STATE
    from plejd_cloud.protocol import set_output_boot_state

    # use_last → 1-byte payload [output]
    v = set_output_boot_state(9, 0, True)
    assert (v[3] << 8) | v[4] == CMD_OUTPUT_BOOT_STATE
    assert v[5:] == bytes([0x00])  # just the output byte

    # off → 2-byte payload [output, 0x00]
    v2 = set_output_boot_state(9, 1, False)
    assert v2[5:] == bytes([0x01, 0x00])


def test_decode_output_boot_state_reply():
    from plejd_cloud.protocol import Command, decode_output_boot_state_reply

    # 1-byte reply → use_last=True
    cmd1 = Command(5, 0x02, 0x00D7, bytes([0x00]))
    assert decode_output_boot_state_reply(cmd1) is True

    # 2-byte reply with 0x00 → off=False
    cmd2 = Command(5, 0x02, 0x00D7, bytes([0x00, 0x00]))
    assert decode_output_boot_state_reply(cmd2) is False

    # 2-byte reply with non-zero second byte → unrecognised → None
    cmd3 = Command(5, 0x02, 0x00D7, bytes([0x00, 0x01]))
    assert decode_output_boot_state_reply(cmd3) is None

    # empty → None
    cmd4 = Command(5, 0x02, 0x00D7, b"")
    assert decode_output_boot_state_reply(cmd4) is None


def test_set_output_relay_off_time_bytes():
    from plejd_cloud.const import CMD_OUTPUT_RELAY_OFF_TIME
    from plejd_cloud.protocol import set_output_relay_off_time

    # 2 seconds = 200 centiseconds = 0x00C8 little-endian
    v = set_output_relay_off_time(9, 0, 2.0)
    assert (v[3] << 8) | v[4] == CMD_OUTPUT_RELAY_OFF_TIME
    assert v[5:] == bytes([0x00, 0xC8, 0x00])  # [output=0, lo=0xC8, hi=0x00]


def test_decode_output_relay_off_time_reply():
    from plejd_cloud.protocol import Command, decode_output_relay_off_time_reply

    # 200 centiseconds = 2.0 seconds
    cmd = Command(5, 0x02, 0x00D4, bytes([0xC8, 0x00]))
    assert decode_output_relay_off_time_reply(cmd) == 2.0

    # 150 centiseconds = 1.5 seconds
    cmd2 = Command(5, 0x02, 0x00D4, bytes([0x96, 0x00]))
    assert decode_output_relay_off_time_reply(cmd2) == 1.5

    # too short → None
    cmd3 = Command(5, 0x02, 0x00D4, bytes([0x01]))
    assert decode_output_relay_off_time_reply(cmd3) is None


def test_request_boot_state_and_relay_off_time_use_read_type():
    from plejd_cloud.const import CMD_OUTPUT_BOOT_STATE, CMD_OUTPUT_RELAY_OFF_TIME
    from plejd_cloud.protocol import TYPE_READ, request_output_boot_state, request_output_relay_off_time

    for req_fn, expected_cmd in [
        (request_output_boot_state, CMD_OUTPUT_BOOT_STATE),
        (request_output_relay_off_time, CMD_OUTPUT_RELAY_OFF_TIME),
    ]:
        v = req_fn(0x05, 1)
        assert v[2] == TYPE_READ
        assert (v[3] << 8) | v[4] == expected_cmd
        assert v[5:] == bytes([0x01])  # output index


def test_notify_events_request_and_decode():
    from plejd_cloud.const import CMD_NOTIFY_EVENTS, CMD_SCENE
    from plejd_cloud.protocol import TYPE_READ, Command, decode_notify_events, request_notify_events

    req = request_notify_events(11)
    assert req[2] == TYPE_READ and (req[3] << 8) | req[4] == CMD_NOTIFY_EVENTS and req[5:] == b""
    # healthy device: all-zero bitfield -> no faults
    assert decode_notify_events(Command(11, 0x03, CMD_NOTIFY_EVENTS, bytes(8))) == frozenset()
    # 0x8 = overtemperature, 0x1 = hard_fault (little-endian uint64)
    faults = decode_notify_events(Command(11, 0x03, CMD_NOTIFY_EVENTS, (0x9).to_bytes(8, "little")))
    assert faults == frozenset({"hard_fault", "overtemperature"})
    # non-NotifyEvents command -> None
    assert decode_notify_events(Command(0, 0, CMD_SCENE, b"")) is None
    # a short/truncated payload (e.g. our own 0-byte read request echoed back by the
    # gateway) must be rejected, not treated as an all-clear empty bitfield.
    assert decode_notify_events(Command(11, 0x03, CMD_NOTIFY_EVENTS, b"")) is None
    assert decode_notify_events(Command(11, 0x03, CMD_NOTIFY_EVENTS, bytes(7))) is None


def test_decode_state_report_keeps_the_full_level_for_thermostats():
    cmd = Command(address=9, command_type=0x10, command=CMD_GROUP_STATE_AND_LEVEL, data=bytes([0x01, 0xC8, 0x21]))
    assert decode_output_state(cmd).raw_level == 0x21C8


def test_decode_read_reply_keeps_the_full_level():
    cmd = Command(address=9, command_type=0x03, command=CMD_OUTPUT_STATE_AND_LEVEL, data=bytes([0x00, 0xC8, 0x21]))
    assert decode_output_state(cmd).raw_level == 0x21C8


def test_decode_thermostat_state_unpacks_mode_target_and_current():
    from plejd_cloud.protocol import decode_thermostat_state

    state = decode_thermostat_state(True, 0x21C8)
    assert (state.mode, state.target, state.current, state.heating) == (7, 22, 23, None)


def test_decode_thermostat_state_reports_heating_from_the_extra_byte():
    from plejd_cloud.protocol import decode_thermostat_state

    assert decode_thermostat_state(True, 0x21C8, 0x80).heating is True
    assert decode_thermostat_state(True, 0x21C8, 0x00).heating is False


def test_decode_thermostat_state_hides_the_mode_on_error():
    from plejd_cloud.protocol import decode_thermostat_state

    # word 0x1E821 = 0x1C821 with the error bit (0x2000) set
    state = decode_thermostat_state(True, 0x21E8)
    assert state.mode is None and state.target == 22 and state.current == 23
