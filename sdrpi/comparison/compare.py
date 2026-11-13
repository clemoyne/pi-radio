#!/usr/bin/env python3
"""Compares a porch day with an attic day, using only the minutes that are clean on both. Reads data, never changes it."""
import collections
import datetime as dt
import json
import re
import sqlite3
import statistics
import subprocess
import sys
from pathlib import Path

OUT = Path("/mnt/data/comparison")
SCANNER_LOG = "/mnt/data/logs/scanner.log"
WIFI_DB = "/mnt/data/wifitracker/tracker.db"
BT_DB = "/mnt/data/bletracker/tracker.db"
FIRST_MINUTE, LAST_MINUTE = 5 * 60, 17 * 60
PORCH_DAY, ATTIC_DAY = "2026-11-09", "2026-11-10"
WIFI_FROM = {PORCH_DAY: 9 * 60 + 24}  # the dongle was dead or on other settings before this
BT_FROM = {PORCH_DAY: 8 * 60 + 32}  # the old 10% listening level before this
CAPTURE_GAP_SECONDS = 180  # auto-recording pauses longer than this mean the radio was busy or failing
WIFI_SILENCE_SECONDS = 90
STRONG_DB = 15
CAPTURE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) capture ([\d.]+) \S+ (.*) peak (-?\d+) dB")
JOURNAL = re.compile(r"^([\d.]+) \S+ systemd\[1\]: ")
CHANNELS = {127.575: "Pearson Departure", 128.8: "Pearson Departure", 124.475: "Pearson Arrival", 118.35: "Pearson Tower",
            118.2: "Billy Bishop Tower", 133.4: "Toronto Terminal"}


def epoch(day, minute=0):
    return dt.datetime.strptime(day, "%Y-%m-%d").timestamp() + minute * 60


def window():
    return set(range(FIRST_MINUTE, LAST_MINUTE))


def minute_of(day, stamp):
    return int((stamp - epoch(day)) // 60)


def remove_interval(minutes, day, start, end):
    first, last = minute_of(day, start), minute_of(day, end)
    minutes.difference_update(range(first, last + 1))


def spans(minutes):
    ordered, out = sorted(minutes), []
    for minute in ordered:
        if out and out[-1][1] == minute - 1:
            out[-1][1] = minute
        else:
            out.append([minute, minute])
    return ", ".join("%02d:%02d-%02d:%02d" % (a // 60, a % 60, (b + 1) // 60, (b + 1) % 60) for a, b in out) or "none"


def read_captures():
    rows = []
    with open(SCANNER_LOG, errors="replace") as log:
        for line in log:
            match = CAPTURE.match(line)
            if match:
                when = dt.datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").timestamp()
                rows.append((when, float(match[2]), match[3], int(match[4])))
    return rows


def scanner_clean(rows, day):
    minutes = window()
    stamps = [row[0] for row in rows]
    for earlier, later in zip(stamps, stamps[1:]):
        if later - earlier > CAPTURE_GAP_SECONDS:
            remove_interval(minutes, day, earlier, later)
    if stamps and stamps[-1] < epoch(day, LAST_MINUTE):
        remove_interval(minutes, day, max(stamps[-1], epoch(day)), epoch(day, LAST_MINUTE))
    return minutes


def wifi_journal(day):
    saved = OUT / ("wifi-journal-%s.txt" % day)
    if saved.exists():
        return saved.read_text(errors="replace")
    return subprocess.run(["journalctl", "-u", "wifitracker", "-o", "short-unix", "--since", day + " 00:00", "--no-pager"],
                          capture_output=True, text=True, check=False).stdout


def wifi_clean(day):
    minutes = window()
    minutes.difference_update(range(FIRST_MINUTE, WIFI_FROM.get(day, FIRST_MINUTE)))
    stopped = None
    for line in wifi_journal(day).splitlines():
        match = JOURNAL.match(line)
        if not match:
            continue
        if "Deactivated successfully" in line:
            stopped = float(match[1])
        elif "Started" in line and stopped is not None:
            remove_interval(minutes, day, stopped - WIFI_SILENCE_SECONDS, float(match[1]))
            stopped = None
    return minutes


def bt_clean(day):
    minutes = window()
    minutes.difference_update(range(FIRST_MINUTE, BT_FROM.get(day, FIRST_MINUTE)))
    return minutes


def scanner_stats(rows, day, minutes):
    picked = [row for row in rows if epoch(day) <= row[0] < epoch(day) + 86400 and minute_of(day, row[0]) in minutes]
    air = [row for row in picked if 118 <= row[1] < 137]
    stats = {"Attempts": len(picked), "Saved clips per 100 attempts": 100 * sum(r[2] == "saved" for r in picked) / max(1, len(picked)),
             "Airband attempts": len(air), "Airband strong (>=%d dB) per 100" % STRONG_DB: 100 * sum(r[3] >= STRONG_DB for r in air) / max(1, len(air)),
             "Airband nothing-heard per 100": 100 * sum(r[2] == "nothing heard" for r in air) / max(1, len(air))}
    for mhz, name in CHANNELS.items():
        peaks = [r[3] for r in air if r[1] == mhz and r[3] > -99]
        tries = sum(r[1] == mhz for r in air)
        stats["%s %.3f heard per 100" % (name, mhz)] = 100 * len(peaks) / max(1, tries)
        stats["%s %.3f median peak dB" % (name, mhz)] = statistics.median(peaks) if peaks else None
    return stats


def connect(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def wifi_stats(db, day, minutes):
    rows = db.execute("SELECT s.mac, s.start, d.role, d.category FROM sessions s JOIN devices d USING (mac) WHERE s.start >= ? AND s.start < ?",
                      (epoch(day), epoch(day) + 86400)).fetchall()
    picked = [row for row in rows if minute_of(day, row[1]) in minutes]
    hours = max(len(minutes) / 60, 1 / 60)
    networks = {row[0] for row in picked if row[2] == "ap"}
    vehicles = {row[0] for row in picked if row[3] == "Vehicle"}
    return {"Clean hours": hours, "Networks heard": len(networks), "Networks per hour": len(networks) / hours,
            "Vehicles heard": len(vehicles), "Vehicles per hour": len(vehicles) / hours,
            "All devices heard": len({row[0] for row in picked})}


def wifi_signal(path, day):
    db = connect(path)
    values = [row[0] for row in db.execute("SELECT rssi FROM devices WHERE rssi IS NOT NULL AND last_seen >= ? AND last_seen < ?",
                                           (epoch(day, FIRST_MINUTE), epoch(day, LAST_MINUTE + 15)))]
    return {"Devices with a signal reading": len(values), "Median signal dBm": statistics.median(values) if values else None,
            "Weakest signal dBm": min(values) if values else None, "Devices weaker than -60 dBm": sum(v < -60 for v in values)}


def whole_hours(minutes):
    return [hour for hour in range(5, 17) if all(hour * 60 + i in minutes for i in range(60))]


def bt_stats(db, day, minutes, hours):
    rows = db.execute("SELECT s.mac, s.start, d.category FROM sessions s JOIN devices d USING (mac) WHERE s.start >= ? AND s.start < ?",
                      (epoch(day), epoch(day) + 86400)).fetchall()
    picked = [row for row in rows if minute_of(day, row[1]) in minutes]
    count = max(len(minutes) / 60, 1 / 60)
    stats = {"Clean hours": count, "Sessions per hour": len(picked) / count, "Named devices heard": len({row[0] for row in picked}),
             "Vehicles heard": len({row[0] for row in picked if row[2] == "Automotive"})}
    stamps = [int(epoch(day, hour * 60)) for hour in hours]
    if stamps:
        marks = ",".join("?" * len(stamps))
        filtered = db.execute("SELECT COALESCE(SUM(rotating),0), COALESCE(SUM(findmy),0) FROM filtered WHERE hour IN (%s)" % marks, stamps).fetchone()
        devices = db.execute("SELECT COALESCE(SUM(devices),0) FROM rotating_counts WHERE hour IN (%s)" % marks, stamps).fetchone()[0]
        stats["Rotating addresses per hour"] = filtered[0] / len(stamps)
        stats["Find My sightings per hour"] = filtered[1] / len(stamps)
        stats["Rotating devices per hour"] = devices / len(stamps)
    return stats


def bt_signal(db, day, hours):
    stamps = [int(epoch(day, hour * 60)) for hour in hours]
    if not stamps:
        return {}
    marks = ",".join("?" * len(stamps))
    totals = collections.defaultdict(lambda: [0, 0])
    for mac, rssi, samples in db.execute("SELECT mac, rssi, samples FROM rssi_hourly WHERE hour IN (%s)" % marks, stamps):
        totals[mac][0] += rssi * samples
        totals[mac][1] += samples
    return {mac: total / samples for mac, (total, samples) in totals.items() if samples}


def text(value):
    if value is None:
        return "-"
    return "%.1f" % value if isinstance(value, float) else str(value)


def table(lines, title, porch, attic):
    lines += ["", title, "  %-44s %10s %10s %8s" % ("", "Porch", "Attic", "Ratio")]
    for key in porch:
        a, b = porch[key], attic.get(key)
        ratio = "%.2fx" % (b / a) if a and b is not None and a > 0 and b > 0 else "-"
        lines.append("  %-44s %10s %10s %8s" % (key, text(a), text(b), ratio))


def main():
    porch_day, attic_day = sys.argv[1:3] if len(sys.argv) >= 3 else (PORCH_DAY, ATTIC_DAY)
    lines = ["Porch %s vs attic %s, 05:00-17:00" % (porch_day, attic_day), "Generated " + dt.datetime.now().strftime("%F %T")]

    rows = read_captures()
    common = scanner_clean(rows, porch_day) & scanner_clean(rows, attic_day)
    lines += ["", "Scanner minutes compared: %d (%s)" % (len(common), spans(common))]
    table(lines, "SCANNER", scanner_stats(rows, porch_day, common), scanner_stats(rows, attic_day, common))

    wifi_minutes = wifi_clean(porch_day) & wifi_clean(attic_day)
    lines += ["", "Wi-Fi minutes compared: %d (%s)" % (len(wifi_minutes), spans(wifi_minutes))]
    db = connect(WIFI_DB)
    table(lines, "WI-FI", wifi_stats(db, porch_day, wifi_minutes), wifi_stats(db, attic_day, wifi_minutes))
    snapshot = OUT / ("wifi-%s.db" % porch_day)
    table(lines, "WI-FI SIGNAL (last reading per device)", wifi_signal(snapshot if snapshot.exists() else WIFI_DB, porch_day), wifi_signal(WIFI_DB, attic_day))

    bt_minutes = bt_clean(porch_day) & bt_clean(attic_day)
    hours = whole_hours(bt_minutes)
    lines += ["", "Bluetooth minutes compared: %d (%s)" % (len(bt_minutes), spans(bt_minutes))]
    db = connect(BT_DB)
    table(lines, "BLUETOOTH", bt_stats(db, porch_day, bt_minutes, hours), bt_stats(db, attic_day, bt_minutes, hours))
    before, after = bt_signal(db, porch_day, hours), bt_signal(db, attic_day, hours)
    matched = [after[mac] - before[mac] for mac in before.keys() & after.keys()]
    lines += ["", "Bluetooth devices heard on both days: %d" % len(matched)]
    if matched:
        lines.append("  Median signal change, attic minus porch: %+.1f dB" % statistics.median(matched))

    atis = OUT / "atis.jsonl"
    if atis.exists():
        lines += ["", "ATIS REFERENCE READINGS (signal-to-noise, dB)"]
        for line in atis.read_text().splitlines():
            r = json.loads(line)
            lines.append("  %-8s %s %-22s median %5.1f  90th pct %5.1f  frames %d" % (r["label"], r["when"], r["name"], r["median"], r["p90"], r["frames"]))

    report = "\n".join(lines) + "\n"
    print(report, end="")
    if len(sys.argv) < 3:
        (OUT / "report.txt").write_text(report)


if __name__ == "__main__":
    main()
