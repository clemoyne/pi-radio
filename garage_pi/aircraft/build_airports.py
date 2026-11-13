#!/usr/bin/env python3
"""Build airports.json from OurAirports' airports.csv, keeping airports with scheduled airline service.

Usage: build_airports.py airports.csv airports.json
"""
import csv
import json
import sys

TYPES = ("large_airport", "medium_airport")


def main():
    in_path, out_path = sys.argv[1:3]
    airports = []
    with open(in_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["type"] in TYPES and row["scheduled_service"] == "yes" and row["elevation_ft"]:
                airports.append([row["ident"], row["name"], round(float(row["latitude_deg"]), 4),
                                 round(float(row["longitude_deg"]), 4), int(float(row["elevation_ft"]))])
    with open(out_path, "w") as f:
        json.dump(airports, f, separators=(",", ":"))
    print("airports", len(airports))


if __name__ == "__main__":
    main()
