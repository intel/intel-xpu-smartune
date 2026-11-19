#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Shared task maps and command helpers for the wrapper generator.

GenAI task naming and weight-format normalization live here so that the single
common generator (gen_wrapper.py) has one source of truth.
"""

import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.global_vars import load_global_vars

GLOBAL_VARS = load_global_vars()


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def get_model_safe_name(model: str) -> str:
    """Filesystem-safe model name."""
    return model.replace('/', '_').replace('.', '_')


def normalize_formats(value, default: List[str]) -> List[str]:
    """
    Normalize a `convert`/`quantize` field (str, list or None) into a
    de-duplicated, lower-cased weight-format list.

    `default` is returned when the field is empty (e.g. ['fp16'] for convert,
    ['int8'] for quantize). Pass [] to mean "nothing to generate".
    """
    if not value:
        return list(default)
    if isinstance(value, str):
        items = [v.strip() for v in value.split(',') if v.strip()]
    elif isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        items = [str(value).strip()]

    seen: set = set()
    result: List[str] = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result or list(default)


# --------------------------------------------------------------------------- #
# Task maps: optimum-cli task -> GenAI benchmark task
# --------------------------------------------------------------------------- #
GENAI_TASK_MAP = {
    # Text generation (general LLM)
    'text-generation': 'text_gen',
    'text-generation-with-past': 'text_gen',
    'text2text-generation': 'text_gen',

    # Text generation (chat/conversational)
    'conversational': 'text_gen_chat',
    'chat': 'text_gen_chat',

    # Code generation
    'code-generation': 'code_gen',
    'code-generation-with-past': 'code_gen',

    # Visual language models
    'image-text-to-text': 'visual_text_gen',
    'visual-question-answering': 'visual_text_gen',
    'image-to-text': 'visual_text_gen',

    # Speech to text
    'automatic-speech-recognition': 'speech_to_text',
    'automatic-speech-recognition-with-past': 'speech_to_text',
    'audio-classification': 'speech_to_text',

    # Image generation/processing
    'image-generation': 'image_gen',
    'text-to-image': 'text-to-image',
    'image-to-image': 'image-to-image',
    'text-to-video': 'text-to-video',
    'inpainting': 'inpainting',

    # Super resolution
    'super-resolution': 'ldm_super_resolution',
    'image-super-resolution': 'ldm_super_resolution',

    # Image classification
    'image-classification': 'image_cls',
    'zero-shot-image-classification': 'image_cls',

    # Text embeddings (default for feature-extraction/sentence-similarity)
    'feature-extraction': 'text_embed',
    'sentence-similarity': 'text_embed',
    # Note: text-classification handled separately below (rerank vs embeddings)

    # Text to speech
    'text-to-audio': 'text_to_speech',
    'text-to-speech': 'text_to_speech',
}


def to_genai_task(task: str, model: str = '') -> str:
    """Convert an optimum-cli task to the GenAI benchmark task name."""
    if task in {'feature-extraction', 'sentence-similarity', 'text-classification'}:
        model_lower = model.lower() if model else ''
        return 'text_rerank' if 'rerank' in model_lower else 'text_embed'
    return GENAI_TASK_MAP.get(task, task)


# --------------------------------------------------------------------------- #
# GenAI benchmark: task-specific defaults
# --------------------------------------------------------------------------- #
# Bare prompt-file names; bench_params.render_args prefixes the prompts/ directory
# llm_bench resolves them from.
DEFAULT_PROMPT_FILE = 'llama-2-7b-chat_l.jsonl'
DEFAULT_RERANK_TEXTS = 'texts_for_rerank.jsonl'


def genai_task_defaults(task: str, route_params: Dict = None) -> Dict:
    """
    Task-specific default parameters for the GenAI benchmark.py command.

    Keyed by bench_params canonical names (not CLI flags), so the user's own
    configuration can override any of them before the command is rendered once.
    See tools/llm_bench/benchmark.py for the per-task argument requirements.
    """
    route_params = route_params or {}
    get = route_params.get

    if task in ['text_gen', 'code_gen']:
        return {'prompt_file': get('prompt_file', DEFAULT_PROMPT_FILE),
                'infer_count': get('max_new_tokens', 128),
                'embedding_max_length': 512}
    if task == 'visual_text_gen':
        return {'prompt': get('prompt', 'Describe this image in detail.'),
                'images': get('images', 'dog.jpg'),
                'infer_count': get('max_new_tokens', 128)}
    if task == 'speech_to_text':
        return {'media': get('media', 'test_audio.wav')}
    if task == 'image_gen':
        return {'prompt': get('prompt', 'A beautiful sunset over mountains'),
                'num_steps': get('num_steps', 20),
                'height': get('height', 512),
                'width': get('width', 512)}
    if task == 'image-to-image':
        return {'prompt': get('prompt', 'Transform this image'),
                'images': get('images', 'input_image.jpg'),
                'num_steps': get('num_steps', 20)}
    if task == 'text-to-video':
        return {'prompt': get('prompt', 'A car driving through a city'),
                'num_steps': get('num_steps', 30),
                'num_frames': get('num_frames', 24),
                'frame_rate': get('frame_rate', 8.0)}
    if task == 'inpainting':
        return {'prompt': get('prompt', 'Fill the masked area'),
                'images': get('images', 'input_image.jpg'),
                'mask_image': get('mask_image', 'mask.jpg'),
                'num_steps': get('num_steps', 20)}
    if task == 'text_embed':
        return {'prompt_file': get('prompt_file', DEFAULT_PROMPT_FILE)}
    if task == 'text_to_speech':
        return {'prompt': get('prompt', 'Hello, this is a test.')}
    if task == 'image_cls':
        return {'images': get('images', 'test_image.jpg')}
    if task == 'text_rerank':
        return {'texts_file': get('text_file', DEFAULT_RERANK_TEXTS),
                'prompt': get('prompt', 'what is the Intel Core Ultra AI Ability?')}
    if task == 'ldm_super_resolution':
        return {'images': get('images', 'low_res_image.jpg'),
                'num_steps': get('num_steps', 20)}
    return {'num_iters': get('num_iters', 2)}
