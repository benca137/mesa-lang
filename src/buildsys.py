from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Dict, List, Optional, Union

from src.ast import (
    ArrayLit,
    Block,
    CallExpr,
    Expr,
    ExprStmt,
    FieldExpr,
    FunDecl,
    Ident,
    LetStmt,
    Program,
    StringLit,
    TupleLit,
    VariantLit,
    TyNamed,
    TyPointer,
    TyPrimitive,
    TyVoid,
    Visibility,
)
from src.stdlib import STD_BUILD_SPECS, is_reserved_std_bare_name


@dataclass
class PackageRootSpec:
    root: str
    name: Optional[str] = None


@dataclass
class LibrarySpec:
    name: str
    abi: Optional[str] = None


@dataclass
class ExecutableTarget:
    name: str
    entry: str
    imports: List[int] = field(default_factory=list)
    library_imports: List[int] = field(default_factory=list)


@dataclass
class BuildPlan:
    packages: List[PackageRootSpec] = field(default_factory=list)
    libraries: List[LibrarySpec] = field(default_factory=list)
    executables: List[ExecutableTarget] = field(default_factory=list)
    default_executable: Optional[int] = None

    def default_target(self) -> Optional[ExecutableTarget]:
        if self.default_executable is None:
            return None
        if self.default_executable < 0 or self.default_executable >= len(self.executables):
            return None
        return self.executables[self.default_executable]


class BuildPlanError(Exception):
    pass


@dataclass
class _BuildHandle:
    pass


@dataclass
class _PackageHandle:
    index: int


@dataclass
class _EntryHandle:
    path: str


@dataclass
class _LibraryHandle:
    index: int


@dataclass
class _ExecutableHandle:
    index: int


def _is_build_param_type(ty) -> bool:
    if not isinstance(ty, TyPointer):
        return False
    inner = ty.inner
    return isinstance(inner, TyNamed) and inner.name in {"build.Build", "std.build.Build"}


def _is_void_type(ty) -> bool:
    return isinstance(ty, TyVoid) or (isinstance(ty, TyPrimitive) and ty.name == "void")


def _extract_build_fn(program: Program) -> FunDecl:
    if program.pkg is not None:
        raise BuildPlanError("build.mesa must not declare a package")
    for decl in program.decls:
        if isinstance(decl, FunDecl) and decl.name == "build":
            if (
                decl.vis != Visibility.PUB
                or len(decl.params) != 1
                or not _is_build_param_type(decl.params[0].type_)
                or not _is_void_type(decl.ret)
            ):
                raise BuildPlanError("build.mesa requires `pub fun build(b: *build.Build) void`")
            if decl.body is None:
                raise BuildPlanError("build.mesa `build` function must have a body")
            return decl
    raise BuildPlanError("build.mesa is missing `pub fun build(b: *build.Build) void`")


def _eval_string(expr: Expr) -> str:
    if isinstance(expr, StringLit):
        return expr.raw
    raise BuildPlanError("build.mesa currently only supports string literals in build API calls")


def _eval_struct(expr: Expr) -> Dict[str, str]:
    if not isinstance(expr, TupleLit):
        raise BuildPlanError("build.mesa build API expects anonymous struct arguments like `.{ source: \"src\" }`")
    result: Dict[str, str] = {}
    for key, value in expr.fields:
        if key is None:
            raise BuildPlanError("build.mesa anonymous struct arguments must use named fields")
        result[key] = _eval_string(value)
    return result


def _split_call_args(expr: CallExpr) -> tuple[list[Expr], Dict[str, Expr]]:
    positional: list[Expr] = []
    named: Dict[str, Expr] = {}
    for arg in expr.args:
        if arg.name is None:
            positional.append(arg.value)
            continue
        if arg.name in named:
            raise BuildPlanError(f"duplicate named argument '{arg.name}' in build.mesa call")
        named[arg.name] = arg.value
    return positional, named


def _eval_handle_struct(
    expr: Expr,
    *,
    item_kind: str,
) -> List[Expr]:
    if not isinstance(expr, TupleLit):
        raise BuildPlanError(f"build.mesa expected an anonymous struct of {item_kind}s like `.{item1, item2}`")
    values: List[Expr] = []
    for key, value in expr.fields:
        if key is not None:
            raise BuildPlanError(f"build.mesa {item_kind} collections must use positional entries like `.{item_kind}1, {item_kind}2`")
        values.append(value)
    return values


def _binding_name_seed(label: str, fallback: str) -> str:
    cleaned = []
    for ch in label:
        cleaned.append(ch if ch.isalnum() or ch == "_" else "_")
    candidate = "".join(cleaned).strip("_") or fallback
    if candidate[0].isdigit():
        candidate = "_" + candidate
    return candidate


def suggest_binding_name(label: str, used: set[str], *, fallback: str) -> str:
    base = _binding_name_seed(label, fallback)
    if base not in used:
        used.add(base)
        return base
    i = 2
    while f"{base}_{i}" in used:
        i += 1
    name = f"{base}_{i}"
    used.add(name)
    return name


def infer_package_name(root: str) -> str:
    label = os.path.basename(os.path.normpath(root))
    if not label:
        raise BuildPlanError("could not infer package name from source path")
    return label


def interpret_build_program(program: Program) -> BuildPlan:
    build_fn = _extract_build_fn(program)
    plan = BuildPlan()
    env: Dict[str, Union[_BuildHandle, _PackageHandle, _EntryHandle, _LibraryHandle, _ExecutableHandle]] = {
        build_fn.params[0].name: _BuildHandle()
    }

    def eval_expr(expr: Expr):
        if isinstance(expr, Ident):
            if expr.name not in env:
                raise BuildPlanError(f"unknown build binding '{expr.name}'")
            return env[expr.name]
        if isinstance(expr, VariantLit):
            return f".{expr.name}"
        if isinstance(expr, StringLit):
            return expr.raw
        if isinstance(expr, TupleLit):
            return _eval_struct(expr)
        if isinstance(expr, ArrayLit):
            return [eval_expr(item) for item in expr.elems]
        if isinstance(expr, CallExpr):
            return eval_call(expr)
        raise BuildPlanError("unsupported expression in build.mesa; v1 supports lets, strings, anonymous structs, and build API calls")

    def eval_call(expr: CallExpr):
        callee = expr.callee
        if not isinstance(callee, FieldExpr):
            raise BuildPlanError("build.mesa v1 only supports method-style build API calls")
        receiver = eval_expr(callee.obj)
        method = callee.field
        args, named = _split_call_args(expr)

        if isinstance(receiver, _BuildHandle):
            if method == "createPackage":
                if len(args) != 1:
                    raise BuildPlanError("Build.createPackage expects one anonymous-struct argument")
                spec = _eval_struct(args[0])
                root = spec.get("root") or spec.get("source")
                if not root:
                    raise BuildPlanError("Build.createPackage requires `root` or `source`")
                pkg = PackageRootSpec(root=root, name=spec.get("name"))
                plan.packages.append(pkg)
                return _PackageHandle(len(plan.packages) - 1)
            if method == "addPackage":
                if len(args) != 1:
                    raise BuildPlanError("Build.addPackage expects the package name as its first argument")
                pkg_name = _eval_string(args[0])
                if is_reserved_std_bare_name(pkg_name):
                    raise BuildPlanError(
                        f"package name '{pkg_name}' is reserved for the standard library; "
                        f"use the bundled std roots or a qualified name like 'myapp.{pkg_name}'"
                    )
                root_expr = named.get("root") or named.get("source")
                if root_expr is None:
                    raise BuildPlanError("Build.addPackage requires `root = \"...\"`")
                pkg = PackageRootSpec(root=_eval_string(root_expr), name=pkg_name)
                plan.packages.append(pkg)
                return _PackageHandle(len(plan.packages) - 1)
            if method == "linkLibrary":
                if len(args) != 1:
                    raise BuildPlanError("Build.linkLibrary expects the library name as its first argument")
                lib_name = _eval_string(args[0])
                abi_expr = named.get("abi")
                abi = eval_expr(abi_expr) if abi_expr is not None else None
                if abi is not None and not isinstance(abi, str):
                    raise BuildPlanError("Build.linkLibrary `abi` must be a variant like `.c`")
                plan.libraries.append(LibrarySpec(name=lib_name, abi=abi))
                return _LibraryHandle(len(plan.libraries) - 1)
            if method == "createEntry":
                if len(args) != 1:
                    raise BuildPlanError("Build.createEntry expects one string path argument")
                return _EntryHandle(_eval_string(args[0]))
            if method == "addExecutable":
                if len(args) < 1:
                    raise BuildPlanError("Build.addExecutable expects at least the executable name")
                exe_name = _eval_string(args[0])
                entry_expr = named.get("entry")
                if entry_expr is None and len(args) >= 2:
                    entry_expr = args[1]
                if entry_expr is None:
                    raise BuildPlanError("Build.addExecutable requires `entry = b.createEntry(...)`")
                entry_value = eval_expr(entry_expr)
                if isinstance(entry_value, _EntryHandle):
                    entry_path = entry_value.path
                elif isinstance(entry_value, str):
                    entry_path = entry_value
                else:
                    raise BuildPlanError("Build.addExecutable `entry` must be a string path or entry handle")
                imports_expr = named.get("imports")
                import_handles: List[int] = []
                library_handles: List[int] = []
                if imports_expr is not None:
                    import_items = _eval_handle_struct(imports_expr, item_kind="import handle")
                    for item_expr in import_items:
                        item = eval_expr(item_expr)
                        if isinstance(item, _PackageHandle):
                            if item.index not in import_handles:
                                import_handles.append(item.index)
                            continue
                        if isinstance(item, _LibraryHandle):
                            if item.index not in library_handles:
                                library_handles.append(item.index)
                            continue
                        raise BuildPlanError("Build.addExecutable `imports` must contain package or library handles")
                exe = ExecutableTarget(
                    name=exe_name,
                    entry=entry_path,
                    imports=import_handles,
                    library_imports=library_handles,
                )
                plan.executables.append(exe)
                return _ExecutableHandle(len(plan.executables) - 1)
            if method == "install":
                if len(args) != 1:
                    raise BuildPlanError("Build.install expects one executable handle")
                handle = eval_expr(args[0])
                if not isinstance(handle, _ExecutableHandle):
                    raise BuildPlanError("Build.install expects an executable handle")
                plan.default_executable = handle.index
                return None
            if method == "setDefault":
                if len(args) != 1:
                    raise BuildPlanError("Build.setDefault expects one executable handle")
                handle = eval_expr(args[0])
                if not isinstance(handle, _ExecutableHandle):
                    raise BuildPlanError("Build.setDefault expects an executable handle")
                plan.default_executable = handle.index
                return None
            raise BuildPlanError(f"unknown Build method '{method}'")

        if isinstance(receiver, _ExecutableHandle):
            if method == "addPackage":
                if len(args) != 1:
                    raise BuildPlanError("Executable.addPackage expects one package handle")
                handle = eval_expr(args[0])
                if not isinstance(handle, _PackageHandle):
                    raise BuildPlanError("Executable.addPackage expects a package handle")
                exe = plan.executables[receiver.index]
                if handle.index not in exe.imports:
                    exe.imports.append(handle.index)
                return None
            if method == "linkLibrary":
                if len(args) != 1:
                    raise BuildPlanError("Executable.linkLibrary expects one library handle")
                handle = eval_expr(args[0])
                if not isinstance(handle, _LibraryHandle):
                    raise BuildPlanError("Executable.linkLibrary expects a library handle")
                exe = plan.executables[receiver.index]
                if handle.index not in exe.library_imports:
                    exe.library_imports.append(handle.index)
                return None
            raise BuildPlanError(f"unknown Executable method '{method}'")

        raise BuildPlanError(f"unsupported build receiver for method '{method}'")

    def exec_stmt(stmt):
        if isinstance(stmt, LetStmt):
            if stmt.init is None:
                raise BuildPlanError("build.mesa let bindings must have initializers")
            env[stmt.name] = eval_expr(stmt.init)
            return
        if isinstance(stmt, ExprStmt):
            eval_expr(stmt.expr)
            return
        raise BuildPlanError("unsupported statement in build.mesa; v1 supports let bindings and expression statements only")

    body: Block = build_fn.body
    for stmt in body.stmts:
        exec_stmt(stmt)
    if body.tail is not None:
        eval_expr(body.tail)

    if not plan.executables:
        raise BuildPlanError("build.mesa must declare at least one executable target")
    if plan.default_executable is None:
        raise BuildPlanError("build.mesa must install a default executable target with `b.install(...)`")
    return plan


def load_build_plan(build_path: str, source_override: Optional[str] = None) -> BuildPlan:
    from src.frontend import _parse_frontend_state_for_path

    state = _parse_frontend_state_for_path(build_path, source_override=source_override)
    if state.tokenize_error is not None:
        raise BuildPlanError(str(state.tokenize_error))
    if state.parse_error is not None:
        raise BuildPlanError(str(state.parse_error))
    if state.program is None:
        raise BuildPlanError("failed to parse build.mesa")
    return interpret_build_program(state.program)


def find_build_file(cwd: str) -> str:
    return os.path.join(os.path.abspath(cwd), "build.mesa")


def render_build_plan(plan: BuildPlan) -> str:
    lines: List[str] = []
    lines.append("pub fun build(b: *build.Build) void {")
    used_names: set[str] = {"b"}
    package_vars: List[str] = []
    library_vars: List[str] = []
    for i, pkg in enumerate(plan.packages):
        label = pkg.name or os.path.basename(os.path.normpath(pkg.root)) or f"pkg{i + 1}"
        var = suggest_binding_name(label, used_names, fallback=f"pkg{i + 1}")
        package_vars.append(var)
        if pkg.name:
            lines.append(f'    let {var} = b.addPackage("{pkg.name}", root = "{pkg.root}")')
        else:
            lines.append(f'    let {var} = b.createPackage(.{{ root: "{pkg.root}" }})')
    for i, lib in enumerate(plan.libraries):
        var = suggest_binding_name(lib.name, used_names, fallback=f"lib{i + 1}")
        library_vars.append(var)
        extras = [f'"{lib.name}"']
        if lib.abi is not None:
            extras.append(f"abi = {lib.abi}")
        lines.append(f"    let {var} = b.linkLibrary({', '.join(extras)})")
    entry_vars: List[str] = []
    exe_vars: List[str] = []
    for i, exe in enumerate(plan.executables):
        exe_var = suggest_binding_name(exe.name, used_names, fallback="exe")
        entry_var = suggest_binding_name(f"{exe_var}_entry", used_names, fallback="entry")
        entry_vars.append(entry_var)
        exe_vars.append(exe_var)
        lines.append(f'    let {entry_var} = b.createEntry("{exe.entry}")')
        extras: List[str] = [f'entry = {entry_var}']
        import_names = [package_vars[pkg_index] for pkg_index in exe.imports]
        import_names.extend(library_vars[lib_index] for lib_index in exe.library_imports)
        if import_names:
            imports = ", ".join(import_names)
            extras.append(f"imports = .{{ {imports} }}")
        joined = ", ".join(extras)
        lines.append(f'    let {exe_var} = b.addExecutable("{exe.name}", {joined})')
    if plan.default_executable is not None:
        lines.append(f"    b.install({exe_vars[plan.default_executable]})")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def create_default_plan(project_name: str) -> BuildPlan:
    packages = [PackageRootSpec(root=root, name=name) for root, name in STD_BUILD_SPECS]
    return BuildPlan(
        packages=packages,
        executables=[ExecutableTarget(name=project_name, entry="src/main.mesa", imports=list(range(len(packages))))],
        default_executable=0,
    )


def ensure_package_in_plan(plan: BuildPlan, source: str, name: Optional[str]) -> bool:
    if name is None:
        name = infer_package_name(source)
    for pkg in plan.packages:
        if pkg.root == source and pkg.name == name:
            return False
    plan.packages.append(PackageRootSpec(root=source, name=name))
    pkg_index = len(plan.packages) - 1
    default = plan.default_target()
    if default is None:
        raise BuildPlanError("build.mesa has no default executable target")
    if pkg_index not in default.imports:
        default.imports.append(pkg_index)
    return True
