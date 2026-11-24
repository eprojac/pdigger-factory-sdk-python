"""Tolerant JSON parsing helpers for the typed models.

Unknown fields are ignored (every model keeps the source object in `raw_json`), optional fields may be absent or
null, and a required field that is absent or of the wrong type raises a `FactoryError` with code `decode`. Numeric
arrays are checked to be arrays but their elements are not copied or converted, which keeps parsing cheap for frames
with many stones and joints.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, TypeVar, overload

from .errors import FactoryError

T = TypeVar("T")

__all__ = ["P", "as_any", "as_arr", "as_bool", "as_int", "as_num", "as_obj", "as_str", "dict_of", "list_of"]


def _bad(what: str) -> FactoryError:
    return FactoryError(code="decode", message=what)


def as_int(v: Any) -> int:
    """An integer (a float with an integral value is accepted, a boolean is not)."""
    if isinstance(v, bool):
        raise _bad(f"expected an integer, got {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    raise _bad(f"expected an integer, got {v!r}")


def as_num(v: Any) -> float:
    """A number (an integer is accepted as it is)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _bad(f"expected a number, got {v!r}")
    return v


_WHOLE_MAX = 2**53
"""The largest whole number of SDK-DESIGN.md 4.6.2: every integer between it and 0 is a double."""


def _number(v: Any) -> bool:
    """A JSON number a double holds (SDK-DESIGN.md 4.6.2): never a boolean, an integer only when it fits a double (json
    reads a longer one as an int, which no double holds), a float only when finite."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(float(v))
    except OverflowError:  # an integer too large for a double
        return False


def _whole(v: Any) -> bool:
    """A number whose double is an integer from -2^53 to 2^53: 3 and 3.0 are both 3; 1.5 and 2^53 + 2 are not."""
    return _number(v) and float(v).is_integer() and abs(float(v)) <= _WHOLE_MAX


def fits_look(v: Any) -> bool:
    """A stone's look fits `StoneLook` by value (SDK-DESIGN.md 4.6.2): `sp`, `q` numbers; `v`, `g`, `nu`, `nv` whole
    numbers; `px` an array of numbers; `at` an object of the numbers `cx`, `cy`, `theta`, `L`, `S`; `d` a string."""
    if not isinstance(v, dict) or not all(_number(v.get(k)) for k in ("sp", "q")):
        return False
    if not all(_whole(v.get(k)) for k in ("v", "g", "nu", "nv")) or not isinstance(v.get("d"), str):
        return False
    px, at = v.get("px"), v.get("at")
    return (
        isinstance(px, list)
        and all(_number(x) for x in px)
        and isinstance(at, dict)
        and all(_number(at.get(k)) for k in ("cx", "cy", "theta", "L", "S"))
    )


def _fits_find(v: Any) -> bool:
    if not isinstance(v, dict) or not _whole(v.get("id")):
        return False
    if v.get("at") is not None and not _whole(v.get("at")):
        return False
    s, q = v.get("score"), v.get("second")
    return (s is None and q is None) or (_number(s) and _number(q))


def fits_speckle(v: Any) -> bool:
    """A speckle answer fits `WorldSpeckle` by value (SDK-DESIGN.md 4.6.12): `v` 1; `tray` a whole number; `ref` and
    `at` objects of a whole `g` and a number `t`; `device` a string; `gpu_ms`, `wall_ms` numbers; `stones` and `refs`
    arrays of finds (`id` a whole number, `at` a whole number or null, `score` and `second` both numbers or both null);
    `pairs` an array of two whole numbers each."""
    if not isinstance(v, dict) or not _whole(v.get("v")) or float(v["v"]) != 1 or not _whole(v.get("tray")):
        return False
    for k in ("ref", "at"):
        p = v.get(k)
        if not isinstance(p, dict) or not _whole(p.get("g")) or not _number(p.get("t")):
            return False
    if not isinstance(v.get("device"), str) or not _number(v.get("gpu_ms")) or not _number(v.get("wall_ms")):
        return False
    for k in ("stones", "refs"):
        if not isinstance(v.get(k), list) or not all(_fits_find(x) for x in v[k]):
            return False
    pairs = v.get("pairs")
    return isinstance(pairs, list) and all(
        isinstance(p, list) and len(p) == 2 and all(_whole(i) for i in p) for p in pairs
    )


def as_str(v: Any) -> str:
    """A string."""
    if not isinstance(v, str):
        raise _bad(f"expected a string, got {v!r}")
    return v


def as_bool(v: Any) -> bool:
    """A boolean."""
    if not isinstance(v, bool):
        raise _bad(f"expected a boolean, got {v!r}")
    return v


def as_arr(v: Any) -> list[Any]:
    """An array, returned as it is (its elements are not checked)."""
    if not isinstance(v, list):
        raise _bad(f"expected an array, got {type(v).__name__}")
    return v


def as_obj(v: Any) -> dict[str, Any]:
    """An object, returned as it is."""
    if not isinstance(v, dict):
        raise _bad(f"expected an object, got {type(v).__name__}")
    return v


def as_any(v: Any) -> Any:
    """Anything."""
    return v


def list_of(conv: Callable[[Any], T]) -> Callable[[Any], list[T]]:
    """An array whose every element is converted with `conv`."""

    def parse(v: Any) -> list[T]:
        return [conv(x) for x in as_arr(v)]

    return parse


def dict_of(conv: Callable[[Any], T]) -> Callable[[Any], dict[str, T]]:
    """An object whose every value is converted with `conv`."""

    def parse(v: Any) -> dict[str, T]:
        return {k: conv(x) for k, x in as_obj(v).items()}

    return parse


class P:
    """Reads the fields of one JSON object for a model, with the model's name in error messages."""

    __slots__ = ("o", "what")

    def __init__(self, data: Any, what: str) -> None:
        if not isinstance(data, dict):
            raise _bad(f"{what}: expected an object, got {type(data).__name__}")
        self.o: dict[str, Any] = data
        self.what = what

    def req(self, key: str, conv: Callable[[Any], T]) -> T:
        """A required field: absent or null raises `decode`."""
        v = self.o.get(key)
        if v is None:
            raise _bad(f"{self.what}: missing field {key!r}")
        try:
            return conv(v)
        except FactoryError as e:
            raise _bad(f"{self.what}.{key}: {e.message}") from None

    @overload
    def opt(self, key: str, conv: Callable[[Any], T]) -> T | None: ...

    @overload
    def opt(self, key: str, conv: Callable[[Any], T], default: T) -> T: ...

    def opt(self, key: str, conv: Callable[[Any], T], default: T | None = None) -> T | None:
        """An optional field: absent or null gives `default`."""
        v = self.o.get(key)
        if v is None:
            return default
        try:
            return conv(v)
        except FactoryError as e:
            raise _bad(f"{self.what}.{key}: {e.message}") from None
