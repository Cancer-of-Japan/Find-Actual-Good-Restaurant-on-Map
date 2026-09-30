"""Playwright review + histogram scraper (PROJECT.md §4, M2).

All Google-Maps-specific selectors are isolated here so breakage is contained to
one file. The scraper is deliberately polite: randomized delays, bounded scroll
passes, and it aborts on any CAPTCHA rather than fighting it.

If Playwright is not installed or the page structure has changed, `scrape_place`
returns an empty ScrapeResult instead of raising, so a snapshot run can still
record Places API data.
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from datetime import datetime, timezone

from .analyzer import parse_relative_date
from .human_mouse import human_click, human_scroll, humancursor_available
from .models import Review, ScrapeResult
from .review_api import fetch_reviews_via_replay
from .stealth import LAUNCH_ARGS, apply_stealth

# Selectors are best-effort and expected to need periodic maintenance.
# Confirmed against the Google Maps place panel (cid= URL, hl=en) Sep 2026.
# Google moved from the older `jftiEf/d4r55/wiI7pd` names to a `Reviews` tab +
# `Actions for ...` review row pattern in the headed browser mode.
_SEL_REVIEWS_BTN = (
    'button[role="tab"][aria-label*="Reviews"], '
    '[role="tab"][aria-label*="Reviews"], '
    'button[aria-label*="Reviews for"], '
    'button[jsaction*="reviewChart"]'
)
# Google removed the old stable review anchors (`jftiEf`, `data-review-id`,
# `role="article"`/`role="listitem"`). The ONLY durable per-review anchor left
# in the headed layout is the "Actions for <name>'s review" button; we enumerate
# those and climb to the surrounding card container (see `_review_cards`). The
# generic `div.fontBodyMedium` is deliberately NOT used as a card selector because
# it also matches Overview text ("See photos", the address, "4.8"), which would
# fabricate junk "reviews".
_SEL_REVIEW_ANCHOR = 'button[aria-label^="Actions for"]'
_SEL_REVIEW_CARD = _SEL_REVIEW_ANCHOR
# Extracts the reviewer name from the anchor aria-label: "Actions for X's review".
_RE_ACTIONS_AUTHOR = re.compile(r"Actions for (.+?)'s review", re.I)
_SEL_AUTHOR = 'div.d4r55, .d4r55, [data-reviewer-name], [role="heading"]'
_SEL_RATING = 'span.kvMYJc, span[role="img"][aria-label*="star"], [aria-label*="stars"]'
_SEL_TEXT = 'span.wiI7pd, .wiI7pd'
_SEL_REL_DATE = 'span.rsqaWe, .rsqaWe, [aria-label*="ago"], time'
# Reviewer subtitle, e.g. "Local Guide · 47 reviews" or "12 reviews" (credibility).
_SEL_REVIEWER_META = 'div.RfnDt, .RfnDt'
# Owner's reply block ("Response from the owner").
_SEL_OWNER_RESP = 'div.CDe7pd, .CDe7pd'
_SEL_HIST_ROW = 'tr.mB0hp, [aria-label*="stars,"]'
_SEL_CAPTCHA = 'iframe[src*="recaptcha"], form#captcha-form'

# "Sort" control + its radio menu items. The menu order is fixed by Google:
# 0 = Most relevant, 1 = Newest, 2 = Highest rating, 3 = Lowest rating.
_SEL_SORT_BTN = (
    'button[aria-label*="Sort reviews"], button[aria-label^="Sort"], '
    'button[data-value="Sort"]'
)
_SEL_SORT_MENU_ITEM = '[role="menuitemradio"], [role="menuitem"], [role="option"]'
_SORT_INDEX = {"relevant": 0, "newest": 1, "highest_rating": 2, "lowest_rating": 3}

_HIST_RE = re.compile(r"([1-5])\s+stars?,\s+([\d,]+)\s+review", re.IGNORECASE)
# Pull the reviewer's lifetime review count out of the subtitle text.
_REVIEWER_COUNT_RE = re.compile(r"([\d,]+)\s+review", re.IGNORECASE)

# §7.3: Google sometimes shows a native notice that reviews were removed (policy
# violations, spam, etc.). We match the notice phrasing (not action menus like
# "Report review") and pull any counts out of it. Best-effort + auditable: the
# raw matched line is stored so a human can verify what triggered it.
_NOTICE_HINT_RE = re.compile(
    r"(were removed|have been removed|was removed|been removed|reviews? removed"
    r"|removed \d|that violate|violate .*polic|removed for )",
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"\d[\d,]*")
_NO_REVIEWS_RE = re.compile(r"\b(no reviews|be the first to review)\b", re.IGNORECASE)


def _review_hash(place_id: str, author: str, text: str, rel_date: str) -> str:
    raw = f"{place_id}|{author}|{text}|{rel_date}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _english_url(maps_url: str) -> str:
    """Normalize to a clean cid URL and force English so dates parse.

    The Places API appends a `g_mp=` attribution token that makes Google render a
    limited card without the reviews section, so we strip everything but `cid`.
    """
    m = re.search(r"[?&]cid=(\d+)", maps_url)
    if m:
        return f"https://maps.google.com/?cid={m.group(1)}&hl=en"
    sep = "&" if "?" in maps_url else "?"
    return f"{maps_url}{sep}hl=en"


def scrape_place(maps_url: str, place_id: str, cfg: dict) -> ScrapeResult:
    """Scrape reviews + histogram for one restaurant. Never raises on scrape failure."""
    result = ScrapeResult(place_id=place_id)
    if not cfg.get("enabled", True):
        return result

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [scraper] Playwright not installed; skipping scrape. "
              "Run: pip install playwright && playwright install chromium")
        return result

    # Upper safety bound on scroll passes so a misbehaving feed can't loop forever.
    # It is NOT the normal stop condition: collection ends when every review from
    # the rating histogram is loaded, or when the feed stops growing for
    # `scroll_stagnation_limit` consecutive passes (whichever comes first).
    max_passes = int(cfg.get("max_scroll_passes", 1000))
    # How many consecutive no-growth passes mean "the feed is exhausted, move on".
    stagnation_limit = max(1, int(cfg.get("scroll_stagnation_limit", 4)))
    headless = bool(cfg.get("headless", False))
    # Scrolling the *same* page to lazy-load more cards is not a fresh request, so
    # it doesn't need the full between-request politeness delay. Reviews stream in
    # well under a second; a short wait keeps collection fast without hammering.
    scroll_min = float(cfg.get("scroll_min_delay", 0.6))
    scroll_max = float(cfg.get("scroll_max_delay", 1.4))

    def scroll_nap() -> None:
        time.sleep(random.uniform(scroll_min, scroll_max))

    if not humancursor_available():
        print("  [scraper] HumanCursor not available; using plain mouse motion.")

    # When set, attach to a running Brave (or any Chromium) exposing a DevTools
    # endpoint instead of launching a throwaway browser. This reuses the user's
    # logged-in profile so Google shows the full, login-gated review set.
    brave_cdp = str(cfg.get("brave_cdp_url", "") or "").strip()

    try:
        with sync_playwright() as p:
            if brave_cdp:
                # The browser and its context are the user's own session, so we
                # never close them on exit -- only the tab we open.
                browser = p.chromium.connect_over_cdp(brave_cdp)
                context = (browser.contexts[0] if browser.contexts
                           else browser.new_context(locale="en-US",
                                                    ignore_https_errors=True))
                owns_browser = False
                print(f"  [scraper] attached to Brave via CDP: {brave_cdp}")
            else:
                browser = p.chromium.launch(headless=headless, args=LAUNCH_ARGS)
                context = browser.new_context(locale="en-US", ignore_https_errors=True)
                apply_stealth(context)
                owns_browser = True

            # Bound every wait so a stalled proxy/navigation can never hang the batch.
            context.set_default_timeout(20000)
            context.set_default_navigation_timeout(30000)
            page = context.new_page()

            def _cleanup() -> None:
                """Close our tab; only close the browser if we launched it."""
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
                if owns_browser:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass

            opened = False
            for attempt in range(3):  # reload if the panel is slow/flaky
                page.goto(_english_url(maps_url), wait_until="domcontentloaded", timeout=30000)
                # Maps polls continuously so networkidle almost never fires; waiting
                # on it just burned ~5s per attempt. The state-based polling in
                # _open_reviews handles slow loads, so we skip straight to it.
                _dismiss_consent(page)
                _dismiss_signin(page)
                if page.query_selector(_SEL_CAPTCHA):
                    print("  [scraper] CAPTCHA detected \u2014 aborting scrape (by design).")
                    _cleanup()
                    return result
                # Retry with a slightly longer wait on later attempts, but prefer
                # state-based detection over long blind sleeps.
                if _open_reviews(page, timeout_ms=7000 + attempt * 3000):
                    opened = True
                    break

            if not opened:
                print("  [scraper] reviews section not found; skipping.")
                _cleanup()
                return result

            result.histogram = _scrape_histogram(page)
            (result.deleted_notice_text,
             result.deleted_notice_min,
             result.deleted_notice_max) = _scrape_deleted_notice(page)

            # Tier 1 (PROJECT.md §7a): replay the internal review endpoint using
            # this session's cookies to pull the *full* review set. Returns None
            # unless enabled (scraper.network_replay) and calibrated, in which
            # case we fall through to the Tier 2 DOM scroll below.
            def _ctx_request(get_url: str):
                resp = context.request.get(get_url)
                return resp.status, resp.text()

            tier1 = None
            try:
                tier1 = fetch_reviews_via_replay(maps_url, place_id, _ctx_request, cfg)
            except Exception:  # noqa: BLE001 - Tier 1 must never break the scrape
                tier1 = None

            if tier1:
                result.reviews = tier1
                result.source = "network_replay"
            else:
                result.source = "dom_scroll_fallback"
                sort_by = str(cfg.get("review_sort", "newest")).lower()
                if _sort_reviews(page, sort_by):
                    print(f"  [scraper] sorted reviews by {sort_by}")
                # The histogram (scraped above) sums to the total review count Google
                # shows next to the star rating. Use it as an early-exit target so we
                # stop scrolling the moment we've loaded them all instead of burning
                # extra empty passes -- a big win for places with few reviews.
                target = sum(result.histogram)
                if target:
                    print(f"  [scraper] target {target} reviews (from rating histogram)")
                # Attached to the user's real logged-in Brave? Then bot-motion is
                # pointless, so scroll fast (direct container jumps) instead of the
                # slow humanised wheel + long naps.
                _scroll_reviews(page, max_passes, scroll_nap,
                                fast=bool(brave_cdp), target_count=target,
                                stagnation_limit=stagnation_limit)
                now = datetime.now(timezone.utc)
                for card, author in _review_cards(page):
                    rv = _parse_card(card, place_id, now, author_hint=author)
                    if rv:
                        result.reviews.append(rv)

            if sum(result.histogram) == 0:
                result.histogram = _rebuild_histogram(result.reviews)
            _cleanup()
    except Exception as exc:  # noqa: BLE001 - scraping is inherently brittle
        print(f"  [scraper] scrape failed ({type(exc).__name__}): {exc}")

    return result


def _dismiss_consent(page) -> None:
    """Click through a cookie/consent dialog only if one is clearly present."""
    for sel in (
        'button[aria-label="Accept all"]',
        'button[aria-label="Reject all"]',
    ):
        el = page.query_selector(sel)
        if el:
            try:
                # Short timeout: this is best-effort. Without it, a present-but-
                # non-actionable consent button burns the full 20s context default.
                el.click(timeout=1500)
                page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass
            return


def _dismiss_signin(page) -> bool:
    """Close the "Sign-in to get the best of Google Maps" interstitial if present.

    Google intermittently overlays a sign-in modal that covers the Reviews tab and
    the whole review feed. We never sign in (no logged-in automation, by design);
    we just dismiss it so the reviews are reachable. It can re-appear after the
    panel settles, so callers invoke this more than once. Best-effort, never raises.
    Returns True when something was actually dismissed.

    SAFETY: we only click genuine controls (`button` / `[role="button"]`) whose OWN
    text is an exact dismiss word. We deliberately do NOT click bare `<a>`/`<span>`/
    `<div>` matches — under a corporate SSL proxy those can trigger a navigation
    that crashes/closes the tab (observed as TargetClosedError), which then wedges
    the whole scrape. Everything is guarded against an already-closed page.
    """
    try:
        if page.is_closed():
            return False
    except Exception:  # noqa: BLE001
        return False

    # 1) Click a real "Dismiss"/"No thanks"/... control by exact text. Restricting
    #    to button-like elements avoids navigating links and layout containers.
    try:
        clicked = page.evaluate(
            """() => {
                const wanted = ['dismiss', 'no thanks', 'stay signed out', 'not now'];
                const cands = Array.from(document.querySelectorAll(
                    'button, [role="button"]'));
                for (const el of cands) {
                    const t = (el.textContent || '').trim().toLowerCase();
                    if (wanted.includes(t) && el.offsetParent !== null) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }"""
        )
        if clicked:
            if not page.is_closed():
                page.wait_for_timeout(600)
            return True
    except Exception:  # noqa: BLE001
        return False

    # 2) A close (X) button on an explicit dialog (specific, non-navigating).
    for sel in (
        'div[role="dialog"] button[aria-label*="Close"]',
        'div[role="dialog"] button[jsaction*="modal.close"]',
    ):
        try:
            if page.is_closed():
                return False
            el = page.query_selector(sel)
            if el:
                # Short timeout: a stale dialog Close button that is present but not
                # actionable would otherwise block the full 20s context default,
                # stalling every _wait_for_review_feed poll that calls this helper.
                el.click(timeout=1500)
                page.wait_for_timeout(600)
                return True
        except Exception:  # noqa: BLE001
            return False
    return False


def _body_text(page) -> str:
    try:
        if page.is_closed():
            return ""
        return page.inner_text("body")
    except Exception:  # noqa: BLE001
        return ""


def _has_no_reviews_signal(page) -> bool:
    return bool(_NO_REVIEWS_RE.search(_body_text(page)))


def _wait_for_review_entry(page, timeout_ms: int = 7000, step_ms: int = 300,
                           none_grace_ms: int = 3000) -> str:
    """Wait for the page to reveal either a reviews entrypoint or a no-reviews state.

    The presence of a Reviews tab or review cards is authoritative and checked on
    every poll (and returned the instant it appears). The body-text "no reviews"
    signal is only consulted AFTER a short grace period (none_grace_ms) so it can't
    false-positive before the Reviews tab has had a chance to render; that lets us
    fast-fail genuinely empty places in ~3s instead of burning the whole timeout.
    """
    start = time.time()
    deadline = start + (timeout_ms / 1000)
    while time.time() < deadline:
        try:
            if page.is_closed():
                return "missing"
        except Exception:  # noqa: BLE001
            return "missing"
        _dismiss_signin(page)
        # Prefer the Reviews tab: the full /maps/place/ layout shows a few preview
        # review cards on the Overview tab that also match _SEL_REVIEW_CARD, so we
        # must NOT treat those as the feed — clicking the tab lands us on the real
        # (sortable, scrollable) review list. Only accept bare cards when there is
        # no tab at all (the simple place-card layout).
        if page.query_selector(_SEL_REVIEWS_BTN):
            return "tab"
        if page.query_selector(_SEL_REVIEW_CARD):
            return "card"
        # Fast-fail a truly empty place once the panel has had a moment to render.
        if (time.time() - start) * 1000 >= none_grace_ms and _has_no_reviews_signal(page):
            return "none"
        page.wait_for_timeout(step_ms)
    if page.query_selector(_SEL_REVIEWS_BTN):
        return "tab"
    if page.query_selector(_SEL_REVIEW_CARD):
        return "card"
    if _has_no_reviews_signal(page):
        return "none"
    return "missing"


def _wait_for_review_feed(page, timeout_ms: int = 6000, step_ms: int = 250) -> bool:
    """Wait until the review feed becomes usable after entering the reviews section."""
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        try:
            if page.is_closed():
                return False
        except Exception:  # noqa: BLE001
            return False
        _dismiss_signin(page)
        if page.query_selector(_SEL_REVIEW_CARD) or page.query_selector(_SEL_SORT_BTN):
            return True
        page.wait_for_timeout(step_ms)
    return bool(page.query_selector(_SEL_REVIEW_CARD) or page.query_selector(_SEL_SORT_BTN))


def _open_reviews(page, timeout_ms: int = 7000) -> bool:
    """Open the reviews list. Returns True once review cards are visible.

    Handles both Maps layouts: the simple place card (a "Reviews for ..." button)
    and the full /maps/place/ view (a "Reviews" role=tab that must be clicked).
    Note: the full layout limits automated sessions to a small number of cards;
    the histogram is the reliable, complete signal we depend on.
    """
    state = _wait_for_review_entry(page, timeout_ms=timeout_ms)
    if state == "none":
        return False
    if state == "card":
        return True

    # Click the Reviews tab/button so we land on the reviews feed (not the
    # overview preview). Retry a few times as it lazy-loads. The tab is clicked
    # FIRST so stray Overview preview cards can't be mistaken for the full feed.
    for _ in range(3):
        _dismiss_signin(page)
        btn = page.query_selector(_SEL_REVIEWS_BTN)
        if btn:
            try:
                human_click(page, btn)
                if _wait_for_review_feed(page, timeout_ms=5000):
                    return True
            except Exception:  # noqa: BLE001
                pass
        elif page.query_selector(_SEL_REVIEW_CARD):
            return True  # simple place-card layout: cards but no distinct tab
        else:
            state = _wait_for_review_entry(page, timeout_ms=2500, step_ms=300)
            if state == "card":
                return True
            if state == "none":
                return False

    if not page.query_selector(_SEL_REVIEW_CARD):
        # Fallback: nudge the panel to lazy-load the reviews section.
        for _ in range(5):
            _dismiss_signin(page)
            if _wait_for_review_feed(page, timeout_ms=700, step_ms=200):
                return True
            try:
                page.mouse.move(250, 400)
                page.mouse.wheel(0, 1500)
            except Exception:  # noqa: BLE001
                pass
            if _wait_for_review_feed(page, timeout_ms=1200, step_ms=250):
                return True

    return _wait_for_review_feed(page, timeout_ms=2000, step_ms=250)


def _sort_reviews(page, sort_by: str, settle_ms: int = 1800) -> bool:
    """Reorder the reviews feed via the Sort menu (best-effort, never raises).

    `sort_by` ∈ {relevant, newest, highest_rating, lowest_rating}. "relevant" is
    Google's default so it's a no-op. Sorting by newest gives cleaner age/trend
    reconstruction and deletion diffing; lowest_rating surfaces the 1★ reviews
    most likely to be purged. Returns True only if a non-default sort was applied.
    """
    idx = _SORT_INDEX.get(sort_by, 0)
    if not idx:  # 0 = relevant (default) or unknown -> nothing to do
        return False
    try:
        if not _wait_for_review_feed(page, timeout_ms=4000, step_ms=250):
            return False
        _dismiss_signin(page)
        # The Sort control renders a beat AFTER the first review cards, so a single
        # query races and misses it (-> no sort -> Google leaves the feed in its
        # lazy 5-card state and scrolling never loads more). Wait for it explicitly.
        try:
            btn = page.wait_for_selector(_SEL_SORT_BTN, timeout=5000)
        except Exception:  # noqa: BLE001
            btn = None
        if not btn:
            return False
        human_click(page, btn)
        page.wait_for_selector(_SEL_SORT_MENU_ITEM, timeout=2000)
        items = page.query_selector_all(_SEL_SORT_MENU_ITEM)
        if len(items) > idx:
            human_click(page, items[idx])
            _wait_for_review_feed(page, timeout_ms=max(settle_ms, 2000), step_ms=250)
            return True
    except Exception:  # noqa: BLE001 - sorting is optional, never break the scrape
        pass
    return False


def _scrape_histogram(page) -> list[int]:
    """Read the 1..5 star distribution from the aria-labelled bar chart."""
    hist = [0, 0, 0, 0, 0]
    for el in page.query_selector_all(_SEL_HIST_ROW):
        label = el.get_attribute("aria-label") or ""
        m = _HIST_RE.search(label)
        if m:
            star = int(m.group(1))
            count = int(m.group(2).replace(",", ""))
            if 1 <= star <= 5:
                hist[star - 1] = count
    return hist


def _scrape_deleted_notice(page) -> tuple[str, int, int]:
    """Best-effort parse of Google's native 'reviews removed' notice (mb4umi-style).

    Returns (raw_notice_line, min_count, max_count). Absent -> ("", 0, 0). When a
    notice is present but carries no number, min/max stay 0 and only the text is
    returned so the report can still surface "Google flags removed reviews here."
    """
    try:
        body = page.inner_text("body")
    except Exception:  # noqa: BLE001
        return "", 0, 0
    for raw in body.splitlines():
        line = raw.strip()
        if not line or len(line) > 240 or "review" not in line.lower():
            continue
        if not _NOTICE_HINT_RE.search(line):
            continue
        nums = [int(n.replace(",", "")) for n in _NUM_RE.findall(line)]
        if nums:
            return line, min(nums), max(nums)
        return line, 0, 0
    return "", 0, 0


def _find_scroll_container(page):
    """Return the scrollable element that holds review cards (varies by layout).

    Anchored on the current, durable review element (the "Actions for ..." button)
    rather than the retired `div.jftiEf` class. We climb to the reviews feed pane
    so `_scroll_reviews` can lazy-load beyond the initial ~5 cards.
    """
    return page.evaluate_handle(
        """() => {
            const card = document.querySelector('button[aria-label^="Actions for"]');
            if (!card) return null;
            // Prefer the known Maps reviews feed pane if it is an ancestor.
            const pane = card.closest('div[role="feed"], div.m6QErb.DxyBCb, div.m6QErb[aria-label]');
            if (pane && pane.scrollHeight > pane.clientHeight + 50) return pane;
            // Otherwise walk up to the first scrollable ancestor.
            let el = card.parentElement;
            while (el) {
                const s = getComputedStyle(el);
                if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
                    && el.scrollHeight > el.clientHeight + 50) return el;
                el = el.parentElement;
            }
            return null;
        }"""
    ).as_element()


def _scroll_reviews(page, max_passes: int, nap, fast: bool = False,
                    target_count: int = 0, stagnation_limit: int = 4) -> None:
    """Scroll the reviews container to lazy-load more cards until the feed is done.

    Collection stops on the first of: (a) every review promised by the rating
    histogram is loaded (``target_count``), (b) the card count stops growing for
    ``stagnation_limit`` consecutive passes (the feed is exhausted -- Google often
    stops serving well before the nominal total), or (c) the ``max_passes`` safety
    ceiling is hit (should rarely happen; it only guards against an endless loop).

    ``fast`` (used when attached to the user's genuine logged-in browser via CDP)
    skips the humanised mouse curve + chunked wheel + long politeness nap and just
    jumps the container to the bottom each pass, loading reviews as fast as Google
    will serve them. Bot-motion evasion is unnecessary in a real logged-in session.

    ``target_count`` (the total review count from the rating histogram) lets us
    stop the instant every review is loaded instead of waiting out the no-growth
    stability passes -- especially valuable for places with only a handful.
    """
    # The feed may report "ready" (Sort control present) a moment before the first
    # review card renders. Wait for a real card first so we don't bail with 0.
    first_deadline = time.time() + 6
    while time.time() < first_deadline:
        try:
            if page.is_closed():
                return
        except Exception:  # noqa: BLE001
            return
        if page.query_selector(_SEL_REVIEW_CARD):
            break
        _dismiss_signin(page)
        page.wait_for_timeout(300)

    # Expand into the full review list. The "More reviews (N)" control has no
    # aria-label on the full layout, so match it by visible text via a JS click.
    try:
        clicked = page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const b = btns.find(x => /more reviews/i.test((x.innerText||'')));
                if (b) { b.click(); return true; }
                return false;
            }"""
        )
        if clicked:
            page.wait_for_timeout(1500)
    except Exception:  # noqa: BLE001
        pass

    container = _find_scroll_container(page)
    prev_count = -1
    stable = 0
    for _ in range(max_passes):
        cards = page.query_selector_all(_SEL_REVIEW_CARD)
        # Early exit: we've loaded every review the rating histogram promised.
        if target_count and len(cards) >= target_count:
            break
        if len(cards) == prev_count:
            stable += 1
            if stable >= stagnation_limit:  # feed stopped growing -> move on
                break
        else:
            stable = 0
        prev_count = len(cards)

        # Fast path (real logged-in browser): jump straight to the bottom of the
        # feed to trigger the next lazy-load, with only a brief pause.
        if fast:
            container = _find_scroll_container(page)
            if container:
                try:
                    page.evaluate("(el) => el.scrollBy(0, el.scrollHeight)", container)
                except Exception:  # noqa: BLE001
                    pass
            elif not cards:
                break
            time.sleep(random.uniform(0.2, 0.45))
            continue

        # Human-like wheel scroll over the feed. Hovering the last card ensures
        # the reviews pane (not the map) receives the wheel events, and the motion
        # follows a natural curve/cadence instead of a mechanical jump.
        anchor = cards[-1] if cards else None
        human_scroll(page, random.uniform(1400, 2200), over=anchor)

        # If the humanised wheel isn't loading new cards, fall back to a direct
        # container jump to force the lazy-load (reliability over stealth).
        if stable >= 1 or not cards:
            container = _find_scroll_container(page)
            if container:
                try:
                    page.evaluate("(el) => el.scrollBy(0, el.scrollHeight)", container)
                except Exception:  # noqa: BLE001
                    pass
            elif not cards:
                break
        nap()



def _review_cards(page):
    """Yield (card_handle, author) for each review currently in the DOM.

    Reviews no longer carry a stable class or data-review-id, so we anchor on the
    per-review "Actions for <name>'s review" button and climb to the nearest
    ancestor that also contains the star rating. The author name is read straight
    from the anchor's aria-label (the most reliable signal available).
    """
    out = []
    for anchor in page.query_selector_all(_SEL_REVIEW_ANCHOR):
        try:
            label = anchor.get_attribute("aria-label") or ""
            m = _RE_ACTIONS_AUTHOR.match(label)
            author = m.group(1).strip() if m else ""
            handle = anchor.evaluate_handle(
                """el => {
                    let n = el;
                    for (let i = 0; i < 8 && n.parentElement; i++) {
                        n = n.parentElement;
                        if (n.querySelector('[role="img"][aria-label*="star"]')) return n;
                    }
                    return el.parentElement;
                }"""
            )
            card = handle.as_element()
            if card is not None:
                out.append((card, author))
        except Exception:  # noqa: BLE001
            continue
    return out


def _parse_card(card, place_id: str, now: datetime, author_hint: str = "") -> Review | None:
    try:
        author_el = card.query_selector(_SEL_AUTHOR)
        text_el = card.query_selector(_SEL_TEXT)
        rel_el = card.query_selector(_SEL_REL_DATE)
        rating_el = card.query_selector(_SEL_RATING)

        author = author_el.inner_text().strip() if author_el else ""
        text = text_el.inner_text().strip() if text_el else ""
        rel = rel_el.inner_text().strip() if rel_el else ""

        if not author:
            author = author_hint

        full = (card.inner_text() or "").replace("\n", " ").strip()
        if not author:
            parts = [p.strip() for p in re.split(r"\s{2,}|\|", full) if p.strip()]
            for part in parts:
                lower = part.lower()
                if any(token in lower for token in ("stars", "review", "ago", "local guide", "shared")):
                    continue
                if 2 <= len(part) <= 80 and not part.isdigit():
                    author = part
                    break
        if not text:
            text = full
            if author:
                text = text.replace(author, "", 1).strip(" .:-")
            text = text[:280]
        if not rel:
            m = re.search(r"(a\s+month\s+ago|\d+\s+(day|days|week|weeks|month|months|year|years)\s+ago|just\s+now|yesterday)", full, re.I)
            rel = m.group(0) if m else ""

        # Reviewer credibility signals (Local Guide + lifetime review count) and
        # whether the owner replied — all borrowed from the ScrapeBadger field set.
        meta_el = card.query_selector(_SEL_REVIEWER_META)
        meta = meta_el.inner_text().strip() if meta_el else ""
        is_local_guide = "local guide" in meta.lower()
        reviewer_review_count = 0
        mc = _REVIEWER_COUNT_RE.search(meta)
        if mc:
            reviewer_review_count = int(mc.group(1).replace(",", ""))
        resp_el = card.query_selector(_SEL_OWNER_RESP)
        owner_response = resp_el.inner_text().strip()[:280] if resp_el else ""

        rating = 0
        if rating_el:
            label = rating_el.get_attribute("aria-label") or ""
            for tok in label.split():
                if tok.replace(".", "").isdigit():
                    rating = int(float(tok))
                    break
        if rating == 0 and re.search(r"(\d(?:\.\d)?)\s*stars", full, re.I):
            m = re.search(r"(\d(?:\.\d)?)\s*stars", full, re.I)
            if m:
                rating = int(float(m.group(1)))

        if not author and not text:
            return None

        est = parse_relative_date(rel, now)
        return Review(
            review_hash=_review_hash(place_id, author, text[:120], rel),
            place_id=place_id,
            rating=rating,
            text_snippet=text[:280],
            author_name=author,
            est_review_date=est.isoformat() if est else "",
            raw_relative_date=rel,
            is_local_guide=is_local_guide,
            reviewer_review_count=reviewer_review_count,
            owner_response=owner_response,
        )
    except Exception:  # noqa: BLE001
        return None


def _rebuild_histogram(reviews: list[Review]) -> list[int]:
    hist = [0, 0, 0, 0, 0]
    for r in reviews:
        if 1 <= r.rating <= 5:
            hist[r.rating - 1] += 1
    return hist
