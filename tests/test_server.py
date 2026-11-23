"""Tests for pixel_sync_server. Run with:  python -m unittest discover tests"""

import http.client
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pixel_sync_server as srv  # noqa: E402

TOKEN = "test-token"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        srv.DEST_DIR = os.path.join(self.tmp.name, "Camera")
        srv.TOKEN = TOKEN
        srv.MAX_PENDING_BYTES = 1e6
        srv.MIN_WAIT_SEC = 0
        srv.ledger = srv.Ledger(os.path.join(self.tmp.name, "ledger.txt"))
        srv.net = srv.Connectivity(force=True)
        self.scanned = []
        srv.media_scan_async = self.scanned.append
        srv.prepare_dirs()
        srv.Handler.log_message = lambda *a: None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, token=TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        conn.request(method, path, body=body, headers={"X-Token": token} if token else {})
        resp = conn.getresponse()
        result = resp.status, resp.read().decode().strip()
        conn.close()
        return result

    def upload(self, name, data=JPEG):
        return self.request("PUT", "/upload?name=" + name, data)

    def age_buffer(self, seconds):
        """Pretend the buffered files and the online period both started `seconds` ago."""
        srv.net.is_online()
        srv.net._since -= seconds
        for _, path, st in srv.live_files():
            os.utime(path, (st.st_atime - seconds, st.st_mtime - seconds))

    # --- auth and routing ---

    def test_ping_needs_no_token(self):
        status, text = self.request("GET", "/ping", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(text.startswith("pong"))

    def test_auth_checked_before_routing_for_every_verb(self):
        self.assertEqual(self.request("GET", "/nope", token="bad")[0], 401)
        self.assertEqual(self.request("PUT", "/nope", b"", token="bad")[0], 401)
        self.assertEqual(self.request("PUT", "/nope", b"")[0], 404)

    # --- upload ---

    def test_upload_sniffs_extension_and_is_idempotent(self):
        self.assertEqual(self.upload("IMG_1_100"), (200, "saved"))
        self.assertTrue(os.path.exists(os.path.join(srv.DEST_DIR, "IMG_1_100.jpg")))
        self.assertEqual(self.upload("IMG_1_100"), (200, "skipped"))

    def test_name_with_media_extension_is_recognized_everywhere(self):
        self.assertEqual(self.upload("IMG_2.HEIC"), (200, "saved"))
        self.assertEqual(self.request("GET", "/exists?name=IMG_2.HEIC"), (200, "yes"))
        self.assertEqual(self.request("PUT", "/missing", b"IMG_2.HEIC\n"), (200, "NONE"))
        self.age_buffer(60)
        srv.clean_once()
        self.assertIn("IMG_2", srv.ledger)
        self.assertEqual(self.request("PUT", "/safe_to_delete", b"IMG_2.HEIC\n"),
                         (200, "|IMG_2.HEIC|"))
        self.assertEqual(self.upload("IMG_2.HEIC"), (200, "skipped"))

    def test_freeing_a_file_reindexes_its_path(self):
        self.upload("gone")
        path = os.path.join(srv.DEST_DIR, "gone.jpg")
        self.age_buffer(60)
        srv.clean_once()
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.scanned, [path, path])  # once on save, once after deletion

    def test_unrecognized_media_is_rejected(self):
        self.assertEqual(self.upload("notes", b"just some text")[0], 415)
        self.assertEqual(self.request("GET", "/exists?name=notes"), (200, "no"))

    def test_busy_when_upload_would_overrun_buffer(self):
        srv.MAX_PENDING_BYTES = len(JPEG) * 1.5
        self.assertEqual(self.upload("a"), (200, "saved"))
        self.assertEqual(self.upload("b"), (200, "BUSY"))

    def test_file_larger_than_buffer_is_rejected(self):
        srv.MAX_PENDING_BYTES = len(JPEG) - 1
        self.assertEqual(self.upload("big")[0], 413)

    def test_chunked_upload(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        conn.request("PUT", "/upload?name=chunky", body=iter([JPEG[:10], JPEG[10:]]),
                     headers={"X-Token": TOKEN}, encode_chunked=True)
        resp = conn.getresponse()
        self.assertEqual((resp.status, resp.read().strip()), (200, b"saved"))
        conn.close()

    # --- cleaner and ledger ---

    def test_cleaner_waits_for_online_time(self):
        srv.MIN_WAIT_SEC = 3600
        self.upload("hold")
        srv.clean_once()
        self.assertEqual(self.request("PUT", "/missing", b"hold\n"), (200, "NONE"))
        self.assertNotIn("hold", srv.ledger)

    def test_ledger_persists(self):
        srv.ledger.add("x")
        srv.ledger.add("x")
        reloaded = srv.Ledger(srv.ledger.path)
        reloaded.load()
        self.assertIn("x", reloaded)
        with open(srv.ledger.path) as f:
            self.assertEqual(f.read(), "x\n")

    # --- pure helpers ---

    def test_identity(self):
        self.assertEqual(srv.identity("../a/IMG 1.JPG"), "IMG 1")
        self.assertEqual(srv.identity("IMG_1.HEIC_123"), "IMG_1.HEIC_123")
        self.assertEqual(srv.identity("we!rd"), "we_rd")

    def test_wait_for_is_capped(self):
        srv.MAX_WAIT_SEC = 10
        self.assertEqual(srv.wait_for(10 ** 12), 10)


if __name__ == "__main__":
    unittest.main()
