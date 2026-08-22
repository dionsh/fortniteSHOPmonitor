"""Discord webhook notifier - the recommended channel.

Free, no quota, ~30 second setup, and rich embeds carry the item artwork.
Webhook limits are 30 messages/minute per webhook and 5 requests/5s per
channel; a shop rotation produces one message, so we are nowhere near them,
but 429 is still handled properly because Discord does enforce it.
"""
from __future__ import annotations

import logging
import time

import requests

from .base import Alert, Notifier, SHOP_URL

log = logging.getLogger(__name__)

# Discord embed colours by rarity, so an alert is readable at a glance.
RARITY_COLORS = {
    "common": 0xB1B1B1,
    "uncommon": 0x5BFF45,
    "rare": 0x3F9DFF,
    "epic": 0xC44DFF,
    "legendary": 0xFF9A3D,
    "mythic": 0xFFD84D,
    "icon series": 0x5AE3E8,
    "dark series": 0xC03BFF,
    "marvel series": 0xED1D24,
    "dc series": 0x0074C6,
    "star wars series": 0x000000,
    "gaming legends series": 0x5B2AD6,
    "shadow series": 0x4A4A4A,
    "slurp series": 0x21C7C7,
    "lava series": 0xE8622D,
    "frozen series": 0x9BE3F5,
}
DEFAULT_COLOR = 0xFF4D4D
STARTUP_COLOR = 0x5865F2

MAX_EMBEDS = 10  # Discord hard limit per message


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(self, config):
        self.config = config
        self.timeout = config.http["timeout_seconds"]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.http["user_agent"]})

    def enabled(self):
        url = self.config.discord_webhook
        return bool(url) and url.startswith("https://")

    # ------------------------------------------------------------------
    def _embed_for(self, item, kind):
        color = RARITY_COLORS.get((item.rarity or "").casefold(), DEFAULT_COLOR)
        if kind == "startup":
            color = STARTUP_COLOR

        fields = [
            {"name": "Type", "value": item.type or "Unknown", "inline": True},
            {"name": "Price", "value": item.price_text(), "inline": True},
        ]
        if item.rarity:
            fields.append({"name": "Rarity", "value": item.rarity, "inline": True})
        if item.extra:  # jam track artist
            fields.append({"name": "Artist", "value": item.extra, "inline": True})
        if item.bundle_name:
            fields.append({"name": "Part of bundle", "value": item.bundle_name, "inline": True})
        if item.section:
            fields.append({"name": "Shop section", "value": item.section, "inline": True})
        if item.out_date:
            fields.append({"name": "Leaves", "value": item.out_date[:10], "inline": True})

        embed = {
            "title": item.name or "Unknown item",
            "url": SHOP_URL,
            "color": color,
            "fields": fields,
            "footer": {"text": "Item ID: {}".format(item.id or "n/a")},
        }
        if item.image:
            embed["thumbnail"] = {"url": item.image}
        return embed

    def build_payload(self, alert):
        items = alert.items[: self.config.notifications["max_items_per_message"]]
        embeds = [self._embed_for(i, alert.kind) for i in items][:MAX_EMBEDS]

        stamp = alert.detected_at.strftime("%H:%M:%S UTC on %d %b %Y")

        content_lines = [
            "**{}**".format(alert.title()),
            "",
            alert.headline(),
            "",
            "Detected: {}".format(stamp),
        ]
        if len(alert.items) > len(items):
            content_lines.append("...and {} more.".format(len(alert.items) - len(items)))
        content_lines.append("Shop: <{}>".format(SHOP_URL))

        return {
            "username": "Fortnite Shop Monitor",
            "content": "\n".join(content_lines)[:2000],
            "embeds": embeds,
            "allowed_mentions": {"parse": []},
        }

    # ------------------------------------------------------------------
    def post(self, payload):
        """POST with 429 handling. Returns True only on a 2xx."""
        for attempt in range(4):
            try:
                resp = self.session.post(
                    self.config.discord_webhook, json=payload, timeout=self.timeout
                )
            except requests.RequestException as exc:
                log.warning("Discord POST failed (%s), attempt %d/4.", exc, attempt + 1)
                time.sleep(min(2 ** attempt, 15))
                continue

            if resp.status_code == 429:
                wait = 5.0
                try:
                    wait = float(resp.json().get("retry_after", 5.0))
                except Exception:
                    pass
                log.warning("Discord rate limited; waiting %.1fs.", wait)
                time.sleep(min(wait + 0.5, 60))
                continue

            if 200 <= resp.status_code < 300:
                return True

            if resp.status_code in (400, 401, 403, 404):
                # Bad webhook URL or malformed payload - retrying cannot help.
                log.error("Discord rejected the message (HTTP %s): %s",
                          resp.status_code, resp.text[:300])
                return False

            log.warning("Discord returned HTTP %s, retrying.", resp.status_code)
            time.sleep(min(2 ** attempt, 15))

        return False

    def send(self, alert):
        if not self.enabled():
            return False
        try:
            return self.post(self.build_payload(alert))
        except Exception as exc:  # never let a notifier take down the monitor
            log.exception("Unexpected Discord error: %s", exc)
            return False

    def send_raw(self, payload):
        if not self.enabled():
            return False
        try:
            return self.post(payload)
        except Exception:
            return False
