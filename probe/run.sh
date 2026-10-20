#!/usr/bin/env bash
set -u
mkdir -p out/pdf
get() { sleep 1.5; curl -s -m 20 -o "$2" -w '%{http_code}' "https://ak-static.cms.nba.com/referee/injury/Injury-Report_$1.pdf"; }
echo "known-good 2026-10-20_11_00AM: $(get 2026-10-20_11_00AM /tmp/x.pdf)" | tee -a out/summary.txt
echo "known-good 2025-12-01_05PM: $(get 2025-12-01_05PM /tmp/x.pdf)" | tee -a out/summary.txt
for d in 2019-01-10 2019-11-20 2020-08-05 2021-01-20 2021-11-17 2022-12-07 2023-03-20 2023-11-15 2024-02-07 2025-03-12 2025-11-19 2026-02-11; do
  have=""
  for h in $(seq 6 23); do
    t="$(printf '%02d' $(( (h%12)==0 ? 12 : h%12 )))$( [ $h -lt 12 ] && echo AM || echo PM)"
    c=$(get "${d}_${t}" /tmp/r.pdf)
    [ "$c" = 200 ] && { have="$have $t"; cp /tmp/r.pdf "out/pdf/${d}_${t}.pdf"; }
    [ "$c" != 200 ] && [ "$c" != 403 ] && have="$have [$t:$c]"
  done
  echo "inj $d:$have" | tee -a out/summary.txt
done
# 15-minute format: current season, and whether 2025-26 used it too
for t in 2026-10-20_05_00PM 2026-10-20_05_15PM 2026-10-20_06_30PM 2026-10-21_09_00AM 2026-10-21_11_30AM 2026-02-11_05_00PM 2026-02-11_05_30PM 2025-11-19_05_30PM; do
  c=$(get "$t" /tmp/r.pdf); [ "$c" = 200 ] && cp /tmp/r.pdf "out/pdf/$t.pdf"; echo "inj $t: $c" | tee -a out/summary.txt
done
du -sh out
