# OpenCode History MCP

A local MCP (Model Context Protocol) server that lets AI coding agents
search your **past OpenCode conversations** — before they start
exploring files or re-doing work you already did.

Everything runs on your machine: it reads OpenCode's own SQLite database
and builds a private full-text search index next to it. No network
calls, no external services, **no data ever leaves your computer**.

[![PyPI](https://img.shields.io/pypi/v/opencode-history-mcp)](https://pypi.org/project/opencode-history-mcp/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-orange)](https://modelcontextprotocol.io)

## Why

If you use [OpenCode](https://opencode.ai) daily across many projects,
you build up thousands of past sessions — bug fixes, feature work,
diagnostics — sitting untapped in `opencode.db`. When you start a new
session on the same module or file, your agent has no idea any of that
happened. It re-explores from scratch, or worse, repeats a mistake you
already fixed three weeks ago.

This server exposes that history as MCP tools any agent can call:
*"has this file been touched before? what did we conclude last time?
what related work exists in this project?"*

## How it works

```
OpenCode's own DB (read-only)          Our derived index (read-write)
┌─────────────────────────┐            ┌──────────────────────────┐
│ opencode.db              │  builds →  │ opencode-history.db       │
│ - session / message /part│            │ - sessions (denormalized) │
│ - JSON blobs per row      │            │ - search_idx (FTS5)       │
└─────────────────────────┘            │ - session_files (index)   │
                                        └──────────────────────────┘
```

- **Source DB stays untouched.** We open it `mode=ro` (read-only, WAL-aware)
  and never write to it.
- **A separate FTS5 index** holds denormalized session metadata + full-text
  search over user/assistant text — orders of magnitude faster than
  scanning JSON blobs on every query.
- **Auto-sync on startup**, TTL-cached (5 min): if OpenCode wrote new
  sessions since the last check, the index catches up incrementally
  before serving results.
- **Privacy is structural, not a policy**: the index lives next to
  OpenCode's own DB, on your machine, under your OS user. There is no
  hosted/shared version of this server — everyone runs their own,
  against their own history.

## Quickstart

### 1. Build the index (first run)

```bash
uvx opencode-history-mcp --build-index
```

This reads your local `opencode.db` and builds `opencode-history.db`
next to it. Takes a few seconds per thousand sessions.

### 2. Add it to your MCP client

<details>
<summary><b>Hermes Agent</b></summary>

```bash
hermes mcp add history \
  --command uvx \
  --args opencode-history-mcp
```

Or in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  history:
    command: uvx
    args:
      - opencode-history-mcp
    enabled: true
```
</details>

<details>
<summary><b>OpenCode</b></summary>

In `~/.config/opencode/opencode.jsonc` (global) or `.opencode/opencode.jsonc`
(project):

```jsonc
{
  "mcp": {
    "history": {
      "type": "local",
      "command": ["uvx", "opencode-history-mcp"],
      "enabled": true
    }
  }
}
```
</details>

<details>
<summary><b>Claude Desktop</b></summary>

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "opencode-history": {
      "command": "uvx",
      "args": ["opencode-history-mcp"]
    }
  }
}
```
</details>

<details>
<summary><b>Cursor / other MCP clients</b></summary>

Any client that supports local stdio MCP servers works the same way —
point it at:

```
command: uvx
args: ["opencode-history-mcp"]
```
</details>

### 3. Keep the index fresh (optional)

The server auto-syncs on startup (checked every 5 minutes per session).
For a fully up-to-date index without waiting on that check, run:

```bash
uvx opencode-history-mcp --sync-index
```

You can schedule this with cron/launchd if you want the index always
warm ahead of time.

## Tools

| Tool | Purpose |
|---|---|
| `search_history` | Full-text search (FTS5) over user prompts and assistant responses. Ranked by relevance + recency + activity. |
| `find_related_work` | Higher-precision match on session titles and original task descriptions. Best first call for "have we done this before?" |
| `find_sessions_by_file` | Find every session that modified or mentioned a specific file. |
| `list_sessions` | Browse sessions in a directory, sorted by date/messages/cost/tokens. |
| `get_session_detail` | Full metadata for one session: task, files touched, cost, tokens, sub-agent count. |
| `get_session_messages` | Read the actual paginated message history of a session. |
| `get_stats` | Aggregate stats: session/message counts, cost, time range, activity distribution. |

All tools accept an optional `directory` parameter to scope results to
one project. **Recommended pattern**: search scoped to the current
project first; if nothing relevant comes back, retry without
`directory` for a global search — related work sometimes lives in a
sibling project.

## Cross-platform paths

The server resolves OpenCode's data directory the same way OpenCode
itself does (its `xdg-basedir`-based resolution — see
[`packages/core/src/global.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/global.ts)
in the OpenCode source):

| Platform | Default path | Notes |
|---|---|---|
| **Linux** | `$XDG_DATA_HOME/opencode` → falls back to `~/.local/share/opencode` | Standard XDG Base Directory behavior. |
| **macOS** | `~/.local/share/opencode` | ⚠️ **Not** `~/Library/Application Support/opencode`. OpenCode has no macOS-specific branch in its path resolution — it uses the same XDG-style path as Linux. This trips people up who assume Apple conventions apply. |
| **Windows** | `%LOCALAPPDATA%\opencode` | Falls back to `%USERPROFILE%\AppData\Local\opencode` if the env var is unset. |
| **WSL (WSL2/WSL1)** | Same as Linux — `~/.local/share/opencode` | WSL runs a real Linux kernel, so `sys.platform` reports `"linux"` and the Linux path applies automatically. **This is only correct if OpenCode itself runs inside WSL.** |

### The WSL + Windows-side-OpenCode edge case

If you installed OpenCode on **Windows natively** (not inside WSL) but
run your MCP client or terminal **inside WSL**, the database lives on
the Windows filesystem, which WSL mounts under `/mnt/c/...`. The
automatic Linux-path resolution will look in the wrong place (your
WSL home directory, not the Windows one) and won't find it.

Fix: point the server explicitly at the mounted Windows path via the
`OPENCODE_DATA_DIR` environment variable:

```bash
export OPENCODE_DATA_DIR="/mnt/c/Users/<your-windows-username>/AppData/Local/opencode"
```

Or set it in your MCP client's `env` config for this server, e.g. for
Hermes:

```yaml
mcp_servers:
  history:
    command: uvx
    args:
      - opencode-history-mcp
    env:
      OPENCODE_DATA_DIR: /mnt/c/Users/yourname/AppData/Local/opencode
    enabled: true
```

### Any other custom setup

`OPENCODE_DATA_DIR` always wins over auto-detection, on every platform
— use it whenever OpenCode's data lives somewhere non-standard (custom
`XDG_DATA_HOME`, a container, a synced/mounted drive, etc).

## Teaching your agent to use this automatically

Having the tools available isn't enough — agents default to exploring
files directly unless told otherwise. Add this to your project's
`AGENTS.md` (OpenCode) or `CLAUDE.md` (Claude Code) to make history
search a mandatory first step:

```markdown
## Check history before starting work

Before exploring files or writing code for any task that touches an
existing module, file, or bug, call the history search tools first:

1. `find_related_work(query="<short description of the task>")` —
   has this exact task been worked on before?
2. If the task names a specific file, also call
   `find_sessions_by_file(file_path="...")`.
3. If step 1 returns nothing relevant, broaden with
   `search_history(query="...")` (full-text, no directory scope).

Only start exploring the codebase directly if history search comes up
empty. If a relevant past session is found, read it with
`get_session_detail` / `get_session_messages` before proceeding —
don't repeat work or re-diagnose an issue that was already solved.
```

This is a strong nudge, not a hard constraint — the agent can still
decide history search isn't relevant for a truly new task. The goal is
making "check first" the default reflex instead of an afterthought.

## Development

```bash
git clone https://github.com/crottolo/opencode-history-mcp.git
cd opencode-history-mcp
uv venv
uv pip install -e .

# Build the index against your own OpenCode history
python -m opencode_history_mcp.build_index --full

# Run the server directly (stdio)
python -m opencode_history_mcp.server

# Inspect with the FastMCP dev tools
fastmcp dev -m opencode_history_mcp.server
```

See [`docs/design.md`](docs/design.md) for the full design rationale
(ranking formula, schema decisions, sync algorithm).

## Contributing

Issues and PRs welcome. If you hit a platform-specific path issue,
please include your OS, `OPENCODE_DATA_DIR` (if set), and the actual
location of your `opencode.db` — that's the fastest way to fix an edge
case in the resolution logic.

## License

MIT — see [LICENSE](LICENSE).
