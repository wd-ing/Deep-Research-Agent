#!/usr/bin/env python3
"""Quick frame snapper: python snap.py t1 t2 ... → /tmp/dr-fX.png"""
import sys, pathlib
from playwright.sync_api import sync_playwright

COMP = pathlib.Path(__file__).parent / "thread" / "comp.html"
OUT = pathlib.Path("/tmp/drframes"); OUT.mkdir(exist_ok=True)

times = [float(a) for a in sys.argv[1:]] or [0.5, 2.0, 3.4, 5.0, 6.5, 8.0, 9.6, 11.0, 14.4, 15.0, 16.8, 20.5, 21.6, 23.5, 25.0, 27.0, 28.2]

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1920, "height": 1080})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(COMP.as_uri())
    pg.wait_for_function("window.__done === true", timeout=25000)
    pg.wait_for_timeout(300)
    for t in times:
        ok = pg.evaluate(f"window.__seek({t})")
        pg.wait_for_timeout(60)
        pg.screenshot(path=str(OUT / f"f{t:05.2f}.png"))
        print(f"t={t} seek→{ok}")
    if errors:
        print("ERRORS:")
        for e in errors[:12]:
            print(" ", e[:300])
    b.close()
