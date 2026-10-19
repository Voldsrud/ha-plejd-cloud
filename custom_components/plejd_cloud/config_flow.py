"""Config flow for the Plejd integration."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
)

from .cloud import (
    PlejdAuthError,
    PlejdCloudError,
    async_get_site,
    async_get_sites,
    async_login,
)
from .const import (
    CONF_CRYPTO_KEY,
    CONF_DEVICE_ADDRESSES,
    CONF_DEVICES,
    CONF_DISCOVERED_ADDRESS,
    CONF_GATEWAYS,
    CONF_INPUTS,
    CONF_INSTALLATION_ID,
    CONF_MOTION,
    CONF_RESOURCE_SET_ID,
    CONF_ROOM_NAMES,
    CONF_ROOMS,
    CONF_SCENES,
    CONF_SHOW_PANEL,
    CONF_SITE_ID,
    DOMAIN,
)
from .coordinator import async_clear_malformed_site_issue, async_reset_self_heal_cooldown

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _site_id(item: dict) -> str:
    # getSiteList items nest the id/title under "site" (validated against the API).
    return (item.get("site") or item)["siteId"]


def _site_title(item: dict) -> str:
    site = item.get("site") or item
    return site.get("title") or site["siteId"]


class PlejdConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Plejd."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow: show or hide the dashboard."""
        return PlejdOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._email: str = ""
        self._password: str = ""
        self._token: str = ""
        self._sites: list[dict] = []

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        # A Plejd mesh device is in range — remember it, then ask for the account login.
        self._discovered_address = discovery_info.address
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]
            session = async_get_clientsession(self.hass)
            try:
                self._token = await async_login(session, self._email, self._password)
            except PlejdAuthError:
                errors["base"] = "invalid_auth"
            except (PlejdCloudError, ClientError, OSError):
                _LOGGER.debug("Plejd cloud login request failed", exc_info=True)
                errors["base"] = "cannot_connect"
            else:
                try:
                    self._sites = await async_get_sites(session, self._token)
                except (PlejdCloudError, ClientError, OSError):
                    _LOGGER.debug("Plejd site-list fetch failed", exc_info=True)
                    errors["base"] = "cannot_connect"
                else:
                    if not self._sites:
                        errors["base"] = "no_sites"
                    elif len(self._sites) == 1:
                        return await self._create_entry(_site_id(self._sites[0]))
                    else:
                        return await self.async_step_site()
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._create_entry(user_input[CONF_SITE_ID])
        options = [{"value": _site_id(s), "label": _site_title(s)} for s in self._sites]
        schema = vol.Schema({vol.Required(CONF_SITE_ID): SelectSelector(SelectSelectorConfig(options=options))})
        return self.async_show_form(step_id="site", data_schema=schema)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Re-fetch the site's device list from the Plejd cloud."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                token = await async_login(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
                site = await async_get_site(session, token, entry.data[CONF_SITE_ID])
            except PlejdAuthError:
                errors["base"] = "invalid_auth"
            except (PlejdCloudError, ClientError, OSError):
                _LOGGER.debug("Plejd reconfigure: cloud unreachable", exc_info=True)
                errors["base"] = "cannot_connect"
            else:
                if site.malformed:
                    # Replacing the cached snapshot with normalized-empty collections would
                    # remove every device/scene/room entity of the affected kind - refuse the
                    # response the same way an unreachable cloud is refused.
                    _LOGGER.debug("Plejd reconfigure: site response malformed (%s)", ", ".join(sorted(site.malformed)))
                    return self.async_show_form(
                        step_id="reconfigure", data_schema=vol.Schema({}), errors={"base": "cannot_connect"}
                    )
                data_updates = {
                    CONF_CRYPTO_KEY: site.crypto_key.hex(),
                    CONF_DEVICES: [asdict(d) for d in site.devices],
                    CONF_INPUTS: [asdict(i) for i in site.inputs],
                    CONF_MOTION: [asdict(m) for m in site.motion],
                    CONF_SCENES: [asdict(s) for s in site.scenes],
                    CONF_ROOMS: [asdict(r) for r in site.rooms],
                    CONF_ROOM_NAMES: {r.room_id: r.name for r in site.all_rooms},
                    CONF_GATEWAYS: site.gateways,
                    CONF_RESOURCE_SET_ID: site.resource_set_id,
                    CONF_DEVICE_ADDRESSES: site.device_addresses,
                }
                # A gateway newly appearing on an entry that predates CONF_INSTALLATION_ID
                # (or never had one) must seed it now - the gateway transport requires it.
                if site.gateways and not entry.data.get(CONF_INSTALLATION_ID):
                    data_updates[CONF_INSTALLATION_ID] = str(uuid4())
                # Reaching here means the cloud just served a usable site, which is exactly
                # what the malformed-cloud repair asks the user to confirm - clear it now
                # rather than leaving the warning up until the new coordinator's first
                # scheduled poll, a day later.
                async_clear_malformed_site_issue(self.hass, entry.entry_id)
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=data_updates,
                    reason="reconfigure_successful",
                )
        return self.async_show_form(step_id="reconfigure", data_schema=vol.Schema({}), errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Re-authentication started (e.g. the gateway rejected stored credentials)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the password again and verify it against the Plejd cloud."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                await async_login(session, entry.data[CONF_EMAIL], user_input[CONF_PASSWORD])
            except PlejdAuthError:
                errors["base"] = "invalid_auth"
            except (PlejdCloudError, ClientError, OSError):
                _LOGGER.debug("Plejd reauth: cloud unreachable", exc_info=True)
                errors["base"] = "cannot_connect"
            else:
                # The credentials work again, so let the setup retry that follows attempt a
                # self-heal immediately instead of waiting out a cooldown recorded while
                # they were still bad - reauth only replaces the password, so that retry
                # still needs the crypto key/gateway data it could not fetch before.
                async_reset_self_heal_cooldown(self.hass, entry.entry_id)
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                    reason="reauth_successful",
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
        )

    async def _create_entry(self, site_id: str) -> ConfigFlowResult:
        session = async_get_clientsession(self.hass)
        try:
            site = await async_get_site(session, self._token, site_id)
        except (PlejdCloudError, ClientError, OSError):
            _LOGGER.debug("Plejd site fetch failed during setup", exc_info=True)
            return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors={"base": "cannot_connect"})
        if site.malformed:
            # A truncated/wrong-typed collection parses into an empty one, so setting up on it
            # would create an entry missing entire device/scene/room sets. Fail like any other
            # bad response and let the user retry instead.
            _LOGGER.debug("Plejd site response malformed during setup (%s)", ", ".join(sorted(site.malformed)))
            return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors={"base": "cannot_connect"})
        await self.async_set_unique_id(site.site_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=site.title,
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_SITE_ID: site.site_id,
                CONF_CRYPTO_KEY: site.crypto_key.hex(),
                CONF_DISCOVERED_ADDRESS: self._discovered_address,
                CONF_DEVICES: [asdict(d) for d in site.devices],
                CONF_INPUTS: [asdict(i) for i in site.inputs],
                CONF_MOTION: [asdict(m) for m in site.motion],
                CONF_SCENES: [asdict(s) for s in site.scenes],
                CONF_ROOMS: [asdict(r) for r in site.rooms],
                CONF_ROOM_NAMES: {r.room_id: r.name for r in site.all_rooms},
                CONF_GATEWAYS: site.gateways,
                CONF_RESOURCE_SET_ID: site.resource_set_id,
                CONF_DEVICE_ADDRESSES: site.device_addresses,
                CONF_INSTALLATION_ID: str(uuid4()),
            },
        )


class PlejdOptionsFlow(OptionsFlow):
    """Show or hide the Plejd dashboard; everything else is configured in the dashboard itself."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="", data={**self._entry.options, CONF_SHOW_PANEL: user_input[CONF_SHOW_PANEL]}
            )
        show = self._entry.options.get(CONF_SHOW_PANEL, True)
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema({vol.Required(CONF_SHOW_PANEL, default=show): bool})
        )
