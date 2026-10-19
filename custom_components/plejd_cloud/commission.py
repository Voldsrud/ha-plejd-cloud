"""Plejd BLE commissioning — provision an unprovisioned device into the mesh.

Sequence (from app Setup()):
  1. Cloud: createPlejdDevice_V2 — registers the device and returns the node index
  2. SetCryptoKey — DH key exchange; delivers the site key to the new device
  3. VerifyLogin — PingPong sanity check that the device accepted the key
  4. SetAccessAddress — writes the mesh access address (site meshKey)
  5. ReplaceLastMeshCommand × 2 — clears any stale command for this node address
  6. SetNodeIndex — assigns the mesh node index; device joins the mesh

All BLE operations connect directly with bleak — no prior mesh auth is needed
for an unprovisioned device. Transport-independent (no Home Assistant imports),
so standalone tools can use this without a running HA instance - see
tools/commission_device.py for a site with no local Bluetooth adapter/proxy.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientSession
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .cloud import (
    NewDeviceAddresses,
    NewDeviceInfo,
    PlejdCloudSite,
    async_create_device,
    async_create_room,
    async_get_needed_output_count,
)
from .connection import reversed_mac
from .const import (
    PLEJD_CHAR_ACCESS_ADDRESS_UUID,
    PLEJD_CHAR_CRYPTO_KEY_UUID,
    PLEJD_CHAR_DATA_UUID,
    PLEJD_CHAR_NODE_INDEX_UUID,
    PLEJD_CHAR_PING_UUID,
)
from .crypto import dh_encrypt_site_key, dh_generate_keypair, dh_generate_shared_key, dh_shared_secret
from .mesh import PlejdMesh
from .protocol import (
    access_address_bytes,
    node_index_bytes,
    public_key_bytes,
)
from .protocol import (
    replace_last_mesh_command as _build_replace_vector,
)

_LOGGER = logging.getLogger(__name__)

# Delays between commissioning steps (mirrors the app's Task.Delay calls).
_STEP_DELAY = 0.5
# How many times to poll for the device's DH public key before giving up.
_DH_KEY_RETRIES = 5
# Pause between DH key polls (device may need a moment to populate the char).
_DH_KEY_POLL_DELAY = 0.2
# Gap between the two ReplaceLastMeshCommand writes (app uses 100 ms).
_REPLACE_CMD_GAP = 0.1


class PlejdCommissioningSession:
    """GATT session for commissioning a single unprovisioned Plejd device."""

    def __init__(self, site_key: bytes) -> None:
        self._key = site_key
        self._client: BleakClientWithServiceCache | None = None
        self._mesh: PlejdMesh | None = None
        self._device_id: bytes | None = None

    async def connect(self, device: BLEDevice) -> None:
        """Connect to the device — no mesh auth needed for unprovisioned devices."""
        self._client = await establish_connection(BleakClientWithServiceCache, device, device.address)
        self._mesh = PlejdMesh(self._key, reversed_mac(device.address))
        self._device_id = bytes.fromhex(device.address.replace(":", ""))

    async def set_crypto_key(self) -> bool:
        """Deliver the site key to the device via Diffie-Hellman exchange.

        Returns False if the device does not supply a valid public key within retries.
        """
        client = self._client
        if client is None or self._device_id is None:
            raise RuntimeError("not connected")

        # The device pre-populates CryptoKeyID with its public key; poll until non-zero.
        device_pk_bytes: bytes | None = None
        for _ in range(_DH_KEY_RETRIES):
            raw = bytes(await client.read_gatt_char(PLEJD_CHAR_CRYPTO_KEY_UUID))
            if len(raw) == 8 and max(raw) != 0:
                device_pk_bytes = raw
                break
            await asyncio.sleep(_DH_KEY_POLL_DELAY)
        if device_pk_bytes is None:
            return False

        private_key, public_key = dh_generate_keypair()
        await client.write_gatt_char(PLEJD_CHAR_CRYPTO_KEY_UUID, public_key_bytes(public_key), response=False)

        remote_pk = int.from_bytes(device_pk_bytes, "little")
        shared_secret = dh_shared_secret(private_key, remote_pk)
        shared_key = dh_generate_shared_key(shared_secret, remote_pk, public_key, self._device_id)
        encrypted_key = dh_encrypt_site_key(self._key, shared_key)
        await client.write_gatt_char(PLEJD_CHAR_CRYPTO_KEY_UUID, encrypted_key, response=False)
        return True

    async def verify_login(self) -> bool:
        """Confirm the device accepted the new key: write N, expect N+1 back."""
        client = self._client
        if client is None:
            raise RuntimeError("not connected")
        ping = 0x01
        await client.write_gatt_char(PLEJD_CHAR_PING_UUID, bytes([ping]), response=False)
        response = bytes(await client.read_gatt_char(PLEJD_CHAR_PING_UUID))
        return len(response) >= 1 and response[0] == (ping + 1) & 0xFF

    async def set_access_address(self, mesh_key: str) -> None:
        """Write the mesh access address from the site meshKey to the device."""
        client = self._client
        if client is None:
            raise RuntimeError("not connected")
        await client.write_gatt_char(PLEJD_CHAR_ACCESS_ADDRESS_UUID, access_address_bytes(mesh_key), response=False)

    async def send_replace_last_mesh_command(self, node_index: int) -> None:
        """Send GetDeviceTypeNumber to the mesh twice (100 ms apart) for this node."""
        client = self._client
        mesh = self._mesh
        if client is None or mesh is None:
            raise RuntimeError("not connected")
        encrypted = mesh.encrypt(_build_replace_vector(node_index))
        await client.write_gatt_char(PLEJD_CHAR_DATA_UUID, encrypted, response=False)
        await asyncio.sleep(_REPLACE_CMD_GAP)
        await client.write_gatt_char(PLEJD_CHAR_DATA_UUID, encrypted, response=False)

    async def set_node_index(self, node_index: int) -> bool:
        """Write the node index and verify it was latched (3 retries)."""
        client = self._client
        if client is None:
            raise RuntimeError("not connected")
        payload = node_index_bytes(node_index)
        for _ in range(3):
            await client.write_gatt_char(PLEJD_CHAR_NODE_INDEX_UUID, payload, response=True)
            response = bytes(await client.read_gatt_char(PLEJD_CHAR_NODE_INDEX_UUID))
            if response and response[0] == node_index & 0xFF:
                return True
        return False

    async def disconnect(self) -> None:
        """Disconnect and clean up."""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
            self._mesh = None
            self._device_id = None


async def async_commission_device(
    http_session: ClientSession,
    token: str,
    site: PlejdCloudSite,
    ble_device: BLEDevice,
    name: str,
    hardware_id: str = "0",
    firmware_build_time: int = 0,
    room_id: str | None = None,
    room_title: str | None = None,
    room_category: str | None = None,
) -> NewDeviceAddresses:
    """Commission a new unprovisioned device into the Plejd site.

    Registers in the cloud first to obtain the node index, then runs the full
    BLE commissioning sequence. The caller must reload the config entry afterwards
    so HA discovers the new device.
    """
    if not site.mesh_key:
        raise RuntimeError("site has no mesh key (meshKey); cannot set the device's access address")

    device_id = ble_device.address.replace(":", "").lower()
    output_count = await async_get_needed_output_count(http_session, token, hardware_id, firmware_build_time)

    # Create the room only after the checks above - a mesh_key or compatibility
    # failure must not leave an orphaned empty room behind.
    if room_title and not room_id:
        create_room_kwargs = {"category": room_category} if room_category else {}
        room_id = await async_create_room(http_session, token, site.site_id, room_title, **create_room_kwargs)

    device_infos = [NewDeviceInfo(title=name, output_index=i, room_id=room_id) for i in range(output_count)]

    _LOGGER.debug("commissioning: registering device %s in cloud", device_id)
    addresses = await async_create_device(
        http_session,
        token,
        site.site_id,
        device_id,
        hardware_id,
        firmware_build_time,
        device_infos=device_infos,
        faceplate_id="0",
    )

    node_index = addresses.device_address
    if node_index is None:
        raise RuntimeError("cloud did not return a node address for the new device")

    _LOGGER.debug("commissioning: node index %d, starting BLE setup", node_index)
    session = PlejdCommissioningSession(site.crypto_key)
    try:
        await session.connect(ble_device)

        if not await session.set_crypto_key():
            raise RuntimeError("could not exchange crypto key with device")
        await asyncio.sleep(_STEP_DELAY)

        if not await session.verify_login():
            raise RuntimeError("device rejected the new crypto key")
        await asyncio.sleep(_STEP_DELAY)

        await session.set_access_address(site.mesh_key)
        await asyncio.sleep(_STEP_DELAY)

        await session.send_replace_last_mesh_command(node_index)
        await asyncio.sleep(_STEP_DELAY)

        if not await session.set_node_index(node_index):
            raise RuntimeError("device did not confirm node index assignment")
        await asyncio.sleep(_STEP_DELAY)
    finally:
        await session.disconnect()

    _LOGGER.debug("commissioning: %s successfully joined mesh at node %d", device_id, node_index)
    return addresses
