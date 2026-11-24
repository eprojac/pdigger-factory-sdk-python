"""An incremental server-sent events parser that follows the HTML living standard's event-stream rules.

Fields `event`, `data` (several lines joined with `\\n`), `id` and `retry`; lines starting with `:` are comments; lines
end with CRLF, LF or CR (also when a CRLF is split between two chunks); the input is UTF-8; a byte order mark at the
very start is ignored; an event that is not ended by a blank line before the stream closes is discarded.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass

from .errors import FactoryError

__all__ = ["MAX_LINE", "LineSplitter", "SseEvent", "SseParser"]

_LINE_END = re.compile(r"\r\n|\r|\n")

MAX_LINE = 64 << 20
"""The longest line, and the most data of one event, a stream may send, in characters (a frame is well under a
million). More is a `decode` error that ends the connection."""


def _too_large(what: str, limit: int) -> FactoryError:
    return FactoryError(code="decode", message=f"{what} of the stream is longer than {limit} characters")


class LineSplitter:
    """Splits text into lines ending in CRLF, LF or CR (also a CRLF split between two chunks). Only the new text is
    scanned for line ends, and a line longer than `limit` characters is a `decode` error."""

    def __init__(self, limit: int = MAX_LINE) -> None:
        self._parts: list[str] = []  # the unfinished line, in pieces
        self._size = 0
        self._skip_lf = False
        self._limit = limit

    def push(self, text: str) -> list[str]:
        """The lines that `text` completes."""
        if self._skip_lf and text:
            self._skip_lf = False
            if text.startswith("\n"):
                text = text[1:]
        lines: list[str] = []
        start = 0
        for m in _LINE_END.finditer(text):
            line = text[start : m.start()]
            if self._parts:
                line = "".join([*self._parts, line])
                self._parts, self._size = [], 0
            if len(line) > self._limit:
                raise _too_large("a line", self._limit)
            lines.append(line)
            start = m.end()
        if start < len(text):
            self._parts.append(text[start:])
            self._size += len(text) - start
            if self._size > self._limit:
                raise _too_large("a line", self._limit)
        if text.endswith("\r"):
            self._skip_lf = True  # the CR may be the first half of a CRLF that the next chunk completes
        return lines

    def close(self) -> None:
        """Forget an unfinished line."""
        self._parts, self._size, self._skip_lf = [], 0, False


@dataclass(slots=True, frozen=True)
class SseEvent:
    """One dispatched event."""

    event: str
    """The event type (`message` when the event had no `event` field)."""
    data: str
    """The data lines joined with `\\n`."""
    id: str | None = None
    """The last event id at dispatch time (it persists from earlier events, as the standard says)."""


class SseParser:
    """Feed it bytes as they arrive; it returns the events completed by each chunk."""

    def __init__(self, limit: int = MAX_LINE) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._lines = LineSplitter(limit)
        self._limit = limit
        self._bom_checked = False
        self._data: list[str] = []
        self._data_size = 0
        self._event = ""
        self._id_buffer: str | None = None
        self.last_id: str | None = None
        """The id of the last event dispatched: the `Last-Event-ID` to resume with (`None` before any, or after an
        empty `id` field). The `id` of an event cut off before its blank line does not count."""
        self.retry: int | None = None
        """The reconnection time the stream asked for with `retry`, in milliseconds."""

    def feed(self, chunk: bytes) -> list[SseEvent]:
        """Parse a chunk; returns the events it completed."""
        return self._text(self._decoder.decode(chunk))

    def close(self) -> None:
        """End of the stream: an unfinished line or event is discarded, as the standard says."""
        self._decoder.reset()
        self._lines.close()
        self._data, self._data_size = [], 0
        self._event = ""
        self._id_buffer = self.last_id

    def _text(self, text: str) -> list[SseEvent]:
        if not self._bom_checked and text:
            self._bom_checked = True
            if text.startswith("\ufeff"):
                text = text[1:]
        out: list[SseEvent] = []
        for line in self._lines.push(text):
            self._line(line, out)
        return out

    def _line(self, line: str, out: list[SseEvent]) -> None:
        if not line:
            self._dispatch(out)
            return
        if line.startswith(":"):
            return
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value
        elif name == "data":
            self._data_size += len(value) + 1
            if self._data_size > self._limit:
                raise _too_large("an event", self._limit)
            self._data.append(value)
        elif name == "id":
            if "\0" not in value:
                self._id_buffer = value or None
        elif name == "retry" and value.isascii() and value.isdigit():
            self.retry = int(value)

    def _dispatch(self, out: list[SseEvent]) -> None:
        self.last_id = self._id_buffer
        if self._data:
            out.append(SseEvent(event=self._event or "message", data="\n".join(self._data), id=self.last_id))
        self._data, self._data_size = [], 0
        self._event = ""
