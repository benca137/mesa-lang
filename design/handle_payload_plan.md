# Handle Payload Plan

## Summary

Add first-pass payload destructuring to `handle |e| { ... }` by supporting `match e { .Variant(payload) => ... }` inside handle blocks.

This pass does not turn the handle binding into a richer first-class error object. Payload access is match-only.

## Key Changes

- Keep the source-level handle binding typed as the concrete error set when known.
- Extend handle lowering so each handle target carries both:
  - the error tag
  - the raw payload bytes from the failing result
- Extend handle-scope symbol metadata so codegen can recognize the current handle binding as having an associated payload buffer.
- Relax checker restrictions only for `match e` on the active handle binding:
  - allow `.Variant(x)` and `.Variant(x, y)` destructuring
  - keep payload access outside variant-pattern matching unsupported
- Reuse the existing catch-style payload extraction model in match codegen:
  - switch on the tag
  - memcpy payload bytes into arm-local bindings
  - emit the arm body normally

## Tests

- Function-level handle payload match:
  - `handle |e| { match e { .Bad(msg) => ... } }`
- Local `with` handle payload match:
  - same destructuring inside `with ... handle`
- Tuple payload destructuring if current pattern parsing already supports it
- Unit variants remain unchanged
- Existing `catch` payload tests continue to pass

## Assumptions

- Payload support is match-only in v1.
- No field access or general payload reads from the bare handle binding.
- Existing result-struct payload storage remains the wire format.
