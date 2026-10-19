"""Plejd light platform."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud import PlejdCloudDevice, PlejdCloudRoom
from .const import CATEGORY_LIGHT, DOMAIN, ROOM_DEVICE_ID_PREFIX
from .coordinator import PlejdCoordinator

SERVICE_START_DIM = "start_dim"
SERVICE_STOP_DIM = "stop_dim"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Plejd lights for the config entry."""
    coordinator: PlejdCoordinator = entry.runtime_data
    async_add_entities(
        PlejdLight(coordinator, device)
        for device in coordinator.devices
        if device.category == CATEGORY_LIGHT and device.address is not None
    )
    # One group light per Plejd room: dims the whole room in a single 0x0098 command
    # (the room's group address), the way the app does — no per-output fan-out.
    async_add_entities(PlejdRoomLight(coordinator, room) for room in coordinator.rooms)
    # Remote hold-to-dim, as entity services so HA handles target expansion
    # (entity/device/area = a whole Plejd room) and per-entity permission checks.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_START_DIM, {vol.Required("direction"): vol.In(["up", "down"])}, "async_start_dim"
    )
    platform.async_register_entity_service(SERVICE_STOP_DIM, {}, "async_stop_dim")


class PlejdLight(LightEntity):
    """A Plejd dimmer/relay output exposed as a light."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: PlejdCoordinator, device: PlejdCloudDevice) -> None:
        self._coordinator = coordinator
        self._device = device
        self._attr_unique_id = (
            device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Plejd",
            model=device.model,
        )
        mode = ColorMode.BRIGHTNESS if device.dimmable else ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    @property
    def available(self) -> bool:
        return self._coordinator.available

    @property
    def is_on(self) -> bool | None:
        state = self._coordinator.state_for(self._device.address)
        return state.on if state is not None else None

    @property
    def brightness(self) -> int | None:
        if not self._device.dimmable:
            return None
        state = self._coordinator.state_for(self._device.address)
        return state.level if state is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        level = kwargs.get(ATTR_BRIGHTNESS)
        if level is None:
            # No brightness requested: restore the last level, or full if unknown. A
            # level of 0 is never a real "on" position for a Plejd dimmer (ramps floor
            # at DIM_MIN=1), so it's treated the same as unknown, not commanded as-is.
            current = self.brightness
            level = current if current else 255
        await self._coordinator.async_set_output(self._device.address, True, level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_output(self._device.address, False, 0)

    async def async_start_dim(self, direction: str) -> None:
        """Start a smooth hold-to-dim ramp on this light (entity service)."""
        if not self._device.dimmable:
            return  # on/off outputs can't ramp; nothing to do
        self._coordinator.dim_ramp.start(self._device.address, 1 if direction == "up" else -1)

    async def async_stop_dim(self) -> None:
        """Stop an in-progress ramp on this light, holding its brightness (entity service)."""
        self._coordinator.dim_ramp.stop(self._device.address)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self.async_write_ha_state))


class PlejdRoomLight(LightEntity):
    """A whole Plejd room as one light: on/off/brightness in a single group command."""

    # Commands target the room's group mesh address, so every output in the room responds
    # to one 0x0098 (like the app dims a room) instead of HA fanning an area out to N
    # per-output commands. State is aggregated from the room's member outputs.

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: PlejdCoordinator, room: PlejdCloudRoom) -> None:
        self._coordinator = coordinator
        self._room = room
        self._attr_unique_id = f"{ROOM_DEVICE_ID_PREFIX}{room.room_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{ROOM_DEVICE_ID_PREFIX}{room.room_id}")},
            name=room.name,
            manufacturer="Plejd",
            model="Room",
        )
        mode = ColorMode.BRIGHTNESS if room.dimmable else ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    @property
    def available(self) -> bool:
        return self._coordinator.available

    def _member_states(self, addresses: list[int]) -> list:
        states = (self._coordinator.state_for(addr) for addr in addresses)
        return [s for s in states if s is not None]

    @property
    def is_on(self) -> bool | None:
        states = self._member_states(self._room.member_addresses)
        return any(s.on for s in states) if states else None

    @property
    def brightness(self) -> int | None:
        if not self._room.dimmable:
            return None
        # Only dimmable members' levels count: an on/off-only member's level is
        # meaningless and would skew the displayed brightness of the room's dimmers.
        # A level of 0 is likewise excluded (never a real "on" position, see PlejdLight).
        on_levels = [s.level for s in self._member_states(self._room.dimmable_addresses) if s.on and s.level]
        return round(sum(on_levels) / len(on_levels)) if on_levels else None

    def _restore_level(self) -> int | None:
        """Average of every dimmable member's last-known level (on or off), for an all-off room's turn_on()."""
        levels = [s.level for s in self._member_states(self._room.dimmable_addresses) if s.level]
        return round(sum(levels) / len(levels)) if levels else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        level = kwargs.get(ATTR_BRIGHTNESS)
        if level is None:
            # An on/off-only room has no meaningful "restore level" (HA never sends
            # brightness for ColorMode.ONOFF anyway) -> always command full, like PlejdLight.
            # A partially-lit room must keep its visible brightness (only the all-off restore
            # average is appropriate when nothing is on yet) - otherwise turning "the room" on
            # while it's already partially lit would re-brighten/dim the already-lit members
            # to a blended level nobody asked for (the dim-ramp path already avoids this).
            # A restored/visible level of 0 is likewise treated as unknown (see PlejdLight).
            if self._room.dimmable:
                current = self.brightness if self.is_on else self._restore_level()
            else:
                current = None
            level = current if current else 255
        await self._coordinator.async_set_group_output(self._room.address, True, level, self._room.member_addresses)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_group_output(self._room.address, False, 0, self._room.member_addresses)

    async def async_start_dim(self, direction: str) -> None:
        """Start a smooth hold-to-dim ramp across the room's group address (entity service)."""
        if not self._room.dimmable:
            return  # on/off-only rooms can't ramp; nothing to do
        # Seed from the room's actual visible brightness (on dimmable members only) when
        # lit, not the all-off restore average - otherwise dimming down a partially-lit
        # room could start from a higher level than what's actually showing and brighten
        # the lit output on the first tick.
        current = (bool(self.is_on), self.brightness or 0)
        self._coordinator.dim_ramp.start(
            self._room.address,
            1 if direction == "up" else -1,
            current=current,
            member_addresses=self._room.member_addresses,
        )

    async def async_stop_dim(self) -> None:
        """Stop an in-progress ramp on this room, holding its brightness (entity service)."""
        self._coordinator.dim_ramp.stop(self._room.address)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self.async_write_ha_state))
