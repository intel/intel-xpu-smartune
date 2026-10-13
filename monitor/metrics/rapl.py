# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Best-effort Intel RAPL package-power sampling."""

import time
from pathlib import Path
from typing import Dict, Optional, Tuple


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None


class RaplPackagePowerSampler:
    """Calculate total package power from sysfs RAPL energy counters."""

    def __init__(self) -> None:
        self._samples: Dict[Path, Tuple[int, float, int]] = {}

    def read_power_w(self) -> Optional[float]:
        now = time.monotonic()
        power_values = []

        try:
            package_paths = sorted(Path("/sys/class/powercap").glob("intel-rapl:*"))
        except OSError:
            return None

        for package_path in package_paths:
            name = _read_text(package_path / "name")
            energy_raw = _read_text(package_path / "energy_uj")
            range_raw = _read_text(package_path / "max_energy_range_uj")
            if not name or not name.startswith("package-") or not energy_raw:
                continue

            try:
                energy_uj = int(energy_raw)
                max_energy_uj = int(range_raw) if range_raw else 0
            except ValueError:
                continue

            previous = self._samples.get(package_path)
            self._samples[package_path] = (energy_uj, now, max_energy_uj)
            if previous is None:
                continue

            previous_energy_uj, previous_time, previous_max_energy_uj = previous
            elapsed = now - previous_time
            if elapsed <= 0:
                continue

            energy_delta_uj = energy_uj - previous_energy_uj
            if energy_delta_uj < 0:
                energy_delta_uj += max_energy_uj or previous_max_energy_uj
            if energy_delta_uj >= 0:
                power_values.append(energy_delta_uj / elapsed / 1_000_000)

        return round(sum(power_values), 2) if power_values else None