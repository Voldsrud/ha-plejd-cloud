"""Turn a device's raw HA device triggers into a friendly, per-button view.

Three layers, each a pure enhancement over the one before it — none can lose a
trigger, they can only present it better:

1. Generic grouping/humanizing (`group_generic`) works for any device: triggers
   that share a "subtype" are one button (subtype is how most integrations — ZHA,
   deCONZ, Zigbee2MQTT-via-MQTT-device-triggers — identify which physical control
   fired); a trigger with no subtype becomes its own single-trigger button.
2. Built-in profiles (`BUILT_IN_PROFILES`) give a handful of common remotes named
   buttons instead of raw type/subtype strings, matched by (manufacturer, model).
3. Custom overrides (`PlejdRemoteProfiles`, Store-backed) let an admin define a
   profile for one specific device_id, no code change or release needed.

`build_buttons_view` applies them in that precedence order (custom > built-in >
generic) and is what `dim_binding_ws.ws_device_triggers` calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.remote_profiles"

DEVICE_KIND_DOOR_WINDOW = "door_window"
DEVICE_KIND_MOTION = "motion"
DEVICE_KIND_REMOTE = "remote"

# binary_sensor device classes that report an open/closed state rather than button
# presses - offering a "buttons" picker for these is misleading (see #107).
_DOOR_WINDOW_CLASSES = frozenset(
    {
        BinarySensorDeviceClass.DOOR,
        BinarySensorDeviceClass.WINDOW,
        BinarySensorDeviceClass.GARAGE_DOOR,
        BinarySensorDeviceClass.OPENING,
    }
)
_MOTION_CLASSES = frozenset({BinarySensorDeviceClass.MOTION, BinarySensorDeviceClass.OCCUPANCY})


def classify_device_kind(hass: HomeAssistant, device_id: str) -> str:
    """Classify a bound device as door/window, motion, or a (default) button remote.

    Based on the device_class of its binary_sensor entities, if any - the same signal
    HA's own dashboards use to distinguish a contact sensor from a motion sensor from
    a plain switch. A device with no binary_sensor entity (the common case for an
    actual button remote) falls back to "remote", the pre-existing behaviour.
    """
    registry = er.async_get(hass)
    for entry in er.async_entries_for_device(registry, device_id, include_disabled_entities=True):
        if entry.entity_id.split(".", 1)[0] != "binary_sensor":
            continue
        device_class = entry.device_class or getattr(entry, "original_device_class", None)
        if device_class in _DOOR_WINDOW_CLASSES:
            return DEVICE_KIND_DOOR_WINDOW
        if device_class in _MOTION_CLASSES:
            return DEVICE_KIND_MOTION
    return DEVICE_KIND_REMOTE


class InvalidRemoteProfile(ValueError):
    """A custom remote button profile is structurally invalid."""


def humanize(value: str) -> str:
    """Turn a raw type/subtype string into a friendly label, e.g. brightness_move_up -> Brightness Move Up."""
    text = value.replace("_", " ").replace("-", " ").strip()
    return text.title() if text else value


def _trigger_label(trigger: dict[str, Any]) -> str:
    type_ = trigger.get("type")
    subtype = trigger.get("subtype")
    # Zigbee2MQTT's convention uses the literal type "action" as a placeholder — the
    # meaningful value (which action fired) is in subtype, so prefer it there or every
    # trigger in a group would be labelled just "Action".
    if type_ and str(type_) != "action":
        return humanize(str(type_))
    if subtype:
        return humanize(str(subtype))
    if type_:
        return humanize(str(type_))
    platform = trigger.get("platform")
    return humanize(str(platform)) if platform else "Trigger"


def group_generic(triggers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group triggers by "subtype" (button/direction identifier); no subtype -> own group.

    Guaranteed to represent every trigger exactly once — this is the fallback that
    must work for literally any remote, so it must never drop one.
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, trigger in enumerate(triggers):
        subtype = trigger.get("subtype")
        if subtype:
            group_id = f"subtype:{subtype}"
            label = humanize(str(subtype))
        else:
            group_id = f"trigger:{index}"
            label = _trigger_label(trigger)
        if group_id not in groups:
            groups[group_id] = {"id": group_id, "label": label, "triggers": []}
            order.append(group_id)
        groups[group_id]["triggers"].append({"label": _trigger_label(trigger), "trigger": trigger})
    return [groups[group_id] for group_id in order]


def _button_specs(*, subtypes: tuple[str, ...] = (), types: tuple[str, ...] = ()) -> list[dict[str, str | None]]:
    """Zigbee2MQTT-convention match specs: type="action", subtype=<action value>."""
    specs = [{"type": "action", "subtype": subtype} for subtype in subtypes]
    specs += [{"type": type_, "subtype": None} for type_ in types]
    return specs


# ── Built-in profiles ──────────────────────────────────────────────────────────
# Best-effort, standard Zigbee2MQTT convention (HA's MQTT device-trigger integration
# publishes one trigger per possible "action" value, as type="action", subtype=<value>).
# Not verified against physical hardware. ZHA/deCONZ report different type/subtype
# strings for the same device and are NOT covered here — those (and any unmatched
# trigger on a matched device) still work correctly via the generic path above; a
# profile is a pure enhancement, never a requirement.
_IKEA_TRADFRI_ON_OFF = {
    "device_type": "IKEA TRADFRI on/off switch",
    "buttons": [
        {"name": "on", "label": "Top button (On)", "triggers": _button_specs(subtypes=("on", "brightness_move_up"))},
        {
            "name": "off",
            "label": "Bottom button (Off)",
            "triggers": _button_specs(subtypes=("off", "brightness_move_down")),
        },
    ],
}

_IKEA_STYRBAR = {
    "device_type": "IKEA STYRBAR remote control",
    "buttons": [
        {"name": "on", "label": "Top button (On)", "triggers": _button_specs(subtypes=("on", "brightness_move_up"))},
        {
            "name": "off",
            "label": "Bottom button (Off)",
            "triggers": _button_specs(subtypes=("off", "brightness_move_down")),
        },
        {
            "name": "arrow_left",
            "label": "Left arrow",
            "triggers": _button_specs(subtypes=("arrow_left_click", "arrow_left_hold")),
        },
        {
            "name": "arrow_right",
            "label": "Right arrow",
            "triggers": _button_specs(subtypes=("arrow_right_click", "arrow_right_hold")),
        },
    ],
}

_IKEA_RODRET = {
    "device_type": "IKEA RODRET wireless dimmer",
    "buttons": [
        {"name": "on", "label": "Top button (On)", "triggers": _button_specs(subtypes=("on", "brightness_move_up"))},
        {
            "name": "off",
            "label": "Bottom button (Off)",
            "triggers": _button_specs(subtypes=("off", "brightness_move_down")),
        },
    ],
}

_HUE_DIMMER = {
    "device_type": "Philips Hue dimmer switch",
    "buttons": [
        {
            "name": "on",
            "label": "On button",
            "triggers": _button_specs(subtypes=("on_press", "on_hold", "on_press_release", "on_hold_release")),
        },
        {
            "name": "up",
            "label": "Dim up button",
            "triggers": _button_specs(subtypes=("up_press", "up_hold", "up_press_release", "up_hold_release")),
        },
        {
            "name": "down",
            "label": "Dim down button",
            "triggers": _button_specs(subtypes=("down_press", "down_hold", "down_press_release", "down_hold_release")),
        },
        {
            "name": "off",
            "label": "Off button",
            "triggers": _button_specs(subtypes=("off_press", "off_hold", "off_press_release", "off_hold_release")),
        },
    ],
}

_HUE_TAP_DIAL = {
    "device_type": "Philips Hue tap dial switch",
    "buttons": [
        {
            "name": f"button_{n}",
            "label": f"Button {n}",
            "triggers": _button_specs(
                subtypes=(
                    f"button_{n}_press",
                    f"button_{n}_hold",
                    f"button_{n}_press_release",
                    f"button_{n}_hold_release",
                )
            ),
        }
        for n in (1, 2, 3, 4)
    ],
}

_AQARA_WXKG = {
    "device_type": "Aqara wireless mini switch",
    "buttons": [
        {"name": "button", "label": "Button", "triggers": _button_specs(subtypes=("single", "double", "hold"))},
    ],
}

_AQARA_OPPLE_6 = {
    "device_type": "Aqara Opple switch (6-gang)",
    "buttons": [
        {
            "name": f"button_{n}",
            "label": f"Button {n}",
            "triggers": _button_specs(subtypes=(f"button_{n}_single", f"button_{n}_double", f"button_{n}_hold")),
        }
        for n in range(1, 7)
    ],
}

_SONOFF_SNZB01 = {
    "device_type": "SONOFF SNZB-01 wireless switch",
    "buttons": [
        {"name": "button", "label": "Button", "triggers": _button_specs(subtypes=("single", "double", "hold"))},
    ],
}

# (manufacturer, model) match aliases -> profile. Multiple aliases per profile hedge
# the different manufacturer/model strings ZHA vs. Zigbee2MQTT vs. deCONZ report for
# the same physical hardware.
_BUILT_IN_MATCHES: list[tuple[tuple[str, ...], tuple[str, ...], dict[str, Any]]] = [
    (("IKEA of Sweden", "IKEA"), ("TRADFRI on/off switch", "E1743"), _IKEA_TRADFRI_ON_OFF),
    (("IKEA of Sweden", "IKEA"), ("STYRBAR remote control", "E2001/E2002", "E2001", "E2002"), _IKEA_STYRBAR),
    (("IKEA of Sweden", "IKEA"), ("RODRET wireless dimmer", "RODRET Dimmer", "E2201"), _IKEA_RODRET),
    (
        ("Philips", "Signify Netherlands B.V."),
        ("Hue dimmer switch", "RWL021", "RWL022", "324131092621"),
        _HUE_DIMMER,
    ),
    (
        ("Philips", "Signify Netherlands B.V."),
        ("Hue tap dial switch", "Z3-1BRL", "8719514440937"),
        _HUE_TAP_DIAL,
    ),
    (
        ("Xiaomi", "Aqara", "LUMI"),
        (
            "lumi.sensor_switch",
            "lumi.sensor_switch.aq2",
            "lumi.remote.b1acn01",
            "WXKG01LM",
            "WXKG11LM",
            "Wireless Mini Switch",
        ),
        _AQARA_WXKG,
    ),
    (
        ("Xiaomi", "Aqara", "LUMI"),
        ("lumi.remote.b686opcn01", "WXCJKG13LM", "Aqara Opple switch (6 gang)"),
        _AQARA_OPPLE_6,
    ),
    (("SONOFF", "ITEAD"), ("SNZB-01", "WB01", "Wireless switch"), _SONOFF_SNZB01),
]


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _build_index() -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for manufacturers, models, profile in _BUILT_IN_MATCHES:
        for manufacturer in manufacturers:
            for model in models:
                index[(_normalize(manufacturer), _normalize(model))] = profile
    return index


_BUILT_IN_INDEX = _build_index()


def match_builtin_profile(
    manufacturer: str | None, model: str | None, model_id: str | None = None
) -> dict[str, Any] | None:
    profile = _BUILT_IN_INDEX.get((_normalize(manufacturer), _normalize(model)))
    if profile is not None:
        return profile
    # Some integrations report the vendor's product code (e.g. "E1743") in the device
    # registry's separate model_id field rather than in model itself.
    return _BUILT_IN_INDEX.get((_normalize(manufacturer), _normalize(model_id)))


def _apply_profile(triggers: list[dict[str, Any]], profile: dict[str, Any], *, source: str) -> dict[str, Any]:
    remaining = list(triggers)
    groups: list[dict[str, Any]] = []
    for button in profile.get("buttons", []):
        matched: list[dict[str, Any]] = []
        for spec in button.get("triggers", []):
            for trigger in list(remaining):
                if trigger.get("type") == spec.get("type") and trigger.get("subtype") == spec.get("subtype"):
                    matched.append(trigger)
                    remaining.remove(trigger)
        if matched:
            groups.append(
                {
                    "id": button["name"],
                    "label": button.get("label") or humanize(button["name"]),
                    "triggers": [{"label": _trigger_label(t), "trigger": t} for t in matched],
                }
            )
    groups.extend(group_generic(remaining))  # anything the profile didn't claim is still reachable
    return {"device_type": profile.get("device_type"), "source": source, "groups": groups}


def build_buttons_view(
    triggers: list[dict[str, Any]],
    *,
    manufacturer: str | None = None,
    model: str | None = None,
    model_id: str | None = None,
    custom_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The grouped-and-humanized "buttons" view: custom override > built-in > generic."""
    if custom_profile is not None:
        return _apply_profile(triggers, custom_profile, source="custom")
    profile = match_builtin_profile(manufacturer, model, model_id)
    if profile is not None:
        return _apply_profile(triggers, profile, source="profile")
    return {"device_type": None, "source": "generic", "groups": group_generic(triggers)}


def _validate_profile(profile: dict[str, Any]) -> None:
    buttons = profile.get("buttons")
    if not isinstance(buttons, list) or not buttons:
        raise InvalidRemoteProfile("a profile needs at least one button")
    for button in buttons:
        if not isinstance(button, dict) or not isinstance(button.get("name"), str) or not button.get("name"):
            raise InvalidRemoteProfile("every button needs a string name")
        triggers = button.get("triggers")
        if not isinstance(triggers, list) or not triggers:
            raise InvalidRemoteProfile(f"button {button['name']!r} needs at least one trigger")
        for trigger in triggers:
            if not isinstance(trigger, dict) or not trigger.get("type"):
                raise InvalidRemoteProfile(f"button {button['name']!r} has an invalid trigger")


class PlejdRemoteProfiles:
    """Loads/persists per-device custom button-profile overrides (admin-editable, no release needed)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self._profiles: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    @property
    def profiles(self) -> dict[str, dict[str, Any]]:
        return self._profiles

    def get(self, device_id: str) -> dict[str, Any] | None:
        return self._profiles.get(device_id)

    async def async_load(self) -> None:
        async with self._lock:
            self._profiles = await self._store.async_load() or {}

    async def async_save(self, device_id: str, profile: dict[str, Any]) -> None:
        _validate_profile(profile)
        async with self._lock:
            profiles = {**self._profiles, device_id: profile}
            await self._store.async_save(profiles)
            self._profiles = profiles

    async def async_delete(self, device_id: str) -> None:
        async with self._lock:
            if device_id not in self._profiles:
                return
            profiles = {k: v for k, v in self._profiles.items() if k != device_id}
            await self._store.async_save(profiles)
            self._profiles = profiles
