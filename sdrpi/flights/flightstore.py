#!/usr/bin/env python3
"""Long-term flight store. Pulls finished flights from garage_pi's aircraft log, works out takeoffs, landings and how
close each flight came to the house, keeps everything on the SSD within MAX_DB_BYTES, and answers the Flights page."""
import collections
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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent
DATA = Path(os.environ.get("FLIGHTS_DATA", "/mnt/data/flights"))
DB_PATH = DATA / "flights.db"
STATUS_PATH = Path("/run/flightstore/status.json")
EXPORT_URL = "http://piaware.local/aircraft/api/export"
# The house's position comes from local.json next to this file, which isn't published; see local.example.json
HOME = tuple(json.loads((APP / "local.json").read_text())["home"])
API_PORT = 8094
API_PREFIX = "/flights/api"

SYNC_SECONDS = 600
BATCH = 100
BATCH_PAUSE_SECONDS = 1  # between export requests, so the Pi 3 running PiAware is never kept busy
FETCH_TIMEOUT_SECONDS = 30
MAX_DB_BYTES = 10 * 10**9
PRUNE_TO_BYTES = int(9.5 * 10**9)
PRUNE_CHUNK = 2000
AIRPORT_RADIUS_KM = 400  # airports farther than this are never heard low enough to matter

# Takeoff: the track starts low near an airport and climbs. Landing: it ends low near one after descending.
EVENT_AIRPORT_KM = 8
EVENT_LOW_FT = 1500  # above the airport
# The receiver, 15 km from Pearson, first hears most of its departures 1,500-2,000 ft up, 3-4 km out, while arrivals come
# lower before they drop out of range; at 1,500 ft about a third of Pearson's takeoffs were missed (284 against 469
# landings on 12 Nov). At 2,500 the two match within a few percent each day and Billy Bishop is unchanged
EVENT_TAKEOFF_LOW_FT = 2500
EVENT_CLIMB_FT = 1000
EVENT_EDGE_SECONDS = 240  # how far into the start or end of a track to look
SEGMENT_GAP_SECONDS = 120  # longer gaps aren't joined when finding the closest approach
GOAROUND_WINDOW_S = 300  # a go-around's low point has a descent before it and a climb after it within this
GOAROUND_LOW_S = 90  # and is the lowest point this close either side
GOAROUND_MERGE_S = 180
GOAROUND_BACK_KM = 2.5  # "back over the same spot" for timing what a go-around added
GOAROUND_BACK_FT = 400
APPROACH_MS = 72  # about 140 knots on final approach
DIVERSION_DAYS = 60
DIVERSION_HISTORY = 2  # other days a flight number must have been seen to know where it usually goes
DIVERSION_OVERFLY_FT = 8000  # those days it stayed above this, so it plainly doesn't land here
DIVERSION_OVERFLY_DAYS = 3  # charter flight numbers wander, so landing somewhere new needs a longer record
DIVERSION_CLIMB_FT = 3000  # a flight that came down towards its airport and then climbed this much has left it
DIVERSION_RETURN_S = 600  # out and back to the same airport, longer than this and...
DIVERSION_RETURN_KM = 20  # ...farther than this, is a flight that turned back
DIVERSION_APPROACH_KM = 30  # diverted away: came this close to its usual airport...
DIVERSION_APPROACH_FT = 8000  # ...this low, then left
# Air ambulances fly a flight number wherever the patient is, so they never count as diverting
DIVERSION_EXCLUDED_PREFIXES = ("PHX",)
MAX_LIST = 200
# What the Types pages group by: the ICAO designator, or the logged type (usually the registry model) when there isn't one
TYPE_KEY = "COALESCE(actype, type)"
VIP_FIELDS = ("hex", "reg", "callsign", "type", "operator", "tags")
GARAGE_COLUMNS = ["hex", "reg", "type", "category", "callsign", "operator", "grp", "tags", "source", "ladd", "private",
                  "emergency", "go_around", "go_around_at", "first_seen", "last_seen", "alt_min", "alt_max",
                  "min_dist_km", "make", "model", "year", "aircraft_class", "registrant_type", "registrant",
                  "owner_state", "owner_country", "mlat", "icao_type"]
AIRLINE_CALLSIGN = re.compile(r"^([A-Z]{3})\d")
REPULL_VERSION = 1  # bump to fetch garage_pi's 30 days again and fill new columns on flights already stored
ROUTE_LOGIC_VERSION = 3  # bump to pick every stored flight's route leg again after changing pick_leg
EVENT_LOGIC_VERSION = 2  # bump to detect every stored track's takeoff and landing again after changing detect_events

# Routes: ADS-B has no origin or destination, so they come from Virtual Radar Server's community route list (CC0),
# one file per airline callsign prefix. Only prefixes heard in ROUTE_PREFIX_DAYS are fetched, each at most weekly.
ROUTE_URL = "https://raw.githubusercontent.com/vradarserver/standing-data/main/routes/schema-01/{letter}/{prefix}-all.csv"
ROUTE_REFRESH_SECONDS = 7 * 86400
ROUTE_PREFIX_DAYS = 30
ROUTE_FETCHES_PER_CYCLE = 30  # spreads a first fill over a few sync cycles
ROUTE_FETCH_PAUSE_SECONDS = 0.5
ROUTE_MAX_BYTES = 5 * 10**6
LOCAL_AIRPORT_KM = 30  # airports this close count as "here" when picking which leg of a multi-stop route we saw
ROUTE_PATTERN = re.compile(r"[A-Z0-9]{3,4}(-[A-Z0-9]{3,4})+")
ROUTE_CALLSIGN = re.compile(r"[A-Z]{3}[0-9][0-9A-Z]{0,4}")

# Business jets and turboprops, so NetJets-style operators with airline callsigns aren't counted as passenger airlines
BUSINESS_TYPES = ("C25A", "C25B", "C25C", "C525", "C510", "C56X", "C560", "C680", "C68A", "C700", "C750", "CL30", "CL35",
                  "CL60", "GLEX", "GL5T", "GL7T", "GLF4", "GLF5", "GLF6", "G280", "GALX", "F2TH", "F900", "FA7X", "FA8X",
                  "FA50", "E55P", "E50P", "E545", "E550", "E35L", "LJ35", "LJ45", "LJ60", "LJ75", "H25B", "BE40", "PRM1",
                  "PC12", "PC24", "B350", "BE20", "TBM7", "TBM9", "SF50", "S22T", "SR22")
# Flight type, most specific first
KIND_SQL = ("CASE WHEN cargo IS NOT NULL THEN 'cargo' WHEN grp IN ('mil', 'gov', 'pol') THEN 'government' "
            "WHEN private = 1 THEN 'private' WHEN actype IN (%s) THEN 'business' WHEN airline IS NOT NULL THEN 'passenger' "
            "ELSE 'other' END" % ", ".join("'%s'" % t for t in BUSINESS_TYPES))
KINDS = ("passenger", "cargo", "business", "private", "government", "other")

# Registry make and model to ICAO type designator, for aircraft SkyAware's database doesn't know (most Canadian ones).
# First match wins, so specific models come before their families.
TYPE_RULES = [(re.compile(make, re.I), re.compile(model, re.I), code) for make, model, code in [
    ("boeing", r"^737-7$", "B37M"), ("boeing", r"^737-8$|^737-8 ?MAX", "B38M"), ("boeing", r"^737-9$", "B39M"),
    ("boeing", r"^737-10$", "B3XM"), ("boeing", r"^737-3", "B733"), ("boeing", r"^737-4", "B734"), ("boeing", r"^737-5", "B735"),
    ("boeing", r"^737-6", "B736"), ("boeing", r"^737-7", "B737"), ("boeing", r"^737-8", "B738"), ("boeing", r"^737-9", "B739"),
    ("boeing", r"^717", "B712"), ("boeing", r"^747-8", "B748"), ("boeing", r"^747-4", "B744"), ("boeing", r"^757-2", "B752"),
    ("boeing", r"^757-3", "B753"), ("boeing", r"^767-2", "B762"), ("boeing", r"^767-3", "B763"), ("boeing", r"^767-4", "B764"),
    ("boeing", r"^777-F|^777-2\w*LR", "B77L"), ("boeing", r"^777-3\w*ER", "B77W"), ("boeing", r"^777-3", "B773"),
    ("boeing", r"^777-2", "B772"), ("boeing", r"^777-9", "B779"), ("boeing", r"^787-8", "B788"), ("boeing", r"^787-9", "B789"),
    ("boeing", r"^787-10", "B78X"),
    ("airbus", r"^BD-500-1A10", "BCS1"), ("airbus", r"^BD-500-1A11", "BCS3"), ("airbus", r"^A318", "A318"),
    ("airbus", r"^A319-\d+N", "A19N"), ("airbus", r"^A319", "A319"), ("airbus", r"^A320-\d+N", "A20N"), ("airbus", r"^A320", "A320"),
    ("airbus", r"^A321-\d+N", "A21N"), ("airbus", r"^A321", "A321"), ("airbus", r"^A330-2", "A332"), ("airbus", r"^A330-3", "A333"),
    ("airbus", r"^A330-9", "A339"), ("airbus", r"^A340-3", "A343"), ("airbus", r"^A340-6", "A346"), ("airbus", r"^A350-9", "A359"),
    ("airbus", r"^A350-10", "A35K"), ("airbus", r"^A380", "A388"),
    ("bombardier|de ?havilland|dhc", r"^DHC-8-4", "DH8D"), ("bombardier|de ?havilland|dhc", r"^DHC-8-3", "DH8C"),
    ("bombardier|de ?havilland|dhc", r"^DHC-8-2", "DH8B"), ("bombardier|de ?havilland|dhc", r"^DHC-8-1", "DH8A"),
    ("bombardier|canadair", r"^CL-600-2B19", "CRJ2"), ("bombardier|canadair", r"^CL-600-2C10", "CRJ7"),
    ("bombardier|canadair", r"^CL-600-2D24", "CRJ9"), ("bombardier|canadair", r"^CL-600-2E25", "CRJX"),
    ("bombardier|canadair", r"^CL-600-2B16", "CL60"), ("bombardier", r"^BD-100-1A10", "CL30"), ("bombardier", r"^BD-700-1A10", "GLEX"),
    ("bombardier", r"^BD-700-1A11", "GL5T"), ("bombardier", r"^BD-700-2A12", "GL7T"), ("bombardier|airbus", r"^BD-500-1A10", "BCS1"),
    ("bombardier|airbus", r"^BD-500-1A11", "BCS3"),
    ("embraer", r"^ERJ 170-100", "E170"), ("embraer", r"^ERJ 170-200", "E75L"), ("embraer", r"^ERJ 190-100", "E190"),
    ("embraer", r"^ERJ 190-200", "E195"), ("embraer", r"^ERJ 190-300", "E290"), ("embraer", r"^ERJ 190-400", "E295"),
    ("embraer", r"^EMB-145", "E145"), ("embraer", r"^EMB-135", "E135"), ("embraer", r"^EMB-505", "E55P"),
    ("embraer", r"^EMB-500", "E50P"), ("embraer", r"^EMB-545", "E545"), ("embraer", r"^EMB-550", "E550"),
    ("cessna|textron", r"^525B$", "C25B"), ("cessna|textron", r"^525A$", "C25A"), ("cessna|textron", r"^525C$", "C25C"),
    ("cessna|textron", r"^525$", "C525"), ("cessna|textron", r"^680A$", "C68A"), ("cessna|textron", r"^680$", "C680"),
    ("cessna|textron", r"^700$", "C700"), ("cessna|textron", r"^750$", "C750"), ("cessna|textron", r"^560XL", "C56X"),
    ("cessna|textron", r"^560$", "C560"), ("learjet", r"^45", "LJ45"), ("learjet", r"^75", "LJ75"), ("pilatus", r"^PC-12", "PC12"),
    ("pilatus", r"^PC-24", "PC24"), ("beech|raytheon", r"^1900", "B190"), ("gulfstream", r"GVI|G650", "GLF6"),
    ("gulfstream", r"GIV|G450", "GLF4"), ("gulfstream", r"^GV|G550", "GLF5"), ("gulfstream|iai", r"G280", "G280"),
    ("dassault", r"FALCON 2000", "F2TH"), ("dassault", r"FALCON 900", "F900"), ("dassault", r"FALCON 7X", "FA7X"),
    ("atr|aerospatiale", r"^ATR 72", "AT76"), ("boeing", r"^777F", "B77L"), ("bombardier|canadair", r"^CL-600-2C11", "CRJ7"),
    ("c series", r"^BD-500-1A10", "BCS1"), ("c series", r"^BD-500-1A11", "BCS3"), ("cessna|textron", r"^208B", "C208"),
    ("fairchild|swearingen", r"^SA227", "SW4"), ("beech|raytheon|textron", r"^B300", "B350"), ("daher|socata", r"^TBM 7", "TBM7"),
    ("daher|socata", r"^TBM 9", "TBM9"), ("agusta|leonardo", r"AW139", "A139"), ("cirrus", r"^SR22T", "S22T"), ("cirrus", r"^SR22", "SR22"),
]]


TYPE_LOGIC_VERSION = 2  # part of the reclassify signature; bump when aircraft_type changes


def aircraft_type(row, names):
    """ICAO type designator: from the registry model, else SkyAware's, else a designator already in "type". The registry
    goes first because SkyAware's hex database is stale for many Canadian aircraft (a Q400 listed as a 757, for one)."""
    make, model = row.get("make") or "", (row.get("model") or "").strip()
    for make_pattern, model_pattern, code in TYPE_RULES:
        if make_pattern.search(make) and model_pattern.search(model):
            return code
    if row.get("icao_type"):
        return row["icao_type"]
    raw = (row.get("type") or "").strip().upper()
    return raw if raw in names else None


def load_cargo_rules(path):
    rules = json.loads(Path(path).read_text())
    rules["owners"] = [(re.compile(pattern, re.I), name) for pattern, name in rules["owners"]]
    rules["freighter_makes"] = re.compile(rules["freighter_makes"], re.I)
    rules["freighter_models"] = re.compile(rules["freighter_models"], re.I)
    return rules


def load_vip_rules(path):
    rules = []
    for rule in json.loads(Path(path).read_text())["rules"]:
        fields = {k: re.compile(v, re.I) for k, v in rule.items() if k in VIP_FIELDS}
        rules.append((fields, rule["label"]))
    return rules


def vip_label(row, rules):
    """Who a VIP aircraft is used for, from vip.json, or None."""
    values = {k: row.get(k) or "" for k in VIP_FIELDS}
    values["reg"] = re.sub(r"[^A-Z0-9]", "", values["reg"].upper())
    values["type"] = " ".join(filter(None, [row.get("type"), row.get("icao_type"), row.get("actype"), row.get("model")]))
    for fields, label in rules:
        if fields and all(pattern.search(values[k]) for k, pattern in fields.items()):
            return label
    return None


def cargo_carrier(row, rules):
    """Same rules as garage_pi's aircraft log: callsign prefix, then registered owner, then a freighter model."""
    callsign = row.get("callsign") or ""
    if AIRLINE_CALLSIGN.match(callsign) and callsign[:3] in rules["callsigns"]:
        return rules["callsigns"][callsign[:3]]
    owner = " ".join(filter(None, (row.get("registrant"), row.get("operator"))))
    for pattern, name in rules["owners"]:
        if owner and pattern.search(owner):
            return name or owner
    if rules["freighter_makes"].search(row.get("make") or "") and rules["freighter_models"].search(row.get("model") or ""):
        return "Freighter" + (" (%s)" % row["registrant"] if row.get("registrant") else "")
    return None
stop_requested = False


def on_signal(_signum, _frame):
    global stop_requested
    stop_requested = True


def log(*parts):
    print(time.strftime("%F %T"), *parts, flush=True)


def to_km(lat, lon):
    return ((lon - HOME[1]) * 111.32 * math.cos(math.radians(HOME[0])), (lat - HOME[0]) * 110.574)


def distance_km(lat1, lon1, lat2, lon2):
    x = (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(x, (lat2 - lat1) * 110.574)


def great_circle_km(lat1, lon1, lat2, lon2):
    """Distance over the globe, for legs too long for distance_km's flat approximation."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(min(1, a)))


def route_airports(db, callsign):
    """The routes row for a callsign. Some aircraft send the flight number with leading zeros (CPA075) where the
    route list has CPA75."""
    if not callsign:
        return None
    row = db.execute("SELECT airports FROM routes WHERE callsign = ?", (callsign,)).fetchone()
    trimmed = re.sub(r"^([A-Z]{3})0+(?=\d)", r"\1", callsign)
    if row is None and trimmed != callsign:
        row = db.execute("SELECT airports FROM routes WHERE callsign = ?", (trimmed,)).fetchone()
    return row


def bearing(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing in degrees."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def closest_approach(points):
    """Closest the track came to the house, along each segment rather than only at the samples: (km, alt, t)."""
    best = None
    for i, (t, lat, lon, alt, _gs) in enumerate(points):
        x, y = to_km(lat, lon)
        candidate = (math.hypot(x, y), alt, t)
        if i:
            t0, lat0, lon0, alt0, _ = points[i - 1]
            if t - t0 <= SEGMENT_GAP_SECONDS:
                x0, y0 = to_km(lat0, lon0)
                dx, dy = x - x0, y - y0
                length = dx * dx + dy * dy
                u = min(1.0, max(0.0, -(x0 * dx + y0 * dy) / length)) if length else 0.0
                alt_u = alt0 + (alt - alt0) * u if alt is not None and alt0 is not None else (alt if u >= 0.5 else alt0)
                inner = (math.hypot(x0 + u * dx, y0 + u * dy), alt_u, int(t0 + (t - t0) * u))
                if inner[0] < candidate[0]:
                    candidate = inner
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        return None, None, None
    return round(best[0], 2), None if best[1] is None else int(best[1]), best[2]


class Airports:
    def __init__(self, path):
        rows = json.loads(Path(path).read_text())
        self.names = {r[0]: r[1] for r in rows}
        self.pos = {r[0]: (r[2], r[3]) for r in rows}
        self.near = [r for r in rows if distance_km(HOME[0], HOME[1], r[2], r[3]) <= AIRPORT_RADIUS_KM]

    def at(self, lat, lon):
        best, best_km = None, EVENT_AIRPORT_KM
        for airport in self.near:
            if abs(airport[2] - lat) < 0.1:
                km = distance_km(lat, lon, airport[2], airport[3])
                if km <= best_km:
                    best, best_km = airport, km
        return best


def detect_events(points, airports):
    """[(kind, airport ident, t)] from one track."""
    known = [p for p in points if p[3] is not None]
    if len(known) < 3:
        return []
    events = []

    def low_near(point, limit=EVENT_LOW_FT):
        airport = airports.at(point[1], point[2])
        return airport if airport and point[3] - airport[4] <= limit else None

    start = [p for p in known if p[0] - known[0][0] <= EVENT_EDGE_SECONDS]
    for i, point in enumerate(start):
        airport = low_near(point, EVENT_TAKEOFF_LOW_FT)
        if airport and max(p[3] for p in known[i:]) - point[3] >= EVENT_CLIMB_FT:
            events.append(("takeoff", airport[0], point[0]))
            break
    end_from = next(i for i, p in enumerate(known) if known[-1][0] - p[0] <= EVENT_EDGE_SECONDS)
    for i in range(len(known) - 1, end_from - 1, -1):
        point = known[i]
        airport = low_near(point)
        if airport and max(p[3] for p in known[:i + 1]) - point[3] >= EVENT_CLIMB_FT:
            if not (events and events[0][2] == point[0]):
                events.append(("landing", airport[0], point[0]))
            break
    return events


def find_go_arounds(points, airports, events):
    """Every go-around in a track, with how much time it added: [{t, airport, lat, lon, alt, agl, back_at, added_s, how}].
    A go-around is a low point near an airport, reached descending, followed by a climb. The time it added is how long
    until the aircraft was back over the same spot at the same height; failing that, the landing time less a normal
    approach from that spot; failing that, at least the time until the track ends."""
    known = [p for p in points if p[3] is not None]
    found = []
    for i, (t, lat, lon, alt) in enumerate(known):
        airport = airports.at(lat, lon)
        if not airport or alt - airport[4] > EVENT_LOW_FT:
            continue
        before = [p[3] for p in known[:i] if t - p[0] <= GOAROUND_WINDOW_S]
        after = [p[3] for p in known[i + 1:] if p[0] - t <= GOAROUND_WINDOW_S]
        nearby = [p[3] for p in known if abs(p[0] - t) <= GOAROUND_LOW_S]
        if (before and after and max(before) - alt >= EVENT_CLIMB_FT and max(after) - alt >= EVENT_CLIMB_FT
                and alt <= min(nearby)):
            if found and t - found[-1]["t"] <= GOAROUND_MERGE_S:  # the same go-around: keep its lowest point
                if alt >= found[-1]["alt"]:
                    continue
                found.pop()
            found.append({"t": t, "airport": airport[0], "lat": lat, "lon": lon, "alt": alt, "agl": alt - airport[4]})
    landings = [(a, at) for kind, a, at in events if kind == "landing"]
    last = known[-1][0] if known else None
    for g in found:
        back = next((p for p in known if p[0] > g["t"] + GOAROUND_LOW_S and p[3] <= g["alt"] + GOAROUND_BACK_FT
                     and distance_km(g["lat"], g["lon"], p[1], p[2]) <= GOAROUND_BACK_KM), None)
        landed = next((at for a, at in landings if a == g["airport"] and at > g["t"]), None)
        if back:
            g.update(back_at=back[0], added_s=back[0] - g["t"], how="same point")
        elif landed:
            # a normal approach would have touched down about this soon after passing the go-around point
            to_runway = distance_km(g["lat"], g["lon"], *airports.pos[g["airport"]]) * 1000 / APPROACH_MS
            g.update(back_at=landed, added_s=max(0, round(landed - g["t"] - to_runway)), how="from the landing")
        elif any(a != g["airport"] for a, _ in landings):
            g.update(back_at=None, added_s=None, how="landed elsewhere")
        else:
            g.update(back_at=None, added_s=last - g["t"] if last else None, how="at least")
    return found


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    fresh = not DB_PATH.exists()
    db = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    if fresh:
        db.execute("PRAGMA auto_vacuum = INCREMENTAL")  # lets pruning hand space back without a full VACUUM
    db.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;
        CREATE TABLE IF NOT EXISTS flights (
            id INTEGER PRIMARY KEY, hex TEXT NOT NULL, reg TEXT, type TEXT, category TEXT, callsign TEXT, airline TEXT,
            operator TEXT, grp TEXT, tags TEXT, source TEXT, ladd INTEGER, private INTEGER, emergency TEXT,
            go_around TEXT, go_around_at INTEGER, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
            alt_min INTEGER, alt_max INTEGER, min_dist_km REAL, make TEXT, model TEXT, year TEXT, aircraft_class TEXT,
            registrant_type TEXT, registrant TEXT, owner_state TEXT, owner_country TEXT, mlat INTEGER,
            closest_km REAL, closest_alt INTEGER, closest_at INTEGER, points INTEGER NOT NULL DEFAULT 0,
            interesting INTEGER NOT NULL DEFAULT 0, UNIQUE (hex, first_seen));
        CREATE TABLE IF NOT EXISTS events (
            flight_id INTEGER NOT NULL, kind TEXT NOT NULL, airport TEXT NOT NULL, t INTEGER NOT NULL,
            PRIMARY KEY (flight_id, kind));
        CREATE TABLE IF NOT EXISTS track_points (
            flight_id INTEGER NOT NULL, t INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, alt INTEGER, gs REAL,
            PRIMARY KEY (flight_id, t)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    existing = {row[1] for row in db.execute("PRAGMA table_info(flights)")}
    for column in ("icao_type", "actype", "cargo", "route", "origin", "destination", "vip"):
        if column not in existing:
            db.execute("ALTER TABLE flights ADD COLUMN %s TEXT" % column)
    if "route_checked" not in existing:
        db.execute("ALTER TABLE flights ADD COLUMN route_checked INTEGER NOT NULL DEFAULT 0")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS routes (callsign TEXT PRIMARY KEY, airports TEXT NOT NULL, prefix TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS routes_prefix ON routes (prefix);
        CREATE INDEX IF NOT EXISTS flights_destination ON flights (destination, first_seen);
        CREATE INDEX IF NOT EXISTS flights_origin ON flights (origin, first_seen);
        CREATE INDEX IF NOT EXISTS flights_route_unchecked ON flights (id) WHERE route_checked = 0;
        CREATE INDEX IF NOT EXISTS flights_actype ON flights (actype, first_seen);
        CREATE INDEX IF NOT EXISTS flights_cargo ON flights (first_seen) WHERE cargo IS NOT NULL;
        CREATE INDEX IF NOT EXISTS flights_first_seen ON flights (first_seen);
        CREATE INDEX IF NOT EXISTS flights_reg ON flights (reg);
        CREATE INDEX IF NOT EXISTS flights_airline ON flights (airline, first_seen);
        CREATE INDEX IF NOT EXISTS flights_closest ON flights (closest_km, first_seen);
        CREATE INDEX IF NOT EXISTS flights_interesting ON flights (first_seen) WHERE interesting = 1;
        CREATE INDEX IF NOT EXISTS flights_notable ON flights (first_seen) WHERE emergency IS NOT NULL OR go_around IS NOT NULL;
        CREATE INDEX IF NOT EXISTS events_airport ON events (airport, t);
        CREATE INDEX IF NOT EXISTS events_t ON events (t);
    """)
    return db


class Store:
    def __init__(self):
        self.db = connect()
        self.lock = threading.Lock()
        self.airports = Airports(APP / "airports.json")
        self.airlines = json.loads((APP / "airlines.json").read_text())
        self.types = json.loads((APP / "types.json").read_text())
        self.iata = json.loads((APP / "iata.json").read_text())
        self.cargo_rules = load_cargo_rules(APP / "cargo.json")
        self.vip_rules = load_vip_rules(APP / "vip.json")
        self.diversions = {}  # flight id -> {kind, airport, t, usual, detail}, rebuilt after each sync
        self.local = {a[0] for a in self.airports.near if distance_km(HOME[0], HOME[1], a[2], a[3]) <= LOCAL_AIRPORT_KM}
        self.status = {"last_sync": None, "last_error": None, "added": 0}
        self.reclassify()
        if self.meta("route_logic", 0) < ROUTE_LOGIC_VERSION:
            with self.db:
                self.db.execute("UPDATE flights SET route_checked = 0")
                self.set_meta("route_logic", ROUTE_LOGIC_VERSION)
        if self.meta("event_logic", 1) < EVENT_LOGIC_VERSION:
            self.redetect_events()
        if self.meta("repull", 0) < REPULL_VERSION:
            with self.db:
                self.set_meta("cursor", [0, 0])
                self.set_meta("repull", REPULL_VERSION)

    def redetect_events(self):
        """Takeoffs and landings again for every flight that still has its track; route legs are picked again after."""
        ids = [r[0] for r in self.db.execute("SELECT id FROM flights WHERE points > 0")]
        with self.db:
            for flight_id in ids:
                points = self.db.execute("SELECT t, lat, lon, alt, gs FROM track_points WHERE flight_id = ? ORDER BY t",
                                         (flight_id,)).fetchall()
                if not points:
                    continue
                self.db.execute("DELETE FROM events WHERE flight_id = ?", (flight_id,))
                self.db.executemany("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)",
                                    [(flight_id, kind, airport, t) for kind, airport, t in detect_events(points, self.airports)])
            self.db.execute("UPDATE flights SET route_checked = 0")
            self.set_meta("event_logic", EVENT_LOGIC_VERSION)
        log("detected takeoffs and landings again for", len(ids), "flights")

    def reclassify(self):
        """Recomputes cargo and aircraft type for every flight when the rules have changed since the last start."""
        signature = (APP / "cargo.json").read_text() + repr([(m.pattern, p.pattern, c) for m, p, c in TYPE_RULES]) + \
            (APP / "types.json").read_text() + str(TYPE_LOGIC_VERSION) + (APP / "vip.json").read_text()
        digest = __import__("hashlib").sha256(signature.encode()).hexdigest()
        if self.meta("rules") == digest:
            return
        columns = ["id", "hex", "reg", "callsign", "registrant", "operator", "make", "model", "type", "icao_type", "tags"]
        with self.db:
            for row in self.db.execute("SELECT %s FROM flights" % ", ".join(columns)).fetchall():
                flight = dict(zip(columns, row))
                flight["actype"] = aircraft_type(flight, self.types)
                vip = vip_label(flight, self.vip_rules)
                self.db.execute("UPDATE flights SET cargo = ?, actype = ?, vip = ?, interesting = MAX(interesting, ?) WHERE id = ?",
                                (cargo_carrier(flight, self.cargo_rules), flight["actype"], vip, int(bool(vip)), flight["id"]))
            self.set_meta("rules", digest)
        log("reclassified cargo, aircraft types and VIP aircraft")

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    def add(self, visit):
        points = [tuple(p) for p in visit.get("points", [])]
        row = [visit.get(c) for c in GARAGE_COLUMNS]
        callsign = visit.get("callsign") or ""
        match = AIRLINE_CALLSIGN.match(callsign)
        airline = match.group(1) if match and not visit.get("grp") and not visit.get("private") else None
        km, alt, at = closest_approach(points) if points else (visit.get("min_dist_km"), None, visit.get("closest_at"))
        cargo, actype = cargo_carrier(visit, self.cargo_rules), aircraft_type(visit, self.types)
        vip = vip_label(dict(visit, actype=actype), self.vip_rules)
        interesting = int(bool(visit.get("grp") or visit.get("ladd") or visit.get("emergency") or visit.get("go_around")
                               or visit.get("private") or vip))
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO flights (%s, airline, closest_km, closest_alt, closest_at, points, interesting, cargo, actype, vip) "
            "VALUES (%s)" % (", ".join(GARAGE_COLUMNS), ", ".join("?" * (len(GARAGE_COLUMNS) + 9))),
            row + [airline, km, alt, at, len(points), interesting, cargo, actype, vip])
        if not cursor.rowcount:
            # Already stored: a re-pull only fills in what newer exports carry
            self.db.execute("UPDATE flights SET icao_type = COALESCE(icao_type, ?), actype = ? WHERE hex = ? AND first_seen = ?",
                            (visit.get("icao_type"), actype, visit["hex"], visit["first_seen"]))
            return False
        flight_id = cursor.lastrowid
        self.db.executemany("INSERT OR IGNORE INTO track_points VALUES (?, ?, ?, ?, ?, ?)",
                            [(flight_id,) + p for p in points])
        self.db.executemany("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)",
                            [(flight_id, kind, airport, t) for kind, airport, t in detect_events(points, self.airports)])
        return True

    def sync(self):
        after, after_id = self.meta("cursor", [0, 0])
        added = 0
        while not stop_requested:
            url = "%s?%s" % (EXPORT_URL, urllib.parse.urlencode({"after": after, "after_id": after_id, "limit": BATCH}))
            with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
                visits = json.loads(response.read().decode())["visits"]
            with self.lock, self.db:
                for visit in visits:
                    added += self.add(visit)
                if visits:
                    after, after_id = visits[-1]["last_seen"], visits[-1]["id"]
                    self.set_meta("cursor", [after, after_id])
            if len(visits) < BATCH:
                break
            time.sleep(BATCH_PAUSE_SECONDS)
        return added

    def refresh_routes(self):
        """Fetches route files for airline prefixes heard recently that are missing or over a week old."""
        fetched = self.meta("route_fetched", {})
        now = int(time.time())
        prefixes = [r[0] for r in self.db.execute(
            "SELECT DISTINCT substr(callsign, 1, 3) FROM flights WHERE first_seen >= ? AND callsign GLOB '[A-Z][A-Z][A-Z][0-9]*'",
            (now - ROUTE_PREFIX_DAYS * 86400,))]
        due = sorted((p for p in prefixes if now - fetched.get(p, 0) >= ROUTE_REFRESH_SECONDS), key=lambda p: fetched.get(p, 0))
        for prefix in due[:ROUTE_FETCHES_PER_CYCLE]:
            if stop_requested:
                break
            rows = []
            try:
                with urllib.request.urlopen(ROUTE_URL.format(letter=prefix[0], prefix=prefix), timeout=FETCH_TIMEOUT_SECONDS) as reply:
                    text = reply.read(ROUTE_MAX_BYTES).decode("utf-8-sig", errors="replace")
                for line in text.splitlines()[1:]:
                    fields = line.split(",")
                    if len(fields) >= 5 and ROUTE_CALLSIGN.fullmatch(fields[0]) and fields[0].startswith(prefix) \
                            and ROUTE_PATTERN.fullmatch(fields[4].strip()):
                        rows.append((fields[0], fields[4].strip(), prefix))
            except urllib.error.HTTPError as error:
                if error.code != 404:  # 404 just means nobody has listed routes for this airline
                    log("route fetch failed for", prefix, error)
                    continue
            except (OSError, ValueError) as error:
                log("route fetch failed for", prefix, error)
                continue
            with self.lock, self.db:
                self.db.execute("DELETE FROM routes WHERE prefix = ?", (prefix,))
                self.db.executemany("INSERT OR REPLACE INTO routes VALUES (?, ?, ?)", rows)
                # Flights that had no route can now be looked up again
                self.db.execute("UPDATE flights SET route_checked = 0 WHERE route IS NULL AND substr(callsign, 1, 3) = ?", (prefix,))
                fetched[prefix] = now
                self.set_meta("route_fetched", fetched)
            time.sleep(ROUTE_FETCH_PAUSE_SECONDS)

    def pick_leg(self, airports, events, descending, ends=None):
        """(origin, destination) for the leg we saw, or None when what we saw contradicts the listed route.
        descending is True or False from the track's altitude, or None when unknown; ends is the track's first and
        last (lat, lon), or None."""
        legs = list(zip(airports, airports[1:]))
        took_off = next((a for k, a, _ in events if k == "takeoff"), None)
        landed = next((a for k, a, _ in events if k == "landing"), None)
        for origin, destination in legs:
            if (took_off and origin == took_off) or (landed and destination == landed):
                return origin, destination
        if took_off or landed:
            return None  # seen at an airport the route doesn't fly from or to: the listing is probably out of date
        if len(legs) == 1:
            return legs[0]
        # A multi-stop route through a local airport: arriving if the track came down, leaving if it went up
        local_legs = [(o, d) for o, d in legs if o in self.local or d in self.local]
        if local_legs and descending is not None:
            for origin, destination in local_legs:
                if (destination in self.local) == descending:
                    return origin, destination
        # Otherwise the leg whose origin is nearest where the track started and destination nearest where it ended,
        # which also gets the direction right on a round trip like KBOS-KDEN-KBOS
        pos = self.airports.pos
        candidates = [leg for leg in (local_legs or legs) if leg[0] in pos and leg[1] in pos]
        if ends and candidates:
            return min(candidates, key=lambda leg: distance_km(*ends[0], *pos[leg[0]]) + distance_km(*ends[1], *pos[leg[1]]))
        return local_legs[0] if local_legs else None

    def live_leg(self, airports, lat, lon, track, descending):
        """(origin, destination) of a route for an aircraft in the air now, from where it is and where it's heading."""
        legs = list(zip(airports, airports[1:]))
        local_legs = [(o, d) for o, d in legs if o in self.local or d in self.local]
        if local_legs and descending is not None:
            for origin, destination in local_legs:
                if (destination in self.local) == descending:
                    return origin, destination
        pos = self.airports.pos
        if lat is None:
            return legs[0] if len(legs) == 1 else (local_legs[0] if local_legs else None)

        def detour(leg):
            # Extra distance flying via where the aircraft is now, or None if it's off the leg or heading away
            if leg[0] not in pos or leg[1] not in pos:
                return None
            if track is not None and abs((bearing(lat, lon, *pos[leg[1]]) - track + 180) % 360 - 180) > 90:
                return None
            length = great_circle_km(*pos[leg[0]], *pos[leg[1]])
            extra = great_circle_km(lat, lon, *pos[leg[0]]) + great_circle_km(lat, lon, *pos[leg[1]]) - length
            return extra if extra <= 150 + 0.25 * length else None
        # Routes from the list can be out of date, so a leg the aircraft plainly isn't flying is no answer
        plausible = [(extra, leg) for leg in legs for extra in [detour(leg)] if extra is not None]
        return min(plausible)[1] if plausible else None

    def track_ends(self, flight_id):
        ends = self.db.execute("""SELECT (SELECT lat || ',' || lon FROM track_points WHERE flight_id = ?1 ORDER BY t LIMIT 1),
                                         (SELECT lat || ',' || lon FROM track_points WHERE flight_id = ?1 ORDER BY t DESC LIMIT 1)""",
                               (flight_id,)).fetchone()
        if ends[0] is None or ends[0] == ends[1]:
            return None
        return [tuple(map(float, e.split(","))) for e in ends]

    def descending(self, flight_id):
        ends = self.db.execute("""SELECT (SELECT alt FROM track_points WHERE flight_id = ?1 AND alt IS NOT NULL ORDER BY t LIMIT 1),
                                         (SELECT alt FROM track_points WHERE flight_id = ?1 AND alt IS NOT NULL ORDER BY t DESC LIMIT 1)""",
                               (flight_id,)).fetchone()
        if ends[0] is None or ends[1] is None or abs(ends[1] - ends[0]) < EVENT_CLIMB_FT:
            return None
        return ends[1] < ends[0]

    def assign_routes(self):
        with self.lock, self.db:
            flights = self.db.execute("SELECT id, callsign FROM flights WHERE route_checked = 0").fetchall()
            for flight_id, callsign in flights:
                row = route_airports(self.db, callsign)
                leg = None
                if row:
                    events = self.db.execute("SELECT kind, airport, t FROM events WHERE flight_id = ?", (flight_id,)).fetchall()
                    airports = row[0].split("-")
                    multi = len(airports) > 2
                    leg = self.pick_leg(airports, events, self.descending(flight_id) if multi else None,
                                        self.track_ends(flight_id) if multi else None)
                self.db.execute("UPDATE flights SET route = ?, origin = ?, destination = ?, route_checked = 1 WHERE id = ?",
                                (row[0] if leg else None, leg[0] if leg else None, leg[1] if leg else None, flight_id))
        return len(flights)

    def refresh_diversions(self):
        with self.lock:
            self.diversions = self.find_diversions()

    def find_diversions(self):
        """Flights that went somewhere other than where they normally go, judged from what was seen here, not from the
        route list (whose listings are often out of date). Three kinds:
          returned: took off and landed back at the same airport after going some way out
          here: landed at an airport where this flight number, seen on other days, only ever flew over high
          away: a flight number that usually lands at an airport came down towards it, then left without landing"""
        since = int(time.time()) - DIVERSION_DAYS * 86400
        flights = [r for r in self.db.execute("""SELECT id, callsign, first_seen, last_seen, alt_min FROM flights
                                                 WHERE airline IS NOT NULL AND callsign IS NOT NULL AND first_seen >= ?""", (since,))
                   if not r[1].startswith(DIVERSION_EXCLUDED_PREFIXES)]
        events = collections.defaultdict(list)
        for flight_id, kind, airport, t in self.db.execute(
                "SELECT e.flight_id, e.kind, e.airport, e.t FROM events e JOIN flights f ON f.id = e.flight_id "
                "WHERE f.first_seen >= ? AND f.airline IS NOT NULL", (since,)):
            events[flight_id].append((kind, airport, t))
        by_callsign = collections.defaultdict(list)
        for row in flights:
            by_callsign[row[1]].append(row)
        day = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))
        found = {}
        for flight_id, callsign, first, last, alt_min in flights:
            mine = events.get(flight_id, [])
            took_off = next((a for k, a, _ in mine if k == "takeoff"), None)
            landing = next(((a, t) for k, a, t in mine if k == "landing"), None)
            others = [r for r in by_callsign[callsign] if r[0] != flight_id and day(r[2]) != day(first)]
            other_days = {day(r[2]) for r in others}
            if took_off and landing and landing[0] == took_off and landing[1] - first >= DIVERSION_RETURN_S:
                farthest = self.farthest_km(flight_id, *self.airports.pos[took_off])
                if farthest >= DIVERSION_RETURN_KM:
                    found[flight_id] = {"kind": "returned", "airport": took_off, "t": landing[1], "usual": None,
                                        "detail": round(farthest)}
                    continue
            if landing and len(other_days) >= DIVERSION_OVERFLY_DAYS:
                landed_there = any(k == "landing" and a == landing[0] for r in others for k, a, _ in events.get(r[0], []))
                overflew = [r for r in others if (r[4] or 0) >= DIVERSION_OVERFLY_FT]
                if not landed_there and len({day(r[2]) for r in overflew}) >= DIVERSION_OVERFLY_DAYS:
                    found[flight_id] = {"kind": "here", "airport": landing[0], "t": landing[1], "usual": self.listed_destination(callsign),
                                        "detail": len(other_days)}
                    continue
            usual = collections.Counter(a for r in others for k, a, _ in events.get(r[0], []) if k == "landing")
            if usual and alt_min is not None and alt_min <= DIVERSION_APPROACH_FT:
                home_airport, count = usual.most_common(1)[0]
                days_there = {day(r[2]) for r in others if any(k == "landing" and a == home_airport for k, a, _ in events.get(r[0], []))}
                if (len(days_there) >= DIVERSION_HISTORY and not any(a == home_airport for _, a, _ in mine)
                        and took_off is None):  # some flight numbers are used both ways; a departure isn't a diversion
                    # A track that just stops on approach is usually a landing out of range, not a diversion, so it
                    # needs to be seen leaving: landing somewhere else, or climbing back up and heading away
                    near, climbed, end_km = self.left_approach(flight_id, *self.airports.pos[home_airport])
                    left = (landing and landing[0] != home_airport) or (climbed >= DIVERSION_CLIMB_FT and end_km > DIVERSION_APPROACH_KM)
                    if near is not None and near <= DIVERSION_APPROACH_FT and left:
                        found[flight_id] = {"kind": "away", "airport": landing[0] if landing else None,
                                            "t": landing[1] if landing else last, "usual": home_airport, "detail": near}
        return found

    def farthest_km(self, flight_id, lat, lon):
        points = self.db.execute("SELECT lat, lon FROM track_points WHERE flight_id = ?", (flight_id,)).fetchall()
        return max((distance_km(lat, lon, a, b) for a, b in points), default=0)

    def left_approach(self, flight_id, lat, lon):
        """(lowest altitude within DIVERSION_APPROACH_KM of a point or None, feet climbed after it, km from the point
        at the end of the track)."""
        points = self.db.execute("SELECT lat, lon, alt FROM track_points WHERE flight_id = ? AND alt IS NOT NULL ORDER BY t",
                                 (flight_id,)).fetchall()
        near = [i for i, (a, b, _) in enumerate(points) if distance_km(lat, lon, a, b) <= DIVERSION_APPROACH_KM]
        if not near:
            return None, 0, 0
        low = min(near, key=lambda i: points[i][2])
        if max(p[2] for p in points[:low + 1]) - points[low][2] < DIVERSION_CLIMB_FT:
            return None, 0, 0  # not reached by coming down, so not an approach
        climbed = max(p[2] for p in points[low:]) - points[low][2]
        return points[low][2], climbed, distance_km(lat, lon, points[-1][0], points[-1][1])

    def listed_destination(self, callsign):
        row = route_airports(self.db, callsign)
        return row[0].split("-")[-1] if row else None

    def size(self):
        return sum(os.path.getsize(p) for p in (DB_PATH, Path(str(DB_PATH) + "-wal")) if p.exists())

    def prune(self):
        """Over the cap, drops the oldest tracks first, then the oldest flights. Interesting flights always stay."""
        if self.size() <= MAX_DB_BYTES:
            return
        with self.lock:
            while self.size() > PRUNE_TO_BYTES and not stop_requested:
                before = self.size()
                with self.db:
                    ids = [r[0] for r in self.db.execute("SELECT id FROM flights WHERE interesting = 0 AND points > 0 "
                                                         "ORDER BY first_seen LIMIT ?", (PRUNE_CHUNK,))]
                    if ids:
                        marks = ",".join("?" * len(ids))
                        self.db.execute("DELETE FROM track_points WHERE flight_id IN (%s)" % marks, ids)
                        self.db.execute("UPDATE flights SET points = 0 WHERE id IN (%s)" % marks, ids)
                    else:
                        ids = [r[0] for r in self.db.execute("SELECT id FROM flights WHERE interesting = 0 "
                                                             "ORDER BY first_seen LIMIT ?", (PRUNE_CHUNK,))]
                        if not ids:
                            log("over the size cap with only interesting flights left")
                            return
                        marks = ",".join("?" * len(ids))
                        self.db.execute("DELETE FROM events WHERE flight_id IN (%s)" % marks, ids)
                        self.db.execute("DELETE FROM flights WHERE id IN (%s)" % marks, ids)
                self.db.execute("PRAGMA incremental_vacuum")
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if self.size() >= before:
                    log("prune stalled at", before, "bytes; stopping")
                    break
            log("pruned to", self.size(), "bytes")

    def write_status(self):
        self.status.update(bytes=self.size(), cap_bytes=MAX_DB_BYTES, generated=int(time.time()))
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.status))
        tmp.replace(STATUS_PATH)

    def run(self):
        while not stop_requested:
            started = time.time()
            try:
                added = self.sync()
                self.status.update(last_sync=int(time.time()), last_error=None, added=added)
                if added:
                    log("added", added, "flights")
                self.refresh_routes()
                self.assign_routes()
                self.prune()
                self.refresh_diversions()
            except Exception as error:  # whatever it was, the next sync tries again
                self.status["last_error"] = str(error)[:200]
                log("sync failed:", error)
            try:
                self.write_status()
            except OSError as error:
                log("status write failed:", error)
            while not stop_requested and time.time() - started < SYNC_SECONDS:
                time.sleep(1)


class Api(http.server.BaseHTTPRequestHandler):
    store = None

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

    def rows(self, sql, params=()):
        cursor = self.db.execute(sql, params)
        names = [d[0] for d in cursor.description]
        rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        for row in rows:
            if "actype" in row:
                row["type_name"] = self.store.types.get(row["actype"]) if row["actype"] else None
            for end in ("origin", "destination"):
                if row.get(end):
                    row[end + "_name"] = self.airport_name(row[end])
        return rows

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        name = url.path[len(API_PREFIX) + 1:] if url.path.startswith(API_PREFIX + "/") else ""
        handler = getattr(self, "api_" + name, None) if re.fullmatch(r"[a-z]+", name) else None
        if handler is None:
            return self.respond(404, {"error": "not found"})
        days = query.get("days", "30")
        days = int(days) if days.isdigit() else 30
        self.since = int(time.time()) - days * 86400 if days else 0
        radius = query.get("r", "1")
        self.radius = float(radius) if re.fullmatch(r"\d+(\.\d+)?", radius) and 0 < float(radius) <= 50 else 1.0
        self.db = None
        try:
            # One read transaction, so a sync landing mid-request can't make its queries disagree
            self.db = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=30, isolation_level=None)
            self.db.execute("BEGIN")
            self.respond(200, handler(query))
        except (sqlite3.Error, OSError) as error:
            self.respond(500, {"error": str(error)})
        finally:
            if self.db is not None:
                self.db.close()

    def airport_name(self, ident):
        return self.store.airports.names.get(ident, ident)

    def airline_name(self, code):
        return self.store.airlines.get(code, code)

    def daily(self, sql, params):
        return [[r[0], r[1]] for r in self.db.execute(sql, params)]

    def api_describe(self, query):
        """Airline, route and aircraft type for an aircraft in the air now, for garage_pi's display page. Takes what
        garage_pi knows (callsign, registry make and model, SkyAware's type) and where it is (lat, lon, track, vr)."""
        def number(name):
            try:
                value = float(query.get(name, ""))
            except ValueError:
                return None
            return value if math.isfinite(value) else None
        row = {k: query.get(k, "")[:80] or None for k in ("callsign", "make", "model", "type", "icao_type", "registrant", "operator")}
        callsign = (row["callsign"] or "").strip().upper()
        row["callsign"] = callsign or None
        actype = aircraft_type(row, self.store.types)
        airline = AIRLINE_CALLSIGN.match(callsign)
        route = None
        found = route_airports(self.db, callsign)
        if found:
            vr = number("vr")
            leg = self.store.live_leg(found[0].split("-"), number("lat"), number("lon"), number("track"),
                                      None if vr is None or abs(vr) < 500 else vr < 0)
            if leg:
                route = [{"ident": a, "name": self.airport_name(a), "iata": (self.store.iata.get(a) or [None])[0],
                          "city": (self.store.iata.get(a) or [None, None])[1]} for a in leg]
        return {"callsign": callsign or None, "airline": airline.group(1) if airline else None,
                "airline_name": self.store.airlines.get(airline.group(1)) if airline else None,
                "actype": actype, "type_name": self.store.types.get(actype) if actype else None,
                "cargo": cargo_carrier(row, self.store.cargo_rules), "route": route}

    def api_summary(self, _query):
        s, r = self.since, self.radius
        one = lambda sql, *p: self.db.execute(sql, p).fetchone()
        flights, aircraft, interesting = one("SELECT COUNT(*), COUNT(DISTINCT hex), COALESCE(SUM(interesting), 0) FROM flights WHERE first_seen >= ?", s)
        takeoffs, landings = one("SELECT COALESCE(SUM(kind = 'takeoff'), 0), COALESCE(SUM(kind = 'landing'), 0) FROM events WHERE t >= ?", s)
        overhead = one("SELECT COUNT(*) FROM flights WHERE closest_km <= ? AND first_seen >= ?", r, s)[0]
        cargo = one("SELECT COUNT(*) FROM flights WHERE cargo IS NOT NULL AND first_seen >= ?", s)[0]
        types = one("SELECT COUNT(DISTINCT %s) FROM flights WHERE first_seen >= ?" % TYPE_KEY, s)[0]
        first, last = one("SELECT MIN(first_seen), MAX(last_seen) FROM flights")
        go_arounds, emergencies = one("SELECT COALESCE(SUM(go_around IS NOT NULL), 0), COALESCE(SUM(emergency IS NOT NULL), 0) FROM flights "
                                      "WHERE (emergency IS NOT NULL OR go_around IS NOT NULL) AND first_seen >= ?", s)
        # Ids let the page mark go-arounds and emergencies that arrived since the viewer last looked
        notable = [{"id": r[0], "kind": "go_around" if r[1] else "emergency"} for r in self.db.execute(
            "SELECT id, go_around IS NOT NULL FROM flights WHERE emergency IS NOT NULL OR go_around IS NOT NULL")]
        days = {}
        for day, count in self.db.execute("SELECT date(first_seen, 'unixepoch', 'localtime'), COUNT(*) FROM flights WHERE first_seen >= ? GROUP BY 1", (s,)):
            days.setdefault(day, {"day": day, "flights": 0, "overhead": 0, "takeoffs": 0, "landings": 0})["flights"] = count
        for day, count in self.db.execute("SELECT date(first_seen, 'unixepoch', 'localtime'), COUNT(*) FROM flights WHERE closest_km <= ? AND first_seen >= ? GROUP BY 1", (r, s)):
            days.setdefault(day, {"day": day, "flights": 0, "overhead": 0, "takeoffs": 0, "landings": 0})["overhead"] = count
        for day, kind, count in self.db.execute("SELECT date(t, 'unixepoch', 'localtime'), kind, COUNT(*) FROM events WHERE t >= ? GROUP BY 1, 2", (s,)):
            days.setdefault(day, {"day": day, "flights": 0, "overhead": 0, "takeoffs": 0, "landings": 0})[kind + "s"] = count
        return {"flights": flights, "aircraft": aircraft, "interesting": interesting, "takeoffs": takeoffs,
                "landings": landings, "overhead": overhead, "radius": r, "first": first, "last": last,
                "go_arounds": go_arounds, "emergencies": emergencies, "notable": notable,
                "diversions": sum(1 for d in self.store.diversions.values() if d["t"] >= s),
                "vip": one("SELECT COUNT(*) FROM flights WHERE vip IS NOT NULL AND first_seen >= ?", s)[0], "cargo": cargo, "types": types,
                "daily": sorted(days.values(), key=lambda d: d["day"]), "store": self.store.status,
                "bytes": self.store.size(), "cap_bytes": MAX_DB_BYTES}

    def api_planes(self, query):
        order = "visits DESC" if query.get("sort") == "visits" else "(takeoffs + landings) DESC, visits DESC"
        return {"planes": self.rows("""
            SELECT COALESCE(f.reg, f.hex) AS key, MAX(f.reg) AS reg, MAX(f.hex) AS hex, MAX(f.type) AS type,
                   MAX(f.actype) AS actype, MAX(f.cargo) AS cargo,
                   MAX(TRIM(COALESCE(f.make, '') || ' ' || COALESCE(f.model, ''))) AS model,
                   MAX(COALESCE(f.registrant, f.operator)) AS owner, MAX(f.airline) AS airline,
                   COUNT(DISTINCT f.id) AS visits, COUNT(DISTINCT CASE WHEN e.kind = 'takeoff' THEN f.id END) AS takeoffs,
                   COUNT(DISTINCT CASE WHEN e.kind = 'landing' THEN f.id END) AS landings, MAX(f.last_seen) AS last_seen,
                   MAX(f.interesting) AS interesting
            FROM flights f LEFT JOIN events e ON e.flight_id = f.id WHERE f.first_seen >= ?
            GROUP BY 1 ORDER BY %s LIMIT ?""" % order, (self.since, MAX_LIST))}

    def plane_where(self, key):
        return ("(f.reg = ? OR (f.reg IS NULL AND f.hex = ?))", [key, key.lower()])

    def api_plane(self, query):
        key = query.get("key", "")[:12]
        where, params = self.plane_where(key)
        flights = self.rows("SELECT f.* FROM flights f WHERE %s AND f.first_seen >= ? ORDER BY f.first_seen DESC LIMIT ?" % where,
                            params + [self.since, MAX_LIST])
        events = self.rows("""SELECT e.kind, e.airport, e.t, f.callsign FROM events e JOIN flights f ON f.id = e.flight_id
                              WHERE %s AND e.t >= ? ORDER BY e.t DESC LIMIT ?""" % where, params + [self.since, MAX_LIST])
        for e in events:
            e["airport_name"] = self.airport_name(e["airport"])
        airports = {}
        for e in events:
            a = airports.setdefault(e["airport"], {"airport": e["airport"], "name": e["airport_name"], "takeoffs": 0, "landings": 0})
            a[e["kind"] + "s"] += 1
        totals = self.db.execute("SELECT COUNT(*), MIN(first_seen), MAX(last_seen) FROM flights f WHERE %s" % where, params).fetchone()
        for f in flights:
            f["airline_name"] = self.airline_name(f["airline"]) if f["airline"] else None
        return {"key": key, "flights": flights, "events": events, "airports": sorted(airports.values(), key=lambda a: -(a["takeoffs"] + a["landings"])),
                "all_time": {"visits": totals[0], "first": totals[1], "last": totals[2]}, "home": HOME}

    def api_airlines(self, _query):
        rows = self.rows("""
            SELECT f.airline AS code, COUNT(DISTINCT f.id) AS flights, COUNT(DISTINCT f.hex) AS aircraft,
                   COUNT(DISTINCT CASE WHEN e.kind = 'takeoff' THEN f.id END) AS takeoffs,
                   COUNT(DISTINCT CASE WHEN e.kind = 'landing' THEN f.id END) AS landings
            FROM flights f LEFT JOIN events e ON e.flight_id = f.id
            WHERE f.airline IS NOT NULL AND f.first_seen >= ? GROUP BY 1 ORDER BY flights DESC LIMIT ?""", (self.since, MAX_LIST))
        for r in rows:
            r["name"] = self.airline_name(r["code"])
            r["cargo"] = self.store.cargo_rules["callsigns"].get(r["code"])
        return {"airlines": rows}

    def api_types(self, _query):
        return {"types": self.rows("""
            SELECT %s AS key, MAX(f.actype) AS actype, COUNT(DISTINCT f.id) AS flights, COUNT(DISTINCT f.hex) AS aircraft,
                   COUNT(DISTINCT CASE WHEN e.kind = 'takeoff' THEN f.id END) AS takeoffs,
                   COUNT(DISTINCT CASE WHEN e.kind = 'landing' THEN f.id END) AS landings,
                   COUNT(DISTINCT CASE WHEN f.cargo IS NOT NULL THEN f.id END) AS cargo, MAX(f.last_seen) AS last_seen
            FROM flights f LEFT JOIN events e ON e.flight_id = f.id WHERE f.first_seen >= ? AND %s IS NOT NULL
            GROUP BY 1 ORDER BY flights DESC LIMIT ?""" % (TYPE_KEY, TYPE_KEY), (self.since, MAX_LIST))}

    def api_actype(self, query):
        key = query.get("key", "")[:80]
        where = "%s = ? AND first_seen >= ?" % TYPE_KEY
        totals = self.db.execute("SELECT COUNT(*), COUNT(DISTINCT hex), MAX(actype) FROM flights WHERE " + where, (key, self.since)).fetchone()
        airports = self.rows("""SELECT e.airport, SUM(e.kind = 'takeoff') AS takeoffs, SUM(e.kind = 'landing') AS landings
                                FROM events e JOIN flights f ON f.id = e.flight_id WHERE %s = ? AND e.t >= ?
                                GROUP BY 1 ORDER BY COUNT(*) DESC""" % TYPE_KEY.replace("actype", "f.actype"), (key, self.since))
        for a in airports:
            a["name"] = self.airport_name(a["airport"])
        airlines = self.rows("""SELECT airline AS code, COUNT(*) AS flights FROM flights WHERE %s AND airline IS NOT NULL
                                GROUP BY 1 ORDER BY 2 DESC LIMIT 30""" % where, (key, self.since))
        for a in airlines:
            a["name"] = self.airline_name(a["code"])
        planes = self.rows("""SELECT COALESCE(reg, hex) AS key, COUNT(*) AS visits, MAX(last_seen) AS last_seen,
                                     MAX(COALESCE(registrant, operator)) AS owner FROM flights WHERE %s
                              GROUP BY 1 ORDER BY visits DESC LIMIT 50""" % where, (key, self.since))
        recent = self.rows("""SELECT id, COALESCE(reg, hex) AS key, callsign, airline, cargo, first_seen, closest_km, origin, destination FROM flights
                              WHERE %s ORDER BY first_seen DESC LIMIT 100""" % where, (key, self.since))
        daily = self.daily("SELECT date(first_seen, 'unixepoch', 'localtime'), COUNT(*) FROM flights WHERE %s GROUP BY 1" % where,
                           (key, self.since))
        return {"key": key, "actype": totals[2], "name": self.store.types.get(totals[2]) if totals[2] else key,
                "flights": totals[0], "aircraft": totals[1], "takeoffs": sum(a["takeoffs"] for a in airports),
                "landings": sum(a["landings"] for a in airports), "airports": airports, "airlines": airlines,
                "planes": planes, "recent": recent, "daily": daily}

    def route_ends(self, end, where, params):
        """Counts of origins or destinations for flights matching where, busiest first, with airport names."""
        rows = self.rows("""SELECT %s AS airport, COUNT(*) AS flights FROM flights WHERE first_seen >= ? AND %s IS NOT NULL AND %s
                            GROUP BY 1 ORDER BY 2 DESC LIMIT 60""" % (end, end, where), [self.since] + list(params))
        for r in rows:
            r["name"] = self.airport_name(r["airport"])
        return rows

    def kind_filter(self, query):
        kind = query.get("kind", "")
        return ("(%s) = ?" % KIND_SQL, [kind]) if kind in KINDS else ("1", [])

    def api_destinations(self, query):
        """Where flights heard here were going and coming from, by flight type, optionally only those departing one
        local airport or starting anywhere else (arrivals and overflights), by route."""
        local = sorted(self.store.local)
        departing = query.get("from", "").upper()
        if departing in local:
            dep_where, dep_params = "origin = ?", [departing]
        elif departing == "OTHER":
            dep_where, dep_params = "origin NOT IN (%s)" % ",".join("?" * len(local)), local
        else:
            departing, dep_where, dep_params = "", "1", []
        kinds = {k: {"kind": k, "flights": 0, "routed": 0} for k in KINDS}
        for kind, flights, routed in self.db.execute(
                "SELECT %s, COUNT(*), SUM(destination IS NOT NULL) FROM flights WHERE first_seen >= ? AND %s GROUP BY 1" % (KIND_SQL, dep_where),
                [self.since] + dep_params):
            kinds[kind].update(flights=flights, routed=routed)
        where, params = self.kind_filter(query)
        where, params = "%s AND %s" % (where, dep_where), params + dep_params
        return {"kinds": list(kinds.values()), "to": self.route_ends("destination", where, params),
                "from": [] if departing in local else self.route_ends("origin", where, params),
                "local": [{"ident": a, "name": self.airport_name(a)} for a in local], "departing": departing.lower() if departing == "OTHER" else departing}

    def api_dest(self, query):
        airport = query.get("airport", "")[:4].upper()
        where, params = self.kind_filter(query)
        flights = self.rows("""SELECT id, COALESCE(reg, hex) AS key, callsign, actype, type, cargo, origin, destination, first_seen,
                                      %s AS kind FROM flights WHERE first_seen >= ? AND (origin = ? OR destination = ?) AND %s
                               ORDER BY first_seen DESC LIMIT ?""" % (KIND_SQL, where), [self.since, airport, airport] + params + [MAX_LIST])
        airlines = self.rows("""SELECT airline AS code, SUM(destination = ?) AS going, SUM(origin = ?) AS coming FROM flights
                                WHERE first_seen >= ? AND (origin = ? OR destination = ?) AND airline IS NOT NULL AND %s
                                GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 30""" % where, [airport, airport, self.since, airport, airport] + params)
        for a in airlines:
            a["name"] = self.airline_name(a["code"])
        kinds = self.rows("""SELECT %s AS kind, COUNT(*) AS flights FROM flights WHERE first_seen >= ? AND (origin = ? OR destination = ?)
                             GROUP BY 1 ORDER BY 2 DESC""" % KIND_SQL, (self.since, airport, airport))
        return {"airport": airport, "name": self.airport_name(airport), "flights": flights, "airlines": airlines, "kinds": kinds,
                "local": airport in self.store.local}

    def api_cargo(self, _query):
        carriers = self.rows("""
            SELECT f.cargo AS carrier, COUNT(DISTINCT f.id) AS flights, COUNT(DISTINCT f.hex) AS aircraft,
                   COUNT(DISTINCT CASE WHEN e.kind = 'takeoff' THEN f.id END) AS takeoffs,
                   COUNT(DISTINCT CASE WHEN e.kind = 'landing' THEN f.id END) AS landings
            FROM flights f LEFT JOIN events e ON e.flight_id = f.id WHERE f.cargo IS NOT NULL AND f.first_seen >= ?
            GROUP BY 1 ORDER BY flights DESC""", (self.since,))
        recent = self.rows("""SELECT id, COALESCE(reg, hex) AS key, callsign, cargo, actype, type, first_seen, closest_km, origin, destination
                              FROM flights WHERE cargo IS NOT NULL AND first_seen >= ? ORDER BY first_seen DESC LIMIT ?""",
                           (self.since, MAX_LIST))
        daily = self.daily("SELECT date(first_seen, 'unixepoch', 'localtime'), COUNT(*) FROM flights WHERE cargo IS NOT NULL "
                           "AND first_seen >= ? GROUP BY 1", (self.since,))
        return {"carriers": carriers, "recent": recent, "daily": daily}

    def api_airline(self, query):
        code = query.get("code", "")[:3].upper()
        airports = self.rows("""SELECT e.airport, SUM(e.kind = 'takeoff') AS takeoffs, SUM(e.kind = 'landing') AS landings
                                FROM events e JOIN flights f ON f.id = e.flight_id WHERE f.airline = ? AND e.t >= ?
                                GROUP BY 1 ORDER BY COUNT(*) DESC""", (code, self.since))
        for a in airports:
            a["name"] = self.airport_name(a["airport"])
        planes = self.rows("""SELECT COALESCE(reg, hex) AS key, MAX(type) AS type, MAX(actype) AS actype, COUNT(*) AS visits, MAX(last_seen) AS last_seen
                              FROM flights WHERE airline = ? AND first_seen >= ? GROUP BY 1 ORDER BY visits DESC LIMIT 50""", (code, self.since))
        types = self.rows("""SELECT %s AS key, MAX(actype) AS actype, COUNT(*) AS flights FROM flights WHERE airline = ? AND first_seen >= ?
                             GROUP BY 1 ORDER BY 3 DESC LIMIT 20""" % TYPE_KEY, (code, self.since))
        daily = self.daily("SELECT date(first_seen, 'unixepoch', 'localtime'), COUNT(*) FROM flights WHERE airline = ? AND first_seen >= ? GROUP BY 1",
                           (code, self.since))
        return {"code": code, "name": self.airline_name(code), "airports": airports, "planes": planes, "daily": daily,
                "types": types, "cargo": self.store.cargo_rules["callsigns"].get(code),
                "to": self.route_ends("destination", "airline = ?", [code]),
                "from": self.route_ends("origin", "airline = ?", [code])}

    def api_airports(self, _query):
        rows = self.rows("""SELECT airport, SUM(kind = 'takeoff') AS takeoffs, SUM(kind = 'landing') AS landings
                            FROM events WHERE t >= ? GROUP BY 1 ORDER BY COUNT(*) DESC""", (self.since,))
        for r in rows:
            r["name"] = self.airport_name(r["airport"])
        return {"airports": rows}

    def api_airport(self, query):
        ident = query.get("ident", "")[:8].upper()
        daily = {}
        for day, kind, count in self.db.execute("""SELECT date(t, 'unixepoch', 'localtime'), kind, COUNT(*) FROM events
                                                    WHERE airport = ? AND t >= ? GROUP BY 1, 2""", (ident, self.since)):
            daily.setdefault(day, {"day": day, "takeoffs": 0, "landings": 0})[kind + "s"] = count
        airlines = self.rows("""SELECT f.airline AS code, SUM(e.kind = 'takeoff') AS takeoffs, SUM(e.kind = 'landing') AS landings
                                FROM events e JOIN flights f ON f.id = e.flight_id WHERE e.airport = ? AND e.t >= ? AND f.airline IS NOT NULL
                                GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 30""", (ident, self.since))
        for a in airlines:
            a["name"] = self.airline_name(a["code"])
        recent = self.rows("""SELECT e.kind, e.t, f.id, COALESCE(f.reg, f.hex) AS key, f.callsign, f.type, f.actype, f.cargo, f.airline FROM events e
                              JOIN flights f ON f.id = e.flight_id WHERE e.airport = ? AND e.t >= ? ORDER BY e.t DESC LIMIT ?""",
                           (ident, self.since, MAX_LIST))
        # By route: where flights leaving here were going, and where flights arriving here came from
        return {"ident": ident, "name": self.airport_name(ident), "daily": sorted(daily.values(), key=lambda d: d["day"]),
                "airlines": airlines, "recent": recent,
                "to": self.route_ends("destination", "origin = ?", [ident]), "from": self.route_ends("origin", "destination = ?", [ident])}

    def api_overhead(self, query):
        alt = query.get("alt", "")
        clauses, params = ["closest_km <= ?", "first_seen >= ?"], [self.radius, self.since]
        if alt.isdigit():
            clauses.append("closest_alt <= ?")
            params.append(int(alt))
        where = " AND ".join(clauses)
        daily = self.daily("SELECT date(first_seen, 'unixepoch', 'localtime'), COUNT(*) FROM flights WHERE %s GROUP BY 1" % where, params)
        flights = self.rows("""SELECT id, COALESCE(reg, hex) AS key, callsign, type, actype, cargo, airline, closest_km, closest_alt, closest_at,
                                      first_seen, interesting, grp FROM flights WHERE %s ORDER BY first_seen DESC LIMIT ?""" % where,
                            params + [MAX_LIST])
        lowest = self.rows("""SELECT id, COALESCE(reg, hex) AS key, callsign, type, actype, closest_km, closest_alt, closest_at FROM flights
                              WHERE %s AND closest_alt > 0 ORDER BY closest_alt LIMIT 10""" % where, params)
        return {"radius": self.radius, "daily": daily, "flights": flights, "lowest": lowest,
                "total": self.db.execute("SELECT COUNT(*) FROM flights WHERE %s" % where, params).fetchone()[0]}

    def api_interesting(self, query):
        """The period's interesting flights; with q, a search of every interesting flight ever stored, whatever the
        period; with vip=1, only VIP aircraft."""
        text = query.get("q", "").strip()[:40]
        where, params = ["interesting = 1"], []
        if text:
            like = "%" + text.replace("%", "").replace("_", "").upper() + "%"
            where.append("(" + " OR ".join("UPPER(COALESCE(%s, '')) LIKE ?" % c for c in
                                           ("reg", "hex", "callsign", "registrant", "operator", "tags", "vip", "actype", "type", "grp")) + ")")
            params += [like] * 10
        else:
            where.append("first_seen >= ?")
            params.append(self.since)
        if query.get("vip") == "1":
            where.append("vip IS NOT NULL")
        sql = """SELECT id, COALESCE(reg, hex) AS key, callsign, type, actype, cargo, grp, ladd, private, emergency, vip, tags,
                        go_around, first_seen, closest_km, registrant, operator FROM flights WHERE %s
                 ORDER BY first_seen DESC LIMIT ?""" % " AND ".join(where)
        rows = self.rows(sql, params + [MAX_LIST])
        for r in rows:  # earlier flights by the same aircraft, all time (the UNIQUE (hex, first_seen) index answers this)
            r["seen_before"] = self.db.execute("SELECT COUNT(*) FROM flights WHERE hex = (SELECT hex FROM flights WHERE id = ?) "
                                               "AND first_seen < ?", (r["id"], r["first_seen"])).fetchone()[0]
        total = self.db.execute("SELECT COUNT(*) FROM flights WHERE %s" % " AND ".join(where), params).fetchone()[0]
        return {"flights": rows, "total": total, "searched": bool(text)}

    def api_notable(self, _query):
        rows = self.rows("""SELECT id, COALESCE(reg, hex) AS key, callsign, type, actype, cargo, airline, emergency, go_around, go_around_at,
                                   first_seen, last_seen, closest_km, points FROM flights
                            WHERE (emergency IS NOT NULL OR go_around IS NOT NULL) AND first_seen >= ?
                            ORDER BY COALESCE(go_around_at, first_seen) DESC LIMIT ?""", (self.since, MAX_LIST))
        for r in rows:
            r["airline_name"] = self.airline_name(r["airline"]) if r["airline"] else None
            r["airport_name"] = self.airport_name(r["go_around"]) if r["go_around"] else None
            if r["go_around"]:
                found = self.go_arounds(r["id"])
                r["go_around_count"] = len(found) or 1
                known_added = [g["added_s"] for g in found if g["added_s"] is not None]
                r["added_s"] = sum(known_added) if known_added else None
                r["added_at_least"] = any(g["how"] == "at least" for g in found)
        return {"flights": rows}

    def api_flight(self, query):
        flight_id = query.get("id", "")
        rows = self.rows("SELECT * FROM flights WHERE id = ?", (int(flight_id),)) if flight_id.isdigit() else []
        if not rows:
            return {"flight": None}
        flight = rows[0]
        flight["type_name"] = self.store.types.get(flight["actype"]) if flight.get("actype") else None
        flight["kind"] = self.db.execute("SELECT %s FROM flights WHERE id = ?" % KIND_SQL, (flight["id"],)).fetchone()[0]
        flight["airline_name"] = self.airline_name(flight["airline"]) if flight["airline"] else None
        flight["go_around_name"] = self.airport_name(flight["go_around"]) if flight["go_around"] else None
        events = self.rows("SELECT kind, airport, t FROM events WHERE flight_id = ? ORDER BY t", (flight["id"],))
        for e in events:
            e["airport_name"] = self.airport_name(e["airport"])
        return {"flight": flight, "events": events, "home": HOME, "go_arounds": self.go_arounds(flight["id"]),
                "diversion": self.diversion(flight["id"])}

    def go_arounds(self, flight_id):
        points = self.db.execute("SELECT t, lat, lon, alt FROM track_points WHERE flight_id = ? ORDER BY t", (flight_id,)).fetchall()
        events = self.db.execute("SELECT kind, airport, t FROM events WHERE flight_id = ?", (flight_id,)).fetchall()
        found = find_go_arounds(points, self.store.airports, events)
        for g in found:
            g["airport_name"] = self.airport_name(g["airport"])
        return found

    def diversion(self, flight_id, diversions=None):
        d = (self.store.diversions if diversions is None else diversions).get(flight_id)
        if not d:
            return None
        return dict(d, airport_name=self.airport_name(d["airport"]) if d["airport"] else None,
                    usual_name=self.airport_name(d["usual"]) if d["usual"] else None)

    def api_diversions(self, _query):
        found = {i: d for i, d in self.store.diversions.items() if d["t"] >= self.since}
        if not found:
            return {"flights": []}
        rows = self.rows("""SELECT id, COALESCE(reg, hex) AS key, callsign, type, actype, cargo, airline, first_seen FROM flights
                            WHERE id IN (%s)""" % ",".join("?" * len(found)), list(found))
        for r in rows:
            r["diversion"] = self.diversion(r["id"], found)  # the same snapshot, though a sync may replace the store's
            r["airline_name"] = self.airline_name(r["airline"]) if r["airline"] else None
        rows.sort(key=lambda r: -r["diversion"]["t"])
        return {"flights": rows[:MAX_LIST]}

    def api_track(self, query):
        flight_id = query.get("id", "")
        if not flight_id.isdigit():
            return {"points": []}
        return {"points": self.db.execute("SELECT t, lat, lon, alt FROM track_points WHERE flight_id = ? ORDER BY t", (int(flight_id),)).fetchall()}

    def api_search(self, query):
        text = query.get("q", "").strip().upper()[:12]
        if len(text) < 2:
            return {"planes": [], "airlines": [], "airports": []}
        like = "%" + text.replace("%", "").replace("_", "") + "%"
        planes = self.rows("""SELECT COALESCE(reg, hex) AS key, MAX(type) AS type, MAX(actype) AS actype, COUNT(*) AS visits FROM flights
                              WHERE UPPER(reg) LIKE ? OR UPPER(hex) LIKE ? OR callsign LIKE ? GROUP BY 1 ORDER BY visits DESC LIMIT 20""",
                           (like, like, like))
        airlines = [{"code": c, "name": n} for c, n in self.store.airlines.items() if text in c or text in n.upper()][:10]
        types = [{"key": c, "name": n} for c, n in self.store.types.items() if text in c or text in n.upper()][:10]
        airports = [{"ident": a[0], "name": a[1]} for a in self.store.airports.near if text in a[0] or text in a[1].upper()][:10]
        return {"planes": planes, "airlines": airlines, "airports": airports, "types": types}


def main():
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    store = Store()
    Api.store = store
    server = http.server.ThreadingHTTPServer(("127.0.0.1", API_PORT), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("flightstore started,", store.size(), "bytes")
    store.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
