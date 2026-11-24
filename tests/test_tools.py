"""The tools: the docs' picture shows what the SDK prints now, and the recordings download checks what it gets."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from .support import ROOT


def tool(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"tool_{name}", ROOT / "tools" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_quickstart_picture_is_current() -> None:
    picture = tool("terminal_picture")
    assert picture.main(["terminal_picture.py", "--check"]) == 0
    svg = (ROOT / "docs" / "img" / "quickstart-replay.svg").read_text(encoding="utf-8")
    assert "21:17.8</tspan> <tspan" in svg
    assert "Right hand lifts stone 2660" in svg
    assert "... 110 lines ..." in svg


def test_a_stale_picture_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    picture = tool("terminal_picture")
    monkeypatch.setattr(picture, "ROOT", tmp_path)
    monkeypatch.setattr(picture, "PICTURE", tmp_path / "docs" / "img" / "x.svg")
    monkeypatch.setattr(
        picture,
        "quickstart_output",
        lambda: (
            (ROOT / "fixtures" / "golden").joinpath("aug28-1270-1300.events.txt").read_text(encoding="utf-8")
            + (ROOT / "fixtures" / "golden").joinpath("aug28-1270-1300.summary.txt").read_text(encoding="utf-8")
        ),
    )
    assert picture.main(["terminal_picture.py", "--check"]) == 1
    assert "is stale" in capsys.readouterr().err
    assert picture.main(["terminal_picture.py"]) == 0
    assert picture.main(["terminal_picture.py", "--check"]) == 0
    assert (tmp_path / "docs" / "img" / "x.svg").read_text(encoding="utf-8") == (
        ROOT / "docs" / "img" / "quickstart-replay.svg"
    ).read_text(encoding="utf-8")


def test_download_recordings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The download, with the network replaced: it fetches, checks sha256, skips what it has, filters videos."""
    download = tool("download_recordings")
    world, summary = b"world bytes", b'{"frames": 1}'

    def entry(video: str) -> dict[str, object]:
        files = {
            "world": {"url": f"https://x/{video}.world", "sha256": hashlib.sha256(world).hexdigest()},
            "summary": {"url": f"https://x/{video}.summary", "sha256": hashlib.sha256(summary).hexdigest()},
        }
        return {"video": video, "files": files, "summary_text": "frames: 1\n"}

    listing = {"recordings": [entry("dry1"), entry("dry2")]}
    asked: list[str] = []

    def get(url: str) -> bytes:
        asked.append(url)
        if url.endswith("recordings.json"):
            return json.dumps(listing).encode()
        return world if url.endswith(".world") else summary

    monkeypatch.setattr(download, "_get", get)
    assert download.main([str(tmp_path), "--base", "https://x/", "--videos", "dry1"]) == 0
    assert (tmp_path / "dry1.world.ndjson.gz").read_bytes() == world
    assert (tmp_path / "dry1.summary.txt").read_text(encoding="utf-8") == "frames: 1\n"
    assert not (tmp_path / "dry2.world.ndjson.gz").exists()
    assert download.main([str(tmp_path), "--base", "https://x/"]) == 0
    assert asked.count("https://x/dry1.world") == 1  # already there with the right sha256
    assert "have  dry1.world.ndjson.gz" in capsys.readouterr().out
    world = b"tampered"
    (tmp_path / "dry2.world.ndjson.gz").unlink()
    with pytest.raises(SystemExit, match="sha256 differs"):
        download.main([str(tmp_path), "--base", "https://x/"])
    assert not (tmp_path / "dry2.world.ndjson.gz").exists()
    # --base moves the files too: the listing names them on the published site
    world = b"world bytes"
    published = {"world": {"url": download.DATA + "dry1.world"}, "summary": {"url": download.DATA + "dry1.summary"}}
    listing["recordings"] = [{**entry("dry1"), "files": published}]
    assert download.main([str(tmp_path / "local"), "--base", "http://localhost:8000/data/"]) == 0
    assert asked[-2:] == ["http://localhost:8000/data/dry1.world", "http://localhost:8000/data/dry1.summary"]
