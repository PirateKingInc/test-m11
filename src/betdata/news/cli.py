"""`betdata news ...` (owner decision 2026-11-05, MODEL.md "News alerts")."""

from __future__ import annotations

import json
from pathlib import Path

import typer

news_app = typer.Typer(help="News alerts: watchlist check and the per-source log.",
                       no_args_is_help=True)  # fmt: skip


@news_app.command("accounts")
def accounts_cmd(out: Path = typer.Option(None, help="Write the account file here.")):
    """Find and verify each Bluesky watchlist account (run where Bluesky is reachable)."""
    from betdata.alerts.feeds import Http
    from betdata.news.accounts import check

    res = check(Http())
    text = json.dumps(res, indent=1)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n")
    for a in res["accounts"]:
        typer.echo(f"{'KEEP' if a['keep'] else 'drop'}  {a['group']:8} {a['name']:24} "
                   f"{a.get('handle') or '-':36} followers {a.get('followers', '-')!s:>7} "
                   f"verified {a.get('verified', '-')!s:5} last {a.get('last_post', '-')[:10]} "
                   f"({a.get('why')})")


@news_app.command("sleeper-check")
def sleeper_check():
    """One call to Sleeper's trending adds (reachability and shape)."""
    from betdata.alerts.feeds import Http
    from betdata.news import config as NC

    s = NC.load()["sleeper"]
    r = Http().get("sleeper", s["trending"], params={"lookback_hours": s["lookback_hours"],
                                                     "limit": s["limit"]})  # fmt: skip
    rows = r.json()
    typer.echo(f"sleeper trending: http {r.status_code}, {len(rows)} rows, first "
               f"{rows[:3]}")


@news_app.command("stream-check")
def stream_check(seconds: int = typer.Option(20), all_posts: bool = typer.Option(
        True, help="Listen to every post (not only the watchlist) to prove the stream.")):
    """Open the Jetstream WebSocket for a few seconds and count messages (reachability)."""
    import time

    from betdata.news import config as NC
    from betdata.news.sources import Stream

    accts = NC.accounts()
    st = Stream(NC.load()["bluesky"]["jetstream"], [a.did for a in accts])
    if all_posts:  # no DID filter: the whole post firehose, to prove messages arrive
        st.dids = []
        st._thread = None
        import threading

        st._stop.clear()
        st._thread = threading.Thread(target=st._run, daemon=True)
        st._thread.start()
    else:
        st.start()
    time.sleep(seconds)
    st.stop()
    typer.echo(f"jetstream: {st.messages} messages in {seconds} s, {st.reconnects} reconnects, "
               f"{len(accts)} watched accounts, cursor {st.cursor}")
