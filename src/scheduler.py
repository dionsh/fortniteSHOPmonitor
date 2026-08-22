"""Adaptive poll scheduling.

The Fortnite Item Shop rotates exactly once a day, at 00:00 UTC, worldwide.
It is not a continuous stream of changes. So polling every few seconds around
the clock would add ~8,600 pointless requests a day and gain nothing.

Instead we run two modes:

    BURST  23:57 -> 00:35 UTC, every ~10s
           Catches the daily rotation within seconds of the upstream cache
           refreshing, which is the only moment that actually matters.

    IDLE   the rest of the day, every ~15 min
           Cheap insurance against Epic's occasional mid-day hotfix additions.

Budget: roughly 230 burst + 92 idle = ~320 requests/day at ~75 KB gzipped,
about 24 MB/day. That is a rounding error for the API and still gives
seconds-level detection at rotation.

Note the hard floor on latency: /v2/shop is served from a 30-minute cache, so
polling faster than ~10s cannot make detection meaningfully quicker. The burst
exists to catch the cache flip promptly, not to beat it.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta, timezone

log = logging.getLogger(__name__)

MODE_BURST = "burst"
MODE_IDLE = "idle"


def _parse_hhmm(text, fallback):
    try:
        hours, minutes = str(text).split(":")
        return dtime(int(hours), int(minutes), tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        log.warning("Bad time value %r in config; using %s.", text, fallback)
        return fallback


class Scheduler:
    def __init__(self, config):
        polling = config.polling
        self.start = _parse_hhmm(polling["burst_window_start_utc"], dtime(23, 57, tzinfo=timezone.utc))
        self.end = _parse_hhmm(polling["burst_window_end_utc"], dtime(0, 35, tzinfo=timezone.utc))
        self.burst_interval = max(5, int(polling["burst_interval_seconds"]))
        self.idle_interval = max(30, int(polling["idle_interval_seconds"]))

        # Set when the shop hash changes unexpectedly, to briefly stay hot.
        self._hot_until = None

    # ------------------------------------------------------------------
    def in_burst_window(self, now=None):
        """True inside the rotation window, which wraps around midnight."""
        now = now or datetime.now(timezone.utc)
        current = now.timetz().replace(tzinfo=timezone.utc)

        if self.start <= self.end:
            return self.start <= current <= self.end
        # Wraps midnight, e.g. 23:57 -> 00:35
        return current >= self.start or current <= self.end

    def mark_change_detected(self, now=None, minutes=10):
        """Stay in fast mode for a while after any unexpected shop change.

        Covers mid-day hotfixes: once something moves, more may follow.
        """
        now = now or datetime.now(timezone.utc)
        self._hot_until = now + timedelta(minutes=minutes)

    def _is_hot(self, now):
        return self._hot_until is not None and now < self._hot_until

    def mode(self, now=None):
        now = now or datetime.now(timezone.utc)
        if self.in_burst_window(now) or self._is_hot(now):
            return MODE_BURST
        return MODE_IDLE

    def next_interval(self, now=None):
        """Seconds to sleep before the next poll."""
        now = now or datetime.now(timezone.utc)

        if self.mode(now) == MODE_BURST:
            return self.burst_interval

        # In idle mode, never sleep past the start of the burst window.
        seconds_to_window = self.seconds_until_window(now)
        return max(5, min(self.idle_interval, seconds_to_window))

    def seconds_until_window(self, now=None):
        now = now or datetime.now(timezone.utc)
        target = datetime.combine(now.date(), self.start, tzinfo=timezone.utc)
        if target <= now:
            target += timedelta(days=1)
        return int((target - now).total_seconds())

    def describe(self, now=None):
        now = now or datetime.now(timezone.utc)
        current = self.mode(now)
        if current == MODE_BURST:
            return "BURST (every {}s)".format(self.burst_interval)
        mins = self.seconds_until_window(now) // 60
        return "IDLE (every {}s; burst window in {}h{:02d}m)".format(
            self.idle_interval, mins // 60, mins % 60)
