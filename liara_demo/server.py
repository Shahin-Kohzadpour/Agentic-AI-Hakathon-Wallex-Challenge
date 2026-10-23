from __future__ import annotations

import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "site"


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        super().end_headers()

    def do_GET(self):
        if self.path.split("?", 1)[0] in ("/health", "/health/"):
            body = json.dumps(
                {
                    "ok": True,
                    "service": "wallex-paper-demo",
                    "mode": "read-only",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def list_directory(self, path):
        self.send_error(403, "Directory listing is disabled")
        return None


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), DemoHandler)
    print(f"Read-only Wallex demo site listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
