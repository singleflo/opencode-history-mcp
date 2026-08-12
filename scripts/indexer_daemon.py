#!/usr/bin/env python3
"""Background indexer daemon for opencode-history-mcp.

Runs incremental sync every N seconds (default 300 = 5 min).
Designed to be launched by launchd (macOS) or systemd (Linux).

Usage:
    python3 indexer_daemon.py [--interval 300]

Exit codes:
    0 = clean shutdown
    1 = fatal error (index missing, can't connect to DB)
"""

from __future__ import annotations

import sys
import time

# Interval between sync checks (seconds)
DEFAULT_INTERVAL = 300


def main() -> None:
    interval = DEFAULT_INTERVAL
    if "--interval" in sys.argv:
        idx = sys.argv.index("--interval")
        if idx + 1 < len(sys.argv):
            interval = int(sys.argv[idx + 1])

    # Add parent src to path so we can import the package
    import os
    src_path = os.path.join(os.path.dirname(__file__), "..", "src")
    sys.path.insert(0, src_path)

    from opencode_history_mcp import build_index

    print(f"[history-indexer] Starting daemon (interval={interval}s)", file=sys.stderr)

    while True:
        try:
            oc = build_index.connect_opencode_db()
            hist = build_index.connect_history_db()

            # Check if sync needed
            wm_row = hist.execute(
                "SELECT value FROM index_meta WHERE key='last_sync_part_time'"
            ).fetchone()

            if not wm_row:
                print("[history-indexer] No watermark, skipping (run --build-index first)", file=sys.stderr)
            else:
                idx_watermark = int(wm_row["value"])
                db_max = oc.execute("SELECT MAX(time_updated) FROM part").fetchone()[0] or 0

                if db_max > idx_watermark:
                    delta = db_max - idx_watermark
                    print(f"[history-indexer] Index stale (delta={delta}ms), syncing...", file=sys.stderr)
                    build_index.incremental_sync(oc, hist)
                    print("[history-indexer] Sync complete.", file=sys.stderr)
                else:
                    print("[history-indexer] Index up to date.", file=sys.stderr)

            hist.close()
            oc.close()

        except Exception as e:
            print(f"[history-indexer] Sync error (non-fatal): {e}", file=sys.stderr)

        time.sleep(interval)


if __name__ == "__main__":
    main()
