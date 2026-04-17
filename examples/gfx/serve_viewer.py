#!/usr/bin/env python3
from __future__ import annotations

import http.server
import socketserver
from pathlib import Path


HOST = "127.0.0.1"
PORT = 8125


def build_handler(root: Path) -> type[http.server.SimpleHTTPRequestHandler]:
    class ViewerHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_POST(self) -> None:
            if self.path != "/input":
                self.send_error(404, "Unknown endpoint")
                return

            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8", errors="ignore")
            root.joinpath("targets", "gfx-input.txt").write_text(payload, encoding="utf-8")

            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    return ViewerHandler


def main() -> None:
    root = Path(__file__).resolve().parent
    root.joinpath("targets").mkdir(exist_ok=True)
    root.joinpath("targets", "gfx-input.txt").write_text("0 0\n", encoding="utf-8")
    handler = build_handler(root)
    with socketserver.TCPServer((HOST, PORT), handler) as server:
        print(f"Serving {root}")
        print(f"Open http://{HOST}:{PORT}/viewer.html")
        print("Use W/S or ArrowUp/ArrowDown in the viewer to move the left paddle.")
        print("Press Ctrl+C to stop.")
        server.serve_forever()


if __name__ == "__main__":
    main()
