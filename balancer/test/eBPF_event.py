# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from bcc import BPF
import ctypes

# 定义与BPF代码中相同的常量
COMM_LEN = 32
PY_MAX_TARGET_LEN = 32
MAX_DYNAMIC_APPS = 32  # 添加最大动态应用数限制

bpf_code = """
#include <uapi/linux/ptrace.h>
#include <linux/sched.h>

#define COMM_LEN 32
#define MAX_TARGET_LEN 32
#define MAX_DYNAMIC_APPS 32  // 在BPF代码中也定义

// 定义事件结构
struct event_t {
    u32 pid;
    char comm[COMM_LEN];
    char filename[64];
    char blocked_type[16];  // "static" or "dynamic"
};

// 使用结构体定义应用名
struct appname_t {
    char name[MAX_TARGET_LEN];
};

// 定义BPF map来存储动态黑名单
BPF_HASH(blocked_apps, u32, struct appname_t);
BPF_PERF_OUTPUT(events);

// 初始静态黑名单
static const char INITIAL_TARGETS[][MAX_TARGET_LEN] = {
    "chrome", "chromium", "edge", "brave", "notepad"
};

static inline int is_substring(const char *str, const char *substr) {
    if (!str || !substr || substr[0] == '\\0') {
        return 0;
    }

    for (int j = 0; j < MAX_TARGET_LEN && str[j] != '\\0'; j++) {
        int k = 0;
        while (k < MAX_TARGET_LEN && substr[k] != '\\0' && str[j + k] == substr[k]) {
            k++;
        }
        if (k < MAX_TARGET_LEN && substr[k] == '\\0') {
            return 1;
        }
    }
    return 0;
}

static inline int bpf_strstr(const char *str, const char *substr) {
    if (!str || !substr || substr[0] == '\\0') return 0;

    for (int i = 0; i < COMM_LEN && str[i] != '\\0'; i++) {
        int match = 1;
        #pragma unroll
        for (int j = 0; j < MAX_TARGET_LEN; j++) {
            if (substr[j] == '\\0') break;
            if (str[i+j] == '\\0' || str[i+j] != substr[j]) {  // 添加对str边界的检查
                match = 0;
                break;
            }
        }
        if (match && substr[0] != '\\0') {  // 确保匹配的是完整子串
            return 1;
        }
    }
    return 0;
}

TRACEPOINT_PROBE(syscalls, sys_enter_execve) {
    const char **argv = (const char **)args->argv;
    char fname[64] = {0};
    char comm[COMM_LEN] = {0};

    bpf_get_current_comm(&comm, sizeof(comm));

    const char *fname_ptr = NULL;
    bpf_probe_read_user(&fname_ptr, sizeof(fname_ptr), &argv[0]);
    if (!fname_ptr || bpf_probe_read_user_str(fname, sizeof(fname), fname_ptr) < 0) {
        return 0;
    }

    // 1. 检查静态黑名单
    #pragma unroll
    for (int i = 0; i < sizeof(INITIAL_TARGETS)/sizeof(INITIAL_TARGETS[0]); i++) {
        if (is_substring(fname, INITIAL_TARGETS[i])) {
            // 创建并提交事件
            struct event_t event = {};
            u64 pid_tgid = bpf_get_current_pid_tgid();
            event.pid = pid_tgid >> 32;
            bpf_probe_read_kernel_str(&event.comm, sizeof(event.comm), comm);
            bpf_probe_read_kernel_str(&event.filename, sizeof(event.filename), fname);
            bpf_probe_read_kernel_str(&event.blocked_type, sizeof(event.blocked_type), "static");

            bpf_trace_printk("BLOCKED(static): comm=%s\\n", comm);
            bpf_trace_printk("BLOCKED(static): path=%s\\n", fname);
            events.perf_submit(args, &event, sizeof(event));
            bpf_send_signal(9);
            return 0;
        }
    }

    // 2. 检查动态黑名单    
    u32 key = 0;
    struct appname_t *val;
    int count = 0;

    while (count < MAX_DYNAMIC_APPS && (val = blocked_apps.lookup(&key))) {
        if (val) {
            if (bpf_strstr(fname, val->name)) {
                // 创建并提交事件
                struct event_t event = {};
                u64 pid_tgid = bpf_get_current_pid_tgid();
                event.pid = pid_tgid >> 32;
                bpf_probe_read_kernel_str(&event.comm, sizeof(event.comm), comm);
                bpf_probe_read_kernel_str(&event.filename, sizeof(event.filename), fname);
                bpf_probe_read_kernel_str(&event.blocked_type, sizeof(event.blocked_type), "dynamic");

                bpf_trace_printk("BLOCKED(dynamic): comm=%s\\n", event.comm);
                bpf_trace_printk("BLOCKED(dynamic): path=%s\\n", event.filename);
                bpf_trace_printk("Submitting event: pid=%d\\n", event.pid);
                events.perf_submit(args, &event, sizeof(event));
                bpf_send_signal(9);
                return 0;
            }
        }
        key++;
        count++;
    }

    return 0;
}
"""

# 初始化BPF
bpf = BPF(text=bpf_code)


# 事件处理函数
def print_event(cpu, event, size):
    event = bpf["events"].event(event)
    filename = event.filename.decode('utf-8', 'ignore')
    comm = event.comm.decode('utf-8', 'ignore')
    blocked_type = event.blocked_type.decode('utf-8', 'ignore')

    print(f"BLOCKED({blocked_type}): PID={event.pid}, COMM={comm}, FILENAME={filename}")


def add_to_blacklist(app_name):
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
            _ = bpf["blocked_apps"][ctypes.c_uint32(next_key)]
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
    bpf["blocked_apps"][key] = value

    # 验证是否设置成功
    val = bpf["blocked_apps"][key]
    print(f"Verification: stored value={val.name.decode('utf-8', errors='replace')}")

    print(f"Added '{app_name}' to dynamic blacklist")


# 示例：添加"ls"到黑名单
add_to_blacklist("firefox")
add_to_blacklist("wget")

# 打开性能缓冲区
print("Opening perf buffer...")
bpf["events"].open_perf_buffer(print_event)
print("Monitoring execve()... Ctrl+C to exit")

while True:
    try:
        # 同时处理trace打印和事件
        bpf.perf_buffer_poll(timeout=100)
        # bpf.trace_print()
    except KeyboardInterrupt:
        break
    except Exception as e:
        print(f"Error: {e}")
        break