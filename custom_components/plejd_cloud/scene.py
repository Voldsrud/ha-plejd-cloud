"""Plejd scene platform."""

from __future__ import annotations

from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import PlejdCloudScene
from .coordinator import PlejdCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Plejd scenes for the config entry."""
    coordinator: PlejdCoordinator = entry.runtime_data
    async_add_entities(PlejdScene(coordinator, scene) for scene in coordinator.scenes)


class PlejdScene(Scene):
    """A Plejd scene, triggered as a mesh broadcast."""

    def __init__(self, coordinator: PlejdCoordinator, scene: PlejdCloudScene) -> None:
        self._coordinator = coordinator
        self._scene = scene
        self._attr_unique_id = f"scene_{scene.scene_id}"
        self._attr_name = scene.name

    @property
    def available(self) -> bool:
        return self._coordinator.available

    async def async_activate(self, **kwargs: Any) -> None:
        await self._coordinator.async_execute_scene(self._scene.index)
