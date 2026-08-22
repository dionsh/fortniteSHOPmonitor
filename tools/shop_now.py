#!/usr/bin/env python3
"""Show what is in the Item Shop right now.

    python tools/shop_now.py                list everything, grouped by type
    python tools/shop_now.py outfit         only that type
    python tools/shop_now.py --search rene  find items by name

Handy for grabbing the exact spelling / item ID to put in watchlist.json.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api import ApiError, FortniteAPI
from src.config import Config
from src.matcher import flatten_shop, normalize_name
from src.watchlist import CatalogResolver
from src.console import enable_utf8_stdout


def main(argv):
    enable_utf8_stdout()
    config = Config()
    api = FortniteAPI(config)

    try:
        shop = api.fetch_shop_freshest(config.polling["race_languages"])
    except ApiError as exc:
        print("Could not fetch the shop: {}".format(exc), file=sys.stderr)
        return 1

    items = flatten_shop(shop)

    # The freshest variant may not be English; restore English display names.
    if shop.language != "en":
        resolver = CatalogResolver(config, api)
        resolver.load()
        resolver.localize_all(items)

    search = None
    type_filter = None
    if "--search" in argv:
        idx = argv.index("--search")
        if idx + 1 < len(argv):
            search = normalize_name(argv[idx + 1])
    elif argv:
        type_filter = normalize_name(argv[0])

    print("\nItem Shop for {}  (hash {}, cache age {}s, {} entries)\n".format(
        shop.date[:10], shop.hash[:12], shop.age_seconds, len(shop.entries)))

    grouped = defaultdict(list)
    for item in items.values():
        if search and search not in item.norm_name:
            continue
        if type_filter and type_filter not in normalize_name(item.type):
            continue
        grouped[item.type].append(item)

    if not grouped:
        print("Nothing matched.\n")
        return 0

    total = 0
    for item_type in sorted(grouped):
        bucket = sorted(grouped[item_type], key=lambda i: i.name.casefold())
        print("--- {} ({}) ---".format(item_type, len(bucket)))
        for item in bucket:
            total += 1
            extra = "  [{}]".format(item.extra) if item.extra else ""
            print("  {:<44} {:>22}   {}{}".format(
                item.name[:44], item.price_text(), item.id or "-", extra))
        print()

    print("{} item(s) shown.\n".format(total))
    print("Copy a name (or the ID) into watchlist.json to track it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
