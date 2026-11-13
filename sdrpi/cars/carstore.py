#!/usr/bin/env python3
"""Counts cars passing the porch from their tire pressure sensors (TPMS). porch_pi runs rtl_433 on its RTL-SDR and POSTs
every decoded message here (/cars/api/ingest, with porch_pi's own timestamps); this keeps the database on sdrpi's SSD
and serves the page, also proxied by the scanner at /cars/.

Each sensor is keyed by a hash of its type and ID (the key stays on this Pi); its raw ID is stored alongside, filled in
from the first message after the raw IDs started being kept, and shown on the page. The data stays on the LAN.
A pass is one model's sensors heard with gaps under PASS_GAP_S; sensors heard in the same pass become one car."""
import collections
import datetime
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # the service runs python3 -I, which leaves this folder off the path
import weather  # noqa: E402

APP = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CARS_STATE", "/mnt/data/cars"))
DB_PATH = STATE / "tpms.db"
KEY_PATH = STATE / "key"
PORT = int(os.environ.get("CARS_PORT", 8095))
PORCH_STALE_S = 30  # porch_pi posts every few seconds, even with nothing to send
SETTINGS_PATH = STATE / "settings.json"
# North American cars use 315 MHz, some newer ones 433.92; "both" hops every HOP_S, halving the time on each.
# "315w" stays on 315 MHz except for a 75-second weather check every 15 minutes on the weather stations' frequencies
# and a weekly station search; porch_pi runs that schedule (see porch_pi/tpms/forward.py).
# The band and the stations' frequencies are handed to porch_pi in each ingest reply.
BANDS = {"315": ["315M"], "433": ["433.92M"], "both": ["315M", "433.92M"], "315w": ["315M"]}
DEFAULT_BAND = "315w"
HOP_S = {"both": 120}
PASS_GAP_S = 60
STOPPED_S = 180  # a pass heard this long is a car stopped or parked nearby, not one going by
MERGE_AFTER = 2  # one shared trip could be two cars with the same sensor make passing together
KEEP_DAYS = 365
RECENT = 60
MAX_POST = 20_000_000  # a long spool from porch_pi arrives in batches well under this
PSI_KPA, BAR_KPA = 6.894757, 100.0
PLAUSIBLE_KPA = (103, 620)  # 15-90 psi; outside it a sensor only resembling its protocol was misread
LOW_PSI = 28  # about 20% under the usual 32-36 psi; dashboard warnings come on at 25% under


def now():
    return time.time()


def open_db():
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.executescript("""
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS vehicles (id INTEGER PRIMARY KEY, model TEXT, first REAL, last REAL);
        CREATE TABLE IF NOT EXISTS sensors (hash TEXT PRIMARY KEY, model TEXT, vehicle INTEGER, first REAL, last REAL, readings INTEGER);
        CREATE TABLE IF NOT EXISTS passes (id INTEGER PRIMARY KEY, vehicle INTEGER, start REAL, end REAL, model TEXT,
            sensors INTEGER, readings INTEGER, repeat INTEGER, stopped INTEGER, mhz REAL, rssi REAL);
        CREATE TABLE IF NOT EXISTS readings (t REAL, sensor TEXT, pass INTEGER, kpa REAL, temp_c REAL, mhz REAL, rssi REAL, snr REAL);
        CREATE TABLE IF NOT EXISTS decodes (hour INTEGER PRIMARY KEY, tpms INTEGER, other INTEGER);
        CREATE TABLE IF NOT EXISTS links (a INTEGER, b INTEGER, n INTEGER, PRIMARY KEY (a, b));  -- trips on which cars a < b were heard together
        CREATE INDEX IF NOT EXISTS passes_start ON passes (start);
        CREATE INDEX IF NOT EXISTS passes_vehicle ON passes (vehicle, start);
        CREATE INDEX IF NOT EXISTS readings_t ON readings (t);
    """)
    if "ident" not in [row[1] for row in db.execute("PRAGMA table_info(sensors)")]:
        db.execute("ALTER TABLE sensors ADD COLUMN ident TEXT")  # raw sensor ID; NULL for sensors last heard before it was kept
    return db


def load_key():
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(secrets.token_bytes(32))
        KEY_PATH.chmod(0o600)
    return KEY_PATH.read_bytes()


def pressure_kpa(m):
    for field, scale in (("pressure_kPa", 1), ("pressure_PSI", PSI_KPA), ("pressure_bar", BAR_KPA)):
        if isinstance(m.get(field), (int, float)):
            return round(m[field] * scale, 1)
    return None


def temperature_c(m):
    if isinstance(m.get("temperature_C"), (int, float)):
        return m["temperature_C"]
    if isinstance(m.get("temperature_F"), (int, float)):
        return round((m["temperature_F"] - 32) / 1.8, 1)
    return None


class Tracker:
    def __init__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self.db, self.key, self.lock = open_db(), load_key(), threading.Lock()
        self.open = {}  # model -> {"start", "last", "readings": [(t, hash, ...)]}
        self.recent = collections.deque(maxlen=RECENT)
        self.last_decode, self.last_tpms = None, None
        self.porch = {"seen": None, "running": False, "band": None, "started": None}
        try:
            self.band = json.loads(SETTINGS_PATH.read_text())["band"]
        except (OSError, ValueError, KeyError):
            self.band = DEFAULT_BAND
        if self.band not in BANDS:
            self.band = DEFAULT_BAND
        self.decode_times = collections.deque()
        self.weather = weather.Weather()

    def sensor_hash(self, model, ident):
        return hmac.new(self.key, f"{model}|{ident}".encode(), hashlib.sha256).hexdigest()[:20]

    def count_decode(self, t, tpms):
        hour = int(t // 3600)
        column = "tpms" if tpms else "other"
        self.db.execute("INSERT INTO decodes (hour, tpms, other) VALUES (?, 0, 0) ON CONFLICT (hour) DO NOTHING", (hour,))
        self.db.execute(f"UPDATE decodes SET {column} = {column} + 1 WHERE hour = ?", (hour,))

    def message(self, m):
        try:
            t = float(m["time"])  # porch_pi's clock, so spooled messages land at the right time
        except (KeyError, TypeError, ValueError):
            t = now()
        tpms = m.get("type") == "TPMS" and m.get("id") is not None
        with self.lock:
            self.last_decode = t
            self.decode_times.append(t)
            self.count_decode(t, tpms)
            if not tpms:
                self.db.commit()
                if weather.is_weather(m):
                    self.weather.store(m, t)
                return
            self.last_tpms = t
            model = str(m.get("model", "?"))[:40]
            sensor = self.sensor_hash(model, m["id"])
            mhz = m.get("freq1", m.get("freq"))
            reading = {"t": t, "sensor": sensor, "ident": str(m["id"])[:24], "model": model, "kpa": pressure_kpa(m), "temp_c": temperature_c(m),
                       "mhz": round(mhz, 2) if isinstance(mhz, (int, float)) else None,
                       "rssi": m.get("rssi"), "snr": m.get("snr")}
            self.db.execute("INSERT INTO sensors (hash, model, vehicle, first, last, readings, ident) VALUES (?, ?, NULL, ?, ?, 1, ?) "
                            "ON CONFLICT (hash) DO UPDATE SET last = excluded.last, readings = readings + 1, ident = excluded.ident",
                            (sensor, model, t, t, reading["ident"]))
            current = self.open.get(model)
            if current and t - current["last"] > PASS_GAP_S:
                self.close(model)
                current = None
            if not current:
                current = self.open[model] = {"start": t, "last": t, "readings": []}
            current["last"], current["seen"] = t, now()
            current["readings"].append(reading)
            self.db.commit()
            self.recent.appendleft(reading)

    def close_idle(self):
        with self.lock:
            for model in [m for m, p in self.open.items() if now() - p["seen"] > PASS_GAP_S]:
                self.close(model)
            self.db.commit()

    def close(self, model):
        """Turns a finished pass into one pass per car: sensors already known to a car stay with it, new ones join the
        car heard most in the pass, or a new car."""
        p = self.open.pop(model)
        db = self.db
        owner = {}
        for r in p["readings"]:
            if r["sensor"] not in owner:
                row = db.execute("SELECT vehicle FROM sensors WHERE hash = ?", (r["sensor"],)).fetchone()
                owner[r["sensor"]] = row[0] if row else None
        hits = collections.Counter(owner[r["sensor"]] for r in p["readings"] if owner[r["sensor"]] is not None)
        if hits:
            main = hits.most_common(1)[0][0]
        else:
            main = db.execute("INSERT INTO vehicles (model, first, last) VALUES (?, ?, ?)", (model, p["start"], p["last"])).lastrowid
        for sensor, vehicle in owner.items():
            if vehicle is None:
                owner[sensor] = main
                db.execute("UPDATE sensors SET vehicle = ? WHERE hash = ?", (main, sensor))
        # Two cars heard together on MERGE_AFTER separate trips are one car whose tires were first caught on different trips
        alias = {}

        def find(v):
            while v in alias:
                v = alias[v]
            return v
        cars = sorted(set(owner.values()))
        for i, a in enumerate(cars):
            for b in cars[i + 1:]:
                n = db.execute("INSERT INTO links (a, b, n) VALUES (?, ?, 1) ON CONFLICT (a, b) DO UPDATE SET n = n + 1 "
                               "RETURNING n", (a, b)).fetchone()[0]
                keep, gone = sorted((find(a), find(b)))
                if n >= MERGE_AFTER and keep != gone:
                    self.merge(keep, gone)
                    alias[gone] = keep
        owner = {sensor: find(vehicle) for sensor, vehicle in owner.items()}
        groups = collections.defaultdict(list)
        for r in p["readings"]:
            groups[owner[r["sensor"]]].append(r)
        for vehicle, readings in groups.items():
            start, end = readings[0]["t"], readings[-1]["t"]
            seen_before = db.execute("SELECT 1 FROM passes WHERE vehicle = ? LIMIT 1", (vehicle,)).fetchone() is not None
            rssis = [r["rssi"] for r in readings if isinstance(r["rssi"], (int, float))]
            pass_id = db.execute(
                "INSERT INTO passes (vehicle, start, end, model, sensors, readings, repeat, stopped, mhz, rssi) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (vehicle, start, end, model, len({r["sensor"] for r in readings}), len(readings), int(seen_before),
                 int(end - start >= STOPPED_S), readings[0]["mhz"], max(rssis) if rssis else None)).lastrowid
            db.execute("UPDATE vehicles SET last = ? WHERE id = ?", (end, vehicle))
            db.executemany("INSERT INTO readings (t, sensor, pass, kpa, temp_c, mhz, rssi, snr) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           [(r["t"], r["sensor"], pass_id, r["kpa"], r["temp_c"], r["mhz"], r["rssi"], r["snr"]) for r in readings])
        for vehicle in set(alias.values()):
            self.retally(find(vehicle))
        if alias:
            log("merged cars", ", ".join(f"{gone} into {find(gone)}" for gone in alias))

    def merge(self, keep, gone):
        """Moves car `gone`'s sensors, passes and links onto car `keep`."""
        db = self.db
        db.execute("UPDATE sensors SET vehicle = ? WHERE vehicle = ?", (keep, gone))
        db.execute("UPDATE passes SET vehicle = ? WHERE vehicle = ?", (keep, gone))
        db.execute("UPDATE vehicles SET first = MIN(first, (SELECT first FROM vehicles WHERE id = ?)), "
                   "last = MAX(last, (SELECT last FROM vehicles WHERE id = ?)) WHERE id = ?", (gone, gone, keep))
        db.execute("DELETE FROM vehicles WHERE id = ?", (gone,))
        for a, b, n in db.execute("SELECT a, b, n FROM links WHERE a = ? OR b = ?", (gone, gone)).fetchall():
            other = b if a == gone else a
            db.execute("DELETE FROM links WHERE a = ? AND b = ?", (a, b))
            if other != keep:
                db.execute("INSERT INTO links (a, b, n) VALUES (?, ?, ?) ON CONFLICT (a, b) DO UPDATE SET n = n + excluded.n",
                           (min(other, keep), max(other, keep), n))

    def retally(self, vehicle):
        """After a merge: joins the car's passes that were one trip split in two, then marks all but its first as repeats."""
        db = self.db
        rows = db.execute("SELECT id, start, end FROM passes WHERE vehicle = ? ORDER BY start", (vehicle,)).fetchall()
        kept = []
        for pass_id, start, end in rows:
            if kept and start - kept[-1][2] <= PASS_GAP_S:
                prev = kept[-1]
                db.execute("UPDATE readings SET pass = ? WHERE pass = ?", (prev[0], pass_id))
                db.execute("DELETE FROM passes WHERE id = ?", (pass_id,))
                kept[-1] = (prev[0], prev[1], max(prev[2], end))
            else:
                kept.append((pass_id, start, end))
        for i, (pass_id, start, end) in enumerate(kept):
            sensors, readings, rssi = db.execute("SELECT COUNT(DISTINCT sensor), COUNT(*), MAX(rssi) FROM readings WHERE pass = ?",
                                                 (pass_id,)).fetchone()
            db.execute("UPDATE passes SET end = ?, sensors = ?, readings = ?, rssi = ?, repeat = ?, stopped = ? WHERE id = ?",
                       (end, sensors, readings, rssi, int(i > 0), int(end - start >= STOPPED_S), pass_id))

    def prune(self):
        cutoff = now() - KEEP_DAYS * 86400
        with self.lock:
            self.db.execute("DELETE FROM readings WHERE t < ?", (cutoff,))
            self.db.execute("DELETE FROM passes WHERE start < ?", (cutoff,))
            self.db.execute("DELETE FROM decodes WHERE hour < ?", (int(cutoff // 3600),))
            self.db.execute("DELETE FROM sensors WHERE last < ?", (cutoff,))
            self.db.execute("DELETE FROM links WHERE a NOT IN (SELECT id FROM vehicles) OR b NOT IN (SELECT id FROM vehicles)")
            self.db.execute("DELETE FROM vehicles WHERE last < ? AND id NOT IN (SELECT vehicle FROM sensors WHERE vehicle IS NOT NULL)",
                            (cutoff,))
            self.db.commit()

    def set_band(self, band):
        """Saves the band; porch_pi switches to it on its next post."""
        self.band = band
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"band": band}))
        tmp.replace(SETTINGS_PATH)

    def ingest(self, body):
        """One post from porch_pi: its radio state and the messages decoded since its last post. Replies with the band."""
        porch = body.get("porch") or {}
        self.porch = {"seen": now(), "running": bool(porch.get("running")), "band": porch.get("band"), "started": porch.get("started"),
                      "doing": porch.get("doing"), "next_weather_check": porch.get("next_weather_check"),
                      "last_station_search": porch.get("last_station_search")}
        for m in body.get("messages") or []:
            if isinstance(m, dict) and "model" in m:
                try:
                    self.message(m)
                except Exception as e:  # one odd message must not stop the rest
                    log("message error", repr(e))
        return {"band": self.band, "weather_freqs": self.weather.freqs()}

    def housekeeping(self):
        last_prune = 0
        while True:
            time.sleep(5)
            try:
                self.close_idle()
                if now() - last_prune > 3600:
                    self.prune()
                    last_prune = now()
            except Exception as e:
                log("housekeeping error", repr(e))

    def status(self):
        with self.lock:
            return self._status()

    def _status(self):
        db, t = self.db, now()
        while self.decode_times and self.decode_times[0] < t - 600:
            self.decode_times.popleft()
        midnight = time.mktime(datetime.date.fromtimestamp(t).timetuple())
        hour_now = int(t // 3600) * 3600

        def counts(since, until=None):
            row = db.execute("SELECT COUNT(*), SUM(repeat), COUNT(DISTINCT vehicle), SUM(readings) FROM passes "
                             "WHERE start >= ? AND start < ? AND stopped = 0", (since, until or t + 1)).fetchone()
            return {"cars": row[0], "repeat": row[1] or 0, "new": row[0] - (row[1] or 0), "vehicles": row[2], "readings": row[3] or 0}

        hours = []
        rows = db.execute("SELECT CAST(start / 3600 AS INTEGER), SUM(1 - repeat), SUM(repeat) FROM passes "
                          "WHERE start >= ? AND stopped = 0 GROUP BY 1", (hour_now - 47 * 3600,)).fetchall()
        by_hour = {h * 3600: (n, r) for h, n, r in rows}
        for i in range(47, -1, -1):
            h = hour_now - i * 3600
            n, r = by_hour.get(h, (0, 0))
            hours.append({"t": h, "new": n, "repeat": r})

        days = []
        today = datetime.date.fromtimestamp(t)
        for i in range(29, -1, -1):
            day = today - datetime.timedelta(days=i)
            start = time.mktime(day.timetuple())
            c = counts(start, time.mktime((day + datetime.timedelta(days=1)).timetuple()))
            days.append({"t": start, "new": c["new"], "repeat": c["repeat"]})

        vehicle_seen = {}

        def car(sensor):
            if sensor not in vehicle_seen:
                row = db.execute("SELECT vehicle FROM sensors WHERE hash = ?", (sensor,)).fetchone()
                vehicle_seen[sensor] = row[0] if row else None
            return vehicle_seen[sensor]

        recent = [{"t": r["t"], "car": car(r["sensor"]), "sensor": r["sensor"], "ident": r["ident"], "model": r["model"],
                   "kpa": r["kpa"], "temp_c": r["temp_c"], "mhz": r["mhz"], "rssi": r["rssi"]} for r in self.recent]
        passes = [dict(zip(("start", "end", "car", "model", "sensors", "readings", "repeat", "stopped", "mhz", "rssi", "ids"), row))
                  for row in db.execute("SELECT start, end, vehicle, p.model, sensors, p.readings, repeat, stopped, mhz, rssi, "
                                        "(SELECT GROUP_CONCAT(DISTINCT COALESCE(s.ident, '?')) FROM readings r JOIN sensors s "
                                        " ON s.hash = r.sensor WHERE r.pass = p.id) "
                                        "FROM passes p ORDER BY start DESC LIMIT 40")]
        regulars = []
        for vehicle, model, n, first, last in db.execute(
                "SELECT vehicle, model, COUNT(*), MIN(start), MAX(start) FROM passes WHERE stopped = 0 "
                "GROUP BY vehicle HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC, MAX(start) DESC LIMIT 40").fetchall():
            starts = [s for (s,) in db.execute("SELECT start FROM passes WHERE vehicle = ? AND stopped = 0", (vehicle,))]
            hist = [0] * 24
            for s in starts:
                hist[time.localtime(s).tm_hour] += 1
            day_count = len({time.strftime("%F", time.localtime(s)) for s in starts})
            ids = [i or "?" for (i,) in db.execute("SELECT ident FROM sensors WHERE vehicle = ? ORDER BY first", (vehicle,))]
            regulars.append({"car": vehicle, "model": model, "passes": n, "days": day_count, "first": first, "last": last,
                             "hours": hist, "ids": ids})
        sensors = self.sensor_rows()
        tires = {name: self.tire_stats(since) for name, since in
                 (("today", midnight), ("week", t - 7 * 86400), ("month", t - 30 * 86400), ("all", 0))}
        spread = collections.Counter(int(kpa / PSI_KPA // 2 * 2) for kpa in self.latest_pressures(t - 30 * 86400))
        models = [{"model": m, "cars": n} for m, n in db.execute(
            "SELECT model, COUNT(DISTINCT vehicle) FROM passes WHERE start >= ? GROUP BY model ORDER BY 2 DESC",
            (t - 30 * 86400,))]
        totals = db.execute("SELECT (SELECT COUNT(*) FROM vehicles), (SELECT COUNT(*) FROM sensors), "
                            "(SELECT COUNT(*) FROM passes WHERE stopped = 0), (SELECT MIN(start) FROM passes)").fetchone()
        stopped_today = db.execute("SELECT COUNT(*) FROM passes WHERE start >= ? AND stopped = 1", (midnight,)).fetchone()[0]
        return {
            "now": t,
            "radio": {"running": self.porch["running"] and self.porch["seen"] is not None and t - self.porch["seen"] < PORCH_STALE_S,
                      "porch_seen": self.porch["seen"], "band": self.band, "porch_band": self.porch["band"],
                      "freqs": list(dict.fromkeys(BANDS.get(self.porch["band"] or self.band, BANDS[self.band]))),
                      "hop_s": HOP_S.get(self.porch["band"] or self.band), "started": self.porch["started"],
                      "doing": self.porch.get("doing"), "next_weather_check": self.porch.get("next_weather_check"),
                      "last_station_search": self.porch.get("last_station_search"),
                      "last_decode": self.last_decode, "last_tpms": self.last_tpms, "decodes_10min": len(self.decode_times),
                      "open_passes": sum(len({r["sensor"] for r in p["readings"]}) for p in self.open.values())},
            "today": {**counts(midnight), "stopped": stopped_today},
            "last_hour": counts(t - 3600),
            "hours": hours, "days": days, "recent": recent, "passes": passes, "regulars": regulars, "models": models, "sensors": sensors, "tires": tires,
            "pressure_spread": [{"psi": k, "tires": n} for k, n in sorted(spread.items())],
            "totals": {"vehicles": totals[0], "sensors": totals[1], "passes": totals[2], "since": totals[3]},
            "settings": {"low_psi": LOW_PSI, "merge_after": MERGE_AFTER, "pass_gap_s": PASS_GAP_S, "stopped_s": STOPPED_S, "keep_days": KEEP_DAYS},
        }


    def sensor_rows(self):
        """Every sensor, most recently heard first, with when it's heard and its typical pressure."""
        db = self.db
        hours = collections.defaultdict(lambda: [0] * 24)
        for sensor, hour, n in db.execute("SELECT sensor, CAST(strftime('%H', t, 'unixepoch', 'localtime') AS INTEGER), "
                                          "COUNT(DISTINCT pass) FROM readings GROUP BY 1, 2"):
            hours[sensor][hour] = n
        rows = []
        for h, ident, model, vehicle, first, last, readings, passes, days, kpa, latest, temp in db.execute("""
                SELECT s.hash, s.ident, s.model, s.vehicle, s.first, s.last, s.readings,
                       COUNT(DISTINCT r.pass), COUNT(DISTINCT date(r.t, 'unixepoch', 'localtime')),
                       AVG(CASE WHEN r.kpa BETWEEN 103 AND 620 THEN r.kpa END),
                       (SELECT kpa FROM readings x WHERE x.sensor = s.hash AND x.kpa BETWEEN 103 AND 620 ORDER BY t DESC LIMIT 1),
                       AVG(CASE WHEN r.temp_c BETWEEN -40 AND 80 THEN r.temp_c END)
                FROM sensors s LEFT JOIN readings r ON r.sensor = s.hash
                GROUP BY s.hash ORDER BY s.last DESC LIMIT 500"""):
            rows.append({"sensor": h, "ident": ident, "model": model, "car": vehicle, "first": first, "last": last,
                         "readings": readings, "passes": passes, "days": days, "kpa": kpa, "latest_kpa": latest, "temp_c": temp, "hours": hours[h]})
        return rows

    def latest_pressures(self, since):
        """Each tire's most recent believable pressure (kPa) heard since `since`, so a tire counts once however often it passes."""
        return [kpa for (kpa,) in self.db.execute("""
            SELECT r.kpa FROM readings r JOIN (
                SELECT sensor, MAX(t) AS t FROM readings WHERE t >= ? AND kpa BETWEEN ? AND ? GROUP BY sensor
            ) latest ON latest.sensor = r.sensor AND latest.t = r.t""", (since, *PLAUSIBLE_KPA))]

    def tire_stats(self, since):
        pressures = self.latest_pressures(since)
        return {"tires": len(pressures), "low": sum(kpa < LOW_PSI * PSI_KPA for kpa in pressures)}

    def sensor_history(self, sensor):
        with self.lock:
            row = self.db.execute("SELECT ident, model, vehicle, first, last FROM sensors WHERE hash = ?", (sensor,)).fetchone()
            if not row:
                return None
            readings = [dict(zip(("t", "kpa", "temp_c", "rssi", "pass"), r)) for r in self.db.execute(
                "SELECT t, kpa, temp_c, rssi, pass FROM readings WHERE sensor = ? ORDER BY t", (sensor,))]
            readings += [{"t": r["t"], "kpa": r["kpa"], "temp_c": r["temp_c"], "rssi": r["rssi"], "pass": "now"}
                         for p in self.open.values() for r in p["readings"] if r["sensor"] == sensor]
        return {"sensor": sensor, "ident": row[0], "model": row[1], "car": row[2], "first": row[3], "last": row[4], "readings": readings}


def log(*parts):
    print(time.strftime("%F %T"), *parts, flush=True)


class Handler(BaseHTTPRequestHandler):
    tracker = None

    def send(self, code, body, kind, cache="no-cache"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/cars"):
            self.send_response(302)
            self.send_header("Location", "/cars/")
            self.end_headers()
        elif path == "/cars/":
            self.send(200, (APP / "www" / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ("/favicon.ico", "/apple-touch-icon.png"):
            kind = "image/x-icon" if path.endswith(".ico") else "image/png"
            self.send(200, (APP / "www" / path.lstrip("/")).read_bytes(), kind, "max-age=86400")
        elif path == "/cars/api/sensor":
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            history = self.tracker.sensor_history(query.get("s", [""])[0])
            if history is None:
                self.send(404, b"unknown sensor", "text/plain")
            else:
                self.send(200, json.dumps(history).encode(), "application/json", "no-store")
        elif path == "/cars/api/status":
            self.send(200, json.dumps(self.tracker.status()).encode(), "application/json", "no-store")
        elif path == "/weather":
            self.send_response(302)
            self.send_header("Location", "/weather/")
            self.end_headers()
        elif path == "/weather/":
            self.send(200, (APP / "www" / "weather.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ("/weather/api/status", "/weather/api/hourly", "/weather/api/daily"):
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            days = query.get("days", ["2"])[0]
            days = int(days) if days.isdigit() else 2
            station = self.tracker.weather
            result = {"status": station.status, "hourly": lambda: station.hourly(days), "daily": lambda: station.daily(days)}[path.rsplit("/", 1)[1]]()
            if path.endswith("/status"):  # when porch_pi next listens for the stations, and last searched for new ones
                porch = self.tracker.porch
                result["schedule"] = {"band": self.tracker.band, "doing": porch.get("doing"), "next_check": porch.get("next_weather_check"),
                                      "last_search": porch.get("last_station_search")}
            self.send(200, json.dumps(result).encode(), "application/json", "no-store")
        else:
            self.send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), MAX_POST)))
        except (ValueError, TypeError):
            body = None
        if self.path == "/cars/api/ingest" and isinstance(body, dict):
            self.send(200, json.dumps(self.tracker.ingest(body)).encode(), "application/json", "no-store")
            return
        if self.path == "/weather/api/station" and isinstance(body, dict):
            try:
                self.send(200, json.dumps(self.tracker.weather.update(body)).encode(), "application/json", "no-store")
            except KeyError:
                self.send(404, b"unknown station", "text/plain")
            return
        if self.path != "/cars/api/band":
            self.send(404, b"not found", "text/plain")
            return
        band = body.get("band") if isinstance(body, dict) else None
        if band not in BANDS:
            self.send(400, b"band must be 315, 433, both or 315w", "text/plain")
            return
        self.tracker.set_band(band)
        log("band set to", band)
        self.send(200, b'{"ok": true}', "application/json", "no-store")

    def log_message(self, *args):
        pass


def main():
    tracker = Tracker()
    threading.Thread(target=tracker.housekeeping, daemon=True).start()
    Handler.tracker = tracker
    log("serving on port", PORT)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
