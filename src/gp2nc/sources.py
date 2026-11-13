"""Where a Takeout lives: a directory on disk, or a Takeout .zip read in place (never unpacked).

Paths handed to a source are POSIX style. For a directory they are ordinary paths; for a zip
they are entry names inside the archive (``Takeout/Google Photos/Vacation/IMG_1.jpg``).
"""

from __future__ import annotations

import calendar
import contextlib
import os
import posixpath
import shutil
import tempfile
import threading
import unicodedata
import zipfile
from collections.abc import Iterator
from typing import Any, BinaryIO

ERROR_LOG_NAME = "gp2nc-error.log"
# Name of the "Google Photos" folder in a Takeout export, per the language of the Google account
# (from simulot/TakeoutLocalization). Matched case-sensitively after Unicode NFC normalisation.
PHOTOS_FOLDER_NAMES = [
    "Google Photos",    # en, fr
    "Google Fotos",     # de
    "Google Foto",      # it
    "Fotky Google",     # sk
    "Google Фото",      # ru
    "Google Снимки",    # bg
]


class Source:
    """Read-only view of a Takeout folder tree."""

    identity: str

    def listdir(self, path: str) -> list[str]:
        """Names of the direct children of ``path`` (no paths), empty if it does not exist."""
        raise NotImplementedError

    def isdir(self, path: str) -> bool:
        raise NotImplementedError

    def isfile(self, path: str) -> bool:
        raise NotImplementedError

    def size(self, path: str) -> int:
        raise NotImplementedError

    def mtime(self, path: str) -> float:
        raise NotImplementedError

    def open(self, path: str) -> contextlib.AbstractContextManager[Any]:
        """Binary, seekable stream of the file. Supports ``len()`` (its size) for HTTP clients."""
        raise NotImplementedError

    def local_copy(self, path: str) -> contextlib.AbstractContextManager[str]:
        """A path on the local disk holding the file's content, for tools that need one (ffprobe)."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> Source:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class DirectorySource(Source):
    identity = "directory"

    def listdir(self, path: str) -> list[str]:
        try:
            return os.listdir(path or ".")
        except OSError:
            return []

    def isdir(self, path: str) -> bool:
        return os.path.isdir(path or ".")

    def isfile(self, path: str) -> bool:
        return os.path.isfile(path)

    def size(self, path: str) -> int:
        return os.path.getsize(path)

    def mtime(self, path: str) -> float:
        return os.path.getmtime(path)

    @contextlib.contextmanager
    def open(self, path: str) -> Iterator[BinaryIO]:
        with open(path, "rb") as handle:
            yield handle

    @contextlib.contextmanager
    def local_copy(self, path: str) -> Iterator[str]:
        yield path


class _SizedReader:
    """Wraps a zip entry stream and reports its size up front.

    ``requests`` sends a ``Content-Length`` for objects with ``__len__`` without reading ahead,
    so uploading from an archive does not have to decompress the file twice. Everything else
    (read, seek, tell) is passed through to the stream.
    """

    def __init__(self, stream: BinaryIO, size: int):
        self._stream = stream
        self._size = size

    def __len__(self) -> int:
        return self._size

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class ZipSource(Source):
    """A Takeout .zip, read through its central directory. Nothing is extracted to disk."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.identity = "zip:" + self.path
        self._files: dict[str, zipfile.ZipInfo] = {}
        # directory -> {child name: is it a directory}; directories that only exist implicitly count too
        self._children: dict[str, dict[str, bool]] = {"": {}}
        with zipfile.ZipFile(self.path) as archive:
            for info in archive.infolist():
                parts = info.filename.strip("/").split("/")
                if parts == [""]:
                    continue
                is_dir = info.is_dir()
                for depth, name in enumerate(parts):
                    parent = "/".join(parts[:depth])
                    child_is_dir = depth < len(parts) - 1 or is_dir
                    children = self._children.setdefault(parent, {})
                    children[name] = children.get(name, False) or child_is_dir
                if not is_dir:
                    self._files["/".join(parts)] = info
        # zipfile handles are not safe to share between threads: each thread opens its own
        self._local = threading.local()
        self._archives: list[zipfile.ZipFile] = []
        self._archives_lock = threading.Lock()

    @staticmethod
    def _key(path: str) -> str:
        key = posixpath.normpath(path).strip("/") if path else ""
        return "" if key == "." else key

    def listdir(self, path: str) -> list[str]:
        return sorted(self._children.get(self._key(path), {}))

    def isdir(self, path: str) -> bool:
        return self._key(path) in self._children

    def isfile(self, path: str) -> bool:
        return self._key(path) in self._files

    def size(self, path: str) -> int:
        return self._files[self._key(path)].file_size

    def mtime(self, path: str) -> float:
        # Zip entries store local time without a zone; read them as UTC like other untimed dates
        return calendar.timegm(self._files[self._key(path)].date_time)

    def _archive(self) -> zipfile.ZipFile:
        archive = getattr(self._local, "archive", None)
        if archive is None:
            archive = zipfile.ZipFile(self.path)
            self._local.archive = archive
            with self._archives_lock:
                self._archives.append(archive)
        return archive

    @contextlib.contextmanager
    def open(self, path: str) -> Iterator[_SizedReader]:
        info = self._files[self._key(path)]
        with self._archive().open(info) as stream:
            yield _SizedReader(stream, info.file_size)

    @contextlib.contextmanager
    def local_copy(self, path: str) -> Iterator[str]:
        fd, copy = tempfile.mkstemp(suffix=posixpath.splitext(path)[1])
        try:
            with os.fdopen(fd, "wb") as out, self.open(path) as stream:
                shutil.copyfileobj(stream, out)
            yield copy
        finally:
            os.remove(copy)

    def close(self) -> None:
        with self._archives_lock:
            for archive in self._archives:
                archive.close()
            self._archives.clear()


def _find_photos_folder(source: Source) -> str:
    """The localized "Google Photos" folder inside a Takeout archive, or "" for the archive root."""
    wanted = {unicodedata.normalize("NFC", name) for name in PHOTOS_FOLDER_NAMES}
    for parent in ("Takeout", ""):
        for name in source.listdir(parent):
            if unicodedata.normalize("NFC", name) in wanted and source.isdir(posixpath.join(parent, name)):
                return posixpath.join(parent, name)
    return ""


def open_takeout(path: str, zip_folder: str | None = None) -> tuple[Source, str]:
    """The source for a Takeout directory or .zip, and the folder inside it that holds the albums.

    For a zip, ``zip_folder`` names that folder; by default the localized "Google Photos" folder
    (see PHOTOS_FOLDER_NAMES) is used if the archive has one, otherwise the archive root.
    The caller closes the returned source.
    """
    if os.path.isfile(path):
        source = ZipSource(path)
        if zip_folder is not None:
            root = source._key(zip_folder)
            if not source.isdir(root):
                source.close()
                raise SystemExit(f"{zip_folder!r} is not a folder inside {path}")
            return source, root
        return source, _find_photos_folder(source)
    return DirectorySource(), posixpath.normpath(os.path.abspath(path).replace(os.sep, "/"))


DIRECTORY = DirectorySource()
