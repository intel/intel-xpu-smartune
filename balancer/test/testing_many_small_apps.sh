#!/bin/bash

# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# DIAGNOSTIC for the "many small apps" gap. This script does not pass or fail --
# it answers four questions that decide WHERE the fix belongs. Run it before
# changing any balancer code.
#
# The gap, in one line: smartune ranks the single top consumer and skips limiting
# when it is below ~25% of MemTotal (reach_threshold in res_monitor.py). Twelve
# apps at 7% each therefore produce no L1 cap, and because L2's candidate list is
# built from *auto-limited* apps, no L2 reclaim either -- the whole ladder stands
# down while the box is 85% full.
#
# THE FOUR QUESTIONS
#   Q1  Does system pressure reach "critical" at all in this shape?
#       It matters because _tick_combined_policy / _tick_separated_policy only
#       enter the limiting branch at critical.  If the answer is no, the fix is
#       NOT "give L2 more candidates" -- it is in the pressure classifier, which
#       is a much bigger change.  This is the question the whole run exists for.
#   Q2  Did L1 fire?  Ground truth: did any scope get memory.high / cpu.max?
#   Q3  Did L2 fire?  Ground truth: did any scope's memory.current fall while its
#       memory.swap.current rose?  That pair can only be reclaim.
#   Q4  Is the memory actually in these apps?  coverage = sum(memory.current) /
#       used_eff.  Low coverage means the shortfall lives in page cache, the
#       kernel, or a cgroup we may not touch -- and then extending L2's candidate
#       set cannot help, no matter how many candidates it gets.
#
# It also settles one side question that came out of the io.max/reclaim review:
#       does swap writeback get charged to the cgroup whose pages are being
#       reclaimed?
# The per-scope table splits each scope's io.stat wbytes into the ticks where its
# memory.current FELL and the ticks where it did not. If writeback is charged to
# the owning cgroup, nearly all the bytes land in the "falling" column. If they
# do not, the io.max-throttles-reclaim concern is weaker than assumed and the
# mutual-exclusion work can be scoped down.
#
# MODE=one is the control. Same total memory, one app instead of N, which puts
# the consumer back above the threshold. Everything smartune does in MODE=one and
# skips in MODE=many is the gap, measured rather than argued.
#
#   MODE=many (default)  N scopes sharing the shortfall -> each below the 25%
#                        line, all of them together far above it.
#   MODE=one             1 scope holding the whole shortfall -> above the line.
#                        Expect L1 to cap it and L2 to pull it down.
#
# TARGET_USED is where used_eff should END UP, not how much to allocate. The
# workload is sized to the gap between the box's current used_eff and that target,
# because a fraction-of-MemTotal knob silently adds to whatever the box is already
# using -- ask for 0.85 on a box sitting at 0.19 and the real demand is 1.04, which
# measures the OOM killer rather than the ladder.
#
# TARGET_USED must be high enough to reach critical, and the bar is higher than it
# looks. mem_scarcity is a logistic centred on memory_busy_threshold that only
# ASYMPTOTES to 1.0, so the sole way past a critical threshold of 1.0 is the
# round-to-two-decimals in calculate_pressure_score: the score has to reach 0.995,
# which at the default 80/8 means free memory <= 6.8%, i.e. used_eff >= 93.2%. A run
# at 0.85 parks at "high" forever and answers nothing. check_target_reached() below
# computes that bar from the live config and says so while there is still time to
# change it.
#
# SWAPPINESS exists because the kernel actively fights this run. Under the default
# vm.swappiness=60 it moves the scopes' quiet anon pages out, which RAISES
# MemAvailable, which LOWERS mem_scarcity -- the workload's own success pushes the
# score away from critical, and --vm-hang means the pages never come back. The box
# then settles at a fixed point below critical no matter what TARGET_USED says. A
# low swappiness keeps those pages resident so the sizing arithmetic holds. It does
# not weaken what is under test: memory.high and memory.reclaim force reclaim
# regardless of swappiness (an L1-capped scope swaps out under any setting), so Q3
# stays measurable. Do not set 0 -- most kernels then refuse anon reclaim entirely.
#
# The workloads are deliberately NOT registered in config.yaml. Unregistered is
# both the realistic case (you do not enroll twelve incidental processes) and the
# stricter one -- unregistered apps fall in the 'undefined' rate band. Do not add
# them in the wizard.
#
# Each scope allocates once, touches every page, then holds it quietly for the
# rest of the run (--vm-hang). Quiet is the point: L2 checks a candidate for
# idleness before reclaiming it, so a workload that keeps re-touching its buffer
# would be skipped for a reason that has nothing to do with the gap under test.
#
# Knobs: MODE, N, TARGET_USED, SWAPPINESS, RUNTIME, SAMPLE, DIR, LOG, VM_HANG.
set -euo pipefail

MODE="${MODE:-many}"
N="${N:-12}"                       # scope count in MODE=many
# The critical band needs the pressure score to round to 1.00, which on the
# mem_scarcity gate means free <= 6.77% of MemTotal. 0.94 measured out at 92.3%
# used (score 0.99) and never crossed: with swap available the kernel pushes the
# quiet anon pages out as fast as they are allocated. 0.96 plus a low swappiness
# holds the box above the bar for the whole run.
TARGET_USED="${TARGET_USED:-0.96}" # used_eff fraction the run should drive the box to
SWAPPINESS="${SWAPPINESS:-5}"      # vm.swappiness for the run; "keep" leaves it alone
RUNTIME="${RUNTIME:-600}"
SAMPLE="${SAMPLE:-5}"
DIR="${DIR:-/tmp}"
CG_ROOT="/sys/fs/cgroup"
SWAP_DIR="${SWAP_DIR:-/var/lib/smartune/swap}"
_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="${CONFIG:-${_REPO}/config/config.yaml}"
LOG="${LOG:-${_REPO}/logs/multi_tasks_latest.log}"

# Start of the window the log greps are restricted to, in the log's own "ts"
# format so the two can be compared as strings. Set here rather than next to the
# workload launch so that a run which dies during setup still has a valid window.
RUN_START_ISO="$(date +%Y-%m-%dT%H:%M:%S)"

case "$MODE" in
    many) ;;
    one)  N=1 ;;
    *) echo "unknown MODE='$MODE' (expected 'many' or 'one')"; exit 1 ;;
esac

# Hold the memory for the whole run rather than cycling: a free/realloc sawtooth
# makes used_eff oscillate, and every dip re-arms timers on smartune's side.
VM_HANG="${VM_HANG:-$RUNTIME}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing '$1'"; exit 1; }; }
need systemd-run
need stress   # stress-ng works too but its --vm-hang semantics differ; not used here

MEM_TOTAL_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
MEM_TOTAL=$((MEM_TOTAL_KB * 1024))
BASE_USED=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{printf "%d", (t-a)*1024}' /proc/meminfo)
BASE_PCT=$(awk -v u="$BASE_USED" -v t="$MEM_TOTAL" 'BEGIN{printf "%.0f", 100*u/t}')
# awk, not bash: byte-scale arithmetic on a float fraction.
TOTAL_BYTES=$(awk -v t="$MEM_TOTAL" -v g="$TARGET_USED" -v b="$BASE_USED" \
                  'BEGIN{v = t*g - b; printf "%d", (v > 0) ? v : 0}')
PER_APP=$((TOTAL_BYTES / N))
PER_APP_PCT=$(awk -v p="$PER_APP" -v t="$MEM_TOTAL" 'BEGIN{printf "%.1f", 100*p/t}')

if [ "$TOTAL_BYTES" -lt $((256 * 1024 * 1024)) ]; then
    # The box is already at or above the target, so there is nothing left to
    # allocate and the run would measure an idle workload against a busy box.
    echo "REFUSING: used_eff is already ${BASE_PCT}%, at or near TARGET_USED=${TARGET_USED}."
    echo "Free memory first, or raise TARGET_USED above ${BASE_PCT}%."
    exit 1
fi

# Per-sample rows. Two files rather than one tagged file: the system row and the
# per-cgroup rows have nothing in common but the timestamp, and one awk pass over
# each is far easier to follow than a union schema with unused columns.
SYS_CSV="$(mktemp)"   # "epoch mem_total mem_available swap_total swap_free psi_some psi_full"
CG_CSV="$(mktemp)"    # "epoch scope mem_current swap_current has_memhigh has_cpumax has_iomax rbytes wbytes"

SCOPES=()
BINS=()
for i in $(seq 1 "$N"); do
    # /proc/<pid>/comm caps at 15 chars, and smartune matches a consumer to an app
    # by comm -- distinct names are what keep the N scopes from being folded into
    # one entry by the cgroup aggregation, which would hand us the single-big-app
    # case and hide the very thing under test.
    SCOPES+=("$(printf 'sm%02d' "$i")")
    BINS+=("${DIR}/$(printf 'stress_s%02d' "$i")")
done

# --- readers ---------------------------------------------------------------------------
_cg_path() {  # $1 = unit name -> absolute cgroup dir, empty when the unit is gone
    local cg; cg=$(systemctl show -p ControlGroup --value "$1" 2>/dev/null || true)
    [ -n "$cg" ] && echo "${CG_ROOT}${cg}" || echo ""
}

_cg_sample() {  # $1 = unit -> "mem swap has_high has_cpumax has_iomax rbytes wbytes", empty if gone
    # Empty rather than zeros when the cgroup is gone: zeros would read as the
    # counters going backwards and wipe the scope's last interval out of the table.
    local d mem swap high cpu io stat r w
    d=$(_cg_path "$1")
    [ -n "$d" ] && [ -r "$d/memory.current" ] || { echo ""; return; }
    mem=$(cat "$d/memory.current" 2>/dev/null || echo 0)
    swap=$(cat "$d/memory.swap.current" 2>/dev/null || echo 0)
    # "max" means uncapped. Any number at all means smartune (or systemd on its
    # behalf) wrote a cap -- the value does not matter here, only that L1 fired.
    high=$(cat "$d/memory.high" 2>/dev/null || echo max)
    [ "$high" = "max" ] && high=0 || high=1
    cpu=$(awk '{print ($1=="max")?0:1}' "$d/cpu.max" 2>/dev/null || echo 0)
    io=$([ -s "$d/io.max" ] && echo 1 || echo 0)
    stat=$(awk '{for(i=1;i<=NF;i++){
                    if($i~/^rbytes=/){sub("rbytes=","",$i); r+=$i}
                    else if($i~/^wbytes=/){sub("wbytes=","",$i); w+=$i}}}
                END{printf "%d %d", r+0, w+0}' "$d/io.stat" 2>/dev/null || echo "0 0")
    echo "$mem $swap $high $cpu $io $stat"
}

_sys_sample() {  # -> "mem_total mem_available swap_total swap_free psi_some psi_full"
    local mi ps
    mi=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} /^SwapTotal:/{st=$2} /^SwapFree:/{sf=$2}
              END{printf "%d %d %d %d", t*1024, a*1024, st*1024, sf*1024}' /proc/meminfo)
    # avg10, the same window the pressure classifier reads. "0 0" when PSI is
    # unavailable (kernel built without it) -- the run still measures everything else.
    ps=$(awk '/^some/{s=$2} /^full/{f=$2}
              END{sub("avg10=","",s); sub("avg10=","",f); printf "%.2f %.2f", s+0, f+0}' \
         /proc/pressure/memory 2>/dev/null || echo "0 0")
    echo "$mi $ps"
}

# --- sampler ---------------------------------------------------------------------------
sample_once() {
    local now sys s cur
    now=$(date +%s)
    sys=$(_sys_sample)
    echo "$now $sys" >> "$SYS_CSV"
    for s in "${SCOPES[@]}"; do
        cur=$(_cg_sample "${s}.scope")
        [ -n "$cur" ] && echo "$now $s $cur" >> "$CG_CSV"
    done
    # Every branch above can be skipped once the scopes are gone, leaving the
    # loop's status as the last failed test. Under `set -e` that would kill the
    # shell from inside the EXIT trap, before the summary prints.
    return 0
}

sample_loop() {
    local sys used_pct swap_used_mb limited coverage line
    while sleep "$SAMPLE"; do
        sample_once
        sys=$(_sys_sample)
        # used_eff = MemTotal - MemAvailable, the same quantity the reclaim target
        # is expressed against. NOT MemTotal-MemFree: page cache is reclaimable and
        # counting it would show 95% on an idle box.
        read -r used_pct swap_used_mb <<<"$(awk -v s="$sys" 'BEGIN{split(s,S," ");
            printf "%.1f %.0f", 100*(S[1]-S[2])/S[1], (S[3]-S[4])/1048576}')"
        # Both read only the newest timestamp in the file: rows arrive grouped by
        # tick, so a new $1 resets the accumulators and the last group survives.
        # memory.current only, deliberately: swapped-out pages have already left
        # MemAvailable, so counting them here would inflate the ratio exactly when
        # reclaim starts working.
        coverage=$(awk -v s="$sys" 'BEGIN{split(s,S," "); used=S[1]-S[2]}
            {if($1>t){t=$1; held=0} held+=$3}
            END{printf "%.0f", (used>0)? 100*held/used : 0}' "$CG_CSV" 2>/dev/null || echo 0)
        limited=$(awk '{if($1>t){t=$1; n=0} if($5==1||$6==1) n++}
                  END{print n+0}' "$CG_CSV" 2>/dev/null || echo 0)
        printf '[%s] used=%s%% swap_used=%sMB psi(some/full)=%s/%s  scopes_limited=%s/%s  coverage=%s%%\n' \
            "$(date +%H:%M:%S)" "$used_pct" "$swap_used_mb" \
            "$(echo "$sys" | awk '{print $5}')" "$(echo "$sys" | awk '{print $6}')" \
            "$limited" "${#SCOPES[@]}" "$coverage"
    done
}

# --- summary ---------------------------------------------------------------------------
summarize() {
    [ -s "$CG_CSV" ] || { echo; echo "(no samples -- the scopes never became readable)"; return 0; }
    echo
    echo "=========================== per-scope: what smartune did ==========================="
    # wbytes is split by whether memory.current FELL in that interval. That split is
    # the swap-writeback attribution test described in the header: pages leaving a
    # cgroup and block writes charged to the same cgroup in the same tick is the
    # only shape that supports "io.max would throttle this reclaim".
    awk '
        { t=$1; s=$2; mem=$3; swp=$4; hi=$5; cp=$6; io=$7; rb=$8; wb=$9
          # First-seen order, not a sort: mawk has no asorti, and the scopes were
          # started in name order anyway so the table comes out sorted regardless.
          if (!(s in seen)) { seen[s]=1; ord[++n_scopes]=s }
          if (mem > peak_mem[s]) peak_mem[s]=mem
          if (swp > peak_swp[s]) peak_swp[s]=swp
          if (hi) { ever_hi[s]=1; if (!(s in hi_at)) hi_at[s]=t }
          if (cp) ever_cp[s]=1
          if (io) ever_io[s]=1
          if (s in pt) {
            dmem = mem - pmem[s]; dw = wb - pwb[s]
            if (dw < 0) dw = 0                       # cgroup recycled; no baseline
            if (dmem < 0) { fall_w[s]+=dw; fall_mb[s]+=-dmem; fall_n[s]++ }
            else          { flat_w[s]+=dw }
          }
          pmem[s]=mem; pwb[s]=wb; pt[s]=t; last_mem[s]=mem; last_swp[s]=swp }
        END{
          m=1048576
          printf "%-7s %9s %9s %9s %9s %7s %7s %7s %11s %11s\n",
                 "scope","peakMem","endMem","peakSwap","endSwap","mem.hi","cpu.max","io.max",
                 "wr@falling","wr@flat"
          n=n_scopes
          for (i=1;i<=n;i++) { s=ord[i]
            printf "%-7s %8.0fM %8.0fM %8.0fM %8.0fM %7s %7s %7s %10.0fM %10.0fM\n",
                   s, peak_mem[s]/m, last_mem[s]/m, peak_swp[s]/m, last_swp[s]/m,
                   (s in ever_hi)?"YES":"-", (s in ever_cp)?"YES":"-", (s in ever_io)?"YES":"-",
                   fall_w[s]/m, flat_w[s]/m
            tot_fall_w += fall_w[s]; tot_flat_w += flat_w[s]
            tot_fall_mb += fall_mb[s]; if (s in ever_hi) n_hi++
            tot_peak += peak_mem[s] }
          printf "\n  mem.hi/cpu.max = L1 ever fired on this scope. io.max = the disk channel capped it.\n"
          printf "  wr@falling = io.stat write bytes in ticks where memory.current FELL;\n"
          printf "  wr@flat    = write bytes in every other tick.\n"
          printf "\n  L1 fired on %d of %d scope(s). Total memory that left the scopes: %.0f MB.\n",
                 n_hi+0, n, tot_fall_mb/m
          if (tot_fall_w + tot_flat_w > 0)
            printf "  Write bytes charged to a scope while its memory was falling: %.0f%% of its total.\n",
                   100*tot_fall_w/(tot_fall_w+tot_flat_w)
          else
            printf "  No block writes were charged to any scope -- see the note below.\n"
        }' "$CG_CSV"
    echo "===================================================================================="

    echo
    echo "============================= system timeline (Q1/Q4) =============================="
    awk -v cg="$CG_CSV" '
        BEGIN{ m=1048576
               # Resident bytes held by the scopes, per timestamp, for the coverage
               # column. memory.current only -- swap is already out of MemAvailable.
               while ((getline l < cg) > 0) { split(l, F, " "); held[F[1]] += F[3] } }
        { t=$1; mt=$2; ma=$3; st=$4; sf=$5; some=$6; full=$7
          used = mt - ma; swu = st - sf
          if (!t0) t0 = t
          if (used > peak_used) peak_used = used
          if (swu > peak_swu) peak_swu = swu
          if (some > peak_some) peak_some = some
          if (full > peak_full) peak_full = full
          if (st > peak_st) peak_st = st
          if (!first_st) first_st = st
          cov = (used>0 && (t in held)) ? 100*held[t]/used : 0
          if (cov > peak_cov) peak_cov = cov
          n++; sum_cov += cov
          printf "  +%4ds  used=%5.1f%%  swapUsed=%6.0fM/%6.0fM  psi some=%5.2f full=%5.2f  coverage=%3.0f%%\n",
                 t-t0, 100*used/mt, swu/m, st/m, some, full, cov
          mt_last = mt }
        END{
          printf "\n  peak used_eff      %.1f%% of MemTotal\n", 100*peak_used/mt_last
          printf "  peak memory PSI    some=%.2f  full=%.2f   (avg10, the window the classifier reads)\n",
                 peak_some, peak_full
          printf "  peak swap in use   %.0f MB\n", peak_swu/m
          printf "  swap total         %.0f MB -> %.0f MB   (L0 grew it by %.0f MB)\n",
                 first_st/m, peak_st/m, (peak_st-first_st)/m
          printf "  coverage           peak %.0f%%, mean %.0f%%\n", peak_cov, (n?sum_cov/n:0)
        }' "$SYS_CSV"
    echo "===================================================================================="
}

# Reads the four questions back out of what was measured, so the run ends with an
# answer rather than a table to interpret.
verdict() {
    [ -s "$CG_CSV" ] || return 0
    echo
    echo "================================== THE VERDICT ====================================="
    local l1 l2 cov peak_used
    l1=$(awk '{if($5==1||$6==1) n++} END{print n+0}' "$CG_CSV")
    # A scope whose memory.current fell while memory.swap.current rose in the same
    # interval. Nothing but reclaim (or the kernel's own pressure, which is the same
    # mechanism) produces that pair.
    l2=$(awk '{s=$2
               if (s in pm && $3 < pm[s] && $4 > ps[s]) n++
               pm[s]=$3; ps[s]=$4} END{print n+0}' "$CG_CSV")
    peak_used=$(awk '{u=$2-$3; if(u/$2 > p) p=u/$2} END{printf "%.1f", 100*p}' "$SYS_CSV")
    cov=$(awk -v cg="$CG_CSV" '
        BEGIN{ while ((getline l < cg) > 0) { split(l, F, " "); held[F[1]] += F[3] } }
        { u=$2-$3; c=(u>0 && ($1 in held)) ? 100*held[$1]/u : 0; if (c>p) p=c }
        END{printf "%.0f", p}' "$SYS_CSV")

    # Highest level smartune itself reported during the window -- the classifier's
    # own verdict, which no amount of /proc reading can reconstruct.
    local peak_lvl
    peak_lvl=$(_log_msgs '\[sys-level\]' \
        | sed -n 's/.*level=\([a-z]*\).*/\1/p' \
        | awk 'BEGIN{r["low"]=0; r["medium"]=1; r["high"]=2; r["critical"]=3; best="(none logged)"; bi=-1}
               {if ($1 in r && r[$1] > bi) {bi=r[$1]; best=$1}} END{print best}')
    printf "  Q1  highest system pressure        -> %s   (peak used_eff %s%%)\n" "$peak_lvl" "$peak_used"
    if [ "$peak_lvl" = "(none logged)" ]; then
        printf "      No [sys-level] line in the window at all. That is INFO whenever the level\n"
        printf "      is not low, so either the level stayed low the whole run, or the daemon was\n"
        printf "      not running -- in which case Q2 and Q3 below mean nothing.\n"
    elif [ "$peak_lvl" != "critical" ]; then
        local bar; bar=$(_critical_free_pct)
        printf "      Never critical, so the limiting branch was never entered at all. Giving L2\n"
        printf "      more candidates cannot help a path that does not run.\n"
        # Two very different conclusions share this branch, and conflating them sent
        # one earlier investigation at the candidate set when the run had simply
        # never crossed the band. Peak used_eff decides which one this was.
        if [ "$bar" != "unreachable" ] \
           && awk -v u="$peak_used" -v b="$bar" 'BEGIN{exit !(100-u > b)}'; then
            printf "      But peak used_eff was only %s%% (free %.1f%%) and critical needs free <= %s%%,\n" \
                "$peak_used" "$(awk -v u="$peak_used" 'BEGIN{print 100-u}')" "$bar"
            printf "      so this run never got close enough to test the classifier either. It shows\n"
            printf "      NOTHING about where the fix belongs -- raise TARGET_USED and repeat.\n"
        else
            printf "      The box DID reach the memory level critical is supposed to fire at, and the\n"
            printf "      classifier still did not call it. At this memory level the fix belongs in\n"
            printf "      the classifier, not in candidate selection.\n"
        fi
    fi
    printf "  Q2  did L1 fire?                   -> %s\n" \
        "$([ "$l1" -gt 0 ] && echo "YES ($l1 sample(s) with a cap present)" || echo "NO -- no scope ever got memory.high or cpu.max")"
    printf "  Q3  did L2 reclaim?                -> %s\n" \
        "$([ "$l2" -gt 0 ] && echo "YES ($l2 interval(s) with mem falling AND swap rising)" || echo "NO -- no scope ever traded RAM for swap")"
    printf "  Q4  is the memory in these apps?   -> peak coverage %s%%\n" "$cov"
    if [ "${cov:-0}" -lt 50 ] 2>/dev/null; then
        printf "      Below 50%%: the shortfall is NOT mostly in these scopes. Extending L2's\n"
        printf "      candidate set cannot fix this shape -- find where the memory actually is\n"
        printf "      before changing the candidate logic.\n"
    fi
    # Only a claim if the box was actually under load. A short or small run that
    # never filled memory also ends with l1=l2=0, and calling that "the gap" would
    # be the script agreeing with whatever it was pointed at.
    if [ "$MODE" = "many" ] && [ "$l1" = "0" ] && [ "$l2" = "0" ]; then
        if awk -v u="$peak_used" 'BEGIN{exit !(u >= 70)}'; then
            printf "\n  MODE=many reproduced the gap: the box was %s%% full and the ladder did nothing.\n" "$peak_used"
            printf "  Now run MODE=one with the same TARGET_USED. If that one DOES get limited,\n"
            printf "  the shape of the workload is the only difference -- which is the whole claim.\n"
        else
            printf "\n  Nothing fired, but peak used_eff was only %s%% -- the box was never under\n" "$peak_used"
            printf "  memory pressure, so this run shows nothing either way. Raise TARGET_USED\n"
            printf "  or RUNTIME and repeat before drawing any conclusion.\n"
        fi
    fi
    echo "===================================================================================="
}

# Lines this run's window, matching $1, as "HH:MM:SS  msg". The log is JSON and one
# raw line is ~400 characters of envelope around a 100-character message -- printed
# whole, four tags' worth buries the summary above it.
# The window filter is a lexicographic compare on the ISO timestamp, which is only
# valid because both sides are local time in the same fixed-width format.
_log_msgs() {
    [ -r "$LOG" ] || return 0
    grep -E "$1" "$LOG" 2>/dev/null | awk -v since="$RUN_START_ISO" '
        { ts=""; msg=""
          if (match($0, /"ts": "[^"]*"/))  ts  = substr($0, RSTART+7, RLENGTH-8)
          if (match($0, /"msg": "[^"]*"/)) msg = substr($0, RSTART+8, RLENGTH-9)
          if (ts >= since) printf "%s  %s\n", substr(ts, 12, 8), msg }'
}

# What /proc cannot show: smartune's own reasoning.
print_log_evidence() {
    echo
    echo "============================ smartune's own reasoning =============================="
    if [ ! -r "$LOG" ]; then
        echo "  log not readable at: $LOG"
        echo "  (override with LOG=/path/to/multi_tasks_latest.log)"
        echo "===================================================================================="
        return 0
    fi
    echo "  from $LOG, this run's window only"
    local tag n
    # [sys-level] first: it is the direct answer to Q1 and it is INFO whenever the
    # level is anything but low, so it shows up even without SMARTUNE_LOG_LEVEL=DEBUG.
    for tag in '\[sys-level\]:8' 'reach_threshold=:8' '\[reclaim\]:12' '\[swap\]:6'; do
        n="${tag##*:}"; tag="${tag%:*}"
        echo
        echo "  --- $tag (last $n)"
        _log_msgs "$tag" | tail -"$n" | sed 's/^/    /'
    done
    echo
    echo "  The two lines that decide the fix:"
    echo "    'deficit N MB but no eligible auto-limited app'  -> L2 ran and found nobody."
    echo "       That is the gap exactly: pressure was real, the ladder had no candidates."
    echo "    'reach_threshold=False'                          -> L1 declined to cap the top"
    echo "       consumer, which is why L2 had no candidates in the first place."
    echo "    Both are DEBUG. Empty above means either level=INFO or neither path ran."
    echo "===================================================================================="
}

# Free-memory percentage at or below which the system pressure score reaches
# critical, derived from the live config rather than hardcoded -- a run that cannot
# cross this line cannot answer Q1, and the line moves whenever anyone touches
# memory_busy_threshold or mem_gate_steepness.
#
# mem_scarcity(free) = 1 / (1 + exp((free - c) / c * k)),  c = 1 - busy/100
# is a logistic: it approaches 1.0 but never reaches it, so comparing it against a
# critical threshold of 1.0 would never fire. What makes critical reachable at all
# is that calculate_pressure_score returns round(score, 2) -- so the real bar is
# `critical - 0.005`. Inverting the logistic at that point:
#   free <= c * (1 + ln(1/target - 1) / k)
# Prints the percentage, or "unreachable" when the config makes the band empty.
_critical_free_pct() {
    # /dev/null rather than a bail-out when the config is missing: the daemon falls
    # back to the config.py defaults in that case, so the awk defaults below are the
    # right answer, not an error.
    local src="$CONFIG"
    [ -r "$src" ] || src=/dev/null
    awk '
        # Top-level keys end the nested block; `thresholds:` and `disk_thresholds:`
        # have identical children, so only the former may set `crit`.
        /^thresholds:/              { blk=1; next }
        /^[^[:space:]#]/            { blk=0 }
        blk && /^[[:space:]]+critical:/ { crit=$2 }
        /^memory_busy_threshold:/   { busy=$2 }
        /^mem_gate_steepness:/      { steep=$2 }
        END{
          # Defaults match config.py, which is what applies when a key is absent.
          if (busy  == "" || busy  <= 0) busy  = 80
          if (steep == "" || steep <= 0) steep = 8
          if (crit  == "") crit = 1.0
          target = crit - 0.005
          c = 1 - busy/100
          if (target <= 0 || target >= 1 || c <= 0) { print "unreachable"; exit }
          free = c * (1 + log(1/target - 1) / steep)
          if (free <= 0) { print "unreachable"; exit }
          printf "%.1f", 100*free
        }' "$src" 2>/dev/null || echo "unreachable"
}

# Did the workload actually land where TARGET_USED aimed, and is that far enough to
# answer Q1? Called ONCE, mid-run, on purpose: every earlier version of this script
# ran the full RUNTIME and only then revealed that the box had settled below the
# critical band -- ten minutes to learn the run was unanswerable. Saying it here
# leaves time to Ctrl-C and raise TARGET_USED (or lower SWAPPINESS) instead.
check_target_reached() {
    local free_pct used_pct bar
    read -r used_pct free_pct <<<"$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2}
        END{printf "%.1f %.1f", 100*(t-a)/t, 100*a/t}' /proc/meminfo)"
    bar=$(_critical_free_pct)
    echo
    if [ "$bar" = "unreachable" ]; then
        printf '  NOTE: settled at used_eff=%s%% (free %s%%). The configured bands leave no\n' \
            "$used_pct" "$free_pct"
        printf '        reachable critical score at any memory level, so Q1 will read "high"\n'
        printf '        however full the box gets. Check thresholds/memory_busy_threshold in\n'
        printf '        %s before reading anything into this run.\n' "$CONFIG"
    elif awk -v f="$free_pct" -v b="$bar" 'BEGIN{exit !(f > b)}'; then
        printf '  WARNING: settled at used_eff=%s%% (free %s%%), but critical needs free <= %s%%.\n' \
            "$used_pct" "$free_pct" "$bar"
        printf '           This run CANNOT answer Q1 -- the limiting branch is never entered, and\n'
        printf '           Q2/Q3 will read NO for that reason rather than for the gap under test.\n'
        printf '           Raise TARGET_USED (>= %.2f would do it), or lower SWAPPINESS so the\n' \
            "$(awk -v b="$bar" 'BEGIN{printf "%.2f", (100-b)/100 + 0.01}')"
        printf '           kernel stops moving the workload out to swap behind the measurement.\n'
        printf '           Ctrl-C now is cheaper than reading the summary in %s more seconds.\n' \
            "$((RUNTIME - ${_SETTLE:-0}))"
    else
        printf '  settled at used_eff=%s%% (free %s%%), under the %s%% free that critical needs.\n' \
            "$used_pct" "$free_pct" "$bar"
        printf '  Q1 is answerable from this run.\n'
    fi
    echo
}

# Three of the four questions are about decisions smartune makes. With the daemon
# down the workload still runs, the tables still fill in, and every answer comes
# back "NO" -- a clean-looking reproduction of a gap that was never given a chance
# to not happen. Freshness of the log is the check, not a unit name: the daemon is
# started by hand here, so there is no unit to ask systemctl about.
check_daemon() {
    [ "${ALLOW_NO_DAEMON:-0}" = "1" ] && return 0
    local newest age
    if [ ! -r "$LOG" ]; then
        echo "REFUSING: no log at $LOG -- is smartune running?"
        echo "Set LOG=/path/to/multi_tasks_latest.log, or ALLOW_NO_DAEMON=1 to measure anyway."
        exit 1
    fi
    newest=$(grep -o '"ts": "[^"]*"' "$LOG" | tail -1 | sed 's/.*"ts": "//; s/"$//')
    age=$(( $(date +%s) - $(date -d "$newest" +%s 2>/dev/null || echo 0) ))
    if [ "$age" -gt 120 ]; then
        echo "REFUSING: newest log line is ${age}s old ($newest) -- smartune is not running."
        echo "Every answer would come back NO for the wrong reason. Start it, then re-run."
        echo "ALLOW_NO_DAEMON=1 overrides this if you only want the raw cgroup numbers."
        exit 1
    fi
}

print_environment() {
    echo
    echo "================================== environment ====================================="
    printf "  MemTotal %.0f MB | mode=%s | scopes=%d | per-scope %.0f MB (%s%% of MemTotal)\n" \
        "$((MEM_TOTAL / 1048576))" "$MODE" "$N" "$((PER_APP / 1048576))" "$PER_APP_PCT"
    echo "  swap devices now:"
    awk 'NR>1{printf "    %-40s %10d KB used %10d KB\n", $1, $3, $4}' /proc/swaps 2>/dev/null \
        || echo "    (none)"
    if [ -d "$SWAP_DIR" ]; then
        # 0700 root:root -- sudo or the listing is always empty, which reads as
        # "L0 did nothing" when in fact L0 may have grown swap all run.
        echo "  smartune-managed swapfiles in $SWAP_DIR:"
        sudo ls -la "$SWAP_DIR" 2>/dev/null | sed 's/^/    /' || echo "    (unreadable)"
    else
        echo "  $SWAP_DIR does not exist -- L0 never grew swap this run."
    fi
    echo "  OOM kills during the run:"
    # -T so the timestamps can be lined up against the timeline above. A kill here
    # invalidates the memory numbers after it: the box shed a workload we were
    # counting, so used_eff drops for a reason that has nothing to do with smartune.
    if dmesg -T 2>/dev/null | grep -iE 'oom-kill|Out of memory' | tail -10 | grep . ; then
        echo "    ^^ memory numbers after the first kill above describe a different machine."
    else
        echo "    (none, or dmesg needs root)"
    fi
    echo "===================================================================================="
}

# vm.swappiness is system-wide and survives this process, so putting it back is the
# one piece of cleanup that must not depend on anything else finishing. It runs
# FIRST in cleanup() for that reason: an earlier version restored it last, after
# the summary, and a `| head -60` on the command line killed the script on SIGPIPE
# partway through the summary -- leaving the machine permanently at SWAPPINESS with
# nothing on screen to say so. Idempotent via the unset, so the INT-then-EXIT path
# cannot run it twice.
restore_swappiness() {
    [ -n "${_SWAPPINESS_OLD:-}" ] || return 0
    local old="$_SWAPPINESS_OLD"
    unset _SWAPPINESS_OLD
    if sudo sysctl -q -w "vm.swappiness=${old}" 2>/dev/null; then
        echo "vm.swappiness restored to ${old}"
    else
        echo "WARNING: vm.swappiness is still ${SWAPPINESS}, NOT the ${old} this box had."
        echo "         Put it back with:  sudo sysctl -w vm.swappiness=${old}"
    fi
    return 0
}

_CLEANED=0
cleanup() {
    [ "$_CLEANED" = 1 ] && return 0   # EXIT may fire after an INT/TERM exit
    _CLEANED=1
    set +e   # the summary is the point of the run: no failing probe may skip it
    restore_swappiness
    sample_once
    [ -n "${LOOP_PID:-}" ] && kill "$LOOP_PID" 2>/dev/null
    summarize
    verdict
    print_environment
    print_log_evidence
    stop_scopes
    sudo rm -f "${BINS[@]}" 2>/dev/null
    rm -f "$SYS_CSV" "$CG_CSV" 2>/dev/null
    return 0
}

stop_scopes() {
    local s
    for s in "${SCOPES[@]}"; do
        sudo systemctl stop "${s}.scope" 2>/dev/null || true
    done
}
trap cleanup EXIT
trap 'exit 130' INT TERM
# Ignored, not handled: piping this script to `head` (or quitting a pager) closes
# stdout, and the default SIGPIPE would kill the shell mid-cleanup -- taking the
# vm.swappiness restore with it. Ignored, the failed write merely returns EPIPE,
# which cleanup's `set +e` absorbs.
trap '' PIPE

# --- run -------------------------------------------------------------------------------
if [ "$MODE" = "many" ]; then
    # Above 25% each scope crosses the threshold on its own and the run silently
    # becomes MODE=one with extra steps -- the opposite of what it is here to show.
    if awk -v p="$PER_APP_PCT" 'BEGIN{exit !(p >= 25)}'; then
        echo "REFUSING: per-scope ${PER_APP_PCT}% is at or above the 25% threshold."
        echo "Raise N or lower TARGET_USED so each scope stays well below it."
        exit 1
    fi
fi

check_daemon

# Set AFTER the refusals above and after `trap cleanup EXIT` is installed, so the
# restore in cleanup() is already armed the moment the value changes.
if [ "$SWAPPINESS" != "keep" ]; then
    _SWAPPINESS_OLD="$(cat /proc/sys/vm/swappiness)"
    if ! sudo sysctl -q -w "vm.swappiness=${SWAPPINESS}"; then
        # Nothing was changed, so restore_swappiness must not "restore" anything --
        # a write it cannot do either would print a second, misleading warning.
        _prev="$_SWAPPINESS_OLD"; unset _SWAPPINESS_OLD
        echo "REFUSING: could not set vm.swappiness=${SWAPPINESS}."
        echo "Run with SWAPPINESS=keep to measure at the current value (${_prev}) anyway,"
        echo "but expect the kernel to swap the workload out and hold the score below critical."
        exit 1
    fi
    echo "vm.swappiness ${_SWAPPINESS_OLD} -> ${SWAPPINESS} for this run (restored on exit)"
fi

for b in "${BINS[@]}"; do cp -f "$(command -v stress)" "$b"; done

printf 'MemTotal %s MB | mode=%s | %s scope(s) x %s MB (%s%% each)\n' \
    "$((MEM_TOTAL / 1048576))" "$MODE" "$N" "$((PER_APP / 1048576))" "$PER_APP_PCT"
printf 'used_eff %s%% now + %s MB allocated -> target %s%% | runtime %ss\n' \
    "$BASE_PCT" "$((TOTAL_BYTES / 1048576))" \
    "$(awk -v f="$TARGET_USED" 'BEGIN{printf "%.0f", 100*f}')" "$RUNTIME"

cat <<'STEPS'
--------------------------------------------------------------------------------
Do NOT register these processes in the Add-App wizard. Unregistered is the case
under test.

While it runs, the live line shows used_eff%, swap in use, memory PSI, how many
scopes currently carry a cap, and coverage (how much of the shortfall is held by
these scopes). What to watch for:

  * scopes_limited stays 0/N while used climbs past 85%  -> the gap, reproduced.
  * scopes_limited goes above 0                          -> L1 fired; note WHEN,
    and compare against the used% at that moment.
  * coverage well below 100%                             -> something outside
    these scopes holds the memory; read Q4 in the verdict before concluding
    anything about the candidate set.

On exit (end of run or Ctrl-C) the per-scope table, the system timeline, the
verdict, the environment and the log evidence all print. Ctrl-C early is fine --
everything measured up to that point is still summarised.
STEPS
# Outside the quoted heredoc so the control command carries THIS run's target: the
# comparison is only valid at the same TARGET_USED, and a hardcoded number here is
# how "MODE=one TARGET_USED=0.85" came to look like 0.85 was doing something when
# it was only restating the default.
printf '\nThen run the control and compare (same target, one scope instead of %s):\n' "$N"
printf '    MODE=one TARGET_USED=%s %s\n' "$TARGET_USED" "$0"
echo "--------------------------------------------------------------------------------"

echo
for i in $(seq 1 "$N"); do
    s="${SCOPES[$((i-1))]}"; b="${BINS[$((i-1))]}"
    # One vm worker per scope: more workers per scope would split the allocation
    # into several processes in the SAME cgroup, which changes nothing for the
    # cgroup totals but does make per-process attribution harder to read.
    sudo systemd-run --scope --unit="${s}.scope" \
        "$b" --vm 1 --vm-bytes "$PER_APP" --vm-hang "$VM_HANG" --timeout "$RUNTIME" \
        >/dev/null 2>&1 &
done

# Let the scopes exist before the first sample, otherwise the first interval has
# no baseline and the falling/flat split below starts one tick late.
for _ in $(seq 1 20); do
    [ -n "$(_cg_path "${SCOPES[0]}.scope")" ] && break
    sleep 0.5
done
sample_once
sample_loop & LOOP_PID=$!

# Long enough for every scope to fault its pages in AND for the kernel to finish
# whatever it is going to move to swap -- the level only settles once both are
# done, and on a 25 GB allocation that took ~2 minutes in practice. Half the
# runtime on a short run, so a quick RUNTIME=60 smoke test still gets the check.
_SETTLE=$(( RUNTIME < 120 ? RUNTIME / 2 : 60 ))
sleep "$_SETTLE"
check_target_reached

sleep $(( RUNTIME - _SETTLE ))
echo
echo "Run complete."
