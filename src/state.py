"""Persistent state, appearance detection, and daily reminders.

Two separate questions are answered here, and keeping them separate is what
makes the behaviour predictable:

  1. "Did this item just appear?"  -> absent -> present transition.
     Tracked with `present_keys`, the set of watched items seen at the last
     *successful* poll. This is what makes a return months later alert again.

  2. "Have I already told you about it today?" -> `last_alert_date`, the shop
     day we last alerted for each item.

With `repeat_daily_while_in_shop` enabled you get one alert per shop day for
as long as an item stays in the shop:

    Day 1 00:01  appears        -> ALERT   (new appearance)
    Day 1 00:15  still there    -> silent  (already alerted for day 1)
    Day 2        still there    -> ALERT   (new shop day)
    Day 3        still there    -> ALERT   (new shop day)
    Day 4        gone           -> silent
    Months later returns        -> ALERT   (absent -> present again)

"Day" is the shop's own date field, not a rolling 24h window, so reminders
land with the 00:00 UTC rotation rather than drifting.

Everything is written atomically (temp file + os.replace) so a crash or power
cut mid-write can never leave a half-written state file behind.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

STATE_VERSION = 2


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def parse_iso(text):
    if not text:
        return None
    try:
        cleaned = str(text).replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def shop_day(shop):
    """The shop's own day key, e.g. '2026-08-22'.

    Using the API's date rather than our clock means reminders stay aligned
    to the 00:00 UTC rotation even if the host's time is off.
    """
    date = getattr(shop, "date", "") or ""
    if len(date) >= 10:
        return date[:10]
    return utcnow().strftime("%Y-%m-%d")


class StateStore:
    def __init__(self, config):
        self.config = config
        self.path = config.state_file

        self.present_keys = set()      # watched items present at last good poll
        self.last_notified = {}        # key -> ISO timestamp of last alert
        self.last_alert_date = {}      # key -> shop day we last alerted for
        self.last_shop_hash = ""
        self.last_shop_date = ""
        self.last_successful_poll = ""
        self.seeded = False            # has the first baseline poll happened?

        self.load()

    # ------------------------------------------------------------------
    def load(self):
        if not self.path.exists():
            log.info("No previous state found - this looks like a first run.")
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Corrupt state must not crash us. Starting clean re-seeds a
            # baseline; at worst that costs one summary message.
            log.error("State file unreadable (%s). Starting from a clean state.", exc)
            self._quarantine()
            return

        if not isinstance(raw, dict):
            log.error("State file was not an object. Starting clean.")
            return

        self.present_keys = set(raw.get("present_keys") or [])
        self.last_notified = raw.get("last_notified") or {}
        self.last_alert_date = raw.get("last_alert_date") or {}
        self.last_shop_hash = raw.get("last_shop_hash") or ""
        self.last_shop_date = raw.get("last_shop_date") or ""
        self.last_successful_poll = raw.get("last_successful_poll") or ""
        self.seeded = bool(raw.get("seeded"))

        if not isinstance(self.last_notified, dict):
            self.last_notified = {}
        if not isinstance(self.last_alert_date, dict):
            self.last_alert_date = {}

        # Migrating v1 state, which had no last_alert_date. Backfill the
        # items already in the shop with the day we last saw, so upgrading
        # does not fire a spurious "reminder" for everything at once.
        if raw.get("version", 1) < 2 and self.present_keys and not self.last_alert_date:
            day = (self.last_shop_date or "")[:10] or utcnow().strftime("%Y-%m-%d")
            self.last_alert_date = {k: day for k in self.present_keys}
            log.info("Migrated state to v%d (backfilled %d reminder date(s)).",
                     STATE_VERSION, len(self.last_alert_date))

        log.info("Loaded state: %d watched item(s) currently in shop, last poll %s.",
                 len(self.present_keys), self.last_successful_poll or "never")

    def _quarantine(self):
        """Move a corrupt state file aside instead of silently deleting it."""
        try:
            backup = self.path.with_suffix(".corrupt-{}".format(int(utcnow().timestamp())))
            self.path.replace(backup)
            log.warning("Corrupt state moved to %s", backup.name)
        except OSError:
            pass

    # ------------------------------------------------------------------
    def save(self):
        payload = {
            "version": STATE_VERSION,
            "present_keys": sorted(self.present_keys),
            "last_notified": self.last_notified,
            "last_alert_date": self.last_alert_date,
            "last_shop_hash": self.last_shop_hash,
            "last_shop_date": self.last_shop_date,
            "last_successful_poll": self.last_successful_poll,
            "seeded": self.seeded,
        }

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)  # atomic on Windows and POSIX
        except OSError as exc:
            log.error("Could not persist state: %s", exc)

    # ------------------------------------------------------------------
    def in_cooldown(self, key):
        """True if this item was alerted too recently to alert again.

        Anti-flap guard only. Racing language variants can briefly disagree
        about the shop, which would otherwise bounce an item
        absent->present->absent. Far shorter than a day, so it can never
        block a daily reminder or a real return.
        """
        hours = self.config.notifications["renotify_cooldown_hours"]
        if hours <= 0:
            return False
        last = parse_iso(self.last_notified.get(key))
        if last is None:
            return False
        return utcnow() - last < timedelta(hours=hours)

    # ------------------------------------------------------------------
    def classify(self, current_keys, day):
        """Split the watched items in the shop into what needs alerting.

        Returns (appeared, reminders):
          appeared  - newly in the shop, or present but never yet alerted
                      (which happens when an earlier delivery failed)
          reminders - still in the shop, but last alerted on an earlier day
        """
        current_keys = set(current_keys)

        # Anything already alerted for today needs nothing.
        pending = {k for k in current_keys if self.last_alert_date.get(k) != day}
        pending = {k for k in pending if not self.in_cooldown(k)}

        appeared = set()
        reminders = set()

        for key in pending:
            if key not in self.present_keys:
                appeared.add(key)            # absent -> present
            elif self.last_alert_date.get(key) is None:
                appeared.add(key)            # present, but never told the user
            else:
                reminders.add(key)           # present since an earlier day

        if not self.repeat_daily:
            reminders = set()

        return appeared, reminders

    @property
    def repeat_daily(self):
        return bool(self.config.notifications.get("repeat_daily_while_in_shop", False))

    def reminder_window_open(self, day, now=None):
        """Whether today's reminder is allowed to go out yet.

        `reminder_at_utc` shifts the daily nudge away from the 00:00 UTC
        rotation, which is the middle of the night in many timezones.
        """
        raw = self.config.notifications.get("reminder_at_utc") or "00:00"
        try:
            hh, mm = (int(part) for part in str(raw).split(":"))
        except (ValueError, TypeError):
            log.warning("Bad reminder_at_utc %r; treating as 00:00.", raw)
            hh, mm = 0, 0

        try:
            date = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True

        threshold = date.replace(hour=hh, minute=mm)
        return (now or utcnow()) >= threshold

    def diff(self, current_keys):
        """Newly appeared items only. Kept for callers that ignore reminders."""
        newly = set(current_keys) - self.present_keys
        return {k for k in newly if not self.in_cooldown(k)}

    # ------------------------------------------------------------------
    def commit(self, current_keys, shop, notified_keys=(), day=None):
        """Record a successful poll. Only ever called with trusted data."""
        now = iso(utcnow())
        day = day or shop_day(shop)

        for key in notified_keys:
            self.last_notified[key] = now
            self.last_alert_date[key] = day

        self.present_keys = set(current_keys)
        self.last_shop_hash = shop.hash
        self.last_shop_date = shop.date
        self.last_successful_poll = now
        self.seeded = True

        # An item that has left the shop needs no reminder bookkeeping; drop
        # it so a later return starts clean.
        self.last_alert_date = {k: v for k, v in self.last_alert_date.items()
                                if k in self.present_keys}

        # Keep last_notified from growing without bound.
        if len(self.last_notified) > 500:
            trimmed = sorted(self.last_notified.items(), key=lambda kv: kv[1], reverse=True)[:300]
            self.last_notified = dict(trimmed)

        self.save()


class DeadLetterQueue:
    """Notifications that could not be delivered, so nothing is lost.

    Appended as JSON lines. Retried on the next poll; a message that keeps
    failing stays on disk for inspection rather than vanishing.
    """

    def __init__(self, config):
        self.path = config.deadletter_file

    def add(self, payload):
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"queued_at": iso(utcnow()), "payload": payload}) + "\n")
        except OSError as exc:
            log.error("Could not write dead-letter entry: %s", exc)

    def drain(self):
        """Return queued payloads and clear the file."""
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.error("Could not read dead-letter queue: %s", exc)
            return []

        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line)["payload"])
            except (json.JSONDecodeError, KeyError):
                continue

        try:
            self.path.unlink()
        except OSError:
            pass
        return out
