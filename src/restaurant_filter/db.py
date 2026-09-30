"""SQLite storage layer: schema creation + basic upserts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import Restaurant, Review, Snapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    place_id         TEXT PRIMARY KEY,
    name             TEXT,
    address          TEXT,
    lat              REAL,
    lng              REAL,
    maps_url         TEXT,
    first_tracked_at TEXT,
    note             TEXT,
    source           TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id           TEXT NOT NULL REFERENCES restaurants(place_id),
    taken_at           TEXT NOT NULL,
    agg_rating         REAL,
    total_review_count INTEGER,
    hist_1 INTEGER DEFAULT 0,
    hist_2 INTEGER DEFAULT 0,
    hist_3 INTEGER DEFAULT 0,
    hist_4 INTEGER DEFAULT 0,
    hist_5 INTEGER DEFAULT 0,
    scrape_source           TEXT DEFAULT 'dom_scroll_fallback',
    reviews_retrieved_count INTEGER DEFAULT 0,
    deleted_notice_min      INTEGER DEFAULT 0,
    deleted_notice_max      INTEGER DEFAULT 0,
    deleted_notice_text     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reviews (
    review_hash            TEXT PRIMARY KEY,
    place_id               TEXT NOT NULL REFERENCES restaurants(place_id),
    rating                 INTEGER,
    text_snippet           TEXT,
    author_name            TEXT,
    est_review_date        TEXT,
    raw_relative_date      TEXT,
    first_seen_snapshot_id INTEGER,
    last_seen_snapshot_id  INTEGER,
    status                 TEXT DEFAULT 'present',
    is_local_guide         INTEGER DEFAULT 0,
    reviewer_review_count  INTEGER DEFAULT 0,
    owner_response         TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_snapshots_place ON snapshots(place_id);
CREATE INDEX IF NOT EXISTS idx_reviews_place   ON reviews(place_id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (idempotent)."""
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(snapshots)")}
        adds = {
            "scrape_source": "TEXT DEFAULT 'dom_scroll_fallback'",
            "reviews_retrieved_count": "INTEGER DEFAULT 0",
            "deleted_notice_min": "INTEGER DEFAULT 0",
            "deleted_notice_max": "INTEGER DEFAULT 0",
            "deleted_notice_text": "TEXT DEFAULT ''",
        }
        for col, decl in adds.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {decl}")
        have_rest = {row["name"] for row in self.conn.execute("PRAGMA table_info(restaurants)")}
        if "source" not in have_rest:
            self.conn.execute("ALTER TABLE restaurants ADD COLUMN source TEXT DEFAULT ''")
        have_rev = {row["name"] for row in self.conn.execute("PRAGMA table_info(reviews)")}
        rev_adds = {
            "is_local_guide": "INTEGER DEFAULT 0",
            "reviewer_review_count": "INTEGER DEFAULT 0",
            "owner_response": "TEXT DEFAULT ''",
        }
        for col, decl in rev_adds.items():
            if col not in have_rev:
                self.conn.execute(f"ALTER TABLE reviews ADD COLUMN {col} {decl}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- restaurants -----------------------------------------------------------
    def upsert_restaurant(self, r: Restaurant) -> None:
        self.conn.execute(
            """
            INSERT INTO restaurants
                (place_id, name, address, lat, lng, maps_url, first_tracked_at, note, source)
            VALUES (:place_id, :name, :address, :lat, :lng, :maps_url, :first_tracked_at, :note, :source)
            ON CONFLICT(place_id) DO UPDATE SET
                name=excluded.name, address=excluded.address,
                lat=excluded.lat, lng=excluded.lng,
                maps_url=excluded.maps_url, note=excluded.note,
                -- keep the first-known provenance; only fill it if it was blank
                source=CASE WHEN restaurants.source IS NULL OR restaurants.source=''
                            THEN excluded.source ELSE restaurants.source END
            """,
            r.__dict__,
        )
        self.conn.commit()

    def get_restaurants(self) -> list[Restaurant]:
        rows = self.conn.execute("SELECT * FROM restaurants ORDER BY name").fetchall()
        return [Restaurant(**dict(row)) for row in rows]

    def has_restaurant(self, place_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM restaurants WHERE place_id = ?", (place_id,)
        ).fetchone()
        return row is not None

    # --- snapshots -------------------------------------------------------------
    def insert_snapshot(self, s: Snapshot) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO snapshots
                (place_id, taken_at, agg_rating, total_review_count,
                 hist_1, hist_2, hist_3, hist_4, hist_5,
                 scrape_source, reviews_retrieved_count,
                 deleted_notice_min, deleted_notice_max, deleted_notice_text)
            VALUES (:place_id, :taken_at, :agg_rating, :total_review_count,
                    :hist_1, :hist_2, :hist_3, :hist_4, :hist_5,
                    :scrape_source, :reviews_retrieved_count,
                    :deleted_notice_min, :deleted_notice_max, :deleted_notice_text)
            """,
            {k: v for k, v in s.__dict__.items() if k != "id"},
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_snapshots(self, place_id: str) -> list[Snapshot]:
        rows = self.conn.execute(
            "SELECT * FROM snapshots WHERE place_id = ? ORDER BY taken_at",
            (place_id,),
        ).fetchall()
        return [Snapshot(**dict(row)) for row in rows]

    # --- reviews ---------------------------------------------------------------
    def get_review_hashes(self, place_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT review_hash FROM reviews WHERE place_id = ?", (place_id,)
        ).fetchall()
        return {row["review_hash"] for row in rows}

    def upsert_review(self, rv: Review) -> None:
        self.conn.execute(
            """
            INSERT INTO reviews
                (review_hash, place_id, rating, text_snippet, author_name,
                 est_review_date, raw_relative_date,
                 first_seen_snapshot_id, last_seen_snapshot_id, status,
                 is_local_guide, reviewer_review_count, owner_response)
            VALUES (:review_hash, :place_id, :rating, :text_snippet, :author_name,
                    :est_review_date, :raw_relative_date,
                    :first_seen_snapshot_id, :last_seen_snapshot_id, :status,
                    :is_local_guide, :reviewer_review_count, :owner_response)
            ON CONFLICT(review_hash) DO UPDATE SET
                last_seen_snapshot_id=excluded.last_seen_snapshot_id,
                status='present',
                is_local_guide=excluded.is_local_guide,
                reviewer_review_count=excluded.reviewer_review_count,
                owner_response=excluded.owner_response
            """,
            rv.__dict__,
        )
        self.conn.commit()

    def get_reviews(self, place_id: str) -> list[Review]:
        rows = self.conn.execute(
            "SELECT * FROM reviews WHERE place_id = ? ORDER BY est_review_date",
            (place_id,),
        ).fetchall()
        return [Review(**dict(row)) for row in rows]

    def mark_missing_reviews(self, place_id: str, present_hashes: set[str], snapshot_id: int) -> int:
        """Flag reviews previously seen but absent from the current scrape (§7.3 Mode A)."""
        existing = self.get_review_hashes(place_id)
        missing = existing - present_hashes
        for h in missing:
            self.conn.execute(
                "UPDATE reviews SET status = ? WHERE review_hash = ? AND status = 'present'",
                (f"missing_since_{snapshot_id}", h),
            )
        self.conn.commit()
        return len(missing)
