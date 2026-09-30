"""Human-like mouse movement for Playwright, powered by HumanCursor.

HumanCursor's public cursors are bound to other stacks: ``WebCursor`` drives a
Selenium WebDriver and ``SystemCursor`` drives the physical OS mouse via
pyautogui. Neither works with Playwright, so we reuse the piece that actually
matters for bot-detection evasion: the natural-motion curve generator
(:class:`HumanizeMouseTrajectory`). We feed its Bezier points into Playwright's
``page.mouse`` so every hover / click / scroll follows a human-looking path with
variable speed, curvature and jitter instead of a teleport.

Only the pure ``numpy``/``pytweening`` curve submodule is imported here -- NOT
the top-level ``humancursor`` package, which would eagerly import selenium and
pyautogui. If HumanCursor (or numpy) isn't installed, every helper degrades to a
plain Playwright move/click so the scraper never hard-depends on the package.
"""

from __future__ import annotations

import random
import time

try:  # Import ONLY the curve generator (numpy + pytweening), not the heavy pkg.
    from humancursor.utilities.human_curve_generator import (  # type: ignore
        HumanizeMouseTrajectory,
    )

    _HC_AVAILABLE = True
except Exception:  # noqa: BLE001 - optional dependency; fall back gracefully
    HumanizeMouseTrajectory = None  # type: ignore
    _HC_AVAILABLE = False


def humancursor_available() -> bool:
    """True when HumanCursor's curve generator imported successfully."""
    return _HC_AVAILABLE


def _viewport(page) -> tuple[float, float]:
    try:
        vp = page.viewport_size or {}
    except Exception:  # noqa: BLE001
        vp = {}
    return float(vp.get("width", 1280)), float(vp.get("height", 800))


def _get_pos(page) -> tuple[float, float]:
    """Current mouse position (Playwright doesn't expose it, so we track it)."""
    pos = getattr(page, "_hc_pos", None)
    if pos is None:
        w, h = _viewport(page)
        pos = (w * 0.5, h * 0.5)
    return pos


def _set_pos(page, x: float, y: float) -> None:
    try:
        page._hc_pos = (float(x), float(y))
    except Exception:  # noqa: BLE001
        pass


def _lerp(a: tuple[float, float], b: tuple[float, float], steps: int):
    return [
        (a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps)
        for i in range(1, steps + 1)
    ]


def human_move(page, x: float, y: float, *, target_points: int = 60) -> None:
    """Move the mouse to ``(x, y)`` along a human-like curve (best-effort)."""
    start = _get_pos(page)
    curve = None
    if _HC_AVAILABLE:
        try:
            curve = HumanizeMouseTrajectory(
                (float(start[0]), float(start[1])),
                (float(x), float(y)),
                target_points=target_points,
                distortion_mean=1.2,
                distortion_st_dev=1.0,
                distortion_frequency=0.5,
                knots_count=2,
            ).points
        except Exception:  # noqa: BLE001
            curve = None
    if not curve:
        curve = _lerp(start, (x, y), steps=24)
    for px, py in curve:
        try:
            page.mouse.move(px, py)
        except Exception:  # noqa: BLE001
            break
        time.sleep(random.uniform(0.001, 0.006))
    _set_pos(page, x, y)


def _element_point(element) -> tuple[float, float] | None:
    """A slightly-jittered point inside an element's box (not dead center)."""
    try:
        box = element.bounding_box()
    except Exception:  # noqa: BLE001
        return None
    if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        return None
    cx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
    cy = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    return cx, cy


def human_click(page, element) -> bool:
    """Approach an element with a human curve, then press+release. Best-effort.

    Returns True if a click was dispatched (either the humanised mouse click or a
    plain ``element.click()`` fallback), False otherwise.
    """
    try:
        element.scroll_into_view_if_needed(timeout=3000)
    except Exception:  # noqa: BLE001
        pass
    pt = _element_point(element)
    if pt is None:
        try:
            element.click()
            return True
        except Exception:  # noqa: BLE001
            return False
    human_move(page, pt[0], pt[1])
    time.sleep(random.uniform(0.03, 0.10))
    try:
        page.mouse.down()
        time.sleep(random.uniform(0.04, 0.12))
        page.mouse.up()
        return True
    except Exception:  # noqa: BLE001
        try:
            element.click()
            return True
        except Exception:  # noqa: BLE001
            return False


def human_scroll(page, dy: float, *, over=None) -> None:
    """Human-ish wheel scroll: hover the target, then wheel in uneven steps.

    ``page.mouse.wheel`` scrolls whatever is under the cursor, so we first move
    over ``over`` (e.g. a review card) to ensure the reviews feed -- not the map
    -- receives the wheel events. The scroll is broken into a few uneven chunks
    with short pauses to mimic a human flick rather than one instant jump.
    """
    if over is not None:
        pt = _element_point(over)
        if pt:
            human_move(page, pt[0], pt[1])
    remaining = float(dy)
    guard = 0
    while abs(remaining) > 1 and guard < 12:
        guard += 1
        step = remaining * random.uniform(0.3, 0.6)
        try:
            page.mouse.wheel(0, step)
        except Exception:  # noqa: BLE001
            break
        remaining -= step
        time.sleep(random.uniform(0.03, 0.12))
