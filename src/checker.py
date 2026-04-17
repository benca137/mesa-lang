"""
Mesa type checker.

Architecture:
  Pass 1 — DeclarationPass:
    Register all types, function signatures, interfaces, def impls.
    Does not check bodies. Allows mutual recursion.

  Pass 2 — BodyChecker:
    Type check all function bodies and package-level lets.
    Bidirectional: synth() for bottom-up, check() for top-down.
    TError propagation prevents error cascades.

  DiagnosticPass (triggered on error):
    Re-checks a specific generic instantiation with full monomorphisation.
    Produces rich error messages with call site info.
"""
from __future__ import annotations
import copy
import os
from typing import Dict, List, Optional, Tuple
import math
from src.ast import *
from src.types import *
from src.env import Environment, DiagnosticBag, Symbol, ImplRegistry
from src.stdlib import canonicalize_std_import_path, is_std_source_path


_INTERNAL_STDLIB_INTRINSICS = {
    "@alloc": "std.mem",
    "@realloc": "std.mem",
    "@freeBytes": "std.mem",
    "@pageSize": "std.mem",
    "@pageAlloc": "std.mem",
    "@pageFree": "std.mem",
    "@cAlloc": "std.mem",
    "@cRealloc": "std.mem",
    "@cFree": "std.mem",
    "@ptrAdd": "std.mem",
}


def _make_test_intrinsic_types() -> tuple[TStruct, TStruct]:
    diagnostic_ty = TStruct(
        name="TestDiagnostic",
        fields={
            "code": T_STR,
            "message": T_STR,
            "hint": T_STR,
            "line": T_I64,
            "col": T_I64,
        },
        methods={},
    )
    setattr(diagnostic_ty, "_c_name", "mesa_test_diagnostic")
    result_ty = TStruct(
        name="TestCompileResult",
        fields={
            "ok": T_BOOL,
            "errors": TVec(diagnostic_ty, None),
        },
        methods={},
    )
    setattr(result_ty, "_c_name", "mesa_test_compile_result")
    return diagnostic_ty, result_ty


def _all_function_entries(program: Program) -> List[tuple[FunDecl, Optional[str]]]:
    entries: List[tuple[FunDecl, Optional[str]]] = []
    for decl in program.decls:
        if isinstance(decl, FunDecl):
            entries.append((decl, None))
        elif isinstance(decl, StructDecl):
            for m in decl.methods:
                entries.append((m, decl.name))
        elif isinstance(decl, DefDecl):
            for m in decl.methods:
                entries.append((m, decl.for_type))
    return entries


def _function_symbol_name(f: FunDecl, receiver: Optional[str]) -> str:
    return f"{receiver}.{f.name}" if receiver else f.name


def _c_pkg_token(pkg_path: Optional[str]) -> str:
    return (pkg_path or "root").replace(".", "__")


def _c_type_name(pkg_path: Optional[str], name: str) -> str:
    return f"mesa__{_c_pkg_token(pkg_path)}__{name}"


def _c_function_name(pkg_path: Optional[str], name: str, receiver: Optional[str] = None) -> str:
    if receiver:
        return f"{_c_type_name(pkg_path, receiver)}__{name}"
    return f"mesa__{_c_pkg_token(pkg_path)}__{name}"


def _qualified_name(expr: Expr) -> Optional[str]:
    if isinstance(expr, Ident):
        return expr.name
    if isinstance(expr, FieldExpr):
        left = _qualified_name(expr.obj)
        if left is None:
            return None
        return f"{left}.{expr.field}"
    return None


def effective_return_type(f: FunDecl, env: Environment) -> Type:
    if getattr(f, "_type_params", []):
        return lower_type(f.ret, env)
    return getattr(f, "_effective_ret_type", None) or lower_type(f.ret, env)


# ══════════════════════════════════════════════════════════════
# Type lowering — AST TypeExpr → internal Type
# ══════════════════════════════════════════════════════════════
def lower_type(ty: TypeExpr, env: Environment,
               line: int = 0, col: int = 0) -> Type:
    """Convert an AST type expression to an internal Type."""
    span = getattr(ty, "span", None)
    if span is not None and (line <= 0 or col <= 0):
        line = span.start.line
        col = span.start.col

    if isinstance(ty, TyPrimitive):
        return PRIMITIVE_MAP.get(ty.name, T_ERR)

    if isinstance(ty, TyNamed):
        # resolve Self to current struct
        if ty.name == "Self":
            struct_name = env.get_current_struct()
            if struct_name:
                t = env.lookup_type(struct_name)
                return t if t else T_ERR
            # Self in interface signatures is valid — returns a type variable
            # that will be resolved when the interface is implemented
            return TVar(name="Self")
        return env.lookup_type_or_error(ty.name, line, col, span=span)

    if isinstance(ty, TyAnyInterface):
        from src.types import TAnyInterface, TInterface
        iface_ty = env.lookup_type(ty.iface_name)
        if not isinstance(iface_ty, TInterface):
            env.diags.error(f"'{ty.iface_name}' is not an interface", line, col)
            return T_ERR
        return TAnyInterface(iface=iface_ty)

    if isinstance(ty, TyPointer):
        inner_ty = lower_type(ty.inner, env, line, col)
        # *any Interface → TDynInterface (heap fat pointer)
        from src.types import TDynInterface, TAnyInterface
        if isinstance(inner_ty, TAnyInterface):
            return TDynInterface(iface=inner_ty.iface)
        return TPointer(inner_ty)

    if isinstance(ty, TySlice):
        return TSlice(lower_type(ty.inner, env, line, col))

    if isinstance(ty, TyOptional):
        return TOptional(lower_type(ty.inner, env, line, col))

    if isinstance(ty, TyErrorSetUnion):
        members: List[TErrorSet] = []
        seen: set[str] = set()
        for member_expr in ty.members:
            member_ty = lower_type(member_expr, env, line, col)
            for member in error_set_members(member_ty):
                if member.name not in seen:
                    seen.add(member.name)
                    members.append(member)
            if not isinstance(member_ty, (TErrorSet, TErrorSetUnion)):
                env.diags.error(
                    f"expected an error set in error union, got {format_type_for_user(member_ty)}",
                    line, col
                )
                return T_ERR
        try:
            error_set_variants(TErrorSetUnion(tuple(sorted(members, key=lambda m: m.name))))
        except ValueError as e:
            env.diags.error(str(e), line, col)
            return T_ERR
        return merge_error_sets(*members)

    if isinstance(ty, TyErrorUnion):
        payload = lower_type(ty.payload, env, line, col)
        eset    = lower_type(ty.error_set, env, line, col) if ty.error_set else None
        return TErrorUnion(eset, payload)

    if isinstance(ty, TyTuple):
        fields = [(name, lower_type(t, env, line, col))
                  for name, t in ty.fields]
        return TTuple(fields)

    if isinstance(ty, TyVec):
        inner = lower_type(ty.elem, env, line, col)
        size  = None
        if isinstance(ty.size, IntLit):
            size = ty.size.value
        return TVec(inner, size)

    if isinstance(ty, TyMat):
        inner = lower_type(ty.elem, env, line, col)
        rows  = ty.rows.value if isinstance(ty.rows, IntLit) else None
        cols  = ty.cols.value if isinstance(ty.cols, IntLit) else None
        return TMat(inner, rows, cols)

    if isinstance(ty, TyUnitful):
        from src.types import TUnitful, make_unitful
        inner = lower_type(ty.inner, env, line, col)
        # Get user unit registry from env if available
        registry = getattr(env, '_unit_registry', None)
        try:
            return make_unitful(inner, ty.unit, registry)
        except ValueError as e:
            env.diags.error(str(e), line, col)
            return T_ERR

    if isinstance(ty, TyFun):
        params = [lower_type(p, env, line, col) for p in ty.params]
        ret    = lower_type(ty.ret, env, line, col)
        return TFun(params, ret)

    if isinstance(ty, TyGeneric):
        # Generic instantiation — substitute concrete type args into the base type
        base = env.lookup_type(ty.name)
        if base is None:
            env.diags.error(f"unknown type '{ty.name}'", line, col, span=span)
            return T_ERR
        args = [lower_type(p, env, line, col) for p in ty.params]
        if isinstance(base, TStruct):
            if len(args) != len(base.type_params):
                env.diags.error(
                    f"wrong number of type arguments for '{ty.name}': expected {len(base.type_params)}, got {len(args)}",
                    line, col
                )
                return T_ERR
            mapping = dict(zip(base.type_params, args))
            fields = {name: substitute(field_ty, mapping)
                      for name, field_ty in base.fields.items()}
            methods = {name: substitute(method_ty, mapping)
                       for name, method_ty in base.methods.items()}
            return TStruct(
                name=base.name,
                fields=fields,
                methods=methods,
                type_params=[],
                type_args=mapping,
            )
        if isinstance(base, TUnion):
            if len(args) != len(base.type_params):
                env.diags.error(
                    f"wrong number of type arguments for '{ty.name}': expected {len(base.type_params)}, got {len(args)}",
                    line, col
                )
                return T_ERR
            mapping = dict(zip(base.type_params, args))
            variants = {
                name: (substitute(variant_ty, mapping) if variant_ty is not None else None)
                for name, variant_ty in base.variants.items()
            }
            return TUnion(name=base.name, variants=variants, type_params=[])
        if isinstance(base, TInterface):
            if len(args) != len(base.params):
                env.diags.error(
                    f"wrong number of type arguments for '{ty.name}': expected {len(base.params)}, got {len(args)}",
                    line, col
                )
                return T_ERR
            mapping = dict(zip(base.params, args))
            methods = {name: substitute(method_ty, mapping)
                       for name, method_ty in base.methods.items()}
            return TInterface(
                name=base.name,
                params=[],
                methods=methods,
                parents=list(base.parents),
                defaults=set(base.defaults),
            )
        return base

    if isinstance(ty, (TyVoid, type(TyVoid()))):
        return T_VOID

    if isinstance(ty, TyInfer):
        return TVar()   # will be filled by unification

    return T_ERR


# ══════════════════════════════════════════════════════════════
# Pass 1 — Declaration pass
# ══════════════════════════════════════════════════════════════
class DeclarationPass:
    """
    Walk top-level declarations, register all types and signatures.
    Does NOT check bodies — that's Pass 2.
    """
    def __init__(self, env: Environment):
        self.env = env

    def run(self, program: Program):
        self._register_builtins()
        type_like = (StructDecl, UnionDecl, InterfaceDecl, TypeAlias, UnitAlias, ErrorDecl)
        function_like = (FunDecl, DefDecl, LetStmt)
        pkg_order: List[Optional[str]] = []
        pkg_groups: Dict[Optional[str], List[Decl]] = {}
        for decl in program.decls:
            pkg_path = getattr(decl, "_pkg_path", None)
            if pkg_path not in pkg_groups:
                pkg_order.append(pkg_path)
                pkg_groups[pkg_path] = []
            pkg_groups[pkg_path].append(decl)

        for pkg_path in pkg_order:
            group = pkg_groups[pkg_path]
            for decl in group:
                if not isinstance(decl, (ImportDecl, FromImportDecl)):
                    continue
                self.env.set_current_pkg(pkg_path)
                if getattr(decl, "path", None) != pkg_path:
                    self._register_decl(decl)
            for decl in group:
                if isinstance(decl, (ImportDecl, FromImportDecl)):
                    continue
                if isinstance(decl, InterfaceDecl):
                    self.env.set_current_pkg(pkg_path)
                    self._register_decl(decl)
            for decl in group:
                if isinstance(decl, (ImportDecl, FromImportDecl, InterfaceDecl)):
                    continue
                if isinstance(decl, type_like):
                    self.env.set_current_pkg(pkg_path)
                    self._register_decl(decl)
            for decl in group:
                if isinstance(decl, (ImportDecl, FromImportDecl)):
                    continue
                if isinstance(decl, function_like):
                    self.env.set_current_pkg(pkg_path)
                    self._register_decl(decl)
            for decl in group:
                if not isinstance(decl, (ImportDecl, FromImportDecl)):
                    continue
                self.env.set_current_pkg(pkg_path)
                if getattr(decl, "path", None) == pkg_path:
                    self._register_decl(decl)
        self.env.set_current_pkg(None)

    def _register_builtins(self):
        """Register built-in functions so they resolve in type checking."""
        from src.types import TFun, T_VOID, T_STR, T_I64, T_F64
        # println/print are special-cased later to accept more printable values.
        println_str = TFun([T_STR], T_VOID)
        println_int = TFun([T_I64], T_VOID)
        println_f64 = TFun([T_F64], T_VOID)
        len_fn = TFun([TVar("T")], T_I64)
        raw_ptr = TPointer(T_VOID)
        i32_ty = TInt(32, True)
        test_diagnostic_ty, test_result_ty = _make_test_intrinsic_types()
        self.env._test_diagnostic_type = test_diagnostic_ty
        self.env._test_compile_result_type = test_result_ty
        self.env.define(Symbol("println", println_str, False))
        self.env.define(Symbol("print",   println_str, False))
        self.env.define(Symbol("println_i64", println_int, False))
        self.env.define(Symbol("println_f64", println_f64, False))
        self.env.define(Symbol("len", len_fn, False))
        self.env.define(Symbol("cap", len_fn, False))
        self.env.define(Symbol("@typeof", TFun([TVar("T")], T_STR), False))
        self.env.define(Symbol("@assert", TFun([T_BOOL], T_VOID), False))
        self.env.define(Symbol("@sizeOf", TFun([TVar("T")], T_I64), False))
        self.env.define(Symbol("@alignOf", TFun([TVar("T")], T_I64), False))
        self.env.define(Symbol("@hasField", TFun([TVar("T"), T_STR], T_BOOL), False))
        self.env.define(Symbol("@alloc", TFun([TVar("A"), T_I64, T_I64], raw_ptr), False))
        self.env.define(Symbol("@realloc", TFun([TVar("A"), raw_ptr, T_I64, T_I64, T_I64], raw_ptr), False))
        self.env.define(Symbol("@freeBytes", TFun([TVar("A"), raw_ptr, T_I64], T_VOID), False))
        self.env.define(Symbol("@memcpy", TFun([raw_ptr, raw_ptr, T_I64], raw_ptr), False))
        self.env.define(Symbol("@memmove", TFun([raw_ptr, raw_ptr, T_I64], raw_ptr), False))
        self.env.define(Symbol("@memset", TFun([raw_ptr, T_I64, T_I64], raw_ptr), False))
        self.env.define(Symbol("@memcmp", TFun([raw_ptr, raw_ptr, T_I64], i32_ty), False))
        self.env.define(Symbol("@panic", TFun([T_STR], T_VOID), False))
        self.env.define(Symbol("@pageSize", TFun([], T_I64), False))
        self.env.define(Symbol("@pageAlloc", TFun([T_I64], raw_ptr), False))
        self.env.define(Symbol("@pageFree", TFun([raw_ptr, T_I64], T_VOID), False))
        self.env.define(Symbol("@cAlloc", TFun([T_I64, T_I64], raw_ptr), False))
        self.env.define(Symbol("@cRealloc", TFun([raw_ptr, T_I64, T_I64, T_I64], raw_ptr), False))
        self.env.define(Symbol("@cFree", TFun([raw_ptr], T_VOID), False))
        self.env.define(Symbol("@ptrAdd", TFun([raw_ptr, T_I64], raw_ptr), False))
        self.env.define(Symbol("@test", TNamespace("@test"), False))
        self.env.register_namespace_value("@test", "compile", TFun([T_STR], test_result_ty))
        self.env.register_namespace_value("@test", "compileFile", TFun([T_STR], test_result_ty))
        self.env.register_namespace_type("@test", "Diagnostic", test_diagnostic_ty, c_name="mesa_test_diagnostic")
        self.env.register_namespace_type("@test", "CompileResult", test_result_ty, c_name="mesa_test_compile_result")

    def _register_decl(self, decl: Decl):
        if isinstance(decl, StructDecl):    self._register_struct(decl)
        elif isinstance(decl, UnionDecl):   self._register_union(decl)
        elif isinstance(decl, InterfaceDecl): self._register_interface(decl)
        elif isinstance(decl, TypeAlias):   self._register_alias(decl)
        elif isinstance(decl, UnitAlias):   self._register_unit_alias(decl)
        elif isinstance(decl, ErrorDecl):   self._register_error(decl)
        elif isinstance(decl, FunDecl):     self._register_fun(decl, receiver=None)
        elif isinstance(decl, DefDecl):     self._register_def(decl)
        elif isinstance(decl, ImportDecl):  self.env.bind_import(decl.path, decl.alias)
        elif isinstance(decl, FromImportDecl):
            for name, alias in decl.names:
                self.env.bind_from_import(decl.path, name, alias)
        elif decl.__class__.__name__ in {"PkgExportDecl", "PkgExportAllDecl"}:
            return
        elif isinstance(decl, LetStmt):     pass   # handled in pass 2
        self._register_pkg_namespace_member(decl)

    def _register_pkg_namespace_member(self, decl: Decl):
        pkg_path = getattr(decl, "_pkg_path", None)
        if not pkg_path:
            return
        export_names = getattr(decl, "_pkg_export_names", None)
        if getattr(decl, "_pkg_facade_controlled", False):
            self.env.register_namespace(pkg_path)
            if not export_names:
                if isinstance(decl, FunDecl):
                    self.env.register_namespace_hidden(pkg_path, decl.name, "function", is_value=True)
                elif isinstance(decl, LetStmt):
                    self.env.register_namespace_hidden(pkg_path, decl.name, "value", is_value=True)
                elif isinstance(decl, (StructDecl, UnionDecl, InterfaceDecl, ErrorDecl, TypeAlias)):
                    self.env.register_namespace_hidden(pkg_path, decl.name, "type", is_value=True, is_type=True)
                return
            self._register_pkg_exports(pkg_path, decl, export_names)
            return
        vis = getattr(decl, "vis", Visibility.PRIVATE)
        if vis == Visibility.PRIVATE:
            if isinstance(decl, FunDecl):
                self.env.register_namespace_hidden(pkg_path, decl.name, "function", is_value=True)
            elif isinstance(decl, LetStmt):
                self.env.register_namespace_hidden(pkg_path, decl.name, "value", is_value=True)
            elif isinstance(decl, (StructDecl, UnionDecl, InterfaceDecl, ErrorDecl, TypeAlias)):
                self.env.register_namespace_hidden(pkg_path, decl.name, "type", is_value=True, is_type=True)
            return
        self.env.register_namespace(pkg_path)
        if isinstance(decl, (StructDecl, UnionDecl, InterfaceDecl, ErrorDecl, TypeAlias)):
            ty = self.env.lookup_type(getattr(decl, "name", ""))
            if ty is not None:
                c_name = self.env.lookup_c_type_name(decl.name)
                self.env.register_namespace_value(pkg_path, decl.name, ty, c_name=c_name)
                self.env.register_namespace_type(pkg_path, decl.name, ty, c_name=c_name)
        elif isinstance(decl, FunDecl):
            sym = self.env.lookup_pkg_symbol(pkg_path, decl.name)
            if sym is not None:
                self.env.register_namespace_value(pkg_path, decl.name, sym.type_, c_name=sym.c_name)
        elif isinstance(decl, LetStmt):
            sym = self.env.lookup_pkg_symbol(pkg_path, decl.name)
            if sym is not None:
                self.env.register_namespace_value(pkg_path, decl.name, sym.type_, c_name=sym.c_name)

    def _opaque_export_type(self, decl: Decl, public_name: str):
        ty = self.env.lookup_type(getattr(decl, "name", ""))
        if ty is None:
            return None
        if isinstance(ty, TStruct):
            opaque_ty = copy.deepcopy(ty)
            opaque_ty.name = public_name
            opaque_ty.fields = {}
            opaque_ty.methods = {}
            return opaque_ty
        if isinstance(ty, TUnion):
            opaque_ty = copy.deepcopy(ty)
            opaque_ty.name = public_name
            opaque_ty.variants = {}
            return opaque_ty
        if isinstance(ty, TErrorSet):
            opaque_ty = copy.deepcopy(ty)
            opaque_ty.name = public_name
            opaque_ty.variants = {}
            return opaque_ty
        return ty

    def _register_pkg_exports(self, pkg_path: str, decl: Decl, export_names: List[Tuple[str, bool]]):
        if isinstance(decl, (StructDecl, UnionDecl, InterfaceDecl, ErrorDecl, TypeAlias)):
            base_ty = self.env.lookup_type(getattr(decl, "name", ""))
            if base_ty is None:
                return
            c_name = self.env.lookup_c_type_name(getattr(decl, "name", ""))
            for public_name, opaque in export_names:
                export_ty = self._opaque_export_type(decl, public_name) if opaque else base_ty
                self.env.register_namespace_value(pkg_path, public_name, export_ty, c_name=c_name)
                self.env.register_namespace_type(pkg_path, public_name, export_ty, c_name=c_name)
        elif isinstance(decl, FunDecl):
            sym = self.env.lookup_pkg_symbol(pkg_path, decl.name)
            if sym is None:
                return
            for public_name, _opaque in export_names:
                self.env.register_namespace_value(pkg_path, public_name, sym.type_, c_name=sym.c_name)
        elif isinstance(decl, LetStmt):
            sym = self.env.lookup_pkg_symbol(pkg_path, decl.name)
            if sym is None:
                return
            for public_name, _opaque in export_names:
                self.env.register_namespace_value(pkg_path, public_name, sym.type_, c_name=sym.c_name)

    def _register_struct(self, s: StructDecl):
        # First pass: register opaque struct so methods can reference it
        fields  = {}
        methods = {}
        struct_ty = TStruct(
            name=s.name,
            fields=fields,
            methods=methods,
            type_params=s.params,
        )
        pkg_path = getattr(s, "_pkg_path", None)
        c_name = _c_type_name(pkg_path, s.name)
        setattr(s, "_c_name", c_name)
        self.env.register_type(s.name, struct_ty, pkg_path=pkg_path, c_name=c_name)

        # Register generic type params as TVar so they resolve in field types
        for param in s.params:
            self.env.register_type(param, TVar(name=param))

        # Set context so 'self: *P' resolves correctly
        self.env.set_current_struct(s.name)

        # Fill in fields
        for f in s.fields:
            fields[f.name] = lower_type(f.type_, self.env)

        # Register methods
        for m in s.methods:
            struct_type_params = list(s.params)
            existing_type_params = list(getattr(m, "_type_params", []))
            if struct_type_params:
                merged = list(existing_type_params)
                for param in struct_type_params:
                    if param not in merged:
                        merged.append(param)
                m._type_params = merged
            m._receiver = s.name
            if getattr(m, "_pkg_path", None) is None:
                m._pkg_path = pkg_path
            if getattr(m, "_source_file", None) is None:
                m._source_file = getattr(s, "_source_file", None)
            fun_ty = self._fun_type(m)
            m._effective_ret_type = fun_ty.ret
            methods[m.name] = fun_ty
            # Also register as a global symbol for direct calls
            mangled = f"{s.name}.{m.name}"
            method_c_name = _c_function_name(pkg_path, m.name, s.name)
            setattr(m, "_c_name", method_c_name)
            self.env.define(Symbol(mangled, fun_ty, False, pkg_path=pkg_path, c_name=method_c_name, decl_node=m))

        self.env.set_current_struct(None)

    def _register_union(self, e: UnionDecl):
        variants = {}
        for v in e.variants:
            payload = lower_type(v.payload, self.env) if v.payload else None
            variants[v.name] = payload
        pkg_path = getattr(e, "_pkg_path", None)
        c_name = _c_type_name(pkg_path, e.name)
        setattr(e, "_c_name", c_name)
        self.env.register_type(e.name, TUnion(e.name, variants, e.params), pkg_path=pkg_path, c_name=c_name)

    def _register_interface(self, i: InterfaceDecl):
        methods  = {}
        defaults = set()
        for m in i.methods:
            methods[m.name] = self._fun_type(m)
            if m.body is not None:
                defaults.add(m.name)
        iface = TInterface(
            name=i.name,
            params=i.params,
            methods=methods,
            parents=i.parents,
            defaults=defaults,
        )
        self.env.register_interface(iface)
        pkg_path = getattr(i, "_pkg_path", None)
        c_name = _c_type_name(pkg_path, i.name)
        setattr(i, "_c_name", c_name)
        self.env.register_type(i.name, iface, pkg_path=pkg_path, c_name=c_name)

    def _register_alias(self, a: TypeAlias):
        ty = lower_type(a.type_, self.env)
        if isinstance(ty, TErrorSetUnion):
            ty = TErrorSetUnion(ty.members, name=a.name)
        pkg_path = getattr(a, "_pkg_path", None)
        c_name = _c_type_name(pkg_path, a.name)
        setattr(a, "_c_name", c_name)
        self.env.register_type(a.name, ty, pkg_path=pkg_path, c_name=c_name, attach_c_name=False)

    def _register_unit_alias(self, a):
        """Register a user-defined unit into the environment's unit registry."""
        from src.types import make_unitful, T_F64
        from src.ast import UnitLit, FloatLit, IntLit
        registry = self.env._unit_registry
        try:
            defn = a.defn
            if isinstance(defn, UnitLit):
                base_unitful = make_unitful(T_F64, defn.unit, registry)
                scale = base_unitful.scale
                if defn.value is not None:
                    if isinstance(defn.value, FloatLit):
                        scale = defn.value.value * base_unitful.scale
                    elif isinstance(defn.value, IntLit):
                        scale = defn.value.value * base_unitful.scale
                registry[a.name] = (base_unitful.dims, scale)
        except (ValueError, AttributeError, KeyError) as e:
            self.diags.error(f"invalid unit definition '{a.name}': {e}",
                             line=0, col=0)

    def _register_error(self, e: ErrorDecl):
        from src.types import TErrorSet
        variants = {}
        for v in e.variants:
            payload_ty = lower_type(v.payload, self.env) if v.payload else None
            variants[v.name] = payload_ty
        pkg_path = getattr(e, "_pkg_path", None)
        c_name = _c_type_name(pkg_path, e.name)
        setattr(e, "_c_name", c_name)
        self.env.register_type(e.name, TErrorSet(name=e.name, variants=variants), pkg_path=pkg_path, c_name=c_name)

    def _register_fun(self, f: FunDecl, receiver: Optional[str]):
        # Temporarily register type params so lower_type can resolve them
        type_params = getattr(f, '_type_params', [])
        for param in type_params:
            self.env.register_type(param, TVar(name=param))
        fun_ty = self._fun_type(f)
        f._effective_ret_type = fun_ty.ret
        name   = f"{receiver}.{f.name}" if receiver else f.name
        pkg_path = getattr(f, "_pkg_path", None)
        c_name = _c_function_name(pkg_path, f.name, receiver)
        link_name = None
        if getattr(f, "is_extern", False):
            for attr in getattr(f, "attrs", []) or []:
                if attr.name not in {"link_name", "cname"}:
                    continue
                if isinstance(attr.value, StringLit):
                    link_name = attr.value.raw
                else:
                    self._error_at(
                        f,
                        f"attribute '{attr.name}' on extern function '{f.name}' requires a string literal",
                        hint='use #[link_name = "symbol_name"]',
                    )
                break
        setattr(f, "_c_name", c_name)
        setattr(f, "_link_name", link_name)
        self.env.define(Symbol(name, fun_ty, False,
                                0,
                                pkg_path=pkg_path,
                                c_name=c_name,
                                decl_node=f))

    def _register_def(self, d: DefDecl):
        self.env.set_current_struct(d.for_type)
        for iface_name in d.interfaces:
            methods = self.env.impls._impls.get((d.for_type, iface_name), {})
            self.env.impls.register(d.for_type, iface_name, methods)
        for method in d.methods:
            # Register method type params
            for param in getattr(method, '_type_params', []):
                self.env.register_type(param, TVar(name=param))
            fun_ty  = self._fun_type(method)
            method._effective_ret_type = fun_ty.ret
            mangled = f"{d.for_type}.{method.name}"
            pkg_path = getattr(d, "_pkg_path", None)
            c_name = _c_function_name(pkg_path, method.name, d.for_type)
            setattr(method, "_c_name", c_name)
            self.env.define(Symbol(mangled, fun_ty, False, pkg_path=pkg_path, c_name=c_name, decl_node=method))
            # Register in impl registry for each interface
            for iface_name in d.interfaces:
                methods = self.env.impls._impls.get(
                    (d.for_type, iface_name), {})
                methods[method.name] = fun_ty
                self.env.impls.register(d.for_type, iface_name, methods)
        self.env.set_current_struct(None)

    def _fun_type(self, f: FunDecl) -> TFun:
        params = []
        for p in f.params:
            params.append(lower_type(p.type_, self.env))
        ret = lower_type(f.ret, self.env)
        return TFun(params, ret,
                    type_params=[], where=f.body and [] or [])


# ══════════════════════════════════════════════════════════════
# Pass 2 — Body checker with bidirectional inference
# ══════════════════════════════════════════════════════════════
class BodyChecker:
    def __init__(self, env: Environment):
        self.env  = env
        self.diags = env.diags
        self._region_counter = 1
        self._region_parent: Dict[int, Optional[int]] = {}
        self._handle_error_sets: List[set[Type]] = []
        self._current_source_file: Optional[str] = None
        self._allow_internal_stdlib_intrinsics = False
        self._in_test_block = False

    def _fresh_region(self) -> int:
        region = self._region_counter
        self._region_counter += 1
        return region

    def _current_region(self) -> Optional[int]:
        stack = getattr(self.env, '_allocator_stack', None) or []
        if stack:
            return stack[-1][2]
        alloc = getattr(self.env, '_active_allocator', None)
        return alloc[2] if alloc is not None else None

    def _outer_allocator(self):
        stack = getattr(self.env, '_allocator_stack', None) or []
        if len(stack) >= 2:
            return stack[-2]
        return None

    def _outer_region(self) -> Optional[int]:
        outer = self._outer_allocator()
        return outer[2] if outer is not None else None

    def _is_allocator_type(self, ty: Type) -> bool:
        if isinstance(ty, TPointer):
            ty = ty.inner
        if isinstance(ty, TStruct):
            return self.env.impls.implements(ty.name, "Allocator")
        return False

    def _is_allocator_operand_type(self, ty: Type) -> bool:
        if self._is_allocator_type(ty):
            return True
        return isinstance(ty, (TDynInterface, TAnyInterface)) and ty.iface.name == "Allocator"

    def _pointerish(self, ty: Type) -> bool:
        return isinstance(ty, (TPointer, TDynInterface))

    def _require_internal_stdlib_intrinsic(self, expr: Expr, name: str) -> bool:
        if self._allow_internal_stdlib_intrinsics:
            return True
        owner = _INTERNAL_STDLIB_INTRINSICS.get(name, "std")
        if owner == "std.mem":
            hint = "use the mem package API instead of compiler-only allocator intrinsics"
        elif owner == "std.io":
            hint = "use the io package API instead of compiler-only stdlib intrinsics"
        else:
            hint = "use the std package API instead of compiler-only stdlib intrinsics"
        self._error_at(
            expr,
            f"{name} is an internal {owner} intrinsic",
            hint=hint,
        )
        return False

    def _intrinsic_target_type(self, expr: Expr) -> Optional[Type]:
        qname = _qualified_name(expr)
        if qname is not None:
            ty = self.env.lookup_type(qname)
            if ty is not None:
                return ty.root() if isinstance(ty, TVar) else ty
        ty = self._synth_expr(expr)
        return ty.root() if isinstance(ty, TVar) else ty

    def _compile_time_has_field(self, ty: Optional[Type], field_name: str) -> bool:
        if ty is None or ty.is_error():
            return False
        if isinstance(ty, TPointer):
            ty = ty.inner
        if isinstance(ty, TStruct):
            return field_name in ty.fields or field_name in ty.methods or self.env.impls.find_method(ty.name, field_name) is not None
        if isinstance(ty, TTuple):
            return ty.field_type(field_name) is not None
        if isinstance(ty, TVec):
            return field_name in {"len", "cap"}
        if isinstance(ty, TSlice):
            return field_name == "len"
        if isinstance(ty, TString):
            return field_name in {"len", "data"}
        if isinstance(ty, (TDynInterface, TAnyInterface)):
            return field_name in ty.iface.methods
        if isinstance(ty, (TErrorSet, TErrorSetUnion)):
            return field_name in error_set_variants(ty)
        return False

    def _test_compile_result_type(self) -> Type:
        return getattr(self.env, "_test_compile_result_type", T_ERR)

    def _string_literal_value(self, lit: StringLit) -> str:
        if any(not isinstance(segment, str) for segment in lit.segments):
            self._error_at(lit, "test compiler intrinsics do not support interpolated strings")
            return lit.raw
        try:
            return bytes(lit.raw, "utf-8").decode("unicode_escape")
        except Exception:
            return lit.raw

    def _run_nested_test_compile(
        self,
        *,
        source_override: Optional[str] = None,
        source_path: Optional[str] = None,
        local_root: Optional[str] = None,
    ) -> dict:
        from src.analysis import analyse
        from src.frontend import build_frontend_state_for_path

        target_path = source_path or self._current_source_file
        if target_path is None:
            return {
                "ok": False,
                "errors": [{
                    "code": "test-context-missing",
                    "message": "@test requires a real source file context",
                    "hint": "",
                    "line": 0,
                    "col": 0,
                }],
            }

        package_roots = list(getattr(self.env, "_package_roots", []) or [])
        state = build_frontend_state_for_path(
            target_path,
            source_override=source_override,
            package_roots=package_roots,
            local_root=local_root,
        )

        if state.program is not None and state.env is not None and not state.diags.has_errors():
            try:
                analyse(state.program, state.env)
            except Exception as exc:
                state.diags.error(
                    f"internal analysis error: {exc}",
                    code="internal-error",
                )

        errors = [{
            "code": diag.code or "",
            "message": diag.message,
            "hint": diag.hint or "",
            "line": diag.line,
            "col": diag.col,
        } for diag in state.diags.all_errors()]
        return {"ok": not errors, "errors": errors}

    def _allocator_target_region(self, alloc_expr: Expr, alloc_ty: Type) -> Optional[int]:
        stack = getattr(self.env, '_allocator_stack', None) or []
        qname = _qualified_name(alloc_expr)
        for resource_expr, _, region in reversed(stack):
            if qname is not None and qname == _qualified_name(resource_expr):
                return region
        return None

    def _esc_error_set(self) -> Optional[Type]:
        return self.env.lookup_type("EscError")

    def _esc_result_type(self, payload: Type) -> Type:
        return TErrorUnion(self._esc_error_set(), payload)

    def _is_esc_cloneable_type(self, ty: Type) -> bool:
        from src.types import (
            TInt, TFloat, TBool, TString, TVoid, TIntLit, TFloatLit,
            TOptional, TVec, TStruct, TUnion, TUnitful, TUncertain,
        )
        if ty.is_error():
            return True
        if isinstance(ty, (TInt, TFloat, TBool, TString, TVoid, TIntLit, TFloatLit)):
            return True
        if isinstance(ty, TOptional):
            return self._is_esc_cloneable_type(ty.inner)
        if isinstance(ty, TVec):
            return self._is_esc_cloneable_type(ty.inner)
        if isinstance(ty, TStruct):
            return all(self._is_esc_cloneable_type(ft) for ft in ty.fields.values())
        if isinstance(ty, TUnion):
            return all(vt is None or self._is_esc_cloneable_type(vt) for vt in ty.variants.values())
        if isinstance(ty, TUnitful):
            return self._is_esc_cloneable_type(ty.inner)
        if isinstance(ty, TUncertain):
            return self._is_esc_cloneable_type(ty.inner)
        return False

    def _push_with_context(self, resource: Expr, alloc_ty: Type,
                           cleanup: Optional[str]):
        prev_alloc = getattr(self.env, '_active_allocator', None)
        region = None
        if self._is_allocator_operand_type(alloc_ty):
            if cleanup:
                region = self._fresh_region()
                self._region_parent[region] = self._current_region()
            entry = (resource, alloc_ty, region)
            self.env._allocator_stack.append(entry)
            self.env._active_allocator = entry
        return prev_alloc, region

    def _pop_with_context(self, prev_alloc):
        stack = getattr(self.env, '_allocator_stack', None) or []
        if stack:
            stack.pop()
        self.env._active_allocator = prev_alloc

    def _push_handle_error_set_scope(self):
        self._handle_error_sets.append(set())

    def _pop_handle_error_set_scope(self) -> set[Type]:
        return self._handle_error_sets.pop() if self._handle_error_sets else set()

    def _note_handle_error_set(self, err_ty: Optional[Type]):
        if self._handle_error_sets and err_ty is not None:
            self._handle_error_sets[-1].add(err_ty)

    def _handle_binding_type(self, error_sets: set[Type]) -> Type:
        concrete = [ty for ty in error_sets if isinstance(ty, (TErrorSet, TErrorSetUnion))]
        if not concrete:
            return T_I64
        merged = merge_error_sets(*concrete)
        return merged if merged is not None else T_I64

    def _region_outlives(self, source: Optional[int], target_context: Optional[int]) -> bool:
        if source is None:
            return True
        cur = target_context
        while cur is not None:
            if cur == source:
                return True
            cur = self._region_parent.get(cur)
        return False

    def _expr_region(self, expr: Optional[Expr]) -> Optional[int]:
        if expr is None:
            return None
        region = getattr(expr, '_lifetime_region', None)
        if region is not None:
            return region
        if isinstance(expr, Ident):
            sym = self.env.lookup(expr.name)
            return sym.lifetime_region if sym else None
        if isinstance(expr, FieldExpr):
            return self._expr_region(expr.obj)
        if isinstance(expr, IndexExpr):
            return self._expr_region(expr.obj)
        if isinstance(expr, UnaryExpr):
            return self._expr_region(expr.operand)
        if isinstance(expr, EscExpr):
            return getattr(expr, '_lifetime_region', None)
        if isinstance(expr, WithAllocExpr):
            return getattr(expr, '_lifetime_region', None)
        if isinstance(expr, BlockExpr):
            return self._expr_region(expr.block.tail)
        if isinstance(expr, IfExpr):
            regions = {r for r in (
                self._expr_region(expr.then_block.tail),
                self._expr_region(expr.else_block.tail if expr.else_block else None),
            ) if r is not None}
            return regions.pop() if len(regions) == 1 else None
        if isinstance(expr, MatchExpr):
            regions = {
                self._expr_region(arm.body.tail)
                for arm in expr.arms
                if arm.body.tail is not None
            }
            regions.discard(None)
            return regions.pop() if len(regions) == 1 else None
        if isinstance(expr, CallExpr) and isinstance(expr.callee, Ident) and expr.callee.name == "__try":
            return self._expr_region(expr.args[0].value if expr.args else None)
        return None

    def _mark_expr_region(self, expr: Optional[Expr], region: Optional[int]):
        if expr is not None and region is not None:
            expr._lifetime_region = region

    def _node_span(self, node: object) -> Optional[SourceSpan]:
        if node is None:
            return None
        if isinstance(node, EscExpr):
            span = getattr(node, "span", None)
            if span is not None:
                line = span.start.line
                col = span.start.col
            else:
                line = getattr(node, "line", 0) or 0
                col = getattr(node, "col", 0) or 0
            if line > 0 and col > 0:
                return SourceSpan(
                    start=SourcePos(line=line, col=col),
                    end=SourcePos(line=line, col=col + 3),
                )
        if isinstance(node, CallExpr) and isinstance(node.callee, Ident) and node.callee.name == "__try":
            span = getattr(node, "span", None)
            if span is not None:
                line = span.start.line
                col = span.start.col
            else:
                line = getattr(node, "line", 0) or 0
                col = getattr(node, "col", 0) or 0
            if line > 0 and col > 0:
                return SourceSpan(
                    start=SourcePos(line=line, col=col),
                    end=SourcePos(line=line, col=col + 3),
                )
        span = getattr(node, "span", None)
        if span is not None:
            return span
        if isinstance(node, BlockExpr):
            return node.block.span
        line = getattr(node, "line", 0) or 0
        col = getattr(node, "col", 0) or 0
        if line > 0 and col > 0:
            end_col = col + 1
            if isinstance(node, Ident):
                end_col = col + max(len(node.name), 1)
            elif isinstance(node, VariantLit):
                end_col = col + max(len(node.name), 1)
            return SourceSpan(
                start=SourcePos(line=line, col=col),
                end=SourcePos(line=line, col=end_col),
            )
        return None

    def _error_at(self, node: object, msg: str,
                  hint: Optional[str] = None,
                  origin: Optional[str] = None,
                  code: Optional[str] = None) -> None:
        span = self._node_span(node)
        if span is not None:
            self.diags.error(
                msg,
                line=span.start.line,
                col=span.start.col,
                hint=hint,
                origin=origin,
                span=span,
                code=code,
            )
            return
        self.diags.error(
            msg,
            line=getattr(node, "line", 0) or 0,
            col=getattr(node, "col", 0) or 0,
            hint=hint,
            origin=origin,
            code=code,
        )

    def _ensure_region_can_escape(self, expr: Optional[Expr], target_context: Optional[int],
                                  what: str, hint: str):
        region = self._expr_region(expr)
        if region is not None and not self._region_outlives(region, target_context):
            self._error_at(
                expr,
                f"{what} would escape a cleanup-bearing with block",
                hint=hint,
                code="region-escape",
            )

    def _assign_target_region(self, target: Expr) -> Optional[int]:
        if isinstance(target, Ident):
            sym = self.env.lookup(target.name)
            return sym.decl_region if sym else None
        if isinstance(target, (FieldExpr, IndexExpr)):
            obj = target.obj
            if isinstance(obj, Ident):
                sym = self.env.lookup(obj.name)
                return sym.decl_region if sym else None
            return self._assign_target_region(obj)
        return None

    def run(self, program: Program):
        for decl in program.decls:
            self._check_decl(decl)

    def _check_decl(self, decl: Decl):
        prev_pkg = getattr(self.env, "_current_pkg", None)
        prev_source = self._current_source_file
        prev_allow_internal = self._allow_internal_stdlib_intrinsics
        self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
        self._current_source_file = getattr(decl, "_source_file", None)
        self._allow_internal_stdlib_intrinsics = is_std_source_path(self._current_source_file)
        if isinstance(decl, FunDecl):
            self._check_fun(decl, receiver=None)
        elif isinstance(decl, StructDecl):
            self.env.set_current_struct(decl.name)
            for m in decl.methods:
                self._check_fun(m, receiver=decl.name)
            self.env.set_current_struct(None)
        elif isinstance(decl, DefDecl):
            self.env.set_current_struct(decl.for_type)
            self._check_def(decl)
            self.env.set_current_struct(None)
        elif isinstance(decl, TestDecl):
            prev_test = self._in_test_block
            self._in_test_block = True
            self.env.push_scope()
            self._check_block(decl.body)
            self.env.pop_scope()
            self._in_test_block = prev_test
        elif isinstance(decl, LetStmt):
            self._check_let(decl)
        self._current_source_file = prev_source
        self._allow_internal_stdlib_intrinsics = prev_allow_internal
        self.env.set_current_pkg(prev_pkg)

    def _check_fun(self, f: FunDecl, receiver: Optional[str]):
        if f.body is None: return   # interface signature

        self.env._current_fn_has_handle = bool(getattr(f, 'handle_block', None))
        self.env.push_scope()

        # Register generic type params (fun foo[T, U](...))
        type_params = getattr(f, '_type_params', [])
        for param in type_params:
            self.env.register_type(param, TVar(name=param))

        ret_ty = effective_return_type(f, self.env)
        self.env.set_return_type(ret_ty)

        # Bind parameters
        for p in f.params:
            pt = lower_type(p.type_, self.env)
            self.env.define(Symbol(p.name, pt, False))

        # Check body
        if getattr(f, 'handle_block', None):
            self._push_handle_error_set_scope()
        block_ty = self._check_block(f.body, expected=ret_ty)
        fn_handle_sets = self._pop_handle_error_set_scope() if getattr(f, 'handle_block', None) else set()

        # Check return type — only if block has a non-void tail expression
        # (explicit return statements are checked in _check_stmt)
        if (block_ty is not None and
                not isinstance(ret_ty, TVoid) and
                not isinstance(block_ty, TVoid)):
            result = unify(block_ty, ret_ty)
            if result.is_error() and not block_ty.is_error() and not ret_ty.is_error():
                self.diags.error(
                    f"function '{f.name}' should return {ret_ty} "
                    f"but tail expression has type {block_ty}",
                    hint=f"add a return statement or change return type to {block_ty}"
                )

        # Check handle block body if present
        h = getattr(f, 'handle_block', None)
        if h:
            self.env.push_scope()
            binding_ty = self._handle_binding_type(fn_handle_sets)
            h._binding_type = binding_ty
            self.env.define(Symbol(
                h.binding,
                binding_ty,
                False,
            ))
            self._check_block(h.body)
            self.env.pop_scope()
        self.env._current_fn_has_handle = False
        self.env.set_return_type(None)
        self.env.pop_scope()

    def _check_def(self, d: DefDecl):
        """Verify def block satisfies all interface contracts."""
        self.env.set_current_struct(d.for_type)
        for iface_name in d.interfaces:
            iface = self.env.lookup_interface(iface_name)
            if iface is None:
                self.diags.error(f"unknown interface '{iface_name}'")
                continue
            # Check all required methods are present.
            # Methods with a body in the interface are default implementations
            # and don't need to be re-implemented by the def block.
            for method_name, required_ty in iface.methods.items():
                provided_decl = next(
                    (m for m in d.methods if m.name == method_name), None)
                if provided_decl is None:
                    has_default = method_name in iface.defaults
                    if not has_default:
                        self.diags.error(
                            f"def {iface_name} for {d.for_type}: "
                            f"missing method '{method_name}'",
                            hint=f"implement: fun {method_name}(...)"
                        )

        # Check method bodies
        for m in d.methods:
            self._check_fun(m, receiver=d.for_type)
        self.env.set_current_struct(None)

    def _check_let(self, l: LetStmt) -> Type:
        expected = lower_type(l.type_, self.env) if l.type_ else None
        pkg_path = getattr(l, "_pkg_path", None)
        c_name = _c_function_name(pkg_path, l.name) if pkg_path else None

        if l.init is not None:
            if expected:
                init_ty = self._check_expr(l.init, expected)
            else:
                init_ty = self._synth_expr(l.init)
            ty = expected or init_ty
        elif expected:
            ty = expected
        else:
            self.diags.error(
                f"cannot infer type of '{l.name}' — add a type annotation "
                f"or an initializer",
                hint=f"let {l.name}: <type> = ..."
            )
            ty = T_ERR

        init_region = self._expr_region(l.init)
        decl_region = self._current_region()
        self._ensure_region_can_escape(
            l.init,
            decl_region,
            f"initializer for '{l.name}'",
            "keep the value inside the same cleanup-bearing with block or copy it to a longer-lived allocator",
        )
        self.env.define(Symbol(
            l.name, ty, l.mutable,
            line=l.line,
            col=l.col,
            lifetime_region=init_region if self._region_outlives(init_region, decl_region) else None,
            decl_region=decl_region,
            pkg_path=pkg_path,
            c_name=c_name,
        ))
        return ty

    # ── Block checking ────────────────────────────────────────

    def _check_block(self, block: Block,
                     expected: Optional[Type] = None) -> Optional[Type]:
        self.env.push_scope()
        for stmt in block.stmts:
            self._check_stmt(stmt)
        tail_ty = None
        if block.tail is not None:
            # First synthesise to detect void control-flow tails
            # (blocks where all paths use explicit returns)
            synth_ty = self._synth_expr(block.tail)
            tail_region = self._expr_region(block.tail)
            if tail_region is not None:
                block.tail._lifetime_region = tail_region
            is_void_control_flow = (
                isinstance(synth_ty, TVoid) and
                isinstance(block.tail, (IfExpr, IfUnwrap, MatchExpr,
                                        WhileUnwrap, BlockExpr))
            )
            if is_void_control_flow:
                # All paths in the control flow use explicit returns
                # No tail value — explicit returns handle type checking
                tail_ty = None
            elif expected:
                tail_ty = self._check_expr(block.tail, expected)
            else:
                tail_ty = synth_ty
        self.env.pop_scope()
        return tail_ty

    # ── Statement checking ────────────────────────────────────

    def _check_stmt(self, stmt: Stmt):
        if isinstance(stmt, LetStmt):
            self._check_let(stmt)

        elif isinstance(stmt, ReturnStmt):
            ret_ty = self.env.get_return_type()
            if stmt.value is not None:
                if not isinstance(stmt.value, WithExpr):
                    self._ensure_region_can_escape(
                        stmt.value,
                        None,
                        "return value",
                        "return a scalar/copied value instead of an allocator-backed value from a cleanup-bearing with block",
                    )
                if ret_ty:
                    self._check_expr(stmt.value, ret_ty)
                else:
                    self._synth_expr(stmt.value)
            else:
                if (ret_ty and
                        not isinstance(ret_ty, TVoid) and
                        not (isinstance(ret_ty, TErrorUnion) and isinstance(ret_ty.payload, TVoid))):
                    self.diags.error(
                        f"empty return in function that returns {ret_ty}",
                        hint="return a value or change return type to void"
                    )

        elif isinstance(stmt, AssignStmt):
            self._check_assign(stmt)

        elif isinstance(stmt, ExprStmt):
            if isinstance(stmt.expr, EscExpr):
                ret_ty = self.env.get_return_type()
                if isinstance(ret_ty, TErrorUnion):
                    self._check_expr(stmt.expr, ret_ty)
                else:
                    self._error_at(
                        stmt.expr,
                        "'esc' can only be used in a function that returns !T",
                        hint="change the function return type to !T, or use 'try esc ...' inside a handle-capable expression",
                        code="esc-context",
                    )
            else:
                self._synth_expr(stmt.expr)

        elif isinstance(stmt, ForRangeStmt):
            self._check_for_range(stmt)

        elif isinstance(stmt, ForIterStmt):
            self._check_for_iter(stmt)

        elif isinstance(stmt, WhileStmt):
            self._check_while(stmt)

        elif isinstance(stmt, DeferStmt):
            self._check_block(stmt.body)

        elif isinstance(stmt, BreakStmt):
            loop = self.env.find_loop(stmt.label)
            if loop is None:
                self.diags.error(
                    "break outside loop" if not stmt.label
                    else f"no loop with label '{stmt.label}'"
                )
            elif stmt.value is not None and loop.loop_type:
                self._check_expr(stmt.value, loop.loop_type)

        elif isinstance(stmt, ContinueStmt):
            loop = self.env.find_loop(stmt.label)
            if loop is None:
                self.diags.error(
                    "continue outside loop" if not stmt.label
                    else f"no loop with label '{stmt.label}'"
                )

    def _check_assign(self, a: AssignStmt):
        self._ensure_region_can_escape(
            a.value,
            self._assign_target_region(a.target),
            "assignment",
            "store the value only in bindings that do not outlive the cleanup-bearing with block",
        )
        # Check LHS is mutable
        if isinstance(a.target, Ident):
            sym = self.env.lookup(a.target.name)
            if sym and not sym.mutable:
                self.diags.error(
                    f"cannot assign to immutable binding '{a.target.name}'",
                    hint=f"change to 'let var {a.target.name} = ...'"
                )
        lhs_ty = self._synth_expr(a.target)
        self._check_expr(a.value, lhs_ty)
        if isinstance(a.target, Ident):
            sym = self.env.lookup(a.target.name)
            if sym:
                src_region = self._expr_region(a.value)
                sym.lifetime_region = (
                    src_region if self._region_outlives(src_region, sym.decl_region) else None
                )

    def _check_for_range(self, f: ForRangeStmt):
        start_ty = self._synth_expr(f.start)
        end_ty   = self._synth_expr(f.end)
        # range variable is an integer
        unified = unify(start_ty, end_ty)
        if not isinstance(default_numeric(unified), TInt):
            self.diags.error(
                f"range bounds must be integers, got {start_ty} and {end_ty}"
            )
        var_ty = default_numeric(unified) if not unified.is_error() else T_I64

        self.env.push_loop(label=f.label)
        self.env.define(Symbol(f.var, var_ty, False))
        if f.filter:
            self._check_expr(f.filter, T_BOOL)
        self._check_block(f.body)
        self.env.pop_loop()

    def _check_for_iter(self, f: ForIterStmt):
        iter_ty = self._synth_expr(f.iter)

        # Determine element type from iterator
        elem_ty = self._elem_type_of(iter_ty)
        if elem_ty is None:
            self.diags.error(
                f"type {iter_ty} is not iterable",
                hint="implement the Iterable interface"
            )
            elem_ty = T_ERR

        if f.filter:
            self.env.push_scope()
            self._bind_for_pattern(f.pattern, elem_ty)
            self._check_expr(f.filter, T_BOOL)
            self.env.pop_scope()

        self.env.push_loop(label=f.label)
        self._bind_for_pattern(f.pattern, elem_ty)
        self._check_block(f.body)
        self.env.pop_loop()

    def _bind_for_pattern(self, pat: ForPattern, elem_ty: Type):
        if isinstance(pat, PatIdent):
            self.env.define(Symbol(pat.name, elem_ty, False))
        elif isinstance(pat, PatRef):
            self.env.define(Symbol(pat.name, TPointer(elem_ty), True))
        elif isinstance(pat, PatTuple):
            if isinstance(elem_ty, TTuple) and len(pat.names) == len(elem_ty.fields):
                for name, (_, ty) in zip(pat.names, elem_ty.fields):
                    self.env.define(Symbol(name, ty, False))
            else:
                for name in pat.names:
                    self.env.define(Symbol(name, T_ERR, False))

    def _check_while(self, w: WhileStmt):
        self._check_expr(w.cond, T_BOOL)
        self.env.push_loop(label=w.label)
        self._check_block(w.body)
        self.env.pop_loop()

    def _check_with_expr(self, expr: WithExpr,
                         expected: Optional[Type]) -> Type:
        alloc_ty = self._synth_expr(expr.resource)
        prev_alloc, region = self._push_with_context(expr.resource, alloc_ty, expr.cleanup)
        saved_with_depth = getattr(self.env, '_with_handle_depth', 0)
        if expr.handle:
            self.env._with_handle_depth = saved_with_depth + 1
            self._push_handle_error_set_scope()

        body_ty = self._check_block(expr.body, expected=expected)

        handle_ty = None
        if expr.handle:
            handle_error_sets = self._pop_handle_error_set_scope()
            self.env.push_scope()
            binding_ty = self._handle_binding_type(handle_error_sets)
            expr.handle._binding_type = binding_ty
            self.env.define(Symbol(
                expr.handle.binding,
                binding_ty,
                False,
            ))
            handle_expected = expected or body_ty
            handle_ty = self._check_block(expr.handle.body, expected=handle_expected)
            self.env.pop_scope()

        self.env._with_handle_depth = saved_with_depth
        self._pop_with_context(prev_alloc)

        result_ty = None
        if expected is not None:
            result_ty = expected
        elif isinstance(body_ty, TErrorUnion) and body_ty.error_set == self._esc_error_set():
            if expr.handle:
                self._error_at(
                    expr,
                    "'esc' with a local with-handle is not supported yet",
                    hint="move the escape to a with block without a local handle, or handle inner errors before escaping",
                    code="esc-local-handle-unsupported",
                )
                result_ty = T_ERR
            else:
                result_ty = body_ty
        elif expr.handle:
            left = body_ty or T_VOID
            right = handle_ty or T_VOID
            merged = unify(left, right)
            if merged.is_error() and not left.is_error() and not right.is_error():
                self.diags.error(
                    f"with expression branches have incompatible types: {left} and {right}",
                    hint="make the success path and handle block produce the same type",
                )
                result_ty = T_ERR
            else:
                result_ty = merged
        else:
            result_ty = body_ty or T_VOID

        if region is not None:
            tail_exprs = [expr.body.tail]
            if expr.handle:
                tail_exprs.append(expr.handle.body.tail)
            for tail in tail_exprs:
                self._ensure_region_can_escape(
                    tail,
                    None,
                    "with expression result",
                    "return a non-escaping value from the with block, or copy allocator-backed data to a longer-lived allocator first",
                )

        if result_ty is None:
            result_ty = T_VOID
        expr._resolved_type = result_ty
        if region is not None:
            tail_regions = {self._expr_region(t) for t in [expr.body.tail, expr.handle.body.tail if expr.handle else None]}
            tail_regions.discard(None)
            if len(tail_regions) == 1:
                expr._lifetime_region = tail_regions.pop()
        return result_ty

    def _elem_type_of(self, ty: Type) -> Optional[Type]:
        """Get the element type of an iterable."""
        if isinstance(ty, TVec):   return ty.inner
        if isinstance(ty, TSlice): return ty.inner
        if isinstance(ty, TArray): return ty.inner
        # Check for Iterable interface implementation
        name = self.env._type_name(ty)
        if name:
            m = self.env.impls.find_method(name, "next")
            if m and isinstance(m.ret, TOptional):
                return m.ret.inner
        return None

    # ══════════════════════════════════════════════════════════
    # Bidirectional expression checking
    # ══════════════════════════════════════════════════════════

    def _check_expr(self, expr: Expr, expected: Type) -> Type:
        """
        Top-down (checking) mode.
        Verify expr is compatible with expected, propagating expected inward.
        Returns the actual type of the expression.
        """
        if expected.is_error(): return T_ERR

        def expected_variant_owner(ty: Type):
            from src.types import TOptional as _TOpt, TUnion as _TUnion, TErrorSet as _TErr, TErrorSetUnion as _TErrU
            if isinstance(ty, _TOpt):
                inner = ty.inner
                if isinstance(inner, (_TUnion, _TErr, _TErrU)):
                    return inner, True
            if isinstance(ty, (_TUnion, _TErr, _TErrU)):
                return ty, False
            return None, False

        # Bare variant literal — expected union type disambiguates it.
        if isinstance(expr, VariantLit):
            owner_ty, wrapped = expected_variant_owner(expected)
            if owner_ty is not None:
                variant_names = owner_ty.variants if isinstance(owner_ty, TUnion) else error_set_variants(owner_ty)
                if expr.name in variant_names:
                    payload_ty = variant_names[expr.name]
                    if payload_ty is not None:
                        kind = "error variant" if isinstance(owner_ty, (TErrorSet, TErrorSetUnion)) else "variant"
                        owner_name = format_type_for_user(owner_ty) if isinstance(owner_ty, (TErrorSet, TErrorSetUnion)) else owner_ty.name
                        self._error_at(
                            expr,
                            f"{kind} '{owner_name}.{expr.name}' requires a payload",
                            hint=f"use '.{expr.name}(...)'",
                        )
                        return T_ERR
                    expr._resolved_type = owner_ty
                    if wrapped:
                        expr._pre_coerce_type = owner_ty
                    return expected if wrapped else owner_ty

        # Bare payload variant constructor — expected union type disambiguates it.
        if isinstance(expr, CallExpr) and isinstance(expr.callee, VariantLit):
            owner_ty, wrapped = expected_variant_owner(expected)
            if owner_ty is not None:
                variant_names = owner_ty.variants if isinstance(owner_ty, TUnion) else error_set_variants(owner_ty)
                if expr.callee.name in variant_names:
                    payload_ty = variant_names[expr.callee.name]
                    kind = "error variant" if isinstance(owner_ty, (TErrorSet, TErrorSetUnion)) else "variant"
                    owner_name = format_type_for_user(owner_ty) if isinstance(owner_ty, (TErrorSet, TErrorSetUnion)) else owner_ty.name
                    if payload_ty is None:
                        self._error_at(
                            expr.callee,
                            f"{kind} '{owner_name}.{expr.callee.name}' does not take a payload"
                        )
                        return T_ERR
                    if isinstance(payload_ty, TTuple):
                        if len(expr.args) != len(payload_ty.fields):
                            self._error_at(
                                expr,
                                f"{kind} '{owner_name}.{expr.callee.name}' expects {len(payload_ty.fields)} arguments, got {len(expr.args)}"
                            )
                            return T_ERR
                        for arg, (_, field_ty) in zip(expr.args, payload_ty.fields):
                            self._check_expr(arg.value, field_ty)
                    else:
                        if len(expr.args) != 1:
                            self._error_at(
                                expr,
                                f"{kind} '{owner_name}.{expr.callee.name}' expects 1 argument, got {len(expr.args)}"
                            )
                            return T_ERR
                        self._check_expr(expr.args[0].value, payload_ty)
                    expr.callee._resolved_type = owner_ty
                    expr._resolved_type = owner_ty
                    if wrapped:
                        expr._pre_coerce_type = owner_ty
                    return expected if wrapped else owner_ty

        # Integer / float literals — resolve to expected type
        if isinstance(expr, IntLit):
            if isinstance(expected, (TInt, TFloat, TIntLit)):
                expr._resolved_type = expected
                return expected
            if isinstance(expected, TIntLit):
                return T_I64
            got = self._synth_expr(expr)
            return self._coerce_or_error(got, expected, expr)

        if isinstance(expr, FloatLit):
            if isinstance(expected, TFloat):
                expr._resolved_type = expected
                return expected
            got = self._synth_expr(expr)
            return self._coerce_or_error(got, expected, expr)

        # Tuple literal — expected type flows into fields
        if isinstance(expr, TupleLit) and isinstance(expected, TTuple):
            if len(expr.fields) != len(expected.fields):
                self.diags.error(
                    f"tuple has {len(expr.fields)} fields, "
                    f"expected {len(expected.fields)}"
                )
                return T_ERR
            result_fields = []
            for (fname, fexpr), (ename, ety) in zip(expr.fields, expected.fields):
                ft = self._check_expr(fexpr, ety)
                result_fields.append((fname or ename, ft))
            result = TTuple(result_fields)
            expr._resolved_type = result
            return result

        if isinstance(expr, TupleLit) and isinstance(expected, TStruct):
            if len(expr.fields) != len(expected.fields):
                self.diags.error(
                    f"struct literal has {len(expr.fields)} fields, expected {len(expected.fields)}",
                    hint=f"use all fields of '{expected.name}' exactly once",
                )
                return T_ERR
            seen = set()
            for fname, fexpr in expr.fields:
                if fname is None:
                    self.diags.error(
                        f"struct literal for '{expected.name}' requires named fields"
                    )
                    return T_ERR
                if fname in seen:
                    self.diags.error(f"duplicate field '{fname}' in '{expected.name}' literal")
                    return T_ERR
                field_ty = expected.fields.get(fname)
                if field_ty is None:
                    self.diags.error(f"'{expected.name}' has no field '{fname}'")
                    return T_ERR
                seen.add(fname)
                self._check_expr(fexpr, field_ty)
            missing = set(expected.fields) - seen
            if missing:
                self.diags.error(
                    f"missing fields in '{expected.name}' literal: {', '.join(sorted(missing))}"
                )
                return T_ERR
            expr._resolved_type = expected
            return expected

        # Vec literal — expected element type flows in
        if isinstance(expr, VecLit) and isinstance(expected, TVec):
            for elem in expr.elems:
                self._check_expr(elem, expected.inner)
            expr._resolved_type = expected
            self._mark_expr_region(expr, self._current_region())
            return expected

        # If expression — both branches checked against expected
        if isinstance(expr, IfExpr):
            return self._check_if(expr, expected)

        if isinstance(expr, MatchExpr):
            return self._check_match(expr, expected)

        # Closure — param types flow in from expected
        if isinstance(expr, Closure) and isinstance(expected, TFun):
            return self._check_closure(expr, expected)

        # Block — tail expression checked against expected
        if isinstance(expr, BlockExpr):
            ty = self._check_block(expr.block, expected=expected)
            expr._resolved_type = ty or T_VOID
            return expr._resolved_type

        if isinstance(expr, WithAllocExpr):
            alloc_ty = self._synth_expr(expr.allocator)
            if not self._is_allocator_type(alloc_ty):
                self._error_at(expr.allocator, f"expected allocator, got {format_type_for_user(alloc_ty)}")
                return T_ERR
            target_region = self._allocator_target_region(expr.allocator, alloc_ty)
            if isinstance(expected, TPointer):
                self._check_expr(expr.expr, expected.inner)
                expr._resolved_type = expected
                self._mark_expr_region(expr, target_region)
                return expected
            inner_expected = expected
            inner_ty = self._check_expr(expr.expr, inner_expected) if inner_expected is not None else self._synth_expr(expr.expr)
            if not self._is_esc_cloneable_type(inner_ty):
                self._error_at(
                    expr,
                    f"'with' allocation target does not yet support values of type {inner_ty}",
                    hint="target a pointer allocation, or use a scalar/string/vec/struct/union made of cloneable fields",
                )
                return T_ERR
            expr._resolved_type = inner_ty
            self._mark_expr_region(expr, target_region)
            return inner_ty

        if isinstance(expr, WithExpr):
            return self._check_with_expr(expr, expected)

        if isinstance(expr, EscExpr):
            esc_set = self._esc_error_set()
            if isinstance(expected, TErrorUnion) and (expected.error_set == esc_set or expected.error_set is None):
                inner_ty = self._check_expr(expr.expr, expected.payload)
                if not self._is_esc_cloneable_type(inner_ty):
                    self._error_at(
                        expr,
                        f"'esc' does not yet support values of type {inner_ty}",
                        hint="escape a scalar/string/vec/struct/union made of cloneable fields, or return it some other way",
                        code="esc-unsupported-type",
                    )
                    return T_ERR
                expr._resolved_type = self._esc_result_type(expected.payload)
                expr._lifetime_region = self._outer_region()
                return expr._resolved_type
            self._error_at(
                expr,
                "'esc' can only be used where !T can propagate",
                hint="change the enclosing function or block to produce !T, or return a non-escaping value instead",
                code="esc-context",
            )
            return T_ERR

        # None literal — ?T expected
        if isinstance(expr, NoneLit):
            if isinstance(expected, TOptional):
                expr._resolved_type = expected   # annotate for codegen
                return expected
            self.diags.error(
                f"'none' used where {expected} is expected",
                hint="change the type to an optional"
            )
            return T_ERR

        # Default — synthesise and check assignability
        got = self._synth_expr(expr)
        return self._coerce_or_error(got, expected, expr)

    def _coerce_or_error(self, got: Type, expected: Type,
                          expr: Expr) -> Type:
        """Check got is assignable to expected, emit error if not."""
        if got.is_error() or expected.is_error():
            return T_ERR
        if is_assignable(got, expected):
            if isinstance(expected, TOptional) and not isinstance(got, TOptional):
                expr._pre_coerce_type = got
                expr._resolved_type = expected
            return expected
        # Coercion: value or error → TErrorUnion
        from src.types import TErrorUnion, TErrorSet, TErrorSetUnion
        if isinstance(expected, TErrorUnion):
            if isinstance(got, (TErrorSet, TErrorSetUnion)):
                if expected.error_set is None or error_set_contains(expected.error_set, got):
                    return expected
                expected_names = error_set_member_names(expected.error_set)
                got_names = error_set_member_names(got)
                unexpected = [name for name in got_names if name not in expected_names]
                missing = [name for name in expected_names if name not in got_names]
                hint_parts = []
                if unexpected:
                    hint_parts.append(f"unexpected here: {', '.join(unexpected)}")
                if missing:
                    hint_parts.append(f"allowed by the return type: {', '.join(expected_names)}")
                hint_parts.append("add the missing error set to the return type, or handle it before returning")
                self.diags.error(
                    f"wrong error set: expected {expected.error_set}, got {got}",
                    line=getattr(expr, 'line', 0),
                    span=self._node_span(expr),
                    code="wrong-error-set",
                    hint="; ".join(hint_parts),
                )
                return T_ERR
            # Returning ok value — check against payload type
            return self._coerce_or_error(got, expected.payload, expr)

        # Coercion: ConcreteType → any Interface  or  *any Interface
        from src.types import TDynInterface, TAnyInterface
        if isinstance(expected, (TDynInterface, TAnyInterface)):
            got_name = None
            if isinstance(got, TStruct): got_name = got.name
            elif hasattr(got, 'name'):   got_name = got.name
            if got_name and self.env.impls.implements(got_name, expected.iface.name):
                expr._pre_coerce_type = got
                expr._resolved_type   = expected
                return expected
            if got_name:
                self.diags.error(
                    f"type '{got_name}' does not implement '{expected.iface.name}'",
                    hint=f"add: def {expected.iface.name} for {got_name} {{ ... }}",
                    line=getattr(expr, 'line', 0), col=getattr(expr, 'col', 0),
                    span=self._node_span(expr),
                    code="missing-interface-impl",
                )
            return T_ERR
        # Allow tuple literal .{...} to construct a struct
        # if field names and count match
        if isinstance(got, TTuple) and isinstance(expected, TStruct):
            if len(got.fields) == len(expected.fields):
                expr._resolved_type = expected   # annotate for codegen
                return expected   # structural match — accept
        # Allow tuple literal .{...} → *Struct when inside a with block
        if isinstance(got, TTuple) and isinstance(expected, TPointer) and isinstance(expected.inner, TStruct):
            if len(got.fields) == len(expected.inner.fields):
                expr._resolved_type = expected.inner   # inner struct type for codegen
                self._mark_expr_region(expr, self._current_region())
                return expected   # will be allocated from active allocator
        result = unify(got, expected)
        if result.is_error():
            self._error_at(
                expr,
                f"type mismatch: expected {expected}, got {got}",
                hint=self._coerce_hint(got, expected),
            )
            return T_ERR
        return result

    def _coerce_hint(self, got: Type, expected: Type) -> Optional[str]:
        if isinstance(got, TIntLit) and isinstance(expected, TFloat):
            return f"add a decimal point: 42.0"
        if isinstance(got, TOptional) and not isinstance(expected, TOptional):
            return f"unwrap the optional with 'if expr |v|' or 'orelse'"
        if isinstance(expected, TOptional) and not isinstance(got, TOptional):
            return f"wrap in optional — this is valid, ?T accepts T"
        return None

    def _is_type_receiver_expr(self, expr: Expr) -> bool:
        if isinstance(expr, Ident):
            type_ty = self.env.lookup_type(expr.name)
            if type_ty is None:
                return False
            sym = self.env.lookup(expr.name)
            if sym is None:
                return True
            type_c_name = self.env.lookup_c_type_name(expr.name)
            return sym.type_ == type_ty and sym.c_name == type_c_name
        if isinstance(expr, FieldExpr):
            obj_ty = self._synth_expr(expr.obj)
            if isinstance(obj_ty, TNamespace):
                return self.env.lookup_namespace_type(obj_ty.name, expr.field) is not None
        return False

    # ── Synthesis — bottom-up type inference ─────────────────

    def _synth_expr(self, expr: Expr) -> Type:
        """
        Bottom-up (synthesis) mode.
        Figure out the type of expr without external context.
        """
        if isinstance(expr, IntLit):
            return getattr(expr, '_resolved_type', T_INTLIT)

        if isinstance(expr, FloatLit):
            return getattr(expr, '_resolved_type', T_FLOATLIT)

        if isinstance(expr, BoolLit):
            return T_BOOL

        if isinstance(expr, NoneLit):
            return TOptional(TVar())   # ?unknown

        if isinstance(expr, UnitLit):
            return self._synth_unit_lit(expr)

        if isinstance(expr, UncertainLit):
            return self._synth_uncertain_lit(expr)

        if isinstance(expr, StringLit):
            # Type-check any interpolated expressions — they can be any type
            for seg in expr.segments:
                if not isinstance(seg, str):
                    self._synth_expr(seg)
            return T_STR

        if isinstance(expr, SelfExpr):
            sym = self.env.lookup("self")
            if sym is None:
                self.diags.error("'self' used outside method")
                return T_ERR
            return sym.type_

        if isinstance(expr, VariantLit):
            # .Variant — look up parent union type
            variant_ty = self.env.find_variant_type(expr.name)
            if variant_ty is not None:
                expr._resolved_type = variant_ty
                return variant_ty
            self._error_at(expr, f"unknown variant '.{expr.name}'",
                           hint="variants must be declared in a union")
            return T_ERR

        if isinstance(expr, Ident):
            # Bare ident is never a variant now — .Variant syntax required
            sym = self.env.lookup(expr.name)
            if sym is not None:
                ty = sym.type_
                expr._bound_symbol = sym
            else:
                type_ns = self.env.lookup_type(expr.name)
                if type_ns is not None:
                    ty = type_ns
                else:
                    ty = self.env.lookup_or_error(
                        expr.name, line=expr.line, col=expr.col, span=getattr(expr, "span", None))
            expr._resolved_type = ty
            return ty

        if isinstance(expr, BinExpr):
            return self._synth_binary(expr)

        if isinstance(expr, UnaryExpr):
            return self._synth_unary(expr)

        if isinstance(expr, FieldExpr):
            if isinstance(expr.obj, Ident):
                type_ns = self.env.lookup_type(expr.obj.name)
                if isinstance(type_ns, TUnion):
                    payload = type_ns.variants.get(expr.field, None)
                    if expr.field in type_ns.variants:
                        if payload is not None:
                            self._error_at(
                                expr,
                                f"variant '{type_ns.name}.{expr.field}' requires a payload",
                                hint=f"use '.{expr.field}(...)' or '{type_ns.name}.{expr.field}(...)'"
                            )
                            return T_ERR
                        expr._resolved_type = type_ns
                        return type_ns
                if isinstance(type_ns, (TErrorSet, TErrorSetUnion)):
                    variants = error_set_variants(type_ns)
                    payload = variants.get(expr.field, None)
                    if expr.field in variants:
                        if payload is not None:
                            self._error_at(
                                expr,
                                f"error variant '{format_type_for_user(type_ns)}.{expr.field}' requires a payload",
                                hint=f"use '.{expr.field}(...)' in an error-returning context"
                            )
                            return T_ERR
                        expr._resolved_type = type_ns
                        return type_ns
            # .value and .units on unitful/uncertain types
            obj_ty = self._synth_expr(expr.obj)
            if isinstance(obj_ty, TNamespace):
                value_ty = self.env.lookup_namespace_value(obj_ty.name, expr.field)
                if value_ty is not None:
                    expr._resolved_type = value_ty
                    return value_ty
                type_ty = self.env.lookup_namespace_type(obj_ty.name, expr.field)
                if type_ty is not None:
                    expr._resolved_type = type_ty
                    return type_ty
                hidden = self.env.lookup_namespace_hidden(obj_ty.name, expr.field)
                if hidden is not None:
                    kind, _, _ = hidden
                    self._error_at(
                        expr,
                        f"namespace '{obj_ty.name}' member '{expr.field}' is private",
                        hint=f"add 'pub' to the declaration of '{expr.field}' in package '{obj_ty.name}' to import it",
                        code="private-member",
                    )
                    return T_ERR
                self._error_at(expr, f"namespace '{obj_ty.name}' has no member '{expr.field}'")
                return T_ERR
            from src.types import TUnitful, TUncertain, TDynInterface, TAnyInterface
            if isinstance(obj_ty, (TDynInterface, TAnyInterface)):
                method_name = expr.field
                if method_name in obj_ty.iface.methods:
                    result = obj_ty.iface.methods[method_name]
                    expr._resolved_type = result
                    return result
                self.diags.error(
                    f"interface '{obj_ty.iface.name}' has no method '{method_name}'"
                )
                return T_ERR
            if isinstance(obj_ty, TUnitful):
                if expr.field == "value":
                    expr._resolved_type = obj_ty.inner
                    return obj_ty.inner   # the numeric type
                if expr.field == "units":
                    expr._resolved_type = T_STR
                    return T_STR          # unit name as string
            if isinstance(obj_ty, TUncertain):
                if expr.field == "value":
                    if isinstance(obj_ty.inner, TUnitful):
                        expr._resolved_type = obj_ty.inner.inner
                        return obj_ty.inner.inner
                    expr._resolved_type = obj_ty.inner
                    return obj_ty.inner
                if expr.field == "units" and isinstance(obj_ty.inner, TUnitful):
                    expr._resolved_type = T_STR
                    return T_STR
                if expr.field == "uncertainty":
                    expr._resolved_type = obj_ty.inner
                    return obj_ty.inner
            return self._synth_field(expr)

        if isinstance(expr, IndexExpr):
            return self._synth_index(expr)

        if isinstance(expr, CallExpr):
            return self._synth_call(expr)

        if isinstance(expr, TupleLit):
            fields = []
            for name, e in expr.fields:
                ty = self._synth_expr(e)
                fields.append((name, ty))
            result = TTuple(fields)
            expr._resolved_type = result
            return result

        if isinstance(expr, ArrayLit):
            if not expr.elems: return TArray(TVar(), 0)
            first = self._synth_expr(expr.elems[0])
            for e in expr.elems[1:]:
                self._check_expr(e, first)
            return TArray(first, len(expr.elems))

        if isinstance(expr, VecLit):
            if not expr.elems:
                result = TVec(TVar(), None)
                expr._resolved_type = result
                self._mark_expr_region(expr, self._current_region())
                return result
            first = self._synth_expr(expr.elems[0])
            for e in expr.elems[1:]:
                self._check_expr(e, first)
            result = TVec(first, None)
            expr._resolved_type = result
            self._mark_expr_region(expr, self._current_region())
            return result

        if isinstance(expr, VecComp):
            iter_ty  = self._synth_expr(expr.iter)
            elem_ty  = self._elem_type_of(iter_ty) or T_ERR
            self.env.push_scope()
            self._bind_for_pattern(expr.pattern, elem_ty)
            if expr.filter:
                self._check_expr(expr.filter, T_BOOL)
            result_ty = self._synth_expr(expr.expr)
            self.env.pop_scope()
            result = TVec(result_ty, None)
            expr._resolved_type = result
            self._mark_expr_region(expr, self._current_region())
            return result


        if isinstance(expr, RangeExpr):
            start = self._synth_expr(expr.start)
            end   = self._synth_expr(expr.end)
            unified = unify(start, end)
            return TVec(default_numeric(unified), None)

        if isinstance(expr, IfExpr):
            return self._check_if(expr, None)

        if isinstance(expr, IfUnwrap):
            return self._check_if_unwrap(expr, None)

        if isinstance(expr, WhileUnwrap):
            return self._check_while_unwrap(expr)

        if isinstance(expr, MatchExpr):
            return self._check_match(expr, None)

        if isinstance(expr, BlockExpr):
            ty = self._check_block(expr.block)
            expr._resolved_type = ty or T_VOID
            return expr._resolved_type

        if isinstance(expr, WithAllocExpr):
            alloc_ty = self._synth_expr(expr.allocator)
            if not self._is_allocator_type(alloc_ty):
                self._error_at(expr.allocator, f"expected allocator, got {format_type_for_user(alloc_ty)}")
                return T_ERR
            inner_ty = self._synth_expr(expr.expr)
            if not self._is_esc_cloneable_type(inner_ty):
                self._error_at(
                    expr,
                    f"'with' allocation target does not yet support values of type {inner_ty}",
                    hint="target a pointer allocation, or use a scalar/string/vec/struct/union made of cloneable fields",
                )
                return T_ERR
            expr._resolved_type = inner_ty
            self._mark_expr_region(expr, self._allocator_target_region(expr.allocator, alloc_ty))
            return inner_ty

        if isinstance(expr, WithExpr):
            return self._check_with_expr(expr, None)

        if isinstance(expr, EscExpr):
            inner_ty = self._synth_expr(expr.expr)
            if not self._is_esc_cloneable_type(inner_ty):
                self._error_at(
                    expr,
                    f"'esc' does not yet support values of type {inner_ty}",
                    hint="escape a scalar/string/vec/struct/union made of cloneable fields, or return it some other way",
                    code="esc-unsupported-type",
                )
                return T_ERR
            expr._resolved_type = self._esc_result_type(inner_ty)
            expr._lifetime_region = self._outer_region()
            return expr._resolved_type

        if isinstance(expr, Closure):
            params = [lower_type(p.type_, self.env) for p in expr.params]
            ret    = lower_type(expr.ret, self.env)
            self.env.push_scope()
            self.env.set_return_type(ret)
            for p, pt in zip(expr.params, params):
                self.env.define(Symbol(p.name, pt, False))
            self._check_block(expr.body, expected=ret)
            self.env.set_return_type(None)
            self.env.pop_scope()
            return TFun(params, ret)

        if isinstance(expr, ComptimeExpr):
            return self._synth_expr(expr.expr)

        # Internal helper calls produced by the parser
        if isinstance(expr, CallExpr) and isinstance(expr.callee, Ident):
            name = expr.callee.name
            if name == "__orelse":
                opt_ty = self._synth_expr(expr.args[0].value)
                if isinstance(opt_ty, TOptional):
                    self._check_expr(expr.args[1].value, opt_ty.inner)
                    return opt_ty.inner
                return opt_ty
            if name == "__try":
                inner = self._synth_expr(expr.args[0].value)
                if not isinstance(inner, TErrorUnion):
                    self._error_at(expr, "'try' used on non-error-union expression", code="try-non-error-union")
                    return inner
                # Verify enclosing function can propagate errors
                ret = self.env.get_return_type()
                has_handle = getattr(self.env, '_current_fn_has_handle', False)
                local_with_handle = getattr(self.env, '_with_handle_depth', 0) > 0
                invalid_try = False
                if self._current_region() is not None and has_handle and not local_with_handle and not isinstance(ret, TErrorUnion):
                    self._error_at(
                        expr,
                        "'try' inside a cleanup-bearing with block needs a local with handle or an E!T return",
                        hint="attach 'handle |e| { ... }' to the with expression, or propagate the error with an E!T return type",
                        code="try-cleanup-needs-local-handle",
                    )
                    invalid_try = True
                if ret is not None and not isinstance(ret, TErrorUnion) and not has_handle and not local_with_handle:
                    self._error_at(
                        expr,
                        "'try' cannot be used here — enclosing function must return E!T or have a handle block",
                        hint="add a handle block on the enclosing function or with expression, or change return type to E!T",
                        code="try-context",
                    )
                    invalid_try = True
                if invalid_try:
                    return T_ERR
                self._note_handle_error_set(inner.error_set)
                return inner.payload
            if name in ("__catch", "__catch_bind"):
                inner = self._synth_expr(expr.args[0].value)
                if isinstance(inner, TErrorUnion):
                    # Type of catch is the ok payload type (arms must match)
                    return inner.payload
                return inner
            if name == "__optional_chain":
                inner = self._synth_expr(expr.args[0].value)
                if isinstance(inner, TOptional):
                    return inner
                return TOptional(inner)

        return T_ERR

    # ── Binary expressions ────────────────────────────────────

    # ── Unit arithmetic helpers ──────────────────────────────

    def _synth_unit_lit(self, expr) -> Type:
        """Synth type for UnitLit: 10.0`N`, `N` (bare), UncertainLit`N`."""
        from src.types import TUnitful, make_unitful, T_F64, T_I64, TIntLit, TFloatLit
        registry = getattr(self.env, '_unit_registry', None)
        try:
            unitful = make_unitful(T_F64, expr.unit, registry)
        except ValueError as e:
            self.diags.error(str(e), line=getattr(expr, 'line', 0),
                             col=getattr(expr, 'col', 0))
            return T_ERR

        if expr.value is None:
            # Bare unit literal `N` — value is 1.0
            expr._resolved_type = unitful
            return unitful

        # Annotated literal: 10.0`N`, (uncertain_val)`N`
        inner_ty = self._synth_expr(expr.value)
        from src.types import TUncertain
        if isinstance(inner_ty, TUncertain):
            # Propagate unitful wrapping into uncertain type
            unitful_inner = TUnitful(inner=inner_ty.inner, dims=unitful.dims,
                                      scale=unitful.scale, name=unitful.name)
            result = TUncertain(inner=unitful_inner)
        elif isinstance(inner_ty, (TIntLit, TFloatLit)):
            result = TUnitful(inner=T_F64, dims=unitful.dims,
                              scale=unitful.scale, name=unitful.name)
        else:
            result = TUnitful(inner=inner_ty, dims=unitful.dims,
                              scale=unitful.scale, name=unitful.name)
        expr._resolved_type = result
        return result

    def _synth_uncertain_lit(self, expr) -> Type:
        """Synth type for UncertainLit: 10.0 +- 0.5"""
        from src.types import TUncertain, T_F64, TIntLit, TFloatLit, default_numeric
        val_ty = self._synth_expr(expr.value)
        err_ty = self._synth_expr(expr.error)
        # Resolve literal types to concrete types
        val_ty = default_numeric(val_ty)
        inner = val_ty if not isinstance(val_ty, (TIntLit, TFloatLit)) else T_F64
        result = TUncertain(inner=inner)
        expr._resolved_type = result
        return result

    def _synth_unit_binary(self, b, lt, rt) -> Type:
        """Handle binary operations where at least one side is TUnitful."""
        from src.types import (TUnitful, make_unitful, dim_mul, dim_div, T_F64,
                               dim_is_dimensionless, DIM_ZERO, _best_unit_name)
        import math

        # Resolve dynamic units at compile time where possible
        l_unitful = lt if isinstance(lt, TUnitful) else None
        r_unitful = rt if isinstance(rt, TUnitful) else None

        op = b.op

        if op in ("+", "-"):
            # Same dimension required
            if l_unitful and r_unitful:
                if l_unitful.dims is None or r_unitful.dims is None:
                    # Dynamic — result is dynamic
                    return TUnitful(inner=T_F64, dims=None, scale=1.0, name="?")
                if l_unitful.dims != r_unitful.dims:
                    self._error_at(
                        b,
                        f"cannot {op} values with incompatible units: "
                        f"`{l_unitful.name}` and `{r_unitful.name}`",
                        hint="units must have the same physical dimension",
                    )
                    return T_ERR
                # Same dimension, possibly different scale → warn + convert
                if abs(l_unitful.scale - r_unitful.scale) > 1e-12:
                    # Warn about implicit conversion — only if diags supports it
                    msg = f"implicit unit conversion: `{r_unitful.name}` → `{l_unitful.name}`"
                    if hasattr(self.diags, 'warn'):
                        self.diags.warn(msg)
                    # TODO: emit compiler warning properly
                return lt  # result has left operand's unit
            # One side is unitless — error
            self._error_at(
                b,
                f"cannot {op} unitful and non-unitful values",
            )
            return T_ERR

        if op == "*":
            if l_unitful and r_unitful:
                if l_unitful.dims is None or r_unitful.dims is None:
                    return TUnitful(inner=T_F64, dims=None, scale=1.0, name="?")
                new_dims  = dim_mul(l_unitful.dims, r_unitful.dims)
                new_scale = l_unitful.scale * r_unitful.scale
                return make_unitful(T_F64, _best_unit_name(new_dims, new_scale))
            # Scalar * unitful or unitful * scalar — result keeps unit
            return l_unitful or r_unitful

        if op == "/":
            if l_unitful and r_unitful:
                if l_unitful.dims is None or r_unitful.dims is None:
                    return TUnitful(inner=T_F64, dims=None, scale=1.0, name="?")
                new_dims  = dim_div(l_unitful.dims, r_unitful.dims)
                new_scale = l_unitful.scale / r_unitful.scale
                if dim_is_dimensionless(new_dims) and abs(new_scale - 1.0) < 1e-12:
                    return T_F64  # dimensionless — plain float
                return make_unitful(T_F64, _best_unit_name(new_dims, new_scale))
            if l_unitful:
                return l_unitful  # unitful / scalar — keeps unit
            return T_F64

        # Comparisons on same-dimension units → bool
        if op in ("==", "!=", "<", ">", "<=", ">="):
            if l_unitful and r_unitful:
                if (l_unitful.dims is not None and r_unitful.dims is not None and
                        l_unitful.dims != r_unitful.dims):
                    self._error_at(
                        b,
                        f"cannot compare `{l_unitful.name}` and `{r_unitful.name}`",
                    )
                    return T_ERR
            return T_BOOL

        return T_ERR

    def _synth_binary(self, b: BinExpr) -> Type:
        if b.op in ("and", "or"):
            self._check_expr(b.left,  T_BOOL)
            self._check_expr(b.right, T_BOOL)
            return T_BOOL

        lt = self._synth_expr(b.left)
        rt = self._synth_expr(b.right)
        from src.types import TUnitful, TUncertain
        if isinstance(lt, TUncertain) or isinstance(rt, TUncertain):
            result = self._synth_uncertain_binary(b, lt, rt)
            b._resolved_type = result
            return result

        # Unit arithmetic — check before numeric widening
        if isinstance(lt, TUnitful) or isinstance(rt, TUnitful):
            result = self._synth_unit_binary(b, lt, rt)
            b._resolved_type = result
            return result

        lhs = self._synth_expr(b.left)
        rhs = self._synth_expr(b.right)

        if lhs.is_error() or rhs.is_error():
            return T_ERR

        # Resolve unresolved literals — default both to concrete types
        lhs = default_numeric(lhs)
        rhs = default_numeric(rhs)

        result = self.env.find_operator_impl(b.op, lhs, rhs)
        if result is None:
            self._error_at(
                b,
                f"operator '{b.op}' not defined for {lhs} and {rhs}",
                hint=self._operator_hint(b.op, lhs, rhs)
            )
            return T_ERR
        return result

    def _synth_uncertain_binary(self, b: BinExpr, lt: Type, rt: Type) -> Type:
        from src.types import TUncertain, TUnitful, T_BOOL

        left_inner = lt.inner if isinstance(lt, TUncertain) else lt
        right_inner = rt.inner if isinstance(rt, TUncertain) else rt

        if isinstance(left_inner, TUnitful) or isinstance(right_inner, TUnitful):
            inner_result = self._synth_unit_binary(b, left_inner, right_inner)
        else:
            left_num = default_numeric(left_inner)
            right_num = default_numeric(right_inner)
            inner_result = self.env.find_operator_impl(b.op, left_num, right_num)
            if inner_result is None:
                self._error_at(
                    b,
                    f"operator '{b.op}' not defined for {lt} and {rt}",
                    hint=self._operator_hint(b.op, left_num, right_num),
                )
                return T_ERR

        if inner_result.is_error():
            return T_ERR
        if inner_result == T_BOOL:
            return T_BOOL
        if b.op.lstrip(".") in ("+", "-", "*", "/"):
            return TUncertain(inner=inner_result)
        return inner_result

    def _sqrt_result_type(self, ty: Type, node) -> Type:
        from src.types import (
            TInt, TFloat, TIntLit, TFloatLit, TUnitful, TUncertain,
            T_F64, _best_unit_name,
        )

        if isinstance(ty, TUncertain):
            inner = self._sqrt_result_type(ty.inner, node)
            if inner.is_error():
                return inner
            return TUncertain(inner=inner)

        if isinstance(ty, TUnitful):
            if ty.dims is None:
                self._error_at(
                    node,
                    "sqrt() is not yet supported for dynamically-unitful values",
                )
                return T_ERR
            if any(exp % 2 != 0 for exp in ty.dims):
                unit_name = ty.name or dim_fmt(ty.dims)
                self._error_at(
                    node,
                    f"sqrt() requires units with even exponents, got `{unit_name}`",
                )
                return T_ERR
            new_dims = tuple(exp // 2 for exp in ty.dims)
            new_scale = math.sqrt(ty.scale)
            registry = getattr(self.env, "_unit_registry", None)
            unit_name = _best_unit_name(new_dims, new_scale, registry)
            return make_unitful(ty.inner, unit_name, registry)

        if isinstance(ty, TFloat):
            return ty
        if isinstance(ty, (TInt, TIntLit, TFloatLit)):
            return T_F64

        self._error_at(node, f"sqrt() not valid for {ty}")
        return T_ERR

    def _operator_hint(self, op: str, lhs: Type, rhs: Type) -> Optional[str]:
        iface_map = {"+": "Add", "-": "Sub", "*": "Mul", "/": "Div"}
        iface = iface_map.get(op)
        if iface and isinstance(lhs, TStruct):
            return f"implement 'def {iface}[{rhs}] for {lhs.name}'"
        return None

    # ── Unary expressions ─────────────────────────────────────

    def _synth_unary(self, u: UnaryExpr) -> Type:
        operand = self._synth_expr(u.operand)
        if operand.is_error(): return T_ERR

        if u.op == "-":
            if isinstance(operand, (TInt, TFloat, TIntLit, TFloatLit)):
                return operand
            self._error_at(u, f"unary '-' not valid for {operand}")
            return T_ERR

        if u.op == "!":
            if isinstance(operand, TBool): return T_BOOL
            self._error_at(u, f"'!' requires bool, got {operand}")
            return T_ERR

        if u.op == "*":   # deref
            if isinstance(operand, TPointer): return operand.inner
            self._error_at(u, f"cannot dereference non-pointer {operand}")
            return T_ERR

        if u.op in ("@", "&"):   # address-of
            return TPointer(operand)

        return T_ERR

    # ── Field access ──────────────────────────────────────────

    def _synth_field(self, f: FieldExpr) -> Type:
        if isinstance(f.obj, Ident):
            type_ns = self.env.lookup_type(f.obj.name)
            if isinstance(type_ns, TUnion):
                payload = type_ns.variants.get(f.field, None)
                if f.field in type_ns.variants:
                    if payload is not None:
                        self._error_at(
                            f,
                            f"variant '{type_ns.name}.{f.field}' requires a payload",
                            hint=f"use '.{f.field}(...)' or '{type_ns.name}.{f.field}(...)'"
                        )
                        return T_ERR
                    f._resolved_type = type_ns
                    return type_ns
            if isinstance(type_ns, (TErrorSet, TErrorSetUnion)):
                variants = error_set_variants(type_ns)
                payload = variants.get(f.field, None)
                if f.field in variants:
                    if payload is not None:
                        self._error_at(
                            f,
                            f"error variant '{format_type_for_user(type_ns)}.{f.field}' requires a payload",
                            hint=f"use '.{f.field}(...)' in an error-returning context"
                        )
                        return T_ERR
                    f._resolved_type = type_ns
                    return type_ns

        obj_ty = self._synth_expr(f.obj)

        # Dereference pointer
        if isinstance(obj_ty, TPointer):
            obj_ty = obj_ty.inner

        if obj_ty.is_error(): return T_ERR

        if isinstance(obj_ty, TStruct):
            # Check fields first
            ft = obj_ty.field_type(f.field)
            if ft: return ft
            # Then methods
            mt = obj_ty.method_type(f.field)
            if mt: return mt
            # Then interface methods
            iface_mt = self.env.impls.find_method(obj_ty.name, f.field)
            if iface_mt: return iface_mt
            self._error_at(
                f,
                f"'{obj_ty.name}' has no field or method '{f.field}'",
                hint=f"available fields: {list(obj_ty.fields.keys())}"
            )
            return T_ERR

        if isinstance(obj_ty, TTuple):
            ft = obj_ty.field_type(f.field)
            if ft: return ft
            self._error_at(f, f"tuple has no field '{f.field}'")
            return T_ERR

        if isinstance(obj_ty, TUnion):
            self._error_at(
                f,
                "use match to access union variants, not field access"
            )
            return T_ERR

        if isinstance(obj_ty, (TErrorSet, TErrorSetUnion)):
            variants = error_set_variants(obj_ty)
            if f.field not in variants:
                self._error_at(
                    f,
                    f"error '{format_type_for_user(obj_ty)}' has no variant '{f.field}'",
                    hint=f"available variants: {list(variants.keys())}"
                )
                return T_ERR
            payload = variants[f.field]
            if payload is None:
                return T_BOOL
            return TOptional(payload)

        # vec.len / vec.cap
        if isinstance(obj_ty, TVec):
            if f.field == "len": return T_I64
            if f.field == "cap": return T_I64
            self._error_at(f, f"vec has no field '{f.field}' (available: len, cap)")
            return T_ERR

        # slice.len
        if isinstance(obj_ty, TSlice):
            if f.field == "len": return T_I64
            self._error_at(f, f"slice has no field '{f.field}' (available: len)")
            return T_ERR

        # str.len → i64, str.data → *u8
        if isinstance(obj_ty, TString):
            if f.field == "len":  return T_I64
            if f.field == "data": return TPointer(TInt(8, False))
            self._error_at(f, f"str has no field '{f.field}' (available: len, data)")
            return T_ERR

        from src.types import TInt as _TIntField, TFloat as _TFloatField, TIntLit as _TIntLitField, TFloatLit as _TFloatLitField, TUnitful as _TUnitfulField, TUncertain as _TUncertainField
        if isinstance(obj_ty, (_TIntField, _TFloatField, _TIntLitField, _TFloatLitField, _TUnitfulField, _TUncertainField)):
            if f.field == "sqrt":
                result = self._sqrt_result_type(obj_ty, f)
                if result.is_error():
                    return result
                f._resolved_type = TFun(params=[], ret=result)
                return f._resolved_type

        self._error_at(f, f"cannot access field '{f.field}' on {obj_ty}")
        return T_ERR

    # ── Index expressions ─────────────────────────────────────

    def _synth_index(self, ix: IndexExpr) -> Type:
        obj_ty = self._synth_expr(ix.obj)
        if obj_ty.is_error(): return T_ERR

        for idx in ix.indices:
            self._check_expr(idx, T_I64)

        if isinstance(obj_ty, (TVec, TSlice, TArray)):
            return obj_ty.inner
        if isinstance(obj_ty, TMat):
            if len(ix.indices) == 2: return obj_ty.inner
            if len(ix.indices) == 1: return TMat(obj_ty.inner, 1, obj_ty.cols)
        self._error_at(ix, f"cannot index into {obj_ty}")
        return T_ERR

    # ── Function calls ────────────────────────────────────────

    def _synth_call(self, c: CallExpr) -> Type:
        qname = _qualified_name(c.callee)
        # Special internal helpers
        result = self._check_special_call(c)
        if result is not None:
            return result

        # Payload variant constructor: .Variant(val)
        if isinstance(c.callee, VariantLit):
            union_ty = self.env.find_variant_type(c.callee.name)
            if isinstance(union_ty, TUnion):
                payload_ty = union_ty.variants.get(c.callee.name)
                if payload_ty is not None and len(c.args) >= 1:
                    # Type-check the payload argument
                    self._check_expr(c.args[0].value, payload_ty)
                    c.callee._resolved_type = union_ty
                    c._resolved_type = union_ty
                    return union_ty
            self.diags.error(f"unknown variant '.{c.callee.name}'",
                             line=c.callee.line, col=c.callee.col,
                             span=getattr(c.callee, "span", None))
            return T_ERR

        if isinstance(c.callee, FieldExpr) and isinstance(c.callee.obj, Ident):
            type_ns = self.env.lookup_type(c.callee.obj.name)
            if isinstance(type_ns, TUnion) and c.callee.field in type_ns.variants:
                payload_ty = type_ns.variants[c.callee.field]
                if payload_ty is None:
                    self._error_at(
                        c.callee,
                        f"variant '{type_ns.name}.{c.callee.field}' does not take a payload"
                    )
                    return T_ERR
                if len(c.args) != 1:
                    self._error_at(
                        c,
                        f"variant '{type_ns.name}.{c.callee.field}' expects 1 argument, got {len(c.args)}"
                    )
                    return T_ERR
                self._check_expr(c.args[0].value, payload_ty)
                c.callee._resolved_type = type_ns
                c._resolved_type = type_ns
                return type_ns
            if isinstance(type_ns, (TErrorSet, TErrorSetUnion)) and c.callee.field in error_set_variants(type_ns):
                payload_ty = error_set_variants(type_ns)[c.callee.field]
                if payload_ty is None:
                    self._error_at(
                        c.callee,
                        f"error variant '{format_type_for_user(type_ns)}.{c.callee.field}' does not take a payload"
                    )
                    return T_ERR
                if len(c.args) != 1:
                    self._error_at(
                        c,
                        f"error variant '{format_type_for_user(type_ns)}.{c.callee.field}' expects 1 argument, got {len(c.args)}"
                    )
                    return T_ERR
                self._check_expr(c.args[0].value, payload_ty)
                c.callee._resolved_type = type_ns
                c._resolved_type = type_ns
                return type_ns

        callee_ty = self._synth_expr(c.callee)

        if callee_ty.is_error(): return T_ERR

        if not isinstance(callee_ty, TFun):
            self._error_at(c, f"cannot call non-function of type {callee_ty}")
            return T_ERR

        # Method calls: receiver is implicit — skip self param
        from src.types import TVar as _TV_check
        is_method_call = isinstance(c.callee, FieldExpr)
        is_static_type_call = is_method_call and isinstance(c.callee, FieldExpr) and self._is_type_receiver_expr(c.callee.obj)
        params = list(callee_ty.params)
        if is_method_call and not is_static_type_call and params:
            first = params[0]
            # self param is the receiver type or a pointer to it
            if isinstance(first, (TStruct, TUnion, TPointer, _TV_check)):
                params = params[1:]

        # Dynamic dispatch through *any Interface or any Interface
        from src.types import TDynInterface, TAnyInterface
        if is_method_call and isinstance(c.callee, FieldExpr):
            recv_ty = self._synth_expr(c.callee.obj)
            if isinstance(recv_ty, (TDynInterface, TAnyInterface)):
                method_name = c.callee.field
                iface = recv_ty.iface
                if method_name in iface.methods:
                    method_ty = iface.methods[method_name]
                    # Skip self param for arg count check
                    iface_params = list(method_ty.params[1:]) if method_ty.params else []
                    if len(c.args) != len(iface_params):
                        self._error_at(c, f"expected {len(iface_params)} arguments, got {len(c.args)}")
                        return T_ERR
                    for arg, pt in zip(c.args, iface_params):
                        self._check_expr(arg.value, pt)
                    return method_ty.ret
                self._error_at(c.callee, f"interface '{iface.name}' has no method '{method_name}'")
                return T_ERR

        if len(c.args) != len(params):
            self._error_at(c, f"expected {len(params)} arguments, got {len(c.args)}")
            return T_ERR

        # Generic call — infer concrete type bindings from arguments
        from src.types import TVar

        def contains_tvar(ty: Type) -> bool:
            if isinstance(ty, TVar):
                root = ty.root()
                return root is ty or contains_tvar(root)
            if isinstance(ty, TOptional):
                return contains_tvar(ty.inner)
            if isinstance(ty, TPointer):
                return contains_tvar(ty.inner)
            if isinstance(ty, TVec):
                return contains_tvar(ty.inner)
            if isinstance(ty, TTuple):
                return any(contains_tvar(field_ty) for _, field_ty in ty.fields)
            if isinstance(ty, TFun):
                return any(contains_tvar(p) for p in ty.params) or contains_tvar(ty.ret)
            if isinstance(ty, TStruct):
                return any(contains_tvar(arg_ty) for arg_ty in ty.type_args.values())
            return False

        def bind_typevars(pattern_ty: Type, concrete_ty: Type, bindings: Dict[str, Type]) -> None:
            if isinstance(pattern_ty, TVar) and pattern_ty.name:
                existing = bindings.get(pattern_ty.name)
                if existing is None:
                    bindings[pattern_ty.name] = concrete_ty
                else:
                    merged = unify(existing, concrete_ty)
                    if not merged.is_error():
                        bindings[pattern_ty.name] = merged
                return
            if isinstance(pattern_ty, TOptional) and isinstance(concrete_ty, TOptional):
                bind_typevars(pattern_ty.inner, concrete_ty.inner, bindings)
                return
            if isinstance(pattern_ty, TPointer) and isinstance(concrete_ty, TPointer):
                bind_typevars(pattern_ty.inner, concrete_ty.inner, bindings)
                return
            if isinstance(pattern_ty, TVec) and isinstance(concrete_ty, TVec):
                bind_typevars(pattern_ty.inner, concrete_ty.inner, bindings)
                return
            if isinstance(pattern_ty, TTuple) and isinstance(concrete_ty, TTuple) and len(pattern_ty.fields) == len(concrete_ty.fields):
                for (_, pty), (_, cty) in zip(pattern_ty.fields, concrete_ty.fields):
                    bind_typevars(pty, cty, bindings)
                return
            if isinstance(pattern_ty, TFun) and isinstance(concrete_ty, TFun) and len(pattern_ty.params) == len(concrete_ty.params):
                for pty, cty in zip(pattern_ty.params, concrete_ty.params):
                    bind_typevars(pty, cty, bindings)
                bind_typevars(pattern_ty.ret, concrete_ty.ret, bindings)
                return
            if isinstance(pattern_ty, TStruct) and isinstance(concrete_ty, TStruct) and pattern_ty.name == concrete_ty.name:
                for key, pty in pattern_ty.type_args.items():
                    cty = concrete_ty.type_args.get(key)
                    if cty is not None:
                        bind_typevars(pty, cty, bindings)

        has_tvars = any(contains_tvar(p) for p in params)
        bindings: Dict[str, Type] = {}
        if has_tvars:
            for arg, param_ty in zip(c.args, params):
                arg_ty = self._synth_expr(arg.value)
                if not arg_ty.is_error():
                    bind_typevars(param_ty, arg_ty, bindings)
            params  = [substitute(p, bindings) for p in params]
            ret_ty  = substitute(callee_ty.ret, bindings)
            # Store bindings and concrete return type on the call node
            c._type_bindings = bindings
            c._resolved_type = ret_ty   # concrete return type for codegen
        else:
            ret_ty = callee_ty.ret

        # Check each argument — println/print accept any numeric
        fn_name = c.callee.name if isinstance(c.callee, Ident) else ""
        for arg, param_ty in zip(c.args, params):
            from src.types import TUnitful, TUncertain
            if fn_name in ("println", "print") and (
                (arg_ty := self._synth_expr(arg.value)).is_numeric() or
                isinstance(arg_ty, TBool) or
                isinstance(arg_ty, TUnitful) or
                isinstance(arg_ty, TUncertain)
            ):
                pass   # accept unitful and uncertain values in println
            elif isinstance(param_ty, TVar):
                pass   # unresolved TVar — skip check (will fail gracefully)
            else:
                self._check_expr(arg.value, param_ty)

        c._resolved_type = ret_ty
        return ret_ty

    def _check_special_call(self, c: CallExpr) -> Optional[Type]:
        """Handle parser-generated synthetic calls."""
        qname = _qualified_name(c.callee)
        name = qname or (c.callee.name if isinstance(c.callee, Ident) else "")
        if name == "__orelse":
            opt_ty = self._synth_expr(c.args[0].value)
            if isinstance(opt_ty, TOptional):
                # Default must match inner type exactly (no widening across types)
                self._check_expr(c.args[1].value, opt_ty.inner)
                c._resolved_type = opt_ty.inner
                return opt_ty.inner
            c._resolved_type = opt_ty
            return opt_ty
        if name in ("len", "cap"):
            if len(c.args) != 1:
                self._error_at(c, f"{name} expects 1 argument, got {len(c.args)}")
                return T_ERR
            inner = self._synth_expr(c.args[0].value)
            if isinstance(inner, TVec):
                c._resolved_type = T_I64
                return T_I64
            if name == "len" and isinstance(inner, (TSlice, TString)):
                c._resolved_type = T_I64
                return T_I64
            self._error_at(c, f"{name} is not defined for {inner}")
            return T_ERR
        if name == "@assert":
            if len(c.args) != 1:
                self._error_at(c, "@assert expects 1 argument")
                return T_ERR
            self._check_expr(c.args[0].value, T_BOOL)
            c._resolved_type = T_VOID
            c._assert_in_test = self._in_test_block
            return T_VOID
        if name in ("@test.compile", "@test.compileFile"):
            if not self._in_test_block:
                self._error_at(c, f"{name} may only be used inside a test block")
                return T_ERR
            if len(c.args) != 1:
                self._error_at(c, f"{name} expects 1 argument, got {len(c.args)}")
                return T_ERR
            arg = c.args[0].value
            if not isinstance(arg, StringLit):
                self._error_at(arg, f"{name} expects a string literal")
                return T_ERR
            arg_value = self._string_literal_value(arg)
            if name == "@test.compile":
                result = self._run_nested_test_compile(
                    source_override=arg_value,
                    source_path=self._current_source_file,
                    local_root=None,
                )
            else:
                base_dir = os.path.dirname(self._current_source_file) if self._current_source_file else os.getcwd()
                target_path = arg_value if os.path.isabs(arg_value) else os.path.abspath(os.path.join(base_dir, arg_value))
                result = self._run_nested_test_compile(
                    source_path=target_path,
                    local_root=os.path.dirname(target_path),
                )
            c._test_compile_result = result
            c._resolved_type = self._test_compile_result_type()
            return c._resolved_type
        if name in ("@sizeOf", "@alignOf"):
            if len(c.args) != 1:
                self._error_at(c, f"{name} expects 1 argument, got {len(c.args)}")
                return T_ERR
            target_ty = self._intrinsic_target_type(c.args[0].value)
            if target_ty is None or target_ty.is_error():
                self._error_at(c, f"{name} requires a valid type or value")
                return T_ERR
            c._resolved_type = T_I64
            return T_I64
        if name == "@hasField":
            if len(c.args) != 2:
                self._error_at(c, f"{name} expects 2 arguments, got {len(c.args)}")
                return T_ERR
            target_ty = self._intrinsic_target_type(c.args[0].value)
            field_arg = c.args[1].value
            if not isinstance(field_arg, StringLit):
                self._error_at(c.args[1].value, "@hasField expects a string literal field name")
                return T_ERR
            if target_ty is None or target_ty.is_error():
                self._error_at(c, "@hasField requires a valid type or value")
                return T_ERR
            c._compile_time_bool = self._compile_time_has_field(target_ty, field_arg.raw)
            c._resolved_type = T_BOOL
            return T_BOOL
        if name == "@alloc":
            if not self._require_internal_stdlib_intrinsic(c, name):
                return T_ERR
            if len(c.args) != 3:
                self._error_at(c, f"{name} expects 3 arguments, got {len(c.args)}")
                return T_ERR
            alloc_ty = self._synth_expr(c.args[0].value)
            if not self._is_allocator_operand_type(alloc_ty):
                self._error_at(c.args[0].value, f"@alloc expects an allocator, got {format_type_for_user(alloc_ty)}")
                return T_ERR
            self._check_expr(c.args[1].value, T_I64)
            self._check_expr(c.args[2].value, T_I64)
            c._resolved_type = TPointer(T_VOID)
            return c._resolved_type
        if name == "@realloc":
            if not self._require_internal_stdlib_intrinsic(c, name):
                return T_ERR
            if len(c.args) != 5:
                self._error_at(c, f"{name} expects 5 arguments, got {len(c.args)}")
                return T_ERR
            alloc_ty = self._synth_expr(c.args[0].value)
            if not self._is_allocator_operand_type(alloc_ty):
                self._error_at(c.args[0].value, f"@realloc expects an allocator, got {format_type_for_user(alloc_ty)}")
                return T_ERR
            ptr_ty = self._synth_expr(c.args[1].value)
            if not self._pointerish(ptr_ty):
                self._error_at(c.args[1].value, f"@realloc expects a pointer, got {format_type_for_user(ptr_ty)}")
                return T_ERR
            self._check_expr(c.args[2].value, T_I64)
            self._check_expr(c.args[3].value, T_I64)
            self._check_expr(c.args[4].value, T_I64)
            c._resolved_type = TPointer(T_VOID)
            return c._resolved_type
        if name == "@freeBytes":
            if not self._require_internal_stdlib_intrinsic(c, name):
                return T_ERR
            if len(c.args) != 3:
                self._error_at(c, f"{name} expects 3 arguments, got {len(c.args)}")
                return T_ERR
            alloc_ty = self._synth_expr(c.args[0].value)
            if not self._is_allocator_operand_type(alloc_ty):
                self._error_at(c.args[0].value, f"@freeBytes expects an allocator, got {format_type_for_user(alloc_ty)}")
                return T_ERR
            ptr_ty = self._synth_expr(c.args[1].value)
            if not self._pointerish(ptr_ty):
                self._error_at(c.args[1].value, f"@freeBytes expects a pointer, got {format_type_for_user(ptr_ty)}")
                return T_ERR
            self._check_expr(c.args[2].value, T_I64)
            c._resolved_type = T_VOID
            return T_VOID
        if name in ("@memcpy", "@memmove"):
            if len(c.args) != 3:
                self._error_at(c, f"{name} expects 3 arguments, got {len(c.args)}")
                return T_ERR
            dst_ty = self._synth_expr(c.args[0].value)
            src_ty = self._synth_expr(c.args[1].value)
            if not self._pointerish(dst_ty):
                self._error_at(c.args[0].value, f"{name} expects a destination pointer, got {format_type_for_user(dst_ty)}")
                return T_ERR
            if not self._pointerish(src_ty):
                self._error_at(c.args[1].value, f"{name} expects a source pointer, got {format_type_for_user(src_ty)}")
                return T_ERR
            self._check_expr(c.args[2].value, T_I64)
            c._resolved_type = TPointer(T_VOID)
            return c._resolved_type
        if name == "@memset":
            if len(c.args) != 3:
                self._error_at(c, f"{name} expects 3 arguments, got {len(c.args)}")
                return T_ERR
            dst_ty = self._synth_expr(c.args[0].value)
            if not self._pointerish(dst_ty):
                self._error_at(c.args[0].value, f"{name} expects a destination pointer, got {format_type_for_user(dst_ty)}")
                return T_ERR
            self._check_expr(c.args[1].value, T_I64)
            self._check_expr(c.args[2].value, T_I64)
            c._resolved_type = TPointer(T_VOID)
            return c._resolved_type
        if name == "@memcmp":
            if len(c.args) != 3:
                self._error_at(c, f"{name} expects 3 arguments, got {len(c.args)}")
                return T_ERR
            lhs_ty = self._synth_expr(c.args[0].value)
            rhs_ty = self._synth_expr(c.args[1].value)
            if not self._pointerish(lhs_ty):
                self._error_at(c.args[0].value, f"{name} expects a pointer, got {format_type_for_user(lhs_ty)}")
                return T_ERR
            if not self._pointerish(rhs_ty):
                self._error_at(c.args[1].value, f"{name} expects a pointer, got {format_type_for_user(rhs_ty)}")
                return T_ERR
            self._check_expr(c.args[2].value, T_I64)
            c._resolved_type = TInt(32, True)
            return c._resolved_type
        if name == "@panic":
            if len(c.args) != 1:
                self._error_at(c, f"{name} expects 1 argument, got {len(c.args)}")
                return T_ERR
            self._check_expr(c.args[0].value, T_STR)
            c._resolved_type = T_VOID
            return T_VOID
        if name == "__try":
            inner = self._synth_expr(c.args[0].value)
            if not isinstance(inner, TErrorUnion):
                self._error_at(c, "'try' used on non-error-union expression", code="try-non-error-union")
                return inner
            ret = self.env.get_return_type()
            has_handle = getattr(self.env, '_current_fn_has_handle', False)
            local_with_handle = getattr(self.env, '_with_handle_depth', 0) > 0
            invalid_try = False
            if self._current_region() is not None and has_handle and not local_with_handle and not isinstance(ret, TErrorUnion):
                self._error_at(
                    c,
                    "'try' inside a cleanup-bearing with block needs a local with handle or an E!T return",
                    hint="attach 'handle |e| { ... }' to the with expression, or propagate the error with an E!T return type",
                    code="try-cleanup-needs-local-handle",
                )
                invalid_try = True
            if ret is not None and not isinstance(ret, TErrorUnion) and not has_handle and not local_with_handle:
                self._error_at(
                    c,
                    "'try' cannot be used here — enclosing function must return E!T or have a handle block",
                    hint="add a handle block on the enclosing function or with expression, or change return type to E!T",
                    code="try-context",
                )
                invalid_try = True
            if invalid_try:
                return T_ERR
            self._note_handle_error_set(inner.error_set)
            c._resolved_type = inner.payload
            return inner.payload
        if name in ("__catch", "__catch_bind"):
            inner = self._synth_expr(c.args[0].value)
            if isinstance(inner, TErrorUnion):
                c._resolved_type = inner.payload
                return inner.payload
            c._resolved_type = inner
            return inner
        if name == "@pageSize":
            if not self._require_internal_stdlib_intrinsic(c, name):
                return T_ERR
            if len(c.args) != 0:
                self._error_at(c, "@pageSize expects 0 arguments")
                return T_ERR
            c._resolved_type = T_I64
            return T_I64
        if name == "@pageAlloc":
            if not self._require_internal_stdlib_intrinsic(c, name):
                return T_ERR
            if len(c.args) != 1:
                self._error_at(c, "@pageAlloc expects 1 argument")
                return T_ERR
            self._check_expr(c.args[0].value, T_I64)
            c._resolved_type = TPointer(T_VOID)
            return c._resolved_type
        if name == "@pageFree":
            if not self._require_internal_stdlib_intrinsic(c, name):
                return T_ERR
            if len(c.args) != 2:
                self._error_at(c, "@pageFree expects 2 arguments")
                return T_ERR
            ptr_ty = self._synth_expr(c.args[0].value)
            if not self._pointerish(ptr_ty):
                self._error_at(c.args[0].value, f"@pageFree expects a pointer, got {format_type_for_user(ptr_ty)}")
                return T_ERR
            self._check_expr(c.args[1].value, T_I64)
            c._resolved_type = T_VOID
            return T_VOID
        if name == "@cAlloc":
            if not self._require_internal_stdlib_intrinsic(c, name):
                return T_ERR
            if len(c.args) != 2:
                self._error_at(c, "@cAlloc expects 2 arguments")
                return T_ERR
            self._check_expr(c.args[0].value, T_I64)
            self._check_expr(c.args[1].value, T_I64)
            c._resolved_type = TPointer(T_VOID)
            return c._resolved_type
        if name == "@cRealloc":
            if not self._require_internal_stdlib_intrinsic(c, name):
                return T_ERR
            if len(c.args) != 4:
                self._error_at(c, "@cRealloc expects 4 arguments")
                return T_ERR
            ptr_ty = self._synth_expr(c.args[0].value)
            if not self._pointerish(ptr_ty):
                self._error_at(c.args[0].value, f"@cRealloc expects a pointer, got {format_type_for_user(ptr_ty)}")
                return T_ERR
            self._check_expr(c.args[1].value, T_I64)
            self._check_expr(c.args[2].value, T_I64)
            self._check_expr(c.args[3].value, T_I64)
            c._resolved_type = TPointer(T_VOID)
            return c._resolved_type
        if name == "@cFree":
            if not self._require_internal_stdlib_intrinsic(c, name):
                return T_ERR
            if len(c.args) != 1:
                self._error_at(c, "@cFree expects 1 argument")
                return T_ERR
            ptr_ty = self._synth_expr(c.args[0].value)
            if not self._pointerish(ptr_ty):
                self._error_at(c.args[0].value, f"@cFree expects a pointer, got {format_type_for_user(ptr_ty)}")
                return T_ERR
            c._resolved_type = T_VOID
            return T_VOID
        if name == "@ptrAdd":
            if not self._require_internal_stdlib_intrinsic(c, name):
                return T_ERR
            if len(c.args) != 2:
                self._error_at(c, "@ptrAdd expects 2 arguments")
                return T_ERR
            ptr_ty = self._synth_expr(c.args[0].value)
            if not self._pointerish(ptr_ty):
                self._error_at(c.args[0].value, f"@ptrAdd expects a pointer, got {format_type_for_user(ptr_ty)}")
                return T_ERR
            self._check_expr(c.args[1].value, T_I64)
            c._resolved_type = TPointer(T_VOID)
            return c._resolved_type
        if name == "__optional_chain":
            inner = self._synth_expr(c.args[0].value)
            if isinstance(inner, TOptional): return inner
            return TOptional(inner)
        if name == "__format":
            return T_STR
        if name in ("@typeof", "@typof"):
            if len(c.args) != 1:
                self._error_at(c, f"{name} expects 1 argument, got {len(c.args)}")
            elif c.args:
                self._synth_expr(c.args[0].value)
            c._resolved_type = T_STR
            return T_STR
        return None

    # ── If expressions ────────────────────────────────────────

    def _check_if(self, ie: IfExpr,
                   expected: Optional[Type]) -> Type:
        self._check_expr(ie.cond, T_BOOL)
        if expected:
            then_ty = self._check_block(ie.then_block, expected)
            else_ty = self._check_block(ie.else_block, expected) if ie.else_block else None
        else:
            then_ty = self._check_block(ie.then_block)
            else_ty = self._check_block(ie.else_block) if ie.else_block else None

        if then_ty is None and else_ty is None: return T_VOID
        if else_ty is None: return TOptional(then_ty or T_VOID)
        # Suffix ternary: else is bare none → ?T
        if isinstance(else_ty, TOptional) and isinstance(else_ty.inner, TVar):
            opt = TOptional(then_ty or T_VOID)
            ie._resolved_type = opt
            return opt
        if isinstance(then_ty, TOptional) and isinstance(then_ty.inner, TVar):
            opt = TOptional(else_ty or T_VOID)
            ie._resolved_type = opt
            return opt
        result = unify(then_ty or T_VOID, else_ty)
        if result.is_error():
            self.diags.error(
                f"if branches have incompatible types: "
                f"{then_ty} and {else_ty}",
                hint="both branches must return the same type"
            )
            return T_ERR
        ie._resolved_type = result  # annotate for codegen
        return result

    def _check_if_unwrap(self, ie: IfUnwrap,
                          expected: Optional[Type]) -> Type:
        opt_ty = self._synth_expr(ie.expr)
        if isinstance(opt_ty, TOptional):
            inner = opt_ty.inner
        elif opt_ty.is_error():
            inner = T_ERR
        else:
            self.diags.error(
                f"if unwrap requires optional type, got {opt_ty}",
                hint="use 'if expr |v|' only with ?T types"
            )
            inner = T_ERR

        bind_ty = TPointer(inner) if ie.is_ref else inner
        self.env.push_scope()
        self.env.define(Symbol(ie.binding, bind_ty, ie.is_ref))
        then_ty = self._check_block(ie.then_block, expected)
        self.env.pop_scope()

        else_ty = None
        if ie.else_block:
            else_ty = self._check_block(ie.else_block, expected)

        if then_ty is not None and else_ty is not None:
            result = unify(then_ty, else_ty)
            if result.is_error():
                self.diags.error(
                    f"if branches have incompatible types: "
                    f"{then_ty} and {else_ty}"
                )
            return result
        if then_ty is not None: return then_ty
        if else_ty is not None: return else_ty
        return T_VOID

    def _check_while_unwrap(self, w: WhileUnwrap) -> Type:
        opt_ty = self._synth_expr(w.expr)
        if isinstance(opt_ty, TOptional):
            inner = opt_ty.inner
        else:
            inner = opt_ty

        bind_ty = TPointer(inner) if w.is_ref else inner
        self.env.push_loop()
        self.env.push_scope()
        self.env.define(Symbol(w.binding, bind_ty, w.is_ref))
        self._check_block(w.body)
        self.env.pop_scope()
        self.env.pop_loop()
        return T_VOID

    # ── Match expressions ─────────────────────────────────────

    def _check_match(self, me: MatchExpr,
                      expected: Optional[Type]) -> Type:
        val_ty  = self._synth_expr(me.value)
        # Annotate for exhaustiveness checker
        me._checked_type = val_ty
        arm_tys = []

        for arm in me.arms:
            self.env.push_scope()
            self._bind_match_pattern(arm.pattern, val_ty)
            arm_ty = self._check_block(arm.body, expected)
            self.env.pop_scope()
            if arm_ty: arm_tys.append(arm_ty)

        if not arm_tys: return T_VOID
        result = arm_tys[0]
        for ty in arm_tys[1:]:
            result = unify(result, ty)
            if result.is_error():
                self.diags.error(
                    "match arms have incompatible types",
                    hint="all arms must return the same type"
                )
                return T_ERR
        me._resolved_type = result
        return result

    def _bind_match_pattern(self, pat: MatchPattern, val_ty: Type):
        if isinstance(pat, PatWildcard): pass
        elif isinstance(pat, PatIdent):
            self.env.define(Symbol(pat.name, val_ty, False))
        elif isinstance(pat, PatVariant):
            if isinstance(val_ty, TUnion):
                payload = val_ty.variant_payload(pat.name)
                if payload is None and (pat.binding or pat.extra_bindings):
                    self.diags.error(f"variant '{pat.name}' has no payload")
                elif payload and pat.binding and not pat.extra_bindings:
                    self.env.define(Symbol(pat.binding, payload, False))
                elif payload and pat.extra_bindings:
                    # tuple payload: Rectangle(w, h)
                    all_bindings = ([pat.binding] if pat.binding else []) + pat.extra_bindings
                    if isinstance(payload, TTuple):
                        for bname, (_, bty) in zip(all_bindings, payload.fields):
                            self.env.define(Symbol(bname, bty, False))
                    else:
                        # fallback: bind all to same payload type
                        for bname in all_bindings:
                            self.env.define(Symbol(bname, payload, False))
            elif isinstance(val_ty, (TErrorSet, TErrorSetUnion)):
                variants = error_set_variants(val_ty)
                payload = variants.get(pat.name)
                if payload is not None and (pat.binding or pat.extra_bindings):
                    if pat.binding and not pat.extra_bindings:
                        self.env.define(Symbol(pat.binding, payload, False))
                    elif pat.extra_bindings:
                        all_bindings = ([pat.binding] if pat.binding else []) + pat.extra_bindings
                        if isinstance(payload, TTuple):
                            for bname, (_, bty) in zip(all_bindings, payload.fields):
                                self.env.define(Symbol(bname, bty, False))
                        else:
                            for bname in all_bindings:
                                self.env.define(Symbol(bname, payload, False))
                elif pat.name in variants and payload is None and (pat.binding or pat.extra_bindings):
                    self.diags.error(f"variant '{pat.name}' has no payload")
            elif not val_ty.is_error():
                self.diags.error(
                    f"pattern '{pat.name}(...)' used on non-union type {val_ty}"
                )

    # ── Closure checking ──────────────────────────────────────

    def _check_closure(self, c: Closure, expected: TFun) -> Type:
        """Check closure against expected function type, inferring param types.

        Closures in Mesa may NOT capture outer variables — use a struct with
        methods to carry state instead. Non-capturing lambdas are supported.
        """
        if len(c.params) != len(expected.params):
            self.diags.error(
                f"closure has {len(c.params)} params, "
                f"expected {len(expected.params)}"
            )
            return T_ERR

        params = []
        for p, expected_ty in zip(c.params, expected.params):
            if isinstance(p.type_, TyInfer):
                pt = expected_ty   # infer from context
            else:
                pt = lower_type(p.type_, self.env)
                result = unify(pt, expected_ty)
                if result.is_error():
                    self.diags.error(
                        f"closure param '{p.name}' has type {pt}, "
                        f"expected {expected_ty}"
                    )
            params.append(pt)

        ret = lower_type(c.ret, self.env) if not isinstance(c.ret, TyInfer) \
              else expected.ret

        # Check body in an isolated scope — outer names are intentionally hidden
        # to detect captures. Any name that resolves only in the outer scope
        # would be an undefined name error here, which is the right behaviour.
        param_names = {p.name for p in c.params}
        self.env.push_scope()
        self.env.set_return_type(ret)
        for p, pt in zip(c.params, params):
            self.env.define(Symbol(p.name, pt, False))
        # Mark that we are inside a closure so the definite-assignment pass
        # can skip outer-scope variables rather than flagging them
        prev_in_closure = getattr(self, '_in_closure', False)
        self._in_closure = True
        self._check_block(c.body, expected=ret)
        self._in_closure = prev_in_closure
        self.env.set_return_type(None)
        self.env.pop_scope()
        return TFun(params, ret)


# ══════════════════════════════════════════════════════════════
# Diagnostic pass — rich error messages for generic failures
# ══════════════════════════════════════════════════════════════
class DiagnosticPass:
    """
    Triggered when codegen (or the normal checker) hits a type error
    in a generic instantiation. Re-checks the specific instantiation
    with full monomorphisation to produce a human-readable message.
    """
    def __init__(self, base_env: Environment):
        self.base_env = base_env

    def check_instantiation(self,
                             fun_decl: FunDecl,
                             type_args: Dict[str, Type],
                             call_site_line: int = 0,
                             call_site_col:  int = 0) -> List[str]:
        """
        Re-check fun_decl with concrete type_args substituted.
        Returns list of human-readable error messages.
        """
        diags   = DiagnosticBag()
        env     = Environment(diags)

        # Copy type registry from base env
        env._types.update(self.base_env._types)
        env.impls  = self.base_env.impls
        env._interfaces = self.base_env._interfaces

        # Check where clause constraints
        messages = []
        for constraint in fun_decl.where:
            # Parse "T : Add, Mul" style constraints
            if ":" in "".join(constraint):
                parts = "".join(constraint).split(":")
                if len(parts) == 2:
                    type_param = parts[0].strip()
                    ifaces     = [i.strip() for i in parts[1].split(",")]
                    concrete   = type_args.get(type_param)
                    if concrete:
                        for iface in ifaces:
                            if not env.check_constraint(concrete, iface):
                                messages.append(
                                    f"  {concrete} does not implement {iface}\n"
                                    f"  required by '{fun_decl.name}' "
                                    f"at [{call_site_line}:{call_site_col}]"
                                )

        # Substitute type args and check body
        # (simplified — full monomorphisation in a production compiler
        #  would clone and rewrite the entire AST)
        if fun_decl.body and messages:
            # Only go deeper if constraint check already found issues
            # Avoids false positives from incomplete substitution
            pass

        return messages


class InferredErrorSetPass:
    def __init__(self, env: Environment):
        self.env = env

    def run(self, program: Program):
        entries = _all_function_entries(program)
        inferred_entries = [
            (f, receiver)
            for f, receiver in entries
            if isinstance(f.ret, TyErrorUnion) and f.ret.error_set is None and f.body is not None
        ]
        if not inferred_entries:
            return

        for f, receiver in entries:
            self._update_fun_symbol(f, receiver, effective_return_type(f, self.env))

        max_rounds = max(4, len(inferred_entries) * 4)
        for _ in range(max_rounds):
            changed = False
            for f, receiver in inferred_entries:
                payload_ty = lower_type(f.ret.payload, self.env)
                inferred_eset = self._infer_fun_error_set(f)
                effective = TErrorUnion(inferred_eset, payload_ty)
                if getattr(f, "_effective_ret_type", None) != effective:
                    f._effective_ret_type = effective
                    self._update_fun_symbol(f, receiver, effective)
                    changed = True
            if not changed:
                break

    def _update_fun_symbol(self, f: FunDecl, receiver: Optional[str], ret_ty: Type):
        sym = self.env.lookup(_function_symbol_name(f, receiver))
        if sym and isinstance(sym.type_, TFun):
            sym.type_ = TFun(
                list(sym.type_.params),
                ret_ty,
                type_params=list(sym.type_.type_params),
                where=list(sym.type_.where),
            )

    def _infer_fun_error_set(self, f: FunDecl) -> Type:
        if f.body is None:
            return empty_error_set()
        scopes = [dict()]
        for p in f.params:
            scopes[-1][p.name] = lower_type(p.type_, self.env)
        body_errors = self._collect_block_errors(
            f.body,
            scopes,
            intercept_try=bool(getattr(f, "handle_block", None)),
            tail_escapes=True,
        )
        handle_errors = None
        if getattr(f, "handle_block", None):
            handle_scope = dict()
            binding_ty = getattr(f.handle_block, "_binding_type", None)
            if binding_ty is not None:
                handle_scope[f.handle_block.binding] = binding_ty
            handle_errors = self._collect_block_errors(
                f.handle_block.body,
                scopes + [handle_scope],
                intercept_try=False,
                tail_escapes=True,
            )
        return merge_error_sets(body_errors, handle_errors, name=None) or empty_error_set()

    def _collect_block_errors(self, block: Optional[Block],
                              scopes: List[Dict[str, Type]],
                              *,
                              intercept_try: bool,
                              tail_escapes: bool) -> Optional[Type]:
        if block is None:
            return None
        local_scopes = scopes + [dict()]
        errors = None
        for stmt in block.stmts:
            errors = merge_error_sets(errors, self._collect_stmt_errors(stmt, local_scopes, intercept_try))
        if block.tail is not None:
            if tail_escapes:
                errors = merge_error_sets(errors, self._collect_return_context(block.tail, local_scopes, intercept_try))
            else:
                errors = merge_error_sets(errors, self._collect_expr_propagation(block.tail, local_scopes, intercept_try))
        return errors

    def _collect_stmt_errors(self, stmt: Stmt, scopes: List[Dict[str, Type]],
                             intercept_try: bool) -> Optional[Type]:
        if isinstance(stmt, LetStmt):
            errors = self._collect_expr_propagation(stmt.init, scopes, intercept_try) if stmt.init else None
            bound_ty = lower_type(stmt.type_, self.env) if stmt.type_ else self._expr_type(stmt.init, scopes)
            if bound_ty is not None:
                scopes[-1][stmt.name] = bound_ty
            return errors
        if isinstance(stmt, ReturnStmt):
            return self._collect_return_context(stmt.value, scopes, intercept_try) if stmt.value is not None else None
        if isinstance(stmt, AssignStmt):
            return self._collect_expr_propagation(stmt.value, scopes, intercept_try)
        if isinstance(stmt, ExprStmt):
            return self._collect_expr_propagation(stmt.expr, scopes, intercept_try)
        if isinstance(stmt, ForRangeStmt):
            errors = merge_error_sets(
                self._collect_expr_propagation(stmt.start, scopes, intercept_try),
                self._collect_expr_propagation(stmt.end, scopes, intercept_try),
                self._collect_expr_propagation(stmt.filter, scopes + [{stmt.var: T_I64}], intercept_try) if stmt.filter else None,
            )
            body_scope = scopes + [{stmt.var: T_I64}]
            return merge_error_sets(errors, self._collect_block_errors(stmt.body, body_scope, intercept_try=intercept_try, tail_escapes=False))
        if isinstance(stmt, ForIterStmt):
            errors = self._collect_expr_propagation(stmt.iter, scopes, intercept_try)
            return merge_error_sets(errors, self._collect_block_errors(stmt.body, scopes, intercept_try=intercept_try, tail_escapes=False))
        if isinstance(stmt, WhileStmt):
            errors = self._collect_expr_propagation(stmt.cond, scopes, intercept_try)
            return merge_error_sets(errors, self._collect_block_errors(stmt.body, scopes, intercept_try=intercept_try, tail_escapes=False))
        if isinstance(stmt, DeferStmt):
            return self._collect_block_errors(stmt.body, scopes, intercept_try=intercept_try, tail_escapes=False)
        return None

    def _collect_return_context(self, expr: Optional[Expr], scopes: List[Dict[str, Type]],
                                intercept_try: bool) -> Optional[Type]:
        if expr is None:
            return None
        errors = self._collect_expr_propagation(expr, scopes, intercept_try)
        if isinstance(expr, BlockExpr):
            return merge_error_sets(errors, self._collect_block_errors(expr.block, scopes, intercept_try=intercept_try, tail_escapes=True))
        if isinstance(expr, IfExpr):
            errors = merge_error_sets(errors, self._collect_block_errors(expr.then_block, scopes, intercept_try=intercept_try, tail_escapes=True))
            if expr.else_block:
                errors = merge_error_sets(errors, self._collect_block_errors(expr.else_block, scopes, intercept_try=intercept_try, tail_escapes=True))
            return errors
        if isinstance(expr, MatchExpr):
            for arm in expr.arms:
                errors = merge_error_sets(errors, self._collect_block_errors(arm.body, scopes, intercept_try=intercept_try, tail_escapes=True))
            return errors
        if isinstance(expr, WithExpr):
            errors = merge_error_sets(
                errors,
                self._collect_block_errors(expr.body, scopes, intercept_try=(intercept_try or bool(expr.handle)), tail_escapes=True),
            )
            if expr.handle:
                handle_scope = scopes + [{expr.handle.binding: getattr(expr.handle, "_binding_type", None) or T_I64}]
                errors = merge_error_sets(
                    errors,
                    self._collect_block_errors(expr.handle.body, handle_scope, intercept_try=False, tail_escapes=True),
                )
            return errors
        direct = self._expr_type(expr, scopes)
        if isinstance(direct, (TErrorSet, TErrorSetUnion)):
            return merge_error_sets(errors, direct)
        return errors

    def _collect_expr_propagation(self, expr: Optional[Expr], scopes: List[Dict[str, Type]],
                                  intercept_try: bool,
                                  suppress_direct_esc: bool = False) -> Optional[Type]:
        if expr is None:
            return None
        if isinstance(expr, EscExpr):
            if suppress_direct_esc:
                return self._collect_expr_propagation(expr.expr, scopes, intercept_try, suppress_direct_esc=False)
            return merge_error_sets(self.env.lookup_type("EscError"), self._collect_expr_propagation(expr.expr, scopes, intercept_try))
        if isinstance(expr, WithAllocExpr):
            return merge_error_sets(
                self._collect_expr_propagation(expr.expr, scopes, intercept_try),
                self._collect_expr_propagation(expr.allocator, scopes, intercept_try),
            )
        if isinstance(expr, CallExpr) and isinstance(expr.callee, Ident) and expr.callee.name == "__try":
            inner = expr.args[0].value if expr.args else None
            inner_err = self._collect_expr_propagation(inner, scopes, intercept_try, suppress_direct_esc=True)
            if intercept_try:
                return inner_err
            inner_ty = self._expr_type(inner, scopes)
            if isinstance(inner_ty, TErrorUnion):
                return merge_error_sets(inner_err, inner_ty.error_set)
            return inner_err
        if isinstance(expr, CallExpr):
            errors = self._collect_expr_propagation(expr.callee, scopes, intercept_try)
            for arg in expr.args:
                errors = merge_error_sets(errors, self._collect_expr_propagation(arg.value, scopes, intercept_try))
            return errors
        if isinstance(expr, BinExpr):
            return merge_error_sets(
                self._collect_expr_propagation(expr.left, scopes, intercept_try),
                self._collect_expr_propagation(expr.right, scopes, intercept_try),
            )
        if isinstance(expr, UnaryExpr):
            return self._collect_expr_propagation(expr.operand, scopes, intercept_try)
        if isinstance(expr, FieldExpr):
            return self._collect_expr_propagation(expr.obj, scopes, intercept_try)
        if isinstance(expr, IndexExpr):
            return merge_error_sets(
                self._collect_expr_propagation(expr.obj, scopes, intercept_try),
                self._collect_expr_propagation(expr.index, scopes, intercept_try),
            )
        if isinstance(expr, TupleLit):
            errors = None
            for _, field_expr in expr.fields:
                errors = merge_error_sets(errors, self._collect_expr_propagation(field_expr, scopes, intercept_try))
            return errors
        if isinstance(expr, VecLit):
            errors = None
            for elem in expr.elems:
                errors = merge_error_sets(errors, self._collect_expr_propagation(elem, scopes, intercept_try))
            return errors
        if isinstance(expr, VecComp):
            errors = self._collect_expr_propagation(expr.iter, scopes, intercept_try)
            if expr.filter:
                errors = merge_error_sets(errors, self._collect_expr_propagation(expr.filter, scopes, intercept_try))
            return merge_error_sets(errors, self._collect_expr_propagation(expr.expr, scopes, intercept_try))
        if isinstance(expr, ArrayLit):
            errors = None
            for elem in expr.elems:
                errors = merge_error_sets(errors, self._collect_expr_propagation(elem, scopes, intercept_try))
            return errors
        if isinstance(expr, IfExpr):
            errors = self._collect_expr_propagation(expr.cond, scopes, intercept_try)
            errors = merge_error_sets(errors, self._collect_block_errors(expr.then_block, scopes, intercept_try=intercept_try, tail_escapes=False))
            if expr.else_block:
                errors = merge_error_sets(errors, self._collect_block_errors(expr.else_block, scopes, intercept_try=intercept_try, tail_escapes=False))
            return errors
        if isinstance(expr, MatchExpr):
            errors = self._collect_expr_propagation(expr.scrutinee, scopes, intercept_try)
            for arm in expr.arms:
                errors = merge_error_sets(errors, self._collect_block_errors(arm.body, scopes, intercept_try=intercept_try, tail_escapes=False))
            return errors
        if isinstance(expr, BlockExpr):
            return self._collect_block_errors(expr.block, scopes, intercept_try=intercept_try, tail_escapes=False)
        if isinstance(expr, WithExpr):
            errors = self._collect_expr_propagation(expr.resource, scopes, intercept_try)
            errors = merge_error_sets(errors, self._collect_block_errors(expr.body, scopes, intercept_try=(intercept_try or bool(expr.handle)), tail_escapes=False))
            if expr.handle:
                handle_scope = scopes + [{expr.handle.binding: getattr(expr.handle, "_binding_type", None) or T_I64}]
                errors = merge_error_sets(errors, self._collect_block_errors(expr.handle.body, handle_scope, intercept_try=False, tail_escapes=False))
            return errors
        return None

    def _expr_type(self, expr: Optional[Expr], scopes: List[Dict[str, Type]]) -> Optional[Type]:
        if expr is None:
            return None
        if isinstance(expr, Ident):
            for scope in reversed(scopes):
                if expr.name in scope:
                    return scope[expr.name]
            sym = self.env.lookup(expr.name)
            return sym.type_ if sym else None
        if isinstance(expr, VariantLit):
            ty = self.env.find_variant_type(expr.name)
            return ty if isinstance(ty, (TErrorSet, TErrorSetUnion)) else getattr(expr, "_resolved_type", None)
        if isinstance(expr, FieldExpr) and isinstance(expr.obj, Ident):
            owner = self.env.lookup_type(expr.obj.name)
            if isinstance(owner, (TErrorSet, TErrorSetUnion)) and expr.field in error_set_variants(owner):
                return owner
        if isinstance(expr, CallExpr):
            if isinstance(expr.callee, Ident) and expr.callee.name == "__try" and expr.args:
                inner = self._expr_type(expr.args[0].value, scopes)
                return inner.payload if isinstance(inner, TErrorUnion) else inner
            if isinstance(expr.callee, Ident) and expr.callee.name in ("__catch", "__catch_bind") and expr.args:
                inner = self._expr_type(expr.args[0].value, scopes)
                return inner.payload if isinstance(inner, TErrorUnion) else inner
            callee_ty = self._expr_type(expr.callee, scopes)
            if isinstance(callee_ty, TFun):
                return callee_ty.ret
        return getattr(expr, "_resolved_type", None)


# ══════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════
def type_check(
    program: Program,
    *,
    package_roots: Optional[List[Tuple[str, Optional[str]]]] = None,
    local_root: Optional[str] = None,
    source_path: Optional[str] = None,
) -> Tuple[Environment, DiagnosticBag]:
    """
    Run the full two-pass type checker on a program.
    Returns the populated environment and diagnostic bag.
    """
    diags = DiagnosticBag()
    env   = Environment(diags)
    env._package_roots = list(package_roots or [])
    env._local_root = local_root
    env._source_path = source_path

    for decl in program.decls:
        if isinstance(decl, (ImportDecl, FromImportDecl)):
            decl.path = canonicalize_std_import_path(decl.path)

    # Allocator types now come from the source-backed mem package.

    # Pass 1 — register all declarations
    decl_pass = DeclarationPass(env)
    decl_pass.run(program)

    # Quiet pre-pass — annotate bodies so inferred !T can see handle/call shapes
    quiet_diags = DiagnosticBag()
    saved_diags = env.diags
    env.diags = quiet_diags
    quiet_body = BodyChecker(env)
    for decl in program.decls:
        if isinstance(decl, (FunDecl, StructDecl, DefDecl)):
            quiet_body._check_decl(decl)
    env.diags = saved_diags

    # Infer concrete error-set unions for bare !T functions, then re-check bodies
    InferredErrorSetPass(env).run(program)
    BodyChecker(env).run(program)

    return env, diags
