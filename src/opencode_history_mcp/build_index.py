"""build_index.py — Indexer for the OpenCode History MCP server.

Reads OpenCode's SQLite database (read-only) and builds a separate FTS5
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
SCHEMA_VERSION = 1

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

    Sub-agent sessions form a tree rooted at the top-level user session;
    this walks that tree so every session (root or sub-agent) can be
    grouped by its ultimate ancestor.
    """
    rows = oc_conn.execute("""
        WITH RECURSIVE root_finder(id, root) AS (
            SELECT id, id FROM session WHERE parent_id IS NULL
            UNION ALL
            SELECT s.id, rf.root FROM session s
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
    print("[build] Starting full index rebuild...")

    hist_conn.executescript("""
        DROP TABLE IF EXISTS sessions;
        DROP TABLE IF EXISTS search_idx;
        DROP TABLE IF EXISTS session_files;
        DROP TABLE IF EXISTS index_meta;
    """)
    create_schema(hist_conn)

    # 1. Resolve all root sessions
    print("[build] Resolving root sessions...")
    root_map = resolve_all_roots(oc_conn)
    print(f"[build]   {len(root_map)} sessions resolved to roots")

    # 2. Index sessions with pre-aggregated stats
    print("[build] Indexing sessions...")
    sessions = oc_conn.execute("""
        SELECT s.id, s.parent_id, s.title, s.directory, s.project_id,
               s.time_created, s.time_updated,
               s.cost, s.tokens_input, s.tokens_output, s.tokens_reasoning,
               s.tokens_cache_read, s.tokens_cache_write
        FROM session s
    """).fetchall()

    print("[build] Computing part counts per session...")
    part_counts: dict[str, dict] = {}
    for row in oc_conn.execute("""
        SELECT session_id,
               SUM(CASE WHEN json_extract(data, '$.type') = 'tool' THEN 1 ELSE 0 END) as tool_count,
               SUM(CASE WHEN json_extract(data, '$.type') = 'patch' THEN 1 ELSE 0 END) as patch_count,
               SUM(CASE WHEN json_extract(data, '$.type') = 'text' THEN 1 ELSE 0 END) as text_count,
               SUM(CASE WHEN json_extract(data, '$.type') = 'reasoning' THEN 1 ELSE 0 END) as reasoning_count,
               COUNT(*) as total
        FROM part GROUP BY session_id
    """).fetchall():
        part_counts[row["session_id"]] = dict(row)

    msg_counts: dict[str, int] = {}
    for row in oc_conn.execute("""
        SELECT session_id, COUNT(*) as cnt FROM message GROUP BY session_id
    """).fetchall():
        msg_counts[row["session_id"]] = row["cnt"]

    session_rows = []
    for s in sessions:
        sid = s["id"]
        root = root_map.get(sid, sid)
        pc = part_counts.get(sid, {})
        mc = msg_counts.get(sid, 0)

        session_rows.append((
            sid,
            root,
            1 if s["parent_id"] else 0,
            s["parent_id"],
            s["title"] or "",
            s["directory"] or "",
            s["project_id"],
            s["time_created"],
            s["time_updated"],
            mc,
            pc.get("tool_count", 0) or 0,
            pc.get("patch_count", 0) or 0,
            pc.get("text_count", 0) or 0,
            pc.get("reasoning_count", 0) or 0,
            s["cost"] or 0,
            s["tokens_input"] or 0,
            s["tokens_output"] or 0,
            s["tokens_reasoning"] or 0,
            s["tokens_cache_read"] or 0,
            s["tokens_cache_write"] or 0,
            None,  # first_user_prompt — filled later
            None,  # last_assistant_text — filled later
            SCHEMA_VERSION,
        ))

    hist_conn.executemany("""
        INSERT INTO sessions (
            session_id, root_session_id, is_sub_agent, parent_id, title, directory,
            project_id, time_created, time_updated,
            msg_count, tool_count, patch_count, text_count, reasoning_count,
            cost, tokens_input, tokens_output, tokens_reasoning,
            tokens_cache_read, tokens_cache_write,
            first_user_prompt, last_assistant_text, schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, session_rows)
    print(f"[build]   {len(session_rows)} sessions indexed")

    # 3. Index text parts into FTS5
    print("[build] Indexing text parts (FTS5)...")
    fts_rows = []
    first_prompts: dict[str, tuple[int, str]] = {}
    last_assistant: dict[str, tuple[int, str]] = {}

    batch_size = 5000
    total_text = 0

    cursor = oc_conn.execute("""
        SELECT p.id, p.session_id, p.time_created, p.data,
               m.data as msg_data
        FROM part p
        JOIN message m ON p.message_id = m.id
        WHERE json_extract(p.data, '$.type') = 'text'
        ORDER BY p.time_created
    """)

    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
        for row in batch:
            try:
                part_data = json.loads(row["data"])
                msg_data = json.loads(row["msg_data"])
            except (json.JSONDecodeError, TypeError):
                continue

            text = part_data.get("text", "")
            role = msg_data.get("role", "unknown")
            sid = row["session_id"]
            root = root_map.get(sid, sid)

            if not text or not text.strip():
                continue

            # Truncate very long text for FTS (keep first 5000 chars)
            fts_text = text[:5000] if len(text) > 5000 else text

            fts_rows.append((
                sid,
                root,
                "",  # directory is joined from `sessions` at query time
                role,
                fts_text,
                row["time_created"],
            ))
            total_text += 1

            if role == "user":
                tc = row["time_created"]
                if sid not in first_prompts or tc < first_prompts[sid][0]:
                    first_prompts[sid] = (tc, text[:500])

            if role == "assistant":
                tc = row["time_created"]
                if sid not in last_assistant or tc > last_assistant[sid][0]:
                    last_assistant[sid] = (tc, text[:500])

        if len(fts_rows) >= batch_size:
            hist_conn.executemany("""
                INSERT INTO search_idx (session_id, root_session_id, directory, role, text, time_created)
                VALUES (?,?,?,?,?,?)
            """, fts_rows)
            fts_rows = []

    if fts_rows:
        hist_conn.executemany("""
            INSERT INTO search_idx (session_id, root_session_id, directory, role, text, time_created)
            VALUES (?,?,?,?,?,?)
        """, fts_rows)

    print(f"[build]   {total_text} text parts indexed in FTS5")

    # 4. Update first_user_prompt and last_assistant_text
    print("[build] Updating first prompts and last assistant texts...")
    for sid, (_tc, text) in first_prompts.items():
        hist_conn.execute(
            "UPDATE sessions SET first_user_prompt = ? WHERE session_id = ?",
            (text, sid)
        )
    for sid, (_tc, text) in last_assistant.items():
        hist_conn.execute(
            "UPDATE sessions SET last_assistant_text = ? WHERE session_id = ?",
            (text, sid)
        )
    print(f"[build]   {len(first_prompts)} first prompts, {len(last_assistant)} last assistant texts")

    # 5. Index patch files into session_files
    print("[build] Indexing patch files...")
    file_rows = []
    for row in oc_conn.execute("""
        SELECT p.session_id, p.time_created, p.data
        FROM part p
        WHERE json_extract(p.data, '$.type') = 'patch'
          AND json_extract(p.data, '$.files') IS NOT NULL
    """).fetchall():
        try:
            data = json.loads(row["data"])
            files = data.get("files", [])
            if not isinstance(files, list):
                continue
            sid = row["session_id"]
            root = root_map.get(sid, sid)
            for f in files:
                if isinstance(f, str) and f.strip():
                    file_rows.append((sid, root, f.strip(), "modified", row["time_created"]))
        except (json.JSONDecodeError, TypeError):
            continue

    if file_rows:
        hist_conn.executemany("""
            INSERT OR IGNORE INTO session_files (session_id, root_session_id, file_path, how, time_created)
            VALUES (?,?,?,?,?)
        """, file_rows)
    print(f"[build]   {len(file_rows)} file references indexed")

    # 6. Update index_meta (watermarks used by incremental sync)
    max_part_updated = oc_conn.execute("SELECT MAX(time_updated) FROM part").fetchone()[0] or 0
    max_session_updated = oc_conn.execute("SELECT MAX(time_updated) FROM session").fetchone()[0] or 0

    hist_conn.executescript(f"""
        INSERT OR REPLACE INTO index_meta (key, value) VALUES
        ('schema_version', '{SCHEMA_VERSION}'),
        ('last_sync_part_time', '{max_part_updated}'),
        ('last_sync_session_time', '{max_session_updated}'),
        ('last_rebuild', '{int(time.time() * 1000)}'),
        ('opencode_db_path', '{OPENCODE_DB}');
    """)

    hist_conn.commit()
    elapsed = time.time() - t0
    db_size = os.path.getsize(HISTORY_DB) / (1024 * 1024)
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
    """True incremental sync: only re-index sessions with new/updated parts.

    Algorithm:
        1. Compare watermarks (time_updated on the `part` table).
        2. Find affected session_ids (parts updated since the watermark).
        3. For each affected session: delete its old index rows, re-index
           only that session.
        4. Detect deleted sessions (present in the index but gone from
           OpenCode's DB) and remove them.
        5. Update the watermark.
    """
    t0 = time.time()

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
        "SELECT value FROM index_meta WHERE key='last_sync_part_time'"
    ).fetchone()
    if not wm_row:
        print("[sync] No watermark found, running full build...")
        full_build(oc_conn, hist_conn)
        return

    watermark = int(wm_row["value"])
    db_max_part = oc_conn.execute("SELECT MAX(time_updated) FROM part").fetchone()[0] or 0
    db_max_sess = oc_conn.execute("SELECT MAX(time_updated) FROM session").fetchone()[0] or 0

    if db_max_part <= watermark:
        deleted = _detect_deleted_sessions(oc_conn, hist_conn)
        if deleted:
            print(f"[sync] Removed {deleted} deleted sessions from index...")
        else:
            print(f"[sync] Index is up to date (watermark={watermark})")
        _update_watermark(hist_conn, db_max_part, db_max_sess)
        return

    print(f"[sync] Syncing changes since watermark={watermark} (db_max={db_max_part})...")

    # 1. Find affected sessions (parts updated since watermark)
    affected_rows = oc_conn.execute("""
        SELECT DISTINCT session_id FROM part WHERE time_updated > ?
    """, (watermark,)).fetchall()
    affected_ids = [r["session_id"] for r in affected_rows]

    # Also check for new sessions not yet reflected in `part`
    sess_wm_row = hist_conn.execute(
        "SELECT value FROM index_meta WHERE key='last_sync_session_time'"
    ).fetchone()
    sess_watermark = int(sess_wm_row["value"]) if sess_wm_row else 0

    new_sessions = oc_conn.execute("""
        SELECT DISTINCT id FROM session WHERE time_updated > ?
    """, (sess_watermark,)).fetchall()
    new_sess_ids = [r["id"] for r in new_sessions if r["id"] not in affected_ids]

    all_affected = list(set(affected_ids + new_sess_ids))

    if not all_affected:
        _update_watermark(hist_conn, db_max_part, db_max_sess)
        print("[sync] No affected sessions found, watermark updated.")
        return

    print(f"[sync] {len(all_affected)} sessions to update ({len(affected_ids)} from parts, {len(new_sess_ids)} new)")

    # 2. Resolve roots for ALL sessions (needed for sub-agent mapping)
    root_map = resolve_all_roots(oc_conn)

    # 3. Delete old index data for affected sessions
    _remove_sessions_from_index(hist_conn, all_affected)

    # 4. Re-index each affected session
    indexed_sessions = 0
    indexed_parts = 0
    indexed_files = 0

    for sid in all_affected:
        s, p, f = _index_single_session(oc_conn, hist_conn, sid, root_map)
        indexed_sessions += s
        indexed_parts += p
        indexed_files += f

    # 5. Detect and remove deleted sessions
    deleted = _detect_deleted_sessions(oc_conn, hist_conn)
    if deleted:
        print(f"[sync] Removed {deleted} deleted sessions from index")

    # 6. Update watermark
    _update_watermark(hist_conn, db_max_part, db_max_sess)

    hist_conn.commit()
    elapsed = time.time() - t0
    print(f"[sync] Done in {elapsed:.1f}s. Sessions: {indexed_sessions}, Parts: {indexed_parts}, Files: {indexed_files}")


def _index_single_session(oc_conn, hist_conn, session_id, root_map):
    """Re-index a single session: insert into sessions, search_idx, session_files.

    Returns (sessions_counted, parts_counted, files_counted).
    """
    s = oc_conn.execute("""
        SELECT id, parent_id, title, directory, project_id,
               time_created, time_updated,
               cost, tokens_input, tokens_output, tokens_reasoning,
               tokens_cache_read, tokens_cache_write
        FROM session WHERE id = ?
    """, (session_id,)).fetchone()

    if not s:
        return 0, 0, 0

    root = root_map.get(session_id, session_id)

    pc = oc_conn.execute("""
        SELECT
            SUM(CASE WHEN json_extract(data, '$.type') = 'tool' THEN 1 ELSE 0 END) as tool_count,
            SUM(CASE WHEN json_extract(data, '$.type') = 'patch' THEN 1 ELSE 0 END) as patch_count,
            SUM(CASE WHEN json_extract(data, '$.type') = 'text' THEN 1 ELSE 0 END) as text_count,
            SUM(CASE WHEN json_extract(data, '$.type') = 'reasoning' THEN 1 ELSE 0 END) as reasoning_count,
            COUNT(*) as total
        FROM part WHERE session_id = ?
    """, (session_id,)).fetchone()

    msg_count = oc_conn.execute(
        "SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)
    ).fetchone()[0]

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
        s["title"] or "", s["directory"] or "", s["project_id"],
        s["time_created"], s["time_updated"],
        msg_count,
        pc["tool_count"] or 0, pc["patch_count"] or 0,
        pc["text_count"] or 0, pc["reasoning_count"] or 0,
        s["cost"] or 0, s["tokens_input"] or 0, s["tokens_output"] or 0,
        s["tokens_reasoning"] or 0, s["tokens_cache_read"] or 0, s["tokens_cache_write"] or 0,
        None, None, SCHEMA_VERSION,
    ))

    parts_counted = 0
    first_prompt = None
    last_assistant = None

    for row in oc_conn.execute("""
        SELECT p.time_created, p.data, m.data as msg_data
        FROM part p JOIN message m ON p.message_id = m.id
        WHERE p.session_id = ? AND json_extract(p.data, '$.type') = 'text'
        ORDER BY p.time_created
    """, (session_id,)).fetchall():
        try:
            pd = json.loads(row["data"])
            md = json.loads(row["msg_data"])
        except (json.JSONDecodeError, TypeError):
            continue

        text = pd.get("text", "")
        role = md.get("role", "unknown")
        if not text or not text.strip():
            continue

        fts_text = text[:5000] if len(text) > 5000 else text
        hist_conn.execute("""
            INSERT INTO search_idx (session_id, root_session_id, directory, role, text, time_created)
            VALUES (?,?,?,?,?,?)
        """, (session_id, root, "", role, fts_text, row["time_created"]))
        parts_counted += 1

        if role == "user":
            if first_prompt is None or row["time_created"] < first_prompt[0]:
                first_prompt = (row["time_created"], text[:500])
        if role == "assistant":
            if last_assistant is None or row["time_created"] > last_assistant[0]:
                last_assistant = (row["time_created"], text[:500])

    if first_prompt:
        hist_conn.execute(
            "UPDATE sessions SET first_user_prompt = ? WHERE session_id = ?",
            (first_prompt[1], session_id)
        )
    if last_assistant:
        hist_conn.execute(
            "UPDATE sessions SET last_assistant_text = ? WHERE session_id = ?",
            (last_assistant[1], session_id)
        )

    files_counted = 0
    for row in oc_conn.execute("""
        SELECT time_created, data FROM part
        WHERE session_id = ? AND json_extract(data, '$.type') = 'patch'
          AND json_extract(data, '$.files') IS NOT NULL
    """, (session_id,)).fetchall():
        try:
            data = json.loads(row["data"])
            files = data.get("files", [])
            if not isinstance(files, list):
                continue
            for f in files:
                if isinstance(f, str) and f.strip():
                    hist_conn.execute("""
                        INSERT OR IGNORE INTO session_files
                        (session_id, root_session_id, file_path, how, time_created)
                        VALUES (?,?,?,?,?)
                    """, (session_id, root, f.strip(), "modified", row["time_created"]))
                    files_counted += 1
        except (json.JSONDecodeError, TypeError):
            continue

    return 1, parts_counted, files_counted


def _remove_sessions_from_index(hist_conn, session_ids):
    """Remove sessions and their index data. If session_ids is None, removes ALL."""
    if session_ids is None:
        hist_conn.execute("DELETE FROM sessions")
        hist_conn.execute("DELETE FROM search_idx")
        hist_conn.execute("DELETE FROM session_files")
        return

    if not session_ids:
        return

    placeholders = ",".join("?" * len(session_ids))
    hist_conn.execute(f"DELETE FROM sessions WHERE session_id IN ({placeholders})", session_ids)
    hist_conn.execute(f"DELETE FROM search_idx WHERE session_id IN ({placeholders})", session_ids)
    hist_conn.execute(f"DELETE FROM session_files WHERE session_id IN ({placeholders})", session_ids)


def _detect_deleted_sessions(oc_conn, hist_conn):
    """Find sessions present in the index but no longer in OpenCode's DB.

    Returns the count removed.
    """
    indexed = hist_conn.execute("SELECT session_id FROM sessions").fetchall()
    indexed_ids = [r["session_id"] for r in indexed]

    if not indexed_ids:
        return 0

    deleted = []
    batch_size = 500
    for i in range(0, len(indexed_ids), batch_size):
        batch = indexed_ids[i:i + batch_size]
        placeholders = ",".join("?" * len(batch))
        existing = oc_conn.execute(
            f"SELECT id FROM session WHERE id IN ({placeholders})", batch
        ).fetchall()
        existing_ids = set(r["id"] for r in existing)
        deleted.extend([sid for sid in batch if sid not in existing_ids])

    if deleted:
        _remove_sessions_from_index(hist_conn, deleted)

    return len(deleted)


def _update_watermark(hist_conn, part_max, session_max):
    """Persist the sync watermark used to detect stale/fresh state."""
    hist_conn.execute("""
        INSERT OR REPLACE INTO index_meta (key, value) VALUES
        ('last_sync_part_time', ?),
        ('last_sync_session_time', ?),
        ('last_sync', ?)
    """, (str(part_max), str(session_max), str(int(time.time() * 1000))))


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

    if mode == "--sync":
        incremental_sync(oc_conn, hist_conn)
    else:
        full_build(oc_conn, hist_conn)

    oc_conn.close()
    hist_conn.close()


if __name__ == "__main__":
    main()
