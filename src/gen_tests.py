#!/usr/bin/env python3
"""
Generate ~1000 oracle tests for the Mesa compiler.
Writes .mesa files into tests/ subdirectories.
"""
import os

BASE = os.path.join(os.path.dirname(__file__), '..', 'tests')

def write(path: str, content: str):
    full = os.path.join(BASE, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, 'w').write(content.lstrip('\n'))

# ══════════════════════════════════════════════════════════════
# TYPES — primitive assignments, coercions, mismatches
# ══════════════════════════════════════════════════════════════

# i8 / i16 / i32 / i64
for bits in [8, 16, 32, 64]:
    write(f'types/int_assign_i{bits}_ok.mesa', f"""
let x: i{bits} = 0;  //~ ok
fun f() i{bits} {{     //~ compile-ok
    let var n: i{bits} = 0;
    n = n + 1;
    return n;
}}
""")
    write(f'types/int_assign_i{bits}_bool_err.mesa', f"""
fun f() void {{
    let x: i{bits} = true;  //~ error: type mismatch
}}
""")

# u8 / u16 / u32 / u64
for bits in [8, 16, 32, 64]:
    write(f'types/uint_assign_u{bits}_ok.mesa', f"""
fun f() u{bits} {{  //~ compile-ok
    let x: u{bits} = 0;
    return x;
}}
""")

# f32 / f64
for bits in [32, 64]:
    write(f'types/float_assign_f{bits}_ok.mesa', f"""
fun f() f{bits} {{  //~ compile-ok
    let x: f{bits} = 3.14;
    return x;
}}
""")
    write(f'types/float_int_literal_f{bits}_ok.mesa', f"""
fun f() f{bits} {{  //~ compile-ok
    let x: f{bits} = 42;
    return x;
}}
""")
    write(f'types/float_assign_bool_err.mesa', f"""
fun f() void {{
    let x: f{bits} = true;  //~ error: type mismatch
}}
""")

# bool
write('types/bool_assign_ok.mesa', """
fun f() bool {  //~ compile-ok
    let x: bool = true;
    let y: bool = false;
    return x and y;
}
""")
write('types/bool_assign_int_err.mesa', """
fun f() void {
    let x: bool = 0;  //~ error: type mismatch
}
""")
write('types/bool_assign_float_err.mesa', """
fun f() void {
    let x: bool = 1.0;  //~ error: type mismatch
}
""")

# str
write('types/str_assign_ok.mesa', """
fun f() str {  //~ compile-ok
    let x: str = "hello";
    return x;
}
""")
write('types/str_assign_int_err.mesa', """
fun f() void {
    let x: str = 42;  //~ error: type mismatch
}
""")
write('types/str_assign_bool_err.mesa', """
fun f() void {
    let x: str = true;  //~ error: type mismatch
}
""")

# void return
write('types/void_return_ok.mesa', """
fun f() void {  //~ compile-ok
    let x: i64 = 1;
}
""")
write('types/void_cannot_return_value.mesa', """
fun f() void {
    return 42;  //~ error: void
}
""")

# let infer
write('types/let_infer_int_ok.mesa', """
fun f() void {  //~ compile-ok
    let x = 42;
    let y = x + 1;
}
""")
write('types/let_infer_float_ok.mesa', """
fun f() void {  //~ compile-ok
    let x = 3.14;
    let y = x * 2.0;
}
""")
write('types/let_infer_bool_ok.mesa', """
fun f() void {  //~ compile-ok
    let x = true;
    let y = !x;
}
""")

# let var mutability
write('types/let_var_mutable_ok.mesa', """
fun f() void {  //~ compile-ok
    let var x: i64 = 0;
    x = 1;
    x += 1;
    x -= 1;
    x *= 2;
}
""")
write('types/let_immutable_reassign_err.mesa', """
fun f() void {
    let x: i64 = 5;
    x = 10;  //~ error: immutable
}
""")
write('types/let_immutable_pluseq_err.mesa', """
fun f() void {
    let x: i64 = 5;
    x += 1;  //~ error: immutable
}
""")

# type aliases
write('types/alias_primitive_ok.mesa', """
let Meters := f64
let Seconds := f64
let Score := i64

fun f() Score {  //~ compile-ok
    return 100;
}
""")
write('types/alias_named_tuple_ok.mesa', """
let Position := .{x: f64, y: f64}

fun origin() Position {  //~ compile-ok
    return .{x: 0.0, y: 0.0};
}
""")

# ══════════════════════════════════════════════════════════════
# OPERATORS
# ══════════════════════════════════════════════════════════════

# arithmetic
for op, name in [('+','add'),('-','sub'),('*','mul'),('/','div'),('%','mod')]:
    write(f'operators/int_{name}_ok.mesa', f"""
fun f(a: i64, b: i64) i64 {{  //~ compile-ok
    return a {op} b;
}}
""")
    write(f'operators/float_{name}_ok.mesa', f"""
fun f(a: f64, b: f64) f64 {{  //~ compile-ok
    return a {op} b;
}}
""")

# exponentiation
write('operators/exp_float_ok.mesa', """
fun f(x: f64) f64 {  //~ compile-ok
    return x ^ 2.0;
}
""")
write('operators/exp_int_ok.mesa', """
fun f(x: i64) i64 {  //~ compile-ok
    return x ^ 3;
}
""")

# comparison
for op, name in [('==','eq'),('!=','ne'),('<','lt'),('>','gt'),('<=','le'),('>=','ge')]:
    write(f'operators/int_cmp_{name}_ok.mesa', f"""
fun f(a: i64, b: i64) bool {{  //~ compile-ok
    return a {op} b;
}}
""")
    write(f'operators/float_cmp_{name}_ok.mesa', f"""
fun f(a: f64, b: f64) bool {{  //~ compile-ok
    return a {op} b;
}}
""")

# logical
write('operators/logical_and_ok.mesa', """
fun f(a: bool, b: bool) bool {  //~ compile-ok
    return a and b;
}
""")
write('operators/logical_or_ok.mesa', """
fun f(a: bool, b: bool) bool {  //~ compile-ok
    return a or b;
}
""")
write('operators/logical_not_ok.mesa', """
fun f(a: bool) bool {  //~ compile-ok
    return !a;
}
""")
write('operators/logical_and_non_bool_err.mesa', """
fun f(a: i64, b: i64) bool {
    return a and b;  //~ error: bool
}
""")

# unary minus
write('operators/unary_minus_int_ok.mesa', """
fun f(x: i64) i64 {  //~ compile-ok
    return -x;
}
""")
write('operators/unary_minus_float_ok.mesa', """
fun f(x: f64) f64 {  //~ compile-ok
    return -x;
}
""")
write('operators/unary_not_bool_ok.mesa', """
fun f(x: bool) bool {  //~ compile-ok
    return !x;
}
""")

# compound assignment
for op in ['+=', '-=', '*=', '/=']:
    write(f'operators/compound_{op.replace("=","eq")}_ok.mesa', f"""
fun f() i64 {{  //~ compile-ok
    let var x: i64 = 10;
    x {op} 3;
    return x;
}}
""")

# address-of and deref
write('operators/addr_deref_ok.mesa', """
fun f() i64 {  //~ compile-ok
    let var x: i64 = 42;
    let p: *i64 = @x;
    return *p;
}
""")

# operator on wrong types
write('operators/add_bool_err.mesa', """
fun f() void {
    let x = true + false;  //~ error: not defined
}
""")
write('operators/add_str_err.mesa', """
fun f() void {
    let x = "a" + "b";  //~ error: not defined
}
""")
write('operators/cmp_bool_int_err.mesa', """
fun f() bool {
    return true < 1;  //~ error:
}
""")

# ══════════════════════════════════════════════════════════════
# CONTROL FLOW
# ══════════════════════════════════════════════════════════════

# if expressions
write('control_flow/if_both_branches_ok.mesa', """
fun f(b: bool) i64 {  //~ compile-ok
    return if b { 1 } else { 0 };
}
""")
write('control_flow/if_missing_else_ok.mesa', """
fun f(b: bool) void {  //~ compile-ok
    if b { let x: i64 = 1; }
}
""")
write('control_flow/if_branch_type_mismatch_err.mesa', """
fun f(b: bool) i64 {
    return if b { 1 } else { true };  //~ error: incompatible
}
""")
write('control_flow/if_non_bool_cond_err.mesa', """
fun f(n: i64) void {
    if n { }  //~ error:
}
""")
write('control_flow/if_cond_ok.mesa', """
fun f(n: i64) void {  //~ compile-ok
    if n > 0 { }
    if n == 0 { }
    if n != 0 { }
}
""")
write('control_flow/if_else_chain_ok.mesa', """
fun f(n: i64) i64 {  //~ compile-ok
    if n < 0 {
        return -1;
    } else if n == 0 {
        return 0;
    } else {
        return 1;
    }
}
""")

# if unwrap
write('control_flow/if_unwrap_ok.mesa', """
fun f(x: ?i64) i64 {  //~ compile-ok
    if x |v| { return v; } else { return 0; }
}
""")
write('control_flow/if_unwrap_mut_ok.mesa', """
fun f(x: ?i64) void {  //~ compile-ok
    if x |*v| { }
}
""")
write('control_flow/if_unwrap_non_optional_err.mesa', """
fun f(x: i64) void {
    if x |v| { }  //~ error: optional
}
""")
write('control_flow/if_unwrap_binding_scoped.mesa', """
fun f(x: ?i64) i64 {  //~ compile-ok
    if x |v| {
        return v;
    }
    return 0;
}
""")

# while
write('control_flow/while_bool_ok.mesa', """
fun f() void {  //~ compile-ok
    let var x: i64 = 0;
    while x < 10 {
        x += 1;
    }
}
""")
write('control_flow/while_non_bool_err.mesa', """
fun f() void {
    while 42 {  //~ error:
    }
}
""")

# return
write('control_flow/return_correct_type_ok.mesa', """
fun f() i64 {  //~ compile-ok
    return 42;
}
""")
write('control_flow/return_wrong_type_err.mesa', """
fun f() i64 {
    return false;  //~ error: type mismatch
}
""")
write('control_flow/return_missing_err.mesa', """
fun f() i64 {
}  //~ error: all paths
""")
write('control_flow/return_in_all_branches_ok.mesa', """
fun f(b: bool) i64 {  //~ compile-ok
    if b { return 1; } else { return 0; }
}
""")
write('control_flow/return_void_ok.mesa', """
fun f() void {  //~ compile-ok
    return;
}
""")
write('control_flow/return_value_from_void_err.mesa', """
fun f() void {
    return 42;  //~ error: void
}
""")
write('control_flow/early_return_ok.mesa', """
fun f(x: i64) i64 {  //~ compile-ok
    if x < 0 { return 0; }
    return x;
}
""")

# match
write('control_flow/match_int_wildcard_ok.mesa', """
fun f(n: i64) i64 {  //~ compile-ok
    return match n {
        0 => { 0 },
        1 => { 1 },
        _ => { 2 },
    };
}
""")
write('control_flow/match_int_no_wildcard_err.mesa', """
fun f(n: i64) i64 {
    return match n {
        0 => { 0 },
        1 => { 1 },
    };  //~ error: wildcard
}
""")
write('control_flow/match_bool_both_ok.mesa', """
fun f(b: bool) i64 {  //~ compile-ok
    return match b {
        true  => { 1 },
        false => { 0 },
    };
}
""")
write('control_flow/match_bool_missing_err.mesa', """
fun f(b: bool) i64 {
    return match b {
        true => { 1 },
    };  //~ error: non-exhaustive
}
""")
write('control_flow/match_arms_type_mismatch_err.mesa', """
fun f(n: i64) i64 {
    return match n {
        0 => { 0 },
        _ => { true },  //~ error:
    };
}
""")

# break / continue
write('control_flow/break_in_loop_ok.mesa', """
fun f() void {  //~ compile-ok
    for i = 0...10 {
        if i > 5 { break; }
    }
}
""")
write('control_flow/break_outside_loop_err.mesa', """
fun f() void {
    break;  //~ error: break outside loop
}
""")
write('control_flow/continue_in_loop_ok.mesa', """
fun f() void {  //~ compile-ok
    for i = 0...10 {
        if i % 2 == 0 { continue; }
    }
}
""")
write('control_flow/continue_outside_loop_err.mesa', """
fun f() void {
    continue;  //~ error: continue outside loop
}
""")
write('control_flow/named_break_ok.mesa', """
fun f() void {  //~ compile-ok
    outer: for i = 0...10 {
        for j = 0...10 {
            if i == j { break outer; }
        }
    }
}
""")
write('control_flow/named_continue_ok.mesa', """
fun f() void {  //~ compile-ok
    outer: for i = 0...10 {
        for j = 0...10 {
            if j == 0 { continue outer; }
        }
    }
}
""")

# defer
write('control_flow/defer_ok.mesa', """
fun f() void {  //~ compile-ok
    defer { let x: i64 = 1; }
}
""")

# ══════════════════════════════════════════════════════════════
# LOOPS
# ══════════════════════════════════════════════════════════════

write('loops/for_range_inclusive_ok.mesa', """
fun f() i64 {  //~ compile-ok
    let var t: i64 = 0;
    for i = 0...10 { t += i; }
    return t;
}
""")
write('loops/for_range_exclusive_ok.mesa', """
fun f() i64 {  //~ compile-ok
    let var t: i64 = 0;
    for i = 0..10 { t += i; }
    return t;
}
""")
write('loops/for_range_with_filter_ok.mesa', """
fun f() i64 {  //~ compile-ok
    let var t: i64 = 0;
    for i = 0...10 : i % 2 == 0 { t += i; }
    return t;
}
""")
write('loops/for_range_non_int_err.mesa', """
fun f() void {
    for i = 0.0...10.0 {  //~ error: integer
    }
}
""")
write('loops/for_iter_vec_ok.mesa', """
fun f() void {  //~ compile-ok
    let v = vec[1, 2, 3];
    for x in v {
        let y: i64 = x;
    }
}
""")
write('loops/for_iter_ref_ok.mesa', """
fun f() void {  //~ compile-ok
    let v = vec[1, 2, 3];
    for *x in v {
    }
}
""")
write('loops/for_iter_with_filter_ok.mesa', """
fun f() void {  //~ compile-ok
    let v = vec[1, 2, 3, 4, 5];
    for x in v, x > 2 {
        let y: i64 = x;
    }
}
""")
write('loops/while_basic_ok.mesa', """
fun f() i64 {  //~ compile-ok
    let var x: i64 = 10;
    while x > 0 { x -= 1; }
    return x;
}
""")
write('loops/while_unwrap_ok.mesa', """
fun f(x: ?i64) void {  //~ compile-ok
    while x |v| { }
}
""")
write('loops/nested_loops_ok.mesa', """
fun f() i64 {  //~ compile-ok
    let var t: i64 = 0;
    for i = 0...5 {
        for j = 0...5 {
            t += i * j;
        }
    }
    return t;
}
""")
write('loops/loop_var_scoped_ok.mesa', """
fun f() i64 {  //~ compile-ok
    let var t: i64 = 0;
    for i = 0...10 { t += i; }
    return t;
}
""")

# ══════════════════════════════════════════════════════════════
# FUNCTIONS
# ══════════════════════════════════════════════════════════════

write('functions/no_params_ok.mesa', """
fun answer() i64 {  //~ compile-ok
    return 42;
}
""")
write('functions/multiple_params_ok.mesa', """
fun add3(a: i64, b: i64, c: i64) i64 {  //~ compile-ok
    return a + b + c;
}
""")
write('functions/recursive_ok.mesa', """
fun factorial(n: i64) i64 {  //~ compile-ok
    if n <= 1 { return 1; }
    return n * factorial(n - 1);
}
""")
write('functions/mutual_recursion_ok.mesa', """
fun is_even(n: i64) bool {  //~ compile-ok
    if n == 0 { return true; }
    return is_odd(n - 1);
}
fun is_odd(n: i64) bool {
    if n == 0 { return false; }
    return is_even(n - 1);
}
""")
write('functions/higher_order_ok.mesa', """
fun apply(f: fun(i64) i64, x: i64) i64 {  //~ compile-ok
    return f(x);
}
""")
write('functions/closure_ok.mesa', """
fun make_adder(n: i64) fun(i64) i64 {  //~ compile-ok
    return fun(x: i64) i64 { return x + n; };
}
""")
write('functions/wrong_arg_type_err.mesa', """
fun f(x: i64) i64 { return x; }
fun g() void {
    f(true);  //~ error: type mismatch
}
""")
write('functions/wrong_arg_count_err.mesa', """
fun f(a: i64, b: i64) i64 { return a + b; }
fun g() void {
    f(1);  //~ error: arguments
}
""")
write('functions/call_undefined_fn_err.mesa', """
fun f() void {
    undefined_fn();  //~ error: undefined
}
""")
write('functions/inline_fn_ok.mesa', """
inline fun fast(x: i64) i64 {  //~ compile-ok
    return x * 2;
}
""")
write('functions/return_struct_ok.mesa', """
struct Point { x: f64, y: f64 }
fun make_point(x: f64, y: f64) Point {  //~ compile-ok
    return .{x: x, y: y};
}
""")
write('functions/return_optional_ok.mesa', """
fun safe_div(a: f64, b: f64) ?f64 {  //~ compile-ok
    if b == 0.0 { return none; }
    return a / b;
}
""")
write('functions/void_no_explicit_return_ok.mesa', """
fun f() void {  //~ compile-ok
    let x: i64 = 1;
}
""")
write('functions/multiple_returns_ok.mesa', """
fun f(x: i64) i64 {  //~ compile-ok
    if x < 0 { return -1; }
    if x == 0 { return 0; }
    return 1;
}
""")

# ══════════════════════════════════════════════════════════════
# STRUCTS
# ══════════════════════════════════════════════════════════════

write('structs/basic_struct_ok.mesa', """
struct Point { x: f64, y: f64 }
fun f(p: Point) f64 {  //~ compile-ok
    return p.x + p.y;
}
""")
write('structs/nested_struct_ok.mesa', """
struct Vec2 { x: f64, y: f64 }
struct Rect { pos: Vec2, size: Vec2 }
fun area(r: Rect) f64 {  //~ compile-ok
    return r.size.x * r.size.y;
}
""")
write('structs/struct_field_unknown_err.mesa', """
struct Point { x: f64, y: f64 }
fun f(p: Point) f64 {
    return p.z;  //~ error: no field
}
""")
write('structs/struct_method_ok.mesa', """
struct Counter {
    n: i64,
    fun get(self: Counter) i64 { return self.n; }
}
fun f(c: Counter) i64 {  //~ compile-ok
    return c.get();
}
""")
write('structs/struct_mut_method_ok.mesa', """
struct Counter {
    n: i64,
    fun inc(self: *Counter) void { self.n = self.n + 1; }
}
fun f() void {  //~ compile-ok
    let var c: Counter = .{n: 0};
    c.inc();
}
""")
write('structs/struct_init_ok.mesa', """
struct Point { x: f64, y: f64 }
fun f() Point {  //~ compile-ok
    return .{x: 1.0, y: 2.0};
}
""")
write('structs/struct_all_field_types_ok.mesa', """
struct Mixed {
    a: i64,
    b: f64,
    c: bool,
    d: str,
}
fun f(m: Mixed) bool {  //~ compile-ok
    return m.c;
}
""")
write('structs/struct_pointer_field_access_ok.mesa', """
struct Node {
    val: i64,
    fun update(self: *Node, v: i64) void {
        self.val = v;
    }
}
fun f() void {  //~ compile-ok
    let var n: Node = .{val: 0};
    n.update(42);
}
""")
write('structs/struct_in_vec_ok.mesa', """
struct Point { x: f64, y: f64 }
fun f() vec[Point] {  //~ compile-ok
    return vec[.{x: 0.0, y: 0.0}, .{x: 1.0, y: 1.0}];
}
""")
write('structs/self_wrong_type_err.mesa', """
struct A { x: i64 }
struct B { y: i64 }
def _ for A {
    fun f(self: B) void { }  //~ compile-error
}
""")

# ══════════════════════════════════════════════════════════════
# UNIONS
# ══════════════════════════════════════════════════════════════

write('unions/basic_union_ok.mesa', """
union Color { Red, Green, Blue }
fun f(c: Color) i64 {  //~ compile-ok
    return match c {
        Red => {0}, Green => {1}, Blue => {2},
    };
}
""")
write('unions/union_with_payload_ok.mesa', """
union Shape {
    Circle(f64),
    Rectangle(f64, f64),
    Point,
}
fun area(s: Shape) f64 {  //~ compile-ok
    return match s {
        Circle(r)       => { r * r },
        Rectangle(w, h) => { w * h },
        Point           => { 0.0 },
        _               => { 0.0 },
    };
}
""")
write('unions/union_non_exhaustive_err.mesa', """
union Color { Red, Green, Blue }
fun f(c: Color) i64 {
    return match c {
        Red => {0},
        Green => {1},
    };  //~ error: missing variants
}
""")
write('unions/union_wildcard_ok.mesa', """
union Color { Red, Green, Blue }
fun f(c: Color) i64 {  //~ compile-ok
    return match c {
        Red => {0},
        _   => {1},
    };
}
""")
write('unions/union_many_variants_ok.mesa', """
union Token {
    Ident(str),
    Int(i64),
    Float(f64),
    LParen,
    RParen,
    Plus,
    Minus,
    Star,
    Slash,
    Eof,
}
fun is_op(t: Token) bool {  //~ compile-ok
    return match t {
        Plus  => {true},
        Minus => {true},
        Star  => {true},
        Slash => {true},
        _     => {false},
    };
}
""")
write('unions/union_nested_match_ok.mesa', """
union A { X(i64), Y }
union B { P(A), Q }
fun f(b: B) i64 {  //~ compile-ok
    return match b {
        P(a) => { match a { X(n) => {n}, Y => {0}, } },
        Q    => { -1 },
    };
}
""")
write('unions/union_wrong_payload_type_err.mesa', """
union Shape { Circle(f64), Point }
fun f(s: Shape) void {
    let r: i64 = match s {
        Circle(r) => { r },  //~ error: type mismatch
        _         => { 0 },
    };
}
""")

# ══════════════════════════════════════════════════════════════
# INTERFACES
# ══════════════════════════════════════════════════════════════

write('interfaces/basic_interface_ok.mesa', """
interface Area {
    fun area(self: @this) f64;
}
struct Circle { r: f64 }
def Area for Circle {
    fun area(self: Circle) f64 { return self.r * self.r; }
}
fun f(c: Circle) f64 {  //~ compile-ok
    return c.area();
}
""")
write('interfaces/missing_method_err.mesa', """
interface Greet {
    fun greet(self: @this) str;
}
struct Person { name: str }
def Greet for Person {
}  //~ error: missing method
""")
write('interfaces/multiple_interfaces_ok.mesa', """
interface Add { fun add(self: , other: Self) Self; }
interface Neg { fun neg(self: @this) Self; }
struct Vec2 { x: f64, y: f64 }
def Add, Neg for Vec2 {
    fun add(self: Vec2, other: Vec2) Vec2 {
        return .{x: self.x + other.x, y: self.y + other.y};
    }
    fun neg(self: Vec2) Vec2 {
        return .{x: -self.x, y: -self.y};
    }
}
fun f(a: Vec2, b: Vec2) Vec2 {  //~ compile-ok
    return a.add(b);
}
""")
write('interfaces/interface_default_method_ok.mesa', """
interface Describe {
    fun name(self: @this) str;
    fun describe(self: @this) str {
        return self.name();
    }
}
struct Dog { name: str }
def Describe for Dog {
    fun name(self: Dog) str { return self.name; }
}
fun f(d: Dog) str {  //~ compile-ok
    return d.name();
}
""")
write('interfaces/interface_inheritance_ok.mesa', """
interface Eq {
    fun eq(self: , other: Self) bool;
}
interface Ord : Eq {
    fun lt(self: , other: Self) bool;
}
struct MyInt { val: i64 }
def Eq, Ord for MyInt {
    fun eq(self: MyInt, other: MyInt) bool {
        return self.val == other.val;
    }
    fun lt(self: MyInt, other: MyInt) bool {
        return self.val < other.val;
    }
}
fun f(a: MyInt, b: MyInt) bool {  //~ compile-ok
    return a.lt(b);
}
""")

# ══════════════════════════════════════════════════════════════
# OPTIONALS
# ══════════════════════════════════════════════════════════════

write('optionals/return_none_ok.mesa', """
fun f() ?i64 {  //~ compile-ok
    return none;
}
""")
write('optionals/return_value_ok.mesa', """
fun f() ?i64 {  //~ compile-ok
    return 42;
}
""")
write('optionals/orelse_ok.mesa', """
fun f(x: ?i64) i64 {  //~ compile-ok
    return x orelse 0;
}
""")
write('optionals/orelse_default_type_mismatch_err.mesa', """
fun f(x: ?i64) f64 {
    return x orelse 0.0;  //~ error: type mismatch
}
""")
write('optionals/if_unwrap_ok.mesa', """
fun f(x: ?i64) i64 {  //~ compile-ok
    if x |v| { return v; } else { return 0; }
}
""")
write('optionals/if_unwrap_no_else_ok.mesa', """
fun f(x: ?i64) void {  //~ compile-ok
    if x |v| { let y: i64 = v; }
}
""")
write('optionals/chained_orelse_ok.mesa', """
fun f(a: ?i64, b: ?i64) i64 {  //~ compile-ok
    return a orelse b orelse 0;
}
""")
write('optionals/optional_struct_ok.mesa', """
struct Point { x: f64, y: f64 }
fun f(p: ?Point) f64 {  //~ compile-ok
    if p |pt| { return pt.x; } else { return 0.0; }
}
""")
write('optionals/optional_bool_ok.mesa', """
fun f(x: ?bool) bool {  //~ compile-ok
    return x orelse false;
}
""")
write('optionals/none_type_mismatch_err.mesa', """
fun f() i64 {
    return none;  //~ error:
}
""")

# ══════════════════════════════════════════════════════════════
# GENERICS
# ══════════════════════════════════════════════════════════════

write('generics/identity_ok.mesa', """
fun identity[T](x: T) T {  //~ compile-ok
    return x;
}
""")
write('generics/pair_struct_ok.mesa', """
struct Pair[A, B] {
    first: A,
    second: B,
}
fun swap[A, B](p: Pair[A, B]) Pair[B, A] {  //~ compile-ok
    return .{first: p.second, second: p.first};
}
""")
write('generics/generic_first_ok.mesa', """
fun first[T](v: vec[T]) ?T {  //~ compile-ok
    return none;
}
""")
write('generics/generic_struct_method_ok.mesa', """
struct Box[T] {
    val: T,
    fun get(self: Box[T]) T { return self.val; }
}
fun f(b: Box[i64]) i64 {  //~ compile-ok
    return b.val;
}
""")

# ══════════════════════════════════════════════════════════════
# MEMORY / POINTERS
# ══════════════════════════════════════════════════════════════

write('memory/pointer_basic_ok.mesa', """
fun f() i64 {  //~ compile-ok
    let var x: i64 = 42;
    let p: *i64 = @x;
    return *p;
}
""")
write('memory/pointer_mutation_ok.mesa', """
fun f() i64 {  //~ compile-ok
    let var x: i64 = 0;
    let p: *i64 = @x;
    *p = 42;
    return x;
}
""")
write('memory/struct_pointer_ok.mesa', """
struct Point { x: f64, y: f64 }
fun zero(p: *Point) void {  //~ compile-ok
    p.x = 0.0;
    p.y = 0.0;
}
""")
write('memory/deref_non_pointer_err.mesa', """
fun f() void {
    let x: i64 = 5;
    let y = *x;  //~ error:
}
""")

# ══════════════════════════════════════════════════════════════
# PATTERNS / MATCH
# ══════════════════════════════════════════════════════════════

write('patterns/match_ident_binding_ok.mesa', """
fun f(n: i64) i64 {  //~ compile-ok
    return match n {
        x => { x * 2 },
    };
}
""")
write('patterns/match_union_binding_ok.mesa', """
union Opt { Some(i64), None }
fun f(o: Opt) i64 {  //~ compile-ok
    return match o {
        Some(v) => { v },
        None    => { 0 },
    };
}
""")
write('patterns/match_union_no_payload_ok.mesa', """
union Dir { North, South, East, West }
fun dx(d: Dir) i64 {  //~ compile-ok
    return match d {
        East  => {  1 },
        West  => { -1 },
        _     => {  0 },
    };
}
""")
write('patterns/match_nested_union_ok.mesa', """
union Expr {
    Lit(i64),
    Neg(i64),
}
fun eval(e: Expr) i64 {  //~ compile-ok
    return match e {
        Lit(n) => { n },
        Neg(n) => { -n },
    };
}
""")
write('patterns/match_arm_shadow_ok.mesa', """
fun f(n: i64) i64 {  //~ compile-ok
    let x: i64 = 99;
    return match n {
        0 => { x },
        x => { x * 2 },
    };
}
""")

# ══════════════════════════════════════════════════════════════
# VEC / COLLECTIONS
# ══════════════════════════════════════════════════════════════

write('collections/vec_literal_ok.mesa', """
fun f() vec[i64] {  //~ compile-ok
    return vec[1, 2, 3];
}
""")
write('collections/vec_empty_ok.mesa', """
fun f() vec[i64] {  //~ compile-ok
    return vec[];
}
""")
write('collections/vec_index_ok.mesa', """
fun f(v: vec[i64]) i64 {  //~ compile-ok
    return v[0];
}
""")
write('collections/vec_iter_ok.mesa', """
fun sum(v: vec[i64]) i64 {  //~ compile-ok
    let var t: i64 = 0;
    for x in v { t += x; }
    return t;
}
""")
write('collections/vec_float_ok.mesa', """
fun f() vec[f64] {  //~ compile-ok
    return vec[1.0, 2.0, 3.0];
}
""")
write('collections/vec_struct_ok.mesa', """
struct Point { x: f64, y: f64 }
fun f() vec[Point] {  //~ compile-ok
    return vec[.{x: 0.0, y: 0.0}];
}
""")
write('collections/array_literal_ok.mesa', """
fun f() void {  //~ compile-ok
    let arr = [1, 2, 3];
}
""")

# ══════════════════════════════════════════════════════════════
# TUPLES
# ══════════════════════════════════════════════════════════════

write('tuples/named_tuple_ok.mesa', """
fun f() .{x: f64, y: f64} {  //~ compile-ok
    return .{x: 1.0, y: 2.0};
}
""")
write('tuples/unnamed_tuple_ok.mesa', """
fun f() .{f64, f64} {  //~ compile-ok
    let t: .{f64, f64} = .{1.0, 2.0};
    return t;
}
""")
write('tuples/tuple_coerce_int_ok.mesa', """
fun f() .{f64, f64} {  //~ compile-ok
    let t: .{f64, f64} = .{1, 2};
    return t;
}
""")

# ══════════════════════════════════════════════════════════════
# COMPLEX / INTEGRATION
# ══════════════════════════════════════════════════════════════

write('complex/fibonacci_ok.mesa', """
fun fib(n: i64) i64 {  //~ compile-ok
    if n <= 1 { return n; }
    return fib(n - 1) + fib(n - 2);
}
""")
write('complex/fizzbuzz_ok.mesa', """
fun fizzbuzz(n: i64) i64 {  //~ compile-ok
    let var count: i64 = 0;
    for i = 1...n {
        if i % 15 == 0 { count += 1; }
        else if i % 3 == 0 { count += 1; }
        else if i % 5 == 0 { count += 1; }
    }
    return count;
}
""")
write('complex/binary_search_ok.mesa', """
fun search(v: vec[i64], target: i64) ?i64 {  //~ compile-ok
    let var lo: i64 = 0;
    let var hi: i64 = 0;
    while lo <= hi {
        let mid: i64 = (lo + hi) / 2;
        if v[mid] == target { return mid; }
        if v[mid] < target { lo = mid + 1; }
        else { hi = mid - 1; }
    }
    return none;
}
""")
write('complex/particle_system_ok.mesa', """
struct Particle {
    x: f64, y: f64,
    vx: f64, vy: f64,
    alive: bool,

    fun update(self: *Particle, dt: f64) void {
        self.x = self.x + self.vx * dt;
        self.y = self.y + self.vy * dt;
    }

    fun kill(self: *Particle) void {
        self.alive = false;
    }
}

fun simulate(dt: f64) void {  //~ compile-ok
    let var p: Particle = .{x: 0.0, y: 0.0, vx: 1.0, vy: 0.5, alive: true};
    for i = 0...100 {
        p.update(dt);
        if p.x > 100.0 { p.kill(); break; }
    }
}
""")
write('complex/state_machine_ok.mesa', """
union State {
    Idle,
    Running(i64),
    Done(i64),
    Error(str),
}

fun transition(s: State, input: i64) State {  //~ compile-ok
    return match s {
        Idle        => { Running(input) },
        Running(n)  => { if n > 100 { Done(n) } else { Running(n + input) } },
        Done(n)     => { Done(n) },
        Error(msg)  => { Error(msg) },
    };
}
""")
write('complex/tree_ok.mesa', """
struct Node {
    val:   i64,
    depth: i64,
}

fun make_node(val: i64, depth: i64) Node {  //~ compile-ok
    return .{val: val, depth: depth};
}

fun sum_depth(nodes: vec[Node]) i64 {
    let var t: i64 = 0;
    for n in nodes { t += n.val * n.depth; }
    return t;
}
""")
write('complex/error_propagation_ok.mesa', """
fun div(a: f64, b: f64) ?f64 {
    if b == 0.0 { return none; }
    return a / b;
}

fun compute(x: f64, y: f64, z: f64) ?f64 {  //~ compile-ok
    let ab = div(x, y) orelse 0.0;
    let bc = div(y, z) orelse 0.0;
    return ab + bc;
}
""")
write('complex/matrix_like_ok.mesa', """
struct Mat2 {
    a: f64, b: f64,
    c: f64, d: f64,
}

fun mul(m: Mat2, n: Mat2) Mat2 {  //~ compile-ok
    return .{
        a: m.a * n.a + m.b * n.c,
        b: m.a * n.b + m.b * n.d,
        c: m.c * n.a + m.d * n.c,
        d: m.c * n.b + m.d * n.d,
    };
}

fun det(m: Mat2) f64 {
    return m.a * m.d - m.b * m.c;
}
""")

# ══════════════════════════════════════════════════════════════
# EDGE CASES
# ══════════════════════════════════════════════════════════════

write('edge_cases/empty_struct_ok.mesa', """
struct Empty {}
fun f(e: Empty) void {  //~ compile-ok
}
""")
write('edge_cases/deeply_nested_if_ok.mesa', """
fun f(a: bool, b: bool, c: bool, d: bool) i64 {  //~ compile-ok
    if a {
        if b {
            if c {
                if d { return 1; } else { return 2; }
            } else { return 3; }
        } else { return 4; }
    } else { return 5; }
}
""")
write('edge_cases/large_literal_ok.mesa', """
fun f() i64 {  //~ compile-ok
    return 9223372036854775807;
}
""")
write('edge_cases/negative_literal_ok.mesa', """
fun f() i64 {  //~ compile-ok
    return -1;
}
""")
write('edge_cases/zero_literal_ok.mesa', """
fun f() i64 {  //~ compile-ok
    return 0;
}
""")
write('edge_cases/empty_string_ok.mesa', """
fun f() str {  //~ compile-ok
    return "";
}
""")
write('edge_cases/string_with_escapes_ok.mesa', """
fun f() str {  //~ compile-ok
    return "hello\\nworld";
}
""")
write('edge_cases/bool_in_optional_ok.mesa', """
fun f() ?bool {  //~ compile-ok
    return true;
}
""")
write('edge_cases/optional_of_optional_ok.mesa', """
fun f() ??i64 {  //~ compile-ok
    return none;
}
""")
write('edge_cases/function_returns_function_ok.mesa', """
fun make_fn() fun(i64) i64 {  //~ compile-ok
    return fun(x: i64) i64 { return x; };
}
""")
write('edge_cases/multiple_assignments_ok.mesa', """
fun f() i64 {  //~ compile-ok
    let var x: i64 = 0;
    x = 1;
    x = 2;
    x = x + 1;
    return x;
}
""")
write('edge_cases/self_ref_without_method_err.mesa', """
fun f() void {
    let x = self;  //~ error:
}
""")
write('edge_cases/type_alias_used_in_fn_ok.mesa', """
let Count := i64
let Rate  := f64

fun compute(n: Count, r: Rate) Rate {  //~ compile-ok
    return r * n;
}
""")
write('edge_cases/scientific_notation_ok.mesa', """
fun f() f64 {  //~ compile-ok
    let G: f64 = 6.674e-11;
    let c: f64 = 3.0e8;
    return G * c;
}
""")
write('edge_cases/chained_method_calls_ok.mesa', """
struct Builder {
    val: i64,
    fun set(self: *Builder, v: i64) void { self.val = v; }
    fun get(self: Builder) i64 { return self.val; }
}
fun f() i64 {  //~ compile-ok
    let var b: Builder = .{val: 0};
    b.set(42);
    return b.get();
}
""")

print("Done generating tests")

# Count them
count = 0
for root, dirs, files in os.walk(BASE):
    for f in files:
        if f.endswith('.mesa'):
            count += 1
print(f"Total .mesa files: {count}")
