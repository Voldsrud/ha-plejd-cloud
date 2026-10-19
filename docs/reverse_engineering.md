# Reverse engineering Plejd

All protocol knowledge here comes from **our own** analysis of the Plejd Android
app and BLE traffic. The app (`com.plejd.plejdapp`, v7.2.1) is a **.NET MAUI**
application — the logic lives in managed assemblies (`Plejd.dll`, `Plejd.Shared`),
not in the Java/dex layer.

Plejd has two surfaces:

1. **Cloud (HTTPS, setup only).** Logging in returns the site's **crypto key** and
   the device list (BLE addresses, output addresses, dimmable flags).
2. **BLE mesh (local, ongoing).** Control and state run over BLE GATT. The phone
   (or Home Assistant) connects to one mesh device and it relays to the rest.
   Datavector payloads are encrypted with the site crypto key, keyed on the
   connected device's BLE address.

## Extracting the app's logic

The managed assemblies are AOT-compiled and bundled in a **.NET assembly store**
inside `lib/arm64-v8a/libassemblies.arm64-v8a.blob.so` (split APK). Layout:

- ELF `.so` with a `payload` section at file offset `0x4000` holding an **XABA**
  store (v2 header: magic, version, entry_count, index_entry_count, index_size).
- Each assembly is an **XALZ** record: `"XALZ"` + uint32 index + uint32
  uncompressed-size + an **LZ4 block**. Decompress (stopping at the uncompressed
  size) to recover the original `.dll`.

`tools/apk/` (gitignored) holds the extractor and the decompiled output. Decompile
with [ILSpy](https://github.com/icsharpcode/ILSpy) (`ilspycmd`). The key classes:
`PlejdConstants.BleCharacteristics`, `BleCrypto`, `CryptableExtension`, `BleCom`.

## BLE GATT layout

Service + characteristics share the base `31BA0001-6085-4726-BE45-040C957391B5`,
with the 3rd 16-bit group varying:

| Suffix | Name (app)             | Role |
|--------|------------------------|------|
| `0001` | Service                | GATT service |
| `0002` | NodeIndex              | node index |
| `0003` | NodeIndexData          | node index data |
| `0004` | **Datavector**         | **write mesh commands here** (encrypted) |
| `0005` | **LastChangedDatavector** | **state-change notifications** (encrypted) |
| `0006` | AccessAddress          | device address used for crypto |
| `0007` | DeviceFirmwareUpdate   | DFU |
| `0008` | CryptoKey              | crypto-key delivery (Diffie-Hellman) |
| `0009` | AuthKey                | login challenge/response |
| `000A` | PingPong               | verify-login ping |
| `000B` | SpecialCommand         | special commands |
| `000C` | ProductSpecific        | product-specific |

Only **Datavector** and **LastChangedDatavector** payloads are encrypted; the
auth/ping/key-exchange characteristics are plaintext.

## Crypto (`BleCrypto`)

**Payload cipher — `EncryptDecryptAes128Ecb(address, data, key)`** (symmetric):

```
seed       = address ++ address ++ address[:4]      # 6 + 6 + 4 = 16 bytes
keystream  = AES-128-ECB(key, seed)                 # one 16-byte block
out[i]     = data[i] XOR keystream[i % 16]
```

- `address` = the device's BLE MAC, **reversed** (6 bytes).
- `key` = the 16-byte site crypto key, or the default-mesh key
  `00 11 22 33 44 55 66 77 88 99 AA BB CC DD EE FF` for an unconfigured device.
- Encryption applies only to firmware `>= 20160801163820`; older devices are plaintext.

Implemented in [`custom_components/plejd_cloud/crypto.py`](../custom_components/plejd_cloud/crypto.py).

**Login response — `Response(challenge, key)`:**

```
response = fold_32_to_16( SHA256( challenge XOR key ) )
   where fold(x)[i] = x[i] XOR x[i + 16]   # 32-byte digest folded to 16
```

**Crypto-key delivery** uses a custom 64-bit Diffie–Hellman (base 2, modulus
`15734018190158744081`). `GenerateSharedKey(secret, remotePub, localPub, deviceId)` =
`SHA256(secret ++ remotePub ++ localPub ++ reverse(deviceId))[:16]`, where the three
uint64s are little-endian 8-byte encodings and `deviceId` is the BLE MAC in its normal
(forward) byte order — the function reverses it internally. The site key is then a
plain 16-byte XOR against this derived key (not the raw DH secret cycled).

## Login / auth handshake (`CryptableExtension`)

1. Connect; discover the service.
2. Read **AccessAddress** (`0006`) — the address fed to the cipher.
3. Challenge: write empty to **AuthKey** (`0009`), receive the challenge (notify/read),
   compute `Response(challenge, cryptoKey)`, write it back to **AuthKey**.
4. Verify: write a random byte to **PingPong** (`000A`); a correct login echoes
   `byte + 1`.
5. Send commands to **Datavector** (`0004`); subscribe to **LastChangedDatavector**
   (`0005`) for state.

## Mesh command framing

Datavector payloads carry a 5-byte header; the command is a **2-byte big-endian**
opcode at offset 3, followed by command data, all XOR-encrypted as above.

Key opcodes (from the app's mesh command schema, 194 commands total):

| Opcode | Name | Payload |
|--------|------|---------|
| `0x00C8` | Output state and level | `output(1)`, `state(1)`, `level(2)` — on/off + brightness |
| `0x00C9` / `0x00CA` | Output min / max level | `channel(1)`, `level(2)` |
| `0x0021` | Execute scene | `scene(1)` only, **DontRespond** type (the app's `ExecuteScene`; `index+0x80` = power-off variant). The `slewrate`/`level` fields belong to scene *configuration* (`0x0022`), not execution. |
| `0x0195` | Input state and level | `channel(1)`, `state(1)`, `level(2)` — state notification |
| `0x001B` | System time | `type(1)`, … |
| `0x0000` / `0x0003` / `0x0004` | Device type / MAC / firmware | query/response |
| `0x0008` | Mesh group membership (join/leave a room) | `0x01`, `room_group_address(1)`, optional trailing `0x01` = join (absent = leave). Confirmed via a live BLE capture of the app's own "move device to room" action, decrypted and cross-checked against the room's real cloud group address (`roomAddress`) — room membership is mesh-group membership, not a Parse cloud field; `address` targets the device's own mesh address (`deviceAddress`), not its output's cloud address. |

## Cloud

- Auth host: **`auth.api.plejd.cloud`** (`installations`, `sites` resources). The
  site object holds `PlejdMesh.CryptoKey` (the master key) and the device list.
- Realtime / gateway comms use **NATS** (subjects for gateway firmware, response
  inboxes, site events). A Plejd gateway lets the cloud reach the mesh remotely.

The exact cloud request/response shapes are best confirmed with a live capture
(see below) — that's issue #2.

## Capture methods

### BLE HCI snoop (the on-air protocol)

1. Phone → Developer options → enable **Bluetooth HCI snoop log**.
2. Drive the Plejd app (toggle lights, set brightness, run a scene).
3. Pull the log: `adb pull /sdcard/btsnoop_hci.log tools/` (path varies — check
   `adb bugreport`). Open in Wireshark, filter on the Datavector ATT writes/notifies.

### APK / assembly analysis (no phone needed once pulled)

`adb shell pm path com.plejd.plejdapp`, pull `base.apk` + `split_config.arm64_v8a.apk`,
then extract + decompile as described above.

### Cloud capture (mitmproxy)

`mitmdump -s tools/capture.py --listen-host 0.0.0.0 --listen-port 8888 --ssl-insecure`
for the login → site → crypto-key calls. The BLE traffic does not cross the proxy.

> **2026-07-21 update, read before relying on the recipe above:** a live capture
> this session found `cloud.plejd.com` certificate-pinned for at least two Parse
> `functions/*` calls (a scene-save and a Semesterläge fetch) — the connection
> failed with "the client does not trust the proxy's certificate" even with the
> mitmproxy CA otherwise trusted and working for every other host. **Login and
> `getSiteById` were not re-tested this session** (the app was already holding a
> cached session throughout), so it's unconfirmed whether pinning also affects the
> login → site → crypto-key flow this section describes, or only some other subset
> of `functions/*` calls. Try the recipe above first; if it fails the same way, the
> whole Parse API needs a pinning bypass (e.g. Frida hooking the app's TLS/pinning
> implementation), not just a mitmproxy CA install.

**This HTTP-proxy mode cannot see the gateway/mesh-relay channel** (`wss://ws-ie.api.plejd.cloud`,
see `docs/gateway_protocol.md`) — confirmed 2026-07-21 by watching a real settings
change round-trip succeed in the app while producing zero proxy-visible traffic.
The app's networking stack for that one channel doesn't respect the system HTTP
proxy setting, even though ordinary HTTPS (analytics, etc.) does. To capture it,
run mitmproxy in **WireGuard mode** instead (intercepts at the IP layer, catching
everything regardless of app-level proxy awareness):

```
mitmdump --mode wireguard --listen-host 0.0.0.0 --listen-port 51820 -s tools/capture.py
```

Import the generated client config into the WireGuard Android app and toggle the
tunnel on. Run this on a host reachable from the phone's WiFi network — a WSL
guest's own IP is typically NAT'd behind its Windows host and unreachable, so run
it on the Windows host directly if capturing from WSL.

This gateway/mesh-relay channel is a **different host** than the pinned
`cloud.plejd.com` above and is not pinned — WireGuard mode was enough to capture
it cleanly.

> **2026-07-22 update — the `cloud.plejd.com` pinning wall above is now bypassable.**
> Live-confirmed cause: the app's HttpClient on Android routes through the
> platform's legacy `com.android.okhttp`/Conscrypt stack (not modern OkHttp3),
> and certificate validation ultimately goes through the app's own .NET-on-Android
> trust manager binding, `net.dot.android.crypto.DotnetProxyTrustManager`
> (`checkServerTrusted`/`checkClientTrusted`). A Frida script hooking that class's
> methods (plus `com.android.okhttp.CertificatePinner.check`/`check$okhttp` and
> `com.android.org.conscrypt.TrustManagerImpl.checkTrustedRecursive`/`verifyChain`
> defensively, since which one actually gates a given call wasn't worth narrowing
> down further) to no-op/pass-through made the app accept mitmproxy's certificate
> for `cloud.plejd.com`, decrypting the Parse `functions/*` calls cleanly.
>
> Setup: push a matching `frida-server` build to `/data/local/tmp/`, run it as
> root (`su -c '/data/local/tmp/frida-server &'`), then either spawn the app fresh
> (`frida -U -f com.plejd.plejdapp -l bypass.js`) or attach to an already-running
> instance (`frida -U -n Plejd -l bypass.js`) — spawning hit an unexplained
> "timed out while waiting for app to launch" a couple of times on this device/OS
> combo (Android 10, SDK 29) even though the process came up fine per `ps`;
> attaching to the already-running app worked reliably instead. Enumerate
> `Java.enumerateLoadedClassesSync()` filtered on TLS-related names first to
> confirm which trust-manager classes are actually loaded before writing hooks -
> this app has no `okhttp3.*` classes loaded at all, so hooking only the modern
> OkHttp3 API (the usual first guess for "Android SSL pinning bypass" scripts)
> silently does nothing.
>
> **2026-07-22 follow-up — the bypass above wasn't enough for every call.** A
> schedule-save specifically kept failing ("the client does not trust the
> proxy's certificate") even with the same script attached, including a case
> where `frida -U -f ... -l bypass.js` reported "Failed to spawn: unexpectedly
> timed out" yet the process had actually launched (confirmed via `ps`) —
> attaching to it (`frida -U -n Plejd -l bypass.js`) afterward worked. The
> deeper cause: killing the Frida CLI process (even after a successful spawn)
> detaches the injected script and reverts its hooks, so a `timeout N frida ...
> &` wrapper that gets killed after N seconds silently undoes the bypass for
> anything that connects afterward — the process must stay attached (run
> detached via `nohup ... &`, never killed until the capture is done) for the
> hooks to still apply to a later call. Separately, some calls weren't caught
> by `DotnetProxyTrustManager` at all; adding hooks for
> `android.security.net.config.NetworkSecurityTrustManager` (Android's own
> Network Security Config trust manager, a separate gate from the app's own
> TLS code) plus a generic sweep of every loaded class implementing
> `X509TrustManager.checkServerTrusted` fixed it.

## Confirmed capture: cloud schedules (2026-07-22)

Live-captured while creating, then deleting, a disposable test schedule (via
the WireGuard-mode mitmproxy + Frida setup above). Real ids below are
placeholders.

**Create** is two Parse calls, not one — `functions/createTimeEvent_V3` is a
separate function from `functions/updateTimeEvent_V3`, confirmed distinct:

```
POST functions/createScene
  {siteId, sceneId, title, order: 0, sceneSteps: [...],
   hiddenFromSceneList: true, settings: "{\"SceneType\":\"AstroEventScene\"}"}
  -> {result: <int>}                      # no CreatedById yet

POST functions/createTimeEvent_V3
  {siteId, timeEventId: null, sceneId, scheduledDays, fadeTime, activated,
   dirtyDevices, dirtyRemovedDevices: [], dirtyRemove: false, mode: "astro",
   version: 2, start: {event, offset}, end: {event, offset},
   pauseStart: "00:00", pauseEnd: "00:00", targetDevices}
  -> {result: {targetDevices, eventId}}    # server generates the real id

POST functions/updateScene                # backfills CreatedById once eventId is known
  {siteId, sceneId, ..., settings: "{\"SceneType\":\"AstroEventScene\",\"CreatedById\":\"<eventId>\"}"}
  -> {result: true}
```

`version: 2` + `pauseStart`/`pauseEnd` (always `"00:00"` here) is what this
capture showed for a schedule *without* night reduction. An earlier capture of
an *existing* schedule *with* night reduction configured showed `version: 3`
and a `nightReduction` object instead of `pauseStart`/`pauseEnd` — both are
individually confirmed-real payloads, but which field actually controls the
switch between them wasn't re-verified in one session (not retested: enabling
night reduction on a fresh schedule to watch `version` change). See
[#106](https://github.com/8408323/ha-plejd/issues/106) and
[#121](https://github.com/8408323/ha-plejd/issues/121).

**Delete** is confirmed as a 5-call sequence:

```
1. updateTimeEvent_V3   — whole-state resend with dirtyRemove: true, dirtyDevices: []
2. updateScene          — the on-scene's step(s) resent with dirtyRemoved: true
3. removeTimeEvent_V3   — {siteId, timeEventId, mode: "astro", deviceIds} -> {result: true}
4. updateScene          — sceneSteps: [] (emptied)
5. removeScene          — {siteId, sceneId} -> {result: true}
```

`removeTimeEvent_V3` (step 3) is the dedicated deletion call — the earlier
`dirtyRemove: true` resend (step 1) isn't itself destructive, it just marks
intent before the real removal.

## Secrets

The crypto key, account credentials, session tokens, BLE addresses, and all
capture/decompile artifacts are secrets. `tools/apk/` and the capture artifacts
(`btsnoop_hci*`, `*.pcap`, `*.cfa`, `*.log`, `capture-*.txt`) are gitignored. The
decompiled app code is Plejd's copyrighted IP — keep it out of git; document facts
(opcodes, UUIDs, algorithms) in our own words instead.
