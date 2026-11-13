#!/usr/bin/env python3
"""Turn the RadioReference exports into a ranked channels.json for the scanner's location.

Scores are estimates: reach (distance vs. transmitter type) times activity
(how often that kind of channel is on the air). The server and page add what
the HackRF actually measures on top of this.
"""
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANALOG = {"AM", "FM", "FMN"}
DECODERS = {"DMR": "-fs", "Project 25": "-f1", "P25": "-f1"}

# Rough distance (km) from the scanner, first match wins. The exports have no coordinates.
# places.json (not published, since it gives away where the scanner is) else places.example.json.
_places = Path(__file__).parent / "places.json"
if not _places.exists():
    _places = _places.with_name("places.example.json")
_local = json.loads(_places.read_text())
PLACES = [tuple(entry) for entry in _local["places"]]

# (pattern over agency + description, activity 0-1, label). First match wins.
AIR_ACTIVITY = [
    (r"\batis\b", 1.0, "continuous recorded broadcast"),
    (r"localizer", 0.9, "continuous navigation tone, not voice"),
    (r"air show|snowbirds|cne waterfront", 0.03, "only during the Labour Day air show"),
    (r"tower", 0.9, "busy tower frequency"),
    (r"ground", 0.75, "ground control"),
    (r"porter|rapidair|nieuport|fixed base|esso", 0.5, "airline and ramp operations"),
]

# GTA list entries that are also in Toronto.csv are trusted; its other rows are not.
ENCRYPTED = re.compile(r"rcmp|grc region|toronto police|\btps\b", re.I)

SYSTEM_NOTES = {
    "Toronto Emergency Services": "P25 Phase II simulcast. Fire and EMS are in the clear, police are encrypted.",
    "Toronto Transit Commission": "PowerTrunk TETRA. Needs a TETRA decoder.",
    "Metrolinx - GO Transit": "P25 Phase II. Some talkgroups encrypted.",
    "Toronto Hydro": "P25 Phase II.",
    "MetroNET": "Emergency interoperability backup, mostly radio checks.",
    "RCMP Central Region/GRC Region centrale": "Encrypted. Cannot be monitored.",
}

NAME_OVERRIDES = {
    110.15: "Billy Bishop Localizer (Runways 08 & 26)", 118.2: "Billy Bishop Tower",
    119.2: "Billy Bishop Tower (alternate)", 121.7: "Billy Bishop Ground", 133.6: "Billy Bishop ATIS",
    123.2: "Billy Bishop ESSO Aviation", 123.35: "Billy Bishop Porter FBO", 128.875: "Billy Bishop Air Canada Rapidair",
    128.975: "Billy Bishop Porter Airlines", 131.05: "Billy Bishop Porter Airlines",
    136.875: "Billy Bishop Porter Airlines", 129.675: "Billy Bishop Nieuport Gate Assignment",
}

# Mentioned in the forum PDF but absent from the CSVs.
FORUM = [
    (146.805, "FM", "", "CANWARN weather net", "ham", 10, "ham", 0.15,
     "active during severe weather"),
    (161.025, "FM", "", "Metrolinx West rail traffic control", "transport", 8, "base", 0.5,
     "reported on air in 2021, rail is moving to NXDN"),
    (161.445, "FM", "", "CN/Metrolinx standby, Weston Sub", "transport", 5, "base", 0.3,
     "standby channel on the nearby Weston Sub"),
]


# Not in the RadioReference exports. From SkyVector's CYYZ page. (MHz, name, activity, why)
PEARSON_KM = _local["pearson_km"]
PEARSON = [
    (120.825, "Pearson ATIS", 1.0, "continuous recorded broadcast"),
    (121.3, "Pearson Clearance Delivery", 0.6, "departure clearances, transmitter at ground level"),
    (119.1, "Pearson Ground (centre)", 0.55, "ground control, transmitter at ground level"),
    (121.65, "Pearson Ground (north)", 0.55, "ground control, transmitter at ground level"),
    (121.9, "Pearson Ground (south)", 0.55, "ground control, transmitter at ground level"),
    (118.35, "Pearson Tower (south)", 0.9, "busy tower frequency"),
    (118.7, "Pearson Tower (north)", 0.9, "busy tower frequency"),
    (124.475, "Pearson Arrival", 0.9, "busy, and aircraft are high up with a clear path to you"),
    (125.4, "Pearson Arrival", 0.8, "busy, and aircraft are high up with a clear path to you"),
    (132.8, "Pearson Arrival", 0.8, "busy, and aircraft are high up with a clear path to you"),
    (134.175, "Pearson Arrival", 0.8, "busy, and aircraft are high up with a clear path to you"),
    (127.575, "Pearson Departure", 0.9, "busy, and aircraft are high up with a clear path to you"),
    (128.8, "Pearson Departure", 0.9, "busy, and aircraft are high up with a clear path to you"),
    (119.3, "Pearson VFR Advisory", 0.3, "light aircraft near the airport"),
    (133.4, "Toronto Terminal (Billy Bishop arrivals and departures)", 0.3, "aircraft near Billy Bishop"),
]


def clean(value):
    return (value or "").strip()


def est_km(text):
    low = text.lower()
    for pattern, km in PLACES:
        if re.search(pattern, low):
            return km
    return 10


def reach(km, range_km):
    return 1 / (1 + (km / range_km) ** 3)


def band_factor(mhz):
    if mhz < 60:
        return 0.6
    if mhz > 1000:
        return 0.35
    if 220 < mhz < 230:
        return 0.7
    return 1.0


def group_of(tag, agency):
    t, a = tag.lower(), agency.lower()
    if tag == "Aircraft":
        return "aviation"
    if tag == "Ham":
        return "ham"
    if tag == "Transportation":
        return "transport"
    if tag in ("Utilities", "Media"):
        return "utility"
    if tag.startswith(("Fire", "EMS", "Emergency", "Law")) or "hatzoloh" in a:
        return "emergency"
    return "venue"


def classify(row, text, km):
    """Return (range_km, activity, why) for one analog row."""
    tag, klass, desc = row["Tag"], row["Class Station Code"], row["Description"].lower()
    low = text.lower()
    has_input = float(row["Frequency Input"] or 0) > 0
    if tag == "Aircraft":
        for pattern, activity, why in AIR_ACTIVITY:
            if re.search(pattern, low):
                return 60, activity, why
        return 60, 0.5, "aviation"
    if tag == "Ham":
        return 40, 0.55, "repeater on a high site, traffic comes in bursts"
    if "chopper" in desc:
        return 120, 0.5, "airborne when flying, so a very strong signal"
    if tag == "Fire-Tac":
        return 30, 0.05, "backup channel, only used when the trunked system fails"
    if tag == "Transportation":
        return 25, 0.35, "dispatch base, though many fleets have moved to apps and data"
    if tag == "Utilities":
        return 25, 0.4, "field crews on a repeater" if has_input else "field crews"
    if tag.startswith("EMS") or tag == "Media":
        return 12, 0.2, "used for events or remotes only"
    if has_input or klass == "RM":
        return 8, 0.3, "local repeater with handheld users"
    return 2.5, 0.3, "low-power handhelds inside a building"


def load_gta_dates():
    dates = defaultdict(list)
    with open(ROOT / "GTA_Frequencies_Full_List.csv", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            dates[round(float(row["Frequency (MHz)"]), 4)].append(clean(row["Last Updated"]))
    return dates


def tier(score):
    if score >= 55:
        return "Very likely"
    if score >= 30:
        return "Likely"
    if score >= 12:
        return "Possible"
    return "Long shot"


def build():
    with open(ROOT / "Toronto.csv", encoding="utf-8-sig", errors="replace") as fh:
        rows = [{k: clean(v) for k, v in r.items()} for r in csv.DictReader(fh)]
    dates = load_gta_dates()
    known = {round(float(r["Frequency Output"]), 4) for r in rows}

    analog, digital, systems = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        mhz = round(float(row["Frequency Output"]), 4)
        if row["Tag"] == "TRS":
            systems[row["Agency/Category"]].append(row)
        elif row["Mode"] in ANALOG:
            analog[mhz].append(row)
        else:
            digital[mhz].append(row)

    out = []
    for mhz, group in analog.items():
        scored = []
        for row in group:
            text = f'{row["Agency/Category"]} {row["Description"]}'
            km = est_km(text)
            range_km, activity, why = classify(row, text, km)
            scored.append((100 * reach(km, range_km) * activity * band_factor(mhz), km, why, row))
        scored.sort(key=lambda s: -s[0])
        score, km, why, top = scored[0]
        updated = max(dates.get(mhz, [""]))
        if updated and updated < "2018" and top["Tag"] not in ("Aircraft", "Ham"):
            score *= 0.7
            why += f"; listing last updated {updated[:4]}"
        users = sorted({s[3]["Description"] for s in scored})
        tones = sorted({s[3]["PL Output Tone"] for s in scored if s[3]["PL Output Tone"] not in ("", "CSQ")})
        name = NAME_OVERRIDES.get(mhz, top["Description"] if len(users) == 1 else f"Shared by {len(users)} users")
        out.append({
            "id": f"{mhz:.4f}a", "mhz": mhz, "mode": "AM" if top["Mode"] == "AM" else "FM",
            "label": top["Mode"], "name": name, "agency": top["Agency/Category"], "users": users,
            "tones": tones, "group": group_of(top["Tag"], top["Agency/Category"]),
            "km": km, "score": round(score), "tier": tier(score), "why": why,
            "listen": True, "source": "Toronto.csv",
        })

    for mhz, mode, tone, name, grp, km, kind, activity, why in FORUM:
        range_km = 40 if kind == "ham" else 25
        score = 100 * reach(km, range_km) * activity
        out.append({
            "id": f"{mhz:.4f}a", "mhz": mhz, "mode": "FM", "label": mode, "name": name, "agency": "Forum PDF",
            "users": [name], "tones": [tone] if tone else [], "group": grp, "km": km, "score": round(score),
            "tier": tier(score), "why": why, "listen": True, "source": "Forum Discussions.pdf",
        })

    for mhz, name, activity, why in PEARSON:
        score = 100 * reach(PEARSON_KM, 60) * activity
        out.append({
            "id": f"{mhz:.4f}a", "mhz": mhz, "mode": "AM", "label": "AM", "name": name, "agency": "Toronto Pearson (YYZ)",
            "users": [name], "tones": [], "group": "aviation", "km": PEARSON_KM, "score": round(score),
            "tier": tier(score), "why": why, "listen": True, "source": "SkyVector CYYZ",
        })

    for mhz, group in digital.items():
        top = next((r for r in group if r["Mode"] in DECODERS), group[0])
        text = f'{top["Agency/Category"]} {top["Description"]}'
        encrypted = bool(ENCRYPTED.search(text))
        users = sorted({r["Description"] for r in group})
        km = est_km(text)
        decoder = None if encrypted else DECODERS.get(top["Mode"])
        score, label_tier, why = 0, "Encrypted" if encrypted else "Needs decoder", None
        if decoder:
            range_km, activity, reason = classify(top, text, km)
            score = 100 * reach(km, range_km) * activity * band_factor(mhz) * 0.8
            label_tier, why = tier(score), f'{top["Mode"]} digital voice; {reason}'
        out.append({
            "id": f"{mhz:.4f}d", "mhz": mhz, "mode": "DIGITAL" if decoder else top["Mode"], "label": top["Mode"],
            "name": top["Description"] if len(users) == 1 else f"Shared by {len(users)} users",
            "agency": top["Agency/Category"], "users": users, "tones": [], "group": "digital",
            "km": km, "score": round(score), "tier": label_tier, "decoder": decoder,
            "why": why or ("Encrypted, cannot be monitored" if encrypted else f'{top["Mode"]} digital voice, no decoder yet'),
            "listen": bool(decoder), "source": "Toronto.csv",
        })

    for agency, group in systems.items():
        freqs = sorted({round(float(r["Frequency Output"]), 4) for r in group})
        mode = group[0]["Mode"]
        encrypted = bool(ENCRYPTED.search(agency))
        out.append({
            "id": f"sys-{re.sub(r'[^a-z0-9]+', '-', agency.lower()).strip('-')}", "mhz": freqs[0], "mode": mode,
            "label": mode, "name": agency, "agency": "Trunked system", "users": [], "tones": [],
            "group": "digital", "km": est_km(agency), "score": 0, "system": True, "freqs": freqs,
            "tier": "Encrypted" if encrypted else "Needs decoder",
            "why": SYSTEM_NOTES.get(agency, f"{mode} trunked system, {len(freqs)} frequencies"),
            "listen": False, "source": "Toronto.csv",
        })

    seen = defaultdict(list)
    with open(ROOT / "GTA_Frequencies_Full_List.csv", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            mhz = round(float(row["Frequency (MHz)"]), 4)
            if mhz not in known and clean(row["Tag"]) != "TRS":
                seen[mhz].append(row)
    for mhz, group in seen.items():
        users = sorted({clean(r["Description"]) for r in group})
        out.append({
            "id": f"{mhz:.4f}u", "mhz": mhz, "mode": "FM", "label": "FM?", "name": users[0],
            "agency": clean(group[0]["Category"]), "users": users, "tones": [], "group": "unverified",
            "km": est_km(" ".join(users)), "score": 0, "tier": "Unverified",
            "why": "Only in the GTA list, whose extra rows look machine-generated. Mode unknown.",
            "listen": True, "source": "GTA_Frequencies_Full_List.csv",
        })

    out.sort(key=lambda c: (-c["score"], c["mhz"]))
    return out


if __name__ == "__main__":
    channels = build()
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "channels.json"
    target.write_text(json.dumps(channels, indent=1))
    print(f"{len(channels)} channels -> {target}")
    for c in [c for c in channels if c["listen"] and c["group"] != "unverified"][:30]:
        print(f'{c["score"]:3} {c["tier"]:11} {c["mhz"]:9.4f} {c["label"]:4} {c["km"]:3}km {c["name"][:42]:42} {c["why"][:50]}')
