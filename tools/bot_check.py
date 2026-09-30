"""Launch the SAME browser the scraper uses and point it at a bot-detection page
(default: https://bot.sannysoft.com/), then report what the page flags.

This mirrors ``scrape_place``'s launch (chromium, headed, en-US context,
ignore_https_errors) so the fingerprint we test is the one the scraper actually
presents. It saves a full-page screenshot and prints every detection row that is
marked as failing/suspicious so the result is auditable.

Usage (from the project root):
    python tools/bot_check.py                 # headed, sannysoft
    python tools/bot_check.py --headless      # headless (for comparison)
    python tools/bot_check.py --url https://... --shot out.png
"""

from __future__ import annotations

import argparse
import os
import sys

# Make ``restaurant_filter`` importable when run as ``python tools/bot_check.py``
# so this check uses the EXACT stealth patches the scraper launches with.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from restaurant_filter.stealth import LAUNCH_ARGS, apply_stealth  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot-detection fingerprint check.")
    ap.add_argument("--url", default="https://bot.sannysoft.com/",
                    help="Detection page to load.")
    ap.add_argument("--shot", default="bot_sannysoft.png",
                    help="Screenshot output path.")
    ap.add_argument("--headless", action="store_true",
                    help="Run headless (scraper runs headed by default).")
    ap.add_argument("--no-stealth", action="store_true",
                    help="Disable the scraper's stealth patches (raw fingerprint).")
    ap.add_argument("--wait", type=int, default=8000,
                    help="Milliseconds to let the tests settle before capture.")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && "
              "playwright install chromium")
        return 2

    with sync_playwright() as p:
        launch_args = [] if args.no_stealth else LAUNCH_ARGS
        browser = p.chromium.launch(headless=args.headless, args=launch_args)
        context = browser.new_context(locale="en-US", ignore_https_errors=True)
        if not args.no_stealth:
            apply_stealth(context)
        page = context.new_page()
        stealth_state = "off" if args.no_stealth else "on"
        print(f"Loading {args.url} (headless={args.headless}, stealth={stealth_state}) ...")
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(args.wait)

        # sannysoft marks each result cell with a class of 'passed' (green) or
        # 'failed'/'warn' (red/orange). Collect every row and flag the bad ones.
        rows = page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('tr').forEach(tr => {
                    const cells = Array.from(tr.querySelectorAll('td'));
                    if (!cells.length) return;
                    const cls = cells.map(c => (c.className || '')).join(' ').toLowerCase();
                    const bad = /fail|warn/.test(cls);
                    const text = cells.map(c => (c.innerText || '').trim())
                                      .filter(Boolean).join(' \u2014 ');
                    if (text) out.push({ text, bad });
                });
                return out;
            }"""
        )

        flagged = [r["text"] for r in rows if r["bad"]]
        total = len(rows)
        print(f"\nParsed {total} result rows.")
        if flagged:
            print(f"\n\u26a0 {len(flagged)} row(s) flagged as bot-like:")
            for t in flagged:
                print(f"  - {t}")
        else:
            print("\n\u2713 No rows flagged as bot-like by the page's own classes.")

        try:
            page.screenshot(path=args.shot, full_page=True)
            print(f"\nSaved screenshot -> {args.shot}")
        except Exception as exc:  # noqa: BLE001
            print(f"\nScreenshot failed: {exc}")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
