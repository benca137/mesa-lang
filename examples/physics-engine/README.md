This example uses the package and `build.mesa` system implemented in this repository.

It shows:
- a package split across multiple files
- a curated public API via `<pkgname>.pkg`
- `pub` declarations shared across the package
- file-private helpers that stay invisible outside their defining file
- a pkg-less `main.mesa` entry point built through `build.mesa`

Run it from this directory with:
- `python3 ../../src/mesa.py build`
- `python3 ../../src/mesa.py run`

If your shell resolves `python3` to an older interpreter, use a Python 3.10+ binary instead.

Layout notes:
- [`examples/physics-engine/build.mesa`](/Users/oppenheimer/mesa_MVP/mesa2/examples/physics-engine/build.mesa) adds `math` and `physics` as explicit package imports and builds `src/main.mesa`
- [`examples/physics-engine/src/math/math.pkg`](/Users/oppenheimer/mesa_MVP/mesa2/examples/physics-engine/src/math/math.pkg) defines the public `math` package
- [`examples/physics-engine/src/physics/physics.pkg`](/Users/oppenheimer/mesa_MVP/mesa2/examples/physics-engine/src/physics/physics.pkg) defines the public `physics` package
- [`examples/physics-engine/src/main.mesa`](/Users/oppenheimer/mesa_MVP/mesa2/examples/physics-engine/src/main.mesa) is pkg-less and belongs to the executable target's local source set
- `mesa build` writes the binary to `targets/physics-engine` by default

The nested `types/` and `world/` directories under `physics/` are organizational only. All of those `.mesa` files still belong to `pkg physics`.
