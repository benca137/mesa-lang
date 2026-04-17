# Mesa-Native Test Projects

This directory holds Mesa-native test projects that are intentionally still
project-scoped instead of being flattened into the top-level `tests/` tree.

Current hand-written suites:
- `language_suite`
- `integration_suite`
- `compile_error_suite`

The generated positive corpus suites have been migrated into the top-level
`tests/` tree as flattened one-file Mesa test projects.

Negative compile-error coverage is also Mesa-native now. Instead of keeping
scattered `*_err.mesa` files under each category, the canonical failing-source
fixtures live under:

- `tests/projects/compile_error_suite/fixtures`

That suite generates Mesa tests that use `@test.compile(...)` and
`@test.compileFile(...)` to assert compile failures and diagnostic details.
