import json, os, subprocess, sys
r = subprocess.run([sys.executable, "-m", "betdata.cli", "nfl", "weather-final-test", "--force"],
                   capture_output=True, text=True)
print(r.stdout[-3000:], r.stderr[-1500:])
for p in ("data/reports/m13_weather_final_test.json",):
    print(p, os.path.exists(p))
    if os.path.exists(p):
        print(open(p).read()[:4000])
