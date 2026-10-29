"""Independent votes on a model's spread pick (MODEL.md "M18 Part A"; NFL and NBA). Since M18
no model-vs-market pick is flagged (research only); these votes are recorded on the grade
(`agree`) and feed the consensus work. At pick time:

  Pinnacle  Pinnacle's no-vig line sides with the model against the DK/FD line: carried to the
            DK/FD number of the side (normal, sigma per sport), Pinnacle's no-vig chance that
            the side covers is above 50%.
  Kalshi    Kalshi's fresh (<= 10 min) yes-ask probability that the picked team wins beats the
            DK/FD no-vig moneyline probability for that team (mean of the books quoting it).

The M17-era "line moved toward the side since open" vote was dropped (Moskowitz 2021: such
moves overreact; live 0-6 for m13w).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, select

from betdata.moneyline import implied, no_vig
from betdata.nfl.book_lines import PICK_BOOKS, Board

PINNACLE, KALSHI = "pinnacle", "kalshi"
FRESH = timedelta(minutes=10)
AGREE_TEXT = (". Research only since M18: never flagged; the card notes whether Pinnacle's "
              "no-vig line or Kalshi sides with the pick")


@dataclass(frozen=True)
class Agreement:
    signals: tuple[str, ...]  # subset of (PINNACLE, KALSHI), in that order
    notes: tuple[str, ...]  # one plain-language line per signal that held

    def __bool__(self) -> bool:
        return bool(self.signals)

    @property
    def code(self) -> str:
        """Stored on the grade: "", "pinnacle", "kalshi" or "pinnacle+kalshi"."""
        return "+".join(self.signals)


NONE = Agreement((), ())


def _fmt(x: float) -> str:
    return "pk" if x == 0 else f"{x:+g}"


def pinnacle_sides(main: Connection, poller, game_id: int, side: str, side_line: float,
                   at: datetime, sport: str, team: str) -> str | None:  # fmt: skip
    """Signal (b): a note when Pinnacle's last quote before `at`, no-vig and carried to the
    DK/FD number `side_line` of the side, gives the side over 50% to cover; else None."""
    from types import SimpleNamespace

    from betdata.alerts.grade import _main_obs, _poller_obs, at_bet_line, latest_close, sigma

    a = SimpleNamespace(game_id=game_id, market="spreads", player_name=None)
    obs = _main_obs(main, a, "pinnacle", at)
    if poller is not None:
        obs += _poller_obs(poller, a, "pinnacle", at)
    c = latest_close(obs, side, "spreads", "pinnacle")
    if c is None or c.line is None or c.price is None or c.other_price is None:
        return None
    nv = no_vig(c.price, c.other_price)
    sig = sigma(sport, "spreads", None)
    if nv is None or sig is None:
        return None
    p = at_bet_line(nv[0], side_line - c.line, sig)
    if p <= 0.5:
        return None
    return (f"Pinnacle {team} {_fmt(c.line)} no-vig gives {team} {100 * p:.0f}% to cover "
            f"{_fmt(side_line)}")


def kalshi_sides(poller, board: Board, game_id: int, side: str, at: datetime,
                team: str) -> str | None:  # fmt: skip
    """Kalshi vote: a note when Kalshi's latest yes ask for the side, polled within FRESH before
    `at`, gives the team a higher win probability than DK/FD's no-vig moneyline; else None."""
    if poller is None:
        return None
    from betdata.alerts.db import quotes as q

    row = poller.execute(select(q.c.price, q.c.polled_at)
                         .where(q.c.game_id == game_id, q.c.book == "kalshi", q.c.market == "h2h",
                                q.c.outcome == side, q.c.polled_at <= at,
                                q.c.polled_at >= at - FRESH)
                         .order_by(q.c.polled_at.desc()).limit(1)).first()  # fmt: skip
    if row is None or row.price is None:
        return None
    ps = []
    for b in PICK_BOOKS:
        pr = board.pair(game_id, b, at)
        if pr is not None and (nv := no_vig(pr[0].price, pr[1].price)) is not None:
            ps.append(nv[0] if side == "home" else nv[1])
    if not ps:
        return None
    k, dkfd = implied(row.price), sum(ps) / len(ps)
    if k <= dkfd:
        return None
    return f"Kalshi gives {team} {100 * k:.0f}% to win vs DK/FD no-vig {100 * dkfd:.0f}%"


def spread_agreement(board: Board, main: Connection, poller, sport: str, game_id: int,
                     side: str, at: datetime, team: str, book: str | None = None,
                     side_line: float | None = None) -> Agreement:  # fmt: skip
    """Both votes for a spread pick of `side` at `at`. `book`/`side_line`: the DK/FD book and
    number the pick was taken at (NFL); without them the best DK/FD number for the side then."""
    if side_line is None or book is None:
        q = board.best(game_id, "spreads", side, at)
        if q is None:
            return NONE
        book, side_line = q.book, q.line
    sig, notes = [], []
    if (n := pinnacle_sides(main, poller, game_id, side, side_line, at, sport, team)) is not None:
        sig.append(PINNACLE)
        notes.append(n)
    if (n := kalshi_sides(poller, board, game_id, side, at, team)) is not None:
        sig.append(KALSHI)
        notes.append(n)
    return Agreement(tuple(sig), tuple(notes))


def card_agreement(card, board: Board, conn: Connection, poller, sport: str, now: datetime,
                   dkfd_line: bool) -> None:  # fmt: skip
    """Set `card.spread_agree` for a card with a threshold spread pick. `dkfd_line`: the card's
    market spread is already the DK/FD quote the pick is taken at (NFL); otherwise (NBA) the
    best DK/FD number for the side is used."""
    if not card.spread_pick:
        return
    side = "home" if card.spread_pick == card.home else "away"
    book = line = None
    q = card.market_spread
    if dkfd_line and q is not None and q.book in PICK_BOOKS:
        book, line = q.book, (q.line if side == "home" else -q.line)
    card.spread_agree = spread_agreement(board, conn, poller, sport, card.game_id, side, now,
                                         card.spread_pick, book, line)  # fmt: skip
