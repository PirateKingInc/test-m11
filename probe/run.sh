#!/usr/bin/env bash
set -u
mkdir -p out
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36'
probe() { # name url [extra curl args]
  local name=$1 url=$2; shift 2
  local code
  code=$(curl -sS -L -m 40 -A "$UA" -o "out/$name" -w '%{http_code} %{size_download}' "$@" "$url" 2>out/$name.err)
  echo "$code $name $url" | tee -a out/summary.txt
}
# cdn.nba.com
probe cdn_schedule.json https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json
probe cdn_scoreboard.json https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json
probe cdn_box_old.json https://cdn.nba.com/static/json/liveData/boxscore/boxscore_0022300001.json
probe cdn_box_new.json https://cdn.nba.com/static/json/liveData/boxscore/boxscore_0022600001.json
probe cdn_odds.json https://cdn.nba.com/static/json/liveData/odds/odds_todaysGames.json
# stats.nba.com
probe stats_scoreboard.json "https://stats.nba.com/stats/scoreboardv2?GameDate=2024-01-01&LeagueID=00&DayOffset=0" -H 'Referer: https://www.nba.com/' -H 'Origin: https://www.nba.com'
# official injury report
probe inj_page_2526.html https://official.nba.com/nba-injury-report-2025-26-season/
probe inj_page_2627.html https://official.nba.com/nba-injury-report-2026-27-season/
probe inj_page_2122.html https://official.nba.com/nba-injury-report-2021-22-season/
probe inj_page_2021.html https://official.nba.com/nba-injury-report-2020-21-season/
for f in Injury-Report_2026-10-20_05PM.pdf Injury-Report_2026-10-20_11AM.pdf Injury-Report_2026-10-20_05_00PM.pdf Injury-Report_2026-10-20_11_00AM.pdf Injury-Report_2025-12-01_05PM.pdf Injury-Report_2025-12-01_05_00PM.pdf Injury-Report_2023-01-10_05PM.pdf Injury-Report_2021-12-01_05PM.pdf Injury-Report_2020-01-10_05PM.pdf Injury-Report_2019-01-10_05PM.pdf Injury-Report_2018-01-10_05PM.pdf; do
  probe "pdf_$f" "https://ak-static.cms.nba.com/referee/injury/$f"
done
# ESPN
probe espn_scoreboard.json "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
probe espn_summary_old.json "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401585601"
probe espn_summary_2016.json "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=400827888"
probe espn_core_odds_old.json "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/401585601/competitions/401585601/odds"
probe espn_core_odds_2016.json "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/400827888/competitions/400827888/odds"
probe espn_injuries.json "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
# other line archives
probe sbro.html "https://www.sportsbookreviewsonline.com/scoresoddsarchives/nba/nbaoddsarchives.htm"
probe kyleskom_odds.sqlite "https://raw.githubusercontent.com/kyleskom/NBA-Machine-Learning-Sports-Betting/master/Data/OddsData.sqlite" -r 0-1023
# sportsdataverse / hoopR release listings
for repo in sportsdataverse/sportsdataverse-data sportsdataverse/hoopR-nba-data sportsdataverse/hoopR-data; do
  gh api "repos/$repo/releases?per_page=100" --jq '.[] | .tag_name' > "out/tags_${repo//\//_}.txt" 2>&1; echo "tags $repo: $(wc -l < "out/tags_${repo//\//_}.txt")" | tee -a out/summary.txt
done
for tag in espn_nba_player_boxscores espn_nba_team_boxscores espn_nba_schedules espn_nba_pbp nba_stats_player_boxscores espn_nba_rosters nba_odds espn_nba_betting; do
  gh api "repos/sportsdataverse/sportsdataverse-data/releases/tags/$tag" --jq '.assets[] | "\(.name) \(.size) \(.updated_at)"' > "out/assets_$tag.txt" 2>&1; echo "assets $tag: $(wc -l < "out/assets_$tag.txt")" | tee -a out/summary.txt
done
rm -f out/*.err
find out -size +20M -delete
ls -la out
