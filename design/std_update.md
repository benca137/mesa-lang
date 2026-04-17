# Revised Standard Library Plan: Bare Std Imports, No `collections` Package

## Summary

Adopt a user-facing model where standard-library packages are imported without the `std.` prefix:

- `import io`
- `from mem import ArenaAllocator`

Chosen rules:
- bare std imports are the only public source form
- top-level std package names are reserved
- projects cannot define bare packages that collide with std names like `io`, `mem`, or `math`
- std packages are still organized internally as `std.*`
- `build.mesa` should switch from `std.build.Build` to `build.Build`

Keep the compiler surface thin:
- `@...` remains reserved for compiler-known intrinsics
- `std.build` stays host/compiler-backed for bootstrap, but under the source-level name `build`
- real std packages should be Mesa source files attached as an explicit std root, added by default in `mesa init`

## Key Changes

### 1. Standard-library import model
Source-level std imports become bare:
- `import io`
- `from mem import ArenaAllocator`

Compiler behavior:
- bare imports matching reserved std names canonicalize internally to `std.<name>`
- `std.<name>` import syntax is not the public form anymore
- collisions with user packages are prevented by reserving std top-level names

Build-system implication:
- the std source root is still explicit in build configuration
- `mesa init` should attach it by default
- users can remove that root for embedded/minimal builds

### 2. Host build API naming
Change build-script source syntax to:

```mesa
pub fun build(b: *build.Build) void
```

Chosen model:
- `build` is the source-level host namespace
- it remains compiler/host-backed during bootstrap
- it is not imported from the std source root
- it is separate from the real `std.*` source packages

### 3. Builtins vs std packages
Keep global builtins:
- `print`
- `println`

Add global builtins:
- `len(x)`
- `cap(x)`

Chosen semantics:
- `len(x)` / `cap(x)` are builtin sugar
- `.len` / `.cap` remain fully supported
- no `make(...)` builtin yet

Canonical library direction:
- `io.print` / `io.println` become the library surface
- bare `print` / `println` remain builtins, likely implemented as aliases/wrappers over `io`

### 4. Real std packages vs compiler intrinsics
Real Mesa std packages should own:
- `mem`
- `io`
- later `math`, `fs`, `time`, `process`

Compiler-owned surface should stay limited to:
- language semantics
- `@...` intrinsics
- host `build`
- internal parser/lowering helpers

Chosen `@` policy:
- `@` remains compiler-reserved
- it means compiler-known form, not necessarily compile-time-only
- future user-defined compile-time facilities must use another mechanism

Needed compiler intrinsics for a real stdlib:
- meta/layout: `@this`, `@typeof`, `@hasField`, `@sizeOf`, `@alignOf`
- runtime/memory: raw alloc/realloc/free-bytes, memcpy/memmove/memset/memcmp, panic/trap, stdout/stderr write hooks

### 5. Allocator architecture
Redesign `mem` around a layered allocator model.

Chosen model:
- base allocators provide raw bytes
- wrappers/adapters add policy

Base allocators:
- page allocator
- C allocator
- fixed-buffer allocator

Wrappers/adapters:
- debug allocator
- arena allocator
- pool allocator

Important decisions:
- `ArenaAllocator` is a strategy wrapper, not the root memory source
- `DebugAllocator` is also a wrapper, not the base source of memory
- `PoolAllocator[T]` wraps a parent allocator, but may default to fixed-buffer behavior when convenient
- current hardcoded allocator constructor handling should eventually be replaced by real `mem` package definitions over intrinsic/runtime hooks

### 6. Future builtin containers
`set`, `deq`, and `hmap` are future language builtins, not std-only types.

Chosen status:
- they should ultimately be closer to `vec` than to ordinary std container types
- but they are out of scope for the immediate stdlib migration
- stdlib work should not depend on shipping them first

## Public Interfaces And Surface Changes

- Std imports:
  - `import io`
  - `from mem import ArenaAllocator`
- Build scripts:
  - `pub fun build(b: *build.Build) void`
- Builtins:
  - keep `print`, `println`
  - add `len(x)`, `cap(x)`
- Future builtin containers:
  - `set[T]`
  - `deq[T]`
  - `hmap[K, V]`
  These are not part of the first stdlib pass.

## Test Plan

- bare std imports resolve correctly to the std source root
- reserved std names cannot be shadowed by project packages
- `build.Build` works in `build.mesa`
- `print` / `println` still work globally
- `len(x)` / `cap(x)` work while `.len` / `.cap` remain valid
- real `mem` and `io` packages load from Mesa source files
- removing the std build root causes expected missing-import failures
- allocator APIs work through the new layered model without checker/codegen injecting a fake public `std.mem` namespace

## Assumptions And Defaults

- Internal canonical std package identities remain `std.*`, even though source syntax omits the prefix.
- Bare std imports are the only intended public source form.
- `mesa init` should attach the std root automatically.
- `make(...)` is deferred until allocator defaults and container allocation semantics are settled.
- `set`, `deq`, and `hmap` are future builtin containers, but not required to begin the stdlib migration.
