"""
Mesa tokenizer.

Token kinds reflect the full language design:
  - Keywords from tokenizer.zig plus additions from design doc
  - Operators including broadcast (.+, .*, etc) and +- for uncertainty
  - Multiline strings (\\) and multiline comments (\\)
  - String interpolation handled at parse time
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional


# ══════════════════════════════════════════════════════════════
# Token kinds
# ══════════════════════════════════════════════════════════════
class TK(Enum):
    # ── Literals ──────────────────────────────────────────────
    INT        = auto()
    FLOAT      = auto()
    STRING     = auto()      # "..." — may contain {interpolations}
    MULTILINE  = auto()      # \\ prefixed lines joined
    TRUE       = auto()
    FALSE      = auto()
    NONE       = auto()

    # ── Identifier ────────────────────────────────────────────
    IDENT      = auto()

    # ── Keywords ──────────────────────────────────────────────
    # declarations
    KW_FUN       = auto()
    KW_LET       = auto()
    KW_VAR       = auto()      # only valid after let: `let var x`
    KW_STRUCT    = auto()
    KW_UNION     = auto()
    KW_INTERFACE = auto()
    KW_DEF       = auto()
    KW_TYPE      = auto()      # type alias: type Vec2 = mat[f32; 2, 1]
    KW_PKG       = auto()
    KW_PUB       = auto()
    KW_EXPORT    = auto()
    KW_OPAQUE    = auto()
    KW_COMPTIME  = auto()
    KW_INLINE    = auto()
    KW_EXTERN    = auto()

    # control flow
    KW_IF        = auto()
    KW_ANY       = auto()   # any — dynamic interface existential
    KW_ERROR     = auto()   # error — error set declaration
    KW_HANDLE    = auto()   # handle — function-level error handler
    KW_ELSE      = auto()
    KW_FOR       = auto()
    KW_WHILE     = auto()
    KW_MATCH     = auto()
    KW_RETURN    = auto()
    KW_BREAK     = auto()
    KW_CONTINUE  = auto()
    KW_IN        = auto()
    KW_WITH      = auto()
    KW_WHEN      = auto()      # reserved for concurrency v2

    # error handling
    KW_TRY       = auto()
    KW_CATCH     = auto()
    KW_ORELSE    = auto()
    KW_DEFER     = auto()
    KW_ESC       = auto()

    # memory

    # logical
    KW_AND       = auto()
    KW_OR        = auto()

    # imports
    KW_IMPORT    = auto()
    KW_FROM      = auto()
    KW_AS        = auto()

    # testing
    KW_TEST      = auto()

    # ── Primitive types ───────────────────────────────────────
    TY_I8    = auto(); TY_I16 = auto(); TY_I32 = auto(); TY_I64 = auto()
    TY_U8    = auto(); TY_U16 = auto(); TY_U32 = auto(); TY_U64 = auto()
    TY_F32   = auto(); TY_F64 = auto()
    TY_BOOL  = auto()
    TY_STR    = auto()
    TY_VOID  = auto()

    # ── Arithmetic operators ──────────────────────────────────
    PLUS       = auto()   # +
    MINUS      = auto()   # -
    STAR       = auto()   # *
    SLASH      = auto()   # /
    PERCENT    = auto()   # %
    CARET      = auto()   # ^ exponentiation
    PLUS_MINUS = auto()   # +- uncertainty

    # ── Compound assignment ───────────────────────────────────
    PLUS_EQ    = auto()   # +=
    MINUS_EQ   = auto()   # -=
    STAR_EQ    = auto()   # *=
    SLASH_EQ   = auto()   # /=
    PERCENT_EQ = auto()   # %=
    CARET_EQ   = auto()   # ^=

    # ── Broadcast operators (.op) ─────────────────────────────
    DOT_PLUS      = auto()   # .+
    DOT_MINUS     = auto()   # .-
    DOT_STAR      = auto()   # .*
    DOT_SLASH     = auto()   # ./
    DOT_PERCENT   = auto()   # .%
    DOT_CARET     = auto()   # .^
    DOT_PLUS_EQ   = auto()   # .+=
    DOT_MINUS_EQ  = auto()   # .-=
    DOT_STAR_EQ   = auto()   # .*=
    DOT_SLASH_EQ  = auto()   # ./=
    DOT_PLUS_MINUS = auto()  # .-+- componentwise uncertainty

    # ── Comparison ────────────────────────────────────────────
    EQ_EQ      = auto()   # ==
    BANG_EQ    = auto()   # !=
    LT         = auto()   # <
    GT         = auto()   # >
    LT_EQ      = auto()   # <=
    GT_EQ      = auto()   # >=

    # ── Broadcast comparison ──────────────────────────────────
    DOT_EQ_EQ  = auto()   # .==
    DOT_BANG_EQ = auto()  # .!=
    DOT_LT     = auto()   # .<
    DOT_GT     = auto()   # .>
    DOT_LT_EQ  = auto()   # .<=
    DOT_GT_EQ  = auto()   # .>=

    # ── Other operators ───────────────────────────────────────
    EQ         = auto()   # =
    COLON_EQ   = auto()   # := type binding
    QUESTION_EQ = auto()  # ?= assign if not none
    AMP        = auto()   # &  (address-of)
    BANG       = auto()   # ! error union prefix / logical not
    QUESTION   = auto()   # ? optional type prefix
    AT         = auto()   # @ address-of
    ARROW      = auto()   # ->
    FAT_ARROW  = auto()   # =>
    QUESTION_DOT = auto() # ?. optional chaining

    # ── Punctuation ───────────────────────────────────────────
    DOT        = auto()   # .
    DOT_DOT    = auto()   # ..  exclusive range
    DOT_DOT_DOT = auto()  # ... inclusive range
    COLON      = auto()   # :
    SEMI       = auto()
    NEWLINE    = auto()   # virtual — inserted by tokenizer post-processing   # ;
    COMMA      = auto()   # ,
    PIPE       = auto()   # | binding delimiter
    UNIT       = auto()   # `unit_expr` — unit annotation e.g. `N`, `m/s²`, `?`

    # ── Delimiters ────────────────────────────────────────────
    LPAREN    = auto()   # (
    RPAREN    = auto()   # )
    LBRACE    = auto()   # {
    RBRACE    = auto()   # }
    LBRACKET  = auto()   # [
    RBRACKET  = auto()   # ]
    HASH_LBRACKET = auto()  # #[ attribute start

    # ── Special ───────────────────────────────────────────────
    EOF        = auto()
    INVALID    = auto()


# ── Keyword map ───────────────────────────────────────────────
KEYWORDS: dict[str, TK] = {
    "fun":       TK.KW_FUN,
    "let":       TK.KW_LET,
    "var":       TK.KW_VAR,
    "struct":    TK.KW_STRUCT,
    "union":     TK.KW_UNION,
    "interface": TK.KW_INTERFACE,
    "def":       TK.KW_DEF,
    "type":      TK.KW_TYPE,
    "pkg":       TK.KW_PKG,
    "pub":       TK.KW_PUB,
    "export":    TK.KW_EXPORT,
    "opaque":    TK.KW_OPAQUE,
    "comptime":  TK.KW_COMPTIME,
    "inline":    TK.KW_INLINE,
    "extern":    TK.KW_EXTERN,
    "if":        TK.KW_IF,
    "else":      TK.KW_ELSE,
    "for":       TK.KW_FOR,
    "error":     TK.KW_ERROR,
    "any":       TK.KW_ANY,
    "while":     TK.KW_WHILE,
    "match":     TK.KW_MATCH,
    "return":    TK.KW_RETURN,
    "break":     TK.KW_BREAK,
    "continue":  TK.KW_CONTINUE,
    "in":        TK.KW_IN,
    "with":      TK.KW_WITH,
    "when":      TK.KW_WHEN,
    "try":       TK.KW_TRY,
    "catch":     TK.KW_CATCH,
    "handle":    TK.KW_HANDLE,
    "orelse":    TK.KW_ORELSE,
    "defer":     TK.KW_DEFER,
    "esc":       TK.KW_ESC,
    "and":       TK.KW_AND,
    "or":        TK.KW_OR,
    "import":    TK.KW_IMPORT,
    "from":      TK.KW_FROM,
    "as":        TK.KW_AS,
    "test":      TK.KW_TEST,
    "true":      TK.TRUE,
    "false":     TK.FALSE,
    "none":      TK.NONE,
    # primitive types
    "i8":     TK.TY_I8,   "i16":    TK.TY_I16,
    "i32":    TK.TY_I32,  "i64":    TK.TY_I64,
    "u8":     TK.TY_U8,   "u16":    TK.TY_U16,
    "u32":    TK.TY_U32,  "u64":    TK.TY_U64,
    "f32":    TK.TY_F32,  "f64":    TK.TY_F64,
    "bool":   TK.TY_BOOL,
    "str":    TK.TY_STR,
    "void":   TK.TY_VOID,
}


# ══════════════════════════════════════════════════════════════
# Token
# ══════════════════════════════════════════════════════════════
@dataclass
class Token:
    kind:   TK
    lexeme: str
    line:   int
    col:    int

    def __repr__(self) -> str:
        return f"Token({self.kind.name}, {self.lexeme!r}, {self.line}:{self.col})"


# ══════════════════════════════════════════════════════════════
# Error
# ══════════════════════════════════════════════════════════════
class TokenizeError(Exception):
    def __init__(self, msg: str, line: int, col: int):
        super().__init__(f"[{line}:{col}] {msg}")
        self.line = line
        self.col  = col


# ══════════════════════════════════════════════════════════════
# Tokenizer
# ══════════════════════════════════════════════════════════════
class Tokenizer:
    def __init__(self, source: str):
        self.source = source
        self.pos    = 0
        self.line   = 1
        self.col    = 1

    # Tokens that can END a statement (so a following newline = terminator)
    _STMT_ENDERS = {
        TK.IDENT, TK.INT, TK.FLOAT, TK.STRING, TK.MULTILINE,
        TK.TRUE, TK.FALSE, TK.NONE,
        TK.RPAREN, TK.RBRACKET, TK.RBRACE,
        TK.KW_BREAK, TK.KW_CONTINUE, TK.KW_RETURN,
    }

    # Tokens that can START a continuation line (so preceding newline = ignored)
    _CONTINUATION_STARTERS = {
        TK.PLUS, TK.MINUS, TK.STAR, TK.SLASH, TK.PERCENT, TK.CARET,
        TK.EQ_EQ, TK.BANG_EQ, TK.LT, TK.GT, TK.LT_EQ, TK.GT_EQ,
        TK.KW_AND, TK.KW_OR, TK.KW_ORELSE,
        TK.DOT, TK.QUESTION_DOT,
        TK.DOT_PLUS, TK.DOT_MINUS, TK.DOT_STAR, TK.DOT_SLASH,
        TK.DOT_EQ_EQ, TK.DOT_BANG_EQ, TK.DOT_LT, TK.DOT_GT,
    }

    def _starts_continuation(self, raw: List[Token], next_index: int) -> bool:
        """Whether raw[next_index] continues the previous line.

        A leading `.ident` continues a daisy chain across newlines, but `.{`
        starts a fresh expression and should terminate the previous statement.
        """
        next_tok = raw[next_index]
        if next_tok.kind == TK.DOT:
            after = raw[next_index + 1] if next_index + 1 < len(raw) else None
            return after is not None and after.kind == TK.IDENT
        return next_tok.kind in self._CONTINUATION_STARTERS

    def tokenize(self) -> List[Token]:
        """Tokenize source, inserting virtual NEWLINE tokens as statement separators."""
        # Pass 1: collect raw tokens, tracking which ones had a newline before them
        raw: List[Token] = []
        newline_before: List[bool] = []
        prev_end_line = 1

        while True:
            # Skip whitespace but track if we crossed a newline
            start_line = self.line
            while (c := self._peek()) is not None and c in " \t\r\n":
                self._advance()
            crossed_newline = (self.line > start_line) or (self.line > prev_end_line)

            t = self._next()
            raw.append(t)
            newline_before.append(crossed_newline)
            prev_end_line = t.line
            if t.kind == TK.EOF:
                break

        # Pass 2: insert NEWLINE tokens at statement boundaries
        # Rules:
        #   - NEWLINE inserted between tokens[i] and tokens[i+1] if:
        #     (a) there was a real newline between them
        #     (b) tokens[i] is a statement ender
        #     (c) tokens[i+1] is NOT a continuation starter
        #     (d) nesting depth is 0
        tokens: List[Token] = []
        nesting: List[str] = []   # tracks paren/bracket nesting and value-brace nesting only

        for i, tok in enumerate(raw):
            tokens.append(tok)

            prev_tok = raw[i - 1] if i > 0 else None
            if tok.kind == TK.LPAREN:
                nesting.append("(")
            elif tok.kind == TK.LBRACKET:
                nesting.append("[")
            elif tok.kind == TK.LBRACE:
                # Only value braces (currently `.{...}`) suppress newline insertion.
                if prev_tok is not None and prev_tok.kind == TK.DOT:
                    nesting.append("{value}")
            elif tok.kind == TK.RPAREN:
                if nesting and nesting[-1] == "(":
                    nesting.pop()
            elif tok.kind == TK.RBRACKET:
                if nesting and nesting[-1] == "[":
                    nesting.pop()
            elif tok.kind == TK.RBRACE:
                if nesting and nesting[-1] == "{value}":
                    nesting.pop()

            if tok.kind == TK.EOF:
                break

            next_tok = raw[i + 1] if i + 1 < len(raw) else None
            if next_tok is None:
                continue

            if (not nesting
                    and newline_before[i + 1]
                    and tok.kind in self._STMT_ENDERS
                    and not self._starts_continuation(raw, i + 1)):
                # Insert virtual NEWLINE token
                tokens.append(Token(TK.NEWLINE, "\n", tok.line, tok.col))

        return tokens

    # ── navigation ───────────────────────────────────────────

    def _peek(self, offset: int = 0) -> Optional[str]:
        i = self.pos + offset
        return self.source[i] if i < len(self.source) else None

    def _advance(self) -> str:
        c = self.source[self.pos]
        self.pos += 1
        if c == "\n": self.line += 1; self.col = 1
        else:         self.col  += 1
        return c

    def _make(self, kind: TK, start: int, start_col: int,
              start_line: int) -> Token:
        return Token(kind, self.source[start:self.pos],
                     start_line, start_col)

    def _skip_whitespace(self):
        """Skip whitespace including newlines (NEWLINE tokens are inserted in pass 2)."""
        while (c := self._peek()) is not None:
            if c in " \t\r\n":
                self._advance()
            else:
                break

    # ── core ─────────────────────────────────────────────────

    def _next(self) -> Token:
        self._skip_whitespace()

        if self.pos >= len(self.source):
            return Token(TK.EOF, "", self.line, self.col)

        start      = self.pos
        start_col  = self.col
        start_line = self.line
        c          = self._advance()

        def make(kind: TK) -> Token:
            return self._make(kind, start, start_col, start_line)

        # ── multiline string / comment  \\  ─────────────────
        if c == "\\" and self._peek() == "\\":
            _ = self._advance()   # eat second \
            # collect all consecutive \\ lines
            lines = []
            while True:
                # read to end of line
                line_start = self.pos
                while self._peek() and self._peek() != "\n":
                    self._advance()
                lines.append(self.source[line_start:self.pos])
                if self._peek() == "\n":
                    self._advance()
                # check if next non-whitespace line starts with \\
                saved_pos  = self.pos
                saved_line = self.line
                saved_col  = self.col
                while self._peek() in (" ", "\t"):
                    self._advance()
                if self._peek() == "\\" and self._peek(1) == "\\":
                    self._advance(); self._advance()  # eat \\
                else:
                    # restore position — not a continuation
                    self.pos  = saved_pos
                    self.line = saved_line
                    self.col  = saved_col
                    break
            text = "\n".join(lines)
            return Token(TK.MULTILINE, text, start_line, start_col)

        # ── single line comment  // ──────────────────────────
        if c == "/" and self._peek() == "/":
            while self._peek() and self._peek() != "\n":
                self._advance()
            return self._next()   # skip comment, get next token

        # ── #[ attribute ─────────────────────────────────────
        if c == "#" and self._peek() == "[":
            self._advance()
            return make(TK.HASH_LBRACKET)

        # ── string literal ───────────────────────────────────
        if c == '"':
            while True:
                sc = self._peek()
                if sc is None:
                    raise TokenizeError("Unterminated string",
                                        self.line, self.col)
                if sc == '"':  self._advance(); break
                if sc == '\\':
                    self._advance()   # eat backslash
                    if self._peek() is None:
                        raise TokenizeError("Unterminated string escape",
                                            self.line, self.col)
                self._advance()
            return make(TK.STRING)

        # ── numeric literal ──────────────────────────────────
        if c.isdigit():
            is_float = False
            # hex literal: 0x[0-9a-fA-F_]+
            if c == "0" and self._peek() in ("x", "X"):
                self._advance()   # eat x
                if not (self._peek() or "").replace("_","").translate(
                        str.maketrans("","","0123456789abcdefABCDEF")) == "":
                    pass  # at least one hex digit exists
                while (nc := self._peek()) and (nc in "0123456789abcdefABCDEF_"):
                    self._advance()
                return make(TK.INT)
            # binary literal: 0b[01_]+
            if c == "0" and self._peek() in ("b", "B"):
                self._advance()   # eat b
                while (nc := self._peek()) and (nc in "01_"):
                    self._advance()
                return make(TK.INT)
            # octal literal: 0o[0-7_]+
            if c == "0" and self._peek() in ("o", "O"):
                self._advance()   # eat o
                while (nc := self._peek()) and (nc in "01234567_"):
                    self._advance()
                return make(TK.INT)
            while (nc := self._peek()) and (nc.isdigit() or nc == "_"):
                self._advance()
            # decimal — only if followed by digit, not ..
            if (self._peek() == "." and
                    self._peek(1) not in (".", None) and
                    (self._peek(1) or "").isdigit()):
                is_float = True
                self._advance()
                while (nc := self._peek()) and nc.isdigit():
                    self._advance()
            # exponent — only if followed by digit or sign+digit
            if self._peek() in ("e", "E"):
                p1 = self._peek(1)
                p2 = self._peek(2)
                has_exp = (p1 is not None and p1.isdigit()) or                           (p1 in ("+", "-") and p2 is not None and p2.isdigit())
                if has_exp:
                    is_float = True
                    self._advance()   # eat e/E
                    if self._peek() in ("+", "-"): self._advance()
                    while (nc := self._peek()) and nc.isdigit():
                        self._advance()
            return make(TK.FLOAT if is_float else TK.INT)

        # ── identifier / keyword ─────────────────────────────
        if c.isalpha() or c == "_":
            while (nc := self._peek()) and (nc.isalnum() or nc == "_"):
                self._advance()
            lexeme = self.source[start:self.pos]
            return Token(KEYWORDS.get(lexeme, TK.IDENT),
                         lexeme, start_line, start_col)

        # ── unit expression  `N`, `m/s²`, `kg*m/s²`, `?`, `1` ──
        if c == "`":
            unit_start = self.pos   # position after opening backtick
            # Scan until closing backtick — unit exprs may contain:
            # letters, digits, *, /, ^, superscript chars, -, +, (, ), ., ?, space
            while self.pos < len(self.source) and self.source[self.pos] != "`":
                self.pos += 1
            if self.pos >= len(self.source):
                raise TokenizeError(
                    f"[{start_line}:{start_col}] Unterminated unit expression"
                )
            unit_expr = self.source[unit_start:self.pos]
            self.pos += 1  # eat closing backtick
            return Token(TK.UNIT, unit_expr.strip(), start_line, start_col)

        # ── dot family  .  ..  ...  .op  ?. ─────────────────
        if c == ".":
            if self._peek() == ".":
                self._advance()
                if self._peek() == ".":
                    self._advance()
                    return make(TK.DOT_DOT_DOT)
                return make(TK.DOT_DOT)
            # broadcast operators
            nc = self._peek()
            if nc == "+":
                self._advance()
                if self._peek() == "-":  # .+-
                    self._advance()
                    return make(TK.DOT_PLUS_MINUS)
                if self._peek() == "=":  # .+=
                    self._advance()
                    return make(TK.DOT_PLUS_EQ)
                return make(TK.DOT_PLUS)
            if nc == "-":
                self._advance()
                if self._peek() == "=":
                    self._advance(); return make(TK.DOT_MINUS_EQ)
                return make(TK.DOT_MINUS)
            if nc == "*":
                self._advance()
                if self._peek() == "=":
                    self._advance(); return make(TK.DOT_STAR_EQ)
                return make(TK.DOT_STAR)
            if nc == "/":
                self._advance()
                if self._peek() == "=":
                    self._advance(); return make(TK.DOT_SLASH_EQ)
                return make(TK.DOT_SLASH)
            if nc == "%":
                self._advance(); return make(TK.DOT_PERCENT)
            if nc == "^":
                self._advance(); return make(TK.DOT_CARET)
            if nc == "=":
                self._advance()
                if self._peek() == "=":
                    self._advance(); return make(TK.DOT_EQ_EQ)
                # .= is not a valid token — return DOT and let = be next
                self.pos -= 1; self.col -= 1
                return make(TK.DOT)
            if nc == "!":
                self._advance()
                if self._peek() == "=":
                    self._advance(); return make(TK.DOT_BANG_EQ)
                self.pos -= 1; self.col -= 1
                return make(TK.DOT)
            if nc == "<":
                self._advance()
                if self._peek() == "=":
                    self._advance(); return make(TK.DOT_LT_EQ)
                return make(TK.DOT_LT)
            if nc == ">":
                self._advance()
                if self._peek() == "=":
                    self._advance(); return make(TK.DOT_GT_EQ)
                return make(TK.DOT_GT)
            return make(TK.DOT)

        # ── ? family  ?  ?=  ?. ──────────────────────────────
        if c == "?":
            if self._peek() == "=":  self._advance(); return make(TK.QUESTION_EQ)
            if self._peek() == ".":  self._advance(); return make(TK.QUESTION_DOT)
            return make(TK.QUESTION)

        # ── +- uncertainty ────────────────────────────────────
        if c == "+" and self._peek() == "-":
            self._advance()
            return make(TK.PLUS_MINUS)

        # ── + ────────────────────────────────────────────────
        if c == "+":
            if self._peek() == "=": self._advance(); return make(TK.PLUS_EQ)
            return make(TK.PLUS)

        # ── - -> ─────────────────────────────────────────────
        if c == "-":
            if self._peek() == ">": self._advance(); return make(TK.ARROW)
            if self._peek() == "=": self._advance(); return make(TK.MINUS_EQ)
            return make(TK.MINUS)

        # ── * ────────────────────────────────────────────────
        if c == "*":
            if self._peek() == "=": self._advance(); return make(TK.STAR_EQ)
            return make(TK.STAR)

        # ── / ────────────────────────────────────────────────
        if c == "/":
            if self._peek() == "=": self._advance(); return make(TK.SLASH_EQ)
            return make(TK.SLASH)

        # ── % ────────────────────────────────────────────────
        if c == "%":
            if self._peek() == "=": self._advance(); return make(TK.PERCENT_EQ)
            return make(TK.PERCENT)

        # ── ^ ────────────────────────────────────────────────
        if c == "^":
            if self._peek() == "=": self._advance(); return make(TK.CARET_EQ)
            return make(TK.CARET)

        # ── = == => ──────────────────────────────────────────
        if c == "=":
            if self._peek() == "=": self._advance(); return make(TK.EQ_EQ)
            if self._peek() == ">": self._advance(); return make(TK.FAT_ARROW)
            return make(TK.EQ)

        # ── ! != ─────────────────────────────────────────────
        if c == "&":
            return make(TK.AMP)

        if c == "!":
            if self._peek() == "=": self._advance(); return make(TK.BANG_EQ)
            return make(TK.BANG)

        # ── < <= ─────────────────────────────────────────────
        if c == "<":
            if self._peek() == "=": self._advance(); return make(TK.LT_EQ)
            return make(TK.LT)

        # ── > >= ─────────────────────────────────────────────
        if c == ">":
            if self._peek() == "=": self._advance(); return make(TK.GT_EQ)
            return make(TK.GT)

        # ── @ ────────────────────────────────────────────────
        if c == "@": return make(TK.AT)

        # ── & ────────────────────────────────────────────────

        # ── : and :=  ────────────────────────────────────────
        if c == ":":
            if self._peek() == "=": self._advance(); return make(TK.COLON_EQ)
            return make(TK.COLON)

        # ── single char ───────────────────────────────────────
        singles = {
            "(": TK.LPAREN,   ")": TK.RPAREN,
            "{": TK.LBRACE,   "}": TK.RBRACE,
            "[": TK.LBRACKET, "]": TK.RBRACKET,
            ";": TK.SEMI,
            ",": TK.COMMA,
            "|": TK.PIPE,
        }
        if c in singles:
            return make(singles[c])

        raise TokenizeError(f"Unexpected character: {c!r}",
                            start_line, start_col)


# ══════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    tests = [
        # basic
        ('let var x: i64 = 42;',
         [TK.KW_LET, TK.KW_VAR, TK.IDENT, TK.COLON, TK.TY_I64,
          TK.EQ, TK.INT, TK.SEMI, TK.EOF]),

        # optional chaining
        ('config?.host orelse "localhost"',
         [TK.IDENT, TK.QUESTION_DOT, TK.IDENT, TK.KW_ORELSE,
          TK.STRING, TK.EOF]),

        # uncertainty
        ('9.8 +- 0.1',
         [TK.FLOAT, TK.PLUS_MINUS, TK.FLOAT, TK.EOF]),

        # broadcast
        ('a .* b .+= c',
         [TK.IDENT, TK.DOT_STAR, TK.IDENT, TK.DOT_PLUS_EQ,
          TK.IDENT, TK.EOF]),

        # ranges
        ('0..10 0...10',
         [TK.INT, TK.DOT_DOT, TK.INT, TK.INT, TK.DOT_DOT_DOT,
          TK.INT, TK.EOF]),

        # attribute
        ('#[simd] struct Vec4',
         [TK.HASH_LBRACKET, TK.IDENT, TK.RBRACKET, TK.KW_STRUCT,
          TK.IDENT, TK.EOF]),

        # exponentiation
        ('x ^ 2 ^= 3',
         [TK.IDENT, TK.CARET, TK.INT, TK.CARET_EQ, TK.INT, TK.EOF]),

        # ?= assign if not none
        ('x ?= expr',
         [TK.IDENT, TK.QUESTION_EQ, TK.IDENT, TK.EOF]),
    ]

    passed = 0
    for src, expected in tests:
        tokens = Tokenizer(src).tokenize()
        kinds  = [t.kind for t in tokens]
        if kinds == expected:
            passed += 1
        else:
            print(f"FAIL: {src!r}")
            print(f"  expected: {[k.name for k in expected]}")
            print(f"  got:      {[k.name for k in kinds]}")

    print(f"\n{passed}/{len(tests)} tests passed")
