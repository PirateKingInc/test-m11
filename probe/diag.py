import json, os, subprocess
p = "data/reports/m13_weather_final_test.json"
before = json.load(open(p)) if os.path.exists(p) else None
print("recorded exists:", before is not None)
r = subprocess.run(["betdata", "nfl", "weather-final-test", "--force"], capture_output=True, text=True)
print(r.stdout[-2500:], r.stderr[-1500:])
after = json.load(open(p)) if os.path.exists(p) else None
def flat(d, pre=""):
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict): out.update(flat(v, pre + k + "."))
        elif isinstance(v, (int, float)): out[pre + k] = v
    return out
fa, fb = flat(after), flat(before)
diff = {k: (fa[k], fb.get(k)) for k in fa if fb.get(k) != fa[k]}
print("numeric keys", len(fa), "diffs", len(diff), list(diff.items())[:15])
