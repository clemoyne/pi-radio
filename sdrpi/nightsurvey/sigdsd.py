#!/usr/bin/env python3
"""Identifies the digital systems in sigcap.py's recordings with DSD-FME (system type, network and site codes; no voice
is kept and encrypted traffic stays undecoded), and reads a Morse ID if asked.
Usage: sigdsd.py DIR NAME:MHZ[,MHZ...] [--morse NAME:MHZ:START_S:SECONDS]"""
import json
import re
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

DSD = "/mnt/data/scanner/decoders/bin/dsd-fme"
OUT_RATE = 48_000
GAIN = 42_400  # the scanner's discriminator scaling, which DSD-FME is tuned to
KEEP = re.compile(r"P25|DMR|NXDN|dPMR|EDACS|ProVoice|X2|YSF|DSTAR|M17|NAC|WACN|SYS|Site|RFSS|Color|CC |RAN|TGT|SRC|"
                  r"Group|Encrypt|ALG|Talkgroup|Net|Channel|Freq|Sync", re.I)


def channel(path, rate, center, mhz, seconds=None, start=0.0, width=12_500, out_rate=OUT_RATE):
    """Complex baseband of one channel at out_rate."""
    raw = np.fromfile(path, np.int8, count=int((start + seconds) * rate * 2) if seconds else -1)[int(start * rate) * 2:]
    iq = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    iq *= np.exp(-2j * np.pi * (mhz * 1e6 - center) / rate * np.arange(len(iq))).astype(np.complex64)
    step = rate // 50_000  # to 50 kS/s
    taps = np.sinc(2 * (width / 2) / rate * (np.arange(-300, 301))) * np.hamming(601)
    taps = (taps / taps.sum()).astype(np.float32)
    y = np.convolve(iq, taps, mode="same")[::step]
    t_in = np.arange(len(y)) / (rate / step)
    t_out = np.arange(0, t_in[-1], 1 / out_rate)
    return np.interp(t_out, t_in, y.real) + 1j * np.interp(t_out, t_in, y.imag)


def discriminator(y):
    return np.angle(y[1:] * np.conj(y[:-1]))


def dsd(path, rate, center, mhz, work):
    disc = discriminator(channel(path, rate, center, mhz))
    pcm = np.clip(disc * GAIN, -32767, 32767).astype(np.int16)
    wav = work / f"{mhz:.4f}.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(pcm.tobytes())
    try:
        r = subprocess.run([DSD, "-fa", "-i", str(wav), "-o", "null", "-n", "0"], capture_output=True, text=True,
                           errors="replace", timeout=240, cwd=work)
        text = r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        text = (e.stdout or b"").decode(errors="replace") + (e.stderr or b"").decode(errors="replace")
    wav.unlink()
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l and KEEP.search(l)]
    counts = {}
    for l in lines:
        key = re.sub(r"\d{2}:\d{2}:\d{2}\s*", "", l)[:120]
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:25]


MORSE = {".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F", "--.": "G", "....": "H", "..": "I",
         ".---": "J", "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
         "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y", "--..": "Z",
         "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4", ".....": "5", "-....": "6",
         "--...": "7", "---..": "8", "----.": "9", "-..-.": "/"}


def morse(path, rate, center, mhz, start, seconds):
    """Keyed tone (on FM audio) or keyed carrier: whichever shows a clear on/off pattern, read as Morse."""
    y = channel(path, rate, center, mhz, seconds=seconds + start, start=start, width=6_000, out_rate=8_000)
    results = {}
    audio = discriminator(y)
    for kind, sig in (("tone", audio - np.mean(audio)), ("carrier", np.abs(y))):
        frame = 80  # 10 ms
        n = len(sig) // frame
        if kind == "tone":
            # strongest audio tone, then its level per 10 ms
            spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
            f = np.fft.rfftfreq(len(sig), 1 / 8000)
            band = (f > 300) & (f < 2500)
            tone = f[band][np.argmax(spec[band])]
            lo = np.exp(-2j * np.pi * tone * np.arange(len(sig)) / 8000) * sig
            env = np.abs(lo[: n * frame].reshape(n, frame).mean(axis=1))
        else:
            env = sig[: n * frame].reshape(n, frame).mean(axis=1)
        on = env > (np.percentile(env, 20) + np.percentile(env, 90)) / 2
        edges = np.flatnonzero(np.diff(on.astype(int))) + 1
        runs = np.split(on, edges)
        marks = [len(r) for r in runs if r[0]]
        if len(marks) < 4:
            continue
        unit = np.percentile(marks, 25)
        text, letter = "", ""
        for r in runs:
            length = len(r) / unit
            if r[0]:
                letter += "." if length < 2 else "-"
            elif length >= 2:
                text += MORSE.get(letter, "?") if letter else ""
                letter = ""
                if length >= 5:
                    text += " "
        text += MORSE.get(letter, "?") if letter else ""
        results[kind] = {"text": text.strip(), "unit_ms": round(float(unit) * 10), "marks": len(marks),
                         "tone_hz": round(float(tone)) if kind == "tone" else None}
    return results


d = Path(sys.argv[1])
caps = {c["name"]: c for c in json.loads((d / "captures.json").read_text())}
out = {}
args = sys.argv[2:]
for arg in args:
    if arg.startswith("--morse="):
        name, mhz, start, seconds = arg.split("=", 1)[1].split(":")
        c = caps[name]
        out[f"morse {mhz}"] = morse(d / f"{name}.cs8", c["rate"], c["center_mhz"] * 1e6, float(mhz), float(start),
                                    float(seconds))
        print(f"morse {mhz}", json.dumps(out[f"morse {mhz}"]), flush=True)
        continue
    name, freqs = arg.split(":")
    c = caps[name]
    for mhz in map(float, freqs.split(",")):
        out[f"{mhz:.4f}"] = dsd(d / f"{name}.cs8", c["rate"], c["center_mhz"] * 1e6, mhz, d)
        print(f"== {mhz:.4f}", flush=True)
        for line, n in out[f"{mhz:.4f}"]:
            print(f"   {n:4} {line}", flush=True)
(d / "dsd.json").write_text(json.dumps(out))
