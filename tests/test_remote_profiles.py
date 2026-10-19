"""Tests for remote button-profile grouping/humanizing and custom overrides."""

from __future__ import annotations

import types

import pytest
from homeassistant.helpers import entity_registry as er
from plejd_cloud.remote_profiles import (
    DEVICE_KIND_DOOR_WINDOW,
    DEVICE_KIND_MOTION,
    DEVICE_KIND_REMOTE,
    InvalidRemoteProfile,
    PlejdRemoteProfiles,
    build_buttons_view,
    classify_device_kind,
    group_generic,
    humanize,
    match_builtin_profile,
)


def _hass(data=None, entity_registry=None):
    h = types.SimpleNamespace(data=data if data is not None else {})
    if entity_registry is not None:
        h.entity_registry = entity_registry
    return h


def _entity(entity_id, device_id, device_class=None):
    return types.SimpleNamespace(entity_id=entity_id, device_id=device_id, device_class=device_class)


# ── humanize ─────────────────────────────────────────────────────────────────


def test_humanize_replaces_underscores_and_title_cases():
    assert humanize("brightness_move_up") == "Brightness Move Up"


def test_humanize_replaces_dashes():
    assert humanize("button-1") == "Button 1"


def test_humanize_empty_string_returns_itself():
    assert humanize("") == ""


# ── generic grouping ─────────────────────────────────────────────────────────


def test_group_generic_groups_by_shared_subtype():
    triggers = [
        {"platform": "device", "type": "remote_button_short_press", "subtype": "button_1"},
        {"platform": "device", "type": "remote_button_long_press", "subtype": "button_1"},
        {"platform": "device", "type": "remote_button_short_press", "subtype": "button_2"},
    ]
    groups = group_generic(triggers)
    assert [g["id"] for g in groups] == ["subtype:button_1", "subtype:button_2"]
    assert groups[0]["label"] == "Button 1"
    assert [t["label"] for t in groups[0]["triggers"]] == ["Remote Button Short Press", "Remote Button Long Press"]
    assert groups[0]["triggers"][0]["trigger"] is triggers[0]


def test_group_generic_groups_triggers_without_subtype_individually():
    triggers = [
        {"platform": "device", "type": "on"},
        {"platform": "device", "type": "off"},
    ]
    groups = group_generic(triggers)
    assert [g["id"] for g in groups] == ["trigger:0", "trigger:1"]
    assert [g["label"] for g in groups] == ["On", "Off"]
    assert all(len(g["triggers"]) == 1 for g in groups)


def test_group_generic_falls_back_to_platform_when_type_missing():
    triggers = [{"platform": "device"}]
    groups = group_generic(triggers)
    assert groups[0]["label"] == "Device"


def test_group_generic_falls_back_to_generic_label_when_type_and_platform_missing():
    triggers = [{}]
    groups = group_generic(triggers)
    assert groups[0]["label"] == "Trigger"


def test_group_generic_prefers_subtype_label_when_type_is_the_zigbee2mqtt_action_placeholder():
    # Zigbee2MQTT device triggers all use type="action"; the real value is in subtype —
    # every trigger must not collapse to the same "Action" label.
    triggers = [
        {"type": "action", "subtype": "on"},
        {"type": "action", "subtype": "brightness_move_up"},
    ]
    groups = group_generic(triggers)
    labels = {t["label"] for group in groups for t in group["triggers"]}
    assert labels == {"On", "Brightness Move Up"}


def test_group_generic_action_type_with_no_subtype_falls_back_to_type_label():
    triggers = [{"type": "action"}]
    groups = group_generic(triggers)
    assert groups[0]["label"] == "Action"


def test_group_generic_every_trigger_is_represented_exactly_once():
    triggers = [
        {"type": "a", "subtype": "x"},
        {"type": "b"},
        {"type": "c", "subtype": "x"},
        {"type": "d"},
    ]
    groups = group_generic(triggers)
    flattened = [entry["trigger"] for group in groups for entry in group["triggers"]]
    assert len(flattened) == len(triggers)
    assert all(trigger in flattened for trigger in triggers)  # every trigger reachable, order not guaranteed


def test_group_generic_empty_list():
    assert group_generic([]) == []


# ── built-in profile matching ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("manufacturer", "model"),
    [
        ("IKEA of Sweden", "TRADFRI on/off switch"),
        ("IKEA", "E1743"),
        ("Signify Netherlands B.V.", "Hue dimmer switch"),
        ("Xiaomi", "lumi.sensor_switch.aq2"),
        ("SONOFF", "SNZB-01"),
    ],
)
def test_match_builtin_profile_known_devices(manufacturer, model):
    assert match_builtin_profile(manufacturer, model) is not None


def test_match_builtin_profile_is_case_insensitive():
    assert match_builtin_profile("ikea", "tradfri on/off switch") is not None


def test_match_builtin_profile_unknown_device_returns_none():
    assert match_builtin_profile("Acme Corp", "Universal Remote 3000") is None


def test_match_builtin_profile_none_manufacturer_and_model():
    assert match_builtin_profile(None, None) is None


def test_match_builtin_profile_falls_back_to_model_id_when_model_does_not_match():
    # Some integrations report the human-readable name in `model` (unrecognized here) and
    # the vendor product code in the separate `model_id` field.
    assert match_builtin_profile("IKEA", "some unrelated friendly name", model_id="E1743") is not None


def test_match_builtin_profile_prefers_model_over_model_id_when_both_present():
    profile = match_builtin_profile("IKEA", "TRADFRI on/off switch", model_id="not-a-real-model-id")
    assert profile is not None
    assert profile["device_type"] == "IKEA TRADFRI on/off switch"


# ── build_buttons_view precedence and matching ──────────────────────────────


def test_build_buttons_view_generic_fallback_for_unmatched_device():
    triggers = [{"type": "on", "subtype": "button_1"}]
    view = build_buttons_view(triggers, manufacturer="Acme", model="Widget")
    assert view["source"] == "generic"
    assert view["device_type"] is None
    assert view["groups"][0]["id"] == "subtype:button_1"


def test_build_buttons_view_matches_builtin_profile_and_names_buttons():
    triggers = [
        {"type": "action", "subtype": "on"},
        {"type": "action", "subtype": "off"},
    ]
    view = build_buttons_view(triggers, manufacturer="IKEA", model="E1743")
    assert view["source"] == "profile"
    assert view["device_type"] == "IKEA TRADFRI on/off switch"
    ids = {g["id"] for g in view["groups"]}
    assert ids == {"on", "off"}
    on_group = next(g for g in view["groups"] if g["id"] == "on")
    assert on_group["label"] == "Top button (On)"


def test_build_buttons_view_builtin_profile_leftover_triggers_still_reachable():
    triggers = [
        {"type": "action", "subtype": "on"},
        {"type": "action", "subtype": "some_unmapped_action"},
    ]
    view = build_buttons_view(triggers, manufacturer="IKEA", model="E1743")
    ids = {g["id"] for g in view["groups"]}
    assert "on" in ids
    assert "subtype:some_unmapped_action" in ids  # unmatched trigger falls to the generic path


def test_build_buttons_view_custom_override_takes_precedence_over_builtin():
    triggers = [{"type": "action", "subtype": "on"}]
    custom = {
        "buttons": [{"name": "custom_on", "label": "My Custom On", "triggers": [{"type": "action", "subtype": "on"}]}]
    }
    view = build_buttons_view(triggers, manufacturer="IKEA", model="E1743", custom_profile=custom)
    assert view["source"] == "custom"
    assert view["groups"][0]["id"] == "custom_on"
    assert view["groups"][0]["label"] == "My Custom On"


def test_build_buttons_view_profile_button_without_explicit_label_is_humanized():
    triggers = [{"type": "action", "subtype": "x"}]
    custom = {"buttons": [{"name": "some_button", "triggers": [{"type": "action", "subtype": "x"}]}]}
    view = build_buttons_view(triggers, custom_profile=custom)
    assert view["groups"][0]["label"] == "Some Button"


def test_build_buttons_view_no_manufacturer_or_model_falls_back_to_generic():
    triggers = [{"type": "on"}]
    view = build_buttons_view(triggers)
    assert view["source"] == "generic"


def test_build_buttons_view_matches_via_model_id():
    triggers = [{"type": "action", "subtype": "on"}]
    view = build_buttons_view(triggers, manufacturer="IKEA", model="unrecognized", model_id="E1743")
    assert view["source"] == "profile"
    assert view["device_type"] == "IKEA TRADFRI on/off switch"


# ── PlejdRemoteProfiles (Store-backed custom overrides) ─────────────────────


async def test_async_load_defaults_to_empty_when_nothing_stored():
    profiles = PlejdRemoteProfiles(_hass())
    await profiles.async_load()
    assert profiles.profiles == {}


async def test_async_save_then_get_roundtrips():
    hass = _hass()
    profiles = PlejdRemoteProfiles(hass)
    await profiles.async_load()
    profile = {"buttons": [{"name": "b1", "label": "Button 1", "triggers": [{"type": "action", "subtype": "on"}]}]}
    await profiles.async_save("dev1", profile)
    assert profiles.get("dev1") == profile
    assert profiles.profiles == {"dev1": profile}


async def test_async_save_persists_across_reload():
    hass = _hass()
    profiles = PlejdRemoteProfiles(hass)
    await profiles.async_load()
    profile = {"buttons": [{"name": "b1", "label": "Button 1", "triggers": [{"type": "action", "subtype": "on"}]}]}
    await profiles.async_save("dev1", profile)

    reloaded = PlejdRemoteProfiles(hass)
    await reloaded.async_load()
    assert reloaded.get("dev1") == profile


async def test_async_delete_removes_saved_profile():
    hass = _hass()
    profiles = PlejdRemoteProfiles(hass)
    await profiles.async_load()
    profile = {"buttons": [{"name": "b1", "label": "Button 1", "triggers": [{"type": "action", "subtype": "on"}]}]}
    await profiles.async_save("dev1", profile)
    await profiles.async_delete("dev1")
    assert profiles.get("dev1") is None
    assert profiles.profiles == {}


async def test_async_delete_nonexistent_device_is_a_noop():
    profiles = PlejdRemoteProfiles(_hass())
    await profiles.async_load()
    await profiles.async_delete("gone")  # no error
    assert profiles.profiles == {}


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {"buttons": []},
        {"buttons": "not-a-list"},
        {"buttons": [{"triggers": [{"type": "on"}]}]},  # missing name
        {"buttons": [{"name": 42, "triggers": [{"type": "on"}]}]},  # non-string name
        {"buttons": [{"name": "b1", "triggers": []}]},  # empty triggers
        {"buttons": [{"name": "b1", "triggers": "not-a-list"}]},
        {"buttons": [{"name": "b1", "triggers": [{"subtype": "x"}]}]},  # trigger missing type
        {"buttons": [{"name": "b1", "triggers": ["not-a-dict"]}]},
        {"buttons": ["not-a-dict"]},
    ],
)
async def test_async_save_rejects_invalid_profile_shapes(profile):
    profiles = PlejdRemoteProfiles(_hass())
    await profiles.async_load()
    with pytest.raises(InvalidRemoteProfile):
        await profiles.async_save("dev1", profile)
    assert profiles.profiles == {}  # rejected before persisting


# ── classify_device_kind ───────────────────────────────────────────────────────


def test_classify_door_sensor():
    registry = er.EntityRegistry({"binary_sensor.front_door": _entity("binary_sensor.front_door", "dev1", "door")})
    assert classify_device_kind(_hass(entity_registry=registry), "dev1") == DEVICE_KIND_DOOR_WINDOW


def test_classify_window_sensor():
    registry = er.EntityRegistry(
        {"binary_sensor.kitchen_window": _entity("binary_sensor.kitchen_window", "dev1", "window")}
    )
    assert classify_device_kind(_hass(entity_registry=registry), "dev1") == DEVICE_KIND_DOOR_WINDOW


def test_classify_garage_door_and_opening_also_door_window():
    for device_class in ("garage_door", "opening"):
        registry = er.EntityRegistry({"binary_sensor.x": _entity("binary_sensor.x", "dev1", device_class)})
        assert classify_device_kind(_hass(entity_registry=registry), "dev1") == DEVICE_KIND_DOOR_WINDOW


def test_classify_motion_sensor():
    registry = er.EntityRegistry({"binary_sensor.hall_motion": _entity("binary_sensor.hall_motion", "dev1", "motion")})
    assert classify_device_kind(_hass(entity_registry=registry), "dev1") == DEVICE_KIND_MOTION


def test_classify_occupancy_also_motion():
    registry = er.EntityRegistry({"binary_sensor.x": _entity("binary_sensor.x", "dev1", "occupancy")})
    assert classify_device_kind(_hass(entity_registry=registry), "dev1") == DEVICE_KIND_MOTION


def test_classify_button_remote_has_no_binary_sensor():
    # A real button remote has no binary_sensor entity at all - falls back to "remote".
    registry = er.EntityRegistry({"event.remote_button": _entity("event.remote_button", "dev1", None)})
    assert classify_device_kind(_hass(entity_registry=registry), "dev1") == DEVICE_KIND_REMOTE


def test_classify_binary_sensor_with_other_device_class_falls_back_to_remote():
    # A binary_sensor that isn't a door/window/motion class (e.g. "problem") doesn't
    # match either bucket - falls back to the default rather than misclassifying it.
    registry = er.EntityRegistry({"binary_sensor.x": _entity("binary_sensor.x", "dev1", "problem")})
    assert classify_device_kind(_hass(entity_registry=registry), "dev1") == DEVICE_KIND_REMOTE


def test_classify_ignores_entities_belonging_to_other_devices():
    registry = er.EntityRegistry({"binary_sensor.other": _entity("binary_sensor.other", "dev2", "door")})
    assert classify_device_kind(_hass(entity_registry=registry), "dev1") == DEVICE_KIND_REMOTE


def test_classify_no_registry_falls_back_to_remote():
    assert classify_device_kind(_hass(), "dev1") == DEVICE_KIND_REMOTE
