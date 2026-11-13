#!/usr/bin/env python3
"""Looks at sigcap.py's recordings: which signals are there, how wide, when they key up, and what kind of modulation.
Writes analysis.json and a spectrogram PNG per recording. Usage: sigan.py DIR"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

NFFT = 4096
ROW_S = 0.05

d = Path(sys.argv[1])
caps = json.loads((d / "captures.json").read_text())
stops = np.array([[8, 12, 28], [14, 40, 78], [22, 92, 140], [60, 160, 196], [150, 218, 232], [240, 252, 255]], np.float32)
lut = np.stack([np.interp(np.linspace(0, 1, 256), np.linspace(0, 1, 6), stops[:, c]) for c in range(3)], 1).astype(np.uint8)


def spectrogram(path, rate):
    per_row = int(rate * ROW_S)
    win = np.hanning(NFFT).astype(np.float32)
    rows = []
    with open(path, "rb") as f:
        while True:
            buf = np.frombuffer(f.read(per_row * 2), np.int8)
            if len(buf) < NFFT * 2:
                break
            iq = buf[0::2].astype(np.float32) + 1j * buf[1::2].astype(np.float32)
            # a handful of FFTs per row is enough to see bursts and keeps this quick on the Pi
            starts = np.linspace(0, len(iq) - NFFT, 8).astype(int)
            spec = np.mean([np.abs(np.fft.fft(iq[s:s + NFFT] * win)) ** 2 for s in starts], axis=0)
            rows.append(10 * np.log10(np.fft.fftshift(spec) + 1e-6))
    return np.array(rows, np.float32)


def modulation(path, rate, center, f0, bw, rows_on):
    """Demodulates the strongest stretch of one signal and describes its frequency and amplitude behaviour."""
    if not len(rows_on):
        return {}
    per_row = int(rate * ROW_S)
    row = int(rows_on[len(rows_on) // 2])
    with open(path, "rb") as f:
        f.seek(row * per_row * 2)
        buf = np.frombuffer(f.read(per_row * 2 * 4), np.int8)
    iq = buf[0::2].astype(np.float32) + 1j * buf[1::2].astype(np.float32)
    t = np.arange(len(iq)) / rate
    iq = iq * np.exp(-2j * np.pi * (f0 - center) * t)
    # low-pass to the signal's width, then decimate to about 8 samples per narrowest symbol
    width = max(bw, 6e3)
    taps = np.sinc(2 * (width / 2 * 1.3) / rate * (np.arange(401) - 200)) * np.hamming(401)
    y = np.convolve(iq, taps / taps.sum(), mode="same")
    step = max(1, int(rate // (width * 4)))
    y = y[::step]
    out_rate = rate / step
    env = np.abs(y)
    on = env > 0.5 * np.median(env[env > np.percentile(env, 70)])
    inst = np.angle(y[1:] * np.conj(y[:-1])) * out_rate / (2 * np.pi)
    inst = inst[on[1:]]
    if len(inst) < 200:
        return {}
    inst = inst - np.median(inst)
    hist, edges = np.histogram(np.clip(inst, -width, width), bins=60)
    smooth = np.convolve(hist, np.ones(3) / 3, mode="same")
    peaks = [i for i in range(1, 59) if smooth[i] > smooth[i - 1] and smooth[i] >= smooth[i + 1] and smooth[i] > 0.25 * smooth.max()]
    levels = [round(float((edges[i] + edges[i + 1]) / 2)) for i in peaks]
    # symbol rate: strongest line in the spectrum of |d(inst)/dt|
    tr = np.abs(np.diff(inst))
    spec = np.abs(np.fft.rfft(tr - tr.mean(), n=1 << 16))
    freqs = np.fft.rfftfreq(1 << 16, 1 / out_rate)
    ok = (freqs > 500) & (freqs < width * 1.2)
    baud = float(freqs[ok][np.argmax(spec[ok])]) if ok.any() else None
    env_on = env[on]
    return {"fm_levels_hz": levels, "deviation_hz": round(float(np.percentile(np.abs(inst), 95))),
            "envelope_spread": round(float(np.std(env_on) / (np.mean(env_on) + 1e-9)), 2),
            "symbol_rate_guess": round(baud) if baud else None}


report = []
for cap in caps:
    if not cap["ok"]:
        continue
    path = d / f"{cap['name']}.cs8"
    rate, center = cap["rate"], cap["center_mhz"] * 1e6
    w = spectrogram(path, rate)
    freqs = center + np.fft.fftshift(np.fft.fftfreq(NFFT, 1 / rate))
    floor = np.median(w)
    maxhold = w.max(axis=0)
    # ignore the HackRF's DC spike and the filter skirts
    usable = (np.abs(freqs - center) > rate * 0.004) & (np.abs(freqs - center) < rate * 0.42)
    hot = usable & (maxhold > floor + 12)
    signals, i = [], 0
    idx = np.flatnonzero(hot)
    groups = np.split(idx, np.flatnonzero(np.diff(idx) > 3) + 1) if len(idx) else []
    for g in groups:
        if len(g) < 2:
            continue
        peak_i = g[np.argmax(maxhold[g])]
        top = maxhold[peak_i]
        edge = g[maxhold[g] > top - 10]
        bw = (edge[-1] - edge[0] + 1) * rate / NFFT
        band = w[:, g].max(axis=1)
        on = band > floor + 10
        rows_on = np.flatnonzero(on)
        bursts = np.split(rows_on, np.flatnonzero(np.diff(rows_on) > 1) + 1) if len(rows_on) else []
        lengths = [len(b) * ROW_S for b in bursts]
        starts = [b[0] * ROW_S for b in bursts]
        sig = {"mhz": round(float(freqs[peak_i]) / 1e6, 4), "width_khz": round(bw / 1e3, 1),
               "above_floor_db": round(float(top - floor), 1), "on_pct": round(100 * float(on.mean()), 1),
               "bursts": len(bursts), "burst_s_median": round(float(np.median(lengths)), 2) if lengths else None,
               "burst_starts_s": [round(s, 2) for s in starts[:40]]}
        if len(signals) < 6:
            sig.update(modulation(path, rate, center, float(freqs[peak_i]), bw, rows_on))
        signals.append(sig)
    signals.sort(key=lambda s: -s["above_floor_db"])
    report.append({"name": cap["name"], "center_mhz": cap["center_mhz"], "rate": rate, "seconds": cap["seconds"],
                   "started": cap["started"], "floor_db": round(float(floor), 1), "signals": signals[:12]})
    keep = usable
    img = w[:, keep]
    if img.shape[1] > 1200:
        f = img.shape[1] // 1200 + 1
        img = img[:, : img.shape[1] // f * f].reshape(img.shape[0], -1, f).max(axis=2)
    if img.shape[0] > 900:
        f = img.shape[0] // 900 + 1
        img = img[: img.shape[0] // f * f].reshape(-1, f, img.shape[1]).max(axis=1)
    v = np.clip((img - floor - 3) / 30, 0, 1)
    Image.fromarray(lut[(v ** 0.8 * 255).astype(np.uint8)]).save(d / f"{cap['name']}.png", optimize=True)
    report[-1]["png_mhz"] = [round(float(freqs[keep][0]) / 1e6, 4), round(float(freqs[keep][-1]) / 1e6, 4)]
    print(json.dumps(report[-1]), flush=True)
(d / "analysis.json").write_text(json.dumps(report))
