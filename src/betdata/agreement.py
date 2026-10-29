"""Spread flags need an agreeing signal (owner decision 2026-10-29, MODEL.md "Spread flags: the
agreement rule"; NFL and NBA). A threshold spread pick is flagged only when, at pick time:

  (a) move      the DK/FD line moved toward the picked side since the book's first stored
                line (the pick's book; the other book when the pick's book has no earlier quote);
  (b) Pinnacle  Pinnacle's no-vig line sides with the model against the DK/FD line: carried to
                the DK/FD number of the side (normal, sigma per sport), Pinnacle's no-vig chance
                that the side covers is above 50%.

Every threshold pick is still graded; `agree` on the grade records which signals held.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection

from betdata.moneyline import no_vig
from betdata.nfl.book_lines import PICK_BOOKS, Board, Quote

MOVE, PINNACLE = "move", "pinnacle"
AGREE_TEXT = (", and an independent signal agrees at pick time: the DraftKings/FanDuel line "
              "moved toward the side since its first quote, or Pinnacle's no-vig line sides with "
              "the model against it")
BOOK_NAMES = {"draftkings": "DK", "fanduel": "FD", "pinnacle": "Pinnacle"}


@dataclass(frozen=True)
class Agreement:
    signals: tuple[str, ...]  # subset of (MOVE, PINNACLE), in that order
    notes: tuple[str, ...]  # one plain-language line per signal that held

    def __bool__(self) -> bool:
        return bool(self.signals)

    @property
    def code(self) -> str:
        """Stored on the grade: "", "move", "pinnacle" or "move+pinnacle"."""
        return "+".join(self.signals)


NONE = Agreement((), ())


def _fmt(x: float) -> str:
    return "pk" if x == 0 else f"{x:+g}"


def _first(board: Board, game_id: int, book: str) -> Quote | None:
    rows = board._rows.get((game_id, "spreads", book, "home"))
    return rows[0][1] if rows else None


def moved(board: Board, game_id: int, side: str, at: datetime, team: str,
          book: str | None = None) -> str | None:  # fmt: skip
    """Signal (a): a note when the book's home line at `at` moved toward `side` from its first
    stored line, else None."""
    order = [book] + [b for b in PICK_BOOKS if b != book] if book else list(PICK_BOOKS)
    for b in order:
        now, first = board.quote(game_id, "spreads", b, "home", at), _first(board, game_id, b)
        if now is None or first is None or first.fetched_at >= now.fetched_at:
            continue
        toward = now.line < first.line if side == "home" else now.line > first.line
        if toward:
            sgn = 1 if side == "home" else -1
            return (f"{BOOK_NAMES[b]} line moved toward {team}: {team} {_fmt(sgn * first.line)}"
                    f" → {_fmt(sgn * now.line)}")
        return None  # the pick's book has a history and it did not move toward the side
    return None


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


def spread_agreement(board: Board, main: Connection, poller, sport: str, game_id: int,
                     side: str, at: datetime, team: str, book: str | None = None,
                     side_line: float | None = None) -> Agreement:  # fmt: skip
    """Both signals for a spread pick of `side` at `at`. `book`/`side_line`: the DK/FD book and
    number the pick was taken at (NFL); without them the best DK/FD number for the side then."""
    if side_line is None or book is None:
        q = board.best(game_id, "spreads", side, at)
        if q is None:
            return NONE
        book, side_line = q.book, q.line
    sig, notes = [], []
    if (n := moved(board, game_id, side, at, team, book)) is not None:
        sig.append(MOVE)
        notes.append(n)
    if (n := pinnacle_sides(main, poller, game_id, side, side_line, at, sport, team)) is not None:
        sig.append(PINNACLE)
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
