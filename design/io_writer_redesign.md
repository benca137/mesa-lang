# I/O Writer Redesign

## Summary

Rebuild `std/io` around three layers, modeled on Zig 0.15+:

1. `File` — a thin wrapper around a platform OS handle (POSIX fd or Windows `HANDLE`). The only thing that talks to the kernel.
2. `Sink` — a low-level interface with a single `drain` method that does unbuffered I/O.
3. `Writer` — a concrete struct that owns a caller-provided buffer, wraps any `*any Sink`, and exposes the high-level API (`write`, `writeAll`, `writeByte`, `writeln`, `flush`).

Buffering lives inside `Writer` itself, not in a separate `BufferedWriter`. There is one `Writer` type that every consumer accepts; composition (counting, hashing, tee) is done by writing new `Sink` implementations, never by wrapping `Writer`.

I/O errors propagate as values via `IoError!T` error unions. The libc `printf`/`fprintf` path is removed; the C runtime calls `write(2)`/`read(2)` on POSIX and `WriteFile`/`ReadFile` on Windows directly on the underlying handle.

The global `print` / `println` builtins are preserved and continue to write to **stdout** — Mesa is positioned as a Python/Zig hybrid, and the Python convention (`print` → stdout) is what users from either side will expect. A symmetric `eprint` / `eprintln` pair is added for stderr (Rust precedent). All four are locked, panic on I/O failure, and flush on every call — they're the convenience surface, not the structured surface.

This is a clean break for the structured `io` surface. The current `Stdout`/`Stderr` empty-tag structs and the `*void`-typed file intrinsics (`stdout_write`, `stderr_write`, `file_open`, `file_is_open`, `file_write`, `file_read_all`, `file_flush`, `file_close`) are deleted. The `print` / `println` builtin call sites do not need editing; only the lowering target changes.

## Motivation

Today's `std/io/base.mesa` has three layers fighting:

- A `Writer` interface whose dispatch is real but pointless — every implementation calls a hardcoded intrinsic that ends in `printf`.
- `Stdout {}` / `Stderr {}` are zero-byte tags whose only job is to pick the right method via interface dispatch.
- Eight extern intrinsics, declared in `base.mesa`, with bodies hand-emitted in `ccodegen.py` as libc `printf` / `fopen` / `fwrite` calls.

Consequences:
- Buffering is libc's hidden line-buffer-or-not behavior; Mesa cannot flush, cannot disable it, and cannot set a buffer size.
- `write` returns `void`. Broken pipe, EBADF, ENOSPC are silently dropped.
- The "interface" is not extensible — a counting or capturing writer would require new intrinsics.
- The implementation is split across three files (`base.mesa`, `ccodegen.py`, the runtime preamble), so any change touches all three.

## New design

### File — the OS-handle primitive

```mesa
pub error IoError {
    BrokenPipe,         // EPIPE   / ERROR_BROKEN_PIPE
    BadHandle,          // EBADF   / ERROR_INVALID_HANDLE
    NoSpace,            // ENOSPC  / ERROR_DISK_FULL
    Interrupted,        // EINTR   (POSIX only; retried internally — surfaces only if uncaught)
    PermissionDenied,   // EACCES, EPERM / ERROR_ACCESS_DENIED
    NotFound,           // ENOENT  / ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
    Unknown,            // anything else; preserves the raw OS error code in payload (later)
}

pub struct File {
    handle: i64,        // platform-abstract: POSIX fd in low 32 bits, Windows HANDLE as a pointer-sized int

    pub fun read(self: File, buf: *u8, len: i64) IoError!i64 { ... }
    pub fun write(self: File, buf: *u8, len: i64) IoError!i64 { ... }
    pub fun close(self: File) IoError!void { ... }
}
```

`File` is the only type that holds an OS handle. `read`/`write` map directly to one syscall each (`read(2)`/`write(2)` on POSIX, `ReadFile`/`WriteFile` on Windows) and return the number of bytes transferred (which may be short — callers retry inside `Writer`). The `i64` width covers both a 32-bit POSIX fd and a 64-bit Windows `HANDLE`; the runtime knows which to interpret it as based on the build target. The Mesa-side API is identical on every platform.

### Sink — the dispatch interface

```mesa
pub interface Sink {
    fun drain(self: @this, data: *u8, len: i64) IoError!i64
}

def Sink for File {
    fun drain(self: File, data: *u8, len: i64) IoError!i64 {
        self.write(data, len)
    }
}
```

`Sink.drain` is the single primitive every `Writer` calls when its buffer fills. It is allowed to do a short write; callers loop. The convention `IoError!i64` returning bytes-consumed (always > 0 on success) matches Zig's `Writer.VTable.drain`.

Other `Sink` implementations live in their own files and are added as needed:

- `Allocating` — drains into a `vec[u8]` (replaces an in-memory writer).
- `Discarding` — counts bytes and throws them away.
- `Hashing[H]` — feeds bytes into a hash.

These are not in scope for this change; the design just needs to leave room for them.

### Writer — concrete struct with embedded buffer

```mesa
pub struct Writer {
    sink:   *any Sink,   // borrowed; caller owns lifetime
    buffer: []u8,        // borrowed; caller owns the storage
    end:    i64,         // bytes currently in buffer (0..buffer.len)

    pub fun init(sink: *any Sink, buffer: []u8) Writer {
        .{ sink: sink, buffer: buffer, end: 0 }
    }

    pub fun writeByte(self: *Writer, b: u8) IoError!void { ... }
    pub fun writeAll(self: *Writer, data: []u8) IoError!void { ... }
    pub fun write(self: *Writer, text: str) IoError!void { ... }
    pub fun writeln(self: *Writer, text: str) IoError!void { ... }
    pub fun flush(self: *Writer) IoError!void { ... }
}
```

`Writer` is **not** generic and is **not** an interface. It's one concrete struct. Consumer code takes `*Writer`, never `any SomeInterface`. This is the central simplification — every serializer, formatter, or rendering helper accepts the same type whether it's writing to a file, memory, a hash, or anywhere else.

The buffer's capacity is `buffer.len` — there is no separate `cap` field. This relies on Mesa's `[]u8` slice carrying its length (it does — `TSlice` is a real type).

Buffering rules:
- `writeByte` / `writeAll` / `write` append to `buffer` and only call `sink.drain` when the buffer is full.
- `flush` drains the buffer fully (loops on short drains) and resets `end = 0`.
- A write larger than `buffer.len` flushes first, then forwards the payload to `sink.drain` directly without copying.
- `flush` is the only way bytes hit the kernel. Programs are expected to call it at exit, on important boundaries, and before reading user input.

The `*any Sink` field follows the same idiom as `std/mem/arena.mesa`'s `buffer: *any Buffer` — Mesa already supports interface-pointer fields.

#### Why Sink, not just `File.write`

The `Sink` indirection exists so that **one `Writer` type works for every destination**. Without it, code that wants to serialize a value has to be specialized to `File`, and writing the same data to memory or a hash or a counter requires either parallel implementations or a parallel buffering layer. With it, a renderer is written once:

```mesa
fun renderJson(w: *Writer, value: any Json) IoError!void { ... }
```

…and calling it for any of the following just works, all sharing `Writer`'s buffering, error handling, and formatting:

- `*File`-backed writer → write to disk
- `*Allocating`-backed writer → capture into a `vec[u8]`
- `*Hashing`-backed writer → SHA-256 the rendered bytes
- `*Counting`-backed writer → measure the size without producing output
- `*Tee`-backed writer → write to two sinks simultaneously

Each of those alternative sinks is a small struct with one `def Sink for X { fun drain(...) }` block — 5 to 10 lines, no new intrinsics. The cost on the hot path is one indirect call per buffer-fill (so per ~4 KB, not per byte). The alternative without `Sink` is the C tradition: `printf` for stdout, `fprintf` for files, `sprintf`/`snprintf` for memory, custom code for hashing. None of that composes; all of it duplicates buffering. Adding `Sink` later would mean changing `Writer`'s shape and breaking every consumer, so the cheap version is to put it in now.

### stdout / stderr / stdin

```mesa
pub fun stdout() File { .{ handle: @stdHandle(1) } }
pub fun stderr() File { .{ handle: @stdHandle(2) } }
pub fun stdin()  File { .{ handle: @stdHandle(0) } }
```

`@stdHandle(which: i32) i64` is a compiler intrinsic: on POSIX it returns the corresponding fd directly (0/1/2), on Windows it calls `GetStdHandle(STD_INPUT_HANDLE | STD_OUTPUT_HANDLE | STD_ERROR_HANDLE)` and returns the `HANDLE` cast to `i64`. The result is cached per-process by the runtime so repeated calls are cheap.

To get a `Writer`, the caller supplies a buffer:

```mesa
fun main() void {
    let var buf: [4096]u8 = ...
    let f = stdout()
    let var w = Writer.init(&f, buf[..])
    try w.write("hello, world\n")
    try w.flush()
}
```

### Global convenience builtins

The global `print` / `println` builtins are preserved, and a symmetric `eprint` / `eprintln` pair is added. All four live in `std/io.mesa` and are exposed as global names by the compiler (the same way `print` / `println` are today).

```mesa
pub fun print(text: str) void   { ... }   // stdout, locked, flush each call, panic on error
pub fun println(text: str) void { ... }   // same + trailing \n
pub fun eprint(text: str) void  { ... }   // stderr, locked, flush each call, panic on error
pub fun eprintln(text: str) void{ ... }   // same + trailing \n
```

Semantics:
- `print` / `println` write to stdout. `eprint` / `eprintln` write to stderr. This matches Python (`print`), Rust (`println!` / `eprintln!`), and Go (`fmt.Println` / `fmt.Fprintln(os.Stderr, …)`). Zig is the deliberate outlier here — Mesa is positioned closer to Python on this axis.
- Each call takes a process-wide lock so concurrent prints from multiple threads don't interleave mid-line.
- Each call uses a small stack buffer (e.g. 256 bytes) and flushes before returning. The lock window is short and bytes are visible before the next call.
- All four panic via `@panic` on `IoError`. There is no error path. Callers that need to handle I/O failure construct a `Writer` over `stdout()` / `stderr()` themselves and use the structured surface.

These are the only place an `IoError` is ever swallowed into a panic; everything else must propagate.

The compiler lowers each builtin call to a dedicated runtime intrinsic (`mesa__std__io__locked_print_stdout`, `_println_stdout`, `_print_stderr`, `_println_stderr`) — the runtime owns the lock and the static buffer, so Mesa source has no synchronization concepts.

## Compiler intrinsic surface

Replace the eight existing `mesa__std__io__*` forward declarations with five `@`-prefixed compiler intrinsics, joining `@panic` / `@assert` / `@sizeOf` as compiler-known forms with no source-level declaration:

| Mesa intrinsic                                              | Runtime — POSIX                       | Runtime — Windows                              |
|-------------------------------------------------------------|---------------------------------------|------------------------------------------------|
| `@stdHandle(which: i32) i64`                                | returns `which` (0/1/2) directly      | `GetStdHandle(STD_*)`, cast to `i64`           |
| `@ioWrite(handle: i64, data: []u8) i64`                     | `write(2)`                            | `WriteFile`                                    |
| `@ioRead(handle: i64, buf: []u8) i64`                       | `read(2)`                             | `ReadFile`                                     |
| `@ioOpen(path: str, mode: OpenMode) i64`                    | `open(2)` with translated flags       | `CreateFileW` after UTF-8 → UTF-16 path conv   |
| `@ioClose(handle: i64) i32`                                 | `close(2)`                            | `CloseHandle` (returns BOOL → 0 or `-error`)   |

All non-handle-returning intrinsics use the convention: `>= 0` on success (bytes transferred or 0 for void), `< 0` on failure where the negative value encodes the OS error code. `@ioOpen` returns the handle directly on success and a sentinel negative value on failure — the exact encoding is up to the runtime, but the Mesa-side decoder only needs to distinguish "success" from "failure mapped to which `IoError` variant".

`EINTR` is retried inside the POSIX wrapper and never surfaced. Windows has no analogue.

`OpenMode` is a Mesa enum defined in `std/io.mesa`, not a flag bag, so the same source code compiles cleanly for both platforms:

```mesa
pub enum OpenMode {
    Read,             // POSIX: O_RDONLY                         | Windows: GENERIC_READ, OPEN_EXISTING
    Write,            // POSIX: O_WRONLY | O_CREAT | O_TRUNC     | Windows: GENERIC_WRITE, CREATE_ALWAYS
    Append,           // POSIX: O_WRONLY | O_CREAT | O_APPEND    | Windows: FILE_APPEND_DATA, OPEN_ALWAYS
    ReadWrite,        // POSIX: O_RDWR | O_CREAT | O_TRUNC       | Windows: GENERIC_READ|WRITE, CREATE_ALWAYS
}
```

The runtime maps `OpenMode` to platform-specific flags. Modes beyond these four (exclusive create, no-truncate read/write, etc.) are deferred; add new variants when needed rather than re-exposing POSIX flag bits.

The four locked-print intrinsics (`@locked_print_stdout`, `@locked_println_stdout`, `@locked_print_stderr`, `@locked_println_stderr`) are also compiler-known. They are not user-callable; the compiler emits calls to them only as the lowering targets of the global `print` / `println` / `eprint` / `eprintln` builtins.

The intrinsics' bodies live in `ccodegen.py::emit_runtime_state_source` as `#ifdef _WIN32` / `#else` C blocks. They have no source-level forward declaration in `std/io.mesa`.

## C runtime contract

`emit_runtime_state_source` and `_emit_preamble` in `ccodegen.py`:

- **Remove**: `mesa_stdout_write`, `mesa_stderr_write` (preamble); `mesa__std__io__stdout_write`, `mesa__std__io__stderr_write`, `mesa__std__io__file_open`, `mesa__std__io__file_is_open`, `mesa__std__io__file_write`, `mesa__std__io__file_read_all`, `mesa__std__io__file_flush`, `mesa__std__io__file_close`. Also remove the `mesa_println_*` family of preamble helpers (`mesa_println_str` / `_i64` / `_f64` / `_bool` / `_cstr`).
- **Add — file I/O intrinsics**: `mesa__std__io__std_handle`, `mesa__std__io__io_write`, `mesa__std__io__io_read`, `mesa__std__io__io_open`, `mesa__std__io__io_close`. Each is emitted as a single C function with `#ifdef _WIN32 ... #else ... #endif` for the platform-specific call. macOS and Linux share the POSIX branch.
- **Add — locked print intrinsics**: `mesa__std__io__locked_print_stdout`, `_println_stdout`, `_print_stderr`, `_println_stderr`. Implementation: a single `static` mutex per process (POSIX `pthread_mutex_t`, Windows `CRITICAL_SECTION`), guarded by a `pthread_once_t` / `InitOnceExecuteOnce` initializer. Each call locks, copies the input string into a 256-byte stack buffer (or longer; large strings are written directly without staging), writes to the appropriate fd, optionally writes a `\n`, unlocks. On any short or failed write after one retry, calls `mesa_panic` directly. The lock is *not* shared with user `Writer` instances — those are unlocked by design (callers are expected to own them per-thread).
- **Keep but rewrite**: `mesa_panic` and the four other internal `fprintf(stderr, ...)` sites (allocator OOM message in the preamble, GC errors and stack-overflow message in `runtime/mesa_gc_runtime.c`) become direct `write(2)` / `WriteFile` calls. This is the only thing standing between Mesa and a libc-stdio-free runtime.
- **Includes**: drop `<stdio.h>` from the preamble. Add `<unistd.h>`, `<fcntl.h>`, `<errno.h>`, `<pthread.h>` for the POSIX path; `<windows.h>` (and `<io.h>` if needed for `_get_osfhandle`) under `_WIN32`.

### Synchronization is runtime-only

Mesa source has no mutex / lock concepts. The locked-print intrinsics own a single static mutex inside the runtime; from Mesa's perspective the call is atomic. When Mesa eventually grows a `std.sync.Mutex`, the lock can move up into Mesa source — the intrinsic contract does not change. The `Writer` API is deliberately unsynchronized; callers that share a writer across threads must serialize access externally.

### Path encoding (Windows)

`@ioOpen` receives a Mesa `str`, which is UTF-8. Windows's `CreateFileA` is ANSI-codepage and unsafe; use `CreateFileW` and convert UTF-8 → UTF-16 with `MultiByteToWideChar(CP_UTF8, ...)`. The conversion buffer is allocated on the heap inside the runtime function (paths can be long; stack allocation is a footgun) and freed before return. POSIX uses the bytes directly with `open(2)`.

### Error mapping

Each platform branch needs a small "OS error → `IoError` variant" table:

- POSIX: `errno` constants (`EPIPE`, `EBADF`, `ENOSPC`, `EINTR`, `EACCES`, `EPERM`, `ENOENT`, …) → variants. `EINTR` retries.
- Windows: `GetLastError()` constants (`ERROR_BROKEN_PIPE`, `ERROR_INVALID_HANDLE`, `ERROR_DISK_FULL`, `ERROR_ACCESS_DENIED`, `ERROR_FILE_NOT_FOUND`, `ERROR_PATH_NOT_FOUND`, …) → variants.

Both tables live in the runtime as small `switch` statements. The negative return value the Mesa side sees is the same regardless of platform — the platform mapping is invisible above the intrinsic boundary.

## Files to change

- `std/io.mesa` (new, single file) — replaces `std/io/base.mesa` and `std/io/io.pkg`. Declares `pkg io`. Contains: `IoError`, `OpenMode`, `Sink`, `File`, `Writer`, `stdout`/`stderr`/`stdin`, the four global builtins (`print`, `println`, `eprint`, `eprintln`), and any module-level helpers. Per `design/build.md`, `pkg`-tagged single-file packages do not need a separate `.pkg` facade — every `pub` symbol is part of the public surface.
- `std/io/base.mesa`, `std/io/io.pkg` — **deleted**.
- `src/ccodegen.py` —
  - Replace the I/O intrinsic emission in `emit_runtime_state_source` with the five platform-branched wrappers (`std_handle`, `io_write`, `io_read`, `io_open`, `io_close`) and the four locked-print intrinsics.
  - Rewrite `mesa_panic` and the preamble `fprintf(stderr, ...)` site to use `write(2)` / `WriteFile`.
  - Delete the `mesa_println_*` preamble helpers.
  - Register the five `@`-prefixed intrinsics (`@stdHandle`, `@ioWrite`, `@ioRead`, `@ioOpen`, `@ioClose`) and the four locked-print intrinsics so they lower to the corresponding runtime functions.
  - Update the lowering of the `print` / `println` / `eprint` / `eprintln` global builtins so they call the locked-print intrinsics. Confirm where the global builtins are registered (likely `src/builtins.py` or analogous); add `eprint` / `eprintln` there.
  - Drop `#include <stdio.h>` from the preamble; add the new POSIX/Windows headers per the C runtime contract.
- `runtime/mesa_gc_runtime.c` — replace the three `fprintf(stderr, ...)` calls with direct `write(2)` / `WriteFile` calls behind a `_WIN32` `#ifdef`.

If the build system does not yet auto-discover `std/io.mesa` as the `io` package without a `.pkg` facade, that's a separate fix in the build pipeline (`mesa pkg add` and friends, per `design/build.md`). It should not gate this work — fall back to a single-line `std/io/io.pkg` if needed and remove later.

No changes needed in the type checker or IR; the redesign is contained in the stdlib + runtime + intrinsic registration + builtin lowering in codegen.

## Migration

The `print` / `println` builtins keep their source-level signature *and* destination — they continue to write to stdout. Only the lowering target changes (now goes through the locked-print intrinsic instead of `mesa_println_str`). Call sites are unaffected.

`eprint` / `eprintln` are new additions; they don't replace anything.

The structured `io` surface is a clean break:

- Mesa source files that wrote to `Stdout` / `Stderr` empty-tag values directly must construct a `Writer`. Search: `Stdout\s*\{|Stderr\s*\{|stdout\(\)\.write|stderr\(\)\.write`.
- Mesa source files that opened files via `openRead` / `openWrite` / `openAppend` / `File.readAll` / `File.flush` / `File.close` must use the new `File` API and `OpenMode`. `readAll` becomes a free function in `std/io.mesa` over a `File` (out of scope for this design — track separately).
- The `Writer for File` `def` block is gone; replaced by `Sink for File`.
- Imports change shape: `from io export Stdout, Stderr` becomes `from io export File`. Re-export of `print`/`println` is unnecessary because they're global builtins.

The `mesa.py` driver itself is Python and uses Python's `sys.stdout` / `sys.stderr`, so it's unaffected.

## Tests

Most of the suite below is platform-agnostic — it exercises Mesa-side logic against a test `Sink`, not the kernel. Cross-platform coverage is explicit where it matters.

Platform-agnostic (run on POSIX and Windows CI):

- **Buffering**: write less than `cap` bytes against a counting test `Sink` — assert no `drain` calls have happened.
- **Auto-drain on full buffer**: write `cap + 1` bytes — assert exactly one `drain` call has happened by the time the second byte hits the buffer.
- **Large write bypass**: write a payload bigger than `cap` after partially filling the buffer — assert the buffer is flushed first, then the payload is forwarded directly without staging through `buffer`.
- **Short drain handling**: a test `Sink` that returns half the requested bytes per call — assert `flush` loops until the buffer is empty.
- **Error propagation**: a test `Sink` whose `drain` returns `IoError.BrokenPipe` — assert `Writer.write` / `Writer.flush` return that error and `end` is not advanced past the unflushed bytes.
- **`cap == 0`**: every write must hit `drain` immediately; no internal buffering.

Platform-touching:

- **stdout / stderr / stdin identity (POSIX)**: assert the `handle` field on `stdout()`, `stderr()`, `stdin()` corresponds to fds 1, 2, 0 respectively. On Windows, just assert the handles are non-null and distinct.
- **End-to-end pipe (POSIX)**: write to `stdout()`, redirect to a pipe in the test harness, read back, assert payload integrity. Skip on Windows in v1; add a `CreatePipe`-based variant later.
- **File round-trip**: open a temp file with `OpenMode.Write`, write a payload, close, reopen with `OpenMode.Read`, read the payload back, assert equality. Run on both POSIX and Windows. Use a Windows-safe temp path on Windows (`%TEMP%`).
- **UTF-8 path on Windows**: open a file whose path contains non-ASCII (e.g. `"téxt.tmp"`), write, close, reopen, assert success. This locks in the UTF-8 → UTF-16 conversion in `@ioOpen`.
- **`print` vs `eprint` channel separation**: assert that `print("data")` output appears on stdout (visible to a piped consumer) and `eprint("diag")` output appears on stderr — exercises the lowering split.

Existing tests:

- **Language-suite stdout tests** that assert program output: keep them as-is. `print` / `println` still write to stdout, so the harness comparison continues to work. Only tests that exercised the underlying `*void`-typed file API need rewriting.

Negative tests in `tests/projects/compile_error_suite/`:

- A type error case that confirms `Writer.write` requires `*Writer` (not `Writer` by value), to lock in the API shape.
- A type error case that confirms `eprint` is a global builtin, not in any importable namespace — `from io export eprint` should fail with "no such export".

## Future: std.os

Platform-specific bindings (raw `GetStdHandle`, `SetConsoleOutputCP`, `epoll`, `kqueue`, `IOCP`, Windows-specific `OVERLAPPED` I/O, etc.) belong in a future `std/os/` package, organized as:

```
std/os/posix.mesa
std/os/linux.mesa
std/os/darwin.mesa
std/os/windows.mesa
```

Each file declares `pkg os.<platform>` and exposes thin Mesa bindings to that platform's APIs (some via libc, some via additional intrinsics if needed). `std/io.mesa` does *not* depend on `std/os` — the I/O intrinsics are compiler-level and bypass it.

This is **out of scope for this change**. The case for `std/os` is weak today (the four I/O intrinsics handle 100% of platform-touching needs); it strengthens the moment a second concern lands — `std.time`, `std.process`, console mode manipulation, async I/O. The doc calls it out so future work has a known home and `std/io` does not accidentally accumulate platform-specific surface.

## Assumptions / open questions

- **Supported platforms: Linux, macOS, Windows.** Linux and macOS share the POSIX runtime branch; macOS needs no special handling beyond standard POSIX. Other Unixes (BSDs) are untested but should work via the POSIX branch. Other targets (WASI, freestanding) are deferred — they would need their own runtime branch and likely a different `File` representation.
- **No async.** `drain` is synchronous and may block. Async I/O is out of scope.
- **OS error code payload.** `IoError.Unknown` does not currently carry the raw `errno` / `GetLastError` value. Adding a payload is a follow-up once Mesa's error-set payloads are ergonomic enough; the design intentionally leaves the negative-return convention extensible (the runtime can encode the OS code in the low bits of the negative return without breaking callers that only check for `< 0`).
- **`vec[u8]`-backed Allocating writer** is mentioned but not built in this change. The design must not foreclose it; concretely, that means `Sink` is a real interface and not specialized to `File`.
- **Buffer ownership.** `Writer` borrows its buffer (`*u8 + cap`), it does not own it. Lifetime is the caller's job. This matches Zig and Mesa's existing `*any Buffer` allocator pattern. If Mesa later grows a `[]u8` slice type, the signature should switch to that.
- **Default buffer size.** Examples use 4096; the stdlib does not enforce a default. Programs that want zero buffering can pass `cap = 0` — every write goes straight to `drain`. Behavior at `cap = 0` is exercised in the test suite.
- **Windows `HANDLE` width.** `i64` is wide enough on both 32-bit and 64-bit Windows builds (a `HANDLE` is a `void*`, max 8 bytes). The runtime always casts via `(intptr_t)` to silence MSVC warnings.
- **Windows console UTF-8 output.** Writing UTF-8 bytes to a `HANDLE` returned by `GetStdHandle(STD_OUTPUT_HANDLE)` works correctly when the console code page is set to 65001 (UTF-8) or when output is redirected to a pipe/file. For the console-attached case on legacy code pages, mojibake is possible. This is a known Windows wart; the runtime does not call `SetConsoleOutputCP` on the user's behalf. Document in the user-facing stdlib docs once they exist.
- **Locking granularity for `std.debug.print`.** A single process-wide mutex is correct but coarse. Programs that print from many threads at high rates will contend. This is acceptable given `std.debug.print` is for debugging, not throughput; matches Zig's choice.
