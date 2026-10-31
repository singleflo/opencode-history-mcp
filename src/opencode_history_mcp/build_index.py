"""build_index.py — Indexer for the OpenCode History MCP server.

Reads OpenCode v2's SQLite database (read-only) and builds a separate FTS5
search index with denormalized metadata for fast queries. This keeps the
source database untouched and lets us do full-text search without paying
the cost of scanning JSON blobs on every query.

Usage:
    python -m opencode_history_mcp.build_index --full   # full rebuild
    python -m opencode_history_mcp.build_index --sync    # incremental
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

from .paths import opencode_db_path, history_index_path

OPENCODE_DB = str(opencode_db_path())
HISTORY_DB = str(history_index_path())
SCHEMA_VERSION = 2

# ═══════════════════════════════════════════════════════════════
# DB CONNECTION HELPERS
# ═══════════════════════════════════════════════════════════════


def connect_opencode_db(path: str = OPENCODE_DB) -> sqlite3.Connection:
    """Connect to the OpenCode DB read-only, respecting WAL (not immutable)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = 1")
    conn.row_factory = sqlite3.Row
    return conn


def connect_history_db(path: str = HISTORY_DB) -> sqlite3.Connection:
    """Connect to our history index DB (read-write)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# ═══════════════════════════════════════════════════════════════
# SOURCE (OpenCode v2) READERS
# ═══════════════════════════════════════════════════════════════

# Tools whose `path` input is a file they modify / only read.
MODIFY_TOOLS = {"write", "edit", "patch"}
READ_TOOLS = {"read"}


class UnsupportedSchemaError(RuntimeError):
    """The OpenCode DB is not an OpenCode v2 database."""


def check_source_schema(oc_conn: sqlite3.Connection) -> None:
    """Fail clearly unless the DB has the OpenCode v2 tables."""
    tables = {r[0] for r in oc_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "session_v2" in tables:
        return
    if "session" in tables:
        raise UnsupportedSchemaError(
            "This is an OpenCode v1 database (table `session`, no `session_v2`). "
            "opencode-history-mcp >= 0.2.0 only supports OpenCode v2; "
            "for v1 use `uvx opencode-history-mcp==0.1.0`."
        )
    raise UnsupportedSchemaError("No OpenCode session tables found (expected `session_v2`).")


def source_watermark(oc_conn: sqlite3.Connection) -> int:
    """Latest time_updated across sessions, messages and parts (epoch ms)."""
    return oc_conn.execute("""
        SELECT MAX(t) FROM (
            SELECT MAX(time_updated) AS t FROM session_v2
            UNION ALL SELECT MAX(time_updated) FROM session_message
            UNION ALL SELECT MAX(time_updated) FROM session_part
        )
    """).fetchone()[0] or 0


def load_messages(oc_conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Return a session's messages ordered by seq.

    Each item: {id, type, time_created, data, content}. `content` is the
    assistant item list, taken from `session_part` when it has rows for the
    message, otherwise from the inline `data.content`.
    """
    parts: dict[str, list[dict]] = {}
    for row in oc_conn.execute(
        "SELECT message_id, data FROM session_part WHERE session_id = ? ORDER BY message_id, ordinal",
        (session_id,),
    ):
        try:
            parts.setdefault(row["message_id"], []).append(json.loads(row["data"]))
        except (json.JSONDecodeError, TypeError):
            continue

    messages = []
    for row in oc_conn.execute(
        "SELECT id, type, time_created, data FROM session_message WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ):
        try:
            data = json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        content = parts.get(row["id"]) or data.get("content") or []
        messages.append({
            "id": row["id"],
            "type": row["type"],
            "time_created": row["time_created"],
            "data": data,
            "content": content,
        })
    return messages


def _resolve_path(path: str, directory: str) -> str:
    path = path.strip()
    if directory and not os.path.isabs(path):
        path = os.path.join(directory, path)
    return os.path.normpath(path)


def tool_files(item: dict, directory: str) -> list[tuple[str, str]]:
    """(file_path, how) pairs touched by one assistant tool item."""
    name = item.get("name")
    if name in MODIFY_TOOLS:
        how = "modified"
    elif name in READ_TOOLS:
        how = "mentioned"
    else:
        return []
    state = item.get("state") or {}
    paths = []
    path = (state.get("input") or {}).get("path")
    if isinstance(path, str) and path.strip():
        paths.append(path)
    for f in (state.get("metadata") or {}).get("files") or []:
        if isinstance(f, dict) and isinstance(f.get("file"), str) and f["file"].strip():
            paths.append(f["file"])
    return [(_resolve_path(p, directory), how) for p in paths]


# ═══════════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════════


def create_schema(conn: sqlite3.Connection) -> None:
    """Create the history index schema from scratch."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id         TEXT PRIMARY KEY,
            root_session_id    TEXT NOT NULL,
            is_sub_agent       INTEGER NOT NULL,
            parent_id          TEXT,
            title              TEXT NOT NULL,
            directory          TEXT NOT NULL,
            project_id         TEXT,
            time_created       INTEGER NOT NULL,
            time_updated       INTEGER NOT NULL,
            msg_count          INTEGER NOT NULL DEFAULT 0,
            tool_count         INTEGER NOT NULL DEFAULT 0,
            patch_count        INTEGER NOT NULL DEFAULT 0,
            text_count         INTEGER NOT NULL DEFAULT 0,
            reasoning_count    INTEGER NOT NULL DEFAULT 0,
            cost               REAL NOT NULL DEFAULT 0,
            tokens_input       INTEGER NOT NULL DEFAULT 0,
            tokens_output      INTEGER NOT NULL DEFAULT 0,
            tokens_reasoning   INTEGER NOT NULL DEFAULT 0,
            tokens_cache_read  INTEGER NOT NULL DEFAULT 0,
            tokens_cache_write INTEGER NOT NULL DEFAULT 0,
            first_user_prompt  TEXT,
            last_assistant_text TEXT,
            schema_version     INTEGER NOT NULL DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_directory ON sessions(directory);
        CREATE INDEX IF NOT EXISTS idx_sessions_root ON sessions(root_session_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_time ON sessions(time_created DESC);

        CREATE VIRTUAL TABLE IF NOT EXISTS search_idx USING fts5(
            session_id UNINDEXED,
            root_session_id UNINDEXED,
            directory UNINDEXED,
            role,
            text,
            time_created UNINDEXED,
            tokenize = 'porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS session_files (
            session_id     TEXT NOT NULL,
            root_session_id TEXT NOT NULL,
            file_path      TEXT NOT NULL,
            how            TEXT NOT NULL,
            time_created   INTEGER NOT NULL,
            PRIMARY KEY (session_id, file_path, how)
        );

        CREATE INDEX IF NOT EXISTS idx_session_files_path ON session_files(file_path);
        CREATE INDEX IF NOT EXISTS idx_session_files_root ON session_files(root_session_id);

        CREATE TABLE IF NOT EXISTS index_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)


# ═══════════════════════════════════════════════════════════════
# ROOT RESOLUTION
# ═══════════════════════════════════════════════════════════════


def resolve_all_roots(oc_conn: sqlite3.Connection) -> dict[str, str]:
    """Resolve root_session_id for ALL sessions using a recursive CTE.

    Sub-agent sessions form a tree (via parent_id) rooted at the top-level
    user session. Forks have no parent_id, so they are independent roots.
    """
    rows = oc_conn.execute("""
        WITH RECURSIVE root_finder(id, root) AS (
            SELECT id, id FROM session_v2 WHERE parent_id IS NULL
            UNION ALL
            SELECT s.id, rf.root FROM session_v2 s
            JOIN root_finder rf ON s.parent_id = rf.id
        )
        SELECT id, root FROM root_finder
    """).fetchall()
    return {r["id"]: r["root"] for r in rows}


# ═══════════════════════════════════════════════════════════════
# FULL BUILD
# ═══════════════════════════════════════════════════════════════


def full_build(oc_conn: sqlite3.Connection, hist_conn: sqlite3.Connection) -> None:
    """Full rebuild of the index from scratch."""
    t0 = time.time()
    check_source_schema(oc_conn)
    print("[build] Starting full index rebuild...")

    hist_conn.executescript("""
        DROP TABLE IF EXISTS sessions;
        DROP TABLE IF EXISTS search_idx;
        DROP TABLE IF EXISTS session_files;
        DROP TABLE IF EXISTS index_meta;
    """)
    create_schema(hist_conn)

    watermark = source_watermark(oc_conn)
    root_map = resolve_all_roots(oc_conn)
    session_ids = [r["id"] for r in oc_conn.execute("SELECT id FROM session_v2")]
    for sid in session_ids:
        _index_single_session(oc_conn, hist_conn, sid, root_map)

    hist_conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?), (?, ?), (?, ?)",
        ("schema_version", str(SCHEMA_VERSION), "last_rebuild", str(int(time.time() * 1000)),
         "opencode_db_path", OPENCODE_DB),
    )
    _update_watermark(hist_conn, watermark)

    hist_conn.commit()
    elapsed = time.time() - t0
    db_size = os.path.getsize(HISTORY_DB) / (1024 * 1024) if os.path.exists(HISTORY_DB) else 0
    print(f"[build] Done in {elapsed:.1f}s. DB size: {db_size:.1f} MB")

    total_sessions = hist_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    root_sessions = hist_conn.execute("SELECT COUNT(*) FROM sessions WHERE is_sub_agent = 0").fetchone()[0]
    fts_count = hist_conn.execute("SELECT COUNT(*) FROM search_idx").fetchone()[0]
    files_count = hist_conn.execute("SELECT COUNT(*) FROM session_files").fetchone()[0]
    print(f"[build] Summary: {total_sessions} sessions ({root_sessions} root), {fts_count} FTS rows, {files_count} file refs")


# ═══════════════════════════════════════════════════════════════
# INCREMENTAL SYNC
# ═══════════════════════════════════════════════════════════════


def incremental_sync(oc_conn: sqlite3.Connection, hist_conn: sqlite3.Connection) -> None:
    """Re-index only sessions changed since the watermark.

    Algorithm:
        1. Compare the index watermark with MAX(time_updated) over
           session_v2, session_message and session_part.
        2. Re-index every session with a row updated after the watermark.
        3. Remove sessions deleted from OpenCode's DB.
        4. Update the watermark.
    """
    t0 = time.time()
    check_source_schema(oc_conn)

    tables = hist_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='index_meta'"
    ).fetchone()
    if not tables:
        print("[sync] No index found, running full build...")
        full_build(oc_conn, hist_conn)
        return

    sv_row = hist_conn.execute(
        "SELECT value FROM index_meta WHERE key='schema_version'"
    ).fetchone()
    if not sv_row or int(sv_row["value"]) != SCHEMA_VERSION:
        print("[sync] Schema mismatch, running full build...")
        full_build(oc_conn, hist_conn)
        return

    wm_row = hist_conn.execute(
        "SELECT value FROM index_meta WHERE key='source_watermark'"
    ).fetchone()
    if not wm_row:
        print("[sync] No watermark found, running full build...")
        full_build(oc_conn, hist_conn)
        return

    watermark = int(wm_row["value"])
    db_max = source_watermark(oc_conn)

    if db_max <= watermark:
        deleted = _detect_deleted_sessions(oc_conn, hist_conn)
        if deleted:
            print(f"[sync] Removed {deleted} deleted sessions from index")
            hist_conn.commit()
        else:
            print(f"[sync] Index is up to date (watermark={watermark})")
        return

    print(f"[sync] Syncing changes since watermark={watermark} (db_max={db_max})...")

    affected = [r[0] for r in oc_conn.execute("""
        SELECT id FROM session_v2 WHERE time_updated > ?1
        UNION SELECT session_id FROM session_message WHERE time_updated > ?1
        UNION SELECT session_id FROM session_part WHERE time_updated > ?1
    """, (watermark,))]

    root_map = resolve_all_roots(oc_conn)
    _remove_sessions_from_index(hist_conn, affected)

    indexed_sessions = indexed_texts = indexed_files = 0
    for sid in affected:
        s, p, f = _index_single_session(oc_conn, hist_conn, sid, root_map)
        indexed_sessions += s
        indexed_texts += p
        indexed_files += f

    deleted = _detect_deleted_sessions(oc_conn, hist_conn)
    if deleted:
        print(f"[sync] Removed {deleted} deleted sessions from index")

    _update_watermark(hist_conn, db_max)

    hist_conn.commit()
    elapsed = time.time() - t0
    print(f"[sync] Done in {elapsed:.1f}s. Sessions: {indexed_sessions}, Texts: {indexed_texts}, Files: {indexed_files}")


def _index_single_session(oc_conn, hist_conn, session_id, root_map):
    """Index one session into sessions, search_idx and session_files.

    Returns (sessions_counted, texts_counted, files_counted).
    """
    s = oc_conn.execute("""
        SELECT id, parent_id, title, directory, project_id,
               time_created, time_updated,
               cost, tokens_input, tokens_output, tokens_reasoning,
               tokens_cache_read, tokens_cache_write
        FROM session_v2 WHERE id = ?
    """, (session_id,)).fetchone()

    if not s:
        return 0, 0, 0

    root = root_map.get(session_id, session_id)
    directory = s["directory"] or ""

    msg_count = tool_count = patch_count = text_count = reasoning_count = 0
    texts: list[tuple[str, str, int]] = []  # (role, text, time_created)
    files: dict[tuple[str, str], int] = {}  # (path, how) -> time_created

    for m in load_messages(oc_conn, session_id):
        tc = m["time_created"]
        if m["type"] == "user":
            msg_count += 1
            text = m["data"].get("text") or ""
            if text.strip():
                texts.append(("user", text, tc))
                text_count += 1
        elif m["type"] == "assistant":
            msg_count += 1
            for item in m["content"]:
                kind = item.get("type")
                if kind == "text":
                    text = item.get("text") or ""
                    if text.strip():
                        texts.append(("assistant", text, tc))
                        text_count += 1
                elif kind == "reasoning":
                    reasoning_count += 1
                elif kind == "tool":
                    tool_count += 1
                    if item.get("name") in MODIFY_TOOLS:
                        patch_count += 1
                    for key in tool_files(item, directory):
                        files.setdefault(key, tc)
            for f in (m["data"].get("snapshot") or {}).get("files") or []:
                if isinstance(f, str) and f.strip():
                    files.setdefault((_resolve_path(f, directory), "modified"), tc)

    first_prompt = next((t[:500] for role, t, _ in texts if role == "user"), None)
    last_assistant = next((t[:500] for role, t, _ in reversed(texts) if role == "assistant"), None)

    hist_conn.execute("""
        INSERT OR REPLACE INTO sessions (
            session_id, root_session_id, is_sub_agent, parent_id, title, directory,
            project_id, time_created, time_updated,
            msg_count, tool_count, patch_count, text_count, reasoning_count,
            cost, tokens_input, tokens_output, tokens_reasoning,
            tokens_cache_read, tokens_cache_write,
            first_user_prompt, last_assistant_text, schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        session_id, root, 1 if s["parent_id"] else 0, s["parent_id"],
        s["title"] or "", directory, s["project_id"],
        s["time_created"], s["time_updated"],
        msg_count, tool_count, patch_count, text_count, reasoning_count,
        s["cost"] or 0, s["tokens_input"] or 0, s["tokens_output"] or 0,
        s["tokens_reasoning"] or 0, s["tokens_cache_read"] or 0, s["tokens_cache_write"] or 0,
        first_prompt, last_assistant, SCHEMA_VERSION,
    ))

    # directory is joined from `sessions` at query time; FTS text capped at 5000 chars
    hist_conn.executemany("""
        INSERT INTO search_idx (session_id, root_session_id, directory, role, text, time_created)
        VALUES (?,?,?,?,?,?)
    """, [(session_id, root, "", role, text[:5000], tc) for role, text, tc in texts])

    hist_conn.executemany("""
        INSERT OR IGNORE INTO session_files (session_id, root_session_id, file_path, how, time_created)
        VALUES (?,?,?,?,?)
    """, [(session_id, root, path, how, tc) for (path, how), tc in files.items()])

    return 1, len(texts), len(files)


def _remove_sessions_from_index(hist_conn, session_ids):
    """Remove sessions and their index data."""
    if not session_ids:
        return
    placeholders = ",".join("?" * len(session_ids))
    hist_conn.execute(f"DELETE FROM sessions WHERE session_id IN ({placeholders})", session_ids)
    hist_conn.execute(f"DELETE FROM search_idx WHERE session_id IN ({placeholders})", session_ids)
    hist_conn.execute(f"DELETE FROM session_files WHERE session_id IN ({placeholders})", session_ids)


def _detect_deleted_sessions(oc_conn, hist_conn):
    """Remove sessions present in the index but gone from OpenCode's DB.

    Returns the count removed.
    """
    existing = {r[0] for r in oc_conn.execute("SELECT id FROM session_v2")}
    deleted = [r[0] for r in hist_conn.execute("SELECT session_id FROM sessions") if r[0] not in existing]
    _remove_sessions_from_index(hist_conn, deleted)
    return len(deleted)


def _update_watermark(hist_conn, watermark):
    """Persist the sync watermark used to detect stale/fresh state."""
    hist_conn.execute("""
        INSERT OR REPLACE INTO index_meta (key, value) VALUES
        ('source_watermark', ?),
        ('last_sync', ?)
    """, (str(watermark), str(int(time.time() * 1000))))


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--full"

    if not os.path.exists(OPENCODE_DB):
        print(f"ERROR: OpenCode DB not found at {OPENCODE_DB}", file=sys.stderr)
        print("Set OPENCODE_DATA_DIR if OpenCode stores its data elsewhere on your system.", file=sys.stderr)
        sys.exit(1)

    print(f"[main] OpenCode DB: {OPENCODE_DB}")
    print(f"[main] History DB: {HISTORY_DB}")

    oc_conn = connect_opencode_db()
    hist_conn = connect_history_db()
    try:
        if mode == "--sync":
            incremental_sync(oc_conn, hist_conn)
        else:
            full_build(oc_conn, hist_conn)
    except UnsupportedSchemaError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        oc_conn.close()
        hist_conn.close()


if __name__ == "__main__":
    main()
