#!/usr/bin/env python3
"""Checks this Pi's health once a minute and writes status.json for the Status page and the alert banner on every page.
Shared by sdrpi and garage_pi; config.json says what to check. Runs on Python 3.7."""
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

SAMPLE_SECONDS = 60
SLOW_SECONDS = 300  # journal and log counts cost more, so they refresh less often
COUNT_WINDOW_SECONDS = 86400
HISTORY_SECONDS = 86400
CHART_STEP_SECONDS = 300
EVENT_SECONDS = 7 * 86400
EVENT_LIMIT = 200
PERSIST_SECONDS = 600  # history goes to disk this often, to spare the SD card
PEER_TIMEOUT_SECONDS = 5
PEER_STALE_SECONDS = 300
LOCAL_TIMEOUT_SECONDS = 3
LOG_TAIL_BYTES = 4 * 1024 * 1024
TEMP_WARN_C, TEMP_CRIT_C = 75, 80
DISK_WARN, DISK_CRIT = 0.10, 0.03
MEM_WARN = 0.10
SERVICE_AFTER_SECONDS = 180  # the Wi-Fi tracker spends about 2.5 minutes restarting after a dongle hang
HEALTH_AFTER_SECONDS = 120
CLOCK_AFTER_SECONDS = 600
ADSB_SILENT_SECONDS = 300
NOW_BITS = [(0, "Undervoltage now"), (1, "CPU speed capped"), (2, "Throttled now"), (3, "Soft temperature limit reached")]
BOOT_BITS = [(16, "undervoltage"), (17, "CPU speed capping"), (18, "throttling"), (19, "soft temperature limit")]
API_PORT = 8093
POWER_ACTIONS = ("reboot", "poweroff", "test")
POWER_DELAY_SECONDS = 3  # lets the page get its answer before the Pi goes down
MAX_BODY_BYTES = 1024
stop_requested = False


def on_signal(_signum, _frame):
    global stop_requested
    stop_requested = True


def run(args):
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                              timeout=20, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def fetch_json(url, timeout):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (OSError, ValueError):
        return None


def throttled():
    out = run(["vcgencmd", "get_throttled"])
    if "=" in out:
        return int(out.strip().split("=")[1], 16)
    text = read("/sys/devices/platform/soc/soc:firmware/get_throttled")
    return int(text, 16) if text and text.strip() else None


def temperature():
    text = read("/sys/class/thermal/thermal_zone0/temp")
    return round(int(text) / 1000, 1) if text else None


def memory_available():
    fields = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        name, _, value = line.partition(":")
        fields[name] = int(value.split()[0]) if value.split() else 0
    return fields.get("MemAvailable", 0) / fields["MemTotal"] if fields.get("MemTotal") else None


def wifi_dbm(iface="wlan0"):
    for line in (read("/proc/net/wireless") or "").splitlines():
        if line.strip().startswith(iface + ":"):
            return int(float(line.split()[3]))
    return None


def log_lines(path, match):
    """Timestamped lines containing match from the last COUNT_WINDOW_SECONDS, read from the end of the log."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
            text = f.read().decode(errors="replace")
    except OSError:
        return None
    cutoff, found = time.time() - COUNT_WINDOW_SECONDS, []
    for line in text.splitlines():
        if match in line:
            try:
                when = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if when >= cutoff:
                found.append((when, line[20:].strip()))
    return found


def journal_lines(unit, match):
    out = run(["journalctl", "-u", unit, "--since", "24 hours ago", "-o", "short-unix", "--no-pager"])
    found = []
    for line in out.splitlines():
        if match in line:
            try:
                found.append((float(line.split()[0]), line.split(": ", 1)[-1]))
            except ValueError:
                pass
    return found


class PowerApi(http.server.BaseHTTPRequestHandler):
    """POST /status/api/power from the Status page. Runs unprivileged: it only writes a request file, which the root-only
    pistatus-power.path unit acts on. Shutting down needs the Pi's name typed, so a stray click can't do it."""
    config = None

    def log_message(self, *_args):
        pass

    def answer(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        name = self.config["name"]
        if self.path != "/status/api/power":
            return self.answer(404, {"error": "not found"})
        # A JSON content type can't be sent cross-site without the browser asking first, and the origin must be this Pi
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.answer(415, {"error": "expected JSON"})
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            return self.answer(403, {"error": "request came from another site"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self.answer(413, {"error": "too large"})
        try:
            command = json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            return self.answer(400, {"error": "bad JSON"})
        action = command.get("action")
        if action not in POWER_ACTIONS:
            return self.answer(400, {"error": "unknown action"})
        if action == "poweroff" and command.get("confirm") != name:
            return self.answer(400, {"error": "type %s to confirm" % name})
        if action == "reboot" and command.get("confirm") != "reboot":
            return self.answer(400, {"error": "not confirmed"})
        who = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        print(time.strftime("%F %T"), "power request", action, "from", who, flush=True)
        timer = threading.Timer(POWER_DELAY_SECONDS, write_power_request, (self.config, action))
        timer.daemon = True
        timer.start()
        self.answer(202, {"ok": True, "action": action})


def write_power_request(config, action):
    path = os.path.join(os.path.dirname(config["output"]), "power-request")
    with open(path + ".tmp", "w") as f:
        f.write(action + "\n")
    os.replace(path + ".tmp", path)


def start_api(config):
    PowerApi.config = config
    server = http.server.ThreadingHTTPServer(("127.0.0.1", API_PORT), PowerApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def minutes(seconds):
    return "%d min" % round(seconds / 60) if seconds < 5400 else "%.1f h" % (seconds / 3600)


class Status:
    def __init__(self, config):
        self.config = config
        self.pending, self.active, self.events, self.temps = {}, {}, [], []
        self.rx = {}
        self.counts, self.next_slow = [], 0
        self.peer_ok, self.started = time.time(), time.time()
        self.adsb_last_messages = time.time()
        self.next_persist = time.time() + PERSIST_SECONDS
        try:
            saved = json.loads(read(config["history"]) or "{}")
            self.temps, self.events = saved.get("temps", []), saved.get("events", [])
        except ValueError:
            pass

    def persist(self):
        path = self.config["history"]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"temps": self.temps, "events": self.events}, f)
        os.replace(tmp, path)

    def update_alerts(self, now, conditions):
        """conditions: (id, level, text, after). An alert shows only once its condition has held for `after` seconds."""
        current = set()
        for key, level, text, after in conditions:
            current.add(key)
            first = self.pending.setdefault(key, now)
            if now - first < after:
                continue
            if key not in self.active:
                self.active[key] = {"id": key, "level": level, "text": text, "since": int(first)}
                self.events.append({"t": int(now), "kind": "start", "level": level, "text": text})
            else:
                self.active[key].update(level=level, text=text)
        for key in list(self.pending):
            if key not in current:
                del self.pending[key]
        for key in list(self.active):
            if key not in current:
                alert = self.active.pop(key)
                self.events.append({"t": int(now), "kind": "end", "level": alert["level"], "text": alert["text"]})
        cutoff = now - EVENT_SECONDS
        self.events = [e for e in self.events if e["t"] >= cutoff][-EVENT_LIMIT:]

    def refresh_counts(self, now):
        counts = []
        for item in self.config.get("log_counts", []):
            found = log_lines(item["path"], item["match"])
            counts.append({"name": item["name"], "count": None if found is None else len(found),
                           "last": found[-1][0] if found else None, "detail": found[-1][1] if found and item.get("detail") else None})
        for item in self.config.get("journal_counts", []):
            found = journal_lines(item["unit"], item["match"])
            counts.append({"name": item["name"], "count": len(found), "last": found[-1][0] if found else None, "detail": None})
        self.counts = counts
        self.next_slow = now + SLOW_SECONDS

    def sample(self):
        now = time.time()
        cfg, conditions, radios = self.config, [], []
        bits = throttled()
        temp = temperature()
        load = (read("/proc/loadavg") or "0 0 0").split()[:3]
        mem = memory_available()
        uptime = float((read("/proc/uptime") or "0").split()[0])
        synced = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip() == "yes"
        if temp is not None:
            self.temps.append([int(now), temp])
            self.temps = [t for t in self.temps if t[0] >= now - HISTORY_SECONDS]

        if bits is not None:
            for bit, text in NOW_BITS:
                if bits & (1 << bit):
                    conditions.append(("power-%d" % bit, "critical" if bit in (0, 2) else "warning", text, 0))
        if temp is not None and temp >= TEMP_WARN_C:
            conditions.append(("temperature", "critical" if temp >= TEMP_CRIT_C else "warning", "CPU at %.0f°C" % temp, 0))
        if mem is not None and mem < MEM_WARN:
            conditions.append(("memory", "warning", "Only %.0f%% of memory free" % (mem * 100), 120))
        if not synced:
            conditions.append(("clock", "warning", "Clock not synchronized", CLOCK_AFTER_SECONDS))

        disks = []
        for path in cfg.get("disks", []):
            try:
                st = os.statvfs(path)
            except OSError:
                conditions.append(("disk:" + path, "critical", "%s not available" % path, 0))
                continue
            free = st.f_bavail / st.f_blocks if st.f_blocks else 0
            disks.append({"path": path, "free_pct": round(free * 100, 1), "free_gb": round(st.f_bavail * st.f_frsize / 1e9, 1),
                          "total_gb": round(st.f_blocks * st.f_frsize / 1e9, 1)})
            if free < DISK_WARN:
                conditions.append(("disk:" + path, "critical" if free < DISK_CRIT else "warning", "%s is %.0f%% full" % (path, 100 - free * 100), 0))

        names = cfg.get("services", [])
        states = run(["systemctl", "is-active"] + names).split() if names else []
        services = [{"name": n, "state": states[i] if i < len(states) else "unknown"} for i, n in enumerate(names)]
        for s in services:
            if s["state"] != "active":
                conditions.append(("service:" + s["name"], "critical", "%s is %s" % (s["name"], s["state"]), SERVICE_AFTER_SECONDS))

        fresh = []
        for item in cfg.get("fresh", []):
            try:
                age = now - os.stat(item["path"]).st_mtime
            except OSError:
                age = None
            fresh.append({"name": item["name"], "age_s": None if age is None else int(age)})
            if age is None or age > item["max_age"]:
                text = "%s missing" % item["name"] if age is None else "%s not updated for %s" % (item["name"], minutes(age))
                conditions.append(("fresh:" + item["path"], "warning", text, 0))

        for item in cfg.get("rx_watch", []):
            text = read(item["path"])
            state = self.rx.setdefault(item["path"], {"value": None, "changed": now})
            if text is None:
                radios.append({"name": item["name"], "ok": False, "detail": "Not found"})
                conditions.append(("rx:" + item["path"], "warning", "%s not found" % item["name"], SERVICE_AFTER_SECONDS))
                continue
            if text.strip() != state["value"]:
                state.update(value=text.strip(), changed=now)
            silent = now - state["changed"]
            radios.append({"name": item["name"], "ok": silent < item["silent"],
                           "detail": "Receiving" if silent < SAMPLE_SECONDS * 2 else "Nothing received for %s" % minutes(silent)})
            if silent >= item["silent"]:
                conditions.append(("rx:" + item["path"], "warning", "%s has received nothing for %s" % (item["name"], minutes(silent)), 0))

        if cfg.get("scanner_health"):
            health = fetch_json(cfg["scanner_health"], LOCAL_TIMEOUT_SECONDS)
            if health is None:
                radios.append({"name": "HackRF", "ok": False, "detail": "Scanner not answering"})
                conditions.append(("scanner", "critical", "Scanner not answering", HEALTH_AFTER_SECONDS))
            elif health.get("error"):
                radios.append({"name": "HackRF", "ok": False, "detail": health["error"]})
                conditions.append(("hackrf", "critical", "HackRF: " + health["error"], HEALTH_AFTER_SECONDS))
            else:
                detail = ("Recording: " + health["recording"]) if health.get("recording") else (
                    "Someone is listening" if health.get("listening") else "Band monitor and auto-recording")
                radios.append({"name": "HackRF", "ok": True, "detail": detail})

        if cfg.get("adsb_stats"):
            text = read(cfg["adsb_stats"])
            try:
                messages = json.loads(text)["last1min"]["messages"] if text else None
            except (ValueError, KeyError, TypeError):
                messages = None
            if messages:
                self.adsb_last_messages = now
            radios.append({"name": "ADS-B receiver", "ok": bool(messages),
                           "detail": "No data" if messages is None else "%d messages in the last minute" % messages})
            if now - self.adsb_last_messages >= ADSB_SILENT_SECONDS:
                conditions.append(("adsb", "warning", "ADS-B receiver has decoded nothing for %s" % minutes(now - self.adsb_last_messages), 0))

        peer = None
        if cfg.get("peer"):
            data = fetch_json(cfg["peer"]["data"], PEER_TIMEOUT_SECONDS)
            peer = {"name": cfg["peer"]["name"], "page": cfg["peer"]["page"], "reachable": data is not None}
            if data is not None:
                self.peer_ok = now
                peer.update(generated=data.get("generated"), alerts=data.get("alerts", []),
                            temp_c=data.get("system", {}).get("temp_c"), uptime_s=data.get("system", {}).get("uptime_s"))
                if data.get("generated") and now - data["generated"] > PEER_STALE_SECONDS:
                    conditions.append(("peer-stale", "warning", "%s status checks stopped updating" % peer["name"], 0))
            elif now - self.peer_ok >= PEER_STALE_SECONDS:
                conditions.append(("peer", "warning", "%s not reachable for %s" % (peer["name"], minutes(now - self.peer_ok)), 0))
            peer["last_ok"] = int(self.peer_ok)

        if now >= self.next_slow:
            self.refresh_counts(now)
        self.update_alerts(now, conditions)

        step, chart, bucket = CHART_STEP_SECONDS, [], None
        for t, c in self.temps:
            if bucket is None or t // step != bucket:
                bucket = t // step
                chart.append([t, c])
            else:
                chart[-1] = [chart[-1][0], max(chart[-1][1], c)]

        status = {
            "name": cfg["name"], "title": cfg.get("title", cfg["name"]), "generated": int(now), "pages": cfg.get("pages", []),
            "system": {"uptime_s": int(uptime), "temp_c": temp, "load": load, "mem_free_pct": None if mem is None else round(mem * 100),
                       "clock_synced": synced, "wifi_dbm": wifi_dbm(),
                       "power_now": [t for b, t in NOW_BITS if bits is not None and bits & (1 << b)],
                       "power_since_boot": [t for b, t in BOOT_BITS if bits is not None and bits & (1 << b)]},
            "disks": disks, "services": services, "fresh": fresh, "radios": radios, "counts": self.counts,
            "alerts": sorted(self.active.values(), key=lambda a: (a["level"] != "critical", a["since"])),
            "events": self.events[-50:][::-1], "temps": chart, "peer": peer,
        }
        tmp = cfg["output"] + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, cfg["output"])
        if now >= self.next_persist:
            self.next_persist = now + PERSIST_SECONDS
            self.persist()

    def run(self):
        while not stop_requested:
            started = time.time()
            try:
                self.sample()
            except Exception as error:  # one bad check must not stop the others for good
                print(time.strftime("%F %T"), "sample failed: %r" % error, flush=True)
            while not stop_requested and time.time() - started < SAMPLE_SECONDS:
                time.sleep(1)
        self.persist()


def main():
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    with open(sys.argv[1]) as f:
        config = json.load(f)
    print(time.strftime("%F %T"), "pistatus started for", config["name"], flush=True)
    start_api(config)
    Status(config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
