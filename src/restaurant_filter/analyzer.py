"""Detection algorithms (PROJECT.md §7). Pure functions where possible."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any

from .models import Review, Snapshot

# --- relative date parsing (§7.1 / §7.2) --------------------------------------

_REL_RE = re.compile(r"(?:a|an|(\d+))\s+(second|minute|hour|day|week|month|year)s?\s+ago")
_UNIT_DAYS = {
    "second": 1 / 86400,
    "minute": 1 / 1440,
    "hour": 1 / 24,
    "day": 1,
    "week": 7,
    "month": 30.44,
    "year": 365.25,
}


def parse_relative_date(relative: str, reference: datetime | None = None) -> date | None:
    """Convert 'a month ago' / '3 weeks ago' into an estimated absolute date."""
    ref = reference or datetime.now(timezone.utc)
    m = _REL_RE.search(relative.strip().lower())
    if not m:
        return None
    qty = int(m.group(1)) if m.group(1) else 1
    unit = m.group(2)
    return (ref - timedelta(days=qty * _UNIT_DAYS[unit])).date()


# --- results container --------------------------------------------------------

@dataclass
class Analysis:
    place_id: str
    age_proxy_date: str | None = None
    age_confidence: str = "Low"
    trend_buckets: list[dict[str, Any]] = field(default_factory=list)
    long_term_decline: float = 0.0       # early-avg minus recent-avg (stars)
    long_term_improvement: float = 0.0
    volume_mismatch: float = 0.0         # 0..1 fraction of reviews not surfaced
    jump_anomaly: float = 0.0            # max positive rating delta over history
    deleted_negative_skew: float = 0.0   # 0..1 how negative the missing reviews were
    deleted_notice: float = 0.0          # 0..1 from Google's own "reviews removed" notice
    deleted_notice_count: int = 0        # max count parsed from that notice
    adjusted_rating: float | None = None  # rating if deleted reviews were all 1★
    polarization: float = 0.0            # (hist_1 + hist_5) / total
    low_credibility: float = 0.0         # 0..1 share of vettable 4-5★ from throwaway accounts
    low_credibility_count: int = 0       # how many such reviews (for the reason text)
    suspicion_score: int = 0
    reasons: list[str] = field(default_factory=list)


# --- §7.1 age proxy -----------------------------------------------------------

def estimate_age(reviews: list[Review], low_conf_count: int) -> tuple[str | None, str]:
    dates = [r.est_review_date for r in reviews if r.est_review_date]
    if not dates:
        return None, "Low"
    earliest = min(dates)
    if len(reviews) < low_conf_count:
        confidence = "Low"
    elif len(reviews) < low_conf_count * 3:
        confidence = "Medium"
    else:
        confidence = "High"
    return earliest, confidence


# --- §7.2 retrospective trend -------------------------------------------------

def retrospective_trend(
    reviews: list[Review], decline_threshold: float, recent_months: int
) -> tuple[list[dict[str, Any]], float, float]:
    dated = [r for r in reviews if r.est_review_date and r.rating]
    if len(dated) < 4:
        return [], 0.0, 0.0

    dated.sort(key=lambda r: r.est_review_date)
    # Bucket by calendar year for the chart.
    buckets: dict[str, list[int]] = {}
    for r in dated:
        year = r.est_review_date[:4]
        buckets.setdefault(year, []).append(r.rating)
    bucket_rows = [
        {"period": yr, "avg": round(mean(vals), 2), "count": len(vals)}
        for yr, vals in sorted(buckets.items())
    ]

    # Early vs recent comparison.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_months * 30.44)).date().isoformat()
    recent = [r.rating for r in dated if r.est_review_date >= cutoff]
    early = [r.rating for r in dated if r.est_review_date < cutoff]
    decline = improvement = 0.0
    if early and recent:
        diff = mean(early) - mean(recent)
        if diff >= decline_threshold:
            decline = round(diff, 2)
        elif -diff >= decline_threshold:
            improvement = round(-diff, 2)
    return bucket_rows, decline, improvement


# --- §7.3 Mode B volume mismatch ----------------------------------------------

def volume_mismatch(reported_total: int | None, scraped_count: int) -> float:
    if not reported_total or reported_total <= 0:
        return 0.0
    gap = max(0, reported_total - scraped_count)
    return round(min(1.0, gap / reported_total), 3)


def deleted_notice_ratio(deleted_max: int, reported_total: int | None) -> float:
    """Fraction of the (would-be) review pool that Google's notice says was removed.

    Uses `deleted / (reported_total + deleted)` so it stays in 0..1. Unlike
    `volume_mismatch`, this reads Google's *own* removal notice, so it is a valid
    first-run signal regardless of scrape tier.
    """
    if deleted_max <= 0 or not reported_total or reported_total <= 0:
        return 0.0
    return round(min(1.0, deleted_max / (reported_total + deleted_max)), 3)


def adjusted_rating_estimate(
    agg_rating: float | None, reported_total: int | None, deleted_estimate: int
) -> float | None:
    """mb4umi-style 'real score': assume every removed review would have been 1★.

    ((rating * total) + deleted*1) / (total + deleted). Returns the original
    rating when there is nothing to adjust.
    """
    if agg_rating is None or not reported_total or reported_total <= 0:
        return agg_rating
    if deleted_estimate <= 0:
        return round(agg_rating, 2)
    denom = reported_total + deleted_estimate
    return round((agg_rating * reported_total + deleted_estimate) / denom, 2)


# --- §7.4 jump detection ------------------------------------------------------

def jump_anomaly(snapshots: list[Snapshot], delta_thresh: float, window_days: int) -> float:
    ordered = [s for s in snapshots if s.agg_rating is not None]
    if len(ordered) < 2:
        return 0.0
    worst = 0.0
    for prev, cur in zip(ordered, ordered[1:]):
        d_rating = (cur.agg_rating or 0) - (prev.agg_rating or 0)
        try:
            t_prev = datetime.fromisoformat(prev.taken_at)
            t_cur = datetime.fromisoformat(cur.taken_at)
            d_days = max(1, (t_cur - t_prev).days)
        except ValueError:
            d_days = window_days
        if d_rating >= delta_thresh and d_days <= window_days:
            worst = max(worst, d_rating)
    return round(worst, 3)


# --- §7.5 bimodality ----------------------------------------------------------

def polarization(histogram: list[int]) -> float:
    total = sum(histogram)
    if total == 0:
        return 0.0
    return round((histogram[0] + histogram[4]) / total, 3)


# --- §7.7 reviewer credibility -------------------------------------------------

def low_credibility_inflation(
    reviews: list[Review], min_reviews: int = 2
) -> tuple[float, int]:
    """Share of high-star reviews that came from low-credibility accounts.

    A high-star (>=4★) review counts as low-credibility when its author is *not* a
    Local Guide and has a parsed lifetime review count of 1..`min_reviews` (a
    throwaway-looking account). Only reviews we could actually vet (Local Guide
    known, or a parsed count > 0) enter the denominator, so unparsed metadata
    never inflates the flag. Returns (ratio, count).
    """
    pool = [
        r for r in reviews
        if r.status == "present" and r.rating >= 4
        and (r.is_local_guide or r.reviewer_review_count > 0)
    ]
    if not pool:
        return 0.0, 0
    low = [
        r for r in pool
        if not r.is_local_guide and 0 < r.reviewer_review_count <= min_reviews
    ]
    return round(len(low) / len(pool), 3), len(low)


# --- §7.6 composite suspicion score ------------------------------------------

def suspicion_score(a: Analysis, agg_rating: float | None, total_reviews: int | None,
                    age_proxy_date: str | None, weights: dict[str, float],
                    analysis_cfg: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    factors: dict[str, float] = {}

    # young + high rating
    young_high = 0.0
    if age_proxy_date and agg_rating is not None:
        try:
            age_days = (date.today() - date.fromisoformat(age_proxy_date)).days
        except ValueError:
            age_days = 9999
        if age_days < 90 and agg_rating >= 4.5:
            young_high = 1.0
            reasons.append(f"Young ({age_days}d) but rated {agg_rating}\u2605")
    factors["young_and_high_rating"] = young_high

    if a.long_term_decline > 0:
        factors["long_term_decline"] = min(1.0, a.long_term_decline / 2.0)
        reasons.append(f"Long-term decline of {a.long_term_decline}\u2605 (early vs recent)")

    if a.jump_anomaly > 0:
        factors["jump_anomaly"] = min(1.0, a.jump_anomaly / 1.0)
        reasons.append(f"Sudden rating jump of +{a.jump_anomaly}\u2605")

    if a.deleted_negative_skew > 0:
        factors["deleted_reviews_negative"] = a.deleted_negative_skew
        reasons.append("Deleted reviews skew negative")

    if a.volume_mismatch > 0.2:
        factors["possible_purge_volume_mismatch"] = a.volume_mismatch
        reasons.append(f"{int(a.volume_mismatch * 100)}% of reviews not surfaced (unconfirmed)")

    if a.deleted_notice > 0:
        factors["deleted_notice"] = a.deleted_notice
        if a.deleted_notice_count > 0:
            reasons.append(
                f"Google's notice reports ~{a.deleted_notice_count} removed review(s)"
                + (f"; adjusted ≈ {a.adjusted_rating}★" if a.adjusted_rating is not None else "")
            )
        else:
            reasons.append("Google flags that reviews were removed here")

    p_low = analysis_cfg.get("polarization_rating_low", 3.5)
    p_high = analysis_cfg.get("polarization_rating_high", 4.2)
    p_thresh = analysis_cfg.get("polarization_flag_threshold", 0.6)
    if a.polarization >= p_thresh and agg_rating is not None and p_low <= agg_rating <= p_high:
        factors["polarization"] = a.polarization
        reasons.append(f"Polarizing distribution ({int(a.polarization * 100)}% at 1\u2605/5\u2605)")
    if a.low_credibility > 0:
        factors["low_credibility_reviews"] = a.low_credibility
        reasons.append(
            f"{int(a.low_credibility * 100)}% of vettable 4–5★ reviews from "
            f"low-credibility accounts ({a.low_credibility_count})"
        )
    total_weight = sum(weights.get(k, 0) for k in factors) or 1.0
    weighted = sum(weights.get(k, 0) * v for k, v in factors.items())
    max_possible = sum(weights.get(k, 0) for k in weights) or 1.0
    score = int(round(100 * weighted / max_possible))
    return min(100, score), reasons
