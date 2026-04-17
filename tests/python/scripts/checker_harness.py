"""Type checker tests."""
import sys
sys.path.insert(0, '.')
from src.parser import parse
from src.checker import type_check
from src.types import *


def check(src: str):
    prog      = parse(src)
    env, diags = type_check(prog)
    return env, diags


def assert_ok(src: str, desc: str = ""):
    env, diags = check(src)
    if diags.has_errors():
        print(f"FAIL (expected ok): {desc or src[:60]!r}")
        for d in diags.all_errors(): print(f"  {d}")
        return False
    print(f"PASS: {desc or src[:60]!r}")
    return True


def assert_error(src: str, expected_msg: str = "", desc: str = ""):
    env, diags = check(src)
    if not diags.has_errors():
        print(f"FAIL (expected error): {desc or src[:60]!r}")
        return False
    if expected_msg and not any(expected_msg in str(d) for d in diags.all_errors()):
        print(f"FAIL (wrong error): {desc or src[:60]!r}")
        print(f"  expected: {expected_msg!r}")
        for d in diags.all_errors(): print(f"  got: {d.message}")
        return False
    print(f"PASS (error): {desc or src[:60]!r}")
    return True


def assert_inferred_error_set(src: str, fn_name: str, expected_names: list[str], desc: str = ""):
    env, diags = check(src)
    if diags.has_errors():
        print(f"FAIL (expected ok): {desc or fn_name!r}")
        for d in diags.all_errors(): print(f"  {d}")
        return False
    sym = env.lookup(fn_name)
    if sym is None or not isinstance(sym.type_, TFun) or not isinstance(sym.type_.ret, TErrorUnion):
        print(f"FAIL (wrong function type): {desc or fn_name!r}")
        return False
    actual = [m.name for m in error_set_members(sym.type_.ret.error_set)]
    if actual != sorted(expected_names):
        print(f"FAIL (wrong inferred error set): {desc or fn_name!r}")
        print(f"  expected: {sorted(expected_names)!r}")
        print(f"  got:      {actual!r}")
        return False
    print(f"PASS: {desc or fn_name!r}")
    return True


def assert_error_hint(src: str, expected_msg: str, expected_hint: str, desc: str = ""):
    env, diags = check(src)
    errors = diags.all_errors()
    if not errors:
        print(f"FAIL (expected error): {desc or src[:60]!r}")
        return False
    for d in errors:
        if expected_msg in d.message and expected_hint in (d.hint or ""):
            print(f"PASS (error): {desc or src[:60]!r}")
            return True
    print(f"FAIL (wrong error/hint): {desc or src[:60]!r}")
    for d in errors:
        print(f"  got: {d.message!r} hint={d.hint!r}")
    return False


def assert_single_error(src: str, expected_msg: str, desc: str = ""):
    env, diags = check(src)
    errors = diags.all_errors()
    if len(errors) != 1:
        print(f"FAIL (wrong error count): {desc or src[:60]!r}")
        print(f"  expected 1 error, got {len(errors)}")
        for d in errors:
            print(f"  got: {d.message!r}")
        return False
    if expected_msg not in errors[0].message:
        print(f"FAIL (wrong error): {desc or src[:60]!r}")
        print(f"  expected: {expected_msg!r}")
        print(f"  got: {errors[0].message!r}")
        return False
    print(f"PASS (error): {desc or src[:60]!r}")
    return True


def assert_single_error_code(src: str, expected_msg: str, expected_code: str, desc: str = ""):
    env, diags = check(src)
    errors = diags.all_errors()
    if len(errors) != 1:
        print(f"FAIL (wrong error count): {desc or src[:60]!r}")
        print(f"  expected 1 error, got {len(errors)}")
        for d in errors:
            print(f"  got: {d.message!r} code={d.code!r}")
        return False
    err = errors[0]
    if expected_msg not in err.message or err.code != expected_code:
        print(f"FAIL (wrong error/code): {desc or src[:60]!r}")
        print(f"  expected msg:  {expected_msg!r}")
        print(f"  expected code: {expected_code!r}")
        print(f"  got msg:       {err.message!r}")
        print(f"  got code:      {err.code!r}")
        return False
    print(f"PASS (error): {desc or src[:60]!r}")
    return True


passed = failed = 0

def run(fn):
    global passed, failed
    if fn(): passed += 1
    else:    failed += 1

# ── Basic types and let bindings ─────────────────────────────
run(lambda: assert_ok(
    "let x: i64 = 42;",
    "let with annotation"))

run(lambda: assert_ok(
    "let x = 42;",
    "let inferred as i64"))

run(lambda: assert_ok(
    "let x: f64 = 42;",
    "integer literal coerces to f64"))

run(lambda: assert_ok(
    "let x: f32 = 3.14;",
    "float literal coerces to f32"))

run(lambda: assert_error(
    "let x: bool = 42;",
    "mismatch",
    "int to bool is an error"))

run(lambda: assert_error_hint(
    "fun f() void { let count = 1; println(cout); }",
    "undefined name 'cout'",
    "did you mean 'count'?",
    "undefined name includes suggestion"))

run(lambda: assert_error_hint(
    "fun f(x: flloat) void { }",
    "unknown type 'flloat'",
    "did you mean 'float'?",
    "unknown type includes suggestion"))

run(lambda: assert_single_error(
    "fun main() void { let count = 1; println(cout); }",
    "undefined name 'cout'",
    "duplicate checker diagnostics are deduped"))

# ── Mutability ───────────────────────────────────────────────
run(lambda: assert_ok(
    "fun f() void { let var x: i64 = 0; x = 1; }",
    "mutable let var can be reassigned"))

run(lambda: assert_error(
    "fun f() void { let x: i64 = 0; x = 1; }",
    "immutable",
    "immutable let cannot be reassigned"))

# ── Functions ────────────────────────────────────────────────
run(lambda: assert_ok(
    "fun add(a: i64, b: i64) i64 { return a + b; }",
    "basic function"))

run(lambda: assert_ok(
    "fun greet(name: str) str { return name; }",
    "string function"))

run(lambda: assert_error(
    "fun f() i64 { return true; }",
    "mismatch",
    "wrong return type"))

# ── Arithmetic operators ─────────────────────────────────────
run(lambda: assert_ok(
    "fun f() f64 { return 1.0 + 2.0; }",
    "float add"))

run(lambda: assert_ok(
    "fun f() i64 { return 2 ^ 10; }",
    "exponentiation"))

run(lambda: assert_error(
    "fun f() void { let x = true + 1; }",
    "not defined",
    "bool + int is an error"))

# ── Structs and field access ──────────────────────────────────
run(lambda: assert_ok("""
struct Point {
    x: f64,
    y: f64,
}
fun dist(p: Point) f64 {
    return p.x + p.y;
}
""", "struct field access"))

run(lambda: assert_error("""
struct Point { x: f64, y: f64 }
fun f(p: Point) f64 { return p.z; }
""", "no field", "unknown field error"))

# ── Struct methods ───────────────────────────────────────────
run(lambda: assert_ok("""
struct Counter {
    n: i64,
    fun get(self: Counter) i64 { return self.n; }
    fun inc(self: *Counter) void { self.n = self.n + 1; }
}
""", "struct with methods"))

# ── Optional types ───────────────────────────────────────────
run(lambda: assert_ok(
    "fun f() ?i64 { return none; }",
    "return none for optional"))

run(lambda: assert_ok(
    "fun f() ?i64 { return 42; }",
    "return value for optional"))

run(lambda: assert_ok("""
fun f(x: ?i64) i64 {
    return x orelse 0;
}
""", "orelse unwrap"))

run(lambda: assert_ok("""
fun f(x: ?i64) i64 {
    if x |v| {
        return v;
    } else {
        return 0;
    }
}
""", "if |v| unwrap"))

# ── If expressions ───────────────────────────────────────────
run(lambda: assert_ok("""
fun f(b: bool) i64 {
    let x = if b { 1 } else { 0 };
    return x;
}
""", "if expression"))

run(lambda: assert_error("""
fun f(b: bool) i64 {
    let x = if b { 1 } else { true };
    return x;
}
""", "incompatible", "if branches incompatible"))

# ── For loops ────────────────────────────────────────────────
run(lambda: assert_ok("""
fun f() void {
    for i = 0...10 { }
}
""", "for range loop"))

run(lambda: assert_ok("""
fun f() void {
    for i = 0...10 : i > 5 { }
}
""", "for range with filter"))

run(lambda: assert_ok("""
fun f(v: vec[i64]) void {
    let var total: i64 = 0;
    for x in v, total += x
}
""", "for iter one-liner"))

run(lambda: assert_ok("""
fun f(x: f64) f64 {
    let y = x
    if y > 1.0 {
        return y
    }
    return 0.0
}
""", "newline after let init does not trigger suffix if"))

# ── Match ────────────────────────────────────────────────────
run(lambda: assert_ok("""
union Color { Red, Green, Blue }
fun f(c: Color) i64 {
    return match c {
        .Red   => { 0 },
        .Green => { 1 },
        .Blue  => { 2 },
        _     => { -1 },
    };
}
""", "match on union"))

# ── Unions ───────────────────────────────────────────────────
run(lambda: assert_ok("""
union Shape {
    Circle(f64),
    Rectangle(f64, f64),
    Point,
}
fun area(s: Shape) f64 {
    return match s {
        .Circle(r)        => { r * r },
        .Rectangle(w, h)  => { w * h },
        .Point            => { 0.0 },
        _                => { 0.0 },
    };
}
""", "union with payloads"))

# ── Interfaces and def ───────────────────────────────────────
run(lambda: assert_ok("""
interface Greet {
    fun greet(self: @this) str;
}
struct Person {
    name: str,
}
def Greet for Person {
    fun greet(self: Person) str {
        return self.name;
    }
}
""", "interface and def"))

run(lambda: assert_error("""
interface Greet {
    fun greet(self: @this) str;
}
struct Person { name: str }
def Greet for Person {
}
""", "missing method", "def missing method"))

# ── Closures ─────────────────────────────────────────────────
run(lambda: assert_ok("""
fun apply(f: fun(i64) i64, x: i64) i64 {
    return f(x);
}
fun f() i64 {
    return apply(fun(x: i64) i64 { return x + 1; }, 5);
}
""", "higher order function"))

run(lambda: assert_ok("""
fun f(x: i64) str {
    return @typeof(x);
}
""", "@typeof intrinsic"))

run(lambda: assert_error("""
import mem
fun f() i64 {
    let arena = mem.arenaPageAllocator(64)
    let p = @alloc(arena, 8, 8)
    let q = @realloc(arena, p, 8, 16, 8)
    @freeBytes(arena, q, 16)
    return @sizeOf(i64) + @alignOf(i64)
}
""", "internal std.mem intrinsic", "allocator intrinsics hidden from user code"))

run(lambda: assert_ok("""
struct Counter {
    value: i64,
}
fun f() bool {
    return @hasField(Counter, "value")
}
""", "@hasField intrinsic"))

run(lambda: assert_ok("""
struct Box[T] { val: T }
fun f(b: Box[i64]) i64 {
    return b.val;
}
""", "generic struct instantiation"))

# ── Vec types ────────────────────────────────────────────────
run(lambda: assert_ok("""
fun f() vec[i64] {
    return vec[1, 2, 3];
}
""", "vec literal"))

run(lambda: assert_ok("""
fun f() vec[f64] {
    let v: vec[f64] = vec[1, 2, 3];
    return v;
}
""", "vec with type annotation coerces elements"))

run(lambda: assert_ok("""
fun f(v: vec[i64]) i64 {
    return v.len;
}
""", "vec len field"))

run(lambda: assert_ok("""
fun f() float {
    let x = 9.8 +- 0.1
    let y = 1.2 +- 0.2
    return (x + y).uncertainty
}
""", "uncertain arithmetic type-checks"))

run(lambda: assert_ok("""
fun f() str {
    let F = 10.0 +- 0.5 `N`
    F.units
}
""", "uncertain unitful exposes units directly"))

run(lambda: assert_ok("""
fun f() float`s` {
    let t2 = 4.0 +- 0.4 `s^2`
    t2.sqrt().uncertainty
}
""", "uncertain sqrt type-checks"))

run(lambda: assert_error("""
fun f() float {
    let x = 9.0 `m`
    return x.sqrt()
}
""", "sqrt() requires units with even exponents"))

# ── With expressions and lifetimes ───────────────────────────
run(lambda: assert_ok("""
error ReadError { Missing(str), Empty }
fun read() ReadError!i64 { return ReadError.Missing("file.txt") }
fun run() void {
    let x = try read()
    println(x)
} handle |e| {
    match e {
        .Missing(path) => println(path),
        .Empty => println("empty"),
    }
}
""", "handle binding allows payload destructuring in match"))

run(lambda: assert_ok("""
error ReadError { Missing(str), Empty }
fun label(e: ReadError) str {
    if e.Empty {
        return "empty"
    }
    if e.Missing |path| {
        return path
    }
    return "?"
}
""", "error object field access exposes bool and optional payload"))

run(lambda: assert_ok("""
error ReadError { Missing(str), Empty }
fun render(e: ReadError) str {
    return match e {
        .Missing(path) => path,
        .Empty => "empty",
    }
}
""", "match payload destructuring works for ordinary error objects"))

run(lambda: assert_ok("""
error ReadError { Missing }
error WriteError { Full }
let MyErrors := ReadError | WriteError

fun fail(flag: bool) MyErrors!void {
    if flag { return .Missing }
    return .Full
}
""", "type alias supports error-set unions"))

run(lambda: assert_ok("""
error ReadError { Missing }
error WriteError { Full }

fun read() ReadError!i64 { return .Missing }
fun write() WriteError!i64 { return .Full }

fun run(flag: bool) void {
    if flag {
        let _ = try read()
    } else {
        let _ = try write()
    }
} handle |e| {
    match e {
        .Missing => println("r"),
        .Full => println("w"),
    }
}
""", "handle binding infers error-set union"))

run(lambda: assert_error("""
error ReadError { Missing }
error WriteError { Full }
fun fail() ReadError!void {
    return .Full
}
""", "wrong error set", "explicit error return type rejects undeclared error set"))

run(lambda: assert_error_hint("""
error ReadError { Missing }
error WriteError { Full }
fun fail() ReadError!void {
    return .Full
}
""", "wrong error set", "unexpected here: WriteError",
    "wrong error set includes unexpected-set hint"))

run(lambda: assert_inferred_error_set("""
error ReadError { Missing }
error WriteError { Full }

fun read(flag: bool) ReadError!i64 {
    if flag { return .Missing }
    return 1
}

fun write(flag: bool) WriteError!void {
    if flag { return .Full }
}

fun run(a: bool, b: bool) !i64 {
    let x = try read(a)
    try write(b)
    return x
}
""", "run", ["ReadError", "WriteError"], "bare !T infers union from propagated calls"))

run(lambda: assert_inferred_error_set("""
error ReadError { Missing }

fun read(flag: bool) ReadError!i64 {
    if flag { return .Missing }
    return 1
}

fun run(flag: bool) !i64 {
    let x = try read(flag)
    return x
} handle |e| {
    return e
}
""", "run", ["ReadError"], "handle return e preserves inferred concrete error set"))

run(lambda: assert_ok("""
error E { Bad }
fun guard(n: i64) E!void {
    if n < 0 { return .Bad; }
}
""", "E!void allows fallthrough success path"))

run(lambda: assert_ok("""
fun good() !i64 {
    esc 42
}
""", "esc outside with uses default gc context"))

# ── Named loops ──────────────────────────────────────────────
run(lambda: assert_ok("""
fun f() void {
    outer: for i = 0...10 {
        for j = 0...10 {
            break outer;
        }
    }
}
""", "named break"))

run(lambda: assert_error("""
fun f() void {
    break;
}
""", "break outside loop", "break outside loop"))

# ── Tuple types ──────────────────────────────────────────────
run(lambda: assert_ok("""
fun f() .{x: f64, y: f64} {
    return .{x: 1.0, y: 2.0};
}
""", "tuple return"))

run(lambda: assert_ok("""
fun f() .{f64, f64} {
    let t: .{f64, f64} = .{1, 2};
    return t;
}
""", "tuple with integer coercion"))

# ── Type aliases ─────────────────────────────────────────────
run(lambda: assert_ok("""
let Score := i64
fun f() Score {
    return 100;
}
""", "type alias"))

# ── Error TError propagation — only one error reported ───────
run(lambda: assert_error("""
fun f() void {
    let x: bool = 42;
    let y = x + 1;
    let z = y * 2;
}
""", desc="TError propagation — only one error not three"))

# Check it's exactly one error
env, diags = check("""
fun f() void {
    let x: bool = 42;
    let y = x + 1;
    let z = y * 2;
}
""")
error_count = len(diags.all_errors())
if error_count <= 2:
    print(f"PASS: TError suppresses cascades ({error_count} error(s))")
    passed += 1
else:
    print(f"FAIL: expected <=2 errors from cascade, got {error_count}")
    failed += 1

print(f"\n{'═'*50}")
print(f"{passed}/{passed+failed} tests passed")
if failed: print(f"{failed} failed")
sys.exit(1 if failed else 0)
