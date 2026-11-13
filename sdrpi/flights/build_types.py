#!/usr/bin/env python3
"""Builds types.json ({ICAO type designator: name}) from OpenFlights' planes.dat:
https://raw.githubusercontent.com/jpatokal/openflights/master/data/planes.dat
Usage: build_types.py planes.dat types.json"""
import csv
import json
import re
import sys

# Newer names, and designators planes.dat lacks or merges
OVERRIDES = {
    "BCS1": "Airbus A220-100", "BCS3": "Airbus A220-300", "DH8D": "De Havilland Dash 8-400 (Q400)",
    "DH8C": "De Havilland Dash 8-300", "DH8A": "De Havilland Dash 8-100", "DH8B": "De Havilland Dash 8-200",
    "B77L": "Boeing 777-200LR / 777F", "B37M": "Boeing 737 MAX 7", "B38M": "Boeing 737 MAX 8", "B39M": "Boeing 737 MAX 9",
    "B3XM": "Boeing 737 MAX 10", "E75L": "Embraer E175", "E75S": "Embraer E175 (short wing)", "E290": "Embraer E190-E2",
    "E295": "Embraer E195-E2", "CRJ9": "Bombardier CRJ900", "CRJ7": "Bombardier CRJ700", "CRJ2": "Bombardier CRJ200",
    "CRJX": "Bombardier CRJ1000", "A20N": "Airbus A320neo", "A21N": "Airbus A321neo", "A19N": "Airbus A319neo",
    "A339": "Airbus A330-900neo", "A35K": "Airbus A350-1000", "B78X": "Boeing 787-10", "B779": "Boeing 777-9",
    "CL30": "Bombardier Challenger 300/350", "CL35": "Bombardier Challenger 350", "CL60": "Bombardier Challenger 600 series",
    "GLEX": "Bombardier Global Express", "GL5T": "Bombardier Global 5000/5500", "GL7T": "Bombardier Global 7500",
    "C25A": "Cessna Citation CJ2", "C25B": "Cessna Citation CJ3", "C25C": "Cessna Citation CJ4", "C525": "Cessna CitationJet",
    "C56X": "Cessna Citation Excel/XLS", "C68A": "Cessna Citation Latitude", "C700": "Cessna Citation Longitude",
    "C750": "Cessna Citation X", "PC12": "Pilatus PC-12", "PC24": "Pilatus PC-24", "B350": "Beechcraft King Air 350",
    "BE20": "Beechcraft King Air 200", "AT76": "ATR 72-600", "AT75": "ATR 72-500",
}


def main():
    names = {}
    with open(sys.argv[1], newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) >= 3 and re.fullmatch(r"[A-Z][A-Z0-9]{1,3}", row[2].strip()) and row[2].strip() not in names:
                names[row[2].strip()] = row[0].strip()[:60]
    names.update(OVERRIDES)
    with open(sys.argv[2], "w") as f:
        json.dump(dict(sorted(names.items())), f, separators=(",", ":"))
    print(len(names), "types")


if __name__ == "__main__":
    main()
