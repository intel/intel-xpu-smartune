#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Regenerate the figures embedded in docs/pressure_model.md.

Dependency-free (pure-Python SVG, no matplotlib/numpy so it runs anywhere the
project does). Emits vector SVGs into docs/images/, which render inline on
GitHub and in most markdown viewers.

    python3 docs/pressure_model_figures.py

Each figure mirrors a formula in monitor/pressure.py or monitor/disk_pressure.py
so the doc stays in sync with the code -- if you tune a default there, re-run this.
"""
import math
import os

OUT = os.path.join(os.path.dirname(__file__), "images")

# ---- palette (light/dark neutral, colour-blind safe) ----------------------
AXIS = "#64748b"
GRID = "#e2e8f0"
TEXT = "#334155"
SERIES = ["#2563eb", "#e11d48", "#059669", "#d97706", "#7c3aed"]
BANDS = [("low", 0.0, "#f1f5f9"), ("medium", 0.4, "#fef9c3"),
         ("high", 0.6, "#fed7aa"), ("critical", 0.8, "#fecaca")]

W, H = 880, 440
ML, MR, MT, MB = 70, 250, 40, 55   # margins (wide right margin so legend text never clips)
PW, PH = W - ML - MR, H - MT - MB


def _x(v, xlo, xhi):
    return ML + (v - xlo) / (xhi - xlo) * PW


def _y(v, ylo, yhi):
    return MT + (1 - (v - ylo) / (yhi - ylo)) * PH


def plot(path, series, xlo, xhi, ylo, yhi, xlabel, ylabel, title,
         xticks, yticks, vlines=(), bands=None, notes=()):
    """series: list of (label, [(x,y),...], color[, opts]). Writes one SVG.

    opts may carry "w" (stroke width) and "dash". Dashing a series that another one
    lies exactly on top of is how a curve defined as the envelope of its inputs stays
    visible: the thick solid envelope shows through the gaps.
    """
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
         f'viewBox="0 0 {W} {H}" font-family="Segoe UI,Helvetica,Arial,sans-serif">']
    s.append(f'<rect width="{W}" height="{H}" fill="white"/>')
    s.append(f'<text x="{ML}" y="24" font-size="16" font-weight="600" fill="{TEXT}">{title}</text>')

    # horizontal threshold bands (optional)
    if bands:
        for i, (name, lo, col) in enumerate(bands):
            hi = bands[i + 1][1] if i + 1 < len(bands) else yhi
            if lo >= yhi:
                continue
            y0, y1 = _y(min(hi, yhi), ylo, yhi), _y(max(lo, ylo), ylo, yhi)
            s.append(f'<rect x="{ML}" y="{y0:.1f}" width="{PW}" height="{(y1-y0):.1f}" fill="{col}" opacity="0.6"/>')
            s.append(f'<text x="{ML+PW-6:.0f}" y="{(y0+12):.1f}" font-size="11" text-anchor="end" fill="{TEXT}">{name}</text>')

    # grid + ticks
    for xt in xticks:
        gx = _x(xt, xlo, xhi)
        s.append(f'<line x1="{gx:.1f}" y1="{MT}" x2="{gx:.1f}" y2="{MT+PH}" stroke="{GRID}"/>')
        s.append(f'<text x="{gx:.1f}" y="{MT+PH+18}" font-size="11" text-anchor="middle" fill="{TEXT}">{xt:g}</text>')
    for yt in yticks:
        gy = _y(yt, ylo, yhi)
        s.append(f'<line x1="{ML}" y1="{gy:.1f}" x2="{ML+PW}" y2="{gy:.1f}" stroke="{GRID}"/>')
        s.append(f'<text x="{ML-8}" y="{gy+4:.1f}" font-size="11" text-anchor="end" fill="{TEXT}">{yt:g}</text>')

    # axes
    s.append(f'<line x1="{ML}" y1="{MT+PH}" x2="{ML+PW}" y2="{MT+PH}" stroke="{AXIS}" stroke-width="1.5"/>')
    s.append(f'<line x1="{ML}" y1="{MT}" x2="{ML}" y2="{MT+PH}" stroke="{AXIS}" stroke-width="1.5"/>')
    s.append(f'<text x="{ML+PW/2:.0f}" y="{H-14}" font-size="13" text-anchor="middle" fill="{TEXT}">{xlabel}</text>')
    s.append(f'<text x="18" y="{MT+PH/2:.0f}" font-size="13" text-anchor="middle" fill="{TEXT}" '
             f'transform="rotate(-90 18 {MT+PH/2:.0f})">{ylabel}</text>')

    # vertical reference lines
    for vx, vlabel, vcol in vlines:
        gx = _x(vx, xlo, xhi)
        s.append(f'<line x1="{gx:.1f}" y1="{MT}" x2="{gx:.1f}" y2="{MT+PH}" stroke="{vcol}" '
                 f'stroke-width="1.3" stroke-dasharray="5 4"/>')
        s.append(f'<text x="{gx+4:.1f}" y="{MT+14}" font-size="11" fill="{vcol}">{vlabel}</text>')

    # series
    for entry in series:
        label, pts, col = entry[:3]
        opts = entry[3] if len(entry) > 3 else {}
        d = " ".join(("M" if i == 0 else "L") + f"{_x(x,xlo,xhi):.1f} {_y(y,ylo,yhi):.1f}"
                     for i, (x, y) in enumerate(pts))
        dash = f' stroke-dasharray="{opts["dash"]}"' if opts.get("dash") else ""
        s.append(f'<path d="{d}" fill="none" stroke="{col}" '
                 f'stroke-width="{opts.get("w", 2.4)}"{dash}/>')

    # legend (right gutter)
    lx, ly = ML + PW + 18, MT + 6
    for i, entry in enumerate(series):
        label, col = entry[0], entry[2]
        opts = entry[3] if len(entry) > 3 else {}
        dash = f' stroke-dasharray="{opts["dash"]}"' if opts.get("dash") else ""
        yy = ly + i * 22
        s.append(f'<line x1="{lx}" y1="{yy}" x2="{lx+22}" y2="{yy}" stroke="{col}" '
                 f'stroke-width="{max(3, opts.get("w", 3))}"{dash}/>')
        s.append(f'<text x="{lx+28}" y="{yy+4}" font-size="12" fill="{TEXT}">{label}</text>')
    for j, note in enumerate(notes):
        s.append(f'<text x="{lx}" y="{ly + len(series)*22 + 16 + j*16}" font-size="10.5" fill="{AXIS}">{note}</text>')

    s.append("</svg>")
    with open(os.path.join(OUT, path), "w") as f:
        f.write("\n".join(s))
    print("wrote", path)


# --------------------------------------------------------------------------
# model functions (copied 1:1 from monitor/pressure.py)
def scarcity(mem_avail, busy_pct=80, steepness=8.0):
    busy = busy_pct / 100.0
    center = max(1.0 - busy, 1e-3)
    x = (mem_avail - center) / center * steepness
    x = max(-60.0, min(60.0, x))
    return 1.0 / (1.0 + math.exp(x))


# ---- disk channel (monitor/disk_pressure.py + classify_disk_pressure) -----
def sig(x, x_half, k=8.0):
    """One USE sub-signal: 0.5 at the half-point, rising above it."""
    if x_half <= 0:
        return 0.0
    z = max(-60.0, min(60.0, -k * (x - x_half) / x_half))
    return 1.0 / (1.0 + math.exp(z))


def combined(p_busiest, n_disks, max_w=0.8):
    """noisy-OR of the mean and the worst disk. Idle disks only dilute the mean."""
    avg = p_busiest / n_disks
    return 1.0 - (1.0 - avg) * (1.0 - p_busiest * max_w)


def stall_severity(some, full, w_some=0.5, w_full=3.0):
    return min(1.0, some * w_some + full * w_full)


def disk_raw(comb, stall, low=0.4, high=0.8):
    """PSI fills disk_combined's remaining headroom, scaled by the saturation ramp."""
    sat = min(1.0, max(0.0, (comb - low) / (high - low)))
    return min(1.0, comb + (1.0 - comb) * stall * sat)


def main():
    os.makedirs(OUT, exist_ok=True)
    XS = [i / 200 for i in range(201)]  # available_ratio 0..1

    # 1. memory scarcity gate -- steepness sweep
    plot("mem_gate_steepness.svg",
         [(f"k = {k}", [(x, scarcity(x, 80, k)) for x in XS], SERIES[i])
          for i, k in enumerate([2, 4, 8, 16])],
         0, 1, 0, 1,
         "free-memory ratio (available_ratio)", "mem_scarcity  (memory contribution)",
         "Memory scarcity gate — effect of mem_gate_steepness (busy=80%)",
         [0, 0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0], [0, 0.25, 0.5, 0.75, 1.0],
         vlines=[(0.2, "busy point (center)", "#0f172a")],
         notes=["higher k -> sharper switch", "at center scarcity = 0.5"])

    # 2. memory scarcity gate -- busy-threshold sweep (center shift)
    plot("mem_gate_busy_threshold.svg",
         [(f"busy = {b}%  (center={round(1-b/100,2)})",
           [(x, scarcity(x, b, 8)) for x in XS], SERIES[i])
          for i, b in enumerate([70, 80, 90])],
         0, 1, 0, 1,
         "free-memory ratio (available_ratio)", "mem_scarcity",
         "Memory scarcity gate — effect of memory_busy_threshold (k=8)",
         [0, 0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0], [0, 0.25, 0.5, 0.75, 1.0],
         notes=["bigger busy% -> curve shifts left", "(tolerate less free RAM)"])

    # 3. CPU/IO self-discount gate: effective PSI vs self_fraction
    def eff(frac, gate, psi=0.8):
        return psi * (1 - frac * gate)
    FR = [i / 100 for i in range(101)]
    plot("cpu_io_self_gate.svg",
         [(f"gate = {g}", [(f, eff(f, g)) for f in FR], SERIES[i])
          for i, g in enumerate([0.0, 0.5, 0.7, 1.0])],
         0, 1, 0, 0.85,
         "self_fraction (share of stall caused by the limited app)",
         "psi_eff  (kept CPU/IO pressure, psi=0.80)",
         "CPU/IO self-inflicted discount — effect of _CPU_IO_SELF_GATE",
         [0, 0.25, 0.5, 0.75, 1.0], [0, 0.2, 0.4, 0.6, 0.8],
         notes=["gate=0: keep all (no discount)", "gate=1: remove all self-share",
                "0.7: keep 30% of self-share"])

    # 4. EWMA step response (fast-attack handled separately in code)
    def ewma_series(alpha, signal):
        s, out = None, []
        for i, x in enumerate(signal):
            s = x if s is None else alpha * x + (1 - alpha) * s
            out.append((i, s))
        return out
    # noisy signal: step 0.2 -> 0.7 at t=10, with +-0.12 square noise
    noisy = []
    for t in range(40):
        base = 0.2 if t < 10 else 0.7
        noisy.append(base + (0.12 if t % 2 else -0.12))
    series4 = [("raw (noisy)", [(t, v) for t, v in enumerate(noisy)], "#cbd5e1")]
    for i, a in enumerate([0.1, 0.3, 0.5]):
        series4.append((f"alpha = {a}", ewma_series(a, noisy), SERIES[i]))
    plot("ewma_smoothing.svg", series4, 0, 39, 0, 0.9,
         "tick (~5 s each)", "smoothed value",
         "EWMA smoothing — effect of alpha (step + noise)",
         [0, 10, 20, 30, 39], [0, 0.2, 0.4, 0.6, 0.8],
         vlines=[(10, "step", "#0f172a")],
         notes=["smaller alpha -> smoother, slower", "larger alpha -> faster, noisier",
                "code uses alpha=0.3"])

    # 5. final score vs memory used% : the max(load, scarcity) channel
    #    hold cpu/io fixed, sweep memory usage; weights 1:8:1
    def final_score(used_pct, cpu_eff=0.4, io_eff=0.35, w=(1, 8, 1)):
        avail = 1 - used_pct / 100
        sc = scarcity(avail, 80, 8)
        load = (w[0] * cpu_eff + w[1] * sc + w[2] * io_eff) / sum(w)
        return min(max(load, sc), 1.0)
    US = [u / 1 for u in range(0, 101)]
    # The final score IS the pointwise max of its two inputs, so a plain solid line for it
    # is hidden under whichever input is currently winning. Draw the inputs dashed and the
    # score thick: it then reads as the envelope, visible through both dash patterns.
    plot("score_vs_memory.svg",
         [("final score", [(u, final_score(u)) for u in US], SERIES[0], {"w": 3.6}),
          ("mem_scarcity (floor)", [(u, scarcity(1 - u / 100, 80, 8)) for u in US],
           SERIES[1], {"w": 1.8, "dash": "7 5"}),
          ("load (normalized avg)",
           [(u, (1 * 0.4 + 8 * scarcity(1 - u / 100, 80, 8) + 1 * 0.35) / 10) for u in US],
           SERIES[2], {"w": 1.8, "dash": "2 4"})],
         0, 100, 0, 1.0,
         "memory used (%)", "score",
         "Final score vs memory usage (weights 1:8:1)",
         [0, 20, 40, 60, 80, 100], [0, 0.4, 0.6, 0.8, 1.0],
         vlines=[(80, "busy 80%", "#0f172a")],
         bands=BANDS,
         notes=["fixed: cpu_eff=0.40, io_eff=0.35",
                "score = min(max(load, scarcity), 1)",
                "score follows load, then the floor",
                "cpu/io-only stays LOW; OOM -> CRITICAL"])

    # 6. level hysteresis on a hovering score
    #    demonstrate up-threshold vs down-threshold on the medium/low boundary
    hov = []
    v = 0.5
    seq = [0.5, 0.62, 0.58, 0.63, 0.57, 0.61, 0.56, 0.59, 0.54, 0.5, 0.44, 0.5, 0.62]
    plot("level_hysteresis.svg",
         [("smoothed score", [(i, s) for i, s in enumerate(seq)], SERIES[0])],
         0, len(seq) - 1, 0.3, 0.75,
         "tick", "smoothed score",
         "Level hysteresis — enter medium at 0.60, drop to low only below 0.55",
         list(range(0, len(seq))), [0.4, 0.55, 0.6, 0.75],
         vlines=[],
         notes=["up threshold = 0.60 (enter medium)",
                "down threshold = 0.55 (0.60 - 0.05)",
                "in [0.55, 0.60): keep prior level",
                "=> no chatter at the boundary"])
    # draw the two hysteresis lines by overlaying (re-open file)
    _overlay_hlines("level_hysteresis.svg", [(0.60, "#e11d48", "medium entry 0.60"),
                                             (0.55, "#059669", "exit 0.55")],
                    0, len(seq) - 1, 0.3, 0.75)

    # 7. media-aware half-points: the same sigmoid, one per media class
    AW = [i / 10 for i in range(0, 401)]  # await 0..40 ms
    plot("disk_media_sigmoid.svg",
         [(f"{m} (half = {h:g} ms)", [(a, sig(a, h)) for a in AW], SERIES[i])
          for i, (m, h) in enumerate([("nvme", 1.0), ("sata_ssd", 5.0),
                                      ("hdd", 20.0), ("usb", 30.0)])],
         0, 40, 0, 1.0,
         "await (ms)", "latency sub-signal  f(await)",
         "Media-aware half-points — same sigmoid (k=8), one pain point per media class",
         [0, 1, 5, 10, 20, 30, 40], [0, 0.25, 0.5, 0.75, 1.0],
         notes=["0.5 at the half-point",
                "20 ms: routine for an HDD,",
                "catastrophic for an NVMe",
                "queue/util use the same shape"])

    # 8. noisy-OR aggregate: how much an idle-disk majority dilutes one hammered disk
    PS = [i / 200 for i in range(201)]
    plot("disk_noisy_or.svg",
         [(f"{n} disk{'s' if n > 1 else ''} ({n-1} idle)",
           [(p, combined(p, n)) for p in PS], SERIES[i])
          for i, n in enumerate([1, 2, 3, 4])],
         0, 1, 0, 1.0,
         "P_disk of the busiest disk", "disk_combined",
         "Aggregation — noisy-OR of mean and worst disk (max_p_weight=0.8)",
         [0, 0.2, 0.4, 0.6, 0.8, 1.0], [0, 0.4, 0.6, 0.8, 1.0],
         bands=BANDS,
         notes=["combined = 1-(1-avg)(1-max*0.8)",
                "idle disks dilute the mean only",
                "3 disks, busiest 0.91 -> 0.81",
                "raise max_p_weight to weigh",
                "the worst disk more"])

    # 9. the PSI gate: saturation and stall both required
    ST = [i / 200 for i in range(201)]
    plot("disk_gate.svg",
         [(f"disk_combined = {c}", [(t, disk_raw(c, t)) for t in ST], SERIES[i])
          for i, c in enumerate([0.5, 0.65, 0.8, 0.9])],
         0, 1, 0, 1.05,
         "stall severity (from io PSI)", "gated disk score (raw)",
         "Disk gate — raw = combined + (1-combined) x stall x sat",
         [0, 0.25, 0.5, 0.75, 1.0], [0, 0.4, 0.6, 0.8, 1.0],
         bands=BANDS,
         notes=["sat ramps 0->1 over [low, high]",
                "= [0.4, 0.8] by default",
                "saturated but no stall -> high",
                "stall but not saturated -> low",
                "critical needs both maxed"])

    # 10. stall severity from the two io PSI terms
    FU = [i / 500 for i in range(251)]  # io.full 0..0.5
    plot("disk_psi_stall.svg",
         [(f"io.some = {s}", [(f, stall_severity(s, f)) for f in FU], SERIES[i])
          for i, s in enumerate([0.2, 0.5, 1.0])],
         0, 0.5, 0, 1.05,
         "io PSI `full` (all non-idle tasks blocked on IO)", "stall severity",
         "Stall severity — stall = min(1, 0.5 x some + 3.0 x full)",
         [0, 0.1, 0.2, 0.3, 0.4, 0.5], [0, 0.25, 0.5, 0.75, 1.0],
         vlines=[(1 / 3, "full = 1/3", "#0f172a")],
         notes=["`full` dominates (weight 3.0)",
                "`some` alone caps at 0.5, so it",
                "can never saturate the stall",
                "=> critical needs full >= ~1/3"])


def _overlay_hlines(path, lines, xlo, xhi, ylo, yhi):
    p = os.path.join(OUT, path)
    with open(p) as f:
        svg = f.read()
    ins = []
    for yv, col, label in lines:
        gy = _y(yv, ylo, yhi)
        ins.append(f'<line x1="{ML}" y1="{gy:.1f}" x2="{ML+PW}" y2="{gy:.1f}" stroke="{col}" '
                   f'stroke-width="1.3" stroke-dasharray="6 4"/>')
        ins.append(f'<text x="{ML+6}" y="{gy-4:.1f}" font-size="11" fill="{col}">{label}</text>')
    svg = svg.replace("</svg>", "\n".join(ins) + "\n</svg>")
    with open(p, "w") as f:
        f.write(svg)
    print("overlaid", path)


if __name__ == "__main__":
    main()
