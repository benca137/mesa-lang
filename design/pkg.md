# Package-Based Multi-File Compilation With `<pkgname>.pkg`

## Summary

Replace the current single-program flattening model with a package model:

- A `.mesa` file is a compilation unit.
- A package is a set of `.mesa` files that all declare the same `pkg <name>`.
- The build root in `build.mesa` supplies the top-level prefix (for example `sim`), so files under that root use relative package names such as `pkg physics` and become fully qualified packages like `sim.physics`.
- Public API is defined only by a package facade file named `<pkgname>.pkg`.
- Subdirectories under a package are organizational only; they do not create subpackages unless files explicitly declare a different `pkg`.

Chosen defaults:
- Use `pkg`, not `module`.
- Implementation files and `<pkgname>.pkg` both use relative package names.
- `pub` means package-visible; no keyword means file-private.
- `export` exists only inside `<pkgname>.pkg`.
- `opaque` exists only as a modifier on explicitly named type exports in `<pkgname>.pkg`.
- Path strings are allowed only in `<pkgname>.pkg`, package-relative, extensionless, and non-user-facing.
- Standard library packages are imported bare, for example `import mem` and `import io`, even though their internal canonical names live under `std.*`.

## Key Changes

### 1. Define package identity and discovery
- `build.mesa` adds named package roots with `b.addPackage(name, root = "...")`.
- Under one package root, `.mesa` files may declare a relative `pkg` such as `pkg physics` or `pkg math`, or omit `pkg` entirely for target-local non-importable files.
- When a package root name is qualified, for example `sim.physics`, imported names resolve through that fully qualified package identity.
- Group all files with the same fully qualified package into one logical package, regardless of nested directory layout.
- Treat subdirectories inside a package as non-semantic organization only; `world/state.mesa`, `world/query.mesa`, and `types/types.mesa` can all belong to `pkg physics`.

### 2. Make `<pkgname>.pkg` the only external API surface
- Require a package facade file at the canonical package directory, for example `sim/physics/physics.pkg`, for any package that is importable from other packages.
- Remove `export` as a declaration visibility modifier from normal `.mesa` files.
- Keep only two declaration visibilities in implementation files:
  - private: file-local
  - `pub`: visible across the whole package
- Define `<pkgname>.pkg` as a curated export file, not a header file:
  - `from "world/state" export World`
  - `from "world/state" export World as PhysicsWorld`
  - `opaque from "world/state" export Body`
  - `opaque from "world/state" export Body as RigidBody`
  - `export "world/api"` as sugar for “export all `pub` names from that file”
- Restrict path-based export syntax to `<pkgname>.pkg` only.
- Resolve export paths relative to the package root, not the build root, and never expose those strings to package consumers.

### 3. Define package namespace and import resolution
- All `pub` names from implementation files live in one shared package namespace.
- Same-package files do not need to import each other; package-visible names are available automatically after the declaration pass.
- Reject duplicate `pub` names within one package unless a later design explicitly adds same-package namespacing.
- Cross-package imports resolve only through the target package’s checked `<pkgname>.pkg` surface.
- Normal source imports stay semantic and package-based, for example `from math import Vec2` from within `pkg physics` under the same build root.
- `<pkgname>.pkg` exports create the consumer-facing namespace of the package; exporting `World` from `"world/state"` makes the public name `physics.World`, not `physics.world.state.World`.
- `opaque` applies only to named type exports:
  - external code can name the type and pass/store it
  - external code cannot rely on representation, fields, or direct construction unless allowed by exported functions
- Disallow `opaque` on bulk file exports such as `opaque export "world/api"`.

### 4. Replace the frontend with a package graph and separate compilation pipeline
- Add compiler concepts:
  - `BuildRoot`
  - `Package`
  - `PackageInterface`
  - `CompilationUnit`
  - `CompilationSession`
- New pipeline:
  1. Read `build.mesa` and discover named package roots attached to the selected executable target.
  2. Scan files, parse `pkg` declarations, and group files into packages.
  3. Parse package facade files and build package export surfaces.
  4. Run a package-wide declaration pass over all implementation files to assemble the shared `pub` namespace.
  5. Validate package-facade exports against that package-wide namespace and referenced file-local origins.
  6. Build a package dependency graph from package imports.
  7. Type-check each `.mesa` file separately using:
     - file-private scope
     - package-wide `pub` scope
     - imported package interfaces
  8. Compile each file separately and link artifacts per target.
- Remove the current behavior that deep-copies imported declarations into a single root `Program`.

This pipeline is now implemented in the current compiler. The important remaining work is around future incremental caching and JIT/session reuse, not basic package semantics.

### 5. Compile files separately and link by package-aware symbols
- Generate one backend artifact per `.mesa` file.
- Link all artifacts for the target together after semantic checking.
- Introduce stable mangling that includes package identity so identical `pub` names in different packages do not collide.
- Keep package-wide semantic sharing separate from file-level code generation: a file can call another file’s `pub` function in the same package without textual merging.
- Generate one checked interface artifact per package from `<pkgname>.pkg`; imported packages depend on that interface artifact, not on raw implementation files.
- Keep this package interface boundary as the basis for future incremental compilation, REPL, and JIT support.

The current backend emits one generated C unit per Mesa source file, compiles those to objects, and links them into the final executable. Package/type/function names use package-aware C names to avoid collisions.

## Public Interfaces / Syntax Changes

- New package declaration keyword in source files: `pkg <relative-name>`
- New package facade filename: `<pkgname>.pkg`
- `export` is valid only in `<pkgname>.pkg`
- `opaque` is valid only as a modifier on explicitly named type exports in `<pkgname>.pkg`
- Path export forms in `<pkgname>.pkg`:
  - `from "path/to/file" export Name`
  - `from "path/to/file" export Name as Alias`
  - `opaque from "path/to/file" export Name`
  - `export "path/to/file"` for bulk `pub` export
- Build system adds package roots that supply the fully qualified prefix; package declarations remain relative to that root
- Bare std imports such as `import mem` and `import io` are canonical user-facing syntax

## Test Plan

- Syntax and discovery:
  - parse `pkg` in `.mesa` files and `<pkgname>.pkg`
  - derive fully qualified package names from build roots plus relative `pkg`
  - group files with the same derived package across nested directories
  - reject conflicting package declarations and misplaced package facades
- Visibility and exports:
  - private names stay file-local
  - `pub` names are visible across files in the same package without imports
  - duplicate `pub` package names are rejected
  - `<pkgname>.pkg` can export named `pub` declarations from specific files
  - `export "file"` exports all `pub` names from that file
  - `opaque` works only on named type exports and rejects non-type targets
- Imports and interfaces:
  - cross-package imports resolve only through `<pkgname>.pkg`
  - implementation files can use `from math import Vec2` under a shared build root
  - packages without a package-facade file are not importable externally
  - exported aliases produce the expected consumer-visible package names
- Separate compilation:
  - files in one package compile independently and link correctly
  - same package, different file calls resolve without flattening
  - different packages with same symbol names link correctly via mangling
- Incremental/session behavior:
  - body-only changes recompile one file
  - `pub` signature changes invalidate package peers as needed
  - package-facade changes invalidate dependent package interfaces
  - interface artifacts are sufficient for future JIT/session reuse

## Assumptions And Defaults

- Named package roots in `build.mesa` are the only place where top-level public package identities like `sim.physics` are declared.
- Relative package names are used everywhere under that build root, including package facades.
- Directory layout below the package root is non-semantic unless a file explicitly declares a different `pkg`.
- `<pkgname>.pkg` is required only for externally importable packages, not for internal subdirectories that are just organization.
- Intra-package namespacing is intentionally flat in v1; naming conflicts among `pub` declarations are resolved by package authors, not by nested internal namespaces.
