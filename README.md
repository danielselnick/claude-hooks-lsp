# lsp-hooks

Proactive LSP context injection for Claude Code

## What it does

lsp-hooks is a Claude Code plugin that automatically injects LSP-powered context into Claude's context window via hooks and a persistent daemon. Before Claude reads, writes, or processes your prompts, the plugin queries live Language Server Protocol data — symbols, diagnostics, type information, and call graphs — and prepends that context automatically. This gives Claude accurate, up-to-date structural knowledge of your codebase without requiring you to manually paste or explain it.

## Architecture

```
Hook Event (pre-read, pre-write, etc.)
    │
    ▼
lsp_hooks.py (hook client)
    │ Unix socket
    ▼
lsp_hooks_daemon.py (persistent daemon)
    │ JSON-RPC stdio
    ▼
lsp-mcp-server (npm package)
    │ LSP protocol
    ▼
Language Servers (rust-analyzer, pylsp, tsserver, etc.)
```

## Install

### Prerequisites

- Python 3.9+
- Node.js 18+
- Claude Code CLI

### Step 1: Clone the repo

```sh
git clone https://github.com/your-org/lsp-hooks.git
cd lsp-hooks
```

### Step 2: Install as a plugin via the marketplace

```sh
claude /plugin marketplace add /path/to/lsp-hooks
```

### Step 3: Run the installer

```sh
python3 install.py
```

This installs npm dependencies, checks for available LSP servers, and starts the daemon.

### Step 4: Enable the plugin

Enable the plugin in Claude Code settings (Settings > Plugins > lsp-hooks).

## Supported Languages

- Rust
- Python
- TypeScript / JavaScript
- C#
- C / C++
- Go

## Hook Events

| Event | Trigger | Context Provided |
|---|---|---|
| pre-read | Before reading a file | Symbol tree, diagnostics, exports |
| pre-write | Before writing/editing | Full structural context, call graph, references |
| pre-bash | Before build commands | Pre-build diagnostics for recently edited files |
| prompt | User submits prompt | Symbol/file lookups for mentioned entities |
| session-start | New session begins | Project overview via workspace symbols |

## Configuration

Global overrides for budgets, limits, and filters can be placed in `~/.lsp-hooks/config.json`. This file is created with defaults on first run and supports options such as maximum context token budgets per hook event, per-language server toggles, and file path exclusion filters.

## Troubleshooting

- **Logs:** `/tmp/lsp-hooks-<user>.log`
- **Check daemon:** Look for a PID file at `/tmp/lsp-hooks-<user>.pid`
- **Restart daemon:**
  ```sh
  python3 lsp_hooks_daemon.py
  ```
- **Hooks not firing:** Ensure the plugin is enabled, then verify with:
  ```sh
  claude /plugin list
  ```

## License

MIT
