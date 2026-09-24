"""Loopback-only JSON API; the frontend runs as a separate service."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import errno
import os
import socket
from pathlib import Path
import secrets
from urllib.parse import urlsplit, unquote
from .storage import DatabaseInUseError
from .controls import ConflictError
from .service import PanelService, BatchNotFoundError

DEFAULT_FRONTEND_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")
DEFAULT_DATA = Path(__file__).resolve().parents[2] / ".runtime" / "console"


def validate_origin(origin):
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in origin)
    ):
        raise ValueError("Frontend origin must be an HTTP(S) origin without a path")
    parsed.port  # Validate malformed ports before opening any server socket.
    return origin


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can let two servers bind the same address.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(service, port=8765, frontend_origins=None):
    allowed_origins = frozenset(
        validate_origin(o)
        for o in (
            DEFAULT_FRONTEND_ORIGINS if frontend_origins is None else frontend_origins
        )
    )
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data, content_type="application/json; charset=utf-8"):
            body = (
                data
                if isinstance(data, bytes)
                else json.dumps(data, ensure_ascii=False, allow_nan=False).encode(
                    "utf-8"
                )
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
            )
            self.send_header("Vary", "Origin")
            origin = self.headers.get("Origin")
            if origin and self.valid_origin():
                self.send_header("Access-Control-Allow-Origin", origin)
                if self.command == "OPTIONS":
                    self.send_header(
                        "Access-Control-Allow-Methods", "GET, POST, OPTIONS"
                    )
                    self.send_header(
                        "Access-Control-Allow-Headers", "Content-Type, X-Control-Token"
                    )
                    self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_host(self):
            port = self.server.server_address[1]
            return self.headers.get("Host") in (
                f"127.0.0.1:{port}",
                f"localhost:{port}",
            )

        def valid_origin(self):
            origin = self.headers.get("Origin")
            port = self.server.server_address[1]
            return (
                origin is None
                or origin in allowed_origins
                or origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
            )

        def do_OPTIONS(self):
            if not self.valid_host() or not self.valid_origin():
                return self.send(403, {"error": "Frontend origin is not allowed"})
            if not urlsplit(self.path).path.startswith("/api/v1/"):
                return self.send(404, {"error": "Not found"})
            method = self.headers.get("Access-Control-Request-Method")
            headers = {
                h.strip().lower()
                for h in self.headers.get("Access-Control-Request-Headers", "").split(
                    ","
                )
                if h.strip()
            }
            if method not in ("GET", "POST") or headers - {
                "content-type",
                "x-control-token",
            }:
                return self.send(
                    403, {"error": "Preflight method or headers are not allowed"}
                )
            self.send(204, b"")

        def do_GET(self):
            if not self.valid_host() or not self.valid_origin():
                return self.send(
                    403, {"error": "Local host or frontend origin is invalid"}
                )
            path = urlsplit(self.path).path
            try:
                if path == "/api/v1/bootstrap":
                    return self.send(
                        200, {**service.call("bootstrap"), "control_token": token}
                    )
                if path == "/api/v1/state":
                    return self.send(200, service.call("state"))
                if path == "/api/v1/history":
                    return self.send(200, service.call("history"))
                if path == "/api/v1/drivers":
                    return self.send(200, service.call("drivers"))
                self.send(404, {"error": "Not found"})
            except Exception as exc:
                self.send(500, {"error": str(exc)})

        def do_POST(self):
            if (
                not self.valid_host()
                or not self.valid_origin()
                or not secrets.compare_digest(
                    self.headers.get("X-Control-Token", ""), token
                )
            ):
                return self.send(
                    403, {"error": "Local control token or origin is invalid"}
                )
            if (
                self.headers.get("Content-Type", "").split(";")[0].strip()
                != "application/json"
            ):
                return self.send(415, {"error": "JSON request body required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.send(400, {"error": "Invalid content length"})
            if length <= 0 or length > 1024 * 1024:
                return self.send(413, {"error": "Request body must be 1 byte to 1 MiB"})
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Body must be a JSON object")
                parts = [unquote(p) for p in urlsplit(self.path).path.split("/") if p]
                if parts == ["api", "v1", "runs"]:
                    if set(payload) - {"actor", "key", "scenario", "workflow"}:
                        raise ValueError("Unsupported submission fields")
                    return self.send(201, service.call("create", **payload))
                if type(payload.get("expected_version")) is not int:
                    raise ValueError("expected_version is required")
                if (
                    parts[:3] == ["api", "v1", "runs"]
                    and len(parts) == 5
                    and parts[4] in ("pause", "resume", "cancel", "recover")
                ):
                    allowed_fields = {"actor", "expected_version"} | (
                        {"reason"} if parts[4] == "cancel" else set()
                    )
                    if set(payload) - allowed_fields:
                        raise ValueError("Unsupported control fields")
                    return self.send(
                        200, service.call(parts[4], run_id=parts[3], **payload)
                    )
                if (
                    parts[:3] == ["api", "v1", "alarms"]
                    and len(parts) == 5
                    and parts[4] == "acknowledge"
                ):
                    if set(payload) - {"actor", "expected_version"}:
                        raise ValueError("Unsupported acknowledgement fields")
                    return self.send(
                        200,
                        service.call(
                            "acknowledge_alarm", command_id=parts[3], **payload
                        ),
                    )
                if (
                    parts[:3] == ["api", "v1", "recoveries"]
                    and len(parts) == 5
                    and parts[4] == "resolve"
                ):
                    if set(payload) - {
                        "actor",
                        "expected_version",
                        "decision",
                        "reason",
                        "evidence",
                    }:
                        raise ValueError("Unsupported recovery fields")
                    return self.send(
                        200, service.call("resolve", command_id=parts[3], **payload)
                    )
                self.send(404, {"error": "Not found"})
            except BatchNotFoundError as exc:
                self.send(404, {"error": str(exc)})
            except ConflictError as exc:
                self.send(409, {"error": str(exc)})
            except (ValueError, TypeError, UnicodeError) as exc:
                self.send(422, {"error": str(exc)})
            except Exception as exc:
                self.send(500, {"error": str(exc)})

    return LocalHTTPServer(("127.0.0.1", port), Handler)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Local laboratory simulation JSON API")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument(
        "--frontend-origin",
        action="append",
        help="Allowed frontend origin; repeat for multiple origins (default: local port 5173)",
    )
    args = parser.parse_args()
    service = None
    server = None
    try:
        try:
            service = PanelService(args.data)
        except DatabaseInUseError as exc:
            parser.error(
                f"后端未启动：运行数据正由另一个后台占用：{exc.path}\n"
                "请使用已运行的服务，或在原终端按 Ctrl+C 停止后再启动。"
                "独立实例需要同时使用不同的 --data 和 --port。"
            )
        try:
            server = create_server(
                service, args.port, frontend_origins=args.frontend_origin
            )
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES) and getattr(
                exc, "winerror", None
            ) not in (10013, 10048):
                raise
            parser.error(
                f"后端未启动：端口 {args.port} 已被占用或被系统禁止。"
                "请先停止已有服务，或使用 --port 指定其他端口。"
            )
        print(
            f"API: http://127.0.0.1:{server.server_address[1]}/api/v1/state", flush=True
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        if service:
            service.close()


if __name__ == "__main__":
    main()
