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

A file is deleted only when it is at least `wait` old AND the device has been continuously
online for at least `wait`; reconnecting restarts that clock. Clients are expected to retain
their originals until the cloud copy is confirmed; local deletion here is space management,
not a backup guarantee.

Every upload is keyed by its identity: the sanitized name with any known media extension
removed. A persistent ledger records every finished identity so backed-up-and-deleted files
are not requested again.

The HTTP API, configuration variables and security model are documented in README.md.
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


def _env(name, default, cast=str):
    return cast(os.environ.get(name, default))


DEST_DIR = _env("PHOTO_DEST", "/storage/emulated/0/DCIM/Camera")
LEDGER_PATH = _env("PHOTO_LEDGER", os.path.expanduser("~/photo_ledger.txt"))
TOKEN = _env("PHOTO_TOKEN", "")
PORT = _env("PHOTO_PORT", "8765", int)

UPLOAD_MBPS = _env("PHOTO_UPLOAD_MBPS", "10", float)
MIN_WAIT_SEC = _env("PHOTO_MIN_WAIT_SEC", "180", float)
WAIT_SAFETY = _env("PHOTO_WAIT_SAFETY", "2.5", float)
MAX_WAIT_SEC = _env("PHOTO_MAX_WAIT_SEC", str(12 * 3600), float)

MAX_PENDING_BYTES = _env("PHOTO_MAX_PENDING_MB", "12000", float) * 1e6
CLEAN_EVERY_SEC = _env("PHOTO_CLEAN_EVERY_SEC", "30", float)
FORCE_ONLINE = _env("PHOTO_FORCE_ONLINE", "") == "1"

PLACEHOLDER_TOKEN = "CHANGE-ME"  # the value shipped in start-server.sh
CHUNK = 1024 * 1024
PART_SUFFIX = ".part"
KNOWN_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".dng",
             ".mov", ".mp4", ".m4v"}


def log(msg, *args):
    print(msg % args if args else msg, flush=True)


def unlink_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


# --- ledger ---------------------------------------------------------------

class Ledger:
    """Append-only, fsync'd set of finished identities."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._names = set()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                self._names.update(line.strip() for line in f if line.strip())
        except FileNotFoundError:
            pass

    def __contains__(self, name):
        return name in self._names

    def __len__(self):
        return len(self._names)

    def add(self, name):
        with self._lock:
            if name in self._names:
                return
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(name + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._names.add(name)


# --- names and types ------------------------------------------------------

def clean_name(raw):
    name = os.path.basename(raw.strip())
    name = re.sub(r"[^A-Za-z0-9._\- ]", "_", name)
    return name.strip(". ") or "unnamed"


def strip_media_ext(fname):
    stem, ext = os.path.splitext(fname)
    return stem if ext.lower() in KNOWN_EXT else fname


def identity(raw):
    """The single key used for every ledger and buffer lookup, so that a client name means
    the same thing on every endpoint whether or not it carries a media extension."""
    return strip_media_ext(clean_name(raw))


def sniff_ext(head):
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"GIF8":
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
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
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=20, check=True)
                return
            except (OSError, subprocess.SubprocessError):
                continue
        log("media scan failed for %s", path)
    threading.Thread(target=run, daemon=True).start()


# --- connectivity ---------------------------------------------------------

class Connectivity:
    """Cached connectivity probe that tracks how long the device has been continuously
    online, so the cleaner can require online time rather than wall-clock age."""

    CACHE_SEC = 30
    PROBE_ADDR = ("8.8.8.8", 53)

    def __init__(self, force=False):
        self._force = force
        self._lock = threading.Lock()
        self._checked_at = 0.0
        self._ok = False
        self._since = 0.0

    def _probe(self):
        if self._force:
            return True
        try:
            socket.create_connection(self.PROBE_ADDR, timeout=4).close()
            return True
        except OSError:
            return False

    def is_online(self):
        with self._lock:
            if time.time() - self._checked_at < self.CACHE_SEC:
                return self._ok
        ok = self._probe()
        now = time.time()
        with self._lock:
            self._checked_at = now
            if ok and not self._ok:
                self._since = now
            self._ok = ok
        return ok

    def online_seconds(self):
        """Seconds continuously online, or None while offline."""
        if not self.is_online():
            return None
        with self._lock:
            return time.time() - self._since


# --- buffer accounting ----------------------------------------------------

def partial_dir():
    return os.path.join(DEST_DIR, ".partial")


def _scan(directory, keep):
    """(name, path, stat) for each regular file in `directory` whose name passes `keep`."""
    out = []
    try:
        with os.scandir(directory) as entries:
            for e in entries:
                if not keep(e.name):
                    continue
                try:
                    if e.is_file():
                        out.append((e.name, e.path, e.stat()))
                except OSError:
                    pass
    except OSError:
        pass
    return out


def live_files():
    return _scan(DEST_DIR, lambda n: not n.startswith("."))


def partial_files():
    return _scan(partial_dir(), lambda n: n.endswith(PART_SUFFIX))


def pending_bytes():
    # completed files plus in-flight uploads, so concurrent uploads count against the cap
    return sum(st.st_size for _, _, st in live_files() + partial_files())


def held_names():
    return {strip_media_ext(n) for n, _, _ in live_files()}


def is_known(name, held=None):
    """True if `name` is finished or currently buffered."""
    key = identity(name)
    return key in ledger or key in (held_names() if held is None else held)


def wait_for(size_bytes):
    est_upload = (size_bytes * 8.0) / (UPLOAD_MBPS * 1e6)
    return min(MAX_WAIT_SEC, MIN_WAIT_SEC + est_upload * WAIT_SAFETY)


# --- cleaner --------------------------------------------------------------

def clean_once():
    up = net.online_seconds()
    if up is None:
        return
    now = time.time()
    for fname, path, st in live_files():
        wait = wait_for(st.st_size)
        age = now - st.st_mtime
        if age >= wait and up >= wait:
            # record first: a crash between the two steps must not lose the ledger entry
            ledger.add(strip_media_ext(fname))
            try:
                os.remove(path)
            except OSError:
                continue
            log("freed %s (held %.0f min, wait %.0f min)", fname, age / 60, wait / 60)


def cleaner_loop():
    while True:
        try:
            clean_once()
        except Exception as exc:  # keep the cleaner alive whatever happens
            log("cleaner error: %s", exc)
        time.sleep(CLEAN_EVERY_SEC)


# --- HTTP -----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "pixel-1-sync/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing --

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

    def _route(self):
        return urlparse(self.path).path

    def _dispatch(self, routes):
        if not (TOKEN and hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN)):
            return self._reply(401, "bad token")
        action = routes.get(self._route())
        if action is None:
            return self._reply(404, "not found")
        action()

    def _query_name(self):
        """The sanitized ?name= value, or None after replying with an error."""
        raw = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
        if not raw:
            self._reply(400, "missing name")
            return None
        return clean_name(raw)

    def _read_exact(self, n):
        while n > 0:
            data = self.rfile.read(min(CHUNK, n))
            if not data:
                raise IOError("connection closed mid-body")
            n -= len(data)
            yield data

    def _body_chunks(self):
        if self.headers.get("Transfer-Encoding", "").lower() != "chunked":
            yield from self._read_exact(int(self.headers.get("Content-Length") or 0))
            return
        while True:
            size = int(self.rfile.readline().split(b";")[0].strip(), 16)
            if size == 0:
                while self.rfile.readline().strip():  # skip trailers up to the blank line
                    pass
                return
            yield from self._read_exact(size)
            self.rfile.readline()

    def _drain(self):
        try:
            for _ in self._body_chunks():
                pass
        except (ValueError, IOError):
            self.close_connection = True

    def _read_names(self):
        """The posted newline-separated names, or None after replying with an error."""
        try:
            blob = b"".join(self._body_chunks())
        except (ValueError, IOError):
            self._reply(400, "bad request body")
            return None
        return [n.strip() for n in blob.decode("utf-8", "replace").splitlines() if n.strip()]

    def _reply_names(self, names):
        self._reply(200, "\n".join(names) if names else "NONE")

    # -- verbs --

    def do_GET(self):
        if self._route() == "/ping":
            return self._ping()
        self._dispatch({"/space": self._space, "/exists": self._exists})

    def do_PUT(self):
        self._dispatch({"/missing": self._missing,
                        "/safe_to_delete": self._safe_to_delete,
                        "/upload": self._upload})

    do_POST = do_PUT

    # -- endpoints --

    def _ping(self):
        self._reply(200, "pong (%d done, %d in buffer, %.0f MB, online=%s)"
                    % (len(ledger), len(live_files()), pending_bytes() / 1e6, net.is_online()))

    def _space(self):
        self._reply(200, str(int(max(0, MAX_PENDING_BYTES - pending_bytes()))))

    def _exists(self):
        name = self._query_name()
        if name is not None:
            self._reply(200, "yes" if is_known(name) else "no")

    def _missing(self):
        names = self._read_names()
        if names is None:
            return
        held = held_names()
        missing = [n for n in names if not is_known(n, held)]
        self.log_message("missing: %d asked, %d missing", len(names), len(missing))
        self._reply_names(missing)

    def _safe_to_delete(self):
        names = self._read_names()
        if names is None:
            return
        safe = ["|%s|" % n for n in names if identity(n) in ledger]
        self.log_message("safe_to_delete: %d of %d", len(safe), len(names))
        self._reply_names(safe)

    def _upload(self):
        name = self._query_name()
        if name is None:
            return
        if is_known(name):
            self._drain()
            return self._reply(200, "skipped")

        try:
            incoming = int(self.headers.get("Content-Length") or 0)  # 0 if chunked
        except ValueError:
            return self._reply(400, "bad content-length")
        if incoming > MAX_PENDING_BYTES:
            return self._reply(413, "file larger than the buffer (PHOTO_MAX_PENDING_MB)")
        if pending_bytes() + incoming > MAX_PENDING_BYTES:
            self._drain()
            return self._reply(200, "BUSY")

        # unique temp name so concurrent uploads of the same name cannot collide
        tmp = os.path.join(partial_dir(), "%s.%s%s" % (name, os.urandom(6).hex(), PART_SUFFIX))
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
            unlink_quietly(tmp)
            return self._reply(400, "bad upload: %s" % exc)
        except Exception as exc:
            unlink_quietly(tmp)
            return self._reply(500, "upload failed: %s" % exc)

        if total == 0:
            unlink_quietly(tmp)
            return self._reply(400, "empty upload")

        final_name = name
        if os.path.splitext(name)[1].lower() not in KNOWN_EXT:
            ext = sniff_ext(head)
            if not ext:
                # Google Photos would ignore it, yet the cleaner would later mark it finished
                unlink_quietly(tmp)
                return self._reply(415, "unrecognized media type")
            final_name += ext
        final_path = os.path.join(DEST_DIR, final_name)
        os.replace(tmp, final_path)
        media_scan_async(final_path)
        self.log_message("saved %s (%d bytes), buffer %.0f MB",
                         final_name, total, pending_bytes() / 1e6)
        self._reply(200, "saved")


ledger = Ledger(LEDGER_PATH)
net = Connectivity(force=FORCE_ONLINE)


def prepare_dirs():
    os.makedirs(partial_dir(), exist_ok=True)
    open(os.path.join(partial_dir(), ".nomedia"), "a").close()
    for _, path, _ in partial_files():  # left behind by an interrupted run
        unlink_quietly(path)


def main():
    if not TOKEN or TOKEN == PLACEHOLDER_TOKEN:
        sys.exit("PHOTO_TOKEN must be set to a long random secret (not %r)" % PLACEHOLDER_TOKEN)
    prepare_dirs()
    ledger.load()
    threading.Thread(target=cleaner_loop, daemon=True).start()
    log("dest=%s ledger=%s port=%d up=%.0fMbps min_wait=%.0fs cap=%.0fMB done=%d",
        DEST_DIR, LEDGER_PATH, PORT, UPLOAD_MBPS, MIN_WAIT_SEC, MAX_PENDING_BYTES / 1e6, len(ledger))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
