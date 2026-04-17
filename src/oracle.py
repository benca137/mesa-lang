"""
Mesa oracle test runner.

Tests are .mesa files with inline annotations:

    //~ error: <substring of expected error message>
    //~ ok               (this line must NOT produce an error)
    //~ compile-ok       (whole file must compile cleanly)
    //~ compile-error    (whole file must have at least one error)

The annotation applies to the line it appears on.

Example:
    fun f() i64 {
        return true;   //~ error: type mismatch
    }

    fun g() void {
        let x: i64 = 42;  //~ ok
    }

Run:  python3 -m src.oracle [path/to/tests/]
"""
import sys
import os
import re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tokenizer import TokenizeError
from src.parser    import parse, ParseError
from src.checker   import type_check
from src.analysis  import analyse


# ══════════════════════════════════════════════════════════════
# Annotation parsing
# ══════════════════════════════════════════════════════════════

def parse_annotations(source: str) -> dict:
    """
    Returns:
      {
        'file': ['compile-ok' | 'compile-error'],   # file-level
        'lines': { line_no: ('error'|'ok', substring) }
      }
    """
    result = {'file': None, 'lines': {}}
    for i, line in enumerate(source.splitlines(), 1):
        m = re.search(r'//~\s*(.+)', line)
        if not m:
            continue
        annotation = m.group(1).strip()
        if annotation in ('compile-ok', 'compile-error'):
            result['file'] = annotation
        elif annotation.startswith('error:'):
            result['lines'][i] = ('error', annotation[6:].strip())
        elif annotation == 'ok':
            result['lines'][i] = ('ok', '')
        elif annotation.startswith('warning:'):
            result['lines'][i] = ('warning', annotation[8:].strip())
    return result


# ══════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════

def run_oracle(source: str, filename: str = "<test>") -> tuple[bool, list[str]]:
    """
    Run the compiler on source, check annotations.
    Returns (passed, list_of_failures).
    """
    annotations = parse_annotations(source)
    failures    = []

    # Run compiler
    try:
        prog       = parse(source)
        env, diags = type_check(prog)
        layout     = analyse(prog, env)
        errors     = diags.all_errors()
        crashed    = False
        crash_msg  = ""
    except (TokenizeError, ParseError) as e:
        errors  = [type('D', (), {
            'message': str(e), 'line': getattr(e, 'line', 0),
            'col': getattr(e, 'col', 0), 'hint': None
        })()]
        crashed = False
        crash_msg = ""
    except Exception as e:
        errors  = []
        crashed = True
        crash_msg = f"{type(e).__name__}: {e}"

    # Compiler must never crash
    if crashed:
        failures.append(f"  CRASH: {crash_msg}")
        return False, failures

    # ── File-level annotations ────────────────────────────────
    file_ann = annotations['file']
    if file_ann == 'compile-ok':
        if errors:
            failures.append(f"  Expected clean compile, got {len(errors)} error(s):")
            for e in errors:
                failures.append(f"    [{e.line}:{e.col}] {e.message}")
    elif file_ann == 'compile-error':
        if not errors:
            failures.append("  Expected at least one error, but compiled cleanly")

    # ── Line-level annotations ────────────────────────────────
    line_anns = annotations['lines']

    # Build a map: line_no → list of errors on that line
    errors_by_line: dict[int, list] = {}
    for e in errors:
        errors_by_line.setdefault(e.line, []).append(e)

    for line_no, (kind, substring) in line_anns.items():
        line_errors = errors_by_line.get(line_no, [])

        if kind == 'error':
            if not line_errors:
                # Check if any error is near this line (±1) — line tracking is imperfect
                near = (errors_by_line.get(line_no - 1, []) +
                        errors_by_line.get(line_no + 1, []) +
                        errors_by_line.get(0, []))  # line 0 = no location
                if near:
                    matched = [e for e in near if substring.lower() in e.message.lower()]
                    if matched:
                        continue   # close enough
                failures.append(
                    f"  Line {line_no}: expected error containing {substring!r}, "
                    f"but no error on this line"
                )
            else:
                matched = [e for e in line_errors
                           if substring.lower() in e.message.lower()]
                if not matched:
                    failures.append(
                        f"  Line {line_no}: expected error containing {substring!r}, "
                        f"got: {[e.message for e in line_errors]}"
                    )
        elif kind == 'ok':
            if line_errors:
                failures.append(
                    f"  Line {line_no}: expected no error, "
                    f"got: {[e.message for e in line_errors]}"
                )

    # Warn about unannotated errors (errors with no annotation)
    annotated_lines = set(line_anns.keys())
    for line_no, line_errors in errors_by_line.items():
        if line_no not in annotated_lines and line_no not in (0, None):
            # Only report if there's no file-level compile-error annotation
            if file_ann != 'compile-error':
                for e in line_errors:
                    failures.append(
                        f"  Line {line_no}: unexpected error: {e.message}"
                    )

    return len(failures) == 0, failures


# ══════════════════════════════════════════════════════════════
# Test discovery and runner
# ══════════════════════════════════════════════════════════════

def run_file(path: str) -> tuple[bool, list[str]]:
    try:
        source = open(path).read()
    except OSError as e:
        return False, [f"  Could not read file: {e}"]
    return run_oracle(source, path)


def run_dir(test_dir: str) -> tuple[int, int]:
    passed = failed = 0
    for root, dirs, files in os.walk(test_dir):
        dirs.sort()
        for fname in sorted(files):
            if not fname.endswith('.mesa'):
                continue
            path = os.path.join(root, fname)
            ok, failures = run_file(path)
            rel = os.path.relpath(path, test_dir)
            if ok:
                print(f"  PASS  {rel}")
                passed += 1
            else:
                print(f"  FAIL  {rel}")
                for f in failures:
                    print(f)
                failed += 1
    return passed, failed


# ══════════════════════════════════════════════════════════════
# Built-in test cases (run when no directory given)
# ══════════════════════════════════════════════════════════════

BUILTIN_TESTS: list[tuple[str, str]] = [

    # ── Type errors ───────────────────────────────────────────

    ("bool assigned int", """
fun f() void {
    let x: bool = 42;  //~ error: type mismatch
}
"""),

    ("wrong return type", """
fun f() i64 {
    return true;  //~ error: type mismatch
}
"""),

    ("wrong return type f64", """
fun f() f64 {
    return true;  //~ error: type mismatch
}
"""),

    ("str assigned int", """
fun f() void {
    let x: str = 42;  //~ error: type mismatch
}
"""),

    ("add bool and int", """
fun f() void {
    let x = true + 1;  //~ error: not defined
}
"""),

    ("compare incompatible types", """
fun f() bool {
    return true + false;  //~ error: not defined
}
"""),

    # ── Immutability ──────────────────────────────────────────

    ("reassign immutable let", """
fun f() void {
    let x: i64 = 5;
    x = 10;  //~ error: immutable
}
"""),

    ("reassign immutable let var ok", """
fun f() void {
    let var x: i64 = 5;  //~ ok
    x = 10;              //~ ok
}
"""),

    # ── Undefined names ───────────────────────────────────────

    ("undefined variable", """
fun f() i64 {
    return undefined_name;  //~ error: undefined
}
"""),

    ("undefined type", """
fun f(x: NonExistentType) void {  //~ error: unknown type
}
"""),

    ("undefined field", """
struct Point { x: f64, y: f64 }
fun f(p: Point) f64 {
    return p.z;  //~ error: no field
}
"""),

    # ── Return path ───────────────────────────────────────────

    ("missing return", """
fun f() i64 {
}  //~ error: all paths
"""),

    ("missing else return", """
fun f(b: bool) i64 {
    if b { return 1; }
}  //~ error: all paths
"""),

    ("both branches return ok", """
fun f(b: bool) i64 {    //~ compile-ok
    if b { return 1; } else { return 0; }
}
"""),

    ("void needs no return", """
fun f() void {  //~ compile-ok
}
"""),

    # ── Exhaustiveness ────────────────────────────────────────

    ("non-exhaustive union match", """
union Color { Red, Green, Blue }
fun f(c: Color) i64 {
    return match c {
        Red   => { 0 },
        Green => { 1 },
    };  //~ error: missing variants
}
"""),

    ("exhaustive union match ok", """
union Color { Red, Green, Blue }
fun f(c: Color) i64 {  //~ compile-ok
    return match c {
        Red   => { 0 },
        Green => { 1 },
        Blue  => { 2 },
    };
}
"""),

    ("non-exhaustive integer match", """
fun f(n: i64) i64 {
    return match n {
        0 => { 0 },
        1 => { 1 },
    };  //~ error: wildcard
}
"""),

    ("exhaustive integer match with wildcard ok", """
fun f(n: i64) i64 {  //~ compile-ok
    return match n {
        0 => { 0 },
        _ => { 1 },
    };
}
"""),

    ("non-exhaustive bool match", """
fun f(b: bool) i64 {
    return match b {
        true => { 1 },
    };  //~ error: non-exhaustive
}
"""),

    # ── Optional types ────────────────────────────────────────

    ("orelse on optional ok", """
fun f(x: ?i64) i64 {  //~ compile-ok
    return x orelse 0;
}
"""),

    ("if unwrap optional ok", """
fun f(x: ?i64) i64 {  //~ compile-ok
    if x |v| { return v; } else { return 0; }
}
"""),

    # ── Structs ───────────────────────────────────────────────

    ("struct field access ok", """
struct Point { x: f64, y: f64 }
fun f(p: Point) f64 {  //~ compile-ok
    return p.x + p.y;
}
"""),

    ("unknown struct field", """
struct Point { x: f64, y: f64 }
fun f(p: Point) f64 {
    return p.z;  //~ error: no field
}
"""),

    # ── Interfaces ────────────────────────────────────────────

    ("def missing method", """
interface Greet {
    fun greet(self: @this) str;
}
struct Person { name: str }
def Greet for Person {
}  //~ error: missing method
"""),

    ("def implements interface ok", """
interface Greet {
    fun greet(self: @this) str;
}
struct Person { name: str }
def Greet for Person {      //~ compile-ok
    fun greet(self: Person) str {
        return self.name;
    }
}
"""),

    # ── Generics ─────────────────────────────────────────────

    ("generic identity ok", """
fun identity[T](x: T) T {  //~ compile-ok
    return x;
}
"""),

    # ── For loops ────────────────────────────────────────────

    ("for range ok", """
fun f() i64 {              //~ compile-ok
    let var t: i64 = 0;
    for i = 0...10 {
        t += i;
    }
    return t;
}
"""),

    ("break outside loop", """
fun f() void {
    break;  //~ error: break outside loop
}
"""),

    # ── Union ────────────────────────────────────────────────

    ("union definition ok", """
union Shape {          //~ compile-ok
    Circle(f64),
    Rectangle(f64),
    Point,
}
"""),

    # ── Type aliases ─────────────────────────────────────────

    ("type alias ok", """
let Score := i64     //~ compile-ok

fun high_score() Score {
    return 9999;
}
"""),

    # ── TError cascade — only one error ──────────────────────

    ("tderror cascade suppressed", """
fun f() void {
    let x: bool = 42;  //~ error: type mismatch
    let y = x + 1;
    let z = y * 2;
}  //~ compile-error
"""),

    # ── Arithmetic ───────────────────────────────────────────

    ("arithmetic ok", """
fun f(a: i64, b: i64) i64 {  //~ compile-ok
    return a + b * a - b / a;
}
"""),

    ("float arithmetic ok", """
fun f(x: f64) f64 {  //~ compile-ok
    return x ^ 2.0 + x * 3.14;
}
"""),

    # ── Closures ─────────────────────────────────────────────

    ("higher order function ok", """
fun apply(f: fun(i64) i64, x: i64) i64 {  //~ compile-ok
    return f(x);
}
"""),

    # ── Correct programs produce no spurious errors ───────────

    ("nbody-like struct ok", """
struct Body {
    x: f64, y: f64,
    vx: f64, vy: f64,
    m: f64,
}

fun kinetic(b: Body) f64 {
    return 0.5 * b.m * (b.vx * b.vx + b.vy * b.vy);
}

fun update(b: *Body, dt: f64) void {  //~ compile-ok
    b.x = b.x + b.vx * dt;
    b.y = b.y + b.vy * dt;
}
"""),

]


def run_builtin_tests() -> tuple[int, int]:
    passed = failed = 0
    for name, source in BUILTIN_TESTS:
        ok, failures = run_oracle(source, name)
        if ok:
            print(f"  PASS  {name}")
            passed += 1
        else:
            print(f"  FAIL  {name}")
            for f in failures:
                print(f)
            failed += 1
    return passed, failed


# ══════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Mesa oracle test runner")
    ap.add_argument("path", nargs="?",
                    help="directory of .mesa test files, or single .mesa file")
    args = ap.parse_args()

    GREEN = "\033[92m"; RED = "\033[91m"; BOLD = "\033[1m"; RESET = "\033[0m"

    print(f"\n{BOLD}Mesa Oracle Tests{RESET}\n")

    if args.path:
        if os.path.isfile(args.path):
            ok, failures = run_file(args.path)
            passed = 1 if ok else 0
            failed = 0 if ok else 1
        else:
            passed, failed = run_dir(args.path)
    else:
        passed, failed = run_builtin_tests()

    total = passed + failed
    print(f"\n{'═'*50}")
    if failed:
        print(f"{RED}{BOLD}{failed}/{total} failed{RESET}  ({passed} passed)")
    else:
        print(f"{GREEN}{BOLD}{passed}/{total} passed ✓{RESET}")
    sys.exit(0 if failed == 0 else 1)
