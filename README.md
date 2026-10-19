> **Renamed fork: Plejd Cloud (`plejd_cloud`).** This is [8408323/ha-plejd](https://github.com/8408323/ha-plejd) with the integration domain renamed from `plejd` to `plejd_cloud` (folder `custom_components/plejd_cloud`) and its Bluetooth auto-discovery matcher removed, so it can be installed alongside [thomasloven/hass-plejd](https://github.com/thomasloven/hass-plejd), which also uses the `plejd` domain. Used here for a gateway (GWY-01) site controlled over the internet. All credit to the upstream author; report bugs upstream unless they are caused by the rename.

# ha-plejd

[![Lint](https://github.com/8408323/ha-plejd/actions/workflows/lint.yml/badge.svg)](https://github.com/8408323/ha-plejd/actions/workflows/lint.yml)
[![Tests](https://github.com/8408323/ha-plejd/actions/workflows/tests.yml/badge.svg)](https://github.com/8408323/ha-plejd/actions/workflows/tests.yml)
[![Validate](https://github.com/8408323/ha-plejd/actions/workflows/validate.yml/badge.svg)](https://github.com/8408323/ha-plejd/actions/workflows/validate.yml)
[![CodeQL](https://github.com/8408323/ha-plejd/actions/workflows/codeql.yml/badge.svg)](https://github.com/8408323/ha-plejd/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Home Assistant custom integration for [Plejd](https://www.plejd.com/) — the Swedish **Bluetooth-mesh** lighting and relay system.

> **Status**: Working. The BLE protocol (GATT, AES-128 crypto, mesh commands) and cloud login are fully decoded, and the core — lights, switches, scenes, buttons, motion/illuminance, and on-device scheduling (a scheduled scene was confirmed firing on real hardware) — is **validated end-to-end against real hardware**. Covers, climate, and the device-config entities are decoded from the app but not yet hardware-validated. Reverse-engineered entirely from our own analysis of the Plejd Android app + BLE capture; not affiliated with Plejd.

## Support

If you find this integration useful, you can buy me a coffee ☕

[![Buy me a coffee](https://img.buymeacoffee.com/button-api/?text=Buy+me+a+coffee&emoji=&slug=jhara&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff)](https://www.buymeacoffee.com/jhara)

## How it works

Plejd devices form a local **Bluetooth mesh** (no per-device IP API). This integration:

1. **Logs in to the Plejd cloud** to fetch your site's *crypto key*, device list (BLE addresses, output addresses, device types), and whether the site has a **Plejd Gateway** (GWY-01). The gateway path also obtains short-lived access tokens from the cloud as needed.
2. **Controls the mesh over whichever transport fits**, validated end-to-end on real hardware:
   - **Local Bluetooth** — connects to one mesh device, which relays commands and state to the rest; payloads are AES-128 encrypted with the site key. Fully local, no internet. State arrives as LastChanged mesh broadcasts.
   - **Remote gateway** — if your site has a GWY-01, commands and state are relayed through Plejd's cloud WebSocket, so control keeps working even when Home Assistant is out of Bluetooth range. State arrives as `mesh.out` pushes.

It prefers the gateway when one exists and **falls back to Bluetooth** automatically. Both paths are push-based (no polling). `iot_class` is `local_push`.

## Requirements

- Home Assistant 2024.6 or newer with the **Bluetooth** integration available (a built-in adapter or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html)).
- A Plejd account (email + password) and at least one Plejd device.

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=8408323&repository=ha-plejd&category=integration)

Or manually:

1. In HACS, go to **Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/8408323/ha-plejd` as an **Integration**.
3. Search for **Plejd** and click **Download**.
4. Restart Home Assistant.

### Manual

1. Copy `custom_components/plejd_cloud/` to your HA `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration** (a Plejd device in
   Bluetooth range is also auto-discovered).
2. Search for *Plejd*.
3. Sign in with your Plejd account; if you have more than one site, pick one. Your
   site's devices and crypto key are fetched once. On a site **without** a gateway,
   control then happens locally over Bluetooth with no internet needed; on a site
   **with** a GWY-01 it defaults to the remote gateway (cloud), falling back to
   local Bluetooth when the gateway is unreachable.

Devices, scenes, buttons and sensors appear automatically. Per-device tuning and
schedules are added as entities (see below).

### The Plejd dashboard

A **Plejd** entry in the sidebar (admins only) has three tabs:

- **Devices** — one card per Plejd room: switch or dim the whole room, or tap a lamp
  to toggle it and drag to dim. **✎ Edit** renames rooms and lights (also in the
  Plejd app), picks how each lamp is drawn (bulb, pendant, spot, ceiling, downlight,
  chandelier, LED strip, table, floor, wall, outdoor lantern, ground light, post
  light), and reorders/resizes the cards; nothing changes until **Save**. Then scenes,
  thermostats, covers, motion sensors and device health.
- **Automations** — on-device weekly schedules and remote → light dim bindings.
- **Log** — lights switched on/off or dimmed and alarm changes from the last 30 days,
  with room and source, searchable and filterable (type, source, room, period, time of
  day). Inside Home Assistant the source is exact (who, or which automation and what
  triggered it). From outside, the Plejd mesh carries no sender: a whole-room command
  is shown as the Plejd app; a remote, Plejd motion sensor, scene or schedule is named
  when it fired just before (a best guess); and an on/off sent straight to a light
  can't be told apart between the Plejd app, Google Home through Plejd, or a switch
  wired to that light's own input. **Night watch** notifies you when a light goes on
  at night that no automation or schedule explains.
- **Settings** — holiday mode (lights + active window), the communication path on
  sites with a gateway, and adding a new device.

The dashboard follows your Home Assistant language (English, Svenska, Norsk, Dansk,
Suomi or Íslenska; anything else falls back to English). **Settings → Language** picks
another one for the current browser.

The integration's own **Configure** dialog only shows or hides the dashboard.

### Adding a new Plejd device

Open the **Plejd** dashboard → **Settings** → **Add a device**. It works the same
whether your site has a GWY-01 or not.

1. Power up the new device; it broadcasts as unprovisioned over Bluetooth. Press **Scan**.
2. Pick it from the list (model, address, signal strength).
3. Give it a name (and optionally a new room). It's commissioned directly from Home
   Assistant — DH key exchange, mesh access address, node index — no need to open
   the Plejd app at all.

Prefer scripting it? The same logic is exposed as the `plejd.add_device` and
`plejd.scan_new_devices` services (**Developer Tools → Actions**), which the wizard
calls under the hood.

## Features

Entities are created automatically from your site:

- **Lights** ✅ — on/off + brightness for dimmers and LED drivers (DIM-01/02, LED-10/75, …)
- **Switches / relays** ✅ — CTR-01, REL-01/02, OUT-01/02
- **Scenes** ✅ — trigger Plejd scenes
- **Buttons** ✅ — wall switches / remotes (WPH-01, WRT-01) fire HA **events** (press / release) for automations
- **Motion & illuminance** ✅ — WMS-01 reports occupancy (`binary_sensor`) and ambient light (`sensor`)
- **Covers** — JAL-01 / MTR-01 blinds and shades *(decoded; not hardware-validated)*
- **Climate** — TRM-01 thermostats: target temperature + Plejd presets *(decoded; not hardware-validated)*

Per-output **device settings** (config entities):

- **Minimum / maximum brightness** — `number` entities that set a dimmer's range
- **Dimming curve** — linear / logarithmic / anti-logarithmic (`select`)
- **Phase dimming** — leading / trailing edge, on phase-cut dimmers (`select`)

**On-device scheduling** — Plejd devices can run weekly time→scene events from their
own clock, so they keep firing even when Home Assistant is offline:

- A **Sync clock** button (and automatic sync on connect + daily) keeps device clocks correct.
- Add **weekly schedules** (day + time → scene) on the dashboard's **Automations**
  tab; each becomes a `switch` you can enable/disable. Prefer HA automations? Just
  don't add any — the choice is yours.

> Astro (sunrise/sunset) schedules and firmware OTA are intentionally out of scope —
> Home Assistant's `sun`-based automations cover the former, and OTA would require
> Plejd's proprietary firmware images.

## Privacy & security

The Plejd **site crypto key** is the master secret for your mesh. This integration keeps it in the Home Assistant config entry and never logs or transmits it. When filing issues, redact the crypto key, your account email, BLE addresses, and any capture artifacts. See [SECURITY.md](SECURITY.md).

## Development

The integration is pure Python with no Home Assistant import needed to run the test suite:

```bash
uv sync --dev
uv run pytest tests/ -v --cov=custom_components/plejd_cloud --cov-fail-under=100
node --test tests/test_panel_logic.mjs
uv run ruff check custom_components/ tests/ tools/
```

The dashboard is a React + Vite app in `frontend/`, built into
`custom_components/plejd_cloud/www/panel.js` (committed, so HACS installs need no build step).
After changing it, rebuild and commit the output:

```bash
cd frontend && npm install && npm run build
```

How the protocol was reverse-engineered (BLE GATT, crypto, mesh commands, cloud) is documented in [docs/reverse_engineering.md](docs/reverse_engineering.md). Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

Not affiliated with or endorsed by Plejd. "Plejd" is a trademark of its owner. All protocol knowledge was obtained from our own analysis of the Plejd Android app. Use at your own risk. Licensed under [MIT](LICENSE).
