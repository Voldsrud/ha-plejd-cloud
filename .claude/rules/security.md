---
paths:
  - "custom_components/plejd_cloud/**"
  - "tools/**"
---

# Security

This integration logs in to the Plejd cloud once (to fetch the site's crypto key and device list), then controls and reads devices **locally over BLE**. The surface is credential handling, the site crypto key, and untrusted data coming off the BLE mesh — not web endpoints.

- The site **crypto key** is the master secret: anyone with it can control the whole mesh. Never log it, never commit it, never put it in a fixture or an issue. It lives only in the HA config entry.
- Validate and bound untrusted input at the boundary: BLE notifications, decrypted payloads, and cloud-API responses may be malformed or hostile. Never assume a field exists, has the expected type, or is in range. A decryption that doesn't authenticate must be dropped, not guessed at.
- Never log secrets, tokens, or PII — the Plejd account email/password, the session token, and the site crypto key must never reach `_LOGGER`. BLE addresses are PII-adjacent; redact them at info level and above.
- Keep credentials out of source and out of committed fixtures. They belong in `PLEJD_USER` / `PLEJD_PASS` (env / gitignored `.env`) and in the HA config entry, never hardcoded.
- Use constant-time comparison (`hmac.compare_digest`) when comparing keys, auth challenges, or message authentication tags.
- Don't build shell commands or paths from untrusted input. Use list-form `subprocess`, never `shell=True` with interpolation.
- Treat capture artifacts (`*.pcap`, `*.cfa`, `*.log`, `btsnoop_hci*`, `capture-*.txt`) as containing live secrets (the crypto-key exchange and auth challenges are in there) — keep them gitignored and never paste their raw contents into code, issues, or PRs.
