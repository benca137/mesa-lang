"""
Mesa internal type representation.

Separate from AST TypeExpr — these are the resolved, concrete types
that the type checker works with after resolving names and generics.

Three states a type can be in:
  Concrete  — fully known: TInt(64, signed=True), TStruct("Particle"), etc.
  Literal   — unresolved literal: TIntLit, TFloatLit
  Unknown   — type variable waiting to be unified: TVar
  Error     — poison type, absorbs operations silently: TError
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
import itertools


# ══════════════════════════════════════════════════════════════
# Base
# ══════════════════════════════════════════════════════════════
class Type:
    """Base class for all internal types."""

    def is_error(self) -> bool:
        return isinstance(self, TError)

    def is_numeric(self) -> bool:
        return isinstance(self, (TInt, TFloat, TIntLit, TFloatLit))

    def is_integer(self) -> bool:
        return isinstance(self, (TInt, TIntLit))

    def is_float(self) -> bool:
        return isinstance(self, (TFloat, TFloatLit))

    def is_optional(self) -> bool:
        return isinstance(self, TOptional)

    def is_void(self) -> bool:
        return isinstance(self, TVoid)

    def contains_error(self) -> bool:
        """True if this type or any sub-type is TError."""
        return self.is_error()

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self.__dict__ == other.__dict__

    def __hash__(self) -> int:
        return hash(repr(self))


# ══════════════════════════════════════════════════════════════
# Primitive types
# ══════════════════════════════════════════════════════════════
@dataclass(eq=False)
class TInt(Type):
    bits:   int    # 8, 16, 32, 64
    signed: bool   # True = i64, False = u64

    def __repr__(self): return f"{'i' if self.signed else 'u'}{self.bits}"
    def __eq__(self, o): return isinstance(o, TInt) and self.bits == o.bits and self.signed == o.signed
    def __hash__(self): return hash(('int', self.bits, self.signed))

@dataclass(eq=False)
class TFloat(Type):
    bits: int   # 32 or 64

    def __repr__(self): return f"f{self.bits}"
    def __eq__(self, o): return isinstance(o, TFloat) and self.bits == o.bits
    def __hash__(self): return hash(('float', self.bits))

class TBool(Type):
    def __repr__(self): return "bool"
    def __eq__(self, o): return isinstance(o, TBool)
    def __hash__(self): return hash('bool')

class TString(Type):
    def __repr__(self): return "str"
    def __eq__(self, o): return isinstance(o, TString)
    def __hash__(self): return hash('str')

class TVoid(Type):
    def __repr__(self): return "void"
    def __eq__(self, o): return isinstance(o, TVoid)
    def __hash__(self): return hash('void')


# ══════════════════════════════════════════════════════════════
# Unresolved literal types
# ══════════════════════════════════════════════════════════════
class TIntLit(Type):
    """
    Unresolved integer literal — will resolve to any integer or float type.
    Used for bare `42` with no context.
    Defaults to i64 if never resolved.
    """
    def __repr__(self): return "{integer}"
    def __eq__(self, o): return isinstance(o, TIntLit)
    def __hash__(self): return hash('intlit')

class TFloatLit(Type):
    """
    Unresolved float literal — resolves to f32 or f64.
    Defaults to f64.
    """
    def __repr__(self): return "{float}"
    def __eq__(self, o): return isinstance(o, TFloatLit)
    def __hash__(self): return hash('floatlit')


# ══════════════════════════════════════════════════════════════
# Type variables — for bidirectional inference
# ══════════════════════════════════════════════════════════════
_var_counter = itertools.count()

class TVar(Type):
    """
    Unknown type variable — filled in by unification.
    Used during bidirectional checking when the type is not yet known.
    """
    def __init__(self, name: Optional[str] = None):
        self.id       = next(_var_counter)
        self.name     = name or f"?{self.id}"
        self._resolved: Optional[Type] = None

    def resolve(self, ty: Type):
        assert not self._resolved, f"TVar {self.name} already resolved"
        self._resolved = ty

    @property
    def resolved(self) -> Optional[Type]:
        return self._resolved

    def root(self) -> Type:
        """Follow resolution chain to the root type."""
        if self._resolved is None:
            return self
        if isinstance(self._resolved, TVar):
            return self._resolved.root()
        return self._resolved

    def __repr__(self): return f"?{self.name}"
    def __eq__(self, o): return isinstance(o, TVar) and self.id == o.id
    def __hash__(self): return hash(('tvar', self.id))


# ══════════════════════════════════════════════════════════════
# Error / poison type
# ══════════════════════════════════════════════════════════════
class TError(Type):
    """
    Poison type. Produced when a type error occurs.
    Propagates silently through operations to prevent error cascades.
    Operations on TError always return TError without emitting new errors.
    """
    def __repr__(self): return "<error>"
    def __eq__(self, o): return isinstance(o, TError)
    def __hash__(self): return hash('error')
    def contains_error(self) -> bool: return True


# ══════════════════════════════════════════════════════════════
# Composite types
# ══════════════════════════════════════════════════════════════
@dataclass(eq=False)
class TOptional(Type):
    inner: Type

    def __repr__(self): return f"?{self.inner}"
    def __eq__(self, o): return isinstance(o, TOptional) and self.inner == o.inner
    def __hash__(self): return hash(('optional', self.inner))
    def contains_error(self): return self.inner.contains_error()

@dataclass(eq=False)
class TPointer(Type):
    inner: Type
    mutable: bool = True

    def __repr__(self): return f"*{self.inner}"
    def __eq__(self, o): return isinstance(o, TPointer) and self.inner == o.inner
    def __hash__(self): return hash(('ptr', self.inner))
    def contains_error(self): return self.inner.contains_error()

@dataclass(eq=False)
class TSlice(Type):
    inner: Type

    def __repr__(self): return f"[]{self.inner}"
    def __eq__(self, o): return isinstance(o, TSlice) and self.inner == o.inner
    def __hash__(self): return hash(('slice', self.inner))
    def contains_error(self): return self.inner.contains_error()

@dataclass(eq=False)
class TArray(Type):
    inner: Type
    size:  int

    def __repr__(self): return f"[{self.inner}; {self.size}]"
    def __eq__(self, o): return isinstance(o, TArray) and self.inner == o.inner and self.size == o.size
    def __hash__(self): return hash(('array', self.inner, self.size))
    def contains_error(self): return self.inner.contains_error()

@dataclass(eq=False)
class TVec(Type):
    inner: Type
    size:  Optional[int]   # None = dynamic

    def __repr__(self):
        return f"vec[{self.inner}; {self.size}]" if self.size else f"vec[{self.inner}]"
    def __eq__(self, o): return isinstance(o, TVec) and self.inner == o.inner and self.size == o.size
    def __hash__(self): return hash(('vec', self.inner, self.size))
    def contains_error(self): return self.inner.contains_error()

@dataclass(eq=False)
class TMat(Type):
    inner: Type
    rows:  Optional[int]
    cols:  Optional[int]

    def __repr__(self):
        if self.rows and self.cols:
            return f"mat[{self.inner}; {self.rows}, {self.cols}]"
        return f"mat[{self.inner}]"
    def __eq__(self, o):
        return isinstance(o, TMat) and self.inner == o.inner and self.rows == o.rows and self.cols == o.cols
    def __hash__(self): return hash(('mat', self.inner, self.rows, self.cols))

@dataclass(eq=False)
class TTuple(Type):
    """`.{x: f64, y: f64}` or `.{f64, f64}`"""
    fields: List[Tuple[Optional[str], Type]]

    def __repr__(self):
        parts = [f"{n}: {t}" if n else repr(t) for n, t in self.fields]
        return ".{" + ", ".join(parts) + "}"
    def __eq__(self, o): return isinstance(o, TTuple) and self.fields == o.fields
    def __hash__(self): return hash(('tuple', tuple((n, t) for n, t in self.fields)))
    def contains_error(self): return any(t.contains_error() for _, t in self.fields)

    def field_type(self, name: str) -> Optional[Type]:
        for n, t in self.fields:
            if n == name: return t
        return None

    def field_index(self, name: str) -> Optional[int]:
        for i, (n, _) in enumerate(self.fields):
            if n == name: return i
        return None


# ══════════════════════════════════════════════════════════════
# Function type
# ══════════════════════════════════════════════════════════════
@dataclass(eq=False)
class TFun(Type):
    params: List[Type]
    ret:    Type
    abi:    Optional[str] = None
    # generic info — None for concrete functions
    type_params: List[str] = field(default_factory=list)
    where:       List[str] = field(default_factory=list)

    def __repr__(self):
        ps = ", ".join(repr(p) for p in self.params)
        if self.abi is not None:
            return f"[{self.abi}]fun({ps}) {self.ret}"
        return f"fun({ps}) {self.ret}"
    def __eq__(self, o):
        return isinstance(o, TFun) and self.params == o.params and self.ret == o.ret and self.abi == o.abi
    def __hash__(self): return hash(('fun', tuple(self.params), self.ret, self.abi))
    def contains_error(self):
        return any(p.contains_error() for p in self.params) or self.ret.contains_error()


# ══════════════════════════════════════════════════════════════
# User-defined types
# ══════════════════════════════════════════════════════════════
@dataclass(eq=False)
class TStruct(Type):
    name:        str
    fields:      Dict[str, Type]           # field name → type
    methods:     Dict[str, TFun]           # method name → type
    type_params: List[str] = field(default_factory=list)
    # generic substitution — filled when instantiated
    type_args:   Dict[str, Type] = field(default_factory=dict)
    opaque:      bool = False

    def __repr__(self):
        if self.type_args:
            args = ", ".join(repr(v) for v in self.type_args.values())
            return f"{self.name}[{args}]"
        return self.name
    def __eq__(self, o):
        return isinstance(o, TStruct) and self.name == o.name and self.type_args == o.type_args
    def __hash__(self): return hash(('struct', self.name))
    def contains_error(self):
        return any(t.contains_error() for t in self.fields.values())

    def field_type(self, name: str) -> Optional[Type]:
        return self.fields.get(name)

    def method_type(self, name: str) -> Optional[TFun]:
        return self.methods.get(name)

@dataclass(eq=False)
class TUnion(Type):
    name:     str
    variants: Dict[str, Optional[Type]]   # variant name → payload type or None
    type_params: List[str] = field(default_factory=list)

    def __repr__(self): return self.name
    def __eq__(self, o): return isinstance(o, TUnion) and self.name == o.name
    def __hash__(self): return hash(('union', self.name))

    def variant_payload(self, name: str) -> Optional[Type]:
        return self.variants.get(name)

@dataclass(eq=False)
class TInterface(Type):
    name:        str
    params:      List[str]
    methods:     Dict[str, TFun]
    parents:     List[str]
    defaults:    set = field(default_factory=set)  # method names with default bodies
    method_visibility: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self): return self.name
    def __eq__(self, o): return isinstance(o, TInterface) and self.name == o.name
    def __hash__(self): return hash(('iface', self.name))

@dataclass(eq=False)
class TNamespace(Type):
    name: str

    def __repr__(self): return self.name
    def __eq__(self, o): return isinstance(o, TNamespace) and self.name == o.name
    def __hash__(self): return hash(('namespace', self.name))

@dataclass(eq=False)
class TErrorSet(Type):
    """An error set — like TUnion but semantically distinct as an error type.
    
    variants maps variant name → payload Type (or None for unit variants).
    The compiler uses this to verify catch exhaustiveness and infer !T return types.
    """
    name:     str
    variants: Dict[str, Optional[Type]]   # name → payload type or None

    def __repr__(self): return f"error {self.name}"
    def __eq__(self, o): return isinstance(o, TErrorSet) and self.name == o.name
    def __hash__(self): return hash(('errset', self.name))

@dataclass(eq=False)
class TErrorSetUnion(Type):
    members: Tuple[TErrorSet, ...]
    name: Optional[str] = None

    def __repr__(self):
        if self.name:
            return self.name
        return " | ".join(m.name for m in self.members)
    def __eq__(self, o):
        return isinstance(o, TErrorSetUnion) and self.members == o.members and self.name == o.name
    def __hash__(self): return hash(('errset_union', self.members, self.name))

@dataclass(eq=False)
class TErrorUnion(Type):
    error_set: Optional[Type]   # None = anyerror
    payload:   Type

    def __repr__(self):
        e = repr(self.error_set) if self.error_set else "!"
        return f"{e}!{self.payload}"
    def __eq__(self, o):
        return isinstance(o, TErrorUnion) and self.error_set == o.error_set and self.payload == o.payload
    def __hash__(self): return hash(('errunion', self.error_set, self.payload))
    def contains_error(self): return self.payload.contains_error()


def error_set_members(ty: Optional[Type]) -> Tuple[TErrorSet, ...]:
    if ty is None:
        return tuple()
    if isinstance(ty, TErrorSet):
        return (ty,)
    if isinstance(ty, TErrorSetUnion):
        return ty.members
    return tuple()


def error_set_member_names(ty: Optional[Type]) -> Tuple[str, ...]:
    return tuple(m.name for m in error_set_members(ty))


def merge_error_sets(*tys: Optional[Type], name: Optional[str] = None) -> Optional[Type]:
    members: Dict[str, TErrorSet] = {}
    for ty in tys:
        for member in error_set_members(ty):
            members[member.name] = member
    if not members:
        return None
    ordered = tuple(sorted(members.values(), key=lambda m: m.name))
    if len(ordered) == 1 and name is None:
        return ordered[0]
    return TErrorSetUnion(ordered, name=name)


def empty_error_set(name: str = "NeverError") -> TErrorSetUnion:
    return TErrorSetUnion(tuple(), name=name)


def error_set_variants(ty: Optional[Type]) -> Dict[str, Optional[Type]]:
    variants: Dict[str, Optional[Type]] = {}
    for member in error_set_members(ty):
        for vname, payload in member.variants.items():
            existing = variants.get(vname, payload)
            if vname in variants and existing != payload:
                raise ValueError(f"conflicting error variant '{vname}' across error sets")
            variants[vname] = payload
    return variants


def error_set_contains(container: Optional[Type], candidate: Optional[Type]) -> bool:
    if candidate is None:
        return True
    container_members = {m.name for m in error_set_members(container)}
    candidate_members = {m.name for m in error_set_members(candidate)}
    return candidate_members.issubset(container_members)


def error_set_key(ty: Optional[Type]) -> str:
    if ty is None:
        return "inferred"
    if isinstance(ty, TErrorSet):
        return ty.name
    if isinstance(ty, TErrorSetUnion):
        if ty.name:
            return ty.name
        return "__".join(m.name for m in ty.members)
    return "unknown_error"


# ══════════════════════════════════════════════════════════════
# Uncertain / Correlated types

# ══════════════════════════════════════════════════════════════
# Unitful types
# ══════════════════════════════════════════════════════════════
# Dimension vector: (kg, m, s, A, K, mol, cd) exponents as ints
# Examples:
#   N   = (1, 1, -2, 0, 0, 0, 0)   kg*m/s²
#   J   = (1, 2, -2, 0, 0, 0, 0)   kg*m²/s²
#   W   = (1, 2, -3, 0, 0, 0, 0)   kg*m²/s³
#   m/s = (0, 1, -1, 0, 0, 0, 0)
#   dimensionless `1` = (0,0,0,0,0,0,0)
DimVec = tuple  # 7-tuple of ints (kg, m, s, A, K, mol, cd)
DIM_ZERO: DimVec = (0, 0, 0, 0, 0, 0, 0)

def dim_mul(a: DimVec, b: DimVec) -> DimVec:
    return tuple(x + y for x, y in zip(a, b))

def dim_div(a: DimVec, b: DimVec) -> DimVec:
    return tuple(x - y for x, y in zip(a, b))

def dim_pow(a: DimVec, n: int) -> DimVec:
    return tuple(x * n for x in a)

def dim_is_dimensionless(a: DimVec) -> bool:
    return all(x == 0 for x in a)

def dim_fmt(dims: DimVec) -> str:
    """Format dimension vector as a human-readable unit string."""
    names = ("kg", "m", "s", "A", "K", "mol", "cd")
    pos = [f"{n}" if e == 1 else f"{n}^{e}"
           for n, e in zip(names, dims) if e > 0]
    neg = [f"{n}" if e == -1 else f"{n}^{-e}"
           for n, e in zip(names, dims) if e < 0]
    if not pos and not neg:
        return "1"
    num = "*".join(pos) if pos else "1"
    if neg:
        return num + "/" + "*".join(neg)
    return num


# The built-in SI unit registry
# Maps unit name → (DimVec, scale_factor)
# scale_factor is relative to SI base units (1.0 for base/coherent SI)
_SI_BASE: dict[str, tuple[DimVec, float]] = {
    # Base SI units
    "kg":  ((1,0,0,0,0,0,0), 1.0),
    "m":   ((0,1,0,0,0,0,0), 1.0),
    "s":   ((0,0,1,0,0,0,0), 1.0),
    "A":   ((0,0,0,1,0,0,0), 1.0),
    "K":   ((0,0,0,0,1,0,0), 1.0),
    "mol": ((0,0,0,0,0,1,0), 1.0),
    "cd":  ((0,0,0,0,0,0,1), 1.0),
    # Dimensionless
    "1":   (DIM_ZERO,         1.0),
    # Common derived SI
    "N":   ((1,1,-2,0,0,0,0), 1.0),    # kg*m/s²
    "J":   ((1,2,-2,0,0,0,0), 1.0),    # N*m
    "W":   ((1,2,-3,0,0,0,0), 1.0),    # J/s
    "Pa":  ((1,-1,-2,0,0,0,0),1.0),    # N/m²
    "Hz":  ((0,0,-1,0,0,0,0), 1.0),    # 1/s
    "V":   ((1,2,-3,-1,0,0,0),1.0),    # W/A
    "C":   ((0,0,1,1,0,0,0),  1.0),    # A*s
    "F_unit": ((-1,-2,4,2,0,0,0),1.0), # C/V (using F_unit to avoid collision with f64)
    "T":   ((1,0,-2,-1,0,0,0),1.0),    # V*s/m²
    "Wb":  ((1,2,-2,-1,0,0,0),1.0),    # V*s
    "Ω":   ((1,2,-3,-2,0,0,0),1.0),    # V/A
    # Angle — dimensionless, scale=1 for rad, scale=π/180 for deg
    "rad": (DIM_ZERO, 1.0),
    "deg": (DIM_ZERO, 0.017453292519943295),  # π/180
    # Common non-SI with known conversion
    "g":   ((1,0,0,0,0,0,0),  0.001),  # gram = 0.001 kg
    "km":  ((0,1,0,0,0,0,0),  1000.0), # kilometre
    "cm":  ((0,1,0,0,0,0,0),  0.01),
    "mm":  ((0,1,0,0,0,0,0),  0.001),
    "ms":  ((0,0,1,0,0,0,0),  0.001),  # millisecond
    "min": ((0,0,1,0,0,0,0),  60.0),
    "h":   ((0,0,1,0,0,0,0),  3600.0),
    "L":   ((0,3,0,0,0,0,0),  0.001),  # litre = 0.001 m³
    "eV":  ((1,2,-2,0,0,0,0), 1.602176634e-19), # electron-volt in Joules
}


@dataclass(eq=False)
class TUnitful(Type):
    """A numeric type annotated with physical units.

    inner  — the underlying numeric type (TFloat, TInt, etc.)
    dims   — dimension exponent vector (kg, m, s, A, K, mol, cd)
             None means dynamic — unit tracked at runtime (float`?`)
    scale  — scale factor relative to SI coherent unit (1.0 for SI base)
    name   — canonical display name e.g. "N", "m/s", "kg*m/s²"
    """
    inner: Type
    dims:  Optional[DimVec]   # None = dynamic float`?`
    scale: float = 1.0
    name:  str   = ""

    def __repr__(self):
        if self.dims is None:
            return f"{self.inner}`?`"
        return f"{self.inner}`{self.name or dim_fmt(self.dims)}`"

    def __eq__(self, o):
        if not isinstance(o, TUnitful): return False
        if self.inner != o.inner: return False
        if self.dims is None or o.dims is None:
            return self.dims is o.dims   # both dynamic
        return self.dims == o.dims and abs(self.scale - o.scale) < 1e-12

    def __hash__(self):
        return hash(('unitful', self.inner, self.dims, round(self.scale, 12)))

    def is_dynamic(self) -> bool:
        return self.dims is None

    def is_dimensionless(self) -> bool:
        return self.dims is not None and dim_is_dimensionless(self.dims)

    def same_dimension(self, other: "TUnitful") -> bool:
        """True if both have same physical dimension (ignoring scale)."""
        if self.dims is None or other.dims is None:
            return False   # dynamic — unknown at compile time
        return self.dims == other.dims


def _best_unit_name(dims: DimVec, scale: float,
                    registry: Optional[dict] = None) -> str:
    """Find the best canonical name for a dim+scale pair, or format it."""
    reg = dict(_SI_BASE)
    if registry:
        reg.update(registry)
    for name, (d, s) in reg.items():
        if d == dims and abs(s - scale) < 1e-12:
            return name
    # Build an expression string from the dims
    return dim_fmt(dims)


def make_unitful(inner: Type, unit_str: str,
                 registry: Optional[dict] = None) -> TUnitful:
    """Parse a unit expression string into a TUnitful.

    unit_str examples: "N", "m/s²", "kg*m/s²", "?", "1"
    registry: user-defined units overlaid on _SI_BASE.
    """
    reg = dict(_SI_BASE)
    if registry:
        reg.update(registry)

    if unit_str == "?":
        return TUnitful(inner=inner, dims=None, scale=1.0, name="?")

    dims, scale = _parse_unit_expr(unit_str, reg)
    # Find canonical name:
    # 1. If the input unit_str is itself in the registry, use it directly
    # 2. Otherwise find the first matching entry (prefer shorter names)
    canon = unit_str
    if unit_str in reg:
        canon = unit_str   # exact match wins
    else:
        # Try to find a known named unit with same dims+scale
        for name, (d, s) in reg.items():
            if d == dims and abs(s - scale) < 1e-12:
                canon = name
                break
    return TUnitful(inner=inner, dims=dims, scale=scale, name=canon)


def _parse_unit_expr(expr: str, reg: dict) -> tuple[DimVec, float]:
    """Parse a unit expression like "N*m/s²" into (dims, scale).

    Supports: name, name*name, name/name, name^n, name²³
    Superscript digits: ² = ^2, ³ = ^3, etc.
    """
    # Normalise superscripts to ^n
    _SUPERSCRIPTS = {"⁰":"0","¹":"1","²":"2","³":"3","⁴":"4",
                     "⁵":"5","⁶":"6","⁷":"7","⁸":"8","⁹":"9","⁻":"-"}
    norm = ""
    i = 0
    while i < len(expr):
        c = expr[i]
        if c in _SUPERSCRIPTS:
            # Collect run of superscript digits
            sup = ""
            while i < len(expr) and expr[i] in _SUPERSCRIPTS:
                sup += _SUPERSCRIPTS[expr[i]]
                i += 1
            norm += "^" + sup
        else:
            norm += c
            i += 1
    expr = norm.strip()

    dims  = DIM_ZERO
    scale = 1.0

    # Split on / first to get numerator and denominator
    if "/" in expr:
        parts = expr.split("/", 1)
        num_dims, num_scale = _parse_unit_product(parts[0].strip(), reg)
        den_dims, den_scale = _parse_unit_product(parts[1].strip(), reg)
        dims  = dim_div(num_dims, den_dims)
        scale = num_scale / den_scale
    else:
        dims, scale = _parse_unit_product(expr, reg)

    return dims, scale


def _parse_unit_product(expr: str, reg: dict) -> tuple[DimVec, float]:
    """Parse "kg*m^2" or "N" into (dims, scale)."""
    dims  = DIM_ZERO
    scale = 1.0
    for part in expr.split("*"):
        part = part.strip()
        if not part or part == "1":
            continue
        # Check for exponent: kg^2
        exp = 1
        if "^" in part:
            base_str, exp_str = part.split("^", 1)
            exp  = int(exp_str)
            part = base_str.strip()
        if part not in reg:
            raise ValueError(f"Unknown unit '{part}'")
        u_dims, u_scale = reg[part]
        dims  = dim_mul(dims, dim_pow(u_dims, exp))
        scale = scale * (u_scale ** exp)
    return dims, scale


# ══════════════════════════════════════════════════════════════
@dataclass(eq=False)
class TUncertain(Type):
    inner: Type   # Uncertain[f64] — inner is f64

    def __repr__(self): return f"Uncertain[{self.inner}]"
    def __eq__(self, o): return isinstance(o, TUncertain) and self.inner == o.inner
    def __hash__(self): return hash(('uncertain', self.inner))

@dataclass(eq=False)
class TCorrelated(Type):
    inner: Type
    n:     int

    def __repr__(self): return f"Correlated[{self.inner}; {self.n}]"
    def __eq__(self, o): return isinstance(o, TCorrelated) and self.inner == o.inner and self.n == o.n
    def __hash__(self): return hash(('correlated', self.inner, self.n))


MESA_SBO_SIZE = 16  # bytes available for inline data in any T

@dataclass(eq=False)
class TAnyInterface(Type):
    """Stack-allocated existential: any Shape.

    32-byte layout: vtable_ptr(8) + inline_buf(16) + is_inline(8)
    Small types (<=16 bytes) stored inline, larger spill to heap.
    """
    iface: "TInterface"

    def __repr__(self): return f"any {self.iface.name}"
    def __eq__(self, o): return isinstance(o, TAnyInterface) and self.iface == o.iface
    def __hash__(self): return hash(('any', self.iface.name))

@dataclass(eq=False)
class TDynInterface(Type):
    """The type of a *any InterfaceName fat pointer.
    
    Represents a heap-allocated { vtable_ptr, concrete_data... } block.
    The vtable is a static table of function pointers for the interface methods.
    Type of *Shape, *Drawable, etc.
    """
    iface: "TInterface"   # the interface being pointed to

    def __repr__(self): return f"*{self.iface.name}"
    def __eq__(self, o): return isinstance(o, TDynInterface) and self.iface == o.iface
    def __hash__(self): return hash(('dyn', self.iface.name))


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

# Well-known primitive singletons
T_I8   = TInt(8,  True);  T_I16 = TInt(16, True)
T_I32  = TInt(32, True);  T_I64 = TInt(64, True)
T_U8   = TInt(8,  False); T_U16 = TInt(16, False)
T_U32  = TInt(32, False); T_U64 = TInt(64, False)
T_F32  = TFloat(32);      T_F64 = TFloat(64)
T_BOOL = TBool();         T_STR = TString()
T_VOID = TVoid();         T_ERR = TError()
T_INTLIT   = TIntLit()
T_FLOATLIT = TFloatLit()

PRIMITIVE_MAP: Dict[str, Type] = {
    "i8":  T_I8,  "i16": T_I16, "i32": T_I32, "i64": T_I64,
    "u8":  T_U8,  "u16": T_U16, "u32": T_U32, "u64": T_U64,
    "f32": T_F32, "f64": T_F64,
    "int": T_I64, "float": T_F64,   # convenient aliases
    "bool": T_BOOL, "str": T_STR, "void": T_VOID,
}

def default_numeric(ty: Type) -> Type:
    """Resolve unresolved literals to their default types."""
    if isinstance(ty, TIntLit):   return T_I64
    if isinstance(ty, TFloatLit): return T_F64
    return ty

def is_assignable(src: Type, dst: Type) -> bool:
    """
    True if a value of type src can be assigned to a variable of type dst.
    Handles literal widening.
    """
    if src.is_error() or dst.is_error(): return True   # don't cascade
    src = default_numeric(src)
    if src == dst: return True
    if isinstance(dst, (TErrorSet, TErrorSetUnion)) and isinstance(src, (TErrorSet, TErrorSetUnion)):
        return error_set_contains(dst, src)
    # integer literal → any integer or float
    if isinstance(src, TIntLit) and isinstance(dst, (TInt, TFloat)): return True
    # float literal → any float
    if isinstance(src, TFloatLit) and isinstance(dst, TFloat): return True
    # optional widening: T → ?T
    if isinstance(dst, TOptional) and is_assignable(src, dst.inner): return True
    return False

def unify(a: Type, b: Type) -> Type:
    """
    Unify two types, resolving unknowns and literals.
    Returns the unified type, or TError if they can't unify.
    """
    # resolve type variables
    if isinstance(a, TVar): a = a.root()
    if isinstance(b, TVar): b = b.root()

    # error propagation
    if a.is_error() or b.is_error(): return T_ERR

    # type variables
    if isinstance(a, TVar):
        a.resolve(b); return b
    if isinstance(b, TVar):
        b.resolve(a); return a

    # literal resolution
    if isinstance(a, TIntLit) and isinstance(b, (TInt, TFloat)):   return b
    if isinstance(b, TIntLit) and isinstance(a, (TInt, TFloat)):   return a
    if isinstance(a, TIntLit) and isinstance(b, TIntLit):          return T_I64
    if isinstance(a, TFloatLit) and isinstance(b, TFloat):         return b
    if isinstance(b, TFloatLit) and isinstance(a, TFloat):         return a
    if isinstance(a, TFloatLit) and isinstance(b, TFloatLit):      return T_F64

    # numeric widening in arithmetic contexts:
    # int + int → wider int, float beats int, larger float wins
    if isinstance(a, TInt) and isinstance(b, TInt):
        return TInt(max(a.bits, b.bits), a.signed or b.signed)
    if isinstance(a, TFloat) and isinstance(b, TFloat):
        return TFloat(max(a.bits, b.bits))
    if isinstance(a, TInt) and isinstance(b, TFloat): return b
    if isinstance(a, TFloat) and isinstance(b, TInt): return a

    if isinstance(a, (TErrorSet, TErrorSetUnion)) and isinstance(b, (TErrorSet, TErrorSetUnion)):
        return merge_error_sets(a, b)

    # structural unification
    if type(a) != type(b): return T_ERR

    if isinstance(a, TOptional):
        inner = unify(a.inner, b.inner)
        return TOptional(inner) if not inner.is_error() else T_ERR

    if isinstance(a, TPointer):
        inner = unify(a.inner, b.inner)
        return TPointer(inner) if not inner.is_error() else T_ERR

    if isinstance(a, TVec):
        inner = unify(a.inner, b.inner)
        if inner.is_error(): return T_ERR
        if a.size != b.size: return T_ERR
        return TVec(inner, a.size)

    if isinstance(a, TFun):
        if len(a.params) != len(b.params): return T_ERR
        if a.abi != b.abi: return T_ERR
        params = [unify(p, q) for p, q in zip(a.params, b.params)]
        ret    = unify(a.ret, b.ret)
        if any(p.is_error() for p in params) or ret.is_error(): return T_ERR
        return TFun(params, ret, abi=a.abi)

    if a == b: return a
    return T_ERR

def substitute(ty: Type, mapping: Dict[str, Type]) -> Type:
    """Substitute type variables in a generic type."""
    memo: Dict[int, Type] = {}

    def _substitute(cur: Type) -> Type:
        if isinstance(cur, TVar):
            root = cur.root()
            if root is not cur:
                return _substitute(root)
            if cur.name in mapping:
                return mapping[cur.name]
            return cur
        if isinstance(cur, TStruct):
            if cur.name in mapping:
                return mapping[cur.name]
            cached = memo.get(id(cur))
            if cached is not None:
                return cached
            clone = TStruct(
                name=cur.name,
                fields={},
                methods={},
                type_params=list(cur.type_params),
                type_args={},
            )
            memo[id(cur)] = clone
            clone.fields = {name: _substitute(field_ty) for name, field_ty in cur.fields.items()}
            clone.methods = {name: _substitute(method_ty) for name, method_ty in cur.methods.items()}
            clone.type_args = {name: _substitute(arg_ty) for name, arg_ty in cur.type_args.items()}
            return clone
        if isinstance(cur, TInt) or isinstance(cur, TFloat): return cur
        if isinstance(cur, (TBool, TString, TVoid, TError)): return cur
        if isinstance(cur, TErrorSetUnion): return TErrorSetUnion(tuple(_substitute(m) for m in cur.members), cur.name)
        if isinstance(cur, TOptional): return TOptional(_substitute(cur.inner))
        if isinstance(cur, TPointer):  return TPointer(_substitute(cur.inner))
        if isinstance(cur, TVec):      return TVec(_substitute(cur.inner), cur.size)
        if isinstance(cur, TFun):
            return TFun(
                [_substitute(p) for p in cur.params],
                _substitute(cur.ret),
            )
        if isinstance(cur, TUnion):
            return TUnion(
                cur.name,
                {name: _substitute(payload) if payload is not None else None for name, payload in cur.variants.items()},
                list(cur.type_params),
            )
        if isinstance(cur, TTuple):
            return TTuple([(n, _substitute(t)) for n, t in cur.fields])
        return cur

    return _substitute(ty)


def format_type_for_user(ty: Type) -> str:
    """Render an internal type using user-facing Mesa syntax."""
    if isinstance(ty, TVar):
        root = ty.root()
        return format_type_for_user(root) if root is not ty else "_"
    if isinstance(ty, TIntLit):
        return "int"
    if isinstance(ty, TFloatLit):
        return "float"
    if isinstance(ty, TInt):
        return "int" if ty.signed and ty.bits == 64 else repr(ty)
    if isinstance(ty, TFloat):
        return "float" if ty.bits == 64 else repr(ty)
    if isinstance(ty, (TBool, TString, TVoid)):
        return repr(ty)
    if isinstance(ty, TOptional):
        return f"?{format_type_for_user(ty.inner)}"
    if isinstance(ty, TPointer):
        return f"*{format_type_for_user(ty.inner)}"
    if isinstance(ty, TSlice):
        return f"[]{format_type_for_user(ty.inner)}"
    if isinstance(ty, TArray):
        return f"[{format_type_for_user(ty.inner)}; {ty.size}]"
    if isinstance(ty, TVec):
        inner = format_type_for_user(ty.inner)
        return f"vec[{inner}; {ty.size}]" if ty.size is not None else f"vec[{inner}]"
    if isinstance(ty, TMat):
        inner = format_type_for_user(ty.inner)
        if ty.rows is not None and ty.cols is not None:
            return f"mat[{inner}; {ty.rows}, {ty.cols}]"
        return f"mat[{inner}]"
    if isinstance(ty, TTuple):
        parts = [f"{n}: {format_type_for_user(t)}" if n else format_type_for_user(t)
                 for n, t in ty.fields]
        return ".{" + ", ".join(parts) + "}"
    if isinstance(ty, TFun):
        params = ", ".join(format_type_for_user(p) for p in ty.params)
        if ty.abi is not None:
            return f"[{ty.abi}]fun({params}) {format_type_for_user(ty.ret)}"
        return f"fun({params}) {format_type_for_user(ty.ret)}"
    if isinstance(ty, TStruct):
        if ty.type_args:
            args = ", ".join(format_type_for_user(v) for v in ty.type_args.values())
            return f"{ty.name}[{args}]"
        return ty.name
    if isinstance(ty, TUnion):
        return ty.name
    if isinstance(ty, TInterface):
        return ty.name
    if isinstance(ty, TNamespace):
        return ty.name
    if isinstance(ty, TAnyInterface):
        return f"any {ty.iface.name}"
    if isinstance(ty, TDynInterface):
        return f"*any {ty.iface.name}"
    if isinstance(ty, TErrorSet):
        return ty.name
    if isinstance(ty, TErrorSetUnion):
        return " | ".join(m.name for m in ty.members)
    if isinstance(ty, TErrorUnion):
        error_name = format_type_for_user(ty.error_set) if ty.error_set else "!"
        return f"{error_name}!{format_type_for_user(ty.payload)}"
    if isinstance(ty, TUnitful):
        unit = ty.name or ("?" if ty.dims is None else dim_fmt(ty.dims))
        return f"{format_type_for_user(ty.inner)}`{unit}`"
    if isinstance(ty, TUncertain):
        if isinstance(ty.inner, TUnitful):
            inner = format_type_for_user(ty.inner.inner)
            unit = ty.inner.name or ("?" if ty.inner.dims is None else dim_fmt(ty.inner.dims))
            return f"({inner} +- {inner})`{unit}`"
        inner = format_type_for_user(ty.inner)
        return f"{inner} +- {inner}"
    return repr(ty)
