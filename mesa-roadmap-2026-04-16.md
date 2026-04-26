# Mesa Roadmap

Date: 2026-04-16

This roadmap is based on the current codebase and test suite, not the design doc. It targets three release states:

- `alpha`: trustworthy for small real programs
- `beta`: usable by external early adopters for medium-sized projects
- `v0.1`: narrow but stable public release

Priority order:

1. Semantic completion
2. Backend/runtime hardening
3. Stdlib
4. FFI
5. Packaging/build system
6. Stabilization
7. Tooling deepening

## Current Read

Mesa already has a substantial language core, checker, C backend, build/test CLI, and a minimal LSP. It feels like a serious pre-alpha rather than a toy compiler.

The main gaps are not basic syntax coverage. The biggest remaining work is finishing semantics, hardening the backend/runtime, broadening the standard library, formalizing the FFI and package/build model, then deepening tooling around a stable core.

## Phase 1: True Alpha

Goal: Mesa is trustworthy for small real programs.

### 1. Semantic completion

- Close all known semantic holes in the currently supported language.
- Resolve explicit `not supported yet` paths, especially around `with`, `esc`, allocator regions, cleanup-bearing scopes, and error-handling interactions.
- Audit the current language surface and mark each feature as one of:
  - supported
  - experimental
  - unsupported
- Expand compile-error and regression coverage so every supported feature has:
  - at least one passing example
  - at least one failing example
  - at least one edge-case or interaction test
- Prefer removing ambiguity over adding surface area.

Alpha gate:

- No known semantic holes in the supported alpha subset.
- Unsupported combinations fail clearly and intentionally.

Likely work areas:

- `src/checker.py`
- `src/analysis.py`
- `src/parser.py`
- `tests/projects/compile_error_suite`
- feature test projects under `tests/`

### 2. Backend/runtime hardening

- Make the C backend deterministic and boring across the existing corpus.
- Add golden-style regression coverage for emitted C on representative programs.
- Harden runtime behavior around:
  - cleanup ordering
  - `defer`
  - `handle`
  - allocator context stack
  - GC roots and collection safety
  - result/error propagation paths
- Add stress tests and fuzz-style tests that exercise checker/codegen/runtime interactions.
- Decide MIR scope clearly:
  - either keep MIR experimental and out of the release contract
  - or use it as an internal validation/lowering layer without half-adopting it

Alpha gate:

- All corpus tests pass reliably.
- Runtime-sensitive semantics have dedicated regression coverage.
- No known backend crashes on valid supported programs.

Likely work areas:

- `src/ccodegen.py`
- `src/gc_runtime.py`
- `src/analysis.py`
- `src/mir.py`
- `src/mirgen.py`
- `tests/python`
- `tests/projects`

### 3. Stdlib minimum viable expansion

- Grow the stdlib just enough to make small real programs pleasant.
- Prioritize:
  - `fmt`
  - string/text helpers
  - vec/slice helpers
  - small filesystem/path layer
  - math basics
  - better test helpers
- Keep the stdlib curated and coherent rather than broad and uneven.
- Add Mesa-native tests for each std package.

Alpha gate:

- A small application should not feel blocked by obvious missing basics.

Likely work areas:

- `std/`
- `src/stdlib.py`
- new tests under `tests/projects` or package-specific suites

### 4. FFI floor

- Formalize the existing `extern` model.
- Define ABI and ownership rules for:
  - `str`
  - slices
  - structs
  - pointers
  - returned memory
  - allocator expectations
- Clarify symbol naming and `link_name` behavior.
- Add one blessed C interop example and tests.

Alpha gate:

- A simple Mesa-to-C interop example works and is documented by tests.

Likely work areas:

- `src/checker.py`
- `src/ccodegen.py`
- `examples/`
- new FFI-focused tests

### 5. Build/package floor

- Extend the current build system enough for real local projects.
- Add support for:
  - library targets
  - multi-target builds
  - clearer target graph validation
  - stronger diagnostics
- Keep dependency resolution out of alpha if necessary, but local multi-package builds should be solid.

Alpha gate:

- Small library + executable projects build cleanly and predictably.

Likely work areas:

- `src/buildsys.py`
- `src/mesa.py`
- `tests/python/test_buildsys.py`
- multi-package fixtures under `tests/projects`

### 6. Stabilization basics

- Add an alpha release checklist:
  - parser
  - checker
  - analysis
  - codegen
  - runtime
  - stdlib
  - project fixtures
- Start classifying bugs by subsystem to improve triage and regression tracking.

### 7. Tooling floor

- Keep compiler-facing editor metadata modest for alpha.
- Track MesaLSP in the separate MesaLSP project instead of this repository.
- Do not count an in-tree `src.lsp.server` as part of the alpha contract.
- Hold off on broader tooling depth until semantics and backend behavior stabilize.

Supporting alpha docs:

- `docs/alpha-feature-matrix.md`
- `docs/alpha-release-checklist.md`
- `docs/alpha-ffi-abi.md`
- `docs/alpha-stdlib.md`

Alpha acceptance criteria:

- `pytest` passes.
- Mesa-native suites pass.
- A small app, a small library, and one C interop sample all build successfully.
- Supported language behavior is clear and test-backed.

## Phase 2: Beta

Goal: external early adopters can build medium-sized projects.

### 1. Semantic completion and freeze

- Freeze the beta language subset.
- Lock down generics, interfaces, exhaustiveness, inference, diagnostics, and region/escape semantics so future changes are regression-tested rather than rediscovered.
- Add compatibility tests so semantic changes are explicit.

### 2. Backend/runtime hardening

- Add platform confidence beyond the current Unix/C99 flow.
- Track compile-time, binary-size, and runtime regressions.
- If MIR remains in scope, add a clear role for it in lowering or validation.
- Harden ABI boundaries for generated binaries and FFI-facing code.

### 3. Stdlib expansion

- Add the first complete general-purpose layer:
  - collections
  - richer string/text support
  - filesystem/process
  - time/random
  - serialization basics
- Mark std packages as stable or experimental.

### 4. FFI maturation

- Support realistic native-library linking workflows.
- Add examples for:
  - Mesa calling C libraries
  - C hosting or integrating Mesa-produced code where practical
- Nail down pointer/ownership conventions in tests and docs.

### 5. Packaging/build evolution

- Add dependency declarations and local/path dependencies.
- Add library artifacts as first-class outputs.
- Support multiple executables/libraries, profiles, and clearer artifact layout.
- Start defining package identity and versioning.

### 6. Stabilization

- Create a conformance suite separate from feature/regression suites.
- Define what kinds of breaking changes are still allowed before `v0.1`.

### 7. Tooling deepening

- Expand LSP to include:
  - references
  - rename
  - workspace symbols
  - stronger completion quality
- Ship a formatter by beta.
- Improve diagnostics UX and test runner ergonomics.

Beta acceptance criteria:

- External users can build a medium-sized multi-package project.
- Core stdlib, FFI, and build flows are documented by examples and tests.
- Basic editing workflows are pleasant enough for everyday use.

## Phase 3: v0.1

Goal: Mesa has a narrow but stable public contract.

### 1. Public contract freeze

- Freeze the `v0.1` language subset.
- Freeze the core stdlib surface.
- Freeze the supported FFI contract.
- Freeze the supported package/build behavior.
- Explicitly keep anything still fuzzy or incomplete out of `v0.1`.

### 2. Release hardening

- Publish:
  - platform support matrix
  - compatibility policy
  - migration notes
  - known limitations
- Separate experimental/internal components from supported user-facing behavior.

### 3. Stable packaging/build story

- Ship a minimal but real package model:
  - package metadata
  - dependency resolution
  - lock behavior
  - reproducible builds
  - stable library and executable targets

### 4. Stable developer experience

- Formatter, core LSP workflows, syntax support, docs, and test UX should be stable enough that new users can succeed without reading compiler internals.
- Provide polished starter projects and canonical examples.

v0.1 acceptance criteria:

- Mesa can be recommended for real experimental projects.
- The supported subset is clear, narrow, and stable.
- Release engineering is versioned and repeatable.

## Execution Order Within Each Phase

1. Semantic completion
2. Backend/runtime hardening
3. Stdlib
4. FFI
5. Packaging/build
6. Stabilization
7. Tooling

## Recommended Immediate Next Steps

1. Build a feature/support inventory from the current AST, checker, analysis, and backend.
2. Burn down every explicit semantic gap in checker/codegen/runtime.
3. Turn alpha release gates into automated tests.
4. Expand stdlib only after the underlying semantics/runtime are stable.
5. Evolve the build/package system once Mesa programs can rely on stable language behavior.

## Suggested Milestones

### Alpha M1: Semantic closure

- Inventory all current language features.
- Mark supported vs experimental.
- Eliminate known semantic gaps.
- Expand negative and interaction tests.

### Alpha M2: Backend/runtime confidence

- Add backend golden tests.
- Add cleanup/GC/error-path stress coverage.
- Resolve deterministic codegen/runtime issues.

### Alpha M3: Usability floor

- Add minimum stdlib packages.
- Land FFI floor.
- Extend build system for libraries and multi-target projects.
- Add a small set of canonical examples.

### Beta M1: Medium-project workflows

- Package/dependency improvements.
- Wider stdlib.
- stronger FFI workflows.

### Beta M2: Tooling and conformance

- Formatter.
- richer LSP.
- conformance suite.

### v0.1 M1: Freeze and ship

- Freeze public surface.
- document compatibility and limits.
- release a stable narrow contract.
