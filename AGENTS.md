CLI + Python library for Things 3, over the Things Cloud sync API. No local
Things app needed — it works headless (including on the mini).

Install with `uv tool install "git+https://github.com/IsakRein/Productivity.Things-Sync"`.

Credentials live in `~/.envrc` as `THINGS3_EMAIL` / `THINGS3_PASSWORD`;
`things auth` exchanges them once for the account's history key, cached in
`~/.config/things-cli/auth.json`. Folded state is cached in
`~/.cache/things-cli/state.json` — `things refresh` drops it.

**Writes are irreversible.** The history is an append-only journal, the server
accepts malformed data with a 200, and Things.app is what crashes — on every
device, permanently. The invariants that prevent this (canonical Base58
identifiers, the `st`/`sr`/`tir` combinations) are enforced at a single choke
point in `sync.py` and covered by tests; read the WRITE-SAFETY block there
before touching payload construction. Use `--dry-run` and a throwaway Things
Cloud account when developing write paths.

Run `uv run pytest` after changes. The suite is fully offline: unit tests plus
vendored fixtures from the reference implementations under
`tests/fixtures/reference/`.

Remember to update the skill under `~/.agents/skills/things-cli` with any
changes to the CLI.
