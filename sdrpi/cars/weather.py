"""Weather from the wireless outdoor sensors and weather stations porch_pi hears. The car store hands over every non-TPMS
message that carries a weather measurement (temperature, humidity, wind, rain, pressure, UV or light), so any station in
range is found on its own: each one becomes a row in `sensors`, and the page lists them under Stations, where they can be
named and one picked as the dashboard's main station. Every reading is kept, with the raw message; the page charts every
reading and lists daily summaries. Served by the car store at /weather/. The dashboard's name is a setting, not part of the code."""
import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("WEATHER_DB", "/mnt/data/weather/weather.db"))
SETTINGS_PATH = DB_PATH.with_name("settings.json")  # {"sensor": main station, "title": dashboard name}
DEFAULT_TITLE = "Weather"
PLAUSIBLE_C = (-50, 60)  # outside this a reading is a misdecode
MIN_READINGS = 3  # a sensor heard fewer times than this is probably one misdecoded message, so it isn't offered yet
MAX_DAYS = 3660
MAX_NAME = 60
REPEAT_S = 3
WEATHER_BANDS = {"433.92M": (433.0, 435.0), "915M": (902.0, 928.0), "868M": (863.0, 870.0), "315M": (314.0, 316.0)}
# rtl_433 field names for each kind of measurement; a station's "measures" are the kinds its messages carry
MEASURES = {
    "temperature": ("temperature_C", "temperature_F", "temperature_1_C", "temperature_2_C"),
    "humidity": ("humidity", "humidity_1", "humidity_2"),
    "wind": ("wind_avg_km_h", "wind_avg_m_s", "wind_avg_mi_h", "wind_max_km_h", "wind_max_m_s", "wind_dir_deg"),
    "rain": ("rain_mm", "rain_in", "rain_rate_mm_h", "rain_rate_in_h"),
    "pressure": ("pressure_hPa", "pressure_inHg"),
    "uv": ("uv", "uvi"),
    "light": ("light_lux", "lux"),
}


def temperature(m):
    if isinstance(m.get("temperature_C"), (int, float)):
        return float(m["temperature_C"])
    if isinstance(m.get("temperature_F"), (int, float)):
        return round((m["temperature_F"] - 32) / 1.8, 2)
    return None


def measures(m):
    return [kind for kind, fields in MEASURES.items() if any(isinstance(m.get(f), (int, float)) for f in fields)]


def is_weather(m):
    """A weather message: not a tire sensor, and carrying at least one weather measurement."""
    return m.get("type") != "TPMS" and bool(measures(m))


def dew_point(temp_c, humidity):
    if temp_c is None or not humidity:
        return None
    a, b = 17.62, 243.12  # Magnus formula
    gamma = math.log(humidity / 100) + a * temp_c / (b + temp_c)
    return round(b * gamma / (a - gamma), 1)


def clean_name(text):
    return " ".join(str(text or "").split())[:MAX_NAME]


class Weather:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS sensors (sensor TEXT PRIMARY KEY, model TEXT, ident TEXT, channel TEXT,
                first REAL, last REAL, readings INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS readings (t REAL NOT NULL, sensor TEXT NOT NULL, temp_c REAL, humidity REAL,
                battery_ok INTEGER, rssi REAL, snr REAL, mhz REAL, raw TEXT);
            CREATE INDEX IF NOT EXISTS readings_sensor_t ON readings (sensor, t);
        """)
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(sensors)")]
        if "name" not in columns:
            self.db.execute("ALTER TABLE sensors ADD COLUMN name TEXT")
        if "measures" not in columns:
            self.db.execute("ALTER TABLE sensors ADD COLUMN measures TEXT")
        for (sensor,) in self.db.execute("SELECT sensor FROM sensors WHERE measures IS NULL").fetchall():
            raw = self.db.execute("SELECT raw FROM readings WHERE sensor = ? ORDER BY t DESC LIMIT 1", (sensor,)).fetchone()
            if raw and raw[0]:
                self.db.execute("UPDATE sensors SET measures = ? WHERE sensor = ?", (",".join(measures(json.loads(raw[0]))), sensor))
        self.db.commit()

    def store(self, m, t):
        kinds = measures(m)
        if not kinds:
            return
        temp, humidity = temperature(m), m.get("humidity")
        if temp is not None and not PLAUSIBLE_C[0] <= temp <= PLAUSIBLE_C[1]:
            temp = None
        if not isinstance(humidity, (int, float)) or not 0 <= humidity <= 100:
            humidity = None
        ident, channel = str(m.get("id", "")), str(m.get("channel", ""))
        sensor = "|".join([m["model"], ident, channel])
        battery = m.get("battery_ok")
        with self.lock:
            last = self.db.execute("SELECT MAX(t) FROM readings WHERE sensor = ? AND t > ?", (sensor, t - REPEAT_S)).fetchone()[0]
            if last is not None and abs(t - last) < REPEAT_S:
                return  # the sensor sends each reading a few times in a burst; one is enough
            self.db.execute("INSERT INTO sensors (sensor, model, ident, channel, first, last, readings, measures) "
                            "VALUES (?, ?, ?, ?, ?, ?, 1, ?) ON CONFLICT (sensor) DO UPDATE SET last = MAX(last, excluded.last), "
                            "readings = readings + 1, measures = excluded.measures",
                            (sensor, m["model"], ident, channel, t, t, ",".join(kinds)))
            self.db.execute("INSERT INTO readings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (t, sensor, temp, humidity, None if battery is None else int(bool(battery)), m.get("rssi"),
                             m.get("snr"), m.get("freq"), json.dumps(m)))
            self.db.commit()

    def settings(self):
        try:
            saved = json.loads(SETTINGS_PATH.read_text())
            return saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self, **changes):
        settings = {**self.settings(), **changes}
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings))
        tmp.replace(SETTINGS_PATH)

    def main_sensor(self):
        """The chosen station, else the one with temperature heard most, else whichever was heard most."""
        rows = self.db.execute("SELECT sensor, measures FROM sensors WHERE readings >= ? ORDER BY readings DESC",
                               (MIN_READINGS,)).fetchall()
        chosen = self.settings().get("sensor")
        if chosen in [r[0] for r in rows]:
            return chosen
        with_temp = [r[0] for r in rows if "temperature" in (r[1] or "")]
        return (with_temp or [r[0] for r in rows] or [None])[0]

    def update(self, body):
        """From the page: {"title"} renames the dashboard; {"sensor", "name"} names a station; {"sensor", "main": true}
        makes it the dashboard's station."""
        with self.lock:
            if "title" in body:
                self.save_settings(title=clean_name(body["title"]) or DEFAULT_TITLE)
            sensor = body.get("sensor")
            if sensor is not None:
                if not self.db.execute("SELECT 1 FROM sensors WHERE sensor = ?", (sensor,)).fetchone():
                    raise KeyError("unknown station")
                if "name" in body:
                    self.db.execute("UPDATE sensors SET name = ? WHERE sensor = ?", (clean_name(body["name"]) or None, sensor))
                    self.db.commit()
                if body.get("main"):
                    self.save_settings(sensor=sensor)
        return self.status()

    def freqs(self):
        """rtl_433 frequencies for porch_pi's 15-minute weather checks: the bands confirmed stations transmit on."""
        with self.lock:
            rows = self.db.execute("""SELECT AVG(r.mhz) FROM readings r JOIN sensors s ON s.sensor = r.sensor
                                      WHERE s.readings >= ? AND r.mhz IS NOT NULL GROUP BY r.sensor""", (MIN_READINGS,)).fetchall()
        found = []
        for (mhz,) in rows:
            band = next((name for name, (lo, hi) in WEATHER_BANDS.items() if lo <= mhz <= hi), None)
            if band and band not in found:
                found.append(band)
        return found or ["433.92M"]

    def stations(self):
        """Every sensor heard, with what it measures and its latest values; ones heard too few times are marked."""
        out = []
        for sensor, model, ident, channel, first, last, readings, name, kinds in self.db.execute(
                "SELECT sensor, model, ident, channel, first, last, readings, name, measures FROM sensors ORDER BY last DESC"):
            latest = self.db.execute("SELECT temp_c, humidity, battery_ok, raw FROM readings WHERE sensor = ? ORDER BY t DESC LIMIT 1",
                                     (sensor,)).fetchone()
            values = {}
            if latest:
                raw = json.loads(latest[3] or "{}")
                values = {"temp_c": latest[0], "humidity": latest[1], "battery_ok": latest[2],
                          "other": {f: raw[f] for fields in list(MEASURES.values())[2:] for f in fields if isinstance(raw.get(f), (int, float))}}
            out.append({"sensor": sensor, "model": model, "ident": ident, "channel": channel, "first": first, "last": last,
                        "readings": readings, "name": name, "measures": (kinds or "").split(",") if kinds else [],
                        "confirmed": readings >= MIN_READINGS, "latest": values})
        return out

    def status(self):
        with self.lock:
            sensor = self.main_sensor()
            stations = self.stations()
            latest = self.db.execute("SELECT t, temp_c, humidity, battery_ok, rssi FROM readings WHERE sensor = ? ORDER BY t DESC LIMIT 1",
                                     (sensor,)).fetchone() if sensor else None
            day = time.time() - 86400
            today = self.db.execute("SELECT MIN(temp_c), MAX(temp_c), MIN(humidity), MAX(humidity), COUNT(*) FROM readings "
                                    "WHERE sensor = ? AND t >= ?", (sensor, day)).fetchone() if sensor else None
            # the change over the hour before the latest reading, if there's a reading 50-70 minutes before it
            hour_ago = self.db.execute("SELECT temp_c FROM readings WHERE sensor = ? AND t BETWEEN ? AND ? AND temp_c IS NOT NULL "
                                       "ORDER BY ABS(t - ?) LIMIT 1",
                                       (sensor, latest[0] - 4200, latest[0] - 3000, latest[0] - 3600)).fetchone() if latest else None
            total = self.db.execute("SELECT COUNT(*), MIN(t) FROM readings").fetchone()
        current = None
        if latest:
            t, temp, humidity, battery, rssi = latest
            current = {"t": t, "temp_c": temp, "humidity": humidity, "battery_ok": battery, "rssi": rssi,
                       "dew_point_c": dew_point(temp, humidity),
                       "change_1h": round(temp - hour_ago[0], 1) if temp is not None and hour_ago else None}
        return {"now": time.time(), "title": self.settings().get("title") or DEFAULT_TITLE, "sensor": sensor,
                "stations": stations, "current": current,
                "last_24h": dict(zip(["temp_min", "temp_max", "hum_min", "hum_max", "readings"], today)) if today else None,
                "readings": total[0], "since": total[1], "min_readings": MIN_READINGS}

    def hourly(self, days):
        """Per hour: low, average and high temperature, average humidity and how many readings, for the main station."""
        days = max(1, min(MAX_DAYS, days))
        with self.lock:
            sensor = self.main_sensor()
            rows = self.db.execute("""SELECT CAST(t / 3600 AS INTEGER) * 3600 AS hour, MIN(temp_c), AVG(temp_c), MAX(temp_c),
                                             AVG(humidity), COUNT(*) FROM readings WHERE sensor = ? AND t >= ?
                                      GROUP BY hour ORDER BY hour""", (sensor, time.time() - days * 86400)).fetchall()
        return {"sensor": sensor, "hours": [{"t": h, "min": r1(lo), "avg": r1(avg), "max": r1(hi), "humidity": r1(hum), "n": n}
                                            for h, lo, avg, hi, hum, n in rows]}

    def readings(self, days):
        """Every reading of the main station in the period, oldest first, as [time, temperature °C, humidity %]."""
        days = max(1, min(MAX_DAYS, days))
        with self.lock:
            sensor = self.main_sensor()
            rows = self.db.execute("SELECT t, temp_c, humidity FROM readings WHERE sensor = ? AND t >= ? ORDER BY t",
                                   (sensor, time.time() - days * 86400)).fetchall()
        return {"sensor": sensor, "readings": [[t, r1(temp), r1(hum)] for t, temp, hum in rows]}

    def daily(self, days):
        """Per local day: low and high temperature, average humidity, for the main station."""
        days = max(1, min(MAX_DAYS, days))
        with self.lock:
            sensor = self.main_sensor()
            rows = self.db.execute("""SELECT date(t, 'unixepoch', 'localtime') AS day, MIN(temp_c), MAX(temp_c), AVG(temp_c),
                                             MIN(humidity), MAX(humidity), AVG(humidity), COUNT(*)
                                      FROM readings WHERE sensor = ? AND t >= ? GROUP BY day ORDER BY day""",
                                   (sensor, time.time() - days * 86400)).fetchall()
        return {"sensor": sensor, "days": [{"day": d, "min": r1(lo), "max": r1(hi), "avg": r1(avg), "hum_min": r1(hmin),
                                            "hum_max": r1(hmax), "humidity": r1(hum), "n": n}
                                           for d, lo, hi, avg, hmin, hmax, hum, n in rows]}


def r1(value):
    return None if value is None else round(value, 1)
