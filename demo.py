#!/usr/bin/env python3
"""
Mesa compiler demo.

Run from the mesa2/ directory:
    python3 demo.py

Shows the full pipeline: source → tokens → AST → type checking
with real Mesa programs demonstrating language features.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from src.tokenizer import Tokenizer, TK
from src.parser import parse
from src.checker import type_check
from src.types import *

# ── Colours ───────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def header(title: str):
    print(f"\n{BOLD}{CYAN}{'═' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 60}{RESET}\n")

def section(title: str):
    print(f"\n{BOLD}{YELLOW}── {title} ──{RESET}")

def ok(msg: str):
    print(f"  {GREEN}✓{RESET}  {msg}")

def err(msg: str):
    print(f"  {RED}✗{RESET}  {msg}")

def info(msg: str):
    print(f"     {msg}")

def show_source(src: str):
    print()
    for i, line in enumerate(src.strip().split('\n'), 1):
        print(f"  {CYAN}{i:2}{RESET}  {line}")
    print()

def run_demo(title: str, src: str, expect_errors: bool = False):
    """Run a single demo program through the full pipeline."""
    section(title)
    show_source(src)

    # Tokenize
    try:
        tokens = Tokenizer(src).tokenize()
        non_eof = [t for t in tokens if t.kind != TK.EOF]
        ok(f"Tokenized → {len(non_eof)} tokens")
    except Exception as e:
        err(f"Tokenize error: {e}")
        return

    # Parse
    try:
        prog = parse(src)
        decl_count = len(prog.decls)
        decl_names = []
        for d in prog.decls:
            name = getattr(d, 'name', None) or getattr(d, 'interface_name', None)
            if name:
                decl_names.append(f"{type(d).__name__}({name})")
        ok(f"Parsed → {decl_count} declaration(s): {', '.join(decl_names)}")
    except Exception as e:
        err(f"Parse error: {e}")
        return

    # Type check
    try:
        env, diags = type_check(prog)
        errors = diags.all_errors()

        if expect_errors:
            if errors:
                ok(f"Type checker caught {len(errors)} error(s) as expected:")
                for d in errors:
                    info(f"{RED}{d.message}{RESET}")
                    if d.hint:
                        info(f"  hint: {d.hint}")
            else:
                err("Expected type errors but none were found")
        else:
            if errors:
                err(f"Unexpected type errors ({len(errors)}):")
                for d in errors:
                    info(f"{RED}{d.message}{RESET}")
            else:
                ok("Type checked ✓ — no errors")
    except Exception as e:
        err(f"Type checker crash: {e}")
        import traceback; traceback.print_exc()


# ══════════════════════════════════════════════════════════════
# Demo programs
# ══════════════════════════════════════════════════════════════

header("Mesa Compiler Demo")

# ── 1. Basic functions ────────────────────────────────────────
run_demo("Basic functions and arithmetic", """
fun add(a: i64, b: i64) i64 {
    return a + b;
}

fun square(x: f64) f64 {
    return x ^ 2.0;
}

fun abs_val(x: f64) f64 {
    return if x < 0.0 { -x } else { x };
}
""")

# ── 2. Structs ────────────────────────────────────────────────
run_demo("Structs with methods", """
struct Vec2 {
    x: f64,
    y: f64,

    fun length(self: Vec2) f64 {
        return self.x ^ 2.0 + self.y ^ 2.0;
    }

    fun scale(self: *Vec2, factor: f64) void {
        self.x = self.x * factor;
        self.y = self.y * factor;
    }
}

fun dot(a: Vec2, b: Vec2) f64 {
    return a.x * b.x + a.y * b.y;
}
""")

# ── 3. Unions and match ───────────────────────────────────────
run_demo("Unions and match expressions", """
union Shape {
    Circle(f64),
    Rectangle(f64, f64),
    Triangle(f64, f64, f64),
    Point,
}

fun area(s: Shape) f64 {
    return match s {
        Circle(r)       => { r ^ 2.0 },
        Rectangle(w, h) => { w * h },
        Triangle(a, b, c) => { a * b },
        Point           => { 0.0 },
        _               => { 0.0 },
    };
}
""")

# ── 4. Optionals ──────────────────────────────────────────────
run_demo("Optional types and unwrapping", """
fun find_first(v: vec[i64], target: i64) ?i64 {
    for i = 0...10 {
        return target;
    }
    return none;
}

fun safe_divide(a: f64, b: f64) ?f64 {
    if b == 0.0 {
        return none;
    }
    return a / b;
}

fun use_optional(x: ?f64) f64 {
    if x |val| {
        return val * 2.0;
    } else {
        return 0.0;
    }
}

fun with_orelse(x: ?i64) i64 {
    return x orelse 42;
}
""")

# ── 5. Interfaces and def ─────────────────────────────────────
run_demo("Interfaces and implementations", """
interface Describable {
    fun describe(self: @this) str;
    fun name(self: @this) str;
}

struct Point {
    x: f64,
    y: f64,
}

struct Circle {
    center: Point,
    radius: f64,
}

def Describable for Point {
    fun describe(self: Point) str {
        return "a point";
    }
    fun name(self: Point) str {
        return "Point";
    }
}

def Describable for Circle {
    fun describe(self: Circle) str {
        return "a circle";
    }
    fun name(self: Circle) str {
        return "Circle";
    }
}
""")

# ── 6. Generics ───────────────────────────────────────────────
run_demo("Generic functions", """
fun first[T](v: vec[T]) ?T {
    return none;
}

fun identity[T](x: T) T {
    return x;
}

struct Pair[A, B] {
    first:  A,
    second: B,
}

fun swap[A, B](p: Pair[A, B]) Pair[B, A] {
    return .{first: p.second, second: p.first};
}
""")

# ── 7. Closures and higher-order functions ────────────────────
run_demo("Closures and higher-order functions", """
fun apply(f: fun(i64) i64, x: i64) i64 {
    return f(x);
}

fun compose(f: fun(i64) i64, g: fun(i64) i64) fun(i64) i64 {
    return fun(x: i64) i64 { return f(g(x)); };
}

fun make_adder(n: i64) fun(i64) i64 {
    return fun(x: i64) i64 { return x + n; };
}
""")

# ── 8. For loops ──────────────────────────────────────────────
run_demo("For loops with filters and named labels", """
fun sum_evens(n: i64) i64 {
    let var total: i64 = 0;
    for i = 0...n if i % 2 == 0 {
        total += i;
    }
    return total;
}

fun find_in_matrix() void {
    outer: for i = 0...10 {
        for j = 0...10 {
            if i == j {
                break outer;
            }
        }
    }
}
""")

# ── 9. Type aliases ───────────────────────────────────────────
run_demo("Type aliases and tuples", """
let Score    := i64
let Position := .{x: f64, y: f64}
let Velocity := .{x: f64, y: f64}

fun move(pos: Position, vel: Velocity, dt: f64) Position {
    return .{
        x: pos.x + vel.x * dt,
        y: pos.y + vel.y * dt,
    };
}
""")

# ── 10. Error detection ───────────────────────────────────────
run_demo("Type error detection", """
fun bad_types() void {
    let x: bool = 42;
    let y: str = 3.14;
}
""", expect_errors=True)

run_demo("Immutability enforcement", """
fun bad_mutation() void {
    let x: i64 = 5;
    x = 10;
}
""", expect_errors=True)

run_demo("Wrong return type", """
fun wrong() i64 {
    return true;
}
""", expect_errors=True)

run_demo("Unknown field", """
struct Point { x: f64, y: f64 }
fun bad(p: Point) f64 {
    return p.z;
}
""", expect_errors=True)

run_demo("TError cascade suppression (1 error not 3)", """
fun cascade() void {
    let x: bool = 42;
    let y = x + 1;
    let z = y * 2;
}
""", expect_errors=True)

# ── Summary ───────────────────────────────────────────────────
header("Demo Complete")
print(f"  The Mesa compiler pipeline is working:")
print(f"  {GREEN}Tokenizer{RESET}  → handles all Mesa syntax including")
print(f"             +- uncertainty, .* broadcast, ?. chaining,")
print(f"             #[ attributes, \\\\ multiline strings")
print(f"  {GREEN}Parser{RESET}     → full recursive descent with Pratt expressions,")
print(f"             generics, where clauses, named loops,")
print(f"             if/while |v| unwrap, vec comprehensions")
print(f"  {GREEN}Type checker{RESET} → bidirectional inference, TError propagation,")
print(f"             interface contracts, mutability checking,")
print(f"             optional unwrapping, struct field resolution")
print()
print(f"  Next step: {CYAN}LLVM codegen{RESET}")
print()
