from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from src.frontend import build_frontend_state_for_path
from src.types import format_type_for_user


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n")


def test_pkg_opaque_export_hides_fields_from_importers(tmp_path: Path):
    root = tmp_path / "sim"

    _write(root / "physics" / "world" / "state.mesa", """
pkg physics.world

pub struct Body {
    value: i64,
}

pub fun make() Body {
    .{ value: 42 }
}
""")
    _write(root / "physics" / "physics.pkg", """
pkg physics

opaque from "world/state" export Body
from "world/state" export make
""")

    _write(root / "game" / "main.mesa", """
pkg game

from physics import Body, make

fun main() void {
    let b: Body = make()
    println(b.value)
}
""")

    state = build_frontend_state_for_path(str(root / "game" / "main.mesa"))
    errors = state.diags.all_errors()
    assert errors
    assert any("has no field or method 'value'" in d.message for d in errors)


def test_pkg_import_does_not_leak_non_exported_pub_names(tmp_path: Path):
    root = tmp_path / "sim"

    _write(root / "physics" / "world" / "state.mesa", """
pkg physics.world

pub struct Body {
    value: i64,
}

pub fun makeBody() Body {
    .{ value: 1 }
}
""")
    _write(root / "physics" / "physics.pkg", """
pkg physics

from "world/state" export Body
""")

    _write(root / "game" / "main.mesa", """
pkg game

from physics import Body

fun main() void {
    let b: Body = makeBody()
    println(1)
}
""")

    state = build_frontend_state_for_path(str(root / "game" / "main.mesa"))
    errors = state.diags.all_errors()
    assert errors
    assert any("undefined name 'makeBody'" in d.message for d in errors)


def test_pkg_export_source_must_declare_path_derived_pkg(tmp_path: Path):
    root = tmp_path / "sim"

    _write(root / "physics" / "world" / "state.mesa", """
pkg physics

pub struct Body {
    value: i64,
}
""")
    _write(root / "physics" / "physics.pkg", """
pkg physics

from "world/state" export Body
""")
    _write(root / "game" / "main.mesa", """
pkg game

from physics import Body
""")

    state = build_frontend_state_for_path(str(root / "game" / "main.mesa"))
    errors = state.diags.all_errors()
    assert errors
    assert any("must declare 'pkg physics.world'" in d.message for d in errors)


def test_std_bare_package_names_are_reserved(tmp_path: Path):
    root = tmp_path / "app"
    _write(root / "mem" / "types.mesa", """
pkg mem

pub struct BadIdea {
    value: i64,
}
""")
    state = build_frontend_state_for_path(str(root / "mem" / "types.mesa"))
    errors = state.diags.all_errors()
    assert errors
    assert any("reserved for the standard library" in d.message for d in errors)


def test_fmt_package_name_is_no_longer_reserved(tmp_path: Path):
    root = tmp_path / "app"
    _write(root / "fmt" / "types.mesa", """
pkg fmt

pub struct Formatter {
    value: i64,
}
""")
    state = build_frontend_state_for_path(str(root / "fmt" / "types.mesa"))
    errors = state.diags.all_errors()
    assert not errors


def test_cli_file_mode_supports_len_and_cap_builtins(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
fun main() void {
    let v = vec[1, 2, 3]
    let s: str = "hello"
    println(len(v))
    println(cap(v))
    println(len(s))
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "3\n3\n5"


def test_cli_file_mode_import_fmt_fails_without_std_package(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
import fmt

fun main() void {
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "package not found for import 'fmt'" in proc.stderr


def test_cli_file_mode_supports_source_backed_mem_package(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
import mem

struct Node {
    val: i64,
}

fun main() void {
    let arena = mem.ArenaAllocator.init(mem.PageBuffer.init(128))
    with arena : .reset {
        let p: *Node = .{ val: 7 }
        println(p.val)
    }
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "7"


def test_cli_file_mode_supports_pool_allocator(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
from mem import ArenaAllocator, PageBuffer, PoolAllocator, poolFreeSlot

struct Node {
    val: i64,
}

fun main() void {
    let arena = ArenaAllocator.init(PageBuffer.init(256))
    let seed: Node = .{ val: 0 }
    let var pool: PoolAllocator[Node] = PoolAllocator.init(arena, 2, seed)
    with pool {
        let first: *Node = .{ val: 7 }
        let second: *Node = .{ val: 9 }
        println(first.val + second.val)
        poolFreeSlot(&pool, first)
        let third: *Node = .{ val: 11 }
        println(third.val)
    }
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "16\n11"


def test_cli_file_mode_supports_debug_allocator(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
from mem import ArenaAllocator, PageBuffer, DebugAllocator

struct Node {
    val: i64,
}

fun main() void {
    let arena = ArenaAllocator.init(PageBuffer.init(256))
    let var dbg = DebugAllocator.init(arena)
    with dbg {
        let p: *Node = .{ val: 7 }
        println(p.val)
    }
    dbg.print()
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("7\nDebugAllocator allocs=1")


def test_cli_file_mode_supports_source_backed_io_package(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
import io

fun main() void {
    io.println("hello")
    io.writeln(io.stderr(), "oops")
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "hello\n"
    assert proc.stderr.endswith("oops\n")


def test_cli_file_mode_accepts_user_defined_io_writer(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
import io
from io import Writer

struct Echo {}

def Writer for Echo {
    fun write(self: Echo, text: str) void {
        io.print(text)
    }
}

fun main() void {
    let out: Echo = .{}
    io.writeln(out, "hello")
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "hello\n"


def test_cli_file_mode_supports_basic_io_file_round_trip(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    data_path = tmp_path / "message.txt"
    main = tmp_path / "main.mesa"
    _write(main, f"""
import io

fun main() void {{
    if io.openWrite("{data_path}") |out| {{
        io.writeln(out, "alpha")
        @assert(out.flush())
        @assert(out.close())
    }} else {{
        @panic("openWrite failed")
    }}

    if io.openRead("{data_path}") |input| {{
        let text = input.readAll()
        @assert(input.close())
        io.write(io.stdout(), text)
    }} else {{
        @panic("openRead failed")
    }}
}}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "alpha\n"


def test_cli_file_mode_reports_missing_io_file_as_none(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    missing_path = tmp_path / "missing.txt"
    main = tmp_path / "main.mesa"
    _write(main, f"""
import io

fun main() void {{
    if io.openRead("{missing_path}") |input| {{
        input.close()
        println("unexpected")
    }} else {{
        println("missing")
    }}
}}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "missing\n"


def test_cli_file_mode_hides_raw_buffer_intrinsics_from_user_code(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
import mem

fun main() void {
    let page = @pageAlloc(@pageSize())
    @pageFree(page, @pageSize())
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "internal std.mem intrinsic" in proc.stderr


def test_cli_file_mode_hides_allocator_intrinsics_from_user_code(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
import mem

fun main() void {
    let arena = mem.ArenaAllocator.init(mem.PageBuffer.init(128))
    let a = @alloc(arena, 4, 1)
    println(a)
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "internal std.mem intrinsic" in proc.stderr


def test_cli_file_mode_no_longer_exposes_io_intrinsics(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
fun main() void {
    @stdoutWrite("hello")
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "undefined name '@stdoutWrite'" in proc.stderr


def test_cli_file_mode_gc_preserves_rooted_pointer_locals(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
struct Node {
    val: i64,
}

fun main() void {
    let head: *Node = .{ val: 7 }
    for i = 0...50000 {
        let temp: *Node = .{ val: i }
        if temp.val < 0 {
            println(0)
        }
    }
    println(head.val)
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--run"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.endswith("7\n")


def test_cli_emit_c_includes_allocator_context_push_helpers(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
from mem import ArenaAllocator, PageBuffer

struct Node {
    val: i64,
}

fun main() void {
    let arena = ArenaAllocator.init(PageBuffer.init(128))
    with arena : .reset {
        let p: *Node = .{ val: 1 }
        println(p.val)
    }
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--emit-c"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "_mesa_allocctx_alloc_mesa__std__mem__ArenaAllocator" in proc.stdout
    assert "mesa_allocctx_push(&arena, _mesa_allocctx_alloc_mesa__std__mem__ArenaAllocator);" in proc.stdout
    assert "mesa_allocctx_pop();" in proc.stdout


def test_ffi_callback_alias_preserves_c_abi_in_rendered_type(tmp_path: Path):
    root = tmp_path / "ffi_app"
    _write(root / "main.mesa", """
import ffi
import libc

@extern(libc)
opaque type FILE

@extern(libc)
fun fopen(path: *ffi.c_char, mode: *ffi.c_char) *FILE

type CompareFn = [.c]fun(*ffi.c_void, *ffi.c_void) ffi.c_int

fun main() void {
}
""")

    state = build_frontend_state_for_path(
        str(root / "main.mesa"),
        foreign_namespaces=["libc"],
    )

    assert state.parse_succeeded
    assert state.typecheck_succeeded, [d.message for d in state.diags.all_errors()]
    compare_fn = state.env.lookup_type("CompareFn")
    assert compare_fn is not None
    assert format_type_for_user(compare_fn) == "[.c]fun(*void, *void) i32"


def test_ffi_callback_types_distinguish_plain_and_c_abi_functions(tmp_path: Path):
    root = tmp_path / "ffi_app"
    _write(root / "main.mesa", """
import ffi

type PlainCompareFn = fun(*ffi.c_void, *ffi.c_void) ffi.c_int
type CompareFn = [.c]fun(*ffi.c_void, *ffi.c_void) ffi.c_int

fun takesPlain(cmp: PlainCompareFn) void {
}

fun demo(cmp: CompareFn) void {
    takesPlain(cmp)
}
""")

    state = build_frontend_state_for_path(str(root / "main.mesa"))
    errors = state.diags.all_errors()
    assert errors
    assert any("expected fun(*void, *void) i32, got [.c]fun(*void, *void) i32" in d.message for d in errors)
    
def test_cli_emit_c_uses_linked_gc_runtime(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
struct Node {
    val: i64,
}

fun main() void {
    let p: *Node = .{ val: 1 }
    println(p.val)
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--emit-c"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert '#include "mesa_gc_runtime.h"' in proc.stdout
    assert "mesa_gc_alloc(" in proc.stdout
    assert "typedef struct Mesa_GC_Obj {" not in proc.stdout
    assert "static void mesa_gc_collect(void)" not in proc.stdout


def test_cli_emit_c_covers_representative_alpha_markers(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    main = tmp_path / "main.mesa"
    _write(main, """
error BadError { Bad }

fun mightFail(ok: bool) BadError!i64 {
    if ok {
        return 7
    }
    return BadError.Bad
}

fun main() void {
    defer println("done")
    let value = mightFail(true) catch { _ => 0 }
    println(value)
}
""")

    proc = subprocess.run(
        [sys.executable, str(repo_root / "src" / "mesa.py"), str(main), "--emit-c"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    emitted = proc.stdout
    assert '#include "mesa_gc_runtime.h"' in emitted
    assert "defer" in emitted or "done" in emitted
    assert "Bad" in emitted
