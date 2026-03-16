"""Shared path constants for lsp-hooks client and daemon.

Single source of truth — prevents path disagreement between components.
"""

import getpass
import os
import tempfile

USER = getpass.getuser()
SOCKET_PATH = os.path.join(tempfile.gettempdir(), f"lsp-hooks-{USER}.sock")
PID_PATH = os.path.join(tempfile.gettempdir(), f"lsp-hooks-{USER}.pid")
LOG_PATH = os.path.join(tempfile.gettempdir(), f"lsp-hooks-{USER}.log")
