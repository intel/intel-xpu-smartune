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


from bcc import BPF
import ctypes

# 定义与BPF代码中相同的常量
COMM_LEN = 32
PY_MAX_TARGET_LEN = 32
MAX_DYNAMIC_APPS = 32  # 添加最大动态应用数限制


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class AppIntercept(metaclass=SingletonMeta):

    def __init__(self, c_src_file="bpf_event.c"):
        self.bpf = BPF(src_file=c_src_file)

    def trace_print(self):
        self.bpf.trace_print()

    # 事件处理函数
    def print_event(self, cpu, event, size):
        event = self.bpf["events"].event(event)
        filename = event.filename.decode('utf-8', 'ignore')
        comm = event.comm.decode('utf-8', 'ignore')
        blocked_type = event.blocked_type.decode('utf-8', 'ignore')

        print(f"BLOCKED({blocked_type}): PID={event.pid}, COMM={comm}, FILENAME={filename}")

    def add_to_blacklist(self, app_name):
        print(f"add_to_blacklist... '{app_name}'")

        # 定义结构体类型
        class AppName(ctypes.Structure):
            _fields_ = [("name", ctypes.c_char * PY_MAX_TARGET_LEN)]

        # 创建结构体实例
        value = AppName()

        # 准备要写入的字符串（确保以null结尾）
        app_name_bytes = app_name.encode('utf-8')[:PY_MAX_TARGET_LEN - 1]
        value.name = app_name_bytes + b'\0'

        # 查找下一个可用的key
        next_key = 0
        while next_key < MAX_DYNAMIC_APPS:
            try:
                # 尝试获取key，如果不存在则使用这个key
                _ = self.bpf["blocked_apps"][ctypes.c_uint32(next_key)]
                next_key += 1
            except KeyError:
                break

        if next_key >= MAX_DYNAMIC_APPS:
            print("Blacklist is full, cannot add more apps")
            return

        key = ctypes.c_uint32(next_key)

        # 打印调试信息
        print(f"Setting key={key.value}, value.name={value.name} (len={len(app_name_bytes)})")

        # 更新map
        self.bpf["blocked_apps"][key] = value

        # 验证是否设置成功
        val = self.bpf["blocked_apps"][key]
        print(f"Verification: stored value={val.name.decode('utf-8', errors='replace')}")

        print(f"Added '{app_name}' to dynamic blacklist")


if __name__ == "__main__":
    # 初始化BPF
    bpf_monitor = AppIntercept()

    bpf_monitor.add_to_blacklist("firefox")
    bpf_monitor.add_to_blacklist("wget")

    # 打开性能缓冲区
    print("Opening perf buffer...")
    bpf_monitor.bpf["events"].open_perf_buffer(bpf_monitor.print_event)
    print("Monitoring execve()... Ctrl+C to exit")

    while True:
        try:
            # 同时处理trace打印和事件
             bpf_monitor.bpf.perf_buffer_poll(timeout=100)
            # bpf.trace_print()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")
            break


