"""Cross-check list semantics against the reference implementation.

The journals under ``fixtures/reference/`` and their expected results are
taken from evanpurkhiser/things3-cloud's test suite (MIT). Folding them
here and comparing output is what caught five real bugs in our list
derivation — Inbox including filed items, Today missing staged and evening
items, Anytime including future-scheduled items, Someday including
project-scoped items, and repeating templates leaking into every list.

Only the fixtures we reproduce exactly are vendored; the reference CLI
also renders group headers, "today" markers, and truncated area sublists,
none of which we emit.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from things_cli import sync
from things_cli.sync import State

FIXTURES = Path(__file__).parent / "fixtures" / "reference"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


@pytest.fixture
def pinned_today(monkeypatch):
    """The reference suite pins "today" via --today-ts; mirror that so the
    date-sensitive lists are reproducible."""

    def pin(ts: int | None) -> None:
        if ts is None:
            return
        day = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).date()
        monkeypatch.setattr(sync, "today", lambda: day)

    return pin


@pytest.mark.parametrize("case", MANIFEST, ids=[c["name"] for c in MANIFEST])
def test_matches_reference_implementation(case, pinned_today):
    pinned_today(case["today_ts"])
    journal = json.loads((FIXTURES / case["name"] / "journal.json").read_text())
    state = State(sync.fold_commits(journal))
    titles = [t.title for t in getattr(state, case["method"])()]
    assert titles == case["titles"]


def test_manifest_covers_every_list_method():
    """A guard against silently dropping coverage for a list."""
    assert {c["method"] for c in MANIFEST} >= {
        "inbox",
        "today_list",
        "anytime",
        "someday",
        "upcoming",
    }
