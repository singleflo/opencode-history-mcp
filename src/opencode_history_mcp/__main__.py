"""CLI entry point.

Handles two invocation modes:
    opencode-history-mcp                  → run the MCP server (stdio)
    opencode-history-mcp --build-index     → full index rebuild
    opencode-history-mcp --sync-index      → incremental index sync
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]

    if "--build-index" in args:
        from . import build_index
        sys.argv = [sys.argv[0], "--full"]
        build_index.main()
        return

    if "--sync-index" in args:
        from . import build_index
        sys.argv = [sys.argv[0], "--sync"]
        build_index.main()
        return

    from .server import run
    run()


if __name__ == "__main__":
    main()
