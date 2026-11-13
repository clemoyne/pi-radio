#!/usr/bin/env python3
"""Logs aircraft heard by dump1090-fa and serves the watch list API. Standard library only (Python 3.7)."""
import ctypes
import functools
import http.server
import json
import math
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request

AIRCRAFT_JSON = "/run/dump1090-fa/aircraft.json"
RECEIVER_JSON = "/run/dump1090-fa/receiver.json"
SKYAWARE_DB = "/usr/share/skyaware/html/db"
STATE_DB = "/var/lib/aircraftlog/log.db"
TAGS_DB = "/opt/aircraftlog/tags.db"
REGISTRY_DB = "/opt/aircraftlog/registry.db"
AIRPORTS_JSON = "/opt/aircraftlog/airports.json"
CARGO_JSON = "/opt/aircraftlog/cargo.json"  # same file as sdrpi's flights/cargo.json
DATA_JSON = "/run/aircraftlog/data.json"

API_PORT = 8091
API_PREFIX = "/aircraft/api"
MAX_BODY_BYTES = 4096
MAX_WATCHES = 500
MAX_NOTE_LENGTH = 120
MAX_SEARCH_RESULTS = 200
MAX_EXPORT_VISITS = 100  # per request, so one export never holds the API for long on this Pi
SEARCH_SINCE_FIELDS = ("first_seen", "last_seen", "go_around_at")

POLL_SECONDS = 15
FLUSH_SECONDS = 60
PRUNE_SECONDS = 86400
VISIT_GAP_SECONDS = 1800
RETENTION_SECONDS = 30 * 86400
MIN_MESSAGES = 5
MAX_INTERESTING = 300
MAX_WATCH_VISITS = 100
MAX_TRACK_POINTS = 5000
EMERGENCY_SQUAWKS = ("7500", "7600", "7700")
IN_RANGE_SECONDS = 60  # "in range now", matching SkyAware; live visits last VISIT_GAP_SECONDS
EMERGENCY_LIST_SECONDS = 30 * 86400
MAX_EMERGENCY_LIST = 50
MAX_EMERGENCY_BYTES = 500 * 1000 * 1000
EMERGENCY_VISIT_BYTES = 400  # measured on-disk estimates, used only for the size limit
TRACK_POINT_BYTES = 48
CHART_SECONDS = 31 * 86400
PRIVATE_OWNERS = ("Individual", "LLC", "Co-owned", "Partnership")
AIRLINER_CATEGORIES = ("A4", "A5")
HELICOPTER = "(category = 'A7' OR aircraft_class IN ('Rotorcraft', 'Helicopter'))"

TRANSPORT_CATEGORIES = ("A2", "A3", "A4", "A5")
AIRLINE_CALLSIGN = re.compile(r"^[A-Z]{3}\d")
GO_AROUND_LOW_FT = 2500  # above the airport, from barometric altitude
GO_AROUND_MIN_CHECK_FT = 12000  # flights never below this can't have gone around, even at a high airport
GO_AROUND_RISE_FT = 1000  # descent before and climb after the low point
GO_AROUND_WINDOW_SECONDS = 300
GO_AROUND_MAX_GAP_SECONDS = 90
GO_AROUND_AIRPORT_KM = 15
GO_AROUND_MIN_KT = 100
GO_AROUND_NOTIFY_URL = "http://sdrpi.local:8080/api/goaround"  # sdrpi records the radio traffic
GO_AROUND_NOTIFY_MAX_AGE_SECONDS = 300
GO_AROUND_NOTIFY_TIMEOUT_SECONDS = 5
GO_AROUND_WATCH_AIRPORTS = ("CYYZ",)  # low flights near these are checked every poll instead of every flush
GO_AROUND_WATCH_KM = 25  # wider than GO_AROUND_AIRPORT_KM because the climb carries the flight past the low point
GO_AROUND_WATCH_FT = 5000  # above the airport
TIME_ERROR = 5  # adjtimex() return value while the clock is unsynchronized

MILITARY_BLOCKS = [(0xADF7C8, 0xAFFFFF), (0xC20000, 0xC3FFFF)]  # United States, Canada
MILITARY_CALLSIGN = re.compile(r"^(RCH|CNV|CFC|RRR|GAF|PAT|SAM|AF1)\d")

# Airline codes people type, mapped to the ICAO prefix aircraft broadcast.
IATA_TO_ICAO = {
    "AC": "ACA", "WS": "WJA", "PD": "POE", "TS": "TSC", "F8": "FLE", "WG": "SWG", "AA": "AAL", "DL": "DAL",
    "UA": "UAL", "B6": "JBU", "WN": "SWA", "AS": "ASA", "NK": "NKS", "F9": "FFT", "BA": "BAW", "LH": "DLH",
    "AF": "AFR", "KL": "KLM",
}

VISIT_COLUMNS = ["id", "hex", "reg", "type", "category", "callsign", "operator", "grp", "tags", "source", "ladd",
                 "first_seen", "last_seen", "messages", "alt_min", "alt_max", "min_dist_km", "closest_at",
                 "rssi_max", "squawk", "mlat", "make", "model", "year", "aircraft_class", "registrant_type",
                 "registrant", "owner_state", "owner_country", "track_points", "emergency", "private", "go_around", "go_around_at",
                 "cargo", "icao_type"]
DESCRIBE_COLUMNS = ["hex", "reg", "type", "icao_type", "make", "model", "registrant", "operator", "callsign", "category",
                    "grp", "tags", "private", "cargo", "ladd"]
MAX_DESCRIBE = 30
REGISTRY_COLUMNS =["make", "model", "year", "aircraft_class", "registrant_type", "registrant", "owner_state",
                    "owner_country"]
WATCH_MATCH = """((w.kind = 'callsign' AND v.callsign = w.value) OR (w.kind = 'reg' AND v.reg_norm = w.value)
                  OR (w.kind = 'hex' AND v.hex = w.value))
                 AND (w.since IS NULL OR v.last_seen >= w.since) AND (w.until IS NULL OR v.first_seen < w.until)"""

NUMBER_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O
stop_requested = False


def on_signal(_signum, _frame):
    global stop_requested
    stop_requested = True


def normalize_reg(reg):
    return re.sub(r"[^A-Z0-9]", "", reg.upper()) if reg else None


def n_letters(rem):
    if rem == 0:
        return ""
    rem -= 1
    return NUMBER_LETTERS[rem // 25] + n_letter(rem % 25)


def n_letter(rem):
    return "" if rem == 0 else NUMBER_LETTERS[rem - 1]


def n_number(value):
    offset = value - 0xA00001
    if offset < 0 or offset >= 915399:
        return None
    reg = "N%d" % (offset // 101711 + 1)
    offset %= 101711
    if offset <= 600:
        return reg + n_letters(offset)
    offset -= 601
    reg += str(offset // 10111)
    offset %= 10111
    if offset <= 600:
        return reg + n_letters(offset)
    offset -= 601
    reg += str(offset // 951)
    offset %= 951
    if offset <= 600:
        return reg + n_letters(offset)
    offset -= 601
    reg += str(offset // 35)
    offset %= 35
    if offset <= 24:
        return reg + n_letter(offset)
    return reg + str(offset - 25)


def canadian_reg(value):
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for start, prefix in ((0xC00001, "C-F"), (0xC044A9, "C-G")):
        offset = value - start
        if 0 <= offset < 26 ** 3:
            return prefix + letters[offset // 676] + letters[offset % 676 // 26] + letters[offset % 26]
    return None


@functools.lru_cache(maxsize=16)
def skyaware_file(prefix):
    try:
        with open(os.path.join(SKYAWARE_DB, prefix + ".json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def skyaware_lookup(hexid):
    prefix, rest = hexid[:1].upper(), hexid[1:].upper()
    while True:
        data = skyaware_file(prefix)
        if rest in data:
            return data[rest]
        next_prefix = prefix + rest[:1]
        if not rest or next_prefix not in data.get("children", []):
            return {}
        prefix, rest = next_prefix, rest[1:]


@functools.lru_cache(maxsize=2048)
def aircraft_identity(hexid):
    record = skyaware_lookup(hexid)
    value = int(hexid, 16)
    return record.get("r") or n_number(value) or canadian_reg(value), record.get("t")


def is_private(visit):
    # A best guess: the feed has no such field. Flying under its own registration is the usual private pattern;
    # fractional and charter jets use company callsigns but are registered to LLCs and co-owners.
    if visit["grp"]:
        return 0
    if visit["callsign"] and visit["callsign"] == normalize_reg(visit["reg"]):
        return 1
    return int(visit["registrant_type"] in PRIVATE_OWNERS and visit["category"] not in AIRLINER_CATEGORIES)


def distance_km(lat1, lon1, lat2, lon2):
    r = math.radians
    a = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def is_airliner(visit):
    return bool(not visit["grp"] and not visit["private"] and visit["category"] in TRANSPORT_CATEGORIES
                and visit["callsign"] and AIRLINE_CALLSIGN.match(visit["callsign"]))


def load_cargo_rules():
    try:
        with open(CARGO_JSON) as f:
            rules = json.load(f)
    except (OSError, ValueError) as error:
        print("cargo rules not loaded: %s" % error, flush=True)
        return None
    rules["owners"] = [(re.compile(pattern, re.I), name) for pattern, name in rules["owners"]]
    rules["freighter_makes"] = re.compile(rules["freighter_makes"], re.I)
    rules["freighter_models"] = re.compile(rules["freighter_models"], re.I)
    return rules


def cargo_carrier(visit, rules):
    """Cargo operator's name, or None: by callsign prefix, then registered owner, then a freighter model."""
    if not rules:
        return None
    match = AIRLINE_CALLSIGN.match(visit.get("callsign") or "")
    if match and visit["callsign"][:3] in rules["callsigns"]:
        return rules["callsigns"][visit["callsign"][:3]]
    owner = " ".join(filter(None, (visit.get("registrant"), visit.get("operator"))))
    for pattern, name in rules["owners"]:
        if owner and pattern.search(owner):
            return name or owner
    if (rules["freighter_makes"].search(visit.get("make") or "")
            and rules["freighter_models"].search(visit.get("model") or "")):
        return "Freighter" + (" (%s)" % visit["registrant"] if visit.get("registrant") else "")
    return None


def nearby_airport(airports, lat, lon):
    best, best_distance = None, GO_AROUND_AIRPORT_KM
    for airport in airports:
        if abs(airport[2] - lat) < 0.2:
            distance = distance_km(lat, lon, airport[2], airport[3])
            if distance <= best_distance:
                best, best_distance = airport, distance
    return best


def airborne_window(points, index, step):
    # Consecutive airborne points on one side of index, stopping at a coverage gap or a ground report.
    window = []
    i = index + step
    while 0 <= i < len(points):
        point, previous = points[i], points[i - step]
        if (abs(point[0] - points[index][0]) > GO_AROUND_WINDOW_SECONDS or point[3] <= 0
                or abs(point[0] - previous[0]) > GO_AROUND_MAX_GAP_SECONDS):
            break
        window.append(point[3])
        i += step
    return window


def detect_go_around(points, airports):
    """Finds a descent to a low point near an airport followed by a climb. points are (t, lat, lon, alt, gs), oldest
    first. Returns (airport ident, time of the low point), or None."""
    known = [point for point in points if point[3] is not None]
    for index, (t, lat, lon, altitude, speed) in enumerate(known):
        if altitude <= 0 or (speed is not None and speed < GO_AROUND_MIN_KT):
            continue
        before, after = airborne_window(known, index, -1), airborne_window(known, index, 1)
        if not before or not after or min(before + after) < altitude:
            continue
        if max(before) - altitude < GO_AROUND_RISE_FT or max(after) - altitude < GO_AROUND_RISE_FT:
            continue
        airport = nearby_airport(airports, lat, lon)
        if airport and altitude - airport[4] <= GO_AROUND_LOW_FT:
            return airport[0], t
    return None


def post_go_around(airport, callsign):
    body = json.dumps({"airport": airport, "callsign": callsign}).encode()
    request = urllib.request.Request(GO_AROUND_NOTIFY_URL, body, {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=GO_AROUND_NOTIFY_TIMEOUT_SECONDS).close()
    except OSError as error:
        print("go-around notify failed: %s" % error, flush=True)


def notify_go_around(airport, callsign):
    # On its own thread so a slow or absent sdrpi can't hold up logging.
    try:
        threading.Thread(target=post_go_around, args=(airport, callsign), daemon=True).start()
    except RuntimeError as error:
        print("go-around notify failed: %s" % error, flush=True)


def wait_for_clock():
    # No RTC: timestamps are meaningless until NTP has set the clock.
    libc = ctypes.CDLL(None)
    timex = ctypes.create_string_buffer(512)
    while not stop_requested and libc.adjtimex(timex) == TIME_ERROR:
        time.sleep(15)


def connect():
    db = sqlite3.connect(STATE_DB, timeout=10)
    db.execute("PRAGMA synchronous = NORMAL")
    return db


def with_seen_before(db, visits):
    """Adds seen_before to each visit: how many earlier flights by the same aircraft the log holds (it keeps 30 days)."""
    for visit in visits:
        visit["seen_before"] = db.execute("SELECT COUNT(*) FROM visits WHERE hex = ? AND first_seen < ?",
                                          (visit["hex"], visit["first_seen"])).fetchone()[0]
    return visits


def parse_watch(body):
    kind = body.get("kind")
    value = str(body.get("value", "")).strip().upper()
    if kind == "callsign":
        value = value.replace(" ", "")
        match = re.fullmatch(r"([A-Z0-9]{2})(\d{1,4}[A-Z]?)", value)
        if match and match.group(1) in IATA_TO_ICAO:
            value = IATA_TO_ICAO[match.group(1)] + match.group(2)
        if not re.fullmatch(r"[A-Z0-9]{2,8}", value):
            raise ValueError("Enter a flight like AC123 or ACA123.")
    elif kind == "reg":
        value = normalize_reg(value)
        if not value or len(value) > 10:
            raise ValueError("Enter a registration like C-FABC or N123AB.")
    elif kind == "hex":
        value = value.lower()
        if not re.fullmatch(r"[0-9a-f]{6}", value):
            raise ValueError("Enter a 6-character ICAO hex code.")
    else:
        raise ValueError("Choose a flight, registration or hex code.")
    note = str(body.get("note", "")).strip()[:MAX_NOTE_LENGTH]
    since, until = body.get("since"), body.get("until")
    for bound in (since, until):
        if bound is not None and (not isinstance(bound, int) or isinstance(bound, bool) or bound < 0):
            raise ValueError("Invalid date.")
    return kind, value, note, since, until


class ApiHandler(http.server.BaseHTTPRequestHandler):
    db = None
    wake = None
    live = None

    def log_message(self, *_args):
        pass

    def respond(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def same_origin(self):
        origin = self.headers.get("Origin")
        return origin is None or urllib.parse.urlsplit(origin).netloc == self.headers.get("Host")

    def respond_track(self, visit_id):
        row = self.db.execute("SELECT %s FROM visits WHERE id = ?" % ", ".join(VISIT_COLUMNS), (visit_id,)).fetchone()
        if row is None:
            self.respond(404, {"error": "That flight is no longer in the log."})
            return
        points = self.db.execute("""SELECT t, lat, lon, alt, gs FROM track_points WHERE visit_id = ?
                                    ORDER BY t DESC LIMIT ?""", (visit_id, MAX_TRACK_POINTS)).fetchall()
        self.respond(200, {"visit": dict(zip(VISIT_COLUMNS, row)), "points": points[::-1]})

    def respond_export(self, query):
        """Finished visits with their tracks, oldest first, for sdrpi's long-term store. A visit is finished once it
        has gone unheard for VISIT_GAP_SECONDS, so its row no longer changes. The cursor is (last_seen, id)."""
        def number(name):
            value = query.get(name, ["0"])[0]
            return int(value) if value.isdigit() else 0
        cutoff = int(time.time()) - VISIT_GAP_SECONDS
        after, after_id = number("after"), number("after_id")
        limit = min(max(number("limit"), 1), MAX_EXPORT_VISITS)
        rows = self.db.execute("""SELECT %s FROM visits WHERE last_seen <= ? AND (last_seen > ? OR (last_seen = ? AND id > ?))
                                  ORDER BY last_seen, id LIMIT ?""" % ", ".join(VISIT_COLUMNS),
                               (cutoff, after, after, after_id, limit)).fetchall()
        visits = [dict(zip(VISIT_COLUMNS, row)) for row in rows]
        for visit in visits:
            visit["points"] = self.db.execute("SELECT t, lat, lon, alt, gs FROM track_points WHERE visit_id = ? ORDER BY t",
                                              (visit["id"],)).fetchall()
        self.respond(200, {"visits": visits, "cutoff": cutoff})

    def respond_describe(self, query):
        """Who each aircraft is, for the display page: the live visit, else the latest logged one, else SkyAware's
        database. hex is a comma-separated list."""
        hexes = [h for h in query.get("hex", [""])[0].lower().split(",") if re.fullmatch(r"[0-9a-f]{6}", h)]
        found = {}
        for hexid in hexes[:MAX_DESCRIBE]:
            visit = self.live.get(hexid)
            if visit is None:
                row = self.db.execute("SELECT %s FROM visits WHERE hex = ? ORDER BY last_seen DESC LIMIT 1" % ", ".join(
                    VISIT_COLUMNS), (hexid,)).fetchone()
                visit = dict(zip(VISIT_COLUMNS, row)) if row else None
            if visit is None:
                reg, type_code = aircraft_identity(hexid)
                visit = {"hex": hexid, "reg": reg, "type": type_code, "icao_type": type_code}
            found[hexid] = {c: visit.get(c) for c in DESCRIBE_COLUMNS}
        self.respond(200, {"aircraft": found})

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == API_PREFIX + "/describe":
            self.respond_describe(urllib.parse.parse_qs(url.query))
            return
        track = re.fullmatch(re.escape(API_PREFIX) + r"/track/(\d+)", url.path)
        if track:
            self.respond_track(int(track.group(1)))
            return
        if url.path == API_PREFIX + "/export":
            self.respond_export(urllib.parse.parse_qs(url.query))
            return
        if url.path != API_PREFIX + "/search":
            self.respond(404, {"error": "Not found"})
            return
        query = urllib.parse.parse_qs(url.query)
        text = query.get("q", [""])[0].strip()[:40]
        group = query.get("group", [""])[0]
        clauses, params = [], []
        if text:
            pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            fields = ("hex", "callsign", "reg", "reg_norm", "operator", "type", "make", "model", "registrant",
                      "owner_state", "owner_country")
            clauses.append("(" + " OR ".join("%s LIKE ? ESCAPE '\\'" % c for c in fields) + ")")
            params += [pattern] * len(fields)
        if group in ("mil", "gov", "pol"):
            clauses.append("grp = ?")
            params.append(group)
        elif group == "ladd":
            clauses.append("ladd = 1")
        elif group == "tagged":
            clauses.append("(grp IS NOT NULL OR ladd = 1 OR go_around IS NOT NULL)")
        elif group == "go_around":
            clauses.append("go_around IS NOT NULL")
        elif group == "emergency":
            clauses.append("emergency IS NOT NULL")
        elif group == "private":
            clauses.append("private = 1")
        elif group == "helicopter":
            clauses.append(HELICOPTER)
        elif group == "cargo":
            clauses.append("cargo IS NOT NULL")
        # The page's summary tiles pass these so a list shows the same flights the tile counted
        since, field = query.get("since", [""])[0], query.get("field", ["first_seen"])[0]
        if since.isdigit() and field in SEARCH_SINCE_FIELDS:
            clauses.append("%s >= ?" % field)
            params.append(int(since))
        km = query.get("km", [""])[0]
        if re.fullmatch(r"\d+(\.\d+)?", km) and float(km) > 0:
            clauses.append("min_dist_km <= ?")
            params.append(float(km))
        where =" WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.db.execute("SELECT %s FROM visits%s ORDER BY last_seen DESC LIMIT ?" % (
            ", ".join(VISIT_COLUMNS), where), params + [MAX_SEARCH_RESULTS]).fetchall()
        self.respond(200, {"visits": with_seen_before(self.db, [dict(zip(VISIT_COLUMNS, row)) for row in rows])})

    def do_POST(self):
        if self.path != API_PREFIX + "/watch" or not self.same_origin():
            self.respond(403, {"error": "Not allowed"})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.respond(415, {"error": "JSON required"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self.respond(413, {"error": "Request too large"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Invalid request.")
            kind, value, note, since, until = parse_watch(body)
        except ValueError as err:
            self.respond(400, {"error": str(err)})
            return
        with self.db:
            if self.db.execute("SELECT COUNT(*) FROM watch").fetchone()[0] >= MAX_WATCHES:
                self.respond(409, {"error": "The watch list is full."})
                return
            self.db.execute("INSERT INTO watch (kind, value, note, since, until, added) VALUES (?, ?, ?, ?, ?, ?)",
                            (kind, value, note, since, until, int(time.time())))
        self.wake.set()
        self.respond(200, {"ok": True, "value": value})

    def do_DELETE(self):
        match = re.fullmatch(re.escape(API_PREFIX) + r"/watch/(\d+)", self.path)
        if not match or not self.same_origin():
            self.respond(403, {"error": "Not allowed"})
            return
        with self.db:
            self.db.execute("DELETE FROM watch WHERE id = ?", (int(match.group(1)),))
        self.wake.set()
        self.respond(200, {"ok": True})


class AircraftLog:
    def __init__(self):
        self.db = connect()
        self.check_existing_tracks = False
        self.init_db()
        self.tags = sqlite3.connect("file:%s?mode=ro&immutable=1" % TAGS_DB, uri=True)
        self.registry = sqlite3.connect("file:%s?mode=ro&immutable=1" % REGISTRY_DB, uri=True)
        with open(AIRPORTS_JSON) as f:
            self.airports = json.load(f)
        self.cargo_rules = load_cargo_rules()
        self.backfill_registry()
        self.backfill_cargo()
        if self.check_existing_tracks:
            self.backfill_go_arounds()
        self.live = {}
        self.finished = []
        self.receiver = None
        self.wake = threading.Event()
        self.restore_live()
        self.start_api()

    def init_db(self):
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY, hex TEXT NOT NULL, reg TEXT, reg_norm TEXT, type TEXT, category TEXT,
                callsign TEXT, operator TEXT, grp TEXT, tags TEXT, source TEXT, ladd INTEGER NOT NULL DEFAULT 0,
                first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, messages INTEGER, alt_min INTEGER,
                alt_max INTEGER, min_dist_km REAL, closest_at INTEGER, rssi_max REAL, squawk TEXT, mlat INTEGER);
            CREATE INDEX IF NOT EXISTS visits_last_seen ON visits (last_seen);
            CREATE INDEX IF NOT EXISTS visits_first_seen ON visits (first_seen);
            CREATE INDEX IF NOT EXISTS visits_hex ON visits (hex);
            CREATE INDEX IF NOT EXISTS visits_callsign ON visits (callsign);
            CREATE INDEX IF NOT EXISTS visits_reg_norm ON visits (reg_norm);
            CREATE TABLE IF NOT EXISTS watch (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL, note TEXT, since INTEGER,
                until INTEGER, added INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS track_points (
                visit_id INTEGER NOT NULL, t INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, alt INTEGER,
                gs REAL, PRIMARY KEY (visit_id, t)) WITHOUT ROWID;
        """)
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(visits)")}
        for column in REGISTRY_COLUMNS:
            if column not in existing:
                self.db.execute("ALTER TABLE visits ADD COLUMN %s TEXT" % column)
        if "track_points" not in existing:
            self.db.execute("ALTER TABLE visits ADD COLUMN track_points INTEGER NOT NULL DEFAULT 0")
        if "emergency" not in existing:
            self.db.execute("ALTER TABLE visits ADD COLUMN emergency TEXT")
            self.db.execute("UPDATE visits SET emergency = squawk WHERE squawk IN (%s)" % ",".join("?" * 3),
                            EMERGENCY_SQUAWKS)
        self.db.execute("CREATE INDEX IF NOT EXISTS visits_emergency ON visits (last_seen) WHERE emergency IS NOT NULL")
        if "private" not in existing:
            self.db.execute("ALTER TABLE visits ADD COLUMN private INTEGER NOT NULL DEFAULT 0")
            columns = ["id", "grp", "callsign", "reg", "registrant_type", "category"]
            for row in self.db.execute("SELECT %s FROM visits" % ", ".join(columns)).fetchall():
                visit = dict(zip(columns, row))
                self.db.execute("UPDATE visits SET private = ? WHERE id = ?", (is_private(visit), visit["id"]))
        self.db.execute("CREATE INDEX IF NOT EXISTS visits_private ON visits (first_seen) WHERE private = 1")
        self.db.execute("CREATE INDEX IF NOT EXISTS visits_helicopter ON visits (last_seen) WHERE " + HELICOPTER)
        if "go_around" not in existing:
            self.db.execute("ALTER TABLE visits ADD COLUMN go_around TEXT")
            self.db.execute("ALTER TABLE visits ADD COLUMN go_around_at INTEGER")
            self.check_existing_tracks = True
        self.db.execute("CREATE INDEX IF NOT EXISTS visits_go_around ON visits (go_around_at) WHERE go_around IS NOT NULL")
        if "cargo" not in existing:
            self.db.execute("ALTER TABLE visits ADD COLUMN cargo TEXT")
        if "icao_type" not in existing:
            # ICAO type designator (B748, A388) from SkyAware's database; "type" becomes the registry model when known
            self.db.execute("ALTER TABLE visits ADD COLUMN icao_type TEXT")
            for row_id, hexid in self.db.execute("SELECT id, hex FROM visits").fetchall():
                self.db.execute("UPDATE visits SET icao_type = ? WHERE id = ?", (aircraft_identity(hexid)[1], row_id))
        self.db.execute("CREATE INDEX IF NOT EXISTS visits_cargo ON visits (first_seen) WHERE cargo IS NOT NULL")
        self.db.commit()

    def apply_registry(self, visit):
        row = self.registry.execute("""SELECT source, make, model, year, category, registrant_type, registrant,
                                       owner_state, owner_country, government FROM registry WHERE hex = ?""",
                                    (visit["hex"],)).fetchone()
        if row is None:
            return False
        source, make, model, year, category, registrant_type, registrant, owner_state, owner_country, government = row
        visit.update(make=make, model=model, year=year, aircraft_class=category, registrant_type=registrant_type,
                     registrant=registrant, owner_state=owner_state, owner_country=owner_country,
                     type=model or visit["type"])
        if government and not visit["grp"]:
            visit.update(grp="gov", source=source + " registry")
        return True

    def backfill_cargo(self):
        # Runs at every start, so a changed cargo.json applies to the whole log
        columns = ["id", "callsign", "registrant", "operator", "make", "model", "cargo"]
        with self.db:
            for row in self.db.execute("SELECT %s FROM visits" % ", ".join(columns)).fetchall():
                visit = dict(zip(columns, row))
                cargo = cargo_carrier(visit, self.cargo_rules)
                if cargo != visit["cargo"]:
                    self.db.execute("UPDATE visits SET cargo = ? WHERE id = ?", (cargo, visit["id"]))

    def backfill_registry(self):
        columns = ["id", "hex", "ladd", "type", "grp", "source"] + REGISTRY_COLUMNS
        rows = self.db.execute("""SELECT %s FROM visits
                                  WHERE make IS NULL OR registrant IS NULL OR owner_country IS NULL
                                  OR type IS NOT model""" % ", ".join(columns)
                               ).fetchall()
        updates = ["type", "grp", "source"] + REGISTRY_COLUMNS
        with self.db:
            for row in rows:
                visit = dict(zip(columns, row))
                if self.apply_registry(visit):
                    self.db.execute("UPDATE visits SET %s WHERE id = ?" % ", ".join(c + " = ?" for c in updates),
                                    [visit[c] for c in updates] + [visit["id"]])

    def check_go_around(self, visit, live=False):
        if visit["go_around"] or not is_airliner(visit) or visit["alt_min"] is None:
            return
        if visit["alt_min"] > GO_AROUND_MIN_CHECK_FT:
            return
        points = self.db.execute("SELECT t, lat, lon, alt, gs FROM track_points WHERE visit_id = ? ORDER BY t",
                                 (visit["id"],)).fetchall()
        found = detect_go_around(points + visit.get("track", []), self.airports)
        if found:
            visit["go_around"], visit["go_around_at"] = found
            self.db.execute("UPDATE visits SET go_around = ?, go_around_at = ? WHERE id = ?", found + (visit["id"],))
            if live and time.time() - found[1] <= GO_AROUND_NOTIFY_MAX_AGE_SECONDS:
                notify_go_around(found[0], visit["callsign"])

    def near_watched_airport(self, point):
        _, lat, lon, altitude, _ = point
        return altitude is not None and any(
            airport[0] in GO_AROUND_WATCH_AIRPORTS and altitude - airport[4] <= GO_AROUND_WATCH_FT
            and distance_km(lat, lon, airport[2], airport[3]) <= GO_AROUND_WATCH_KM for airport in self.airports)

    def watch_approaches(self):
        # Works from the unwritten track too, so this adds no database writes until a go-around is found.
        with self.db:
            for visit in self.live.values():
                if visit["track"] and self.near_watched_airport(visit["track"][-1]):
                    self.check_go_around(visit, live=True)

    def backfill_go_arounds(self):
        columns = ["id", "grp", "private", "category", "callsign", "alt_min", "go_around", "go_around_at"]
        rows = self.db.execute("SELECT %s FROM visits WHERE track_points > 0" % ", ".join(columns)).fetchall()
        with self.db:
            for row in rows:
                self.check_go_around(dict(zip(columns, row)))

    def start_api(self):
        # live is only read by the handler, and single dict lookups are safe across threads
        handler = type("Handler", (ApiHandler,), {"wake": self.wake, "live": self.live})
        server = http.server.HTTPServer(("127.0.0.1", API_PORT), handler)

        def serve():
            handler.db = connect()
            server.serve_forever()

        threading.Thread(target=serve, daemon=True).start()

    def restore_live(self):
        cutoff = int(time.time()) - VISIT_GAP_SECONDS
        rows = self.db.execute("SELECT %s FROM visits WHERE last_seen >= ?" % ", ".join(VISIT_COLUMNS),
                               (cutoff,)).fetchall()
        for row in rows:
            visit = dict(zip(VISIT_COLUMNS, row))
            visit.update(dirty=False, track=[], last_position=None)
            self.live[visit["hex"]] = visit

    def classify(self, visit):
        visit["ladd"] = 0
        tag = self.tags.execute("SELECT reg, operator, type, type_code, grp, tags FROM tagged WHERE hex = ?",
                                (visit["hex"],)).fetchone()
        if tag:
            reg, operator, type_name, type_code, group, tags = tag
            visit.update(reg=visit["reg"] or reg or None, operator=operator or None, grp=group,
                         type=visit["type"] or type_code or type_name or None, tags=tags or None,
                         source="plane-alert-db")
        else:
            value = int(visit["hex"], 16)
            if any(low <= value <= high for low, high in MILITARY_BLOCKS):
                visit.update(grp="mil", source="military ID block")
            elif visit["callsign"] and MILITARY_CALLSIGN.match(visit["callsign"]):
                visit.update(grp="mil", source="military callsign")
        tokens = [token for token in (normalize_reg(visit["reg"]), visit["callsign"]) if token]
        if tokens and self.tags.execute("SELECT 1 FROM ladd WHERE token IN (%s) LIMIT 1" % ",".join("?" * len(tokens)),
                                        tokens).fetchone():
            visit["ladd"] = 1
        self.apply_registry(visit)
        visit["cargo"] = cargo_carrier(visit, self.cargo_rules)

    def new_visit(self, hexid, now):
        reg, type_code = aircraft_identity(hexid)
        return {"id": None, "hex": hexid, "reg": reg, "type": type_code, "category": None, "callsign": None,
                "operator": None, "grp": None, "tags": None, "source": None, "ladd": 0, "first_seen": now,
                "last_seen": now, "messages": 0, "alt_min": None, "alt_max": None, "min_dist_km": None,
                "closest_at": None, "rssi_max": None, "squawk": None, "mlat": 0, "make": None, "model": None,
                "year": None, "aircraft_class": None, "registrant_type": None, "registrant": None,
                "owner_state": None, "owner_country": None, "track_points": 0, "emergency": None, "private": 0,
                "go_around": None, "go_around_at": None, "cargo": None, "icao_type": type_code, "track": [],
                "last_position": None, "dirty": True}

    @staticmethod
    def add_track_point(visit, aircraft, now):
        position = (round(aircraft["lat"], 5), round(aircraft["lon"], 5))
        if position == visit["last_position"]:
            return
        visit["last_position"] = position
        altitude = aircraft.get("alt_baro")
        visit["track"].append((now - int(aircraft["seen_pos"]), position[0], position[1],
                               0 if altitude == "ground" else altitude, aircraft.get("gs")))

    def update_visit(self, visit, aircraft, callsign, now):
        first_callsign = callsign and visit["callsign"] is None
        if first_callsign:
            visit["callsign"] = callsign
        if first_callsign or visit["id"] is None:
            self.classify(visit)
        visit["last_seen"] = now
        visit["messages"] = aircraft.get("messages", visit["messages"])
        visit["category"] = aircraft.get("category", visit["category"])
        visit["squawk"] = aircraft.get("squawk", visit["squawk"])
        if visit["squawk"] in EMERGENCY_SQUAWKS:
            visit["emergency"] = visit["squawk"]
        if aircraft.get("mlat"):
            visit["mlat"] = 1
        rssi = aircraft.get("rssi")
        if rssi is not None:
            visit["rssi_max"] = rssi if visit["rssi_max"] is None else max(visit["rssi_max"], rssi)
        altitude = aircraft.get("alt_baro")
        if isinstance(altitude, (int, float)):
            visit["alt_min"] = altitude if visit["alt_min"] is None else min(visit["alt_min"], altitude)
            visit["alt_max"] = altitude if visit["alt_max"] is None else max(visit["alt_max"], altitude)
        if self.receiver and "lat" in aircraft and aircraft.get("seen_pos", 999) <= POLL_SECONDS * 2:
            distance = distance_km(self.receiver[0], self.receiver[1], aircraft["lat"], aircraft["lon"])
            if visit["min_dist_km"] is None or distance < visit["min_dist_km"]:
                visit["min_dist_km"] = round(distance, 1)
                visit["closest_at"] = now
        if "lat" in aircraft and aircraft.get("seen_pos", 999) <= POLL_SECONDS * 2:
            self.add_track_point(visit, aircraft, now)
        visit["dirty"] = True

    def poll(self, now):
        try:
            with open(AIRCRAFT_JSON) as f:
                aircraft_list = json.load(f)["aircraft"]
        except (OSError, ValueError, KeyError):
            return
        for aircraft in aircraft_list:
            hexid = aircraft.get("hex", "")
            if not re.fullmatch(r"[0-9a-f]{6}", hexid) or aircraft.get("seen", 999) > POLL_SECONDS * 2:
                continue
            if aircraft.get("messages", 0) < MIN_MESSAGES:
                continue
            callsign = (aircraft.get("flight") or "").strip().upper() or None
            visit = self.live.get(hexid)
            if visit and callsign and visit["callsign"] and callsign != visit["callsign"]:
                self.finished.append(visit)
                visit = None
            if visit is None:
                visit = self.live[hexid] = self.new_visit(hexid, now)
            self.update_visit(visit, aircraft, callsign, now)

    def write_visit(self, visit):
        visit["track_points"] += len(visit["track"])
        visit["private"] = is_private(visit)
        columns = [c for c in VISIT_COLUMNS if c != "id"]
        values = [visit[c] for c in columns]
        if visit["id"] is None:
            cursor = self.db.execute("INSERT INTO visits (reg_norm, %s) VALUES (?, %s)" % (
                ", ".join(columns), ", ".join("?" * len(columns))), [normalize_reg(visit["reg"])] + values)
            visit["id"] = cursor.lastrowid
        else:
            self.db.execute("UPDATE visits SET reg_norm = ?, %s WHERE id = ?" % ", ".join(c + " = ?" for c in columns),
                            [normalize_reg(visit["reg"])] + values + [visit["id"]])
        self.db.executemany("INSERT OR IGNORE INTO track_points VALUES (?, ?, ?, ?, ?, ?)",
                            [(visit["id"],) + point for point in visit["track"]])
        visit["track"] = []
        visit["dirty"] = False

    def load_receiver(self):
        try:
            with open(RECEIVER_JSON) as f:
                data = json.load(f)
            self.receiver = (data["lat"], data["lon"])
        except (OSError, ValueError, KeyError):
            self.receiver = None

    def flush(self, now):
        expired = [hexid for hexid, v in self.live.items() if now - v["last_seen"] > VISIT_GAP_SECONDS]
        with self.db:
            for visit in self.finished + list(self.live.values()):
                if visit["dirty"]:
                    self.write_visit(visit)
                    self.check_go_around(visit, live=True)
        self.finished = []
        for hexid in expired:
            del self.live[hexid]

    def delete_visits(self, where, params):
        self.db.execute("DELETE FROM track_points WHERE visit_id IN (SELECT id FROM visits WHERE %s)" % where, params)
        self.db.execute("DELETE FROM visits WHERE " + where, params)

    def prune(self, now):
        expired = "last_seen < ? AND NOT EXISTS (SELECT 1 FROM watch w WHERE %s)" % WATCH_MATCH.replace("v.", "visits.")
        cutoff = (now - RETENTION_SECONDS,)
        # Emergency flights outlive the normal limit, newest first, until they reach the size limit.
        stored = 0
        over_limit = []
        for visit_id, points in self.db.execute("""SELECT id, track_points FROM visits
                                                   WHERE emergency IS NOT NULL AND %s ORDER BY last_seen DESC""" % expired,
                                                cutoff).fetchall():
            stored += EMERGENCY_VISIT_BYTES + points * TRACK_POINT_BYTES
            if stored > MAX_EMERGENCY_BYTES:
                over_limit.append(visit_id)
        with self.db:
            self.delete_visits("emergency IS NULL AND " + expired, cutoff)
            for visit_id in over_limit:
                self.delete_visits("id = ?", (visit_id,))

    def rows(self, sql, params):
        return with_seen_before(self.db, [dict(zip(VISIT_COLUMNS, row)) for row in self.db.execute(sql, params).fetchall()])

    def build_data(self, now):
        columns = ", ".join(VISIT_COLUMNS)
        day_ago = now - 86400
        visits_24h, mil, gov, pol, ladd, cargo = self.db.execute("""
            SELECT COUNT(*), COALESCE(SUM(grp = 'mil'), 0), COALESCE(SUM(grp = 'gov'), 0),
                   COALESCE(SUM(grp = 'pol'), 0), COALESCE(SUM(ladd), 0), COALESCE(SUM(cargo IS NOT NULL), 0)
            FROM visits WHERE first_seen >= ?""", (day_ago,)).fetchone()
        since = self.db.execute("SELECT MIN(first_seen) FROM visits").fetchone()[0]
        # Each flight counts once: an opt-out flight that is also military stays under military.
        # Hourly because the Pi runs on UTC; the page groups these into the viewer's local days.
        hourly = [[row[0] * 3600] + [value or 0 for value in row[1:]] for row in self.db.execute("""
            SELECT first_seen / 3600, SUM(grp = 'mil'), SUM(grp = 'gov'), SUM(grp = 'pol'),
                   SUM(grp IS NULL AND ladd = 1)
            FROM visits WHERE (grp IS NOT NULL OR ladd = 1) AND first_seen >= ? GROUP BY 1""", (now - CHART_SECONDS,))]
        # Sent per flight so the page can count distinct aircraft per local day at any distance.
        private = self.db.execute("SELECT first_seen, hex, min_dist_km FROM visits WHERE private = 1 AND first_seen >= ?",
                                  (now - CHART_SECONDS,)).fetchall()
        go_arounds = [row[0] for row in self.db.execute("""SELECT go_around_at FROM visits
                                                           WHERE go_around IS NOT NULL AND go_around_at >= ?""",
                                                        (now - CHART_SECONDS,))]
        interesting = self.rows("""SELECT %s FROM visits
                                   WHERE (grp IS NOT NULL OR ladd = 1 OR go_around IS NOT NULL) AND last_seen >= ?
                                   ORDER BY last_seen DESC LIMIT ?""" % columns,
                                (now - RETENTION_SECONDS, MAX_INTERESTING))
        watches = []
        for row in self.db.execute("SELECT id, kind, value, note, since, until, added FROM watch ORDER BY added DESC"):
            watch = dict(zip(["id", "kind", "value", "note", "since", "until", "added"], row))
            match = WATCH_MATCH.replace("w.kind", "?").replace("w.value", "?").replace("w.since", "?").replace(
                "w.until", "?")
            params = [watch[part] for part in re.findall(r"w\.(kind|value|since|until)", WATCH_MATCH)]
            watch["visits"] = self.rows("SELECT %s FROM visits v WHERE %s ORDER BY v.last_seen DESC LIMIT ?" % (
                columns, match), params + [MAX_WATCH_VISITS])
            watches.append(watch)
        in_range = [v for v in self.live.values() if now - v["last_seen"] <= IN_RANGE_SECONDS]
        emergencies_now = [{c: v[c] for c in VISIT_COLUMNS} for v in in_range if v["squawk"] in EMERGENCY_SQUAWKS]
        emergencies = self.rows("""SELECT %s FROM visits WHERE emergency IS NOT NULL AND last_seen >= ?
                                   ORDER BY last_seen DESC LIMIT ?""" % columns, (now - EMERGENCY_LIST_SECONDS, MAX_EMERGENCY_LIST))
        return {
            "generated": now, "tracking_since": since, "retention_days": RETENTION_SECONDS // 86400,
            "summary": {"now": len(in_range), "recent": len(self.live), "visits_24h": visits_24h, "mil_24h": mil,
                        "gov_24h": gov, "pol_24h": pol, "ladd_24h": ladd, "cargo_24h": cargo},
            "hourly": hourly, "private": private, "go_arounds": go_arounds, "interesting": interesting, "watches": watches, "receiver": self.receiver,
            "emergencies_now": emergencies_now, "emergencies": emergencies,
        }

    def write_json(self, now):
        tmp = DATA_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.build_data(now), f, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, DATA_JSON)

    def run(self):
        self.load_receiver()
        now = time.time()
        next_poll = now
        next_flush = now + 5
        next_prune = now + 60
        while not stop_requested:
            self.wake.wait(1)
            now = time.time()
            if now >= next_poll:
                next_poll = now + POLL_SECONDS
                self.poll(int(now))
                self.watch_approaches()
            if now >= next_flush or self.wake.is_set():
                self.wake.clear()
                next_flush = now + FLUSH_SECONDS
                self.load_receiver()
                self.flush(int(now))
                self.write_json(int(now))
            if now >= next_prune:
                next_prune = now + PRUNE_SECONDS
                self.prune(int(now))
        self.flush(int(time.time()))


def main():
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    wait_for_clock()
    if stop_requested:
        return 0
    AircraftLog().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
