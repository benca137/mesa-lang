# Mesa Alpha Feature Matrix

Alpha means Mesa is trustworthy for small real programs. Features below are
classified by their intended alpha contract, not by long-term language ambition.

## Supported For Alpha

- Syntax: modules, imports, functions, methods, structs, unions, interfaces,
  `def`, type aliases, attributes, blocks, loops, `match`, optionals, closures,
  test blocks, and string interpolation.
- Types: integers, floats, bool, str, void, pointers, arrays, slices, vecs,
  tuples, structs, unions, interfaces, optionals, error unions, and C ABI
  function types.
- Semantics: generic functions and types, interface dispatch, overload-like
  methods through interfaces, exhaustiveness checks, `defer`, basic `with`
  allocator contexts, local packages, `.pkg` facades, and compile-error tests.
- Backend/runtime: C99 codegen, linked GC/runtime support, allocator context
  stack, generated test runners, and host C linking on Unix-like systems.
- FFI: `@extern(lib)`, `@extern(lib, name = "...")`, `@layout(.c)`, opaque
  foreign types, `[.c]fun(...) Ret`, and `b.linkLibrary(..., abi = .c)`.
- Stdlib: `io`, `mem`, and `ffi`.
- Build: `mesa init`, `build`, `run`, `test`, `pkg add`, package roots,
  package-style library targets, executable targets, and C library imports.

## Experimental For Alpha

- MIR work remains wanted but is developed on the `mir` branch and merged by
  PRs behind a clear policy.
- Nested `.pkg` namespace preservation is tracked by GitHub issue #7.
- `esc` and advanced `with` promotion are tracked by GitHub issues #1 and #2.
- MesaLSP lives outside this repository and is tracked separately.

## Unsupported Or Out Of Scope For Alpha

- `fmt` package; string interpolation is the alpha formatting story.
- Static or shared Mesa library artifact emission.
- Dependency resolution beyond local package roots.
- Bindgen/header parsing, variadics, C++, Objective-C, Win32 ABI design, and
  non-C ABI families.
- Broad stdlib coverage such as process management, serialization, rich time,
  networking, and full filesystem/path APIs.

## Required Coverage

- Every supported feature family should have at least one passing Mesa-native
  test and one negative compile-error or Python regression test.
- Unsupported combinations must either have a stable diagnostic or be listed
  here as explicitly out of alpha scope.
