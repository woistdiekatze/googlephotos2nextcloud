# Google Photos to Nextcloud (Python)

Python port of [rikmeijer/googlephotos2nextcloud](https://github.com/rikmeijer/googlephotos2nextcloud) (PHP, MIT).

Imports a Google Photos Takeout into Nextcloud over WebDAV:

- uploads every photo/video to `<target>/<YYYY>/<MM>/` based on when it was taken
  (Takeout JSON sidecar → companion image for Live Photo videos → EXIF → `ffprobe` creation time → file mtime)
- skips files that already exist on the server (matched by MD5, so re-runs are safe)
- recreates your albums in the Nextcloud Photos app and adds the photos to them
- renames already-uploaded duplicates such as `IMG_1234(1).jpg` when the same content is imported under its real name

Requires the Nextcloud **Photos** app (for albums).

## Setup

The project is managed with [uv](https://docs.astral.sh/uv/). Install the dependencies (and create `.venv`) with:

```
uv sync                # core dependencies
uv sync --extra heif   # also install pillow-heif, to read HEIC EXIF data
```

Python 3.11+ is supported (older versions are end-of-life); uv can fetch an interpreter if none is installed. `ffprobe` (part of ffmpeg) is optional
and only used to read the creation date of videos.

## Configuration

Environment variables, or a `.env` file in the Takeout directory (or in the directory `gp2nc` is started from).
Real environment variables win over `.env`.

```
NEXTCLOUD_URL=https://<USER>:<APP_PASSWORD>@nextcloud.example.org/Photos
NEXTCLOUD_USER=usernamehere        # optional, overrides the user in NEXTCLOUD_URL
NEXTCLOUD_PASSWORD=secret          # optional, overrides the password in NEXTCLOUD_URL
NEXTCLOUD_PREFIX=nextcloud         # optional, Nextcloud in a sub dir; no leading/trailing /
GP2NC_RETRY_DELAY=10               # optional, seconds between retries of failed requests
GP2NC_THREADS=4                    # optional, parallel workers; same as --threads
```

The path of `NEXTCLOUD_URL` (`/Photos` above) is the folder in Nextcloud the photos are imported to. It must exist.
Use an app password (Settings → Security) instead of your account password.

## Run

From the `Takeout/Google Photos` directory (where the album folders and JSON files are):

```
uv run --project /path/to/gp2nc gp2nc
```

or point it at the directory:

```
uv run --project /path/to/gp2nc gp2nc "/path/to/Takeout/Google Photos"
```

Use `--threads N` to set the number of parallel uploads. `python -m gp2nc` works too, once the package is installed.

### Takeout zip files

You don't have to unpack the Takeout. Pass the `.zip` files directly; they are read in place:

```
uv run --project /path/to/gp2nc gp2nc takeout-001.zip takeout-002.zip
```

- Google's Takeout splits albums over several zips. Pass them all; they are processed one after another, and the
  duplicate check means a photo that appears in two zips is uploaded once.
- Inside each zip the albums are taken from `Takeout/Google Photos` if the archive has that folder, otherwise from
  the archive root. Use `--zip-folder "Some/Folder"` to pick another folder inside the archive.
- Zip entries store their date without a time zone; it is read as UTC, the same as EXIF dates without a zone.
- A directory that fails gets its `gp2nc-error.log` in `gp2nc-errors/<zip name>/<album>/` next to the zip, since
  a zip cannot be written to. Fix the cause, delete that log and run again.

### Docker

```
docker build . -t gp2nc
docker run --rm -v "/path/to/Takeouts":/photos -e NEXTCLOUD_URL=<NEXTCLOUD_URL> gp2nc takeout-001.zip takeout-002.zip
```

Mount the folder that holds the zips (or the `Takeout/Google Photos` directory) at `/photos` and name the inputs
relative to it. Without arguments the image imports the directory mounted at `/photos`.

The image installs the locked dependencies (including `pillow-heif`) with `uv sync --locked`.

## Project layout

```
pyproject.toml          project metadata, dependencies, console script
uv.lock                 locked dependency versions (commit this)
.python-version         Python version used for development
src/gp2nc/importer.py   the import logic and command line entry point
src/gp2nc/sources.py    reads a Takeout directory or a .zip in place
src/gp2nc/__main__.py   enables `python -m gp2nc`
tests/test_gp2nc.py     unit and end-to-end tests
Dockerfile
```

## Tests

```
uv run pytest
```

`uv run` installs the dev group (pytest) automatically.

The suite includes an end-to-end run against a small fake Nextcloud WebDAV server (upload, album creation,
duplicate rename, idempotent second run).

## Differences from the PHP version

Behavior is the same, except for these deliberate changes:

- **Pillow instead of Imagick** for EXIF. Only the header is read, so the "cache resources exhausted" error of the
  PHP version cannot happen. Unreadable files fall back to `ffprobe`, then to the file mtime, instead of failing
  the whole directory. EXIF times without a timezone are treated as UTC (as in the PHP Docker image).
- **Remote MD5 parsing.** Nextcloud reports checksums as one string (`SHA1:… MD5:… ADLER32:…`); the PHP code only
  recognises MD5 when it comes first. This version finds it anywhere in the string. Files with no obtainable hash
  are skipped with a log line.
- **Numbered sidecars.** For `IMG_1.jpg` the PHP glob also matches `IMG_1.jpg.supplemental-metadata(1).json`
  (which belongs to `IMG_1(1).jpg`) and can pick it first. Numbered sidecars are now ignored for un-numbered files.
- **URL encoding.** Remote names are encoded with `%20` (PHP `rawurlencode`) everywhere; the PHP rename path used
  `urlencode`, which turns spaces into `+`. Album duplicate checks compare decoded paths.
- **Rename follow-up.** After renaming a remote duplicate, the album copy uses the new path (the PHP version used
  the old, now missing path). `findAvailable` treats a 404 as "free" (the PHP `catch` referenced a class that
  doesn't exist in its namespace).
- A missing upload folder, a missing Photos app or a bad URL stops with a clear message; a failing album lookup
  only fails that directory.
- Optional positional argument for the Takeout directory; each directory's result is logged.
- The unused `Progress` class of the PHP code was not ported.

Like the original, a directory that fails is skipped on later runs while its `gp2nc-error.log` exists.
Fix the cause, delete the log, and run again.
