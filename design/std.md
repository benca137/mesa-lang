# Standard Library Plan: Mesa-First, Thin Compiler Surface, Layered Allocators

## Summary

Implement the standard library primarily as real Mesa source packages, not compiler-hosted fake namespaces.

Chosen architecture:
- `mem` and `io` should be real Mesa source packages.
- `@name` stays reserved for compiler-known intrinsics only.
- `build` stays host/compiler-backed during bootstrap.
- `print` and `println` remain global builtins, with `io` as the eventual library home.
- Add `len(x)` and `cap(x)` as builtins while keeping `.len` / `.cap`.
- Do **not** add `make(...)` yet.
- Redesign allocators around a layered model: base allocators provide bytes, wrappers/adapters add strategy and instrumentation.

Chosen std discovery model:
- Standard library sources are attached as an **explicit** build root.
- `mesa init` should include that std root by default in generated `build.mesa`.
- Removing std from a project/build should be possible, which keeps embedded/minimal-toolchain use cases viable.

## Key Changes

### 1. Split compiler surface from real std packages
Keep in the compiler:
- core language semantics
- parser/compiler-only synthetic helpers
- compiler-known `@...` intrinsics
- bootstrap `std.build`

Move to Mesa source packages:
- `mem`
- `io`
- later `math`, `fs`, `process`, `time`

Rule:
- if something can be expressed in Mesa source on top of a small intrinsic/runtime boundary, it belongs in `std.*`, not in checker/codegen special cases.

### 2. Define what `@...` means
`@name` remains “compiler-known form”, not “user-defined function syntax”.

Chosen policy:
- `@` names are reserved to compiler intrinsics.
- Future user-defined compile-time facilities must use a separate mechanism, not raw user-defined `@foo`.
- Do not expand the macro/comptime surface in this pass beyond what stdlib implementation needs.
- Do not commit to replacing the existing `comptime` keyword in this pass; that is a separate metaprogramming design pass.

Compiler-known intrinsics needed for a real stdlib:
- semantic/meta: `@this`, `@typeof`, `@hasField`, `@sizeOf`, `@alignOf`
- low-level/runtime: raw allocation, reallocation, free-bytes, memory copy/set/compare, fatal trap/panic, stdout/stderr write hooks

Keep parser-generated internal helpers like `__orelse`, `__try`, `__catch`, `__optional_chain`, `__format` internal only.

### 3. Builtins vs std APIs
Keep these global builtins:
- `print`
- `println`

Add these global builtins:
- `len(x)`
- `cap(x)`

Chosen semantics for `len` / `cap`:
- they are builtin sugar
- `.len` / `.cap` remain fully supported
- no shift away from structural field access yet

Do **not** add yet:
- `make(...)`

Reason:
- `make` becomes ambiguous until allocator defaults and container allocation semantics are fully settled.
- constructors should remain the way to build normal user-defined values.
- if `make` lands later, it should likely be limited to std runtime-managed containers, not arbitrary structs.

Canonical library layering:
- `io.print` / `io.println` become the canonical library names
- bare `print` / `println` remain as compatibility-friendly builtins

### 4. Redesign `mem` around layered allocators
Move away from treating `ArenaAllocator`, `PoolAllocator`, and `DebugAllocator` as unrelated hardcoded runtime kinds.

Chosen model:
- base allocators provide raw memory
- wrappers/adapters layer policy on top

Base allocators:
- `page_allocator`
- `c_allocator`
- `fixed_buffer_allocator`

Wrappers/adapters:
- `debug_allocator(inner)`
- `arena_allocator(inner)`
- `pool_allocator[T](inner, ...)`

Important decisions:
- `DebugAllocator` is not the base allocation source; it wraps another allocator and adds instrumentation.
- `ArenaAllocator` is a strategy layer, not the fundamental source of bytes.
- `PoolAllocator[T]` should also be a wrapper over a parent allocator.
- current single-constructor allocator forms can remain as compatibility shims while `std.mem` is migrated.

Public `mem` surface should be Mesa-defined:
- `Allocator`
- `ArenaAllocator`
- `PoolAllocator[T]`
- `DebugAllocator`
- `gc`
- base allocator values/types exposed in Mesa form

Compiler/runtime responsibility:
- provide the minimum intrinsic hooks and runtime ABI needed for `mem` to implement these in Mesa source
- phase out direct compiler-hosted public namespace injection once the source-backed package exists

### 5. Build-system integration for std
Add an explicit std-root concept to the build system.

Chosen behavior:
- std source packages are not magical implicit imports
- generated `build.mesa` from `mesa init` should include std by default
- projects can remove or replace that std root intentionally
- `build` remains host-provided and separate from the source-backed std root for now

## Builtins/Intrinsics We Are Missing

Missing global builtins:
- `len(x)`
- `cap(x)`

Keep existing global builtins:
- `print`
- `println`

Missing compiler intrinsics needed for real std packages:
- `@hasField`
- `@sizeOf`
- `@alignOf`
- raw memory intrinsics for copy/move/set/compare
- raw allocator intrinsics for alloc/realloc/free-bytes
- fatal trap/panic intrinsic
- stdout/stderr write intrinsics

Not chosen for this pass:
- `make(...)`
- user-defined `@name`
- a new full comptime/macro surface
- replacing `.len` / `.cap` with builtin-only access

## Test Plan

- `mem`, `io`, and later std packages resolve from real Mesa package files.
- `print` / `println` still work globally and match `io.print` / `io.println`.
- `len(x)` / `cap(x)` work on the same shapes that currently expose `.len` / `.cap`.
- `.len` / `.cap` remain valid and unchanged.
- `build.Build` still works in `build.mesa` after std source packages are introduced.
- `ArenaAllocator`, `PoolAllocator[T]`, and `DebugAllocator` work through the new layered model without public checker/codegen name injection.
- removing the std build root produces expected missing-import behavior instead of silently injecting std.
- internal compiler helpers remain unimportable and undocumented.

## Assumptions And Defaults

- Standard library source should ship with the Mesa toolchain, but be attached explicitly in build configuration.
- `mesa init` should wire std in by default.
- `@` remains compiler-owned syntax.
- `len` / `cap` become builtins now, but `make` is deferred.
- There is intentionally no `collections` std package; future `set`, `deq`, and `hmap` are builtin-container work.
