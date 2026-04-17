"""Tests for static analysis passes."""
import sys
sys.path.insert(0, '.')
from src.parser import parse
from src.checker import type_check
from src.analysis import analyse, LayoutPass
from src.types import *


def run_analysis(src: str):
    prog       = parse(src)
    env, diags = type_check(prog)
    layout     = analyse(prog, env)
    return env, diags, layout


def assert_ok(src: str, desc: str = ""):
    env, diags, _ = run_analysis(src)
    errors = diags.all_errors()
    if errors:
        print(f"FAIL (expected ok): {desc or src[:60]!r}")
        for d in errors: print(f"  {d.message}")
        return False
    print(f"PASS: {desc or src[:60]!r}")
    return True


def assert_error(src: str, expected_msg: str = "", desc: str = ""):
    env, diags, _ = run_analysis(src)
    errors = diags.all_errors()
    if not errors:
        print(f"FAIL (expected error): {desc or src[:60]!r}")
        return False
    if expected_msg and not any(expected_msg in d.message for d in errors):
        print(f"FAIL (wrong error): {desc or src[:60]!r}")
        print(f"  expected: {expected_msg!r}")
        for d in errors: print(f"  got: {d.message}")
        return False
    print(f"PASS (error): {desc or src[:60]!r}")
    return True


passed = failed = 0

def run(fn):
    global passed, failed
    if fn(): passed += 1
    else:    failed += 1


# ══════════════════════════════════════════════════════════════
# 1. Exhaustiveness
# ══════════════════════════════════════════════════════════════

run(lambda: assert_ok("""
union Color { Red, Green, Blue }
fun f(c: Color) i64 {
    return match c {
        .Red   => { 0 },
        .Green => { 1 },
        .Blue  => { 2 },
    };
}
""", "exhaustive union match"))

run(lambda: assert_ok("""
union Color { Red, Green, Blue }
fun f(c: Color) i64 {
    return match c {
        .Red => { 0 },
        _   => { 1 },
    };
}
""", "union match with wildcard"))

run(lambda: assert_error("""
union Color { Red, Green, Blue }
fun f(c: Color) i64 {
    return match c {
        .Red   => { 0 },
        .Green => { 1 },
    };
}
""", "missing variants", "non-exhaustive union match"))

run(lambda: assert_ok("""
fun f(b: bool) i64 {
    return match b {
        true  => { 1 },
        false => { 0 },
    };
}
""", "exhaustive bool match"))

run(lambda: assert_error("""
fun f(b: bool) i64 {
    return match b {
        true => { 1 },
    };
}
""", "non-exhaustive", "non-exhaustive bool match"))

run(lambda: assert_error("""
fun f(n: i64) i64 {
    return match n {
        0 => { 0 },
        1 => { 1 },
    };
}
""", "wildcard", "integer match without wildcard"))

run(lambda: assert_ok("""
fun f(n: i64) i64 {
    return match n {
        0 => { 0 },
        _ => { 1 },
    };
}
""", "integer match with wildcard"))


# ══════════════════════════════════════════════════════════════
# 2. Definite assignment
# ══════════════════════════════════════════════════════════════

run(lambda: assert_ok("""
fun f() i64 {
    let x: i64 = 42;
    return x;
}
""", "assigned before use"))

run(lambda: assert_ok("""
fun f(x: i64) i64 {
    return x;
}
""", "param always assigned"))

run(lambda: assert_ok("""
fun f(b: bool) i64 {
    let var x: i64 = 0;
    if b { x = 1; }
    return x;
}
""", "assigned in all paths via initialiser"))

# ══════════════════════════════════════════════════════════════
# 3. Return path checking
# ══════════════════════════════════════════════════════════════

run(lambda: assert_ok("""
fun f() void { }
""", "void function needs no return"))

run(lambda: assert_ok("""
fun f() i64 { return 42; }
""", "simple return"))

run(lambda: assert_ok("""
fun f(b: bool) i64 {
    if b { return 1; } else { return 0; }
}
""", "if-else both return"))

run(lambda: assert_error("""
fun f(b: bool) i64 {
    if b { return 1; }
}
""", "all paths", "missing else return"))

run(lambda: assert_error("""
fun f() i64 {
}
""", "all paths", "empty non-void function"))

run(lambda: assert_ok("""
fun f(b: bool) i64 {
    return if b { 1 } else { 0 };
}
""", "if expression as return"))

run(lambda: assert_ok("""
union Shape { Circle(f64), Point }
fun area(s: Shape) f64 {
    return match s {
        .Circle(r) => { r * r },
        .Point     => { 0.0 },
    };
}
""", "match expression covers all paths"))

run(lambda: assert_ok("""
error E { Bad }
fun guard(n: i64) E!void {
    if n < 0 { return .Bad; }
}
""", "E!void may fall through on success"))

run(lambda: assert_error("""
fun f(b: bool) i64 {
    if b { return 1; }
    if !b { return 0; }
}
""", "all paths", "checker can't prove complementary ifs exhaustive"))


# ══════════════════════════════════════════════════════════════
# 4. Layout pass
# ══════════════════════════════════════════════════════════════

def check_layout(src: str, type_name: str,
                 expected_size: int, desc: str = ""):
    global passed, failed
    prog        = parse(src)
    env, diags  = type_check(prog)
    layout_pass = analyse(prog, env)
    layout      = layout_pass.layouts.get(type_name)
    if layout is None:
        print(f"FAIL: {desc} — layout not found for {type_name!r}")
        failed += 1
        return
    if layout.size == expected_size:
        print(f"PASS: {desc} — {type_name} size={layout.size}")
        passed += 1
    else:
        print(f"FAIL: {desc} — {type_name} expected size={expected_size}, got {layout.size}")
        failed += 1


check_layout("""
struct Point { x: f64, y: f64 }
""", "Point", 16, "Point{f64,f64} = 16 bytes")

check_layout("""
struct Mixed { a: i8, b: i64, c: i32 }
""", "Mixed", 24, "Mixed with padding: i8(1)+pad(7)+i64(8)+i32(4)+pad(4) = 24")

check_layout("""
struct Single { x: f32 }
""", "Single", 4, "Single{f32} = 4 bytes")

check_layout("""
struct Vec3 { x: f32, y: f32, z: f32 }
""", "Vec3", 12, "Vec3{f32,f32,f32} = 12 bytes")

# Primitive sizes via layout_of
env2, _ = type_check(parse("let x: i64 = 0;"))
lp = LayoutPass(env2)
lp.run(parse("let x: i64 = 0;"))

def check_size(ty: Type, expected: int, desc: str):
    global passed, failed
    got = lp.size_of(ty)
    if got == expected:
        print(f"PASS: {desc} size={got}")
        passed += 1
    else:
        print(f"FAIL: {desc} expected={expected} got={got}")
        failed += 1

check_size(T_I64,  8,  "i64 = 8 bytes")
check_size(T_I32,  4,  "i32 = 4 bytes")
check_size(T_F64,  8,  "f64 = 8 bytes")
check_size(T_F32,  4,  "f32 = 4 bytes")
check_size(T_BOOL, 1,  "bool = 1 byte")
check_size(TPointer(T_I64), 8, "*i64 = 8 bytes (pointer)")
check_size(TOptional(T_I64), 16, "?i64 = 16 bytes (i64 + bool + padding)")

# Field offsets
check_layout("""
struct Particle { mass: f32, x: f64, y: f64 }
""", "Particle", 24, "Particle with alignment: f32(4)+pad(4)+f64(8)+f64(8)=24")

prog3 = parse("struct Particle { mass: f32, x: f64, y: f64 }")
env3, _ = type_check(prog3)
lp3 = LayoutPass(env3)
lp3.run(prog3)
offsets = {f.name: f.offset for f in lp3.layouts["Particle"].fields}
expected_offsets = {"mass": 0, "x": 8, "y": 16}
if offsets == expected_offsets:
    print(f"PASS: Particle field offsets correct: {offsets}")
    passed += 1
else:
    print(f"FAIL: Particle field offsets wrong: got {offsets}, expected {expected_offsets}")
    failed += 1

print(f"\n{'═'*50}")
print(f"{passed}/{passed+failed} tests passed")
if failed: print(f"{failed} failed")
sys.exit(1 if failed else 0)
