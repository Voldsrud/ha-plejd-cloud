"""Plejd select platform — per-output dimmer tuning (curve, phase-dim edge, boot state).

Device *settings*, not live state. Settings are read back over BLE after connect;
entities fall back to restore-state when the read hasn't arrived yet.
Wire values are byte-exact, decoded from the app (LoadCurve / PhaseOutputType / BootState).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .cloud import PlejdCloudDevice
from .const import (
    BOOT_STATE_OPTIONS,
    CATEGORY_LIGHT,
    CATEGORY_SWITCH,
    CURVE_OPTIONS,
    DOMAIN,
    PHASE_DIM_HARDWARE,
    PHASE_DIM_OPTIONS,
    RELAY_CONFIG_HARDWARE,
    RELAY_POLE_OPTIONS,
)
from .coordinator import PlejdCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Plejd dimmer-tuning selects (curve + phase edge + boot state + relay pole)."""
    coordinator: PlejdCoordinator = entry.runtime_data
    entities: list[SelectEntity] = []
    for device in coordinator.devices:
        if device.address is None:
            continue
        if device.category == CATEGORY_LIGHT and device.dimmable:
            entities.append(PlejdOutputSettingSelect(coordinator, device, "curve"))
            # Phase-edge only matters on real phase-cut dimmers, not LED-driver/DALI loads.
            if device.hardware_id in PHASE_DIM_HARDWARE:
                entities.append(PlejdOutputSettingSelect(coordinator, device, "phase"))
        if device.category in (CATEGORY_LIGHT, CATEGORY_SWITCH):
            entities.append(PlejdBootStateSelect(coordinator, device))
        if device.hardware_id in RELAY_CONFIG_HARDWARE:
            entities.append(PlejdRelayPoleSelect(coordinator, device))
    async_add_entities(entities)


class PlejdOutputSettingSelect(SelectEntity, RestoreEntity):
    """A dimmer's dimming curve or phase-dim edge, as a config select."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PlejdCoordinator, device: PlejdCloudDevice, kind: str) -> None:
        self._coordinator = coordinator
        self._device = device
        self._kind = kind
        if kind == "curve":
            self._options_map = CURVE_OPTIONS
            self._setter: Callable[[int, int, int], Awaitable[None]] = coordinator.async_set_output_curve
            self._attr_translation_key = "dim_curve"
        else:
            self._options_map = PHASE_DIM_OPTIONS
            self._setter = coordinator.async_set_output_phase_dim
            self._attr_translation_key = "phase_dim"
        self._attr_options = list(self._options_map)
        base = device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
        self._attr_unique_id = f"{base}_{kind}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Plejd",
            model=device.model,
        )

    async def async_select_option(self, option: str) -> None:
        await self._setter(self._device.address, self._device.output_index, self._options_map[option])
        self._attr_current_option = option
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_coordinator_update))
        settings = self._coordinator.settings_for(self._device.address)
        raw = (settings.curve if self._kind == "curve" else settings.phase_dim) if settings else None
        if raw is None:
            last = await self.async_get_last_state()
            if last is not None and last.state in self._options_map:
                self._attr_current_option = last.state
            settings = self._coordinator.settings_for(self._device.address)
            raw = (settings.curve if self._kind == "curve" else settings.phase_dim) if settings else None
        # Reverse-map the raw byte to the option key.
        inv = {v: k for k, v in self._options_map.items()}
        if raw is not None and raw in inv:
            self._attr_current_option = inv[raw]

    @callback
    def _handle_coordinator_update(self) -> None:
        settings = self._coordinator.settings_for(self._device.address)
        if settings is None:
            return
        raw = settings.curve if self._kind == "curve" else settings.phase_dim
        inv = {v: k for k, v in self._options_map.items()}
        if raw is not None and raw in inv:
            option = inv[raw]
            if option != getattr(self, "_attr_current_option", None):
                self._attr_current_option = option
                self.async_write_ha_state()


class PlejdBootStateSelect(SelectEntity, RestoreEntity):
    """After-power-outage boot state for a Plejd output (restore last / boot off)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "boot_state"
    _attr_options = list(BOOT_STATE_OPTIONS)

    def __init__(self, coordinator: PlejdCoordinator, device: PlejdCloudDevice) -> None:
        self._coordinator = coordinator
        self._device = device
        base = device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
        self._attr_unique_id = f"{base}_boot_state"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Plejd",
            model=device.model,
        )

    async def async_select_option(self, option: str) -> None:
        await self._coordinator.async_set_output_boot_state(
            self._device.address, self._device.output_index, BOOT_STATE_OPTIONS[option]
        )
        self._attr_current_option = option
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_coordinator_update))
        settings = self._coordinator.settings_for(self._device.address)
        raw = settings.boot_state if settings else None
        if raw is None:
            last = await self.async_get_last_state()
            if last is not None and last.state in BOOT_STATE_OPTIONS:
                self._attr_current_option = last.state
            settings = self._coordinator.settings_for(self._device.address)
            raw = settings.boot_state if settings else None
        inv = {v: k for k, v in BOOT_STATE_OPTIONS.items()}
        if raw is not None and raw in inv:
            self._attr_current_option = inv[raw]

    @callback
    def _handle_coordinator_update(self) -> None:
        settings = self._coordinator.settings_for(self._device.address)
        if settings is None:
            return
        inv = {v: k for k, v in BOOT_STATE_OPTIONS.items()}
        if settings.boot_state is not None and settings.boot_state in inv:
            option = inv[settings.boot_state]
            if option != getattr(self, "_attr_current_option", None):
                self._attr_current_option = option
                self.async_write_ha_state()


class PlejdRelayPoleSelect(SelectEntity, RestoreEntity):
    """Relay pole wiring for a DIM-01-2P or DIM-01-2P-LC2 (one-pole vs two-pole)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "relay_pole_config"
    _attr_options = list(RELAY_POLE_OPTIONS)

    def __init__(self, coordinator: PlejdCoordinator, device: PlejdCloudDevice) -> None:
        self._coordinator = coordinator
        self._device = device
        base = device.device_id if device.output_index == 0 else f"{device.device_id}_{device.output_index}"
        self._attr_unique_id = f"{base}_relay_pole_config"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="Plejd",
            model=device.model,
        )

    async def async_select_option(self, option: str) -> None:
        await self._coordinator.async_set_output_relay_config(
            self._device.address, self._device.output_index, RELAY_POLE_OPTIONS[option]
        )
        self._attr_current_option = option
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_coordinator_update))
        settings = self._coordinator.settings_for(self._device.address)
        raw = settings.relay_pole_config if settings else None
        if raw is None:
            last = await self.async_get_last_state()
            if last is not None and last.state in RELAY_POLE_OPTIONS:
                self._attr_current_option = last.state
            settings = self._coordinator.settings_for(self._device.address)
            raw = settings.relay_pole_config if settings else None
        inv = {v: k for k, v in RELAY_POLE_OPTIONS.items()}
        if raw is not None and raw in inv:
            self._attr_current_option = inv[raw]

    @callback
    def _handle_coordinator_update(self) -> None:
        settings = self._coordinator.settings_for(self._device.address)
        if settings is None:
            return
        inv = {v: k for k, v in RELAY_POLE_OPTIONS.items()}
        if settings.relay_pole_config is not None and settings.relay_pole_config in inv:
            option = inv[settings.relay_pole_config]
            if option != getattr(self, "_attr_current_option", None):
                self._attr_current_option = option
                self.async_write_ha_state()
