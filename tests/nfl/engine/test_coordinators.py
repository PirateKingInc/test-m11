import numpy as np
import pandas as pd

from betdata.nfl.engine import coordinators as C


def test_seed_file_carries_the_attribution_and_reads_back(tmp_path):
    raw = [{"season": 2016, "team": "DEN", "head_coach": "Gary Kubiak", "oc": "Rick Dennison",
            "dc": "Wade Phillips", "oc_from": "page", "dc_from": "page", "revid": 7,
            "source": "https://en.wikipedia.org/w/index.php?oldid=7"}]
    path = tmp_path / "c.csv"
    C.build(raw, path)
    text = path.read_text()
    assert text.startswith("# NFL head coaches") and "CC BY-SA 4.0" in text
    df = C.load(path)
    assert df.loc[0, "oc"] == "Rick Dennison" and list(df.columns) == C.COLUMNS


def test_persistence_compares_same_and_changed_coordinators():
    rng = np.random.default_rng(0)
    rows, coords = [], []
    for t in range(80):
        base = rng.normal(0.5, 0.05)
        for s in (2016, 2017):
            changed = t % 2 == 0 and s == 2017
            v = (rng.normal(0.5, 0.05) if changed else base + rng.normal(0, 0.01))
            rows.append({"team": f"T{t}", "opp": f"T{(t + 1) % 80}", "season": s,
                         "neutral_dropbacks": v * 300, "neutral_plays": 300,
                         "neutral_secs": 30.0, "neutral_gaps": 1.0, "pressures": 1.0,
                         "pressure_n": 3.0, "pass_epa": 1.0, "dropbacks": 10.0})
            coords.append({"team": f"T{t}", "season": s, "head_coach": "H",
                           "oc": f"new{t}" if changed else "same", "dc": "d"})
    res = C.persistence(pd.DataFrame(coords), pd.DataFrame(rows))
    p = res["neutral_pass_rate"]
    assert p["pairs_same"] == 40 and p["pairs_changed"] == 40
    assert p["r_same"] > 0.8 > p["r_changed"]
    assert p["used"] is False  # under 60 pairs in each group
