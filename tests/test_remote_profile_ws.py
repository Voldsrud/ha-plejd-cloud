"""Tests for the custom remote-profile WebSocket API."""

from __future__ import annotations

import types

from plejd_cloud import remote_profile_ws
from plejd_cloud.remote_profile_ws import DATA_REMOTE_PROFILES
from plejd_cloud.remote_profiles import InvalidRemoteProfile


class _Conn:
    def __init__(self):
        self.result = None
        self.error = None

    def send_result(self, msg_id, payload):
        self.result = (msg_id, payload)

    def send_error(self, msg_id, code, message):
        self.error = (msg_id, code, message)


class _Profiles:
    def __init__(self, items=None):
        self.profiles = items or {}
        self.saved = None
        self.deleted = None

    async def async_save(self, device_id, profile):
        self.saved = (device_id, profile)
        self.profiles = {**self.profiles, device_id: profile}

    async def async_delete(self, device_id):
        self.deleted = device_id
        self.profiles = {k: v for k, v in self.profiles.items() if k != device_id}


def _hass(**kw):
    return types.SimpleNamespace(data={}, **kw)


async def test_list_returns_profiles():
    hass = _hass()
    hass.data[DATA_REMOTE_PROFILES] = _Profiles({"dev1": {"buttons": []}})
    conn = _Conn()
    await remote_profile_ws.ws_list(hass, conn, {"id": 1})
    assert conn.result == (1, {"profiles": {"dev1": {"buttons": []}}})


async def test_list_errors_when_not_loaded():
    conn = _Conn()
    await remote_profile_ws.ws_list(_hass(), conn, {"id": 1})
    assert conn.error[0] == 1 and conn.error[1] == "not_loaded"
    assert conn.result is None


async def test_save_stores_profile_and_returns_all():
    hass = _hass()
    store = _Profiles()
    hass.data[DATA_REMOTE_PROFILES] = store
    conn = _Conn()
    profile = {"buttons": [{"name": "b1", "triggers": [{"type": "action", "subtype": "on"}]}]}
    await remote_profile_ws.ws_save(hass, conn, {"id": 2, "device_id": "dev1", "profile": profile})
    assert store.saved == ("dev1", profile)
    assert conn.result == (2, {"profiles": {"dev1": profile}})


async def test_save_errors_when_not_loaded():
    conn = _Conn()
    await remote_profile_ws.ws_save(_hass(), conn, {"id": 2, "device_id": "dev1", "profile": {}})
    assert conn.error[0] == 2 and conn.error[1] == "not_loaded"
    assert conn.result is None


class _RejectingProfiles:
    profiles: dict = {}

    async def async_save(self, device_id, profile):
        raise InvalidRemoteProfile("a profile needs at least one button")


async def test_save_rejects_invalid_profile_with_reason():
    hass = _hass()
    hass.data[DATA_REMOTE_PROFILES] = _RejectingProfiles()
    conn = _Conn()
    await remote_profile_ws.ws_save(hass, conn, {"id": 3, "device_id": "dev1", "profile": {}})
    assert conn.error[0] == 3 and conn.error[1] == "invalid_profile"
    assert "button" in conn.error[2]
    assert conn.result is None


class _FailingProfiles:
    profiles: dict = {}

    async def async_save(self, device_id, profile):
        raise RuntimeError("storage down")

    async def async_delete(self, device_id):
        raise RuntimeError("storage down")


async def test_save_returns_generic_error_on_unexpected_failure():
    hass = _hass()
    hass.data[DATA_REMOTE_PROFILES] = _FailingProfiles()
    conn = _Conn()
    await remote_profile_ws.ws_save(hass, conn, {"id": 4, "device_id": "dev1", "profile": {}})
    assert conn.error[0] == 4 and conn.error[1] == "save_failed"
    assert conn.result is None


async def test_delete_removes_profile_and_returns_all():
    hass = _hass()
    store = _Profiles({"dev1": {"buttons": []}})
    hass.data[DATA_REMOTE_PROFILES] = store
    conn = _Conn()
    await remote_profile_ws.ws_delete(hass, conn, {"id": 5, "device_id": "dev1"})
    assert store.deleted == "dev1"
    assert conn.result == (5, {"profiles": {}})


async def test_delete_errors_when_not_loaded():
    conn = _Conn()
    await remote_profile_ws.ws_delete(_hass(), conn, {"id": 5, "device_id": "dev1"})
    assert conn.error[0] == 5 and conn.error[1] == "not_loaded"
    assert conn.result is None


async def test_delete_returns_generic_error_on_unexpected_failure():
    hass = _hass()
    hass.data[DATA_REMOTE_PROFILES] = _FailingProfiles()
    conn = _Conn()
    await remote_profile_ws.ws_delete(hass, conn, {"id": 6, "device_id": "dev1"})
    assert conn.error[0] == 6 and conn.error[1] == "delete_failed"
    assert conn.result is None


def test_async_register_registers_all_commands():
    hass = _hass()
    remote_profile_ws.async_register(hass)
    registered = hass.data["ws_commands"]
    assert remote_profile_ws.ws_list in registered
    assert remote_profile_ws.ws_save in registered
    assert remote_profile_ws.ws_delete in registered
