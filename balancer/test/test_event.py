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

bpf_code = """
#include <uapi/linux/ptrace.h>
#include <linux/sched.h>

struct data_t {
    u32 pid;
    char comm[16];
    char filename[256];
};

BPF_PERF_OUTPUT(events);

int kprobe__sys_execve(struct pt_regs *ctx) {
    struct data_t data = {};
    u64 pid_tgid = bpf_get_current_pid_tgid();
    data.pid = pid_tgid >> 32;
    bpf_get_current_comm(&data.comm, sizeof(data.comm));

    const char *filename = (const char *)PT_REGS_PARM1(ctx);
    if (filename) {
        int ret = bpf_probe_read_user_str(&data.filename, sizeof(data.filename), filename);
        if (ret > 0) {
            bpf_trace_printk("Execve: PID=%d, FILE=%s\\n", data.pid, data.filename);
        }
    }

    events.perf_submit(ctx, &data, sizeof(data));
    return 0;
}
"""

b = BPF(text=bpf_code)


def print_event(cpu, data, size):
    event = b["events"].event(data)
    filename = event.filename.decode('utf-8', 'ignore')
    comm = event.comm.decode('utf-8', 'ignore')

    # 打印所有事件进行调试
    print(f"DEBUG: PID={event.pid}, COMM={comm}, FILENAME={filename}")

    # 保留原始过滤条件
    if "firefox" in comm.lower() or "firefox" in filename.lower():
        print(f"Firefox launched! PID: {event.pid}, Path: {filename}")


b["events"].open_perf_buffer(print_event)
print("Listening for execve events... (Ctrl+C to exit)")

while True:
    try:
        b.perf_buffer_poll(timeout=100)
    except KeyboardInterrupt:
        break
    except Exception as e:
        print(f"Error: {e}")
        break
