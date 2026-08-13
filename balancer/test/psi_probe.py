#!/usr/bin/env python3
# Read-only PSI probe: samples system + workload-cgroup pressure, utilization and the
# resource limits smartune has applied. Does NOT modify anything. Safe to run while
# smartune is active.
#
#   usage: python3 psi_probe.py [samples=60] [interval_sec=2]
#                               [--comm stress,fio,dd] [--scope hi-io.scope,lo-io.scope]
#                               [--config path/to/config.yaml]
#
# Which cgroups are shown:
#   * every process whose comm matches --comm (default: stress, stress-ng, fio, dd), and
#   * every cgroup whose leaf name matches a --scope unit (any process inside it), so a
#     workload launched via `systemd-run --scope --unit=NAME` is tracked regardless of
#     its comm, and
#   * the scopes/services listed as controlled_apps[].id in --config (if given), so you
#     can watch exactly the apps smartune is set up to throttle.
import argparse
import os
import sys
import time

CG = "/sys/fs/cgroup"
_DEFAULT_COMMS = ("stress", "stress-ng", "fio", "dd")


def read_psi(path, is_proc=False):
    """Return {'cpu':(some,full),'io':..,'memory':..} avg10 percentages, or None."""
    out = {}
    for res in ("cpu", "io", "memory"):
        fp = os.path.join(path, res) if is_proc else os.path.join(path, f"{res}.pressure")
        try:
            some = full = 0.0
            with open(fp) as f:
                for line in f:
                    v = 0.0
                    for tok in line.split():
                        if tok.startswith("avg10="):
                            v = float(tok.split("=")[1])
                    if line.startswith("some"):
                        some = v
                    elif line.startswith("full"):
                        full = v
            out[res] = (some, full)
        except Exception:
            out[res] = (None, None)
    return out


def _proc_comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except Exception:
        return None


def _proc_cgroup(pid):
    """Relative cgroup v2 path of a pid ("0::/path"), or None."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            return f.read().strip().split("::")[-1]
    except Exception:
        return None


def workload_cgroups(comms, scopes):
    """Resolve unique cgroup paths to watch (cgroup v2).

    A cgroup is included when either the process comm is in ``comms`` OR the cgroup's
    leaf name is in ``scopes`` (so systemd-run --unit=NAME scopes are tracked whatever
    the workload binary is).
    """
    cgs = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        rel = _proc_cgroup(pid)
        if not rel:
            continue
        leaf = os.path.basename(rel.rstrip("/"))
        if leaf in scopes:
            cgs.add(rel)
            continue
        if comms and _proc_comm(pid) in comms:
            cgs.add(rel)
    return sorted(cgs)


def read_limits(full_path):
    """Read current limit params to detect whether smartune has throttled this cgroup."""
    def rd(name):
        try:
            with open(os.path.join(full_path, name)) as f:
                return f.read().strip()
        except Exception:
            return "-"
    io_max = rd("io.max")
    return (f"cpu.max={rd('cpu.max')} mem.high={rd('memory.high')} "
            f"io.max={io_max[:60] if io_max not in ('', '-') else '(none)'}")


def read_io_bytes(full_path):
    """Sum cumulative (rbytes, wbytes) across all devices from a cgroup's io.stat, or None."""
    try:
        r = w = 0
        with open(os.path.join(full_path, "io.stat")) as f:
            for line in f:
                for tok in line.split():
                    if tok.startswith("rbytes="):
                        r += int(tok.split("=")[1])
                    elif tok.startswith("wbytes="):
                        w += int(tok.split("=")[1])
        return r, w
    except Exception:
        return None


def fmt(psi):
    def p(x):
        return "  -  " if x is None else f"{x:5.1f}"
    return (f"cpu[s{p(psi['cpu'][0])} f{p(psi['cpu'][1])}] "
            f"io[s{p(psi['io'][0])} f{p(psi['io'][1])}] "
            f"mem[s{p(psi['memory'][0])} f{p(psi['memory'][1])}]")


def _scopes_from_config(path):
    """controlled_apps[].id entries that look like systemd units (.scope/.service)."""
    try:
        import yaml
    except Exception:
        print(f"# --config ignored: PyYAML not installed", file=sys.stderr)
        return set()
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"# --config ignored: cannot read {path}: {e}", file=sys.stderr)
        return set()
    out = set()
    for app in cfg.get("controlled_apps") or []:
        app_id = (app or {}).get("id")
        if isinstance(app_id, str) and app_id.endswith((".scope", ".service")):
            out.add(app_id)
    return out


def _parse_args(argv):
    ap = argparse.ArgumentParser(add_help=True, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("samples", nargs="?", type=int, default=60)
    ap.add_argument("interval", nargs="?", type=float, default=2.0)
    ap.add_argument("--comm", default=",".join(_DEFAULT_COMMS),
                    help="comma-separated process comm names to watch "
                         "(default: %(default)s; pass '' to disable comm matching)")
    ap.add_argument("--scope", default="",
                    help="comma-separated systemd unit leaf names to watch "
                         "(e.g. hi-io.scope,lo-io.scope), matched by cgroup name")
    ap.add_argument("--config", default="",
                    help="config.yaml path; adds controlled_apps[].id scopes/services")
    return ap.parse_args(argv)


def main():
    import psutil
    args = _parse_args(sys.argv[1:])
    comms = {c.strip() for c in args.comm.split(",") if c.strip()}
    scopes = {s.strip() for s in args.scope.split(",") if s.strip()}
    if args.config:
        scopes |= _scopes_from_config(args.config)

    psutil.cpu_percent(interval=None)  # prime
    print(f"# watching comm={sorted(comms) or '-'} scope={sorted(scopes) or '-'}", flush=True)
    print("# ts        cpu% mem% | SYSTEM some/full avg10 | CGROUP some/full avg10 | limits", flush=True)
    print("#   per-cg extra line: actual rd/wr MB/s (from io.stat delta) + io.max cap", flush=True)
    prev_io = {}  # {cgroup_rel: (rbytes, wbytes)} from the previous sample, for rate calc
    for _ in range(args.samples):
        t = time.strftime("%H:%M:%S")
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        sysp = read_psi("/proc/pressure", is_proc=True)
        line = f"{t} {cpu:5.1f} {mem:5.1f} | SYS {fmt(sysp)}"
        cgs = workload_cgroups(comms, scopes)
        if cgs:
            for rel in cgs:
                fp = os.path.join(CG, rel.lstrip("/"))
                cgp = read_psi(fp)
                lim = read_limits(fp)
                # Actual achieved throughput: delta of io.stat bytes over the interval.
                cur = read_io_bytes(fp)
                if cur is not None and rel in prev_io and args.interval > 0:
                    dr = max(0, cur[0] - prev_io[rel][0]) / (1024.0 * 1024.0) / args.interval
                    dw = max(0, cur[1] - prev_io[rel][1]) / (1024.0 * 1024.0) / args.interval
                    io_rate = f"rd={dr:6.1f} wr={dw:6.1f} MB/s"
                else:
                    io_rate = "rd=   -   wr=   -   MB/s"
                if cur is not None:
                    prev_io[rel] = cur
                line += (f"\n         (cg {rel})"
                         f"\n           CG  {fmt(cgp)} | {lim}"
                         f"\n           IO  {io_rate}")
        else:
            line += "  | (no matching workload)"
        print(line, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
