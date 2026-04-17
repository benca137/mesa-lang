"""
Mesa recursive-descent parser.

Precedence (lowest → highest):
    or
    and
    == != .== .!=
    < > <= >= .< .> .<= .>=
    + - .+ .-
    * / % .* ./ .%
    ^ .^                    (right associative)
    unary  (- ! * @)
    postfix (call . ?. index)
    primary
"""
from __future__ import annotations
from typing import List, Optional, Union
from src.tokenizer import Token, TK, Tokenizer
from src.ast import *
from src.ast import TyTuple


class ParseError(Exception):
    def __init__(self, msg: str, tok: Token):
        super().__init__(
            f"[{tok.line}:{tok.col}] {msg} "
            f"(got {tok.kind.name} {tok.lexeme!r})"
        )
        self.token = tok


# ── Precedence table ─────────────────────────────────────────
_PREC: dict[TK, int] = {
    TK.KW_OR:       1,
    TK.KW_AND:      2,
    TK.EQ_EQ:       3, TK.BANG_EQ:    3,
    TK.DOT_EQ_EQ:   3, TK.DOT_BANG_EQ: 3,
    TK.LT:          4, TK.GT:         4,
    TK.LT_EQ:       4, TK.GT_EQ:      4,
    TK.DOT_LT:      4, TK.DOT_GT:     4,
    TK.DOT_LT_EQ:   4, TK.DOT_GT_EQ:  4,
    TK.PLUS:        5, TK.MINUS:      5,
    TK.DOT_PLUS:    5, TK.DOT_MINUS:  5,
    TK.STAR:        6, TK.SLASH:      6, TK.PERCENT:    6,
    TK.DOT_STAR:    6, TK.DOT_SLASH:  6, TK.DOT_PERCENT: 6,
    TK.CARET:       7, TK.DOT_CARET:  7,   # right-associative
}

_BINOP: dict[TK, str] = {
    TK.PLUS: "+",      TK.MINUS: "-",
    TK.STAR: "*",      TK.SLASH: "/",    TK.PERCENT: "%",
    TK.CARET: "^",
    TK.DOT_PLUS: ".+", TK.DOT_MINUS: ".-",
    TK.DOT_STAR: ".*", TK.DOT_SLASH: "./", TK.DOT_PERCENT: ".%",
    TK.DOT_CARET: ".^",
    TK.EQ_EQ: "==",    TK.BANG_EQ: "!=",
    TK.LT: "<",        TK.GT: ">",
    TK.LT_EQ: "<=",    TK.GT_EQ: ">=",
    TK.DOT_EQ_EQ: ".==",  TK.DOT_BANG_EQ: ".!=",
    TK.DOT_LT: ".<",   TK.DOT_GT: ".>",
    TK.DOT_LT_EQ: ".<=", TK.DOT_GT_EQ: ".>=",
    TK.KW_AND: "and",  TK.KW_OR: "or",
    TK.PLUS_MINUS: "+-",
    TK.DOT_PLUS_MINUS: ".+-",
}

_ASSIGN_OPS: dict[TK, str] = {
    TK.EQ: "=",            TK.PLUS_EQ: "+=",
    TK.MINUS_EQ: "-=",     TK.STAR_EQ: "*=",
    TK.SLASH_EQ: "/=",     TK.PERCENT_EQ: "%=",
    TK.CARET_EQ: "^=",     TK.QUESTION_EQ: "?=",
    TK.DOT_PLUS_EQ: ".+=", TK.DOT_MINUS_EQ: ".-=",
    TK.DOT_STAR_EQ: ".*=", TK.DOT_SLASH_EQ: "./=",
}

_RIGHT_ASSOC = {TK.CARET, TK.DOT_CARET}

_PRIM_TYPES = {
    TK.TY_I8: "i8",   TK.TY_I16: "i16",
    TK.TY_I32: "i32", TK.TY_I64: "i64",
    TK.TY_U8: "u8",   TK.TY_U16: "u16",
    TK.TY_U32: "u32", TK.TY_U64: "u64",
    TK.TY_F32: "f32", TK.TY_F64: "f64",
    TK.TY_BOOL: "bool",
    TK.TY_STR: "str",
    TK.TY_VOID: "void",
}


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos    = 0

    # ── navigation ───────────────────────────────────────────

    def _peek(self, offset: int = 0) -> Token:
        i = self.pos + offset
        return self.tokens[i] if i < len(self.tokens) else self.tokens[-1]

    @property
    def _cur(self) -> Token:
        return self._peek(0)

    @property
    def _prev(self) -> Token:
        if self.pos <= 0:
            return self.tokens[0]
        return self.tokens[self.pos - 1]

    def _advance(self) -> Token:
        t = self._cur
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return t

    def _check(self, *kinds: TK) -> bool:
        return self._cur.kind in kinds

    def _eat(self, *kinds: TK) -> Optional[Token]:
        if self._cur.kind in kinds:
            return self._advance()
        return None

    def _eat_newlines(self):
        """Consume NEWLINE and SEMI tokens (both are statement separators)."""
        while self._cur.kind in (TK.NEWLINE, TK.SEMI):
            self._advance()

    def _expect_newline_or(self, *extra: TK):
        """Expect a NEWLINE, SEMI, or one of the extra tokens, eating it."""
        if self._cur.kind in (TK.NEWLINE, TK.SEMI):
            self._advance(); return
        if self._cur.kind in extra:
            self._advance(); return
        # Neither — just continue (lenient)

    def _expect(self, kind: TK) -> Token:
        if self._cur.kind == kind:
            return self._advance()
        raise ParseError(f"expected {kind.name}", self._cur)

    def _expect_ident(self) -> str:
        return self._expect(TK.IDENT).lexeme

    def _expect_ident_tok(self):
        """Return the full Token for an identifier (for location tracking)."""
        return self._expect(TK.IDENT)

    def _token_end_pos(self, tok: Token) -> SourcePos:
        line = tok.line
        col = tok.col
        lexeme = tok.lexeme or ""
        parts = lexeme.split("\n")
        if len(parts) == 1:
            return SourcePos(line=line, col=col + len(lexeme))
        return SourcePos(line=line + len(parts) - 1, col=len(parts[-1]) + 1)

    def _span_from_tokens(self, start_tok: Token, end_tok: Token) -> SourceSpan:
        return SourceSpan(
            start=SourcePos(line=start_tok.line, col=start_tok.col),
            end=self._token_end_pos(end_tok),
        )

    def _span_from_token_and_end(self, start_tok: Token, end: SourcePos) -> SourceSpan:
        return SourceSpan(start=SourcePos(line=start_tok.line, col=start_tok.col), end=end)

    def _span_from_exprs(self, start_expr: Expr, end_expr: Expr) -> Optional[SourceSpan]:
        start = self._expr_span(start_expr)
        end = self._expr_span(end_expr)
        if start is None or end is None:
            return None
        return SourceSpan(start=start.start, end=end.end)

    def _type_span(self, ty: TypeExpr) -> Optional[SourceSpan]:
        return getattr(ty, "span", None)

    def _expr_span(self, expr: Expr) -> Optional[SourceSpan]:
        span = getattr(expr, "span", None)
        if span is not None:
            return span
        if isinstance(expr, BlockExpr):
            return expr.block.span
        line = getattr(expr, "line", 0)
        col = getattr(expr, "col", 0)
        if line and col:
            length = 1
            if isinstance(expr, Ident):
                length = max(len(expr.name), 1)
            elif isinstance(expr, VariantLit):
                length = max(len(expr.name) + 1, 1)
            elif isinstance(expr, BoolLit):
                length = 4 if expr.value else 5
            elif isinstance(expr, IntLit):
                length = max(len(str(expr.value)), 1)
            elif isinstance(expr, FloatLit):
                length = max(len(str(expr.value)), 1)
            elif isinstance(expr, StringLit):
                length = max(len(expr.raw) + 2, 1)
            return SourceSpan(
                start=SourcePos(line=line, col=col),
                end=SourcePos(line=line, col=col + length),
            )
        return None

    # ── attributes ──────────────────────────────────────────

    def _parse_attrs(self) -> List[Attribute]:
        attrs = []
        while self._check(TK.HASH_LBRACKET):
            self._advance()   # eat #[
            name = self._expect_ident()
            value = None
            if self._eat(TK.EQ):
                value = self._parse_expr()
            elif self._eat(TK.LPAREN):
                value = self._parse_expr()
                self._expect(TK.RPAREN)
            self._expect(TK.RBRACKET)
            attrs.append(Attribute(name=name, value=value))
        return attrs

    # ── visibility ───────────────────────────────────────────

    def _parse_vis(self) -> Visibility:
        match self._cur.kind:
            case TK.KW_PUB:
                self._advance()
                return Visibility.PUB
            case TK.KW_EXPORT:
                self._advance()
                return Visibility.EXPORT
            case _:
                return Visibility.PRIVATE

    # ══════════════════════════════════════════════════════════
    # Entry point
    # ══════════════════════════════════════════════════════════

    def parse(self) -> Program:
        pkg     = None
        imports = []
        decls   = []

        self._eat_newlines()

        # optional pkg declaration — must come first
        if self._check(TK.KW_PKG):
            pkg = self._parse_pkg_decl()
            self._eat_newlines()

        # top-level declarations/imports
        while not self._check(TK.EOF):
            self._eat_newlines()
            if self._check(TK.EOF): break
            decls.append(self._parse_decl())
            self._eat_newlines()

        return Program(pkg=pkg, imports=imports, decls=decls)

    def _peek_after_dotted_name(self, start_offset: int = 1) -> TK:
        """Return the token kind after a dotted-name sequence."""
        i = start_offset
        if self._peek(i).kind != TK.IDENT:
            return self._peek(i).kind
        i += 1
        while self._peek(i).kind == TK.DOT and self._peek(i + 1).kind == TK.IDENT:
            i += 2
        return self._peek(i).kind

    # ══════════════════════════════════════════════════════════
    # Package and imports
    # ══════════════════════════════════════════════════════════

    def _parse_pkg_decl(self) -> PkgDecl:
        start_tok = self._expect(TK.KW_PKG)
        path = self._parse_dotted_name()
        end_tok = self._prev
        self._eat_newlines()
        return PkgDecl(path=path, span=self._span_from_tokens(start_tok, end_tok))

    def _parse_dotted_name(self) -> str:
        parts = [self._expect_ident()]
        while self._eat(TK.DOT):
            parts.append(self._expect_ident())
        return ".".join(parts)

    def _parse_string_path(self) -> str:
        tok = self._expect(TK.STRING)
        return tok.lexeme[1:-1]

    def _parse_import(self) -> Union[ImportDecl, FromImportDecl]:
        match self._cur.kind:
            case TK.KW_IMPORT:
                start_tok = self._advance()
                path = self._parse_dotted_name()
                alias = self._expect_ident() if self._eat(TK.KW_AS) else None
                end_tok = self._prev
                self._eat_newlines()
                return ImportDecl(path=path, alias=alias,
                                  span=self._span_from_tokens(start_tok, end_tok))
            case TK.KW_FROM:
                start_tok = self._advance()
                path = self._parse_dotted_name()
                self._expect(TK.KW_IMPORT)
                names = []
                while True:
                    name = self._expect_ident()
                    alias = self._expect_ident() if self._eat(TK.KW_AS) else None
                    names.append((name, alias))
                    if not self._eat(TK.COMMA):
                        break
                end_tok = self._prev
                self._eat_newlines()
                return FromImportDecl(path=path, names=names,
                                      span=self._span_from_tokens(start_tok, end_tok))
            case _:
                raise ParseError("expected import", self._cur)

    def _parse_pkg_export(self, opaque: bool = False) -> PkgExportDecl:
        start_tok = self._expect(TK.KW_FROM)
        source_path = self._parse_string_path()
        self._expect(TK.KW_EXPORT)
        names = []
        while True:
            name = self._expect_ident()
            alias = self._expect_ident() if self._eat(TK.KW_AS) else None
            names.append((name, alias))
            if not self._eat(TK.COMMA):
                break
        end_tok = self._prev
        self._eat_newlines()
        return PkgExportDecl(
            source_path=source_path,
            names=names,
            opaque=opaque,
            span=self._span_from_tokens(start_tok, end_tok),
        )

    def _parse_pkg_export_all(self) -> PkgExportAllDecl:
        start_tok = self._expect(TK.KW_EXPORT)
        source_path = self._parse_string_path()
        end_tok = self._prev
        self._eat_newlines()
        return PkgExportAllDecl(
            source_path=source_path,
            span=self._span_from_tokens(start_tok, end_tok),
        )

    # ══════════════════════════════════════════════════════════
    # Declarations
    # ══════════════════════════════════════════════════════════

    def _parse_decl(self) -> Decl:
        if self._check(TK.KW_OPAQUE):
            start_tok = self._advance()
            if not self._check(TK.KW_FROM):
                raise ParseError("expected FROM after OPAQUE", self._cur)
            decl = self._parse_pkg_export(opaque=True)
            decl.span = self._span_from_tokens(start_tok, self._prev)
            return decl
        if self._check(TK.KW_EXPORT) and self._peek(1).kind == TK.STRING:
            return self._parse_pkg_export_all()
        if self._check(TK.KW_FROM) and self._peek(1).kind == TK.STRING:
            return self._parse_pkg_export()

        attrs = self._parse_attrs()
        self._eat_newlines()
        vis   = self._parse_vis()

        # function modifiers
        is_inline = bool(self._eat(TK.KW_INLINE))
        is_extern = bool(self._eat(TK.KW_EXTERN))

        match self._cur.kind:
            case TK.KW_FUN:
                decl = self._parse_fun(vis, attrs)
                decl.is_inline = is_inline
                decl.is_extern = is_extern
                return decl
            case TK.KW_TEST:
                return self._parse_test_decl()
            case TK.KW_STRUCT:
                return self._parse_struct(vis, attrs)
            case TK.KW_UNION:
                return self._parse_union(vis)
            case TK.KW_INTERFACE:
                return self._parse_interface(vis)
            case TK.KW_DEF:
                return self._parse_def()
            case TK.KW_TYPE:
                return self._parse_type_alias(vis)
            case TK.KW_IMPORT:
                return self._parse_import()
            case TK.KW_LET:
                return self._parse_let(vis, attrs)
            case TK.KW_FROM:
                tail = self._peek_after_dotted_name()
                if tail == TK.KW_IMPORT:
                    return self._parse_import()
                if tail == TK.KW_EXPORT:
                    return self._parse_from_export()
                raise ParseError("expected IMPORT or EXPORT after package path", self._cur)
            case TK.KW_ERROR:
                self._advance()
                return self._parse_error_decl(vis)
            case TK.SEMI | TK.NEWLINE:
                self._advance()
                return self._parse_decl()
            case _:
                raise ParseError("expected declaration", self._cur)

    # ── fun ──────────────────────────────────────────────────

    def _parse_test_decl(self) -> TestDecl:
        start_tok = self._expect(TK.KW_TEST)
        name_tok = self._expect(TK.STRING)
        body = self._parse_block()
        return TestDecl(
            name=name_tok.lexeme[1:-1],
            body=body,
            span=self._span_from_token_and_end(start_tok, body.span.end if body.span else self._token_end_pos(name_tok)),
        )

    def _parse_fun(self, vis: Visibility,
                   attrs: List[Attribute]) -> FunDecl:
        start_tok = self._expect(TK.KW_FUN)
        name        = self._expect_ident()
        type_params = self._parse_generic_params()  # fun foo[T, U](...)
        self._expect(TK.LPAREN)
        params = self._parse_params()
        self._expect(TK.RPAREN)
        ret    = self._parse_type()
        if self._check(TK.LBRACE):
            body = self._parse_block()
        else:
            self._eat_newlines()
            body = None
        # Optional handle block: } handle |e| { ... }
        handle = self._parse_handle_block() if self._check(TK.KW_HANDLE) else None
        d = FunDecl(vis=vis, attrs=attrs, name=name,
                       params=params, ret=ret, body=body,
                       handle_block=handle)
        if handle and handle.body.span is not None:
            d.span = self._span_from_token_and_end(start_tok, handle.body.span.end)
        elif body and body.span is not None:
            d.span = self._span_from_token_and_end(start_tok, body.span.end)
        else:
            d.span = self._span_from_tokens(start_tok, self._prev)
        d._type_params = type_params  # store for checker
        return d

    def _parse_params(self) -> List[Param]:
        params = []
        while not self._check(TK.RPAREN, TK.EOF):
            if params: self._expect(TK.COMMA)
            if self._check(TK.RPAREN): break

            if self._check(TK.IDENT) and self._cur.lexeme == "self":
                self._advance()
                # self must have explicit type annotation: self: *P or self: P
                self._expect(TK.COLON)
                ty = self._parse_type()
                params.append(Param(name="self", type_=ty))
                continue

            name = self._expect_ident()
            self._expect(TK.COLON)
            ty = self._parse_type()
            default = None
            if self._eat(TK.EQ):
                default = self._parse_expr()
            params.append(Param(name=name, type_=ty, default=default))
        return params

    # ── struct ───────────────────────────────────────────────

    def _parse_struct(self, vis: Visibility,
                      attrs: List[Attribute]) -> StructDecl:
        start_tok = self._expect(TK.KW_STRUCT)
        name   = self._expect_ident()
        params = self._parse_generic_params()
        where  = self._parse_where()
        self._expect(TK.LBRACE)
        self._eat_newlines()
        fields  = []
        methods = []
        while not self._check(TK.RBRACE, TK.EOF):
            self._eat_newlines()
            if self._check(TK.RBRACE, TK.EOF):
                break
            if self._check(TK.KW_FUN, TK.KW_PUB, TK.KW_INLINE, TK.KW_EXTERN):
                m_attrs = self._parse_attrs()
                m_vis   = self._parse_vis()
                m_inline = bool(self._eat(TK.KW_INLINE))
                m = self._parse_fun(m_vis, m_attrs)
                m.is_inline = m_inline
                methods.append(m)
            else:
                fname = self._expect_ident()
                self._expect(TK.COLON)
                ftype = self._parse_type()
                fdefault = None
                if self._eat(TK.EQ):
                    fdefault = self._parse_expr()
                self._eat(TK.COMMA)
                fields.append(FieldDecl(name=fname, type_=ftype,
                                        default=fdefault))
            self._eat_newlines()
        end_tok = self._expect(TK.RBRACE)
        return StructDecl(vis=vis, attrs=attrs, name=name,
                          params=params, fields=fields,
                          methods=methods, where=where,
                          span=self._span_from_tokens(start_tok, end_tok))

    # ── union ────────────────────────────────────────────────

    def _parse_union(self, vis: Visibility) -> UnionDecl:
        start_tok = self._expect(TK.KW_UNION)
        name     = self._expect_ident()
        params   = self._parse_generic_params()
        self._expect(TK.LBRACE)
        self._eat_newlines()
        variants = []
        while not self._check(TK.RBRACE, TK.EOF):
            self._eat_newlines()
            if self._check(TK.RBRACE, TK.EOF):
                break
            vname   = self._expect_ident()
            payload = None
            if self._eat(TK.LPAREN):
                first = self._parse_type()
                if self._eat(TK.COMMA):
                    # multiple payload types → tuple
                    fields = [(None, first)]
                    while True:
                        fields.append((None, self._parse_type()))
                        if not self._eat(TK.COMMA): break
                    payload = TyTuple(fields)
                else:
                    payload = first
                self._expect(TK.RPAREN)
            self._eat(TK.COMMA)
            variants.append(UnionVariant(name=vname, payload=payload))
            self._eat_newlines()
        end_tok = self._expect(TK.RBRACE)
        return UnionDecl(vis=vis, name=name, params=params,
                        variants=variants,
                        span=self._span_from_tokens(start_tok, end_tok))

    # ── interface ────────────────────────────────────────────

    def _parse_interface(self, vis: Visibility) -> InterfaceDecl:
        start_tok = self._expect(TK.KW_INTERFACE)
        name    = self._expect_ident()
        params  = self._parse_generic_params()
        parents = []
        if self._eat(TK.COLON):
            parents.append(self._expect_ident())
            while self._eat(TK.COMMA):
                parents.append(self._expect_ident())
        where   = self._parse_where()
        self._expect(TK.LBRACE)
        self._eat_newlines()
        methods = []
        while not self._check(TK.RBRACE, TK.EOF):
            self._eat_newlines()
            if self._check(TK.RBRACE, TK.EOF):
                break
            # optional method: ?fun
            is_optional = bool(self._eat(TK.QUESTION))
            m_attrs = self._parse_attrs()
            m = self._parse_fun(Visibility.PRIVATE, m_attrs)
            if is_optional:
                m.attrs.append(Attribute(name="optional"))
            methods.append(m)
            self._eat_newlines()
        end_tok = self._expect(TK.RBRACE)
        return InterfaceDecl(vis=vis, name=name, params=params,
                             parents=parents, methods=methods,
                             where=where,
                             span=self._span_from_tokens(start_tok, end_tok))

    # ── def ──────────────────────────────────────────────────

    def _parse_def(self) -> DefDecl:
        start_tok = self._expect(TK.KW_DEF)
        interfaces = [self._expect_ident()]
        while self._eat(TK.COMMA):
            interfaces.append(self._expect_ident())
        self._expect(TK.KW_FOR)   # uses `for` keyword
        for_type = self._expect_ident()
        where    = self._parse_where()
        self._expect(TK.LBRACE)
        self._eat_newlines()
        methods  = []
        while not self._check(TK.RBRACE, TK.EOF):
            self._eat_newlines()
            if self._check(TK.RBRACE, TK.EOF):
                break
            m_attrs = self._parse_attrs()
            m = self._parse_fun(Visibility.PRIVATE, m_attrs)
            methods.append(m)
            self._eat_newlines()
        end_tok = self._expect(TK.RBRACE)
        return DefDecl(interfaces=interfaces, for_type=for_type,
                       methods=methods, where=where,
                       span=self._span_from_tokens(start_tok, end_tok))

    # ── type alias ───────────────────────────────────────────

    def _parse_type_alias(self, vis: Visibility) -> TypeAlias:
        start_tok = self._expect(TK.KW_TYPE)
        name  = self._expect_ident()
        self._expect(TK.EQ)
        type_ = self._parse_type()
        end_tok = self._prev
        self._eat_newlines()
        return TypeAlias(vis=vis, name=name, type_=type_,
                         span=self._span_from_tokens(start_tok, end_tok))

    # ── error declaration ────────────────────────────────────

    def _parse_error_decl(self, vis: Visibility) -> ErrorDecl:
        # KW_ERROR already eaten by caller
        start_tok = self._prev
        name = self._expect_ident()
        self._expect(TK.LBRACE)
        self._eat_newlines()
        variants = []
        while not self._check(TK.RBRACE, TK.EOF):
            vname = self._expect_ident()
            payload = None
            if self._eat(TK.LPAREN):
                payload = self._parse_type()
                self._expect(TK.RPAREN)
            variants.append(UnionVariant(name=vname, payload=payload))
            self._eat(TK.COMMA)
            self._eat_newlines()
        end_tok = self._expect(TK.RBRACE)
        return ErrorDecl(vis=vis, name=name, variants=variants,
                         span=self._span_from_tokens(start_tok, end_tok))

    # ── from X export Y (<pkgname>.pkg) ──────────────────────

    def _parse_from_export(self) -> FromExportDecl:
        start_tok = self._expect(TK.KW_FROM)
        path = self._parse_dotted_name()
        self._expect(TK.KW_EXPORT)
        names  = []
        fields = None
        first  = self._expect_ident()
        names.append(first)
        # field restriction: from X export Type { field1, field2 }
        if self._eat(TK.LBRACE):
            fields = []
            while not self._check(TK.RBRACE, TK.EOF):
                fname = self._expect_ident()
                self._expect(TK.COLON)
                self._parse_type()   # consume type — stored implicitly
                fields.append(fname)
                self._eat(TK.COMMA)
            self._expect(TK.RBRACE)
        else:
            while self._eat(TK.COMMA):
                names.append(self._expect_ident())
        end_tok = self._prev
        self._eat_newlines()
        return FromExportDecl(path=path, names=names, fields=fields,
                              span=self._span_from_tokens(start_tok, end_tok))

    # ── generic params & where ───────────────────────────────

    def _parse_generic_params(self) -> List[str]:
        if not self._eat(TK.LBRACKET): return []
        params = [self._expect_ident()]
        while self._eat(TK.COMMA):
            params.append(self._expect_ident())
        self._expect(TK.RBRACKET)
        return params

    def _parse_where(self) -> List[str]:
        # parse where T : Add, Mul as raw strings for now
        constraints = []
        if not (self._check(TK.IDENT) and self._cur.lexeme == "where"):
            return constraints
        self._advance()   # eat "where"
        # collect until { or other block-starting token
        start = self.pos
        depth = 0
        while not self._check(TK.LBRACE, TK.SEMI, TK.EOF):
            constraints.append(self._cur.lexeme)
            self._advance()
        return constraints

    # ══════════════════════════════════════════════════════════
    # Let statement
    # ══════════════════════════════════════════════════════════

    def _parse_let(self, vis: Visibility = Visibility.PRIVATE, attrs: List[Attribute] = None) -> LetStmt:
        if attrs is None: attrs = []
        start_tok = self._expect(TK.KW_LET)
        mutable  = bool(self._eat(TK.KW_VAR))

        # Unit alias: let `N` := `kg*m/s²`  or  let `km` := 1000.0`m`
        if self._check(TK.UNIT):
            unit_name = self._advance().lexeme
            self._expect(TK.COLON_EQ)
            defn = self._parse_expr()   # UnitLit or bare UnitLit
            end_tok = self._prev
            self._eat_newlines()
            return UnitAlias(vis=vis, name=unit_name, defn=defn,
                             span=self._span_from_tokens(start_tok, end_tok))

        name_tok = self._expect_ident_tok()
        name     = name_tok.lexeme

        # type binding: let Meters := f64
        if self._eat(TK.COLON_EQ):
            type_ = self._parse_type()
            end_tok = self._prev
            self._eat_newlines()
            return TypeAlias(vis=vis, name=name, type_=type_,
                             span=self._span_from_tokens(start_tok, end_tok))

        type_   = None
        if self._eat(TK.COLON):
            type_ = self._parse_type()
        init = None
        if self._eat(TK.EQ):
            init = self._parse_expr()
        end_tok = self._prev
        self._eat_newlines()
        return LetStmt(mutable=mutable, name=name, type_=type_,
                       vis=vis,
                       init=init, attrs=attrs,
                       line=name_tok.line, col=name_tok.col)

    # ══════════════════════════════════════════════════════════
    # Type expressions
    # ══════════════════════════════════════════════════════════

    def _parse_type(self) -> TypeExpr:
        if self._eat(TK.BANG):
            start_tok = self._prev
            payload = self._parse_type()
            end = self._type_span(payload).end if self._type_span(payload) else self._token_end_pos(start_tok)
            return TyErrorUnion(None, payload, span=self._span_from_token_and_end(start_tok, end))
        ty = self._parse_type_base()
        while self._eat(TK.PIPE):
            rhs = self._parse_type_base()
            if isinstance(ty, TyErrorSetUnion):
                ty.members.append(rhs)
                if ty.span is None and self._type_span(rhs) is not None:
                    ty.span = self._type_span(rhs)
                elif ty.span is not None and self._type_span(rhs) is not None:
                    ty.span = SourceSpan(start=ty.span.start, end=self._type_span(rhs).end)
            else:
                left_span = self._type_span(ty)
                right_span = self._type_span(rhs)
                span = SourceSpan(start=left_span.start, end=right_span.end) if left_span and right_span else None
                ty = TyErrorSetUnion([ty, rhs], span=span)
        if self._eat(TK.BANG):
            payload = self._parse_type()
            left_span = self._type_span(ty)
            right_span = self._type_span(payload)
            span = SourceSpan(start=left_span.start, end=right_span.end) if left_span and right_span else None
            return TyErrorUnion(ty, payload, span=span)
        return self._wrap_unitful(ty)

    def _parse_type_base(self) -> TypeExpr:
        match self._cur.kind:
            case TK.QUESTION:
                start_tok = self._advance()
                inner = self._parse_type()
                end = self._type_span(inner).end if self._type_span(inner) else self._token_end_pos(start_tok)
                return TyOptional(inner, span=self._span_from_token_and_end(start_tok, end))
            case TK.STAR:
                start_tok = self._advance()
                if self._eat(TK.KW_ANY):
                    iface_name = self._parse_dotted_name()
                    end_tok = self._prev
                    inner = TyAnyInterface(
                        iface_name=iface_name,
                        span=self._span_from_tokens(start_tok, end_tok),
                    )
                    return TyPointer(inner, span=self._span_from_tokens(start_tok, end_tok))
                inner = self._parse_type()
                end = self._type_span(inner).end if self._type_span(inner) else self._token_end_pos(start_tok)
                return TyPointer(inner, span=self._span_from_token_and_end(start_tok, end))
            case TK.KW_ANY:
                start_tok = self._advance()
                iface_name = self._parse_dotted_name()
                end_tok = self._prev
                return TyAnyInterface(iface_name=iface_name, span=self._span_from_tokens(start_tok, end_tok))
            case TK.LBRACKET:
                start_tok = self._advance()
                if self._eat(TK.RBRACKET):
                    inner = self._parse_type()
                    end = self._type_span(inner).end if self._type_span(inner) else self._token_end_pos(start_tok)
                    return TySlice(inner, span=self._span_from_token_and_end(start_tok, end))
                inner = self._parse_type()
                self._expect_newline_or(TK.SEMI)
                size = self._parse_expr()
                end_tok = self._expect(TK.RBRACKET)
                return TyVec(elem=inner, size=size, span=self._span_from_tokens(start_tok, end_tok))
            case TK.TY_VOID:
                tok = self._advance()
                ty = TyVoid()
                ty.span = self._span_from_tokens(tok, tok)
                return ty
            case TK.IDENT if self._cur.lexeme == "vec":
                start_tok = self._advance()
                self._expect(TK.LBRACKET)
                elem = self._parse_type()
                size = self._parse_expr() if self._eat(TK.SEMI) else None
                end_tok = self._expect(TK.RBRACKET)
                return TyVec(elem=elem, size=size, span=self._span_from_tokens(start_tok, end_tok))
            case TK.IDENT if self._cur.lexeme == "mat":
                start_tok = self._advance()
                self._expect(TK.LBRACKET)
                elem = self._parse_type()
                rows = cols = None
                if self._eat(TK.SEMI):
                    rows = self._parse_expr()
                    if self._eat(TK.COMMA):
                        cols = self._parse_expr()
                end_tok = self._expect(TK.RBRACKET)
                return TyMat(elem=elem, rows=rows, cols=cols, span=self._span_from_tokens(start_tok, end_tok))
            case TK.DOT:
                start_tok = self._advance()
                self._expect(TK.LBRACE)
                fields = []
                while not self._check(TK.RBRACE, TK.EOF):
                    if fields:
                        self._expect(TK.COMMA)
                    if self._check(TK.RBRACE):
                        break
                    fname = None
                    if self._check(TK.IDENT) and self._peek(1).kind == TK.COLON:
                        fname = self._advance().lexeme
                        self._advance()
                    fields.append((fname, self._parse_type()))
                end_tok = self._expect(TK.RBRACE)
                return TyTuple(fields, span=self._span_from_tokens(start_tok, end_tok))
            case TK.KW_FUN:
                start_tok = self._advance()
                self._expect(TK.LPAREN)
                params = []
                while not self._check(TK.RPAREN, TK.EOF):
                    if params:
                        self._expect(TK.COMMA)
                    params.append(self._parse_type())
                self._expect(TK.RPAREN)
                ret = self._parse_type()
                end = self._type_span(ret).end if self._type_span(ret) else self._token_end_pos(start_tok)
                return TyFun(params=params, ret=ret, span=self._span_from_token_and_end(start_tok, end))
            case TK.AT:
                start_tok = self._advance()
                ident_tok = self._expect_ident_tok()
                return TyNamed("Self" if ident_tok.lexeme == "this" else ident_tok.lexeme,
                               span=self._span_from_tokens(start_tok, ident_tok))
            case kind if kind in _PRIM_TYPES:
                tok = self._advance()
                return TyPrimitive(_PRIM_TYPES[tok.kind], span=self._span_from_tokens(tok, tok))
            case TK.IDENT:
                start_tok = self._advance()
                parts = [start_tok.lexeme]
                end_tok = start_tok
                while self._eat(TK.DOT):
                    ident_tok = self._expect_ident_tok()
                    parts.append(ident_tok.lexeme)
                    end_tok = ident_tok
                name = ".".join(parts)
                if self._eat(TK.LBRACKET):
                    params = [self._parse_type()]
                    while self._eat(TK.COMMA):
                        params.append(self._parse_type())
                    end_tok = self._expect(TK.RBRACKET)
                    return TyGeneric(name=name, params=params, span=self._span_from_tokens(start_tok, end_tok))
                return TyNamed(name, span=self._span_from_tokens(start_tok, end_tok))
            case _:
                ty = TyInfer()
                return ty

    # ══════════════════════════════════════════════════════════
    # Statements
    # ══════════════════════════════════════════════════════════

    def _parse_block(self) -> Block:
        start_tok = self._expect(TK.LBRACE)
        self._eat_newlines()
        stmts = []
        tail  = None
        while not self._check(TK.RBRACE, TK.EOF):
            self._eat_newlines()
            if self._check(TK.RBRACE, TK.EOF):
                break
            stmt = self._parse_stmt()
            if isinstance(stmt, ExprStmt) and self._check(TK.RBRACE):
                tail = stmt.expr
            else:
                stmts.append(stmt)
            self._eat_newlines()
        end_tok = self._expect(TK.RBRACE)
        return Block(stmts=stmts, tail=tail,
                     span=self._span_from_tokens(start_tok, end_tok))

    def _parse_stmt(self) -> Stmt:
        self._eat_newlines()
        # named loop label:  label_name: for/while { }
        label = None
        if (self._check(TK.IDENT) and
                self._peek(1).kind == TK.COLON and
                self._peek(2).kind in (TK.KW_FOR, TK.KW_WHILE)):
            label = self._advance().lexeme
            self._advance()   # eat :
            stmt  = self._parse_loop_stmt(label)
            return stmt

        match (self._cur.kind, self._peek(1).kind):
            case (TK.BANG, TK.KW_DEFER):
                self._advance()
                self._advance()
                return DeferStmt(error_only=True, body=self._parse_defer_body())
            case _:
                pass

        match self._cur.kind:
            case TK.KW_DEFER:
                self._advance()
                return DeferStmt(error_only=False, body=self._parse_defer_body())
            case TK.KW_LET:
                return self._parse_let()
            case TK.KW_RETURN:
                return self._parse_return()
            case TK.KW_BREAK:
                return self._parse_break()
            case TK.KW_CONTINUE:
                self._advance()
                label = self._advance().lexeme if self._check(TK.IDENT) else None
                self._eat_newlines()
                return ContinueStmt(label=label)
            case TK.KW_FOR:
                return self._parse_for()
            case TK.KW_WHILE:
                return self._parse_while()
            case _:
                return self._parse_expr_stmt()

    def _parse_return(self) -> ReturnStmt:
        ret_tok = self._expect(TK.KW_RETURN)
        val = None
        if not self._check(TK.SEMI, TK.RBRACE, TK.EOF):
            val = self._parse_expr()
        self._eat_newlines()
        return ReturnStmt(value=val, line=ret_tok.line, col=ret_tok.col)

    def _parse_break(self) -> BreakStmt:
        self._expect(TK.KW_BREAK)
        label = None
        val   = None
        # break label_name  or  break value  or  break label_name value
        if self._check(TK.IDENT) and self._peek(1).kind in (TK.SEMI, TK.RBRACE, TK.EOF):
            label = self._advance().lexeme
        elif self._check(TK.IDENT) and self._peek(1).kind != TK.EQ:
            # could be a label or a value expression — if next is semi/} it's a label
            pass
        if not self._check(TK.SEMI, TK.RBRACE, TK.EOF):
            val = self._parse_expr()
        self._eat_newlines()
        return BreakStmt(label=label, value=val)

    def _parse_loop_stmt(self, label: Optional[str]) -> Stmt:
        """Dispatch to for/while with a label already parsed."""
        match self._cur.kind:
            case TK.KW_FOR:
                stmt = self._parse_for()
                stmt.label = label
                return stmt
            case TK.KW_WHILE:
                stmt = self._parse_while()
                if hasattr(stmt, "label"):
                    stmt.label = label
                return stmt
            case _:
                raise ParseError("expected for or while after label", self._cur)

    def _parse_for(self) -> Stmt:
        self._expect(TK.KW_FOR)

        # range: for i = 0...n if cond { }
        if self._check(TK.IDENT) and self._peek(1).kind == TK.EQ:
            var = self._advance().lexeme
            self._advance()   # eat =
            start = self._prec(0)  # no suffix-if for range bounds
            if self._eat(TK.DOT_DOT_DOT): inclusive = True
            else: self._expect(TK.DOT_DOT); inclusive = False
            end    = self._prec(0)  # no suffix-if for range bounds
            filter = None
            if self._eat(TK.COLON):
                filter = self._parse_expr()
            body   = self._parse_block_or_comma_stmt()
            return ForRangeStmt(var=var, start=start, end=end,
                                inclusive=inclusive, filter=filter,
                                body=body, label=None)

        # iterator: for *p in iter : cond { }  or  , single_stmt
        pat    = self._parse_for_pattern()
        self._expect(TK.KW_IN)
        iter_  = self._parse_expr()
        filter = None
        if self._eat(TK.COLON):
            filter = self._parse_expr()
        body   = self._parse_block_or_comma_stmt()
        return ForIterStmt(pattern=pat, iter=iter_,
                           filter=filter, body=body, label=None)

    def _parse_for_pattern(self) -> ForPattern:
        if self._eat(TK.STAR):
            return PatRef(self._expect_ident())
        if self._eat(TK.LPAREN):
            names = [self._expect_ident()]
            while self._eat(TK.COMMA):
                names.append(self._expect_ident())
            self._expect(TK.RPAREN)
            return PatTuple(names)
        return PatIdent(self._expect_ident())

    def _parse_while(self) -> Stmt:
        self._expect(TK.KW_WHILE)
        # while expr |v| { } — unwrapping form
        expr = self._parse_expr()
        if self._eat(TK.PIPE):   # | binding |
            is_ref  = bool(self._eat(TK.STAR))
            binding = self._expect_ident()
            self._expect(TK.PIPE)
            body = self._parse_block()
            return ExprStmt(WhileUnwrap(expr=expr, binding=binding,
                                        is_ref=is_ref, body=body))
        body = self._parse_block_or_comma_stmt()
        return WhileStmt(cond=expr, body=body)

    def _parse_cond_expr(self) -> Expr:
        """Parse a condition for suffix ternary — binary ops only, no orelse/catch."""
        # We parse with _prec but stop before postfix orelse/catch by
        # only going through _parse_unary (which calls _parse_postfix,
        # which handles orelse as postfix). The trick: after _prec, we're done.
        # The issue is _parse_postfix consumes orelse. We need _prec without orelse.
        # Solution: temporarily parse a binary expression that stops at known terminators.
        left = self._parse_unary_no_postfix_chain()
        while True:
            prec = _PREC.get(self._cur.kind)
            if prec is None or prec < 0: break
            op_tok = self._advance()
            op = _BINOP[op_tok.kind]
            next_prec = prec if op_tok.kind in _RIGHT_ASSOC else prec + 1
            right = self._parse_unary_no_postfix_chain()
            left = BinExpr(op=op, left=left, right=right, span=self._span_from_exprs(left, right))
        return left

    def _parse_unary_no_postfix_chain(self) -> Expr:
        """Parse unary + primary without the postfix orelse/catch chain."""
        match self._cur.kind:
            case TK.MINUS:
                op_tok = self._advance()
                operand = self._parse_unary_no_postfix_chain()
                return UnaryExpr("-", operand, span=self._span_from_token_and_end(op_tok, self._expr_span(operand).end if self._expr_span(operand) else SourcePos(op_tok.line, op_tok.col + 1)))
            case TK.BANG:
                op_tok = self._advance()
                operand = self._parse_unary_no_postfix_chain()
                return UnaryExpr("!", operand, span=self._span_from_token_and_end(op_tok, self._expr_span(operand).end if self._expr_span(operand) else SourcePos(op_tok.line, op_tok.col + 1)))
            case TK.STAR:
                op_tok = self._advance()
                operand = self._parse_unary_no_postfix_chain()
                return UnaryExpr("*", operand, span=self._span_from_token_and_end(op_tok, self._expr_span(operand).end if self._expr_span(operand) else SourcePos(op_tok.line, op_tok.col + 1)))
            case TK.AMP:
                op_tok = self._advance()
                operand = self._parse_unary_no_postfix_chain()
                return UnaryExpr("&", operand, span=self._span_from_token_and_end(op_tok, self._expr_span(operand).end if self._expr_span(operand) else SourcePos(op_tok.line, op_tok.col + 1)))
            case _:
                return self._parse_postfix_chain(
                    self._parse_primary(),
                    allow_orelse=False,
                    allow_catch=False,
                )

    def _wrap_unitful(self, inner):
        """Consume a trailing UNIT token and return TyUnitful(inner, unit)."""
        if self._check(TK.UNIT):
            unit_tok = self._advance()
            inner_span = self._type_span(inner)
            span = (
                SourceSpan(start=inner_span.start, end=self._token_end_pos(unit_tok))
                if inner_span is not None else
                self._span_from_tokens(unit_tok, unit_tok)
            )
            return TyUnitful(inner=inner, unit=unit_tok.lexeme, span=span)
        return inner

    def _parse_block_or_comma_stmt(self) -> Block:
        """Parse { block } or , single_stmt for one-liner bodies."""
        if self._eat(TK.COMMA):
            stmt = self._parse_stmt()
            if isinstance(stmt, ExprStmt):
                return Block(stmts=[], tail=stmt.expr)
            return Block(stmts=[stmt], tail=None)
        return self._parse_block()

    def _parse_defer_body(self) -> Block:
        """Parse defer body as either { ... } or a single statement."""
        if self._check(TK.LBRACE):
            return self._parse_block()
        stmt = self._parse_stmt()
        if isinstance(stmt, ExprStmt):
            return Block(stmts=[], tail=stmt.expr)
        return Block(stmts=[stmt], tail=None)

    def _parse_handle_block(self) -> HandleBlock:
        self._expect(TK.KW_HANDLE)
        self._expect(TK.PIPE)
        binding = self._expect_ident()
        self._expect(TK.PIPE)
        return HandleBlock(binding=binding, body=self._parse_block())

    def _parse_expr_stmt(self) -> Stmt:
        expr = self._parse_expr()
        if self._cur.kind in _ASSIGN_OPS:
            op  = _ASSIGN_OPS[self._advance().kind]
            rhs = self._parse_expr()
            self._eat_newlines()
            return AssignStmt(target=expr, op=op, value=rhs)
        self._eat_newlines()
        return ExprStmt(expr)

    # ══════════════════════════════════════════════════════════
    # Expressions — precedence climbing
    # ══════════════════════════════════════════════════════════

    def _parse_expr(self) -> Expr:
        expr = self._prec(0)
        # Uncertain literal: value +- error  or  value +- error `unit`
        if self._eat(TK.PLUS_MINUS):
            err = self._prec(0)
            # If the error term already consumed a unit (0.5`N`), lift it up:
            # 10.0 +- 0.5`N`  →  UnitLit(UncertainLit(10.0, 0.5), "N")
            if isinstance(err, UnitLit) and err.value is not None:
                inner = UncertainLit(
                    value=expr,
                    error=err.value,
                    span=self._span_from_exprs(expr, err.value),
                )
                expr = UnitLit(
                    value=inner,
                    unit=err.unit,
                    span=self._span_from_exprs(expr, err),
                )
            else:
                expr = UncertainLit(value=expr, error=err, span=self._span_from_exprs(expr, err))
        # Unit suffix on full expression: 10.0`N`, (a + b)`m`, UncertainLit`N`
        if self._check(TK.UNIT):
            unit_tok = self._advance()
            expr = UnitLit(
                value=expr,
                unit=unit_tok.lexeme,
                span=SourceSpan(
                    start=self._expr_span(expr).start,
                    end=self._token_end_pos(unit_tok),
                ) if self._expr_span(expr) else None,
            )
        # Suffix ternary: expr if condition  →  ?T (lowest precedence, same line only)
        # Only applies to scalar/call expressions — NOT to compound exprs ending in }
        # (IfExpr, BlockExpr, MatchExpr, WhileExpr) to avoid "if a { } if b" ambiguity.
        _COMPOUND = (IfExpr, BlockExpr, MatchExpr, WithExpr)
        if (self._check(TK.KW_IF) and
                self._cur.line == self._prev.line and
                self._peek(1).kind not in (TK.LBRACE, TK.NEWLINE, TK.SEMI) and
                not isinstance(expr, _COMPOUND)):
            start_expr = expr
            self._advance()  # eat 'if'
            # Parse condition without consuming postfix operators (orelse, catch)
            # that belong to the outer expression (e.g. "val if cond orelse default")
            cond = self._parse_cond_expr()
            then_block = Block(stmts=[], tail=expr)
            else_block = Block(stmts=[], tail=NoneLit())
            result = IfExpr(
                cond=cond,
                then_block=then_block,
                else_block=else_block,
                span=self._span_from_exprs(start_expr, cond),
            )
            # Apply remaining postfix operators (orelse, catch) to the whole ?T result
            while True:
                if self._eat(TK.KW_ORELSE):
                    rhs = self._parse_expr()
                    result = CallExpr(callee=Ident("__orelse"),
                                      args=[Arg(None, result), Arg(None, rhs)])
                elif self._eat(TK.KW_CATCH):
                    binding = None
                    if self._eat(TK.PIPE):
                        binding = self._expect_ident()
                        self._expect(TK.PIPE)
                    handler = self._parse_block()
                    if binding:
                        result = CallExpr(callee=Ident("__catch_bind"),
                                          args=[Arg(None, result),
                                                Arg("binding", StringLit(binding, [])),
                                                Arg("handler", BlockExpr(handler))])
                    else:
                        result = CallExpr(callee=Ident("__catch"),
                                          args=[Arg(None, result),
                                                Arg("handler", BlockExpr(handler))])
                else:
                    break
            return result
        return expr

    def _prec(self, min_prec: int) -> Expr:
        left = self._parse_unary()
        while True:
            prec = _PREC.get(self._cur.kind)
            if prec is None or prec < min_prec: break
            op_tok = self._advance()
            op     = _BINOP[op_tok.kind]
            # right-associative for ^
            next_prec = prec if op_tok.kind in _RIGHT_ASSOC else prec + 1
            right = self._prec(next_prec)
            left  = BinExpr(op=op, left=left, right=right, span=self._span_from_exprs(left, right))
        return left

    def _parse_unary(self) -> Expr:
        match self._cur.kind:
            case TK.MINUS:
                op_tok = self._advance()
                operand = self._parse_unary()
                return UnaryExpr("-", operand, span=self._span_from_token_and_end(op_tok, self._expr_span(operand).end if self._expr_span(operand) else SourcePos(op_tok.line, op_tok.col + 1)))
            case TK.BANG:
                op_tok = self._advance()
                operand = self._parse_unary()
                return UnaryExpr("!", operand, span=self._span_from_token_and_end(op_tok, self._expr_span(operand).end if self._expr_span(operand) else SourcePos(op_tok.line, op_tok.col + 1)))
            case TK.STAR:
                op_tok = self._advance()
                operand = self._parse_unary()
                return UnaryExpr("*", operand, span=self._span_from_token_and_end(op_tok, self._expr_span(operand).end if self._expr_span(operand) else SourcePos(op_tok.line, op_tok.col + 1)))
            case TK.AMP:
                op_tok = self._advance()
                operand = self._parse_unary()
                return UnaryExpr("&", operand, span=self._span_from_token_and_end(op_tok, self._expr_span(operand).end if self._expr_span(operand) else SourcePos(op_tok.line, op_tok.col + 1)))
            case TK.AT:
                self._advance()
                if self._cur.kind != TK.IDENT and not self._cur.kind.name.startswith("KW_"):
                    raise ParseError("expected IDENT", self._cur)
                tok = self._advance()
                base = Ident(
                    f"@{tok.lexeme}",
                    line=tok.line,
                    col=tok.col,
                    span=self._span_from_tokens(tok, tok),
                )
                return self._parse_postfix_chain(
                    base,
                    allow_orelse=True,
                    allow_catch=True,
                )
            case TK.KW_COMPTIME:
                start_tok = self._advance()
                operand = self._parse_unary()
                return ComptimeExpr(
                    operand,
                    span=self._span_from_token_and_end(start_tok, self._expr_span(operand).end if self._expr_span(operand) else SourcePos(start_tok.line, start_tok.col + len(start_tok.lexeme))),
                )
            case TK.KW_TRY:
                start_tok = self._advance()
                operand = self._parse_unary()
                callee = Ident("__try")
                return CallExpr(
                    callee=callee,
                    args=[Arg(None, operand)],
                    line=start_tok.line,
                    col=start_tok.col,
                    span=self._span_from_token_and_end(start_tok, self._expr_span(operand).end if self._expr_span(operand) else SourcePos(start_tok.line, start_tok.col + len(start_tok.lexeme))),
                )
            case TK.KW_ESC:
                start_tok = self._advance()
                operand = self._parse_unary()
                return EscExpr(
                    expr=operand,
                    span=self._span_from_token_and_end(
                        start_tok,
                        self._expr_span(operand).end if self._expr_span(operand) else SourcePos(start_tok.line, start_tok.col + len(start_tok.lexeme)),
                    ),
                )
            case _:
                return self._parse_postfix()

    def _parse_postfix_chain(self, base: Expr, *, allow_orelse: bool, allow_catch: bool) -> Expr:
        while True:
            match self._cur.kind:
                case TK.QUESTION_DOT:
                    dot_tok = self._advance()
                    field_tok = self._expect_ident_tok()
                    base = FieldExpr(
                        obj=CallExpr(callee=Ident("__optional_chain"), args=[Arg(None, base)]),
                        field=field_tok.lexeme,
                        span=self._span_from_token_and_end(
                            dot_tok,
                            self._token_end_pos(field_tok),
                        ),
                    )
                case TK.DOT:
                    self._advance()
                    field_tok = self._expect_ident_tok()
                    if self._check(TK.LPAREN):
                        args = self._parse_arglist()
                        field_expr = FieldExpr(
                            obj=base,
                            field=field_tok.lexeme,
                            span=SourceSpan(
                                start=self._expr_span(base).start,
                                end=self._token_end_pos(field_tok),
                            ) if self._expr_span(base) else None,
                        )
                        base = CallExpr(
                            callee=field_expr,
                            args=args,
                            line=self._expr_span(base).start.line if self._expr_span(base) else 0,
                            col=self._expr_span(base).start.col if self._expr_span(base) else 0,
                            span=SourceSpan(
                                start=self._expr_span(base).start,
                                end=self._token_end_pos(self._prev),
                            ) if self._expr_span(base) else None,
                        )
                    else:
                        base = FieldExpr(
                            obj=base,
                            field=field_tok.lexeme,
                            span=SourceSpan(
                                start=self._expr_span(base).start,
                                end=self._token_end_pos(field_tok),
                            ) if self._expr_span(base) else None,
                        )
                case TK.LPAREN:
                    args = self._parse_arglist()
                    base = CallExpr(
                        callee=base,
                        args=args,
                        line=self._expr_span(base).start.line if self._expr_span(base) else 0,
                        col=self._expr_span(base).start.col if self._expr_span(base) else 0,
                        span=SourceSpan(
                            start=self._expr_span(base).start,
                            end=self._token_end_pos(self._prev),
                        ) if self._expr_span(base) else None,
                    )
                case TK.LBRACKET:
                    self._advance()
                    indices = [self._parse_expr()]
                    while self._eat(TK.COMMA):
                        indices.append(self._parse_expr())
                    end_tok = self._expect(TK.RBRACKET)
                    base = IndexExpr(
                        obj=base,
                        indices=indices,
                        span=SourceSpan(
                            start=self._expr_span(base).start,
                            end=self._token_end_pos(end_tok),
                        ) if self._expr_span(base) else None,
                    )
                case TK.KW_ORELSE if allow_orelse:
                    start_tok = self._advance()
                    rhs = self._parse_expr()
                    base = CallExpr(
                        callee=Ident("__orelse"),
                        args=[Arg(None, base), Arg(None, rhs)],
                        line=start_tok.line,
                        col=start_tok.col,
                        span=self._span_from_exprs(base, rhs),
                    )
                case TK.KW_CATCH if allow_catch:
                    start_tok = self._advance()
                    binding = None
                    if self._eat(TK.PIPE):
                        binding = self._expect_ident()
                        self._expect(TK.PIPE)
                    arms = self._parse_catch_arms()
                    if binding:
                        base = CallExpr(
                            callee=Ident("__catch_bind"),
                            args=[Arg(None, base),
                                  Arg("binding", StringLit(binding, [])),
                                  Arg("arms", MatchExpr(value=base, arms=arms))],
                            line=start_tok.line,
                            col=start_tok.col,
                        )
                    else:
                        base = CallExpr(
                            callee=Ident("__catch"),
                            args=[Arg(None, base), Arg("arms", MatchExpr(value=base, arms=arms))],
                            line=start_tok.line,
                            col=start_tok.col,
                        )
                case TK.KW_WITH:
                    with_tok = self._advance()
                    alloc = self._parse_unary()
                    base = WithAllocExpr(
                        expr=base,
                        allocator=alloc,
                        span=self._span_from_token_and_end(
                            with_tok,
                            self._expr_span(alloc).end if self._expr_span(alloc) else SourcePos(with_tok.line, with_tok.col + len(with_tok.lexeme)),
                        ),
                    )
                case _:
                    return base

    def _parse_postfix(self) -> Expr:
        return self._parse_postfix_chain(
            self._parse_primary(),
            allow_orelse=True,
            allow_catch=True,
        )

    def _parse_arglist(self) -> List[Arg]:
        self._expect(TK.LPAREN)
        args = []
        while not self._check(TK.RPAREN, TK.EOF):
            if args: self._expect(TK.COMMA)
            if self._check(TK.RPAREN): break
            # named arg: key = value
            name = None
            if self._check(TK.IDENT) and self._peek(1).kind == TK.EQ:
                name = self._advance().lexeme
                self._advance()
            val = self._parse_expr()
            args.append(Arg(name=name, value=val))
        self._expect(TK.RPAREN)
        return args

    def _parse_primary(self) -> Expr:
        match self._cur.kind:
            case TK.UNIT:
                tok = self._advance()
                return UnitLit(value=None, unit=tok.lexeme, span=self._span_from_tokens(tok, tok))
            case TK.INT:
                tok = self._advance()
                raw = tok.lexeme.replace("_", "")
                if raw.startswith(("0x", "0X")):
                    val = int(raw, 16)
                elif raw.startswith(("0b", "0B")):
                    val = int(raw, 2)
                elif raw.startswith(("0o", "0O")):
                    val = int(raw, 8)
                else:
                    val = int(raw)
                lit = IntLit(value=val, line=tok.line, col=tok.col)
                if self._check(TK.UNIT):
                    unit_tok = self._advance()
                    return UnitLit(
                        value=lit,
                        unit=unit_tok.lexeme,
                        span=self._span_from_tokens(tok, unit_tok),
                    )
                return lit
            case TK.FLOAT:
                tok = self._advance()
                lit = FloatLit(value=float(tok.lexeme.replace("_", "")), line=tok.line, col=tok.col)
                if self._check(TK.UNIT):
                    unit_tok = self._advance()
                    return UnitLit(
                        value=lit,
                        unit=unit_tok.lexeme,
                        span=self._span_from_tokens(tok, unit_tok),
                    )
                return lit
            case TK.TRUE:
                tok = self._advance()
                return BoolLit(value=True, line=tok.line, col=tok.col)
            case TK.FALSE:
                tok = self._advance()
                return BoolLit(value=False, line=tok.line, col=tok.col)
            case TK.NONE:
                self._advance()
                return NoneLit()
            case TK.STRING:
                tok = self._advance()
                raw = tok.lexeme[1:-1]
                return StringLit(raw=raw, line=tok.line, col=tok.col,
                                 segments=self._parse_interpolation(raw))
            case TK.MULTILINE:
                tok = self._advance()
                raw = tok.lexeme
                return StringLit(raw=raw, line=tok.line, col=tok.col,
                                 segments=self._parse_interpolation(raw))
            case kind if kind in _PRIM_TYPES:
                tok = self._advance()
                return Ident(
                    name=_PRIM_TYPES[kind],
                    line=tok.line,
                    col=tok.col,
                    span=self._span_from_tokens(tok, tok),
                )
            case TK.IDENT if self._cur.lexeme == "self":
                self._advance()
                return SelfExpr()
            case TK.IDENT if self._cur.lexeme == "vec":
                return self._parse_vec()
            case TK.IDENT if self._cur.lexeme == "box":
                start_tok = self._advance()
                self._expect(TK.LBRACKET)
                elems = []
                while not self._check(TK.RBRACKET, TK.EOF):
                    if elems:
                        self._expect(TK.COMMA)
                    elems.append(self._parse_expr())
                end_tok = self._expect(TK.RBRACKET)
                return BoxLit(elems, span=self._span_from_tokens(start_tok, end_tok))
            case TK.IDENT:
                tok = self._advance()
                return Ident(
                    name=tok.lexeme,
                    line=tok.line,
                    col=tok.col,
                    span=self._span_from_tokens(tok, tok),
                )
            case TK.LPAREN:
                self._advance()
                expr = self._parse_expr()
                self._expect(TK.RPAREN)
                return expr
            case TK.LBRACE:
                block = self._parse_block()
                return BlockExpr(block, span=block.span)
            case TK.KW_WITH:
                start_tok = self._advance()
                resource = self._parse_expr()
                cleanup = None
                if self._eat(TK.COLON):
                    self._expect(TK.DOT)
                    cleanup = self._expect_ident()
                body = self._parse_block()
                handle = self._parse_handle_block() if self._check(TK.KW_HANDLE) else None
                end = handle.body.span.end if handle and handle.body.span else (body.span.end if body.span else self._expr_span(resource).end)
                return WithExpr(
                    resource=resource,
                    cleanup=cleanup,
                    body=body,
                    handle=handle,
                    span=self._span_from_token_and_end(start_tok, end),
                )
            case TK.KW_IF:
                return self._parse_if()
            case TK.KW_MATCH:
                return self._parse_match()
            case TK.KW_WHILE:
                start_tok = self._advance()
                expr = self._parse_expr()
                is_ref = False
                if self._eat(TK.PIPE):
                    is_ref = bool(self._eat(TK.STAR))
                    binding = self._expect_ident()
                    self._expect(TK.PIPE)
                    body = self._parse_block()
                    return WhileUnwrap(
                        expr=expr,
                        binding=binding,
                        is_ref=is_ref,
                        body=body,
                        span=self._span_from_token_and_end(start_tok, body.span.end if body.span else self._expr_span(expr).end),
                    )
                body = self._parse_block()
                block = Block(stmts=[WhileStmt(cond=expr, body=body)], tail=None, span=body.span)
                return BlockExpr(block, span=block.span)
            case TK.LBRACKET:
                start_tok = self._advance()
                elems = []
                while not self._check(TK.RBRACKET, TK.EOF):
                    if elems:
                        self._expect(TK.COMMA)
                    if self._check(TK.RBRACKET):
                        break
                    elems.append(self._parse_expr())
                end_tok = self._expect(TK.RBRACKET)
                return ArrayLit(elems, span=self._span_from_tokens(start_tok, end_tok))
            case TK.DOT:
                dot_tok = self._advance()
                if self._check(TK.IDENT):
                    tok = self._advance()
                    return VariantLit(
                        name=tok.lexeme,
                        line=tok.line,
                        col=tok.col,
                        span=self._span_from_tokens(dot_tok, tok),
                    )
                lbrace_tok = self._expect(TK.LBRACE)
                return self._parse_tuple_lit_body(dot_tok, lbrace_tok)
            case TK.KW_FUN if self._peek(1).kind == TK.LPAREN:
                start_tok = self._advance()
                self._expect(TK.LPAREN)
                params = self._parse_params()
                self._expect(TK.RPAREN)
                ret = self._parse_type()
                body = self._parse_block()
                return Closure(
                    params=params,
                    ret=ret,
                    body=body,
                    span=self._span_from_token_and_end(start_tok, body.span.end if body.span else SourcePos(start_tok.line, start_tok.col + len(start_tok.lexeme))),
                )
            case TK.KW_COMPTIME:
                start_tok = self._advance()
                expr = self._parse_expr()
                return ComptimeExpr(
                    expr,
                    span=self._span_from_token_and_end(start_tok, self._expr_span(expr).end if self._expr_span(expr) else SourcePos(start_tok.line, start_tok.col + len(start_tok.lexeme))),
                )
            case _:
                raise ParseError("expected expression", self._cur)

    def _parse_if(self) -> Expr:
        start_tok = self._expect(TK.KW_IF)
        expr = self._parse_expr()
        # if expr |v| { } — optional unwrap
        if self._eat(TK.PIPE):
            is_ref  = bool(self._eat(TK.STAR))
            binding = self._expect_ident()
            self._expect(TK.PIPE)
            then = self._parse_block_or_comma_stmt()
            else_ = None
            self._eat_newlines()
            if self._eat(TK.KW_ELSE):
                else_ = self._parse_block_or_comma_stmt()
            end = else_.span.end if else_ and else_.span else (then.span.end if then.span else self._expr_span(expr).end)
            return IfUnwrap(
                expr=expr,
                binding=binding,
                is_ref=is_ref,
                then_block=then,
                else_block=else_,
                span=self._span_from_token_and_end(start_tok, end),
            )
        # regular if — can use comma for single-statement body
        then = self._parse_block_or_comma_stmt()
        else_ = None
        self._eat_newlines()
        if self._eat(TK.KW_ELSE):
            if self._check(TK.KW_IF):
                # else if chain — wrap in block
                inner = self._parse_if()
                else_ = Block(stmts=[], tail=inner)
            else:
                else_ = self._parse_block_or_comma_stmt()
        end = else_.span.end if else_ and else_.span else (then.span.end if then.span else self._expr_span(expr).end)
        return IfExpr(
            cond=expr,
            then_block=then,
            else_block=else_,
            span=self._span_from_token_and_end(start_tok, end),
        )

    def _parse_catch_arms(self) -> list:
        """Parse { .Variant => expr, .Other(p) => expr, _ => expr } for catch."""
        self._expect(TK.LBRACE)
        self._eat_newlines()
        arms = []
        while not self._check(TK.RBRACE, TK.EOF):
            pat  = self._parse_match_pattern()
            self._eat_newlines()
            self._expect(TK.FAT_ARROW)
            self._eat_newlines()
            if self._check(TK.LBRACE):
                body = self._parse_block()
            else:
                expr = self._parse_expr()
                body = Block(stmts=[], tail=expr)
            self._eat(TK.COMMA)
            self._eat_newlines()
            arms.append(MatchArm(pattern=pat, body=body))
        self._expect(TK.RBRACE)
        return arms

    def _parse_match(self) -> MatchExpr:
        start_tok = self._expect(TK.KW_MATCH)
        val  = self._parse_expr()
        self._expect(TK.LBRACE)
        self._eat_newlines()
        arms = []
        while not self._check(TK.RBRACE, TK.EOF):
            pat  = self._parse_match_pattern()
            self._eat_newlines()
            self._expect(TK.FAT_ARROW)
            self._eat_newlines()
            # Arm body: a single expression (no braces needed)
            # If next token is { it might be a block expression for multi-statement arms
            if self._check(TK.LBRACE):
                body = self._parse_block()
            else:
                expr = self._parse_expr()
                body = Block(stmts=[], tail=expr)
            self._eat(TK.COMMA)
            self._eat_newlines()
            arms.append(MatchArm(pattern=pat, body=body))
        end_tok = self._expect(TK.RBRACE)
        return MatchExpr(value=val, arms=arms, span=self._span_from_tokens(start_tok, end_tok))

    def _parse_match_pattern(self) -> MatchPattern:
        match self._cur.kind:
            case TK.IDENT if self._cur.lexeme == "_":
                self._advance()
                return PatWildcard()
            case TK.INT:
                return PatInt(int(self._advance().lexeme))
            case TK.FLOAT:
                return PatFloat(float(self._advance().lexeme))
            case TK.TRUE:
                self._advance()
                return PatBool(True)
            case TK.FALSE:
                self._advance()
                return PatBool(False)
            case TK.NONE:
                self._advance()
                return PatNone()
            case TK.DOT:
                self._advance()
                name = self._expect_ident()
                if self._eat(TK.LPAREN):
                    bindings = []
                    while not self._check(TK.RPAREN, TK.EOF):
                        if bindings:
                            self._expect(TK.COMMA)
                        bindings.append(self._expect_ident())
                    self._expect(TK.RPAREN)
                    binding = bindings[0] if bindings else None
                    extra = bindings[1:] if len(bindings) > 1 else []
                    return PatVariant(name=name, binding=binding, extra_bindings=extra)
                return PatVariant(name=name, binding=None, extra_bindings=[])
            case _:
                return PatIdent(self._expect_ident())

    def _parse_vec(self) -> Expr:
        start_tok = self._advance()   # eat vec
        self._expect(TK.LBRACKET)
        if self._check(TK.RBRACKET):
            end_tok = self._advance()
            return VecLit([], span=self._span_from_tokens(start_tok, end_tok))
        first = self._parse_expr()
        # comprehension: vec[expr for pat in iter : cond]
        if self._eat(TK.KW_FOR):
            pat    = self._parse_for_pattern()
            self._expect(TK.KW_IN)
            # Stop before the comprehension's trailing `: ...` filter.
            iter_  = self._prec(0)
            filter = None
            if self._eat(TK.COLON):
                filter = self._parse_expr()
            end_tok = self._expect(TK.RBRACKET)
            return VecComp(
                expr=first,
                pattern=pat,
                iter=iter_,
                filter=filter,
                span=self._span_from_tokens(start_tok, end_tok),
            )
        # literal: vec[a, b, c] or vec[a, b, c,]  (trailing comma ok)
        elems = [first]
        while self._eat(TK.COMMA):
            if self._check(TK.RBRACKET): break   # trailing comma
            elems.append(self._parse_expr())
        end_tok = self._expect(TK.RBRACKET)
        return VecLit(elems, span=self._span_from_tokens(start_tok, end_tok))

    def _parse_tuple_lit_body(self, dot_tok: Token, lbrace_tok: Token) -> TupleLit:
        self._eat_newlines()
        fields = []
        while not self._check(TK.RBRACE, TK.EOF):
            if fields: self._expect(TK.COMMA)
            self._eat_newlines()
            if self._check(TK.RBRACE): break   # trailing comma
            fname = None
            if self._check(TK.IDENT) and self._peek(1).kind == TK.COLON:
                fname = self._advance().lexeme
                self._advance()
            val = self._parse_expr()
            fields.append((fname, val))
            self._eat_newlines()
        end_tok = self._expect(TK.RBRACE)
        return TupleLit(fields, span=self._span_from_tokens(dot_tok, end_tok))

    # ── string interpolation ─────────────────────────────────

    def _parse_interpolation(self, raw: str) -> List[Union[str, Expr]]:
        """Parse {expr} and {expr:fmt} segments from a string."""
        segments: List[Union[str, Expr]] = []
        i = 0
        buf = ""
        while i < len(raw):
            if raw[i] == "{" and i + 1 < len(raw) and raw[i+1] != "{":
                if buf: segments.append(buf); buf = ""
                # find matching }
                depth = 1; j = i + 1
                while j < len(raw) and depth > 0:
                    if raw[j] == "{": depth += 1
                    elif raw[j] == "}": depth -= 1
                    j += 1
                inner = raw[i+1:j-1]
                # strip format specifier :fmt
                colon = inner.rfind(":")
                expr_src = inner[:colon] if colon != -1 else inner
                fmt      = inner[colon+1:] if colon != -1 else None
                try:
                    toks = Tokenizer(expr_src).tokenize()
                    expr = Parser(toks).parse_expr_only()
                    if fmt:
                        expr = CallExpr(
                            callee=Ident("__format"),
                            args=[Arg(None, expr),
                                  Arg("fmt", StringLit(fmt, [fmt]))]
                        )
                    segments.append(expr)
                except Exception:
                    segments.append("{" + inner + "}")
                i = j
            elif raw[i:i+2] == "{{":
                buf += "{"; i += 2
            elif raw[i:i+2] == "}}":
                buf += "}"; i += 2
            else:
                buf += raw[i]; i += 1
        if buf: segments.append(buf)
        return segments

    def parse_expr_only(self) -> Expr:
        """Parse a single expression — used for string interpolation."""
        return self._parse_expr()


# ══════════════════════════════════════════════════════════════
# Convenience
# ══════════════════════════════════════════════════════════════
def parse(source: str) -> Program:
    tokens = Tokenizer(source).tokenize()
    return Parser(tokens).parse()


# ══════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    tests = [
        # let bindings
        ("let x = 42;",                    "LetStmt"),
        ("let var x: i64 = 0;",            "LetStmt"),

        # functions
        ("fun add(a: i64, b: i64) i64 { return a + b; }", "FunDecl"),

        # structs
        ("struct Vec2 { x: f64, y: f64, }", "StructDecl"),

        # unions
        ("union Shape { Circle(f64), Point, }", "UnionDecl"),

        # interfaces
        ("interface Add[T] { fun add(self: @this, other: T) Self; }", "InterfaceDecl"),

        # def
        ("def Add for Vec2 { fun add(self: Self, other: Vec2) Vec2 { return .{0.0, 0.0}; } }", "DefDecl"),

        # optional unwrap
        ("if x |v| { v } else { 0 }", "IfUnwrap"),

        # for range with filter
        ("for i = 0...10 : i > 5 { }", "ForRangeStmt"),

        # for iter with filter
        ("for *p in particles : !p.dead { }", "ForIterStmt"),
        ("for x in items, println(x)", "ForIterStmt"),

        # match
        ("match x { 1 => { }, _ => { } }", "MatchExpr"),

        # vec comprehension
        ("vec[x * 2 for x in data : x > 0]", "VecComp"),

        # with expression
        ("with arena : .free { 42 } handle |e| { 0 }", "WithExpr"),

        # type alias
        ("type Vec2 = mat[f32; 2, 1];", "TypeAlias"),

        # pkg + import
        ("pkg math.linalg", "PkgDecl"),
        ("import std.math", "ImportDecl"),
        ("from std.math import sin, cos", "FromImportDecl"),

        # string interpolation
        ('"hello {name}"', "StringLit"),

        # closure
        ("fun(x: f64) f64 { return x ^ 2; }", "Closure"),

        # broadcast ops
        ("a .* b .+ c", "BinExpr"),

        # comptime
        ("comptime size_of(Vec3)", "ComptimeExpr"),

        # directives
        ("@typeof(x)", "CallExpr"),
    ]

    # expression-level tests (not declarations)
    EXPR_TESTS = {
        "if x |v| { v } else { 0 }",
        "match x { 1 => { }, _ => { } }",
        "vec[x * 2 for x in data : x > 0]",
        "with arena : .free { 42 } handle |e| { 0 }",
        '"hello {name}"',
        "fun(x: f64) f64 { return x ^ 2; }",
        "a .* b .+ c",
        "comptime size_of(Vec3)",
        "@typeof(x)",
    }
    # statement-level tests (wrapped in a fun body)
    STMT_TESTS = {
        "for i = 0...10 : i > 5 { }": "ForRangeStmt",
        "for *p in particles : !p.dead { }": "ForIterStmt",
        "for x in items, println(x)": "ForIterStmt",
    }

    passed = 0
    for src, expected_type in tests:
        try:
            if src in STMT_TESTS:
                wrapped = f"fun __test() void {{ {src} }}"
                prog = parse(wrapped)
                fun_decl = prog.decls[0]
                node = fun_decl.body.stmts[0]
            elif src in EXPR_TESTS:
                toks = Tokenizer(src).tokenize()
                node = Parser(toks).parse_expr_only()
            else:
                prog = parse(src)
                if prog.decls:      node = prog.decls[0]
                elif prog.pkg:      node = prog.pkg
                else:               node = None

            got = type(node).__name__ if node else "None"
            if got == expected_type:
                passed += 1
            else:
                print(f"FAIL: {src!r}")
                print(f"  expected: {expected_type}, got: {got}")
        except Exception as e:
            print(f"FAIL: {src!r}")
            print(f"  error: {e}")

    print(f"\n{passed}/{len(tests)} tests passed")
