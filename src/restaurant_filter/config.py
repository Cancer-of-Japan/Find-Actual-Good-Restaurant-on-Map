"""Configuration loading: environment secrets + YAML settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Project root = two levels up from this file (src/restaurant_filter/config.py).
ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_SETTINGS_PATH = ROOT / "config" / "settings.yaml"
_EXAMPLE_SETTINGS_PATH = ROOT / "config" / "settings.example.yaml"


@dataclass
class Settings:
    """Parsed application settings, backed by a plain dict for flexibility."""

    raw: dict[str, Any] = field(default_factory=dict)
    api_key: str | None = None

    # --- convenience accessors -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return ROOT / self.raw.get("database", {}).get("path", "data/history.sqlite")

    @property
    def output_path(self) -> Path:
        return ROOT / self.raw.get("output", {}).get("path", "output/report.html")

    @property
    def scraper(self) -> dict[str, Any]:
        return self.raw.get("scraper", {})

    @property
    def analysis(self) -> dict[str, Any]:
        return self.raw.get("analysis", {})

    @property
    def weights(self) -> dict[str, float]:
        return self.raw.get("weights", {})

    def require_api_key(self) -> str:
        if not self.api_key or self.api_key == "your_api_key_here":
            raise RuntimeError(
                "GOOGLE_MAPS_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        return self.api_key


def load_settings(settings_path: Path | None = None) -> Settings:
    """Load .env secrets and YAML settings, falling back to the example file."""
    load_dotenv(ROOT / ".env")

    path = settings_path or _DEFAULT_SETTINGS_PATH
    if not path.exists():
        path = _EXAMPLE_SETTINGS_PATH

    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    return Settings(raw=raw, api_key=os.environ.get("GOOGLE_MAPS_API_KEY"))
