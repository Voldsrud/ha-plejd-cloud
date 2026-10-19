"""Tests for the Plejd illuminance sensor."""

from __future__ import annotations

import types

from plejd_cloud.cloud import PlejdCloudMotion
from plejd_cloud.protocol import MotionEvent
from plejd_cloud.sensor import PlejdConnectivitySensor, PlejdIlluminanceSensor, async_setup_entry


class _Coordinator:
    def __init__(self, motion, active_transport="ble"):
        self.motion = motion
        self.motion_listeners = []
        self.listeners = []
        self.site_id = "site-1"
        self.active_transport = active_transport

    def async_add_motion_listener(self, cb):
        self.motion_listeners.append(cb)
        return lambda: self.motion_listeners.remove(cb)

    def async_add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)


def _sensor():
    return PlejdIlluminanceSensor(_Coordinator([]), PlejdCloudMotion("w1", "Motion", 33))


async def test_setup_creates_connection_and_illuminance_sensors():
    coord = _Coordinator([PlejdCloudMotion("w1", "Motion", 33)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 2  # one connection sensor + one illuminance sensor
    assert any(isinstance(s, PlejdConnectivitySensor) for s in added)
    assert any(isinstance(s, PlejdIlluminanceSensor) for s in added)


def test_connectivity_reports_active_transport():
    assert PlejdConnectivitySensor(_Coordinator([], active_transport="gateway")).native_value == "gateway"


def test_connectivity_reports_disconnected_when_none():
    assert PlejdConnectivitySensor(_Coordinator([], active_transport=None)).native_value == "disconnected"


def test_connectivity_unique_id():
    assert PlejdConnectivitySensor(_Coordinator([]))._attr_unique_id == "connection_site-1"


async def test_connectivity_subscribes_to_updates():
    coord = _Coordinator([])
    await PlejdConnectivitySensor(coord).async_added_to_hass()
    assert len(coord.listeners) == 1


def test_lux_updates_on_matching_address():
    s = _sensor()
    s._handle(MotionEvent(33, True, 42))
    assert s._attr_native_value == 42


def test_lux_ignores_other_address_or_missing_lux():
    s = _sensor()
    s._handle(MotionEvent(99, True, 5))
    s._handle(MotionEvent(33, True, None))
    assert not hasattr(s, "_attr_native_value")


def test_attributes():
    s = _sensor()
    assert s._attr_unique_id == "illuminance_w1"
    assert s._attr_native_unit_of_measurement == "lx"


async def test_added_to_hass_subscribes():
    coord = _Coordinator([PlejdCloudMotion("w1", "M", 33)])
    await PlejdIlluminanceSensor(coord, coord.motion[0]).async_added_to_hass()
    assert len(coord.motion_listeners) == 1
