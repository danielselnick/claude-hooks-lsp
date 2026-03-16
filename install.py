#!/usr/bin/env python3
"""LSP Hooks installer — sets up npm deps, LSP servers, and daemon."""

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HOOKS_DIR = Path(__file__).resolve().parent
HOOK_SCRIPT = HOOKS_DIR / "lsp_hooks.py"
DAEMON_SCRIPT = HOOKS_DIR / "lsp_hooks_daemon.py"
DOTNET_TOOLS = Path.home() / ".dotnet" / "tools"
ZPROFILE = Path.home() / ".zprofile"

# ---------------------------------------------------------------------------
# LSP servers: (id, check_cmd, install_fn_or_instructions)
# ---------------------------------------------------------------------------

def _npm_global_install(pkg: str):
    return lambda: subprocess.run(
        ["npm", "install", "-g", pkg], check=True,
    )

def _uv_tool_install(pkg: str):
    return lambda: subprocess.run(
        ["uv", "tool", "install", pkg], check=True,
    )

def _cargo_install(pkg: str):
    return lambda: subprocess.run(
        ["cargo", "install", pkg], check=True,
    )

def _dotnet_tool_install(pkg: str):
    return lambda: subprocess.run(
        ["dotnet", "tool", "install", "--global", pkg], check=True,
    )

LSP_SERVERS = [
    {
        "id": "rust-analyzer",
        "binary": "rust-analyzer",
        "languages": "Rust (.rs)",
        "install": _cargo_install("rust-analyzer"),
        "install_hint": "cargo install rust-analyzer",
    },
    {
        "id": "typescript-language-server",
        "binary": "typescript-language-server",
        "languages": "TypeScript/JavaScript (.ts .tsx .js .jsx)",
        "install": _npm_global_install("typescript-language-server"),
        "install_hint": "npm install -g typescript-language-server",
    },
    {
        "id": "pylsp",
        "binary": "pylsp",
        "languages": "Python (.py .pyi)",
        "install": _uv_tool_install("python-lsp-server"),
        "install_hint": "uv tool install python-lsp-server",
    },
    {
        "id": "csharp-ls",
        "binary": "csharp-ls",
        "extra_paths": [str(DOTNET_TOOLS)],
        "languages": "C# (.cs)",
        "install": _dotnet_tool_install("csharp-ls"),
        "install_hint": "dotnet tool install --global csharp-ls",
    },
    {
        "id": "clangd",
        "binary": "clangd",
        "languages": "C/C++ (.c .cpp .h .hpp)",
        "install": None,
        "install_hint": "Install via Xcode CLI tools or brew install llvm",
    },
    {
        "id": "gopls",
        "binary": "gopls",
        "languages": "Go (.go)",
        "install": lambda: subprocess.run(
            ["go", "install", "golang.org/x/tools/gopls@latest"], check=True,
        ),
        "install_hint": "go install golang.org/x/tools/gopls@latest",
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"

def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"

def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m"

def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m"

def _which(binary: str, extra_paths: list[str] | None = None) -> str | None:
    result = shutil.which(binary)
    if result:
        return result
    for p in (extra_paths or []):
        candidate = os.path.join(p, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None

def _ask(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer in ("y", "yes")

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def check_prerequisites():
    """Verify python3 and node are available."""
    print(_bold("\n=== Prerequisites ==="))
    ok = True

    py = shutil.which("python3")
    if py:
        print(f"  {_green('✓')} python3: {py}")
    else:
        print(f"  {_red('✗')} python3 not found")
        ok = False

    node = shutil.which("node")
    if node:
        print(f"  {_green('✓')} node: {node}")
    else:
        print(f"  {_red('✗')} node not found (required for lsp-mcp-server)")
        ok = False

    uv = shutil.which("uv")
    if uv:
        print(f"  {_green('✓')} uv: {uv}")
    else:
        print(f"  {_red('✗')} uv not found — install via: curl -LsSf https://astral.sh/uv/install.sh | sh")
        ok = False

    return ok


def install_npm_deps():
    """Run npm install --production in plugin directory."""
    print(_bold("\n=== npm Dependencies ==="))
    plugin_dir = Path(__file__).resolve().parent
    pkg_json = plugin_dir / "package.json"
    if not pkg_json.exists():
        print(f"  {_red('✗')} package.json not found in {plugin_dir}")
        return False

    entry = plugin_dir / "node_modules" / "lsp-mcp-server" / "dist" / "index.js"
    if entry.exists():
        print(f"  {_green('✓')} lsp-mcp-server already installed")
        return True

    print(f"  Installing npm dependencies...")
    try:
        subprocess.run(
            ["npm", "install", "--production"],
            cwd=plugin_dir, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        print(f"  {_red('✗')} npm install failed: {e}")
        return False

    if entry.exists():
        print(f"  {_green('✓')} lsp-mcp-server installed")
        return True
    else:
        print(f"  {_red('✗')} lsp-mcp-server dist/index.js not found after install")
        return False


def install_lsp_servers():
    """Check and optionally install LSP language servers."""
    print(_bold("\n=== LSP Language Servers ==="))
    missing = []

    for server in LSP_SERVERS:
        path = _which(server["binary"], server.get("extra_paths"))
        if path:
            print(f"  {_green('✓')} {server['id']}: {path}  ({server['languages']})")
        else:
            print(f"  {_yellow('✗')} {server['id']}: not found  ({server['languages']})")
            missing.append(server)

    if not missing:
        print(f"\n  All language servers installed.")
        return

    print(f"\n  {len(missing)} server(s) missing.")
    for server in missing:
        if server["install"] is None:
            print(f"  {_yellow('→')} {server['id']}: {server['install_hint']} (manual)")
            continue
        if _ask(f"  Install {server['id']}? ({server['install_hint']})"):
            try:
                server["install"]()
                # Verify
                path = _which(server["binary"], server.get("extra_paths"))
                if path:
                    print(f"  {_green('✓')} {server['id']} installed: {path}")
                else:
                    print(f"  {_yellow('!')} {server['id']} installed but not on PATH")
                    _fix_path_if_needed(server)
            except subprocess.CalledProcessError as e:
                print(f"  {_red('✗')} Failed to install {server['id']}: {e}")
            except FileNotFoundError as e:
                print(f"  {_red('✗')} Package manager not found: {e}")


def _fix_path_if_needed(server: dict):
    """If a binary was installed to a known location not on PATH, offer to fix it."""
    if server["id"] == "csharp-ls" and DOTNET_TOOLS.exists():
        if str(DOTNET_TOOLS) not in os.environ.get("PATH", ""):
            if _ask(f"  Add {DOTNET_TOOLS} to PATH in ~/.zprofile?"):
                _add_to_zprofile(
                    "# .NET tools",
                    f'export PATH="$PATH:{DOTNET_TOOLS}"',
                )


def _add_to_zprofile(*lines: str):
    """Append lines to ~/.zprofile if not already present."""
    existing = ZPROFILE.read_text() if ZPROFILE.exists() else ""
    to_add = []
    for line in lines:
        if line not in existing:
            to_add.append(line)
    if to_add:
        with open(ZPROFILE, "a") as f:
            f.write("\n" + "\n".join(to_add) + "\n")
        print(f"  {_green('✓')} Updated {ZPROFILE}")


def cleanup_old_hooks():
    """Detect and offer to remove old lsp-hooks entries from ~/.claude/settings.json."""
    print(_bold("\n=== Cleanup Old Hooks ==="))
    settings_path = Path.home() / ".claude" / "settings.json"

    if not settings_path.exists():
        print(f"  No settings.json found — nothing to clean up")
        return

    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"  {_yellow('!')} Could not read settings.json: {e}")
        return

    hooks = settings.get("hooks", {})
    if not hooks:
        print(f"  No hooks found in settings.json")
        return

    # Check if any hook commands reference lsp_hooks.py
    has_lsp_hooks = any(
        "lsp_hooks.py" in h.get("command", "")
        for group in hooks.values()
        for entry in group
        for h in entry.get("hooks", [])
    )

    if not has_lsp_hooks:
        print(f"  No lsp-hooks entries found in settings.json hooks")
        return

    print(f"  Found lsp-hooks entries in {settings_path}")
    print(f"  These are now handled by the plugin's hooks/hooks.json")
    if _ask("  Remove old hooks from settings.json?"):
        import shutil
        backup = settings_path.with_suffix(".json.bak")
        shutil.copy2(settings_path, backup)
        print(f"  Backup: {backup}")

        del settings["hooks"]
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        print(f"  {_green('✓')} Removed hooks from settings.json")
    else:
        print(f"  {_yellow('!')} Skipped — old hooks left in place")
        print(f"  Note: duplicate hooks may fire if both plugin and settings.json hooks are active")


def start_daemon():
    """Start the daemon if not already running."""
    print(_bold("\n=== Daemon ==="))

    from lsp_hooks_paths import PID_PATH
    pid_path = Path(PID_PATH)

    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            print(f"  {_green('✓')} Daemon already running (pid={pid})")
            if _ask("  Restart?", default=False):
                os.kill(pid, 15)
                print(f"  Stopped pid={pid}")
            else:
                return
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    plugin_dir = Path(__file__).resolve().parent
    daemon_script = plugin_dir / "lsp_hooks_daemon.py"
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(plugin_dir))

    print(f"  Starting daemon...")
    proc = subprocess.Popen(
        [sys.executable, str(daemon_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )

    import time
    time.sleep(1)

    if proc.poll() is None:
        print(f"  {_green('✓')} Daemon started (pid={proc.pid})")
    else:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        print(f"  {_red('✗')} Daemon exited immediately: {stderr[:200]}")


def print_summary():
    from lsp_hooks_paths import LOG_PATH, SOCKET_PATH
    plugin_dir = Path(__file__).resolve().parent

    print(_bold("\n=== Done ==="))
    print(f"  Plugin dir:   {plugin_dir}")
    print(f"  Logs:         {LOG_PATH}")
    print(f"  Socket:       {SOCKET_PATH}")
    print()
    print(f"  To install as a Claude Code plugin:")
    print(f"    claude /plugin marketplace add {plugin_dir}")
    print()
    print(f"  To restart daemon:")
    print(f"    python3 {plugin_dir / 'lsp_hooks_daemon.py'}")
    print(f"  To view logs:")
    print(f"    tail -f {LOG_PATH}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(_bold("LSP Hooks Installer"))
    print(f"Platform: {platform.system()} {platform.machine()}")

    if not check_prerequisites():
        print(f"\n{_red('Aborting:')} prerequisites not met.")
        sys.exit(1)

    if not install_npm_deps():
        print(f"\n{_red('Aborting:')} npm dependencies not available.")
        sys.exit(1)

    install_lsp_servers()
    cleanup_old_hooks()
    start_daemon()
    print_summary()


if __name__ == "__main__":
    main()
