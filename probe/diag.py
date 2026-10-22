import os
for k in ("SHARPAPI_KEY", "SPORTSGAMEODDS_API_KEY", "ODDS_API_KEY", "NTFY_TOPIC"):
    v = os.environ.get(k) or ""
    print(k, "SET" if v else "MISSING", "len>0" if v else "")
