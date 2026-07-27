"""A local HTTP origin for the refusal/payload terminals.

Assumption 9 of the pagekind report was that `access` and `payload` — the two
dimensions that decide REFUSAL — had never seen a live response. Every terminal
that says "stop" was therefore untested. This server closes that without
hammering a real host: it reproduces, on localhost, the exact behaviours
observed on www.ubiquitypress.com in July 2026.

Routes model REAL observed behaviour, not invented cases:

  /waf              403 unless Accept-Language is sent  (the observed WAF rule:
                    UA alone -> 403, UA+Accept -> 403, UA+Accept-Language -> 200)
  /forbidden        403 always, tiny body      -> access=auth_required
  /unauthorized     401                        -> access=auth_required
  /ratelimited      429                        -> access=rate_limited
  /missing          404                        -> richness=error
  /octet.pdf        a real PDF served as `binary/octet-stream` (observed: the
                    ubiquitypress chapter file) -> payload must still be pdf
  /proper.pdf       the same bytes as application/pdf
  /viewer           a pdf.js viewer shell whose iframe carries ?file=<pdf>
  /ok               ordinary prose HTML
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: a minimal but genuinely valid PDF (magic bytes + trailer)
PDF_BYTES = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
             b"trailer<</Root 1 0 R>>\n%%EOF\n")

VIEWER_HTML = (
    '<!doctype html><html lang="en"><head><meta charSet="utf-8"/>'
    "<title>[PDF] A Chapter</title></head><body><div id=\"__next\">"
    '<iframe title="Hypothesis" '
    'src="/assets/hypothesis/web/viewer.html?file=/files/chapter-01.pdf"></iframe>'
    "</div>"
    '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>'
    "<script src=\"/_next/static/chunks/main.js\">" + ("x" * 4000) + "</script>"
    "</body></html>"
).encode()

OK_HTML = ("<!doctype html><html><head><title>A Page</title></head><body><h1>A Page</h1>"
           "<p>" + ("Ordinary prose that is comfortably longer than the two hundred "
                    "character threshold so the page classifies as static rather than "
                    "thin, with nothing else remarkable about it at all. ") * 3
           + "</p></body></html>").encode()

TINY_403 = b"<html><head><title>403 Forbidden</title></head><body>Forbidden</body></html>"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # keep the pytest output clean
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                        # noqa: N802
        path = self.path.split("?")[0]
        self.server.seen_headers.append(dict(self.headers))   # type: ignore[attr-defined]

        if path == "/waf":
            # the observed rule: Accept-Language is what the WAF actually wants
            if not self.headers.get("Accept-Language"):
                return self._send(403, TINY_403, "text/html; charset=UTF-8")
            return self._send(200, OK_HTML, "text/html; charset=utf-8")
        if path == "/forbidden":
            return self._send(403, TINY_403, "text/html; charset=UTF-8")
        if path == "/unauthorized":
            return self._send(401, b"<html><body>Sign in</body></html>", "text/html")
        if path == "/ratelimited":
            return self._send(429, b"<html><body>Slow down</body></html>", "text/html",
                              {"Retry-After": "60"})
        if path == "/missing":
            return self._send(404, b"<html><body>Not here</body></html>", "text/html")
        if path == "/octet.pdf":
            return self._send(200, PDF_BYTES, "binary/octet-stream")
        if path == "/proper.pdf":
            return self._send(200, PDF_BYTES, "application/pdf")
        if path == "/files/chapter-01.pdf":
            return self._send(200, PDF_BYTES, "binary/octet-stream")
        if path == "/viewer":
            return self._send(200, VIEWER_HTML, "text/html; charset=utf-8")
        if path == "/ok":
            return self._send(200, OK_HTML, "text/html; charset=utf-8")
        return self._send(404, b"<html><body>Not here</body></html>", "text/html")


class Origin:
    """A localhost origin, started on an ephemeral port."""

    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.seen_headers = []                         # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "Origin":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def url(self, path: str) -> str:
        return self.base + path

    @property
    def seen_headers(self) -> list[dict]:
        return self.server.seen_headers                        # type: ignore[attr-defined]
