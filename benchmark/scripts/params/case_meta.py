#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


import argparse
import json
import os
import sys
from pathlib import Path

CASE_SCHEMA_VERSION = 1


def _load_meta(path: str) -> dict:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _float_or_none(text: str):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _manifest(args) -> None:
    meta = _load_meta(args.meta)
    record = {
        "case_key": args.case_key,
        "model": args.model,
        "quant": args.quant,
        "task": args.task,
        "device": args.device,
        "model_dir": args.model_dir,
        "command": args.command,
        "tool": meta.get("tool"),
        "variant_id": meta.get("variant_id"),
        "params": meta.get("params") or {},
        "swept": meta.get("swept") or {},
        "errors": meta.get("errors") or [],
        "warnings": meta.get("warnings") or [],
    }
    with open(args.out, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _case(args) -> None:
    meta = _load_meta(args.meta)
    out = Path(args.out)
    case = {
        "schema": CASE_SCHEMA_VERSION,
        "case_key": args.case_key,
        "model": args.model,
        "quant": args.quant,
        "device": args.device,
        "model_dir": args.model_dir,
        "tool": meta.get("tool") or args.tool,
        "task": meta.get("task") or args.task or None,
        "variant_id": meta.get("variant_id"),
        "params": meta.get("params") or {},
        "swept": meta.get("swept") or {},
        "swept_keys": meta.get("swept_keys") or [],
        "extra_args": meta.get("extra_args") or [],
        "errors": meta.get("errors") or [],
        "warnings": meta.get("warnings") or [],
        "status": args.status,
        # Process launch, before the model is loaded: the analysis takes the
        # pre-run memory/temperature snapshot from the sample just before it.
        "launch_ts": _float_or_none(args.launch_ts),
        "end_ts": _float_or_none(args.end_ts),
        # The operator rewrote this case's command in the plan review, so
        # `params` describes what was planned, not necessarily what ran.
        "overridden": args.overridden == "1",
        "command": args.command,
    }
    # Which round of a multi-round run this is, and whether the sampler ran in
    # full for it (run_template.sh's round loop exports both). Round 0 is the
    # warm-up. Absent from a single-round run, which is sampled as always.
    rnd = os.environ.get("BENCH_ROUND", "")
    if rnd.isdigit():
        case["round"] = int(rnd)
        case["rounds"] = int(os.environ.get("BENCH_ROUNDS_TOTAL") or 0) or None
        case["warmup"] = rnd == "0"
    case["sampled"] = os.environ.get("BENCH_ROUND_SAMPLED", "1") != "0"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(case, indent=2, default=str), encoding="utf-8")
    tmp.replace(out)


def _errors(args) -> None:
    for error in _load_meta(args.meta).get("errors") or []:
        print(error)


def main() -> int:
    parser = argparse.ArgumentParser(description="Case JSON bookkeeping for the benchmark commons")
    sub = parser.add_subparsers(dest="action", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--meta", default="")
    common.add_argument("--case-key", default="")
    common.add_argument("--model", default="")
    common.add_argument("--quant", default="")
    common.add_argument("--task", default="")
    common.add_argument("--tool", default="")
    common.add_argument("--device", default="")
    common.add_argument("--model-dir", default="")
    common.add_argument("--command", default="")

    manifest = sub.add_parser("manifest", parents=[common])
    manifest.add_argument("--out", required=True)

    case = sub.add_parser("case", parents=[common])
    case.add_argument("--out", required=True)
    case.add_argument("--status", default="")
    case.add_argument("--launch-ts", default="")
    case.add_argument("--end-ts", default="")
    case.add_argument("--overridden", default="0")

    errors = sub.add_parser("errors")
    errors.add_argument("--meta", default="")

    args = parser.parse_args()
    {"manifest": _manifest, "case": _case, "errors": _errors}[args.action](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
