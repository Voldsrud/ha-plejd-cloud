"""Tests for the Plejd button platform (site clock-sync, all off)."""

from __future__ import annotations

import types

from homeassistant.const import EntityCategory
from plejd_cloud.button import PlejdAllOffButton, PlejdSyncClockButton, async_setup_entry


class _Coordinator:
    site_id = "site-1"

    def __init__(self):
        self.syncs = 0
        self.all_off_calls = 0

    async def async_sync_clock(self):
        self.syncs += 1

    async def async_all_off(self):
        self.all_off_calls += 1


async def test_setup_creates_clock_and_all_off_buttons():
    entry = types.SimpleNamespace(runtime_data=_Coordinator())
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 2
    unique_ids = {e._attr_unique_id for e in added}
    assert unique_ids == {"site-1_sync_clock", "site-1_all_off"}
    clock_button = next(e for e in added if e._attr_unique_id == "site-1_sync_clock")
    assert clock_button._attr_entity_category == EntityCategory.CONFIG


async def test_press_syncs_the_clock():
    coord = _Coordinator()
    button = PlejdSyncClockButton(coord)
    await button.async_press()
    assert coord.syncs == 1


async def test_all_off_button_unique_id_and_device():
    coord = _Coordinator()
    button = PlejdAllOffButton(coord)
    assert button._attr_unique_id == "site-1_all_off"
    assert button._attr_device_info["identifiers"] == {("plejd_cloud", "site-1")}


async def test_all_off_button_press_turns_off_every_light():
    coord = _Coordinator()
    button = PlejdAllOffButton(coord)
    await button.async_press()
    assert coord.all_off_calls == 1
