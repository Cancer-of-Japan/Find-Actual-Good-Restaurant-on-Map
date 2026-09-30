# Google Maps Restaurant Trust Filter — Project Plan

## 1. Problem Statement
Google Maps ratings are easy to game or misleading at a glance:
- A 4.8★ restaurant that opened 3 weeks ago and has 40 reviews (likely incentivized/fake).
- A 3.9★ average hiding a bimodal split (900× 5★ + 900× 1★) — polarizing, not "mediocre."
- A rating that jumped from 3.5★ to 4.6★ in a short window (review bombing, or review purge).
- An owner quietly deleting negative reviews over time to inflate the average.

Google's UI shows none of this. This project builds a personal tool that collects data over
repeated runs, detects these patterns, and renders a filterable/sortable local HTML dashboard.

## 2. Goals
1. Track a list of restaurants (from area search and/or manual Maps links) over time.
2. Estimate **restaurant age** (proxy: earliest review found among scraped reviews) — available
   from the very first run.
3. Reconstruct a **long-term rating trend** from a single scrape (e.g. "great 5 years ago, bad
   lately") by bucketing reviews by their own dates — available from the very first run.
4. Detect **sudden rating jumps/anomalies** — the *live, ongoing* version of #3, which sharpens
   as you re-run the tool over time and build real snapshot history.
5. Detect **likely-deleted reviews**, in two tiers: an immediate volume-mismatch heuristic
   (first run) and a definitive hash-diff (needs multiple runs over time).
6. Detect **bimodal / polarized rating distributions** (1★+5★ heavy, few in between).
7. Combine the above into a per-restaurant **suspicion score**.
8. Output a single static `report.html` you open in Chrome, with client-side
   filtering, sorting, and per-restaurant detail charts.

## 3. Non-Goals (v1)
- No scheduled/automatic background runs — **manual execution only**, per your preference.
- No mobile app / browser extension — pure Python backend + static HTML output.
- No guarantee of a "true" opening date (Google doesn't expose business registration data);
  age is always a *best-effort proxy*, clearly labeled as such in the UI.
- No bypassing anti-bot protections aggressively — scraping is rate-limited, small-batch,
  personal-use only (see Risks §8).

## 4. Data Source Strategy (decision)

| Need | Source | Why |
|---|---|---|
| Discover restaurants in an area | Google Places API (Text Search / Nearby Search) | Free-tier friendly, stable, legal |
| Canonical name/address/place_id/current rating & count | Google Places API (Place Details) | Authoritative, cheap, legal |
| Full review list w/ timestamps + text | Playwright scraper on maps.google.com | Free API caps reviews at 5 — no other free way to get history |
| Star histogram (1★–5★ bar breakdown) | Playwright scraper | Not exposed by any free API |

> **API note:** Use **Places API (New)** (`places.googleapis.com/v1`) rather than the deprecated
> legacy endpoints. `places:searchText` / `places:searchNearby` handle discovery; `places/{id}`
> (Place Details) returns canonical metadata + up to **5** reviews max. Anything beyond those 5
> reviews (full history + the 1★–5★ histogram) only comes from the scraper.

| "About"/opened info (rare, not always present) | Playwright scraper | Best-effort only |
| Historical trend (age, jumps, deletions) | **Your own SQLite snapshots over time** | Nobody exposes this — you must build it by re-running the tool periodically |

**Accepted tradeoff:** scraping the Maps web page is against Google's Terms of Service.
Mitigations: personal/non-commercial use, manual low-frequency runs (you decide when),
small batches, randomized delays, no attempt to defeat CAPTCHAs (if one appears, the run
stops — don't fight it).

## 5. Architecture

```
                 ┌─────────────────────┐
                 │  restaurants.yaml   │  <- manual Maps links you paste in
                 │  (seed list)        │
                 └─────────┬───────────┘
                           │
   ┌───────────────────────┼─────────────────────────┐
   │                       │                          │
   ▼                       ▼                          │
[Area Search]        [Manual Links]                    │
Places API Text        (place_id or URL               │
Search / Nearby        parsed directly)                │
Search  → place_ids ────────────────────────────────────┘
                           │
                           ▼
                 ┌─────────────────────┐
                 │   collector.py      │
                 │  - Places API call  │  (metadata + current rating)
                 │  - Playwright scrape│  (full reviews + histogram)
                 └─────────┬───────────┘
                           ▼
                 ┌─────────────────────┐
                 │   history.sqlite    │
                 │  restaurants        │
                 │  snapshots          │
                 │  reviews            │
                 └─────────┬───────────┘
                           ▼
                 ┌─────────────────────┐
                 │   analyzer.py       │
                 │  - age estimate     │
                 │  - jump detection   │
                 │  - deletion diff    │
                 │  - bimodality score │
                 │  - suspicion score  │
                 └─────────┬───────────┘
                           ▼
                 ┌─────────────────────┐
                 │   report_builder.py │
                 │  Jinja2 + Chart.js  │
                 │  → report.html      │
                 └─────────┬───────────┘
                           ▼
                  open report.html in Chrome
                  (client-side filter/sort, no server)
```

## 5.5 Project Structure
```
Find-Actual-Good-Restaurant-on-Map/
├── PROJECT.md                 # this plan
├── README.md                  # quickstart + usage
├── requirements.txt           # pinned dependencies
├── .env.example               # template for GOOGLE_MAPS_API_KEY (never commit real .env)
├── .gitignore
├── cli.py                     # command entry point (add-area / add-link / snapshot / report)
├── config/
│   ├── settings.example.yaml  # thresholds, weights, scraper delays
│   └── restaurants.example.yaml  # seed list of manual Maps links
├── src/restaurant_filter/
│   ├── __init__.py
│   ├── config.py              # load .env + yaml settings
│   ├── db.py                  # SQLite connection + schema migrations
│   ├── models.py              # dataclasses: Restaurant, Snapshot, Review
│   ├── places_api.py          # Places API (New) client
│   ├── discovery.py           # §7b grid-search area tiling (overcome 60-result cap)
│   ├── scraper.py             # Playwright review + histogram scraper
│   ├── analyzer.py            # age / trend / jump / deletion / bimodality / score
│   ├── report_builder.py      # Jinja2 → output/report.html
│   └── templates/
│       └── report.html.j2
├── output/                    # generated report.html (gitignored)
├── data/                      # history.sqlite lives here (gitignored)
└── tests/
    └── test_analyzer.py       # pure-function detection tests (no network)
```

## 6. Data Model (SQLite)

**restaurants**
| column | notes |
|---|---|
| place_id (PK) | Google's stable ID |
| name, address, lat, lng | from Places API |
| maps_url | canonical link |
| first_tracked_at | when you first added it to your list |
| source | how it entered the DB: `seed` / `link` / `area` / `grid` (§7b). First-known provenance is preserved on re-discovery, so the report can hide discovery-only grid rows that don't have snapshots yet without losing them. |
| note | free-text note (e.g. from `add-link --note`) |

**snapshots** (one row per restaurant per run)
| column | notes |
|---|---|
| id (PK), place_id, taken_at |
| agg_rating, total_review_count | from Places API, at time of run |
| hist_1, hist_2, hist_3, hist_4, hist_5 | scraped histogram bar counts/percentages |

**reviews** (accumulates across runs; a review can appear in many snapshots)
| column | notes |
|---|---|
| review_hash (PK) | hash of author+text+relative-date-at-first-seen (Google review IDs aren't public, so we hash) |
| place_id |
| rating, text_snippet, author_name |
| est_review_date | parsed from Google's relative date ("3 weeks ago") + snapshot time |
| first_seen_snapshot_id, last_seen_snapshot_id |
| status | `present` / `missing_since_<snapshot_id>` (flips when a review disappears) |
| is_local_guide / reviewer_review_count | reviewer credibility signals (§7.7); `reviewer_review_count=0` means we couldn't parse it, not zero reviews |
| owner_response | owner's reply text if present (engagement signal; empty = no reply) |
**snapshots table also gains:**
| column | notes |
|---|---|
| scrape_source | `network_replay` (Tier 1, full pull) or `dom_scroll_fallback` (Tier 2, partial) |
| reviews_retrieved_count | how many reviews were actually parsed this run, vs. `total_review_count` from Places API — a large gap here (especially under `dom_scroll_fallback`) is expected and should not itself be treated as a purge signal; only treat volume-mismatch (§7.3 Mode B) as meaningful when `scrape_source = network_replay` |
| deleted_notice_min / deleted_notice_max | count range parsed from Google's own on-page "reviews removed" notice (§7.3 Mode C); `0/0` when no numeric count was shown |
| deleted_notice_text | the raw notice line as displayed, stored verbatim for auditability (a human can confirm what triggered the flag — we never fabricate the count) |
## 7. Detection Algorithms

### 7.1 Restaurant Age (proxy)
- `age_proxy_date = min(est_review_date across all reviews ever scraped)`
- Confidence label: **Low** if fewer than ~10 reviews seen total (age likely underestimated
  because old reviews may not surface in the scraped page order); **Medium/High** otherwise.
- Display as "at least X months/years old" — never claim exact founding date.

### 7.2 Retrospective Rating Trend (single-run reconstruction) — NEW, high value
Key realization: we don't need to wait years for this. Every review we scrape *today* already
carries an estimated date and a star rating, so we can reconstruct a historical trend line in
**one single run**, without needing repeated snapshots first.

- Take every scraped review for a place, sort by `est_review_date`.
- Bucket into periods (e.g. by year, or by "first half of visible history" vs "last 12 months").
- Compute average rating per bucket → plot as a line: "Year 1–2 avg: 4.7★, Last 12mo avg: 3.1★"
  directly surfaces the "was great 5 years ago, bad lately" pattern (or the reverse: "was rough
  early on, has genuinely improved").
- Flag `long_term_decline` when early-period average minus recent-period average exceeds a
  threshold (e.g. > 0.7★ drop), and `long_term_improvement` for the mirror case (less suspicious,
  arguably a positive signal — new ownership/management fixed things).

**Critical caveat — survivorship bias:** this reconstruction only uses reviews that are *still
visible today*. If an owner deleted old 1★ reviews, they disappear from this history too.
Consequences:
- A **detected decline** is trustworthy — it means the drop is real and even survived any
  potential cleanup, so treat it as strong signal.
- A **suspiciously flat/stable-looking** long history combined with a large gap between the
  restaurant's *stated* total review count (from Places API) and the number of reviews we
  actually manage to scrape/surface → itself becomes a weaker heuristic flag
  (`possible_purge_volume_mismatch`), since it suggests reviews exist that aren't surfacing.

### 7.3 Deleted Review Detection (two modes)
**Mode A — multi-run diff (definitive, needs history):**
- On each new run, diff current scraped review_hash set vs. all previously-seen hashes for
  that place_id.
- Any previously-seen hash not found now → mark `missing_since_<snapshot_id>`.
- Report: count of missing reviews, and **skew of missing reviews' star ratings** (if most
  disappeared reviews were 1★/2★, that's a strong manipulation signal).

**Mode B — single-run volume-mismatch heuristic (works immediately, weaker evidence):**
- Compare Places API's `total_review_count` against the number of reviews actually retrievable
  by scrolling the scraper (Google's infinite scroll has practical limits and ranking/sorting
  quirks, so some gap is normal — this is a soft signal, not proof).
- A large, unexplained gap → contributes a small weight to the suspicion score and is labeled
  clearly as "unconfirmed, needs future runs to verify" until Mode A can confirm it.

**Mode C — Google's native removal notice (authoritative, works on first run):**
- Google itself sometimes renders a notice on the place page stating that reviews were removed
  (policy violations, spam, etc.), occasionally with a count. We parse that notice text directly
  (borrowed from the `mb4umi/maps-deleted-reviews` approach) — no anti-bot evasion, we only read
  what Google already shows any visitor.
- Unlike Mode B, this is **not** gated on `scrape_source`: it is Google's own statement, so it is
  valid regardless of which scrape tier ran. Stored as `deleted_notice_min/max` plus the raw
  `deleted_notice_text` for audit.
- Signals produced:
  - `deleted_notice_ratio = deleted_max / (reported_total + deleted_max)` — how large the purge is
    relative to the surviving pool.
  - `adjusted_rating` — an mb4umi-style "real score" that re-adds the removed reviews as if each had
    been 1★: `((rating * total) + deleted_estimate) / (total + deleted_estimate)`, where
    `deleted_estimate` is the midpoint of min/max. Shown as a tooltip/secondary value, never as a
    claimed true rating (it's a worst-case what-if, clearly labeled).

### 7.4 Sudden Rating Jump / Anomaly (multi-run, needs your own history)
- Build a time series per restaurant: at each snapshot, `(taken_at, agg_rating, total_review_count)`.
- Also build an *expected* rating from newly-appeared reviews only (their own star values),
  and compare to the *actual* new aggregate — a mismatch (aggregate rises more than the new
  reviews alone justify) signals possible old-review deletion, not just new 5★ reviews.
- Flag when `Δrating / Δtime` exceeds a threshold (e.g., +0.3★ in under 30 days) relative to
  review-count growth — a small batch of new reviews swinging the average a lot is suspicious.
- This complements §7.2: §7.2 gives you the *retrospective* long-term picture from one run;
  §7.4 gives you a *live, ongoing* watch as you keep re-running the tool going forward.

### 7.5 Bimodal / Polarized Distribution
- From the scraped histogram (counts per star 1–5), compute a simple bimodality signal:
  `polarization = (hist_1 + hist_5) / total_reviews` vs. a baseline expectation.
- High polarization + moderate average rating (e.g., 3.5–4.2★) → flag as "polarizing," distinct
  from genuinely mediocre (which would show a normal-ish bell curve centered near 3★).

### 7.6 Suspicion Score (composite, v1 — simple weighted sum, tunable later)
```
score = w1 * young_and_high_rating_flag
      + w2 * long_term_decline_magnitude        (from §7.2, available on first run)
      + w3 * jump_anomaly_magnitude             (from §7.4, needs multi-run history)
      + w4 * deleted_reviews_skewed_negative    (from §7.3 Mode A, needs multi-run history)
      + w5 * possible_purge_volume_mismatch     (from §7.3 Mode B, available on first run)
      + w6 * polarization_score
      + w7 * deleted_notice                     (from §7.3 Mode C, Google's own notice, first run)
      + w8 * low_credibility_reviews            (from §7.7, available on first run)
```
Displayed as a 0–100 badge with a breakdown tooltip (which factors triggered it) — never a
black-box number alone.

### 7.7 Reviewer Credibility (fake-review inflation, first run)
Borrowed from the ScrapeBadger field set: each scraped review also carries the reviewer's
**Local Guide** status and **lifetime review count**, plus whether the **owner responded**.
- `low_credibility_reviews` = share of high-star (>=4★) reviews — *among those we could actually
  vet* — authored by accounts that are **not** Local Guides and have a parsed lifetime count of
  1..`low_credibility_max_reviews`. Throwaway accounts posting 5★ are a classic paid/incentivized
  pattern. Reviews with unparsed metadata are excluded from the denominator, so missing data never
  inflates the flag (data-quality: don't guess).
- Owner-response presence is stored for future engagement analysis; it does not feed the score yet.

**Review sort order (scraper).** The DOM tier reads the feed in a configurable order
(`scraper.review_sort`: `relevant` | `newest` | `highest_rating` | `lowest_rating`, default
`newest`). `newest` yields cleaner age/trend reconstruction (§7.2) and snapshot-to-snapshot
deletion diffing (§7.3 Mode A); `lowest_rating` front-loads the 1★ reviews most likely to be
purged. No anti-bot evasion — it just clicks Google's own Sort control.

## 7a. Scraper Implementation Strategy (hybrid: network replay → DOM-scroll fallback)

### Background — why DOM-scrolling alone isn't enough
A human scrolling the Maps review pane in a real browser gets lazy-loaded batches
indefinitely (all reviews, e.g. all 2,450). An automated Playwright session driving the
identical-looking scroll action typically gets throttled after the first ~5 cards. This is
**not** a hard page limit — it's Google's behavioral anti-automation detection (scroll
velocity/pattern, absence of real mouse movement, `navigator.webdriver` flag, missing
browser fingerprints) recognizing the session as scripted and cutting off further
lazy-load responses. The star histogram (aggregate distribution) is unaffected by this and
loads reliably regardless, since it comes from the page's initial data blob, not the
paginated review-list calls.

### Chosen approach: Tier 1 network-endpoint replay, Tier 2 DOM-scroll fallback

**Tier 1 — Network-endpoint replay (primary, attempted first for every place):**
Google's review pane is itself powered by an internal RPC endpoint
(`/maps/preview/review/listentitiesreviews`) that returns plain JSON (after stripping an
anti-XSSI prefix), paginated via an `offset` parameter in the request's protobuf-encoded
`pb` string. Once the request shape is known, it can be replayed directly via plain HTTP
(`httpx`/`requests`) — incrementing `offset` — with **no browser automation involved in the
pull itself**, which removes the DOM-scroll behavioral fingerprint entirely and reaches the
full review set, not just 5.

*One-time capture step (per Google Maps template/version, re-done whenever it breaks):*
1. Run a throwaway **headed** Playwright script against a sample restaurant page.
2. Attach a Playwright network listener (`page.on("request")` / `page.on("response")`)
   that logs every outgoing request URL, method, and payload during a real scroll session.
3. Manually scroll the reviews pane a few times within that same script (or interact
   normally) so the listing endpoint fires and gets captured in the log.
4. Extract from the log: the endpoint URL, the `pb` payload structure, required headers
   (cookies/referer/user-agent), and how `offset`/sort parameters change between calls.
5. Save this as a small `network_capture_log.json` / notes file for reference, and hardcode
   the confirmed request template into the scraper.
6. Re-run this capture step whenever Tier 1 requests start failing/returning malformed data
   (signal that Google changed the internal schema).

*Ongoing use:* the review-API client builds the request from the template + place's feature
ID, loops incrementing `offset` until the response is empty/repeats, parses each review's
rating, relative date, text, author — no headless browser needed for this part at all.

**Tier 2 — DOM-scroll Playwright fallback (used only if Tier 1 fails for a given place or
breaks entirely after a Google-side change):**
Falls back to the original plan: headless/headed Playwright, scroll the `div[role="feed"]`
review pane, wait for lazy-load, parse rendered review cards. Expected yield in this mode:
histogram (reliable) + only ~5 reviews (throttled) — logged clearly in the data as
`scrape_source: dom_scroll_fallback` so the analyzer and report can flag that place's data
as incomplete rather than silently treating it as equivalent to a full pull.

### Why hybrid, not network-only
- Tier 1 depends on an **undocumented internal API** whose request schema (`pb` protobuf
  encoding) can change without notice — when it does, Tier 1 will start failing across the
  board until re-captured.
- Tier 2 guarantees *something* (histogram + a handful of reviews) keeps working during that
  gap, so the tool degrades gracefully instead of returning nothing.
- Every scraped record is tagged with its `scrape_source` tier and yield count, so the report
  can visually distinguish "full history" places from "partial data, treat conclusions
  cautiously" places.

### Risk notes specific to Tier 1
- Reverse-engineering and replaying an internal, undocumented endpoint is a firmer ToS
  gray area than scraping a rendered public page — mitigate the same way as before:
  personal use, low volume, manual runs, randomized delays between paginated calls, back off
  immediately on errors/CAPTCHAs rather than retrying aggressively.
- Still subject to the same IP-based rate limiting Google applies to the DOM path — this
  removes the *behavioral* detection surface, not the *volume/rate* one.
- Tier 1 is **disabled by default** (`scraper.network_replay: false`) until the one-time
  capture step above has been run on a network without an intercepting proxy; until then the
  scraper always uses Tier 2 and every snapshot is tagged `dom_scroll_fallback`.

## 7b. Area Discovery Strategy — Grid Search (overcoming the 60-result cap)

### The limit
Every official Places API search endpoint — Text Search and Nearby Search, legacy and new —
caps out at **60 results total per query**, delivered 20-per-page across up to 3 pages via
`next_page_token`/`pageToken` (each subsequent page requires a short delay before the token
activates, and each page counts as a separately billed request). This is a hard,
Google-side limit: there is an official Issue Tracker ticket requesting more results per
query, closed as **"Won't Fix (Infeasible)."** The Google Maps UI itself only surfaces
roughly 120 listings per view for the same underlying reason.

Consequence for this project: a single `add-area --location "Taipei" --radius 2000` style
call, no matter how it paginates, will never return more than 60 restaurants — even if the
true count in that area is in the hundreds.

### The workaround: slice the area into a grid, query each block, merge + de-dupe
Instead of one query over a large radius, subdivide the requested area into a grid of
smaller tiles small enough that **no single tile is likely to contain more than 60
restaurants**, run one (paginated, up to 60-result) query per tile, then merge all tile
results and de-duplicate by `place_id`.

**Tiling algorithm (planned for `add-area` in `cli.py` / `discovery.py`):**
1. Take the requested center point (lat/lng) and overall radius (or bounding box).
2. Compute a grid of tile centers spaced at `block_size` meters apart, covering the full
   requested area (simple square/hex grid in lat/lng, converted from meters via local
   great-circle approximation).
3. **Overlap tiles slightly** (e.g. tile radius = `block_size * 0.6`, so adjacent circles
   overlap a bit) to avoid missing restaurants that sit near a tile boundary.
4. For each tile:
   - Run Nearby Search (`keyword`/`type` filters as given) centered on that tile, with the
     tile's radius.
   - Paginate through all pages Google returns for that tile (up to 60, i.e. up to 3 pages).
   - If a tile itself returns the full 60 (hits the cap), flag it as `tile_saturated: true`
     — a signal that this tile is too coarse and should be **automatically re-split into 4
     smaller sub-tiles** and re-queried (recursive subdivision), rather than silently
     accepting an incomplete result for that tile.
5. Merge all tiles' results; de-duplicate by `place_id` (a restaurant appearing in multiple
   overlapping tiles is kept once).
6. Insert deduplicated results into the `restaurants` table, tagging each with which tile(s)
   it was found in (useful for debugging grid coverage later).

**Default tiling parameters (tunable via config):**
| Parameter | Default | Notes |
|---|---|---|
| `block_size` | 500 m | Dense urban cores (e.g. downtown Taipei) may need 250–300 m; sparse suburbs can use 1000–2000 m |
| `tile_overlap_factor` | 0.6× block_size as tile radius | Ensures boundary coverage without excessive duplicate queries |
| `max_recursion_depth` | 2 | Caps how many times a saturated tile can auto-split, to bound worst-case API call count |
| `saturation_threshold` | 20 results (Places API **New** Nearby Search hard cap) | Triggers auto re-split of that tile |

> **New-API accuracy note (implemented behavior):** the 60-result / 3-page
> `pageToken` figure above is precise for **Text Search (New)**. **Nearby Search
> (New)** — which the grid actually uses, because it takes a hard circular
> `locationRestriction` — returns at most **20 results per call with no page
> token**. The implementation therefore treats a tile as saturated at **20**
> results and relies on recursive subdivision (`max_recursion_depth`) to cover
> dense tiles, rather than per-tile pagination. `add-area` without `--grid`
> still uses Text Search and paginates up to 60.

**Cost/call-count tradeoff:** each tile (and each of its up to 3 pages) is a separately
billed Places API request, so smaller `block_size` directly multiplies total API calls for
a given area. At the Places API free tier (~$200/month credit) this comfortably covers
personal-scale exploration of a district at a reasonably fine grid, but covering an entire
city at fine granularity would need cost monitoring — `cli.py add-area` should print an
estimated tile count and API call count *before* executing, so you can adjust `block_size`
or the overall radius first if the estimate looks too large.

**Output of this step:** a deduplicated list of `place_id`s covering the full requested
area (as completely as Places API structurally allows), ready to feed into the
Place Details lookups (§4) and the Tier 1/Tier 2 review scraper (§7a) for each restaurant.

## 8. Tech Stack
- Python 3.11+
- `googlemaps` or plain `requests` for Places API calls
- `playwright` (Chromium headless) for review/histogram scraping, with randomized delays
- `sqlite3` (stdlib) or SQLAlchemy for storage
- `pandas` for time-series analysis in `analyzer.py`
- `jinja2` + `Chart.js` (CDN or vendored) for the static report — plain JS `Array.filter/sort`
  for interactivity, no backend server needed to view it
- `pyyaml` for the seed list config
- `python-dotenv` for loading `GOOGLE_MAPS_API_KEY` from a local `.env` (never committed)

### 8.1 Secrets & Config
- API key lives in `.env` (gitignored). `.env.example` documents the expected keys.
- Tunable thresholds/weights (suspicion score, decline threshold, scraper delays) live in
  `config/settings.yaml`, so behavior changes need no code edits.
- `data/history.sqlite` and `output/` are gitignored — they hold your personal collected data.

## 9. Workflow / CLI (planned commands)
```bash
# one-time setup
python -m venv .venv && pip install -r requirements.txt
playwright install chromium

# add restaurants
python cli.py add-area --query "ramen" --location "Taipei" --radius 2000
# §7b grid discovery (overcome the 60-result cap): tiles the area, dedupes by place_id
python cli.py add-area --grid --location "East District, Hsinchu" --radius 2500 --block-size 500
python cli.py add-area --grid --lat 24.7975 --lng 120.9698 --radius 2000 --dry-run  # estimate calls only
python cli.py add-link "https://maps.google.com/?cid=..."

# run a snapshot (manual, whenever you choose)
python cli.py snapshot          # hits Places API + scrapes reviews for all tracked restaurants
python cli.py snapshot --only "place_id1,place_id2"   # just a subset

# build the report from accumulated history
python cli.py report            # writes ./output/report.html
```
Open `output/report.html` in Chrome after each `report` run.

## 10. Report / UI Spec (report.html)
- **Table view** (default): Name | Current★ | # Reviews | Age (proxy) | Long-term trend (↑/→/↓) | Jump flag | Deleted-review count | Polarization | Suspicion score
  - Client-side filters: min/max rating, min age, min suspicion score, "declining trend" toggle, "has deleted reviews" toggle
  - Sortable columns
- **Detail panel** (click a row):
  - Line chart A — **retrospective trend** (§7.2): avg rating per period reconstructed from
    review dates in this single scrape; available from day one.
  - Line chart B — **live snapshot history** (§7.4): aggregate rating across your own runs
    over time; starts empty, fills in as you re-run the tool.
  - Bar chart: current star histogram
  - Note if `possible_purge_volume_mismatch` triggered: "Google reports N total reviews, only
    M were retrievable — some history may be hidden/deleted."
  - List: reviews flagged missing via Mode A hash-diff (once available), with star rating and
    last-seen date
  - Plain-language explanation of why the suspicion score is what it is

## 11. Milestones
1. **M1 — Skeleton & Places API integration**: config/seed list, Places API calls, SQLite schema, `snapshot` command populates `restaurants` + `snapshots` tables (no scraping yet).
1b. **M1b — Area discovery grid search (§7b)**: `discovery.py` tiling + recursive
   subdivision + `place_id` de-dup; `add-area --grid` with a pre-run estimate and
   `--dry-run`; Nearby Search capped at 20/tile (New API) with auto re-split.
2. **M2 — Scraper (hybrid)**:
   - M2a: one-time network capture (throwaway headed Playwright run + request logger) to
     determine the Tier 1 `listentitiesreviews` request template.
   - M2b: build Tier 1 network-replay client (`review_api.py`, offset pagination) into the
     `reviews` table; hash-based dedup; relative-date parsing.
   - M2c: build Tier 2 DOM-scroll Playwright fallback, triggered automatically when Tier 1
     fails; tag results with `scrape_source`.
3. **M3 — Analyzer**: age proxy + retrospective trend reconstruction first (both work on a
   single run, highest immediate value), then volume-mismatch heuristic and polarization score,
   then multi-run-dependent features (live jump detection, hash-diff deletion) once repeat runs
   exist. Composite suspicion score last, combining whatever is available at run time.
4. **M4 — Report builder**: static HTML with table + Chart.js detail views, client-side filter/sort.
5. **M5 — Polish**: score explanation tooltips, CSV export, config for thresholds/weights.

## 12. Risks & Mitigations
| Risk | Mitigation |
|---|---|
| Scraping breaks Google's ToS | Personal use, manual/low-frequency runs, small batches, back off on any block/CAPTCHA |
| Google changes Maps page structure, scraper breaks | Isolate scraping selectors in one module; expect periodic fixes |
| Age proxy is inaccurate (old reviews not surfaced) | Always label as proxy + confidence level, never "founding date" |
| Relative dates ("3 weeks ago") are imprecise | Store both the raw string and a computed estimate window, not false precision |
| False positives on suspicion score | Show score breakdown, never just a single opaque number |

## 13. Open Questions / Backlog for Later
- Should suspicion-score weights be user-tunable via a config file? (likely yes, v1.1)
- Add city-wide batch comparisons ("this restaurant vs. area average")?
- Export flagged restaurants list to share/compare with friends?
- Optional: cross-check against other platforms (Yelp/TripAdvisor) if you ever revisit the
  free-only constraint.
