#!/usr/bin/env python3
"""
Merge convert pass results (pass1/pass2/pass3) into a single convert_results.json.

Usage:
    python gen_convert_results_merged.py \
        --logs-dir /path/to/skill_env/logs \
        --output /path/to/convert_results.json
"""

import argparse
import json
from pathlib import Path


def load_json_if_exists(path: Path) -> dict | None:
    """Load JSON file if it exists, return None otherwise."""
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def build_routing_index(routing: dict) -> dict:
    """
    Build a flat model -> route_info mapping from the routing JSON.
    Covers pass1_models, pass2_models, pass3_models.
    """
    index = {}
    for pass_key in ("pass1_models", "pass2_models", "pass3_models"):
        for route in routing.get(pass_key, []):
            index[route["model"]] = route
    return index


def extract_result_entry(exec_result: dict, route_info: dict | None, pass_num: int) -> dict:
    """Build a single result entry from an executed script result + routing metadata."""
    model = exec_result.get("model", "unknown")
    status = exec_result.get("status", "unknown")

    entry = {
        "model": model,
        "status": status,
        "pass": pass_num,
    }

    if route_info:
        entry["convert_strategy"] = route_info.get("strategy", "unknown")
        entry["convert_tool"] = route_info.get("tool", "unknown")
    else:
        entry["convert_strategy"] = "unknown"
        entry["convert_tool"] = "unknown"

    entry["ir_path"] = exec_result.get("output_dir", "")

    # Extract task / library from routing parameters when available
    params = route_info.get("parameters", {}) if route_info else {}
    if "task" in params:
        entry["task"] = params["task"]
    if "library" in params:
        entry["library"] = params["library"]

    entry["execution_time_seconds"] = exec_result.get("execution_time_seconds", 0.0)

    if status != "success":
        # Attach a human-readable reason when available
        stderr_log = exec_result.get("stderr_log", "")
        entry["reason"] = f"exit_code={exec_result.get('exit_code', '?')}, see {stderr_log}"

    return entry


def main():
    parser = argparse.ArgumentParser(
        description="Merge convert pass results into a single convert_results.json"
    )
    parser.add_argument(
        "--logs-dir",
        required=True,
        help="Directory containing pass*_results.json and convert_routing.json",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output path for the merged convert_results.json",
    )
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)

    # Load routing for metadata (strategy / tool / task / library)
    routing_path = logs_dir / "convert_routing.json"
    routing = load_json_if_exists(routing_path) or {}
    routing_index = build_routing_index(routing)

    results = []
    total_success = 0
    total_failed = 0

    # Process each pass in order
    for pass_num in (1, 2, 3):
        pass_file = logs_dir / f"pass{pass_num}_results.json"
        pass_data = load_json_if_exists(pass_file)
        if pass_data is None:
            continue  # Pass was skipped or not yet run

        for exec_result in pass_data.get("executed_scripts", []):
            model = exec_result.get("model", "unknown")
            route_info = routing_index.get(model)
            entry = extract_result_entry(exec_result, route_info, pass_num)
            results.append(entry)

            if exec_result.get("status") == "success":
                total_success += 1
            else:
                total_failed += 1

    # Include models that the router marked as failed before any pass ran
    for failed in routing.get("failed_models", []):
        model = failed.get("model", "unknown")
        # Avoid double-counting if a later pass recorded it
        already_recorded = any(r["model"] == model for r in results)
        if not already_recorded:
            results.append({
                "model": model,
                "status": "failed",
                "pass": 0,
                "convert_strategy": "unknown",
                "convert_tool": "unknown",
                "ir_path": "",
                "reason": failed.get("reason", "routing failed"),
            })
            total_failed += 1

    total_models = routing.get("total_models", len(results))

    merged = {
        "total_models": total_models,
        "successful_models": total_success,
        "failed_models": total_failed,
        "results": results,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(merged, f, indent=2)

    print(f"Merged convert results written to: {output_path}")
    print(f"  total={total_models}, success={total_success}, failed={total_failed}")


if __name__ == "__main__":
    main()
