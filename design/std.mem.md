# `std.mem` Foundation

## Summary

`std.mem` is now a real source-backed standard-library package imported as `mem`, and it is built on compiler-known low-level intrinsics rather than compiler-injected public allocator types.

Chosen defaults:
- User-facing imports are bare: `import mem`, `from mem import ArenaAllocator`.
- The public allocator surface is source-defined, not checker-injected.
- Low-level allocation/memory operations are compiler intrinsics used by stdlib implementation code.
- Fallible allocators remain out of scope.

## Key Changes

### 1. Public `mem` package
- `mem` is provided by source files under `std/mem`.
- The current public package includes:
  - `Allocator` interface
  - `Buffer` interface
  - `PageBuffer`
  - `FixedBuffer`
  - `CBuffer`
  - `ArenaAllocator`
- Type-associated constructors are the intended style:
  - `PageBuffer.init(...)`
  - `FixedBuffer.init(...)`
  - `CBuffer.init(...)`
  - `ArenaAllocator.init(...)`
  - `ArenaAllocator.page(...)`
  - `ArenaAllocator.c(...)`
  - `ArenaAllocator.fixed(...)`

### 2. Internal compiler/runtime boundary
- The compiler keeps low-level allocation intrinsics and runtime helpers internal to the implementation boundary.
- Public-facing code is not expected to call raw allocator intrinsics directly.
- The current intrinsic substrate used by `mem` includes:
  - raw layout/meta intrinsics such as `@sizeOf`, `@alignOf`, `@hasField`
  - raw byte operations such as `@memcpy`, `@memmove`, `@memset`, `@memcmp`
  - low-level allocation primitives such as page allocation, C allocation, pointer arithmetic, and fatal panic

### 3. Allocator API
- `Allocator` defines the allocation contract used by runtime-managed containers and explicit allocation flows:
  - `alloc(size, align) -> *void`
  - `realloc(ptr, old_size, new_size, align) -> *void`
  - `free_bytes(ptr, size) -> void`
- `.reset` and `.free` / release remain concrete operations on allocator or buffer types that support them.
- `ArenaAllocator` implements `Allocator` by using an underlying `*any Buffer`.
- `Buffer` is the lower-level policy for obtaining and growing backing memory.

### 4. Backing buffers
- `PageBuffer` obtains backing memory from page-level OS allocation.
- `CBuffer` obtains backing memory from the C allocator.
- `FixedBuffer` wraps an existing fixed memory region and panics on overflow.
- These buffers expose a common `Buffer` interface used by higher-level allocators.

### 5. Arena design
- `ArenaAllocator` is a strategy wrapper over a `Buffer`.
- It tracks used bytes and delegates underlying growth to its buffer.
- Current convenience constructors:
  - `ArenaAllocator.page(capacity)`
  - `ArenaAllocator.c(capacity)`
  - `ArenaAllocator.fixed(base, capacity)`
- This is the current implemented direction for safe allocator layering.
- `DebugAllocator` and `PoolAllocator[T]` are implemented as source-backed wrappers.
- A fuller GC-facing public surface is still future work.

## Public Interfaces And Language Surface

- Import style:
  - `import mem`
  - `from mem import ArenaAllocator, PageBuffer`
- Current primary public names:
  - `mem.Allocator`
  - `mem.Buffer`
  - `mem.PageBuffer`
  - `mem.FixedBuffer`
  - `mem.CBuffer`
  - `mem.ArenaAllocator`
- `Allocator` includes `realloc` in addition to `alloc` and `free_bytes`.
- `expr with alloc` and `esc expr` remain part of the language-level allocation/promotion story.

## Test Plan

- Parser/checker:
  - `mem.ArenaAllocator`, `mem.PageBuffer`, `mem.CBuffer`, `mem.FixedBuffer`, `mem.Allocator`, and `mem.Buffer` resolve normally.
  - `expr with alloc` rejects unsupported/non-cloneable values.
  - `esc` inside local-handle `with` is accepted and routes errors correctly.
- Runtime/codegen:
  - source-defined `ArenaAllocator` methods lower correctly through the interface/codegen pipeline.
  - `ArenaAllocator.init(PageBuffer.init(...))` and related static constructors work.
  - `expr with outer_alloc` deep-copies strings/vecs/structs/unions correctly.
  - `esc` and explicit-target promotion produce equivalent escaped values when targeting the same allocator.
- Compatibility:
  - old hardcoded public allocator scaffolding has been removed rather than preserved as compatibility surface.

## Assumptions And Defaults

- `mem` is the user-facing import name for the standard memory package.
- Runtime support remains emitted through the generated C support layer for now.
- Fallible allocators are postponed; allocator API remains infallible in this pass.
- `ArenaAllocator` over concrete buffers is the currently implemented allocator model.
