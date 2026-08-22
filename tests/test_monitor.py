"""Behavioural tests for the monitor.

Covers exactly the scenarios in the brief:
  * a newly appeared tracked item alerts
  * an item that stays in the shop does NOT alert again
  * an item that leaves and returns alerts again
  * several items appearing at once are handled
  * API errors do not crash the app
  * notification failures are retried, not swallowed
  * state survives a restart
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from helpers import (ExplodingNotifier, RecordingNotifier, TempConfig,
                     make_entry, make_item, make_shop)

from src.api import ApiError, MockAPI
from src.matcher import flatten_shop, match_watchlist, normalize_name
from src.monitor import Monitor
from src.notifiers import Dispatcher
from src.scheduler import MODE_BURST, MODE_IDLE, Scheduler
from src.state import DeadLetterQueue, StateStore

RENEGADE = make_item("CID_028_Athena_Commando_F", "Renegade Raider")
TAKE_THE_L = make_item("EID_TakeTheL", "Take The L", "Emote", "Icon Series")
STAR_WAND = make_item("Pickaxe_ID_179_StarWand", "Star Wand", "Pickaxe", "Epic")
FILLER = make_item("CID_999_Filler", "Some Other Skin")


def build(watchlist, scenario, notifier=None, overrides=None):
    """Wire a Monitor onto a mock API and a recording notifier."""
    config = TempConfig(watchlist_items=watchlist, overrides=overrides)
    api = MockAPI(config, scenario=scenario)
    recorder = notifier or RecordingNotifier()

    dispatcher = Dispatcher(config, DeadLetterQueue(config))
    dispatcher.active = [recorder]  # replace real channels

    monitor = Monitor(config, api=api, dispatcher=dispatcher)
    monitor.resolver.loaded = False  # no network; match by name/pinned ID
    return config, api, recorder, monitor


class TestDuplicateDetection(unittest.TestCase):
    """The core requirement: alert on appearance, never on mere presence."""

    def setUp(self):
        self.scenario = [
            make_shop([FILLER], "h0"),                       # 0: absent
            make_shop([FILLER, RENEGADE], "h1"),             # 1: appears
            make_shop([FILLER, RENEGADE], "h1"),             # 2: still there
            make_shop([FILLER, RENEGADE], "h2"),             # 3: still there, shop changed
            make_shop([FILLER], "h3"),                       # 4: leaves
            make_shop([FILLER], "h4"),                       # 5: still gone
            make_shop([FILLER, RENEGADE], "h5"),             # 6: returns
        ]
        self.config, self.api, self.rec, self.mon = build(
            ["Renegade Raider"], self.scenario,
            overrides={"notifications": {"renotify_cooldown_hours": 0}})

    def tearDown(self):
        self.config.cleanup()

    def test_full_lifecycle(self):
        # step 0 - first run seeds a baseline, item absent, no alert
        self.mon.poll_once()
        self.assertEqual(self.rec.names(), [], "first run should not alert")

        # step 1 - appears -> ALERT
        self.api.advance()
        self.mon.poll_once()
        self.assertEqual(self.rec.names(), ["Renegade Raider"])

        # steps 2,3 - still present -> silence
        for _ in range(2):
            self.api.advance()
            self.mon.poll_once()
        self.assertEqual(len(self.rec.alerts), 1, "must not re-alert while present")

        # steps 4,5 - leaves -> silence
        for _ in range(2):
            self.api.advance()
            self.mon.poll_once()
        self.assertEqual(len(self.rec.alerts), 1, "leaving must not alert")

        # step 6 - returns -> ALERT again
        self.api.advance()
        self.mon.poll_once()
        self.assertEqual(len(self.rec.alerts), 2, "return must alert again")
        self.assertEqual(self.rec.names(), ["Renegade Raider", "Renegade Raider"])

    def test_present_on_first_run_sends_startup_summary_not_new_alert(self):
        config, api, rec, mon = build(
            ["Renegade Raider"], [make_shop([RENEGADE], "h1")])
        try:
            mon.poll_once()
            self.assertEqual(len(rec.alerts), 1)
            self.assertEqual(rec.alerts[0].kind, "startup",
                             "already-present items must be a startup summary")
            # A second poll with no change stays silent.
            mon.poll_once()
            self.assertEqual(len(rec.alerts), 1)
        finally:
            config.cleanup()


class TestMultipleItems(unittest.TestCase):
    def test_several_items_appear_together(self):
        scenario = [
            make_shop([FILLER], "h0"),
            make_shop([FILLER, RENEGADE, TAKE_THE_L, STAR_WAND], "h1"),
        ]
        config, api, rec, mon = build(
            ["Renegade Raider", "Take The L", "Star Wand"], scenario)
        try:
            mon.poll_once()
            api.advance()
            mon.poll_once()

            self.assertEqual(len(rec.alerts), 1, "combine_multiple should batch them")
            self.assertCountEqual(
                rec.names(), ["Renegade Raider", "Take The L", "Star Wand"])
        finally:
            config.cleanup()

    def test_separate_messages_when_combining_disabled(self):
        scenario = [
            make_shop([FILLER], "h0"),
            make_shop([FILLER, RENEGADE, TAKE_THE_L], "h1"),
        ]
        config, api, rec, mon = build(
            ["Renegade Raider", "Take The L"], scenario,
            overrides={"notifications": {"combine_multiple": False}})
        try:
            mon.poll_once()
            api.advance()
            mon.poll_once()
            self.assertEqual(len(rec.alerts), 2, "should send one message per item")
        finally:
            config.cleanup()

    def test_staggered_arrivals_only_alert_the_new_one(self):
        scenario = [
            make_shop([FILLER], "h0"),
            make_shop([FILLER, RENEGADE], "h1"),
            make_shop([FILLER, RENEGADE, TAKE_THE_L], "h2"),
        ]
        config, api, rec, mon = build(["Renegade Raider", "Take The L"], scenario)
        try:
            mon.poll_once()
            api.advance(); mon.poll_once()
            api.advance(); mon.poll_once()

            self.assertEqual(len(rec.alerts), 2)
            self.assertEqual(rec.alerts[1].items[0].name, "Take The L",
                             "second alert must contain only the newly added item")
        finally:
            config.cleanup()


class TestResilience(unittest.TestCase):
    def test_api_failure_does_not_crash_or_lose_state(self):
        scenario = [
            make_shop([FILLER, RENEGADE], "h1"),
            make_shop([FILLER, RENEGADE], "h1"),
        ]
        config, api, rec, mon = build(["Renegade Raider"], scenario)
        try:
            mon.poll_once()                      # seed with item present
            baseline = set(mon.state.present_keys)
            self.assertTrue(baseline)

            api.fail_next = 3
            for _ in range(3):
                ok, items = mon.poll_once()
                self.assertFalse(ok, "failed poll must report failure")
                self.assertEqual(items, [])

            self.assertEqual(mon.state.present_keys, baseline,
                             "a failed poll must never overwrite good state")
            self.assertEqual(mon.consecutive_failures, 3)

            # Recovery: still present, so still silent - no false 'new' alert.
            ok, _ = mon.poll_once()
            self.assertTrue(ok)
            self.assertEqual(mon.consecutive_failures, 0)
            self.assertEqual(len(rec.alerts), 1, "recovery must not re-alert")
        finally:
            config.cleanup()

    def test_live_client_rejects_bad_payloads(self):
        """Implausible responses must raise, not be mistaken for a real shop.

        An empty `entries` list is the dangerous one: trusting it would look
        like every tracked item left the shop, and would fire a false alert
        storm the moment the API recovered.
        """
        from src.api import FortniteAPI

        bad_payloads = [
            ({"status": 200, "data": {"hash": "h", "date": "d", "entries": []}},
             "empty entries"),
            ({"status": 200, "data": {"hash": "h", "date": "d"}},
             "missing entries"),
            ({"status": 200, "data": None}, "null data"),
            ({"status": 503, "error": "maintenance"}, "non-200 API status"),
            ({"status": 200, "data": {"entries": "not-a-list"}}, "entries wrong type"),
        ]

        config = TempConfig()
        try:
            client = FortniteAPI(config)
            for payload, label in bad_payloads:
                with self.subTest(case=label):
                    client._get = lambda url, params=None, _p=payload: (_p, {})
                    with self.assertRaises(ApiError):
                        client.fetch_shop()

            # A well-formed payload still works, and parses the cache Age.
            good = {"status": 200, "data": {
                "hash": "abc", "date": "2026-08-22T00:00:00Z",
                "entries": [make_entry([RENEGADE])]}}
            client._get = lambda url, params=None: (good, {"Age": "42"})
            resp = client.fetch_shop()
            self.assertEqual(resp.hash, "abc")
            self.assertEqual(resp.age_seconds, 42)
        finally:
            config.cleanup()

    def test_freshest_variant_wins_and_survives_partial_failure(self):
        """Racing language variants picks the least-stale, and tolerates one dying."""
        from src.api import FortniteAPI

        config = TempConfig()
        try:
            client = FortniteAPI(config)
            ages = {"en": 1700, "de": 12}

            def fake_fetch(language="en"):
                if language == "fr":
                    raise ApiError("variant down")
                shop = make_shop([RENEGADE], "hash-" + language)
                from src.api import ShopResponse
                return ShopResponse(shop, ages[language], language)

            client.fetch_shop = fake_fetch
            best = client.fetch_shop_freshest(["en", "de", "fr"])
            self.assertEqual(best.language, "de", "should pick the lowest Age")
            self.assertEqual(best.age_seconds, 12)

            # All variants failing is a real error.
            client.fetch_shop = lambda language="en": (_ for _ in ()).throw(ApiError("down"))
            with self.assertRaises(ApiError):
                client.fetch_shop_freshest(["en", "de"])
        finally:
            config.cleanup()

    def test_malformed_watchlist_keeps_previous_list(self):
        config, api, rec, mon = build(
            ["Renegade Raider"], [make_shop([FILLER], "h0")])
        try:
            mon.load_watchlist(force=True)
            self.assertEqual(len(mon.watchlist), 1)

            config.watchlist_file.write_text("{ this is not json", encoding="utf-8")
            mon.load_watchlist(force=True)
            self.assertEqual(len(mon.watchlist), 1,
                             "broken JSON must not wipe the watchlist")
        finally:
            config.cleanup()

    def test_notifier_exception_does_not_propagate(self):
        scenario = [make_shop([FILLER], "h0"), make_shop([FILLER, RENEGADE], "h1")]
        config, api, rec, mon = build(["Renegade Raider"], scenario,
                                      notifier=ExplodingNotifier())
        try:
            mon.poll_once()
            api.advance()
            ok, _ = mon.poll_once()   # must not raise
            self.assertTrue(ok)
        finally:
            config.cleanup()


class TestNotificationFailure(unittest.TestCase):
    def test_failed_delivery_is_queued_and_retried(self):
        scenario = [make_shop([FILLER], "h0"), make_shop([FILLER, RENEGADE], "h1")]
        failing = RecordingNotifier(fail=True)
        config, api, rec, mon = build(["Renegade Raider"], scenario, notifier=failing)
        try:
            mon.poll_once()
            api.advance()
            mon.poll_once()

            queued = config.deadletter_file
            self.assertTrue(queued.exists(), "failed alert must be dead-lettered")
            lines = [l for l in queued.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["payload"]["notifier"], "recording")

            # Once the channel recovers, the queue drains.
            failing.fail = False
            resent = mon.dispatcher.retry_failed()
            self.assertEqual(resent, 1)
            self.assertFalse(queued.exists())
        finally:
            config.cleanup()

    def test_failed_delivery_does_not_mark_item_notified(self):
        """A failed send must not stamp the cooldown, or the alert is lost."""
        scenario = [make_shop([FILLER], "h0"), make_shop([FILLER, RENEGADE], "h1")]
        failing = RecordingNotifier(fail=True)
        config, api, rec, mon = build(["Renegade Raider"], scenario, notifier=failing)
        try:
            mon.poll_once()
            api.advance()
            mon.poll_once()
            self.assertEqual(mon.state.last_notified, {},
                             "nothing was delivered, so nothing should be stamped")
        finally:
            config.cleanup()


class TestRestartSafety(unittest.TestCase):
    def test_state_survives_restart(self):
        scenario = [make_shop([FILLER, RENEGADE], "h1")] * 3
        config, api, rec, mon = build(["Renegade Raider"], scenario)
        try:
            mon.poll_once()
            self.assertTrue(mon.state.present_keys)

            # Simulate a process restart against the same state file.
            api2 = MockAPI(config, scenario=scenario)
            rec2 = RecordingNotifier()
            dispatcher2 = Dispatcher(config, DeadLetterQueue(config))
            dispatcher2.active = [rec2]
            mon2 = Monitor(config, api=api2, dispatcher=dispatcher2)
            mon2.resolver.loaded = False

            self.assertTrue(mon2.state.seeded, "restart must load prior state")
            mon2.poll_once()
            self.assertEqual(rec2.alerts, [],
                             "restart must not re-alert for already-known items")
        finally:
            config.cleanup()

    def test_corrupt_state_is_quarantined_not_fatal(self):
        config = TempConfig(watchlist_items=["Renegade Raider"])
        try:
            config.state_file.write_text("<<<not json>>>", encoding="utf-8")
            store = StateStore(config)          # must not raise
            self.assertFalse(store.seeded)
            backups = list(config.state_dir.glob("state.corrupt-*"))
            self.assertEqual(len(backups), 1, "corrupt state should be kept for inspection")
        finally:
            config.cleanup()

    def test_atomic_write_leaves_no_temp_files(self):
        config = TempConfig(watchlist_items=[])
        try:
            store = StateStore(config)
            store.present_keys = {"a", "b"}
            store.save()
            leftovers = list(config.state_dir.glob("*.tmp"))
            self.assertEqual(leftovers, [], "temp files must be cleaned up")
            self.assertTrue(config.state_file.exists())
        finally:
            config.cleanup()


class TestCooldown(unittest.TestCase):
    def test_cooldown_absorbs_cache_variant_flapping(self):
        """Racing language variants can briefly disagree about the shop.

        That could bounce an item absent->present->absent->present within
        seconds. The cooldown must collapse that into a single alert.
        """
        scenario = [
            make_shop([FILLER], "h0"),                # absent
            make_shop([FILLER, RENEGADE], "h1"),      # fresh variant: present
            make_shop([FILLER], "h0"),                # stale variant: absent
            make_shop([FILLER, RENEGADE], "h1"),      # fresh again: present
            make_shop([FILLER], "h0"),                # stale again
            make_shop([FILLER, RENEGADE], "h1"),      # settled
        ]
        config, api, rec, mon = build(
            ["Renegade Raider"], scenario,
            overrides={"notifications": {"renotify_cooldown_hours": 1}})
        try:
            for _ in range(len(scenario)):
                mon.poll_once()
                api.advance()
            self.assertEqual(len(rec.alerts), 1,
                             "flapping must produce exactly one alert, not four")
        finally:
            config.cleanup()

    def test_one_hour_cooldown_cannot_block_a_next_day_return(self):
        """A real return is >=24h later, so the 1h guard must not suppress it."""
        from src.state import StateStore, iso, utcnow
        from datetime import timedelta

        config = TempConfig(overrides={"notifications": {"renotify_cooldown_hours": 1}})
        try:
            store = StateStore(config)
            key = "CID_028_Athena_Commando_F"
            store.last_notified[key] = iso(utcnow() - timedelta(hours=24))
            self.assertFalse(store.in_cooldown(key),
                             "a 24h-old alert must be well outside a 1h cooldown")

            store.last_notified[key] = iso(utcnow() - timedelta(minutes=5))
            self.assertTrue(store.in_cooldown(key), "5 minutes ago is inside it")
        finally:
            config.cleanup()

    def test_cooldown_blocks_rapid_repeat(self):
        """Safety net if state is lost: don't spam the same item."""
        scenario = [make_shop([FILLER], "h0"), make_shop([FILLER, RENEGADE], "h1")]
        config, api, rec, mon = build(
            ["Renegade Raider"], scenario,
            overrides={"notifications": {"renotify_cooldown_hours": 12}})
        try:
            mon.poll_once()
            api.advance()
            mon.poll_once()
            self.assertEqual(len(rec.alerts), 1)

            # Wipe presence memory but keep last_notified, as a state
            # rollback would. The cooldown must still suppress a repeat.
            mon.state.present_keys = set()
            mon.poll_once()
            self.assertEqual(len(rec.alerts), 1, "cooldown must suppress the repeat")
        finally:
            config.cleanup()


class TestMatching(unittest.TestCase):
    def test_name_normalisation(self):
        self.assertEqual(normalize_name("Take The L!"), "take the l")
        self.assertEqual(normalize_name("  RENEGADE   RAIDER "), "renegade raider")
        self.assertEqual(normalize_name("Ölaf-9"), "olaf 9")
        self.assertEqual(normalize_name(None), "")

    def test_match_by_id_survives_a_rename(self):
        renamed = make_item("CID_028_Athena_Commando_F", "Renegade Raider (Legacy)")
        shop = make_shop([renamed], "h1")

        config = TempConfig()
        try:
            from src.api import ShopResponse
            from src.watchlist import WatchEntry
            items = flatten_shop(ShopResponse(shop, 0, "en"))
            watch = [WatchEntry("Renegade Raider", "CID_028_Athena_Commando_F")]
            found = match_watchlist(items, watch)
            self.assertEqual(len(found), 1, "pinned ID should match despite the rename")
        finally:
            config.cleanup()

    def test_match_by_name_when_id_unknown(self):
        config = TempConfig()
        try:
            from src.api import ShopResponse
            from src.watchlist import WatchEntry
            items = flatten_shop(ShopResponse(make_shop([TAKE_THE_L], "h1"), 0, "en"))
            found = match_watchlist(items, [WatchEntry("take the l!")])
            self.assertEqual(len(found), 1, "name matching should be punctuation-insensitive")
        finally:
            config.cleanup()

    def test_non_br_containers_are_matched(self):
        """Jam tracks, cars and instruments must be watchable too."""
        track_entry = make_entry(
            [{"id": "SID_Track_01", "title": "Less Than", "artist": "Nine Inch Nails",
              "albumArt": "https://example.invalid/art.png"}],
            container="tracks")
        shop = {"hash": "h1", "date": "d", "entries": [track_entry]}

        config = TempConfig()
        try:
            from src.api import ShopResponse
            from src.watchlist import WatchEntry
            items = flatten_shop(ShopResponse(shop, 0, "en"))
            self.assertEqual(len(items), 1)
            item = list(items.values())[0]
            self.assertEqual(item.type, "Jam Track")
            self.assertEqual(item.extra, "Nine Inch Nails")

            found = match_watchlist(items, [WatchEntry("Less Than")])
            self.assertEqual(len(found), 1)
        finally:
            config.cleanup()

    def test_duplicate_offers_keep_the_cheapest(self):
        config = TempConfig()
        try:
            from src.api import ShopResponse
            shop = {"hash": "h", "date": "d", "entries": [
                make_entry([RENEGADE], price=1500, offer_id="a"),
                make_entry([RENEGADE], price=1200, offer_id="b"),
            ]}
            items = flatten_shop(ShopResponse(shop, 0, "en"))
            self.assertEqual(len(items), 1, "same item in two offers = one tracked item")
            self.assertEqual(list(items.values())[0].final_price, 1200)
        finally:
            config.cleanup()

    def test_price_formatting(self):
        config = TempConfig()
        try:
            from src.api import ShopResponse
            shop = {"hash": "h", "date": "d", "entries": [
                make_entry([RENEGADE], price=1200, regular=1500)]}
            item = list(flatten_shop(ShopResponse(shop, 0, "en")).values())[0]
            self.assertTrue(item.discounted)
            self.assertEqual(item.price_text(), "1,200 V-Bucks (was 1,500)")
        finally:
            config.cleanup()


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.config = TempConfig()

    def tearDown(self):
        self.config.cleanup()

    def test_burst_window_wraps_midnight(self):
        sched = Scheduler(self.config)
        def at(h, m):
            return datetime(2026, 8, 22, h, m, tzinfo=timezone.utc)

        self.assertEqual(sched.mode(at(23, 58)), MODE_BURST, "just before rotation")
        self.assertEqual(sched.mode(at(0, 0)), MODE_BURST, "at rotation")
        self.assertEqual(sched.mode(at(0, 20)), MODE_BURST, "shortly after rotation")
        self.assertEqual(sched.mode(at(0, 40)), MODE_IDLE, "after the window")
        self.assertEqual(sched.mode(at(12, 0)), MODE_IDLE, "midday")

    def test_intervals_match_mode(self):
        sched = Scheduler(self.config)
        burst = sched.next_interval(datetime(2026, 8, 22, 0, 5, tzinfo=timezone.utc))
        idle = sched.next_interval(datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(burst, self.config.polling["burst_interval_seconds"])
        self.assertEqual(idle, self.config.polling["idle_interval_seconds"])

    def test_idle_never_sleeps_past_the_window(self):
        sched = Scheduler(self.config)
        # 2 minutes before the burst window opens
        now = datetime(2026, 8, 22, 23, 55, tzinfo=timezone.utc)
        self.assertLessEqual(sched.next_interval(now), 120,
                             "must wake up in time for the rotation")

    def test_hash_change_triggers_hot_mode(self):
        sched = Scheduler(self.config)
        midday = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(sched.mode(midday), MODE_IDLE)
        sched.mark_change_detected(midday, minutes=10)
        self.assertEqual(sched.mode(midday + timedelta(minutes=5)), MODE_BURST)
        self.assertEqual(sched.mode(midday + timedelta(minutes=15)), MODE_IDLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
