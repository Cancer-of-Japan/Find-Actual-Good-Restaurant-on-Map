"""Tier 1 review scraper: replay Google Maps' internal review-listing endpoint.

PROJECT.md §7a. Google's review pane is powered by an internal RPC endpoint,
`/maps/preview/review/listentitiesreviews`, which returns anti-XSSI-prefixed JSON
paginated by a token. Replaying it directly (reusing a real browser session's
cookies) reaches the full review set instead of the ~5 cards a scripted DOM
scroll is throttled to — without the DOM-scroll behavioral fingerprint.

IMPORTANT — calibration required (M2a):
    The `pb` protobuf layout and the response array indices below are
    version-specific and MUST be confirmed via the one-time headed-capture step
    in PROJECT.md §7a before Tier 1 will return data. Until then this module is
    defensive: any parsing/HTTP failure returns None so the caller cleanly falls
    back to the Tier 2 DOM scraper. Tier 1 is gated by `scraper.network_replay`
    (default false).

This module never raises to its caller; it returns a list of Review on success
or None on any failure/uncertainty.
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timezone

from .analyzer import parse_relative_date
from .models import Review

# Feature IDs look like `0x346835c174dcb7ff:0x2346504d6f22d15a` and appear in the
# `/maps/place/.../data=...!1s<fid>` form of a Maps URL.
_FEATURE_ID_RE = re.compile(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
_XSSI_PREFIX = ")]}'"

_ENDPOINT = "https://www.google.com/maps/preview/review/listentitiesreviews"

# Sort codes accepted by the endpoint: 1=most relevant, 2=newest, 3=highest,
# 4=lowest. Newest is most useful for age/trend analysis.
SORT_NEWEST = 2


def extract_feature_id(url: str) -> str | None:
    """Pull the `0x..:0x..` feature ID from a Maps URL, if present.

    cid-only URLs (`?cid=<decimal>`) do not carry the high 64 bits of the feature
    ID, so Tier 1 cannot be built from them — callers should keep a full
    `/maps/place/` URL for places they want Tier 1 to cover.
    """
    m = _FEATURE_ID_RE.search(url)
    return m.group(1) if m else None


def _build_pb(feature_id: str, page_token: str, sort: int) -> str:
    """Construct the protobuf-encoded `pb` query parameter.

    NOTE (M2a): confirm this template against a captured request before relying
    on it. Pagination is by the opaque `page_token` (empty string for page 1).
    """
    token_part = f"!2s{page_token}" if page_token else ""
    return (
        f"!1m6!1s{feature_id}"
        f"!6m4!4m1!1e1!4m1!1e3"
        f"!2m2!1i10{token_part}"
        f"!5m2!1sSESSION!7e81"
        f"!8m9!2b1!3b1!5b1!7b1!12m4!1b1!2b1!4m1!1e1!11m0!13m1!1e{sort}"
    )


def _parse_reviews(payload: str, place_id: str, now: datetime) -> tuple[list[Review], str | None]:
    """Strip the XSSI prefix, parse JSON, and extract reviews + next page token.

    Returns (reviews, next_token). Raises on malformed input so the caller can
    treat it as a Tier 1 failure and fall back. Field indices are best-effort and
    part of what M2a calibration must confirm.
    """
    body = payload.lstrip()
    if body.startswith(_XSSI_PREFIX):
        body = body[len(_XSSI_PREFIX):]
    data = json.loads(body)

    # Response shape (to confirm via capture): [ <meta>, [ <review>, ... ], <next_token> ]
    review_nodes = data[2] if len(data) > 2 and isinstance(data[2], list) else []
    next_token = data[1] if len(data) > 1 and isinstance(data[1], str) and data[1] else None

    reviews: list[Review] = []
    for node in review_nodes:
        rv = _parse_review_node(node, place_id, now)
        if rv:
            reviews.append(rv)
    return reviews, next_token


def _parse_review_node(node, place_id: str, now: datetime) -> Review | None:
    """Extract one review from its nested array. Defensive: returns None on any miss."""
    try:
        from .scraper import _review_hash  # reuse the canonical hash

        # These indices are the documented-community layout and must be verified
        # by M2a capture; wrapped in try/except so a schema change degrades to None.
        author = _first_str(node, ["name", "author"]) or ""
        text = _deep_first_text(node) or ""
        rel = _first_relative_date(node) or ""
        rating = _first_rating(node) or 0

        if not author and not text:
            return None

        est = parse_relative_date(rel, now)
        return Review(
            review_hash=_review_hash(place_id, author, text[:120], rel),
            place_id=place_id,
            rating=int(rating),
            text_snippet=text[:280],
            author_name=author,
            est_review_date=est.isoformat() if est else "",
            raw_relative_date=rel,
        )
    except Exception:  # noqa: BLE001
        return None


# --- best-effort field pickers over the nested arrays -------------------------
# Kept small and defensive; calibration (M2a) can replace these with exact paths.

def _iter_scalars(node):
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
        else:
            yield cur


def _first_str(node, _hints) -> str | None:
    for v in _iter_scalars(node):
        if isinstance(v, str) and 1 < len(v) < 80 and " " in v:
            return v
    return None


def _deep_first_text(node) -> str | None:
    best = None
    for v in _iter_scalars(node):
        if isinstance(v, str) and len(v) > (len(best) if best else 40):
            best = v
    return best


_REL_RE = re.compile(r"\b(a|an|\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago\b", re.I)


def _first_relative_date(node) -> str | None:
    for v in _iter_scalars(node):
        if isinstance(v, str) and _REL_RE.search(v):
            return v
    return None


def _first_rating(node) -> int | None:
    for v in _iter_scalars(node):
        if isinstance(v, int) and 1 <= v <= 5:
            return v
    return None


def fetch_reviews_via_replay(url: str, place_id: str, request, cfg: dict) -> list[Review] | None:
    """Attempt a full Tier 1 pull. Returns reviews on success, or None to fall back.

    `request` is a callable ``request(get_url) -> (status_code, text)`` — typically
    backed by a real browser session (Playwright ``context.request``) so cookies
    and consent state are valid. We paginate politely and stop immediately on any
    non-200, empty page, or parse failure (no aggressive retrying — §7a risk notes).
    """
    if not cfg.get("network_replay", False):
        return None

    feature_id = extract_feature_id(url)
    if not feature_id:
        return None  # cid-only URL: cannot build Tier 1 request

    min_d = float(cfg.get("min_delay", 2.0))
    max_d = float(cfg.get("max_delay", 5.0))
    max_pages = int(cfg.get("network_replay_max_pages", 20))
    now = datetime.now(timezone.utc)

    collected: dict[str, Review] = {}
    token = ""
    try:
        for _ in range(max_pages):
            pb = _build_pb(feature_id, token, SORT_NEWEST)
            get_url = f"{_ENDPOINT}?authuser=0&hl=en&gl=us&pb={pb}"
            status, text = request(get_url)
            if status != 200 or not text:
                break
            reviews, token = _parse_reviews(text, place_id, now)
            if not reviews:
                break
            for rv in reviews:
                collected[rv.review_hash] = rv
            if not token:
                break
            time.sleep(random.uniform(min_d, max_d))  # polite pagination delay
    except Exception:  # noqa: BLE001 - any failure => fall back to Tier 2
        return None

    return list(collected.values()) or None
