"""Pytest configuration — make the plejd integration importable without the full HA stack.

If `homeassistant` is installed it is used as-is. Otherwise (the lean uv CI env) we
inject the minimal set of HA symbols the skeleton imports, then expose the integration
as the top-level `plejd_cloud` package so tests can `from plejd_cloud... import ...`.
"""

from __future__ import annotations

import enum
import os
import sys
import types

_CC = os.path.join(os.path.dirname(__file__), "..", "custom_components")
sys.path.insert(0, os.path.abspath(_CC))

_CONF = {"CONF_EMAIL": "email", "CONF_PASSWORD": "pass" + "word"}

try:
    import homeassistant.config_entries  # noqa: F401
    import homeassistant.const  # noqa: F401
    import homeassistant.core  # noqa: F401
except ImportError:
    _ha = types.ModuleType("homeassistant")
    sys.modules.setdefault("homeassistant", _ha)

    _const = types.ModuleType("homeassistant.const")
    for _k, _v in _CONF.items():
        setattr(_const, _k, _v)

    _const.ATTR_TEMPERATURE = "temperature"  # type: ignore[attr-defined]

    class _UnitOfTemperature:
        CELSIUS = "\u00b0C"

    _const.UnitOfTemperature = _UnitOfTemperature  # type: ignore[attr-defined]
    _const.LIGHT_LUX = "lx"  # type: ignore[attr-defined]

    class _Platform(str, enum.Enum):
        LIGHT = "light"
        SWITCH = "switch"
        COVER = "cover"
        CLIMATE = "climate"
        BINARY_SENSOR = "binary_sensor"
        SENSOR = "sensor"
        EVENT = "event"
        SCENE = "scene"
        NUMBER = "number"
        SELECT = "select"
        BUTTON = "button"
        UPDATE = "update"

    _const.Platform = _Platform  # type: ignore[attr-defined]
    _const.PERCENTAGE = "%"  # type: ignore[attr-defined]

    class _UnitOfTime:
        SECONDS = "s"

    _const.UnitOfTime = _UnitOfTime  # type: ignore[attr-defined]

    class _EntityCategory(str, enum.Enum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    _const.EntityCategory = _EntityCategory  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.const", _const)

    _core = types.ModuleType("homeassistant.core")

    class _HomeAssistant:
        pass

    def _callback(func):
        return func

    _core.HomeAssistant = _HomeAssistant  # type: ignore[attr-defined]
    _core.callback = _callback  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.core", _core)

    _exc = types.ModuleType("homeassistant.exceptions")

    class _HomeAssistantError(Exception):
        pass

    class _ConfigEntryNotReady(_HomeAssistantError):
        pass

    class _ConfigEntryAuthFailed(_HomeAssistantError):
        pass

    _exc.HomeAssistantError = _HomeAssistantError  # type: ignore[attr-defined]
    _exc.ConfigEntryNotReady = _ConfigEntryNotReady  # type: ignore[attr-defined]
    _exc.ConfigEntryAuthFailed = _ConfigEntryAuthFailed  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.exceptions", _exc)

    _ce = types.ModuleType("homeassistant.config_entries")

    class _ConfigEntry:
        pass

    class _ConfigFlow:
        def __init_subclass__(cls, domain=None, **kwargs):
            super().__init_subclass__(**kwargs)

        async def async_set_unique_id(self, unique_id):
            self.unique_id = unique_id
            return None

        def _abort_if_unique_id_configured(self):
            return None

        def async_create_entry(self, *, title, data):
            return {"type": "create_entry", "title": title, "data": data}

        def async_show_form(self, *, step_id, data_schema=None, errors=None, **kwargs):
            return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors}

        def _get_reauth_entry(self):
            return getattr(self, "_reauth_entry", None)

        def _get_reconfigure_entry(self):
            return getattr(self, "_reconfigure_entry", None)

        def async_update_reload_and_abort(self, entry, *, data_updates=None, reason="reconfigure_successful", **kwargs):
            return {"type": "abort", "reason": reason, "data_updates": data_updates}

    class _OptionsFlow:
        def async_create_entry(self, *, title, data):
            return {"type": "create_entry", "title": title, "data": data}

        def async_show_form(self, *, step_id, data_schema=None, errors=None, **kwargs):
            return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors, **kwargs}

        def async_show_menu(self, *, step_id, menu_options):
            return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    _ce.ConfigEntry = _ConfigEntry  # type: ignore[attr-defined]
    _ce.ConfigFlow = _ConfigFlow  # type: ignore[attr-defined]
    _ce.OptionsFlow = _OptionsFlow  # type: ignore[attr-defined]
    _ce.ConfigFlowResult = dict  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.config_entries", _ce)

    _helpers = types.ModuleType("homeassistant.helpers")
    sys.modules.setdefault("homeassistant.helpers", _helpers)

    _aiohttp = types.ModuleType("homeassistant.helpers.aiohttp_client")

    def _async_get_clientsession(hass):
        return getattr(hass, "session", None)

    _aiohttp.async_get_clientsession = _async_get_clientsession  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.aiohttp_client", _aiohttp)

    _selector = types.ModuleType("homeassistant.helpers.selector")

    class _SelectSelectorConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _SelectSelector:
        def __init__(self, config=None):
            self.config = config

        def __call__(self, value):
            return value

    class _EntitySelectorConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _EntitySelector:
        def __init__(self, config=None):
            self.config = config

        def __call__(self, value):
            return value

    class _TimeSelector:
        def __init__(self, config=None):
            self.config = config

        def __call__(self, value):
            return value

    _selector.SelectSelector = _SelectSelector  # type: ignore[attr-defined]
    _selector.SelectSelectorConfig = _SelectSelectorConfig  # type: ignore[attr-defined]
    _selector.EntitySelector = _EntitySelector  # type: ignore[attr-defined]
    _selector.EntitySelectorConfig = _EntitySelectorConfig  # type: ignore[attr-defined]
    _selector.TimeSelector = _TimeSelector  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.selector", _selector)

    _components = types.ModuleType("homeassistant.components")
    sys.modules.setdefault("homeassistant.components", _components)

    _diag = types.ModuleType("homeassistant.components.diagnostics")

    def _async_redact_data(data, to_redact):
        if isinstance(data, dict):
            return {
                k: ("**REDACTED**" if k in to_redact else _async_redact_data(v, to_redact)) for k, v in data.items()
            }
        if isinstance(data, list):
            return [_async_redact_data(v, to_redact) for v in data]
        return data

    _diag.async_redact_data = _async_redact_data  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.diagnostics", _diag)

    _bt = types.ModuleType("homeassistant.components.bluetooth")

    class _BluetoothServiceInfoBleak:
        pass

    def _async_discovered_service_info(hass, connectable=True):
        return getattr(hass, "service_infos", [])

    def _async_ble_device_from_address(hass, address, connectable=True):
        return getattr(hass, "ble_devices", {}).get(address)

    def _async_scanner_count(hass, connectable=True):
        return getattr(hass, "scanner_count", 1)

    _bt.BluetoothServiceInfoBleak = _BluetoothServiceInfoBleak  # type: ignore[attr-defined]
    _bt.async_discovered_service_info = _async_discovered_service_info  # type: ignore[attr-defined]
    _bt.async_ble_device_from_address = _async_ble_device_from_address  # type: ignore[attr-defined]
    _bt.async_scanner_count = _async_scanner_count  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.bluetooth", _bt)

    _light = types.ModuleType("homeassistant.components.light")
    _light.ATTR_BRIGHTNESS = "brightness"  # type: ignore[attr-defined]

    class _ColorMode(str, enum.Enum):
        ONOFF = "onoff"
        BRIGHTNESS = "brightness"

    class _LightEntity:
        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _light.ColorMode = _ColorMode  # type: ignore[attr-defined]
    _light.LightEntity = _LightEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.light", _light)

    _switch = types.ModuleType("homeassistant.components.switch")

    class _SwitchEntity:
        @property
        def is_on(self):
            return getattr(self, "_attr_is_on", None)

        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _switch.SwitchEntity = _SwitchEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.switch", _switch)

    _scene = types.ModuleType("homeassistant.components.scene")

    class _Scene:
        pass

    _scene.Scene = _Scene  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.scene", _scene)

    _climate = types.ModuleType("homeassistant.components.climate")

    class _HVACMode(str, enum.Enum):
        OFF = "off"
        HEAT = "heat"

    class _HVACAction(str, enum.Enum):
        HEATING = "heating"
        IDLE = "idle"

    class _ClimateEntityFeature(enum.IntFlag):
        TARGET_TEMPERATURE = 1
        PRESET_MODE = 16

    class _ClimateEntity:
        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _climate.HVACMode = _HVACMode  # type: ignore[attr-defined]
    _climate.HVACAction = _HVACAction  # type: ignore[attr-defined]
    _climate.ClimateEntityFeature = _ClimateEntityFeature  # type: ignore[attr-defined]
    _climate.ClimateEntity = _ClimateEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.climate", _climate)

    _event = types.ModuleType("homeassistant.components.event")

    class _EventEntity:
        def _trigger_event(self, event_type, *args):
            self._last_event = event_type

        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _event.EventEntity = _EventEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.event", _event)

    _bsensor = types.ModuleType("homeassistant.components.binary_sensor")

    class _BinarySensorDeviceClass(str, enum.Enum):
        MOTION = "motion"
        OCCUPANCY = "occupancy"
        PROBLEM = "problem"
        DOOR = "door"
        WINDOW = "window"
        GARAGE_DOOR = "garage_door"
        OPENING = "opening"

    class _BinarySensorEntity:
        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _bsensor.BinarySensorDeviceClass = _BinarySensorDeviceClass  # type: ignore[attr-defined]
    _bsensor.BinarySensorEntity = _BinarySensorEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.binary_sensor", _bsensor)

    _sensor = types.ModuleType("homeassistant.components.sensor")

    class _SensorDeviceClass(str, enum.Enum):
        ILLUMINANCE = "illuminance"
        ENUM = "enum"

    class _SensorStateClass(str, enum.Enum):
        MEASUREMENT = "measurement"

    class _SensorEntity:
        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _sensor.SensorDeviceClass = _SensorDeviceClass  # type: ignore[attr-defined]
    _sensor.SensorStateClass = _SensorStateClass  # type: ignore[attr-defined]
    _sensor.SensorEntity = _SensorEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.sensor", _sensor)

    _evt_helper = types.ModuleType("homeassistant.helpers.event")

    def _async_call_later(hass, delay, action):
        return lambda: None

    def _async_track_time_interval(hass, action, interval):
        return lambda: None

    _evt_helper.async_call_later = _async_call_later  # type: ignore[attr-defined]
    _evt_helper.async_track_time_interval = _async_track_time_interval  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.event", _evt_helper)

    _util = types.ModuleType("homeassistant.util")
    _dt = types.ModuleType("homeassistant.util.dt")
    import datetime as _datetime

    def _now():
        # Fixed aware local time (CEST, +02:00) for deterministic clock-sync tests.
        return _datetime.datetime(2026, 5, 31, 12, 0, 0, tzinfo=_datetime.timezone(_datetime.timedelta(hours=2)))

    _dt.now = _now  # type: ignore[attr-defined]
    _dt.as_local = lambda value: value.astimezone(_now().tzinfo)  # type: ignore[attr-defined]
    _util.dt = _dt  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.util", _util)
    sys.modules.setdefault("homeassistant.util.dt", _dt)

    _button = types.ModuleType("homeassistant.components.button")

    class _ButtonEntity:
        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _button.ButtonEntity = _ButtonEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.button", _button)

    _update = types.ModuleType("homeassistant.components.update")

    class _UpdateDeviceClass(str, enum.Enum):
        FIRMWARE = "firmware"

    class _UpdateEntity:
        _attr_release_summary = None

        @property
        def release_summary(self):
            return self._attr_release_summary

        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _update.UpdateDeviceClass = _UpdateDeviceClass  # type: ignore[attr-defined]
    _update.UpdateEntity = _UpdateEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.update", _update)

    _cover = types.ModuleType("homeassistant.components.cover")
    _cover.ATTR_POSITION = "position"  # type: ignore[attr-defined]

    class _CoverEntityFeature(enum.IntFlag):
        OPEN = 1
        CLOSE = 2
        SET_POSITION = 4
        STOP = 8

    class _CoverEntity:
        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _cover.CoverEntityFeature = _CoverEntityFeature  # type: ignore[attr-defined]
    _cover.CoverEntity = _CoverEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.cover", _cover)

    _number = types.ModuleType("homeassistant.components.number")

    class _NumberMode(str, enum.Enum):
        SLIDER = "slider"
        BOX = "box"

    class _RestoreNumber:
        async def async_added_to_hass(self):
            return None

        async def async_get_last_number_data(self):
            return None

        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _number.NumberEntity = _RestoreNumber  # type: ignore[attr-defined]
    _number.NumberMode = _NumberMode  # type: ignore[attr-defined]
    _number.RestoreNumber = _RestoreNumber  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.number", _number)

    _select = types.ModuleType("homeassistant.components.select")

    class _SelectEntity:
        pass

    _select.SelectEntity = _SelectEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.select", _select)

    _restore = types.ModuleType("homeassistant.helpers.restore_state")

    class _RestoreEntity:
        async def async_added_to_hass(self):
            return None

        async def async_get_last_state(self):
            return None

        def async_on_remove(self, func):
            self._unsub = func

        def async_write_ha_state(self):
            return None

    _restore.RestoreEntity = _RestoreEntity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.restore_state", _restore)

    _ir = types.ModuleType("homeassistant.helpers.issue_registry")

    def _ir_create_issue(hass, domain, issue_id, **kwargs):
        hass.created_issues = getattr(hass, "created_issues", {})
        hass.created_issues[issue_id] = {"domain": domain, **kwargs}

    def _ir_delete_issue(hass, domain, issue_id):
        getattr(hass, "created_issues", {}).pop(issue_id, None)
        hass.deleted_issues = getattr(hass, "deleted_issues", [])
        hass.deleted_issues.append(issue_id)

    _ir.async_create_issue = _ir_create_issue  # type: ignore[attr-defined]
    _ir.async_delete_issue = _ir_delete_issue  # type: ignore[attr-defined]

    class _IssueSeverity:
        ERROR = "error"
        WARNING = "warning"

    _ir.IssueSeverity = _IssueSeverity  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.issue_registry", _ir)

    _dr = types.ModuleType("homeassistant.helpers.device_registry")

    class _DeviceInfo(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    _dr.DeviceInfo = _DeviceInfo  # type: ignore[attr-defined]
    _dr.EVENT_DEVICE_REGISTRY_UPDATED = "device_registry_updated"  # type: ignore[attr-defined]

    def _dr_async_get(hass):
        return getattr(hass, "device_registry", None)

    _dr.async_get = _dr_async_get  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.device_registry", _dr)

    _ar = types.ModuleType("homeassistant.helpers.area_registry")

    def _ar_async_get(hass):
        return getattr(hass, "area_registry", None)

    _ar.async_get = _ar_async_get  # type: ignore[attr-defined]
    _ar.EVENT_AREA_REGISTRY_UPDATED = "area_registry_updated"  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.area_registry", _ar)

    _er = types.ModuleType("homeassistant.helpers.entity_registry")

    class _EntityRegistry:
        def __init__(self, entities=None):
            self._entities = entities or {}

        def async_get(self, entity_id):
            return self._entities.get(entity_id)

        def async_get_entity_id(self, domain, platform, unique_id):
            return next(
                (
                    entity_id
                    for entity_id, e in self._entities.items()
                    if entity_id.startswith(f"{domain}.")
                    and getattr(e, "platform", None) == platform
                    and getattr(e, "unique_id", None) == unique_id
                ),
                None,
            )

    def _er_async_get(hass):
        return getattr(hass, "entity_registry", None) or _EntityRegistry()

    def _er_async_entries_for_config_entry(registry, config_entry_id):
        entities = getattr(registry, "_entities", {})
        return [e for e in entities.values() if getattr(e, "config_entry_id", None) == config_entry_id]

    def _er_async_entries_for_device(registry, device_id, include_disabled_entities=False):
        entities = getattr(registry, "_entities", {})
        return [e for e in entities.values() if getattr(e, "device_id", None) == device_id]

    _er.async_get = _er_async_get  # type: ignore[attr-defined]
    _er.async_entries_for_config_entry = _er_async_entries_for_config_entry  # type: ignore[attr-defined]
    _er.async_entries_for_device = _er_async_entries_for_device  # type: ignore[attr-defined]
    _er.EntityRegistry = _EntityRegistry  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.entity_registry", _er)

    _ep = types.ModuleType("homeassistant.helpers.entity_platform")
    _ep.AddEntitiesCallback = object  # type: ignore[attr-defined]

    class _CurrentPlatform:
        def async_register_entity_service(self, name, schema, func):
            return None

    _ep.async_get_current_platform = _CurrentPlatform  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.entity_platform", _ep)

    _frontend = types.ModuleType("homeassistant.components.frontend")

    def _async_remove_panel(hass, frontend_url_path, *, warn_if_unknown=True):
        return None

    _frontend.async_remove_panel = _async_remove_panel  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.frontend", _frontend)

    _panel_custom = types.ModuleType("homeassistant.components.panel_custom")

    async def _async_register_panel(hass, **kwargs):
        return None

    _panel_custom.async_register_panel = _async_register_panel  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.panel_custom", _panel_custom)

    _http = types.ModuleType("homeassistant.components.http")

    class _StaticPathConfig:
        def __init__(self, url_path, path, cache_headers=True):
            self.url_path = url_path
            self.path = path
            self.cache_headers = cache_headers

    _http.StaticPathConfig = _StaticPathConfig  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.http", _http)

    _storage = types.ModuleType("homeassistant.helpers.storage")

    class _Store:
        def __init__(self, hass, version, key):
            self._hass = hass
            self._key = ("store", key)

        async def async_load(self):
            return (getattr(self._hass, "data", None) or {}).get(self._key)

        async def async_save(self, data):
            self._hass.data[self._key] = data

        def async_delay_save(self, data_func, delay=0):
            self._hass.data[self._key] = data_func()

        async def async_remove(self):
            self._hass.data.pop(self._key, None)

    _storage.Store = _Store  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.storage", _storage)

    _trigger = types.ModuleType("homeassistant.helpers.trigger")

    async def _async_initialize_triggers(hass, trigger_config, action, domain, name, log_cb, **kwargs):
        return lambda: None  # tests monkeypatch this to capture (config, action)

    _trigger.async_initialize_triggers = _async_initialize_triggers  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.helpers.trigger", _trigger)

    _wsapi = types.ModuleType("homeassistant.components.websocket_api")

    def _identity_decorator(arg=None):
        # Usable both as @require_admin/@async_response and as @websocket_command({...}).
        if callable(arg):
            return arg
        return lambda func: func

    def _ws_async_register_command(hass, func):
        hass.data.setdefault("ws_commands", []).append(func)

    _wsapi.require_admin = _identity_decorator  # type: ignore[attr-defined]
    _wsapi.async_response = _identity_decorator  # type: ignore[attr-defined]
    _wsapi.websocket_command = _identity_decorator  # type: ignore[attr-defined]
    _wsapi.async_register_command = _ws_async_register_command  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.websocket_api", _wsapi)

    _devauto = types.ModuleType("homeassistant.components.device_automation")

    class _DeviceAutomationType:
        TRIGGER = "trigger"

    async def _async_get_device_automations(hass, automation_type, device_ids):
        return getattr(hass, "device_automations", {})

    _devauto.DeviceAutomationType = _DeviceAutomationType  # type: ignore[attr-defined]
    _devauto.async_get_device_automations = _async_get_device_automations  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.device_automation", _devauto)

    _devauto_exc = types.ModuleType("homeassistant.components.device_automation.exceptions")

    class _DeviceNotFound(Exception):
        pass

    _devauto_exc.DeviceNotFound = _DeviceNotFound  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.device_automation.exceptions", _devauto_exc)
