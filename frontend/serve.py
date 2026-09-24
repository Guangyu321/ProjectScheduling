"""Independent local static frontend server; no backend imports or data access."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import errno
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

PUBLIC = Path(__file__).resolve().parent / "public"
DEFAULT_API = "http://127.0.0.1:8765"


def validate_api_base(value):
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(c.isspace() or c in "\\;\"'" for c in value)
    ):
        raise ValueError(
            "API base must be an HTTP(S) origin without credentials or a path"
        )
    parsed.port
    return value


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can let two servers bind the same address.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(port=5173, api_base=DEFAULT_API):
    api_base = validate_api_base(api_base)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type="text/plain; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                f"img-src 'self' data:; connect-src {api_base}; frame-ancestors 'none'; base-uri 'self'",
            )
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_GET(self):
            port = self.server.server_address[1]
            if self.headers.get("Host") not in (
                f"127.0.0.1:{port}",
                f"localhost:{port}",
            ):
                return self.send(403, "Invalid local host")
            path = urlsplit(self.path).path
            if path == "/config.js":
                return self.send(
                    200,
                    "window.LAB_CONFIG = " + json.dumps({"apiBase": api_base}) + ";",
                    "text/javascript; charset=utf-8",
                )
            if path == "/favicon.ico":
                return self.send(204, b"", "image/x-icon")
            files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            if path not in files:
                return self.send(404, "Not found")
            filename, mime = files[path]
            try:
                self.send(200, (PUBLIC / filename).read_bytes(), mime)
            except OSError:
                self.send(500, "Frontend asset unavailable")

        do_HEAD = do_GET

    return LocalHTTPServer(("127.0.0.1", port), Handler)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Standalone LabFlow frontend")
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--api-base", default=DEFAULT_API)
    args = parser.parse_args()
    try:
        server = create_server(args.port, args.api_base)
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, errno.EACCES) and getattr(
            exc, "winerror", None
        ) not in (10013, 10048):
            raise
        parser.error(
            f"前端未启动：端口 {args.port} 已被占用或被系统禁止。"
            "请使用已运行的页面，或在原终端按 Ctrl+C 停止后再启动。"
            "也可使用 --port 指定其他端口，并同步后端 --frontend-origin。"
        )
    print(f"Frontend: http://127.0.0.1:{server.server_address[1]}", flush=True)
    print(f"API: {validate_api_base(args.api_base)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
