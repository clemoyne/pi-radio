#!/usr/bin/env python3
"""Meteor-M weather satellite passes (LRPT on 137.1 or 137.9 MHz).

  satwx.py passes [HOURS]              list passes from now
  satwx.py schedule FROM TO            a transient timer per pass between two local times (YYYY-MM-DDTHH:MM)
  satwx.py record SAT AOS LOS          take the HackRF from the scanner for one pass, give it back, then process it
  satwx.py process DIR                 waterfall, signal levels and SatDump decode of a recording (again)

Each pass gets a folder in PASSES with meta.json (pass, sky track, outcome), the raw recording (cs8, 2 MS/s centred on
137.5 MHz so both channels fit), waterfall.png, levels.json and SatDump's output per channel tried."""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path("/mnt/data/satwx")
PASSES = ROOT / "passes"
TLE = ROOT / "weather.tle"
WINDOWS = ROOT / "windows.json"  # [[from, to], ...] local times given to schedule
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle"
HOME = Path("/mnt/data/flights/app/local.json")  # the receiver's position (not published)
SCANNER_HEALTH = "http://127.0.0.1:8080/api/health"
TZ = ZoneInfo("America/Toronto")

SATS = ["METEOR-M2 3", "METEOR-M2 4"]
CHANNELS = [137.1e6, 137.9e6]
CENTER = 137.5e6
RATE = 2_000_000
GAINS = ["-a", "1", "-l", "32", "-g", "24"]
MIN_PEAK_EL = 10  # lower passes rarely decode
EARLY_S = 120  # timer fires this long before the satellite rises
WAIT_FOR_RADIO_S = 180  # past rise, how long to wait for a go-around or someone listening to finish
MAX_RAW_BYTES = 20e9  # oldest raw recordings deleted beyond this (the decoded images stay)
PIPELINES = ["meteor_m2-x_lrpt", "meteor_m2-x_lrpt_80k"]
SIGNAL_DB = 3  # channel must rise this far above its own floor to be worth decoding


def log(*parts):
    print(time.strftime("%F %T"), *parts, flush=True)


def tles():
    if not TLE.exists() or time.time() - TLE.stat().st_mtime > 86400:
        try:
            with urllib.request.urlopen(TLE_URL, timeout=30) as r:
                data = r.read()
            if b"METEOR" in data:
                TLE.write_bytes(data)
        except OSError as e:
            log("couldn't refresh orbits, using the saved ones:", e)
    from skyfield.api import load, wgs84
    home = json.loads(HOME.read_text())["home"]
    lat, lon = (home["lat"], home["lon"]) if isinstance(home, dict) else home[:2]
    sats = {s.name: s for s in load.tle_file(str(TLE)) if s.name in SATS}
    return load.timescale(), sats, wgs84.latlon(lat, lon)


def find_passes(t0, t1):
    ts, sats, here = tles()
    out = []
    for name, sat in sats.items():
        times, events = sat.find_events(here, ts.from_datetime(t0), ts.from_datetime(t1), altitude_degrees=0)
        cur = {}
        for t, ev in zip(times, events):
            if ev == 0:
                cur = {"sat": name, "aos": t.utc_datetime()}
            elif ev == 1 and cur:
                cur["peak_el"] = round(float((sat - here).at(t).altaz()[0].degrees), 1)
            elif ev == 2 and "peak_el" in cur:
                cur["los"] = t.utc_datetime()
                out.append(cur)
                cur = {}
    return sorted(out, key=lambda p: p["aos"])


def sky_track(name, aos, los):
    ts, sats, here = tles()
    sat, track, t = sats[name], [], aos
    while t <= los:
        alt, az, dist = (sat - here).at(ts.from_datetime(t)).altaz()
        track.append([int(t.timestamp()), round(float(az.degrees), 1), round(float(alt.degrees), 1), round(float(dist.km))])
        t += timedelta(seconds=10)
    return track


def local(dt):
    return dt.astimezone(TZ).strftime("%a %d %b %H:%M")


def cmd_passes(hours="24"):
    now = datetime.now(timezone.utc)
    for p in find_passes(now, now + timedelta(hours=float(hours))):
        mark = "" if p["peak_el"] >= MIN_PEAK_EL else "  (too low, skipped)"
        print(f"{p['sat']:12} {local(p['aos'])} -> {local(p['los'])[-5:]}  peak {p['peak_el']:4.1f}°{mark}")


def cmd_schedule(start, end):
    """Remembers the window in WINDOWS, so satwx-restore.service can set its timers again after a reboot."""
    windows = [w for w in load_windows() if datetime.fromisoformat(w[1]).replace(tzinfo=TZ) > datetime.now(TZ)]
    if [start, end] not in windows:
        windows.append([start, end])
    WINDOWS.write_text(json.dumps(windows))
    set_timers(datetime.fromisoformat(start).replace(tzinfo=TZ), datetime.fromisoformat(end).replace(tzinfo=TZ))


def load_windows():
    try:
        return json.loads(WINDOWS.read_text())
    except (OSError, ValueError):
        return []


def cmd_restore():
    """At boot: timers again for every remembered window still to come (transient timers don't survive a reboot)."""
    now = datetime.now(TZ)
    for start, end in load_windows():
        t0, t1 = datetime.fromisoformat(start).replace(tzinfo=TZ), datetime.fromisoformat(end).replace(tzinfo=TZ)
        if t1 > now:
            set_timers(max(t0, now + timedelta(seconds=EARLY_S + 30)), t1)


def set_timers(t0, t1):
    for p in find_passes(t0, t1):
        if p["peak_el"] < MIN_PEAK_EL:
            log("skipping", p["sat"], local(p["aos"]), "peak", p["peak_el"])
            continue
        fire = (p["aos"] - timedelta(seconds=EARLY_S)).astimezone(TZ)
        unit = "satwx-" + p["aos"].astimezone(TZ).strftime("%Y%m%d-%H%M")
        if subprocess.run(["systemctl", "is-active", "--quiet", unit + ".timer"]).returncode == 0:
            continue  # already set
        subprocess.run(["sudo", "systemd-run", "--unit", unit, "--uid=pi", "--gid=pi",
                        "--on-calendar", fire.strftime("%Y-%m-%d %H:%M:%S"), "--timer-property=AccuracySec=1s",
                        "-p", "RuntimeMaxSec=4h", "-p", "Nice=5",
                        "-p", f"StandardOutput=append:{ROOT}/satwx.log", "-p", f"StandardError=append:{ROOT}/satwx.log",
                        "/usr/bin/python3", str(Path(__file__).resolve()), "record", p["sat"],
                        p["aos"].isoformat(), p["los"].isoformat()], check=True)
        log("scheduled", p["sat"], local(p["aos"]), "->", local(p["los"])[-5:], "peak", p["peak_el"], "as", unit)


def radio_state():
    try:
        with urllib.request.urlopen(SCANNER_HEALTH, timeout=5) as r:
            h = json.loads(r.read())
    except OSError:
        return None  # scanner not running; the radio is free
    if h.get("listening"):
        return "someone was listening on the scanner"
    if h.get("recording") and not h["recording"].startswith("Collecting"):  # a collection picks up again afterwards
        return "the scanner was busy: " + h["recording"]
    return None


def sleep_until(dt):
    while (left := (dt - datetime.now(timezone.utc)).total_seconds()) > 0:
        time.sleep(min(left, 30))


def cmd_record(name, aos, los):
    aos, los = datetime.fromisoformat(aos), datetime.fromisoformat(los)
    folder = PASSES / (aos.astimezone(TZ).strftime("%Y%m%d-%H%M") + "-" + name.replace(" ", "").lower())
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"sat": name, "aos": int(aos.timestamp()), "los": int(los.timestamp()), "center_hz": CENTER, "rate": RATE,
            "gains": GAINS, "track": sky_track(name, aos, los)}
    meta["peak_el"] = max(p[2] for p in meta["track"])
    write_meta(folder, meta)
    sleep_until(aos - timedelta(seconds=30))
    busy = radio_state()
    while busy and datetime.now(timezone.utc) < aos + timedelta(seconds=WAIT_FOR_RADIO_S):
        time.sleep(10)
        busy = radio_state()
    if busy:
        meta["outcome"] = "skipped: " + busy
        write_meta(folder, meta)
        log(name, "skipped,", busy)
        return
    prune_raw()
    log(name, "taking the radio from the scanner")
    subprocess.run(["sudo", "systemctl", "stop", "sdrpi-scanner"], check=False)
    raw = folder / "baseband.cs8"
    try:
        time.sleep(2)  # let the scanner release the HackRF
        start = datetime.now(timezone.utc)
        seconds = max((los - start).total_seconds() + 30, 60)
        meta["recorded_from"] = int(start.timestamp())
        result = subprocess.run(["hackrf_transfer", "-r", str(raw), "-f", str(int(CENTER)), "-s", str(RATE),
                                 "-b", "1750000", "-n", str(int(seconds * RATE))] + GAINS,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=seconds + 120)
        meta["recorded_to"] = int(time.time())
        if result.returncode:
            meta["record_error"] = result.stderr.strip().splitlines()[-3:]
    finally:
        subprocess.run(["sudo", "systemctl", "start", "sdrpi-scanner"], check=False)
        log(name, "radio handed back to the scanner")
    write_meta(folder, meta)
    process(folder)


def write_meta(folder, meta):
    tmp = folder / "meta.json.tmp"
    tmp.write_text(json.dumps(meta))
    tmp.replace(folder / "meta.json")


def prune_raw():
    raws = sorted(PASSES.glob("*/baseband.cs8"), key=lambda p: p.stat().st_mtime)
    while raws and sum(p.stat().st_size for p in raws) > MAX_RAW_BYTES:
        log("deleting old recording", raws[0])
        raws.pop(0).unlink()


def waterfall(folder, meta):
    """0.5 s rows of averaged 4096-point spectra; keeps 136.8-138.2 MHz."""
    raw = folder / "baseband.cs8"
    nfft, row = 4096, RATE // 2
    freqs = CENTER + np.fft.fftshift(np.fft.fftfreq(nfft, 1 / RATE))
    keep = (freqs >= 136.8e6) & (freqs <= 138.2e6)
    window = np.hanning(nfft).astype(np.float32)
    rows = []
    with open(raw, "rb") as f:
        while True:
            buf = np.frombuffer(f.read(row * 2), np.int8)
            if len(buf) < nfft * 2:
                break
            iq = (buf[0::2].astype(np.float32) + 1j * buf[1::2].astype(np.float32))
            blocks = iq[: len(iq) // nfft * nfft].reshape(-1, nfft) * window
            spec = np.mean(np.abs(np.fft.fft(blocks, axis=1)) ** 2, axis=0)
            rows.append(10 * np.log10(np.fft.fftshift(spec)[keep] + 1e-9))
    w = np.array(rows, np.float32)
    f = freqs[keep]
    # per channel: power within +-60 kHz against the quiet stretch 137.35-137.45 MHz, in 5 s steps
    quiet = (f > 137.35e6) & (f < 137.45e6)
    levels = {"step_s": 5, "start": meta.get("recorded_from", meta["aos"]), "channels": {}}
    for ch in CHANNELS:
        band = np.abs(f - ch) < 60e3
        snr = 10 * np.log10(np.mean(10 ** (w[:, band] / 10), axis=1) / np.mean(10 ** (w[:, quiet] / 10), axis=1))
        snr = snr[: len(snr) // 10 * 10].reshape(-1, 10).mean(axis=1)
        levels["channels"][f"{ch / 1e6:.1f}"] = [round(float(x), 2) for x in snr]
    (folder / "levels.json").write_text(json.dumps(levels))
    # image: each column against its own median, 0-20 dB
    from PIL import Image
    v = np.clip((w - np.median(w, axis=0, keepdims=True)) / 20, 0, 1)
    v = v[:, : v.shape[1] // 3 * 3].reshape(v.shape[0], -1, 3).max(axis=2)
    stops = np.array([[8, 12, 28], [14, 40, 78], [22, 92, 140], [60, 160, 196], [150, 218, 232], [240, 252, 255]],
                     np.float32)
    lut = np.stack([np.interp(np.linspace(0, 1, 256), np.linspace(0, 1, len(stops)), stops[:, c]) for c in range(3)],
                   1).astype(np.uint8)
    Image.fromarray(lut[(v ** 0.8 * 255).astype(np.uint8)]).save(folder / "waterfall.png", optimize=True)
    return levels, [float(f[0]), float(f[v.shape[1] * 3 - 1])]


def process(folder):
    folder = Path(folder)
    meta = json.loads((folder / "meta.json").read_text())
    raw = folder / "baseband.cs8"
    if not raw.exists() or raw.stat().st_size < RATE * 20:
        meta["outcome"] = meta.get("outcome") or "no recording"
        write_meta(folder, meta)
        return
    log(folder.name, "making the waterfall")
    levels, span = waterfall(folder, meta)
    meta["waterfall_hz"] = span
    best = {ch: (max(v) if v else -99) for ch, v in levels["channels"].items()}
    meta["best_snr"] = best
    meta["decodes"] = {}
    for ch, snr in sorted(best.items(), key=lambda kv: -kv[1]):
        if snr < SIGNAL_DB:
            continue
        shift = int(CENTER - float(ch) * 1e6)
        # SatDump's shift direction isn't documented; the expected one first, then the other
        for pipeline, sign in [(p, s) for s in (1, -1) for p in PIPELINES]:
            out = folder / f"decode-{ch}-{pipeline.rsplit('_', 1)[-1]}{'' if sign > 0 else '-flip'}"
            shutil.rmtree(out, ignore_errors=True)
            log(folder.name, "decoding", ch, "MHz with", pipeline, "shift", sign * shift)
            r = subprocess.run(["nice", "satdump", pipeline, "baseband", str(raw), str(out), "--samplerate", str(RATE),
                                "--baseband_format", "cs8", "--freq_shift", str(sign * shift), "--dc_block"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out.mkdir(exist_ok=True)
            (out / "satdump.log").write_text(r.stdout[-200_000:])
            images = sorted(p.relative_to(folder).as_posix() for p in out.rglob("*.png"))
            meta["decodes"][out.name] = {"channel": ch, "pipeline": pipeline, "shift": sign * shift, "images": images}
            if images:
                meta["shift_sign"] = sign
                break
        if any(d["images"] for d in meta["decodes"].values()):
            break
    got = any(d["images"] for d in meta["decodes"].values())
    if got:
        meta["outcome"] = "decoded"
    elif max(best.values()) >= SIGNAL_DB:
        meta["outcome"] = "heard but not decoded"
    else:
        meta["outcome"] = "not heard"
    write_meta(folder, meta)
    log(folder.name, meta["outcome"], "best levels", best)


if __name__ == "__main__":
    PASSES.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    {"passes": cmd_passes, "schedule": cmd_schedule, "restore": cmd_restore, "record": cmd_record,
     "process": process}[args[0]](*args[1:])
