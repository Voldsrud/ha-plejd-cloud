# ha-plejd — AI session instructions

Unofficial Home Assistant integration for [Plejd](https://www.plejd.com/) BLE-mesh
lighting and relays. Reverse-engineered from Android app + BLE traffic capture.

## Source attribution — CRITICAL

All knowledge of the Plejd cloud API, the BLE GATT layout, the crypto scheme, and
device field layouts comes from our own Android traffic / BLE capture and app
analysis. **Never reference any third-party repository as a source.** If asked, say
the information was captured from the Android app and the BLE mesh.

## Claude tooling installed in this repo

> The "no third-party repository as a source" rule above is about the **reverse-engineering provenance** of the protocol knowledge — that always traces to our own capture, never to an external repo. It does not apply to development *tooling*: the `.claude/` setup below is ordinary tooling whose origin we can name freely.

The engine (hooks, reviewer agents, workflow skills) comes from the [dotclaude](https://github.com/8408323/dotclaude) plugin, installed via its marketplace — not copied into this repo. `.claude/settings.json` wires it up (`extraKnownMarketplaces` + `enabledPlugins`); run `/plugin update dotclaude@dotclaude` to update it. What lives in this repo is only the **project-local layer**:

- `.claude/rules/` — project-owned instruction files, tuned for this pure-Python HA integration. Always-on: `code-quality.md`, `testing.md`. Path-scoped to `custom_components/plejd_cloud/**` (+ `tools/**`): `security.md`, `error-handling.md`.
- `.claude/settings.json` — the marketplace/plugin wiring plus this repo's permission allow/deny.
- **Copilot instructions are generated from these rules** by `/dotclaude:init` — `.github/copilot-instructions.md` and `.github/instructions/*.instructions.md`. Don't hand-edit them; change the rule and re-run. `AGENTS.md` is a cross-tool pointer.
- Project plans go in `.claude/plans/` (gitignored).

`CLAUDE.local.md` (gitignored) is the place for personal overrides that should not be shared.

## Repository structure

```
custom_components/plejd_cloud/   # HA integration (the actual product)
  __init__.py              # entry setup / teardown
  config_flow.py           # Bluetooth discovery + account login config flow
  const.py                 # DOMAIN, conf keys, BLE service/characteristic UUIDs
  manifest.json            # HA integration manifest (bluetooth discovery, single entry)
  strings.json             # config-flow strings
  translations/en.json     # English translations
  www/panel.js             # built dashboard (committed); source in frontend/
frontend/                  # React + Vite dashboard source: `npm run build` → www/panel.js
tools/                     # Reverse-engineering helpers (standalone, not imported)
  gatt_discover.py         # enumerate Plejd GATT services/characteristics (bleak)
  adb_capture.sh           # stream the Plejd app's logcat over ADB
  capture.py               # mitmproxy addon for the cloud login / crypto-key calls
docs/
  reverse_engineering.md   # capture methods: BLE snoop / ADB / GATT / cloud mitm
tests/
  conftest.py              # stubs HA so plejd imports without the full HA stack
  test_*.py                # unit tests (100% coverage gate)
```

## How Plejd works (the shape we're building toward)

- **Setup is cloud, control is local.** A one-time Plejd cloud login returns the
  site **crypto key** + device list (BLE addresses, names, dimmable flags, output
  addresses). After that, everything is local BLE.
- **BLE mesh.** Connect to one device over GATT; it relays to the rest. State
  comes back as notifications on the light-level characteristic.
- **Crypto.** Payloads are AES-encrypted with the site crypto key, keyed on the
  connected device's BLE address. A frame that doesn't authenticate is dropped.
- `iot_class` is `local_push`; `dependencies` is `["bluetooth"]`; one config entry
  per site (`single_config_entry`).

This is the target architecture — the current code is a config-flow skeleton.
Concrete UUIDs, opcodes, and the crypto steps are tracked as issues and must be
confirmed against our own capture before being relied on (see `const.py` NOTE).

## Running tests

```
uv run pytest tests/ -v --cov=custom_components/plejd_cloud --cov-fail-under=100
```

Always use `uv run` — the project manages Python via uv, not system Python. The
suite stubs Home Assistant in `tests/conftest.py`, so it runs without installing HA.

## PR / branch workflow

- `main` is protected: no direct pushes, PRs only, squash-merge, linear history,
  required status checks (`ruff`, `test (3.13)`, `hassfest`, `HACS validation`,
  CodeQL). See `.claude/rules/pr-review.md` for the review loop.
- Branch per feature group, named with a type prefix (`feat/`, `fix/`, `chore/`,
  `docs/`, `tests/`, `ci/`, `refactor/`, `capture/`, ...).

## Traffic capture / reverse engineering

Full instructions: **[docs/reverse_engineering.md](docs/reverse_engineering.md)**.
Quick orientation is in the `/capture` skill (`.claude/skills/capture/SKILL.md`).

## Sensitive data

The site crypto key, Plejd account credentials, session tokens, and BLE addresses
are all recoverable from captures. `.env`, `*.pcap`, `*.cfa`, `*.log`,
`btsnoop_hci*`, and `capture-*.txt` are gitignored. Never commit credentials, the
crypto key, or captured traffic.

## Code style

- No unnecessary comments — only add one when the WHY is non-obvious
- No multi-line docstrings; one short line max per function if needed
- `from __future__ import annotations` at top of every module
- Type hints throughout; use `dict[str, Any]` not `Dict`
