"""Probe: what does navigator.webdriver actually evaluate to under our patches?"""
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
    pg.goto("about:blank")
    print("navigator.webdriver =", pg.evaluate("() => navigator.webdriver"))
    print("has own on proto     =", pg.evaluate("() => Object.getPrototypeOf(navigator).hasOwnProperty('webdriver')"))
    print("in navigator         =", pg.evaluate("() => 'webdriver' in navigator"))
    print("descriptor           =", pg.evaluate("() => { const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(navigator), 'webdriver'); return d ? Object.keys(d).join(',') : 'none'; }"))
    b.close()
