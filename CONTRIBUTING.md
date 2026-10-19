# Contributing

Thanks for your interest in ha-plejd. This is an unofficial, reverse-engineered
integration, so most features start from a capture of the Plejd app's behaviour.

## Development setup

```bash
uv sync --dev
uv run pytest tests/ -v --cov=custom_components/plejd_cloud --cov-fail-under=100
uv run ruff check custom_components/ tests/ tools/
uv run ruff format --check custom_components/ tests/ tools/
```

The test suite stubs Home Assistant (see `tests/conftest.py`), so it runs without
a full HA install.

## Workflow

- **`main` is protected.** No direct pushes — open a pull request. Merges are
  squash-only with linear history.
- **Branch naming:** use a type prefix — `feat/`, `fix/`, `chore/`, `docs/`,
  `tests/`, `ci/`, `refactor/`, `capture/`.
- **Keep PRs focused** — one logical change each. New behaviour needs tests; the
  coverage gate is 100%.
- **Required checks** must pass: `ruff`, `test (3.13)`, `hassfest`, `HACS
  validation`, and CodeQL.
- See the PR review loop in [.claude/rules/pr-review.md](.claude/rules/pr-review.md).

## Reverse engineering

If you're adding support for a device or feature, capture it first — see
[docs/reverse_engineering.md](docs/reverse_engineering.md). Record confirmed
findings in `const.py` / the docs, not just in a transient capture file.

## Sensitive data — do not commit

The site crypto key, Plejd account credentials, session tokens, BLE addresses, and
all capture artifacts (`*.pcap`, `*.cfa`, `btsnoop_hci*`, `capture-*.txt`) are
secrets. They are gitignored — keep them so, and redact before pasting anything
into an issue or PR.

## Reporting bugs

Use the issue templates. For suspected security problems, use the
[private advisory flow](https://github.com/8408323/ha-plejd/security/advisories/new)
instead of a public issue (see [SECURITY.md](SECURITY.md)).
