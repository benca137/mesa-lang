# Mesa Alpha Stdlib Floor

The alpha stdlib is intentionally small.

## Included Packages

- `io`: text writers, stdout/stderr, basic file open/read/write/append/flush/close.
- `mem`: allocator interfaces, page/fixed/C buffers, arena, pool, and debug allocators.
- `ffi`: C ABI aliases used by foreign bindings.

## Formatting

There is no alpha `fmt` package. String interpolation is the formatting surface
for alpha.

## Deferred

- Rich string/text algorithms.
- Full vec/slice algorithms.
- Broad math helpers beyond compiler/language basics.
- Full filesystem/path layer.
- Process, time, random, serialization, networking, and OS APIs.

Any deferred package can be added after alpha without changing the alpha
contract.
