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
import re
from typing import List, Dict, Optional


def _check_pids_io_usage(running_pids: List[int], threshold_mb: float = 10.0) -> Dict[str, any]:
    """
    批量同步采样多个PID（同一App）的磁盘IO使用情况，判断是否超过指定MB/s阈值
    :param running_pids: 目标PID列表（同一App的多个进程PID）
    :param threshold_mb: 磁盘IO告警阈值（单位：MB/s），默认10MB/s
    :return: 包含采样结果、总IO速率、繁忙判断的结构化字典
    """
    # ---------------------- 第一步：参数合法性校验 ----------------------
    # 校验PID列表
    if not isinstance(running_pids, List) or len(running_pids) == 0:
        raise ValueError("错误：running_pids必须为非空的整数列表")
    for pid in running_pids:
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError(f"错误：PID {pid} 无效，必须为正整数")

    # 校验阈值参数
    if not isinstance(threshold_mb, (int, float)) or threshold_mb < 0:
        raise ValueError("错误：threshold_mb必须为非负数字（单位：MB/s）")

    # ---------------------- 第二步：配置采样参数（满足0.6秒完成采样） ----------------------
    sample_times: int = 3  # 采样次数，固定3次
    sample_interval: float = 0.2  # 单次采样间隔，固定0.2秒（总时长=3*0.2=0.6秒）
    kb_to_mb: float = 1024.0  # KB转MB的换算系数

    # ---------------------- 第三步：构造iotop命令（多PID同步采样） ----------------------
    # iotop命令基础参数（非交互、多PID、仅显示有IO活动、KB单位、指定采样次数/间隔）
    iotop_cmd = [
        "sudo",
        "iotop",
        "-b",  # 非交互模式（批量输出，适合捕获结果）
        "-o",  # 仅显示有磁盘IO活动的进程（减少无效数据）
        "-k",  # 以KB为单位显示数据（便于转换为MB）
        "-n", str(sample_times),  # 采样次数
        "-d", str(sample_interval)  # 采样间隔（秒）
    ]

    # 为每个PID添加 -p 参数（实现多PID同步监控，一次命令完成所有PID采样）
    for pid in running_pids:
        iotop_cmd.extend(["-p", str(pid)])

    # ---------------------- 第四步：正则表达式（匹配多PID的IO数据） ----------------------
    # 匹配格式示例：79075  ...  0.00 K/s  1250.00 K/s  ...  gnome-calculator
    io_pattern = re.compile(
        r"(?P<pid>\d+)\s+.+?\s+(?P<read_kb>\d+\.\d+)\s+K/s\s+(?P<write_kb>\d+\.\d+)\s+K/s"
    )

    # ---------------------- 第五步：执行iotop命令并捕获输出 ----------------------
    try:
        result = subprocess.run(
            iotop_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )

        # 处理命令执行异常
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            if "no such file or directory" in error_msg.lower():
                raise Exception("未安装iotop，请先执行 sudo apt install iotop 或 sudo yum install iotop 安装")
            elif "permission denied" in error_msg.lower():
                raise Exception("缺少sudo权限，请提供管理员权限执行该函数")
            else:
                raise Exception(f"iotop命令执行失败：{error_msg}")

        # ---------------------- 第六步：解析输出，汇总所有PID的IO数据 ----------------------
        # 初始化数据存储：key=PID，value=包含读/写KB/s列表的字典
        pid_io_data: Dict[int, Dict[str, List[float]]] = {
            pid: {"read_kb_list": [], "write_kb_list": []} for pid in running_pids
        }
        output_lines = result.stdout.strip().split("\n")

        for line in output_lines:
            line = line.strip()
            # 跳过表头、空行（不处理无效数据）
            if not line or "PID" in line or "DISK READ" in line:
                continue

            # 匹配单条IO记录（对应某个PID的一次采样数据）
            match = io_pattern.search(line)
            if match:
                pid = int(match.group("pid"))
                read_kb = float(match.group("read_kb"))
                write_kb = float(match.group("write_kb"))

                # 仅记录目标PID列表中的数据（过滤可能的无关进程）
                if pid in pid_io_data:
                    pid_io_data[pid]["read_kb_list"].append(read_kb)
                    pid_io_data[pid]["write_kb_list"].append(write_kb)

        # ---------------------- 第七步：计算汇总指标（转换为MB/s，判断繁忙状态） ----------------------
        # 1. 计算每个PID的平均IO速率（KB/s -> MB/s）
        pid_avg_io: Dict[int, Dict[str, float]] = {}
        total_read_mb_per_sec: float = 0.0
        total_write_mb_per_sec: float = 0.0

        for pid, io_data in pid_io_data.items():
            read_list = io_data["read_kb_list"]
            write_list = io_data["write_kb_list"]

            # 无有效采样数据的PID，平均速率记为0
            avg_read_kb = sum(read_list) / len(read_list) if read_list else 0.0
            avg_write_kb = sum(write_list) / len(write_list) if write_list else 0.0

            # 转换为MB/s（保留4位小数，避免精度丢失）
            avg_read_mb = round(avg_read_kb / kb_to_mb, 4)
            avg_write_mb = round(avg_write_kb / kb_to_mb, 4)

            # 存入单个PID平均数据
            pid_avg_io[pid] = {
                "avg_read_mb_per_sec": avg_read_mb,
                "avg_write_mb_per_sec": avg_write_mb,
                "total_io_mb_per_sec": round(avg_read_mb + avg_write_mb, 4)
            }

            # 累加至App总IO速率
            total_read_mb_per_sec += avg_read_mb
            total_write_mb_per_sec += avg_write_mb

        # 2. 计算App总IO速率，判断是否超过阈值
        app_total_io_mb_per_sec = round(total_read_mb_per_sec + total_write_mb_per_sec, 4)
        is_disk_busy = app_total_io_mb_per_sec > threshold_mb

        # ---------------------- 第八步：构造并返回结构化结果 ----------------------
        return {
            "app_io_summary": {
                "total_read_mb_per_sec": round(total_read_mb_per_sec, 4),
                "total_write_mb_per_sec": round(total_write_mb_per_sec, 4),
                "total_io_mb_per_sec": app_total_io_mb_per_sec,
                "threshold_mb_per_sec": threshold_mb,
                "is_disk_busy": is_disk_busy,  # True=繁忙（超过阈值），False=空闲（低于阈值）
                "sample_config": {
                    "sample_times": sample_times,
                    "sample_interval_sec": sample_interval,
                    "total_sample_duration_sec": round(sample_times * sample_interval, 2)
                }
            },
            "individual_pid_io": pid_avg_io,  # 单个PID的详细IO数据
            "input_params": {
                "running_pids": running_pids,
                "threshold_mb": threshold_mb
            }
        }

    except Exception as e:
        print(f"错误：获取多PID IO使用情况时发生异常 - {str(e)}")
        return {
            "app_io_summary": {"is_disk_busy": False, "error": str(e)},
            "individual_pid_io": {},
            "input_params": {"running_pids": running_pids, "threshold_mb": threshold_mb}
        }


if __name__ == "__main__":
    # 示例：同一App的多个PID（替换为你的实际PID列表）
    target_pids = [79074, 79075, 79076, 79077, 79078, 79079, 79080]

    # 调用函数：判断是否超过10MB/s阈值（默认值，可自定义如threshold_mb=20）
    io_check_result = _check_pids_io_usage(
        running_pids=target_pids,
        threshold_mb=10.0
    )

    # 打印结果（格式化输出，便于查看）
    print("=" * 80)
    print("App 磁盘IO使用情况检查结果")
    print("=" * 80)

    # 打印汇总信息
    summary = io_check_result["app_io_summary"]
    if "error" not in summary:
        print(f"\n【汇总信息】")
        print(f"  总磁盘读取速率：{summary['total_read_mb_per_sec']} MB/s")
        print(f"  总磁盘写入速率：{summary['total_write_mb_per_sec']} MB/s")
        print(f"  总磁盘IO速率：{summary['total_io_mb_per_sec']} MB/s")
        print(f"  告警阈值：{summary['threshold_mb_per_sec']} MB/s")
        print(f"  App磁盘状态：{'繁忙（超过阈值）' if summary['is_disk_busy'] else '空闲（低于阈值）'}")
        print(
            f"  采样配置：{summary['sample_config']['sample_times']}次采样，间隔{summary['sample_config']['sample_interval_sec']}秒，总时长{summary['sample_config']['total_sample_duration_sec']}秒")

        # 打印单个PID详细信息
        print(f"\n【单个PID详细IO数据】")
        for pid, pid_io in io_check_result["individual_pid_io"].items():
            print(f"  PID {pid}：")
            print(f"    平均读取速率：{pid_io['avg_read_mb_per_sec']} MB/s")
            print(f"    平均写入速率：{pid_io['avg_write_mb_per_sec']} MB/s")
            print(f"    平均总IO速率：{pid_io['total_io_mb_per_sec']} MB/s")
    else:
        print(f"\n【错误信息】：{summary['error']}")

    print("\n" + "=" * 80)

