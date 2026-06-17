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
    passive_resource_control: dict = None
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


    # ------------------------------------------------------------------
    # Generic YAML list helpers
    #
    # The wizard / future UI editors need to add, remove and edit items in
    # top-level YAML lists (controlled_apps, network_system_ports,
    # testing_network_app, ...).  These helpers operate on *any* top-level
    # list section while preserving comments and the existing indentation
    # style, so each new "UI-editable section" does not need a bespoke
    # YAML patcher.
    # ------------------------------------------------------------------

    @staticmethod
    def _format_yaml_value(value: Any) -> str:
        """Render a Python value as an inline YAML scalar / flow-collection."""
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return f"{value}"
        if isinstance(value, list):
            return "[" + ", ".join(Config._format_yaml_value(v) for v in value) + "]"
        if isinstance(value, dict):
            inner = ", ".join(
                f"{Config._format_yaml_value(k)}: {Config._format_yaml_value(v)}"
                for k, v in value.items()
            )
            return "{" + inner + "}"
        # String — escape and wrap in double quotes.
        s = str(value).replace("\\", "\\\\").replace("\"", "\\\"")
        return f"\"{s}\""

    @staticmethod
    def _find_top_level_key(lines: list[str], section: str) -> int:
        """Return the line index of ``section:`` at indent 0, or -1."""
        prefix = f"{section}:"
        for idx, line in enumerate(lines):
            if line.startswith(prefix) and (
                len(line) == len(prefix)
                or line[len(prefix)] in (" ", "\t", "\n", "\r", "#")
            ):
                return idx
        return -1

    @staticmethod
    def _find_block_end(lines: list[str], start_idx: int) -> int:
        """Return the index of the first line *after* the YAML block whose
        header lives at ``start_idx``.  The block ends at the next non-blank,
        non-comment line whose indentation is 0 (a sibling top-level key)
        or at EOF."""
        for idx in range(start_idx + 1, len(lines)):
            line = lines[idx]
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                continue
            if line[0] not in (" ", "\t"):
                return idx
        return len(lines)

    @staticmethod
    def _detect_list_item_indent(lines: list[str], header_idx: int, end_idx: int) -> tuple[int, int]:
        """Look at the existing items in this list block to figure out the
        indent of ``- `` (dash_indent) and of mapping sub-keys (subkey_indent).
        Falls back to (2, 4) if the block is empty."""
        for idx in range(header_idx + 1, end_idx):
            line = lines[idx]
            stripped = line.lstrip(" \t")
            if stripped.startswith("- "):
                dash_indent = len(line) - len(stripped)
                # Try to find a sub-key on a subsequent line at higher indent.
                for jdx in range(idx + 1, end_idx):
                    sub = lines[jdx]
                    s_stripped = sub.lstrip(" \t")
                    if not s_stripped or s_stripped.startswith("#"):
                        continue
                    sub_indent = len(sub) - len(s_stripped)
                    if sub_indent > dash_indent:
                        return dash_indent, sub_indent
                    break
                return dash_indent, dash_indent + 2
        return 2, 4

    def _render_list_item(self, value: Any, dash_indent: int, subkey_indent: int) -> list[str]:
        """Render ``value`` as one or more YAML lines suitable for inserting
        into a sequence block.  Mapping values are expanded over multiple
        lines (one key per line); scalars become a single ``- value`` line."""
        dash_pad = " " * dash_indent
        sub_pad = " " * subkey_indent

        if isinstance(value, dict):
            keys = list(value.keys())
            if not keys:
                return [f"{dash_pad}- {{}}\n"]
            out = [f"{dash_pad}- {keys[0]}: {self._format_yaml_value(value[keys[0]])}\n"]
            for k in keys[1:]:
                out.append(f"{sub_pad}{k}: {self._format_yaml_value(value[k])}\n")
            return out

        return [f"{dash_pad}- {self._format_yaml_value(value)}\n"]

    def append_to_list_section(
        self,
        section: str,
        entry: Any,
        path: Optional[str] = None,
    ) -> bool:
        """Append ``entry`` to the top-level list named ``section`` in YAML.

        ``entry`` may be a dict (rendered as a multi-line mapping item) or a
        scalar / list / nested dict (rendered inline).  Comments and the
        existing indentation style are preserved.  The in-memory attribute
        ``self.<section>`` is updated to match.
        """
        from utils.logger import logger

        target = path or self._config_path
        with self._persist_lock:
            with open(target, "r", encoding="utf-8") as f:
                lines = f.readlines()

            header_idx = self._find_top_level_key(lines, section)
            if header_idx == -1:
                logger.warning(f"append_to_list_section: section '{section}:' not found")
                return False

            end_idx = self._find_block_end(lines, header_idx)
            dash_indent, subkey_indent = self._detect_list_item_indent(lines, header_idx, end_idx)

            new_lines = self._render_list_item(entry, dash_indent, subkey_indent)

            # Insert just before end_idx, walking back over trailing blank
            # lines so the new item lives inside the block.
            insert_idx = end_idx
            while insert_idx > header_idx + 1 and not lines[insert_idx - 1].strip():
                insert_idx -= 1

            lines[insert_idx:insert_idx] = new_lines

            with open(target, "w", encoding="utf-8") as f:
                f.writelines(lines)

            existing = getattr(self, section, None)
            if not isinstance(existing, list):
                existing = []
            existing.append(entry)
            setattr(self, section, existing)

        logger.info(f"append_to_list_section: appended to '{section}'")
        return True

    def remove_from_list_section(
        self,
        section: str,
        match: dict[str, Any],
        path: Optional[str] = None,
    ) -> int:
        """Remove every item from list ``section`` whose keys match all of
        ``match``.  Returns the number of items removed.  Only supports
        list-of-mapping sections; scalar lists should use a different helper.
        """
        from utils.logger import logger

        if not match:
            return 0

        target = path or self._config_path
        with self._persist_lock:
            with open(target, "r", encoding="utf-8") as f:
                lines = f.readlines()

            header_idx = self._find_top_level_key(lines, section)
            if header_idx == -1:
                return 0
            end_idx = self._find_block_end(lines, header_idx)
            dash_indent, _ = self._detect_list_item_indent(lines, header_idx, end_idx)
            dash_prefix = " " * dash_indent + "- "

            # Gather (start, end) ranges for each list item.
            item_ranges: list[tuple[int, int]] = []
            cur_start = -1
            for idx in range(header_idx + 1, end_idx):
                line = lines[idx]
                if line.startswith(dash_prefix):
                    if cur_start != -1:
                        item_ranges.append((cur_start, idx))
                    cur_start = idx
            if cur_start != -1:
                item_ranges.append((cur_start, end_idx))

            removed_ranges: list[tuple[int, int]] = []
            for (s, e) in item_ranges:
                # Crude key:value scan within the item block.
                fields: dict[str, str] = {}
                for ln in lines[s:e]:
                    stripped = ln.strip()
                    if stripped.startswith("- "):
                        stripped = stripped[2:]
                    if ":" not in stripped or stripped.startswith("#"):
                        continue
                    k, _, v = stripped.partition(":")
                    fields[k.strip()] = v.strip().strip("\"'")
                if all(str(fields.get(k, "")) == str(v) for k, v in match.items()):
                    removed_ranges.append((s, e))

            if not removed_ranges:
                return 0

            # Delete from the bottom up so earlier indices stay valid.
            for s, e in reversed(removed_ranges):
                del lines[s:e]

            with open(target, "w", encoding="utf-8") as f:
                f.writelines(lines)

            existing = getattr(self, section, None)
            if isinstance(existing, list):
                setattr(
                    self,
                    section,
                    [
                        item
                        for item in existing
                        if not (
                            isinstance(item, dict)
                            and all(str(item.get(k, "")) == str(v) for k, v in match.items())
                        )
                    ],
                )

        logger.info(f"remove_from_list_section: removed {len(removed_ranges)} item(s) from '{section}'")
        return len(removed_ranges)

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
