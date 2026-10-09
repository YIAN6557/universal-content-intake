"""A stand-in for an OpenAI-compatible chat API, for CI runs that have no real API key.

  python tests/fixtures/fake_translation_server.py PORT

Every subtitle line comes back as "【译】<original>", which contains Chinese, so
the setup check and the subtitle pipeline treat it as a translation.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if self.headers.get("Authorization") != "Bearer sk-test":
            return self._reply(401, {"error": {"message": "invalid api key"}})
        lines = json.loads(request["messages"][-1]["content"])["lines"]
        answer = {"lines": [{"id": line["id"], "zh": f"【译】{line['text']}"} for line in lines]}
        self._reply(200, {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
