#!/usr/bin/env python3
"""
Shared task maps and command helpers for the wrapper generator.

Everything the generators used to duplicate about GenAI task naming and
weight-format normalization lives here so that the single common generator
(gen_wrapper.py) has one source of truth.
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
    """Filesystem-safe model name (matches the historical replacement rules)."""
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
# GenAI benchmark: task-specific command-line options
# --------------------------------------------------------------------------- #
def genai_task_specific_opt(task: str, route_params: Dict = None) -> str:
    """
    Build task-specific options for the GenAI benchmark.py command.

    See tools/llm_bench/benchmark.py for the per-task argument requirements.
    Returns a backslash-newline joined option string ready to append.
    """
    route_params = route_params or {}
    opts: List[str] = []

    num_iters = route_params.get('num_iters', 2)
    default_prompt_file = 'prompts/llama-2-7b-chat_l.jsonl'

    if task in ['text_gen', 'code_gen']:
        prompt_file = route_params.get('prompt_file', default_prompt_file)
        infer_count = route_params.get('max_new_tokens', 128)
        opts.append(f'-pf "{prompt_file}"')
        opts.append(f'-ic {infer_count}')
        opts.append(f'--embedding_max_length 512')

    elif task == 'visual_text_gen':
        prompt = route_params.get('prompt', 'Describe this image in detail.')
        images = route_params.get('images', 'synthetic_448x448.jpg')
        infer_count = route_params.get('max_new_tokens', 128)
        opts.append(f'-p "{prompt}"')
        opts.append(f'-i "{images}"')
        opts.append(f'-ic {infer_count}')

    elif task == 'speech_to_text':
        media = route_params.get('media', 'test_audio.wav')
        opts.append(f'--media "{media}"')

    elif task == 'image_gen':
        prompt = route_params.get('prompt', 'A beautiful sunset over mountains')
        num_steps = route_params.get('num_steps', 20)
        height = route_params.get('height', 512)
        width = route_params.get('width', 512)
        opts.append(f'-p "{prompt}"')
        opts.append(f'--num_steps {num_steps}')
        opts.append(f'--height {height}')
        opts.append(f'--width {width}')

    elif task == 'image-to-image':
        prompt = route_params.get('prompt', 'Transform this image')
        images = route_params.get('images', 'input_image.jpg')
        num_steps = route_params.get('num_steps', 20)
        opts.append(f'-p "{prompt}"')
        opts.append(f'-i "{images}"')
        opts.append(f'--num_steps {num_steps}')

    elif task == 'text-to-video':
        prompt = route_params.get('prompt', 'A car driving through a city')
        num_steps = route_params.get('num_steps', 30)
        num_frames = route_params.get('num_frames', 24)
        frame_rate = route_params.get('frame_rate', 8.0)
        opts.append(f'-p "{prompt}"')
        opts.append(f'--num_steps {num_steps}')
        opts.append(f'--num_frames {num_frames}')
        opts.append(f'--frame_rate {frame_rate}')

    elif task == 'inpainting':
        prompt = route_params.get('prompt', 'Fill the masked area')
        images = route_params.get('images', 'input_image.jpg')
        mask_image = route_params.get('mask_image', 'mask.jpg')
        num_steps = route_params.get('num_steps', 20)
        opts.append(f'-p "{prompt}"')
        opts.append(f'-i "{images}"')
        opts.append(f'-mi "{mask_image}"')
        opts.append(f'--num_steps {num_steps}')

    elif task == 'text_embed':
        prompt_file = route_params.get('prompt_file', default_prompt_file)
        opts.append(f'-pf "{prompt_file}"')

    elif task == 'text_to_speech':
        prompt = route_params.get('prompt', 'Hello, this is a test.')
        opts.append(f'-p "{prompt}"')

    elif task == 'image_cls':
        images = route_params.get('images', 'test_image.jpg')
        opts.append(f'-i "{images}"')

    elif task == 'text_rerank':
        text_file = route_params.get('text_file', 'prompts/texts_for_rerank.jsonl')
        opts.append(f'--texts_file "{text_file}"')
        prompt = route_params.get('prompt', 'what is the Intel Core Ultra AI Ability?')
        opts.append(f'-p "{prompt}"')

    elif task == 'ldm_super_resolution':
        images = route_params.get('images', 'low_res_image.jpg')
        num_steps = route_params.get('num_steps', 20)
        opts.append(f'-i "{images}"')
        opts.append(f'--num_steps {num_steps}')

    else:
        opts.append(f'-n {num_iters}')

    return ' \\\n        '.join(opts)
