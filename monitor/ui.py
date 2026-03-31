# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import curses
import time
from collections import deque
from typing import Iterable, List, Dict

from .types import MetricSample

MAX_HISTORY = 240
PLOT_HEIGHT = 6

class HistoryBuffer:
    def __init__(self, maxlen: int = MAX_HISTORY):
        self.data: Dict[str, deque] = {}
        self.maxlen = maxlen

    def update(self, samples: Iterable[MetricSample]):
        for sample in samples:
            for k, v in sample.metrics.items():
                if isinstance(v, (int, float)):
                    key = f"{sample.name}_{k}"
                    if key not in self.data:
                        self.data[key] = deque([0.0] * self.maxlen, maxlen=self.maxlen)
                    self.data[key].append(float(v))

    def get_last(self, sample_name: str, metric_key: str) -> List[float]:
        key = f"{sample_name}_{metric_key}"
        return list(self.data[key]) if key in self.data else [0.0] * self.maxlen

buffer = HistoryBuffer(MAX_HISTORY)

class BrailleCanvas:
    def __init__(self, rows: int, cols: int):
        self.rows = rows
        self.cols = cols
        self.grid: Dict[tuple[int, int], int] = {}

    def set_pixel(self, x: int, y: int):
        if x < 0 or y < 0: 
            return
        char_x = x // 2
        char_y = y // 4
        if char_x >= self.cols or char_y >= self.rows: 
            return
        dx = x % 2
        dy = y % 4
        braille_row = 3 - dy
        masks = [[0x1, 0x8], [0x2, 0x10], [0x4, 0x20], [0x40, 0x80]]
        mask = masks[braille_row][dx]
        k = (char_y, char_x)
        self.grid[k] = self.grid.get(k, 0) | mask

    def get_char(self, r: int, c: int) -> str:
        mask = self.grid.get((r, c), 0)
        return chr(0x2800 + mask)

def draw_widget(stdscr, y, x, h, w, title, data, max_val, is_proc=False, proc_text=""):
    if w < 4 or h < 4: 
        return
    try:
        stdscr.attron(curses.color_pair(1))
        # Render box borders 
        stdscr.addstr(y, x, "┌" + "─" * (w - 2) + "┐")
        for i in range(1, h - 1):
            stdscr.addstr(y + i, x, "│")
            stdscr.addstr(y + i, x + w - 1, "│")
        stdscr.addstr(y + h - 1, x, "└" + "─" * (w - 2) + "┘")
        
        # Render widget title
        clean_title = f" {title} "
        max_title_len = w - 2
        if max_title_len > 0:
            stdscr.addstr(y, x + 2, clean_title[:max_title_len])

        if is_proc:
            lines = proc_text.split('\n')
            for i, line in enumerate(lines):
                if i < h - 2:
                    safe_len = w - 4
                    if safe_len > 0:
                        stdscr.addstr(y + 1 + i, x + 2, line[:safe_len])
            return

        chart_h = h - 3
        chart_w = w - 10
        if chart_h < 1 or chart_w < 1: 
            return

        for i in range(chart_h):
            val = (max_val * (chart_h - 1 - i)) / max(1, chart_h - 1)
            stdscr.addstr(y + 1 + i, x + 1, f"{val:>6.1f}"[:7])

        canvas = BrailleCanvas(chart_h, chart_w)
        px_height = chart_h * 4
        px_width = chart_w * 2
        plot_data = data[-px_width:]
        prev_py = None
        
        for i, val in enumerate(plot_data):
            ratio = val / max_val if max_val > 0 else 0
            py = int(ratio * (px_height - 1))
            py = max(0, min(py, px_height - 1))
            px = i
            canvas.set_pixel(px, py)
            if prev_py is not None:
                start, end = min(prev_py, py), max(prev_py, py)
                for k in range(start, end):
                    canvas.set_pixel(px, k)
            prev_py = py

        start_y = y + 1
        start_x = x + 9
        for r in range(chart_h):
            canvas_row = chart_h - 1 - r
            line_str = ""
            for c in range(chart_w):
                line_str += canvas.get_char(canvas_row, c)
            stdscr.addstr(start_y + r, start_x, line_str)

        footer_y = y + h - 2
        for t in range(0, chart_w, 10):
             if start_x + t < x + w - 1:
                stdscr.addstr(footer_y, start_x + t, f"{t}")

    except curses.error:
        pass

def render_console(samples: Iterable[MetricSample], stdscr) -> None:
    sh, sw = stdscr.getmaxyx()
    stdscr.erase()
    s_list = list(samples)
    buffer.update(s_list)

    def gv(name, key, default=0.0):
        for s in s_list:
            if s.name == name: return s.metrics.get(key, default)
        return default

    proc_h = 5 
    chart_total_h = sh - proc_h
    box_h = chart_total_h // 3
    box_w = sw // 2

    plots = [
        ("CPU使用率", gv("CPU", "cpu_percent"), "%", buffer.get_last("CPU", "cpu_percent"), 100.0),
        ("MEM使用量", gv("MEM", "system_used_mb")/1024, "GB", [v/1024 for v in buffer.get_last("MEM", "system_used_mb")], 16.0),
        ("iGPU利用率", gv("iGPU", "utilization"), "%", buffer.get_last("iGPU", "utilization"), 100.0),
        ("NPU利用率", gv("NPU", "usage_percent"), "%", buffer.get_last("NPU", "usage_percent"), 100.0),
        ("iGPU频率", gv("iGPU", "frequency_mhz"), "MHz", buffer.get_last("iGPU", "frequency_mhz"), 2250.0),
        ("NPU频率", gv("NPU", "npu_current_frequency"), "MHz", buffer.get_last("NPU", "npu_current_frequency"), 1600.0)
    ]

    for i, cfg in enumerate(plots):
        r, c = i // 2, i % 2
        draw_widget(stdscr, r*box_h, c*box_w, box_h, box_w, f"{cfg[0]} {cfg[1]:.2f}{cfg[2]}", cfg[3], cfg[4])

    bottom_y = box_h * 3
    actual_proc_h = sh - bottom_y
    proc_w = sw // 3
    
    if actual_proc_h >= 3:
        # CPU
        draw_widget(stdscr, bottom_y, 0, actual_proc_h, proc_w, "CPU 进程", [], 0, True, gv("CPU", "top_proc", ""))
        # iGPU
        draw_widget(stdscr, bottom_y, proc_w, actual_proc_h, proc_w, "iGPU 进程", [], 0, True, gv("iGPU", "top_proc", ""))
        # NPU 
        last_w = sw - (proc_w * 2) - 1
        if last_w > 4:
            draw_widget(stdscr, bottom_y, proc_w * 2, actual_proc_h, last_w, "NPU 进程", [], 0, True, gv("NPU", "top_proc", ""))

    stdscr.refresh()

def display_loop(get_samples, *, interval_s: float) -> int:
    def main_loop(stdscr):
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            try: 
                curses.init_pair(1, -1, -1)
            except Exception:
                pass
            stdscr.bkgd(' ', curses.color_pair(1))
        
        curses.curs_set(0)
        stdscr.nodelay(True)
        
        while True:
            samples = get_samples()
            render_console(samples, stdscr)
            try:
                if stdscr.getch() == ord('q'): 
                    break
            except Exception:
                pass
            time.sleep(max(0.05, float(interval_s)))
            
    try:
        curses.wrapper(main_loop)
    except KeyboardInterrupt:
        pass
    return 0