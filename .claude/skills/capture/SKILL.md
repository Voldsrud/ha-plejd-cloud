---
name: capture
description: Capture and decode Plejd traffic (BLE GATT + cloud login) for reverse engineering. Use when you need to observe device behaviour, confirm a characteristic or crypto step, or capture a new command. Project-specific to ha-plejd.
argument-hint: "[btsnoop | adb | mitm | gatt] [what you're trying to capture]"
disable-model-invocation: true
allowed-tools:
  - Bash(uv run *)
  - Bash(bash tools/*)
  - Bash(adb *)
  - Read
  - Glob
  - Grep
---

# capture

Orchestrates this repo's reverse-engineering capture methods. Full reference: **[docs/reverse_engineering.md](docs/reverse_engineering.md)**. Pick the method that fits what you're after.

Plejd is local **BLE**: the phone (or HA) connects to one mesh device over GATT and that device relays to the rest. Setup-time secrets (the site **crypto key** + device list) come from the Plejd **cloud** login. So there are two capture surfaces.

## 1. Pick a method

- **`btsnoop`** — the BLE GATT traffic itself (commands, light-level notifications, the auth handshake). Enable *Bluetooth HCI snoop log* in Android developer options, drive the Plejd app, then pull `btsnoop_hci.log` (`adb pull` from the bug-report or the snoop path). This is the primary method for the on-air protocol.
- **`adb`** — phone on USB. `bash tools/adb_capture.sh` (logcat from the Plejd app + optional HCI snoop pull). See the ADB quick reference in [CLAUDE.md](CLAUDE.md).
- **`mitm`** — the **cloud** side only (login → site list → crypto key fetch over HTTPS). `mitmdump -s tools/capture.py --listen-host 0.0.0.0 --listen-port 8888 --ssl-insecure`, phone proxy → this host, install the cert via `http://mitm.it`. BLE never crosses the proxy.
- **`gatt`** — enumerate services/characteristics of a device directly from this host. `uv run python tools/gatt_discover.py` (bleak). Confirms the service/char UUIDs and which char carries light level vs. data vs. auth.

## 2. Decode

- The on-air payloads are **encrypted** with the site crypto key (AES, keyed on the connected device's BLE address). Decode with the codec in `custom_components/plejd_cloud/crypto.py` — feed it the captured frames + the key from your own cloud capture. Without the key the bytes are opaque; that's expected.
- Cross-check field names and command opcodes against `custom_components/plejd_cloud/const.py`. Keep wire/cloud keys verbatim (`cryptoKey`, `outputAddress`, `deviceId`) — don't snake-case them.

## 3. Handle the artifacts (important)

- Capture output is **live secrets** — `btsnoop_hci*`, `*.pcap`, `*.cfa`, `*.log`, `capture-*.txt` are gitignored. Keep them so. The crypto-key fetch and the BLE auth challenge are both in there.
- Never paste raw capture contents (the crypto key, account email, session token, BLE addresses) into code, commits, issues, or PRs. Redact first.
- Credentials come from `PLEJD_USER` / `PLEJD_PASS` (env / gitignored `.env`), never hardcoded.

## 4. Record the finding

When a capture confirms something new (a characteristic UUID, an opcode, a crypto step), write it where it belongs: a decode in `crypto.py`/`const.py`, capture method notes in `docs/reverse_engineering.md`. Don't leave it only in a transient capture file.
