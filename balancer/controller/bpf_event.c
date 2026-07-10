// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

#ifndef __COVERITY__
#include <uapi/linux/ptrace.h>
#include <linux/sched.h>
#else
#include "vmlinux.h"
#endif

#define COMM_LEN 32
#define MAX_FILE_LEN 64

enum event_type {
    APP_START,
    APP_EXIT
};

struct event_t {
    u32 pid;
    u32 type;  // APP_START or APP_EXIT
    char comm[COMM_LEN];
    char filename[MAX_FILE_LEN];
};

BPF_PERF_OUTPUT(events);

// Trace execve syscall (app launch)
TRACEPOINT_PROBE(syscalls, sys_enter_execve) {
    const char **argv = (const char **)args->argv;
    char fname[MAX_FILE_LEN] = {0};
    char comm[COMM_LEN] = {0};

    bpf_get_current_comm(&comm, sizeof(comm));

    const char *fname_ptr = NULL;
    bpf_probe_read_user(&fname_ptr, sizeof(fname_ptr), &argv[0]);
    if (!fname_ptr || bpf_probe_read_user_str(fname, sizeof(fname), fname_ptr) < 0) {
        return 0;
    }

    struct event_t event = {};
    u64 pid_tgid = bpf_get_current_pid_tgid();
    event.pid = pid_tgid >> 32;
    event.type = APP_START;
    bpf_probe_read_kernel_str(&event.comm, sizeof(event.comm), comm);
    bpf_probe_read_kernel_str(&event.filename, sizeof(event.filename), fname);

    events.perf_submit(args, &event, sizeof(event));

    return 0;
}

// Trace process exit
TRACEPOINT_PROBE(sched, sched_process_exit) {
    struct task_struct *task = (struct task_struct *)bpf_get_current_task();

    char comm[COMM_LEN];
    bpf_probe_read_kernel_str(&comm, sizeof(comm), task->comm);

    struct event_t event = {};
    u64 pid_tgid = bpf_get_current_pid_tgid();
    event.pid = pid_tgid >> 32;
    event.type = APP_EXIT;
    bpf_probe_read_kernel_str(&event.comm, sizeof(event.comm), comm);
    // For exit events, reuse comm as filename
    bpf_probe_read_kernel_str(&event.filename, sizeof(event.filename), comm);

    events.perf_submit(args, &event, sizeof(event));

    return 0;
}