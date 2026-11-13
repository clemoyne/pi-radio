#!/usr/bin/env python3
"""Passive BLE presence tracker. Standard library only (Python 3.7)."""
import collections
import ctypes
import functools
import json
import os
import re
import signal
import socket
import sqlite3
import struct
import sys
import time

STATE_DB = "/var/lib/bletracker/tracker.db"
IDS_DB = "/opt/bletracker/ids.db"
DATA_JSON = "/run/bletracker/data.json"
UPTIME_FILE = "/var/lib/piuptime/uptime.json"

SCAN_INTERVAL = 480  # 300 ms in 0.625 ms units
SCAN_WINDOW = 48  # 30 ms in 0.625 ms units
SCAN_RESTART_SECONDS = 60  # clears the controller's duplicate filter
SESSION_GAP_SECONDS = 900
FLUSH_SECONDS = 60
MEMORY_CHECK_SECONDS = 30
PRUNE_SECONDS = 86400
RETENTION_SECONDS = 90 * 86400
MIN_AVAILABLE_MB = 100
RESUME_AVAILABLE_MB = 120
MAX_LIVE_DEVICES = 3000
MAX_JSON_DEVICES = 500
MAX_TIMELINE_DEVICES = 40
MAX_EVENTS = 60
MAX_META_ITEMS = 8
MAX_ROTATING_ADDRESSES = 5000
ROTATING_WINDOW_SECONDS = 300
ROTATING_SILENCE_SECONDS = 270  # an address this quiet has ended, so its successor can be picked
ROTATING_LINK_GAP_SECONDS = 240
ROTATING_LINK_OVERLAP_SECONDS = 5
ROTATING_LINK_MAX_RSSI_DB = 10
ROTATING_KEEP_SECONDS = 7200
FIND_MY_VENDOR_KEY = "findmy"
RECENT_READINGS = 15
MAX_HOUR_READINGS = 120
TREND_MIN_READINGS = 6
TREND_MIN_DB = 8
MOBILITY_REFRESH_SECONDS = 900
MOBILITY_WINDOW_SECONDS = 14 * 86400
MOBILITY_MIN_TRACKED_HOURS = 24
NO_VENDOR = "No manufacturer broadcast"
UNREGISTERED_VENDOR = "Unregistered ID"

SET_SCAN_PARAMS = 0x200B
SET_SCAN_ENABLE = 0x200C

APPLE_COMPANY = "004c"
APPLE_FIND_MY = 0x12
APPLE_IBEACON = 0x02
TIME_ERROR = 5  # adjtimex() return value while the clock is unsynchronized

SHADE_MODELS = {"GW3": "PowerView gateway", "SIL": "PowerView Silhouette shade", "DUE": "PowerView Duette shade"}
VENDOR_CATEGORIES = [
    (r"sonos|bose|sonova|sennheiser|harman|jbl", "Audio"),
    (r"tile", "Tracker"),
    (r"nest|google|etekcity|govee|ihoment|allegion|schlage", "Smart home"),
    (r"samsung|\blg\b|sony|vizio|roku", "Consumer electronics"),
    (r"\bhp\b|canon|epson|brother", "Printer"),
    (r"microsoft", "Computer"),
    (r"garmin|fitbit|whoop|huami", "Wearable"),
]
APPEARANCE_CATEGORIES = {
    1: "Phone", 2: "Computer", 3: "Wearable", 4: "Clock", 5: "Display", 6: "Remote control",
    7: "Eyewear", 8: "Tag", 9: "Keyring", 10: "Audio", 11: "Barcode scanner", 12: "Thermometer",
    13: "Heart rate sensor", 15: "Input device", 49: "Wearable",
}

stop_requested = False


def on_signal(_signum, _frame):
    global stop_requested
    stop_requested = True


def open_hci():
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    sock.bind((0,))
    # Packet type: events. Events: Command Complete, Command Status, LE Meta.
    sock.setsockopt(0, 2, struct.pack("<IIIH2x", 1 << 4, 0xC000, 1 << 30, 0))
    sock.settimeout(0.5)
    return sock


def address_kind(addr_type, addr):
    if addr_type in (0, 2):
        return "public"
    return {3: "random_static", 1: "rpa", 0: "nrpa"}.get(addr[5] >> 6, "reserved")


def parse_ad(data):
    ad = {"names": [], "uuid16": [], "uuid128": [], "service_data": [], "company_ids": [],
          "apple_types": [], "appearance": None, "tx_power": None}
    i = 0
    while i < len(data):
        length = data[i]
        if length == 0 or i + 1 + length > len(data):
            break
        ad_type = data[i + 1]
        value = data[i + 2:i + 1 + length]
        i += 1 + length
        if ad_type in (0x08, 0x09):
            name = "".join(c for c in value.decode("utf-8", "replace") if c.isprintable()).strip()
            if name:
                ad["names"].append(name[:64])
        elif ad_type in (0x02, 0x03):
            for j in range(0, len(value) - 1, 2):
                ad["uuid16"].append("%04x" % struct.unpack("<H", value[j:j + 2])[0])
        elif ad_type in (0x06, 0x07):
            for j in range(0, len(value) - 15, 16):
                ad["uuid128"].append(value[j:j + 16][::-1].hex())
        elif ad_type == 0x16 and len(value) >= 2:
            ad["service_data"].append("%04x" % struct.unpack("<H", value[:2])[0])
        elif ad_type == 0xFF and len(value) >= 2:
            company = "%04x" % struct.unpack("<H", value[:2])[0]
            ad["company_ids"].append(company)
            if company == APPLE_COMPANY and len(value) >= 3:
                ad["apple_types"].append(value[2])
        elif ad_type == 0x19 and len(value) == 2:
            ad["appearance"] = struct.unpack("<H", value)[0]
        elif ad_type == 0x0A and len(value) == 1:
            ad["tx_power"] = struct.unpack("b", value)[0]
    return ad


class Ids:
    def __init__(self, path):
        self.db = None
        if os.path.exists(path):
            self.db = sqlite3.connect("file:%s?mode=ro&immutable=1" % path, uri=True)

    @functools.lru_cache(maxsize=512)
    def lookup(self, table, column, key):
        if self.db is None:
            return None
        row = self.db.execute("SELECT name FROM %s WHERE %s = ?" % (table, column), (key,)).fetchone()
        return row[0] if row else None

    def company(self, hex_id):
        return self.lookup("company", "id", int(hex_id, 16))

    def uuid16(self, hex_id):
        return self.lookup("uuid16", "id", int(hex_id, 16))

    def oui(self, mac):
        return self.lookup("oui", "prefix", mac[:8])


def vendor_category(vendor):
    for pattern, category in VENDOR_CATEGORIES:
        if re.search(pattern, vendor, re.I):
            return category
    return "Other"


def clean_vendor(vendor):
    return re.sub(r"[,.]?\s+(Inc|Corp|Corporation|Co|Ltd|LLC|GmbH|Limited)\.?.*$", "", vendor).strip()


def classify(dev, ids):
    names = sorted(dev["names"])
    evidence = []
    if names:
        evidence.append("name %s" % names[0])

    prefix = names[0].split(":")[0] if names and re.match(r"^[A-Z0-9]{3}:[0-9A-Fa-f]{4}$", names[0]) else None
    if "0819" in dev["company_ids"]:
        evidence.append("company ID 0x0819 (Hunter Douglas)")
        return ("Window covering", SHADE_MODELS.get(prefix, "Hunter Douglas PowerView device"), "high", evidence)

    apple_types = set(dev["apple_types"])
    if APPLE_COMPANY in dev["company_ids"] and APPLE_IBEACON in apple_types:
        evidence.append("Apple iBeacon advertisement")
        return ("Beacon", "iBeacon broadcaster", "medium", evidence)

    for name in names:
        if re.match(r"(ihoment|govee)_", name, re.I):
            return ("Smart home", "Govee device", "high", evidence)

    if dev["appearance"]:
        category = APPEARANCE_CATEGORIES.get(dev["appearance"] >> 6)
        if category:
            evidence.append("appearance 0x%04x" % dev["appearance"])
            return (category, category, "medium", evidence)

    if APPLE_COMPANY in dev["company_ids"]:
        evidence.append("company ID 0x004c (Apple)")
        return ("Apple", "Apple device", "medium", evidence)

    for hex_id in sorted(dev["company_ids"]):
        vendor = ids.company(hex_id)
        if vendor:
            evidence.append("company ID 0x%s (%s)" % (hex_id, vendor))
            return (vendor_category(vendor), "%s device" % clean_vendor(vendor), "medium", evidence)

    for hex_id in sorted(dev["uuid16"] | dev["service_data"]):
        vendor = ids.uuid16(hex_id)
        if vendor:
            evidence.append("service 0x%s (%s)" % (hex_id, vendor))
            return (vendor_category(vendor), "%s device" % clean_vendor(vendor), "medium", evidence)

    if dev["kind"] == "public":
        vendor = ids.oui(dev["mac"])
        if vendor:
            evidence.append("address prefix %s (%s)" % (dev["mac"][:8], vendor))
            return (vendor_category(vendor), "%s device" % clean_vendor(vendor), "low", evidence)

    return ("Unidentified", "Unidentified", "none", evidence or ["no identifying data broadcast"])


def vendor_key(ad):
    if ad["company_ids"]:
        return "c:" + ad["company_ids"][0]
    services = ad["uuid16"] + ad["service_data"]
    return "u:" + services[0] if services else ""


class RotatingCounter:
    """Estimates how many devices sit behind rotating addresses.

    Addresses and the links between them stay in memory; only the counts leave this class.
    """

    def __init__(self, now):
        self.addrs = {}
        self.windows = {}
        self.reports = {}
        self.started_window = now // ROTATING_WINDOW_SECONDS * ROTATING_WINDOW_SECONDS

    def record(self, addr, key, signature, rssi, now):
        addr = bytes(addr)
        state = self.addrs.get(addr)
        if state is None:
            if len(self.addrs) >= MAX_ROTATING_ADDRESSES:
                return
            state = self.addrs[addr] = {
                "key": key, "signature": signature, "first": now, "last": now, "start_rssi": [],
                "end_rssi": collections.deque(maxlen=5), "parent": None, "checked": False,
            }
        state["last"] = now
        state["end_rssi"].append(rssi)
        if len(state["start_rssi"]) < 3:
            state["start_rssi"].append(rssi)
        window = now // ROTATING_WINDOW_SECONDS * ROTATING_WINDOW_SECONDS
        self.windows.setdefault(window, {}).setdefault(state["key"], set()).add(addr)
        hour_key = (now // 3600 * 3600, state["key"])
        self.reports[hour_key] = self.reports.get(hour_key, 0) + 1

    def root(self, addr):
        state = self.addrs.get(addr)
        while state is not None and state["parent"] in self.addrs:
            addr = state["parent"]
            state = self.addrs[addr]
        return addr

    def link(self, now):
        ended = [(s["last"], addr) for addr, s in self.addrs.items()
                 if not s["checked"] and now - s["last"] >= ROTATING_SILENCE_SECONDS]
        for _, addr in sorted(ended):
            state = self.addrs[addr]
            state["checked"] = True
            end_rssi = median(state["end_rssi"])
            best, best_score = None, None
            for other, candidate in self.addrs.items():
                if (other == addr or candidate["parent"] is not None or candidate["key"] != state["key"]
                        or candidate["signature"] != state["signature"] or other == self.root(addr)):
                    continue
                gap = candidate["first"] - state["last"]
                if gap < -ROTATING_LINK_OVERLAP_SECONDS or gap > ROTATING_LINK_GAP_SECONDS:
                    continue
                difference = abs(median(candidate["start_rssi"]) - end_rssi)
                if difference > ROTATING_LINK_MAX_RSSI_DB:
                    continue
                score = difference + abs(gap) / 30
                if best is None or score < best_score:
                    best, best_score = other, score
            if best is not None:
                self.addrs[best]["parent"] = addr

    def hour_counts(self, hour, now):
        """Returns {vendor key: (typical at once, peak at once, different devices)} for one hour."""
        starts = [hour + i * ROTATING_WINDOW_SECONDS for i in range(3600 // ROTATING_WINDOW_SECONDS)]
        starts = [w for w in starts if self.started_window <= w <= now]
        complete = [w for w in starts if w + ROTATING_WINDOW_SECONDS <= now]
        used = complete or starts
        by_key = {}
        for window in used:
            for key, addrs in self.windows.get(window, {}).items():
                by_key.setdefault(key, {})[window] = {self.root(addr) for addr in addrs}
        counts = {}
        for key, by_window in by_key.items():
            sizes = sorted(len(by_window.get(window, ())) for window in used)
            counts[key] = (sizes[len(sizes) // 2], sizes[-1], len(set().union(*by_window.values())))
        return counts

    def take_reports(self, hour, key):
        return self.reports.pop((hour, key), 0)

    def prune(self, now):
        for window in [w for w in self.windows if w < now - ROTATING_KEEP_SECONDS]:
            del self.windows[window]
        margin = ROTATING_KEEP_SECONDS + 2 * ROTATING_WINDOW_SECONDS
        for addr in [a for a, s in self.addrs.items() if s["last"] < now - margin]:
            del self.addrs[addr]
        for hour_key in [k for k in self.reports if k[0] < now // 3600 * 3600 - 3600]:
            del self.reports[hour_key]


def median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def distance_band(rssi):
    if rssi is None:
        return None
    if rssi > -60:
        return "Very close"
    if rssi > -75:
        return "Nearby"
    if rssi > -90:
        return "In or near the building"
    return "Edge of range"


def signal_trend(readings):
    if len(readings) < TREND_MIN_READINGS:
        return None
    values = list(readings)
    middle = len(values) // 2
    change = median(values[middle:]) - median(values[:middle])
    if change >= TREND_MIN_DB:
        return "Getting closer"
    if change <= -TREND_MIN_DB:
        return "Moving away"
    return None


def mobility_prior(category, label):
    if category in ("Window covering", "Printer", "Smart home") or "Sonos" in label:
        return ("likely_fixed", "Likely fixed", "based on device type")
    if category in ("Tracker", "Wearable", "Phone", "Tag", "Keyring"):
        return ("moves", "Likely moves", "based on device type")
    return None


def classify_mobility(category, label, hourly_rssi, tracked_hours, visits, here):
    observed = len(hourly_rssi)
    if tracked_hours >= MOBILITY_MIN_TRACKED_HOURS and observed:
        ordered = sorted(hourly_rssi)
        spread = ordered[int(observed * 0.9)] - ordered[int(observed * 0.1)]
        presence = observed / max(1, tracked_hours)
        strongest = ordered[-1]
        if observed >= 4:
            if spread >= 15:
                return ("moves", "Likely moves", "signal varied by %d dB between hours" % spread)
            if presence >= 0.8 and spread <= 10:
                return ("fixed", "Fixed", "heard in %d%% of hours with a steady signal" % round(presence * 100))
            if presence >= 0.4 and spread <= 8 and strongest <= -85:
                return ("fixed", "Fixed", "steady weak signal, at the edge of range")
            if visits >= 3 and presence < 0.4 and strongest >= -80:
                return ("moves", "Likely moves", "%d separate visits, away most of the time" % visits)
        elif observed <= 2 and not here and strongest >= -85:
            return ("moves", "Likely moves", "heard briefly, then gone")
    prior = mobility_prior(category, label)
    if prior:
        return prior
    if tracked_hours < MOBILITY_MIN_TRACKED_HOURS:
        return ("unknown", "Not sure yet", "needs 24 hours of data")
    return ("unknown", "Not sure yet", "pattern is unclear")


def read_uptime():
    with open("/proc/uptime") as f:
        since_boot = int(float(f.read().split()[0]))
    uptime = {"since_boot": since_boot, "total": None}
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot_id = f.read().strip()
        with open(UPTIME_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return uptime
    boots = dict(state["boots"])
    boots[boot_id] = max(boots.get(boot_id, 0), since_boot)
    uptime.update(total=sum(boots.values()), boots=len(boots), longest=max(boots.values()),
                  counting_since=state["counting_since"])
    return uptime


def available_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return None


def wait_for_clock():
    # No RTC: timestamps are meaningless until NTP has set the clock.
    libc = ctypes.CDLL(None)
    timex = ctypes.create_string_buffer(512)
    while not stop_requested and libc.adjtimex(timex) == TIME_ERROR:
        time.sleep(15)


def meta_to_json(dev):
    return json.dumps({
        "names": sorted(dev["names"]), "uuid16": sorted(dev["uuid16"]), "uuid128": sorted(dev["uuid128"]),
        "service_data": sorted(dev["service_data"]), "company_ids": sorted(dev["company_ids"]),
        "apple_types": sorted(dev["apple_types"]), "appearance": dev["appearance"], "tx_power": dev["tx_power"],
    })


class Tracker:
    def __init__(self):
        self.sock = None
        self.ids = Ids(IDS_DB)
        self.db = sqlite3.connect(STATE_DB)
        self.live = {}
        self.filtered = {"rotating": 0, "findmy": 0}
        self.counter = RotatingCounter(int(time.time()))
        self.mobility = {}
        self.tracked_hours = 0
        self.paused = False
        self.started = time.time()
        self.init_db()
        self.restore_live()

    def init_db(self):
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            CREATE TABLE IF NOT EXISTS devices (
                mac TEXT PRIMARY KEY, kind TEXT NOT NULL, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                reports INTEGER NOT NULL, meta TEXT NOT NULL, category TEXT, label TEXT, confidence TEXT,
                evidence TEXT, last_rssi INTEGER);
            CREATE TABLE IF NOT EXISTS sessions (
                mac TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, reports INTEGER NOT NULL,
                rssi_min INTEGER, rssi_max INTEGER, rssi_sum INTEGER, PRIMARY KEY (mac, start));
            CREATE INDEX IF NOT EXISTS sessions_end ON sessions (end);
            CREATE TABLE IF NOT EXISTS filtered (
                hour INTEGER PRIMARY KEY, rotating INTEGER NOT NULL, findmy INTEGER NOT NULL);
            DROP TABLE IF EXISTS rotating_vendors;
            CREATE TABLE IF NOT EXISTS rotating_counts (
                hour INTEGER NOT NULL, vendor TEXT NOT NULL, reports INTEGER NOT NULL, typical INTEGER NOT NULL,
                peak INTEGER NOT NULL, devices INTEGER NOT NULL, PRIMARY KEY (hour, vendor));
            CREATE TABLE IF NOT EXISTS rssi_hourly (
                mac TEXT NOT NULL, hour INTEGER NOT NULL, rssi INTEGER NOT NULL, samples INTEGER NOT NULL,
                PRIMARY KEY (mac, hour));
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(devices)")}
        if "rssi_median" not in columns:
            self.db.execute("ALTER TABLE devices ADD COLUMN rssi_median INTEGER")
            self.db.commit()

    def device_from_row(self, mac, kind, first_seen, reports, meta):
        meta = json.loads(meta)
        return {
            "mac": mac, "kind": kind, "first_seen": first_seen, "total_reports": reports, "start": None,
            "last": None, "reports": 0, "rssi_min": None, "rssi_max": None, "rssi_sum": 0, "last_rssi": None,
            "names": set(meta["names"]), "uuid16": set(meta["uuid16"]), "uuid128": set(meta["uuid128"]),
            "service_data": set(meta["service_data"]), "company_ids": set(meta["company_ids"]),
            "apple_types": set(meta["apple_types"]), "appearance": meta["appearance"], "tx_power": meta["tx_power"],
            "dirty": False, "recent": collections.deque(maxlen=RECENT_READINGS), "hour": None, "hour_rssi": [],
        }

    def restore_live(self):
        cutoff = int(time.time()) - SESSION_GAP_SECONDS
        rows = self.db.execute("""
            SELECT d.mac, d.kind, d.first_seen, d.reports, d.meta, s.start, s.end, s.reports,
                   s.rssi_min, s.rssi_max, s.rssi_sum, d.last_rssi
            FROM sessions s JOIN devices d ON d.mac = s.mac WHERE s.end >= ?""", (cutoff,)).fetchall()
        for row in rows:
            dev = self.device_from_row(*row[:5])
            dev.update(start=row[5], last=row[6], reports=row[7], rssi_min=row[8], rssi_max=row[9],
                       rssi_sum=row[10], last_rssi=row[11])
            if row[11] is not None:
                dev["recent"].append(row[11])
            self.live[dev["mac"]] = dev

    def new_session(self, mac, kind, now):
        row = self.db.execute("SELECT mac, kind, first_seen, reports, meta FROM devices WHERE mac = ?",
                              (mac,)).fetchone()
        if row:
            dev = self.device_from_row(*row)
        else:
            dev = self.device_from_row(mac, kind, now, 0, json.dumps({
                "names": [], "uuid16": [], "uuid128": [], "service_data": [], "company_ids": [],
                "apple_types": [], "appearance": None, "tx_power": None}))
        dev["start"] = now
        return dev

    def send_command(self, opcode, params):
        self.sock.send(struct.pack("<BHB", 1, opcode, len(params)) + params)
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                pkt = self.sock.recv(1024)
            except socket.timeout:
                continue
            if len(pkt) >= 7 and pkt[1] == 0x0E and struct.unpack("<H", pkt[4:6])[0] == opcode:
                return pkt[6]
            if len(pkt) >= 7 and pkt[1] == 0x0F and struct.unpack("<H", pkt[5:7])[0] == opcode:
                return pkt[3]
            self.handle_packet(pkt, int(time.time()))
        return None

    def set_scan(self, enabled):
        return self.send_command(SET_SCAN_ENABLE, bytes([1 if enabled else 0, 1]))

    def start_scan(self):
        self.set_scan(False)
        params = struct.pack("<BHHBB", 0, SCAN_INTERVAL, SCAN_WINDOW, 0, 0)
        status = self.send_command(SET_SCAN_PARAMS, params)
        if status != 0:
            raise RuntimeError("set scan params failed: %s" % status)
        self.enable_scan()

    def enable_scan(self):
        status = self.set_scan(True)
        if status != 0:
            raise RuntimeError("enable scan failed: %s" % status)

    def handle_packet(self, pkt, now):
        if len(pkt) < 6 or pkt[0] != 0x04 or pkt[1] != 0x3E or pkt[3] != 0x02:
            return
        offset = 5
        for _ in range(pkt[4]):
            if offset + 9 > len(pkt):
                return
            addr_type = pkt[offset + 1]
            addr = pkt[offset + 2:offset + 8]
            end = offset + 9 + pkt[offset + 8]
            if end >= len(pkt):
                return
            data = pkt[offset + 9:end]
            rssi = struct.unpack("b", pkt[end:end + 1])[0]
            offset = end + 1
            self.handle_report(addr_type, addr, data, rssi, now)

    def record_rotating(self, addr, ad, rssi, now, find_my=False):
        key = FIND_MY_VENDOR_KEY if find_my else vendor_key(ad)
        self.counter.record(addr, key, tuple(sorted(set(ad["apple_types"]))), rssi, now)

    def rotating_vendor_name(self, key):
        if not key:
            return NO_VENDOR
        if key == FIND_MY_VENDOR_KEY:
            return "Apple Find My"
        kind, hex_id = key.split(":")
        vendor = self.ids.company(hex_id) if kind == "c" else self.ids.uuid16(hex_id)
        return clean_vendor(vendor) if vendor else UNREGISTERED_VENDOR

    def handle_report(self, addr_type, addr, data, rssi, now):
        kind = address_kind(addr_type, addr)
        ad = parse_ad(data)
        if kind not in ("public", "random_static"):
            self.filtered["rotating"] += 1
            self.record_rotating(addr, ad, rssi, now)
            return
        if APPLE_FIND_MY in ad["apple_types"]:
            self.filtered["findmy"] += 1
            self.record_rotating(addr, ad, rssi, now, find_my=True)
            return

        mac = ":".join("%02X" % b for b in addr[::-1])
        dev = self.live.get(mac)
        if dev is None:
            if len(self.live) >= MAX_LIVE_DEVICES:
                return
            dev = self.live[mac] = self.new_session(mac, kind, now)
        dev["last"] = now
        dev["reports"] += 1
        dev["total_reports"] += 1
        dev["rssi_min"] = rssi if dev["rssi_min"] is None else min(dev["rssi_min"], rssi)
        dev["rssi_max"] = rssi if dev["rssi_max"] is None else max(dev["rssi_max"], rssi)
        dev["rssi_sum"] += rssi
        dev["last_rssi"] = rssi
        dev["recent"].append(rssi)
        hour = now // 3600 * 3600
        if dev["hour"] != hour:
            dev["hour"] = hour
            dev["hour_rssi"] = []
        if len(dev["hour_rssi"]) < MAX_HOUR_READINGS:
            dev["hour_rssi"].append(rssi)
        dev["dirty"] = True
        for key in ("names", "uuid16", "uuid128", "service_data", "company_ids", "apple_types"):
            for value in ad[key]:
                if len(dev[key]) < MAX_META_ITEMS:
                    dev[key].add(value)
        if ad["appearance"] is not None:
            dev["appearance"] = ad["appearance"]
        if ad["tx_power"] is not None:
            dev["tx_power"] = ad["tx_power"]

    def write_device(self, dev):
        category, label, confidence, evidence = classify(dev, self.ids)
        self.db.execute("""
            INSERT OR REPLACE INTO devices
            (mac, kind, first_seen, last_seen, reports, meta, category, label, confidence, evidence, last_rssi,
             rssi_median)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (dev["mac"], dev["kind"], dev["first_seen"], dev["last"], dev["total_reports"],
                         meta_to_json(dev), category, label, confidence, "; ".join(evidence), dev["last_rssi"],
                         median(dev["recent"]) if dev["recent"] else dev["last_rssi"]))
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (dev["mac"], dev["start"], dev["last"], dev["reports"], dev["rssi_min"], dev["rssi_max"],
                         dev["rssi_sum"]))
        if dev["hour_rssi"]:
            self.db.execute("INSERT OR REPLACE INTO rssi_hourly VALUES (?, ?, ?, ?)",
                            (dev["mac"], dev["hour"], median(dev["hour_rssi"]), len(dev["hour_rssi"])))

    def flush(self, now):
        expired = [mac for mac, dev in self.live.items() if now - dev["last"] > SESSION_GAP_SECONDS]
        hour = now // 3600 * 3600
        with self.db:
            for dev in self.live.values():
                if dev["dirty"]:
                    self.write_device(dev)
                    dev["dirty"] = False
            if self.filtered["rotating"] or self.filtered["findmy"]:
                self.db.execute("""
                    INSERT INTO filtered VALUES (?, ?, ?) ON CONFLICT (hour) DO UPDATE SET
                    rotating = rotating + excluded.rotating, findmy = findmy + excluded.findmy""",
                                (hour, self.filtered["rotating"], self.filtered["findmy"]))
                self.filtered = {"rotating": 0, "findmy": 0}
            self.counter.link(now)
            for count_hour in (hour - 3600, hour):
                for vendor, (typical, peak, devices) in self.counter.hour_counts(count_hour, now).items():
                    self.db.execute("""
                        INSERT INTO rotating_counts VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (hour, vendor) DO UPDATE SET
                        reports = reports + excluded.reports, typical = excluded.typical,
                        peak = excluded.peak, devices = excluded.devices""",
                                    (count_hour, vendor, self.counter.take_reports(count_hour, vendor),
                                     typical, peak, devices))
        self.counter.prune(now)
        for mac in expired:
            del self.live[mac]

    def prune(self, now):
        cutoff = now - RETENTION_SECONDS
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE end < ?", (cutoff,))
            self.db.execute("DELETE FROM devices WHERE last_seen < ?", (cutoff,))
            self.db.execute("DELETE FROM filtered WHERE hour < ?", (cutoff,))
            self.db.execute("DELETE FROM rotating_counts WHERE hour < ?", (cutoff,))
            self.db.execute("DELETE FROM rssi_hourly WHERE hour < ?", (now - MOBILITY_WINDOW_SECONDS,))

    def refresh_mobility(self, now):
        window_start = now - MOBILITY_WINDOW_SECONDS
        since = self.db.execute("SELECT MIN(first_seen) FROM devices").fetchone()[0]
        self.tracked_hours = (now - max(since, window_start)) // 3600 if since else 0
        hourly = {}
        for mac, rssi in self.db.execute("SELECT mac, rssi FROM rssi_hourly WHERE hour >= ?", (window_start,)):
            hourly.setdefault(mac, []).append(rssi)
        visits = dict(self.db.execute("SELECT mac, COUNT(*) FROM sessions WHERE start >= ? GROUP BY mac",
                                      (window_start,)).fetchall())
        mobility = {}
        for mac, category, label in self.db.execute("SELECT mac, category, label FROM devices").fetchall():
            mobility[mac] = classify_mobility(category, label, hourly.get(mac, []), self.tracked_hours,
                                              visits.get(mac, 0), mac in self.live)
        self.mobility = mobility

    def build_rotating_vendors(self, first_hour):
        rows = self.db.execute(
            "SELECT hour, vendor, reports, typical, peak, devices FROM rotating_counts WHERE hour >= ?",
            (first_hour,)).fetchall()
        grouped = {}
        for hour, vendor, reports, typical, peak, devices in rows:
            name = self.rotating_vendor_name(vendor)
            entry = grouped.setdefault(name, {"sightings": 0, "hours": {}})
            entry["sightings"] += reports
            totals = entry["hours"].setdefault(hour, [0, 0, 0])
            totals[0] += typical
            totals[1] += peak
            totals[2] += devices
        vendors = []
        for name, entry in grouped.items():
            hours = list(entry["hours"].values())
            vendors.append({"name": name, "sightings": entry["sightings"], "hours_heard": len(hours),
                            "typical": median([h[0] for h in hours]), "peak": max(h[1] for h in hours),
                            "devices_per_hour": median([h[2] for h in hours])})
        vendors.sort(key=lambda v: (v["name"] in (NO_VENDOR, UNREGISTERED_VENDOR), -v["hours_heard"], -v["sightings"]))
        return {"hours": len({row[0] for row in rows}), "vendors": vendors}

    def build_data(self, now):
        day_ago = now - 86400
        first_hour = now // 3600 * 3600 - 23 * 3600
        sessions = self.db.execute(
            "SELECT mac, start, end FROM sessions WHERE end >= ? ORDER BY start", (first_hour,)).fetchall()
        labels = {}
        counts = dict(self.db.execute("SELECT mac, COUNT(*) FROM sessions GROUP BY mac").fetchall())
        devices = []
        rows = self.db.execute("""
            SELECT mac, kind, first_seen, last_seen, reports, meta, category, label, confidence, evidence,
                   COALESCE(rssi_median, last_rssi)
            FROM devices ORDER BY last_seen DESC""").fetchall()
        for mac, kind, first, last, reports, meta, category, label, confidence, evidence, rssi in rows:
            labels[mac] = label
            if len(devices) < MAX_JSON_DEVICES or mac in self.live:
                names = json.loads(meta)["names"]
                mobility = self.mobility.get(mac) or classify_mobility(
                    category, label, [], self.tracked_hours, counts.get(mac, 0), mac in self.live)
                devices.append({"mac": mac, "kind": kind, "first_seen": first, "last_seen": last, "reports": reports,
                                "name": names[0] if names else None, "category": category, "label": label,
                                "confidence": confidence, "evidence": evidence, "rssi": rssi,
                                "band": distance_band(rssi), "visits": counts.get(mac, 0), "here": mac in self.live,
                                "trend": signal_trend(self.live[mac]["recent"]) if mac in self.live else None,
                                "mobility": mobility[0], "mobility_label": mobility[1], "mobility_basis": mobility[2]})

        hourly = []
        for i in range(24):
            start = first_hour + i * 3600
            macs = {s[0] for s in sessions if s[1] < start + 3600 and s[2] >= start}
            hourly.append({"t": start, "devices": len(macs)})

        by_mac = {}
        for mac, start, end in sessions:
            by_mac.setdefault(mac, []).append([start, now if mac in self.live and end == self.live[mac]["last"] else end])
        ranked = sorted(by_mac.items(), key=lambda kv: (kv[0] not in self.live, -sum(e - s for s, e in kv[1])))
        timeline = [{"mac": mac, "label": labels.get(mac, mac), "here": mac in self.live, "sessions": spans}
                    for mac, spans in ranked[:MAX_TIMELINE_DEVICES]]

        events = []
        for mac, start, end in sessions:
            if start >= day_ago:
                events.append({"t": start, "type": "arrived", "mac": mac, "label": labels.get(mac, mac)})
            if end >= day_ago and mac not in self.live and end + SESSION_GAP_SECONDS <= now:
                events.append({"t": end, "type": "left", "mac": mac, "label": labels.get(mac, mac)})
        events.sort(key=lambda e: -e["t"])

        rotating, findmy = self.db.execute(
            "SELECT COALESCE(SUM(rotating), 0), COALESCE(SUM(findmy), 0) FROM filtered WHERE hour >= ?",
            (day_ago,)).fetchone()
        known, since, new = self.db.execute(
            "SELECT COUNT(*), MIN(first_seen), COALESCE(SUM(first_seen >= ?), 0) FROM devices", (day_ago,)).fetchone()
        arrivals = self.db.execute("SELECT COUNT(*) FROM sessions WHERE start >= ?", (day_ago,)).fetchone()[0]
        return {
            "generated": now, "paused": self.paused, "tracking_since": since, "session_gap": SESSION_GAP_SECONDS,
            "summary": {"here": len(self.live), "known": known, "new_24h": new, "arrivals_24h": arrivals,
                        "filtered_rotating_24h": rotating, "filtered_findmy_24h": findmy},
            "hourly": hourly, "timeline": timeline, "events": events[:MAX_EVENTS], "devices": devices,
            "rotating": self.build_rotating_vendors(first_hour), "uptime": read_uptime(),
        }

    def write_json(self, now):
        tmp = DATA_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.build_data(now), f, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, DATA_JSON)

    def check_memory(self):
        mb = available_mb()
        if mb is None:
            return
        if not self.paused and mb < MIN_AVAILABLE_MB:
            print("pausing scan: %d MB available" % mb, flush=True)
            self.set_scan(False)
            self.paused = True
        elif self.paused and mb >= RESUME_AVAILABLE_MB:
            print("resuming scan: %d MB available" % mb, flush=True)
            self.enable_scan()
            self.paused = False

    def run(self):
        self.sock = open_hci()
        try:
            self.start_scan()
            print("scanning", flush=True)
            now = time.time()
            next_restart = now + SCAN_RESTART_SECONDS
            next_flush = now + 5
            next_memory = now + MEMORY_CHECK_SECONDS
            next_prune = now + PRUNE_SECONDS
            next_mobility = now
            while not stop_requested:
                try:
                    pkt = self.sock.recv(1024)
                except socket.timeout:
                    pkt = None
                if pkt:
                    self.handle_packet(pkt, int(time.time()))
                now = time.time()
                if now >= next_memory:
                    next_memory = now + MEMORY_CHECK_SECONDS
                    self.check_memory()
                if now >= next_restart:
                    next_restart = now + SCAN_RESTART_SECONDS
                    if not self.paused:
                        self.set_scan(False)
                        self.enable_scan()
                if now >= next_flush:
                    next_flush = now + FLUSH_SECONDS
                    self.flush(int(now))
                    if now >= next_mobility:
                        next_mobility = now + MOBILITY_REFRESH_SECONDS
                        self.refresh_mobility(int(now))
                    self.write_json(int(now))
                if now >= next_prune:
                    next_prune = now + PRUNE_SECONDS
                    self.prune(int(now))
        finally:
            self.set_scan(False)
            self.flush(int(time.time()))
            self.sock.close()
            self.db.close()


def main():
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    wait_for_clock()
    if stop_requested:
        return 0
    Tracker().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
