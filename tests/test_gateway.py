"""Tests for the Plejd gateway (remote/cloud) transport codec."""

from __future__ import annotations

import base64
import json

import pytest
from plejd_cloud import gateway
from plejd_cloud.const import CMD_OUTPUT_STATE_AND_LEVEL
from plejd_cloud.protocol import decode_command, encode_command, set_output_state_and_level


def test_repackage_command_to_ws_layout():
    vector = set_output_state_and_level(address=11, output=0, on=True, level=80)
    packet = gateway.repackage_command_to_ws(vector)
    assert len(packet) == 23
    assert packet[0] == vector[2]  # command type
    assert packet[1] == (CMD_OUTPUT_STATE_AND_LEVEL & 0xFF)  # opcode low
    assert packet[2] == (CMD_OUTPUT_STATE_AND_LEVEL >> 8)  # opcode high
    assert packet[3] == len(vector) - 5  # data length
    assert packet[4 : 4 + packet[3]] == vector[5:]


def test_repackage_round_trip_preserves_vector():
    vector = set_output_state_and_level(address=24, output=1, on=False, level=0)
    packet = gateway.repackage_command_to_ws(vector)
    assert gateway.repackage_ws_to_command(packet, address=24) == vector


def test_repackage_command_to_ws_rejects_short_vector():
    with pytest.raises(ValueError, match="too short"):
        gateway.repackage_command_to_ws(b"\x01\x01\x00")


def test_repackage_command_to_ws_rejects_oversized_data():
    vector = encode_command(5, 0x00C8, data=bytes(20))  # 20 > 19 data bytes
    with pytest.raises(ValueError, match="too long"):
        gateway.repackage_command_to_ws(vector)


def test_repackage_ws_to_command_rejects_wrong_length():
    with pytest.raises(ValueError, match="must be 23 bytes"):
        gateway.repackage_ws_to_command(b"\x00" * 10, address=5)


def test_repackage_ws_to_command_rejects_bad_data_length():
    packet = bytearray(23)
    packet[3] = 200  # claims more data than the packet can hold
    with pytest.raises(ValueError, match="data length out of range"):
        gateway.repackage_ws_to_command(bytes(packet), address=5)


def test_build_mesh_publish_envelope():
    vector = set_output_state_and_level(address=11, output=0, on=True, level=80)
    envelope = gateway.build_mesh_publish(vector, ack=True)
    assert envelope["op"] == "publish" and envelope["ack"] is True
    inner = json.loads(base64.b64decode(envelope["data"]))
    assert inner["index"] == 11
    assert base64.b64decode(inner["raw"]) == gateway.repackage_command_to_ws(vector)


def test_build_mesh_publish_without_ack():
    vector = set_output_state_and_level(address=5, output=0, on=True, level=255)
    assert gateway.build_mesh_publish(vector, ack=False)["ack"] is False


def test_parse_mesh_state_report():
    report = {
        "controlType": "MeshStateReply",
        "11": "1,65535",  # on, full → brightness 255
        "24": "0,0",  # off
        "36": "1,20560",  # on, 0x5050 → level high byte 0x50
        "bad": "2,5",  # invalid state → skipped
        "x": "1,7",  # non-int address → skipped
        "9": "notvalid",  # malformed value → skipped
    }
    states = gateway.parse_mesh_state_report(report)
    assert set(states) == {11, 24, 36}
    assert states[11].on is True and states[11].level == 255
    assert states[24].on is False and states[24].level == 0
    assert states[36].on is True and states[36].level == 0x50


def test_parse_mesh_state_report_clamps_oversized_level():
    # Untrusted level beyond uint16 must not yield brightness > 255.
    states = gateway.parse_mesh_state_report({"controlType": "MeshStateReply", "11": "1,999999"})
    assert states[11].level == 255


def test_parse_mesh_state_report_rejects_other_control_type():
    with pytest.raises(ValueError, match="not a MeshStateReply"):
        gateway.parse_mesh_state_report({"controlType": "Pong"})


def test_incoming_ws_packet_decodes_as_output_state():
    # A repackaged incoming packet rebuilds a vector our protocol decoder understands.
    vector = set_output_state_and_level(address=11, output=0, on=True, level=200)
    packet = gateway.repackage_command_to_ws(vector)
    rebuilt = gateway.repackage_ws_to_command(packet, address=11)
    assert decode_command(rebuilt).command == CMD_OUTPUT_STATE_AND_LEVEL
