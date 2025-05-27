from bcc import BPF

bpf_code = """
int kprobe__sys_execve(void *ctx) {
    char comm[16];
    bpf_get_current_comm(&comm, sizeof(comm));
    bpf_trace_printk("EXEC: %s\\n", comm);
    return 0;
}
"""

bpf = BPF(text=bpf_code)
print("Monitoring execve()... Ctrl+C to exit")
bpf.trace_print()
