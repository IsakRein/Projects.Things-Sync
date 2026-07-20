"""things-cli — a self-contained interface to Things 3 via Things Cloud.

Reads and writes go straight to Cultured Code's sync backend, so nothing
requires the Things app to be installed or running.

    from things_cli import fetch_state

    state, client = fetch_state()
    for todo in state.today_list():
        print(todo.title)
"""

from .api import Client, CloudError, Session
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
from .sync import State, SyncError, fetch_state

__all__ = [
    "Client",
    "CloudError",
    "Session",
    "State",
    "SyncError",
    "fetch_state",
    "Area",
    "ChecklistItem",
    "Heading",
    "Project",
    "StartBucket",
    "Status",
    "Tag",
    "Todo",
]
