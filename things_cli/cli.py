"""`things` CLI — a self-contained interface to Things 3 via Things Cloud.

Reads and writes talk straight to Cultured Code's sync backend, so nothing
here needs the Things app installed or running — it works on any machine
with the account credentials.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace
from typing import Callable

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from things_cli import api, sync
from things_cli.api import CloudError
from things_cli.models import Project, StartBucket, Status, Todo, to_json_dict
from things_cli.sync import UNSET, State, SyncError

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


def cloud_state() -> tuple[State, api.Client]:
    """Pull current cloud state (the single source of truth), or exit with
    a clear error."""
    try:
        return sync.fetch_state()
    except (CloudError, SyncError) as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        raise SystemExit(1)


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


def _print_todos(
    todos: list[Todo], args: SimpleNamespace, *, show_status: bool = False
) -> int:
    if getattr(args, "json", False):
        print(json.dumps([to_json_dict(t) for t in todos], indent=2))
        return 0
    if not todos:
        console.print("[dim]no todos[/dim]")
        return 0
    console.print(todos_table(todos, show_status=show_status))
    return 0


# ---------- read commands ----------


def cmd_status(args: SimpleNamespace) -> int:
    state, _client = cloud_state()
    open_todos = [t for t in state.todos.values() if not t.trashed and t.status == Status.OPEN]
    console.print("[bold]source:[/bold]   [dim]cloud.culturedcode.com[/dim]")
    console.print(f"[bold]head:[/bold]     [dim]index {state.head_index}[/dim]")
    console.print(
        f"[bold]open:[/bold]     {len(open_todos)} todos "
        f"[dim]({len(state.todos)} total)[/dim]"
    )
    console.print(
        f"[bold]projects:[/bold] {len(state.open_projects())} open "
        f"[dim]({len(state.projects)} total)[/dim]"
    )
    console.print(
        f"[bold]areas:[/bold]    {len(state.areas)}   "
        f"[bold]tags:[/bold] {len(state.tags)}"
    )
    today_items = state.today_list()
    inbox_items = state.inbox()
    line = Text("today:    ", style="bold")
    line.append(f"{len(today_items)} scheduled", style="yellow" if today_items else "dim")
    line.append(f"   inbox: {len(inbox_items)}", style="dim")
    console.print(line)
    return 0


def _list_cmd(fetch: Callable[[State], list[Todo]], *, show_status: bool = False):
    def run(args: SimpleNamespace) -> int:
        state, _client = cloud_state()
        return _print_todos(fetch(state), args, show_status=show_status)

    return run


def cmd_logbook(args: SimpleNamespace) -> int:
    state, _client = cloud_state()
    try:
        limit = int(args.limit) if args.limit else 25
    except ValueError:
        err_console.print("[bold red]error:[/bold red] --limit must be an integer")
        return 2
    return _print_todos(state.logbook(limit=limit), args, show_status=True)


def cmd_todos(args: SimpleNamespace) -> int:
    state, _client = cloud_state()
    try:
        todos = list(state.todos.values())
        if args.project:
            todos = state.todos_in_project(
                state.require(args.project, "project"), open_only=not args.all
            )
        elif args.area:
            todos = state.todos_in_area(
                state.require(args.area, "area"), open_only=not args.all
            )
        elif args.tag:
            todos = state.todos_with_tag(args.tag)
        else:
            todos = [t for t in state.todos.values() if not t.trashed]
            if not args.all:
                todos = [t for t in todos if t.status == Status.OPEN]
            todos.sort(key=lambda t: t.index)
        if args.search:
            q = args.search.lower()
            todos = [t for t in todos if q in t.title.lower() or q in t.notes.lower()]
    except SyncError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    return _print_todos(todos, args, show_status=args.all)


def cmd_projects(args: SimpleNamespace) -> int:
    state, _client = cloud_state()
    try:
        projects = state.all_projects() if args.all else state.open_projects()
        if args.area:
            area_id = state.require(args.area, "area")
            projects = [p for p in projects if p.area_id == area_id]
    except SyncError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    if args.json:
        print(json.dumps([to_json_dict(p) for p in projects], indent=2))
        return 0
    if not projects:
        console.print("[dim]no projects[/dim]")
        return 0
    console.print(projects_table(projects, show_status=args.all))
    return 0


def cmd_areas(args: SimpleNamespace) -> int:
    state, _client = cloud_state()
    areas = sorted(state.areas.values(), key=lambda a: a.index)
    if args.json:
        print(json.dumps([to_json_dict(a) for a in areas], indent=2))
        return 0
    if not areas:
        console.print("[dim]no areas[/dim]")
        return 0
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Area")
    table.add_column("Projects", justify="right", no_wrap=True)
    table.add_column("Open", justify="right", no_wrap=True)
    for a in areas:
        projects = [p for p in state.open_projects() if p.area_id == a.id]
        todos = state.todos_in_area(a.id)
        table.add_row(
            a.id[:8],
            Text(a.title),
            str(len(projects)) if projects else "",
            str(len(todos)) if todos else "",
        )
    console.print(table)
    return 0


def cmd_tags(args: SimpleNamespace) -> int:
    state, _client = cloud_state()
    tags = sorted(state.tags.values(), key=lambda t: t.index)
    if args.json:
        print(json.dumps([to_json_dict(t) for t in tags], indent=2))
        return 0
    if not tags:
        console.print("[dim]no tags[/dim]")
        return 0
    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Tag")
    table.add_column("Shortcut", no_wrap=True)
    table.add_column("Parent", style="dim")
    table.add_column("Used", justify="right", no_wrap=True)
    for t in tags:
        used = len(state.todos_with_tag(t.title))
        table.add_row(
            t.id[:8],
            Text(t.title, style="magenta"),
            t.shortcut,
            state.tags[t.parent_id].title if t.parent_id in state.tags else "",
            str(used) if used else "",
        )
    console.print(table)
    return 0


def cmd_search(args: SimpleNamespace) -> int:
    state, _client = cloud_state()
    todos, projects = state.search(args.query)
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


def _show_todo(state: State, uuid: str, as_json: bool) -> int:
    t = state.todos[uuid]
    checklist = state.checklists.get(uuid, [])
    if as_json:
        d = to_json_dict(t)
        d["checklist"] = [to_json_dict(c) for c in checklist]
        print(json.dumps(d, indent=2))
        return 0
    console.print(
        Text.assemble(_STATUS_GLYPH.get(t.status, Text("?")), " ", (t.title or "(untitled)", "bold"))
    )
    console.print(f"  [dim]{t.id}[/dim]")
    _detail("status", t.status.value + (" (trashed)" if t.trashed else ""))
    if t.when:
        label = "today" if t.when <= date.today() else t.when.isoformat()
        _detail("when", label + (" evening" if t.evening else ""))
    elif t.start == StartBucket.SOMEDAY:
        _detail("when", "someday")
    elif t.start == StartBucket.INBOX:
        _detail("when", "inbox")
    else:
        _detail("when", "anytime")
    _detail("deadline", t.deadline.isoformat() if t.deadline else None)
    _detail("project", t.project_title)
    _detail("heading", t.heading_title)
    _detail("area", t.area_title)
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
            style = "dim" if c.completed else ""
            console.print(f"  {mark} [{style or 'default'}]{c.title}[/]")
    return 0


def _show_project(state: State, uuid: str, as_json: bool) -> int:
    p = state.projects[uuid]
    todos = state.todos_in_project(uuid)
    headings = state.project_headings(uuid)
    if as_json:
        d = to_json_dict(p)
        d["headings"] = [to_json_dict(h) for h in headings]
        d["todos"] = [to_json_dict(t) for t in todos]
        print(json.dumps(d, indent=2))
        return 0
    console.print(
        Text.assemble(_STATUS_GLYPH.get(p.status, Text("?")), " ", (p.title or "(untitled)", "bold"))
    )
    console.print(f"  [dim]{p.id}[/dim]")
    _detail("status", p.status.value + (" (trashed)" if p.trashed else ""))
    _detail("area", p.area_title)
    _detail("deadline", p.deadline.isoformat() if p.deadline else None)
    _detail("tags", ", ".join(p.tags))
    if p.notes:
        console.print()
        for line in p.notes.splitlines():
            console.print(f"  {line}")
    if headings:
        console.print()
        for h in headings:
            under = [t for t in todos if t.heading_id == h.id]
            console.print(f"  [bold]{h.title}[/bold] [dim]{h.id[:8]}[/dim]")
            if under:
                console.print(todos_table(under, show_status=True))
        loose = [t for t in todos if not t.heading_id]
        if loose:
            console.print(todos_table(loose, show_status=True))
    elif todos:
        console.print()
        console.print(todos_table(todos, show_status=True))
    return 0


def cmd_show(args: SimpleNamespace) -> int:
    state, _client = cloud_state()
    try:
        kind, uuid = state.resolve(args.id)
    except SyncError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    if kind == "todo":
        return _show_todo(state, uuid, args.json)
    if kind == "project":
        return _show_project(state, uuid, args.json)
    if kind == "area":
        area = state.areas[uuid]
        projects = [p for p in state.open_projects() if p.area_id == uuid]
        todos = state.todos_in_area(uuid)
        if args.json:
            d = to_json_dict(area)
            d["projects"] = [to_json_dict(p) for p in projects]
            d["todos"] = [to_json_dict(t) for t in todos]
            print(json.dumps(d, indent=2))
            return 0
        console.print(Text(area.title, style="bold"))
        console.print(f"  [dim]{area.id}[/dim]")
        if projects:
            console.print(projects_table(projects))
        if todos:
            console.print(todos_table(todos))
        return 0
    if kind == "tag":
        tag = state.tags[uuid]
        todos = state.todos_with_tag(tag.title)
        if args.json:
            d = to_json_dict(tag)
            d["todos"] = [to_json_dict(t) for t in todos]
            print(json.dumps(d, indent=2))
            return 0
        console.print(Text(tag.title, style="bold magenta"))
        console.print(f"  [dim]{tag.id}[/dim]")
        return _print_todos(todos, SimpleNamespace(json=False))
    # heading
    h = state.headings[uuid]
    todos = [t for t in state.todos.values() if t.heading_id == uuid and not t.trashed]
    if args.json:
        d = to_json_dict(h)
        d["todos"] = [to_json_dict(t) for t in todos]
        print(json.dumps(d, indent=2))
        return 0
    console.print(Text(h.title, style="bold"))
    console.print(f"  [dim]{h.id}[/dim]  in {h.project_title or '?'}")
    return _print_todos(sorted(todos, key=lambda t: t.index), SimpleNamespace(json=False))


# ---------- write commands ----------


def _commit(
    state: State, client: api.Client, changes: dict, args: SimpleNamespace
) -> bool:
    """Commit unless --dry-run. Returns False when nothing was sent."""
    if args.dry_run:
        console.print("[dim]would commit:[/dim]")
        console.print_json(json.dumps(changes, default=str))
        return False
    sync.commit(client, state, changes)
    return True


def _tag_ids(state: State, raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return state.tag_ids_for(p.strip() for p in raw.split(",") if p.strip())


def _checklist(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


def _container(state: State, args: SimpleNamespace) -> tuple[str | None, str | None, str | None]:
    """Resolve --project / --area / --heading into ids."""
    project_id = state.require(args.project, "project") if args.project else None
    area_id = state.require(args.area, "area") if args.area else None
    heading_id = state.require(args.heading, "heading") if args.heading else None
    if heading_id and not project_id:
        # A heading implies its project.
        project_id = state.headings[heading_id].project_id
    return project_id, area_id, heading_id


def cmd_add(args: SimpleNamespace) -> int:
    state, client = cloud_state()
    try:
        project_id, area_id, heading_id = _container(state, args)
        uuid, changes = sync.build_create_todo(
            args.title,
            notes=args.notes,
            when=args.when,
            deadline=args.deadline,
            tag_ids=_tag_ids(state, args.tags),
            project_id=project_id,
            area_id=area_id,
            heading_id=heading_id,
            checklist=_checklist(args.checklist),
            evening=args.evening,
            index=state.next_todo_index(
                project_id=project_id, area_id=area_id, heading_id=heading_id
            ),
            today_index=state.next_today_index(),
        )
        if not _commit(state, client, changes, args):
            return 0
    except (SyncError, CloudError) as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    line = Text.from_markup("[bold green]＋ added[/bold green] ")
    line.append(args.title)
    line.append(f"  {uuid[:8]}", style="dim")
    console.print(line)
    return 0


def cmd_add_project(args: SimpleNamespace) -> int:
    state, client = cloud_state()
    try:
        area_id = state.require(args.area, "area") if args.area else None
        uuid, changes = sync.build_create_project(
            args.title,
            notes=args.notes,
            when=args.when,
            deadline=args.deadline,
            tag_ids=_tag_ids(state, args.tags),
            area_id=area_id,
            index=state.next_project_index(area_id),
        )
        if not _commit(state, client, changes, args):
            return 0
    except (SyncError, CloudError) as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    line = Text.from_markup("[bold green]＋ added project[/bold green] ")
    line.append(args.title)
    line.append(f"  {uuid[:8]}", style="dim")
    console.print(line)
    return 0


def cmd_add_area(args: SimpleNamespace) -> int:
    state, client = cloud_state()
    try:
        uuid, changes = sync.build_create_area(
            args.title, index=state.next_area_index()
        )
        if not _commit(state, client, changes, args):
            return 0
    except (SyncError, CloudError) as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    line = Text.from_markup("[bold green]＋ added area[/bold green] ")
    line.append(args.title)
    line.append(f"  {uuid[:8]}", style="dim")
    console.print(line)
    return 0


def cmd_edit(args: SimpleNamespace) -> int:
    """Only the fields you pass change; `--when=` / `--deadline=` clear."""
    state, client = cloud_state()
    try:
        kind, uuid = state.resolve(args.id)
        if kind not in ("todo", "project"):
            raise SyncError(f"{args.id!r} is a {kind}; edit takes a todo or project")
        project_id, area_id, heading_id = _container(state, args)
        changes = sync.build_update(
            uuid,
            state.entity_of(uuid),
            title=args.title if args.title is not None else UNSET,
            notes=args.notes if args.notes is not None else UNSET,
            when=(args.when or None) if args.when is not None else UNSET,
            deadline=(args.deadline or None) if args.deadline is not None else UNSET,
            tag_ids=_tag_ids(state, args.tags) if args.tags is not None else UNSET,
            project_id=project_id if args.project is not None else UNSET,
            area_id=area_id if args.area is not None else UNSET,
            heading_id=heading_id if args.heading is not None else UNSET,
            evening=True if args.evening else UNSET,
        )
        if len(changes[uuid]["p"]) <= 1:  # only the md stamp
            err_console.print(
                "[bold red]error:[/bold red] nothing to change "
                "(pass --title/--notes/--when/--deadline/--tags/--project/--area)"
            )
            return 2
        name = args.title or state.title_of(uuid)
        if not _commit(state, client, changes, args):
            return 0
    except (SyncError, CloudError) as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    console.print(Text.assemble(("✎ edited ", "bold"), name))
    return 0


def _status_cmd(status: Status, head: str, style: str):
    def run(args: SimpleNamespace) -> int:
        state, client = cloud_state()
        try:
            kind, uuid = state.resolve(args.id)
            if kind not in ("todo", "project"):
                raise SyncError(f"{args.id!r} is a {kind}; expected a todo or project")
            name = state.title_of(uuid)
            changes = sync.build_update(uuid, state.entity_of(uuid), status=status)
            if not _commit(state, client, changes, args):
                return 0
        except (SyncError, CloudError) as e:
            err_console.print(f"[bold red]error:[/bold red] {e}")
            return 1
        console.print(Text.assemble((head + " ", style), name))
        return 0

    return run


def cmd_delete(args: SimpleNamespace) -> int:
    state, client = cloud_state()
    try:
        kind, uuid = state.resolve(args.id)
        name = state.title_of(uuid)
    except SyncError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    # Only todos and projects carry a `tr` flag, so only they can be
    # trashed; areas and tags have no Trash in Things and must go outright.
    trashable = kind in ("todo", "project")
    permanent = args.permanent or not trashable

    preview = args.dry_run or not args.yes
    if preview and not args.dry_run:
        verb = "permanently delete" if permanent else "move to Trash"
        console.print(Text.assemble((f"would {verb} ", "dim"), f"{kind} ", name))
        console.print("[dim]pass --yes to confirm[/dim]")
        return 0

    targets = [(uuid, state.entity_of(uuid))]
    try:
        if permanent:
            # Nothing cascades server-side, so a todo's checklist items must
            # go in the same commit or they are stranded forever.
            if kind == "todo":
                targets += [
                    (c.id, state.entity_of(c.id))
                    for c in state.checklists.get(uuid, [])
                ]
            changes = sync.build_delete_many(targets)
        else:
            changes = sync.build_update(uuid, state.entity_of(uuid), trashed=True)
        if not _commit(state, client, changes, args):
            return 0
    except (SyncError, CloudError) as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 1

    if permanent:
        line = Text.assemble(("🗑 deleted ", "bold red"), f"{kind} ", name)
        if len(targets) > 1:
            line.append(f"  (+{len(targets) - 1} checklist items)", style="dim")
        if not trashable and not args.permanent:
            line.append("  (this type has no Trash)", style="dim")
    else:
        line = Text.assemble(("🗑 trashed ", "bold red"), f"{kind} ", name)
        line.append("  (recoverable in Things)", style="dim")
    console.print(line)
    return 0


# ---------- auth / doctor ----------


def cmd_auth(args: SimpleNamespace) -> int:
    """Fetch and save the account's history key to ~/.config/things-cli."""
    creds = api.env_credentials()
    if creds is None:
        err_console.print(
            f"[bold red]error:[/bold red] set {api.ENV_EMAIL} and "
            f"{api.ENV_PASSWORD} (e.g. in ~/.envrc, then `direnv allow ~`)"
        )
        return 1
    email, password = creds
    try:
        key = api.fetch_history_key(email, password)
    except CloudError as e:
        err_console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    path = api.save_session(api.Session(email=email, history_key=key))
    sync.clear_cache()
    console.print(f"[bold green]✓ saved session[/bold green] [dim]for {email}[/dim]")
    console.print(f"  [dim]→ {path}[/dim]")
    console.print(f"  [dim]history[/dim] {key[:8]}…")
    return 0


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
    console.print("[bold underline]Auth[/bold underline]")
    session = api.load_session()
    env = api.env_credentials()
    if session is not None:
        src = f"session file ({api.SESSION_PATH})"
    elif env is not None:
        src = f"env ({api.ENV_EMAIL} + {api.ENV_PASSWORD})"
    else:
        src = ""
    check(
        "credentials available",
        session is not None or env is not None,
        f"from {src}" if src else
        f"set {api.ENV_EMAIL}/{api.ENV_PASSWORD} in ~/.envrc, then run `things auth`",
    )

    console.print()
    console.print("[bold underline]Cloud[/bold underline]")
    if session is None and env is None:
        console.print("  [yellow]⊘[/yellow] [dim]skipped — set up auth first[/dim]")
    else:
        try:
            client = api.Client.connect()
            status = client.history_status()
            check(
                "cloud.culturedcode.com reachable",
                True,
                f"latest-server-index={status.get('latest-server-index')}",
            )
            state, _ = sync.fetch_state(client)
            check(
                "history readable",
                True,
                f"{len(state.todos)} todos, {len(state.projects)} projects, "
                f"{len(state.areas)} areas, {len(state.tags)} tags",
            )
        except (CloudError, SyncError) as e:
            check("cloud.culturedcode.com reachable", False, str(e))

    console.print()
    console.print("[bold underline]Cache[/bold underline]")
    console.print(f"  [dim]{sync.CACHE_PATH}[/dim]")

    console.print()
    if ok:
        console.print("status: [bold green]OK[/bold green]")
    else:
        console.print("status: [bold red]FAIL[/bold red] [dim]— fix the items above[/dim]")
    return 0 if ok else 1


def cmd_refresh(args: SimpleNamespace) -> int:
    """Drop the local cache so the next command re-folds the whole history."""
    sync.clear_cache()
    console.print("[dim]cache cleared — next command does a full pull[/dim]")
    return 0


# ---------- command registry + hand-rolled parsing (rich-rendered help) ----------

PROG = "things"
DESCRIPTION = (
    "A self-contained interface to Things 3 — read and manage your tasks "
    "via the Things Cloud backend (no app required)."
)

_JSON = Flag("--json", False, "emit JSON (uncolored)")
_DRY = Flag("--dry-run", False, "show the change without sending it")
_WHEN = "today / evening / anytime / someday / tomorrow / yyyy-mm-dd"

COMMANDS: list[Command] = [
    Command("status", "Summary of the account + today's load", cmd_status),
    Command("inbox", "List Inbox todos", _list_cmd(lambda s: s.inbox()), [_JSON]),
    Command("today", "List Today", _list_cmd(lambda s: s.today_list()), [_JSON]),
    Command("upcoming", "List Upcoming (scheduled ahead)", _list_cmd(lambda s: s.upcoming()), [_JSON]),
    Command("anytime", "List Anytime", _list_cmd(lambda s: s.anytime()), [_JSON]),
    Command("someday", "List Someday", _list_cmd(lambda s: s.someday()), [_JSON]),
    Command(
        "logbook",
        "List completed/canceled todos, newest first",
        cmd_logbook,
        [Flag("--limit", True, "max rows (default 25)", "N"), _JSON],
    ),
    Command("trash", "List trashed todos", _list_cmd(lambda s: s.trash(), show_status=True), [_JSON]),
    Command("deadlines", "List open todos with deadlines", _list_cmd(lambda s: s.deadlines()), [_JSON]),
    Command(
        "todos",
        "List todos with filters",
        cmd_todos,
        [
            Flag("--project", True, "filter by project (title or id)", "REF"),
            Flag("--area", True, "filter by area (title or id)", "REF"),
            Flag("--tag", True, "filter by tag name", "NAME"),
            Flag("--search", True, "title/notes substring", "TEXT"),
            Flag("--all", False, "include completed/canceled"),
            _JSON,
        ],
    ),
    Command(
        "projects",
        "List projects",
        cmd_projects,
        [
            Flag("--area", True, "filter by area (title or id)", "REF"),
            Flag("--all", False, "include completed/canceled"),
            _JSON,
        ],
    ),
    Command("areas", "List areas", cmd_areas, [_JSON]),
    Command("tags", "List tags", cmd_tags, [_JSON]),
    Command(
        "show",
        "Show a todo/project/area/tag/heading in detail",
        cmd_show,
        [_JSON],
        [Arg("id", "id, unique id prefix, or exact title")],
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
            Flag("--when", True, _WHEN, "WHEN"),
            Flag("--deadline", True, "yyyy-mm-dd", "DATE"),
            Flag("--notes", True, "notes text", "TEXT"),
            Flag("--tags", True, "comma-separated existing tag names", "TAGS"),
            Flag("--project", True, "file into project (title or id)", "REF"),
            Flag("--area", True, "file into area (title or id)", "REF"),
            Flag("--heading", True, "file under heading (id)", "REF"),
            Flag("--checklist", True, "comma-separated checklist items", "ITEMS"),
            Flag("--evening", False, "put it in This Evening"),
            _DRY,
        ],
        [Arg("title", "todo title", variadic=True)],
    ),
    Command(
        "add-project",
        "Add a project",
        cmd_add_project,
        [
            Flag("--when", True, _WHEN, "WHEN"),
            Flag("--deadline", True, "yyyy-mm-dd", "DATE"),
            Flag("--notes", True, "notes text", "TEXT"),
            Flag("--tags", True, "comma-separated existing tag names", "TAGS"),
            Flag("--area", True, "file into area (title or id)", "REF"),
            _DRY,
        ],
        [Arg("title", "project title", variadic=True)],
    ),
    Command(
        "add-area",
        "Add an area",
        cmd_add_area,
        [_DRY],
        [Arg("title", "area title", variadic=True)],
    ),
    Command(
        "edit",
        "Edit a todo/project by id (only the fields you pass change)",
        cmd_edit,
        [
            Flag("--title", True, "new title", "TEXT"),
            Flag("--notes", True, "replace notes (--notes= clears)", "TEXT"),
            Flag("--when", True, _WHEN + " (--when= clears)", "WHEN"),
            Flag("--deadline", True, "yyyy-mm-dd (--deadline= clears)", "DATE"),
            Flag("--tags", True, "replace tags (--tags= clears)", "TAGS"),
            Flag("--project", True, "move into project (--project= clears)", "REF"),
            Flag("--area", True, "move into area (--area= clears)", "REF"),
            Flag("--heading", True, "move under heading (--heading= clears)", "REF"),
            Flag("--evening", False, "move to This Evening"),
            _DRY,
        ],
        [Arg("id", "id or unique id prefix")],
    ),
    Command(
        "complete",
        "Mark a todo/project completed",
        _status_cmd(Status.COMPLETED, "✓ completed", "bold green"),
        [_DRY],
        [Arg("id", "id or unique id prefix")],
    ),
    Command(
        "cancel",
        "Mark a todo/project canceled",
        _status_cmd(Status.CANCELED, "✗ canceled", "bold red"),
        [_DRY],
        [Arg("id", "id or unique id prefix")],
    ),
    Command(
        "reopen",
        "Reopen a completed/canceled todo/project",
        _status_cmd(Status.OPEN, "↺ reopened", "bold"),
        [_DRY],
        [Arg("id", "id or unique id prefix")],
    ),
    Command(
        "delete",
        "Move a todo/project to Trash (areas/tags are removed outright)",
        cmd_delete,
        [
            Flag("--yes", False, "confirm — required to actually delete"),
            Flag("--permanent", False, "destroy instead of trashing (no undo)"),
            Flag("--dry-run", False, "preview only (same as omitting --yes)"),
        ],
        [Arg("id", "id or unique id prefix")],
    ),
    Command(
        "auth",
        "Fetch and save the Things Cloud session (from env credentials)",
        cmd_auth,
    ),
    Command("doctor", "Verify credentials, cloud reachability, and the history", cmd_doctor),
    Command("refresh", "Clear the local cache (forces a full re-pull)", cmd_refresh),
]
COMMANDS_BY_NAME = {c.name: c for c in COMMANDS}


def _flag_label(f: Flag) -> str:
    return f"{f.name} {f.metavar}".strip() if f.takes_value else f.name


def _arg_token(a: Arg) -> str:
    """Plain label, e.g. '<id>' or '[title…]'. Not markup-safe — wrap in
    Text (table cells) or escape the leading bracket (markup strings)."""
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
