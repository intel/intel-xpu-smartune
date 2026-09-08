#!/usr/bin/env python3
"""
Unified wrapper executor - Execute wrapper scripts with timeout, logging, and validation.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.global_vars import load_global_vars, get_logs_dir

GLOBAL_VARS = load_global_vars()


def execute_single_wrapper(
    script_path: str,
    timeout: int = 3600
) -> Dict:
    """
    Execute a single wrapper script.

    Status is derived solely from the script's exit code. Output/artifact
    validation is the wrapper script's own responsibility (e.g. the convert
    template checks for *.xml/*.bin and exits non-zero on failure). Keeping
    this executor artifact-agnostic lets it drive any stage, including
    benchmark, which produces no model files.

    Args:
        script_path: Path to wrapper script
        timeout: Timeout in seconds

    Returns:
        Dict with execution results
    """
    start_time = time.time()
    script_path_obj = Path(script_path)

    # Determine log file path
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_base = logs_dir / script_path_obj.stem

    stdout_log = f"{log_base}.stdout.log"
    stderr_log = f"{log_base}.stderr.log"

    try:
        # Execute script
        with open(stdout_log, 'w') as stdout_f, open(stderr_log, 'w') as stderr_f:
            result = subprocess.run(
                ['bash', script_path],
                stdout=stdout_f,
                stderr=stderr_f,
                timeout=timeout,
                text=True
            )

        exit_code = result.returncode
        status = 'success' if exit_code == 0 else 'failed'

    except subprocess.TimeoutExpired:
        exit_code = -1
        status = 'timeout'

    except Exception as e:
        exit_code = -2
        status = 'error'
        with open(stderr_log, 'a') as f:
            f.write(f"\nException: {str(e)}\n")

    execution_time = time.time() - start_time

    return {
        'script_path': script_path,
        'status': status,
        'exit_code': exit_code,
        'execution_time_seconds': round(execution_time, 2),
        'stdout_log': stdout_log,
        'stderr_log': stderr_log,
    }


def execute_batch_wrappers(
    scripts_metadata: List[Dict],
    timeout: int = 3600
) -> List[Dict]:
    """
    Execute multiple wrapper scripts in sequence.

    Args:
        scripts_metadata: List of script metadata dicts (from generator)
        timeout: Timeout per script in seconds

    Returns:
        List of execution result dicts
    """
    results = []

    for meta in scripts_metadata:
        script_path = meta['script_path']

        print(f"Executing: {str(Path(script_path))}")

        result = execute_single_wrapper(script_path, timeout)
        result['model'] = meta.get('model', 'unknown')
        result['output_dir'] = meta.get('output_dir', '')
        result['strategy'] = meta.get('strategy', '')

        results.append(result)

        print(f"  Status: {result['status']} ({result['execution_time_seconds']}s)")

    return results


def main():
    parser = argparse.ArgumentParser(
        description='Execute wrapper scripts with timeout and validation'
    )
    parser.add_argument(
        '--scripts-metadata',
        required=True,
        help='Path to scripts metadata JSON (from generator)'
    )
    parser.add_argument(
        '--timeout',
        type=int,
        default=3600,
        help='Timeout per script in seconds (default: 3600)'
    )
    parser.add_argument(
        '--output',
        help='Output file for execution results (JSON)'
    )

    args = parser.parse_args()

    # Load scripts metadata
    with open(args.scripts_metadata) as f:
        metadata_data = json.load(f)

    scripts = metadata_data.get('generated_scripts', [])

    if not scripts:
        print("No scripts to execute")
        result = {'executed_scripts': [], 'count': 0}
    else:
        # Execute all scripts
        exec_results = execute_batch_wrappers(scripts, args.timeout)

        # Summary
        success_count = sum(1 for r in exec_results if r['status'] == 'success')
        failed_count = len(exec_results) - success_count

        # Per-strategy breakdown keeps a combined (--strategy all) run legible.
        by_strategy: Dict[str, Dict[str, int]] = {}
        for r in exec_results:
            strat = r.get('strategy') or 'unknown'
            bucket = by_strategy.setdefault(strat, {'success': 0, 'failed': 0})
            bucket['success' if r['status'] == 'success' else 'failed'] += 1

        result = {
            'executed_scripts': exec_results,
            'count': len(exec_results),
            'success_count': success_count,
            'failed_count': failed_count,
            'by_strategy': by_strategy,
        }

    # Output
    output_str = json.dumps(result, indent=2)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(output_str)
    else:
        print(output_str)


if __name__ == '__main__':
    main()
