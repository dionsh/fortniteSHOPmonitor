"""Telegram bot notifier - alternative or redundant second channel.

Setup is slightly longer than Discord (BotFather token, then chat id), but it
is equally free and instant. Running Discord and Telegram together gives real
redundancy: if one service is down, the other still delivers.
"""
from __future__ import annotations

import html
import logging
import time

import requests

from .base import Alert, Notifier, SHOP_URL

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 4096


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, config):
        self.config = config
        self.timeout = config.http["timeout_seconds"]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.http["user_agent"]})

    def enabled(self):
        return bool(self.config.telegram_token and self.config.telegram_chat_id)

    # ------------------------------------------------------------------
    def _render(self, alert):
        esc = html.escape
        lines = ["<b>{}</b>".format(esc(alert.title())), ""]

        if alert.kind == "startup":
            lines.append(esc("Already in the shop right now:"))
        elif alert.is_single:
            lines.append("<b>{}</b> is now in the Item Shop!".format(esc(alert.items[0].name)))
        else:
            lines.append("{} of your tracked items are in the Item Shop!".format(len(alert.items)))
        lines.append("")

        limit = self.config.notifications["max_items_per_message"]
        for item in alert.items[:limit]:
            lines.append("<b>{}</b>".format(esc(item.name or "Unknown")))
            lines.append("  Type: {}".format(esc(item.type)))
            if item.rarity:
                lines.append("  Rarity: {}".format(esc(item.rarity)))
            if item.extra:
                lines.append("  Artist: {}".format(esc(item.extra)))
            lines.append("  Price: {}".format(esc(item.price_text())))
            if item.bundle_name:
                lines.append("  Bundle: {}".format(esc(item.bundle_name)))
            if item.out_date:
                lines.append("  Leaves: {}".format(esc(item.out_date[:10])))
            if item.image:
                lines.append('  <a href="{}">Image</a>'.format(esc(item.image)))
            lines.append("")

        if len(alert.items) > limit:
            lines.append("...and {} more.".format(len(alert.items) - limit))

        lines.append("Detected: {}".format(alert.detected_at.strftime("%H:%M:%S UTC on %d %b %Y")))
        lines.append('<a href="{}">Open the Item Shop</a>'.format(SHOP_URL))

        return "\n".join(lines)[:MAX_MESSAGE]

    def build_payload(self, alert):
        return {
            "chat_id": self.config.telegram_chat_id,
            "text": self._render(alert),
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

    # ------------------------------------------------------------------
    def post(self, payload):
        url = API_BASE.format(token=self.config.telegram_token, method="sendMessage")

        for attempt in range(4):
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("Telegram POST failed (%s), attempt %d/4.", exc, attempt + 1)
                time.sleep(min(2 ** attempt, 15))
                continue

            if resp.status_code == 429:
                wait = 5.0
                try:
                    wait = float(resp.json()["parameters"]["retry_after"])
                except Exception:
                    pass
                log.warning("Telegram rate limited; waiting %.1fs.", wait)
                time.sleep(min(wait + 0.5, 60))
                continue

            if 200 <= resp.status_code < 300:
                return True

            if resp.status_code in (400, 401, 403, 404):
                log.error("Telegram rejected the message (HTTP %s): %s",
                          resp.status_code, resp.text[:300])
                return False

            log.warning("Telegram returned HTTP %s, retrying.", resp.status_code)
            time.sleep(min(2 ** attempt, 15))

        return False

    def send(self, alert):
        if not self.enabled():
            return False
        try:
            return self.post(self.build_payload(alert))
        except Exception as exc:
            log.exception("Unexpected Telegram error: %s", exc)
            return False

    def send_raw(self, payload):
        if not self.enabled():
            return False
        try:
            return self.post(payload)
        except Exception:
            return False
