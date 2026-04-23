"""
Mesa → C backend.

Emits standard C99 that compiles with any cc/gcc/clang.
No LLVM required. True zero-dependency compilation on any Unix.

Usage:
    from src.ccodegen import CCodegen
    cg = CCodegen(env, layout)
    cg.emit_all(prog)
    c_source = cg.output()

    # then: cc output.c -o binary -lm

Mesa → C type mapping:
    i8/i16/i32/i64   → int8_t/int16_t/int32_t/int64_t
    u8/u16/u32/u64   → uint8_t/uint16_t/uint32_t/uint64_t
    f32/f64          → float/double
    bool             → int  (0/1)
    str              → mesa_str  (struct { const char* data; int64_t len; })
    void             → void
    ?T               → struct { T value; int has_value; }
    *T               → T*
    vec[T]           → mesa_vec  (struct { T* data; int64_t len; int64_t cap; })
    .{f1: T1, ...}   → anonymous struct
    struct S         → typedef struct S S; struct S { ... };
    union U          → typedef struct U U; struct U { int64_t tag; union { ... } payload; };
"""
from __future__ import annotations
from typing import Dict, List, Optional, Set, Tuple
from src.ast import *
from src.types import *
from src.env import Environment
from src.analysis import LayoutPass

_ACTIVE_ENV: Optional[Environment] = None


def _is_builtin_allocator_iface_name(name: str) -> bool:
    return False


def _lookup_c_type_name(name: str) -> str:
    if _ACTIVE_ENV is not None:
        resolved = _ACTIVE_ENV.lookup_c_type_name(name)
        if resolved is not None:
            return resolved
    return name


def _iface_c_name(iface: TInterface) -> str:
    if _is_builtin_allocator_iface_name(iface.name):
        return "Allocator"
    return getattr(iface, "_c_name", None) or _lookup_c_type_name(iface.name)


def _error_key(eset: Optional[Type]) -> str:
    if eset is None:
        return "anyerror"
    c_name = getattr(eset, "_c_name", None)
    if c_name:
        return c_name
    if isinstance(eset, TErrorSet):
        return _lookup_c_type_name(eset.name)
    if isinstance(eset, TErrorSetUnion):
        if eset.name:
            return _lookup_c_type_name(eset.name)
        return "__".join(_error_key(member) for member in eset.members)
    return error_set_key(eset).replace(".", "_")


def _c_string_literal(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'(mesa_str){{"{escaped}", {len(text)}}}'


# ══════════════════════════════════════════════════════════════
# Type mapping
# ══════════════════════════════════════════════════════════════

def c_type(ty: Type, name: str = "") -> str:
    """Convert Mesa internal type to C type string."""
    if isinstance(ty, TInt):
        prefix = "int" if ty.signed else "uint"
        return f"{prefix}{ty.bits}_t"
    if isinstance(ty, TFloat):
        return "float" if ty.bits == 32 else "double"
    if isinstance(ty, TBool):   return "int"
    if isinstance(ty, TString): return "mesa_str"
    if isinstance(ty, TVoid):   return "void"
    if isinstance(ty, TPointer): return f"{c_type(ty.inner)}*"
    if isinstance(ty, TOptional):
        inner = c_type(ty.inner)
        return f"mesa_opt_{_mangle_type(ty.inner)}"
    if isinstance(ty, TSlice):
        return f"mesa_slice_{_mangle_type(ty.inner)}"
    if isinstance(ty, TVec):
        if ty.size is not None:
            return f"{c_type(ty.inner)}[{ty.size}]"
        return f"mesa_vec_{_mangle_type(ty.inner)}"
    if isinstance(ty, TArray):
        return f"{c_type(ty.inner)}[{ty.size}]"  # caller must handle
    if isinstance(ty, TTuple):
        return f"mesa_{_mangle_type(ty)}"
    if isinstance(ty, TStruct): return getattr(ty, "_c_name", None) or _lookup_c_type_name(ty.name)
    if isinstance(ty, TUnion):  return getattr(ty, "_c_name", None) or _lookup_c_type_name(ty.name)
    if isinstance(ty, TInterface):
        if _is_builtin_allocator_iface_name(ty.name):
            return "Mesa_Allocator"
        return _iface_c_name(ty)
    if isinstance(ty, TFun):
        params = ", ".join(c_type(p) for p in ty.params)
        if name:
            return f"{c_type(ty.ret)} (*{name})({params})"
        return f"{c_type(ty.ret)} (*)({params})"
    if isinstance(ty, TIntLit):   return "int64_t"
    if isinstance(ty, TFloatLit): return "double"
    if isinstance(ty, TVar):
        root = ty.root()
        return c_type(root) if root is not ty else "int64_t"
    if isinstance(ty, TError): return "int64_t"
    if isinstance(ty, TVar):
        return "int64_t"
    from src.types import TErrorUnion, TErrorSet, TErrorSetUnion
    if isinstance(ty, (TErrorSet, TErrorSetUnion)):
        return f"Mesa_error_{_error_key(ty)}"
    if isinstance(ty, TErrorUnion):
        ename = _error_key(ty.error_set)
        tmangle = _mangle_type(ty.payload)
        return f"Mesa_result_{ename}_{tmangle}"  # key matches _emit_result_struct
    from src.types import TUnitful, TUncertain, TDynInterface, TAnyInterface
    if isinstance(ty, TAnyInterface):
        if _is_builtin_allocator_iface_name(ty.iface.name):
            return "Mesa_Allocator"
        return f"Mesa_any_{_iface_c_name(ty.iface)}"   # stack existential — value type
    if isinstance(ty, TDynInterface):
        if _is_builtin_allocator_iface_name(ty.iface.name):
            return "Mesa_Allocator*"
        return f"Mesa_{_iface_c_name(ty.iface)}*"      # heap fat pointer
    if isinstance(ty, TNamespace):
        return "int64_t"
    if isinstance(ty, TUnitful):
        if ty.dims is None:  # float`?` — runtime unit struct
            return "mesa_unitful"
        return c_type(ty.inner)  # static unit — transparent, just the inner type
    if isinstance(ty, TUncertain):
        return f"mesa_uncertain_{_mangle_type(ty.inner)}"
    return "int64_t"

def c_typeexpr(ty: TypeExpr) -> str:
    """Convert AST TypeExpr to C type string."""
    if isinstance(ty, TyVoid):    return "void"
    if isinstance(ty, TyInfer):   return "int64_t"
    if isinstance(ty, TyPrimitive):
        m = {
            "i8": "int8_t",   "i16": "int16_t",
            "i32": "int32_t", "i64": "int64_t",
            "u8": "uint8_t",  "u16": "uint16_t",
            "u32": "uint32_t","u64": "uint64_t",
            "f32": "float",   "f64": "double",
            "bool": "int",    "str": "mesa_str",
            "void": "void",
        }
        return m.get(ty.name, "int64_t")
    if isinstance(ty, TyPointer):
        return f"{c_typeexpr(ty.inner)}*"
    if isinstance(ty, TySlice):
        return f"mesa_slice_{_mangle_typeexpr(ty.inner)}"
    if isinstance(ty, TyOptional):
        return f"mesa_opt_{_mangle_typeexpr(ty.inner)}"
    if isinstance(ty, TyVec):
        if ty.size is not None and isinstance(ty.size, IntLit):
            return f"{c_typeexpr(ty.elem)}[{ty.size.value}]"
        return f"mesa_vec_{_mangle_typeexpr(ty.elem)}"
    if isinstance(ty, TyNamed):
        # Normalise int/float aliases to proper C types
        if _is_builtin_allocator_iface_name(ty.name):
            return "Mesa_Allocator"
        return {"int": "int64_t", "float": "double"}.get(ty.name, _lookup_c_type_name(ty.name))
    if isinstance(ty, TyGeneric):
        return _lookup_c_type_name(ty.name)
    if isinstance(ty, TyTuple):
        return f"mesa_{_mangle_typeexpr(ty)}"
    if isinstance(ty, TyFun):
        params = ", ".join(c_typeexpr(p) for p in ty.params)
        return f"{c_typeexpr(ty.ret)} (*)({params})"
    if isinstance(ty, TyUnitful):
        if ty.unit == "?":
            return "mesa_unitful"
        return c_typeexpr(ty.inner)  # static unit → transparent
    if isinstance(ty, TyErrorUnion):
        # Use _mangle_typeexpr for the key — must match _emit_result_struct key
        ename = _mangle_typeexpr(ty.error_set) if ty.error_set else "anyerror"
        return f"Mesa_result_{ename}_{_mangle_typeexpr(ty.payload)}"
    if isinstance(ty, TyErrorSetUnion):
        return f"Mesa_error_{_mangle_typeexpr(ty)}"
    return "int64_t"

def _mangle_type(ty: Type) -> str:
    if isinstance(ty, TInt):      return f"{'i' if ty.signed else 'u'}{ty.bits}"
    if isinstance(ty, TFloat):    return f"f{ty.bits}"
    if isinstance(ty, TBool):     return "bool"
    if isinstance(ty, TString):   return "str"
    if isinstance(ty, TVoid):     return "void"
    if isinstance(ty, TIntLit):   return "i64"   # unresolved literal → default i64
    if isinstance(ty, TFloatLit): return "f64"   # unresolved literal → default f64
    if isinstance(ty, TOptional): return f"opt_{_mangle_type(ty.inner)}"
    if isinstance(ty, TSlice):    return f"slice_{_mangle_type(ty.inner)}"
    if isinstance(ty, TVec):      return f"vec_{_mangle_type(ty.inner)}"
    if isinstance(ty, TArray):    return f"arr_{_mangle_type(ty.inner)}_{ty.size}"
    if isinstance(ty, TTuple):
        parts = []
        for idx, (name, field_ty) in enumerate(ty.fields):
            field_name = name or str(idx)
            parts.append(f"{field_name}_{_mangle_type(field_ty)}")
        return "tuple_" + ("__".join(parts) if parts else "empty")
    if isinstance(ty, TStruct): return (getattr(ty, "_c_name", None) or _lookup_c_type_name(ty.name)).replace("_", "_")
    if isinstance(ty, TUnion):  return (getattr(ty, "_c_name", None) or _lookup_c_type_name(ty.name)).replace("_", "_")
    if isinstance(ty, (TErrorSet, TErrorSetUnion)):
        return _error_key(ty).replace(".", "_")
    from src.types import TUnitful, TUncertain, TDynInterface, TAnyInterface
    if isinstance(ty, TAnyInterface):
        return f"Mesa_any_{_iface_c_name(ty.iface)}"   # stack existential — value type
    if isinstance(ty, TDynInterface):
        return f"Mesa_{_iface_c_name(ty.iface)}*"      # heap fat pointer
    if isinstance(ty, TUnitful):
        if ty.dims is None: return "unitful"
        return _mangle_type(ty.inner)
    if isinstance(ty, TUncertain):
        return _mangle_type(ty.inner)
    return "unknown"

def _mangle_typeexpr(ty: TypeExpr) -> str:
    _ALIASES = {"int": "i64", "float": "f64"}
    if isinstance(ty, TyVoid):
        return "void"
    if isinstance(ty, TyPrimitive):
        name = ty.name.replace(" ", "_")
        return _ALIASES.get(name, name)
    if isinstance(ty, TyNamed):
        return _ALIASES.get(ty.name, _lookup_c_type_name(ty.name))
    if isinstance(ty, TyOptional):
        return f"opt_{_mangle_typeexpr(ty.inner)}"
    if isinstance(ty, TySlice):
        return f"slice_{_mangle_typeexpr(ty.inner)}"
    if isinstance(ty, TyVec):
        return f"vec_{_mangle_typeexpr(ty.elem)}"
    if isinstance(ty, TyErrorUnion):
        ename = _mangle_typeexpr(ty.error_set) if ty.error_set else "inferred"
        return f"{ename}_{_mangle_typeexpr(ty.payload)}"
    if isinstance(ty, TyErrorSetUnion):
        return "__".join(_mangle_typeexpr(m) for m in ty.members)
    if isinstance(ty, TyTuple):
        parts = []
        for idx, (name, field_ty) in enumerate(ty.fields):
            field_name = name or str(idx)
            parts.append(f"{field_name}_{_mangle_typeexpr(field_ty)}")
        return "tuple_" + ("__".join(parts) if parts else "empty")
    if isinstance(ty, TyUnitful):   return _mangle_typeexpr(ty.inner)
    return "t"

def _is_fp(ty_str: str) -> bool:
    return ty_str in ("float", "double")

def _zero(ty_str: str) -> str:
    if ty_str in ("float", "double"): return "0.0"
    if ty_str == "int":               return "0"
    if "mesa_str" in ty_str:          return '{"", 0}'
    if "*" in ty_str:                 return "NULL"
    return "0"


# ══════════════════════════════════════════════════════════════
# Code writer
# ══════════════════════════════════════════════════════════════

class Writer:
    def __init__(self):
        self._lines: List[str] = []
        self._indent = 0

    def line(self, s: str = ""):
        if s:
            self._lines.append("    " * self._indent + s)
        else:
            self._lines.append("")

    def indent(self):   self._indent += 1
    def dedent(self):   self._indent -= 1

    def output(self) -> str:
        return "\n".join(self._lines) + "\n"


# ══════════════════════════════════════════════════════════════
# C Codegen
# ══════════════════════════════════════════════════════════════

class CCodegen:
    def __init__(self, env: Environment, layout: LayoutPass):
        global _ACTIVE_ENV
        _ACTIVE_ENV = env
        self.env    = env
        self.layout = layout
        self.w      = Writer()

        self._closures:  List[str] = []   # forward declarations  
        self._fn_typedefs: dict = {}      # TyFun hash → typedef name

        self._opt_types: Set[str]  = set()  # optional types emitted
        self._error_object_types: Set[str] = set()
        self._vtables_emitted:  set = set()  # (concrete, iface) pairs already emitted
        self._result_types:     set = set()  # "EName_TMangle" result structs emitted
        self._mono_emitted:     set = set()  # "fn_name__bindings" monomorphised fns emitted
        self._pending_mono: list = []         # (FunDecl, bindings_dict) to emit
        self._mono_bindings: dict = {}        # active monomorphisation bindings
        self._allocator_stack: List[Tuple[str, Type]] = []
        self._active_alloc  = None             # (expr_str, alloc_ty) inside with block
        self._cleanup_frames: List[dict] = []  # active cleanup-bearing with frames
        self._block_depth = 0
        self._loop_stack: List[Optional[str]] = []
        self._try_targets: List[Tuple[str, str, Optional[TErrorSet], Optional[dict]]] = []  # (label, err_target, err_type, excluded_cleanup_frame)
        self._toplevel_w = None              # top-level writer, set in emit_all
        self._vec_types: Set[str]  = set()  # vec types emitted
        self._tup_types: Dict[int, str] = {}  # tuple id → C typedef
        self._ptr_params: Set[str] = set()   # names that are pointer types
        self._allocctx_helpers_emitted: Set[str] = set()
        self._current_fn_ret_ty = None       # resolved Type of current function return
        self._current_receiver: Optional[str] = None  # struct name being emitted
        self._current_fn_ret = None                   # return TypeExpr of current function
        self._current_fn_handle_target = None         # (label, err_target, err_type, excluded_cleanup_frame)
        self._counter   = 0
        self._assume_shared_support = False

    def _c_decl_name(self, decl, fallback: Optional[str] = None) -> str:
        return getattr(decl, "_c_name", None) or fallback or getattr(decl, "name", "")

    def _decl_pkg_path(self, decl, fallback: Optional[str] = None) -> Optional[str]:
        return getattr(decl, "_pkg_path", None) or fallback

    def _c_type_name(self, name: str) -> str:
        return self.env.lookup_c_type_name(name) or name

    def _error_tag_name(self, owner_ty, variant: str) -> str:
        return f"Mesa_{_mangle_type(owner_ty)}_{variant}"

    def _iface_type_name(self, iface) -> str:
        return _iface_c_name(iface)

    def _iface_box_name(self, iface) -> str:
        return f"Mesa_{self._iface_type_name(iface)}"

    def _iface_any_name(self, iface) -> str:
        return f"Mesa_any_{self._iface_type_name(iface)}"

    def _iface_vtable_name(self, iface) -> str:
        return f"Mesa_{self._iface_type_name(iface)}_vtable"

    def _iface_method_is_object_safe(self, mty: TFun) -> bool:
        if isinstance(mty.ret, TVar):
            return False
        extra_params = list(mty.params[1:]) if mty.params else []
        return not any(isinstance(param_ty, TVar) for param_ty in extra_params)

    def _fresh(self, hint: str = "t") -> str:
        n = self._counter; self._counter += 1
        return f"_m_{hint}{n}"

    def _qualified_name(self, expr: Expr) -> Optional[str]:
        if isinstance(expr, Ident):
            return expr.name
        if isinstance(expr, FieldExpr):
            left = self._qualified_name(expr.obj)
            if left is None:
                return None
            return f"{left}.{expr.field}"
        return None

    def _all_error_sets(self) -> List[Type]:
        sets: List[Type] = [
            ty for ty in getattr(self.env, "_types", {}).values()
            if isinstance(ty, (TErrorSet, TErrorSetUnion))
        ]
        for pkg_types in getattr(self.env, "_pkg_types", {}).values():
            for ty in pkg_types.values():
                if isinstance(ty, (TErrorSet, TErrorSetUnion)):
                    sets.append(ty)
        seen = set()
        unique: List[Type] = []
        for eset in sorted(sets, key=_error_key):
            key = _error_key(eset)
            if key in seen:
                continue
            seen.add(key)
            unique.append(eset)
        return unique

    def _all_base_error_sets(self) -> List[TErrorSet]:
        sets: List[TErrorSet] = [
            ty for ty in getattr(self.env, "_types", {}).values()
            if isinstance(ty, TErrorSet)
        ]
        for pkg_types in getattr(self.env, "_pkg_types", {}).values():
            for ty in pkg_types.values():
                if isinstance(ty, TErrorSet):
                    sets.append(ty)
        seen = set()
        unique: List[TErrorSet] = []
        for eset in sorted(sets, key=_error_key):
            key = _error_key(eset)
            if key in seen:
                continue
            seen.add(key)
            unique.append(eset)
        return unique

    def _max_error_payload_size(self, eset_ty: Optional[Type]) -> int:
        max_payload = 0
        sets = [eset_ty] if isinstance(eset_ty, (TErrorSet, TErrorSetUnion)) else self._all_error_sets()
        for eset in sets:
            for vty in error_set_variants(eset).values():
                if vty is None:
                    continue
                try:
                    lo = self.layout.layout_of(vty)
                    max_payload = max(max_payload, lo.size)
                except Exception:
                    max_payload = max(max_payload, 16)
        return max(max_payload, 8)

    def _emit_error_object_types(self):
        w = self.w
        for eset in self._all_error_sets():
            self._emit_error_object_type(eset)
        if self._all_error_sets():
            w.line()

    def _emit_error_object_type(self, eset: Type):
        key = _mangle_type(eset)
        if key in self._error_object_types:
            return
        self._error_object_types.add(key)
        if self._assume_shared_support:
            return
        payload_size = self._max_error_payload_size(eset)
        self.w.line(f"typedef struct {{")
        self.w.indent()
        self.w.line("uint16_t tag;")
        self.w.line(f"char payload[{payload_size}];")
        self.w.dedent()
        self.w.line(f"}} Mesa_error_{key};")
        self.w.line(f"typedef Mesa_error_{key} {key};")

    def _emit_error_tag_constants(self):
        w = self.w
        next_tag = 1
        for eset in self._all_base_error_sets():
            for vname in eset.variants:
                w.line(f"#define {self._error_tag_name(eset, vname)} {next_tag}")
                next_tag += 1
        if next_tag > 1:
            w.line()

    def _emit_cleanup_call(self, frame: dict, *, error_exit: bool = False):
        if frame.get("emitting"):
            return
        kind = frame.get("kind", "with")
        if kind == "defer":
            if frame.get("error_only") and not error_exit:
                return
            frame["emitting"] = True
            try:
                self._emit_block_body(frame["body"])
            finally:
                frame["emitting"] = False
            return
        if kind == "allocctx":
            self.w.line("mesa_allocctx_pop();")
            return
        if kind == "gc_root":
            self.w.line("mesa_gc_pop();")
            return
        cleanup = frame.get("cleanup")
        if not cleanup:
            return
        alloc_name = frame["alloc_name"]
        alloc_ty = frame["alloc_ty"]
        from src.types import TStruct, TPointer
        target_ty = alloc_ty.inner if isinstance(alloc_ty, TPointer) else alloc_ty
        if isinstance(target_ty, TStruct):
            self.w.line(f"{c_type(target_ty)}__{cleanup}(&{alloc_name});")

    def _alloc_bytes_expr_for(self, alloc_entry, size_expr: str, align: int) -> str:
        if alloc_entry:
            if isinstance(alloc_entry, str):
                return f"mesa_allocctx_alloc({alloc_entry}, {size_expr}, {align})"
            alloc_expr, alloc_ty = alloc_entry
            from src.types import TStruct, TPointer
            target_ty = alloc_ty.inner if isinstance(alloc_ty, TPointer) else alloc_ty
            if isinstance(target_ty, TStruct) and self.env.impls.implements(target_ty.name, "Allocator"):
                return f"{c_type(target_ty)}__alloc(&{alloc_expr}, {size_expr}, {align})"
        return f"mesa_c_alloc({size_expr}, {align})"

    def _emit_cleanups_for_exit(self, *,
                                min_loop_depth: Optional[int] = None,
                                error_exit: bool = False,
                                exclude_frame: Optional[dict] = None):
        for frame in reversed(self._cleanup_frames):
            if frame.get("emitting"):
                continue
            if exclude_frame is frame:
                continue
            if min_loop_depth is not None and frame["loop_depth"] < min_loop_depth:
                continue
            self._emit_cleanup_call(frame, error_exit=error_exit)

    def _emit_block_fallthrough_cleanups(self, block_depth: int):
        for frame in reversed(self._cleanup_frames):
            if frame.get("emitting"):
                continue
            if frame.get("kind") not in ("defer", "gc_root"):
                continue
            if frame.get("block_depth") != block_depth:
                continue
            self._emit_cleanup_call(frame, error_exit=False)

    def _prune_block_cleanup_frames(self, block_depth: int):
        self._cleanup_frames = [
            frame for frame in self._cleanup_frames
            if not (
                frame.get("kind") in ("defer", "gc_root")
                and frame.get("block_depth") == block_depth
            )
        ]

    def _allocctx_helper_name(self, alloc_ty: Type) -> Optional[str]:
        from src.types import TStruct, TPointer

        target_ty = alloc_ty.inner if isinstance(alloc_ty, TPointer) else alloc_ty
        if not isinstance(target_ty, TStruct):
            return None
        if not self.env.impls.implements(target_ty.name, "Allocator"):
            return None
        return f"_mesa_allocctx_alloc_{_mangle_type(target_ty)}"

    def _emit_allocctx_helpers(self):
        seen: Set[str] = set()
        struct_types: List[TStruct] = []
        struct_types.extend(
            ty for ty in getattr(self.env, "_types", {}).values()
            if isinstance(ty, TStruct)
        )
        for pkg_types in getattr(self.env, "_pkg_types", {}).values():
            struct_types.extend(ty for ty in pkg_types.values() if isinstance(ty, TStruct))
        for ty in struct_types:
            if not self.env.impls.implements(ty.name, "Allocator"):
                continue
            helper_name = self._allocctx_helper_name(ty)
            if helper_name is None or helper_name in seen:
                continue
            seen.add(helper_name)
            target_c = c_type(ty)
            self.w.line(f"static void* {helper_name}(void* self, size_t size, size_t align) {{")
            self.w.line(f"    return {target_c}__alloc(({target_c}*)self, size, align);")
            self.w.line("}")
            self.w.line()

    def _emit_gc_root_push(self, root_names: List[str], *, block_depth: int):
        if not root_names:
            return
        roots_name = self._fresh("gc_roots")
        frame_name = self._fresh("gc_frame")
        roots_init = ", ".join(f"(void**)&{name}" for name in root_names)
        self.w.line(f"void** {roots_name}[] = {{{roots_init}}};")
        self.w.line(f"Mesa_GC_Frame {frame_name};")
        self.w.line(f"mesa_gc_push(&{frame_name}, {roots_name}, {len(root_names)});")
        self._cleanup_frames.append({
            "kind": "gc_root",
            "loop_depth": len(self._loop_stack),
            "block_depth": block_depth,
        })

    def _emit_block_body(self, block: Optional[Block], *,
                         is_fn_body: bool = False,
                         assign_target: Optional[str] = None,
                         assign_target_ty: Optional[Type] = None):
        if block is None:
            return
        saved_depth = self._block_depth
        self._block_depth += 1
        cur_depth = self._block_depth
        for stmt in block.stmts:
            self._emit_stmt(stmt)
        if block.tail is not None:
            if is_fn_body:
                ret_ty = self._current_fn_ret_ty
                returns_value = (
                    ret_ty is not None and
                    not isinstance(ret_ty, TVoid) and
                    not (isinstance(ret_ty, TErrorUnion) and isinstance(ret_ty.payload, TVoid))
                )
                if returns_value:
                    self._emit_return_value(block.tail)
                else:
                    tail_c = self._expr(block.tail)
                    if tail_c and tail_c not in ("/* void */", "/* undef */") and not tail_c.startswith("_m_"):
                        self.w.line(f"{tail_c};")
            elif assign_target is not None:
                tail_c = self._expr(block.tail)
                if tail_c and tail_c not in ("/* void */", "/* undef */"):
                    tail_ty = self._expr_type(block.tail)
                    if (
                        isinstance(assign_target_ty, TErrorUnion) and
                        isinstance(tail_ty, TErrorUnion) and
                        assign_target_ty.error_set is None and
                        tail_ty.error_set is not None
                    ):
                        tmp = self._fresh("assign_erru")
                        self.w.line(f"{c_type(tail_ty)} {tmp} = {tail_c};")
                        self._emit_anyerror_assign_from_result(assign_target, tmp, tail_ty, assign_target_ty)
                    else:
                        tail_c = self._coerce_expr_for_target_type(tail_c, block.tail, assign_target_ty)
                        self.w.line(f"{assign_target} = {tail_c};")
            else:
                tail_c = self._expr(block.tail)
                if tail_c and tail_c not in ("/* void */", "/* undef */"):
                    if not tail_c.startswith("_m_"):
                        self.w.line(f"{tail_c};")
        self._emit_block_fallthrough_cleanups(cur_depth)
        self._prune_block_cleanup_frames(cur_depth)
        self._block_depth = saved_depth

    def _target_loop_depth(self, label: Optional[str]) -> Optional[int]:
        if not self._loop_stack:
            return None
        if label is None:
            return len(self._loop_stack)
        for i in range(len(self._loop_stack) - 1, -1, -1):
            if self._loop_stack[i] == label:
                return i + 1
        return None

    # ══════════════════════════════════════════════════════════
    # Top-level emission
    # ══════════════════════════════════════════════════════════

    def emit_all(self, program: Program):
        self._emit_preamble()
        self._emit_type_decls(program)
        self._toplevel_w = self.w  # save for deferred typedef emission
        # Pre-scan for all generic instantiations so fwd decls appear before main
        self._prescan_mono(program)
        self._emit_forward_decls(program)
        self._emit_allocctx_helpers()
        # Emit mono forward decls after regular fwd decls
        seen_fwd = set()
        for fn_decl, bindings in self._pending_mono:
            suffix = self._mono_suffix(bindings)
            key = f"{getattr(fn_decl, '_c_name', None) or fn_decl.name}__{suffix}"
            if key not in seen_fwd:
                seen_fwd.add(key)
                self._emit_mono_fwd(fn_decl, bindings)
        self._emit_decls(program)
        # Emit mono bodies after all regular function bodies
        seen_body = set()
        for fn_decl, bindings in self._pending_mono:
            suffix = self._mono_suffix(bindings)
            key = f"{getattr(fn_decl, '_c_name', None) or fn_decl.name}__{suffix}"
            if key not in seen_body:
                seen_body.add(key)
                self._mono_emitted.discard(key)
                self._emit_mono_fn(fn_decl, bindings)

    def emit_support_header(self, program: Program, header_guard: str = "MESA_GENERATED_SHARED_H"):
        self.w.line(f"#ifndef {header_guard}")
        self.w.line(f"#define {header_guard}")
        self.w.line()
        self._emit_preamble(extern_runtime_state=True)
        self._emit_type_decls(program)
        self._toplevel_w = self.w
        self._prescan_mono(program)
        self._emit_forward_decls(program)
        self._emit_allocctx_helpers()
        seen_fwd = set()
        for fn_decl, bindings in self._pending_mono:
            suffix = self._mono_suffix(bindings)
            key = f"{getattr(fn_decl, '_c_name', None) or fn_decl.name}__{suffix}"
            if key not in seen_fwd:
                seen_fwd.add(key)
                self.env.set_current_pkg(getattr(fn_decl, "_pkg_path", None))
                self._emit_mono_fwd(fn_decl, bindings)
        self.env.set_current_pkg(None)
        self.w.line(f"#endif /* {header_guard} */")

    def emit_runtime_state_source(self, header_name: str):
        self.w.line(f'#include "{header_name}"')
        self.w.line()
        self.w.line("void mesa__std__io__stdout_write(mesa_str text) {")
        self.w.line("    mesa_stdout_write(text);")
        self.w.line("}")
        self.w.line()
        self.w.line("void mesa__std__io__stderr_write(mesa_str text) {")
        self.w.line("    mesa_stderr_write(text);")
        self.w.line("}")
        self.w.line()
        self.w.line("static int64_t _mesa_test_total = 0;")
        self.w.line("static int64_t _mesa_test_failed = 0;")
        self.w.line("static int64_t _mesa_test_current_failures = 0;")
        self.w.line("static const char* _mesa_test_current_name = NULL;")
        self.w.line()
        self.w.line("void mesa_test_begin(const char* name) {")
        self.w.line("    _mesa_test_current_name = name;")
        self.w.line("    _mesa_test_current_failures = 0;")
        self.w.line("    _mesa_test_total++;")
        self.w.line('    printf("test %s\\n", name);')
        self.w.line("}")
        self.w.line()
        self.w.line("void mesa_test_assert(int cond, int64_t line, int64_t col) {")
        self.w.line("    if (cond) return;")
        self.w.line("    _mesa_test_current_failures++;")
        self.w.line('    printf("  fail at %lld:%lld\\n", (long long)line, (long long)col);')
        self.w.line("}")
        self.w.line()
        self.w.line("void mesa_test_end(void) {")
        self.w.line("    if (_mesa_test_current_failures == 0) {")
        self.w.line('        printf("  ok\\n");')
        self.w.line("    } else {")
        self.w.line("        _mesa_test_failed++;")
        self.w.line('        printf("  failed (%lld assertion%s)\\n",')
        self.w.line('               (long long)_mesa_test_current_failures,')
        self.w.line('               _mesa_test_current_failures == 1 ? "" : "s");')
        self.w.line("    }")
        self.w.line("    _mesa_test_current_name = NULL;")
        self.w.line("}")
        self.w.line()
        self.w.line("int mesa_test_finish(void) {")
        self.w.line("    int64_t passed = _mesa_test_total - _mesa_test_failed;")
        self.w.line('    printf("\\nresult: %lld passed; %lld failed\\n",')
        self.w.line('           (long long)passed, (long long)_mesa_test_failed);')
        self.w.line("    return _mesa_test_failed == 0 ? 0 : 1;")
        self.w.line("}")

    def emit_unit_source(self, program: Program, header_name: str, pending_mono=None):
        self.w.line(f'#include "{header_name}"')
        self.w.line()
        self._pending_mono = list(pending_mono or [])
        self._assume_shared_support = True
        self._toplevel_w = self.w
        self._prescan_mono(program)
        seen_fwd = set()
        for fn_decl, bindings in self._pending_mono:
            suffix = self._mono_suffix(bindings)
            key = f"{getattr(fn_decl, '_c_name', None) or fn_decl.name}__{suffix}"
            if key not in seen_fwd:
                seen_fwd.add(key)
                self._emit_mono_fwd(fn_decl, bindings)
        if seen_fwd:
            self.w.line()
        self._emit_decls(program)
        seen_body = set()
        unit_sources = {
            getattr(decl, "_source_file", None)
            for decl in program.decls
            if getattr(decl, "_source_file", None)
        }
        for fn_decl, bindings in self._pending_mono:
            if unit_sources and getattr(fn_decl, "_source_file", None) not in unit_sources:
                continue
            suffix = self._mono_suffix(bindings)
            key = f"{getattr(fn_decl, '_c_name', None) or fn_decl.name}__{suffix}"
            if key not in seen_body:
                seen_body.add(key)
                self._mono_emitted.discard(key)
                self.env.set_current_pkg(getattr(fn_decl, "_pkg_path", None))
                self._emit_mono_fn(fn_decl, bindings)
        self.env.set_current_pkg(None)
        self._assume_shared_support = False

    def _emit_preamble(self, extern_runtime_state: bool = False):
        w = self.w
        w.line("/* Mesa generated C — do not edit */")
        w.line("#include <stdint.h>")
        w.line("#include <stdio.h>")
        w.line("#include <math.h>")
        w.line("#include <string.h>")
        w.line("#include <stdlib.h>")
        w.line("#include <stddef.h>  /* offsetof */")
        w.line("#include <errno.h>")
        w.line("#include <sys/mman.h>")
        w.line("#include <unistd.h>")
        w.line('#include "mesa_gc_runtime.h"')
        w.line()
        # Mesa runtime types
        w.line("/* ── Mesa runtime types ─────────────────────────── */")
        w.line("typedef struct { const char* data; int64_t len; } mesa_str;")
        w.line("typedef struct {")
        w.line("    mesa_str code;")
        w.line("    mesa_str message;")
        w.line("    mesa_str hint;")
        w.line("    int64_t line;")
        w.line("    int64_t col;")
        w.line("} mesa_test_diagnostic;")
        w.line("typedef struct { mesa_test_diagnostic* data; int64_t len; int64_t cap; } mesa_vec_mesa_test_diagnostic;")
        w.line("typedef struct { int ok; mesa_vec_mesa_test_diagnostic errors; } mesa_test_compile_result;")
        w.line("/* Dynamic unitful: float`?` */")
        w.line("typedef struct {")
        w.line("    double value;")
        w.line("    int    dims[7];   /* kg m s A K mol cd exponents */")
        w.line("    double scale;     /* scale factor vs SI base unit */")
        w.line("    const char* name; /* canonical unit name, e.g. N */")
        w.line("} mesa_unitful;")
        w.line()
        # println built-ins
        w.line("/* ── Built-ins ───────────────────────────────────── */")
        w.line("static inline void mesa_stdout_write(mesa_str s) { printf(\"%.*s\", (int)s.len, s.data); }")
        w.line("static inline void mesa_stderr_write(mesa_str s) { fprintf(stderr, \"%.*s\", (int)s.len, s.data); }")
        w.line("static inline void mesa_println_str(mesa_str s) { printf(\"%.*s\\n\", (int)s.len, s.data); }")
        w.line("static inline void mesa_println_i64(int64_t n)  { printf(\"%lld\\n\", (long long)n); }")
        w.line("static inline void mesa_println_f64(double f)   { printf(\"%.6g\\n\", f); }")
        w.line("static inline void mesa_println_bool(int b)     { printf(\"%s\\n\", b ? \"true\" : \"false\"); }")
        w.line("static inline void mesa_println_cstr(const char* s) { printf(\"%s\\n\", s); }")
        w.line("static inline void mesa_panic(mesa_str s) { fprintf(stderr, \"%.*s\\n\", (int)s.len, s.data); abort(); }")
        w.line("void mesa_test_begin(const char* name);")
        w.line("void mesa_test_assert(int cond, int64_t line, int64_t col);")
        w.line("void mesa_test_end(void);")
        w.line("int mesa_test_finish(void);")
        w.line("static inline int64_t mesa_page_size(void) {")
        w.line("    long page = sysconf(_SC_PAGESIZE);")
        w.line("    return (int64_t)(page > 0 ? page : 4096);")
        w.line("}")
        w.line("static inline void* mesa_page_alloc(int64_t size) {")
        w.line("    size_t actual = (size_t)(size > 0 ? size : mesa_page_size());")
        w.line("    void* ptr = mmap(NULL, actual, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);")
        w.line("    if (ptr == MAP_FAILED) { perror(\"mmap\"); abort(); }")
        w.line("    return ptr;")
        w.line("}")
        w.line("static inline void mesa_page_free(void* ptr, int64_t size) {")
        w.line("    if (!ptr) return;")
        w.line("    size_t actual = (size_t)(size > 0 ? size : mesa_page_size());")
        w.line("    if (munmap(ptr, actual) != 0) { perror(\"munmap\"); abort(); }")
        w.line("}")
        w.line("static inline void* mesa_c_alloc(int64_t size, int64_t align) {")
        w.line("    size_t actual_size = (size_t)(size > 0 ? size : 1);")
        w.line("    size_t actual_align = (size_t)(align > (int64_t)sizeof(void*) ? align : (int64_t)sizeof(void*));")
        w.line("    void* ptr = NULL;")
        w.line("    if (posix_memalign(&ptr, actual_align, actual_size) != 0) {")
        w.line("        fprintf(stderr, \"posix_memalign failed\\n\");")
        w.line("        abort();")
        w.line("    }")
        w.line("    return ptr;")
        w.line("}")
        w.line("static inline void* mesa_c_realloc(void* ptr, int64_t old_size, int64_t new_size, int64_t align) {")
        w.line("    void* next = mesa_c_alloc(new_size, align);")
        w.line("    if (ptr && old_size > 0) memcpy(next, ptr, (size_t)(old_size < new_size ? old_size : new_size));")
        w.line("    free(ptr);")
        w.line("    return next;")
        w.line("}")
        w.line("static inline void mesa_c_free(void* ptr) { free(ptr); }")
        w.line("static inline void* mesa_ptr_add(void* ptr, int64_t offset) { return (void*)(((char*)ptr) + offset); }")
        w.line("static inline int  mesa_str_eq(mesa_str a, mesa_str b) {")
        w.line("    return a.len == b.len && (a.len == 0 || memcmp(a.data, b.data, (size_t)a.len) == 0);")
        w.line("}")
        w.line()

    def _emit_type_decls(self, program: Program):
        w = self.w
        w.line("/* ── Type declarations ───────────────────────────── */")

        # Forward declare all named struct/union types before helper typedefs
        # so vec/slice/optional helpers can legally reference them.
        for decl in program.decls:
            self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
            if isinstance(decl, (StructDecl, OpaqueTypeDecl)):
                c_name = self._c_decl_name(decl, decl.name)
                w.line(f"typedef struct {c_name} {c_name};")
            elif isinstance(decl, UnionDecl):
                ty = self.env.lookup_type(decl.name)
                if isinstance(ty, TUnion) and any(vty is not None for vty in ty.variants.values()):
                    c_name = self._c_decl_name(decl, decl.name)
                    w.line(f"typedef struct {c_name} {c_name};")
        w.line()

        # Emit optional typedefs first (needed before function signatures)
        self._emit_tuple_typedefs(program)
        self._emit_optional_typedefs(program)
        self._emit_uncertain_typedefs(program)
        self._emit_error_tag_constants()
        self._emit_error_object_types()

        # Emit interface vtable and fat-pointer structs
        self._emit_interface_types(program)
        # Pre-emit result structs for all error-returning functions
        self._emit_all_result_structs(program)

        # Type aliases: let Score := i64  →  typedef int64_t Score;
        from src.ast import TypeAlias
        for decl in program.decls:
            self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
            if isinstance(decl, TypeAlias):
                alias_ty = self.env.lookup_type(decl.name)
                if isinstance(alias_ty, (TErrorSet, TErrorSetUnion)):
                    continue
                alias_c = self._c_decl_name(decl, decl.name)
                if isinstance(alias_ty, TFun):
                    params_c = ", ".join(c_type(param) for param in alias_ty.params) or "void"
                    w.line(f"typedef {c_type(alias_ty.ret)} (*{alias_c})({params_c});")
                    continue
                target_c = c_type(alias_ty) if alias_ty is not None else c_typeexpr(decl.type_)
                if target_c == alias_c and alias_ty is not None:
                    target_c = c_type(alias_ty)
                if target_c != alias_c:
                    w.line(f"typedef {target_c} {alias_c};")

        # Full definitions
        for decl in program.decls:
            self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
            if isinstance(decl, StructDecl):
                self._emit_struct_type(decl)
            elif isinstance(decl, UnionDecl):
                self._emit_union_type(decl)
        self._emit_deferred_helper_types()
        self.env.set_current_pkg(None)

    def _type_requires_complete_definition(self, ty: Type) -> bool:
        if isinstance(ty, TStruct) and getattr(ty, "opaque", False):
            return False
        return isinstance(ty, (TStruct, TUnion, TTuple))

    def _defer_optional_type(self, inner_ty: str, mangle: str):
        if not hasattr(self, "_deferred_optional_types"):
            self._deferred_optional_types = []
            self._deferred_optional_keys = set()
        if mangle in self._deferred_optional_keys or mangle in self._opt_types:
            return
        self._deferred_optional_keys.add(mangle)
        self._deferred_optional_types.append((inner_ty, mangle))

    def _defer_uncertain_type(self, inner_ty: str, mangle: str):
        if not hasattr(self, "_deferred_uncertain_types"):
            self._deferred_uncertain_types = []
            self._deferred_uncertain_keys = set()
        if mangle in self._deferred_uncertain_keys or mangle in getattr(self, "_uncertain_types", set()):
            return
        self._deferred_uncertain_keys.add(mangle)
        self._deferred_uncertain_types.append((inner_ty, mangle))

    def _defer_vec_type(self, inner_ty: str, mangle: str):
        if not hasattr(self, "_deferred_vec_types"):
            self._deferred_vec_types = []
            self._deferred_vec_keys = set()
        if mangle in self._deferred_vec_keys or mangle in self._vec_types:
            return
        self._deferred_vec_keys.add(mangle)
        self._deferred_vec_types.append((inner_ty, mangle))

    def _emit_deferred_helper_types(self):
        for fields, c_name in getattr(self, "_deferred_tuple_types", []):
            self._emit_tuple_type(fields, c_name)
        for inner_ty, mangle in getattr(self, "_deferred_optional_types", []):
            self._emit_optional_type(inner_ty, mangle)
        for inner_ty, mangle in getattr(self, "_deferred_uncertain_types", []):
            self._emit_uncertain_type(inner_ty, mangle)
        for inner_ty, mangle in getattr(self, "_deferred_vec_types", []):
            self._emit_vec_type(inner_ty, mangle)

    def _defer_tuple_type(self, ty: TTuple):
        if not hasattr(self, "_deferred_tuple_types"):
            self._deferred_tuple_types = []
            self._deferred_tuple_names = set()
        c_name = c_type(ty)
        if c_name in self._deferred_tuple_names or c_name in getattr(self, "_tuple_types", set()):
            return
        self._deferred_tuple_names.add(c_name)
        self._deferred_tuple_types.append((list(ty.fields), c_name))

    def _emit_tuple_typedefs(self, program: Program):
        def emit_for_type(ty):
            if ty is None:
                return
            if isinstance(ty, TTuple):
                c_name = c_type(ty)
                if any(self._type_requires_complete_definition(field_ty) for _, field_ty in ty.fields):
                    self._defer_tuple_type(ty)
                else:
                    self._emit_tuple_type(list(ty.fields), c_name)
                for _, field_ty in ty.fields:
                    emit_for_type(field_ty)
            elif isinstance(ty, TOptional):
                emit_for_type(ty.inner)
            elif isinstance(ty, TVec):
                emit_for_type(ty.inner)
            elif isinstance(ty, TStruct):
                for field_ty in ty.fields.values():
                    emit_for_type(field_ty)
            elif isinstance(ty, TUnion):
                for field_ty in ty.variants.values():
                    emit_for_type(field_ty)

        def scan_expr(expr):
            if expr is None:
                return
            emit_for_type(getattr(expr, "_resolved_type", None))
            for attr in ('left','right','operand','obj','callee','value','init','expr',
                         'cond','then_block','else_block','tail','body','handle','block'):
                child = getattr(expr, attr, None)
                if child and hasattr(child, '__dict__'):
                    scan_expr(child)
            for attr in ('stmts','args','elems','fields','arms','params'):
                for child in getattr(expr, attr, []):
                    if hasattr(child, '__dict__'):
                        scan_expr(child)
                    cv = getattr(child, 'value', None)
                    if cv and hasattr(cv, '__dict__'):
                        scan_expr(cv)

        def scan_block(block):
            if block is None:
                return
            for stmt in block.stmts:
                scan_expr(stmt)
            scan_expr(block.tail)

        for decl in program.decls:
            self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
            if isinstance(decl, FunDecl):
                fn_sym = self.env.lookup(decl.name)
                emit_for_type(getattr(fn_sym, "type_", None))
                scan_block(decl.body)
            elif isinstance(decl, StructDecl):
                for field in decl.fields:
                    emit_for_type(self.env.lookup_type(field.type_.name) if isinstance(field.type_, TyNamed) else None)
                for method in decl.methods:
                    scan_block(method.body)
            elif isinstance(decl, DefDecl):
                for method in decl.methods:
                    scan_block(method.body)
        self.env.set_current_pkg(None)

    def _emit_tuple_type(self, fields: List[Tuple[Optional[str], Type]], c_name: str):
        if not hasattr(self, "_tuple_types"):
            self._tuple_types = set()
        if c_name in self._tuple_types:
            return
        self._tuple_types.add(c_name)
        if self._assume_shared_support:
            return
        self.w.line(f"typedef struct {{")
        self.w.indent()
        for idx, (field_name, field_ty) in enumerate(fields):
            name = field_name or f"_{idx}"
            self.w.line(f"{c_type(field_ty)} {name};")
        self.w.dedent()
        self.w.line(f"}} {c_name};")

    def _mono_suffix(self, bindings: dict) -> str:
        """Turn {'T': i64, 'U': f64} into '__T_i64__U_f64'."""
        parts = []
        for k, v in sorted(bindings.items()):
            parts.append(f"{k}_{_mangle_type(v)}")
        return "__".join(parts)

    def _prescan_mono(self, program):
        """Walk all function bodies collecting generic call instantiations."""
        from src.ast import CallExpr as CE2, FieldExpr as FE2, Ident as ID2, FunDecl as FD2, Block as BK2
        from src.types import TPointer as _TPMono, TStruct as _TSMono
        def walk_expr(e):
            if e is None: return
            bindings = getattr(e, '_type_bindings', None)
            if bindings and isinstance(e, CE2):
                fd = None
                if isinstance(e.callee, ID2):
                    fn_sym = self.env.lookup(e.callee.name)
                    if fn_sym is None or not getattr(fn_sym, "decl_node", None):
                        fn_sym = self.env.lookup_any_pkg_symbol(e.callee.name)
                    if fn_sym and hasattr(fn_sym, 'decl_node') and fn_sym.decl_node:
                        fd = fn_sym.decl_node
                elif isinstance(e.callee, FE2):
                    recv_ty = self._expr_type(e.callee.obj)
                    if isinstance(recv_ty, _TPMono):
                        recv_ty = recv_ty.inner
                    if isinstance(recv_ty, _TSMono):
                        method_name = f"{recv_ty.name}.{e.callee.field}"
                        method_sym = self.env.lookup(method_name) or self.env.lookup_any_pkg_symbol(method_name)
                        if method_sym and hasattr(method_sym, 'decl_node') and method_sym.decl_node:
                            fd = method_sym.decl_node
                if fd and getattr(fd, '_type_params', []):
                    self._pending_mono.append((fd, bindings))
            elif isinstance(e, CE2) and isinstance(e.callee, FE2):
                recv_ty = self._expr_type(e.callee.obj)
                if isinstance(recv_ty, _TPMono):
                    recv_ty = recv_ty.inner
                if isinstance(recv_ty, _TSMono) and recv_ty.type_args:
                    method_name = f"{recv_ty.name}.{e.callee.field}"
                    method_sym = self.env.lookup(method_name) or self.env.lookup_any_pkg_symbol(method_name)
                    if method_sym and hasattr(method_sym, 'decl_node') and method_sym.decl_node:
                        fd = method_sym.decl_node
                        if getattr(fd, '_type_params', []):
                            self._pending_mono.append((fd, dict(recv_ty.type_args)))
            for attr in ('args', 'stmts', 'elems'):
                for item in getattr(e, attr, []) or []:
                    walk_expr(getattr(item, 'value', item))
            for attr in ('callee', 'obj', 'left', 'right', 'operand',
                         'condition', 'then_block', 'else_block', 'value', 'init', 'tail', 'body', 'expr'):
                walk_expr(getattr(e, attr, None))
        def walk_block(b):
            if b is None: return
            for s in (b.stmts or []):
                walk_expr(getattr(s, 'value', None))
                walk_expr(getattr(s, 'init', None))
                walk_expr(getattr(s, 'expr', None))
                walk_block(getattr(s, 'body', None))
                walk_block(getattr(s, 'else_block', None))
            walk_expr(b.tail)
        for decl in program.decls:
            self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
            if isinstance(decl, FD2):
                walk_block(decl.body)
        self.env.set_current_pkg(None)

    def _emit_mono_fwd(self, f, bindings: dict):
        """Emit forward declaration for a monomorphised function."""
        suffix = self._mono_suffix(bindings)
        base_name = getattr(f, "_c_name", None) or f.name
        key = f"{base_name}__{suffix}"
        from src.checker import effective_return_type as _ert7, lower_type as _lt7
        prev_pkg = getattr(self.env, "_current_pkg", None)
        prev_receiver = getattr(self.env, "_current_struct", None)
        self.env.set_current_pkg(getattr(f, "_pkg_path", None))
        self.env.set_current_struct(getattr(f, "_receiver", None))
        try:
            ret_ty = substitute(_ert7(f, self.env), bindings)
            ret_c  = c_type(ret_ty)
            params_c = []
            for p in f.params:
                pt = substitute(_lt7(p.type_, self.env), bindings)
                c_pt = c_type(pt)
                if isinstance(pt, TFun):
                    params_c.append(c_type(pt, p.name))
                else:
                    params_c.append(f"{c_pt} {p.name}")
            sig = f"{ret_c} {key}({', '.join(params_c) or 'void'})"
            self.w.line(sig + ";")
        finally:
            self.env.set_current_struct(prev_receiver)
            self.env.set_current_pkg(prev_pkg)

    def _emit_mono_fn(self, f, bindings: dict):
        """Emit a monomorphised version of generic function f with given bindings."""
        suffix = self._mono_suffix(bindings)
        base_name = getattr(f, "_c_name", None) or f.name
        key = f"{base_name}__{suffix}"
        if key in self._mono_emitted: return
        self._mono_emitted.add(key)
        prev_pkg = getattr(self.env, "_current_pkg", None)
        prev_receiver_struct = getattr(self.env, "_current_struct", None)
        receiver = getattr(f, "_receiver", None)
        self.env.set_current_pkg(getattr(f, "_pkg_path", None))
        self.env.set_current_struct(receiver)
        lookup_name = f"{receiver}.{f.name}" if receiver else f.name
        fn_sym = self.env.lookup(lookup_name) or self.env.lookup_pkg_symbol(getattr(f, "_pkg_path", None), lookup_name)

        # Build C signature with substituted types
        ret_ty = substitute(fn_sym.type_.ret, bindings) if fn_sym and isinstance(fn_sym.type_, TFun) else None
        if ret_ty is None:
            from src.checker import lower_type as _lt5
            ret_ty = substitute(_lt5(f.ret, self.env), bindings)
        ret_c = c_type(ret_ty) if ret_ty else "int64_t"

        params_c = []
        for p in f.params:
            from src.checker import lower_type as _lt6
            pt = _lt6(p.type_, self.env)
            pt = substitute(pt, bindings)
            c_pt = c_type(pt)
            if isinstance(pt, TFun):
                params_c.append(c_type(pt, p.name))
            else:
                params_c.append(f"{c_pt} {p.name}")

        w = self.w
        w.line(f"/* {f.name}[{', '.join(f'{k}={_mangle_type(v)}' for k,v in bindings.items())}] */")
        w.line(f"{ret_c} {key}({', '.join(params_c) or 'void'}) {{")
        w.indent()

        # Temporarily override type env for body emission
        saved_ptr_params = set(self._ptr_params)
        saved_receiver = self._current_receiver
        saved_fn_ret = self._current_fn_ret
        saved_fn_ret_ty = self._current_fn_ret_ty
        saved_fn_name = getattr(self, '_current_fn_name', None)
        saved_fn_handle = getattr(self, '_current_fn_has_handle', False)
        self._current_fn_ret = f.ret
        self._current_fn_ret_ty = ret_ty
        self._current_fn_name = key
        self._current_fn_has_handle = False
        self._current_receiver = receiver
        for p in f.params:
            if isinstance(p.type_, TyPointer):
                self._ptr_params.add(p.name)
        self._mono_bindings = bindings   # used by _c_type_for_tvar

        self._emit_block_stmts(f.body)
        if f.body and f.body.tail:
            tail_c = self._expr(f.body.tail)
            if tail_c and tail_c != "/* void */":
                self._emit_return_value(f.body.tail)

        self._mono_bindings = {}
        self._ptr_params = saved_ptr_params
        self._current_receiver = saved_receiver
        self._current_fn_ret = saved_fn_ret
        self._current_fn_ret_ty = saved_fn_ret_ty
        self._current_fn_name = saved_fn_name
        self._current_fn_has_handle = saved_fn_handle
        self.env.set_current_pkg(prev_pkg)
        self.env.set_current_struct(prev_receiver_struct)
        w.dedent()
        w.line("}")
        w.line()

    def _emit_all_result_structs(self, program: Program):
        """Pre-scan all functions and emit result structs in the type section."""
        from src.types import TErrorUnion
        from src.checker import effective_return_type as _ert3
        def scan_fn(f):
            ret = _ert3(f, self.env)
            if isinstance(ret, TErrorUnion):
                self._emit_result_struct(ret.error_set, ret.payload)
            def scan_expr(expr):
                if expr is None or not hasattr(expr, "__dict__"):
                    return
                ty = getattr(expr, "_resolved_type", None)
                if isinstance(ty, TErrorUnion):
                    self._emit_result_struct(ty.error_set, ty.payload)
                for attr in ("left", "right", "operand", "obj", "callee", "value",
                             "init", "expr", "cond", "then_block", "else_block",
                             "tail", "body", "handle", "block"):
                    child = getattr(expr, attr, None)
                    if child is not None:
                        scan_expr(child)
                for attr in ("stmts", "args", "elems", "fields", "arms", "params"):
                    for child in getattr(expr, attr, []) or []:
                        scan_expr(getattr(child, "value", child))
                        scan_expr(getattr(child, "body", None))
            if f.body:
                scan_expr(f.body)
        for decl in program.decls:
            self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
            if isinstance(decl, FunDecl): scan_fn(decl)
            elif isinstance(decl, StructDecl):
                for m in decl.methods: scan_fn(m)
            elif isinstance(decl, DefDecl):
                for m in decl.methods: scan_fn(m)
        self.env.set_current_pkg(None)

    def _emit_interface_types(self, program: Program):
        """Emit vtable struct and fat-pointer struct for each interface."""
        w = self.w
        for decl in program.decls:
            if not isinstance(decl, InterfaceDecl): continue
            self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
            iface_ty = self.env.lookup_type(decl.name)
            if not isinstance(iface_ty, TInterface): continue
            if _is_builtin_allocator_iface_name(iface_ty.name):
                continue

            iname = self._c_decl_name(decl, decl.name)
            # Vtable struct: one function pointer per interface method
            w.line(f"/* *{iname} — vtable and fat pointer */")
            w.line(f"typedef struct {self._iface_vtable_name(iface_ty)} {{")
            w.indent()
            for mname, mty in iface_ty.methods.items():
                if not self._iface_method_is_object_safe(mty):
                    continue
                # First param of method is self — replace with void* for vtable
                params = [c_type(p) for p in mty.params[1:]] if mty.params else []
                params_str = ", ".join(["void*"] + params) or "void*"
                ret_str = c_type(mty.ret)
                w.line(f"{ret_str} (*{mname})({params_str});")
            w.dedent()
            w.line(f"}} {self._iface_vtable_name(iface_ty)};")
            w.line()
            # *any Interface: heap fat pointer — vtable ptr + flexible data
            w.line(f"typedef struct {{")
            w.indent()
            w.line(f"const {self._iface_vtable_name(iface_ty)}* vtable;")
            w.line(f"char data[];   /* flexible array member */")
            w.dedent()
            w.line(f"}} {self._iface_box_name(iface_ty)};")
            w.line()
            # any Interface: stack existential — vtable + SBO union + flag
            w.line(f"typedef struct {{")
            w.indent()
            w.line(f"const {self._iface_vtable_name(iface_ty)}* vtable;")
            w.line(f"union {{")
            w.indent()
            w.line(f"char  inline_buf[16]; /* small types stored here */")
            w.line(f"void* heap_ptr;       /* large types: pointer to heap */")
            w.dedent()
            w.line(f"}};")
            w.line(f"int64_t is_inline;    /* 1 = inline_buf, 0 = heap_ptr */")
            w.dedent()
            w.line(f"}} {self._iface_any_name(iface_ty)};")
            w.line()
        self.env.set_current_pkg(None)

    def _emit_result_struct(self, eset_ty, payload_ty):
        """Emit typedef for Mesa_result_EName_TMangle if not already done."""
        ename    = _error_key(eset_ty)
        tmangle  = _mangle_type(payload_ty)
        key      = f"{ename}_{tmangle}"
        if key in self._result_types: return
        self._result_types.add(key)
        if self._assume_shared_support:
            return
        w = self.w
        if isinstance(payload_ty, TVec) and payload_ty.size is None:
            self._emit_vec_type(c_type(payload_ty.inner), _mangle_type(payload_ty.inner))
        if isinstance(payload_ty, TOptional):
            self._emit_optional_type(c_type(payload_ty.inner), _mangle_type(payload_ty.inner))
        payload_is_void = isinstance(payload_ty, TVoid)
        payload_c = c_type(payload_ty)
        max_payload = self._max_error_payload_size(eset_ty)
        # Emit the result struct
        w.line(f"typedef struct {{")
        w.indent()
        w.line(f"int is_err;")
        w.line(f"union {{")
        w.indent()
        if payload_is_void:
            w.line(f"char _void;")
        else:
            w.line(f"{payload_c} value;")
        w.line(f"struct {{")
        w.indent()
        w.line(f"uint16_t tag;")
        w.line(f"char payload[{max_payload}];")
        w.dedent()
        w.line(f"}} err;")
        w.dedent()
        w.line(f"}};")
        w.dedent()
        w.line(f"}} Mesa_result_{key};")
        w.line()

    def _emit_ok_error_union_return(self, ret_ty: TErrorUnion):
        eset = ret_ty.error_set
        self._emit_result_struct(eset, ret_ty.payload)
        key = f"{_error_key(eset)}_{_mangle_type(ret_ty.payload)}"
        self.w.line(f"return (Mesa_result_{key}){{.is_err=0}};")

    def _emit_anyerror_return_from_error_tag(self, tag_expr: str,
                                             payload_expr: Optional[str],
                                             payload_ty: Type):
        key = f"{_error_key(None)}_{_mangle_type(payload_ty)}"
        self._emit_result_struct(None, payload_ty)
        self.w.line(f"{{ Mesa_result_{key} _r = (Mesa_result_{key}){{0}};")
        self.w.line("  _r.is_err = 1;")
        self.w.line(f"  _r.err.tag = {tag_expr};")
        if payload_expr:
            self.w.line(f"  memcpy(_r.err.payload, &({payload_expr}), sizeof({payload_expr}));")
        self.w.line("  return _r; }")

    def _emit_anyerror_return_from_result(self, tmp: str,
                                          src_ty: TErrorUnion,
                                          dest_ty: TErrorUnion):
        key = f"{_error_key(None)}_{_mangle_type(dest_ty.payload)}"
        self._emit_result_struct(None, dest_ty.payload)
        self.w.line(f"{{ Mesa_result_{key} _r = (Mesa_result_{key}){{0}};")
        self.w.line(f"  if ({tmp}.is_err) {{")
        self.w.line("    _r.is_err = 1;")
        self.w.line(f"    _r.err.tag = {tmp}.err.tag;")
        self.w.line(f"    memcpy(_r.err.payload, {tmp}.err.payload, sizeof({tmp}.err.payload));")
        self.w.line("  } else {")
        self.w.line(f"    _r.is_err = 0; _r.value = {tmp}.value;")
        self.w.line("  }")
        self.w.line("  return _r; }")

    def _emit_anyerror_assign_from_result(self, target: str, tmp: str,
                                          src_ty: TErrorUnion,
                                          dest_ty: TErrorUnion):
        key = f"{_error_key(None)}_{_mangle_type(dest_ty.payload)}"
        self._emit_result_struct(None, dest_ty.payload)
        self.w.line(f"{{ Mesa_result_{key} _r = (Mesa_result_{key}){{0}};")
        self.w.line(f"  if ({tmp}.is_err) {{")
        self.w.line("    _r.is_err = 1;")
        self.w.line(f"    _r.err.tag = {tmp}.err.tag;")
        self.w.line(f"    memcpy(_r.err.payload, {tmp}.err.payload, sizeof({tmp}.err.payload));")
        self.w.line("  } else {")
        self.w.line(f"    _r.is_err = 0; _r.value = {tmp}.value;")
        self.w.line("  }")
        self.w.line(f"  {target} = _r; }}")

    def _emit_result_return_from_result(self, tmp: str,
                                        src_ty: TErrorUnion,
                                        dest_ty: TErrorUnion):
        key = f"{_error_key(dest_ty.error_set)}_{_mangle_type(dest_ty.payload)}"
        self._emit_result_struct(dest_ty.error_set, dest_ty.payload)
        self.w.line(f"{{ Mesa_result_{key} _r = (Mesa_result_{key}){{0}};")
        self.w.line(f"  if ({tmp}.is_err) {{")
        self.w.line("    _r.is_err = 1;")
        self.w.line(f"    _r.err.tag = {tmp}.err.tag;")
        self.w.line(f"    memcpy(_r.err.payload, {tmp}.err.payload, sizeof({tmp}.err.payload));")
        self.w.line("  } else {")
        self.w.line("    _r.is_err = 0;")
        if not isinstance(src_ty.payload, TVoid) and not isinstance(dest_ty.payload, TVoid):
            self.w.line(f"    _r.value = {tmp}.value;")
        self.w.line("  }")
        self.w.line("  return _r; }")

    def _emit_pattern_payload_bindings_from_bytes(self, pat: PatVariant,
                                                  payload_ty: Type,
                                                  payload_src: str):
        all_bindings = ([pat.binding] if pat.binding else []) + list(pat.extra_bindings or [])
        if not all_bindings:
            return
        if isinstance(payload_ty, TTuple) and pat.extra_bindings:
            tmp = self._fresh("pat_payload")
            ct = c_type(payload_ty)
            self.w.line(f"{ct} {tmp};")
            self.w.line(f"memcpy(&{tmp}, {payload_src}, sizeof({tmp}));")
            for idx, (bname, (_, bty)) in enumerate(zip(all_bindings, payload_ty.fields)):
                field = payload_ty.fields[idx][0] or f"_{idx}"
                self.w.line(f"{c_type(bty)} {bname} = {tmp}.{field};")
            return
        if pat.binding and not pat.extra_bindings:
            ct = c_type(payload_ty)
            self.w.line(f"{ct} {pat.binding};")
            self.w.line(f"memcpy(&{pat.binding}, {payload_src}, sizeof({pat.binding}));")
            return
        tmp = self._fresh("pat_payload")
        ct = c_type(payload_ty)
        self.w.line(f"{ct} {tmp};")
        self.w.line(f"memcpy(&{tmp}, {payload_src}, sizeof({tmp}));")
        for bname in all_bindings:
            self.w.line(f"{ct} {bname} = {tmp};")

    def _coerce_expr_for_target_type(self,
                                     rendered: str,
                                     value_expr: Expr,
                                     target_ty: Optional[Type]) -> str:
        if rendered in ("/* void */", "/* undef */") or target_ty is None:
            return rendered
        value_ty = self._expr_type_raw(value_expr) or self._expr_type(value_expr)
        from src.types import is_assignable
        if (
            isinstance(target_ty, TOptional)
            and value_ty is not None
            and value_ty != target_ty
            and is_assignable(value_ty, target_ty.inner)
        ):
            mangle = _mangle_type(target_ty.inner)
            return f"(mesa_opt_{mangle}){{{rendered}, 1}}"
        return rendered

    def _error_object_literal(self, eset: TErrorSet, tag_expr: str,
                              payload_expr: Optional[str] = None) -> str:
        err_c = c_type(eset)
        if payload_expr is None:
            return f"(({err_c}){{.tag = {tag_expr}}})"
        tmp = self._fresh("err")
        return (
            "({ "
            f"{err_c} {tmp} = ({err_c}){{0}}; "
            f"{tmp}.tag = {tag_expr}; "
            f"__auto_type _payload = ({payload_expr}); "
            f"memcpy({tmp}.payload, &_payload, sizeof(_payload)); "
            f"{tmp}; "
            "})"
        )

    def _emit_error_object_assign_from_result(self, target: str, eset: TErrorSet,
                                              err_src: str):
        self.w.line(f"{target}.tag = {err_src}.tag;")
        self.w.line(f"memcpy({target}.payload, {err_src}.payload, sizeof({target}.payload));")

    def _emit_struct_type(self, s: StructDecl):
        w = self.w
        c_name = self._c_decl_name(s, s.name)
        w.line(f"struct {c_name} {{")
        w.indent()
        ty = self.env.lookup_type(s.name)
        ptr_offsets = []
        if isinstance(ty, TStruct):
            offset = 0
            for fname, ftype in ty.fields.items():
                ct = c_type(ftype)
                # Track pointer field offsets for GC descriptor
                from src.types import TPointer as _TPgc, TDynInterface as _TDgc, TAnyInterface as _TAgc
                if isinstance(ftype, (_TPgc, _TDgc, _TAgc)):
                    ptr_offsets.append(f"offsetof({c_name}, {fname})")
                if isinstance(ftype, TArray):
                    w.line(f"{c_type(ftype.inner)} {fname}[{ftype.size}];")
                else:
                    w.line(f"{ct} {fname};")
        w.dedent()
        w.line("};")
        # Emit GC descriptor
        if ptr_offsets:
            offsets_str = ", ".join(ptr_offsets)
            w.line(f"static const int _mesa_gc_desc_{c_name}[] = {{{offsets_str}, -1}};")
        else:
            w.line(f"#define _mesa_gc_desc_{c_name} _mesa_gc_desc_none")
        w.line()
        w.line()

    def _fn_typedef(self, ty) -> str:
        """Get or create a typedef name for a function pointer type."""
        from src.ast import TyFun
        if not isinstance(ty, TyFun):
            return c_typeexpr(ty)
        params_str = ", ".join(c_typeexpr(p) for p in ty.params) or "void"
        ret_str = c_typeexpr(ty.ret)
        key = f"{ret_str}__{'_'.join(c_typeexpr(p) for p in ty.params)}"
        key = key.replace(" ", "_").replace("*", "ptr").replace("(", "").replace(")", "")
        if key not in self._fn_typedefs:
            tname = f"_mesa_fn_{key}"
            self.w.line(f"typedef {ret_str} (*{tname})({params_str});")
            self._fn_typedefs[key] = tname
        return self._fn_typedefs[key]

    def _emit_optional_typedefs(self, program: Program):
        """Scan annotations/expressions and emit helper typedefs used by generated C."""
        from src.ast import TyOptional, IfExpr as IExpr, NoneLit as NLit
        seen = set()
        def emit_for_type(ty):
            if ty is None: return
            from src.types import TOptional as TOpt
            if isinstance(ty, TOpt):
                emit_for_type(ty.inner)
                if isinstance(ty.inner, TVar):
                    return
                mangle = _mangle_type(ty.inner)
                if mangle not in seen:
                    seen.add(mangle)
                    inner_c = c_type(ty.inner)
                    if self._type_requires_complete_definition(ty.inner):
                        self._defer_optional_type(inner_c, mangle)
                    else:
                        self._emit_optional_type(inner_c, mangle)
            if isinstance(ty, TVec) and ty.size is None:
                emit_for_type(ty.inner)
                if isinstance(ty.inner, TVar):
                    return
                mangle = _mangle_type(ty.inner)
                if mangle not in self._vec_types:
                    inner_c = c_type(ty.inner)
                    if self._type_requires_complete_definition(ty.inner):
                        self._defer_vec_type(inner_c, mangle)
                    else:
                        self._emit_vec_type(inner_c, mangle)
        for eset in self._all_error_sets():
            for payload_ty in error_set_variants(eset).values():
                if payload_ty is not None:
                    emit_for_type(TOptional(payload_ty))
        def scan_typeexpr(ty):
            if ty is None: return
            if isinstance(ty, TyOptional):
                scan_typeexpr(ty.inner)
                mangle = _mangle_typeexpr(ty.inner)
                if mangle not in seen:
                    seen.add(mangle)
                    if isinstance(ty.inner, TyNamed):
                        resolved = self.env.lookup_type(ty.inner.name)
                        if resolved is None:
                            return
                        if self._type_requires_complete_definition(resolved):
                            self._defer_optional_type(c_typeexpr(ty.inner), mangle)
                        else:
                            self._emit_optional_type(c_typeexpr(ty.inner), mangle)
                    else:
                        self._emit_optional_type(c_typeexpr(ty.inner), mangle)
            if isinstance(ty, TyVec) and ty.size is None:
                scan_typeexpr(ty.elem)
                if isinstance(ty.elem, TyNamed) and self.env.lookup_type(ty.elem.name) is None:
                    return
                mangle = _mangle_typeexpr(ty.elem)
                if mangle not in self._vec_types:
                    if isinstance(ty.elem, TyNamed):
                        resolved = self.env.lookup_type(ty.elem.name)
                        if resolved is not None and self._type_requires_complete_definition(resolved):
                            self._defer_vec_type(c_typeexpr(ty.elem), mangle)
                        else:
                            self._emit_vec_type(c_typeexpr(ty.elem), mangle)
                    else:
                        self._emit_vec_type(c_typeexpr(ty.elem), mangle)
            if isinstance(ty, TyErrorUnion):
                scan_typeexpr(ty.payload)
            if hasattr(ty, 'inner'): scan_typeexpr(ty.inner)
            if hasattr(ty, 'params'):
                for p in ty.params: scan_typeexpr(p)
            if hasattr(ty, 'elem'):
                scan_typeexpr(ty.elem)
            if hasattr(ty, 'ret'): scan_typeexpr(ty.ret)
        def scan_expr(expr):
            if expr is None: return
            # Suffix ternary: IfExpr with NoneLit else
            if isinstance(expr, IExpr):
                et = expr.else_block.tail if expr.else_block else None
                if isinstance(et, NLit):
                    # The then-branch type gives us the optional inner type
                    ty = self._expr_type(expr.then_block.tail) if expr.then_block.tail else None
                    if ty is not None:
                        mangle = _mangle_type(ty)
                        if mangle not in seen:
                            seen.add(mangle)
                            self._emit_optional_type(c_type(ty), mangle)
            if isinstance(expr, (VecLit, VecComp)):
                emit_for_type(self._expr_type(expr))
            for attr in ('left','right','operand','obj','callee','value','init','expr',
                         'cond','then_block','else_block','tail','body','handle','block'):
                child = getattr(expr, attr, None)
                if child and hasattr(child, '__dict__'):
                    scan_expr(child)
            for attr in ('stmts','args','elems','fields','arms','params'):
                for child in getattr(expr, attr, []):
                    if hasattr(child, '__dict__'):
                        scan_expr(child)
                        scan_typeexpr(getattr(child, 'type_', None))
                    cv = getattr(child, 'value', None)
                    if cv and hasattr(cv, '__dict__'): scan_expr(cv)
        def scan_block(block):
            if block is None: return
            for s in block.stmts:
                scan_expr(s)
                scan_typeexpr(getattr(s, 'type_', None))
            scan_expr(block.tail)
        def scan_decl(decl):
            self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
            if isinstance(decl, FunDecl):
                if getattr(decl, "_type_params", []):
                    return
                scan_typeexpr(decl.ret)
                for p in decl.params: scan_typeexpr(p.type_)
                if decl.body: scan_block(decl.body)
            elif isinstance(decl, StructDecl):
                for f in decl.fields: scan_typeexpr(f.type_)
                for m in decl.methods: scan_decl(m)
            elif isinstance(decl, DefDecl):
                for m in decl.methods: scan_decl(m)
        for decl in program.decls:
            scan_decl(decl)
        self.env.set_current_pkg(None)

    def _emit_union_type(self, e: UnionDecl):
        w = self.w
        ty = self.env.lookup_type(e.name)
        if not isinstance(ty, TUnion): return
        c_name = self._c_decl_name(e, e.name)

        variants = list(ty.variants.items())
        has_payload = any(vty is not None for _, vty in variants)

        if not has_payload:
            # Unit-only union → clean C enum
            w.line(f"typedef enum {{")
            w.indent()
            for i, (vname, _) in enumerate(variants):
                comma = "," if i < len(variants) - 1 else ""
                w.line(f"{c_name}_{vname} = {i}{comma}")
            w.dedent()
            w.line(f"}} {c_name};")
        else:
            # Mixed union → tagged struct with anonymous union for payloads
            w.line(f"typedef enum {{")
            w.indent()
            for i, (vname, _) in enumerate(variants):
                comma = "," if i < len(variants) - 1 else ""
                w.line(f"{c_name}_{vname} = {i}{comma}")
            w.dedent()
            w.line(f"}} {c_name}_tag;")
            w.line(f"struct {c_name} {{")
            w.indent()
            w.line(f"{c_name}_tag tag;")
            payload_variants = [(n, t) for n, t in variants if t is not None]
            if payload_variants:
                w.line("union {")
                w.indent()
                for vname, vty in payload_variants:
                    ct = c_type(vty)
                    field = vname.lower()
                    # Avoid C keyword collisions
                    c_keywords = {"int","float","double","char","long","short","void",
                                  "unsigned","signed","const","static","extern","auto",
                                  "register","volatile","return","if","else","for",
                                  "while","do","switch","case","break","continue",
                                  "goto","sizeof","struct","union","enum","typedef",
                                  "default","inline","restrict","bool"}
                    if field in c_keywords:
                        field = f"v_{field}"
                    w.line(f"{ct} {field};")
                w.dedent()
                w.line("} payload;")
            w.dedent()
            w.line(f"}};")
        w.line()

    def _emit_optional_type(self, inner_ty: str, mangle: str):
        # Normalise aliases: int→int64_t/i64, float→double/f64
        _ty_map   = {"int": "int64_t", "float": "double"}
        _name_map = {"int": "i64",     "float": "f64"}
        inner_ty = _ty_map.get(inner_ty, inner_ty)
        mangle   = _name_map.get(mangle, mangle)
        if mangle in self._opt_types: return
        self._opt_types.add(mangle)
        if self._assume_shared_support:
            return
        self.w.line(f"typedef struct {{ {inner_ty} value; int has_value; }} mesa_opt_{mangle};")

    def _emit_uncertain_typedefs(self, program: Program):
        seen = set()

        def emit_for_type(ty):
            if ty is None:
                return
            if isinstance(ty, TUncertain):
                mangle = _mangle_type(ty.inner)
                if mangle not in seen:
                    seen.add(mangle)
                    inner_c = c_type(ty.inner)
                    if self._type_requires_complete_definition(ty.inner):
                        self._defer_uncertain_type(inner_c, mangle)
                    else:
                        self._emit_uncertain_type(inner_c, mangle)
                emit_for_type(ty.inner)
            elif isinstance(ty, TOptional):
                emit_for_type(ty.inner)
            elif isinstance(ty, TVec):
                emit_for_type(ty.inner)
            elif isinstance(ty, TStruct):
                for ft in ty.fields.values():
                    emit_for_type(ft)
            elif isinstance(ty, TUnion):
                for vt in ty.variants.values():
                    emit_for_type(vt)

        def scan_expr(expr):
            if expr is None:
                return
            ty = getattr(expr, "_resolved_type", None)
            emit_for_type(ty)
            for attr in ('left','right','operand','obj','callee','value','init','expr',
                         'cond','then_block','else_block','tail','body','handle','block'):
                child = getattr(expr, attr, None)
                if child and hasattr(child, '__dict__'):
                    scan_expr(child)
            for attr in ('stmts','args','elems','fields','arms','params'):
                for child in getattr(expr, attr, []):
                    if hasattr(child, '__dict__'):
                        scan_expr(child)
                        emit_for_type(getattr(child, 'type_', None))
                    cv = getattr(child, 'value', None)
                    if cv and hasattr(cv, '__dict__'):
                        scan_expr(cv)

        def scan_block(block):
            if block is None:
                return
            for s in block.stmts:
                scan_expr(s)
            scan_expr(block.tail)

        def scan_decl(decl):
            if isinstance(decl, FunDecl):
                emit_for_type(getattr(self.env.lookup(decl.name), "type_", None))
                if decl.body:
                    scan_block(decl.body)
            elif isinstance(decl, StructDecl):
                for m in decl.methods:
                    scan_decl(m)
            elif isinstance(decl, DefDecl):
                for m in decl.methods:
                    scan_decl(m)

        for decl in program.decls:
            scan_decl(decl)

    def _emit_uncertain_type(self, inner_ty: str, mangle: str):
        if not hasattr(self, "_uncertain_types"):
            self._uncertain_types = set()
        if mangle in self._uncertain_types:
            return
        self._uncertain_types.add(mangle)
        if self._assume_shared_support:
            return
        self.w.line(
            f"typedef struct {{ {inner_ty} value; {inner_ty} uncertainty; }} "
            f"mesa_uncertain_{mangle};"
        )

    def _emit_vec_type(self, inner_ty: str, mangle: str):
        if mangle in self._vec_types: return
        self._vec_types.add(mangle)
        if self._assume_shared_support:
            return
        self.w.line(f"typedef struct {{ {inner_ty}* data; int64_t len; int64_t cap; }} mesa_vec_{mangle};")

    def _emit_forward_decls(self, program: Program):
        w = self.w
        w.line("/* ── Function forward declarations ───────────────── */")
        for decl in program.decls:
            decl_pkg = getattr(decl, "_pkg_path", None)
            self.env.set_current_pkg(decl_pkg)
            if isinstance(decl, UnitAlias):
                pass   # unit aliases are compile-time only
            elif isinstance(decl, FunDecl):
                if not getattr(decl, '_type_params', []):
                    w.line(self._fn_signature(decl, None) + ";")
            elif isinstance(decl, StructDecl):
                for m in decl.methods:
                    self.env.set_current_pkg(self._decl_pkg_path(m, decl_pkg))
                    w.line(self._fn_signature(m, decl.name) + ";")
            elif isinstance(decl, DefDecl):
                for m in decl.methods:
                    self.env.set_current_pkg(self._decl_pkg_path(m, decl_pkg))
                    w.line(self._fn_signature(m, decl.for_type) + ";")
                self.env.set_current_pkg(decl_pkg)
                self._emit_vtable_forward_decl(decl)
        self.env.set_current_pkg(None)
        w.line()

    def _fn_signature(self, f: FunDecl, receiver: Optional[str]) -> str:
        name    = self._c_decl_name(f, f"{receiver}__{f.name}" if receiver else f.name)
        link_name = getattr(f, "_link_name", None)
        # Mangle user functions that collide with C stdlib or type keywords
        _C_RESERVED = {"pow", "sqrt", "abs", "log", "exp", "sin", "cos", "tan",
                        "double", "float", "int", "char", "long", "short", "void",
                        "unsigned", "signed", "struct", "union", "enum", "typedef"}
        if not receiver and name in _C_RESERVED:
            name = f"mesa_{name}"
        # C requires main() to return int regardless of Mesa return type
        saved_receiver_struct = getattr(self.env, "_current_struct", None)
        self.env.set_current_struct(receiver)
        try:
            resolved_ret = None
            if getattr(f, "_is_entrypoint", False) and not receiver:
                name = "main"
                ret = "int"
            else:
                from src.types import TDynInterface, TAnyInterface
                from src.checker import effective_return_type
                resolved_ret = effective_return_type(f, self.env)
                if isinstance(resolved_ret, TAnyInterface):
                    ret = self._iface_any_name(resolved_ret.iface)
                elif isinstance(resolved_ret, TDynInterface):
                    ret = f"{self._iface_box_name(resolved_ret.iface)}*"
                elif isinstance(resolved_ret, TFun):
                    inner_params = ", ".join(c_type(p) for p in resolved_ret.params) or "void"
                    inner_ret = c_type(resolved_ret.ret)
                    ret = None
                else:
                    ret = c_type(resolved_ret)
            params  = []
            for p in f.params:
                if isinstance(p.type_, TyFun):
                    inner_params = ", ".join(c_typeexpr(ip) for ip in p.type_.params)
                    ir = c_typeexpr(p.type_.ret)
                    params.append(f"{ir} (*{p.name})({inner_params})")
                else:
                    from src.types import TDynInterface, TAnyInterface
                    from src.checker import lower_type as _lower_type
                    resolved_p = _lower_type(p.type_, self.env)
                    if isinstance(resolved_p, TAnyInterface):
                        params.append(f"{self._iface_any_name(resolved_p.iface)} {p.name}")
                    elif isinstance(resolved_p, TDynInterface):
                        params.append(f"{self._iface_box_name(resolved_p.iface)}* {p.name}")
                    else:
                        params.append(f"{c_type(resolved_p)} {p.name}")
            if not params:
                params = ["void"]
            params_sig = ", ".join(params)
            if isinstance(resolved_ret, TFun):
                sig = f"{inner_ret} (*{name}({params_sig}))({inner_params})"
            else:
                sig = f"{ret} {name}({params_sig})"
            if link_name and getattr(f, "is_extern", False) and not receiver:
                return f'{sig} __asm__("{link_name}")'
            return sig
        finally:
            self.env.set_current_struct(saved_receiver_struct)

    def _emit_decls(self, program: Program):
        for decl in program.decls:
            if isinstance(decl, UnitAlias):
                pass   # unit aliases are compile-time only — nothing to emit
            elif isinstance(decl, ErrorDecl):
                pass   # error set structs emitted lazily when first used
            elif isinstance(decl, FunDecl):
                if not getattr(decl, '_type_params', []):
                    self._emit_fn(decl, None)
            elif isinstance(decl, StructDecl):
                for m in decl.methods:
                    self._emit_fn(m, decl.name, pkg_path=self._decl_pkg_path(m, getattr(decl, "_pkg_path", None)))
            elif isinstance(decl, DefDecl):
                for m in decl.methods:
                    self._emit_fn(m, decl.for_type, pkg_path=self._decl_pkg_path(m, getattr(decl, "_pkg_path", None)))
                # Emit static vtable for this def block
                self._emit_vtable(decl)
            elif isinstance(decl, LetStmt) and decl.init is not None:
                self._emit_global_let(decl)

    def _emit_global_let(self, l: LetStmt):
        # Type alias: let Score := i64  →  typedef int64_t Score;
        if l.init is None and l.type_ is not None:
            ty = c_typeexpr(l.type_)
            self.w.line(f"typedef {ty} {l.name};")
            return
        ty = c_typeexpr(l.type_) if l.type_ else "int64_t"
        if l.init is not None:
            mesa_ty = self._expr_type(l.init)
            if mesa_ty is not None:
                ty = c_type(mesa_ty)
        val = self._expr(l.init) if l.init else _zero(ty)
        self.w.line(f"static {ty} {l.name} = {val};")

    # ══════════════════════════════════════════════════════════
    # Function emission
    # ══════════════════════════════════════════════════════════

    def _emit_vtable(self, decl: "DefDecl"):
        """Emit thunks and static vtable for 'def IFace for ConcreteType'."""
        w = self.w
        prev_pkg = getattr(self.env, "_current_pkg", None)
        self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
        iface_names = self.env.impls.all_interfaces_for(decl.for_type)
        for iface_name in iface_names:
            iface_ty = self.env.lookup_type(iface_name)
            if not isinstance(iface_ty, TInterface): continue
            concrete = decl.for_type
            key = (concrete, iface_name)
            if key in self._vtables_emitted:
                continue
            self._vtables_emitted.add(key)
            concrete_c = self._c_type_name(concrete)
            iface_c = self._iface_type_name(iface_ty)
            iface_box = self._iface_box_name(iface_ty)
            iface_vtable = self._iface_vtable_name(iface_ty)
            # Emit thunk functions: bridge void* → concrete type by-value
            for mname, mty in iface_ty.methods.items():
                if not self._iface_method_is_object_safe(mty):
                    continue
                ret_str    = c_type(mty.ret)
                extra_params = list(mty.params[1:]) if mty.params else []
                extra_str  = "".join(f", {c_type(p)} _a{i}" for i, p in enumerate(extra_params))
                extra_call = "".join(f", _a{i}" for i in range(len(extra_params)))
                # Check if self param is pointer receiver (*T) or value receiver (T)
                self_param = mty.params[0] if mty.params else None
                from src.types import TPointer as _TPtr
                if isinstance(self_param, _TPtr):
                    self_cast = f"({concrete_c}*)_self"   # pointer receiver
                    direct_target = f"{concrete_c}__{mname}"
                else:
                    self_cast = f"*({concrete_c}*)_self"  # value receiver — dereference
                    direct_target = f"{concrete_c}__{mname}"
                w.line(f"static {ret_str} _thunk_{concrete_c}_{iface_c}_{mname}"
                       f"(void* _self{extra_str}) {{")
                w.indent()
                impl_method = self.env.impls.get_method(concrete, iface_name, mname)
                if impl_method is None:
                    if ret_str == "void":
                        w.line("(void)_self;")
                    else:
                        w.line(f"return ({ret_str}){{0}};")
                else:
                    ret_prefix = "" if str(mty.ret) == "void" else "return "
                    w.line(f"{ret_prefix}{direct_target}({self_cast}{extra_call});")
                w.dedent()
                w.line("}")
            # Emit static vtable using thunks
            vtable_name = f"_mesa_vtable_{concrete_c}_{iface_c}"
            w.line(f"const {iface_vtable} {vtable_name} = {{")
            w.indent()
            for mname, mty in iface_ty.methods.items():
                if not self._iface_method_is_object_safe(mty):
                    continue
                w.line(f".{mname} = _thunk_{concrete_c}_{iface_c}_{mname},")
            w.dedent()
            w.line("};")
            w.line()
        self.env.set_current_pkg(prev_pkg)

    def _emit_vtable_forward_decl(self, decl: "DefDecl"):
        iface_names = self.env.impls.all_interfaces_for(decl.for_type)
        concrete_c = self._c_type_name(decl.for_type)
        for iface_name in iface_names:
            iface_ty = self.env.lookup_type(iface_name)
            if not isinstance(iface_ty, TInterface):
                continue
            iface_vtable = self._iface_vtable_name(iface_ty)
            iface_c = self._iface_type_name(iface_ty)
            vtable_name = f"_mesa_vtable_{concrete_c}_{iface_c}"
            self.w.line(f"extern const {iface_vtable} {vtable_name};")

    def _emit_fn(self, f: FunDecl, receiver: Optional[str], pkg_path: Optional[str] = None):
        if f.body is None: return
        w = self.w
        prev_pkg = getattr(self.env, "_current_pkg", None)
        self.env.set_current_pkg(self._decl_pkg_path(f, pkg_path))
        is_main = (f.name == "main" and not receiver)
        sig = self._fn_signature(f, receiver)
        w.line(sig + " {")
        w.indent()
        # Track which params are pointers for field access arrow vs dot
        saved_ptr_params   = set(self._ptr_params)
        saved_receiver     = self._current_receiver
        saved_fn_ret       = self._current_fn_ret
        saved_fn_ret_ty    = self._current_fn_ret_ty
        saved_fn_name      = getattr(self, '_current_fn_name', None)
        saved_fn_handle    = getattr(self, '_current_fn_has_handle', False)
        saved_fn_handle_target = getattr(self, '_current_fn_handle_target', None)
        fn_handle = getattr(f, 'handle_block', None)
        fn_handle_binding_ty = getattr(fn_handle, '_binding_type', None)
        from src.checker import effective_return_type as _effective_ret
        self._current_fn_ret  = f.ret
        self._current_fn_ret_ty = _effective_ret(f, self.env)
        self._current_fn_name = f.name
        self._current_fn_has_handle = bool(fn_handle)
        self._current_fn_handle_target = (
            (
                f"_handle_{f.name}",
                f"_handle_err_{f.name}",
                fn_handle_binding_ty if isinstance(fn_handle_binding_ty, (TErrorSet, TErrorSetUnion)) else None,
                None,
            )
            if self._current_fn_has_handle else None
        )
        self._current_receiver = receiver
        for p in f.params:
            if isinstance(p.type_, TyPointer):
                self._ptr_params.add(p.name)
        param_gc_roots = [p.name for p in f.params if isinstance(p.type_, TyPointer)]
        if param_gc_roots:
            self._emit_gc_root_push(param_gc_roots, block_depth=self._block_depth + 1)
        # Declare error accumulator for handle block
        if self._current_fn_has_handle:
            handle_binding_ty = getattr(getattr(f, 'handle_block', None), '_binding_type', None)
            if isinstance(handle_binding_ty, (TErrorSet, TErrorSetUnion)):
                self._emit_error_object_type(handle_binding_ty)
                w.line(f"{c_type(handle_binding_ty)} _handle_err_{f.name} = ({c_type(handle_binding_ty)}){{0}};")
            else:
                w.line(f"uint16_t _handle_err_{f.name} = 0;")
        self._emit_block_stmts(f.body, is_fn_body=not is_main)
        # Emit handle block epilogue if present
        if self._current_fn_has_handle and getattr(f, 'handle_block', None):
            h = f.handle_block
            w.line(f"goto _handle_done_{f.name};")
            w.dedent()
            w.line(f"_handle_{f.name}:;")
            w.indent()
            binding_ty = getattr(h, '_binding_type', None)
            binding_c = c_type(binding_ty) if binding_ty is not None else "int64_t"
            w.line(f"{binding_c} {h.binding} = _handle_err_{f.name};")
            self._emit_block_stmts(h.body)
            w.dedent()
            w.line(f"_handle_done_{f.name}:;")
            w.indent()
        ret_ty_lower = self._current_fn_ret_ty
        if isinstance(ret_ty_lower, TErrorUnion) and isinstance(ret_ty_lower.payload, TVoid):
            self._emit_cleanups_for_exit()
            self._emit_ok_error_union_return(ret_ty_lower)
        # main() always returns int 0 to the OS — emit unconditionally
        if is_main:
            w.line("return 0;")
        self._ptr_params       = saved_ptr_params
        self._current_receiver = saved_receiver
        self._current_fn_ret       = saved_fn_ret
        self._current_fn_ret_ty    = saved_fn_ret_ty
        self._current_fn_name      = saved_fn_name
        self._current_fn_has_handle = saved_fn_handle
        self._current_fn_handle_target = saved_fn_handle_target
        self.env.set_current_pkg(prev_pkg)
        w.dedent()
        w.line("}")
        w.line()

    # ══════════════════════════════════════════════════════════
    # Statement emission
    # ══════════════════════════════════════════════════════════

    def _emit_block_stmts(self, block: Optional[Block],
                           is_fn_body: bool = False):
        """Emit block statements. If is_fn_body=True, tail becomes return."""
        self._emit_block_body(block, is_fn_body=is_fn_body)

    def _emit_return_value(self, value_expr: Expr):
        w = self.w
        from src.types import TErrorUnion, TErrorSet, TErrorSetUnion
        ret_ty = self._current_fn_ret_ty
        val_ty = self._expr_type(value_expr)
        rendered = self._expr(value_expr)
        if rendered in ("/* void */", "/* undef */"):
            return

        if isinstance(ret_ty, TErrorUnion) and isinstance(val_ty, TErrorUnion):
            val_c = c_type(val_ty)
            tmp = self._fresh("ret_erru")
            val = rendered
            w.line(f"{val_c} {tmp} = {val};")
            if ret_ty.error_set is None and val_ty.error_set is not None:
                w.line(f"if ({tmp}.is_err) {{")
                w.indent()
                self._emit_cleanups_for_exit(error_exit=True)
                self._emit_anyerror_return_from_result(tmp, val_ty, ret_ty)
                w.dedent()
                w.line("}")
                self._emit_cleanups_for_exit()
                self._emit_anyerror_return_from_result(tmp, val_ty, ret_ty)
                return
            if (ret_ty.error_set != val_ty.error_set and
                    error_set_contains(ret_ty.error_set, val_ty.error_set)):
                w.line(f"if ({tmp}.is_err) {{")
                w.indent()
                self._emit_cleanups_for_exit(error_exit=True)
                self._emit_result_return_from_result(tmp, val_ty, ret_ty)
                w.dedent()
                w.line("}")
                self._emit_cleanups_for_exit()
                self._emit_result_return_from_result(tmp, val_ty, ret_ty)
                return
            w.line(f"if ({tmp}.is_err) {{")
            w.indent()
            self._emit_cleanups_for_exit(error_exit=True)
            w.line(f"return {tmp};")
            w.dedent()
            w.line("}")
            self._emit_cleanups_for_exit()
            w.line(f"return {tmp};")
            return

        if isinstance(ret_ty, TErrorUnion):
            eset   = ret_ty.error_set
            key    = f"{_error_key(eset)}_{_mangle_type(ret_ty.payload)}"
            self._emit_result_struct(eset, ret_ty.payload)
            if isinstance(val_ty, (TErrorSet, TErrorSetUnion)):
                err_c = c_type(val_ty)
                tmp = self._fresh("ret_err")
                val = self._expr(value_expr)
                w.line(f"{err_c} {tmp} = {val};")
                self._emit_cleanups_for_exit(error_exit=True)
                if eset is None:
                    any_key = f"{_error_key(None)}_{_mangle_type(ret_ty.payload)}"
                    self._emit_result_struct(None, ret_ty.payload)
                    w.line(f"{{ Mesa_result_{any_key} _r = (Mesa_result_{any_key}){{0}};")
                    w.line("  _r.is_err = 1;")
                    w.line(f"  _r.err.tag = {tmp}.tag;")
                    w.line(f"  memcpy(_r.err.payload, {tmp}.payload, sizeof({tmp}.payload));")
                    w.line("  return _r; }")
                else:
                    w.line(f"{{ Mesa_result_{key} _r = (Mesa_result_{key}){{0}}; _r.is_err=1; _r.err.tag={tmp}.tag;")
                    w.line(f"  memcpy(_r.err.payload, {tmp}.payload, sizeof({tmp}.payload));")
                    w.line(f"  return _r; }}")
                return
            else:
                val = self._expr(value_expr)
                self._emit_cleanups_for_exit()
                if val in ("/* void */", "/* undef */"):
                    w.line(f"return (Mesa_result_{key}){{.is_err=0}};")
                else:
                    w.line(f"return (Mesa_result_{key}){{.is_err=0, .value={val}}};")
                return

        val = self._coerce_expr_for_target_type(rendered, value_expr, self._current_fn_ret_ty)
        self._emit_cleanups_for_exit()
        w.line(f"return {val};")

    def _emit_stmt(self, stmt: Stmt):
        w = self.w
        if isinstance(stmt, LetStmt):
            self._emit_let(stmt)
        elif isinstance(stmt, ReturnStmt):
            if stmt.value:
                self._emit_return_value(stmt.value)
            else:
                ret_ty = self._current_fn_ret_ty
                self._emit_cleanups_for_exit()
                if isinstance(ret_ty, TErrorUnion) and isinstance(ret_ty.payload, TVoid):
                    self._emit_ok_error_union_return(ret_ty)
                else:
                    w.line("return;")
        elif isinstance(stmt, AssignStmt):
            self._emit_assign(stmt)
        elif isinstance(stmt, ExprStmt):
            if isinstance(stmt.expr, EscExpr):
                self._emit_return_value(stmt.expr)
            elif isinstance(stmt.expr, WhileUnwrap):
                self._emit_while_unwrap(stmt.expr)
            else:
                e = self._expr(stmt.expr)
                if e and e != "/* void */":
                    w.line(f"{e};")
        elif isinstance(stmt, ForRangeStmt):
            self._emit_for_range(stmt)
        elif isinstance(stmt, ForIterStmt):
            self._emit_for_iter(stmt)
        elif isinstance(stmt, WhileStmt):
            self._emit_while(stmt)
        elif isinstance(stmt, DeferStmt):
            self._cleanup_frames.append({
                "kind": "defer",
                "body": stmt.body,
                "error_only": stmt.error_only,
                "loop_depth": len(self._loop_stack),
                "block_depth": self._block_depth,
            })
        elif isinstance(stmt, BreakStmt):
            target_depth = self._target_loop_depth(stmt.label)
            if target_depth is not None:
                self._emit_cleanups_for_exit(min_loop_depth=target_depth)
            if stmt.label:
                w.line(f"goto _break_{stmt.label};")
            else:
                w.line("break;")
        elif isinstance(stmt, ContinueStmt):
            target_depth = self._target_loop_depth(stmt.label)
            if target_depth is not None:
                self._emit_cleanups_for_exit(min_loop_depth=target_depth)
            if stmt.label:
                w.line(f"goto _cont_{stmt.label};")
            else:
                w.line("continue;")

    def _emit_let(self, l: LetStmt):
        w    = self.w
        target_mesa_ty: Optional[Type] = None
        decl_includes_name = False
        # Pointers are never const — you need to write through them
        is_ptr = isinstance(l.type_, TyPointer) if l.type_ else False
        if not is_ptr and l.init:
            from src.types import TPointer as _TPtrInfer
            init_ty = self._expr_type(l.init)
            is_ptr = isinstance(init_ty, _TPtrInfer)
        is_allocator = False
        if l.type_:
            from src.checker import lower_type as _lower_type
            lowered = _lower_type(l.type_, self.env)
            is_allocator = self._is_allocator_operand_type(lowered)
        elif l.init:
            init_ty = self._expr_type(l.init)
            is_allocator = self._is_allocator_operand_type(init_ty)
        qual = "" if (l.mutable or is_ptr or is_allocator) else "const "
        if l.type_ and isinstance(l.type_, TyFun):
            ty = self._fn_typedef(l.type_)
        elif l.type_:
            # Check if it's a pointer to an interface
            from src.types import TDynInterface
            from src.checker import lower_type as _lower_type
            resolved = _lower_type(l.type_, self.env)
            target_mesa_ty = resolved
            if isinstance(resolved, TAnyInterface):
                ty = self._iface_any_name(resolved.iface)
            elif isinstance(resolved, TDynInterface):
                ty = f"{self._iface_box_name(resolved.iface)}*"
            else:
                ty = c_typeexpr(l.type_)
        else:
            ty = "int64_t"

        # Infer type from initialiser if no annotation
        if not l.type_ and l.init:
            mesa_ty = self._expr_type(l.init)
            # Resolve TVar in mono context
            from src.types import TVar as _TVlet
            if isinstance(mesa_ty, _TVlet) and mesa_ty.name and mesa_ty.name in self._mono_bindings:
                mesa_ty = self._mono_bindings[mesa_ty.name]
            from src.types import TOptional as TOpt, TVar, TUnitful, TUncertain
            if isinstance(mesa_ty, TUnitful):
                # Static unitful — use inner type (usually double)
                ty = c_type(mesa_ty.inner) if mesa_ty.dims is not None else "mesa_unitful"
            elif isinstance(mesa_ty, TUncertain):
                ty = c_type(mesa_ty)
            elif isinstance(mesa_ty, TOpt):
                inner = mesa_ty.inner
                # If inner is a TVar (unresolved), get concrete type from IfExpr then-branch
                if isinstance(inner, TVar):
                    from src.ast import IfExpr as IEx
                    if isinstance(l.init, IEx) and l.init.then_block.tail:
                        inner = self._expr_type(l.init.then_block.tail) or inner
                mangle = _mangle_type(inner)
                inner_c = c_type(inner)
                self._emit_optional_type(inner_c, mangle)
                ty = f"mesa_opt_{mangle}"
            elif mesa_ty is not None:
                ty = c_type(mesa_ty) if c_type(mesa_ty) != "int64_t" else self._infer_c_type(l.init)
            else:
                ty = self._infer_c_type(l.init)
            from src.types import TFun as _TFunLet
            if isinstance(mesa_ty, _TFunLet):
                ty = c_type(mesa_ty, l.name)
                decl_includes_name = True
                qual = ""

        if l.init:
            # Pointer let inside with block — allocate from active allocator
            from src.ast import TyPointer as _TyPtr
            if self._active_alloc and isinstance(l.type_, _TyPtr) and not isinstance(l.init, WithAllocExpr):
                alloc_expr, alloc_ty = self._active_alloc
                from src.checker import lower_type as _ltalloc
                inner_ty = _ltalloc(l.type_.inner, self.env)
                inner_c  = c_type(inner_ty)
                lo    = self.layout.layout_of(inner_ty) if self.layout else None
                align = lo.align if lo else 8
                from src.types import TStruct, TPointer
                target_ty = alloc_ty.inner if isinstance(alloc_ty, TPointer) else alloc_ty
                if isinstance(target_ty, TStruct) and self.env.impls.implements(target_ty.name, "Allocator"):
                    alloc_call = f"{c_type(target_ty)}__alloc(&{alloc_expr}, sizeof({inner_c}), {align})"
                else:
                    alloc_call = f"mesa_c_alloc(sizeof({inner_c}), {align})"
                init_val = self._expr(l.init)
                w.line(f"{inner_c}* {l.name} = ({inner_c}*){alloc_call};")
                w.line(f"*{l.name} = {init_val};")
                self._ptr_params.add(l.name)  # field access uses -> not .
            else:
                # No active allocator — use GC for pointer types with struct literal init
                from src.ast import TyPointer as _TyPtrGC, TupleLit as _TupGC
                if isinstance(l.type_, _TyPtrGC) and not self._active_alloc and isinstance(l.init, _TupGC):
                    from src.checker import lower_type as _ltgc
                    inner_ty = _ltgc(l.type_.inner, self.env)
                    inner_c  = c_type(inner_ty)
                    desc_name = f"_mesa_gc_desc_{c_type(inner_ty)}" if hasattr(inner_ty, 'name') else "_mesa_gc_desc_none"
                    align = self._alignment_of(inner_ty)
                    gc_call  = f"mesa_gc_alloc(sizeof({inner_c}), {align}, {desc_name})"
                    init_val = self._expr(l.init)
                    w.line(f"{inner_c}* {l.name} = ({inner_c}*){gc_call};")
                    w.line(f"*{l.name} = {init_val};")
                    self._ptr_params.add(l.name)
                else:
                    val = self._expr(l.init)
                    if target_mesa_ty is not None:
                        val = self._coerce_expr_for_target_type(val, l.init, target_mesa_ty)
                    from src.types import TPointer as _TPtrLet
                    init_ty = self._expr_type(l.init)
                    if isinstance(init_ty, _TPtrLet):
                        self._ptr_params.add(l.name)
                    if "[" in ty and "]" in ty:
                        base, size = ty.split("[", 1)
                        size = size.rstrip("]")
                        w.line(f"{qual}{base.strip()} {l.name}[{size}] = {val};")
                    else:
                        if decl_includes_name:
                            w.line(f"{qual}{ty} = {val};")
                        else:
                            w.line(f"{qual}{ty} {l.name} = {val};")
        else:
            if decl_includes_name:
                w.line(f"{qual}{ty} = {_zero(ty)};")
            else:
                w.line(f"{qual}{ty} {l.name} = {_zero(ty)};")
        if is_ptr:
            self._emit_gc_root_push([l.name], block_depth=self._block_depth)

    def _emit_assign(self, a: AssignStmt):
        w   = self.w
        lhs = self._lvalue(a.target)
        rhs = self._expr(a.value)
        op_map = {
            "=": "=", "+=": "+=", "-=": "-=", "*=": "*=",
            "/=": "/=", "%=": "%=", "^=": None,
            ".+=": "+=", ".-=": "-=", ".*=": "*=", "./=": "/=",
        }
        op = op_map.get(a.op)
        if op == "=":
            w.line(f"{lhs} = {rhs};")
        elif op:
            w.line(f"{lhs} {op} {rhs};")
        elif a.op == "^=":
            w.line(f"{lhs} = pow({lhs}, {rhs});")
        else:
            w.line(f"{lhs} = {rhs};")

    def _emit_for_range(self, fr: ForRangeStmt):
        w    = self.w
        op   = "<=" if fr.inclusive else "<"
        end  = self._expr(fr.end)
        start = self._expr(fr.start)

        label_break = f"_break_{fr.label}" if fr.label else None
        label_cont  = f"_cont_{fr.label}"  if fr.label else None

        w.line(f"for (int64_t {fr.var} = {start}; {fr.var} {op} {end}; {fr.var}++) {{")
        w.indent()
        self._loop_stack.append(fr.label)
        if fr.filter:
            w.line(f"if (!({self._expr(fr.filter)})) continue;")
        self._emit_block_stmts(fr.body)
        if label_cont:
            w.line(f"{label_cont}: ;")
        self._loop_stack.pop()
        w.dedent()
        w.line("}")
        if label_break:
            w.line(f"{label_break}: ;")

    def _emit_for_iter(self, fi: ForIterStmt):
        w        = self.w
        iter_var = self._fresh("iter")
        label_break = f"_break_{fi.label}" if fi.label else None
        label_cont  = f"_cont_{fi.label}"  if fi.label else None
        w.line(f"{{")
        w.indent()
        iter_expr = self._expr(fi.iter)
        w.line(f"__auto_type {iter_var} = {iter_expr};")

        idx = self._fresh("i")
        w.line(f"for (int64_t {idx} = 0; {idx} < {iter_var}.len; {idx}++) {{")
        w.indent()
        self._loop_stack.append(fi.label)
        self._emit_for_pattern_binding(fi.pattern, iter_var, idx)

        if fi.filter:
            w.line(f"if (!({self._expr(fi.filter)})) continue;")
        self._emit_block_stmts(fi.body)
        if label_cont:
            w.line(f"{label_cont}: ;")
        self._loop_stack.pop()
        w.dedent()
        w.line("}")
        if label_break:
            w.line(f"{label_break}: ;")
        w.dedent()
        w.line("}")

    def _emit_for_pattern_binding(self, pattern: ForPattern, iter_var: str, idx: str):
        elem_expr = f"{iter_var}.data[{idx}]"
        if isinstance(pattern, PatIdent):
            self.w.line(f"__auto_type {pattern.name} = {elem_expr};")
        elif isinstance(pattern, PatRef):
            self.w.line(f"__auto_type* {pattern.name} = &{iter_var}.data[{idx}];")
        elif isinstance(pattern, PatTuple):
            for j, name in enumerate(pattern.names):
                self.w.line(f"__auto_type {name} = {elem_expr}._{j};")

    def _alignment_of(self, ty: Type) -> int:
        lo = self.layout.layout_of(ty) if self.layout else None
        return lo.align if lo else 8

    def _alloc_bytes_expr(self, size_expr: str, align: int) -> str:
        if self._active_alloc:
            alloc_expr, alloc_ty = self._active_alloc
            from src.types import TStruct, TPointer
            target_ty = alloc_ty.inner if isinstance(alloc_ty, TPointer) else alloc_ty
            if isinstance(target_ty, TStruct) and self.env.impls.implements(target_ty.name, "Allocator"):
                return f"{c_type(target_ty)}__alloc(&{alloc_expr}, {size_expr}, {align})"
        return f"mesa_c_alloc({size_expr}, {align})"

    def _realloc_bytes_expr(self, ptr_expr: str, old_size_expr: str, new_size_expr: str, align: int) -> str:
        if self._active_alloc:
            alloc_expr, alloc_ty = self._active_alloc
            from src.types import TStruct, TPointer
            target_ty = alloc_ty.inner if isinstance(alloc_ty, TPointer) else alloc_ty
            if isinstance(target_ty, TStruct) and self.env.impls.implements(target_ty.name, "Allocator"):
                return f"{c_type(target_ty)}__realloc(&{alloc_expr}, {ptr_expr}, {old_size_expr}, {new_size_expr}, {align})"
        return f"mesa_c_realloc({ptr_expr}, {old_size_expr}, {new_size_expr}, {align})"

    def _emit_while(self, w_stmt: WhileStmt):
        w = self.w
        label_break = f"_break_{w_stmt.label}" if w_stmt.label else None
        label_cont  = f"_cont_{w_stmt.label}"  if w_stmt.label else None
        w.line(f"while ({self._expr(w_stmt.cond)}) {{")
        w.indent()
        self._loop_stack.append(w_stmt.label)
        self._emit_block_stmts(w_stmt.body)
        if label_cont:
            w.line(f"{label_cont}: ;")
        self._loop_stack.pop()
        w.dedent()
        w.line("}")
        if label_break:
            w.line(f"{label_break}: ;")

    def _emit_while_unwrap(self, wu: WhileUnwrap):
        w = self.w
        opt = self._expr(wu.expr)
        tmp = self._fresh("opt")
        w.line("while (1) {")
        w.indent()
        w.line(f"__auto_type {tmp} = {opt};")
        w.line(f"if (!{tmp}.has_value) break;")
        if wu.is_ref:
            try:
                src = self._lvalue(wu.expr)
                w.line(f"__auto_type {wu.binding} = &({src}).value;")
            except Exception:
                w.line(f"__auto_type {wu.binding} = &{tmp}.value;")
        else:
            w.line(f"__auto_type {wu.binding} = {tmp}.value;")
        self._loop_stack.append(None)
        self._emit_block_stmts(wu.body)
        self._loop_stack.pop()
        w.dedent()
        w.line("}")

    # ══════════════════════════════════════════════════════════
    # Expression emission — returns C expression string
    # ══════════════════════════════════════════════════════════

    def _expr(self, expr: Expr) -> str:
        # Coerce concrete type to *any Interface or any Interface if annotated
        from src.types import TDynInterface as _TDyn, TAnyInterface as _TAny
        rt = getattr(expr, '_resolved_type', None)
        if isinstance(rt, (_TDyn, _TAny)):
            return self._coerce_to_dyn(expr, rt)
        if isinstance(expr, IntLit):
            ty = getattr(expr, '_resolved_type', None)
            if isinstance(ty, TFloat):
                return f"{float(expr.value)}"
            return str(expr.value) + "LL"

        if isinstance(expr, FloatLit):
            # Ensure it has a decimal point
            s = repr(expr.value)
            if "." not in s and "e" not in s.lower():
                s += ".0"
            return s

        if isinstance(expr, BoolLit):
            return "1" if expr.value else "0"

        if isinstance(expr, NoneLit):
            # Try to use annotated type for proper optional literal
            rt = getattr(expr, '_resolved_type', None)
            if rt is not None:
                from src.types import TOptional as TOpt
                if isinstance(rt, TOpt):
                    mangle = _mangle_type(rt.inner)
                    return f"(mesa_opt_{mangle}){{0, 0}}"
            return "{0, 0}"

        if isinstance(expr, VariantLit):
            return self._variant_lit(expr)

        if isinstance(expr, StringLit):
            # If there are interpolation segments with expressions, emit runtime concat
            if expr.segments and any(not isinstance(s, str) for s in expr.segments):
                return self._emit_interpolation(expr)
            escaped = expr.raw.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            return f'(mesa_str){{"{escaped}", {len(expr.raw)}}}'

        if isinstance(expr, SelfExpr):
            return "self"

        if isinstance(expr, Ident):
            capture_names = getattr(self, "_closure_capture_names", None)
            if capture_names and expr.name in capture_names:
                return capture_names[expr.name]
            sym = getattr(expr, "_bound_symbol", None) or self.env.lookup(expr.name)
            if sym is not None and sym.c_name:
                return sym.c_name
            return expr.name

        if isinstance(expr, BinExpr):
            return self._binary(expr)

        if isinstance(expr, UnaryExpr):
            return self._unary(expr)

        if isinstance(expr, FieldExpr):
            qualified_variant = self._qualified_variant_expr(expr)
            if qualified_variant is not None:
                return qualified_variant
            qname = self._qualified_name(expr)
            # .value and .units on unitful/uncertain types — intercept before normal field access
            obj_ty = self._expr_type(expr.obj)
            from src.types import TUnitful, TUncertain, TNamespace
            if isinstance(obj_ty, TNamespace):
                if qname and qname.startswith("std.mem."):
                    return qname
                ns_name = getattr(obj_ty, "name", None)
                if ns_name:
                    c_name = self.env.lookup_namespace_value_c_name(ns_name, expr.field)
                    if c_name is not None:
                        return c_name
                return expr.field
            if isinstance(obj_ty, TUnitful):
                if expr.field == "value":
                    return self._expr(expr.obj)   # IS the value — unit is type-only
                if expr.field == "units":
                    if obj_ty.dims is not None:
                        return f'(mesa_str){{"{obj_ty.name}", {len(obj_ty.name)}}}'
                    else:
                        return f"({self._expr(expr.obj)}).name"
            if isinstance(obj_ty, TUncertain):
                if expr.field == "value":
                    return f"({self._expr(expr.obj)}).value"
                if expr.field == "units" and isinstance(obj_ty.inner, TUnitful):
                    if obj_ty.inner.dims is not None:
                        return f'(mesa_str){{"{obj_ty.inner.name}", {len(obj_ty.inner.name)}}}'
                    return f"(({self._expr(expr.obj)}).value).name"
                if expr.field == "uncertainty":
                    return f"({self._expr(expr.obj)}).uncertainty"
            return self._field_access(expr)

        if isinstance(expr, IndexExpr):
            return self._index_expr(expr)

        if isinstance(expr, CallExpr):
            return self._call_expr(expr)

        if isinstance(expr, TupleLit):
            return self._tuple_lit(expr)

        if isinstance(expr, ArrayLit):
            return self._array_lit(expr)

        if isinstance(expr, VecLit):
            return self._vec_lit(expr)

        if isinstance(expr, VecComp):
            return self._vec_comp(expr)

        if isinstance(expr, IfExpr):
            return self._if_expr(expr)

        if isinstance(expr, IfUnwrap):
            return self._if_unwrap(expr)

        if isinstance(expr, MatchExpr):
            return self._match_expr(expr)

        if isinstance(expr, BlockExpr):
            return self._block_expr(expr)

        if isinstance(expr, WithExpr):
            return self._with_expr(expr)

        if isinstance(expr, WithAllocExpr):
            return self._with_alloc_expr(expr)

        if isinstance(expr, Closure):
            return self._closure(expr)

        if isinstance(expr, ComptimeExpr):
            return self._expr(expr.expr)

        if isinstance(expr, EscExpr):
            return self._emit_esc_expr(expr)

        if isinstance(expr, UnitLit):
            # Static unitful — emit just the numeric value; unit is compile-time only
            if expr.value is None:
                return "1.0"   # bare `N` = 1.0 of that unit
            return self._expr(expr.value)

        if isinstance(expr, UncertainLit):
            ty = self._expr_type(expr)
            cty = c_type(ty)
            value = self._expr(expr.value)
            err = self._expr(expr.error)
            return (
                f"({cty}){{"
                f".value = {value}, "
                f".uncertainty = (({err}) < 0 ? -({err}) : ({err}))"
                f"}}"
            )

        return "/* undef */"

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
            obj_ty = self._expr_type(expr.obj)
            if isinstance(obj_ty, TNamespace):
                return self.env.lookup_namespace_type(obj_ty.name, expr.field) is not None
        return False

    # ── Binary ────────────────────────────────────────────────

    def _binary(self, b: BinExpr) -> str:
        if b.op in ("and", "or"):
            op = "&&" if b.op == "and" else "||"
            return f"({self._expr(b.left)} {op} {self._expr(b.right)})"

        lt = self._expr_type(b.left)
        rt = self._expr_type(b.right)
        if isinstance(lt, TUncertain) or isinstance(rt, TUncertain):
            return self._uncertain_binary(b, lt, rt)

        lhs = self._expr(b.left)
        rhs = self._expr(b.right)
        op  = b.op.lstrip(".")   # strip broadcast prefix

        # str == str needs mesa_str_eq
        if op in ("==", "!="):
            from src.types import TString
            if isinstance(lt, TString) or isinstance(rt, TString):
                eq = f"mesa_str_eq({lhs}, {rhs})"
                return eq if op == "==" else f"(!{eq})"

        c_ops = {
            "+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
            "==": "==", "!=": "!=", "<": "<", ">": ">",
            "<=": "<=", ">=": ">=",
        }

        if op == "^":
            return f"pow({lhs}, {rhs})"
        if op == "%":
            if isinstance(lt, TFloat) or isinstance(rt, TFloat):
                return f"fmod({lhs}, {rhs})"
            return f"({lhs} % {rhs})"
        if op == "+-":
            # Uncertain — just return value for now
            return lhs
        if op in c_ops:
            return f"({lhs} {c_ops[op]} {rhs})"
        return lhs

    def _uncertain_binary(self, b: BinExpr, lt: Type, rt: Type) -> str:
        lhs = self._expr(b.left)
        rhs = self._expr(b.right)
        left_val = f"(({lhs}).value)" if isinstance(lt, TUncertain) else f"({lhs})"
        right_val = f"(({rhs}).value)" if isinstance(rt, TUncertain) else f"({rhs})"
        left_unc = f"(({lhs}).uncertainty)" if isinstance(lt, TUncertain) else "0"
        right_unc = f"(({rhs}).uncertainty)" if isinstance(rt, TUncertain) else "0"
        op = b.op.lstrip(".")

        if op in ("==", "!=", "<", ">", "<=", ">="):
            cmp_ops = {
                "==": "==", "!=": "!=", "<": "<", ">": ">",
                "<=": "<=", ">=": ">=",
            }
            return f"({left_val} {cmp_ops[op]} {right_val})"

        result_ty = self._expr_type(b)
        result_c = c_type(result_ty)
        if op == "+":
            return (
                f"({result_c}){{.value = ({left_val} + {right_val}), "
                f".uncertainty = sqrt(({left_unc})*({left_unc}) + ({right_unc})*({right_unc}))}}"
            )
        if op == "-":
            return (
                f"({result_c}){{.value = ({left_val} - {right_val}), "
                f".uncertainty = sqrt(({left_unc})*({left_unc}) + ({right_unc})*({right_unc}))}}"
            )
        if op == "*":
            return (
                f"({result_c}){{.value = ({left_val} * {right_val}), "
                f".uncertainty = sqrt((({right_val})*({left_unc}))*(({right_val})*({left_unc})) + "
                f"(({left_val})*({right_unc}))*(({left_val})*({right_unc})))}}"
            )
        if op == "/":
            left_term = f"(({left_unc})/({right_val}))"
            right_term = f"((({left_val})*({right_unc}))/(({right_val})*({right_val})))"
            return (
                f"({result_c}){{.value = ({left_val} / {right_val}), "
                f".uncertainty = sqrt(({left_term})*({left_term}) + ({right_term})*({right_term}))}}"
            )
        return left_val

    # ── Unary ─────────────────────────────────────────────────

    def _unary(self, u: UnaryExpr) -> str:
        operand = self._expr(u.operand)
        if u.op == "-":  return f"(-{operand})"
        if u.op == "!":  return f"(!{operand})"
        if u.op == "*":  return f"(*{operand})"
        if u.op == "&":  return f"(&{operand})"
        if u.op == "@":  return f"(&{operand})"  # legacy
        return operand

    def _union_payload_field(self, name: str) -> str:
        field = name.lower()
        c_keywords = {"int","float","double","char","long","short","void",
                      "unsigned","signed","const","static","extern","auto",
                      "register","volatile","return","if","else","for",
                      "while","do","switch","case","break","continue",
                      "goto","sizeof","struct","union","enum","typedef",
                      "default","inline","restrict","bool"}
        return f"v_{field}" if field in c_keywords else field

    def _esc_needs_allocator(self, ty: Type) -> bool:
        from src.types import TString, TVec, TOptional, TStruct, TUnion, TUnitful, TUncertain
        if isinstance(ty, TString):
            return True
        if isinstance(ty, TVec):
            return True
        if isinstance(ty, TOptional):
            return self._esc_needs_allocator(ty.inner)
        if isinstance(ty, TStruct):
            return any(self._esc_needs_allocator(ft) for ft in ty.fields.values())
        if isinstance(ty, TUnion):
            return any(vt is not None and self._esc_needs_allocator(vt) for vt in ty.variants.values())
        if isinstance(ty, TUnitful):
            return self._esc_needs_allocator(ty.inner)
        if isinstance(ty, TUncertain):
            return self._esc_needs_allocator(ty.inner)
        return False

    def _clone_value_to_allocator(self, src_expr: str, ty: Type, alloc_entry) -> str:
        from src.types import (
            TInt, TFloat, TBool, TString, TVoid, TIntLit, TFloatLit,
            TOptional, TVec, TStruct, TUnion, TUnitful, TUncertain,
        )
        w = self.w
        if isinstance(ty, (TInt, TFloat, TBool, TVoid, TIntLit, TFloatLit)):
            return src_expr
        if isinstance(ty, TUnitful):
            return self._clone_value_to_allocator(src_expr, ty.inner, alloc_entry)
        if isinstance(ty, TUncertain):
            unc_c = c_type(ty)
            src = self._fresh("esc_unc_src")
            dst = self._fresh("esc_unc")
            w.line(f"{unc_c} {src} = {src_expr};")
            w.line(f"{unc_c} {dst} = ({unc_c}){{0}};")
            val = self._clone_value_to_allocator(f"{src}.value", ty.inner, alloc_entry)
            unc = self._clone_value_to_allocator(f"{src}.uncertainty", ty.inner, alloc_entry)
            w.line(f"{dst}.value = {val};")
            w.line(f"{dst}.uncertainty = {unc};")
            return dst
        if isinstance(ty, TString):
            src = self._fresh("esc_str_src")
            dst = self._fresh("esc_str")
            w.line(f"mesa_str {src} = {src_expr};")
            w.line(f"mesa_str {dst} = (mesa_str){{\"\", 0}};")
            w.line(f"if ({src}.len > 0) {{")
            w.indent()
            buf = self._fresh("esc_buf")
            alloc = self._alloc_bytes_expr_for(alloc_entry, f"(size_t){src}.len", 1)
            w.line(f"char* {buf} = (char*){alloc};")
            w.line(f"memcpy({buf}, {src}.data, (size_t){src}.len);")
            w.line(f"{dst} = (mesa_str){{{buf}, {src}.len}};")
            w.dedent()
            w.line("}")
            return dst
        if isinstance(ty, TOptional):
            opt_c = c_type(ty)
            src = self._fresh("esc_opt_src")
            dst = self._fresh("esc_opt")
            w.line(f"{opt_c} {src} = {src_expr};")
            w.line(f"{opt_c} {dst} = ({opt_c}){{0}};")
            w.line(f"if ({src}.has_value) {{")
            w.indent()
            inner = self._clone_value_to_allocator(f"{src}.value", ty.inner, alloc_entry)
            w.line(f"{dst}.value = {inner};")
            w.line(f"{dst}.has_value = 1;")
            w.dedent()
            w.line("}")
            return dst
        if isinstance(ty, TVec):
            vec_c = c_type(ty)
            elem_c = c_type(ty.inner)
            align = self._alignment_of(ty.inner)
            src = self._fresh("esc_vec_src")
            dst = self._fresh("esc_vec")
            idx = self._fresh("esc_i")
            w.line(f"{vec_c} {src} = {src_expr};")
            w.line(f"{vec_c} {dst} = ({vec_c}){{NULL, {src}.len, {src}.len}};")
            w.line(f"if ({src}.len > 0) {{")
            w.indent()
            alloc = self._alloc_bytes_expr_for(
                alloc_entry,
                f"(size_t){src}.len * sizeof({elem_c})",
                align,
            )
            w.line(f"{dst}.data = ({elem_c}*){alloc};")
            w.line(f"for (int64_t {idx} = 0; {idx} < {src}.len; {idx}++) {{")
            w.indent()
            cloned = self._clone_value_to_allocator(f"{src}.data[{idx}]", ty.inner, alloc_entry)
            w.line(f"{dst}.data[{idx}] = {cloned};")
            w.dedent()
            w.line("}")
            w.dedent()
            w.line("}")
            return dst
        if isinstance(ty, TStruct):
            struct_c = c_type(ty)
            src = self._fresh("esc_struct_src")
            dst = self._fresh("esc_struct")
            w.line(f"{struct_c} {src} = {src_expr};")
            w.line(f"{struct_c} {dst} = ({struct_c}){{0}};")
            for fname, fty in ty.fields.items():
                cloned = self._clone_value_to_allocator(f"{src}.{fname}", fty, alloc_entry)
                w.line(f"{dst}.{fname} = {cloned};")
            return dst
        if isinstance(ty, TUnion):
            if not any(vt is not None for vt in ty.variants.values()):
                return src_expr
            union_c = c_type(ty)
            src = self._fresh("esc_union_src")
            dst = self._fresh("esc_union")
            w.line(f"{union_c} {src} = {src_expr};")
            w.line(f"{union_c} {dst} = ({union_c}){{0}};")
            w.line(f"{dst}.tag = {src}.tag;")
            w.line(f"switch ({src}.tag) {{")
            w.indent()
            for vname, vty in ty.variants.items():
                w.line(f"case {ty.name}_{vname}: {{")
                w.indent()
                if vty is not None:
                    field = self._union_payload_field(vname)
                    cloned = self._clone_value_to_allocator(f"{src}.payload.{field}", vty, alloc_entry)
                    w.line(f"{dst}.payload.{field} = {cloned};")
                w.line("break;")
                w.dedent()
                w.line("}")
            w.line("default: break;")
            w.dedent()
            w.line("}")
            return dst
        return src_expr

    def _emit_esc_expr(self, esc: EscExpr) -> str:
        esc_ty = self._expr_type(esc)
        if not isinstance(esc_ty, TErrorUnion) or not isinstance(esc_ty.error_set, (TErrorSet, TErrorSetUnion)):
            return "/* bad esc */"
        payload_ty = esc_ty.payload
        eset = esc_ty.error_set
        self._emit_result_struct(eset, payload_ty)
        key = f"{_error_key(eset)}_{_mangle_type(payload_ty)}"
        result_c = f"Mesa_result_{key}"
        result = self._fresh("esc")
        outer_alloc = self._fresh("outer_alloc")
        self.w.line(f"{result_c} {result};")
        self.w.line(f"Mesa_AllocContext* {outer_alloc} = mesa_allocctx_escape_target();")
        if self._esc_needs_allocator(payload_ty):
            cloned = self._clone_value_to_allocator(self._expr(esc.expr), payload_ty, outer_alloc)
            self.w.line(f"{result}.is_err = 0;")
            self.w.line(f"{result}.value = {cloned};")
        else:
            cloned = self._clone_value_to_allocator(self._expr(esc.expr), payload_ty, outer_alloc)
            self.w.line(f"{result}.is_err = 0;")
            self.w.line(f"{result}.value = {cloned};")
        return result

    def _with_alloc_expr(self, expr: WithAllocExpr) -> str:
        result_ty = self._expr_type(expr)
        alloc_ty = self._expr_type(expr.allocator)
        alloc_name = self._lvalue(expr.allocator)
        alloc_entry = (alloc_name, alloc_ty)
        if isinstance(result_ty, TPointer):
            inner_ty = result_ty.inner
            inner_c = c_type(inner_ty)
            align = self._alignment_of(inner_ty)
            tmp = self._fresh("with_alloc_ptr")
            init_val = self._expr(expr.expr)
            alloc = self._alloc_bytes_expr_for(alloc_entry, f"sizeof({inner_c})", align)
            self.w.line(f"{inner_c}* {tmp} = ({inner_c}*){alloc};")
            self.w.line(f"*{tmp} = {init_val};")
            return tmp
        return self._clone_value_to_allocator(self._expr(expr.expr), result_ty, alloc_entry)

    # ── Field access ──────────────────────────────────────────

    def _field_access(self, f: FieldExpr) -> str:
        obj = self._expr(f.obj)
        obj_ty = self._expr_type(f.obj)
        if isinstance(obj_ty, TPointer):
            obj_ty = obj_ty.inner
        if isinstance(obj_ty, (TErrorSet, TErrorSetUnion)):
            variants = error_set_variants(obj_ty)
            if f.field not in variants:
                obj_ty = None
            else:
                payload_ty = variants[f.field]
                owner = next((m for m in error_set_members(obj_ty) if f.field in m.variants), None)
                tag = self._error_tag_name(owner, f.field) if owner else "0"
                if payload_ty is None:
                    return f"(({obj}).tag == {tag})"
                mangle = _mangle_type(payload_ty)
                payload_c = c_type(payload_ty)
                self._emit_optional_type(payload_c, mangle)
                tmp = self._fresh("err_field")
                return (
                    "({ "
                    f"mesa_opt_{mangle} {tmp} = (mesa_opt_{mangle}){{0}}; "
                    f"if (({obj}).tag == {tag}) {{ "
                    f"memcpy(&{tmp}.value, ({obj}).payload, sizeof({tmp}.value)); "
                    f"{tmp}.has_value = 1; "
                    "} "
                    f"{tmp}; "
                    "})"
                )
        # If obj is a pointer type expression, use ->
        # Heuristic: if the Mesa type of f.obj is a pointer
        obj_is_ptr = self._expr_is_pointer(f.obj)
        arrow = "->" if obj_is_ptr else "."
        return f"{obj}{arrow}{f.field}"

    def _qualified_variant_expr(self, f: FieldExpr) -> Optional[str]:
        from src.ast import Ident
        from src.types import TUnion, TErrorSet, TErrorSetUnion

        if not isinstance(f.obj, Ident):
            return None

        type_ns = self.env.lookup_type(f.obj.name)
        if isinstance(type_ns, TUnion) and f.field in type_ns.variants:
            payload = type_ns.variants[f.field]
            type_c = c_type(type_ns)
            tag_const = f"{type_c}_{f.field}"
            if payload is not None:
                return None
            payload_needed = any(vty is not None for _, vty in type_ns.variants.items())
            if payload_needed:
                return f"(({type_c}){{{tag_const}}})"
            return tag_const

        if isinstance(type_ns, (TErrorSet, TErrorSetUnion)) and f.field in error_set_variants(type_ns):
            variants = error_set_variants(type_ns)
            if variants[f.field] is not None:
                return None
            owner = next((m for m in error_set_members(type_ns) if f.field in m.variants), None)
            return self._error_object_literal(type_ns, self._error_tag_name(owner, f.field))

        return None

    def _expr_is_pointer(self, expr: Expr) -> bool:
        """Best-effort check if expression has pointer type."""
        if isinstance(expr, UnaryExpr) and expr.op == "*":
            return False
        if isinstance(expr, UnaryExpr) and expr.op in ("@", "&"):
            return True
        if isinstance(expr, SelfExpr):
            # _ptr_params takes priority — set by _emit_fn for *Self params
            if "self" in self._ptr_params:
                return True
            sym = self.env.lookup("self")
            if sym and isinstance(sym.type_, TPointer):
                return True
        if isinstance(expr, Ident):
            # _ptr_params takes priority — tracks pointer params locally
            if expr.name in self._ptr_params:
                return True
            sym = self.env.lookup(expr.name)
            if sym and isinstance(sym.type_, TPointer):
                return True
        return False

    # ── Index ─────────────────────────────────────────────────

    def _index_expr(self, ix: IndexExpr) -> str:
        obj = self._expr(ix.obj)
        if not ix.indices:
            return obj
        idx = self._expr(ix.indices[0])
        # For vec/slice types, access .data[i]
        sym_ty = self._expr_type(ix.obj)
        if isinstance(sym_ty, (TVec, TSlice)):
            return f"{obj}.data[{idx}]"
        return f"{obj}[{idx}]"

    def _expr_type(self, expr: Expr) -> Optional[Type]:
        from src.types import T_I64, T_F64, T_BOOL, T_STR, TInt, TFloat, TBool, TString, TPointer
        from src.ast import IntLit, FloatLit, BoolLit, StringLit, BinExpr, UnaryExpr, SelfExpr
        if isinstance(expr, SelfExpr):
            # Look up 'self' symbol — the checker registers it in the method scope
            sym = self.env.lookup("self")
            if sym:
                ty = sym.type_
                return ty.inner if isinstance(ty, TPointer) else ty
            # Fallback: check _current_receiver set during _emit_fn
            if self._current_receiver:
                return self.env.lookup_type(self._current_receiver)
            return None
        resolved = getattr(expr, '_resolved_type', None)
        if resolved is not None and not isinstance(expr, (IntLit, FloatLit)):
            return resolved
        if isinstance(expr, Ident):
            # Prefer type annotated during checking
            if hasattr(expr, '_resolved_type') and expr._resolved_type is not None:
                return expr._resolved_type
            sym = self.env.lookup(expr.name)
            return sym.type_ if sym else None
        if isinstance(expr, IntLit):
            ty = getattr(expr, '_resolved_type', None)
            return ty if ty else T_I64
        if isinstance(expr, FloatLit):
            ty = getattr(expr, '_resolved_type', None)
            return ty if ty else T_F64
        if isinstance(expr, BoolLit):  return T_BOOL
        if isinstance(expr, StringLit): return T_STR
        if isinstance(expr, BinExpr):
            lt = self._expr_type(expr.left)
            rt = self._expr_type(expr.right)
            if lt and rt:
                if isinstance(lt, TFloat) or isinstance(rt, TFloat): return T_F64
                if isinstance(lt, TBool) and isinstance(rt, TBool):  return T_BOOL
                if isinstance(lt, TInt) and isinstance(rt, TInt):    return lt
            return lt or rt
        if isinstance(expr, UnaryExpr):
            return self._expr_type(expr.operand)
        if isinstance(expr, EscExpr):
            return getattr(expr, '_resolved_type', None)
        from src.ast import FieldExpr, CallExpr as CExpr, VariantLit as ELit, ArrayLit as ALit
        if isinstance(expr, ALit):
            from src.types import TArray as _TArray
            if not expr.elems:
                return _TArray(T_I64, 0)
            first = self._expr_type(expr.elems[0]) or T_I64
            return _TArray(first, len(expr.elems))
        if isinstance(expr, FieldExpr):
            if isinstance(expr.obj, Ident):
                type_ns = self.env.lookup_type(expr.obj.name)
                from src.types import TUnion as _TU, TErrorSet as _TES, TErrorSetUnion as _TESU
                if isinstance(type_ns, _TU) and expr.field in type_ns.variants:
                    return type_ns
                if isinstance(type_ns, (_TES, _TESU)) and expr.field in error_set_variants(type_ns):
                    return type_ns
            obj_ty = self._expr_type(expr.obj)
            if isinstance(obj_ty, TNamespace):
                value_ty = self.env.lookup_namespace_value(obj_ty.name, expr.field)
                if value_ty is not None:
                    return value_ty
                type_ty = self.env.lookup_namespace_type(obj_ty.name, expr.field)
                if type_ty is not None:
                    return type_ty
            from src.types import TStruct as _TS
            if isinstance(obj_ty, _TS):
                return obj_ty.field_type(expr.field)
            if isinstance(obj_ty, TPointer) and isinstance(obj_ty.inner, _TS):
                return obj_ty.inner.field_type(expr.field)
            if isinstance(obj_ty, TUncertain):
                if expr.field == "value":
                    if isinstance(obj_ty.inner, TUnitful):
                        return obj_ty.inner.inner
                    return obj_ty.inner
                if expr.field == "uncertainty":
                    return obj_ty.inner
                if expr.field == "units" and isinstance(obj_ty.inner, TUnitful):
                    return T_STR
            if isinstance(obj_ty, TPointer):
                obj_ty = obj_ty.inner
            if isinstance(obj_ty, (TErrorSet, TErrorSetUnion)) and expr.field in error_set_variants(obj_ty):
                payload = error_set_variants(obj_ty)[expr.field]
                if payload is None:
                    return T_BOOL
                return TOptional(payload)
        if isinstance(expr, ELit):
            return getattr(expr, '_resolved_type', None) or self.env.find_variant_type(expr.name)
        # UnitLit and UncertainLit — use checker-annotated type
        if isinstance(expr, UnitLit):
            return getattr(expr, '_resolved_type', None)
        return getattr(expr, '_resolved_type', None)

    def _intrinsic_target_type(self, expr: Expr) -> Optional[Type]:
        qname = self._qualified_name(expr)
        if qname is not None:
            ty = self.env.lookup_type(qname)
            if ty is not None:
                return ty.root() if isinstance(ty, TVar) else ty
        ty = self._expr_type(expr)
        return ty.root() if isinstance(ty, TVar) else ty

    def _is_allocator_operand_type(self, ty: Optional[Type]) -> bool:
        from src.types import TStruct, TPointer, TAnyInterface, TDynInterface
        if ty is None:
            return False
        if isinstance(ty, (TAnyInterface, TDynInterface)):
            return ty.iface.name == "Allocator"
        if isinstance(ty, TPointer):
            ty = ty.inner
        return isinstance(ty, TStruct) and self.env.impls.implements(ty.name, "Allocator")

    def _allocator_intrinsic_call(self, alloc_expr: Expr, op: str, *args: str) -> str:
        alloc_c = self._expr(alloc_expr)
        alloc_ty = self._expr_type(alloc_expr)
        from src.types import TStruct, TPointer, TAnyInterface, TDynInterface
        if isinstance(alloc_ty, TPointer) and isinstance(alloc_ty.inner, TStruct) and self.env.impls.implements(alloc_ty.inner.name, "Allocator"):
            method = op if op != "freeBytes" else "free_bytes"
            fn = f"{c_type(alloc_ty.inner)}__{method}"
            return f"{fn}({alloc_c}, {', '.join(args)})"
        if isinstance(alloc_ty, TStruct) and self.env.impls.implements(alloc_ty.name, "Allocator"):
            method = op if op != "freeBytes" else "free_bytes"
            fn = f"{c_type(alloc_ty)}__{method}"
            return f"{fn}(&{alloc_c}, {', '.join(args)})"
        if isinstance(alloc_ty, TAnyInterface) and alloc_ty.iface.name == "Allocator":
            data_expr = f"(({alloc_c}).is_inline ? (void*)({alloc_c}).inline_buf : ({alloc_c}).heap_ptr)"
            if op == "alloc":
                return f"(({alloc_c}).vtable->alloc({data_expr}, {args[0]}, {args[1]}))"
            if op == "realloc":
                return f"(({alloc_c}).vtable->realloc({data_expr}, {args[0]}, {args[1]}, {args[2]}, {args[3]}))"
            if op == "freeBytes":
                return f"(({alloc_c}).vtable->free_bytes({data_expr}, {args[0]}, {args[1]}), (void)0)"
        if isinstance(alloc_ty, TDynInterface) and alloc_ty.iface.name == "Allocator":
            data_expr = f"({alloc_c})->data"
            if op == "alloc":
                return f"(({alloc_c})->vtable->alloc({data_expr}, {args[0]}, {args[1]}))"
            if op == "realloc":
                return f"(({alloc_c})->vtable->realloc({data_expr}, {args[0]}, {args[1]}, {args[2]}, {args[3]}))"
            if op == "freeBytes":
                return f"(({alloc_c})->vtable->free_bytes({data_expr}, {args[0]}, {args[1]}), (void)0)"
        if op == "alloc":
            return f"mesa_c_alloc({args[0]}, {args[1]})"
        if op == "realloc":
            return f"mesa_c_realloc({args[0]}, {args[1]}, {args[2]}, {args[3]})"
        return f"mesa_c_free({args[0]})"
        if isinstance(expr, UncertainLit):
            return getattr(expr, '_resolved_type', None)
        if isinstance(expr, TupleLit):
            return getattr(expr, '_resolved_type', None) or getattr(expr, '_checked_type', None)
        if isinstance(expr, VecLit):
            ty = getattr(expr, '_resolved_type', None)
            if ty is not None:
                return ty
            if expr.elems:
                inner = self._expr_type(expr.elems[0])
                if inner is not None:
                    return TVec(inner, None)
            return None
        if isinstance(expr, VecComp):
            return getattr(expr, '_resolved_type', None)
        if isinstance(expr, WithAllocExpr):
            return getattr(expr, '_resolved_type', None)

        if isinstance(expr, CExpr):
            # Return type from checker annotation
            rt = getattr(expr, '_resolved_type', None)
            if rt is not None:
                return rt
            # Special internal calls
            if isinstance(expr.callee, Ident):
                if expr.callee.name == "__orelse" and len(expr.args) >= 2:
                    # orelse: ?T orelse T → T (type of the default value)
                    return self._expr_type(expr.args[1].value)
                if expr.callee.name == "__try" and expr.args:
                    # try: E!T → T
                    inner = self._expr_type(expr.args[0].value)
                    from src.types import TErrorUnion
                    if isinstance(inner, TErrorUnion):
                        return inner.payload
                    return inner
                sym = self.env.lookup(expr.callee.name)
                if sym and isinstance(sym.type_, TFun):
                    return sym.type_.ret
            if isinstance(expr.callee, FieldExpr):
                recv_ty = self._expr_type(expr.callee.obj)
                if isinstance(recv_ty, TPointer):
                    recv_ty = recv_ty.inner
                if isinstance(recv_ty, TStruct):
                    mt = recv_ty.method_type(expr.callee.field)
                    if isinstance(mt, TFun):
                        return mt.ret
        # IfExpr — use checker-annotated type
        from src.ast import IfExpr as IExpr, MatchExpr as MExpr
        if isinstance(expr, IExpr):
            return getattr(expr, '_resolved_type', None)
        if isinstance(expr, MExpr):
            return getattr(expr, '_resolved_type', None)
        if isinstance(expr, WithExpr):
            return getattr(expr, '_resolved_type', None)
        from src.ast import CallExpr as CExpr2, FieldExpr as FExpr
        from src.types import TDynInterface, TAnyInterface as _TAny2
        if isinstance(expr, CExpr2) and isinstance(expr.callee, FExpr):
            oty = self._expr_type(expr.callee.obj)
            if isinstance(oty, (TDynInterface, _TAny2)) and expr.callee.field in oty.iface.methods:
                return oty.iface.methods[expr.callee.field].ret
        if isinstance(expr, FExpr):
            oty = self._expr_type(expr.obj)
            if isinstance(oty, (TDynInterface, _TAny2)) and expr.field in oty.iface.methods:
                return oty.iface.methods[expr.field].ret
        # MatchExpr — infer from first non-void arm tail
        from src.ast import MatchExpr as MExpr
        if isinstance(expr, MExpr):
            for arm in expr.arms:
                if arm.body.tail:
                    ty = self._expr_type(arm.body.tail)
                    if ty is not None:
                        return ty
        return None

    # ── Calls ─────────────────────────────────────────────────

    def _emit_try_expr(self, inner_expr) -> str:
        """try <expr> — propagate error to caller, or unwrap ok value.
        
        Emits:
            Mesa_result_E_T _try0 = <inner>;
            if (_try0.is_err) return _try0;
            _try0.value
        """
        from src.types import TErrorUnion, TErrorSet
        inner_ty = self._expr_type(inner_expr)
        if not isinstance(inner_ty, TErrorUnion):
            return self._expr(inner_expr)   # not an error union — pass through
        eset = inner_ty.error_set
        self._emit_result_struct(eset, inner_ty.payload)
        key  = f"{_error_key(eset)}_{_mangle_type(inner_ty.payload)}"
        tmp  = self._fresh("try")
        val  = self._expr(inner_expr)
        self.w.line(f"Mesa_result_{key} {tmp} = {val};")
        target = self._try_targets[-1] if self._try_targets else getattr(self, '_current_fn_handle_target', None)
        if target is not None:
            label, err_target, err_type, excluded_cleanup = target
            self.w.line(f"if ({tmp}.is_err) {{")
            self.w.indent()
            if isinstance(err_type, (TErrorSet, TErrorSetUnion)):
                self._emit_error_object_assign_from_result(err_target, err_type, f"{tmp}.err")
            else:
                self.w.line(f"{err_target} = {tmp}.err.tag;")
            self._emit_cleanups_for_exit(error_exit=True, exclude_frame=excluded_cleanup)
            self.w.line(f"goto {label};")
            self.w.dedent()
            self.w.line("}")
        else:
            fn_ret = self._current_fn_ret_ty
            if isinstance(fn_ret, TErrorUnion):
                self.w.line(f"if ({tmp}.is_err) {{")
                self.w.indent()
                self._emit_cleanups_for_exit(error_exit=True)
                if fn_ret.error_set is None and inner_ty.error_set is not None:
                    self._emit_anyerror_return_from_result(tmp, inner_ty, fn_ret)
                elif fn_ret.error_set != inner_ty.error_set and error_set_contains(fn_ret.error_set, inner_ty.error_set):
                    self._emit_result_return_from_result(tmp, inner_ty, fn_ret)
                else:
                    self.w.line(f"return {tmp};")
                self.w.dedent()
                self.w.line("}")
        if isinstance(inner_ty.payload, TVoid):
            return "/* void */"
        return f"({tmp}.value)"

    def _emit_catch_expr(self, c: CallExpr) -> str:
        """expr catch { .V(p) => expr, _ => expr }"""
        from src.types import TErrorUnion, TErrorSet
        from src.ast import MatchExpr, PatVariant, PatWildcard
        inner_expr = c.args[0].value
        # __catch_bind: args = [inner, StringLit(binding), MatchExpr(arms)]
        # __catch:      args = [inner, MatchExpr(arms)]
        binding_name = None
        if c.callee.name == "__catch_bind" and len(c.args) >= 3:
            binding_name = c.args[1].value.raw if hasattr(c.args[1].value, 'raw') else None
            match_expr = c.args[2].value if len(c.args) > 2 else None
        else:
            match_expr = c.args[1].value if len(c.args) > 1 else None

        inner_ty = self._expr_type(inner_expr)
        if not isinstance(inner_ty, TErrorUnion):
            return self._expr(inner_expr)
        eset = inner_ty.error_set
        if not isinstance(eset, (TErrorSet, TErrorSetUnion)):
            return self._expr(inner_expr)

        self._emit_result_struct(eset, inner_ty.payload)
        key       = f"{_error_key(eset)}_{_mangle_type(inner_ty.payload)}"
        payload_c = c_type(inner_ty.payload)
        tmp    = self._fresh("catch")
        result = self._fresh("cresult")
        val    = self._expr(inner_expr)
        w      = self.w

        w.line(f"Mesa_result_{key} {tmp} = {val};")
        w.line(f"{payload_c} {result};")
        # If binding present, declare it as the error tag (for use in arms)
        if binding_name:
            w.line(f"uint16_t {binding_name} = {tmp}.err.tag;  /* catch |{binding_name}| */")
        w.line(f"if (!{tmp}.is_err) {{")
        w.indent()
        w.line(f"{result} = {tmp}.value;")
        w.dedent()
        w.line(f"}} else switch ({tmp}.err.tag) {{")
        w.indent()

        has_wildcard = False
        arms = match_expr.arms if isinstance(match_expr, MatchExpr) else []
        for arm in arms:
            pat  = arm.pattern
            body = arm.body
            if isinstance(pat, PatVariant):
                owner = next((m for m in error_set_members(eset) if pat.name in m.variants), None)
                tag = self._error_tag_name(owner, pat.name) if owner else "0"
                w.line(f"case {tag}: {{")
                w.indent()
                # Extract payload into binding if present
                payload_ty = error_set_variants(eset).get(pat.name)
                if pat.binding and payload_ty:
                    pname = pat.binding
                    w.line(f"{c_type(payload_ty)} {pname};")
                    w.line(f"memcpy(&{pname}, {tmp}.err.payload, sizeof({pname}));")
                tail = self._expr(body.tail) if body.tail else "0LL"
                w.line(f"{result} = {tail};")
                w.line("break;")
                w.dedent()
                w.line("}")
            elif isinstance(pat, PatWildcard):
                has_wildcard = True
                w.line("default: {")
                w.indent()
                tail = self._expr(body.tail) if body.tail else "0LL"
                w.line(f"{result} = {tail};")
                w.line("break;")
                w.dedent()
                w.line("}")

        if not has_wildcard:
            w.line("default: break;")
        w.dedent()
        w.line("}")
        return result

    def _test_compile_result_expr(self, c: CallExpr) -> str:
        result = getattr(c, "_test_compile_result", None) or {"ok": True, "errors": []}
        errors = result.get("errors", [])
        if not errors:
            return "((mesa_test_compile_result){ .ok = 1, .errors = { .data = NULL, .len = 0, .cap = 0 } })"

        entries = []
        for error in errors:
            entries.append(
                "{ "
                f".code = {_c_string_literal(error.get('code', ''))}, "
                f".message = {_c_string_literal(error.get('message', ''))}, "
                f".hint = {_c_string_literal(error.get('hint', ''))}, "
                f".line = {int(error.get('line', 0))}, "
                f".col = {int(error.get('col', 0))} "
                "}"
            )
        array_expr = f"(mesa_test_diagnostic[]){{ {', '.join(entries)} }}"
        count = len(entries)
        ok_value = "1" if result.get("ok", False) else "0"
        return (
            "((mesa_test_compile_result){ "
            f".ok = {ok_value}, "
            f".errors = {{ .data = {array_expr}, .len = {count}, .cap = {count} }} "
            "})"
        )

    def _call_expr(self, c: CallExpr) -> str:
        qname = self._qualified_name(c.callee)
        if qname in ("@test.compile", "@test.compileFile"):
            return self._test_compile_result_expr(c)
        # Payload variant constructor: .Variant(val)
        if isinstance(c.callee, VariantLit):
            return self._payload_variant_expr(c)

        qualified_variant = self._qualified_payload_variant_expr(c)
        if qualified_variant is not None:
            return qualified_variant

        if isinstance(c.callee, Ident):
            name = c.callee.name
            if name == "__orelse":
                opt = self._expr(c.args[0].value)
                alt = self._expr(c.args[1].value)
                return f"(({opt}).has_value ? ({opt}).value : {alt})"
            if name == "len":
                arg = self._expr(c.args[0].value) if c.args else "0"
                return f"({arg}).len"
            if name == "cap":
                arg = self._expr(c.args[0].value) if c.args else "0"
                return f"({arg}).cap"
            if name in ("@sizeOf", "@alignOf"):
                target_ty = self._intrinsic_target_type(c.args[0].value) if c.args else None
                target_c = c_type(target_ty) if target_ty is not None else "int64_t"
                op = "sizeof" if name == "@sizeOf" else "__alignof__"
                return f"((int64_t){op}({target_c}))"
            if name == "@hasField":
                has_field = bool(getattr(c, "_compile_time_bool", False))
                return "1" if has_field else "0"
            if name == "@assert":
                cond_expr = self._expr(c.args[0].value) if c.args else "0"
                span = getattr(c, "span", None)
                line = span.start.line if span is not None else 0
                col = span.start.col if span is not None else 0
                if getattr(c, "_assert_in_test", False):
                    return f"(mesa_test_assert(({cond_expr}) ? 1 : 0, {line}, {col}), (void)0)"
                return (
                    f"((({cond_expr}) ? (void)0 : "
                    f"mesa_panic((mesa_str){{\"assertion failed\", 16}})), (void)0)"
                )
            if name == "@pageSize":
                return "mesa_page_size()"
            if name == "@pageAlloc":
                size_expr = self._expr(c.args[0].value)
                return f"mesa_page_alloc({size_expr})"
            if name == "@pageFree":
                ptr_expr = self._expr(c.args[0].value)
                size_expr = self._expr(c.args[1].value)
                return f"(mesa_page_free({ptr_expr}, {size_expr}), (void)0)"
            if name == "@cAlloc":
                size_expr = self._expr(c.args[0].value)
                align_expr = self._expr(c.args[1].value)
                return f"mesa_c_alloc({size_expr}, {align_expr})"
            if name == "@cRealloc":
                ptr_expr = self._expr(c.args[0].value)
                old_size_expr = self._expr(c.args[1].value)
                new_size_expr = self._expr(c.args[2].value)
                align_expr = self._expr(c.args[3].value)
                return f"mesa_c_realloc({ptr_expr}, {old_size_expr}, {new_size_expr}, {align_expr})"
            if name == "@cFree":
                ptr_expr = self._expr(c.args[0].value)
                return f"(mesa_c_free({ptr_expr}), (void)0)"
            if name == "@ptrAdd":
                ptr_expr = self._expr(c.args[0].value)
                offset_expr = self._expr(c.args[1].value)
                return f"mesa_ptr_add({ptr_expr}, {offset_expr})"
            if name == "@alloc":
                size_expr = self._expr(c.args[1].value)
                align_expr = self._expr(c.args[2].value)
                return self._allocator_intrinsic_call(c.args[0].value, "alloc", size_expr, align_expr)
            if name == "@realloc":
                ptr_expr = self._expr(c.args[1].value)
                old_size_expr = self._expr(c.args[2].value)
                new_size_expr = self._expr(c.args[3].value)
                align_expr = self._expr(c.args[4].value)
                return self._allocator_intrinsic_call(c.args[0].value, "realloc", ptr_expr, old_size_expr, new_size_expr, align_expr)
            if name == "@freeBytes":
                ptr_expr = self._expr(c.args[1].value)
                size_expr = self._expr(c.args[2].value)
                return self._allocator_intrinsic_call(c.args[0].value, "freeBytes", ptr_expr, size_expr)
            if name == "@memcpy":
                dst = self._expr(c.args[0].value)
                src = self._expr(c.args[1].value)
                size_expr = self._expr(c.args[2].value)
                return f"memcpy({dst}, {src}, (size_t){size_expr})"
            if name == "@memmove":
                dst = self._expr(c.args[0].value)
                src = self._expr(c.args[1].value)
                size_expr = self._expr(c.args[2].value)
                return f"memmove({dst}, {src}, (size_t){size_expr})"
            if name == "@memset":
                dst = self._expr(c.args[0].value)
                byte_expr = self._expr(c.args[1].value)
                size_expr = self._expr(c.args[2].value)
                return f"memset({dst}, (int){byte_expr}, (size_t){size_expr})"
            if name == "@memcmp":
                lhs = self._expr(c.args[0].value)
                rhs = self._expr(c.args[1].value)
                size_expr = self._expr(c.args[2].value)
                return f"((int32_t)memcmp({lhs}, {rhs}, (size_t){size_expr}))"
            if name == "@panic":
                arg = self._expr(c.args[0].value) if c.args else '(mesa_str){"panic", 5}'
                return f"mesa_panic({arg})"
            if name == "__optional_chain":
                return self._expr(c.args[0].value)
            if name == "__try":
                return self._emit_try_expr(c.args[0].value)
            if name in ("__catch", "__catch_bind"):
                return self._emit_catch_expr(c)
            if name == "__format":
                return self._expr(c.args[0].value)
            if name in ("@typeof", "@typof"):
                ty = self._expr_type(c.args[0].value) if c.args else None
                text = format_type_for_user(ty) if ty is not None else "unknown"
                escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                return f'(mesa_str){{"{escaped}", {len(text)}}}'
            sym = getattr(c.callee, "_bound_symbol", None) or self.env.lookup(name)
            if name in ("println", "print") and getattr(sym, "pkg_path", None) is None:
                return self._println(c)

        # Method call — obj.method(args)
        if isinstance(c.callee, FieldExpr):
            from src.types import TNamespace
            if isinstance(self._expr_type(c.callee.obj), TNamespace):
                fn = self._expr(c.callee)
                args = ", ".join(self._expr(a.value) for a in c.args)
                return f"{fn}({args})"
            return self._method_call(c)

        # Generic call — queue monomorphisation, call by mangled name
        bindings = getattr(c, '_type_bindings', None)
        if bindings and isinstance(c.callee, Ident):
            fn_sym = self.env.lookup(c.callee.name)
            if fn_sym is None or not getattr(fn_sym, "decl_node", None):
                fn_sym = self.env.lookup_any_pkg_symbol(c.callee.name)
            if fn_sym and hasattr(fn_sym, 'decl_node') and fn_sym.decl_node:
                fn_decl = fn_sym.decl_node
                if getattr(fn_decl, '_type_params', []):
                    suffix = self._mono_suffix(bindings)
                    base_name = getattr(fn_decl, "_c_name", None) or (fn_sym.c_name or c.callee.name)
                    mono_name = f"{base_name}__{suffix}"
                    # Queue for emission after all regular decls
                    key = f"{base_name}__{suffix}"
                    if key not in self._mono_emitted:
                        self._pending_mono.append((fn_decl, bindings))
                    args_c = ", ".join(self._expr(a.value) for a in c.args)
                    return f"{mono_name}({args_c})"

        fn   = self._expr(c.callee)
        # Mangle user-defined functions that shadow C stdlib/keyword names
        _C_RESERVED = {"pow", "sqrt", "abs", "log", "exp", "sin", "cos", "tan",
                        "double", "float", "int", "char", "long", "short"}
        if isinstance(c.callee, Ident) and c.callee.name in _C_RESERVED:
            sym = self.env.lookup(c.callee.name)
            if sym is not None:  # user-defined, not builtin
                fn = sym.c_name or f"mesa_{c.callee.name}"
        # Coerce args: if param expects ?T and value is T, wrap as (mesa_opt_T){val, 1}
        callee_sym = self.env.lookup(c.callee.name) if isinstance(c.callee, Ident) else None
        param_types = (callee_sym.type_.params if callee_sym and isinstance(callee_sym.type_, TFun)
                       else [])
        args = []
        for i, a in enumerate(c.args):
            val = self._expr(a.value)
            if i < len(param_types):
                from src.types import TOptional as TOpt, is_assignable
                pt = param_types[i]
                at = self._expr_type(a.value)
                if (
                    isinstance(pt, TOpt)
                    and at is not None
                    and at != pt
                    and is_assignable(at, pt.inner)
                ):
                    mangle = _mangle_type(pt.inner)
                    val = f"(mesa_opt_{mangle}){{{val}, 1}}"
            args.append(val)
        return f"{fn}({', '.join(args)})"

    def _qualified_payload_variant_expr(self, c: CallExpr) -> Optional[str]:
        from src.ast import Ident
        from src.types import TUnion, TErrorSet, TErrorSetUnion, TTuple

        if not isinstance(c.callee, FieldExpr) or not isinstance(c.callee.obj, Ident):
            return None

        type_ns = self.env.lookup_type(c.callee.obj.name)
        if isinstance(type_ns, TUnion) and c.callee.field in type_ns.variants:
            payload_ty = type_ns.variants[c.callee.field]
            if payload_ty is None:
                return None
            type_c = c_type(type_ns)
            tag_const = f"{type_c}_{c.callee.field}"
            if isinstance(payload_ty, TTuple) and len(c.args) > 1:
                val = f"({c_type(payload_ty)}){{{', '.join(self._expr(a.value) for a in c.args)}}}"
            else:
                val = self._expr(c.args[0].value) if c.args else "0"
            raw_field = c.callee.field.lower()
            c_kws = {"int","float","double","char","long","short","void","unsigned",
                     "signed","const","static","extern","return","if","else",
                     "for","while","switch","case","break","continue","goto",
                     "sizeof","struct","union","enum","typedef","bool"}
            field = f"v_{raw_field}" if raw_field in c_kws else raw_field
            return f"(({type_c}){{{tag_const}, .payload.{field} = {val}}})"

        if isinstance(type_ns, (TErrorSet, TErrorSetUnion)) and c.callee.field in error_set_variants(type_ns):
            payload_ty = error_set_variants(type_ns)[c.callee.field]
            if payload_ty is None:
                return None
            val = self._expr(c.args[0].value) if c.args else "0"
            owner = next((m for m in error_set_members(type_ns) if c.callee.field in m.variants), None)
            return self._error_object_literal(
                type_ns,
                self._error_tag_name(owner, c.callee.field),
                val,
            )

        return None

    def _println(self, c: CallExpr) -> str:
        if not c.args:
            return 'printf("\\n")'
        arg   = c.args[0].value
        val   = self._expr(arg)
        ty    = self._expr_type(arg)
        # Unwrap unitful/uncertain to inner type for dispatch
        from src.types import TUnitful, TUncertain, TDynInterface, TAnyInterface as _TAny3, TVar as _TVpr, TFloatLit as _TFLpr, TFloat as _TF2
        if isinstance(ty, (_TAny3, TDynInterface)):
            ty = None
        # Resolve TVar from mono context
        if isinstance(ty, _TVpr) and ty.name and ty.name in self._mono_bindings:
            ty = self._mono_bindings[ty.name]
        # TFloatLit → concrete f64
        if isinstance(ty, _TFLpr):
            ty = _TF2(64)
        if isinstance(ty, TUnitful):
            ty = ty.inner
        if isinstance(ty, TUncertain):
            val = f"({val}).value"
            ty = ty.inner
        if isinstance(ty, TUnitful):
            ty = ty.inner
        if isinstance(ty, TString):
            return f"mesa_println_str({val})"
        if isinstance(ty, TFloat):
            return f"mesa_println_f64({val})"
        if isinstance(ty, TBool):
            return f"mesa_println_bool({val})"
        # Check if it's a string literal
        if isinstance(arg, StringLit):
            escaped = arg.raw.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            return f'mesa_println_cstr("{escaped}")'
        # Float literals and float-typed expressions
        if isinstance(arg, (FloatLit, UnitLit, UncertainLit)):
            return f"mesa_println_f64({val})"
        # Default: integer
        return f"mesa_println_i64((int64_t)({val}))"

    def _method_call(self, c: CallExpr) -> str:
        fe   = c.callee
        recv = self._expr(fe.obj)

        # Find the receiver type
        recv_ty = self._expr_type(fe.obj)
        from src.types import TStruct as _TStruct, TDynInterface

        # Dynamic dispatch through *Interface
        if isinstance(recv_ty, (TDynInterface, TAnyInterface)):
            from src.types import TAnyInterface as _TAny
            method = fe.field
            if isinstance(recv_ty, _TAny):
                # Stack existential: data is either inline_buf or *(void*)inline_buf
                data_expr = (f"({recv}).is_inline ? (void*)({recv}).inline_buf "
                             f": ({recv}).heap_ptr")
                args = ", ".join([f"({data_expr})"] +
                                 [self._expr(a.value) for a in c.args])
                return f"(({recv}).vtable->{method}({args}))"
            else:
                # Heap fat pointer
                args = ", ".join([f"({recv})->data"] +
                                 [self._expr(a.value) for a in c.args])
                return f"(({recv})->vtable->{method}({args}))"

        from src.types import TInt as _TIntM, TFloat as _TFloatM, TIntLit as _TIntLitM, TFloatLit as _TFloatLitM
        if fe.field == "sqrt":
            if isinstance(recv_ty, (_TIntM, _TFloatM, _TIntLitM, _TFloatLitM, TUnitful)):
                return f"sqrt({recv})"
            if isinstance(recv_ty, TUncertain):
                result_ty = self._expr_type(c)
                result_c = c_type(result_ty)
                recv_value = f"({recv}).value"
                recv_unc = f"({recv}).uncertainty"
                return (
                    f"({result_c}){{.value = sqrt({recv_value}), "
                    f".uncertainty = fabs({recv_unc}) / (2.0 * sqrt({recv_value}))}}"
                )

        obj_is_ptr = isinstance(recv_ty, TPointer)
        if obj_is_ptr:
            recv_ty = recv_ty.inner

        type_name = recv_ty.name if isinstance(recv_ty, (_TStruct, TUnion)) else None
        is_static_type_call = self._is_type_receiver_expr(fe.obj)

        if type_name:
            lookup_name = f"{type_name}.{fe.field}"
            mangled = None
            method_decl = None

            # Look up method type from: env global, impls registry, or struct.methods
            method_ty = None
            sym = self.env.lookup(lookup_name) or self.env.lookup_any_pkg_symbol(lookup_name)
            if sym and isinstance(sym.type_, TFun):
                method_ty = sym.type_
                method_decl = getattr(sym, "decl_node", None)
                mangled = sym.c_name or getattr(sym.decl_node, "_c_name", None) or lookup_name.replace(".", "__")
            if method_ty is None:
                iface_method = self.env.impls.find_method(type_name, fe.field)
                if isinstance(iface_method, TFun):
                    method_ty = iface_method
                    mangled = f"{c_type(recv_ty)}__{fe.field}"
            if method_ty is None and isinstance(recv_ty, TStruct):
                method_ty = recv_ty.method_type(fe.field)
            if mangled is None:
                mangled = f"{c_type(recv_ty)}__{fe.field}"

            if method_ty:
                bindings = getattr(c, "_type_bindings", None)
                if bindings is None and isinstance(recv_ty, TStruct) and recv_ty.type_args:
                    bindings = dict(recv_ty.type_args)
                if bindings and method_decl is not None and getattr(method_decl, "_type_params", []):
                    suffix = self._mono_suffix(bindings)
                    base_name = mangled
                    mangled = f"{base_name}__{suffix}"
                    key = f"{base_name}__{suffix}"
                    if key not in self._mono_emitted:
                        self._pending_mono.append((method_decl, bindings))
                if is_static_type_call or not method_ty.params:
                    args = ", ".join(self._expr(a.value) for a in c.args)
                    return f"{mangled}({args})"
                first_param = method_ty.params[0]
                # self: *T — pass address of receiver
                if isinstance(first_param, TPointer):
                    recv_expr = recv if obj_is_ptr else f"&{recv}"
                else:
                    recv_expr = recv
                args = ", ".join([recv_expr] + [self._expr(a.value) for a in c.args])
                return f"{mangled}({args})"

        # Fallback: function pointer field call
        args = ", ".join(self._expr(a.value) for a in c.args)
        arrow = "->" if obj_is_ptr else "."
        return f"{recv}{arrow}{fe.field}({args})"

    # ── Literals ──────────────────────────────────────────────

    def _payload_variant_expr(self, c) -> str:
        """Emit .Variant(payload) as a tagged union compound literal."""
        el = c.callee
        ty = getattr(el, '_resolved_type', None) or self.env.find_variant_type(el.name)
        from src.types import TUnion, TErrorSet, TErrorSetUnion, TTuple
        if isinstance(ty, (TErrorSet, TErrorSetUnion)):
            val = self._expr(c.args[0].value) if c.args else "0"
            owner = next((m for m in error_set_members(ty) if el.name in m.variants), None)
            return self._error_object_literal(ty, self._error_tag_name(owner, el.name), val)
        if not isinstance(ty, TUnion):
            return f"/* .{el.name}(...) */"
        type_c = c_type(ty)
        tag_const = f"{type_c}_{el.name}"
        payload_ty = ty.variants.get(el.name)
        if isinstance(payload_ty, TTuple) and len(c.args) > 1:
            val = f"({c_type(payload_ty)}){{{', '.join(self._expr(a.value) for a in c.args)}}}"
        else:
            val = self._expr(c.args[0].value) if c.args else "0"
        raw_field = el.name.lower()
        c_kws = {"int","float","double","char","long","short","void","unsigned",
                 "signed","const","static","extern","return","if","else",
                 "for","while","switch","case","break","continue","goto",
                 "sizeof","struct","union","enum","typedef","bool"}
        field = f"v_{raw_field}" if raw_field in c_kws else raw_field
        return f"(({type_c}){{{tag_const}, .payload.{field} = {val}}})"

    def _variant_lit(self, el: VariantLit) -> str:
        """Emit .Variant as a C value."""
        ty = getattr(el, '_resolved_type', None)
        if ty is None:
            ty = self.env.find_variant_type(el.name)
        from src.types import TUnion, TErrorSet, TErrorSetUnion
        if isinstance(ty, (TErrorSet, TErrorSetUnion)):
            owner = next((m for m in error_set_members(ty) if el.name in m.variants), None)
            return self._error_object_literal(ty, self._error_tag_name(owner, el.name))
        if not isinstance(ty, TUnion):
            return f"0 /* .{el.name} */"
        type_c = c_type(ty)
        tag_const = f"{type_c}_{el.name}"
        payload_needed = any(vty is not None for _, vty in ty.variants.items())
        if payload_needed:
            # Tagged struct — unit variant: zero-init payload
            return f"(({type_c}){{{tag_const}}})"
        else:
            # Unit-only union
            return tag_const

    def _tuple_lit(self, tl: TupleLit) -> str:
        fields = ", ".join(
            f".{name} = {self._expr(val)}" if name else self._expr(val)
            for name, val in tl.fields
        )
        # If we know the target struct type, emit a C compound literal
        # so it's valid in expression position (e.g. return .{...})
        resolved = getattr(tl, '_resolved_type', None)
        if resolved is None:
            resolved = getattr(tl, '_checked_type', None)
        from src.types import TStruct, TTuple
        if isinstance(resolved, (TStruct, TTuple)):
            return f"({c_type(resolved)}){{{fields}}}"
        return f"{{{fields}}}"

    def _array_lit(self, al: ArrayLit) -> str:
        elems = ", ".join(self._expr(e) for e in al.elems)
        return f"{{{elems}}}"

    def _vec_lit(self, vl: VecLit) -> str:
        vec_ty = self._expr_type(vl)
        if not isinstance(vec_ty, TVec):
            return "{NULL, 0, 0}"
        elem_ty = vec_ty.inner
        if isinstance(elem_ty, TVar) and vl.elems:
            elem_ty = self._expr_type(vl.elems[0]) or elem_ty
        elem_c = c_type(elem_ty)
        self._emit_vec_type(elem_c, _mangle_type(elem_ty))
        vec_c = c_type(vec_ty)
        align = self._alignment_of(elem_ty)
        if not vl.elems:
            return f"({vec_c}){{NULL, 0, 0}}"

        tmp = self._fresh("vec")
        data = self._fresh("vec_data")
        size_expr = f"sizeof({elem_c}) * (size_t){len(vl.elems)}"
        lines = [
            f"{vec_c} {tmp}",
            f"{elem_c}* {data} = ({elem_c}*){self._alloc_bytes_expr(size_expr, align)}",
            f"if (!{data}) {{ fprintf(stderr, \"vec literal: OOM\\n\"); abort(); }}",
        ]
        for i, elem in enumerate(vl.elems):
            lines.append(f"{data}[{i}] = {self._expr(elem)}")
        lines.extend([
            f"{tmp}.data = {data}",
            f"{tmp}.len = {len(vl.elems)}",
            f"{tmp}.cap = {len(vl.elems)}",
            tmp,
        ])
        return "({ " + "; ".join(lines) + "; })"

    def _vec_comp(self, vc: VecComp) -> str:
        vec_ty = self._expr_type(vc)
        if not isinstance(vec_ty, TVec):
            return "{NULL, 0, 0}"
        elem_ty = vec_ty.inner
        elem_c = c_type(elem_ty)
        self._emit_vec_type(elem_c, _mangle_type(elem_ty))
        vec_c = c_type(vec_ty)
        align = self._alignment_of(elem_ty)

        iter_var = self._fresh("iter")
        idx = self._fresh("i")
        result = self._fresh("vec")
        new_cap = self._fresh("cap")
        oom = "vec comprehension: OOM\\n"
        parts = [
            f"__auto_type {iter_var} = {self._expr(vc.iter)}",
            f"{vec_c} {result}",
            f"{result}.len = 0",
            f"{result}.cap = ({iter_var}.len > 0 ? {iter_var}.len : 4)",
            f"{result}.data = ({elem_c}*){self._alloc_bytes_expr(f'sizeof({elem_c}) * (size_t){result}.cap', align)}",
            f"if (!{result}.data) {{ fprintf(stderr, \"{oom}\"); abort(); }}",
            f"for (int64_t {idx} = 0; {idx} < {iter_var}.len; {idx}++) {{",
        ]
        if isinstance(vc.pattern, PatIdent):
            parts.append(f"__auto_type {vc.pattern.name} = {iter_var}.data[{idx}]")
        elif isinstance(vc.pattern, PatRef):
            parts.append(f"__auto_type* {vc.pattern.name} = &{iter_var}.data[{idx}]")
        elif isinstance(vc.pattern, PatTuple):
            for j, name in enumerate(vc.pattern.names):
                parts.append(f"__auto_type {name} = {iter_var}.data[{idx}]._{j}")
        if vc.filter:
            parts.append(f"if (!({self._expr(vc.filter)})) continue")
        parts.extend([
            f"if ({result}.len == {result}.cap) {{",
            f"int64_t {new_cap} = ({result}.cap > 0 ? {result}.cap * 2 : 4)",
            f"{result}.data = ({elem_c}*){self._realloc_bytes_expr(f'{result}.data', f'sizeof({elem_c}) * (size_t){result}.cap', f'sizeof({elem_c}) * (size_t){new_cap}', align)}",
            f"if (!{result}.data) {{ fprintf(stderr, \"{oom}\"); abort(); }}",
            f"{result}.cap = {new_cap}",
            "}",
            f"{result}.data[{result}.len++] = {self._expr(vc.expr)}",
            "}",
            result,
        ])
        return "({ " + "; ".join(parts) + "; })"

    # ── If / match ────────────────────────────────────────────

    def _if_expr(self, ie: IfExpr) -> str:
        cond = self._expr(ie.cond)
        # Simple ternary for single-expression bodies
        then_tail = ie.then_block.tail
        else_tail = ie.else_block.tail if ie.else_block else None

        if (then_tail and not ie.then_block.stmts and
                else_tail and not ie.else_block.stmts):
            # Suffix ternary: else is NoneLit → (cond ? (mesa_opt_T){val,1} : (mesa_opt_T){0,0})
            if isinstance(else_tail, NoneLit):
                then_ty = self._expr_type(then_tail)
                if then_ty is not None:
                    raw_then_ty = self._expr_type_raw(then_tail) or then_ty
                    val = self._expr(then_tail)
                    if isinstance(then_ty, TOptional):
                        mangle = _mangle_type(then_ty.inner)
                        inner_c = c_type(then_ty.inner)
                        self._emit_optional_type(inner_c, mangle)
                        if not isinstance(raw_then_ty, TOptional):
                            val = f"(mesa_opt_{mangle}){{{val}, 1}}"
                        return (f"(({cond}) ? "
                                f"{val} : "
                                f"(mesa_opt_{mangle}){{0, 0}})")
                    mangle = _mangle_type(raw_then_ty)
                    inner_c = c_type(raw_then_ty)
                    self._emit_optional_type(inner_c, mangle)  # ensure typedef exists
                    return (f"(({cond}) ? "
                            f"(mesa_opt_{mangle}){{{val}, 1}} : "
                            f"(mesa_opt_{mangle}){{0, 0}})")
                # Fallback to multi-statement path
            else:
                then_c = self._expr(then_tail)
                else_c = self._expr(else_tail)
                if then_c in ("/* void */", "/* undef */") and else_c in ("/* void */", "/* undef */"):
                    return "/* void */"
                if then_c in ("/* void */", "/* undef */") or else_c in ("/* void */", "/* undef */"):
                    # One branch emits control flow or statements, so fall back
                    # to the statement-based lowering below.
                    pass
                else:
                    return f"({cond} ? {then_c} : {else_c})"

        # Multi-statement: emit as GNU statement expression or use temp var
        tmp = self._fresh("if")
        w   = self.w
        # Determine type from then branch tail
        ret_ty = self._infer_c_type(then_tail) if then_tail else "void"

        # Detect void tail WITHOUT emitting — check AST structure
        if then_tail:
            ret_ty = self._tail_type(then_tail)

        if ret_ty != "void":
            w.line(f"{ret_ty} {tmp};")
            w.line(f"if ({cond}) {{")
            w.indent()
            self._emit_block_body(ie.then_block, assign_target=tmp)
            w.dedent()
            w.line("} else {")
            w.indent()
            if ie.else_block:
                self._emit_block_body(ie.else_block, assign_target=tmp)
            else:
                w.line(f"{tmp} = {_zero(ret_ty)};")
            w.dedent()
            w.line("}")
            return tmp
        else:
            w.line(f"if ({cond}) {{")
            w.indent()
            self._emit_block_stmts(ie.then_block)
            w.dedent()
            if ie.else_block:
                w.line("} else {")
                w.indent()
                self._emit_block_stmts(ie.else_block)
                w.dedent()
            w.line("}")
            return "/* void */"

    def _if_unwrap(self, ie: IfUnwrap) -> str:
        opt  = self._expr(ie.expr)
        w    = self.w
        tmp  = self._fresh("opt")
        ret_ty = "void"

        then_tail = ie.then_block.tail
        if then_tail:
            ret_ty = self._tail_type(then_tail)

        if ret_ty != "void":
            w.line(f"{ret_ty} {tmp};")

        w.line(f"if (({opt}).has_value) {{")
        w.indent()
        if ie.is_ref:
            w.line(f"__auto_type {ie.binding} = &({opt}).value;")
        else:
            w.line(f"__auto_type {ie.binding} = ({opt}).value;")
        self._emit_block_body(ie.then_block, assign_target=tmp if ret_ty != "void" else None)
        w.dedent()

        if ie.else_block:
            w.line("} else {")
            w.indent()
            self._emit_block_body(ie.else_block, assign_target=tmp if ret_ty != "void" else None)
            w.dedent()

        w.line("}")
        return tmp if ret_ty != "void" else "/* void */"

    def _match_expr(self, me: MatchExpr) -> str:
        val    = self._expr(me.value)
        val_ty = getattr(me, '_checked_type', None)
        match_ty = self._expr_type(me)
        w      = self.w
        tmp    = self._fresh("match")

        # Determine result type from first non-void arm
        ret_ty = "void"
        for arm in me.arms:
            if arm.body.tail:
                mesa_ty = self._expr_type(arm.body.tail)
                if mesa_ty is not None:
                    from src.types import TVoid
                    if isinstance(mesa_ty, TVoid):
                        ret_ty = "void"
                    else:
                        ret_ty = c_type(mesa_ty)
                else:
                    ret_ty = self._infer_c_type(arm.body.tail)
                break

        if ret_ty != "void":
            from src.types import TStruct as _TS_match, TTuple as _TT_match, TUnion as _TU_match
            if isinstance(match_ty, (_TS_match, _TT_match, _TU_match)):
                w.line(f"{ret_ty} {tmp} = ({ret_ty}){{0}};")
            else:
                w.line(f"{ret_ty} {tmp} = {_zero(ret_ty)};")

        # Union match → switch on tag (or directly for unit-only unions)
        if isinstance(val_ty, TUnion):
            tmp_val = self._fresh("mval")
            has_payload = any(vt is not None for vt in val_ty.variants.values())
            w.line(f"__auto_type {tmp_val} = {val};")
            if has_payload:
                w.line(f"switch ({tmp_val}.tag) {{")
            else:
                w.line(f"switch ({tmp_val}) {{")
            w.indent()
            for i, (vname, vtype) in enumerate(val_ty.variants.items()):
                # Find the arm for this variant
                arm = next((a for a in me.arms
                           if isinstance(a.pattern, (PatVariant, PatIdent))
                           and getattr(a.pattern, 'name', None) == vname), None)
                if arm is None:
                    arm = next((a for a in me.arms
                               if isinstance(a.pattern, (PatWildcard, PatIdent))
                               and getattr(a.pattern, 'name', None) not in val_ty.variants), None)
                if arm:
                    w.line(f"case {c_type(val_ty)}_{vname}: {{")
                    w.indent()
                    # Bind payload if variant has one
                    if isinstance(arm.pattern, PatVariant) and (arm.pattern.binding or arm.pattern.extra_bindings) and vtype:
                        field = self._union_payload_field(vname)
                        self._emit_pattern_payload_bindings_from_bytes(
                            arm.pattern,
                            vtype,
                            f"&{tmp_val}.payload.{field}"
                        )
                    self._emit_block_body(arm.body, assign_target=tmp if ret_ty != "void" else None)
                    w.line("break;")
                    w.dedent()
                    w.line("}")
            w.dedent()
            w.line("}")

        # Integer / bool match → switch
        elif isinstance(val_ty, (TErrorSet, TErrorSetUnion)):
            tmp_val = self._fresh("merr")
            w.line(f"__auto_type {tmp_val} = {val};")
            w.line(f"switch ({tmp_val}.tag) {{")
            w.indent()
            has_default = False
            for arm in me.arms:
                if isinstance(arm.pattern, PatVariant):
                    owner = next((m for m in error_set_members(val_ty) if arm.pattern.name in m.variants), None)
                    tag = self._error_tag_name(owner, arm.pattern.name) if owner else "0"
                    w.line(f"case {tag}: {{")
                else:
                    w.line("default: {")
                    has_default = True
                w.indent()
                if isinstance(arm.pattern, PatVariant):
                    payload_ty = error_set_variants(val_ty).get(arm.pattern.name)
                    if payload_ty is not None:
                        self._emit_pattern_payload_bindings_from_bytes(
                            arm.pattern, payload_ty, f"{tmp_val}.payload"
                        )
                self._emit_block_body(arm.body, assign_target=tmp if ret_ty != "void" else None)
                w.line("break;")
                w.dedent()
                w.line("}")
            if not has_default:
                w.line("default: break;")
            w.dedent()
            w.line("}")

        # Integer / bool match → switch
        elif isinstance(val_ty, (TInt, TBool, TIntLit)):
            w.line(f"switch ({val}) {{")
            w.indent()
            has_default = False
            for arm in me.arms:
                if isinstance(arm.pattern, (PatWildcard, PatIdent)):
                    w.line("default:")
                    has_default = True
                elif isinstance(arm.pattern, PatInt):
                    w.line(f"case {arm.pattern.value}LL:")
                elif isinstance(arm.pattern, PatBool):
                    w.line(f"case {'1' if arm.pattern.value else '0'}:")
                else:
                    w.line("default:")
                    has_default = True
                w.indent()
                if isinstance(arm.pattern, PatIdent):
                    w.line(f"{c_type(val_ty)} {arm.pattern.name} = {val};")
                self._emit_block_body(arm.body, assign_target=tmp if ret_ty != "void" else None)
                w.line("break;")
                w.dedent()
            if not has_default:
                w.line("default: break;")
            w.dedent()
            w.line("}")

        # Fallback: if-else chain
        else:
            first = True
            for arm in me.arms:
                if isinstance(arm.pattern, (PatWildcard, PatIdent)):
                    if not first: w.line("} else {")
                    else:         w.line("{")
                else:
                    cond = self._pattern_cond(arm.pattern, val)
                    if first: w.line(f"if ({cond}) {{")
                    else:     w.line(f"}} else if ({cond}) {{")
                w.indent()
                self._emit_block_body(arm.body, assign_target=tmp if ret_ty != "void" else None)
                w.dedent()
                first = False
            if not first:
                w.line("}")

        return tmp if ret_ty != "void" else "/* void */"

    def _pattern_cond(self, pat: MatchPattern, val: str) -> str:
        if isinstance(pat, PatWildcard): return "1"
        if isinstance(pat, PatInt):      return f"({val} == {pat.value}LL)"
        if isinstance(pat, PatBool):     return f"({val} == {'1' if pat.value else '0'})"
        if isinstance(pat, PatVariant):  return f"({val}.tag == {pat.name})"
        return "1"

    def _block_expr(self, be: BlockExpr) -> str:
        result_ty = self._expr_type(be)
        saved_writer = self.w
        expr_writer = Writer()
        self.w = expr_writer
        if result_ty is None or isinstance(result_ty, TVoid):
            self._emit_block_body(be.block)
            self.w = saved_writer
            body = expr_writer.output().strip()
            if not body:
                return "/* void */"
            return "({\n" + body + "\n(void)0;\n})"
        result_name = self._fresh("block")
        result_c = c_type(result_ty)
        self.w.line(f"{result_c} {result_name} = ({result_c}){{0}};")
        self._emit_block_body(be.block, assign_target=result_name, assign_target_ty=result_ty)
        self.w.line(f"{result_name};")
        self.w = saved_writer
        return "({\n" + expr_writer.output().strip() + "\n})"

    def _with_expr(self, we: WithExpr) -> str:
        w = self.w
        alloc_ty = self._expr_type(we.resource)
        alloc_name = self._lvalue(we.resource)
        prev_alloc = self._active_alloc
        from src.types import TVoid
        allocctx_frame = None
        if self._is_allocator_operand_type(alloc_ty):
            self._active_alloc = (alloc_name, alloc_ty)
            self._allocator_stack.append(self._active_alloc)
            allocctx_helper = self._allocctx_helper_name(alloc_ty)
            if allocctx_helper is not None:
                w.line(f"mesa_allocctx_push(&{alloc_name}, {allocctx_helper});")
            allocctx_frame = {
                "kind": "allocctx",
                "loop_depth": len(self._loop_stack),
            }
            self._cleanup_frames.append(allocctx_frame)

        result_ty = self._expr_type(we)
        result_is_void = (result_ty is None or isinstance(result_ty, TVoid))
        result_name = self._fresh("with")
        if not result_is_void:
            result_c = c_type(result_ty)
            w.line(f"{result_c} {result_name} = ({result_c}){{0}};")

        handle_binding_ty = getattr(we.handle, '_binding_type', None) if we.handle else None
        err_name = self._fresh("with_err") if we.handle else None
        handle_label = self._fresh("with_handle") if we.handle else None
        cleanup_label = self._fresh("with_cleanup") if we.cleanup else None
        done_label = self._fresh("with_done") if we.handle else None

        if err_name:
            if isinstance(handle_binding_ty, (TErrorSet, TErrorSetUnion)):
                self._emit_error_object_type(handle_binding_ty)
                w.line(f"{c_type(handle_binding_ty)} {err_name} = ({c_type(handle_binding_ty)}){{0}};")
            else:
                w.line(f"uint16_t {err_name} = 0;")

        cleanup_frame = None
        if we.cleanup:
            cleanup_frame = {
                "alloc_name": alloc_name,
                "alloc_ty": alloc_ty,
                "cleanup": we.cleanup,
                "loop_depth": len(self._loop_stack),
            }
            self._cleanup_frames.append(cleanup_frame)

        if we.handle and handle_label and err_name:
            self._try_targets.append((
                handle_label,
                err_name,
                handle_binding_ty if isinstance(handle_binding_ty, (TErrorSet, TErrorSetUnion)) else None,
                cleanup_frame,
            ))

        w.line("{  /* with expr */")
        w.indent()
        self._emit_block_body(
            we.body,
            assign_target=result_name if not result_is_void else None,
            assign_target_ty=result_ty,
        )

        if we.handle:
            self._try_targets.pop()
            if cleanup_label:
                w.line(f"goto {cleanup_label};")
            elif done_label:
                w.line(f"goto {done_label};")
            w.dedent()
            w.line(f"{handle_label}:;")
            w.indent()
            binding_c = c_type(handle_binding_ty) if handle_binding_ty is not None else "int64_t"
            w.line(f"{binding_c} {we.handle.binding} = {err_name};")
            self._emit_block_body(
                we.handle.body,
                assign_target=result_name if not result_is_void else None,
                assign_target_ty=result_ty,
            )

        if we.cleanup and cleanup_label:
            w.dedent()
            w.line(f"{cleanup_label}:;")
            w.indent()
            self._emit_cleanup_call(cleanup_frame)

        if allocctx_frame is not None:
            self._emit_cleanup_call(allocctx_frame)

        if we.handle and done_label:
            w.dedent()
            w.line(f"{done_label}:;")
            w.indent()

        w.dedent()
        w.line("}")

        if cleanup_frame is not None:
            self._cleanup_frames.pop()
        if allocctx_frame is not None:
            self._cleanup_frames.pop()
        if self._is_allocator_operand_type(alloc_ty) and self._allocator_stack:
            self._allocator_stack.pop()
        self._active_alloc = prev_alloc
        return result_name if not result_is_void else "/* void */"

    # ── Closure ───────────────────────────────────────────────

    def _coerce_to_dyn(self, expr, dyn_ty) -> str:
        """Coerce concrete type to any Interface (stack) or *any Interface (heap)."""
        from src.types import TStruct, TDynInterface, TAnyInterface, MESA_SBO_SIZE
        w = self.w

        concrete_ty = self._expr_type_raw(expr)
        if isinstance(concrete_ty, TStruct):
            concrete_name = concrete_ty.name
            concrete_c_name = c_type(concrete_ty)
        else:
            iface_box = self._iface_box_name(dyn_ty.iface)
            if isinstance(dyn_ty, TDynInterface):
                return f"({iface_box}*)({self._expr_raw(expr)})"
            return self._expr_raw(expr)

        iface_name = self._iface_type_name(dyn_ty.iface)
        iface_box = self._iface_box_name(dyn_ty.iface)
        iface_any = self._iface_any_name(dyn_ty.iface)
        iface_vtable = self._iface_vtable_name(dyn_ty.iface)
        vtable_owner  = concrete_c_name
        vtable_name   = f"_mesa_vtable_{vtable_owner}_{iface_name}"
        val           = self._expr_raw(expr)
        tmp_data      = self._fresh("dyn_data")

        if isinstance(dyn_ty, TDynInterface):
            # *any Interface — heap allocate { vtable_ptr, data... }
            tmp_ptr = self._fresh("dyn")
            w.line(f"{concrete_c_name} {tmp_data} = {val};")
            w.line(f"{iface_box}* {tmp_ptr} = ({iface_box}*)"
                   f"malloc(sizeof({iface_vtable}*) + sizeof({concrete_c_name}));")
            w.line(f"{tmp_ptr}->vtable = &{vtable_name};")
            w.line(f"memcpy({tmp_ptr}->data, &{tmp_data}, sizeof({concrete_c_name}));")
            return tmp_ptr
        else:
            # any Interface — stack existential with SBO
            tmp_ex = self._fresh("ex")
            w.line(f"{concrete_c_name} {tmp_data} = {val};")
            w.line(f"{iface_any} {tmp_ex};")
            w.line(f"{tmp_ex}.vtable = &{vtable_name};")
            # Check concrete type size for SBO vs heap
            # Check concrete type size for SBO decision
            size = None
            if self.layout:
                from src.types import TStruct as _TS2
                ct2 = self.env.lookup_type(concrete_name)
                if isinstance(ct2, _TS2):
                    try:
                        lo = self.layout.layout_of(ct2)
                        size = lo.size
                    except Exception:
                        pass
            if size is not None and size <= MESA_SBO_SIZE:
                w.line(f"memcpy({tmp_ex}.inline_buf, &{tmp_data}, sizeof({concrete_c_name}));")
                w.line(f"{tmp_ex}.is_inline = 1;")
            else:
                w.line(f"{tmp_ex}.heap_ptr = malloc(sizeof({concrete_c_name}));")
                w.line(f"memcpy({tmp_ex}.heap_ptr, &{tmp_data}, sizeof({concrete_c_name}));")
                w.line(f"{tmp_ex}.is_inline = 0;")
            return tmp_ex

    def _expr_raw(self, expr) -> str:
        """Emit expression WITHOUT the TDynInterface coercion intercept."""
        # Temporarily clear _resolved_type to skip the coercion check
        saved = getattr(expr, '_resolved_type', None)
        if saved is not None:
            expr._resolved_type = None
        result = self._expr(expr)
        if saved is not None:
            expr._resolved_type = saved
        return result

    def _expr_type_raw(self, expr):
        """Get the concrete type of expr before any coercion."""
        from src.ast import Ident
        if isinstance(expr, Ident):
            sym = self.env.lookup(expr.name)
            if sym:
                pre = getattr(expr, '_pre_coerce_type', None)
                return pre if pre is not None else sym.type_
        # CallExpr resolving to allocator type
        rt = getattr(expr, '_pre_coerce_type', None)
        if rt is not None: return rt
        rt = getattr(expr, '_resolved_type', None)
        return rt

    def _collect_closure_captures(self, c) -> List[Tuple[str, Type, str]]:
        from src.ast import (
            AssignStmt, BinExpr, Block, BreakStmt, CallExpr, Closure, ContinueStmt,
            DeferStmt, ExprStmt, FieldExpr, ForIterStmt, ForRangeStmt, HandleBlock,
            Ident, IfExpr, IndexExpr, LetStmt, MatchExpr, ReturnStmt, UnaryExpr,
            WhileStmt, WithAllocExpr, EscExpr, ComptimeExpr,
        )

        builtins = {"print", "println", "len", "cap"}
        params = {p.name for p in c.params}
        captures: Dict[str, Tuple[Type, str]] = {}

        def bind_pattern(scope: Set[str], pat):
            if pat is None:
                return
            if isinstance(pat, PatIdent):
                scope.add(pat.name)
            elif isinstance(pat, PatRef):
                scope.add(pat.name)
            elif isinstance(pat, PatTuple):
                scope.update(pat.names)

        def bind_match_pattern(scope: Set[str], pat):
            if isinstance(pat, PatVariant):
                if pat.binding:
                    scope.add(pat.binding)
                scope.update(pat.extra_bindings or [])
            elif isinstance(pat, PatIdent):
                scope.add(pat.name)

        def is_local(name: str, scopes: List[Set[str]]) -> bool:
            return any(name in scope for scope in reversed(scopes))

        def visit_expr(expr, scopes: List[Set[str]]):
            if expr is None:
                return
            if isinstance(expr, Ident):
                sym = getattr(expr, "_bound_symbol", None) or self.env.lookup(expr.name)
                if (
                    sym is not None
                    and expr.name not in builtins
                    and not expr.name.startswith("@")
                    and sym.pkg_path is None
                    and not is_local(expr.name, scopes)
                ):
                    source_name = sym.c_name or expr.name
                    captures.setdefault(expr.name, (sym.type_, source_name))
                return
            if isinstance(expr, Closure):
                return
            if isinstance(expr, BinExpr):
                visit_expr(expr.left, scopes)
                visit_expr(expr.right, scopes)
                return
            if isinstance(expr, UnaryExpr):
                visit_expr(expr.operand, scopes)
                return
            if isinstance(expr, FieldExpr):
                visit_expr(expr.obj, scopes)
                return
            if isinstance(expr, IndexExpr):
                visit_expr(expr.obj, scopes)
                for idx in expr.indices:
                    visit_expr(idx, scopes)
                return
            if isinstance(expr, CallExpr):
                visit_expr(expr.callee, scopes)
                for arg in expr.args:
                    visit_expr(arg.value, scopes)
                return
            if isinstance(expr, IfExpr):
                visit_expr(expr.cond, scopes)
                visit_block(expr.then_block, scopes)
                visit_block(expr.else_block, scopes)
                return
            if isinstance(expr, MatchExpr):
                visit_expr(expr.value, scopes)
                for arm in expr.arms:
                    arm_scope = set()
                    bind_match_pattern(arm_scope, arm.pattern)
                    visit_block(arm.body, scopes + [arm_scope])
                return
            if isinstance(expr, WithAllocExpr):
                visit_expr(expr.expr, scopes)
                visit_expr(expr.allocator, scopes)
                return
            if isinstance(expr, (EscExpr, ComptimeExpr)):
                visit_expr(expr.expr, scopes)
                return
            for attr in ("left", "right", "operand", "obj", "callee", "value", "init",
                         "expr", "cond", "body", "handle", "block", "iter", "filter",
                         "start", "end"):
                child = getattr(expr, attr, None)
                if isinstance(child, Block):
                    visit_block(child, scopes)
                elif hasattr(child, "__dict__"):
                    visit_expr(child, scopes)
            for attr in ("stmts", "args", "elems", "fields", "arms", "params"):
                for child in getattr(expr, attr, []):
                    if hasattr(child, "value") and hasattr(child.value, "__dict__"):
                        visit_expr(child.value, scopes)
                    elif hasattr(child, "__dict__"):
                        visit_expr(child, scopes)

        def visit_stmt(stmt, scopes: List[Set[str]]):
            if isinstance(stmt, LetStmt):
                visit_expr(stmt.init, scopes)
                scopes[-1].add(stmt.name)
            elif isinstance(stmt, ReturnStmt):
                visit_expr(stmt.value, scopes)
            elif isinstance(stmt, AssignStmt):
                visit_expr(stmt.target, scopes)
                visit_expr(stmt.value, scopes)
            elif isinstance(stmt, ExprStmt):
                visit_expr(stmt.expr, scopes)
            elif isinstance(stmt, ForRangeStmt):
                visit_expr(stmt.start, scopes)
                visit_expr(stmt.end, scopes)
                visit_expr(stmt.filter, scopes)
                visit_block(stmt.body, scopes + [{stmt.var}])
            elif isinstance(stmt, ForIterStmt):
                visit_expr(stmt.iter, scopes)
                visit_expr(stmt.filter, scopes)
                loop_scope: Set[str] = set()
                bind_pattern(loop_scope, stmt.pattern)
                visit_block(stmt.body, scopes + [loop_scope])
            elif isinstance(stmt, WhileStmt):
                visit_expr(stmt.cond, scopes)
                visit_block(stmt.body, scopes)
            elif isinstance(stmt, DeferStmt):
                visit_block(stmt.body, scopes)
            elif isinstance(stmt, (BreakStmt, ContinueStmt)):
                return

        def visit_block(block, scopes: List[Set[str]]):
            if block is None:
                return
            block_scope: Set[str] = set()
            local_scopes = scopes + [block_scope]
            for stmt in block.stmts:
                visit_stmt(stmt, local_scopes)
            visit_expr(block.tail, local_scopes)

        visit_block(c.body, [set(params)])
        return [(name, ty, source) for name, (ty, source) in captures.items()]

    def _closure(self, c) -> str:
        """Emit a closure as a top-level static function with simple captures."""
        uid     = id(c) & 0xFFFFFF
        name    = f"_mesa_closure_{uid}"
        ret_str = c_typeexpr(c.ret)
        params  = ", ".join(f"{c_typeexpr(p.type_)} {p.name}" for p in c.params) or "void"
        captures = self._collect_closure_captures(c)
        capture_names = {
            capture_name: f"{name}__capture__{capture_name}"
            for capture_name, _, _ in captures
        }

        w_saved   = self.w
        fn_writer = Writer()
        self.w    = fn_writer

        for capture_name, capture_ty, _ in captures:
            self.w.line(f"static {c_type(capture_ty)} {capture_names[capture_name]};")
        if captures:
            self.w.line()

        self.w.line(f"static {ret_str} {name}({params}) {{")
        self.w.indent()
        saved_capture_names = getattr(self, "_closure_capture_names", None)
        self._closure_capture_names = capture_names
        self._emit_block_stmts(c.body)
        self._closure_capture_names = saved_capture_names
        self.w.dedent()
        self.w.line("}")

        self._closures.append(fn_writer.output())
        self.w = w_saved
        if captures:
            assigns = [f"{capture_names[capture_name]} = {source_name}" for capture_name, _, source_name in captures]
            return "({ " + "; ".join(assigns + [name]) + "; })"
        return name

    # ── LValue ────────────────────────────────────────────────

    def _lvalue(self, expr: Expr) -> str:
        if isinstance(expr, Ident):
            capture_names = getattr(self, "_closure_capture_names", None)
            if capture_names and expr.name in capture_names:
                return capture_names[expr.name]
            return expr.name
        if isinstance(expr, SelfExpr):
            return "self"
        if isinstance(expr, FieldExpr):
            qname = self._qualified_name(expr)
            # .value and .units on unitful/uncertain types — intercept before normal field access
            obj_ty = self._expr_type(expr.obj)
            from src.types import TUnitful, TUncertain, TNamespace
            if isinstance(obj_ty, TNamespace):
                return qname or "0"
            if isinstance(obj_ty, TUnitful):
                if expr.field == "value":
                    return self._expr(expr.obj)   # IS the value — unit is type-only
                if expr.field == "units":
                    if obj_ty.dims is not None:
                        return f'(mesa_str){{"{obj_ty.name}", {len(obj_ty.name)}}}'
                    else:
                        return f"({self._expr(expr.obj)}).name"
            if isinstance(obj_ty, TUncertain):
                if expr.field == "value":
                    return self._expr(expr.obj)
                if expr.field == "uncertainty":
                    return "0.0"   # placeholder
            return self._field_access(expr)
        if isinstance(expr, UnaryExpr) and expr.op == "*":
            return f"(*{self._expr(expr.operand)})"
        if isinstance(expr, IndexExpr):
            return self._index_expr(expr)
        return self._expr(expr)

    # ── Type inference helpers ────────────────────────────────

    def _infer_c_type(self, expr: Optional[Expr]) -> str:
        if expr is None: return "void"
        if isinstance(expr, IntLit):
            ty = getattr(expr, '_resolved_type', None)
            if isinstance(ty, TFloat): return "double"
            return "int64_t"
        if isinstance(expr, FloatLit):
            ty = getattr(expr, '_resolved_type', None)
            if isinstance(ty, TFloat) and ty.bits == 32: return "float"
            return "double"
        if isinstance(expr, BoolLit):   return "int"
        if isinstance(expr, StringLit): return "mesa_str"
        if isinstance(expr, Ident):
            sym = self.env.lookup(expr.name)
            if sym:
                from src.types import TVar
                t = sym.type_
                if isinstance(t, TVar) and t.name and t.name in self._mono_bindings:
                    t = self._mono_bindings[t.name]
                return c_type(t)
            return "int64_t"
        if isinstance(expr, BinExpr):
            lt = self._infer_c_type(expr.left)
            rt = self._infer_c_type(expr.right)
            if "double" in (lt, rt): return "double"
            if "float"  in (lt, rt): return "float"
            return lt
        if isinstance(expr, TupleLit):
            return "/* struct */"
        # For any other expression, use _expr_type which understands try/catch etc.
        ty = self._expr_type(expr)
        if ty is not None:
            ct = c_type(ty)
            if ct and ct != "int64_t":
                return ct
        return "int64_t"

    def _emit_interpolation(self, sl) -> str:
        """Emit string interpolation as snprintf into a stack buffer."""
        # Build a format string and argument list from segments
        fmt_parts = []
        args = []
        for seg in sl.segments:
            if isinstance(seg, str):
                # Escape for C string
                escaped = seg.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                fmt_parts.append(escaped)
            else:
                # Expression — use Mesa type system for format selection
                c_expr = self._expr(seg)
                mesa_ty = self._expr_type(seg)
                from src.types import TFloat, TBool, TString, TInt
                if isinstance(mesa_ty, TFloat):
                    fmt_parts.append("%.6g")
                    args.append(f"(double)({c_expr})")
                elif isinstance(mesa_ty, TBool):
                    fmt_parts.append("%s")
                    args.append(f"(({c_expr}) ? \"true\" : \"false\")")
                elif isinstance(mesa_ty, TString):
                    fmt_parts.append("%.*s")
                    args.append(f"(int)({c_expr}).len, ({c_expr}).data")
                elif isinstance(mesa_ty, TInt):
                    fmt_parts.append("%lld")
                    args.append(f"(long long)({c_expr})")
                else:
                    # Fallback: use C-level type inference
                    ty = self._infer_c_type(seg)
                    if "double" in ty or ty == "float":
                        fmt_parts.append("%.6g")
                        args.append(f"(double)({c_expr})")
                    elif ty == "int":
                        fmt_parts.append("%s")
                        args.append(f"(({c_expr}) ? \"true\" : \"false\")")
                    elif "mesa_str" in ty:
                        fmt_parts.append("%.*s")
                        args.append(f"(int)({c_expr}).len, ({c_expr}).data")
                    else:
                        fmt_parts.append("%lld")
                        args.append(f"(long long)({c_expr})")
        fmt = "".join(fmt_parts)
        # Use a static buffer — not ideal but works for demos
        buf_var = self._fresh("ibuf")
        self.w.line(f"char {buf_var}[4096];")
        if args:
            args_str = ", ".join(args)
            self.w.line(f'snprintf({buf_var}, sizeof({buf_var}), "{fmt}", {args_str});')
        else:
            self.w.line(f'snprintf({buf_var}, sizeof({buf_var}), "{fmt}");')
        n = self._fresh("ilen")
        self.w.line(f"int64_t {n} = (int64_t)strlen({buf_var});")
        return f"(mesa_str){{{buf_var}, {n}}}"

    def _tail_type(self, expr) -> str:
        """Determine C return type of a tail expression without emitting it."""
        from src.ast import CallExpr, Ident
        from src.types import TVoid, TString, TFloat, TInt, TBool
        if isinstance(expr, CallExpr):
            # println / print always return void
            if isinstance(expr.callee, Ident) and expr.callee.name in ('println','print'):
                sym = getattr(expr.callee, "_bound_symbol", None) or self.env.lookup(expr.callee.name)
                if getattr(sym, "pkg_path", None) is None:
                    return "void"
            # Check resolved return type via checker annotation
            callee_ty = getattr(expr.callee, '_resolved_type', None)
            if callee_ty is not None:
                from src.types import TFun
                if isinstance(callee_ty, TFun):
                    return "void" if isinstance(callee_ty.ret, TVoid) else self._infer_c_type(expr)
        mesa_ty = self._expr_type(expr)
        if mesa_ty is not None:
            if isinstance(mesa_ty, TVoid):   return "void"
            if isinstance(mesa_ty, TFloat):  return "double"
            if isinstance(mesa_ty, TBool):   return "int"
            if isinstance(mesa_ty, TString): return "mesa_str"
            if isinstance(mesa_ty, TInt):    return "int64_t"
        return self._infer_c_type(expr)

    # ══════════════════════════════════════════════════════════
    # Output
    # ══════════════════════════════════════════════════════════

    def output(self) -> str:
        # Prepend any closures that were emitted during expression traversal
        closure_preamble = "#include <stdint.h>\n#include <math.h>\n" if self._closures else ""
        closure_text = "\n".join(self._closures)
        return closure_preamble + closure_text + ("\n" if self._closures else "") + self.w.output()
