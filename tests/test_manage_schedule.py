"""Tests for async_create_schedule / async_update_schedule (cloud schedule management)."""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from plejd_cloud import schedule_ws
from plejd_cloud.cloud import PlejdAuthError, PlejdCloudError, PlejdCloudSceneInfo, PlejdCloudSite
from plejd_cloud.manage_schedule import (
    _sync_cloud_schedules_cache,
    async_create_schedule,
    async_remove_schedule,
    async_update_schedule,
)

_KEY = bytes(range(16))
_STEP = {"device_id": "d1", "output": 0, "state": "On", "value": 255}
_NIGHT_STEP = {"device_id": "d1", "output": 0, "state": "Off", "value": 0}


def _scene(scene_id="s1", name="Garage") -> PlejdCloudSceneInfo:
    return PlejdCloudSceneInfo(scene_id=scene_id, name=name)


def _site(all_scenes=None) -> PlejdCloudSite:
    return PlejdCloudSite(
        site_id="S1",
        title="Home",
        crypto_key=_KEY,
        mesh_key="01-02-03-04",
        devices=[],
        inputs=[],
        motion=[],
        scenes=[],
        gateways=[],
        resource_set_id=None,
        all_scenes=all_scenes or [],
    )


class _FakeBus:
    def __init__(self):
        self.fired: list[tuple[str, dict]] = []

    def async_fire(self, event_type, data):
        self.fired.append((event_type, data))


def _hass():
    return types.SimpleNamespace(
        data={},
        bus=_FakeBus(),
        async_block_till_done=AsyncMock(),
        config_entries=types.SimpleNamespace(
            async_update_entry=lambda entry, data: setattr(entry, "data", data),
            async_reload=AsyncMock(),
        ),
    )


def _entry(data=None):
    return types.SimpleNamespace(
        entry_id="e1",
        data=data or {"email": "u@x.com", "password": "pw", "site_id": "S1"},
        options={},
        async_start_reauth=lambda hass: None,
    )


def _cached_schedule(**overrides) -> dict:
    schedule = {
        "schedule_id": "te1",
        "scene_id": "s1",
        "title": "Garage",
        "scheduled_days": [0, 1, 2, 3, 4, 5, 6],
        "fade_time": 0,
        "activated": True,
        "start_event": "sunset",
        "start_offset": 15,
        "end_event": "sunrise",
        "end_offset": 0,
        "night_reduction": None,
    }
    schedule.update(overrides)
    return schedule


# --- create_schedule ---


async def test_create_schedule_raises_with_no_steps():
    with pytest.raises(HomeAssistantError, match="at least one scene step"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )


async def test_create_schedule_raises_on_invalid_start_event():
    with pytest.raises(HomeAssistantError, match="start_event must be one of"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="noon",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )


async def test_create_schedule_raises_on_invalid_end_offset():
    with pytest.raises(HomeAssistantError, match="end_offset must be between"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=999,
        )


async def test_create_schedule_raises_on_invalid_scheduled_days():
    with pytest.raises(HomeAssistantError, match="scheduled_days must be integers"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
            scheduled_days=[0, 9],
        )


async def test_create_schedule_raises_on_empty_scheduled_days():
    # An empty list is falsy-but-meaningful input, not "not specified" - it must be rejected,
    # not silently treated the same as omitting scheduled_days (which defaults to every day).
    with pytest.raises(HomeAssistantError, match="scheduled_days must include at least one day"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
            scheduled_days=[],
        )


async def test_create_schedule_raises_when_night_reduction_has_no_steps():
    with pytest.raises(HomeAssistantError, match="night_reduction needs at least one scene step"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
            night_reduction={"scene_steps": [], "start_time": "23:00", "end_time": "05:00"},
        )


async def test_create_schedule_raises_when_night_reduction_missing_times():
    with pytest.raises(HomeAssistantError, match="night_reduction needs start_time and end_time"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
            night_reduction={"scene_steps": [_NIGHT_STEP]},
        )


async def test_create_schedule_raises_when_only_one_weekend_time_given():
    with pytest.raises(HomeAssistantError, match="weekend_start_time and weekend_end_time must be given together"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
            night_reduction={
                "scene_steps": [_NIGHT_STEP],
                "start_time": "23:00",
                "end_time": "05:00",
                "weekend_start_time": "23:30",
            },
        )


async def test_create_schedule_raises_on_malformed_night_reduction_start_time():
    with pytest.raises(HomeAssistantError, match='start_time must be a 24-hour "HH:MM" time'):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
            night_reduction={"scene_steps": [_NIGHT_STEP], "start_time": "25:99", "end_time": "05:00"},
        )


async def test_create_schedule_raises_on_malformed_weekend_time():
    with pytest.raises(HomeAssistantError, match='weekend_start_time must be a 24-hour "HH:MM" time'):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
            night_reduction={
                "scene_steps": [_NIGHT_STEP],
                "start_time": "23:00",
                "end_time": "05:00",
                "weekend_start_time": "bad",
                "weekend_end_time": "06:00",
            },
        )


async def test_create_schedule_raises_on_malformed_weekend_end_time():
    with pytest.raises(HomeAssistantError, match='weekend_end_time must be a 24-hour "HH:MM" time'):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
            night_reduction={
                "scene_steps": [_NIGHT_STEP],
                "start_time": "23:00",
                "end_time": "05:00",
                "weekend_start_time": "23:30",
                "weekend_end_time": "bad",
            },
        )


async def test_create_schedule_raises_on_login_failure(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(side_effect=PlejdCloudError("down")))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )


async def test_create_schedule_triggers_reauth_on_stale_credentials(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(side_effect=PlejdAuthError("bad creds")))
    hass = _hass()
    entry = _entry()
    entry.async_start_reauth = MagicMock()
    with pytest.raises(HomeAssistantError, match="reauthentication started"):
        await async_create_schedule(
            hass,
            entry,
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )
    entry.async_start_reauth.assert_called_once_with(hass)


async def test_create_schedule_raises_on_get_site_failure(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(side_effect=PlejdCloudError("down")))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )


async def test_create_schedule_raises_on_cloud_error_creating_scene(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(side_effect=PlejdCloudError("down"))
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error creating schedule"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )


async def test_create_schedule_raises_when_cloud_rejects_time_event(monkeypatch):
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-scene-id"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value=None))
    remove_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_scene", remove_mock)
    with pytest.raises(HomeAssistantError, match="Plejd cloud rejected the schedule creation"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )
    remove_mock.assert_awaited_once_with(None, "tok", "S1", "new-scene-id")


async def test_create_schedule_persists_cache_before_firing_event(monkeypatch):
    # A listener reacting to plejd_schedule_created synchronously must already see the new
    # schedule as tracked - entry.data has to be written before the event fires, not after
    # (the full refresh, which writes it again, only follows afterward).
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-scene-id"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))

    seen_at_fire_time = {}
    original_fire = hass.bus.async_fire

    def _capture_fire(event_type, data):
        seen_at_fire_time["cloud_schedules"] = list(entry.data.get("cloud_schedules", []))
        original_fire(event_type, data)

    hass.bus.async_fire = _capture_fire

    await async_create_schedule(
        hass,
        entry,
        title="X",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=0,
        end_event="sunrise",
        end_offset=0,
    )

    assert len(seen_at_fire_time["cloud_schedules"]) == 1
    assert seen_at_fire_time["cloud_schedules"][0]["schedule_id"] == "te1"


async def test_create_schedule_succeeds_and_caches_it(monkeypatch):
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    create_scene_mock = AsyncMock(return_value="new-scene-id")
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", create_scene_mock)
    create_time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_time_event", create_time_event_mock)
    update_scene_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", update_scene_mock)

    schedule_id = await async_create_schedule(
        hass,
        entry,
        title="Garage",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=15,
        end_event="sunrise",
        end_offset=0,
    )

    # The id is server-generated (createTimeEvent_V3's response eventId), not client-generated -
    # it isn't otherwise discoverable (getSiteById can't rediscover it, diagnostics redacts the
    # cache), so callers (the create_schedule service handler, which fires it as an event) need
    # this return value to surface it.
    assert schedule_id == "te1"
    assert schedule_id == entry.data["cloud_schedules"][0]["schedule_id"]
    create_scene_mock.assert_awaited_once_with(
        None,
        "tok",
        "S1",
        "Garage",
        [_STEP],
        hidden_from_scene_list=True,
        settings='{"SceneType": "AstroEventScene"}',
    )
    create_time_event_mock.assert_awaited_once_with(
        None,
        "tok",
        "S1",
        "new-scene-id",
        scheduled_days=[0, 1, 2, 3, 4, 5, 6],
        fade_time=0,
        activated=True,
        start_event="sunset",
        start_offset=15,
        end_event="sunrise",
        end_offset=0,
        dirty_devices=["d1"],
        night_reduction=None,
    )
    # CreatedById can only be set once the server-generated eventId is known, so it's
    # backfilled via a follow-up updateScene call rather than set at scene-creation time.
    update_scene_mock.assert_awaited_once_with(
        None, "tok", "S1", "new-scene-id", settings='{"SceneType": "AstroEventScene", "CreatedById": "te1"}'
    )
    cached = entry.data["cloud_schedules"]
    assert len(cached) == 1
    assert cached[0]["scene_id"] == "new-scene-id"
    assert cached[0]["title"] == "Garage"
    assert cached[0]["device_ids"] == ["d1"]
    assert cached[0]["night_reduction"] is None
    hass.config_entries.async_reload.assert_awaited_once_with("e1")
    assert hass.bus.fired == [("plejd_schedule_created", {"schedule_id": "te1"})]


async def test_create_schedule_does_not_raise_when_only_the_reload_fails(monkeypatch, caplog):
    # The schedule was already created on the cloud (non-idempotent) by the time the
    # reload is attempted - raising here would make the whole service call look failed,
    # inviting a retry that creates a duplicate schedule. The id must still be returned.
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-scene-id"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))
    hass.config_entries.async_reload = AsyncMock(return_value=False)

    schedule_id = await async_create_schedule(
        hass,
        entry,
        title="Garage",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=15,
        end_event="sunrise",
        end_offset=0,
    )

    assert schedule_id == "te1"
    assert "entry failed to reload after a schedule create" in caplog.text


async def test_create_schedule_logs_when_createdby_backfill_is_rejected(monkeypatch, caplog):
    # The schedule works even if this backfill fails (the scene(s) and trigger are already
    # live) - so a rejection is logged, not raised.
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-scene-id"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=False))

    schedule_id = await async_create_schedule(
        _hass(),
        _entry(),
        title="X",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=0,
        end_event="sunrise",
        end_offset=0,
    )

    assert schedule_id == "te1"
    assert "new-scene-id" in caplog.text
    assert "CreatedById" in caplog.text


async def test_create_schedule_logs_when_createdby_backfill_raises(monkeypatch, caplog):
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-scene-id"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(side_effect=PlejdCloudError("down"))
    )

    schedule_id = await async_create_schedule(
        _hass(),
        _entry(),
        title="X",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=0,
        end_event="sunrise",
        end_offset=0,
    )

    assert schedule_id == "te1"
    assert "te1" in caplog.text


async def test_create_schedule_logs_when_night_reduction_createdby_backfill_is_rejected(monkeypatch, caplog):
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(side_effect=["on-scene-id", "night-scene-id"])
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    # The on-scene backfill succeeds, but the night-reduction scene's is rejected.
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(side_effect=[True, False]))

    schedule_id = await async_create_schedule(
        _hass(),
        _entry(),
        title="X",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=0,
        end_event="sunrise",
        end_offset=0,
        night_reduction={"scene_steps": [_NIGHT_STEP], "start_time": "23:00", "end_time": "05:00"},
    )

    assert schedule_id == "te1"
    assert "night-scene-id" in caplog.text
    assert "CreatedById" in caplog.text


async def test_create_schedule_fires_schedule_created_event_even_if_the_refresh_fails(monkeypatch):
    # The schedule is already created and persisted at the point the refresh runs - a
    # transient refresh failure must not also cost the user the only way to learn its id.
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_get_site", AsyncMock(side_effect=[_site(), PlejdCloudError("down")])
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-id"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))

    with pytest.raises(HomeAssistantError, match="Plejd cloud error refreshing site"):
        await async_create_schedule(
            hass,
            entry,
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )

    assert len(hass.bus.fired) == 1
    assert hass.bus.fired[0][0] == "plejd_schedule_created"
    assert hass.bus.fired[0][1]["schedule_id"] == entry.data["cloud_schedules"][0]["schedule_id"]


async def test_create_schedule_appends_to_an_existing_non_empty_cache(monkeypatch):
    # With only ever 0-or-1 cached schedules in other tests, a bug that dropped or replaced
    # the rest of the list (instead of appending) would still pass every other assertion.
    hass = _hass()
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [_cached_schedule(schedule_id="other", scene_id="s0", title="Hallway")],
        }
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-scene-id"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))

    await async_create_schedule(
        hass,
        entry,
        title="Garage",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=15,
        end_event="sunrise",
        end_offset=0,
    )

    cached = entry.data["cloud_schedules"]
    assert len(cached) == 2
    assert cached[0]["schedule_id"] == "other"
    assert cached[1]["scene_id"] == "new-scene-id"


async def test_create_schedule_serializes_concurrent_calls_via_a_lock(monkeypatch):
    # Two concurrent create_schedule calls against the same entry must not race on
    # reading+writing entry.data[CONF_CLOUD_SCHEDULES] - without the lock, both could read
    # the same pre-update list and the second write would silently drop the first schedule.
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))

    async def _get_site(*args, **kwargs):
        await asyncio.sleep(0)  # yield control so both calls get a chance to interleave
        return _site()

    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", _get_site)
    scene_ids = iter(["scene-a", "scene-b"])
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(side_effect=lambda *a, **k: next(scene_ids))
    )
    event_ids = iter(["te-a", "te-b"])
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event",
        AsyncMock(side_effect=lambda *a, **k: {"eventId": next(event_ids)}),
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))

    await asyncio.gather(
        async_create_schedule(
            hass,
            entry,
            title="A",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        ),
        async_create_schedule(
            hass,
            entry,
            title="B",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        ),
    )

    assert len(entry.data["cloud_schedules"]) == 2


async def test_create_schedule_with_night_reduction_on_a_different_device_marks_both_dirty(monkeypatch):
    # dirtyDevices must cover devices touched by EITHER scene, not just the primary on-scene -
    # a device that's only in the night-reduction scene still needs its cloud cache synced.
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(side_effect=["on-scene-id", "night-scene-id"])
    )
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_time_event", time_event_mock)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))
    night_step = {"device_id": "d2", "output": 0, "state": "Off", "value": 0}

    await async_create_schedule(
        hass,
        entry,
        title="Garage",
        scene_steps=[_STEP],  # device d1
        start_event="sunset",
        start_offset=15,
        end_event="sunrise",
        end_offset=0,
        night_reduction={"scene_steps": [night_step], "start_time": "23:00", "end_time": "05:00"},  # device d2
    )

    assert time_event_mock.await_args.kwargs["dirty_devices"] == ["d1", "d2"]
    assert entry.data["cloud_schedules"][0]["device_ids"] == ["d1"]
    assert entry.data["cloud_schedules"][0]["night_reduction"]["device_ids"] == ["d2"]


async def test_create_schedule_deduplicates_and_sorts_scheduled_days(monkeypatch):
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-scene-id"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))

    await async_create_schedule(
        hass,
        entry,
        title="X",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=0,
        end_event="sunrise",
        end_offset=0,
        scheduled_days=[3, 1, 1],
    )

    assert entry.data["cloud_schedules"][0]["scheduled_days"] == [1, 3]


async def test_create_schedule_cleanup_swallows_a_failed_removal(monkeypatch):
    # The cleanup after a rejected time-event call is best-effort: if the compensating
    # remove_scene call itself fails, the original "rejected" error must still surface.
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-scene-id"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_remove_scene", AsyncMock(side_effect=PlejdCloudError("gone already"))
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud rejected the schedule creation"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )


async def test_create_schedule_logs_when_cloud_rejects_cleanup_removal(monkeypatch, caplog):
    # async_cloud_remove_scene returning False (Parse rejected removeScene, no exception)
    # must not be treated as a silent success - the orphaned scene needs a support trail too.
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-scene-id"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value=None))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_scene", AsyncMock(return_value=False))

    with pytest.raises(HomeAssistantError, match="Plejd cloud rejected the schedule creation"):
        await async_create_schedule(
            _hass(),
            _entry(),
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )
    assert "new-scene-id" in caplog.text
    assert "rejected cleanup" in caplog.text


async def test_create_schedule_with_night_reduction_creates_both_scenes(monkeypatch):
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    create_mock = AsyncMock(side_effect=["on-scene-id", "night-scene-id"])
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", create_mock)
    create_time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_time_event", create_time_event_mock)
    update_scene_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", update_scene_mock)

    await async_create_schedule(
        hass,
        entry,
        title="Garage",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=15,
        end_event="sunrise",
        end_offset=0,
        night_reduction={"scene_steps": [_NIGHT_STEP], "start_time": "23:15", "end_time": "05:30"},
    )

    assert create_mock.await_count == 2
    night_call = create_mock.await_args_list[1]
    assert night_call.args[3] == "Garage Nattläge"
    assert night_call.kwargs["settings"] == '{"SceneType": "NightReductionScene"}'
    night_reduction_sent = create_time_event_mock.await_args.kwargs["night_reduction"]
    assert night_reduction_sent["scene_id"] == "night-scene-id"
    assert night_reduction_sent["start_time"] == "23:15"
    # Both the on-scene and the night-reduction scene get their CreatedById backfilled.
    assert update_scene_mock.await_count == 2
    assert update_scene_mock.await_args_list[0].args[3] == "on-scene-id"
    assert update_scene_mock.await_args_list[1].args[3] == "night-scene-id"
    cached = entry.data["cloud_schedules"][0]["night_reduction"]
    assert cached["scene_id"] == "night-scene-id"


async def test_create_schedule_raises_on_cloud_error_during_refresh(monkeypatch):
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_get_site", AsyncMock(side_effect=[_site(), PlejdCloudError("down")])
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-id"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))
    with pytest.raises(HomeAssistantError, match="Plejd cloud error refreshing site"):
        await async_create_schedule(
            hass,
            entry,
            title="X",
            scene_steps=[_STEP],
            start_event="sunset",
            start_offset=0,
            end_event="sunrise",
            end_offset=0,
        )
    # The schedule was already created on the cloud before the refresh failed - losing this
    # write would leave it untracked forever, since it can't be rediscovered from getSiteById.
    assert entry.data["cloud_schedules"][0]["scene_id"] == "new-id"


async def test_create_schedule_runs_a_follow_up_reload_for_a_concurrent_change(monkeypatch):
    hass = _hass()
    entry = _entry()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="new-id"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))

    calls: list[str] = []

    async def _reload_sets_pending(entry_id):
        calls.append(entry_id)
        if len(calls) == 1:
            hass.data[schedule_ws.DATA_RELOAD_PENDING] = entry_id
        return True

    hass.config_entries.async_reload = AsyncMock(side_effect=_reload_sets_pending)

    await async_create_schedule(
        hass,
        entry,
        title="X",
        scene_steps=[_STEP],
        start_event="sunset",
        start_offset=0,
        end_event="sunrise",
        end_offset=0,
    )

    assert hass.config_entries.async_reload.await_count == 2
    assert schedule_ws.DATA_RELOAD_PENDING not in hass.data


# --- update_schedule ---


async def test_update_schedule_raises_if_not_tracked():
    with pytest.raises(HomeAssistantError, match="isn't tracked by this integration"):
        await async_update_schedule(_hass(), _entry(), schedule_id="missing", title="X")


async def test_update_schedule_raises_when_no_fields_given():
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    with pytest.raises(HomeAssistantError, match="at least one field"):
        await async_update_schedule(_hass(), entry, schedule_id="te1")


async def test_update_schedule_raises_on_invalid_start_offset():
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    with pytest.raises(HomeAssistantError, match="start_offset must be between"):
        await async_update_schedule(_hass(), entry, schedule_id="te1", start_offset=999)


async def test_update_schedule_raises_on_empty_scheduled_days():
    # services.yaml documents this as a "full replacement list" - an explicit empty list must
    # be rejected, not silently reactivate the schedule for every day (the previous bug).
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    with pytest.raises(HomeAssistantError, match="scheduled_days must include at least one day"):
        await async_update_schedule(_hass(), entry, schedule_id="te1", scheduled_days=[])


async def test_update_schedule_rejects_empty_scene_steps():
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    with pytest.raises(HomeAssistantError, match="at least one"):
        await async_update_schedule(_hass(), entry, schedule_id="te1", scene_steps=[])


async def test_update_schedule_rejects_night_reduction_without_steps():
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    with pytest.raises(HomeAssistantError, match="night_reduction needs at least one scene step"):
        await async_update_schedule(
            _hass(),
            entry,
            schedule_id="te1",
            night_reduction={"scene_steps": [], "start_time": "23:00", "end_time": "05:00"},
        )


async def test_update_schedule_raises_if_scene_not_found_on_site(monkeypatch):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([])))
    with pytest.raises(HomeAssistantError, match="not found on this site"):
        await async_update_schedule(_hass(), entry, schedule_id="te1", title="Renamed")


async def test_update_schedule_raises_if_cached_night_reduction_scene_missing_from_site(monkeypatch):
    existing = _cached_schedule(
        night_reduction={
            "scene_id": "night1",
            "start_time": "23:00",
            "end_time": "05:00",
            "weekend_start_time": None,
            "weekend_end_time": None,
        }
    )
    entry = _entry(data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [existing]})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    # Site has the on-scene but not the cached night-reduction scene - even an update that
    # doesn't touch night_reduction must not silently resend a dangling night_reduction scene_id.
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    with pytest.raises(HomeAssistantError, match="night1.*not found on this site"):
        await async_update_schedule(_hass(), entry, schedule_id="te1", start_offset=30)


async def test_update_schedule_raises_when_cloud_rejects_scene_update(monkeypatch):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="rejected the schedule's scene update"):
        await async_update_schedule(_hass(), entry, schedule_id="te1", title="Renamed")


async def test_update_schedule_raises_on_cloud_error_updating_scene(monkeypatch):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(side_effect=PlejdCloudError("down"))
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error updating schedule"):
        await async_update_schedule(_hass(), entry, schedule_id="te1", title="Renamed")


async def test_update_schedule_raises_when_cloud_rejects_time_event_update(monkeypatch):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value=None))
    with pytest.raises(HomeAssistantError, match="rejected the schedule update"):
        await async_update_schedule(_hass(), entry, schedule_id="te1", start_offset=30)


async def test_update_schedule_partial_update_resends_full_state(monkeypatch):
    hass = _hass()
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    scene_update_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", scene_update_mock)
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", time_event_mock)

    await async_update_schedule(hass, entry, schedule_id="te1", start_offset=30)

    scene_update_mock.assert_not_awaited()  # neither title nor scene_steps changed
    time_event_mock.assert_awaited_once_with(
        None,
        "tok",
        "S1",
        "te1",
        "s1",
        scheduled_days=[0, 1, 2, 3, 4, 5, 6],
        fade_time=0,
        activated=True,
        start_event="sunset",
        start_offset=30,
        end_event="sunrise",
        end_offset=0,
        dirty_devices=[],
        dirty_removed_devices=[],
        night_reduction=None,
    )
    cached = entry.data["cloud_schedules"][0]
    assert cached["start_offset"] == 30
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


async def test_update_schedule_preserves_non_null_night_reduction_on_unrelated_update(monkeypatch):
    # A regression that derived night_reduction_result from the (absent) night_reduction
    # parameter instead of falling back to the cached value would silently wipe it here.
    hass = _hass()
    existing_nr = {
        "scene_id": "night1",
        "start_time": "23:00",
        "end_time": "05:00",
        "weekend_start_time": None,
        "weekend_end_time": None,
    }
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [_cached_schedule(night_reduction=existing_nr)],
        }
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_get_site",
        AsyncMock(return_value=_site([_scene(), _scene(scene_id="night1", name="Garage Nattläge")])),
    )
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", time_event_mock)

    await async_update_schedule(hass, entry, schedule_id="te1", start_offset=30)

    assert time_event_mock.await_args.kwargs["night_reduction"] == existing_nr
    assert entry.data["cloud_schedules"][0]["night_reduction"] == existing_nr


async def test_update_schedule_targets_only_the_matching_schedule_in_a_multi_schedule_cache(monkeypatch):
    hass = _hass()
    other = _cached_schedule(schedule_id="other", scene_id="s0", title="Hallway")
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [other, _cached_schedule()],
        }
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value={"eventId": "te1"})
    )

    await async_update_schedule(hass, entry, schedule_id="te1", start_offset=30)

    cached = entry.data["cloud_schedules"]
    assert len(cached) == 2
    assert cached[0] == other  # untouched
    assert cached[1]["start_offset"] == 30


async def test_update_schedule_persists_a_succeeded_scene_rename_even_if_the_trigger_update_is_rejected(monkeypatch):
    # The scene rename is a separate, independently-committed cloud call from the trigger
    # update - if the trigger update is later rejected, the rename must not be lost from the
    # local cache, or a later read of the schedule's title would be stale relative to the cloud.
    hass = _hass()
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value=None))

    with pytest.raises(HomeAssistantError, match="rejected the schedule update"):
        await async_update_schedule(hass, entry, schedule_id="te1", title="Renamed", start_offset=30)

    # The rename already succeeded on the cloud before the trigger update was rejected -
    # entry.data must reflect that, not silently keep showing the pre-rename title.
    assert entry.data["cloud_schedules"][0]["title"] == "Renamed"
    # But fields that only take effect via the (rejected) trigger update stay unchanged.
    assert entry.data["cloud_schedules"][0]["start_offset"] == 15
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


async def test_update_schedule_title_only_does_not_resend_the_time_event(monkeypatch):
    # A title-only rename must not resend the cached whole-state TimeEvent payload - doing so
    # would silently overwrite trigger/night-reduction data if the schedule was since edited
    # in the Plejd app (this integration has no way to detect that).
    hass = _hass()
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", time_event_mock)

    await async_update_schedule(hass, entry, schedule_id="te1", title="Renamed")

    time_event_mock.assert_not_awaited()
    assert entry.data["cloud_schedules"][0]["title"] == "Renamed"
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


async def test_update_schedule_renames_scene_and_updates_days(monkeypatch):
    hass = _hass()
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    scene_update_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", scene_update_mock)
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", time_event_mock)

    await async_update_schedule(
        hass, entry, schedule_id="te1", title="Renamed", scene_steps=[_STEP], scheduled_days=[0]
    )

    scene_update_mock.assert_awaited_once_with(None, "tok", "S1", "s1", title="Renamed", scene_steps=[_STEP])
    # scene_steps changed -> the new device membership must be resent as dirty_devices, not [].
    assert time_event_mock.await_args.kwargs["dirty_devices"] == ["d1"]
    assert entry.data["cloud_schedules"][0]["title"] == "Renamed"
    assert entry.data["cloud_schedules"][0]["scheduled_days"] == [0]
    assert entry.data["cloud_schedules"][0]["device_ids"] == ["d1"]


async def test_update_schedule_marks_a_dropped_device_as_dirty_removed(monkeypatch):
    hass = _hass()
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [_cached_schedule(device_ids=["d1", "d2"])],
        }
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", time_event_mock)

    # Replacing scene_steps with only d1 drops d2 - the cloud must be told d2 is no longer dirty.
    await async_update_schedule(hass, entry, schedule_id="te1", scene_steps=[_STEP])

    assert time_event_mock.await_args.kwargs["dirty_devices"] == ["d1"]
    assert time_event_mock.await_args.kwargs["dirty_removed_devices"] == ["d2"]


async def test_update_schedule_keeps_old_device_ids_if_the_trigger_update_is_rejected(monkeypatch):
    # The scene edit (dropping d2) really did land on the cloud, but the trigger update that
    # would tell the cloud d2 is no longer dirty was rejected - caching the NEW device_ids
    # anyway would make a later retry think d2 was already reported removed and never resend
    # dirtyRemovedDevices for it.
    hass = _hass()
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [_cached_schedule(device_ids=["d1", "d2"])],
        }
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value=None))

    with pytest.raises(HomeAssistantError, match="rejected the schedule update"):
        await async_update_schedule(hass, entry, schedule_id="te1", scene_steps=[_STEP])

    assert entry.data["cloud_schedules"][0]["device_ids"] == ["d1", "d2"]


async def test_update_schedule_adds_night_reduction_creating_its_scene(monkeypatch):
    hass = _hass()
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    create_mock = AsyncMock(return_value="night-scene-id")
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", create_mock)
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", time_event_mock)

    await async_update_schedule(
        hass,
        entry,
        schedule_id="te1",
        night_reduction={"scene_steps": [_NIGHT_STEP], "start_time": "23:15", "end_time": "05:30"},
    )

    create_mock.assert_awaited_once_with(
        None,
        "tok",
        "S1",
        "Garage Nattläge",
        [_NIGHT_STEP],
        hidden_from_scene_list=True,
        settings=create_mock.await_args.kwargs["settings"],
    )
    assert '"SceneType": "NightReductionScene"' in create_mock.await_args.kwargs["settings"]
    sent_nr = time_event_mock.await_args.kwargs["night_reduction"]
    assert sent_nr["scene_id"] == "night-scene-id"
    cached_nr = entry.data["cloud_schedules"][0]["night_reduction"]
    assert cached_nr["scene_id"] == "night-scene-id"


async def test_update_schedule_cleans_up_a_new_night_reduction_scene_if_the_trigger_update_is_rejected(monkeypatch):
    # The night-reduction scene is created before the trailing updateTimeEvent_V3 call - if
    # that call is then rejected, the freshly-created scene must not become a silently
    # orphaned/untracked object: it should be removed, and the cache must not claim the
    # (never actually applied) night reduction is now active.
    hass = _hass()
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_create_scene", AsyncMock(return_value="night-scene-id")
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value=None))
    remove_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_scene", remove_mock)

    with pytest.raises(HomeAssistantError, match="rejected the schedule update"):
        await async_update_schedule(
            hass,
            entry,
            schedule_id="te1",
            night_reduction={"scene_steps": [_NIGHT_STEP], "start_time": "23:15", "end_time": "05:30"},
        )

    remove_mock.assert_awaited_once_with(None, "tok", "S1", "night-scene-id")
    assert entry.data["cloud_schedules"][0]["night_reduction"] is None


async def test_update_schedule_updates_existing_night_reduction_scene(monkeypatch):
    hass = _hass()
    existing = _cached_schedule(
        night_reduction={
            "scene_id": "night1",
            "start_time": "23:00",
            "end_time": "05:00",
            "weekend_start_time": None,
            "weekend_end_time": None,
        }
    )
    entry = _entry(data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [existing]})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_get_site",
        AsyncMock(return_value=_site([_scene(), _scene(scene_id="night1", name="Garage Nattläge")])),
    )
    create_mock = AsyncMock()
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_create_scene", create_mock)
    night_update_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", night_update_mock)
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", time_event_mock)

    await async_update_schedule(
        hass,
        entry,
        schedule_id="te1",
        night_reduction={"scene_steps": [_NIGHT_STEP], "start_time": "23:30", "end_time": "05:15"},
    )

    create_mock.assert_not_awaited()
    night_update_mock.assert_awaited_once_with(None, "tok", "S1", "night1", scene_steps=[_NIGHT_STEP])
    sent_nr = time_event_mock.await_args.kwargs["night_reduction"]
    assert sent_nr == {
        "scene_id": "night1",
        "device_ids": ["d1"],
        "start_time": "23:30",
        "end_time": "05:15",
        "weekend_start_time": None,
        "weekend_end_time": None,
    }


async def test_update_schedule_raises_when_cloud_rejects_night_reduction_scene_update(monkeypatch):
    existing = _cached_schedule(
        night_reduction={
            "scene_id": "night1",
            "start_time": "23:00",
            "end_time": "05:00",
            "weekend_start_time": None,
            "weekend_end_time": None,
        }
    )
    entry = _entry(data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [existing]})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_get_site",
        AsyncMock(return_value=_site([_scene(), _scene(scene_id="night1", name="Garage Nattläge")])),
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="rejected the night-reduction scene update"):
        await async_update_schedule(
            _hass(),
            entry,
            schedule_id="te1",
            night_reduction={"scene_steps": [_NIGHT_STEP], "start_time": "23:30", "end_time": "05:15"},
        )


async def test_update_schedule_activated_and_fade_time_default_to_cached_values(monkeypatch):
    hass = _hass()
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [_cached_schedule(fade_time=5, activated=False)],
        }
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site([_scene()])))
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", time_event_mock)

    await async_update_schedule(hass, entry, schedule_id="te1", activated=True)

    assert time_event_mock.await_args.kwargs["fade_time"] == 5
    assert time_event_mock.await_args.kwargs["activated"] is True
    assert entry.data["cloud_schedules"][0]["activated"] is True
    assert entry.data["cloud_schedules"][0]["fade_time"] == 5


async def test_update_schedule_raises_on_cloud_error_during_refresh(monkeypatch):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_get_site",
        AsyncMock(side_effect=[_site([_scene()]), PlejdCloudError("down")]),
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error refreshing site"):
        await async_update_schedule(_hass(), entry, schedule_id="te1", start_offset=30)
    # The trigger update already succeeded on the cloud before the refresh failed - the local
    # cache must still reflect it, not silently revert to the pre-update state.
    assert entry.data["cloud_schedules"][0]["start_offset"] == 30


async def test_update_schedule_preserves_the_original_error_when_refresh_also_fails(monkeypatch, caplog):
    # A refresh failure occurring while an earlier update failure is already pending must not
    # replace it - the user needs to see why the update itself failed, not a later, unrelated
    # refresh outage.
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_get_site",
        AsyncMock(side_effect=[_site([_scene()]), PlejdCloudError("refresh down")]),
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value=None))

    with pytest.raises(HomeAssistantError, match="rejected the schedule update"):
        await async_update_schedule(_hass(), entry, schedule_id="te1", start_offset=30)

    assert "refresh" in caplog.text


async def test_update_schedule_serializes_concurrent_calls_to_the_same_schedule(monkeypatch):
    # Two concurrent update_schedule calls for the same schedule must not race on the
    # read-build-send-persist sequence: without full serialization, both could send a
    # whole-state cloud payload built from the same stale snapshot, and the later one to
    # persist locally could roll back the other's already-applied change.
    hass = _hass()
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))

    async def _get_site(*args, **kwargs):
        await asyncio.sleep(0)
        return _site([_scene()])

    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", _get_site)
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value={"eventId": "te1"})
    )

    await asyncio.gather(
        async_update_schedule(hass, entry, schedule_id="te1", fade_time=5),
        async_update_schedule(hass, entry, schedule_id="te1", activated=False),
    )

    cached = entry.data["cloud_schedules"][0]
    assert cached["fade_time"] == 5
    assert cached["activated"] is False


# --- remove_schedule ---


async def test_remove_schedule_raises_if_not_tracked():
    with pytest.raises(HomeAssistantError, match="isn't tracked by this integration"):
        await async_remove_schedule(_hass(), _entry(), schedule_id="missing")


async def test_remove_schedule_raises_on_cloud_error_marking_dirty_remove(monkeypatch):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(side_effect=PlejdCloudError("down"))
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error removing schedule"):
        await async_remove_schedule(_hass(), entry, schedule_id="te1")


async def test_remove_schedule_raises_when_dirty_remove_rejected(monkeypatch):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value=None))
    with pytest.raises(HomeAssistantError, match="Plejd cloud rejected removing schedule te1"):
        await async_remove_schedule(_hass(), entry, schedule_id="te1")


async def test_remove_schedule_sends_dirty_remove_with_empty_devices(monkeypatch):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", time_event_mock)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_time_event", AsyncMock(return_value=True))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_scene", AsyncMock(return_value=True))

    await async_remove_schedule(_hass(), entry, schedule_id="te1")

    time_event_mock.assert_awaited_once_with(
        None,
        "tok",
        "S1",
        "te1",
        "s1",
        scheduled_days=[0, 1, 2, 3, 4, 5, 6],
        fade_time=0,
        activated=True,
        start_event="sunset",
        start_offset=15,
        end_event="sunrise",
        end_offset=0,
        dirty_devices=[],
        dirty_removed_devices=[],
        dirty_remove=True,
        night_reduction=None,
    )


async def test_remove_schedule_raises_on_cloud_error_calling_remove_time_event(monkeypatch):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_remove_time_event", AsyncMock(side_effect=PlejdCloudError("down"))
    )
    with pytest.raises(HomeAssistantError, match="Plejd cloud error removing schedule"):
        await async_remove_schedule(_hass(), entry, schedule_id="te1")


async def test_remove_schedule_raises_when_remove_time_event_rejected(monkeypatch):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_time_event", AsyncMock(return_value=False))
    with pytest.raises(HomeAssistantError, match="Plejd cloud rejected removing schedule te1"):
        await async_remove_schedule(_hass(), entry, schedule_id="te1")


async def test_remove_schedule_removes_on_scene_and_updates_cache(monkeypatch):
    hass = _hass()
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [_cached_schedule(device_ids=["d1"])],
        }
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    remove_time_event_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_time_event", remove_time_event_mock)
    update_scene_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", update_scene_mock)
    remove_scene_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_scene", remove_scene_mock)

    await async_remove_schedule(hass, entry, schedule_id="te1")

    remove_time_event_mock.assert_awaited_once_with(None, "tok", "S1", "te1", device_ids=["d1"])
    update_scene_mock.assert_awaited_once_with(None, "tok", "S1", "s1", scene_steps=[])
    remove_scene_mock.assert_awaited_once_with(None, "tok", "S1", "s1")
    assert entry.data["cloud_schedules"] == []
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


async def test_remove_schedule_also_removes_night_reduction_scene(monkeypatch):
    entry = _entry(
        data={
            "email": "u@x.com",
            "password": "pw",
            "site_id": "S1",
            "cloud_schedules": [
                _cached_schedule(
                    device_ids=["d1"],
                    night_reduction={
                        "scene_id": "night1",
                        "device_ids": ["d2"],
                        "start_time": "23:00",
                        "end_time": "05:00",
                        "weekend_start_time": None,
                        "weekend_end_time": None,
                    },
                )
            ],
        }
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    time_event_mock = AsyncMock(return_value={"eventId": "te1"})
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_time_event", time_event_mock)
    remove_time_event_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_time_event", remove_time_event_mock)
    update_scene_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", update_scene_mock)
    remove_scene_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_scene", remove_scene_mock)

    await async_remove_schedule(_hass(), entry, schedule_id="te1")

    # removeTimeEvent_V3's deviceIds covers both the on-scene and night-reduction devices.
    remove_time_event_mock.assert_awaited_once_with(None, "tok", "S1", "te1", device_ids=["d1", "d2"])
    assert update_scene_mock.await_count == 2
    assert {c.args[3] for c in update_scene_mock.await_args_list} == {"s1", "night1"}
    assert remove_scene_mock.await_count == 2
    assert {c.args[3] for c in remove_scene_mock.await_args_list} == {"s1", "night1"}


async def test_remove_schedule_logs_when_scene_cleanup_raises(monkeypatch, caplog):
    # removeTimeEvent_V3 already succeeded - the schedule itself is gone, so a failure
    # cleaning up its scene afterward must be logged, not raised.
    hass = _hass()
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_time_event", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(side_effect=PlejdCloudError("down"))
    )

    await async_remove_schedule(hass, entry, schedule_id="te1")

    assert "s1" in caplog.text
    assert entry.data["cloud_schedules"] == []  # the schedule is still considered removed


async def test_remove_schedule_logs_when_scene_removal_is_rejected(monkeypatch, caplog):
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_time_event", AsyncMock(return_value=True))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_scene", AsyncMock(return_value=False))

    await async_remove_schedule(_hass(), entry, schedule_id="te1")

    assert "s1" in caplog.text
    assert "rejected removing" in caplog.text


async def test_remove_schedule_serializes_via_the_lock(monkeypatch):
    # Uses the same per-entry lock as create/update - just confirm it's acquired without
    # deadlocking or interfering with a normal single removal.
    hass = _hass()
    entry = _entry(
        data={"email": "u@x.com", "password": "pw", "site_id": "S1", "cloud_schedules": [_cached_schedule()]}
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_login", AsyncMock(return_value="tok"))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_get_site", AsyncMock(return_value=_site()))
    monkeypatch.setattr(
        "plejd_cloud.manage_schedule.async_cloud_update_time_event", AsyncMock(return_value={"eventId": "te1"})
    )
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_time_event", AsyncMock(return_value=True))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_update_scene", AsyncMock(return_value=True))
    monkeypatch.setattr("plejd_cloud.manage_schedule.async_cloud_remove_scene", AsyncMock(return_value=True))

    await async_remove_schedule(hass, entry, schedule_id="te1")

    assert entry.data["cloud_schedules"] == []


async def test_sync_cloud_schedules_cache_runs_a_follow_up_reload_for_a_concurrent_change():
    hass = _hass()
    entry = _entry()
    hass.data[schedule_ws.DATA_RELOAD_PENDING] = "e1"

    await _sync_cloud_schedules_cache(hass, entry, [])

    hass.config_entries.async_reload.assert_awaited_once_with("e1")
    assert schedule_ws.DATA_RELOAD_PENDING not in hass.data


async def test_sync_cloud_schedules_cache_logs_when_the_follow_up_reload_raises(caplog):
    hass = _hass()
    entry = _entry()
    hass.data[schedule_ws.DATA_RELOAD_PENDING] = "e1"
    hass.config_entries.async_reload = AsyncMock(side_effect=RuntimeError("boom"))

    await _sync_cloud_schedules_cache(hass, entry, [])

    assert "follow-up reload for a concurrent change failed" in caplog.text
