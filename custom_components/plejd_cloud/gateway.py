"""Plejd gateway (remote / cloud) transport codec.

Pure codec for the WebSocket relay to a Plejd Gateway (``wss://ws-ie.api.plejd.cloud``):
repackage plaintext mesh command vectors to/from the 23-byte websocket packet, build the
pub/sub message envelope, and parse mesh-state reports. See docs/gateway_protocol.md.

The gateway holds the site crypto key and encrypts onto the mesh, so this path carries
**plaintext** command structure — there is no `crypto` here, unlike the BLE path.
"""

from __future__ import annotations

import base64
import json

from .protocol import OutputState

# Production remote-control endpoint (Host.Websocket).
GATEWAY_WS_URL = "wss://ws-ie.api.plejd.cloud"
GATEWAY_HEALTH_URL = "https://ws-ie.api.plejd.cloud/status"

# Pub/sub topics: *.in = client→cloud, *.out = cloud→client.
TOPIC_MESH_IN = "mesh.in"
TOPIC_MESH_OUT = "mesh.out"
TOPIC_CONTROL_IN = "control.in"
TOPIC_CONTROL_OUT = "control.out"

CONTROL_TYPE_MESH_STATE = "MeshStateReply"
CONTROL_TYPE_PING = "Ping"
CONTROL_TYPE_PONG = "Pong"

_WS_PACKET_LEN = 23
_WS_DATA_OFFSET = 4
_MAX_WS_DATA = _WS_PACKET_LEN - _WS_DATA_OFFSET


def repackage_command_to_ws(vector: bytes) -> bytes:
    """Plaintext Datavector vector ``[addr,0x01,type,opHi,opLo,data…]`` → 23-byte ws packet.

    Drops the address + marker, keeps the command type, stores the opcode
    little-endian, then a length byte and the data. The address travels separately
    as the envelope's ``index`` field.
    """
    if len(vector) < 5:
        raise ValueError(f"command vector too short: {len(vector)} bytes")
    data = vector[5:]
    if len(data) > _MAX_WS_DATA:
        raise ValueError(f"command data too long for ws packet: {len(data)} bytes")
    packet = bytearray(_WS_PACKET_LEN)
    packet[0] = vector[2]  # command type
    packet[1] = vector[4]  # opcode low (little-endian on this wire)
    packet[2] = vector[3]  # opcode high
    packet[3] = len(data)
    packet[_WS_DATA_OFFSET : _WS_DATA_OFFSET + len(data)] = data
    return bytes(packet)


def repackage_ws_to_command(packet: bytes, address: int) -> bytes:
    """23-byte ws packet + originating address → plaintext Datavector vector.

    The inverse of :func:`repackage_command_to_ws`; rebuilds a vector that
    ``protocol.decode_command`` understands (incoming LastChanged state).
    """
    if len(packet) != _WS_PACKET_LEN:
        raise ValueError(f"ws packet must be {_WS_PACKET_LEN} bytes, got {len(packet)}")
    n = packet[3]
    if n > _MAX_WS_DATA:
        raise ValueError(f"ws packet data length out of range: {n}")
    header = bytes([address & 0xFF, 0x01, packet[0], packet[2], packet[1]])
    return header + packet[_WS_DATA_OFFSET : _WS_DATA_OFFSET + n]


def build_mesh_publish(vector: bytes, ack: bool = True) -> dict:
    """Build the ``mesh.in`` publish envelope for a plaintext command vector."""
    inner = {"raw": base64.b64encode(repackage_command_to_ws(vector)).decode(), "index": vector[0]}
    data = base64.b64encode(json.dumps(inner).encode()).decode()
    return {"op": "publish", "ack": ack, "data": data}


def parse_mesh_state_report(report: dict) -> dict[int, OutputState]:
    """Parse a ``control.out`` MeshStateReply into ``address → OutputState``.

    Every key other than ``controlType`` is a mesh address whose value is the string
    ``"<state>,<level>"`` — state ``"0"``/``"1"`` and level a uint16. Brightness is the
    level high byte, matching the BLE 0x0098 convention.
    """
    if report.get("controlType") != CONTROL_TYPE_MESH_STATE:
        raise ValueError("not a MeshStateReply")
    states: dict[int, OutputState] = {}
    for key, value in report.items():
        if key == "controlType":
            continue
        try:
            address = int(key)
        except (TypeError, ValueError):
            continue
        parts = str(value).split(",")
        if len(parts) < 2 or parts[0] not in ("0", "1") or not parts[1].isdigit():
            continue
        # Untrusted: clamp to the documented uint16 so brightness can't exceed 255.
        level = min(int(parts[1]), 0xFFFF)
        states[address] = OutputState(output=address, on=parts[0] == "1", level=level >> 8)
    return states
