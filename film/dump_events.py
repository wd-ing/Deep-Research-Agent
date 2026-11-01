#!/usr/bin/env python3
"""dump __events() from the comp → events.json (run with system python3)."""
import json, pathlib
from playwright.sync_api import sync_playwright
COMP = pathlib.Path(__file__).parent / "thread" / "comp.html"
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1920, "height": 1080})
    pg.on("pageerror", lambda e: print("PAGEERR:", str(e)[:400]))
    pg.goto(COMP.as_uri())
    pg.wait_for_function("window.__done === true", timeout=30000)
    ev = pg.evaluate("window.__events()")
    b.close()
out = pathlib.Path(__file__).parent / "events.json"
out.write_text(json.dumps(ev, indent=1))
print(f"{len(ev)} events → {out}")
from collections import Counter
print(Counter(e["k"] for e in ev))
