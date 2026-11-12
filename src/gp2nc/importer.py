"""Import a Google Photos Takeout into Nextcloud.

Python port of https://github.com/rikmeijer/googlephotos2nextcloud (PHP).

Run it from (or point it at) the ``Takeout/Google Photos`` directory, i.e. the
directory that contains the album / "Photos from ..." folders and their JSON
sidecar files.

Configuration (environment variables or a ``.env`` file):

    NEXTCLOUD_URL       e.g. https://user:app-password@cloud.example.com/Photos
                        (the path part is the target folder inside Nextcloud)
    NEXTCLOUD_USER      optional, overrides the user in NEXTCLOUD_URL
    NEXTCLOUD_PASSWORD  optional, overrides the password in NEXTCLOUD_URL
    NEXTCLOUD_PREFIX    optional sub directory Nextcloud lives in, no leading
                        or trailing slash (e.g. "nextcloud")
    GP2NC_RETRY_DELAY   optional, seconds between retries (default 10)
    GP2NC_THREADS       optional, number of parallel workers (default 4), same as --threads
"""
from __future__ import annotations

import argparse
import dataclasses
import glob
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urlparse

import requests

try:  # optional: lets Pillow read HEIC/HEIF EXIF data
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional dependency
    pass

from PIL import Image

DAV = "{DAV:}"
OC = "{http://owncloud.org/ns}"
NC = "{http://nextcloud.org/ns}"

MEDIA_PROPERTIES = [
    DAV + "displayname",
    DAV + "getcontentlength",
    DAV + "getcontenttype",
    DAV + "resourcetype",
    OC + "checksums",
    NC + "creation_time",
    OC + "fileid",
]

RETRY_ATTEMPTS = 5


def rawurlencode(value: str) -> str:
    """Equivalent of PHP's rawurlencode()."""
    return quote(value, safe="")


# --------------------------------------------------------------------------
# IO
# --------------------------------------------------------------------------
_log_lock = threading.Lock()


def log(line: str) -> None:
    # one write under a lock, so lines from different workers never get mixed up
    with _log_lock:
        sys.stdout.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] - {line}\n")
        sys.stdout.flush()


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# WebDAV client (replacement for Sabre\DAV\Client + Attempt)
# --------------------------------------------------------------------------
class DavHttpError(Exception):
    def __init__(self, status: int, method: str, path: str):
        super().__init__(f"{method} {path} failed with HTTP status {status}")
        self.status = status
        self.method = method
        self.path = path


def _prop_value(element: ET.Element) -> Any:
    if len(element) == 0:
        return element.text or ""
    return [{"name": child.tag, "value": "".join(child.itertext())} for child in element]


def parse_multistatus(content: bytes) -> dict[str, dict[str, Any]]:
    """Parse a PROPFIND response into {href: {clark-name: value}} (200 props only)."""
    root = ET.fromstring(content)
    result: dict[str, dict[str, Any]] = {}
    for response in root.findall(DAV + "response"):
        href = (response.findtext(DAV + "href") or "").strip()
        if href.startswith(("http://", "https://")):
            href = urlparse(href).path
        props: dict[str, Any] = {}
        for propstat in response.findall(DAV + "propstat"):
            status_parts = (propstat.findtext(DAV + "status") or "").split()
            if len(status_parts) < 2 or status_parts[1] != "200":
                continue
            for prop in propstat.findall(DAV + "prop"):
                for child in prop:
                    props[child.tag] = _prop_value(child)
        result[href] = props
    return result


def build_propfind_body(properties: list[str]) -> bytes:
    parts = []
    for index, clark in enumerate(properties):
        namespace, _, local = clark[1:].partition("}")
        parts.append(f'<p{index}:{local} xmlns:p{index}="{namespace}"/>')
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:"><d:prop>' + "".join(parts) + "</d:prop></d:propfind>"
    )
    return body.encode("utf-8")


class DavClient:
    """Minimal WebDAV client. Transport errors are retried like PHP's Attempt class."""

    def __init__(self, origin: str, user: str | None, password: str | None):
        self.origin = origin.rstrip("/")
        self.user = user
        self.password = password
        self.retry_delay = float(os.environ.get("GP2NC_RETRY_DELAY", "10"))
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        """One session per thread: requests sessions are not guaranteed to be thread-safe,
        and pool threads live for the whole run, so their connections are reused."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            if self.user is not None:
                session.auth = (self.user, self.password or "")
            self._local.session = session
        return session

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return self.origin + path

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
        upload_from: str | None = None,
    ) -> requests.Response:
        attempts = 0
        while True:
            attempts += 1
            try:
                if upload_from is not None:
                    # re-open on every attempt so a retry starts at byte 0
                    with open(upload_from, "rb") as handle:
                        return self.session.request(
                            method, self.url(path), headers=headers, data=handle,
                            allow_redirects=False, timeout=(30, 600),
                        )
                return self.session.request(
                    method, self.url(path), headers=headers, data=data,
                    allow_redirects=False, timeout=(30, 600),
                )
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                if attempts >= RETRY_ATTEMPTS:
                    raise
                log(f"attempt failed, retrying in {self.retry_delay:g} seconds...")
                time.sleep(self.retry_delay)

    def propfind(self, path: str, properties: list[str], depth: int = 0) -> dict[str, dict[str, Any]]:
        response = self.request(
            "PROPFIND",
            path,
            headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
            data=build_propfind_body(properties),
        )
        if response.status_code != 207:
            raise DavHttpError(response.status_code, "PROPFIND", path)
        return parse_multistatus(response.content)


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------
def md5_file(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_from_tokens(text: str) -> str | None:
    # Nextcloud reports one string such as "SHA1:abc MD5:def ADLER32:123"
    for token in re.split(r"[\s,]+", text.strip()):
        algo, _, value = token.partition(":")
        if value and algo.lower() == "md5":
            return value.lower()
    return None


def hash_from_properties(properties: Mapping[str, Any]) -> str | None:
    """MD5 from the PROPFIND checksums property, no network needed."""
    checksums = properties.get(OC + "checksums")
    if isinstance(checksums, list):
        for checksum in checksums:
            found = _md5_from_tokens(checksum["value"])
            if found:
                return found
    elif isinstance(checksums, str) and checksums:
        return _md5_from_tokens(checksums)
    return None


def hash_via_request(dav: DavClient, file_path: str) -> str | None:
    """Ask the server to (re)calculate the MD5 and read it back."""
    dav.request("PATCH", file_path, headers={"X-Recalculate-Hash": "md5"})
    response = dav.request("HEAD", file_path, headers={"X-Hash": "md5"})
    return _md5_from_tokens(response.headers.get("oc-checksum", ""))


def retrieve_remote_hash(dav: DavClient, file_path: str, properties: Mapping[str, Any]) -> str | None:
    return hash_from_properties(properties) or hash_via_request(dav, file_path)


# --------------------------------------------------------------------------
# Metadata (taken time of a photo / video)
# --------------------------------------------------------------------------
EXIF_DATE_SOURCES = ["exif:DateTimeOriginal", "exif:DateTime", "exif:DateTimeDigitized"]
EXIF_FORMATS = [
    ("%Y-%m-%d_%H:%M:%S", re.compile(r"^\d+-\d+-\d+_\d+:\d+:\d+$")),
    ("%Y:%m:%d %H:%M:%S", re.compile(r"^\d+:\d+:\d+ \d+:\d+:\d+$")),
    ("%Y:%m:%d %H:%M:%S.%f", re.compile(r"^\d+:\d+:\d+ \d+:\d+:\d+\.\d+$")),
]
MOTION_VIDEO_EXTENSIONS = ["MP4", "MOV"]
COMPANION_IMAGE_EXTENSIONS = ["JPG", "JPEG", "HEIC", "HEIF"]

_taken_cache: dict[str, datetime] = {}


def utc_from_timestamp(timestamp: int | float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _glob(pattern_prefix: str, suffix: str = "") -> list[str]:
    """glob(escape(prefix) + suffix), sorted like PHP's glob()."""
    return sorted(glob.glob(glob.escape(pattern_prefix) + suffix))


def metadata_timestamp(path: str, name_for_logging: str) -> int | None:
    def say(message: str) -> None:
        log(f"[{name_for_logging}] - {message}")

    directory, filename = os.path.split(path)
    stem, dot_ext = os.path.splitext(filename)
    ext = dot_ext[1:]
    basename = f"{directory or '.'}/{stem}"

    options = _glob(path, "*.json")
    match = re.search(r"([^(]+)(\(\d+\))$", basename)
    if match:
        prefix, increment = match.group(1), match.group(2)
        original_filename = prefix.rstrip() + "." + ext

        def sidecar_glob() -> list[str]:
            return sorted(glob.glob(glob.escape(prefix + "." + ext) + "*" + glob.escape(increment) + ".json"))

        if not os.path.exists(original_filename):
            say(f"Possible duplicate of `{os.path.basename(original_filename)}` in filename, but original file missing")
        elif os.path.getsize(path) == os.path.getsize(original_filename):
            say(
                f"Duplicate of `{os.path.basename(original_filename)}`, try to find additional "
                f"metadata files: {original_filename}.*{increment}.json"
            )
            options = options + sidecar_glob()
        else:
            say(f"Possible duplicate of `{os.path.basename(original_filename)}` in filename, but filesize mismatch")
            options = sidecar_glob()
    else:
        # `IMG_1.jpg*.json` also matches `IMG_1.jpg.supplemental-metadata(1).json`, which belongs to the
        # numbered duplicate `IMG_1(1).jpg` and sorts first. (The PHP original picks it up here.)
        own = [option for option in options if not re.search(r"\(\d+\)\.json$", option)]
        options = own or options

    if not options and ext.lower() == "mp4":
        # Live Photo: the MP4 motion clip shares metadata with its HEIC/JPG companion
        for companion_ext in ["HEIC", "heic", "JPG", "jpg", "JPEG", "jpeg"]:
            companion_options = _glob(basename + "." + companion_ext, "*.json")
            if companion_options:
                options = companion_options
                break

    if not options:
        say("No metadata files found")
        return None

    for option in options:
        if not os.path.isfile(option):
            continue
        photo_metadata = read_json(option)
        if not isinstance(photo_metadata, dict):
            continue
        if photo_metadata.get("photoTakenTime") is not None:
            say("Found `photoTakenTime` in metadata")
            return int(photo_metadata["photoTakenTime"]["timestamp"])
        if photo_metadata.get("creationTime") is not None:
            say("Found `creationTime` in metadata")
            return int(photo_metadata["creationTime"]["timestamp"])
        say("Found no datetime in metadata")
    return None


def taken_time_from_companion_image(video_path: str, video_filename: str) -> datetime | None:
    directory, filename = os.path.split(video_path)
    stem = os.path.splitext(filename)[0]
    companions: list[str] = []
    for companion_ext in COMPANION_IMAGE_EXTENSIONS:
        for ext_option in (companion_ext, companion_ext.lower()):
            candidate = f"{directory or '.'}/{stem}.{ext_option}"
            if os.path.isfile(candidate) and candidate not in companions:
                companions.append(candidate)

    for companion in companions:
        timestamp = metadata_timestamp(companion, video_filename)
        if timestamp is None:
            continue
        log(f"[{video_filename}] - Using metadata from companion image `{os.path.basename(companion)}`")
        return utc_from_timestamp(timestamp)
    return None


def _read_exif_dates(path: str) -> dict[str, str] | None:
    """EXIF date strings, {} if the image has none, None if it is not a readable image."""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            try:
                exif_ifd = exif.get_ifd(0x8769)
            except Exception:
                exif_ifd = {}
            raw = {
                "exif:DateTimeOriginal": exif_ifd.get(36867),
                "exif:DateTime": exif.get(306),
                "exif:DateTimeDigitized": exif_ifd.get(36868),
            }
    except Exception:
        return None

    dates: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, bytes):
            value = value.decode("ascii", errors="ignore")
        if value:
            dates[key] = str(value).strip("\x00 \t\r\n")
    return dates


def _ffprobe_creation_time(path: str) -> datetime | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_entries", "format_tags=creation_time:stream_tags=creation_time", "-i", path],
            capture_output=True, text=True, timeout=60, check=True,
        )
        info = json.loads(completed.stdout or "{}")
    except Exception:
        return None

    candidates = [(info.get("format") or {}).get("tags", {}).get("creation_time")]
    candidates += [(stream.get("tags") or {}).get("creation_time") for stream in info.get("streams", [])]
    for value in candidates:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.year >= 1990:  # QuickTime "unset" dates are 1904 / 1970
            return parsed
    return None


def _taken_time_from_file_contents(photo_path: str, photo_filename: str) -> datetime | None:
    exif = _read_exif_dates(photo_path)
    if exif is None:
        # not an image Pillow can read (e.g. a video): ask ffprobe, if available
        created = _ffprobe_creation_time(photo_path)
        if created is not None:
            log(f"[{photo_filename}] - Found `{created.isoformat()}` in media container metadata")
        return created

    for source in EXIF_DATE_SOURCES:
        value = exif.get(source)
        if value is None:
            continue
        for fmt, regex in EXIF_FORMATS:
            if regex.match(value):
                try:
                    parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    break  # e.g. "0000:00:00 00:00:00", try the next source
                log(f"[{photo_filename}] - Found `{value}` in {source}")
                return parsed
    return None


def taken_time(photo_path: str) -> datetime:
    cached = _taken_cache.get(photo_path)
    if cached is not None:
        return cached

    photo_filename = os.path.basename(photo_path)
    timestamp = metadata_timestamp(photo_path, photo_filename)
    if timestamp is not None:
        result = utc_from_timestamp(timestamp)
    else:
        result = None
        extension = os.path.splitext(photo_path)[1][1:].upper()
        if extension in MOTION_VIDEO_EXTENSIONS:
            result = taken_time_from_companion_image(photo_path, photo_filename)
        if result is None:
            result = _taken_time_from_file_contents(photo_path, photo_filename)
        if result is None:
            log(f"[{photo_filename}] - Found no datetime in exif data nor metadata, falling back to filemtime")
            result = utc_from_timestamp(int(os.path.getmtime(photo_path)))

    _taken_cache[photo_path] = result
    return result


# --------------------------------------------------------------------------
# Remote directories / files
# --------------------------------------------------------------------------
class RemoteDirectories:
    def __init__(self, dav: DavClient):
        self.dav = dav
        self._cache: set[str] = set()
        # Without this, two workers creating the same /YYYY/MM would both see a 404, both MKCOL,
        # and the loser would fail. Only the first creation of a folder ever waits on this lock.
        self._lock = threading.Lock()

    def _mkdir(self, remote_base: str, remote_path: str) -> None:
        head = self.dav.request("HEAD", remote_base + remote_path)
        if head.status_code != 404:
            log(f'Directory "{remote_path}" already exists remotely')
            return
        response = self.dav.request("MKCOL", remote_base + remote_path)
        if response.status_code < 200 or response.status_code > 399:
            raise RuntimeError(f'Failed creating "{remote_path}" remotely, status {response.status_code}')
        log(f'Directory "{remote_path}" created remotely')

    def create(self, remote_base: str, remote_path: str) -> str:
        key = remote_base + remote_path
        if key in self._cache:
            return key
        with self._lock:
            if key in self._cache:  # another worker created it while we waited
                return key
            creating = ""
            for part in remote_path.lstrip("/").split("/"):
                creating += "/" + part
                self._mkdir(remote_base, creating)
            self._cache.add(key)
        return key


def upload(dav: DavClient, source: str, target: str) -> None:
    local_size = os.path.getsize(source)
    mtime = int(os.path.getmtime(source))
    ctime = int(taken_time(source).timestamp())  # resolve BEFORE streaming the file

    response = dav.request(
        "PUT",
        target,
        headers={"X-OC-MTime": str(mtime), "X-OC-CTime": str(ctime), "OC-Total-Length": str(local_size)},
        upload_from=source,
    )
    if response.status_code < 200 or response.status_code > 399:
        raise RuntimeError(f"Upload failed, invalid response code ({response.status_code}) received")

    head = dav.request("HEAD", target)
    if head.status_code != 200:
        raise RuntimeError(f"Could not validate remote file, invalid response code ({head.status_code}) received")
    if "content-length" not in head.headers:
        raise RuntimeError("Could not validate remote file, missing content-length headers")
    if local_size != int(head.headers["content-length"]):
        raise RuntimeError("Could not validate remote file, differ from local size")


def move_remote(dav: DavClient, file_path: str, destination: str, _retry: bool = True) -> bool:
    result = dav.request("MOVE", file_path, headers={"Destination": destination, "Overwrite": "F"})
    status = result.status_code
    if status == 409:
        log("destination is missing")
        mkcol = dav.request("MKCOL", posixpath.dirname(destination))
        if mkcol.status_code == 201 and _retry:
            return move_remote(dav, file_path, destination, _retry=False)
        return False
    if status == 412:
        log("destination already exists")
        return False
    if status == 415:
        log("destination is not a collection")
        return False
    if status in (201, 204):
        return True
    log(str(status))
    return False


def remote_exists(dav: DavClient, directory: str, filename: str) -> bool:
    try:
        dav.propfind(directory + "/" + rawurlencode(filename), [DAV + "displayname"], 0)
        return True
    except DavHttpError as error:
        if error.status == 404:
            return False
        raise


def _join_name(stem: str, ext: str) -> str:
    return f"{stem}.{ext}" if ext else stem


def find_available(exists: Callable[[str], bool], orig_filename: str) -> str:
    stem, dot, ext = orig_filename.rpartition(".")
    if not dot:
        stem, ext = orig_filename, ""
    match = re.search(r"\(\d+\)$", stem)
    if match:
        stem = stem[: match.start()]

    available = _join_name(stem, ext)
    tries = 0
    while True:
        log(f"Trying {available}")
        if not exists(available):
            return available
        tries += 1
        available = _join_name(f"{stem}({tries})", ext)


# --------------------------------------------------------------------------
# Walking a local directory
# --------------------------------------------------------------------------
INCREMENT = re.compile(r"\([\w\s]+\)")


def filename_contains_increment(filename: str) -> bool:
    return INCREMENT.search(filename) is not None


def filenames_without_increment_are_identical(first: str, second: str) -> bool:
    return INCREMENT.sub("", first) == INCREMENT.sub("", second)


def _normalize(path: str) -> str:
    return unquote(path).rstrip("/")


class DirectoryTask:
    def __init__(
        self,
        dav: DavClient,
        files_base_path: str,
        albums_base_path: str,
        user_albums: list[str],
        remote_files: dict[str, dict[str, dict[str, Any]]],
        executor: ThreadPoolExecutor,
    ):
        self.dav = dav
        self.files_base_path = files_base_path
        self.albums_base_path = albums_base_path
        self.user_albums = set(user_albums)
        self.remote_files = remote_files
        self.executor = executor
        self.directories = RemoteDirectories(dav)
        self._hash_locks: dict[str, threading.Lock] = {}
        self._hash_locks_guard = threading.Lock()

    def _hash_lock(self, content_hash: str) -> threading.Lock:
        with self._hash_locks_guard:
            return self._hash_locks.setdefault(content_hash, threading.Lock())

    @staticmethod
    def store_exception(path: str, error: BaseException) -> str:
        trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        with open(os.path.join(path, "gp2nc-error.log"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(["Failed processing directory: " + str(error), trace]))
        return "failed: " + str(error)

    def _copy_to_album(self, source: str, album_path: str, filename: str, debug: Callable[[str], None]) -> None:
        response = self.dav.request(
            "COPY", source, headers={"Destination": album_path + "/" + rawurlencode(filename)}
        )
        if response.status_code >= 400:
            debug(f"Copy to album failed with status {response.status_code}")

    def __call__(self, origin_path: str) -> str:
        directory_name = os.path.basename(origin_path)
        album_path = (
            self.albums_base_path + "/" + rawurlencode(directory_name)
            if directory_name in self.user_albums
            else None
        )

        if os.path.isfile(os.path.join(origin_path, "gp2nc-error.log")):
            return "skip to prevent recurring crashes, resolve errors first and delete gp2nc-error.log"

        files = [p for p in _glob(origin_path, "/*") if os.path.isfile(p)]
        log(f'Found "{directory_name}", containing {len(files)} files')

        try:
            album_photos: set[str] = set()
            if album_path is not None:
                listing = self.dav.propfind(album_path, [DAV + "displayname"], 1)
                album_photos = {_normalize(href) for href in listing}
                log(f'Found album "{directory_name}", containing {len(listing)} photos')

            json_files = [p for p in files if p.endswith(".json")]
            metadata_jsons = [p for p in json_files if p.endswith(("metadata.json", "Metadaten.json"))]
            photo_files = [p for p in files if not p.endswith(".json")]
            no_photos = len(photo_files)
            if not metadata_jsons:
                log("No metadata found")
            log(f"Found {no_photos} photo files")

            self._process_all(photo_files, directory_name, album_path, album_photos)
        except Exception as error:  # noqa: BLE001 - mirrors the PHP "log and skip directory" behaviour
            return self.store_exception(origin_path, error)
        return "done"

    def _process_all(
        self,
        photo_files: list[str],
        directory_name: str,
        album_path: str | None,
        album_photos: set[str],
    ) -> None:
        """Process the photos of one directory on the worker pool.

        Like the PHP version, the first failure aborts the directory: photos that have not
        started yet are skipped, photos already in flight finish, then the error is raised.
        """
        no_photos = len(photo_files)
        stop = threading.Event()
        errors: list[BaseException] = []

        def work(index: int, photo_path: str) -> None:
            if stop.is_set():
                return
            try:
                self._process_photo(photo_path, index, no_photos, directory_name, album_path, album_photos)
            except Exception as error:  # noqa: BLE001 - collected and re-raised below
                errors.append(error)
                stop.set()

        futures = [self.executor.submit(work, index, path) for index, path in enumerate(photo_files)]
        try:
            for future in futures:
                future.result()
        except BaseException:  # e.g. Ctrl-C: do not keep uploading the queued files
            stop.set()
            raise
        if errors:
            raise errors[0]

    def _process_photo(
        self,
        photo_path: str,
        photo_index: int,
        no_photos: int,
        directory_name: str,
        album_path: str | None,
        album_photos: set[str],
    ) -> None:
        local_filename = os.path.basename(photo_path)

        def debug(message: str) -> None:
            log(f"[{local_filename}] - {message}")

        local_hash = md5_file(photo_path)
        debug(f"Photo {photo_index} of {no_photos}")

        duplicates = self.remote_files.get(local_hash)
        if duplicates:
            debug("Same file already exists remotely as: ")
            for remote_path, remote_props in list(duplicates.items()):
                debug("-> " + remote_path)
                remote_filename = posixpath.basename(unquote(remote_path))

                if local_filename == remote_filename:
                    debug("Identical filename, already uploaded")
                elif filename_contains_increment(local_filename):
                    debug("Local file name contains file incrementer, e.g.: (1)")
                elif not filename_contains_increment(remote_filename):
                    debug("Remote file name does not contain file incrementer, e.g.: (1)")
                elif filenames_without_increment_are_identical(local_filename, remote_filename):
                    debug(
                        "Locally there is no incrementer, remotely there is. "
                        "Without the incrementer, files are identical"
                    )
                else:
                    remote_directory = posixpath.dirname(remote_path)
                    available = find_available(
                        lambda name: remote_exists(self.dav, remote_directory, name), local_filename
                    )
                    new_path = remote_directory + "/" + rawurlencode(available)
                    if move_remote(self.dav, remote_path, new_path):
                        debug(
                            f"Remote has a very different filename, but contents are identical. "
                            f"Renamed remote file ({remote_filename}-->{available})"
                        )
                        duplicates[new_path] = duplicates.pop(remote_path)
                        remote_path, remote_filename = new_path, available

                if album_path is not None:
                    fileid = remote_props.get(OC + "fileid", "")
                    identifier = _normalize(album_path) + "/" + f"{fileid}-{remote_filename}"
                    debug("Photo must be in album " + album_path)
                    if identifier not in album_photos:
                        self._copy_to_album(remote_path, album_path, remote_filename, debug)
                        album_photos.add(identifier)
                        debug("Copied")
            return

        taken = taken_time(photo_path)
        debug(f"Photo or video taken @ {taken:%Y-%m-%d %H:%M:%S}")

        directory_remote_path = self.directories.create(self.files_base_path, taken.strftime("/%Y/%m"))
        photo_remote_path = directory_remote_path + "/" + rawurlencode(local_filename)

        upload(self.dav, photo_path, photo_remote_path)
        debug(f'Succesfully uploaded to "{photo_remote_path.replace(self.files_base_path, "")}"')

        if album_path is not None:
            debug(f'Copying to album "{directory_name}"')
            self._copy_to_album(photo_remote_path, album_path, local_filename, debug)


# --------------------------------------------------------------------------
# Configuration + main flow
# --------------------------------------------------------------------------
@dataclass
class Config:
    origin: str
    dav_prefix: str
    user: str
    password: str | None
    upload_path: str
    threads: int = 4

    @property
    def files_base_path(self) -> str:
        return f"/{self.dav_prefix}/files/{quote(self.user, safe='@')}"

    @property
    def albums_base_path(self) -> str:
        return f"/{self.dav_prefix}/photos/{quote(self.user, safe='@')}/albums"


def load_config(env: Mapping[str, str]) -> Config:
    url = env.get("NEXTCLOUD_URL")
    if not url:
        raise SystemExit("Nextcloud URL missing, please set in .env or as environment variable")
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit(f"NEXTCLOUD_URL is not a valid URL: {url!r}")

    origin = f"{parsed.scheme}://{parsed.netloc.rpartition('@')[2]}"
    log(f"Working on {origin}/")

    prefix = env.get("NEXTCLOUD_PREFIX", "").strip("/")
    if prefix:
        log(f"Path prefix {prefix}")
        dav_prefix = f"{prefix}/remote.php/dav"
    else:
        dav_prefix = "remote.php/dav"

    user = env.get("NEXTCLOUD_USER") or (unquote(parsed.username) if parsed.username else None)
    if not user:
        raise SystemExit("No Nextcloud user: put it in NEXTCLOUD_URL or set NEXTCLOUD_USER")
    log(f"Identifing as {user}")

    password = env.get("NEXTCLOUD_PASSWORD") or (unquote(parsed.password) if parsed.password else None)
    log("Identifing with a password." if password is not None else "Not using a password.")

    threads = int(env.get("GP2NC_THREADS", "4"))
    if threads < 1:
        raise SystemExit("GP2NC_THREADS must be at least 1")

    return Config(origin, dav_prefix, user, password, parsed.path.rstrip("/"), threads)


def run(config: Config, working_directory: str) -> None:
    log(f"Working from {working_directory}")
    dav = DavClient(config.origin, config.user, config.password)

    user_albums: list[str] = []
    metadata_files = _glob(working_directory, "/*/metadata.json") + _glob(working_directory, "/*/Metadaten.json")
    for metadata_file in metadata_files:
        album_dir_name = os.path.basename(os.path.dirname(metadata_file))
        metadata = read_json(metadata_file)
        title = metadata.get("title") if isinstance(metadata, dict) else None
        if title is None:
            continue
        if str(title) not in ("", "0"):
            user_albums.append(str(title).replace("/", "-"))
        else:
            user_albums.append(album_dir_name)
    log(f"Found {len(user_albums)} user albums")

    files_base_path = config.files_base_path
    albums_base_path = config.albums_base_path

    log("Retrieving remote albums...")
    try:
        album_resources = dav.propfind(albums_base_path, [DAV + "displayname", DAV + "getcontentlength"], 1)
    except DavHttpError as error:
        raise SystemExit(
            f"Could not list albums ({error}). Is the Nextcloud Photos app installed, "
            "and are NEXTCLOUD_URL / NEXTCLOUD_PREFIX / credentials correct?"
        )
    available_albums = {unquote(href.rstrip("/").rpartition("/")[2]) for href in album_resources}
    log(f"Found {len(available_albums)} available albums")

    creatable: list[str] = []
    for album in user_albums:
        if album not in available_albums and album not in creatable:
            creatable.append(album)
    log(f"Creating {len(creatable)} albums")
    for album in creatable:
        log("--> " + rawurlencode(album))
        response = dav.request("MKCOL", albums_base_path + "/" + rawurlencode(album))
        if response.status_code >= 400:
            log(f"Creating album failed with status {response.status_code}")

    log("Retrieving remote files...")
    upload_root = files_base_path + config.upload_path
    try:
        listing = dav.propfind(upload_root, MEDIA_PROPERTIES, 3)
    except DavHttpError as error:
        raise SystemExit(f"Could not list the upload folder ({error}). Does {config.upload_path or '/'} exist in Nextcloud?")

    remote_files: dict[str, dict[str, dict[str, Any]]] = {}
    for file_path, properties in listing.items():
        if DAV + "getcontentlength" not in properties:
            continue
        file_hash = retrieve_remote_hash(dav, file_path, properties)
        if file_hash is None:
            log(f"Could not determine a hash for {file_path}, ignoring it")
            continue
        remote_files.setdefault(file_hash, {})[file_path] = properties
    log(f"Found {len(remote_files)} files remotely")

    log("Walking directories...")
    with ThreadPoolExecutor(max_workers=config.threads) as executor:
        task = DirectoryTask(dav, upload_root, albums_base_path, user_albums, remote_files, executor)
        for path in _glob(working_directory, "/*"):
            if not os.path.isdir(path):
                continue
            result = task(path)
            log(f'"{os.path.basename(path)}": {result}')
    log("Done!")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a Google Photos Takeout into Nextcloud.")
    parser.add_argument(
        "takeout_dir", nargs="?", default=".",
        help="the 'Takeout/Google Photos' directory (default: current directory)",
    )
    parser.add_argument("--threads", type=int, help="number of parallel workers (default: GP2NC_THREADS or 4)")
    args = parser.parse_args(argv)
    working_directory = os.path.abspath(args.takeout_dir)

    try:
        from dotenv import load_dotenv

        # .env in the Takeout directory, else in the directory gp2nc is started from
        env_dir = working_directory if os.path.isfile(os.path.join(working_directory, ".env")) else os.getcwd()
        load_dotenv(os.path.join(env_dir, ".env"), override=False)  # real env vars win, like createImmutable
    except ImportError:
        pass

    config = load_config(os.environ)
    if args.threads is not None:
        if args.threads < 1:
            parser.error("--threads must be at least 1")
        config = dataclasses.replace(config, threads=args.threads)
    run(config, working_directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
