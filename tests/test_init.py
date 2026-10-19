"""Tests for plejd integration setup/teardown."""

from __future__ import annotations

import asyncio
import inspect
import types
from unittest.mock import AsyncMock

import plejd_cloud
import pytest
from plejd_cloud import (
    PLATFORMS,
    SERVICE_ADD_DEVICE,
    SERVICE_ALL_OFF,
    SERVICE_CREATE_SCENE,
    SERVICE_CREATE_SCHEDULE,
    SERVICE_MOVE_DEVICE_TO_ROOM,
    SERVICE_REMOVE_DEVICE,
    SERVICE_REMOVE_ROOM,
    SERVICE_REMOVE_SCENE,
    SERVICE_REMOVE_SCHEDULE,
    SERVICE_SCAN_DEVICES,
    SERVICE_UPDATE_ROOM,
    SERVICE_UPDATE_SCENE,
    SERVICE_UPDATE_SCHEDULE,
    async_setup_entry,
    async_unload_entry,
)


class _FakeCoordinator:
    instances: list = []

    def __init__(self, hass, entry):
        self.started = False
        self.shutdown = False
        self.all_off_calls = 0
        _FakeCoordinator.instances.append(self)

    async def async_start(self):
        self.started = True

    async def async_shutdown(self):
        self.shutdown = True

    async def async_handle_device_registry_update(self, event):
        return None

    async def async_all_off(self):
        self.all_off_calls += 1


class _FakeHolidayMode:
    instances: list = []
    call_order: list = []
    stop_result = True

    def __init__(self, hass, entry):
        self.started = False
        self.stopped = False
        self.is_running = False
        _FakeHolidayMode.instances.append(self)

    async def async_start(self):
        self.started = True
        self.is_running = True
        _FakeHolidayMode.call_order.append("holiday_mode.async_start")

    async def async_stop(self):
        self.stopped = True
        self.is_running = False
        _FakeHolidayMode.call_order.append("holiday_mode.async_stop")
        return _FakeHolidayMode.stop_result


class _FakeConfigEntries:
    unload_result = True

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded = platforms

    async def async_unload_platforms(self, entry, platforms):
        _FakeHolidayMode.call_order.append("async_unload_platforms")
        return self.unload_result

    async def async_reload(self, entry_id):
        self.reloaded = entry_id
        return getattr(self, "reload_result", True)

    def async_update_entry(self, entry, *, data=None, **kwargs):
        if data is not None:
            entry.data = data


class _FakeServices:
    def __init__(self):
        self._handlers: dict[str, object] = {}

    def async_register(self, domain, service, handler, schema=None):
        self._handlers[f"{domain}.{service}"] = handler

    def async_remove(self, domain, service):
        self._handlers.pop(f"{domain}.{service}", None)

    def has_service(self, domain, service):
        return f"{domain}.{service}" in self._handlers


async def _run_unload(unload) -> None:
    """Run one on_unload callback the way HA does: a returned coroutine is awaited."""
    result = unload()
    if inspect.iscoroutine(result):
        await asyncio.gather(result)


class _FakeBus:
    def __init__(self):
        self.fired: list[tuple[str, dict]] = []
        self.listeners = []

    def async_fire(self, event_type: str, data: dict) -> None:
        self.fired.append((event_type, data))

    def async_listen(self, event, handler):
        self.listeners.append((event, handler))
        return lambda: None


async def _register_static_paths(configs):
    return None


def _hass():
    h = types.SimpleNamespace(config_entries=_FakeConfigEntries())
    h.services = _FakeServices()
    h.bus = _FakeBus()
    h.session = None
    h.ble_devices = {}
    h.service_infos = []
    h.http = types.SimpleNamespace(async_register_static_paths=_register_static_paths)
    h.data = {}
    return h


def _entry(data=None):
    return types.SimpleNamespace(
        entry_id="e1",
        data=data or {},
        options={},
        runtime_data=None,
        async_on_unload=lambda f: None,
        add_update_listener=lambda f: lambda: None,
    )


async def test_setup_starts_coordinator_and_forwards(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    assert await async_setup_entry(hass, entry) is True
    coord = entry.runtime_data
    assert coord.started is True
    assert hass.config_entries.forwarded == PLATFORMS


async def test_setup_registers_device_rename_listener(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    events = [e for e, _ in hass.bus.listeners]
    assert "device_registry_updated" in events
    handler = next(h for e, h in hass.bus.listeners if e == "device_registry_updated")
    assert handler == entry.runtime_data.async_handle_device_registry_update


async def test_setup_shuts_down_when_forward_fails(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    async def _boom(entry, platforms):
        raise RuntimeError("platform setup failed")

    hass.config_entries.async_forward_entry_setups = _boom
    with pytest.raises(RuntimeError, match="platform setup failed"):
        await async_setup_entry(hass, entry)
    assert _FakeCoordinator.instances[-1].shutdown is True


async def test_setup_stops_holiday_mode_when_forward_fails(monkeypatch):
    from plejd_cloud.holiday_mode import DATA_HOLIDAY_MODE

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    monkeypatch.setattr(plejd_cloud, "PlejdHolidayMode", _FakeHolidayMode)
    _FakeCoordinator.instances.clear()
    _FakeHolidayMode.instances.clear()
    _FakeHolidayMode.call_order.clear()
    hass, entry = _hass(), _entry()

    async def _boom(entry, platforms):
        raise RuntimeError("platform setup failed")

    hass.config_entries.async_forward_entry_setups = _boom
    with pytest.raises(RuntimeError, match="platform setup failed"):
        await async_setup_entry(hass, entry)
    assert DATA_HOLIDAY_MODE not in hass.data  # no stale manager left for a later retry
    # A platform forwarded before the failure could have already started it (e.g. a
    # restored-on holiday switch) — its timer must not be left running (#89 review).
    assert _FakeHolidayMode.instances[-1].stopped is True
    # start() runs first so a manager no platform had reached yet still loads its
    # persisted deadlines before stop() persists — otherwise stop would overwrite the
    # store with an empty, never-loaded state (#89 review).
    assert _FakeHolidayMode.call_order == ["holiday_mode.async_start", "holiday_mode.async_stop"]


async def test_unload_shuts_down_coordinator(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    entry = _entry()
    entry.runtime_data = _FakeCoordinator(None, entry)
    hass = _hass()
    assert await async_unload_entry(hass, entry) is True
    assert entry.runtime_data.shutdown is True


async def test_failed_unload_keeps_coordinator(monkeypatch):
    entry = _entry()
    entry.runtime_data = _FakeCoordinator(None, entry)
    hass = _hass()
    hass.config_entries.unload_result = False
    assert await async_unload_entry(hass, entry) is False
    assert entry.runtime_data.shutdown is False


async def test_reload_listener_reloads_entry():
    from plejd_cloud import _async_reload_entry

    hass, entry = _hass(), _entry()
    await _async_reload_entry(hass, entry)
    assert hass.config_entries.reloaded == "e1"


async def test_reload_listener_clears_a_stale_pending_marker_after_reloading():
    # A follow-up reload that failed deliberately leaves DATA_RELOAD_PENDING set. This
    # direct reload applies whatever the entry currently holds, including that change - so
    # leaving the marker would make the next management operation reload a second time for
    # something already live, a needless teardown and BLE/gateway reconnect.
    from plejd_cloud import _async_reload_entry, schedule_ws

    hass, entry = _hass(), _entry()
    hass.data[schedule_ws.DATA_RELOAD_PENDING] = entry.entry_id
    await _async_reload_entry(hass, entry)
    assert hass.config_entries.reloaded == "e1"
    assert schedule_ws.DATA_RELOAD_PENDING not in hass.data


async def test_reload_listener_keeps_a_pending_marker_created_during_its_own_reload():
    # A change landing AFTER this reload read the entry is not covered by it, so its marker
    # must survive - otherwise the lock holder skips the follow-up and that change never
    # gets applied until some later reload or a restart.
    from plejd_cloud import _async_reload_entry, schedule_ws

    hass, entry = _hass(), _entry()
    schedule_ws.async_mark_reload_pending(hass, entry.entry_id)  # an older, covered change
    covered_token = schedule_ws.async_reload_pending_token(hass, entry.entry_id)

    async def _reload_then_a_change_lands(entry_id):
        hass.config_entries.reloaded = entry_id
        schedule_ws.async_mark_reload_pending(hass, entry_id)  # a NEWER one, mid-reload
        return True

    hass.config_entries.async_reload = _reload_then_a_change_lands
    await _async_reload_entry(hass, entry)

    # the newer marker survives, and is genuinely a different one
    assert hass.data[schedule_ws.DATA_RELOAD_PENDING] == entry.entry_id
    assert schedule_ws.async_reload_pending_token(hass, entry.entry_id) != covered_token


async def test_reload_listener_keeps_the_pending_marker_when_the_reload_is_rejected():
    # Only a reload that actually applied may clear it - otherwise the concurrent change it
    # stands for would be silently dropped.
    from plejd_cloud import _async_reload_entry, schedule_ws

    hass, entry = _hass(), _entry()
    hass.config_entries.reload_result = False
    hass.data[schedule_ws.DATA_RELOAD_PENDING] = entry.entry_id
    await _async_reload_entry(hass, entry)
    assert hass.data[schedule_ws.DATA_RELOAD_PENDING] == entry.entry_id


async def test_reload_listener_skips_when_reload_lock_is_held():
    from plejd_cloud import _async_reload_entry, schedule_ws

    hass, entry = _hass(), _entry()
    await schedule_ws.async_get_reload_lock(hass, entry.entry_id).acquire()
    await _async_reload_entry(hass, entry)
    assert getattr(hass.config_entries, "reloaded", None) is None


async def test_reload_listener_does_not_mark_pending_for_its_own_update(monkeypatch):
    # A listener call while the lock is held, anticipated via async_mark_expecting_self_reload
    # (the lock holder's own async_update_entry() triggering this same listener for the
    # change it's already handling), is not a genuinely concurrent change - so it must not
    # schedule a redundant follow-up reload (issue #94 thread 3: every schedule edit was
    # otherwise reloading twice).
    from plejd_cloud import _async_reload_entry, schedule_ws

    hass, entry = _hass(), _entry()
    await schedule_ws.async_get_reload_lock(hass, entry.entry_id).acquire()
    schedule_ws.async_mark_expecting_self_reload(hass, entry.entry_id)
    await _async_reload_entry(hass, entry)
    assert schedule_ws.DATA_RELOAD_PENDING not in hass.data


async def test_reload_listener_marks_pending_on_an_unanticipated_concurrent_call():
    # A call while the lock is held that the lock holder did NOT mark as expected is a
    # genuinely different, concurrent change (e.g. a concurrent options-flow edit) - it must
    # be marked pending so the in-flight lock holder runs a follow-up for it once done
    # (issue #94 thread 2), rather than the change never taking effect.
    from plejd_cloud import _async_reload_entry, schedule_ws

    hass, entry = _hass(), _entry()
    await schedule_ws.async_get_reload_lock(hass, entry.entry_id).acquire()
    await _async_reload_entry(hass, entry)
    assert getattr(hass.config_entries, "reloaded", None) is None
    assert hass.data[schedule_ws.DATA_RELOAD_PENDING] == entry.entry_id


async def test_reload_listener_does_not_mark_pending_for_a_different_entry():
    from plejd_cloud import _async_reload_entry, schedule_ws

    hass, entry = _hass(), _entry()
    await schedule_ws.async_get_reload_lock(hass, "some-other-entry").acquire()
    await _async_reload_entry(hass, entry)
    assert hass.config_entries.reloaded == "e1"
    assert schedule_ws.DATA_RELOAD_PENDING not in hass.data


def test_every_platform_module_is_forwarded():
    # Guard against adding a platform file but forgetting to forward it.
    import pathlib

    cc = pathlib.Path(__file__).parent.parent / "custom_components" / "plejd_cloud"
    platform_files = {
        f.stem
        for f in cc.glob("*.py")
        if f.stem in {"light", "switch", "cover", "climate", "binary_sensor", "sensor", "event", "scene"}
    }
    forwarded = {p.value for p in PLATFORMS}
    assert platform_files <= forwarded, f"not forwarded: {platform_files - forwarded}"


# ── Service: add_device ───────────────────────────────────────────────────────
#
# async_add_device() itself (cloud calls, BLE validation, commissioning, error
# paths) is unit-tested in test_add_device.py, where it lives. These tests only
# cover that the service is registered and forwards call.data correctly.


async def test_add_device_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_ADD_DEVICE}" in hass.services._handlers


def test_input_setting_schema_requires_input_and_button_type():
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        plejd_cloud._INPUT_SETTING_SCHEMA({"button_type": "Toggle"})
    with pytest.raises(vol.Invalid):
        plejd_cloud._INPUT_SETTING_SCHEMA({"input": 0})
    assert plejd_cloud._INPUT_SETTING_SCHEMA({"input": 0, "button_type": "Toggle"}) == {
        "input": 0,
        "button_type": "Toggle",
    }


def test_add_device_schema_rejects_invalid_room_category():
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        plejd_cloud._ADD_DEVICE_SCHEMA(
            {"device_address": "AA:BB:CC:DD:EE:FF", "name": "X", "room_category": "KidsRoom"}
        )
    assert (
        plejd_cloud._ADD_DEVICE_SCHEMA({"device_address": "AA:BB:CC:DD:EE:FF", "name": "X", "room_category": "Garage"})[
            "room_category"
        ]
        == "Garage"
    )


def test_update_room_schema_rejects_negative_order():
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        plejd_cloud._UPDATE_ROOM_SCHEMA({"room_id": "r1", "order": -1})
    assert plejd_cloud._UPDATE_ROOM_SCHEMA({"room_id": "r1", "order": 0})["order"] == 0


def test_update_room_schema_rejects_invalid_category():
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        plejd_cloud._UPDATE_ROOM_SCHEMA({"room_id": "r1", "category": "NotARealCategory"})
    assert plejd_cloud._UPDATE_ROOM_SCHEMA({"room_id": "r1", "category": "Kitchen"})["category"] == "Kitchen"


_VALID_STEP = {"device_id": "d1", "output": 0, "state": "On", "value": 255}


def test_scene_step_schema_rejects_invalid_state():
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        plejd_cloud._SCENE_STEP_SCHEMA({**_VALID_STEP, "state": "NotARealState"})
    assert plejd_cloud._SCENE_STEP_SCHEMA(_VALID_STEP)["state"] == "On"


def test_create_scene_schema_rejects_negative_order():
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        plejd_cloud._CREATE_SCENE_SCHEMA({"title": "X", "scene_steps": [_VALID_STEP], "order": -1})
    assert plejd_cloud._CREATE_SCENE_SCHEMA({"title": "X", "scene_steps": [_VALID_STEP]})["order"] == 0


def test_update_scene_schema_rejects_negative_order():
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        plejd_cloud._UPDATE_SCENE_SCHEMA({"scene_id": "s1", "order": -1})
    assert plejd_cloud._UPDATE_SCENE_SCHEMA({"scene_id": "s1", "order": 0})["order"] == 0


async def test_add_device_service_survives_entry_unload_cleanup(monkeypatch):
    # Cloud-only services are registered once and NOT torn down by entry.async_on_unload,
    # since HA runs every already-registered on_unload callback as cleanup when
    # async_setup_entry raises ConfigEntryNotReady - if this service were removed by that
    # cleanup, it would flicker unavailable on every failed mesh-connection retry.
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_ADD_DEVICE}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_ADD_DEVICE}" in hass.services._handlers


async def test_remove_device_service_survives_a_mesh_connection_failure(monkeypatch):
    # Mirrors real HA behavior for the exact scenario this feature exists for: raising
    # ConfigEntryNotReady from async_setup_entry (a failed mesh connection) makes HA run
    # every already-registered on_unload callback as cleanup before scheduling a retry -
    # simulated here by invoking the captured callbacks after the exception propagates,
    # the same as config_entries.py's ConfigEntryNotReady handling does.
    from homeassistant.exceptions import ConfigEntryNotReady

    class _FailingCoordinator(_FakeCoordinator):
        async def async_start(self):
            raise ConfigEntryNotReady("mesh unreachable")

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FailingCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_REMOVE_DEVICE}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_REMOVE_DEVICE}" in hass.services._handlers


async def test_add_device_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    add_device = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_add_device", add_device)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_ADD_DEVICE}"]
    call = types.SimpleNamespace(
        data={
            "device_address": "AA:BB:CC:DD:EE:FF",
            "name": "Taklampa",
            "hardware_id": "1",
            "room_id": "r1",
            "room_title": "Sovrum",
            "room_category": "Bedroom",
            "firmware_build_time": 123,
            "input_settings": [{"input": 0, "button_type": "Toggle"}],
        }
    )
    await handler(call)

    add_device.assert_awaited_once_with(
        hass,
        entry,
        address="AA:BB:CC:DD:EE:FF",
        name="Taklampa",
        hardware_id="1",
        room_id="r1",
        room_title="Sovrum",
        room_category="Bedroom",
        firmware_build_time=123,
        input_settings=[{"input": 0, "button_type": "Toggle"}],
    )


async def test_add_device_service_forwards_defaults(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    add_device = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_add_device", add_device)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_ADD_DEVICE}"]
    call = types.SimpleNamespace(data={"device_address": "AA:BB:CC:DD:EE:FF", "name": "X"})
    await handler(call)

    add_device.assert_awaited_once_with(
        hass,
        entry,
        address="AA:BB:CC:DD:EE:FF",
        name="X",
        hardware_id="0",
        room_id=None,
        room_title=None,
        room_category=None,
        firmware_build_time=0,
        input_settings=[],
    )


# ── Service: scan_new_devices ─────────────────────────────────────────────────


def _fake_service_info(address, service_uuids, mfr_data, rssi=-70, name=None):
    """Build a minimal BluetoothServiceInfoBleak stand-in for scanner tests."""
    from plejd_cloud.const import PLEJD_SERVICE_UUID

    return types.SimpleNamespace(
        address=address,
        name=name,
        rssi=rssi,
        service_uuids=service_uuids or [PLEJD_SERVICE_UUID],
        manufacturer_data=mfr_data,
    )


async def test_scan_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_SCAN_DEVICES}" in hass.services._handlers


async def test_scan_raises_when_bluetooth_unavailable(monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    hass.scanner_count = 0  # no local adapter, no ESPHome Bluetooth proxy
    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_SCAN_DEVICES}"]
    with pytest.raises(HomeAssistantError, match="Bluetooth is not available"):
        await handler(types.SimpleNamespace(data={}))


async def test_scan_finds_unprovisioned_on_default_mesh(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    # LoginByte 0x08 = IsOnDefaultMesh, hardware_id byte at offset 3 = 1 (DIM-01)
    hass.service_infos = [_fake_service_info("AA:BB:CC:DD:EE:FF", None, {887: bytes([0x08, 0, 0, 1])})]
    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_SCAN_DEVICES}"]
    await handler(types.SimpleNamespace(data={}))
    scans = [f for f in hass.bus.fired if f[0] == "plejd_new_devices_found"]
    assert len(scans) == 1
    event_type, data = scans[0]
    devs = data["devices"]
    assert len(devs) == 1
    assert devs[0]["address"] == "AA:BB:CC:DD:EE:FF"
    assert devs[0]["hardware_id"] == "1"
    assert devs[0]["model"] == "DIM-01"


async def test_scan_finds_unclaimed_device(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    # LoginByte 0x00 = no flags set = unclaimed; hardware_id at offset 3 = 3 (CTR-01)
    hass.service_infos = [_fake_service_info("11:22:33:44:55:66", None, {887: bytes([0x00, 0, 0, 3])})]
    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_SCAN_DEVICES}"]
    await handler(types.SimpleNamespace(data={}))
    devs = [f for f in hass.bus.fired if f[0] == "plejd_new_devices_found"][0][1]["devices"]
    assert devs[0]["model"] == "CTR-01"


async def test_scan_excludes_provisioned_device(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    # LoginByte 0x07 = all three provisioning bits set, not on default mesh → provisioned
    hass.service_infos = [_fake_service_info("AA:BB:CC:DD:EE:FF", None, {887: bytes([0x07, 0, 0, 1])})]
    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_SCAN_DEVICES}"]
    await handler(types.SimpleNamespace(data={}))
    devs = [f for f in hass.bus.fired if f[0] == "plejd_new_devices_found"][0][1]["devices"]
    assert devs == []


async def test_scan_excludes_non_plejd_devices(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    # Different service UUID — not a Plejd device
    hass.service_infos = [
        _fake_service_info("AA:BB:CC:DD:EE:FF", ["0000180a-0000-1000-8000-00805f9b34fb"], {887: bytes([0x08, 0, 0, 1])})
    ]
    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_SCAN_DEVICES}"]
    await handler(types.SimpleNamespace(data={}))
    devs = [f for f in hass.bus.fired if f[0] == "plejd_new_devices_found"][0][1]["devices"]
    assert devs == []


async def test_scan_fires_event_with_empty_list_when_no_devices(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    hass.service_infos = []
    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_SCAN_DEVICES}"]
    await handler(types.SimpleNamespace(data={}))
    assert [f for f in hass.bus.fired if f[0] == "plejd_new_devices_found"] == [
        ("plejd_new_devices_found", {"devices": []})
    ]


# ── Service: all_off ──────────────────────────────────────────────────────────


async def test_all_off_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_ALL_OFF}" in hass.services._handlers


async def test_all_off_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_ALL_OFF}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_ALL_OFF}" in hass.services._handlers


async def test_unload_callbacks_return_nothing_ha_would_try_to_schedule(monkeypatch):
    # HA schedules any non-None on_unload return value as a task, so a callback that
    # returns e.g. the object it popped crashes the unload (and every entry reload).
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    results = [unload() for unload in unloads]
    assert [r for r in results if r is not None and not inspect.iscoroutine(r)] == []
    await asyncio.gather(*(r for r in results if inspect.iscoroutine(r)))  # HA runs these as tasks


async def test_setup_tells_an_open_dashboard_to_reload_rooms(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert [f[0] for f in hass.bus.fired] == ["plejd_rooms_changed"]


async def test_all_off_service_calls_coordinator(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_ALL_OFF}"]
    await handler(types.SimpleNamespace(data={}))
    assert entry.runtime_data.all_off_calls == 1


async def test_all_off_service_uses_the_current_coordinator_after_a_retry(monkeypatch):
    # The handler is only ever (re-)registered on the first setup attempt (the guard
    # skips re-registering on a later successful retry), so it must look up
    # entry.runtime_data fresh at call time rather than close over the first attempt's
    # (failed) coordinator instance.
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)  # first attempt
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_ALL_OFF}"]
    await async_setup_entry(hass, entry)  # simulated retry: new coordinator instance
    await handler(types.SimpleNamespace(data={}))
    assert entry.runtime_data is _FakeCoordinator.instances[-1]
    assert entry.runtime_data.all_off_calls == 1
    assert _FakeCoordinator.instances[0].all_off_calls == 0


# ── Services: update_room / remove_room ───────────────────────────────────────
#
# async_update_room() / async_remove_room() themselves (cloud calls, precondition
# checks, error paths) are unit-tested in test_manage_room.py. These tests only
# cover that the services are registered and forward call.data correctly.


async def test_update_room_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_UPDATE_ROOM}" in hass.services._handlers


async def test_update_room_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_UPDATE_ROOM}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_UPDATE_ROOM}" in hass.services._handlers


async def test_update_room_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    update_room = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_update_room", update_room)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_UPDATE_ROOM}"]
    call = types.SimpleNamespace(data={"room_id": "r1", "title": "Kök", "order": 2, "category": "Kitchen"})
    await handler(call)

    update_room.assert_awaited_once_with(hass, entry, room_id="r1", title="Kök", order=2, category="Kitchen")


async def test_update_room_service_rebinds_to_a_new_entry_after_reinstall(monkeypatch):
    # The handler is only ever (re-)registered on the first setup attempt (the guard
    # skips re-registering later), so removing the integration and adding it again
    # (a genuinely different ConfigEntry object, not just a retry of the same one) must
    # still route to the new entry, not the one the closures were first created for.
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry1 = _hass(), _entry()
    await async_setup_entry(hass, entry1)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_UPDATE_ROOM}"]

    entry2 = _entry()
    entry2.entry_id = "e2"
    await async_setup_entry(hass, entry2)

    update_room = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_update_room", update_room)
    await handler(types.SimpleNamespace(data={"room_id": "r1"}))

    update_room.assert_awaited_once_with(hass, entry2, room_id="r1", title=None, order=None, category=None)


async def test_update_room_service_forwards_defaults(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    update_room = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_update_room", update_room)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_UPDATE_ROOM}"]
    await handler(types.SimpleNamespace(data={"room_id": "r1"}))

    update_room.assert_awaited_once_with(hass, entry, room_id="r1", title=None, order=None, category=None)


async def test_remove_room_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_REMOVE_ROOM}" in hass.services._handlers


async def test_remove_room_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_REMOVE_ROOM}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_REMOVE_ROOM}" in hass.services._handlers


async def test_remove_room_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    remove_room = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_remove_room", remove_room)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_REMOVE_ROOM}"]
    await handler(types.SimpleNamespace(data={"room_id": "r1"}))

    remove_room.assert_awaited_once_with(hass, entry, room_id="r1")


# ── Services: create_scene / update_scene / remove_scene ──────────────────────
#
# async_create_scene() / async_update_scene() / async_remove_scene() themselves
# are unit-tested in test_manage_scene.py. These tests only cover that the
# services are registered and forward call.data correctly.

_STEP = {"device_id": "d1", "output": 0, "state": "On", "value": 255}


async def test_create_scene_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_CREATE_SCENE}" in hass.services._handlers


async def test_create_scene_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_CREATE_SCENE}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_CREATE_SCENE}" in hass.services._handlers


async def test_create_scene_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    create_scene = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_create_scene", create_scene)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_CREATE_SCENE}"]
    call = types.SimpleNamespace(
        data={"title": "Movie Night", "scene_steps": [_STEP], "order": 2, "hidden_from_scene_list": True}
    )
    await handler(call)

    create_scene.assert_awaited_once_with(
        hass, entry, title="Movie Night", scene_steps=[_STEP], order=2, hidden_from_scene_list=True
    )


async def test_update_scene_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_UPDATE_SCENE}" in hass.services._handlers


async def test_update_scene_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_UPDATE_SCENE}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_UPDATE_SCENE}" in hass.services._handlers


async def test_update_scene_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    update_scene = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_update_scene", update_scene)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_UPDATE_SCENE}"]
    call = types.SimpleNamespace(
        data={"scene_id": "s1", "title": "X", "order": 1, "scene_steps": [_STEP], "hidden_from_scene_list": False}
    )
    await handler(call)

    update_scene.assert_awaited_once_with(
        hass, entry, scene_id="s1", title="X", order=1, scene_steps=[_STEP], hidden_from_scene_list=False
    )


async def test_update_scene_service_forwards_defaults(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    update_scene = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_update_scene", update_scene)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_UPDATE_SCENE}"]
    await handler(types.SimpleNamespace(data={"scene_id": "s1"}))

    update_scene.assert_awaited_once_with(
        hass, entry, scene_id="s1", title=None, order=None, scene_steps=None, hidden_from_scene_list=None
    )


async def test_remove_scene_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_REMOVE_SCENE}" in hass.services._handlers


async def test_remove_scene_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_REMOVE_SCENE}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_REMOVE_SCENE}" in hass.services._handlers


async def test_remove_scene_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    remove_scene = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_remove_scene", remove_scene)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_REMOVE_SCENE}"]
    await handler(types.SimpleNamespace(data={"scene_id": "s1"}))

    remove_scene.assert_awaited_once_with(hass, entry, scene_id="s1")


# ── Service: remove_device ─────────────────────────────────────────────────────
#
# async_remove_device() itself is unit-tested in test_manage_device.py. These
# tests only cover that the service is registered and forwards call.data.


async def test_remove_device_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_REMOVE_DEVICE}" in hass.services._handlers


async def test_remove_device_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_REMOVE_DEVICE}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_REMOVE_DEVICE}" in hass.services._handlers


async def test_remove_device_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    remove_device = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_remove_device", remove_device)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_REMOVE_DEVICE}"]
    await handler(types.SimpleNamespace(data={"device_id": "d1"}))

    remove_device.assert_awaited_once_with(hass, entry, device_id="d1")


# ── Service: move_device_to_room ───────────────────────────────────────────────
#
# async_move_device_to_room() itself is unit-tested in test_manage_device_room.py.
# These tests only cover that the service is registered and forwards call.data.


async def test_move_device_to_room_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_MOVE_DEVICE_TO_ROOM}" in hass.services._handlers


async def test_move_device_to_room_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_MOVE_DEVICE_TO_ROOM}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_MOVE_DEVICE_TO_ROOM}" in hass.services._handlers


async def test_move_device_to_room_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    move_device_to_room = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_move_device_to_room", move_device_to_room)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_MOVE_DEVICE_TO_ROOM}"]
    await handler(types.SimpleNamespace(data={"device_id": "d1", "room_id": "r2"}))

    move_device_to_room.assert_awaited_once_with(hass, entry, device_id="d1", room_id="r2")


# ── Services: create_schedule / update_schedule ────────────────────────────────
#
# async_create_schedule() / async_update_schedule() themselves are unit-tested in
# test_manage_schedule.py. These tests only cover that the services are
# registered and forward call.data correctly.


async def test_create_schedule_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_CREATE_SCHEDULE}" in hass.services._handlers


async def test_create_schedule_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_CREATE_SCHEDULE}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_CREATE_SCHEDULE}" in hass.services._handlers


async def test_create_schedule_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    create_schedule = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_create_schedule", create_schedule)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_CREATE_SCHEDULE}"]
    night_reduction = {"scene_steps": [_STEP], "start_time": "23:15", "end_time": "05:30"}
    call = types.SimpleNamespace(
        data={
            "title": "Garage",
            "scene_steps": [_STEP],
            "start_event": "sunset",
            "start_offset": 15,
            "end_event": "sunrise",
            "end_offset": 0,
            "scheduled_days": [0, 1],
            "fade_time": 2,
            "night_reduction": night_reduction,
        }
    )
    await handler(call)

    create_schedule.assert_awaited_once_with(
        hass,
        entry,
        title="Garage",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=15,
        end_event="sunrise",
        end_offset=0,
        scheduled_days=[0, 1],
        fade_time=2,
        night_reduction=night_reduction,
    )


async def test_update_schedule_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_UPDATE_SCHEDULE}" in hass.services._handlers


async def test_update_schedule_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_UPDATE_SCHEDULE}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_UPDATE_SCHEDULE}" in hass.services._handlers


async def test_update_schedule_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    update_schedule = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_update_schedule", update_schedule)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_UPDATE_SCHEDULE}"]
    call = types.SimpleNamespace(
        data={
            "schedule_id": "te1",
            "title": "Renamed",
            "scene_steps": [_STEP],
            "start_event": "sunrise",
            "start_offset": -30,
            "end_event": "sunset",
            "end_offset": 30,
            "scheduled_days": [5, 6],
            "fade_time": 1,
            "activated": False,
            "night_reduction": None,
        }
    )
    await handler(call)

    update_schedule.assert_awaited_once_with(
        hass,
        entry,
        schedule_id="te1",
        title="Renamed",
        scene_steps=[_STEP],
        start_event="sunrise",
        start_offset=-30,
        end_event="sunset",
        end_offset=30,
        scheduled_days=[5, 6],
        fade_time=1,
        activated=False,
        night_reduction=None,
    )


async def test_update_schedule_service_forwards_defaults(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    update_schedule = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_update_schedule", update_schedule)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_UPDATE_SCHEDULE}"]
    await handler(types.SimpleNamespace(data={"schedule_id": "te1"}))

    update_schedule.assert_awaited_once_with(
        hass,
        entry,
        schedule_id="te1",
        title=None,
        scene_steps=None,
        start_event=None,
        start_offset=None,
        end_event=None,
        end_offset=None,
        scheduled_days=None,
        fade_time=None,
        activated=None,
        night_reduction=None,
    )


async def test_remove_schedule_service_is_registered_on_setup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_REMOVE_SCHEDULE}" in hass.services._handlers


async def test_remove_schedule_service_survives_entry_unload_cleanup(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert f"plejd_cloud.{SERVICE_REMOVE_SCHEDULE}" in hass.services._handlers
    for unload in unloads:
        await _run_unload(unload)
    assert f"plejd_cloud.{SERVICE_REMOVE_SCHEDULE}" in hass.services._handlers


async def test_remove_schedule_service_forwards_call_data(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()

    remove_schedule = AsyncMock()
    monkeypatch.setattr(plejd_cloud, "async_remove_schedule", remove_schedule)

    await async_setup_entry(hass, entry)
    handler = hass.services._handlers[f"plejd_cloud.{SERVICE_REMOVE_SCHEDULE}"]
    await handler(types.SimpleNamespace(data={"schedule_id": "te1"}))

    remove_schedule.assert_awaited_once_with(hass, entry, schedule_id="te1")


# ── Dashboard panel ───────────────────────────────────────────────────────────


async def test_setup_registers_panel_by_default(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    calls = []

    async def _reg(hass):
        calls.append(hass)

    monkeypatch.setattr(plejd_cloud.panel, "async_register_panel", _reg)
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert calls == [hass]  # dashboard registered by default


async def test_setup_skips_panel_when_disabled(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    calls = []

    async def _reg(hass):
        calls.append(hass)

    monkeypatch.setattr(plejd_cloud.panel, "async_register_panel", _reg)
    hass, entry = _hass(), _entry()
    entry.options = {"show_panel": False}
    await async_setup_entry(hass, entry)
    assert calls == []  # hidden → not registered


async def test_unload_removes_panel(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    monkeypatch.setattr(plejd_cloud.panel, "async_register_panel", _noop_async)
    removed = []
    monkeypatch.setattr(plejd_cloud.panel, "async_unregister_panel", lambda hass: removed.append(hass))
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    for unload in unloads:
        await _run_unload(unload)
    assert removed == [hass]  # sidebar entry removed on unload/reload


async def _noop_async(hass):
    return None


async def test_setup_survives_panel_registration_failure(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()

    async def _boom(hass):
        raise ValueError("panel url path already taken")

    monkeypatch.setattr(plejd_cloud.panel, "async_register_panel", _boom)
    hass, entry = _hass(), _entry()
    # A url-path clash must NOT abort setup — the mesh/lights still load.
    assert await async_setup_entry(hass, entry) is True
    assert entry.runtime_data.started is True


# ── Dim bindings wiring ───────────────────────────────────────────────────────


async def test_setup_loads_bindings_and_registers_ws(monkeypatch):
    from plejd_cloud.dim_binding_ws import DATA_BINDINGS

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert DATA_BINDINGS in hass.data  # bindings manager available for the WS API
    assert "ws_commands" in hass.data  # WS commands registered


async def test_unload_cleans_up_bindings(monkeypatch):
    from plejd_cloud.dim_binding_ws import DATA_BINDINGS

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert DATA_BINDINGS in hass.data
    for unload in unloads:
        await _run_unload(unload)
    assert DATA_BINDINGS not in hass.data


# ── Schedules wiring ─────────────────────────────────────────────────────────


async def test_setup_stores_entry_and_registers_schedule_ws(monkeypatch):
    from plejd_cloud.schedule_ws import DATA_ENTRY, ws_add, ws_delete, ws_list

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert hass.data[DATA_ENTRY] is entry  # entry available for the schedule WS API
    registered = hass.data["ws_commands"]
    assert ws_list in registered and ws_add in registered and ws_delete in registered


async def test_unload_cleans_up_schedule_entry(monkeypatch):
    from plejd_cloud.schedule_ws import DATA_ENTRY

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert DATA_ENTRY in hass.data
    for unload in unloads:
        await _run_unload(unload)
    assert DATA_ENTRY not in hass.data


async def test_setup_survives_binding_load_failure(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()

    class _BadBindings:
        def __init__(self, hass):
            pass

        async def async_load(self):
            raise ValueError("corrupt store")

        def shutdown(self):
            return None

    monkeypatch.setattr(plejd_cloud, "PlejdDimBindings", _BadBindings)
    hass, entry = _hass(), _entry()
    assert await async_setup_entry(hass, entry) is True  # storage error must not abort setup


# ── Holiday mode wiring ────────────────────────────────────────────────────────


async def test_setup_registers_holiday_mode(monkeypatch):
    from plejd_cloud.holiday_mode import DATA_HOLIDAY_MODE

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    monkeypatch.setattr(plejd_cloud, "PlejdHolidayMode", _FakeHolidayMode)
    _FakeCoordinator.instances.clear()
    _FakeHolidayMode.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert hass.data[DATA_HOLIDAY_MODE] is _FakeHolidayMode.instances[-1]


async def test_unload_stops_holiday_mode_before_unloading_platforms(monkeypatch):
    from plejd_cloud.holiday_mode import DATA_HOLIDAY_MODE

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    monkeypatch.setattr(plejd_cloud, "PlejdHolidayMode", _FakeHolidayMode)
    _FakeCoordinator.instances.clear()
    _FakeHolidayMode.instances.clear()
    _FakeHolidayMode.call_order.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert DATA_HOLIDAY_MODE in hass.data

    assert await async_unload_entry(hass, entry) is True

    assert DATA_HOLIDAY_MODE not in hass.data
    assert _FakeHolidayMode.instances[-1].stopped is True
    # Stopping (and turning off any lights it owns) must happen before the light platform
    # and the mesh connection go away, or the cleanup would target entities that no
    # longer exist (#89 review).
    assert _FakeHolidayMode.call_order == ["holiday_mode.async_stop", "async_unload_platforms"]


async def test_unload_without_holiday_mode_registered_is_a_noop(monkeypatch):
    # Setup failed before holiday mode was constructed (or it was already popped) —
    # unload must tolerate a missing DATA_HOLIDAY_MODE entry.
    entry = _entry()
    entry.runtime_data = _FakeCoordinator(None, entry)
    hass = _hass()
    assert await async_unload_entry(hass, entry) is True


async def test_unload_failure_keeps_holiday_mode_registered_for_a_retry(monkeypatch):
    from plejd_cloud.holiday_mode import DATA_HOLIDAY_MODE

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    monkeypatch.setattr(plejd_cloud, "PlejdHolidayMode", _FakeHolidayMode)
    _FakeCoordinator.instances.clear()
    _FakeHolidayMode.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    await hass.data[DATA_HOLIDAY_MODE].async_start()  # the holiday switch was on before unload
    hass.config_entries.unload_result = False  # some other platform refuses to unload

    assert await async_unload_entry(hass, entry) is False

    # Still stopped/cleaned up (it's this integration's own resource to own)...
    assert _FakeHolidayMode.instances[-1].stopped is True
    # ...but not removed from hass.data: the entry stays loaded, and a later unload
    # retry must still be able to find it (#89 review).
    assert DATA_HOLIDAY_MODE in hass.data
    # ...and resumed, because it was actually running before the stop above (#89 review).
    assert _FakeHolidayMode.instances[-1].started is True


async def test_activity_log_is_stopped_only_by_a_successful_unload(monkeypatch):
    from plejd_cloud import activity

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    monkeypatch.setattr(plejd_cloud, "PlejdHolidayMode", _FakeHolidayMode)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    log = hass.data[activity.DATA_ACTIVITY]
    for unload in unloads:  # older HA runs these even when the unload is refused
        await _run_unload(unload)
    hass.config_entries.unload_result = False
    assert await async_unload_entry(hass, entry) is False
    assert hass.data[activity.DATA_ACTIVITY] is log and log._unsubs  # still logging
    assert log._entry.runtime_data is not None  # and still reaching the coordinator, without hass.data
    hass.config_entries.unload_result = True
    assert await async_unload_entry(hass, entry) is True
    assert activity.DATA_ACTIVITY not in hass.data and not log._unsubs


async def test_a_failed_activity_save_on_unload_still_shuts_the_coordinator_down(monkeypatch):
    from plejd_cloud import activity

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    monkeypatch.setattr(plejd_cloud, "PlejdHolidayMode", _FakeHolidayMode)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)

    async def _disk_full():
        raise OSError("disk full")

    monkeypatch.setattr(hass.data[activity.DATA_ACTIVITY], "async_stop", _disk_full)
    assert await async_unload_entry(hass, entry) is True
    assert entry.runtime_data.shutdown is True


async def test_unload_failure_does_not_resume_holiday_mode_that_was_already_off(monkeypatch):
    from plejd_cloud.holiday_mode import DATA_HOLIDAY_MODE

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    monkeypatch.setattr(plejd_cloud, "PlejdHolidayMode", _FakeHolidayMode)
    _FakeCoordinator.instances.clear()
    _FakeHolidayMode.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)  # holiday switch was off: async_start() never ran
    hass.config_entries.unload_result = False  # some other platform refuses to unload

    assert await async_unload_entry(hass, entry) is False

    # Still stopped (a no-op, since it was never running) and left registered for a retry...
    assert _FakeHolidayMode.instances[-1].stopped is True
    assert DATA_HOLIDAY_MODE in hass.data
    # ...but NOT resumed: it wasn't running before the veto, so resuming it would start a
    # hidden timer behind a switch entity that still reads off (#89 review).
    assert _FakeHolidayMode.instances[-1].started is False


# ── Remote profiles wiring ──────────────────────────────────────────────────────


async def test_setup_loads_remote_profiles_and_registers_ws(monkeypatch):
    from plejd_cloud.remote_profile_ws import DATA_REMOTE_PROFILES

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert DATA_REMOTE_PROFILES in hass.data  # profiles manager available for the WS API
    assert "ws_commands" in hass.data


async def test_unload_cleans_up_remote_profiles(monkeypatch):
    from plejd_cloud.remote_profile_ws import DATA_REMOTE_PROFILES

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()
    hass, entry = _hass(), _entry()
    unloads: list = []
    entry.async_on_unload = unloads.append
    await async_setup_entry(hass, entry)
    assert DATA_REMOTE_PROFILES in hass.data
    for unload in unloads:
        await _run_unload(unload)
    assert DATA_REMOTE_PROFILES not in hass.data


async def test_setup_survives_remote_profile_load_failure(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()

    class _BadProfiles:
        def __init__(self, hass):
            pass

        async def async_load(self):
            raise ValueError("corrupt store")

    monkeypatch.setattr(plejd_cloud, "PlejdRemoteProfiles", _BadProfiles)
    hass, entry = _hass(), _entry()
    assert await async_setup_entry(hass, entry) is True  # storage error must not abort setup


async def test_setup_survives_activity_log_load_failure(monkeypatch):
    from plejd_cloud import activity

    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    _FakeCoordinator.instances.clear()

    async def _corrupt(self):
        raise ValueError("corrupt store")

    monkeypatch.setattr(activity.PlejdActivityLog, "async_load", _corrupt)
    hass, entry = _hass(), _entry()
    assert await async_setup_entry(hass, entry) is True  # storage error must not abort setup
    assert hass.data[activity.DATA_ACTIVITY].entries == []  # starts empty, still listening


async def test_remove_entry_keeps_cleaning_up_when_the_log_cannot_be_deleted(monkeypatch):
    from plejd_cloud import activity

    async def _denied(hass, entry_id):
        raise PermissionError("read-only")

    monkeypatch.setattr(activity, "async_remove_store", _denied)
    cleared = []
    monkeypatch.setattr(
        plejd_cloud, "async_clear_malformed_site_issue", lambda hass, entry_id: cleared.append(entry_id)
    )
    await plejd_cloud.async_remove_entry(_hass(), _entry())
    assert cleared  # the rest of the removal cleanup still ran


async def test_remove_entry_clears_the_persistent_repair_issue():
    # The malformed-cloud issue is persistent, and its only other clear paths (a healthy
    # poll, a successful reconfigure) are unreachable once the entry is gone - so without
    # this the user keeps an orphaned warning about an integration they removed.
    from plejd_cloud import async_remove_entry
    from plejd_cloud.coordinator import DATA_LAST_SELF_HEAL

    hass, entry = _hass(), _entry()
    hass.created_issues = {f"malformed_cloud_site_{entry.entry_id}": {"domain": "plejd_cloud"}}
    hass.data[DATA_LAST_SELF_HEAL] = {entry.entry_id: 1_000.0}

    await async_remove_entry(hass, entry)

    assert f"malformed_cloud_site_{entry.entry_id}" not in hass.created_issues
    assert entry.entry_id not in hass.data[DATA_LAST_SELF_HEAL]


async def test_setup_syncs_areas_only_when_enabled(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    calls = []

    async def _sync(hass, entry):
        assert "area_registry_updated" in [e for e, _ in hass.bus.listeners]  # subscribed before syncing
        calls.append(entry.entry_id)

    monkeypatch.setattr(plejd_cloud.area_sync, "async_sync_areas", _sync)
    hass, entry = _hass(), _entry()
    await async_setup_entry(hass, entry)
    assert calls == []

    entry.options = {"sync_areas": True}
    hass = _hass()
    await async_setup_entry(hass, entry)
    assert calls == ["e1"]
    assert "area_registry_updated" in [e for e, _ in hass.bus.listeners]


async def test_setup_survives_area_sync_failure(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)

    async def _boom(hass, entry):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(plejd_cloud.area_sync, "async_sync_areas", _boom)
    entry = _entry()
    entry.options = {"sync_areas": True}
    assert await async_setup_entry(_hass(), entry) is True


async def test_remove_entry_deletes_the_area_sync_store():
    from plejd_cloud import async_remove_entry

    hass, entry = _hass(), _entry()
    hass.data[("store", "plejd_cloud.area_sync.e1")] = {"DEV": {"room": "r1:Garage", "area": "garage"}}
    await async_remove_entry(hass, entry)
    assert ("store", "plejd_cloud.area_sync.e1") not in hass.data


async def test_setup_with_area_sync_off_clears_its_state(monkeypatch):
    # Clearing on the reload that turns syncing off is what makes re-enabling align every device.
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)
    hass, entry = _hass(), _entry()
    hass.data[("store", "plejd_cloud.area_sync.e1")] = {"DEV": {"room": "r1:Garage", "area": "hall"}}
    await async_setup_entry(hass, entry)
    assert ("store", "plejd_cloud.area_sync.e1") not in hass.data


async def test_setup_survives_area_sync_reset_failure(monkeypatch):
    monkeypatch.setattr(plejd_cloud, "PlejdCoordinator", _FakeCoordinator)

    async def _boom(hass, entry):
        raise OSError("read-only storage")

    monkeypatch.setattr(plejd_cloud.area_sync, "async_reset_areas", _boom)
    assert await async_setup_entry(_hass(), _entry()) is True
