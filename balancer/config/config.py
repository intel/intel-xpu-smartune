# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

@dataclass
class Config:
    cgroup_mount: str = "/sys/fs/cgroup"
    vendor: str = "generic"
    thresholds: dict = None
    weights: dict = None
    weights_top: dict = None
    dominant_app_reduce_factor: float = 3.0
    workloads: dict = None
    app_priority: dict = None
    limit_policy: dict = None
    blacklist: list = None
    cooldown_time: float = 15
    cpu_busy_threshold: float = 90
    memory_busy_threshold: float = 90
    disk_utilization_threshold: float = 95
    disk_iowait_threshold: float = 10
    disk_io_throughput_threshold_kb: float = 102400  # KB/s, i.e. 100 MB/s
    regular_update_sys_pressure_time: float = 5
    monitor_idle_check_interval: float = 10
    network_thresholds: dict = None
    network_interface: dict = None
    network_bandwidth_kbit: int = 1000000 #kbit/s
    enable_network_control: bool = True
    config_network_bw: dict = None
    testing_network_app: list = None
    network_burst_map: dict = None
    network_system_ports: list = None
    monitor_apps: dict = None
    all_apps: dict = None
    controlled_apps: list = None
    _config_path: str = field(default="config/config.yaml", repr=False, compare=False)
    _persist_lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False, init=False)

    @classmethod
    def from_file(cls, path: str):
        with open(path) as f:
            data = yaml.safe_load(f)
        cfg = cls(**data)
        cfg._config_path = path
        return cfg

    @staticmethod
    def _format_yaml_scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    @classmethod
    def _replace_yaml_scalar_line(cls, lines: list[str], path: tuple[str, ...], value: Any) -> bool:
        key_pattern = re.compile(r"^(?P<indent>\s*)(?P<key>[^:#]+):(?P<rest>.*)$")
        parent_idx = -1
        parent_indent = -2

        for depth, key in enumerate(path):
            target_indent = parent_indent + 2
            found_idx = None

            for idx in range(parent_idx + 1, len(lines)):
                raw_line = lines[idx]
                match = key_pattern.match(raw_line)
                if not match:
                    continue

                indent_len = len(match.group("indent"))
                if parent_idx >= 0 and indent_len <= parent_indent:
                    break
                if indent_len != target_indent:
                    continue
                if match.group("key").strip() != key:
                    continue

                found_idx = idx
                parent_idx = idx
                parent_indent = indent_len
                break

            if found_idx is None:
                return False

            if depth == len(path) - 1:
                line = lines[found_idx]
                comment = ""
                if "#" in line:
                    value_part, comment_part = line.split("#", 1)
                    line = value_part.rstrip()
                    comment = "  #" + comment_part.strip()
                prefix = line.split(":", 1)[0]
                lines[found_idx] = f"{prefix}: {cls._format_yaml_scalar(value)}{comment}\n"
                return True

        return False

    def _patch_limit_policy_yaml(self, path_values: dict[tuple[str, ...], Any], path: Optional[str] = None) -> bool:
        target = path or self._config_path
        with open(target, "r", encoding="utf-8") as f:
            lines = f.readlines()

        changed = False
        for yaml_path, value in path_values.items():
            changed = self._replace_yaml_scalar_line(lines, yaml_path, value) or changed

        if changed:
            with open(target, "w", encoding="utf-8") as f:
                f.writelines(lines)
        return changed

    def update_limit_policy_for_priority(self, priority: str, limit_overrides: Optional[dict[str, Any]]) -> bool:
        # NOTE: This method mutates the shared global limit_policy used by the auto-balancer.
        # It is no longer called from the manual-limit flow (set_resource_limit now stores
        # per-app overrides in the DB via limit_overrides_json).  Retained for admin/scripting
        # use only; do not call it for per-app UI limits.
        if not isinstance(limit_overrides, dict):
            return False

        p = (priority or "undefined").lower()
        modified = False
        yaml_updates: dict[tuple[str, ...], Any] = {}

        with self._persist_lock:
            if not isinstance(self.limit_policy, dict):
                self.limit_policy = {}

            policy = self.limit_policy

            def _ensure_resource_cfg(name: str) -> dict[str, Any]:
                cfg = policy.get(name)
                if not isinstance(cfg, dict):
                    cfg = {}
                    policy[name] = cfg
                rates = cfg.get("rate")
                if not isinstance(rates, dict):
                    cfg["rate"] = {}
                return cfg

            def _set_enabled(cfg: dict[str, Any], override_cfg: dict[str, Any]):
                nonlocal modified
                if "enabled" in override_cfg:
                    enabled = bool(override_cfg.get("enabled"))
                    if cfg.get("enabled") != enabled:
                        cfg["enabled"] = enabled
                        yaml_updates[("limit_policy", resource_name, "enabled")] = enabled
                        modified = True

            cpu_ovr = limit_overrides.get("cpu")
            if isinstance(cpu_ovr, dict):
                resource_name = "cpu"
                cpu_cfg = _ensure_resource_cfg("cpu")
                _set_enabled(cpu_cfg, cpu_ovr)
                if "rate" in cpu_ovr and cpu_ovr.get("rate") is not None:
                    try:
                        cpu_rate = float(cpu_ovr["rate"])
                        if cpu_cfg["rate"].get(p) != cpu_rate:
                            cpu_cfg["rate"][p] = cpu_rate
                            yaml_updates[("limit_policy", "cpu", "rate", p)] = cpu_rate
                            modified = True
                    except (TypeError, ValueError):
                        pass

            mem_ovr = limit_overrides.get("memory")
            if isinstance(mem_ovr, dict):
                resource_name = "memory"
                mem_cfg = _ensure_resource_cfg("memory")
                _set_enabled(mem_cfg, mem_ovr)
                if "rate" in mem_ovr and mem_ovr.get("rate") is not None:
                    try:
                        mem_rate = float(mem_ovr["rate"])
                        if mem_cfg["rate"].get(p) != mem_rate:
                            mem_cfg["rate"][p] = mem_rate
                            yaml_updates[("limit_policy", "memory", "rate", p)] = mem_rate
                            modified = True
                    except (TypeError, ValueError):
                        pass

            disk_ovr = limit_overrides.get("disk_io")
            if isinstance(disk_ovr, dict):
                resource_name = "disk_io"
                disk_cfg = _ensure_resource_cfg("disk_io")
                _set_enabled(disk_cfg, disk_ovr)
                disk_rate_raw = disk_ovr.get("rate")
                if isinstance(disk_rate_raw, dict):
                    existing = disk_cfg["rate"].get(p)
                    disk_rate_cfg = existing.copy() if isinstance(existing, dict) else {}
                    for key in ("write", "read", "write_iops", "read_iops"):
                        if key not in disk_rate_raw:
                            continue
                        try:
                            value = max(1, int(float(disk_rate_raw[key])))
                        except (TypeError, ValueError):
                            continue
                        if disk_rate_cfg.get(key) != value:
                            disk_rate_cfg[key] = value
                            yaml_updates[("limit_policy", "disk_io", "rate", p, key)] = value
                            modified = True
                    if disk_rate_cfg:
                        disk_cfg["rate"][p] = disk_rate_cfg

            if modified:
                logger.info(f"Configuration updated: {section} - {list(yaml_updates.keys())}")
                self._patch_limit_policy_yaml(yaml_updates, self._config_path)

        return modified


    def update_config_section(self, section: str, updates: dict[str, Any]) -> bool:
        """Generic method to update a config section (e.g., weights_top, thresholds, etc.).

        Args:
            section: The top-level config section name (e.g., 'weights_top', 'thresholds')
            updates: Dictionary with key-value pairs to update within that section

        Returns:
            True if config was updated successfully, False otherwise
        """
        from utils.logger import logger

        if not isinstance(updates, dict) or not section:
            return False

        modified = False
        yaml_updates = {}

        with self._persist_lock:
            # Get or create the section
            section_data = getattr(self, section, None)
            if section_data is None:
                setattr(self, section, {})
                section_data = {}
            elif not isinstance(section_data, dict):
                # Section exists but is not a dict, create new dict
                setattr(self, section, {})
                section_data = {}

            # Update each key-value pair
            for key, value in updates.items():
                try:
                    # Type conversion based on existing type or default to int for weights
                    if section_data and key in section_data:
                        # Match existing type
                        existing_type = type(section_data[key])
                        if existing_type in (int, float, bool, str):
                            new_value = existing_type(value)
                        else:
                            new_value = value
                    else:
                        # Default conversion for common types
                        if isinstance(value, (int, float, bool, str)):
                            new_value = value
                        else:
                            new_value = value

                    if section_data.get(key) != new_value:
                        section_data[key] = new_value
                        yaml_updates[(section, key)] = new_value
                        modified = True
                except (TypeError, ValueError) as e:
                    logger.warning(f"Could not update {section}.{key}: {e}")
                    pass

            # Update the section attribute
            setattr(self, section, section_data)

            if modified:
                logger.info(f"Configuration updated: {section} - {list(yaml_updates.keys())}")
                self._patch_limit_policy_yaml(yaml_updates, self._config_path)

        return modified


b_config = Config.from_file("config/config.yaml")
