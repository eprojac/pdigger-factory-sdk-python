"""Download the published recordings for the opt-in recording tests, and check their sha256.

    python tools/download_recordings.py recordings                 # every video: world file and summaries
    python tools/download_recordings.py recordings --videos dry1   # only some
    PDIGGER_FACTORY_RECORDINGS=recordings pytest -m integration tests/test_integration.py

It reads `recordings.json` of https://factory.pdigger.com/developers/data/ (or of `--base`, for example a local copy
of the site) and fetches `<video>.world.ndjson.gz` and `<video>.summary.json` of each video from there (the world
files are tens of MB), and writes `<video>.summary.txt` from the listing. A file already there
with the right sha256 is not fetched again. Standard library only; the integration workflow runs it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DATA = "https://factory.pdigger.com/developers/data/"
USER_AGENT = "pdigger-factory-sdk-python-tools/1.0.0"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        data: bytes = response.read()
        return data


def _at(base: str, url: str) -> str:
    """A file's URL under `base`: the listing names the files on the published site."""
    if url.startswith(DATA):
        url = url[len(DATA) :]
    return urllib.parse.urljoin(base, url)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _fetch(url: str, path: Path, sha256: str | None) -> None:
    if path.exists() and sha256 is not None and _sha256(path) == sha256:
        print(f"have  {path.name}")
        return
    path.write_bytes(_get(url))
    if sha256 is not None and _sha256(path) != sha256:
        path.unlink()
        raise SystemExit(f"{path.name}: sha256 differs from recordings.json")
    print(f"got   {path.name} ({path.stat().st_size} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--videos", help="comma-separated videos (default: every one listed)")
    parser.add_argument("--base", default=DATA, help="where the recordings are published")
    args = parser.parse_args(argv)
    args.folder.mkdir(parents=True, exist_ok=True)
    listing: dict[str, Any] = json.loads(_get(args.base + "recordings.json"))
    (args.folder / "recordings.json").write_text(json.dumps(listing, indent=1) + "\n", encoding="utf-8")
    wanted = set(args.videos.split(",")) if args.videos else None
    for entry in listing["recordings"]:
        video = entry["video"]
        if wanted is not None and video not in wanted:
            continue
        files = entry["files"]
        world, summary = files["world"], files["summary"]
        _fetch(_at(args.base, world["url"]), args.folder / f"{video}.world.ndjson.gz", world.get("sha256"))
        _fetch(_at(args.base, summary["url"]), args.folder / f"{video}.summary.json", summary.get("sha256"))
        (args.folder / f"{video}.summary.txt").write_text(entry["summary_text"], encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
