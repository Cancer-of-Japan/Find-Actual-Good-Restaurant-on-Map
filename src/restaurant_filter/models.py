"""Domain models mirroring the SQLite schema (PROJECT.md §6)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Restaurant:
    place_id: str
    name: str = ""
    address: str = ""
    lat: float | None = None
    lng: float | None = None
    maps_url: str = ""
    first_tracked_at: str = ""  # ISO 8601 UTC
    note: str = ""
    source: str = ""  # how it entered the DB: seed | link | area | grid


@dataclass
class Snapshot:
    place_id: str
    taken_at: str  # ISO 8601 UTC
    agg_rating: float | None = None
    total_review_count: int | None = None
    hist_1: int = 0
    hist_2: int = 0
    hist_3: int = 0
    hist_4: int = 0
    hist_5: int = 0
    # Provenance (PROJECT.md §6 / §7a): which scraper tier produced this snapshot
    # and how many reviews it actually parsed, so the report can flag partial data.
    scrape_source: str = "dom_scroll_fallback"  # "network_replay" | "dom_scroll_fallback"
    reviews_retrieved_count: int = 0
    # §7.3: Google's own "reviews removed" notice, if shown on the page. Unlike the
    # scrape/reported volume gap, this is an authoritative first-run purge signal.
    deleted_notice_min: int = 0
    deleted_notice_max: int = 0
    deleted_notice_text: str = ""
    id: int | None = None  # assigned by DB

    @property
    def histogram(self) -> list[int]:
        return [self.hist_1, self.hist_2, self.hist_3, self.hist_4, self.hist_5]


@dataclass
class Review:
    review_hash: str  # hash(author + text + relative-date-at-first-seen)
    place_id: str
    rating: int
    text_snippet: str = ""
    author_name: str = ""
    est_review_date: str = ""  # ISO 8601 date estimate
    raw_relative_date: str = ""  # e.g. "3 weeks ago"
    first_seen_snapshot_id: int | None = None
    last_seen_snapshot_id: int | None = None
    status: str = "present"  # "present" | "missing_since_<snapshot_id>"
    # Reviewer credibility + engagement signals (ScrapeBadger field set / §7.7).
    is_local_guide: bool = False
    reviewer_review_count: int = 0  # author's lifetime review count, 0 if unknown
    owner_response: str = ""  # owner reply text (presence => business responded)


@dataclass
class ScrapeResult:
    """What the scraper returns for one restaurant."""

    place_id: str
    reviews: list[Review] = field(default_factory=list)
    histogram: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    reported_total: int | None = None  # total shown on the Maps page, if visible
    # Which tier produced these reviews (PROJECT.md §7a).
    source: str = "dom_scroll_fallback"  # "network_replay" | "dom_scroll_fallback"
    # §7.3 Google's native "reviews removed" notice, parsed from the page text.
    deleted_notice_min: int = 0
    deleted_notice_max: int = 0
    deleted_notice_text: str = ""

    @property
    def retrieved_count(self) -> int:
        return len(self.reviews)
