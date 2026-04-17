from __future__ import annotations

import unittest

from src.frontend import build_frontend_state
from src.mirgen import emit_mir_for_frontend


def emit_source(source: str) -> str:
    state = build_frontend_state(source)
    if state.diags.has_errors():
        messages = "\n".join(d.message for d in state.diags.all_errors())
        raise AssertionError(messages)
    return emit_mir_for_frontend(state).render()


class MIRGenTests(unittest.TestCase):
    def test_straight_line_function(self):
        source = """
fun add(a: i64, b: i64) i64 {
    let sum = a + b
    sum
}
"""
        text = emit_source(source)
        self.assertIn("@add(%a: int, %b: int) int:", text)
        self.assertIn("%sum = %a + %b", text)
        self.assertIn("return %sum", text)

    def test_if_expression_uses_named_blocks(self):
        source = """
fun max(a: i64, b: i64) i64 {
    if a >= b { a } else { b }
}
"""
        text = emit_source(source)
        self.assertIn("if %a >= %b goto if_true else if_false", text)
        self.assertIn("if_done(%if_value: int):", text)
        self.assertIn("return %if_value", text)

    def test_if_statement_merges_reassigned_binding(self):
        source = """
fun choose(a: i64, b: i64) i64 {
    let var best = a
    if b > best {
        best = b
    }
    best
}
"""
        text = emit_source(source)
        self.assertIn("%best = %a", text)
        self.assertIn("if %b > %best goto if_true else if_skip", text)
        self.assertIn("if_done(%best_", text)

    def test_tagged_constructor_and_match(self):
        source = """
union Maybe { Just(i64), Nothing }

fun make(v: i64) Maybe {
    .Just(v)
}

fun from_maybe(m: Maybe, d: i64) i64 {
    match m {
        .Just(v) => { v },
        .Nothing => { d },
    }
}
"""
        text = emit_source(source)
        self.assertIn("%just = make.tagged Maybe.Just(%v)", text)
        self.assertIn("switch.tag %m:", text)
        self.assertIn("Just -> match_just", text)
        self.assertIn("Nothing -> match_nothing", text)
        self.assertIn("read.payload %m as Maybe.Just", text)
        self.assertIn("match_done(%match_value: int):", text)

    def test_try_propagates_in_error_returning_function(self):
        source = """
error ParseError { Empty }

fun parse(text: str) ParseError!i64 {
    if text.len == 0 {
        return .Empty
    }
    return 1
}

fun use(text: str) ParseError!i64 {
    let n = try parse(text)
    return n + 1
}
"""
        text = emit_source(source)
        self.assertIn("@use(%text: str) ParseError!int:", text)
        self.assertIn("switch.result %n:", text)
        self.assertIn("ok -> try_ok(%n.ok)", text)
        self.assertIn("err -> try_err(%n.err)", text)
        self.assertIn("try_err(%err: ParseError):", text)
        self.assertIn("return result.err(%err)", text)
        self.assertIn("return result.ok(%sum)", text)

    def test_try_targets_function_handle(self):
        source = """
error ParseError { Empty }

fun parse(text: str) ParseError!i64 {
    if text.len == 0 {
        return .Empty
    }
    return 1
}

fun parse_or_zero(text: str) i64 {
    let n = try parse(text)
    n
} handle |e| {
    0
}
"""
        text = emit_source(source)
        self.assertIn("@parse_or_zero(%text: str) int:", text)
        self.assertIn("switch.result %n:", text)
        self.assertIn("err -> handle(%n.err)", text)
        self.assertIn("handle(%e: ParseError):", text)
        self.assertIn("return 0", text)

    def test_cleanup_with_emits_region_cleanup(self):
        source = """
interface Allocator {
    fun tag(self: @this) i64
}

struct DummyAlloc {}

def Allocator for DummyAlloc {
fun tag(self: DummyAlloc) i64 {
        0
    }
}

fun bump(x: i64, scratch: DummyAlloc) i64 {
    let y = with scratch : .free {
        x + 1
    }
    y
}
"""
        text = emit_source(source)
        self.assertIn("@bump(%x: int, %scratch: DummyAlloc) int:", text)
        self.assertIn("goto with_body", text)
        self.assertIn("with_merge(", text)
        self.assertIn("with_cleanup(%with_value: int):", text)
        self.assertIn("region.cleanup %scratch free", text)
        self.assertIn("with_done(", text)

    def test_esc_outside_with_uses_parent_region(self):
        source = """
fun lift(n: i64) EscError!i64 {
    esc n
}
"""
        text = emit_source(source)
        self.assertIn("@lift(%n: int) EscError!int:", text)
        self.assertIn("region.promote %n from region.current to region.parent", text)
        self.assertIn("return result.ok(%esc)", text)

    def test_nested_with_escapes_to_outer_allocator_frame(self):
        source = """
interface Allocator {
    fun tag(self: @this) i64
}

struct DummyAlloc {}

def Allocator for DummyAlloc {
fun tag(self: DummyAlloc) i64 {
        0
    }
}

fun copy_up(text: str, out: DummyAlloc, scratch: DummyAlloc) EscError!str {
    return with out {
        with scratch : .free {
            esc text
        }
    }
}
"""
        text = emit_source(source)
        self.assertIn("@copy_up(%text: str, %out: DummyAlloc, %scratch: DummyAlloc) EscError!str:", text)
        self.assertIn("region.promote %text from %scratch to %out", text)
        self.assertIn("region.cleanup %scratch free", text)
        self.assertIn("return %with_value", text)

    def test_with_value_position_can_fully_terminate_through_cleanup(self):
        source = """
error ParseError { Bad }

interface Allocator {
    fun tag(self: @this) i64
}

struct DummyAlloc {}

def Allocator for DummyAlloc {
fun tag(self: DummyAlloc) i64 {
        0
    }
}

fun parse(ok: bool) ParseError!i64 {
    if ok {
        return 7
    }
    return .Bad
}

fun use(ok: bool, scratch: DummyAlloc) ParseError!i64 {
    with scratch : .free {
        let n = try parse(ok)
        return n + 1
    }
}
"""
        text = emit_source(source)
        self.assertIn("@use(%ok: bool, %scratch: DummyAlloc) ParseError!int:", text)
        self.assertIn("switch.result %n:", text)
        self.assertIn("err -> try_err(%n.err)", text)
        self.assertIn("try_err(%err: ParseError):", text)
        self.assertIn("goto with_return(result.err(%err))", text)
        self.assertIn("with_return(%return_value: ParseError!int):", text)
        self.assertIn("region.cleanup %scratch free", text)
        self.assertIn("return %return_value", text)

    def test_with_handle_routes_try_to_local_handle_before_cleanup(self):
        source = """
error ParseError { Bad }

interface Allocator {
    fun tag(self: @this) i64
}

struct DummyAlloc {}

def Allocator for DummyAlloc {
fun tag(self: DummyAlloc) i64 {
        0
    }
}

fun parse(ok: bool) ParseError!i64 {
    if ok {
        return 7
    }
    return .Bad
}

fun parse_or_zero(ok: bool, scratch: DummyAlloc) i64 {
    with scratch : .free {
        let n = try parse(ok)
        n
    } handle |e| {
        0
    }
}
"""
        text = emit_source(source)
        self.assertIn("@parse_or_zero(%ok: bool, %scratch: DummyAlloc) int:", text)
        self.assertIn("switch.result %n:", text)
        self.assertIn("err -> with_handle(%n.err)", text)
        self.assertIn("with_handle(%e: ParseError):", text)
        self.assertIn("goto with_cleanup(", text)
        self.assertIn("region.cleanup %scratch free", text)

    def test_with_handle_without_cleanup_merges_success_and_handle_values(self):
        source = """
error ParseError { Bad }

interface Allocator {
    fun tag(self: @this) i64
}

struct DummyAlloc {}

def Allocator for DummyAlloc {
fun tag(self: DummyAlloc) i64 {
        0
    }
}

fun parse(ok: bool) ParseError!i64 {
    if ok {
        return 7
    }
    return .Bad
}

fun parse_or_zero(ok: bool, scratch: DummyAlloc) i64 {
    with scratch {
        let n = try parse(ok)
        n
    } handle |e| {
        0
    }
}
"""
        text = emit_source(source)
        self.assertIn("@parse_or_zero(%ok: bool, %scratch: DummyAlloc) int:", text)
        self.assertIn("err -> with_handle(%n.err)", text)
        self.assertIn("with_handle(%e: ParseError):", text)
        self.assertIn("with_done(%with_value: int):", text)
        self.assertIn("return %with_value", text)

    def test_while_loop_uses_loop_cond_body_exit_names(self):
        source = """
fun countdown(n: i64) i64 {
    let var x = n
    while x > 0 {
        x -= 1
    }
    x
}
"""
        text = emit_source(source)
        self.assertIn("@countdown(%n: int) int:", text)
        self.assertIn("goto loop_cond(%x)", text)
        self.assertIn("loop_cond(%x: int):", text)
        self.assertIn("if %x > 0 goto loop_body(%x) else loop_exit(%x)", text)
        self.assertIn("loop_body(%x: int):", text)
        self.assertIn("goto loop_cond(%x_2)", text)
        self.assertIn("loop_exit(%x: int):", text)

    def test_break_and_continue_inside_with_cleanup_route_through_cleanup_blocks(self):
        source = """
interface Allocator {
    fun tag(self: @this) i64
}

struct DummyAlloc {}

def Allocator for DummyAlloc {
fun tag(self: DummyAlloc) i64 {
        0
    }
}

fun step(n: i64, scratch: DummyAlloc) i64 {
    let var x = n
    while x > 0 {
        with scratch : .free {
            if x == 2 {
                break;
            }
            x -= 1
            continue;
        }
    }
    x
}
"""
        text = emit_source(source)
        self.assertIn("@step(%n: int, %scratch: DummyAlloc) int:", text)
        self.assertIn("loop_cond(%x: int):", text)
        self.assertIn("loop_body(%x: int):", text)
        self.assertIn("loop_exit(%x: int):", text)
        self.assertIn("break_cleanup(%jump: int):", text)
        self.assertIn("continue_cleanup(%jump_", text)
        self.assertIn("region.cleanup %scratch free", text)
        self.assertIn("goto loop_exit(%jump)", text)
        self.assertIn("goto loop_cond(%jump_", text)

    def test_for_range_uses_loop_cond_body_exit_names(self):
        source = """
fun count_to(n: i64) i64 {
    let var sum = 0
    for i = 0...n {
        sum += i
    }
    sum
}
"""
        text = emit_source(source)
        self.assertIn("@count_to(%n: int) int:", text)
        self.assertIn("goto loop_cond(%sum, %i)", text)
        self.assertIn("loop_cond(%sum: int, %i: int):", text)
        self.assertIn("loop_body(%sum: int, %i: int):", text)
        self.assertIn("loop_step(%sum: int, %i: int):", text)
        self.assertIn("loop_exit(%sum: int):", text)

    def test_for_iter_vec_uses_loop_block_params(self):
        source = """
fun sum_vec(v: vec[i64]) i64 {
    let var sum = 0
    for x in v {
        sum += x
    }
    sum
}
"""
        text = emit_source(source)
        self.assertIn("@sum_vec(%v: vec[int]) int:", text)
        self.assertIn("%iter_len = vec.len %iter", text)
        self.assertIn("goto loop_cond(%sum, %i)", text)
        self.assertIn("loop_cond(%sum: int, %iter_index: int):", text)
        self.assertIn("%elem = vec.get %iter, %iter_index", text)
        self.assertIn("goto loop_step(%sum_2, %iter_index)", text)
        self.assertIn("loop_exit(%sum: int):", text)

    def test_for_iter_ref_supports_pointer_updates(self):
        source = """
fun bump(v: vec[i64]) i64 {
    for *x in v {
        *x += 1
    }
    0
}
"""
        text = emit_source(source)
        self.assertIn("@bump(%v: vec[int]) int:", text)
        self.assertIn("%x = vec.ref %iter, %iter_index", text)
        self.assertIn("%load = load %x", text)
        self.assertIn("store %x, %store_value", text)

    def test_break_value_is_lowered_without_aborting_loop_codegen(self):
        source = """
fun stop() i64 {
    let var x = 0
    while x < 10 {
        break x + 1
    }
    x
}
"""
        text = emit_source(source)
        self.assertIn("@stop() int:", text)
        self.assertIn("%break_value = %x + 1", text)
        self.assertIn("%loop_value = loop.unset -> int", text)
        self.assertIn("goto loop_exit(%x, %break_value)", text)

    def test_defer_runs_on_normal_block_exit(self):
        source = """
fun defer_demo() i64 {
    let var x = 1
    {
        defer { x = 2 }
    }
    x
}
"""
        text = emit_source(source)
        self.assertIn("@defer_demo() int:", text)
        self.assertIn("%x_2 = 2", text)
        self.assertIn("return %x_2", text)

    def test_error_only_defer_runs_on_error_return(self):
        source = """
error ParseError { Bad }

fun parse(ok: bool) ParseError!i64 {
    let var x = 1
    !defer { x = 2 }
    if ok {
        return x
    }
    return .Bad
}
"""
        text = emit_source(source)
        self.assertIn("@parse(%ok: bool) ParseError!int:", text)
        self.assertIn("if_true:", text)
        self.assertIn("return result.ok(%x)", text)
        self.assertIn("goto return_defer(result.err(%Bad))", text)
        self.assertIn("return_defer(%jump: ParseError!int):", text)
        self.assertIn("%x_2 = 2", text)

    def test_error_only_defer_splits_direct_result_return(self):
        source = """
error ParseError { Bad }

fun parse(ok: bool) ParseError!i64 {
    if ok { return 7 }
    return .Bad
}

fun use(ok: bool) ParseError!i64 {
    !defer { let y = 2 }
    return parse(ok)
}
"""
        text = emit_source(source)
        self.assertIn("@use(%ok: bool) ParseError!int:", text)
        self.assertIn("switch.result %parse:", text)
        self.assertIn("ok -> return_ok", text)
        self.assertIn("err -> return_err", text)
        self.assertIn("return_ok:", text)
        self.assertIn("return %parse", text)
        self.assertIn("return_err:", text)
        self.assertIn("goto return_defer(%parse)", text)


if __name__ == "__main__":
    unittest.main()
