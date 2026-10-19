"""Tests for plejd constants."""

from __future__ import annotations

from plejd_cloud.const import (
    CONF_DISCOVERED_ADDRESS,
    DOMAIN,
    PLEJD_CHAR_DATA_UUID,
    PLEJD_SERVICE_UUID,
)


def test_domain_is_plejd():
    assert DOMAIN == "plejd_cloud"


def test_conf_discovered_address_key():
    assert CONF_DISCOVERED_ADDRESS == "discovered_address"


def test_ble_uuids_share_plejd_base():
    # Every Plejd characteristic lives under the same 128-bit base UUID.
    base_suffix = PLEJD_SERVICE_UUID[8:]
    assert PLEJD_CHAR_DATA_UUID.endswith(base_suffix)
