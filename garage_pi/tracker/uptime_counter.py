#!/usr/bin/env python3
"""Records this boot's uptime so a lifetime total can survive reboots and power cuts."""
import json
import os
import time

STATE_FILE = "/var/lib/piuptime/uptime.json"


def main():
    with open("/proc/sys/kernel/random/boot_id") as f:
        boot_id = f.read().strip()
    with open("/proc/uptime") as f:
        uptime = int(float(f.read().split()[0]))

    if os.path.exists(STATE_FILE):
        # A file that can't be parsed raises here, leaving it in place rather than resetting the count.
        with open(STATE_FILE) as f:
            state = json.load(f)
    else:
        state = {"counting_since": int(time.time()) - uptime, "boots": {}}
    state["boots"][boot_id] = max(state["boots"].get(boot_id, 0), uptime)

    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE_FILE)
    directory = os.open(os.path.dirname(STATE_FILE), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


if __name__ == "__main__":
    main()
