"""Replay a published recording offline and print its events and summary; no unit needed.

Run: `python examples/quickstart_replay.py` (dry1, streamed from factory.pdigger.com), or give it a downloaded
recording: `python examples/quickstart_replay.py dry2.world.ndjson.gz`.
"""

import sys

from pdigger_factory_sdk import InterpreterCore, Replay

DRY1 = "https://factory.pdigger.com/developers/data/dry1.world.ndjson.gz"


def main(source: str = DRY1) -> None:
    core = InterpreterCore()  # one pass: the events as they are read, then the summary of the same pass
    for event in Replay.open(source).events(core=core):
        print(event.describe())  # 5:02.8 Right hand places stone 643
    print(core.summary().describe(), end="")


if __name__ == "__main__":
    main(*sys.argv[1:2])
