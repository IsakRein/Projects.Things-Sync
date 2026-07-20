"""Cloud-state machinery for Things Cloud.

The account's history is an append-only journal of commits; each commit is
a ``{uuid: {"t": op, "e": entity, "p": {...}}}`` map. Current state is the
fold of every commit in order. This module folds that journal into typed
entities, caches the result between invocations (so each run only pulls
the delta), and builds the wire payloads for writes.

Wire format notes, ported from evanpurkhiser/things3-cloud and
arthursoares/things-cloud-sdk rather than re-derived — the field
combinations below are load-bearing and several of them crash Things.app
if got wrong (see WRITE-SAFETY below).

WRITE-SAFETY invariants, all learned the hard way by those projects:

* Identifiers must be canonical Base58 (see :mod:`things_cli.base58`).
  The server accepts any string; Things.app crashes on anything that
  doesn't decode to exactly 16 bytes, and the journal cannot be rewound.
* ``st`` is a *start state*, not a list name. Today is ``st=1`` with
  ``sr``/``tir`` set to today; Anytime is ``st=1`` with no date; Someday
  is ``st=2``; Upcoming is ``st=2`` with a future date. ``st=2`` paired
  with today's date has no valid UI representation and crashes the app.
* Anything filed into a project, heading, or area is already triaged and
  must not be ``st=0`` (Inbox). Projects and headings are never ``st=0``.
* ``cd``/``md`` are fractional unix timestamps; integer truncation risks
  conflict-resolution ordering bugs.
"""

from __future__ import annotations

import json
import os
import time
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import api, base58
from .models import (
    Area,
    ChecklistItem,
    Heading,
    Project,
    StartBucket,
    Status,
    Tag,
    Todo,
)

# ---------- op / entity codes ----------

OP_CREATE = 0
OP_UPDATE = 1
OP_DELETE = 2

ENTITY_TASK = "Task6"
ENTITY_CHECKLIST = "ChecklistItem3"
ENTITY_TAG = "Tag4"
ENTITY_AREA = "Area3"

# Current entity names plus the legacy ones still present in old histories.
_KIND_BY_ENTITY = {
    "Task3": "task", "Task4": "task", "Task6": "task",
    "ChecklistItem": "checklist", "ChecklistItem2": "checklist",
    "ChecklistItem3": "checklist",
    "Tag3": "tag", "Tag4": "tag",
    "Area2": "area", "Area3": "area",
    "Tombstone": "tombstone", "Tombstone2": "tombstone",
}

# `tp` — a Task6 is a todo, a project, or a heading.
TYPE_TODO = 0
TYPE_PROJECT = 1
TYPE_HEADING = 2

_STATUS_BY_INT = {0: Status.OPEN, 2: Status.CANCELED, 3: Status.COMPLETED}
_INT_BY_STATUS = {v: k for k, v in _STATUS_BY_INT.items()}


class UNSET:
    """Sentinel distinguishing "field absent" from "field set to null".

    The wire protocol treats these differently: an absent key leaves the
    field alone, an explicit ``null`` clears it. Python's ``None`` alone
    can't express both, and conflating them either makes dates
    unclearable or silently wipes fields on every update.
    """

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "UNSET"


UNSET = UNSET()  # type: ignore[assignment]


class SyncError(Exception):
    """A user-facing sync/materialisation problem."""


# ---------- time helpers ----------


def now_ts() -> float:
    """Fractional unix seconds, as Things writes `cd`/`md`."""
    return time.time()


def day_ts(d: date) -> int:
    """A `sr`/`tir`/`dd` day timestamp: midnight UTC of that calendar day."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def day_from_ts(v: Any) -> date | None:
    if v is None:
        return None
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def dt_from_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    try:
        return datetime.fromtimestamp(float(v))
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def today() -> date:
    return date.today()


# ---------- notes ----------


def encode_notes(text: str) -> dict[str, Any]:
    """Modern structured note payload (`nt`): type 1 carries the whole
    body in `v` with a crc32 checksum in `ch`."""
    return {
        "_t": "tx",
        "t": 1,
        "ch": zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF,
        "v": text,
    }


def decode_notes(nt: Any) -> str:
    """Plain text out of either note representation.

    Legacy histories store a bare string (sometimes XML-ish); modern ones
    a structured object, either whole-text (`t=1`) or a paragraph list
    (`t=2`). Unicode line/paragraph separators are normalised to \\n.
    """
    if nt is None:
        return ""
    if isinstance(nt, str):
        return nt.replace(" ", "\n").replace(" ", "\n").strip()
    if isinstance(nt, dict):
        kind = nt.get("t")
        if kind == 1:
            v = nt.get("v") or ""
            return v.replace(" ", "\n").replace(" ", "\n").strip()
        if kind == 2:
            lines = [p.get("r") or "" for p in (nt.get("ps") or [])]
            return "\n".join(lines).strip()
    return ""


# ---------- journal folding ----------

RawState = dict[str, dict[str, Any]]


def fold_commits(commits: Iterable[dict[str, Any]], state: RawState | None = None) -> RawState:
    """Fold journal commits into ``{uuid: {"e": entity, "p": props}}``.

    ``t=0`` replaces an object wholesale, ``t=1`` shallow-merges its keys
    (a JSON ``null`` clears that field), ``t=2`` removes it. Unknown
    entity types are skipped rather than failing the whole pull — Cultured
    Code has bumped entity versions before and will again.
    """
    state = {} if state is None else state
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for uuid, change in commit.items():
            if not isinstance(change, dict):
                continue
            entity = change.get("e") or ""
            kind = _KIND_BY_ENTITY.get(entity)
            if kind is None or kind == "tombstone":
                # Tombstones record deletions of already-removed objects;
                # the delete op itself is what mutates state.
                continue
            op = change.get("t")
            props = change.get("p") or {}
            if op == OP_DELETE:
                state.pop(uuid, None)
            elif op == OP_CREATE:
                state[uuid] = {"e": entity, "k": kind, "p": dict(props)}
            elif op == OP_UPDATE:
                existing = state.get(uuid)
                if existing is None:
                    state[uuid] = {"e": entity, "k": kind, "p": dict(props)}
                else:
                    existing["p"].update(props)
                    existing["e"] = entity
    return state


# ---------- materialised state ----------


def _first(v: Any) -> str | None:
    """`pr`/`ar`/`agr` are arrays that hold at most one id in practice."""
    if isinstance(v, list) and v:
        return str(v[0])
    return None


def _tag_ids(v: Any) -> tuple[str, ...]:
    return tuple(str(x) for x in v) if isinstance(v, list) else ()


class State:
    """Materialised, queryable view of the folded journal."""

    def __init__(self, raw: RawState, head_index: int = 0) -> None:
        self.raw = raw
        self.head_index = head_index

        self.areas: dict[str, Area] = {}
        self.tags: dict[str, Tag] = {}
        self.todos: dict[str, Todo] = {}
        self.projects: dict[str, Project] = {}
        self.headings: dict[str, Heading] = {}
        self.checklists: dict[str, list[ChecklistItem]] = {}
        # Repeating generator rows, hidden from every list.
        self._template_projects: set[str] = set()

        self._build()

    # -- construction ---------------------------------------------------

    def _build(self) -> None:
        tasks: dict[str, dict[str, Any]] = {}
        checklist_rows: list[tuple[str, dict[str, Any]]] = []

        for uuid, obj in self.raw.items():
            kind, p = obj.get("k"), obj.get("p") or {}
            if kind == "area":
                self.areas[uuid] = Area(
                    id=uuid,
                    title=p.get("tt") or "",
                    tags=_tag_ids(p.get("tg")),
                    index=int(p.get("ix") or 0),
                )
            elif kind == "tag":
                self.tags[uuid] = Tag(
                    id=uuid,
                    title=p.get("tt") or "",
                    shortcut=p.get("sh") or "",
                    parent_id=_first(p.get("pn")),
                    index=int(p.get("ix") or 0),
                )
            elif kind == "task":
                tasks[uuid] = p
            elif kind == "checklist":
                checklist_rows.append((uuid, p))

        tag_title = {uid: t.title for uid, t in self.tags.items()}
        area_title = {uid: a.title for uid, a in self.areas.items()}

        # Projects and headings first — todos resolve their titles.
        for uuid, p in tasks.items():
            tp = int(p.get("tp") or 0)
            if tp == TYPE_PROJECT:
                if p.get("rr"):
                    self._template_projects.add(uuid)
                area_id = _first(p.get("ar"))
                self.projects[uuid] = Project(
                    id=uuid,
                    title=p.get("tt") or "",
                    notes=decode_notes(p.get("nt")),
                    status=_STATUS_BY_INT.get(int(p.get("ss") or 0), Status.OPEN),
                    when=day_from_ts(p.get("sr")),
                    deadline=day_from_ts(p.get("dd")),
                    trashed=bool(p.get("tr")),
                    area_id=area_id,
                    area_title=area_title.get(area_id or ""),
                    tags=tuple(
                        tag_title.get(t, t) for t in _tag_ids(p.get("tg"))
                    ),
                    created=dt_from_ts(p.get("cd")),
                    modified=dt_from_ts(p.get("md")),
                    stopped=dt_from_ts(p.get("sp")),
                    index=int(p.get("ix") or 0),
                )
            elif tp == TYPE_HEADING:
                self.headings[uuid] = Heading(
                    id=uuid,
                    title=p.get("tt") or "",
                    project_id=_first(p.get("pr")),
                    trashed=bool(p.get("tr")),
                    index=int(p.get("ix") or 0),
                )

        # Headings inherit their project's title for display.
        for uuid, h in list(self.headings.items()):
            proj = self.projects.get(h.project_id or "")
            if proj is not None:
                self.headings[uuid] = Heading(
                    id=h.id,
                    title=h.title,
                    project_id=h.project_id,
                    project_title=proj.title,
                    trashed=h.trashed,
                    index=h.index,
                )

        checklist_open: dict[str, int] = {}
        checklist_total: dict[str, int] = {}
        for uuid, p in checklist_rows:
            owner = _first(p.get("ts"))
            if owner is None:
                continue
            status = int(p.get("ss") or 0)
            item = ChecklistItem(
                id=uuid,
                title=p.get("tt") or "",
                completed=status == 3,
                stopped=dt_from_ts(p.get("sp")),
                index=int(p.get("ix") or 0),
            )
            self.checklists.setdefault(owner, []).append(item)
            checklist_total[owner] = checklist_total.get(owner, 0) + 1
            if status == 0:
                checklist_open[owner] = checklist_open.get(owner, 0) + 1
        for items in self.checklists.values():
            items.sort(key=lambda c: c.index)

        for uuid, p in tasks.items():
            if int(p.get("tp") or 0) != TYPE_TODO:
                continue
            heading_id = _first(p.get("agr"))
            heading = self.headings.get(heading_id or "")
            # A todo under a heading belongs to that heading's project even
            # when its own `pr` is empty.
            project_id = _first(p.get("pr")) or (heading.project_id if heading else None)
            project = self.projects.get(project_id or "")
            # Likewise an area can come via the parent project.
            area_id = _first(p.get("ar")) or (project.area_id if project else None)
            self.todos[uuid] = Todo(
                id=uuid,
                title=p.get("tt") or "",
                notes=decode_notes(p.get("nt")),
                status=_STATUS_BY_INT.get(int(p.get("ss") or 0), Status.OPEN),
                start=_start_bucket(p.get("st")),
                when=day_from_ts(p.get("sr")),
                deadline=day_from_ts(p.get("dd")),
                evening=bool(p.get("sb")),
                repeating=bool(p.get("rr")) or bool(p.get("rt")),
                is_template=bool(p.get("rr")),
                trashed=bool(p.get("tr")),
                project_id=project_id,
                project_title=project.title if project else None,
                area_id=area_id,
                area_title=area_title.get(area_id or ""),
                heading_id=heading_id,
                heading_title=heading.title if heading else None,
                tags=tuple(tag_title.get(t, t) for t in _tag_ids(p.get("tg"))),
                created=dt_from_ts(p.get("cd")),
                modified=dt_from_ts(p.get("md")),
                stopped=dt_from_ts(p.get("sp")),
                index=int(p.get("ix") or 0),
                today_index=int(p.get("ti") or 0),
                checklist_total=checklist_total.get(uuid, 0),
                checklist_open=checklist_open.get(uuid, 0),
            )

        # Open-todo counts per project, for the projects table.
        counts: dict[str, int] = {}
        for t in self.todos.values():
            if t.project_id and not t.trashed and t.status == Status.OPEN:
                counts[t.project_id] = counts.get(t.project_id, 0) + 1
        for uuid, proj in list(self.projects.items()):
            if counts.get(uuid):
                self.projects[uuid] = Project(
                    **{**proj.__dict__, "open_count": counts[uuid]}
                )

    # -- built-in lists -------------------------------------------------
    #
    # Derived from `st` + `sr` exactly as the Things UI derives them; see
    # the start-state matrix in this module's docstring.

    def _live(self) -> list[Todo]:
        """Open, untrashed todos whose container is alive.

        Repeating *templates* are excluded: they are hidden generator rows
        that spawn the real instances, and Things never shows them in a
        list. Instances (which carry `rt`, not `rr`) do show.
        """
        out = []
        for t in self.todos.values():
            if t.trashed or t.status != Status.OPEN or t.is_template:
                continue
            proj = self.projects.get(t.project_id or "")
            if proj is not None and (proj.trashed or proj.status != Status.OPEN):
                continue
            head = self.headings.get(t.heading_id or "")
            if head is not None and head.trashed:
                continue
            out.append(t)
        return out

    def inbox(self) -> list[Todo]:
        """Unfiled items only — anything already in a project or area has
        been triaged out of the Inbox even if it kept ``st=0``."""
        return _by_index(
            t
            for t in self._live()
            if t.start == StartBucket.INBOX
            and not t.project_id
            and not t.area_id
            and not t.heading_id
        )

    def today_list(self) -> list[Todo]:
        """Today = started-and-arrived, deferred-but-arrived ("staged"),
        anything parked in This Evening, and undated overdue deadlines."""
        d = today()
        items = [
            t
            for t in self._live()
            if (t.when is not None and t.when <= d)
            or (t.evening and t.start == StartBucket.ANYTIME)
            or (
                t.when is None
                and t.deadline is not None
                and t.deadline <= d
            )
        ]
        # Evening sorts below the rest, then by Things' own Today ordering.
        items.sort(key=lambda t: (t.evening, t.today_index, t.index))
        return items

    def upcoming(self) -> list[Todo]:
        d = today()
        items = [t for t in self._live() if t.when is not None and t.when > d]
        items.sort(key=lambda t: (t.when or d, t.index))
        return items

    def anytime(self) -> list[Todo]:
        """Started items that aren't waiting on a future date (a
        future-scheduled item belongs to Upcoming, not Anytime)."""
        d = today()
        return _by_index(
            t
            for t in self._live()
            if t.start == StartBucket.ANYTIME and (t.when is None or t.when <= d)
        )

    def someday(self) -> list[Todo]:
        """Deferred, undated, and not inside a project — project-scoped
        someday items live under their project, not in the global list."""
        return _by_index(
            t
            for t in self._live()
            if t.start == StartBucket.SOMEDAY
            and t.when is None
            and not t.project_id
        )

    def logbook(self, limit: int | None = None) -> list[Todo]:
        items = [
            t
            for t in self.todos.values()
            if not t.trashed and t.status != Status.OPEN
        ]
        items.sort(key=lambda t: t.stopped or datetime.min, reverse=True)
        return items[:limit] if limit else items

    def trash(self) -> list[Todo]:
        return _by_index(t for t in self.todos.values() if t.trashed)

    def deadlines(self) -> list[Todo]:
        items = [t for t in self._live() if t.deadline is not None]
        items.sort(key=lambda t: (t.deadline or date.max, t.index))
        return items

    def open_projects(self) -> list[Project]:
        items = [
            p
            for p in self.projects.values()
            if not p.trashed
            and p.status == Status.OPEN
            and p.id not in self._template_projects
        ]
        items.sort(key=lambda p: p.index)
        return items

    def all_projects(self) -> list[Project]:
        items = [
            p
            for p in self.projects.values()
            if not p.trashed and p.id not in self._template_projects
        ]
        items.sort(key=lambda p: p.index)
        return items

    def project_headings(self, project_id: str) -> list[Heading]:
        return sorted(
            (h for h in self.headings.values()
             if h.project_id == project_id and not h.trashed),
            key=lambda h: h.index,
        )

    def todos_in_project(self, project_id: str, *, open_only: bool = False) -> list[Todo]:
        items = [
            t
            for t in self.todos.values()
            if t.project_id == project_id and not t.trashed
            and (not open_only or t.status == Status.OPEN)
        ]
        return sorted(items, key=lambda t: t.index)

    def todos_in_area(self, area_id: str, *, open_only: bool = True) -> list[Todo]:
        items = [
            t
            for t in self.todos.values()
            if t.area_id == area_id and not t.trashed
            and (not open_only or t.status == Status.OPEN)
        ]
        return sorted(items, key=lambda t: t.index)

    def todos_with_tag(self, tag_title: str) -> list[Todo]:
        return _by_index(t for t in self._live() if tag_title in t.tags)

    def search(self, query: str) -> tuple[list[Todo], list[Project]]:
        q = query.lower()
        todos = [
            t
            for t in self.todos.values()
            if not t.trashed and (q in t.title.lower() or q in t.notes.lower())
        ]
        projects = [
            p
            for p in self.projects.values()
            if not p.trashed and (q in p.title.lower() or q in p.notes.lower())
        ]
        return sorted(todos, key=lambda t: t.index), sorted(projects, key=lambda p: p.index)

    # -- resolution -----------------------------------------------------

    def resolve(self, ref: str) -> tuple[str, str]:
        """Resolve a reference to ``(kind, uuid)``.

        Accepts a full id, a unique id prefix, or an exact title (for
        projects, areas, and tags). Raises :class:`SyncError` when nothing
        matches or the prefix is ambiguous.
        """
        ref = ref.strip()
        if not ref:
            raise SyncError("empty identifier")

        buckets: list[tuple[str, dict[str, Any]]] = [
            ("todo", self.todos),
            ("project", self.projects),
            ("heading", self.headings),
            ("area", self.areas),
            ("tag", self.tags),
        ]
        for kind, bucket in buckets:
            if ref in bucket:
                return kind, ref

        # Exact title across every kind. Titles aren't unique in Things, so
        # more than one hit is refused rather than guessed at — this same
        # resolver backs `delete` and `complete`.
        titled = [
            (kind, uid)
            for kind, bucket in buckets
            for uid, obj in bucket.items()
            if obj.title == ref and not getattr(obj, "trashed", False)
        ]
        if len(titled) == 1:
            return titled[0]
        if len(titled) > 1:
            shown = ", ".join(f"{k} {u[:8]}" for k, u in titled[:5])
            raise SyncError(
                f"{len(titled)} items are titled {ref!r} ({shown}) — use an id"
            )

        matches = [
            (kind, uid)
            for kind, bucket in buckets
            for uid in bucket
            if uid.startswith(ref)
        ]
        if not matches:
            raise SyncError(f"nothing matches {ref!r}")
        if len(matches) > 1:
            shown = ", ".join(uid[:10] for _, uid in matches[:5])
            raise SyncError(f"{ref!r} is ambiguous ({shown})")
        return matches[0]

    def require(self, ref: str, *kinds: str) -> str:
        kind, uuid = self.resolve(ref)
        if kinds and kind not in kinds:
            raise SyncError(f"{ref!r} is a {kind}, expected {' or '.join(kinds)}")
        return uuid

    def tag_ids_for(self, names: Iterable[str]) -> list[str]:
        """Map tag titles (or ids/prefixes) onto tag ids."""
        by_title = {t.title: uid for uid, t in self.tags.items()}
        out = []
        for name in names:
            name = name.strip()
            if not name:
                continue
            if name in by_title:
                out.append(by_title[name])
                continue
            matches = [uid for uid in self.tags if uid.startswith(name)]
            if len(matches) == 1:
                out.append(matches[0])
            else:
                raise SyncError(f"unknown tag {name!r} (create it in Things first)")
        return out

    # -- sort placement ---------------------------------------------------
    #
    # `ix` orders an item within its container and `ti` within Today.
    # Creating everything at 0 collides every item at the same position and
    # leaves ordering to an undefined tiebreak, so new items are placed
    # after their current siblings instead.

    def next_todo_index(
        self,
        *,
        project_id: str | None = None,
        area_id: str | None = None,
        heading_id: str | None = None,
    ) -> int:
        def sibling(t: Todo) -> bool:
            if heading_id:
                return t.heading_id == heading_id
            if project_id:
                return t.project_id == project_id and not t.heading_id
            if area_id:
                return t.area_id == area_id and not t.project_id
            return not t.project_id and not t.area_id

        return _after([t.index for t in self.todos.values()
                       if not t.trashed and sibling(t)])

    def next_today_index(self) -> int:
        return _after([t.today_index for t in self.today_list()])

    def next_project_index(self, area_id: str | None = None) -> int:
        return _after([p.index for p in self.projects.values()
                       if not p.trashed and p.area_id == area_id])

    def next_area_index(self) -> int:
        return _after([a.index for a in self.areas.values()])

    def title_of(self, uuid: str) -> str:
        for bucket in (self.todos, self.projects, self.headings, self.areas, self.tags):
            obj = bucket.get(uuid)
            if obj is not None:
                return obj.title
        return uuid[:8]

    def entity_of(self, uuid: str) -> str:
        obj = self.raw.get(uuid)
        return (obj or {}).get("e") or ENTITY_TASK


def _start_bucket(v: Any) -> StartBucket:
    try:
        return StartBucket(int(v or 0))
    except (ValueError, TypeError):
        return StartBucket.ANYTIME


def _by_index(items: Iterable[Todo]) -> list[Todo]:
    return sorted(items, key=lambda t: t.index)


def _after(indices: list[int]) -> int:
    """One past the highest sibling index, so a new item lands last."""
    return max(indices) + 1 if indices else 0


# ---------- state cache ----------
#
# The journal is append-only, so a cached fold plus the head index lets each
# run pull only new commits. Pure performance: delete it any time and the
# next run does a full pull.

CACHE_PATH = Path(
    os.environ.get("THINGS_CLI_CACHE")
    or os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "things-cli" / "state.json"


def load_cache(history_key: str) -> tuple[RawState, int]:
    """Cached ``(raw_state, head_index)``, or ``({}, 0)`` on any miss —
    including a different account, so a re-auth never mixes histories."""
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}, 0
    if data.get("history_key") != history_key or data.get("version") != 1:
        return {}, 0
    raw = data.get("state")
    if not isinstance(raw, dict):
        return {}, 0
    return raw, int(data.get("head_index") or 0)


def save_cache(history_key: str, raw: RawState, head_index: int) -> None:
    payload = {
        "version": 1,
        "history_key": history_key,
        "head_index": head_index,
        "state": raw,
    }
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except OSError:
        pass  # cache is optional; never fail a command over it


def clear_cache() -> None:
    CACHE_PATH.unlink(missing_ok=True)


def fetch_state(client: api.Client | None = None) -> tuple[State, api.Client]:
    """Pull new commits since the cached head and return the folded state."""
    client = client or api.Client.connect()
    key = client.session.history_key
    raw, head = load_cache(key)
    commits, new_head = client.pull_items(head)
    if commits:
        raw = fold_commits(commits, raw)
        save_cache(key, raw, new_head)
    elif head == 0:
        save_cache(key, raw, new_head)
    return State(raw, new_head), client


# ---------- write payloads ----------


def _conflict_meta() -> dict[str, Any]:
    """CRDT conflict metadata; clients pass this through verbatim."""
    return {"_t": "oo", "sn": {}}


def empty_note() -> dict[str, Any]:
    return {"_t": "tx", "ch": 0, "t": 1, "v": ""}


def _base_props(title: str, now: float) -> dict[str, Any]:
    """A complete Task6 property set, with Things' own defaults.

    Every key Things.app writes on a create must be present, even when it
    holds a default. A partial create is accepted by the server and even
    renders in a running client, but the object is DISCARDED when
    Things.app rebuilds its store from the journal — so the items silently
    vanish on next launch. (The reference Rust implementation gets this for
    free: its struct serialises every field. Building a dict of only the
    fields you set does not.)
    """
    return {
        "acrd": None,          # after-completion reference day
        "agr": [],             # heading (action group)
        "ar": [],              # area
        "ato": None,           # alarm time offset
        "cd": now,             # creation timestamp
        "dd": None,            # deadline day
        "dds": None,           # deadline suppression day
        "dl": [],              # deadline list metadata
        "do": 0,               # due-date offset
        "icc": 0,              # instance/checklist count
        "icp": False,          # instance creation paused
        "icsd": None,          # instance creation suppressed day
        "ix": 0,               # sort index in its container
        "lai": None,           # last alarm interaction
        "lt": False,           # leaves tombstone on delete
        "md": now,             # modification timestamp
        "nt": empty_note(),    # notes
        "pr": [],              # parent project
        "rmd": None,           # reminder metadata
        "rp": None,            # reminder payload
        "rr": None,            # recurrence rule
        "rt": [],              # recurrence template link
        "sb": 0,               # evening bit
        "sp": None,            # stop (completion) timestamp
        "sr": None,            # scheduled day
        "ss": 0,               # status
        "st": StartBucket.INBOX.value,  # start state
        "tg": [],              # tags
        "ti": 0,               # Today sort index
        "tir": None,           # today index reference day
        "tp": TYPE_TODO,       # type
        "tr": False,           # trashed
        "tt": title,           # title
        "xx": _conflict_meta(),
    }


def apply_when(props: dict[str, Any], when: str | None, *, evening: bool = False) -> None:
    """Set the `st`/`sr`/`tir`/`sb` group from a human schedule word.

    Accepts today / evening / anytime / someday / YYYY-MM-DD. The
    combinations here are the ones the real client emits — see this
    module's WRITE-SAFETY notes before changing any of them.
    """
    if when is None:
        return
    w = when.strip().lower()
    if w == "anytime":
        props["st"] = StartBucket.ANYTIME.value
        props["sr"] = None
        props["tir"] = None
    elif w == "someday":
        props["st"] = StartBucket.SOMEDAY.value
        props["sr"] = None
        props["tir"] = None
    elif w in ("today", "evening"):
        ts = day_ts(today())
        props["st"] = StartBucket.ANYTIME.value
        props["sr"] = ts
        props["tir"] = ts
        if w == "evening" or evening:
            props["sb"] = 1
    elif w == "inbox":
        props["st"] = StartBucket.INBOX.value
        props["sr"] = None
        props["tir"] = None
    else:
        d = parse_day(when)
        ts = day_ts(d)
        if d <= today():
            # A past/today date is Today, never a deferred item — `st=2`
            # with today's date crashes Things.
            props["st"] = StartBucket.ANYTIME.value
        else:
            props["st"] = StartBucket.SOMEDAY.value
        props["sr"] = ts
        props["tir"] = ts
    if evening and props.get("st") == StartBucket.ANYTIME.value:
        props["sb"] = 1


def parse_day(s: str) -> date:
    """Parse YYYY-MM-DD, or today/tomorrow."""
    v = s.strip().lower()
    if v == "today":
        return today()
    if v == "tomorrow":
        from datetime import timedelta

        return today() + timedelta(days=1)
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise SyncError(f"could not parse date {s!r} (expected YYYY-MM-DD)")


def build_create_todo(
    title: str,
    *,
    notes: str | None = None,
    when: str | None = None,
    deadline: str | None = None,
    tag_ids: list[str] | None = None,
    project_id: str | None = None,
    area_id: str | None = None,
    heading_id: str | None = None,
    checklist: list[str] | None = None,
    evening: bool = False,
    index: int = 0,
    today_index: int = 0,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Build the changes map for a new todo (plus any checklist items).

    ``index`` / ``today_index`` place the item among its siblings; get them
    from :meth:`State.next_todo_index` / :meth:`State.next_today_index` so
    every new item doesn't collide at position 0.

    Returns ``(todo_uuid, changes)``.
    """
    now = now_ts()
    uuid = base58.new_uuid()
    props = _base_props(title, now)
    props["ix"] = index
    props["ti"] = today_index

    if project_id:
        props["pr"] = [base58.validate(project_id)]
    if heading_id:
        props["agr"] = [base58.validate(heading_id)]
    if area_id:
        props["ar"] = [base58.validate(area_id)]
    # Anything filed somewhere is triaged, so it must leave the Inbox state.
    if project_id or heading_id or area_id:
        props["st"] = StartBucket.ANYTIME.value

    apply_when(props, when, evening=evening)
    if deadline:
        props["dd"] = day_ts(parse_day(deadline))
    if notes:
        props["nt"] = encode_notes(notes)
    if tag_ids:
        props["tg"] = [base58.validate(t) for t in tag_ids]

    items = list(checklist or [])
    if items:
        props["icc"] = len(items)

    changes: dict[str, dict[str, Any]] = {
        uuid: {"t": OP_CREATE, "e": ENTITY_TASK, "p": props}
    }
    for i, item in enumerate(items):
        changes[base58.new_uuid()] = {
            "t": OP_CREATE,
            "e": ENTITY_CHECKLIST,
            "p": {
                "cd": now,
                "ix": i,
                "lt": False,
                "md": now,
                "sp": None,
                "ss": 0,
                "ts": [uuid],
                "tt": item,
                "xx": _conflict_meta(),
            },
        }
    return uuid, changes


def build_create_project(
    title: str,
    *,
    notes: str | None = None,
    when: str | None = None,
    deadline: str | None = None,
    tag_ids: list[str] | None = None,
    area_id: str | None = None,
    index: int = 0,
) -> tuple[str, dict[str, dict[str, Any]]]:
    now = now_ts()
    uuid = base58.new_uuid()
    props = _base_props(title, now)
    props["ix"] = index
    props["tp"] = TYPE_PROJECT
    # Projects are never Inbox items.
    props["st"] = StartBucket.ANYTIME.value

    if area_id:
        props["ar"] = [base58.validate(area_id)]
    apply_when(props, when)
    if props.get("st") == StartBucket.INBOX.value:
        props["st"] = StartBucket.ANYTIME.value
    if deadline:
        props["dd"] = day_ts(parse_day(deadline))
    if notes:
        props["nt"] = encode_notes(notes)
    if tag_ids:
        props["tg"] = [base58.validate(t) for t in tag_ids]
    return uuid, {uuid: {"t": OP_CREATE, "e": ENTITY_TASK, "p": props}}


def build_create_area(
    title: str, *, index: int = 0
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Full Area3 property set — see :func:`_base_props` on why partial
    creates disappear."""
    uuid = base58.new_uuid()
    props = {"ix": index, "tg": [], "tt": title, "xx": _conflict_meta()}
    return uuid, {uuid: {"t": OP_CREATE, "e": ENTITY_AREA, "p": props}}


def build_create_tag(
    title: str, *, index: int = 0
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Full Tag4 property set."""
    uuid = base58.new_uuid()
    props = {"ix": index, "pn": [], "sh": None, "tt": title, "xx": _conflict_meta()}
    return uuid, {uuid: {"t": OP_CREATE, "e": ENTITY_TAG, "p": props}}


def build_update(
    uuid: str,
    entity: str = ENTITY_TASK,
    *,
    title: Any = UNSET,
    notes: Any = UNSET,
    when: Any = UNSET,
    deadline: Any = UNSET,
    tag_ids: Any = UNSET,
    project_id: Any = UNSET,
    area_id: Any = UNSET,
    heading_id: Any = UNSET,
    status: Any = UNSET,
    trashed: Any = UNSET,
    evening: Any = UNSET,
) -> dict[str, dict[str, Any]]:
    """Build a sparse update patch.

    Every parameter defaults to :data:`UNSET` — omitted keys leave the
    field untouched. Passing ``None`` where the protocol allows it emits
    an explicit null, which *clears* that field.
    """
    base58.validate(uuid)
    props: dict[str, Any] = {"md": now_ts()}

    if title is not UNSET:
        props["tt"] = title
    if notes is not UNSET:
        props["nt"] = encode_notes(notes) if notes else encode_notes("")
    if when is not UNSET:
        if when is None:
            props["st"] = StartBucket.ANYTIME.value
            props["sr"] = None
            props["tir"] = None
        else:
            apply_when(props, when, evening=bool(evening is True))
    if deadline is not UNSET:
        props["dd"] = day_ts(parse_day(deadline)) if deadline else None
    if tag_ids is not UNSET:
        props["tg"] = [base58.validate(t) for t in (tag_ids or [])]
    if project_id is not UNSET:
        props["pr"] = [base58.validate(project_id)] if project_id else []
    if heading_id is not UNSET:
        props["agr"] = [base58.validate(heading_id)] if heading_id else []
    if area_id is not UNSET:
        props["ar"] = [base58.validate(area_id)] if area_id else []
    # Moving something into a container triages it out of the Inbox.
    if (
        (project_id is not UNSET and project_id)
        or (heading_id is not UNSET and heading_id)
        or (area_id is not UNSET and area_id)
    ) and when is UNSET:
        props["st"] = StartBucket.ANYTIME.value
    if evening is not UNSET and when is UNSET:
        props["sb"] = 1 if evening else 0
    if status is not UNSET:
        props["ss"] = _INT_BY_STATUS[status]
        props["sp"] = now_ts() if status != Status.OPEN else None
    if trashed is not UNSET:
        props["tr"] = bool(trashed)

    return {uuid: {"t": OP_UPDATE, "e": entity, "p": props}}


def build_delete(uuid: str, entity: str = ENTITY_TASK) -> dict[str, dict[str, Any]]:
    base58.validate(uuid)
    return {uuid: {"t": OP_DELETE, "e": entity, "p": {}}}


def build_delete_many(items: Iterable[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    """Delete several objects in one commit, from ``(uuid, entity)`` pairs.

    Used to take a todo's checklist items down with it — the journal has no
    cascade, so deleting only the parent would strand its children forever.
    """
    changes: dict[str, dict[str, Any]] = {}
    for uuid, entity in items:
        changes.update(build_delete(uuid, entity))
    return changes


def commit(client: api.Client, state: State, changes: dict[str, dict[str, Any]]) -> int:
    """Validate then push a changes map, returning the new head index.

    Every identifier is re-validated here: this is the last point before
    a malformed id would reach the server, where it becomes permanent.
    """
    for uuid in changes:
        base58.validate(uuid)
    head = client.commit(changes, state.head_index)
    # The commit is now upstream; drop the cache so the next read re-folds
    # it rather than showing pre-write state.
    clear_cache()
    return head
