"""Flatten shop entries into comparable items and match them to the watchlist.

A single shop *entry* is one purchasable offer. It can carry several different
kinds of content, and the API keeps each kind in its own list:

    brItems      outfits, emotes, pickaxes, back blings, gliders, wraps, ...
    tracks       Festival jam tracks (title/artist instead of name)
    instruments  Festival instruments
    cars         Rocket Racing vehicles and parts
    legoKits     LEGO kits

We normalise all of them into one `ShopItem` shape so the rest of the app
never has to care which bucket something came from.
"""
from __future__ import annotations

import re
import unicodedata

# Every container an entry may use to hold actual items.
ITEM_CONTAINERS = ("brItems", "tracks", "instruments", "cars", "legoKits")


def normalize_name(name):
    """Casefold + strip accents/punctuation so 'Take The L!' == 'take the l'."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


class ShopItem:
    """One concrete cosmetic currently offered in the shop."""

    def __init__(self, item_id, name, item_type, entry, container, rarity="", image="", extra=""):
        self.id = item_id or ""
        self.name = name or ""
        self.type = item_type or "Unknown"
        self.rarity = rarity or ""
        self.image = image or ""
        self.extra = extra  # e.g. artist for a jam track
        self.container = container

        # Offer-level details live on the parent entry.
        self.final_price = entry.get("finalPrice")
        self.regular_price = entry.get("regularPrice")
        self.in_date = entry.get("inDate") or ""
        self.out_date = entry.get("outDate") or ""
        self.offer_id = entry.get("offerId") or ""
        self.giftable = entry.get("giftable", False)

        bundle = entry.get("bundle") or {}
        self.bundle_name = bundle.get("name") or ""

        layout = entry.get("layout") or {}
        self.section = layout.get("name") or layout.get("category") or ""

    @property
    def norm_name(self):
        return normalize_name(self.name)

    @property
    def key(self):
        """Stable identity for duplicate detection.

        Prefer the real item ID; fall back to a normalised name so an item
        with a missing/renamed ID is still tracked rather than silently
        re-alerting every poll.
        """
        return self.id if self.id else "name:" + self.norm_name

    @property
    def discounted(self):
        try:
            return self.regular_price is not None and self.final_price is not None \
                and self.final_price < self.regular_price
        except TypeError:
            return False

    def price_text(self):
        if self.final_price is None:
            return "Unknown"
        if self.final_price == 0:
            return "Free"
        text = "{:,} V-Bucks".format(self.final_price)
        if self.discounted:
            text += " (was {:,})".format(self.regular_price)
        return text

    def __repr__(self):
        return "<ShopItem {} '{}' {}>".format(self.id, self.name, self.type)


def _image_from(images):
    """Pick the nicest available artwork, largest first."""
    if not isinstance(images, dict):
        return ""
    for key in ("featured", "icon", "large", "full", "smallIcon", "small"):
        value = images.get(key)
        if isinstance(value, str) and value:
            return value
    # Some payloads nest another dict (e.g. images.other.*)
    for value in images.values():
        if isinstance(value, str) and value.startswith("http"):
            return value
    return ""


def extract_items(entry):
    """Turn one shop entry into a list of ShopItem, across every container."""
    items = []
    if not isinstance(entry, dict):
        return items

    for container in ITEM_CONTAINERS:
        bucket = entry.get(container)
        if not isinstance(bucket, list):
            continue

        for raw in bucket:
            if not isinstance(raw, dict):
                continue

            if container == "tracks":
                # Jam tracks use title/artist and albumArt.
                items.append(ShopItem(
                    item_id=raw.get("id"),
                    name=raw.get("title"),
                    item_type="Jam Track",
                    entry=entry,
                    container=container,
                    image=raw.get("albumArt") or "",
                    extra=raw.get("artist") or "",
                ))
                continue

            type_field = raw.get("type")
            if isinstance(type_field, dict):
                item_type = type_field.get("displayValue") or type_field.get("value") or ""
            else:
                item_type = str(type_field or "")

            rarity_field = raw.get("rarity")
            if isinstance(rarity_field, dict):
                rarity = rarity_field.get("displayValue") or rarity_field.get("value") or ""
            else:
                rarity = str(rarity_field or "")

            if not item_type:
                item_type = {
                    "instruments": "Instrument",
                    "cars": "Car Body",
                    "legoKits": "LEGO Kit",
                }.get(container, "Cosmetic")

            items.append(ShopItem(
                item_id=raw.get("id"),
                name=raw.get("name"),
                item_type=item_type,
                entry=entry,
                container=container,
                rarity=rarity,
                image=_image_from(raw.get("images")),
            ))

    return items


def flatten_shop(shop):
    """All items currently in the shop, de-duplicated by item key.

    The same cosmetic can legitimately appear in several offers (solo plus a
    bundle). We keep the cheapest offer for each, which is the one the user
    actually cares about.
    """
    best = {}
    for entry in shop.entries:
        for item in extract_items(entry):
            if not item.id and not item.norm_name:
                continue
            existing = best.get(item.key)
            if existing is None:
                best[item.key] = item
                continue
            new_price = item.final_price if item.final_price is not None else 10 ** 9
            old_price = existing.final_price if existing.final_price is not None else 10 ** 9
            if new_price < old_price:
                best[item.key] = item
    return best


def match_watchlist(shop_items, watchlist):
    """Return the watched entries that are present in the shop right now.

    Matching is by ID first (stable across renames), then by normalised name
    (survives the API changing an internal ID). Result maps the item key to a
    (ShopItem, WatchEntry) pair.
    """
    by_id = {}
    by_name = {}
    for item in shop_items.values():
        if item.id:
            by_id[item.id.casefold()] = item
        if item.norm_name:
            by_name.setdefault(item.norm_name, item)

    found = {}
    for watched in watchlist:
        hit = None

        if watched.item_id:
            hit = by_id.get(watched.item_id.casefold())

        if hit is None and watched.norm_name:
            hit = by_name.get(watched.norm_name)

        if hit is None and watched.norm_name:
            # Last resort: a watched name that is a distinct word-boundary
            # substring of a shop item, e.g. "Travis Scott" -> "Travis Scott".
            for norm, item in by_name.items():
                if watched.norm_name and watched.norm_name in norm.split(" | "):
                    hit = item
                    break

        if hit is not None:
            found[hit.key] = (hit, watched)

    return found
