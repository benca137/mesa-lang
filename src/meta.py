"""
Editor-facing metadata built on top of the Mesa frontend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from src.syntax.ast import (
    Arg,
    AssignStmt,
    Block,
    BlockExpr,
    BreakStmt,
    CallExpr,
    ContinueStmt,
    DefDecl,
    DeferStmt,
    ErrorDecl,
    Expr,
    ExprStmt,
    FieldExpr,
    ForIterStmt,
    ForPattern,
    ForRangeStmt,
    FromExportDecl,
    FromImportDecl,
    FunDecl,
    HandleBlock,
    Ident,
    IfExpr,
    IfUnwrap,
    ImportDecl,
    IndexExpr,
    InterfaceDecl,
    LetStmt,
    MatchArm,
    MatchExpr,
    MatchPattern,
    PkgDecl,
    PkgExportAllDecl,
    PkgExportDecl,
    Param,
    PatIdent,
    PatRef,
    PatTuple,
    PatVariant,
    PatWildcard,
    Program,
    ReturnStmt,
    SelfExpr,
    SourcePos,
    SourceSpan,
    StructDecl,
    TypeAlias,
    TypeExpr,
    UnionDecl,
    WhileStmt,
    WhileUnwrap,
    WithExpr,
)
from src.semantics.checker import lower_type
from src.semantics.env import Diagnostic, Environment
from src.frontend import (
    FrontendState,
    _parse_frontend_state_for_path,
    build_frontend_state,
    build_frontend_state_for_path,
)
from src.buildsys import BuildPlanError, load_build_plan
from src.syntax.parser import ParseError, Parser
from src.stdlib import is_std_source_path, resolve_package_root_path
from src.syntax.tokenizer import KEYWORDS, TK, Token, TokenizeError, Tokenizer
from src.semantics.types import (
    TArray,
    TAnyInterface,
    TBool,
    TDynInterface,
    TErrorSet,
    TFun,
    TFloat,
    TInterface,
    TInt,
    TMat,
    TOptional,
    TPointer,
    TSlice,
    TStruct,
    TString,
    TTuple,
    TUnion,
    TVec,
    TVoid,
    Type,
    TNamespace,
    T_BOOL,
    T_F64,
    T_I64,
    T_STR,
    T_VOID,
)


KEYWORD_LABELS = sorted(
    name for name in KEYWORDS.keys()
    if name not in {"true", "false", "none"}
)

SNIPPETS: dict[str, str] = {
    "fun": "fun ${1:name}(${2}) ${3:void} {\n    ${4}\n}",
    "struct": "struct ${1:Name} {\n    ${2}\n}",
    "if": "if ${1:cond} {\n    ${2}\n}",
    "for": "for ${1:item} in ${2:items} {\n    ${3}\n}",
    "match": "match ${1:value} {\n    ${2:_ => { }}\n}",
}

PUBLIC_BUILTIN_NAMES = (
    "print", "println", "println_i64", "println_f64", "len", "cap",
    "@typeof", "@assert", "@sizeOf", "@alignOf", "@hasField",
    "@test",
    "@memcpy", "@memmove", "@memset", "@memcmp",
    "@panic",
)
INTERNAL_BUILTIN_NAMES = (
    "@alloc", "@realloc", "@freeBytes",
    "@pageSize", "@pageAlloc", "@pageFree",
    "@cAlloc", "@cRealloc", "@cFree",
    "@ptrAdd",
)
PRIMITIVE_TYPE_NAMES = {"int", "float", "vec", "mat"}
ALLOCATOR_TYPE_NAMES = {"Allocator"}


@dataclass(frozen=True)
class CompletionCandidate:
    label: str
    kind: str
    detail: str = ""
    insert_text: Optional[str] = None
    insert_text_format: str = "plain"


@dataclass(frozen=True)
class SemanticTokenData:
    line: int
    start: int
    length: int
    token_type: str


@dataclass
class SymbolMeta:
    name: str
    kind: str
    type_: Optional[Type] = None
    detail: str = ""
    span: Optional[SourceSpan] = None
    defined_at: Optional[SourcePos] = None


@dataclass
class ScopeMeta:
    span: SourceSpan
    symbols: List[SymbolMeta] = field(default_factory=list)
    children: List["ScopeMeta"] = field(default_factory=list)


@dataclass
class MemberMeta:
    name: str
    kind: str
    detail: str
    type_: Optional[Type] = None
    owner: str = ""


@dataclass
class DocumentMeta:
    source: str
    frontend: FrontendState
    root_scope: ScopeMeta
    top_level_symbols: List[SymbolMeta]
    members_by_type: Dict[str, List[MemberMeta]]
    type_decls: Dict[str, Type]

    @property
    def tokens(self) -> List[Token]:
        return self.frontend.tokens

    @property
    def program(self) -> Optional[Program]:
        return self.frontend.program

    @property
    def env(self) -> Optional[Environment]:
        return self.frontend.env

    @property
    def diagnostics(self) -> List[Diagnostic]:
        return self.frontend.diags.all_diags()

    @property
    def parse_succeeded(self) -> bool:
        return self.frontend.parse_succeeded

    @property
    def typecheck_succeeded(self) -> bool:
        return self.frontend.typecheck_succeeded

    def visible_symbols_at(self, pos: SourcePos, fallback: Optional["DocumentMeta"] = None) -> List[SymbolMeta]:
        base = self if self.parse_succeeded else (fallback or self)
        collected: dict[str, SymbolMeta] = {}
        for sym in base._visible_symbols_in_scope(base.root_scope, pos):
            collected[sym.name] = sym
        return list(collected.values())

    def complete(self, line0: int, char0: int, fallback: Optional["DocumentMeta"] = None) -> List[CompletionCandidate]:
        pos = SourcePos(line=line0 + 1, col=char0 + 1)
        context = _completion_context(self.source, pos)
        base = self if self.parse_succeeded else (fallback or self)

        items: dict[str, CompletionCandidate] = {}
        if context.kind == "member":
            owner_type = base.resolve_receiver_type(context.receiver_text, pos)
            if owner_type is not None:
                for member in base.members_for_type(owner_type):
                    items[member.name] = CompletionCandidate(
                        label=member.name,
                        kind=member.kind,
                        detail=member.detail,
                    )
                return _sorted_candidates(items.values(), context.prefix)

        for label, snippet in SNIPPETS.items():
            items[label] = CompletionCandidate(
                label=label,
                kind="snippet",
                detail="Mesa snippet",
                insert_text=snippet,
                insert_text_format="snippet",
            )
        for keyword in KEYWORD_LABELS:
            items.setdefault(keyword, CompletionCandidate(label=keyword, kind="keyword"))
        for sym in base.visible_symbols_at(pos, fallback=fallback):
            items[sym.name] = CompletionCandidate(
                label=sym.name,
                kind=sym.kind,
                detail=sym.detail or (_type_detail(sym.type_) if sym.type_ else ""),
            )
        return _sorted_candidates(items.values(), context.prefix)

    def resolve_receiver_type(self, receiver_text: str, pos: SourcePos) -> Optional[Type]:
        if not receiver_text:
            return None
        try:
            expr = Parser(Tokenizer(receiver_text).tokenize()).parse_expr_only()
        except (TokenizeError, ParseError):
            return None
        return self._resolve_expr_type(expr, pos)

    def members_for_type(self, ty: Type) -> List[MemberMeta]:
        items = self._members_for_type(ty)
        return sorted(items, key=lambda item: item.name)

    def semantic_tokens(self) -> List[SemanticTokenData]:
        tokens = [tok for tok in self.tokens if tok.kind not in (TK.EOF, TK.NEWLINE)]
        marked: dict[tuple[int, int], SemanticTokenData] = {}

        def mark(tok: Token, token_type: str) -> None:
            key = (tok.line, tok.col)
            marked[key] = SemanticTokenData(
                line=tok.line - 1,
                start=tok.col - 1,
                length=max(len(tok.lexeme), 1),
                token_type=token_type,
            )

        nominal_type_names = set()
        if self.env is not None:
            for name, ty in self.env._types.items():
                if isinstance(ty, (TStruct, TUnion, TErrorSet, TInterface)):
                    nominal_type_names.add(name)
        nominal_type_names.update(ALLOCATOR_TYPE_NAMES)
        primitive_type_names = set(PRIMITIVE_TYPE_NAMES)
        union_like_names = {
            decl.name
            for decl in (self.program.decls if self.program else [])
            if isinstance(decl, (UnionDecl, ErrorDecl))
        }
        variant_names = {
            variant.name
            for decl in (self.program.decls if self.program else [])
            if isinstance(decl, (UnionDecl, ErrorDecl))
            for variant in decl.variants
        }
        type_parameter_names = _collect_type_parameter_names(self.program)

        for index, tok in enumerate(tokens):
            prev_tok = tokens[index - 1] if index > 0 else None
            next_tok = tokens[index + 1] if index + 1 < len(tokens) else None
            next2_tok = tokens[index + 2] if index + 2 < len(tokens) else None

            if tok.kind in {
                TK.TY_I8, TK.TY_I16, TK.TY_I32, TK.TY_I64,
                TK.TY_U8, TK.TY_U16, TK.TY_U32, TK.TY_U64,
                TK.TY_F32, TK.TY_F64, TK.TY_BOOL, TK.TY_STR, TK.TY_VOID,
            }:
                mark(tok, "type")
            elif tok.kind in (TK.KW_STRUCT, TK.KW_UNION, TK.KW_ERROR, TK.KW_INTERFACE, TK.KW_TYPE):
                if next_tok and next_tok.kind == TK.IDENT:
                    mark(next_tok, "type")
            elif tok.kind == TK.KW_FUN:
                if next_tok and next_tok.kind == TK.IDENT:
                    mark(next_tok, "function")
            elif tok.kind == TK.IDENT and tok.lexeme in type_parameter_names and (
                _looks_like_type_usage(tokens, index, primitive_type_names | nominal_type_names | type_parameter_names)
                or _looks_like_generic_param_decl(tokens, index)
            ):
                mark(tok, "typeParameter")
            elif tok.kind == TK.IDENT and tok.lexeme in nominal_type_names and _looks_like_type_usage(tokens, index, primitive_type_names | nominal_type_names | type_parameter_names):
                mark(tok, "type")
            elif tok.kind == TK.IDENT and tok.lexeme in nominal_type_names and next_tok and next_tok.kind == TK.LPAREN:
                mark(tok, "type")
            elif tok.kind == TK.IDENT and tok.lexeme in primitive_type_names and _looks_like_type_usage(tokens, index, primitive_type_names | nominal_type_names | type_parameter_names):
                mark(tok, "type")
            elif tok.kind == TK.IDENT and tok.lexeme in primitive_type_names and next_tok and next_tok.kind == TK.LBRACKET:
                mark(tok, "type")
            elif tok.kind == TK.IDENT and _looks_like_pkg_path_token(tokens, index):
                mark(tok, "namespace")
            elif tok.kind == TK.IDENT and _looks_like_param_decl(tokens, index):
                mark(tok, "parameter")
            elif tok.kind == TK.IDENT:
                symbol = _visible_symbol_for_token(self, tok)
                if symbol is not None and symbol.kind == "package":
                    mark(tok, "namespace")
                elif symbol is not None and symbol.kind == "parameter":
                    mark(tok, "parameter")
                elif symbol is not None and isinstance(symbol.type_, TFun):
                    mark(tok, "function")
            elif tok.kind == TK.IDENT and next_tok and next_tok.kind == TK.LPAREN and (prev_tok is None or prev_tok.kind not in (TK.KW_FUN, TK.DOT, TK.QUESTION_DOT)):
                mark(tok, "function")
            elif tok.kind in (TK.DOT, TK.QUESTION_DOT) and next_tok and next_tok.kind == TK.IDENT:
                receiver_text = _extract_receiver_expr(self.source, _offset_for_pos(self.source, SourcePos(tok.line, tok.col)))
                receiver_ty = self.resolve_receiver_type(receiver_text, SourcePos(next_tok.line, next_tok.col))
                field_ty = _field_type_from_type(receiver_ty, next_tok.lexeme, self.env) if receiver_ty is not None else None
                if prev_tok and prev_tok.kind == TK.IDENT and prev_tok.lexeme in union_like_names:
                    mark(prev_tok, "type")
                    mark(next_tok, "enumMember")
                elif (prev_tok is None or prev_tok.kind not in (TK.IDENT, TK.RPAREN, TK.RBRACKET)) and next_tok.lexeme in variant_names:
                    mark(next_tok, "enumMember")
                elif isinstance(field_ty, TFun):
                    mark(next_tok, "function")
                else:
                    mark(next_tok, "property")

        if self.program is not None:
            for decl in self.program.decls:
                if isinstance(decl, StructDecl):
                    for tok in _struct_field_tokens(tokens, decl):
                        mark(tok, "property")
                elif isinstance(decl, (UnionDecl, ErrorDecl)):
                    for tok in _variant_decl_tokens(tokens, decl):
                        mark(tok, "enumMember")
                elif isinstance(decl, DefDecl):
                    for tok in _def_decl_type_tokens(tokens, decl):
                        mark(tok, "type")
                elif isinstance(decl, ImportDecl):
                    for tok in _import_path_tokens(tokens, decl):
                        mark(tok, "namespace")
                    if decl.alias:
                        alias_tok = _find_ident_token_in_span(tokens, decl.span, decl.alias)
                        if alias_tok is not None:
                            mark(alias_tok, "namespace")
                elif isinstance(decl, FromImportDecl):
                    for tok in _from_import_path_tokens(tokens, decl):
                        mark(tok, "namespace")
            if self.program.pkg is not None:
                for tok in _pkg_decl_path_tokens(tokens, self.program.pkg):
                    mark(tok, "namespace")

        for tok in _tuple_literal_field_tokens(tokens):
            mark(tok, "property")
        for tok in _with_cleanup_tokens(tokens):
            mark(tok, "function")

        return sorted(marked.values(), key=lambda item: (item.line, item.start, item.token_type))

    def _visible_symbols_in_scope(self, scope: ScopeMeta, pos: SourcePos) -> List[SymbolMeta]:
        if not _span_contains(scope.span, pos):
            return []

        visible: List[SymbolMeta] = []
        for sym in scope.symbols:
            if sym.defined_at is None or _pos_le(sym.defined_at, pos):
                visible.append(sym)

        for child in scope.children:
            if _span_contains(child.span, pos):
                visible.extend(self._visible_symbols_in_scope(child, pos))
                break
        return visible

    def _resolve_expr_type(self, expr: Expr, pos: SourcePos) -> Optional[Type]:
        resolved = getattr(expr, "_resolved_type", None)
        if resolved is not None:
            return resolved

        if isinstance(expr, Ident):
            for sym in reversed(self.visible_symbols_at(pos)):
                if sym.name == expr.name:
                    if sym.type_ is not None:
                        return sym.type_
                    break
            if self.env is not None:
                sym = self.env.lookup(expr.name)
                if sym is not None:
                    return sym.type_
                return self.env.lookup_type(expr.name)
            return None

        if isinstance(expr, SelfExpr):
            for sym in reversed(self.visible_symbols_at(pos)):
                if sym.name == "self":
                    return sym.type_
            return None

        if isinstance(expr, FieldExpr):
            obj_ty = self._resolve_expr_type(expr.obj, pos)
            if obj_ty is None:
                return None
            if isinstance(obj_ty, TNamespace) and self.env is not None:
                value_ty = self.env.lookup_namespace_value(obj_ty.name, expr.field)
                if value_ty is not None:
                    return value_ty
                type_ty = self.env.lookup_namespace_type(obj_ty.name, expr.field)
                if type_ty is not None:
                    return type_ty
            return _field_type_from_type(obj_ty, expr.field, self.env)

        if isinstance(expr, CallExpr):
            callee_ty = self._resolve_expr_type(expr.callee, pos)
            if isinstance(callee_ty, TFun):
                return callee_ty.ret
            return None

        if isinstance(expr, IndexExpr):
            obj_ty = self._resolve_expr_type(expr.obj, pos)
            obj_ty = _unwrap_member_type(obj_ty)
            if isinstance(obj_ty, (TVec, TSlice, TArray)):
                return obj_ty.inner
            if isinstance(obj_ty, TMat):
                return obj_ty.inner
            return None

        if isinstance(expr, IfExpr):
            return getattr(expr, "_resolved_type", None)

        if isinstance(expr, IfUnwrap):
            return getattr(expr, "_resolved_type", None)

        if isinstance(expr, MatchExpr):
            return getattr(expr, "_resolved_type", None)

        if isinstance(expr, WhileUnwrap):
            return T_VOID

        if hasattr(expr, "value") and isinstance(getattr(expr, "value", None), int):
            return T_I64
        if hasattr(expr, "value") and isinstance(getattr(expr, "value", None), float):
            return T_F64
        if isinstance(getattr(expr, "raw", None), str):
            return T_STR
        if isinstance(expr, WithExpr):
            return getattr(expr, "_resolved_type", None)
        return None

    def _members_for_type(self, ty: Type) -> List[MemberMeta]:
        ty = _unwrap_member_type(ty)
        env = self.env
        if ty is None:
            return []

        if isinstance(ty, TStruct):
            items: dict[str, MemberMeta] = {}
            for name, field_ty in ty.fields.items():
                items[name] = MemberMeta(
                    name=name,
                    kind="field",
                    detail=_type_detail(field_ty),
                    type_=field_ty,
                    owner=ty.name,
                )
            for name, method_ty in ty.methods.items():
                items[name] = MemberMeta(
                    name=name,
                    kind="method",
                    detail=_type_detail(method_ty),
                    type_=method_ty,
                    owner=ty.name,
                )
            if env is not None:
                for iface_name in env.impls.all_interfaces_for(ty.name):
                    methods = env.impls._impls.get((ty.name, iface_name), {})
                    for name, method_ty in methods.items():
                        items.setdefault(
                            name,
                            MemberMeta(
                                name=name,
                                kind="method",
                                detail=f"{iface_name}: {_type_detail(method_ty)}",
                                type_=method_ty,
                                owner=iface_name,
                            ),
                        )
            return list(items.values())

        if isinstance(ty, TInterface):
            return [
                MemberMeta(
                    name=name,
                    kind="method",
                    detail=_type_detail(method_ty),
                    type_=method_ty,
                    owner=ty.name,
                )
                for name, method_ty in ty.methods.items()
            ]

        if isinstance(ty, TAnyInterface):
            return self._members_for_type(ty.iface)

        if isinstance(ty, TDynInterface):
            return self._members_for_type(ty.iface)

        if isinstance(ty, TTuple):
            items: List[MemberMeta] = []
            for idx, (name, field_ty) in enumerate(ty.fields):
                label = name if name else str(idx)
                items.append(MemberMeta(
                    name=label,
                    kind="field",
                    detail=_type_detail(field_ty),
                    type_=field_ty,
                    owner="tuple",
                ))
            return items

        return []


def build_document_meta(source: str, source_path: Optional[str] = None) -> DocumentMeta:
    if source_path:
        frontend = _build_editor_frontend(source, source_path)
    else:
        frontend = build_frontend_state(source)
    file_span = _full_span(source)
    root_scope = ScopeMeta(span=file_span)

    top_level_symbols: List[SymbolMeta] = []
    type_decls: Dict[str, Type] = {}
    if frontend.env is not None:
        builtin_names = list(PUBLIC_BUILTIN_NAMES)
        if is_std_source_path(source_path):
            builtin_names.extend(INTERNAL_BUILTIN_NAMES)
        for name in builtin_names:
            sym = frontend.env.lookup(name)
            top_level_symbols.append(SymbolMeta(
                name=name,
                kind="function",
                type_=sym.type_ if sym else None,
                detail=_type_detail(sym.type_) if sym else "",
                defined_at=file_span.start,
            ))
        for name, ty in frontend.env._types.items():
            if name in {"i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64", "f32", "f64", "bool", "str", "void", "int", "float"}:
                continue
            type_decls[name] = ty

    for sym in top_level_symbols:
        root_scope.symbols.append(sym)

    if frontend.program is not None:
        for decl in frontend.program.decls:
            _index_top_level_decl(frontend.program, decl, frontend.env, root_scope, top_level_symbols)

    members_by_type: Dict[str, List[MemberMeta]] = {}
    temp_meta = DocumentMeta(
        source=source,
        frontend=frontend,
        root_scope=root_scope,
        top_level_symbols=top_level_symbols,
        members_by_type=members_by_type,
        type_decls=type_decls,
    )
    for name, ty in type_decls.items():
        members_by_type[name] = temp_meta.members_for_type(ty)

    return temp_meta


def _find_editor_build_file(source_path: str) -> Optional[str]:
    cur = Path(source_path).resolve()
    if cur.is_file():
        cur = cur.parent
    while True:
        candidate = cur / "build.mesa"
        if candidate.is_file():
            return str(candidate)
        if cur.parent == cur:
            return None
        cur = cur.parent


def _build_frontend_for_build_file(source: str, source_path: str) -> FrontendState:
    frontend = _parse_frontend_state_for_path(source_path, source_override=source)
    if frontend.program is None:
        return frontend
    try:
        load_build_plan(source_path, source_override=source)
    except BuildPlanError as exc:
        frontend.diags.error(str(exc))
    return frontend


def _build_editor_frontend(source: str, source_path: str) -> FrontendState:
    abs_path = str(Path(source_path).resolve())
    if Path(abs_path).name == "build.mesa":
        return _build_frontend_for_build_file(source, abs_path)

    build_path = _find_editor_build_file(abs_path)
    if build_path is not None:
        try:
            plan = load_build_plan(build_path)
            target = plan.default_target()
            if target is not None:
                build_dir = os.path.dirname(build_path)
                package_roots = [
                    (resolve_package_root_path(plan.packages[i].root, cwd=build_dir), plan.packages[i].name)
                    for i in target.imports
                ]
                entry_path = os.path.join(build_dir, target.entry)
                return build_frontend_state_for_path(
                    abs_path,
                    source_override=source,
                    package_roots=package_roots,
                    local_root=os.path.dirname(entry_path),
                )
        except BuildPlanError:
            pass

    return build_frontend_state_for_path(abs_path, source_override=source)


def _index_top_level_decl(
    program: Program,
    decl: object,
    env: Optional[Environment],
    root_scope: ScopeMeta,
    top_level_symbols: List[SymbolMeta],
) -> None:
    top_level_defined_at = root_scope.span.start
    prev_pkg = getattr(env, "_current_pkg", None) if env is not None else None
    decl_pkg = getattr(decl, "_pkg_path", None)
    if env is not None:
        env.set_current_pkg(decl_pkg)

    try:
        if isinstance(decl, FunDecl):
            sym = SymbolMeta(
                name=decl.name,
                kind="function",
                type_=_lookup_global_symbol_type(env, decl.name),
                detail=_type_detail(_lookup_global_symbol_type(env, decl.name)),
                span=decl.span,
                defined_at=top_level_defined_at,
            )
            root_scope.symbols.append(sym)
            top_level_symbols.append(sym)
            if decl.body and decl.body.span:
                func_scope = ScopeMeta(span=decl.body.span)
                root_scope.children.append(func_scope)
                for param in decl.params:
                    func_scope.symbols.append(_param_symbol(param, env, decl.body.span.start))
                _index_block(decl.body, func_scope, env)
                if decl.handle_block:
                    _index_handle_block(decl.handle_block, func_scope, env)
            return

        if isinstance(decl, StructDecl):
            ty = env.lookup_type(decl.name) if env else None
            sym = SymbolMeta(
                name=decl.name,
                kind="struct",
                type_=ty,
                detail=decl.name,
                span=decl.span,
                defined_at=top_level_defined_at,
            )
            root_scope.symbols.append(sym)
            top_level_symbols.append(sym)
            for method in decl.methods:
                _index_method_body(method, root_scope, env)
            return

        if isinstance(decl, InterfaceDecl):
            ty = env.lookup_type(decl.name) if env else None
            sym = SymbolMeta(
                name=decl.name,
                kind="interface",
                type_=ty,
                detail=decl.name,
                span=decl.span,
                defined_at=top_level_defined_at,
            )
            root_scope.symbols.append(sym)
            top_level_symbols.append(sym)
            return

        if isinstance(decl, UnionDecl):
            ty = env.lookup_type(decl.name) if env else None
            sym = SymbolMeta(
                name=decl.name,
                kind="union",
                type_=ty,
                detail=decl.name,
                span=decl.span,
                defined_at=top_level_defined_at,
            )
            root_scope.symbols.append(sym)
            top_level_symbols.append(sym)
            return

        if isinstance(decl, ErrorDecl):
            ty = env.lookup_type(decl.name) if env else None
            sym = SymbolMeta(
                name=decl.name,
                kind="error",
                type_=ty,
                detail=decl.name,
                span=decl.span,
                defined_at=top_level_defined_at,
            )
            root_scope.symbols.append(sym)
            top_level_symbols.append(sym)
            return

        if isinstance(decl, TypeAlias):
            ty = env.lookup_type(decl.name) if env else None
            sym = SymbolMeta(
                name=decl.name,
                kind="type",
                type_=ty,
                detail=_type_detail(ty),
                span=decl.span,
                defined_at=top_level_defined_at,
            )
            root_scope.symbols.append(sym)
            top_level_symbols.append(sym)
            return

        if isinstance(decl, DefDecl):
            for method in decl.methods:
                _index_method_body(method, root_scope, env)
            return

        if isinstance(decl, LetStmt):
            sym = SymbolMeta(
                name=decl.name,
                kind="variable",
                type_=_let_type(decl, env),
                detail=_type_detail(_let_type(decl, env)),
                span=_name_span(decl.name, decl.line, decl.col),
                defined_at=SourcePos(decl.line, decl.col),
            )
            root_scope.symbols.append(sym)
            top_level_symbols.append(sym)
            return

        if isinstance(decl, ImportDecl):
            name = decl.alias or decl.path.split(".")[-1]
            sym = SymbolMeta(
                name=name,
                kind="package",
                detail=decl.path,
                span=decl.span,
                defined_at=top_level_defined_at,
            )
            root_scope.symbols.append(sym)
            top_level_symbols.append(sym)
            return

        if isinstance(decl, FromImportDecl):
            for name, alias in decl.names:
                label = alias or name
                sym = SymbolMeta(
                    name=label,
                    kind="package",
                    detail=f"{decl.path}.{name}",
                    span=decl.span,
                    defined_at=top_level_defined_at,
                )
                root_scope.symbols.append(sym)
                top_level_symbols.append(sym)
            return

        if isinstance(decl, FromExportDecl):
            return

        if isinstance(decl, (PkgExportDecl, PkgExportAllDecl)):
            return

        if isinstance(decl, PkgDecl):
            return
    finally:
        if env is not None:
            env.set_current_pkg(prev_pkg)


def _index_method_body(method: FunDecl, root_scope: ScopeMeta, env: Optional[Environment]) -> None:
    if not method.body or not method.body.span:
        return
    scope = ScopeMeta(span=method.body.span)
    root_scope.children.append(scope)
    for param in method.params:
        scope.symbols.append(_param_symbol(param, env, method.body.span.start))
    _index_block(method.body, scope, env)
    if method.handle_block:
        _index_handle_block(method.handle_block, scope, env)


def _index_handle_block(handle: HandleBlock, parent_scope: ScopeMeta, env: Optional[Environment]) -> None:
    if not handle.body.span:
        return
    scope = ScopeMeta(span=handle.body.span)
    parent_scope.children.append(scope)
    scope.symbols.append(SymbolMeta(
        name=handle.binding,
        kind="variable",
        detail="error",
        span=_name_span(handle.binding, handle.body.span.start.line, handle.body.span.start.col),
        defined_at=handle.body.span.start,
    ))
    _index_block(handle.body, scope, env)


def _index_block(block: Block, scope: ScopeMeta, env: Optional[Environment]) -> None:
    for stmt in block.stmts:
        if isinstance(stmt, LetStmt):
            sym_ty = _let_type(stmt, env)
            scope.symbols.append(SymbolMeta(
                name=stmt.name,
                kind="variable",
                type_=sym_ty,
                detail=_type_detail(sym_ty),
                span=_name_span(stmt.name, stmt.line, stmt.col),
                defined_at=SourcePos(stmt.line, stmt.col),
            ))
            continue

        if isinstance(stmt, ForRangeStmt) and stmt.body.span:
            child = ScopeMeta(span=stmt.body.span)
            child.symbols.append(SymbolMeta(
                name=stmt.var,
                kind="variable",
                type_=T_I64,
                detail=_type_detail(T_I64),
                span=_name_span(stmt.var, stmt.body.span.start.line, stmt.body.span.start.col),
                defined_at=stmt.body.span.start,
            ))
            scope.children.append(child)
            _index_block(stmt.body, child, env)
            continue

        if isinstance(stmt, ForIterStmt) and stmt.body.span:
            child = ScopeMeta(span=stmt.body.span)
            for bound in _bind_for_pattern_meta(stmt.pattern, _iter_elem_type(stmt.iter, env), stmt.body.span.start):
                child.symbols.append(bound)
            scope.children.append(child)
            _index_block(stmt.body, child, env)
            continue

        if isinstance(stmt, WhileStmt) and stmt.body.span:
            child = ScopeMeta(span=stmt.body.span)
            scope.children.append(child)
            _index_block(stmt.body, child, env)
            continue

        if isinstance(stmt, DeferStmt) and stmt.body.span:
            child = ScopeMeta(span=stmt.body.span)
            scope.children.append(child)
            _index_block(stmt.body, child, env)
            continue

        if isinstance(stmt, ExprStmt):
            _index_expr(stmt.expr, scope, env)
            continue

        if isinstance(stmt, AssignStmt):
            _index_expr(stmt.value, scope, env)
            continue

        if isinstance(stmt, ReturnStmt) and stmt.value is not None:
            _index_expr(stmt.value, scope, env)
            continue

        if isinstance(stmt, BreakStmt) and stmt.value is not None:
            _index_expr(stmt.value, scope, env)
            continue

        if isinstance(stmt, ContinueStmt):
            continue

    if block.tail is not None:
        _index_expr(block.tail, scope, env)


def _index_expr(expr: Expr, scope: ScopeMeta, env: Optional[Environment]) -> None:
    if isinstance(expr, IfExpr):
        if expr.then_block.span:
            then_scope = ScopeMeta(span=expr.then_block.span)
            scope.children.append(then_scope)
            _index_block(expr.then_block, then_scope, env)
        if expr.else_block and expr.else_block.span:
            else_scope = ScopeMeta(span=expr.else_block.span)
            scope.children.append(else_scope)
            _index_block(expr.else_block, else_scope, env)
        return

    if isinstance(expr, IfUnwrap):
        inner_ty = _unwrap_optional_type(_expr_type(expr.expr))
        if expr.then_block.span:
            then_scope = ScopeMeta(span=expr.then_block.span)
            then_scope.symbols.append(SymbolMeta(
                name=expr.binding,
                kind="variable",
                type_=TPointer(inner_ty) if expr.is_ref and inner_ty is not None else inner_ty,
                detail=_type_detail(TPointer(inner_ty) if expr.is_ref and inner_ty is not None else inner_ty),
                span=_name_span(expr.binding, expr.then_block.span.start.line, expr.then_block.span.start.col),
                defined_at=expr.then_block.span.start,
            ))
            scope.children.append(then_scope)
            _index_block(expr.then_block, then_scope, env)
        if expr.else_block and expr.else_block.span:
            else_scope = ScopeMeta(span=expr.else_block.span)
            scope.children.append(else_scope)
            _index_block(expr.else_block, else_scope, env)
        return

    if isinstance(expr, WhileUnwrap) and expr.body.span:
        inner_ty = _unwrap_optional_type(_expr_type(expr.expr))
        body_scope = ScopeMeta(span=expr.body.span)
        body_scope.symbols.append(SymbolMeta(
            name=expr.binding,
            kind="variable",
            type_=TPointer(inner_ty) if expr.is_ref and inner_ty is not None else inner_ty,
            detail=_type_detail(TPointer(inner_ty) if expr.is_ref and inner_ty is not None else inner_ty),
            span=_name_span(expr.binding, expr.body.span.start.line, expr.body.span.start.col),
            defined_at=expr.body.span.start,
        ))
        scope.children.append(body_scope)
        _index_block(expr.body, body_scope, env)
        return

    if isinstance(expr, MatchExpr):
        match_ty = getattr(expr, "_checked_type", None)
        for arm in expr.arms:
            if arm.body.span:
                arm_scope = ScopeMeta(span=arm.body.span)
                for sym in _bind_match_pattern_meta(arm.pattern, match_ty, arm.body.span.start):
                    arm_scope.symbols.append(sym)
                scope.children.append(arm_scope)
                _index_block(arm.body, arm_scope, env)
        return

    if isinstance(expr, WithExpr):
        if expr.body.span:
            body_scope = ScopeMeta(span=expr.body.span)
            scope.children.append(body_scope)
            _index_block(expr.body, body_scope, env)
        if expr.handle and expr.handle.body.span:
            handle_scope = ScopeMeta(span=expr.handle.body.span)
            handle_scope.symbols.append(SymbolMeta(
                name=expr.handle.binding,
                kind="variable",
                detail="error",
                span=_name_span(expr.handle.binding, expr.handle.body.span.start.line, expr.handle.body.span.start.col),
                defined_at=expr.handle.body.span.start,
            ))
            scope.children.append(handle_scope)
            _index_block(expr.handle.body, handle_scope, env)
        return

    if isinstance(expr, BlockExpr) and expr.block.span:
        child = ScopeMeta(span=expr.block.span)
        scope.children.append(child)
        _index_block(expr.block, child, env)


def _iter_elem_type(iter_expr: Expr, env: Optional[Environment]) -> Optional[Type]:
    ty = _expr_type(iter_expr)
    if ty is None:
        return None
    ty = _unwrap_member_type(ty)
    if isinstance(ty, (TVec, TSlice, TArray)):
        return ty.inner
    if isinstance(ty, TMat):
        return ty.inner
    if env is not None:
        type_name = getattr(ty, "name", None)
        if type_name:
            method = env.impls.find_method(type_name, "next")
            if method and isinstance(method.ret, TOptional):
                return method.ret.inner
    return None


def _bind_for_pattern_meta(pattern: ForPattern, elem_ty: Optional[Type], defined_at: SourcePos) -> List[SymbolMeta]:
    if elem_ty is None:
        return []
    if isinstance(pattern, PatIdent):
        return [_symbol_at(pattern.name, "variable", elem_ty, defined_at)]
    if isinstance(pattern, PatRef):
        return [_symbol_at(pattern.name, "variable", TPointer(elem_ty), defined_at)]
    if isinstance(pattern, PatTuple) and isinstance(elem_ty, TTuple):
        items: List[SymbolMeta] = []
        for name, (_, field_ty) in zip(pattern.names, elem_ty.fields):
            items.append(_symbol_at(name, "variable", field_ty, defined_at))
        return items
    return []


def _bind_match_pattern_meta(pattern: MatchPattern, val_ty: Optional[Type], defined_at: SourcePos) -> List[SymbolMeta]:
    if val_ty is None:
        return []
    if isinstance(pattern, PatWildcard):
        return []
    if isinstance(pattern, PatIdent) and pattern.name != "_":
        return [_symbol_at(pattern.name, "variable", val_ty, defined_at)]
    if isinstance(pattern, PatVariant):
        if isinstance(val_ty, TUnion):
            payload = val_ty.variant_payload(pattern.name)
            names = ([pattern.binding] if pattern.binding else []) + list(pattern.extra_bindings or [])
            if payload is None:
                return []
            if isinstance(payload, TTuple) and names:
                return [
                    _symbol_at(name, "variable", field_ty, defined_at)
                    for name, (_, field_ty) in zip(names, payload.fields)
                ]
            if names:
                return [_symbol_at(name, "variable", payload, defined_at) for name in names]
        if isinstance(val_ty, TErrorSet):
            return []
    return []


def _param_symbol(param: Param, env: Optional[Environment], defined_at: SourcePos) -> SymbolMeta:
    ty = None
    if env is not None:
        ty = lower_type(param.type_, env)
    return _symbol_at(param.name, "parameter", ty, defined_at)


def _symbol_at(name: str, kind: str, ty: Optional[Type], defined_at: SourcePos) -> SymbolMeta:
    return SymbolMeta(
        name=name,
        kind=kind,
        type_=ty,
        detail=_type_detail(ty),
        span=_name_span(name, defined_at.line, defined_at.col),
        defined_at=defined_at,
    )


def _lookup_global_symbol_type(env: Optional[Environment], name: str) -> Optional[Type]:
    if env is None:
        return None
    sym = env.lookup(name)
    return sym.type_ if sym else None


def _let_type(stmt: LetStmt, env: Optional[Environment]) -> Optional[Type]:
    if env is not None and stmt.type_ is not None:
        return lower_type(stmt.type_, env, stmt.line, stmt.col)
    if stmt.init is not None:
        return _expr_type(stmt.init)
    return None


def _expr_type(expr: Optional[Expr]) -> Optional[Type]:
    if expr is None:
        return None
    resolved = getattr(expr, "_resolved_type", None)
    if resolved is not None:
        return resolved
    if hasattr(expr, "value") and isinstance(getattr(expr, "value", None), int):
        return T_I64
    if hasattr(expr, "value") and isinstance(getattr(expr, "value", None), float):
        return T_F64
    if isinstance(getattr(expr, "raw", None), str):
        return T_STR
    if expr.__class__.__name__ == "BoolLit":
        return T_BOOL
    return None


def _field_type_from_type(ty: Type, field: str, env: Optional[Environment]) -> Optional[Type]:
    ty = _unwrap_member_type(ty)
    if ty is None:
        return None
    if isinstance(ty, TStruct):
        if field in ty.fields:
            return ty.fields[field]
        if field in ty.methods:
            return ty.methods[field]
        if env is not None:
            for iface_name in env.impls.all_interfaces_for(ty.name):
                methods = env.impls._impls.get((ty.name, iface_name), {})
                if field in methods:
                    return methods[field]
    if isinstance(ty, TInterface):
        return ty.methods.get(field)
    if isinstance(ty, TAnyInterface):
        return ty.iface.methods.get(field)
    if isinstance(ty, TDynInterface):
        return ty.iface.methods.get(field)
    if isinstance(ty, TTuple):
        return ty.field_type(field)
    if isinstance(ty, TVec):
        if field in {"len", "cap"}:
            return T_I64
    if isinstance(ty, TSlice):
        if field == "len":
            return T_I64
    if isinstance(ty, TString):
        if field == "len":
            return T_I64
        if field == "data":
            return TPointer(TInt(8, False))
    return None


def _unwrap_member_type(ty: Optional[Type]) -> Optional[Type]:
    while isinstance(ty, (TPointer, TOptional)):
        ty = ty.inner
    return ty


def _unwrap_optional_type(ty: Optional[Type]) -> Optional[Type]:
    if isinstance(ty, TOptional):
        return ty.inner
    return ty


def _type_detail(ty: Optional[Type]) -> str:
    return repr(ty) if ty is not None else ""


def _name_span(name: str, line: int, col: int) -> SourceSpan:
    return SourceSpan(start=SourcePos(line, col), end=SourcePos(line, col + len(name)))


def _full_span(source: str) -> SourceSpan:
    lines = source.splitlines()
    if not lines:
        return SourceSpan(start=SourcePos(1, 1), end=SourcePos(1, 1))
    return SourceSpan(
        start=SourcePos(1, 1),
        end=SourcePos(len(lines), len(lines[-1]) + 1),
    )


def _full_span_from_program(program: Program) -> SourceSpan:
    if program.decls:
        first = getattr(program.decls[0], "span", None)
        last = getattr(program.decls[-1], "span", None)
        if first and last:
            return SourceSpan(start=first.start, end=last.end)
    return SourceSpan(start=SourcePos(1, 1), end=SourcePos(1, 1))


def _span_contains(span: SourceSpan, pos: SourcePos) -> bool:
    return _pos_le(span.start, pos) and _pos_lt(pos, span.end)


def _pos_lt(a: SourcePos, b: SourcePos) -> bool:
    return (a.line, a.col) < (b.line, b.col)


def _pos_le(a: SourcePos, b: SourcePos) -> bool:
    return (a.line, a.col) <= (b.line, b.col)


@dataclass(frozen=True)
class _CompletionContext:
    kind: str
    prefix: str
    receiver_text: str = ""


def _completion_context(source: str, pos: SourcePos) -> _CompletionContext:
    offset = _offset_for_pos(source, pos)
    fragment_start = offset
    while fragment_start > 0 and (source[fragment_start - 1].isalnum() or source[fragment_start - 1] == "_"):
        fragment_start -= 1
    prefix = source[fragment_start:offset]

    if fragment_start >= 2 and source[fragment_start - 2:fragment_start] == "?.":
        receiver_end = fragment_start - 2
        return _CompletionContext(
            kind="member",
            prefix=prefix,
            receiver_text=_extract_receiver_expr(source, receiver_end),
        )
    if fragment_start >= 1 and source[fragment_start - 1] == ".":
        receiver_end = fragment_start - 1
        return _CompletionContext(
            kind="member",
            prefix=prefix,
            receiver_text=_extract_receiver_expr(source, receiver_end),
        )
    return _CompletionContext(kind="global", prefix=prefix)


def _offset_for_pos(source: str, pos: SourcePos) -> int:
    lines = source.splitlines(keepends=True)
    line_index = max(0, pos.line - 1)
    if line_index >= len(lines):
        return len(source)
    return sum(len(line) for line in lines[:line_index]) + min(pos.col - 1, len(lines[line_index]))


def _extract_receiver_expr(source: str, receiver_end: int) -> str:
    depth_paren = 0
    depth_bracket = 0
    depth_brace = 0
    i = receiver_end - 1
    while i >= 0:
        ch = source[i]
        if ch == ")":
            depth_paren += 1
        elif ch == "(":
            if depth_paren == 0:
                break
            depth_paren -= 1
        elif ch == "]":
            depth_bracket += 1
        elif ch == "[":
            if depth_bracket == 0:
                break
            depth_bracket -= 1
        elif ch == "}":
            depth_brace += 1
        elif ch == "{":
            if depth_brace == 0:
                break
            depth_brace -= 1
        elif depth_paren == depth_bracket == depth_brace == 0 and ch in "\n;,=:+-*/%^!&|<>":
            break
        i -= 1
    return source[i + 1:receiver_end].strip()


def _sorted_candidates(items: Iterable[CompletionCandidate], prefix: str) -> List[CompletionCandidate]:
    prefix_lower = prefix.lower()
    return sorted(
        items,
        key=lambda item: (
            0 if prefix_lower and item.label.lower().startswith(prefix_lower) else 1,
            0 if item.kind in {"variable", "field", "method"} else 1,
            item.label,
        ),
    )


def _visible_symbol_for_token(meta: DocumentMeta, tok: Token) -> Optional[SymbolMeta]:
    pos = SourcePos(tok.line, tok.col + len(tok.lexeme))
    for sym in reversed(meta.visible_symbols_at(pos)):
        if sym.name == tok.lexeme:
            return sym
    return None


def _collect_type_parameter_names(program: Optional[Program]) -> set[str]:
    names: set[str] = set()
    if program is None:
        return names
    for decl in program.decls:
        if isinstance(decl, StructDecl):
            names.update(decl.params)
        elif isinstance(decl, UnionDecl):
            names.update(decl.params)
        elif isinstance(decl, InterfaceDecl):
            names.update(decl.params)
        elif isinstance(decl, FunDecl):
            names.update(getattr(decl, "_type_params", []))
    names.add("Self")
    return names


def _looks_like_type_usage(tokens: Sequence[Token], index: int, type_names: set[str]) -> bool:
    tok = tokens[index]
    if tok.kind != TK.IDENT:
        return False
    if tok.lexeme == "vec":
        next_tok = _peek_seq(tokens, index + 1)
        return next_tok is not None and next_tok.kind == TK.LBRACKET

    prev_tok = _peek_seq(tokens, index - 1)
    if prev_tok is None:
        return False

    if prev_tok.kind in {TK.COLON, TK.QUESTION, TK.STAR, TK.BANG, TK.KW_ANY, TK.KW_FOR, TK.LBRACKET}:
        return True

    if prev_tok.kind == TK.COMMA:
        return _inside_type_clause(tokens, index - 1)

    if prev_tok.kind == TK.RPAREN:
        return _follows_fun_signature(tokens, index - 1)

    return False


def _looks_like_generic_param_decl(tokens: Sequence[Token], index: int) -> bool:
    tok = _peek_seq(tokens, index)
    prev_tok = _peek_seq(tokens, index - 1)
    next_tok = _peek_seq(tokens, index + 1)
    if tok is None or tok.kind != TK.IDENT:
        return False
    if prev_tok is None or next_tok is None:
        return False
    return prev_tok.kind in {TK.LBRACKET, TK.COMMA} and next_tok.kind in {TK.COMMA, TK.RBRACKET}


def _looks_like_param_decl(tokens: Sequence[Token], index: int) -> bool:
    tok = _peek_seq(tokens, index)
    next_tok = _peek_seq(tokens, index + 1)
    if tok is None or tok.kind != TK.IDENT or next_tok is None or next_tok.kind != TK.COLON:
        return False
    return _inside_fun_param_list(tokens, index)


def _inside_fun_param_list(tokens: Sequence[Token], index: int) -> bool:
    depth = 0
    i = index
    while i >= 0:
        tok = tokens[i]
        if tok.kind == TK.RPAREN:
            depth += 1
        elif tok.kind == TK.LPAREN:
            if depth == 0:
                j = i - 1
                if j >= 0 and tokens[j].kind == TK.RBRACKET:
                    bracket_depth = 1
                    j -= 1
                    while j >= 0 and bracket_depth > 0:
                        if tokens[j].kind == TK.RBRACKET:
                            bracket_depth += 1
                        elif tokens[j].kind == TK.LBRACKET:
                            bracket_depth -= 1
                        j -= 1
                if j >= 0 and tokens[j].kind == TK.IDENT:
                    j -= 1
                return j >= 0 and tokens[j].kind == TK.KW_FUN
            depth -= 1
        i -= 1
    return False


def _looks_like_pkg_path_token(tokens: Sequence[Token], index: int) -> bool:
    tok = _peek_seq(tokens, index)
    if tok is None or tok.kind != TK.IDENT:
        return False

    i = index - 1
    while i >= 0 and tokens[i].kind == TK.DOT:
        i -= 1
        if i < 0 or tokens[i].kind != TK.IDENT:
            break
        i -= 1
    start_tok = _peek_seq(tokens, i + 1)
    prev_tok = _peek_seq(tokens, i)
    if start_tok is tok and prev_tok is not None and prev_tok.kind in {TK.KW_PKG, TK.KW_IMPORT, TK.KW_FROM}:
        return True
    if prev_tok is not None and prev_tok.kind in {TK.KW_PKG, TK.KW_IMPORT, TK.KW_FROM}:
        return True

    if _peek_seq(tokens, index - 1) and _peek_seq(tokens, index - 1).kind == TK.DOT:
        j = index - 2
        while j >= 0 and tokens[j].kind == TK.IDENT:
            if j == 0 or tokens[j - 1].kind != TK.DOT:
                break
            j -= 2
        before = _peek_seq(tokens, j - 1)
        return before is not None and before.kind in {TK.KW_PKG, TK.KW_IMPORT, TK.KW_FROM}
    return False


def _inside_type_clause(tokens: Sequence[Token], index: int) -> bool:
    depth = 0
    i = index
    while i >= 0:
        tok = tokens[i]
        if tok.kind == TK.RBRACKET:
            depth += 1
        elif tok.kind == TK.LBRACKET:
            if depth == 0:
                return True
            depth -= 1
        elif depth == 0 and tok.kind in {TK.COLON, TK.QUESTION, TK.STAR, TK.BANG, TK.KW_ANY}:
            return True
        elif depth == 0 and tok.kind in {TK.EQ, TK.SEMI, TK.NEWLINE, TK.LBRACE}:
            return False
        i -= 1
    return False


def _follows_fun_signature(tokens: Sequence[Token], rp_index: int) -> bool:
    depth = 0
    i = rp_index
    while i >= 0:
        tok = tokens[i]
        if tok.kind == TK.RPAREN:
            depth += 1
        elif tok.kind == TK.LPAREN:
            depth -= 1
            if depth == 0:
                j = i - 1
                if j >= 0 and tokens[j].kind == TK.RBRACKET:
                    bracket_depth = 1
                    j -= 1
                    while j >= 0 and bracket_depth > 0:
                        if tokens[j].kind == TK.RBRACKET:
                            bracket_depth += 1
                        elif tokens[j].kind == TK.LBRACKET:
                            bracket_depth -= 1
                        j -= 1
                if j >= 0 and tokens[j].kind == TK.IDENT:
                    j -= 1
                return j >= 0 and tokens[j].kind == TK.KW_FUN
        i -= 1
    return False


def _tokens_in_span(tokens: Sequence[Token], span: Optional[SourceSpan]) -> List[Token]:
    if span is None:
        return []
    return [tok for tok in tokens if _token_in_span(tok, span)]


def _token_in_span(tok: Token, span: SourceSpan) -> bool:
    pos = SourcePos(tok.line, tok.col)
    return _pos_le(span.start, pos) and _pos_lt(pos, span.end)


def _struct_field_tokens(tokens: Sequence[Token], decl: StructDecl) -> List[Token]:
    result: List[Token] = []
    body_started = False
    brace_depth = 0
    paren_depth = 0
    for index, tok in enumerate(_tokens_in_span(tokens, decl.span)):
        if tok.kind == TK.LBRACE:
            brace_depth += 1
            body_started = True
            continue
        if tok.kind == TK.RBRACE:
            brace_depth -= 1
            continue
        if tok.kind == TK.LPAREN:
            paren_depth += 1
            continue
        if tok.kind == TK.RPAREN:
            paren_depth -= 1
            continue
        if not body_started or brace_depth != 1 or paren_depth != 0:
            continue
        next_tok = _peek_seq(tokens=_tokens_in_span(tokens, decl.span), index=index + 1)
        if tok.kind == TK.IDENT and next_tok and next_tok.kind == TK.COLON:
            prev_tok = _peek_seq(tokens=_tokens_in_span(tokens, decl.span), index=index - 1)
            if prev_tok and prev_tok.kind == TK.KW_FUN:
                continue
            result.append(tok)
    return result


def _tuple_literal_field_tokens(tokens: Sequence[Token]) -> List[Token]:
    result: List[Token] = []
    brace_depth = 0
    paren_depth = 0
    bracket_depth = 0
    in_tuple_literal = False

    for index, tok in enumerate(tokens):
        prev_tok = _peek_seq(tokens, index - 1)
        next_tok = _peek_seq(tokens, index + 1)

        if tok.kind == TK.LBRACKET:
            bracket_depth += 1
        elif tok.kind == TK.RBRACKET:
            bracket_depth -= 1

        if tok.kind == TK.LPAREN:
            paren_depth += 1
        elif tok.kind == TK.RPAREN:
            paren_depth -= 1

        if tok.kind == TK.LBRACE:
            if prev_tok is not None and prev_tok.kind == TK.DOT:
                in_tuple_literal = True
                brace_depth = 1
                continue
            if in_tuple_literal:
                brace_depth += 1
            continue

        if tok.kind == TK.RBRACE and in_tuple_literal:
            brace_depth -= 1
            if brace_depth <= 0:
                in_tuple_literal = False
                brace_depth = 0
            continue

        if not in_tuple_literal or brace_depth != 1 or paren_depth != 0 or bracket_depth != 0:
            continue

        if tok.kind == TK.IDENT and next_tok is not None and next_tok.kind == TK.COLON:
            result.append(tok)

    return result


def _with_cleanup_tokens(tokens: Sequence[Token]) -> List[Token]:
    result: List[Token] = []
    in_with = False
    brace_depth = 0
    paren_depth = 0
    bracket_depth = 0

    for index, tok in enumerate(tokens):
        if tok.kind == TK.KW_WITH:
            in_with = True
            brace_depth = 0
            paren_depth = 0
            bracket_depth = 0
            continue

        if not in_with:
            continue

        if tok.kind == TK.LPAREN:
            paren_depth += 1
        elif tok.kind == TK.RPAREN and paren_depth > 0:
            paren_depth -= 1
        elif tok.kind == TK.LBRACKET:
            bracket_depth += 1
        elif tok.kind == TK.RBRACKET and bracket_depth > 0:
            bracket_depth -= 1
        elif tok.kind == TK.LBRACE:
            if brace_depth == 0:
                in_with = False
            else:
                brace_depth += 1
            continue
        elif tok.kind == TK.RBRACE and brace_depth > 0:
            brace_depth -= 1

        if paren_depth != 0 or bracket_depth != 0:
            continue

        next_tok = _peek_seq(tokens, index + 1)
        next2_tok = _peek_seq(tokens, index + 2)
        if tok.kind == TK.COLON and next_tok is not None and next_tok.kind == TK.DOT and next2_tok is not None and next2_tok.kind == TK.IDENT:
            result.append(next2_tok)

    return result


def _variant_decl_tokens(tokens: Sequence[Token], decl: UnionDecl | ErrorDecl) -> List[Token]:
    result: List[Token] = []
    body_started = False
    brace_depth = 0
    paren_depth = 0
    scoped = _tokens_in_span(tokens, decl.span)
    for index, tok in enumerate(scoped):
        if tok.kind == TK.LBRACE:
            brace_depth += 1
            body_started = True
            continue
        if tok.kind == TK.RBRACE:
            brace_depth -= 1
            continue
        if tok.kind == TK.LPAREN:
            paren_depth += 1
        elif tok.kind == TK.RPAREN:
            paren_depth -= 1
        if not body_started or brace_depth != 1 or paren_depth != 0:
            continue
        next_tok = _peek_seq(scoped, index + 1)
        if tok.kind == TK.IDENT and next_tok and next_tok.kind in (TK.LPAREN, TK.COMMA, TK.RBRACE):
            result.append(tok)
    return result


def _def_decl_type_tokens(tokens: Sequence[Token], decl: DefDecl) -> List[Token]:
    scoped = _tokens_in_span(tokens, decl.span)
    wanted = set(decl.interfaces)
    wanted.add(decl.for_type)
    result: List[Token] = []
    seen_def = False
    for tok in scoped:
        if tok.kind == TK.KW_DEF:
            seen_def = True
            continue
        if not seen_def:
            continue
        if tok.kind == TK.LBRACE:
            break
        if tok.kind == TK.IDENT and tok.lexeme in wanted:
            result.append(tok)
    return result


def _pkg_decl_path_tokens(tokens: Sequence[Token], decl: PkgDecl) -> List[Token]:
    scoped = _tokens_in_span(tokens, decl.span)
    return [tok for tok in scoped if tok.kind == TK.IDENT and tok.lexeme in decl.path.split(".")]


def _import_path_tokens(tokens: Sequence[Token], decl: ImportDecl) -> List[Token]:
    scoped = _tokens_in_span(tokens, decl.span)
    path_parts = decl.path.split(".")
    seen_import = False
    result: List[Token] = []
    for tok in scoped:
        if tok.kind == TK.KW_IMPORT:
            seen_import = True
            continue
        if tok.kind == TK.KW_AS:
            break
        if seen_import and tok.kind == TK.IDENT and tok.lexeme in path_parts:
            result.append(tok)
    return result


def _from_import_path_tokens(tokens: Sequence[Token], decl: FromImportDecl) -> List[Token]:
    scoped = _tokens_in_span(tokens, decl.span)
    path_parts = decl.path.split(".")
    seen_from = False
    result: List[Token] = []
    for tok in scoped:
        if tok.kind == TK.KW_FROM:
            seen_from = True
            continue
        if tok.kind == TK.KW_IMPORT:
            break
        if seen_from and tok.kind == TK.IDENT and tok.lexeme in path_parts:
            result.append(tok)
    return result


def _find_ident_token_in_span(tokens: Sequence[Token], span: Optional[SourceSpan], name: str) -> Optional[Token]:
    for tok in _tokens_in_span(tokens, span):
        if tok.kind == TK.IDENT and tok.lexeme == name:
            return tok
    return None


def _peek_seq(tokens: Sequence[Token], index: int) -> Optional[Token]:
    if 0 <= index < len(tokens):
        return tokens[index]
    return None
