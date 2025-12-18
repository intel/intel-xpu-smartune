#
#  Copyright (C) 2025 Intel Corporation
#
#  This software and the related documents are Intel copyrighted materials,
#  and your use of them is governed by the express license under which they
#  were provided to you ("License"). Unless the License provides otherwise,
#  you may not use, modify, copy, publish, distribute, disclose or transmit
#  his software or the related documents without Intel's prior written permission.
#
#  This software and the related documents are provided as is, with no express
#  or implied warranties, other than those that are expressly stated in the License.
#


import subprocess
import time
from utils.logger import logger
from config.config import b_config
from monitor.network import NetworkMonitor
from utils import app_utils

class NetworkController:
    def __init__(self):
        self.config = b_config
        self.dev = self.config.network_interface or "enp1s0"
        self.IFB_DEV = "ifb0"
        self.handle_id = 50
        self.limit_cooldown = 10
        self.recover_cooldown = 30
        self.tx_last_limit_time = 0
        self.rx_last_limit_time = 0
        self.tx_last_recover_time = 0
        self.rx_last_recover_time = 0
        self.mark_pool = set(hex(i) for i in range(0x1000, 0x2000))
        self.app_mark_map = {}
        self.app_filter_info = {}
        self.tx_network_limit_stage = 0
        self.rx_network_limit_stage = 0
        self.total_bw = self.config.network_bandwidth_kbit
        self.enable_network_control = self.config.enable_network_control
        config_network_bw = getattr(self.config, "config_network_bw", None)
        if not config_network_bw:
            config_network_bw = {
                "critical": {"min": int(self.total_bw*0.6), "max": int(self.total_bw*0.9)},
                "high": {"min": int(self.total_bw*0.3), "max": int(self.total_bw*0.8)},
                "low": {"min": int(self.total_bw*0.1), "max": int(self.total_bw*0.3)},
                "system": {"min": 50000, "max": 100000},
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
        self.ingress_classids = []
        self.egress_classids = []
        self.network = NetworkMonitor(self.config.network_interface,
                                      self.config.network_bandwidth_kbit)

    def _allocate_mark(self):
        if self.mark_pool:
            return self.mark_pool.pop()
        else:
            return hex(0x2000 + len(self.app_mark_map))

    def _release_mark(self, mark):
        self.mark_pool.add(mark)

    def _add_app_network_rules(self, app, idx):
        priority = app.get("priority", "low")
        app_id = app.get("app_id")
        cgroup_path = app.get("cgroup_path")
        handle_id = self.handle_id
        dev = self.dev
        IFB_DEV = self.IFB_DEV
        if priority == "system":
            self.app_filter_info[app_id] = {
                "dev": dev,
                "ifb_dev": IFB_DEV,
                "prio_egress": None,
                "prio_ifb": None,
                "classid_egress": self._get_classid(handle_id, priority),
                "classid_ifb": self._get_classid(handle_id+1, priority),
                "mark": None,
                "cgroup_path": cgroup_path
            }
            return
        mark = self._allocate_mark()
        mark_int = int(mark, 16)
        self.app_mark_map[app_id] = mark
        classid_egress = self._get_classid(handle_id, priority)
        classid_ifb = self._get_classid(handle_id+1, priority)
        prio_egress = 10 + idx
        prio_ifb = 21 + idx
        if cgroup_path:
            subprocess.run(f"iptables -t mangle -A OUTPUT -m cgroup --path {cgroup_path} -j MARK --set-mark {mark_int}", shell=True)
        subprocess.run(f"tc filter add dev {dev} parent {handle_id}: protocol ip prio {prio_egress} u32 match mark {mark_int} 0xffffffff flowid {classid_egress}", shell=True)
        subprocess.run(f"tc filter add dev {IFB_DEV} parent {handle_id+1}: protocol ip prio {prio_ifb} u32 match mark {mark_int} 0xffffffff flowid {classid_ifb}", shell=True)
        self.app_filter_info[app_id] = {
            "dev": dev,
            "ifb_dev": IFB_DEV,
            "prio_egress": prio_egress,
            "prio_ifb": prio_ifb,
            "classid_egress": classid_egress,
            "classid_ifb": classid_ifb,
            "mark": mark,
            "mark_int": mark_int,
            "cgroup_path": cgroup_path,
            "priority": priority
        }

    def _get_set_networked_system_ports(self):
        handle_id = self.handle_id
        dev = self.dev
        IFB_DEV = self.IFB_DEV
        system_ports = set(getattr(b_config, 'network_system_ports', [22, 53, 80, 443, 123]))
        classid_egress = self._get_classid(handle_id, "system")
        classid_ifb = self._get_classid(handle_id+1, "system")
        for idx, port in enumerate(system_ports):
            prio_egress = 1000 + idx
            prio_ifb = 2000 + idx
            subprocess.run(f"tc filter add dev {dev} parent {handle_id}: protocol ip prio {prio_egress} u32 match ip dport {port} 0xffff flowid {classid_egress}", shell=True)
            subprocess.run(f"tc filter add dev {dev} parent {handle_id}: protocol ip prio {prio_egress} u32 match ip sport {port} 0xffff flowid {classid_egress}", shell=True)
            subprocess.run(f"tc filter add dev {IFB_DEV} parent {handle_id+1}: protocol ip prio {prio_ifb} u32 match ip dport {port} 0xffff flowid {classid_ifb}", shell=True)
            subprocess.run(f"tc filter add dev {IFB_DEV} parent {handle_id+1}: protocol ip prio {prio_ifb} u32 match ip sport {port} 0xffff flowid {classid_ifb}", shell=True)

    def _remove_app_network_rules(self, app_id):
        info = self.app_filter_info.get(app_id)
        if not info:
            return
        dev = info["dev"]
        ifb_dev = info["ifb_dev"]
        prio_egress = info["prio_egress"]
        prio_ifb = info["prio_ifb"]
        classid_egress = info["classid_egress"]
        classid_ifb = info["classid_ifb"]
        mark = info["mark"]
        cgroup_path = info["cgroup_path"]
        if cgroup_path:
            subprocess.run(f"iptables -t mangle -D OUTPUT -m cgroup --path {cgroup_path} -j MARK --set-mark {int(mark,16)}", shell=True)
        subprocess.run(f"tc filter del dev {dev} parent {self.handle_id}: protocol ip prio {prio_egress}", shell=True)
        subprocess.run(f"tc filter del dev {ifb_dev} parent {self.handle_id+1}: protocol ip prio {prio_ifb}", shell=True)
        self._release_mark(mark)
        self.app_mark_map.pop(app_id, None)
        self.app_filter_info.pop(app_id, None)

    def setup_tc_classes_and_filters(self):
        if not self.enable_network_control:
            logger.info("NetworkControl 被禁用，跳过 tc classes 和 filters 的设置")
            return
        dev = self.dev
        IFB_DEV = self.IFB_DEV
        handle_id = self.handle_id
        subprocess.run(f"tc qdisc add dev {dev} root handle {handle_id}: htb default 30", shell=True)
        subprocess.run(f"tc class add dev {dev} parent {handle_id}: classid {handle_id}:1 htb rate {self.total_bw}kbit ceil {self.total_bw}kbit burst 128k cburst 128k", shell=True)
        subprocess.run("modprobe ifb", shell=True)
        subprocess.run(f"ip link add {IFB_DEV} type ifb", shell=True)
        subprocess.run(f"ip link set {IFB_DEV} up", shell=True)
        subprocess.run(f"tc qdisc add dev {IFB_DEV} root handle {handle_id+1}: htb default 30", shell=True)
        subprocess.run(f"tc qdisc add dev {dev} ingress handle ffff:", shell=True)
        subprocess.run(f"tc class add dev {IFB_DEV} parent {handle_id+1}: classid {handle_id+1}:1 htb rate {self.total_bw}kbit ceil {self.total_bw}kbit burst 128k cburst 128k", shell=True)
        subprocess.run(f"tc filter add dev {dev} parent ffff: protocol all prio 10 u32 match u32 0 0 flowid {handle_id+1}:1 action connmark action mirred egress redirect dev {IFB_DEV}", shell=True)
        for key in ["critical", "high", "low", "system"]:
            classid_egress = self._get_classid(handle_id, key)
            min_bw, max_bw = self._get_class_bandwidth(key)
            burst = self.network_burst_map.get(key, "16k")
            subprocess.run(f"tc class add dev {dev} parent {handle_id}:1 classid {classid_egress} htb rate {min_bw}kbit ceil {max_bw}kbit burst {burst} cburst {burst}", shell=True)
            classid_ifb = self._get_classid(handle_id+1, key)
            subprocess.run(f"tc class add dev {IFB_DEV} parent {handle_id+1}:1 classid {classid_ifb} htb rate {min_bw}kbit ceil {max_bw}kbit burst {burst} cburst {burst}", shell=True)
        self._get_set_networked_system_ports()
        self.ingress_classids = self._get_all_classids(self.handle_id, direction="ingress")
        self.egress_classids = self._get_all_classids(self.handle_id, direction="egress")

    def _limit_network_class(self, dev, classid, min_bw, max_bw=None, burst="16k", direction="egress", level=None):
        if max_bw is None:
            max_bw = min_bw
        subprocess.run(f"tc class change dev {dev} classid {classid} htb rate {min_bw}kbit ceil {max_bw}kbit burst {burst} cburst {burst}", shell=True)

    def update_app_network_control(self):
        controlled_apps = app_utils.get_controlled_apps_net() or []
        handle_id = self.handle_id
        dev = self.dev
        IFB_DEV = self.IFB_DEV
        new_app_ids = set(app.get("app_id") for app in controlled_apps)
        old_app_ids = set(self.app_filter_info.keys())
        # 1. 移除已不存在的 app
        for app_id in old_app_ids - new_app_ids:
            self._remove_app_network_rules(app_id)
        # 2. 检查 priority 变化，或新 app
        for idx, app in enumerate(controlled_apps):
            app_id = app.get("app_id")
            new_priority = app.get("priority", "low")
            if app_id in old_app_ids:
                old_info = self.app_filter_info.get(app_id, {})
                old_priority = old_info.get("priority", "low")
                if old_priority != new_priority:
                    self._remove_app_network_rules(app_id)
                    self._add_app_network_rules(app, idx)
            else:
                self._add_app_network_rules(app, idx)
            # 保证 OUTPUT 链只存在一个 CONNMARK --save-mark 规则，并且在所有 MARK 规则之后
            subprocess.run("iptables -t mangle -D OUTPUT -j CONNMARK --save-mark || true", shell=True)
            subprocess.run("iptables -t mangle -A OUTPUT -j CONNMARK --save-mark", shell=True)

    def _get_classid(self, handle, priority):
        mapping = {"critical": 10, "high": 20, "low": 30, "system": 5}
        num = mapping.get(priority, 30)
        return f"{handle}:{num}"

    def _get_class_bandwidth(self, priority):
        bw = self.config_network_bw.get(priority, {})
        min_bw = bw.get("min", 0)
        max_bw = bw.get("max", 0)
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

    def _recover_network_pressure(self, limit_stage, direction, dev, handle_id, rates, config_network_bw, config_total_rate, actual_total_bw, limit_stage_attr):
        handle = handle_id if direction == "egress" else handle_id + 1
        limit_stage_to_priority = {
            1: "low",
            2: "low",
            3: "high",
            4: "high"
        }
        key = limit_stage_to_priority.get(limit_stage, "high")
        min_bw = config_network_bw[key]["min"]
        max_bw = config_network_bw[key]["max"]
        classid = self._get_classid(handle, key)
        burst = self.network_burst_map.get(key, "16k")
        current_class_bw = rates.get(classid, 0)
        half_bw = int((max_bw - min_bw) / 2 + min_bw)
        critical_threshold = self.config.network_thresholds["critical"] * config_total_rate
        # 从哪个阶段开始恢复
        stage_table = {
            1: (half_bw, 0, 0),
            2: (min_bw, 0, 1),
            3: (half_bw, 2, 2),
            4: (min_bw, 2, 3),
        }
        stage_transition_point, stage_full, stage_half = stage_table.get(limit_stage, (min_bw, 0, 0))
        # 分级进行恢复，从高到低，每次一个类别 (half -> max)
        if limit_stage > 0:
            if current_class_bw < stage_transition_point * 0.9:
                self._limit_network_class(dev, classid, min_bw, max_bw, burst, direction=direction, level=key)
                setattr(self, limit_stage_attr, stage_full)
                logger.info(f"{direction.upper()} 恢复 {key} app class流量，完全放开到 {max_bw} kbit/s")
            else:
                if limit_stage in (4, 2):
                    expected_total_bw = half_bw + actual_total_bw - min_bw
                else:
                    expected_total_bw = max_bw + actual_total_bw - half_bw
                if expected_total_bw < critical_threshold:
                    if limit_stage in (4, 2):
                        self._limit_network_class(dev, classid, min_bw, half_bw, burst, direction=direction, level=key)
                        logger.info(f"{direction.upper()} 恢复 {key} app class 放开一半流量到 {half_bw} kbit/s")
                    else:
                        self._limit_network_class(dev, classid, min_bw, max_bw, burst, direction=direction, level=key)
                        logger.info(f"{direction.upper()} 恢复 {key} app class流量，完全放开到 {max_bw} kbit/s")
                    setattr(self, limit_stage_attr, stage_half)
                else:
                    logger.info(f"{direction.upper()} {key} app class保持限速 {stage_transition_point} kbit/s，恢复会导致总流量超出临界阈值")

    def _apply_bandwidth_limit(self, stage, direction, handle_id, config_network_bw, rates, limit_stage_attr):
        handle = handle_id if direction == "egress" else handle_id + 1
        dev = self.dev if direction == "egress" else self.IFB_DEV
        limit_stage_to_priority = {
            0: "low",
            1: "low",
            2: "high",
            3: "high"
        }
        key = limit_stage_to_priority.get(stage, "high")
        min_bw = config_network_bw[key]["min"]
        max_bw = config_network_bw[key]["max"]
        classid = self._get_classid(handle, key)
        burst = self.network_burst_map.get(key, "16k")
        current_stage_bw = rates.get(classid, 0)
        half_bw = int((max_bw - min_bw) / 2 + min_bw)
        # 限速到哪个阶段
        stage_table = {
            0: (half_bw, 2, 1),
            1: (min_bw, 2, 2),
            2: (half_bw, 4, 3),
            3: (min_bw, 4, 4),
        }
        stage_transition_point, stage_full, stage_half = stage_table.get(stage, (min_bw, 0, 0))
        # 分级进行限速，从低到高，每次一个类别
        if stage in (0, 2):
            if current_stage_bw < stage_transition_point:
                self._limit_network_class(dev, classid, min_bw, min_bw, burst, direction=direction, level=key)
                setattr(self, limit_stage_attr, stage_full)
                logger.info(f"{direction.upper()} 限速 {key} class app 流量到 {min_bw}")
            else:
                self._limit_network_class(dev, classid, min_bw, half_bw, burst, direction=direction, level=key)
                setattr(self, limit_stage_attr, stage_half if half_bw != min_bw else stage_full)
                logger.info(f"{direction.upper()} 限速 {key} class app 流量到 {half_bw}")
        elif stage in (1, 3):
            self._limit_network_class(dev, classid, min_bw, min_bw, burst, direction=direction, level=key)
            setattr(self, limit_stage_attr, stage_full)
            logger.info(f"{direction.upper()} 再次限速 {key} class app 流量到 {min_bw}")

    def _can_switch(self, cooldown, last_limit_time, last_recover_time):
        time_since_limit = time.time() - last_limit_time
        time_since_recover = time.time() - last_recover_time
        return time_since_limit > cooldown and time_since_recover > cooldown

    def handle_network_pressure(self, tx_pressure, rx_pressure, ingress_rates, egress_rates, network_data):
        handle_id = self.handle_id
        config_network_bw = self.config_network_bw
        config_total_rate = self.config.network_bandwidth_kbit
        tx_total_bw = self.total_bw * network_data['tx']
        rx_total_bw = self.total_bw * network_data['rx']
        # TX 限速
        if tx_pressure == "critical" and self._can_switch(self.limit_cooldown, self.tx_last_limit_time, self.tx_last_recover_time):
            self._apply_bandwidth_limit(self.tx_network_limit_stage, "egress", handle_id, config_network_bw, egress_rates, "tx_network_limit_stage")
            self.tx_last_limit_time = time.time()
        # RX 限速
        if rx_pressure == "critical" and self._can_switch(self.limit_cooldown, self.rx_last_limit_time, self.rx_last_recover_time):
            self._apply_bandwidth_limit(self.rx_network_limit_stage, "ingress", handle_id, config_network_bw, ingress_rates, "rx_network_limit_stage")
            self.rx_last_limit_time = time.time()
        # TX 压力恢复
        if tx_pressure != "critical" and self.tx_network_limit_stage > 0 and self._can_switch(self.recover_cooldown, self.tx_last_limit_time, self.tx_last_recover_time):
            self._recover_network_pressure(
                self.tx_network_limit_stage,
                "egress",
                self.dev,
                handle_id,
                egress_rates,
                config_network_bw,
                config_total_rate,
                tx_total_bw,
                "tx_network_limit_stage"
            )
            self.tx_last_recover_time = time.time()
        # RX 压力恢复
        if rx_pressure != "critical" and self.rx_network_limit_stage > 0 and self._can_switch(self.recover_cooldown, self.rx_last_limit_time, self.rx_last_recover_time):
            self._recover_network_pressure(
                self.rx_network_limit_stage,
                "ingress",
                self.IFB_DEV,
                handle_id,
                ingress_rates,
                config_network_bw,
                config_total_rate,
                rx_total_bw,
                "rx_network_limit_stage"
            )
            self.rx_last_recover_time = time.time()

    def clear_network_rules_on_exit(self):
        if not self.enable_network_control:
            logger.info("NetworkControl 被禁用，跳过清理 tc 队列和 iptables 规则")
            return
        dev = self.dev
        IFB_DEV = self.IFB_DEV
        handle_id = self.handle_id
        subprocess.run(f"tc qdisc del dev {dev} handle {handle_id}: root", shell=True)
        subprocess.run(f"tc qdisc del dev {IFB_DEV} handle {handle_id+1}: root", shell=True)
        subprocess.run(f"tc qdisc del dev {dev} ingress", shell=True)
        for app_id, info in list(self.app_filter_info.items()):
            mark = info.get("mark")
            cgroup_path = info.get("cgroup_path")
            if cgroup_path and mark:
                subprocess.run(f"iptables -t mangle -D OUTPUT -m cgroup --path {cgroup_path} -j MARK --set-mark {int(mark,16)}", shell=True)
        logger.info("已清理所有由 balancer 创建的 tc 队列和 iptables mark 规则")
