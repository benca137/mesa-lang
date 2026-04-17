"""
Standalone Mesa MIR generator.

This deliberately does not replace the existing C backend or CLI. It is an
isolated lowering path from the checked AST into the new human-readable MIR so
we can iterate on syntax and pass design safely.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Dict, List, Optional, Tuple

from src.ast import (
    AssignStmt,
    BinExpr,
    Block,
    BlockExpr,
    BoolLit,
    BreakStmt,
    CallExpr,
    ContinueStmt,
    Decl,
    DeferStmt,
    ErrorDecl,
    Expr,
    ExprStmt,
    FieldExpr,
    FloatLit,
    ForIterStmt,
    ForRangeStmt,
    FunDecl,
    Ident,
    IfExpr,
    IfUnwrap,
    ImportDecl,
    IntLit,
    LetStmt,
    MatchExpr,
    MatchPattern,
    NoneLit,
    Param,
    PatBool,
    PatFloat,
    PatIdent,
    PatInt,
    PatNone,
    PatRef,
    PatTuple,
    PatVariant,
    PatWildcard,
    Program,
    ReturnStmt,
    StringLit,
    StructDecl,
    TestDecl,
    TypeAlias,
    UnaryExpr,
    UnionDecl,
    VariantLit,
    WhileStmt,
    WhileUnwrap,
    EscExpr,
    WithAllocExpr,
    WithExpr,
)
from src.checker import effective_return_type, lower_type
from src.frontend import FrontendState, build_frontend_state_for_path
from src.mir import (
    MIRAssign,
    MIRBlock,
    MIRCmpGoto,
    MIREval,
    MIRFunction,
    MIRGoto,
    MIRModule,
    MIRParam,
    MIRReturn,
    MIRSwitchResult,
    MIRSwitchCase,
    MIRSwitchTag,
    MIRTypeDecl,
)
from src.types import TArray, TBool, TErrorSet, TErrorSetUnion, TErrorUnion, TPointer, TSlice, TTuple, TUnion, TVec, TVoid, Type, error_set_variants, format_type_for_user


_COMPARE_OPS = {"==", "!=", "<", ">", "<=", ">="}
_COMPOUND_BINOPS = {
    "+=": "+",
    "-=": "-",
    "*=": "*",
    "/=": "/",
    "%=": "%",
    "^=": "^",
}


class MIRGenError(Exception):
    pass


@dataclass
class Binding:
    ssa_name: str
    type_: Optional[str]
    mutable: bool

    def clone(self) -> "Binding":
        return Binding(self.ssa_name, self.type_, self.mutable)


@dataclass
class BranchResult:
    block: MIRBlock
    snapshot: List[Dict[str, Binding]]
    value: Optional[str]
    terminated: bool


@dataclass
class AllocatorFrame:
    region_value: str
    cleanup: Optional[str]
    outer_target: str


@dataclass
class CleanupContext:
    region_value: str
    cleanup: str
    return_block: MIRBlock
    handle_block: Optional[MIRBlock]


@dataclass
class LoopContext:
    label: Optional[str]
    continue_target: str
    break_target: str
    continue_names: List[str]
    break_names: List[str]
    result_name: Optional[str] = None


class MIRGenerator:
    def __init__(self, env):
        self.env = env
        self.module = MIRModule()
        self._block_counts: Dict[str, int] = {}
        self._value_counts: Dict[str, int] = {}
        self._blocks: List[MIRBlock] = []
        self._current: Optional[MIRBlock] = None
        self._scopes: List[Dict[str, Binding]] = []
        self._current_fn_return_type: Optional[Type] = None
        self._current_fn_return_type_text: Optional[str] = None
        self._current_fn_handle_block: Optional[MIRBlock] = None
        self._current_fn_handle_binding_type: Optional[str] = None
        self._allocator_frames: List[AllocatorFrame] = []
        self._cleanup_stack: List[CleanupContext] = []
        self._local_handle_blocks: List[MIRBlock] = []
        self._loop_stack: List[LoopContext] = []
        self._defer_scopes: List[List[DeferStmt]] = []

    def emit_program(self, program: Program) -> MIRModule:
        self.module.type_decls = self._emit_type_decls(program)
        self.module.functions = self._emit_functions(program)
        return self.module

    def _emit_type_decls(self, program: Program) -> List[MIRTypeDecl]:
        decls: List[MIRTypeDecl] = []
        for decl in program.decls:
            if isinstance(decl, StructDecl):
                lines = ["struct {"]
                for field in decl.fields:
                    field_ty = format_type_for_user(lower_type(field.type_, self.env))
                    lines.append(f"    {field.name}: {field_ty},")
                lines.append("}")
                decls.append(MIRTypeDecl(decl.name, "\n".join(lines)))
            elif isinstance(decl, UnionDecl):
                lines = ["tagged {"]
                for variant in decl.variants:
                    if variant.payload is None:
                        lines.append(f"    {variant.name},")
                    else:
                        payload_ty = format_type_for_user(lower_type(variant.payload, self.env))
                        lines.append(f"    {variant.name}({payload_ty}),")
                lines.append("}")
                decls.append(MIRTypeDecl(decl.name, "\n".join(lines)))
            elif isinstance(decl, ErrorDecl):
                lines = ["error {"]
                for variant in decl.variants:
                    if variant.payload is None:
                        lines.append(f"    {variant.name},")
                    else:
                        payload_ty = format_type_for_user(lower_type(variant.payload, self.env))
                        lines.append(f"    {variant.name}({payload_ty}),")
                lines.append("}")
                decls.append(MIRTypeDecl(decl.name, "\n".join(lines)))
            elif isinstance(decl, TypeAlias):
                alias_ty = format_type_for_user(lower_type(decl.type_, self.env))
                decls.append(MIRTypeDecl(decl.name, alias_ty))
        return decls

    def _emit_functions(self, program: Program) -> List[MIRFunction]:
        functions: List[MIRFunction] = []
        for decl in program.decls:
            if isinstance(decl, FunDecl):
                functions.append(self._emit_function(decl))
        return functions

    def _emit_function(self, decl: FunDecl) -> MIRFunction:
        self._block_counts.clear()
        self._value_counts.clear()
        self._blocks = []
        self._current = None
        self._scopes = []
        self._current_fn_handle_block = None
        self._current_fn_handle_binding_type = None
        self._allocator_frames = []
        self._cleanup_stack = []
        self._local_handle_blocks = []
        self._loop_stack = []
        self._defer_scopes = []

        params = [self._emit_param(param) for param in decl.params]
        effective_ret = effective_return_type(decl, self.env)
        return_type = format_type_for_user(effective_ret)
        fn = MIRFunction(
            name=decl.name,
            params=params,
            return_type=return_type,
            is_extern=decl.is_extern or decl.body is None,
        )
        if fn.is_extern:
            return fn

        self.push_scope()
        self._current_fn_return_type = effective_ret
        self._current_fn_return_type_text = return_type
        for param in fn.params:
            self.define_binding(param.name[1:], Binding(param.name, param.type_, mutable=False))

        begin = self.new_block("begin")
        self.switch_to(begin)
        if decl.handle_block is not None:
            binding_ty = getattr(decl.handle_block, "_binding_type", None)
            binding_type_text = format_type_for_user(binding_ty) if isinstance(binding_ty, Type) else "int"
            handle_param = MIRParam(self._fresh_value_name(decl.handle_block.binding), binding_type_text)
            self._current_fn_handle_block = self.new_block("handle", [handle_param])
            self._current_fn_handle_binding_type = binding_type_text
        self._lower_block(decl.body, allow_value_fallthrough=True)
        if self.current.terminator is None:
            if return_type == "void":
                self.current.terminator = MIRReturn()
            else:
                raise MIRGenError(f"function '{decl.name}' falls through without producing {return_type}")

        if decl.handle_block is not None and self._current_fn_handle_block is not None:
            self.switch_to(self._current_fn_handle_block)
            self.push_scope()
            try:
                handle_param = self._current_fn_handle_block.params[0]
                self.define_binding(
                    decl.handle_block.binding,
                    Binding(handle_param.name, handle_param.type_, mutable=False),
                )
                self._lower_block(decl.handle_block.body, allow_value_fallthrough=True)
                if self.current.terminator is None:
                    if return_type == "void":
                        self.current.terminator = MIRReturn()
                    else:
                        raise MIRGenError(f"handle block in '{decl.name}' falls through without producing {return_type}")
            finally:
                self.pop_scope()

        self.pop_scope()
        self._current_fn_return_type = None
        self._current_fn_return_type_text = None
        fn.blocks = self._blocks
        return fn

    def _emit_param(self, param: Param) -> MIRParam:
        ty = format_type_for_user(lower_type(param.type_, self.env))
        return MIRParam(self._prefixed_value_name(param.name), ty)

    @property
    def current(self) -> MIRBlock:
        if self._current is None:
            raise MIRGenError("no active MIR block")
        return self._current

    def push_scope(self):
        self._scopes.append({})

    def pop_scope(self):
        self._scopes.pop()

    def visible_bindings(self) -> Dict[str, Binding]:
        result: Dict[str, Binding] = {}
        for scope in self._scopes:
            result.update(scope)
        return result

    def snapshot_scopes(self) -> List[Dict[str, Binding]]:
        return [{name: binding.clone() for name, binding in scope.items()} for scope in self._scopes]

    def restore_scopes(self, snapshot: List[Dict[str, Binding]]):
        self._scopes = [{name: binding.clone() for name, binding in scope.items()} for scope in snapshot]

    def define_binding(self, name: str, binding: Binding):
        self._scopes[-1][name] = binding

    def lookup_binding(self, name: str) -> Binding:
        for scope in reversed(self._scopes):
            binding = scope.get(name)
            if binding is not None:
                return binding
        raise MIRGenError(f"unknown binding '{name}' during MIR lowering")

    def update_binding(self, name: str, binding: Binding):
        for scope in reversed(self._scopes):
            if name in scope:
                scope[name] = binding
                return
        raise MIRGenError(f"cannot assign unknown binding '{name}'")

    def new_block(self, seed: str, params: Optional[List[MIRParam]] = None) -> MIRBlock:
        name = self._fresh_block_name(seed)
        block = MIRBlock(name=name, params=list(params or []))
        self._blocks.append(block)
        return block

    def switch_to(self, block: MIRBlock):
        self._current = block

    def emit_assign(
        self,
        target: str,
        value: str,
        result_type: Optional[str] = None,
        *,
        annotate_type: bool = False,
    ) -> str:
        self.current.instructions.append(
            MIRAssign(
                target=target,
                value=value,
                result_type=result_type,
                annotate_type=annotate_type,
            )
        )
        return target

    def emit_eval(self, value: str):
        self.current.instructions.append(MIREval(value=value))

    def _lower_block(self, block: Block, *, allow_value_fallthrough: bool) -> Optional[str]:
        self.push_scope()
        defer_scope: List[DeferStmt] = []
        self._defer_scopes.append(defer_scope)
        try:
            for stmt in block.stmts:
                if self.current.terminator is not None:
                    break
                self._lower_stmt(stmt)
            if self.current.terminator is not None:
                return None
            if block.tail is not None:
                value = self._lower_expr(block.tail)
                if self.current.terminator is not None:
                    return None
                self._run_defer_scope(defer_scope)
                if self.current.terminator is not None:
                    return None
                if allow_value_fallthrough:
                    self._emit_return(block.tail, value)
                    return None
                return value
            self._run_defer_scope(defer_scope)
            return None
        finally:
            self._defer_scopes.pop()
            self.pop_scope()

    def _lower_stmt(self, stmt):
        if isinstance(stmt, LetStmt):
            if stmt.init is None:
                raise MIRGenError(f"let without initializer is not lowered yet: {stmt.name}")
            value = self._materialize_binding(stmt.name, stmt.init)
            if self.current.terminator is not None:
                return
            self.define_binding(
                stmt.name,
                Binding(value, self._expr_type_text(stmt.init), stmt.mutable),
            )
            return

        if isinstance(stmt, AssignStmt):
            if isinstance(stmt.target, UnaryExpr) and stmt.target.op == "*":
                ptr_value = self._lower_expr(stmt.target.operand)
                if self.current.terminator is not None:
                    return
                pointee_type = self._expr_type_text(stmt.target) or "int"
                value_text = self._lower_pointer_assignment_value(stmt, ptr_value, pointee_type)
                if self.current.terminator is not None:
                    return
                self.emit_eval(f"store {ptr_value}, {value_text}")
                return
            if not isinstance(stmt.target, Ident):
                raise MIRGenError("only identifier and pointer assignment targets are lowered in MIR v1")
            binding = self.lookup_binding(stmt.target.name)
            if not binding.mutable:
                raise MIRGenError(f"cannot lower assignment to immutable binding '{stmt.target.name}'")
            expr = stmt.value
            if stmt.op != "=":
                if stmt.op not in _COMPOUND_BINOPS:
                    raise MIRGenError(f"compound assignment '{stmt.op}' is not lowered yet")
                expr = BinExpr(_COMPOUND_BINOPS[stmt.op], stmt.target, stmt.value)
            value = self._materialize_binding(stmt.target.name, expr)
            if self.current.terminator is not None:
                return
            self.update_binding(
                stmt.target.name,
                Binding(value, self._expr_type_text(expr) or binding.type_, True),
            )
            return

        if isinstance(stmt, ReturnStmt):
            value = self._lower_expr(stmt.value) if stmt.value is not None else None
            if self.current.terminator is not None:
                return
            self._emit_return(stmt.value, value)
            return

        if isinstance(stmt, ExprStmt):
            self._lower_expr_stmt(stmt.expr)
            return

        if isinstance(stmt, DeferStmt):
            self._lower_defer(stmt)
            return

        if isinstance(stmt, WhileStmt):
            self._lower_while(stmt)
            return

        if isinstance(stmt, ForRangeStmt):
            self._lower_for_range(stmt)
            return

        if isinstance(stmt, ForIterStmt):
            self._lower_for_iter(stmt)
            return

        if isinstance(stmt, BreakStmt):
            self._lower_break(stmt)
            return

        if isinstance(stmt, ContinueStmt):
            self._lower_continue(stmt)
            return

        raise MIRGenError(f"unsupported statement for MIR lowering: {type(stmt).__name__}")

    def _lower_expr_stmt(self, expr: Expr):
        if isinstance(expr, IfExpr):
            self._lower_if(expr, value_required=False, preferred_name=None)
            return
        if isinstance(expr, MatchExpr):
            self._lower_match(expr, value_required=False, preferred_name=None)
            return
        if isinstance(expr, BlockExpr):
            self._lower_nested_block(expr.block)
            return
        if isinstance(expr, CallExpr):
            if isinstance(expr.callee, Ident) and expr.callee.name == "__try":
                self._lower_try_expr(expr, preferred_name=None)
                return
            rendered = self._render_call(expr)
            rendered_type = self._expr_type_text(expr)
            if rendered_type == "void":
                self.emit_eval(rendered)
            else:
                temp = self._fresh_value_name(self._call_hint(expr))
                self.emit_assign(temp, rendered, rendered_type)
            return
        if isinstance(expr, WithExpr):
            self._lower_with(expr, value_required=False, preferred_name=None)
            return
        self._lower_expr(expr)

    def _lower_nested_block(self, block: Block) -> Optional[str]:
        self.push_scope()
        defer_scope: List[DeferStmt] = []
        self._defer_scopes.append(defer_scope)
        try:
            for stmt in block.stmts:
                if self.current.terminator is not None:
                    break
                self._lower_stmt(stmt)
            if self.current.terminator is not None:
                return None
            if block.tail is not None:
                value = self._lower_expr(block.tail)
                if self.current.terminator is not None:
                    return None
                self._run_defer_scope(defer_scope)
                if self.current.terminator is not None:
                    return None
                return value
            self._run_defer_scope(defer_scope)
            return None
        finally:
            self._defer_scopes.pop()
            self.pop_scope()

    def _lower_expr(self, expr: Optional[Expr], preferred_name: Optional[str] = None) -> str:
        if expr is None:
            raise MIRGenError("cannot lower missing expression")

        if isinstance(expr, IntLit):
            return self._materialize_simple(str(expr.value), self._expr_type_text(expr), preferred_name)

        if isinstance(expr, FloatLit):
            text = repr(expr.value)
            return self._materialize_simple(text, self._expr_type_text(expr), preferred_name)

        if isinstance(expr, BoolLit):
            text = "true" if expr.value else "false"
            return self._materialize_simple(text, self._expr_type_text(expr), preferred_name)

        if isinstance(expr, StringLit):
            text = self._quote_string(expr.raw)
            return self._materialize_simple(text, self._expr_type_text(expr), preferred_name)

        if isinstance(expr, NoneLit):
            return self._materialize_simple("none", self._expr_type_text(expr), preferred_name)

        if isinstance(expr, Ident):
            binding = self.lookup_binding(expr.name)
            if preferred_name is None:
                return binding.ssa_name
            return self.emit_assign(
                self._fresh_value_name(preferred_name),
                binding.ssa_name,
                binding.type_,
            )

        if isinstance(expr, VariantLit):
            tagged = self._render_unit_variant(expr)
            if tagged is not None:
                return self._materialize_value(
                    tagged,
                    self._expr_type_text(expr),
                    preferred_name or expr.name,
                )
            ty = self._expr_type_text(expr)
            return self._materialize_simple(expr.name, ty, preferred_name)

        if isinstance(expr, UnaryExpr):
            operand = self._lower_expr(expr.operand)
            text = f"{expr.op}{operand}"
            return self._materialize_value(text, self._expr_type_text(expr), preferred_name or self._unary_hint(expr.op))

        if isinstance(expr, FieldExpr):
            tagged = self._render_qualified_unit_variant(expr)
            if tagged is not None:
                return self._materialize_value(
                    tagged,
                    self._expr_type_text(expr),
                    preferred_name or expr.field,
                )
            base = self._lower_expr(expr.obj)
            text = f"{base}.{expr.field}"
            return self._materialize_value(text, self._expr_type_text(expr), preferred_name or expr.field)

        if isinstance(expr, BinExpr):
            left = self._lower_expr(expr.left)
            right = self._lower_expr(expr.right)
            text = f"{left} {expr.op} {right}"
            return self._materialize_value(text, self._expr_type_text(expr), preferred_name or self._bin_hint(expr.op))

        if isinstance(expr, CallExpr):
            if isinstance(expr.callee, Ident) and expr.callee.name == "__try":
                return self._lower_try_expr(expr, preferred_name)
            rendered = self._render_call(expr)
            return self._materialize_value(
                rendered,
                self._expr_type_text(expr),
                preferred_name or self._call_hint(expr),
                annotate_type=True,
            )

        if isinstance(expr, IfExpr):
            return self._lower_if(expr, value_required=True, preferred_name=preferred_name)

        if isinstance(expr, MatchExpr):
            return self._lower_match(expr, value_required=True, preferred_name=preferred_name)

        if isinstance(expr, WithExpr):
            return self._lower_with(expr, value_required=True, preferred_name=preferred_name)

        if isinstance(expr, WithAllocExpr):
            return self._lower_with_alloc(expr, preferred_name=preferred_name)

        if isinstance(expr, EscExpr):
            return self._lower_esc(expr, preferred_name=preferred_name)

        if isinstance(expr, BlockExpr):
            value = self._lower_nested_block(expr.block)
            if self.current.terminator is not None:
                return ""
            if value is None:
                raise MIRGenError("block expression did not produce a value")
            if preferred_name is None:
                return value
            return self.emit_assign(
                self._fresh_value_name(preferred_name),
                value,
                self._expr_type_text(expr),
            )

        if isinstance(expr, (IfUnwrap, WhileUnwrap)):
            raise MIRGenError(f"{type(expr).__name__} is not lowered yet")

        raise MIRGenError(f"unsupported expression for MIR lowering: {type(expr).__name__}")

    def _lower_if(self, expr: IfExpr, *, value_required: bool, preferred_name: Optional[str]) -> str:
        if value_required and expr.else_block is None:
            raise MIRGenError("if-expression without else cannot produce a MIR value")

        before = self.snapshot_scopes()
        cond_text = self._lower_condition(expr.cond)

        then_block = self.new_block("if_true")
        else_block = self.new_block("if_false" if expr.else_block is not None else "if_skip")
        self.current.terminator = MIRCmpGoto(cond_text, then_block.name, else_block.name)

        branches = [
            self._lower_branch_into(then_block, before, expr.then_block),
            self._lower_branch_into(else_block, before, expr.else_block if expr.else_block is not None else Block(stmts=[], tail=None)),
        ]
        return self._finish_branch_merge(
            before,
            branches,
            merge_seed="if_done",
            value_required=value_required,
            result_type=self._expr_type_text(expr),
            result_name_seed=preferred_name or "if_value",
        )

    def _lower_try_expr(self, expr: CallExpr, preferred_name: Optional[str]) -> str:
        if not expr.args:
            raise MIRGenError("try expression is missing its operand")
        inner_expr = expr.args[0].value
        inner_ty = getattr(inner_expr, "_resolved_type", None) or getattr(inner_expr, "_checked_type", None)
        if not isinstance(inner_ty, TErrorUnion):
            return self._lower_expr(inner_expr, preferred_name=preferred_name)

        source_block = self.current
        result_value = self._lower_expr(inner_expr, preferred_name=preferred_name or "try_result")
        payload_ty = inner_ty.payload
        payload_type_text = format_type_for_user(payload_ty)
        ok_name = ""
        ok_params: List[MIRParam] = []
        ok_target_args = f"{result_value}.ok"
        if not isinstance(payload_ty, TVoid):
            ok_name = self._fresh_value_name(preferred_name or "ok")
            ok_params = [MIRParam(ok_name, payload_type_text)]
        else:
            ok_target_args = ""
        ok_block = self.new_block("try_ok", ok_params)

        if self._local_handle_blocks:
            err_target = self._route_handle_target(f"{result_value}.err")
        elif self._current_fn_handle_block is not None:
            err_target = self._route_handle_target(f"{result_value}.err")
        elif isinstance(self._current_fn_return_type, TErrorUnion):
            err_name = self._fresh_value_name("err")
            err_block = self.new_block("try_err", [MIRParam(err_name, format_type_for_user(inner_ty.error_set) if inner_ty.error_set is not None else "error")])
            self.switch_to(err_block)
            self.current.terminator = self._route_return_terminator(
                MIRReturn(f"result.err({err_name})"),
                error_exit=True,
            )
            err_target = f"{err_block.name}({result_value}.err)"
        else:
            raise MIRGenError("try is only lowered inside error-returning functions or functions with handle blocks")

        source_block.terminator = MIRSwitchResult(
            result_value,
            ok_target=f"{ok_block.name}({ok_target_args})" if ok_target_args else ok_block.name,
            err_target=err_target,
        )
        self.switch_to(ok_block)
        return ok_name

    def _lower_with(self, expr: WithExpr, *, value_required: bool, preferred_name: Optional[str]) -> str:
        region_value = self._lower_expr(expr.resource)
        frame = AllocatorFrame(
            region_value=region_value,
            cleanup=expr.cleanup,
            outer_target=self._outer_allocator_target(),
        )

        handle_block: Optional[MIRBlock] = None
        handle_binding_type = self._with_handle_binding_type(expr)
        if expr.handle is not None:
            handle_param = MIRParam(self._fresh_value_name(expr.handle.binding), handle_binding_type)
            handle_block = self.new_block("with_handle", [handle_param])

        before = self.snapshot_scopes()

        if expr.cleanup is None:
            body_block = self.new_block("with_body") if expr.handle is not None else self.current
            if expr.handle is not None:
                self.current.terminator = MIRGoto(body_block.name)
            body_branch = self._lower_with_branch(
                body_block,
                before,
                expr.body,
                frame=frame,
                cleanup_context=None,
                active_handle_block=handle_block,
            )

            branches = [body_branch]
            if expr.handle is not None and handle_block is not None:
                branches.append(
                    self._lower_with_handle_branch(
                        handle_block,
                        before,
                        expr,
                        frame=frame,
                        cleanup_context=None,
                    )
                )

            result_name = self._finish_branch_merge(
                before,
                branches,
                merge_seed="with_done",
                value_required=value_required,
                result_type=self._expr_type_text(expr),
                result_name_seed=preferred_name or "with_value",
            )
            if not value_required:
                return ""
            return result_name

        source_block = self.current
        body_block = self.new_block("with_body")
        source_block.terminator = MIRGoto(body_block.name)

        cleanup_context = self._push_cleanup_context(frame)

        body_branch = self._lower_with_branch(
            body_block,
            before,
            expr.body,
            frame=frame,
            cleanup_context=cleanup_context,
            active_handle_block=handle_block,
        )

        branches = [body_branch]
        if expr.handle is not None and handle_block is not None:
            branches.append(
                self._lower_with_handle_branch(
                    handle_block,
                    before,
                    expr,
                    frame=frame,
                    cleanup_context=cleanup_context,
                )
            )

        body_value = self._finish_branch_merge(
            before,
            branches,
            merge_seed="with_merge",
            value_required=value_required,
            result_type=self._expr_type_text(expr),
            result_name_seed=preferred_name or "with_value",
        )

        if all(branch.terminated for branch in branches):
            self._finalize_cleanup_context(cleanup_context)
            return ""

        cleanup_param_type = self._with_body_output_type(expr, value_required=value_required)
        cleanup_params = [MIRParam(self._fresh_value_name("with_value"), cleanup_param_type)] if cleanup_param_type else []
        cleanup_block = self.new_block("with_cleanup", cleanup_params)
        done_params = [MIRParam(self._fresh_value_name(preferred_name or "with_value"), self._expr_type_text(expr))] if value_required else []
        done_block = self.new_block("with_done", done_params)
        if cleanup_param_type and body_value is None:
            raise MIRGenError("cleanup-bearing with body did not produce a value")

        cleanup_args = [body_value] if body_value is not None else []
        self.current.terminator = MIRGoto(cleanup_block.name, cleanup_args)

        self.switch_to(cleanup_block)
        self.emit_eval(f"region.cleanup {frame.region_value} {expr.cleanup}")
        if value_required:
            final_value = cleanup_block.params[0].name if cleanup_block.params else None
            final_value = self._wrap_with_result(expr, final_value)
            self.current.terminator = MIRGoto(done_block.name, [final_value] if final_value is not None else [])
        else:
            self.current.terminator = MIRGoto(done_block.name)

        self._finalize_cleanup_context(cleanup_context)
        self.switch_to(done_block)
        if not value_required:
            return ""
        return done_block.params[0].name

    def _lower_with_branch(
        self,
        block: MIRBlock,
        before: List[Dict[str, Binding]],
        body: Block,
        *,
        frame: AllocatorFrame,
        cleanup_context: Optional[CleanupContext],
        active_handle_block: Optional[MIRBlock],
    ) -> BranchResult:
        self.switch_to(block)
        self.restore_scopes(before)
        self.push_scope()
        defer_scope: List[DeferStmt] = []
        self._defer_scopes.append(defer_scope)
        self._allocator_frames.append(frame)
        if cleanup_context is not None:
            self._cleanup_stack.append(cleanup_context)
        if active_handle_block is not None:
            self._local_handle_blocks.append(active_handle_block)
        try:
            for stmt in body.stmts:
                if self.current.terminator is not None:
                    break
                self._lower_stmt(stmt)
            value = None
            if self.current.terminator is None and body.tail is not None:
                value = self._lower_expr(body.tail)
            if self.current.terminator is None:
                self._run_defer_scope(defer_scope)
            if self.current.terminator is not None:
                value = None
            return BranchResult(
                block=self.current,
                snapshot=self.snapshot_scopes(),
                value=value,
                terminated=self.current.terminator is not None,
            )
        finally:
            if active_handle_block is not None:
                self._local_handle_blocks.pop()
            if cleanup_context is not None:
                self._cleanup_stack.pop()
            self._allocator_frames.pop()
            self._defer_scopes.pop()
            self.pop_scope()

    def _lower_with_handle_branch(
        self,
        block: MIRBlock,
        before: List[Dict[str, Binding]],
        expr: WithExpr,
        *,
        frame: AllocatorFrame,
        cleanup_context: Optional[CleanupContext],
    ) -> BranchResult:
        if expr.handle is None:
            raise MIRGenError("missing local with handle body")
        self.switch_to(block)
        self.restore_scopes(before)
        self.push_scope()
        defer_scope: List[DeferStmt] = []
        self._defer_scopes.append(defer_scope)
        self._allocator_frames.append(frame)
        if cleanup_context is not None:
            self._cleanup_stack.append(cleanup_context)
        try:
            handle_param = block.params[0]
            self.define_binding(
                expr.handle.binding,
                Binding(handle_param.name, handle_param.type_, mutable=False),
            )
            for stmt in expr.handle.body.stmts:
                if self.current.terminator is not None:
                    break
                self._lower_stmt(stmt)
            value = None
            if self.current.terminator is None and expr.handle.body.tail is not None:
                value = self._lower_expr(expr.handle.body.tail)
            if self.current.terminator is None:
                self._run_defer_scope(defer_scope)
            if self.current.terminator is not None:
                value = None
            return BranchResult(
                block=self.current,
                snapshot=self.snapshot_scopes(),
                value=value,
                terminated=self.current.terminator is not None,
            )
        finally:
            if cleanup_context is not None:
                self._cleanup_stack.pop()
            self._allocator_frames.pop()
            self._defer_scopes.pop()
            self.pop_scope()

    def _lower_with_alloc(self, expr: WithAllocExpr, preferred_name: Optional[str]) -> str:
        value = self._lower_expr(expr.expr)
        target = self._lower_expr(expr.allocator)
        source = self._current_allocator_source()
        inner_ty = self._with_alloc_value_type(expr)
        return self._materialize_value(
            f"region.promote {value} from {source} to {target}",
            inner_ty,
            preferred_name or "promote",
            annotate_type=True,
        )

    def _lower_esc(self, expr: EscExpr, preferred_name: Optional[str]) -> str:
        frame = self._allocator_frames[-1] if self._allocator_frames else None
        value = self._lower_expr(expr.expr)
        promoted_ty = self._esc_value_type(expr)
        source = frame.region_value if frame is not None else "region.current"
        target = frame.outer_target if frame is not None else "region.parent"
        return self._materialize_value(
            f"region.promote {value} from {source} to {target}",
            promoted_ty,
            preferred_name or "esc",
            annotate_type=True,
        )

    def _lower_match(self, expr: MatchExpr, *, value_required: bool, preferred_name: Optional[str]) -> str:
        match_ty = getattr(expr, "_checked_type", None)
        if not isinstance(match_ty, (TUnion, TErrorSet, TErrorSetUnion)):
            raise MIRGenError(f"match lowering currently supports only tagged values, got {type(match_ty).__name__}")

        before = self.snapshot_scopes()
        source_block = self.current
        match_value = self._lower_expr(expr.value)

        arm_blocks: List[MIRBlock] = []
        arm_info: List[tuple[MatchPattern, BranchResult]] = []
        cases: List[MIRSwitchCase] = []
        default_target: Optional[str] = None

        for arm in expr.arms:
            label_seed = self._pattern_block_seed(arm.pattern)
            arm_block = self.new_block(label_seed)
            branch = self._lower_match_arm(arm_block, before, match_value, match_ty, arm.pattern, arm.body)
            arm_blocks.append(arm_block)
            arm_info.append((arm.pattern, branch))
            if isinstance(arm.pattern, PatVariant):
                cases.append(MIRSwitchCase(arm.pattern.name, arm_block.name))
            elif isinstance(arm.pattern, PatWildcard):
                default_target = arm_block.name
            elif isinstance(arm.pattern, PatIdent):
                default_target = arm_block.name
            else:
                raise MIRGenError(f"match pattern {type(arm.pattern).__name__} is not lowered yet")

        source_block.terminator = MIRSwitchTag(match_value, cases=cases, default_target=default_target)
        branches = [branch for _, branch in arm_info]
        return self._finish_branch_merge(
            before,
            branches,
            merge_seed="match_done",
            value_required=value_required,
            result_type=self._expr_type_text(expr),
            result_name_seed=preferred_name or "match_value",
        )

    def _lower_branch_block(self, block: Block) -> Optional[str]:
        self.push_scope()
        defer_scope: List[DeferStmt] = []
        self._defer_scopes.append(defer_scope)
        try:
            for stmt in block.stmts:
                if self.current.terminator is not None:
                    break
                self._lower_stmt(stmt)
            if self.current.terminator is not None:
                return None
            if block.tail is not None:
                value = self._lower_expr(block.tail)
                if self.current.terminator is not None:
                    return None
                self._run_defer_scope(defer_scope)
                if self.current.terminator is not None:
                    return None
                return value
            self._run_defer_scope(defer_scope)
            return None
        finally:
            self._defer_scopes.pop()
            self.pop_scope()

    def _lower_with_body(self, block: Block) -> tuple[Optional[str], bool]:
        self.push_scope()
        defer_scope: List[DeferStmt] = []
        self._defer_scopes.append(defer_scope)
        try:
            for stmt in block.stmts:
                if self.current.terminator is not None:
                    break
                self._lower_stmt(stmt)
            if self.current.terminator is not None:
                return None, True
            if block.tail is not None:
                value = self._lower_expr(block.tail)
                if self.current.terminator is not None:
                    return None, True
                self._run_defer_scope(defer_scope)
                if self.current.terminator is not None:
                    return None, True
                return value, False
            self._run_defer_scope(defer_scope)
            if self.current.terminator is not None:
                return None, True
            return None, False
        finally:
            self._defer_scopes.pop()
            self.pop_scope()

    def _lower_branch_into(
        self,
        block: MIRBlock,
        before: List[Dict[str, Binding]],
        body: Block,
    ) -> BranchResult:
        self.switch_to(block)
        self.restore_scopes(before)
        value = self._lower_branch_block(body)
        return BranchResult(
            block=self.current,
            snapshot=self.snapshot_scopes(),
            value=value,
            terminated=self.current.terminator is not None,
        )

    def _lower_match_arm(
        self,
        block: MIRBlock,
        before: List[Dict[str, Binding]],
        match_value: str,
        match_ty: Type,
        pattern: MatchPattern,
        body: Block,
    ) -> BranchResult:
        self.switch_to(block)
        self.restore_scopes(before)
        self.push_scope()
        try:
            self._bind_match_pattern_value(pattern, match_value, match_ty)
            for stmt in body.stmts:
                if self.current.terminator is not None:
                    break
                self._lower_stmt(stmt)
            value = None
            if self.current.terminator is None and body.tail is not None:
                value = self._lower_expr(body.tail)
            if self.current.terminator is not None:
                value = None
            return BranchResult(
                block=self.current,
                snapshot=self.snapshot_scopes(),
                value=value,
                terminated=self.current.terminator is not None,
            )
        finally:
            self.pop_scope()

    def _finish_branch_merge(
        self,
        before: List[Dict[str, Binding]],
        branches: List[BranchResult],
        *,
        merge_seed: str,
        value_required: bool,
        result_type: Optional[str],
        result_name_seed: str,
    ) -> str:
        if all(branch.terminated for branch in branches):
            self.restore_scopes(before)
            return ""

        merge_block = self.new_block(merge_seed)
        merge_params: List[MIRParam] = []
        branch_args: List[List[str]] = [[] for _ in branches]

        before_visible = self._visible_bindings_from_snapshot(before)
        branch_visible = [self._visible_bindings_from_snapshot(branch.snapshot) for branch in branches]
        merged_bindings: Dict[str, Binding] = dict(before_visible)

        for name, original in before_visible.items():
            branch_bindings = [visible.get(name, original) for visible in branch_visible]
            first_name = branch_bindings[0].ssa_name if branch_bindings else original.ssa_name
            if all(binding.ssa_name == first_name for binding in branch_bindings):
                merged_bindings[name] = branch_bindings[0] if branch_bindings else original
                continue
            param_name = self._fresh_value_name(name)
            param_type = original.type_ or next((binding.type_ for binding in branch_bindings if binding.type_), None)
            merge_params.append(MIRParam(param_name, param_type))
            for idx, binding in enumerate(branch_bindings):
                branch_args[idx].append(binding.ssa_name)
            merged_bindings[name] = Binding(param_name, param_type, original.mutable)

        result_name = ""
        if value_required:
            if any(branch.value is None and not branch.terminated for branch in branches):
                raise MIRGenError("branch did not produce a value for expression lowering")
            result_name = self._fresh_value_name(result_name_seed)
            merge_params.append(MIRParam(result_name, result_type))
            for idx, branch in enumerate(branches):
                if branch.value is None:
                    continue
                branch_args[idx].append(branch.value)

        merge_block.params = merge_params

        for idx, branch in enumerate(branches):
            if branch.terminated:
                continue
            self.switch_to(branch.block)
            self.current.terminator = MIRGoto(merge_block.name, branch_args[idx])

        self.switch_to(merge_block)
        self.restore_scopes(before)
        for name, binding in merged_bindings.items():
            self.update_binding(name, binding)
        return result_name

    def _outer_allocator_target(self) -> str:
        if self._allocator_frames:
            return self._allocator_frames[-1].region_value
        return "region.parent"

    def _current_allocator_source(self) -> str:
        if self._allocator_frames:
            return self._allocator_frames[-1].region_value
        return "region.current"

    def _with_resource_hint(self, expr: Expr) -> Optional[str]:
        if isinstance(expr, Ident):
            return expr.name
        return "region"

    def _push_cleanup_context(self, frame: AllocatorFrame) -> CleanupContext:
        return_params: List[MIRParam] = []
        if self._current_fn_return_type_text is not None and self._current_fn_return_type_text != "void":
            return_params.append(MIRParam(self._fresh_value_name("return_value"), self._current_fn_return_type_text))

        handle_block: Optional[MIRBlock] = None
        if self._current_fn_handle_block is not None and self._current_fn_handle_binding_type is not None:
            handle_block = self.new_block(
                "with_handle",
                [MIRParam(self._fresh_value_name("handle_error"), self._current_fn_handle_binding_type)],
            )

        return CleanupContext(
            region_value=frame.region_value,
            cleanup=frame.cleanup or "cleanup",
            return_block=self.new_block("with_return", return_params),
            handle_block=handle_block,
        )

    def _finalize_cleanup_context(self, context: CleanupContext):
        self.switch_to(context.return_block)
        self.emit_eval(f"region.cleanup {context.region_value} {context.cleanup}")
        if self._cleanup_stack:
            outer = self._cleanup_stack[-1].return_block
            args = [context.return_block.params[0].name] if context.return_block.params else []
            self.current.terminator = MIRGoto(outer.name, args)
        else:
            value = context.return_block.params[0].name if context.return_block.params else None
            self.current.terminator = MIRReturn(value)

        if context.handle_block is None:
            return

        self.switch_to(context.handle_block)
        self.emit_eval(f"region.cleanup {context.region_value} {context.cleanup}")
        err_value = context.handle_block.params[0].name
        if self._cleanup_stack:
            outer = self._cleanup_stack[-1].handle_block
            if outer is None:
                raise MIRGenError("cleanup handle edge is missing an outer target")
            self.current.terminator = MIRGoto(outer.name, [err_value])
            return
        if self._current_fn_handle_block is None:
            raise MIRGenError("cleanup handle edge is missing a function handle target")
        self.current.terminator = MIRGoto(self._current_fn_handle_block.name, [err_value])

    def _with_body_output_type(self, expr: WithExpr, *, value_required: bool) -> Optional[str]:
        if not value_required:
            return None
        result_type = self._expr_type_text(expr)
        if result_type is not None and not isinstance(expr.body.tail, EscExpr):
            return result_type
        if expr.body.tail is None:
            return None
        return self._with_inner_value_type(expr)

    def _with_handle_binding_type(self, expr: WithExpr) -> str:
        if expr.handle is None:
            return "int"
        binding_ty = getattr(expr.handle, "_binding_type", None)
        if isinstance(binding_ty, Type):
            return format_type_for_user(binding_ty)
        return "int"

    def _for_iter_element_type(self, stmt: ForIterStmt) -> str:
        binding_ty = getattr(stmt.pattern, "_binding_type", None)
        if isinstance(binding_ty, Type):
            return format_type_for_user(binding_ty)
        iter_ty = getattr(stmt.iter, "_resolved_type", None) or getattr(stmt.iter, "_checked_type", None)
        if hasattr(iter_ty, "inner") and isinstance(iter_ty, Type):
            inner = getattr(iter_ty, "inner", None)
            if isinstance(inner, Type):
                return format_type_for_user(inner)
        return "int"

    def _for_iter_len_expr(self, iter_value: str, iter_ty: Type) -> str:
        if isinstance(iter_ty, TVec):
            return f"vec.len {iter_value}"
        if isinstance(iter_ty, TSlice):
            return f"slice.len {iter_value}"
        if isinstance(iter_ty, TArray):
            return str(iter_ty.size)
        raise MIRGenError(f"unsupported iterable type for for-in length: {type(iter_ty).__name__}")

    def _for_iter_get_expr(self, iter_value: str, iter_ty: Type, index_value: str) -> str:
        if isinstance(iter_ty, TVec):
            return f"vec.get {iter_value}, {index_value}"
        if isinstance(iter_ty, TSlice):
            return f"slice.get {iter_value}, {index_value}"
        if isinstance(iter_ty, TArray):
            return f"array.get {iter_value}, {index_value}"
        raise MIRGenError(f"unsupported iterable type for for-in element access: {type(iter_ty).__name__}")

    def _for_iter_ref_expr(self, iter_value: str, iter_ty: Type, index_value: str) -> str:
        if isinstance(iter_ty, TVec):
            return f"vec.ref {iter_value}, {index_value}"
        if isinstance(iter_ty, TSlice):
            return f"slice.ref {iter_value}, {index_value}"
        if isinstance(iter_ty, TArray):
            return f"array.ref {iter_value}, {index_value}"
        raise MIRGenError(f"unsupported iterable type for for-in reference access: {type(iter_ty).__name__}")

    def _lower_for_iter_body(self, stmt: ForIterStmt, iter_ty: Type):
        self.push_scope()
        try:
            self._bind_for_pattern(stmt.pattern, iter_ty)
            self._lower_statement_block(stmt.body)
        finally:
            self.pop_scope()

    def _bind_for_pattern(self, pattern, iter_ty: Type):
        elem_ty = self._iter_elem_type(iter_ty)
        elem_type_text = format_type_for_user(elem_ty)
        iter_binding = self.lookup_binding("__iter_value")
        index_binding = self.lookup_binding("__iter_index")
        elem_value = self.emit_assign(
            self._fresh_value_name("elem"),
            self._for_iter_get_expr(iter_binding.ssa_name, iter_ty, index_binding.ssa_name),
            elem_type_text,
        )

        if isinstance(pattern, PatIdent):
            self.define_binding(pattern.name, Binding(elem_value, elem_type_text, mutable=False))
            return
        if isinstance(pattern, PatRef):
            ref_type = format_type_for_user(TPointer(elem_ty)) if isinstance(elem_ty, Type) else None
            ref_value = self.emit_assign(
                self._fresh_value_name(pattern.name),
                self._for_iter_ref_expr(iter_binding.ssa_name, iter_ty, index_binding.ssa_name),
                ref_type,
            )
            self.define_binding(pattern.name, Binding(ref_value, ref_type, mutable=True))
            return
        if isinstance(pattern, PatTuple):
            if not isinstance(elem_ty, TTuple) or len(pattern.names) != len(elem_ty.fields):
                raise MIRGenError("tuple for-pattern requires an iterable of matching tuple elements")
            for idx, (name, (_, field_ty)) in enumerate(zip(pattern.names, elem_ty.fields)):
                field_type_text = format_type_for_user(field_ty)
                field_value = self.emit_assign(
                    self._fresh_value_name(name),
                    f"{elem_value}._{idx}",
                    field_type_text,
                )
                self.define_binding(name, Binding(field_value, field_type_text, mutable=False))
            return
        raise MIRGenError(f"unsupported for-pattern in MIR lowering: {type(pattern).__name__}")

    def _iter_elem_type(self, iter_ty: Type) -> Type:
        if isinstance(iter_ty, (TVec, TSlice, TArray)):
            return iter_ty.inner
        raise MIRGenError(f"unsupported iterable type for for-in lowering: {type(iter_ty).__name__}")

    def _loop_carried_from_snapshot(
        self,
        snapshot: List[Dict[str, Binding]],
        *,
        include: Optional[List[str]] = None,
    ) -> List[tuple[str, Optional[str]]]:
        visible = self._visible_bindings_from_snapshot(snapshot)
        carried: List[tuple[str, Optional[str]]] = []
        include_set = set(include or [])
        for name, binding in visible.items():
            if binding.mutable or name in include_set:
                carried.append((name, binding.type_))
                include_set.discard(name)
        for name in include or []:
            if all(existing != name for existing, _ in carried):
                binding = visible.get(name)
                carried.append((name, binding.type_ if binding is not None else None))
        return carried

    def _loop_params(self, carried: List[tuple[str, Optional[str]]]) -> List[MIRParam]:
        return [MIRParam(self._prefixed_value_name(name), type_) for name, type_ in carried]

    def _loop_initial_args(
        self,
        snapshot: List[Dict[str, Binding]],
        carried: List[tuple[str, Optional[str]]],
    ) -> List[str]:
        visible = self._visible_bindings_from_snapshot(snapshot)
        return [visible[name].ssa_name for name, _ in carried]

    def _restore_loop_scope(
        self,
        snapshot: List[Dict[str, Binding]],
        carried: List[tuple[str, Optional[str]]],
        block: MIRBlock,
    ):
        self.restore_scopes(snapshot)
        for (name, type_), param in zip(carried, block.params):
            existing = self.lookup_binding(name)
            self.update_binding(name, Binding(param.name, type_ or existing.type_, existing.mutable))

    def _loop_current_args(self, carried: List[tuple[str, Optional[str]]]) -> List[str]:
        return [self.lookup_binding(name).ssa_name for name, _ in carried]

    def _loop_jump_args(self, names: List[str]) -> tuple[List[str], List[Optional[str]]]:
        args: List[str] = []
        arg_types: List[Optional[str]] = []
        for name in names:
            binding = self.lookup_binding(name)
            args.append(binding.ssa_name)
            arg_types.append(binding.type_)
        return args, arg_types

    def _loop_result_name(self) -> str:
        return "__loop_result"

    def _prepare_loop_result_binding(self, body: Block, label: Optional[str]) -> Optional[str]:
        result_type = self._infer_loop_result_type(body, label)
        if result_type is None:
            return None
        result_value = self.emit_assign(
            self._fresh_value_name("loop_value"),
            "loop.unset",
            result_type,
            annotate_type=True,
        )
        result_name = self._loop_result_name()
        self.define_binding(result_name, Binding(result_value, result_type, mutable=True))
        return result_name

    def _infer_loop_result_type(self, body: Block, label: Optional[str]) -> Optional[str]:
        result_types: List[str] = []

        def record(expr: Optional[Expr]):
            if expr is None:
                return
            type_text = self._infer_expr_type_text(expr)
            if type_text is not None:
                result_types.append(type_text)

        def visit_expr(expr: Optional[Expr], nested_loops: int):
            if expr is None:
                return
            if isinstance(expr, IfExpr):
                visit_block(expr.then_block, nested_loops)
                visit_block(expr.else_block, nested_loops)
                return
            if isinstance(expr, MatchExpr):
                for arm in expr.arms:
                    visit_block(arm.body, nested_loops)
                return
            if isinstance(expr, BlockExpr):
                visit_block(expr.block, nested_loops)
                return
            if isinstance(expr, WithExpr):
                visit_block(expr.body, nested_loops)
                if expr.handle is not None:
                    visit_block(expr.handle.body, nested_loops)
                return
            if isinstance(expr, WhileUnwrap):
                visit_block(expr.body, nested_loops + 1)
                return

        def visit_stmt(stmt, nested_loops: int):
            if isinstance(stmt, BreakStmt):
                if self._break_targets_loop(stmt.label, label, nested_loops):
                    record(stmt.value)
                return
            if isinstance(stmt, WhileStmt):
                visit_block(stmt.body, nested_loops + 1)
                return
            if isinstance(stmt, ForRangeStmt):
                visit_block(stmt.body, nested_loops + 1)
                return
            if isinstance(stmt, ForIterStmt):
                visit_block(stmt.body, nested_loops + 1)
                return
            if isinstance(stmt, DeferStmt):
                visit_block(stmt.body, nested_loops)
                return
            if isinstance(stmt, LetStmt):
                visit_expr(stmt.init, nested_loops)
                return
            if isinstance(stmt, AssignStmt):
                visit_expr(stmt.value, nested_loops)
                return
            if isinstance(stmt, ReturnStmt):
                visit_expr(stmt.value, nested_loops)
                return
            if isinstance(stmt, ExprStmt):
                visit_expr(stmt.expr, nested_loops)
                return

        def visit_block(block: Optional[Block], nested_loops: int):
            if block is None:
                return
            for stmt in block.stmts:
                visit_stmt(stmt, nested_loops)
            visit_expr(block.tail, nested_loops)

        visit_block(body, 0)
        unique_types = []
        for result_type in result_types:
            if result_type not in unique_types:
                unique_types.append(result_type)
        if not unique_types:
            return None
        if len(unique_types) > 1:
            raise MIRGenError(f"loop break values produced incompatible MIR types: {', '.join(unique_types)}")
        return unique_types[0]

    def _break_targets_loop(self, break_label: Optional[str], loop_label: Optional[str], nested_loops: int) -> bool:
        if break_label is not None:
            return break_label == loop_label
        return nested_loops == 0

    def _lower_while(self, stmt: WhileStmt):
        result_name = self._prepare_loop_result_binding(stmt.body, stmt.label)
        before = self.snapshot_scopes()
        carried = self._loop_carried_from_snapshot(before)
        cond_block = self.new_block("loop_cond", self._loop_params(carried))
        body_block = self.new_block("loop_body", self._loop_params(carried))
        exit_block = self.new_block("loop_exit", self._loop_params(carried))
        self.current.terminator = MIRGoto(cond_block.name, self._loop_initial_args(before, carried))

        self._loop_stack.append(
            LoopContext(
                label=stmt.label,
                continue_target=cond_block.name,
                break_target=exit_block.name,
                continue_names=[name for name, _ in carried],
                break_names=[name for name, _ in carried],
                result_name=result_name,
            )
        )
        try:
            self.switch_to(cond_block)
            self._restore_loop_scope(before, carried, cond_block)
            cond_text = self._lower_condition(stmt.cond)
            cond_args = [param.name for param in cond_block.params]
            self.current.terminator = MIRCmpGoto(
                cond_text,
                f"{body_block.name}({', '.join(cond_args)})" if cond_args else body_block.name,
                f"{exit_block.name}({', '.join(cond_args)})" if cond_args else exit_block.name,
            )

            self.switch_to(body_block)
            self._restore_loop_scope(before, carried, body_block)
            self._lower_statement_block(stmt.body)
            if self.current.terminator is None:
                self.current.terminator = MIRGoto(cond_block.name, self._loop_current_args(carried))
        finally:
            self._loop_stack.pop()

        self.switch_to(exit_block)
        self._restore_loop_scope(before, carried, exit_block)

    def _lower_for_range(self, stmt: ForRangeStmt):
        self.push_scope()
        try:
            start_value = self._materialize_binding(stmt.var, stmt.start)
            self.define_binding(
                stmt.var,
                Binding(start_value, self._expr_type_text(stmt.start) or "int", mutable=True),
            )
            result_name = self._prepare_loop_result_binding(stmt.body, stmt.label)
            end_value = self._materialize_value(
                self._lower_expr(stmt.end),
                self._expr_type_text(stmt.end) or "int",
                "loop_end",
            )

            before = self.snapshot_scopes()
            carried = self._loop_carried_from_snapshot(before, include=[stmt.var])
            cond_block = self.new_block("loop_cond", self._loop_params(carried))
            body_block = self.new_block("loop_body", self._loop_params(carried))
            step_block = self.new_block("loop_step", self._loop_params(carried))
            exit_carried = [(name, type_) for name, type_ in carried if name != stmt.var]
            exit_block = self.new_block("loop_exit", self._loop_params(exit_carried))
            self.current.terminator = MIRGoto(cond_block.name, self._loop_initial_args(before, carried))

            self._loop_stack.append(
                LoopContext(
                    label=stmt.label,
                    continue_target=step_block.name,
                    break_target=exit_block.name,
                    continue_names=[name for name, _ in carried],
                    break_names=[name for name, _ in exit_carried],
                    result_name=result_name,
                )
            )
            try:
                self.switch_to(cond_block)
                self._restore_loop_scope(before, carried, cond_block)
                loop_var = self.lookup_binding(stmt.var).ssa_name
                cmp_op = "<=" if stmt.inclusive else "<"
                body_args = [param.name for param in cond_block.params]
                exit_args = [self.lookup_binding(name).ssa_name for name, _ in exit_carried]
                self.current.terminator = MIRCmpGoto(
                    f"{loop_var} {cmp_op} {end_value}",
                    f"{body_block.name}({', '.join(body_args)})" if body_args else body_block.name,
                    f"{exit_block.name}({', '.join(exit_args)})" if exit_args else exit_block.name,
                )

                if stmt.filter is None:
                    self.switch_to(body_block)
                    self._restore_loop_scope(before, carried, body_block)
                    self._lower_statement_block(stmt.body)
                    if self.current.terminator is None:
                        self.current.terminator = MIRGoto(step_block.name, self._loop_current_args(carried))
                else:
                    filter_body_block = self.new_block("loop_body_filter")
                    self.switch_to(body_block)
                    self._restore_loop_scope(before, carried, body_block)
                    filter_text = self._lower_condition(stmt.filter)
                    current_args = self._loop_current_args(carried)
                    self.current.terminator = MIRCmpGoto(
                        filter_text,
                        f"{filter_body_block.name}({', '.join(current_args)})" if current_args else filter_body_block.name,
                        f"{step_block.name}({', '.join(current_args)})" if current_args else step_block.name,
                    )

                    filter_body_block.params = self._loop_params(carried)
                    self.switch_to(filter_body_block)
                    self._restore_loop_scope(before, carried, filter_body_block)
                    self._lower_statement_block(stmt.body)
                    if self.current.terminator is None:
                        self.current.terminator = MIRGoto(step_block.name, self._loop_current_args(carried))

                self.switch_to(step_block)
                self._restore_loop_scope(before, carried, step_block)
                current_var = self.lookup_binding(stmt.var)
                next_var = self.emit_assign(
                    self._fresh_value_name(stmt.var),
                    f"{current_var.ssa_name} + 1",
                    current_var.type_,
                )
                self.update_binding(stmt.var, Binding(next_var, current_var.type_, mutable=True))
                self.current.terminator = MIRGoto(cond_block.name, self._loop_current_args(carried))
            finally:
                self._loop_stack.pop()

            self.switch_to(exit_block)
            self._restore_loop_scope(before, exit_carried, exit_block)
        finally:
            self.pop_scope()

    def _lower_for_iter(self, stmt: ForIterStmt):
        iter_ty = getattr(stmt.iter, "_resolved_type", None) or getattr(stmt.iter, "_checked_type", None)
        if not isinstance(iter_ty, (TVec, TSlice, TArray)):
            raise MIRGenError("for-in lowering currently supports vec, slice, and array iterables")

        self.push_scope()
        try:
            result_name = self._prepare_loop_result_binding(stmt.body, stmt.label)
            iter_source = self._lower_expr(stmt.iter)
            if self.current.terminator is not None:
                return
            iter_value = self._materialize_value(
                iter_source,
                self._expr_type_text(stmt.iter),
                "iter",
            )
            index_value = self.emit_assign(
                self._fresh_value_name("i"),
                "0",
                "int",
            )
            len_expr = self._for_iter_len_expr(iter_value, iter_ty)
            len_type = "int"
            len_value = self._materialize_value(len_expr, len_type, "iter_len")

            self.define_binding("__iter_value", Binding(iter_value, self._expr_type_text(stmt.iter), mutable=False))
            self.define_binding("__iter_index", Binding(index_value, "int", mutable=True))
            self.define_binding("__iter_len", Binding(len_value, len_type, mutable=False))

            before = self.snapshot_scopes()
            carried = self._loop_carried_from_snapshot(before)
            cond_block = self.new_block("loop_cond", self._loop_params(carried))
            body_block = self.new_block("loop_body", self._loop_params(carried))
            step_block = self.new_block("loop_step", self._loop_params(carried))
            exit_carried = [(name, type_) for name, type_ in carried if name != "__iter_index"]
            exit_block = self.new_block("loop_exit", self._loop_params(exit_carried))
            self.current.terminator = MIRGoto(cond_block.name, self._loop_initial_args(before, carried))

            self._loop_stack.append(
                LoopContext(
                    label=stmt.label,
                    continue_target=step_block.name,
                    break_target=exit_block.name,
                    continue_names=[name for name, _ in carried],
                    break_names=[name for name, _ in exit_carried],
                    result_name=result_name,
                )
            )
            try:
                self.switch_to(cond_block)
                self._restore_loop_scope(before, carried, cond_block)
                index_name = self.lookup_binding("__iter_index").ssa_name
                len_name = self.lookup_binding("__iter_len").ssa_name
                body_args = [param.name for param in cond_block.params]
                exit_args = [self.lookup_binding(name).ssa_name for name, _ in exit_carried]
                self.current.terminator = MIRCmpGoto(
                    f"{index_name} < {len_name}",
                    f"{body_block.name}({', '.join(body_args)})" if body_args else body_block.name,
                    f"{exit_block.name}({', '.join(exit_args)})" if exit_args else exit_block.name,
                )

                if stmt.filter is None:
                    self.switch_to(body_block)
                    self._restore_loop_scope(before, carried, body_block)
                    self._lower_for_iter_body(stmt, iter_ty)
                    if self.current.terminator is None:
                        self.current.terminator = MIRGoto(step_block.name, self._loop_current_args(carried))
                else:
                    filter_body_block = self.new_block("loop_body_filter", self._loop_params(carried))
                    self.switch_to(body_block)
                    self._restore_loop_scope(before, carried, body_block)
                    self.push_scope()
                    try:
                        self._bind_for_pattern(stmt.pattern, iter_ty)
                        filter_text = self._lower_condition(stmt.filter)
                    finally:
                        self.pop_scope()
                    current_args = self._loop_current_args(carried)
                    self.current.terminator = MIRCmpGoto(
                        filter_text,
                        f"{filter_body_block.name}({', '.join(current_args)})" if current_args else filter_body_block.name,
                        f"{step_block.name}({', '.join(current_args)})" if current_args else step_block.name,
                    )

                    self.switch_to(filter_body_block)
                    self._restore_loop_scope(before, carried, filter_body_block)
                    self._lower_for_iter_body(stmt, iter_ty)
                    if self.current.terminator is None:
                        self.current.terminator = MIRGoto(step_block.name, self._loop_current_args(carried))

                self.switch_to(step_block)
                self._restore_loop_scope(before, carried, step_block)
                current_index = self.lookup_binding("__iter_index")
                next_index = self.emit_assign(
                    self._fresh_value_name("i"),
                    f"{current_index.ssa_name} + 1",
                    current_index.type_,
                )
                self.update_binding("__iter_index", Binding(next_index, current_index.type_, mutable=True))
                self.current.terminator = MIRGoto(cond_block.name, self._loop_current_args(carried))
            finally:
                self._loop_stack.pop()

            self.switch_to(exit_block)
            self._restore_loop_scope(before, exit_carried, exit_block)
        finally:
            self.pop_scope()


    def _lower_break(self, stmt: BreakStmt):
        break_value = None
        if stmt.value is not None:
            break_value = self._lower_expr(stmt.value, preferred_name="break_value")
            if self.current.terminator is not None:
                return
        loop = self._find_loop(stmt.label)
        if loop is None:
            raise MIRGenError("break outside loop reached MIR lowering")
        break_args, break_types = self._loop_jump_args(loop.break_names)
        if break_value is not None and loop.result_name is not None:
            try:
                result_index = loop.break_names.index(loop.result_name)
            except ValueError as exc:
                raise MIRGenError("loop result binding is missing from break arguments") from exc
            break_args[result_index] = break_value
        self.current.terminator = self._route_jump(loop.break_target, break_args, break_types, "break_cleanup")

    def _lower_continue(self, stmt: ContinueStmt):
        loop = self._find_loop(stmt.label)
        if loop is None:
            raise MIRGenError("continue outside loop reached MIR lowering")
        continue_args, continue_types = self._loop_jump_args(loop.continue_names)
        self.current.terminator = self._route_jump(loop.continue_target, continue_args, continue_types, "continue_cleanup")

    def _lower_statement_block(self, block: Block):
        self.push_scope()
        defer_scope: List[DeferStmt] = []
        self._defer_scopes.append(defer_scope)
        try:
            for stmt in block.stmts:
                if self.current.terminator is not None:
                    break
                self._lower_stmt(stmt)
            if self.current.terminator is not None:
                return
            if block.tail is not None:
                self._lower_expr_stmt(block.tail)
            if self.current.terminator is None:
                self._run_defer_scope(defer_scope)
        finally:
            self._defer_scopes.pop()
            self.pop_scope()

    def _lower_defer(self, stmt: DeferStmt):
        if not self._defer_scopes:
            raise MIRGenError("defer used without an active scope")
        self._defer_scopes[-1].append(stmt)

    def _run_defer_scope(self, defer_scope: List[DeferStmt], *, error_exit: bool = False):
        for stmt in reversed(defer_scope):
            if stmt.error_only and not error_exit:
                continue
            if self.current.terminator is not None:
                return
            self._lower_statement_block(stmt.body)

    def _find_loop(self, label: Optional[str]) -> Optional[LoopContext]:
        if label is None:
            return self._loop_stack[-1] if self._loop_stack else None
        for loop in reversed(self._loop_stack):
            if loop.label == label:
                return loop
        return None

    def _route_jump(
        self,
        target: str,
        args: List[str],
        arg_types: List[Optional[str]],
        seed: str,
        *,
        error_exit: bool = False,
    ) -> MIRGoto:
        final_target, final_args = self._route_cleanup_chain(target, args, arg_types, seed)
        final_target, final_args = self._route_defer_chain(
            final_target,
            final_args,
            arg_types,
            seed,
            error_exit=error_exit,
        )
        return MIRGoto(final_target, final_args)

    def _route_cleanup_chain(
        self,
        target: str,
        args: List[str],
        arg_types: List[Optional[str]],
        seed: str,
    ) -> Tuple[str, List[str]]:
        final_target = target
        final_args = list(args)
        saved_block = self.current
        for ctx in self._cleanup_stack:
            params = [MIRParam(self._fresh_value_name("jump"), ty) for ty in arg_types]
            cleanup_block = self.new_block(seed, params)
            self.switch_to(cleanup_block)
            self.emit_eval(f"region.cleanup {ctx.region_value} {ctx.cleanup}")
            self.current.terminator = MIRGoto(final_target, [param.name for param in params] if params else [])
            final_target = cleanup_block.name
            final_args = list(args)
        self.switch_to(saved_block)
        return final_target, final_args

    def _route_defer_chain(
        self,
        target: str,
        args: List[str],
        arg_types: List[Optional[str]],
        seed: str,
        *,
        error_exit: bool,
    ) -> Tuple[str, List[str]]:
        final_target = target
        final_args = list(args)
        saved_block = self.current
        for scope in reversed(self._defer_scopes):
            for stmt in reversed(scope):
                if stmt.error_only and not error_exit:
                    continue
                params = [MIRParam(self._fresh_value_name("jump"), ty) for ty in arg_types]
                defer_block = self.new_block(seed, params)
                self.switch_to(defer_block)
                self._lower_statement_block(stmt.body)
                if self.current.terminator is None:
                    self.current.terminator = MIRGoto(final_target, [param.name for param in params] if params else [])
                final_target = defer_block.name
                final_args = list(args)
        self.switch_to(saved_block)
        return final_target, final_args

    def _with_inner_value_type(self, expr: WithExpr) -> Optional[str]:
        if isinstance(expr.body.tail, EscExpr):
            return self._esc_value_type(expr.body.tail)
        return self._expr_type_text(expr.body.tail)

    def _with_alloc_value_type(self, expr: WithAllocExpr) -> Optional[str]:
        inner_ty = getattr(expr.expr, "_resolved_type", None) or getattr(expr.expr, "_checked_type", None)
        if isinstance(inner_ty, TErrorUnion):
            return format_type_for_user(inner_ty.payload)
        return self._expr_type_text(expr.expr)

    def _esc_value_type(self, expr: EscExpr) -> Optional[str]:
        inner = getattr(expr.expr, "_resolved_type", None) or getattr(expr.expr, "_checked_type", None)
        if isinstance(inner, Type):
            return format_type_for_user(inner)
        esc_ty = getattr(expr, "_resolved_type", None)
        if isinstance(esc_ty, TErrorUnion):
            return format_type_for_user(esc_ty.payload)
        return None

    def _wrap_with_result(self, expr: WithExpr, value_name: Optional[str]) -> Optional[str]:
        result_ty = getattr(expr, "_resolved_type", None)
        if not isinstance(result_ty, TErrorUnion):
            return value_name
        if isinstance(expr.body.tail, EscExpr):
            if isinstance(result_ty.payload, TVoid):
                return "result.ok()"
            return f"result.ok({value_name})"
        return value_name

    def _route_return_terminator(
        self,
        terminator: MIRReturn,
        *,
        error_exit: bool = False,
    ) -> MIRReturn | MIRGoto:
        args = [terminator.value] if terminator.value is not None else []
        arg_types = [self._current_fn_return_type_text] if terminator.value is not None else []

        if self._cleanup_stack:
            target, routed_args = self._route_defer_chain(
                self._cleanup_stack[-1].return_block.name,
                args,
                arg_types,
                "return_defer",
                error_exit=error_exit,
            )
            return MIRGoto(target, routed_args)

        has_defer_routing = any(
            (not stmt.error_only or error_exit)
            for scope in self._defer_scopes
            for stmt in scope
        )
        if not has_defer_routing:
            return terminator

        params = [MIRParam(self._fresh_value_name("return_value"), ty) for ty in arg_types]
        return_block = self.new_block("return_exit", params)
        saved_block = self.current
        self.switch_to(return_block)
        return_value = return_block.params[0].name if return_block.params else None
        self.current.terminator = MIRReturn(return_value)
        self.switch_to(saved_block)
        target, routed_args = self._route_defer_chain(
            return_block.name,
            args,
            arg_types,
            "return_defer",
            error_exit=error_exit,
        )
        return MIRGoto(target, routed_args)

    def _route_handle_target(self, err_value: str) -> str:
        if self._local_handle_blocks:
            handle_block = self._local_handle_blocks[-1]
        elif self._cleanup_stack:
            handle_block = self._cleanup_stack[-1].handle_block
            if handle_block is None:
                raise MIRGenError("missing cleanup-aware handle target")
        else:
            handle_block = self._current_fn_handle_block
            if handle_block is None:
                raise MIRGenError("missing function handle target")
        arg_types = [handle_block.params[0].type_] if handle_block.params else []
        target, args = self._route_defer_chain(
            handle_block.name,
            [err_value],
            arg_types,
            "handle_defer",
            error_exit=True,
        )
        if args:
            return f"{target}({', '.join(args)})"
        return target

    def _lower_condition(self, expr: Expr) -> str:
        if isinstance(expr, BinExpr) and expr.op in _COMPARE_OPS:
            left = self._lower_expr(expr.left)
            right = self._lower_expr(expr.right)
            return f"{left} {expr.op} {right}"
        return self._lower_expr(expr)

    def _materialize_binding(self, name: str, expr: Expr) -> str:
        return self._lower_expr(expr, preferred_name=name)

    def _return_terminator_for_value(self, expr: Optional[Expr], value: Optional[str]) -> MIRReturn:
        ret_ty = self._current_fn_return_type
        if not isinstance(ret_ty, TErrorUnion):
            return MIRReturn(value)
        if expr is None:
            if isinstance(ret_ty.payload, TVoid):
                return MIRReturn("result.ok()")
            return MIRReturn()

        expr_ty = getattr(expr, "_resolved_type", None) or getattr(expr, "_checked_type", None)
        if isinstance(expr, EscExpr):
            if isinstance(ret_ty.payload, TVoid):
                return MIRReturn("result.ok()")
            return MIRReturn(f"result.ok({value})")
        if isinstance(expr, WithExpr) and isinstance(expr_ty, TErrorUnion):
            return MIRReturn(value)
        if isinstance(expr_ty, TErrorUnion):
            return MIRReturn(value)
        if isinstance(expr_ty, (TErrorSet, TErrorSetUnion)):
            return MIRReturn(f"result.err({value})")
        if isinstance(ret_ty.payload, TVoid):
            return MIRReturn("result.ok()")
        return MIRReturn(f"result.ok({value})")

    def _emit_return(self, expr: Optional[Expr], value: Optional[str]):
        if expr is not None and self._should_split_result_return(expr):
            self._emit_split_result_return(value)
            return
        self.current.terminator = self._route_return_terminator(
            self._return_terminator_for_value(expr, value),
            error_exit=self._is_error_return_expr(expr),
        )

    def _should_split_result_return(self, expr: Expr) -> bool:
        if not self._has_error_only_defers():
            return False
        expr_ty = getattr(expr, "_resolved_type", None) or getattr(expr, "_checked_type", None)
        return isinstance(expr_ty, TErrorUnion)

    def _emit_split_result_return(self, value: Optional[str]):
        if value is None:
            raise MIRGenError("cannot split a missing result return")
        source_block = self.current
        ok_block = self.new_block("return_ok")
        err_block = self.new_block("return_err")
        source_block.terminator = MIRSwitchResult(
            value,
            ok_target=ok_block.name,
            err_target=err_block.name,
        )
        self.switch_to(ok_block)
        self.current.terminator = self._route_return_terminator(MIRReturn(value), error_exit=False)
        self.switch_to(err_block)
        self.current.terminator = self._route_return_terminator(MIRReturn(value), error_exit=True)

    def _is_error_return_expr(self, expr: Optional[Expr]) -> bool:
        ret_ty = self._current_fn_return_type
        if expr is None or not isinstance(ret_ty, TErrorUnion):
            return False
        expr_ty = getattr(expr, "_resolved_type", None) or getattr(expr, "_checked_type", None)
        return isinstance(expr_ty, (TErrorSet, TErrorSetUnion))

    def _has_error_only_defers(self) -> bool:
        return any(stmt.error_only for scope in self._defer_scopes for stmt in scope)

    def _lower_pointer_assignment_value(self, stmt: AssignStmt, ptr_value: str, pointee_type: str) -> str:
        if stmt.op == "=":
            return self._lower_expr(stmt.value)
        if stmt.op not in _COMPOUND_BINOPS:
            raise MIRGenError(f"compound assignment '{stmt.op}' is not lowered yet")
        loaded = self.emit_assign(
            self._fresh_value_name("load"),
            f"load {ptr_value}",
            pointee_type,
        )
        rhs = self._lower_expr(stmt.value)
        return self.emit_assign(
            self._fresh_value_name("store_value"),
            f"{loaded} {_COMPOUND_BINOPS[stmt.op]} {rhs}",
            pointee_type,
        )

    def _materialize_simple(
        self,
        text: str,
        type_text: Optional[str],
        preferred_name: Optional[str],
        *,
        annotate_type: bool = False,
    ) -> str:
        if preferred_name is None:
            return text
        return self.emit_assign(
            self._fresh_value_name(preferred_name),
            text,
            type_text,
            annotate_type=annotate_type,
        )

    def _materialize_value(
        self,
        text: str,
        type_text: Optional[str],
        preferred_name: str,
        *,
        annotate_type: bool = False,
    ) -> str:
        return self.emit_assign(
            self._fresh_value_name(preferred_name),
            text,
            type_text,
            annotate_type=annotate_type,
        )

    def _expr_type_text(self, expr: Optional[Expr]) -> Optional[str]:
        if expr is None:
            return None
        ty = getattr(expr, "_resolved_type", None) or getattr(expr, "_checked_type", None)
        if isinstance(ty, Type):
            return format_type_for_user(ty)
        if isinstance(expr, IntLit):
            return "int"
        if isinstance(expr, FloatLit):
            return "float"
        if isinstance(expr, BoolLit):
            return "bool"
        if isinstance(expr, StringLit):
            return "str"
        return None

    def _infer_expr_type_text(self, expr: Optional[Expr]) -> Optional[str]:
        direct = self._expr_type_text(expr)
        if direct is not None:
            return direct
        if expr is None:
            return None
        if isinstance(expr, Ident):
            return self.lookup_binding(expr.name).type_
        if isinstance(expr, BinExpr):
            if expr.op in _COMPARE_OPS:
                return "bool"
            left_type = self._infer_expr_type_text(expr.left)
            right_type = self._infer_expr_type_text(expr.right)
            if left_type is not None:
                return left_type
            return right_type
        if isinstance(expr, UnaryExpr):
            return self._infer_expr_type_text(expr.operand)
        return None

    def _render_call(self, expr: CallExpr) -> str:
        tagged = self._render_tagged_constructor(expr)
        if tagged is not None:
            return tagged
        callee = self._direct_name(expr.callee)
        args = ", ".join(self._lower_expr(arg.value) for arg in expr.args)
        if callee is not None:
            return f"{callee}({args})"
        target = self._lower_expr(expr.callee)
        return f"invoke {target}({args})"

    def _render_tagged_constructor(self, expr: CallExpr) -> Optional[str]:
        owner = self._tagged_owner_name(getattr(expr, "_resolved_type", None))
        if owner is None:
            return None

        variant_name = None
        if isinstance(expr.callee, VariantLit):
            variant_name = expr.callee.name
        elif isinstance(expr.callee, FieldExpr) and isinstance(expr.callee.obj, Ident):
            variant_name = expr.callee.field
        if variant_name is None:
            return None

        args = ", ".join(self._lower_expr(arg.value) for arg in expr.args)
        if args:
            return f"make.tagged {owner}.{variant_name}({args})"
        return f"make.tagged {owner}.{variant_name}"

    def _direct_name(self, expr: Expr) -> Optional[str]:
        if isinstance(expr, Ident):
            return expr.name if expr.name.startswith("@") else f"@{expr.name}"
        if isinstance(expr, FieldExpr):
            root = self._direct_name(expr.obj)
            if root is None:
                return None
            return f"{root}.{expr.field}"
        return None

    def _render_unit_variant(self, expr: VariantLit) -> Optional[str]:
        owner = self._tagged_owner_name(getattr(expr, "_resolved_type", None))
        if owner is None:
            return None
        return f"make.tagged {owner}.{expr.name}"

    def _render_qualified_unit_variant(self, expr: FieldExpr) -> Optional[str]:
        if not isinstance(expr.obj, Ident):
            return None
        owner_ty = getattr(expr, "_resolved_type", None)
        owner = self._tagged_owner_name(owner_ty)
        if owner is None:
            return None
        return f"make.tagged {owner}.{expr.field}"

    def _tagged_owner_name(self, ty: Optional[Type]) -> Optional[str]:
        if isinstance(ty, TUnion):
            return ty.name
        if isinstance(ty, TErrorSet):
            return ty.name
        return None

    def _pattern_block_seed(self, pattern: MatchPattern) -> str:
        if isinstance(pattern, PatVariant):
            return f"match_{pattern.name.lower()}"
        if isinstance(pattern, PatWildcard):
            return "match_default"
        if isinstance(pattern, PatIdent):
            return "match_bind"
        return "match_arm"

    def _bind_match_pattern_value(self, pattern: MatchPattern, match_value: str, match_ty: Type):
        if isinstance(pattern, PatWildcard):
            return
        if isinstance(pattern, PatIdent):
            if pattern.name != "_":
                self.define_binding(pattern.name, Binding(match_value, format_type_for_user(match_ty), mutable=False))
            return
        if not isinstance(pattern, PatVariant):
            raise MIRGenError(f"match pattern {type(pattern).__name__} is not lowered yet")

        payload_ty = self._match_payload_type(match_ty, pattern.name)
        if payload_ty is None:
            return

        payload_type_text = format_type_for_user(payload_ty)
        payload_name = self._fresh_value_name(pattern.binding or pattern.name.lower())
        owner = self._tagged_owner_name(match_ty)
        if owner is None:
            owner = format_type_for_user(match_ty)
        self.emit_assign(
            payload_name,
            f"read.payload {match_value} as {owner}.{pattern.name}",
            payload_type_text,
        )

        all_bindings = ([pattern.binding] if pattern.binding else []) + list(pattern.extra_bindings or [])
        if not all_bindings:
            return

        if isinstance(payload_ty, TTuple) and len(all_bindings) > 1:
            for idx, binding_name in enumerate(all_bindings):
                field_ty = payload_ty.fields[idx][1] if idx < len(payload_ty.fields) else payload_ty
                field_value = self._fresh_value_name(binding_name)
                self.emit_assign(
                    field_value,
                    f"tuple.get {payload_name}, {idx}",
                    format_type_for_user(field_ty),
                )
                self.define_binding(binding_name, Binding(field_value, format_type_for_user(field_ty), mutable=False))
            return

        if pattern.binding:
            self.define_binding(pattern.binding, Binding(payload_name, payload_type_text, mutable=False))
        for binding_name in pattern.extra_bindings or []:
            alias_name = self._fresh_value_name(binding_name)
            self.emit_assign(alias_name, payload_name, payload_type_text)
            self.define_binding(binding_name, Binding(alias_name, payload_type_text, mutable=False))

    def _match_payload_type(self, match_ty: Type, variant_name: str) -> Optional[Type]:
        if isinstance(match_ty, TUnion):
            return match_ty.variant_payload(variant_name)
        if isinstance(match_ty, (TErrorSet, TErrorSetUnion)):
            return error_set_variants(match_ty).get(variant_name)
        return None

    def _visible_bindings_from_snapshot(self, snapshot: List[Dict[str, Binding]]) -> Dict[str, Binding]:
        result: Dict[str, Binding] = {}
        for scope in snapshot:
            result.update({name: binding.clone() for name, binding in scope.items()})
        return result

    def _fresh_block_name(self, seed: str) -> str:
        base = self._sanitize(seed)
        count = self._block_counts.get(base, 0) + 1
        self._block_counts[base] = count
        if count == 1:
            return base
        return f"{base}_{count}"

    def _fresh_value_name(self, seed: str) -> str:
        base = self._sanitize(seed).lstrip("%") or "tmp"
        count = self._value_counts.get(base, 0) + 1
        self._value_counts[base] = count
        if count == 1:
            return f"%{base}"
        return f"%{base}_{count}"

    def _prefixed_value_name(self, name: str) -> str:
        return f"%{self._sanitize(name)}"

    def _sanitize(self, text: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
        if not cleaned:
            cleaned = "tmp"
        if cleaned[0].isdigit():
            cleaned = f"v_{cleaned}"
        return cleaned

    def _bin_hint(self, op: str) -> str:
        return {
            "+": "sum",
            "-": "diff",
            "*": "prod",
            "/": "quot",
            "%": "rem",
            "^": "pow",
            "==": "eq",
            "!=": "ne",
            "<": "lt",
            ">": "gt",
            "<=": "le",
            ">=": "ge",
        }.get(op, "tmp")

    def _unary_hint(self, op: str) -> str:
        return {
            "-": "neg",
            "!": "not",
            "*": "load",
            "@": "addr",
        }.get(op, "tmp")

    def _call_hint(self, expr: CallExpr) -> str:
        if isinstance(expr.callee, VariantLit):
            return expr.callee.name.lower()
        if isinstance(expr.callee, FieldExpr):
            owner = self._tagged_owner_name(getattr(expr, "_resolved_type", None))
            if owner is not None:
                return expr.callee.field.lower()
        direct = self._direct_name(expr.callee)
        if direct is None:
            return "call"
        return direct.lstrip("@").split(".")[-1]

    def _quote_string(self, text: str) -> str:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'


def emit_mir(program: Program, env) -> MIRModule:
    return MIRGenerator(env).emit_program(program)


def emit_mir_for_frontend(state: FrontendState) -> MIRModule:
    if state.program is None or state.env is None:
        raise MIRGenError("frontend state has no checked program")
    return emit_mir(state.program, state.env)


def emit_mir_for_path(
    source_path: str,
    *,
    package_roots=None,
    local_root=None,
) -> MIRModule:
    state = build_frontend_state_for_path(
        source_path,
        package_roots=package_roots,
        local_root=local_root,
    )
    if state.diags.has_errors():
        raise MIRGenError("cannot emit MIR for an invalid program")
    return emit_mir_for_frontend(state)
