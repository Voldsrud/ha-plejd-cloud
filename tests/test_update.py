"""Tests for the Plejd firmware update entity."""

from __future__ import annotations

import types

from plejd_cloud.cloud import PlejdCloudDevice, PlejdCloudMotion
from plejd_cloud.coordinator import PlejdFirmwareStatus
from plejd_cloud.update import UPDATE_WARNING, PlejdFirmwareUpdate, async_setup_entry


def _device(device_id="d1", output_index=0):
    return PlejdCloudDevice(
        device_id=device_id,
        name="Kitchen",
        address=5,
        output_index=output_index,
        outputs=[5],
        hardware_id=1,
        model="DIM-01",
        category="light",
        dimmable=True,
        traits=3,
        room_id="r1",
    )


class _Coordinator:
    def __init__(self, devices, motion=None, gateways=None, firmware=None):
        self.devices = devices
        self.motion = motion or []
        self.gateways = gateways or []
        self.firmware = firmware or {}
        self.listeners = []

    def async_add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)


def _entity(coord, device_id="d1"):
    return PlejdFirmwareUpdate(coord, device_id, "Kitchen", "DIM-01")


async def test_setup_covers_outputs_sensors_and_gateways():
    coord = _Coordinator(
        [_device("d1", 0), _device("d1", 1), _device("d2", 0)],
        motion=[PlejdCloudMotion(device_id="w1", name="Motion sensor", address=33)],
        gateways=["gw1"],
    )
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    # d1's two outputs collapse to one entity; d2, the WMS-01 sensor, and the gateway each get one
    assert {e._attr_unique_id for e in added} == {"firmware_d1", "firmware_d2", "firmware_w1", "firmware_gw1"}


def test_installed_version_unknown_before_refresh():
    entity = _entity(_Coordinator([_device()]))
    assert entity.installed_version is None
    assert entity.latest_version is None


def test_versions_when_up_to_date():
    status = PlejdFirmwareStatus("6.43.3", 20260324155701, None, None)
    entity = _entity(_Coordinator([_device()], firmware={"d1": status}))
    assert entity.installed_version == "6.43.3"
    assert entity.latest_version == "6.43.3"  # equal -> HA shows up to date


def test_versions_when_update_available():
    status = PlejdFirmwareStatus("6.40.0", 20251201000000, "6.43.3", 20260324155701)
    entity = _entity(_Coordinator([_device()], firmware={"d1": status}))
    assert entity.installed_version == "6.40.0"
    assert entity.latest_version == "6.43.3"


def test_latest_ignores_older_build_even_with_higher_version_string():
    # latest_version string would sort higher, but buildTime is older -> stay on installed
    status = PlejdFirmwareStatus("6.43.3", 20260324155701, "6.99.0", 20200101000000)
    entity = _entity(_Coordinator([_device()], firmware={"d1": status}))
    assert entity.latest_version == "6.43.3"


def test_equal_build_time_is_up_to_date():
    # boundary: latest IS present and exactly equals installed -> no update (strict >, not >=)
    status = PlejdFirmwareStatus("6.43.3", 20260324155701, "6.43.3", 20260324155701)
    assert status.update_available is False
    entity = _entity(_Coordinator([_device()], firmware={"d1": status}))
    assert entity.latest_version == "6.43.3"


def test_release_summary_warns_to_use_the_app():
    entity = _entity(_Coordinator([_device()]))
    assert entity.release_summary is UPDATE_WARNING
    assert "Plejd app" in UPDATE_WARNING


async def test_added_to_hass_subscribes_to_coordinator():
    coord = _Coordinator([_device()])
    entity = _entity(coord)
    await entity.async_added_to_hass()
    assert len(coord.listeners) == 1
