"""Probe: inspect what bot.sannysoft.com reports for the WebDriver rows."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from restaurant_filter.stealth import LAUNCH_ARGS, apply_stealth
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=LAUNCH_ARGS)
    ctx = b.new_context(locale="en-US", ignore_https_errors=True)
    apply_stealth(ctx)
    pg = ctx.new_page()
    print("webdriver value =", pg.evaluate("() => navigator.webdriver"))
    pg.goto("https://bot.sannysoft.com/", wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(6000)
    rows = pg.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('tr').forEach(tr => {
                const cells = Array.from(tr.querySelectorAll('td'));
                const txt = cells.map(c => (c.innerText||'').trim()).join(' | ');
                if (/webdriver/i.test(txt)) {
                    out.push(cells.map(c => `[${c.className}] ${(c.innerText||'').trim()}`).join('  ||  '));
                }
            });
            return out;
        }"""
    )
    for r in rows:
        print("ROW:", r)
    b.close()
