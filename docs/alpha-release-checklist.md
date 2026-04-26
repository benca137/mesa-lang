# Mesa Alpha Release Checklist

Run these gates before calling a branch alpha-ready:

```sh
python -m pytest tests/python
python -m pytest tests/python/test_mesa_projects.py
```

The first command covers build-system, package, stdlib, FFI, emitted-C, editor
metadata, and external-MesaLSP policy tests. The second command runs all
Mesa-native project suites.

## Required Green Areas

- Python tests pass without unexpected failures.
- Mesa-native project suites pass.
- `std.io`, `std.mem`, and `std.ffi` smoke tests pass.
- The build system can initialize, build, run, test, add packages, use
  package-style library targets, and link C libraries.
- `--emit-c` regression tests confirm linked runtime support and representative
  alpha language constructs.
- A C interop sample using the alpha FFI syntax compiles.

## Known Alpha Blockers

- GitHub issues #1 and #2: `esc` / `with` semantics.
- GitHub issues #3, #4, and #5: MIR branch merge policy and coverage.
- GitHub issue #7: `.pkg` subpackage namespace behavior.

## Non-Blocking Cleanup

- GitHub issue #8: function-test organization.
- MesaLSP implementation work, which belongs in the MesaLSP repository unless
  this repository re-adopts an in-tree language server.
