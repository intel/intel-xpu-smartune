
//
//  Copyright (C) 2025 Intel Corporation
//
//  This software and the related documents are Intel copyrighted materials,
//  and your use of them is governed by the express license under which they
//  were provided to you ("License"). Unless the License provides otherwise,
//  you may not use, modify, copy, publish, distribute, disclose or transmit
//  his software or the related documents without Intel's prior written permission.
//
//  This software and the related documents are provided as is, with no express
//  or implied warranties, other than those that are expressly stated in the License.
//


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
    if (!str || !substr || substr[0] == '\0') {
        return 0;
    }

    for (int j = 0; j < MAX_TARGET_LEN && str[j] != '\0'; j++) {
        int k = 0;
        while (k < MAX_TARGET_LEN && substr[k] != '\0' && str[j + k] == substr[k]) {
            k++;
        }
        if (k < MAX_TARGET_LEN && substr[k] == '\0') {
            return 1;
        }
    }
    return 0;
}

static inline int bpf_strstr(const char *str, const char *substr) {
    if (!str || !substr || substr[0] == '\0') return 0;

    for (int i = 0; i < COMM_LEN && str[i] != '\0'; i++) {
        int match = 1;
        #pragma unroll
        for (int j = 0; j < MAX_TARGET_LEN; j++) {
            if (substr[j] == '\0') break;
            if (str[i+j] == '\0' || str[i+j] != substr[j]) {  // 添加对str边界的检查
                match = 0;
                break;
            }
        }
        if (match && substr[0] != '\0') {  // 确保匹配的是完整子串
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

            bpf_trace_printk("BLOCKED(static): comm=%s\n", comm);
            bpf_trace_printk("BLOCKED(static): path=%s\n", fname);
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

                bpf_trace_printk("BLOCKED(dynamic): comm=%s\n", event.comm);
                bpf_trace_printk("BLOCKED(dynamic): path=%s\n", event.filename);
                bpf_trace_printk("Submitting event: pid=%d\n", event.pid);
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