"""Tests for async_create_scene / async_update_scene / async_remove_scene (the HA-facing scene-management orchestration)."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from plejd_cloud import schedule_ws
from plejd_cloud.cloud import PlejdAuthError, PlejdCloudError, PlejdCloudScene, PlejdCloudSceneInfo, PlejdCloudSite
from plejd_cloud.manage_scene import async_create_scene, async_remove_scene, async_update_scene

_KEY = bytes(range(16))
_STEP = {"device_id": "d1", "output": 0, "state": "On", "value": 255}


def _scene(scene_id="s1", name="Movie Night") -> PlejdCloudSceneInfo:
    return PlejdCloudSceneInfo(scene_id=scene_id, name=name)


def _site(all_scenes=None, scenes=None) -> PlejdCloudSite:
    return PlejdCloudSite(
        site_id="S1",
        title="Home",
        crypto_key=_KEY,
        mesh_key="01-02-03-04",
        devices=[],
        inputs=[],
        motion=[],
        scenes=scenes or [],
        gateways=[],
        resource_set_id=None,
        all_scenes=all_scenes or [],
    )


def _hass():
    return types.SimpleNamespace(
        data={},
        config_entries=types.SimpleNamespace(
            async_update_entry=lambda entry, data: setattr(entry, "data", data),
            async_reload=AsyncMock(),
        ),
    )


def _entry(data=None, options=None):
    return types.SimpleNamespace(
        entry_id="e1",
        data=data or {"email": "u@x.com", "password": "pw", "site_id": "S1"},
        options=options or {},
        async_start_reauth=lambda hass: None,
    )


async def test_create_scene_raises_with_no_steps(monkeypatch):
    with pytest.raises(HomeAssistantError, match="at least one scene step"):
        await async_create_scene(_hass(), _entry(), title="X", scene_steps=[])


async def test_create_scene_raises_on_login_failure(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(side_effect=PlejdCloudError("down")))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error"):
        await async_create_scene(_hass(), _entry(), title="X", scene_steps=[_STEP])


async def test_create_scene_triggers_reauth_on_stale_credentials(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(side_effect=PlejdAuthError("bad creds")))
    hass = _hass()
    entry = _entry()
    entry.async_start_reauth = MagicMock()
    with pytest.raises(HomeAssistantError, match="reauthentication started"):
        await async_create_scene(hass, entry, title="X", scene_steps=[_STEP])
    entry.async_start_reauth.assert_called_once_with(hass)


async def test_create_scene_raises_on_get_site_failure(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(side_effect=PlejdCloudError("down")))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error"):
        await async_create_scene(_hass(), _entry(), title="X", scene_steps=[_STEP])


async def test_create_scene_raises_on_cloud_error(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_cloud_create_scene", AsyncMock(side_effect=PlejdCloudError("down"))
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error creating scene"):
        await async_create_scene(_hass(), _entry(), title="X", scene_steps=[_STEP])


async def test_create_scene_forwards_fields_and_reloads(monkeypatch):
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(side_effect=[_site(), _site([_scene()])]))
    create_mock = AsyncMock(return_value="new-scene-id")
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_create_scene", create_mock)

    await async_create_scene(
        hass, entry, title="Movie Night", scene_steps=[_STEP], order=2, hidden_from_scene_list=True
    )

    create_mock.assert_awaited_once_with(
        None, "tok", "S1", "Movie Night", [_STEP], order=2, hidden_from_scene_list=True
    )
    assert entry.data["scenes"] == []  # refreshed from fresh_site.scenes
    hass.config_entries.async_reload.assert_awaited_once_with("e1")
    assert not schedule_ws.async_get_reload_lock(hass, entry.entry_id).locked()


async def test_create_scene_does_not_raise_when_only_the_reload_fails(monkeypatch, caplog):
    # The scene was already created on the cloud (non-idempotent) by the time the reload
    # is attempted - raising here would make the whole service call look failed, inviting
    # a retry that creates a duplicate scene. Only the reload itself needs a manual retry.
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(side_effect=[_site(), _site([_scene()])]))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_create_scene", AsyncMock(return_value="new-scene-id"))
    hass.config_entries.async_reload = AsyncMock(return_value=False)

    await async_create_scene(hass, entry, title="Movie Night", scene_steps=[_STEP])

    assert "entry failed to reload after a scene create" in caplog.text


async def test_create_scene_raises_on_cloud_error_during_refresh(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_get_site", AsyncMock(side_effect=[_site(), PlejdCloudError("down")])
    )
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_create_scene", AsyncMock(return_value="new-id"))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error refreshing site"):
        await async_create_scene(_hass(), _entry(), title="X", scene_steps=[_STEP])


async def test_create_scene_runs_a_follow_up_reload_for_a_concurrent_change(monkeypatch):
    # _async_reload_entry sets DATA_RELOAD_PENDING when it suppressed a genuinely
    # concurrent options/data change's own reload while ours was in flight - that change
    # must still get its own reload afterward instead of being silently dropped.
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(side_effect=[_site(), _site([_scene()])]))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_create_scene", AsyncMock(return_value="new-id"))

    calls: list[str] = []

    async def _reload_sets_pending(entry_id):
        calls.append(entry_id)
        if len(calls) == 1:  # only the first reload race-loses to the concurrent change
            hass.data[schedule_ws.DATA_RELOAD_PENDING] = entry_id
        return True

    hass.config_entries.async_reload = AsyncMock(side_effect=_reload_sets_pending)

    await async_create_scene(hass, entry, title="X", scene_steps=[_STEP])

    assert hass.config_entries.async_reload.await_count == 2  # ours, then the follow-up
    assert schedule_ws.DATA_RELOAD_PENDING not in hass.data


async def test_update_scene_raises_with_no_fields(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([_scene()])))
    with pytest.raises(HomeAssistantError, match="at least one of"):
        await async_update_scene(_hass(), _entry(), scene_id="s1")


async def test_update_scene_raises_if_scene_not_found(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([])))
    with pytest.raises(HomeAssistantError, match="not found"):
        await async_update_scene(_hass(), _entry(), scene_id="missing", title="X")


async def test_update_scene_rejects_empty_scene_steps(monkeypatch):
    # scene_steps replaces every existing step - an empty list must be rejected as invalid
    # input, not silently accepted as "clear the scene" (#116 review).
    with pytest.raises(HomeAssistantError, match="at least one"):
        await async_update_scene(_hass(), _entry(), scene_id="s1", scene_steps=[])


async def test_update_scene_raises_when_cloud_rejects_update(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_update_scene", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="rejected"):
        await async_update_scene(_hass(), _entry(), scene_id="s1", title="X")


async def test_update_scene_raises_on_cloud_error_during_update(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_cloud_update_scene", AsyncMock(side_effect=PlejdCloudError("down"))
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error updating scene"):
        await async_update_scene(_hass(), _entry(), scene_id="s1", title="X")


async def test_update_scene_forwards_fields_and_reloads(monkeypatch):
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_get_site",
        AsyncMock(side_effect=[_site([_scene()]), _site([_scene(name="Renamed")])]),
    )
    update_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_update_scene", update_mock)

    await async_update_scene(hass, entry, scene_id="s1", title="Renamed", order=1, scene_steps=[_STEP])

    update_mock.assert_awaited_once_with(
        None, "tok", "S1", "s1", title="Renamed", order=1, scene_steps=[_STEP], hidden_from_scene_list=None
    )
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


async def test_update_scene_refuses_when_it_is_a_cloud_schedule_on_scene(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([_scene()])))
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [{"scene_id": "s1", "title": "Garage", "night_reduction": None}],
        }
    )
    update_mock = AsyncMock()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_update_scene", update_mock)
    with pytest.raises(HomeAssistantError, match="Garage"):
        await async_update_scene(_hass(), entry, scene_id="s1", title="Hacked")
    update_mock.assert_not_awaited()  # must not even attempt the cloud call


async def test_update_scene_refuses_when_it_is_a_cloud_schedule_night_reduction_scene(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([_scene(scene_id="night1")]))
    )
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [{"scene_id": "s1", "title": "Garage", "night_reduction": {"scene_id": "night1"}}],
        }
    )
    update_mock = AsyncMock()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_update_scene", update_mock)
    with pytest.raises(HomeAssistantError, match="Garage"):
        await async_update_scene(_hass(), entry, scene_id="night1", title="Hacked")
    update_mock.assert_not_awaited()


async def test_remove_scene_raises_if_scene_not_found(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([])))
    with pytest.raises(HomeAssistantError, match="not found"):
        await async_remove_scene(_hass(), _entry(), scene_id="missing")


async def test_remove_scene_refuses_when_referenced_by_a_schedule(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_get_site",
        AsyncMock(return_value=_site([_scene()], scenes=[PlejdCloudScene(scene_id="s1", name="Movie Night", index=3)])),
    )
    entry = _entry(options={"schedules": [{"id": 0, "name": "Evening", "days": [0], "time": "20:00", "scene": 3}]})
    remove_mock = AsyncMock()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_remove_scene", remove_mock)
    with pytest.raises(HomeAssistantError, match="Evening"):
        await async_remove_scene(_hass(), entry, scene_id="s1")
    remove_mock.assert_not_awaited()  # must not even attempt the cloud call


async def test_remove_scene_refuses_when_referenced_by_a_cloud_schedule_on_scene(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([_scene()])))
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [{"scene_id": "s1", "title": "Garage", "night_reduction": None}],
        }
    )
    remove_mock = AsyncMock()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_remove_scene", remove_mock)
    with pytest.raises(HomeAssistantError, match="Garage"):
        await async_remove_scene(_hass(), entry, scene_id="s1")
    remove_mock.assert_not_awaited()


async def test_remove_scene_refuses_when_referenced_by_a_cloud_schedule_night_reduction(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([_scene(scene_id="night1")]))
    )
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [{"scene_id": "s1", "title": "Garage", "night_reduction": {"scene_id": "night1"}}],
        }
    )
    remove_mock = AsyncMock()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_remove_scene", remove_mock)
    with pytest.raises(HomeAssistantError, match="Garage"):
        await async_remove_scene(_hass(), entry, scene_id="night1")
    remove_mock.assert_not_awaited()


async def test_remove_scene_allows_when_no_schedule_references_it(monkeypatch):
    hass = _hass()
    entry = _entry(options={"schedules": [{"id": 0, "name": "Evening", "days": [0], "time": "20:00", "scene": 99}]})
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_get_site",
        AsyncMock(
            side_effect=[
                _site([_scene()], scenes=[PlejdCloudScene(scene_id="s1", name="Movie Night", index=3)]),
                _site([]),
            ]
        ),
    )
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_remove_scene", AsyncMock(return_value=True))

    await async_remove_scene(hass, entry, scene_id="s1")

    hass.config_entries.async_reload.assert_awaited_once_with("e1")


async def test_remove_scene_raises_on_cloud_error_during_removal(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_cloud_remove_scene", AsyncMock(side_effect=PlejdCloudError("down"))
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error removing scene"):
        await async_remove_scene(_hass(), _entry(), scene_id="s1")


async def test_remove_scene_raises_when_cloud_rejects_removal(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_remove_scene", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="rejected"):
        await async_remove_scene(_hass(), _entry(), scene_id="s1")


async def test_remove_scene_succeeds_and_reloads(monkeypatch):
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_get_site", AsyncMock(side_effect=[_site([_scene()]), _site([])])
    )
    remove_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_remove_scene", remove_mock)

    await async_remove_scene(hass, entry, scene_id="s1")

    remove_mock.assert_awaited_once_with(None, "tok", "S1", "s1")
    assert entry.data["scenes"] == []
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


async def test_remove_scene_does_not_raise_when_only_the_reload_fails(monkeypatch, caplog):
    # The scene's cloud removal already happened (non-idempotent - retrying just gets
    # "not found") by the time the reload is attempted - raising here would report a
    # successful removal as a failed one, with no way to retry through this same path.
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_scene.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_scene.async_get_site", AsyncMock(side_effect=[_site([_scene()]), _site([])])
    )
    monkeypatch.setattr("plejd_cloud.manage_scene.async_cloud_remove_scene", AsyncMock(return_value=True))
    hass.config_entries.async_reload = AsyncMock(return_value=False)

    await async_remove_scene(hass, entry, scene_id="s1")

    assert "entry failed to reload after a scene remove" in caplog.text
