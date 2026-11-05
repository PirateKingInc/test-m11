"""`betdata news accounts`: find and verify each watchlist account on Bluesky (public AppView,
no auth): it exists, it is the right person or team (display name, follower count, Bluesky's
verification badge where present) and it is active (a post within `active_days`). Runs where the
network reaches Bluesky (a GitHub runner); its JSON becomes config/news_accounts.json, and the
kept and dropped accounts go in PROJECT.md "News sources"."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from betdata.news import config as NC

MIN_FOLLOWERS = {"national": 1000, "beat": 300, "official": 1000}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _verified(p: dict) -> bool:
    v = (p.get("verification") or {})
    return v.get("verifiedStatus") == "valid" or v.get("trustedVerifierStatus") == "valid"


def best(name: str, candidates: list[dict]) -> dict | None:
    """The candidate whose display name matches the name, verified first, then followers."""
    want = _norm(name)
    ok = [c for c in candidates if _norm(c.get("displayName", "")) == want
          or _norm(c.get("displayName", "")).startswith(want)]  # fmt: skip
    if not ok:
        return None
    return sorted(ok, key=lambda c: (not _verified(c), -(c.get("followersCount") or 0)))[0]


def check(http, now: datetime | None = None) -> dict:
    """Search, then check each watchlist entry; returns the account file's content."""
    cfg = NC.load()["bluesky"]
    base, days = cfg["appview"], cfg.get("active_days", 30)
    now = now or datetime.now(UTC)
    out = []
    for e in NC.watch_names():
        row = {"name": e["name"], "group": e["group"], "team": e.get("team"), "keep": False}
        try:
            r = http.get("bsky", f"{base}/app.bsky.actor.searchActors",
                         params={"q": e["name"], "limit": 15})  # fmt: skip
            cand = best(e["name"], r.json().get("actors", []))
            if cand is None:
                row["why"] = "no account found"
                out.append(row)
                continue
            prof = http.get("bsky", f"{base}/app.bsky.actor.getProfile",
                            params={"actor": cand["did"]}).json()  # fmt: skip
            feed = http.get("bsky", f"{base}/app.bsky.feed.getAuthorFeed",
                            params={"actor": cand["did"], "limit": 5,
                                    "filter": "posts_no_replies"}).json()  # fmt: skip
            last = max((x["post"].get("indexedAt", "") for x in feed.get("feed", [])),
                       default="")  # fmt: skip
            followers = prof.get("followersCount") or 0
            row.update(handle=prof.get("handle"), did=prof.get("did"),
                       display=prof.get("displayName"), followers=followers,
                       posts=prof.get("postsCount"), verified=_verified(prof),
                       last_post=last)  # fmt: skip
            recent = bool(last) and (now - datetime.fromisoformat(last.replace("Z", "+00:00"))
                                     <= timedelta(days=days))  # fmt: skip
            big = followers >= MIN_FOLLOWERS[e["group"]] or row["verified"]
            # an official account must carry Bluesky's verification (fan and impostor
            # accounts take team names)
            real = row["verified"] or e["group"] != "official"
            row["keep"] = recent and big and real
            row["why"] = ("ok" if row["keep"] else
                          f"not active (no post in {days} days)" if not recent
                          else f"too few followers ({followers})" if not big
                          else "not verified by Bluesky (official accounts must be: "
                               "impostor/fan risk)")  # fmt: skip
        except Exception as ex:  # noqa: BLE001 - one account never stops the check
            row["why"] = f"error {type(ex).__name__}"
        out.append(row)
    return {"checked_at": now.isoformat(), "active_days": days, "accounts": out}
