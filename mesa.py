#!/usr/bin/env python3
"""
mesa — Mesa language compiler frontend.

Usage:
    mesa <file.mesa>                # compile to binary (./out by default)
    mesa <file.mesa> -o <name>     # compile to named binary
    mesa <file.mesa> --emit-c      # print C source to stdout
    mesa <file.mesa> --emit-c -o x.c  # write C source to file
    mesa <file.mesa> --check       # type check only, no output
    mesa --help

Compilation pipeline:
    .mesa  →  parse  →  type check  →  analysis  →  C codegen  →  cc  →  binary

Requires cc/gcc to link (installed by default on macOS and Linux).
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.frontend  import build_frontend_state_for_path
from src.analysis  import analyse
from src.ccodegen  import CCodegen
from src.ast       import FunDecl, Program, TestDecl, TyVoid, Visibility
from src.buildsys  import (
    BuildPlanError,
    create_default_plan,
    ensure_package_in_plan,
    find_build_file,
    infer_package_name,
    load_build_plan,
    render_build_plan,
)
from src.stdlib import resolve_package_root_path


# ══════════════════════════════════════════════════════════════
# Colours
# ══════════════════════════════════════════════════════════════

USE_COLOR = sys.stderr.isatty()

def _c(code, text): return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def red(s):    return _c("91", s)
def yellow(s): return _c("93", s)
def green(s):  return _c("92", s)
def bold(s):   return _c("1",  s)
def cyan(s):   return _c("96", s)
def dim(s):    return _c("2",  s)


# ══════════════════════════════════════════════════════════════
# Error formatting
# ══════════════════════════════════════════════════════════════

def _show_source_line(source_lines, line, col):
    if line < 1 or line > len(source_lines):
        return
    src_line = source_lines[line - 1]
    print(f"  {dim(str(line))}  {src_line}", file=sys.stderr)
    if col > 0:
        arrow = " " * (col + 2) + "^"
        print(f"  {red(arrow)}", file=sys.stderr)

def _show_span(source_lines, span):
    if span is None:
        return
    start = span.start
    end = span.end
    if start.line != end.line:
        _show_source_line(source_lines, start.line, start.col)
        return
    if start.line < 1 or start.line > len(source_lines):
        return
    src_line = source_lines[start.line - 1]
    print(f"  {dim(str(start.line))}  {src_line}", file=sys.stderr)
    width = max((end.col - start.col), 1)
    arrow = " " * (start.col + 2) + "^" * width
    print(f"  {red(arrow)}", file=sys.stderr)

def print_errors(source, diags):
    lines = source.split("\n")
    errors = diags.all_errors()
    if not errors:
        return
    print(file=sys.stderr)
    for d in errors:
        loc = f"[{d.line}:{d.col}]" if d.line else ""
        code = f" {dim('(' + d.code + ')')}" if getattr(d, "code", None) else ""
        print(f"{red('error')} {dim(loc)} {d.message}{code}", file=sys.stderr)
        if d.span is not None:
            _show_span(lines, d.span)
        elif d.line:
            _show_source_line(lines, d.line, d.col)
        if d.hint:
            print(f"  {cyan('hint:')} {d.hint}", file=sys.stderr)
        for rel in getattr(d, "related", []) or []:
            print(f"  {cyan('note:')} {rel.message}", file=sys.stderr)
            _show_span(lines, rel.span)
    print(file=sys.stderr)


# ══════════════════════════════════════════════════════════════
# C compiler detection
# ══════════════════════════════════════════════════════════════

def _find_cc():
    for name in ("cc", "gcc", "clang", "g++"):
        try:
            r = subprocess.run(["which", name], capture_output=True, text=True)
            if r.returncode == 0:
                return name
        except FileNotFoundError:
            pass
    return None

def _compile_c_to_object(c_path, obj_path, verbose=False):
    cc = _find_cc()
    if not cc:
        return False
    cmd = [cc, "-c", c_path, "-o", obj_path, "-O2", "-std=c99"]
    if verbose:
        print(dim(f"  → {' '.join(cmd)}"), file=sys.stderr)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        return True
    print(red(f"compiler error:\n{r.stderr}"), file=sys.stderr)
    return False


def _link_objects_to_binary(obj_paths, out_path, verbose=False):
    cc = _find_cc()
    if not cc:
        return False
    cmd = [cc, *obj_paths, "-o", out_path, "-lm", "-O2", "-std=c99"]
    if verbose:
        print(dim(f"  → {' '.join(cmd)}"), file=sys.stderr)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        return True
    print(red(f"linker error:\n{r.stderr}"), file=sys.stderr)
    return False


# ══════════════════════════════════════════════════════════════
# Compilation pipeline
# ══════════════════════════════════════════════════════════════

def _display_run_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    if path.startswith("./") or path.startswith("../"):
        return path
    return f"./{path}"


def _ensure_output_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _run_binary(path: str, verbose: bool = False) -> int:
    run_path = _display_run_path(path)
    if verbose:
        print(dim(f"  running {run_path}..."), file=sys.stderr)
    try:
        r = subprocess.run([run_path])
    except FileNotFoundError:
        print(red(f"error: binary not found: {run_path}"), file=sys.stderr)
        return 1
    except OSError as e:
        print(red(f"error running binary: {e}"), file=sys.stderr)
        return 1
    return r.returncode


def _has_entrypoint(program) -> bool:
    for decl in getattr(program, "decls", []):
        if isinstance(decl, FunDecl) and decl.name == "main":
            return True
    return False


def _group_program_by_source_file(program, fallback_source_path):
    groups = {}
    order = []
    for decl in getattr(program, "decls", []):
        source_file = getattr(decl, "_source_file", None) or os.path.abspath(fallback_source_path)
        if getattr(decl, "_source_file", None) is None:
            setattr(decl, "_source_file", source_file)
        if source_file not in groups:
            groups[source_file] = []
            order.append(source_file)
        groups[source_file].append(decl)
    return [(source_file, Program(pkg=program.pkg, imports=[], decls=groups[source_file])) for source_file in order]


def _escape_c_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _lower_tests_for_codegen(program: Program):
    decls = []
    tests = []
    test_index = 0
    for decl in getattr(program, "decls", []):
        if isinstance(decl, TestDecl):
            fn = FunDecl(
                vis=Visibility.PRIVATE,
                attrs=[],
                name=f"__mesa_test_{test_index}",
                params=[],
                ret=TyVoid(),
                body=decl.body,
                span=decl.span,
            )
            setattr(fn, "_source_file", getattr(decl, "_source_file", None))
            setattr(fn, "_pkg_path", getattr(decl, "_pkg_path", None))
            setattr(fn, "_c_name", f"mesa_test_{test_index}")
            setattr(fn, "_generated_test", True)
            decls.append(fn)
            tests.append((decl.name, getattr(fn, "_c_name")))
            test_index += 1
            continue
        if isinstance(decl, FunDecl) and decl.name == "main":
            continue
        decls.append(decl)
    return Program(pkg=program.pkg, imports=program.imports, decls=decls), tests


def _emit_test_runner_source(header_name: str, tests: list[tuple[str, str]]) -> str:
    lines = [f'#include "{header_name}"', ""]
    lines.append("int main(void) {")
    for test_name, c_name in tests:
        lines.append(f'    mesa_test_begin("{_escape_c_string(test_name)}");')
        lines.append(f"    {c_name}();")
        lines.append("    mesa_test_end();")
    lines.append("    return mesa_test_finish();")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def compile_file(
    source_path,
    output_path,
    emit_c,
    check_only,
    run_binary,
    verbose,
    timings,
    *,
    package_roots=None,
    foreign_namespaces=None,
    local_root=None,
):
    try:
        source = open(source_path).read()
    except FileNotFoundError:
        print(red(f"error: file not found: {source_path}"), file=sys.stderr)
        return 1
    except OSError as e:
        print(red(f"error: {e}"), file=sys.stderr)
        return 1

    module_name = os.path.splitext(os.path.basename(source_path))[0]
    t0 = time.perf_counter()

    def elapsed(label):
        if timings:
            ms = (time.perf_counter() - t0) * 1000
            print(dim(f"  {label:<20} {ms:.1f}ms"), file=sys.stderr)

    if verbose: print(dim("  parsing..."), file=sys.stderr)
    try:
        state = build_frontend_state_for_path(
            source_path,
            package_roots=package_roots,
            foreign_namespaces=foreign_namespaces,
            local_root=local_root,
        )
    except Exception as e:
        print(red(f"internal parse error: {e}"), file=sys.stderr)
        if verbose:
            import traceback; traceback.print_exc()
        return 1

    if state.tokenize_error is not None:
        print(red(f"tokenize error: {state.tokenize_error}"), file=sys.stderr)
        return 1
    if state.parse_error is not None:
        print(red(f"parse error: {state.parse_error}"), file=sys.stderr)
        return 1

    prog = state.program
    env = state.env
    diags = state.diags
    elapsed("parse")
    elapsed("type check")

    if diags.has_errors():
        n = len(diags.all_errors())
        print(red(bold(f"type error — {n} error{'s' if n > 1 else ''}")), file=sys.stderr)
        print_errors(source, diags)
        return 1

    if not emit_c and not check_only and not _has_entrypoint(prog):
        print(red("error: no entrypoint found"), file=sys.stderr)
        print(cyan("hint:") + " add `fun main() void { ... }` to build an executable", file=sys.stderr)
        return 1

    if prog is not None:
        root_pkg_path = prog.pkg.path if prog.pkg is not None else None
        for decl in getattr(prog, "decls", []):
            if isinstance(decl, FunDecl) and decl.name == "main" and getattr(decl, "_pkg_path", None) == root_pkg_path:
                setattr(decl, "_is_entrypoint", True)

    # ── Analysis ─────────────────────────────────────────────
    if verbose: print(dim("  analysis..."), file=sys.stderr)
    try:
        layout = analyse(prog, env)
    except Exception as e:
        print(red(f"internal analysis error: {e}"), file=sys.stderr)
        if verbose:
            import traceback; traceback.print_exc()
        return 1

    if diags.has_errors():
        n = len(diags.all_errors())
        print(red(bold(f"analysis error — {n} error{'s' if n > 1 else ''}")), file=sys.stderr)
        print_errors(source, diags)
        return 1
    elapsed("analysis")

    if check_only:
        print(green("✓ OK"), file=sys.stderr)
        return 0

    # ── C Codegen ────────────────────────────────────────────
    if verbose: print(dim("  generating C..."), file=sys.stderr)
    try:
        cg = CCodegen(env, layout)
        cg.emit_all(prog)
        c_source = cg.output()
    except Exception as e:
        print(red(f"internal codegen error: {e}"), file=sys.stderr)
        if verbose:
            import traceback; traceback.print_exc()
        return 1
    elapsed("codegen")

    # ── Output ───────────────────────────────────────────────
    if emit_c:
        if output_path:
            _ensure_output_parent(output_path)
            open(output_path, "w").write(c_source)
            print(green(f"✓ {output_path}"), file=sys.stderr)
        else:
            print(c_source)
        return 0

    out = output_path or "out"
    _ensure_output_parent(out)
    with tempfile.TemporaryDirectory(prefix=f"{module_name}_build_") as tmpdir:
        header_path = os.path.join(tmpdir, "mesa_shared.h")
        runtime_path = os.path.join(tmpdir, "mesa_runtime_state.c")
        object_paths = []

        header_cg = CCodegen(env, layout)
        header_cg.emit_support_header(prog)
        open(header_path, "w").write(header_cg.output())
        pending_mono = list(header_cg._pending_mono)

        runtime_cg = CCodegen(env, layout)
        runtime_cg.emit_runtime_state_source(os.path.basename(header_path))
        open(runtime_path, "w").write(runtime_cg.output())
        runtime_obj = os.path.join(tmpdir, "mesa_runtime_state.o")
        if not _compile_c_to_object(runtime_path, runtime_obj, verbose=verbose):
            return 1
        object_paths.append(runtime_obj)

        unit_groups = _group_program_by_source_file(prog, source_path)
        for index, (_unit_source, unit_program) in enumerate(unit_groups):
            unit_c_path = os.path.join(tmpdir, f"unit_{index}.c")
            unit_obj_path = os.path.join(tmpdir, f"unit_{index}.o")
            unit_cg = CCodegen(env, layout)
            unit_cg.emit_unit_source(unit_program, os.path.basename(header_path), pending_mono=pending_mono)
            open(unit_c_path, "w").write(unit_cg.output())
            if not _compile_c_to_object(unit_c_path, unit_obj_path, verbose=verbose):
                return 1
            object_paths.append(unit_obj_path)

        if verbose:
            print(dim(f"  linking {len(object_paths)} objects → {out}..."), file=sys.stderr)
        ok = _link_objects_to_binary(object_paths, out, verbose=verbose)
        if ok:
            elapsed("compile")
            size = os.path.getsize(out)
            print(green(f"✓ {out}") + dim(f"  ({size:,} bytes)"), file=sys.stderr)
            if run_binary:
                return _run_binary(out, verbose=verbose)
            print(dim(f"  run with {_display_run_path(out)}"), file=sys.stderr)
            return 0
        return 1


def compile_tests(
    source_path,
    output_path,
    verbose,
    timings,
    *,
    package_roots=None,
    foreign_namespaces=None,
    local_root=None,
):
    try:
        source = open(source_path).read()
    except FileNotFoundError:
        print(red(f"error: file not found: {source_path}"), file=sys.stderr)
        return 1
    except OSError as e:
        print(red(f"error: {e}"), file=sys.stderr)
        return 1

    module_name = os.path.splitext(os.path.basename(source_path))[0]
    t0 = time.perf_counter()

    def elapsed(label):
        if timings:
            ms = (time.perf_counter() - t0) * 1000
            print(dim(f"  {label:<20} {ms:.1f}ms"), file=sys.stderr)

    if verbose:
        print(dim("  parsing..."), file=sys.stderr)
    try:
        state = build_frontend_state_for_path(
            source_path,
            package_roots=package_roots,
            foreign_namespaces=foreign_namespaces,
            local_root=local_root,
        )
    except Exception as e:
        print(red(f"internal parse error: {e}"), file=sys.stderr)
        if verbose:
            import traceback; traceback.print_exc()
        return 1

    if state.tokenize_error is not None:
        print(red(f"tokenize error: {state.tokenize_error}"), file=sys.stderr)
        return 1
    if state.parse_error is not None:
        print(red(f"parse error: {state.parse_error}"), file=sys.stderr)
        return 1

    prog = state.program
    env = state.env
    diags = state.diags
    elapsed("parse")
    elapsed("type check")

    if diags.has_errors():
        n = len(diags.all_errors())
        print(red(bold(f"type error — {n} error{'s' if n > 1 else ''}")), file=sys.stderr)
        print_errors(source, diags)
        return 1

    if verbose:
        print(dim("  analysis..."), file=sys.stderr)
    try:
        layout = analyse(prog, env)
    except Exception as e:
        print(red(f"internal analysis error: {e}"), file=sys.stderr)
        if verbose:
            import traceback; traceback.print_exc()
        return 1

    if diags.has_errors():
        n = len(diags.all_errors())
        print(red(bold(f"analysis error — {n} error{'s' if n > 1 else ''}")), file=sys.stderr)
        print_errors(source, diags)
        return 1
    elapsed("analysis")

    test_program, tests = _lower_tests_for_codegen(prog)
    if not tests:
        print(red("error: no tests found"), file=sys.stderr)
        print(cyan("hint:") + ' add `test "name" { @assert(...) }` blocks to your Mesa sources', file=sys.stderr)
        return 1

    out = output_path or "out"
    _ensure_output_parent(out)
    with tempfile.TemporaryDirectory(prefix=f"{module_name}_test_build_") as tmpdir:
        header_path = os.path.join(tmpdir, "mesa_shared.h")
        runtime_path = os.path.join(tmpdir, "mesa_runtime_state.c")
        runner_path = os.path.join(tmpdir, "mesa_tests_runner.c")
        object_paths = []

        header_cg = CCodegen(env, layout)
        header_cg.emit_support_header(test_program)
        open(header_path, "w").write(header_cg.output())
        pending_mono = list(header_cg._pending_mono)

        runtime_cg = CCodegen(env, layout)
        runtime_cg.emit_runtime_state_source(os.path.basename(header_path))
        open(runtime_path, "w").write(runtime_cg.output())
        runtime_obj = os.path.join(tmpdir, "mesa_runtime_state.o")
        if not _compile_c_to_object(runtime_path, runtime_obj, verbose=verbose):
            return 1
        object_paths.append(runtime_obj)

        unit_groups = _group_program_by_source_file(test_program, source_path)
        for index, (_unit_source, unit_program) in enumerate(unit_groups):
            unit_c_path = os.path.join(tmpdir, f"unit_{index}.c")
            unit_obj_path = os.path.join(tmpdir, f"unit_{index}.o")
            unit_cg = CCodegen(env, layout)
            unit_cg.emit_unit_source(unit_program, os.path.basename(header_path), pending_mono=pending_mono)
            open(unit_c_path, "w").write(unit_cg.output())
            if not _compile_c_to_object(unit_c_path, unit_obj_path, verbose=verbose):
                return 1
            object_paths.append(unit_obj_path)

        open(runner_path, "w").write(_emit_test_runner_source(os.path.basename(header_path), tests))
        runner_obj = os.path.join(tmpdir, "mesa_tests_runner.o")
        if not _compile_c_to_object(runner_path, runner_obj, verbose=verbose):
            return 1
        object_paths.append(runner_obj)

        if verbose:
            print(dim(f"  linking {len(object_paths)} objects → {out}..."), file=sys.stderr)
        ok = _link_objects_to_binary(object_paths, out, verbose=verbose)
        if not ok:
            return 1
        elapsed("compile")
        size = os.path.getsize(out)
        print(green(f"✓ {out}") + dim(f"  ({size:,} bytes)"), file=sys.stderr)
        return _run_binary(out, verbose=verbose)


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def _build_file_missing_error() -> int:
    print(red("error: build.mesa not found in the current directory"), file=sys.stderr)
    print(cyan("hint:") + " run `mesa init` to create one", file=sys.stderr)
    return 1


def _default_project_name(cwd: str) -> str:
    name = os.path.basename(os.path.abspath(cwd))
    return name or "app"


def _write_text_file(path: str, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)


def _init_project(cwd: str) -> int:
    build_path = find_build_file(cwd)
    src_dir = os.path.join(cwd, "src")
    main_path = os.path.join(src_dir, "main.mesa")
    if os.path.exists(build_path):
        print(red(f"error: {build_path} already exists"), file=sys.stderr)
        return 1
    if os.path.exists(main_path):
        print(red(f"error: {main_path} already exists"), file=sys.stderr)
        return 1
    os.makedirs(src_dir, exist_ok=True)
    plan = create_default_plan(_default_project_name(cwd))
    _write_text_file(build_path, render_build_plan(plan))
    _write_text_file(main_path, 'fun main() void {\n    println("Hello, Mesa!")\n}\n')
    print(green(f"✓ {build_path}"), file=sys.stderr)
    print(green(f"✓ {main_path}"), file=sys.stderr)
    return 0


def _load_build_plan_from_cwd(cwd: str):
    build_path = find_build_file(cwd)
    if not os.path.isfile(build_path):
        return None, None, _build_file_missing_error()
    try:
        return load_build_plan(build_path), build_path, 0
    except BuildPlanError as exc:
        print(red(f"error: {exc}"), file=sys.stderr)
        return None, build_path, 1


def _compile_default_target(cwd: str, output_path, run_binary_flag, verbose, timings) -> int:
    plan, build_path, status = _load_build_plan_from_cwd(cwd)
    if status != 0:
        return status
    assert plan is not None and build_path is not None
    target = plan.default_target()
    if target is None:
        print(red("error: build.mesa has no default executable target"), file=sys.stderr)
        return 1
    package_roots = []
    for pkg_index in target.imports:
        pkg = plan.packages[pkg_index]
        package_roots.append((resolve_package_root_path(pkg.root, cwd=cwd), pkg.name))
    foreign_namespaces = [plan.libraries[lib_index].name for lib_index in target.library_imports]
    entry_path = os.path.join(cwd, target.entry)
    target_output = output_path or os.path.join("targets", target.name)
    return compile_file(
        source_path=entry_path,
        output_path=target_output,
        emit_c=False,
        check_only=False,
        run_binary=run_binary_flag,
        verbose=verbose,
        timings=timings,
        package_roots=package_roots,
        foreign_namespaces=foreign_namespaces,
        local_root=os.path.dirname(entry_path),
    )


def _test_default_target(cwd: str, output_path, verbose, timings) -> int:
    plan, build_path, status = _load_build_plan_from_cwd(cwd)
    if status != 0:
        return status
    assert plan is not None and build_path is not None
    target = plan.default_target()
    if target is None:
        print(red("error: build.mesa has no default executable target"), file=sys.stderr)
        return 1
    package_roots = []
    for pkg_index in target.imports:
        pkg = plan.packages[pkg_index]
        package_roots.append((resolve_package_root_path(pkg.root, cwd=cwd), pkg.name))
    foreign_namespaces = [plan.libraries[lib_index].name for lib_index in target.library_imports]
    entry_path = os.path.join(cwd, target.entry)
    target_output = output_path or os.path.join("targets", f"{target.name}-tests")
    return compile_tests(
        source_path=entry_path,
        output_path=target_output,
        verbose=verbose,
        timings=timings,
        package_roots=package_roots,
        foreign_namespaces=foreign_namespaces,
        local_root=os.path.dirname(entry_path),
    )


def _pkg_add(cwd: str, source: str, name: str | None) -> int:
    plan, build_path, status = _load_build_plan_from_cwd(cwd)
    if status != 0:
        return status
    assert plan is not None and build_path is not None
    source = os.path.normpath(source)
    pkg_name = name or infer_package_name(source)
    try:
        changed = ensure_package_in_plan(plan, source, pkg_name)
    except BuildPlanError as exc:
        print(red(f"error: {exc}"), file=sys.stderr)
        return 1
    abs_source_dir = os.path.join(cwd, source)
    os.makedirs(abs_source_dir, exist_ok=True)
    facade_path = os.path.join(abs_source_dir, f"{pkg_name.split('.')[-1]}.pkg")
    if not os.path.exists(facade_path):
        _write_text_file(facade_path, f"pkg {pkg_name}\n")
    if changed:
        _write_text_file(build_path, render_build_plan(plan))
        print(green(f"✓ updated {build_path}"), file=sys.stderr)
    else:
        print(green("✓ package root already present"), file=sys.stderr)
    return 0


def _legacy_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="mesa",
        description="Mesa language compiler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  mesa nbody.mesa              # compile to ./out
  mesa nbody.mesa -o nbody     # compile to ./nbody
  mesa nbody.mesa --emit-c     # print generated C
  mesa nbody.mesa --check      # type check only
  mesa nbody.mesa -v --timings # verbose with timing info
        """
    )
    ap.add_argument("file", help="Mesa source file (.mesa)")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="output file (default: ./out, or stdout for --emit-c)")
    ap.add_argument("--emit-c", action="store_true",
                    help="emit C source instead of compiling to binary")
    ap.add_argument("--check", action="store_true",
                    help="type check only, do not generate code")
    ap.add_argument("--run", action="store_true",
                    help="run the compiled binary after a successful build")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show compilation steps")
    ap.add_argument("--timings", action="store_true",
                    help="show timing for each pass")
    ap.add_argument("--version", action="version", version="mesa 0.1.0")
    args = ap.parse_args(argv)

    if args.run and args.emit_c:
        ap.error("--run cannot be used with --emit-c")
    if args.run and args.check:
        ap.error("--run cannot be used with --check")

    if args.verbose:
        print(bold(cyan("mesa")) + dim(" 0.1.0"), file=sys.stderr)
        print(dim(f"  source: {args.file}"), file=sys.stderr)

    return compile_file(
        source_path=args.file,
        output_path=args.output,
        emit_c=args.emit_c,
        check_only=args.check,
        run_binary=args.run,
        verbose=args.verbose,
        timings=args.timings,
    )


def _command_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mesa", description="Mesa language compiler")
    ap.add_argument("--version", action="version", version="mesa 0.1.0")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create build.mesa and a starter src/main.mesa")

    build_ap = sub.add_parser("build", help="build the default target from ./build.mesa")
    build_ap.add_argument("-o", "--output", metavar="FILE", help="output binary path")
    build_ap.add_argument("-v", "--verbose", action="store_true", help="show compilation steps")
    build_ap.add_argument("--timings", action="store_true", help="show timing for each pass")

    run_ap = sub.add_parser("run", help="build and run the default target from ./build.mesa")
    run_ap.add_argument("-o", "--output", metavar="FILE", help="output binary path")
    run_ap.add_argument("-v", "--verbose", action="store_true", help="show compilation steps")
    run_ap.add_argument("--timings", action="store_true", help="show timing for each pass")

    test_ap = sub.add_parser("test", help="build and run Mesa test blocks from ./build.mesa")
    test_ap.add_argument("-o", "--output", metavar="FILE", help="output test binary path")
    test_ap.add_argument("-v", "--verbose", action="store_true", help="show compilation steps")
    test_ap.add_argument("--timings", action="store_true", help="show timing for each pass")

    sub.add_parser("lsp", help="run the Mesa stdio language server")

    pkg_ap = sub.add_parser("pkg", help="package root management")
    pkg_sub = pkg_ap.add_subparsers(dest="pkg_command", required=True)
    pkg_add_ap = pkg_sub.add_parser("add", help="add a package root to ./build.mesa")
    pkg_add_ap.add_argument("source", help="package source root, relative to the current directory")
    pkg_add_ap.add_argument("--name", help="optional top-level package prefix")

    args = ap.parse_args(argv)
    cwd = os.getcwd()

    if args.command == "init":
        return _init_project(cwd)
    if args.command == "build":
        return _compile_default_target(cwd, args.output, False, args.verbose, args.timings)
    if args.command == "run":
        return _compile_default_target(cwd, args.output, True, args.verbose, args.timings)
    if args.command == "test":
        return _test_default_target(cwd, args.output, args.verbose, args.timings)
    if args.command == "pkg" and args.pkg_command == "add":
        return _pkg_add(cwd, args.source, args.name)
    ap.error("unknown command")
    return 2


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {"init", "build", "run", "test", "lsp", "pkg"}:
        sys.exit(_command_main(sys.argv[1:]))
    sys.exit(_legacy_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
