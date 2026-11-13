#!/usr/bin/env python3
"""Receive-only HackRF scanner: band monitor plus AM/FM audio to the browser."""
import asyncio
import collections
import datetime
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import threading
import time
import traceback
import wave
from pathlib import Path

import numpy as np
from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
from scipy import fft, signal

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("SCANNER_DATA", "/mnt/data/scanner"))
SURVEY_FILE = DATA / "survey.json"
PORT = int(os.environ.get("SCANNER_PORT", "8080"))
DSD_FME = DATA / "decoders" / "bin" / "dsd-fme"
# ACARS mode: acarsdec (built with libacars, like DSD-FME) takes the HackRF directly and decodes up to 8 channels
# inside one 2 MHz window. These are the common North American ones, 131.475 included.
ACARSDEC = DATA / "decoders" / "bin" / "acarsdec"
ACARS_FREQS = ["130.025", "130.425", "130.450", "131.125", "131.475", "131.550"]
ACARS_GAIN = 40
ACARS_DB = DATA / "acars.db"
ACARS_MAX_HOURS = 12
ACARS_PAGE_LIMIT = 300
# VDL Mode 2 carries most airline datalink now; dumpvdl2 (github.com/szpajder/dumpvdl2, uses the same libacars) decodes it.
# ACARS mode runs it instead of acarsdec when acars_vdl2 is set, and its messages go in the same table.
DUMPVDL2 = DATA / "decoders" / "bin" / "dumpvdl2"
VDL2_FREQS = ["136.650", "136.700", "136.725", "136.775", "136.800", "136.825", "136.875", "136.975"]
VDL2_CENTER = 136_900_000  # sits between channels so the HackRF's DC spike misses them
VDL2_OVERSAMPLE = 20  # dumpvdl2 samples at 105 kS/s times this: 2.1 MS/s, inside the HackRF's range
VDL2_GAINS = "LNA=32,VGA=24,AMP=0"
SETTINGS_PATH = DATA / "settings.json"
DEFAULT_SETTINGS = {"goaround_interrupts_acars": True, "emergency_starts_acars": True, "acars_vdl2": False}
# Emergency squawks come from garage_pi's aircraft log, which lists aircraft squawking 7500/7600/7700 in range now
EMERGENCY_URL = os.environ.get("SCANNER_EMERGENCY_URL", "http://piaware.local/aircraft/data/data.json")
EMERGENCY_POLL_S = 60  # the log rewrites that file once a minute
EMERGENCY_HOLD_S = 600  # ACARS mode keeps going this long after the last emergency squawk is seen
ACARS_SUBLABELS = {"DF": "from the flight data acquisition unit (engine and performance monitoring)",
                   "M1": "from the flight management computer"}
# Lookups shared with the Flights dashboard, used to put names and distances in ACARS summaries
FLIGHTS_APP = Path("/mnt/data/flights/app")
ACARS_MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
ACARS_DATE = re.compile(r"\b(%s)(\d{2})\b" % "|".join(ACARS_MONTHS))
ACARS_POSITION = re.compile(r"([NS])(\d{2})(\d{3})([EW]) ?(0\d{2}|\d{2})(\d{3})")
ACARS_AIRPORT_PAIR = re.compile(r"(?=([A-Z]{4})[ ,/]?([A-Z]{4}))")
ACARS_FLIGHT_NUMBER = re.compile(r"/FN ?(\w+)")
ACARS_NEAR_KM = 600  # positions decoded from free text farther than this are probably not positions
ACARS_MAX_POINTS = 3
ACARS_MAX_BYTES = 5 * 10**9  # oldest messages are deleted past this; SQLite reuses the space, so the file stops growing
ACARS_SIZE_CHECK_EVERY = 500
ACARS_PRUNE_ROWS = 20000
ACARS_PLAIN_FIELDS = {"timestamp", "station_id", "channel", "freq", "level", "error", "mode", "label", "block_id", "ack",
                      "tail", "flight", "msgno", "text", "end", "assstat", "app"}  # anything else acarsdec adds is decoded content
# What common ACARS labels mean; many others are airline-specific
ACARS_LABELS = {
    "_d": "No text: an acknowledgement or link keep-alive", "Q0": "Link test", "SQ": "Squitter: a ground station announcing itself",
    "H1": "To or from an onboard system (flight computer, maintenance or cockpit printer)", "SA": "Datalink status (which radio link is in use)",
    "5Z": "Airline-defined message", "RA": "Uplink printed in the cockpit", "AA": "Air traffic control message (CPDLC)",
    "BA": "Air traffic control message (CPDLC)", "A6": "Air traffic services message (ADS-C or CPDLC)",
    "B6": "Air traffic services message (ADS-C or CPDLC)", "10": "Airline operations report", "80": "Airline operations report",
}
BLUETOOTH_PAGE = Path("/mnt/data/bletracker/app/www/index.html")
BLUETOOTH_DATA = Path("/run/bletracker/data.json")
WIFI_PAGE = Path("/mnt/data/wifitracker/app/www/index.html")
WIFI_DATA = Path("/run/wifitracker/data.json")
STATUS_WWW = Path("/mnt/data/pistatus/app/www")
STATUS_DATA = Path("/run/pistatus/status.json")
STATUS_API = "http://127.0.0.1:8093/status/api/power"  # pistatus checks and forwards power requests; it never runs as root
FLIGHTS_PAGE = Path("/mnt/data/flights/app/www/index.html")
FLIGHTS_API = "http://127.0.0.1:8094"  # flightstore
CARS_PAGE = Path("/mnt/data/cars/app/www/index.html")
WEATHER_PAGE = Path("/mnt/data/cars/app/www/weather.html")
CARS_API = "http://127.0.0.1:8095"  # carstore, fed by porch_pi

CHANNELS = json.loads((HERE / "channels.json").read_text())
BY_ID = {c["id"]: c for c in CHANNELS}

SWEEP_BANDS = ["108:138", "144:174", "406:470", "860:880"]
SWEEP_LNA, SWEEP_VGA = 24, 20
ACTIVE_DB = 14.0
ACTIVE_HOLD_S = 2.0

GAINS = {"low": (16, 16), "normal": (24, 20), "high": (32, 30)}

# Overlap-save channeliser. N/2 must keep the mixing bin even so blocks stay in phase.
FS = 2_000_000
N = 25_000
HOP = N // 2
M = 400
AUDIO_RATE = 16_000
OFFSET_HZ = 300_000
BIN_HZ = FS / N
OFFSET_BIN = int(OFFSET_HZ / BIN_HZ)
HOPS_PER_FRAME = 4
SPECTRUM_SPAN_BINS = 500
SPECTRUM_POINTS = 200
DISCRIMINATOR_GAIN = 42_400

CAPTURABLE = [c for c in CHANNELS if c["listen"] and c["group"] != "unverified"]
CAPTURE_SQUELCH = 10.0
CAPTURE_MAX_PCT = 60.0
CapturePlan = collections.namedtuple("CapturePlan", "wait silence longest saved_cooldown empty_cooldown")
PLAN_DEFAULT = CapturePlan(3.0, 1.5, 12.0, 120, 600)
PLAN_DIGITAL = CapturePlan(6.0, 1.5, 12.0, 120, 600)
# Air traffic is short bursts with gaps, so stay tuned for the next one.
PLAN_AVIATION = CapturePlan(15.0, 4.0, 20.0, 60, 30)
# Collections: the HackRF sits on one 2 MHz block, watches every channel in it at once, and records whole conversations
# (only the transmissions that pass the TransmissionGate) until a goal is reached. The band monitor's sweep can't be
# used to find them: it shows a comb of false activity across the railway band.
COLLECT_PATH = DATA / "collect.json"
COLLECT_JOIN_S = 0.3  # silence put between the transmissions kept from one conversation
COLLECT_OPEN_DB = 10.0  # channel power over the block's noise floor that counts as keyed up
COLLECT_TRIGGER_DB = 12.0
COLLECT_CHANNEL_HZ = 5_000  # half-width summed for a channel's power
COLLECT_SILENCE_S = 10.0  # a conversation ends after this long with nobody keying up
COLLECT_LONGEST_S = 180.0
COLLECT_REST_S = 900  # a channel whose conversation kept nothing (a carrier, data) is ignored this long
COLLECT_REACH_HZ = 860_000  # the HackRF's 1.75 MHz filter starts cutting in beyond this
COLLECT_DC_HZ = 6_000
# Railway calls are often a second or two and barely rise and fall; 12 Nov rejects of real calls varied 3.6-4.8 dB
COLLECT_MIN_VAR_DB = 3.5
COLLECT_MAX_HISS = 1.0  # 2.5-4 kHz hiss must stay below the 300-1200 Hz voice band
COLLECT_REJECTS = DATA / "collect-rejects"  # if this folder exists, the first 40 turned-away conversations are saved in it
BY_MHZ = {round(c["mhz"], 4): c for c in CHANNELS if c["mode"] == "FM"}


def collect_grid(first, step, last, label):
    out = []
    for i in range(int(round((last - first) / step)) + 1):
        mhz = round(first + i * step, 5)
        known = BY_MHZ.get(round(mhz, 4))
        out.append({"id": f"{label.lower()}-{mhz:.4f}", "mhz": mhz, "mode": "FM", "decoder": None, "group": label.lower(),
                    "name": known["name"] if known else label, "label": f"{label} {mhz:.4f}"})
    return out


COLLECTIONS = {
    # the 15 kHz railway channels; only two are in the frequency lists, so the rest are named by frequency
    "railway": {"title": "Railway", "center": 160_717_500, "seconds": 900, "conversations": 0,
                "channels": collect_grid(159.81, 0.015, 161.61, "Railway")},
    # business radio is on a 6.25 kHz raster; 462-464 MHz held most of the business traffic in the 11-12 Nov survey
    "business": {"title": "Business radio", "center": 463_003_125, "seconds": 300, "conversations": 3,
                 "channels": collect_grid(462.15, 0.00625, 463.85, "Business")},
}
AVIATION_MAX_PCT = 5.0
CAPTURE_MIN_S = 1.0
CAPTURE_MIN_RMS = 300
CAPTURE_MIN_VAR_DB = 5.0
MONITOR_RESTART_S = 1.0
CAPTURE_GAP_S = 10.0
RESET_AFTER_FAILURES = 2
WARN_AFTER_FAILURES = 6
MAX_CLIPS = 300
MAX_RECORD_S = 600
GATE_MIN_SNR_DB = 10.0
GATE_HANG_SAMPLES = AUDIO_RATE // 2
GATE_MIN_SAMPLES = int(0.4 * AUDIO_RATE)
GATE_MAX_SAMPLES = 30 * AUDIO_RATE
CONTINUOUS_LIMIT_BYTES = int(os.environ.get("SCANNER_CONTINUOUS_LIMIT", 1_000_000_000))
CONTINUOUS_SYNC_S = 30
GOAROUND_WINDOW_S = int(os.environ.get("SCANNER_GOAROUND_SECONDS", 300))
GOAROUND_SQUELCH = float("-inf")  # squelch stays open so a weak call can't be dropped
# Afterwards the static between calls is cut out, keeping the full recording too. Open-squelch static stays level
# (its 20 ms loudness varies about 0.5 dB) while speech varies 3 dB or more, and a carrier lifts the SNR.
TRIM_BLOCK_S = 0.5
TRIM_MIN_VAR_DB = 2.0
TRIM_LEVEL_DB = 3.0
TRIM_SNR_DB = 6.0
TRIM_SKIP_S = 1.0  # the demodulator's filters settle in the first second
TRIM_BEFORE_S = 1.0
TRIM_AFTER_S = 1.5
TRIM_GAP_S = 0.4
# Pearson has two departure frequencies; the one heard most recently is used. Billy Bishop has none listed, so Tower.
AIRPORT_CHANNELS = {"CYYZ": ("127.5750a", "128.8000a"), "CYTZ": ("133.4000a",)}
AIRPORT_NAMES = {"CYYZ": "Pearson", "CYTZ": "Billy Bishop"}
CONTINUOUS_MIN_FREE_BYTES = 2_000_000_000
MIN_RECORD_S = 0.5
MAX_EMPTY_COOLDOWN_S = 600
POWER_POLL_S = 2
POWER_HOLD_S = 60
UNDERVOLTAGE_NOW = 0x1
# A Pi 4 reports only a flag that trips near 4.6 V; it cannot measure its supply voltage.
UNDERVOLTAGE_TEXT = "UNDERVOLTAGE- <4.6V"
CLIP_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}_[0-9.]+(-full)?\.wav$")


class Survey:
    """Per-channel signal statistics, kept across restarts on the SSD."""

    def __init__(self):
        self.idx = {}
        self.mhz = []
        for c in CHANNELS:
            for f in c.get("freqs", [c["mhz"]]):
                self.idx.setdefault(round(f, 4), len(self.mhz))
                if len(self.mhz) <= self.idx[round(f, 4)]:
                    self.mhz.append(round(f, 4))
        n = len(self.mhz)
        self.hz = np.array(self.mhz) * 1e6
        self.now = np.full(n, -99.0)
        self.last_active = np.zeros(n)
        self.streak = np.zeros(n)
        self.peak = np.full(n, -99.0)
        self.hits = np.zeros(n)
        self.samples = np.zeros(n)
        self.sweep_seconds = 0.0
        self.lookup = {}
        self.load()

    def load(self):
        try:
            saved = json.loads(SURVEY_FILE.read_text())
        except (OSError, ValueError):
            return
        self.sweep_seconds = saved.get("seconds", 0.0)
        for key, (peak, hits, samples) in saved.get("freqs", {}).items():
            i = self.idx.get(float(key))
            if i is not None:
                self.peak[i], self.hits[i], self.samples[i] = peak, hits, samples

    def save(self):
        seen = self.samples > 0
        payload = {
            "seconds": self.sweep_seconds,
            "freqs": {
                str(self.mhz[i]): [round(float(self.peak[i]), 1), int(self.hits[i]), int(self.samples[i])]
                for i in np.nonzero(seen)[0]
            },
        }
        tmp = SURVEY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(SURVEY_FILE)

    def ingest(self, low, high, power):
        entry = self.lookup.get(low)
        if entry is None:
            width = (high - low) / len(power)
            inside = np.nonzero((self.hz >= low) & (self.hz < high))[0]
            entry = (inside, ((self.hz[inside] - low) / width).astype(int))
            self.lookup[low] = entry
        inside, bins = entry
        if inside.size == 0:
            return
        snr = power[bins] - np.median(power)
        now = time.monotonic()
        self.now[inside] = snr
        self.samples[inside] += 1
        self.peak[inside] = np.maximum(self.peak[inside], snr)
        above = snr >= ACTIVE_DB
        self.hits[inside[above]] += 1
        self.streak[inside] = (self.streak[inside] + 1) * above
        steady = inside[self.streak[inside] >= 2]
        self.last_active[steady] = now

    def pct(self, mhz):
        i = self.idx[round(mhz, 4)]
        return 100 * float(self.hits[i] / self.samples[i]) if self.samples[i] else 0.0

    def active(self):
        cutoff = time.monotonic() - ACTIVE_HOLD_S
        return {str(self.mhz[i]): round(float(self.now[i]), 1) for i in np.nonzero(self.last_active > cutoff)[0]}

    def stats(self):
        return {
            "seconds": round(self.sweep_seconds),
            "freqs": {
                str(self.mhz[i]): {
                    "peak": round(float(self.peak[i]), 1),
                    "pct": round(100 * float(self.hits[i] / self.samples[i]), 2),
                }
                for i in np.nonzero(self.samples > 0)[0]
            },
        }


class Demodulator:
    """Turns raw HackRF IQ into 16 kHz audio frames for one channel."""

    def __init__(self, mode, offset_hz=OFFSET_HZ):
        self.mode = mode
        self.offset_bin = int(round(offset_hz / BIN_HZ))  # where the channel sits from the tuned frequency
        self.m = 600 if mode == "DIGITAL" else M
        self.rate = FS * self.m / N
        half = {"FM": 112, "AM": 62, "DIGITAL": 78}[mode]
        taper = 15
        mask = np.zeros(self.m)
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(taper) / taper)
        centre = self.m // 2
        mask[centre - half : centre + half] = 1
        mask[centre - half : centre - half + taper] = ramp
        mask[centre + half - taper : centre + half] = ramp[::-1]
        self.band = np.arange(-self.m // 2, self.m // 2)
        self.dc = 0.0
        self.h = fft.ifftshift(mask)
        self.in_band = np.abs(self.band) < half - taper
        self.tail = np.zeros(HOP, dtype=np.complex64)
        self.prev = 0j
        low_cut = 300 if mode == "FM" else 200
        self.sos = signal.butter(4, [low_cut, 3400], "bandpass", fs=32_000, output="sos")
        self.zi = signal.sosfilt_zi(self.sos) * 0
        self.level = 0.0
        self.mean_env = 1e-3
        self.snr = 0.0
        self.offset_hz = 0.0
        self.last_spectrum = None

    def process(self, iq):
        x = np.concatenate([self.tail, iq])
        self.tail = iq
        spectrum = fft.fft(x, workers=1)
        idx = (self.offset_bin + self.band) % N
        y = spectrum[idx]
        y = fft.ifftshift(y) * self.h
        z = fft.ifft(y)[self.m // 2 :] * (self.m / N)

        power = np.abs(spectrum[idx]) ** 2
        wide = np.abs(spectrum[(self.offset_bin + np.arange(-2500, 2500)) % N]) ** 2
        noise = np.median(wide) / math.log(2) + 1e-12
        snr = 10 * math.log10(np.mean(power[self.in_band]) / noise + 1e-12)
        self.snr += 0.25 * (snr - self.snr)

        self.last_spectrum = spectrum
        if self.mode == "DIGITAL":
            joined = np.concatenate([[self.prev], z])
            phase = np.angle(joined[1:] * np.conj(joined[:-1]))
            self.prev = z[-1]
            self.dc += 0.01 * (float(np.mean(phase)) - self.dc)
            self.offset_hz = self.dc * self.rate / (2 * np.pi)
            return (np.clip((phase - self.dc) * DISCRIMINATOR_GAIN, -32767, 32767)).astype(np.int16)

        if self.mode == "FM":
            joined = np.concatenate([[self.prev], z])
            phase = np.angle(joined[1:] * np.conj(joined[:-1]))
            self.prev = z[-1]
            self.offset_hz += 0.05 * (float(np.mean(phase)) * 32_000 / (2 * np.pi) - self.offset_hz)
            audio = phase * 0.8
        else:
            env = np.abs(z)
            self.mean_env += 0.05 * (float(np.mean(env)) - self.mean_env)
            audio = (env / (self.mean_env + 1e-9) - 1) * 0.9

        audio, self.zi = signal.sosfilt(self.sos, audio, zi=self.zi)
        return audio[::2]

    def spectrum_points(self):
        if self.last_spectrum is None:
            return None
        span = np.abs(self.last_spectrum[(self.offset_bin + np.arange(-SPECTRUM_SPAN_BINS, SPECTRUM_SPAN_BINS)) % N]) ** 2
        chunks = span[: len(span) // SPECTRUM_POINTS * SPECTRUM_POINTS].reshape(SPECTRUM_POINTS, -1)
        return np.round(10 * np.log10(chunks.max(axis=1) + 1e-9) - 10 * math.log10(N), 1).tolist()


class DigitalBridge:
    """Feeds discriminator audio to DSD-FME and collects decoded voice."""

    def __init__(self, flag):
        self.stereo = flag == "-fs"
        self.status, self.lines, self.lock = "Searching for digital sync", collections.deque(maxlen=200), threading.Lock()
        self.pcm, self.started, self.last_voice, self.closed = collections.deque(), False, 0.0, False
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(0.2)
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(8)
        tcp_port, udp_port = server.getsockname()[1], self.udp.getsockname()[1]
        self.proc = subprocess.Popen(
            [str(DSD_FME), flag, "-i", f"tcp:127.0.0.1:{tcp_port}", "-o", f"udp:127.0.0.1:{udp_port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, cwd=DATA, text=True, errors="replace",
        )
        self.conn, _ = server.accept()
        server.close()
        self.conn.settimeout(1)
        threading.Thread(target=self.read_audio, daemon=True).start()
        threading.Thread(target=self.read_log, daemon=True).start()

    def read_audio(self):
        while not self.closed:
            try:
                data = self.udp.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            pcm = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16).astype(np.float32)
            if self.stereo:
                pcm = pcm[: len(pcm) // 2 * 2].reshape(-1, 2).sum(axis=1).clip(-32767, 32767)
            if pcm.size:
                up = np.interp(np.arange(pcm.size * 2) / 2, np.arange(pcm.size), pcm).astype(np.int16)
                with self.lock:
                    self.pcm.extend(up.tolist())
                self.last_voice = time.monotonic()

    def read_log(self):
        for line in self.proc.stderr:
            text = line.strip()
            if text:
                self.lines.append(text)
                self.status = describe_decoder(text, self.status)

    def feed(self, samples):
        try:
            self.conn.sendall(samples.tobytes())
        except OSError:
            return False
        return True

    def take(self, count):
        with self.lock:
            if not self.started and len(self.pcm) >= 1600:
                self.started = True
            if not self.started:
                return np.zeros(count, np.int16)
            got = [self.pcm.popleft() for _ in range(min(count, len(self.pcm)))]
            if len(got) < count:
                self.started = False
        return np.array(got + [0] * (count - len(got)), np.int16)

    def close(self):
        self.closed = True
        for sock in (self.conn, self.udp):
            sock.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def describe_decoder(text, current):
    if "Sync:" not in text:
        return current
    if "no sync" in text.lower():
        return "Searching for digital sync"
    if "ENC" in text.upper():
        return "Encrypted traffic, muted"
    for name in ("DMR", "P25"):
        if name in text:
            return f"{name} signal locked"
    return current


class Listener(threading.Thread):
    """Runs hackrf_transfer and the demodulator; frames go to an asyncio queue."""

    def __init__(self, channel, gain, loop, queue):
        super().__init__(daemon=True)
        self.channel, self.loop, self.queue = channel, loop, queue
        self.lna, self.vga = GAINS.get(gain, GAINS["normal"])
        self.squelch = 8.0
        self.stopped = threading.Event()
        self.proc = None

    def post(self, item):
        def push():
            if self.queue.qsize() > 8:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self.queue.put_nowait(item)

        self.loop.call_soon_threadsafe(push)

    def run(self):
        centre = int(round(self.channel["mhz"] * 1e6)) - OFFSET_HZ
        cmd = [
            "hackrf_transfer", "-r", "-", "-f", str(centre), "-s", str(FS), "-b", "1750000",
            "-a", "0", "-l", str(self.lna), "-g", str(self.vga),
        ]
        demod = Demodulator(self.channel["mode"])
        bridge = DigitalBridge(self.channel["decoder"]) if self.channel["mode"] == "DIGITAL" else None
        self.bridge = bridge
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        raw_len = HOP * 2
        pending, frames, buffered, open_until, last_spec = bytearray(), 0, [], 0.0, 0.0
        try:
            while not self.stopped.is_set():
                chunk = self.proc.stdout.read(raw_len)
                if not chunk:
                    break
                pending += chunk
                while len(pending) >= raw_len:
                    block = np.frombuffer(bytes(pending[:raw_len]), dtype=np.int8).astype(np.float32) / 128
                    del pending[:raw_len]
                    output = demod.process(block[0::2] + 1j * block[1::2])
                    if bridge:
                        bridge.feed(output)
                    else:
                        buffered.append(output)
                    frames += 1
                    if frames < HOPS_PER_FRAME:
                        continue
                    frames = 0
                    now = time.monotonic()
                    if bridge:
                        pcm = bridge.take(400)
                        is_open = now - bridge.last_voice < 0.5
                    else:
                        audio = np.concatenate(buffered)
                        buffered = []
                        if demod.snr >= self.squelch:
                            open_until = now + 0.5
                        is_open = now < open_until
                        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16) if is_open else np.zeros_like(audio, np.int16)
                    self.post(struct.pack("<ffI", demod.snr, demod.offset_hz, int(is_open)) + pcm.tobytes())
                    if now - last_spec > 0.2:
                        last_spec = now
                        self.post({"spectrum": demod.spectrum_points(), "decoder": bridge.status if bridge else ""})
            if not self.stopped.is_set():
                self.post({"error": "HackRF stopped streaming. It may need replugging."})
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            if bridge:
                bridge.close()

    def stop(self):
        self.stopped.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.join(timeout=5)


def speech_like(audio, short_calls=False):
    """Speech rises and falls from syllable to syllable; noise and carriers stay level.
    short_calls also accepts gentler rises and falls (a quick "roger" barely varies) when the sound sits in the voice
    band rather than in hiss."""
    size = int(0.02 * AUDIO_RATE)
    frames = audio[: len(audio) // size * size].reshape(-1, size).astype(float)
    level = 20 * np.log10(np.sqrt(np.mean(frames**2, axis=1)) + 1)
    spread = float(np.std(level))
    if spread >= CAPTURE_MIN_VAR_DB:
        return True
    if not short_calls or spread < COLLECT_MIN_VAR_DB:
        return False
    spec = np.abs(np.fft.rfft(audio.astype(float)))
    freqs = np.fft.rfftfreq(len(audio), 1 / AUDIO_RATE)
    voice = spec[(freqs > 300) & (freqs < 1200)].mean()
    hiss = spec[(freqs > 2500) & (freqs < 4000)].mean()
    return hiss < COLLECT_MAX_HISS * voice


class TransmissionGate:
    """Holds audio per transmission and releases it only if it is real traffic, not static or a steady tone."""

    def __init__(self, digital, short_calls=False):
        self.digital, self.short_calls = digital, short_calls
        self.segment, self.samples, self.quiet = [], 0, 0

    def flush(self):
        if not self.segment:
            return []
        audio = np.concatenate(self.segment)
        self.segment, self.samples, self.quiet = [], 0, 0
        if len(audio) < GATE_MIN_SAMPLES or not (self.digital or speech_like(audio, self.short_calls)):
            return []
        return [audio]

    def feed(self, snr, is_open, pcm):
        if is_open and (self.digital or snr >= GATE_MIN_SNR_DB):
            self.segment.append(pcm)
            self.samples += len(pcm)
            self.quiet = 0
        elif self.segment and is_open and self.quiet < GATE_HANG_SAMPLES:
            self.segment.append(pcm)
            self.samples += len(pcm)
            self.quiet += len(pcm)
        else:
            return self.flush()
        return self.flush() if self.samples >= GATE_MAX_SAMPLES else []


def block_snrs(chunks):
    """Peak SNR in each TRIM_BLOCK_S of a recording, from (snr, samples) per chunk."""
    block = int(TRIM_BLOCK_S * AUDIO_RATE)
    index = (np.cumsum([n for _, n in chunks]) - 1) // block
    peaks = np.full(index[-1] + 1, -np.inf)
    np.maximum.at(peaks, index, [snr for snr, _ in chunks])
    return peaks


def trim_static(pcm, snrs=None):
    """Cuts the static between transmissions out of an open-squelch recording, or None if nothing sounds like traffic."""
    block, frame = int(TRIM_BLOCK_S * AUDIO_RATE), int(0.02 * AUDIO_RATE)
    count = len(pcm) // block
    if not count:
        return None
    frames = pcm[: count * block].astype(np.float32).reshape(count, -1, frame)
    level = 20 * np.log10(np.sqrt(np.mean(frames**2, axis=2)) + 1)
    mean = level.mean(axis=1)
    active = (level.std(axis=1) >= TRIM_MIN_VAR_DB) | (np.abs(mean - np.median(mean)) >= TRIM_LEVEL_DB)
    if snrs is not None and len(snrs):
        snrs = snrs[:count]
        active[: len(snrs)] |= snrs >= np.median(snrs) + TRIM_SNR_DB
    active[: int(TRIM_SKIP_S / TRIM_BLOCK_S)] = False
    if not active.any():
        return None
    keep = np.zeros(count, bool)
    before, after = int(TRIM_BEFORE_S / TRIM_BLOCK_S), int(TRIM_AFTER_S / TRIM_BLOCK_S)
    for i in np.flatnonzero(active):
        keep[max(0, i - before) : i + after + 1] = True
    edges = np.flatnonzero(np.diff(np.concatenate([[0], keep.astype(np.int8), [0]])))
    gap = np.zeros(int(TRIM_GAP_S * AUDIO_RATE), np.int16)
    parts = []
    for start, end in zip(edges[::2], edges[1::2]):
        parts += [pcm[start * block : end * block], gap]
    return np.concatenate(parts[:-1])


class ClipStore:
    """Short WAV clips of detected traffic, newest first, capped at MAX_CLIPS."""

    def __init__(self):
        self.dir = DATA / "clips"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index = DATA / "clips.json"
        self.enabled, self.items = True, []
        try:
            saved = json.loads(self.index.read_text())
            self.enabled, self.items = saved["enabled"], saved["clips"]
        except (OSError, ValueError, KeyError):
            pass

    def persist(self):
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps({"enabled": self.enabled, "clips": self.items}))
        tmp.replace(self.index)

    def add(self, channel, pcm, peak, manual=False, collection=None):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f'{stamp}_{channel["mhz"]:.4f}.wav'
        with wave.open(str(self.dir / name), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(AUDIO_RATE)
            out.writeframes(pcm.tobytes())
        entry = {
            "name": name, "when": time.strftime("%Y-%m-%d %H:%M:%S"), "mhz": channel["mhz"],
            "title": channel["name"], "label": channel["label"], "group": channel["group"],
            "seconds": round(len(pcm) / AUDIO_RATE, 1), "peak": round(peak, 1), "manual": manual,
        }
        if collection:
            entry["collection"] = collection  # kept until deleted, like manual recordings
        self.items.insert(0, entry)
        expired = {c["name"] for c in [c for c in self.items if not c.get("manual") and not c.get("collection")][MAX_CLIPS:]}
        for name in expired:
            (self.dir / name).unlink(missing_ok=True)
        self.items = [c for c in self.items if c["name"] not in expired]
        self.persist()

    def open_stream(self, channel, title=None, kind="continuous"):
        name = f'{time.strftime("%Y%m%d-%H%M%S")}_{channel["mhz"]:.4f}.wav'
        writer = wave.open(str(self.dir / name), "wb")
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(AUDIO_RATE)
        entry = {
            "name": name, "when": time.strftime("%Y-%m-%d %H:%M:%S"), "mhz": channel["mhz"], "title": title or channel["name"],
            "label": channel["label"], "group": channel["group"], "seconds": 0.0, "peak": -99.0, "manual": True,
            "continuous": True, "stopped": "recording", "kind": kind,
        }
        self.items.insert(0, entry)
        self.persist()
        return name, entry, writer

    def trim(self, entry, snrs=None):
        """Keeps only the transmissions of an open-squelch recording, moving the full one to <name>-full.wav."""
        path = self.dir / entry["name"]
        with wave.open(str(path), "rb") as f:
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        trimmed = trim_static(pcm, snrs)
        if trimmed is None:
            entry["no_traffic"] = True
            return
        if len(trimmed) >= 0.9 * len(pcm):
            return
        full = path.with_name(path.stem + "-full.wav")
        path.replace(full)
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(AUDIO_RATE)
            out.writeframes(trimmed.tobytes())
        entry.update(full=full.name, full_seconds=round(len(pcm) / AUDIO_RATE, 1), seconds=round(len(trimmed) / AUDIO_RATE, 1))

    def remove(self, name):
        for c in self.items:
            if c["name"] == name and c.get("full"):
                (self.dir / c["full"]).unlink(missing_ok=True)
        (self.dir / name).unlink(missing_ok=True)
        self.items = [c for c in self.items if c["name"] != name]
        self.persist()


class ContinuousJob:
    """Records one channel straight to disk, independent of any open page."""

    def __init__(self, radio, channel, listener, queue, gate, window=None, title=None, kind="continuous"):
        self.radio, self.channel, self.listener, self.queue, self.gate = radio, channel, listener, queue, gate
        self.kind, self.title = kind, title or channel["name"]
        self.deadline = time.monotonic() + window if window else None
        self.name, self.entry, self.writer = radio.clips.open_stream(channel, title, kind)
        self.bytes, self.peak, self.last_sync = 0, -99.0, time.monotonic()
        self.subscribers, self.finished, self.task = set(), False, None
        self.snrs = []  # (snr, samples) per chunk, for cutting the static out of a go-around afterwards

    def start(self):
        self.task = asyncio.create_task(self.run())

    def info(self):
        return {"mhz": self.channel["mhz"], "id": self.channel["id"], "name": self.channel["name"], "bytes": self.bytes,
                "limit": CONTINUOUS_LIMIT_BYTES, "seconds": round(self.bytes / (2 * AUDIO_RATE)), "kind": self.kind,
                "title": self.title, "ends_in": max(0, round(self.deadline - time.monotonic())) if self.deadline else None}

    def sync(self):
        self.entry.update(seconds=round(self.bytes / (2 * AUDIO_RATE), 1), peak=round(self.peak, 1))
        self.radio.clips.persist()

    async def broadcast(self, send, item):
        for ws in list(self.subscribers):
            try:
                await getattr(ws, send)(item)
            except (ConnectionResetError, RuntimeError):
                self.subscribers.discard(ws)

    async def run(self):
        loop = asyncio.get_running_loop()
        try:
            while True:
                item = await self.queue.get()
                if self.deadline and time.monotonic() >= self.deadline:
                    await self.finish("window")
                    return
                if not isinstance(item, bytes):
                    if "error" in item:
                        await self.finish("error")
                        return
                    await self.broadcast("send_json", item)
                    continue
                snr, _, is_open = struct.unpack("<ffI", item[:12])
                if self.gate:
                    kept = self.gate.feed(snr, is_open, np.frombuffer(item[12:], dtype=np.int16).copy())
                else:
                    kept = [np.frombuffer(item[12:], dtype=np.int16)] if is_open else []
                if is_open:
                    self.peak = max(self.peak, snr)
                for audio in kept:
                    await loop.run_in_executor(None, self.writer.writeframes, audio.tobytes())
                    self.bytes += audio.size * 2
                    if self.kind == "goaround":
                        self.snrs.append((snr, audio.size))
                if self.bytes >= CONTINUOUS_LIMIT_BYTES:
                    await self.finish("capped")
                    return
                if kept:
                    if time.monotonic() - self.last_sync >= CONTINUOUS_SYNC_S:
                        self.last_sync = time.monotonic()
                        self.sync()
                        if shutil.disk_usage(DATA).free < CONTINUOUS_MIN_FREE_BYTES:
                            await self.finish("disk")
                            return
                await self.broadcast("send_bytes", item)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            await self.finish("error")

    async def finish(self, reason):
        if self.finished:
            return
        self.finished = True
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        loop = asyncio.get_running_loop()
        for audio in self.gate.flush() if self.gate else []:
            await loop.run_in_executor(None, self.writer.writeframes, audio.tobytes())
            self.bytes += audio.size * 2
        await loop.run_in_executor(None, self.writer.close)
        radio = self.radio
        if self.bytes:
            self.entry["stopped"] = reason
            self.sync()
            if self.kind == "goaround":
                try:
                    await loop.run_in_executor(None, radio.clips.trim, self.entry, block_snrs(self.snrs))
                    radio.clips.persist()
                except Exception:
                    traceback.print_exc()
        else:
            radio.clips.remove(self.name)
        await loop.run_in_executor(None, self.listener.stop)
        radio.job, radio.listening = None, None
        hours = self.bytes / (2 * AUDIO_RATE) / 3600
        title = f'{self.title} ({self.channel["mhz"]:.4f})'
        messages = {
            "capped": f"Continuous recording of {title} stopped at the 1 GB limit after {hours:.1f} hours of audio. It is saved in Recordings.",
            "disk": f"Continuous recording of {title} stopped because the SSD is nearly full. It is saved in Recordings.",
            "error": f"Continuous recording of {title} stopped early because the HackRF stopped streaming. What was captured is saved in Recordings.",
        }
        if self.kind == "goaround":
            messages["error"] = f"{title}: recording stopped early because the HackRF stopped streaming."
            message = messages.get(reason, f"{title}: {self.entry['seconds']:.0f} seconds of radio traffic saved in Recordings."
                                   if self.entry.get("full") else f"{title}: {self.bytes / (2 * AUDIO_RATE):.0f} seconds saved in Recordings.")
        else:
            message = messages.get(reason, f"Continuous recording of {title} saved in Recordings ({self.bytes / 1e6:.0f} MB).")
        if reason in ("capped", "disk", "error"):
            radio.notice = message
        print(time.strftime("%F %T"), "continuous", self.name, reason, self.bytes, flush=True)
        for ws in list(self.subscribers):
            try:
                await ws.send_json({"continuous_done": message})
                await ws.close()
            except (ConnectionResetError, RuntimeError):
                pass
        await asyncio.sleep(MONITOR_RESTART_S)
        await radio.after_job()


def acars_db():
    db = sqlite3.connect(str(ACARS_DB))
    db.executescript("""
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, t REAL NOT NULL, freq REAL, level REAL, error INTEGER, mode TEXT, label TEXT,
            block_id TEXT, ack TEXT, tail TEXT, flight TEXT, msgno TEXT, text TEXT, decoded TEXT);
        CREATE INDEX IF NOT EXISTS messages_t ON messages (t);
    """)
    return db


class AcarsJob:
    """ACARS mode: runs acarsdec on the HackRF until a set time and stores every message it decodes."""
    kind = "acars"

    def __init__(self, radio, until, reason=None):
        self.radio, self.until, self.reason = radio, until, reason  # reason is set when started automatically
        self.vdl2 = radio.settings["acars_vdl2"] and DUMPVDL2.exists()
        self.freqs = VDL2_FREQS if self.vdl2 else ACARS_FREQS
        self.title = "ACARS mode (VDL2)" if self.vdl2 else "ACARS mode"
        self.channel = {"id": "acars", "mhz": float(self.freqs[0]), "name": "ACARS"}
        self.count, self.proc, self.task, self.drain_task, self.finished = 0, None, None, None, False
        self.subscribers = set()
        self.db = acars_db()
        self.errors = collections.deque(maxlen=6)

    def start(self):
        self.task = asyncio.create_task(self.run())

    def info(self):
        return {"kind": "acars", "title": self.title, "id": "acars", "name": "ACARS", "mhz": self.channel["mhz"],
                "freqs": self.freqs, "vdl2": self.vdl2, "messages": self.count, "ends_in": max(0, round(self.until - time.time())), "reason": self.reason,
                "seconds": 0, "bytes": 0, "limit": 0}

    @staticmethod
    def from_vdl2(m):
        """dumpvdl2's JSON as acarsdec-style fields; None for frames without an ACARS message (link upkeep)."""
        vdl = m.get("vdl2") if isinstance(m, dict) else None
        avlc = (vdl or {}).get("avlc") or {}
        acars = avlc.get("acars")
        if not isinstance(acars, dict):
            return None
        t = vdl.get("t") or {}
        out = {"timestamp": t.get("sec", time.time()) + t.get("usec", 0) / 1e6, "freq": (vdl.get("freq") or 0) / 1e6,
               "level": vdl.get("sig_level"), "error": 0 if acars.get("crc_ok", True) else 1, "mode": acars.get("mode"),
               "label": acars.get("label"), "block_id": acars.get("blk_id"), "ack": acars.get("ack"),
               "tail": acars.get("reg"), "flight": acars.get("flight"),
               "msgno": (acars.get("msg_num") or "") + (acars.get("msg_num_seq") or "") or None, "text": acars.get("msg_text"),
               "via": "VDL2", "vdl2_from": (avlc.get("src") or {}).get("addr"), "vdl2_to": (avlc.get("dst") or {}).get("addr")}
        plain = {"err", "crc_ok", "more", "reg", "mode", "label", "blk_id", "ack", "flight", "msg_num", "msg_num_seq", "msg_text"}
        out.update({k: v for k, v in acars.items() if k not in plain})  # what libacars decoded (ADS-C, CPDLC, sublabels)
        return out

    def store(self, line):
        try:
            m = json.loads(line)
        except ValueError:
            return
        if self.vdl2:
            m = self.from_vdl2(m)
        if not isinstance(m, dict) or "timestamp" not in m:
            return
        decoded = {k: v for k, v in m.items() if k not in ACARS_PLAIN_FIELDS}
        self.db.execute("INSERT INTO messages (t, freq, level, error, mode, label, block_id, ack, tail, flight, msgno, text, decoded) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (m["timestamp"], m.get("freq"), m.get("level"), m.get("error"), m.get("mode"), m.get("label"),
                         m.get("block_id"), str(m.get("ack")), (m.get("tail") or "").strip(".") or None, m.get("flight") or None,
                         m.get("msgno") or None, m.get("text"), json.dumps(decoded) if decoded else None))
        self.db.commit()
        self.count += 1
        if self.count % ACARS_SIZE_CHECK_EVERY == 0:
            self.prune()

    def prune(self):
        def size():
            return sum(p.stat().st_size for p in (ACARS_DB, Path(str(ACARS_DB) + "-wal")) if p.exists())
        while size() > ACARS_MAX_BYTES:
            deleted = self.db.execute("DELETE FROM messages WHERE id IN (SELECT id FROM messages ORDER BY id LIMIT ?)",
                                      (ACARS_PRUNE_ROWS,)).rowcount
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if not deleted:
                break
            print(time.strftime("%F %T"), "acars: deleted", deleted, "oldest messages to stay under the size cap", flush=True)

    async def drain(self):
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            self.errors.append(line.decode(errors="replace").strip())

    async def run(self):
        if self.vdl2:
            cmd = [str(DUMPVDL2), "--soapysdr", "driver=hackrf", "--centerfreq", str(VDL2_CENTER),
                   "--oversample", str(VDL2_OVERSAMPLE), "--soapy-gain", VDL2_GAINS,
                   "--output", "decoded:json:file:path=-", *[str(int(float(f) * 1e6)) for f in VDL2_FREQS]]
        else:
            cmd = [str(ACARSDEC), "-o", "4", "-g", str(ACARS_GAIN), "-d", "driver=hackrf", *ACARS_FREQS]
        try:
            self.proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.drain_task = asyncio.create_task(self.drain())
            print(time.strftime("%F %T"), self.title, "started until", time.strftime("%F %T", time.localtime(self.until)), flush=True)
            while True:
                remaining = self.until - time.time()
                if remaining <= 0:
                    await self.finish("window")
                    return
                try:
                    line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=min(remaining, 5))
                except asyncio.TimeoutError:
                    continue
                if not line:
                    await self.finish("error")
                    return
                self.store(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            await self.finish("error")

    async def finish(self, reason, resume=True):
        """resume=False when something else is taking the radio straight away, so the band monitor stays off."""
        if self.finished:
            return
        self.finished = True
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
        if self.drain_task:
            self.drain_task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        self.db.close()
        radio = self.radio
        radio.job, radio.listening = None, None
        print(time.strftime("%F %T"), "acars mode ended", reason, self.count, "messages", list(self.errors)[-2:] if reason == "error" else "", flush=True)
        if reason == "error":
            radio.notice = "ACARS mode stopped early: the decoder exited. " + (self.errors[-1] if self.errors else "")
        if resume:
            await asyncio.sleep(MONITOR_RESTART_S)
            await radio.start_monitor()


class CollectJob:
    """One collection group: watches every channel in its 2 MHz block and records conversations until the goal is met.
    Steps aside (finish, then the collection loop starts it again later) for a listener, a go-around or ACARS mode."""
    kind = "collect"

    def __init__(self, radio, group):
        self.radio, self.group, self.goal = radio, group, COLLECTIONS[group]
        self.title = "Collecting: " + self.goal["title"]
        self.center = self.goal["center"]
        self.channel = {"id": "collect-" + group, "mhz": self.center / 1e6, "name": self.goal["title"]}
        self.channels = [c for c in self.goal["channels"]
                         if COLLECT_DC_HZ < abs(c["mhz"] * 1e6 - self.center) < COLLECT_REACH_HZ]
        offsets = np.array([c["mhz"] * 1e6 - self.center for c in self.channels])
        half = int(COLLECT_CHANNEL_HZ / BIN_HZ)
        self.bins = (np.round(offsets / BIN_HZ).astype(int)[:, None] + np.arange(-half, half + 1)) % N
        self.rest_until = {}  # channel index -> monotonic time it can be picked again
        self.on_channel = None
        self.stopped, self.finished, self.thread, self.proc = threading.Event(), False, None, None
        self.subscribers = set()
        self.loop = None

    def start(self):
        self.loop = asyncio.get_running_loop()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def info(self):
        progress = self.radio.collect["groups"][self.group]
        return {"kind": "collect", "title": self.title, "id": self.channel["id"], "name": self.goal["title"],
                "mhz": self.channel["mhz"], "on": self.on_channel, "seconds": round(progress["seconds"]),
                "conversations": progress["conversations"], "goal_seconds": self.goal["seconds"],
                "goal_conversations": self.goal["conversations"], "bytes": 0, "limit": 0,
                "ends_in": max(0, round(self.radio.collect["until"] - time.time()))}

    def remaining_s(self):
        return self.goal["seconds"] - self.radio.collect["groups"][self.group]["seconds"]

    def run(self):
        lna, vga = GAINS["normal"]
        self.proc = subprocess.Popen(["hackrf_transfer", "-r", "-", "-f", str(self.center), "-s", str(FS), "-b", "1750000",
                                      "-a", "0", "-l", str(lna), "-g", str(vga)],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        raw_len = HOP * 2
        tail = np.zeros(HOP, np.complex64)
        snr = np.zeros(len(self.channels))
        armed = np.zeros(len(self.channels), int)
        demod = gate = None
        kept, audio, pending = [], [], bytearray()
        current = peak = None
        open_until = last_open = began = 0.0
        frames = 0
        try:
            while not self.stopped.is_set():
                chunk = self.proc.stdout.read(raw_len)
                if not chunk:
                    break
                pending += chunk
                while len(pending) >= raw_len:
                    block = np.frombuffer(bytes(pending[:raw_len]), dtype=np.int8).astype(np.float32) / 128
                    del pending[:raw_len]
                    iq = block[0::2] + 1j * block[1::2]
                    now = time.monotonic()
                    power = np.abs(fft.fft(np.concatenate([tail, iq]), workers=1)) ** 2
                    tail = iq
                    floor = np.median(power) / math.log(2) + 1e-12
                    level = 10 * np.log10(power[self.bins].mean(axis=1) / floor + 1e-12)
                    snr += 0.25 * (level - snr)
                    if current is None:
                        armed = (armed + 1) * (snr >= COLLECT_TRIGGER_DB)
                        ready = [i for i in np.flatnonzero(armed >= 2) if now >= self.rest_until.get(i, 0)]
                        if not ready or self.remaining_s() <= 0:
                            continue
                        current = max(ready, key=lambda i: snr[i])
                        channel = self.channels[current]
                        demod = Demodulator("FM", channel["mhz"] * 1e6 - self.center)
                        gate, kept, audio, peak, frames = TransmissionGate(False, short_calls=True), [], [], -99.0, 0
                        heard = []
                        began = last_open = now
                        self.on_channel = channel["mhz"]
                    audio.append(demod.process(iq))
                    is_open = snr[current] >= COLLECT_OPEN_DB
                    if is_open:
                        open_until, last_open, peak = now + 0.5, now, max(peak, float(snr[current]))
                    frames += 1
                    if frames >= HOPS_PER_FRAME:
                        frames = 0
                        pcm = (np.clip(np.concatenate(audio), -1, 1) * 32767).astype(np.int16)
                        audio = []
                        if now < open_until:
                            heard.append(pcm)
                        kept += gate.feed(float(snr[current]), now < open_until, pcm)
                    kept_s = sum(map(len, kept)) / AUDIO_RATE
                    if (now - last_open > COLLECT_SILENCE_S or now - began > COLLECT_LONGEST_S
                            or kept_s >= self.remaining_s()):
                        kept += gate.flush()
                        self.conversation_over(self.channels[current], kept, peak, now - began, heard)
                        if not kept:
                            self.rest_until[current] = now + COLLECT_REST_S
                        current, armed[:] = None, 0
                        self.on_channel = None
            if not self.stopped.is_set():
                self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.finish("error")))
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def conversation_over(self, channel, kept, peak, seconds, heard):
        if not kept:
            print(time.strftime("%F %T"), "collect", channel["label"], f"{seconds:.0f} s, nothing kept (static or data)", flush=True)
            if heard and COLLECT_REJECTS.exists() and len(list(COLLECT_REJECTS.glob("*.wav"))) < 40:
                # kept while tuning the gate: what was turned away, to check it really was static or data
                with wave.open(str(COLLECT_REJECTS / f'{time.strftime("%Y%m%d-%H%M%S")}_{channel["mhz"]:.4f}.wav'), "wb") as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(AUDIO_RATE)
                    out.writeframes(np.concatenate(heard).tobytes())
            return
        gap = np.zeros(int(COLLECT_JOIN_S * AUDIO_RATE), np.int16)
        audio = np.concatenate([part for seg in kept for part in (seg, gap)][:-1]).astype(np.int16)
        self.loop.call_soon_threadsafe(self.save, channel, audio, peak, len(kept))

    def save(self, channel, audio, peak, transmissions):
        """On the event loop, so the clip index isn't changed from two threads."""
        self.radio.clips.add(channel, audio, peak, collection=self.goal["title"])
        progress = self.radio.collect["groups"][self.group]
        progress["seconds"] = round(progress["seconds"] + len(audio) / AUDIO_RATE, 1)
        progress["conversations"] += 1
        self.radio.save_collect()
        print(time.strftime("%F %T"), "collect", channel["label"], "saved", transmissions, "transmissions",
              f"{len(audio) / AUDIO_RATE:.0f} s", f"peak {peak:.0f} dB;", self.goal["title"], "now", progress, flush=True)
        if self.radio.collection_done(self.group, progress):
            asyncio.ensure_future(self.finish("goal"))

    async def finish(self, reason, resume=True):
        """resume=False when something else is taking the radio straight away."""
        if self.finished:
            return
        self.finished = True
        self.stopped.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if self.thread:
            await asyncio.get_running_loop().run_in_executor(None, self.thread.join, 5)
        radio = self.radio
        if radio.job is self:
            radio.job, radio.listening = None, None
        if reason == "stopped" and radio.collect:  # the user stopped it: no more collecting tonight
            radio.collect["until"] = min(radio.collect["until"], time.time())
            radio.save_collect()
        print(time.strftime("%F %T"), "collect", self.group, "ended:", reason, flush=True)
        if reason == "error":
            radio.notice = f"{self.title} paused: the HackRF stopped streaming. It will try again shortly."
            radio.collect_pause_until = time.monotonic() + 60
        if resume:
            await asyncio.sleep(MONITOR_RESTART_S)
            await radio.start_monitor()


def parse_throttled(text):
    return int(text.strip().split("=")[1], 16)


class PowerMonitor:
    """Polls the Pi's undervoltage flag; a dip stays reported for POWER_HOLD_S."""

    def __init__(self):
        self.low = False
        self.last_low = None
        self.task = None

    async def run(self):
        while True:
            proc = await asyncio.create_subprocess_exec(
                "vcgencmd", "get_throttled", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
            try:
                low = bool(parse_throttled(out.decode()) & UNDERVOLTAGE_NOW)
            except (ValueError, IndexError):
                low = self.low
            if low != self.low:
                print(time.strftime("%F %T"), "undervoltage" if low else "voltage normal", flush=True)
            self.low = low
            if low:
                self.last_low = time.monotonic()
            await asyncio.sleep(POWER_POLL_S)

    def status(self):
        recent = self.last_low is not None and time.monotonic() - self.last_low < POWER_HOLD_S
        return {"warning": self.low or recent, "now": self.low, "text": UNDERVOLTAGE_TEXT}


class Radio:
    """Owns the HackRF: band monitor when idle, one listener at a time otherwise."""

    def __init__(self, survey, clips):
        self.survey = survey
        self.clips = clips
        self.capturing = None
        self.preempt = asyncio.Event()
        self.capture_done = asyncio.Event()
        self.job = None
        self.notice = ""
        self.cooldown = {}
        self.misses = {}
        self.next_capture = 0.0
        self.hunter_task = None
        self.monitor_task = None
        self.proc = None
        self.error = ""
        self.listening = None
        self.generation = 0
        self.lock = asyncio.Lock()
        self.cancel_listener = None
        self.settings = dict(DEFAULT_SETTINGS)
        try:
            self.settings.update({k: bool(v) for k, v in json.loads(SETTINGS_PATH.read_text()).items() if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass
        self.resume_acars = None  # (until, reason) of ACARS mode paused by a go-around
        self.emergency_task = None
        self.emergencies = []  # what garage_pi last reported squawking in range
        self.emergency_declined = False  # the user stopped emergency ACARS mode; stays off until the emergencies end
        self.saver = None
        try:
            # {start, until, plan: [{group, from, to}], groups: {name: {seconds, conversations}}}
            self.collect = json.loads(COLLECT_PATH.read_text())
        except (OSError, ValueError):
            self.collect = None
        self.collect_task = None
        self.collect_pause_until = 0.0  # after the HackRF fails during a collection, wait before trying again

    @property
    def mode(self):
        if self.listening:
            return "listen"
        return "capture" if self.capturing else "monitor"

    @staticmethod
    def collection_done(name, progress):
        goal = COLLECTIONS[name]
        if goal["conversations"] and progress["conversations"] >= goal["conversations"]:
            return True
        return progress["seconds"] >= goal["seconds"]

    def collecting(self):
        """The collection groups still short of their goal, while a collection's window is open."""
        c = self.collect
        if not c or not c["start"] <= time.time() < c["until"]:
            return {}
        return {name: p for name, p in c["groups"].items() if not self.collection_done(name, p)}

    def current_group(self):
        """The group to collect now: this time slot's, or, once that one has met its goal, any other still short of it."""
        groups = self.collecting()
        if not groups or time.monotonic() < self.collect_pause_until:
            return None
        now = time.time()
        for slot in self.collect.get("plan", []):
            if slot["from"] <= now < slot["to"] and slot["group"] in groups:
                return slot["group"]
        return next(iter(groups))

    async def collect_watch(self):
        """Starts and stops collection jobs; waits whenever someone is listening or another job has the radio."""
        while True:
            await asyncio.sleep(5)
            try:
                group, job = self.current_group(), self.job
                if job and job.kind == "collect":
                    if group != job.group:
                        await job.finish("schedule")
                    continue
                if not group or job or self.listening:
                    continue
                async with self.lock:
                    if self.job or self.listening:
                        continue
                    self.generation += 1
                    if self.capturing:
                        self.preempt.set()
                        await self.capture_done.wait()
                        await asyncio.sleep(0.3)
                    await self.stop_monitor()
                    self.listening = "collect"  # keeps auto-recording and the band monitor off
                    self.job = CollectJob(self, group)
                    self.job.start()
                print(time.strftime("%F %T"), "collect", group, "watching", len(self.job.channels), "channels around",
                      self.job.center / 1e6, "MHz", flush=True)
            except Exception:
                traceback.print_exc()

    async def yield_collection(self, reason):
        """A listener, go-around or ACARS mode takes the radio from a collection; it carries on afterwards."""
        if self.job and self.job.kind == "collect":
            await self.job.finish(reason, resume=False)

    def save_collect(self):
        tmp = COLLECT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.collect))
        tmp.replace(COLLECT_PATH)

    def collect_info(self):
        c = self.collect
        if not c:
            return None
        return {"start": c["start"], "until": c["until"], "open": bool(c["start"] <= time.time() < c["until"]),
                "groups": {name: dict(p, title=COLLECTIONS[name]["title"], goal_seconds=COLLECTIONS[name]["seconds"],
                                      goal_conversations=COLLECTIONS[name]["conversations"],
                                      done=self.collection_done(name, p)) for name, p in c["groups"].items()}}

    def pick_target(self):
        active = self.survey.active()
        now = time.monotonic()
        found = [
            c for c in CAPTURABLE
            if str(c["mhz"]) in active and now >= self.cooldown.get(c["id"], 0)
            and self.survey.pct(c["mhz"]) < CAPTURE_MAX_PCT
        ]
        return max(found, key=lambda c: (c["score"], active[str(c["mhz"])]), default=None)

    async def hunter(self):
        while True:
            await asyncio.sleep(0.5)
            if not self.clips.enabled or self.listening or self.capturing or not self.monitor_task:
                continue
            if time.monotonic() < self.next_capture:
                continue
            target = self.pick_target()
            if target:
                try:
                    await self.capture(target)
                except Exception:
                    traceback.print_exc()
                    self.capturing = None
                    self.capture_done.set()
                    await self.start_monitor()

    async def capture(self, channel):
        async with self.lock:
            if self.listening:
                return
            self.capturing = channel["mhz"]
            self.preempt.clear()
            self.capture_done.clear()
            await self.stop_monitor()
            loop = asyncio.get_running_loop()
            queue = asyncio.Queue()
            listener = Listener(channel, "normal", loop, queue)
            listener.squelch = CAPTURE_SQUELCH
            await loop.run_in_executor(None, listener.start)
        pcm, peak, began, last_open = [], -99.0, time.monotonic(), None
        plan = PLAN_DEFAULT
        if channel["mode"] == "DIGITAL":
            plan = PLAN_DIGITAL
        elif channel["group"] == "aviation" and self.survey.pct(channel["mhz"]) < AVIATION_MAX_PCT:
            plan = PLAN_AVIATION
        try:
            while not self.preempt.is_set():
                try:
                    item = await asyncio.wait_for(queue.get(), 0.5)
                except asyncio.TimeoutError:
                    item = None
                now = time.monotonic()
                if isinstance(item, bytes):
                    snr, _, is_open = struct.unpack("<ffI", item[:12])
                    if is_open:
                        pcm.append(np.frombuffer(item[12:], dtype=np.int16).copy())
                        peak, last_open = max(peak, snr), now
                if last_open is None and now - began > plan.wait:
                    break
                if last_open is not None and now - last_open > plan.silence:
                    break
                if sum(map(len, pcm)) >= plan.longest * AUDIO_RATE:
                    break
        finally:
            await loop.run_in_executor(None, listener.stop)
        saved, outcome = False, "nothing heard"
        if self.preempt.is_set():
            outcome = "interrupted"
        elif pcm:
            audio = np.concatenate(pcm)
            voiced = audio[audio != 0]
            if len(audio) < CAPTURE_MIN_S * AUDIO_RATE:
                outcome = "too short"
            elif not voiced.size or np.sqrt(np.mean(voiced.astype(float) ** 2)) < CAPTURE_MIN_RMS:
                outcome = "too quiet"
            elif channel["mode"] != "DIGITAL" and not speech_like(audio):
                outcome = "not speech"
            else:
                await loop.run_in_executor(None, self.clips.add, channel, audio, peak)
                saved, outcome = True, "saved"
        print(time.strftime("%F %T"), "capture", channel["mhz"], channel["label"], outcome, f"peak {peak:.0f} dB", flush=True)
        misses = 0 if saved else self.misses.get(channel["id"], 0) + 1
        self.misses[channel["id"]] = misses
        delay = plan.saved_cooldown if saved else min(MAX_EMPTY_COOLDOWN_S, plan.empty_cooldown * 2 ** (misses - 1))
        self.cooldown[channel["id"]] = time.monotonic() + delay
        self.next_capture = time.monotonic() + CAPTURE_GAP_S
        preempted = self.preempt.is_set()
        self.capturing = None
        self.capture_done.set()
        if not preempted:
            await asyncio.sleep(MONITOR_RESTART_S)
            await self.start_monitor()

    def pick_departure(self, ids):
        def recency(channel_id):
            return self.survey.last_active[self.survey.idx[round(BY_ID[channel_id]["mhz"], 4)]]

        return BY_ID[max(ids, key=recency)]

    async def start_goaround(self, airport, callsign):
        ids = AIRPORT_CHANNELS.get(airport)
        if not ids:
            print(time.strftime("%F %T"), "go-around ignored, no channel for", airport, callsign, flush=True)
            return {"status": "no channel"}
        async with self.lock:
            await self.yield_collection("go-around")
            if self.job and self.job.kind == "acars" and self.settings["goaround_interrupts_acars"]:
                # Pause ACARS mode for the go-around; after_job() picks it up again until its original end
                self.resume_acars = (self.job.until, self.job.reason)
                print(time.strftime("%F %T"), "acars mode paused for a go-around", flush=True)
                await self.job.finish("go-around", resume=False)
            if self.job:
                print(time.strftime("%F %T"), "go-around not recorded, radio busy:", airport, callsign, flush=True)
                return {"status": "busy", "recording": self.job.title}
            self.generation += 1
            if self.capturing:
                self.preempt.set()
                await self.capture_done.wait()
                await asyncio.sleep(0.3)
            if self.listening:
                await self.cancel_listener("A go-around was detected, so the radio switched to record it.")
                await asyncio.sleep(0.3)
            await self.stop_monitor()
            channel = self.pick_departure(ids)
            loop, queue = asyncio.get_running_loop(), asyncio.Queue()
            listener = Listener(channel, "normal", loop, queue)
            listener.squelch = GOAROUND_SQUELCH
            await loop.run_in_executor(None, listener.start)
            title = f"Go-around: {callsign} at {AIRPORT_NAMES[airport]}"
            self.listening = channel["id"]
            self.job = ContinuousJob(self, channel, listener, queue, None, GOAROUND_WINDOW_S, title, "goaround")
            self.job.start()
        print(time.strftime("%F %T"), "go-around recording", airport, callsign, channel["mhz"], flush=True)
        return {"status": "recording", "mhz": channel["mhz"]}

    async def start_acars(self, until, reason=None):
        async with self.lock:
            await self.yield_collection("ACARS mode")
            if self.job:
                return {"status": "busy", "recording": self.job.title}
            self.generation += 1
            if self.capturing:
                self.preempt.set()
                await self.capture_done.wait()
                await asyncio.sleep(0.3)
            if self.listening:
                await self.cancel_listener("An emergency squawk started ACARS mode, so the radio switched over to it." if reason
                                           else "ACARS mode was turned on, so the radio switched over to it.")
                await asyncio.sleep(0.3)
            await self.stop_monitor()
            self.listening = "acars"  # keeps auto-recording and the band monitor off
            self.job = AcarsJob(self, until, reason)
            self.job.start()
            self.resume_acars = None
        return {"status": "started", "until": until}

    async def after_job(self):
        """When a recording ends: resume ACARS mode that a go-around paused, if it still has time left, else the monitor."""
        resume, self.resume_acars = self.resume_acars, None
        if resume and resume[0] > time.time() + 60:
            print(time.strftime("%F %T"), "acars mode resumed after the go-around", flush=True)
            if (await self.start_acars(*resume))["status"] == "started":
                return
        await self.start_monitor()

    def save_settings(self):
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.settings))
        tmp.replace(SETTINGS_PATH)

    async def emergency_watch(self):
        """Runs ACARS mode while garage_pi reports an emergency squawk in range, unless the user turned that off."""
        while True:
            await asyncio.sleep(EMERGENCY_POLL_S)
            try:
                async with ClientSession() as session:
                    async with session.get(EMERGENCY_URL, timeout=ClientTimeout(total=15)) as reply:
                        data = await reply.json(content_type=None)
                current = [v for v in data.get("emergencies_now") or [] if v.get("squawk") in ("7500", "7600", "7700")]
            except (ClientError, asyncio.TimeoutError, ValueError, AttributeError):
                continue
            names = sorted({f'{v.get("callsign") or v.get("reg") or v.get("hex")} {v["squawk"]}' for v in current})
            if names != sorted({f'{v.get("callsign") or v.get("reg") or v.get("hex")} {v["squawk"]}' for v in self.emergencies}):
                print(time.strftime("%F %T"), "emergency squawks in range:", ", ".join(names) or "none", flush=True)
            self.emergencies = current
            if not current:
                self.emergency_declined = False
                continue
            if not self.settings["emergency_starts_acars"] or self.emergency_declined:
                continue
            until, reason = time.time() + EMERGENCY_HOLD_S, "Emergency squawk: " + ", ".join(names)
            job = self.job
            if job and job.kind == "acars":
                job.until = max(job.until, until)  # keep it going while the emergency lasts
                job.reason = job.reason or reason
            elif self.resume_acars:
                self.resume_acars = (max(self.resume_acars[0], until), self.resume_acars[1] or reason)
            elif not job:  # never interrupts a go-around or the user's own recording; tries again next poll
                if (await self.start_acars(until, reason))["status"] == "started":
                    print(time.strftime("%F %T"), "acars mode started for", reason, flush=True)

    async def monitor(self):
        args = ["hackrf_sweep"]
        for band in SWEEP_BANDS:
            args += ["-f", band]
        args += ["-w", "13333", "-P", "estimate", "-l", str(SWEEP_LNA), "-g", str(SWEEP_VGA), "-a", "0", "-B"]
        last_low, marked, failures = None, time.monotonic(), 0
        while True:
            self.proc = await asyncio.create_subprocess_exec(
                *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            tail = collections.deque(maxlen=6)
            drainer = asyncio.create_task(self.drain(self.proc.stderr, tail))
            try:
                while True:
                    size = struct.unpack("<I", await self.proc.stdout.readexactly(4))[0]
                    record = await self.proc.stdout.readexactly(size)
                    low, high = struct.unpack("<QQ", record[:16])
                    self.survey.ingest(low, high, np.frombuffer(record[16:], dtype=np.float32))
                    await asyncio.sleep(0)
                    self.error, failures = "", 0
                    if last_low is None:
                        last_low = low
                    elif low == last_low:
                        now = time.monotonic()
                        self.survey.sweep_seconds += now - marked
                        marked = now
            except asyncio.IncompleteReadError:
                failures += 1
                if failures >= WARN_AFTER_FAILURES:
                    self.error = "Band monitor stopped. The HackRF may need replugging."
                await self.reap()
                drainer.cancel()
                print(time.strftime("%F %T"), "hackrf_sweep exited", self.proc.returncode, list(tail), flush=True)
                if failures >= RESET_AFTER_FAILURES:
                    await self.reset_hackrf()
                await asyncio.sleep(1)
                marked = time.monotonic()
            except asyncio.CancelledError:
                drainer.cancel()
                await self.reap()
                raise

    @staticmethod
    async def reset_hackrf():
        """The HackRF can stop streaming while still answering USB requests; a reset clears it."""
        proc = await asyncio.create_subprocess_exec(
            "hackrf_spiflash", "-R", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            await asyncio.wait_for(proc.wait(), 15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        print(time.strftime("%F %T"), "hackrf reset, exit", proc.returncode, flush=True)
        await asyncio.sleep(4)

    @staticmethod
    async def drain(stream, tail):
        async for line in stream:
            tail.append(line.decode(errors="replace").strip())

    async def reap(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()

    async def start_monitor(self):
        if not self.monitor_task and not self.job and not self.listening and not self.capturing:
            self.monitor_task = asyncio.create_task(self.monitor())

    async def stop_monitor(self):
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None
            self.error = ""
            await asyncio.sleep(0.3)


async def index(_request):
    return web.FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


async def bluetooth_redirect(_request):
    raise web.HTTPFound("/bluetooth/")


async def bluetooth_page(_request):
    return web.FileResponse(BLUETOOTH_PAGE, headers={"Cache-Control": "no-cache"})


async def bluetooth_data(_request):
    if not BLUETOOTH_DATA.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(BLUETOOTH_DATA, headers={"Cache-Control": "no-store"})


async def wifi_redirect(_request):
    raise web.HTTPFound("/wifi/")


async def wifi_page(_request):
    return web.FileResponse(WIFI_PAGE, headers={"Cache-Control": "no-cache"})


async def wifi_data(_request):
    if not WIFI_DATA.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(WIFI_DATA, headers={"Cache-Control": "no-store"})


async def status_redirect(_request):
    raise web.HTTPFound("/status/")


async def status_page(_request):
    return web.FileResponse(STATUS_WWW / "index.html", headers={"Cache-Control": "no-cache"})


async def status_script(_request):
    return web.FileResponse(STATUS_WWW / "alerts.js")


async def status_data(_request):
    if not STATUS_DATA.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(STATUS_DATA, headers={"Cache-Control": "no-store"})


async def status_power(request):
    headers = {key: request.headers[key] for key in ("Content-Type", "Origin", "Host") if key in request.headers}
    headers["X-Forwarded-For"] = request.remote or ""
    try:
        async with ClientSession() as session:
            async with session.post(STATUS_API, data=await request.read(), headers=headers, timeout=ClientTimeout(total=5)) as reply:
                return web.Response(status=reply.status, body=await reply.read(), content_type="application/json")
    except (ClientError, asyncio.TimeoutError):
        return web.json_response({"error": "status service not answering"}, status=503)


async def flights_redirect(_request):
    raise web.HTTPFound("/flights/")


async def flights_page(_request):
    return web.FileResponse(FLIGHTS_PAGE, headers={"Cache-Control": "no-cache"})


async def flights_map_library(request):
    return web.FileResponse(FLIGHTS_PAGE.parent / "ol" / request.match_info["name"])


async def flights_api(request):
    try:
        async with ClientSession() as session:
            async with session.get(FLIGHTS_API + request.path_qs, timeout=ClientTimeout(total=30)) as reply:
                # Readable from any page on the LAN, for garage_pi's display page
                return web.Response(status=reply.status, body=await reply.read(), content_type="application/json",
                                    headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})
    except (ClientError, asyncio.TimeoutError):
        return web.json_response({"error": "flight store not answering"}, status=503)


async def cars_redirect(_request):
    raise web.HTTPFound("/cars/")


async def cars_page(_request):
    return web.FileResponse(CARS_PAGE, headers={"Cache-Control": "no-cache"})


async def weather_redirect(_request):
    raise web.HTTPFound("/weather/")


async def weather_page(_request):
    return web.FileResponse(WEATHER_PAGE, headers={"Cache-Control": "no-cache"})


async def cars_api(request):
    """Also carries /weather/api/: the car store keeps the porch weather sensor too."""
    try:
        async with ClientSession() as session:
            async with session.request(request.method, CARS_API + request.path_qs, data=await request.read(),
                                       headers={"Content-Type": "application/json"}, timeout=ClientTimeout(total=30)) as reply:
                headers = {"Cache-Control": "no-store"}
                if request.method == "GET" and request.path.startswith("/weather/api/"):
                    headers["Access-Control-Allow-Origin"] = "*"  # garage_pi's display shows the temperature; car data stays same-origin
                return web.Response(status=reply.status, body=await reply.read(), content_type="application/json", headers=headers)
    except (ClientError, asyncio.TimeoutError):
        return web.json_response({"error": "car store not answering"}, status=503)


async def health(request):
    radio = request.app["radio"]
    return web.json_response({"error": radio.error, "listening": bool(radio.listening),
                              "recording": radio.job.title if radio.job else None,
                              "collecting": sorted(radio.collecting())})


async def power_status(request):
    return web.json_response(request.app["power"].status())


async def power_script(_request):
    return web.FileResponse(HERE / "static" / "power.js")


async def icon(request):
    # Browsers ask for these at the root, so every page on this port gets them
    return web.FileResponse(HERE / "static" / request.path.lstrip("/"), headers={"Cache-Control": "max-age=86400"})


async def channels(_request):
    return web.json_response(CHANNELS)


async def survey_stats(request):
    return web.json_response(request.app["survey"].stats())


async def state_socket(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    radio, survey = request.app["radio"], request.app["survey"]

    async def pump():
        while not ws.closed:
            await ws.send_json(
                {
                    "mode": radio.mode,
                    "error": radio.error,
                    "tuned": radio.listening,
                    "capturing": radio.capturing,
                    "capture": radio.clips.enabled,
                    "clips": len(radio.clips.items),
                    "continuous": radio.job.info() if radio.job else None,
                    "notice": radio.notice,
                    "active": survey.active() if radio.mode == "monitor" else {},
                    "seconds": round(survey.sweep_seconds),
                }
            )
            await asyncio.sleep(0.5)

    sender = asyncio.create_task(pump())
    try:
        async for _ in ws:
            pass
    finally:
        sender.cancel()
    return ws


async def listen_socket(request):
    channel = BY_ID.get(request.query.get("id", ""))
    if not channel or not channel["listen"]:
        raise web.HTTPBadRequest(text="unknown or non-listenable channel")
    try:
        initial_squelch = float(request.query.get("squelch", 8))
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="invalid squelch")
    if not math.isfinite(initial_squelch):
        raise web.HTTPBadRequest(text="invalid squelch")
    radio = request.app["radio"]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    await radio.yield_collection("someone is listening")
    if radio.job:
        job = radio.job
        if job.kind == "acars":
            await ws.send_json({"error": "ACARS mode is using the radio. Stop it first to listen."})
            await ws.close()
            return ws
        if job.channel["id"] != channel["id"]:
            await ws.send_json({"error": f'{job.title} is recording on {job.channel["mhz"]:.4f}. Stop it first to tune elsewhere.'})
            await ws.close()
            return ws
        job.subscribers.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        command = json.loads(msg.data)
                    except ValueError:
                        continue
                    if isinstance(command, dict) and command.get("continuous") is False:
                        await job.finish("stopped")
        finally:
            job.subscribers.discard(ws)
        return ws
    async with radio.lock:
        radio.generation += 1
        generation = radio.generation
        if radio.capturing:
            radio.preempt.set()
            await radio.capture_done.wait()
            await asyncio.sleep(0.3)
        if radio.listening:
            await radio.cancel_listener()
            await asyncio.sleep(0.3)
        await radio.stop_monitor()
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        listener = Listener(channel, request.query.get("gain", "normal"), loop, queue)
        listener.squelch = initial_squelch
        done = asyncio.Event()

        async def cancel(message=None):
            if message:
                await ws.send_json({"error": message})
            await ws.close()
            await done.wait()

        radio.cancel_listener = cancel
        radio.listening = channel["id"]
        await loop.run_in_executor(None, listener.start)

    take = {"pcm": None, "samples": 0, "peak": -99.0, "gate": None, "heard": [], "heard_samples": 0}

    def keep(chunks):
        for audio in chunks:
            take["pcm"].append(audio)
            take["samples"] += len(audio)

    def new_gate(command):
        return TransmissionGate(channel["mode"] == "DIGITAL") if command.get("transmissions", True) else None

    async def finish_take():
        """Saves the recording in progress and returns the message for the page. If the filter rejected everything,
        saves what passed the squelch instead, so a recording is only empty when nothing could be heard."""
        if take["pcm"] is not None and take["gate"]:
            keep(take["gate"].flush())
        pcm, samples, heard, heard_samples = take["pcm"], take["samples"], take["heard"], take["heard_samples"]
        take.update(pcm=None, heard=[], heard_samples=0)
        unfiltered = samples < MIN_RECORD_S * AUDIO_RATE
        if unfiltered:
            pcm, samples = heard, heard_samples
        if not pcm or samples < MIN_RECORD_S * AUDIO_RATE:
            return {"record_done": 0}
        await loop.run_in_executor(None, radio.clips.add, channel, np.concatenate(pcm), take["peak"], True)
        return {"record_done": round(samples / AUDIO_RATE, 1), "unfiltered": unfiltered and bool(take["gate"])}

    async def pump():
        while True:
            item = await queue.get()
            if isinstance(item, bytes):
                if take["pcm"] is not None:
                    snr, _, is_open = struct.unpack("<ffI", item[:12])
                    samples = np.frombuffer(item[12:], dtype=np.int16).copy()
                    if take["gate"]:
                        keep(take["gate"].feed(snr, is_open, samples))
                    elif is_open:
                        keep([samples])
                    if is_open:
                        take["peak"] = max(take["peak"], snr)
                        take["heard"].append(samples)
                        take["heard_samples"] += len(samples)
                    if take["heard_samples"] >= MAX_RECORD_S * AUDIO_RATE:
                        await ws.send_json(await finish_take())
                await ws.send_bytes(item)
            else:
                await ws.send_json(item)

    sender = asyncio.create_task(pump())
    job = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    command = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(command, dict):
                    continue
                if "squelch" in command:
                    try:
                        squelch = float(command["squelch"])
                    except (ValueError, TypeError):
                        continue
                    if math.isfinite(squelch):
                        listener.squelch = squelch
                if command.get("continuous") is True and job is None:
                    sender.cancel()
                    try:
                        await sender
                    except asyncio.CancelledError:
                        pass
                    if take["pcm"] is not None:
                        await ws.send_json(await finish_take())
                    job = radio.job = ContinuousJob(radio, channel, listener, queue, new_gate(command))
                    job.subscribers.add(ws)
                    job.start()
                elif command.get("continuous") is False and job:
                    await job.finish("stopped")
                elif command.get("record") is True and take["pcm"] is None:
                    take.update(pcm=[], samples=0, peak=-99.0, gate=new_gate(command), heard=[], heard_samples=0)
                elif command.get("record") is False:
                    await ws.send_json(await finish_take())
    finally:
        sender.cancel()
        if job:
            job.subscribers.discard(ws)
        else:
            await finish_take()
            await loop.run_in_executor(None, listener.stop)
            done.set()
            if radio.generation == generation:
                radio.listening = None
                await asyncio.sleep(MONITOR_RESTART_S)
                await radio.start_monitor()
    return ws


async def clip_list(request):
    return web.json_response(request.app["radio"].clips.items)


async def clip_file(request):
    name = request.match_info["name"]
    if not CLIP_NAME.match(name):
        raise web.HTTPBadRequest()
    path = request.app["radio"].clips.dir / name
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})  # go-around recordings are rewritten when trimmed


async def clip_delete(request):
    name = request.match_info["name"]
    if not CLIP_NAME.match(name):
        raise web.HTTPBadRequest()
    job = request.app["radio"].job
    if job and getattr(job, "name", None) == name:
        raise web.HTTPConflict(text="stop the continuous recording first")
    request.app["radio"].clips.remove(name)
    return web.json_response({"ok": True})


async def goaround_trigger(request):
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest()
    if not isinstance(body, dict):
        raise web.HTTPBadRequest()
    callsign = re.sub(r"[^A-Z0-9-]", "", str(body.get("callsign") or "").upper())[:10] or "unknown"
    return web.json_response(await request.app["radio"].start_goaround(str(body.get("airport", "")), callsign))


async def collect_start(request):
    """{"plan": [{"group": "railway", "from": unix, "to": unix}, ...]}: which group to collect in each time slot.
    A group that meets its goal hands the rest of its slot to any group still short of its own."""
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("expected object")
        plan = [{"group": str(s["group"]), "from": float(s["from"]), "to": float(s["to"])} for s in body["plan"]]
    except (ValueError, TypeError, KeyError, AttributeError):
        raise web.HTTPBadRequest(text="needs a plan of {group, from, to}")
    if not plan or any(not (math.isfinite(s["from"]) and math.isfinite(s["to"])) for s in plan):
        raise web.HTTPBadRequest(text="needs a plan of {group, from, to}")
    start, until = min(s["from"] for s in plan), max(s["to"] for s in plan)
    if (any(s["group"] not in COLLECTIONS or s["from"] >= s["to"] for s in plan)
            or until <= time.time() or until - start > 24 * 3600):
        raise web.HTTPBadRequest(text="groups must be among %s, and the plan in the future and under 24 hours" % ", ".join(COLLECTIONS))
    radio = request.app["radio"]
    if radio.job and radio.job.kind == "collect":
        await radio.job.finish("new plan")
    radio.collect = {"start": start, "until": until, "plan": plan,
                     "groups": {s["group"]: {"seconds": 0.0, "conversations": 0} for s in plan}}
    radio.save_collect()
    for s in plan:
        print(time.strftime("%F %T"), "collection planned:", s["group"], time.strftime("%F %T", time.localtime(s["from"])), "to",
              time.strftime("%F %T", time.localtime(s["to"])), flush=True)
    return web.json_response(radio.collect_info())


async def collect_status(request):
    return web.json_response(request.app["radio"].collect_info())


async def collect_stop(request):
    radio = request.app["radio"]
    if radio.job and radio.job.kind == "collect":
        await radio.job.finish("stopped")
    elif radio.collect:
        radio.collect["until"] = min(radio.collect["until"], time.time())
        radio.save_collect()
    return web.json_response(radio.collect_info())


async def continuous_stop(request):
    job = request.app["radio"].job
    if job:
        await job.finish("stopped")
    return web.json_response({"ok": True})


async def acars_start(request):
    try:
        until = float((await request.json()).get("until"))
    except (ValueError, TypeError, AttributeError):
        raise web.HTTPBadRequest(text="until must be a time")
    now = time.time()
    if not now + 60 <= until <= now + ACARS_MAX_HOURS * 3600:
        raise web.HTTPBadRequest(text="pick an end between a minute and %d hours away" % ACARS_MAX_HOURS)
    return web.json_response(await request.app["radio"].start_acars(until))


async def acars_stop(request):
    radio = request.app["radio"]
    job = radio.job
    if job and job.kind == "acars":
        if job.reason:
            radio.emergency_declined = True  # don't start it again for the emergencies already in progress
        await job.finish("stopped")
    radio.resume_acars = None
    return web.json_response({"ok": True})


async def acars_settings(request):
    radio = request.app["radio"]
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        radio.settings.update({k: bool(v) for k, v in body.items() if k in DEFAULT_SETTINGS})
        radio.save_settings()
    return web.json_response({**radio.settings, "emergencies": radio.emergencies, "declined": radio.emergency_declined})


# Canadian area control centres, by the first three letters of their CPDLC ground address
ATC_CENTRES = {"YYZ": "Toronto Centre", "YUL": "Montreal Centre", "YWG": "Winnipeg Centre", "YQM": "Moncton Centre",
               "YQX": "Gander Oceanic and Domestic", "YEG": "Edmonton Centre", "YVR": "Vancouver Centre",
               "USA": "the FAA's Data Comm system for US airspace"}
# FANS-1/A message kinds, from the three letters after the ground address in the text
FANS_IMI = {
    "AT1": "Controller–pilot datalink (CPDLC): air traffic control and the pilots texting instead of talking",
    "CR1": "Air traffic control opens a CPDLC connection with the aircraft",
    "CC1": "The aircraft confirms its CPDLC connection with air traffic control (logged on)",
    "DR1": "The aircraft closes its CPDLC connection with this control centre, usually on leaving its airspace",
    "ADS": "Automatic position report (ADS-C) for air traffic control",
}
# The common CPDLC message elements in everyday words (libacars gives the formal wording as choice_label)
CPDLC_MEANINGS = {
    "WILCO": "will comply: the pilots accept the instruction",
    "UNABLE": "the pilots can't do what was asked",
    "STANDBY": "wait, an answer is coming",
    "ROGER": "message received",
    "AFFIRM": "yes",
    "NEGATIVE": "no",
    "LOGICAL ACKNOWLEDGEMENT": "an automatic receipt that the message arrived",
    "REQUEST VOICE CONTACT [frequency]": "the pilots want to talk by radio on this frequency",
    "ERROR [errorinformation]": "something in the exchange went wrong",
    "NOT CURRENT DATA AUTHORITY": "the plane is talking by datalink with a different control centre, so it turned this one's message away",
}


class AcarsExplainer:
    """Turns an ACARS message into plain-English lines: route, positions and dates found in the text, and the fields
    libacars decoded (ADS-C position reports and the like). Airline formats vary, so anything unrecognised stays raw."""

    def __init__(self):
        try:
            rows = json.loads((FLIGHTS_APP / "airports.json").read_text())
            self.airports = {r[0]: r[1] for r in rows}
        except (OSError, ValueError):
            self.airports = {}
        try:
            self.types = json.loads((FLIGHTS_APP / "types.json").read_text())
        except (OSError, ValueError):
            self.types = {}
        try:
            self.home = json.loads((FLIGHTS_APP / "local.json").read_text())["home"]
        except (OSError, ValueError, KeyError):
            self.home = None

    def place(self, lat, lon):
        text = "%.3f°%s, %.3f°%s" % (abs(lat), "N" if lat >= 0 else "S", abs(lon), "E" if lon >= 0 else "W")
        km = self.km(lat, lon)
        return text + (" (%.0f km from the house)" % km if km is not None else "")

    def km(self, lat, lon):
        if not self.home:
            return None
        x = (lon - self.home[1]) * 111.32 * math.cos(math.radians(self.home[0]))
        return math.hypot(x, (lat - self.home[0]) * 110.574)

    def airport(self, code):
        return "%s (%s)" % (self.airports[code], code) if code in self.airports else code

    @staticmethod
    def compass(degrees):
        return ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"][round(degrees / 45) % 8]

    @staticmethod
    def climb(fpm):
        if fpm is None:
            return None
        return "level" if abs(fpm) < 100 else ("climbing %d ft/min" % fpm if fpm > 0 else "descending %d ft/min" % -fpm)

    def from_text(self, text):
        lines = []
        first = text.split(",", 1)[0].strip()
        if first in self.types:
            lines.append("Aircraft: " + self.types[first])
        for match in ACARS_AIRPORT_PAIR.finditer(text):
            origin, destination = match.groups()
            if origin != destination and origin in self.airports and destination in self.airports:
                lines.append("Route: %s → %s" % (self.airport(origin), self.airport(destination)))
                break
        number = ACARS_FLIGHT_NUMBER.search(text)
        if number:
            lines.append("Flight number: " + number.group(1))
        date = ACARS_DATE.search(text)
        if date:
            lines.append("Date: %s %d" % (date.group(1).title(), int(date.group(2))))
        points = []
        for ns, lat_d, lat_t, ew, lon_d, lon_t in ACARS_POSITION.findall(text):
            lat = (int(lat_d) + int(lat_t) / 1000) * (1 if ns == "N" else -1)
            lon = (int(lon_d) + int(lon_t) / 1000) * (1 if ew == "E" else -1)
            km = self.km(lat, lon)
            if abs(lat) <= 90 and abs(lon) <= 180 and (km is None or km <= ACARS_NEAR_KM):
                points.append((lat, lon))
        if points:
            shown = "; ".join(self.place(*p) for p in points[:ACARS_MAX_POINTS])
            more = len(points) - ACARS_MAX_POINTS
            lines.append("Position%s: %s%s" % ("s" if len(points) > 1 else "", shown, " and %d more" % more if more > 0 else ""))
        if "EXTEND/DOWN" in text:
            lines.append("Reports the landing gear extended and down")
        return lines

    def adsc(self, tags):
        lines = []
        for tag in tags:
            for name, d in tag.items():
                if not isinstance(d, dict):
                    continue
                if name == "basic_report":
                    lines.append("Position %s at %s ft, accurate to %s nm%s" % (
                        self.place(d["lat"], d["lon"]), format(d.get("alt", 0), ","), d.get("pos_accuracy_nm", "?"),
                        ", collision avoidance (TCAS) available" if d.get("tcas_avail") else ""))
                elif name == "earth_ref_data":
                    parts = []
                    if d.get("true_trk_deg") is not None:
                        parts.append("track %.0f° (%s)" % (d["true_trk_deg"], self.compass(d["true_trk_deg"])))
                    if d.get("gnd_spd_kts") is not None:
                        parts.append("ground speed %.0f knots" % d["gnd_spd_kts"])
                    parts.append(self.climb(d.get("vspd_ftmin")))
                    lines.append("Moving: " + ", ".join(p for p in parts if p))
                elif name == "air_ref_data":
                    parts = []
                    if d.get("true_hdg_deg") is not None:
                        parts.append("heading %.0f°" % d["true_hdg_deg"])
                    if d.get("spd_mach") is not None:
                        parts.append("Mach %.2f" % d["spd_mach"])
                    parts.append(self.climb(d.get("vspd_ftmin")))
                    lines.append("Through the air: " + ", ".join(p for p in parts if p))
                elif name == "predicted_route":
                    nxt, after = d.get("next_wpt") or {}, d.get("next_next_wpt") or {}
                    if nxt:
                        eta = " in %d s" % nxt["eta_sec"] if nxt.get("eta_sec") is not None else ""
                        lines.append("Next waypoint %s at %s ft%s" % (self.place(nxt["lat"], nxt["lon"]), format(nxt.get("alt", 0), ","), eta))
                    if after:
                        lines.append("Then %s" % self.place(after["lat"], after["lon"]))
                elif name == "meteo":
                    parts = []
                    if d.get("wind_spd_kts") is not None:
                        parts.append("wind %.0f knots from %.0f°" % (d["wind_spd_kts"], d.get("wind_dir_true_deg", 0)))
                    if d.get("temp_c") is not None:
                        parts.append("outside air %.0f °C" % d["temp_c"])
                    lines.append("Weather at the aircraft: " + ", ".join(parts))
                elif name == "flight_id":
                    lines.append("Flight: %s" % d.get("id", d))
                else:
                    lines.extend(self.fields(name, d))
        return lines

    @staticmethod
    def atc_unit(address):
        """CPDLC ground addresses start with the area control centre's three-letter code."""
        centre = ATC_CENTRES.get(address[:3])
        return "%s (%s)" % (address, centre) if centre else address

    def cpdlc(self, msg):
        """One CPDLC message: who sent it, what it says in plain words, and what it answers."""
        if msg.get("err"):
            return ["The CPDLC content couldn't be decoded"]
        lines = []
        for key, sender in (("atc_uplink_msg", "Controller to pilots"), ("atc_downlink_msg", "Pilots to controller")):
            body = msg.get(key)
            if not isinstance(body, dict):
                continue
            header = body.get("header") or {}
            elements = [body.get(k) for k in body if k.startswith("atc_") and k.endswith("_element_id")]
            elements += [e for k, v in body.items() if k.endswith("_element_id_seq") and isinstance(v, list) for e in v]
            for element in elements:
                if not isinstance(element, dict):
                    continue
                label = element.get("choice_label") or element.get("choice") or "?"
                if label == "[versionnumber]":  # the logon handshake, not a message anyone typed
                    version = (element.get("data") or {}).get("ver_num")
                    return ["Agrees to use CPDLC version %s" % version if version is not None else "Agrees on the CPDLC version"]
                meaning = CPDLC_MEANINGS.get(label.strip().upper())
                lines.append("%s: %s%s" % (sender, label, " (%s)" % meaning if meaning else ""))
                lines.extend("  " + detail for detail in self.fields("data", element.get("data") or {}))
            stamp = header.get("timestamp") or {}
            sent = ""
            if stamp:
                utc = datetime.datetime.now(datetime.timezone.utc).replace(hour=stamp.get("hour", 0), minute=stamp.get("min", 0),
                                                                           second=stamp.get("sec", 0), microsecond=0)
                sent = " sent at %s UTC (%s here)" % (utc.strftime("%H:%M:%S"), utc.astimezone().strftime("%H:%M:%S"))
            ref = header.get("msg_ref")
            lines.append("Message #%s%s%s" % (header.get("msg_id", "?"), sent,
                                               ", answering the controller's message #%s" % ref if ref is not None and key == "atc_downlink_msg"
                                               else ", answering the pilots' message #%s" % ref if ref is not None else ""))
        return lines

    def fields(self, name, value, depth=0):
        """Unrecognised decoded content, as 'Some field: value' lines rather than JSON."""
        label = name.replace("_", " ").capitalize()
        if isinstance(value, dict):
            lines = []
            for key, inner in value.items():
                lines.extend(self.fields(key, inner, depth + 1) if depth < 4 else [])
            return lines
        if isinstance(value, list):
            return [line for item in value[:10] for line in self.fields(name, item, depth + 1)]
        if isinstance(value, bool):
            value = "yes" if value else "no"
        return ["%s: %s" % (label, value)]

    def explain(self, row):
        lines = []
        decoded = row.get("decoded") or {}
        if decoded.get("sublabel"):
            lines.append("Sent %s" % ACARS_SUBLABELS.get(decoded["sublabel"], "by onboard system %s" % decoded["sublabel"]))
        # acarsdec nests libacars' decode under "libacars"; dumpvdl2 (VDL2) puts it straight in the message
        a622 = (decoded.get("libacars") or {}).get("arinc622") or decoded.get("arinc622") or {}
        if a622:
            kind = a622.get("msg_type", "").replace("fans1a_", "")
            kinds = {"adsc_msg": "Automatic position report (ADS-C) for air traffic control",
                     "cpdlc_msg": "Controller–pilot datalink (CPDLC): air traffic control and the pilots texting instead of talking"}
            # the text starts "/<ground address>.<IMI>.<registration>"; the IMI names the kind of message
            imi = re.search(r"\.([A-Z]{2}[A-Z0-9])\.", row.get("text") or "")
            imi = imi.group(1) if imi else ""
            lines.append(FANS_IMI.get(imi) or kinds.get(kind, "Air traffic services message"))
            if a622.get("gs_addr"):
                lines.append("Air traffic control datalink address: " + self.atc_unit(a622["gs_addr"]))
            if a622.get("crc_ok") is False:
                lines.append("Checksum failed, so parts may be garbled")
            if isinstance(a622.get("adsc"), dict):
                lines.extend(self.adsc(a622["adsc"].get("tags") or []))
            if isinstance(a622.get("cpdlc"), dict):
                lines.extend(self.cpdlc(a622["cpdlc"]))
            for key, value in a622.items():
                if key not in ("msg_type", "gs_addr", "air_addr", "crc_ok", "adsc", "cpdlc"):
                    lines.extend(self.fields(key, value))
        elif decoded.get("libacars"):
            lines.extend(self.fields("decoded", decoded["libacars"]))
        if row.get("text") and not a622:
            lines.extend(self.from_text(row["text"]))
        return lines


ACARS_EXPLAINER = None


async def acars_messages(request):
    after = request.query.get("after", "0")
    after = int(after) if after.isdigit() else 0
    if not ACARS_DB.exists():
        return web.json_response({"messages": [], "summary": {}, "freqs": ACARS_FREQS})

    def read():
        db = sqlite3.connect("file:%s?mode=ro" % ACARS_DB, uri=True)
        try:
            cursor = db.execute("SELECT * FROM messages WHERE id > ? ORDER BY id DESC LIMIT ?", (after, ACARS_PAGE_LIMIT))
            names = [d[0] for d in cursor.description]
            rows = [dict(zip(names, r)) for r in cursor.fetchall()]
            day = time.time() - 86400
            summary = dict(zip(["messages", "aircraft", "flights", "first"], db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT tail), COUNT(DISTINCT flight), MIN(t) FROM messages WHERE t >= ?", (day,)).fetchone()))
            summary["by_freq"] = [list(r) for r in db.execute("SELECT freq, COUNT(*) FROM messages WHERE t >= ? GROUP BY 1 ORDER BY 2 DESC", (day,))]
            summary["by_label"] = [list(r) for r in db.execute("SELECT label, COUNT(*) FROM messages WHERE t >= ? GROUP BY 1 ORDER BY 2 DESC", (day,))]
            return rows, summary
        finally:
            db.close()

    global ACARS_EXPLAINER
    if ACARS_EXPLAINER is None:
        ACARS_EXPLAINER = AcarsExplainer()
    rows, summary = await asyncio.get_running_loop().run_in_executor(None, read)
    for r in rows:
        r["label_name"] = ACARS_LABELS.get(r["label"]) or "Label %s: meaning varies by airline" % r["label"]
        # A digit block id is sent by the aircraft; a letter comes from the ground
        r["direction"] = "from aircraft" if (r["block_id"] or "").isdigit() else "from ground" if r["block_id"] else ""
        r["decoded"] = json.loads(r["decoded"]) if r["decoded"] else None
        r["summary"] = ACARS_EXPLAINER.explain(r)
    for pair in summary["by_label"]:
        pair.append(ACARS_LABELS.get(pair[0]) or "Label %s" % pair[0])
    job = request.app["radio"].job
    return web.json_response({"messages": rows, "summary": summary, "freqs": ACARS_FREQS,
                              "running": job.info() if job and job.kind == "acars" else None})


async def acars_redirect(_request):
    raise web.HTTPFound("/acars/")


async def acars_page(_request):
    return web.FileResponse(HERE / "static" / "acars.html", headers={"Cache-Control": "no-cache"})


async def continuous_dismiss(request):
    request.app["radio"].notice = ""
    return web.json_response({"ok": True})


async def capture_toggle(request):
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest()
    if not isinstance(body, dict):
        raise web.HTTPBadRequest()
    clips = request.app["radio"].clips
    clips.enabled = bool(body.get("on"))
    clips.persist()
    return web.json_response({"on": clips.enabled})


async def saver(app):
    while True:
        await asyncio.sleep(60)
        app["survey"].save()


async def on_start(app):
    await app["radio"].start_monitor()
    app["radio"].saver = asyncio.create_task(saver(app))
    app["radio"].hunter_task = asyncio.create_task(app["radio"].hunter())
    app["radio"].emergency_task = asyncio.create_task(app["radio"].emergency_watch())
    app["radio"].collect_task = asyncio.create_task(app["radio"].collect_watch())
    app["power"].task = asyncio.create_task(app["power"].run())


async def on_stop(app):
    if app["radio"].job:
        await app["radio"].job.finish("shutdown")
    app["radio"].saver.cancel()
    app["radio"].hunter_task.cancel()
    app["radio"].emergency_task.cancel()
    app["radio"].collect_task.cancel()
    app["power"].task.cancel()
    app["radio"].preempt.set()
    await app["radio"].stop_monitor()
    app["survey"].save()


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    app = web.Application()
    app["survey"] = Survey()
    app["radio"] = Radio(app["survey"], ClipStore())
    app["power"] = PowerMonitor()
    app.on_startup.append(on_start)
    app.on_shutdown.append(on_stop)
    app.add_routes(
        [
            web.get("/", index),
            web.get("/power.js", power_script),
            web.get("/favicon.ico", icon),
            web.get("/apple-touch-icon.png", icon),
            web.get("/api/power", power_status),
            web.get("/bluetooth", bluetooth_redirect),
            web.get("/bluetooth/", bluetooth_page),
            web.get("/bluetooth/data/data.json", bluetooth_data),
            web.get("/wifi", wifi_redirect),
            web.get("/wifi/", wifi_page),
            web.get("/wifi/data/data.json", wifi_data),
            web.get("/status", status_redirect),
            web.get("/status/", status_page),
            web.get("/status/alerts.js", status_script),
            web.get("/status/data/status.json", status_data),
            web.post("/status/api/power", status_power),
            web.get("/api/health", health),
            web.get("/cars", cars_redirect),
            web.get("/cars/", cars_page),
            web.get(r"/cars/api/{name:[a-z]+}", cars_api),
            web.post(r"/cars/api/{name:band}", cars_api),
            web.get("/weather", weather_redirect),
            web.get("/weather/", weather_page),
            web.get(r"/weather/api/{name:[a-z]+}", cars_api),
            web.post(r"/weather/api/{name:station}", cars_api),
            web.get("/flights", flights_redirect),
            web.get("/flights/", flights_page),
            web.get(r"/flights/api/{name:[a-z]+}", flights_api),
            web.get(r"/flights/ol/{name:ol\.(js|css)}", flights_map_library),
            web.get("/api/channels", channels),
            web.get("/api/survey", survey_stats),
            web.get("/api/clips", clip_list),
            web.delete("/api/clips/{name}", clip_delete),
            web.post("/api/capture", capture_toggle),
            web.post("/api/goaround", goaround_trigger),
            web.get("/api/collect", collect_status),
            web.post("/api/collect", collect_start),
            web.post("/api/collect/stop", collect_stop),
            web.post("/api/continuous/stop", continuous_stop),
            web.post("/api/continuous/dismiss", continuous_dismiss),
            web.post("/api/acars/start", acars_start),
            web.post("/api/acars/stop", acars_stop),
            web.get("/api/acars/messages", acars_messages),
            web.get("/api/acars/settings", acars_settings),
            web.post("/api/acars/settings", acars_settings),
            web.get("/acars", acars_redirect),
            web.get("/acars/", acars_page),
            web.get("/clips/{name}", clip_file),
            web.get("/ws/state", state_socket),
            web.get("/ws/listen", listen_socket),
        ]
    )
    web.run_app(app, port=PORT, access_log=None)


if __name__ == "__main__":
    main()
