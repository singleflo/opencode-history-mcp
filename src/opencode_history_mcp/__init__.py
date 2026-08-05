"""opencode_history_mcp — MCP server exposing local OpenCode session
history as searchable tools (search, file-lookup, session detail,
stats) for AI coding agents (Hermes, OpenCode, Claude Desktop, Cursor,
and any other MCP-compatible client).

Runs entirely locally: it reads OpenCode's own SQLite database and
builds a private FTS5 search index. No network calls, no external
services, no data leaves your machine.
"""

__version__ = "0.1.0"
