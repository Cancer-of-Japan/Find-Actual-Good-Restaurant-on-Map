"""Pure-function tests for the analyzer (no network / no DB)."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from restaurant_filter import analyzer as az
from restaurant_filter import scraper as sc
from restaurant_filter.models import Review, Snapshot


def _review(rating: int, days_ago: int) -> Review:
    d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
    return Review(review_hash=f"h{rating}{days_ago}", place_id="p", rating=rating,
                  est_review_date=d)


def test_parse_relative_date():
    ref = datetime(2026, 9, 24, tzinfo=timezone.utc)
    assert az.parse_relative_date("3 weeks ago", ref) == date(2026, 9, 3)
    assert az.parse_relative_date("a month ago", ref) is not None
    assert az.parse_relative_date("just now") is None


def test_polarization():
    assert az.polarization([50, 0, 0, 0, 50]) == 1.0
    assert az.polarization([0, 0, 0, 0, 0]) == 0.0


def test_volume_mismatch():
    assert az.volume_mismatch(100, 40) == 0.6
    assert az.volume_mismatch(None, 40) == 0.0
    assert az.volume_mismatch(50, 60) == 0.0


def test_retrospective_decline_detected():
    reviews = [_review(5, 1500), _review(5, 1400), _review(2, 20), _review(1, 10)]
    buckets, decline, improve = az.retrospective_trend(reviews, 0.7, 12)
    assert decline > 0
    assert improve == 0.0
    assert len(buckets) >= 1


def test_jump_anomaly():
    now = datetime.now(timezone.utc)
    s1 = Snapshot(place_id="p", taken_at=(now - timedelta(days=10)).isoformat(), agg_rating=3.5)
    s2 = Snapshot(place_id="p", taken_at=now.isoformat(), agg_rating=4.6)
    assert az.jump_anomaly([s1, s2], 0.3, 30) > 0


def test_estimate_age_confidence():
    reviews = [_review(5, 100)]
    _, conf = az.estimate_age(reviews, low_conf_count=10)
    assert conf == "Low"


def test_review_selectors_match_current_maps_dom():
    assert 'Reviews' in sc._SEL_REVIEWS_BTN
    assert 'Actions for' in sc._SEL_REVIEW_CARD
    assert sc._SEL_REVIEWS_BTN.startswith('button[role="tab"][aria-label*="Reviews"]')
