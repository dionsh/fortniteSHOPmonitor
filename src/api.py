"""Fortnite API client.

Data source: https://fortnite-api.com  (free, no API key required)

Measured characteristics that shaped this client:
  * /v2/shop responses sit behind a 30-minute (1800s) server cache. The `Age`
    header climbs 1:1 with the clock and resets at exactly 1800.
  * Cache-busting query params do NOT defeat that cache.
  * HEAD is rejected (405) and there is no ETag/Last-Modified, so there is no
    cheap conditional poll - every check pulls the whole body.
  * gzip shrinks the payload from ~578 KB to ~75 KB, so we always ask for it.
  * Each `language` variant has its own independently staggered cache, so
    racing a couple of them lowers worst-case staleness.
"""
from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE = "https://fortnite-api.com"
SHOP_ENDPOINT = BASE + "/v2/shop"
COSMETICS_ENDPOINT = BASE + "/v2/cosmetics/br"
SEARCH_ENDPOINT = BASE + "/v2/cosmetics/br/search"


class ApiError(RuntimeError):
    """Raised when the API could not give us usable data."""


class ShopResponse:
    """A validated shop payload."""

    def __init__(self, data, age_seconds, language):
        self.hash = data.get("hash") or ""
        self.date = data.get("date") or ""
        self.entries = data.get("entries") or []
        self.vbuck_icon = data.get("vbuckIcon") or ""
        self.age_seconds = age_seconds
        self.language = language

    def __repr__(self):
        return (
            "<ShopResponse hash={} date={} entries={} age={}s lang={}>".format(
                self.hash[:12], self.date, len(self.entries),
                self.age_seconds, self.language,
            )
        )


class FortniteAPI:
    def __init__(self, config):
        self.config = config
        self.timeout = config.http["timeout_seconds"]
        self.max_retries = config.http["max_retries"]
        self.backoff_base = config.http["backoff_base_seconds"]
        self.backoff_max = config.http["backoff_max_seconds"]

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.http["user_agent"],
            "Accept": "application/json",
            # 578 KB -> 75 KB. Not optional; this is what makes frequent
            # polling reasonable for a free public API.
            "Accept-Encoding": "gzip, deflate",
        })

    # ------------------------------------------------------------------
    # low level
    # ------------------------------------------------------------------
    def _get(self, url, params=None):
        """GET with retry + exponential backoff + jitter.

        Retries transient problems (network errors, 5xx, 429) and gives up on
        genuine client errors, which retrying would never fix.
        """
        last_error = None

        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
                delay += random.uniform(0, delay * 0.25)  # jitter: avoid lockstep retries
                log.warning("Retry %d/%d for %s in %.1fs (%s)",
                            attempt, self.max_retries, url, delay, last_error)
                time.sleep(delay)

            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc  # DNS failure, connection reset, timeout, no internet
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = self.backoff_max
                if retry_after:
                    try:
                        wait = min(float(retry_after), 300.0)
                    except ValueError:
                        pass
                log.warning("Rate limited (429). Waiting %.0fs before retrying.", wait)
                time.sleep(wait)
                last_error = ApiError("rate limited")
                continue

            if 500 <= resp.status_code < 600:
                last_error = ApiError("server error {}".format(resp.status_code))
                continue

            if resp.status_code != 200:
                # 4xx other than 429 - retrying is pointless.
                raise ApiError("{} returned HTTP {}".format(url, resp.status_code))

            try:
                payload = resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                # Malformed body, or an HTML error page from a proxy.
                last_error = ApiError("invalid JSON: {}".format(exc))
                continue

            if not isinstance(payload, dict):
                last_error = ApiError("response was not a JSON object")
                continue

            return payload, dict(resp.headers)

        raise ApiError("{} failed after {} retries: {}".format(
            url, self.max_retries, last_error))

    # ------------------------------------------------------------------
    # shop
    # ------------------------------------------------------------------
    def fetch_shop(self, language="en"):
        payload, headers = self._get(SHOP_ENDPOINT, {"language": language})

        if payload.get("status") != 200:
            raise ApiError("API status {}: {}".format(
                payload.get("status"), payload.get("error")))

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ApiError("shop payload had no 'data' object")

        entries = data.get("entries")
        if not isinstance(entries, list):
            raise ApiError("shop payload had no 'entries' list")
        if not entries:
            # A genuinely empty shop should never happen. Treat as bad data
            # rather than concluding every tracked item just left.
            raise ApiError("shop payload contained zero entries - refusing to trust it")

        age = None
        try:
            age = int(headers.get("Age", ""))
        except (TypeError, ValueError):
            pass

        return ShopResponse(data, age, language)

    def fetch_shop_freshest(self, languages):
        """Fetch several language variants and return the least-stale one.

        Each variant has its own independently staggered 30-minute cache, so
        the lowest `Age` is the freshest view of the shop available to us.
        Any variant that fails is skipped; we only fail if all of them do.
        """
        if not languages:
            languages = ["en"]

        best = None
        errors = []

        for lang in languages:
            try:
                resp = self.fetch_shop(lang)
            except ApiError as exc:
                errors.append("{}: {}".format(lang, exc))
                continue
            if best is None:
                best = resp
            elif (resp.age_seconds is not None and best.age_seconds is not None
                  and resp.age_seconds < best.age_seconds):
                best = resp

        if best is None:
            raise ApiError("all language variants failed ({})".format("; ".join(errors)))
        if errors:
            log.warning("Some shop variants failed: %s", "; ".join(errors))
        return best

    # ------------------------------------------------------------------
    # cosmetics catalog (for name -> ID resolution)
    # ------------------------------------------------------------------
    def fetch_cosmetics(self):
        payload, _ = self._get(COSMETICS_ENDPOINT)
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise ApiError("cosmetics payload was empty or malformed")
        return data

    def search_cosmetic(self, name):
        try:
            payload, _ = self._get(SEARCH_ENDPOINT, {"name": name, "matchMethod": "full"})
        except ApiError:
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None


class MockAPI:
    """Offline stand-in driven by JSON fixtures, for tests and dry runs.

    Set FORTNITE_SHOP_SOURCE=mock. Point it at a scenario file listing the
    shop states to walk through, so an item entering, staying, leaving and
    returning can all be exercised without waiting for the real shop.
    """

    def __init__(self, config, scenario=None):
        self.config = config
        self.step = 0
        self.fail_next = 0  # simulate N consecutive API failures

        if scenario is not None:
            # Explicit scenario: the caller drives stepping (tests do this).
            self.scenario = scenario
            self.auto_advance = False
            self.step_file = None
        else:
            path = Path(config.root) / "tests" / "fixtures" / "scenario.json"
            if path.exists():
                self.scenario = json.loads(path.read_text(encoding="utf-8"))
            else:
                self.scenario = []
            # Driven from run.py: walk the scenario automatically, and
            # remember where we are so repeated `--once` runs make progress.
            self.auto_advance = True
            self.step_file = Path(config.state_dir) / "mock_step.txt"
            self.step = self._read_step()

    def _read_step(self):
        try:
            return int(self.step_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError, AttributeError):
            return 0

    def _write_step(self, value):
        try:
            self.step_file.write_text(str(value), encoding="utf-8")
        except (OSError, AttributeError):
            pass

    def fetch_shop(self, language="en"):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ApiError("simulated API failure")
        if not self.scenario:
            raise ApiError("mock scenario is empty")

        index = min(self.step, len(self.scenario) - 1)
        state = self.scenario[index]
        log.info("MOCK step %d/%d: %s", index + 1, len(self.scenario),
                 state.get("_label", state.get("hash", "")))

        if self.auto_advance:
            self.step = index + 1
            self._write_step(self.step)

        return ShopResponse(state, 0, language)

    def fetch_shop_freshest(self, languages):
        return self.fetch_shop(languages[0] if languages else "en")

    def advance(self):
        self.step += 1

    def fetch_cosmetics(self):
        return []

    def search_cosmetic(self, name):
        return None
