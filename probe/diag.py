"""M16 live check: the Alerts section on the published page (390 px), and the page's status."""

import json
import re
import subprocess
import sys
import urllib.request

URL = "https://piratekinginc.github.io/test-m11/"
html = urllib.request.urlopen(URL + "?nocache=3").read().decode()
st = json.loads(re.search(r'<script id="status" type="application/json">(.*?)</script>', html,
                          re.S).group(1).replace("<\\/", "</"))
print("generated_at", st.get("generated_at"), "last_run", st.get("last_run"))
m = re.search(r'<section id="alerts".*?</section>', html, re.S)
print("ALERTS:", re.sub(r"\s+", " ", re.sub("<[^>]+>", " | ", m.group(0)))[:2500] if m else "none")
try:
    print("alerts.json:", urllib.request.urlopen(URL + "alerts.json").read().decode()[:1500])
except Exception as e:  # noqa: BLE001
    print("alerts.json:", e)
subprocess.run(["uv", "pip", "install", "--python", sys.executable, "playwright==1.48.0"],
               check=True, capture_output=True)
subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
               check=True, capture_output=True)
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    page.goto(URL + "?nocache=4", wait_until="networkidle")
    if page.locator("#alerts").count():
        page.locator("#alerts").screenshot(path="out/alerts_390px.png")
    print("overflow:", page.evaluate("document.documentElement.scrollWidth > window.innerWidth"))
    b.close()
