# /// script
# requires-python = ">=3.12"
# dependencies = ["dechatter", "openpyxl"]
# [tool.uv.sources]
# dechatter = { path = "..", editable = true }
# ///
"""Score the chatter detector against the Purdue CNC cutting-sound dataset.

    uv run validation/purdue.py <cache-dir>

Downloads the IMI microphone recordings (about 370 MB) of
https://github.com/purduelamm/mt_cutting_dataset into <cache-dir>, classifies every labelled
cut with `dechatter.detect.classify`, and compares with the two operators' labels
(0 = no chatter, 1 = slight, 2 = chatter). Cuts the operators disagree on, or that either
calls slight, are reported separately: they have no clear ground truth.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

import openpyxl

from dechatter import audio, detect

REPO = "purduelamm/mt_cutting_dataset"
RAW = f"https://raw.githubusercontent.com/{REPO}/main/"
TREE = f"https://api.github.com/repos/{REPO}/git/trees/main?recursive=1"
TRIM = 0.15
AIR_GUARD_S = 0.3
AIR_MAX_S = 2.0


def fetch(cache: Path) -> dict[str, Path]:
    """Experiment name -> microphone recording, downloading what is missing."""
    with urllib.request.urlopen(TREE) as response:
        paths = [item["path"] for item in json.load(response)["tree"]]
    wanted = [p for p in paths if re.fullmatch(r"IMI/[^/]+/.*_sensor0\.wav", p)]
    wanted.append("IMI/labeling_all_details.xlsx")
    for path in wanted:
        target = cache / path
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {path}", file=sys.stderr)
        urllib.request.urlretrieve(RAW + path, target)
    return {Path(p).parent.name: cache / p for p in wanted if p.endswith(".wav")}


def truth(first: int, second: int) -> str:
    if first == second == 0:
        return "stable"
    if first == second == 2:
        return "chatter"
    return "unclear"


def main() -> None:
    cache = Path(sys.argv[1])
    recordings = fetch(cache)
    workbook = openpyxl.load_workbook(cache / "IMI/labeling_all_details.xlsx", data_only=True)
    tally: Counter[tuple[str, str]] = Counter()
    for name, wav in sorted(recordings.items()):
        samples, rate = audio.load(wav)
        previous_end = 0.0
        for row in workbook[name].iter_rows(min_row=2, values_only=True):
            if row[0] is None or row[13] is None:
                continue
            flutes = int(re.search(r"(\d)-flute", row[2]).group(1))
            rpm, start, end = float(row[6]), float(row[11]), float(row[12])
            trim = TRIM * (end - start)
            cut = samples[int((start + trim) * rate) : int((end - trim) * rate)]
            air_start = max(previous_end + AIR_GUARD_S, start - AIR_GUARD_S - AIR_MAX_S)
            air = samples[int(air_start * rate) : int((start - AIR_GUARD_S) * rate)]
            previous_end = end
            try:
                reading = detect.classify(cut, air, rate, rpm, flutes)
            except detect.TooShort:
                tally[(truth(row[13], row[14]), "too short")] += 1
                continue
            tally[(truth(row[13], row[14]), reading.verdict.value)] += 1
            excess = None if reading.excess_db is None else round(reading.excess_db, 1)
            print(
                f"{name:9} {row[0]:3} rpm {rpm:6.0f} labels {row[13]}{row[14]} "
                f"{reading.verdict:8} peak {reading.chatter_hz:7.1f} Hz prom "
                f"{reading.prominence_db:5.1f} air {excess}"
            )
    print("\nlabel    -> verdict   count")
    for (label, verdict), count in sorted(tally.items()):
        print(f"{label:8} -> {verdict:9} {count}")


if __name__ == "__main__":
    main()
