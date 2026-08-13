#!/bin/bash

# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# Three-priority disk-IO bandwidth test WITHOUT manual cgroup editing.
#
# Each workload runs in its own transient systemd scope (systemd-run --scope --unit=...),
# so smartune discovers it and applies io.max by priority on its own -- no hardcoded cgroup
# paths, no cgexec. This exercises the real product path (_handle_disk_io_stressed).
#
# DISTINCT binary names are used (fio_hi / fio_lo / fio_lo2) on purpose: smartune matches a
# top disk-IO consumer to a controlled app by process (comm) name, so same-named `fio`
# processes could not be told apart by priority.
#
# The three scopes cover every case the throttle path has to distinguish:
#     hi-io.scope   fio_hi   registered Critical    -> never throttled
#     lo-io.scope   fio_lo   registered Low         -> capped at the Low rates
#     lo2-io.scope  fio_lo2  NOT registered at all  -> capped at the 'undefined' rates,
#                                                      which are STRICTER than Low
# fio_lo2 is deliberately left unregistered -- do not add it in the wizard. An unmanaged
# heavy writer is a case the balancer must handle, and it also keeps a throttleable app in
# contention for the top-consumer slot when the Critical fio_hi is the #1 IO user.
#
# The run has TWO phases, write then read, because io.max caps the two directions
# separately (wbps/wiops vs rbps/riops) and only running both shows that the read half is
# wired up at all. Each phase relaunches the same three scopes, so a phase starts
# unthrottled and earns its own cap; both phases land in ONE summary table at the end.
#
# The three scopes run CONCURRENTLY within a phase (each systemd-run is backgrounded). A
# sampler prints each scope's live rd/wr MB/s and IOPS from io.stat, and on exit a
# per-scope before-vs-after-throttle table is printed -- that table is the result.
#
# PROFILE picks WHICH HALF of the disk gate the workload exercises. The gate needs both
# device saturation AND task stall to reach critical, and the two are produced by very
# different IO patterns:
#
#   PROFILE=device (default)  async O_DIRECT, deep queues.
#       Saturates the DEVICE: high util / await / queue depth -> disk_combined ~0.85.
#       Stall comes from the block layer, not from writeback: once the in-flight IO
#       exceeds the tag pool, submitters block in blk_mq_get_tag() and io.full rises.
#       A device with a deeper tag pool never blocks there, so the gate stops at "high"
#       (armed, top consumer identified, nothing throttled) -- use PROFILE=stall then.
#
#   PROFILE=stall             synchronous BUFFERED writes with periodic fsync.
#       Every job blocks in pwrite()/fsync(), and once dirty pages exceed the kernel's
#       dirty_ratio, balance_dirty_pages() throttles *every* writer on the box -- which
#       is what io.full actually measures. Use this to drive the gate to critical and
#       exercise the throttle path. It WILL make the desktop feel sluggish; that is the
#       point, and it is what "the disk is hurting the system" means.
#
# config.yaml:
#
#   # Disk-IO test apps driven by balancer/test/testing_io.sh. Only the leading executable of
#   # `commandline` is ever read (to derive an exe name), so the fio flags are deliberately
#   # left out: the script relaunches each workload with different flags for its write and
#   # read phase, and a pinned full command line would stop matching halfway through the run.
#   # NOTE: the script's third workload (fio_lo2 / lo2-io.scope) is deliberately absent here
#   # and must NOT be registered: it is the unmanaged-app case, capped at 'undefined' rates.
#   - name: "fio_high"
#     id: "hi-io.scope"
#     commandline: "/tmp/fio_hi --name=hi"
#     bpf_name: ["fio_hi"]
#     process_names: ["fio_hi"]
#   - name: "fio_lo"
#     id: "lo-io.scope"
#     commandline: "/tmp/fio_lo --name=lo"
#     bpf_name: ["fio_lo"]
#     process_names: ["fio_lo"]
#
# Individual knobs override the profile: SIZE, RUNTIME, READ_RUNTIME, DIR, JOBS, IODEPTH,
# SAMPLE, ENGINE, DIRECT, FSYNC, BS.
set -euo pipefail

PROFILE="${PROFILE:-device}"
case "$PROFILE" in
    # Keep JOBS x IODEPTH x 3 scopes at or above the tag pool (nr_requests x
    # nr_hw_queues): below it nothing stalls and the gate stops at "high".
    device) _ENGINE=libaio; _DIRECT=1; _FSYNC=0;  _BS=4k;  _JOBS=16; _IODEPTH=128 ;;
    stall)  _ENGINE=psync;  _DIRECT=0; _FSYNC=32; _BS=64k; _JOBS=32; _IODEPTH=1   ;;
    *) echo "unknown PROFILE='$PROFILE' (expected 'device' or 'stall')"; exit 1 ;;
esac

# Hot span per scope, deliberately small. fio lays every file out before the phase
# starts, and enough write volume drops an SSD out of its burst regime mid-run -- every
# scope then slows at once, which reads like the cap hurting the Critical app when it is
# really the device. Depth, not span, saturates the disk, so a small span is free.
SIZE="${SIZE:-2G}"
RUNTIME="${RUNTIME:-600}"          # long by default so you can register the apps mid-run
READ_RUNTIME="${READ_RUNTIME:-$RUNTIME}"   # second phase; total wall clock is the sum
DIR="${DIR:-/tmp}"
JOBS="${JOBS:-$_JOBS}"             # per-scope fio jobs
IODEPTH="${IODEPTH:-$_IODEPTH}"    # per-job queue depth; deep queue -> high await/aqu
SAMPLE="${SAMPLE:-5}"              # rate-comparison sampling interval (s)
ENGINE="${ENGINE:-$_ENGINE}"
DIRECT="${DIRECT:-$_DIRECT}"       # 0 = buffered (dirty-page writeback throttling -> io.full)
FSYNC="${FSYNC:-$_FSYNC}"          # fsync every N writes; 0 disables
BS="${BS:-$_BS}"
FIO_HI="${DIR}/fio_hi"
FIO_LO="${DIR}/fio_lo"
FIO_LO2="${DIR}/fio_lo2"
CG_ROOT="/sys/fs/cgroup"
SCOPES=(hi-io lo-io lo2-io)
# Human-readable priority per scope, printed in the comparison table.
declare -A SCOPE_PRIORITY=( [hi-io]="critical" [lo-io]="low" [lo2-io]="undefined*" )
# Per-sample log: "epoch scope cum_rbytes cum_wbytes cum_rios cum_wios limited cap phase"
SAMPLES_CSV="$(mktemp)"
# Read by the sampler on every tick so each sample is tagged with the phase that produced
# it. A file rather than a variable: the sampler runs in a subshell and cannot see later
# assignments made by the main script.
PHASE_FILE="$(mktemp)"; echo "write" > "$PHASE_FILE"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing '$1'"; exit 1; }; }
need systemd-run
need fio

# Distinct-named copies so comm differs (fio_hi vs fio_lo vs fio_lo2).
# /proc/<pid>/comm caps at 15 chars.
cp -f "$(command -v fio)" "$FIO_HI"
cp -f "$(command -v fio)" "$FIO_LO"
cp -f "$(command -v fio)" "$FIO_LO2"

echo "Dropping page cache (needs sudo)..."
sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null

# --- cgroup readers -------------------------------------------------------------------
_cg_path() {  # $1 = unit name -> absolute cgroup dir, empty when the unit is gone
    local cg; cg=$(systemctl show -p ControlGroup --value "$1" 2>/dev/null || true)
    [ -n "$cg" ] && echo "${CG_ROOT}${cg}" || echo ""
}
_iostat_counters() {  # $1 = unit -> "rbytes wbytes rios wios", or empty when unreadable.
    # Empty (not "0 0 0 0") on purpose: once a scope exits its cgroup disappears, and
    # feeding zeros to the sampler would look like the counters went backwards and wipe
    # out the final interval of the after-throttle phase.
    local d; d=$(_cg_path "$1")
    [ -n "$d" ] && [ -r "$d/io.stat" ] || { echo ""; return; }
    awk '{for(i=1;i<=NF;i++){
             if($i~/^rbytes=/){sub("rbytes=","",$i); r+=$i}
             else if($i~/^wbytes=/){sub("wbytes=","",$i); w+=$i}
             else if($i~/^rios=/){sub("rios=","",$i); ri+=$i}
             else if($i~/^wios=/){sub("wios=","",$i); wi+=$i}}}
         END{printf "%d %d %d %d", r+0, w+0, ri+0, wi+0}' "$d/io.stat"
}
_scope_cap() {  # $1 = unit -> io.max bandwidth caps as "w20r30" (MB/s), "-" when unthrottled
    # io.max is empty while unthrottled; a concrete cap shows e.g. "259:0 rbps=31457280
    # wbps=20971520 riops=9000 wiops=6000".  Unset directions read "max", which the
    # digits-only match below skips, so a read-only cap prints as "r30".
    local d cap; d=$(_cg_path "$1")
    [ -n "$d" ] && [ -r "$d/io.max" ] || { echo "-"; return; }
    cap=$(awk '{for(i=1;i<=NF;i++){
                   if($i~/^wbps=[0-9]+$/){sub("wbps=","",$i); w=$i}
                   else if($i~/^rbps=[0-9]+$/){sub("rbps=","",$i); r=$i}}}
               END{s=""; if(w!="") s=s sprintf("w%.0f", w/1048576)
                         if(r!="") s=s sprintf("r%.0f", r/1048576); print s}' "$d/io.max")
    echo "${cap:--}"
}

# --- live rate comparison -------------------------------------------------------------
compare_loop() {
    declare -A prev   # "<scope>" -> "rbytes wbytes rios wios" from the previous sample
    local now line s cur cap phase
    # Wait for the scopes to exist before taking baselines, otherwise the first interval
    # reports each scope's whole cumulative counter as if it happened in one SAMPLE window.
    for _ in $(seq 1 20); do
        [ -n "$(_iostat_counters hi-io.scope)" ] && break
        sleep 0.5
    done
    for s in "${SCOPES[@]}"; do prev[$s]=$(_iostat_counters "${s}.scope"); done

    while sleep "$SAMPLE"; do
        now=$(date +%s)
        phase=$(cat "$PHASE_FILE" 2>/dev/null || echo write)
        line=""
        for s in "${SCOPES[@]}"; do
            cur=$(_iostat_counters "${s}.scope")
            cap=$(_scope_cap "${s}.scope")
            if [ -n "$cur" ]; then
                printf '%s %s %s %s %s %s\n' "$now" "$s" "$cur" \
                    "$([ "$cap" = "-" ] && echo 0 || echo 1)" "$cap" "$phase" >> "$SAMPLES_CSV"
                # A scope relaunched for the read phase is a brand-new cgroup whose counters
                # restart at zero, so the stored baseline belongs to a cgroup that no longer
                # exists; using it would print a large negative rate for one interval.
                if [ -n "${prev[$s]}" ] && \
                   awk -v p="${prev[$s]}" -v c="$cur" 'BEGIN{split(p,P," ");split(c,C," ");
                        exit (C[1]>=P[1] && C[2]>=P[2]) ? 0 : 1}'; then
                    line+=$(awk -v p="${prev[$s]}" -v c="$cur" -v iv="$SAMPLE" -v s="$s" -v cap="$cap" '
                        BEGIN{split(p,P," "); split(c,C," "); m=1048576*iv;
                              printf "%s[%s]: rd=%5.1f wr=%5.1f MB/s  rIOPS=%6.0f wIOPS=%6.0f | ",
                                     s, (cap=="-"?"free":"CAP " cap),
                                     (C[1]-P[1])/m, (C[2]-P[2])/m, (C[3]-P[3])/iv, (C[4]-P[4])/iv}')
                fi
                prev[$s]="$cur"
            fi
        done
        [ -n "$line" ] && echo "[${phase}] ${line%| }"
    done
}

# --- diagnostics: why did (or didn't) the throttle fire -------------------------------
print_diagnostics() {
    echo
    echo "----------------------------- pressure diagnostics -----------------------------"
    if [ -r /proc/pressure/io ]; then
        echo "  /proc/pressure/io (system-wide task stall on IO):"
        sed 's/^/    /' /proc/pressure/io
        echo "    NOTE: the disk gate needs stall severity = min(1, some*W_some + full*W_full)"
        echo "          to reach 1.0 while the disk is saturated. With the shipped weights"
        echo "          (some=0.5 full=3.0) that means io.full >= ~0.33. If 'full avg10' above"
        echo "          is near zero, the workload never blocks tasks on IO -- re-run with"
        echo "          ENGINE=psync, or raise disk_psi_weights.some in config/config.yaml."
    fi
    echo "  io.max actually written by smartune:"
    local s d
    for s in "${SCOPES[@]}"; do
        d=$(_cg_path "${s}.scope")
        if [ -n "$d" ] && [ -r "$d/io.max" ] && [ -s "$d/io.max" ]; then
            sed "s|^|    ${s}: |" "$d/io.max"
        else
            echo "    ${s}: (no cap / cgroup already gone)"
        fi
    done
    echo "  smartune's own view (grep the service log for these tags):"
    echo "    [disk-level] the disk level, the USE + PSI numbers behind it, per-disk detail"
    echo "    [disk-io]    candidate list, why each was skipped, and the cap applied"
    echo "--------------------------------------------------------------------------------"
}

# End-of-run comparison, both workload phases in one table. Every row is a time-weighted
# average over the samples in its segment, not a peak.
#
# A throttled scope is split at ITS OWN first throttle: lo-io and lo2-io are limited on
# different ticks, so one global split point would mis-bucket the other's samples. A scope
# that was never throttled -- hi-io, which is Critical and must stay untouched -- is split
# at the RUN's first throttle instead, so it still gets a before/after pair covering the
# same two time windows as the scopes around it.
#
# A SECOND table adds the aggregate and each scope's share of it, because absolute MB/s
# cannot tell a working cap from a device that slowed down on its own: if TOTAL collapses
# while the uncapped scope's share climbs, the pie shrank and the cap still handed it a
# bigger slice. That table splits EVERY scope at the run's first throttle -- rows summed
# into a TOTAL have to cover one window, which per-scope split points do not.
summarize_rates() {
    [ -s "$SAMPLES_CSV" ] || { echo; echo "(no samples collected -- scopes never became readable)"; return 0; }
    echo
    echo "======================== Throughput: before vs after throttle ========================"
    awk -v p_hi="${SCOPE_PRIORITY[hi-io]}" -v p_lo="${SCOPE_PRIORITY[lo-io]}" \
        -v p_lo2="${SCOPE_PRIORITY[lo2-io]}" '
        BEGIN{ PRIO["hi-io"]=p_hi; PRIO["lo-io"]=p_lo; PRIO["lo2-io"]=p_lo2 }
        { n++; T[n]=$1; S[n]=$2; R[n]=$3; W[n]=$4; RI[n]=$5; WI[n]=$6; L[n]=$7; C[n]=$8; P[n]=$9
          if ($7==1){
            if (!(($2,$9) in tlim)) tlim[$2,$9]=$1
            if (!($9 in gfirst) || $1 < gfirst[$9]) gfirst[$9]=$1
            cap[$2,$9]=$8 } }
        END{
          m=1048576
          for(i=1;i<=n;i++){ s=S[i]; wl=P[i]; b=s SUBSEP wl
            # Baselines are per (scope, workload): a scope is relaunched between phases, so
            # its counters restart at zero and the previous phase is not a valid baseline.
            if(b in pt){ dt=T[i]-pt[b]
              if(dt>0){ dr=R[i]-pr[b]; dw=W[i]-pw[b]; dri=RI[i]-pri[b]; dwi=WI[i]-pwi[b]
                if(dr<0)dr=0; if(dw<0)dw=0; if(dri<0)dri=0; if(dwi<0)dwi=0
                split_t = ((s,wl) in tlim) ? tlim[s,wl] : ((wl in gfirst) ? gfirst[wl] : 0)
                ph = (split_t>0 && T[i]>split_t) ? "after" : "before"
                k = s SUBSEP wl SUBSEP ph
                SR[k]+=dr; SW[k]+=dw; SRI[k]+=dri; SWI[k]+=dwi; SD[k]+=dt
                # Same interval, bucketed by the RUN-wide split point, for the share table.
                gsplit = (wl in gfirst) ? gfirst[wl] : 0
                gph = (gsplit>0 && T[i]>gsplit) ? "after" : "before"
                gk = s SUBSEP wl SUBSEP gph
                GR[gk]+=dr; GW[gk]+=dw; GD[gk]+=dt } }
            pr[b]=R[i]; pw[b]=W[i]; pri[b]=RI[i]; pwi[b]=WI[i]; pt[b]=T[i] }

          printf "%-8s %-11s %-9s %-7s %9s %9s %9s %9s %7s %8s\n",
                 "scope","priority","workload","phase","rd MB/s","wr MB/s","rd IOPS","wr IOPS",
                 "secs","cap"
          nw=split("write read", wls, " ")
          nord=split("hi-io lo-io lo2-io", ord, " ")
          any_lim=0
          for(q=1;q<=nw;q++){ wl=wls[q]; rows=0
            for(o=1;o<=nord;o++){ s=ord[o]
              for(p=0;p<2;p++){ ph=(p==0?"before":"after"); k=s SUBSEP wl SUBSEP ph
                if(k in SD && SD[k]>0){
                  rows++
                  if(ph=="after" && ((s,wl) in cap)) any_lim=1
                  printf "%-8s %-11s %-9s %-7s %9.1f %9.1f %9.0f %9.0f %7.0f %8s\n",
                         s, PRIO[s], wl, ph, SR[k]/m/SD[k], SW[k]/m/SD[k],
                         SRI[k]/SD[k], SWI[k]/SD[k], SD[k],
                         (ph=="after" && ((s,wl) in cap)) ? cap[s,wl] : "-" } } }
            if(rows && q<nw) printf "\n" }

          if(!any_lim){
            printf "\n  NO SCOPE WAS EVER THROTTLED -- every row above covers a whole phase.\n"
            printf "  The disk never reached the critical disk-IO level, so there is nothing to\n"
            printf "  compare. The diagnostics below show which half of the gate fell short.\n" }
          else{
            printf "\n  cap: the io.max bandwidth smartune wrote, w=write r=read, in MB/s.\n"
            printf "  hi-io is split at the run first throttle (it is never capped itself), so its\n"
            printf "  before/after rows cover the same windows as the scopes above -- flat is a pass.\n"
            printf "  (* lo2-io is intentionally unregistered -> capped at the stricter\n     undefined rates, not the Low rates.)\n" }

          printf "\n%s\n", any_lim \
            ? "------- aggregate and share (every scope split at the RUN first throttle) -------" \
            : "------------- aggregate and share (never throttled: whole phase) ---------------"
          printf "%-8s %-9s %-7s %9s %9s %8s %7s\n",
                 "scope","workload","phase","rd MB/s","wr MB/s","share%","secs"
          for(q=1;q<=nw;q++){ wl=wls[q]
            for(p=0;p<2;p++){ ph=(p==0?"before":"after")
              tr=0; tw=0; tsec=0; rows=0
              for(o=1;o<=nord;o++){ gk=ord[o] SUBSEP wl SUBSEP ph
                rr[o]=0; rw[o]=0
                if(gk in GD && GD[gk]>0){
                  rows++
                  rr[o]=GR[gk]/m/GD[gk]; rw[o]=GW[gk]/m/GD[gk]
                  if(GD[gk]>tsec) tsec=GD[gk] }
                tr+=rr[o]; tw+=rw[o] }
              if(!rows) continue
              # A phase moves one direction; sharing out the idle one is noise.
              tot = (wl=="write") ? tw : tr
              for(o=1;o<=nord;o++){
                sh = (tot>0) ? 100*((wl=="write") ? rw[o] : rr[o])/tot : 0
                printf "%-8s %-9s %-7s %9.1f %9.1f %7.1f%% %7.0f\n",
                       ord[o], wl, ph, rr[o], rw[o], sh, tsec }
              printf "%-8s %-9s %-7s %9.1f %9.1f %8s %7.0f\n\n",
                     "TOTAL", wl, ph, tr, tw, "100.0%", tsec } }

          printf "  TOTAL is the three scopes summed (~whole-device throughput on an idle box);\n"
          printf "  share%% is of TOTAL in the phase direction. A working cap RAISES hi-io share.\n"
          printf "  If TOTAL drops too, the device itself got slower -- that is not the cap.\n"
        }' "$SAMPLES_CSV"
    echo "======================================================================================"
}

# One extra sample outside the sampler's own cadence. Needed at the end of each phase and
# at exit: the scopes vanish the moment fio finishes, so without it the last SAMPLE seconds
# -- often the most throttled part of a phase -- never make it into the table.
sample_once() {
    local now s cur cap phase
    now=$(date +%s)
    phase=$(cat "$PHASE_FILE" 2>/dev/null || echo write)
    for s in "${SCOPES[@]}"; do
        cur=$(_iostat_counters "${s}.scope")
        cap=$(_scope_cap "${s}.scope")
        if [ -n "$cur" ]; then
            printf '%s %s %s %s %s %s\n' "$now" "$s" "$cur" \
                "$([ "$cap" = "-" ] && echo 0 || echo 1)" "$cap" "$phase" >> "$SAMPLES_CSV"
        fi
    done
    # The call at end-of-phase runs after every scope is gone, so every branch above is
    # skipped and the loop's status is the last failed test. Under `set -e` a function
    # returning 1 as a bare command kills the shell -- from inside the EXIT trap that
    # aborted cleanup before it could print the table.
    return 0
}

_CLEANED=0
cleanup() {
    [ "$_CLEANED" = 1 ] && return 0   # idempotent: EXIT may fire after an INT/TERM exit
    _CLEANED=1
    set +e   # the summary is the point of the run: no single failing probe may skip it
    sample_once
    [ -n "${CMP_PID:-}" ] && kill "$CMP_PID" 2>/dev/null || true
    summarize_rates
    print_diagnostics
    # hi.dat/lo*.dat are created by root (sudo systemd-run), so remove them with sudo.
    sudo rm -f "$FIO_HI" "$FIO_LO" "$FIO_LO2" \
        "${DIR}/hi.dat" "${DIR}/lo.dat" "${DIR}/lo2.dat" 2>/dev/null || true
    rm -f "$SAMPLES_CSV" "$PHASE_FILE" 2>/dev/null || true
    stop_scopes
}

# Stop the transient fio scopes -- they run as root under systemd, so a bare Ctrl-C on this
# script would otherwise leave them writing in the background. Also used between phases:
# systemd refuses to start a unit name that is still registered.
stop_scopes() {
    local s
    for s in "${SCOPES[@]}"; do
        sudo systemctl stop "${s}.scope" 2>/dev/null || true
    done
}
# EXIT does the work; INT/TERM just exit so the EXIT trap runs cleanup exactly once
# (the _CLEANED guard makes a double-fire a no-op regardless).
trap cleanup EXIT
trap 'exit 130' INT TERM

# Random IO raises device latency + queue depth (what drives USE pressure on SSD/NVMe --
# sequential dd only tops out %util, not pressure). Whether it also stalls *tasks* depends
# on DIRECT/ENGINE/FSYNC, i.e. on PROFILE -- see the header.
_fio_args() {  # $1 = rw mode, $2 = runtime (s)
    local a="--ioengine=${ENGINE} --direct=${DIRECT} --rw=$1 --bs=${BS} --numjobs=${JOBS} \
--iodepth=${IODEPTH} --time_based --runtime=$2 --group_reporting --size=${SIZE}"
    # fsync is a write-side knob; passing it to a read job is a fio error.
    [ "${FSYNC}" -gt 0 ] 2>/dev/null && [ "$1" = "randwrite" ] && a="${a} --fsync=${FSYNC}"
    echo "$a"
}

run_phase() {  # $1 = phase name (write|read), $2 = rw mode, $3 = runtime (s)
    local args hi lo lo2 w
    args=$(_fio_args "$2" "$3")
    # A transient scope lingers for a moment after its process exits, and systemd-run
    # refuses to reuse a unit name that is still registered. No-op on the first phase.
    for w in $(seq 1 20); do
        [ -z "$(_cg_path hi-io.scope)$(_cg_path lo-io.scope)$(_cg_path lo2-io.scope)" ] && break
        sleep 0.5
    done
    echo "$1" > "$PHASE_FILE"
    echo
    echo "===== phase '$1' ($2, ${3}s): hi-io (Critical), lo-io (Low), lo2-io (unregistered) ====="
    sudo systemd-run --scope --unit=hi-io.scope "$FIO_HI" --name=hi $args --filename="${DIR}/hi.dat" &
    hi=$!
    sudo systemd-run --scope --unit=lo-io.scope "$FIO_LO" --name=lo $args --filename="${DIR}/lo.dat" &
    lo=$!
    sudo systemd-run --scope --unit=lo2-io.scope "$FIO_LO2" --name=lo2 $args --filename="${DIR}/lo2.dat" &
    lo2=$!
    if [ -z "${CMP_PID:-}" ]; then compare_loop & CMP_PID=$!; fi
    wait "$hi" "$lo" "$lo2" || echo "  (a workload exited non-zero -- continuing)"
    sample_once   # before the scopes disappear, so the phase's last window is counted
    stop_scopes
}

echo "  PROFILE=${PROFILE}: engine=${ENGINE} direct=${DIRECT} fsync=${FSYNC} bs=${BS}" \
     "jobs=${JOBS} iodepth=${IODEPTH} runtime=${RUNTIME}s+${READ_RUNTIME}s"
[ "$PROFILE" = "device" ] && echo \
"  NOTE: this profile saturates the device but produces little task stall, so the gate may
        stop at 'high'. Use PROFILE=stall to force it to critical."

cat <<'STEPS'
--------------------------------------------------------------------------------
Three workloads run CONCURRENTLY (only together do they push the disk hard enough
to matter), first a write phase and then a read phase over the same files.
While running:
  1. In the dashboard "Add App" wizard, register exactly TWO of them:
       fio_hi   -> priority Critical   (never throttled)
       fio_lo   -> priority Low        (throttled when disk IO hits critical)
     Leave fio_lo2 UNREGISTERED on purpose -- it is the unmanaged-app case and will be
     capped at the stricter 'undefined' rates. Register them during the WRITE phase;
     the read phase reuses the same app entries.
  2. The [write]/[read] lines show each scope's live rd/wr MB/s and IOPS, tagged
     free / CAP <n>. Once smartune throttles a scope its rate drops to the cap while
     hi-io keeps running at full speed.
  3. On exit (run end or Ctrl-C) ONE table covering both phases prints, followed by
     pressure diagnostics explaining the outcome either way.
     (Also: python3 balancer/test/psi_probe.py 600 1 \
              --scope hi-io.scope,lo-io.scope,lo2-io.scope)
--------------------------------------------------------------------------------
STEPS

# The EXIT trap runs cleanup -> summarize_rates, so the table prints whether the run ends
# at RUNTIME or at a Ctrl-C part-way through either phase.
run_phase write randwrite "$RUNTIME"

# Reads must come off the device, not out of the page cache the write phase just filled --
# with DIRECT=0 (PROFILE=stall) a cached read produces no disk IO and no pressure at all.
echo "Dropping page cache before the read phase (needs sudo)..."
sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null
run_phase read randread "$READ_RUNTIME"

echo "Testing done."
