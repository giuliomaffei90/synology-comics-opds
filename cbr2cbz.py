#!/usr/bin/env python3
"""Convert real RAR comics to CBZ, in place, keeping every original.

    python3 cbr2cbz.py --dry-run                 # list what would change
    python3 cbr2cbz.py --limit 1                 # convert one, then look at it
    python3 cbr2cbz.py                           # convert the rest
    python3 cbr2cbz.py --self-test               # check the repacking logic

Which files are converted is decided by magic bytes, not by extension, so a
`.cbr` that is already a ZIP is left alone. A file is replaced only after the
new archive has been reopened and found to hold exactly the same members, and
the original is moved aside rather than deleted - review the quarantine folder
yourself, then remove it when you are satisfied.

Requires `rarfile` and an `unrar` binary. See the README.
"""
import argparse
import os
import shutil
import sys
import tempfile
import zipfile

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
sys.path.insert(0, APP)
if os.path.isdir(LIB):
    sys.path.insert(0, LIB)
import library


def find_rars(root):
    """Every genuine RAR under root, by magic bytes rather than extension."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != "__MACOSX")
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if fn.startswith(".") or ext not in library.BOOK_EXT:
                continue
            full = os.path.join(dirpath, fn)
            if library.detect_kind(full, ext) == "rar":
                out.append(full)
    return out


def repack(archive, dest, workdir):
    """Extract every member, then store it in a ZIP. Returns the member names.

    Extract-then-zip rather than member-by-member: solid RAR archives make
    reading one entry at a time quadratic, while unrar handles the whole file
    in a single pass. Members are stored, not deflated - comic pages are
    already compressed images, so deflate would burn NAS cpu for nothing.
    """
    names = [n for n in archive.namelist() if not n.endswith("/")]
    archive.extractall(workdir)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        for name in sorted(names, key=library.natkey):
            src = os.path.join(workdir, name.replace("/", os.sep))
            if os.path.isfile(src):
                z.write(src, name)
            else:
                # unrar rewrites names it deems unsafe - a '|' in the page names of
                # some scans - so the extracted file is not where its name says it
                # is. Read that member straight out of the archive instead.
                z.writestr(name, archive.read(name))
    return names


def verify(dest, names):
    """Reopen the result and insist it holds the same members and pages."""
    with zipfile.ZipFile(dest) as z:
        if z.testzip() is not None:
            raise ValueError("the new archive does not read back cleanly")
        got, want = set(z.namelist()), set(names)
        if got != want:
            missing, extra = sorted(want - got)[:3], sorted(got - want)[:3]
            raise ValueError("members differ (missing %s, unexpected %s)"
                             % (missing, extra))
        pages_new = library.page_names(z)
    if not pages_new:
        raise ValueError("the new archive contains no readable pages")
    return len(pages_new)


def convert(path, root, quarantine, dry_run):
    dest = os.path.splitext(path)[0] + ".cbz"
    rel = os.path.relpath(path, root)
    if os.path.exists(dest):
        return "skipped", rel, "a .cbz of the same name already exists"

    size = os.path.getsize(path)
    free = shutil.disk_usage(os.path.dirname(path)).free
    if free < size * 2.5:
        return "failed", rel, "not enough free space (%.1f GB left)" % (free / 1e9)
    if dry_run:
        return "would convert", rel, "%.0f MB" % (size / 1e6)

    archive = None
    workdir = tempfile.mkdtemp(prefix="cbr2cbz-", dir=os.path.dirname(path))
    tmp_dest = dest + ".part"
    try:
        archive = library.open_archive(path, "rar")
        if archive is None:
            return "failed", rel, "no RAR reader available (install rarfile)"
        names = repack(archive, tmp_dest, workdir)
        pages = verify(tmp_dest, names)

        stat = os.stat(path)
        os.utime(tmp_dest, (stat.st_atime, stat.st_mtime))

        keep = os.path.join(quarantine, rel)
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        shutil.move(path, keep)          # never deleted, only moved aside
        os.replace(tmp_dest, dest)
        return "converted", rel, "%d pages, %.0f MB" % (pages, os.path.getsize(dest) / 1e6)
    except Exception as e:
        if os.path.exists(tmp_dest):
            os.remove(tmp_dest)
        return "failed", rel, str(e)
    finally:
        if archive is not None:
            archive.close()
        shutil.rmtree(workdir, ignore_errors=True)


def self_test():
    """Repack and verify a synthetic archive, so the risky part is exercised
    before it is pointed at a real library."""
    tmp = tempfile.mkdtemp(prefix="cbr2cbz-selftest-")
    try:
        src = os.path.join(tmp, "source.zip")
        members = ["1.png", "2.png", "10.png", "sub/11.png", "ComicInfo.xml"]
        with zipfile.ZipFile(src, "w") as z:
            for n in members:
                z.writestr(n, b"\x89PNG\r\n\x1a\n" + n.encode())
        dest = os.path.join(tmp, "out.cbz")
        with zipfile.ZipFile(src) as archive:
            names = repack(archive, dest, os.path.join(tmp, "work"))
        assert set(names) == set(members), names
        pages = verify(dest, names)
        assert pages == 4, pages          # ComicInfo.xml is not a page
        with zipfile.ZipFile(dest) as z:
            assert library.page_names(z) == ["1.png", "2.png", "10.png", "sub/11.png"]
            assert z.read("ComicInfo.xml").endswith(b"ComicInfo.xml")
            assert z.getinfo("1.png").compress_type == zipfile.ZIP_STORED
        print("ok  repack: every member kept, natural page order, metadata preserved")

        # some archives extract under different names than they advertise; the
        # repack must fall back to reading those members directly
        class Renaming:
            def __init__(self, z):
                self.z = z

            def namelist(self):
                return self.z.namelist()

            def extractall(self, path):
                pass                      # as if every name had been rewritten

            def read(self, name):
                return self.z.read(name)

        awkward = os.path.join(tmp, "awkward.cbz")
        with zipfile.ZipFile(src) as archive:
            names = repack(Renaming(archive), awkward, os.path.join(tmp, "work2"))
        assert verify(awkward, names) == 4
        with zipfile.ZipFile(awkward) as z:
            assert z.read("sub/11.png").endswith(b"sub/11.png")
        print("ok  repack: members that do not land on disk are read from the archive")

        broken = os.path.join(tmp, "broken.cbz")
        with zipfile.ZipFile(broken, "w") as z:
            z.writestr("1.png", b"x")
        try:
            verify(broken, members)
            raise AssertionError("verify accepted an archive with missing members")
        except ValueError:
            pass
        print("ok  verify: refuses to replace an original when members are lost")
        print("\nAll checks passed.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("library", nargs="?", default="/volume1/Contents/Comics")
    p.add_argument("--quarantine", help="where originals are moved "
                                        "(default: <library>-rar-originals, outside the library)")
    p.add_argument("--limit", type=int, help="convert at most this many files")
    p.add_argument("--dry-run", action="store_true", help="list, change nothing")
    p.add_argument("--self-test", action="store_true", help="check the logic and exit")
    args = p.parse_args()

    if args.self_test:
        return self_test()

    root = os.path.realpath(args.library)
    if not os.path.isdir(root):
        print("no such library: %s" % root, file=sys.stderr)
        return 2
    # deliberately outside the library, or the server would index the originals
    quarantine = os.path.realpath(args.quarantine or root + "-rar-originals")
    if quarantine.startswith(root + os.sep):
        print("the quarantine folder must sit outside the library", file=sys.stderr)
        return 2
    if library.rarfile is None and not args.dry_run:
        print("rarfile is not installed - see the README", file=sys.stderr)
        return 2

    print("library    : %s" % root)
    print("originals  : %s" % ("not moved (dry run)" if args.dry_run else quarantine))
    files = find_rars(root)
    if args.limit:
        files = files[:args.limit]
    print("real RAR   : %d file(s), %.1f GB\n"
          % (len(files), sum(os.path.getsize(f) for f in files) / 1e9))

    tally = {}
    for i, path in enumerate(files, 1):
        status, rel, detail = convert(path, root, quarantine, args.dry_run)
        tally[status] = tally.get(status, 0) + 1
        print("[%d/%d] %-13s %s  (%s)" % (i, len(files), status, rel, detail))
        sys.stdout.flush()

    print("\n" + ", ".join("%s: %d" % kv for kv in sorted(tally.items())))
    if tally.get("converted"):
        print("\nOriginals kept in %s - delete that folder once you are happy." % quarantine)
        print("Then re-index:  python3 app/server.py scan")
    return 1 if tally.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
