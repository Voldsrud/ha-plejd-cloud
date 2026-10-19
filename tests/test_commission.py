"""Tests for the Plejd BLE commissioning flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice
from plejd_cloud.cloud import NewDeviceAddresses, PlejdCloudSite
from plejd_cloud.commission import _REPLACE_CMD_GAP, PlejdCommissioningSession, async_commission_device
from plejd_cloud.const import (
    PLEJD_CHAR_ACCESS_ADDRESS_UUID,
    PLEJD_CHAR_CRYPTO_KEY_UUID,
    PLEJD_CHAR_DATA_UUID,
    PLEJD_CHAR_NODE_INDEX_UUID,
    PLEJD_CHAR_PING_UUID,
)
from plejd_cloud.crypto import dh_generate_shared_key, dh_shared_secret

_KEY = bytes(range(16))
_ADDR = "AA:BB:CC:DD:EE:FF"


def _device(address: str = _ADDR) -> BLEDevice:
    d = MagicMock(spec=BLEDevice)
    d.address = address
    return d


def _mock_client() -> AsyncMock:
    client = AsyncMock()
    client.is_connected = True
    return client


def _site(mesh_key: str = "01-02-03-04") -> PlejdCloudSite:
    return PlejdCloudSite(
        site_id="S1",
        title="Home",
        crypto_key=_KEY,
        mesh_key=mesh_key,
        devices=[],
        inputs=[],
        motion=[],
        scenes=[],
        gateways=[],
        resource_set_id=None,
    )


# ── PlejdCommissioningSession ─────────────────────────────────────────────────


async def test_connect_sets_up_mesh(monkeypatch):
    client = _mock_client()
    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
    assert session._client is client
    assert session._mesh is not None


async def test_set_crypto_key_reads_device_key_then_writes_ours_then_encrypted(monkeypatch):
    client = _mock_client()
    device_pk = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])
    client.read_gatt_char.return_value = device_pk

    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        result = await session.set_crypto_key()

    assert result is True
    client.read_gatt_char.assert_called_once_with(PLEJD_CHAR_CRYPTO_KEY_UUID)
    assert client.write_gatt_char.call_count == 2
    # First write: our public key (8 bytes LE)
    first_write = client.write_gatt_char.call_args_list[0]
    assert first_write[0][0] == PLEJD_CHAR_CRYPTO_KEY_UUID
    assert len(first_write[0][1]) == 8
    # Second write: site key encrypted with the derived shared key (16 bytes)
    second_write = client.write_gatt_char.call_args_list[1]
    assert second_write[0][0] == PLEJD_CHAR_CRYPTO_KEY_UUID
    assert len(second_write[0][1]) == 16


async def test_set_crypto_key_encrypts_with_reference_shared_key_algorithm(monkeypatch):
    # Regression test for the SetCryptoKey encryption: it must XOR the site key with
    # dh_generate_shared_key's derived 16-byte key (BleCrypto.GenerateSharedKey), not
    # the raw DH secret cycled - fixing the keypair makes the expected bytes computable.
    client = _mock_client()
    device_pub_bytes = (0x1122334455667788).to_bytes(8, "little")
    client.read_gatt_char.return_value = device_pub_bytes

    fixed_private, fixed_public = 999, 0xAABBCCDDEEFF0011
    monkeypatch.setattr("plejd_cloud.commission.dh_generate_keypair", lambda: (fixed_private, fixed_public))

    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        result = await session.set_crypto_key()

    assert result is True
    encrypted = client.write_gatt_char.call_args_list[1][0][1]

    remote_pk = int.from_bytes(device_pub_bytes, "little")
    shared_secret = dh_shared_secret(fixed_private, remote_pk)
    device_id = bytes.fromhex(_ADDR.replace(":", ""))
    shared_key = dh_generate_shared_key(shared_secret, remote_pk, fixed_public, device_id)
    recovered = bytes(a ^ b for a, b in zip(encrypted, shared_key))
    assert recovered == _KEY


async def test_set_crypto_key_retries_on_zero_key():
    client = _mock_client()
    zero = bytes(8)
    valid = bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08])
    # First two reads return all-zeros; third returns a valid key.
    client.read_gatt_char.side_effect = [zero, zero, valid]

    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
    ):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        result = await session.set_crypto_key()

    assert result is True
    assert client.read_gatt_char.call_count == 3


async def test_set_crypto_key_returns_false_after_max_retries():
    client = _mock_client()
    client.read_gatt_char.return_value = bytes(8)  # always all-zeros

    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
    ):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        result = await session.set_crypto_key()

    assert result is False
    assert client.write_gatt_char.call_count == 0


async def test_set_crypto_key_not_connected_raises():
    session = PlejdCommissioningSession(_KEY)
    with pytest.raises(RuntimeError, match="not connected"):
        await session.set_crypto_key()


async def test_verify_login_success_on_ping_plus_one():
    client = _mock_client()
    client.read_gatt_char.return_value = bytes([0x02])  # 0x01 + 1

    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        result = await session.verify_login()

    assert result is True
    client.write_gatt_char.assert_called_once_with(PLEJD_CHAR_PING_UUID, bytes([0x01]), response=False)
    client.read_gatt_char.assert_called_once_with(PLEJD_CHAR_PING_UUID)


async def test_verify_login_fails_on_wrong_response():
    client = _mock_client()
    client.read_gatt_char.return_value = bytes([0x05])  # not ping+1

    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        result = await session.verify_login()

    assert result is False


async def test_verify_login_fails_on_empty_response():
    client = _mock_client()
    client.read_gatt_char.return_value = bytes()

    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        result = await session.verify_login()

    assert result is False


async def test_verify_login_not_connected_raises():
    session = PlejdCommissioningSession(_KEY)
    with pytest.raises(RuntimeError, match="not connected"):
        await session.verify_login()


async def test_set_access_address_writes_correct_bytes():
    client = _mock_client()

    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        await session.set_access_address("AA-BB-CC-DD")

    client.write_gatt_char.assert_called_once_with(
        PLEJD_CHAR_ACCESS_ADDRESS_UUID, bytes([0xAA, 0xBB, 0xCC, 0xDD]), response=False
    )


async def test_set_access_address_not_connected_raises():
    session = PlejdCommissioningSession(_KEY)
    with pytest.raises(RuntimeError, match="not connected"):
        await session.set_access_address("AA-BB")


async def test_send_replace_last_mesh_command_writes_twice_with_gap():
    client = _mock_client()
    sleep_calls: list[float] = []

    async def _fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep", side_effect=_fake_sleep),
    ):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        await session.send_replace_last_mesh_command(7)

    # Two writes to Datavector
    data_writes = [c for c in client.write_gatt_char.call_args_list if c[0][0] == PLEJD_CHAR_DATA_UUID]
    assert len(data_writes) == 2
    # Both writes carry the same encrypted payload
    assert data_writes[0][0][1] == data_writes[1][0][1]
    # Sleep between the two writes
    assert _REPLACE_CMD_GAP in sleep_calls


async def test_send_replace_last_mesh_command_not_connected_raises():
    session = PlejdCommissioningSession(_KEY)
    with pytest.raises(RuntimeError, match="not connected"):
        await session.send_replace_last_mesh_command(1)


async def test_set_node_index_success_on_matching_read_back():
    client = _mock_client()
    client.read_gatt_char.return_value = bytes([5])

    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        result = await session.set_node_index(5)

    assert result is True
    client.write_gatt_char.assert_called_once_with(PLEJD_CHAR_NODE_INDEX_UUID, bytes([5]), response=True)
    client.read_gatt_char.assert_called_once_with(PLEJD_CHAR_NODE_INDEX_UUID)


async def test_set_node_index_retries_on_mismatch_then_succeeds():
    client = _mock_client()
    # First read returns wrong value, second returns correct.
    client.read_gatt_char.side_effect = [bytes([9]), bytes([5])]

    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        result = await session.set_node_index(5)

    assert result is True
    assert client.write_gatt_char.call_count == 2


async def test_set_node_index_returns_false_after_max_retries():
    client = _mock_client()
    client.read_gatt_char.return_value = bytes([99])  # never matches

    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        result = await session.set_node_index(5)

    assert result is False
    assert client.write_gatt_char.call_count == 3


async def test_set_node_index_not_connected_raises():
    session = PlejdCommissioningSession(_KEY)
    with pytest.raises(RuntimeError, match="not connected"):
        await session.set_node_index(1)


async def test_disconnect_clears_client_and_mesh():
    client = _mock_client()

    with patch("plejd_cloud.commission.establish_connection", return_value=client):
        session = PlejdCommissioningSession(_KEY)
        await session.connect(_device())
        await session.disconnect()

    client.disconnect.assert_called_once()
    assert session._client is None
    assert session._mesh is None


async def test_disconnect_noop_when_not_connected():
    session = PlejdCommissioningSession(_KEY)
    await session.disconnect()  # must not raise


# ── async_commission_device ───────────────────────────────────────────────────


async def test_async_commission_device_full_flow(monkeypatch):
    client = _mock_client()
    client.read_gatt_char.side_effect = [
        bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08]),  # DH: device public key
        bytes([0x02]),  # verify_login: ping+1
        bytes([3]),  # set_node_index: read-back matches
    ]

    addresses = NewDeviceAddresses(device_address=3, output_addresses={0: 30})

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))

    monkeypatch.setattr(cm, "async_create_device", AsyncMock(return_value=addresses))
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
    ):
        result = await async_commission_device(MagicMock(), "tok", _site(), _device(), "Kitchen Light")

    assert result.device_address == 3
    # set_node_index write uses response=True
    node_writes = [c for c in client.write_gatt_char.call_args_list if c[0][0] == PLEJD_CHAR_NODE_INDEX_UUID]
    assert len(node_writes) == 1


async def test_async_commission_device_derives_device_id_from_mac(monkeypatch):
    client = _mock_client()
    client.read_gatt_char.side_effect = [
        bytes([1, 2, 3, 4, 5, 6, 7, 8]),
        bytes([0x02]),
        bytes([3]),
    ]
    addresses = NewDeviceAddresses(device_address=3, output_addresses={})
    captured: list[dict] = []

    async def _fake_create(session, token, site_id, device_id, hw, fw, device_infos=None, **kw):
        captured.append({"device_id": device_id, "device_infos": device_infos})
        return addresses

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))

    monkeypatch.setattr(cm, "async_create_device", _fake_create)
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
    ):
        await async_commission_device(MagicMock(), "tok", _site(), _device("AA:BB:CC:DD:EE:FF"), "Lamp")

    assert captured[0]["device_id"] == "aabbccddeeff"
    assert captured[0]["device_infos"][0].title == "Lamp"


async def test_async_commission_device_builds_one_info_per_output(monkeypatch):
    # A DIM-02-style device has 2 outputs - deviceInfo must have one entry per output,
    # not just a single hardcoded output_index=0.
    client = _mock_client()
    client.read_gatt_char.side_effect = [bytes([1, 2, 3, 4, 5, 6, 7, 8]), bytes([0x02]), bytes([3])]
    addresses = NewDeviceAddresses(device_address=3, output_addresses={})
    captured: list[dict] = []

    async def _fake_create(session, token, site_id, device_id, hw, fw, device_infos=None, **kw):
        captured.append(device_infos)
        return addresses

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=2))
    monkeypatch.setattr(cm, "async_create_device", _fake_create)
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
    ):
        await async_commission_device(MagicMock(), "tok", _site(), _device(), "Dimmer")

    infos = captured[0]
    assert [i.output_index for i in infos] == [0, 1]
    assert all(i.title == "Dimmer" for i in infos)


async def test_async_commission_device_builds_no_infos_for_zero_output_hardware(monkeypatch):
    # A wall controller (WPH-01, WRT-01, ...) needs zero output addresses.
    client = _mock_client()
    client.read_gatt_char.side_effect = [bytes([1, 2, 3, 4, 5, 6, 7, 8]), bytes([0x02]), bytes([3])]
    addresses = NewDeviceAddresses(device_address=3, output_addresses={})
    captured: list[dict] = []

    async def _fake_create(session, token, site_id, device_id, hw, fw, device_infos=None, **kw):
        captured.append(device_infos)
        return addresses

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=0))
    monkeypatch.setattr(cm, "async_create_device", _fake_create)
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
    ):
        await async_commission_device(MagicMock(), "tok", _site(), _device(), "Switch")

    assert captured[0] == []


async def test_async_commission_device_passes_room_id_and_hardware(monkeypatch):
    client = _mock_client()
    client.read_gatt_char.side_effect = [
        bytes([1, 2, 3, 4, 5, 6, 7, 8]),
        bytes([0x02]),
        bytes([10]),
    ]
    addresses = NewDeviceAddresses(device_address=10, output_addresses={})
    captured: list[dict] = []

    async def _fake_create(session, token, site_id, device_id, hw, fw, device_infos=None, **kw):
        captured.append({"hw": hw, "fw": fw, "infos": device_infos})
        return addresses

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))

    monkeypatch.setattr(cm, "async_create_device", _fake_create)
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
    ):
        await async_commission_device(
            MagicMock(), "tok", _site(), _device(), "Hall", hardware_id="1", firmware_build_time=20241101, room_id="r1"
        )

    assert captured[0]["hw"] == "1"
    assert captured[0]["fw"] == 20241101
    assert captured[0]["infos"][0].room_id == "r1"


async def test_async_commission_device_creates_room_from_title(monkeypatch):
    client = _mock_client()
    client.read_gatt_char.side_effect = [bytes([1, 2, 3, 4, 5, 6, 7, 8]), bytes([0x02]), bytes([10])]
    addresses = NewDeviceAddresses(device_address=10, output_addresses={})
    captured: list[dict] = []

    async def _fake_create(session, token, site_id, device_id, hw, fw, device_infos=None, **kw):
        captured.append(device_infos)
        return addresses

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))
    create_room = AsyncMock(return_value="room-uuid-1")
    monkeypatch.setattr(cm, "async_create_room", create_room)
    monkeypatch.setattr(cm, "async_create_device", _fake_create)
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
    ):
        await async_commission_device(MagicMock(), "tok", _site(), _device(), "Hall", room_title="Bibliotek")

    create_room.assert_awaited_once()
    assert captured[0][0].room_id == "room-uuid-1"


async def test_async_commission_device_forwards_room_category(monkeypatch):
    client = _mock_client()
    client.read_gatt_char.side_effect = [bytes([1, 2, 3, 4, 5, 6, 7, 8]), bytes([0x02]), bytes([10])]
    addresses = NewDeviceAddresses(device_address=10, output_addresses={})
    session = MagicMock()
    site = _site()

    async def _fake_create(session, token, site_id, device_id, hw, fw, device_infos=None, **kw):
        return addresses

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))
    create_room = AsyncMock(return_value="room-uuid-1")
    monkeypatch.setattr(cm, "async_create_room", create_room)
    monkeypatch.setattr(cm, "async_create_device", _fake_create)
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
    ):
        await async_commission_device(
            session, "tok", site, _device(), "Hall", room_title="Garage", room_category="Garage"
        )

    create_room.assert_awaited_once_with(session, "tok", site.site_id, "Garage", category="Garage")


async def test_async_commission_device_omits_category_kwarg_when_not_supplied(monkeypatch):
    client = _mock_client()
    client.read_gatt_char.side_effect = [bytes([1, 2, 3, 4, 5, 6, 7, 8]), bytes([0x02]), bytes([10])]
    addresses = NewDeviceAddresses(device_address=10, output_addresses={})

    async def _fake_create(session, token, site_id, device_id, hw, fw, device_infos=None, **kw):
        return addresses

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))
    create_room = AsyncMock(return_value="room-uuid-1")
    monkeypatch.setattr(cm, "async_create_room", create_room)
    monkeypatch.setattr(cm, "async_create_device", _fake_create)
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
    ):
        await async_commission_device(MagicMock(), "tok", _site(), _device(), "Hall", room_title="Bibliotek")

    # No category kwarg at all when the caller didn't supply one - async_create_room's
    # own default ("Other") applies, this call site doesn't hardcode anything.
    assert "category" not in create_room.await_args.kwargs


async def test_async_commission_device_does_not_create_room_if_mesh_key_missing(monkeypatch):
    import plejd_cloud.commission as cm

    create_room = AsyncMock(return_value="room-uuid-1")
    monkeypatch.setattr(cm, "async_create_room", create_room)
    with pytest.raises(RuntimeError, match="mesh key"):
        await async_commission_device(MagicMock(), "tok", _site(mesh_key=""), _device(), "X", room_title="Bibliotek")
    # No orphaned room left behind when the preflight fails.
    create_room.assert_not_awaited()


async def test_async_commission_device_raises_if_cloud_returns_no_address(monkeypatch):
    addresses = NewDeviceAddresses(device_address=None, output_addresses={})

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))

    monkeypatch.setattr(cm, "async_create_device", AsyncMock(return_value=addresses))
    with pytest.raises(RuntimeError, match="node address"):
        await async_commission_device(MagicMock(), "tok", _site(), _device(), "X")


async def test_async_commission_device_raises_if_site_has_no_mesh_key(monkeypatch):
    addresses = NewDeviceAddresses(device_address=5, output_addresses={})

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))

    create_device = AsyncMock(return_value=addresses)
    monkeypatch.setattr(cm, "async_create_device", create_device)
    with pytest.raises(RuntimeError, match="mesh key"):
        await async_commission_device(MagicMock(), "tok", _site(mesh_key=""), _device(), "X")
    # Checked before any cloud mutation - no orphaned device registration.
    create_device.assert_not_awaited()


async def test_async_commission_device_raises_if_set_crypto_key_fails(monkeypatch):
    client = _mock_client()
    client.read_gatt_char.return_value = bytes(8)  # always zero → set_crypto_key returns False

    addresses = NewDeviceAddresses(device_address=5, output_addresses={})

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))

    monkeypatch.setattr(cm, "async_create_device", AsyncMock(return_value=addresses))
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
        pytest.raises(RuntimeError, match="crypto key"),
    ):
        await async_commission_device(MagicMock(), "tok", _site(), _device(), "X")


async def test_async_commission_device_raises_if_verify_login_fails(monkeypatch):
    client = _mock_client()
    client.read_gatt_char.side_effect = [
        bytes([1, 2, 3, 4, 5, 6, 7, 8]),  # set_crypto_key: device pk
        bytes([0x99]),  # verify_login: wrong response
    ]

    addresses = NewDeviceAddresses(device_address=5, output_addresses={})

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))

    monkeypatch.setattr(cm, "async_create_device", AsyncMock(return_value=addresses))
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
        pytest.raises(RuntimeError, match="rejected"),
    ):
        await async_commission_device(MagicMock(), "tok", _site(), _device(), "X")


async def test_async_commission_device_raises_if_set_node_index_fails(monkeypatch):
    client = _mock_client()
    client.read_gatt_char.side_effect = [
        bytes([1, 2, 3, 4, 5, 6, 7, 8]),  # set_crypto_key: device pk
        bytes([0x02]),  # verify_login: success
        bytes([99]),  # set_node_index: always wrong → retries × 3, all fail
        bytes([99]),
        bytes([99]),
    ]

    addresses = NewDeviceAddresses(device_address=5, output_addresses={})

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))

    monkeypatch.setattr(cm, "async_create_device", AsyncMock(return_value=addresses))
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
        pytest.raises(RuntimeError, match="node index"),
    ):
        await async_commission_device(MagicMock(), "tok", _site(), _device(), "X")


async def test_async_commission_device_disconnects_on_error(monkeypatch):
    client = _mock_client()
    client.read_gatt_char.return_value = bytes(8)  # zero → set_crypto_key fails

    addresses = NewDeviceAddresses(device_address=5, output_addresses={})

    import plejd_cloud.commission as cm

    monkeypatch.setattr(cm, "async_get_needed_output_count", AsyncMock(return_value=1))

    monkeypatch.setattr(cm, "async_create_device", AsyncMock(return_value=addresses))
    with (
        patch("plejd_cloud.commission.establish_connection", return_value=client),
        patch("plejd_cloud.commission.asyncio.sleep"),
        pytest.raises(RuntimeError, match="crypto key"),
    ):
        await async_commission_device(MagicMock(), "tok", _site(), _device(), "X")

    # disconnect() must always be called (via finally)
    client.disconnect.assert_called_once()
