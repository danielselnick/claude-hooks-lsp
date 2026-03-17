"""Shared fixtures for lsp-hooks smoke, regression, and end-to-end tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid

import pytest
import pytest_asyncio

# Add project root to path so we can import daemon modules
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

MOCK_MCP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_mcp_server.py")


# ---------------------------------------------------------------------------
# Sample project fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """Create a temporary project directory with sample source files."""
    main_py = tmp_path / "main.py"
    main_py.write_text(
        'import os\nimport sys\n\ndef main():\n    x = 1\n    print(x)\n\n'
        'class MyClass:\n    def method_a(self):\n        pass\n'
    )
    utils_py = tmp_path / "utils.py"
    utils_py.write_text('def helper(arg):\n    return arg + 1\n')

    app_ts = tmp_path / "app.ts"
    app_ts.write_text('export function run(): void {\n  console.log("hello");\n}\n')

    empty_py = tmp_path / "empty.py"
    empty_py.write_text("")

    readme = tmp_path / "README.md"
    readme.write_text("# Test Project\n")

    # Create a node_modules dir with a file (for excluded path test)
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = {};")

    return tmp_path


# ---------------------------------------------------------------------------
# Daemon config fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def daemon_config(tmp_path):
    """Return an isolated daemon config dict using temp paths."""
    uid = uuid.uuid4().hex[:8]
    return {
        "lsp_mcp_server_path": MOCK_MCP_PATH,
        "socket_path": os.path.join(tempfile.gettempdir(), f"test-lsp-hooks-{uid}.sock"),
        "pid_path": os.path.join(tempfile.gettempdir(), f"test-lsp-hooks-{uid}.pid"),
        "version_path": os.path.join(tempfile.gettempdir(), f"test-lsp-hooks-{uid}.version"),
        "limits": {"max_symbols_per_file": 10000, "max_callers_shown": 10000},
        "cache_ttl_seconds": 60,
        "file_watcher": {"enabled": False, "batch_size": 4, "debounce_ms": 500},
    }


# ---------------------------------------------------------------------------
# MCPClient monkeypatch — use python3 instead of node
# ---------------------------------------------------------------------------

def _patch_mcp_start(monkeypatch):
    """Monkeypatch MCPClient.start to use python3 for .py mock servers.

    Instead of reimplementing start() internals (which diverges when MCPClient
    changes), we delegate to the real start() and only intercept subprocess
    creation to swap ``node`` for ``sys.executable``.
    """
    import unittest.mock
    import lsp_hooks_daemon as daemon_mod

    original_start = daemon_mod.MCPClient.start

    async def patched_start(self):
        if self.server_path.endswith(".py"):
            self.is_npx = False
            orig_exec = asyncio.create_subprocess_exec

            async def _exec(*args, **kw):
                args = list(args)
                # The real start() launches via node; replace with python
                if args and args[0] == "node":
                    args[0] = sys.executable
                return await orig_exec(*args, **kw)

            with unittest.mock.patch("asyncio.create_subprocess_exec", side_effect=_exec):
                await original_start(self)
        else:
            await original_start(self)

    monkeypatch.setattr(daemon_mod.MCPClient, "start", patched_start)


# ---------------------------------------------------------------------------
# Running daemon fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def running_daemon(daemon_config, monkeypatch, tmp_path):
    """Start a daemon with mock MCP, yield socket_path, teardown after test."""
    import lsp_hooks_daemon as daemon_mod
    from lsp_hooks_cache import SQLiteCache

    # Use isolated cache DB
    db_path = os.path.join(str(tmp_path), "test_cache.db")
    monkeypatch.setattr(daemon_mod, "CACHE_DB_PATH", db_path)

    _patch_mcp_start(monkeypatch)

    daemon = daemon_mod.LSPHooksDaemon(daemon_config)
    daemon.sqlite_cache = SQLiteCache(db_path)

    # run() handles start + background tasks + cleanup
    daemon_task = asyncio.create_task(daemon.run())

    # Wait for socket to be ready
    socket_path = daemon_config["socket_path"]
    for _ in range(50):  # up to 5 seconds
        if os.path.exists(socket_path):
            break
        await asyncio.sleep(0.1)
    else:
        daemon_task.cancel()
        try:
            await daemon_task
        except (asyncio.CancelledError, Exception):
            pass
        pytest.fail("Daemon socket never appeared")

    yield {
        "socket_path": socket_path,
        "daemon": daemon,
        "config": daemon_config,
        "db_path": db_path,
    }

    # Teardown — send SIGINT-like stop via the internal signal handler
    # Since run() listens on signal handlers, we can't easily trigger those in tests.
    # Instead, just cancel the task and let it clean up.
    daemon_task.cancel()
    try:
        await asyncio.wait_for(daemon_task, timeout=5.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    # Clean up files
    for p in (socket_path, daemon_config["pid_path"], daemon_config["version_path"]):
        if os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Socket client helper
# ---------------------------------------------------------------------------

async def send_request(socket_path: str, method: str, params: dict | None = None,
                       request_id: str | None = None, timeout: float = 10.0) -> dict:
    """Send a request to the daemon and return the parsed response."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        req = {
            "method": method,
            "request_id": request_id or uuid.uuid4().hex[:8],
        }
        if params is not None:
            req["params"] = params
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return json.loads(data.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def send_query(socket_path: str, event: str, file_path: str = "",
                     tool_input: dict | None = None, cwd: str = "/tmp",
                     permission_mode: str = "default", **kwargs) -> dict:
    """Send a query request to the daemon."""
    params = {
        "event": event,
        "file_path": file_path,
        "tool_input": tool_input or {},
        "cwd": cwd,
        "permission_mode": permission_mode,
    }
    return await send_request(socket_path, "query", params, **kwargs)
