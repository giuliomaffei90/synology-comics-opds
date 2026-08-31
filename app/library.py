"""Comic library indexing: scanning, SQLite, archives, covers.

No mandatory external dependencies. `rarfile` is used only when it happens to be
installed, which enables covers and page streaming for genuine RAR archives.
"""
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
import zipfile
from xml.etree import ElementTree as ET

try:
    import rarfile  # optional: covers and page streaming for real RAR
except ImportError:
    rarfile = None

log = logging.getLogger("library")

BOOK_EXT = {".cbz", ".cbr", ".cba", ".cb7", ".zip", ".rar", ".pdf", ".epub"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
SKIP_NAMES = {"comicinfo.xml", "metadata.xml", "thumbs.db", ".ds_store"}

MIME = {
    "zip": "application/vnd.comicbook+zip",
    "rar": "application/vnd.comicbook-rar",
    "pdf": "application/pdf",
    "epub": "application/epub+zip",
    "unknown": "application/octet-stream",
}
IMAGE_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}

SCHEMA_VERSION = 2      # bump to force a database rebuild

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id         TEXT PRIMARY KEY,
    path       TEXT UNIQUE NOT NULL,   -- relative to the library root
    filename   TEXT NOT NULL,
    dir        TEXT NOT NULL,          -- relative folder, '' = library root
    dir_id     TEXT NOT NULL,
    series     TEXT,                   -- series label (ComicInfo or folder name)
    size       INTEGER NOT NULL,
    mtime      REAL NOT NULL,
    kind       TEXT NOT NULL,          -- zip|rar|pdf|epub|unknown
    title      TEXT, number TEXT, volume TEXT, year TEXT,
    writer     TEXT, publisher TEXT, summary TEXT,
    pages      INTEGER NOT NULL DEFAULT 0,
    cover      TEXT                    -- filename under cache/covers, NULL = not extracted yet
);
CREATE INDEX IF NOT EXISTS idx_dir ON books(dir_id);

-- folder tree, rebuilt from the book paths on every scan
CREATE TABLE IF NOT EXISTS folders (
    id         TEXT PRIMARY KEY,
    path       TEXT UNIQUE NOT NULL,
    parent_id  TEXT,                   -- NULL for the root only
    name       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parent ON folders(parent_id);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def sid(text):
    """Stable, opaque id. Never exposes the filesystem."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def natkey(name):
    """Natural sort: 1.jpg < 2.jpg < 10.jpg."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def detect_kind(path, ext):
    """Real type from the magic bytes; the extension is only a fallback."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as e:
        log.warning("read header failed %s: %s", path, e)
        return "unknown"
    if head[:4] == b"PK\x03\x04":
        return "epub" if ext == ".epub" else "zip"
    if head[:4] == b"Rar!":
        return "rar"
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "unknown"  # cb7: downloadable, but we cannot look inside
    return "unknown"


def _is_page(name):
    base = os.path.basename(name)
    if not base or base.startswith(".") or name.startswith("__MACOSX"):
        return False
    if base.lower() in SKIP_NAMES:
        return False
    return os.path.splitext(base)[1].lower() in IMAGE_EXT


def open_archive(path, kind):
    """A ZipFile/RarFile, or None. The caller is responsible for closing it."""
    if kind in ("zip", "epub"):
        return zipfile.ZipFile(path)
    if kind == "rar" and rarfile is not None:
        return rarfile.RarFile(path)
    return None


def page_names(archive):
    return sorted((n for n in archive.namelist() if _is_page(n)), key=natkey)


# ---------------------------------------------------------------- metadata

_CI_FIELDS = {
    "Title": "title", "Series": "series_meta", "Number": "number",
    "Volume": "volume", "Year": "year", "Writer": "writer",
    "Publisher": "publisher", "Summary": "summary",
}


def read_comicinfo(archive):
    """ComicInfo.xml from the archive root. {} when missing or unreadable."""
    try:
        name = next(n for n in archive.namelist()
                    if os.path.basename(n).lower() == "comicinfo.xml")
        root = ET.fromstring(archive.read(name))
    except (StopIteration, KeyError, ET.ParseError, OSError, ValueError):
        return {}
    out = {}
    for el in root:
        key = _CI_FIELDS.get(el.tag)
        if key and el.text and el.text.strip():
            out[key] = el.text.strip()
    return out


def guess_title(filename):
    """Fallback without ComicInfo: cleaned-up filename, issue number if recognisable."""
    stem = os.path.splitext(filename)[0].strip()
    m = re.search(r"(?:#|\bn\.?\s*)?(\d{1,4})\s*$", stem)
    number = m.group(1).lstrip("0") or "0" if m else None
    return stem or filename, number


# ---------------------------------------------------------------- database

class Library:
    def __init__(self, db_path, root, cover_dir):
        self.db_path = str(db_path)
        self.root = os.path.realpath(str(root))
        self.cover_dir = str(cover_dir)
        self._local = threading.local()
        self.scan_lock = threading.Lock()
        self.root_id = sid("")
        with self.connect() as c:
            version = c.execute("PRAGMA user_version").fetchone()[0]
            # databases created before versioning report 0, and need rebuilding too
            if version != SCHEMA_VERSION and c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                log.warning("database schema v%d -> v%d: rebuilding, "
                            "a full scan will follow", version, SCHEMA_VERSION)
                for table in ("books", "folders", "meta"):
                    c.execute("DROP TABLE IF EXISTS " + table)
            c.executescript(SCHEMA)
            c.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)

    def connect(self):
        """One connection per thread: sqlite3 connections cannot be shared."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    # -- reads used by the routes

    def folder(self, folder_id):
        return self.connect().execute(
            "SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()

    def subfolders(self, folder_id):
        """Direct subfolders, with subtree counts and last-modified time.

        ponytail: the correlated subqueries scan `books` once per child. Negligible
        on a personal library; if it ever gets slow, materialise the counts into
        `folders` at the end of each scan.
        """
        rows = self.connect().execute("""
            SELECT f.*,
                   (SELECT COUNT(*) FROM books b WHERE b.dir=f.path
                       OR substr(b.dir, 1, length(f.path)+1)=f.path||'/') total,
                   (SELECT MAX(b.mtime) FROM books b WHERE b.dir=f.path
                       OR substr(b.dir, 1, length(f.path)+1)=f.path||'/') mtime
            FROM folders f WHERE f.parent_id=?""", (folder_id,)).fetchall()
        return sorted(rows, key=lambda r: natkey(r["name"]))

    def child_cover_ids(self, folder):
        """For each direct subfolder, the id of the first comic in its subtree that has
        an extractable cover. This is what gives folders a thumbnail.

        ponytail: one query over the subtree, sorted in Python because SQLite has
        no natural sort. Instant on a personal library.
        """
        prefix = folder["path"] + "/" if folder["path"] else ""
        rows = self.connect().execute(
            "SELECT id, path FROM books WHERE pages>0 AND substr(path,1,?)=?",
            (len(prefix), prefix)).fetchall()
        covers = {}
        for r in sorted(rows, key=lambda r: natkey(r["path"])):
            rest = r["path"][len(prefix):]
            if "/" not in rest:
                continue                      # a file of this folder, not of a child
            covers.setdefault(sid(prefix + rest.split("/")[0]), r["id"])
        return covers

    def folder_books(self, folder_id):
        rows = self.connect().execute(
            "SELECT * FROM books WHERE dir_id=?", (folder_id,)).fetchall()
        return sorted(rows, key=lambda r: natkey(r["filename"]))

    def book(self, book_id):
        return self.connect().execute(
            "SELECT * FROM books WHERE id=?", (book_id,)).fetchone()

    def stats(self):
        c = self.connect()
        n_books = c.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        n_folders = c.execute(
            "SELECT COUNT(*) FROM folders WHERE parent_id IS NOT NULL").fetchone()[0]
        last = c.execute("SELECT v FROM meta WHERE k='last_scan'").fetchone()
        return {"books": n_books, "folders": n_folders,
                "last_scan": last[0] if last else "never"}

    def abspath(self, row):
        """Absolute path, verified to stay inside the library root (traversal guard)."""
        p = os.path.realpath(os.path.join(self.root, row["path"]))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ValueError("path outside the library: %r" % row["path"])
        return p

    # -- covers (extracted on demand, then cached on disk)

    def cover_path(self, row):
        """Path to the cached cover, extracting it if needed. None when unavailable."""
        if row["cover"]:
            cached = os.path.join(self.cover_dir, row["cover"])
            if os.path.exists(cached):
                return cached
        return self._extract_cover(row)

    def _extract_cover(self, row):
        archive = None
        try:
            archive = open_archive(self.abspath(row), row["kind"])
            if archive is None:
                return None
            names = page_names(archive)
            if not names:
                return None
            ext = os.path.splitext(names[0])[1].lower()
            data = archive.read(names[0])
        except Exception as e:  # a corrupt archive must never take down a request
            log.warning("cover extraction failed %s: %s", row["path"], e)
            return None
        finally:
            if archive is not None:
                archive.close()
        name = row["id"] + ext
        dest = os.path.join(self.cover_dir, name)
        tmp = dest + ".tmp"
        os.makedirs(self.cover_dir, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        conn = self.connect()
        conn.execute("UPDATE books SET cover=? WHERE id=?", (name, row["id"]))
        conn.commit()
        return dest

    # -- scanning

    def scan(self, force=False):
        """Incremental scan. Returns (added, updated, removed, errors).

        force=True re-reads every file even when unchanged. Needed when the
        indexer itself gained an ability, such as reading RAR archives once
        `rarfile` is installed: the files did not change, but what we can
        learn from them did.
        """
        if not self.scan_lock.acquire(blocking=False):
            log.info("scan already running, skipping")
            return None
        try:
            return self._scan(force)
        finally:
            self.scan_lock.release()

    def _scan(self, force=False):
        t0 = time.time()
        log.info("scan start: %s%s", self.root, " (forced)" if force else "")
        conn = self.connect()
        known = {r["path"]: r for r in conn.execute(
            "SELECT path, size, mtime FROM books")}
        seen, added, updated, errors = set(), 0, 0, 0

        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and d != "__MACOSX")
            for fn in sorted(filenames):
                if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in BOOK_EXT:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.root)
                seen.add(rel)
                try:
                    st = os.stat(full)
                except OSError as e:
                    log.warning("stat failed %s: %s", rel, e)
                    errors += 1
                    continue
                old = known.get(rel)
                if (not force and old and old["size"] == st.st_size
                        and old["mtime"] == st.st_mtime):
                    continue
                try:
                    self._index(conn, full, rel, fn, st)
                except Exception as e:
                    log.warning("indexing failed %s: %s", rel, e)
                    errors += 1
                    continue
                if old:
                    updated += 1
                    log.info("updated: %s", rel)
                else:
                    added += 1
                    log.info("added: %s", rel)

        removed = 0
        for rel in set(known) - seen:
            row = conn.execute("SELECT id, cover FROM books WHERE path=?", (rel,)).fetchone()
            if row and row["cover"]:
                self._drop_cover(row["cover"])
            conn.execute("DELETE FROM books WHERE path=?", (rel,))
            removed += 1
            log.info("removed: %s", rel)

        self._rebuild_folders(conn)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_scan', ?)",
                     (time.strftime("%Y-%m-%d %H:%M:%S"),))
        conn.commit()
        log.info("scan end in %.1fs: +%d ~%d -%d errors=%d",
                 time.time() - t0, added, updated, removed, errors)
        return added, updated, removed, errors

    def _rebuild_folders(self, conn):
        """Rebuild the tree from the book paths: no incremental logic to keep in sync,
        and emptied folders disappear on their own."""
        paths = {""}
        for (d,) in conn.execute("SELECT DISTINCT dir FROM books"):
            parts = [p for p in d.split("/") if p]
            for i in range(len(parts)):
                paths.add("/".join(parts[:i + 1]))
        rows = []
        for p in sorted(paths):
            if not p:
                rows.append((self.root_id, "", None, "Comics"))
            else:
                parent, _, name = p.rpartition("/")
                rows.append((sid(p), p, sid(parent), name))
        conn.execute("DELETE FROM folders")
        conn.executemany("INSERT INTO folders (id,path,parent_id,name) VALUES (?,?,?,?)", rows)

    def _drop_cover(self, name):
        try:
            os.remove(os.path.join(self.cover_dir, name))
        except OSError:
            pass

    def _index(self, conn, full, rel, fn, st):
        directory = os.path.dirname(rel).replace(os.sep, "/")
        ext = os.path.splitext(fn)[1].lower()
        kind = detect_kind(full, ext)
        title, number = guess_title(fn)
        meta, pages = {}, 0

        archive = None
        try:
            archive = open_archive(full, kind)
            if archive is not None:
                pages = len(page_names(archive))   # central directory only, no decompression
                meta = read_comicinfo(archive)
        except Exception as e:
            log.warning("unreadable archive %s: %s", rel, e)
        finally:
            if archive is not None:
                archive.close()

        # ComicInfo wins over the fallback, but only for the fields it actually has
        series_label = (meta.get("series_meta")
                        or os.path.basename(directory) or "No series")
        if meta.get("title"):
            title = meta["title"]
        number = meta.get("number") or number

        # the file changed, so its cover did too: drop the stale one
        old = conn.execute("SELECT cover FROM books WHERE path=?", (rel,)).fetchone()
        if old and old["cover"]:
            self._drop_cover(old["cover"])

        conn.execute(
            "INSERT OR REPLACE INTO books (id,path,filename,dir,dir_id,series,size,mtime,"
            "kind,title,number,volume,year,writer,publisher,summary,pages,cover) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (sid(rel), rel, fn, directory, sid(directory), series_label,
             st.st_size, st.st_mtime,
             kind, title, number, meta.get("volume"), meta.get("year"),
             meta.get("writer"), meta.get("publisher"), meta.get("summary"), pages))
