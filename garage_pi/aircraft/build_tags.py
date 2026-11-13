#!/usr/bin/env python3
"""Build tags.db from plane-alert-db and the archived FAA LADD filter.

Usage: build_tags.py plane-alert-db.csv industry_filter.txt tags.db
"""
import csv
import re
import sqlite3
import sys

GROUPS = {"Mil": "mil", "Gov": "gov", "Pol": "pol"}


def tagged_rows(path):
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            hexid = row["$ICAO"].strip().lower()
            group = GROUPS.get(row["#CMPG"].strip())
            if group and re.fullmatch(r"[0-9a-f]{6}", hexid):
                tags = ", ".join(t for t in (row["$Tag 1"], row["$#Tag 2"], row["$#Tag 3"]) if t.strip())
                yield hexid, row["$Registration"].strip(), row["$Operator"].strip(), row["$Type"].strip(), \
                    row["$ICAO Type"].strip(), group, tags


def ladd_tokens(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            token = re.sub(r"[^A-Z0-9]", "", line.upper())
            if token:
                yield token,


def main():
    alert_path, ladd_path, out_path = sys.argv[1:4]
    db = sqlite3.connect(out_path)
    db.executescript("""
        DROP TABLE IF EXISTS tagged;
        DROP TABLE IF EXISTS ladd;
        CREATE TABLE tagged (hex TEXT PRIMARY KEY, reg TEXT, operator TEXT, type TEXT, type_code TEXT,
                             grp TEXT NOT NULL, tags TEXT) WITHOUT ROWID;
        CREATE TABLE ladd (token TEXT PRIMARY KEY) WITHOUT ROWID;
    """)
    db.executemany("INSERT OR REPLACE INTO tagged VALUES (?, ?, ?, ?, ?, ?, ?)", tagged_rows(alert_path))
    db.executemany("INSERT OR IGNORE INTO ladd VALUES (?)", ladd_tokens(ladd_path))
    db.commit()
    for table in ("tagged", "ladd"):
        print(table, db.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
    db.execute("VACUUM")
    db.close()


if __name__ == "__main__":
    main()
