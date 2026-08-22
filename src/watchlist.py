"""Load watchlist.json and resolve plain names to stable item IDs.

The user should be able to write:

    { "items": ["Renegade Raider", "Take The L"] }

...and never touch code. On startup we resolve each name against the full
cosmetics catalog (16k+ items, cached locally) so matching can key off IDs
like CID_028_Athena_Commando_F, which survive display-name changes.
"""
from __future__ import annotations

import difflib
import json
import logging
import time
from typing import Any

from .matcher import normalize_name

log = logging.getLogger(__name__)


class WatchEntry:
    def __init__(self, raw_name, item_id=None, source="name"):
        self.raw_name = raw_name or ""
        self.item_id = item_id or ""
        self.source = source  # how the ID was obtained: pinned | catalog | fuzzy | unresolved

    @property
    def norm_name(self):
        return normalize_name(self.raw_name)

    @property
    def label(self):
        return self.raw_name or self.item_id

    def __repr__(self):
        return "<WatchEntry '{}' id={} via={}>".format(
            self.raw_name, self.item_id or "-", self.source)


def load_watchlist(path):
    """Read watchlist.json. Tolerates strings and {id,name} objects.

    A malformed file returns an empty list plus a loud log line rather than
    crashing - the monitor keeps running and the user can fix the file live.
    """
    if not path.exists():
        log.error("watchlist.json not found at %s", path)
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("watchlist.json is not valid JSON (%s). Keeping previous watchlist.", exc)
        raise

    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        log.error("watchlist.json has no 'items' list.")
        return []

    entries = []
    seen = set()
    for element in items:
        if isinstance(element, str):
            name, item_id = element.strip(), ""
        elif isinstance(element, dict):
            name = str(element.get("name") or "").strip()
            item_id = str(element.get("id") or "").strip()
        else:
            log.warning("Skipping unrecognised watchlist element: %r", element)
            continue

        if not name and not item_id:
            continue

        dedupe_key = (item_id.casefold(), normalize_name(name))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        entries.append(WatchEntry(name, item_id, "pinned" if item_id else "name"))

    return entries


class CatalogResolver:
    """Resolves names to IDs using a locally cached copy of the catalog."""

    def __init__(self, config, api):
        self.config = config
        self.api = api
        self.by_name = {}
        self.by_id = {}
        self.all_names = []
        self.loaded = False

    def _cache_is_fresh(self):
        path = self.config.catalog_file
        if not path.exists():
            return False
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
        return age_hours < self.config.catalog["refresh_hours"]

    def load(self):
        """Load the catalog from cache, refreshing from the API when stale.

        A network failure here is never fatal: a stale cache still resolves
        names fine, and with no cache at all we fall back to name matching.
        """
        data = None

        if self._cache_is_fresh():
            try:
                data = json.loads(self.config.catalog_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Cached catalog unreadable (%s); refetching.", exc)
                data = None

        if data is None:
            try:
                data = self.api.fetch_cosmetics()
                tmp = self.config.catalog_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                tmp.replace(self.config.catalog_file)
                log.info("Cosmetics catalog refreshed (%d items).", len(data))
            except Exception as exc:
                log.warning("Could not refresh cosmetics catalog: %s", exc)
                if self.config.catalog_file.exists():
                    try:
                        data = json.loads(self.config.catalog_file.read_text(encoding="utf-8"))
                        log.info("Falling back to stale cached catalog.")
                    except Exception:
                        data = None

        if not data:
            log.warning("No cosmetics catalog available - watchlist will match by name only.")
            self.loaded = False
            return

        for item in data:
            if not isinstance(item, dict):
                continue
            norm = normalize_name(item.get("name"))
            if norm:
                self.by_name.setdefault(norm, item)
            item_id = item.get("id")
            if item_id:
                self.by_id.setdefault(str(item_id).casefold(), item)

        self.all_names = list(self.by_name.keys())
        self.loaded = True

    # ------------------------------------------------------------------
    def localize(self, shop_item):
        """Rewrite a shop item's display fields using the English catalog.

        We may have fetched the shop in another language, because each
        language variant has its own independently staggered cache and the
        freshest one wins. Item IDs and prices are language-independent, so we
        recover the English name/type/rarity by ID from the cached catalog.
        Without this, a German cache hit would produce German alerts and break
        name-based watchlist matching.
        """
        if not self.loaded or not shop_item.id:
            return shop_item

        record = self.by_id.get(shop_item.id.casefold())
        if not record:
            return shop_item

        name = record.get("name")
        if name:
            shop_item.name = name

        type_field = record.get("type")
        if isinstance(type_field, dict):
            display = type_field.get("displayValue") or type_field.get("value")
            if display:
                shop_item.type = display

        rarity_field = record.get("rarity")
        if isinstance(rarity_field, dict):
            display = rarity_field.get("displayValue") or rarity_field.get("value")
            if display:
                shop_item.rarity = display

        return shop_item

    def localize_all(self, shop_items):
        """Localize a {key: ShopItem} map in place and return it."""
        for item in shop_items.values():
            self.localize(item)
        return shop_items

    def resolve(self, entry):
        """Fill in entry.item_id when we can. Mutates and returns the entry."""
        if entry.item_id:
            entry.source = "pinned"
            return entry

        if not self.loaded or not entry.norm_name:
            entry.source = "unresolved"
            return entry

        exact = self.by_name.get(entry.norm_name)
        if exact:
            entry.item_id = exact.get("id") or ""
            entry.source = "catalog"
            return entry

        threshold = self.config.catalog["fuzzy_match_threshold"]
        close = difflib.get_close_matches(entry.norm_name, self.all_names, n=1, cutoff=threshold)
        if close:
            match = self.by_name[close[0]]
            entry.item_id = match.get("id") or ""
            entry.source = "fuzzy"
            log.info("Watchlist '%s' fuzzy-matched to '%s' (%s).",
                     entry.raw_name, match.get("name"), entry.item_id)
            return entry

        entry.source = "unresolved"
        log.warning("Could not resolve '%s' to an item ID. It will still be matched by "
                    "name, but pinning an ID is more reliable.", entry.raw_name)
        return entry

    def resolve_all(self, entries):
        return [self.resolve(e) for e in entries]
