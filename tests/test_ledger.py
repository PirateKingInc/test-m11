"""The permanent pick ledger (betdata/ledger.py) and the weekly report (betdata/weekly.py):
- pick fields are written once and results once;
- the CSVs are append-only, both by construction and in their git history;
- voids stay in the ledger but out of W-L and ROI;
- a rebuilt poller database gets its picks back;
- milestones are announced once;
- the weekly report is due on Mondays after the morning run."""

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import insert, select

from betdata import ledger as L
from betdata import weekly as W
from betdata.alerts.db import alert_grades, alerts, consensus_grades, engine_for

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # Monday 10 AM ET


def _alert(p, key, signal="snipe", result=None, clv=None, tip=NOW - timedelta(days=1),
           player="A Player", market="rebounds", details=None, profit=None):  # fmt: skip
    aid = p.execute(insert(alerts).values(
        signal=signal, sport="nba", game_id=7, game_label="NYK @ BOS", tip=tip, market=market,
        player_name=player, side="over", line=6.5, price=110, book="draftkings",
        fair_prob=0.55, book_prob=0.46, reason="r  with\nspaces", dedup_key=key,
        created_at=tip - timedelta(hours=3), quiet=False, sent=True,
        details=details)).inserted_primary_key[0]  # fmt: skip
    if result is not None:
        p.execute(insert(alert_grades).values(alert_id=aid, result=result,
                                              profit=profit if profit is not None else
                                              (1.1 if result == "win" else -1.0
                                               if result == "loss" else 0.0),
                                              graded_at=NOW))  # fmt: skip
    if clv is not None:
        p.execute(insert(consensus_grades).values(alert_id=aid, clv=clv, close_prob=0.5,
                                                  close_method="median", graded_at=NOW))
    return aid


@pytest.fixture
def pdb(tmp_path):
    return engine_for(tmp_path / "poller.sqlite")


def test_append_rows_is_append_only(tmp_path):
    f = tmp_path / "x.csv"
    assert L.append_rows(f, ("pick_id", "v"), [{"pick_id": "a", "v": "1"}]) == 1
    before = f.read_text()
    # the same key with a different value is not rewritten; a new key is appended
    assert L.append_rows(f, ("pick_id", "v"), [{"pick_id": "a", "v": "CHANGED"},
                                               {"pick_id": "b", "v": "2"}]) == 1
    after = f.read_text()
    assert after.startswith(before) and "CHANGED" not in after and after.count("\n") == 3
    with pytest.raises(L.AppendOnlyError):
        L.append_rows(f, ("pick_id", "other"), [{"pick_id": "c", "other": "x"}])


def test_sync_writes_picks_once_and_results_once(pdb, tmp_path):
    out = tmp_path / "ledger"
    with pdb.begin() as p:
        a1 = _alert(p, "k1", result="win", clv=0.03)
        _alert(p, "k2")  # not final yet
        _alert(p, "k3", result="void", clv=None)
    with pdb.connect() as p:
        r = L.sync(p, None, out, NOW)
    assert (r["picks"], r["results"]) == (3, 2)
    picks = (out / "picks.csv").read_text()
    # a later change in the database never rewrites a pick; a new result is appended
    with pdb.begin() as p:
        p.execute(alerts.update().where(alerts.c.alert_id == a1).values(price=-500))
        aid2 = p.execute(select(alerts.c.alert_id).where(alerts.c.dedup_key == "k2")).scalar()
        p.execute(insert(alert_grades).values(alert_id=aid2, result="loss", profit=-1.0,
                                              graded_at=NOW))  # fmt: skip
    with pdb.connect() as p:
        r = L.sync(p, None, out, NOW + timedelta(hours=1))
    assert (r["picks"], r["results"]) == (0, 1)
    assert (out / "picks.csv").read_text() == picks
    rows = L.joined(out)
    assert [x["result"] for x in rows] == ["win", "loss", "void"]
    assert rows[0]["odds"] == "110" and rows[0]["reason"] == "r with spaces"
    assert rows[0]["pick_id"] == L.pick_id("k1") and rows[0]["signal_name"] == "F"
    rec = L.record(rows)  # the void is kept but not in W-L or ROI
    assert (rec.picks, rec.wins, rec.losses, rec.voids) == (3, 1, 1, 1)
    assert rec.units == pytest.approx(0.1) and rec.roi == pytest.approx(0.05)
    assert rec.clv_stats()[0] == 1


def test_reload_after_a_rebuilt_poller_database(pdb, tmp_path):
    out = tmp_path / "ledger"
    with pdb.begin() as p:
        _alert(p, "k1", result="win")
        _alert(p, "k2")
    with pdb.connect() as p:
        L.sync(p, None, out, NOW)
    fresh = engine_for(tmp_path / "rebuilt.sqlite")
    with fresh.begin() as p:
        assert L.reload_alerts(p, out) == 2
        assert L.reload_alerts(p, out) == 0  # idempotent
    with fresh.connect() as p:
        keys = sorted(p.execute(select(alerts.c.dedup_key)).scalars())
        # back in the ledger, nothing new: the reloaded picks keep their ids
        assert L.sync(p, None, out, NOW)["picks"] == 0
    assert keys == ["k1", "k2"]


def test_edge_source_backfill_for_a_stored_value_alert(pdb, tmp_path):
    # the first live C-NBA alert (CHA @ WAS): the model moved 0.1 points; the edge was FanDuel
    # vs the DK/FD median
    d = {"fair": 0.5411, "edge": 0.0454, "consensus_before": {"fair": -0.3922, "method": "median"},
         "margin_before": -5.78, "margin_after": -5.91}
    with pdb.begin() as p:
        p.execute(insert(alerts).values(
            signal="value", sport="nba", game_id=1, game_label="CHA @ WAS",
            tip=NOW + timedelta(days=1), market="spreads", side="away", line=1.5, price=-108,
            book="fanduel", fair_prob=0.5411, book_prob=0.4957, reason="r", dedup_key="v1",
            created_at=NOW, quiet=False, sent=True, details=d))  # fmt: skip
    with pdb.connect() as p:
        L.sync(p, None, tmp_path, NOW)
    [row] = L.read_rows(tmp_path / "picks.csv")
    assert row["edge_source"] == "book disagreement" and 0 < float(row["news_move"]) < 0.01


def test_milestones_are_announced_once(pdb, tmp_path):
    with pdb.begin() as p:
        for i in range(60):
            _alert(p, f"k{i}", result="win" if i % 2 else "loss")
    with pdb.connect() as p:
        r = L.sync(p, None, tmp_path, NOW)
        again = L.sync(p, None, tmp_path, NOW)
    assert [(m["signal_name"], m["graded"]) for m in r["milestones"]] == [("F", "60")]
    assert again["milestones"] == []


def test_ledger_history_is_append_only():
    """Every committed version of each ledger file starts with the previous one."""
    for name in ("picks.csv", "results.csv", "milestones.csv", "annotations.csv", "labels.csv"):
        rel = f"ledger/{name}"
        try:
            shas = subprocess.run(["git", "log", "--format=%H", "--reverse", "--", rel],
                                  cwd=ROOT, capture_output=True, text=True,
                                  check=True).stdout.split()  # fmt: skip
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("no git history")
        prev = ""
        for sha in shas:
            cur = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=ROOT,
                                 capture_output=True, text=True).stdout
            assert cur.startswith(prev), f"{rel} was rewritten at {sha}"
            prev = cur
        if (ROOT / rel).exists():
            assert (ROOT / rel).read_text().startswith(prev), f"{rel} rewritten in the tree"


def test_weekly_report_and_pushes(pdb, tmp_path):
    root = tmp_path
    with pdb.begin() as p:
        _alert(p, "k1", result="win", clv=0.03)
        _alert(p, "k2", signal="value", result="loss", clv=-0.01, market="spreads",
               player=None, details={"edge_source": "news", "news_move": 0.05, "edge": 0.06})
    with pdb.connect() as p:
        L.sync(p, None, root / "ledger", NOW)
    assert W.due(root, NOW)
    assert not W.due(root, NOW - timedelta(hours=4))  # 6 AM ET: before the morning run
    assert not W.due(root, NOW + timedelta(days=1))  # Tuesday
    with pdb.connect() as p:
        md, summary = W.build(root, NOW, None, p)
    assert "## All-time" in md and "| F |" in md and "| C-NBA |" in md
    assert "news" in md and "## System health" in md and "too few to judge" in md
    assert len(summary) == 5 and summary[0].startswith("Weekly 2026-11-02: 2 new picks")
    posts = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: posts.append((str(r.url), r.headers["title"], r.content)) or
        httpx.Response(200)))  # fmt: skip
    assert W.ntfy("topic-x", "Weekly picks report", summary, client) == "http 200"
    assert posts[0][0].endswith("/topic-x") and posts[0][2].count(b"\n") == 4
    assert W.ntfy(None, "t", summary) == "no NTFY_TOPIC"


def test_page_record(pdb, tmp_path):
    from betdata.page.value import record_html

    with pdb.begin() as p:
        _alert(p, "k1", result="win", clv=0.03)
    with pdb.connect() as p:
        L.sync(p, None, tmp_path, NOW)
    html = record_html(L.joined(tmp_path))
    assert "All-time record" in html and "Download all picks (CSV)" in html
    assert "too few to judge" in html and "+3.00 pp" in html and "1-0-0" in html
    assert "By signal" in html and "Props vs game markets" in html and "By sport" in html


def test_m16_legacy_and_duplicates_stay_in_the_csv_but_out_of_the_record(pdb, tmp_path):
    from betdata.page.value import record_html

    out = tmp_path / "ledger"
    # picks written before the rename: the M16 rows read "news-nba" and stay as written
    keys = {"news:22853:spreads:away:Injury-Report_2026-10-25_06_45PM.pdf": "win",
            "news:22853:spreads:away:Injury-Report_2026-10-25_07_00PM.pdf": "win"}
    assert L.pick_id(list(keys)[1]) in L.M16_DUPLICATES
    with pdb.begin() as p:
        for k, res in keys.items():
            _alert(p, k, signal="news", result=res, clv=0.02, market="spreads", player=None)
        _alert(p, "k1", result="loss", clv=-0.01)
    L.append_rows(out / "picks.csv", L.PICK_FIELDS, [
        {**r, "signal_name": "news-nba"} if r["signal"] == "news" else r for r in _picks(pdb)])
    picks = (out / "picks.csv").read_text()
    with pdb.connect() as p:
        L.sync(p, None, out, NOW)
        L.sync(p, None, out, NOW)  # annotations are written once
    assert (out / "picks.csv").read_text() == picks  # never rewritten
    notes = L.read_rows(out / "annotations.csv")
    assert sorted((n["field"], n["value"]) for n in notes) == [
        ("duplicate", "true"), ("signal_name", "m16-legacy"), ("signal_name", "m16-legacy")]
    rows = L.joined(out)
    assert [(r["signal_name"], r["duplicate"]) for r in rows] == [
        ("m16-legacy", ""), ("m16-legacy", "true"), ("F", "")]
    assert [r["signal_name"] for r in L.counted(rows)] == ["F"]
    html = record_html(rows)
    assert "Excludes 2 M16 legacy alerts (pre-M18 rules), available in the CSV." in html
    assert "m16-legacy" not in html and "0-1-0" in html and "1-0-0" not in html
    md, summary = W.build(tmp_path, NOW, None, None)
    assert "m16-legacy" not in md and "Excludes 2 M16 legacy alerts" in md
    assert summary[1].startswith("All-time 0-1-0")
    # a new M16 alert is named m16-legacy when written
    with pdb.begin() as p:
        _alert(p, "news:new", signal="news", market="spreads", player=None)
    with pdb.connect() as p:
        L.sync(p, None, out, NOW)
    assert L.read_rows(out / "picks.csv")[-1]["signal_name"] == "m16-legacy"


def _picks(pdb):
    with pdb.connect() as p:
        return [L.pick_row(p, None, a) for a in p.execute(select(alerts))]


def _lab(label, stake):
    return {"label": {"label": label, "stake": stake, "edge": 0.07, "threshold": 0.03,
                      "edge_source": "news", "pinnacle_sides": None, "available": True}}


def test_labels_and_stakes_are_written_once(pdb, tmp_path):
    """Labels are fixed when the pick is created: a later change in the database never
    rewrites them, and picks from before labels shipped get none (no backfill)."""
    out = tmp_path / "ledger"
    with pdb.begin() as p:
        take = _alert(p, "k1", result="win", details=_lab("TAKE", 1.0), profit=0.8)
        _alert(p, "k2", result="loss", details=_lab("LEAN", 0.5))
        _alert(p, "k0", result="win")  # before labels: unlabelled
    with pdb.connect() as p:
        L.sync(p, None, out, NOW)
    first = (out / "labels.csv").read_text()
    assert [(r["label"], r["stake"]) for r in L.read_rows(out / "labels.csv")] == [
        ("TAKE", "1.0"), ("LEAN", "0.5")]
    with pdb.begin() as p:  # the stored label changes later: the ledger keeps the first
        p.execute(alerts.update().where(alerts.c.alert_id == take)
                  .values(details=_lab("LEAN", 0.5)))
    with pdb.connect() as p:
        L.sync(p, None, out, NOW + timedelta(hours=1))
    assert (out / "labels.csv").read_text() == first
    with pytest.raises(L.AppendOnlyError):  # a new column would rewrite the header
        L.append_rows(out / "labels.csv", (*L.LABEL_FIELDS, "x"), [{"pick_id": "z"}])
    rows = {r["dedup_key"]: r for r in L.joined(out)}
    assert (rows["k1"]["label"], rows["k1"]["stake"]) == ("TAKE", "1.0")
    assert (rows["k0"]["label"], rows["k0"]["stake"]) == ("", "")
    # flat stakes from 100 units: TAKE won 0.8 at 1 unit, LEAN lost 1 at 0.5 unit
    b = L.bank(list(rows.values()))
    assert (b.picks, b.wins, b.losses) == (2, 1, 1)
    assert b.units == pytest.approx(0.3) and b.bankroll == pytest.approx(100.3)
    assert b.roi == pytest.approx(0.3 / 1.5)


def test_page_bankroll_and_badges(pdb, tmp_path):
    from betdata.page.value import bank_html, label_badge

    with pdb.begin() as p:
        _alert(p, "k1", result="win", details=_lab("TAKE", 1.0), profit=0.8)
        _alert(p, "k2", details=_lab("LEAN", 0.5))
    with pdb.connect() as p:
        L.sync(p, None, tmp_path, NOW)
    html = bank_html(L.joined(tmp_path), "NBA")
    assert "Simulated bankroll · NBA" in html and "<b>100.80u</b>" in html and "+0.80u" in html
    assert "ROI +80.0%" in html and "2 labelled picks (1-0-0)" in html
    assert "<span class='lbl take'>TAKE 1u</span>" in html and "LEAN 0.5u" in html
    assert label_badge(None, None) == ""
