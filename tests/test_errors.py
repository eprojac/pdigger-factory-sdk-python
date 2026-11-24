"""`FactoryError`, error codes and the JSON parsing helpers."""

from __future__ import annotations

import pickle
from typing import Any

import httpx
import pytest

from pdigger_factory_sdk import ErrorCode, FactoryError
from pdigger_factory_sdk._parse import (
    P,
    as_any,
    as_arr,
    as_bool,
    as_int,
    as_num,
    as_obj,
    as_str,
    dict_of,
    list_of,
)

from .support import api_bytes, api_index


def test_codes_compare_equal_to_strings() -> None:
    code: str = "not_retained"  # as a server sends it
    assert code == ErrorCode.NOT_RETAINED
    assert str(ErrorCode.TRANSPORT) == "transport"
    assert ErrorCode("session_gone") is ErrorCode.SESSION_GONE


@pytest.mark.parametrize("key", sorted(k for k, v in api_index().items() if v["status"] >= 400))
def test_every_recorded_error_reply_parses(key: str) -> None:
    entry = api_index()[key]
    err = FactoryError.from_reply(entry["status"], api_bytes(key), entry.get("headers"))
    assert err.status == entry["status"]
    assert err.code in {c.value for c in ErrorCode}
    assert err.message
    assert str(err).startswith(f"HTTP {entry['status']} {err.code}: ")


def test_not_retained_carries_the_window() -> None:
    err = FactoryError.from_reply(404, api_bytes("getFrame-404"))
    assert err.is_not_retained
    assert not err.is_session_gone
    assert not err.is_retryable
    assert err.retained is not None
    assert err.retained.n > 0
    assert err.retained.g0 is not None
    assert err.body is not None
    assert err.field == "g"


def test_errors_cross_processes() -> None:
    """A `FactoryError` pickles (multiprocessing, task queues): every field travels, the headers as a plain dict."""
    err = FactoryError.from_reply(404, api_bytes("getFrame-404"), httpx.Headers({"Retry-After": "2"}))
    again = pickle.loads(pickle.dumps(err))
    assert type(again) is FactoryError
    assert str(again) == str(err)
    assert (again.code, again.status, again.field, again.retry_ms) == ("not_retained", 404, "g", 2000)
    assert again.retained == err.retained
    assert again.body == err.body
    assert again.headers == {"retry-after": "2"}
    local = pickle.loads(pickle.dumps(FactoryError(code="transport", message="GET /x: ConnectError")))
    assert (local.code, local.status, local.headers, local.waiting) == ("transport", None, None, None)


def test_session_gone() -> None:
    err = FactoryError.from_reply(410, api_bytes("getFrame-410"))
    assert err.is_session_gone
    assert err.retained is None


def test_bad_retained_window_is_dropped() -> None:
    err = FactoryError.from_reply(404, b'{"code":"not_retained","error":"x","retained":{"session":"no"}}')
    assert err.retained is None
    assert err.code == "not_retained"


AT = "Wed, 21 Oct 2026 07:28:00 GMT"
"""A `Retry-After` date; `NOW` is 2.5 s before it on the local clock."""
NOW = 1792567677.5


@pytest.mark.parametrize(
    ("body", "headers", "retry_ms"),
    [
        (b'{"code":"busy","error":"wait","retry_ms":800}', {"Retry-After": "1"}, 800),
        (b'{"code":"busy","error":"wait","retry_ms":true}', {"Retry-After": "2"}, 2000),
        (b'{"code":"busy","error":"wait"}', {"Retry-After": "0.25"}, 250),
        (b'{"code":"busy","error":"wait"}', {"Retry-After": AT}, 2500),  # a date, against the local clock
        (b'{"code":"busy","error":"wait"}', {"Retry-After": AT, "Date": "Wed, 21 Oct 2026 07:27:59 GMT"}, 1000),
        (b'{"code":"busy","error":"wait"}', {"Retry-After": AT, "Date": "yesterday"}, 2500),
        (b'{"code":"busy","error":"wait"}', {"Retry-After": "Wed, 21 Oct 2026 07:27:00 GMT"}, 0),  # passed
        (b'{"code":"busy","error":"wait"}', {"Retry-After": "Fri, 23 Oct 2026 07:28:00 GMT"}, None),  # 2 days
        (b'{"code":"busy","error":"wait"}', {"Retry-After": "Wed, 21 Oct 2026 07:28:00 -0000"}, 2500),  # naive
        (b'{"code":"busy","error":"wait"}', {"Retry-After": "soon"}, None),
        (b'{"code":"busy","error":"wait"}', {"Retry-After": "-1"}, None),
        (b'{"code":"busy","error":"wait"}', {"Retry-After": "inf"}, None),
        (b'{"code":"busy","error":"wait"}', {"Date": AT}, None),
        (b'{"code":"busy","error":"wait"}', None, None),
    ],
)
def test_retry_hints(body: bytes, headers: dict[str, str] | None, retry_ms: int | None) -> None:
    err = FactoryError.from_reply(429, body, headers, now=NOW)
    assert err.retry_ms == retry_ms
    assert err.is_retryable


def test_a_retry_after_date_without_a_clock_is_measured_against_the_system_clock() -> None:
    err = FactoryError.from_reply(503, b"", {"Retry-After": "Mon, 01 Jan 2001 00:00:00 GMT"})
    assert err.retry_ms == 0  # long passed


@pytest.mark.parametrize(
    ("status", "content", "code", "message"),
    [
        (416, b"", "invalid_param", "HTTP 416"),
        (502, b"<html>Bad Gateway</html>", "internal", "<html>Bad Gateway</html>"),
        (404, b"not here", "not_found", "not here"),
        (418, b"\xff\xfe", "decode", "��"),
        (400, b"[1, 2]", "invalid_param", "[1, 2]"),
        (400, b'{"code": 7}', "invalid_param", '{"code": 7}'),
    ],
)
def test_replies_without_the_json_error_body(status: int, content: bytes, code: str, message: str) -> None:
    err = FactoryError.from_reply(status, content, {"Retry-After": "3"})
    assert err.code == code
    assert err.field == ("range" if status == 416 else None)
    assert err.message == message
    assert err.retry_ms == 3000
    assert err.body is None


def test_waiting_field_and_message_fallback() -> None:
    err = FactoryError.from_reply(503, b'{"code":"not_ready","waiting":["engines", 3],"field":5}')
    assert err.waiting == ["engines", "3"]
    assert err.message == "not_ready"
    assert err.field is None
    assert err.is_retryable


def test_local_errors_have_no_status() -> None:
    err = FactoryError(code="not_seekable", message="no", field="time")
    assert str(err) == "not_seekable: no (field time)"
    assert repr(err) == "FactoryError(code='not_seekable', status=None, message='no')"
    assert FactoryError(code="transport", message="refused").is_retryable


@pytest.mark.parametrize(
    ("conv", "good", "bad"),
    [
        (as_int, [(3, 3), (4.0, 4)], [True, 4.5, "4", None]),
        (as_num, [(3, 3), (2.5, 2.5)], [False, "1", None]),
        (as_str, [("a", "a")], [1, None]),
        (as_bool, [(True, True)], [1, "true"]),
        (as_arr, [([1], [1])], [(1,), {}]),
        (as_obj, [({"a": 1}, {"a": 1})], [[], "x"]),
        (as_any, [(None, None), (5, 5)], []),
    ],
)
def test_converters(conv: Any, good: list[tuple[Any, Any]], bad: list[Any]) -> None:
    for value, want in good:
        assert conv(value) == want
    for value in bad:
        with pytest.raises(FactoryError) as info:
            conv(value)
        assert info.value.code == "decode"


def test_list_and_dict_of() -> None:
    assert list_of(as_int)([1, 2.0]) == [1, 2]
    assert dict_of(as_str)({"a": "b"}) == {"a": "b"}
    with pytest.raises(FactoryError):
        list_of(as_int)(["x"])
    with pytest.raises(FactoryError):
        dict_of(as_int)([])


def test_field_reader() -> None:
    p = P({"a": 1, "b": None, "c": "x"}, "Thing")
    assert p.req("a", as_int) == 1
    assert p.opt("b", as_int) is None
    assert p.opt("b", as_int, 9) == 9
    assert p.opt("z", as_str, "d") == "d"
    with pytest.raises(FactoryError, match=r"Thing: missing field 'b'"):
        p.req("b", as_int)
    with pytest.raises(FactoryError, match=r"Thing\.c: expected an integer"):
        p.req("c", as_int)
    with pytest.raises(FactoryError, match=r"Thing\.c: expected an integer"):
        p.opt("c", as_int)
    with pytest.raises(FactoryError, match=r"Thing: expected an object, got list"):
        P([], "Thing")
