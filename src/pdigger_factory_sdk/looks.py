"""Looks (SDK-DESIGN.md 4.6.2): how well one stone's look agrees with another's, the same number in every SDK.

A look (`StoneLook`; the 20.20 unit sends looks since build `claude-20261120-looks1`, the published recordings carry
them) is the band-pass texture of a stone's face on a grid of cells. `look_score(a, b)` is the best normalised
cross-correlation of a's read cells with b's over b's turns (half a turn round, and a quarter and three quarters round
when both faces are near square), whole-cell shifts of up to 3 cells and then half-cell shifts around each turn's best.
It works from exact integer sums: 1 is the same texture, other stones score about 0.2 to 0.4.

    from pdigger_factory_sdk.looks import look_score
    score = look_score(stone.look, other.look)   # None: not comparable

`look_pairing` gives the score with the pair count of the placement that won, `permille` the integer the tray rules
compare, and `usable_look` the cells of a look the tray rules use (SDK-DESIGN.md 4.6.3).
"""

from __future__ import annotations

import base64
import binascii
import math
from dataclasses import dataclass
from operator import mul

from .models import StoneLook

__all__ = [
    "LOOK_CELLS",
    "LOOK_MIN",
    "LOOK_SHIFT",
    "LOOK_SQUARE",
    "look_pairing",
    "look_score",
    "permille",
    "usable_look",
]

LOOK_SHIFT = 3
"""Looks are compared at whole-cell shifts of up to this many cells each way ..."""
LOOK_MIN = 24
"""... where at least this many cells (and half the smaller look's read cells) pair."""
LOOK_SQUARE = 0.12
"""Faces whose sides differ by less than this share are also compared a quarter turn round."""
LOOK_CELLS = 4096
"""A look of more cells than this (nu x nv) is not a look: every sum stays exact in a double."""

Rows = list[list[int]]


@dataclass(slots=True, frozen=True)
class Cells:
    """A look the rules can read: rows of values (0 where not read), of read marks (1 or 0) and of squared values."""

    g: int
    square: bool
    n: int
    v: Rows
    m: Rows
    vv: Rows


def read_look(lk: StoneLook | None) -> Cells | None:
    """The cells of a look; None when the rules cannot read it (another version, more than LOOK_CELLS cells, cells
    that are not canonical base64 of nu x nv bytes)."""
    if lk is None or lk.v != 1 or lk.nu < 1 or lk.nv < 1 or lk.nu * lk.nv > LOOK_CELLS:
        return None
    if len(lk.d) != 4 * ((lk.nu * lk.nv + 2) // 3):  # not the length of nu x nv bytes: no need to decode it
        return None
    try:
        raw = base64.b64decode(lk.d, validate=True)
    except (binascii.Error, ValueError):
        return None
    if base64.b64encode(raw).decode("ascii") != lk.d or len(raw) != lk.nu * lk.nv:
        return None
    nu, nv = lk.nu, lk.nv
    v: Rows = []
    m: Rows = []
    vv: Rows = []
    n = 0
    for j in range(nv):
        row = [b - 256 if b > 127 else b for b in raw[j * nu : (j + 1) * nu]]
        v.append([0 if x == -128 else x for x in row])
        m.append([0 if x == -128 else 1 for x in row])
        vv.append([0 if x == -128 else x * x for x in row])
        n += sum(m[-1])
    big, small = max(float(lk.at.L), float(lk.at.S)), min(float(lk.at.L), float(lk.at.S))  # as doubles
    return Cells(lk.g, big > 0 and (big - small) / big < LOOK_SQUARE, n, v, m, vv)


def _turned(rows: Rows, q: int) -> Rows:
    nv, nu = len(rows), len(rows[0])
    if q == 2:
        return [r[::-1] for r in rows[::-1]]
    if q == 1:
        return [[rows[c][nu - 1 - r] for c in range(nv)] for r in range(nu)]
    return [[rows[nv - 1 - c][r] for c in range(nv)] for r in range(nu)]


def _sums(a: Cells, bv: Rows, bm: Rows, bb: Rows, ov: int, ou: int) -> tuple[int, int, int, int, int, int]:
    """The six integer sums of a's cells against b's, a's cell (0, 0) on b's cell (ov, ou)."""
    n = sa = sb = saa = sbb = sab = 0
    i0, i1 = max(0, -ou), min(len(a.v[0]), len(bv[0]) - ou)
    for j in range(max(0, -ov), min(len(a.v), len(bv) - ov)):
        av, am, aa = a.v[j][i0:i1], a.m[j][i0:i1], a.vv[j][i0:i1]
        rv, rm, rb = bv[j + ov][i0 + ou : i1 + ou], bm[j + ov][i0 + ou : i1 + ou], bb[j + ov][i0 + ou : i1 + ou]
        n += sum(map(mul, am, rm))
        sa += sum(map(mul, av, rm))
        sb += sum(map(mul, rv, am))
        saa += sum(map(mul, aa, rm))
        sbb += sum(map(mul, rb, am))
        sab += sum(map(mul, av, rv))
    return n, sa, sb, saa, sbb, sab


def _zncc(s: tuple[int, int, int, int, int, int], need: int) -> float | None:
    n, sa, sb, saa, sbb, sab = s
    if n < need:
        return None
    da, db = n * saa - sa * sa, n * sbb - sb * sb
    if da <= 0 or db <= 0:
        return None
    return (n * sab - sa * sb) / math.sqrt(float(da) * float(db))


def _half(bv: Rows, bm: Rows, nva: int, nua: int, ov: int, ou: int, hv: int, hu: int) -> tuple[Rows, Rows, Rows]:
    """b sampled half a cell from (ov, ou) toward (hv, hu): each cell the sum of the 2 or 4 cells around, read only
    when all of them are."""
    rows = (0,) if hv == 0 else ((0, 1) if hv > 0 else (-1, 0))
    cols = (0,) if hu == 0 else ((0, 1) if hu > 0 else (-1, 0))
    nvb, nub = len(bv), len(bv[0])
    hvv: Rows = []
    hm: Rows = []
    for j in range(nva):
        rv, rm = [0] * nua, [0] * nua
        for i in range(nua):
            s, ok = 0, True
            for a in rows:
                for c in cols:
                    y, x = j + ov + a, i + ou + c
                    if 0 <= y < nvb and 0 <= x < nub and bm[y][x]:
                        s += bv[y][x]
                    else:
                        ok = False
            if ok:
                rv[i], rm[i] = s, 1
        hvv.append(rv)
        hm.append(rm)
    return hvv, hm, [[x * x for x in r] for r in hvv]


def usable_look(lk: StoneLook | None) -> Cells | None:
    """The cells of a look the tray rules use (SDK-DESIGN.md 4.6.3): one `read_look` reads, with pitch `sp` 2 and
    scale `q` 32, at least 3 x 3 cells, both sides of its face above 0 and at least LOOK_MIN cells read; else None."""
    c = read_look(lk)
    if c is None or lk is None or float(lk.sp) != 2 or float(lk.q) != 32 or lk.nu < 3 or lk.nv < 3:
        return None
    if not (float(lk.at.L) > 0 and float(lk.at.S) > 0) or c.n < LOOK_MIN:
        return None
    return c


def permille(score: float | None) -> int | None:
    """A score as the integer the tray rules compare: floor(score * 1000 + 0.5) (an IEEE multiply, then an add)."""
    return None if score is None else math.floor(score * 1000 + 0.5)


def score_cells(a: Cells, b: Cells) -> float | None:
    """look_score of two read looks."""
    r = pair_cells(a, b)
    return None if r is None else r[0]


def pair_cells(a: Cells, b: Cells) -> tuple[float, int] | None:
    """look_pairing of two read looks: the score and the pair count of the placement that gave it."""
    need = max(LOOK_MIN, (min(a.n, b.n) + 1) // 2)
    nva, nua = len(a.v), len(a.v[0])
    turns = [(b.v, b.m, b.vv), (_turned(b.v, 2), _turned(b.m, 2), _turned(b.vv, 2))]
    if a.square and b.square:
        turns += [(_turned(b.v, q), _turned(b.m, q), _turned(b.vv, q)) for q in (1, 3)]
    best: float | None = None
    best_n = 0
    for bv, bm, bb in turns:
        ov, ou = (len(bv) - nva) // 2, (len(bv[0]) - nua) // 2  # floor of the centre offset
        top: float | None = None
        tn = tv = tu = 0
        for dv in range(-LOOK_SHIFT, LOOK_SHIFT + 1):
            for du in range(-LOOK_SHIFT, LOOK_SHIFT + 1):
                sums = _sums(a, bv, bm, bb, ov + dv, ou + du)
                z = _zncc(sums, need)
                if z is not None and (top is None or z > top):
                    top, tn, tv, tu = z, sums[0], dv, du
        if top is None:
            continue
        for hv in (-1, 0, 1):
            for hu in (-1, 0, 1):
                if hu == 0 and hv == 0:
                    continue
                h = _half(bv, bm, nva, nua, ov + tv, ou + tu, hv, hu)
                sums = _sums(a, *h, 0, 0)
                z = _zncc(sums, need)
                if z is not None and z > top:
                    top, tn = z, sums[0]
        if best is None or top > best:
            best, best_n = top, tn
    return None if best is None else (best, best_n)


def look_score(a: StoneLook | None, b: StoneLook | None) -> float | None:
    """How well look `a` agrees with look `b` (SDK-DESIGN.md 4.6.2): the best normalised cross-correlation of a's read
    cells with b's over b's turns (half a turn round, and a quarter and three quarters round when both faces are near
    square), whole-cell shifts of up to 3 cells and then half-cell shifts around each turn's best, from exact integer
    sums: the same number in every SDK. 1 is the same texture; other stones score about 0.2 to 0.4. None when either is
    not a look the rules can read or no placement pairs enough cells (24, and half the smaller look's read cells)."""
    x, y = read_look(a), read_look(b)
    if x is None or y is None:
        return None
    return score_cells(x, y)


def look_pairing(a: StoneLook | None, b: StoneLook | None) -> tuple[float, int] | None:
    """`look_score(a, b)` with `n`, the pair count of the placement that gave it (SDK-DESIGN.md 4.6.2); None exactly
    when the score is None."""
    x, y = read_look(a), read_look(b)
    if x is None or y is None:
        return None
    return pair_cells(x, y)
