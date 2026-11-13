#!/usr/bin/env python3
"""Build ids.db from the Bluetooth SIG tables and Wireshark's manuf file.

Usage: build_ids.py company_identifiers.yaml member_uuids.yaml manuf ids.db
"""
import re
import sqlite3
import sys


def parse_yaml_pairs(path, key):
    text = open(path, encoding="utf-8").read()
    pattern = r"-\s*%s:\s*0x([0-9A-Fa-f]+)\s*\n\s*name:\s*(.+)" % key
    for value, name in re.findall(pattern, text):
        name = name.strip()
        if len(name) >= 2 and name[0] == name[-1] and name[0] in "'\"":
            name = name[1:-1].replace("''", "'")
        yield int(value, 16), name


def parse_manuf(path):
    for line in open(path, encoding="utf-8", errors="replace"):
        if line.startswith("#"):
            continue
        parts = [part.strip() for part in line.rstrip("\n").split("\t")]
        if len(parts) < 2 or not re.fullmatch(r"([0-9A-Fa-f]{2}[:-]){2}[0-9A-Fa-f]{2}", parts[0]):
            continue
        yield parts[0].replace("-", ":").upper(), parts[2] if len(parts) > 2 and parts[2] else parts[1]


def main():
    company_path, uuid_path, manuf_path, out_path = sys.argv[1:5]
    db = sqlite3.connect(out_path)
    db.executescript("""
        DROP TABLE IF EXISTS company;
        DROP TABLE IF EXISTS uuid16;
        DROP TABLE IF EXISTS oui;
        CREATE TABLE company (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE uuid16 (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE oui (prefix TEXT PRIMARY KEY, name TEXT NOT NULL) WITHOUT ROWID;
    """)
    db.executemany("INSERT OR REPLACE INTO company VALUES (?, ?)", parse_yaml_pairs(company_path, "value"))
    db.executemany("INSERT OR REPLACE INTO uuid16 VALUES (?, ?)", parse_yaml_pairs(uuid_path, "uuid"))
    db.executemany("INSERT OR REPLACE INTO oui VALUES (?, ?)", parse_manuf(manuf_path))
    db.commit()
    for table in ("company", "uuid16", "oui"):
        print(table, db.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
    db.execute("VACUUM")
    db.close()


if __name__ == "__main__":
    main()
