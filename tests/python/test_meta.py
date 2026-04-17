"""Tests for Mesa editor metadata and completions."""
from __future__ import annotations

from src.ast import SourcePos
from src.meta import build_document_meta


def _strip_marker(source: str, marker: str = "<<CURSOR>>") -> tuple[str, SourcePos]:
    idx = source.index(marker)
    prefix = source[:idx]
    line = prefix.count("\n") + 1
    col = len(prefix.rsplit("\n", 1)[-1]) + 1
    return source.replace(marker, "", 1), SourcePos(line=line, col=col)


def _labels(items):
    return [item.label for item in items]


def _semantic_pairs(meta):
    lexemes = {
        (tok.line - 1, tok.col - 1): tok.lexeme
        for tok in meta.tokens
        if tok.kind.name not in {"NEWLINE", "EOF"}
    }
    return {(lexemes[(item.line, item.start)], item.token_type) for item in meta.semantic_tokens()}


def test_build_document_meta_valid_source():
    meta = build_document_meta("""
fun add(a: i64, b: i64) i64 {
    let sum = a + b
    return sum
}
""".strip())

    assert meta.parse_succeeded
    assert meta.program is not None
    assert meta.env is not None
    assert meta.root_scope.symbols


def test_build_document_meta_parse_error_collects_diagnostic():
    meta = build_document_meta("fun broken( i64 {")

    assert not meta.parse_succeeded
    assert meta.program is None
    assert meta.diagnostics
    assert "expected IDENT" in meta.diagnostics[0].message


def test_build_document_meta_type_error_collects_diagnostic():
    meta = build_document_meta("""
fun bad() void {
    let x: i64 = true
}
""".strip())

    assert meta.parse_succeeded
    assert meta.env is not None
    assert meta.diagnostics
    assert "expected i64" in meta.diagnostics[0].message


def test_visible_symbols_respect_block_scope_and_shadowing():
    source, pos = _strip_marker("""
fun demo(a: i64) i64 {
    let outer = a
    if true {
        let outer = 42
        let inner = outer
        <<CURSOR>>
    }
    return outer
}
""".strip())

    meta = build_document_meta(source)
    visible = {sym.name: sym for sym in meta.visible_symbols_at(pos)}

    assert "a" in visible
    assert "outer" in visible
    assert "inner" in visible
    assert repr(visible["outer"].type_) == "i64"


def test_completion_includes_keywords_and_locals():
    source, pos = _strip_marker("""
fun demo(a: i64) i64 {
    let local = a
    lo<<CURSOR>>
}
""".strip())

    meta = build_document_meta(source)
    items = meta.complete(pos.line - 1, pos.col - 1)
    labels = _labels(items)

    assert "local" in labels
    assert "fun" in labels


def test_member_completion_on_valid_document():
    source, pos = _strip_marker("""
struct Vec2 {
    x: f64,
    y: f64,

    fun len(self: Vec2) f64 {
        self.x
    }
}

fun demo(v: Vec2) f64 {
    v.l<<CURSOR>>
}
""".strip())

    meta = build_document_meta(source)
    items = meta.complete(pos.line - 1, pos.col - 1)
    labels = _labels(items)

    assert "len" in labels
    assert "x" in labels
    assert "y" in labels


def test_member_completion_uses_last_good_meta_after_parse_break():
    last_good_source, _ = _strip_marker("""
struct Vec2 {
    x: f64,
    y: f64,

    fun len(self: Vec2) f64 {
        self.x
    }
}

fun demo(v: Vec2) f64 {
    v.l<<CURSOR>>
}
""".strip())
    broken_source, pos = _strip_marker("""
struct Vec2 {
    x: f64,
    y: f64,

    fun len(self: Vec2) f64 {
        self.x
    }
}

fun demo(v: Vec2) f64 {
    v.<<CURSOR>>
}
""".strip())

    last_good = build_document_meta(last_good_source)
    broken = build_document_meta(broken_source)
    items = broken.complete(pos.line - 1, pos.col - 1, fallback=last_good)
    labels = _labels(items)

    assert not broken.parse_succeeded
    assert "len" in labels
    assert "x" in labels


def test_qualified_union_variant_access_is_allowed():
    meta = build_document_meta("""
union Option { first, second }
fun main() void {
    let x = Option.first
    let y: Option = .first
}
""".strip())

    assert meta.parse_succeeded
    assert meta.typecheck_succeeded
    assert meta.diagnostics == []


def test_semantic_tokens_cover_core_categories():
    meta = build_document_meta("""
error Problem { broken }
union Option { first, second }
struct Pair {
    first: i64,
    fun pick(self: Pair) i64 {
        self.first
    }
}
fun takes(v: vec[i64], f: fun() void) Option {
    let p: Pair = .{first: 1}
    let x = Option.first
    let y = p.pick()
    let z = p.first
    let fun_ref = f
    x
}
fun main() void {
    let noop = takes
    let copy = noop
}
""".strip())

    pairs = _semantic_pairs(meta)

    assert ("Option", "type") in pairs
    assert ("Pair", "type") in pairs
    assert ("void", "type") in pairs
    assert ("vec", "type") in pairs
    assert ("pick", "function") in pairs
    assert ("first", "property") in pairs
    assert ("broken", "enumMember") in pairs or ("first", "enumMember") in pairs
    assert ("noop", "function") in pairs
    assert ("v", "parameter") in pairs
    assert ("f", "parameter") in pairs


def test_qualified_union_variant_with_payload_is_allowed():
    meta = build_document_meta("""
union Option { first(str), second }
fun main() void {
    let x = Option.first("hello")
}
""".strip())

    assert meta.parse_succeeded
    assert meta.typecheck_succeeded


def test_semantic_tokens_cover_user_defined_return_types():
    meta = build_document_meta("""
error ParseError { bad }
struct Foo { value: i64 }
fun make() Foo {
    .{value: 1}
}
fun parse() ParseError!Foo {
    .bad
}
""".strip())

    pairs = _semantic_pairs(meta)

    assert ("Foo", "type") in pairs
    assert ("ParseError", "type") in pairs


def test_semantic_tokens_cover_allocator_types_and_methods():
    meta = build_document_meta("""
import mem

fun main() void {
    let arena: mem.ArenaAllocator = mem.ArenaAllocator.init(mem.PageBuffer.init(1024))
    let resetter = arena.reset
    arena.reset()
}
""".strip(), source_path="/Users/oppenheimer/mesa_MVP/mesa2/examples/defer_in_with.mesa")

    pairs = _semantic_pairs(meta)

    assert ("ArenaAllocator", "property") in pairs
    assert ("init", "function") in pairs
    assert ("reset", "property") in pairs


def test_semantic_tokens_cover_method_references_and_anonymous_struct_fields():
    meta = build_document_meta("""
struct Pair {
    first: i64,

    fun pick(self: Pair) i64 {
        self.first
    }
}

fun main() void {
    let p: Pair = .{first: 1}
    let f = p.pick
}
""".strip())

    pairs = _semantic_pairs(meta)

    assert ("first", "property") in pairs
    assert ("pick", "function") in pairs


def test_semantic_tokens_cover_with_cleanup_function_refs():
    meta = build_document_meta("""
fun main() void {
    let dbg: DebugAllocator = DebugAllocator(ArenaAllocator(1024))
    with dbg : .free {
        42
    }
}
""".strip())

    pairs = _semantic_pairs(meta)

    assert ("free", "function") in pairs


def test_semantic_tokens_cover_def_decl_interfaces():
    meta = build_document_meta("""
interface Add {
    fun add(self: Self, other: Self) Self;
}

interface Show {
    fun show(self: Self) str;
}

struct Vec2 {
    x: f64,
}

def Add, Show for Vec2 {
    fun add(self: Self, other: Self) Self {
        self
    }

    fun show(self: Self) str {
        "Vec2"
    }
}
""".strip())

    pairs = _semantic_pairs(meta)

    assert ("Add", "type") in pairs
    assert ("Show", "type") in pairs
    assert ("Vec2", "type") in pairs


def test_semantic_tokens_cover_type_parameters_and_modules():
    meta = build_document_meta("""
pkg std.math
import io as io

fun identity[T](x: T) T {
    x
}
""".strip())

    pairs = _semantic_pairs(meta)

    assert ("std", "namespace") in pairs
    assert ("math", "namespace") in pairs
    assert ("io", "namespace") in pairs
    assert ("T", "typeParameter") in pairs
