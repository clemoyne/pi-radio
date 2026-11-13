#!/usr/bin/env python3
"""Overnight band survey: runs hackrf_sweep across F_LOW-F_HIGH and keeps, for every BIN_HZ slice, each minute's peak
and mean power. Writes peak.f16 and mean.f16 (one row of float16 per minute) and minutes.txt (the row times) to OUT.
Usage: sweep.py OUT_DIR"""
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

F_LOW_MHZ, F_HIGH_MHZ = 24, 1800
BIN_HZ = 100_000
GAINS = ["-l", "32", "-g", "30"]  # LNA and VGA; amp off so strong local signals don't swamp the sweep

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
start_hz = F_LOW_MHZ * 1_000_000
count = (F_HIGH_MHZ - F_LOW_MHZ) * 1_000_000 // BIN_HZ
(out / "bins.txt").write_text(f"start_hz={start_hz} bin_hz={BIN_HZ} count={count}\n")


def fresh():
    return np.full(count, -200.0, np.float32), np.zeros(count, np.float64), np.zeros(count, np.int32)


peak, total, n = fresh()
minute = int(time.time() // 60)
cmd = ["hackrf_sweep", "-f", f"{F_LOW_MHZ}:{F_HIGH_MHZ}", "-w", str(BIN_HZ)] + GAINS
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1 << 20)
sweeps = 0
for line in proc.stdout:
    parts = line.split(",")
    if len(parts) < 7:
        continue
    low = int(parts[2])
    values = np.array(parts[6:], dtype=np.float32)
    first = (low - start_hz) // BIN_HZ
    if first < 0 or first + len(values) > count:
        values = values[max(0, -first): count - first]
        first = max(0, first)
    if not len(values):
        continue
    sl = slice(first, first + len(values))
    np.maximum(peak[sl], values, out=peak[sl])
    total[sl] += values
    n[sl] += 1
    if first == 0:
        sweeps += 1
    now_minute = int(time.time() // 60)
    if now_minute != minute:
        mean = np.where(n > 0, total / np.maximum(n, 1), -200.0)
        with open(out / "peak.f16", "ab") as f:
            f.write(peak.astype(np.float16).tobytes())
        with open(out / "mean.f16", "ab") as f:
            f.write(mean.astype(np.float16).tobytes())
        with open(out / "minutes.txt", "a") as f:
            f.write(f"{minute * 60} {sweeps}\n")
        peak, total, n = fresh()
        minute, sweeps = now_minute, 0
print("hackrf_sweep exited", proc.wait(), flush=True)
