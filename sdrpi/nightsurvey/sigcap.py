#!/usr/bin/env python3
"""Short IQ recordings of unidentified signals from the night survey, for analysis afterwards.
Takes the HackRF from the scanner (unless someone is listening), records each target, then hands it back.
Usage: sigcap.py OUT_DIR"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# name, centre MHz, sample rate, seconds; each L-band target twice, 2.5 MHz apart, to tell real signals from HackRF spurs
TARGETS = [
    ("410", 410.55, 2_000_000, 150),  # the :02/:32 burst at 410.05
    ("422", 422.90, 2_000_000, 30),  # 422.2-422.6, on 80% of the time
    ("413", 413.45, 2_000_000, 30),  # 412.96 and 413.46-413.59, also near constant
    ("937", 937.25, 2_000_000, 30),  # 936.75
    ("923", 923.85, 2_000_000, 40),  # 923.35 bursts
    ("1508a", 1508.0, 8_000_000, 15),
    ("1508b", 1510.5, 8_000_000, 15),
    ("1430a", 1430.0, 8_000_000, 15),
    ("1430b", 1432.5, 8_000_000, 15),
]
GAINS = ["-a", "0", "-l", "32", "-g", "30"]  # the night survey's settings


def busy():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=5) as r:
            h = json.loads(r.read())
    except OSError:
        return None
    return "listening" if h.get("listening") else h.get("recording")


out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
if reason := busy():
    print(time.strftime("%F %T"), "skipped, scanner busy:", reason, flush=True)
    sys.exit(0)
subprocess.run(["sudo", "systemctl", "stop", "sdrpi-scanner"], check=False)
done = []
try:
    time.sleep(2)
    for name, mhz, rate, seconds in TARGETS:
        path = out / f"{name}.cs8"
        started = time.time()
        r = subprocess.run(["hackrf_transfer", "-r", str(path), "-f", str(int(mhz * 1e6)), "-s", str(rate),
                            "-n", str(rate * seconds)] + GAINS, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        done.append({"name": name, "center_mhz": mhz, "rate": rate, "seconds": seconds, "started": started,
                     "ok": r.returncode == 0})
        print(time.strftime("%F %T"), name, "recorded" if r.returncode == 0 else r.stderr.strip()[-200:], flush=True)
finally:
    subprocess.run(["sudo", "systemctl", "start", "sdrpi-scanner"], check=False)
    (out / "captures.json").write_text(json.dumps(done))
    print(time.strftime("%F %T"), "radio handed back", flush=True)
