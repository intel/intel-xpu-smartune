# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""GPU subsystem: card discovery, engines, frequencies, VRAM, PCIe, EU count."""

import glob
import os
import re
from typing import Any, Dict, List, Optional

import psutil

from monitor.gpu_monitor import is_integrated_pci_slot
from monitor.metrics.utils import safe_read, read_first_existing, parse_freq_val, run_cmd
from utils.logger import get_logger
logger = get_logger(__name__)

INTEL_GPU_DRIVERS = ("i915", "xe")


def get_gpu_cards() -> List[str]:
    cards = sorted(glob.glob("/sys/class/drm/card[0-9]"))
    return [
        c for c in cards
        if os.path.isdir(c) and get_gpu_driver_name(c) in INTEL_GPU_DRIVERS
    ]


# Cached card_name -> 'GPU.N' mapping.  The DRM topology is static for the
# life of the process, so we compute each label once on first access and reuse
# the result on subsequent calls.
_gpu_label_cache: Dict[str, str] = {}


def card_to_gpu_label(card: str) -> str:
    """Map a DRM card path or name (e.g. '/sys/class/drm/card0' or 'card0') to
    the public-facing 'GPU.N' label, where N is derived from the paired
    renderD node (N = renderD_number - 128).  Card and render node are matched
    by shared PCI device.  This matches OpenCL / Level Zero enumeration order
    which follows /dev/dri/renderD* openings.

    Falls back to the original card name when no render node is found or the
    input is not a recognisable 'cardN'.
    """
    card_name = os.path.basename(card)
    cached = _gpu_label_cache.get(card_name)
    if cached is not None:
        return cached

    label = card_name
    try:
        card_pci = os.path.realpath(f"/sys/class/drm/{card_name}/device")
        entries = os.listdir("/sys/class/drm")
        for name in entries:
            if not name.startswith("renderD"):
                continue
            try:
                render_pci = os.path.realpath(f"/sys/class/drm/{name}/device")
            except OSError:
                continue
            if render_pci == card_pci:
                try:
                    label = f"GPU.{int(name[len('renderD'):]) - 128}"
                except ValueError:
                    pass
                break
    except OSError:
        pass

    _gpu_label_cache[card_name] = label
    return label


def get_gpu_names() -> List[str]:
    output = run_cmd(["lspci", "-nn"])
    if not output:
        return []
    matches = []
    for line in output.splitlines():
        if re.search(r"VGA|3D|Display", line, re.IGNORECASE) and "[8086:" in line:
            matches.append(line.strip())
    return matches


def get_gpu_engines(cards: List[str]) -> Dict[str, List[str]]:
    engines: Dict[str, List[str]] = {}
    for card in cards:
        gpu_label = card_to_gpu_label(card)

        driver = get_gpu_driver_name(card)

        if driver == "i915":
            # i915: /sys/class/drm/cardX/engine/ has instance entries (bcs0, rcs0, ...)
            engine_root = os.path.join(card, "engine")
            if os.path.isdir(engine_root):
                names: List[str] = []
                for entry in sorted(os.listdir(engine_root)):
                    name_path = os.path.join(engine_root, entry, "name")
                    name = safe_read(name_path)
                    names.append(name or entry)
                if names:
                    engines[gpu_label] = names

        elif driver == "xe":
            # Xe: /sys/class/drm/cardX/device/tile*/gt*/engines/ has class dirs (bcs, ccs, ...)
            xe_names: List[str] = []
            for gt_dir in sorted(glob.glob(os.path.join(card, "device", "tile*", "gt*"))):
                engines_dir = os.path.join(gt_dir, "engines")
                if not os.path.isdir(engines_dir):
                    continue
                try:
                    for entry in sorted(os.listdir(engines_dir)):
                        if normalize_gpu_engine_name(entry) is not None:
                            xe_names.append(entry)
                except OSError:
                    continue
            if xe_names:
                engines[gpu_label] = xe_names

    return engines


def normalize_gpu_engine_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    lowered = str(name).strip().lower()
    if not lowered:
        return None

    if "video-enhance" in lowered or lowered.startswith("vecs"):
        return "vecs"
    if "video" in lowered or lowered.startswith("vcs"):
        return "vcs"
    if "render" in lowered or lowered.startswith("rcs"):
        return "rcs"
    if "copy" in lowered or lowered.startswith("bcs"):
        return "bcs"
    if "compute" in lowered or lowered.startswith("ccs"):
        return "ccs"
    return None


def normalize_gpu_engines(engines: List[str]) -> List[str]:
    """Deduplicate engine names to type-level keys (e.g. ccs0,ccs1 -> ccs)."""
    normalized = {
        engine
        for engine in (normalize_gpu_engine_name(name) for name in engines)
        if engine
    }
    order = ["bcs", "ccs", "rcs", "vcs", "vecs"]
    return [engine for engine in order if engine in normalized]


_ENGINE_TYPE_ORDER = {"bcs": 0, "ccs": 1, "rcs": 2, "vcs": 3, "vecs": 4}


def sort_gpu_engine_instances(engines: List[str]) -> List[str]:
    """Filter and sort engine instance names, preserving instance numbers.

    Input:  ['ccs3', 'ccs0', 'bcs0', 'rcs0', 'ccs1', 'vcs0', 'ccs2', 'vcs1', 'vecs0']
    Output: ['bcs0', 'ccs0', 'ccs1', 'ccs2', 'ccs3', 'rcs0', 'vcs0', 'vcs1', 'vecs0']
    """
    result: List[str] = []
    seen: set = set()
    for name in engines:
        lowered = name.strip().lower()
        if not lowered or lowered in seen:
            continue
        engine_type = normalize_gpu_engine_name(lowered)
        if engine_type is None:
            continue
        seen.add(lowered)
        result.append(lowered)

    result.sort(key=lambda n: (_ENGINE_TYPE_ORDER.get(normalize_gpu_engine_name(n) or "", 99), n))
    return result


def get_gpu_freq_bounds(cards: List[str]) -> Dict[str, Dict[str, Optional[float]]]:
    """Legacy flat bounds (GT0 only) kept for backward compatibility."""
    result: Dict[str, Dict[str, Optional[float]]] = {}
    for card in cards:
        min_paths = [
            os.path.join(card, "gt_min_freq_mhz"),
            os.path.join(card, "device", "gt_min_freq_mhz"),
            os.path.join(card, "gt", "gt0", "rps_min_freq_mhz"),
            os.path.join(card, "gt", "gt0", "min_freq_mhz"),
        ]
        max_paths = [
            os.path.join(card, "gt_max_freq_mhz"),
            os.path.join(card, "device", "gt_max_freq_mhz"),
            os.path.join(card, "gt", "gt0", "rps_max_freq_mhz"),
            os.path.join(card, "gt", "gt0", "max_freq_mhz"),
        ]
        min_val = read_first_existing(min_paths)
        max_val = read_first_existing(max_paths)
        if min_val or max_val:
            result[card_to_gpu_label(card)] = {
                "min_mhz": parse_freq_val(min_val),
                "max_mhz": parse_freq_val(max_val),
            }
    return result


def get_gpu_driver_name(card: str) -> Optional[str]:
    driver_path = os.path.join(card, "device", "driver")
    if not os.path.exists(driver_path):
        return None
    try:
        resolved = os.path.realpath(driver_path)
    except Exception:
        return None
    name = os.path.basename(resolved)
    return name or None


def get_gpu_gt_freq_bounds_sysfs(
    cards: List[str],
) -> Dict[str, Dict[str, Dict[str, Optional[float]]]]:
    """Read per-GT frequency bounds directly from sysfs."""
    result: Dict[str, Dict[str, Dict[str, Optional[float]]]] = {}

    for card in cards:
        gpu_label = card_to_gpu_label(card)
        driver = get_gpu_driver_name(card)
        gt_bounds: Dict[str, Dict[str, Optional[float]]] = {}

        if driver == "xe":
            tile_root = os.path.join(card, "device")
            for tile_dir in sorted(glob.glob(os.path.join(tile_root, "tile*"))):
                if not os.path.isdir(tile_dir):
                    continue
                for gt_dir in sorted(glob.glob(os.path.join(tile_dir, "gt*"))):
                    if not os.path.isdir(gt_dir):
                        continue
                    gt_name = os.path.basename(gt_dir).lower()
                    freq0_dir = os.path.join(gt_dir, "freq0")
                    min_val = safe_read(os.path.join(freq0_dir, "min_freq"))
                    max_val = safe_read(os.path.join(freq0_dir, "max_freq"))
                    if min_val or max_val:
                        gt_bounds[gt_name] = {
                            "min_mhz": parse_freq_val(min_val),
                            "max_mhz": parse_freq_val(max_val),
                        }

        elif driver == "i915":
            gt_root = os.path.join(card, "gt")
            for gt_dir in sorted(glob.glob(os.path.join(gt_root, "gt*"))):
                if not os.path.isdir(gt_dir):
                    continue
                gt_name = os.path.basename(gt_dir).lower()
                min_val = safe_read(os.path.join(gt_dir, "rps_min_freq_mhz"))
                max_val = safe_read(os.path.join(gt_dir, "rps_max_freq_mhz"))
                if min_val or max_val:
                    gt_bounds[gt_name] = {
                        "min_mhz": parse_freq_val(min_val),
                        "max_mhz": parse_freq_val(max_val),
                    }

        if gt_bounds:
            result[gpu_label] = gt_bounds

    return result



_EMPTY_VRAM: Dict[str, Optional[float]] = {
    "total_bytes": None,
    "used_bytes": None,
    "usage_percent": None,
}

# GPU labels already warned about missing VRAM, so periodic sampling logs once.
_vram_warned: set = set()


def _get_card_pci_slot(card: str) -> Optional[str]:
    """Return the PCI address of a DRM card (e.g. 0000:03:00.0), or None."""
    try:
        pci_addr = os.path.basename(os.path.realpath(os.path.join(card, "device")))
    except Exception:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", pci_addr):
        return pci_addr
    return None


def _parse_xe_vram_mm(card_index: int) -> Dict[str, Optional[float]]:
    """Parse size/usage from Xe debugfs tile*/vram_mm, summed over all tiles."""
    paths = sorted(glob.glob(f"/sys/kernel/debug/dri/{card_index}/tile*/vram_mm"))
    total_bytes = 0.0
    used_bytes = 0.0
    found = False
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as exc:
            logger.debug("Read failed for %s: %s", path, exc)
            continue

        size_match = re.search(r"(?m)^\s*size:\s*(\d+)\s*$", content)
        if not size_match:
            size_match = re.search(r"(?m)^\s*man\s+size\s*:\s*(\d+)\s*$", content)
        usage_match = re.search(r"(?m)^\s*usage:\s*(\d+)\s*$", content)
        if not size_match or not usage_match:
            logger.debug("No size/usage found in %s", path)
            continue

        total_bytes += float(size_match.group(1))
        used_bytes += float(usage_match.group(1))
        found = True

    if not found:
        return {}

    usage_percent = round((used_bytes / total_bytes) * 100, 2) if total_bytes > 0 else None
    return {
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "usage_percent": usage_percent,
    }


def _parse_i915_gem_objects_vram(card_index: int) -> Dict[str, Optional[float]]:
    """Parse local memory from i915_gem_objects debugfs (i915 dGPU only).

    Uses the buddy allocator line "chunk_size: ..., total: XMiB, free: YMiB",
    summed over all local regions; it covers the whole LMEM regardless of BAR
    size. The TTM "size:"/"usage:" lines are not used because i915 reports
    size in pages but usage in bytes. Falls back to visible_size/visible_avail
    (CPU-visible BAR window only) if no buddy line is present.
    """
    path = f"/sys/kernel/debug/dri/{card_index}/i915_gem_objects"
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception as exc:
        logger.debug("Read failed for %s: %s", path, exc)
        return {}

    mib = 1024 * 1024
    buddy = re.findall(r"(?m)^\s*chunk_size:.*?total:\s*(\d+)\s*MiB,\s*free:\s*(\d+)\s*MiB", content)
    if buddy:
        total_bytes = sum(float(t) for t, _ in buddy) * mib
        free_bytes = sum(float(f) for _, f in buddy) * mib
    else:
        size_match = re.search(r"(?m)^\s*visible_size:\s*([0-9]+)\s*MiB", content)
        avail_match = re.search(r"(?m)^\s*visible_avail:\s*([0-9]+)\s*MiB", content)
        if not size_match or not avail_match:
            return {}
        total_bytes = float(size_match.group(1)) * mib
        free_bytes = float(avail_match.group(1)) * mib

    used_bytes = max(0.0, total_bytes - free_bytes)
    usage_percent = round((used_bytes / total_bytes) * 100, 2) if total_bytes > 0 else None

    return {
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "usage_percent": usage_percent,
    }


def _get_system_memory_usage_bytes() -> Dict[str, Optional[float]]:
    mem = psutil.virtual_memory()
    total_bytes = float(mem.total) if mem.total else None
    used_bytes = float(mem.used) if mem.used else None
    usage_percent = None
    if total_bytes is not None and total_bytes > 0 and used_bytes is not None:
        usage_percent = round((used_bytes / total_bytes) * 100, 2)
    return {
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "usage_percent": usage_percent,
    }


def get_gpu_vram(cards: List[str]) -> Dict[str, Dict[str, Optional[float]]]:
    """Per-GPU memory: iGPU shares system memory; dGPU reads driver debugfs.

    Each entry also carries "pci_slot" so consumers can match it to a device.
    A dGPU whose debugfs cannot be read (e.g. not running as root) reports
    all-None values rather than falling back to system memory.
    """
    result: Dict[str, Dict[str, Any]] = {}
    system_memory_stats: Optional[Dict[str, Optional[float]]] = None

    for card in cards:
        gpu_label = card_to_gpu_label(card)
        pci_slot = _get_card_pci_slot(card)
        vram: Dict[str, Optional[float]] = {}

        if is_integrated_pci_slot(pci_slot):
            if system_memory_stats is None:
                system_memory_stats = _get_system_memory_usage_bytes()
            vram = dict(system_memory_stats)
        else:
            match = re.match(r"^card(\d+)$", os.path.basename(card))
            driver = get_gpu_driver_name(card)
            if match:
                card_index = int(match.group(1))
                if driver == "xe":
                    vram = _parse_xe_vram_mm(card_index)
                elif driver == "i915":
                    vram = _parse_i915_gem_objects_vram(card_index)

            if not vram:
                if gpu_label not in _vram_warned:
                    _vram_warned.add(gpu_label)
                    logger.warning(
                        "dGPU VRAM unavailable for %s (driver=%s): debugfs unreadable "
                        "(need root / debugfs mounted?)",
                        gpu_label, driver,
                    )
                vram = dict(_EMPTY_VRAM)

        result[gpu_label] = {**vram, "pci_slot": pci_slot}
    return result


def get_igpu_eu_count(cards: List[str]) -> Dict[str, Optional[int]]:
    """Read EU count per card."""
    result: Dict[str, Optional[int]] = {}
    for card in cards:
        card_name = os.path.basename(card)
        gpu_label = card_to_gpu_label(card)
        match = re.match(r"^card(\d+)$", card_name)
        if not match:
            continue
        card_index = int(match.group(1))
        driver = get_gpu_driver_name(card)

        if driver == "i915":
            path = f"/sys/kernel/debug/dri/{card_index}/i915_sseu_status"
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                eu_match = re.search(r"(?m)^\s*Available EU Total:\s*(\d+)", content)
                if eu_match:
                    result[gpu_label] = int(eu_match.group(1))
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.debug("Read failed for %s: %s", path, exc)

        elif driver == "xe":
            result[gpu_label] = None

    return result


def get_gpu_pcie(cards: List[str]) -> Dict[str, Dict[str, Optional[str]]]:
    result: Dict[str, Dict[str, Optional[str]]] = {}
    for card in cards:
        base = os.path.join(card, "device")
        current_speed = safe_read(os.path.join(base, "current_link_speed"))
        current_width = safe_read(os.path.join(base, "current_link_width"))
        max_speed = safe_read(os.path.join(base, "max_link_speed"))
        max_width = safe_read(os.path.join(base, "max_link_width"))
        has_real_speed = (current_speed and "GT/s" in current_speed) or (max_speed and "GT/s" in max_speed)
        if has_real_speed:
            result[card_to_gpu_label(card)] = {
                "current_speed": current_speed,
                "current_width": current_width,
                "max_speed": max_speed,
                "max_width": max_width,
            }
    return result


def get_gpu_pci_addresses(cards: List[str]) -> Dict[str, str]:
    """Return mapping of GPU label -> PCI address (e.g. GPU.0 -> 0000:00:02.0)."""
    result: Dict[str, str] = {}
    for card in cards:
        pci_addr = _get_card_pci_slot(card)
        if pci_addr:
            result[card_to_gpu_label(card)] = pci_addr
    return result
