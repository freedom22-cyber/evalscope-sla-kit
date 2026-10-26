#!/usr/bin/env python3
"""Mock OpenAI chat-completions server for smoke-testing the kit.

Streams `out_tokens` chunks of fake content at ~1ms intervals, echoes the
Host header it received into a side log so the kit's Host-header switch can
be verified. Usage: python3 mock_server.py [port] [out_tokens]
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
OUT_TOKENS = int(sys.argv[2]) if len(sys.argv) > 2 else 16
SEEN_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_seen_headers.log")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        with open(SEEN_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"Host": self.headers.get("Host"), "path": self.path,
                                "model": body.get("model"), "max_tokens": body.get("max_tokens"),
                                "n_msgs": len(body.get("messages", []))}) + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(payload):
            data = ("data: " + json.dumps(payload) + "\n\n").encode()
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")

        for i in range(OUT_TOKENS):
            payload = {
                "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "model": body.get("model", "mock"),
                "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}],
            }
            chunk(payload)
            self.wfile.flush()
            time.sleep(0.001)
        payload = {
            "id": "chatcmpl-mock", "object": "chat.completion.chunk",
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": OUT_TOKENS, "total_tokens": 8 + OUT_TOKENS},
        }
        chunk(payload)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    print(f"mock server on :{PORT}, out_tokens={OUT_TOKENS}, header log: {SEEN_LOG}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
