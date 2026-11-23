#!/usr/bin/env python3
"""
pixel-1-sync upload server.

A small, root-free HTTP server that runs in Termux on a first-generation Google Pixel
and turns the phone into a relay for unlimited Google Photos backups. A client (e.g. an
iOS Shortcut) uploads photos and videos over a private network; each file lands in the
phone's Camera folder, Google Photos backs it up as a Pixel upload, and a background
cleaner removes the local copy once it has had enough time to upload.

Design
------
The phone holds only a small buffer at a time, so total library size is unbounded; the
only hard requirement is that a single file fits the buffer. When the buffer is full the
server answers "BUSY" and the client retries later.

Backup is not verified. Google Photos' backup state is not readable without root, so the
server deletes a file once it has had enough ONLINE time to have uploaded, scaled to its
size:

    wait = MIN_WAIT_SEC + (file_size_bits / (UPLOAD_MBPS * 1e6)) * WAIT_SAFETY   (<= MAX_WAIT_SEC)

The wait is measured in online time: while the device is offline the clock does not
advance, so a file is never deleted immediately after the device reconnects. Clients are
expected to retain their originals until the cloud copy is confirmed; local deletion here
is space management, not a backup guarantee.

A persistent ledger records every finished name so backed-up-and-deleted files are not
requested again.

Network
-------
The server binds 0.0.0.0 and is intended to be reached over a private overlay network
(e.g. Tailscale). It is therefore also reachable on the local LAN. All mutating endpoints
require a shared secret in the X-Token header; /ping and /exists expose only counts.

Endpoints (all except /ping require header  X-Token: <token>):
    GET  /ping                            health, buffer and ledger counts
    GET  /space                           bytes of buffer headroom remaining
    GET  /exists?name=<name>              "yes" | "no"
    PUT  /missing        (body: names)    names neither finished nor currently held
    PUT  /safe_to_delete (body: names)    finished names as |name| (presumed backed up)
    PUT  /upload?name=<name> (body: file) "saved" | "skipped" | "BUSY"

Configuration (environment variables)
-------------------------------------
    PHOTO_TOKEN        shared secret, required
    PHOTO_DEST         destination folder (default: /storage/emulated/0/DCIM/Camera)
    PHOTO_LEDGER       ledger path (default: ~/photo_ledger.txt)
    PHOTO_PORT         listen port (default: 8765)
    PHOTO_UPLOAD_MBPS  assumed upload speed in Mbps (default: 10)
    PHOTO_MIN_WAIT_SEC minimum hold per file (default: 180)
    PHOTO_WAIT_SAFETY  multiple of estimated upload time (default: 2.5)
    PHOTO_MAX_WAIT_SEC upper bound on the hold (default: 43200)
    MAX_PENDING_MB     buffer cap in MB (default: 12000)
    CLEAN_EVERY_SEC    cleaner interval (default: 30)
    PHOTO_FORCE_ONLINE "1" to skip the connectivity probe (testing)
"""

import hmac
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEST_DIR = os.environ.get("PHOTO_DEST", "/storage/emulated/0/DCIM/Camera")
LEDGER = os.environ.get("PHOTO_LEDGER", os.path.expanduser("~/photo_ledger.txt"))
TOKEN = os.environ.get("PHOTO_TOKEN", "")
PORT = int(os.environ.get("PHOTO_PORT", "8765"))

UPLOAD_MBPS = float(os.environ.get("PHOTO_UPLOAD_MBPS", "10"))
MIN_WAIT_SEC = float(os.environ.get("PHOTO_MIN_WAIT_SEC", "180"))
WAIT_SAFETY = float(os.environ.get("PHOTO_WAIT_SAFETY", "2.5"))
MAX_WAIT_SEC = float(os.environ.get("PHOTO_MAX_WAIT_SEC", str(12 * 3600)))

MAX_PENDING_BYTES = float(os.environ.get("MAX_PENDING_MB", "12000")) * 1e6
CLEAN_EVERY_SEC = int(os.environ.get("CLEAN_EVERY_SEC", "30"))
CHUNK = 1024 * 1024

KNOWN_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".mov", ".mp4", ".m4v", ".dng", ".webp"}

_ledger_lock = threading.Lock()
_seen = set()

_net_lock = threading.Lock()
_net = {"at": 0.0, "ok": False, "since": 0.0}


# --- ledger ---------------------------------------------------------------

def load_ledger():
    if os.path.exists(LEDGER):
        with open(LEDGER, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                name = line.strip()
                if name:
                    _seen.add(name)


def remember(name):
    with _ledger_lock:
        if name in _seen:
            return
        _seen.add(name)
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(name + "\n")
            f.flush()
            os.fsync(f.fileno())


# --- names and types ------------------------------------------------------

def clean_name(raw):
    name = os.path.basename(raw.strip())
    name = re.sub(r"[^A-Za-z0-9._\- ]", "_", name)
    return name.strip(". ") or "unnamed"


def base_of(fname):
    stem, ext = os.path.splitext(fname)
    return stem if ext.lower() in KNOWN_EXT else fname


def sniff_ext(head):
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"GIF8":
        return ".gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return ".heic"
        if brand == b"qt  ":
            return ".mov"
        return ".mp4"
    return ""


# --- media scan (asynchronous; never blocks the request) ------------------

def media_scan_async(path):
    def run():
        for cmd in (["termux-media-scan", path],
                    ["am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                     "-d", "file://" + path]):
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                return
            except Exception:
                continue
    threading.Thread(target=run, daemon=True).start()


# --- connectivity ---------------------------------------------------------

def is_online():
    """Cached connectivity check. Records when the device most recently came online so the
    cleaner can require continuous online time rather than wall-clock age."""
    if os.environ.get("PHOTO_FORCE_ONLINE") == "1":
        with _net_lock:
            if not _net["ok"]:
                _net["ok"] = True
                _net["since"] = time.time()
            return True
    with _net_lock:
        if time.time() - _net["at"] < 30:
            return _net["ok"]
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=4).close()
        ok = True
    except OSError:
        ok = False
    now = time.time()
    with _net_lock:
        _net["at"] = now
        if ok and not _net["ok"]:
            _net["since"] = now
        _net["ok"] = ok
    return ok


def online_since():
    with _net_lock:
        return _net["since"]


# --- buffer accounting ----------------------------------------------------

def partial_dir():
    return os.path.join(DEST_DIR, ".partial")


def live_files():
    out = []
    try:
        for f in os.listdir(DEST_DIR):
            if f.startswith("."):
                continue
            p = os.path.join(DEST_DIR, f)
            if os.path.isfile(p):
                try:
                    st = os.stat(p)
                    out.append((f, p, st.st_size, st.st_mtime))
                except OSError:
                    pass
    except OSError:
        pass
    return out


def partial_bytes():
    total = 0
    try:
        for f in os.listdir(partial_dir()):
            if f.endswith(".part"):
                try:
                    total += os.path.getsize(os.path.join(partial_dir(), f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def pending_bytes():
    # completed files plus in-flight uploads, so concurrent uploads cannot overrun the cap
    return sum(sz for _, _, sz, _ in live_files()) + partial_bytes()


def held_names():
    return {base_of(f) for f, _, _, _ in live_files()}


def wait_for(size_bytes):
    est_upload = (size_bytes * 8.0) / (UPLOAD_MBPS * 1e6)
    return min(MAX_WAIT_SEC, MIN_WAIT_SEC + est_upload * WAIT_SAFETY)


# --- cleaner --------------------------------------------------------------

def remove_stale_partials():
    try:
        for f in os.listdir(partial_dir()):
            if f.endswith(".part"):
                try:
                    os.remove(os.path.join(partial_dir(), f))
                except OSError:
                    pass
    except OSError:
        pass


def cleaner_loop():
    while True:
        try:
            if is_online():
                now = time.time()
                since = online_since()
                for fname, path, sz, mtime in live_files():
                    wait = wait_for(sz)
                    # eligible only once the file is old enough AND the device has been
                    # continuously online that long, so Google Photos had time to upload it
                    if (now - mtime) >= wait and (now - since) >= wait:
                        name = base_of(fname)
                        try:
                            os.remove(path)
                            remember(name)
                            print("freed %s (held %.0f min, wait %.0f min)"
                                  % (fname, (now - mtime) / 60, wait / 60), flush=True)
                        except OSError:
                            pass
        except Exception as exc:
            print("cleaner error:", exc, flush=True)
        time.sleep(CLEAN_EVERY_SEC)


# --- HTTP -----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "pixel-1-sync/1.0"
    protocol_version = "HTTP/1.1"

    def _reply(self, code, text):
        body = (text + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        if code >= 400:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if TOKEN and hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return True
        self._reply(401, "bad token")
        return False

    def _query_name(self):
        raw = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
        if not raw:
            self._reply(400, "missing name")
            return None
        return clean_name(raw)

    def _body_chunks(self):
        enc = self.headers.get("Transfer-Encoding", "").lower()
        if enc == "chunked":
            while True:
                line = self.rfile.readline()
                size = int(line.strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    return
                remaining = size
                while remaining:
                    data = self.rfile.read(min(CHUNK, remaining))
                    if not data:
                        raise IOError("connection closed mid-body")
                    remaining -= len(data)
                    yield data
                self.rfile.readline()
        else:
            remaining = int(self.headers.get("Content-Length", "0"))
            while remaining > 0:
                data = self.rfile.read(min(CHUNK, remaining))
                if not data:
                    raise IOError("connection closed mid-body")
                remaining -= len(data)
                yield data

    def _drain(self):
        try:
            for _ in self._body_chunks():
                pass
        except (ValueError, IOError):
            self.close_connection = True

    def _read_names(self):
        """Return the posted newline-separated names, or None after replying with an error."""
        try:
            blob = b"".join(self._body_chunks())
        except (ValueError, IOError):
            self._reply(400, "bad request body")
            return None
        return [n.strip() for n in blob.decode("utf-8", "replace").splitlines() if n.strip()]

    # -- GET --

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/ping":
            return self._reply(200, "pong (%d done, %d in buffer, %.0f MB, online=%s)"
                               % (len(_seen), len(live_files()), pending_bytes() / 1e6, is_online()))
        if not self._authorized():
            return
        if route == "/space":
            return self._reply(200, str(int(max(0, MAX_PENDING_BYTES - pending_bytes()))))
        if route == "/exists":
            name = self._query_name()
            if name is not None:
                self._reply(200, "yes" if (name in _seen or name in held_names()) else "no")
            return
        self._reply(404, "not found")

    # -- PUT / POST --

    def do_PUT(self):
        route = urlparse(self.path).path
        if route in ("/missing", "/safe_to_delete"):
            if not self._authorized():
                return
            names = self._read_names()
            if names is None:
                return
            if route == "/missing":
                seen, held = _seen, held_names()
                missing = [n for n in names if clean_name(n) not in seen and clean_name(n) not in held]
                self.log_message("missing: %d asked, %d missing", len(names), len(missing))
                return self._reply(200, "\n".join(missing) if missing else "NONE")
            safe = ["|%s|" % n for n in names if clean_name(n) in _seen]
            self.log_message("safe_to_delete: %d of %d", len(safe), len(names))
            return self._reply(200, "\n".join(safe) if safe else "NONE")

        if route != "/upload":
            return self._reply(404, "not found")
        if not self._authorized():
            return
        name = self._query_name()
        if name is None:
            return

        if name in _seen or name in held_names():
            self._drain()
            return self._reply(200, "skipped")

        if pending_bytes() >= MAX_PENDING_BYTES:
            self._drain()
            return self._reply(200, "BUSY")

        os.makedirs(partial_dir(), exist_ok=True)
        open(os.path.join(partial_dir(), ".nomedia"), "a").close()
        # unique temp name so concurrent uploads of the same name cannot collide
        tmp = os.path.join(partial_dir(), "%s.%s.part" % (name, os.urandom(6).hex()))

        head, total = b"", 0
        try:
            with open(tmp, "wb") as f:
                for data in self._body_chunks():
                    if len(head) < 16:
                        head += data[: 16 - len(head)]
                    f.write(data)
                    total += len(data)
                f.flush()
                os.fsync(f.fileno())
        except (ValueError, IOError) as exc:
            self._safe_unlink(tmp)
            return self._reply(400, "bad upload: %s" % exc)
        except Exception as exc:
            self._safe_unlink(tmp)
            return self._reply(500, "upload failed: %s" % exc)

        if total == 0:
            self._safe_unlink(tmp)
            return self._reply(400, "empty upload")

        final_name = name
        if os.path.splitext(name)[1].lower() not in KNOWN_EXT:
            final_name = name + (sniff_ext(head) or "")
        final_path = os.path.join(DEST_DIR, final_name)
        os.replace(tmp, final_path)
        media_scan_async(final_path)
        self.log_message("saved %s (%d bytes), buffer %.0f MB", final_name, total, pending_bytes() / 1e6)
        self._reply(200, "saved")

    do_POST = do_PUT

    @staticmethod
    def _safe_unlink(path):
        try:
            os.remove(path)
        except OSError:
            pass


def main():
    if not TOKEN:
        sys.exit("PHOTO_TOKEN is required (export PHOTO_TOKEN=<a long random string>)")
    os.makedirs(DEST_DIR, exist_ok=True)
    os.makedirs(partial_dir(), exist_ok=True)
    remove_stale_partials()
    load_ledger()
    threading.Thread(target=cleaner_loop, daemon=True).start()
    print("dest=%s ledger=%s port=%d up=%.0fMbps min_wait=%.0fs cap=%.0fMB done=%d"
          % (DEST_DIR, LEDGER, PORT, UPLOAD_MBPS, MIN_WAIT_SEC, MAX_PENDING_BYTES / 1e6, len(_seen)),
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
