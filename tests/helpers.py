"""Shared test scaffolding: fake shop payloads and an isolated Config."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.notifiers.base import Notifier


def make_item(item_id, name, item_type="Outfit", rarity="Rare"):
    return {
        "id": item_id,
        "name": name,
        "description": "Test item.",
        "type": {"value": item_type.lower(), "displayValue": item_type,
                 "backendValue": "Athena" + item_type},
        "rarity": {"value": rarity.lower(), "displayValue": rarity,
                   "backendValue": "EFortRarity::" + rarity},
        "images": {"icon": "https://example.invalid/{}.png".format(item_id),
                   "smallIcon": "https://example.invalid/{}_s.png".format(item_id)},
    }


def make_entry(items, price=1200, regular=None, in_date="2026-08-22T00:00:00Z",
               out_date="2026-08-23T23:59:59Z", offer_id=None, container="brItems"):
    return {
        "regularPrice": regular if regular is not None else price,
        "finalPrice": price,
        "devName": "[VIRTUAL] test offer",
        "offerId": offer_id or ("offer-" + "-".join(i["id"] for i in items)),
        "inDate": in_date,
        "outDate": out_date,
        "giftable": True,
        "refundable": True,
        "sortPriority": 0,
        "layout": {"id": "Test", "name": "Featured", "category": "Featured"},
        "bundle": None,
        container: items,
    }


def make_shop(items, shop_hash="hash0", date="2026-08-22T00:00:00Z", price=1200):
    """Build a shop payload containing one offer per item."""
    return {
        "hash": shop_hash,
        "date": date,
        "vbuckIcon": "https://example.invalid/vbuck.png",
        "entries": [make_entry([i], price=price) for i in items],
    }


class TempConfig(Config):
    """Config rooted in a throwaway directory, so tests never touch real state."""

    def __init__(self, watchlist_items=None, overrides=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="fnshop-test-"))
        super().__init__(path=Path("/nonexistent-config.json"))

        self.state_dir = self.tmp / "state"
        self.cache_dir = self.tmp / "cache"
        self.logs_dir = self.tmp / "logs"
        for directory in (self.state_dir, self.cache_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.state_file = self.state_dir / "state.json"
        self.deadletter_file = self.state_dir / "failed_notifications.jsonl"
        self.catalog_file = self.cache_dir / "cosmetics.json"
        self.watchlist_file = self.tmp / "watchlist.json"

        self.write_watchlist(watchlist_items or [])

        # Tests must never wait on real backoff.
        self.http["max_retries"] = 0
        self.http["backoff_base_seconds"] = 0
        self.http["backoff_max_seconds"] = 0

        if overrides:
            for section, values in overrides.items():
                getattr(self, section).update(values)

    def write_watchlist(self, items):
        self.watchlist_file.write_text(json.dumps({"items": items}), encoding="utf-8")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Credentials are never read from the real environment during tests.
    @property
    def discord_webhook(self):
        return ""

    @property
    def telegram_token(self):
        return ""

    @property
    def telegram_chat_id(self):
        return ""

    @property
    def source(self):
        return "mock"

    @property
    def log_level(self):
        return "CRITICAL"


class RecordingNotifier(Notifier):
    """Captures alerts instead of sending them; can be made to fail on demand."""

    name = "recording"

    def __init__(self, fail=False):
        self.alerts = []
        self.raw = []
        self.fail = fail

    def enabled(self):
        return True

    def build_payload(self, alert):
        return alert.to_dict()

    def post(self, payload):
        return not self.fail

    def send(self, alert):
        if self.fail:
            return False
        self.alerts.append(alert)
        return True

    def send_raw(self, payload):
        if self.fail:
            return False
        self.raw.append(payload)
        return True

    # convenience
    def names(self):
        return [i.name for alert in self.alerts for i in alert.items]

    def reset(self):
        self.alerts.clear()
        self.raw.clear()


class ExplodingNotifier(Notifier):
    """A notifier that raises, to prove one bad channel cannot kill the loop."""

    name = "exploding"

    def enabled(self):
        return True

    def build_payload(self, alert):
        raise RuntimeError("build blew up")

    def post(self, payload):
        raise RuntimeError("post blew up")

    def send(self, alert):
        raise RuntimeError("send blew up")

    def send_raw(self, payload):
        raise RuntimeError("send_raw blew up")
