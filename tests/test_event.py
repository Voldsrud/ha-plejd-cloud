"""Tests for the Plejd event (button) platform."""

from __future__ import annotations

import types

from plejd_cloud.cloud import PlejdCloudInput
from plejd_cloud.event import EVENT_PRESS, EVENT_RELEASE, PlejdButtonEvent, async_setup_entry


class _Coordinator:
    def __init__(self, inputs):
        self.inputs = inputs
        self.button_listeners = []

    def async_add_button_listener(self, cb):
        self.button_listeners.append(cb)
        return lambda: self.button_listeners.remove(cb)


async def test_setup_creates_event_per_input():
    coord = _Coordinator([PlejdCloudInput("d1", "Kitchen", 11), PlejdCloudInput("d2", "Hall", 13)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 2


def test_attributes():
    e = PlejdButtonEvent(_Coordinator([]), PlejdCloudInput("d1", "Kitchen", 11))
    assert e._attr_unique_id == "button_d1_11"
    assert e._attr_event_types == [EVENT_PRESS, EVENT_RELEASE]
    assert e._attr_device_info["name"] == "Kitchen"


def test_handle_fires_press_and_release_on_matching_address():
    e = PlejdButtonEvent(_Coordinator([]), PlejdCloudInput("d1", "K", 11))
    e._handle(11, True)
    assert e._last_event == EVENT_PRESS
    e._handle(11, False)
    assert e._last_event == EVENT_RELEASE


def test_handle_ignores_other_address():
    e = PlejdButtonEvent(_Coordinator([]), PlejdCloudInput("d1", "K", 11))
    e._handle(99, True)
    assert not hasattr(e, "_last_event")


async def test_added_to_hass_subscribes():
    coord = _Coordinator([PlejdCloudInput("d1", "K", 11)])
    await PlejdButtonEvent(coord, coord.inputs[0]).async_added_to_hass()
    assert len(coord.button_listeners) == 1
