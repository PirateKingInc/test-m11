"""The early-warning sources (owner decision 2026-11-05).

- `Stream`: Bluesky's Jetstream (free, unauthenticated JSON over a WebSocket), filtered on the
  server to the watchlist's DIDs and to new posts. A background thread holds it open only while
  the poller asks (game-day windows) and hands each post to a queue the poller drains every
  minute; it reconnects with a back-off and resumes from its last cursor.
- `sleeper_spikes`: Sleeper's NBA trending adds (free, read-only, non-commercial), every few
  minutes in the same windows; a player whose adds jump (x3 and >= 25) is a hint only. The full
  player list (names, teams) is fetched at most once a day, as Sleeper asks.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Post:
    did: str
    text: str
    created_at: datetime
    uri: str


def parse_event(raw: str | bytes, dids: set[str]) -> tuple[Post | None, int | None]:
    """One Jetstream message -> (a new post by a watched account, the event's cursor)."""
    try:
        ev = json.loads(raw)
    except ValueError:
        return None, None
    cursor = ev.get("time_us")
    c = ev.get("commit") or {}
    if (ev.get("kind") != "commit" or c.get("operation") != "create"
            or c.get("collection") != "app.bsky.feed.post" or ev.get("did") not in dids):
        return None, cursor
    rec = c.get("record") or {}
    try:
        at = datetime.fromisoformat(rec.get("createdAt", "").replace("Z", "+00:00"))
    except ValueError:
        at = datetime.fromtimestamp((cursor or 0) / 1e6, UTC)
    uri = f"at://{ev['did']}/app.bsky.feed.post/{c.get('rkey', '')}"
    return Post(ev["did"], rec.get("text", ""), at.astimezone(UTC), uri), cursor


class Stream:
    """The Jetstream connection in a background thread (start/stop from the poller loop)."""

    def __init__(self, url: str, dids: list[str], connect=None):
        self.url, self.dids = url, sorted(set(dids))
        self.q: queue.Queue[Post] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cursor: int | None = None
        self.connect = connect  # injectable (tests); default websockets.sync.client.connect
        self.messages = self.reconnects = 0

    def address(self) -> str:
        q = [("wantedCollections", "app.bsky.feed.post")] + [("wantedDids", d)
                                                              for d in self.dids]  # fmt: skip
        if self.cursor:
            q.append(("cursor", str(self.cursor - 5_000_000)))  # replay 5 s on reconnect
        return f"{self.url}?{urlencode(q)}"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running or not self.dids:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jetstream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def drain(self) -> list[Post]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def _run(self) -> None:
        if self.connect is None:
            from websockets.sync.client import connect
        else:
            connect = self.connect
        wanted, backoff = set(self.dids), 1.0
        while not self._stop.is_set():
            try:
                with connect(self.address(), open_timeout=15, close_timeout=2) as ws:
                    backoff = 1.0
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        self.messages += 1
                        post, cur = parse_event(raw, wanted)
                        if cur:
                            self.cursor = cur
                        if post is not None:
                            self.q.put(post)
            except Exception as e:  # noqa: BLE001 - reconnect; the poller loop is never stopped
                if self._stop.is_set():
                    break
                self.reconnects += 1
                log.warning("jetstream: %s; reconnecting in %.0f s", type(e).__name__, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)


def resolve_dids(http, appview: str, handles: list[str]) -> dict[str, str]:
    """handle -> DID through the public AppView (once per poller job)."""
    out = {}
    for h in handles:
        try:
            r = http.get("bsky", f"{appview}/com.atproto.identity.resolveHandle",
                         params={"handle": h})  # fmt: skip
            if r.status_code == 200:
                out[h] = r.json()["did"]
        except Exception as e:  # noqa: BLE001
            log.warning("resolve %s: %s", h, e)
    return out


# --- Sleeper ----------------------------------------------------------------------------------


def sleeper_players(http, url: str, cached: dict | None, now: datetime) -> dict:
    """{player_id: [full name, team]}, refreshed at most once a day (Sleeper's guidance)."""
    if cached and now - datetime.fromisoformat(cached["at"]) < timedelta(hours=24):
        return cached
    r = http.get("sleeper", url)
    raw = r.json() if r.status_code == 200 else {}
    names = {pid: [p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}",
                   p.get("team")] for pid, p in raw.items()
             if p.get("team") and p.get("active", True)}  # fmt: skip
    return {"at": now.isoformat(), "players": names} if names else (cached or {})


def sleeper_spikes(rows: list[dict], previous: dict[str, int], spike: float,
                   min_adds: int) -> list[tuple[str, int]]:  # fmt: skip
    """(player_id, adds) whose adds reached `min_adds` and grew `spike`x since the last check
    (or first appear with at least `spike` x `min_adds`)."""
    out = []
    for r in rows:
        pid, n = str(r.get("player_id")), int(r.get("count") or 0)
        before = previous.get(pid)
        if n >= min_adds and ((before is None and n >= spike * min_adds)
                              or (before is not None and n >= spike * max(before, 1))):
            out.append((pid, n))
    return out


def monotonic() -> float:
    return time.monotonic()
