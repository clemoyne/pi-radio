#!/usr/bin/env python3
"""Passive 2.4 GHz Wi-Fi presence tracker. Reads frame headers and management data only."""
import collections
import functools
import http.server
import json
import math
import os
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

# The defaults are sdrpi's; porch_pi's unit sets these, since its network also runs on an rtl8192cu dongle
STATE_DB = os.environ.get("WIFITRACKER_DB", "/mnt/data/wifitracker/tracker.db")
IDS_DB = os.environ.get("WIFITRACKER_IDS", "/mnt/data/wifitracker/app/ids.db")
DATA_JSON = "/run/wifitracker/data.json"
DONGLE_MAC = os.environ.get("WIFITRACKER_MAC", "").lower()  # when set, only this dongle is used
DATA_PORT = int(os.environ.get("WIFITRACKER_PORT", "0"))  # when set, data.json is served here for sdrpi's page
DRIVER = "rtl8192cu"
IW = "/usr/sbin/iw"
IP = "/usr/sbin/ip"

CHANNELS = [1, 6, 11]
DWELL_SECONDS = 2.0
FRAME_TIMEOUT_SECONDS = 90  # the dongle can stop receiving without any error; exiting lets systemd restart it
SESSION_GAP_SECONDS = 900
FLUSH_SECONDS = 60
PRUNE_SECONDS = 86400
RETENTION_SECONDS = 90 * 86400
MAX_LIVE_DEVICES = 3000
MAX_JSON_DEVICES = 500
MAX_RANDOM_ADDRESSES = 5000
RANDOM_WINDOW_SECONDS = 300
RANDOM_KEEP_SECONDS = 7200
RECENT_READINGS = 15

# Log-distance estimate; walls make this rough, so the page shows it as "likely".
REFERENCE_DBM_AT_1M = -35
PATH_LOSS_EXPONENT = 3.0
BANDS = [(-55, "Very close (under 5 m)"), (-70, "In or near the house (5-15 m)"),
         (-82, "Next door (15-40 m)"), (-200, "Street or farther (40 m+)")]

MGMT, DATA = 0, 2
BEACON, PROBE_RESP, PROBE_REQ, ASSOC_REQ, REASSOC_REQ = 8, 5, 4, 0, 2
GENERATIONS = ["Unknown", "802.11b", "802.11g", "Wi-Fi 4 (n)", "Wi-Fi 5 (ac)", "Wi-Fi 6 (ax)", "Wi-Fi 7 (be)"]

AP_SSID_RULES = [
    (r"carplay|android[ _-]?auto", "Vehicle", "Car stereo"),
    (r"dash[ _-]?cam|(car|type[ _-]?s)[ _-]?dvr", "Vehicle", "Dashcam"),  # plain "DVR" is usually a home security recorder
    (r"tesla|audi(?!o)|bmw|\bford\b|fordpass|lincoln|onstar|chevy|chevrolet|\bgmc\b|buick|cadillac|jeep|dodge|chrysler|"
     r"volkswagen|\bvw\b|toyota|lexus|honda|acura|hyundai|\bkia\b|nissan|infiniti|mercedes|subaru|mazda|volvo|porsche|"
     r"mitsubishi|rivian|polestar|equinox|silverado|escalade|f-?150|wrangler|rav4|camry|corolla|\bcr-?v\b|tucson|"
     r"sportage|telluride|altima|tiguan|jetta|passat|crosstrek|^car[ _-]?\d+$|\bcar\b.*(wi-?fi|hotspot)",
     "Vehicle", "Vehicle hotspot"),
    (r"iphone|ipad|galaxy|pixel|androidap|android|oneplus|motorola|moto |xiaomi|redmi|hotspot",
     "Phone hotspot", "Phone or tablet hotspot"),
    (r"^direct-|hp-print|epson|canon|brother", "Printer", "Printer"),
    (r"^esp[_-]|tasmota|shelly|wyze|\bring\b|^ring-|govee|tuya|smartlife|sonoff|nest|kasa|tp-link_smart",
     "Smart home", "Smart device setup network"),
    (r"chromecast|roku|fire ?tv|samsung.*tv|\[tv\]|\blg\b.*tv", "TV or streamer", "TV or streaming device"),
    (r"gopro|dji", "Camera", "Camera or drone"),
    (r"rogers|bell|telus|shaw|hitron|teksavvy|xfinity|ignite|virgin|fido|cogeco|altice|eero|orbi|deco|linksys|"
     r"netgear|asus|ubnt|unifi|tp-link|d-link|dlink", "Router", "Router or access point"),
]
CLIENT_VENDOR_RULES = [
    (r"apple", "Phone or computer", "Apple device"),
    (r"samsung", "Phone or TV", "Samsung device"),
    (r"google|nest", "Smart home", "Google or Nest device"),
    (r"amazon", "Smart home", "Amazon device"),
    (r"espressif|tuya|shelly|wyze|sonoff|tp-link.*smart|\bgree\b|azurewave|chongqing fugui|hui zhou gaoshengda",
     "Smart home", "Smart home or IoT device"),
    (r"sonos|bose|harman", "Audio", "Speaker or audio device"),
    (r"roku|vizio|\blg\b|hisense|tcl|sony", "TV or streamer", "TV or streaming device"),
    (r"hewlett|\bhp\b|epson|canon|brother", "Printer", "Printer"),
    (r"intel|realtek|liteon|dell|lenovo|microsoft|asustek", "Computer", "Computer"),
    (r"nintendo|valve", "Game console", "Game console"),
    (r"raspberry", "Computer", "Raspberry Pi"),
    (r"tesla|bayerische|\bbmw\b|volkswagen|toyota|honda|hyundai|\bkia\b|nissan|mercedes|ford motor|denso|harman becker|"
     r"continental automotive|lg innotek automotive", "Vehicle", "Vehicle or car system"),
    (r"motorola|oneplus|xiaomi|huawei|oppo|vivo|zte|nokia", "Phone or computer", "Phone or tablet"),
]

stop_requested = False


def on_signal(_signum, _frame):
    global stop_requested
    stop_requested = True


def route_interfaces():
    with open("/proc/net/route") as routes:
        return {line.split()[0] for line in list(routes)[1:] if line.split()[1] == "00000000"}


def find_dongle():
    carrying = route_interfaces()
    for path in Path("/sys/class/net").glob("wlan*"):
        driver = path / "device" / "driver"
        if not driver.exists() or os.path.basename(os.readlink(driver)) != DRIVER:
            continue
        if DONGLE_MAC and (path / "address").read_text().strip().lower() != DONGLE_MAC:
            continue
        if path.name in carrying:
            raise RuntimeError("%s carries the network connection; not putting it in monitor mode" % path.name)
        return path.name
    raise RuntimeError("no %s Wi-Fi dongle%s found" % (DRIVER, " with address " + DONGLE_MAC if DONGLE_MAC else ""))


class DataHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = open(DATA_JSON, "rb").read() if self.path == "/data.json" else None
        except FileNotFoundError:
            body = None
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body or b"{}")

    def log_message(self, *_args):
        pass


def serve_data():
    server = http.server.ThreadingHTTPServer(("", DATA_PORT), DataHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def start_monitor(iface):
    subprocess.run([IP, "link", "set", iface, "down"], check=True)
    subprocess.run([IW, "dev", iface, "set", "type", "monitor"], check=True)
    subprocess.run([IP, "link", "set", iface, "up"], check=True)


def open_capture(iface):
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    sock.bind((iface, 0))
    sock.settimeout(0.05)
    return sock


def parse_radiotap(frame):
    """Returns (header length, dBm, flags) or None."""
    if len(frame) < 8:
        return None
    length, present = struct.unpack("<HI", frame[2:8])
    if length > len(frame):
        return None
    offset, word = 8, present
    while word & (1 << 31):
        word = struct.unpack("<I", frame[offset:offset + 4])[0]
        offset += 4
    dbm, flags = None, 0
    for bit, (size, align) in enumerate([(8, 8), (1, 1), (1, 1), (4, 2), (2, 2), (1, 1)]):
        if present & (1 << bit):
            offset = (offset + align - 1) // align * align
            if offset + size > length:
                return None
            if bit == 1:
                flags = frame[offset]
            elif bit == 5:
                dbm = struct.unpack("b", frame[offset:offset + 1])[0]
            offset += size
    return length, dbm, flags


def rsn_security(value):
    try:
        pairwise = struct.unpack("<H", value[6:8])[0]
        at = 8 + 4 * pairwise
        akms = struct.unpack("<H", value[at:at + 2])[0]
        types = {value[at + 2 + 4 * i + 3] for i in range(akms)}
    except (struct.error, IndexError):
        return "WPA2"
    if 8 in types or 24 in types:
        return "WPA2/WPA3" if 2 in types else "WPA3"
    if 18 in types:
        return "Enhanced Open"
    if 1 in types or 5 in types:
        return "Enterprise"
    return "WPA2"


def parse_ies(data, want_ssid):
    info = {"ssid": None, "channel": None, "generation": 0, "security": None}
    rates = []
    ht = vht = he = eht = False
    i = 0
    while i + 2 <= len(data):
        tag, size = data[i], data[i + 1]
        value = data[i + 2:i + 2 + size]
        if len(value) < size:
            break
        i += 2 + size
        if tag == 0 and want_ssid:
            name = "".join(c for c in value.decode("utf-8", "replace") if c.isprintable()).strip()
            info["ssid"] = name[:32]
        elif tag in (1, 50):
            rates += [(b & 0x7F) / 2 for b in value]
        elif tag == 3 and size == 1:
            info["channel"] = value[0]
        elif tag == 45:
            ht = True
        elif tag == 191:
            vht = True
        elif tag == 255 and size:
            he = he or value[0] == 35
            eht = eht or value[0] == 108
        elif tag == 48:
            info["security"] = rsn_security(value)
        elif tag == 221 and value[:4] == b"\x00\x50\xf2\x01" and info["security"] is None:
            info["security"] = "WPA"
    if eht:
        info["generation"] = 6
    elif he:
        info["generation"] = 5
    elif vht:
        info["generation"] = 4
    elif ht:
        info["generation"] = 3
    elif rates:
        info["generation"] = 2 if max(rates) > 11 else 1
    return info


def band_for(rssi):
    if rssi is None:
        return None
    for floor, name in BANDS:
        if rssi > floor:
            return name
    return BANDS[-1][1]


def likely_metres(rssi):
    if rssi is None:
        return None
    return max(1, round(10 ** ((REFERENCE_DBM_AT_1M - rssi) / (10 * PATH_LOSS_EXPONENT))))


def median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


class Ids:
    def __init__(self, path):
        self.db = sqlite3.connect("file:%s?mode=ro&immutable=1" % path, uri=True) if os.path.exists(path) else None

    @functools.lru_cache(maxsize=2048)
    def oui(self, mac):
        if self.db is None:
            return None
        row = self.db.execute("SELECT name FROM oui WHERE prefix = ?", (mac[:8],)).fetchone()
        return row[0] if row else None


def clean_vendor(vendor):
    return re.sub(r"[,.]?\s+(Inc|Corp|Corporation|Co|Ltd|LLC|GmbH|Limited)\.?.*$", "", vendor).strip()


def classify(dev, vendor):
    if dev["role"] == "ap":
        ssid = dev["ssid"] or ""
        for pattern, category, label in AP_SSID_RULES:
            if ssid and re.search(pattern, ssid, re.I):
                return category, label, "medium", "network name"
        if vendor:
            return "Router", "%s access point" % clean_vendor(vendor), "low", "address prefix %s" % dev["mac"][:8]
        return "Router", "Access point", "low", "broadcasts as a network"
    if vendor:
        for pattern, category, label in CLIENT_VENDOR_RULES:
            if re.search(pattern, vendor, re.I):
                return category, label, "medium", "address prefix %s (%s)" % (dev["mac"][:8], vendor)
        return "Other", "%s device" % clean_vendor(vendor), "low", "address prefix %s (%s)" % (dev["mac"][:8], vendor)
    return "Unidentified", "Unidentified", "none", "unknown manufacturer"


class RandomCounter:
    """Counts devices using randomized addresses. Addresses never leave this class or memory."""

    def __init__(self):
        self.windows = {}

    def record(self, addr, generation, rssi, now):
        window = now // RANDOM_WINDOW_SECONDS * RANDOM_WINDOW_SECONDS
        seen = self.windows.setdefault(window, {}).setdefault(generation, {})
        if addr in seen or sum(map(len, self.windows[window].values())) < MAX_RANDOM_ADDRESSES:
            seen[addr] = max(seen.get(addr, -127), rssi)

    def current(self, now):
        window = now // RANDOM_WINDOW_SECONDS * RANDOM_WINDOW_SECONDS
        return sum(map(len, self.windows.get(window, {}).values()))

    def hour_counts(self, hour):
        """Returns {(generation, band): (typical, peak)} over the windows in one hour."""
        windows = [w for w in self.windows if hour <= w < hour + 3600]
        per_key = collections.defaultdict(dict)
        for window in windows:
            tally = collections.Counter()
            for generation, seen in self.windows[window].items():
                for rssi in seen.values():
                    tally[(generation, band_for(rssi))] += 1
            for key, count in tally.items():
                per_key[key][window] = count
        return {key: (median([counts.get(w, 0) for w in windows]), max(counts.values()))
                for key, counts in per_key.items()}

    def prune(self, now):
        for window in [w for w in self.windows if w < now - RANDOM_KEEP_SECONDS]:
            del self.windows[window]


class Tracker:
    def __init__(self):
        self.ids = Ids(IDS_DB)
        self.db = sqlite3.connect(STATE_DB)
        self.live = {}
        self.random = RandomCounter()
        self.init_db()
        self.restore_live()

    def init_db(self):
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            CREATE TABLE IF NOT EXISTS devices (
                mac TEXT PRIMARY KEY, role TEXT NOT NULL, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                frames INTEGER NOT NULL, ssid TEXT, channel INTEGER, generation INTEGER NOT NULL, security TEXT,
                vendor TEXT, category TEXT, label TEXT, confidence TEXT, evidence TEXT, rssi INTEGER);
            CREATE TABLE IF NOT EXISTS sessions (
                mac TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, frames INTEGER NOT NULL,
                PRIMARY KEY (mac, start));
            CREATE INDEX IF NOT EXISTS sessions_end ON sessions (end);
            CREATE TABLE IF NOT EXISTS random_counts (
                hour INTEGER NOT NULL, generation INTEGER NOT NULL, band TEXT NOT NULL, typical INTEGER NOT NULL,
                peak INTEGER NOT NULL, PRIMARY KEY (hour, generation, band));
        """)

    def blank(self, mac, role, now):
        row = self.db.execute("SELECT role, first_seen, frames, ssid, channel, generation, security FROM devices "
                              "WHERE mac = ?", (mac,)).fetchone()
        dev = {"mac": mac, "role": role, "first_seen": now, "total": 0, "ssid": None, "channel": None, "generation": 0,
               "security": None, "start": now, "last": now, "frames": 0, "rssi": None, "dirty": False,
               "recent": collections.deque(maxlen=RECENT_READINGS)}
        if row:
            dev.update(role=row[0], first_seen=row[1], total=row[2], ssid=row[3], channel=row[4], generation=row[5],
                       security=row[6])
        return dev

    def restore_live(self):
        cutoff = int(time.time()) - SESSION_GAP_SECONDS
        for mac, start, end, frames, rssi, role in self.db.execute(
                "SELECT s.mac, s.start, s.end, s.frames, d.rssi, d.role FROM sessions s JOIN devices d ON d.mac = s.mac "
                "WHERE s.end >= ?", (cutoff,)).fetchall():
            dev = self.blank(mac, role, start)
            dev.update(start=start, last=end, frames=frames, rssi=rssi)
            if rssi is not None:
                dev["recent"].append(rssi)
            self.live[mac] = dev

    def note(self, addr, role, rssi, info, now):
        if addr[0] & 1:
            return
        generation = info["generation"] if info else 0
        if role == "client" and addr[0] & 2:
            self.random.record(bytes(addr), generation, rssi, now)
            return
        mac = ":".join("%02X" % b for b in addr)
        dev = self.live.get(mac)
        if dev is None:
            if len(self.live) >= MAX_LIVE_DEVICES:
                return
            dev = self.live[mac] = self.blank(mac, role, now)
        if role == "ap":
            dev["role"] = "ap"
        dev.update(last=now, rssi=rssi, dirty=True)
        dev["frames"] += 1
        dev["total"] += 1
        dev["recent"].append(rssi)
        if info:
            dev["generation"] = max(dev["generation"], generation)
            if role == "ap":
                if info["ssid"]:
                    dev["ssid"] = info["ssid"]
                if info["channel"]:
                    dev["channel"] = info["channel"]
                dev["security"] = info["security"] or dev["security"] or "Open or WEP"

    def handle(self, frame, now):
        parsed = parse_radiotap(frame)
        if parsed is None or parsed[1] is None or parsed[2] & 0x40:
            return
        length, rssi, flags = parsed
        dot11 = frame[length:-4] if flags & 0x10 else frame[length:]
        if len(dot11) < 24:
            return
        control = struct.unpack("<H", dot11[:2])[0]
        kind, subtype = (control >> 2) & 3, (control >> 4) & 15
        sender = dot11[10:16]
        if kind == MGMT:
            if subtype in (BEACON, PROBE_RESP):
                info = parse_ies(dot11[36:], want_ssid=True)
                privacy = len(dot11) >= 36 and struct.unpack("<H", dot11[34:36])[0] & 0x10
                if info["security"] is None:
                    info["security"] = "WEP" if privacy else "Open"
                self.note(sender, "ap", rssi, info, now)
            elif subtype == PROBE_REQ:
                # Probes name networks a device has joined before, so the SSID is never read.
                self.note(sender, "client", rssi, parse_ies(dot11[24:], want_ssid=False), now)
            elif subtype in (ASSOC_REQ, REASSOC_REQ):
                skip = 28 if subtype == ASSOC_REQ else 34
                self.note(sender, "client", rssi, parse_ies(dot11[skip:], want_ssid=False), now)
        elif kind == DATA:
            to_ds, from_ds = (control >> 8) & 1, (control >> 9) & 1
            if to_ds and not from_ds:
                self.note(sender, "client", rssi, None, now)
            elif from_ds and not to_ds:
                self.note(sender, "ap", rssi, None, now)

    def write(self, dev):
        vendor = self.ids.oui(dev["mac"])
        category, label, confidence, evidence = classify(dev, vendor)
        rssi = median(dev["recent"]) if dev["recent"] else dev["rssi"]
        self.db.execute("INSERT OR REPLACE INTO devices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (dev["mac"], dev["role"], dev["first_seen"], dev["last"], dev["total"], dev["ssid"],
                         dev["channel"], dev["generation"], dev["security"], vendor, category, label, confidence,
                         evidence, rssi))
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                        (dev["mac"], dev["start"], dev["last"], dev["frames"]))

    def flush(self, now):
        hour = now // 3600 * 3600
        with self.db:
            for dev in self.live.values():
                if dev["dirty"]:
                    self.write(dev)
                    dev["dirty"] = False
            for count_hour in (hour - 3600, hour):
                for (generation, band), (typical, peak) in self.random.hour_counts(count_hour).items():
                    self.db.execute("INSERT OR REPLACE INTO random_counts VALUES (?,?,?,?,?)",
                                    (count_hour, generation, band, typical, peak))
        self.random.prune(now)
        for mac in [m for m, d in self.live.items() if now - d["last"] > SESSION_GAP_SECONDS]:
            del self.live[mac]

    def prune(self, now):
        cutoff = now - RETENTION_SECONDS
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE end < ?", (cutoff,))
            self.db.execute("DELETE FROM devices WHERE last_seen < ?", (cutoff,))
            self.db.execute("DELETE FROM random_counts WHERE hour < ?", (cutoff,))

    def build_data(self, now):
        day_ago = now - 86400
        first_hour = now // 3600 * 3600 - 23 * 3600
        visits = dict(self.db.execute("SELECT mac, COUNT(*) FROM sessions GROUP BY mac").fetchall())
        sessions = self.db.execute("SELECT mac, start, end FROM sessions WHERE end >= ?", (first_hour,)).fetchall()
        devices = []
        tallies = {"wifi_type": collections.Counter(), "device_type": collections.Counter(),
                   "distance": collections.Counter(), "security": collections.Counter()}
        for row in self.db.execute("SELECT mac, role, first_seen, last_seen, frames, ssid, channel, generation, security, "
                                   "vendor, category, label, confidence, evidence, rssi FROM devices "
                                   "ORDER BY last_seen DESC").fetchall():
            mac, role, first, last, frames, ssid, channel, generation, security, vendor, category, label, confidence, \
                evidence, rssi = row
            band = band_for(rssi)
            if last >= day_ago:
                tallies["wifi_type"][GENERATIONS[generation]] += 1
                tallies["device_type"][category] += 1
                tallies["distance"][band] += 1
                if role == "ap":
                    tallies["security"][security or "Unknown"] += 1
            if len(devices) < MAX_JSON_DEVICES or mac in self.live:
                devices.append({"mac": mac, "role": role, "first_seen": first, "last_seen": last, "frames": frames,
                                "ssid": ssid, "channel": channel, "wifi_type": GENERATIONS[generation],
                                "security": security, "vendor": clean_vendor(vendor) if vendor else None,
                                "category": category, "label": label, "confidence": confidence, "evidence": evidence,
                                "rssi": rssi, "band": band, "metres": likely_metres(rssi), "here": mac in self.live,
                                "visits": visits.get(mac, 0)})
        hourly = []
        for i in range(24):
            start = first_hour + i * 3600
            hourly.append({"t": start, "devices": len({s[0] for s in sessions if s[1] < start + 3600 and s[2] >= start})})
        grouped = collections.defaultdict(lambda: {"typical": 0, "peak": 0})
        hours = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
        for hour, generation, band, typical, peak in self.db.execute(
                "SELECT hour, generation, band, typical, peak FROM random_counts WHERE hour >= ?", (first_hour,)):
            hours[(GENERATIONS[generation], band)][hour] = [typical, peak]
        for key, by_hour in hours.items():
            grouped[key] = {"typical": median([v[0] for v in by_hour.values()]), "peak": max(v[1] for v in by_hour.values())}
        random_rows = [{"wifi_type": k[0], "band": k[1], **v} for k, v in grouped.items()]
        random_rows.sort(key=lambda r: -r["peak"])
        new = self.db.execute("SELECT COUNT(*) FROM devices WHERE first_seen >= ?", (day_ago,)).fetchone()[0]
        known, since = self.db.execute("SELECT COUNT(*), MIN(first_seen) FROM devices").fetchone()
        return {
            "generated": now, "tracking_since": since, "bands": [name for _, name in BANDS], "order": GENERATIONS,
            "summary": {"here": len(self.live), "access_points": sum(d["role"] == "ap" for d in self.live.values()),
                        "clients": sum(d["role"] == "client" for d in self.live.values()), "known": known, "new_24h": new,
                        "random_now": self.random.current(now)},
            "hourly": hourly, "tallies": {k: dict(v) for k, v in tallies.items()}, "devices": devices, "random": random_rows,
        }

    def write_json(self, now):
        tmp = DATA_JSON + ".tmp"
        with open(tmp, "w") as out:
            json.dump(self.build_data(now), out, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, DATA_JSON)

    def run(self):
        iface = find_dongle()
        start_monitor(iface)
        sock = open_capture(iface)
        print("capturing on", iface, flush=True)
        position, next_hop, next_flush = 0, 0.0, time.time() + 5
        next_prune = time.time() + PRUNE_SECONDS
        last_frame = time.time()
        try:
            while not stop_requested:
                now = time.time()
                if now >= next_hop:
                    next_hop = now + DWELL_SECONDS
                    subprocess.run([IW, "dev", iface, "set", "channel", str(CHANNELS[position])], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    position = (position + 1) % len(CHANNELS)
                try:
                    self.handle(sock.recv(2048), int(now))
                    last_frame = now
                except socket.timeout:
                    if now - last_frame >= FRAME_TIMEOUT_SECONDS:
                        print("no frames for %d s, restarting" % FRAME_TIMEOUT_SECONDS, flush=True)
                        return
                if now >= next_flush:
                    next_flush = now + FLUSH_SECONDS
                    self.flush(int(now))
                    self.write_json(int(now))
                if now >= next_prune:
                    next_prune = now + PRUNE_SECONDS
                    self.prune(int(now))
        finally:
            self.flush(int(time.time()))
            sock.close()
            self.db.close()


def main():
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    if DATA_PORT:
        serve_data()
    Tracker().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
