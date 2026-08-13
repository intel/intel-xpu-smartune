#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Read-only comparison of the three ways to resolve "which PIDs are this app".

Writes nothing, changes nothing, starts nothing. Safe to run while smartune is active.
It answers one question before any code is changed: for the app names actually in
config.yaml, on this machine, right now -- which strategy over-matches, and which misses?

    fuzzy    what production uses today (utils/app_utils.get_app_processes):
             `pgrep -fi NAME`, i.e. a substring of the WHOLE command line. Finds the
             app, and also finds every launcher that merely mentions it and every
             process whose name has it as a prefix.
    exact    NAME must equal argv[0]'s basename, comm, or the exe basename. Kills the
             over-match, but cannot see an app whose configured name is a script
             (`python3 server.py` is configured as "server.py" while all three fields
             read "python3").
    derived  run monitor/app_discovery._derive_process_name over each process and compare
             exactly. That is the SAME rule the "Add Application" wizard used to write
             process_names into config.yaml, so matching is symmetric with writing --
             interpreter/shell wrappers resolve to the script on both sides.
    union    exact OR derived -- what utils/app_utils.get_app_processes_by_exact_name now
             does, and the column to read. Neither half matches on a command-line
             substring, so their union still cannot over-match; it is only harder to miss
             than either half. An empty union means production limits nothing and logs
             what fuzzy would have picked up instead.

Why the cgroup column matters: a resource limit is a property of a cgroup, not of a
process. Every distinct cgroup a strategy reaches is a cgroup that would get io.max
written into it -- including cgroups belonging to somebody else.

  usage: python3 balancer/test/app_match_probe.py [--config config/config.yaml]
                                                  [--name NAME]... [--all-procs] [-v]

  --name       probe an extra name that is not in controlled_apps (e.g. "fio_lo2",
               the deliberately unmanaged workload). Repeatable.
  --all-procs  also list, for every strategy, the processes it matched that the
               others did not -- the raw disagreement, unsummarised.
  -v           print the full command line instead of a truncated one.
"""

import argparse
import os
import re
import shlex
import subprocess  # nosec - fixed argv, no shell
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# The one import that must come from production code: re-implementing the derivation
# here would make the probe measure the probe, not the proposal.
from monitor.app_discovery import _derive_process_name, _is_interpreter, _shell_tools  # noqa: E402


# --------------------------------------------------------------------------- snapshot

def _read(path, binary=False):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data if binary else data.decode("utf-8", "replace")
    except OSError:
        return b"" if binary else ""


def _cgroup_of(pid):
    """Leaf of the cgroup v2 path, e.g. "lo-io.scope". Empty when unreadable."""
    for line in _read(f"/proc/{pid}/cgroup").splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            return parts[2].strip()
    return ""


def snapshot():
    """One pass over /proc. All three strategies score the same table, so a process
    that exits mid-run cannot show up as a disagreement between them."""
    procs = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        raw = _read(f"/proc/{pid}/cmdline", binary=True)
        tokens = [t.decode("utf-8", "replace") for t in raw.split(b"\0") if t]
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            exe = ""  # other user's process, or a kernel thread
        comm = _read(f"/proc/{pid}/comm").strip()
        if not comm and not tokens:
            continue
        procs[pid] = {
            "pid": pid,
            "comm": comm,
            "exe": exe,
            "cmdline_argv0": tokens[0] if tokens else "",
            "cmdline_tokens": tokens,
            "cmdline": " ".join(tokens) or f"[{comm}]",
            "cgroup": _cgroup_of(pid),
        }
    return procs


# ------------------------------------------------------------------------ strategies

def match_fuzzy(name, procs):
    """utils/app_utils.get_app_processes, faithfully.

    The single-token path shells out to the real pgrep rather than imitating its regex,
    so the numbers below are what production would actually get.
    """
    query = (name or "").strip()
    if not query:
        return set()
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()

    if len(tokens) > 1:
        target = os.path.basename(tokens[0]).lower()
        required = [t.lower() for t in tokens[1:] if t.strip()]
        hits = set()
        for pid, p in procs.items():
            if not p["cmdline_tokens"]:
                continue
            names = {
                os.path.basename(p["cmdline_argv0"]).lower(),
                p["comm"].lower(),
                os.path.basename(p["exe"]).lower(),
            }
            if target not in names:
                continue
            low = [t.strip().lower() for t in p["cmdline_tokens"] if t.strip()]
            if all(tok in low for tok in required):
                hits.add(pid)
        return hits

    try:
        res = subprocess.run(["pgrep", "-fi", query], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)  # nosec
        if res.returncode == 0:
            return {int(p) for p in res.stdout.split() if p.strip().isdigit()} & set(procs)
    except OSError:
        pass
    return set()


def match_exact(name, procs):
    """utils/app_utils.get_app_processes_by_exact_name, faithfully."""
    q = (name or "").strip().lower()
    if not q:
        return set()
    hits = set()
    for pid, p in procs.items():
        names = {
            os.path.basename(p["cmdline_argv0"]).lower(),
            p["comm"].lower(),
            os.path.basename(p["exe"]).lower(),
        }
        if q in names:
            hits.add(pid)
    return hits


def match_derived(name, procs):
    """The proposal: compare against the wizard's own derivation of each process."""
    q = (name or "").strip().lower()
    if not q:
        return set()
    return {pid for pid, p in procs.items()
            if _derive_process_name(p).strip().lower() == q}


def match_union(name, procs):
    """What production resolves a limit against today."""
    return match_exact(name, procs) | match_derived(name, procs)


STRATEGIES = (("fuzzy", match_fuzzy), ("exact", match_exact),
              ("derived", match_derived), ("union", match_union))


# ----------------------------------------------------------------------- config input

def load_names(config_path):
    """[(app_label, process_name)] from controlled_apps, without importing the app.

    Hand-rolled instead of PyYAML: the probe must run even where the service's
    dependencies are not installed, and it only needs two keys.
    """
    out = []
    if not config_path or not os.path.exists(config_path):
        return out
    in_apps = False
    label = ""
    for line in _read(config_path).splitlines():
        if re.match(r"^controlled_apps:", line):
            in_apps = True
            continue
        if in_apps and line and not line[0].isspace():
            break  # next top-level key
        if not in_apps:
            continue
        stripped = line.strip()
        m = re.match(r"^-?\s*(name|id):\s*[\"']?([^\"'#]+)", stripped)
        if m and m.group(1) == "name":
            label = m.group(2).strip()
            continue
        m = re.match(r"^process_names:\s*\[(.*)\]", stripped)
        if m:
            for tok in m.group(1).split(","):
                tok = tok.strip().strip("\"'")
                if tok:
                    out.append((label, tok))
    return out


# --------------------------------------------------------------------------- printing

def cgset(pids, procs):
    return sorted({procs[p]["cgroup"] for p in pids if procs[p]["cgroup"]})


def report(label, name, procs, verbose, all_procs):
    hits = {key: fn(name, procs) for key, fn in STRATEGIES}
    union = sorted(set().union(*hits.values()))

    header = f"process_name {name!r}" + (f"   (app: {label})" if label else "")
    print(f"\n{header}")
    print("-" * len(header))

    if not union:
        print("  no process matched by any strategy -- app not running?")
        return {"name": name, "hits": hits, "notes": ["not running"]}

    cols = " ".join(k[0].upper() for k, _ in STRATEGIES)
    print(f"  {'pid':>7}  {cols}  {'comm':<16} {'derived':<20} cgroup")
    for pid in union:
        p = procs[pid]
        flags = " ".join("x" if pid in hits[k] else "." for k, _ in STRATEGIES)
        cg = p["cgroup"] or "?"
        if not verbose and len(cg) > 46:
            cg = "..." + cg[-43:]
        print(f"  {pid:>7}  {flags}  "
              f"{p['comm'][:16]:<16} {_derive_process_name(p)[:20]:<20} {cg}")
        if verbose:
            print(f"           {p['cmdline']}")

    for key, _ in STRATEGIES:
        cgs = cgset(hits[key], procs)
        print(f"  {key:<8} {len(hits[key]):>2} pid(s), {len(cgs)} cgroup(s): "
              f"{', '.join(os.path.basename(c) for c in cgs) or '-'}")

    notes = []
    over = hits["fuzzy"] - hits["union"]
    if over:
        extra_cg = set(cgset(hits['fuzzy'], procs)) - set(cgset(hits['union'], procs))
        notes.append(f"fuzzy over-matches {len(over)} pid(s) {sorted(over)[:6]}"
                     + (f", dragging in {len(extra_cg)} extra cgroup(s)" if extra_cg else ""))
    only_derived = hits["derived"] - hits["exact"]
    if only_derived:
        notes.append(f"only derived finds {len(only_derived)} pid(s) {sorted(only_derived)[:6]}"
                     " -- wrapped/interpreted app, exact alone would have missed it")
    only_exact = hits["exact"] - hits["derived"]
    if only_exact:
        notes.append(f"only exact finds {len(only_exact)} pid(s) {sorted(only_exact)[:6]}"
                     " -- derived alone would have missed it")
    if not hits["union"]:
        notes.append("union is EMPTY -- production limits nothing here and logs the fuzzy hits;"
                     " if the app IS running, its process_names entry is wrong")
    low = name.strip().lower()
    if low in _shell_tools() or _is_interpreter(low):
        notes.append(f"{name!r} is a shell tool / interpreter: derived resolves such a process to"
                     " the script it runs, so it can never return this name -- this is the case"
                     " the exact half of the union covers. Only a hand-edited config gets here;"
                     " the wizard would never write it.")

    for n in notes:
        print(f"  ! {n}")
    if not notes:
        print("  = all three agree")

    if all_procs:
        for key, _ in STRATEGIES:
            others = set().union(*(v for k, v in hits.items() if k != key))
            uniq = hits[key] - others
            if uniq:
                print(f"  only {key}: " + "; ".join(
                    f"{p} {procs[p]['cmdline'][:70]}" for p in sorted(uniq)))

    return {"name": name, "hits": hits, "notes": notes}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(_REPO_ROOT, "config", "config.yaml"))
    ap.add_argument("--name", action="append", default=[],
                    help="extra process name to probe; repeatable")
    ap.add_argument("--all-procs", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    targets = load_names(args.config) + [("", n) for n in args.name]
    if not targets:
        print(f"no process_names found in {args.config} and no --name given", file=sys.stderr)
        return 2

    procs = snapshot()
    print(f"{len(procs)} processes sampled; "
          "F=fuzzy  E=exact-fields  D=derived  U=union (what production limits)")
    if os.geteuid() != 0:
        print("note: not root -- exe/cgroup of other users' processes are unreadable, "
              "which understates every strategy equally")

    results = [report(label, name, procs, args.verbose, args.all_procs)
               for label, name in targets]

    seen = [r for r in results if "not running" not in r["notes"]]
    resolved = [r for r in results if r["hits"]["union"]]
    print(f"\n=== summary: {len(resolved)}/{len(results)} name(s) resolved to a real process; "
          f"{len(seen) - len(resolved)} matched by fuzzy only ===")
    print(f"  {len([r for r in seen if not r['notes']])} name(s) where every strategy agrees")
    for r in seen:
        for n in r["notes"]:
            print(f"  {r['name']}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
