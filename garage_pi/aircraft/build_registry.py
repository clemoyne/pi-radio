#!/usr/bin/env python3
"""Build registry.db from Transport Canada's CCARCS download and the FAA's releasable aircraft file.

Usage: build_registry.py carscurr.txt carsownr.txt MASTER.txt ACFTREF.txt registry.db

Street addresses are deliberately left out.
"""
import csv
import sqlite3
import sys

MARK, MAKE, MODEL, CATEGORY, ENGINE, PURPOSE, MANUFACTURED, STATUS, MODE_S = 0, 3, 4, 10, 15, 24, 31, 38, 42
OWNER_MARK, OWNER_NAME, OWNER_STATE, OWNER_COUNTRY, OWNER_TYPE, OWNER_ACTIVE = 0, 1, 6, 9, 11, 13

FAA_REGISTRANTS = {"1": "Individual", "2": "Partnership", "3": "Corporation", "4": "Co-owned", "5": "Government",
                   "7": "LLC", "8": "Non-citizen corporation", "9": "Co-owned"}
FAA_GOVERNMENT = "5"
FAA_CATEGORIES = {"1": "Glider", "2": "Balloon", "3": "Blimp", "4": "Fixed wing single-engine",
                  "5": "Fixed wing multi-engine", "6": "Rotorcraft", "7": "Weight-shift", "8": "Powered parachute",
                  "9": "Gyroplane", "H": "Hybrid lift", "O": "Other"}
FAA_ENGINES = {"0": "None", "1": "Reciprocating", "2": "Turboprop", "3": "Turboshaft", "4": "Turbojet",
               "5": "Turbofan", "6": "Ramjet", "7": "2-cycle", "8": "4-cycle", "10": "Electric", "11": "Rotary"}


def rows(path, width):
    with open(path, encoding="latin-1", newline="") as f:
        for row in csv.reader(f):
            if len(row) >= width:
                yield [field.strip() for field in row]


def load_owners(path):
    owners = {}
    for row in rows(path, 14):
        if row[OWNER_ACTIVE] != "A":
            continue
        entry = owners.setdefault(row[OWNER_MARK], {"entity": None, "individual": None})
        owner = (row[OWNER_NAME], row[OWNER_STATE] or None, row[OWNER_COUNTRY] or None)
        if row[OWNER_TYPE] == "Entity" and entry["entity"] is None:
            entry["entity"] = owner
        elif row[OWNER_TYPE] == "Individual" and entry["individual"] is None:
            entry["individual"] = owner
    return owners


def registry_rows(aircraft_path, owner_path):
    owners = load_owners(owner_path)
    for row in rows(aircraft_path, 43):
        if row[STATUS] != "Registered" or len(row[MODE_S]) != 24:
            continue
        owner = owners.get(row[MARK], {"entity": None, "individual": None})
        if owner["entity"]:
            registrant_type = "Company or organization"
        else:
            registrant_type = "Individual" if owner["individual"] else None
        registrant, state, country = owner["entity"] or owner["individual"] or (None, None, None)
        yield ("%06x" % int(row[MODE_S], 2), "Transport Canada", row[MAKE], row[MODEL], row[MANUFACTURED][:4] or None,
               row[CATEGORY], row[ENGINE], registrant_type, registrant, state, country,
               1 if row[PURPOSE] == "State" else 0)


def faa_records(path):
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = [column.strip() for column in next(reader)]
        for row in reader:
            if row:
                yield dict(zip(header, (field.strip() for field in row)))


def faa_rows(master_path, reference_path):
    models = {r["CODE"]: (r["MFR"].title(), r["MODEL"]) for r in faa_records(reference_path)}
    for record in faa_records(master_path):
        hexid = record["MODE S CODE HEX"].lower()
        if len(hexid) != 6:
            continue
        make, model = models.get(record["MFR MDL CODE"], (None, None))
        kind = record["TYPE REGISTRANT"]
        yield (hexid, "FAA", make, model, record["YEAR MFR"] or None, FAA_CATEGORIES.get(record["TYPE AIRCRAFT"]),
               FAA_ENGINES.get(record["TYPE ENGINE"]), FAA_REGISTRANTS.get(kind), record["NAME"].title() or None,
               record["STATE"] or None, record["COUNTRY"] or None, 1 if kind == FAA_GOVERNMENT else 0)


def main():
    aircraft_path, owner_path, master_path, reference_path, out_path = sys.argv[1:6]
    db = sqlite3.connect(out_path)
    db.executescript("""
        DROP TABLE IF EXISTS registry;
        CREATE TABLE registry (hex TEXT PRIMARY KEY, source TEXT NOT NULL, make TEXT, model TEXT, year TEXT,
                               category TEXT, engine TEXT, registrant_type TEXT, registrant TEXT, owner_state TEXT,
                               owner_country TEXT, government INTEGER NOT NULL) WITHOUT ROWID;
    """)
    db.executemany("INSERT OR REPLACE INTO registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   registry_rows(aircraft_path, owner_path))
    db.executemany("INSERT OR REPLACE INTO registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   faa_rows(master_path, reference_path))
    db.commit()
    print("registry", db.execute("SELECT COUNT(*) FROM registry").fetchone()[0])
    print("government", db.execute("SELECT COUNT(*) FROM registry WHERE government = 1").fetchone()[0])
    print("with owner name", db.execute("SELECT COUNT(*) FROM registry WHERE registrant IS NOT NULL").fetchone()[0])
    db.execute("VACUUM")
    db.close()


if __name__ == "__main__":
    main()
