"""Tests for the Plejd config flow."""

from __future__ import annotations

import types

import pytest
from aiohttp import ClientError
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from plejd_cloud import config_flow as cf
from plejd_cloud.cloud import (
    PlejdAuthError,
    PlejdCloudDevice,
    PlejdCloudError,
    PlejdCloudInput,
    PlejdCloudMotion,
    PlejdCloudScene,
    PlejdCloudSite,
)
from plejd_cloud.config_flow import PlejdConfigFlow
from plejd_cloud.const import (
    CONF_CRYPTO_KEY,
    CONF_DEVICE_ADDRESSES,
    CONF_DEVICES,
    CONF_GATEWAYS,
    CONF_INSTALLATION_ID,
    CONF_RESOURCE_SET_ID,
    CONF_SITE_ID,
)

_LOGIN = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pw"}


def _flow():
    flow = PlejdConfigFlow()
    flow.hass = types.SimpleNamespace(session=None, data={})
    flow.context = {}
    return flow


def _site(site_id="S1", malformed=None):
    dev = PlejdCloudDevice(
        device_id="d1",
        name="Kitchen",
        address=1,
        output_index=0,
        outputs=[11],
        hardware_id=1,
        model="DIM-01",
        category="light",
        dimmable=True,
        traits=3,
        room_id="r1",
    )
    scene = PlejdCloudScene("sc1", "Movie", 3)
    return PlejdCloudSite(
        site_id=site_id,
        title="Home",
        crypto_key=bytes(16),
        mesh_key="AA-BB-CC-DD",
        devices=[dev],
        inputs=[PlejdCloudInput("d1", "Kitchen", 11)],
        motion=[PlejdCloudMotion("w1", "Motion", 33)],
        scenes=[scene],
        gateways=["gw1"],
        resource_set_id="rsABC",
        device_addresses={"d1": 1, "w1": 33},
        malformed=frozenset(malformed or ()),
    )


def _patch_cloud(monkeypatch, *, login=None, sites=None, site=None):
    async def _login(session, email, password):
        if isinstance(login, Exception):
            raise login
        return login or "tok"

    async def _get_sites(session, token):
        if isinstance(sites, Exception):
            raise sites
        return sites if sites is not None else []

    async def _get_site(session, token, site_id):
        if isinstance(site, Exception):
            raise site
        return site if site is not None else _site(site_id)

    monkeypatch.setattr(cf, "async_login", _login)
    monkeypatch.setattr(cf, "async_get_sites", _get_sites)
    monkeypatch.setattr(cf, "async_get_site", _get_site)


async def test_user_step_shows_form():
    result = await _flow().async_step_user()
    assert result["type"] == "form" and result["step_id"] == "user"


async def test_invalid_auth(monkeypatch):
    _patch_cloud(monkeypatch, login=PlejdAuthError("bad"))
    result = await _flow().async_step_user(_LOGIN)
    assert result["errors"] == {"base": "invalid_auth"}


async def test_cannot_connect(monkeypatch):
    _patch_cloud(monkeypatch, sites=PlejdCloudError("down"))
    result = await _flow().async_step_user(_LOGIN)
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    "error",
    [ConnectionResetError("connection reset"), TimeoutError("timed out"), ClientError("client error")],
)
async def test_user_step_handles_login_transport_failure(monkeypatch, error):
    _patch_cloud(monkeypatch, login=error)
    result = await _flow().async_step_user(_LOGIN)
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    "error",
    [ConnectionResetError("connection reset"), TimeoutError("timed out"), ClientError("client error")],
)
async def test_user_step_handles_site_list_transport_failure(monkeypatch, error):
    _patch_cloud(monkeypatch, login="tok", sites=error)
    result = await _flow().async_step_user(_LOGIN)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_no_sites(monkeypatch):
    _patch_cloud(monkeypatch, sites=[])
    result = await _flow().async_step_user(_LOGIN)
    assert result["errors"] == {"base": "no_sites"}


async def test_single_site_creates_entry(monkeypatch):
    _patch_cloud(monkeypatch, sites=[{"siteId": "S1", "title": "Home"}])
    result = await _flow().async_step_user(_LOGIN)
    assert result["type"] == "create_entry"
    assert result["title"] == "Home"
    assert result["data"][CONF_CRYPTO_KEY] == bytes(16).hex()
    assert result["data"][CONF_SITE_ID] == "S1"
    assert result["data"][CONF_DEVICES][0]["model"] == "DIM-01"
    assert result["data"][CONF_GATEWAYS] == ["gw1"]
    assert result["data"][CONF_RESOURCE_SET_ID] == "rsABC"
    assert result["data"][CONF_DEVICE_ADDRESSES] == {"d1": 1, "w1": 33}
    assert len(result["data"][CONF_INSTALLATION_ID]) == 36  # a generated uuid4


async def test_multiple_sites_shows_picker(monkeypatch):
    _patch_cloud(monkeypatch, sites=[{"siteId": "S1", "title": "Home"}, {"siteId": "S2", "title": "Cabin"}])
    flow = _flow()
    result = await flow.async_step_user(_LOGIN)
    assert result["type"] == "form" and result["step_id"] == "site"
    # then pick one
    result2 = await flow.async_step_site({CONF_SITE_ID: "S2"})
    assert result2["type"] == "create_entry"
    assert result2["data"][CONF_SITE_ID] == "S2"


async def test_site_step_shows_form_when_no_input(monkeypatch):
    _patch_cloud(monkeypatch)
    flow = _flow()
    flow._sites = [{"siteId": "S1", "title": "Home"}, {"siteId": "S2"}]
    result = await flow.async_step_site()
    assert result["type"] == "form" and result["step_id"] == "site"


async def test_create_entry_handles_site_fetch_error(monkeypatch):
    _patch_cloud(monkeypatch, sites=[{"siteId": "S1"}], site=PlejdCloudError("nope"))
    result = await _flow().async_step_user(_LOGIN)
    assert result["type"] == "form" and result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    "error",
    [ConnectionResetError("connection reset"), TimeoutError("timed out"), ClientError("client error")],
)
async def test_create_entry_handles_site_fetch_transport_failure(monkeypatch, error):
    _patch_cloud(monkeypatch, sites=[{"siteId": "S1"}], site=error)
    result = await _flow().async_step_user(_LOGIN)
    assert result["type"] == "form" and result["errors"] == {"base": "cannot_connect"}


async def test_create_entry_refuses_a_malformed_site_response(monkeypatch):
    # A truncated/wrong-typed collection parses into an empty one, so setting up on it would
    # create an entry missing whole device/scene/room sets. Refuse it like any bad response.
    _patch_cloud(monkeypatch, sites=[{"siteId": "S1"}], site=_site(malformed={"devices"}))
    result = await _flow().async_step_user(_LOGIN)
    assert result["type"] == "form" and result["errors"] == {"base": "cannot_connect"}


async def test_bluetooth_step_routes_to_user(monkeypatch):
    flow = _flow()
    info = types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="Plejd DIM-01")
    result = await flow.async_step_bluetooth(info)
    assert result["type"] == "form" and result["step_id"] == "user"
    assert flow._discovered_address == "AA:BB:CC:DD:EE:FF"
    assert flow.context["title_placeholders"] == {"name": "Plejd DIM-01"}


@pytest.mark.parametrize("title", [None, "Home"])
async def test_site_label_fallback(monkeypatch, title):
    _patch_cloud(
        monkeypatch, sites=[{"siteId": "S1", "title": "A"}, {"siteId": "S2", **({"title": title} if title else {})}]
    )
    flow = _flow()
    await flow.async_step_user(_LOGIN)
    result = await flow.async_step_site()
    assert result["step_id"] == "site"


def _reauth_flow(reauth_entry):
    flow = _flow()
    flow._reauth_entry = reauth_entry
    return flow


async def test_reauth_routes_to_confirm():
    flow = _reauth_flow(types.SimpleNamespace(data={CONF_EMAIL: "u@x.se"}))
    res = await flow.async_step_reauth({CONF_EMAIL: "u@x.se"})
    assert res["type"] == "form" and res["step_id"] == "reauth_confirm"


async def test_reauth_confirm_success_updates_password(monkeypatch):
    _patch_cloud(monkeypatch, login="tok")
    flow = _reauth_flow(types.SimpleNamespace(entry_id="e1", data={CONF_EMAIL: "u@x.se", CONF_PASSWORD: "old"}))
    res = await flow.async_step_reauth_confirm({CONF_PASSWORD: "newpw"})
    assert res["type"] == "abort" and res["reason"] == "reauth_successful"
    assert res["data_updates"] == {CONF_PASSWORD: "newpw"}


async def test_reauth_confirm_invalid_auth(monkeypatch):
    _patch_cloud(monkeypatch, login=PlejdAuthError("bad"))
    flow = _reauth_flow(types.SimpleNamespace(data={CONF_EMAIL: "u@x.se"}))
    res = await flow.async_step_reauth_confirm({CONF_PASSWORD: "x"})
    assert res["errors"] == {"base": "invalid_auth"}


@pytest.mark.parametrize(
    "error",
    [
        PlejdCloudError("down"),
        ConnectionResetError("connection reset"),
        TimeoutError("timed out"),
        ClientError("client error"),
    ],
)
async def test_reauth_confirm_cannot_connect(monkeypatch, error):
    _patch_cloud(monkeypatch, login=error)
    flow = _reauth_flow(types.SimpleNamespace(data={CONF_EMAIL: "u@x.se"}))
    res = await flow.async_step_reauth_confirm({CONF_PASSWORD: "x"})
    assert res["errors"] == {"base": "cannot_connect"}


def _reconfigure_flow(reconfigure_entry):
    flow = _flow()
    flow._reconfigure_entry = reconfigure_entry
    return flow


def _stored_entry(site_id="S1"):
    return types.SimpleNamespace(
        entry_id="e1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "pw",
            CONF_SITE_ID: site_id,
        },
    )


async def test_reconfigure_shows_form():
    flow = _reconfigure_flow(_stored_entry())
    res = await flow.async_step_reconfigure()
    assert res["type"] == "form" and res["step_id"] == "reconfigure"


async def test_reconfigure_fetches_and_updates_entry(monkeypatch):
    new_site = _site()
    _patch_cloud(monkeypatch, login="tok", site=new_site)
    flow = _reconfigure_flow(_stored_entry())
    res = await flow.async_step_reconfigure({})
    assert res["type"] == "abort" and res["reason"] == "reconfigure_successful"
    updates = res["data_updates"]
    assert updates[CONF_DEVICES][0]["model"] == "DIM-01"
    assert updates[CONF_GATEWAYS] == ["gw1"]
    assert updates[CONF_DEVICE_ADDRESSES] == {"d1": 1, "w1": 33}
    assert updates[CONF_RESOURCE_SET_ID] == "rsABC"
    assert updates[CONF_CRYPTO_KEY] == bytes(16).hex()
    # _stored_entry() predates CONF_INSTALLATION_ID; a gateway showing up must seed one
    # now, or the gateway transport this reload constructs would KeyError on it.
    assert updates[CONF_INSTALLATION_ID]


async def test_reconfigure_does_not_overwrite_existing_installation_id(monkeypatch):
    new_site = _site()
    _patch_cloud(monkeypatch, login="tok", site=new_site)
    entry = _stored_entry()
    entry.data[CONF_INSTALLATION_ID] = "already-set"
    flow = _reconfigure_flow(entry)
    res = await flow.async_step_reconfigure({})
    assert CONF_INSTALLATION_ID not in res["data_updates"]  # left untouched, not regenerated


async def test_reconfigure_invalid_auth(monkeypatch):
    _patch_cloud(monkeypatch, login=PlejdAuthError("bad"))
    flow = _reconfigure_flow(_stored_entry())
    res = await flow.async_step_reconfigure({})
    assert res["type"] == "form" and res["errors"] == {"base": "invalid_auth"}


async def test_reconfigure_cannot_connect(monkeypatch):
    _patch_cloud(monkeypatch, login=PlejdCloudError("down"))
    flow = _reconfigure_flow(_stored_entry())
    res = await flow.async_step_reconfigure({})
    assert res["type"] == "form" and res["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_cannot_connect_on_site_fetch(monkeypatch):
    _patch_cloud(monkeypatch, login="tok", site=PlejdCloudError("down"))
    flow = _reconfigure_flow(_stored_entry())
    res = await flow.async_step_reconfigure({})
    assert res["type"] == "form" and res["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_refuses_a_malformed_site_response(monkeypatch):
    # Replacing the cached snapshot with normalized-empty collections would remove every
    # entity of the affected kind - the entry must be left untouched instead.
    entry = _stored_entry()
    before = dict(entry.data)
    _patch_cloud(monkeypatch, login="tok", site=_site(malformed={"scenes"}))
    flow = _reconfigure_flow(entry)
    res = await flow.async_step_reconfigure({})
    assert res["type"] == "form" and res["errors"] == {"base": "cannot_connect"}
    assert entry.data == before  # nothing persisted


async def test_reconfigure_cannot_connect_on_transport_failure(monkeypatch):
    # A raw transport failure (DNS/socket/TLS/timeout) isn't a PlejdCloudError, but must
    # still show cannot_connect rather than crash the flow with an unhandled exception.
    _patch_cloud(monkeypatch, login=OSError("connection reset"))
    flow = _reconfigure_flow(_stored_entry())
    res = await flow.async_step_reconfigure({})
    assert res["type"] == "form" and res["errors"] == {"base": "cannot_connect"}


def _opt_flow(options=None):
    return cf.PlejdOptionsFlow(types.SimpleNamespace(options=options or {}, data={}))


def test_get_options_flow_returns_options_flow():
    flow = cf.PlejdConfigFlow.async_get_options_flow(types.SimpleNamespace())
    assert isinstance(flow, cf.PlejdOptionsFlow)


async def test_options_shows_dashboard_toggle():
    res = await _opt_flow(options={"show_panel": True}).async_step_init()
    assert res["type"] == "form" and res["step_id"] == "init"
    assert [getattr(k, "schema", None) for k in res["data_schema"].schema] == ["show_panel"]


async def test_options_saves_toggle_and_preserves_other_options():
    res = await _opt_flow(options={"schedules": [{"slot": 0}], "transport": "gateway"}).async_step_init(
        {"show_panel": False}
    )
    assert res["type"] == "create_entry"
    assert res["data"] == {"schedules": [{"slot": 0}], "transport": "gateway", "show_panel": False}


async def test_reconfigure_clears_the_malformed_cloud_repair_issue(monkeypatch):
    # The repair tells the user to try Reconfigure; succeeding here proves the cloud is
    # healthy again. The issue is persistent and the replacement coordinator does not poll
    # until its 24h interval, so without this the warning would linger a full day.
    entry = _stored_entry()
    _patch_cloud(monkeypatch, login="tok", site=_site())
    flow = _reconfigure_flow(entry)
    flow.hass.created_issues = {"malformed_cloud_site_e1": {"domain": "plejd_cloud"}}
    res = await flow.async_step_reconfigure({})
    assert res["reason"] == "reconfigure_successful"
    assert "malformed_cloud_site_e1" not in flow.hass.created_issues


async def test_reauth_success_releases_the_self_heal_cooldown(monkeypatch):
    # The cooldown is deliberately held through an auth failure, so the setup retry that
    # follows a successful reauth would otherwise wait it out - at the exact moment it could
    # finally fetch the crypto key/gateway data that reauth itself does not touch.
    from plejd_cloud.coordinator import DATA_LAST_SELF_HEAL

    entry = _stored_entry()
    _patch_cloud(monkeypatch, login="tok")
    flow = _flow()
    flow._reauth_entry = entry
    flow.hass.data[DATA_LAST_SELF_HEAL] = {entry.entry_id: 1_000.0}
    res = await flow.async_step_reauth_confirm({CONF_PASSWORD: "new-pw"})
    assert res["reason"] == "reauth_successful"
    assert entry.entry_id not in flow.hass.data[DATA_LAST_SELF_HEAL]
