# Mesa Language Design Document
*v0.3 — Working Draft*

Mesa is a compiled, statically-typed, general-purpose programming language. It is designed for anyone who wants Python-level expressiveness in their daily work but wants to reach for low-level control when performance demands it — scientists, engineers, game developers, and application programmers alike.

The language has a particular affinity for scientific computing: first-class uncertain and correlated data types, and unitful types with automatic dimensional analysis, make numerical work natural. But Mesa is not a niche language — a web developer, a game programmer, and a physicist should all find it equally usable.

---

## 1. Design Principles

**Readable without ceremony.** Newlines serve as statement separators. No semicolons required. Minimal keywords. Code reads like intent.

**Safe by default, explicit when needed.** A default garbage-collected allocator means most code never thinks about memory. When performance demands it, any function can opt into an explicit allocator — arena, pool, or custom — without changing the rest of the program.

**Errors are values.** Functions that can fail say so in their return type. The compiler enforces handling. No exceptions, no hidden control flow. Error types are ordinary unions and can carry rich payloads.

**First-class optionals.** Nullable values are expressed as `?T`. There is no null pointer. Absence is always explicit and handled at the call site.

**Scientific types in the language, not in a library.** Uncertain values (`f64 +- f64`), correlated quantities, and unitful types (`float\`N\``, `float\`m/s²\``) are built into the type system. Uncertainty propagates automatically through arithmetic, and dimensional mismatches are compile errors.

**Zero-cost abstractions.** Structs with methods, interfaces, generics — all resolve at compile time with no runtime overhead beyond what the algorithm requires.

**Dynamic dispatch when you ask for it.** `any Interface` and `*any Interface` give you open-ended polymorphism exactly where you need it, with a clear syntax that makes the cost visible.

**Simple, learnable surface area.** A programmer who knows Python or Go should be productive within a day. Every keyword earns its place.

---

## 2. Syntax

### 2.1 Hello World

```mesa
fun main() void {
    println("Hello, world!")
}
```

### 2.2 Variables and Bindings

```mesa
let x: i64 = 42           // immutable
let var y: f64 = 3.14     // mutable
let z = true              // type inferred

// int and float are convenient aliases for i64 and f64
let n: int = 100
let r: float = 2.718

y = y * 2.0
y += 1.0
```

### 2.3 Statement Separators

Newlines terminate statements. A line continues onto the next if the next line begins with a binary operator or `.ident` / `?.ident` (method or field chain), or if the current position is inside matched delimiters `()` or `[]`. Semicolons are accepted anywhere as a synonym.

Block braces do not suppress statement breaks. This matters for constructs like:

```mesa
let r = sqrt_f64(r2)
if r > 1.0 {
    println(r)
}
```

An important exception: `.{...}` is an anonymous struct / tuple literal and may span lines, but it starts a fresh value expression rather than continuing a daisy chain from the previous line.

```mesa
let magnitude = x * x
    + y * y          // continues — leading +
    + z * z

let result = vec
    .filter(is_positive)   // continues — leading .
    .sum()

dbg.print()
.{ok: false, note: "fresh expression"}   // new statement / tail expression
```

### 2.4 One-liner Bodies

Any block body can be replaced with a comma and a single statement:

```mesa
if x > 0, println("positive")
if x > 0, println("pos"); else, println("neg")

for i = 0..10, println(i)
while running, tick()
```

### 2.5 String Interpolation

```mesa
let n: i64 = 42
let name = "Alice"
println("Hello {name}, n={n}, n²={n*n}")
// Hello Alice, n=42, n²=1764
```

### 2.6 Control Flow

```mesa
// if as expression
let abs = x if x >= 0 else -x 

// Suffix ternary — produces ?T, chains with orelse
let label = "positive" if x > 0
let label = "positive" if x > 0 orelse "non-positive"

// for ranges
for i = 0...5 { println(i) }   // 0 1 2 3 4 5  (inclusive)
for i = 0..5  { println(i) }   // 0 1 2 3 4    (exclusive)

// for with filter (colon)
for i = 0..20 : i % 2 == 0 { println(i) }

// match — exhaustive, comma-separated arms
let s = match n {
    0 => "zero",
    1 => "one",
    _ => "other",
}
```

### 2.7 Functions and Implicit Return

```mesa
// Explicit return
fun add(a: i64, b: i64) i64 {
    return a + b
}

// Implicit return — last expression is the return value
fun add(a: i64, b: i64) i64 {
    a + b
}

fun classify(x: int) str {
    if x > 0 { "positive" } else { "non-positive" }
}

// Higher-order
fun apply(f: fun(i64) i64, x: i64) i64 { f(x) }

// Non-capturing lambda
let double = fun(x: i64) i64 { x * 2 }
println(apply(double, 21))   // 42
```

### 2.8 Addresses and Pointers

`&expr` takes the address of a value. `*p` dereferences. `@name` is reserved for compiler directives such as `@this`.

```mesa
fun zero(p: *int) void { *p = 0 }

let var x: int = 99
zero(&x)
println(x)   // 0
```

### 2.9 Generics

Single-type-parameter functions are fully supported. The type is inferred at the call site and the compiler emits a monomorphised C function per concrete instantiation.

```mesa
fun identity[T](x: T) T { x }
fun wrap[T](x: T) T { let y = x; y }
fun first[T](a: T, b: T) T { a }

println(identity(42))        // 42
println(identity(3.14))      // 3.14
println(identity("hello"))   // hello
```

Multi-parameter generics and generics with arithmetic operators on `T` are type-checked but not yet monomorphised.

### 2.10 Packages and Imports

Mesa uses a package-based multi-file compilation model.

```mesa
pkg physics

from math import Vec2

pub struct Body {
    position: Vec2,
    velocity: Vec2,
}
```

- `pkg name` declares which package a file belongs to.
- A package is a set of `.mesa` files that share the same `pkg` declaration.
- `pub` means visible across the whole package.
- No visibility keyword means file-private.
- Imports in normal source files are package-based, for example `from math import Vec2`.
- Standard library packages are imported without a `std.` prefix, for example `import mem` and `import io`.

An externally importable package exposes a curated public surface through a package facade file named `<pkgname>.pkg`.
It is a facade, not a header file.

Example:

```mesa
pkg physics

from "world/state" export World
opaque from "world/state" export Body
from "world/api" export createWorld, addBody, step
```

Current package model:

- `build.mesa` defines package roots and executable targets.
- `pkg` is optional. Files without `pkg` are local to the target subtree and are not importable as packages.
- Subdirectories inside a package are organizational only unless a file explicitly declares a different `pkg`.
- `export` exists only inside `<pkgname>.pkg`.
- `opaque` applies only to explicitly named type exports in `<pkgname>.pkg`.
- The compiler parses files separately, compiles each source file to its own generated C translation unit, compiles those to objects, and links them into the final binary.
- Package/type/function symbols are mangled with package-aware names to avoid collisions across packages.

The more detailed design for this system lives in [`design/pkg.md`](design/pkg.md).

### 2.11 Tests

Mesa has built-in test declarations:

```mesa
test "arena init works" {
    let arena = mem.ArenaAllocator.init(mem.PageBuffer.init(64))
    with arena : .reset {
        @assert(true)
    }
}
```

- `test "name" { ... }` declares a test block.
- Test bodies are ordinary Mesa code.
- `@assert(expr)` is the assertion primitive.
- Outside a test block, failed `@assert` aborts like a panic.
- Inside a test block, failed `@assert` records a test failure instead of aborting immediately.
- `mesa test` builds a generated test runner, executes all discovered test blocks, and prints a summary report.

---

## 3. Type System

### 3.1 Primitives

| Type | C equivalent | Notes |
|------|-------------|-------|
| `i8` – `i64`, `u8` – `u64` | `int8_t` … `uint64_t` | Integer widening in arithmetic |
| `int` | `int64_t` | Alias for `i64` |
| `f32`, `f64` | `float`, `double` | IEEE 754 |
| `float` | `double` | Alias for `f64` |
| `bool` | `int` (0/1) | |
| `str` | `{ char* data; int64_t len }` | Immutable slice, not null-terminated |
| `void` | `void` | No value |

### 3.2 Optionals

```mesa
fun safe_div(a: i64, b: i64) ?i64 {
    return a / b if b != 0 else none
}

let r = safe_div(10, 0) orelse -1     // -1

if safe_div(10, 2) |v| {
    println("got {v}")
}
```

### 3.3 Error Unions

A function that can fail declares its error set on the left of `!` and its return type on the right. Error sets use the `error` keyword (distinct from regular unions) and can carry payloads.

```mesa
error ParseError { InvalidChar(str), Overflow, Empty }

fun parse_int(s: str) ParseError!i64 {
    if s.len == 0 { return .Empty }
    // ...
}

// Propagate — requires E!T return type or a handle block
let n = try parse_int(input)

// Handle locally
let n = try parse_int(input) catch {
    .InvalidChar(c) => { println("bad char: {c}"); 0 },
    .Overflow       => max_int,
    .Empty          => 0,
}
```

`E!void` is valid and means “this function may fail, but on success it produces no value”. A successful `E!void` function may simply reach the end of the body without an explicit happy-path `return`.

```mesa
error ValidateError { Empty }

fun ensure_name(name: str) ValidateError!void {
    if name.len == 0 { return .Empty }
}
```

### 3.4 Handle Blocks

`try` can be used in a `void` function if the function has a `handle |e| { }` clause. Any `try` failure inside the body jumps to the handle block. Cleanup code before the jump is still run, and cleanup-bearing `with` blocks run their cleanup after the handle finishes.

```mesa
error E { Bad, Ugly }

fun process(n: int) void {
    let conn = try connect()
    !defer conn.disconnect() // defer on error in scope
    
    let r = try fetch(n)
    println(r)
} handle |e| {
    match e {
        .Bad  => println("bad"),
        .Ugly => println("ugly"),
    }
} // cleanup runs after handle
```

The handle block is optional. Without it, `try` is only valid in functions that return `E!T`.

When all handled `try` sites agree on a single error set, the binding `e` has that error-set type and can be matched with `.Variant` arms normally.

```mesa
error BatchError { NegativeInput, EmptyBatch }

fun run(xs: vec[int]) void {
    let n = try parse_batch(xs)
    println(n)
} handle |e| {
    match e {
        .NegativeInput => println("negative"),
        .EmptyBatch    => println("empty"),
    }
}
```

Current implementation note: handle bindings expose the error tag, not an error payload value, so payload destructuring in a handle match is not supported yet.

`handle` is for errors produced inside the current function or `with` body. It is not intended as the catch point for region-escape operations such as `esc`, whose failures conceptually belong to leaving the current allocator region rather than to the inner body itself.

### 3.4.1 `defer` And `!defer`

Mesa has two lexical defers:

```mesa
defer close_file()
!defer rollback()
```

- `defer` runs when leaving the current lexical block on any exit path.
- `!defer` runs only on error exits from the current lexical block.
- Both run in LIFO order.

Within a cleanup-bearing `with`, the ordering is:

1. body-local `defer` / `!defer`
2. local `handle`, if an inner `try` jumps there
3. defers inside the handle block
4. allocator cleanup such as `.reset` / `.free`

So `!defer` inside a `with` body is an error-path cleanup for that body. It does not wait until after the handle block.

### 3.5 Uncertain Types

Uncertain values carry a measurement and its uncertainty. Arithmetic automatically propagates uncertainty using standard first-order error propagation rules.

```mesa
let mass = 9.109e-31 +- 1e-35
let charge = 1.602e-19 +- 1e-24

let ratio = charge / mass
println(ratio)
println(@typeof(ratio)) // f64 +- f64
// ratio.value ≈ 1.759e11
// ratio.uncertainty computed automatically
```

### 3.6 Unitful Types

Physical units are part of the type. The compiler tracks dimensional analysis automatically — adding metres to newtons is a compile error, and multiplying newtons by metres derives joules.

```mesa
let F  = 10.0`N`        // float`N`  — force in newtons
let dx = 5.0`m`         // float`m`  — distance in metres
let W  = F * dx         // float`J`  — compiler derives N*m = J
let t  = 2.0`s`
let P  = W / t          // float`W`  — power in watts (J/s)

// Uncertain and unitful compose:
let F = 10.0 +- 0.5 `N`   // (float +- float)`N`

// User-defined units
let `furlong` := 201.168`m`
let `lbs`     := 0.453592`kg`
```

### 3.7 Structs, Unions, Interfaces

```mesa
struct Vec2 { x: f64, y: f64 }

union Shape { Circle(f64), Rect(f64, f64), Point }

// @this refers to the implementing type in interface signatures
interface Drawable {
    fun draw(self: @this) void
    fun bounds(self: @this) Rect
}

// Interface implementation — always in a def block
def Drawable for Vec2 {
    fun draw(self: Vec2)   void { println("({self.x}, {self.y})") }
    fun bounds(self: Vec2) Rect { Rect(.{x: self.x, y: self.y, w: 0.0, h: 0.0}) }
}

// Pattern matching over union payloads
let my_shape = Shape.Circle(10.0)
match my_shape {
    .Circle(r) => println("Radius {r}"),
    .Rect(w, h) => println("Width {w}, Height {h}"),
    .Point => println("No extension!")
}
```

---

## 4. Dynamic Interface References ★

Mesa has two forms for dynamic dispatch through interfaces. Both use a **vtable** — a static table of function pointers generated automatically for each `def Interface for ConcreteType` block. *NOTE: any Interface has been deprecated

```mesa
any Drawable    // stack-allocated existential
*any Drawable   // heap-allocated fat pointer
```

The `any` keyword makes dynamic dispatch explicit. `*ConcreteType` remains a plain pointer to a known concrete type — no vtable involved.

### `any Interface` — stack existential (DEPRECATED)

A 32-byte value on the stack: vtable pointer + a 16-byte union (inline data or heap pointer) + an inline flag. Concrete types ≤ 16 bytes are stored directly in the buffer with no allocation. Larger types spill to the heap automatically. *NOTE: now *any Interface silently is on the stack when it fits*

### `*any Interface` — heap fat pointer

A single pointer to a heap block containing the vtable pointer followed immediately by the concrete value. Allocated via the current allocator context.

### Choosing between them

| | `any Interface` | `*any Interface` |
|---|---|---|
| Storage | 32 bytes on stack | 1 pointer (8 bytes) |
| Allocation | None for ≤16 byte types | Always allocates |
| Copying | Copies 32 bytes | Copies a pointer |
| Collections | Contiguous, cache-friendly | Pointer indirection |

---

## 5. Memory Model

Mesa's memory model is built around an **allocator stack**. At the bottom is always the GC. Explicit allocators can be pushed on top for regions of code where you want direct control. GC can be excluded entirely from build given the right build configuration.

### 5.1 The Allocator Stack

```
[GC]                        // always at the bottom — implicit default
[GC | arena]                // inside a with arena block
[GC | arena | pool]         // inside a nested with pool block
[GC]                        // after both exits
```

The top of the stack is the active allocator. Any `let p: *T = .{...}` inside a `with` block uses the active allocator. Outside any `with` block, the GC is used automatically.

### 5.2 GC Allocator (default)

Every `*T` allocation outside a `with` block goes to the GC. No syntax required — it just works. The current implementation uses a simple mark-and-sweep GC implemented directly in the C preamble. It maintains a linked list of all GC allocations and a shadow stack of roots. Collection is triggered when the heap exceeds a threshold (default 1MB).

```mesa
// These allocate from the GC automatically
let p: *Point = .{x: 1.0, y: 2.0}
let q: *Node  = .{val: 42}
```

The GC implementation is intentionally replaceable. The plan is to evaluate Boehm GC (a mature conservative GC requiring zero codegen changes) or a custom deferred reference-counting scheme once the language is more complete.

### 5.3 ArenaAllocator

A fixed-size bump allocator. Fast O(1) allocation, no individual frees — everything is released at once via `.reset` or `.free`. Backed by a single `malloc` at creation time. Panics on OOM. Future: change to a wrapper for an underlying allocator (e.g. page, bump)

```mesa
let arena = ArenaAllocator(65536)   // 64KB backing buffer

with arena : .reset {
    let p: *Point = .{x: 1.0, y: 2.0}   // allocated from arena
    let q: *Node  = .{val: 42}            // allocated from arena
    // ...
}  // arena.reset() called — bump pointer reset to zero, memory reused

// Block with no cleanup — memory stays until arena.free() is called manually
with arena {
    let r: *Point = .{x: 3.0, y: 4.0}
}
defer arena.free()

let count = with arena : .reset {
    let p: *Point = .{x: 0.0, y: -1.0}
    1
} // arena.reset() called here

// Manual methods
arena.reset()   // reset bump pointer, keep backing buffer
arena.free()    // free the backing buffer entirely
```

### 5.3.1 `with` As An Expression

`with` is both a scope form and an expression form. The block’s tail expression is the value of the `with`.

```mesa
let arena = ArenaAllocator(4096)

let total = with arena : .reset {
    let p: *Point = .{x: 20, y: 22}
    p.x + p.y
}
```

`with` may also have a local `handle`, and the success path plus handle path must produce compatible result types:

```mesa
let summary = with arena : .free {
    try build_summary()
} handle |e| {
    match e {
        .BadInput => .{ok: false, note: "bad input"},
        .Empty    => .{ok: false, note: "empty"},
    }
}
```

For cleanup-bearing `with` blocks (`.reset` / `.free`), cleanup happens on every exit path and runs after any local `handle` block.

That includes early exits such as `return`, `break`, and `continue`, plus error jumps from `try`.

```mesa
let label = with arena : .reset {
    if count == 0, "empty"; else "ok"
}
```

### 5.3.2 Lifetime Rule For Cleanup-Bearing `with`

A cleanup-bearing `with` creates a lexical allocation region. Allocator-backed values created in that region may not escape it.

This is rejected:

```mesa
fun bad() *Point {
    let arena = ArenaAllocator(64)
    return with arena : .reset {
        let p: *Point = .{x: 1.0, y: 2.0}
        p
    }
}
```

This rule applies equally to error payloads. An error payload created inside a cleanup-bearing `with` may be inspected by that `with`'s local `handle`, because cleanup runs after the handle, but it may not propagate outside the `with` unless it is first copied to a longer-lived allocator.

Nested `with` blocks follow the same rule recursively: values tied to the inner region may not survive the inner cleanup unless explicitly copied outward first.

This is allowed:

```mesa
fun good() int {
    let arena = ArenaAllocator(64)
    return with arena : .reset {
        let p: *Point = .{x: 20, y: 22}
        p.x + p.y
    }
}
```

### 5.4 `mem` Package And Allocator Interface

The current memory surface lives in the standard `mem` package.

All allocator types implement the `Allocator` interface and can be passed as `*any Allocator`:

```mesa
import mem

interface Allocator {
    fun alloc(self: *@this, size: int, align: int) *void
    fun realloc(self: *@this, ptr: *void, old_size: int, new_size: int, align: int) *void
    fun free_bytes(self: *@this, ptr: *void, size: int) void
}

fun setup(a: *any Allocator) void { ... }

let arena = mem.ArenaAllocator.init(mem.PageBuffer.init(4096))
setup(arena)   // coerces to *any Allocator automatically
```

The backing-memory direction is layered:

- `mem.PageBuffer` gets backing memory from page allocation
- `mem.CBuffer` gets backing memory from the C allocator
- `mem.FixedBuffer` wraps an existing fixed region
- `mem.ArenaAllocator` is a strategy wrapper over a `*any Buffer`

The intended constructor style is type-associated:

```mesa
import mem

let arena = mem.ArenaAllocator.init(mem.PageBuffer.init(4096))
let page_arena = mem.ArenaAllocator.page(4096)
let c_arena = mem.ArenaAllocator.c(4096)
```

### 5.5 Explicit Allocation To A Specific Allocator

To allocate a single value to a specific allocator without a full `with` block — for example, to target a non-top allocator from inside a block — use the postfix `with` operator:

```mesa
with arena {
    let p: *Point = .{x: 1.0, y: 2.0}            // from arena (top of stack)
    let q: *Node  = .{val: 42} with outer_alloc   // explicitly from a chosen allocator
}
```

This is intended for named, longer-lived allocators that are already in scope.

The same general shape is also the intended long-term story for promoting non-pointer values between allocator regions. For example, `detail with outer` would mean “deep-copy `detail` into `outer` and produce the copied value”.

### 5.6 Fallible Allocators

Infallible allocators (Arena, Pool, GC) panic on OOM. A future `FallibleAllocator` interface will return `AllocError!*T` and require a `handle` clause on the `with` block:

```mesa
with systemAlloc : .free {
    let p: *Point = .{x: 1.0, y: 2.0}
    let q = Point{0, 0}
} handle |e| {
    match e { .OutOfMemory => println("OOM") }
} // free called after handle because of .free cleanup syntax
```

The compiler will enforce that a `with` block using a fallible allocator must have a `handle` clause — the same rule as `try` in a void function.

### 5.7 Escaping Values Out Of A Region

Error payloads and other allocator-backed values eventually need a way to cross cleanup boundaries intentionally.

The current design direction is an `esc` form:

```mesa
with inner : .free {
    let detail = make_detail(inner)
    return .BadThing(esc detail)
}
```

`esc expr` means:

- evaluate `expr` in the current region
- deep-copy it to the next outer allocator region if needed
- produce the copied value
- if promotion fails, propagate that failure outward automatically

So `esc` is conceptually fallible even when spelled without an explicit `try`. It is only valid in a fallible context, such as a function returning `E!T` or another enclosing context that can absorb the failure.

This should also work naturally in tail position:

```mesa
with inner : .free {
    esc make_detail(inner)
}
```

and with an explicit destination allocator when the nearest outer allocator is not the intended owner:

```mesa
with inner : .free {
    let detail = make_detail(inner)
    return .BadThing(detail with outer)
}
```

Design note: `esc` failure should bypass the local `with ... handle` by default. A local handle is for errors produced inside the body; `esc` is about exporting a value out of the region.

---

## 6. Errors and Optionals

### 6.1 Optionals

```mesa
fun find(haystack: []str, needle: str) ?i64 {
    for i = 0...haystack.len {
        if haystack[i] == needle { return i }
    }
    return none
}

let idx = find(words, "mesa") orelse -1

if find(words, "mesa") |i| {
    println("found at index {i}")
}
```

### 6.2 Error Unions

```mesa
error IoError { NotFound(str), PermissionDenied, Interrupted }

fun read_file(path: str) IoError!str { ... }

let contents = try read_file("/etc/hosts") catch {
    .NotFound(p)      => { println("missing: {p}"); "" },
    .PermissionDenied => { println("access denied"); "" },
    .Interrupted      => try read_file(path),
}
```

### 6.3 Handle Blocks

```mesa
fun process(path: str) void {
    let data = try read_file(path)
    let n    = try parse_int(data)
    println(n)
} handle |e| {
    println("failed")
}
```

Any `try` failure jumps to the `handle` block. When the handled `try` sites agree on one error set, the binding `e` is typed as that error set for matching purposes; internally it is still represented as an error tag. The compiler enforces that `try` in a void function requires a `handle` clause.

---

## 7. Scientific Types ★

### 7.1 Uncertain Values

```mesa
let g:  f64 +- f64 = 9.81 +- 0.01
let h:  f64 +- f64 = 1.50 +- 0.005

let t = (2.0 * h / g).sqrt()
// t.value ≈ 0.553 s, t.uncertainty computed via ∂t/∂h and ∂t/∂g
```

### 7.2 Unitful Types

```mesa
let F  = 10.0`N`
let dx = 5.0`m`
let W  = F * dx    // float`J` — compiler derives N·m = J

// Arithmetic rules
let v = 10.0`m` / 2.0`s`     // float`m/s`
let r = 5.0`m` / 5.0`m`      // float`1` → plain float (dimensionless)

// Unit arithmetic errors at compile time
let bad = F + dx   // error: cannot add N and m

// Dynamic units — runtime-tracked
let x: float`?` = sensor.read()

// User-defined units
let `furlong` := 201.168`m`
let `mph`     := 0.44704`m/s`

// Uncertain + unitful
let F = 10.0 +- 0.5 `N`    // (float +- float)`N`

// Field access
F.value    // 10.0 — plain float, units stripped
F.units    // "N"  — unit name as string
```

---

## 8. Compilation

Mesa compiles to C99 via a multi-pass pipeline.

| Pass | Description |
|------|-------------|
| Tokenizer | Source → tokens. Inserts virtual NEWLINE tokens at statement boundaries. |
| Parser | Tokens → AST. Recursive descent + Pratt operator precedence. |
| Type checker | Two-pass: declare all types/functions, then check expressions. |
| Analysis | Definite assignment, exhaustive match, unreachable code, return paths. |
| C codegen | Typed AST → C99. Minimal runtime emitted as a static preamble. |
| C compiler | C99 → native binary. Uses cc/gcc/clang. |

### CLI

```
mesa file.mesa              # compile to ./out
mesa file.mesa -o name      # compile to ./name
mesa file.mesa --emit-c     # print generated C
mesa file.mesa --check      # type-check only
mesa --version
```

---

## 9. Implementation Status

| Feature | Status | Notes |
|---------|--------|-------|
| Primitives (i8–i64, u8–u64, f32, f64, bool, str) | ✅ done | Integer widening, float promotion |
| `int` / `float` aliases | ✅ done | Aliases for `i64` / `f64` |
| Let bindings, type inference | ✅ done | |
| Implicit return (last expression) | ✅ done | Explicit `return` still works |
| Functions, recursion, higher-order | ✅ done | |
| Non-capturing lambdas | ✅ done | Capturing closures deferred |
| Structs with methods | ✅ done | Value + pointer receivers |
| Tagged unions with `.Variant` syntax | ✅ done | Unit-only unions → C enum, payload → tagged struct |
| Exhaustive match | ✅ done | Union, integer, bool |
| Interfaces (`interface` / `def`) | ✅ done | `@this` for the implementing type |
| `any Interface` stack existential | ✅ done | 32-byte SBO, inline ≤ 16 bytes |
| `*any Interface` heap fat pointer | ✅ done | vtable + flexible data array |
| Optionals (`?T`, `none`, `orelse`) | ✅ done | |
| Suffix ternary (`expr if cond`) | ✅ done | Produces `?T`, chains with `orelse` |
| String interpolation | ✅ done | Type-correct format dispatch |
| Type aliases | ✅ done | |
| Newline as statement separator | ✅ done | `.ident` continues; `.{...}` starts a new expression |
| One-liner bodies (`if cond, stmt`) | ✅ done | Works for if, for, while |
| For filter (`for i = 0..n : cond`) | ✅ done | |
| Stack pointers (`*T`, `&expr`) | ✅ done | |
| For/while/break/continue | ✅ done | |
| Error sets (`error E { ... }`) | ✅ done | Distinct from regular unions |
| `try` / `catch` | ✅ done | Full codegen with goto-based handle blocks |
| `handle |e| { }` blocks | ✅ done | Void functions with typed tag matching when error set is known |
| `E!void` | ✅ done | No explicit success-path return required |
| Generics (single type param) | ✅ done | Monomorphised C functions per instantiation |
| Unitful types (static `float\`N\``) | ✅ done | SI registry, zero runtime overhead |
| Unit aliases (`let \`N\` := …`) | ✅ done | User-defined units |
| Dynamic units (`float\`?\``) | ✅ done | Runtime unit descriptor |
| `mem` package | ✅ done | Bare std import, source-backed package |
| `Allocator` interface | ✅ done | Source-defined allocator contract |
| `Buffer` interface | ✅ done | Source-defined backing-memory contract |
| `PageBuffer` / `CBuffer` / `FixedBuffer` | ✅ done | Concrete backing buffers |
| `ArenaAllocator` | ✅ done | Source-defined arena over `*any Buffer` |
| GC allocator (implicit default) | ✅ done | Mark-and-sweep, fires when heap > 1MB |
| `with` allocator context | ✅ done | Allocator stack, cleanup on block exit |
| `with` as expression | ✅ done | Local `handle` supported; cleanup runs after handle |
| `test "name" { ... }` | ✅ done | Runs via `mesa test` |
| `@assert` in tests | ✅ done | Records failure instead of panicking |
| Uncertain types (`f64 +- f64`) | 🔶 partial | Types exist; arithmetic propagation pending |
| Generics with arithmetic on T | 🔶 partial | Type-checked; multi-param generics pending |
| Correlated types | 🔶 partial | Types exist; tracking pending |
| Vec / slice codegen | ✅ done | Literals, iteration, len/cap, and comprehensions |
| `defer` / `!defer` | ✅ done | Lexical LIFO cleanup on normal and error exits |
| `mem` bare std import | ✅ done | `import mem`, `import io` |
| `std.mem.gc` singleton | 🔷 planned | Explicit GC escape hatch still future work |
| Fallible allocators | 🔷 planned | `AllocError!*T`, handle clause required |
| `esc` region promotion | 🔷 planned | Implicitly fallible promotion to outer allocator |
| GC improvement | 🔷 planned | Evaluate Boehm GC or deferred RC |
| Closures with capture | ⏸ deferred | Pending memory model stabilisation |
| Package declarations (`pkg`) | ✅ done | Optional for target-local files |
| Separate compilation / packages | ✅ done | Per-file C/object emission and link step |
| `<pkgname>.pkg` facades | ✅ done | Curated package export surfaces |
| `build.mesa` | ✅ done | Pure Mesa build entrypoint and CLI wiring |
| Standard library | 🔶 partial | `mem` and `io` active; more to come |

---

## 10. Roadmap

### Near term — language completeness
- Uncertain arithmetic propagation (∂f/∂x error propagation)
- Generics with arithmetic operators on T, multi-param generics
- `catch` with payload binding (`.NotFound(path) => use path`)
- Error payload binding in `handle |e| { ... }`
- More pure-Mesa tests replacing Python-only scaffolding

### Medium term — memory model completion
- `std.mem.gc` singleton for explicit GC escape from within `with` blocks
- `esc` value promotion to outer allocator / explicit allocator target
- Fallible allocator interface with handle clause enforcement
- GC improvement — Boehm GC or deferred reference counting
- Shadow stack registration for GC roots (currently only struct literal allocs tracked)
- Additional allocators layered over buffers, including debug and pool forms
- Closures with capture (pending memory model stabilisation)

### Longer term — ecosystem
- Standard library growth — `math`, `fs`, `time`, `process`
- Native backend (x86-64 / ARM64)
- WebAssembly target
- Package manager
- Language server (LSP)

---

## 11. C Runtime Preamble

The Mesa compiler currently emits its runtime support through generated C support code and shared headers. The current runtime layer includes:

- **Mesa runtime types** — `mesa_str`, `mesa_unitful`
- **Built-in print functions** — `mesa_println_str/i64/f64/bool`
- **Low-level memory primitives** — page allocation, C allocation, raw copy/set/compare helpers
- **GC** — `Mesa_GC_Obj`, `Mesa_GC_Frame`, shadow stack, `mesa_gc_alloc/collect`
- **Allocator stack support** — ambient allocation context for `with` / `esc`
- **Test runtime** — per-test begin/end, assertion collection, pretty summary reporting
- **Optional types** — `mesa_opt_T` per concrete optional type used
- **Error result types** — `Mesa_result_E_T` per error-union type used
- **Existential types** — `Mesa_any_I` per interface used with `any I`

---

## 12. Extended Example

```mesa
error DbError { NotFound(str), Timeout }

struct Record { id: int, value: float }

interface Queryable {
    fun fetch(self: @this, id: int) DbError!*Record
}

struct InMemoryDb { }

def Queryable for InMemoryDb {
    fun fetch(self: InMemoryDb, id: int) DbError!*Record {
        if id < 0 { return .NotFound("negative id") }
        // GC-allocated — no allocator needed at call site
        let r: *Record = .{id: id, value: id * 2.0}
        r
    }
}

fun load_and_print(db: any Queryable, id: int) void {
    let r = try db.fetch(id)
    println(r.value)
} handle |e| {
    println("error fetching record")
}

fun benchmark() void {
    let arena = mem.ArenaAllocator.page(1024 * 1024)   // 1MB arena

    with arena : .reset {
        for i = 0..100 {
            let r: *Record = .{id: i, value: i * 3.14}
            println(r.value)
        }
    }
}

fun main() void {
    let db: InMemoryDb = .{}
    load_and_print(db, 5)     // 10
    load_and_print(db, -1)    // error fetching record
    benchmark()
}
```

---

*Mesa v0.3 — Working Draft. Compiler ~16,100 lines Python. 178 exec tests + 1,000 oracle tests passing.*
*★ = feature unique to Mesa among compiled languages.*
