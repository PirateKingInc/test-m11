#!/usr/bin/env bash
set -u
mkdir -p out/pdf
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36'
H=(-H 'Referer: https://www.nba.com/' -H 'Origin: https://www.nba.com' -H 'Accept: application/json')
code() { curl -s -L -m 30 -A "$UA" -o "$2" -w '%{http_code}' "${@:3}" "$1"; }
echo "cdn with headers: $(code https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json out/cdn.json "${H[@]}")" | tee -a out/summary.txt
echo "cdn no UA: $(curl -s -m 30 -o /dev/null -w '%{http_code}' https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json)" | tee -a out/summary.txt
# ESPN live endpoints
echo "espn sb today: $(code 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261021' out/espn_sb_20261021.json)" | tee -a out/summary.txt
echo "espn sb yday: $(code 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261020' out/espn_sb_20261020.json)" | tee -a out/summary.txt
echo "espn sb 2016: $(code 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20160105' out/espn_sb_20160105.json)" | tee -a out/summary.txt
gid=$(python3 -c "import json;d=json.load(open('out/espn_sb_20261020.json'));print(d['events'][0]['id'])" 2>/dev/null)
echo "espn summary $gid: $(code "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=$gid" out/espn_summary_live.json)" | tee -a out/summary.txt
echo "espn odds $gid: $(code "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/$gid/competitions/$gid/odds" out/espn_odds_live.json)" | tee -a out/summary.txt
# 2019-20 and 2020-21 era events: odds availability
for e in 401160620 401266805 401307582 401360000 401468000 401584700; do
  echo "espn odds $e: $(code "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/$e/competitions/$e/odds" out/espn_odds_$e.json)" | tee -a out/summary.txt
done
# Injury report hours on sample dates (hour format; 15-min format for 2026)
fmt_hour() { python3 - "$1" <<'PY'
import sys
h=int(sys.argv[1]); print(f"{(h%12) or 12:02d}{'AM' if h<12 else 'PM'}")
PY
}
for d in 2019-01-10 2019-11-20 2020-02-05 2020-08-05 2021-01-20 2021-04-10 2021-11-17 2022-03-15 2022-12-07 2023-03-20 2023-11-15 2024-02-07 2024-11-20 2025-03-12 2025-11-19 2026-02-11 2026-10-20; do
  have=""
  for h in $(seq 0 23); do
    t=$(fmt_hour $h)
    c=$(curl -s -m 20 -o /tmp/r.pdf -w '%{http_code}' "https://ak-static.cms.nba.com/referee/injury/Injury-Report_${d}_${t}.pdf")
    if [ "$c" = 200 ]; then have="$have $t"; cp /tmp/r.pdf "out/pdf/${d}_${t}.pdf"; fi
    for m in 00 15 30 45; do
      t2="$(printf '%02d' $(( (h%12)==0 ? 12 : h%12 )))_${m}$( [ $h -lt 12 ] && echo AM || echo PM)"
      c=$(curl -s -m 20 -o /tmp/r.pdf -w '%{http_code}' "https://ak-static.cms.nba.com/referee/injury/Injury-Report_${d}_${t2}.pdf")
      if [ "$c" = 200 ]; then have="$have $t2"; [ "$m" = 00 ] && cp /tmp/r.pdf "out/pdf/${d}_${t2}.pdf"; fi
    done
  done
  echo "inj $d:$have" | tee -a out/summary.txt
done
# SBR archive links
curl -s -m 30 -A "$UA" https://www.sportsbookreviewsonline.com/scoresoddsarchives/nba/nbaoddsarchives.htm | grep -o 'href="[^"]*\.xlsx\?"' | head -30 | tee out/sbro_links.txt
du -sh out
