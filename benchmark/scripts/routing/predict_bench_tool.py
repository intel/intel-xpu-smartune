#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

#Which benchmark tool (genai / benchmark_app / notebook) a model will get.


import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / 'generators'))
sys.path.insert(0, str(Path(__file__).parent.parent))

import route  # noqa: E402
import task_maps as tm  # noqa: E402
from params import bench_params as bp  # noqa: E402


def _routes(model_id: str, models_dir: str) -> list:
    safe = route._model_safe_name(model_id)
    return route.route_benchmark_from_dir(models_dir, allowed_safe_names={safe}).get('routes') or []


def _defaults(model_id: str, routes: list) -> dict:
    """Default parameters per routed tool, as gen_wrapper.py would resolve them."""
    defaults = {}
    by_tool = {}
    for r in routes:
        by_tool.setdefault(r.get('strategy'), r)  # one representative precision
    if 'genai' in by_tool:
        params = by_tool['genai'].get('parameters') or {}
        task = tm.to_genai_task(params.get('task', 'text-generation-with-past'), model_id)
        schema = bp.merged_schema('genai')
        defaults['genai'] = bp.resolve('genai', schema, tm.genai_task_defaults(task, params), {})
    if 'benchmark_app' in by_tool:
        # gen_wrapper owns the shape detection; imported only when needed, it
        # brings jinja2 and the generator's globals with it.
        import gen_wrapper
        r = by_tool['benchmark_app']
        model_xml = (r.get('parameters') or {}).get('model_xml', '')
        task_defaults = {}
        if model_xml and Path(model_xml).exists():
            spec = gen_wrapper.analyze_model_input_shapes(model_xml)
            if spec:
                task_defaults['shape'] = spec[len('-shape '):] if spec.startswith('-shape ') else spec
        schema = bp.merged_schema('benchmark_app')
        defaults['benchmark_app'] = bp.resolve('benchmark_app', schema, task_defaults, {})
    return defaults


def main() -> int:
    parser = argparse.ArgumentParser(description="Which benchmark tool a model will get.")
    parser.add_argument('--model', required=True)
    parser.add_argument('--models-dir', required=True)
    args = parser.parse_args()

    out = {'model': args.model}
    routes = _routes(args.model, args.models_dir) if Path(args.models_dir).is_dir() else []
    if routes:
        precisions = {}
        for r in routes:
            params = r.get('parameters') or {}
            task = params.get('task')
            precisions[params.get('precision') or '?'] = {
                'tool': r.get('strategy'),
                'task': tm.to_genai_task(task, args.model) if task else None,
            }
        # Precisions of one model nearly always route alike; when they do not,
        # the page shows both tabs anyway and names which precisions each takes.
        tools = sorted({v['tool'] for v in precisions.values() if v['tool']})
        tasks = sorted({v['task'] for v in precisions.values() if v['task']})
        out.update(source='built', tool=tools[0] if len(tools) == 1 else None,
                   tools=tools, task=tasks[0] if tasks else None, precisions=precisions,
                   defaults=_defaults(args.model, routes))
    else:
        out.update(source='not_built', tool=None, tools=[], task=None)
    print(json.dumps(out))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
