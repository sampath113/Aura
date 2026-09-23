"""HTTP-layer tests for aura.server.

The server is exercised through a fake socket, so a full request cycle runs
(RequestHandler.setup/handle/finish, routing, JSON, static files) without
binding a port. That keeps the suite runnable anywhere, including environments
with no networking at all.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aura import llama_server, server  # noqa: E402
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


def request(handler_cls, method, path, body=b"", headers=None, host="127.0.0.1:8765",
            client=("127.0.0.1", 54321)):
    lines = ["{} {} HTTP/1.1".format(method, path), "Host: " + host]
    for key, value in (headers or {}).items():
        lines.append("{}: {}".format(key, value))
    if method in ("POST", "PUT", "PATCH") and "Content-Length" not in (headers or {}):
        lines.append("Content-Length: {}".format(len(body)))
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body
    sock = FakeSocket(raw)
    handler_cls(sock, client, StubServer())
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
        llama_server.reset()
        self.temp.cleanup()

    def get(self, path, **kwargs):
        return request(self.handler_cls, "GET", path, **kwargs)

    def post(self, path, payload=None, raw=None, headers=None):
        if raw is None:
            raw = json.dumps(payload or {}).encode("utf-8")
            headers = dict(headers or {}, **{"Content-Type": "application/json"})
        return request(self.handler_cls, "POST", path, body=raw, headers=headers)

    def post_from(self, path, client, payload=None):
        """A POST that arrives from another machine on the network."""
        raw = json.dumps(payload or {}).encode("utf-8")
        return request(self.handler_cls, "POST", path, body=raw,
                       headers={"Content-Type": "application/json"}, client=client)

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

    def test_reingest_rebuilds_the_library_from_disk(self):
        target = self.root / "notes.md"
        target.write_text("# Title\n\nOsmosis moves water across a membrane.\n", encoding="utf-8")
        status, _headers, body = self.post("/api/add", {"path": str(target)})
        self.assertEqual(status, 200, body)
        target.write_text("# Title\n\nRibosomes assemble proteins from amino acids.\n", encoding="utf-8")

        status, _headers, body = self.post("/api/reingest", {})
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["reingested"], 1)
        self.assertEqual(payload["skipped"], [])
        self.assertEqual(self.library.stats()["documents"], 1)
        status, _headers, body = self.post("/api/ask", {"question": "what assembles proteins"})
        self.assertIn("ribosome", json.loads(body)["text"].lower())

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

    # ------------------------------------------------------------------- models
    def wait_for_job(self, job_id, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _status, _headers, body = self.get("/api/jobs/" + job_id)
            job = json.loads(body)["job"]
            if job["done"]:
                return job
            time.sleep(0.05)
        self.fail("the job never finished")

    def test_health_reports_the_local_model_state(self):
        status, _headers, body = self.get("/health")
        self.assertEqual(status, 200)
        model = json.loads(body)["model"]
        self.assertEqual(model["state"], "stopped")
        self.assertFalse(model["engine_installed"])
        self.assertIn("models_dir", model)

    def test_models_endpoint_lists_every_model_aura_can_fetch(self):
        status, _headers, body = self.get("/api/models")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["catalog"]), len(server.catalog.MODELS))
        self.assertEqual(payload["installed"], [])
        self.assertEqual(payload["selected"], "")
        first = payload["catalog"][0]
        for key in ("id", "name", "size", "bytes", "ram_gb", "license", "state", "path", "note"):
            self.assertIn(key, first)
        self.assertTrue(any(model["recommended"] for model in payload["catalog"]))

    def test_an_unknown_model_id_cannot_be_downloaded(self):
        status, _headers, body = self.post("/api/model/download", {"id": "not-a-model"})
        self.assertEqual(status, 400)
        self.assertIn("I do not know a model", body)

    def test_downloading_a_model_runs_as_a_job_and_becomes_the_chosen_one(self):
        name = "tiny-test.gguf"
        payload = b"tiny model bytes"
        entry = {
            "id": "tiny-test", "name": "Tiny Test", "params": "0.1B", "quant": "Q4", "bytes": len(payload),
            "context": 512, "ram_gb": 1, "license": "test", "note": "a test model",
            "recommended": False, "file": name, "repo_url": "",
            "files": [{"name": name, "bytes": len(payload), "url": "https://example.test/tiny.gguf",
                       "sha256": hashlib.sha256(payload).hexdigest()}],
        }

        def fake_download(url, dest, expected_bytes=0, sha256="", on_progress=None,
                          cancelled=None, timeout=60.0, opener=None):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(payload)
            if on_progress:
                on_progress(len(payload), len(payload))
            self.assertEqual(expected_bytes, len(payload))
            self.assertEqual(sha256, hashlib.sha256(payload).hexdigest())
            return Path(dest)

        with mock.patch.object(server.catalog, "entry", return_value=entry), \
                mock.patch.object(server.downloads, "download", fake_download):
            status, _headers, body = self.post("/api/model/download", {"id": "tiny-test"})
            self.assertEqual(status, 200, body)
            job = self.wait_for_job(json.loads(body)["job_id"])

        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["result"]["model"], "tiny-test")
        saved = self.root / "models" / name
        self.assertTrue(saved.exists())
        self.assertEqual(self.library.settings["llm_model_path"], str(saved))
        self.assertIn("engine", job["result"]["engine_detail"])

        status, _headers, body = self.get("/api/models")
        payload_after = json.loads(body)
        self.assertEqual(payload_after["selected"], str(saved))
        self.assertTrue(any(item["name"] == name for item in payload_after["installed"]))

    def test_a_model_on_disk_can_be_chosen_and_removed(self):
        folder = self.root / "models"
        folder.mkdir()
        model = folder / "custom.gguf"
        model.write_bytes(b"x")

        status, _headers, body = self.post("/api/model/select", {"path": str(model)})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["selected"], str(model))
        self.assertEqual(self.library.settings["llm_model_path"], str(model))

        status, _headers, body = self.post("/api/model/select", {"path": ""})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.library.settings["llm_model_path"], "")

        self.post("/api/model/select", {"path": str(model)})
        status, _headers, body = self.post("/api/model/delete", {"path": str(model)})
        self.assertEqual(status, 200, body)
        self.assertFalse(model.exists())
        self.assertEqual(self.library.settings["llm_model_path"], "")

    def test_only_files_inside_the_models_folder_can_be_deleted(self):
        outside = self.root / "notes.md"
        outside.write_text("keep me", encoding="utf-8")
        status, _headers, body = self.post("/api/model/delete", {"path": str(outside)})
        self.assertEqual(status, 400)
        self.assertIn("only files inside", body)
        self.assertTrue(outside.exists())

    def test_a_model_that_is_not_a_gguf_is_refused(self):
        odd = self.root / "model.txt"
        odd.write_text("not a model", encoding="utf-8")
        status, _headers, body = self.post("/api/model/select", {"path": str(odd)})
        self.assertEqual(status, 400)
        self.assertIn(".gguf", body)

    def test_starting_without_a_model_says_so(self):
        status, _headers, body = self.post("/api/model/start", {})
        self.assertEqual(status, 400)
        self.assertIn("no model has been chosen", body)

    def test_stopping_when_nothing_runs_is_harmless(self):
        status, _headers, body = self.post("/api/model/stop", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["engine"]["state"], "stopped")

    def test_job_routes(self):
        status, _headers, body = self.get("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["jobs"], [])

        _status, _headers, body = self.post("/api/model/download", {"id": "nope"})
        status, _headers, body = self.get("/api/jobs/does-not-exist")
        self.assertEqual(status, 400)
        self.assertIn("no job called", body)
        status, _headers, body = self.post("/api/jobs/does-not-exist/cancel", {})
        self.assertEqual(status, 400)
        self.assertIn("no job called", body)

    def test_a_settings_change_does_not_touch_the_model_choice(self):
        status, _headers, body = self.post("/api/settings", {"llm_n_ctx": 8192, "llm_threads": 2})
        self.assertEqual(status, 200)
        settings = json.loads(body)["settings"]
        self.assertEqual(settings["llm_n_ctx"], 8192)
        self.assertEqual(settings["llm_threads"], 2)
        self.assertEqual(settings["llm_model_path"], "")

    # ------------------------------------------------ the app's own window
    def test_health_reports_the_window_and_the_address(self):
        self.api.address = {"url": "http://127.0.0.1:8765", "lan_url": "http://192.168.1.9:8765",
                            "lan_allowed": True}
        self.api.shell = "webview"
        status, _headers, body = self.get("/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["shell"], "webview")
        self.assertEqual(payload["address"]["url"], "http://127.0.0.1:8765")
        self.assertTrue(payload["client"]["local"])

    def test_a_request_from_the_network_is_not_a_local_client(self):
        status, _headers, body = request(self.handler_cls, "GET", "/health",
                                         client=("192.168.1.9", 5000))
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["client"]["local"])

    def test_quitting_stops_the_server(self):
        stopped = []
        self.api.on_quit = lambda: stopped.append(True)
        status, _headers, body = self.post("/api/quit")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["quitting"])
        for _ in range(80):  # the hook fires just after the reply is sent
            if stopped:
                break
            time.sleep(0.05)
        self.assertTrue(stopped)

    def test_another_machine_cannot_stop_aura(self):
        stopped = []
        self.api.on_quit = lambda: stopped.append(True)
        status, _headers, body = self.post_from("/api/quit", ("192.168.1.9", 5000))
        self.assertEqual(status, 403)
        self.assertIn("only this machine", json.loads(body)["error"])
        self.assertEqual(stopped, [])

    def test_open_in_browser_is_handled_by_the_desktop_module(self):
        self.api.address = {"url": "http://127.0.0.1:8765"}
        opened = []
        with mock.patch.object(server.desktop, "open_in_browser",
                               lambda url: opened.append(url) or True):
            status, _headers, body = self.post("/api/open-browser")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["opened"])
        self.assertEqual(opened, ["http://127.0.0.1:8765"])

    def test_another_machine_cannot_open_a_browser_here(self):
        with mock.patch.object(server.desktop, "open_in_browser",
                               lambda url: self.fail("should not open anything")):
            status, _headers, body = self.post_from("/api/open-browser", ("10.0.0.7", 4040))
        self.assertEqual(status, 403)
        self.assertIn("only this machine", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
