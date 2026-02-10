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


import os, signal, time
from dataclasses import dataclass
from typing import Dict, Optional, Union

from collections import OrderedDict
from monitor.appIntercept import AppIntercept

from utils.logger import logger
from utils import app_utils
from config.config import b_config
import threading
from multiprocessing import JoinableQueue
import queue
import heapq
from controller.network import NetworkController
from controller.io import IOController


g_limited_apps = OrderedDict()  # 记录被限制的应用
g_limited_apps_manual = OrderedDict()  # 记录被限制的应用
g_app_id_mapping = {}  # {app_id: effective_app_id}
is_limited_app_dominant = False  # critical情况下，用于判断拿到的Top进程是否为已限制的进程

@dataclass
class WorkloadGroup:
    name: str
    priority: int
    cpu_weight: int
    memory_min: int = 0
    io_weight: int = 100


@dataclass
class WorkloadTask:
    workload: WorkloadGroup
    params: Dict
    pid: Optional[int] = None
    task_id: str = ""


class MaxPriorityQueue:
    def __init__(self):
        self._queue = queue.PriorityQueue()
        self._index = 0  # 用于处理相同优先级的情况

    def put(self, item):
        # 存储负值实现最大堆，使用三元组 (负优先级, 自增索引, 数据)
        priority = -item[1]
        heapq.heappush(self._queue.queue, (priority, self._index, item))
        self._index += 1

    def get(self):
        # 获取时取出原始数据
        return heapq.heappop(self._queue.queue)[-1]

    def remove_if(self, condition_func):
        """
        删除满足条件的项目（通用方法，不涉及业务逻辑）
        :param condition_func: 接受一个队列项目（元组 (data, priority)），返回 bool
        :return: 被删除的项目列表
        """
        removed_items = []
        new_queue = []

        for priority, idx, item in self._queue.queue:
            if condition_func(item):
                removed_items.append(item)
            else:
                new_queue.append((priority, idx, item))

        self._queue.queue = new_queue
        heapq.heapify(self._queue.queue)  # 重新堆化
        return removed_items

    def empty(self):
        """检查队列是否为空"""
        return len(self._queue.queue) == 0

    def __str__(self):
        # 按优先级降序展示（实际存储是升序）
        items = sorted(((-priority, data) for priority, _, data in self._queue.queue), reverse=True)
        return str([(k, v) for (_, (k, v)) in items])

    def __len__(self):
        """获取队列当前元素数量"""
        return len(self._queue.queue)


class DynamicBalancer:
    def __init__(self):
        self.bpf_monitor = AppIntercept("monitor/bpf_event.c")
        self.config = b_config
        self.controlManager = self.bpf_monitor.controlManager
        self.resource_monitor = self.controlManager.res
        self.io_ctl = IOController()

        # 资源管理
        self.workload_groups = {}  # 注册的workload类型
        self.running_tasks = {}  # pid -> WorkloadTask
        self.known_pids = set()  # 已识别的PID集合

        self.is_running = False
        self.app_detect_queue = JoinableQueue(1000000)
        self.app_priority_queue = MaxPriorityQueue()

        self._init_default_workloads()

        # 网络控制器
        self.network_controller = NetworkController()

    def _init_default_workloads(self):
        default_groups = [
            WorkloadGroup("critical", 100, 300, 2<<30, 500),
            WorkloadGroup("high", 80, 200, 1<<30, 300),
            WorkloadGroup("normal", 50, 100, 0, 200),
            WorkloadGroup("best-effort", 20, 50, 0, 100)
        ]
        # for group in default_groups:
        #     self.register_workload_group(group)

    def start(self):
        """
        启动服务，包括启动服务线程来处理任务队列中的任务
        """
        self.network_controller.setup_tc_classes_and_filters()
        self.is_running = True

        self.monitor_thread = threading.Thread(target=self._run_monitor_resource_loop, daemon=True)
        self.monitor_thread.start()

        self.handle_thread = threading.Thread(target=self._run_handle_loop, daemon=True)
        self.handle_thread.start()

        self.app_intercept_thread = threading.Thread(target=self._run_app_intercept_loop, daemon=True)
        self.app_intercept_thread.start()

        logger.info("服务已启动，线程已开始运行")

    def _run_monitor_resource_loop(self):
        logger.info("Monitor resource service started")
        global g_limited_apps, is_limited_app_dominant
        idle_check_interval = 10  # 单位：秒
        last_check_time = 0
        last_network_sample_time = 0
        network_sample_interval = 5  # 网络采样间隔（秒）
        top_consume_apps = []  # 保存获取到的top应用列表
        reach_threshold = False  # 有些app可能资源占用非常低，限制意义不大
        restore_pending = False  # 标记是否有待恢复的应用
        pressure_start_time = None  # 记录压力值进入medium/low的时间
        current_pressure = None  # 记录当前的压力值，主要用于判断压力状态是否稳定
        STABLE_PERIOD = 1800  # 30分钟的稳定期（秒）
        disk_io_not_stressed_start_time = None  # 记录Disk IO压力解除的时间
        STABLE_DISK_IO_PERIOD = 300  # 5分钟的稳定期（秒）
        policy = self.config.limit_policy['policy']

        def reset_state():
            nonlocal top_consume_apps, idle_check_interval, pressure_start_time
            # logger.debug("reset_state called")
            top_consume_apps = []
            idle_check_interval = 10
            pressure_start_time = None  # 重置计时器

        def handle_network_operations():
            nonlocal last_network_sample_time, current_time
            if self.network_controller.enable_network_control:
                self.network_controller.update_app_network_control()
                self.network_controller.network.get_tc_class_stats(self.network_controller.IFB_DEV,
                                                                   self.network_controller.handle_id + 1,
                                                                   classids=self.network_controller.ingress_classids,
                                                                   direction="ingress")
                self.network_controller.network.get_tc_class_stats(self.network_controller.dev,
                                                                   self.network_controller.handle_id,
                                                                   classids=self.network_controller.egress_classids,
                                                                   direction="egress")
            self.network_controller.network.sample_network_pressure()
            if current_time - last_network_sample_time >= network_sample_interval:
                # 采样 ingress 流量
                last_network_sample_time = current_time
                network_data = self.network_controller.network.get_current_pressure()
                tx_pressure, rx_pressure = self.controlManager.update_network_pressure_level(network_data)
                tx_total_bw = self.network_controller.total_bw * network_data['tx']
                rx_total_bw = self.network_controller.total_bw * network_data['rx']
                logger.debug(
                    f"NetworkMonitor {self.network_controller.dev} TX level: {tx_pressure} (pressure: {network_data['tx']:.2f}),"
                    f" RX level: {rx_pressure} (pressure: {network_data['rx']:.2f})")
                if self.network_controller.enable_network_control:
                    ingress_rates = self.network_controller.network.get_tc_class_stats_rate_ingress()
                    egress_rates = self.network_controller.network.get_tc_class_stats_rate_egress()
                    rates = self.network_controller.get_rates(self.network_controller.handle_id, egress_rates,
                                                              ingress_rates)
                    logger.debug(
                        f"NetworkMonitor {self.network_controller.dev} TX_total_BW={tx_total_bw:,.2f}kbit/s (App Class BW: System - {rates["egress_system"]:,.2f},"
                        f" Critical - {rates["egress_critical"]:,.2f} , High - {rates["egress_high"]:,.2f}, Low - {rates["egress_low"]:,.2f}),"
                        f" RX_total_BW={rx_total_bw:,.2f}kbit/s (App Class BW: System - {rates["ingress_system"]:,.2f},"
                        f" Critical - {rates["ingress_critical"]:,.2f} , High - {rates["ingress_high"]:,.2f}, Low - {rates["ingress_low"]:,.2f})")
                    self.network_controller.handle_network_pressure(tx_pressure, rx_pressure, ingress_rates,
                                                                    egress_rates, network_data)
        while self.is_running:
            try:
                current_time = time.time()

                # 当队列不为空时立即处理，为空时每10s检查一次
                if not self.app_priority_queue.empty() or (current_time - last_check_time) >= idle_check_interval:
                    if policy == "separated":
                        pressure, is_disk_io_stressed = self.controlManager.get_current_pressure_level()
                    else:  # policy == "combined"
                        pressure, _ = self.controlManager.get_current_pressure_level()
                        is_disk_io_stressed = False

                    last_check_time = current_time

                    if policy == "separated":
                        if pressure == "critical" or is_disk_io_stressed:
                            # 重置low状态计时器
                            restore_pending = False

                            if not is_disk_io_stressed:
                                pressure_start_time = None
                                top_consume_apps, reach_threshold = self.resource_monitor.get_top_resource_consumers()
                            else:
                                disk_io_not_stressed_start_time = None
                                top_consume_apps = self.resource_monitor.get_top_disk_io_consumers()
                                reach_threshold = True  # IO压力默认视为达到阈值
                            # logger.debug(f"Top resource consumers(currently = 1): {top_consume_apps}")
                            """
                                Top resource consumers:[
                                   {
                                      "process":{
                                         "pid":5508,
                                         "name":"stress",
                                         "cmdline":"stress --cpu 25 --io 30 --vm 3 --vm-bytes 21G",
                                         "score":469.53,
                                         "cpu_avg":96.2,
                                         "mem_rss":16.11,
                                         "io_read_rate":0.0
                                      },
                                      "app":{
                                         "type":"cgroup",
                                         "id":"vte-spawn-4573f009-2887-47a4-a7d8-f573b6965109.scope",
                                         "name":"CGroup: vte-spawn-4573f009-2887-47a4-a7d8-f573b6965109.scope"
                                      }
                                   },
                                   {  # Only top1 currently.
                                     ...
                                   },
                                   ...
                                ]
                            """
                            if top_consume_apps:
                                # 判断该进程是否被限制过
                                for app_info in top_consume_apps:
                                    current_app_id = (app_info.get('app') or {}).get('id')

                                    if current_app_id in g_limited_apps:
                                        _, _, _, state = g_limited_apps[current_app_id]
                                        is_limited_app_dominant = (state != "partially_restored")  # 仅当未部分恢复时视为已限制
                                        break
                                    else:
                                        is_limited_app_dominant = False

                                logger.debug(f"Balance- was the process limited before? {is_limited_app_dominant}")
                                self.controlManager.set_limited_app_dominant(is_limited_app_dominant)

                                if not is_disk_io_stressed:
                                    should_adjust, is_controlled, app_id, limit_rates = self._handle_critical_pressure(
                                        top_consume_apps, reach_threshold)
                                else:
                                    should_adjust, is_controlled, app_id, limit_rates = self._handle_disk_io_stressed(
                                        top_consume_apps)

                                if not is_limited_app_dominant and reach_threshold and should_adjust and app_id:
                                    self._apply_resource_limits(
                                        top_consume_apps[0],
                                        app_id,
                                        limit_rates,
                                        is_controlled,
                                        is_disk_io_stressed=is_disk_io_stressed  # 标记是否为Disk IO压力场景
                                    )

                                # 无论是否处理，都移除已检查的app
                                top_consume_apps.pop(0)
                                # idle_check_interval = 5  # critical下，缩短检测时间
                            else:
                                reset_state()

                        elif not self.app_priority_queue.empty():
                            # 处理队列中的应用
                            app_data, priority = self.app_priority_queue.get()
                            logger.info(
                                f"Starting app: {app_data['app_name']} (PID: {app_data['pid']}, Priority: {priority})")
                            os.kill(app_data['pid'], signal.SIGCONT)
                            app_utils.update_app_status(app_data['app_id'], "running")
                            app_utils.callback_manager.send_callback_notification({
                                'app_id': app_data['app_id'],
                                'app_name': app_data['app_name'],
                                'status': "running",
                                'purpose': "app"
                            }, True)
                            # 处理完队列后重置top应用状态
                            reset_state()
                        else:
                            if g_limited_apps and not restore_pending:
                                should_check_pressure = (pressure in ("medium", "low") and
                                                         any(app_data[2].get('cpu_mem_limited', False) for app_data in
                                                             g_limited_apps.values()))
                                should_check_io = (not is_disk_io_stressed and
                                                   any(app_data[2].get('io_limited', False) for app_data in
                                                       g_limited_apps.values()))
                                if should_check_pressure or should_check_io:
                                    # 压力级别不稳定需要重新计时
                                    logger.info(f"pressure_start_time: {pressure_start_time}, "
                                                f"current_pressure: {current_pressure}, pressure: {pressure}")
                                    if should_check_pressure:
                                        if (pressure_start_time is None) or (current_pressure != pressure):
                                            pressure_start_time = current_time
                                            current_pressure = pressure
                                            logger.info(
                                                f"Pressure level changed to {pressure}. "
                                                f"Will restore resources after {STABLE_PERIOD} sec if it remains stable.")

                                    if should_check_io:
                                        if disk_io_not_stressed_start_time is None:
                                            disk_io_not_stressed_start_time = current_time
                                            logger.info(
                                                f"Disk IO stress resolved. Will consider for restoration after {STABLE_DISK_IO_PERIOD} sec if it remains stable.")

                                    pressure_stable = (should_check_pressure and
                                                       (current_time - pressure_start_time >= STABLE_PERIOD))
                                    io_stable = (should_check_io and
                                                 (current_time - disk_io_not_stressed_start_time >= STABLE_DISK_IO_PERIOD))
                                    io_double_stable = (should_check_io and
                                                 (current_time - disk_io_not_stressed_start_time >= STABLE_DISK_IO_PERIOD * 2))

                                    logger.info(f"pressure_stable: {pressure_stable}, io_stable: {io_stable}, io_double_stable: {io_double_stable}")

                                    # 检查是否达到稳定期
                                    if pressure_stable and pressure == "medium":
                                        restore_pending = True
                                        app_id, (app_name, limit_rates, limit_parts, state) = next(
                                            iter(g_limited_apps.items()))
                                        if state != "partially_restored":
                                            logger.info(
                                                f"Pressure remained at 'medium' for {STABLE_PERIOD} sec. "
                                                f"Partially restoring app {app_id}.")
                                            if self.restore_resources(app_id, app_name, limit_rates, limit_parts, "partial"):
                                                g_limited_apps[app_id] = (
                                                app_name, limit_rates, limit_parts, "partially_restored")
                                            else:
                                                logger.warning(f"Partial restore failed for {app_name}")
                                            g_limited_apps.move_to_end(app_id)
                                    elif io_stable and not io_double_stable:
                                        restore_pending = True
                                        app_id, (app_name, limit_rates, limit_parts, state) = next(
                                            iter(g_limited_apps.items()))
                                        if state != "partially_restored":
                                            logger.info(f"Disk IO stress resolved. Partially restoring app {app_id}.")
                                            if self.restore_resources(app_id, app_name, limit_rates, limit_parts, "partial"):
                                                g_limited_apps[app_id] = (
                                                app_name, limit_rates, limit_parts, "partially_restored")
                                            else:
                                                logger.warning(f"Partial restore failed for {app_name}")
                                            g_limited_apps.move_to_end(app_id)
                                    elif (pressure_stable and pressure == "low") or io_double_stable:
                                        restore_pending = True
                                        app_id, (app_name, limit_rates, limit_parts, state) = next(
                                            iter(g_limited_apps.items()))

                                        # 执行恢复操作
                                        success = self.restore_resources(app_id, app_name, limit_rates, limit_parts,
                                                                         "full")
                                        if success:
                                            updated_limits = g_limited_apps[app_id][2]
                                            is_fully_restored = not (
                                                        updated_limits.get('cpu_mem_limited') or updated_limits.get('io_limited'))
                                            if is_fully_restored:
                                                app_utils.update_app_status(app_id, "running")
                                                app_utils.callback_manager.send_callback_notification({
                                                    'app_id': app_id,
                                                    'app_name': app_name,
                                                    'status': "running",
                                                    'purpose': "app"
                                                }, False)
                                                g_limited_apps.pop(app_id, None)
                                                logger.info(f"Fully restored app {app_id}, removed from limited apps")

                                                # 仅在完全恢复且IO稳定时重置计时器
                                                if io_double_stable:
                                                    disk_io_not_stressed_start_time = None
                                                    logger.debug("Reset IO stress timer after full restoration")
                                            else:
                                                g_limited_apps.move_to_end(app_id)
                                                logger.info(f"Partial restore for app {app_id}, moved to end of queue")
                                        else:
                                            logger.error(f"Failed to restore resources for app {app_id}")
                                            g_limited_apps.move_to_end(app_id)
                                    restore_pending = False
                                else:
                                    reset_state()
                    elif policy == "combined":
                        if pressure == "critical":
                            # 重置low状态计时器
                            pressure_start_time = None
                            # 如果是第一次检测到critical状态，获取top应用列表
                            restore_pending = False
                            if not top_consume_apps:
                                top_consume_apps, reach_threshold = self.resource_monitor.get_top_resource_consumers()
                                # logger.debug(f"Top resource consumers(currently = 1): {top_consume_apps}")

                            if top_consume_apps:
                                # 判断该进程是否被限制过
                                for app_info in top_consume_apps:
                                    current_app_id = (app_info.get('app') or {}).get('id')

                                    if current_app_id in g_limited_apps:
                                        _, _, _, state = g_limited_apps[current_app_id]
                                        is_limited_app_dominant = (state != "partially_restored")  # 仅当未部分恢复时视为已限制
                                        break
                                    else:
                                        is_limited_app_dominant = False

                                logger.debug(f"Balance- was the process limited before? {is_limited_app_dominant}")
                                self.controlManager.set_limited_app_dominant(is_limited_app_dominant)
                                # 调用独立的处理函数
                                should_adjust, is_controlled, app_id, limit_rates = self._handle_critical_pressure(
                                    top_consume_apps, reach_threshold)

                                if not is_limited_app_dominant and reach_threshold and should_adjust and app_id:
                                    # 执行资源调整
                                    target = top_consume_apps[0]
                                    app_name = target.get('process', {}).get('name') or ''
                                    total_mem = self.resource_monitor.get_total_memory()
                                    logger.info(f"Adjusting resources for app: {app_id}")

                                    # 初始化限制结果标志
                                    resource_limited = False
                                    io_limited = False

                                    # CPU/内存限制
                                    cpu_rate = int(100 * limit_rates["cpu_rate"]) if limit_rates.get("cpu_rate") else None
                                    mem_rate = int(total_mem * limit_rates["mem_rate"]) if limit_rates.get(
                                        "mem_rate") else None

                                    if (cpu_rate is not None or mem_rate is not None) and self.is_running:
                                        auto_limit = self.controlManager.adjust_resources(
                                            app_id,
                                            "critical",
                                            cpu_quota=cpu_rate,
                                            mem_high=mem_rate,
                                        )
                                        if auto_limit:
                                            resource_limited = True
                                            logger.info(f"Successfully limited CPU/Memory for {app_name}")
                                        else:
                                            logger.warning(f"Failed to limit CPU/Memory for {app_name}")

                                    # 磁盘IO限制
                                    io_limits = limit_rates.get("disk_io_rate", {})
                                    if io_limits and self.is_running:
                                        limits = {
                                            "default": {  # 如果需要为不同disk设置不同参数，可增加类似"nvme0n1": {...}配置
                                                "rbps": io_limits['read'] * 1024 ** 2,
                                                "wbps": io_limits['write'] * 1024 ** 2,
                                                "wiops": io_limits['write_iops'],
                                                "riops": io_limits['read_iops']
                                            }
                                        }
                                        io_limited = self.io_ctl.set_disk_io_throttle(
                                            app_id,
                                            limits=limits
                                        )
                                        if not io_limited:
                                            logger.error(f"Failed to set write IO limit for {app_name}")

                                    # 只要CPU/内存或IO有一个限制成功，就记录到g_limited_apps
                                    if resource_limited or io_limited:
                                        g_limited_apps[app_id] = (app_name, limit_rates, {
                                            'cpu_mem_limited': resource_limited,
                                            'io_limited': io_limited
                                        }, None)  # None 表示完全限制

                                        if is_controlled:
                                            app_utils.update_app_status(app_id, "limited")

                                        app_utils.callback_manager.send_callback_notification({
                                            'app_id': app_id,
                                            'app_name': app_name,
                                            'status': "limited",
                                            'purpose': "app"
                                        }, False)
                                    else:
                                        logger.warning(f"No resource limits successfully applied for {app_name}")

                                # 无论是否处理，都移除已检查的app
                                top_consume_apps.pop(0)
                                # idle_check_interval = 5  # critical下，缩短检测时间
                            else:
                                reset_state()
                        elif not self.app_priority_queue.empty():
                            # 处理队列中的应用
                            app_data, priority = self.app_priority_queue.get()
                            logger.info(
                                f"Starting app: {app_data['app_name']} (PID: {app_data['pid']}, Priority: {priority})")
                            os.kill(app_data['pid'], signal.SIGCONT)
                            app_utils.update_app_status(app_data['app_id'], "running")
                            app_utils.callback_manager.send_callback_notification({
                                'app_id': app_data['app_id'],
                                'app_name': app_data['app_name'],
                                'status': "running",
                                'purpose': "app"
                            }, True)
                            # 处理完队列后重置top应用状态
                            reset_state()
                        else:
                            # 非 critical 状态：等待 STABLE_PERIOD 后恢复
                            if g_limited_apps and not restore_pending:
                                if pressure in ("medium", "low"):
                                    # 压力级别不稳定需要重新计时，只有压力稳定在某个级别才执行对应的动作
                                    if (pressure_start_time is None) or (current_pressure != pressure):
                                        pressure_start_time = current_time
                                        current_pressure = pressure
                                        logger.info(
                                            f"Pressure level changed to {pressure}. "
                                            f"Will restore resources after {STABLE_PERIOD} sec if it remains stable."
                                        )

                                    # 检查是否达到稳定期
                                    elif current_time - pressure_start_time >= STABLE_PERIOD:
                                        restore_pending = True

                                        # 根据压力级别选择恢复策略
                                        if pressure == "medium":
                                            # 这时还不能移除，因为还没全部恢复
                                            app_id, (app_name, limit_rates, limit_parts, state) = next(iter(g_limited_apps.items()))
                                            if state != "partially_restored":
                                                total_mem = self.resource_monitor.get_total_memory()
                                                logger.info(
                                                    f"Pressure remained at 'medium' for {STABLE_PERIOD} sec. "
                                                    f"Partially restoring app {app_id} (twice the rate of limited resources)."
                                                )

                                                restore_success = True

                                                # 恢复CPU/内存（如果之前限制了）
                                                if limit_parts.get('cpu_mem_limited', False):
                                                    cpu_restore = int(100 * limit_rates[
                                                        "cpu_rate"] * 2) if "cpu_rate" in limit_rates else None
                                                    mem_restore = int(total_mem * limit_rates[
                                                        "mem_rate"] * 2) if "mem_rate" in limit_rates else None

                                                    if (cpu_restore is not None or mem_restore is not None) and self.is_running:
                                                        cpu_mem_restored = self.controlManager.adjust_resources(
                                                            app_id,
                                                            "medium",
                                                            cpu_quota=cpu_restore,
                                                            mem_high=mem_restore,
                                                            is_restore=False,
                                                        )
                                                        if not cpu_mem_restored:
                                                            logger.error(
                                                                f"Failed to partially restore CPU/Memory for {app_name}")
                                                            restore_success = False

                                                # 恢复IO限制（如果之前限制了）
                                                if (limit_parts.get('io_limited', False) and "disk_io_rate" in limit_rates) and self.is_running:
                                                    io_restored = True
                                                    io_limits = limit_rates["disk_io_rate"]

                                                    limits = {
                                                        "default": {  # 如果需要为不同disk设置不同参数，可增加类似"nvme0n1": {...}配置
                                                            "rbps": io_limits['read'] * 2 * 1024 ** 2,
                                                            "wbps": io_limits['write'] * 2 * 1024 ** 2,
                                                            "wiops": io_limits['write_iops'] * 2,
                                                            "riops": io_limits['read_iops'] * 2
                                                        }
                                                    }
                                                    io_limited = self.io_ctl.set_disk_io_throttle(
                                                        app_id,
                                                        limits=limits
                                                    )

                                                    if not io_limited:
                                                        logger.error(
                                                            f"Failed to partially restore disk IO for {app_name}")
                                                        io_restored = False

                                                    if not io_restored:
                                                        restore_success = False

                                                # 更新状态
                                                if restore_success:
                                                    g_limited_apps[app_id] = (
                                                    app_name, limit_rates, limit_parts, "partially_restored")
                                                else:
                                                    logger.warning(f"Partial restore failed for {app_name}")

                                                g_limited_apps.move_to_end(app_id)  # 移到末尾防止重复限制同一个app
                                        else:  # pressure == "low"
                                            # 完全恢复并移除
                                            app_id, (app_name, _, limit_parts, _) = g_limited_apps.popitem()
                                            logger.info(
                                                f"Pressure remained at 'low' for {STABLE_PERIOD} sec. "
                                                f"Fully restoring app {app_id} (100% resources)."
                                            )

                                            restore_success = True

                                            # 恢复CPU/内存（如果之前限制了）
                                            if limit_parts.get('cpu_mem_limited', False) and self.is_running:
                                                if not self.controlManager.adjust_resources(app_id, "low"):
                                                    logger.error(f"Failed to fully restore CPU/Memory for {app_name}")
                                                    restore_success = False

                                            # 恢复IO限制（如果之前限制了）
                                            if limit_parts.get('io_limited', False) and self.is_running:
                                                io_restored = True

                                                # 移除IO限制
                                                if not self.io_ctl.restore_disk_io_throttle(app_id):
                                                    logger.error(f"Failed to remove IO limits for {app_name}")
                                                    io_restored = False

                                                if not io_restored:
                                                    restore_success = False

                                            # 资源完全恢复后，需要通知用户
                                            if restore_success:
                                                app_utils.update_app_status(app_id, "running")
                                                app_utils.callback_manager.send_callback_notification({
                                                    'app_id': app_id,
                                                    'app_name': app_name,
                                                    'status': "running",
                                                    'purpose': "app"
                                                }, False)
                                            else:
                                                logger.error(f"Failed to fully restore resources for {app_name}")

                                        restore_pending = False
                                        reset_state()  # 重置计时器和当前压力状态
                                else:
                                    reset_state()
                handle_network_operations()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in monitor loop: {str(e)}", exc_info=True)
                reset_state()
                time.sleep(1)

        logger.info("Monitor resource service stopped")

    def _run_handle_loop(self):
        logger.info("Resource handle service is wait for processing")
        while self.is_running:
            try:
                # 从app_detect_queue任务队列中获取任务并处理
                coming_app = self.bpf_monitor.app_pending_queue.get(block=True, timeout=5)
                logger.info(f"_run_handle_loop: Processing app {coming_app}")

                # 从DB中获取coming_app priority,如没有设置，就是low
                # priority = "1000"  # critical
                # priority_value = {"Calculator": 1000, "test2": 1500, "test3": 1300}
                priority = self.controlManager.get_app_priority(app_name=coming_app["app_name"])
                logger.info(f"_run_handle_loop: App {coming_app['app_name']} priority is {priority}")

                # # 将任务放入待处理队列
                priority_num = self.controlManager.get_priority_value(priority)
                logger.debug(f"_run_handle_loop: priority value is {priority_num}")
                self.app_priority_queue.put((coming_app, priority_num))
                logger.info(f"_run_handle_loop: Resource insufficient, {coming_app} app added to pending queue")

            except:
                time.sleep(2)
        logger.debug("退出_run_handle_loop")

    def _run_app_intercept_loop(self):
        logger.info("Resource app intercept service is wait for processing")

        # 打开性能缓冲区
        self.bpf_monitor.bpf["events"].open_perf_buffer(self.bpf_monitor.print_event)
        logger.debug("Ctrl+C to exit")

        monitor_apps = app_utils.get_controlled_apps()

        if monitor_apps:
            # 将受控应用添加到BPF监控列表
            monitored_names = [app["app_name"] for app in monitor_apps]
            self.bpf_monitor.add_to_monitorlist(monitored_names)
            logger.info(f"Monitoring execve() for: {', '.join(monitored_names)}")

            # 为critical应用调整OOM优先级
            logger.debug(f"monitor_apps: {monitor_apps}")
            for app in monitor_apps:
                app_utils.adjust_oom_priority(app["app_id"], app["app_name"], app["priority"], app.get("cmdline", ""))
        else:
            logger.warning("No controlled apps to monitor")

        while self.is_running:
            try:
                # 监控启动事件
                self.bpf_monitor.bpf.perf_buffer_poll(timeout=100)
            except KeyboardInterrupt:
                logger.debug("Exiting...")
                break
            except Exception as e:
                logger.error(f"App intercept error: {str(e)}")
                time.sleep(3)
                break

    def _apply_resource_limits(self, target_app, app_id, limit_rates, is_controlled, is_disk_io_stressed=False):
        """应用资源限制（公共逻辑）"""
        app_name = target_app.get('process', {}).get('name') or ''
        total_mem = self.resource_monitor.get_total_memory()
        logger.info(f"Adjusting resources for app: {app_id}")

        # 初始化限制结果标志
        resource_limited = False
        io_limited = False

        # CPU/内存限制（仅非IO压力场景处理）
        if not is_disk_io_stressed:
            cpu_rate = int(100 * limit_rates["cpu_rate"]) if limit_rates.get("cpu_rate") else None
            mem_rate = int(total_mem * limit_rates["mem_rate"]) if limit_rates.get("mem_rate") else None

            if (cpu_rate is not None or mem_rate is not None) and self.is_running:
                auto_limit = self.controlManager.adjust_resources(
                    app_id,
                    "critical",
                    cpu_quota=cpu_rate,
                    mem_high=mem_rate,
                )
                if auto_limit:
                    resource_limited = True
                    logger.info(f"Successfully limited CPU/Memory for {app_name}")

        # 磁盘IO限制
        if is_disk_io_stressed and limit_rates.get("disk_io_rate"):
            io_limits = limit_rates.get("disk_io_rate", {})
            if io_limits and self.is_running:
                limits = {
                    "default": { # 如果需要为不同disk设置不同参数，可增加类似"nvme0n1": {...}配置
                        "rbps": io_limits['read'] * 1024 ** 2,
                        "wbps": io_limits['write'] * 1024 ** 2,
                        "wiops": io_limits['write_iops'],
                        "riops": io_limits['read_iops']
                    }
                }
                io_limited = self.io_ctl.set_disk_io_throttle(app_id, limits=limits)
                if not io_limited:
                    logger.error(f"Failed to set IO limit for {app_name}")

        # 记录限制结果
        if resource_limited or io_limited:
            g_limited_apps[app_id] = (
                app_name,
                limit_rates,
                {'cpu_mem_limited': resource_limited, 'io_limited': io_limited},
                None  # None表示完全限制
            )

            if is_controlled:
                app_utils.update_app_status(app_id, "limited")

            app_utils.callback_manager.send_callback_notification({
                'app_id': app_id,
                'app_name': app_name,
                'status': "limited",
                'purpose': "app"
            }, False)

    def restore_resources(self, app_id, app_name, limit_rates, limit_parts, restore_type):
        """
        通用资源恢复逻辑
        :param app_id: 应用ID
        :param app_name: 应用名称
        :param limit_rates: 限制速率配置
        :param limit_parts: 限制部分标志
        :param restore_type: 恢复类型（"partial" 或 "full"）
        :return: 是否恢复成功，以及恢复的部分详情
        """
        global g_limited_apps
        restore_success = True

        if self.is_running:
            # 恢复CPU/内存
            if limit_parts.get('cpu_mem_limited', False):
                if restore_type == "partial":
                    cpu_restore = int(100 * limit_rates["cpu_rate"] * 2) if "cpu_rate" in limit_rates else None
                    mem_restore = int(self.resource_monitor.get_total_memory() * limit_rates[
                        "mem_rate"] * 2) if "mem_rate" in limit_rates else None
                    if not self.controlManager.adjust_resources(
                        app_id, "medium", cpu_quota=cpu_restore, mem_high=mem_restore, is_restore=False
                    ):
                        logger.error(f"Failed to partially restore CPU/Memory for {app_name}")
                        restore_success = False
                else:  # full restore
                    cpu_mem_restored = self.controlManager.adjust_resources(app_id, "low")
                    if not cpu_mem_restored:
                        logger.error(f"Failed to fully restore CPU/Memory for {app_name}")
                        restore_success = False
                    else:
                        g_limited_apps[app_id] = (app_name, limit_rates, {
                            'cpu_mem_limited': False,
                            'io_limited': limit_parts['io_limited']
                        }, None)
            # 恢复IO限制
            if limit_parts.get('io_limited', False):
                if restore_type == "partial" and "disk_io_rate" in limit_rates:
                    io_limits = limit_rates["disk_io_rate"]
                    limits = {
                        "default": {
                            "rbps": io_limits['read'] * 2 * 1024 ** 2,
                            "wbps": io_limits['write'] * 2 * 1024 ** 2,
                            "wiops": io_limits['write_iops'] * 2,
                            "riops": io_limits['read_iops'] * 2
                        }
                    }
                    if not self.io_ctl.set_disk_io_throttle(app_id, limits=limits):
                        logger.error(f"Failed to partially restore disk IO for {app_name}")
                        restore_success = False
                elif restore_type == "full":
                    if not self.io_ctl.restore_disk_io_throttle(app_id):
                        logger.error(f"Failed to fully restore disk IO for {app_name}")
                        restore_success = False
                    else:
                        g_limited_apps[app_id] = (app_name, limit_rates, {
                            'cpu_mem_limited': limit_parts['cpu_mem_limited'],
                            'io_limited': False
                        }, None)

        return restore_success

    def _handle_disk_io_stressed(self, top_consumers):
        """
            Disk IO压力场景处理策略
            Disk IO管控机制与cpu/mem管控不太一样：
            1. 非管控应用：在使用Disk IO造成压力大时，只要管控应用未运行或运行但极少占用I/O，则不干预；否则需让步。
            2. 管控应用：在使用Disk IO造成压力大时，只需要检查critical应用的状态
            3. 关键应用：使用Disk IO时不受干预
        """
        app_info = top_consumers[0] if top_consumers else None
        if not app_info:
            return False, False, None, None

        # 获取管控状态（复用之前抽离的函数）
        app_id = app_info['app'].get('id') if app_info.get('app') else None
        app_name = (app_info.get('process', {}).get('name') or '').lower()

        is_controlled, controlled_data = app_utils.get_app_control_info(app_id, app_name)
        priority = controlled_data.get('priority') if controlled_data else None

        # 场景1：当前进程是非管控应用
        if not is_controlled:
            controlled_apps = app_utils.get_controlled_apps() or []
            # logger.debug(f"Disk IO stressed - checking controlled apps: {controlled_apps}")
            for controlled_app in controlled_apps:
                # 检查该管控应用是否在运行且占用高IO
                running_pids = app_utils.get_app_processes(controlled_app['app_name'])
                logger.debug(f"Disk IO stressed - controlled app {controlled_app['app_name']} running PIDs: {running_pids}")
                if running_pids:
                    # 检查这些PID的磁盘IO占用是否超过阈值或者能否通过PID看到有没有cpu IOwait(不支持)
                    # iotop -b -p <pid> -o -k -n 3 -d 1
                    is_high_io, msg = app_utils.check_pids_disk_io_usage(running_pids, threshold_mb=100)  # 假设阈值100MB/s

                    if is_high_io:  # 非管控应用对disk IO的使用需要让步
                        return True, False, app_id, self.get_limited_rates("undefined")
                    else:
                        logger.info(f"Disk IO stressed - No controlled app with high IO usage found.")
            return False, False, None, None

        # 场景2：当前进程是管控应用，只需要判断critical的app是否在运行且占用IO高
        elif priority != 'critical':
            critical_apps = app_utils.get_controlled_apps(priority="Critical") or []
            for critical_app in critical_apps:
                running_pids = app_utils.get_app_processes(critical_app['app_name'])
                if running_pids:
                    is_high_io = app_utils.check_pids_disk_io_usage([running_pids], threshold_mb=100)
                    if is_high_io:
                        return True, True, app_id, self.get_limited_rates(priority or "undefined")

        return False, False, None, None

    def _handle_critical_pressure(self, top_consumers, reach_threshold):
        """处理资源压力 (单次执行只处理一个app)"""
        if not top_consumers or not top_consumers[0]:
            return False, False, None, None

        # 初始化类成员变量
        self._critical_counter = getattr(self, '_critical_counter', 0)
        self._last_notification_time = getattr(self, '_last_notification_time', 0)

        app_info = top_consumers[0]
        app_id = app_info['app'].get('id') if app_info.get('app') else None
        app_name = (app_info.get('process', {}).get('name') or '').lower()

        is_controlled, controlled_data = app_utils.get_app_control_info(app_id, app_name)
        priority = controlled_data.get('priority') if controlled_data else None

        # 系统资源使用情况
        usage_data = self.resource_monitor.get_resource_usage()
        is_sys_busy = usage_data['cpu']['is_busy'] or usage_data['memory']['is_busy']

        # 情况0：特殊场景处理
        if is_sys_busy and not reach_threshold:
            current_time = time.time()
            if current_time - self._last_notification_time >= self.config.cooldown_time:
                app_utils.callback_manager.send_callback_notification({
                    'app_id': "",
                    'app_name': "",
                    'status': "high_usage_by_multiple_instances",
                    'purpose': "notify"
                }, False)
                self._last_notification_time = current_time
            self._critical_counter = 0
            return False, False, None, None

        # 非管控应用 -> 直接调整, 管控但非critical -> 直接调整
        if not is_controlled or priority != 'critical':
            self._critical_counter = 0
            return True, is_controlled, app_id, self.get_limited_rates(priority or "undefined")

        # critical管控 -> 不处理，增加计数器
        self._critical_counter += 1
        if self._critical_counter >= 1:
            current_time = time.time()
            if current_time - self._last_notification_time >= self.config.cooldown_time:
                app_utils.callback_manager.send_callback_notification({
                    'app_id': "",
                    'app_name': "",
                    'status': "manual_app_limit_by_user",
                    'purpose': "notify"
                }, False)
                self._last_notification_time = current_time
            self._critical_counter = 0

        return False, False, None, None

    def restore_all_limited_apps_resources(self):
        """Restore all limited apps resources"""
        global g_limited_apps, g_limited_apps_manual
        if not g_limited_apps and not g_limited_apps_manual:
            logger.info("No limited apps to restore")
            return

        logger.info(
            f"Restoring resources for {len(g_limited_apps)} limited apps and "
            f"{len(g_limited_apps_manual)} manual limited apps")

        all_limited_apps = {}
        all_limited_apps.update(g_limited_apps)
        all_limited_apps.update(g_limited_apps_manual)

        for app_id, (app_name, _, limit_parts, _) in list(all_limited_apps.items()):
            try:
                app_source = "manual" if app_id in g_limited_apps_manual else "auto"
                logger.info(f"Restoring resources for {app_source} limited app: {app_id}, name: {app_name}")
                restore_success = True

                # Restore CPU/Memory limits
                if limit_parts.get('cpu_mem_limited', False):
                    if not self.controlManager.adjust_resources(app_id, "low"):
                        logger.error(f"Failed to restore CPU/Memory for {app_source} limited app {app_id}")
                        restore_success = False

                # Restore IO limits
                if limit_parts.get('io_limited', False):
                    if not self.io_ctl.restore_disk_io_throttle(app_id):
                        logger.error(f"Failed to remove IO limits for {app_source} limited app {app_id}")
                        restore_success = False

                if restore_success:
                    logger.info(f"{app_source.capitalize()} limited app resources restoration completed")
            except Exception as e:
                logger.error(f"Failed to restore resources for app {app_id}: {str(e)}")
            finally:
                # Remove app from tracking regardless of restore success to avoid repeated attempts on failure
                g_limited_apps.pop(app_id, None)
                g_limited_apps_manual.pop(app_id, None)

        logger.info("All limited apps resources restoration completed")

    def cancel_relaunch_by_app_id(self, app_id: str) -> bool:
        """ 根据 app_id 删除队列中的项目，并杀死对应进程 """
        def condition(item):
            data, _ = item
            return data.get('app_id') == app_id

        # 从队列中删除符合条件的项目
        removed_items = self.app_priority_queue.remove_if(condition)

        # 杀死对应的进程
        killed = False
        for item in removed_items:
            data, _ = item
            pid = data.get('pid')
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed = True
                except ProcessLookupError:
                    pass

        return killed

    def get_limited_rates(self, priority: str) -> Dict[str, Union[float, Dict[str, int], None]]:
        """
        根据优先级获取所有已启用的资源限制配置
        :return:
            {
                "cpu_rate": float比率 或 None,
                "mem_rate": float比率 或 None,
                "disk_io_rate": {"write": x, "read": y} 或 None
            }
        """
        priority = priority.lower()
        result = {
            "cpu_rate": None,
            "mem_rate": None,
            "disk_io_rate": None
        }

        # 检查是否有limit_policy配置
        if not hasattr(self.config, 'limit_policy'):
            return result

        # 处理CPU限制
        if 'cpu' in self.config.limit_policy and self.config.limit_policy['cpu'].get('enabled', False):
            cpu_rates = self.config.limit_policy['cpu'].get('rate', {})
            result['cpu_rate'] = cpu_rates.get(priority)

        # 处理内存限制
        if 'memory' in self.config.limit_policy and self.config.limit_policy['memory'].get('enabled', False):
            mem_rates = self.config.limit_policy['memory'].get('rate', {})
            result['mem_rate'] = mem_rates.get(priority)

        # 处理磁盘IO限制
        if 'disk_io' in self.config.limit_policy and self.config.limit_policy['disk_io'].get('enabled', False):
            disk_rates = self.config.limit_policy['disk_io'].get('rate', {})
            result['disk_io_rate'] = disk_rates.get(priority)

        logger.debug(f"Priority '{priority}' limit rates: {result}")
        return result

    def set_resource_limit(self, app_id: str, app_name: str, priority: str = None) -> bool:
        """设置应用资源限制（平衡版）"""
        global g_limited_apps_manual, g_app_id_mapping

        # Get limit rates based on priority
        priority = priority or "undefined"
        limit_rates = self.get_limited_rates(priority)
        if not limit_rates:
            logger.error(f"No limit rates defined for priority: {priority}")
            return False

        # Get app resource usage data.
        usage = app_utils.get_app_resource_usage(app_id, app_name)
        if usage is None:
            logger.warning(f"No resource usage data for {app_name}, using empty defaults")
            usage = {}

        effective_app_id = os.path.basename(usage.get("cgroup_path", ""))
        cpu_usage_percent = usage.get("cpu_percent", 0) if usage.get("cpu_percent", 0) > 10 else 0
        mem_current = usage.get("mem_current", 0)
        io_read_mb = usage.get("io_read_mb", 0)
        io_write_mb = usage.get("io_write_mb", 0)
        is_io_limit = False if (io_read_mb + io_write_mb) < 100 else True  # 假设100MB/s作为IO压力的阈值

        # Set limits based on usage and configured rates
        cpu_quota = int(cpu_usage_percent * limit_rates["cpu_rate"]) if (limit_rates.get("cpu_rate") and
                                                                         cpu_usage_percent > 0) else None
        mem_high = int(mem_current * limit_rates["mem_rate"]) if limit_rates.get("mem_rate") else None
        io_limits = limit_rates.get("disk_io_rate", {})

        logger.debug(f"Calculated limits - CPU: {cpu_quota if cpu_quota else 'No Limit'}, "
                     f"Memory: {mem_high if mem_high else 'No Limit'}, is_io_limit: {is_io_limit}")

        # 5. 应用资源限制
        resource_limited = False
        io_limited = False

        # CPU/内存限制
        if (cpu_quota is not None or mem_high is not None) and self.is_running:
            if self.controlManager.adjust_resources(
                    effective_app_id, "critical",
                    cpu_quota=cpu_quota,
                    mem_high=mem_high
            ):
                resource_limited = True
                # The memory limit will affect the data of PSI, causing misjudgment of the system pressure,
                # and it is necessary to reduce the effect of data on psi
                self.controlManager.set_limited_app_dominant(True)
                logger.info(f"Successfully set CPU/Memory limits for {app_name}")
            else:
                logger.error(f"Failed to set CPU/Memory limits for {app_name}")

        # 磁盘IO限制
        if is_io_limit and io_limits and self.is_running:
            limits = {
                "default": {  # 如果需要为不同disk设置不同参数，可增加类似"nvme0n1": {...}配置
                    "rbps": io_limits['read'] * 1024 ** 2,
                    "wbps": io_limits['write'] * 1024 ** 2,
                    "wiops": io_limits['write_iops'],
                    "riops": io_limits['read_iops']
                }
            }
            io_limited = self.io_ctl.set_disk_io_throttle(
                effective_app_id,
                limits=limits
            )

            if io_limited:
                logger.info(f"Successfully set disk IO limits for {app_name}")
            else:
                logger.error(f"Failed to set disk IO limit for {app_name}")

        # 6. 记录限制状态（只要有一个限制成功就记录）
        if resource_limited or io_limited:
            g_limited_apps_manual[effective_app_id] = (app_name, limit_rates, {
                'cpu_mem_limited': resource_limited,
                'io_limited': io_limited
            }, None)  # None 表示完全限制
            g_app_id_mapping[app_id] = effective_app_id
            app_utils.update_app_status(app_id, "a_limited")
            app_utils.callback_manager.send_callback_notification({
                'app_id': app_id,
                'app_name': app_name,
                'status': "a_limited",
                'purpose': "app"
            }, False)
            logger.info(f"Recorded resource limits for {app_name}")
            return True

        logger.warning(f"No resource limits successfully applied for {app_name}")
        return False

    def set_restore_resource(self, app_id: str) -> bool:
        """根据 app_id 恢复资源限制"""
        global g_limited_apps_manual, g_app_id_mapping

        # 获取有效应用ID
        effective_app_id = g_app_id_mapping.pop(app_id, app_id)
        app_name, _, limit_parts, _ = g_limited_apps_manual.pop(effective_app_id, None)
        restore_success = True
        try:
            logger.info(f"Restoring resources for app: {app_id}, name: {app_name}")

            # 恢复CPU/内存
            if limit_parts.get('cpu_mem_limited', False):
                if not self.controlManager.adjust_resources(effective_app_id, "low"):
                    logger.error(f"Failed to restore CPU/Memory for {app_id}")
                    restore_success = False

            # 恢复IO限制
            if limit_parts.get('io_limited', False):
                if not self.io_ctl.restore_disk_io_throttle(effective_app_id):
                    logger.error(f"Failed to remove IO limits for {app_id}")
                    restore_success = False

            if restore_success:
                app_utils.update_app_status(app_id, "running")
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "running",
                    'purpose': "app"
                }, False)
                logger.info(f"Resources restored for {app_id}")

            return restore_success
        except Exception as e:
            logger.error(f"Failed to restore resources for {app_id}: {str(e)}")
            return False
        finally:
            # PSI data is may not right, so we need to delay the reset of dominant status.
            time.sleep(self.config.regular_update_sys_pressure_time)
            self.controlManager.set_limited_app_dominant(False)

    def _execute_task(self, task: WorkloadTask, pressure_level: str) -> bool:
        """执行任务"""
        try:
            if task.pid:
                self.running_tasks[task.pid] = task
                logger.info("Task %s registered (PID: %d)", task.workload.name, task.pid)

                self.controlManager.adjust_resources("", pressure_level)
                return True
            return False
        except Exception as e:
            logger.error("Task registration failed: %s", str(e))
            return False


    def register_workload_group(self, group: WorkloadGroup):
        """注册可用的workload类型"""
        with self._lock:
            self.workload_groups[group.name] = group
            logger.info(f"Registered workload group: {group.name}")


    def add_workload(self, group_name: str, params: Dict = None) -> bool:
        """添加具体任务到队列"""
        if group_name not in self.workload_groups:
            logger.error(f"Unknown workload group: {group_name}")
            return False

        task = {
            "type": "new_app",
            "group": group_name,
            "params": params or {},
            "task_id": f"wl_{time.time_ns()}"
        }
        self.push_task(task)
        logger.debug(f"add workload to task: {task}")
        return True


    def shutdown(self):
        """
        停止服务线程，设置运行标志为False，并等待线程结束，同时确保任务队列中的任务都已处理完成
        """
        logger.info("服务开始停止.")
        if not self.is_running:
            logger.debug("服务已经停止，无需再次操作")
            return
        self.is_running = False

        self.restore_all_limited_apps_resources()
        self.network_controller.clear_network_rules_on_exit()
        if hasattr(self, "monitor_thread"):
            self.monitor_thread.join(timeout=1)  # 等待线程结束
        if hasattr(self, "handle_thread"):
            self.handle_thread.join(timeout=1)
        if hasattr(self, "app_intercept_thread"):
            self.app_intercept_thread.join(timeout=1)
        logger.info("服务已停止，线程已结束")
