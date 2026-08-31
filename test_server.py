#!/usr/bin/env python3
"""End-to-end self-check: builds a fake library, scans it, starts the server and
verifies OPDS, auth, Range, covers and page streaming.

    python3 test_server.py
"""
import base64
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
import server as srv
import library as lib

PNG = (b"\x89PNG\r\n\x1a\n" +
       b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1, 1) + b"\x08\x02\x00\x00\x00" +
       struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">II", 1, 1) + b"\x08\x02\x00\x00\x00")) +
       b"\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05" +
       struct.pack(">I", 0) + b"\x00\x00\x00\x00IEND\xaeB`\x82")

COMICINFO = """<?xml version="1.0"?>
<ComicInfo><Title>The Judge</Title><Series>Grendel</Series><Number>1</Number>
<Year>1986</Year><Writer>Matt Wagner</Writer><Publisher>Comico</Publisher>
<Summary>Hunter Rose.</Summary></ComicInfo>"""

PAGES = ["1.png", "2.png", "10.png"]   # natural sort: 1, 2, 10


def make_cbz(path, pages=PAGES, comicinfo=None, junk=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        if junk:
            z.writestr("__MACOSX/._1.png", b"junk")
            z.writestr(".hidden.png", b"junk")
            z.writestr("metadata.xml", b"<x/>")
        for i, name in enumerate(pages):
            z.writestr(name, PNG + bytes([i]))     # every page has distinct bytes
        if comicinfo:
            z.writestr("ComicInfo.xml", comicinfo)


def build_library(root):
    make_cbz(os.path.join(root, "Watchmen", "Watchmen 01.cbz"))
    make_cbz(os.path.join(root, "Watchmen", "Watchmen 02.cbz"))
    make_cbz(os.path.join(root, "Grendel", "#1.cbr"), comicinfo=COMICINFO)  # a ZIP wearing a .cbr extension
    make_cbz(os.path.join(root, "Batman", "Year One.cbz"))
    # nested tree plus a mixed folder: files and subfolders side by side
    torre = os.path.join(root, "La Torre Nera")
    make_cbz(os.path.join(torre, "#1 - La Nascita del Pistolero", "#1.cbr"))
    make_cbz(os.path.join(torre, "#1 - La Nascita del Pistolero", "#2.cbr"))
    make_cbz(os.path.join(torre, "#2 - La Lunga Via", "#1.cbr"))
    make_cbz(os.path.join(torre, "#10 - Ultimo", "#1.cbr"))
    make_cbz(os.path.join(torre, "Artbook.cbz"))
    make_cbz(os.path.join(torre, "Extra material", "note.cbz"))
    os.makedirs(os.path.join(root, "Broken"), exist_ok=True)
    with open(os.path.join(root, "Broken", "corrupt.cbz"), "wb") as f:
        f.write(b"non sono uno zip" * 10)
    with open(os.path.join(root, "Broken", "manual.pdf"), "wb") as f:
        f.write(b"%PDF-1.4\n" + b"x" * 5000)
    with open(os.path.join(root, "Broken", "real.cbr"), "wb") as f:
        f.write(b"Rar!\x1a\x07\x00" + b"y" * 3000)     # a real RAR, with no `rarfile` installed


def get(url, user="tester", password="s3gr3t0", **kw):
    req = urllib.request.Request(url, **kw)
    if user:
        token = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    return urllib.request.urlopen(req, timeout=10)


def main():
    tmp = tempfile.mkdtemp(prefix="opdstest-")
    root = os.path.join(tmp, "Comics")
    build_library(root)

    cfg = srv.load_config(None)
    cfg["library"]["path"] = root
    cfg["database"]["path"] = os.path.join(tmp, "data", "library.db")
    cfg["cache"]["path"] = os.path.join(tmp, "cache")
    cfg["logging"]["path"] = os.path.join(tmp, "logs", "server.log")
    cfg["server"]["port"] = 0            # ephemeral port
    cfg["scan"]["on_startup"] = False
    cfg["scan"]["interval_minutes"] = 0
    cfg["security"] = {"enabled": True, "username": "tester",
                       "password_hash": srv.hash_password("s3gr3t0", iterations=1000)}
    srv.setup_logging(cfg)

    # ---- config parser
    written = os.path.join(tmp, "c.yaml")
    open(written, "w").write("server:\n  port: 2202  # commento\n  base_url: \n"
                             "security:\n  enabled: false\n")
    parsed = srv.load_config(written)
    assert parsed["server"]["port"] == 2202, parsed["server"]
    assert parsed["security"]["enabled"] is False
    assert parsed["server"]["base_url"] is None
    os.environ["OPDS_SERVER_PORT"] = "9999"
    assert srv.load_config(written)["server"]["port"] == 9999
    del os.environ["OPDS_SERVER_PORT"]
    print("ok  config: minimal yaml + environment overrides")

    # ---- password
    h = srv.hash_password("abc", iterations=1000)
    assert srv.verify_password("abc", h) and not srv.verify_password("abd", h)
    assert not srv.verify_password("abc", "garbage")
    print("ok  password: pbkdf2 accepts and rejects")

    # ---- natural sort
    assert lib.page_names.__module__  # sanity
    assert sorted(["10.jpg", "2.jpg", "1.jpg"], key=lib.natkey) == ["1.jpg", "2.jpg", "10.jpg"]
    print("ok  natural sort 1 < 2 < 10")

    # ---- range parser (unit)
    assert srv._parse_range("bytes=0-99", 1000) == (0, 99)
    assert srv._parse_range("bytes=500-", 1000) == (500, 999)
    assert srv._parse_range("bytes=-100", 1000) == (900, 999)
    assert srv._parse_range("bytes=0-9999", 1000) == (0, 999)
    assert srv._parse_range("bytes=1000-", 1000) is None
    assert srv._parse_range("pages=0-1", 1000) is None
    print("ok  Range parser (open-ended, suffix, past-EOF, invalid)")

    # ---- port guard: a foreground instance must not be clobbered silently
    import socket as _socket
    busy = _socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    taken = busy.getsockname()[1]
    assert srv.port_in_use("127.0.0.1", taken)
    busy.close()
    assert not srv.port_in_use("127.0.0.1", taken)
    print("ok  port guard: an occupied port is detected before daemonising")

    # ---- scanning
    library = srv.build_library(cfg)
    added, updated, removed, errors = library.scan()
    assert (added, updated, removed) == (13, 0, 0), (added, updated, removed)
    print("ok  first scan: %d added, %d errors "
          "(the corrupt .cbz stays downloadable as unknown)" % (added, errors))

    again = library.scan()
    assert again == (0, 0, 0, 0), again
    print("ok  incremental scan: nothing re-read")

    forced = library.scan(force=True)
    assert forced == (0, 13, 0, 0), forced   # every file re-read, none added or lost
    print("ok  forced rescan: every file re-read though nothing changed")

    os.remove(os.path.join(root, "Batman", "Year One.cbz"))
    assert library.scan()[2] == 1
    make_cbz(os.path.join(root, "Batman", "Year One.cbz"))
    assert library.scan()[0] == 1
    print("ok  scan: removal and re-insertion")

    # ---- type detection and metadata
    # keyed by relative path: filenames repeat across folders (#1.cbr)
    books = {r["path"]: r for r in library.connect().execute("SELECT * FROM books")}
    grendel = books["Grendel/#1.cbr"]
    assert grendel["kind"] == "zip", grendel["kind"]      # .cbr on the outside, ZIP on the inside
    assert grendel["title"] == "The Judge" and grendel["writer"] == "Matt Wagner"
    assert grendel["year"] == "1986" and grendel["number"] == "1"
    assert grendel["pages"] == 3, grendel["pages"]        # no __MACOSX, no metadata.xml
    assert books["Broken/manual.pdf"]["kind"] == "pdf"
    assert books["Broken/corrupt.cbz"]["kind"] == "unknown"
    assert books["Watchmen/Watchmen 01.cbz"]["title"] == "Watchmen 01"
    rar = books["Broken/real.cbr"]
    assert rar["kind"] == "rar" and rar["pages"] == 0
    print("ok  magic bytes (.cbr=ZIP), ComicInfo.xml, filename fallback")

    # ---- page list cache: sorting hundreds of names on every page turn is waste
    row = books["Watchmen/Watchmen 01.cbz"]
    archive = lib.open_archive(library.abspath(row), row["kind"])
    first = lib.cached_page_names(archive, row["path"], row["size"], row["mtime"])
    assert list(first) == PAGES
    # a hit must not touch the archive at all - None would explode if it did
    assert lib.cached_page_names(None, row["path"], row["size"], row["mtime"]) is first
    # a changed file must miss, so a re-scanned comic never serves stale pages
    fresh = lib.cached_page_names(archive, row["path"], row["size"], row["mtime"] + 1)
    assert fresh == first and fresh is not first
    archive.close()
    print("ok  page list cache: hit avoids reopening, edited file invalidates")


    # ---- server HTTP
    srv.Handler.lib, srv.Handler.cfg = library, cfg
    httpd = srv.Server(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    time.sleep(0.2)

    try:
        # auth
        try:
            get(base + "/opds", user=None)
            raise AssertionError("unauthenticated access allowed")
        except urllib.error.HTTPError as e:
            assert e.code == 401 and "Basic" in e.headers.get("WWW-Authenticate", "")
        try:
            get(base + "/opds", password="sbagliata")
            raise AssertionError("wrong password accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        assert get(base + "/health", user=None).status == 200
        print("ok  auth: 401 with no and with wrong credentials, /health open")

        # feed root
        r = get(base + "/opds")
        assert r.headers["Content-Type"].startswith("application/atom+xml")
        feed = ET.fromstring(r.read())
        ns = {"a": "http://www.w3.org/2005/Atom"}
        titles = [e.findtext("a:title", namespaces=ns) for e in feed.findall("a:entry", ns)]
        assert set(titles) == {"Watchmen", "Grendel", "Batman", "Broken",
                               "La Torre Nera"}, titles
        assert "#1 - La Nascita del Pistolero" not in titles, \
            "subfolders must not surface in the root feed"
        print("ok  root feed: top-level folders only (%d)" % len(titles))

        # --- tree: La Torre Nera holds both subfolders and a file (mixed feed)
        torre_href = next(e.find("a:link[@rel='subsection']", ns).get("href")
                          for e in feed.findall("a:entry", ns)
                          if e.findtext("a:title", namespaces=ns) == "La Torre Nera")
        torre = ET.fromstring(get(torre_href).read())
        order = [e.findtext("a:title", namespaces=ns) for e in torre.findall("a:entry", ns)]
        # folders and files in one order, not folders-then-files
        assert order == ["#1 - La Nascita del Pistolero", "#2 - La Lunga Via",
                         "#10 - Ultimo", "Artbook", "Extra material"], order
        up = torre.find("a:link[@rel='up']", ns)
        assert up is not None and up.get("href").endswith("/opds")
        # every folder announces an acquisition feed, so readers show cover art
        # instead of a generic folder icon - including folders holding only folders
        for e in feed.findall("a:entry", ns) + torre.findall("a:entry", ns):
            link = e.find("a:link[@rel='subsection']", ns)
            if link is not None:
                assert link.get("type").endswith("kind=acquisition"), \
                    (e.findtext("a:title", namespaces=ns), link.get("type"))
        print("ok  tree: single order for folders+files (#2 < #10 < Artbook), up link")

        # --- folder thumbnail = cover of the first comic in the subtree
        first_book = books["La Torre Nera/#1 - La Nascita del Pistolero/#1.cbr"]["id"]
        torre_entry = next(e for e in feed.findall("a:entry", ns)
                           if e.findtext("a:title", namespaces=ns) == "La Torre Nera")
        thumb = torre_entry.find("a:link[@rel='http://opds-spec.org/image/thumbnail']", ns)
        assert thumb is not None and thumb.get("href").endswith("/cover/" + first_book), \
            "a folder must show the cover of its first comic, however deep"
        assert get(thumb.get("href")).read().startswith(b"\x89PNG")
        # folder with nothing openable inside: no thumbnail, and no broken link
        rotti = next(e for e in feed.findall("a:entry", ns)
                     if e.findtext("a:title", namespaces=ns) == "Broken")
        assert rotti.find("a:link[@rel='http://opds-spec.org/image/thumbnail']", ns) is None
        print("ok  folder thumbnails: recursive cover, absent when not extractable")

        # --- leaf: comics live inside the subfolder, not flattened upwards
        leaf_href = next(e.find("a:link[@rel='subsection']", ns).get("href")
                         for e in torre.findall("a:entry", ns)
                         if e.findtext("a:title", namespaces=ns) == "#1 - La Nascita del Pistolero")
        leaf = ET.fromstring(get(leaf_href).read())
        assert len(leaf.findall("a:entry", ns)) == 2
        assert leaf.find("a:link[@rel='up']", ns).get("href") == torre_href
        print("ok  tree: two-level navigation, up returns to the parent folder")

        # folder feed
        href = next(e.find("a:link[@rel='subsection']", ns).get("href")
                    for e in feed.findall("a:entry", ns)
                    if e.findtext("a:title", namespaces=ns) == "Grendel")
        assert href.startswith("http://127.0.0.1"), href
        entries = ET.fromstring(get(href).read()).findall("a:entry", ns)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.findtext("a:title", namespaces=ns) == "The Judge #1"
        links = {l.get("rel"): l for l in entry.findall("a:link", ns)}
        assert links["http://opds-spec.org/acquisition"].get("type") == \
            "application/vnd.comicbook+zip"
        assert links["http://opds-spec.org/image/thumbnail"] is not None
        pse = links["http://vaemendis.net/opds-pse/stream"]
        assert pse.get("{http://vaemendis.net/opds-pse/ns}count") == "3"
        assert "{pageNumber}" in pse.get("href")
        print("ok  folder feed: complete OPDS entry, correct MIME, pse:count=3")

        # download + Range
        file_url = links["http://opds-spec.org/acquisition"].get("href")
        full = get(file_url)
        data = full.read()
        assert full.headers["Accept-Ranges"] == "bytes"
        assert int(full.headers["Content-Length"]) == len(data)
        assert "#1.cbr" in full.headers["Content-Disposition"]
        part = get(file_url, headers={"Range": "bytes=10-19"})
        chunk = part.read()
        assert part.status == 206 and chunk == data[10:20], (part.status, len(chunk))
        assert part.headers["Content-Range"] == "bytes 10-19/%d" % len(data)
        tail = get(file_url, headers={"Range": "bytes=-16"})
        assert tail.read() == data[-16:]
        try:
            get(file_url, headers={"Range": "bytes=%d-" % (len(data) + 5)})
            raise AssertionError("unsatisfiable range accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 416
        head = get(file_url, method="HEAD")
        assert head.read() == b"" and int(head.headers["Content-Length"]) == len(data)
        print("ok  download: partial 206, suffix, 416, HEAD (%d bytes)" % len(data))

        # covers and caching
        cover_url = links["http://opds-spec.org/image"].get("href")
        c = get(cover_url)
        assert c.headers["Content-Type"] == "image/png"
        assert c.read().startswith(b"\x89PNG")
        cached = os.path.join(cfg["cache"]["path"], "covers", grendel["id"] + ".png")
        assert os.path.exists(cached), os.listdir(os.path.dirname(cached))
        mtime = os.path.getmtime(cached)
        get(cover_url).read()
        assert os.path.getmtime(cached) == mtime, "cover regenerated instead of served from cache"
        print("ok  cover: real first page, cached on disk, not regenerated")

        # no cover available (pdf) -> clean 404
        pdf_id = books["Broken/manual.pdf"]["id"]
        try:
            get("%s/cover/%s" % (base, pdf_id))
            raise AssertionError("unexpected PDF cover")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        print("ok  formats without covers: 404, no crash")

        # PSE
        page_url = pse.get("href").replace("{pageNumber}", "2")
        p = get(page_url)
        body = p.read()
        assert p.headers["Content-Type"] == "image/png"
        assert body == PNG + bytes([2]), "wrong page: expected index 2 (10.png)"
        try:
            get(pse.get("href").replace("{pageNumber}", "99"))
            raise AssertionError("out-of-range page accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        print("ok  PSE: /page?page=N returns the right page in natural order")

        # a RAR we cannot read - either `rarfile` is missing, or the archive is
        # broken - stays downloadable, and must fail cleanly rather than with a 500
        rar_entry = next(e for e in ET.fromstring(get(base + "/opds/folder/" +
                         rar["dir_id"]).read()).findall("a:entry", ns)
                         if e.findtext("a:title", namespaces=ns).startswith("real"))
        rar_links = {l.get("rel"): l for l in rar_entry.findall("a:link", ns)}
        assert set(rar_links) == {"http://opds-spec.org/acquisition"}, rar_links
        assert rar_links["http://opds-spec.org/acquisition"].get("type") == \
            "application/vnd.comicbook-rar"
        assert len(get(rar_links["http://opds-spec.org/acquisition"].get("href")).read()) == 3007
        try:
            get("%s/page/%s?page=0" % (base, rar["id"]))
            raise AssertionError("page streaming on an unreadable RAR was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 415, "expected 415, got %d" % e.code
        print("ok  unreadable RAR: download works, no cover/PSE, clean 415 on /page")

        # reverse proxy
        r = get(base + "/opds", headers={"X-Forwarded-Proto": "https",
                                         "X-Forwarded-Host": "comics.example.com"})
        assert b"https://comics.example.com/opds/folder/" in r.read()
        # a proxy may include the port in X-Forwarded-Host (Tailscale Funnel does)
        r = get(base + "/opds", headers={"X-Forwarded-Proto": "https",
                                         "X-Forwarded-Host": "nas.example.ts.net:8443"})
        assert b"https://nas.example.ts.net:8443/opds/folder/" in r.read()
        cfg["server"]["base_url"] = "https://forced.example/comics"
        r = get(base + "/opds")
        assert b"https://forced.example/comics/opds/folder/" in r.read()
        cfg["server"]["base_url"] = ""
        print("ok  reverse proxy: X-Forwarded-* and a forced base_url")

        # no real paths in the feed, and traversal attempts rejected
        body = get(base + "/opds").read().decode()
        assert tmp not in body and "Comics/" not in body
        for bad in ("/file/../../etc/passwd", "/file/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                    "/cover/doesnotexist"):
            try:
                get(base + bad)
                raise AssertionError("accepted %s" % bad)
            except urllib.error.HTTPError as e:
                assert e.code == 404, (bad, e.code)
        print("ok  security: no paths in the feed, traversal rejected")

        # --- library folder selector
        altra = os.path.join(tmp, "Comics2")
        make_cbz(os.path.join(altra, "Nuova Serie", "#1.cbz"))
        cfg_file = os.path.join(tmp, "server.yaml")
        with open(cfg_file, "w") as f:
            f.write("library:\n  path: %s\n  browse_root: %s\n" % (root, tmp))
        srv.Handler.config_path = cfg_file
        cfg["library"]["browse_root"] = tmp

        os.makedirs(os.path.join(tmp, "@eaDir"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "#recycle"), exist_ok=True)
        scelte = srv.library_choices(cfg)
        assert altra in scelte and root in scelte, scelte
        assert "/etc" not in scelte
        # le cartelle di servizio di DSM non sono librerie
        assert not [c for c in scelte if os.path.basename(c)[:1] in ("@", "#", ".")], scelte

        def posta(dati):
            req = urllib.request.Request(base + "/admin/library", method="POST",
                                         data=dati.encode())
            req.add_header("Authorization", "Basic " + base64.b64encode(
                b"tester:s3gr3t0").decode())
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            return urllib.request.urlopen(req, timeout=10)

        try:
            posta("path=/etc")
            raise AssertionError("cartella arbitraria accettata")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code

        assert posta("path=" + urllib.parse.quote(altra)).status in (200, 303)
        for _ in range(50):
            if srv.Handler.lib.root == os.path.realpath(altra):
                break
            time.sleep(0.1)
        assert srv.Handler.lib.root == os.path.realpath(altra), srv.Handler.lib.root
        assert "path: %s" % altra in open(cfg_file).read(), open(cfg_file).read()
        for _ in range(60):
            if srv.Handler.lib.stats()["books"] == 1:
                break
            time.sleep(0.1)
        assert srv.Handler.lib.stats()["books"] == 1, srv.Handler.lib.stats()
        # tornando indietro l'indice si ricostruisce: il database e' lo stesso,
        # quindi il passaggio di cartella svuota e ripopola
        srv.Handler.lib, cfg["library"]["path"] = library, root
        library.scan()
        assert library.stats()["books"] == 13, library.stats()
        print("ok  library selector: only listed folders, config saved, reindexed")

        # homepage and admin scan
        home = get(base + "/").read().decode()
        assert "Folders" in home and ">9<" in home  # 9 folders, root excluded
        req = urllib.request.Request(base + "/admin/scan", method="POST", data=b"")
        token = base64.b64encode(b"tester:s3gr3t0").decode()
        req.add_header("Authorization", "Basic " + token)
        assert urllib.request.urlopen(req, timeout=10).status in (200, 303)
        print("ok  homepage and POST /admin/scan")
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
