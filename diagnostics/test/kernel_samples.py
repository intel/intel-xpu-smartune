# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Kernel log samples in the printk formats the kernel emits, and a helper
that turns them into journal records the way the journal source does."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics.sources.base import LogRecord  # noqa: E402

T0 = 1_900_000_000.0


def kernel_records(text, *, start=100.0, step=0.001, boot="boot-1", levels=None, fields=None):
    """One kernel LogRecord per line of ``text``, ``step`` seconds apart on the
    monotonic clock. ``levels`` maps a line index to its level (default INFO)."""
    records = []
    for index, line in enumerate(text.split("\n")):
        mono = start + index * step
        records.append(LogRecord(
            ts_epoch=T0 + mono, ts_iso="", level=(levels or {}).get(index, "INFO"),
            source="journal", logger="kernel", message=line,
            fields={"transport": "kernel", "boot_id": boot, "monotonic_s": mono,
                    "cursor": f"{boot}:{mono:.3f}", **(fields or {})}))
    return records


def user_record(message, mono, boot="boot-1", **fields):
    """A non-kernel journal record (a systemd unit's line)."""
    return LogRecord(
        ts_epoch=T0 + mono, ts_iso="", level="INFO", source="journal", logger="systemd",
        message=message, fields={"transport": "journal", "boot_id": boot, **fields})


HUNG_TASK_GPU = """INFO: task python3:4321 blocked for more than 122 seconds.
      Tainted: G     U     OE      6.8.0-45-generic #45-Ubuntu
"echo 0 > /proc/sys/kernel/hung_task_timeout_secs" disables this message.
task:python3         state:D stack:0     pid:4321  tgid:4321  ppid:1     flags:0x00004002
Call Trace:
 <TASK>
 __schedule+0x27c/0x6b0
 schedule+0x33/0x110
 schedule_timeout+0x157/0x170
 dma_fence_default_wait+0x1e5/0x250
 dma_fence_wait_timeout+0x116/0x140
 xe_vm_bind_ioctl+0x8a1/0xd20 [xe]
 drm_ioctl+0x2a3/0x4f0
 __x64_sys_ioctl+0x97/0xd0
 do_syscall_64+0x7e/0x170
 entry_SYSCALL_64_after_hwframe+0x76/0x7e
 </TASK>"""

HUNG_TASK_IO = """INFO: task jbd2/nvme0n1p2-8:512 blocked for more than 120 seconds.
      Not tainted 6.8.0-45-generic #45-Ubuntu
"echo 0 > /proc/sys/kernel/hung_task_timeout_secs" disables this message.
task:jbd2/nvme0n1p2- state:D stack:0     pid:512   tgid:512   ppid:2      flags:0x00004000
Call Trace:
 <TASK>
 __schedule+0x27c/0x6b0
 schedule+0x33/0x110
 io_schedule+0x46/0x80
 folio_wait_bit_common+0x13d/0x350
 </TASK>"""

WARN_XE = """------------[ cut here ]------------
WARNING: CPU: 3 PID: 1234 at drivers/gpu/drm/xe/xe_gt.c:123 xe_gt_reset_async+0x1a/0x40 [xe]
Modules linked in: xe drm_gpuvm drm_exec gpu_sched drm_buddy
CPU: 3 PID: 1234 Comm: kworker/3:1 Tainted: G        W          6.8.0-45-generic #45-Ubuntu
Hardware name: Intel Corporation Client Platform/Board, BIOS 1.0 01/01/2026
Workqueue: events xe_gt_reset_worker [xe]
RIP: 0010:xe_gt_reset_async+0x1a/0x40 [xe]
Code: 0f 1f 44 00 00 55 48 89 e5 41 54 53 48 89 fb
RSP: 0018:ffffb2a1c0e7fd60 EFLAGS: 00010246
Call Trace:
 <TASK>
 process_one_work+0x178/0x350
 </TASK>
---[ end trace 0000000000000000 ]---"""

# drm_WARN: the driver's own message sits between the marker and the WARNING line.
DRM_WARN = """------------[ cut here ]------------
xe 0000:03:00.0: [drm] Assertion `vma->gpuva.flags & XE_VMA_DESTROYED` failed!
WARNING: CPU: 1 PID: 777 at drivers/gpu/drm/xe/xe_vm.c:1043 xe_vma_destroy+0x2f/0x60 [xe]
CPU: 1 PID: 777 Comm: python3 Tainted: G     U             6.8.0-45-generic #45-Ubuntu
---[ end trace 0000000000000000 ]---"""

NULL_DEREF_OOPS = """BUG: kernel NULL pointer dereference, address: 0000000000000008
#PF: supervisor read access in kernel mode
#PF: error_code(0x0000) - not-present page
PGD 0 P4D 0
Oops: 0000 [#1] PREEMPT SMP NOPTI
CPU: 2 PID: 4321 Comm: python3 Tainted: G     U     OE      6.8.0-45-generic #45-Ubuntu
Hardware name: Intel Corporation Client Platform/Board, BIOS 1.0 01/01/2026
RIP: 0010:xe_bo_move+0x2c/0x5f0 [xe]
Code: 0f 1f 44 00 00 55 48 89 e5 41 57 41 56 41 55
RSP: 0018:ffffb2a1c0e7fb70 EFLAGS: 00010282
Call Trace:
 <TASK>
 ttm_bo_handle_move_mem+0xc8/0x180 [ttm]
 </TASK>
CR2: 0000000000000008
---[ end trace 0000000000000000 ]---"""

KERNEL_BUG_AT = """kernel BUG at mm/slub.c:432!
invalid opcode: 0000 [#1] PREEMPT SMP NOPTI
CPU: 0 PID: 88 Comm: kswapd0 Not tainted 6.8.0-45-generic #45-Ubuntu
RIP: 0010:kfree+0x1d2/0x2e0
---[ end trace 0000000000000000 ]---"""

SLEEPING_IN_ATOMIC = """BUG: sleeping function called from invalid context at kernel/locking/mutex.c:580
in_atomic(): 1, irqs_disabled(): 0, non_block: 0, pid: 2468, name: kworker/2:0
CPU: 2 PID: 2468 Comm: kworker/2:0 Tainted: G        W          6.8.0-45-generic #45-Ubuntu
Call Trace:
 <TASK>
 dump_stack_lvl+0x48/0x70
 </TASK>"""

SOFT_LOCKUP = """watchdog: BUG: soft lockup - CPU#5 stuck for 26s! [kworker/5:2:789]
Modules linked in: xe drm_exec
CPU: 5 PID: 789 Comm: kworker/5:2 Tainted: G             L     6.8.0-45-generic #45-Ubuntu
RIP: 0010:xe_guc_ct_send_locked+0x91/0x2a0 [xe]"""

HARD_LOCKUP = """watchdog: Watchdog detected hard LOCKUP on cpu 7
Modules linked in: xe
CPU: 7 PID: 0 Comm: swapper/7 Not tainted 6.8.0-45-generic #45-Ubuntu
RIP: 0010:native_queued_spin_lock_slowpath+0x6e/0x2c0"""

RCU_STALL = """rcu: INFO: rcu_preempt self-detected stall on CPU
rcu: \t4-....: (5249 ticks this GP) idle=e4c4/1/0x4000000000000000 softirq=1/1 fqs=2350"""

WQ_LOCKUP = """BUG: workqueue lockup - pool cpus=3 node=0 flags=0x0 nice=0 stuck for 45s!
Showing busy workqueues and worker pools:"""

SEGFAULT = """python3[4242]: segfault at 0 ip 00007f3a2b1c4d5e sp 00007ffd1e2f3a40 error 4 in libfoo.so.1[7f3a2b1a0000+30000] likely on CPU 3 (core 3, socket 0)
Code: 48 8b 07 c3 0f 1f 80 00 00 00 00 f3 0f 1e fa"""

TRAP_GPF = ("traps: ollama[1234] general protection fault ip:7f00c0ffee00 sp:7ffd00000000"
            " error:0 in libggml.so[7f00c0f00000+100000]")

TRAP_INVALID_OPCODE = "traps: app[99] trap invalid opcode ip:55d0a0001234 sp:7ffe00001000 error:0 in app[55d0a0000000+2000]"

# MCi_STATUS 0x8c00004000010090: VAL set, UC and PCC clear -> corrected.
MCE_CORRECTED = """mce: [Hardware Error]: Machine check events logged
mce: [Hardware Error]: CPU 0: Machine Check: 0 Bank 5: 8c00004000010090
mce: [Hardware Error]: TSC 0 ADDR 3f8c2a000 MISC 7ff0000086
mce: [Hardware Error]: PROCESSOR 0:906ea TIME 1900000000 SOCKET 0 APIC 0 microcode f0"""

# A fatal error read back from the banks at boot is logged with the same words
# as a corrected one; MCi_STATUS 0xbf80000000000124 has UC and PCC set.
MCE_REPLAYED_FATAL = """mce: [Hardware Error]: Machine check events logged
mce: [Hardware Error]: CPU 2: Machine Check: 0 Bank 1: bf80000000000124
mce: [Hardware Error]: TSC 0 ADDR 1ffffc0000 MISC 86"""

AER_CORRECTED = """pcieport 0000:00:1c.0: AER: Corrected error message received from 0000:01:00.0
nvme 0000:01:00.0: PCIe Bus Error: severity=Correctable, type=Physical Layer, (Receiver ID)
nvme 0000:01:00.0:   device [8086:f1a8] error status/mask=00000001/00002000
nvme 0000:01:00.0:    [ 0] RxErr                  (First)"""

AER_UNCORRECTED = """pcieport 0000:00:01.0: AER: Uncorrected (Fatal) error message received from 0000:02:00.0
xe 0000:02:00.0: PCIe Bus Error: severity=Uncorrected (Fatal), type=Transaction Layer, (Requester ID)
xe 0000:02:00.0:   device [8086:e20b] error status/mask=00004000/00000000
xe 0000:02:00.0:    [14] CmpltTO                (First)"""

OOM_KILL = """python3 invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE|__GFP_COMP), order=0, oom_score_adj=0
CPU: 6 PID: 5150 Comm: python3 Tainted: G     U     OE      6.8.0-45-generic #45-Ubuntu
Call Trace:
 <TASK>
 dump_stack_lvl+0x48/0x70
 </TASK>
Mem-Info:
active_anon:3912345 inactive_anon:12345 isolated_anon:0
[  pid  ]   uid  tgid total_vm      rss pgtables_bytes swapents oom_score_adj name
[   5150]  1000  5150 16912345  3912345 31457280        0             0 python3
oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,mems_allowed=0,global_oom,task_memcg=/user.slice,task=python3,pid=5150,uid=1000
Out of memory: Killed process 5150 (python3) total-vm:67649380kB, anon-rss:15649380kB, file-rss:0kB, shmem-rss:0kB, UID:1000 pgtables:30720kB oom_score_adj:0"""
