"""The look score (SDK-DESIGN.md 4.6.2) against the reference, on the synthetic looks of fixtures/looks: the same
double for every pair, and no score where the reference has none."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from pdigger_factory_sdk.looks import LOOK_CELLS, look_pairing, look_score, permille, read_look, usable_look
from pdigger_factory_sdk.models import StoneLook, StoneView

from .support import FIXTURES


def vectors() -> dict[str, Any]:
    v: dict[str, Any] = json.loads((FIXTURES / "looks" / "looks-synthetic.json").read_text())
    return v


def models() -> dict[str, StoneLook]:
    """Every synthetic look as `StoneView.look` reads it: the ones of `model_none` do not fit `StoneLook` by value."""
    v = vectors()
    out = {}
    for name, x in v["looks"].items():
        lk = StoneView.from_json(dict(STONE, look=x)).look
        assert (lk is None) == (name in v["model_none"]), name
        if lk is not None:
            out[name] = lk
    return out


STONE: dict[str, Any] = {
    "id": 7,
    "mode": "kept",
    "family": "rect",
    "shape": "frozen",
    "detected": True,
    "uncertain": False,
    "side": None,
    "outline": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]],
    "solid": {"pos": [0.0, 0.0, 0.05], "q": [0.0, 0.0, 0.0, 1.0], "dims": [0.3, 0.2]},
}


def test_look_score_matches_the_reference() -> None:
    v = vectors()
    looks = models()
    assert len(v["scores"]) >= 300
    assert len(v["unreadable"]) >= 6
    assert len(v["model_none"]) >= 8
    for name in v["unreadable"]:
        if name in looks:
            assert read_look(looks[name]) is None, name
            assert look_score(looks[name], looks[name]) is None, name
    for name in looks:
        assert (read_look(looks[name]) is None) == (name in v["unreadable"]), name
        assert (usable_look(looks[name]) is None) == (name in v["unreadable"] or name in v["tray_unusable"]), name
    scored = 0
    for a, b, want, n, p in v["scores"]:
        got = look_score(looks[a], looks[b])
        pair = look_pairing(looks[a], looks[b])
        if want is None:
            assert got is None, (a, b, got)
            assert pair is None, (a, b, pair)
            assert n is None, (a, b)
            assert p is None, (a, b)
        else:
            assert got == float(want), (a, b, got, want)
            assert pair == (got, n), (a, b, pair, n)
            assert permille(got) == p, (a, b, got, p)
            scored += 1
    assert scored >= 150
    assert permille(None) is None


def test_a_bad_look_never_makes_its_stone_unreadable() -> None:
    v = vectors()
    for name in v["model_none"]:
        stone = StoneView.from_json(dict(STONE, look=v["looks"][name]))
        assert stone.look is None, name
        assert stone.id == 7, name
    whole = StoneView.from_json(dict(STONE, look=v["looks"]["type-nu-whole-float"])).look
    assert whole is not None
    assert whole.nu == 18
    assert read_look(whole) is not None
    big = StoneView.from_json(dict(STONE, look=v["looks"]["type-over-4096-cells"])).look
    assert big is not None
    assert big.nu * big.nv > LOOK_CELLS
    assert read_look(big) is None
    huge = dict(v["looks"]["brick-b"], sp=float("inf"))
    assert StoneView.from_json(dict(STONE, look=huge)).look is None
    assert StoneView.from_json(dict(STONE, look=[1, 2])).look is None
    # a whole number is its double, as in every SDK: 2^53 + 1 reads as 2^53, and 2^53 + 2 is no look
    edge = StoneView.from_json(dict(STONE, look=dict(v["looks"]["brick-b"], g=2**53 + 1))).look
    assert edge is not None
    assert edge.g == 2**53
    assert StoneView.from_json(dict(STONE, look=dict(v["looks"]["brick-b"], g=2**53 + 2))).look is None


def test_cells_of_the_wrong_length_are_never_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """4.6.2: the length of `d` is checked first, so a look whose cells are not nu x nv bytes long costs no decoding."""
    decoded: list[int] = []
    real = base64.b64decode

    def counting(s: str | bytes, validate: bool = False) -> bytes:
        decoded.append(len(s))
        return real(s, validate=validate)

    monkeypatch.setattr(base64, "b64decode", counting)  # looks.py reads base64.b64decode at each call
    one = dict(vectors()["looks"]["brick-b"], nu=1, nv=1)
    for d in ("A" * 4_000_000, "AA==AA==", "A", ""):
        assert read_look(StoneLook.from_json(dict(one, d=d))) is None
    assert decoded == []  # none of them reached the decoder
    assert read_look(StoneLook.from_json(dict(one, d="AAA="))) is None  # the right length: decoded, two bytes
    assert read_look(StoneLook.from_json(dict(one, d="AA=="))) is not None
    assert decoded == [4, 4]


def test_what_is_not_a_look() -> None:
    v = vectors()
    good = StoneLook.from_json(v["looks"]["brick-b"])
    assert look_score(None, good) is None
    assert read_look(StoneLook.from_json(dict(v["looks"]["brick-b"], nu=0))) is None
    assert read_look(StoneLook.from_json(dict(v["looks"]["brick-b"], d="gBAg8OAw0MAA9yé"))) is None
    one = dict(v["looks"]["brick-b"], nu=1, nv=1)
    assert read_look(StoneLook.from_json(dict(one, d="AA=="))) is not None  # one cell: 4 characters
    assert read_look(StoneLook.from_json(dict(one, d="A" * 4_000_000))) is None  # far too long: not decoded at all
    assert look_score(good, good) == 1.0
