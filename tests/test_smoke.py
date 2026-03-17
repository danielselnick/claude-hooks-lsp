"""Smoke tests — black-box testing of the daemon via Unix socket.

Each test starts a daemon with a mock MCP server and communicates
exclusively through the socket protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
import pytest_asyncio

from conftest import send_request, send_query

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(30),
]


# ---------------------------------------------------------------------------
# Protocol-level tests
# ---------------------------------------------------------------------------

class TestProtocol:
    """Basic protocol functionality — ping, version, unknown methods."""

    async def test_ping(self, running_daemon):
        resp = await send_request(running_daemon["socket_path"], "ping")
        assert resp["ok"] is True
        assert resp.get("pong") is True

    async def test_version(self, running_daemon):
        resp = await send_request(running_daemon["socket_path"], "version")
        assert resp["ok"] is True
        assert "version" in resp
        # Should be a semver-like string
        v = resp["version"]
        assert isinstance(v, str)
        parts = v.split(".")
        assert len(parts) >= 2

    async def test_unknown_method(self, running_daemon):
        resp = await send_request(running_daemon["socket_path"], "bogus")
        assert resp["ok"] is False
        assert "error" in resp
        assert "unknown" in resp["error"].lower() or "bogus" in resp["error"].lower()

    async def test_response_is_valid_json_with_ok(self, running_daemon):
        """Every response must have an 'ok' field and be valid JSON."""
        socket_path = running_daemon["socket_path"]

        for method in ["ping", "version", "bogus"]:
            resp = await send_request(socket_path, method)
            assert "ok" in resp, f"Response for {method} missing 'ok' field"
            assert isinstance(resp["ok"], bool)


# ---------------------------------------------------------------------------
# Event handler smoke tests
# ---------------------------------------------------------------------------

class TestPreRead:
    """Pre-read event handler."""

    async def test_pre_read_returns_context(self, running_daemon, tmp_project):
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-read",
            file_path=str(tmp_project / "main.py"),
            tool_input={"file_path": str(tmp_project / "main.py")},
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True
        context = resp.get("context", "")
        assert context, "pre-read should return non-empty context"
        assert "[LSP]" in context

    async def test_pre_read_with_range(self, running_daemon, tmp_project):
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-read",
            file_path=str(tmp_project / "main.py"),
            tool_input={
                "file_path": str(tmp_project / "main.py"),
                "offset": 1,
                "limit": 5,
            },
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True


class TestPreWrite:
    """Pre-write event handler."""

    async def test_pre_write_returns_context(self, running_daemon, tmp_project):
        fp = str(tmp_project / "main.py")
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-write",
            file_path=fp,
            tool_input={
                "file_path": fp,
                "old_string": "def main():",
                "new_string": "def main(args):",
            },
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True
        context = resp.get("context", "")
        assert context, "pre-write should return non-empty context"
        assert "[LSP]" in context


class TestPreBash:
    """Pre-bash event handler."""

    async def test_pre_bash_build_command(self, running_daemon, tmp_project):
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-bash",
            tool_input={"command": "cargo build"},
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True
        # Build commands should produce diagnostics context
        # (may be empty if no recent writes, but should not error)

    async def test_pre_bash_non_build_command(self, running_daemon, tmp_project):
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-bash",
            tool_input={"command": "ls -la"},
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True
        context = resp.get("context", "")
        # Non-build commands should return empty context
        assert context == ""


class TestPreGrep:
    """Pre-grep event handler."""

    async def test_pre_grep_with_symbol(self, running_daemon, tmp_project):
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-grep",
            file_path=str(tmp_project),
            tool_input={"pattern": "MyClass", "path": str(tmp_project)},
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True


class TestPreGlob:
    """Pre-glob event handler."""

    async def test_pre_glob(self, running_daemon, tmp_project):
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-glob",
            file_path=str(tmp_project),
            tool_input={"pattern": "*.py", "path": str(tmp_project)},
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True


class TestPrompt:
    """Prompt event handler."""

    async def test_prompt_with_file_mention(self, running_daemon, tmp_project):
        fp = str(tmp_project / "main.py")
        resp = await send_query(
            running_daemon["socket_path"],
            event="prompt",
            tool_input={"user_prompt": f"Can you refactor {fp}?"},
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True


class TestSessionStart:
    """Session-start event handler."""

    async def test_session_start_returns_overview(self, running_daemon, tmp_project):
        resp = await send_query(
            running_daemon["socket_path"],
            event="session-start",
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True
        context = resp.get("context", "")
        assert context, "session-start should return non-empty context"
        assert "[LSP]" in context


class TestEmptyEvent:
    """Edge case: empty or missing event."""

    async def test_empty_event(self, running_daemon):
        resp = await send_query(
            running_daemon["socket_path"],
            event="",
            cwd="/tmp",
        )
        # Empty event may return ok=false (no handler) or ok=true with empty context
        assert "ok" in resp
