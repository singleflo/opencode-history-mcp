# Design

This document explains the architecture, schema, sync strategy, and
ranking formula behind `opencode-history-mcp` — useful if you're
extending the server or just curious how it works under the hood.

## 1. Architecture

```
src/opencode_history_mcp/
├── __init__.py
├── __main__.py       # CLI entry point (server / --build-index / --sync-index)
├── paths.py           # cross-platform resolution of OpenCode's data dir
├── build_index.py     # indexer: OpenCode DB → FTS5 index
└── server.py           # MCP server (FastMCP, 7 tools, stdio)

OpenCode's DB (read-only):   <data_dir>/opencode.db
Our index (read-write):       <data_dir>/opencode-history.db
```

**Stack**: [FastMCP](https://github.com/jlowin/fastmcp) (the standard
Python MCP SDK), stdio transport, `sqlite3` from the standard library —
zero runtime dependencies beyond FastMCP itself.

## 2. Why a separate index

OpenCode's own database stores conversation content as JSON blobs
inside `message.data` and `part.data` columns. Searching that directly
means scanning and `json_extract()`-ing every row on every query —
fine for a handful of sessions, painfully slow once you have thousands
(a real-world OpenCode history can reach 10+ GB and 500k+ `part` rows).

So we build a **separate, derived index**:

- The source DB stays completely untouched — opened `mode=ro` (respects
  WAL, unlike `immutable=1` which silently misses uncommitted-but-WAL'd
  data).
- The index holds denormalized session metadata (message/tool/patch
  counts, cost, tokens — pre-aggregated at build time) plus an FTS5
  virtual table for full-text search over user/assistant text.
- Everything not needed for search or discovery is excluded: tool call
  outputs, step markers, file/base64 blobs, compaction summaries.

## 3. Index schema (`opencode-history.db`)

### 3.1 `sessions` — denormalized metadata

```sql
CREATE TABLE sessions (
    session_id         TEXT PRIMARY KEY,
    root_session_id    TEXT NOT NULL,   -- resolved recursively; = session_id for roots
    is_sub_agent        INTEGER NOT NULL,
    parent_id           TEXT,
    title                TEXT NOT NULL,
    directory            TEXT NOT NULL,
    project_id           TEXT,
    time_created         INTEGER NOT NULL,
    time_updated         INTEGER NOT NULL,
    msg_count            INTEGER NOT NULL DEFAULT 0,
    tool_count           INTEGER NOT NULL DEFAULT 0,
    patch_count          INTEGER NOT NULL DEFAULT 0,
    text_count           INTEGER NOT NULL DEFAULT 0,
    reasoning_count      INTEGER NOT NULL DEFAULT 0,
    cost                 REAL NOT NULL DEFAULT 0,
    tokens_input          INTEGER NOT NULL DEFAULT 0,
    tokens_output         INTEGER NOT NULL DEFAULT 0,
    tokens_reasoning      INTEGER NOT NULL DEFAULT 0,
    tokens_cache_read     INTEGER NOT NULL DEFAULT 0,
    tokens_cache_write    INTEGER NOT NULL DEFAULT 0,
    first_user_prompt     TEXT,   -- the session's original task, truncated to 500 chars
    last_assistant_text    TEXT,   -- stand-in for a "summary" (OpenCode has no compaction summary field)
    schema_version         INTEGER NOT NULL DEFAULT 1
);
```

Sub-agent sessions (spawned by a root session, e.g. explore/librarian
agents) are resolved to their `root_session_id` via a recursive CTE
over `parent_id` at build time — this lets search results group by
"real" conversation instead of surfacing every sub-agent run as a
separate hit.

### 3.2 `search_idx` — FTS5, text parts only

```sql
CREATE VIRTUAL TABLE search_idx USING fts5(
    session_id UNINDEXED,
    root_session_id UNINDEXED,
    directory UNINDEXED,
    role,             -- 'user' | 'assistant'
    text,
    time_created UNINDEXED,
    tokenize = 'porter unicode61'
);
```

Only `part.type = 'text'` rows go in — no tool output, no reasoning
traces, no patch diffs. This keeps the index small and the search
signal clean.

### 3.3 `session_files` — file → session lookup

```sql
CREATE TABLE session_files (
    session_id      TEXT NOT NULL,
    root_session_id TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    how             TEXT NOT NULL,   -- 'modified' (from a patch part)
    time_created    INTEGER NOT NULL,
    PRIMARY KEY (session_id, file_path, how)
);
```

Populated from `patch`-type parts' `files` array at build time —
answers "which sessions touched this file?" in under 50ms via the
indexed `file_path` column, instead of a `LIKE` scan over every text
part.

### 3.4 `index_meta` — sync bookkeeping

Key/value table tracking `schema_version`, the sync watermark
(`last_sync_part_time`), and rebuild timestamps — see §4.

## 4. Sync strategy

### Full build

```bash
opencode-history-mcp --build-index
```

Drops and recreates the schema, then does a single-pass extraction:
resolve all roots → index sessions → index text parts (batched
inserts, 5000 rows/batch) → index patch files → write the watermark.

### Incremental sync (on server startup)

The server checks the index on startup (TTL-cached, 5 minutes between
checks to avoid overhead on every tool call):

```
db_max = MAX(part.time_updated) in OpenCode's DB
idx_max = index_meta['last_sync_part_time']

if db_max > idx_max:
    affected_sessions = sessions with part.time_updated > idx_max
    for each affected session: delete its old index rows, re-index it
    detect and remove sessions that no longer exist in OpenCode's DB
    update the watermark
```

We use `time_updated` (not `time_created`) because OpenCode sessions
are long-lived — a session opened weeks ago can still receive new
messages today, and `time_created` alone would miss that.

### Schema versioning

If `index_meta.schema_version` doesn't match the server's expected
version, a full rebuild runs automatically instead of erroring.

## 5. The 7 tools

| Tool | Backing query | Typical latency |
|---|---|---|
| `search_history` | FTS5 `MATCH` + BM25 ranking on `search_idx` | ~20-50ms |
| `find_related_work` | `LIKE` on `sessions.title` / `first_user_prompt` (small table, root sessions only) | <5ms |
| `find_sessions_by_file` | `LIKE` on indexed `session_files.file_path` | <50ms |
| `list_sessions` | Sorted scan of `sessions`, pre-aggregated columns | <5ms |
| `get_session_detail` | Single-row lookup + `session_files` join | <5ms |
| `get_session_messages` | Paginated join against OpenCode's own DB (`message`/`part`) — the only tool that reads the source DB directly, since full message content isn't duplicated into the index | ~10-30ms |
| `get_stats` | Aggregate `COUNT`/`SUM`/`FILTER` over `sessions` | <10ms |

`get_session_messages` deliberately reads from OpenCode's DB rather
than the index — duplicating full message content into a second
database would double storage for no query-speed benefit, since it's
only called after a session is already identified.

## 6. Ranking formula

Used by `search_history` and `find_related_work`:

```
final_score = (relevance × 0.5) + (recency × 0.3) + (activity × 0.2)
```

**Relevance** — FTS5's BM25 score is negative and unbounded, so we
normalize it relative to the best result in the current query:

```python
relevance = abs(bm25_score) / max(abs(s) for s in all_scores)
```

**Recency** — exponential decay, 180-day half-life (chosen because
long-running projects can span many months; a 90-day half-life
over-penalizes still-relevant older work):

```python
days_ago = (now - session.time_updated) / 86400
recency = 0.5 ** (days_ago / 180.0)
```

| Days ago | Recency |
|---|---|
| 0 | 1.00 |
| 30 | 0.89 |
| 90 | 0.71 |
| 180 | 0.50 |

**Activity** — a session with real tool/patch activity ranks above an
equally-relevant but idle one, on a log scale so a handful of extra
tool calls doesn't dominate the score:

```python
action_count = tool_count + (patch_count * 3)  # patches weighted higher
activity = min(1.0, log10(action_count + 1) / 3.0)
```

## 7. Deduplication

Sub-agent sessions inflate raw match counts — a single root
conversation with 10 explore sub-agents can produce 11 FTS5 hits for
the same underlying task. Results are deduplicated by `root_session_id`
after ranking, keeping only the best-scoring hit per root.

## 8. Response size discipline

Every tool response is capped to keep token usage predictable:

- Snippets: 200 chars max
- Message text parts (`get_session_messages`): 1000 chars, hard-truncated
  with an explicit `[... truncated, N chars total]` marker
- Tool call parts: name + status only, never the raw output
- `search_history` / `find_sessions_by_file`: 20 results max
- `get_session_messages`: 50 messages max per page

This is a deliberate trade-off: discovery tools stay cheap enough to
call liberally, and `get_session_messages` is the explicit "drill in
and pay the token cost" step once a relevant session is found.
