#!/usr/bin/env python3
"""Static file server for the validation bench. Serves static/ and nothing else.

Deliberately not webui.py. The bench must not be able to answer an API call at
all — every mutating route on the panel does something irreversible at the far
end, and "the fixture stub intercepts it" is a second line of defence, not the
first one.

`python -m http.server` would do the job except for throughput: it copies in
16KB chunks through a buffered socket writer and manages about 2MB/s on
loopback here, which turns the bundled 25MB CJK font into an eleven-second
transfer. Fonts referenced from CSS hold the document's load event, and headless
Chrome's --dump-dom waits for it, so every probe run timed out waiting for one
file. Reading each file once and handing the whole thing to the socket is ~30x
faster and is the entire difference between this and the stdlib server.

Usage:  python scripts/bench_server.py <port>
"""
from __future__ import annotations

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


class BenchHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"      # keep-alive; the page pulls a dozen files

    def translate_path(self, path):
        # The shell references its own assets as /static/... because that is
        # where webui.py mounts them. Here static/ IS the root, so those would
        # all 404 — and a 404 on the bundled CJK face is not a cosmetic loss:
        # the Chinese screenshots would silently fall back to a system font and
        # look like a rendering bug that is not in the panel at all.
        if path.startswith("/static/"):
            path = path[len("/static"):]
        return super().translate_path(path)

    def copyfile(self, source, outputfile):
        outputfile.write(source.read())

    def end_headers(self):
        # The panel's own server sends this; the bench sends it too so a run
        # here exercises the same freshness behaviour the browser will see.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass                            # the probe's output is the report


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    handler = partial(BenchHandler, directory=str(STATIC))
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"bench: static/ on http://127.0.0.1:{port}", flush=True)
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
