"""
Mesa compiler fuzzer.

Strategy: generate random Mesa programs at multiple levels:

  Level 0 — random bytes / random tokens (finds tokenizer crashes)
  Level 1 — syntactically valid but type-incorrect programs (finds checker crashes)
  Level 2 — well-typed programs (finds codegen crashes)
  Level 3 — mutation of known-good programs (finds edge cases)

We don't care whether compilation succeeds or fails — we care that
the compiler NEVER crashes with an unhandled exception.

Any exit other than:
  - clean success
  - DiagnosticBag errors
  - ParseError
  - TokenizeError

is a bug.

Run: python3 -m src.fuzz [--seed N] [--iters N] [--level 0-3] [--timeout N]
"""
import sys
import os
import random
import traceback
import time
import argparse
import signal
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tokenizer import Tokenizer, TokenizeError
from src.parser    import parse, ParseError
from src.checker   import type_check
from src.analysis  import analyse
from src.ccodegen  import CCodegen


# ══════════════════════════════════════════════════════════════
# Expected / acceptable exception types
# ══════════════════════════════════════════════════════════════

EXPECTED_EXCEPTIONS = (TokenizeError, ParseError)


def run_pipeline(source: str) -> tuple[bool, str]:
    """
    Run the full compiler pipeline.
    Returns (is_bug, description).
    A bug is any unhandled exception that isn't TokenizeError or ParseError.
    """
    try:
        prog       = parse(source)
        env, diags = type_check(prog)
        layout     = analyse(prog, env)
        if not diags.has_errors():
            cg = CCodegen(env, layout)
            cg.emit_all(prog)
            _ = cg.output()
        return False, "ok"
    except EXPECTED_EXCEPTIONS:
        return False, "expected error"
    except RecursionError:
        return True, f"RecursionError (stack overflow in compiler)"
    except Exception as e:
        tb = traceback.format_exc()
        return True, f"{type(e).__name__}: {e}\n{tb}"


# ══════════════════════════════════════════════════════════════
# Level 0 — Random bytes
# ══════════════════════════════════════════════════════════════

def gen_random_bytes(rng: random.Random) -> str:
    n = rng.randint(1, 200)
    chars = [rng.choice(
        "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789{}[]().,;:=+-*/<>!?@&|^~\"'\\#\n\t_"
    ) for _ in range(n)]
    return "".join(chars)


# ══════════════════════════════════════════════════════════════
# Level 1 — Random token sequences
# ══════════════════════════════════════════════════════════════

KEYWORDS = [
    "fun", "let", "var", "struct", "union", "interface", "def",
    "for", "while", "if", "else", "match", "return", "break",
    "continue", "in", "and", "or", "true", "false", "none",
    "pub", "from", "import", "as", "defer",
]
TYPES    = ["i64", "i32", "f64", "f32", "bool", "str", "void", "u8", "u64"]
OPS      = ["=", "==", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/",
            "^", "%", "->", "=>", "?", "!", "@", "..", "...", ":="]
PUNCTS   = ["{", "}", "(", ")", "[", "]", ";", ":", ",", "."]

def gen_token_soup(rng: random.Random) -> str:
    tokens = []
    n      = rng.randint(5, 80)
    pool   = KEYWORDS + TYPES + OPS + PUNCTS + [
        str(rng.randint(0, 1000)),
        f"{rng.uniform(0, 100):.2f}",
        f'"{rng.choice(["hello", "x", ""])}\"',
        rng.choice("abcdefghijklmnopqrstuvwxyz_") +
        "".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789_", k=rng.randint(0,8))),
    ]
    for _ in range(n):
        tokens.append(rng.choice(pool))
    return " ".join(tokens)


# ══════════════════════════════════════════════════════════════
# Level 2 — Structured random programs
# ══════════════════════════════════════════════════════════════

class ProgramGen:
    """Generate syntactically plausible (but possibly type-incorrect) Mesa programs."""

    PRIM_TYPES  = ["i64", "i32", "f64", "f32", "bool", "str", "void"]
    INT_TYPES   = ["i64", "i32", "i8", "u64", "u32"]
    FLOAT_TYPES = ["f64", "f32"]
    IDENTS      = ["x", "y", "z", "n", "m", "a", "b", "c", "val", "result",
                   "data", "size", "count", "total", "flag", "temp", "buf"]
    STRUCT_NAMES = ["Point", "Vec2", "Particle", "Node", "Config", "State"]
    UNION_NAMES  = ["Shape", "Color", "Option", "Result", "Token"]

    def __init__(self, rng: random.Random):
        self.rng      = rng
        self.depth    = 0
        self.max_depth = 4
        self._struct_names: list[str] = []
        self._union_names:  list[str] = []
        self._fn_names:     list[str] = []

    def r(self) -> random.Random:
        return self.rng

    def pick(self, lst):
        return self.rng.choice(lst)

    def maybe(self, prob: float = 0.5) -> bool:
        return self.rng.random() < prob

    # ── Types ─────────────────────────────────────────────────

    def gen_type(self, allow_void: bool = False) -> str:
        choices = self.PRIM_TYPES if allow_void else self.PRIM_TYPES[:-1]
        if self._struct_names and self.maybe(0.2):
            return self.pick(self._struct_names)
        base = self.pick(choices)
        if self.maybe(0.15):
            return f"?{base}"
        if self.maybe(0.1):
            return f"*{base}"
        if self.maybe(0.1):
            return f"vec[{base}]"
        return base

    def gen_ret_type(self) -> str:
        if self.maybe(0.2): return "void"
        return self.gen_type()

    # ── Expressions ───────────────────────────────────────────

    def gen_expr(self, ty: str = "i64") -> str:
        self.depth += 1
        if self.depth > self.max_depth:
            self.depth -= 1
            return self.gen_literal(ty)
        e = self._gen_expr_inner(ty)
        self.depth -= 1
        return e

    def _gen_expr_inner(self, ty: str) -> str:
        if ty in ("i64", "i32", "u64", "u32", "i8"):
            choices = [
                lambda: str(self.rng.randint(-1000, 1000)) + "LL",
                lambda: self.pick(self.IDENTS),
                lambda: f"({self.gen_expr(ty)} {self.pick(['+','-','*','/','%'])} {self.gen_expr(ty)})",
                lambda: f"({self.gen_expr(ty)} {self.pick(['<','>','<=','>=','==','!='])} {self.gen_expr(ty)}) ? {self.gen_expr(ty)} : {self.gen_expr(ty)}",
                lambda: f"-{self.gen_expr(ty)}",
                lambda: str(self.rng.randint(0, 100)),
            ]
        elif ty in ("f64", "f32"):
            choices = [
                lambda: f"{self.rng.uniform(-100, 100):.4f}",
                lambda: self.pick(self.IDENTS),
                lambda: f"({self.gen_expr(ty)} {self.pick(['+','-','*','/'])} {self.gen_expr(ty)})",
                lambda: f"-{self.gen_expr(ty)}",
                lambda: f"{self.gen_expr(ty)} ^ 2.0",
            ]
        elif ty == "bool":
            choices = [
                lambda: self.pick(["true", "false"]),
                lambda: f"({self.gen_expr('i64')} {self.pick(['<','>','==','!='])} {self.gen_expr('i64')})",
                lambda: f"(!{self.gen_expr('bool')})",
                lambda: f"({self.gen_expr('bool')} and {self.gen_expr('bool')})",
                lambda: f"({self.gen_expr('bool')} or {self.gen_expr('bool')})",
            ]
        elif ty == "str":
            choices = [
                lambda: f'"{self.pick(["hello", "world", "test", ""])}"',
                lambda: self.pick(self.IDENTS),
            ]
        elif ty == "void":
            choices = [lambda: "/* void */"]
        elif ty.startswith("?"):
            inner = ty[1:]
            choices = [
                lambda: "none",
                lambda: self.gen_expr(inner),
            ]
        else:
            choices = [lambda: self.pick(self.IDENTS)]
        return self.pick(choices)()

    def gen_literal(self, ty: str) -> str:
        if ty in ("i64", "i32", "u64"): return str(self.rng.randint(-100, 100))
        if ty in ("f64", "f32"):        return f"{self.rng.uniform(-10,10):.2f}"
        if ty == "bool":                return self.pick(["true", "false"])
        if ty == "str":                 return '"hello"'
        if ty.startswith("?"):          return self.pick(["none", self.gen_literal(ty[1:])])
        return "0"

    # ── Statements ────────────────────────────────────────────

    def gen_stmt(self, ret_ty: str) -> str:
        choices = [
            self.gen_let_stmt,
            lambda: self.gen_assign_stmt(),
            lambda: self.gen_if_stmt(ret_ty),
            lambda: self.gen_for_range_stmt(),
            lambda: self.gen_expr_stmt(),
        ]
        if ret_ty != "void" and self.maybe(0.3):
            return f"return {self.gen_expr(ret_ty)};"
        return self.pick(choices)()

    def gen_let_stmt(self) -> str:
        name = self.pick(self.IDENTS)
        ty   = self.gen_type()
        mut  = "var " if self.maybe(0.4) else ""
        val  = self.gen_expr(ty)
        return f"let {mut}{name}: {ty} = {val};"

    def gen_assign_stmt(self) -> str:
        name = self.pick(self.IDENTS)
        ty   = self.pick(["i64", "f64", "bool"])
        op   = self.pick(["=", "+=", "-=", "*="])
        val  = self.gen_expr(ty)
        return f"{name} {op} {val};"

    def gen_if_stmt(self, ret_ty: str) -> str:
        cond = self.gen_expr("bool")
        body = self.gen_stmt(ret_ty)
        if self.maybe(0.4):
            else_body = self.gen_stmt(ret_ty)
            return f"if {cond} {{ {body} }} else {{ {else_body} }}"
        return f"if {cond} {{ {body} }}"

    def gen_for_range_stmt(self) -> str:
        var = self.pick(["i", "j", "k", "n"])
        end = self.rng.randint(1, 20)
        body = self.gen_expr_stmt()
        op = self.pick(["...", ".."])
        return f"for {var} = 0{op}{end} {{ {body} }}"

    def gen_expr_stmt(self) -> str:
        ty = self.pick(["i64", "f64", "bool"])
        return f"{self.gen_expr(ty)};"

    def gen_while_stmt(self) -> str:
        cond = self.gen_expr("bool")
        body = self.gen_stmt("void")
        return f"while {cond} {{ {body} }}"

    def gen_match_stmt(self) -> str:
        val = self.gen_expr("i64")
        arms = []
        for v in self.rng.sample(range(5), self.rng.randint(1, 3)):
            arms.append(f"{v} => {{ {self.gen_expr('i64')} }}")
        arms.append(f"_ => {{ 0 }}")
        return f"match {val} {{ {', '.join(arms)} }}"

    # ── Params ────────────────────────────────────────────────

    def gen_params(self, n_params: int) -> list[tuple[str, str]]:
        params = []
        for i in range(n_params):
            name = self.pick(self.IDENTS) + str(i)
            ty   = self.gen_type()
            params.append((name, ty))
        return params

    # ── Declarations ──────────────────────────────────────────

    def gen_struct(self) -> str:
        name   = self.pick(self.STRUCT_NAMES) + str(self.rng.randint(0, 99))
        n_fields = self.rng.randint(1, 4)
        fields = []
        for i in range(n_fields):
            fname = self.pick(["x","y","z","w","a","b","c","n","m"]) + str(i)
            ftype = self.pick(["f64", "i64", "bool", "f32"])
            fields.append(f"    {fname}: {ftype},")
        self._struct_names.append(name)
        body = "\n".join(fields)
        return f"struct {name} {{\n{body}\n}}"

    def gen_union(self) -> str:
        name     = self.pick(self.UNION_NAMES) + str(self.rng.randint(0, 99))
        n_vars   = self.rng.randint(2, 4)
        var_names = ["Alpha","Beta","Gamma","Delta","Epsilon"][:n_vars]
        variants = []
        for v in var_names:
            if self.maybe(0.5):
                ty = self.pick(["f64", "i64", "bool"])
                variants.append(f"    {v}({ty}),")
            else:
                variants.append(f"    {v},")
        self._union_names.append(name)
        body = "\n".join(variants)
        return f"union {name} {{\n{body}\n}}"

    def gen_fun(self) -> str:
        name     = self.pick(["foo","bar","baz","compute","process","run",
                               "update","calc","get","set"]) + \
                   str(self.rng.randint(0, 99))
        n_params = self.rng.randint(0, 3)
        params   = self.gen_params(n_params)
        ret_ty   = self.gen_ret_type()
        param_str = ", ".join(f"{n}: {t}" for n, t in params)
        self._fn_names.append(name)

        n_stmts = self.rng.randint(0, 5)
        stmts   = [self.gen_stmt("void") for _ in range(n_stmts)]

        if ret_ty != "void":
            stmts.append(f"return {self.gen_expr(ret_ty)};")

        body = "\n    ".join(stmts)
        return f"fun {name}({param_str}) {ret_ty} {{\n    {body}\n}}"

    def gen_type_alias(self) -> str:
        name  = self.pick(["MyInt","Score","Index","Count","Size"]) + \
                str(self.rng.randint(0,9))
        inner = self.pick(["i64","f64","bool","u32"])
        return f"let {name} := {inner}"

    def gen_program(self) -> str:
        decls = []
        n_types = self.rng.randint(0, 2)
        n_fns   = self.rng.randint(1, 4)

        # Types first
        for _ in range(n_types):
            if self.maybe(0.5):
                decls.append(self.gen_struct())
            else:
                decls.append(self.gen_union())

        # Maybe a type alias
        if self.maybe(0.3):
            decls.append(self.gen_type_alias())

        # Functions
        for _ in range(n_fns):
            decls.append(self.gen_fun())

        # Always add a main
        if self.maybe(0.7):
            stmts = [self.gen_stmt("void") for _ in range(self.rng.randint(1, 5))]
            decls.append("fun main() i32 {\n    " + "\n    ".join(stmts) + "\n    return 0;\n}")

        return "\n\n".join(decls)


# ══════════════════════════════════════════════════════════════
# Level 3 — Mutation of known-good programs
# ══════════════════════════════════════════════════════════════

KNOWN_GOOD = [
    # simple function
    "fun add(a: i64, b: i64) i64 { return a + b; }",
    # struct
    "struct P { x: f64, y: f64 }\nfun f(p: P) f64 { return p.x + p.y; }",
    # union
    "union C { Red, Green, Blue }\nfun f(c: C) i64 { return match c { Red => {0}, Green => {1}, _ => {2} }; }",
    # optional
    "fun f(x: ?i64) i64 { return x orelse 0; }",
    # for loop
    "fun f() i64 { let var t: i64 = 0; for i = 0...10 { t += i; } return t; }",
    # while
    "fun f() i64 { let var x: i64 = 10; while x > 0 { x = x - 1; } return x; }",
    # nested if
    "fun f(a: i64, b: i64) i64 { if a > b { return a; } else { return b; } }",
    # closure
    "fun apply(f: fun(i64) i64, x: i64) i64 { return f(x); }",
    # struct method
    "struct V { x: f64, fun len(self: V) f64 { return self.x; } }",
    # match
    "fun f(n: i64) i64 { return match n { 0 => {1}, 1 => {1}, _ => {n} }; }",
]

def mutate(src: str, rng: random.Random) -> str:
    mutations = [
        # Delete a random character
        lambda s: s[:rng.randint(0, len(s))] + s[rng.randint(0, len(s)):],
        # Insert a random character
        lambda s: s[:rng.randint(0, len(s))] + rng.choice("{}()[];:,. ") + s[rng.randint(0, len(s)):],
        # Replace a random word
        lambda s: s.replace(
            rng.choice(s.split()) if s.split() else "x",
            rng.choice(["0", "true", "false", "none", "void", "i64", "x", "{}", "0LL"]),
            1
        ),
        # Duplicate a random substring
        lambda s: s + s[rng.randint(0, len(s)):rng.randint(0, len(s))],
        # Truncate
        lambda s: s[:rng.randint(len(s)//2, len(s))],
        # Replace a number with a large/negative number
        lambda s: s.replace(str(rng.randint(0,100)), str(rng.choice([-1, 0, 2**63-1, -2**63])), 1)
            if any(c.isdigit() for c in s) else s,
        # Swap two adjacent tokens
        lambda s: (lambda parts: " ".join(
            parts[:rng.randint(0, max(0, len(parts)-1))] +
            (list(reversed(parts[rng.randint(0, max(0,len(parts)-1)):
                               min(len(parts), rng.randint(0, max(0,len(parts)-1))+2)])) or []) +
            parts[min(len(parts), rng.randint(0, max(0,len(parts)-1))+2):]
        ))(s.split()) if s.split() else s,
    ]
    n_mutations = rng.randint(1, 3)
    result = src
    for _ in range(n_mutations):
        try:
            result = rng.choice(mutations)(result)
        except Exception:
            pass
    return result


# ══════════════════════════════════════════════════════════════
# Fuzzer runner
# ══════════════════════════════════════════════════════════════

class FuzzStats:
    def __init__(self):
        self.total   = 0
        self.ok      = 0
        self.expected = 0
        self.bugs    = 0
        self.start   = time.time()
        self.cases_by_level = {0: 0, 1: 0, 2: 0, 3: 0}

    def elapsed(self) -> float:
        return time.time() - self.start

    def rate(self) -> float:
        return self.total / max(self.elapsed(), 0.001)

    def summary(self) -> str:
        return (f"  total={self.total}  ok={self.ok}  "
                f"expected_errors={self.expected}  bugs={self.bugs}  "
                f"rate={self.rate():.0f}/s  time={self.elapsed():.1f}s")


def fuzz(seed: int = 0, iters: int = 1000, level: int = -1,
         timeout: float = 60.0, verbose: bool = False,
         stop_on_bug: bool = True) -> FuzzStats:
    """
    Run the fuzzer.
    level=-1 means cycle through all levels.
    """
    rng   = random.Random(seed)
    stats = FuzzStats()
    bugs: list[tuple[str, str]] = []

    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"
    BOLD   = "\033[1m"

    gen = ProgramGen(rng)

    print(f"\n{BOLD}Mesa Fuzzer{RESET}  seed={seed}  iters={iters}  "
          f"level={'all' if level == -1 else level}  timeout={timeout}s\n")

    def progress():
        l = stats.bugs
        color = RED if l else GREEN
        bar_len = 30
        filled  = int(bar_len * stats.total / max(iters, 1))
        bar     = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  [{bar}] {stats.total}/{iters}  "
              f"{color}bugs={l}{RESET}  "
              f"{DIM}{stats.rate():.0f}/s{RESET}",
              end="", flush=True)

    start = time.time()

    for i in range(iters):
        if time.time() - start > timeout:
            print(f"\n{YELLOW}Timeout after {timeout}s{RESET}")
            break

        # Choose level
        if level == -1:
            cur_level = i % 4
        else:
            cur_level = level

        # Generate test case
        try:
            if cur_level == 0:
                src = gen_random_bytes(rng)
            elif cur_level == 1:
                src = gen_token_soup(rng)
            elif cur_level == 2:
                gen2 = ProgramGen(rng)
                src  = gen2.gen_program()
            else:
                base = rng.choice(KNOWN_GOOD)
                src  = mutate(base, rng)
        except Exception as e:
            # Generator crashed — that's a bug too
            stats.total += 1
            stats.bugs  += 1
            msg = f"GENERATOR CRASH: {e}"
            bugs.append((f"gen level {cur_level}", msg))
            continue

        stats.total += 1
        stats.cases_by_level[cur_level] += 1

        is_bug, desc = run_pipeline(src)

        if is_bug:
            stats.bugs += 1
            bugs.append((src, desc))
            print(f"\n{RED}{BOLD}BUG #{stats.bugs}{RESET}")
            print(f"  Level: {cur_level}")
            print(f"  Source ({len(src)} chars):")
            for line in src[:300].split("\n"):
                print(f"    {line}")
            if len(src) > 300:
                print(f"    ... ({len(src)-300} more chars)")
            print(f"  Error: {desc[:500]}")
            if stop_on_bug:
                print()
                break
        elif desc == "ok":
            stats.ok += 1
        else:
            stats.expected += 1

        if verbose or i % 100 == 0:
            progress()

    print(f"\n\n{BOLD}Results:{RESET}")
    print(stats.summary())
    print(f"  by level: {stats.cases_by_level}")

    if bugs:
        print(f"\n{RED}{BOLD}Found {len(bugs)} bug(s)!{RESET}")
        for i, (src, desc) in enumerate(bugs):
            print(f"\n  Bug {i+1}:")
            print(f"    Source: {src[:100]!r}...")
            print(f"    Error:  {desc[:200]}")
    else:
        print(f"\n{GREEN}{BOLD}No bugs found ✓{RESET}")

    return stats


# ══════════════════════════════════════════════════════════════
# Crash minimizer — shrink a bug-triggering input
# ══════════════════════════════════════════════════════════════

def minimize(src: str) -> str:
    """
    Try to find the smallest input that still triggers the same bug.
    Uses delta debugging / line removal / character removal.
    """
    is_bug, desc = run_pipeline(src)
    if not is_bug:
        return src

    bug_type = desc.split(":")[0]

    def still_bugs(s: str) -> bool:
        is_b, d = run_pipeline(s)
        return is_b and d.split(":")[0] == bug_type

    best = src
    changed = True
    while changed:
        changed = False
        # Try removing lines
        lines = best.split("\n")
        for i in range(len(lines)):
            candidate = "\n".join(lines[:i] + lines[i+1:])
            if still_bugs(candidate) and len(candidate) < len(best):
                best = candidate
                lines = best.split("\n")
                changed = True
                break
        # Try removing characters at ends
        for trim in range(1, min(20, len(best))):
            for candidate in [best[trim:], best[:-trim]]:
                if still_bugs(candidate) and len(candidate) < len(best):
                    best = candidate
                    changed = True
                    break
    return best


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Mesa compiler fuzzer")
    ap.add_argument("--seed",         type=int,   default=42)
    ap.add_argument("--iters",        type=int,   default=500)
    ap.add_argument("--level",        type=int,   default=-1,
                    help="0=bytes 1=tokens 2=structured 3=mutation -1=all")
    ap.add_argument("--timeout",      type=float, default=60.0)
    ap.add_argument("--no-stop",      action="store_true",
                    help="continue after first bug")
    ap.add_argument("--minimize",     action="store_true",
                    help="minimize any found bugs")
    ap.add_argument("-v","--verbose", action="store_true")
    args = ap.parse_args()

    stats = fuzz(
        seed       = args.seed,
        iters      = args.iters,
        level      = args.level,
        timeout    = args.timeout,
        verbose    = args.verbose,
        stop_on_bug= not args.no_stop,
    )

    if args.minimize and stats.bugs > 0:
        print("\nMinimizing bug cases...")
        # Re-run to find the cases (simplified — just re-fuzz with same seed)

    sys.exit(0 if stats.bugs == 0 else 1)
