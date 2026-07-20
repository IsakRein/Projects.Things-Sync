from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import Enum


class Status(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELED = "canceled"


class StartBucket(int, Enum):
    """The wire `st` field — a task's start state. ANYTIME with a scheduled
    date that has arrived is Today; SOMEDAY with a future date is Upcoming."""

    INBOX = 0
    ANYTIME = 1
    SOMEDAY = 2


def _jsonable(v):
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, tuple):
        return list(v)
    return v


def to_json_dict(obj) -> dict:
    return {k: _jsonable(v) for k, v in asdict(obj).items()}


@dataclass(frozen=True)
class Todo:
    id: str
    title: str
    notes: str = ""
    status: Status = Status.OPEN
    start: StartBucket = StartBucket.INBOX
    when: date | None = None          # scheduled day (`sr`) — drives Today/Upcoming
    deadline: date | None = None
    evening: bool = False             # This Evening (`sb`)
    repeating: bool = False           # carries or came from a repeat rule
    is_template: bool = False         # the hidden generator row (`rr`), not a real item
    trashed: bool = False
    project_id: str | None = None
    project_title: str | None = None
    area_id: str | None = None
    area_title: str | None = None
    heading_id: str | None = None
    heading_title: str | None = None
    tags: tuple[str, ...] = ()
    created: datetime | None = None
    modified: datetime | None = None
    stopped: datetime | None = None   # completion/cancellation time
    index: int = 0
    today_index: int = 0
    checklist_total: int = 0
    checklist_open: int = 0


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    notes: str = ""
    status: Status = Status.OPEN
    when: date | None = None
    deadline: date | None = None
    trashed: bool = False
    area_id: str | None = None
    area_title: str | None = None
    tags: tuple[str, ...] = ()
    created: datetime | None = None
    modified: datetime | None = None
    stopped: datetime | None = None
    index: int = 0
    open_count: int = 0               # open, untrashed todos inside


@dataclass(frozen=True)
class Heading:
    id: str
    title: str
    project_id: str | None = None
    project_title: str | None = None
    trashed: bool = False
    index: int = 0


@dataclass(frozen=True)
class Area:
    id: str
    title: str
    tags: tuple[str, ...] = ()
    index: int = 0


@dataclass(frozen=True)
class Tag:
    id: str
    title: str
    shortcut: str = ""
    parent_id: str | None = None
    index: int = 0


@dataclass(frozen=True)
class ChecklistItem:
    id: str
    title: str
    completed: bool = False
    stopped: datetime | None = None
    index: int = 0
