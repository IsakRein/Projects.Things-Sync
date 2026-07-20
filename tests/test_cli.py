"""CLI wiring — command dispatch, rendering, and the write guard rails.

Runs entirely against a stubbed cloud, so no credentials or network.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from things_cli import cli, sync
from things_cli.sync import State

TAG = "VJ1edXTP9q3PmFDUuy8EQh"
AREA = "FQxaqvLBkbR5q2Q5oRoknc"
PROJ = "BVU8qZ9dNjrdxLvDHPvfDS"
TODO = "A7h5eCi24RvAWKC3Hv3muf"


class FakeClient:
    """Records commits instead of sending them."""

    def __init__(self):
        self.commits: list[tuple[dict, int]] = []

    def commit(self, changes, ancestor_index):
        self.commits.append((changes, ancestor_index))
        return ancestor_index + 1


@pytest.fixture
def cloud(monkeypatch):
    """A small account: one area, one project, one tag, one scheduled todo."""
    today_ts = sync.day_ts(date.today())
    commits = [
        {AREA: {"t": 0, "e": "Area3", "p": {"tt": "Work", "ix": 0}}},
        {TAG: {"t": 0, "e": "Tag4", "p": {"tt": "urgent", "ix": 0}}},
        {PROJ: {"t": 0, "e": "Task6", "p": {"tt": "Website", "tp": 1, "st": 1, "ar": [AREA]}}},
        {
            TODO: {
                "t": 0,
                "e": "Task6",
                "p": {
                    "tt": "Ship the thing",
                    "tp": 0,
                    "st": 1,
                    "sr": today_ts,
                    "tir": today_ts,
                    "pr": [PROJ],
                    "tg": [TAG],
                    "notes": "",
                },
            }
        },
    ]
    state = State(sync.fold_commits(commits), head_index=42)
    client = FakeClient()
    monkeypatch.setattr(sync, "fetch_state", lambda *a, **k: (state, client))
    monkeypatch.setattr(sync, "clear_cache", lambda: None)
    return client


def _run(argv) -> int:
    """Invoke the CLI exactly as the console script does."""
    import sys

    old = sys.argv
    sys.argv = ["things", *argv]
    try:
        return cli.main()
    finally:
        sys.argv = old


# ---------- reads ----------


@pytest.mark.parametrize(
    "command",
    ["status", "today", "inbox", "upcoming", "anytime", "someday",
     "logbook", "trash", "deadlines", "todos", "projects", "areas", "tags"],
)
def test_read_commands_render_without_error(cloud, capsys, command):
    assert _run([command]) == 0
    assert capsys.readouterr().out.strip()


def test_today_lists_the_scheduled_todo(cloud, capsys):
    _run(["today"])
    assert "Ship the thing" in capsys.readouterr().out


def test_json_output_is_valid_and_carries_fields(cloud, capsys):
    _run(["today", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["title"] == "Ship the thing"
    assert payload[0]["project_title"] == "Website"
    assert payload[0]["tags"] == ["urgent"]


def test_show_resolves_a_prefix(cloud, capsys):
    assert _run(["show", TODO[:6]]) == 0
    out = capsys.readouterr().out
    assert "Ship the thing" in out and "Website" in out


def test_show_reports_an_unknown_id(cloud, capsys):
    assert _run(["show", "zzzz"]) == 1
    assert "nothing matches" in capsys.readouterr().err


def test_search_finds_across_todos_and_projects(cloud, capsys):
    _run(["search", "web"])
    assert "Website" in capsys.readouterr().out


def test_projects_filtered_by_area_title(cloud, capsys):
    _run(["projects", "--area", "Work"])
    assert "Website" in capsys.readouterr().out


# ---------- writes ----------


def test_dry_run_prints_payload_and_sends_nothing(cloud, capsys):
    assert _run(["add", "New task", "--when", "today", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would commit" in out
    assert "Task6" in out
    assert cloud.commits == []


def test_add_commits_a_well_formed_create(cloud, capsys):
    assert _run(["add", "New task", "--when", "today"]) == 0
    (changes, ancestor), = cloud.commits
    assert ancestor == 42
    (change,) = changes.values()
    assert change["t"] == sync.OP_CREATE and change["e"] == "Task6"
    assert change["p"]["tt"] == "New task"
    assert change["p"]["st"] == 1  # today is *started*, never deferred


def test_add_resolves_container_and_tags_by_name(cloud):
    _run(["add", "Filed task", "--project", "Website", "--tags", "urgent"])
    (changes, _), = cloud.commits
    props = next(iter(changes.values()))["p"]
    assert props["pr"] == [PROJ]
    assert props["tg"] == [TAG]
    assert props["st"] == 1  # filed items leave the Inbox


def test_add_rejects_an_unknown_tag(cloud, capsys):
    assert _run(["add", "x", "--tags", "nonexistent"]) == 1
    assert "unknown tag" in capsys.readouterr().err
    assert cloud.commits == []


def test_edit_only_sends_the_fields_given(cloud):
    _run(["edit", TODO[:6], "--title", "Renamed"])
    (changes, _), = cloud.commits
    props = changes[TODO]["p"]
    assert props["tt"] == "Renamed"
    assert set(props) == {"tt", "md"}


def test_edit_with_empty_value_clears_the_field(cloud):
    _run(["edit", TODO[:6], "--deadline="])
    (changes, _), = cloud.commits
    assert changes[TODO]["p"]["dd"] is None


def test_edit_without_any_field_is_an_error(cloud, capsys):
    assert _run(["edit", TODO[:6]]) == 2
    assert "nothing to change" in capsys.readouterr().err
    assert cloud.commits == []


def test_complete_sets_status_and_stop_date(cloud):
    _run(["complete", TODO[:6]])
    (changes, _), = cloud.commits
    assert changes[TODO]["p"]["ss"] == 3
    assert changes[TODO]["p"]["sp"] is not None


def test_reopen_clears_the_stop_date(cloud):
    _run(["reopen", TODO[:6]])
    (changes, _), = cloud.commits
    assert changes[TODO]["p"]["ss"] == 0
    assert changes[TODO]["p"]["sp"] is None


def test_delete_requires_confirmation(cloud, capsys):
    assert _run(["delete", TODO[:6]]) == 0
    assert "pass --yes" in capsys.readouterr().out
    assert cloud.commits == []


def test_delete_trashes_by_default(cloud):
    """`delete` should mean what it means in Things' own UI — recoverable —
    rather than destroying the item outright."""
    assert _run(["delete", TODO[:6], "--yes"]) == 0
    (changes, _), = cloud.commits
    assert changes[TODO]["t"] == sync.OP_UPDATE
    assert changes[TODO]["p"]["tr"] is True


def test_permanent_delete_is_opt_in(cloud):
    assert _run(["delete", TODO[:6], "--yes", "--permanent"]) == 0
    (changes, _), = cloud.commits
    assert changes[TODO]["t"] == sync.OP_DELETE


def test_areas_have_no_trash_so_they_are_removed_outright(cloud):
    assert _run(["delete", AREA[:6], "--yes"]) == 0
    (changes, _), = cloud.commits
    assert changes[AREA]["t"] == sync.OP_DELETE


def test_delete_takes_checklist_items_with_it(monkeypatch, cloud):
    """Nothing cascades server-side, so a permanent delete of the parent
    would strand its checklist rows in the journal forever."""
    item = "CK9dARrf2ezbFvrVUUxkHE"
    commits = [
        {TODO: {"t": 0, "e": "Task6", "p": {"tt": "Parent", "st": 1}}},
        {item: {"t": 0, "e": "ChecklistItem3", "p": {"tt": "step", "ts": [TODO], "ix": 0}}},
    ]
    state = State(sync.fold_commits(commits), head_index=1)
    monkeypatch.setattr(sync, "fetch_state", lambda *a, **k: (state, cloud))

    assert _run(["delete", TODO[:6], "--yes", "--permanent"]) == 0
    (changes, _), = cloud.commits
    assert set(changes) == {TODO, item}
    assert all(c["t"] == sync.OP_DELETE for c in changes.values())


def test_ambiguous_reference_is_refused_before_writing(cloud, capsys, monkeypatch):
    state = State(
        sync.fold_commits([
            {"dup1aaa": {"t": 0, "e": "Task6", "p": {"tt": "One", "st": 1}}},
            {"dup2bbb": {"t": 0, "e": "Task6", "p": {"tt": "Two", "st": 1}}},
        ])
    )
    monkeypatch.setattr(sync, "fetch_state", lambda *a, **k: (state, cloud))
    assert _run(["complete", "dup"]) == 1
    assert "ambiguous" in capsys.readouterr().err
    assert cloud.commits == []


# ---------- parser ----------


def test_unknown_command_exits_two(capsys):
    assert _run(["bogus"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_unknown_option_exits_two(capsys):
    assert _run(["today", "--nope"]) == 2
    assert "unknown option" in capsys.readouterr().err


def test_missing_required_argument_exits_two(capsys):
    assert _run(["show"]) == 2
    assert "missing argument" in capsys.readouterr().err


def test_variadic_title_joins_words(cloud):
    _run(["add", "several", "words", "here"])
    (changes, _), = cloud.commits
    assert next(iter(changes.values()))["p"]["tt"] == "several words here"


def test_help_lists_every_command(capsys):
    assert _run([]) == 0
    out = capsys.readouterr().out
    for name in ("today", "add", "edit", "complete", "delete", "doctor"):
        assert name in out
