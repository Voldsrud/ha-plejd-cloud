"""Tests for the Plejd scene platform."""

from __future__ import annotations

import types

from plejd_cloud.cloud import PlejdCloudScene
from plejd_cloud.scene import PlejdScene, async_setup_entry


class _Coordinator:
    def __init__(self, scenes):
        self.scenes = scenes
        self.executed: list[int] = []
        self.available = True

    async def async_execute_scene(self, index):
        self.executed.append(index)


def test_scene_available_follows_coordinator():
    coord = _Coordinator([])
    sc = PlejdScene(coord, PlejdCloudScene("s1", "Movie", 3))
    assert sc.available is True
    coord.available = False
    assert sc.available is False


async def test_setup_creates_scene_entities():
    coord = _Coordinator([PlejdCloudScene("s1", "Movie", 3), PlejdCloudScene("s2", "Night", 4)])
    entry = types.SimpleNamespace(runtime_data=coord)
    added = []
    await async_setup_entry(None, entry, lambda entities: added.extend(entities))
    assert len(added) == 2


async def test_activate_triggers_scene():
    coord = _Coordinator([])
    sc = PlejdScene(coord, PlejdCloudScene("s1", "Movie", 3))
    assert sc._attr_name == "Movie" and sc._attr_unique_id == "scene_s1"
    await sc.async_activate()
    assert coord.executed == [3]
