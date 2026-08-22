"""Notifier interface and the shared alert payload."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SHOP_URL = "https://fortnite.gg/shop"


class Alert:
    """A delivery-agnostic description of what to tell the user.

    Notifiers render this into their own format, so adding a channel never
    touches the monitoring logic.
    """

    def __init__(self, items, detected_at, kind="new"):
        self.items = items            # list of ShopItem
        self.detected_at = detected_at  # datetime, UTC
        self.kind = kind              # "new" | "startup" | "test"

    @property
    def is_single(self):
        return len(self.items) == 1

    def title(self):
        if self.kind == "startup":
            return "Fortnite Shop Monitor started"
        if self.kind == "test":
            return "Fortnite Shop Monitor test"
        if self.kind == "reminder":
            if self.is_single:
                return "STILL IN THE ITEM SHOP"
            return "STILL IN THE ITEM SHOP ({} items)".format(len(self.items))
        if self.is_single:
            return "FORTNITE ITEM SHOP ALERT"
        return "FORTNITE ITEM SHOP ALERT ({} items)".format(len(self.items))

    def headline(self):
        if self.kind == "startup":
            return "Already in the shop right now:"
        if self.kind == "reminder":
            # Deliberately not "is now in the shop" - it appeared on an
            # earlier day, and saying otherwise would be misleading.
            if self.is_single:
                return "**{}** is still available in the Item Shop today.".format(
                    self.items[0].name)
            return "{} of your tracked items are still in the Item Shop today.".format(
                len(self.items))
        if self.is_single:
            return "**{}** is now in the Item Shop!".format(self.items[0].name)
        return "{} of your tracked items are in the Item Shop!".format(len(self.items))

    def to_dict(self):
        """Serialisable form, used by the dead-letter queue."""
        return {
            "kind": self.kind,
            "detected_at": self.detected_at.isoformat(),
            "items": [
                {
                    "id": i.id, "name": i.name, "type": i.type, "rarity": i.rarity,
                    "price": i.price_text(), "image": i.image, "extra": i.extra,
                    "in_date": i.in_date, "out_date": i.out_date,
                    "bundle": i.bundle_name, "section": i.section,
                }
                for i in self.items
            ],
        }


class Notifier:
    name = "base"

    def enabled(self):
        raise NotImplementedError

    def send(self, alert):
        """Deliver the alert. Return True on success, False on failure.

        Must never raise - the monitor treats a False as "queue and retry".
        """
        raise NotImplementedError

    def send_raw(self, payload):
        """Re-send a previously serialised alert from the dead-letter queue."""
        raise NotImplementedError
