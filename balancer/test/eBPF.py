# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from bcc import BPF
import ctypes

# 定义与BPF代码中相同的常量
COMM_LEN = 32
PY_MAX_TARGET_LEN = 32

bpf_code = """
#include <uapi/linux/ptrace.h>

#define COMM_LEN 32
#define MAX_TARGET_LEN 32
// 使用结构体定义应用名
struct appname_t {
    char name[MAX_TARGET_LEN];
};

// 定义BPF map来存储动态黑名单
BPF_HASH(blocked_apps, u32, struct appname_t);

// 初始静态黑名单
static const char INITIAL_TARGETS[][MAX_TARGET_LEN] = {
    "firefox", "chrome", "chromium", "edge", "brave",
    "gedit", "notepad", "kate", "pluma", "gnome-text-editor",
    "gnome-system-monitor", "top", "htop", "btop"
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

TRACEPOINT_PROBE(syscalls, sys_enter_execve) {
    const char **argv = (const char **)args->argv;
    char fname[256] = {0};
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
        if (is_substring(comm, INITIAL_TARGETS[i]) || is_substring(fname, INITIAL_TARGETS[i])) {
            bpf_trace_printk("BLOCKED(static): comm=%s\\n", comm);
            bpf_trace_printk("BLOCKED(static): path=%s\\n", fname);
            bpf_send_signal(9);
            return 0;
        }
    }

    // 2. 检查动态黑名单
    u32 key = 0;
    struct appname_t *target = blocked_apps.lookup(&key);
    if (target) {
        bpf_trace_printk("Dynamic target found: %s\\n", target->name);
        if (target->name[0] != '\\0' &&
            (is_substring(comm, target->name) || is_substring(fname, target->name))) {
            bpf_trace_printk("BLOCKED(dynamic): comm=%s\\n", comm);
            bpf_trace_printk("BLOCKED(dynamic): path=%s\\n", fname);
            bpf_send_signal(9);
            return 0;
        }
    } else {
        bpf_trace_printk("No dynamic target found\\n");
    }

    return 0;
}
"""

# 初始化BPF
bpf = BPF(text=bpf_code)


def add_to_blacklist(app_name):
    # 定义结构体类型
    class AppName(ctypes.Structure):
        _fields_ = [("name", ctypes.c_char * PY_MAX_TARGET_LEN)]

    # 创建结构体实例
    value = AppName()

    # 准备要写入的字符串（确保以null结尾）
    app_name_bytes = app_name.encode('utf-8')[:PY_MAX_TARGET_LEN - 1]
    value.name = app_name_bytes + b'\0'

    # Use ctypes.create_string_buffer to handle the string
    # buffer = ctypes.create_string_buffer(null_terminated, PY_MAX_FILE_LEN)
    # ctypes.memmove(value.name, buffer, len(buffer))

    # 使用固定键0
    key = ctypes.c_uint32(0)

    # 打印调试信息
    print(f"Setting key={key.value}, value.name={value.name} (len={len(app_name_bytes)})")

    # 更新map
    bpf["blocked_apps"][key] = value

    # 验证是否设置成功
    val = bpf["blocked_apps"][key]
    print(f"Verification: stored value={val.name.decode('utf-8', errors='replace')}")

    print(f"Added '{app_name}' to dynamic blacklist")


# 示例：添加"ls"到黑名单
add_to_blacklist("ls")

print("Monitoring execve()... Ctrl+C to exit")
bpf.trace_print()
