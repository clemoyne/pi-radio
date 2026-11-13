#!/usr/bin/env python3
"""Build iata.json from OurAirports' airports.csv (https://davidmegginson.github.io/ourairports-data/airports.csv):
{ICAO ident: [IATA code, city]} for airports with scheduled service, for short routes like "ORD-LAX" on the display.

Usage: build_iata.py airports.csv iata.json
"""
import csv
import json
import sys


def main():
    in_path, out_path = sys.argv[1:3]
    codes = {}
    with open(in_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["scheduled_service"] == "yes" and len(row["iata_code"]) == 3:
                codes[row["ident"]] = [row["iata_code"], row["municipality"]]
    with open(out_path, "w") as f:
        json.dump(codes, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    print("airports", len(codes))


if __name__ == "__main__":
    main()
