#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


class _Captured(Exception):
    def __init__(self, parser):
        super().__init__("parser captured")
        self.parser = parser


def _capture(build) -> argparse.ArgumentParser:
    """Call `build` and return the parser it was about to parse with.

    benchmark_app's parse_args() is misnamed: it returns the parser and leaves
    parsing to its caller, so a returned parser is taken as-is.
    """
    originals = (argparse.ArgumentParser.parse_args,
                 argparse.ArgumentParser.parse_known_args)

    def _stub(self, *args, **kwargs):
        raise _Captured(self)

    argparse.ArgumentParser.parse_args = _stub
    argparse.ArgumentParser.parse_known_args = _stub
    try:
        returned = build()
        if isinstance(returned, argparse.ArgumentParser):
            return returned
    except _Captured as captured:
        return captured.parser
    finally:
        (argparse.ArgumentParser.parse_args,
         argparse.ArgumentParser.parse_known_args) = originals
    raise RuntimeError("the tool never reached parse_args")


def _json_safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


_FLAG_ACTIONS = (argparse._StoreTrueAction, argparse._StoreFalseAction,
                 argparse._StoreConstAction, argparse._CountAction)


def _describe(parser: argparse.ArgumentParser) -> list:
    params = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._VersionAction)):
            continue
        if not action.option_strings:
            continue  # positionals: neither tool has any we would set
        type_name = getattr(action.type, "__name__", None) if action.type else None
        params.append({
            "dest": action.dest,
            "flags": list(action.option_strings),
            "kind": "flag" if isinstance(action, _FLAG_ACTIONS) else "value",
            "type": type_name,
            "choices": _json_safe(list(action.choices)) if action.choices else None,
            "default": _json_safe(action.default),
            "nargs": _json_safe(action.nargs),
            "required": bool(action.required),
            "help": (action.help or "")[:400] if action.help != argparse.SUPPRESS else "",
        })
    return params


def _probe_genai(benchmark_py: Path) -> dict:
    # benchmark.py imports its siblings (llm_bench_utils, task) by bare name.
    sys.path.insert(0, str(benchmark_py.parent))
    spec = importlib.util.spec_from_file_location("llm_bench_benchmark", benchmark_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = _capture(module.get_argparser)
    return {"source": str(benchmark_py),
            "sha256": hashlib.sha256(benchmark_py.read_bytes()).hexdigest(),
            "params": _describe(parser)}


def _probe_benchmark_app() -> dict:
    from openvino import get_version
    from openvino.tools.benchmark import parameters
    parser = _capture(parameters.parse_args)
    source = Path(parameters.__file__)
    return {"source": str(source),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "openvino": get_version(),
            "params": _describe(parser)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump a benchmark tool's command-line parameters as JSON.")
    parser.add_argument("tool", choices=["genai", "benchmark_app"])
    parser.add_argument("--benchmark-py", help="llm_bench benchmark.py (genai only)")
    args = parser.parse_args()

    if args.tool == "genai":
        if not args.benchmark_py:
            parser.error("genai needs --benchmark-py")
        result = _probe_genai(Path(args.benchmark_py))
    else:
        result = _probe_benchmark_app()
    result["tool"] = args.tool
    # The tools log while importing; the JSON is the last line so a caller can
    # take it from the end of stdout whatever was printed before it.
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
