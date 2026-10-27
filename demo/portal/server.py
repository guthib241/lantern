"""Run the City Permit Portal on 127.0.0.1 (standard library only).

    python demo/portal/server.py                    # http://127.0.0.1:8770, layout v1
    python demo/portal/server.py --layout v2        # start in the redesigned layout
    python demo/portal/server.py --attacks          # every page carries the attack pack's planted instructions
"""
from __future__ import annotations

import argparse
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portal import MAX_BODY, Portal  # noqa: E402


def make_handler(portal: Portal):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CityPermitPortal/1.0"

        def _serve(self, method: str) -> None:
            url = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.send_error(413, "Request too large")
                return
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            r = portal.handle(method, url.path, url.query, headers, body)
            self.send_response(r.status)
            for k, v in r.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(r.body)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(r.body)

        def do_GET(self):  # noqa: N802
            self._serve("GET")

        def do_HEAD(self):  # noqa: N802
            self._serve("HEAD")

        def do_POST(self):  # noqa: N802
            self._serve("POST")

        def log_message(self, fmt, *args):  # quiet unless asked
            if getattr(portal, "verbose", False):
                super().log_message(fmt, *args)
    return Handler


def serve(port: int = 8770, layout: str = "v1", attacks: bool = False, verbose: bool = False,
          background: bool = False) -> tuple[ThreadingHTTPServer, Portal]:
    """Start the portal. With background=True it runs on a daemon thread (tests); call server.shutdown()."""
    portal = Portal(layout, "attack" if attacks else "")
    portal.verbose = verbose
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(portal))
    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, portal


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="City Permit Portal (local demo, fake data only)")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--layout", choices=["v1", "v2"], default="v1")
    p.add_argument("--attacks", action="store_true", help="add the attack pack's planted instructions to every page")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)
    httpd, _ = serve(a.port, a.layout, a.attacks, a.verbose)
    print(f"City Permit Portal on http://127.0.0.1:{httpd.server_address[1]}  (layout {a.layout}"
          f"{', attack variant' if a.attacks else ''}; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
