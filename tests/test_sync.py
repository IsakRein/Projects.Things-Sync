"""Journal folding, date encoding, and write-payload construction.

All offline — no network, no Things app.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from things_cli import sync
from things_cli.models import StartBucket, Status
from things_cli.sync import UNSET, State


def task(uuid, **props):
    return {uuid: {"t": sync.OP_CREATE, "e": "Task6", "p": props}}


def update(uuid, **props):
    return {uuid: {"t": sync.OP_UPDATE, "e": "Task6", "p": props}}


def state_from(*commits) -> State:
    return State(sync.fold_commits(list(commits)))


# ---------- day timestamps ----------


def test_day_ts_is_midnight_utc():
    ts = sync.day_ts(date(2026, 3, 25))
    assert ts % 86400 == 0
    assert datetime.fromtimestamp(ts, tz=timezone.utc).date() == date(2026, 3, 25)


def test_day_ts_round_trips():
    for d in (date(1990, 1, 1), date(2026, 7, 20), date(2040, 12, 31)):
        assert sync.day_from_ts(sync.day_ts(d)) == d


def test_day_from_ts_tolerates_garbage():
    assert sync.day_from_ts(None) is None
    assert sync.day_from_ts("nonsense") is None


# ---------- notes ----------


def test_notes_round_trip_through_structured_form():
    payload = sync.encode_notes("first line\nsecond line")
    assert payload["_t"] == "tx"
    assert payload["t"] == 1
    assert sync.decode_notes(payload) == "first line\nsecond line"


def test_notes_checksum_is_crc32_of_utf8():
    import zlib

    text = "café ☕"
    assert sync.encode_notes(text)["ch"] == zlib.crc32(text.encode("utf-8"))


def test_decode_notes_handles_legacy_plain_string():
    assert sync.decode_notes("just text") == "just text"


def test_decode_notes_handles_paragraph_list():
    nt = {"_t": "tx", "t": 2, "ps": [{"r": "one"}, {"r": "two"}]}
    assert sync.decode_notes(nt) == "one\ntwo"


def test_decode_notes_handles_missing_and_unknown():
    assert sync.decode_notes(None) == ""
    assert sync.decode_notes({"t": 99}) == ""


# ---------- folding ----------


def test_create_then_update_merges_fields():
    state = state_from(
        task("a", tt="Write tests", ss=0, st=1),
        update("a", tt="Write better tests"),
    )
    assert state.todos["a"].title == "Write better tests"
    assert state.todos["a"].status == Status.OPEN


def test_update_null_clears_a_field():
    """A JSON null in a patch clears the field — distinct from the key
    being absent, which leaves it alone."""
    state = state_from(
        task("a", tt="Scheduled", st=2, sr=sync.day_ts(date(2030, 1, 1))),
        update("a", sr=None),
    )
    assert state.todos["a"].when is None


def test_delete_removes_the_object():
    state = state_from(
        task("a", tt="Doomed"),
        {"a": {"t": sync.OP_DELETE, "e": "Task6", "p": {}}},
    )
    assert "a" not in state.todos


def test_update_before_create_is_treated_as_create():
    """Histories can start mid-stream when folding from a cursor."""
    state = state_from(update("a", tt="Orphan patch"))
    assert state.todos["a"].title == "Orphan patch"


def test_unknown_entity_types_are_skipped_not_fatal():
    raw = sync.fold_commits([{"x": {"t": 0, "e": "Task99Future", "p": {"tt": "?"}}}])
    assert raw == {}


def test_legacy_entity_names_still_fold():
    state = state_from({"a": {"t": 0, "e": "Task3", "p": {"tt": "Old", "st": 1}}})
    assert state.todos["a"].title == "Old"


# ---------- materialisation ----------


def test_project_and_heading_relationships_resolve():
    state = state_from(
        {"area1": {"t": 0, "e": "Area3", "p": {"tt": "Work"}}},
        task("proj1", tt="Launch", tp=1, ar=["area1"], st=1),
        task("head1", tt="Phase one", tp=2, pr=["proj1"], st=1),
        task("todo1", tt="Draft copy", tp=0, agr=["head1"], st=1),
    )
    todo = state.todos["todo1"]
    assert todo.heading_title == "Phase one"
    # project comes via the heading even though `pr` is empty on the todo
    assert todo.project_title == "Launch"
    # and the area comes via the project
    assert todo.area_title == "Work"


def test_tags_resolve_to_titles():
    state = state_from(
        {"tag1": {"t": 0, "e": "Tag4", "p": {"tt": "urgent"}}},
        task("a", tt="Tagged", tg=["tag1"], st=1),
    )
    assert state.todos["a"].tags == ("urgent",)


def test_checklist_counts_attach_to_their_todo():
    state = state_from(
        task("a", tt="With checklist", st=1),
        {"c1": {"t": 0, "e": "ChecklistItem3", "p": {"tt": "one", "ss": 3, "ts": ["a"], "ix": 0}}},
        {"c2": {"t": 0, "e": "ChecklistItem3", "p": {"tt": "two", "ss": 0, "ts": ["a"], "ix": 1}}},
    )
    assert state.todos["a"].checklist_total == 2
    assert state.todos["a"].checklist_open == 1
    assert [c.title for c in state.checklists["a"]] == ["one", "two"]


def test_project_open_count_counts_only_open_untrashed():
    state = state_from(
        task("p", tt="Proj", tp=1, st=1),
        task("t1", tt="open", pr=["p"], st=1),
        task("t2", tt="done", pr=["p"], st=1, ss=3),
        task("t3", tt="trashed", pr=["p"], st=1, tr=True),
    )
    assert state.projects["p"].open_count == 1


# ---------- built-in lists ----------


def test_today_is_started_with_an_arrived_date():
    today_ts = sync.day_ts(date.today())
    state = state_from(
        task("a", tt="Due now", st=1, sr=today_ts, tir=today_ts),
        task("b", tt="Anytime", st=1),
        task("c", tt="Someday", st=2),
    )
    assert [t.id for t in state.today_list()] == ["a"]


def test_upcoming_is_a_future_date():
    future = sync.day_ts(date.today() + timedelta(days=7))
    state = state_from(task("a", tt="Later", st=2, sr=future, tir=future))
    assert [t.id for t in state.upcoming()] == ["a"]
    assert state.someday() == []


def test_someday_excludes_dated_items():
    state = state_from(
        task("a", tt="Someday", st=2),
        task("b", tt="Upcoming", st=2, sr=sync.day_ts(date.today() + timedelta(days=3))),
    )
    assert [t.id for t in state.someday()] == ["a"]


def test_inbox_is_the_not_started_bucket():
    state = state_from(task("a", tt="Unfiled", st=0), task("b", tt="Filed", st=1))
    assert [t.id for t in state.inbox()] == ["a"]


def test_evening_items_sort_after_the_rest_of_today():
    today_ts = sync.day_ts(date.today())
    state = state_from(
        task("evening", tt="Evening", st=1, sr=today_ts, tir=today_ts, sb=1),
        task("day", tt="Daytime", st=1, sr=today_ts, tir=today_ts),
    )
    assert [t.id for t in state.today_list()] == ["day", "evening"]


def test_lists_hide_todos_whose_project_is_trashed():
    state = state_from(
        task("p", tt="Dead project", tp=1, st=1, tr=True),
        task("t", tt="Orphan", pr=["p"], st=1),
    )
    assert state.anytime() == []


def test_logbook_is_newest_first_and_limited():
    base = datetime(2026, 3, 1).timestamp()
    state = state_from(
        task("a", tt="First", st=1, ss=3, sp=base),
        task("b", tt="Second", st=1, ss=3, sp=base + 3600),
        task("c", tt="Canceled", st=1, ss=2, sp=base + 7200),
    )
    assert [t.id for t in state.logbook()] == ["c", "b", "a"]
    assert [t.id for t in state.logbook(limit=1)] == ["c"]


def test_trash_and_deadlines():
    state = state_from(
        task("a", tt="Trashed", st=1, tr=True),
        task("b", tt="Due", st=1, dd=sync.day_ts(date(2026, 9, 1))),
    )
    assert [t.id for t in state.trash()] == ["a"]
    assert [t.id for t in state.deadlines()] == ["b"]


# ---------- list semantics caught by differential testing ----------
#
# Every case below was a real bug, found by folding the reference project's
# fixtures and diffing against its expected output. Keep them.


def test_inbox_excludes_items_already_filed():
    """`st=0` is not enough — a task in a project or area has been triaged
    out of the Inbox even though it kept the not-started state."""
    state = state_from(
        task("loose", tt="Top-level inbox task", st=0),
        task("p", tt="Kitchen Refresh", tp=1, st=1),
        task("filed", tt="Choose tile samples", st=0, pr=["p"]),
        {"area1": {"t": 0, "e": "Area3", "p": {"tt": "Work"}}},
        task("inarea", tt="Draft quarterly goals", st=0, ar=["area1"]),
    )
    assert [t.id for t in state.inbox()] == ["loose"]


def test_today_includes_staged_someday_items():
    """A deferred item whose date has arrived shows in Today."""
    today_ts = sync.day_ts(date.today())
    state = state_from(
        task("staged", tt="Call back eye doctor", st=2, sr=today_ts),
        task("normal", tt="Set up git config", st=1, sr=today_ts, tir=today_ts),
    )
    assert {t.id for t in state.today_list()} == {"staged", "normal"}


def test_today_includes_undated_evening_items():
    state = state_from(task("e", tt="Loose evening task", st=1, sb=1))
    assert [t.id for t in state.today_list()] == ["e"]


def test_today_includes_undated_overdue_deadlines():
    overdue = sync.day_ts(date.today() - timedelta(days=2))
    state = state_from(task("d", tt="Overdue", st=1, dd=overdue))
    assert [t.id for t in state.today_list()] == ["d"]


def test_anytime_excludes_future_scheduled_items():
    """A future-dated item is waiting in Upcoming, not available Anytime."""
    tomorrow = sync.day_ts(date.today() + timedelta(days=1))
    state = state_from(
        task("now", tt="Visible anytime task", st=1),
        task("later", tt="Future scheduled", st=1, sr=tomorrow),
    )
    assert [t.id for t in state.anytime()] == ["now"]
    # ...and it is still reachable, via Upcoming
    assert [t.id for t in state.upcoming()] == ["later"]


def test_someday_excludes_project_scoped_items():
    state = state_from(
        task("proj", tt="Cabin renovation", tp=1, st=2),
        task("loose", tt="Read design books", st=2),
        task("inproj", tt="Research insulation", st=2, pr=["proj"]),
    )
    assert [t.id for t in state.someday()] == ["loose"]


def test_repeating_templates_are_hidden_from_lists():
    """`rr` marks the hidden generator row; only its instances show."""
    state = state_from(
        task("tmpl", tt="Water houseplants", st=2, rr={"tp": 0, "fu": 8}),
        task("real", tt="Read design books", st=2),
    )
    assert [t.id for t in state.someday()] == ["real"]
    assert state.todos["tmpl"].is_template is True
    # the instance links back via `rt` and stays visible
    inst = state_from(task("i", tt="Instance", st=1, rt=["tmpl"]))
    assert inst.anytime()[0].id == "i"
    assert inst.todos["i"].is_template is False


def test_template_projects_are_hidden_from_the_projects_list():
    state = state_from(
        task("p1", tt="Real project", tp=1, st=1),
        task("p2", tt="Template project", tp=1, st=1, rr={"tp": 0}),
    )
    assert [p.id for p in state.open_projects()] == ["p1"]


# ---------- resolution ----------


def test_resolve_by_prefix_title_and_full_id():
    state = state_from(
        task("abcdef123", tt="A todo", st=1),
        task("proj9999", tt="Website", tp=1, st=1),
    )
    assert state.resolve("abcdef123") == ("todo", "abcdef123")
    assert state.resolve("abcd") == ("todo", "abcdef123")
    assert state.resolve("Website") == ("project", "proj9999")


def test_ambiguous_prefix_raises():
    state = state_from(task("aaa1", tt="One", st=1), task("aaa2", tt="Two", st=1))
    with pytest.raises(sync.SyncError, match="ambiguous"):
        state.resolve("aaa")


def test_unknown_reference_raises():
    with pytest.raises(sync.SyncError, match="nothing matches"):
        state_from().resolve("nope")


def test_duplicate_titles_are_refused_not_guessed():
    """Todo titles aren't unique. Since this resolver also backs `delete`
    and `complete`, more than one hit must be an error, and the message
    must say so — not claim nothing matched."""
    state = state_from(
        task("aaa1", tt="Test", st=1),
        task("bbb2", tt="Test", st=0),
    )
    with pytest.raises(sync.SyncError, match="2 items are titled 'Test'"):
        state.resolve("Test")


def test_a_unique_todo_title_resolves():
    state = state_from(task("aaa1", tt="Distinctive name", st=1))
    assert state.resolve("Distinctive name") == ("todo", "aaa1")


def test_require_rejects_the_wrong_kind():
    state = state_from(task("a", tt="A todo", st=1))
    with pytest.raises(sync.SyncError, match="expected project"):
        state.require("a", "project")


# ---------- schedule invariants (the crash-inducing ones) ----------


def test_today_uses_started_state_never_deferred():
    """`st=2` (deferred) paired with today's date has no valid UI
    representation and crashes Things.app."""
    props: dict = {}
    sync.apply_when(props, "today")
    assert props["st"] == StartBucket.ANYTIME.value
    assert props["sr"] == props["tir"] == sync.day_ts(date.today())


def test_a_past_date_is_treated_as_today_not_deferred():
    props: dict = {}
    sync.apply_when(props, (date.today() - timedelta(days=5)).isoformat())
    assert props["st"] == StartBucket.ANYTIME.value


def test_future_date_is_deferred():
    props: dict = {}
    future = date.today() + timedelta(days=10)
    sync.apply_when(props, future.isoformat())
    assert props["st"] == StartBucket.SOMEDAY.value
    assert props["sr"] == sync.day_ts(future)


def test_anytime_and_someday_clear_the_date():
    for when, expected in (("anytime", 1), ("someday", 2)):
        props: dict = {}
        sync.apply_when(props, when)
        assert props["st"] == expected
        assert props["sr"] is None and props["tir"] is None


def test_evening_sets_the_evening_bit_on_today():
    props: dict = {}
    sync.apply_when(props, "evening")
    assert props["st"] == StartBucket.ANYTIME.value
    assert props["sb"] == 1


def test_parse_day_accepts_words_and_iso():
    assert sync.parse_day("today") == date.today()
    assert sync.parse_day("tomorrow") == date.today() + timedelta(days=1)
    assert sync.parse_day("2026-05-01") == date(2026, 5, 1)
    with pytest.raises(sync.SyncError):
        sync.parse_day("next tuesday")


# ---------- write payloads ----------


# The exact property set Things.app itself writes on a Task6 create,
# captured from a real client's traffic. A create missing any of these is
# accepted by the server and renders in a running client, but is DISCARDED
# when Things rebuilds its store from the journal — the items silently
# vanish on next launch.
REAL_TASK6_KEYS = {
    "acrd", "agr", "ar", "ato", "cd", "dd", "dds", "dl", "do", "icc",
    "icp", "icsd", "ix", "lai", "lt", "md", "nt", "pr", "rmd", "rp",
    "rr", "rt", "sb", "sp", "sr", "ss", "st", "tg", "ti", "tir", "tp",
    "tr", "tt", "xx",
}


def test_created_todo_emits_the_complete_property_set():
    _uuid, changes = sync.build_create_todo("Complete payload")
    assert set(next(iter(changes.values()))["p"]) == REAL_TASK6_KEYS


def test_created_project_emits_the_complete_property_set():
    _uuid, changes = sync.build_create_project("Complete project")
    assert set(next(iter(changes.values()))["p"]) == REAL_TASK6_KEYS


def test_every_scheduling_variant_still_emits_the_complete_set():
    for when in (None, "today", "evening", "anytime", "someday", "2030-01-01"):
        _uuid, changes = sync.build_create_todo("x", when=when)
        assert set(next(iter(changes.values()))["p"]) == REAL_TASK6_KEYS, when


def test_created_area_and_tag_emit_their_complete_sets():
    _uuid, changes = sync.build_create_area("Area")
    assert set(next(iter(changes.values()))["p"]) == {"ix", "tg", "tt", "xx"}
    _uuid, changes = sync.build_create_tag("Tag")
    assert set(next(iter(changes.values()))["p"]) == {"ix", "pn", "sh", "tt", "xx"}


def test_created_checklist_item_emits_its_complete_set():
    _uuid, changes = sync.build_create_todo("parent", checklist=["one"])
    item = [c for c in changes.values() if c["e"] == "ChecklistItem3"][0]
    assert set(item["p"]) == {"cd", "ix", "lt", "md", "sp", "ss", "ts", "tt", "xx"}


def test_checklist_count_is_recorded_on_the_parent():
    _uuid, changes = sync.build_create_todo("parent", checklist=["a", "b", "c"])
    parent = [c for c in changes.values() if c["e"] == "Task6"][0]
    assert parent["p"]["icc"] == 3


def test_new_items_are_placed_after_their_siblings():
    """Creating everything at ix=0 collides every item at one position and
    leaves ordering to an undefined tiebreak."""
    state = state_from(
        task("a", tt="first", st=1, ix=0),
        task("b", tt="second", st=1, ix=1),
    )
    assert state.next_todo_index() == 2


def test_sibling_index_is_scoped_to_the_container():
    state = state_from(
        task("p", tt="Proj", tp=1, st=1, ix=0),
        task("loose", tt="loose", st=1, ix=7),
        task("inproj", tt="child", st=1, ix=3, pr=["p"]),
    )
    assert state.next_todo_index() == 8              # root siblings only
    assert state.next_todo_index(project_id="p") == 4  # project siblings only


def test_area_and_project_indices_are_independent():
    state = state_from(
        {"ar1": {"t": 0, "e": "Area3", "p": {"tt": "A", "ix": 5}}},
        task("p1", tt="P1", tp=1, st=1, ix=2, ar=["ar1"]),
    )
    assert state.next_area_index() == 6
    assert state.next_project_index("ar1") == 3
    assert state.next_project_index(None) == 0  # no siblings in that container


def test_created_items_carry_the_index_they_were_given():
    _uuid, changes = sync.build_create_todo("x", index=9, today_index=4)
    p = next(iter(changes.values()))["p"]
    assert p["ix"] == 9 and p["ti"] == 4
    _uuid, changes = sync.build_create_area("A", index=3)
    assert next(iter(changes.values()))["p"]["ix"] == 3


def test_updates_stay_sparse():
    """Only creates need the full set — real clients patch sparsely, and
    sending defaults on an update would clobber fields."""
    changes = sync.build_update("VJ1edXTP9q3PmFDUuy8EQh", title="new")
    assert set(changes["VJ1edXTP9q3PmFDUuy8EQh"]["p"]) == {"tt", "md"}


def test_created_todo_has_valid_identifier_and_base_fields():
    from things_cli import base58

    uuid, changes = sync.build_create_todo("Buy milk")
    base58.validate(uuid)
    change = changes[uuid]
    assert change["t"] == sync.OP_CREATE and change["e"] == "Task6"
    p = change["p"]
    assert p["tt"] == "Buy milk"
    assert p["tp"] == 0 and p["ss"] == 0
    assert p["st"] == StartBucket.INBOX.value  # unfiled, unscheduled → Inbox
    assert isinstance(p["cd"], float) and p["cd"] != int(p["cd"]) or p["cd"] > 0
    assert p["xx"] == {"_t": "oo", "sn": {}}


def test_timestamps_are_fractional_floats():
    """Integer truncation risks conflict-resolution ordering bugs."""
    _uuid, changes = sync.build_create_todo("x")
    p = next(iter(changes.values()))["p"]
    assert isinstance(p["cd"], float)
    assert isinstance(p["md"], float)


def test_filing_into_a_container_leaves_the_inbox_state():
    """Anything placed in a project/area/heading is already triaged;
    leaving it at st=0 strands it in the Inbox."""
    for kwargs in (
        {"project_id": "VJ1edXTP9q3PmFDUuy8EQh"},
        {"area_id": "FQxaqvLBkbR5q2Q5oRoknc"},
        {"heading_id": "BVU8qZ9dNjrdxLvDHPvfDS"},
    ):
        _uuid, changes = sync.build_create_todo("filed", **kwargs)
        p = next(iter(changes.values()))["p"]
        assert p["st"] == StartBucket.ANYTIME.value


def test_projects_are_never_inbox_items():
    _uuid, changes = sync.build_create_project("Big project")
    p = next(iter(changes.values()))["p"]
    assert p["tp"] == 1
    assert p["st"] != StartBucket.INBOX.value


def test_checklist_items_are_created_alongside_and_linked():
    uuid, changes = sync.build_create_todo("Trip", checklist=["passport", "tickets"])
    items = [c for c in changes.values() if c["e"] == "ChecklistItem3"]
    assert len(items) == 2
    assert all(c["p"]["ts"] == [uuid] for c in items)
    assert [c["p"]["ix"] for c in items] == [0, 1]


def test_create_rejects_a_malformed_container_id():
    from things_cli.base58 import Base58Error

    with pytest.raises(Base58Error):
        sync.build_create_todo("bad", project_id="not-base58!")


# ---------- update patch semantics (UNSET vs None) ----------


def test_unset_fields_are_absent_from_the_patch():
    uuid = "VJ1edXTP9q3PmFDUuy8EQh"
    changes = sync.build_update(uuid, title="Renamed")
    p = changes[uuid]["p"]
    assert p["tt"] == "Renamed"
    # nothing else was touched — only the modification stamp rides along
    assert set(p) == {"tt", "md"}


def test_explicit_none_clears_rather_than_being_skipped():
    uuid = "VJ1edXTP9q3PmFDUuy8EQh"
    changes = sync.build_update(uuid, deadline=None)
    assert changes[uuid]["p"]["dd"] is None


def test_clearing_a_project_sends_an_empty_array():
    uuid = "VJ1edXTP9q3PmFDUuy8EQh"
    changes = sync.build_update(uuid, project_id=None)
    assert changes[uuid]["p"]["pr"] == []


def test_moving_into_a_container_also_triages_out_of_inbox():
    uuid = "VJ1edXTP9q3PmFDUuy8EQh"
    changes = sync.build_update(uuid, project_id="FQxaqvLBkbR5q2Q5oRoknc")
    p = changes[uuid]["p"]
    assert p["pr"] == ["FQxaqvLBkbR5q2Q5oRoknc"]
    assert p["st"] == StartBucket.ANYTIME.value


def test_an_explicit_when_wins_over_the_container_default():
    uuid = "VJ1edXTP9q3PmFDUuy8EQh"
    changes = sync.build_update(
        uuid, project_id="FQxaqvLBkbR5q2Q5oRoknc", when="someday"
    )
    assert changes[uuid]["p"]["st"] == StartBucket.SOMEDAY.value


def test_completing_sets_status_and_stop_date():
    uuid = "VJ1edXTP9q3PmFDUuy8EQh"
    p = sync.build_update(uuid, status=Status.COMPLETED)[uuid]["p"]
    assert p["ss"] == 3
    assert isinstance(p["sp"], float)


def test_reopening_clears_the_stop_date():
    uuid = "VJ1edXTP9q3PmFDUuy8EQh"
    p = sync.build_update(uuid, status=Status.OPEN)[uuid]["p"]
    assert p["ss"] == 0
    assert p["sp"] is None


def test_update_validates_the_target_identifier():
    from things_cli.base58 import Base58Error

    with pytest.raises(Base58Error):
        sync.build_update("not-base58!", title="x")


def test_delete_builds_a_delete_op():
    uuid = "VJ1edXTP9q3PmFDUuy8EQh"
    changes = sync.build_delete(uuid)
    assert changes[uuid]["t"] == sync.OP_DELETE
