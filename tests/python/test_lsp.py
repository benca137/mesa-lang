"""Integration tests for the Mesa stdio LSP server."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _send(proc: subprocess.Popen, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    assert proc.stdin is not None
    proc.stdin.write(header)
    proc.stdin.write(body)
    proc.stdin.flush()


def _read(proc: subprocess.Popen) -> dict:
    assert proc.stdout is not None
    headers = {}
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("LSP server closed stdout unexpectedly")
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("utf-8").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers["content-length"])
    payload = proc.stdout.read(length)
    return json.loads(payload.decode("utf-8"))


def _collect_until_response(proc: subprocess.Popen, request_id: int) -> tuple[dict, list[dict]]:
    notifications = []
    while True:
        message = _read(proc)
        if message.get("id") == request_id:
            return message, notifications
        notifications.append(message)


def test_stdio_lsp_completion_and_diagnostics():
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.lsp.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": None, "capabilities": {}},
        })
        response, _ = _collect_until_response(proc, 1)
        assert response["result"]["capabilities"]["completionProvider"]["triggerCharacters"] == ["."]

        _send(proc, {
            "jsonrpc": "2.0",
            "method": "initialized",
            "params": {},
        })

        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": "file:///demo.mesa",
                    "languageId": "mesa",
                    "version": 1,
                    "text": """
struct Vec2 {
    x: f64,
    y: f64,

    fun len(self: Vec2) f64 {
        self.x
    }
}

fun demo(v: Vec2) f64 {
    v.x
}
""".strip(),
                }
            },
        })
        opened = _read(proc)
        assert opened["method"] == "textDocument/publishDiagnostics"
        assert opened["params"]["diagnostics"] == []

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "textDocument/completion",
            "params": {
                "textDocument": {"uri": "file:///demo.mesa"},
                "position": {"line": 10, "character": 7},
            },
        })
        completion, _ = _collect_until_response(proc, 2)
        labels = [item["label"] for item in completion["result"]["items"]]
        assert "len" in labels
        assert "x" in labels

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "mesa/semanticTokens",
            "params": {
                "textDocument": {"uri": "file:///demo.mesa"},
            },
        })
        semantic, _ = _collect_until_response(proc, 4)
        token_types = {item["tokenType"] for item in semantic["result"]}
        assert "type" in token_types
        assert "function" in token_types
        assert "property" in token_types

        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": "file:///demo.mesa", "version": 2},
                "contentChanges": [{
                    "text": """
fun broken() void {
    let x: i64 = true
}
""".strip()
                }]
            },
        })
        changed = _read(proc)
        assert changed["method"] == "textDocument/publishDiagnostics"
        assert changed["params"]["diagnostics"]

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "shutdown",
            "params": {},
        })
        _collect_until_response(proc, 3)
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": {}})
    finally:
        proc.kill()


def test_stdio_lsp_diagnostics_include_spans_and_hints():
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.lsp.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": None, "capabilities": {}},
        })
        _collect_until_response(proc, 1)
        _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": "file:///diag.mesa",
                    "languageId": "mesa",
                    "version": 1,
                    "text": """
struct Pair { first: i64 }

fun main() void {
    let p: Pair = .{first: 1}
    p.second
}
""".strip(),
                }
            },
        })
        opened = _read(proc)
        assert opened["method"] == "textDocument/publishDiagnostics"
        diagnostics = opened["params"]["diagnostics"]
        assert diagnostics
        diag = diagnostics[0]
        assert diag["range"]["end"]["character"] > diag["range"]["start"]["character"] + 1
        assert "available fields" in diag["message"]

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "shutdown",
            "params": {},
        })
        _collect_until_response(proc, 2)
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": {}})
    finally:
        proc.kill()


def test_stdio_lsp_diagnostics_include_codes_and_related_info():
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.lsp.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": None, "capabilities": {}},
        })
        _collect_until_response(proc, 1)
        _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": "file:///dup.mesa",
                    "languageId": "mesa",
                    "version": 1,
                    "text": """
fun main() void {
    let count = 1
    let count = 2
}
""".strip(),
                }
            },
        })
        opened = _read(proc)
        diagnostics = opened["params"]["diagnostics"]
        assert diagnostics
        diag = diagnostics[0]
        assert diag["code"] == "duplicate-definition"
        assert diag["relatedInformation"]
        assert "previous definition" in diag["relatedInformation"][0]["message"]

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "shutdown",
            "params": {},
        })
        _collect_until_response(proc, 2)
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": {}})
    finally:
        proc.kill()


def test_stdio_lsp_dedupes_repeated_diagnostics():
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.lsp.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": None, "capabilities": {}},
        })
        _collect_until_response(proc, 1)
        _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": "file:///dedupe.mesa",
                    "languageId": "mesa",
                    "version": 1,
                    "text": """
fun main() void {
    let count = 1
    println(cout)
}
""".strip(),
                }
            },
        })
        opened = _read(proc)
        diagnostics = opened["params"]["diagnostics"]
        assert len(diagnostics) == 1
        assert diagnostics[0]["code"] == "undefined-name"

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "shutdown",
            "params": {},
        })
        _collect_until_response(proc, 2)
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": {}})
    finally:
        proc.kill()


def test_stdio_lsp_unknown_type_highlights_type_token():
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.lsp.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": None, "capabilities": {}},
        })
        _collect_until_response(proc, 1)
        _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": "file:///unknown-type.mesa",
                    "languageId": "mesa",
                    "version": 1,
                    "text": "fun scale(x: flloat) float { return x }",
                }
            },
        })
        opened = _read(proc)
        diagnostics = opened["params"]["diagnostics"]
        assert len(diagnostics) == 1
        diag = diagnostics[0]
        assert diag["code"] == "unknown-type"
        assert diag["range"]["start"] == {"line": 0, "character": 13}
        assert diag["range"]["end"] == {"line": 0, "character": 19}

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "shutdown",
            "params": {},
        })
        _collect_until_response(proc, 2)
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": {}})
    finally:
        proc.kill()


def test_stdio_lsp_private_package_member_reports_private_hint(tmp_path):
    helper = tmp_path / "private_helper.mesa"
    helper.write_text(
        "pkg private_helper\n\n"
        "fun add(a: i64, b: i64) i64 {\n"
        "    a + b\n"
        "}\n"
    )
    main = tmp_path / "main_private_import_err.mesa"
    main_text = "from private_helper import add\n\nfun main() void {\n    println(add(1, 2))\n}\n"
    main.write_text(main_text)

    proc = subprocess.Popen(
        [sys.executable, "-m", "src.lsp.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": None, "capabilities": {}},
        })
        _collect_until_response(proc, 1)
        _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": main.resolve().as_uri(),
                    "languageId": "mesa",
                    "version": 1,
                    "text": main_text,
                }
            },
        })
        opened = _read(proc)
        diagnostics = opened["params"]["diagnostics"]
        assert len(diagnostics) == 1
        diag = diagnostics[0]
        assert diag["code"] == "private-member"
        assert "private_helper.add" in diag["message"]
        assert "add 'pub'" in diag["message"]

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "shutdown",
            "params": {},
        })
        _collect_until_response(proc, 2)
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": {}})
    finally:
        proc.kill()


def test_stdio_lsp_try_cleanup_with_handle_highlights_try_keyword():
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.lsp.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": None, "capabilities": {}},
        })
        _collect_until_response(proc, 1)
        _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": "file:///try-cleanup-with-handle.mesa",
                    "languageId": "mesa",
                    "version": 1,
                    "text": """
import mem

error E { Bad }

fun fail() E!i64 {
    return .Bad
}

fun bad() void {
    let arena = mem.ArenaAllocator.init(mem.PageBuffer.init(64))
    with arena : .reset {
        try fail()
    }
} handle |e| { }
""".strip(),
                }
            },
        })
        opened = _read(proc)
        diagnostics = opened["params"]["diagnostics"]
        assert len(diagnostics) == 1
        diag = diagnostics[0]
        assert diag["code"] == "try-cleanup-needs-local-handle"
        assert diag["range"]["start"] == {"line": 11, "character": 8}
        assert diag["range"]["end"] == {"line": 11, "character": 11}

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "shutdown",
            "params": {},
        })
        _collect_until_response(proc, 2)
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": {}})
    finally:
        proc.kill()


def test_stdio_lsp_hover_for_with_keyword():
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.lsp.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": None, "capabilities": {}},
        })
        response, _ = _collect_until_response(proc, 1)
        assert response["result"]["capabilities"]["hoverProvider"] is True
        _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        text = """
from mem import ArenaAllocator, PageBuffer

fun main() void {
    let arena = ArenaAllocator.init(PageBuffer.init(64))
    with arena : .reset {
        println(1)
    }
}
""".strip()
        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": "file:///hover-with.mesa",
                    "languageId": "mesa",
                    "version": 1,
                    "text": text,
                }
            },
        })
        _read(proc)

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "textDocument/hover",
            "params": {
                "textDocument": {"uri": "file:///hover-with.mesa"},
                "position": {"line": 4, "character": 4},
            },
        })
        hover, _ = _collect_until_response(proc, 2)
        assert "allocator context" in hover["result"]["contents"]["value"]
        assert hover["result"]["range"]["start"] == {"line": 4, "character": 4}

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "shutdown",
            "params": {},
        })
        _collect_until_response(proc, 3)
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": {}})
    finally:
        proc.kill()


def test_stdio_lsp_hover_for_allocator_type():
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.lsp.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": None, "capabilities": {}},
        })
        _collect_until_response(proc, 1)
        _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        text = """
from mem import ArenaAllocator, PageBuffer

fun main() void {
    let arena = ArenaAllocator.init(PageBuffer.init(64))
    println(1)
}
""".strip()
        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": "file:///hover-alloc.mesa",
                    "languageId": "mesa",
                    "version": 1,
                    "text": text,
                }
            },
        })
        _read(proc)

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "textDocument/hover",
            "params": {
                "textDocument": {"uri": "file:///hover-alloc.mesa"},
                "position": {"line": 3, "character": 16},
            },
        })
        hover, _ = _collect_until_response(proc, 2)
        assert "ArenaAllocator" in hover["result"]["contents"]["value"]
        assert hover["result"]["range"]["start"] == {"line": 3, "character": 16}

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "shutdown",
            "params": {},
        })
        _collect_until_response(proc, 3)
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": {}})
    finally:
        proc.kill()


def test_stdio_lsp_resolves_bare_std_imports_inside_build_project(tmp_path):
    build = tmp_path / "build.mesa"
    build.write_text(
        """
pub fun build(b: *build.Build) void {
    let std = b.addPackage("std", root = "@std")
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry, imports = .{ std })
    b.install(app)
}
""".strip() + "\n"
    )
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    main = src_dir / "main.mesa"
    text = """
import mem

fun main() void {
    let arena = mem.ArenaAllocator.init(mem.PageBuffer.init(64))
    with arena : .reset {
        println(1)
    }
}
""".strip()
    main.write_text(text + "\n")

    proc = subprocess.Popen(
        [sys.executable, "-m", "src.lsp.server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"processId": None, "rootUri": tmp_path.as_uri(), "capabilities": {}},
        })
        _collect_until_response(proc, 1)
        _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        _send(proc, {
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": main.resolve().as_uri(),
                    "languageId": "mesa",
                    "version": 1,
                    "text": text,
                }
            },
        })
        opened = _read(proc)
        assert opened["method"] == "textDocument/publishDiagnostics"
        assert opened["params"]["diagnostics"] == []

        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "shutdown",
            "params": {},
        })
        _collect_until_response(proc, 2)
        _send(proc, {"jsonrpc": "2.0", "method": "exit", "params": {}})
    finally:
        proc.kill()
