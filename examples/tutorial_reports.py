"""The first tutorial (docs/tutorials/00-start-here.md) in one program, offline: one report per tray, the same reports
as data, and the details behind a report that is not complete.

Run: `python examples/tutorial_reports.py fixtures/recordings/dry1-0018-0153.ndjson.gz` (any recording: a path, or an
http(s) URL such as https://factory.pdigger.com/developers/data/dry1.world.ndjson.gz).
"""

import json
import sys

from pdigger_factory_sdk import Replay


def reports(source: str) -> None:
    """Step 1: one report per tray: the stones removed, added and moved, when and where, and whether that is all of
    it."""
    for report in Replay.open(source).tray_reports():
        print(report.describe())


THRESHOLD = 0.9  # keep the changes at least this likely to be right


def rows(source: str) -> None:
    """Step 2: the same reports as data, one JSON line per tray, for a database, an MES or a spreadsheet."""
    for report in Replay.open(source).tray_reports():
        row = {
            "tray": report.tray,
            "arrived": report.arrived.wall,  # UTC epoch seconds (None when the unit's clock is not known)
            "left": report.left.wall,
            "removed": [change.stone for change in report.removed if change.confidence >= THRESHOLD],
            "added": [change.stone for change in report.added if change.confidence >= THRESHOLD],
            "moved": [change.stone for change in report.moved],
            "unclear": report.unclear,
            "complete": report.complete,
        }
        print(json.dumps(row))  # report.to_dict() has every field


def details(source: str) -> None:
    """Step 3: behind a report that is not complete, advanced mode: the tray's facts, each with its reason."""
    for tray in Replay.open(source).trays():
        report = tray.report()
        if report is None or report.complete:
            continue
        for fact in tray.facts:
            print(fact.describe())  # 0:30.1 tray 1: stone 20261122T023847.0Z-1-94 is not sure any more (it was pushed)


def main(source: str) -> None:
    reports(source)
    rows(source)
    details(source)


if __name__ == "__main__":
    main(sys.argv[1])
