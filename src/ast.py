"""
Mesa AST node definitions.

Covers the surface-level language features for the first iteration:
  - let / let var bindings
  - if / else expressions
  - match
  - for loops (range, iter, filter)
  - while
  - return, break, continue
  - structs, unions, interfaces, def
  - optionals — ?T, none, orelse, ?., |v| unwrapping
  - closures
  - operator overloading via interfaces
  - broadcast .op operators
  - string interpolation
  - type aliases
  - attributes #[...]
  - pub / export visibility
  - pkg declarations and imports
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Union
from enum import Enum, auto


# ══════════════════════════════════════════════════════════════
# Source locations
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SourcePos:
    line: int
    col:  int


@dataclass(frozen=True)
class SourceSpan:
    start: SourcePos
    end:   SourcePos


# ══════════════════════════════════════════════════════════════
# Visibility
# ══════════════════════════════════════════════════════════════
class Visibility(Enum):
    PRIVATE = auto()   # no keyword — file only
    PUB     = auto()   # pub — visible within package
    EXPORT  = auto()   # export — visible outside module/package facade


# ══════════════════════════════════════════════════════════════
# Attributes  #[name]  #[name = value]
# ══════════════════════════════════════════════════════════════
@dataclass
class Attribute:
    name:  str
    value: Optional["Expr"] = None   # #[align(16)] or #[derivative = fun...]


# ══════════════════════════════════════════════════════════════
# Type expressions
# ══════════════════════════════════════════════════════════════
@dataclass
class TyPrimitive:
    name: str   # "i64", "f64", "bool", "str", "void" etc.
    span: Optional[SourceSpan] = None

@dataclass
class TyNamed:
    name: str   # user-defined type
    span: Optional[SourceSpan] = None

@dataclass
class TyPointer:
    inner: "TypeExpr"
    span: Optional[SourceSpan] = None

@dataclass
class TySlice:
    inner: "TypeExpr"
    span: Optional[SourceSpan] = None

@dataclass
class TyOptional:
    inner: "TypeExpr"
    span: Optional[SourceSpan] = None

@dataclass
class TyErrorUnion:
    error_set: Optional["TypeExpr"]   # None = inferred error set
    payload:   "TypeExpr"
    span: Optional[SourceSpan] = None

@dataclass
class TyErrorSetUnion:
    members: List["TypeExpr"]
    span: Optional[SourceSpan] = None

@dataclass
class TyTuple:
    """`.{x: f64, y: f64}` or `.{f64, f64}`"""
    fields: List[tuple[Optional[str], "TypeExpr"]]
    span: Optional[SourceSpan] = None

@dataclass
class TyVec:
    elem: "TypeExpr"
    size: Optional["Expr"]   # None = dynamic
    span: Optional[SourceSpan] = None

@dataclass
class TyMat:
    elem: "TypeExpr"
    rows: Optional["Expr"]   # None = runtime
    cols: Optional["Expr"]
    span: Optional[SourceSpan] = None

@dataclass
class TyFun:
    """fun(T, U) V — function type"""
    params: List["TypeExpr"]
    ret:    "TypeExpr"
    abi:    Optional[str] = None
    span: Optional[SourceSpan] = None

@dataclass
class TyGeneric:
    """SomeType[T, U]"""
    name:   str
    params: List["TypeExpr"]
    span: Optional[SourceSpan] = None

class TyVoid:
    span: Optional[SourceSpan] = None
    def __repr__(self): return "void"

class TyInfer:
    span: Optional[SourceSpan] = None
    def __repr__(self): return "_"

@dataclass
class TyUnitful:
    """float`N`, float`m/s²`, float`?`
    
    inner — the numeric type being annotated (usually TyPrimitive("f64"))
    unit  — the unit expression string: "N", "m/s²", "kg*m/s²", "?"
            "?" means dynamically-unitful (unit tracked at runtime)
            "1" means explicitly dimensionless
    """
    inner: "TypeExpr"
    unit:  str   # raw unit expression from between backticks
    span: Optional[SourceSpan] = None

@dataclass
class TyAnyInterface:
    """any Shape — stack-allocated existential with small-buffer optimisation.

    Layout (32 bytes):
      vtable ptr  :  8 bytes
      inline_buf  : 16 bytes  (concrete data if small enough)
      is_inline   :  8 bytes  (1 = data in inline_buf, 0 = data in heap_ptr)

    If sizeof(concrete) <= 16: data stored in inline_buf, is_inline = 1
    If sizeof(concrete) >  16: data stored at *(void*)inline_buf, is_inline = 0

    *any Shape is TyPointer(TyAnyInterface("Shape")) — heap-allocated fat pointer.
    """
    iface_name: str   # the interface name, e.g. "Shape"
    span: Optional[SourceSpan] = None

TypeExpr = Union[
    TyPrimitive, TyNamed, TyPointer, TySlice, TyOptional,
    TyErrorUnion, TyErrorSetUnion, TyTuple, TyVec, TyMat, TyFun,
    TyGeneric, TyVoid, TyInfer, TyUnitful, TyAnyInterface,
]


# ══════════════════════════════════════════════════════════════
# Patterns  (for match arms and for-loop bindings)
# ══════════════════════════════════════════════════════════════
@dataclass
class PatWildcard:
    """_"""

@dataclass
class PatIdent:
    name: str

@dataclass
class PatVariant:
    name:           str
    binding:        Optional[str]          # Pattern(x)
    extra_bindings: List[str] = None       # Pattern(x, y, z)

    def __post_init__(self):
        if self.extra_bindings is None:
            self.extra_bindings = []

@dataclass
class PatInt:
    value: int

@dataclass
class PatFloat:
    value: float

@dataclass
class PatBool:
    value: bool

@dataclass
class PatNone:
    pass

@dataclass
class PatTuple:
    """(a, b, c)"""
    names: List[str]

@dataclass
class PatRef:
    """*p — mutable reference"""
    name: str

MatchPattern = Union[
    PatWildcard, PatIdent, PatVariant,
    PatInt, PatFloat, PatBool, PatNone,
]

ForPattern = Union[PatIdent, PatRef, PatTuple]


# ══════════════════════════════════════════════════════════════
# Expressions
# ══════════════════════════════════════════════════════════════
@dataclass
class IntLit:
    value: int
    line: int = 0
    col:  int = 0

@dataclass
class FloatLit:
    value: float
    line: int = 0
    col:  int = 0

@dataclass
class BoolLit:
    value: bool
    line: int = 0
    col:  int = 0

@dataclass
class StringLit:
    """
    Raw string content — may contain {interpolation} segments.
    segments is a list of either str (literal text) or Expr (interpolated).
    """
    raw:      str
    segments: List[Union[str, "Expr"]] = field(default_factory=list)
    line:     int = 0
    col:      int = 0

@dataclass
class NoneLit:
    pass

@dataclass
class VariantLit:
    """Dot-prefixed union variant expression: .Red, .Some, etc."""
    name: str          # bare variant name without the dot
    line: int = 0
    col:  int = 0
    span: Optional[SourceSpan] = None

@dataclass
class Ident:
    name: str
    line: int = 0
    col:  int = 0
    span: Optional[SourceSpan] = None

@dataclass
class SelfExpr:
    pass

@dataclass
class BinExpr:
    op:    str   # "+", "-", "*", "/", "%", "^",
                 # "==", "!=", "<", ">", "<=", ">=",
                 # "and", "or",
                 # ".+", ".-", ".*", "./", ".%", ".^",
                 # ".==", ".!=", ".<", ".>", ".<=", ".>=",
                 # "+-", ".+-"
    left:  "Expr"
    right: "Expr"
    span:  Optional[SourceSpan] = None

@dataclass
class UnaryExpr:
    op:      str   # "-", "!", "*" (deref), "@" (addr)
    operand: "Expr"
    span:    Optional[SourceSpan] = None

@dataclass
class FieldExpr:
    obj:   "Expr"
    field: str
    span:  Optional[SourceSpan] = None

@dataclass
class IndexExpr:
    obj:     "Expr"
    indices: List["Expr"]   # A[i, j] for mat — multiple indices
    span:    Optional[SourceSpan] = None

@dataclass
class CallExpr:
    callee: "Expr"
    args:   List["Arg"]
    line:   int = 0
    col:    int = 0
    span:   Optional[SourceSpan] = None

@dataclass
class Arg:
    name:  Optional[str]
    value: "Expr"

@dataclass
class TupleLit:
    """.{x: 1.0, y: 2.0} or .{a, b}"""
    fields: List[tuple[Optional[str], "Expr"]]
    span:   Optional[SourceSpan] = None

@dataclass
class ArrayLit:
    """[a, b, c] — stack allocated"""
    elems: List["Expr"]
    span:  Optional[SourceSpan] = None

@dataclass
class VecLit:
    """vec[a, b, c]"""
    elems: List["Expr"]
    span:  Optional[SourceSpan] = None

@dataclass
class VecComp:
    """vec[expr for pat in iter : cond]"""
    expr:    "Expr"
    pattern: ForPattern
    iter:    "Expr"
    filter:  Optional["Expr"]
    span:    Optional[SourceSpan] = None

@dataclass
class BoxLit:
    """box[a, b, c]"""
    elems: List["Expr"]
    span:  Optional[SourceSpan] = None

@dataclass
class RangeExpr:
    start:     "Expr"
    end:       "Expr"
    inclusive: bool   # .. = exclusive, ... = inclusive
    span:      Optional[SourceSpan] = None

@dataclass
class IfExpr:
    cond:       "Expr"
    then_block: "Block"
    else_block: Optional["Block"]
    span:       Optional[SourceSpan] = None

@dataclass
class IfUnwrap:
    """if expr |v| { } else { } — optional unwrapping"""
    expr:       "Expr"
    binding:    str
    is_ref:     bool          # |*v| vs |v|
    then_block: "Block"
    else_block: Optional["Block"]
    span:       Optional[SourceSpan] = None

@dataclass
class WhileUnwrap:
    """while expr |v| { } — iteration unwrapping"""
    expr:    "Expr"
    binding: str
    is_ref:  bool
    body:    "Block"
    span:    Optional[SourceSpan] = None

@dataclass
class MatchExpr:
    value: "Expr"
    arms:  List["MatchArm"]
    span:  Optional[SourceSpan] = None

@dataclass
class MatchArm:
    pattern: MatchPattern
    body:    "Block"

@dataclass
class BlockExpr:
    block: "Block"
    span:  Optional[SourceSpan] = None

@dataclass
class WithExpr:
    resource: "Expr"
    cleanup:  Optional[str]   # .reset, .free, etc. — None = no cleanup
    body:     "Block"
    handle:   Optional["HandleBlock"] = None
    span:     Optional[SourceSpan] = None

@dataclass
class Closure:
    """fun(x: T) R { body }"""
    params: List["Param"]
    ret:    TypeExpr
    body:   "Block"
    span:   Optional[SourceSpan] = None

@dataclass
class ComptimeExpr:
    """comptime expr"""
    expr: "Expr"
    span: Optional[SourceSpan] = None

@dataclass
class EscExpr:
    """esc expr — promote value to the next outer allocator context."""
    expr: "Expr"
    span: Optional[SourceSpan] = None

@dataclass
class WithAllocExpr:
    """expr with alloc — allocate or promote into a specific allocator."""
    expr: "Expr"
    allocator: "Expr"
    span: Optional[SourceSpan] = None

@dataclass
class OptionalChain:
    """a?.b?.c — flattened chain"""
    steps: List[Union[str, "Expr"]]   # field names or index exprs
    span:  Optional[SourceSpan] = None

@dataclass
class AsCast:
    expr:  "Expr"
    type_: TypeExpr
    span:  Optional[SourceSpan] = None

@dataclass
class UncertainLit:
    """9.8 +- 0.1 — produces Uncertain[T]"""
    value: "Expr"
    error: "Expr"
    span:  Optional[SourceSpan] = None

@dataclass
class UnitLit:
    """Numeric literal or expression annotated with a unit.

    10.0`N`       → UnitLit(value=FloatLit(10.0), unit="N")
    `N`           → UnitLit(value=None, unit="N")   bare unit = 1.0`N`
    10 +- 0.5 `N` → UncertainLit(UnitLit(...), UnitLit(...))
    """
    value: "Optional[Expr]"   # None for bare unit literals like `N`
    unit:  str                # raw unit string: "N", "m/s²", "?", "1"
    span:  Optional[SourceSpan] = None

Expr = Union[
    IntLit, FloatLit, BoolLit, StringLit, NoneLit,
    Ident, SelfExpr, BinExpr, UnaryExpr,
    FieldExpr, IndexExpr, CallExpr,
    TupleLit, ArrayLit, VecLit, VecComp,
    RangeExpr, IfExpr, IfUnwrap, WhileUnwrap,
    MatchExpr, BlockExpr, WithExpr, Closure,
    ComptimeExpr, EscExpr, WithAllocExpr, OptionalChain, AsCast, UncertainLit, UnitLit,
]


# ══════════════════════════════════════════════════════════════
# Statements
# ══════════════════════════════════════════════════════════════
@dataclass
class Block:
    stmts: List["Stmt"]
    tail:  Optional[Expr]   # expression-valued tail (no trailing ;)
    span:  Optional[SourceSpan] = None

@dataclass
class LetStmt:
    mutable: bool            # let var x vs let x
    name:    str
    type_:   Optional[TypeExpr]
    init:    Optional[Expr]
    vis:     Visibility = Visibility.PRIVATE
    attrs:   List[Attribute] = field(default_factory=list)
    line:    int = 0
    col:     int = 0

@dataclass
class ReturnStmt:
    value: Optional[Expr]
    line:  int = 0
    col:   int = 0

@dataclass
class BreakStmt:
    label: Optional[str]
    value: Optional[Expr]

@dataclass
class ContinueStmt:
    label: Optional[str]

@dataclass
class AssignStmt:
    target: Expr
    op:     str   # "=", "+=", "-=", "*=", "/=", "^=", "?="
                  # also broadcast: ".+=", ".*=" etc.
    value:  Expr
    line:   int = 0
    col:    int = 0

@dataclass
class ForRangeStmt:
    var:       str
    start:     Expr
    end:       Expr
    inclusive: bool
    filter:    Optional[Expr]
    body:      Block
    label:     Optional[str] = None

@dataclass
class ForIterStmt:
    pattern: ForPattern
    iter:    Expr
    filter:  Optional[Expr]
    body:    Block
    label:   Optional[str] = None

@dataclass
class WhileStmt:
    cond:  Expr
    body:  Block
    label: Optional[str] = None

@dataclass
class DeferStmt:
    error_only: bool   # !defer vs defer
    body:       Block

@dataclass
class ExprStmt:
    expr: Expr

Stmt = Union[
    LetStmt, ReturnStmt, BreakStmt, ContinueStmt,
    AssignStmt, ForRangeStmt, ForIterStmt,
    WhileStmt, DeferStmt, ExprStmt,
]


# ══════════════════════════════════════════════════════════════
# Declarations
# ══════════════════════════════════════════════════════════════
@dataclass
class Param:
    name:    str
    type_:   TypeExpr
    default: Optional[Expr] = None

@dataclass
class FunDecl:
    vis:     Visibility
    attrs:   List[Attribute]
    name:    str
    params:  List[Param]
    ret:     TypeExpr
    body:    Optional[Block]   # None = interface signature
    # modifiers
    is_inline:    bool = False
    is_extern:    bool = False
    handle_block: Optional["HandleBlock"] = None
    span:         Optional[SourceSpan] = None

@dataclass
class OpaqueTypeDecl:
    vis:     Visibility
    attrs:   List[Attribute]
    name:    str
    span:    Optional[SourceSpan] = None

@dataclass
class HandleBlock:
    """handle |e| { ... } — error handler attached to a function or with expr."""
    binding: str
    body:    Block

@dataclass
class FieldDecl:
    name:  str
    type_: TypeExpr
    default: Optional[Expr] = None

@dataclass
class StructDecl:
    vis:     Visibility
    attrs:   List[Attribute]
    name:    str
    params:  List[str]         # generic type params: struct Foo[T, U]
    fields:  List[FieldDecl]
    methods: List[FunDecl]
    where:   List[str]         # where constraints as raw strings for now
    span:    Optional[SourceSpan] = None

@dataclass
class UnionVariant:
    name:    str
    payload: Optional[TypeExpr]

@dataclass
class UnionDecl:
    vis:      Visibility
    name:     str
    params:   List[str]
    variants: List[UnionVariant]
    span:     Optional[SourceSpan] = None

@dataclass
class InterfaceDecl:
    vis:     Visibility
    name:    str
    params:  List[str]         # interface Add[T]
    parents: List[str]         # interface Ord[T] : Eq[T]
    methods: List[FunDecl]
    where:   List[str]
    span:    Optional[SourceSpan] = None

@dataclass
class DefDecl:
    """def Interface[T] for Type where T : Constraint"""
    interfaces: List[str]      # multiple: def Add, Sub, Mul for Vec3
    for_type:   str
    methods:    List[FunDecl]
    where:      List[str]
    span:       Optional[SourceSpan] = None

@dataclass
class TypeAlias:
    """type Vec2 = mat[f32; 2, 1]"""
    vis:  Visibility
    name: str
    type_: TypeExpr
    span: Optional[SourceSpan] = None

@dataclass
class UnitAlias:
    """let `N` := `kg*m/s²`   or   let `km` := 1000.0`m`

    Defines a named unit in terms of existing units.
    name — the unit being defined, e.g. "N", "km"
    defn — either a unit expression string ("kg*m/s²")
            or a UnitLit for scale factor (1000.0`m`)
    """
    vis:  Visibility
    name: str       # unit name without backticks, e.g. "N"
    defn: "Expr"    # UnitLit(1000.0, "m") or UnitLit(None, "kg*m/s²")
    span: Optional[SourceSpan] = None

@dataclass
class ErrorDecl:
    """error IoError { NotFound(str), PermissionDenied, Timeout(u64) }
    
    Like a union but the compiler knows these are error variants.
    Supports the same payload syntax as union.
    """
    vis:      Visibility
    name:     str
    variants: List[UnionVariant]
    span:     Optional[SourceSpan] = None

@dataclass
class PkgDecl:
    """pkg math.linalg"""
    path: str   # "math.linalg"
    span: Optional[SourceSpan] = None

@dataclass
class ImportDecl:
    """import std.math  /  import std.math as m"""
    path:  str
    alias: Optional[str]
    span:  Optional[SourceSpan] = None

@dataclass
class FromImportDecl:
    """from std.math import sin, cos  /  from std.math import sin as sine"""
    path:    str
    names:   List[tuple[str, Optional[str]]]   # (name, alias)
    span:    Optional[SourceSpan] = None

@dataclass
class FromExportDecl:
    """from physics.particles export Particle, simulate  (<pkgname>.pkg only)"""
    path:   str
    names:  List[str]
    fields: Optional[List[str]]   # field restriction for structs
    span:   Optional[SourceSpan] = None

@dataclass
class PkgExportDecl:
    """from "world/state" export World, Body as RigidBody"""
    source_path: str
    names: List[tuple[str, Optional[str]]]
    opaque: bool = False
    span: Optional[SourceSpan] = None

@dataclass
class PkgExportAllDecl:
    """export "world/api" — export all pub declarations from a file"""
    source_path: str
    span: Optional[SourceSpan] = None

@dataclass
class TestDecl:
    """test "name" { ... }"""
    name: str
    body: Block
    span: Optional[SourceSpan] = None

Decl = Union[
    FunDecl, OpaqueTypeDecl, StructDecl, UnionDecl, InterfaceDecl, DefDecl,
    TypeAlias, UnitAlias, ErrorDecl, LetStmt,
    PkgDecl, ImportDecl, FromImportDecl, FromExportDecl,
    PkgExportDecl, PkgExportAllDecl, TestDecl,
]


# ══════════════════════════════════════════════════════════════
# Program
# ══════════════════════════════════════════════════════════════
@dataclass
class Program:
    pkg: Optional[PkgDecl]
    imports: List[Union[ImportDecl, FromImportDecl]]
    decls:   List[Decl]
