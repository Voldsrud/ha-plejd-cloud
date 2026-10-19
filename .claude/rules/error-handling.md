---
paths:
  - "custom_components/plejd_cloud/**"
---

# Error Handling

Follow Home Assistant conventions for an integration that fetches a site config from a cloud API once and then talks to a BLE mesh.

- Raise the right typed HA exception, not a bare `Exception`: `ConfigEntryAuthFailed` for expired/invalid Plejd-cloud credentials (triggers reauth), `ConfigEntryNotReady` for transient setup failures including "no mesh device in range yet" (triggers retry), `UpdateFailed` from the coordinator's update method, and `HomeAssistantError` for user-facing service/command failures.
- Never swallow errors silently. If you catch, either re-raise with added context or log at the appropriate level with what operation failed. A bare `except: pass` hides real bugs.
- Distinguish expected failures from bugs. A BLE device dropping out of range, a notification that fails to authenticate, or a field absent in a payload is expected — handle it locally. An unexpected decrypt/parse error is a bug — let it propagate so it's visible, don't mask it with a default.
- Retry only transient failures (BLE disconnect, GATT timeout, device out of range, cloud 5xx) with backoff. Fail fast on auth and validation errors — retrying them just delays the reauth flow.
- Don't leak the crypto key, account credentials, or raw payloads in exception messages or logs — they surface in the HA UI and logs.
- Every awaited call has an owner: don't fire-and-forget coroutines. Use `hass.async_create_task` (or `entry.async_create_background_task`) for BLE reconnect loops and notification handlers so failures are surfaced, not lost.
