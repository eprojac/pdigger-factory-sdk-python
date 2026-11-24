"""The SDK's one error type, `FactoryError`, and the stable error codes.

Code is keyed on `FactoryError.code` (the API's stable code string, or one of the SDK-only codes `transport`,
`decode`, `live_unavailable`, `live_timeout` and `stream_closed`), never on the message.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Protocol

__all__ = ["ErrorCode", "FactoryError", "RetainedWindow"]


class RetainedWindow(Protocol):
    """The retained frame window an error reports (a `models.Retained`)."""

    @property
    def session(self) -> int: ...
    @property
    def g0(self) -> int | None: ...
    @property
    def g1(self) -> int | None: ...
    @property
    def t0(self) -> float | None: ...
    @property
    def t1(self) -> float | None: ...
    @property
    def n(self) -> int: ...


class ErrorCode(str, Enum):
    """Every error code: the API's `ErrorCode` enum plus the SDK-only codes.

    Members compare equal to their strings, so `err.code == ErrorCode.NOT_RETAINED` and `err.code == "not_retained"`
    are the same test. A server may send a code that is not listed here; the SDK passes it through as a plain string.
    """

    INVALID_JSON = "invalid_json"
    NOT_AN_OBJECT = "not_an_object"
    CHUNKED_REFUSED = "chunked_refused"
    LENGTH_REQUIRED = "length_required"
    BODY_TOO_LARGE = "body_too_large"
    BODY_NOT_ALLOWED = "body_not_allowed"
    CROSS_ORIGIN = "cross_origin"
    JSON_ONLY = "json_only"
    TOO_MANY_CONTROLS = "too_many_controls"
    TOO_MANY_REQUESTS = "too_many_requests"
    TOO_MANY_VIEWERS = "too_many_viewers"
    TOO_MANY_STREAMS = "too_many_streams"
    UNKNOWN_VIDEO = "unknown_video"
    TIME_OUT_OF_RANGE = "time_out_of_range"
    INVALID_TYPE = "invalid_type"
    MISSING_FIELD = "missing_field"
    INVALID_PARAM = "invalid_param"
    UNKNOWN_LAYER = "unknown_layer"
    NOT_RETAINED = "not_retained"
    SESSION_GONE = "session_gone"
    STALE_SESSION = "stale_session"
    UNKNOWN_STONE = "unknown_stone"
    WORLD_UNAVAILABLE = "world_unavailable"
    DEBUG_DISABLED = "debug_disabled"
    BUSY = "busy"
    NOT_READY = "not_ready"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    INTERNAL = "internal"
    NOT_SEEKABLE = "not_seekable"
    # SDK-only codes
    TRANSPORT = "transport"
    """No HTTP reply: the connection failed, broke or timed out."""
    DECODE = "decode"
    """A reply that is not the documented JSON."""
    LIVE_UNAVAILABLE = "live_unavailable"
    """The server's catalog has no live camera."""
    LIVE_TIMEOUT = "live_timeout"
    """The session's camera did not go live in time (`Session.until_live`)."""
    STREAM_CLOSED = "stream_closed"
    """A stream ended or broke and was not resumed (`resume=False`), or was used after it was closed."""

    def __str__(self) -> str:
        return self.value


# The code of an error reply that does not carry the API's JSON error body (a proxy page, an empty 416).
_STATUS_CODES = {
    400: "invalid_param",
    403: "cross_origin",
    404: "not_found",
    405: "method_not_allowed",
    413: "body_too_large",
    415: "json_only",
    416: "invalid_param",
    429: "too_many_requests",
    503: "not_ready",
}

_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class FactoryError(Exception):
    """An error of the pDigger Factory API or of the SDK.

    Attributes:
        status: The HTTP status, or `None` when there was no HTTP reply (`transport`) or the SDK refused a call
            locally (for example `not_seekable` for a seek on the live camera).
        code: The stable error code (see `ErrorCode`).
        message: The human-readable message (the API's `error`).
        field: The parameter or body field at fault, if any.
        retry_ms: How long the server asks to wait before retrying, if it said.
        retained: The retained frame window (`not_retained` errors).
        waiting: What the server is still waiting for (`GET /ready`).
        body: The error reply's JSON object, when there was one.
        headers: The error reply's HTTP headers, when there was a reply.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status: int | None = None,
        field: str | None = None,
        retry_ms: int | None = None,
        retained: RetainedWindow | None = None,
        waiting: list[str] | None = None,
        body: dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field = field
        self.retry_ms = retry_ms
        self.retained = retained
        self.waiting = waiting
        self.body = body
        self.headers = headers

    def __str__(self) -> str:
        where = f" (field {self.field})" if self.field else ""
        status = f"HTTP {self.status} " if self.status is not None else ""
        return f"{status}{self.code}: {self.message}{where}"

    def __repr__(self) -> str:
        return f"FactoryError(code={self.code!r}, status={self.status!r}, message={self.message!r})"

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        """Pickle by keyword arguments, so the error crosses processes (multiprocessing, task queues).

        The headers travel as a plain dict; the cause and the traceback stay behind, as with every exception.
        """
        kwargs = {
            "code": self.code,
            "message": self.message,
            "status": self.status,
            "field": self.field,
            "retry_ms": self.retry_ms,
            "retained": self.retained,
            "waiting": self.waiting,
            "body": self.body,
            "headers": None if self.headers is None else dict(self.headers.items()),
        }
        return _restore, (type(self), kwargs)

    @property
    def is_retryable(self) -> bool:
        """True for a transport error and for 429, 502, 503 and 504 replies."""
        return self.code == ErrorCode.TRANSPORT or self.status in _RETRYABLE_STATUS

    @property
    def is_not_retained(self) -> bool:
        """True when the frame asked for is outside the retained window (`not_retained`)."""
        return self.code == ErrorCode.NOT_RETAINED

    @property
    def is_session_gone(self) -> bool:
        """True when the session asked for has ended (`session_gone`): another viewer selected or seeked."""
        return self.code == ErrorCode.SESSION_GONE

    @classmethod
    def from_reply(
        cls, status: int, content: bytes, headers: Mapping[str, str] | None = None, *, now: float | None = None
    ) -> FactoryError:
        """The error of an HTTP error reply: its JSON body when it has one, else a code from the status.

        `retry_ms` is the body's `retry_ms`, else the `Retry-After` header: seconds, or an HTTP date, which is measured
        against the reply's own `Date` header (the server's clock), else against `now` (epoch seconds on the local
        wall clock; `time.time()` when not given). A date that has passed gives 0; a wait of a day or more is ignored.
        """
        from .models import Retained  # late import: models import this module

        retry_after = _retry_after_ms(headers, now)
        body: Any = None
        try:
            body = json.loads(content.decode("utf-8")) if content else None
        except (UnicodeDecodeError, ValueError, RecursionError):
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("code"), str):
            code = _STATUS_CODES.get(status, "internal" if status >= 500 else "decode")
            text = content.decode("utf-8", "replace").strip()[:200]
            return cls(
                code=code,
                status=status,
                message=text or f"HTTP {status}",
                field="range" if status == 416 else None,
                retry_ms=retry_after,
                headers=headers,
            )
        retained: RetainedWindow | None = None
        if isinstance(body.get("retained"), dict):
            try:
                retained = Retained.from_json(body["retained"])
            except FactoryError:
                retained = None
        retry_ms = body.get("retry_ms")
        waiting = body.get("waiting")
        field = body.get("field")
        return cls(
            code=body["code"],
            status=status,
            message=str(body.get("error") or body["code"]),
            field=field if isinstance(field, str) else None,
            retry_ms=retry_ms if isinstance(retry_ms, int) and not isinstance(retry_ms, bool) else retry_after,
            retained=retained,
            waiting=[str(w) for w in waiting] if isinstance(waiting, list) else None,
            body=body,
            headers=headers,
        )


def _restore(cls: type[FactoryError], kwargs: dict[str, Any]) -> FactoryError:
    """Rebuild a pickled `FactoryError` (see `FactoryError.__reduce__`)."""
    return cls(**kwargs)


_DAY = 86400.0
"""The longest `Retry-After` the SDK waits, seconds (a longer one is ignored, and the backoff is used)."""


def _http_date(value: str) -> float | None:
    """An HTTP date (`Wed, 18 Nov 2026 04:30:00 GMT`) as epoch seconds, or `None`."""
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return (when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)).timestamp()


def _retry_after_ms(headers: Mapping[str, str] | None, now: float | None) -> int | None:
    """The `Retry-After` header (RFC 9110) in milliseconds: delay-seconds, or an HTTP date measured against the reply's
    `Date` header, else against `now` (the local wall clock). `None` when it is missing, does not parse, or asks for a
    negative wait or one of a day or more."""
    value = None if headers is None else headers.get("Retry-After")
    if headers is None or value is None:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        when = _http_date(text)
        if when is None:
            return None
        server = _http_date(headers.get("Date", ""))
        base = server if server is not None else time.time() if now is None else now
        seconds = max(0.0, when - base)
    return int(seconds * 1000) if 0 <= seconds < _DAY else None
