"""HTTP-layer tests for aura.server.

The server is exercised through a fake socket, so a full request cycle runs
(RequestHandler.setup/handle/finish, routing, JSON, static files) without
binding a port. That keeps the suite runnable anywhere, including environments
with no networking at all.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aura.server import Handler, Api  # noqa: E402
from aura.store import Library  # noqa: E402


class FakeSocket:
    """Just enough socket for socketserver's StreamRequestHandler."""

    def __init__(self, request: bytes):
        self._in = io.BytesIO(request)
        self.out = io.BytesIO()

    def makefile(self, mode, buffering=-1, **kwargs):
        return self._in if "r" in mode else self.out

    def sendall(self, data):
        self.out.write(data)

    def settimeout(self, value):
        pass

    def setsockopt(self, *args):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass


class StubServer:
    server_name = "aura-test"
    server_port = 0
    server_version = "AURA-test"


def request(handler_cls, method, path, body=b"", headers=None, host="127.0.0.1:8765"):
    lines = ["{} {} HTTP/1.1".format(method, path), "Host: " + host]
    for key, value in (headers or {}).items():
        lines.append("{}: {}".format(key, value))
    if method in ("POST", "PUT", "PATCH") and "Content-Length" not in (headers or {}):
        lines.append("Content-Length: {}".format(len(body)))
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body
    sock = FakeSocket(raw)
    handler_cls(sock, ("127.0.0.1", 54321), StubServer())
    payload = sock.out.getvalue().decode("utf-8", "replace")
    head, _, rest = payload.partition("\r\n\r\n")
    status = int(head.split(" ")[1]) if head.split(" ")[1:2] else 0
    parsed_headers = {}
    for line in head.split("\r\n")[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            parsed_headers[key.strip().lower()] = value.strip()
    return status, parsed_headers, rest


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = Library(root=self.root)
        self.api = Api(self.library)
        self.handler_cls = type("BoundHandler", (Handler,), {"api": self.api})

    def tearDown(self):
        self.temp.cleanup()

    def get(self, path, **kwargs):
        return request(self.handler_cls, "GET", path, **kwargs)

    def post(self, path, payload=None, raw=None, headers=None):
        if raw is None:
            raw = json.dumps(payload or {}).encode("utf-8")
            headers = dict(headers or {}, **{"Content-Type": "application/json"})
        return request(self.handler_cls, "POST", path, body=raw, headers=headers)

    def test_health(self):
        status, _headers, body = self.get("/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stats"]["documents"], 0)
        self.assertIn("backend", payload)

    def test_index_page_is_served(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn("AURA", body)

    def test_assets_are_served_with_a_javascript_type(self):
        status, headers, body = self.get("/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/javascript")
        self.assertIn("AndroidAura", body)

    def test_path_traversal_is_refused(self):
        status, _headers, body = self.get("/../aura/config.py")
        self.assertIn(status, (403, 404))
        self.assertNotIn("DEFAULTS", body)

    def test_unknown_route(self):
        status, _headers, body = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertIn("unknown route", body)

    def test_library_starts_empty(self):
        status, _headers, body = self.get("/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["documents"], [])

    def test_upload_then_ask(self):
        content = (b"Photosynthesis converts light energy into chemical energy.\n\n"
                   b"Chlorophyll absorbs blue and red light in the thylakoid membrane.\n")
        status, _headers, body = self.post("/api/upload?name=lecture.txt", raw=content)
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(self.library.stats()["documents"], 1)

        status, _headers, body = self.post("/api/ask", {"question": "where does chlorophyll absorb light"})
        self.assertEqual(status, 200, body)
        answer = json.loads(body)
        self.assertIn("[S1", answer["text"])
        self.assertTrue(answer["hits"])
        self.assertEqual(answer["hits"][0]["chunk"]["doc_name"], "lecture.txt")

    def test_upload_without_a_name_still_works(self):
        status, _headers, body = self.post("/api/upload", raw=b"Calvin cycle fixes carbon dioxide.\n")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["count"], 1)

    def test_upload_with_an_unreadable_file_reports_an_error_without_dying(self):
        status, _headers, body = self.post("/api/upload?name=blob.bin", raw=b"\x00\x01" * 500)
        self.assertIn(status, (400, 500))
        self.assertIn("error", json.loads(body))
        self.assertTrue(self.get("/health")[0] == 200)

    def test_add_by_path_and_remove(self):
        target = self.root / "notes.md"
        target.write_text("# Title\n\nOsmosis moves water across a membrane.\n", encoding="utf-8")
        status, _headers, body = self.post("/api/add", {"path": str(target)})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.library.stats()["documents"], 1)

        status, _headers, body = self.get("/api/library")
        doc_id = json.loads(body)["documents"][0]["doc_id"]
        status, _headers, body = self.get("/api/document/" + doc_id)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["removed"])
        self.assertEqual(self.library.stats()["documents"], 0)

    def test_settings_round_trip_over_http(self):
        status, _headers, body = self.post("/api/settings", {"top_k": 4, "bogus_key": 1})
        self.assertEqual(status, 200)
        settings = json.loads(body)["settings"]
        self.assertEqual(settings["top_k"], 4)
        self.assertNotIn("bogus_key", settings)
        status, _headers, body = self.get("/api/settings")
        self.assertEqual(json.loads(body)["settings"]["top_k"], 4)

    def test_malformed_json_is_rejected_cleanly(self):
        status, _headers, body = self.post("/api/ask", raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("not valid JSON", body)

    def test_empty_body_is_drained_before_responding(self):
        """A handler must consume the request body, or the client sees a reset."""
        big = b"x" * 20000
        status, _headers, body = self.post("/api/ask", raw=big)
        self.assertIn(status, (400, 500))
        self.assertIn("error", json.loads(body))


if __name__ == "__main__":
    unittest.main(verbosity=2)
