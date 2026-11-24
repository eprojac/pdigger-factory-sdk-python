"""The high-level tutorial (docs/tutorials/01-high-level.md) in one program, offline: a recording's activities, its
trays and the stones removed from them, and one pass with a core of your own for the summary and a stone's path.

Run: `python examples/tutorial_high_level.py fixtures/recordings/dry1-0018-0153.ndjson.gz` (any recording: a path, or
an http(s) URL such as https://factory.pdigger.com/developers/data/dry1.world.ndjson.gz).
"""

import sys

from pdigger_factory_sdk import InterpreterCore, Move, Replay
from pdigger_factory_sdk.text import clock


def activities(source: str) -> None:
    """Step 1: what the hands did, one sentence per activity (a move, or a burst of pushes)."""
    for activity in Replay.open(source).activities():
        print(activity.describe())  # 1:19.2 Left hand takes stone 155 away (1.0 s)


def moves(source: str) -> None:
    """Step 2: the same activities as data: each move's stone, hand, outcome and length."""
    for activity in Replay.open(source).activities():
        if isinstance(activity, Move):  # a hand moving one stone; the other activities are bursts of pushes
            took = f"{activity.duration:.1f} s" if activity.duration is not None else "still open"
            at = clock(activity.start.t)
            print(f"{at} stone {activity.stone_id}: {activity.hand} hand, {activity.outcome}, {took}")


def tray_facts(source: str) -> None:
    """Step 3: the trays, as facts about the SDK's own stone ids (one per stone, never reused)."""
    for fact in Replay.open(source).tray_facts():
        print(fact.describe())  # 0:22.5 tray 1 arrived: 42 stones


def trays(source: str) -> None:
    """Step 4: each tray's arrival and leaving points, and the stones removed from it for certain."""
    for tray in Replay.open(source).trays():
        window = tray.window()
        if window is None:
            continue  # a stop that never settled
        arrived, leaving = window
        gone = [stone.id for stone in tray.stones if stone.status == "removed" and stone.certain]
        print(f"tray {tray.n}: {clock(arrived)} to {clock(leaving)}, {len(gone)} stones removed for certain")


def summary_and_track(source: str, stone: int) -> None:
    """Step 5: one pass with a core of your own: its summary, and the path of one stone of the world."""
    core = InterpreterCore()
    for _ in Replay.open(source).updates(core=core):
        pass  # every update of the pass: the events, activities and tray facts of each frame
    print(core.summary().describe(), end="")
    path = core.track(stone)  # where the stone was, frame by frame (scene units)
    if path:
        print(f"stone {stone}: {len(path)} points from {clock(path[0].t)} to {clock(path[-1].t)}")


def main(source: str = "fixtures/recordings/dry1-0018-0153.ndjson.gz", stone: int = 155) -> None:
    activities(source)
    moves(source)
    tray_facts(source)
    trays(source)
    summary_and_track(source, stone)


if __name__ == "__main__":
    if len(sys.argv) > 2:
        main(sys.argv[1], int(sys.argv[2]))
    elif len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        main()
