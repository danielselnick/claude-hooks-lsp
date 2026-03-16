# lsp-hooks

Proactive LSP context injection for Claude Code.

Automatically gives Claude structural knowledge of your codebase — symbols, diagnostics, type info, call graphs — before every read, write, and prompt.

## Quick Start

```sh
# 1. Clone
git clone https://github.com/danielselnick/claude-hooks-lsp.git
cd claude-hooks-lsp

# 2. Register as a plugin marketplace
claude /plugin marketplace add "$(pwd)"

# 3. Run the installer (installs npm deps, checks LSP servers, starts daemon)
python3 install.py

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
| Rust | rust-analyzer | `cargo install rust-analyzer` |
| Python | pylsp | `uv tool install python-lsp-server` |
| TypeScript/JS | typescript-language-server | `npm install -g typescript-language-server` |
| C# | csharp-ls | `dotnet tool install --global csharp-ls` |
| C/C++ | clangd | Xcode CLI tools or `brew install llvm` |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` |

The installer (`python3 install.py`) will check for these and offer to install any that are missing.

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
python3 lsp_hooks_daemon.py
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
