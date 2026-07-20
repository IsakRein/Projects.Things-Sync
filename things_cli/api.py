"""Direct HTTP client for the Things Cloud sync backend.

Talks straight to ``https://cloud.culturedcode.com/version/1`` — the same
private API Things.app syncs through — so no local app is needed. The
account's history is an append-only journal of items; reads pull items
since an index, writes POST a commit against the current head index.

Auth model: ``GET /account/{email}`` with an ``Authorization: Password …``
header returns the account's ``history-key``. That key is the actual
credential — every later request only needs it in the URL. We cache it in
a session file so routine runs never send the password.

Credentials come from ``THINGS_CLOUD_EMAIL`` / ``THINGS_CLOUD_PASSWORD``
(direnv-loaded from ~/.envrc), used once by ``things auth`` (or lazily on
first run) to fetch and persist the history key.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_URL = "https://cloud.culturedcode.com/version/1"

# Present ourselves as a current Things Mac client (matching things3-cloud).
HEADERS = {
    "Accept": "application/json",
    "Accept-Charset": "UTF-8",
    "User-Agent": "ThingsMac/32209501",
    "App-Id": "com.culturedcode.ThingsMac",
    "App-Instance-Id": "-com.culturedcode.ThingsMac",
    "Schema": "301",
}

ENV_EMAIL = "THINGS_CLOUD_EMAIL"
ENV_PASSWORD = "THINGS_CLOUD_PASSWORD"

CONFIG_DIR = Path.home() / ".config" / "things-cli"
SESSION_PATH = CONFIG_DIR / "auth.json"


class CloudError(Exception):
    """A user-facing Things Cloud problem (auth, network, protocol)."""


@dataclass
class Session:
    email: str
    history_key: str


# ---------- session / credentials ----------


def env_credentials() -> tuple[str, str] | None:
    email = os.environ.get(ENV_EMAIL, "").strip()
    password = os.environ.get(ENV_PASSWORD, "").strip()
    if email and password:
        return email, password
    return None


def load_session(path: Path = SESSION_PATH) -> Session | None:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    email, key = data.get("email"), data.get("history_key")
    if not email or not key:
        return None
    return Session(email=str(email), history_key=str(key))


def save_session(session: Session, path: Path = SESSION_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": 1, "email": session.email, "history_key": session.history_key},
            indent=2,
        )
        + "\n"
    )
    path.chmod(0o600)
    return path


# ---------- HTTP ----------


def _request(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json; charset=UTF-8"
        headers["Content-Encoding"] = "UTF-8"  # yes, literally — Things sends this
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=60) as resp:
            text = resp.read().decode()
    except HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 401:
            raise CloudError("Things Cloud rejected the credentials (HTTP 401)") from e
        raise CloudError(f"Things Cloud HTTP {e.code} for {url}: {detail}") from e
    except URLError as e:
        raise CloudError(f"Things Cloud network error: {e.reason}") from e
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise CloudError(f"invalid JSON from {url}") from e


def fetch_history_key(email: str, password: str) -> str:
    """Log in (GET account with the password header) → the history key."""
    url = f"{BASE_URL}/account/{quote(email, safe='')}"
    result = _request(
        "GET",
        url,
        extra_headers={"Authorization": f"Password {quote(password, safe='')}"},
    )
    key = result.get("history-key")
    if not key:
        raise CloudError("no history-key in Things Cloud account response")
    return str(key)


class Client:
    """Requests against one account history."""

    def __init__(self, session: Session):
        self.session = session

    @classmethod
    def connect(cls) -> "Client":
        """Session file if present, else authenticate from env credentials
        (persisting the session for next time)."""
        session = load_session()
        if session is None:
            creds = env_credentials()
            if creds is None:
                raise CloudError(
                    f"No Things Cloud auth. Set {ENV_EMAIL} + {ENV_PASSWORD} "
                    f"(e.g. via direnv in ~/.envrc), or run `things auth`."
                )
            email, password = creds
            session = Session(email=email, history_key=fetch_history_key(email, password))
            save_session(session)
        return cls(session)

    # ---------- history reads ----------

    def history_status(self) -> dict[str, Any]:
        """``{"latest-server-index": N, "is-empty": bool, …}``"""
        url = f"{BASE_URL}/history/{self.session.history_key}"
        return _request("GET", url)

    def items_page(self, start_index: int) -> dict[str, Any]:
        url = (
            f"{BASE_URL}/history/{self.session.history_key}/items"
            f"?start-index={int(start_index)}"
        )
        return _request("GET", url)

    def pull_items(self, start_index: int = 0) -> tuple[list[dict[str, Any]], int]:
        """All commit maps from ``start_index`` to the head.

        Returns ``(commits, head_index)`` where each commit is one
        ``{uuid: {"t":…,"e":…,"p":…}, …}`` map. Pagination advances via the
        server's ``current-item-index`` — computing it client-side from item
        counts triggers HTTP 500s (per things-cloud-sdk bug 7).
        """
        commits: list[dict[str, Any]] = []
        head = start_index
        for _ in range(10_000):  # backstop, never hit in practice
            page = self.items_page(head)
            items = page.get("items") or []
            commits.extend(items)
            head = int(page.get("current-item-index") or head)
            end = int(page.get("end-total-content-size") or 0)
            latest = int(page.get("latest-total-content-size") or 0)
            if end >= latest or not items:
                return commits, head
        raise CloudError("items pagination did not terminate")

    # ---------- writes ----------

    def commit(self, changes: dict[str, dict[str, Any]], ancestor_index: int) -> int:
        """POST one commit; returns the new server head index.

        ``changes`` is ``{uuid: {"t":…,"e":…,"p":…}}``. ``ancestor_index``
        must be the head index the caller last saw.
        """
        if not changes:
            raise CloudError("empty commit")
        url = (
            f"{BASE_URL}/history/{self.session.history_key}/commit"
            f"?ancestor-index={int(ancestor_index)}&_cnt=1"
        )
        result = _request(
            "POST", url, body=changes, extra_headers={"Push-Priority": "10"}
        )
        return int(result.get("server-head-index") or ancestor_index)
