#!/usr/bin/env python3
"""Mock MCP server for testing. Speaks JSON-RPC 2.0 over stdin/stdout.

Launched as a subprocess by the daemon in place of the real lsp-mcp-server.
Returns canned LSP responses for all known tool calls.
"""
import json
import os
import sys
import time

# Optional: artificial delay for slow-response tests
DELAY_TOOL = os.environ.get("MOCK_MCP_DELAY_TOOL", "")
DELAY_SECONDS = float(os.environ.get("MOCK_MCP_DELAY_SECONDS", "0"))

MOCK_RESPONSES = {
    "lsp_document_symbols": {
        "content": [{"type": "text", "text": json.dumps({
            "symbols": [
                {
                    "name": "main",
                    "kind": "Function",
                    "range": {"start": {"line": 1, "column": 1}, "end": {"line": 10, "column": 1}},
                    "selection_range": {"start": {"line": 1, "column": 5}, "end": {"line": 1, "column": 9}},
                },
                {
                    "name": "MyClass",
                    "kind": "Class",
                    "range": {"start": {"line": 12, "column": 1}, "end": {"line": 30, "column": 1}},
                    "selection_range": {"start": {"line": 12, "column": 7}, "end": {"line": 12, "column": 14}},
                    "children": [
                        {
                            "name": "method_a",
                            "kind": "Method",
                            "range": {"start": {"line": 14, "column": 5}, "end": {"line": 20, "column": 5}},
                            "selection_range": {"start": {"line": 14, "column": 9}, "end": {"line": 14, "column": 17}},
                        }
                    ],
                },
            ]
        })}],
    },
    "lsp_diagnostics": {
        "content": [{"type": "text", "text": json.dumps({
            "diagnostics": [
                {"message": "unused variable `x`", "severity": 2,
                 "range": {"start": {"line": 5, "column": 9}, "end": {"line": 5, "column": 10}}},
                {"message": "missing semicolon", "severity": 1,
                 "range": {"start": {"line": 8, "column": 1}, "end": {"line": 8, "column": 2}}},
                {"message": "consider using let", "severity": 3,
                 "range": {"start": {"line": 3, "column": 1}, "end": {"line": 3, "column": 5}}},
                {"message": "unnecessary parentheses", "severity": 4,
                 "range": {"start": {"line": 7, "column": 1}, "end": {"line": 7, "column": 10}}},
            ]
        })}],
    },
    "lsp_file_exports": {
        "content": [{"type": "text", "text": json.dumps({
            "exports": [
                {"name": "main", "kind": "Function", "line": 1, "signature": "def main() -> None"},
                {"name": "MyClass", "kind": "Class", "line": 12},
            ]
        })}],
    },
    "lsp_file_imports": {
        "content": [{"type": "text", "text": json.dumps({
            "imports": [
                {"module": "os", "names": ["path"]},
                {"module": "sys", "names": []},
            ]
        })}],
    },
    "lsp_related_files": {
        "content": [{"type": "text", "text": json.dumps({
            "imports": ["/project/utils.py"],
            "imported_by": ["/project/test_main.py"],
        })}],
    },
    "lsp_hover": {
        "content": [{"type": "text", "text": json.dumps({
            "content": "(function) def main() -> None",
        })}],
    },
    "lsp_smart_search": {
        "content": [{"type": "text", "text": json.dumps({
            "definition": {"path": "/project/main.py", "line": 1, "column": 5},
            "references": {"total_count": 3, "items": []},
            "hover": {"content": "def main() -> None"},
        })}],
    },
    "lsp_workspace_symbols": {
        "content": [{"type": "text", "text": json.dumps({
            "symbols": [
                {"name": "main", "kind": "Function", "path": "/project/main.py", "line": 1},
                {"name": "MyClass", "kind": "Class", "path": "/project/main.py", "line": 12},
                {"name": "helper", "kind": "Function", "path": "/project/utils.py", "line": 1},
                {"name": "Config", "kind": "Interface", "path": "/project/config.ts", "line": 5},
            ]
        })}],
    },
    "lsp_workspace_diagnostics": {
        "content": [{"type": "text", "text": json.dumps({
            "diagnostics": [
                {"file": "/project/main.py", "line": 5, "message": "unused variable", "severity": 2},
                {"file": "/project/main.py", "line": 8, "message": "missing semicolon", "severity": 1},
                {"file": "/project/utils.py", "line": 3, "message": "type mismatch", "severity": 1},
            ]
        })}],
    },
    "lsp_find_symbol": {
        "content": [{"type": "text", "text": json.dumps({
            "match": {"name": "MyClass", "path": "/project/main.py", "line": 12, "kind": "Class"},
            "references": {"total_count": 5},
            "incoming_calls": [{"from": {"name": "run", "uri": "/project/run.py"}}],
            "outgoing_calls": [{"to": {"name": "helper", "uri": "/project/utils.py"}}],
        })}],
    },
    "lsp_call_hierarchy": {
        "content": [{"type": "text", "text": json.dumps({
            "incoming": [{"from": {"name": "caller_fn", "uri": "/project/caller.py"}}],
            "outgoing": [{"to": {"name": "callee_fn", "uri": "/project/callee.py"}}],
        })}],
    },
    "lsp_type_hierarchy": {
        "content": [{"type": "text", "text": json.dumps({
            "supertypes": [{"name": "BaseClass", "path": "/project/base.py", "line": 1}],
            "subtypes": [{"name": "SubClass", "path": "/project/sub.py", "line": 5}],
        })}],
    },
    "lsp_find_references": {
        "content": [{"type": "text", "text": json.dumps({
            "references": [
                {"path": "/project/main.py", "line": 10, "column": 5},
                {"path": "/project/test.py", "line": 3, "column": 1},
            ]
        })}],
    },
    "lsp_find_implementations": {
        "content": [{"type": "text", "text": json.dumps({
            "implementations": [
                {"name": "ConcreteImpl", "path": "/project/impl.py", "line": 8},
            ]
        })}],
    },
    "lsp_goto_definition": {
        "content": [{"type": "text", "text": json.dumps({
            "definitions": [{"path": "/project/main.py", "line": 1, "column": 5}],
        })}],
    },
    "lsp_goto_type_definition": {
        "content": [{"type": "text", "text": json.dumps({
            "definitions": [{"path": "/project/types.py", "line": 10, "column": 1}],
        })}],
    },
    "lsp_signature_help": {
        "content": [{"type": "text", "text": json.dumps({
            "signatures": [{"label": "def main(args: list[str]) -> int", "parameters": []}],
        })}],
    },
    "lsp_code_actions": {
        "content": [{"type": "text", "text": json.dumps({
            "actions": [
                {"title": "Remove unused variable", "kind": "quickfix"},
            ]
        })}],
    },
}


def handle(msg):
    method = msg.get("method", "")
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "capabilities": {},
                "serverInfo": {"name": "mock-mcp-server", "version": "1.0.0"},
                "protocolVersion": "2024-11-05",
            },
        }

    if method == "notifications/initialized":
        return None  # notification — no response

    if method == "tools/call":
        tool_name = msg.get("params", {}).get("name", "")

        # Optional artificial delay for specific tools
        if DELAY_TOOL and tool_name == DELAY_TOOL and DELAY_SECONDS > 0:
            time.sleep(DELAY_SECONDS)

        result = MOCK_RESPONSES.get(tool_name, {
            "content": [{"type": "text", "text": "{}"}],
        })
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # Unknown method — return empty result
    return {"jsonrpc": "2.0", "id": msg_id, "result": {}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
