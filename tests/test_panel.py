"""Tests for the Plejd sidebar dashboard panel registration."""

from __future__ import annotations

import types

from plejd_cloud import panel


class _Http:
    def __init__(self):
        self.static = []

    async def async_register_static_paths(self, configs):
        self.static.append(configs)


def _spy_register(recorder):
    async def _reg(hass, **kw):
        recorder.append(kw)

    return _reg


async def test_register_serves_static_once_and_adds_sidebar(monkeypatch):
    reg = []
    monkeypatch.setattr(panel.panel_custom, "async_register_panel", _spy_register(reg))
    hass = types.SimpleNamespace(http=_Http(), data={})
    await panel.async_register_panel(hass)
    await panel.async_register_panel(hass)  # idempotent
    assert len(hass.http.static) == 1  # JS served exactly once
    assert hass.http.static[0][0].url_path == panel.PANEL_STATIC_URL
    assert len(reg) == 1  # sidebar entry added exactly once
    assert reg[0]["frontend_url_path"] == panel.PANEL_URL_PATH
    assert reg[0]["webcomponent_name"] == "plejd-panel"
    assert reg[0]["module_url"].startswith(f"{panel.PANEL_MODULE_URL}?v=")  # cache-busted per build
    assert reg[0]["sidebar_title"] == panel.PANEL_TITLE
    assert reg[0]["require_admin"] is True  # configuration dashboard → admin only
    assert hass.data[panel._PANEL_KEY] is True


async def test_register_skips_sidebar_but_reuses_served_js(monkeypatch):
    reg = []
    monkeypatch.setattr(panel.panel_custom, "async_register_panel", _spy_register(reg))
    # static already served, panel not yet in sidebar
    hass = types.SimpleNamespace(http=_Http(), data={panel._STATIC_KEY: True})
    await panel.async_register_panel(hass)
    assert hass.http.static == []  # did not re-serve the JS
    assert len(reg) == 1  # but did add the sidebar entry


def test_unregister_removes_sidebar_and_is_idempotent(monkeypatch):
    removed = []
    monkeypatch.setattr(panel.frontend, "async_remove_panel", lambda hass, url, **kw: removed.append(url))
    hass = types.SimpleNamespace(data={panel._PANEL_KEY: True})
    panel.async_unregister_panel(hass)
    assert removed == [panel.PANEL_URL_PATH]
    assert hass.data[panel._PANEL_KEY] is False
    panel.async_unregister_panel(hass)  # already gone → no-op
    assert removed == [panel.PANEL_URL_PATH]
