"""Plejd dashboard: a custom Home Assistant sidebar panel served from www/panel.js (built from frontend/).

The JS is served once per Home Assistant run; the sidebar entry itself is added/removed
so the dashboard can be shown or hidden from the left navbar via the integration options.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

PANEL_URL_PATH = "plejd"
# A URL prefix mapped to the www/ *directory* (HA static paths serve directories);
# the panel module is then served as a file within it.
PANEL_STATIC_URL = "/plejd_dashboard"
WWW = Path(__file__).parent / "www"
PANEL_MODULE_URL = f"{PANEL_STATIC_URL}/panel.js"
PANEL_TITLE = "Plejd"
PANEL_ICON = "mdi:lightbulb-group"

_STATIC_KEY = f"{DOMAIN}_panel_static"
_PANEL_KEY = f"{DOMAIN}_panel_registered"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve the panel JS (once) and add the Plejd entry to the sidebar."""
    if not hass.data.get(_STATIC_KEY):
        await hass.http.async_register_static_paths([StaticPathConfig(PANEL_STATIC_URL, str(WWW), cache_headers=False)])
        hass.data[_STATIC_KEY] = True
    if hass.data.get(_PANEL_KEY):
        return  # already in the sidebar
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="plejd-panel",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        # The build always emits panel.js; the mtime makes browsers fetch a new build after an update.
        module_url=f"{PANEL_MODULE_URL}?v={int((WWW / 'panel.js').stat().st_mtime)}",
        require_admin=True,  # a configuration dashboard — admin only
    )
    hass.data[_PANEL_KEY] = True


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the Plejd sidebar entry (the served JS stays, just hidden from the navbar)."""
    if not hass.data.get(_PANEL_KEY):
        return
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    hass.data[_PANEL_KEY] = False
