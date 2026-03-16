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

from lsp_hooks_paths import LOG_PATH, SOCKET_PATH, PID_PATH, VERSION_PATH, CACHE_DB_PATH
from lsp_hooks_cache import SQLiteCache

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,
    format="%(asctime)s [daemon] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lsp_hooks_daemon")

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="--------")


def _rid() -> str:
    return _request_id.get()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS = {
    "lsp_mcp_server_path": "",
    "socket_path": SOCKET_PATH,
    "pid_path": PID_PATH,
    "version_path": VERSION_PATH,
    "limits": {
        "max_symbols_per_file": 5,
        "max_callers_shown": 3,
        "max_references_shown": 3,
        "max_related_files_shown": 5,
    },
    "filters": {
        "supported_extensions": [
            ".rs", ".toml",
            ".py", ".pyi",
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            ".cs",
        ],
        "excluded_paths": ["target/", ".git/", "node_modules/"],
    },
    "cache_ttl_seconds": 30,
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
        fut = asyncio.get_event_loop().create_future()
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
        log.debug("[%s] MCP >>> %s(%s)", _rid(), tool_name, json.dumps(arguments, default=str)[:500])
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
            log.debug("[%s] MCP <<< %s OK (%.0fms, %d chars)", _rid(), tool_name, elapsed, len(raw_text))
            try:
                return json.loads(raw_text)
            except (json.JSONDecodeError, KeyError):
                return raw_text
        log.debug("[%s] MCP <<< %s OK (%.0fms, no text content)", _rid(), tool_name, elapsed)
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


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _rel(abs_path: str, cwd: str) -> str:
    try:
        return os.path.relpath(abs_path, cwd)
    except ValueError:
        return abs_path


def _fmt_symbol_list(symbols: list, limit: int = 5) -> str:
    items = []
    for s in symbols[:limit]:
        name = s.get("name", "?")
        kind = _display_kind(s.get("kind", ""))
        line = s.get("range", {}).get("start", {}).get("line", s.get("line", "?"))
        items.append(f"`{name}` ({kind}, L{line})")
    tail = f" ({len(symbols)} total)" if len(symbols) > limit else ""
    return ", ".join(items) + tail


def _fmt_callers(calls: list, limit: int = 3) -> str | None:
    if not calls:
        return None
    parts = []
    for c in calls[:limit]:
        fi = c.get("from", {})
        name = fi.get("name", "?")
        path = fi.get("uri", fi.get("path", ""))
        if "/" in path:
            path = path.rsplit("/", 1)[-1]
        parts.append(f"`{name}` in {path}")
    s = ", ".join(parts)
    if len(calls) > limit:
        s += f" ({len(calls)} total)"
    return s


def _fmt_callees(calls: list, limit: int = 3) -> str | None:
    if not calls:
        return None
    parts = [f"`{c.get('to', {}).get('name', '?')}`" for c in calls[:limit]]
    s = ", ".join(parts)
    if len(calls) > limit:
        s += f" ({len(calls)} total)"
    return s


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
    """Recursively flatten symbol tree into a flat list with depth info."""
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


def _fmt_symbol_tree(symbols: list, limit: int = 20) -> str:
    """Format symbols as an indented tree showing nesting (impl > methods).

    Modules are collected and shown as a single summary line at the top
    so they don't consume slots meant for real type/function symbols.
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

    # Modules as one-liner
    if modules:
        if len(modules) <= 6:
            lines.append(f"Modules: {', '.join(f'`{m}`' for m in modules)}")
        else:
            shown = ', '.join(f'`{m}`' for m in modules[:5])
            lines.append(f"Modules: {shown} ({len(modules)} total)")

    # Tree for the interesting symbols
    count = 0
    def _walk(syms, indent=0):
        nonlocal count
        for s in syms:
            if count >= limit:
                return
            count += 1
            name = s.get("name", "?")
            kind = _display_kind(s.get("kind", ""))
            ln = s.get("range", {}).get("start", {}).get("line", s.get("line", "?"))
            prefix = "  " * indent
            lines.append(f"{prefix}`{name}` ({kind}, L{ln})")
            children = s.get("children", [])
            if children:
                _walk(children, indent + 1)
    _walk(rest)
    if count >= limit:
        total_flat = len(_flatten_symbols(rest))
        if total_flat > limit:
            lines.append(f"  ... ({total_flat} symbols total)")
    return "\n".join(lines)


def _fmt_exports(exp_data, limit: int = 8) -> str | None:
    """Format exports, filtering out impl blocks/modules and showing kind."""
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
        # Skip impl blocks — already visible in symbol tree
        if n.startswith("impl "):
            continue
        # Skip modules and test modules — already shown in symbol tree
        if kind == "Module":
            continue
        names.append(f"`{n}` ({_display_kind(kind)})")
        if len(names) >= limit:
            break
    if not names:
        return None
    return f"Exports: {', '.join(names)}"


def _extract_list(data, *keys) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if k in data and isinstance(data[k], list):
                return data[k]
    return []


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
        self._last_permission_mode = "default"

    # -- lifecycle --

    async def start(self):
        pid_path = self.cfg["pid_path"]
        socket_path = self.cfg["socket_path"]

        with open(pid_path, "w") as f:
            f.write(str(os.getpid()))

        # Write current version so the hook client can detect upgrades
        try:
            plugin_json = Path(__file__).resolve().parent / ".claude-plugin" / "plugin.json"
            version = json.loads(plugin_json.read_text()).get("version", "unknown")
        except Exception:
            version = "unknown"
        version_path = self.cfg.get("version_path", "")
        if version_path:
            with open(version_path, "w") as f:
                f.write(version)
            log.info("wrote version %s to %s", version, version_path)

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
        await self.mcp.stop()
        self.sqlite_cache.close()
        for p in (self.cfg["socket_path"], self.cfg["pid_path"], self.cfg.get("version_path", "")):
            if os.path.exists(p):
                os.unlink(p)

    async def run(self):
        await self.start()
        loop = asyncio.get_event_loop()
        stop = asyncio.Event()

        def _sig():
            stop.set()

        for s in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(s, _sig)

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
                if self._last_permission_mode == "plan":
                    continue  # Never evict during plan mode
                try:
                    self.sqlite_cache.evict_stale()
                except Exception as e:
                    log.warning("cache eviction error: %s", e)

        wd = asyncio.create_task(_watchdog())
        ev = asyncio.create_task(_cache_evictor())
        await stop.wait()
        wd.cancel()
        ev.cancel()
        await self.cleanup()
        print("[lsp-hooks] stopped", file=sys.stderr)

    # -- socket handler --

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        t0 = time.monotonic()
        try:
            data = await reader.readline()
            if not data:
                return
            req = json.loads(data.decode().strip())
            _request_id.set(req.get("request_id", uuid.uuid4().hex[:8]))
            method = req.get("method")
            log.info("[%s] socket >>> method=%s params=%s", _rid(), method,
                     json.dumps(req.get("params", {}), default=str)[:500])
            if method == "ping":
                resp = {"ok": True, "pong": True}
            elif method == "query":
                resp = await self._dispatch(req.get("params", {}))
            else:
                resp = {"ok": False, "error": f"unknown method: {method}"}
            elapsed = (time.monotonic() - t0) * 1000
            ctx_len = len(resp.get("context", ""))
            log.info("[%s] socket <<< ok=%s context=%d chars (%.0fms): %s",
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

        if not self.mcp.is_alive():
            try:
                await self.mcp.start()
            except Exception as e:
                return {"ok": False, "error": f"MCP restart failed: {e}"}

        cache_key = f"{event}:{file_path}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            log.info("[%s] L1 cache HIT for %s", _rid(), cache_key)
            return {"ok": True, "context": cached}

        handlers = {
            "pre-read": self._h_pre_read,
            "pre-write": self._h_pre_write,
            "pre-bash": self._h_pre_bash,
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

    # --------------- handlers ---------------

    async def _h_pre_read(self, file_path: str, tool_input: dict, cwd: str) -> str:
        self.recent_reads.add(file_path)
        syms_r, diag_r, exp_r = await asyncio.gather(
            self._tc_cached("lsp_document_symbols", {"file_path": file_path},
                            file_path=file_path),
            self._tc_cached("lsp_diagnostics", {"file_path": file_path, "severity_filter": "error"},
                            file_path=file_path),
            self._tc_cached("lsp_file_exports", {"file_path": file_path},
                            file_path=file_path),
            return_exceptions=True,
        )
        syms_r = syms_r if not isinstance(syms_r, Exception) else None
        diag_r = diag_r if not isinstance(diag_r, Exception) else None
        exp_r = exp_r if not isinstance(exp_r, Exception) else None

        rel = _rel(file_path, cwd)
        lines: list[str] = [f"[LSP] Structure of {rel}:"]

        if syms_r:
            syms = _extract_symbols(syms_r)
            if syms:
                tree = _fmt_symbol_tree(syms, limit=20)
                if tree:
                    lines.append(tree)

        exp_line = _fmt_exports(exp_r)
        if exp_line:
            lines.append(exp_line)

        if diag_r:
            diag_list = _extract_list(diag_r, "diagnostics")
            if diag_list:
                lines.append(f"Diagnostics: {len(diag_list)} error(s)")
                for d in diag_list[:3]:
                    if isinstance(d, dict):
                        msg = d.get("message", str(d))[:100]
                        ln = d.get("range", {}).get("start", {}).get("line", d.get("line", "?"))
                        ctx = d.get("context", "")
                        line_str = f"  L{ln}: {msg}"
                        if ctx:
                            line_str += f"\n    > {ctx[:80]}"
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

        # Invalidate stale caches (L1 + L2)
        for ev in ("pre-read", "pre-write"):
            self.cache.invalidate(f"{ev}:{file_path}")
        self.sqlite_cache.invalidate_file(file_path)

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
        relevant = relevant[:max_sym]

        # Step 2 — smart search per symbol + exports (parallel)
        tasks: list = []
        for sym in relevant:
            # Use selection_range (the name) if available, else range start
            sel = sym.get("selection_range", sym.get("range", {}))
            ln = sel.get("start", {}).get("line", sym.get("line", 1))
            col = sel.get("start", {}).get("column", sym.get("column", 1))
            ln = max(ln, 1)
            col = max(col, 1)
            tasks.append(self._tc_cached("lsp_smart_search", {
                "file_path": file_path, "line": ln, "column": col,
                "include": ["hover", "references", "incoming_calls", "outgoing_calls", "implementations"],
                "references_limit": 10,
            }, file_path=file_path))
        tasks.append(self._tc_cached("lsp_file_exports", {"file_path": file_path},
                                     file_path=file_path))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        smart = [r if not isinstance(r, Exception) else None for r in results[:len(relevant)]]
        exp_data = results[len(relevant)] if not isinstance(results[len(relevant)], Exception) else None

        # Format
        rel_path = _rel(file_path, cwd)
        lines: list[str] = [f"[LSP] Structural context for {rel_path}:"]

        # Full symbol tree overview
        tree = _fmt_symbol_tree(top_syms, limit=15)
        if tree:
            lines.append(tree)
            lines.append("")

        # Per-symbol smart search details
        for sym, sr in zip(relevant, smart):
            if not sr or not isinstance(sr, dict):
                continue
            name = sym.get("name", "?")
            kind = sym.get("kind", "")
            ln = sym.get("range", {}).get("start", {}).get("line", sym.get("line", "?"))

            # Extract type signature from hover if available
            hover = sr.get("hover", {})
            sig = hover.get("contents", "") if isinstance(hover, dict) else ""
            sig_line = ""
            if sig:
                # Take first meaningful line of hover (usually the signature)
                for hl in sig.split("\n"):
                    hl = hl.strip()
                    if hl and not hl.startswith("---") and not hl.startswith("```"):
                        sig_line = f" — `{hl[:120]}`"
                        break

            lines.append(f"`{name}` ({kind}, L{ln}){sig_line}:")

            callers = _fmt_callers(sr.get("incoming_calls", []), lim["max_callers_shown"])
            if callers:
                lines.append(f"  Called by: {callers}")
            callees = _fmt_callees(sr.get("outgoing_calls", []), lim["max_callers_shown"])
            if callees:
                lines.append(f"  Calls: {callees}")

            impls = sr.get("implementations")
            if impls and isinstance(impls, dict):
                impl_items = impls.get("items", [])
                if impl_items:
                    impl_names = ", ".join(
                        f"`{i.get('context', i.get('path', '?')).rsplit('/', 1)[-1]}`"
                        for i in impl_items[:3]
                    )
                    lines.append(f"  Implementations: {impl_names}")

            refs = _fmt_refs(sr.get("references"))
            if refs:
                lines.append(f"  {refs}")

        if exp_data:
            exp_list = _extract_list(exp_data, "exports")
            if exp_list:
                names = []
                for exp in exp_list[:8]:
                    if isinstance(exp, dict):
                        n = exp.get("name", str(exp))
                        s = exp.get("signature", "")
                        names.append(f"`{n}`" + (f" — `{s}`" if s else ""))
                    else:
                        names.append(f"`{exp}`")
                lines.append(f"Exports: {', '.join(names)}")

        return "\n".join(lines) if len(lines) > 1 else ""

    async def _h_pre_bash(self, _file_path: str, tool_input: dict, cwd: str) -> str:
        command = tool_input.get("command", "")
        if not re.search(r"cargo\s+(build|test|check|clippy|run|bench)|npm\s+(run|test|build)|npx\s+tsc|pytest|python\s+-m\s+(pytest|unittest)|dotnet\s+(build|test|run)", command):
            return ""

        if self.recent_writes:
            tasks = [
                self._tc_cached("lsp_diagnostics", {"file_path": fp, "severity_filter": "error"},
                                file_path=fp)
                for fp in self.recent_writes[-5:]
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            lines = ["[LSP] Pre-build diagnostics:"]
            has = False
            for fp, res in zip(self.recent_writes[-5:], results):
                if isinstance(res, Exception) or not res:
                    continue
                dl = _extract_list(res, "diagnostics")
                if dl:
                    has = True
                    r = _rel(fp, cwd)
                    for d in dl[:3]:
                        if isinstance(d, dict):
                            msg = d.get("message", str(d))[:80]
                            ln = d.get("range", {}).get("start", {}).get("line", d.get("line", "?"))
                            lines.append(f"  {r}:{ln}: {msg}")
            return "\n".join(lines) if has else ""

        # No recent writes — try workspace diagnostics
        wd = await self._tc_cached("lsp_workspace_diagnostics", {
            "severity_filter": "error", "limit": 10, "group_by": "file",
        }, file_path=None)
        if not wd:
            return ""
        items = _extract_list(wd, "diagnostics", "items")
        if not items:
            return ""
        lines = ["[LSP] Pre-build diagnostics:"]
        for item in items[:10]:
            if isinstance(item, dict):
                fp = item.get("file", item.get("path", ""))
                msg = item.get("message", str(item))[:80]
                ln = item.get("line", "?")
                lines.append(f"  {_rel(fp, cwd)}:{ln}: {msg}")
        return "\n".join(lines) if len(lines) > 1 else ""

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
        sym_pats = re.findall(r"(?:fn|struct|trait|impl|enum|mod)\s+(\w+)", prompt)
        pascal = re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", prompt)
        for n in sym_pats + pascal:
            if n not in seen:
                seen.add(n)
                entities.append(("symbol", n))

        if not entities:
            return ""

        lines = ["[LSP] Context for prompt:"]
        for etype, val in entities[:3]:
            try:
                if etype == "file":
                    res = await self._tc_cached("lsp_document_symbols", {"file_path": val},
                                                file_path=val)
                    if res:
                        syms = _extract_symbols(res)
                        if syms:
                            flat = _flatten_symbols(syms)
                            lines.append(f"  {_rel(val, cwd)}: {_fmt_symbol_list(flat, limit=8)}")
                else:
                    res = await self._tc_cached("lsp_find_symbol", {
                        "name": val,
                        "include": ["references", "incoming_calls", "outgoing_calls"],
                        "references_limit": 5,
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
                                c = _fmt_callers(ic, 3)
                                if c:
                                    lines.append(f"    Called by: {c}")
            except Exception:
                continue

        return "\n".join(lines) if len(lines) > 1 else ""

    async def _h_session_start(self, _file_path: str, _tool_input: dict, cwd: str) -> str:
        try:
            res = await self._tc_cached("lsp_workspace_symbols", {"query": " ", "limit": 20},
                                        file_path=None)
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
        for kind in ("Struct", "Class", "Interface", "Trait", "Enum", "Function", "Module"):
            names = by_kind.get(kind, [])
            if names:
                display = ", ".join(f"`{n}`" for n in names[:5])
                if len(names) > 5:
                    display += f" ({len(names)} total)"
                lines.append(f"  {kind}s: {display}")

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
