"""Tests for the Plejd cover platform."""

from __future__ import annotations

import types

from homeassistant.components.cover import ATTR_POSITION, CoverEntityFeature
from plejd_cloud.cloud import PlejdCloudDevice
from plejd_cloud.cover import PlejdCover, async_setup_entry


def _device(category="cover", address=7):
    return PlejdCloudDevice(
        device_id="j1",
        name="Blind",
        address=address,
        output_index=0,
        outputs=[address],
        hardware_id=16,
        model="JAL-01",
        category=category,
        dimmable=False,
        traits=0x10,
        room_id="r1",
    )


class _Coordinator:
    def __init__(self, devices):
        self.devices = devices
        self.positions = []
        self.stops = []

    async def async_set_cover_position(self, address, position):
        self.positions.append((address, position))

    async def async_cover_stop(self, address):
        self.stops.append(address)


async def test_setup_creates_covers_only_for_cover_devices():
    coord = _Coordinator([_device(), _device(category="light"), _device(address=None)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 1


def test_attributes():
    c = PlejdCover(_Coordinator([]), _device())
    assert c._attr_assumed_state is True
    assert c._attr_supported_features == (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP | CoverEntityFeature.SET_POSITION
    )
    assert c._attr_unique_id == "j1" and c.is_closed is None


async def test_open_close_set_stop():
    coord = _Coordinator([])
    c = PlejdCover(coord, _device())
    await c.async_open_cover()
    await c.async_close_cover()
    await c.async_set_cover_position(**{ATTR_POSITION: 40})
    await c.async_stop_cover()
    assert coord.positions == [(7, 100), (7, 0), (7, 40)]
    assert coord.stops == [7]
