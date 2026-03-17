# lsp-hooks Specification

> **Version**: 1.11.0
> **Status**: Production
> **Last updated**: 2026-03-16

This document is a language-agnostic specification for the lsp-hooks system. It captures all requirements, architecture, behaviors, and lessons learned — suitable for reimplementation in any language.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Hook Events](#3-hook-events)
4. [Daemon](#4-daemon)
5. [Caching](#5-caching)
6. [MCP/LSP Bridge](#6-mcplsp-bridge)
7. [Version Management](#7-version-management)
8. [Symbol Extraction](#8-symbol-extraction)
9. [Language Support](#9-language-support)
10. [Error Handling](#10-error-handling)
11. [Configuration](#11-configuration)
12. [Lessons Learned](#12-lessons-learned)

---

## 1. System Overview

### What It Does

lsp-hooks is a Claude Code plugin that **proactively injects LSP-derived context** into Claude's system messages before tool calls execute. It intercepts hook events (reads, writes, searches, prompts, session starts), queries language servers for structural information (symbols, diagnostics, references, call hierarchies), and returns that context so Claude can make better-informed decisions.

### Why It Exists

Without lsp-hooks, Claude Code has no awareness of a codebase's type system, symbol structure, or compile-time errors until it reads files and reasons about them. lsp-hooks front-loads this information by:

- Showing the symbol tree and diagnostics before Claude reads a file
- Providing callers, callees, references, and exports before Claude writes to a file
- Surfacing pre-build diagnostics before Claude runs build/test commands
- Enriching grep/glob searches with semantic symbol context
- Giving Claude a project-wide structural overview at session start

### Design Principles

1. **Never block Claude Code.** Every hook must exit 0 regardless of errors. Timeouts are enforced by Claude Code; the daemon runs to completion and caches results for next time.
2. **Warm-up on first miss.** If a daemon query exceeds the hook timeout, Claude Code kills the hook process — but the daemon finishes the work and caches the result. The next invocation is a cache hit.
3. **Graceful degradation everywhere.** Every failure path returns silently. Cache failures, MCP failures, socket failures — all swallowed with logging.
4. **Minimal client, heavy daemon.** The hook client is a thin forwarder (~340 lines). All logic lives in the persistent daemon.

---

## 2. Architecture

### 5-Layer Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1: Claude Code                                            │
│   Fires hook event → passes JSON on stdin → reads stdout        │
├─────────────────────────────────────────────────────────────────┤
│ Layer 2: Hook Client  (spawned per event, <100ms target)        │
│   - Parses event + stdin JSON                                   │
│   - Extension/path filtering                                    │
│   - Version checks (file-based + socket-based)                  │
│   - Auto-starts daemon if needed                                │
│   - Forwards query over Unix socket                             │
│   - Emits {continue, systemMessage} or exits silently           │
├─────────────────────────────────────────────────────────────────┤
│ Layer 3: Daemon  (persistent async process)                     │
│   - Unix socket server                                          │
│   - L1 in-memory TTL cache                                      │
│   - L2 SQLite persistent cache                                  │
│   - Dispatches to event-specific handlers                       │
│   - Watchdog (restarts MCP if it dies)                          │
│   - Cache evictor (background task)                             │
├─────────────────────────────────────────────────────────────────┤
│ Layer 4: MCP Server  (child process, JSON-RPC 2.0 over stdio)  │
│   - Node.js process running lsp-mcp-server                      │
│   - Exposes 24 LSP-wrapping tools                               │
│   - Manages language server lifecycles                          │
├─────────────────────────────────────────────────────────────────┤
│ Layer 5: Language Servers                                       │
│   rust-analyzer, pylsp, typescript-language-server,             │
│   csharp-ls, clangd, gopls, solargraph, intelephense, etc.     │
└─────────────────────────────────────────────────────────────────┘
```

### Process Model

- **Hook client**: spawned by Claude Code for each hook event as a short-lived subprocess. Communicates with the daemon via Unix socket. Exits after receiving a response or timing out.
- **Daemon**: long-lived async process, detached from Claude Code's process group (`start_new_session=true`). Survives Claude Code restarts. Manages a single MCP server child process.
- **MCP server**: child of the daemon, communicates via stdio. Manages one or more language server processes internally.
- **Language servers**: children of the MCP server, one per language detected in the workspace.

### IPC

| Boundary | Transport | Protocol |
|---|---|---|
| Claude Code → Hook client | stdin/stdout | JSON (Claude Code hook protocol) |
| Hook client → Daemon | Unix domain socket | Newline-delimited JSON |
| Daemon → MCP server | stdio (stdin/stdout of child) | JSON-RPC 2.0, newline-delimited |
| MCP server → Language servers | stdio | LSP (JSON-RPC 2.0, Content-Length framed) |

### Recursion Guard

The daemon sets an environment variable (`LSP_HOOKS_ACTIVE=1`) when auto-starting. The hook client checks this variable at entry and exits immediately if set, preventing infinite recursion if a hook triggers another hook.

---

## 3. Hook Events

### 3.1 Hook Registration Schema

Hooks are registered in `hooks/hooks.json`:

```json
{
  "hooks": {
    "<ClaudeCodeEventType>": [
      {
        "matcher": "<ToolNameRegex>",
        "hooks": [
          {
            "type": "command",
            "command": "<interpreter> ${CLAUDE_PLUGIN_ROOT}/<script> --event <event-name>",
            "timeout": <milliseconds>
          }
        ]
      }
    ]
  }
}
```

- `${CLAUDE_PLUGIN_ROOT}` is resolved at runtime by Claude Code to the plugin directory
- `matcher` is a regex tested against the tool name (only for `PreToolUse`)
- `matcher` is absent for `UserPromptSubmit` and `SessionStart` (fires unconditionally)

### 3.2 Registered Events

| Claude Event | Internal Event | Matcher | Timeout | Description |
|---|---|---|---|---|
| `PreToolUse` | `pre-read` | `Read` | 1000ms | Before reading a file |
| `PreToolUse` | `pre-write` | `Write\|Edit\|MultiEdit` | 2000ms | Before writing/editing a file |
| `PreToolUse` | `pre-bash` | `Bash` | 1000ms | Before running a shell command |
| `PreToolUse` | `pre-grep` | `Grep` | 2000ms | Before searching file contents |
| `PreToolUse` | `pre-glob` | `Glob` | 2000ms | Before searching for files |
| `UserPromptSubmit` | `prompt` | *(none)* | 2000ms | When user submits a prompt |
| `SessionStart` | `session-start` | *(none)* | 3000ms | When a Claude Code session begins |

### 3.3 Input Schema (stdin from Claude Code)

```json
{
  "tool_name": "Edit",
  "tool_input": { ... },
  "cwd": "/absolute/path",
  "permission_mode": "default",
  "prompt": "user text"
}
```

- `tool_name` and `tool_input`: present for `PreToolUse` events
- `prompt`: present only for `UserPromptSubmit`
- `cwd`: always present
- `permission_mode`: always present ("default", "plan", etc.)

### 3.4 Output Schema (stdout to Claude Code)

```json
{"continue": true, "systemMessage": "<context string>"}
```

- `continue`: always `true` — hooks never block tool calls
- `systemMessage`: the LSP context to inject
- If no context is available, the hook exits 0 with no output (empty stdout)

### 3.5 Extension and Path Filtering

Applied by the hook client before forwarding to the daemon (only for `pre-read` and `pre-write`):

**Supported extensions**: `.rs`, `.toml`, `.py`, `.pyi`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.cs`, `.go`, `.c`, `.cpp`, `.h`, `.hpp`

**Excluded path prefixes**: `target/`, `.git/`, `node_modules/`

Files not matching are silently skipped.

### 3.6 Event Handler Details

#### `pre-read` — Before Reading a File

**Purpose**: Show Claude the structure of a file before it reads the raw content.

**Parallel MCP calls** (with gather timeout):
1. `lsp_document_symbols` — hierarchical symbol tree
2. `lsp_diagnostics` (severity=error) — compile errors only
3. `lsp_file_exports` — public API surface

**Output format**:
```
[LSP] Structure of <relative_path>:
`SymbolName` (Kind, L<line>)
  `child_method` (Method, L<line>)
Exports: `foo` (Function), `Bar` (Struct)
Diagnostics: 2 error(s)
  L42: mismatched types
    > let x: i32 = "hello";
```

#### `pre-write` — Before Writing/Editing a File

**Purpose**: Provide full semantic context (callers, callees, references, implementations) for symbols Claude is about to modify.

**Steps**:
1. **Invalidate caches** — both L1 and L2 entries for the target file
2. **Fetch document symbols** and flatten the tree
3. **Select relevant symbols**:
   - For Edit: symbols whose names appear in `old_string`
   - For Write: ranked by kind priority (Function > Method > Struct > Trait > Enum > Constant)
4. **Parallel `lsp_smart_search`** for each relevant symbol — includes hover, references, incoming calls, outgoing calls, implementations
5. **Fetch `lsp_file_exports`** in parallel

**Output format**:
```
[LSP] Structural context for <relative_path>:
`SymbolTree` (overview)

`handle_request` (Function, L24) — `fn handle_request(req: &Request) -> Response`:
  Called by: `main` in main.rs, `dispatch` in router.rs
  Calls: `validate`, `process`, `respond`
  Referenced in 8 locations across 3 files
Exports: `handle_request`, `Config`
```

#### `pre-bash` — Before Running a Shell Command

**Purpose**: Surface diagnostics before build/test commands so Claude can anticipate failures.

**Trigger condition**: command matches this pattern:
```
cargo (build|test|check|clippy|run|bench)
npm (run|test|build)
npx tsc
pytest
python -m (pytest|unittest)
dotnet (build|test|run)
```

Commands not matching this pattern are silently skipped.

**Behavior**:
- If there are recent writes (tracked in a list, capped at 20): fetch `lsp_diagnostics` for the last 5 written files
- Otherwise: fetch `lsp_workspace_diagnostics` (error severity, limit 10, grouped by file)

**Output format**:
```
[LSP] Pre-build diagnostics:
  src/server.rs:42: mismatched types
  src/lib.rs:15: cannot borrow as mutable
```

#### `pre-grep` — Before Searching File Contents

**Purpose**: Enrich grep searches with semantic symbol information.

**Behavior**:
1. Extract symbol candidates from the grep regex pattern (see [Symbol Extraction](#8-symbol-extraction))
2. Query up to 2 candidates via `lsp_find_symbol` (includes references, incoming calls, outgoing calls)

**Output format**:
```
[LSP] Symbol context for search `handle_request|process_request`:
  `handle_request` (Function, src/server.rs:24)
    fn handle_request(req: &Request) -> Response
    8 references
    Called by: `main` in main.rs
    Calls: `validate`, `process`
```

#### `pre-glob` — Before Searching for Files

**Purpose**: Provide symbol context when glob patterns suggest the user is looking for a specific symbol.

**Two strategies**:
1. **Symbol extraction from glob basename**: strip wildcards and extension from the last path segment; if result is a valid identifier >= 3 chars, look it up via `lsp_find_symbol`
   - Example: `**/UserService*.ts` → queries `UserService`
2. **Directory-scoped workspace symbols**: if `path` is a subdirectory (not project root), query `lsp_workspace_symbols` filtered to that directory

**Output format** (strategy 1):
```
[LSP] Symbol context for glob `**/UserService*.ts`:
  `UserService` (Class, src/services/UserService.ts:5)
    45 references
    Called by: `UserController` in controllers/UserController.ts
```

#### `prompt` — When User Submits a Prompt

**Purpose**: Pre-fetch context for files and symbols mentioned in the user's natural language prompt.

**Extraction from prompt text**:
- **File paths**: regex match for paths ending in supported extensions
- **Symbol names**: patterns like `fn X`, `struct X`, `trait X`, `impl X`, `enum X`, `mod X`
- **PascalCase identifiers**: multi-word capitalized names (e.g., `UserService`, `HttpClient`)

**Limits**: queries up to 3 entities. Skips files already in `recent_reads` set.

**Output format**:
```
[LSP] Context for prompt:
  src/server.rs: `Server` (Struct, L12), `new` (Method, L15), `run` (Method, L20)
  `HttpClient` at src/client.rs:8 — 12 references
```

#### `session-start` — When a Session Begins

**Purpose**: Give Claude a structural overview of the project.

**Behavior**: queries `lsp_workspace_symbols` with a broad query, groups results by symbol kind.

**Output format**:
```
[LSP] Project overview for my-project:
  Structs: `Config`, `Server`, `Request`
  Functions: `main`, `run`, `handle`
  Traits: `Handler`, `Middleware`
```

---

## 4. Daemon

### 4.1 Lifecycle

**Startup sequence** (in order):
1. Read version from plugin manifest (`plugin.json`)
2. Write VERSION file (must happen before PID file — see [Lessons Learned](#12-lessons-learned))
3. Write PID file
4. Remove stale socket file if present
5. Start MCP server child process (`mcp.start()`)
6. Begin listening on Unix socket (`asyncio.start_unix_server`)
7. Start watchdog task
8. Start cache evictor task

**Shutdown sequence** (triggered by SIGTERM or SIGINT):
1. Stop accepting new connections (close socket server)
2. Drain active handlers — wait up to 2 seconds for in-flight requests
3. Stop MCP child — send SIGTERM, wait up to 5 seconds, then SIGKILL
4. Close SQLite cache connection
5. Unlink socket file, PID file, and VERSION file

### 4.2 Signal Handling

Both SIGTERM and SIGINT trigger the same clean shutdown path. The handler sets an event flag; the main loop awaits that flag, then proceeds to the shutdown sequence. This avoids async-signal-safety issues.

### 4.3 Watchdog

A background task polls the MCP child process every 5 seconds. If the process has exited:

1. Cancel the old reader task
2. Reject all in-flight pending futures with a connection error
3. Clear the pending requests map
4. Launch a new MCP server process
5. Re-run the MCP initialize/initialized handshake

### 4.4 Active Handler Tracking

The daemon tracks all in-flight socket handler tasks in a set. Each handler adds itself on entry and removes itself on exit (in a finally block). During shutdown, `asyncio.wait` is called on all active handlers with a 2-second timeout to allow graceful drain.

### 4.5 Socket Server

**Methods supported**:

| Method | Purpose | Response |
|---|---|---|
| `ping` | Health check | `{"ok": true, "pong": true}` |
| `version` | Report running version | `{"ok": true, "version": "<semver>"}` |
| `query` | Dispatch to event handler | `{"ok": true, "context": "<text>"}` or `{"ok": false, "error": "<msg>"}` |

**Request schema** (hook client to daemon):
```json
{
  "method": "query",
  "request_id": "<8-char hex>",
  "params": {
    "event": "pre-read",
    "file_path": "/absolute/path",
    "tool_input": { ... },
    "cwd": "/absolute/dir",
    "permission_mode": "default"
  }
}
```

### 4.6 Recent Activity Tracking

The daemon maintains two collections for cross-event intelligence:

- **`recent_writes`**: ordered list, capped at 20 entries. Used by `pre-bash` to fetch diagnostics for recently modified files.
- **`recent_reads`**: set, capped at 50 entries (arbitrary eviction on overflow). Used by `prompt` handler to skip files Claude has already read.

### 4.7 Gather with Partial Results

The daemon uses a custom gather function instead of the standard library's `gather`. It runs multiple MCP calls in parallel with a timeout (`GATHER_TIMEOUT = 4.0s`). When the timeout expires:

- Completed tasks return their results
- Timed-out tasks are cancelled and return `None`
- The handler proceeds with whatever partial results are available

This prevents a single slow MCP call from blocking the entire response.

---

## 5. Caching

### 5.1 Two-Layer Architecture

```
┌──────────────────────────────────────────────────────┐
│  L1: In-Memory TTL Cache                             │
│  - Key: "<event>:<file_path>" or "<event>::<hash>"   │
│  - TTL: configurable (default 60s)                   │
│  - Checked first in dispatch                         │
│  - Not persisted across daemon restarts              │
├──────────────────────────────────────────────────────┤
│  L2: SQLite Persistent Cache                         │
│  - Key: (tool_name, SHA-256(canonical_json(args)))   │
│  - WAL mode, synchronous=NORMAL                      │
│  - Persisted at ~/.lsp-hooks/cache.db                │
│  - Survives daemon restarts                          │
└──────────────────────────────────────────────────────┘
```

### 5.2 L1 Cache Details

- Data structure: dictionary mapping cache key to `(value, timestamp)` tuple
- Key format for file-scoped events: `"{event}:{file_path}"`
- Key format for non-file events: `"{event}::{md5(tool_input)[:12]}"`
- Checked first in the dispatch function; hits bypass all MCP calls
- Explicitly invalidated on write events (both `pre-read` and `pre-write` keys for the target file)

### 5.3 L2 Cache Details

**Schema**:
```sql
CREATE TABLE tool_cache (
  id            INTEGER PRIMARY KEY,
  tool_name     TEXT NOT NULL,
  args_hash     TEXT NOT NULL,
  file_path     TEXT,
  file_mtime_ns INTEGER,
  result_json   TEXT NOT NULL,
  created_at    REAL NOT NULL,
  last_hit_at   REAL NOT NULL,
  hit_count     INTEGER DEFAULT 1,
  UNIQUE(tool_name, args_hash)
);
```

**Schema versioning**: a `schema_version` table tracks the current schema version. On version mismatch, the schema is dropped and recreated.

**SQLite pragmas**: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=1000`, connection `timeout=5.0`.

### 5.4 Invalidation Rules

| Scope | Trigger | Mechanism |
|---|---|---|
| File-scoped (L1) | `pre-write` event | Explicit delete of `pre-read:{path}` and `pre-write:{path}` keys |
| File-scoped (L2) | `pre-write` event | `DELETE FROM tool_cache WHERE file_path = ?` |
| File-scoped (L2) | On read | Compare `st_mtime_ns` — if file changed or deleted, delete row, return miss |
| Workspace (L2) | On read | TTL check — `WORKSPACE_TTL = 300s` (5 minutes); stale entries deleted on access |

### 5.5 Eviction

A background task runs every 600 seconds (10 minutes):

1. Delete entries with `last_hit_at` older than 24 hours (`MAX_AGE = 86400`)
2. If row count exceeds 10,000, keep only the 10,000 most-recently-hit rows
3. **Eviction is skipped when `permission_mode == "plan"`** to preserve cached context during planning sessions

---

## 6. MCP/LSP Bridge

### 6.1 MCP Protocol

The daemon communicates with the MCP server via **JSON-RPC 2.0 over stdio**, newline-delimited.

**Initialization handshake**:
1. Send `initialize` request with `protocolVersion: "2024-11-05"`, `capabilities: {}`, `clientInfo: {name: "lsp-hooks", version: "1.0.0"}`
2. Receive capability response
3. Send `notifications/initialized` notification (no params)

**Request multiplexing**: multiple concurrent `tools/call` requests can be in-flight simultaneously. Each is assigned an integer ID. A single reader loop reads stdout, parses JSON, and dispatches responses to the matching Future by ID. A write lock serializes all writes to stdin.

**Notifications** (responses without an `id` field) are silently dropped.

### 6.2 MCP Server Path Resolution

The MCP server binary is located in this order:
1. `$CLAUDE_PLUGIN_ROOT/node_modules/lsp-mcp-server/dist/index.js`
2. `<script_directory>/node_modules/lsp-mcp-server/dist/index.js`
3. Fallback: `npx lsp-mcp-server`

### 6.3 MCP Tools Available

The MCP server exposes 24 tools. The daemon uses a subset:

| Tool | Used By | Purpose |
|---|---|---|
| `lsp_document_symbols` | pre-read, pre-write | Hierarchical symbol tree for a file |
| `lsp_diagnostics` | pre-read, pre-bash | Errors/warnings for a specific file |
| `lsp_file_exports` | pre-read, pre-write | Public API surface of a file |
| `lsp_smart_search` | pre-write | One-shot: hover + definition + refs + calls + implementations |
| `lsp_find_symbol` | pre-grep, pre-glob, prompt | Find symbol by name (no file/position needed) |
| `lsp_workspace_symbols` | pre-glob, session-start | Fuzzy symbol search across project |
| `lsp_workspace_diagnostics` | pre-bash | All errors/warnings across project |

**Full tool inventory** (available but not all used by default handlers):
`lsp_goto_definition`, `lsp_goto_type_definition`, `lsp_find_references`, `lsp_find_implementations`, `lsp_hover`, `lsp_signature_help`, `lsp_document_symbols`, `lsp_workspace_symbols`, `lsp_file_exports`, `lsp_file_imports`, `lsp_related_files`, `lsp_diagnostics`, `lsp_workspace_diagnostics`, `lsp_completions`, `lsp_rename`, `lsp_code_actions`, `lsp_call_hierarchy`, `lsp_type_hierarchy`, `lsp_format_document`, `lsp_smart_search`, `lsp_find_symbol`, `lsp_server_status`, `lsp_start_server`, `lsp_stop_server`

### 6.4 LSP Server Auto-Start

Language servers are started on-demand when the MCP server receives a tool call for a file with a matching extension. Root pattern detection determines the workspace root:

| Language | Root patterns |
|---|---|
| TypeScript/JS | `tsconfig.json`, `jsconfig.json`, `package.json` |
| Python | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt` |
| Rust | `Cargo.toml` |
| Go | `go.mod`, `go.work` |
| C/C++ | `compile_commands.json`, `CMakeLists.txt`, `Makefile`, `.clangd` |

### 6.5 LSP Initialize Capabilities

The MCP server requests these LSP capabilities from language servers:
- Text document synchronization
- Completion (with snippet support)
- Hover, signature help
- Go-to definition, type definition, implementation, references
- Document symbols (hierarchical)
- Rename (with prepare support)
- Publish diagnostics (with related information)
- Code actions (literal kinds)
- Call hierarchy, type hierarchy
- Formatting

---

## 7. Version Management

### 7.1 Dual-Check Strategy

Two independent version checks catch different failure modes:

**Check 1 — File-based (every hook invocation)**:
1. Read `plugin.json` for `current_version`
2. Read VERSION file (`/tmp/lsp-hooks-<user>.version`) for `running_version`
3. If `current > running` → restart daemon (plugin was upgraded)
4. If `running > current` → keep existing daemon (downgrade protection)
5. If VERSION file missing but PID file older than 5 seconds → restart (handles legacy daemons)

**Check 2 — Socket-based (session-start only)**:
1. Send `{"method": "version"}` over the socket
2. Read response with 2-second timeout
3. If daemon version < current version, or daemon doesn't understand the `version` method → restart daemon
4. Reconnect with up to 3 retries (300ms delay between each)

**Why both?** The file-based check is fast (no socket overhead) and runs every invocation. The socket-based check is authoritative and catches stale cached hook code that predates the file-based check mechanism.

### 7.2 Daemon Restart Flow

1. Send SIGTERM to old daemon PID
2. Wait 300ms
3. Delete PID file, VERSION file, and socket file
4. Launch new daemon process (detached)
5. Wait 500ms for daemon to initialize
6. Reconnect to new socket

---

## 8. Symbol Extraction

### 8.1 Regex-to-Identifier (`_extract_symbol_candidates`)

Used by `pre-grep` to extract likely symbol names from grep regex patterns.

**Algorithm**:
1. If the entire pattern is a plain identifier (`[A-Za-z_]\w{2,}`), return it directly
2. Split on unescaped `|` (regex alternation)
3. For each part:
   - Strip: `\b`, `\w`, `\d`, `\s` and similar character class escapes
   - Strip: `^` and `$` anchors
   - Strip: `.*`, `.+`, `.?` quantified wildcards
   - Strip: `[...]` character classes
   - Strip: `(){}?+*` meta-characters
   - Unescape: `\X` → `X` for remaining escaped characters
4. If the cleaned result is a valid identifier of length >= 3, include it
5. Return up to 3 candidates

**Examples**:
- `handle_request` → `["handle_request"]`
- `fn\s+handle_request|process_request` → `["handle_request", "process_request"]`
- `\bUserService\b` → `["UserService"]`
- `.*` → `[]` (no valid identifier)

### 8.2 Glob-to-Identifier (`_extract_symbol_from_glob`)

Used by `pre-glob` to extract a symbol name from glob patterns.

**Algorithm**:
1. Take the last path segment (basename)
2. Remove the file extension (`\.\w+$`)
3. Remove glob wildcard characters (`*`, `?`, `[`, `]`)
4. If the result is a valid identifier of length >= 3, return it

**Examples**:
- `**/UserService*.ts` → `["UserService"]`
- `src/handlers/**/*.rs` → `[]` (basename `*.rs` yields empty string)
- `**/config.json` → `[]` (not a supported code extension / too short after stripping)

### 8.3 Prompt Symbol Detection

Used by the `prompt` handler to find symbols mentioned in natural language.

**Patterns detected**:
- Language keyword patterns: `fn X`, `struct X`, `trait X`, `impl X`, `enum X`, `mod X`
- PascalCase multi-word identifiers: `[A-Z][a-z]+(?:[A-Z][a-z]+)+` (e.g., `UserService`, `HttpClient`)
- File paths: regex for paths ending in supported extensions

---

## 9. Language Support

### 9.1 Supported Languages

| Language | Extensions | LSP Server | Install Command |
|---|---|---|---|
| Rust | `.rs` | `rust-analyzer` | `cargo install rust-analyzer` |
| TypeScript/JS | `.ts` `.tsx` `.js` `.jsx` `.mjs` `.cjs` | `typescript-language-server` | `npm install -g typescript-language-server` |
| Python | `.py` `.pyi` | `pylsp` | `uv tool install python-lsp-server` |
| C# | `.cs` | `csharp-ls` | `dotnet tool install --global csharp-ls` |
| C/C++ | `.c` `.h` `.cpp` `.hpp` `.cc` `.hh` `.cxx` `.hxx` | `clangd` | Xcode CLI tools or `brew install llvm` |
| Go | `.go` | `gopls` | `go install golang.org/x/tools/gopls@latest` |
| Ruby | `.rb` `.rake` `.gemspec` | `solargraph` | `gem install solargraph` |
| PHP | `.php` `.phtml` | `intelephense` | `npm install -g intelephense` |
| Elixir | `.ex` `.exs` `.heex` `.leex` | `elixir-ls` | `mix escript.install hex elixir_ls` |
| Kotlin | `.kt` `.kts` | `kotlin-lsp` | `brew install JetBrains/utils/kotlin-lsp` |
| Java | `.java` | `jls` | Build from source |

### 9.2 Extension Filter vs Server Support

The hook client's extension filter (`SUPPORTED_EXTENSIONS`) is narrower than what the MCP server supports. The client gates only `pre-read` and `pre-write` events. Languages not in the filter (Ruby, PHP, Elixir, Kotlin, Java) are still handled by the MCP server for other event types (grep, glob, prompt, session-start) and can be added to the filter as needed.

### 9.3 Adding a New Language

To add a new language:
1. Ensure the MCP server's language detector recognizes the extension
2. Add the extension to the hook client's `SUPPORTED_EXTENSIONS` set
3. Add an install check and command to the installer
4. Define root patterns for workspace detection

---

## 10. Error Handling

### 10.1 Philosophy

**Never block Claude Code. Never crash. Always exit 0.**

Every error path in the system leads to one of:
- Silent exit (hook client)
- Return `None` (daemon handler)
- Log and continue (daemon infrastructure)

### 10.2 Hook Client Error Handling

| Failure | Behavior |
|---|---|
| stdin parse error | Log to stderr, exit 0 |
| Unsupported extension | Exit 0 silently |
| Socket connect timeout (100ms) | Try to start daemon, retry once |
| Daemon not running | Auto-start daemon, wait 500ms, retry |
| Socket send/receive error | Exit 0 silently |
| Version mismatch | Restart daemon, retry up to 3 times |
| Any uncaught exception | Log to stderr, exit 0 |

### 10.3 Daemon Error Handling

| Failure | Behavior |
|---|---|
| MCP tool call error | Return `None`, log warning |
| MCP process death | Watchdog restarts it within 5 seconds |
| Handler exception | Return `{"ok": false, "error": "..."}` |
| Client disconnect mid-request | Log at DEBUG level, continue |
| SQLite error (any operation) | Log warning, return `None` / skip operation |
| Response write failure | Swallow silently |

### 10.4 Timeout Strategy

| Timeout | Value | Purpose |
|---|---|---|
| Hook timeout (Claude Code) | 1000-3000ms | Kills the hook client process |
| Socket connect | 100ms | Fail fast if daemon is down |
| Gather timeout | 4000ms | Cap parallel MCP calls, return partial results |
| MCP per-call | *(none)* | Deliberately removed — see [Lessons Learned](#12-lessons-learned) |
| Socket version check | 2000ms | Receive timeout for version query |
| Daemon restart wait | 500ms | Wait for new daemon to initialize |
| Reconnect retry delay | 300ms | Between reconnect attempts after restart |
| Handler drain | 2000ms | Wait for in-flight handlers during shutdown |
| MCP process termination | 5000ms | Wait before SIGKILL |
| Watchdog poll | 5000ms | Check if MCP process is alive |
| Cache eviction interval | 600000ms | Background eviction task |

---

## 11. Configuration

### 11.1 User Configuration

**File**: `~/.lsp-hooks/config.json` (optional)

**Schema with defaults**:
```json
{
  "lsp_mcp_server_path": "",
  "socket_path": "/tmp/lsp-hooks-<user>.sock",
  "pid_path": "/tmp/lsp-hooks-<user>.pid",
  "version_path": "/tmp/lsp-hooks-<user>.version",
  "limits": {
    "max_symbols_per_file": 10000,
    "max_callers_shown": 10000
  },
  "cache_ttl_seconds": 60
}
```

Merging: shallow merge for top-level keys, shallow merge for nested dicts (e.g., `limits`). User values override defaults.

### 11.2 Path Conventions

All runtime files use the system temp directory with user-namespaced filenames:

| File | Path | Lifecycle |
|---|---|---|
| Unix socket | `/tmp/lsp-hooks-<user>.sock` | Created on daemon start, deleted on clean exit |
| PID file | `/tmp/lsp-hooks-<user>.pid` | Created on daemon start, deleted on clean exit |
| Log file | `/tmp/lsp-hooks-<user>.log` | Appended, never deleted by daemon |
| Version file | `/tmp/lsp-hooks-<user>.version` | Created on daemon start, deleted on clean exit |
| SQLite cache | `~/.lsp-hooks/cache.db` | Persistent, never deleted by daemon |
| User config | `~/.lsp-hooks/config.json` | User-managed, optional |

**Design rationale**: runtime files go to `/tmp` (OS-managed, cleaned on reboot, no explicit cleanup needed). Persistent state goes to `~/.lsp-hooks/` (survives reboots). This avoids XDG complexity while providing natural lifecycle management.

### 11.3 Environment Variables

| Variable | Purpose |
|---|---|
| `CLAUDE_PLUGIN_ROOT` | Plugin directory path, set by Claude Code |
| `LSP_HOOKS_ACTIVE` | Recursion guard, set by daemon when auto-starting |

### 11.4 Plugin Manifest

**File**: `.claude-plugin/plugin.json`

```json
{
  "name": "lsp-hooks",
  "version": "<semver>",
  "description": "...",
  "author": { "name": "...", "url": "..." }
}
```

**Critical rule**: the version MUST be bumped on every change. Claude Code uses this version to decide whether to update its plugin cache. No version bump = no cache update for end users, even with `autoUpdate: true`.

---

## 12. Lessons Learned

### 12.1 Bug: hooks.json Schema Requires `"hooks"` Wrapper

**Problem**: the original `hooks.json` had event keys (`PreToolUse`, `UserPromptSubmit`) at the top level. Claude Code's plugin loader requires them nested under a `"hooks"` key.

**Symptom**: hooks were silently ignored — no errors, just no hook execution.

**Fix**: wrap all event registrations under `{"hooks": {...}}`.

**Takeaway**: always validate hook registration against Claude Code's expected schema. Silent failures are the worst kind.

### 12.2 Bug: Python 3.9 Compatibility

**Problem**: type annotations like `dict[str, list[str]]` and `str | None` are syntax errors on Python 3.9 (the documented minimum).

**Fix**: add `from __future__ import annotations` to defer annotation evaluation.

**Takeaway**: if a minimum Python version is declared, test on that version. Modern syntax is tempting but breaks older runtimes.

### 12.3 Bug: pip Install Fails on Homebrew Python

**Problem**: Homebrew Python enforces PEP 668 (externally-managed environment) and rejects bare `pip install`.

**Fix**: migrated from `pip` to `uv tool install` for Python LSP server installation. `uv` is now a hard prerequisite.

**Takeaway**: system Python package management is fragmented. Use isolated tools (`uv`, `pipx`) instead of raw `pip`.

### 12.4 Bug: VERSION File Race Condition

**Problem**: the daemon wrote the PID file before the VERSION file. If the hook client checked during the startup window, it would see a PID but no VERSION file and incorrectly restart the daemon.

**Fix**: reverse the write order — VERSION file first, PID file second.

**Takeaway**: when multiple sentinel files signal readiness, write them in dependency order. The file that others depend on must exist first.

### 12.5 Bug: marketplace.json Schema Mismatch

**Problem**: `marketplace.json` used `"author"` instead of `"owner"`, and was missing the `"category"` field.

**Symptom**: Claude Code's marketplace loader rejected the file.

**Fix**: update to match the required schema.

**Takeaway**: external schema compliance must be verified against the actual loader, not just documentation.

### 12.6 Design Decision: Remove Per-Call MCP Timeouts

**Problem**: per-method timeout budgets (e.g., `pre_write_ms: 2000`) caused MCP calls to be cancelled before their results could be cached. The next invocation would miss the cache and start the same slow call again — an infinite timeout loop.

**Solution**: remove all daemon-side timeouts. Let MCP calls run to completion. Results are always cached. Claude Code's hook timeout is the only latency bound.

**Result**: first invocation may exceed the hook timeout (hook client is killed, context is lost), but the daemon caches the result. Second invocation is an instant cache hit. This "warm-up" pattern is the core performance guarantee.

**Takeaway**: in a system with external timeouts (Claude Code kills the hook), internal timeouts are counterproductive if they prevent caching. Let the external timeout be the only enforcer; focus internal logic on ensuring work products are persisted.

### 12.7 Design Decision: Gather with Partial Results

**Problem**: `asyncio.gather` is all-or-nothing — if one task is slow, all results are delayed.

**Solution**: custom `_gather_partial` that returns completed results after a timeout, cancels pending tasks, and substitutes `None` for timed-out results.

**Takeaway**: in latency-sensitive systems where some data is better than no data, partial-result gathering is essential.

### 12.8 Design Decision: Dual Version Check

**Rationale**: the file-based check is fast but can be stale (old hook code might not have the check). The socket-based check is authoritative but adds latency. Running the file check on every invocation and the socket check only on `session-start` balances correctness against performance.

### 12.9 Design Decision: Cache Eviction Skipped in Plan Mode

**Rationale**: planning sessions can be long-running. Evicting cache entries during planning would force re-queries when the plan is executed. Skipping eviction preserves warm caches across the planning/execution boundary.

### 12.10 Gotcha: `Object` Symbol Kind

rust-analyzer reports `impl` blocks with the LSP symbol kind `Object`. The kind-to-label mapping must handle this explicitly (e.g., display as `"impl"` rather than `"Object"`).

### 12.11 Gotcha: Symbol Extraction is Heuristic

Both regex-to-identifier and glob-to-identifier extraction are best-effort. They can produce false positives (nonsense identifiers) or false negatives (miss valid symbols in complex patterns). Since all failures are swallowed silently and the context is advisory, this is acceptable.

### 12.12 Gotcha: Prompt Symbol Detection is Language-Biased

The prompt handler's keyword patterns (`fn`, `struct`, `trait`, `impl`, `enum`, `mod`) are Rust-specific. PascalCase detection is cross-language for class-style names. Expanding to other languages' keywords (e.g., `def`, `class`, `func`) would improve coverage.

---

## Appendix A: Wire Protocol Examples

### Hook Client → Daemon (query)

```json
{"method":"query","request_id":"a1b2c3d4","params":{"event":"pre-read","file_path":"/home/user/project/src/main.rs","tool_input":{"file_path":"/home/user/project/src/main.rs"},"cwd":"/home/user/project","permission_mode":"default"}}
```

### Daemon → Hook Client (success)

```json
{"ok":true,"context":"[LSP] Structure of src/main.rs:\n`main` (Function, L1)\n`Config` (Struct, L15)\n  `new` (Method, L20)"}
```

### Daemon → Hook Client (error)

```json
{"ok":false,"error":"MCP server not available"}
```

### Hook Client → Claude Code (stdout)

```json
{"continue":true,"systemMessage":"[LSP] Structure of src/main.rs:\n`main` (Function, L1)\n`Config` (Struct, L15)\n  `new` (Method, L20)"}
```

### Daemon → MCP Server (JSON-RPC 2.0)

```json
{"jsonrpc":"2.0","method":"tools/call","id":42,"params":{"name":"lsp_document_symbols","arguments":{"file_path":"/home/user/project/src/main.rs"}}}
```

---

## Appendix B: Formatting Conventions

All LSP context messages are prefixed with `[LSP]`.

**Symbol tree** (indented, backtick-quoted names):
```
`SymbolName` (Kind, L<line>)
  `child` (Kind, L<line>)
    `grandchild` (Kind, L<line>)
```

**Diagnostics**:
```
L<line>: <message>
  > <source line if available>
```

**References/callers/callees** (inline lists):
```
Called by: `name` in file.rs, `name2` in file2.rs
Calls: `name`, `name2`, `name3`
Referenced in N locations across M files
```

**Exports** (comma-separated):
```
Exports: `name` (Kind), `name2` (Kind)
```
