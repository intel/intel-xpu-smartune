#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Merge per-run windowed_metric_medians.csv files into one backend table.

Why this exists:
- export_windowed_metric_medians.py windows each case against one run-level
    metrics CSV.
- Running it over a backend tree with multiple runs can leave older runs with
    blank hardware medians because their windows are outside the current run's
    samples.
- This script merges per-run outputs so each run is aggregated against its own
    samples.

Merge semantics:
- One row per case; later inputs win.
- Callers pass existing backend CSV first and newer per-run CSVs after it.
- Rows are replaced as a whole (no field back-fill from older rows).

Pruning:
- Rows whose case_dir no longer exists are dropped by default.
- --keep-missing keeps those rows for moved/offline result trees.
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_windowed_metric_medians import write_summary_csv  # noqa: E402


def read_rows(path: Path) -> list[dict[str, str]]:
    """One CSV as a list of dicts, or nothing if it is unreadable."""
    if not path.is_file():
        return []
    try:
        with path.open(encoding='utf-8', newline='') as handle:
            return [
                {key: (value or '') for key, value in row.items() if key}
                for row in csv.DictReader(handle)
                if row
            ]
    except OSError as exc:
        print(f'Warning: could not read {path}: {exc}')
        return []


def row_key(row: dict[str, str]) -> str:
    """What makes two rows the same case.

    The case directory is unique per measurement and is what the dashboard joins
    on. case_name is not: a backend-wide file holds the same name once per
    device. It is only the fallback for a row the aggregator wrote without a
    directory, which no current version does.
    """
    case_dir = (row.get('case_dir') or '').strip()
    if case_dir:
        return case_dir
    return 'name:' + (row.get('case_name') or '').strip()


def merge(paths: list[Path], prune: bool) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for path in paths:
        for row in read_rows(path):
            key = row_key(row)
            if not key or key == 'name:':
                continue
            # Replacing in place keeps the first-seen order: the backend file is
            # read first, so existing cases stay where the previous report had
            # them and only genuinely new ones are appended.
            merged[key] = row

    rows = list(merged.values())
    if not prune:
        return rows

    kept = []
    for row in rows:
        case_dir = (row.get('case_dir') or '').strip()
        if case_dir and not Path(case_dir).is_dir():
            continue
        kept.append(row)
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Merge per-run windowed_metric_medians.csv files into one table.'
    )
    parser.add_argument('inputs', nargs='+',
                        help='CSVs to merge, lowest precedence first. Missing files are skipped.')
    parser.add_argument('-o', '--output', required=True, help='Output CSV path.')
    parser.add_argument('--keep-missing', action='store_true',
                        help='Keep rows whose case directory no longer exists on disk.')
    args = parser.parse_args()

    paths = [Path(p) for p in args.inputs]
    rows = merge(paths, prune=not args.keep_missing)
    if not rows:
        # Writing a header-only file here would replace a good report with an
        # empty one whenever every input happened to be unreadable.
        print('Nothing to merge; leaving the existing output alone.')
        return 0

    write_summary_csv(rows, Path(args.output))
    print(f'Merged {len(rows)} rows from {len(paths)} input(s) into {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
