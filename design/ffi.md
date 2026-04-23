# FFI Syntax Draft

## Summary

Define a syntax-first FFI surface for Mesa that replaces the current `extern` keyword form with compiler-known directives and keeps the raw foreign boundary explicit and zero-cost.

Chosen defaults:
- `extern` is no longer a declaration keyword.
- `@extern(lib)` binds a function or opaque type to a foreign library namespace.
- `opaque` remains a keyword and is used for foreign nominal handle types.
- `@layout(.c)` marks structs that must follow the target platform's C ABI layout rules.
- `[.c]fun(...) Ret` is the spelling for C-ABI function types such as callbacks.
- The foreign symbol name defaults from the Mesa declaration name.
- `name = "..."` exists only for symbol mismatches such as `_fopen`.
- The linked foreign namespace comes from `b.linkLibrary(..., abi = .c)`.

This document is intentionally syntax-first. It does not attempt to design full bindgen, header parsing, or the full raw type matrix for FFI v1.

## Key Changes

### 1. Replace keyword-style extern declarations

Use `@extern(...)` on declarations instead of `extern fun`.

Canonical forms:

```mesa
import ffi
import libc

@extern(libc)
opaque type FILE

@extern(libc)
fun fopen(path: *ffi.c_char, mode: *ffi.c_char) *FILE

@extern(libc)
fun fclose(file: *FILE) ffi.c_int
```

Meaning:
- `@extern(libc)` declares that the symbol or type belongs to the foreign namespace `libc`.
- For functions, the foreign symbol name defaults to the Mesa declaration name.
- For opaque types, the foreign type name defaults to the Mesa type name.

Use `name = "..."` only when the foreign name differs:

```mesa
@extern(libc, name = "_fopen")
fun fopen(path: *ffi.c_char, mode: *ffi.c_char) *FILE
```

This keeps common bindings terse while staying honest about the fact that symbol existence is still fundamentally verified at link time unless Mesa later gains declared foreign member inventories.

### 2. Make C layout explicit on aggregate types

Use `@layout(.c)` on structs that must match the target platform's C ABI layout.

```mesa
@layout(.c)
struct Timespec {
    tv_sec: ffi.time_t,
    tv_nsec: ffi.c_long,
}
```

Meaning:
- `.c` is an ABI/layout convention tag, not a statement about the source language a library was written in.
- `@layout(.c)` means field order, alignment, and padding must match what the target C ABI expects.

This is primarily about data layout. It is separate from function calling convention.

### 3. Make ABI visible in function types

Use `[.c]fun(...) Ret` for C-ABI function types.

```mesa
type CompareFn = [.c]fun(*ffi.c_void, *ffi.c_void) ffi.c_int
```

Meaning:
- ABI is part of the function type itself.
- `fun(...) Ret` and `[.c]fun(...) Ret` are distinct types.
- ABI must remain visible in rendered and resolved types such as `@typeof`.

This keeps callback and function-pointer types explicit without changing generic syntax.

### 4. Tie foreign namespaces to the build system

Foreign namespaces come from the build description:

```mesa
pub fun build(b: *build.Build) void {
    let std = b.addPackage("std", root = "@std")
    let libc = b.linkLibrary("libc", abi = .c)
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry, imports = .{ std, libc })
    b.install(app)
}
```

Meaning:
- `b.linkLibrary("libc", abi = .c)` creates a foreign namespace handle.
- `import libc` makes that namespace available for `@extern(...)` bindings.
- The library's default ABI comes from the link-library declaration unless later syntax adds an override.

## Public Interfaces / Syntax Changes

- Remove `extern` as a declaration keyword.
- Keep `opaque` as a declaration keyword.
- Add `@extern(lib)` and `@extern(lib, name = "...")`.
- Add `@layout(.c)` on struct declarations.
- Add `[.c]fun(...) Ret` as a function-type spelling.
- Add `b.linkLibrary(name, abi = .c)` as the linked foreign namespace declaration in `build.mesa`.

## Test Plan

- Parser tests for:
  - `@extern(lib)` on `fun`
  - `@extern(lib, name = "...")`
  - `@extern(lib)` on `opaque type`
  - `@layout(.c)` on struct declarations
  - `[.c]fun(...) Ret` type aliases
- Type-checking tests for:
  - `[.c]fun(...) Ret` remains distinct from plain `fun(...) Ret`
  - ABI-qualified callback aliases preserve `[.c]fun` in rendered and resolved types
- Negative tests for:
  - invalid `@extern` targets
  - invalid `@layout(.c)` targets
  - invalid ABI qualifiers on non-function types
  - legacy `extern fun` keyword syntax after migration
- One end-to-end FFI-shaped sample using:
  - `opaque type FILE`
  - `@extern(libc)` functions
  - a `[.c]fun` callback alias

## Non-Goals

- Full header parsing or bindgen
- Variadics
- C++ / Objective-C / Win32 ABI design
- A complete raw FFI type matrix beyond what is needed to explain the syntax surface

## Assumptions And Defaults

- This is a syntax-first design step, not the full FFI completion plan.
- Symbol existence remains fundamentally a link-time property unless Mesa later gains declared foreign member inventories.
- Canonical binding style is `@extern(lib)` with the name defaulted from the Mesa declaration.
- `name = "..."` is an escape hatch for symbol mismatches, not the primary form.
- `[.c]fun` is the chosen ABI-qualified function type spelling even though it is novel, because it keeps ABI in the type without changing Mesa's generic syntax.
