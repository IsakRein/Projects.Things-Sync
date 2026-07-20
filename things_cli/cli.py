"""`things` CLI — a self-contained interface to Things 3.

Reads go straight to Things' SQLite database (fast, works with the app in
the background); writes go through the Things URL scheme; deletes through
AppleScript. See README for the architecture.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from things_cli import applescript, urlscheme
from things_cli.db import DBError, ThingsDB, default_db_path
from things_cli.models import (
    Project,
    StartBucket,
    Status,
    Todo,
    to_json_dict,
)
from things_cli.urlscheme import UrlError

console = Console()
err_console = Console(stderr=True)


class CliError(Exception):
    """A user-facing argument-parsing error."""


@dataclass
class Flag:
    name: str            # long option, e.g. "--when"
    takes_value: bool
    help: str
    metavar: str = ""    # placeholder shown in help for value flags

    @property
    def attr(self) -> str:
        return self.name.lstrip("-").replace("-", "_")


@dataclass
class Arg:
    name: str            # positional name, e.g. "id"
    help: str
    required: bool = True
    variadic: bool = False   # captures all remaining positionals, space-joined


@dataclass
class Command:
    name: str
    help: str
    func: Callable[[SimpleNamespace], int]
    flags: list[Flag] = field(default_factory=list)
    args: list[Arg] = field(default_factory=list)


def _error(msg: str) -> int:
    err_console.print(f"[bold red]error:[/bold red] {msg}")
    return 1


def get_db() -> ThingsDB:
    """Open the database or exit with a clear error."""
    try:
        return ThingsDB()
    except DBError as e:
        raise SystemExit(_error(str(e)))


# ---------- rendering ----------


def _when_cell(t: Todo, today: date) -> Text:
    if t.when is not None:
        if t.when <= today:
            return Text("evening" if t.evening else "today", style="yellow")
        return Text(t.when.isoformat(), style="cyan")
    if t.start == StartBucket.INBOX:
        return Text("inbox", style="dim")
    if t.start == StartBucket.SOMEDAY:
        return Text("someday", style="dim")
    return Text("")


def _deadline_cell(d: date | None, today: date) -> Text:
    if d is None:
        return Text("")
    if d < today:
        return Text(d.isoformat(), style="bold red")
    if d == today:
        return Text(d.isoformat(), style="yellow")
    return Text(d.isoformat())


_STATUS_GLYPH = {
    Status.OPEN: Text("○", style="dim"),
    Status.COMPLETED: Text("✓", style="green"),
    Status.CANCELED: Text("✗", style="red"),
}


def _title_cell(t: Todo) -> Text:
    title = Text()
    if t.repeating:
        title.append("↻ ", style="dim")
    title.append(t.title or "(untitled)")
    if t.checklist_total:
        done = t.checklist_total - t.checklist_open
        title.append(f"  ☑ {done}/{t.checklist_total}", style="dim")
    return title


def todos_table(todos: list[Todo], *, show_status: bool = False) -> Table:
    today = date.today()
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    if show_status:
        table.add_column("", no_wrap=True)
    table.add_column("Title")
    table.add_column("When", no_wrap=True)
    table.add_column("Deadline", no_wrap=True)
    table.add_column("Where", style="dim")
    table.add_column("Tags", style="magenta")
    for t in todos:
        row: list = [t.id[:8]]
        if show_status:
            row.append(_STATUS_GLYPH.get(t.status, Text("?")))
        where = t.project_title or t.area_title or ""
        row += [
            _title_cell(t),
            _when_cell(t, today),
            _deadline_cell(t.deadline, today),
            where,
            ", ".join(t.tags),
        ]
        table.add_row(*row)
    return table


def projects_table(projects: list[Project], *, show_status: bool = False) -> Table:
    today = date.today()
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    if show_status:
        table.add_column("", no_wrap=True)
    table.add_column("Project")
    table.add_column("Open", justify="right", no_wrap=True)
    table.add_column("Deadline", no_wrap=True)
    table.add_column("Area", style="dim")
    table.add_column("Tags", style="magenta")
    for p in projects:
        row: list = [p.id[:8]]
        if show_status:
            row.append(_STATUS_GLYPH.get(p.status, Text("?")))
        row += [
            Text(p.title or "(untitled)"),
            str(p.open_count) if p.open_count else "",
            _deadline_cell(p.deadline, today),
            p.area_title or "",
            ", ".join(p.tags),
        ]
        table.add_row(*row)
    return table


def _print_todos(todos: list[Todo], args: SimpleNamespace, *, show_status: bool = False) -> int:
    if getattr(args, "json", False):
        print(json.dumps([to_json_dict(t) for t in todos], indent=2))
        return 0
    if not todos:
        console.print("[dim]no todos[/dim]")
        return 0
    console.print(todos_table(todos, show_status=show_status))
    return 0


# ---------- reference resolution ----------


def _resolve_kind(db: ThingsDB, ref: str, kinds: tuple[str, ...]) -> tuple[str, str]:
    """Resolve a UUID/prefix and require one of the given kinds."""
    kind, uuid = db.resolve(ref)
    if kind not in kinds:
        raise DBError(f"{ref!r} is a {kind}, expected {' or '.join(kinds)}")
    return kind, uuid


def _project_id_for(db: ThingsDB, ref: str) -> str:
    """A project given by exact title, full UUID, or UUID prefix."""
    p = db.project_by_name(ref)
    if p is not None:
        return p.id
    return _resolve_kind(db, ref, ("project",))[1]


def _area_id_for(db: ThingsDB, ref: str) -> str:
    a = db.area_by_name(ref)
    if a is not None:
        return a.id
    return _resolve_kind(db, ref, ("area",))[1]


def _list_id_for(db: ThingsDB, ns: SimpleNamespace) -> str | None:
    """--project / --area on add/update → a URL-scheme list-id."""
    if getattr(ns, "project", None):
        return _project_id_for(db, ns.project)
    if getattr(ns, "area", None):
        return _area_id_for(db, ns.area)
    return None


def _split_multi(v: str | None) -> list[str] | None:
    """Split a comma-separated multi-value flag (--checklist, --todos)."""
    if not v:
        return None
    return [p.strip() for p in v.split(",") if p.strip()]


# ---------- list commands ----------


def _list_cmd(fetch: Callable[[ThingsDB], list[Todo]], *, show_status: bool = False):
    def run(args: SimpleNamespace) -> int:
        try:
            todos = fetch(get_db())
        except DBError as e:
            return _error(str(e))
        return _print_todos(todos, args, show_status=show_status)

    return run


def cmd_logbook(args: SimpleNamespace) -> int:
    try:
        limit = int(args.limit) if args.limit else 25
    except ValueError:
        return _error("--limit must be an integer")
    try:
        todos = get_db().logbook(limit=limit)
    except DBError as e:
        return _error(str(e))
    return _print_todos(todos, args, show_status=True)


def cmd_todos(args: SimpleNamespace) -> int:
    db = get_db()
    status: Status | None = Status.OPEN
    if args.status:
        if args.status == "all":
            status = None
        else:
            try:
                status = Status(args.status)
            except ValueError:
                return _error("--status must be open, completed, canceled, or all")
    try:
        project_id = _project_id_for(db, args.project) if args.project else None
        area_id = _area_id_for(db, args.area) if args.area else None
        todos = db.todos(
            status=status,
            project_id=project_id,
            area_id=area_id,
            tag=args.tag,
            query=args.search,
        )
    except DBError as e:
        return _error(str(e))
    return _print_todos(todos, args, show_status=status is None)


def cmd_projects(args: SimpleNamespace) -> int:
    db = get_db()
    try:
        area_id = _area_id_for(db, args.area) if args.area else None
        projects = db.projects(area_id=area_id, status=None if args.all else Status.OPEN)
    except DBError as e:
        return _error(str(e))
    if args.json:
        print(json.dumps([to_json_dict(p) for p in projects], indent=2))
        return 0
    if not projects:
        console.print("[dim]no projects[/dim]")
        return 0
    console.print(projects_table(projects, show_status=args.all))
    return 0


def cmd_areas(args: SimpleNamespace) -> int:
    try:
        areas = get_db().areas()
    except DBError as e:
        return _error(str(e))
    if args.json:
        print(json.dumps([to_json_dict(a) for a in areas], indent=2))
        return 0
    if not areas:
        console.print("[dim]no areas[/dim]")
        return 0
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Area")
    table.add_column("Tags", style="magenta")
    for a in areas:
        name = Text(a.title)
        if not a.visible:
            name.append("  hidden", style="dim")
        table.add_row(a.id[:8], name, ", ".join(a.tags))
    console.print(table)
    return 0


def cmd_tags(args: SimpleNamespace) -> int:
    try:
        tags = get_db().tags()
    except DBError as e:
        return _error(str(e))
    if args.json:
        print(json.dumps([to_json_dict(t) for t in tags], indent=2))
        return 0
    if not tags:
        console.print("[dim]no tags[/dim]")
        return 0
    by_id = {t.id: t.title for t in tags}
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Tag")
    table.add_column("Shortcut", no_wrap=True)
    table.add_column("Parent", style="dim")
    for t in tags:
        table.add_row(t.id[:8], t.title, t.shortcut, by_id.get(t.parent_id or "", ""))
    console.print(table)
    return 0


def cmd_search(args: SimpleNamespace) -> int:
    db = get_db()
    try:
        todos = db.todos(status=None, query=args.query)
        projects = [
            p
            for p in db.projects(status=None)
            if args.query.lower() in p.title.lower()
            or args.query.lower() in p.notes.lower()
        ]
    except DBError as e:
        return _error(str(e))
    if args.json:
        print(
            json.dumps(
                {
                    "todos": [to_json_dict(t) for t in todos],
                    "projects": [to_json_dict(p) for p in projects],
                },
                indent=2,
            )
        )
        return 0
    if not todos and not projects:
        console.print("[dim]no matches[/dim]")
        return 0
    if projects:
        console.print(projects_table(projects, show_status=True))
    if todos:
        console.print(todos_table(todos, show_status=True))
    return 0


# ---------- show ----------


def _detail(label: str, value) -> None:
    if value:
        console.print(f"  [bold]{label}:[/bold] {value}")


def _show_todo(db: ThingsDB, uuid: str, as_json: bool) -> int:
    t = db.todo(uuid)
    if t is None:
        return _error(f"todo {uuid} not found")
    checklist = db.checklist(uuid)
    if as_json:
        d = to_json_dict(t)
        d["checklist"] = [to_json_dict(c) for c in checklist]
        print(json.dumps(d, indent=2))
        return 0
    glyph = _STATUS_GLYPH.get(t.status, Text("?"))
    header = Text.assemble(glyph, " ", (t.title or "(untitled)", "bold"))
    console.print(header)
    console.print(f"  [dim]{t.id}[/dim]")
    _detail("status", t.status.value + (" (trashed)" if t.trashed else ""))
    if t.when:
        label = "today" if t.when <= date.today() else t.when.isoformat()
        _detail("when", label + (" evening" if t.evening else ""))
    elif t.start == StartBucket.SOMEDAY:
        _detail("when", "someday")
    elif t.start == StartBucket.INBOX:
        _detail("when", "inbox")
    _detail("deadline", t.deadline.isoformat() if t.deadline else None)
    _detail("project", f"{t.project_title} [dim]{(t.project_id or '')[:8]}[/dim]" if t.project_id else None)
    _detail("heading", t.heading_title)
    _detail("area", f"{t.area_title} [dim]{(t.area_id or '')[:8]}[/dim]" if t.area_id else None)
    _detail("tags", ", ".join(t.tags))
    _detail("repeating", "yes" if t.repeating else None)
    _detail("created", t.created.strftime("%Y-%m-%d %H:%M") if t.created else None)
    _detail("stopped", t.stopped.strftime("%Y-%m-%d %H:%M") if t.stopped else None)
    if t.notes:
        console.print()
        for line in t.notes.splitlines():
            console.print(f"  {line}")
    if checklist:
        console.print()
        for c in checklist:
            mark = "[green]☑[/green]" if c.completed else "☐"
            style = " dim" if c.completed else ""
            console.print(f"  {mark} [{'default' + style}]{c.title}[/]")
    return 0


def _show_project(db: ThingsDB, uuid: str, as_json: bool) -> int:
    p = db.project(uuid)
    if p is None:
        return _error(f"project {uuid} not found")
    todos = db.todos(status=None, project_id=uuid)
    if as_json:
        d = to_json_dict(p)
        d["todos"] = [to_json_dict(t) for t in todos]
        print(json.dumps(d, indent=2))
        return 0
    glyph = _STATUS_GLYPH.get(p.status, Text("?"))
    console.print(Text.assemble(glyph, " ", (p.title or "(untitled)", "bold")))
    console.print(f"  [dim]{p.id}[/dim]")
    _detail("status", p.status.value + (" (trashed)" if p.trashed else ""))
    _detail("area", p.area_title)
    _detail("deadline", p.deadline.isoformat() if p.deadline else None)
    _detail("tags", ", ".join(p.tags))
    if p.notes:
        console.print()
        for line in p.notes.splitlines():
            console.print(f"  {line}")
    if todos:
        console.print()
        console.print(todos_table(todos, show_status=True))
    return 0


def cmd_show(args: SimpleNamespace) -> int:
    db = get_db()
    try:
        kind, uuid = db.resolve(args.id)
    except DBError as e:
        return _error(str(e))
    if kind == "todo":
        return _show_todo(db, uuid, args.json)
    if kind == "project":
        return _show_project(db, uuid, args.json)
    if kind == "area":
        a = db.area(uuid)
        todos = db.todos(area_id=uuid)
        projects = db.projects(area_id=uuid)
        if args.json:
            d = to_json_dict(a)
            d["projects"] = [to_json_dict(p) for p in projects]
            d["todos"] = [to_json_dict(t) for t in todos]
            print(json.dumps(d, indent=2))
            return 0
        console.print(Text(a.title, style="bold"))
        console.print(f"  [dim]{a.id}[/dim]")
        if projects:
            console.print(projects_table(projects))
        if todos:
            console.print(todos_table(todos))
        return 0
    # tag
    tag = next((t for t in db.tags() if t.id == uuid), None)
    todos = db.todos(tag=tag.title) if tag else []
    if args.json:
        d = to_json_dict(tag)
        d["todos"] = [to_json_dict(t) for t in todos]
        print(json.dumps(d, indent=2))
        return 0
    console.print(Text(tag.title, style="bold magenta"))
    console.print(f"  [dim]{tag.id}[/dim]")
    return _print_todos(todos, SimpleNamespace(json=False))


# ---------- write commands ----------


def _fire(url: str, args: SimpleNamespace) -> bool:
    """Open the URL unless --dry-run. Returns False (with a printed line)
    on dry runs."""
    if args.dry_run:
        console.print(f"[dim]would open:[/dim] {url}")
        return False
    urlscheme.launch(url)
    return True


def cmd_add(args: SimpleNamespace) -> int:
    list_id = None
    if args.project or args.area:
        try:
            list_id = _list_id_for(get_db(), args)
        except DBError as e:
            return _error(str(e))
    url = urlscheme.build_add_url(
        args.title,
        notes=args.notes,
        when=args.when,
        deadline=args.deadline,
        tags=args.tags,
        checklist=_split_multi(args.checklist),
        list_id=list_id,
        heading=args.heading,
        completed=args.completed,
        reveal=args.reveal,
    )
    try:
        started = datetime.now() - timedelta(seconds=5)
        if not _fire(url, args):
            return 0
    except UrlError as e:
        return _error(str(e))
    line = Text.from_markup("[bold green]＋ added[/bold green] ")
    line.append(args.title)
    created = _wait_for_created(args.title, started)
    if created is not None:
        line.append(f"  {created.id}", style="dim")
    console.print(line)
    return 0


def _wait_for_created(title: str, since: datetime) -> Todo | None:
    """Best-effort UUID recovery: the URL scheme reports nothing back, so
    poll the database briefly for the todo we just created."""
    try:
        db = ThingsDB()
    except DBError:
        return None
    for _ in range(12):
        try:
            t = db.find_created_since(title, since)
        except (DBError, Exception):
            return None
        if t is not None:
            return t
        time.sleep(0.25)
    return None


def cmd_add_project(args: SimpleNamespace) -> int:
    area_id = None
    if args.area:
        try:
            area_id = _area_id_for(get_db(), args.area)
        except DBError as e:
            return _error(str(e))
    url = urlscheme.build_add_project_url(
        args.title,
        notes=args.notes,
        when=args.when,
        deadline=args.deadline,
        tags=args.tags,
        area_id=area_id,
        todos=_split_multi(args.todos),
        reveal=args.reveal,
    )
    try:
        if not _fire(url, args):
            return 0
    except UrlError as e:
        return _error(str(e))
    console.print(Text.assemble(("＋ added project ", "bold green"), args.title))
    return 0


def _resolve_todo(args_id: str) -> tuple[ThingsDB, str, Todo | None]:
    db = get_db()
    _, uuid = _resolve_kind(db, args_id, ("todo",))
    return db, uuid, db.todo(uuid)


def cmd_update(args: SimpleNamespace) -> int:
    try:
        db, uuid, todo = _resolve_todo(args.id)
        list_id = _list_id_for(db, args)
        url = urlscheme.build_update_url(
            uuid,
            title=args.title,
            notes=args.notes,
            prepend_notes=args.prepend_notes,
            append_notes=args.append_notes,
            when=args.when,
            deadline=args.deadline,
            tags=args.tags,
            add_tags=args.add_tags,
            checklist=_split_multi(args.checklist),
            append_checklist=_split_multi(args.append_checklist),
            list_id=list_id,
            heading=args.heading,
        )
        if not _fire(url, args):
            return 0
    except (DBError, UrlError) as e:
        return _error(str(e))
    name = args.title or (todo.title if todo else uuid[:8])
    console.print(Text.assemble(("✎ updated ", "bold"), name))
    return 0


def cmd_update_project(args: SimpleNamespace) -> int:
    try:
        db = get_db()
        _, uuid = _resolve_kind(db, args.id, ("project",))
        proj = db.project(uuid)
        area_id = _area_id_for(db, args.area) if args.area else None
        url = urlscheme.build_update_url(
            uuid,
            project=True,
            title=args.title,
            notes=args.notes,
            when=args.when,
            deadline=args.deadline,
            tags=args.tags,
            add_tags=args.add_tags,
            area_id=area_id,
        )
        if not _fire(url, args):
            return 0
    except (DBError, UrlError) as e:
        return _error(str(e))
    name = args.title or (proj.title if proj else uuid[:8])
    console.print(Text.assemble(("✎ updated project ", "bold"), name))
    return 0


def _status_change(args: SimpleNamespace, *, completed: bool | None, canceled: bool | None, head: str, style: str) -> int:
    try:
        db = get_db()
        kind, uuid = _resolve_kind(db, args.id, ("todo", "project"))
        item = db.todo(uuid) if kind == "todo" else db.project(uuid)
        url = urlscheme.build_update_url(
            uuid, project=kind == "project", completed=completed, canceled=canceled
        )
        if not _fire(url, args):
            return 0
    except (DBError, UrlError) as e:
        return _error(str(e))
    name = item.title if item else uuid[:8]
    console.print(Text.assemble((head + " ", style), name))
    return 0


def cmd_complete(args: SimpleNamespace) -> int:
    return _status_change(args, completed=True, canceled=None, head="✓ completed", style="bold green")


def cmd_cancel(args: SimpleNamespace) -> int:
    return _status_change(args, completed=None, canceled=True, head="✗ canceled", style="bold red")


def cmd_reopen(args: SimpleNamespace) -> int:
    return _status_change(args, completed=False, canceled=None, head="↺ reopened", style="bold")


def cmd_delete(args: SimpleNamespace) -> int:
    try:
        db = get_db()
        kind, uuid = _resolve_kind(db, args.id, ("todo", "project", "area", "tag"))
        item = db.todo(uuid) if kind == "todo" else db.project(uuid) if kind == "project" else None
        name = item.title if item else uuid[:8]
    except DBError as e:
        return _error(str(e))
    preview = args.dry_run or not args.yes
    if preview:
        console.print(Text.assemble(("would delete ", "dim"), f"{kind} ", name))
        if not args.dry_run:
            console.print("[dim]pass --yes to actually delete[/dim]")
        return 0
    try:
        applescript.delete(kind, uuid)
    except applescript.ScriptError as e:
        return _error(str(e))
    tail = " → Trash" if kind in ("todo", "project") else ""
    console.print(Text.assemble(("🗑 deleted ", "bold red"), f"{kind} ", name, (tail, "dim")))
    return 0


def cmd_empty_trash(args: SimpleNamespace) -> int:
    if not args.yes:
        console.print("[dim]would empty the Things Trash — pass --yes to confirm[/dim]")
        return 0
    try:
        applescript.empty_trash()
    except applescript.ScriptError as e:
        return _error(str(e))
    console.print("[bold red]🗑 emptied trash[/bold red]")
    return 0


def cmd_open(args: SimpleNamespace) -> int:
    try:
        _, uuid = get_db().resolve(args.id)
        urlscheme.launch(urlscheme.build_show_url(uuid), foreground=True)
    except (DBError, UrlError) as e:
        return _error(str(e))
    return 0


# ---------- auth / doctor ----------


def cmd_auth(args: SimpleNamespace) -> int:
    tok = urlscheme.auth_token()
    if tok:
        console.print(f"[bold green]✓[/bold green] {urlscheme.ENV_TOKEN} is set [dim](…{tok[-4:]})[/dim]")
        return 0
    console.print(f"[bold red]✗[/bold red] {urlscheme.ENV_TOKEN} is not set")
    console.print()
    console.print("Updates via the Things URL scheme need Things' auth token:")
    console.print("  1. Open Things → Settings → General → Enable Things URLs")
    console.print("  2. Manage → copy the token")
    console.print(f"  3. Add to ~/.envrc:  export {urlscheme.ENV_TOKEN}=<token>")
    console.print("  4. direnv allow ~")
    return 1


def cmd_doctor(args: SimpleNamespace) -> int:
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "[green]✓[/green]" if passed else "[red]✗[/red]"
        console.print(f"  {mark} {label}")
        if detail:
            console.print(f"      [dim]{detail}[/dim]")
        if not passed:
            ok = False

    console.print("[bold]things doctor[/bold]")
    console.print()
    console.print("[bold underline]App[/bold underline]")
    app = Path("/Applications/Things3.app")
    check("Things 3 installed", app.exists(), str(app) if app.exists() else "not found in /Applications")
    check("`open` available", shutil.which("open") is not None)
    check("`osascript` available", shutil.which("osascript") is not None, "needed only for delete/empty-trash")

    console.print()
    console.print("[bold underline]Database (reads)[/bold underline]")
    try:
        db = ThingsDB()
        counts = db.counts()
        check(
            "database readable",
            True,
            f"{db.path}  ({counts['open_todos']} open todos, {counts['projects']} projects)",
        )
    except (DBError, Exception) as e:
        check("database readable", False, str(e))

    console.print()
    console.print("[bold underline]Auth (updates)[/bold underline]")
    tok = urlscheme.auth_token()
    check(
        f"{urlscheme.ENV_TOKEN} set",
        tok is not None,
        f"…{tok[-4:]}" if tok else "needed for update/complete/cancel — run `things auth`",
    )

    console.print()
    if ok:
        console.print("status: [bold green]OK[/bold green]")
    else:
        console.print("status: [bold red]FAIL[/bold red] [dim]— fix the items above[/dim]")
    return 0 if ok else 1


# ---------- command registry + hand-rolled parsing (rich-rendered help) ----------

PROG = "things"
DESCRIPTION = (
    "A self-contained interface to Things 3 — reads from the app's database, "
    "writes through the Things URL scheme."
)

_JSON = Flag("--json", False, "emit JSON (uncolored)")
_DRY = Flag("--dry-run", False, "print the Things URL without opening it")

_WHEN_HELP = "today / tomorrow / evening / anytime / someday / yyyy-mm-dd"

COMMANDS: list[Command] = [
    Command("inbox", "List Inbox todos", _list_cmd(lambda db: db.inbox()), [_JSON]),
    Command("today", "List Today (incl. overdue deadlines)", _list_cmd(lambda db: db.today()), [_JSON]),
    Command("upcoming", "List Upcoming (scheduled ahead)", _list_cmd(lambda db: db.upcoming()), [_JSON]),
    Command("anytime", "List Anytime", _list_cmd(lambda db: db.anytime()), [_JSON]),
    Command("someday", "List Someday", _list_cmd(lambda db: db.someday()), [_JSON]),
    Command(
        "logbook",
        "List completed/canceled todos, newest first",
        cmd_logbook,
        [Flag("--limit", True, "max rows (default 25)", "N"), _JSON],
    ),
    Command("trash", "List trashed todos", _list_cmd(lambda db: db.trash(), show_status=True), [_JSON]),
    Command("deadlines", "List open todos with deadlines", _list_cmd(lambda db: db.deadlines()), [_JSON]),
    Command(
        "todos",
        "List todos with filters",
        cmd_todos,
        [
            Flag("--project", True, "filter by project title or id", "REF"),
            Flag("--area", True, "filter by area title or id", "REF"),
            Flag("--tag", True, "filter by tag name", "NAME"),
            Flag("--status", True, "open (default) / completed / canceled / all", "S"),
            Flag("--search", True, "title/notes substring", "TEXT"),
            _JSON,
        ],
    ),
    Command(
        "projects",
        "List projects",
        cmd_projects,
        [
            Flag("--area", True, "filter by area title or id", "REF"),
            Flag("--all", False, "include completed/canceled"),
            _JSON,
        ],
    ),
    Command("areas", "List areas", cmd_areas, [_JSON]),
    Command("tags", "List tags", cmd_tags, [_JSON]),
    Command(
        "show",
        "Show a todo/project/area/tag in detail",
        cmd_show,
        [_JSON],
        [Arg("id", "UUID or unique prefix (from any list's ID column)")],
    ),
    Command(
        "search",
        "Search todos and projects by title/notes",
        cmd_search,
        [_JSON],
        [Arg("query", "substring to search for", variadic=True)],
    ),
    Command(
        "add",
        "Add a todo",
        cmd_add,
        [
            Flag("--notes", True, "notes text", "TEXT"),
            Flag("--when", True, _WHEN_HELP, "WHEN"),
            Flag("--deadline", True, "yyyy-mm-dd", "DATE"),
            Flag("--tags", True, "comma-separated tag names", "TAGS"),
            Flag("--checklist", True, "comma-separated checklist items", "ITEMS"),
            Flag("--project", True, "file into project (title or id)", "REF"),
            Flag("--area", True, "file into area (title or id)", "REF"),
            Flag("--heading", True, "heading title within the project", "NAME"),
            Flag("--completed", False, "log it as already completed"),
            Flag("--reveal", False, "show the new todo in Things"),
            _DRY,
        ],
        [Arg("title", "todo title", variadic=True)],
    ),
    Command(
        "add-project",
        "Add a project",
        cmd_add_project,
        [
            Flag("--notes", True, "notes text", "TEXT"),
            Flag("--when", True, _WHEN_HELP, "WHEN"),
            Flag("--deadline", True, "yyyy-mm-dd", "DATE"),
            Flag("--tags", True, "comma-separated tag names", "TAGS"),
            Flag("--area", True, "file into area (title or id)", "REF"),
            Flag("--todos", True, "comma-separated initial todos", "ITEMS"),
            Flag("--reveal", False, "show the new project in Things"),
            _DRY,
        ],
        [Arg("title", "project title", variadic=True)],
    ),
    Command(
        "update",
        "Update a todo (needs auth token; empty --when= / --deadline= clears)",
        cmd_update,
        [
            Flag("--title", True, "new title", "TEXT"),
            Flag("--notes", True, "replace notes (--notes= clears)", "TEXT"),
            Flag("--prepend-notes", True, "prepend to notes", "TEXT"),
            Flag("--append-notes", True, "append to notes", "TEXT"),
            Flag("--when", True, _WHEN_HELP + " (--when= clears)", "WHEN"),
            Flag("--deadline", True, "yyyy-mm-dd (--deadline= clears)", "DATE"),
            Flag("--tags", True, "replace tags (--tags= clears)", "TAGS"),
            Flag("--add-tags", True, "add tags, keeping existing", "TAGS"),
            Flag("--checklist", True, "replace checklist (comma-separated)", "ITEMS"),
            Flag("--append-checklist", True, "append checklist items", "ITEMS"),
            Flag("--project", True, "move into project (title or id)", "REF"),
            Flag("--area", True, "move into area (title or id)", "REF"),
            Flag("--heading", True, "move under heading (title)", "NAME"),
            _DRY,
        ],
        [Arg("id", "todo UUID or unique prefix")],
    ),
    Command(
        "update-project",
        "Update a project (needs auth token)",
        cmd_update_project,
        [
            Flag("--title", True, "new title", "TEXT"),
            Flag("--notes", True, "replace notes", "TEXT"),
            Flag("--when", True, _WHEN_HELP, "WHEN"),
            Flag("--deadline", True, "yyyy-mm-dd (--deadline= clears)", "DATE"),
            Flag("--tags", True, "replace tags", "TAGS"),
            Flag("--add-tags", True, "add tags, keeping existing", "TAGS"),
            Flag("--area", True, "move into area (title or id)", "REF"),
            _DRY,
        ],
        [Arg("id", "project UUID or unique prefix")],
    ),
    Command("complete", "Mark a todo/project completed", cmd_complete, [_DRY], [Arg("id", "UUID or unique prefix")]),
    Command("cancel", "Mark a todo/project canceled", cmd_cancel, [_DRY], [Arg("id", "UUID or unique prefix")]),
    Command("reopen", "Reopen a completed/canceled todo/project", cmd_reopen, [_DRY], [Arg("id", "UUID or unique prefix")]),
    Command(
        "delete",
        "Delete a todo/project/area/tag (todos/projects → Trash)",
        cmd_delete,
        [
            Flag("--yes", False, "confirm — required to actually delete"),
            Flag("--dry-run", False, "preview only (same as omitting --yes)"),
        ],
        [Arg("id", "UUID or unique prefix")],
    ),
    Command(
        "empty-trash",
        "Purge everything in the Things Trash",
        cmd_empty_trash,
        [Flag("--yes", False, "confirm — required to actually purge")],
    ),
    Command("open", "Reveal an item in the Things app", cmd_open, [], [Arg("id", "UUID or unique prefix")]),
    Command("auth", "Check the URL-scheme auth token + setup steps", cmd_auth),
    Command("doctor", "Verify app, database access, and auth token", cmd_doctor),
]
COMMANDS_BY_NAME = {c.name: c for c in COMMANDS}


def _flag_label(f: Flag) -> str:
    return f"{f.name} {f.metavar}".strip() if f.takes_value else f.name


def _arg_token(a: Arg) -> str:
    tok = f"{a.name}…" if a.variadic else a.name
    return f"<{tok}>" if a.required else f"[{tok}]"


def _usage(cmd: Command) -> str:
    # markup-string context: escape the leading '[' of optional tokens
    parts = [_arg_token(a).replace("[", "\\[") for a in cmd.args]
    parts += [f"\\[{_flag_label(f)}]" for f in cmd.flags]
    return " ".join(parts)


def print_help() -> None:
    console.print(f"[bold]{PROG}[/bold] — Things 3, from the terminal")
    console.print(f"[dim]{DESCRIPTION}[/dim]")
    console.print()
    console.print(f"[bold]Usage:[/bold] {PROG} [cyan]<command>[/cyan] [dim]\\[args/options][/dim]")
    console.print()
    table = Table(box=box.SIMPLE, show_header=False, expand=False, pad_edge=False)
    table.add_column("Command", style="bold cyan", no_wrap=True)
    table.add_column("Description")
    for c in COMMANDS:
        table.add_row(c.name, c.help)
    console.print(table)
    console.print(f"Run [bold]{PROG} <command> -h[/bold] for command options.")


def print_command_help(cmd: Command) -> None:
    console.print(
        f"[bold]Usage:[/bold] {PROG} [cyan]{cmd.name}[/cyan] [dim]{_usage(cmd)}[/dim]".rstrip()
    )
    console.print(f"[dim]{cmd.help}[/dim]")
    rows = [(_arg_token(a), a.help) for a in cmd.args]
    rows += [(_flag_label(f), f.help) for f in cmd.flags]
    if rows:
        console.print()
        table = Table(box=box.SIMPLE, show_header=False, expand=False, pad_edge=False)
        table.add_column("", style="bold", no_wrap=True)
        table.add_column("Help")
        for label, helptext in rows:
            table.add_row(Text(label), helptext)  # Text: no markup parsing on labels
        console.print(table)


def parse_command(cmd: Command, argv: list[str]) -> SimpleNamespace:
    """Parse a command's positionals + flags into a namespace. Raises CliError
    on bad input; raises SystemExit(0) after printing help for -h/--help."""
    ns = SimpleNamespace(**{f.attr: (None if f.takes_value else False) for f in cmd.flags})
    for a in cmd.args:
        setattr(ns, a.name, None)
    by_name = {f.name: f for f in cmd.flags}
    positionals: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-h", "--help"):
            print_command_help(cmd)
            raise SystemExit(0)
        if tok.startswith("--"):
            name, eq, inline = tok.partition("=")
            f = by_name.get(name)
            if f is None:
                raise CliError(f"unknown option {tok!r} for `{cmd.name}`")
            if f.takes_value:
                if eq:
                    value = inline
                else:
                    i += 1
                    if i >= len(argv):
                        raise CliError(f"{name} requires a value")
                    value = argv[i]
                setattr(ns, f.attr, value)
            else:
                if eq:
                    raise CliError(f"{name} takes no value")
                setattr(ns, f.attr, True)
        else:
            positionals.append(tok)
        i += 1
    # map positionals onto declared args (last arg may be variadic)
    for idx, a in enumerate(cmd.args):
        if a.variadic:
            rest = positionals[idx:]
            setattr(ns, a.name, " ".join(rest) if rest else None)
            break
        if idx < len(positionals):
            setattr(ns, a.name, positionals[idx])
    else:
        extra = positionals[len(cmd.args):]
        if extra:
            raise CliError(f"unexpected argument {extra[0]!r} for `{cmd.name}`")
    for a in cmd.args:
        if a.required and not getattr(ns, a.name):
            raise CliError(f"missing argument <{a.name}> for `{cmd.name}`")
    return ns


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print_help()
        return 0
    name, rest = argv[0], argv[1:]
    cmd = COMMANDS_BY_NAME.get(name)
    if cmd is None:
        err_console.print(f"[bold red]error:[/bold red] unknown command {name!r}")
        console.print()
        print_help()
        return 2
    try:
        ns = parse_command(cmd, rest)
    except CliError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 2
    return cmd.func(ns)


if __name__ == "__main__":
    sys.exit(main())
