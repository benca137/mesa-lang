"""
Mesa static analysis passes.

Four passes run after the type checker:

  1. ExhaustivenessChecker  — match arms cover all variants
  2. DefiniteAssignment     — variables used before initialisation
  3. ReturnPathChecker      — all code paths return a value
  4. LayoutPass             — compute byte size and field offsets for all types

All passes share the DiagnosticBag from the type checker environment.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from src.ast import *
from src.types import *
from src.env import Environment, DiagnosticBag


# ══════════════════════════════════════════════════════════════
# 1. Exhaustiveness checker
# ══════════════════════════════════════════════════════════════

class ExhaustivenessChecker:
    """
    Verify every match expression covers all possible values.

    Rules:
      - A wildcard `_` or bare ident pattern covers everything
      - A union type must have all variants covered (or a wildcard)
      - Bool must cover true and false (or a wildcard)
      - Integer matches always need a wildcard (infinite domain)
    """
    def __init__(self, env: Environment):
        self.env   = env
        self.diags = env.diags

    def run(self, program: Program):
        for decl in program.decls:
            self._check_decl(decl)

    def _check_decl(self, decl: Decl):
        prev_pkg = getattr(self.env, "_current_pkg", None)
        self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
        if isinstance(decl, FunDecl):
            self._check_block(decl.body)
            if decl.handle_block:
                self._check_block(decl.handle_block.body)
        elif isinstance(decl, TestDecl):
            self._check_block(decl.body)
        elif isinstance(decl, StructDecl):
            for m in decl.methods:
                self._check_block(m.body)
                if m.handle_block:
                    self._check_block(m.handle_block.body)
        elif isinstance(decl, DefDecl):
            for m in decl.methods:
                self._check_block(m.body)
                if m.handle_block:
                    self._check_block(m.handle_block.body)
        self.env.set_current_pkg(prev_pkg)

    def _check_block(self, block: Optional[Block]):
        if block is None: return
        for stmt in block.stmts:
            self._check_stmt(stmt)
        if block.tail:
            self._check_expr(block.tail)

    def _check_stmt(self, stmt: Stmt):
        if isinstance(stmt, LetStmt):
            if stmt.init: self._check_expr(stmt.init)
        elif isinstance(stmt, ReturnStmt):
            if stmt.value: self._check_expr(stmt.value)
        elif isinstance(stmt, AssignStmt):
            self._check_expr(stmt.value)
        elif isinstance(stmt, ExprStmt):
            self._check_expr(stmt.expr)
        elif isinstance(stmt, ForRangeStmt):
            self._check_block(stmt.body)
        elif isinstance(stmt, ForIterStmt):
            self._check_block(stmt.body)
        elif isinstance(stmt, WhileStmt):
            self._check_block(stmt.body)

    def _check_expr(self, expr: Expr):
        if isinstance(expr, MatchExpr):
            self._check_match(expr)
        elif isinstance(expr, IfExpr):
            self._check_block(expr.then_block)
            if expr.else_block: self._check_block(expr.else_block)
        elif isinstance(expr, IfUnwrap):
            self._check_block(expr.then_block)
            if expr.else_block: self._check_block(expr.else_block)
        elif isinstance(expr, BlockExpr):
            self._check_block(expr.block)
        elif isinstance(expr, WithExpr):
            self._check_expr(expr.resource)
            self._check_block(expr.body)
            if expr.handle:
                self._check_block(expr.handle.body)
        elif isinstance(expr, CallExpr):
            for a in expr.args: self._check_expr(a.value)
        elif isinstance(expr, BinExpr):
            self._check_expr(expr.left)
            self._check_expr(expr.right)

    def _check_match(self, me: MatchExpr):
        # Recurse into arm bodies first
        for arm in me.arms:
            self._check_block(arm.body)

        # Check for wildcard — if present, match is trivially exhaustive
        has_wildcard = any(
            isinstance(arm.pattern, PatWildcard) or
            (isinstance(arm.pattern, PatIdent) and arm.pattern.name == "_")
            for arm in me.arms
        )
        if has_wildcard:
            return

        # Look up the type of the matched value
        val_ty = getattr(me, '_checked_type', None)
        if val_ty is None:
            # Try to get from environment — best effort
            if isinstance(me.value, Ident):
                sym = self.env.lookup(me.value.name)
                val_ty = sym.type_ if sym else None

        if val_ty is None:
            return  # can't determine — skip

        # Resolve type aliases
        if isinstance(val_ty, TVar) and val_ty.resolved:
            val_ty = val_ty.root()

        # Union-like tag types — check all variants covered
        if isinstance(val_ty, (TUnion, TErrorSet)):
            covered = set()
            for arm in me.arms:
                if isinstance(arm.pattern, PatVariant):
                    covered.add(arm.pattern.name)
                elif isinstance(arm.pattern, PatIdent) and arm.pattern.name != "_":
                    covered.add(arm.pattern.name)

            all_variants = set(val_ty.variants.keys())
            missing = all_variants - covered

            if missing:
                self.diags.error(
                    f"non-exhaustive match on '{val_ty.name}' — "
                    f"missing variants: {', '.join(sorted(missing))}",
                    hint="add the missing arms or a wildcard '_'"
                )

        # Bool — check true and false covered
        elif isinstance(val_ty, TBool):
            has_true  = any(isinstance(a.pattern, PatBool) and a.pattern.value
                           for a in me.arms)
            has_false = any(isinstance(a.pattern, PatBool) and not a.pattern.value
                           for a in me.arms)
            if not (has_true and has_false):
                missing = []
                if not has_true:  missing.append("true")
                if not has_false: missing.append("false")
                self.diags.error(
                    f"non-exhaustive match on bool — missing: {', '.join(missing)}",
                    hint="add the missing arms or a wildcard '_'"
                )

        # Integer — needs wildcard OR bare ident binding (which covers everything)
        elif isinstance(val_ty, TInt):
            has_binding = any(
                isinstance(arm.pattern, PatIdent) and arm.pattern.name != "_"
                for arm in me.arms
            )
            if not has_binding:
                self.diags.error(
                    "non-exhaustive match on integer type — add a wildcard '_' arm",
                    hint="integer matches have infinite possible values"
                )


# ══════════════════════════════════════════════════════════════
# 2. Definite assignment checker
# ══════════════════════════════════════════════════════════════

class DefiniteAssignment:
    """
    Verify every variable is definitely assigned before use.

    Tracks a set of "definitely assigned" names through the CFG.
    At joins (if/match branches) a name is definitely assigned only
    if it's assigned on ALL branches.
    """
    def __init__(self, env: Environment):
        self.env   = env
        self.diags = env.diags

    def run(self, program: Program):
        for decl in program.decls:
            self._check_decl(decl)

    def _check_decl(self, decl: Decl):
        prev_pkg = getattr(self.env, "_current_pkg", None)
        self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
        if isinstance(decl, FunDecl) and decl.body:
            # Params are definitely assigned
            assigned = {p.name for p in decl.params}
            self._check_block(decl.body, assigned)
            if decl.handle_block:
                handle_assigned = set(assigned)
                handle_assigned.add(decl.handle_block.binding)
                self._check_block(decl.handle_block.body, handle_assigned)
        elif isinstance(decl, TestDecl):
            self._check_block(decl.body, set())
        elif isinstance(decl, StructDecl):
            for m in decl.methods:
                if m.body:
                    assigned = {p.name for p in m.params}
                    self._check_block(m.body, assigned)
                    if m.handle_block:
                        handle_assigned = set(assigned)
                        handle_assigned.add(m.handle_block.binding)
                        self._check_block(m.handle_block.body, handle_assigned)
        elif isinstance(decl, DefDecl):
            for m in decl.methods:
                if m.body:
                    assigned = {p.name for p in m.params}
                    self._check_block(m.body, assigned)
                    if m.handle_block:
                        handle_assigned = set(assigned)
                        handle_assigned.add(m.handle_block.binding)
                        self._check_block(m.handle_block.body, handle_assigned)
        self.env.set_current_pkg(prev_pkg)

    def _check_block(self, block: Optional[Block],
                     assigned: Set[str]) -> Set[str]:
        """Returns the set of definitely assigned names after the block."""
        if block is None:
            return set(assigned)
        assigned = set(assigned)
        for stmt in block.stmts:
            assigned = self._check_stmt(stmt, assigned)
        if block.tail:
            self._check_expr(block.tail, assigned)
        return assigned

    def _check_stmt(self, stmt: Stmt,
                    assigned: Set[str]) -> Set[str]:
        assigned = set(assigned)

        if isinstance(stmt, LetStmt):
            if stmt.init:
                self._check_expr(stmt.init, assigned)
                assigned.add(stmt.name)
            elif stmt.type_ is not None:
                # declared but not initialised — not assigned yet
                pass
            else:
                pass  # inferred type needs init — parser should catch this

        elif isinstance(stmt, AssignStmt):
            self._check_expr(stmt.value, assigned)
            # Mark target as assigned
            if isinstance(stmt.target, Ident):
                assigned.add(stmt.target.name)

        elif isinstance(stmt, ReturnStmt):
            if stmt.value:
                self._check_expr(stmt.value, assigned)

        elif isinstance(stmt, ExprStmt):
            self._check_expr(stmt.expr, assigned)

        elif isinstance(stmt, ForRangeStmt):
            self._check_expr(stmt.start, assigned)
            self._check_expr(stmt.end, assigned)
            loop_assigned = set(assigned)
            loop_assigned.add(stmt.var)
            if stmt.filter:
                self._check_expr(stmt.filter, loop_assigned)
            self._check_block(stmt.body, loop_assigned)
            # After loop, loop var is out of scope — don't add to assigned

        elif isinstance(stmt, ForIterStmt):
            self._check_expr(stmt.iter, assigned)
            loop_assigned = set(assigned)
            self._bind_pattern(stmt.pattern, loop_assigned)
            if stmt.filter:
                self._check_expr(stmt.filter, loop_assigned)
            self._check_block(stmt.body, loop_assigned)

        elif isinstance(stmt, WhileStmt):
            self._check_expr(stmt.cond, assigned)
            self._check_block(stmt.body, assigned)

        elif isinstance(stmt, DeferStmt):
            self._check_block(stmt.body, assigned)

        elif isinstance(stmt, BreakStmt):
            if stmt.value:
                self._check_expr(stmt.value, assigned)

        return assigned

    def _check_expr(self, expr: Expr, assigned: Set[str]):
        if isinstance(expr, VariantLit):
            return   # .Variant — always valid, no assignment needed
        if isinstance(expr, WithExpr):
            self._check_expr(expr.resource, assigned)
            self._check_block(expr.body, assigned)
            if expr.handle:
                handle_assigned = set(assigned)
                handle_assigned.add(expr.handle.binding)
                self._check_block(expr.handle.body, handle_assigned)
            return
        # Note: CallExpr(VariantLit, args) is handled by the normal CallExpr path

        if isinstance(expr, Ident):
            # Skip synthetic compiler-internal names (__ prefix)
            if expr.name.startswith('__'):
                return
            if (expr.name not in assigned and
                    expr.name not in ('none', 'true', 'false', 'self') and
                    self.env.lookup(expr.name) is None):
                sym = self.env.lookup(expr.name)
                if sym is None and expr.name not in assigned:
                    if self.env.lookup_type(expr.name) is None:
                        self.diags.error(
                            f"variable '{expr.name}' used before assignment",
                            hint=f"initialise '{expr.name}' before use"
                        )

        elif isinstance(expr, BinExpr):
            self._check_expr(expr.left, assigned)
            self._check_expr(expr.right, assigned)

        elif isinstance(expr, UnaryExpr):
            self._check_expr(expr.operand, assigned)

        elif isinstance(expr, FieldExpr):
            self._check_expr(expr.obj, assigned)

        elif isinstance(expr, CallExpr):
            self._check_expr(expr.callee, assigned)
            for a in expr.args:
                self._check_expr(a.value, assigned)

        elif isinstance(expr, IfExpr):
            self._check_expr(expr.cond, assigned)
            then_out = self._check_block(expr.then_block, assigned)
            else_out = self._check_block(expr.else_block, assigned) \
                       if expr.else_block else set(assigned)
            # After if: only definitely assigned if assigned on BOTH branches
            assigned.intersection_update(then_out | else_out)
            assigned.update(then_out & else_out)

        elif isinstance(expr, IfUnwrap):
            self._check_expr(expr.expr, assigned)
            then_in = set(assigned)
            then_in.add(expr.binding)
            then_out = self._check_block(expr.then_block, then_in)
            else_out = self._check_block(expr.else_block, assigned) \
                       if expr.else_block else set(assigned)
            assigned.update(then_out & else_out)

        elif isinstance(expr, MatchExpr):
            self._check_expr(expr.value, assigned)
            arm_outs = []
            for arm in expr.arms:
                arm_in = set(assigned)
                self._bind_match_pattern(arm.pattern, arm_in)
                arm_out = self._check_block(arm.body, arm_in)
                arm_outs.append(arm_out)
            if arm_outs:
                # Definitely assigned after match = intersection of all arms
                intersection = arm_outs[0]
                for out in arm_outs[1:]:
                    intersection = intersection & out
                assigned.update(intersection)

        elif isinstance(expr, BlockExpr):
            self._check_block(expr.block, assigned)

        elif isinstance(expr, TupleLit):
            for _, e in expr.fields:
                self._check_expr(e, assigned)

        elif isinstance(expr, VecLit):
            for e in expr.elems:
                self._check_expr(e, assigned)

        elif isinstance(expr, VecComp):
            self._check_expr(expr.iter, assigned)
            inner = set(assigned)
            self._bind_pattern(expr.pattern, inner)
            if expr.filter: self._check_expr(expr.filter, inner)
            self._check_expr(expr.expr, inner)

        elif isinstance(expr, IndexExpr):
            self._check_expr(expr.obj, assigned)
            for i in expr.indices: self._check_expr(i, assigned)

        elif isinstance(expr, Closure):
            # Closures capture the outer scope — include currently assigned names
            closure_assigned = set(assigned) | {p.name for p in expr.params}
            self._check_block(expr.body, closure_assigned)

    def _bind_pattern(self, pat: ForPattern, assigned: Set[str]):
        if isinstance(pat, PatIdent):   assigned.add(pat.name)
        elif isinstance(pat, PatRef):   assigned.add(pat.name)
        elif isinstance(pat, PatTuple): assigned.update(pat.names)

    def _bind_match_pattern(self, pat: MatchPattern, assigned: Set[str]):
        if isinstance(pat, PatVariant):
            if pat.binding: assigned.add(pat.binding)
            assigned.update(pat.extra_bindings or [])
        elif isinstance(pat, PatIdent):
            assigned.add(pat.name)


# ══════════════════════════════════════════════════════════════
# 3. Return path checker
# ══════════════════════════════════════════════════════════════

class ReturnPathChecker:
    """
    Verify every code path in a non-void function returns a value.

    A block "definitely returns" if:
      - It contains a return statement, OR
      - Its tail expression is non-void, OR
      - It's an if/match where ALL branches definitely return

    A block "possibly returns" if any branch returns.
    """
    def __init__(self, env: Environment):
        self.env   = env
        self.diags = env.diags

    def run(self, program: Program):
        for decl in program.decls:
            self._check_decl(decl)

    def _check_decl(self, decl: Decl):
        prev_pkg = getattr(self.env, "_current_pkg", None)
        self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
        if isinstance(decl, FunDecl):
            self._check_fun(decl)
        elif isinstance(decl, TestDecl):
            return
        elif isinstance(decl, StructDecl):
            for m in decl.methods: self._check_fun(m)
        elif isinstance(decl, DefDecl):
            for m in decl.methods: self._check_fun(m)
        self.env.set_current_pkg(prev_pkg)

    def _check_fun(self, f: FunDecl):
        if f.body is None: return
        ret_ty = self.env.lookup_type(
            f.ret.name if isinstance(f.ret, TyNamed) else None or ""
        )
        # Check if return type is void
        if isinstance(f.ret, (TyVoid, type(TyVoid()))):
            return
        if isinstance(f.ret, TyPrimitive) and f.ret.name == "void":
            return
        if isinstance(f.ret, TyErrorUnion):
            payload = f.ret.payload
            if isinstance(payload, (TyVoid, type(TyVoid()))):
                return
            if isinstance(payload, TyPrimitive) and payload.name == "void":
                return

        if not self._block_returns(f.body):
            self.diags.error(
                f"function '{f.name}' does not return a value on all paths",
                hint="add a return statement or ensure all branches return"
            )

    def _block_returns(self, block: Optional[Block]) -> bool:
        """True if this block definitely returns on all paths."""
        if block is None:
            return False

        # Check statements for definite return
        for stmt in block.stmts:
            if self._stmt_returns(stmt):
                return True

        # Check tail expression
        if block.tail is not None:
            return self._expr_returns(block.tail)

        return False

    def _stmt_returns(self, stmt: Stmt) -> bool:
        if isinstance(stmt, ReturnStmt):
            return True
        if isinstance(stmt, ExprStmt):
            return self._expr_returns(stmt.expr)
        if isinstance(stmt, LetStmt):
            return False
        if isinstance(stmt, ForRangeStmt):
            return False  # loop might not execute
        if isinstance(stmt, ForIterStmt):
            return False
        if isinstance(stmt, WhileStmt):
            return False
        if isinstance(stmt, BreakStmt):
            return False
        if isinstance(stmt, ContinueStmt):
            return False
        return False

    def _expr_returns(self, expr: Expr) -> bool:
        """True if this expression definitely produces a return value."""
        if isinstance(expr, IfExpr):
            # Both branches must return
            if expr.else_block is None:
                return False
            return (self._block_returns(expr.then_block) and
                    self._block_returns(expr.else_block))

        if isinstance(expr, IfUnwrap):
            if expr.else_block is None:
                return False
            return (self._block_returns(expr.then_block) and
                    self._block_returns(expr.else_block))

        if isinstance(expr, MatchExpr):
            # All arms must return, and there must be a wildcard or full coverage
            if not expr.arms:
                return False
            return all(self._block_returns(arm.body) for arm in expr.arms)

        if isinstance(expr, BlockExpr):
            return self._block_returns(expr.block)

        # Any other expression — it's a value, counts as returning
        return True


# ══════════════════════════════════════════════════════════════
# 4. Layout pass — compute sizes and field offsets
# ══════════════════════════════════════════════════════════════

@dataclass
class FieldLayout:
    name:   str
    type_:  Type
    offset: int   # byte offset from start of struct
    size:   int   # size in bytes

@dataclass
class TypeLayout:
    size:      int               # total size in bytes
    align:     int               # alignment requirement
    fields:    List[FieldLayout] = field(default_factory=list)
    # For unions: size = max variant size + tag
    is_union:  bool = False
    tag_size:  int  = 0


class LayoutPass:
    """
    Compute the byte size and alignment of every type.
    Results stored in self.layouts — used by codegen for:
      - Stack frame allocation
      - Struct field offsets (GEP indices)
      - Array element stride
    """
    def __init__(self, env: Environment):
        self.env     = env
        self.diags   = env.diags
        self.layouts: Dict[str, TypeLayout] = {}
        self._computing: Set[str] = set()  # cycle detection

        # Pre-populate primitive sizes
        self._primitive_sizes: Dict[str, Tuple[int, int]] = {
            # name: (size, align)
            "i8":   (1, 1),  "i16":  (2, 2),
            "i32":  (4, 4),  "i64":  (8, 8),
            "u8":   (1, 1),  "u16":  (2, 2),
            "u32":  (4, 4),  "u64":  (8, 8),
            "f32":  (4, 4),  "f64":  (8, 8),
            "bool": (1, 1),
            "str":  (16, 8),  # fat pointer: {*u8, u64}
            "void": (0, 1),
        }

    def run(self, program: Program):
        # Layout all declared types
        for decl in program.decls:
            self.env.set_current_pkg(getattr(decl, "_pkg_path", None))
            if isinstance(decl, StructDecl):
                self.layout_struct(decl.name)
            elif isinstance(decl, UnionDecl):
                self.layout_union(decl.name)
        self.env.set_current_pkg(None)

    def layout_of(self, ty: Type) -> TypeLayout:
        """Get or compute the layout of a type."""
        if isinstance(ty, TInt):
            key = f"{'i' if ty.signed else 'u'}{ty.bits}"
            s, a = self._primitive_sizes[key]
            return TypeLayout(size=s, align=a)

        if isinstance(ty, TFloat):
            s, a = self._primitive_sizes[f"f{ty.bits}"]
            return TypeLayout(size=s, align=a)

        if isinstance(ty, TBool):
            return TypeLayout(size=1, align=1)

        if isinstance(ty, TString):
            return TypeLayout(size=16, align=8)  # fat pointer

        if isinstance(ty, TVoid):
            return TypeLayout(size=0, align=1)

        if isinstance(ty, TPointer):
            return TypeLayout(size=8, align=8)  # 64-bit pointer

        if isinstance(ty, TOptional):
            inner = self.layout_of(ty.inner)
            # { T, bool } — padded to alignment
            total = self._align_up(inner.size, inner.align) + 1
            total = self._align_up(total, inner.align)
            return TypeLayout(size=total, align=inner.align)

        if isinstance(ty, TSlice):
            return TypeLayout(size=16, align=8)  # {*T, u64}

        if isinstance(ty, TVec):
            if ty.size is not None:
                inner = self.layout_of(ty.inner)
                return TypeLayout(size=inner.size * ty.size, align=inner.align)
            return TypeLayout(size=24, align=8)  # {*T, u64, u64} dynamic

        if isinstance(ty, TArray):
            inner = self.layout_of(ty.inner)
            return TypeLayout(size=inner.size * ty.size, align=inner.align)

        if isinstance(ty, TTuple):
            return self._layout_fields(
                [(n or f"_{i}", t) for i, (n, t) in enumerate(ty.fields)]
            )

        if isinstance(ty, TStruct):
            if ty.name in self.layouts:
                return self.layouts[ty.name]
            return self.layout_struct(ty.name)

        if isinstance(ty, TUnion):
            if ty.name in self.layouts:
                return self.layouts[ty.name]
            return self.layout_union(ty.name)

        if isinstance(ty, TFun):
            return TypeLayout(size=8, align=8)  # function pointer


        if isinstance(ty, TVar):
            root = ty.root()
            if root is ty:
                return TypeLayout(size=8, align=8)  # unknown — assume pointer size
            return self.layout_of(root)

        if isinstance(ty, TError):
            return TypeLayout(size=0, align=1)

        # Default
        return TypeLayout(size=8, align=8)

    def layout_struct(self, name: str) -> TypeLayout:
        if name in self.layouts:
            return self.layouts[name]
        if name in self._computing:
            # Recursive type — use pointer-sized placeholder
            return TypeLayout(size=8, align=8)

        ty = self.env.lookup_type(name)
        if not isinstance(ty, TStruct):
            return TypeLayout(size=8, align=8)

        self._computing.add(name)
        layout = self._layout_fields(list(ty.fields.items()))
        self._computing.discard(name)

        self.layouts[name] = layout
        return layout

    def layout_union(self, name: str) -> TypeLayout:
        if name in self.layouts:
            return self.layouts[name]

        ty = self.env.lookup_type(name)
        if not isinstance(ty, TUnion):
            return TypeLayout(size=8, align=8)

        # Tag is u64 (8 bytes)
        tag_size   = 8
        max_size   = 0
        max_align  = 8

        for variant_ty in ty.variants.values():
            if variant_ty is not None:
                vl = self.layout_of(variant_ty)
                max_size  = max(max_size, vl.size)
                max_align = max(max_align, vl.align)

        # Total: tag + padding + max_payload
        payload_offset = self._align_up(tag_size, max_align)
        total          = self._align_up(payload_offset + max_size, max_align)

        layout = TypeLayout(
            size=total, align=max_align,
            is_union=True, tag_size=tag_size
        )
        self.layouts[name] = layout
        return layout

    def _layout_fields(self, fields: List[Tuple[str, Type]]) -> TypeLayout:
        """Compute struct layout with proper alignment padding."""
        offset    = 0
        max_align = 1
        laid_out  = []

        for name, ty in fields:
            fl = self.layout_of(ty)
            # Align this field
            offset = self._align_up(offset, fl.align)
            laid_out.append(FieldLayout(
                name=name, type_=ty, offset=offset, size=fl.size
            ))
            offset    += fl.size
            max_align  = max(max_align, fl.align)

        # Pad struct to its own alignment
        total = self._align_up(offset, max_align)
        return TypeLayout(size=total, align=max_align, fields=laid_out)

    def _align_up(self, offset: int, align: int) -> int:
        if align <= 1: return offset
        return (offset + align - 1) & ~(align - 1)

    def size_of(self, ty: Type) -> int:
        return self.layout_of(ty).size

    def align_of(self, ty: Type) -> int:
        return self.layout_of(ty).align

    def field_offset(self, struct_name: str, field_name: str) -> Optional[int]:
        layout = self.layouts.get(struct_name)
        if layout is None: return None
        for f in layout.fields:
            if f.name == field_name: return f.offset
        return None


# ══════════════════════════════════════════════════════════════
# Combined analysis pass
# ══════════════════════════════════════════════════════════════

def analyse(program: Program, env: Environment) -> LayoutPass:
    """
    Run all four static analysis passes after type checking.
    Returns the LayoutPass (needed by codegen).
    Errors added to env.diags.
    """
    ExhaustivenessChecker(env).run(program)
    DefiniteAssignment(env).run(program)
    ReturnPathChecker(env).run(program)
    layout = LayoutPass(env)
    layout.run(program)
    return layout
