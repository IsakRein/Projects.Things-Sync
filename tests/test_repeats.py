"""Repeat-rule parsing and template payload construction.

All offline — the rrv=4 rule format and template shape were decoded from
Things.app's own local rules and wire commits (2026-07-20).
"""

from __future__ import annotations

from datetime import date

import pytest

from things_cli import sync
from things_cli.models import StartBucket
from things_cli.sync import SyncError

MON = date(2026, 7, 20)  # a Monday


# ---------- parse_reminder ----------


def test_parse_reminder_noon():
    assert sync.parse_reminder("12:00") == 43200


def test_parse_reminder_bounds():
    assert sync.parse_reminder("0:00") == 0
    assert sync.parse_reminder("23:59") == 23 * 3600 + 59 * 60
    for bad in ("24:00", "9:60", "noon", "9"):
        with pytest.raises(SyncError):
            sync.parse_reminder(bad)


# ---------- parse_repeat ----------


def test_daily():
    core, first = sync.parse_repeat("every day", MON)
    assert core == {"fa": 1, "fu": 16, "of": [{"dy": 0}], "tp": 0}
    assert first == MON


def test_after_completion_days():
    core, first = sync.parse_repeat("after 30 days", MON)
    assert core == {"fa": 30, "fu": 16, "of": [{"dy": 0}], "tp": 1}
    assert first == MON


def test_daily_rejects_on():
    with pytest.raises(SyncError):
        sync.parse_repeat("every day on monday", MON)


def test_weekly_defaults_to_base_weekday():
    core, first = sync.parse_repeat("every week", MON)
    assert core["fu"] == 256
    assert core["of"] == [{"wd": 1}]  # Monday=1, Sunday=0
    assert first == MON


def test_weekly_on_friday_advances_to_next_friday():
    core, first = sync.parse_repeat("every 3 weeks on friday", MON)
    assert core == {"fa": 3, "fu": 256, "of": [{"wd": 5}], "tp": 0}
    assert first == date(2026, 7, 24)


def test_weekly_multiple_days():
    core, first = sync.parse_repeat("every week on mon, wed and sun", MON)
    assert core["of"] == [{"wd": 1}, {"wd": 3}, {"wd": 0}]
    assert first == MON


def test_monthly_dom_is_zero_based_on_wire():
    core, first = sync.parse_repeat("every month on the 10th", MON)
    assert core == {"fa": 1, "fu": 8, "of": [{"dy": 9}], "tp": 0}
    assert first == date(2026, 8, 10)  # Jul 10 already past base


def test_monthly_last_day():
    core, first = sync.parse_repeat("every month on the last day", MON)
    assert core["of"] == [{"dy": -1}]
    assert first == date(2026, 7, 31)


def test_monthly_short_month_skips_to_month_that_has_it():
    _, first = sync.parse_repeat("every month on the 31st", date(2026, 8, 1))
    assert first == date(2026, 8, 31)
    _, first = sync.parse_repeat("every month on the 31st", date(2026, 9, 1))
    assert first == date(2026, 10, 31)  # September has no 31st


def test_yearly():
    core, first = sync.parse_repeat("every year on jul 31", MON)
    assert core == {"fa": 1, "fu": 4, "of": [{"dy": 30, "mo": 6}], "tp": 0}
    assert first == date(2026, 7, 31)


def test_yearly_day_first_and_wraparound():
    core, first = sync.parse_repeat("every year on 6 january", MON)
    assert core["of"] == [{"dy": 5, "mo": 0}]
    assert first == date(2027, 1, 6)


def test_garbage_rejected():
    for bad in ("weekly", "every fortnight", "every 2", "sometimes"):
        with pytest.raises(SyncError):
            sync.parse_repeat(bad, MON)


# ---------- build_repeat_rule ----------


def test_rule_payload_shape():
    rule, show = sync.build_repeat_rule("every day", MON)
    assert rule == {
        "ed": sync.RR_NEVER,
        "fa": 1,
        "fu": 16,
        "ia": float(sync.day_ts(MON)),
        "of": [{"dy": 0}],
        "rc": 0,
        "rrv": 4,
        "sr": float(sync.day_ts(MON)),
        "tp": 0,
        "ts": 0,
    }
    assert show == MON


def test_rule_deadline_early_shifts_start():
    rule, show = sync.build_repeat_rule(
        "every month on the 10th", MON, deadline_early=5
    )
    assert rule["ts"] == -5
    assert rule["ia"] == float(sync.day_ts(date(2026, 8, 10)))
    assert rule["sr"] == float(sync.day_ts(date(2026, 8, 5)))
    assert show == date(2026, 8, 5)


# ---------- build_create_todo with repeat ----------


def props_of(changes, uuid):
    return changes[uuid]["p"]


def test_template_props():
    uuid, changes = sync.build_create_todo(
        "Trim toenails", repeat="after 3 weeks", when="2026-07-20",
        reminder=43200,
    )
    p = props_of(changes, uuid)
    assert p["rr"]["tp"] == 1
    assert p["rr"]["fu"] == 256
    assert p["st"] == StartBucket.SOMEDAY.value
    assert p["sr"] is None and p["tir"] is None and p["ti"] == 0
    assert p["icsd"] == sync.day_ts(MON)
    assert p["icc"] == 0 and p["icp"] is False
    assert p["ato"] == 43200
    assert p["dd"] is None


def test_template_deadline_sentinel():
    uuid, changes = sync.build_create_todo(
        "Momsredovisning", repeat="every 3 months on the 10th",
        when="2026-07-20", deadline_early=40,
    )
    p = props_of(changes, uuid)
    assert p["dd"] == sync.RR_NEVER
    assert p["rr"]["ts"] == -40


def test_repeat_rejects_deadline_and_vague_when():
    with pytest.raises(SyncError):
        sync.build_create_todo("x", repeat="every day", deadline="2026-08-01")
    with pytest.raises(SyncError):
        sync.build_create_todo("x", repeat="every day", when="someday")


def test_checklist_does_not_touch_icc():
    uuid, changes = sync.build_create_todo("Morning", checklist=["a", "b", "c"])
    assert props_of(changes, uuid)["icc"] == 0
    items = [c for k, c in changes.items() if k != uuid]
    assert [c["p"]["tt"] for c in items] == ["a", "b", "c"]
    assert all(c["p"]["ts"] == [uuid] for c in items)
