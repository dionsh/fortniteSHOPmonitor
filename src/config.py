"""Configuration loading: config.json for behaviour, .env for secrets."""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # keep the app usable even if python-dotenv is missing
    def load_dotenv(*_a: Any, **_kw: Any) -> bool:
        return False

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "polling": {
        "burst_window_start_utc": "23:57",
        "burst_window_end_utc": "00:35",
        "burst_interval_seconds": 10,
        "idle_interval_seconds": 900,
        "race_languages": ["en"],
    },
    "http": {
        "timeout_seconds": 20,
        "max_retries": 4,
        "backoff_base_seconds": 2,
        "backoff_max_seconds": 60,
        "user_agent": "fortnite-shop-monitor/1.0",
    },
    "notifications": {
        "combine_multiple": True,
        "max_items_per_message": 10,
        "renotify_cooldown_hours": 1,
    },
    "catalog": {"refresh_hours": 24, "fuzzy_match_threshold": 0.87},
}

log = logging.getLogger(__name__)


def _merge(base: dict, override: dict) -> dict:
    """Deep-merge override onto base so a partial config.json still works.

    Deep-copies the base so a section the user omitted can never end up
    aliasing DEFAULTS - otherwise mutating config at runtime would silently
    rewrite the module-level defaults for everyone.
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """Behaviour settings plus credentials, kept apart on purpose."""

    def __init__(self, path: Path | None = None) -> None:
        load_dotenv(ROOT / ".env")
        self.root = ROOT
        self.path = path or (ROOT / "config.json")

        raw: dict[str, Any] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                # A broken config must not stop the monitor - fall back to defaults.
                log.error("Could not read %s (%s); using built-in defaults.", self.path, exc)
                raw = {}

        merged = _merge(DEFAULTS, raw)
        self.polling = merged["polling"]
        self.http = merged["http"]
        self.notifications = merged["notifications"]
        self.catalog = merged["catalog"]

        if self.source == "mock":
            # A simulation walks days of shop history in seconds, so the
            # anti-flap cooldown would suppress legitimate re-appearances and
            # make the simulator lie about real behaviour.
            self.notifications["renotify_cooldown_hours"] = 0

        # Paths
        self.state_dir = ROOT / "state"
        self.cache_dir = ROOT / "cache"
        self.logs_dir = ROOT / "logs"
        for directory in (self.state_dir, self.cache_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.state_file = self.state_dir / "state.json"
        self.deadletter_file = self.state_dir / "failed_notifications.jsonl"
        self.catalog_file = self.cache_dir / "cosmetics.json"
        self.watchlist_file = ROOT / "watchlist.json"

    # -- credentials (env only, never written to disk by us) --
    @property
    def discord_webhook(self) -> str:
        return (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()

    @property
    def telegram_token(self) -> str:
        return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

    @property
    def telegram_chat_id(self) -> str:
        return (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

    @property
    def source(self) -> str:
        """'live' or 'mock' - mock reads fixtures instead of the network."""
        return (os.getenv("FORTNITE_SHOP_SOURCE") or "live").strip().lower()

    @property
    def log_level(self) -> str:
        return (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
