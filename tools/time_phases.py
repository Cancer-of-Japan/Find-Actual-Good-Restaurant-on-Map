"""Time each phase of the scrape pipeline to find the real bottleneck.

Prints elapsed seconds (to stderr, unbuffered) for: playwright start, CDP connect,
context/page, page.goto, and a settle. Run against the small place's cid URL.
"""
import sys
import time

def mark(label, t0):
    print(f"[{time.time()-t0:7.2f}s] {label}", file=sys.stderr, flush=True)

url = sys.argv[1] if len(sys.argv) > 1 else "https://maps.google.com/?cid=..."
CDP = "http://127.0.0.1:9222"

t0 = time.time()
mark("start", t0)
from playwright.sync_api import sync_playwright  # noqa: E402
mark("import playwright", t0)

with sync_playwright() as p:
    mark("sync_playwright ready", t0)
    browser = p.chromium.connect_over_cdp(CDP)
    mark("connect_over_cdp", t0)
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    mark("context", t0)
    page = ctx.new_page()
    mark("new_page", t0)
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    mark("goto domcontentloaded", t0)
    page.wait_for_timeout(3000)
    mark("settle 3s", t0)
    print("FINAL_URL:", page.url, file=sys.stderr, flush=True)
    page.close()
    mark("page.close", t0)
mark("done", t0)
