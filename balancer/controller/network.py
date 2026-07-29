# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments
# with shell=False (default). No untrusted shell execution or string
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
import threading
import time
from utils.logger import logger
from config.config import b_config
from monitor import NetworkMonitor
from monitor.system_info import _get_network_static_info
from utils import app_utils


# Network traffic shaping recognizes only four tiers. The app-level priority
# scheme (critical/high/medium/low/undefined) is collapsed onto these: any
# value that is not a recognized network tier — notably "medium" and
# "undefined" — is treated as "low", the default network tier.
NETWORK_PRIORITIES = ("critical", "high", "low", "system")

# Target HTB quantum (bytes) for the highest-rate class. The kernel warns when a
# class quantum exceeds ~200000 bytes ("quantum ... is big; Consider r2q
# change"); aiming at half that leaves headroom. quantum = rate_bytes / r2q, so
# we pick r2q per NIC from its total bandwidth rather than hard-coding it — this
# keeps quantum in range whether the link is 100Mbit or 10Gbit.
_TARGET_HTB_QUANTUM_BYTES = 100000


def compute_r2q(total_bw_kbit):
    """Pick an HTB r2q so the top class's quantum stays in the kernel's sane range.

    Derived from the NIC's total bandwidth: quantum = rate_bytes / r2q, and we
    want the full-rate class near _TARGET_HTB_QUANTUM_BYTES. Lower-priority
    classes have rate <= total, so their quantum is automatically <= the target.
    """
    total_bytes_per_sec = (total_bw_kbit * 1000) / 8
    return max(1, round(total_bytes_per_sec / _TARGET_HTB_QUANTUM_BYTES))


def normalize_net_priority(priority):
    """Map an app priority onto a network tier; unknown values fall back to low."""
    return priority if priority in NETWORK_PRIORITIES else "low"


class _NicShaper:
    """Traffic-shaping state for a single NIC.

    Each controlled interface owns an independent tc tree: the physical device
    plus a paired IFB device (for ingress shaping), a unique tc handle, its own
    bandwidth budget and NetworkMonitor, and per-direction throttle stages and
    class ids. Holding one of these per NIC is what makes multi-NIC support
    possible — the controller simply iterates over a list of shapers.
    """

    def __init__(self, dev, ifb_dev, handle_id, total_bw):
        self.dev = dev
        self.ifb_dev = ifb_dev
        self.handle_id = handle_id
        self.total_bw = total_bw
        self.monitor = NetworkMonitor(dev, total_bw)
        # Per-direction throttle stage (0 == unlimited) and cooldown timestamps.
        self.tx_limit_stage = 0
        self.rx_limit_stage = 0
        self.tx_last_limit_time = 0
        self.rx_last_limit_time = 0
        self.tx_last_recover_time = 0
        self.rx_last_recover_time = 0
        self.ingress_classids = []
        self.egress_classids = []


class NetworkController:
    def __init__(self):
        self.config = b_config
        # Base tc handle; each NIC gets base + 2*index so the physical handle and
        # its IFB handle (handle + 1) never collide across interfaces.
        self.base_handle_id = 50
        self.limit_cooldown = 10
        self.recover_cooldown = 30
        # Marks are global per app (one iptables MARK rule per app, independent
        # of how many NICs the app's traffic is classified on).
        self.mark_pool = set(hex(i) for i in range(0x1000, 0x2000))
        self.app_mark_map = {}
        self.app_filter_info = {}
        # Set when the API persists new network config; the monitor thread picks
        # this up at the top of its next cycle and rebuilds, so all tc mutations
        # stay on a single thread (no locking around live tc operations).
        self._reload_pending = threading.Event()

        self._load_config_state()

        # Build one shaper per existing configured interface.
        self.shapers = self._build_shapers()
        if not self.shapers:
            logger.warning("No usable network interface found; disable network sampling/control.")
            self.enable_network_control = False

    def _load_config_state(self):
        """(Re)read all config-derived network state into instance attributes.

        Called at init and on every hot reload so an edited config.yaml is
        reflected without recreating the controller.
        """
        # Network QoS is independent of passive_resource_control, but it still
        # has its own global switch. Respect config.enable_network_control so
        # disabling from UI/API truly suspends class shaping without service restart.
        self.enable_network_control = bool(getattr(self.config, "enable_network_control", False))
        # Independent switch for pressure-driven dynamic throttle/recover. When
        # OFF, class assignment/shaping remains active but no stage transitions
        # are performed from network pressure levels.
        self.enable_network_pressure_shaping = bool(
            getattr(self.config, "enable_network_pressure_shaping", True)
        )

        # Per-tier bandwidth is configured as ratios (0..1) of each NIC's total
        # bandwidth, converted to kbit/s on demand via _get_class_bandwidth. This
        # keeps tiers from ever exceeding the link and avoids hand-scaling when
        # total bandwidth changes.
        config_network_bw = getattr(self.config, "config_network_bw", None)
        if not config_network_bw:
            config_network_bw = {
                "critical": {"min": 0.6, "max": 0.9},
                "high": {"min": 0.3, "max": 0.8},
                "low": {"min": 0.1, "max": 0.3},
                "system": {"min": 0.05, "max": 0.1},
            }
        burst_map = getattr(self.config, "network_burst_map", None)
        if not burst_map:
            burst_map = {
                "critical": "64k",
                "high": "32k",
                "low": "16k",
                "system": "8k"
            }
        self.config_network_bw = config_network_bw
        self.network_burst_map = burst_map

    def request_reload(self):
        """Signal the monitor thread to rebuild network shaping from current config.

        Thread-safe and cheap: the API thread only sets a flag; the actual
        teardown/re-detect/rebuild runs inside the monitor thread's next cycle.
        """
        self._reload_pending.set()
        logger.info("Network config reload requested; will apply on next monitor cycle.")

    def reload(self):
        """Tear down existing shaping, re-read config, re-detect NICs, rebuild.

        Must run on the monitor thread (called from process_network_cycle) so it
        never races the per-cycle tc reads/writes.
        """
        logger.info("Reloading network configuration...")
        # Tear down whatever the current shapers/app filters created.
        try:
            self._teardown_all()
        except Exception as e:
            logger.error("Network reload teardown failed: %s", str(e), exc_info=True)

        # Reset per-app and mark state; apps get re-added on the next cycle.
        self.app_mark_map = {}
        self.app_filter_info = {}
        self.mark_pool = set(hex(i) for i in range(0x1000, 0x2000))

        # Re-read config and re-detect interfaces.
        self._load_config_state()
        self.shapers = self._build_shapers()
        if not self.shapers:
            logger.warning("No usable network interface found after reload; network control disabled.")
            self.enable_network_control = False
            return
        self.setup_tc_classes_and_filters()
        logger.info("Network configuration reload complete (%d interface(s) active).", len(self.shapers))

    # ------------------------------------------------------------------ config

    def _resolve_nic_specs(self):
        """Return the NICs to shape as a list of (name,) tuples.

        A configured ``network_interfaces`` list is the user's explicit
        selection. When no selection has ever been saved, all usable physical
        NICs are selected automatically. Bandwidth is always system-detected.
        """
        nics = getattr(self.config, "network_interfaces", None)
        if nics is not None:
            specs = []
            seen = set()
            for entry in nics:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if not name or name in seen:
                    continue
                if not entry.get("enabled", True):
                    logger.info("Interface '%s' disabled in config; skipping.", name)
                    continue
                seen.add(name)
                specs.append((name, None))
            return specs

        detected = _get_network_static_info().get("valid_nics", [])
        names = [nic.get("name") for nic in detected if isinstance(nic, dict) and nic.get("name")]
        logger.info("No network interface selection configured; auto-detected usable NICs: %s", names)
        return [(name, None) for name in names]

    @staticmethod
    def _detect_link_speed_kbit(iface):
        """Read a NIC's link speed from sysfs, returned in kbit/s (0 if unknown).

        ``/sys/class/net/<iface>/speed`` is in Mbit/s and may be missing, 0, or
        -1 for WiFi/virtual NICs whose drivers don't report a link speed.
        """
        try:
            with open(f"/sys/class/net/{iface}/speed") as f:
                mbit = int(f.read().strip())
            if mbit > 0:
                return mbit * 1000
        except (OSError, ValueError):
            pass
        return 0

    def _bandwidth_for(self, iface):
        """Detect the bandwidth (kbit/s) for a given interface.

        Returns the link speed read from sysfs. Returns 0 when the speed can't
        be determined (link down, WiFi/virtual driver). The caller treats 0 as
        "not shapeable" and skips the NIC — bandwidth is never user-configured.
        """
        detected = self._detect_link_speed_kbit(iface)
        if detected:
            logger.info("Interface '%s': detected link speed %d kbit/s.", iface, detected)
        return detected

    def _build_shapers(self):
        shapers = []
        idx = 0
        for iface, _unused in self._resolve_nic_specs():
            if not os.path.exists(f"/sys/class/net/{iface}"):
                logger.warning(f"Network interface '{iface}' does not exist; skipping.")
                continue
            total_bw = self._bandwidth_for(iface)
            if total_bw <= 0:
                logger.warning(
                    "Skipping interface '%s': link speed unavailable "
                    "(link down or driver does not report speed).", iface)
                continue
            handle_id = self.base_handle_id + 2 * idx
            ifb_dev = f"ifb{idx}"
            shapers.append(_NicShaper(iface, ifb_dev, handle_id, total_bw))
            logger.info(f"Network shaping enabled on '{iface}' (ifb={ifb_dev}, "
                        f"handle={handle_id}, bandwidth={total_bw} kbit/s)")
            idx += 1
        return shapers

    # ------------------------------------------------------------------- marks

    def _allocate_mark(self):
        if self.mark_pool:
            return self.mark_pool.pop()
        else:
            return hex(0x2000 + len(self.app_mark_map))

    def _release_mark(self, mark):
        self.mark_pool.add(mark)

    # -------------------------------------------------------------- app rules

    def _add_app_network_rules(self, app, idx):
        priority = normalize_net_priority(app.get("network_priority") or app.get("priority", "low"))
        app_id = app.get("app_id")
        raw_paths = app.get("cgroup_paths") or []
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        # Keep paths deterministic and deduplicated for idempotent comparisons.
        cgroup_paths = sorted({str(p).strip() for p in raw_paths if str(p).strip()})

        # System-priority apps are classified by port filters (see
        # _get_set_networked_system_ports), not by a per-app mark, so we only
        # record a placeholder with the per-NIC system class ids.
        if priority == "system":
            self.app_filter_info[app_id] = {
                "mark": None,
                "cgroup_paths": cgroup_paths,
                "priority": priority,
                "nics": {
                    shaper.dev: {
                        "ifb_dev": shaper.ifb_dev,
                        "prio_egress": None,
                        "prio_ifb": None,
                        "classid_egress": self._get_classid(shaper.handle_id, priority),
                        "classid_ifb": self._get_classid(shaper.handle_id + 1, priority),
                    }
                    for shaper in self.shapers
                },
            }
            return

        mark = self._allocate_mark()
        mark_int = int(mark, 16)
        self.app_mark_map[app_id] = mark

        # One mark rule per app, independent of NIC count.
        for cgroup_path in cgroup_paths:
            subprocess.run(
                [
                    "iptables", "-t", "mangle", "-A", "OUTPUT",
                    "-m", "cgroup", "--path", cgroup_path,
                    "-j", "MARK", "--set-mark", str(mark_int),
                ],
                check=False,
            )

        nics = {}
        # Install a mark-matching tc filter on every NIC's egress and IFB tree.
        for shaper in self.shapers:
            classid_egress = self._get_classid(shaper.handle_id, priority)
            classid_ifb = self._get_classid(shaper.handle_id + 1, priority)
            prio_egress = 10 + idx
            prio_ifb = 21 + idx
            subprocess.run(["tc", "filter", "add", "dev", shaper.dev, "parent", f"{shaper.handle_id}:",
                            "protocol", "ip", "prio", str(prio_egress), "u32", "match", "mark",
                            str(mark_int), "0xffffffff", "flowid", classid_egress], check=False)
            subprocess.run(["tc", "filter", "add", "dev", shaper.ifb_dev, "parent", f"{shaper.handle_id+1}:",
                            "protocol", "ip", "prio", str(prio_ifb), "u32", "match", "mark",
                            str(mark_int), "0xffffffff", "flowid", classid_ifb], check=False)
            nics[shaper.dev] = {
                "ifb_dev": shaper.ifb_dev,
                "handle_id": shaper.handle_id,
                "prio_egress": prio_egress,
                "prio_ifb": prio_ifb,
                "classid_egress": classid_egress,
                "classid_ifb": classid_ifb,
            }
        self.app_filter_info[app_id] = {
            "mark": mark,
            "mark_int": mark_int,
            "cgroup_paths": cgroup_paths,
            "priority": priority,
            "nics": nics,
        }

    def _get_set_networked_system_ports(self, shaper):
        raw_ports = getattr(b_config, 'network_system_ports', [22, 53, 80, 443, 123])
        system_ports = set()
        if isinstance(raw_ports, (list, tuple, set)):
            for port in raw_ports:
                if isinstance(port, bool):
                    continue
                try:
                    port = int(port)
                except (TypeError, ValueError):
                    continue
                if 1 <= port <= 65535:
                    system_ports.add(port)

        classid_egress = self._get_classid(shaper.handle_id, "system")
        classid_ifb = self._get_classid(shaper.handle_id + 1, "system")
        for idx, port in enumerate(sorted(system_ports)):
            prio_egress = 1000 + idx
            prio_ifb = 2000 + idx
            base_cmd = ["tc", "filter", "add", "dev", shaper.dev, "parent", f"{shaper.handle_id}:", "protocol", "ip", "prio", str(prio_egress), "u32"]
            subprocess.run(base_cmd + ["match", "ip", "dport", str(port), "0xffff", "flowid", classid_egress], check=False)
            subprocess.run(base_cmd + ["match", "ip", "sport", str(port), "0xffff", "flowid", classid_egress], check=False)

            ifb_cmd = ["tc", "filter", "add", "dev", shaper.ifb_dev, "parent", f"{shaper.handle_id+1}:", "protocol", "ip", "prio", str(prio_ifb), "u32"]
            subprocess.run(ifb_cmd + ["match", "ip", "dport", str(port), "0xffff", "flowid", classid_ifb], check=False)
            subprocess.run(ifb_cmd + ["match", "ip", "sport", str(port), "0xffff", "flowid", classid_ifb], check=False)

    def _remove_app_network_rules(self, app_id):
        info = self.app_filter_info.get(app_id)
        if not info:
            return
        mark = info.get("mark")
        cgroup_paths = info.get("cgroup_paths") or []
        if isinstance(cgroup_paths, str):
            cgroup_paths = [cgroup_paths]
        cgroup_paths = [str(p).strip() for p in cgroup_paths if str(p).strip()]

        if mark:
            mark_int = str(int(mark, 16))
            for cgroup_path in cgroup_paths:
                subprocess.run(
                    [
                        "iptables", "-t", "mangle", "-D", "OUTPUT",
                        "-m", "cgroup", "--path", cgroup_path,
                        "-j", "MARK", "--set-mark", mark_int,
                    ],
                    check=False,
                )

        for dev, nic in info.get("nics", {}).items():
            prio_egress = nic.get("prio_egress")
            prio_ifb = nic.get("prio_ifb")
            handle_id = nic.get("handle_id")
            if prio_egress is None or handle_id is None:
                continue
            subprocess.run(["tc", "filter", "del", "dev", dev, "parent", f"{handle_id}:", "protocol", "ip", "prio", str(prio_egress)], check=False)
            subprocess.run(["tc", "filter", "del", "dev", nic["ifb_dev"], "parent", f"{handle_id+1}:", "protocol", "ip", "prio", str(prio_ifb)], check=False)

        if mark:
            self._release_mark(mark)
        self.app_mark_map.pop(app_id, None)
        self.app_filter_info.pop(app_id, None)

    # -------------------------------------------------------------- tc setup

    def setup_tc_classes_and_filters(self):
        if not self.enable_network_control:
            logger.info("NetworkControl is disabled, skipping tc classes and filters setup")
            return

        subprocess.run(["modprobe", "ifb"], check=False)

        for shaper in self.shapers:
            dev = shaper.dev
            IFB_DEV = shaper.ifb_dev
            handle_id = shaper.handle_id

            subprocess.run(["tc", "qdisc", "del", "dev", dev, "handle", f"{handle_id}:", "root"], stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["tc", "qdisc", "del", "dev", dev, "ingress"], stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["tc", "qdisc", "del", "dev", IFB_DEV, "handle", f"{handle_id+1}:", "root"], stderr=subprocess.DEVNULL, check=False)

            # r2q tunes how HTB derives each class's quantum (quantum = rate/r2q).
            # The default r2q=10 makes quantum too large for high-rate classes
            # (kernel logs "quantum ... is big; Consider r2q change"). Compute it
            # per NIC from its bandwidth so it stays correct across the full range
            # of link speeds and any bandwidth change applied via reload.
            r2q = str(compute_r2q(shaper.total_bw))
            subprocess.run(["tc", "qdisc", "add", "dev", dev, "root", "handle", f"{handle_id}:", "htb", "default", "30", "r2q", r2q], check=False)
            subprocess.run(["tc", "class", "add", "dev", dev, "parent", f"{handle_id}:", "classid", f"{handle_id}:1", "htb", "rate", f"{shaper.total_bw}kbit", "ceil", f"{shaper.total_bw}kbit", "burst", "128k", "cburst", "128k"], check=False)

            subprocess.run(["ip", "link", "add", IFB_DEV, "type", "ifb"], check=False)
            subprocess.run(["ip", "link", "set", IFB_DEV, "up"], check=False)

            subprocess.run(["tc", "qdisc", "add", "dev", IFB_DEV, "root", "handle", f"{handle_id+1}:", "htb", "default", "30", "r2q", r2q], check=False)
            subprocess.run(["tc", "class", "add", "dev", IFB_DEV, "parent", f"{handle_id+1}:", "classid", f"{handle_id+1}:1", "htb", "rate", f"{shaper.total_bw}kbit", "ceil", f"{shaper.total_bw}kbit", "burst", "128k", "cburst", "128k"], check=False)

            subprocess.run(["tc", "qdisc", "add", "dev", dev, "ingress", "handle", "ffff:"], check=False)
            subprocess.run(["tc", "filter", "add", "dev", dev, "parent", "ffff:", "protocol", "all", "prio", "10", "u32", "match", "u32", "0", "0", "flowid", f"{handle_id+1}:1", "action", "connmark", "action", "mirred", "egress", "redirect", "dev", IFB_DEV], check=False)

            for key in ["critical", "high", "low", "system"]:
                min_bw, max_bw = self._get_class_bandwidth(key, shaper.total_bw)
                burst = self.network_burst_map.get(key, "16k")

                classid_egress = self._get_classid(handle_id, key)
                subprocess.run(["tc", "class", "add", "dev", dev, "parent", f"{handle_id}:1", "classid", classid_egress, "htb", "rate", f"{min_bw}kbit", "ceil", f"{max_bw}kbit", "burst", burst, "cburst", burst], check=False)

                classid_ifb = self._get_classid(handle_id + 1, key)
                subprocess.run(["tc", "class", "add", "dev", IFB_DEV, "parent", f"{handle_id+1}:1", "classid", classid_ifb, "htb", "rate", f"{min_bw}kbit", "ceil", f"{max_bw}kbit", "burst", burst, "cburst", burst], check=False)

            self._get_set_networked_system_ports(shaper)
            shaper.ingress_classids = self._get_all_classids(handle_id, direction="ingress")
            shaper.egress_classids = self._get_all_classids(handle_id, direction="egress")

    def _limit_network_class(self, dev, classid, min_bw, max_bw=None, burst="16k", direction="egress", level=None):
        if max_bw is None:
            max_bw = min_bw
        subprocess.run(["tc", "class", "change", "dev", dev, "classid", classid, "htb", "rate", f"{min_bw}kbit", "ceil", f"{max_bw}kbit", "burst", str(burst), "cburst", str(burst)], check=False)

    def update_app_network_control(self):
        controlled_apps = app_utils.get_controlled_apps_net() or []
        new_app_ids = set(app.get("app_id") for app in controlled_apps)
        old_app_ids = set(self.app_filter_info.keys())
        # 1. Remove apps that no longer exist
        for app_id in old_app_ids - new_app_ids:
            self._remove_app_network_rules(app_id)
        # 2. Handle priority changes or newly added apps
        for idx, app in enumerate(controlled_apps):
            app_id = app.get("app_id")
            new_priority = normalize_net_priority(app.get("network_priority") or app.get("priority", "low"))
            new_paths = app.get("cgroup_paths") or []
            if isinstance(new_paths, str):
                new_paths = [new_paths]
            new_paths_set = {str(p).strip() for p in new_paths if str(p).strip()}

            if app_id in old_app_ids:
                old_info = self.app_filter_info.get(app_id, {})
                old_priority = normalize_net_priority(old_info.get("priority", "low"))
                old_paths = old_info.get("cgroup_paths") or []
                if isinstance(old_paths, str):
                    old_paths = [old_paths]
                old_paths_set = {str(p).strip() for p in old_paths if str(p).strip()}

                # Idempotent update: if both class and cgroup set are unchanged,
                # keep existing rules and avoid duplicate add operations.
                if old_priority != new_priority or old_paths_set != new_paths_set:
                    self._remove_app_network_rules(app_id)
                    self._add_app_network_rules(app, idx)
            else:
                self._add_app_network_rules(app, idx)
            # Ensure only one CONNMARK --save-mark rule exists in OUTPUT, placed after all MARK rules
            subprocess.run(["iptables", "-t", "mangle", "-D", "OUTPUT", "-j", "CONNMARK", "--save-mark"], stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["iptables", "-t", "mangle", "-A", "OUTPUT", "-j", "CONNMARK", "--save-mark"], check=False)

    # ----------------------------------------------------------- class ids

    def _get_classid(self, handle, priority):
        mapping = {"critical": 10, "high": 20, "low": 30, "system": 5}
        num = mapping[normalize_net_priority(priority)]
        return f"{handle}:{num}"

    def _get_class_bandwidth(self, priority, total_bw):
        # Tiers are stored as ratios (0..1) of total_bw; convert to kbit/s here.
        # Fall back to the "low" tier (not an empty/zero range) for any priority
        # without its own band, so medium/undefined apps inherit low's bandwidth
        # instead of being throttled to zero.
        key = normalize_net_priority(priority)
        bw = self.config_network_bw.get(key) or self.config_network_bw.get("low", {})
        min_bw = int(bw.get("min", 0) * total_bw)
        max_bw = int(bw.get("max", 0) * total_bw)
        return min_bw, max_bw

    def _get_all_classids(self, handle, priorities=None, direction="egress"):
        if priorities is None:
            priorities = ["critical", "high", "low", "system"]
        if direction == "ingress":
            handle = handle + 1
        return [self._get_classid(handle, key) for key in priorities]

    def _get_ratios_classids(self, handle_id):
        return {
            "egress_low": self._get_classid(handle_id, "low"),
            "egress_high": self._get_classid(handle_id, "high"),
            "egress_critical": self._get_classid(handle_id, "critical"),
            "egress_system": self._get_classid(handle_id, "system"),
            "ingress_low": self._get_classid(handle_id + 1, "low"),
            "ingress_high": self._get_classid(handle_id + 1, "high"),
            "ingress_critical": self._get_classid(handle_id + 1, "critical"),
            "ingress_system": self._get_classid(handle_id + 1, "system"),
        }

    def get_rates(self, handle_id, egress_rates, ingress_rates):
        classids = self._get_ratios_classids(handle_id)
        rates = {
            "egress_low": egress_rates.get(classids["egress_low"], 0),
            "egress_high": egress_rates.get(classids["egress_high"], 0),
            "egress_critical": egress_rates.get(classids["egress_critical"], 0),
            "egress_system": egress_rates.get(classids["egress_system"], 0),
            "ingress_low": ingress_rates.get(classids["ingress_low"], 0),
            "ingress_high": ingress_rates.get(classids["ingress_high"], 0),
            "ingress_critical": ingress_rates.get(classids["ingress_critical"], 0),
            "ingress_system": ingress_rates.get(classids["ingress_system"], 0),
        }
        return rates

    # ------------------------------------------------------- throttle / recover

    def _recover_network_pressure(self, shaper, limit_stage, direction, rates, config_total_rate, actual_total_bw, limit_stage_attr):
        dev = shaper.dev if direction == "egress" else shaper.ifb_dev
        handle = shaper.handle_id if direction == "egress" else shaper.handle_id + 1
        limit_stage_to_priority = {
            1: "low",
            2: "low",
            3: "high",
            4: "high"
        }
        key = limit_stage_to_priority.get(limit_stage, "high")
        min_bw, max_bw = self._get_class_bandwidth(key, shaper.total_bw)
        classid = self._get_classid(handle, key)
        burst = self.network_burst_map.get(key, "16k")
        current_class_bw = rates.get(classid, 0)
        half_bw = int((max_bw - min_bw) / 2 + min_bw)
        critical_threshold = self.config.network_thresholds["critical"] * config_total_rate
        # Determine the stage from which to begin restoring bandwidth
        stage_table = {
            1: (half_bw, 0, 0),
            2: (min_bw, 0, 1),
            3: (half_bw, 2, 2),
            4: (min_bw, 2, 3),
        }
        stage_transition_point, stage_full, stage_half = stage_table.get(limit_stage, (min_bw, 0, 0))
        # Restore bandwidth tier by tier, from highest to lowest (half -> max)
        if limit_stage > 0:
            if current_class_bw < stage_transition_point * 0.9:
                self._limit_network_class(dev, classid, min_bw, max_bw, burst, direction=direction, level=key)
                setattr(shaper, limit_stage_attr, stage_full)
                logger.info(f"{dev} {direction.upper()} fully restoring {key} app class bandwidth to {max_bw} kbit/s")
            else:
                if limit_stage in (4, 2):
                    expected_total_bw = half_bw + actual_total_bw - min_bw
                else:
                    expected_total_bw = max_bw + actual_total_bw - half_bw
                if expected_total_bw < critical_threshold:
                    if limit_stage in (4, 2):
                        self._limit_network_class(dev, classid, min_bw, half_bw, burst, direction=direction, level=key)
                        logger.info(f"{dev} {direction.upper()} partially restoring {key} app class bandwidth to {half_bw} kbit/s")
                    else:
                        self._limit_network_class(dev, classid, min_bw, max_bw, burst, direction=direction, level=key)
                        logger.info(f"{dev} {direction.upper()} fully restoring {key} app class bandwidth to {max_bw} kbit/s")
                    setattr(shaper, limit_stage_attr, stage_half)
                else:
                    logger.info(f"{dev} {direction.upper()} {key} app class kept at {stage_transition_point} kbit/s; full restore would exceed bandwidth threshold")

    def _apply_bandwidth_limit(self, shaper, stage, direction, rates, limit_stage_attr):
        dev = shaper.dev if direction == "egress" else shaper.ifb_dev
        handle = shaper.handle_id if direction == "egress" else shaper.handle_id + 1
        limit_stage_to_priority = {
            0: "low",
            1: "low",
            2: "high",
            3: "high"
        }
        key = limit_stage_to_priority.get(stage, "high")
        min_bw, max_bw = self._get_class_bandwidth(key, shaper.total_bw)
        classid = self._get_classid(handle, key)
        burst = self.network_burst_map.get(key, "16k")
        current_stage_bw = rates.get(classid, 0)
        half_bw = int((max_bw - min_bw) / 2 + min_bw)
        # Determine the throttle stage target
        stage_table = {
            0: (half_bw, 2, 1),
            1: (min_bw, 2, 2),
            2: (half_bw, 4, 3),
            3: (min_bw, 4, 4),
        }
        stage_transition_point, stage_full, stage_half = stage_table.get(stage, (min_bw, 0, 0))
        # Apply throttle tier by tier, from lowest priority to highest
        if stage in (0, 2):
            if current_stage_bw < stage_transition_point:
                self._limit_network_class(dev, classid, min_bw, min_bw, burst, direction=direction, level=key)
                setattr(shaper, limit_stage_attr, stage_full)
                logger.info(f"{dev} {direction.upper()} throttling {key} class app bandwidth to {min_bw}")
            else:
                self._limit_network_class(dev, classid, min_bw, half_bw, burst, direction=direction, level=key)
                setattr(shaper, limit_stage_attr, stage_half if half_bw != min_bw else stage_full)
                logger.info(f"{dev} {direction.upper()} throttling {key} class app bandwidth to {half_bw}")
        elif stage in (1, 3):
            self._limit_network_class(dev, classid, min_bw, min_bw, burst, direction=direction, level=key)
            setattr(shaper, limit_stage_attr, stage_full)
            logger.info(f"{dev} {direction.upper()} re-throttling {key} class app bandwidth to {min_bw}")

    def _can_switch(self, cooldown, last_limit_time, last_recover_time):
        time_since_limit = time.time() - last_limit_time
        time_since_recover = time.time() - last_recover_time
        return time_since_limit > cooldown and time_since_recover > cooldown

    def handle_network_pressure(self, shaper, tx_pressure, rx_pressure, ingress_rates, egress_rates, network_data):
        config_total_rate = shaper.total_bw
        tx_total_bw = shaper.total_bw * network_data['tx']
        rx_total_bw = shaper.total_bw * network_data['rx']
        # TX throttle
        if tx_pressure == "critical" and self._can_switch(self.limit_cooldown, shaper.tx_last_limit_time, shaper.tx_last_recover_time):
            self._apply_bandwidth_limit(shaper, shaper.tx_limit_stage, "egress", egress_rates, "tx_limit_stage")
            shaper.tx_last_limit_time = time.time()
        # RX throttle
        if rx_pressure == "critical" and self._can_switch(self.limit_cooldown, shaper.rx_last_limit_time, shaper.rx_last_recover_time):
            self._apply_bandwidth_limit(shaper, shaper.rx_limit_stage, "ingress", ingress_rates, "rx_limit_stage")
            shaper.rx_last_limit_time = time.time()
        # TX pressure restore
        if tx_pressure != "critical" and shaper.tx_limit_stage > 0 and self._can_switch(self.recover_cooldown, shaper.tx_last_limit_time, shaper.tx_last_recover_time):
            self._recover_network_pressure(
                shaper,
                shaper.tx_limit_stage,
                "egress",
                egress_rates,
                config_total_rate,
                tx_total_bw,
                "tx_limit_stage"
            )
            shaper.tx_last_recover_time = time.time()
        # RX pressure restore
        if rx_pressure != "critical" and shaper.rx_limit_stage > 0 and self._can_switch(self.recover_cooldown, shaper.rx_last_limit_time, shaper.rx_last_recover_time):
            self._recover_network_pressure(
                shaper,
                shaper.rx_limit_stage,
                "ingress",
                ingress_rates,
                config_total_rate,
                rx_total_bw,
                "rx_limit_stage"
            )
            shaper.rx_last_recover_time = time.time()

    # ----------------------------------------------------------- main-loop API

    def process_network_cycle(self, control_manager, do_pressure_eval):
        """Run one network-shaping cycle across every controlled NIC.

        Called every balancer loop iteration. ``do_pressure_eval`` gates the
        (more expensive) pressure classification + throttle/restore decision so
        it only runs on the configured sampling interval, while lightweight
        sampling and stats collection happen every iteration.
        """
        # Apply a pending hot reload first, on this (monitor) thread, before any
        # per-cycle tc work. Checked even when shapers is empty so a controller
        # that started with no usable NIC can recover once config is fixed.
        if self._reload_pending.is_set():
            self._reload_pending.clear()
            self.reload()

        if not self.shapers:
            return

        if self.enable_network_control:
            self.update_app_network_control()

        # Isolate failures per NIC: a tc/parsing error on one interface must not
        # skip the remaining interfaces' sampling and shaping for this cycle.
        for shaper in self.shapers:
            try:
                self._process_shaper_cycle(shaper, control_manager, do_pressure_eval)
            except Exception as e:
                logger.error("Network cycle failed for interface '%s': %s",
                             shaper.dev, str(e), exc_info=True)

    def _process_shaper_cycle(self, shaper, control_manager, do_pressure_eval):
        if self.enable_network_control:
            shaper.monitor.get_tc_class_stats(shaper.ifb_dev, shaper.handle_id + 1,
                                              classids=shaper.ingress_classids,
                                              direction="ingress")
            shaper.monitor.get_tc_class_stats(shaper.dev, shaper.handle_id,
                                              classids=shaper.egress_classids,
                                              direction="egress")
        shaper.monitor.sample_network_pressure()

        if not do_pressure_eval:
            return

        network_data = shaper.monitor.get_current_pressure()
        tx_pressure, rx_pressure, *_ = control_manager.update_network_pressure_level(network_data)
        tx_total_bw = shaper.total_bw * network_data['tx']
        rx_total_bw = shaper.total_bw * network_data['rx']
        logger.debug(
            f"NetworkMonitor {shaper.dev} TX level: {tx_pressure} (pressure: {network_data['tx']:.2f}),"
            f" RX level: {rx_pressure} (pressure: {network_data['rx']:.2f})")
        if self.enable_network_control and self.enable_network_pressure_shaping:
            ingress_rates = shaper.monitor.get_tc_class_stats_rate_ingress()
            egress_rates = shaper.monitor.get_tc_class_stats_rate_egress()
            rates = self.get_rates(shaper.handle_id, egress_rates, ingress_rates)
            logger.debug(
                f"NetworkMonitor {shaper.dev} TX_total_BW={tx_total_bw:,.2f}kbit/s (App Class BW: System - {rates['egress_system']:,.2f},"
                f" Critical - {rates['egress_critical']:,.2f} , High - {rates['egress_high']:,.2f}, Low - {rates['egress_low']:,.2f}),"
                f" RX_total_BW={rx_total_bw:,.2f}kbit/s (App Class BW: System - {rates['ingress_system']:,.2f},"
                f" Critical - {rates['ingress_critical']:,.2f} , High - {rates['ingress_high']:,.2f}, Low - {rates['ingress_low']:,.2f})")
            self.handle_network_pressure(shaper, tx_pressure, rx_pressure, ingress_rates, egress_rates, network_data)

    def _teardown_all(self):
        """Remove every tc qdisc, IFB device, and iptables mark rule we created.

        Unconditional (unlike clear_network_rules_on_exit's enable gate) so it
        can be reused by reload() to wipe state regardless of the prior switch.
        """
        for shaper in self.shapers:
            subprocess.run(["tc", "qdisc", "del", "dev", shaper.dev, "handle", f"{shaper.handle_id}:", "root"], stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["tc", "qdisc", "del", "dev", shaper.ifb_dev, "handle", f"{shaper.handle_id+1}:", "root"], stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["tc", "qdisc", "del", "dev", shaper.dev, "ingress"], stderr=subprocess.DEVNULL, check=False)
            # Remove the IFB device this controller created so repeated restarts
            # or reloads don't leave ifb0/ifb1/... interfaces accumulating.
            subprocess.run(["ip", "link", "del", shaper.ifb_dev], stderr=subprocess.DEVNULL, check=False)
        for app_id, info in list(self.app_filter_info.items()):
            mark = info.get("mark")
            cgroup_paths = info.get("cgroup_paths") or []
            if isinstance(cgroup_paths, str):
                cgroup_paths = [cgroup_paths]
            if mark:
                mark_value = str(int(mark, 16))
                for cgroup_path in cgroup_paths:
                    cgroup_path = str(cgroup_path).strip()
                    if not cgroup_path:
                        continue
                    subprocess.run(
                        [
                            "iptables", "-t", "mangle", "-D", "OUTPUT",
                            "-m", "cgroup", "--path", cgroup_path,
                            "-j", "MARK", "--set-mark", mark_value,
                        ],
                        check=False,
                    )

    def clear_network_rules_on_exit(self):
        if not self.enable_network_control:
            logger.info("NetworkControl is disabled, skipping tc queue and iptables rule cleanup")
            return
        self._teardown_all()
        logger.info("Cleaned up all tc queues and iptables mark rules created by the balancer")
