"""Draw `docs/img/quickstart-replay.svg`: the real output of `examples/quickstart_replay.py` as a terminal.

It runs the example over the fixture recording `aug28-1270-1300` (aug28 from 21:10 to 21:40, the 1088 crop; a replay
on a Mac of the model's outputs through the interpreter of build `claude-20261122-hr4x1`, as its header's
`recorded_from` says) and keeps two windows of its event lines: the pushes before the lift of stone 2660 at 21:17.8
(frame g 6389) and its place at 21:21.8 (g 6409); every line left out is counted on screen. Then the summary, whole.

    python tools/terminal_picture.py           # writes the picture
    python tools/terminal_picture.py --check   # exit 1 if the picture is not what the code prints now

`tests/test_tools.py` runs the check, so the picture always shows what the SDK prints.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
RECORDING = "fixtures/recordings/aug28-1270-1300.ndjson.gz"
PICTURE = ROOT / "docs" / "img" / "quickstart-replay.svg"
COMMAND = f"python examples/quickstart_replay.py {RECORDING}"

FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"
SIZE, LINE, PAD, BAR = 13, 19, 20, 34
CHAR = 7.85  # the advance of one character at SIZE, for the picture's width
COLOURS = {
    "bg": "#0e1420",
    "bar": "#1a2232",
    "text": "#d6deeb",
    "dim": "#7d8799",
    "time": "#8792a8",
    "prompt": "#7fb4ff",
    "left": "#f2b766",
    "right": "#79c0ff",
    "both": "#c9a2ff",
    "label": "#ffffff",
}


def quickstart_output() -> str:
    """What `examples/quickstart_replay.py RECORDING` prints."""
    spec = importlib.util.spec_from_file_location("quickstart_replay", ROOT / "examples" / "quickstart_replay.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    main: Callable[[str], None] = module.main
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(str(ROOT / RECORDING))
    return out.getvalue()


def _index(lines: list[str], text: str) -> int:
    return next(i for i, line in enumerate(lines) if line.endswith(text))


def screen(output: str) -> list[tuple[str, str]]:
    """The picture's rows as (style, text): the command, the two windows of events with counts between, the summary."""
    lines = output.splitlines()
    summary = next(i for i, line in enumerate(lines) if line.startswith("frames: "))
    events, tail = lines[:summary], lines[summary:]
    first = _index(events, "Right hand pushes stone 2643")
    place = _index(events, "Right hand places stone 2660")
    windows = [(first, first + 24), (place - 1, place + 2)]
    rows: list[tuple[str, str]] = [("command", COMMAND)]
    shown = 0
    for start, end in windows:
        if start > shown:
            rows.append(("skip", f"... {start - shown} lines ..."))
        rows += [("event", line) for line in events[start:end]]
        shown = end
    if len(events) > shown:
        rows.append(("skip", f"... {len(events) - shown} lines ..."))
    rows += [("summary", line) for line in tail]
    return rows


HANDS = {"Left hand": "left", "Right hand": "right", "Both hands": "both", "A hand": "both"}


def _event(line: str) -> str:
    clock, _, sentence = line.partition(" ")
    tone = next((side for hand, side in HANDS.items() if sentence.startswith(hand)), "dim")
    time = f'<tspan fill="{COLOURS["time"]}">{escape(clock)}</tspan>'
    return f'{time} <tspan fill="{COLOURS[tone]}">{escape(sentence)}</tspan>'


def _row(style: str, text: str) -> str:
    if style == "command":
        return f'<tspan fill="{COLOURS["prompt"]}">$</tspan> {escape(text)}'
    if style == "skip":
        return f'<tspan fill="{COLOURS["dim"]}" font-style="italic">{escape(text)}</tspan>'
    if style == "event":
        return _event(text)
    label, _, rest = text.partition(":")
    return f'<tspan fill="{COLOURS["label"]}" font-weight="bold">{escape(label)}:</tspan>{escape(rest)}'


def render(output: str | None = None) -> str:
    """The SVG of `output` (by default the quickstart's output now)."""
    rows = screen(quickstart_output() if output is None else output)
    width = round(PAD * 2 + CHAR * max(len(text) + (2 if style == "command" else 0) for style, text in rows))
    height = BAR + PAD + LINE * len(rows) + PAD // 2
    dots = "".join(f'<circle cx="{18 + 18 * i}" cy="{BAR // 2}" r="5.5" fill="#3a4458"/>' for i in range(3))
    text_rows = "\n".join(
        f'  <text x="{PAD}" y="{BAR + PAD + LINE * n + SIZE - 4}" xml:space="preserve">{_row(style, text)}</text>'
        for n, (style, text) in enumerate(rows)
    )
    size = f'width="{width}" height="{height}" viewBox="0 0 {width} {height}"'
    return f"""<svg xmlns="http://www.w3.org/2000/svg" {size} role="img" \
aria-label="Output of examples/quickstart_replay.py over the recording aug28-1270-1300">
<title>examples/quickstart_replay.py over fixtures/recordings/aug28-1270-1300 (aug28, 21:10 to 21:40)</title>
<rect width="{width}" height="{height}" rx="10" fill="{COLOURS["bg"]}"/>
<path d="M0 10a10 10 0 0 1 10-10h{width - 20}a10 10 0 0 1 10 10v{BAR - 10}h-{width}z" fill="{COLOURS["bar"]}"/>
{dots}
<text x="{width // 2}" y="{BAR // 2 + 4}" text-anchor="middle" font-family="{FONT}" font-size="12" \
fill="{COLOURS["dim"]}">quickstart_replay.py</text>
<g font-family="{FONT}" font-size="{SIZE}" fill="{COLOURS["text"]}">
{text_rows}
</g>
</svg>
"""


def main(argv: list[str]) -> int:
    svg = render()
    if argv[1:2] == ["--check"]:
        current = PICTURE.read_text(encoding="utf-8") if PICTURE.exists() else ""
        if current != svg:
            print(f"{PICTURE.relative_to(ROOT)} is stale: run python tools/terminal_picture.py", file=sys.stderr)
            return 1
        return 0
    PICTURE.parent.mkdir(parents=True, exist_ok=True)
    PICTURE.write_text(svg, encoding="utf-8")
    print(f"wrote {PICTURE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
