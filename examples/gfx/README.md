This example adds a substantial `gfx` package implemented entirely in Mesa.

It is intentionally a software renderer, not a GPU wrapper. That makes it a good
fit for the current compiler while still showing how a future graphics library
could be structured:

- a curated package facade in `gfx.pkg`
- public data types for colors, geometry, canvases, and sprites
- internal helpers shared across multiple package files
- raster operations such as rectangle fill, border drawing, lines, circles, and sprite blits
- a presentation layer that turns the framebuffer into ASCII output
- a file-output backend that writes a real `P3` PPM image
- a live RGBA swapchain writer that alternates between front/back framebuffers
- a tiny browser-driven input path backed by `targets/gfx-input.txt`
- package-level tests that can be run with `mesa test`

Run it from this directory with:

- `python3.14 ../../mesa.py test`
- `python3.14 ../../mesa.py run`

Running `mesa run` now produces all of:

- an ASCII preview in the terminal
- a real image file at `targets/gfx-demo.ppm`
- a live stream state file at `targets/gfx-stream.txt`
- alternating RGBA framebuffers at `targets/gfx-front.rgba` and `targets/gfx-back.rgba`
- a tiny pong-style animation rendered frame by frame
- browser-fed player input at `targets/gfx-input.txt`

To inspect the output in the browser viewer:

- `python3 serve_viewer.py`
- open `http://127.0.0.1:8123/viewer.html`

The viewer can:

- poll the live front/back swapchain while the Mesa demo is running
- capture `W`/`S` and `ArrowUp`/`ArrowDown`, then post that state back to the local server
- reload `targets/gfx-demo.ppm` as a static fallback
- or open any local `.ppm` file through the file picker

Layout notes:

- [`examples/gfx/build.mesa`](/Users/oppenheimer/mesa_MVP/mesa2/examples/gfx/build.mesa) wires the `gfx` package into the executable target.
- [`examples/gfx/src/gfx/gfx.pkg`](/Users/oppenheimer/mesa_MVP/mesa2/examples/gfx/src/gfx/gfx.pkg) is the only public API surface for consumers.
- [`examples/gfx/src/gfx/canvas.mesa`](/Users/oppenheimer/mesa_MVP/mesa2/examples/gfx/src/gfx/canvas.mesa) owns framebuffer setup, sampling, and bookkeeping.
- [`examples/gfx/src/gfx/raster.mesa`](/Users/oppenheimer/mesa_MVP/mesa2/examples/gfx/src/gfx/raster.mesa) contains software drawing primitives.
- [`examples/gfx/src/gfx/sprite.mesa`](/Users/oppenheimer/mesa_MVP/mesa2/examples/gfx/src/gfx/sprite.mesa) shows a reusable asset-style surface layered on top.
- [`examples/gfx/src/gfx/ascii.mesa`](/Users/oppenheimer/mesa_MVP/mesa2/examples/gfx/src/gfx/ascii.mesa) provides a simple presentation backend.
- [`examples/gfx/src/gfx/ppm.mesa`](/Users/oppenheimer/mesa_MVP/mesa2/examples/gfx/src/gfx/ppm.mesa) writes the framebuffer to a PPM file through a small libc FFI bridge.
- [`examples/gfx/src/gfx/stream.mesa`](/Users/oppenheimer/mesa_MVP/mesa2/examples/gfx/src/gfx/stream.mesa) publishes RGBA frames, polls viewer input, and manages the swapchain state files.
- [`examples/gfx/viewer.html`](/Users/oppenheimer/mesa_MVP/mesa2/examples/gfx/viewer.html) is a zero-dependency canvas viewer for the generated PPM file.
- [`examples/gfx/serve_viewer.py`](/Users/oppenheimer/mesa_MVP/mesa2/examples/gfx/serve_viewer.py) serves the example directory and accepts input posts from the viewer.

This is a useful stepping stone toward a future FFI-backed graphics stack:
the high-level API shape can stay mostly the same even when the backend later
changes from software rasterization to Vulkan, OpenGL, Metal, or another API.
