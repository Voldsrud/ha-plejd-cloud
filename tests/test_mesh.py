"""Tests for the Plejd mesh engine (encrypt/decrypt + state from notifications)."""

from __future__ import annotations

from plejd_cloud.mesh import PlejdMesh
from plejd_cloud.protocol import (
    CMD_OUTPUT_STATE_AND_LEVEL,
    TYPE_READ,
    OutputState,
    decode_command,
    execute_scene,
    set_output_state_and_level,
)

_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
_MAC = bytes.fromhex("0102030405a0")  # connected device MAC, reversed


def _mesh():
    return PlejdMesh(_KEY, _MAC)


def _output_cipher(mesh, address, output, on, level):
    # Build a plaintext output command (codec lives in protocol) and encrypt it.
    return mesh.encrypt(set_output_state_and_level(address, output, on, level))


def test_encrypt_decrypt_round_trips():
    mesh = _mesh()
    cmd = decode_command(mesh.decrypt(_output_cipher(mesh, 5, 0, True, 200)))
    assert cmd.address == 5 and cmd.command == CMD_OUTPUT_STATE_AND_LEVEL
    assert cmd.data == bytes([0, 1, 200, 200])


def test_request_output_uses_read_type():
    mesh = _mesh()
    cmd = decode_command(mesh.decrypt(mesh.request_output(address=3, output=1)))
    assert cmd.command_type == TYPE_READ and cmd.data == bytes([1])


def test_handle_notification_updates_state():
    mesh = _mesh()
    command = mesh.handle_notification(_output_cipher(mesh, 5, 0, True, 128))
    assert command is not None
    assert (mesh.state[5].on, mesh.state[5].level) == (True, 128)


def test_handle_notification_drops_undecodable():
    mesh = _mesh()
    # Random ciphertext won't decrypt to a valid marker -> dropped, state untouched.
    assert mesh.handle_notification(bytes(range(9))) is None
    assert mesh.state == {}


def test_handle_notification_returns_command_without_touching_output_state():
    mesh = _mesh()
    # A non-output command (scene) still decodes to a Command but updates no state.
    command = mesh.handle_notification(mesh.encrypt(execute_scene(1, 2)))
    assert command is not None and command.command == 0x0021
    assert mesh.state == {}


def test_state_returns_a_copy():
    mesh = _mesh()
    mesh.handle_notification(_output_cipher(mesh, 2, 0, True, 10))
    snapshot = mesh.state
    snapshot.clear()
    assert 2 in mesh.state  # mutating the snapshot doesn't affect the engine


def test_set_state_records_locally():
    mesh = _mesh()
    mesh.set_state(9, OutputState(output=0, on=True, level=200))
    assert mesh.state[9] == OutputState(output=0, on=True, level=200)
