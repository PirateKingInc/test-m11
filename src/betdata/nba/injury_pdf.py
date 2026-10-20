"""The official NBA injury report PDF, as text lines with positions, and those lines as rows.

The report is a table: Game Date, Game Time, Matchup, Team, Player Name, Current Status, Reason
(2018-21 reports also have Category and Previous Status). Date, time, matchup and team are only
printed on the first row they apply to and carry down, across pages too. Each page repeats the
header, which gives the columns' x positions; a row is a Player Name cell, and its status and
reason are the cells at the same height in their columns (a reason can wrap onto two lines).

`pdf_lines` is the only step that reads the PDF (pdfminer.six); its output is what gets archived
and what `parse_lines` turns into rows, so parsing is replayable without the PDF.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime

from betdata.core.errors import ParseError

STATUSES = ("Out", "Doubtful", "Questionable", "Probable", "Available")
HEADERS = {"Game Date": "date", "Game Time": "time", "Matchup": "matchup", "Team": "team",
           "Player Name": "player", "Category": "category", "Reason": "reason",
           "Current Status": "status", "Previous Status": "previous"}  # fmt: skip
TITLE = re.compile(r"Injury Report:\s*(\d{2}/\d{2}/\d{2})\s+(\d{1,2}:\d{2})\s*([AP]M)")
DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
MATCHUP = re.compile(r"^[A-Z]{2,4}@[A-Z]{2,4}$")
ROW_TOL = 6.0  # points: cells of one row sit within this of the player's line
WRAP = 12.0  # a wrapped reason is centred on its row: its lines sit up to this far away
NOT_SUBMITTED = "NOT YET SUBMITTED"
TEAMS = {
    "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets", "Chicago Bulls",
    "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets", "Detroit Pistons",
    "Golden State Warriors", "Houston Rockets", "Indiana Pacers", "LA Clippers",
    "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies", "Miami Heat",
    "Milwaukee Bucks", "Minnesota Timberwolves", "New Orleans Pelicans", "New York Knicks",
    "Oklahoma City Thunder", "Orlando Magic", "Philadelphia 76ers", "Phoenix Suns",
    "Portland Trail Blazers", "Sacramento Kings", "San Antonio Spurs", "Toronto Raptors",
    "Utah Jazz", "Washington Wizards",
}


def _merge_wrapped_teams(cells: list) -> list:
    """A long team name can wrap onto two lines of the Team column ("Minnesota" over
    "Timberwolves"); join such pairs into one cell at the upper line's height."""
    teams = sorted((c for c in cells if c[3] == "team"), key=lambda c: -c[1])
    drop, out = set(), []
    for i, c in enumerate(teams):
        if id(c) in drop or c[2] in TEAMS:
            continue
        for d in teams[i + 1:i + 3]:
            if c[1] - d[1] <= 2 * WRAP and f"{c[2]} {d[2]}" in TEAMS:
                out.append((c[0], c[1], f"{c[2]} {d[2]}", "team"))
                drop.update((id(c), id(d)))
                break
    return [c for c in cells if id(c) not in drop] + out


def pdf_lines(pdf: bytes) -> list[list]:
    """[page, x0, y, text] for every text line of the PDF (y grows upwards, per page)."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTTextContainer, LTTextLine

    out = []
    try:
        pages = extract_pages(io.BytesIO(pdf), laparams=LAParams(char_margin=1.0,
                                                                  line_margin=0.2,
                                                                  word_margin=0.1))  # fmt: skip
        for n, page in enumerate(pages):
            for el in page:
                if not isinstance(el, LTTextContainer):
                    continue
                for line in el:
                    if isinstance(line, LTTextLine):
                        text = " ".join(line.get_text().split())
                        if text:
                            out.append([n, round(line.x0, 1), round(line.y1, 1), text])
    except Exception as e:  # pdfminer raises many types for a damaged file
        raise ParseError(f"injury report: not a readable PDF: {e}") from e
    return out


@dataclass
class Report:
    report_at: datetime | None  # stated time, US Eastern, naive
    rows: list[dict] = field(default_factory=list)
    # (game_date, matchup, team_name) of every team on the report, and whether it submitted
    teams: dict[tuple[str, str, str], bool] = field(default_factory=dict)


def _columns(items) -> dict[str, float] | None:
    cols = {HEADERS[t]: x for x, _, t in items if t in HEADERS}
    return cols if {"player", "status", "team", "matchup"} <= set(cols) else None


def _column(x: float, cols: dict[str, float]) -> str | None:
    best = None
    for name, start in cols.items():
        if x >= start - 3 and (best is None or start > cols[best]):
            best = name
    return best


def parse_lines(lines: list[list]) -> Report:
    report = Report(None)
    for _, _, _, text in lines:
        m = TITLE.search(text)
        if m:
            report.report_at = datetime.strptime(" ".join(m.groups()), "%m/%d/%y %I:%M %p")
            break
    carry = {"date": None, "time": None, "matchup": None, "team": None}
    cols = None
    for page in sorted({ln[0] for ln in lines}):
        items = sorted(((x, y, t) for p, x, y, t in lines if p == page), key=lambda i: (-i[1],
                                                                                        i[0]))
        header_y = None
        for y in sorted({i[1] for i in items}, reverse=True):
            found = _columns([i for i in items if abs(i[1] - y) < 1.0])
            if found:
                cols, header_y = found, y
                break
        if cols is None:
            continue
        body = [i for i in items if header_y is None or i[1] < header_y - 1.0]
        cells = [(x, y, t, _column(x, cols)) for x, y, t in body if not TITLE.search(t)]
        cells = _merge_wrapped_teams(cells)
        players = [c for c in cells if c[3] == "player" and not c[2].startswith("Page ")]
        # Carried cells (date, time, matchup, team) apply from their line downwards.
        carried = sorted((c for c in cells if c[3] in carry), key=lambda c: -c[1])
        for i, (_, y, name, _) in enumerate(players):
            for _x, cy, t, col in carried:
                if cy >= y - ROW_TOL:
                    if col == "date" and DATE.match(t):
                        carry["date"] = t
                    elif col == "matchup" and MATCHUP.match(t):
                        carry["matchup"] = t
                    elif col in ("time", "team"):
                        carry[col] = t
            carried = [c for c in carried if c[1] < y - ROW_TOL]
            above = players[i - 1][1] if i else None
            below = players[i + 1][1] if i + 1 < len(players) else None
            top = y + WRAP if above is None else (y + above) / 2
            bottom = y - WRAP if below is None else (y + below) / 2
            status = [t for _, cy, t, col in cells if col == "status" and abs(cy - y) <= ROW_TOL
                      and t in STATUSES]  # fmt: skip
            reason = [t for _, cy, t, col in sorted(cells, key=lambda c: (c[3] != "category",
                                                                          -c[1]))
                      if col in ("reason", "category") and bottom < cy <= top]  # fmt: skip
            if not status or not carry["date"] or not carry["matchup"] or not carry["team"]:
                continue
            key = (_iso(carry["date"]), carry["matchup"], carry["team"])
            report.teams[key] = True
            report.rows.append(dict(game_date=key[0], matchup=key[1], team_name=key[2],
                                    player_name=name, status=status[0],
                                    reason=" ".join(t for t in reason if t != "-")[:256] or None,
                                    tip=carry["time"]))  # fmt: skip
        # Teams listed as not yet submitted (no player rows).
        for _x, y, t, _col in cells:
            if NOT_SUBMITTED in t.upper():
                team = [c for c in cells if c[3] == "team" and abs(c[1] - y) <= ROW_TOL]
                mu = [c for c in cells if c[3] == "matchup" and c[1] >= y - ROW_TOL]
                if team and (mu or carry["matchup"]) and carry["date"]:
                    key = (_iso(carry["date"]), (mu[-1][2] if mu else carry["matchup"]),
                           team[0][2])  # fmt: skip
                    report.teams.setdefault(key, False)
    if report.report_at is None:
        raise ParseError("injury report: no 'Injury Report: <date> <time>' title found")
    return report


def _iso(us_date: str) -> str:
    m, d, y = us_date.split("/")
    return f"{y}-{m}-{d}"
