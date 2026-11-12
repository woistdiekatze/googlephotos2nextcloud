"""Tests for gp2nc, including an end-to-end run against a fake in-memory Nextcloud."""
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse
from xml.sax.saxutils import escape

import pytest
from PIL import Image

from gp2nc import importer as gp2nc

os.environ["GP2NC_RETRY_DELAY"] = "0"

FILES = "/remote.php/dav/files/user"
ALBUMS = "/remote.php/dav/photos/user/albums"


# --------------------------------------------------------------------------
# Fake Nextcloud (just enough WebDAV for gp2nc)
# --------------------------------------------------------------------------
class FakeNextcloud:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.ids: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.put_headers: dict[str, dict[str, str]] = {}
        self._next_id = 100
        for directory in (FILES + "/Photos", ALBUMS):
            self.add_dir(directory)

    def add_dir(self, path: str) -> None:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            self.dirs.add("/" + "/".join(parts[:i]))

    def fileid(self, path: str) -> int:
        if path not in self.ids:
            self.ids[path] = self._next_id
            self._next_id += 1
        return self.ids[path]

    def count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)


def make_handler(nc: FakeNextcloud):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def path_(self) -> str:
            return unquote(urlparse(self.path).path).rstrip("/") or "/"

        def dest_(self) -> str:
            return unquote(urlparse(self.headers["Destination"]).path).rstrip("/")

        def reply(self, status, body=b"", headers=None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def record(self):
            nc.calls.append((self.command, self.path_()))

        def do_PROPFIND(self):
            self.record()
            self.body()
            path = self.path_()
            if path not in nc.dirs and path not in nc.files:
                return self.reply(404)
            depth = int(self.headers.get("Depth", "0"))
            entries = [path]
            if depth > 0:
                base = path.count("/")
                for candidate in sorted(nc.dirs | set(nc.files)):
                    if candidate.startswith(path + "/") and candidate.count("/") - base <= depth:
                        entries.append(candidate)
            out = ['<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">']
            for entry in entries:
                is_dir = entry in nc.dirs
                props = [f"<d:displayname>{escape(entry.rpartition('/')[2])}</d:displayname>"]
                props.append(f"<oc:fileid>{nc.fileid(entry)}</oc:fileid>")
                if is_dir:
                    props.append("<d:resourcetype><d:collection/></d:resourcetype>")
                else:
                    data = nc.files[entry]
                    props.append("<d:resourcetype/>")
                    props.append(f"<d:getcontentlength>{len(data)}</d:getcontentlength>")
                    props.append(
                        "<oc:checksums><oc:checksum>SHA1:%s MD5:%s ADLER32:00000001</oc:checksum></oc:checksums>"
                        % (hashlib.sha1(data).hexdigest(), hashlib.md5(data).hexdigest())
                    )
                href = quote(entry) + ("/" if is_dir else "")
                out.append(
                    f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{''.join(props)}</d:prop>"
                    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                )
            out.append("</d:multistatus>")
            self.reply(207, "".join(out).encode(), {"Content-Type": "application/xml"})

        def do_HEAD(self):
            self.record()
            path = self.path_()
            if path in nc.files:
                return self._head_file(path)
            if path in nc.dirs:
                return self.reply(200)
            self.reply(404)

        def _head_file(self, path):
            self.send_response(200)
            self.send_header("Content-Length", str(len(nc.files[path])))
            self.end_headers()

        def do_MKCOL(self):
            self.record()
            path = self.path_()
            if path in nc.dirs:
                return self.reply(405)
            if path.rpartition("/")[0] not in nc.dirs:
                return self.reply(409)
            nc.dirs.add(path)
            self.reply(201)

        def do_PUT(self):
            self.record()
            data = self.body()
            path = self.path_()
            if path.rpartition("/")[0] not in nc.dirs:
                return self.reply(409)
            nc.files[path] = data
            nc.put_headers[path] = {k.lower(): v for k, v in self.headers.items()}
            nc.fileid(path)
            self.reply(201)

        def do_PATCH(self):
            self.record()
            self.body()
            self.reply(204)

        def do_COPY(self):
            self.record()
            source, destination = self.path_(), self.dest_()
            if source not in nc.files:
                return self.reply(404)
            parent, _, name = destination.rpartition("/")
            if parent.startswith(ALBUMS):  # Photos app names album entries <fileid>-<name>
                destination = f"{parent}/{nc.fileid(source)}-{name}"
            if destination in nc.files:
                return self.reply(412)
            if parent not in nc.dirs:
                return self.reply(409)
            nc.files[destination] = nc.files[source]
            self.reply(201)

        def do_MOVE(self):
            self.record()
            source, destination = self.path_(), self.dest_()
            if source not in nc.files:
                return self.reply(404)
            if destination.rpartition("/")[0] not in nc.dirs:
                return self.reply(409)
            if destination in nc.files:
                return self.reply(412)
            nc.files[destination] = nc.files.pop(source)
            nc.ids[destination] = nc.ids.pop(source)
            self.reply(201)

    return Handler


@pytest.fixture
def nextcloud():
    nc = FakeNextcloud()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(nc))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    nc.url = f"http://user:secret@127.0.0.1:{server.server_address[1]}/Photos"
    yield nc
    server.shutdown()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def make_jpeg(path, color, size=(8, 8), exif_original=None):
    image = Image.new("RGB", size, color)
    kwargs = {}
    if exif_original:
        exif = Image.Exif()
        exif[0x8769] = {36867: exif_original}
        kwargs["exif"] = exif
    image.save(path, "JPEG", **kwargs)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


# --------------------------------------------------------------------------
# Unit tests
# --------------------------------------------------------------------------
def test_filename_helpers():
    assert gp2nc.filename_contains_increment("a(1).jpg")
    assert not gp2nc.filename_contains_increment("a.jpg")
    assert gp2nc.filenames_without_increment_are_identical("a.jpg", "a(2).jpg")
    assert not gp2nc.filenames_without_increment_are_identical("a.jpg", "b(2).jpg")


def test_find_available_skips_existing_names():
    taken = {"IMG.jpg", "IMG(1).jpg"}
    assert gp2nc.find_available(lambda n: n in taken, "IMG(7).jpg") == "IMG(2).jpg"
    assert gp2nc.find_available(lambda n: False, "plain") == "plain"


def test_parse_multistatus_checksum_tokens():
    xml = (
        b'<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:response><d:href>/x/a%20b.jpg</d:href>'
        b"<d:propstat><d:prop><oc:checksums><oc:checksum>SHA1:aa MD5:BB</oc:checksum></oc:checksums>"
        b"<d:getcontentlength>3</d:getcontentlength></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        b"<d:propstat><d:prop><oc:fileid/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
        b"</d:response></d:multistatus>"
    )
    parsed = gp2nc.parse_multistatus(xml)
    props = parsed["/x/a%20b.jpg"]
    assert gp2nc.retrieve_remote_hash(None, "/x/a%20b.jpg", props) == "bb"
    assert "{http://owncloud.org/ns}fileid" not in props


def test_taken_time_priority(tmp_path):
    exif = tmp_path / "exif.jpg"
    make_jpeg(exif, "red", exif_original="2019:07:04 10:11:12")
    assert gp2nc.taken_time(str(exif)).strftime("%Y-%m-%d %H:%M:%S") == "2019-07-04 10:11:12"

    plain = tmp_path / "plain.jpg"
    make_jpeg(plain, "blue")
    os.utime(plain, (1519905600, 1519905600))
    assert gp2nc.taken_time(str(plain)).strftime("%Y-%m") == "2018-03"

    with_json = tmp_path / "pic.jpg"
    make_jpeg(with_json, "green", exif_original="2001:01:01 00:00:00")
    write_json(tmp_path / "pic.jpg.supplemental-metadata.json", {"photoTakenTime": {"timestamp": "1619870000"}})
    assert gp2nc.taken_time(str(with_json)).strftime("%Y-%m-%d") == "2021-05-01"  # JSON beats EXIF


def test_duplicate_numbered_file_uses_numbered_sidecar(tmp_path):
    make_jpeg(tmp_path / "IMG_1.jpg", "red")
    (tmp_path / "IMG_1(1).jpg").write_bytes((tmp_path / "IMG_1.jpg").read_bytes())
    write_json(tmp_path / "IMG_1.jpg.supplemental-metadata.json", {"photoTakenTime": {"timestamp": "1400000000"}})
    write_json(tmp_path / "IMG_1.jpg.supplemental-metadata(1).json", {"photoTakenTime": {"timestamp": "1500000000"}})
    assert gp2nc.metadata_timestamp(str(tmp_path / "IMG_1.jpg"), "x") == 1400000000
    # the numbered copy sees both sidecars; the plain glob does not match, the numbered one is appended
    assert gp2nc.metadata_timestamp(str(tmp_path / "IMG_1(1).jpg"), "x") == 1500000000


def test_load_config(monkeypatch):
    config = gp2nc.load_config(
        {"NEXTCLOUD_URL": "https://me:p%40ss@cloud.example.org:8443/Photos/", "NEXTCLOUD_PREFIX": "nextcloud"}
    )
    assert config.origin == "https://cloud.example.org:8443"
    assert (config.user, config.password) == ("me", "p@ss")
    assert config.upload_path == "/Photos"
    assert config.files_base_path == "/nextcloud/remote.php/dav/files/me"
    with pytest.raises(SystemExit):
        gp2nc.load_config({})


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------
def test_end_to_end_import_is_correct_and_idempotent(tmp_path, nextcloud):
    takeout = tmp_path / "Takeout" / "Google Photos"
    album = takeout / "Vacation"
    album.mkdir(parents=True)
    write_json(album / "metadata.json", {"title": "Vacation"})
    make_jpeg(album / "beach.jpg", "red", exif_original="2019:07:04 10:11:12")
    make_jpeg(album / "sunset.jpg", "orange", size=(9, 9))
    write_json(album / "sunset.jpg.supplemental-metadata.json", {"photoTakenTime": {"timestamp": "1619870000"}})
    make_jpeg(album / "old photo.jpg", "gray", size=(10, 10))
    os.utime(album / "old photo.jpg", (1519905600, 1519905600))
    make_jpeg(album / "holiday.jpg", "teal", size=(11, 11))

    # already on the server under a very different, numbered name
    nextcloud.add_dir(FILES + "/Photos/2017/02")
    nextcloud.files[FILES + "/Photos/2017/02/IMG_9999(1).jpg"] = (album / "holiday.jpg").read_bytes()

    config = gp2nc.load_config({"NEXTCLOUD_URL": nextcloud.url})
    gp2nc.run(config, str(takeout))

    base = FILES + "/Photos"
    assert base + "/2019/07/beach.jpg" in nextcloud.files
    assert base + "/2021/05/sunset.jpg" in nextcloud.files
    assert base + "/2018/03/old photo.jpg" in nextcloud.files  # space survives encoding
    assert base + "/2017/02/holiday.jpg" in nextcloud.files  # renamed
    assert base + "/2017/02/IMG_9999(1).jpg" not in nextcloud.files
    assert nextcloud.files[base + "/2019/07/beach.jpg"] == (album / "beach.jpg").read_bytes()

    headers = nextcloud.put_headers[base + "/2021/05/sunset.jpg"]
    assert headers["x-oc-ctime"] == "1619870000"

    assert ALBUMS + "/Vacation" in nextcloud.dirs
    entries = sorted(p.rpartition("/")[2].partition("-")[2] for p in nextcloud.files if p.startswith(ALBUMS + "/Vacation/"))
    assert entries == ["beach.jpg", "holiday.jpg", "old photo.jpg", "sunset.jpg"]
    assert not (album / "gp2nc-error.log").exists()

    # second run: nothing left to do
    before = {m: nextcloud.count(m) for m in ("PUT", "COPY", "MOVE")}
    gp2nc.run(config, str(takeout))
    assert {m: nextcloud.count(m) for m in ("PUT", "COPY", "MOVE")} == before


def test_missing_upload_folder_aborts_with_clear_message(tmp_path, nextcloud):
    takeout = tmp_path / "gp"
    folder = takeout / "Photos from 2020"
    folder.mkdir(parents=True)
    make_jpeg(folder / "a.jpg", "red", exif_original="2020:02:02 02:02:02")

    nextcloud.dirs.discard(FILES + "/Photos")  # target folder does not exist on the server
    config = gp2nc.load_config({"NEXTCLOUD_URL": nextcloud.url})
    with pytest.raises(SystemExit):
        gp2nc.run(config, str(takeout))
