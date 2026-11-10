"""The Spread Picks review at 50 graded picks and the quiet comparison (owner 2026-11-10)."""

import csv
from datetime import UTC, datetime

from betdata import spread_review as SR
from betdata.ledger import CARD_FIELDS, PICK_FIELDS, RESULT_FIELDS, milestones, read_rows
from betdata.spread import SPREAD_FIELDS
from betdata.weekly import milestone_lines

NOW = datetime(2026, 12, 20, 12, tzinfo=UTC)
EG = ("key", "game_id", "game", "tip", "kind", "market", "player", "side", "line", "book",
      "p_engine", "p_close", "result", "y", "snapshot_at", "graded_at", "version")


def _w(path, fields, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def _ledger(out, n):
    picks, results, spread, card = [], [], [], []
    for i in range(n):
        ml, sp = f"ml{i}", f"sp{i}"
        base = {"sport": "nba", "game_id": str(100 + i), "game": "CLE @ CHA",
                "tip": "2026-11-20T00:00:00+00:00", "created_at": "2026-11-19T11:15:00+00:00",
                "player": "", "side": "home", "book": "draftkings", "reason": "r"}
        picks += [{**base, "pick_id": ml, "dedup_key": ml, "market": "h2h", "odds": "250",
                   "signal": "engine", "signal_name": "nba-engine"},
                  {**base, "pick_id": sp, "dedup_key": sp, "market": "spreads", "line": "7.5",
                   "odds": "-110", "signal": "dogsprd", "signal_name": "dog-spread"}]
        win = i % 2 == 0
        results += [{"pick_id": ml, "result": "loss", "profit": "-1.0"},
                    {"pick_id": sp, "result": "win" if win else "loss",
                     "profit": "0.9091" if win else "-1.0"}]
        card.append({"pick_id": ml, "sport": "nba", "card_date": "2026-11-19", "card": "yes",
                     "card_label": "LEAN", "card_stake": "0.5"})
        spread.append({"pick_id": sp, "sport": "nba", "card_date": "2026-11-19",
                       "spread": "yes", "spread_label": "SPREAD", "spread_stake": "1.0",
                       "trigger_pick_id": ml, "backfill": "false"})
    _w(out / "picks.csv", PICK_FIELDS, picks)
    _w(out / "results.csv", RESULT_FIELDS, results)
    _w(out / "card.csv", CARD_FIELDS, card)
    _w(out / "spread_card.csv", SPREAD_FIELDS, spread)


def test_milestone_at_50_once_with_one_ntfy_line(tmp_path):
    _ledger(tmp_path, 49)
    assert [m for m in milestones(tmp_path, NOW) if m["signal_name"] == SR.MILESTONE] == []
    _ledger(tmp_path, 50)
    got = [m for m in milestones(tmp_path, NOW) if m["signal_name"] == SR.MILESTONE]
    assert got == [{"signal_name": "spread-card", "graded": "50",
                    "reached_at": "2026-12-20T12:00:00+00:00"}]
    assert not [m for m in milestones(tmp_path, NOW) if m["signal_name"] == SR.MILESTONE]
    root = tmp_path.parent / "root"
    (root / "ledger").mkdir(parents=True)
    _ledger(root / "ledger", 50)
    (line,) = milestone_lines(got, root)
    assert line.startswith("Spread Picks reached 50 graded picks: 25-25-0")
    assert "nothing turns off" in line


def test_report_contents(tmp_path):
    root = tmp_path
    (root / "ledger").mkdir()
    _ledger(root / "ledger", 4)
    md = SR.report(root / "ledger", NOW)
    assert "Graded: 4 (2-2-0)" in md and "Average line: +7.5" in md
    assert "ATS 2-2 vs ML 0-4 on the same picks." in md
    assert "| 2026-11-19 | CLE @ CHA | CHA +7.5 | -110 | win | loss |" in md
    assert "Quiet comparison" in md and "Cover margin distribution" in md
    assert SR.write(root, NOW).read_text() == md


def test_quiet_comparison_from_engine_grades_not_on_the_card(tmp_path):
    def g(gid, market, side, line, pe, pc, res):
        return {"key": f"{gid}:{market}:", "game_id": gid, "game": "DEN @ HOU",
                "tip": "2026-11-10T01:30:00+00:00", "kind": "game", "market": market,
                "side": side, "line": line, "p_engine": pe, "p_close": pc, "result": res}

    _w(tmp_path / "engine_grades.csv", EG, [
        g("1", "h2h", "home", "", "0.52", "0.63", "win"),  # engine likes DEN (away dog)
        g("1", "spreads", "home", "-4.5", "0.41", "0.51", "win"),  # HOU covered: DEN lost
        g("2", "h2h", "home", "", "0.40", "0.30", "loss"),  # likes the home dog, but on card
        g("2", "spreads", "home", "7.5", "0.6", "0.5", "win"),
        g("3", "h2h", "home", "", "0.20", "0.30", "loss"),  # sides with the favourite: out
    ])  # fmt: skip
    _w(tmp_path / "picks.csv", PICK_FIELDS, [{"pick_id": "x", "dedup_key": "x",
                                              "game_id": "2", "market": "h2h", "side": "home",
                                              "signal": "engine"}])  # fmt: skip
    _w(tmp_path / "card.csv", CARD_FIELDS, [{"pick_id": "x", "card": "yes"}])
    (r,) = SR.quiet_comparison(tmp_path)
    assert (r["game_id"], r["team"], r["ml"], r["ats"], r["line"]) == ("1", "DEN", "loss",
                                                                       "loss", 4.5)
    assert read_rows(tmp_path / "picks.csv")  # never writes a pick
