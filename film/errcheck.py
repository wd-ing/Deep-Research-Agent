#!/usr/bin/env python3
import sys, pathlib
from playwright.sync_api import sync_playwright
COMP = pathlib.Path(__file__).parent / "thread" / "comp.html"
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1920, "height": 1080})
    pg.on("pageerror", lambda e: print("PAGEERR:", str(e)[:500]))
    pg.on("console", lambda m: print("CONSOLE:", m.text[:300]) if m.type in ("error", "warning") else None)
    pg.goto(COMP.as_uri())
    pg.wait_for_timeout(6000)
    print("done flag:", pg.evaluate("window.__done"))
    print("ready type:", pg.evaluate("typeof window.__ready"))
    print("path len:", pg.evaluate("window.__dbglen = (typeof PATH!=='undefined') ? (PATH?PATH.length:'null') : 'undef'"))
    b.close()
