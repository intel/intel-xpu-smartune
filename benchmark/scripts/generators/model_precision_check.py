#!/usr/bin/env python3
"""Analyze OpenVINO XML const element_type distribution for precision validation."""

import argparse
import json
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def _get_hf_api():
    try:
        from huggingface_hub import HfApi
    except ImportError as e:
        raise RuntimeError(
            "precision checker requires huggingface_hub. "
            "Please install it (e.g. pip install huggingface_hub)."
        ) from e
    return HfApi()


def _get_hf_hub_download():
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as e:
        raise RuntimeError(
            "precision checker requires huggingface_hub. "
            "Please install it (e.g. pip install huggingface_hub)."
        ) from e
    return hf_hub_download

PRECISION_RATIO_THRESHOLDS = {
    'fp16': 0.85,
    'fp32': 0.85,
    'int8': 0.60,
}


def _expected_types_for_weight_format(weight_format: str):
    fmt = str(weight_format).strip().lower()
    if fmt == 'fp16':
        return {'f16'}
    if fmt == 'fp32':
        return {'f32'}
    if fmt == 'int8':
        return {'i8', 'u8'}
    return set()


def _dominant_pool_types_for_format(weight_format: str):
    fmt = str(weight_format).strip().lower()
    if fmt in {'fp16', 'fp32'}:
        return {'f16', 'f32', 'bf16'}
    if fmt == 'int8':
        return {'i8', 'u8'}
    return set()


def _const_element_type_stats(xml_path: Path):
    root = ET.parse(xml_path).getroot()
    bytes_by_type = {}
    count_by_type = {}
    for layer in root.findall('.//layer'):
        if layer.get('type') != 'Const':
            continue
        data = layer.find('data')
        if data is None:
            continue
        etype = (data.get('element_type') or '').strip().lower()
        if not etype:
            continue

        count_by_type[etype] = count_by_type.get(etype, 0) + 1
        size_text = (data.get('size') or '').strip()
        try:
            size_val = int(size_text) if size_text else 0
        except ValueError:
            size_val = 0
        bytes_by_type[etype] = bytes_by_type.get(etype, 0) + max(size_val, 0)

    return {
        'bytes_by_type': bytes_by_type,
        'count_by_type': count_by_type,
    }


def _format_type_distribution(bytes_by_type, total_bytes: int) -> str:
    if not bytes_by_type:
        return 'none'
    base = total_bytes if total_bytes > 0 else sum(bytes_by_type.values())
    if base <= 0:
        base = 1

    ordered = sorted(bytes_by_type.items(), key=lambda x: x[1], reverse=True)
    parts = []
    for etype, size in ordered[:8]:
        pct = (size / base) * 100.0
        parts.append(f"{etype}:{pct:.1f}%")
    return ', '.join(parts)


def evaluate_xml_precision(xml_path: Path, weight_format: str):
    expected = _expected_types_for_weight_format(weight_format)
    if not expected:
        return {
            'ok': True,
            'detail': f"precision '{weight_format}' has no strict checker, accepted",
        }

    stats = _const_element_type_stats(xml_path)
    bytes_by_type = stats['bytes_by_type']
    if not bytes_by_type:
        return {
            'ok': False,
            'detail': f'no const element_type found in {xml_path}',
        }

    fmt = str(weight_format).strip().lower()
    dominant_pool = _dominant_pool_types_for_format(fmt)
    threshold = PRECISION_RATIO_THRESHOLDS.get(fmt, 0.50)

    expected_bytes = sum(bytes_by_type.get(t, 0) for t in expected)
    pool_bytes = sum(bytes_by_type.get(t, 0) for t in dominant_pool)
    total_bytes = sum(bytes_by_type.values())
    ratio_base = pool_bytes if pool_bytes > 0 else total_bytes
    ratio = (expected_bytes / ratio_base) if ratio_base > 0 else 0.0
    distribution = _format_type_distribution(bytes_by_type, total_bytes)

    ok = expected_bytes > 0 and ratio >= threshold
    detail = (
        f"expected={sorted(expected)}, ratio={ratio:.2%}, threshold={threshold:.0%}, "
        f"ratio_base_bytes={ratio_base}, total_const_bytes={total_bytes}, "
        f"distribution={distribution}"
    )

    return {
        'ok': ok,
        'detail': detail,
        'ratio': ratio,
        'threshold': threshold,
        'expected_types': sorted(expected),
        'distribution': distribution,
    }


def _find_largest_bin_and_xml(api, candidate_id: str):
    info = api.model_info(candidate_id, files_metadata=True)
    siblings = info.siblings or []
    files = {getattr(s, 'rfilename', ''): getattr(s, 'size', None) for s in siblings}

    bin_files = [name for name in files if name and name.endswith('.bin')]
    if not bin_files:
        return '', '', 0

    largest_bin = max(bin_files, key=lambda f: files.get(f) or -1)
    largest_size = files.get(largest_bin) or 0
    xml_candidate = largest_bin[:-4] + '.xml'
    if xml_candidate in files:
        return largest_bin, xml_candidate, largest_size

    fallback_xml = 'openvino_model.xml'
    if fallback_xml in files:
        return largest_bin, fallback_xml, largest_size
    return '', '', 0


def _hf_download_file(candidate_id: str, repo_file: str, dst_dir: Path) -> Path:
    hf_hub_download = _get_hf_hub_download()
    downloaded = hf_hub_download(
        repo_id=candidate_id,
        filename=repo_file,
        local_dir=str(dst_dir),
        repo_type='model',
    )
    return Path(downloaded)


def probe_candidate_precision(candidate_id: str, weight_format: str):
    api = _get_hf_api()
    tmp_root = Path(tempfile.mkdtemp(prefix='ov_probe_'))
    try:
        largest_bin, xml_file, largest_size = _find_largest_bin_and_xml(api, candidate_id)
        if not largest_bin or not xml_file:
            return {
                'ok': False,
                'detail': 'no BIN/XML pair found in repo',
            }

        xml_path = _hf_download_file(candidate_id, xml_file, tmp_root)
        result = evaluate_xml_precision(xml_path, weight_format)
        prefix = 'PASS' if result.get('ok') else 'FAIL'
        result['detail'] = (
            f"{prefix}: largest pair {largest_bin} ({largest_size} bytes) -> {xml_file}; "
            f"{result.get('detail', '')}"
        )
        return result
    finally:
        try:
            for path in sorted(tmp_root.rglob('*'), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    path.rmdir()
            tmp_root.rmdir()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description='Validate OpenVINO XML precision by const type ratio')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--xml-path', help='Path to OpenVINO XML file')
    group.add_argument('--candidate-id', help='HF repo id to probe (largest BIN corresponding XML)')
    parser.add_argument('--weight-format', required=True, help='Target precision format (fp16/fp32/int8)')
    args = parser.parse_args()

    try:
        if args.candidate_id:
            result = probe_candidate_precision(args.candidate_id, args.weight_format)
        else:
            xml_path = Path(args.xml_path)
            if not xml_path.exists():
                print(json.dumps({'ok': False, 'detail': f'xml not found: {xml_path}'}))
                raise SystemExit(2)
            result = evaluate_xml_precision(xml_path, args.weight_format)
    except Exception as e:
        print(json.dumps({'ok': False, 'detail': str(e)}))
        raise SystemExit(2)

    print(json.dumps(result))
    raise SystemExit(0 if result.get('ok') else 2)


if __name__ == '__main__':
    main()
