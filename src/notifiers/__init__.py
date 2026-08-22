"""Notifier registry and fan-out dispatcher."""
from __future__ import annotations

import logging

from .base import Alert, Notifier, SHOP_URL
from .console import ConsoleNotifier
from .discord import DiscordNotifier
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)

__all__ = ["Alert", "Notifier", "SHOP_URL", "Dispatcher",
           "ConsoleNotifier", "DiscordNotifier", "TelegramNotifier"]


class Dispatcher:
    """Sends an alert to every enabled channel.

    Delivery is best-effort per channel and deliberately independent: Discord
    failing must not stop Telegram, and a total failure queues the payload for
    retry rather than dropping it. Success on *any* channel counts as
    delivered for duplicate-suppression purposes, so a flaky second channel
    can never cause a repeat alert on the first.
    """

    def __init__(self, config, deadletter=None, extra=None):
        self.config = config
        self.deadletter = deadletter

        self.channels = [ConsoleNotifier(config), DiscordNotifier(config),
                         TelegramNotifier(config)]
        if extra:
            self.channels.extend(extra)

        self.active = [c for c in self.channels if c.enabled()]

    def describe(self):
        remote = [c.name for c in self.active if c.name != "console"]
        return ", ".join(remote) if remote else "console only (no remote channel configured)"

    def has_remote_channel(self):
        return any(c.name != "console" for c in self.active)

    # ------------------------------------------------------------------
    def send(self, alert):
        """Returns True if at least one channel accepted the alert."""
        delivered = False

        for channel in self.active:
            try:
                ok = channel.send(alert)
            except Exception as exc:  # a notifier bug must not kill the loop
                log.exception("Notifier '%s' raised: %s", channel.name, exc)
                ok = False

            if ok:
                delivered = True
                if channel.name != "console":
                    log.info("Alert delivered via %s.", channel.name)
            else:
                log.error("Notifier '%s' failed to deliver.", channel.name)
                if self.deadletter is not None and channel.name != "console":
                    try:
                        self.deadletter.add({
                            "notifier": channel.name,
                            "payload": channel.build_payload(alert),
                        })
                    except Exception:
                        log.exception("Could not queue failed %s alert.", channel.name)

        return delivered

    def retry_failed(self):
        """Replay anything the dead-letter queue is holding."""
        if self.deadletter is None:
            return 0

        pending = self.deadletter.drain()
        if not pending:
            return 0

        log.info("Retrying %d queued notification(s).", len(pending))
        by_name = {c.name: c for c in self.active}
        resent = 0

        for record in pending:
            name = record.get("notifier")
            payload = record.get("payload")
            channel = by_name.get(name)

            if channel is None or payload is None:
                continue

            try:
                ok = channel.send_raw(payload)
            except Exception:
                ok = False

            if ok:
                resent += 1
            else:
                self.deadletter.add(record)  # still failing - keep it queued

        if resent:
            log.info("Re-sent %d queued notification(s).", resent)
        return resent
