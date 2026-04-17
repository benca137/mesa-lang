from __future__ import annotations

import os
import sys
from typing import Iterable, List, Optional, Sequence, Tuple


STD_BARE_IMPORTS: dict[str, str] = {
    "mem": "std.mem",
    "io": "std.io",
}

STD_BUILD_SPECS: tuple[tuple[str, str], ...] = (
    ("@std", "std"),
)


def compiler_root() -> str:
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return os.path.abspath(bundle_root)
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def std_root_dir() -> str:
    return os.path.join(compiler_root(), "std")


def is_std_source_path(path: str | None) -> bool:
    if not path:
        return False
    try:
        return os.path.commonpath([os.path.abspath(path), std_root_dir()]) == std_root_dir()
    except ValueError:
        return False


def canonicalize_std_import_path(import_path: str) -> str:
    return STD_BARE_IMPORTS.get(import_path, import_path)


def resolve_package_root_path(root: str, *, cwd: Optional[str] = None) -> str:
    if root == "@std":
        return std_root_dir()
    if root.startswith("@std/"):
        rel = root[len("@std/"):]
        return os.path.abspath(os.path.join(std_root_dir(), rel))
    base = os.path.abspath(cwd or os.getcwd())
    return os.path.abspath(os.path.join(base, root))


def default_std_package_roots(*, absolute: bool = False) -> List[Tuple[str, str]]:
    roots: List[Tuple[str, str]] = []
    for root, name in STD_BUILD_SPECS:
        roots.append((resolve_package_root_path(root) if absolute else root, name))
    return roots


def augment_package_roots_with_std(
    package_roots: Optional[Sequence[Tuple[str, Optional[str]]]],
    *,
    cwd: Optional[str] = None,
) -> List[Tuple[str, Optional[str]]]:
    seen: set[tuple[str, Optional[str]]] = set()
    merged: List[Tuple[str, Optional[str]]] = []
    for root, name in package_roots or []:
        resolved = resolve_package_root_path(root, cwd=cwd)
        key = (resolved, name)
        if key in seen:
            continue
        seen.add(key)
        merged.append((resolved, name))
    for root, name in default_std_package_roots(absolute=True):
        key = (root, name)
        if key in seen:
            continue
        seen.add(key)
        merged.append((root, name))
    return merged


def is_reserved_std_bare_name(name: str) -> bool:
    return name in STD_BARE_IMPORTS
