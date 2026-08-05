"""paths.py — Cross-platform resolution of OpenCode's data directory.

Mirrors the exact resolution logic OpenCode itself uses (see
packages/core/src/global.ts in the opencode repo, which relies on the
`xdg-basedir` npm package). We replicate it here so this tool finds the
same `opencode.db` that OpenCode reads and writes, on every platform.

Resolution order:
    1. OPENCODE_DATA_DIR   — explicit override, wins unconditionally.
    2. XDG_DATA_HOME        — respected on Linux AND macOS (OpenCode does
                               NOT use `~/Library/Application Support` on
                               macOS — this is a common point of confusion).
    3. Platform fallback:
         - Windows: %LOCALAPPDATA%\\opencode
         - Linux/macOS: ~/.local/share/opencode

WSL note: WSL runs a real Linux kernel, so `sys.platform` reports
"linux" and path #3 above applies automatically — no special-casing
needed. The only WSL edge case is when OpenCode itself is installed on
the Windows side (not inside WSL): its data then lives under the
Windows-side AppData, which WSL mounts at
`/mnt/c/Users/<user>/AppData/Local/opencode`. In that scenario, set
OPENCODE_DATA_DIR explicitly to that mounted path — see the README for
a copy-paste example.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def opencode_data_dir() -> Path:
    """Return the directory containing opencode.db, using OpenCode's own
    resolution order (XDG-first, with platform-specific fallbacks)."""

    # 1. Explicit override always wins.
    if override := os.environ.get("OPENCODE_DATA_DIR"):
        return Path(override).expanduser()

    # 2. XDG_DATA_HOME — OpenCode honors this on Linux and macOS alike.
    if xdg := os.environ.get("XDG_DATA_HOME"):
        return Path(xdg).expanduser() / "opencode"

    # 3. Platform-specific fallback.
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "opencode"

    # Linux, macOS, and WSL (which reports "linux") all land here.
    # Note: macOS does NOT get ~/Library/Application Support — OpenCode's
    # own xdg-basedir resolution has no macOS-specific branch.
    return Path.home() / ".local" / "share" / "opencode"


def opencode_db_path() -> Path:
    """Path to OpenCode's own SQLite database (read-only for us)."""
    return opencode_data_dir() / "opencode.db"


def history_index_path() -> Path:
    """Path to our derived FTS5 search index (read-write, owned by us)."""
    return opencode_data_dir() / "opencode-history.db"
