"""One pass over a recording: its header, every event and activity, the summary, and a check against a published one.

Run: `python examples/replay_in_one_pass.py dry1.world.ndjson.gz dry1.summary.json`.
"""

import json
import sys
from pathlib import Path

from pdigger_factory_sdk import Move, Replay, Summary


def main(source: str, published: str | None = None) -> None:
    result = Replay.open(source).interpret()
    header = result.header
    if header is not None:
        print(f"{header.name}: {header.video}, frames g {header.g_first} to {header.g_last}, build {header.build}")

    moves = [a for a in result.activities if isinstance(a, Move)]
    print(f"{len(result.events)} events, {len(result.activities)} activities, {len(moves)} moves")
    for move in moves:
        print(f"{move.outcome}: {move.describe()}")

    if published is not None:
        expected = Summary.from_dict(json.loads(Path(published).read_text(encoding="utf-8")))
        print("the summary matches" if result.summary == expected else "the summary differs")


if __name__ == "__main__":
    main(*sys.argv[1:3])
