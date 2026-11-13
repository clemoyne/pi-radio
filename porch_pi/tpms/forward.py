#!/usr/bin/env python3
"""Runs rtl_433 on the porch RTL-SDR and posts every decoded message to sdrpi's car store, which keeps the data and the
page. Messages that can't be delivered wait in SPOOL and go with the next post. Each reply names the band to listen on,
and the frequencies of the weather stations found so far. Port 8080 redirects to the page on sdrpi."""
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = Path(__file__).resolve().parent
STATE = Path(os.environ.get("TPMS_STATE", "/var/lib/tpms"))
SETTINGS_PATH = STATE / "settings.json"
SPOOL = STATE / "spool.jsonl"
STORE = os.environ.get("CARS_STORE", "http://sdrpi.local:8095/cars/api/ingest")
PAGE = "http://sdrpi.local:8080/cars/"
PORT = 8080
BANDS = {"315": ["315M"], "433": ["433.92M"], "both": ["315M", "433.92M"], "315w": ["315M"]}
HOP_S = {"both": 120}
# 315w: tire sensors on 315 MHz, leaving every WEATHER_EVERY_S for WEATHER_LISTEN_S on each weather station's frequency
# (the sensor here sends every 50 s, so one visit catches a reading), and once a week, when the fewest cars pass, for
# DISCOVER_S on each of DISCOVER_FREQS to find stations not heard before
WEATHER_EVERY_S = 900
WEATHER_LISTEN_S = 75
DEFAULT_WEATHER_FREQS = ["433.92M"]
DISCOVER_FREQS = ["433.92M", "915M"]  # most North American stations use one of these
DISCOVER_S = 600
DISCOVER_WEEKDAY, DISCOVER_HOUR = 6, 3  # Sunday, 3 am local time
FREQ = re.compile(r"\d{2,4}(\.\d{1,3})?M")
POST_EVERY_S = 3
RETRY_S = 30  # while sdrpi is unreachable
BATCH = 2000  # messages per post when catching up from the spool
MAX_SPOOL_BYTES = 200_000_000  # about a month of busy street; the oldest half goes beyond it


def log(*parts):
    print(time.strftime("%F %T"), *parts, flush=True)


class Forwarder:
    def __init__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        try:
            saved = json.loads(SETTINGS_PATH.read_text())
        except (OSError, ValueError):
            saved = {}
        self.band = saved.get("band") if saved.get("band") in BANDS else "315"
        self.last_search = saved.get("last_search", 0)
        self.messages = queue.Queue()
        self.proc, self.running, self.started, self.switching = None, False, None, False
        self.weather_freqs = list(DEFAULT_WEATHER_FREQS)
        self.next_check, self.check_until, self.search_until = time.time() + 30, 0, 0
        self.current = None  # the plan rtl_433 is running now

    def save(self):
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"band": self.band, "last_search": self.last_search}))
        tmp.replace(SETTINGS_PATH)

    def plan(self):
        """(frequencies, hop seconds or None, what for) rtl_433 should be running right now."""
        now = time.time()
        if self.band != "315w":
            return tuple(BANDS[self.band]), HOP_S.get(self.band), "tires"
        if now < self.search_until:
            return tuple(DISCOVER_FREQS), DISCOVER_S, "station search"
        if now < self.check_until:
            return tuple(self.weather_freqs), WEATHER_LISTEN_S, "weather check"
        return tuple(BANDS["315w"]), None, "tires"

    def scheduler(self):
        """Starts the weather checks and the weekly station search, and restarts rtl_433 when the plan changes."""
        while True:
            time.sleep(1)
            now, local = time.time(), time.localtime()
            if self.band == "315w":
                if (local.tm_wday == DISCOVER_WEEKDAY and local.tm_hour == DISCOVER_HOUR and now - self.last_search > 6 * 86400
                        and now >= self.search_until):
                    self.last_search = now
                    self.save()
                    self.search_until = now + DISCOVER_S * len(DISCOVER_FREQS)
                    self.next_check = self.search_until + WEATHER_EVERY_S
                    log("weekly weather station search on", " and ".join(DISCOVER_FREQS))
                elif now >= self.next_check and now >= self.search_until:
                    self.check_until = now + WEATHER_LISTEN_S * len(self.weather_freqs)
                    self.next_check = now + WEATHER_EVERY_S
            if self.current is not None and self.plan() != self.current and self.proc:
                self.switching = True
                self.proc.terminate()

    def radio(self):
        while True:
            self.current = plan = self.plan()
            freqs, hop, purpose = plan
            cmd = ["rtl_433", "-M", "time:unix", "-M", "level", "-M", "protocol", "-F", "json"]
            cmd += ["-H", str(hop)] if len(freqs) > 1 and hop else []
            for f in freqs:
                cmd += ["-f", f]
            self.switching = False
            proc = self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            self.running, self.started = True, time.time()
            if purpose != "weather check":  # the 15-minute checks would fill the journal
                log("rtl_433 started on", " and ".join(dict.fromkeys(freqs)), "for", purpose)
            for line in proc.stdout:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if isinstance(m, dict) and "model" in m:
                    self.messages.put(m)
            self.running = False
            code = proc.wait()
            if not self.switching:
                log("rtl_433 exited", code)
            time.sleep(1 if self.switching else 10)  # the stick needs a moment to be released

    def set_band(self, band):
        if band not in BANDS or band == self.band:
            return
        self.band = band
        self.save()
        log("band set to", band)  # the scheduler sees the plan change and restarts rtl_433

    def set_weather_freqs(self, freqs):
        """The frequencies of the weather stations sdrpi knows about, checked every WEATHER_EVERY_S."""
        if isinstance(freqs, list):
            freqs = [f for f in freqs if isinstance(f, str) and FREQ.fullmatch(f)][:4]
            if freqs and freqs != self.weather_freqs:
                self.weather_freqs = freqs
                log("weather checks on", " and ".join(freqs))

    def spooled(self):
        try:
            with open(SPOOL) as f:
                return [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            return []

    def spool(self, messages):
        if SPOOL.exists() and SPOOL.stat().st_size > MAX_SPOOL_BYTES:
            kept = self.spooled()
            messages = kept[len(kept) // 2:] + messages
            SPOOL.unlink()
            log("spool over its cap; dropped the oldest", len(kept) // 2)
        with open(SPOOL, "a") as f:
            f.writelines(json.dumps(m) + "\n" for m in messages)

    def post(self, messages):
        body = json.dumps({"porch": {"running": self.running, "band": self.band, "started": self.started,
                                     "doing": self.current[2] if self.current else None, "next_weather_check": self.next_check,
                                     "last_station_search": self.last_search or None},
                           "messages": messages}).encode()
        request = urllib.request.Request(STORE, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as reply:
            return json.loads(reply.read())

    def sender(self):
        down_since = None
        while True:
            time.sleep(POST_EVERY_S if not down_since else RETRY_S)
            fresh = []
            while not self.messages.empty():
                fresh.append(self.messages.get())
            backlog = self.spooled() if SPOOL.exists() else []
            pending, sent = backlog + fresh, 0
            try:
                reply = {}
                for i in range(0, max(len(pending), 1), BATCH):
                    reply = self.post(pending[i:i + BATCH])
                    sent = min(i + BATCH, len(pending))
                if backlog:
                    SPOOL.unlink()
                    log("delivered", len(backlog), "spooled messages")
                if down_since:
                    log("sdrpi reachable again after", round(time.time() - down_since), "s")
                    down_since = None
                self.set_band(reply.get("band"))
                self.set_weather_freqs(reply.get("weather_freqs"))
            except (OSError, urllib.error.URLError, ValueError) as e:
                if not down_since:
                    down_since = time.time()
                    log("can't reach sdrpi, spooling:", repr(e))
                if sent:  # part of the backlog got through; keep only the rest so nothing is counted twice
                    SPOOL.unlink(missing_ok=True)
                    self.spool(pending[sent:])
                elif fresh:
                    self.spool(fresh)


class Redirect(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/favicon.ico", "/apple-touch-icon.png"):
            body = (APP / "www" / self.path.lstrip("/")).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/x-icon" if self.path.endswith(".ico") else "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self.send_header("Location", PAGE)
        self.end_headers()

    def log_message(self, *args):
        pass


def main():
    forwarder = Forwarder()
    threading.Thread(target=forwarder.radio, daemon=True).start()
    threading.Thread(target=forwarder.sender, daemon=True).start()
    threading.Thread(target=forwarder.scheduler, daemon=True).start()
    log("forwarding to", STORE)
    ThreadingHTTPServer(("", PORT), Redirect).serve_forever()


if __name__ == "__main__":
    main()
