"""
Type checker environment.

Manages:
  - Scoped symbol tables (name → Type + mutability)
  - Type registry (struct/union/interface/alias names → Type)
  - Interface implementation registry (type → set of interfaces it implements)
  - Diagnostic collection with TError propagation
"""
from __future__ import annotations
from dataclasses import dataclass, field
import difflib
from typing import Dict, List, Optional, Set, Tuple
from src.ast import SourcePos, SourceSpan
from src.types import *


# ══════════════════════════════════════════════════════════════
# Diagnostics
# ══════════════════════════════════════════════════════════════
@dataclass
class DiagnosticRelated:
    message: str
    span: SourceSpan


@dataclass
class Diagnostic:
    kind:     str    # "error" | "warning" | "note"
    message:  str
    line:     int
    col:      int
    span:     Optional[SourceSpan] = None
    hint:     Optional[str] = None   # suggestion / help text
    origin:   Optional[str] = None   # "tvar" if caused by TError propagation
    code:     Optional[str] = None
    related:  List[DiagnosticRelated] = field(default_factory=list)

    def __str__(self):
        loc = f"[{self.line}:{self.col}]"
        out = f"{self.kind.upper()} {loc} {self.message}"
        if self.code:
            out += f" ({self.code})"
        if self.hint: out += f"\n  hint: {self.hint}"
        for rel in self.related:
            out += (
                f"\n  note: {rel.message}"
                f" [{rel.span.start.line}:{rel.span.start.col}]"
            )
        return out

    @property
    def is_secondary(self) -> bool:
        """True if this error was caused by a prior TError — suppress in normal mode."""
        return self.origin == "tvar"


class DiagnosticBag:
    def __init__(self):
        self._diags: List[Diagnostic] = []

    def _diag_key(self, diag: Diagnostic):
        span_key = None
        if diag.span is not None:
            span_key = (
                diag.span.start.line, diag.span.start.col,
                diag.span.end.line, diag.span.end.col,
            )
        related_key = tuple(
            (
                rel.message,
                rel.span.start.line, rel.span.start.col,
                rel.span.end.line, rel.span.end.col,
            )
            for rel in diag.related
        )
        return (
            diag.kind,
            diag.message,
            diag.line,
            diag.col,
            span_key,
            diag.hint,
            diag.origin,
            diag.code,
            related_key,
        )

    def _append_unique(self, diag: Diagnostic):
        key = self._diag_key(diag)
        for existing in self._diags:
            if self._diag_key(existing) == key:
                return
        self._diags.append(diag)

    def error(self, msg: str, line: int = 0, col: int = 0,
              hint: str = None, origin: str = None,
              span: Optional[SourceSpan] = None,
              code: Optional[str] = None,
              related: Optional[List[DiagnosticRelated]] = None):
        if span is not None and (line <= 0 or col <= 0):
            line = span.start.line
            col = span.start.col
        self._append_unique(Diagnostic("error", msg, line, col, span, hint, origin, code, list(related or [])))

    def warning(self, msg: str, line: int = 0, col: int = 0, hint: str = None,
                span: Optional[SourceSpan] = None,
                code: Optional[str] = None,
                related: Optional[List[DiagnosticRelated]] = None):
        if span is not None and (line <= 0 or col <= 0):
            line = span.start.line
            col = span.start.col
        self._append_unique(Diagnostic("warning", msg, line, col, span, hint, None, code, list(related or [])))

    def note(self, msg: str, line: int = 0, col: int = 0,
             span: Optional[SourceSpan] = None,
             code: Optional[str] = None,
             related: Optional[List[DiagnosticRelated]] = None):
        if span is not None and (line <= 0 or col <= 0):
            line = span.start.line
            col = span.start.col
        self._append_unique(Diagnostic("note", msg, line, col, span, None, None, code, list(related or [])))

    def has_errors(self) -> bool:
        return any(d.kind == "error" and not d.is_secondary for d in self._diags)

    def all_errors(self) -> List[Diagnostic]:
        return [d for d in self._diags if d.kind == "error" and not d.is_secondary]

    def all_diags(self) -> List[Diagnostic]:
        return list(self._diags)

    def print_all(self):
        for d in self.all_errors():
            print(d)

    def __len__(self): return len(self._diags)


# ══════════════════════════════════════════════════════════════
# Symbol — a name binding in scope
# ══════════════════════════════════════════════════════════════
@dataclass
class Symbol:
    name:     str
    type_:    Type
    mutable:  bool        # let var vs let
    line:     int = 0
    col:      int = 0
    lifetime_region: Optional[int] = None
    decl_region:     Optional[int] = None
    pkg_path: Optional[str] = None
    c_name: Optional[str] = None

    # for functions — the original FunDecl node (for diagnostic re-checking)
    decl_node: object = None


# ══════════════════════════════════════════════════════════════
# Interface implementation registry
# ══════════════════════════════════════════════════════════════
class ImplRegistry:
    """
    Tracks which types implement which interfaces.
    Populated during the declaration pass when `def` blocks are processed.
    """
    def __init__(self):
        # (type_name, interface_name) → { method_name: TFun }
        self._impls: Dict[Tuple[str, str], Dict[str, TFun]] = {}

    def register(self, type_name: str, iface_name: str,
                 methods: Dict[str, TFun]):
        key = (type_name, iface_name)
        self._impls[key] = methods

    def implements(self, type_name: str, iface_name: str) -> bool:
        return (type_name, iface_name) in self._impls

    def get_method(self, type_name: str, iface_name: str,
                   method_name: str) -> Optional[TFun]:
        key = (type_name, iface_name)
        methods = self._impls.get(key, {})
        return methods.get(method_name)

    def all_interfaces_for(self, type_name: str) -> List[str]:
        return [iface for (t, iface) in self._impls if t == type_name]

    def find_method(self, type_name: str, method_name: str) -> Optional[TFun]:
        """Search all interfaces implemented by type_name for a method."""
        for (t, iface), methods in self._impls.items():
            if t == type_name and method_name in methods:
                return methods[method_name]
        return None


# ══════════════════════════════════════════════════════════════
# Scope — one level of the symbol table stack
# ══════════════════════════════════════════════════════════════
class Scope:
    def __init__(self, label: Optional[str] = None,
                 loop_type: Optional[Type] = None):
        self._symbols: Dict[str, Symbol] = {}
        self.label     = label       # for named loops
        self.loop_type = loop_type   # expected type for break values

    def define(self, sym: Symbol) -> bool:
        """Returns False if name already defined in this scope."""
        if sym.name in self._symbols:
            return False
        self._symbols[sym.name] = sym
        return True

    def lookup(self, name: str) -> Optional[Symbol]:
        return self._symbols.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._symbols


# ══════════════════════════════════════════════════════════════
# Environment — the full type checker state
# ══════════════════════════════════════════════════════════════
class Environment:
    def __init__(self, diags: DiagnosticBag):
        self.diags   = diags
        self._scopes: List[Scope] = [Scope()]   # global scope at bottom

        # type registry — user-defined type names → Type
        self._types: Dict[str, Type] = dict(PRIMITIVE_MAP)
        self._types["EscError"] = TErrorSet("EscError", {"AllocatorStackEmpty": None})
        self._pkg_symbols: Dict[str, Dict[str, Symbol]] = {}
        self._pkg_types: Dict[str, Dict[str, Type]] = {}
        self._type_c_names: Dict[str, str] = {}
        self._pkg_type_c_names: Dict[str, Dict[str, str]] = {}

        # interface registry — interface name → TInterface
        self._interfaces: Dict[str, TInterface] = {}
        self._namespace_values: Dict[str, Dict[str, Type]] = {}
        self._namespace_types: Dict[str, Dict[str, Type]] = {}
        self._namespace_hidden: Dict[str, Dict[str, Tuple[str, bool, bool]]] = {}
        self._namespace_value_c_names: Dict[str, Dict[str, str]] = {}
        self._namespace_type_c_names: Dict[str, Dict[str, str]] = {}

        # implementation registry
        self.impls = ImplRegistry()

        # current function return type — for checking return statements
        self._return_type: Optional[Type] = None

        # unit registry — user-defined units: name → (DimVec, scale)
        # overlaid on top of the built-in _SI_BASE registry in types.py
        from src.types import _SI_BASE
        self._unit_registry: dict = dict(_SI_BASE)

        # current struct being checked — for self resolution
        self._current_struct: Optional[str] = None
        self._current_pkg: Optional[str] = None

        # loop stack — for break/continue type checking
        self._loop_stack: List[Scope] = []

        # allocator / lifetime tracking for cleanup-bearing with blocks
        self._allocator_stack: List[Tuple[object, Type, Optional[int]]] = []
        self._active_allocator = None      # (resource_expr, alloc_ty, cleanup_region)
        self._with_handle_depth = 0

    # ── scope management ─────────────────────────────────────

    def push_scope(self, label: Optional[str] = None,
                   loop_type: Optional[Type] = None):
        self._scopes.append(Scope(label=label, loop_type=loop_type))

    def pop_scope(self):
        assert len(self._scopes) > 1, "cannot pop global scope"
        self._scopes.pop()

    def push_loop(self, label: Optional[str] = None,
                  loop_type: Optional[Type] = None):
        s = Scope(label=label, loop_type=loop_type)
        self._loop_stack.append(s)
        self._scopes.append(s)

    def pop_loop(self):
        self._loop_stack.pop()
        self._scopes.pop()

    # ── symbol lookup ─────────────────────────────────────────

    def define(self, sym: Symbol) -> bool:
        if len(self._scopes) == 1 and sym.pkg_path is not None:
            pkg_symbols = self._pkg_symbols.setdefault(sym.pkg_path, {})
            existing = pkg_symbols.get(sym.name)
            if existing is not None:
                related = []
                if existing.line > 0 and existing.col > 0:
                    related.append(DiagnosticRelated(
                        message=f"previous definition of '{sym.name}' is here",
                        span=SourceSpan(
                            start=SourcePos(existing.line, existing.col),
                            end=SourcePos(existing.line, existing.col + max(len(sym.name), 1)),
                        ),
                    ))
                self.diags.error(
                    f"'{sym.name}' already defined in this package",
                    sym.line, sym.col,
                    hint=f"rename or move it to a different package",
                    code="duplicate-definition",
                    related=related,
                )
                return False
            pkg_symbols[sym.name] = sym
            return True
        cur = self._scopes[-1]
        existing = cur.lookup(sym.name)
        if existing is not None:
            related = []
            if existing.line > 0 and existing.col > 0:
                related.append(DiagnosticRelated(
                    message=f"previous definition of '{sym.name}' is here",
                    span=SourceSpan(
                        start=SourcePos(existing.line, existing.col),
                        end=SourcePos(existing.line, existing.col + max(len(sym.name), 1)),
                    ),
                ))
            self.diags.error(
                f"'{sym.name}' already defined in this scope",
                sym.line, sym.col,
                hint=f"rename or shadow in a new block",
                code="duplicate-definition",
                related=related,
            )
            return False
        cur.define(sym)
        return True

    def lookup(self, name: str) -> Optional[Symbol]:
        for scope in reversed(self._scopes):
            sym = scope.lookup(name)
            if sym is not None:
                return sym
        if self._current_pkg is not None:
            sym = self._pkg_symbols.get(self._current_pkg, {}).get(name)
            if sym is not None:
                return sym
        return None

    def lookup_pkg_symbol(self, pkg_path: str, name: str) -> Optional[Symbol]:
        return self._pkg_symbols.get(pkg_path, {}).get(name)

    def lookup_any_pkg_symbol(self, name: str) -> Optional[Symbol]:
        for symbols in self._pkg_symbols.values():
            sym = symbols.get(name)
            if sym is not None:
                return sym
        return None

    def lookup_or_error(self, name: str, line: int = 0,
                        col: int = 0, span: Optional[SourceSpan] = None) -> Type:
        sym = self.lookup(name)
        if sym is None:
            self.diags.error(
                f"undefined name '{name}'",
                line, col,
                span=span,
                hint=self._suggest_name_hint(name),
                code="undefined-name",
            )
            return T_ERR
        return sym.type_

    # ── type registry ─────────────────────────────────────────

    def _attach_c_name(self, ty: Type, c_name: Optional[str]):
        if c_name is None:
            return
        try:
            setattr(ty, "_c_name", c_name)
        except Exception:
            pass

    def register_type(
        self,
        name: str,
        ty: Type,
        pkg_path: Optional[str] = None,
        c_name: Optional[str] = None,
        *,
        attach_c_name: bool = True,
    ):
        if attach_c_name:
            self._attach_c_name(ty, c_name)
        if pkg_path is not None:
            self._pkg_types.setdefault(pkg_path, {})[name] = ty
            if c_name is not None:
                self._pkg_type_c_names.setdefault(pkg_path, {})[name] = c_name
            return
        self._types[name] = ty
        if c_name is not None:
            self._type_c_names[name] = c_name

    def register_namespace(self, path: str):
        self._namespace_values.setdefault(path, {})
        self._namespace_types.setdefault(path, {})
        self._namespace_hidden.setdefault(path, {})
        self._namespace_value_c_names.setdefault(path, {})
        self._namespace_type_c_names.setdefault(path, {})

    def register_namespace_value(self, path: str, name: str, ty: Type, c_name: Optional[str] = None):
        self.register_namespace(path)
        self._namespace_values[path][name] = ty
        if c_name is not None:
            self._namespace_value_c_names[path][name] = c_name

    def register_namespace_type(self, path: str, name: str, ty: Type, c_name: Optional[str] = None):
        self.register_namespace(path)
        self._attach_c_name(ty, c_name)
        self._namespace_types[path][name] = ty
        if c_name is not None:
            self._namespace_type_c_names[path][name] = c_name

    def register_namespace_hidden(self, path: str, name: str, kind: str,
                                  *, is_value: bool = False, is_type: bool = False):
        self.register_namespace(path)
        self._namespace_hidden[path][name] = (kind, is_value, is_type)

    def lookup_namespace_value(self, path: str, name: str) -> Optional[Type]:
        return self._namespace_values.get(path, {}).get(name)

    def lookup_namespace_type(self, path: str, name: str) -> Optional[Type]:
        return self._namespace_types.get(path, {}).get(name)

    def lookup_namespace_value_c_name(self, path: str, name: str) -> Optional[str]:
        return self._namespace_value_c_names.get(path, {}).get(name)

    def lookup_namespace_type_c_name(self, path: str, name: str) -> Optional[str]:
        return self._namespace_type_c_names.get(path, {}).get(name)

    def lookup_namespace_hidden(self, path: str, name: str) -> Optional[Tuple[str, bool, bool]]:
        return self._namespace_hidden.get(path, {}).get(name)

    def bind_import(self, path: str, alias: Optional[str] = None):
        self.register_namespace(path)
        bind_name = alias or path.split(".")[-1]
        self.define(Symbol(bind_name, TNamespace(path), False, pkg_path=self._current_pkg))

    def bind_from_import(self, path: str, name: str, alias: Optional[str] = None):
        bind_name = alias or name
        value_ty = self.lookup_namespace_value(path, name)
        type_ty = self.lookup_namespace_type(path, name)
        value_c_name = self.lookup_namespace_value_c_name(path, name)
        type_c_name = self.lookup_namespace_type_c_name(path, name)
        hidden = self.lookup_namespace_hidden(path, name)
        if value_ty is not None:
            existing = self.lookup(bind_name)
            if existing is None or existing.type_ != value_ty:
                self.define(Symbol(bind_name, value_ty, False, pkg_path=self._current_pkg, c_name=value_c_name))
        if type_ty is not None:
            self.register_type(
                bind_name,
                type_ty,
                pkg_path=self._current_pkg,
                c_name=type_c_name,
                attach_c_name=False,
            )
            if isinstance(type_ty, TInterface):
                self.register_interface(type_ty)
        if value_ty is None and type_ty is None and hidden is not None:
            kind, is_value, is_type = hidden
            self.diags.error(
                f"cannot import private {kind} '{path}.{name}'",
                hint=f"add 'pub' to the declaration of '{name}' to make it importable",
                code="private-member",
            )
            if is_value:
                existing = self.lookup(bind_name)
                if existing is None:
                    self.define(Symbol(bind_name, T_ERR, False, pkg_path=self._current_pkg))
            if is_type:
                self.register_type(bind_name, T_ERR, pkg_path=self._current_pkg)

    def find_variant_type(self, variant_name: str) -> Optional[Type]:
        """Return the TUnion or TErrorSet that owns this variant name, or None."""
        from src.types import TErrorSet as _TES
        if self._current_pkg is not None:
            for ty in self._pkg_types.get(self._current_pkg, {}).values():
                if isinstance(ty, (TUnion, _TES)) and variant_name in ty.variants:
                    return ty
        for ty in self._types.values():
            if isinstance(ty, (TUnion, _TES)):
                if variant_name in ty.variants:
                    return ty
        return None

    def find_unit_variant_type(self, variant_name: str) -> Optional[Type]:
        """Return the TUnion or TErrorSet owning this unit (no-payload) variant."""
        from src.types import TErrorSet as _TES
        if self._current_pkg is not None:
            for ty in self._pkg_types.get(self._current_pkg, {}).values():
                if isinstance(ty, (TUnion, _TES)):
                    if variant_name in ty.variants and ty.variants[variant_name] is None:
                        return ty
        for ty in self._types.values():
            if isinstance(ty, (TUnion, _TES)):
                if variant_name in ty.variants and ty.variants[variant_name] is None:
                    return ty
        return None

    def lookup_type(self, name: str) -> Optional[Type]:
        if "." in name:
            path, leaf = name.rsplit(".", 1)
            ty = self.lookup_namespace_type(path, leaf)
            if ty is not None:
                return ty
        if self._current_pkg is not None:
            ty = self._pkg_types.get(self._current_pkg, {}).get(name)
            if ty is not None:
                return ty
        return self._types.get(name)

    def lookup_c_type_name(self, name: str) -> Optional[str]:
        if "." in name:
            path, leaf = name.rsplit(".", 1)
            c_name = self.lookup_namespace_type_c_name(path, leaf)
            if c_name is not None:
                return c_name
        if self._current_pkg is not None:
            c_name = self._pkg_type_c_names.get(self._current_pkg, {}).get(name)
            if c_name is not None:
                return c_name
        return self._type_c_names.get(name)

    def set_current_pkg(self, pkg_path: Optional[str]):
        self._current_pkg = pkg_path

    def lookup_type_or_error(self, name: str, line: int = 0,
                              col: int = 0, span: Optional[SourceSpan] = None) -> Type:
        ty = self.lookup_type(name)
        if ty is None:
            self.diags.error(
                f"unknown type '{name}'",
                line, col,
                span=span,
                hint=self._suggest_type_hint(name),
                code="unknown-type",
            )
            return T_ERR
        return ty

    def _suggest_name_hint(self, name: str) -> Optional[str]:
        names: List[str] = []
        seen: Set[str] = set()
        for scope in reversed(self._scopes):
            for candidate in scope._symbols.keys():
                if candidate not in seen:
                    names.append(candidate)
                    seen.add(candidate)
        if not names:
            return None
        matches = difflib.get_close_matches(name, names, n=1, cutoff=0.6)
        if matches:
            return f"did you mean '{matches[0]}'?"
        return None

    def _suggest_type_hint(self, name: str) -> Optional[str]:
        names = list(self._types.keys())
        matches = difflib.get_close_matches(name, names, n=1, cutoff=0.6)
        if matches:
            return f"did you mean '{matches[0]}'?"
        return None

    # ── interface registry ────────────────────────────────────

    def register_interface(self, iface: TInterface):
        self._interfaces[iface.name] = iface

    def lookup_interface(self, name: str) -> Optional[TInterface]:
        return self._interfaces.get(name)

    # ── return type ───────────────────────────────────────────

    def set_return_type(self, ty: Type):
        self._return_type = ty

    def get_return_type(self) -> Optional[Type]:
        return self._return_type

    # ── struct context ────────────────────────────────────────

    def set_current_struct(self, name: Optional[str]):
        self._current_struct = name

    def get_current_struct(self) -> Optional[str]:
        return self._current_struct

    # ── loop context ─────────────────────────────────────────

    def find_loop(self, label: Optional[str] = None) -> Optional[Scope]:
        """Find the innermost loop, or a named loop if label given."""
        if not self._loop_stack: return None
        if label is None: return self._loop_stack[-1]
        for loop in reversed(self._loop_stack):
            if loop.label == label: return loop
        return None

    # ── constraint checking ───────────────────────────────────

    def check_constraint(self, ty: Type, iface_name: str) -> bool:
        """
        Check if ty implements iface_name.
        Returns True if satisfied, False if not.
        """
        if ty.is_error(): return True   # don't cascade

        type_name = self._type_name(ty)
        if type_name is None: return False

        # primitives implement their compiler-defined interfaces
        if isinstance(ty, TInt):
            if iface_name in ("Add", "Sub", "Mul", "Div",
                              "Eq", "Ord", "Num", "Integer", "Bitwise"):
                return True
        if isinstance(ty, TFloat):
            if iface_name in ("Add", "Sub", "Mul", "Div",
                              "Eq", "Ord", "Num", "Numeric", "Float"):
                return True
        if isinstance(ty, TBool):
            if iface_name in ("Eq",): return True

        return self.impls.implements(type_name, iface_name)

    def _type_name(self, ty: Type) -> Optional[str]:
        if isinstance(ty, TStruct):  return ty.name
        if isinstance(ty, TUnion):   return ty.name
        if isinstance(ty, TInt):
            return f"{'i' if ty.signed else 'u'}{ty.bits}"
        if isinstance(ty, TFloat):   return f"f{ty.bits}"
        if isinstance(ty, TBool):    return "bool"
        if isinstance(ty, TString):  return "str"
        return None

    def find_operator_impl(self, op: str, left_ty: Type,
                           right_ty: Type) -> Optional[Type]:
        """
        Find the return type of op(left, right) by looking up
        the appropriate interface implementation.
        Returns None if no implementation found.
        """
        if left_ty.is_error() or right_ty.is_error():
            return T_ERR

        # Map operator to interface name
        op_to_iface = {
            "+": "Add",  "-": "Sub",  "*": "Mul",
            "/": "Div",  "%": "Mod",  "^": "Pow",
            "==": "Eq",  "!=": "Eq",
            "<": "Ord",  ">": "Ord",  "<=": "Ord",  ">=": "Ord",
        }
        # broadcast ops map to same interfaces
        broadcast_to_base = {
            ".+": "+", ".-": "-", ".*": "*", "./": "/",
            ".%": "%", ".^": "^",
            ".==": "==", ".!=": "!=",
            ".<": "<",  ".>": ">", ".<=": "<=", ".>=": ">=",
        }
        actual_op = broadcast_to_base.get(op, op)
        iface     = op_to_iface.get(actual_op)
        if iface is None: return None

        type_name = self._type_name(left_ty)
        if type_name is None: return None


        # bool supports == and != 
        if isinstance(left_ty, TBool) and isinstance(right_ty, TBool):
            if actual_op in ("==", "!="):
                return T_BOOL
            return None

        # str supports == and !=
        if isinstance(left_ty, TString) and isinstance(right_ty, TString):
            if actual_op in ("==", "!="):
                return T_BOOL
            return None

        # primitives
        if isinstance(left_ty, (TInt, TFloat, TIntLit, TFloatLit)):
            resolved_left  = default_numeric(left_ty)
            resolved_right = default_numeric(right_ty)
            unified = unify(resolved_left, resolved_right)
            if unified.is_error(): return None
            # comparison ops return bool
            if actual_op in ("==", "!=", "<", ">", "<=", ">="):
                return T_BOOL
            # broadcast comparison returns mat[bool]
            if op in (".==", ".!=", ".<", ".>", ".<=", ".>="):
                return T_BOOL   # simplified — full impl returns mat[bool]
            return unified

        # user types — look up impl
        impl_method = self.impls.find_method(type_name,
            {"Add": "add", "Sub": "sub", "Mul": "mul",
             "Div": "div", "Eq": "eq", "Ord": "lt"}.get(iface, iface))
        if impl_method:
            return impl_method.ret
        return None
