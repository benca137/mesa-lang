# Mesa Alpha FFI ABI Contract

Mesa alpha supports a deliberately small C interop surface.

## Supported

- Foreign namespaces are declared in `build.mesa` with
  `b.linkLibrary("name", abi = .c)`.
- Foreign functions use `@extern(lib)` or
  `@extern(lib, name = "...")`.
- Foreign opaque handles use `@extern(lib) opaque type Name`.
- C-compatible structs use `@layout(.c)`.
- C callback and function pointer types use `[.c]fun(...) Ret`.
- `str` crossing the boundary is Mesa's `(data, len)` string value. Use
  `.data` when calling C APIs that expect C strings, and ensure the callee's
  lifetime/termination expectations are met.
- Pointers are raw C pointers. Mesa does not own or free foreign memory unless
  a Mesa API explicitly says so.
- Returned foreign pointers are treated as borrowed or externally owned unless
  documented by the binding.

## Out Of Scope For Alpha

- Header parsing or bindgen.
- Variadic functions.
- C++, Objective-C, Win32, and non-C ABI families.
- Automatic ownership inference for returned memory.
- Automatic conversion between Mesa slices and foreign buffer protocols.

## Test Expectations

- Positive tests cover opaque types, extern functions, C layout structs,
  callbacks, and linked libraries.
- Negative tests cover invalid legacy `extern fun`, invalid `@layout(...)`,
  wrong ABI-qualified function types, and missing foreign namespaces.
