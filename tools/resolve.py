#!/usr/bin/env python3
"""Find the exact item ID for a cosmetic, to pin in watchlist.json.

    python tools/resolve.py renegade          search the catalog
    python tools/resolve.py --check           check every watchlist entry

Pinning IDs is more reliable than names: an ID survives Epic renaming the
display name, and avoids ambiguity when several items share a name.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api import FortniteAPI
from src.config import Config
from src.console import enable_utf8_stdout
from src.matcher import normalize_name
from src.watchlist import CatalogResolver, load_watchlist


def check_watchlist(config, resolver):
    entries = load_watchlist(config.watchlist_file)
    if not entries:
        print("Watchlist is empty.")
        return 0

    print("\nWatchlist resolution:\n")
    unresolved = 0
    for entry in resolver.resolve_all(entries):
        if entry.item_id:
            mark = "OK"
        else:
            mark = "!!"
            unresolved += 1
        print("  [{}] {:<36} -> {:<38} ({})".format(
            mark, entry.raw_name or "(id only)", entry.item_id or "NOT RESOLVED", entry.source))

    print()
    if unresolved:
        print("{} entry/entries did not resolve. They will still be matched by "
              "name, but check the spelling.".format(unresolved))
    else:
        print("All entries resolved to item IDs.")
    return 0


def search(resolver, query, limit=25):
    needle = normalize_name(query)
    if not needle:
        print("Give me something to search for.")
        return 1

    hits = []
    for norm, record in resolver.by_name.items():
        if needle in norm:
            hits.append(record)

    if not hits:
        print("No cosmetic matched '{}'.".format(query))
        return 1

    hits.sort(key=lambda r: (len(r.get("name") or ""), r.get("name") or ""))
    print("\n{} match(es) for '{}':\n".format(len(hits), query))

    for record in hits[:limit]:
        type_field = record.get("type") or {}
        rarity_field = record.get("rarity") or {}
        print("  {:<40} {:<44}".format(
            (record.get("name") or "?")[:40], record.get("id") or "?"))
        print("      type={}  rarity={}  added={}".format(
            type_field.get("displayValue", "?"),
            rarity_field.get("displayValue", "?"),
            (record.get("added") or "?")[:10]))

    if len(hits) > limit:
        print("\n  ...and {} more. Narrow the search.".format(len(hits) - limit))

    print('\nPin one in watchlist.json like:')
    example = hits[0]
    print('  {{ "id": "{}", "name": "{}" }}'.format(
        example.get("id"), example.get("name")))
    return 0


def main(argv):
    enable_utf8_stdout()
    config = Config()
    resolver = CatalogResolver(config, FortniteAPI(config))
    resolver.load()

    if not resolver.loaded:
        print("Could not load the cosmetics catalog (offline?).", file=sys.stderr)
        return 1

    print("Catalog: {} cosmetics.".format(len(resolver.by_name)))

    if not argv or argv[0] == "--check":
        return check_watchlist(config, resolver)

    return search(resolver, " ".join(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
