# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Process discovery helpers used by the "Add Application" wizard.

The wizard asks the user for a few keywords (app name fragments) while the
target application is running.  We then scan /proc to find the matching
processes, present them to the user, and once they pick the right ones we
extract the fields that would otherwise have to be filled in by hand
(bpf_name / process_names / commandline / id).

This module is BPF-agnostic and DB-agnostic on purpose so it can be unit
tested with a normal ``python -m balancer.monitor.app_discovery``.
"""

import os
from dataclasses import dataclass, asdict
from typing import Iterable, Optional

from config.config import b_config


# Shells / tiny tools — would never be the "app" the user wants to monitor.
_SHELL_TOOLS = frozenset({
    "bash", "sh", "dash", "zsh", "fish", "tcsh",
    "sudo", "su", "env", "which", "getent",
    "cat", "head", "tail", "ls", "find", "xargs",
    "grep", "sed", "awk", "cut", "sort", "uniq", "wc", "tr", "tee",
    "ps", "top", "pgrep", "pkill",
    "true", "false", "sleep", "kill",
    "mv", "cp", "rm", "ln", "mkdir", "rmdir", "touch", "chmod", "chown",
})


@dataclass
class Candidate:
    """A single /proc entry that matched the user's keywords."""
    pid: int
    comm: str            # /proc/<pid>/comm — same 15-byte truncation BPF reports
    exe: str             # readlink /proc/<pid>/exe (full path, may be empty)
    cmdline: str         # nul-joined cmdline, rendered with spaces
    cgroup_unit: str     # systemd unit/scope from /proc/<pid>/cgroup, or ""
    ppid: int
    score: int           # higher = more likely to be the user-launched main process


@dataclass
class ExtractResult:
    bpf_name: list[str]
    process_names: list[str]
    commandline: list[str]
    id_suggestion: str   # systemd unit if all PIDs share one, else ""


def _read_text(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return ""


def _read_proc(pid: int) -> Optional[dict]:
    """Snapshot the bits of /proc/<pid> we care about. None if the pid vanished."""
    base = f"/proc/{pid}"
    comm = _read_text(f"{base}/comm").strip()
    if not comm:
        return None

    cmdline_raw = _read_text(f"{base}/cmdline")
    # /proc cmdline is nul-separated; argv[0] is everything up to the first nul.
    cmdline_argv0 = cmdline_raw.split("\x00", 1)[0] if cmdline_raw else ""
    cmdline_pretty = cmdline_raw.replace("\x00", " ").strip()

    try:
        exe = os.readlink(f"{base}/exe")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        exe = ""

    # Parse PPID from /proc/<pid>/status (cheap, robust against comm-with-spaces
    # in /proc/<pid>/stat).
    ppid = 0
    status = _read_text(f"{base}/status")
    for line in status.splitlines():
        if line.startswith("PPid:"):
            try:
                ppid = int(line.split()[1])
            except (IndexError, ValueError):
                ppid = 0
            break

    cgroup_unit = _parse_systemd_unit(_read_text(f"{base}/cgroup"))

    return {
        "pid": pid,
        "comm": comm,
        "exe": exe,
        "cmdline_argv0": cmdline_argv0,
        "cmdline_pretty": cmdline_pretty,
        "ppid": ppid,
        "cgroup_unit": cgroup_unit,
    }


def _parse_systemd_unit(cgroup_text: str) -> str:
    """Extract the systemd unit/scope name from the cgroup file contents.

    cgroup v2 line looks like:
        0::/system.slice/hs_agent.service
        0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.gnome.Calculator-...scope
    We return the basename ending in .service / .scope / .slice if present.
    """
    if not cgroup_text:
        return ""
    for line in cgroup_text.splitlines():
        # v2 lines start with "0::"; v1 lines may have "name=systemd:..." too.
        path = line.split(":", 2)[-1].strip()
        if not path:
            continue
        # Walk segments from the rightmost looking for a .service/.scope/.slice
        for segment in reversed(path.split("/")):
            if segment.endswith((".service", ".scope", ".slice")):
                return segment
    return ""


def _is_blacklisted(comm: str, exe_basename: str) -> bool:
    comm_lower = comm.lower()
    exe_lower = exe_basename.lower()
    # Shell-tool filter only checks comm: when a script is launched via a
    # shebang the kernel renames comm to the script's basename even though
    # /proc/<pid>/exe still points at /bin/bash.  Filtering on exe would
    # silently drop user-written launcher scripts that the wizard is
    # specifically meant to surface.
    if comm_lower in _SHELL_TOOLS:
        return True
    # Service / daemon noise checks both fields because some daemons spawn
    # short-lived helpers whose comm is benign but whose exe is the daemon.
    for bad in (b_config.blacklist or ()):
        b = bad.lower().strip()
        if not b:
            continue
        if b in comm_lower or b in exe_lower:
            return True
    return False


def _matches_any_keyword(info: dict, keywords_lower: list[str]) -> bool:
    haystacks = (
        info["comm"].lower(),
        os.path.basename(info["exe"]).lower(),
        info["cmdline_pretty"].lower(),
    )
    return any(any(kw in h for h in haystacks) for kw in keywords_lower)


def _score(info: dict) -> int:
    """Heuristic: prefer user-session processes and longer-lived ones."""
    score = 0
    unit = info["cgroup_unit"]
    if unit.endswith(".scope") and "user-" in unit:
        score += 50
    elif unit.endswith(".service") and "user@" in unit:
        score += 40
    elif unit.endswith(".service"):
        score += 20  # system service — still valid (helicon search pattern)
    if info["exe"]:
        score += 10
    if info["ppid"] in (1,):
        # PID 1 child = systemd-managed, often the main service process
        score += 5
    return score


def search_processes(
    keywords: Iterable[str],
    *,
    max_results: int = 100,
) -> list[Candidate]:
    """Return /proc entries matching any of ``keywords`` (case-insensitive substring).

    Filtering uses the unified ``b_config.blacklist`` list (see config.yaml).
    """
    keywords_lower = [k.strip().lower() for k in keywords if k and k.strip()]
    if not keywords_lower:
        return []

    results: list[Candidate] = []
    try:
        pid_dirs = os.listdir("/proc")
    except OSError:
        return []

    for entry in pid_dirs:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid < 100:
            # PID < 100 are kernel threads / very early init processes; never
            # the kind of thing a user would want to monitor.
            continue

        info = _read_proc(pid)
        if info is None:
            continue

        comm = info["comm"]
        exe_basename = os.path.basename(info["exe"]) if info["exe"] else ""

        if _is_blacklisted(comm, exe_basename):
            continue

        if not _matches_any_keyword(info, keywords_lower):
            continue

        results.append(Candidate(
            pid=pid,
            comm=comm,
            exe=info["exe"],
            cmdline=info["cmdline_pretty"],
            cgroup_unit=info["cgroup_unit"],
            ppid=info["ppid"],
            score=_score(info),
        ))

    results.sort(key=lambda c: (-c.score, c.pid))
    return results[:max_results]


def _slugify(text: str) -> str:
    """Lower-case, replace runs of non-[a-z0-9] with '-', strip leading/trailing '-'.

    Used to derive a default ``id`` from the display ``name`` when no
    systemd unit is shared by the selected PIDs.  Empty input → empty out.
    """
    out = []
    prev_dash = False
    for ch in (text or "").lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
            prev_dash = False
        elif out:
            if not prev_dash:
                out.append("-")
                prev_dash = True
    return "".join(out).strip("-")


def extract_fields(pids: Iterable[int], name: str = "") -> ExtractResult:
    """Aggregate /proc fields across the user-selected PIDs.

    Output is what the wizard would otherwise force the user to type:
    bpf_name (from comm), process_names (from exe basename), commandline
    (from cmdline argv[0]), and id_suggestion.

    ``id_suggestion`` is derived in this order:
      1. If all selected PIDs share a single systemd unit (other than
         ``.slice``), use that unit name verbatim — this matches the
         convention used by existing config entries like ``hs_agent.service``.
      2. Otherwise, if ``name`` was provided, fall back to ``slugify(name)
         + ".id"`` so the user gets a sensible default rather than an empty
         input field.  The user can still edit the value before committing.
      3. If ``name`` is empty too, return ``""``.
    """
    bpf_name: list[str] = []
    process_names: list[str] = []
    commandline: list[str] = []
    units: set[str] = set()

    seen_comm, seen_proc, seen_cmd = set(), set(), set()

    for pid in pids:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            continue
        info = _read_proc(pid_int)
        if info is None:
            continue

        comm = info["comm"].strip()
        if comm and comm not in seen_comm:
            bpf_name.append(comm)
            seen_comm.add(comm)

        exe_base = os.path.basename(info["exe"]) if info["exe"] else ""
        # When the exe is a shell, the *real* program identity is in comm
        # (the kernel renames comm to the script name on shebang launch).
        # Using exe_base would write "bash" or "sh" into process_names, which
        # later causes get_app_processes() to pgrep-match every shell on the
        # system and break per-app resource aggregation and OOM scoring.
        if exe_base.lower() in _SHELL_TOOLS:
            exe_base = comm
        if exe_base and exe_base not in seen_proc:
            process_names.append(exe_base)
            seen_proc.add(exe_base)

        argv0 = info["cmdline_argv0"].strip()
        if argv0 and argv0 not in seen_cmd:
            commandline.append(argv0)
            seen_cmd.add(argv0)

        if info["cgroup_unit"]:
            units.add(info["cgroup_unit"])

    id_suggestion = ""
    if len(units) == 1:
        only = next(iter(units))
        # .slice is a generic grouping — not a useful per-app id.
        if not only.endswith(".slice"):
            id_suggestion = only
    if not id_suggestion and name:
        slug = _slugify(name)
        if slug:
            id_suggestion = f"{slug}.id"

    return ExtractResult(
        bpf_name=sorted(bpf_name),
        process_names=sorted(process_names),
        commandline=sorted(commandline),
        id_suggestion=id_suggestion,
    )


def candidate_to_dict(c: Candidate) -> dict:
    return asdict(c)


def extract_to_dict(r: ExtractResult) -> dict:
    return asdict(r)


if __name__ == "__main__":
    # Smoke-test from the command line:
    #   python -m balancer.monitor.app_discovery helicon vlm
    import json
    import sys
    kws = sys.argv[1:] or ["helicon"]
    cands = search_processes(kws)
    print(f"Found {len(cands)} candidates for keywords {kws}:")
    for c in cands:
        print(json.dumps(candidate_to_dict(c), ensure_ascii=False))
    if cands:
        result = extract_fields([c.pid for c in cands], name=" ".join(kws))
        print("\nExtracted fields:")
        print(json.dumps(extract_to_dict(result), ensure_ascii=False, indent=2))
