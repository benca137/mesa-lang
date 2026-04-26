"""
Shared Mesa frontend helpers for tokenizing, parsing, and type checking.
"""
from __future__ import annotations

import os
import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from src.syntax.ast import (
    Decl,
    ErrorDecl,
    FromImportDecl,
    FunDecl,
    ImportDecl,
    InterfaceDecl,
    LetStmt,
    PkgDecl,
    PkgExportAllDecl,
    PkgExportDecl,
    Program,
    SourcePos,
    SourceSpan,
    StructDecl,
    TypeAlias,
    UnionDecl,
    Visibility,
)
from src.semantics.checker import type_check
from src.semantics.env import DiagnosticBag, Environment
from src.syntax.parser import ParseError, Parser
from src.stdlib import (
    augment_package_roots_with_std,
    canonicalize_std_import_path,
    is_reserved_std_bare_name,
)
from src.syntax.tokenizer import Token, TokenizeError, Tokenizer


@dataclass
class FrontendState:
    source: str
    tokens: List[Token] = field(default_factory=list)
    program: Optional[Program] = None
    env: Optional[Environment] = None
    diags: DiagnosticBag = field(default_factory=DiagnosticBag)
    tokenize_error: Optional[TokenizeError] = None
    parse_error: Optional[ParseError] = None

    @property
    def parse_succeeded(self) -> bool:
        return self.program is not None and self.parse_error is None and self.tokenize_error is None

    @property
    def typecheck_succeeded(self) -> bool:
        return self.parse_succeeded and self.env is not None and not self.diags.has_errors()


def _parse_frontend_state(source: str) -> FrontendState:
    state = FrontendState(source=source)

    try:
        state.tokens = Tokenizer(source).tokenize()
    except TokenizeError as exc:
        state.tokenize_error = exc
        state.diags.error(
            str(exc).split("] ", 1)[-1],
            line=exc.line,
            col=exc.col,
            span=SourceSpan(
                start=SourcePos(exc.line, exc.col),
                end=SourcePos(exc.line, exc.col + 1),
            ),
        )
        return state

    try:
        state.program = Parser(state.tokens).parse()
    except ParseError as exc:
        state.parse_error = exc
        tok = exc.token
        length = max(len(tok.lexeme), 1)
        state.diags.error(
            str(exc),
            line=tok.line,
            col=tok.col,
            span=SourceSpan(
                start=SourcePos(tok.line, tok.col),
                end=SourcePos(tok.line, tok.col + length),
            ),
        )
        return state

    return state


def build_frontend_state(source: str) -> FrontendState:
    state = _parse_frontend_state(source)
    if state.program is None:
        return state
    _canonicalize_program_imports(state.program, current_pkg_name=state.program.pkg.path if state.program.pkg is not None else None, package_roots=[])
    state.env, state.diags = type_check(state.program)
    return state


_BUILTIN_PACKAGES: set[str] = set()
PackageRoot = Tuple[str, Optional[str]]
PkgExportSpec = Tuple[str, str, bool]  # namespace path, public name, opaque
PkgExportTarget = Tuple[str, str]  # declared package path, effective package path


def _pkg_facade_basename(pkg_path: str) -> str:
    return f"{pkg_path.split('.')[-1]}.pkg"


def _resolve_legacy_package_file(import_path: str, base_dir: str) -> Optional[str]:
    rel = import_path.replace(".", os.sep)
    candidates = [
        os.path.join(base_dir, rel + ".mesa"),
        os.path.join(base_dir, rel, _pkg_facade_basename(import_path)),
        os.path.join(base_dir, rel, "mesa.pkg"),
        os.path.join(base_dir, rel, "mod.mesa"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return os.path.abspath(path)
    return None


def _clone_program_decls(program: Program) -> List[Decl]:
    return copy.deepcopy(program.decls)


def _annotate_decls(
    decls: List[Decl],
    *,
    pkg_path: Optional[str],
    source_file: Optional[str],
    imported_interface: bool = False,
):
    for decl in decls:
        if pkg_path:
            setattr(decl, "_pkg_path", pkg_path)
        if source_file:
            setattr(decl, "_source_file", source_file)
        if imported_interface:
            setattr(decl, "_imported_interface", True)
    return decls


def _parse_frontend_state_for_path(path: str, source_override: Optional[str] = None) -> FrontendState:
    source = source_override if source_override is not None else open(path).read()
    return _parse_frontend_state(source)


def _find_pkg_root(source_path: str, pkg_name: Optional[str]) -> Optional[str]:
    if not pkg_name:
        return None
    parts = pkg_name.split(".")
    cur = os.path.dirname(os.path.abspath(source_path))
    while True:
        segs = [p for p in cur.split(os.sep) if p]
        if len(segs) >= len(parts) and segs[-len(parts):] == parts:
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _normalize_package_roots(
    package_roots: Optional[List[PackageRoot]],
    *,
    include_std: bool = True,
) -> List[PackageRoot]:
    normalized: List[PackageRoot] = []
    roots = augment_package_roots_with_std(package_roots) if include_std else list(package_roots or [])
    for source, name in roots:
        normalized.append((os.path.abspath(source), name))
    return normalized


def _pkg_name_tail(pkg_name: Optional[str]) -> Optional[str]:
    if not pkg_name:
        return None
    return pkg_name.split(".")[-1]


def _resolve_pkg_identity_for_source(
    source_path: str,
    pkg_name: str,
    package_roots: List[PackageRoot],
) -> Tuple[Optional[str], str]:
    abs_source = os.path.abspath(source_path)
    rel = pkg_name.replace(".", os.sep)
    for source_root, public_name in package_roots:
        if public_name is None:
            pkg_root = os.path.abspath(os.path.join(source_root, rel))
            try:
                if os.path.commonpath([abs_source, pkg_root]) == pkg_root:
                    return pkg_root, pkg_name
            except ValueError:
                continue
            continue

        declared_candidates = {public_name, _pkg_name_tail(public_name)}
        if pkg_name not in declared_candidates:
            candidate_prefix = f"{public_name}.{pkg_name}"
            try:
                if os.path.commonpath([abs_source, source_root]) == source_root:
                    return os.path.abspath(os.path.join(source_root, pkg_name.replace(".", os.sep))), candidate_prefix
            except ValueError:
                continue
            continue
        try:
            if os.path.commonpath([abs_source, source_root]) == source_root:
                return source_root, public_name
        except ValueError:
            continue
    pkg_root = _find_pkg_root(source_path, pkg_name)
    return pkg_root, pkg_name


def _reserved_std_pkg_error(pkg_name: str) -> str:
    return f"package name '{pkg_name}' is reserved for the standard library"


def _collect_same_pkg_states(
    pkg_root: str,
    pkg_name: str,
    *,
    root_source_path: str,
    source_override: Optional[str] = None,
) -> Tuple[List[Tuple[str, FrontendState]], Optional[str]]:
    collected: List[Tuple[str, FrontendState]] = []
    for dirpath, _dirnames, filenames in os.walk(pkg_root):
        for filename in sorted(filenames):
            if not filename.endswith(".mesa"):
                continue
            path = os.path.abspath(os.path.join(dirpath, filename))
            state = _parse_frontend_state_for_path(
                path,
                source_override=source_override if os.path.abspath(root_source_path) == path else None,
            )
            if state.program is None:
                collected.append((path, state))
                continue
            if state.program.pkg is None or state.program.pkg.path != pkg_name:
                continue
            collected.append((path, state))
    if not collected:
        return [], f"no source files found for package '{pkg_name}'"
    return collected, None


def _collect_pkgless_states(
    local_root: str,
    *,
    root_source_path: str,
    source_override: Optional[str] = None,
) -> Tuple[List[Tuple[str, FrontendState]], Optional[str]]:
    collected: List[Tuple[str, FrontendState]] = []
    for dirpath, _dirnames, filenames in os.walk(local_root):
        for filename in sorted(filenames):
            if not filename.endswith(".mesa"):
                continue
            path = os.path.abspath(os.path.join(dirpath, filename))
            state = _parse_frontend_state_for_path(
                path,
                source_override=source_override if os.path.abspath(root_source_path) == path else None,
            )
            if state.program is None or state.program.pkg is None:
                collected.append((path, state))
    if not collected:
        return [], f"no source files found under '{local_root}'"
    return collected, None


def _resolve_import_target(
    import_path: str,
    *,
    from_dir: str,
    package_roots: List[PackageRoot],
) -> Optional[str]:
    candidates: List[str] = []
    for source_root, public_name in package_roots:
        if public_name is None:
            rel = import_path.replace(".", os.sep)
            candidates.extend([
                os.path.join(source_root, rel, _pkg_facade_basename(import_path)),
                os.path.join(source_root, rel, "mesa.pkg"),
                os.path.join(source_root, rel + ".mesa"),
            ])
            continue
        if import_path == public_name:
            candidates.extend([
                os.path.join(source_root, _pkg_facade_basename(public_name)),
                os.path.join(source_root, "mesa.pkg"),
                os.path.join(source_root, public_name.split(".")[-1] + ".mesa"),
            ])
            continue
        if import_path.startswith(public_name + "."):
            suffix = import_path[len(public_name) + 1 :]
            rel = suffix.replace(".", os.sep)
            candidates.extend([
                os.path.join(source_root, rel, _pkg_facade_basename(import_path)),
                os.path.join(source_root, rel, _pkg_facade_basename(suffix)),
                os.path.join(source_root, rel, "mesa.pkg"),
                os.path.join(source_root, rel + ".mesa"),
            ])
    rel = import_path.replace(".", os.sep)
    candidates.extend([
        os.path.join(from_dir, rel, _pkg_facade_basename(import_path)),
        os.path.join(from_dir, rel, "mesa.pkg"),
        os.path.join(from_dir, rel + ".mesa"),
    ])
    for path in candidates:
        if os.path.isfile(path):
            return os.path.abspath(path)
    return _resolve_legacy_package_file(import_path, from_dir)


def _canonicalize_import_path(
    import_path: str,
    *,
    current_pkg_name: Optional[str],
    package_roots: List[PackageRoot],
) -> str:
    import_path = canonicalize_std_import_path(import_path)
    if "." in import_path or current_pkg_name is None:
        return import_path
    if "." in current_pkg_name:
        parent_prefix = current_pkg_name.rsplit(".", 1)[0]
        candidate = f"{parent_prefix}.{import_path}"
        for _source_root, public_name in package_roots:
            if public_name == candidate:
                return candidate
    for _source_root, public_name in package_roots:
        if public_name == import_path:
            return import_path
    return import_path


def _canonicalize_program_imports(
    program: Program,
    *,
    current_pkg_name: Optional[str],
    package_roots: List[PackageRoot],
) -> None:
    for decl in program.decls:
        if isinstance(decl, (ImportDecl, FromImportDecl)):
            decl.path = _canonicalize_import_path(
                decl.path,
                current_pkg_name=current_pkg_name,
                package_roots=package_roots,
            )


def _resolve_export_target(target: str, pkg_root: str) -> str:
    if target.endswith(".mesa"):
        return os.path.abspath(os.path.join(pkg_root, target))
    return os.path.abspath(os.path.join(pkg_root, target + ".mesa"))


def _expected_export_pkg_path(facade_pkg_path: str, source_path: str) -> Optional[str]:
    rel = source_path[:-5] if source_path.endswith(".mesa") else source_path
    parts = [
        part
        for part in rel.replace("\\", "/").split("/")[:-1]
        if part and part != "."
    ]
    if any(part == ".." for part in parts):
        return None
    if not parts:
        return facade_pkg_path
    return ".".join([facade_pkg_path, *parts])


def _check_export_target_pkg(
    target_state: FrontendState,
    *,
    source_path: str,
    expected_pkg_path: str,
) -> Optional[str]:
    if target_state.program is None:
        return f"failed to parse export source '{source_path}'"
    if target_state.program.pkg is None:
        return f"export source '{source_path}' must declare 'pkg {expected_pkg_path}'"
    actual = target_state.program.pkg.path
    if actual != expected_pkg_path:
        return (
            f"export source '{source_path}' must declare 'pkg {expected_pkg_path}', "
            f"not 'pkg {actual}'"
        )
    return None


def _find_exportable_decl(program: Program, name: str) -> Optional[Decl]:
    for decl in program.decls:
        if getattr(decl, "name", None) == name:
            return decl
    return None


def _make_interface_decl(decl: Decl, *, public_name: Optional[str], opaque: bool) -> Decl:
    exported = copy.deepcopy(decl)
    if hasattr(exported, "vis"):
        exported.vis = Visibility.PUB
    if public_name and hasattr(exported, "name"):
        exported.name = public_name
    if isinstance(exported, FunDecl):
        exported.body = None
        exported.handle_block = None
    elif opaque and isinstance(exported, StructDecl):
        exported.fields = []
        exported.methods = []
    elif opaque and isinstance(exported, UnionDecl):
        exported.variants = []
    elif opaque and isinstance(exported, ErrorDecl):
        exported.variants = []
    return exported


def _collect_interface_decls(
    facade_state: FrontendState,
    facade_path: str,
    pkg_root: str,
) -> Tuple[List[Decl], Optional[str]]:
    if facade_state.program is None:
        return [], None
    target_cache: Dict[str, FrontendState] = {}
    exported_decls: List[Decl] = []
    import_decls: List[Decl] = []

    def target_state_for(path_key: str) -> FrontendState:
        target_path = _resolve_export_target(path_key, pkg_root)
        if target_path not in target_cache:
            target_cache[target_path] = _parse_frontend_state_for_path(target_path)
        return target_cache[target_path]

    for decl in facade_state.program.decls:
        if isinstance(decl, PkgExportAllDecl):
            target_state = target_state_for(decl.source_path)
            if target_state.program is None:
                return [], f"failed to parse export source '{decl.source_path}'"
            for target_decl in target_state.program.decls:
                if getattr(target_decl, "vis", Visibility.PRIVATE) != Visibility.PUB:
                    continue
                if isinstance(target_decl, (FunDecl, StructDecl, UnionDecl, InterfaceDecl, ErrorDecl, TypeAlias, LetStmt)):
                    exported_decls.append(_make_interface_decl(target_decl, public_name=None, opaque=False))
            import_decls.extend(copy.deepcopy(target_state.program.imports or []))
        elif isinstance(decl, PkgExportDecl):
            target_state = target_state_for(decl.source_path)
            if target_state.program is None:
                return [], f"failed to parse export source '{decl.source_path}'"
            for name, alias in decl.names:
                target_decl = _find_exportable_decl(target_state.program, name)
                if target_decl is None:
                    return [], f"export target '{decl.source_path}.{name}' not found"
                if getattr(target_decl, "vis", Visibility.PRIVATE) != Visibility.PUB:
                    return [], f"cannot export private declaration '{decl.source_path}.{name}'"
                if decl.opaque and not isinstance(target_decl, (StructDecl, UnionDecl, ErrorDecl)):
                    return [], f"opaque export requires a named type, got '{name}'"
                exported_decls.append(
                    _make_interface_decl(target_decl, public_name=alias or name, opaque=decl.opaque)
                )
            import_decls.extend(copy.deepcopy(target_state.program.imports or []))

    return import_decls + exported_decls, None


def _collect_pkg_export_specs(
    facade_state: FrontendState,
    pkg_root: str,
    *,
    effective_pkg_path: Optional[str] = None,
) -> Tuple[Dict[Tuple[str, str], List[PkgExportSpec]], Dict[str, PkgExportTarget], Optional[str]]:
    if facade_state.program is None:
        return {}, {}, None
    if facade_state.program.pkg is None:
        return {}, {}, None
    declared_pkg_path = facade_state.program.pkg.path
    export_pkg_path = effective_pkg_path or declared_pkg_path
    specs: Dict[Tuple[str, str], List[PkgExportSpec]] = {}
    target_pkgs: Dict[str, PkgExportTarget] = {}
    target_cache: Dict[str, FrontendState] = {}

    def target_state_for(path_key: str) -> tuple[str, FrontendState]:
        target_path = _resolve_export_target(path_key, pkg_root)
        if target_path not in target_cache:
            target_cache[target_path] = _parse_frontend_state_for_path(target_path)
        return target_path, target_cache[target_path]

    for decl in facade_state.program.decls:
        if isinstance(decl, PkgExportAllDecl):
            target_path, target_state = target_state_for(decl.source_path)
            declared_target_pkg_path = _expected_export_pkg_path(declared_pkg_path, decl.source_path)
            target_pkg_path = _expected_export_pkg_path(export_pkg_path, decl.source_path)
            if declared_target_pkg_path is None or target_pkg_path is None:
                return {}, {}, f"invalid export source path '{decl.source_path}'"
            pkg_err = _check_export_target_pkg(
                target_state,
                source_path=decl.source_path,
                expected_pkg_path=declared_target_pkg_path,
            )
            if pkg_err:
                return {}, {}, pkg_err
            target_pkgs[target_path] = (declared_target_pkg_path, target_pkg_path)
            for target_decl in target_state.program.decls:
                if getattr(target_decl, "vis", Visibility.PRIVATE) != Visibility.PUB:
                    continue
                name = getattr(target_decl, "name", None)
                if name is None:
                    continue
                specs.setdefault((target_path, name), []).append((target_pkg_path, name, False))
        elif isinstance(decl, PkgExportDecl):
            target_path, target_state = target_state_for(decl.source_path)
            declared_target_pkg_path = _expected_export_pkg_path(declared_pkg_path, decl.source_path)
            target_pkg_path = _expected_export_pkg_path(export_pkg_path, decl.source_path)
            if declared_target_pkg_path is None or target_pkg_path is None:
                return {}, {}, f"invalid export source path '{decl.source_path}'"
            pkg_err = _check_export_target_pkg(
                target_state,
                source_path=decl.source_path,
                expected_pkg_path=declared_target_pkg_path,
            )
            if pkg_err:
                return {}, {}, pkg_err
            target_pkgs[target_path] = (declared_target_pkg_path, target_pkg_path)
            for name, alias in decl.names:
                target_decl = _find_exportable_decl(target_state.program, name)
                if target_decl is None:
                    return {}, {}, f"export target '{decl.source_path}.{name}' not found"
                if getattr(target_decl, "vis", Visibility.PRIVATE) != Visibility.PUB:
                    return {}, {}, f"cannot export private declaration '{decl.source_path}.{name}'"
                if decl.opaque and not isinstance(target_decl, (StructDecl, UnionDecl, ErrorDecl)):
                    return {}, {}, f"opaque export requires a named type, got '{name}'"
                specs.setdefault((target_path, name), []).append((export_pkg_path, alias or name, decl.opaque))
    return specs, target_pkgs, None


def _load_package_graph(
    source_path: str,
    source_override: Optional[str] = None,
    *,
    package_roots: Optional[List[PackageRoot]] = None,
    foreign_namespaces: Optional[List[str]] = None,
    local_root: Optional[str] = None,
) -> Tuple[FrontendState, List[Tuple[str, FrontendState]], Optional[str]]:
    root_state = _parse_frontend_state_for_path(source_path, source_override=source_override)
    if root_state.program is None:
        return root_state, [], None

    explicit_roots = _normalize_package_roots(package_roots, include_std=False)
    normalized_roots = _normalize_package_roots(package_roots, include_std=True)
    root_pkg_rel = root_state.program.pkg.path if root_state.program.pkg is not None else None
    root_pkg_name: Optional[str] = None
    root_pkg_root: Optional[str] = None

    if root_pkg_rel:
        root_pkg_root, root_pkg_name = _resolve_pkg_identity_for_source(
            source_path,
            root_pkg_rel,
            normalized_roots,
        )
        if root_pkg_name and is_reserved_std_bare_name(root_pkg_name):
            return root_state, [], _reserved_std_pkg_error(root_pkg_name)
        if root_pkg_root and not explicit_roots:
            legacy_root = (os.path.dirname(root_pkg_root), None)
            if legacy_root not in normalized_roots:
                normalized_roots.append(legacy_root)

    _canonicalize_program_imports(
        root_state.program,
        current_pkg_name=root_pkg_name or root_pkg_rel,
        package_roots=normalized_roots,
    )

    if root_pkg_root and root_pkg_rel and root_pkg_name:
        same_pkg_states, pkg_err = _collect_same_pkg_states(
            root_pkg_root,
            root_pkg_rel,
            root_source_path=source_path,
            source_override=source_override,
        )
        if pkg_err:
            return root_state, [], pkg_err
        for path, state in same_pkg_states:
            if state.program is None:
                return state, [], None
        combined_root_decls: List[Decl] = []
        for path, state in same_pkg_states:
            if state.program is None:
                continue
            combined_root_decls.extend(
                _annotate_decls(
                    _clone_program_decls(state.program),
                    pkg_path=root_pkg_name,
                    source_file=path,
                )
            )
        root_state.program = Program(
            pkg=PkgDecl(path=root_pkg_name, span=root_state.program.pkg.span),
            imports=root_state.program.imports,
            decls=combined_root_decls,
        )
    elif root_state.program.pkg is None and local_root:
        same_local_states, local_err = _collect_pkgless_states(
            os.path.abspath(local_root),
            root_source_path=source_path,
            source_override=source_override,
        )
        if local_err:
            return root_state, [], local_err
        for path, state in same_local_states:
            if state.program is None:
                return state, [], None
        combined_root_decls: List[Decl] = []
        for path, state in same_local_states:
            if state.program is None:
                continue
            combined_root_decls.extend(
                _annotate_decls(
                    _clone_program_decls(state.program),
                    pkg_path=None,
                    source_file=path,
                )
            )
        root_state.program = Program(pkg=None, imports=root_state.program.imports, decls=combined_root_decls)

    loaded: Dict[str, FrontendState] = {}
    order: List[Tuple[str, FrontendState]] = []
    visiting: Set[str] = set()

    def visit(program: Program, from_dir: str, current_pkg_name: Optional[str]):
        for decl in program.decls:
            mod_path = None
            if decl.__class__.__name__ == "ImportDecl":
                mod_path = decl.path
            elif decl.__class__.__name__ == "FromImportDecl":
                mod_path = decl.path
            if mod_path:
                mod_path = _canonicalize_import_path(
                    mod_path,
                    current_pkg_name=current_pkg_name,
                    package_roots=normalized_roots,
                )
                setattr(decl, "path", mod_path)
            if (
                not mod_path
                or mod_path in _BUILTIN_PACKAGES
                or mod_path in set(foreign_namespaces or [])
                or mod_path == root_pkg_name
                or mod_path == current_pkg_name
            ):
                continue
            mod_file = _resolve_import_target(mod_path, from_dir=from_dir, package_roots=normalized_roots)
            if mod_file is None:
                return f"package not found for import '{mod_path}'"
            if mod_file in visiting:
                return f"cyclic import involving '{mod_path}'"
            if mod_file in loaded:
                continue
            visiting.add(mod_file)
            if mod_file.endswith(".pkg"):
                mod_state = _parse_frontend_state_for_path(mod_file)
                if mod_state.program is not None and mod_state.program.pkg is not None:
                    pkg_root, effective_pkg_name = _resolve_pkg_identity_for_source(
                        mod_file,
                        mod_state.program.pkg.path,
                        normalized_roots,
                    )
                    if effective_pkg_name and is_reserved_std_bare_name(effective_pkg_name):
                        visiting.remove(mod_file)
                        return _reserved_std_pkg_error(effective_pkg_name)
                    export_specs, export_targets, export_err = _collect_pkg_export_specs(
                        mod_state,
                        pkg_root or os.path.dirname(mod_file),
                        effective_pkg_path=effective_pkg_name,
                    )
                    if export_err:
                        visiting.remove(mod_file)
                        return export_err
                    else:
                        pkg_name = mod_state.program.pkg.path
                        pkg_root = pkg_root or os.path.dirname(mod_file)
                        pkg_states, pkg_err = _collect_same_pkg_states(
                            pkg_root,
                            pkg_name,
                            root_source_path=mod_file,
                        )
                        impl_decls: List[Decl] = []
                        seen_impl_paths: Set[str] = set()
                        if pkg_err:
                            if not export_targets:
                                visiting.remove(mod_file)
                                return pkg_err
                        else:
                            for path, state in pkg_states:
                                if state.program is None:
                                    continue
                                seen_impl_paths.add(path)
                                cloned = _annotate_decls(
                                    _clone_program_decls(state.program),
                                    pkg_path=effective_pkg_name,
                                    source_file=path,
                                    imported_interface=True,
                                )
                                for cloned_decl in cloned:
                                    setattr(cloned_decl, "_pkg_facade_controlled", True)
                                    export_meta = export_specs.get((path, getattr(cloned_decl, "name", "")), [])
                                    if export_meta:
                                        setattr(cloned_decl, "_pkg_export_names", export_meta)
                                impl_decls.extend(cloned)
                        for declared_target_pkg, target_pkg_path in sorted(set(export_targets.values())):
                            target_states, target_err = _collect_same_pkg_states(
                                pkg_root,
                                declared_target_pkg,
                                root_source_path=mod_file,
                            )
                            if target_err:
                                visiting.remove(mod_file)
                                return target_err
                            for path, state in target_states:
                                if path in seen_impl_paths:
                                    continue
                                seen_impl_paths.add(path)
                                if state.program is None:
                                    return f"failed to parse export source '{path}'"
                                cloned = _annotate_decls(
                                    _clone_program_decls(state.program),
                                    pkg_path=target_pkg_path,
                                    source_file=path,
                                    imported_interface=True,
                                )
                                for cloned_decl in cloned:
                                    setattr(cloned_decl, "_pkg_facade_controlled", True)
                                    export_meta = export_specs.get((path, getattr(cloned_decl, "name", "")), [])
                                    if export_meta:
                                        setattr(cloned_decl, "_pkg_export_names", export_meta)
                                impl_decls.extend(cloned)
                        mod_state.program = Program(
                            pkg=PkgDecl(path=effective_pkg_name, span=mod_state.program.pkg.span),
                            imports=[],
                            decls=impl_decls,
                        )
            else:
                mod_state = _parse_frontend_state_for_path(mod_file)
                if mod_state.program is not None:
                    if mod_state.program.pkg is None:
                        return f"cannot import pkg-less file '{mod_path}'"
                    _pkg_root, imported_pkg = _resolve_pkg_identity_for_source(
                        mod_file,
                        mod_state.program.pkg.path,
                        normalized_roots,
                    )
                    if imported_pkg and is_reserved_std_bare_name(imported_pkg):
                        visiting.remove(mod_file)
                        return _reserved_std_pkg_error(imported_pkg)
                    mod_state.program = Program(
                        pkg=PkgDecl(path=imported_pkg, span=mod_state.program.pkg.span),
                        imports=mod_state.program.imports,
                        decls=_annotate_decls(
                            _clone_program_decls(mod_state.program),
                            pkg_path=imported_pkg,
                            source_file=mod_file,
                            imported_interface=True,
                        ),
                    )
            if mod_state.program is None:
                loaded[mod_file] = mod_state
                order.append((mod_path, mod_state))
                visiting.remove(mod_file)
                continue
            next_pkg_name = mod_state.program.pkg.path if mod_state.program.pkg is not None else mod_path
            err = visit(mod_state.program, os.path.dirname(mod_file), next_pkg_name)
            loaded[mod_file] = mod_state
            order.append((next_pkg_name, mod_state))
            visiting.remove(mod_file)
            if err:
                return err
        return None

    err = visit(root_state.program, os.path.dirname(os.path.abspath(source_path)), root_pkg_name)
    return root_state, order, err


def build_frontend_state_for_path(
    source_path: str,
    source_override: Optional[str] = None,
    *,
    package_roots: Optional[List[PackageRoot]] = None,
    foreign_namespaces: Optional[List[str]] = None,
    local_root: Optional[str] = None,
) -> FrontendState:
    try:
        root_state, loaded_packages, graph_error = _load_package_graph(
            source_path,
            source_override=source_override,
            package_roots=package_roots,
            foreign_namespaces=foreign_namespaces,
            local_root=local_root,
        )
    except FileNotFoundError:
        state = FrontendState(source="")
        state.diags.error(f"file not found: {source_path}")
        return state
    except OSError as exc:
        state = FrontendState(source="")
        state.diags.error(str(exc))
        return state

    if root_state.program is None:
        return root_state

    if graph_error:
        root_state.diags.error(graph_error)
        return root_state

    for _, pkg_state in loaded_packages:
        if pkg_state.program is None:
            root_state.diags = pkg_state.diags
            root_state.tokenize_error = pkg_state.tokenize_error
            root_state.parse_error = pkg_state.parse_error
            return root_state

    combined_decls = []
    seen_packages: Set[str] = set()
    for pkg_path, pkg_state in loaded_packages:
        if pkg_path in seen_packages:
            continue
        seen_packages.add(pkg_path)
        combined_decls.extend(_clone_program_decls(pkg_state.program))
    combined_decls.extend(root_state.program.decls)
    root_state.program = Program(
        pkg=root_state.program.pkg,
        imports=root_state.program.imports,
        decls=combined_decls,
    )
    root_state.env, root_state.diags = type_check(
        root_state.program,
        package_roots=_normalize_package_roots(package_roots, include_std=True),
        foreign_namespaces=foreign_namespaces,
        local_root=local_root,
        source_path=os.path.abspath(source_path),
    )
    return root_state
