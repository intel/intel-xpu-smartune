#!/bin/bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

SCRIPT_DIR=$(cd "$(dirname "$0")"; pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.."; pwd)

VMLINUX_H="$SCRIPT_DIR/vmlinux.h"
echo "Generating vmlinux.h at $VMLINUX_H ..."
bpftool btf dump file /sys/kernel/btf/vmlinux format c > "$VMLINUX_H"

BCC_MACROS=(
    "-D__KERNEL__"
    "-D__BPF_TRACING__"
    "-D__TARGET_ARCH_x86"
    "-D__COVERITY__"
    "-DNULL=((void*)0)"
    "-DBPF_PERF_OUTPUT(x)=struct { int (*perf_submit)(void*, void*, int); } x"
    "-DTRACEPOINT_PROBE(x,y)=struct tracepoint_args_##x##_##y { const char **argv; }; int tracepoint_##x##_##y(struct tracepoint_args_##x##_##y *args)"
    "-DBPF_HASH(name, ...)=struct { void* (*lookup)(void*); int (*update)(void*, void*); int (*delete)(void*); } name"
    "-Dbpf_get_current_task()=( (struct task_struct *)0 )"
    "-Dbpf_get_current_pid_tgid()=( (u64)0 )"
)

FILES=(
    "$PROJECT_ROOT/balancer/monitor/bpf_event.c"
    "$PROJECT_ROOT/balancer/test/bpf_event_direct.c"
    "$PROJECT_ROOT/balancer/test/bpf_event.c"
)

for FILE in "${FILES[@]}"; do
    echo "Compiling $FILE for Coverity..."
    clang -target bpf -O2 \
          -I"$SCRIPT_DIR" \
          -I"$PROJECT_ROOT" \
          "${BCC_MACROS[@]}" \
          -Wno-implicit-function-declaration \
          -Wno-int-conversion \
          -Wno-return-type \
          -c "$FILE" -o /dev/null
done

if [ -f "$VMLINUX_H" ]; then
    rm -v "$VMLINUX_H"
fi
