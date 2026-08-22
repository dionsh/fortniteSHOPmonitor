"""Windows console encoding helper.

Item names contain plenty of non-ASCII (umlauts, accents, CJK, emoji). The
default Windows console codepage is cp1252, which cannot encode them and
raises UnicodeEncodeError mid-print - which would take down a notification.
Switching stdout/stderr to UTF-8 with a replacement error handler makes
output lossy at worst instead of fatal.
"""
from __future__ import annotations

import sys


def enable_utf8_stdout():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Redirected to something that cannot be reconfigured; harmless.
            pass
