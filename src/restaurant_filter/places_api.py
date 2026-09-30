"""Google Places API (New) client + Maps-URL helpers.

Docs: https://developers.google.com/maps/documentation/places/web-service/op-overview
Uses the v1 REST endpoints with an API key and a field mask.
"""

from __future__ import annotations

import re
import time
from typing import Any

import requests

_BASE = "https://places.googleapis.com/v1"

# Transient errors we retry: SSL drops / connection resets from the corporate
# proxy, plus Google 429/5xx throttling responses.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3

# Field mask for Place Details / search results. Keep it minimal to stay cheap.
_DETAIL_FIELDS = (
    "id,displayName,formattedAddress,location,rating,userRatingCount,"
    "googleMapsUri,reviews"
)
_SEARCH_FIELDS = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.rating,places.userRatingCount,places.googleMapsUri"
)


class PlacesClient:
    def __init__(self, api_key: str, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self, field_mask: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": field_mask,
        }

    def _request(self, method: str, url: str, field_mask: str, json: dict | None = None) -> dict[str, Any]:
        """Issue a request with retry/backoff for transient proxy SSL drops and 429/5xx."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=self._headers(field_mask),
                    json=json,
                    timeout=self.timeout,
                )
                if resp.status_code in _RETRY_STATUS:
                    raise requests.HTTPError(f"{resp.status_code} {resp.reason}")
                resp.raise_for_status()
                return resp.json()
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout, requests.HTTPError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(1.5 * (attempt + 1))  # linear backoff
        raise RuntimeError(f"Places API request failed after {_MAX_RETRIES} attempts: {last_exc}")

    def search_text(
        self, query: str, location_bias: dict | None = None, max_pages: int = 1
    ) -> list[dict[str, Any]]:
        """Text Search, e.g. query='ramen in Taipei'.

        Text Search (New) returns 20 results per page and up to 3 pages (~60
        total) via `nextPageToken`; set `max_pages` > 1 to follow them. Each page
        token needs a short delay before it activates.
        """
        body: dict[str, Any] = {"textQuery": query}
        if location_bias:
            body["locationBias"] = location_bias
        field_mask = _SEARCH_FIELDS + ",nextPageToken"
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(max(1, max_pages)):
            if token:
                body["pageToken"] = token
            data = self._request(
                "POST", f"{_BASE}/places:searchText", field_mask, json=body
            )
            out.extend(data.get("places", []))
            token = data.get("nextPageToken")
            if not token:
                break
            time.sleep(2.0)  # page token needs a moment to become valid
        return out

    def search_nearby(
        self, lat: float, lng: float, radius_m: float, included_types: list[str] | None = None
    ) -> list[dict[str, Any]]:
        # Nearby Search (New) returns at most 20 results per call and provides no
        # page token — the grid search (§7b) subdivides tiles to cover more.
        body: dict[str, Any] = {
            "includedTypes": included_types or ["restaurant"],
            "maxResultCount": 20,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": radius_m,
                }
            },
        }
        return self._request(
            "POST", f"{_BASE}/places:searchNearby", _SEARCH_FIELDS, json=body
        ).get("places", [])

    def place_details(self, place_id: str) -> dict[str, Any]:
        """Place Details. `place_id` may be a bare id or a 'places/<id>' resource name."""
        name = place_id if place_id.startswith("places/") else f"places/{place_id}"
        return self._request("GET", f"{_BASE}/{name}", _DETAIL_FIELDS)


# --- URL parsing --------------------------------------------------------------

_PLACE_ID_RE = re.compile(r"place_id:([A-Za-z0-9_\-]+)")
_CID_RE = re.compile(r"[?&]cid=(\d+)")


def extract_place_id(url: str) -> str | None:
    """Best-effort extraction of a place_id from a Google Maps URL.

    Handles `...?q=place_id:ChIJ...` style links directly. `cid=` links do not
    contain a place_id and need a Place Details lookup by other means, so we
    return None and let the caller resolve them via search.
    """
    m = _PLACE_ID_RE.search(url)
    if m:
        return m.group(1)
    return None


def extract_cid(url: str) -> str | None:
    m = _CID_RE.search(url)
    return m.group(1) if m else None
