#!/usr/bin/env python3
"""Minimal local receiver for TMS webhook alerts (stdlib only, no deps).

Mirrors app/notifications/channels/webhook.py: reads the raw body, verifies the
X-TMS-Signature HMAC over those exact bytes with a constant-time compare (as the
sender's docstring mandates), pretty-prints headers + JSON, returns 200.

This is a DEV/TEST tool. Run it anywhere with a system python3 (no venv needed);
it imports nothing from the app. See README.md for the ngrok / SSH-tunnel flows.

Usage:
    # no signing (WEBHOOK_SIGNING_SECRET unset on the sender)
    python3 webhook_receiver.py

    # with signing: set the SAME secret the sender uses
    WEBHOOK_SIGNING_SECRET=dev-secret python3 webhook_receiver.py --port 8001
"""

import argparse
import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Must match the sender: app/notifications/channels/webhook.py.
SIGNATURE_HEADER = "X-TMS-Signature"
SIGNATURE_PREFIX = "sha256="

SECRET = os.environ.get("WEBHOOK_SIGNING_SECRET", "")


def _verify(raw: bytes, header_value: str | None) -> str:
    """Return a human-readable signature verdict for the received body."""
    if not SECRET:
        return "no secret configured — skipping verification"
    if not header_value:
        return "MISSING signature header (sender has a secret set, or spoofed)"
    if not header_value.startswith(SIGNATURE_PREFIX):
        return f"malformed header (want '{SIGNATURE_PREFIX}<hex>', got {header_value!r})"
    sent = header_value[len(SIGNATURE_PREFIX):]
    expected = hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    # Constant-time compare to avoid a timing oracle (sender docstring mandates this).
    return "OK — signature valid" if hmac.compare_digest(sent, expected) else "INVALID signature"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        print("\n" + "=" * 72)
        print(f"POST {self.path}")
        for k, v in self.headers.items():
            print(f"  {k}: {v}")
        print(f"signature: {_verify(raw, self.headers.get(SIGNATURE_HEADER))}")
        try:
            print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            print(raw.decode("utf-8", "replace"))

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self) -> None:
        # Liveness only: lets you confirm the nginx route reaches the receiver
        # from a browser before any alert fires. Alerts always arrive as POST.
        body = b'{"ok":true,"hint":"POST alerts here"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence the default per-request noise
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Local TMS webhook receiver.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    signing = "ON" if SECRET else "OFF (set WEBHOOK_SIGNING_SECRET to verify)"
    print(f"Listening on http://{args.host}:{args.port}/  |  signature check: {signing}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
