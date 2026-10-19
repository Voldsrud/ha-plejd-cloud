"""Tests for the Plejd motion binary_sensor."""

from __future__ import annotations

import types

from plejd_cloud.binary_sensor import PlejdMotionBinarySensor, PlejdProblemBinarySensor, async_setup_entry
from plejd_cloud.cloud import PlejdCloudDevice, PlejdCloudInput, PlejdCloudMotion
from plejd_cloud.protocol import MotionEvent


def _device(device_id="d1", address=5, output_index=0):
    return PlejdCloudDevice(
        device_id=device_id,
        name="Lamp",
        address=address,
        output_index=output_index,
        outputs=[address] if address is not None else [],
        hardware_id=1,
        model="DIM-01",
        category="light",
        dimmable=True,
        traits=3,
        room_id="r1",
    )


class _Coordinator:
    def __init__(self, motion, devices=(), gateways=(), inputs=(), device_addresses=None):
        self.motion = motion
        self.devices = list(devices)
        self.gateways = list(gateways)
        self.inputs = list(inputs)
        # default: assume each device's own output address is also its physical address,
        # unless a test explicitly overrides this to exercise a mismatch.
        self._device_addresses = (
            device_addresses if device_addresses is not None else {d.device_id: d.address for d in self.devices}
        )
        self.motion_listeners = []
        self.fault_listeners = []
        self._faults = {}

    def async_add_motion_listener(self, cb):
        self.motion_listeners.append(cb)
        return lambda: self.motion_listeners.remove(cb)

    def async_add_fault_listener(self, cb):
        self.fault_listeners.append(cb)
        return lambda: self.fault_listeners.remove(cb)

    def faults_for(self, address):
        return self._faults.get(address, frozenset())

    def device_address_for(self, device_id):
        return self._device_addresses.get(device_id)


def _sensor():
    s = PlejdMotionBinarySensor(_Coordinator([]), PlejdCloudMotion("w1", "Motion", 33))
    s.hass = None
    return s


async def test_setup_creates_motion_sensor():
    coord = _Coordinator([PlejdCloudMotion("w1", "Motion", 33)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    # a motion sensor also gets its own fault (problem) sensor — it has no output,
    # but is still a physical device that reports NotifyEvents.
    assert len(added) == 2
    assert {type(e) for e in added} == {PlejdMotionBinarySensor, PlejdProblemBinarySensor}


def test_motion_on_then_clear():
    s = _sensor()
    assert s._attr_is_on is False
    s._handle(MotionEvent(33, True, 5))
    assert s._attr_is_on is True
    s._handle(MotionEvent(33, True, 6))  # re-trigger cancels the previous timer
    s._clear(None)
    assert s._attr_is_on is False


def test_motion_ignores_other_address_and_non_motion():
    s = _sensor()
    s._handle(MotionEvent(99, True, 5))
    s._handle(MotionEvent(33, False, 5))
    assert s._attr_is_on is False


def test_attributes():
    s = _sensor()
    assert s._attr_unique_id == "motion_w1" and s._attr_device_info["model"] == "WMS-01"


async def test_added_to_hass_subscribes():
    coord = _Coordinator([PlejdCloudMotion("w1", "M", 33)])
    await PlejdMotionBinarySensor(coord, coord.motion[0]).async_added_to_hass()
    assert len(coord.motion_listeners) == 1


async def test_setup_creates_one_problem_sensor_per_device():
    devices = [_device("d1", 5), _device("d1", 5, output_index=1), _device("d2", 7)]  # d1 shares device_id
    coord = _Coordinator([PlejdCloudMotion("w1", "M", 33)], devices=devices)
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    problems = [e for e in added if isinstance(e, PlejdProblemBinarySensor)]
    # one per physical device (d1, d2), not per output, plus the motion sensor (w1)
    assert len(problems) == 3
    assert {p._attr_unique_id for p in problems} == {"fault_d1", "fault_d2", "fault_w1"}


async def test_setup_uses_physical_address_not_output_address():
    """A multi-output device's fault sensor must poll/listen on its physical address."""
    device = _device("d1", address=11)  # output address 11 ...
    coord = _Coordinator([], devices=[device], device_addresses={"d1": 5})  # ... but physical address is 5
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 1
    assert added[0]._address == 5


async def test_setup_creates_fault_sensor_for_input_only_device():
    """A wall-switch/input device with no output still gets a fault sensor, using its
    physical address (device_address_for), not any single button's own address."""
    buttons = [PlejdCloudInput("btn1", "Switch", 11), PlejdCloudInput("btn1", "Switch", 12)]  # 2-button WPH-01
    coord = _Coordinator([], inputs=buttons, device_addresses={"btn1": 20})
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 1  # deduped by device_id, not one per button channel
    assert added[0]._attr_unique_id == "fault_btn1" and added[0]._address == 20
    assert added[0]._attr_device_info.get("model") is None  # no known hardware model for inputs


async def test_setup_skips_input_device_with_unresolved_address():
    coord = _Coordinator([], inputs=[PlejdCloudInput("btn1", "Switch", 11)], device_addresses={})
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert added == []


async def test_setup_creates_gateway_fault_sensor():
    coord = _Coordinator([], gateways=["gw1"], device_addresses={"gw1": 9})
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 1
    assert added[0]._attr_unique_id == "fault_gw1" and added[0]._address == 9


async def test_setup_skips_a_gateway_id_already_covered():
    # defensive: a gateway_id that coincides with an already-seen device_id/sensor
    # (shouldn't happen in real Plejd data, but must not double-add an entity).
    coord = _Coordinator([PlejdCloudMotion("w1", "M", 33)], gateways=["w1"], device_addresses={"w1": 33})
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    problems = [e for e in added if isinstance(e, PlejdProblemBinarySensor)]
    assert len(problems) == 1  # not duplicated for the gateway pass


async def test_setup_skips_devices_with_unresolved_address():
    coord = _Coordinator([], devices=[_device("d1", 5)], gateways=["gw1"], device_addresses={})
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert added == []  # neither d1 nor gw1 has a resolvable physical address


def test_problem_sensor_reflects_faults():
    coord = _Coordinator([], devices=[_device("d1", 5)])
    s = PlejdProblemBinarySensor(coord, "d1", 5, "Lamp", "DIM-01")
    assert s.is_on is False and s.extra_state_attributes == {"active_faults": []}
    coord._faults[5] = frozenset({"overtemperature", "hard_fault"})
    assert s.is_on is True
    assert s.extra_state_attributes == {"active_faults": ["hard_fault", "overtemperature"]}


async def test_problem_sensor_subscribes_and_updates_on_match():
    coord = _Coordinator([], devices=[_device("d1", 5)])
    s = PlejdProblemBinarySensor(coord, "d1", 5, "Lamp", "DIM-01")
    writes = []
    s.async_write_ha_state = lambda: writes.append(1)
    await s.async_added_to_hass()
    assert len(coord.fault_listeners) == 1
    coord.fault_listeners[0](5, frozenset({"overtemperature"}))  # matching address -> writes
    coord.fault_listeners[0](99, frozenset({"x"}))  # other address -> ignored
    assert writes == [1]
