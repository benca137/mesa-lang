# Pure-Mesa `build.mesa`

## Summary

`build.mesa` is a special pure-Mesa file with one canonical entrypoint:

```mesa
pub fun build(b: *build.Build) void {
    let std = b.addPackage("std", root = "@std")
    let physics = b.addPackage("physics", root = "src/physics")
    let math = b.addPackage("math", root = "src/math")
    let entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("app", entry = entry, imports = .{ std, physics, math })
    b.install(app)
}
```

Chosen defaults:
- `build.mesa` has no `pkg` header.
- Build API lives in `build`.
- `Build.addPackage(name, root = "...")` creates named package roots.
- `Build.createEntry("...")` creates an entry handle.
- `Build.addExecutable(...)` accepts `imports = .{ ... }`.
- `mesa build` / `mesa run` only use `./build.mesa` from the current working directory.
- If `./build.mesa` is missing, `mesa build` / `mesa run` error and point to `mesa init`.
- `mesa init` creates `build.mesa`, `src/`, and `src/main.mesa`.
- `pkg` is optional in normal source files. If omitted, the file is not part of any package.
- Pkg-less files under the executable entry subtree participate as target-local sources only and are never importable.
- `mesa test` uses the same `./build.mesa` lookup and runs all `test "..." { ... }` blocks reachable from the default target entry tree.

## Key Changes

### 1. Build API and script interpretation
- Add compiler-provided `build.Build`, `build.Package`, and `build.Executable`.
- Current supported calls:
  - `Build.addPackage(name: str, root = str) -> Package`
  - `Build.createEntry(path: str) -> Entry`
  - `Build.addExecutable(name: str, entry = Entry, imports = .{ pkg1, pkg2, ... }) -> Executable`
  - `Build.install(exe: Executable) -> void`
- Parse `build.mesa` with the normal Mesa parser, then interpret only the restricted build subset needed for:
  - `let` bindings
  - string literals
  - anonymous struct literals
  - method/function calls on build handles
- Keep this pure Mesa syntax, but do not support arbitrary Mesa execution in v1.

### 2. Source discovery and package semantics
- `pkg` becomes optional in all `.mesa` files.
- If a file has `pkg X`, it belongs to named package `X` and is resolved through attached package roots.
- If a file has no `pkg`, it is not part of any package.
- For an executable target, all pkg-less `.mesa` files under the entry file’s directory subtree are compiled into one anonymous target-local source set.
- That anonymous source set is never importable through `import` / `from`.
- Package discovery ignores pkg-less files; package roots only collect files with explicit `pkg`.

### 3. CLI behavior
- Add:
  - `mesa init`
  - `mesa build`
  - `mesa run`
  - `mesa test`
  - `mesa pkg add <source> [--name <prefix>]`
- `mesa build` and `mesa run`:
  - only look for `./build.mesa`
  - error if missing
  - use the default executable target
- `mesa test`:
  - only looks for `./build.mesa`
  - errors if missing
  - uses the default executable target
  - builds and runs a generated test binary
- `mesa init`:
  - creates `./build.mesa`
  - creates `./src/main.mesa`
  - creates a minimal executable target plus one bundled std root
- `mesa pkg add`:
  - only edits `./build.mesa`
  - errors if `./build.mesa` is missing and tells the user to run `mesa init`
  - creates the source directory if missing
  - creates a stub `<pkgname>.pkg` when appropriate
  - adds `b.addPackage(...)` to `build.mesa`
  - adds the new package handle to the default executable target’s `imports = .{ ... }`
  - avoids duplicates

### 4. Default generated `build.mesa`

`mesa init` currently generates this shape:

```mesa
pub fun build(b: *build.Build) void {
    let std = b.addPackage("std", root = "@std")
    let app_entry = b.createEntry("src/main.mesa")
    let app = b.addExecutable("your-project-name", entry = app_entry, imports = .{ std })
    b.install(app)
}
```

### 5. Build file editing policy
- Treat compiler-generated `build.mesa` as canonical and structurally editable.
- `mesa init` should generate a stable shape that later CLI tools can patch safely.
- `mesa pkg add` should update that canonical shape directly.
- If `build.mesa` has been customized beyond safe structural editing, fail with a clear error instead of guessing.

## Test Plan

- `mesa init` creates runnable `build.mesa` and `src/main.mesa`.
- `mesa build`, `mesa run`, and `mesa test` fail clearly when `./build.mesa` is missing.
- `mesa build`, `mesa run`, and `mesa test` use only the current directory’s `build.mesa`, not nearest-parent search.
- `Build.addPackage("physics", root = "src/physics")` and `Build.addPackage("sim.physics", root = "src/physics")` both resolve packages correctly.
- Pkg-less files under the entry subtree compile together and can reference each other, but cannot be imported as packages.
- Files with explicit `pkg` continue to resolve through attached package roots only.
- `mesa pkg add src`
  - updates `build.mesa`
  - creates `src` if missing
  - attaches the new package root to the default executable
- `mesa pkg add src/sim --name sim` writes the named-root form correctly.
- `mesa test` runs `test "..." { ... }` blocks and prints a readable pass/fail summary.
- Re-running `mesa pkg add` is idempotent.
- Existing package tests remain green after making `pkg` optional.

## Assumptions And Defaults

- `build.mesa` is special and never needs `pkg`.
- `mesa init` uses the current directory name as the executable name by default.
- `src/main.mesa` generated by `mesa init` is pkg-less.
- The default executable target is required for `mesa build`, `mesa run`, `mesa test`, and `mesa pkg add`.
- V1 supports executable targets only.
