"""Console notifier - always on, so a run is never silent.

Also the channel used by the test suite to assert what *would* have been sent
without touching any network service.
"""
from __future__ import annotations

import logging

from .base import Notifier, SHOP_URL

log = logging.getLogger(__name__)


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, config):
        self.config = config
        self.sent = []  # captured alerts, for tests

    def enabled(self):
        return True

    def build_payload(self, alert):
        return alert.to_dict()

    def render(self, alert):
        bar = "=" * 60
        lines = [bar, alert.title(), bar, "", alert.headline(), ""]

        for item in alert.items[: self.config.notifications["max_items_per_message"]]:
            lines.append("  {}".format(item.name or "Unknown"))
            lines.append("    Type:   {}".format(item.type))
            if item.rarity:
                lines.append("    Rarity: {}".format(item.rarity))
            if item.extra:
                lines.append("    Artist: {}".format(item.extra))
            lines.append("    Price:  {}".format(item.price_text()))
            if item.bundle_name:
                lines.append("    Bundle: {}".format(item.bundle_name))
            if item.image:
                lines.append("    Image:  {}".format(item.image))
            if item.id:
                lines.append("    ID:     {}".format(item.id))
            lines.append("")

        lines.append("Detected: {}".format(
            alert.detected_at.strftime("%H:%M:%S UTC on %d %b %Y")))
        lines.append("Shop: {}".format(SHOP_URL))
        lines.append(bar)
        return "\n".join(lines)

    def post(self, payload):
        return True

    def send(self, alert):
        self.sent.append(alert)
        print(self.render(alert), flush=True)
        return True

    def send_raw(self, payload):
        return True
