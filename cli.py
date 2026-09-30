"""Command-line entry point for the Restaurant Trust Filter.

Usage:
    python cli.py add-area --query "ramen" --location "Taipei" --radius 2000
    python cli.py add-link "https://maps.google.com/?q=place_id:ChIJ..."
    python cli.py seed                 # import config/restaurants.yaml
    python cli.py snapshot [--only place_id1,place_id2] [--no-scrape]
    python cli.py report
    python cli.py list
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make src/ importable without installation.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from restaurant_filter import analyzer as az  # noqa: E402
from restaurant_filter.config import load_settings  # noqa: E402
from restaurant_filter.db import Database  # noqa: E402
from restaurant_filter.models import Restaurant, Snapshot  # noqa: E402
from restaurant_filter.places_api import (  # noqa: E402
    PlacesClient,
    extract_place_id,
)
from restaurant_filter.report_builder import build_report  # noqa: E402
from restaurant_filter.scraper import scrape_place  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _restaurant_from_details(details: dict, maps_url: str, note: str = "", source: str = "") -> Restaurant:
    loc = details.get("location", {})
    return Restaurant(
        place_id=details.get("id", ""),
        name=(details.get("displayName") or {}).get("text", ""),
        address=details.get("formattedAddress", ""),
        lat=loc.get("latitude"),
        lng=loc.get("longitude"),
        maps_url=details.get("googleMapsUri", maps_url),
        first_tracked_at=_now(),
        note=note,
        source=source,
    )


# --- commands -----------------------------------------------------------------

def cmd_add_area(args, settings) -> None:
    client = PlacesClient(settings.require_api_key())
    if getattr(args, "grid", False):
        _add_area_grid(args, settings, client)
        return
    if not args.query:
        print("add-area needs --query (text search) or --grid (area tiling).")
        return
    query = f"{args.query} in {args.location}" if args.location else args.query
    places = client.search_text(query, max_pages=3)  # up to ~60 results
    with Database(settings.db_path) as db:
        for pl in places:
            r = _restaurant_from_details(pl, pl.get("googleMapsUri", ""), source="area")
            if r.place_id:
                db.upsert_restaurant(r)
                print(f"  + {r.name} ({r.place_id})")
    print(f"Added {len(places)} restaurant(s) from area search.")


def _add_area_grid(args, settings, client) -> None:
    """§7b grid discovery: tile the area, query each tile, merge + de-dupe."""
    from restaurant_filter.discovery import DiscoveryConfig, discover_area, estimate

    cfg = DiscoveryConfig.from_dict(settings.raw.get("discovery"))
    if getattr(args, "block_size", None):
        cfg.block_size_m = float(args.block_size)

    # Resolve a center point: explicit --lat/--lng, else geocode via Text Search.
    if args.lat is not None and args.lng is not None:
        clat, clng = args.lat, args.lng
    else:
        seed_q = (f"{args.query} in {args.location}" if args.query and args.location
                  else (args.query or args.location))
        if not seed_q:
            print("Grid needs --lat/--lng or a --location/--query to resolve a center.")
            return
        seed = client.search_text(seed_q, max_pages=1)
        loc = (seed[0].get("location") if seed else None) or {}
        clat, clng = loc.get("latitude"), loc.get("longitude")
        if clat is None or clng is None:
            print(f"Could not resolve a center from '{seed_q}'; pass --lat/--lng.")
            return
        print(f"Center resolved to {clat:.5f},{clng:.5f} via '{seed_q}'.")

    est = estimate(clat, clng, float(args.radius), cfg)
    print(f"Grid plan: block_size={cfg.block_size_m:.0f}m radius={args.radius}m "
          f"overlap={cfg.tile_overlap_factor} -> ~{est['tiles']} tiles "
          f"(>= {est['min_api_calls']} API calls before any re-splits).")
    if getattr(args, "dry_run", False):
        print("Dry run - no API calls made.")
        return

    itype = args.type or "restaurant"

    def _search(lat, lng, radius_m):
        return client.search_nearby(lat, lng, radius_m, included_types=[itype])

    def _on_tile(tile, n):
        print(f"  tile {tile.lat:.4f},{tile.lng:.4f} r={tile.radius_m:.0f}m d{tile.depth} -> {n}")

    res = discover_area(_search, clat, clng, float(args.radius), cfg, on_tile=_on_tile)
    added = 0
    with Database(settings.db_path) as db:
        for pl in res.places.values():
            r = _restaurant_from_details(pl, pl.get("googleMapsUri", ""), source="grid")
            if r.place_id:
                db.upsert_restaurant(r)
                added += 1
    print(f"Grid discovery: {res.tiles_queried} tiles queried, "
          f"{res.saturated_tiles} saturated/re-split, "
          f"{len(res.places)} unique restaurants, {added} upserted.")


def cmd_add_link(args, settings) -> None:
    place_id = extract_place_id(args.url)
    client = PlacesClient(settings.require_api_key())
    if not place_id:
        print("Could not read a place_id from the URL. Trying a name-based search is not "
              "supported for cid= links yet; paste a '...place_id:...' link instead.")
        return
    details = client.place_details(place_id)
    r = _restaurant_from_details(details, args.url, args.note or "", source="link")
    with Database(settings.db_path) as db:
        db.upsert_restaurant(r)
    print(f"Added: {r.name} ({r.place_id})")


def cmd_seed(args, settings) -> None:
    seed_path = ROOT / "config" / "restaurants.yaml"
    if not seed_path.exists():
        print(f"No {seed_path.name}. Copy config/restaurants.example.yaml to it first.")
        return
    data = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
    entries = data.get("restaurants", [])
    client = PlacesClient(settings.require_api_key())
    added = 0
    with Database(settings.db_path) as db:
        for entry in entries:
            url = entry.get("url", "")
            place_id = extract_place_id(url)
            if not place_id:
                print(f"  ! skipping (no place_id in URL): {url}")
                continue
            details = client.place_details(place_id)
            r = _restaurant_from_details(details, url, entry.get("note", ""), source="seed")
            db.upsert_restaurant(r)
            added += 1
            print(f"  + {r.name}")
    print(f"Seeded {added} restaurant(s).")


def cmd_snapshot(args, settings) -> None:
    client = PlacesClient(settings.require_api_key())
    only = set(args.only.split(",")) if args.only else None
    gap = float(settings.scraper.get("between_places_delay", 0))
    with Database(settings.db_path) as db:
        restaurants = db.get_restaurants()
        if only:
            restaurants = [r for r in restaurants if r.place_id in only]
        if getattr(args, "retry_missing", False):
            restaurants = [
                r for r in restaurants
                if not (db.get_snapshots(r.place_id) and sum(db.get_snapshots(r.place_id)[-1].histogram) > 0)
            ]
            print(f"Retrying {len(restaurants)} restaurant(s) with an empty histogram.")
        if not restaurants:
            print("No restaurants tracked. Use add-link / add-area / seed first.")
            return

        # The DOM-scroll fallback sometimes stalls on the first ~5 lazy-loaded
        # cards when the feed pane isn't ready yet. Rather than blocking the whole
        # batch retrying in place, we defer those places and re-run them once at
        # the end (by which time throttling has eased). A place whose *true* total
        # is <= this count isn't stuck, so it is never deferred.
        STUCK_COUNT = 5

        def _process(r) -> dict | None:
            """Snapshot one restaurant. Returns a status dict, or None if skipped."""
            # Place Details gives the current aggregate rating/count. If the
            # network/proxy blocks the API, fall back to the last snapshot so
            # scraping (histogram + reviews) can still proceed.
            agg_rating = None
            total_count = None
            try:
                details = client.place_details(r.place_id)
                agg_rating = details.get("rating")
                total_count = details.get("userRatingCount")
            except Exception as exc:  # noqa: BLE001
                prev = db.get_snapshots(r.place_id)
                if prev:
                    agg_rating = prev[-1].agg_rating
                    total_count = prev[-1].total_review_count
                print(f"  ! API unavailable ({type(exc).__name__}); "
                      f"using last known rating {agg_rating}/{total_count}")

            snap = Snapshot(
                place_id=r.place_id,
                taken_at=_now(),
                agg_rating=agg_rating,
                total_review_count=total_count,
            )
            if args.no_scrape:
                db.insert_snapshot(snap)
                return None

            sr = scrape_place(r.maps_url, r.place_id, settings.scraper)
            snap.hist_1, snap.hist_2, snap.hist_3, snap.hist_4, snap.hist_5 = sr.histogram
            snap.scrape_source = sr.source
            snap.reviews_retrieved_count = sr.retrieved_count
            snap.deleted_notice_min = sr.deleted_notice_min
            snap.deleted_notice_max = sr.deleted_notice_max
            snap.deleted_notice_text = sr.deleted_notice_text
            snap_id = db.insert_snapshot(snap)
            present = set()
            for rv in sr.reviews:
                rv.first_seen_snapshot_id = rv.first_seen_snapshot_id or snap_id
                rv.last_seen_snapshot_id = snap_id
                db.upsert_review(rv)
                present.add(rv.review_hash)
            if present:
                missing = db.mark_missing_reviews(r.place_id, present, snap_id)
                print(f"  scraped {len(sr.reviews)} reviews via {sr.source}, "
                      f"{missing} newly missing")
            else:
                print("  scraped 0 reviews (panel not loaded / limited)")
            if sr.deleted_notice_text:
                est = sr.deleted_notice_max or "unspecified"
                print(f"  ⚠ Google removal notice (~{est}): {sr.deleted_notice_text}")
            return {"scraped": len(sr.reviews), "total": total_count, "source": sr.source}

        def _looks_stuck(status: dict | None) -> bool:
            """True when a DOM scrape stalled at ~5 cards but the place has more."""
            if not status or status["source"] != "dom_scroll_fallback":
                return False
            total = status["total"]
            return status["scraped"] == STUCK_COUNT and (total is None or total > STUCK_COUNT)

        deferred = []
        for idx, r in enumerate(restaurants):
            if idx > 0 and gap > 0 and not args.no_scrape:
                time.sleep(gap)  # polite pause between places to avoid throttling
            print(f"Snapshotting {r.name or r.place_id} ...")
            try:
                status = _process(r)
            except Exception as exc:  # noqa: BLE001 - keep the batch going on transient errors
                print(f"  ! skipped ({type(exc).__name__}): {exc}")
                continue
            if _looks_stuck(status):
                print(f"  ↻ only {STUCK_COUNT} reviews (of {status['total']}); "
                      "deferring for a second pass.")
                deferred.append(r)

        # Second pass: re-run the places that stalled at 5 cards, now that the
        # batch is done and throttling has likely relaxed. Only one extra attempt
        # each; whatever we get the second time is accepted.
        if deferred:
            print(f"\nRe-running {len(deferred)} deferred place(s) that stalled at "
                  f"{STUCK_COUNT} reviews ...")
            for idx, r in enumerate(deferred):
                if gap > 0:
                    time.sleep(gap)
                print(f"Re-snapshotting {r.name or r.place_id} ...")
                try:
                    status = _process(r)
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! skipped ({type(exc).__name__}): {exc}")
                    continue
                if _looks_stuck(status):
                    print(f"  · still {STUCK_COUNT} on retry; accepting as-is.")
    print("Snapshot complete.")


def cmd_report(args, settings) -> None:
    a_cfg = settings.analysis
    weights = settings.weights
    rows = []
    with Database(settings.db_path) as db:
        for r in db.get_restaurants():
            snaps = db.get_snapshots(r.place_id)
            reviews = db.get_reviews(r.place_id)
            latest = snaps[-1] if snaps else None
            # A later failed scrape can insert an empty histogram on top of a
            # good one; use the most recent snapshot that actually captured a
            # histogram so we don't discard data we already measured.
            hist_snap = next(
                (s for s in reversed(snaps) if sum(s.histogram) > 0), latest
            )

            age_date, age_conf = az.estimate_age(
                reviews, a_cfg.get("low_confidence_review_count", 10)
            )
            buckets, decline, improve = az.retrospective_trend(
                reviews,
                a_cfg.get("long_term_decline_threshold", 0.7),
                a_cfg.get("recent_window_months", 12),
            )
            jump = az.jump_anomaly(
                snaps,
                a_cfg.get("jump_rating_delta", 0.3),
                a_cfg.get("jump_window_days", 30),
            )
            hist = hist_snap.histogram if hist_snap else [0, 0, 0, 0, 0]
            polar = az.polarization(hist)
            reported_total = latest.total_review_count if latest else None
            # §7.3 Mode B / §6: a scrape/reported gap is only a meaningful purge
            # signal when Tier 1 (network_replay) actually pulled the full set.
            # Under dom_scroll_fallback the gap is expected, so don't score it.
            scrape_source = latest.scrape_source if latest else "dom_scroll_fallback"
            if scrape_source == "network_replay":
                vmm = az.volume_mismatch(reported_total, len(reviews))
            else:
                vmm = 0.0
            deleted_count = sum(1 for rv in reviews if rv.status != "present")

            # §7.3: Google's own "reviews removed" notice. Authoritative on the
            # first run regardless of scrape tier (unlike volume_mismatch).
            notice_max = latest.deleted_notice_max if latest else 0
            notice_est = (
                (latest.deleted_notice_min + latest.deleted_notice_max) // 2
                if latest and latest.deleted_notice_max else 0
            )
            deleted_notice = az.deleted_notice_ratio(notice_max, reported_total)
            adjusted_rating = az.adjusted_rating_estimate(
                latest.agg_rating if latest else None, reported_total, notice_est
            )

            # §7.7: fake-review inflation — high-star reviews from throwaway accounts.
            low_cred, low_cred_n = az.low_credibility_inflation(
                reviews, a_cfg.get("low_credibility_max_reviews", 2)
            )

            analysis = az.Analysis(
                place_id=r.place_id,
                age_proxy_date=age_date,
                age_confidence=age_conf,
                trend_buckets=buckets,
                long_term_decline=decline,
                long_term_improvement=improve,
                volume_mismatch=vmm,
                jump_anomaly=jump,
                deleted_notice=deleted_notice,
                deleted_notice_count=notice_max,
                adjusted_rating=adjusted_rating,
                polarization=polar,
                low_credibility=low_cred,
                low_credibility_count=low_cred_n,
            )
            score, reasons = az.suspicion_score(
                analysis,
                latest.agg_rating if latest else None,
                reported_total,
                age_date,
                weights,
                a_cfg,
            )
            rows.append({
                "place_id": r.place_id,
                "name": r.name,
                "maps_url": r.maps_url,
                "source": r.source or "",
                "has_data": latest is not None,
                "rating": latest.agg_rating if latest else None,
                "total_reviews": reported_total,
                "age_proxy_date": age_date,
                "age_confidence": age_conf,
                "trend_buckets": buckets,
                "long_term_decline": decline,
                "long_term_improvement": improve,
                "trend_dir": -decline if decline else improve,
                "jump_anomaly": jump,
                "deleted_count": deleted_count,
                "deleted_notice_max": notice_max,
                "adjusted_rating": adjusted_rating,
                "polarization": polar,
                "low_credibility": low_cred,
                "low_credibility_count": low_cred_n,
                "histogram": hist,
                "scrape_source": scrape_source,
                "reviews_scraped": len(reviews),
                "suspicion_score": score,
                "reasons": reasons,
            })
    out = build_report(rows, settings.output_path)
    print(f"Report written to {out}")


def cmd_list(args, settings) -> None:
    with Database(settings.db_path) as db:
        for r in db.get_restaurants():
            print(f"  {r.place_id}  {r.name}")


# --- arg parsing --------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Google Maps Restaurant Trust Filter")
    sub = parser.add_subparsers(dest="command", required=True)

    p_area = sub.add_parser("add-area", help="discover restaurants by area text search or grid tiling")
    p_area.add_argument("--query", default="")
    p_area.add_argument("--location", default="")
    p_area.add_argument("--radius", type=int, default=2000)
    p_area.add_argument("--grid", action="store_true",
                        help="§7b grid discovery: tile the area to beat the 60-result cap")
    p_area.add_argument("--lat", type=float, default=None, help="grid center latitude")
    p_area.add_argument("--lng", type=float, default=None, help="grid center longitude")
    p_area.add_argument("--block-size", type=float, default=None,
                        help="grid tile spacing in meters (default from config)")
    p_area.add_argument("--type", default="", help="Places includedType (default: restaurant)")
    p_area.add_argument("--dry-run", action="store_true",
                        help="print the grid estimate (tiles/API calls) without querying")
    p_area.set_defaults(func=cmd_add_area)

    p_link = sub.add_parser("add-link", help="track one restaurant by Maps place_id link")
    p_link.add_argument("url")
    p_link.add_argument("--note", default="")
    p_link.set_defaults(func=cmd_add_link)

    p_seed = sub.add_parser("seed", help="import config/restaurants.yaml")
    p_seed.set_defaults(func=cmd_seed)

    p_snap = sub.add_parser("snapshot", help="collect a data snapshot for tracked restaurants")
    p_snap.add_argument("--only", default="")
    p_snap.add_argument("--no-scrape", action="store_true", help="Places API only, skip scraping")
    p_snap.add_argument("--retry-missing", action="store_true",
                        help="only (re)snapshot restaurants whose latest histogram is empty")
    p_snap.set_defaults(func=cmd_snapshot)

    p_report = sub.add_parser("report", help="build output/report.html from history")
    p_report.set_defaults(func=cmd_report)

    p_list = sub.add_parser("list", help="list tracked restaurants")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    settings = load_settings()
    try:
        args.func(args, settings)
    except RuntimeError as exc:
        print(f"\n[error] {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
