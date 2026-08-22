"""The monitoring loop: poll, match, diff, notify, persist, sleep, repeat.

Design rule that everything else follows: state is only ever advanced from a
*trusted* poll. If a fetch fails or returns something implausible we log it,
back off and try again - we never let a bad response convince us that every
tracked item just left the shop (which would cause a false alert storm when
it came back).
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone

from .api import ApiError, FortniteAPI, MockAPI
from .matcher import flatten_shop, match_watchlist
from .notifiers import Alert, Dispatcher
from .scheduler import MODE_BURST, Scheduler
from .state import DeadLetterQueue, StateStore, parse_iso, shop_day, utcnow
from .watchlist import CatalogResolver, load_watchlist

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, config, api=None, dispatcher=None):
        self.config = config
        self.api = api or (MockAPI(config) if config.source == "mock" else FortniteAPI(config))

        self.deadletter = DeadLetterQueue(config)
        self.dispatcher = dispatcher or Dispatcher(config, self.deadletter)
        self.state = StateStore(config)
        self.scheduler = Scheduler(config)

        self.watchlist = []
        self.watchlist_mtime = None
        self.resolver = CatalogResolver(config, self.api)

        self.running = True
        self.consecutive_failures = 0

    # ------------------------------------------------------------------
    # watchlist handling
    # ------------------------------------------------------------------
    def load_watchlist(self, force=False):
        """(Re)load the watchlist, hot-reloading when the file changes.

        Lets the user add items while the monitor is running - no restart.
        """
        path = self.config.watchlist_file
        try:
            mtime = path.stat().st_mtime if path.exists() else None
        except OSError:
            mtime = None

        if not force and mtime == self.watchlist_mtime and self.watchlist:
            return False

        try:
            entries = load_watchlist(path)
        except Exception as exc:
            # Broken JSON while editing: keep running on the old list.
            log.error("Keeping previous watchlist: %s", exc)
            return False

        if not self.resolver.loaded:
            self.resolver.load()

        self.watchlist = self.resolver.resolve_all(entries)
        self.watchlist_mtime = mtime

        resolved = sum(1 for e in self.watchlist if e.item_id)
        log.info("Watchlist loaded: %d item(s), %d resolved to IDs.",
                 len(self.watchlist), resolved)
        for entry in self.watchlist:
            log.debug("  %s", entry)
        return True

    # ------------------------------------------------------------------
    # one poll
    # ------------------------------------------------------------------
    def poll_once(self):
        """Run a single check. Returns (ok, newly_appeared_items)."""
        self.load_watchlist()

        if not self.watchlist:
            log.warning("Watchlist is empty - nothing to look for.")
            return True, []

        try:
            shop = self.api.fetch_shop_freshest(self.config.polling["race_languages"])
        except ApiError as exc:
            self.consecutive_failures += 1
            log.error("Shop fetch failed (%d in a row): %s", self.consecutive_failures, exc)
            return False, []
        except Exception as exc:
            self.consecutive_failures += 1
            log.exception("Unexpected error fetching shop (%d in a row): %s",
                          self.consecutive_failures, exc)
            return False, []

        if self.consecutive_failures:
            log.info("Shop fetch recovered after %d failure(s).", self.consecutive_failures)
        self.consecutive_failures = 0

        shop_items = flatten_shop(shop)
        # The freshest cache hit may have been a non-English variant, so
        # restore English names/types before matching or notifying.
        if shop.language != "en":
            self.resolver.localize_all(shop_items)
        matched = match_watchlist(shop_items, self.watchlist)
        current_keys = set(matched.keys())

        hash_changed = bool(self.state.last_shop_hash) and shop.hash != self.state.last_shop_hash
        if hash_changed:
            log.info("Shop contents changed (hash %s -> %s, %d entries, cache age %ss).",
                     self.state.last_shop_hash[:8], shop.hash[:8],
                     len(shop.entries), shop.age_seconds)
            self.scheduler.mark_change_detected()

        day = shop_day(shop)

        # First ever run: establish a baseline instead of alerting on
        # everything already sitting in the shop.
        if not self.state.seeded:
            return self._handle_first_run(matched, current_keys, shop, day)

        appeared, reminders = self.state.classify(current_keys, day)

        # A daily reminder can be held back until a civilised hour; a genuine
        # new appearance always goes out immediately.
        if reminders and not self.state.reminder_window_open(day):
            reminders = set()

        if not appeared and not reminders:
            self.state.commit(current_keys, shop, day=day)
            return True, []

        delivered_keys = []
        reported = []

        if appeared:
            items = [matched[k][0] for k in sorted(appeared)]
            log.info("NEW: %s", ", ".join(i.name for i in items))
            delivered_keys += self._notify(items, kind="new")
            reported += items

        if reminders:
            items = [matched[k][0] for k in sorted(reminders)]
            log.info("STILL IN SHOP (day %s): %s", day, ", ".join(i.name for i in items))
            delivered_keys += self._notify(items, kind="reminder")
            reported += items

        self.state.commit(current_keys, shop, notified_keys=delivered_keys, day=day)
        return True, reported

    def _handle_first_run(self, matched, current_keys, shop, day):
        """Seed state on first run, reporting what is already available."""
        items = [matched[k][0] for k in sorted(current_keys)]

        if items:
            log.info("First run - %d tracked item(s) already in the shop.", len(items))
            delivered = self._notify(items, kind="startup")
        else:
            log.info("First run - none of your tracked items are in the shop right now.")
            delivered = []

        self.state.commit(current_keys, shop, notified_keys=delivered, day=day)
        return True, []

    def _notify(self, items, kind):
        """Send an alert. Returns the keys that were successfully delivered.

        Only delivered items get their cooldown stamped, so a failed send is
        retried on the next poll rather than silently swallowed.
        """
        now = utcnow()
        combine = self.config.notifications["combine_multiple"]
        delivered = []

        try:
            if combine or len(items) == 1:
                if self.dispatcher.send(Alert(items, now, kind=kind)):
                    delivered.extend(i.key for i in items)
            else:
                for item in items:
                    if self.dispatcher.send(Alert([item], now, kind=kind)):
                        delivered.append(item.key)
        except Exception as exc:
            log.exception("Notification dispatch failed: %s", exc)

        if not delivered:
            log.error("Nothing was delivered; will retry on the next poll.")

        return delivered

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def _install_signal_handlers(self):
        def handler(signum, _frame):
            log.info("Signal %s received - shutting down cleanly.", signum)
            self.running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, AttributeError, OSError):
                pass  # not available on this platform/thread

    def run(self):
        self._install_signal_handlers()
        self.load_watchlist(force=True)

        log.info("Monitoring started. Channels: %s", self.dispatcher.describe())
        if not self.dispatcher.has_remote_channel():
            log.warning("No Discord/Telegram credentials found in .env - alerts will only "
                        "print to this console.")
        log.info("Schedule: %s", self.scheduler.describe())

        while self.running:
            cycle_start = time.monotonic()

            try:
                self.dispatcher.retry_failed()
                ok, _ = self.poll_once()
            except Exception as exc:
                # Absolute backstop. The loop must never die.
                log.exception("Unhandled error in poll cycle: %s", exc)
                ok = False

            now = datetime.now(timezone.utc)

            if ok:
                interval = self.scheduler.next_interval(now)
            else:
                # Back off on repeated failure, but stay responsive in the
                # burst window so we do not sleep through the rotation.
                base = self.scheduler.next_interval(now)
                penalty = min(2 ** min(self.consecutive_failures, 6), 300)
                interval = base + penalty
                if self.scheduler.mode(now) == MODE_BURST:
                    interval = min(interval, 60)
                log.warning("Backing off %ds after failure.", interval)

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(1.0, interval - elapsed)

            # Sleep in slices so Ctrl+C is responsive during long idle waits.
            deadline = time.monotonic() + sleep_for
            while self.running and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))

        log.info("Monitor stopped.")
