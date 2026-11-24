"""Simple mode: one report per tray, which stones were removed, which were added and which moved, when and where, and
whether that is the tray's whole account. No unit needed for a recording.

Run: `python examples/quickstart_reports.py` (dry1, streamed from factory.pdigger.com), or give it a downloaded
recording: `python examples/quickstart_reports.py dry2.world.ndjson.gz`. `follow()` does the same against the unit, a
report as each tray leaves: `PDIGGER_FACTORY_URL=http://factory-orin.local:8120 python examples/quickstart_reports.py
--follow`.
"""

import asyncio
import sys

from pdigger_factory_sdk import FactoryClient, Replay

DRY1 = "https://factory.pdigger.com/developers/data/dry1.world.ndjson.gz"


def main(source: str = DRY1) -> None:
    for report in Replay.open(source).tray_reports():
        print(report.describe())


async def follow() -> None:
    async with FactoryClient() as factory:  # PDIGGER_FACTORY_URL, else http://factory-orin.local:8120
        async for report in factory.interpreter().tray_reports():
            print(report.describe())  # report.to_dict() is the same as data, for your MES or ERP


if __name__ == "__main__":
    if sys.argv[1:2] == ["--follow"]:
        asyncio.run(follow())
    else:
        main(*sys.argv[1:2])
