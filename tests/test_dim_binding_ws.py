"""Tests for the dim-binding WebSocket API."""

from __future__ import annotations

import types

from plejd_cloud import dim_binding_ws
from plejd_cloud.bindings import _ERR_STOPLESS_BINDING, InvalidDimBinding
from plejd_cloud.dim_binding_ws import DATA_BINDINGS


class _Conn:
    def __init__(self):
        self.result = None
        self.error = None

    def send_result(self, msg_id, payload):
        self.result = (msg_id, payload)

    def send_error(self, msg_id, code, message):
        self.error = (msg_id, code, message)


class _Bindings:
    def __init__(self, items):
        self.bindings = items
        self.replaced = None

    async def async_replace(self, items):
        self.replaced = items
        self.bindings = items


def _hass(**kw):
    return types.SimpleNamespace(data={}, **kw)


async def test_list_returns_bindings():
    hass = _hass()
    hass.data[DATA_BINDINGS] = _Bindings([{"id": "b1"}])
    conn = _Conn()
    await dim_binding_ws.ws_list(hass, conn, {"id": 7})
    assert conn.result == (7, {"bindings": [{"id": "b1"}]})


async def test_list_errors_when_not_loaded():
    conn = _Conn()
    await dim_binding_ws.ws_list(_hass(), conn, {"id": 7})
    # not an empty list — an error, so the editor won't later save [] over stored bindings
    assert conn.error[0] == 7 and conn.error[1] == "not_loaded"
    assert conn.result is None


async def test_save_replaces_and_returns():
    hass = _hass()
    store = _Bindings([])
    hass.data[DATA_BINDINGS] = store
    conn = _Conn()
    new = [{"id": "b1", "up": {"x": 1}}]
    await dim_binding_ws.ws_save(hass, conn, {"id": 8, "bindings": new})
    assert store.replaced == new
    assert conn.result == (8, {"bindings": new})


async def test_save_errors_when_not_loaded():
    conn = _Conn()
    await dim_binding_ws.ws_save(_hass(), conn, {"id": 9, "bindings": []})
    assert conn.error[0] == 9 and conn.error[1] == "not_loaded"
    assert conn.result is None


async def test_device_triggers_returns_devices_triggers():
    trigs = [{"type": "brightness_move_up"}, {"type": "brightness_stop"}]
    hass = _hass(device_automations={"dev1": trigs})
    conn = _Conn()
    await dim_binding_ws.ws_device_triggers(hass, conn, {"id": 5, "device_id": "dev1"})
    msg_id, payload = conn.result
    assert msg_id == 5
    assert payload["triggers"] == trigs  # unchanged, still the raw flat list


async def test_device_triggers_empty_for_unknown_device():
    hass = _hass(device_automations={})
    conn = _Conn()
    await dim_binding_ws.ws_device_triggers(hass, conn, {"id": 5, "device_id": "gone"})
    assert conn.result == (
        5,
        {
            "triggers": [],
            "buttons": {"device_type": None, "source": "generic", "groups": []},
            "device_kind": "remote",
        },
    )


# ── "buttons" grouped view (device_triggers extension) ──────────────────────


class _Device:
    def __init__(self, manufacturer, model, model_id=None):
        self.manufacturer = manufacturer
        self.model = model
        if model_id is not None:
            self.model_id = model_id


class _Registry:
    def __init__(self, device):
        self._device = device

    def async_get(self, device_id):
        return self._device


class _RemoteProfiles:
    def __init__(self, overrides=None):
        self._overrides = overrides or {}

    def get(self, device_id):
        return self._overrides.get(device_id)


async def test_device_triggers_buttons_falls_back_to_generic_with_no_registry():
    trigs = [{"platform": "device", "type": "on", "subtype": "button_1"}]
    hass = _hass(device_automations={"dev1": trigs})
    conn = _Conn()
    await dim_binding_ws.ws_device_triggers(hass, conn, {"id": 5, "device_id": "dev1"})
    _, payload = conn.result
    assert payload["buttons"]["source"] == "generic"
    assert payload["buttons"]["groups"][0]["id"] == "subtype:button_1"


async def test_device_triggers_buttons_uses_builtin_profile_from_device_registry():
    trigs = [{"type": "action", "subtype": "on"}, {"type": "action", "subtype": "off"}]
    hass = _hass(device_automations={"dev1": trigs})
    hass.device_registry = _Registry(_Device("IKEA", "E1743"))
    conn = _Conn()
    await dim_binding_ws.ws_device_triggers(hass, conn, {"id": 5, "device_id": "dev1"})
    _, payload = conn.result
    assert payload["buttons"]["source"] == "profile"
    assert payload["buttons"]["device_type"] == "IKEA TRADFRI on/off switch"


async def test_device_triggers_buttons_matches_builtin_profile_via_model_id():
    # Some integrations report the vendor product code in the registry's separate
    # model_id field rather than in model itself.
    trigs = [{"type": "action", "subtype": "on"}]
    hass = _hass(device_automations={"dev1": trigs})
    hass.device_registry = _Registry(_Device("IKEA", "unrecognized friendly name", model_id="E1743"))
    conn = _Conn()
    await dim_binding_ws.ws_device_triggers(hass, conn, {"id": 5, "device_id": "dev1"})
    _, payload = conn.result
    assert payload["buttons"]["source"] == "profile"
    assert payload["buttons"]["device_type"] == "IKEA TRADFRI on/off switch"


async def test_device_triggers_buttons_prefers_custom_override_over_builtin_profile():
    from plejd_cloud.dim_binding_ws import DATA_REMOTE_PROFILES

    trigs = [{"type": "action", "subtype": "on"}]
    custom = {
        "buttons": [{"name": "my_button", "label": "My Button", "triggers": [{"type": "action", "subtype": "on"}]}]
    }
    hass = _hass(device_automations={"dev1": trigs})
    hass.device_registry = _Registry(_Device("IKEA", "E1743"))
    hass.data[DATA_REMOTE_PROFILES] = _RemoteProfiles({"dev1": custom})
    conn = _Conn()
    await dim_binding_ws.ws_device_triggers(hass, conn, {"id": 5, "device_id": "dev1"})
    _, payload = conn.result
    assert payload["buttons"]["source"] == "custom"
    assert payload["buttons"]["groups"][0]["id"] == "my_button"


async def test_device_triggers_reports_door_window_device_kind():
    from homeassistant.helpers import entity_registry as er

    trigs = [{"platform": "device", "type": "opened"}, {"platform": "device", "type": "closed"}]
    hass = _hass(device_automations={"dev1": trigs})
    hass.entity_registry = er.EntityRegistry(
        {
            "binary_sensor.front_door": types.SimpleNamespace(
                entity_id="binary_sensor.front_door", device_id="dev1", device_class="door"
            )
        }
    )
    conn = _Conn()
    await dim_binding_ws.ws_device_triggers(hass, conn, {"id": 5, "device_id": "dev1"})
    _, payload = conn.result
    assert payload["device_kind"] == "door_window"


async def test_device_triggers_buttons_ignores_missing_device_in_registry():
    trigs = [{"type": "on"}]
    hass = _hass(device_automations={"dev1": trigs})
    hass.device_registry = _Registry(None)  # registry present, device not found
    conn = _Conn()
    await dim_binding_ws.ws_device_triggers(hass, conn, {"id": 5, "device_id": "dev1"})
    _, payload = conn.result
    assert payload["buttons"]["source"] == "generic"


def test_async_register_registers_all_commands():
    hass = _hass()
    dim_binding_ws.async_register(hass)
    registered = hass.data["ws_commands"]
    assert dim_binding_ws.ws_list in registered
    assert dim_binding_ws.ws_save in registered
    assert dim_binding_ws.ws_device_triggers in registered


class _FailingBindings:
    bindings: list = []

    async def async_replace(self, items):
        raise RuntimeError("storage down")


class _ValueErroringBindings:
    bindings: list = []

    async def async_replace(self, items):
        raise ValueError("an internal ValueError, not client input")


async def test_save_returns_error_when_replace_fails():
    hass = _hass()
    hass.data[DATA_BINDINGS] = _FailingBindings()
    conn = _Conn()
    await dim_binding_ws.ws_save(hass, conn, {"id": 3, "bindings": [{}]})
    assert conn.error[0] == 3 and conn.error[1] == "save_failed"
    assert conn.result is None


async def test_save_maps_plain_valueerror_to_generic_error():
    # A ValueError that isn't InvalidDimBinding is an internal fault, not client input:
    # it must not be surfaced as "invalid_binding" (nor leak its message).
    hass = _hass()
    hass.data[DATA_BINDINGS] = _ValueErroringBindings()
    conn = _Conn()
    await dim_binding_ws.ws_save(hass, conn, {"id": 3, "bindings": [{}]})
    assert conn.error[0] == 3 and conn.error[1] == "save_failed"
    assert conn.result is None


class _RejectingBindings:
    bindings: list = []

    async def async_replace(self, items):
        raise InvalidDimBinding(_ERR_STOPLESS_BINDING)


async def test_save_rejects_invalid_binding_with_reason():
    hass = _hass()
    hass.data[DATA_BINDINGS] = _RejectingBindings()
    conn = _Conn()
    await dim_binding_ws.ws_save(hass, conn, {"id": 4, "bindings": [{"up": {"x": 1}}]})
    assert conn.error[0] == 4 and conn.error[1] == "invalid_binding"
    assert "stop trigger" in conn.error[2]  # the reason is surfaced to the client
    assert conn.result is None


async def test_device_triggers_reports_lookup_failure(monkeypatch):
    async def _boom(hass, automation_type, device_ids):
        raise RuntimeError("backend error")

    monkeypatch.setattr(dim_binding_ws, "async_get_device_automations", _boom)
    conn = _Conn()
    await dim_binding_ws.ws_device_triggers(_hass(), conn, {"id": 5, "device_id": "gone"})
    # a genuine failure is surfaced (not a misleading empty) so the editor can retry
    assert conn.error[0] == 5 and conn.error[1] == "triggers_failed"
    assert conn.result is None


async def test_device_triggers_reports_device_not_found(monkeypatch):
    async def _gone(hass, automation_type, device_ids):
        raise dim_binding_ws.DeviceNotFound

    monkeypatch.setattr(dim_binding_ws, "async_get_device_automations", _gone)
    conn = _Conn()
    await dim_binding_ws.ws_device_triggers(_hass(), conn, {"id": 5, "device_id": "removed"})
    # a removed device is terminal, not retryable — a distinct error, not triggers_failed
    assert conn.error[0] == 5 and conn.error[1] == "device_not_found"
    assert conn.result is None
