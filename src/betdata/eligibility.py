"""Card eligibility rules v2 (owner decision 2026-11-09, MODEL.md "Rules v2"; provisional: the
rules were chosen after seeing the live ledger through Nov 9).

No model refit, and no change to how candidates are created, stored or graded: every candidate
keeps being created and graded as before; these rules only decide what is eligible for the
cards, the parlay and news alerts. From the 6:15 AM ET update of RULES_V2_START.

- OG card: (a) assists only (rebound picks, other props and game markets are not eligible);
  (b) no unders on assist lines of 5.5 or higher; (c) no unders when 2 or more of the
  player's rotation teammates are Out or Doubtful at pick time.
- Next Gen card and news alerts: (e) player props: assists only (points, rebounds, threes and
  PRA are tracked only), with (b) and (c); game markets keep the EV bar and the long-shot gate.
- Parlay: legs follow (a)-(c) and (e): assist props and game markets only.

"Rotation teammates" are the key teammates of the review fields (betdata/review.py): players
of the same team averaging >= 24 minutes over their last 10 games, with their latest NBA
injury-report status known at the pick's creation time (the evidence used the same fields).

Every candidate a rule removes is stored with its reason as a ledger annotation (`filter`),
and still graded; the Filters table reports what each reason removed.
"""

from __future__ import annotations

from datetime import date, datetime

RULES_V2_START = date(2026, 11, 10)
# Rules v3 (owner 2026-11-12, MODEL.md "Rules v3"): from the Nov 13 card, rebound OVERS are
# eligible for the official OG card again; rebound unders stay out with their own reason. Only
# `alert_reason`'s OG branch changes; `reason` (Next Gen, the parlay, news) is untouched.
RULES_V3_START = date(2026, 11, 13)
REBOUND_UNDERS = "filtered: rebound unders"
REBOUNDS = "filtered: rebounds"
UNDER_ASSISTS = "filtered: under 5.5+ assists"
TEAMMATES = "filtered: teammates out"
NON_ASSIST = "filtered: non-assist prop"
NOT_ASSISTS_OG = "filtered: not assists (OG)"  # OG game markets: "assists only"
REASONS = (REBOUNDS, NON_ASSIST, NOT_ASSISTS_OG, UNDER_ASSISTS, TEAMMATES, REBOUND_UNDERS)
UNDER_LINE = 5.5
TEAMMATES_MAX = 2  # 2 or more key teammates Out / Doubtful: no unders
OG_SIGNALS = ("value", "snipe")
NEXTGEN_SIGNALS = ("engine", "flash")
GAME_MARKETS = ("spreads", "totals", "h2h")


def card_of(signal: str) -> str | None:
    if signal in OG_SIGNALS:
        return "og"
    if signal in NEXTGEN_SIGNALS:
        return "nextgen"
    return None


def reason(card: str, market: str, side: str, line: float | None,
           teammates_out: int | None) -> str | None:  # fmt: skip
    """The first rule a candidate fails for `card` ("og" or "nextgen"; the parlay uses
    "nextgen"), or None when eligible. `teammates_out`: key teammates Out/Doubtful at pick time
    (None: unknown, the rule cannot apply)."""
    if market in GAME_MARKETS:
        return NOT_ASSISTS_OG if card == "og" else None
    if market == "rebounds":
        return REBOUNDS
    if market != "assists":
        return NON_ASSIST
    if side == "under":
        if line is not None and line >= UNDER_LINE - 1e-9:
            return UNDER_ASSISTS
        if teammates_out is not None and teammates_out >= TEAMMATES_MAX:
            return TEAMMATES
    return None


def applies(tip: datetime | None) -> bool:
    """Rules v2 cover picks for games from RULES_V2_START (Eastern date)."""
    if tip is None:
        return False
    from betdata.alerts.rules import EASTERN

    return tip.astimezone(EASTERN).date() >= RULES_V2_START


def _norm(s: str) -> str:
    from betdata.review import _norm as n

    return n(s)


def teammates_out(m, game_id: int, team_id: int | None, player: str | None,
                  at: datetime) -> int | None:  # fmt: skip
    """Key teammates (>= 24 minutes over their last 10 games) listed Out or Doubtful on the
    NBA injury report known at `at`; None when unknown (no team, or no report yet)."""
    if m is None or not player:
        return None
    from betdata import review as R

    if team_id is None:
        team_id = R.nba_player_team(m, player, int(game_id), at)
    if team_id is None:
        return None
    keys = [n for n in R.nba_key_players(m, int(team_id), at) if _norm(n) != _norm(player)]
    st = R.nba_statuses(m, int(game_id), keys, at)
    if st is None:
        return 0 if keys else None
    return sum(v in ("Out", "Doubtful") for v in st.values())


def alert_reason(m, a) -> str | None:
    """The rules-v2 reason for an alert (a card candidate), or None when eligible or out of
    scope (another signal or sport, or a game before RULES_V2_START)."""
    card = card_of(a.signal)
    if card is None or a.sport != "nba" or not applies(a.tip):
        return None
    tm = None
    if a.player_name and a.market == "assists" and a.side == "under":
        from betdata.alerts.grade import utc

        tm = teammates_out(m, a.game_id, (a.details or {}).get("team_id"), a.player_name,
                           utc(a.created_at))  # fmt: skip
    why = reason(card, a.market, a.side, a.line, tm)
    if card == "og" and why == REBOUNDS and v3_applies(a.tip):  # Rules v3: overs back on OG
        return None if a.side == "over" else REBOUND_UNDERS
    return why


def v3_applies(tip: datetime | None) -> bool:
    """Rules v3 cover OG candidates for games from RULES_V3_START (Eastern date)."""
    if tip is None:
        return False
    from betdata.alerts.rules import EASTERN

    return tip.astimezone(EASTERN).date() >= RULES_V3_START


# --- the Filters table (owner 2026-11-09): what each rule removed, against the card ------------

REVIEW_N = 100  # graded removed picks before a filter is reviewed
REVIEW_RULE = (f"Review rule (fixed 2026-11-09): each filter is revisited once it has removed "
               f"{REVIEW_N} graded picks; dropping it is recommended if the picks it removed "
               "would have been profitable (positive units at a flat 1 unit). The owner decides.")
CARD_SIGNALS = {"og": ("F", "C-NBA"), "nextgen": ("nba-engine", "news-alert")}


def filters_table(rows: list[dict]) -> list[dict]:
    """Per card and filter reason, since RULES_V2_START: the removed picks' graded record (flat
    1 unit), win rate vs the break-even at their average odds, and the card's own record over
    the same period (card stakes); with the review status."""
    from betdata import cards as C
    from betdata import official as O

    since = RULES_V2_START.isoformat()
    out = []
    for card in ("nextgen", "og"):
        mine = [r for r in rows if r["sport"] == "nba" and r["signal_name"] in CARD_SIGNALS[card]
                and r["day"] >= since]  # fmt: skip
        card_rec = O.record([r for r in C.view(rows, card) if r["day"] >= since],
                            O.official_stake)  # fmt: skip
        for why in REASONS:
            removed = [r for r in mine if r.get("filter") == why]
            if not removed:
                continue
            rec = O.record(removed)
            g = O.goal(removed)  # its break-even at the removed picks' average odds
            d = rec.wins + rec.losses
            status = (f"review now: {'recommend dropping' if rec.units > 0 else 'keep'}"
                      if rec.graded >= REVIEW_N else f"review at {REVIEW_N} ({rec.graded} so far)")
            out.append({"card": C.name(card), "reason": why, "removed": len(removed),
                        "graded": rec.graded, "wlp": rec.wlp, "units": round(rec.units, 2),
                        "win": None if not d else round(rec.wins / d, 4),
                        "needed": None if g.break_even is None else round(g.break_even, 4),
                        "card_wlp": card_rec.wlp, "card_units": round(card_rec.units, 2),
                        "card_graded": card_rec.graded, "status": status})  # fmt: skip
    return out


def pct(x) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"
