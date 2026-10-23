"""Temporary loopback server for one immutable document; no directory access."""

import hashlib
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class TemporaryFileServer:
    """A tunnel may expose this server; the caller controls that tunnel's lifetime.

    The unguessable path acts as a bearer capability. Do not log or publish it.
    Only explicitly supplied bytes are served, with an expiry and no directory routes.
    """

    def __init__(self, content: bytes, *, content_type: str, ttl_seconds: float = 600):
        if not content or len(content) > 25 * 1024 * 1024:
            raise ValueError("Document must contain between 1 byte and 25 MiB.")
        suffixes = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}
        if content_type not in suffixes or not 0 < ttl_seconds <= 1800:
            raise ValueError("Unsupported document type or lifetime.")
        self.content = bytes(content)
        self.content_type = content_type
        self.ttl_seconds = ttl_seconds
        self.sha256 = hashlib.sha256(self.content).hexdigest()
        self.path = "/" + secrets.token_urlsafe(32) + "/document" + suffixes[content_type]
        self.completed_gets = 0
        self._lock = threading.Lock()
        self._server = None

    def __enter__(self):
        if self._server is not None:
            raise RuntimeError("Server is already running.")
        owner = self
        self.expires_at = time.monotonic() + self.ttl_seconds

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.serve_document(head=True)

            def do_GET(self):
                self.serve_document(head=False)

            def serve_document(self, *, head):
                with owner._lock:
                    valid = secrets.compare_digest(self.path.encode(), owner.path.encode())
                    expired = time.monotonic() >= owner.expires_at
                    content = owner.content
                    content_type = owner.content_type
                if not valid:
                    self.send_error(404)
                    return
                if expired:
                    self.send_error(410)
                    return
                self.connection.settimeout(10)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(content)
                        self.wfile.flush()
                    except (OSError, TimeoutError):
                        return
                    with owner._lock:
                        owner.completed_gets += 1

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}{self.path}"
        self.origin = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def replace_document(self, content, content_type):
        replacement = TemporaryFileServer(content, content_type=content_type, ttl_seconds=self.ttl_seconds)
        with self._lock:
            self.content = replacement.content
            self.content_type = replacement.content_type
            self.sha256 = replacement.sha256
            self.path = replacement.path
            self.expires_at = time.monotonic() + self.ttl_seconds
            self.url = self.origin + self.path

    def revoke(self):
        with self._lock:
            self.expires_at = 0

    def __exit__(self, *args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._server = None
