from __future__ import annotations

from pathlib import Path
import subprocess

from src.meta import build_document_meta


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n")


def _mesa(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(REPO_ROOT / "mesa.py"), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def test_build_errors_without_cwd_build_file(tmp_path: Path):
    proc = _mesa(tmp_path, "build")
    assert proc.returncode == 1
    assert "build.mesa not found in the current directory" in proc.stderr
    assert "mesa init" in proc.stderr


def test_test_errors_without_cwd_build_file(tmp_path: Path):
    proc = _mesa(tmp_path, "test")
    assert proc.returncode == 1
    assert "build.mesa not found in the current directory" in proc.stderr
    assert "mesa init" in proc.stderr


def test_mesa_init_creates_runnable_project(tmp_path: Path):
    init = _mesa(tmp_path, "init")
    assert init.returncode == 0, init.stderr
    assert (tmp_path / "build.mesa").is_file()
    assert (tmp_path / "src" / "main.mesa").is_file()
    build_text = (tmp_path / "build.mesa").read_text()
    assert "pub fun build(b: *build.Build) void {" in build_text
    assert 'b.addPackage("std", root = "@std")' in build_text
    assert "imports = .{ std }" in build_text

    run = _mesa(tmp_path, "run")
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "Hello, Mesa!"
    assert (tmp_path / "targets" / tmp_path.name).is_file()


def test_build_uses_flat_package_root_and_pkgless_local_subtree(tmp_path: Path):
    _write(tmp_path / "build.mesa", """
pub fun build(b: *build.Build) void {
    let math = b.addPackage("math", root = "src/math")
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry, imports = .{ math })
    b.install(app)
}
""")
    _write(tmp_path / "src" / "math" / "types" / "vector.mesa", """
pkg math

pub struct Vec2 {
    x: f64,
    y: f64,
}
""")
    _write(tmp_path / "src" / "math" / "math.pkg", """
pkg math

from "types/vector" export Vec2
""")
    _write(tmp_path / "src" / "helpers" / "numbers.mesa", """
fun answer() i64 {
    42
}
""")
    _write(tmp_path / "src" / "main.mesa", """
from math import Vec2

fun main() void {
    let v: Vec2 = .{ x: 1.0, y: 2.0 }
    println(answer())
}
""")

    proc = _mesa(tmp_path, "run")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "42"
    assert (tmp_path / "targets" / "app").is_file()


def test_mesa_test_runs_test_blocks_and_pretty_prints_report(tmp_path: Path):
    _write(tmp_path / "build.mesa", """
pub fun build(b: *build.Build) void {
    let std = b.addPackage("std", root = "@std")
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry, imports = .{ std })
    b.install(app)
}
""")
    _write(tmp_path / "src" / "main.mesa", """
import mem

fun main() void {
    println("hello")
}

test "arena init works" {
    let arena = mem.ArenaAllocator.init(mem.PageBuffer.init(64))
    with arena : .reset {
        @assert(true)
    }
}

test "failing assertion is reported" {
    @assert(1 == 2)
}
""")

    proc = _mesa(tmp_path, "test")
    assert proc.returncode == 1, proc.stderr
    assert "test arena init works" in proc.stdout
    assert "test failing assertion is reported" in proc.stdout
    assert "fail at" in proc.stdout
    assert "result: 1 passed; 1 failed" in proc.stdout
    assert (tmp_path / "targets" / "app-tests").is_file()


def test_build_writes_default_target_into_targets_directory(tmp_path: Path):
    _write(tmp_path / "build.mesa", """
pub fun build(b: *build.Build) void {
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("sample-app", entry = entry)
    b.install(app)
}
""")
    _write(tmp_path / "src" / "main.mesa", """
fun main() void {
    println("ok")
}
""")

    proc = _mesa(tmp_path, "build")
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "targets" / "sample-app").is_file()
    assert "targets/sample-app" in proc.stderr


def test_pkg_add_updates_build_file_and_is_idempotent(tmp_path: Path):
    _write(tmp_path / "build.mesa", """
pub fun build(b: *build.Build) void {
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry)
    b.install(app)
}
""")

    first = _mesa(tmp_path, "pkg", "add", "src/sim", "--name", "sim")
    assert first.returncode == 0, first.stderr
    build_text = (tmp_path / "build.mesa").read_text()
    assert 'let sim = b.addPackage("sim", root = "src/sim")' in build_text
    assert 'let app = b.addExecutable("app", entry = app_entry, imports = .{ sim })' in build_text
    assert (tmp_path / "src" / "sim").is_dir()
    assert (tmp_path / "src" / "sim" / "sim.pkg").is_file()

    second = _mesa(tmp_path, "pkg", "add", "src/sim", "--name", "sim")
    assert second.returncode == 0, second.stderr
    build_text = (tmp_path / "build.mesa").read_text()
    assert build_text.count('let sim = b.addPackage("sim", root = "src/sim")') == 1


def test_pkg_add_mangles_binding_name_when_package_name_needs_it(tmp_path: Path):
    _write(tmp_path / "build.mesa", """
pub fun build(b: *build.Build) void {
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry)
    b.install(app)
}
""")

    proc = _mesa(tmp_path, "pkg", "add", "src/physics", "--name", "sim.physics")
    assert proc.returncode == 0, proc.stderr
    build_text = (tmp_path / "build.mesa").read_text()
    assert 'let sim_physics = b.addPackage("sim.physics", root = "src/physics")' in build_text
    assert 'imports = .{ sim_physics }' in build_text
    assert (tmp_path / "src" / "physics" / "physics.pkg").is_file()


def test_build_rejects_reserved_std_package_names(tmp_path: Path):
    _write(tmp_path / "build.mesa", """
pub fun build(b: *build.Build) void {
    let mem = b.addPackage("mem", root = "src/mem")
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry, imports = .{ mem })
    b.install(app)
}
""")
    _write(tmp_path / "src" / "main.mesa", """
fun main() void {
    println("ok")
}
""")

    proc = _mesa(tmp_path, "build")
    assert proc.returncode == 1
    assert "reserved for the standard library" in proc.stderr


def test_named_package_root_resolves_prefixed_imports(tmp_path: Path):
    _write(tmp_path / "build.mesa", """
pub fun build(b: *build.Build) void {
    let sim_math = b.addPackage("sim.math", root = "src/sim/math")
    let sim_physics = b.addPackage("sim.physics", root = "src/sim/physics")
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry, imports = .{ sim_math, sim_physics })
    b.install(app)
}
""")
    _write(tmp_path / "src" / "sim" / "math" / "vector.mesa", """
pkg math

pub struct Vec2 {
    x: f64,
    y: f64,
}
""")
    _write(tmp_path / "src" / "sim" / "math" / "math.pkg", """
pkg math

from "vector" export Vec2
""")
    _write(tmp_path / "src" / "sim" / "physics" / "api.mesa", """
pkg physics

from math import Vec2

pub fun zero() Vec2 {
    .{ x: 0.0, y: 0.0 }
}
""")
    _write(tmp_path / "src" / "sim" / "physics" / "physics.pkg", """
pkg physics

from "api" export zero
""")
    _write(tmp_path / "src" / "main.mesa", """
from sim.physics import zero

fun main() void {
    let v = zero()
    println(1)
}
""")

    proc = _mesa(tmp_path, "run")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "1"


def test_meta_uses_enclosing_build_file_for_editor_diagnostics(tmp_path: Path):
    _write(tmp_path / "build.mesa", """
pub fun build(b: *build.Build) void {
    let math = b.addPackage("math", root = "src/math")
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry, imports = .{ math })
    b.install(app)
}
""")
    _write(tmp_path / "src" / "math" / "vector.mesa", """
pkg math

pub struct Vec2 {
    x: f64,
    y: f64,
}
""")
    _write(tmp_path / "src" / "math" / "math.pkg", """
pkg math

from "vector" export Vec2
""")
    _write(tmp_path / "src" / "main.mesa", """
from math import Vec2

fun main() void {
    let v: Vec2 = .{ x: 0.0, y: 0.0 }
    println(1)
}
""")

    meta = build_document_meta((tmp_path / "src" / "main.mesa").read_text(), source_path=str(tmp_path / "src" / "main.mesa"))
    assert meta.parse_succeeded
    assert meta.typecheck_succeeded, [d.message for d in meta.diagnostics]


def test_meta_treats_build_file_as_build_script_not_normal_source(tmp_path: Path):
    _write(tmp_path / "build.mesa", """
pub fun build(b: *build.Build) void {
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry)
    b.install(app)
}
""")
    meta = build_document_meta((tmp_path / "build.mesa").read_text(), source_path=str(tmp_path / "build.mesa"))
    assert meta.parse_succeeded
    assert not meta.diagnostics, [d.message for d in meta.diagnostics]
