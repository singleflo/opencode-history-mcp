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


def reap_orphaned_mcp_servers() -> int:
    """Kill orphaned opencode-history-mcp server processes.

    These accumulate when Hermes/OpenCode die ungracefully (kill -9, crash,
    force-quit) — the mcp_stdio_watchdog.py wrapper is supposed to handle this
    but doesn't always fire. We detect orphans by checking if the parent PID
    is 1 (init/launchd) — a live MCP server should always have a real parent.

    Returns count of processes killed.
    """
    import os
    import signal

    killed = 0
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,comm,args"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n")[1:]:
            parts = line.strip().split(None, 3)
            if len(parts) < 4:
                continue
            pid, ppid, comm, args = int(parts[0]), int(parts[1]), parts[2], parts[3]

            # Match our MCP server processes (both entry point and old server.py path)
            if "opencode-history-mcp" not in args and "opencode_history_mcp" not in args:
                continue

            # Skip ourselves (the indexer daemon)
            if "indexer_daemon" in args:
                continue

            # Skip the watchdog wrapper
            if "mcp_stdio_watchdog" in args:
                continue

            # If parent is init (PID 1 = launchd on macOS), it's an orphan
            if ppid == 1:
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed += 1
                    print(f"[history-indexer] Reaped orphan MCP server PID {pid}", file=sys.stderr)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception as e:
        print(f"[history-indexer] Orphan reap error (non-fatal): {e}", file=sys.stderr)

    return killed


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
            # Reap orphaned MCP server processes
            reaped = reap_orphaned_mcp_servers()
            if reaped:
                print(f"[history-indexer] Reaped {reaped} orphan(s).", file=sys.stderr)

            oc = build_index.connect_opencode_db()
            hist = build_index.connect_history_db()

            # Check if sync needed
            wm_row = hist.execute(
                "SELECT value FROM index_meta WHERE key='source_watermark'"
            ).fetchone()

            if not wm_row:
                print("[history-indexer] No watermark, skipping (run --build-index first)", file=sys.stderr)
            else:
                idx_watermark = int(wm_row["value"])
                build_index.check_source_schema(oc)
                db_max = build_index.source_watermark(oc)

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
