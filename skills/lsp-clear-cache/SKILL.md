---
name: lsp-clear-cache
description: "Full reset: stop the lsp-hooks daemon and its child processes, delete the log file and SQLite cache, then restart fresh."
disable-model-invocation: true
allowed-tools: Bash, Read
---

# Full lsp-hooks reset (clear cache + restart)

Follow these steps exactly:

## 1. Resolve all runtime paths

```bash
cd "${CLAUDE_PLUGIN_ROOT}" && python3 -c "
from lsp_hooks_paths import *
print(f'PID_PATH={PID_PATH}')
print(f'LOG_PATH={LOG_PATH}')
print(f'SOCKET_PATH={SOCKET_PATH}')
print(f'VERSION_PATH={VERSION_PATH}')
print(f'CACHE_DB_PATH={CACHE_DB_PATH}')
"
```

Save all printed paths for use in subsequent steps.

## 2. Stop the daemon and its children

Read the PID file. If it exists, kill child processes first (e.g. lsp-mcp-server node child), then the daemon itself:

```bash
PID_FILE="<PID_PATH from step 1>"
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  pkill -P "$OLD_PID" 2>/dev/null || true
  sleep 0.2
  kill "$OLD_PID" 2>/dev/null || true
  sleep 0.3
fi
```

## 3. Clean up runtime files

```bash
rm -f "<PID_PATH>" "<VERSION_PATH>" "<SOCKET_PATH>"
```

## 4. Delete the log file

Record the size before deleting for the report:

```bash
LOG_FILE="<LOG_PATH from step 1>"
if [ -f "$LOG_FILE" ]; then
  LOG_SIZE=$(du -h "$LOG_FILE" | cut -f1)
  rm -f "$LOG_FILE"
  echo "Deleted log file ($LOG_SIZE)"
else
  echo "No log file found"
fi
```

## 5. Delete the SQLite cache

```bash
CACHE_FILE="<CACHE_DB_PATH from step 1>"
if [ -f "$CACHE_FILE" ]; then
  CACHE_SIZE=$(du -h "$CACHE_FILE" | cut -f1)
  rm -f "$CACHE_FILE"
  echo "Deleted cache DB ($CACHE_SIZE)"
else
  echo "No cache DB found"
fi
```

## 6. Start a fresh daemon

```bash
cd "${CLAUDE_PLUGIN_ROOT}" && python3 lsp_hooks_daemon.py &
disown
sleep 0.5
```

## 7. Verify and report

```bash
PID_FILE="<PID_PATH from step 1>"
if [ -f "$PID_FILE" ]; then
  echo "Daemon restarted successfully (PID $(cat "$PID_FILE"))"
else
  echo "WARNING: PID file did not reappear — daemon may have failed to start."
fi
```

Report to the user what was cleaned up (log size, cache size) and the new daemon PID. Note that the first LSP query after a cache clear may be slower (cold cache).
