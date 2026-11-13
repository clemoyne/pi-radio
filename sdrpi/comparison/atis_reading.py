#!/usr/bin/env python3
"""Measures signal-to-noise on constant broadcasts, a reference that doesn't depend on how busy the airwaves are."""
import asyncio
import json
import re
import statistics
import struct
import sys
import time

import aiohttp

BASE = "http://localhost:8080"
LOG = "/mnt/data/logs/scanner.log"
OUTPUT = "/mnt/data/comparison/atis.jsonl"
STATIONS = [("120.8250a", "Pearson ATIS 120.825"), ("133.6000a", "Billy Bishop ATIS 133.6")]
SETTLE_SECONDS = 4
READ_SECONDS = 60
PAUSE_SECONDS = 8
IDLE_WITHIN_SECONDS = 180  # a recent auto-recording line means nobody is listening


def radio_idle():
    latest = None
    with open(LOG, errors="replace") as log:
        for line in log:
            match = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) capture ", line)
            if match:
                latest = match[1]
    return latest and time.time() - time.mktime(time.strptime(latest, "%Y-%m-%d %H:%M:%S")) <= IDLE_WITHIN_SECONDS


async def read_station(session, channel, name, label):
    readings, started = [], time.monotonic()
    async with session.ws_connect("%s/ws/listen?id=%s&squelch=-100" % (BASE, channel)) as ws:
        while time.monotonic() - started < SETTLE_SECONDS + READ_SECONDS:
            message = await asyncio.wait_for(ws.receive(), 15)
            if message.type == aiohttp.WSMsgType.BINARY and time.monotonic() - started >= SETTLE_SECONDS:
                readings.append(struct.unpack("<f", message.data[:4])[0])
            elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
    readings.sort()
    record = {"label": label, "when": time.strftime("%F %T"), "name": name, "frames": len(readings),
              "median": round(statistics.median(readings), 1) if readings else -99.0,
              "p90": round(readings[int(len(readings) * 0.9)], 1) if readings else -99.0}
    with open(OUTPUT, "a") as out:
        out.write(json.dumps(record) + "\n")
    print(record, flush=True)


async def main(label):
    if not radio_idle():
        print("radio looks busy, reading skipped", flush=True)
        return
    async with aiohttp.ClientSession() as session:
        for channel, name in STATIONS:
            await read_station(session, channel, name, label)
            await asyncio.sleep(PAUSE_SECONDS)


asyncio.run(main(sys.argv[1]))
