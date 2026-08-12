"""server.py — OpenCode History MCP Server

MCP server (FastMCP, stdio transport) for searching past OpenCode
conversations. Exposes 7 read-only tools for session discovery,
drill-down, and stats — all backed by a local FTS5 index built from
OpenCode's own SQLite database.

Usage:
    # Direct (invoked by MCP clients via stdio)
    opencode-history-mcp

    # Development inspection
    fastmcp dev -m opencode_history_mcp.server
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .paths import opencode_db_path, history_index_path

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

OPENCODE_DB = str(opencode_db_path())
HISTORY_DB = str(history_index_path())
SCHEMA_VERSION = 1

# Hard caps
MAX_SNIPPET_CHARS = 200
MAX_PART_CHARS = 1000
MAX_RESULTS = 20
MAX_MESSAGES = 50
DEFAULT_RESULTS = 10
DEFAULT_MESSAGES = 10

SYNC_TTL_SECONDS = 300  # 5 minutes between sync checks
W_RELEVANCE = 0.5
W_RECENCY = 0.3
W_ACTIVITY = 0.2
RECENCY_HALF_LIFE_DAYS = 180

# Part types to SKIP in get_session_messages
SKIP_PART_TYPES = {
    "step-start", "step-finish", "agent", "compaction", "file"
}

# ═══════════════════════════════════════════════════════════════
# DB CONNECTIONS
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# LAZY SYNC (background, non-blocking)
# ═══════════════════════════════════════════════════════════════

_sync_done = False


def _lazy_sync_once() -> None:
    """Run sync check on first tool call, not on startup.

    Spawns a daemon thread so the server starts instantly and OpenCode/Hermes
    shutdown is never blocked by a sync in progress.
    """
    global _sync_done
    if _sync_done:
        return
    _sync_done = True
    try:
        import threading
        threading.Thread(target=_do_sync, daemon=True).start()
    except Exception:
        pass  # Non-fatal — stale index is still usable


def _do_sync() -> None:
    """Background sync wrapper — never raises."""
    try:
        check_and_sync()
    except Exception as e:
        print(f"[history-mcp] Background sync failed (non-fatal): {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════
# DB CONNECTIONS
# ═══════════════════════════════════════════════════════════════

def get_history_conn() -> sqlite3.Connection:
    """Get a connection to the history index DB."""
    _lazy_sync_once()  # Trigger background sync on first real use
    conn = sqlite3.connect(HISTORY_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_opencode_conn() -> sqlite3.Connection:
    """Get a read-only connection to OpenCode's DB (respects WAL)."""
    conn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = 1")
    return conn


# ═══════════════════════════════════════════════════════════════
# RANKING HELPERS
# ═══════════════════════════════════════════════════════════════


def compute_recency(time_updated_ms: int) -> float:
    """Recency score: exponential decay, half-life 180 days. Timestamps are epoch ms."""
    now_ms = int(time.time() * 1000)
    days_ago = max(0, (now_ms - time_updated_ms) / 86400000.0)
    return 0.5 ** (days_ago / RECENCY_HALF_LIFE_DAYS)


def compute_activity(tool_count: int, patch_count: int) -> float:
    """Activity score based on real actions (tools + patches weighted)."""
    action_count = tool_count + (patch_count * 3)
    return min(1.0, math.log10(action_count + 1) / 3.0)


def normalize_bm25(scores: list[float]) -> list[float]:
    """Normalize BM25 scores (negative, unbounded) to a 0-1 range relative to max."""
    if not scores:
        return []
    abs_scores = [abs(s) for s in scores]
    max_score = max(abs_scores) if abs_scores else 1.0
    if max_score == 0:
        return [0.0] * len(scores)
    return [s / max_score for s in abs_scores]


# ═══════════════════════════════════════════════════════════════
# DEDUP HELPER
# ═══════════════════════════════════════════════════════════════


def dedup_by_root(results: list[dict], limit: int) -> list[dict]:
    """Deduplicate results by root_session_id, keeping the best score per root."""
    seen = set()
    deduped = []
    for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True):
        root = r.get("root_session_id", r.get("session_id", ""))
        if root not in seen:
            deduped.append(r)
            seen.add(root)
        if len(deduped) >= limit:
            break
    return deduped


# ═══════════════════════════════════════════════════════════════
# SYNC ON STARTUP
# ═══════════════════════════════════════════════════════════════

_last_sync_check = 0.0


def check_and_sync() -> bool:
    """Check if the index needs an update on startup. Runs an incremental
    sync if it's stale.

    Uses a TTL cache (5 min) to avoid checking on every tool call. The
    sync itself is incremental — only new/updated sessions are re-indexed.
    """
    global _last_sync_check

    now = time.time()

    if _last_sync_check and (now - _last_sync_check) < SYNC_TTL_SECONDS:
        return True

    _last_sync_check = now

    try:
        if not os.path.exists(HISTORY_DB):
            print(
                "[history-mcp] WARNING: History index not found. "
                "Run 'opencode-history-mcp --build-index' first.",
                file=sys.stderr,
            )
            return False

        hist = get_history_conn()

        row = hist.execute("SELECT value FROM index_meta WHERE key='schema_version'").fetchone()
        if not row or int(row["value"]) != SCHEMA_VERSION:
            print("[history-mcp] WARNING: Schema version mismatch. Run --build-index.", file=sys.stderr)
            hist.close()
            return False

        wm = hist.execute("SELECT value FROM index_meta WHERE key='last_sync_part_time'").fetchone()
        if not wm:
            print("[history-mcp] WARNING: No watermark in index. Run --build-index.", file=sys.stderr)
            hist.close()
            return False

        idx_watermark = int(wm["value"])

        try:
            oc = get_opencode_conn()
            db_max = oc.execute("SELECT MAX(time_updated) FROM part").fetchone()[0] or 0
            oc.close()
        except Exception:
            # Can't access OpenCode's DB — not fatal, serve the stale index.
            hist.close()
            return True

        if db_max > idx_watermark:
            print(f"[history-mcp] Index stale (delta={db_max - idx_watermark}ms), syncing...", file=sys.stderr)
            try:
                from . import build_index
                oc2 = build_index.connect_opencode_db()
                hist2 = build_index.connect_history_db()
                build_index.incremental_sync(oc2, hist2)
                hist2.close()
                oc2.close()
                print("[history-mcp] Sync complete.", file=sys.stderr)
            except Exception as e:
                print(f"[history-mcp] Sync failed (non-fatal, serving stale): {e}", file=sys.stderr)

        hist.close()
        return True
    except Exception as e:
        print(f"[history-mcp] WARNING: Index check failed: {e}", file=sys.stderr)
        return False


# ═══════════════════════════════════════════════════════════════
# MCP SERVER
# ═══════════════════════════════════════════════════════════════

mcp = FastMCP(name="OpenCode History")

RO = ToolAnnotations(readOnlyHint=True, idempotentHint=True)


@mcp.tool(annotations=RO)
def search_history(
    query: str,
    directory: Optional[str] = None,
    limit: int = DEFAULT_RESULTS,
) -> dict:
    """Search past OpenCode conversations by keyword (full-text on user prompts and assistant responses).

    Args:
        query: Search terms (supports FTS5 syntax: keywords, phrases, prefixes)
        directory: Optional project directory to scope results. If omitted, searches globally.
        limit: Max results (default 10, max 20)

    Returns:
        List of matching sessions with title, directory, snippet, and relevance score.
        Results are deduplicated by root session (sub-agents grouped).
    """
    limit = min(limit, MAX_RESULTS)
    conn = get_history_conn()

    try:
        params = [query]
        dir_filter = ""
        if directory:
            dir_filter = "AND s.directory = ?"
            params.append(directory)

        rows = conn.execute(f"""
            SELECT si.session_id, si.root_session_id, s.title, s.directory,
                   snippet(search_idx, 4, '«', '»', '…', 10) as snippet,
                   bm25(search_idx) as bm25_score,
                   s.time_updated, s.msg_count, s.tool_count, s.patch_count
            FROM search_idx si
            JOIN sessions s ON si.session_id = s.session_id
            WHERE si.search_idx MATCH ?
            {dir_filter}
            ORDER BY bm25(search_idx) ASC
            LIMIT 100
        """, params).fetchall()

        if not rows:
            result = {
                "results": [],
                "total_found": 0,
                "returned": 0,
                "scope": "directory" if directory else "global",
                "query": query,
            }
            if directory:
                result["hint"] = f"No results in '{directory}'. Try without directory for a global search."
            return result

        raw_scores = [r["bm25_score"] for r in rows]
        norm_scores = normalize_bm25(raw_scores)

        results = []
        for i, r in enumerate(rows):
            recency = compute_recency(r["time_updated"])
            activity = compute_activity(r["tool_count"], r["patch_count"])
            score = (norm_scores[i] * W_RELEVANCE) + (recency * W_RECENCY) + (activity * W_ACTIVITY)

            results.append({
                "session_id": r["session_id"],
                "root_session_id": r["root_session_id"],
                "title": r["title"][:100],
                "directory": r["directory"],
                "snippet": (r["snippet"] or "")[:MAX_SNIPPET_CHARS],
                "score": round(score, 3),
            })

        results = dedup_by_root(results, limit)

        return {
            "results": results[:limit],
            "total_found": len(rows),
            "returned": len(results[:limit]),
            "scope": "directory" if directory else "global",
            "query": query,
        }
    finally:
        conn.close()


@mcp.tool(annotations=RO)
def find_sessions_by_file(
    file_path: str,
    directory: Optional[str] = None,
    limit: int = DEFAULT_RESULTS,
) -> dict:
    """Find sessions that modified or mentioned a specific file.

    Searches patch records (files actually changed) and file mentions in text.
    Uses an indexed file_paths table for fast lookup (<50ms).

    Args:
        file_path: File path or basename to search for (e.g. 'webhook_queue.py')
        directory: Optional project directory scope
        limit: Max results (default 10, max 20)

    Returns:
        Sessions that touched the file, with how (modified/mentioned) and date.
    """
    limit = min(limit, MAX_RESULTS)
    conn = get_history_conn()

    try:
        params = [f"%{file_path}%"]
        dir_filter = ""
        if directory:
            dir_filter = "AND s.directory = ?"
            params.append(directory)

        rows = conn.execute(f"""
            SELECT DISTINCT sf.root_session_id, sf.session_id,
                   s.title, s.directory, sf.how, sf.time_created
            FROM session_files sf
            JOIN sessions s ON sf.session_id = s.session_id
            WHERE sf.file_path LIKE ?
            {dir_filter}
            ORDER BY sf.time_created DESC
            LIMIT 100
        """, params).fetchall()

        results = []
        for r in rows:
            results.append({
                "session_id": r["session_id"],
                "root_session_id": r["root_session_id"],
                "title": (r["title"] or "")[:100],
                "directory": r["directory"],
                "how": r["how"],
                "date": datetime.fromtimestamp(r["time_created"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            })

        results = dedup_by_root(results, limit)

        return {
            "results": results[:limit],
            "total_found": len(rows),
            "returned": len(results[:limit]),
            "scope": "directory" if directory else "global",
        }
    finally:
        conn.close()


@mcp.tool(annotations=RO)
def find_related_work(
    query: str,
    directory: Optional[str] = None,
    limit: int = 5,
) -> dict:
    """Find sessions related to a task description by matching session titles and original user prompts.

    More precise than search_history for finding sessions on the same topic.

    Args:
        query: Task description (e.g. 'multi-company analytic accounts')
        directory: Optional project directory scope
        limit: Max results (default 5, max 20)

    Returns:
        Sessions whose title or first user prompt matches the query.
    """
    limit = min(limit, MAX_RESULTS)
    conn = get_history_conn()

    try:
        pattern = f"%{query}%"
        params = [pattern, pattern]
        dir_filter = ""
        if directory:
            dir_filter = "AND directory = ?"
            params.append(directory)

        rows = conn.execute(f"""
            SELECT session_id, root_session_id, title, directory,
                   time_created, time_updated, msg_count, tool_count, patch_count,
                   first_user_prompt
            FROM sessions
            WHERE is_sub_agent = 0
              AND (title LIKE ? OR first_user_prompt LIKE ?)
            {dir_filter}
            ORDER BY time_updated DESC
            LIMIT 50
        """, params).fetchall()

        if not rows:
            result = {
                "results": [],
                "total_found": 0,
                "returned": 0,
                "scope": "directory" if directory else "global",
                "query": query,
            }
            if directory:
                result["hint"] = f"No results in '{directory}'. Try without directory for a global search."
            return result

        results = []
        for r in rows:
            recency = compute_recency(r["time_updated"])
            activity = compute_activity(r["tool_count"], r["patch_count"])
            relevance = 1.0 if query.lower() in (r["title"] or "").lower() else 0.6
            score = (relevance * W_RELEVANCE) + (recency * W_RECENCY) + (activity * W_ACTIVITY)

            snippet = (r["first_user_prompt"] or "")[:MAX_SNIPPET_CHARS]
            results.append({
                "session_id": r["session_id"],
                "root_session_id": r["root_session_id"],
                "title": (r["title"] or "")[:100],
                "directory": r["directory"],
                "snippet": snippet,
                "score": round(score, 3),
            })

        results = sorted(results, key=lambda x: x["score"], reverse=True)[:limit]

        return {
            "results": results,
            "total_found": len(rows),
            "returned": len(results),
            "scope": "directory" if directory else "global",
            "query": query,
        }
    finally:
        conn.close()


@mcp.tool(annotations=RO)
def get_session_detail(session_id: str) -> dict:
    """Get full details of a specific session.

    Returns metadata, original task (first user prompt), files modified,
    and the last assistant message.

    Args:
        session_id: The session ID (e.g. 'ses_abc123')

    Returns:
        Complete session metadata including stats, cost, files, and context summary.
    """
    conn = get_history_conn()
    try:
        r = conn.execute("""
            SELECT * FROM sessions WHERE session_id = ?
        """, (session_id,)).fetchone()

        if not r:
            return {"error": f"Session '{session_id}' not found"}

        files = conn.execute("""
            SELECT DISTINCT file_path FROM session_files
            WHERE session_id = ? AND how = 'modified'
            ORDER BY file_path LIMIT 50
        """, (session_id,)).fetchall()

        sub_count = conn.execute("""
            SELECT COUNT(*) FROM sessions WHERE root_session_id = ? AND is_sub_agent = 1
        """, (r["root_session_id"],)).fetchone()[0]

        return {
            "session_id": r["session_id"],
            "root_session_id": r["root_session_id"],
            "is_sub_agent": bool(r["is_sub_agent"]),
            "parent_id": r["parent_id"],
            "title": r["title"],
            "directory": r["directory"],
            "time_created": datetime.fromtimestamp(r["time_created"] / 1000, tz=timezone.utc).isoformat(),
            "time_updated": datetime.fromtimestamp(r["time_updated"] / 1000, tz=timezone.utc).isoformat(),
            "stats": {
                "messages": r["msg_count"],
                "tools_used": r["tool_count"],
                "patches": r["patch_count"],
                "text_parts": r["text_count"],
                "reasoning_parts": r["reasoning_count"],
            },
            "cost": round(r["cost"], 2),
            "tokens": {
                "input": r["tokens_input"],
                "output": r["tokens_output"],
                "reasoning": r["tokens_reasoning"],
                "cache_read": r["tokens_cache_read"],
                "cache_write": r["tokens_cache_write"],
            },
            "first_user_prompt": r["first_user_prompt"] or "(no user prompt found)",
            "last_assistant_text": r["last_assistant_text"] or "(no assistant text found)",
            "files_modified": [f["file_path"] for f in files],
            "sub_agent_count": sub_count,
        }
    finally:
        conn.close()


@mcp.tool(annotations=RO)
def get_session_messages(
    session_id: str,
    limit: int = DEFAULT_MESSAGES,
    offset: int = 0,
) -> dict:
    """Read the actual messages from a session (paginated).

    Use this after finding a session with the discovery tools to read its
    content. Each text part is hard-truncated to 1000 chars to control
    token budget. Tool calls return only name + status (not output).
    File/base64 parts are skipped.

    Args:
        session_id: The session ID
        limit: Max messages to return (default 10, max 50)
        offset: Pagination offset (for reading beyond the first page)

    Returns:
        Messages with role, parts (text/tool/patch), and timestamps.
    """
    limit = min(limit, MAX_MESSAGES)
    conn = get_opencode_conn()

    try:
        check = conn.execute("SELECT id FROM session WHERE id = ?", (session_id,)).fetchone()
        if not check:
            return {"error": f"Session '{session_id}' not found"}

        rows = conn.execute("""
            SELECT m.id as msg_id, m.time_created as msg_time, m.data as msg_data,
                   p.id as part_id, p.time_created as part_time, p.data as part_data
            FROM (
                SELECT id, time_created, data, session_id
                FROM message
                WHERE session_id = ?
                ORDER BY time_created ASC
                LIMIT ? OFFSET ?
            ) m
            LEFT JOIN part p ON p.message_id = m.id
            ORDER BY m.time_created ASC, p.time_created ASC
        """, (session_id, limit, offset)).fetchall()

        messages = []
        current_msg = None
        msg_count = 0

        for row in rows:
            msg_id = row["msg_id"]
            if msg_id is None:
                continue

            if current_msg is None or msg_id != current_msg["msg_id"]:
                if current_msg and len(current_msg["parts"]) > 0:
                    messages.append(current_msg)
                    msg_count += 1
                    if msg_count >= limit:
                        break

                try:
                    msg_data = json.loads(row["msg_data"]) if row["msg_data"] else {}
                except json.JSONDecodeError:
                    msg_data = {}

                current_msg = {
                    "msg_id": msg_id,
                    "role": msg_data.get("role", "unknown"),
                    "time": datetime.fromtimestamp(row["msg_time"] / 1000, tz=timezone.utc).isoformat(),
                    "parts": [],
                }

            try:
                pd = json.loads(row["part_data"]) if row["part_data"] else None
            except json.JSONDecodeError:
                pd = None

            if not pd:
                continue

            ptype = pd.get("type", "")

            if ptype == "text":
                text = pd.get("text", "")
                if len(text) > MAX_PART_CHARS:
                    text = text[:MAX_PART_CHARS] + f" [... truncated, {len(text)} chars total]"
                current_msg["parts"].append({"type": "text", "text": text})

            elif ptype == "tool":
                state = pd.get("state", {})
                current_msg["parts"].append({
                    "type": "tool",
                    "name": pd.get("tool", "unknown"),
                    "status": state.get("status", "unknown"),
                })

            elif ptype == "patch":
                files = pd.get("files", [])
                if files:
                    current_msg["parts"].append({"type": "patch", "files": files[:10]})

            elif ptype == "reasoning":
                text = pd.get("text", "")
                if text:
                    if len(text) > MAX_PART_CHARS:
                        text = text[:MAX_PART_CHARS] + f" [... truncated, {len(text)} chars total]"
                    current_msg["parts"].append({"type": "reasoning", "text": text})

            # step-start, step-finish, agent, file, compaction → SKIP

        if current_msg and len(current_msg["parts"]) > 0:
            current_msg.pop("msg_id", None)
            messages.append(current_msg)

        total = conn.execute("SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)).fetchone()[0]

        return {
            "session_id": session_id,
            "messages": messages,
            "total_messages": total,
            "returned": len(messages),
            "offset": offset,
            "has_more": (offset + len(messages)) < total,
        }
    finally:
        conn.close()


@mcp.tool(annotations=RO)
def list_sessions(
    directory: Optional[str] = None,
    sort: str = "date",
    limit: int = 20,
) -> dict:
    """Browse sessions filtered by directory. Root sessions only (sub-agents excluded).

    Args:
        directory: Optional project directory filter
        sort: Sort order - 'date', 'messages', 'cost', or 'tokens'
        limit: Max results (default 20, max 50)

    Returns:
        Session list with title, directory, date, message count, and cost.
    """
    limit = min(limit, 50)
    conn = get_history_conn()

    try:
        dir_filter = ""
        params = []
        if directory:
            dir_filter = "WHERE directory = ?"
            params.append(directory)

        sort_map = {
            "date": "time_created DESC",
            "messages": "msg_count DESC",
            "cost": "cost DESC",
            "tokens": "(tokens_input + tokens_output) DESC",
        }
        order = sort_map.get(sort, "time_created DESC")

        rows = conn.execute(f"""
            SELECT session_id, title, directory, time_created, time_updated,
                   msg_count, tool_count, patch_count, cost
            FROM sessions
            WHERE is_sub_agent = 0
            {"AND directory = ?" if directory else ""}
            ORDER BY {order}
            LIMIT ?
        """, params + [limit]).fetchall()

        results = [{
            "session_id": r["session_id"],
            "title": (r["title"] or "")[:100],
            "directory": r["directory"],
            "date": datetime.fromtimestamp(r["time_created"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "message_count": r["msg_count"],
            "cost": round(r["cost"], 2),
        } for r in rows]

        total = conn.execute(f"""
            SELECT COUNT(*) FROM sessions WHERE is_sub_agent = 0
            {"AND directory = ?" if directory else ""}
        """, params).fetchone()[0] if params else conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE is_sub_agent = 0"
        ).fetchone()[0]

        return {
            "results": results,
            "total_found": total,
            "returned": len(results),
            "scope": "directory" if directory else "global",
        }
    finally:
        conn.close()


@mcp.tool(annotations=RO)
def get_stats(directory: Optional[str] = None) -> dict:
    """Overview statistics: session counts, messages, cost, time range, and distribution.

    Root vs sub-agent breakdown. Top directories by activity.

    Args:
        directory: Optional scope (global if omitted)

    Returns:
        Aggregate stats including distribution by message count tiers.
    """
    conn = get_history_conn()

    try:
        dir_where = "WHERE directory = ?" if directory else ""
        params = [directory] if directory else []

        row = conn.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE is_sub_agent = 0) as root_sessions,
                COUNT(*) FILTER (WHERE is_sub_agent = 1) as sub_agents,
                SUM(msg_count) as total_msgs,
                SUM(tool_count) as total_tools,
                SUM(patch_count) as total_patches,
                ROUND(SUM(cost), 2) as total_cost,
                MIN(time_created) as first_ts,
                MAX(time_updated) as last_ts
            FROM sessions
            {dir_where}
        """, params).fetchone()

        dist = conn.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE msg_count <= 1) as noise_1_msg,
                COUNT(*) FILTER (WHERE msg_count BETWEEN 2 AND 5) as brief_2_5,
                COUNT(*) FILTER (WHERE msg_count BETWEEN 6 AND 50) as normal_6_50,
                COUNT(*) FILTER (WHERE msg_count BETWEEN 51 AND 500) as rich_51_500,
                COUNT(*) FILTER (WHERE msg_count > 500) as epic_500_plus
            FROM sessions
            WHERE is_sub_agent = 0
            {"AND directory = ?" if directory else ""}
        """, params).fetchone()

        top_dirs = []
        if not directory:
            for r in conn.execute("""
                SELECT directory,
                       COUNT(*) FILTER (WHERE is_sub_agent = 0) as roots,
                       SUM(msg_count) as msgs
                FROM sessions
                WHERE is_sub_agent = 0
                GROUP BY directory
                ORDER BY roots DESC LIMIT 5
            """).fetchall():
                top_dirs.append({
                    "directory": r["directory"],
                    "sessions": r["roots"],
                    "messages": r["msgs"],
                })

        return {
            "scope": directory or "global",
            "total_sessions": row["root_sessions"],
            "total_sub_agents": row["sub_agents"],
            "total_messages": row["total_msgs"],
            "total_cost": row["total_cost"],
            "time_range": {
                "first": datetime.fromtimestamp(row["first_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if row["first_ts"] else None,
                "last": datetime.fromtimestamp(row["last_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if row["last_ts"] else None,
            },
            "distribution": {
                "noise_1_msg": dist["noise_1_msg"],
                "brief_2_5": dist["brief_2_5"],
                "normal_6_50": dist["normal_6_50"],
                "rich_51_500": dist["rich_51_500"],
                "epic_500_plus": dist["epic_500_plus"],
            },
            "top_directories": top_dirs,
        }
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════


def run() -> None:
    """Entry point used by the `opencode-history-mcp` console script."""
    mcp.run()


if __name__ == "__main__":
    run()
