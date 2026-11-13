# porch_pi

Overall project state (what's built across the three Pis, what's next) is kept in `../sdrpi/CLAUDE.md` under Project state.

## Target

- Raspberry Pi 2 Model B (1 GB RAM, armv7l), 8 GB SD card, Raspberry Pi OS 13 (trixie), on the porch
- Access: `ssh -i ~/.ssh/id_ed25519_pi pi@porch-pi.local` (user `pi`, passwordless sudo via `/etc/sudoers.d/010_pi-nopasswd`)
- Nooelec NESDR SMArt v5 (RTL2832U, R820T) on USB, plus an RTL8188CUS Wi-Fi dongle
- The kernel DVB-T driver is blocked in `/etc/modprobe.d/blacklist-rtl-dvb.conf`; `pi` opens the stick through `plugdev` (udev rules from the `rtl-sdr` package)
- Packages: `rtl-433` (25.02) and `rtl-sdr` from Debian. No SSD, so keep writes small

## Car counter (TPMS)

- The page, database and car logic live on sdrpi (`sdrpi/cars/`, http://sdrpi.local:8080/cars/); see sdrpi's CLAUDE.md. Port 8080 here only redirects there
- `tpms/forward.py` (deployed to `/opt/tpms`, unit `tpms`, runs as `pi`, journal log) runs `rtl_433 -M time:unix -F json` and POSTs every decoded message to `http://sdrpi.local:8095/cars/api/ingest` every 3 s with its radio state. Each reply names the band (315, 433 or both, chosen on the page); it saves it to `/var/lib/tpms/settings.json` and restarts rtl_433 on it
- If sdrpi can't be reached, messages go to `/var/lib/tpms/spool.jsonl` (capped at 200 MB, oldest half dropped beyond) and are delivered with their original times once it answers, retrying every 30 s. A batch that half got through is trimmed so nothing is counted twice
- `/var/lib/tpms/tpms.db.moved-to-sdrpi` and `key` are the pre-move copy from 11 Nov 2026; the live key is on sdrpi. Delete them once sdrpi's copy has been confirmed
- Bands: `315`, `433`, `both` (2 min each) and `315w` (the default since 12 Nov): 315 MHz for tires, with `scheduler` restarting rtl_433 for a 75-second weather check every 15 minutes on the frequencies sdrpi sends as `weather_freqs`, and for a station search on 433.92 and 915 MHz, 10 minutes each, on Sundays at 3 am local time (`last_search` kept in `/var/lib/tpms/settings.json`). Each post reports what the radio is doing and when the next check is
- Sensor IDs identify vehicles: the data stays on the LAN and out of git
- Deploy: copy `forward.py` to `/opt/tpms`, then `sudo systemctl restart tpms`
- Remove: `sudo systemctl disable --now tpms`, delete `/etc/systemd/system/tpms.service`, `sudo systemctl daemon-reload`, delete `/opt/tpms` and `/var/lib/tpms`
