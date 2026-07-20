# things-cli

A self-contained interface to **Things 3**, talking directly to Cultured
Code's private **Things Cloud** sync backend. No Things app required —
reads and writes work from any machine with the account credentials,
including headless ones (the Mac mini).

Same shape as [`tt`](https://github.com/IsakRein/Projects.Timelines-Sync):
a `Client` for the sync API, a folded state cache so each run only pulls
the delta, and a rich-rendered CLI over the top.

```bash
things today
things add "Book flights" --when today --deadline 2026-08-01 --tags travel
things complete a1b2c3d4
```

## Install

```bash
uv tool install "git+https://github.com/IsakRein/Projects.Things-Sync"
```

For development:

```bash
uv sync --extra dev
uv run pytest
```

## Auth

Things Cloud authenticates with your account email + password, which are
exchanged **once** for the account's `history-key`. That key is the real
credential for every subsequent request, so the password is never stored
and never sent again.

Add the credentials to `~/.envrc` (direnv-loaded, untracked):

```bash
export THINGS3_EMAIL=you@example.com
export THINGS3_PASSWORD='…'
```

Then:

```bash
direnv allow ~
things auth        # fetches the history key → ~/.config/things-cli/auth.json (0600)
things doctor      # verifies credentials, reachability, and the history
```

`things auth` is optional — the first command that needs the cloud will
authenticate and persist the session itself.

## Commands

Reads:

| Command | What |
|---|---|
| `status` | account summary + today's load |
| `inbox` / `today` / `upcoming` / `anytime` / `someday` | built-in lists |
| `logbook [--limit N]` / `trash` / `deadlines` | history and due work |
| `todos [--project/--area/--tag/--search/--all]` | filtered todos |
| `projects [--area] [--all]`, `areas`, `tags` | containers |
| `show <id>` | a todo/project/area/tag/heading in detail |
| `search <query>` | title/notes across todos and projects |

Writes:

| Command | What |
|---|---|
| `add <title>` | new todo (`--when`, `--deadline`, `--notes`, `--tags`, `--project`, `--area`, `--heading`, `--checklist`, `--evening`) |
| `add-project` / `add-area` | new containers |
| `edit <id>` | change only the fields you pass |
| `complete` / `cancel` / `reopen` `<id>` | status |
| `delete <id> --yes` | remove |

Every write takes `--dry-run`, which prints the exact wire payload instead
of sending it. Every read takes `--json`.

Identifiers are Things' own; anywhere an `<id>` is accepted you can pass a
full id, a unique id prefix (the 8-char one the tables show), or — for
projects, areas, and tags — an exact title.

### Scheduling

`--when` takes `today`, `evening`, `anytime`, `someday`, `tomorrow`, or
`YYYY-MM-DD`. On `edit`, an empty value clears the field:

```bash
things edit a1b2c3d4 --when=          # unschedule
things edit a1b2c3d4 --deadline=      # drop the deadline
things edit a1b2c3d4 --project=       # move out of its project
```

Omitting a flag leaves that field alone — passing it empty clears it. The
two are genuinely different on the wire, so the distinction is preserved
end to end.

## As a library

```python
from things_cli import fetch_state

state, client = fetch_state()

for todo in state.today_list():
    print(todo.title, todo.project_title)

overdue = [t for t in state.deadlines() if t.deadline < date.today()]
```

`State` exposes `todos`, `projects`, `headings`, `areas`, `tags`,
`checklists` as dicts keyed by id, the built-in list methods above, and
`resolve()` / `require()` for turning user-supplied references into ids.
Entities are frozen dataclasses in `things_cli.models`.

## How it works

The account's history is an **append-only journal**. Each commit is a
`{id: {"t": op, "e": entity, "p": props}}` map; current state is the fold
of every commit in order (`t=0` create, `t=1` sparse patch, `t=2` delete).
Writes POST a new commit against the head index.

```
things_cli/
  api.py       — HTTP client: auth, item pagination, commit
  sync.py      — journal folding, state cache, wire payload builders
  base58.py    — canonical identifier codec
  models.py    — frozen dataclasses
  cli.py       — commands + rich rendering
```

The folded state is cached at `~/.cache/things-cli/state.json` with the
head index, so each invocation pulls only new commits. It's a performance
hint, not data — delete it any time, or run `things refresh`.

## Write safety

This speaks a private, undocumented protocol. Reads are harmless. **Writes
are not**, and the failure mode is unusual enough to be worth stating
plainly:

- The journal is append-only. A bad item cannot be deleted or rolled back.
- The server accepts anything — you get an HTTP 200 either way. It's
  *Things.app* that rejects malformed data, by crashing, on every device
  on the account, forever after.
- The known landmines are identifier encoding and a handful of field
  combinations (`st=2` with today's date; `st=0` on anything filed into a
  container, or on a project/heading).

Those invariants are ported from — and credited to —
[evanpurkhiser/things3-cloud](https://github.com/evanpurkhiser/things3-cloud)
and [arthursoares/things-cloud-sdk](https://github.com/arthursoares/things-cloud-sdk)
(both MIT), whose maintainers found them the hard way. They're enforced
here at a single choke point in `sync.py` and covered by tests, including
a 2000-draw canonical-identifier check for the leading-zero-byte bug that
silently corrupts roughly 1 in 256 naively-encoded ids.

**Exercise writes against a throwaway Things Cloud account before pointing
this at your real one.** `--dry-run` prints the exact payload without
sending it.
