// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

/* coverity_stub.h */
#ifndef COVERITY_STUB_H
#define COVERITY_STUB_H

#define _ASM_X86_BITOPS_H
#define _ASM_X86_PTRACE_H
#define _ASM_X86_CURRENT_H
#define _LINUX_TYPES_H
#define __KERNEL__

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef unsigned long long u64;
typedef long long s64;

#define NULL ((void*)0)

#define BPF_PERF_OUTPUT(name) \
    struct { int (*perf_submit)(void*, void*, int); } name
#define BPF_HASH(name, ...) int name
#define TRACEPOINT_PROBE(category, event) \
    struct tracepoint_args_##category##_##event; \
    void tracepoint_##category##_##event(struct tracepoint_args_##category##_##event *args)

void bpf_get_current_comm(void *buf, int size);
u64 bpf_get_current_pid_tgid(void);
u64 bpf_get_current_task(void);
long bpf_probe_read_user(void *dst, int size, const void *unsafe_ptr);
long bpf_probe_read_user_str(void *dst, int size, const void *unsafe_ptr);
long bpf_probe_read_kernel_str(void *dst, int size, const void *unsafe_ptr);

struct task_struct { char comm[16]; int pid; };
struct pt_regs { unsigned long ip; }; 

#endif
