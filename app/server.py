#!/usr/bin/env python3
"""Minimal OPDS server. Standard library only.

Usage:
    python3 server.py run [config.yaml]      # foreground (debugging)
    python3 server.py start|stop|restart     # daemon with a PID file
    python3 server.py scan                   # one-off incremental scan
    python3 server.py rescan                 # re-read every file, ignoring mtimes
    python3 server.py passwd                 # generate a hash for config.yaml
"""
import sys

if sys.version_info < (3, 8):  # Synology also ships Python 2: fail clearly, not with a SyntaxError
    raise SystemExit("Python 3.8+ required. Found: %s\nUse python3, not python."
                     % sys.version.split()[0])

import base64
import hmac
import html
import http.server
import logging
import logging.handlers
import os
import signal
import socket
import threading
import time
from functools import lru_cache
from hashlib import pbkdf2_hmac
from urllib.parse import unquote, urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Optional extras installed beside the application rather than into a user
# site-packages, e.g.
#     python3 -m pip install --target <install>/lib rarfile
# They then survive DSM package updates and work whichever user runs the
# server, which matters because Task Scheduler starts it as root.
_LIB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if os.path.isdir(_LIB):
    sys.path.insert(0, _LIB)

import library
import opds
from library import Library

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(APP_DIR)
PBKDF2_ITER = 120_000
CHUNK = 64 * 1024

log = logging.getLogger("server")


# ------------------------------------------------------------------ config

DEFAULTS = {
    "server": {"host": "0.0.0.0", "port": 2202, "base_url": ""},
    "library": {"path": "/volume1/Contents/Comics"},
    "database": {"path": os.path.join(BASE_DIR, "data", "library.db")},
    "cache": {"path": os.path.join(BASE_DIR, "cache")},
    "security": {"enabled": True, "username": "", "password_hash": ""},
    "scan": {"on_startup": True, "interval_minutes": 60},
    "logging": {"path": os.path.join(BASE_DIR, "logs", "server.log"),
                "level": "INFO", "max_bytes": 2_000_000, "backups": 3},
}


def _coerce(v):
    v = v.strip()
    if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~", ""):
        return None
    try:
        return int(v)
    except ValueError:
        return v


def load_config(path):
    """Read the YAML subset used by config.yaml: two levels of 'key: value'.

    ponytail: a minimal parser instead of PyYAML, to stay dependency-free on
    DSM 7 / ARM64. No lists, anchors or multi-line strings: if those are ever
    needed, switch to PyYAML.
    """
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if path and os.path.exists(path):
        section = None
        with open(path, encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.split("#", 1)[0].rstrip()
                if not line.strip():
                    continue
                indent = len(line) - len(line.lstrip())
                if ":" not in line:
                    raise ValueError("%s:%d: unsupported line: %r" % (path, lineno, raw.strip()))
                key, _, value = line.strip().partition(":")
                key = key.strip()
                if indent == 0:
                    if value.strip():
                        cfg.setdefault("_root", {})[key] = _coerce(value)
                    else:
                        section = key
                        cfg.setdefault(section, {})
                else:
                    if section is None:
                        raise ValueError("%s:%d: value outside any section" % (path, lineno))
                    cfg[section][key] = _coerce(value)

    # environment overrides: OPDS_SERVER_PORT, OPDS_LIBRARY_PATH, ...
    for section, keys in cfg.items():
        for key in keys:
            env = os.environ.get("OPDS_%s_%s" % (section.upper(), key.upper()))
            if env is not None:
                cfg[section][key] = _coerce(env)
    return cfg


# ---------------------------------------------------------------- password

def hash_password(password, salt=None, iterations=PBKDF2_ITER):
    salt = salt or os.urandom(16)
    dk = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2$%d$%s$%s" % (iterations, salt.hex(), dk.hex())


def verify_password(password, stored):
    try:
        algo, iters, salt, digest = stored.split("$")
        assert algo == "pbkdf2"
        expected = pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt), int(iters))
    except (ValueError, AssertionError):
        return False
    return hmac.compare_digest(expected.hex(), digest)


# ------------------------------------------------------------------ logging

def setup_logging(cfg, to_stderr=False):
    logcfg = cfg["logging"]
    os.makedirs(os.path.dirname(logcfg["path"]), exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(logcfg["level"]).upper(), logging.INFO))
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        logcfg["path"], maxBytes=int(logcfg["max_bytes"]),
        backupCount=int(logcfg["backups"]), encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if to_stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ------------------------------------------------------------------ handler

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # drop connections a client stopped using: a phone that sleeps or changes
    # network leaves the socket open, and the thread serving it would otherwise
    # wait on the kernel keepalive, which is hours away
    timeout = 60
    server_version = "ComicsOPDS/1.0"
    sys_version = ""

    # iniettati da serve()
    lib = None
    cfg = None

    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        log.warning("%s %s", self.address_string(), fmt % args)

    # -- helpers risposta

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, ("%d %s\n" % (code, message)).encode())

    def _base_url(self):
        """URL pubblico: config, altrimenti X-Forwarded-*, altrimenti Host."""
        configured = self.cfg["server"].get("base_url")
        if configured:
            return str(configured).rstrip("/")
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        host = (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host") or "localhost").split(",")[0].strip()
        return "%s://%s" % (proto, host)

    # -- auth

    def _authorized(self):
        sec = self.cfg["security"]
        if not sec.get("enabled"):
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        ok = _check_basic(header[6:], sec.get("username") or "", sec.get("password_hash") or "")
        if not ok:
            log.warning("failed login from %s on %s", self.address_string(), self.path)
        return ok

    def _require_auth(self):
        self._send(401, b"401 Unauthorized\n",
                   headers=[("WWW-Authenticate", 'Basic realm="Comics", charset="UTF-8"')])

    # -- routing

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/health":                      # for the reverse proxy, no auth
            return self._send(200, b"ok\n")
        if not self._authorized():
            return self._require_auth()

        try:
            if path == "/" and self.command in ("GET", "HEAD"):
                return self._home()
            if path == "/opds":
                return self._opds_folder(self.lib.root_id)
            if path.startswith("/opds/folder/"):
                return self._opds_folder(path[len("/opds/folder/"):])
            if path.startswith("/cover/"):
                return self._cover(path[len("/cover/"):])
            if path.startswith("/file/"):
                return self._file(path[len("/file/"):])
            if path.startswith("/page/"):
                return self._page(path[len("/page/"):], query)
            if path == "/admin/scan" and self.command == "POST":
                return self._scan()
        except BrokenPipeError:
            pass  # client hung up mid-download: routine with OPDS readers
        except Exception:
            log.exception("error on %s", self.path)
            return self._error(500, "Internal Server Error")
        self._error(404, "Not Found")

    # -- route

    def _home(self):
        s = self.lib.stats()
        body = ("""<!doctype html><meta charset=utf-8>
<title>Comics OPDS</title>
<style>body{font-family:system-ui;margin:3rem auto;max-width:34rem}
td{padding:.2rem 1rem .2rem 0}button{padding:.5rem 1rem}</style>
<h1>Comics OPDS</h1>
<table>
<tr><td>Folders<td><b>%d</b>
<tr><td>Comics<td><b>%d</b>
<tr><td>Last scan<td>%s
<tr><td>Catalogue<td><a href="/opds">/opds</a>
</table>
<form method=post action=/admin/scan><button>Scan now</button></form>
""" % (s["folders"], s["books"], html.escape(s["last_scan"]))).encode()
        self._send(200, body, "text/html; charset=utf-8")

    def _opds_folder(self, folder_id):
        folder = self.lib.folder(folder_id)
        if folder is None:
            return self._error(404, "No such folder")
        children = self.lib.subfolders(folder_id)
        books = self.lib.folder_books(folder_id)
        body = opds.folder_feed(self._base_url(), folder, children, books,
                                self.lib.root_id, self.lib.child_cover_ids(folder))
        self._send(200, body, opds.FEED)

    def _cover(self, book_id):
        row = self.lib.book(book_id)
        if row is None:
            return self._error(404, "No such comic")
        path = self.lib.cover_path(row)
        if path is None:
            return self._error(404, "No cover available")
        ctype = library.IMAGE_MIME.get(os.path.splitext(path)[1].lower(), "image/jpeg")
        self._send_file(path, ctype, download_name=None,
                        headers=[("Cache-Control", "public, max-age=604800")])

    def _file(self, book_id):
        row = self.lib.book(book_id)
        if row is None:
            return self._error(404, "No such comic")
        self._send_file(self.lib.abspath(row),
                        library.MIME.get(row["kind"], library.MIME["unknown"]),
                        download_name=row["filename"])

    def _page(self, book_id, query):
        """OPDS-PSE: a single page out of the archive, without downloading all of it."""
        row = self.lib.book(book_id)
        if row is None:
            return self._error(404, "No such comic")
        try:
            index = int(query.get("page", ["0"])[0])
        except ValueError:
            return self._error(400, "Invalid page parameter")
        archive, out_of_range = None, False
        try:
            archive = library.open_archive(self.lib.abspath(row), row["kind"])
            if archive is None:
                raise ValueError("no reader for %s archives" % row["kind"])
            names = library.cached_page_names(
                archive, row["path"], row["size"], row["mtime"])
            if not names:
                raise ValueError("no readable pages inside")
            out_of_range = not 0 <= index < len(names)
            if not out_of_range:
                data = archive.read(names[index])
                ctype = library.IMAGE_MIME.get(
                    os.path.splitext(names[index])[1].lower(), "image/jpeg")
        except Exception as e:
            # unsupported format, or an archive that will not open: a broken file
            # is the client's answer, never a 500
            log.warning("cannot stream pages of %s: %s", row["path"], e)
            return self._error(415, "Page streaming unavailable for this file")
        finally:
            if archive is not None:
                archive.close()
        if out_of_range:
            return self._error(404, "Page out of range")
        self._send(200, data, ctype, headers=[("Cache-Control", "public, max-age=86400")])

    def _scan(self):
        threading.Thread(target=self.lib.scan, daemon=True, name="scan").start()
        self._send(303, b"", headers=[("Location", "/")])

    # -- file serving with Range support (never the whole file in memory)

    def _send_file(self, path, ctype, download_name=None, headers=()):
        try:
            size = os.path.getsize(path)
        except OSError as e:
            log.warning("missing file %s: %s", path, e)
            return self._error(404, "File unavailable")

        extra = list(headers) + [("Accept-Ranges", "bytes")]
        if download_name:
            ascii_name = download_name.encode("ascii", "replace").decode()
            extra.append(("Content-Disposition",
                          'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                          % (ascii_name.replace('"', "_"),
                             _pct(download_name))))

        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng and size:
            parsed = _parse_range(rng, size)
            if parsed is None:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            status = 206
            extra.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))

        length = end - start + 1 if size else 0
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD" or length <= 0:
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def _pct(text):
    return "".join(c if c.isalnum() or c in "-._~" else
                   "".join("%%%02X" % b for b in c.encode("utf-8")) for c in text)


def _parse_range(header, size):
    """Single ranges only, which is all OPDS readers ask for. None means 416."""
    if not header.startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    first, _, last = spec.partition("-")
    try:
        if not first:                      # bytes=-500 -> the last 500
            n = int(last)
            if n <= 0:
                return None
            return max(0, size - n), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


@lru_cache(maxsize=16)
def _check_basic(b64, username, password_hash):
    """pbkdf2 is deliberately slow: the cache avoids recomputing it for every cover."""
    try:
        user, _, password = base64.b64decode(b64).decode("utf-8").partition(":")
    except Exception:
        return False
    if not hmac.compare_digest(user, username):
        return False
    return verify_password(password, password_hash)


# -------------------------------------------------------------------- serve

class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET
    request_queue_size = 32


def serve(cfg):
    lib = build_library(cfg)
    Handler.lib, Handler.cfg = lib, cfg

    if cfg["security"].get("enabled") and not cfg["security"].get("password_hash"):
        log.error("security.enabled=true but password_hash is empty: run 'server.py passwd'")
        raise SystemExit(2)

    log.info("RAR archives: %s", "readable" if library.rarfile
             else "download only (pip install rarfile for covers and page streaming)")

    if cfg["scan"].get("on_startup"):
        threading.Thread(target=lib.scan, daemon=True, name="scan-startup").start()

    interval = int(cfg["scan"].get("interval_minutes") or 0)
    stop = threading.Event()
    if interval > 0:
        def loop():
            while not stop.wait(interval * 60):
                lib.scan()
        threading.Thread(target=loop, daemon=True, name="scan-timer").start()

    host, port = cfg["server"]["host"], int(cfg["server"]["port"])
    httpd = Server((host, port), Handler)
    log.info("server listening on %s:%s (library %s, auth %s)", host, port,
             lib.root, "on" if cfg["security"].get("enabled") else "OFF")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=httpd.shutdown).start())
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()
        log.info("server stopped")


def build_library(cfg):
    cover_dir = os.path.join(str(cfg["cache"]["path"]), "covers")
    for d in (os.path.dirname(str(cfg["database"]["path"])), cover_dir):
        os.makedirs(d, exist_ok=True)
    root = str(cfg["library"]["path"])
    if not os.path.isdir(root):
        log.error("library folder not found: %s", root)
        raise SystemExit(2)
    return Library(cfg["database"]["path"], root, cover_dir)


# ---------------------------------------------------------------------- CLI

def port_in_use(host, port):
    """True when something is already listening there.

    A stale foreground instance holds the port without owning the PID file, so
    checking the PID file alone is not enough to tell whether we can start.
    """
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def pidfile(cfg):
    return os.path.join(os.path.dirname(str(cfg["database"]["path"])), "server.pid")


def running_pid(cfg):
    try:
        with open(pidfile(cfg)) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def cmd_start(cfg):
    if running_pid(cfg):
        print("already running (pid %d)" % running_pid(cfg))
        return 1
    # check loudly in the terminal, before disappearing into the background
    if not os.path.isdir(str(cfg["library"]["path"])):
        print("library folder not found: %s" % cfg["library"]["path"], file=sys.stderr)
        return 2
    if cfg["security"].get("enabled") and not cfg["security"].get("password_hash"):
        print("security.enabled=true but password_hash is empty "
              "(generate one with: python3 server.py passwd)", file=sys.stderr)
        return 2
    host, port = str(cfg["server"]["host"]), int(cfg["server"]["port"])
    if port_in_use(host, port):
        print("%s:%d is already in use - another instance, or a foreground "
              "'server.py run' still alive?" % (host, port), file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(pidfile(cfg)), exist_ok=True)
    if os.fork() > 0:                       # parent returns at once, for Task Scheduler
        for _ in range(20):
            time.sleep(0.25)
            pid = running_pid(cfg)
            if pid:
                print("started (pid %d)" % pid)
                return 0
        print("startup failed, see logs/startup.log", file=sys.stderr)
        return 1
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    startup_log = os.path.join(os.path.dirname(str(cfg["logging"]["path"])),
                               "startup.log")
    with open(os.devnull, "rb") as devnull, open(startup_log, "ab", buffering=0) as out:
        os.dup2(devnull.fileno(), 0)
        os.dup2(out.fileno(), 1)
        os.dup2(out.fileno(), 2)
    with open(pidfile(cfg), "w") as f:
        f.write(str(os.getpid()))
    try:
        serve(cfg)
    finally:
        try:
            os.remove(pidfile(cfg))
        except OSError:
            pass
    os._exit(0)


def cmd_stop(cfg):
    pid = running_pid(cfg)
    if not pid:
        print("not running")
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.2)
        if not running_pid(cfg):
            print("stopped")
            return 0
    os.kill(pid, signal.SIGKILL)
    print("stopped (SIGKILL)")
    return 0


def cmd_passwd():
    import getpass
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat: "):
        print("passwords do not match", file=sys.stderr)
        return 1
    print("\nPaste this into config.yaml under security:\n")
    print("  password_hash: %s" % hash_password(pw))
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "passwd":
        return cmd_passwd()
    config_path = argv[2] if len(argv) > 2 else os.path.join(BASE_DIR, "config", "config.yaml")
    cfg = load_config(config_path)

    if cmd in ("run", "scan", "rescan"):
        setup_logging(cfg, to_stderr=True)
    else:
        setup_logging(cfg)

    if cmd == "run":
        serve(cfg)
        return 0
    if cmd == "scan":
        build_library(cfg).scan()
        return 0
    if cmd == "rescan":
        build_library(cfg).scan(force=True)
        return 0
    if cmd == "start":
        return cmd_start(cfg)
    if cmd == "stop":
        return cmd_stop(cfg)
    if cmd == "restart":
        cmd_stop(cfg)
        return cmd_start(cfg)
    if cmd == "status":
        pid = running_pid(cfg)
        print("running (pid %d)" % pid if pid else "not running")
        return 0 if pid else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
