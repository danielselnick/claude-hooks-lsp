# lsp-hooks

**A structural knowledge graph for Claude Code.**

lsp-hooks gives Claude a formal understanding of your codebase's type system, symbol hierarchy, call graph, and diagnostics — injected automatically before every tool call. Claude doesn't grep and hope; it already knows.

## Why lsp-hooks?

There are three ways to give an AI agent access to language intelligence. Two of them don't work well in practice.

| Approach | Model | Problem |
|---|---|---|
| **Plain LSP** (IDE-style) | Pull-only — the model must know what to ask for | Great for autocomplete. Useless for an agent that doesn't know what it doesn't know. Claude would need to issue dozens of LSP queries per task, and it has no reason to. |
| **MCP server** (tool-based) | Request/response — Claude *can* call LSP tools | Claude must decide to query. In practice it rarely does, burning tokens re-reading files and guessing at structure instead. |
| **lsp-hooks** (push model) | Context injected *before* every tool call via hooks | Claude never has to ask. It already knows the symbol tree, callers, diagnostics, and exports before it reads, writes, or builds. Zero-effort, zero-token-waste structural awareness. |

The key insight: **push, not pull.** Claude Code's hook system lets lsp-hooks inject LSP context into the system message before every read, write, build, search, and prompt. The agent receives structural knowledge as a side effect of acting — no extra tool calls, no wasted tokens, no missed context.

## How It Helps Plan Mode

lsp-hooks transforms Plan Mode from "grep and hope" into "reason over a knowledge graph":

- **Session start** — Claude receives a project-wide structural overview (workspace symbols grouped by kind: structs, functions, traits, enums)
- **Before every file read** — Symbol tree + diagnostics + exports. Claude knows the shape of a file before seeing raw text
- **Before every edit** — Full call graph, references, implementations. Claude understands blast radius before changing a single line
- **Before build commands** — Pre-build diagnostics surface known errors before wasting a build cycle
- **Cache eviction is skipped during Plan Mode** — The warm knowledge graph persists across the planning/execution boundary

**Net effect:** Plan Mode operates on a formal knowledge graph instead of grepping and hoping. Fewer file reads, fewer wrong turns, faster plans.

## The Knowledge Graph

lsp-hooks builds and maintains a live structural model of your codebase:

- **Persistent daemon** maintains a two-layer cache (in-memory TTL + SQLite on disk)
- **Language servers** provide the ground truth — types, call hierarchies, references, diagnostics
- **Every hook event enriches the graph** — reads warm the cache, writes invalidate stale entries
- **Partial results are always returned** — parallel LSP queries with a gather timeout mean some context is always better than none
- **The graph survives across sessions** — SQLite cache persists across daemon restarts; in-memory cache warms up as you work
- **First miss, second hit** — If a query exceeds the hook timeout, the daemon finishes the work and caches the result. Next invocation is instant.

## Quick Start

```sh
# 1. Clone
git clone https://github.com/danielselnick/claude-hooks-lsp.git
cd claude-hooks-lsp

# 2. Register as a plugin marketplace
claude /plugin marketplace add "$(pwd)"

# 3. Run the installer (installs npm deps, checks LSP servers, starts daemon)
uv run install.py

# 4. Enable the plugin when prompted, or manually:
claude /plugin enable lsp-hooks
```

That's it. Open a new Claude Code session in any supported project and LSP context will appear automatically.

## What Happens

When you use Claude Code in a supported project, lsp-hooks fires on every hook event:

```
You open a session in a Rust project
  → SessionStart hook fires
  → Claude sees: "[LSP] Project overview: Structs: `Config`, `Server`; Functions: `main`, `run`"

You ask Claude to read src/server.rs
  → PreToolUse(Read) hook fires
  → Claude sees: "[LSP] Structure of src/server.rs: `Server` (Struct, L12), `new` (Method, L15)..."

You ask Claude to edit a function
  → PreToolUse(Edit) hook fires
  → Claude sees: "[LSP] Structural context: `handle_request` called by `main`, `dispatch`; 8 references across 3 files"

You ask Claude to run `cargo build`
  → PreToolUse(Bash) hook fires
  → Claude sees: "[LSP] Pre-build diagnostics: src/server.rs:42: mismatched types"
```

## Architecture

```
Hook Event (pre-read, pre-write, pre-bash, prompt, session-start)
    │
    ▼
lsp_hooks.py  ─── thin client, <100ms overhead
    │ Unix socket
    ▼
lsp_hooks_daemon.py  ─── persistent process, caches results
    │ JSON-RPC over stdio
    ▼
lsp-mcp-server  ─── npm package, bundled automatically
    │ LSP protocol
    ▼
Language Servers (rust-analyzer, pylsp, typescript-language-server, etc.)
```

The daemon auto-starts on first hook invocation if not already running.

## Prerequisites

| Requirement | Version | Check |
|---|---|---|
| Python | 3.9+ | `python3 --version` |
| Node.js | 18+ | `node --version` |
| uv | latest | `uv --version` |
| Claude Code | latest | `claude --version` |

Don't have uv? `curl -LsSf https://astral.sh/uv/install.sh | sh`

At least one LSP server for your language(s):

| Language | Server | Install |
|---|---|---|
| Rust | rust-analyzer | `rustup component add rust-analyzer` |
| Python | pylsp | `uv tool install python-lsp-server` |
| TypeScript/JS | typescript-language-server | `npm install -g typescript-language-server` |
| C# | csharp-ls | `dotnet tool install --global csharp-ls` |
| C/C++ | clangd | Xcode CLI tools or `brew install llvm` |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` |

The installer (`uv run install.py`) will check for these and offer to install any that are missing.

## Hook Events

| Event | Trigger | Context Provided |
|---|---|---|
| `session-start` | New session begins | Project overview via workspace symbols |
| `pre-read` | Before reading a file | Symbol tree, diagnostics, exports |
| `pre-write` | Before writing/editing | Full structural context, call graph, references |
| `pre-bash` | Before build/test commands | Pre-build diagnostics for recently edited files |
| `prompt` | User submits a prompt | Symbol/file lookups for mentioned entities |

## Configuration

Optional overrides in `~/.lsp-hooks/config.json`:

```json
{
  "limits": {
    "max_symbols_per_file": 5,
    "max_callers_shown": 3
  },
  "cache_ttl_seconds": 60
}
```

## Troubleshooting

**View logs:**
```sh
tail -f /tmp/lsp-hooks-$(whoami).log
```

**Restart daemon:**
```sh
uv run lsp_hooks_daemon.py
```

**Check if daemon is running:**
```sh
cat /tmp/lsp-hooks-$(whoami).pid && ps -p $(cat /tmp/lsp-hooks-$(whoami).pid)
```

**Hooks not firing:**
```sh
# Verify plugin is registered and enabled
claude /plugin list
```

**No LSP context appearing:**
- Ensure a language server is installed for your project's language
- Check the log for `MCP started` — if missing, the daemon can't reach lsp-mcp-server
- Run `npm install` in the plugin directory if `node_modules/` is missing

## Uninstall

```sh
# Remove plugin
claude /plugin marketplace remove lsp-hooks

# Stop daemon
kill $(cat /tmp/lsp-hooks-$(whoami).pid)

# Delete clone
rm -rf /path/to/claude-hooks-lsp
```

## License

MIT
