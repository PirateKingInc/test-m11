"""Clear status language about a player, from a post (owner decision 2026-11-05): only "out",
"won't play", "ruled out" (-> Out) and "available", "starting" (-> Available) and close variants;
anything hedged ("questionable", "game-time decision", "expected to", "hopeful", "might") is not
clear and is ignored."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

OUT, AVAILABLE = "Out", "Available"

_OUT = [r"\bruled out\b", r"\bwon[’']?t play\b", r"\bwill not play\b", r"\bnot playing\b",
        r"\bis out\b", r"\bare out\b", r"\bout tonight\b", r"\bout (?:vs\.?|against|at|for)\b",
        r"\bwill miss\b", r"\bsidelined\b", r"\bout\b(?=[\s.,;:!)]*$)", r"\(\w+\) out\b",
        r"\bOUT\b"]  # fmt: skip
_AVAILABLE = [r"\bis available\b", r"\bavailable tonight\b", r"\bwill play\b", r"\bis playing\b",
              r"\bcleared to play\b", r"\bupgraded to available\b", r"\bwill start\b",
              r"\bis starting\b", r"\bstarting tonight\b", r"\bstarts tonight\b",
              r"\bin the starting lineup\b", r"\bwill return\b", r"\breturns tonight\b",
              r"\bavailable\b", r"\bstarting\b"]  # fmt: skip
_HEDGE = [r"\bquestionable\b", r"\bdoubtful\b", r"\bprobable\b", r"\bgame[- ]time\b",
          r"\bexpected to\b", r"\bhopeful\b", r"\bmight\b", r"\bmay\b", r"\blikely\b",
          r"\bunlikely\b", r"\bnot expected\b", r"\bpossible\b", r"\bwarm[- ]?ups?\b",
          r"\bnot starting\b", r"\bnot available\b", r"\bunavailable\b", r"\bif\b",
          r"\bwould\b", r"\btrending\b", r"\bplans? to\b"]  # fmt: skip


def _words(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", " ", s)
    return re.sub(r"[^a-z ]", " ", s).split()


def status_of(text: str) -> str | None:
    """Out / Available when the language is clear, else None. "Unavailable", "not available"
    and every hedge are not clear (a hedge with a clear out is still out only if the out phrase
    is unhedged: we take no chances and drop the post)."""
    t = text or ""
    low = t.lower()
    if any(re.search(h, low) for h in _HEDGE):
        return None
    out = any(re.search(p, t if p == r"\bOUT\b" else low) for p in _OUT)
    avail = any(re.search(p, low) for p in _AVAILABLE)
    if out == avail:  # neither, or both (a post about two players): not clear
        return None
    return OUT if out else AVAILABLE


@dataclass(frozen=True)
class Player:
    name: str
    team: str  # abbreviation
    game_id: int
    minutes: float | None = None


def match(text: str, players: list[Player]) -> list[Player]:
    """Players named in the post: the full name, or a last name unique among today's players
    when the team (abbreviation or nickname) is also named."""
    w = " " + " ".join(_words(text)) + " "
    hits = [p for p in players if " " + " ".join(_words(p.name)) + " " in w]
    if hits:
        return hits
    last: dict[str, list[Player]] = {}
    for p in players:
        parts = _words(p.name)
        if parts:
            last.setdefault(parts[-1], []).append(p)
    out = []
    for name, ps in last.items():
        if len(ps) == 1 and f" {name} " in w and _team_named(text, ps[0].team):
            out.append(ps[0])
    return out


NICK = {"ATL": "hawks", "BOS": "celtics", "BKN": "nets", "CHA": "hornets", "CHI": "bulls",
        "CLE": "cavaliers cavs", "DAL": "mavericks mavs", "DEN": "nuggets", "DET": "pistons",
        "GSW": "warriors dubs", "HOU": "rockets", "IND": "pacers", "LAC": "clippers",
        "LAL": "lakers", "MEM": "grizzlies grizz", "MIA": "heat", "MIL": "bucks",
        "MIN": "timberwolves wolves", "NOP": "pelicans pels", "NYK": "knicks", "OKC": "thunder",
        "ORL": "magic", "PHI": "76ers sixers", "PHX": "suns", "POR": "blazers trail",
        "SAC": "kings", "SAS": "spurs", "TOR": "raptors", "UTA": "jazz",
        "WAS": "wizards"}  # fmt: skip


def _team_named(text: str, team: str) -> bool:
    w = set(_words(text))
    return team.lower() in w or bool(w & set(NICK.get(team, "").split()))
