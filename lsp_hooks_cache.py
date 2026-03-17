"""SQLite L2 cache for lsp-hooks daemon.

Persistent cache keyed by tool_name + SHA-256(canonical JSON args) with
mtime + content-SHA invalidation for file-scoped entries and TTL for workspace ops.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time

log = logging.getLogger("lsp_hooks_daemon")

SCHEMA_VERSION = 2
WORKSPACE_TTL = 300.0  # 5 minutes for non-file-scoped entries

# Core tools that the file watcher pre-caches for every file
CORE_TOOLS = frozenset([
    "lsp_document_symbols",
    "lsp_diagnostics",
    "lsp_file_exports",
    "lsp_file_imports",
    "lsp_related_files",
])


def _args_hash(args: dict) -> str:
    """SHA-256 of canonical JSON representation of args."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _file_mtime_ns(path: str) -> int | None:
    """Return st_mtime_ns for path, or None if file doesn't exist."""
    try:
        return os.stat(path).st_mtime_ns
    except (FileNotFoundError, OSError):
        return None


def _file_content_sha(path: str) -> str | None:
    """Return SHA-256 hex digest of file content, or None if unreadable."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (FileNotFoundError, OSError, PermissionError):
        return None


class SQLiteCache:
    """Persistent L2 cache backed by SQLite with WAL mode."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=1000")
        self._check_schema(conn)
        self._conn = conn
        return conn

    def _check_schema(self, conn: sqlite3.Connection):
        """Create or recreate schema if version mismatches."""
        try:
            row = conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if row and row[0] == SCHEMA_VERSION:
                return
        except sqlite3.OperationalError:
            pass
        # Drop and recreate
        conn.executescript("""
            DROP TABLE IF EXISTS tool_cache;
            DROP TABLE IF EXISTS schema_version;

            CREATE TABLE schema_version (
                version INTEGER NOT NULL
            );

            CREATE TABLE tool_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                args_hash TEXT NOT NULL,
                file_path TEXT,
                file_mtime_ns INTEGER,
                content_sha TEXT,
                result_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_hit_at REAL NOT NULL,
                hit_count INTEGER DEFAULT 0,
                UNIQUE(tool_name, args_hash)
            );

            CREATE INDEX idx_tool_args ON tool_cache(tool_name, args_hash);
            CREATE INDEX idx_file_path ON tool_cache(file_path);
            CREATE INDEX idx_last_hit ON tool_cache(last_hit_at);
        """)
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
        )
        conn.commit()
        log.info("sqlite cache: schema v%d created at %s", SCHEMA_VERSION, self._db_path)

    def get(self, tool_name: str, args: dict, file_path: str | None = None) -> object | None:
        """Look up cached result. Returns None on miss or stale entry."""
        try:
            conn = self._ensure_conn()
            ah = _args_hash(args)
            row = conn.execute(
                "SELECT id, file_path, file_mtime_ns, content_sha, result_json, created_at "
                "FROM tool_cache WHERE tool_name=? AND args_hash=?",
                (tool_name, ah),
            ).fetchone()
            if row is None:
                return None
            row_id, row_fp, row_mtime, row_sha, result_json, created_at = row

            # File-scoped: validate mtime + content SHA
            if row_fp:
                current_mtime = _file_mtime_ns(row_fp)
                if current_mtime is None:
                    # File deleted — remove stale entry
                    conn.execute("DELETE FROM tool_cache WHERE id=?", (row_id,))
                    conn.commit()
                    return None
                if current_mtime != row_mtime:
                    # mtime changed — check content SHA for real change
                    current_sha = _file_content_sha(row_fp)
                    if current_sha and row_sha and current_sha == row_sha:
                        # Content unchanged (e.g. touch) — update mtime in-place
                        conn.execute(
                            "UPDATE tool_cache SET file_mtime_ns=?, last_hit_at=?, hit_count=hit_count+1 WHERE id=?",
                            (current_mtime, time.time(), row_id),
                        )
                        conn.commit()
                        return json.loads(result_json)
                    # Content actually changed — stale
                    conn.execute("DELETE FROM tool_cache WHERE id=?", (row_id,))
                    conn.commit()
                    return None
            else:
                # Workspace op: enforce TTL
                if (time.time() - created_at) > WORKSPACE_TTL:
                    conn.execute("DELETE FROM tool_cache WHERE id=?", (row_id,))
                    conn.commit()
                    return None

            # Cache hit — update stats
            conn.execute(
                "UPDATE tool_cache SET last_hit_at=?, hit_count=hit_count+1 WHERE id=?",
                (time.time(), row_id),
            )
            conn.commit()
            return json.loads(result_json)
        except Exception as e:
            log.warning("sqlite cache get error: %s", e)
            return None

    def put(
        self,
        tool_name: str,
        args: dict,
        result: object,
        file_path: str | None = None,
    ):
        """Store result in cache. Skips empty/None results."""
        if result is None:
            return
        try:
            result_json = json.dumps(result, default=str)
            if not result_json or result_json in ("null", '""'):
                return
        except (TypeError, ValueError):
            return
        try:
            conn = self._ensure_conn()
            ah = _args_hash(args)
            mtime = _file_mtime_ns(file_path) if file_path else None
            sha = _file_content_sha(file_path) if file_path else None
            now = time.time()
            conn.execute(
                "INSERT INTO tool_cache "
                "(tool_name, args_hash, file_path, file_mtime_ns, content_sha, result_json, "
                "created_at, last_hit_at, hit_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(tool_name, args_hash) DO UPDATE SET "
                "file_mtime_ns=excluded.file_mtime_ns, "
                "content_sha=excluded.content_sha, "
                "result_json=excluded.result_json, "
                "last_hit_at=excluded.last_hit_at",
                (tool_name, ah, file_path, mtime, sha, result_json, now, now),
            )
            conn.commit()
        except Exception as e:
            log.warning("sqlite cache put error: %s", e)

    def get_all_for_file(self, file_path: str) -> dict[str, object]:
        """Return {tool_name: result} for all cached entries for a file.

        Validates mtime+sha freshness. Returns only fresh entries.
        """
        try:
            conn = self._ensure_conn()
            current_mtime = _file_mtime_ns(file_path)
            if current_mtime is None:
                return {}
            rows = conn.execute(
                "SELECT tool_name, file_mtime_ns, content_sha, result_json "
                "FROM tool_cache WHERE file_path=?",
                (file_path,),
            ).fetchall()
            result = {}
            for tool_name, row_mtime, row_sha, result_json in rows:
                if row_mtime == current_mtime:
                    result[tool_name] = json.loads(result_json)
                elif row_sha:
                    current_sha = _file_content_sha(file_path)
                    if current_sha and current_sha == row_sha:
                        result[tool_name] = json.loads(result_json)
            return result
        except Exception as e:
            log.warning("sqlite cache get_all_for_file error: %s", e)
            return {}

    def has_fresh_entry(self, file_path: str, mtime_ns: int, content_sha: str) -> bool:
        """Check if ALL core tools are cached fresh for this file."""
        try:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT tool_name, file_mtime_ns, content_sha "
                "FROM tool_cache WHERE file_path=?",
                (file_path,),
            ).fetchall()
            cached_tools = set()
            for tool_name, row_mtime, row_sha in rows:
                if tool_name in CORE_TOOLS:
                    if row_mtime == mtime_ns or (row_sha and row_sha == content_sha):
                        cached_tools.add(tool_name)
            return CORE_TOOLS.issubset(cached_tools)
        except Exception as e:
            log.warning("sqlite cache has_fresh_entry error: %s", e)
            return False

    def all_cached_files(self) -> dict[str, tuple[int | None, str | None]]:
        """Return {file_path: (mtime_ns, content_sha)} for all file-scoped entries."""
        try:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT DISTINCT file_path, file_mtime_ns, content_sha "
                "FROM tool_cache WHERE file_path IS NOT NULL"
            ).fetchall()
            result = {}
            for fp, mtime, sha in rows:
                result[fp] = (mtime, sha)
            return result
        except Exception as e:
            log.warning("sqlite cache all_cached_files error: %s", e)
            return {}

    def invalidate_file(self, file_path: str):
        """Remove all cached entries for a specific file."""
        try:
            conn = self._ensure_conn()
            conn.execute("DELETE FROM tool_cache WHERE file_path=?", (file_path,))
            conn.commit()
        except Exception as e:
            log.warning("sqlite cache invalidate error: %s", e)

    def evict_stale(self, max_age: float = 86400.0, max_rows: int = 10000):
        """Remove entries older than max_age seconds and cap total rows."""
        try:
            conn = self._ensure_conn()
            cutoff = time.time() - max_age
            conn.execute("DELETE FROM tool_cache WHERE last_hit_at < ?", (cutoff,))
            # Cap rows: keep most recently hit
            count = conn.execute("SELECT COUNT(*) FROM tool_cache").fetchone()[0]
            if count > max_rows:
                conn.execute(
                    "DELETE FROM tool_cache WHERE id NOT IN "
                    "(SELECT id FROM tool_cache ORDER BY last_hit_at DESC LIMIT ?)",
                    (max_rows,),
                )
            conn.commit()
        except Exception as e:
            log.warning("sqlite cache evict error: %s", e)

    def close(self):
        """Close the database connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
