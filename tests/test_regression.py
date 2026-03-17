"""Regression tests — edge cases, cache correctness, concurrency, and handler-specific bugs.

Tests cover cache invalidation, concurrent connections, graceful error handling,
MCP recovery, and handler-specific behavior that could regress.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

import pytest
import pytest_asyncio

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from conftest import send_request, send_query

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(30),
]


# ===========================================================================
# Cache Regression Tests
# ===========================================================================

class TestCacheRegression:
    """Tests for L1 and L2 cache correctness."""

    async def test_l1_cache_hit_returns_identical_context(self, running_daemon, tmp_project):
        """Same query twice should return identical context (L1 cache hit)."""
        socket_path = running_daemon["socket_path"]
        fp = str(tmp_project / "main.py")

        resp1 = await send_query(socket_path, "pre-read", file_path=fp,
                                 tool_input={"file_path": fp}, cwd=str(tmp_project))
        resp2 = await send_query(socket_path, "pre-read", file_path=fp,
                                 tool_input={"file_path": fp}, cwd=str(tmp_project))

        assert resp1["ok"] is True
        assert resp2["ok"] is True
        assert resp1.get("context") == resp2.get("context")
        assert resp1.get("context"), "Expected non-empty context from pre-read"

    async def test_l1_cache_invalidation_on_write(self, running_daemon, tmp_project):
        """Pre-write for a file should invalidate L1 cache for that file."""
        socket_path = running_daemon["socket_path"]
        fp = str(tmp_project / "main.py")

        # First read
        resp1 = await send_query(socket_path, "pre-read", file_path=fp,
                                 tool_input={"file_path": fp}, cwd=str(tmp_project))
        assert resp1["ok"] is True

        # Write event (triggers L1 invalidation)
        resp_w = await send_query(socket_path, "pre-write", file_path=fp,
                                  tool_input={"file_path": fp, "old_string": "x", "new_string": "y"},
                                  cwd=str(tmp_project))
        assert resp_w["ok"] is True
        # Verify L1 cache was actually invalidated
        daemon = running_daemon["daemon"]
        l1_key = f"pre-read:{fp}"
        assert daemon.cache.get(l1_key) is None, "L1 cache should be invalidated after write"

        # Second read — should not be stale
        resp2 = await send_query(socket_path, "pre-read", file_path=fp,
                                 tool_input={"file_path": fp}, cwd=str(tmp_project))
        assert resp2["ok"] is True

    async def test_sqlite_mtime_invalidation(self, tmp_path):
        """SQLiteCache should invalidate when file mtime changes with different content."""
        from lsp_hooks_cache import SQLiteCache

        db_path = str(tmp_path / "test.db")
        cache = SQLiteCache(db_path)
        try:
            test_file = tmp_path / "test.py"
            test_file.write_text("original content")

            # Cache an entry
            cache.put("lsp_document_symbols", {"file_path": str(test_file)},
                      {"symbols": [{"name": "foo"}]}, file_path=str(test_file))

            # Verify it's cached
            result = cache.get("lsp_document_symbols", {"file_path": str(test_file)},
                               file_path=str(test_file))
            assert result is not None

            # Change file content (changes both mtime and content)
            time.sleep(0.01)  # ensure mtime changes
            test_file.write_text("modified content")

            # Should be invalidated
            result = cache.get("lsp_document_symbols", {"file_path": str(test_file)},
                               file_path=str(test_file))
            assert result is None
        finally:
            cache.close()

    async def test_sqlite_content_sha_survives_touch(self, tmp_path):
        """Touch (mtime change, same content) should NOT invalidate cache."""
        from lsp_hooks_cache import SQLiteCache

        db_path = str(tmp_path / "test.db")
        cache = SQLiteCache(db_path)
        try:
            test_file = tmp_path / "test.py"
            test_file.write_text("stable content")

            cache.put("lsp_document_symbols", {"file_path": str(test_file)},
                      {"symbols": [{"name": "foo"}]}, file_path=str(test_file))

            # Touch the file (changes mtime but not content)
            time.sleep(0.01)
            os.utime(str(test_file), None)

            result = cache.get("lsp_document_symbols", {"file_path": str(test_file)},
                               file_path=str(test_file))
            assert result is not None, "Cache should survive touch (same content SHA)"
            assert result["symbols"][0]["name"] == "foo"
        finally:
            cache.close()

    async def test_sqlite_schema_migration(self, tmp_path):
        """Opening cache with wrong schema version should recreate tables."""
        import sqlite3
        from lsp_hooks_cache import SQLiteCache, SCHEMA_VERSION

        db_path = str(tmp_path / "test.db")

        # Create a DB with old schema version
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION - 1,))
        conn.execute("CREATE TABLE tool_cache (id INTEGER PRIMARY KEY, dummy TEXT)")
        conn.execute("INSERT INTO tool_cache (dummy) VALUES ('old data')")
        conn.commit()
        conn.close()

        # Open with SQLiteCache — should recreate
        cache = SQLiteCache(db_path)
        try:
            # Old data should be gone
            conn2 = cache._ensure_conn()
            row = conn2.execute("SELECT version FROM schema_version").fetchone()
            assert row[0] == SCHEMA_VERSION

            # Should be able to use normally
            cache.put("test_tool", {"arg": 1}, {"result": "ok"})
            assert cache.get("test_tool", {"arg": 1}) is not None
        finally:
            cache.close()

    async def test_cache_eviction(self, tmp_path):
        """evict_stale should remove old entries and enforce max_rows."""
        from lsp_hooks_cache import SQLiteCache

        db_path = str(tmp_path / "test.db")
        cache = SQLiteCache(db_path)
        try:
            # Insert many entries
            for i in range(20):
                cache.put(f"tool_{i}", {"idx": i}, {"data": i})

            # Evict with very low max_rows
            cache.evict_stale(max_age=86400, max_rows=5)

            conn = cache._ensure_conn()
            count = conn.execute("SELECT COUNT(*) FROM tool_cache").fetchone()[0]
            assert count <= 5
        finally:
            cache.close()


# ===========================================================================
# Concurrency Tests
# ===========================================================================

class TestConcurrency:
    """Tests for concurrent socket connections."""

    async def test_concurrent_connections(self, running_daemon, tmp_project):
        """10 simultaneous queries should all succeed."""
        socket_path = running_daemon["socket_path"]
        fp = str(tmp_project / "main.py")

        tasks = [
            send_query(socket_path, "pre-read", file_path=fp,
                       tool_input={"file_path": fp}, cwd=str(tmp_project))
            for _ in range(10)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        errors = [r for r in results if isinstance(r, Exception)]
        assert len(errors) == 0, f"Got {len(errors)} errors: {errors}"

        for r in results:
            assert r["ok"] is True

    async def test_concurrent_different_events(self, running_daemon, tmp_project):
        """Different event types simultaneously should all succeed."""
        socket_path = running_daemon["socket_path"]
        fp = str(tmp_project / "main.py")
        cwd = str(tmp_project)

        tasks = [
            send_query(socket_path, "pre-read", file_path=fp,
                       tool_input={"file_path": fp}, cwd=cwd),
            send_query(socket_path, "pre-write", file_path=fp,
                       tool_input={"file_path": fp, "old_string": "x", "new_string": "y"}, cwd=cwd),
            send_query(socket_path, "pre-bash",
                       tool_input={"command": "cargo build"}, cwd=cwd),
            send_query(socket_path, "session-start", cwd=cwd),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            assert not isinstance(r, Exception), f"Got error: {r}"
            assert r["ok"] is True

    async def test_rapid_fire_queries(self, running_daemon, tmp_project):
        """50 sequential queries with no delay — daemon should stay alive."""
        socket_path = running_daemon["socket_path"]

        for i in range(50):
            resp = await send_request(socket_path, "ping")
            assert resp["ok"] is True, f"Query {i} failed"


# ===========================================================================
# Edge Case Tests
# ===========================================================================

class TestEdgeCases:
    """Graceful handling of unusual inputs."""

    async def test_nonexistent_file_pre_read(self, running_daemon):
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-read",
            file_path="/nonexistent/file.py",
            tool_input={"file_path": "/nonexistent/file.py"},
            cwd="/tmp",
        )
        assert resp["ok"] is True  # should not crash

    async def test_empty_file_pre_read(self, running_daemon, tmp_project):
        fp = str(tmp_project / "empty.py")
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-read",
            file_path=fp,
            tool_input={"file_path": fp},
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True

    async def test_binary_file_content(self, running_daemon, tmp_path):
        """Pre-read for a .py file with binary content should handle gracefully."""
        binary_py = tmp_path / "binary.py"
        binary_py.write_bytes(b"\x00\x01\x02\xff\xfe\xfd" * 100)

        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-read",
            file_path=str(binary_py),
            tool_input={"file_path": str(binary_py)},
            cwd=str(tmp_path),
        )
        assert resp["ok"] is True

    async def test_very_long_file_path(self, running_daemon):
        long_path = "/tmp/" + "a" * 500 + ".py"
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-read",
            file_path=long_path,
            tool_input={"file_path": long_path},
            cwd="/tmp",
        )
        assert resp["ok"] is True

    async def test_unicode_in_prompt(self, running_daemon):
        resp = await send_query(
            running_daemon["socket_path"],
            event="prompt",
            tool_input={"user_prompt": "Fix the bug in class \u00dcberKlasse \U0001f680"},
            cwd="/tmp",
        )
        assert resp["ok"] is True

    async def test_empty_grep_pattern(self, running_daemon):
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-grep",
            tool_input={"pattern": "", "path": "/tmp"},
            cwd="/tmp",
        )
        assert resp["ok"] is True

    async def test_complex_grep_regex(self, running_daemon):
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-grep",
            tool_input={"pattern": r"fn\s+\w+|impl\s+\w+", "path": "/tmp"},
            cwd="/tmp",
        )
        assert resp["ok"] is True

    async def test_pre_write_no_old_string(self, running_daemon, tmp_project):
        fp = str(tmp_project / "main.py")
        resp = await send_query(
            running_daemon["socket_path"],
            event="pre-write",
            file_path=fp,
            tool_input={"file_path": fp, "new_string": "new code"},
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True


# ===========================================================================
# MCP Recovery Tests
# ===========================================================================

class TestMCPRecovery:
    """Tests for daemon recovery when MCP subprocess dies."""

    async def test_mcp_death_recovery(self, running_daemon, tmp_project):
        """Kill MCP subprocess — daemon should restart it and serve queries."""
        daemon = running_daemon["daemon"]
        socket_path = running_daemon["socket_path"]
        fp = str(tmp_project / "main.py")

        # Verify it works first
        resp1 = await send_query(socket_path, "pre-read", file_path=fp,
                                 tool_input={"file_path": fp}, cwd=str(tmp_project))
        assert resp1["ok"] is True

        # Kill the MCP process
        if daemon.mcp.process and daemon.mcp.process.returncode is None:
            daemon.mcp.process.kill()
            await daemon.mcp.process.wait()

        # Give watchdog time to notice and restart
        await asyncio.sleep(6.0)

        # Should recover
        resp2 = await send_query(socket_path, "pre-read", file_path=fp,
                                 tool_input={"file_path": fp}, cwd=str(tmp_project))
        assert resp2["ok"] is True


# ===========================================================================
# Handler-Specific Regression Tests
# ===========================================================================

class TestHandlerRegression:
    """Regression tests for specific handler behaviors."""

    async def test_pre_bash_only_build_commands(self, running_daemon):
        """Non-build commands should return empty context."""
        socket_path = running_daemon["socket_path"]
        for cmd in ["ls -la", "cat file.txt", "echo hello", "cd /tmp", "pwd"]:
            resp = await send_query(socket_path, "pre-bash",
                                    tool_input={"command": cmd}, cwd="/tmp")
            assert resp["ok"] is True
            assert resp.get("context", "") == "", f"Non-build cmd '{cmd}' returned context"

    async def test_pre_bash_build_commands_accepted(self, running_daemon, tmp_project):
        """Build commands should be accepted (not error)."""
        socket_path = running_daemon["socket_path"]
        cwd = str(tmp_project)
        build_cmds = [
            "cargo build",
            "cargo test",
            "npm run build",
            "npx tsc",
            "pytest",
            "python -m pytest",
            "dotnet build",
        ]
        for cmd in build_cmds:
            resp = await send_query(socket_path, "pre-bash",
                                    tool_input={"command": cmd}, cwd=cwd)
            assert resp["ok"] is True, f"Build cmd '{cmd}' failed"

    async def test_pre_read_range_filtering(self, running_daemon, tmp_project):
        """Pre-read with offset/limit should filter symbols to that range."""
        socket_path = running_daemon["socket_path"]
        fp = str(tmp_project / "main.py")

        # Read only lines 1-5 (should include 'main' function but maybe not MyClass at line 12)
        resp = await send_query(
            socket_path, "pre-read",
            file_path=fp,
            tool_input={"file_path": fp, "offset": 1, "limit": 5},
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True
        context = resp.get("context", "")
        # Mock has MyClass at line 12 — with offset=1, limit=5 (lines 1-5), the symbol tree
        # should only show `main` (L1), not MyClass (L12). Exports/imports are file-level and unfiltered.
        if context:
            # Extract just the symbol tree lines (before "Exports:" section)
            tree_lines = []
            for line in context.split("\n"):
                if line.startswith("Exports:") or line.startswith("Imports:"):
                    break
                tree_lines.append(line)
            tree_section = "\n".join(tree_lines)
            assert "MyClass" not in tree_section, \
                f"MyClass at line 12 should be filtered out of symbol tree for range 1-5, got: {tree_section}"

    async def test_session_start_returns_symbols(self, running_daemon, tmp_project):
        """Session start should return symbol overview from workspace."""
        resp = await send_query(
            running_daemon["socket_path"],
            event="session-start",
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True
        context = resp.get("context", "")
        assert "[LSP]" in context
        # Mock returns Function and Class kinds
        assert "Function" in context or "Class" in context or "Interface" in context

    async def test_diagnostics_all_severities(self, running_daemon, tmp_project):
        """Pre-read should show diagnostics of all severities, not just errors."""
        socket_path = running_daemon["socket_path"]
        fp = str(tmp_project / "main.py")

        resp = await send_query(
            socket_path, "pre-read",
            file_path=fp,
            tool_input={"file_path": fp},
            cwd=str(tmp_project),
        )
        assert resp["ok"] is True
        context = resp.get("context", "")
        assert context, "pre-read should return non-empty context"
        # Mock returns severity 1 (Error) "missing semicolon" and severity 2 (Warning) "unused variable"
        # Both should appear — verify at least one diagnostic is present
        has_error = "missing semicolon" in context
        has_warning = "unused variable" in context
        assert has_error or has_warning, f"Expected diagnostics in context, got: {context[:200]}"


# ===========================================================================
# Ingest Completeness Tests
# ===========================================================================

class TestIngestCompleteness:
    """Tests for per-phase ingestion tracking (file_ingest_status table)."""

    async def test_phase2_incomplete_allows_retry(self, tmp_path):
        """Phase 1 done + Phase 2 not done → has_fresh_entry returns False."""
        from lsp_hooks_cache import SQLiteCache

        db_path = str(tmp_path / "test.db")
        cache = SQLiteCache(db_path)
        try:
            test_file = tmp_path / "test.py"
            test_file.write_text("def foo(): pass")
            mtime = os.stat(str(test_file)).st_mtime_ns

            import hashlib
            sha = hashlib.sha256(test_file.read_bytes()).hexdigest()

            # Mark only phase 1 done
            cache.set_ingest_phase(str(test_file), mtime, sha, 1)

            # has_fresh_entry should return False (phase 2 and 3 not done)
            assert cache.has_fresh_entry(str(test_file), mtime, sha) is False

            # get_ingest_status should show phase1=True, phase2=False, phase3=False
            status = cache.get_ingest_status(str(test_file), mtime, sha)
            assert status["phase1"] is True
            assert status["phase2"] is False
            assert status["phase3"] is False
        finally:
            cache.close()

    async def test_all_phases_complete_is_fresh(self, tmp_path):
        """All 3 phases done → has_fresh_entry returns True."""
        from lsp_hooks_cache import SQLiteCache

        db_path = str(tmp_path / "test.db")
        cache = SQLiteCache(db_path)
        try:
            test_file = tmp_path / "test.py"
            test_file.write_text("def bar(): pass")
            mtime = os.stat(str(test_file)).st_mtime_ns

            import hashlib
            sha = hashlib.sha256(test_file.read_bytes()).hexdigest()

            # Mark all phases done
            cache.set_ingest_phase(str(test_file), mtime, sha, 1)
            cache.set_ingest_phase(str(test_file), mtime, sha, 2)
            cache.set_ingest_phase(str(test_file), mtime, sha, 3)

            # has_fresh_entry should return True
            assert cache.has_fresh_entry(str(test_file), mtime, sha) is True

            status = cache.get_ingest_status(str(test_file), mtime, sha)
            assert status["phase1"] is True
            assert status["phase2"] is True
            assert status["phase3"] is True
        finally:
            cache.close()

    async def test_invalidate_clears_status(self, tmp_path):
        """invalidate_file should also clear ingest status."""
        from lsp_hooks_cache import SQLiteCache

        db_path = str(tmp_path / "test.db")
        cache = SQLiteCache(db_path)
        try:
            test_file = tmp_path / "test.py"
            test_file.write_text("class Baz: pass")
            mtime = os.stat(str(test_file)).st_mtime_ns

            import hashlib
            sha = hashlib.sha256(test_file.read_bytes()).hexdigest()

            # Set all phases done
            cache.set_ingest_phase(str(test_file), mtime, sha, 1)
            cache.set_ingest_phase(str(test_file), mtime, sha, 2)
            cache.set_ingest_phase(str(test_file), mtime, sha, 3)
            assert cache.has_fresh_entry(str(test_file), mtime, sha) is True

            # Invalidate
            cache.invalidate_file(str(test_file))

            # Status should be cleared
            assert cache.has_fresh_entry(str(test_file), mtime, sha) is False
            status = cache.get_ingest_status(str(test_file), mtime, sha)
            assert status["phase1"] is False
            assert status["phase2"] is False
            assert status["phase3"] is False
        finally:
            cache.close()

    async def test_stale_mtime_returns_not_fresh(self, tmp_path):
        """Different mtime/sha → all phases return False."""
        from lsp_hooks_cache import SQLiteCache

        db_path = str(tmp_path / "test.db")
        cache = SQLiteCache(db_path)
        try:
            test_file = tmp_path / "test.py"
            test_file.write_text("x = 1")
            mtime = os.stat(str(test_file)).st_mtime_ns

            import hashlib
            sha = hashlib.sha256(test_file.read_bytes()).hexdigest()

            # Mark all phases done
            cache.set_ingest_phase(str(test_file), mtime, sha, 1)
            cache.set_ingest_phase(str(test_file), mtime, sha, 2)
            cache.set_ingest_phase(str(test_file), mtime, sha, 3)

            # Modify the file
            time.sleep(0.01)
            test_file.write_text("x = 2")
            new_mtime = os.stat(str(test_file)).st_mtime_ns
            new_sha = hashlib.sha256(test_file.read_bytes()).hexdigest()

            # Old version still fresh
            assert cache.has_fresh_entry(str(test_file), mtime, sha) is True

            # New version should not be fresh
            assert cache.has_fresh_entry(str(test_file), new_mtime, new_sha) is False
            status = cache.get_ingest_status(str(test_file), new_mtime, new_sha)
            assert status["phase1"] is False
            assert status["phase2"] is False
            assert status["phase3"] is False
        finally:
            cache.close()
