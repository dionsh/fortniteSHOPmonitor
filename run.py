#!/usr/bin/env python3
"""Fortnite Item Shop Monitor - entry point.

    python run.py                 start monitoring (this is the normal one)
    python run.py --once          run a single check and exit
    python run.py --test-notify   send a test alert to your configured channels
    python run.py --status        show config, watchlist and state, then exit
    python run.py --reset-state   forget what is currently in the shop
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from datetime import datetime, timezone

from src.api import ApiError, FortniteAPI, MockAPI
from src.config import Config
from src.console import enable_utf8_stdout
from src.matcher import flatten_shop, match_watchlist
from src.monitor import Monitor
from src.notifiers import Alert, Dispatcher
from src.state import DeadLetterQueue, StateStore


def setup_logging(config):
    enable_utf8_stdout()
    level = getattr(logging, config.log_level, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file log so an unattended 24/7 run cannot fill the disk.
    try:
        handler = logging.handlers.RotatingFileHandler(
            config.logs_dir / "monitor.log", maxBytes=2_000_000,
            backupCount=3, encoding="utf-8")
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError as exc:
        print("Warning: file logging disabled ({})".format(exc), file=sys.stderr)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root


def cmd_status(config):
    from src.watchlist import CatalogResolver, load_watchlist

    api = MockAPI(config) if config.source == "mock" else FortniteAPI(config)
    state = StateStore(config)
    dispatcher = Dispatcher(config)

    print("\n=== Fortnite Shop Monitor - status ===\n")
    print("Source:          {}".format(config.source))
    print("Channels:        {}".format(dispatcher.describe()))
    print("Burst window:    {} -> {} UTC every {}s".format(
        config.polling["burst_window_start_utc"],
        config.polling["burst_window_end_utc"],
        config.polling["burst_interval_seconds"]))
    print("Idle interval:   {}s".format(config.polling["idle_interval_seconds"]))
    print("Racing langs:    {}".format(", ".join(config.polling["race_languages"])))
    print("State file:      {}".format(config.state_file))
    print("Seeded:          {}".format(state.seeded))
    print("Last poll:       {}".format(state.last_successful_poll or "never"))
    print("Tracked in shop: {}".format(len(state.present_keys)))

    print("\n--- watchlist ---")
    entries = load_watchlist(config.watchlist_file)
    resolver = CatalogResolver(config, api)
    resolver.load()
    for entry in resolver.resolve_all(entries):
        marker = "OK " if entry.item_id else "?? "
        print("  {}{:<34} {:<34} [{}]".format(
            marker, entry.raw_name or "(id only)", entry.item_id or "-", entry.source))
    print()
    return 0


def cmd_test_notify(config):
    from src.matcher import ShopItem

    dispatcher = Dispatcher(config, DeadLetterQueue(config))
    print("Channels: {}\n".format(dispatcher.describe()))

    fake_entry = {
        "finalPrice": 1200, "regularPrice": 1500,
        "inDate": datetime.now(timezone.utc).isoformat(),
        "outDate": "2026-12-31T23:59:59Z",
        "offerId": "test-offer",
        "bundle": {}, "layout": {"name": "Featured"},
    }
    item = ShopItem(
        item_id="CID_028_Athena_Commando_F",
        name="Renegade Raider",
        item_type="Outfit",
        entry=fake_entry,
        container="brItems",
        rarity="Rare",
        image="https://fortnite-api.com/images/cosmetics/br/cid_028_athena_commando_f/icon.png",
    )

    alert = Alert([item], datetime.now(timezone.utc), kind="test")
    ok = dispatcher.send(alert)

    if ok:
        print("\nTest alert delivered. Check your notifications.")
        return 0
    print("\nTest alert FAILED on every channel. Check your .env credentials.", file=sys.stderr)
    return 1


def cmd_once(config):
    monitor = Monitor(config)
    monitor.load_watchlist(force=True)
    ok, new_items = monitor.poll_once()

    if not ok:
        print("\nPoll failed - see the log above.", file=sys.stderr)
        return 1
    if new_items:
        print("\n{} newly appeared item(s) reported.".format(len(new_items)))
    else:
        print("\nNo newly appeared tracked items.")
    return 0


def cmd_reset_state(config):
    if config.state_file.exists():
        config.state_file.unlink()
        print("State cleared. The next run will re-establish a baseline.")
    else:
        print("No state file to clear.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Monitor the Fortnite Item Shop and alert on watchlist items.")
    parser.add_argument("--once", action="store_true", help="run one check and exit")
    parser.add_argument("--test-notify", action="store_true", help="send a test notification")
    parser.add_argument("--status", action="store_true", help="show configuration and state")
    parser.add_argument("--reset-state", action="store_true", help="clear stored shop state")
    args = parser.parse_args(argv)

    config = Config()
    setup_logging(config)

    try:
        if args.status:
            return cmd_status(config)
        if args.test_notify:
            return cmd_test_notify(config)
        if args.reset_state:
            return cmd_reset_state(config)
        if args.once:
            return cmd_once(config)

        Monitor(config).run()
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except ApiError as exc:
        logging.getLogger("run").error("API error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
