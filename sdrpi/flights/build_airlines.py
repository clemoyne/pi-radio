#!/usr/bin/env python3
"""Builds airlines.json ({ICAO code: name}) from OpenFlights' airlines.dat:
https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat
Usage: build_airlines.py airlines.dat airlines.json"""
import csv
import json
import re
import sys

# OpenFlights is out of date for some codes heard here
OVERRIDES = {"ROU": "Air Canada Rouge", "JZA": "Jazz (Air Canada Express)", "FLE": "Flair Airlines", "LYC": "Lynx Air",
             "EDV": "Endeavor Air", "MXY": "Breeze Airways", "WUP": "Wheels Up", "NOJ": "NovaJet",
             "TAI": "Avianca El Salvador"}


def main():
    names, active = {}, {}
    with open(sys.argv[1], newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) < 8:
                continue
            code, name, is_active = row[4].strip().upper(), row[1].strip(), row[7].strip() == "Y"
            if not re.fullmatch(r"[A-Z]{3}", code) or not name or name == r"\N":
                continue
            # Codes get reused; prefer an airline still flying
            if code not in names or (is_active and not active[code]):
                names[code], active[code] = name[:60], is_active
    names.update(OVERRIDES)
    with open(sys.argv[2], "w") as f:
        json.dump(dict(sorted(names.items())), f, separators=(",", ":"))
    print(len(names), "airlines")


if __name__ == "__main__":
    main()
