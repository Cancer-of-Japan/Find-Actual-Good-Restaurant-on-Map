# Find Actual Good Restaurant on Map

A personal tool that looks past a Google Maps star rating and detects
manipulation patterns — fake early hype, long-term decline hidden behind a high
average, sudden rating jumps, quietly deleted negative reviews, and polarized
1★/5★ distributions — then renders a filterable static HTML dashboard.

See [PROJECT.md](PROJECT.md) for the full design, algorithms, and rationale.

## Quickstart

```powershell
# 1. Create a virtual environment and install dependencies
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium

# 2. Configure secrets and settings
Copy-Item .env.example .env
# edit .env and paste your Google Places API (New) key
Copy-Item config\settings.example.yaml config\settings.yaml
Copy-Item config\restaurants.example.yaml config\restaurants.yaml   # optional seed list

# 3. Track restaurants
python cli.py add-link "https://www.google.com/maps/place/?q=place_id:ChIJ..."
python cli.py add-area --query "ramen" --location "Taipei"
python cli.py seed          # import config/restaurants.yaml

# 4. Collect a snapshot (Places API + scraper). Re-run over time to build history.
python cli.py snapshot                # all tracked restaurants
python cli.py snapshot --no-scrape    # Places API metadata only (no browser)

# 5. Build and open the dashboard
python cli.py report
start output\report.html
```

## Running the snapshot on the Linux host (Brave CDP mode)

Snapshots normally run on the Linux box at **192.168.10.2**, where the scraper
attaches to a real, logged-in **Brave** browser over the DevTools protocol (CDP).
This lets Google show the *full* login-gated review set instead of the ~5 cards
an anonymous session sees.

Prerequisites (already configured on that host):

- Project lives at `~/Desktop/Projects/Find Restaurant/Find-Actual-Good-Restaurant-on-Map`.
- Virtualenv at `.venv`; run Python as `./.venv/bin/python`.
- `config/settings.yaml` has `scraper.brave_cdp_url: 'http://127.0.0.1:9222'`.
- Brave is signed in to the Google account you want to scrape as.

```bash
# 1. SSH into the host (run from your workstation)
ssh wnc@192.168.10.2

# 2. Go to the project (the path has a space, so quote it)
cd "~/Desktop/Projects/Find Restaurant/Find-Actual-Good-Restaurant-on-Map"

# 3. Start Brave with the remote-debugging port (idempotent; relaunches Brave).
#    Must run inside the desktop session — it renders headed on the GNOME/Wayland
#    display. Pass the CDP port (matches settings.yaml).
./tools/start_brave_debug.sh 9222

# 4. Run the snapshot
./.venv/bin/python cli.py snapshot                       # all tracked restaurants
./.venv/bin/python cli.py snapshot --only "ChIJ...,ChIJ..."   # specific place_id(s)
./.venv/bin/python cli.py snapshot --no-scrape           # Places API metadata only

# 5. (Optional) rebuild the dashboard
./.venv/bin/python cli.py report
```

Notes:

- Keep Brave open on port `9222` while snapshotting. `start_brave_debug.sh` is
  safe to re-run; it will relaunch Brave with the debugging port if needed.
- `HumanCursor not available; using plain mouse motion.` is expected over SSH and
  is harmless — Playwright still drives Brave via CDP.
- The scraper closes only its own tab on cleanup; it never closes your Brave.

## How it works (short version)

- **Places API (New)** supplies canonical name/address/current rating/count.
- A **Playwright** scraper pulls the full review list + star histogram (the free
  API caps reviews at 5).
- Everything is stored in `data/history.sqlite`. Some signals (age proxy,
  retrospective trend, polarization, volume-mismatch) work on the **very first
  run**; others (live rating jumps, definitive deleted-review diffs) sharpen as
  you **re-run** the tool over time.
- `report` computes a per-restaurant **suspicion score** with a plain-language
  breakdown and writes a self-contained `output/report.html`.

## Notes & limits

- Scraping the Maps web page is against Google's ToS — this is a **personal,
  manual, low-frequency** tool. It aborts on any CAPTCHA rather than fighting it.
- "Age" is a best-effort **proxy** (earliest surviving review), never a founding date.
- If a corporate SSL proxy blocks requests, `snapshot --no-scrape` still records
  Places API data, and the scraper fails gracefully without stopping the run.

## Development

```powershell
pip install pytest
pytest
```

`tests/test_analyzer.py` covers the detection functions with no network or DB.
