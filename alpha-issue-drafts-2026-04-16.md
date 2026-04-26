# Alpha Issue Drafts

Date: 2026-04-16

These issue drafts came from reviewing the Phase 1 "True Alpha" roadmap plus explicit unfinished markers in `src/`.

Issue creation was attempted against `benca137/mesa-lang`, but the GitHub integration returned `403 Resource not accessible by integration`.

## 1. Support `esc` inside `with` expressions that use a local `handle` block

### Why

Phase 1 alpha explicitly calls out unresolved semantics around `with`, `esc`, allocator regions, cleanup-bearing scopes, and error-handling interactions. The checker still rejects one of those combinations outright:

- `src/checker.py:1456` emits `"'esc' with a local with-handle is not supported yet"`
- diagnostic code: `esc-local-handle-unsupported`

That leaves a semantic hole in exactly the area the roadmap flags as alpha-critical.

### What to do

- Define the intended semantics for `esc` when a `with` expression has a local `handle` block.
- Implement the checker/runtime/codegen behavior needed to make that combination work, or explicitly mark it unsupported in the alpha surface if the semantics are not ready.
- Make cleanup ordering and error propagation behavior test-backed.

### Suggested acceptance criteria

- At least one positive test covering `esc` from a `with ... handle` expression.
- At least one negative test covering invalid or error-ordering cases.
- Clear diagnostics if the feature remains intentionally unsupported.

### References

- `Mesa/mesa-roadmap-2026-04-16.md` Phase 1 / Semantic completion
- `src/checker.py:1456-1458`
- adjacent cleanup/error diagnostics in `src/checker.py` for `try-cleanup-needs-local-handle`

## 2. Finish or narrow `esc` / `with` value-promotion semantics beyond cloneable types

### Why

The alpha roadmap calls out allocator regions and escape semantics as unfinished. The checker currently hard-rejects a broad set of values for both `with` promotion and `esc`:

- `src/checker.py:1700` and `src/checker.py:2090`: `"'with' allocation target does not yet support values of type ..."`
- `src/checker.py:1718` and `src/checker.py:2106`: `"'esc' does not yet support values of type ..."`
- diagnostic code: `esc-unsupported-type`

Today the implementation only supports the subset described in the hints: scalar/string/vec/struct/union made of cloneable fields. That is an explicit semantic hole in the allocator story.

### What to do

- Inventory which Mesa value categories are supposed to be alpha-supported for region promotion.
- Either implement promotion/escape for the remaining supported categories or clearly classify them as unsupported or experimental.
- Add passing and failing tests for each supported category and each intentionally unsupported category.

### Suggested acceptance criteria

- A documented feature matrix for `esc` and `with allocator` promotion.
- Positive tests for every supported promotable type family.
- Negative tests with stable diagnostics for intentionally unsupported types.

### References

- `Mesa/mesa-roadmap-2026-04-16.md` Phase 1 / Semantic completion
- `src/checker.py:1700-1704`
- `src/checker.py:1718-1720`
- `src/checker.py:2090-2094`
- `src/checker.py:2106-2108`
- `tests/projects/compile_error_suite/src/main.mesa:113-116`

## 3. Decide MIR alpha status and remove the current half-adopted state

### Why

Phase 1 backend/runtime hardening explicitly says to decide MIR scope clearly:

- either keep MIR experimental and out of the release contract
- or use it as an internal validation/lowering layer without half-adopting it

The source currently describes MIR as unfinished integration work:

- `src/mir.py:4-6` says it is `"not wired into the main compiler pipeline yet"`
- the CLI pipeline in `src/mesa.py` still goes parse -> type check -> analysis -> C codegen -> cc
- there is a dedicated MIR test suite in `tests/test_mirgen.py`

That means MIR exists, is tested, but is not yet clearly part of the compiler contract.

### What to do

- Make an explicit alpha decision about MIR.
- If MIR is experimental: document that, gate it behind an explicit developer-facing entrypoint, and keep unsupported constructs out of the alpha contract.
- If MIR is part of the compiler plan: define its required coverage, add regression expectations, and wire it into a supported workflow.

### Suggested acceptance criteria

- A documented alpha policy for MIR.
- Either an explicit experimental-only interface or a defined compiler-internal role.
- No ambiguity about whether MIR failures block alpha.

### References

- `Mesa/mesa-roadmap-2026-04-16.md` Phase 1 / Backend-runtime hardening
- `src/mir.py:1-6`
- `src/mesa.py` compilation pipeline docstring
- `tests/test_mirgen.py`

## 4. Complete MIR lowering for remaining statement and expression forms

### Why

`src/mirgen.py` still throws explicit `not lowered yet` or `unsupported` errors for several AST forms that are already part of the frontend language surface:

- `src/mirgen.py:388`: `let` without initializer
- `src/mirgen.py:417` and `src/mirgen.py:2008`: compound assignment
- `src/mirgen.py:623-624`: `IfUnwrap` and `WhileUnwrap`
- `src/mirgen.py:463`: unsupported statements
- `src/mirgen.py:626`: unsupported expressions

The checker/parser already know about `IfUnwrap` and `WhileUnwrap`, and the C backend has dedicated support for them. MIR coverage is lagging the supported source language.

### What to do

- Decide which statement/expression forms MIR v1 must cover.
- Implement lowering for the missing forms, or explicitly reject MIR emission earlier with intentional diagnostics.
- Add one MIR regression per newly-supported construct.

### Suggested acceptance criteria

- MIR can lower the same statement/expression subset that alpha considers supported, or it fails early with a clear contract-level diagnostic.
- `tests/test_mirgen.py` covers each supported form.

### References

- `src/mirgen.py:388`
- `src/mirgen.py:417`
- `src/mirgen.py:463`
- `src/mirgen.py:623-626`
- `src/mirgen.py:2008`
- `src/checker.py:2067-2070`
- `src/ccodegen.py:2466`
- `src/ccodegen.py:4005`

## 5. Complete MIR lowering for match-pattern and `for`-iteration coverage

### Why

Beyond basic expression lowering, MIR still has explicit gaps around pattern-heavy control flow and iteration:

- `src/mirgen.py:941`: match lowering currently supports only tagged values
- `src/mirgen.py:965` and `src/mirgen.py:2167`: match patterns `not lowered yet`
- `src/mirgen.py:1235-1253`: unsupported iterable type helpers for `for-in`
- `src/mirgen.py:1298`: unsupported `for` pattern in MIR lowering
- `src/mirgen.py:1303` and `src/mirgen.py:1595`: `for-in` lowering currently supports only vec/slice/array iterables

This prevents MIR from representing a meaningful portion of Mesa's existing control-flow surface.

### What to do

- Define the alpha-supported MIR subset for `match` and `for-in`.
- Implement the missing lowering for supported pattern/iterator forms.
- Add regression coverage for each supported match-pattern family and iterable family.

### Suggested acceptance criteria

- MIR supports the same `match`/`for-in` subset that alpha calls supported, or explicitly rejects out-of-contract constructs before lowering.
- Tests cover at least one happy path and one unsupported-path diagnostic per family.

### References

- `src/mirgen.py:941-965`
- `src/mirgen.py:1235-1253`
- `src/mirgen.py:1298-1303`
- `src/mirgen.py:1595`
- `tests/test_mirgen.py`

## Notes On Exclusions

- I did not turn `src/buildsys.py`'s `build.mesa` v1 limitations into issues here because they read like intentional scope limits for the build DSL rather than unfinished compiler-core behavior.
- I also left out the unit-system TODOs and `sqrt()` dynamic-unit gap because they look real, but not as central to the alpha compiler contract as the `esc`/`with` and MIR gaps above.
