"""Plejd mesh engine — the pure, transport-independent core of the coordinator.

Encrypts outgoing commands and decodes incoming state notifications, tracking the
last-known on/off + level per mesh output address. All BLE payloads are encrypted
with the *connected* device's BLE MAC (reversed) and the site crypto key — see
crypto.py. The bleak connection/auth glue lives in the coordinator; this part is
fully unit-testable.
"""

from __future__ import annotations

from . import crypto, protocol
from .protocol import OutputState


class PlejdMesh:
    """Builds encrypted commands and folds decrypted notifications into state."""

    def __init__(self, crypto_key: bytes, connection_address: bytes) -> None:
        # connection_address: the connected mesh device's BLE MAC, reversed (6 bytes).
        self._key = crypto_key
        self._address = connection_address
        self._state: dict[int, OutputState] = {}

    @property
    def state(self) -> dict[int, OutputState]:
        """A copy of the last-known output state, keyed by mesh output address."""
        return dict(self._state)

    def set_state(self, address: int, state: OutputState) -> None:
        """Locally record an output's state, e.g. an optimistic update after a local write."""
        self._state[address] = state

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt a Datavector payload for the connected device."""
        return crypto.encrypt_decrypt(self._address, plaintext, self._key)

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt an incoming Datavector/LastChangedDatavector payload (symmetric)."""
        return crypto.encrypt_decrypt(self._address, ciphertext, self._key)

    def request_output(self, address: int, output: int) -> bytes:
        """Encrypted command to read an output's current state (0x00C8, Read)."""
        return self.encrypt(protocol.request_output_state_and_level(address, output))

    def handle_notification(self, ciphertext: bytes) -> protocol.Command | None:
        """Decrypt + decode an incoming vector, updating output state as a side effect.

        Returns the decoded Command so the coordinator can route non-output events
        (buttons, motion), or None if it fails the marker check (wrong key / corrupt).
        """
        try:
            command = protocol.decode_command(self.decrypt(ciphertext))
        except ValueError:
            return None
        state = protocol.decode_output_state(command)
        if state is not None:
            self._state[command.address] = state
        return command
