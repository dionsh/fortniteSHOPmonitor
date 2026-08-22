"""Persistent state and the absent -> present transition logic.

Duplicate suppression is deliberately *not* "is this item in the shop?" - that
would re-fire on every poll. Instead we remember the set of watched items that
were present at the last successful poll and alert only on the transition from
absent to present:

    poll N-1: {}                    poll N: {renegade}   -> NEW, alert
    poll N:   {renegade}            poll N+1: {renegade} -> unchanged, silent
    ...days later: {}               -> left the shop, silent
    ...weeks later: {renegade}      -> absent->present again, alert

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

STATE_VERSION = 1


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


class StateStore:
    def __init__(self, config):
        self.config = config
        self.path = config.state_file

        self.present_keys = set()      # watched items present at last good poll
        self.last_notified = {}        # key -> ISO timestamp of last alert
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
        self.last_shop_hash = raw.get("last_shop_hash") or ""
        self.last_shop_date = raw.get("last_shop_date") or ""
        self.last_successful_poll = raw.get("last_successful_poll") or ""
        self.seeded = bool(raw.get("seeded"))

        if not isinstance(self.last_notified, dict):
            self.last_notified = {}

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

        Pure safety net against pathological cases (state loss, clock jumps).
        Normal absent->present logic already prevents repeats.
        """
        hours = self.config.notifications["renotify_cooldown_hours"]
        if hours <= 0:
            return False
        last = parse_iso(self.last_notified.get(key))
        if last is None:
            return False
        return utcnow() - last < timedelta(hours=hours)

    def diff(self, current_keys):
        """Which watched items just appeared, relative to the last good poll."""
        newly = set(current_keys) - self.present_keys
        return {k for k in newly if not self.in_cooldown(k)}

    def commit(self, current_keys, shop, notified_keys=()):
        """Record a successful poll. Only ever called with trusted data."""
        now = iso(utcnow())
        for key in notified_keys:
            self.last_notified[key] = now

        self.present_keys = set(current_keys)
        self.last_shop_hash = shop.hash
        self.last_shop_date = shop.date
        self.last_successful_poll = now
        self.seeded = True

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
