"""OPDS 1.2 (Atom) feed generation."""
import time
from urllib.parse import quote
from xml.etree import ElementTree as ET

from library import MIME, natkey

ATOM = "http://www.w3.org/2005/Atom"
OPDS = "http://opds-spec.org/2010/catalog"
DC = "http://purl.org/dc/terms/"
PSE = "http://vaemendis.net/opds-pse/ns"

NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"
REL_ACQ = "http://opds-spec.org/acquisition"
REL_IMG = "http://opds-spec.org/image"
REL_THUMB = "http://opds-spec.org/image/thumbnail"
REL_PSE = "http://vaemendis.net/opds-pse/stream"

for prefix, uri in (("", ATOM), ("opds", OPDS), ("dc", DC), ("pse", PSE)):
    ET.register_namespace(prefix, uri)


def rfc3339(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = text
    return el


def _feed(base, feed_id, title, updated, self_href, self_type):
    feed = ET.Element("{%s}feed" % ATOM)
    _sub(feed, "id", feed_id)
    _sub(feed, "title", title)
    _sub(feed, "updated", rfc3339(updated))
    _sub(_sub(feed, "author"), "name", "Comics OPDS")
    _sub(feed, "link", rel="self", href=base + self_href, type=self_type)
    _sub(feed, "link", rel="start", href=base + "/opds", type=NAV)
    return feed


def _render(feed):
    body = ET.tostring(feed, encoding="utf-8", xml_declaration=False)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + body


def folder_href(folder_id, root_id):
    """The root lives at /opds: that is the URL a user configures in their reader."""
    return "/opds" if folder_id == root_id else "/opds/folder/" + quote(folder_id)


def entry_title(b):
    """Displayed title, also used as the sort key."""
    title = b["title"] or b["filename"]
    if b["number"] and b["number"] not in title:
        title = "%s #%s" % (title, b["number"])
    return title


def folder_feed(base, folder, children, books, root_id, covers):
    """One folder: subfolders as navigation entries, comics as acquisition entries.

    OPDS 1.x allows mixed entries in a single feed, which is what lets the folder
    tree survive instead of being flattened.
    """
    updated = max([b["mtime"] for b in books]
                  + [c["mtime"] for c in children if c["mtime"]],
                  default=time.time())
    self_href = folder_href(folder["id"], root_id)
    feed = _feed(base, "urn:comics:folder:" + folder["id"], folder["name"],
                 updated, self_href, ACQ if books else NAV)
    if folder["parent_id"]:
        _sub(feed, "link", rel="up", type=NAV,
             href=base + folder_href(folder["parent_id"], root_id))

    # folders and comics interleaved in one natural order, the way a reader sees them
    items = [(natkey(c["name"]), _folder_entry(base, c, root_id, covers.get(c["id"])))
             for c in children]
    items += [(natkey(entry_title(b)), _entry(base, b)) for b in books]
    for _, element in sorted(items, key=lambda item: item[0]):
        feed.append(element)
    return _render(feed)


def _folder_entry(base, c, root_id, cover_id):
    entry = ET.Element("entry")
    _sub(entry, "id", "urn:comics:folder:" + c["id"])
    _sub(entry, "title", c["name"])
    _sub(entry, "updated", rfc3339(c["mtime"] or time.time()))
    _sub(entry, "content", "%d comics" % c["total"], type="text")
    # navigation link first: some clients just follow the first <link> they find.
    # kind=acquisition only when the folder actually holds files
    _sub(entry, "link", rel="subsection", type=ACQ if c["direct"] else NAV,
         href=base + folder_href(c["id"], root_id))
    # folder thumbnail = the cover of its first comic
    if cover_id:
        for rel in (REL_THUMB, REL_IMG):
            _sub(entry, "link", rel=rel, type="image/jpeg",
                 href="%s/cover/%s" % (base, cover_id))
    return entry


def _entry(base, b):
    entry = ET.Element("entry")
    _sub(entry, "id", "urn:comics:book:" + b["id"])
    _sub(entry, "title", entry_title(b))
    _sub(entry, "updated", rfc3339(b["mtime"]))
    if b["writer"]:
        _sub(_sub(entry, "author"), "name", b["writer"])
    if b["publisher"]:
        _sub(entry, "{%s}publisher" % DC, b["publisher"])
    if b["year"]:
        _sub(entry, "{%s}issued" % DC, str(b["year"]))
    if b["summary"]:
        _sub(entry, "summary", b["summary"], type="text")

    if b["pages"]:
        for rel in (REL_THUMB, REL_IMG):
            _sub(entry, "link", rel=rel, type="image/jpeg",
                 href="%s/cover/%s" % (base, b["id"]))

    _sub(entry, "link", rel=REL_ACQ, type=MIME.get(b["kind"], MIME["unknown"]),
         href="%s/file/%s" % (base, b["id"]), length=str(b["size"]))

    # Page streaming: the client reads one page at a time, no full download
    if b["pages"]:
        pse = _sub(entry, "link", rel=REL_PSE, type="image/jpeg",
                   href="%s/page/%s?page={pageNumber}" % (base, b["id"]))
        pse.set("{%s}count" % PSE, str(b["pages"]))
    return entry
