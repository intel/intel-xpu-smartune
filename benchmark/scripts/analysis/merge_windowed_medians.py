#!/usr/bin/env python3
"""Merge per-run windowed_metric_medians.csv files into one backend-wide table.

Why this exists
---------------
export_windowed_metric_medians.py windows each case against ONE hardware
sampling CSV -- the run-level file benchmark/service/sampler.py wrote while that
run was going. Pointing it at a whole backend tree therefore only works while
the tree holds a single run: every case belonging to an *earlier* run has a
measurement window that lies entirely outside the current run's samples, so
collect_csv_medians finds nothing in range and writes a blank for every hardware
column. The KPIs survive (they are scraped from each case's detail.log, which
never goes stale), which is exactly the symptom that shows up in the dashboard:
older runs keep their throughput and latency and lose their power, utilisation,
frequency and memory columns.

Since runner.py started giving each run its own results directory, the fix is to
aggregate each run against its own samples -- into its own directory, which
benchmark/service/results.py's _artifact already prefers -- and then merge those
per-run files here into the backend-wide one that pivot_report.py pivots over.
Old rows are carried through untouched; only the run that just finished is
recomputed.

Merge rule: one row per case, later inputs winning. Callers pass the existing
backend CSV first and the freshly written per-run files after it, so a case that
was re-measured takes its new numbers and every other case keeps the ones it
already had. A row is replaced whole rather than field-merged: a blank column in
a newer row means "this collector had nothing to say about this case", and
back-filling it from an older measurement of the same case would invent a
reading that was never taken.

Rows whose case directory no longer exists are dropped, so deleting a case
(benchmark/service/results.py's delete_cases) does not leave the report pivoting
over measurements with nothing behind them. --keep-missing turns that off for a
tree that has been moved since it was produced, where every recorded case_dir is
stale but the data is still good.
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
