# Mesa IR Architecture Design Draft

## Summary

Mesa should move from AST-driven code generation to a staged IR pipeline:

`AST -> checked Mesa types -> Mesa MIR -> portable LIR -> arm64 LIR -> machine code`

The first native backend target is arm64. The architecture should still keep x86-64 viable by sharing a portable low-level IR before target-specific lowering.

The key design choice is to use two main internal IRs:

- `Mesa MIR`: human-readable, optimization-facing, Mesa-aware SSA
- `portable LIR`: lower-level, backend-facing SSA shared by arm64 and x86-64

This keeps Mesa-specific semantics visible long enough to design and run meaningful optimization passes without forcing each backend to rediscover language behavior from the AST.

## Goals

- Replace direct AST-to-C / AST-to-LLVM lowering with a real middle-end.
- Make IR readable enough to reason about optimization passes by inspection.
- Preserve Mesa-specific semantics where they matter:
  - allocator regions and cleanup
  - escape/promotion
  - error values and propagation
  - optionals
  - tagged values and pattern dispatch
- Support arm64 first without painting the compiler into an arm-only corner.
- Leave room for later JIT support using the same IR stack.

## Non-Goals

- Do not make the IR a generic frontend-neutral VM.
- Do not preserve every Mesa surface construct literally in the IR.
- Do not design around WebAssembly or microcontrollers in the first milestone.
- Do not keep source-level unions as sacred; lower them into tagged-value operations when useful.

## IR Stack

### 1. Mesa MIR

Mesa MIR is the main optimization IR.

Properties:
- typed
- SSA-based
- block-structured CFG
- human-readable text form
- Mesa-aware operations for regions, results, optionals, and tagged values

Mesa MIR should still express:
- `with` regions
- cleanup ordering
- `esc` / promotion to outer allocator
- `try` / `handle` as explicit CFG
- optionals and results as structured values
- tagged dispatch for pattern matching

Mesa MIR should not carry source syntax unnecessarily. It should normalize:
- `if` into branches
- `match` into `switch.*`
- `orelse` into optional/result control flow
- source unions into tagged-value creation and switching ops

### 2. Portable LIR

Portable LIR is shared by arm64 and x86-64.

Properties:
- typed or lightly typed
- SSA-based
- explicit loads, stores, stack slots, aggregate layout use
- target-neutral instruction vocabulary
- no named physical registers
- explicit ABI shaping for calls and returns

Portable LIR should own:
- aggregate decomposition
- explicit memory access
- call argument expansion
- return convention lowering
- stack slot introduction
- control-flow simplification closer to machine code

Portable LIR should not mention:
- `x0`, `w0`, flags registers
- AAPCS64 details by name
- x86 condition codes by name

### 3. Target LIR

Target LIR is target-specific and short-lived.

For arm64:
- physical registers
- AAPCS64 calling convention
- concrete branch/select forms
- instruction selection
- stack frame layout
- register allocation constraints

x86-64 should get its own target LIR later from the same portable LIR.

## Why Multiple IRs

### Pros
- Mesa MIR stays readable and useful for optimization design.
- Portable LIR prevents arm64-specific choices from contaminating the whole compiler.
- Backends become smaller and easier to reason about.
- Future x86-64 and JIT work reuse most of the pipeline.

### Cons
- More compiler infrastructure.
- More validation points.
- Pass ownership must be defined carefully.

### Chosen Tradeoff
Use multiple IRs because Mesa has enough language semantics that a single IR will either:
- stay too high-level and burden every backend, or
- become too low-level and erase the right place for Mesa-aware analysis.

## Text Syntax

The printed IR should optimize for human reading.

Conventions:
- no `fn` keyword on function declarations
- Python-like indentation
- `:` after function and block headers
- human-readable block names such as `begin`, `parse_qty`, `cleanup_error`
- `%name` for SSA values
- `@name` for functions and globals
- postfix return type on declarations: `@name(...) ReturnType:`
- `->` for expression result/type annotation when helpful
- direct `@foo(...)` means direct call
- indirect calls use a distinct keyword such as `invoke`

Example style:

```text
@load_record(%out: region<arena>, %line: str) LoadResult:
begin:
    %scratch = region.new arena(4096)
    goto split_line

split_line:
    %cols = @split_csv(%scratch, %line) -> vec<str>@%scratch
    %len = vec.len %cols
    cmp.goto %len >= 3: parse_qty else: missing_field
```

This syntax should be used for dumps, tests, and design discussion. The in-memory representation does not need to preserve exact formatting.

## Mesa MIR Instruction Model

### Core categories
- constants and literals
- arithmetic and comparisons
- memory-independent structured value ops
- CFG ops
- region ops
- tagged-value ops
- result/optional ops
- direct and indirect calls

### Representative ops
- `goto target`
- `cmp.goto %a >= %b: then_block else: else_block`
- `switch.tag %v: VariantA -> a, VariantB -> b`
- `switch.result %r: ok -> ok_block(%r.ok), err -> err_block(%r.err)`
- `region.new arena(4096)`
- `region.promote %v from %src to %dst`
- `region.cleanup %r free`
- `make.tagged Type.Variant(%payload)`
- `read.tag %v`
- `read.payload %v as Type.Variant`
- `result.ok(%v)`
- `result.err(%e)`
- `optional.some(%v)`
- `optional.none`
- `@callee(...)`
- `invoke %fn(...)`

### Type annotations
Type annotations should be present where helpful, not everywhere. Rule of thumb:
- keep function signatures typed
- keep block parameters typed when needed
- allow result annotations on non-obvious operations
- avoid cluttering every arithmetic line

Region attachment may appear in value types when relevant:
- `str@%scratch`
- `vec<str>@%out`

## Data Model Lowering

### Structs and tuples
Keep as aggregates in Mesa MIR.

### Optionals
Lower to explicit optional operations, not null conventions.

### Errors and results
Model as structured result values plus CFG. `try` is always an explicit branch/switch in MIR.

### Tagged unions / ADTs
Do not preserve source unions as syntax if that gets in the way. Lower them into tagged-value operations:
- `make.tagged`
- `read.tag`
- `read.payload`
- `switch.tag`

This keeps pattern matching and optimization straightforward without committing to source-level union syntax internally.

### Interfaces
Keep dispatch explicit in MIR, but lower representation details later.

### Scientific types
For the first cut:
- unitful and uncertainty semantics are checked in the frontend
- MIR lowers them to ordinary structured values and helper operations
- do not make them first-class machine model concepts yet

## Region and Allocator Semantics

This is one of the main reasons Mesa needs its own MIR.

Mesa MIR must model:
- region creation
- active region identity
- cleanup points
- promotion to outer region
- values tied to a region

Design intent:
- region legality should be checkable in MIR
- cleanup ordering should not depend on backend cleverness
- `esc` should already be lowered before portable LIR

Region-sensitive passes should be able to answer:
- which values are region-owned
- whether a value crosses a cleanup boundary illegally
- whether promotion/cloning is required
- whether cleanup edges are complete

## Lowering Responsibilities

### Frontend -> Mesa MIR
- normalize control flow
- lower `try`, `handle`, `orelse`, `match`
- lower source unions into tagged-value ops
- attach region ownership to values where needed
- preserve checked Mesa types

### Mesa MIR -> portable LIR
- lower structured values toward concrete layouts
- introduce explicit loads/stores
- lower calls/returns toward ABI shapes
- decompose aggregates when useful
- prepare stack slots

### Portable LIR -> arm64 LIR
- assign AAPCS64 call/return locations
- choose arm64 instruction forms
- materialize branch conditions
- lower memory addressing
- prepare for register allocation and frame layout

## Optimization Pass Placement

### Mesa MIR passes
- CFG cleanup
- constant folding
- dead code elimination
- copy propagation
- common subexpression elimination for pure ops
- region escape analysis
- cleanup edge verification
- result/optional simplification
- match/tag simplification
- inlining policy for Mesa-level functions

### Portable LIR passes
- stack slot coalescing
- load/store forwarding
- aggregate copy simplification
- branch folding
- calling convention cleanup
- lower-level DCE and copy propagation

### arm64 LIR passes
- instruction selection cleanup
- register allocation
- spill insertion
- branch shortening / final scheduling if desired

## Verification

Add verifier passes at each boundary.

### Mesa MIR verifier
- SSA correctness
- block parameter / join correctness
- type consistency
- valid region ownership and promotion
- cleanup dominance / exit correctness
- result/tagged-value structural correctness

### Portable LIR verifier
- explicit operand typing/layout legality
- memory op correctness
- call/return ABI shape validity
- stack slot consistency

### arm64 verifier
- legal register classes
- legal instruction operands
- valid frame references
- calling convention adherence

## Testing

- golden textual tests for Mesa MIR dumps
- golden textual tests for portable LIR dumps
- execution equivalence tests against current language fixtures
- targeted region tests:
  - cleanup ordering
  - illegal escape rejection
  - legal promotion behavior
- targeted result/optional tests:
  - `try`
  - `handle`
  - `catch`
  - `orelse`
- arm64 codegen smoke tests first
- x86-64 remains a follow-on target using the same portable LIR inputs

## Initial Milestones

### Milestone 1
- define Mesa MIR data structures and printer
- lower a core subset:
  - arithmetic
  - locals
  - branches
  - returns
  - structs
  - optionals
  - results
  - tagged dispatch
  - basic region ops

### Milestone 2
- add Mesa MIR verifier
- route current C backend from MIR instead of AST if useful as a transition step

### Milestone 3
- define portable LIR
- lower Mesa MIR to portable LIR

### Milestone 4
- implement arm64 target LIR and code emission
- compile a small Mesa subset natively on arm64

### Milestone 5
- expand coverage to interfaces, generics, and more allocator-heavy code paths

## Assumptions

- arm64 is the first native backend.
- x86-64 should remain a planned second backend through shared portable LIR.
- JIT support is a future consumer of the same IR stack, not a separate architecture.
- Source unions need not remain literal IR constructs once lowered to tagged-value ops.
- Printed IR should favor readability over matching existing compiler IR traditions exactly.
- `%` for SSA values is useful and should be adopted.
