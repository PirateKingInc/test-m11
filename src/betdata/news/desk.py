"""The news desk in the poller loop (owner decision 2026-11-05, MODEL.md "News alerts").

Each poller minute in a game-day injury-report window (`alerts/nba.py active`):
1. the Bluesky stream (Jetstream, the verified watchlist) is held open; new posts are drained;
2. a post with clear status language ("out", "won't play", "ruled out" / "available",
   "starting") about exactly one rotation player (Next Gen projects >= 15 minutes) is a claim,
   logged always; it counts only if the official report does not show it yet ("ahead");
3. an ahead claim reprices the game and the player's teammates' props with Next Gen (the player
   set Out / Available) at fresh DK/FD prices; a candidate that clears Next Gen's own bar
   (EV >= theta) becomes a news alert: "News alert — not a card pick", with minutes since the
   news, the "good down to" price and "Unconfirmed: per @source";
4. every few minutes, Sleeper's trending adds: a spike is a hint only, logged and used to
   corroborate a claim (never to alert);
5. pending claims are settled against the official report (confirmed / contradicted /
   unconfirmed at tip), and their alerts marked so.

Pushes go out only when the M16 lag study's decision allows it (`alerts_on`); logging always
runs. News alerts are their own signal (`flash`, research name `news-alert`), never on a
card.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from betdata.news import claims as NC_
from betdata.news import config as NC
from betdata.news.parse import OUT, Player, match, status_of

log = logging.getLogger(__name__)
SIGNAL = "flash"  # research name `news-alert` (alerts/gates.py); 8 characters at most
TITLE = "News alert — not a card pick"
TITLE_HEADER = "News alert - not a card pick"  # HTTP headers are latin-1: no em dash
CORROBORATE = timedelta(minutes=60)


def alerts_on(p) -> bool:
    """News alert pushes follow the M16 lag study (owner 2026-11-05): on only once its decision
    is taken and the pooled DK+FD median lag is at least 5 minutes; before the decision, and
    under 5 minutes, alerts stay off (no pushes) and all logging keeps running."""
    from betdata.alerts.db import get_state
    from betdata.alerts.rules import LAG_RULE_MIN

    d = get_state(p, "signal1:decision")
    if not d:
        return False
    med = d.get("soft_median_min")
    return med is not None and med >= LAG_RULE_MIN


def _abbr(label: str, is_home: bool) -> str:
    aw, _, hm = (label or "? @ ?").partition(" @ ")
    return hm if is_home else aw


def rotation(doc: dict | None, now: datetime, minutes: float) -> tuple[list[Player], dict]:
    """Rotation players of upcoming games in the live document, and their engine rows."""
    out, rows = [], {}
    for g in (doc or {}).get("games", []):
        if datetime.fromisoformat(g["tip"]) <= now:
            continue
        for pl in g.get("players", []):
            if not pl.get("name") or (pl.get("minutes") or 0) < minutes:
                continue
            p = Player(pl["name"], _abbr(g.get("label", ""), pl.get("is_home")),
                       int(g["game_id"]), pl.get("minutes"))  # fmt: skip
            out.append(p)
            rows[(p.game_id, p.name)] = {**pl, "tip": g["tip"], "label": g.get("label")}
    return out, rows


def alert_text(a, d: dict, now: datetime) -> str:
    """The push body (and the alert's reason): game, pick, line, odds, book, minutes since the
    news, the "good down to" price, and the source when ahead of the official report."""
    from betdata.alerts.notify import BOOKS, _fmt_price, selection
    from betdata.alerts.rules import EASTERN

    when = a.tip.astimezone(EASTERN).strftime("%-I:%M %p ET")
    lines = [TITLE,
             f"{a.game_label} {when}: {selection(a)} {_fmt_price(a.price)} at "
             f"{BOOKS.get(a.book, a.book)}",
             f"Good down to {_fmt_price(d['good_down_to'])} · {d['minutes_since']:.0f} min since "
             "the news"]  # fmt: skip
    if d.get("unconfirmed"):
        lines.append(f"Unconfirmed: per @{d['account']}")
    lines.append(f"News: {d['player_news']} {d['status_news'].lower()} (Next Gen reprice: fair "
                 f"{100 * a.fair_prob:.1f}%, EV {100 * d['ev']:+.1f}%)")
    return "\n".join(lines)


class NewsDesk:
    def __init__(self, poller, stream=None):
        self.poller = poller
        self.stream = stream
        self.by_did = {a.did: a for a in NC.accounts()}

    # --- the loop ------------------------------------------------------------------------------

    def cycle(self, now: datetime) -> dict:
        from betdata.nba.engine import live as L
        from betdata.news.sources import Stream

        cfg = NC.load()
        if self.stream is None and self.by_did:
            self.stream = Stream(cfg["bluesky"]["jetstream"], list(self.by_did))
        if self.stream is not None:
            self.stream.start()
        players, rows = rotation(L.read(L.LIVE_PATH), now, cfg.get("rotation_minutes", 15))
        posts = self.stream.drain() if self.stream is not None else []
        made = sum(len(self.on_post(x, players, rows, now)) for x in posts)
        if self.poller._due("due:sleeper", timedelta(minutes=cfg["sleeper"]["every_minutes"]),
                            now):  # fmt: skip
            self.poller._guard("sleeper", self.sleeper, now, players)
        self.poller._guard("news resolve", self.resolve, now, rows)
        return {"posts": len(posts), "claims": made}

    def stop(self) -> None:
        if self.stream is not None:
            self.stream.stop()

    # --- a post --------------------------------------------------------------------------------

    def on_post(self, post, players: list[Player], rows: dict, now: datetime) -> list[int]:
        """A watched account's post -> a logged claim (and alerts when ahead)."""
        acct = self.by_did.get(post.did)
        status = status_of(post.text)
        if acct is None or status is None:
            return []
        hits = match(post.text, players)
        if len(hits) != 1:  # none, or several players in one post: not clear
            return []
        pl = hits[0]
        cid = self.claim(post, acct, pl, status, now)
        return [cid] if cid is not None else []

    def claim(self, post, acct, pl: Player, status: str, now: datetime) -> int | None:
        from betdata.alerts.db import early_claims as ec

        with self.poller.main.connect() as m:
            is_ahead, cur = NC_.ahead(m, pl.game_id, pl.name, status, now)
        with self.poller.pdb.begin() as p:
            hint = p.execute(select(ec.c.claim_id).where(
                ec.c.source == "sleeper", ec.c.player_name == pl.name,
                ec.c.posted_at >= post.created_at - CORROBORATE)).first() is not None  # fmt: skip
            res = p.execute(sqlite_insert(ec).values(
                source="bluesky", account=acct.handle, group_=acct.group,
                player_name=pl.name, team=pl.team, game_id=pl.game_id, status=status,
                posted_at=post.created_at, detected_at=now, text=post.text[:500],
                uri=post.uri, official_before=cur, ahead=is_ahead, corroborated=hint,
                outcome="pending" if is_ahead else "official first",
                resolved_at=None if is_ahead else now).on_conflict_do_nothing())  # fmt: skip
            if not res.rowcount:
                return None  # the same claim again
            cid = res.inserted_primary_key[0]
        self.poller.notes.append(f"news: @{acct.handle} {pl.name} {status} "
                                 f"({'ahead' if is_ahead else 'official first'})")
        if is_ahead:
            ids = self.poller._guard(f"news alert {cid}", self.alert, cid, post, acct, pl,
                                     status, now) or []  # fmt: skip
            with self.poller.pdb.begin() as p:
                p.execute(update(ec).where(ec.c.claim_id == cid).values(alert_ids=ids))
        return cid

    # --- the reprice and the alerts -----------------------------------------------------------

    def alert(self, cid: int, post, acct, pl: Player, status: str, now: datetime) -> list[int]:
        """Reprice the game and the player's teammates' props with Next Gen; alert on each
        candidate that clears Next Gen's bar."""
        from betdata.alerts.db import get_state
        from betdata.alerts.notify import Alert, record
        from betdata.nba.engine import live as L
        from betdata.nba.engine import picks as K

        doc = L.read(L.LIVE_PATH)
        g = next((x for x in (doc or {}).get("games", []) if x["game_id"] == pl.game_id), None)
        me = next((x for x in (g or {}).get("players", []) if x.get("name") == pl.name), None)
        if g is None or me is None:
            return []
        game = self.poller.engine_override(pl.game_id, now, me, OUT if status == OUT
                                           else "Available")  # fmt: skip
        if game is None:
            return []
        design = K.Design.from_json(L.load_fit()[1]["design"])
        qs = self.poller.poll_game("nba", pl.game_id, now, None)
        mates = {x["name"] for x in game.get("players", [])
                 if x.get("team_id") == me.get("team_id") and x.get("name") != pl.name}
        cands = [c for c in self.poller.engine_candidates(pl.game_id, qs, now, game, design)
                 if c.player is None or c.player in mates]  # fmt: skip
        label, tip = self.poller.game_label(pl.game_id)
        since = (now - post.created_at).total_seconds() / 60
        with self.poller.pdb.connect() as p:
            push_on = alerts_on(p)
            topic_ok = get_state(p, "news:pushes_off") is None
        # rules v2 (owner 2026-11-09): news alerts are Next Gen's: assist props (no unders at
        # 5.5+, none with 2+ key teammates out) and game markets only
        from betdata import eligibility as EL

        def eligible(c) -> bool:
            if not EL.applies(datetime.fromisoformat(g["tip"])):
                return True
            tm = None
            if c.player and c.market == "assists" and c.side == "under":
                with self.poller.main.connect() as m:
                    tm = EL.teammates_out(m, pl.game_id, me.get("team_id"), c.player, now)
            return EL.reason("nextgen", c.market, c.side, c.line, tm) is None

        cands = [c for c in cands if eligible(c)]
        out = []
        for c in K.best_picks(cands, design):
            theta = design.theta["prop" if c.player else "game"]
            d = {"claim_id": cid, "source": "bluesky", "account": acct.handle,
                 "posted_at": post.created_at.isoformat(), "minutes_since": round(since, 1),
                 "good_down_to": NC_.good_down_to(c.fair, theta), "ev": c.ev, "theta": theta,
                 "edge_pp": round(K.edge_pp(c.fair, c.p_market), 4), "gate": K.gate(c.price),
                 "p_engine": c.p_engine, "p_market": c.p_market,
                 "market_source": c.market_source, "unconfirmed": True,
                 "news_status": "unconfirmed", "player_news": pl.name, "status_news": status,
                 "label_text": TITLE, "alerts_on": push_on}  # fmt: skip
            a = Alert(SIGNAL, "nba", pl.game_id, label, tip, c.market, c.side, c.line,
                      c.price, c.book, "", f"flash:{cid}:{c.market}:{(c.player or '')}",
                      c.fair, c.book_nv, c.player, None, True, d)  # fmt: skip
            a.reason = alert_text(a, d, now)
            with self.poller.pdb.begin() as pc:
                aid = record(pc, a, now)
            if aid is None:
                continue
            out.append(aid)
            if push_on and topic_ok:
                self.push(aid, a, now)
        if out:
            self.poller.notes.append(f"news alert {cid}: {len(out)} picks "
                                     f"({'pushed' if push_on else 'alerts off: logged'})")
        return out

    def push(self, aid: int, a, now: datetime) -> str:
        import httpx

        from betdata.alerts.db import alerts
        from betdata.alerts.notify import NTFY, PAGE

        topic = self.poller.keys.ntfy_topic
        if not topic:
            status = "no NTFY_TOPIC"
        else:
            try:
                r = (self.poller.push_client or httpx.Client(timeout=15)).post(
                    NTFY + topic, content=a.reason.encode(),
                    headers={"Title": TITLE_HEADER, "Click": PAGE, "Tags": "newspaper"})
                status = f"http {r.status_code}"
            except Exception as e:  # noqa: BLE001
                status = f"error {type(e).__name__}"
        with self.poller.pdb.begin() as p:
            p.execute(update(alerts).where(alerts.c.alert_id == aid).values(
                sent=status.startswith("http 2"), sent_at=now, send_status=status))  # fmt: skip
        return status

    # --- Sleeper (a hint only) ----------------------------------------------------------------

    def sleeper(self, now: datetime, players: list[Player]) -> int:
        from betdata.alerts.db import early_claims as ec
        from betdata.alerts.db import get_state, set_state
        from betdata.news import sources as S
        from betdata.news.parse import _words

        s = NC.load()["sleeper"]
        http = self.poller.http
        with self.poller.pdb.connect() as p:
            cached, prev = get_state(p, "sleeper:players"), get_state(p, "sleeper:counts", {})
        names = S.sleeper_players(http, s["players"], cached, now)
        r = http.get("sleeper", s["trending"], params={"lookback_hours": s["lookback_hours"],
                                                       "limit": s["limit"]})  # fmt: skip
        rows = r.json() if r.status_code == 200 else []
        spikes = S.sleeper_spikes(rows, prev, s["spike"], s["min_adds"])
        by_name = {" ".join(_words(pl.name)): pl for pl in players}
        n = 0
        with self.poller.pdb.begin() as p:
            if names is not cached:
                set_state(p, "sleeper:players", names, now)
            set_state(p, "sleeper:counts", {str(x["player_id"]): int(x.get("count") or 0)
                                            for x in rows}, now)  # fmt: skip
            for pid, adds in spikes:
                nm = (names.get("players", {}).get(pid) or [None])[0]
                pl = by_name.get(" ".join(_words(nm or "")))
                if pl is None:
                    continue
                res = p.execute(sqlite_insert(ec).values(
                    source="sleeper", account="trending adds", group_="hint",
                    player_name=pl.name, team=pl.team, game_id=pl.game_id, status=None,
                    posted_at=now, detected_at=now, text=f"{adds} adds in "
                    f"{s['lookback_hours']} h", ahead=True, outcome="pending"
                ).on_conflict_do_nothing())  # fmt: skip
                n += res.rowcount or 0
        return n

    # --- settling claims -----------------------------------------------------------------------

    def resolve(self, now: datetime, rows: dict) -> list[int]:
        from betdata.alerts.db import alerts, early_claims

        tips = {gid: datetime.fromisoformat(r["tip"]) for (gid, _), r in rows.items()}
        with self.poller.pdb.begin() as p, self.poller.main.connect() as m:
            done = NC_.resolve(p, m, now, tips)
            for c in p.execute(select(early_claims).where(
                    early_claims.c.claim_id.in_(done))).all() if done else []:
                for aid in c.alert_ids or []:
                    a = p.execute(select(alerts.c.details).where(
                        alerts.c.alert_id == aid)).scalar()  # fmt: skip
                    if a is None:
                        continue
                    p.execute(update(alerts).where(alerts.c.alert_id == aid).values(
                        details={**a, "news_status": c.outcome,
                                 "unconfirmed": c.outcome not in ("confirmed",
                                                                  "contradicted"),
                                 "official_at": None if c.official_at is None
                                 else c.official_at.isoformat()}))  # fmt: skip
        return done
