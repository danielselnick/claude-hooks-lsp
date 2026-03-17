#!/usr/bin/env python3
"""LSP Hooks Daemon — persistent process managing lsp-mcp-server over stdio."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path

import hashlib as _hashlib
import subprocess as _subprocess

from lsp_hooks_paths import LOG_PATH, SOCKET_PATH, PID_PATH, VERSION_PATH, CACHE_DB_PATH
from lsp_hooks_cache import SQLiteCache, _file_mtime_ns, _file_content_sha, CORE_TOOLS

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG if os.environ.get("LSP_HOOKS_VERBOSE", "") == "1" else logging.INFO,
    format="%(asctime)s [daemon] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lsp_hooks_daemon")

VERBOSE = os.environ.get("LSP_HOOKS_VERBOSE", "") == "1"

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="--------")


def _rid() -> str:
    return _request_id.get()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GATHER_TIMEOUT = 4.0  # seconds — max time to wait for parallel MCP calls
INGEST_TIMEOUT = 10.0  # seconds — more generous timeout for background ingestion

DEFAULTS = {
    "lsp_mcp_server_path": "",
    "socket_path": SOCKET_PATH,
    "pid_path": PID_PATH,
    "version_path": VERSION_PATH,
    "limits": {
        "max_symbols_per_file": 10000,
        "max_callers_shown": 10000,
    },
    "cache_ttl_seconds": 60,
    "file_watcher": {
        "enabled": True,
        "batch_size": 4,
        "debounce_ms": 500,
    },
}


def load_config():
    config = dict(DEFAULTS)
    config_path = Path.home() / ".lsp-hooks" / "config.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                user_cfg = json.load(f)
            for key, val in user_cfg.items():
                if isinstance(val, dict) and isinstance(config.get(key), dict):
                    config[key].update(val)
                else:
                    config[key] = val
        except Exception as e:
            print(f"[lsp-hooks] config load warning: {e}", file=sys.stderr)
    return config


def _resolve_mcp_server_path() -> tuple[str, bool]:
    """Resolve lsp-mcp-server path. Returns (path, is_npx).

    Search order:
    1. $CLAUDE_PLUGIN_ROOT/node_modules/lsp-mcp-server/dist/index.js
    2. <script_dir>/node_modules/lsp-mcp-server/dist/index.js
    3. npx lsp-mcp-server (fallback)
    """
    rel = os.path.join("node_modules", "lsp-mcp-server", "dist", "index.js")

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if plugin_root:
        candidate = os.path.join(plugin_root, rel)
        if os.path.isfile(candidate):
            return candidate, False

    script_dir = str(Path(__file__).resolve().parent)
    candidate = os.path.join(script_dir, rel)
    if os.path.isfile(candidate):
        return candidate, False

    # npx fallback
    return "lsp-mcp-server", True


# ---------------------------------------------------------------------------
# MCP Client — newline-delimited JSON-RPC 2.0 over child stdio
# ---------------------------------------------------------------------------

class MCPClient:
    def __init__(self, server_path: str, is_npx: bool = False):
        self.server_path = server_path
        self.is_npx = is_npx
        self.process = None
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    async def start(self):
        # Cancel old reader if restarting
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        # Reject any pending requests from the old process
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("MCP server restarted"))
        self._pending.clear()

        if self.is_npx:
            cmd = ["npx", self.server_path]
        else:
            cmd = ["node", self.server_path]
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._next_id = 1
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._initialize()

    async def _initialize(self):
        resp = await self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "lsp-hooks", "version": "1.0.0"},
        })
        async with self._write_lock:
            await self._send_raw({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            })
        return resp

    # -- low-level IO --

    async def _send_raw(self, msg: dict):
        data = json.dumps(msg) + "\n"
        self.process.stdin.write(data.encode())
        await self.process.stdin.drain()

    async def _reader_loop(self):
        """Single reader that dispatches responses to waiting futures by ID."""
        buf = ""
        try:
            while True:
                chunk = await self.process.stdout.read(8192)
                if not chunk:
                    break
                buf += chunk.decode()
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg_id = msg.get("id")
                    if msg_id is None:
                        continue  # notification — skip
                    fut = self._pending.pop(msg_id, None)
                    if fut and not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("reader loop died: %s", e)
        finally:
            # Reject all remaining pending requests
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("MCP reader stopped"))
            self._pending.clear()

    async def _call(self, method: str, params: dict):
        req_id = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            async with self._write_lock:
                await self._send_raw({
                    "jsonrpc": "2.0", "id": req_id,
                    "method": method, "params": params,
                })
            return await fut
        finally:
            self._pending.pop(req_id, None)

    # -- public API --

    async def tools_call(self, tool_name: str, arguments: dict):
        t0 = time.monotonic()
        log.info("[%s] MCP >>> %s(%s)", _rid(), tool_name, json.dumps(arguments, default=str)[:500])
        resp = await self._call("tools/call", {
            "name": tool_name, "arguments": arguments,
        })
        elapsed = (time.monotonic() - t0) * 1000
        if "error" in resp:
            log.warning("[%s] MCP <<< %s ERROR (%.0fms): %s", _rid(), tool_name, elapsed,
                        json.dumps(resp["error"], default=str)[:300])
            return None
        content = resp.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            raw_text = content[0].get("text", "")
            log.info("[%s] MCP <<< %s OK (%.0fms, %d chars)", _rid(), tool_name, elapsed, len(raw_text))
            try:
                return json.loads(raw_text)
            except (json.JSONDecodeError, KeyError):
                return raw_text
        log.info("[%s] MCP <<< %s OK (%.0fms, no text content)", _rid(), tool_name, elapsed)
        return resp.get("result")

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def stop(self):
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()


# ---------------------------------------------------------------------------
# TTL Cache
# ---------------------------------------------------------------------------

class Cache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._store: dict = {}

    def get(self, key: str):
        entry = self._store.get(key)
        if entry and (time.monotonic() - entry[1]) < self.ttl:
            return entry[0]
        self._store.pop(key, None)
        return None

    def set(self, key: str, value):
        self._store[key] = (value, time.monotonic())

    def invalidate(self, key: str):
        self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str):
        """Remove all entries whose key starts with prefix."""
        to_del = [k for k in self._store if k.startswith(prefix)]
        for k in to_del:
            del self._store[k]


# ---------------------------------------------------------------------------
# File Watcher — OS-native via watchfiles
# ---------------------------------------------------------------------------

# Supported source extensions for ingestion
_SOURCE_EXTS = frozenset([
    ".rs", ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".cs", ".go", ".java", ".kt", ".swift", ".c", ".cpp", ".h", ".hpp",
    ".rb", ".lua", ".zig", ".toml", ".json",
])

# Directories to skip during enumeration/watching
_SKIP_DIRS = frozenset([
    "node_modules", ".git", "target", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".next", ".nuxt", "venv", ".venv",
    ".tox", ".eggs",
])


def _is_source_file(path: str) -> bool:
    """Check if a file path has a supported source extension."""
    _, ext = os.path.splitext(path)
    return ext.lower() in _SOURCE_EXTS


def _enumerate_files(cwd: str) -> list[str]:
    """Enumerate project source files. Uses git ls-files if in a git repo, else os.walk."""
    files = []
    try:
        result = _subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for rel in result.stdout.splitlines():
                rel = rel.strip()
                if not rel:
                    continue
                abs_path = os.path.join(cwd, rel)
                if _is_source_file(abs_path):
                    files.append(abs_path)
            return files
    except Exception:
        pass

    # Fallback: os.walk with exclusions
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.endswith(".egg-info")]
        for fname in filenames:
            abs_path = os.path.join(dirpath, fname)
            if _is_source_file(abs_path):
                files.append(abs_path)
    return files


class FileWatcher:
    """OS-native file watcher using watchfiles (Rust-backed)."""

    def __init__(self, cwd: str, change_queue: asyncio.Queue, debounce_ms: int = 500):
        self.cwd = cwd
        self._change_queue = change_queue
        self._debounce_ms = debounce_ms

    async def watch(self, stop: asyncio.Event):
        """Watch for file changes and enqueue them. Runs until stop is set."""
        try:
            from watchfiles import awatch, Change
        except ImportError:
            log.warning("watchfiles not installed, file watcher disabled")
            await stop.wait()
            return

        def _watch_filter(change: Change, path: str) -> bool:
            """Filter to only source files, excluding common non-source dirs."""
            if not _is_source_file(path):
                return False
            for skip in _SKIP_DIRS:
                if f"/{skip}/" in path or path.endswith(f"/{skip}"):
                    return False
            if ".egg-info/" in path or path.endswith(".egg-info"):
                return False
            return True

        try:
            async for changes in awatch(self.cwd, watch_filter=_watch_filter,
                                         stop_event=stop, debounce=self._debounce_ms,
                                         rust_timeout=5000):
                for change_type, path in changes:
                    await self._change_queue.put((change_type, path))
        except Exception as e:
            if not stop.is_set():
                log.warning("file watcher error: %s", e)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _rel(abs_path: str, cwd: str) -> str:
    try:
        return os.path.relpath(abs_path, cwd)
    except ValueError:
        return abs_path


def _fmt_symbol_list(symbols: list) -> str:
    items = []
    for s in symbols:
        name = s.get("name", "?")
        kind = _display_kind(s.get("kind", ""))
        line = s.get("range", {}).get("start", {}).get("line", s.get("line", "?"))
        items.append(f"`{name}` ({kind}, L{line})")
    return ", ".join(items)


def _fmt_callers(calls: list) -> str | None:
    if not calls:
        return None
    parts = []
    for c in calls:
        fi = c.get("from", {})
        name = fi.get("name", "?")
        path = fi.get("uri", fi.get("path", ""))
        if "/" in path:
            path = path.rsplit("/", 1)[-1]
        parts.append(f"`{name}` in {path}")
    return ", ".join(parts)


def _fmt_callees(calls: list) -> str | None:
    if not calls:
        return None
    parts = [f"`{c.get('to', {}).get('name', '?')}`" for c in calls]
    return ", ".join(parts)


def _fmt_refs(refs_data) -> str | None:
    if not refs_data or not isinstance(refs_data, dict):
        return None
    items = refs_data.get("items", [])
    total = refs_data.get("total_count", len(items))
    if not items:
        return None
    files = {r.get("path", "") for r in items}
    return f"Referenced in {total} locations across {len(files)} files"


def _extract_symbols(data) -> list:
    """Normalize various symbol response shapes into a list of top-level symbols."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("symbols", "items", "children"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


def _flatten_symbols(symbols: list) -> list:
    """Recursively flatten symbol tree into a flat list with depth info.

    Note: mutates each symbol dict in-place by adding ``_depth``.  This is
    intentional — the key is only used for indentation in ``_fmt_symbol_tree``
    and does not interfere with cache lookups or other consumers.
    """
    result = []
    def _walk(syms, depth=0):
        for s in syms:
            s["_depth"] = depth
            result.append(s)
            children = s.get("children", [])
            if children:
                _walk(children, depth + 1)
    _walk(symbols)
    return result


_KIND_LABELS = {"Object": "impl"}  # rust-analyzer maps impl blocks to Object


def _display_kind(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


def _filter_symbols_by_range(symbols: list, start: int, end: int) -> list:
    """Filter symbol tree to only symbols overlapping LSP line range [start, end] (0-indexed).

    Parent symbols are included if any child is in range, but only in-range children are kept.
    """
    result = []
    for s in symbols:
        sr = s.get("range", {})
        sym_start = sr.get("start", {}).get("line", 0)
        sym_end = sr.get("end", {}).get("line", sym_start)

        children = s.get("children", [])
        if children:
            filtered_children = _filter_symbols_by_range(children, start, end)
            if filtered_children:
                # Parent included with only in-range children
                copy = {k: v for k, v in s.items() if k != "children"}
                copy["children"] = filtered_children
                result.append(copy)
                continue

        # Leaf or childless parent: include if range overlaps
        if sym_start <= end and sym_end >= start:
            result.append(s)
    return result


def _fmt_symbol_tree(symbols: list) -> str:
    """Format symbols as an indented tree showing nesting (impl > methods).

    Modules are collected and shown as a single summary line at the top.
    No caps — shows every symbol.
    """
    # Partition top-level: modules vs everything else
    modules = []
    rest = []
    for s in symbols:
        if s.get("kind") == "Module":
            modules.append(s.get("name", "?"))
        else:
            rest.append(s)

    lines = []

    # Modules
    if modules:
        lines.append(f"Modules: {', '.join(f'`{m}`' for m in modules)}")

    # Full tree for all symbols
    def _walk(syms, indent=0):
        for s in syms:
            name = s.get("name", "?")
            kind = _display_kind(s.get("kind", ""))
            ln = s.get("range", {}).get("start", {}).get("line", s.get("line", "?"))
            prefix = "  " * indent
            lines.append(f"{prefix}`{name}` ({kind}, L{ln})")
            children = s.get("children", [])
            if children:
                _walk(children, indent + 1)
    _walk(rest)
    return "\n".join(lines)


def _fmt_exports(exp_data) -> str | None:
    """Format exports, filtering out impl blocks/modules and showing kind. No caps."""
    if not exp_data:
        return None
    exp_list = _extract_list(exp_data, "exports")
    if not exp_list:
        return None
    names = []
    for exp in exp_list:
        if not isinstance(exp, dict):
            continue
        n = exp.get("name", "")
        kind = exp.get("kind", "")
        if n.startswith("impl "):
            continue
        if kind == "Module":
            continue
        sig = exp.get("signature", "")
        entry = f"`{n}` ({_display_kind(kind)})"
        if sig:
            entry += f" — `{sig}`"
        names.append(entry)
    if not names:
        return None
    return f"Exports: {', '.join(names)}"


def _fmt_imports(import_data) -> str | None:
    """Format file imports. No caps."""
    if not import_data:
        return None
    imp_list = _extract_list(import_data, "imports")
    if not imp_list:
        return None
    names = []
    for imp in imp_list:
        if isinstance(imp, dict):
            names.append(imp.get("module", imp.get("name", str(imp))))
        else:
            names.append(str(imp))
    if not names:
        return None
    return f"Imports: {', '.join(f'`{n}`' for n in names)}"


def _fmt_related_files(related_data, cwd: str) -> str | None:
    """Format related files. No caps — shows all relationships."""
    if not related_data or not isinstance(related_data, dict):
        return None
    parts = []
    imported_by = related_data.get("imported_by", [])
    if imported_by:
        names = []
        for f in imported_by:
            if isinstance(f, dict):
                fp = f.get("path", f.get("file", ""))
            else:
                fp = str(f)
            if fp:
                names.append(f"`{os.path.basename(_rel(fp, cwd))}`")
        if names:
            parts.append(f"Imported by: {', '.join(names)}")
    imports = related_data.get("imports", [])
    if imports:
        names = []
        for f in imports:
            if isinstance(f, dict):
                fp = f.get("path", f.get("file", ""))
            else:
                fp = str(f)
            if fp:
                names.append(f"`{os.path.basename(_rel(fp, cwd))}`")
        if names:
            parts.append(f"Imports from: {', '.join(names)}")
    return "; ".join(parts) if parts else None


def _fmt_type_hierarchy(hierarchy_data) -> str | None:
    """Format type hierarchy (supertypes + subtypes). No caps."""
    if not hierarchy_data or not isinstance(hierarchy_data, dict):
        return None
    parts = []
    supertypes = hierarchy_data.get("supertypes", [])
    if supertypes:
        names = [f"`{s.get('name', '?')}`" if isinstance(s, dict) else f"`{s}`"
                 for s in supertypes]
        parts.append(f"Supertypes: {', '.join(names)}")
    subtypes = hierarchy_data.get("subtypes", [])
    if subtypes:
        names = [f"`{s.get('name', '?')}`" if isinstance(s, dict) else f"`{s}`"
                 for s in subtypes]
        parts.append(f"Subtypes: {', '.join(names)}")
    return "; ".join(parts) if parts else None


def _fmt_call_hierarchy(ch_data) -> str | None:
    """Format call hierarchy (incoming + outgoing calls)."""
    if not ch_data or not isinstance(ch_data, dict):
        return None
    parts = []
    incoming = ch_data.get("incoming", ch_data.get("incoming_calls", []))
    if incoming:
        callers = _fmt_callers(incoming)
        if callers:
            parts.append(f"Called by: {callers}")
    outgoing = ch_data.get("outgoing", ch_data.get("outgoing_calls", []))
    if outgoing:
        callees = _fmt_callees(outgoing)
        if callees:
            parts.append(f"Calls: {callees}")
    return "; ".join(parts) if parts else None


def _fmt_code_actions(ca_data) -> str | None:
    """Format available code actions (quickfixes, refactors)."""
    if not ca_data:
        return None
    actions = ca_data if isinstance(ca_data, list) else _extract_list(ca_data, "actions", "items")
    if not actions:
        return None
    parts = []
    for a in actions:
        if isinstance(a, dict):
            title = a.get("title", "?")
            kind = a.get("kind", "")
            parts.append(f"`{title}` ({kind})" if kind else f"`{title}`")
    return f"Code actions: {', '.join(parts)}" if parts else None


_SEVERITY_LABELS = {1: "Error", 2: "Warning", 3: "Info", 4: "Hint",
                    "error": "Error", "warning": "Warning", "info": "Info", "hint": "Hint"}


def _severity_label(sev) -> str:
    return _SEVERITY_LABELS.get(sev, str(sev))


def _extract_list(data, *keys) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if k in data and isinstance(data[k], list):
                return data[k]
    return []


async def _gather_partial(coros, timeout):
    """Like asyncio.gather(return_exceptions=True) but returns partial results on timeout."""
    tasks = [asyncio.create_task(c) for c in coros]
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:
        t.cancel()
    # Suppress CancelledError from cancelled tasks
    await asyncio.gather(*pending, return_exceptions=True)
    results = []
    for t in tasks:
        if t in done:
            try:
                results.append(t.result())
            except Exception:
                results.append(None)
        else:
            results.append(None)
    return results, len(pending)


def _extract_symbol_candidates(pattern: str) -> list[str]:
    """Extract likely symbol names from a grep regex pattern.

    Returns up to 3 candidate identifiers suitable for lsp_find_symbol queries.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    # 1. If whole pattern is a plain identifier, use it directly
    if re.fullmatch(r'[A-Za-z_]\w{2,}', pattern):
        return [pattern]

    # 2. Split on | (regex alternation) and try each part
    parts = re.split(r'(?<!\\)\|', pattern)
    for part in parts:
        # Strip common regex constructs: anchors, char classes, quantifiers, groups
        clean = part.strip()
        clean = re.sub(r'\\[bBdDwWsS]', '', clean)  # \b, \w, \d, etc.
        clean = re.sub(r'[\^$]', '', clean)            # anchors
        clean = re.sub(r'\.\*|\.\+|\.\?', '', clean)  # .*, .+, .?
        clean = re.sub(r'\[.*?\]', '', clean)          # char classes
        clean = re.sub(r'[(){}?+*]', '', clean)        # groups/quantifiers
        clean = re.sub(r'\\(.)', r'\1', clean)         # unescape literals
        clean = clean.strip()

        # Must be a valid identifier of length >= 3
        if re.fullmatch(r'[A-Za-z_]\w{2,}', clean) and clean not in seen:
            seen.add(clean)
            candidates.append(clean)

    return candidates[:3]


def _extract_symbol_from_glob(pattern: str) -> list[str]:
    """Extract potential symbol names from a file glob pattern.

    e.g. '**/UserService*.ts' -> ['UserService']
         'src/handlers/**/*.rs' -> []  (no symbol name)
    """
    candidates = []
    # Get the filename part (last path segment before extension)
    basename = pattern.rsplit("/", 1)[-1] if "/" in pattern else pattern
    # Remove extension
    name_part = re.sub(r'\.\w+$', '', basename)
    # Remove glob wildcards
    name_part = re.sub(r'[*?\[\]]', '', name_part)
    # Must be a valid identifier >= 3 chars
    if re.fullmatch(r'[A-Za-z_]\w{2,}', name_part):
        candidates.append(name_part)
    return candidates


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class LSPHooksDaemon:
    def __init__(self, config: dict):
        self.cfg = config
        server_path = config["lsp_mcp_server_path"]
        is_npx = False
        if not server_path or not os.path.isfile(server_path):
            server_path, is_npx = _resolve_mcp_server_path()
        log.info("MCP server: %s (npx=%s)", server_path, is_npx)
        self.mcp = MCPClient(server_path, is_npx=is_npx)
        self.cache = Cache(config["cache_ttl_seconds"])
        self.sqlite_cache = SQLiteCache(CACHE_DB_PATH)
        self.recent_writes: list[str] = []
        self.recent_reads: set[str] = set()
        self._server = None
        self._active_handlers: set = set()
        self._last_permission_mode = "default"
        self._version = "unknown"
        # File watcher state
        self._project_cwd: str | None = None
        self._cwd_event = asyncio.Event()
        self._change_queue: asyncio.Queue = asyncio.Queue()
        self._file_watcher: FileWatcher | None = None
        fw_cfg = config.get("file_watcher", {})
        self._fw_enabled = fw_cfg.get("enabled", True)
        self._fw_batch_size = fw_cfg.get("batch_size", 4)
        self._fw_debounce_ms = fw_cfg.get("debounce_ms", 500)
        self._ingest_locks: dict[str, asyncio.Lock] = {}
        self._initial_ingest_done = asyncio.Event()
        self._watch_task: asyncio.Task | None = None

    # -- lifecycle --

    async def start(self):
        pid_path = self.cfg["pid_path"]
        socket_path = self.cfg["socket_path"]

        # Read current version from plugin.json
        try:
            plugin_json = Path(__file__).resolve().parent / ".claude-plugin" / "plugin.json"
            self._version = json.loads(plugin_json.read_text()).get("version", "unknown")
        except Exception:
            self._version = "unknown"

        # Write VERSION first (eliminates race where PID exists but VERSION doesn't)
        version_path = self.cfg.get("version_path", "")
        if version_path:
            with open(version_path, "w") as f:
                f.write(self._version)
            log.info("wrote version %s to %s", self._version, version_path)

        # Write PID second
        with open(pid_path, "w") as f:
            f.write(str(os.getpid()))

        if os.path.exists(socket_path):
            os.unlink(socket_path)

        await self.mcp.start()
        log.info("MCP started (pid=%d)", self.mcp.process.pid)
        print(f"[lsp-hooks] MCP started (pid={self.mcp.process.pid})", file=sys.stderr)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=socket_path,
        )
        log.info("listening on %s", socket_path)
        print(f"[lsp-hooks] listening on {socket_path}", file=sys.stderr)

    async def cleanup(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._active_handlers:
            log.info("draining %d active handler(s)…", len(self._active_handlers))
            _done, pending = await asyncio.wait(self._active_handlers, timeout=2.0)
            if pending:
                log.warning("drain timeout: %d handler(s) still active", len(pending))
        await self.mcp.stop()
        self.sqlite_cache.close()
        for p in (self.cfg["socket_path"], self.cfg["pid_path"], self.cfg.get("version_path", "")):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    async def run(self):
        await self.start()
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        def _sig():
            stop.set()

        for s in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(s, _sig)

        try:
            async def _watchdog():
                while not stop.is_set():
                    if not self.mcp.is_alive():
                        print("[lsp-hooks] MCP died, restarting…", file=sys.stderr)
                        try:
                            await self.mcp.start()
                            print("[lsp-hooks] MCP restarted", file=sys.stderr)
                        except Exception as e:
                            print(f"[lsp-hooks] restart failed: {e}", file=sys.stderr)
                    await asyncio.sleep(5)

            async def _cache_evictor():
                while not stop.is_set():
                    await asyncio.sleep(600)  # 10 minutes
                    try:
                        self.sqlite_cache.evict_stale()
                    except Exception as e:
                        log.warning("cache eviction error: %s", e)

            wd = asyncio.create_task(_watchdog())
            ev = asyncio.create_task(_cache_evictor())
            # File watcher background tasks
            ig = asyncio.create_task(self._background_ingest(stop)) if self._fw_enabled else None
            cc = asyncio.create_task(self._change_consumer(stop)) if self._fw_enabled else None
            await stop.wait()
            wd.cancel()
            ev.cancel()
            if ig:
                ig.cancel()
            if cc:
                cc.cancel()
            wt = self._watch_task
            if wt:
                wt.cancel()
            all_tasks = [t for t in [wd, ev, ig, cc, wt] if t is not None]
            await asyncio.gather(*all_tasks, return_exceptions=True)
        finally:
            for s in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(s)
            await self.cleanup()
        print("[lsp-hooks] stopped", file=sys.stderr)

    # -- socket handler --

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self._active_handlers.add(task)
        t0 = time.monotonic()
        try:
            data = await reader.readline()
            if not data:
                return
            req = json.loads(data.decode().strip())
            _request_id.set(req.get("request_id", uuid.uuid4().hex[:8]))
            method = req.get("method")
            log.debug("[%s] socket >>> method=%s params=%s", _rid(), method,
                      json.dumps(req.get("params", {}), default=str)[:500])
            if method == "ping":
                resp = {"ok": True, "pong": True}
            elif method == "version":
                resp = {"ok": True, "version": self._version}
            elif method == "query":
                resp = await self._dispatch(req.get("params", {}))
            else:
                resp = {"ok": False, "error": f"unknown method: {method}"}
            elapsed = (time.monotonic() - t0) * 1000
            ctx_len = len(resp.get("context", ""))
            log.debug("[%s] socket <<< ok=%s context=%d chars (%.0fms): %s",
                      _rid(), resp.get("ok"), ctx_len, elapsed,
                      resp.get("context", resp.get("error", ""))[:300])
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except ConnectionResetError:
            elapsed = (time.monotonic() - t0) * 1000
            log.debug("[%s] client disconnected before response (%.0fms)", _rid(), elapsed)
        except Exception as e:
            log.exception("[%s] socket handler error: %s", _rid(), e)
            try:
                writer.write((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            self._active_handlers.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    # -- dispatch + caching --

    async def _dispatch(self, params: dict) -> dict:
        event = params.get("event", "")
        file_path = params.get("file_path", "")
        tool_input = params.get("tool_input", {})
        cwd = params.get("cwd", "")
        permission_mode = params.get("permission_mode", "default")

        self._last_permission_mode = permission_mode

        # Set project CWD for file watcher on first request
        if cwd and self._project_cwd is None:
            self._project_cwd = cwd
            self._cwd_event.set()
            log.info("project CWD set to %s", cwd)

        if not self.mcp.is_alive():
            try:
                await self.mcp.start()
            except Exception as e:
                return {"ok": False, "error": f"MCP restart failed: {e}"}

        # Include content hash when file_path is empty to avoid key collisions
        if file_path:
            # Include offset/limit in cache key for range-scoped pre-read
            r_offset = tool_input.get("offset")
            r_limit = tool_input.get("limit")
            if r_offset is not None or r_limit is not None:
                cache_key = f"{event}:{file_path}:{r_offset}:{r_limit}"
            else:
                cache_key = f"{event}:{file_path}"
        else:
            content_hash = _hashlib.md5(
                json.dumps(tool_input, sort_keys=True, default=str).encode()
            ).hexdigest()[:12]
            cache_key = f"{event}::{content_hash}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            log.debug("[%s] L1 cache HIT for %s", _rid(), cache_key)
            return {"ok": True, "context": cached}

        handlers = {
            "pre-read": self._h_pre_read,
            "pre-write": self._h_pre_write,
            "pre-bash": self._h_pre_bash,
            "pre-grep": self._h_pre_grep,
            "pre-glob": self._h_pre_glob,
            "prompt": self._h_prompt,
            "session-start": self._h_session_start,
        }
        handler = handlers.get(event)
        if not handler:
            return {"ok": False, "error": f"unknown event: {event}"}

        try:
            ctx = await handler(file_path, tool_input, cwd)
            if ctx:
                self.cache.set(cache_key, ctx)
                return {"ok": True, "context": ctx}
            return {"ok": True, "context": ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # -- safe MCP wrapper --

    async def _tc(self, tool: str, args: dict):
        try:
            return await self.mcp.tools_call(tool, args)
        except Exception as e:
            log.warning("[%s] _tc(%s) failed: %s", _rid(), tool, e)
            return None

    async def _tc_cached(self, tool: str, args: dict, file_path: str | None = None):
        cached = self.sqlite_cache.get(tool, args, file_path=file_path)
        if cached is not None:
            log.debug("[%s] sqlite HIT for %s", _rid(), tool)
            return cached

        result = await self._tc(tool, args)

        if result is not None:
            self.sqlite_cache.put(tool, args, result, file_path=file_path)

        return result

    # --------------- file watcher ingestion ---------------

    async def _ingest_file(self, file_path: str):
        """Pre-cache ALL available LSP data for a single file."""
        # Per-file lock to prevent concurrent ingestion of the same file
        # setdefault is atomic in CPython, avoiding the race where two callers
        # each create a different Lock for the same key.
        lock = self._ingest_locks.setdefault(file_path, asyncio.Lock())

        async with lock:
            mtime = _file_mtime_ns(file_path)
            sha = _file_content_sha(file_path)
            if mtime is None or sha is None:
                return  # file gone or unreadable

            # Per-phase status — only skip phases that actually finished
            status = self.sqlite_cache.get_ingest_status(file_path, mtime, sha)
            if status["phase1"] and status["phase2"] and status["phase3"]:
                return  # fully ingested

            # If version changed, invalidate stale entries and reset status
            if not status["phase1"] and not status["phase2"] and not status["phase3"]:
                self.sqlite_cache.invalidate_file(file_path)

            # Phase 1: file-level tools (no symbol positions needed)
            syms_r = diag_r = None
            if not status["phase1"]:
                (syms_r, diag_r, exp_r, imp_r, rel_r), _ = await _gather_partial([
                    self._tc_cached("lsp_document_symbols", {"file_path": file_path},
                                    file_path=file_path),
                    self._tc_cached("lsp_diagnostics", {"file_path": file_path, "severity_filter": "all"},
                                    file_path=file_path),
                    self._tc_cached("lsp_file_exports", {"file_path": file_path},
                                    file_path=file_path),
                    self._tc_cached("lsp_file_imports", {"file_path": file_path},
                                    file_path=file_path),
                    self._tc_cached("lsp_related_files", {"file_path": file_path, "relationship": "all"},
                                    file_path=file_path),
                ], timeout=INGEST_TIMEOUT)
                self.sqlite_cache.set_ingest_phase(file_path, mtime, sha, 1)
            else:
                # Phase 1 already done — retrieve cached symbols/diagnostics for Phase 2/3
                cached = self.sqlite_cache.get_all_for_file(file_path)
                syms_r = cached.get("lsp_document_symbols")
                diag_r = cached.get("lsp_diagnostics")

            # Phase 2: per-symbol tools — ALL symbols, flattened into one gather
            if not status["phase2"]:
                if syms_r:
                    all_syms = _flatten_symbols(_extract_symbols(syms_r))
                    sym_tasks = []
                    for sym in all_syms:
                        sel = sym.get("selection_range", sym.get("range", {}))
                        ln = max(sel.get("start", {}).get("line", sym.get("line", 1)), 1)
                        col = max(sel.get("start", {}).get("column", sym.get("column", 1)), 1)

                        sym_tasks.extend([
                            self._tc_cached("lsp_smart_search", {
                                "file_path": file_path, "line": ln, "column": col,
                                "include": ["definition", "references", "hover",
                                            "implementations", "incoming_calls", "outgoing_calls"],
                                "references_limit": 50,
                            }, file_path=file_path),
                            self._tc_cached("lsp_hover", {
                                "file_path": file_path, "line": ln, "column": col,
                            }, file_path=file_path),
                            self._tc_cached("lsp_call_hierarchy", {
                                "file_path": file_path, "line": ln, "column": col,
                                "direction": "both",
                            }, file_path=file_path),
                            self._tc_cached("lsp_find_references", {
                                "file_path": file_path, "line": ln, "column": col,
                                "include_declaration": True, "limit": 500,
                            }, file_path=file_path),
                            self._tc_cached("lsp_find_implementations", {
                                "file_path": file_path, "line": ln, "column": col,
                                "limit": 100,
                            }, file_path=file_path),
                            self._tc_cached("lsp_type_hierarchy", {
                                "file_path": file_path, "line": ln, "column": col,
                                "direction": "both",
                            }, file_path=file_path),
                            self._tc_cached("lsp_goto_definition", {
                                "file_path": file_path, "line": ln, "column": col,
                            }, file_path=file_path),
                            self._tc_cached("lsp_goto_type_definition", {
                                "file_path": file_path, "line": ln, "column": col,
                            }, file_path=file_path),
                            self._tc_cached("lsp_signature_help", {
                                "file_path": file_path, "line": ln, "column": col,
                            }, file_path=file_path),
                        ])
                    if sym_tasks:
                        _, n_pending = await _gather_partial(sym_tasks, timeout=INGEST_TIMEOUT)
                        if n_pending:
                            log.debug("_ingest_file: %d/%d sym tasks timed out for %s",
                                      n_pending, len(sym_tasks), file_path)
                        else:
                            self.sqlite_cache.set_ingest_phase(file_path, mtime, sha, 2)
                    else:
                        self.sqlite_cache.set_ingest_phase(file_path, mtime, sha, 2)
                else:
                    # No symbols → Phase 2 trivially done
                    self.sqlite_cache.set_ingest_phase(file_path, mtime, sha, 2)

            # Phase 3: code actions for diagnostic ranges
            if not status["phase3"]:
                if diag_r:
                    diag_list = _extract_list(diag_r, "diagnostics")
                    ca_tasks = []
                    for d in diag_list:
                        if not isinstance(d, dict):
                            continue
                        rng = d.get("range", {})
                        start = rng.get("start", {})
                        end = rng.get("end", {})
                        sl = max(start.get("line", 1), 1)
                        sc = max(start.get("column", 1), 1)
                        el = max(end.get("line", sl), 1)
                        ec = max(end.get("column", sc), 1)
                        ca_tasks.append(self._tc_cached("lsp_code_actions", {
                            "file_path": file_path,
                            "start_line": sl, "start_column": sc,
                            "end_line": el, "end_column": ec,
                            "kinds": ["quickfix", "refactor", "source.fixAll"],
                        }, file_path=file_path))
                    if ca_tasks:
                        _, n_pending = await _gather_partial(ca_tasks, timeout=INGEST_TIMEOUT)
                        if n_pending:
                            log.debug("_ingest_file: %d/%d code-action tasks timed out for %s",
                                      n_pending, len(ca_tasks), file_path)
                        else:
                            self.sqlite_cache.set_ingest_phase(file_path, mtime, sha, 3)
                    else:
                        self.sqlite_cache.set_ingest_phase(file_path, mtime, sha, 3)
                else:
                    # No diagnostics → Phase 3 trivially done
                    self.sqlite_cache.set_ingest_phase(file_path, mtime, sha, 3)

    async def _background_ingest(self, stop: asyncio.Event):
        """Background task: enumerate all project files and pre-cache LSP data."""
        try:
            # Wait for CWD to be known (set by first client request)
            try:
                await asyncio.wait_for(self._cwd_event.wait(), timeout=300)
            except asyncio.TimeoutError:
                log.warning("file watcher: no CWD received within 5 minutes, aborting ingestion")
                return

            cwd = self._project_cwd
            if not cwd:
                return

            # Brief delay to let LSP server warm up
            await asyncio.sleep(2.0)

            log.info("background ingestion: starting for %s", cwd)
            t0 = time.monotonic()

            files = await asyncio.get_running_loop().run_in_executor(
                None, _enumerate_files, cwd,
            )
            log.info("background ingestion: found %d source files", len(files))

            ingested = 0
            batch_size = self._fw_batch_size

            for i in range(0, len(files), batch_size):
                if stop.is_set():
                    break
                batch = files[i:i + batch_size]
                await _gather_partial(
                    [self._ingest_file(fp) for fp in batch],
                    timeout=INGEST_TIMEOUT * 2,
                )
                ingested += len(batch)
                if ingested % 50 == 0:
                    log.info("background ingestion: %d/%d files processed", ingested, len(files))
                # Small yield to avoid starving request handlers
                await asyncio.sleep(0.05)

            elapsed = time.monotonic() - t0
            log.info("background ingestion: done — %d files in %.1fs", ingested, elapsed)

            # Prune ingest locks that are no longer held
            self._ingest_locks = {k: v for k, v in self._ingest_locks.items() if v.locked()}

            # Start file watcher after initial ingestion
            if self._fw_enabled and not stop.is_set():
                self._file_watcher = FileWatcher(cwd, self._change_queue, self._fw_debounce_ms)
                self._watch_task = asyncio.create_task(self._file_watcher.watch(stop))
                log.info("file watcher: started for %s", cwd)
        finally:
            self._initial_ingest_done.set()

    async def _change_consumer(self, stop: asyncio.Event):
        """Consume file change events from the watcher, debounce, and re-ingest."""
        # Wait for initial ingestion to complete before processing changes
        await self._initial_ingest_done.wait()
        debounce_s = self._fw_debounce_ms / 1000.0

        while not stop.is_set():
            try:
                # Wait for first change
                change_type, path = await asyncio.wait_for(
                    self._change_queue.get(), timeout=5.0,
                )
            except asyncio.TimeoutError:
                continue

            # Debounce: collect more changes for a short window
            changed_paths: set[str] = {path}
            deadline = time.monotonic() + debounce_s
            while time.monotonic() < deadline:
                try:
                    _, p = await asyncio.wait_for(
                        self._change_queue.get(), timeout=max(0, deadline - time.monotonic()),
                    )
                    changed_paths.add(p)
                except asyncio.TimeoutError:
                    break

            # Invalidate L1 cache for changed files
            for fp in changed_paths:
                self.cache.invalidate_prefix(f"pre-read:{fp}")
                self.cache.invalidate_prefix(f"pre-write:{fp}")

            # Re-ingest changed files
            batch = [fp for fp in changed_paths if os.path.isfile(fp)]
            if batch:
                log.info("file watcher: re-ingesting %d changed file(s)", len(batch))
                await _gather_partial(
                    [self._ingest_file(fp) for fp in batch],
                    timeout=INGEST_TIMEOUT * 2,
                )
                # Prune ingest locks that are no longer held
                self._ingest_locks = {k: v for k, v in self._ingest_locks.items() if v.locked()}

    # --------------- handlers ---------------

    async def _h_pre_read(self, file_path: str, tool_input: dict, cwd: str) -> str:
        self.recent_reads.add(file_path)
        if len(self.recent_reads) > 50:
            # Discard arbitrary element to cap size
            self.recent_reads.pop()

        # Determine visible line range (Read offset/limit are 1-indexed)
        offset = tool_input.get("offset")  # 1-indexed start line
        limit = tool_input.get("limit")    # number of lines
        if offset is not None or limit is not None:
            start_1 = offset if offset is not None else 1
            # Convert to 0-indexed LSP lines
            lsp_start = start_1 - 1
            lsp_end = (start_1 + limit - 2) if limit is not None else None
        else:
            lsp_start = None
            lsp_end = None

        # Try pre-populated cache from file watcher first
        cached_data = self.sqlite_cache.get_all_for_file(file_path)
        if all(t in cached_data for t in CORE_TOOLS):
            syms_r = cached_data.get("lsp_document_symbols")
            diag_r = cached_data.get("lsp_diagnostics")
            exp_r = cached_data.get("lsp_file_exports")
            imp_r = cached_data.get("lsp_file_imports")
            rel_r = cached_data.get("lsp_related_files")
            log.debug("[%s] pre-read: served from file watcher cache", _rid())
        else:
            # Graceful degradation: fetch on-demand if not yet ingested
            (syms_r, diag_r, exp_r, imp_r, rel_r), n_pending = await _gather_partial([
                self._tc_cached("lsp_document_symbols", {"file_path": file_path},
                                file_path=file_path),
                self._tc_cached("lsp_diagnostics", {"file_path": file_path, "severity_filter": "all"},
                                file_path=file_path),
                self._tc_cached("lsp_file_exports", {"file_path": file_path},
                                file_path=file_path),
                self._tc_cached("lsp_file_imports", {"file_path": file_path},
                                file_path=file_path),
                self._tc_cached("lsp_related_files", {"file_path": file_path, "relationship": "all"},
                                file_path=file_path),
            ], timeout=GATHER_TIMEOUT)
            if n_pending:
                log.debug("[%s] pre-read: %d/5 MCP calls timed out", _rid(), n_pending)

        rel = _rel(file_path, cwd)
        range_suffix = f" (L{offset}-{offset + limit - 1})" if offset is not None and limit is not None else ""
        lines: list[str] = [f"[LSP] Structure of {rel}{range_suffix}:"]

        if syms_r:
            syms = _extract_symbols(syms_r)
            if syms and lsp_start is not None:
                syms = _filter_symbols_by_range(
                    syms, lsp_start,
                    lsp_end if lsp_end is not None else float("inf"),
                )
            if syms:
                tree = _fmt_symbol_tree(syms)
                if tree:
                    lines.append(tree)

        exp_line = _fmt_exports(exp_r)
        if exp_line:
            lines.append(exp_line)

        imp_line = _fmt_imports(imp_r)
        if imp_line:
            lines.append(imp_line)

        rel_line = _fmt_related_files(rel_r, cwd)
        if rel_line:
            lines.append(rel_line)

        # Hover + call hierarchy + signature for ALL visible symbols (no caps)
        if syms_r:
            # Reuse `syms` already extracted and range-filtered above
            visible = syms if syms else []
            flat = _flatten_symbols(visible) if visible else []
            hover_syms = [s for s in flat
                          if s.get("kind") in ("Function", "Method", "Struct", "Class",
                                                "Trait", "Interface", "Enum",
                                                "Constructor", "Property", "Field",
                                                "Constant", "Variable")]
            if hover_syms:
                hover_tasks = []
                ch_tasks = []
                sig_tasks = []
                for sym in hover_syms:
                    sel = sym.get("selection_range", sym.get("range", {}))
                    ln = max(sel.get("start", {}).get("line", sym.get("line", 1)), 1)
                    col = max(sel.get("start", {}).get("column", sym.get("column", 1)), 1)
                    hover_tasks.append(self._tc_cached("lsp_hover", {
                        "file_path": file_path, "line": ln, "column": col,
                    }, file_path=file_path))
                    ch_tasks.append(self._tc_cached("lsp_call_hierarchy", {
                        "file_path": file_path, "line": ln, "column": col,
                        "direction": "both",
                    }, file_path=file_path))
                    sig_tasks.append(self._tc_cached("lsp_signature_help", {
                        "file_path": file_path, "line": ln, "column": col,
                    }, file_path=file_path))
                all_tasks = hover_tasks + ch_tasks + sig_tasks
                all_results, _ = await _gather_partial(all_tasks, timeout=GATHER_TIMEOUT)
                n = len(hover_syms)
                hover_results = all_results[:n]
                ch_results = all_results[n:2*n]
                sig_results = all_results[2*n:3*n]

                hover_lines = []
                for sym, hr in zip(hover_syms, hover_results):
                    if hr and isinstance(hr, dict):
                        contents = hr.get("contents", "")
                        if contents:
                            for hl in contents.split("\n"):
                                hl = hl.strip()
                                if hl and not hl.startswith("---") and not hl.startswith("```"):
                                    hover_lines.append(f"  `{sym.get('name', '?')}` — `{hl}`")
                                    break
                if hover_lines:
                    lines.append("Hover:")
                    lines.extend(hover_lines)

                # Call hierarchy for visible symbols
                ch_lines = []
                for sym, ch in zip(hover_syms, ch_results):
                    ch_str = _fmt_call_hierarchy(ch)
                    if ch_str:
                        ch_lines.append(f"  `{sym.get('name', '?')}`: {ch_str}")
                if ch_lines:
                    lines.append("Call graph:")
                    lines.extend(ch_lines)

                # Signature help
                sig_lines = []
                for sym, sh in zip(hover_syms, sig_results):
                    if sh and isinstance(sh, dict):
                        sigs = sh.get("signatures", [])
                        for s in sigs:
                            label = s.get("label", "")
                            if label:
                                sig_lines.append(f"  `{sym.get('name', '?')}`: `{label}`")
                if sig_lines:
                    lines.append("Signatures:")
                    lines.extend(sig_lines)

        # Diagnostics — all severities, no caps
        if diag_r:
            diag_list = _extract_list(diag_r, "diagnostics")
            if diag_list and lsp_start is not None:
                diag_list = [
                    d for d in diag_list
                    if isinstance(d, dict) and
                    lsp_start <= d.get("range", {}).get("start", {}).get("line", d.get("line", 0)) <= (lsp_end if lsp_end is not None else float("inf"))
                ]
            if diag_list:
                lines.append(f"Diagnostics: {len(diag_list)} issue(s)")
                for d in diag_list:
                    if isinstance(d, dict):
                        msg = d.get("message", str(d))
                        ln = d.get("range", {}).get("start", {}).get("line", d.get("line", "?"))
                        sev = _severity_label(d.get("severity", ""))
                        ctx = d.get("context", "")
                        line_str = f"  [{sev}] L{ln}: {msg}"
                        if ctx:
                            line_str += f"\n    > {ctx}"
                        lines.append(line_str)

        return "\n".join(lines) if len(lines) > 1 else ""

    async def _h_pre_write(self, file_path: str, tool_input: dict, cwd: str) -> str:
        lim = self.cfg["limits"]
        max_sym = lim["max_symbols_per_file"]

        # Track file for bash handler
        if file_path not in self.recent_writes:
            self.recent_writes.append(file_path)
            if len(self.recent_writes) > 20:
                self.recent_writes.pop(0)

        # Invalidate L1 cache — L2 will be refreshed by file watcher after write
        for ev in ("pre-read", "pre-write"):
            self.cache.invalidate(f"{ev}:{file_path}")

        # Step 1 — symbols (flatten tree to reach methods inside impl blocks)
        syms_data = await self._tc_cached("lsp_document_symbols", {"file_path": file_path},
                                          file_path=file_path)
        if not syms_data:
            return ""
        top_syms = _extract_symbols(syms_data)
        if not top_syms:
            return ""
        all_flat = _flatten_symbols(top_syms)

        # Pick relevant symbols for smart_search
        # For Edit: match symbols whose name appears in old_string
        # For Write: pick most important symbols from flat list
        old_string = tool_input.get("old_string", "")
        if old_string:
            relevant = [s for s in all_flat if s.get("name", "") and s["name"] in old_string]
            if not relevant:
                # Fallback: top-level non-module symbols
                relevant = [s for s in all_flat if s.get("kind") not in ("Module",)]
        else:
            # Write: prioritize functions/methods/structs/traits from the flat list
            priority = {"Function": 0, "Method": 1, "Struct": 2, "Trait": 3, "Enum": 4, "Constant": 5}
            relevant = sorted(
                [s for s in all_flat if s.get("kind") in priority],
                key=lambda s: priority.get(s.get("kind", ""), 10),
            )
            if not relevant:
                relevant = all_flat
        # No cap — process all relevant symbols

        # Step 2 — smart search per symbol + exports + type hierarchy (parallel, no caps)
        tasks: list = []
        for sym in relevant:
            sel = sym.get("selection_range", sym.get("range", {}))
            ln = sel.get("start", {}).get("line", sym.get("line", 1))
            col = sel.get("start", {}).get("column", sym.get("column", 1))
            ln = max(ln, 1)
            col = max(col, 1)
            tasks.append(self._tc_cached("lsp_smart_search", {
                "file_path": file_path, "line": ln, "column": col,
                "include": ["definition", "references", "hover",
                            "implementations", "incoming_calls", "outgoing_calls"],
                "references_limit": 50,
            }, file_path=file_path))
        tasks.append(self._tc_cached("lsp_file_exports", {"file_path": file_path},
                                     file_path=file_path))
        tasks.append(self._tc_cached("lsp_file_imports", {"file_path": file_path},
                                     file_path=file_path))
        tasks.append(self._tc_cached("lsp_related_files",
                                     {"file_path": file_path, "relationship": "all"},
                                     file_path=file_path))
        # Type hierarchy for ALL class-like symbols (no cap)
        class_like_kinds = ("Class", "Struct", "Trait", "Interface", "Enum")
        hier_syms = [s for s in relevant if s.get("kind") in class_like_kinds]
        for sym in hier_syms:
            sel = sym.get("selection_range", sym.get("range", {}))
            ln = max(sel.get("start", {}).get("line", sym.get("line", 1)), 1)
            col = max(sel.get("start", {}).get("column", sym.get("column", 1)), 1)
            tasks.append(self._tc_cached("lsp_type_hierarchy", {
                "file_path": file_path, "line": ln, "column": col, "direction": "both",
            }, file_path=file_path))
        # Call hierarchy for ALL symbols
        for sym in relevant:
            sel = sym.get("selection_range", sym.get("range", {}))
            ln = max(sel.get("start", {}).get("line", sym.get("line", 1)), 1)
            col = max(sel.get("start", {}).get("column", sym.get("column", 1)), 1)
            tasks.append(self._tc_cached("lsp_call_hierarchy", {
                "file_path": file_path, "line": ln, "column": col, "direction": "both",
            }, file_path=file_path))

        results, n_pending = await _gather_partial(tasks, timeout=GATHER_TIMEOUT)
        if n_pending:
            log.debug("[%s] pre-write: %d/%d MCP calls timed out", _rid(), n_pending, len(tasks))
        n_rel = len(relevant)
        smart = results[:n_rel]
        exp_data = results[n_rel] if len(results) > n_rel else None
        imp_data = results[n_rel + 1] if len(results) > n_rel + 1 else None
        rel_files_data = results[n_rel + 2] if len(results) > n_rel + 2 else None
        hier_results = results[n_rel + 3:n_rel + 3 + len(hier_syms)]
        ch_results = results[n_rel + 3 + len(hier_syms):n_rel + 3 + len(hier_syms) + n_rel]

        # Format
        rel_path = _rel(file_path, cwd)
        lines: list[str] = [f"[LSP] Structural context for {rel_path}:"]

        # Full symbol tree overview — no limit
        tree = _fmt_symbol_tree(top_syms)
        if tree:
            lines.append(tree)
            lines.append("")

        # Per-symbol smart search details — all symbols
        for idx, (sym, sr) in enumerate(zip(relevant, smart)):
            if not sr or not isinstance(sr, dict):
                continue
            name = sym.get("name", "?")
            kind = sym.get("kind", "")
            ln = sym.get("range", {}).get("start", {}).get("line", sym.get("line", "?"))

            hover = sr.get("hover", {})
            sig = hover.get("contents", "") if isinstance(hover, dict) else ""
            sig_line = ""
            if sig:
                for hl in sig.split("\n"):
                    hl = hl.strip()
                    if hl and not hl.startswith("---") and not hl.startswith("```"):
                        sig_line = f" — `{hl}`"
                        break

            lines.append(f"`{name}` ({kind}, L{ln}){sig_line}:")

            # Definition location
            defn = sr.get("definition")
            if defn and isinstance(defn, dict):
                def_path = defn.get("path", "")
                def_line = defn.get("line", "?")
                if def_path:
                    lines.append(f"  Defined at: {_rel(def_path, cwd)}:{def_line}")

            callers = _fmt_callers(sr.get("incoming_calls", []))
            if callers:
                lines.append(f"  Called by: {callers}")
            callees = _fmt_callees(sr.get("outgoing_calls", []))
            if callees:
                lines.append(f"  Calls: {callees}")

            impls = sr.get("implementations")
            if impls and isinstance(impls, dict):
                impl_items = impls.get("items", [])
                if impl_items:
                    impl_names = ", ".join(
                        f"`{i.get('context', i.get('path', '?')).rsplit('/', 1)[-1]}`"
                        for i in impl_items
                    )
                    lines.append(f"  Implementations: {impl_names}")

            refs = _fmt_refs(sr.get("references"))
            if refs:
                lines.append(f"  {refs}")

            # Call hierarchy from dedicated tool
            if idx < len(ch_results):
                ch_str = _fmt_call_hierarchy(ch_results[idx])
                if ch_str and not callers and not callees:
                    lines.append(f"  {ch_str}")

        exp_line = _fmt_exports(exp_data)
        if exp_line:
            lines.append(exp_line)

        # Type hierarchy for all class-like symbols
        for sym, hr in zip(hier_syms, hier_results):
            th_line = _fmt_type_hierarchy(hr)
            if th_line:
                lines.append(f"  `{sym.get('name', '?')}`: {th_line}")

        imp_line = _fmt_imports(imp_data)
        if imp_line:
            lines.append(imp_line)

        rel_line = _fmt_related_files(rel_files_data, cwd)
        if rel_line:
            lines.append(rel_line)

        return "\n".join(lines) if len(lines) > 1 else ""

    async def _h_pre_bash(self, _file_path: str, tool_input: dict, cwd: str) -> str:
        command = tool_input.get("command", "")
        if not re.search(r"cargo\s+(build|test|check|clippy|run|bench)|npm\s+(run|test|build)|npx\s+tsc|pytest|python\s+-m\s+(pytest|unittest)|dotnet\s+(build|test|run)", command):
            return ""

        if self.recent_writes:
            recent = self.recent_writes[-5:]
            # Try file watcher cache first for per-file diagnostics
            per_file_results = []
            uncached_fps = []
            for fp in recent:
                cached_data = self.sqlite_cache.get_all_for_file(fp)
                diag = cached_data.get("lsp_diagnostics")
                if diag is not None:
                    per_file_results.append(diag)
                else:
                    uncached_fps.append((len(per_file_results), fp))
                    per_file_results.append(None)

            # Fetch uncached diagnostics + workspace diagnostics — all severities, max limits
            tasks = []
            for _, fp in uncached_fps:
                tasks.append(self._tc_cached("lsp_diagnostics", {"file_path": fp, "severity_filter": "all"},
                                             file_path=fp))
            tasks.append(self._tc_cached("lsp_workspace_diagnostics", {
                "severity_filter": "all", "limit": 200, "group_by": "file",
            }, file_path=None))
            results, n_pending = await _gather_partial(tasks, timeout=GATHER_TIMEOUT)
            if n_pending:
                log.debug("[%s] pre-bash: %d/%d calls timed out", _rid(), n_pending, len(tasks))
            for i, (idx, _fp) in enumerate(uncached_fps):
                per_file_results[idx] = results[i] if i < len(results) else None
            ws_diag = results[len(uncached_fps)] if len(results) > len(uncached_fps) else None
            lines = ["[LSP] Pre-build diagnostics:"]
            has = False
            for fp, res in zip(recent, per_file_results):
                if not res:
                    continue
                dl = _extract_list(res, "diagnostics")
                if dl:
                    has = True
                    r = _rel(fp, cwd)
                    for d in dl:
                        if isinstance(d, dict):
                            msg = d.get("message", str(d))
                            ln = d.get("range", {}).get("start", {}).get("line", d.get("line", "?"))
                            sev = _severity_label(d.get("severity", ""))
                            lines.append(f"  [{sev}] {r}:{ln}: {msg}")
            # Cross-file workspace diagnostics — no caps
            if ws_diag:
                ws_items = _extract_list(ws_diag, "diagnostics", "items")
                shown_files = set(recent)
                cross = [i for i in ws_items if isinstance(i, dict) and
                         i.get("file", i.get("path", "")) not in shown_files]
                if cross:
                    lines.append("Cross-file issues:")
                    for item in cross:
                        fp = item.get("file", item.get("path", ""))
                        msg = item.get("message", str(item))
                        ln = item.get("line", "?")
                        sev = _severity_label(item.get("severity", ""))
                        lines.append(f"  [{sev}] {_rel(fp, cwd)}:{ln}: {msg}")
                    has = True
            return "\n".join(lines) if has else ""

        # No recent writes — try workspace diagnostics
        wd = await self._tc_cached("lsp_workspace_diagnostics", {
            "severity_filter": "all", "limit": 200, "group_by": "file",
        }, file_path=None)
        if not wd:
            return ""
        items = _extract_list(wd, "diagnostics", "items")
        if not items:
            return ""
        lines = ["[LSP] Pre-build diagnostics:"]
        for item in items:
            if isinstance(item, dict):
                fp = item.get("file", item.get("path", ""))
                msg = item.get("message", str(item))
                ln = item.get("line", "?")
                sev = _severity_label(item.get("severity", ""))
                lines.append(f"  [{sev}] {_rel(fp, cwd)}:{ln}: {msg}")
        return "\n".join(lines) if len(lines) > 1 else ""

    async def _h_pre_grep(self, file_path: str, tool_input: dict, cwd: str) -> str:
        pattern = tool_input.get("pattern", "")
        if not pattern:
            return ""

        candidates = _extract_symbol_candidates(pattern)
        if not candidates:
            return ""

        lines = [f"[LSP] Symbol context for search `{pattern}`:"]

        for name in candidates:  # all candidates, no cap
            try:
                res = await self._tc_cached("lsp_find_symbol", {
                    "name": name,
                    "include": ["definition", "references", "hover",
                                "implementations", "incoming_calls", "outgoing_calls"],
                    "references_limit": 50,
                }, file_path=None)

                if not res or not isinstance(res, dict):
                    continue

                match = res.get("match", {})
                if not match:
                    continue

                sym_name = match.get("name", name)
                path = match.get("path", "")
                ln = match.get("line", "?")
                kind = match.get("kind", "")
                hover = match.get("hover", "")

                lines.append(f"  `{sym_name}` ({kind}, {_rel(path, cwd)}:{ln})")
                if hover:
                    for hl in hover.split("\n"):
                        hl = hl.strip()
                        if hl and not hl.startswith("---") and not hl.startswith("```"):
                            lines.append(f"    `{hl}`")
                            break

                refs = (res.get("references") or {})
                ref_items = refs.get("items", [])
                total = refs.get("total_count", len(ref_items))
                if total:
                    ref_files = {r.get("path", "") for r in ref_items}
                    lines.append(f"    {total} references across {len(ref_files)} files")

                impls = res.get("implementations")
                if impls and isinstance(impls, dict):
                    impl_items = impls.get("items", [])
                    if impl_items:
                        impl_names = ", ".join(
                            f"`{i.get('context', i.get('path', '?')).rsplit('/', 1)[-1]}`"
                            for i in impl_items
                        )
                        lines.append(f"    Implementations: {impl_names}")

                ic = res.get("incoming_calls", [])
                if ic:
                    callers = _fmt_callers(ic)
                    if callers:
                        lines.append(f"    Called by: {callers}")

                oc = res.get("outgoing_calls", [])
                if oc:
                    callees = _fmt_callees(oc)
                    if callees:
                        lines.append(f"    Calls: {callees}")

                # Type hierarchy for class-like symbols
                if kind in ("Class", "Struct", "Trait", "Interface", "Enum") and path:
                    th_data = await self._tc_cached("lsp_type_hierarchy", {
                        "file_path": path,
                        "line": max(ln if isinstance(ln, int) else 1, 1),
                        "column": max(match.get("column", 1), 1),
                        "direction": "both",
                    }, file_path=path)
                    th_line = _fmt_type_hierarchy(th_data)
                    if th_line:
                        lines.append(f"    {th_line}")
            except Exception:
                continue

        return "\n".join(lines) if len(lines) > 1 else ""

    async def _h_pre_glob(self, file_path: str, tool_input: dict, cwd: str) -> str:
        pattern = tool_input.get("pattern", "")
        search_path = file_path or cwd

        # Strategy 1: Extract symbol names from glob pattern
        candidates = _extract_symbol_from_glob(pattern)
        if candidates:
            lines = [f"[LSP] Symbol context for glob `{pattern}`:"]
            for name in candidates:  # all candidates, no cap
                try:
                    res = await self._tc_cached("lsp_find_symbol", {
                        "name": name,
                        "include": ["definition", "references", "hover",
                                    "implementations", "incoming_calls", "outgoing_calls"],
                        "references_limit": 50,
                    }, file_path=None)

                    if not res or not isinstance(res, dict):
                        continue
                    match = res.get("match", {})
                    if not match:
                        continue

                    sym_name = match.get("name", name)
                    path = match.get("path", "")
                    ln = match.get("line", "?")
                    kind = match.get("kind", "")
                    hover = match.get("hover", "")
                    lines.append(f"  `{sym_name}` ({kind}, {_rel(path, cwd)}:{ln})")
                    if hover:
                        for hl in hover.split("\n"):
                            hl = hl.strip()
                            if hl and not hl.startswith("---") and not hl.startswith("```"):
                                lines.append(f"    `{hl}`")
                                break

                    refs = (res.get("references") or {})
                    ref_items = refs.get("items", [])
                    total = refs.get("total_count", len(ref_items))
                    if total:
                        ref_files = {r.get("path", "") for r in ref_items}
                        lines.append(f"    {total} references across {len(ref_files)} files")

                    ic = res.get("incoming_calls", [])
                    if ic:
                        callers = _fmt_callers(ic)
                        if callers:
                            lines.append(f"    Called by: {callers}")

                    oc = res.get("outgoing_calls", [])
                    if oc:
                        callees = _fmt_callees(oc)
                        if callees:
                            lines.append(f"    Calls: {callees}")

                    # Related files
                    if path:
                        rel_data = await self._tc_cached("lsp_related_files", {
                            "file_path": path, "relationship": "all",
                        }, file_path=path)
                        rel_line = _fmt_related_files(rel_data, cwd)
                        if rel_line:
                            lines.append(f"    {rel_line}")
                except Exception:
                    continue

            if len(lines) > 1:
                return "\n".join(lines)

        # Strategy 2: If path is a specific subdirectory, show workspace symbols — max limit
        if search_path and search_path != cwd and os.path.isdir(search_path):
            rel_dir = _rel(search_path, cwd)
            try:
                res = await self._tc_cached("lsp_workspace_symbols",
                                             {"query": os.path.basename(search_path), "limit": 100},
                                             file_path=None)
                if res:
                    syms = _extract_symbols(res)
                    in_dir = [s for s in syms
                              if s.get("path", "").startswith(search_path)]
                    if in_dir:
                        lines = [f"[LSP] Symbols in {rel_dir}/:"]
                        for s in in_dir:
                            name = s.get("name", "?")
                            kind = s.get("kind", "?")
                            ln = s.get("line", "?")
                            lines.append(f"  `{name}` ({kind}, L{ln})")
                        return "\n".join(lines)
            except Exception:
                pass

        return ""

    async def _h_prompt(self, _file_path: str, tool_input: dict, cwd: str) -> str:
        prompt = tool_input.get("user_prompt", "")
        if not prompt:
            return ""

        entities: list[tuple[str, str]] = []
        seen: set[str] = set()

        # File paths (dedup + skip files already covered by pre-read)
        for fp in re.findall(r"[\w./\-]+\.(?:rs|toml|py|pyi|tsx?|jsx?|mjs|cjs|cs)\b", prompt):
            abs_fp = fp if os.path.isabs(fp) else os.path.join(cwd, fp)
            if abs_fp in seen or abs_fp in self.recent_reads:
                continue
            if os.path.exists(abs_fp):
                seen.add(abs_fp)
                entities.append(("file", abs_fp))
        # Symbol names
        sym_pats = re.findall(r"(?:fn|struct|trait|impl|enum|mod|def|class|func|type|interface|protocol)\s+(\w+)", prompt)
        pascal = re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", prompt)
        for n in sym_pats + pascal:
            if n not in seen:
                seen.add(n)
                entities.append(("symbol", n))

        if not entities:
            return ""

        lines = ["[LSP] Context for prompt:"]
        for etype, val in entities:
            try:
                if etype == "file":
                    # Try file watcher cache first
                    cached_data = self.sqlite_cache.get_all_for_file(val)
                    if "lsp_document_symbols" in cached_data and "lsp_file_imports" in cached_data:
                        res = cached_data["lsp_document_symbols"]
                        imp_data = cached_data["lsp_file_imports"]
                    else:
                        (res, imp_data), _ = await _gather_partial([
                            self._tc_cached("lsp_document_symbols", {"file_path": val},
                                            file_path=val),
                            self._tc_cached("lsp_file_imports", {"file_path": val},
                                            file_path=val),
                        ], timeout=GATHER_TIMEOUT)
                    if res:
                        syms = _extract_symbols(res)
                        if syms:
                            flat = _flatten_symbols(syms)
                            lines.append(f"  {_rel(val, cwd)}: {_fmt_symbol_list(flat)}")
                    imp_line = _fmt_imports(imp_data)
                    if imp_line:
                        lines.append(f"    {imp_line}")
                else:
                    res = await self._tc_cached("lsp_find_symbol", {
                        "name": val,
                        "include": ["definition", "references", "hover", "implementations", "incoming_calls", "outgoing_calls"],
                        "references_limit": 50,
                    }, file_path=None)
                    if res and isinstance(res, dict):
                        match = res.get("match", {})
                        if match:
                            name = match.get("name", val)
                            path = match.get("path", "")
                            ln = match.get("line", "?")
                            lines.append(f"  `{name}` at {_rel(path, cwd)}:{ln}")
                            total = (res.get("references") or {}).get("total_count", 0)
                            if total:
                                lines.append(f"    {total} references")
                            ic = res.get("incoming_calls", [])
                            if ic:
                                c = _fmt_callers(ic)
                                if c:
                                    lines.append(f"    Called by: {c}")
                            oc = res.get("outgoing_calls", [])
                            if oc:
                                c = _fmt_callees(oc)
                                if c:
                                    lines.append(f"    Calls: {c}")
                            hover = res.get("hover", {})
                            if hover:
                                content = hover.get("content", hover.get("contents", ""))
                                if isinstance(content, str) and content:
                                    lines.append(f"    Type: {content}")
                            impls = res.get("implementations", [])
                            if impls:
                                impl_names = [i.get("name", i.get("path", "?")) for i in impls if isinstance(i, dict)]
                                if impl_names:
                                    lines.append(f"    Implementations: {', '.join(f'`{n}`' for n in impl_names)}")
            except Exception:
                continue

        return "\n".join(lines) if len(lines) > 1 else ""

    async def _h_session_start(self, _file_path: str, _tool_input: dict, cwd: str) -> str:
        try:
            (res, ws_diag), _ = await _gather_partial([
                self._tc_cached("lsp_workspace_symbols", {"query": " ", "limit": 100},
                                file_path=None),
                self._tc_cached("lsp_workspace_diagnostics", {
                    "severity_filter": "all", "limit": 200, "group_by": "file",
                }, file_path=None),
            ], timeout=GATHER_TIMEOUT)
        except Exception:
            return "[LSP] Language server starting up, context available shortly"

        if not res:
            return "[LSP] Language server starting up, context available shortly"

        syms = _extract_symbols(res)
        if not syms:
            return ""

        lines = [f"[LSP] Project overview for {os.path.basename(cwd)}:"]
        by_kind: dict[str, list[str]] = {}
        for s in syms:
            by_kind.setdefault(s.get("kind", "Unknown"), []).append(s.get("name", "?"))
        for kind in ("Struct", "Class", "Interface", "Trait", "Enum", "Function", "Module",
                      "Variable", "Constant", "Property", "Field", "Method", "Constructor",
                      "Namespace", "Package", "TypeParameter", "Event", "Operator",
                      "EnumMember", "Key", "Array", "Object"):
            names = by_kind.get(kind, [])
            if names:
                display = ", ".join(f"`{n}`" for n in names)
                lines.append(f"  {kind}s: {display}")

        # Workspace health
        if ws_diag:
            ws_items = _extract_list(ws_diag, "diagnostics", "items")
            if ws_items:
                by_sev: dict[str, int] = {}
                for item in ws_items:
                    if isinstance(item, dict):
                        sev = _severity_label(item.get("severity", 1))
                        by_sev[sev] = by_sev.get(sev, 0) + 1
                sev_summary = ", ".join(f"{c} {s}" for s, c in by_sev.items())
                lines.append(f"  Diagnostics: {sev_summary}")
                for item in ws_items:
                    if isinstance(item, dict):
                        fp = item.get("file", item.get("path", ""))
                        msg = item.get("message", str(item))
                        ln = item.get("line", "?")
                        sev = _severity_label(item.get("severity", 1))
                        lines.append(f"    [{sev}] {_rel(fp, cwd)}:{ln}: {msg}")

        return "\n".join(lines) if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

async def main():
    config = load_config()
    daemon = LSPHooksDaemon(config)
    await daemon.run()


if __name__ == "__main__":
    asyncio.run(main())
