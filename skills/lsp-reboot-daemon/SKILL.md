---
name: lsp-reboot-daemon
description: Restart the lsp-hooks daemon. Use when LSP context stops appearing or the daemon is unresponsive.
disable-model-invocation: true
allowed-tools: Bash, Read
---

# Restart the lsp-hooks daemon

Follow these steps exactly:

## 1. Resolve runtime paths

```bash
cd "${CLAUDE_PLUGIN_ROOT}" && python3 -c "
from lsp_hooks_paths import *
print(f'PID_PATH={PID_PATH}')
print(f'SOCKET_PATH={SOCKET_PATH}')
print(f'VERSION_PATH={VERSION_PATH}')
"
```

Save the printed paths for use in subsequent steps.

## 2. Stop the old daemon (if running)

Read the PID file. If it exists and contains a numeric PID, kill the daemon:

```bash
PID_FILE="<PID_PATH from step 1>"
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  kill "$OLD_PID" 2>/dev/null || true
  sleep 0.3
fi
```

## 3. Clean up stale runtime files

Remove the PID, version, and socket files so the new daemon starts cleanly:

```bash
rm -f "<PID_PATH>" "<VERSION_PATH>" "<SOCKET_PATH>"
```

## 4. Start a fresh daemon

```bash
cd "${CLAUDE_PLUGIN_ROOT}" && python3 lsp_hooks_daemon.py &
disown
sleep 0.5
```

## 5. Verify

Check that the PID file reappears:

```bash
PID_FILE="<PID_PATH from step 1>"
if [ -f "$PID_FILE" ]; then
  echo "Daemon restarted successfully (PID $(cat "$PID_FILE"))"
else
  echo "WARNING: PID file did not reappear — daemon may have failed to start. Check the log file."
fi
```

Report the result to the user.
