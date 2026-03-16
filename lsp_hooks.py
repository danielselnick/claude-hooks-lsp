#!/usr/bin/env python3
"""LSP Hooks — Claude Code hook script for proactive LSP context injection.

Ultra-thin client: reads hook input, delegates to daemon via Unix socket,
outputs systemMessage. Graceful degradation on any failure (exit 0).
"""

import json
import logging
import os
import socket
import sys
import time

from lsp_hooks_paths import LOG_PATH, SOCKET_PATH, PID_PATH

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,
    format="%(asctime)s [hook] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lsp_hooks")

CONNECT_TIMEOUT = 0.1  # 100ms
SUPPORTED_EXTENSIONS = frozenset((
    ".rs", ".toml",
    ".py", ".pyi",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".cs",
))
EXCLUDED_PATHS = ("target/", ".git/", "node_modules/")
BUDGETS_MS = {
    "pre-write": 2000,
    "pre-read": 1000,
    "pre-bash": 1000,
    "prompt": 2000,
    "session-start": 3000,
}


def _try_start_daemon():
    """Auto-start daemon if not running. Returns True if started."""
    import subprocess
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", os.path.dirname(os.path.abspath(__file__)))
    daemon_script = os.path.join(plugin_root, "lsp_hooks_daemon.py")
    if not os.path.exists(daemon_script):
        log.warning("daemon script not found: %s", daemon_script)
        return False
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=plugin_root)
    try:
        subprocess.Popen(
            [sys.executable, daemon_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        log.info("auto-started daemon via %s", daemon_script)
        return True
    except Exception as e:
        log.warning("failed to auto-start daemon: %s", e)
        return False


def main():
    t0 = time.monotonic()

    # Recursion guard
    if os.environ.get("LSP_HOOKS_ACTIVE"):
        log.debug("recursion guard — exiting")
        return

    # Parse --event
    event = None
    args = sys.argv
    for i, a in enumerate(args):
        if a == "--event" and i + 1 < len(args):
            event = args[i + 1]
            break
    if not event:
        log.debug("no --event arg — exiting")
        return

    # Read stdin
    try:
        raw = sys.stdin.read()
        if not raw:
            log.debug("empty stdin — exiting")
            return
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, IOError) as e:
        log.error("stdin parse error: %s", e)
        print("lsp-hooks: stdin parse error", file=sys.stderr)
        sys.exit(1)

    log.info(">>> event=%s tool=%s file=%s",
             event,
             hook_input.get("tool_name", "-"),
             hook_input.get("tool_input", {}).get("file_path", "-"))
    log.debug(">>> stdin: %s", json.dumps(hook_input, default=str)[:2000])

    cwd = hook_input.get("cwd", "")
    tool_input = hook_input.get("tool_input", {})

    # Extract file_path for file-specific events
    file_path = ""
    if event in ("pre-write", "pre-read"):
        file_path = tool_input.get("file_path", "")
    elif event == "prompt":
        # Pass user_prompt through tool_input for daemon
        # Field is "prompt" in actual stdin (not "user_prompt" as SKILL.md claims)
        tool_input = dict(tool_input) if tool_input else {}
        tool_input["user_prompt"] = hook_input.get("prompt", "") or hook_input.get("user_prompt", "")

    # Resolve relative paths
    if file_path and not os.path.isabs(file_path):
        file_path = os.path.join(cwd, file_path)

    # Extension filter
    if file_path:
        _, ext = os.path.splitext(file_path)
        if ext not in SUPPORTED_EXTENSIONS:
            log.debug("skipped: unsupported extension %s", ext)
            return
        for excl in EXCLUDED_PATHS:
            if excl in file_path:
                log.debug("skipped: excluded path %s", excl)
                return

    # Connect to daemon (auto-start on first failure)
    budget_ms = BUDGETS_MS.get(event, 2000)
    sock_timeout = max((budget_ms - 200) / 1000.0, 0.3)

    sock = None
    for attempt in range(2):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(SOCKET_PATH)
            sock.settimeout(sock_timeout)
            break
        except (socket.error, OSError) as e:
            if attempt == 0:
                log.warning("daemon unreachable, attempting auto-start: %s", e)
                if _try_start_daemon():
                    time.sleep(0.5)
                    continue
            log.warning("daemon unreachable: %s (%.0fms)", e, (time.monotonic() - t0) * 1000)
            return  # daemon not running — graceful degradation

    if sock is None:
        return

    # Query daemon
    try:
        req_obj = {
            "method": "query",
            "params": {
                "event": event,
                "file_path": file_path,
                "tool_input": tool_input,
                "cwd": cwd,
            },
        }
        log.debug(">>> daemon request: %s", json.dumps(req_obj, default=str)[:2000])
        request = json.dumps(req_obj) + "\n"
        sock.sendall(request.encode())

        # Read response (single newline-delimited JSON line)
        buf = b""
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        sock.close()

        if not buf:
            return

        response = json.loads(buf.decode().split("\n", 1)[0])
    except (socket.timeout, socket.error, json.JSONDecodeError, OSError) as e:
        log.warning("daemon communication error: %s (%.0fms)", e, (time.monotonic() - t0) * 1000)
        try:
            sock.close()
        except Exception:
            pass
        return

    elapsed_ms = (time.monotonic() - t0) * 1000
    log.debug("<<< daemon response: %s", json.dumps(response, default=str)[:2000])

    context = response.get("context", "")
    if not context:
        log.info("<<< empty context (%.0fms)", elapsed_ms)
        return

    # Emit hook output — systemMessage injects context for Claude
    output = {"continue": True, "systemMessage": context}
    log.info("<<< output (%d chars, %.0fms): %s", len(context), elapsed_ms, context[:200])
    print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("unhandled exception: %s", e)
        sys.exit(0)
