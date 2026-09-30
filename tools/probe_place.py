"""Probe what actually loads for a given Maps cid URL in the CDP-attached Brave.

Usage: python tools/probe_place.py "https://maps.google.com/?cid=...&hl=en"
Reports final URL, title, and whether a Reviews tab / review cards / no-reviews
signal is present, plus a dump of visible role=tab aria-labels.
"""
import sys
import time
from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"
url = sys.argv[1] if len(sys.argv) > 1 else "https://maps.google.com/?cid=8573377726494702568&hl=en"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(6)
    print("FINAL_URL:", page.url)
    print("TITLE:", page.title())
    tabs = page.query_selector_all('[role="tab"]')
    print("TABS:", [t.get_attribute("aria-label") for t in tabs])
    print("REVIEWS_BTN:", bool(page.query_selector(
        'button[role="tab"][aria-label*="Reviews"], [role="tab"][aria-label*="Reviews"], '
        'button[aria-label*="Reviews for"], button[jsaction*="reviewChart"]')))
    print("REVIEW_CARD:", bool(page.query_selector('button[aria-label^="Actions for"]')))
    body = (page.inner_text("body") or "")[:1500]
    print("BODY_HEAD:", body.replace("\n", " | ")[:1200])
    page.close()
