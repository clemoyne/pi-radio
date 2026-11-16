# porch_pi

Overall project state (what's built across the three Pis, what's next) is kept in `../sdrpi/CLAUDE.md` under Project state.

## Target

- Raspberry Pi 2 Model B (1 GB RAM, armv7l), 8 GB SD card, Raspberry Pi OS 13 (trixie), on the porch
- Access: `ssh -i ~/.ssh/id_ed25519_pi pi@porch-pi.local` (user `pi`, passwordless sudo via `/etc/sudoers.d/010_pi-nopasswd`)
- Nooelec NESDR SMArt v5 (RTL2832U, R820T) on USB, plus two RTL8188CUS Wi-Fi dongles (`rtl8192cu`): one carries the network (the Pi 2 has no Wi-Fi of its own), the other, moved from sdrpi on 15 Nov 2026, is the Wi-Fi tracker's. Both report the same USB serial and either can come up as `wlan0`, so each is told apart by MAC address (in `CLAUDE.local.md`)
- The kernel DVB-T driver is blocked in `/etc/modprobe.d/blacklist-rtl-dvb.conf`; `pi` opens the stick through `plugdev` (udev rules from the `rtl-sdr` package)
- Packages: `rtl-433` (25.02) and `rtl-sdr` from Debian. No SSD, so keep writes small
- If the Mac can't resolve `porch-pi.local` (it sometimes can't when sdrpi can), get the address with `ssh sdrpi getent hosts porch-pi.local` and connect with `-o HostKeyAlias=porch-pi.local`

## Network watchdog

- The dongle (`rtl8192cu`) dropped off Wi-Fi on 11-12 Nov and again at 19:40 on 12 Nov while the Pi kept running and spooling; a power cycle at 22:06 fixed it. Source in `netwatch/`
- Wi-Fi power saving is off (`/etc/NetworkManager/conf.d/90-wifi-powersave-off.conf`; check with `iw dev wlan0 get power_save`). The driver has no power options of its own
- `netwatch.timer` runs `/usr/local/lib/netwatch/netwatch.sh` each minute: if the router doesn't answer pings, it reconnects the managed Wi-Fi interface (the tracker's dongle is unmanaged, so whichever name the network dongle got) at 3 minutes, reloads `rtl8192cu` at 6 (which resets both dongles; the tracker restarts itself) and reboots at 10 (not within 15 minutes of boot). Logged in `journalctl -u netwatch`
- The journal is kept on the SD card (`/etc/systemd/journald.conf.d/50-persistent.conf`, overriding Raspberry Pi OS's volatile default, capped at 40 MB), so `journalctl -b -1` shows the boot before a reboot. systemd's hardware watchdog (1 min, the default here) already reboots a frozen system
- Remove: `sudo systemctl disable --now netwatch.timer`, delete the two units in `/etc/systemd/system`, `/usr/local/lib/netwatch` and the two conf files, then `sudo systemctl daemon-reload`

## Wi-Fi tracker

- sdrpi's passive tracker (`wifi/wifitracker.py`, the same file as `sdrpi/wifi/wifitracker.py`; see sdrpi's CLAUDE.md for what it reads and keeps), on the second dongle. Deployed to `/opt/wifitracker` with `ids.db` (copied from sdrpi), unit `wifitracker` (runs as `pi`, journal log, `CPUQuota=50%`; about 9% CPU)
- `/etc/default/wifitracker` sets `WIFITRACKER_MAC`; the tracker only uses that dongle and refuses any interface carrying the default route. `/etc/NetworkManager/conf.d/90-wifi-tracker-dongle.conf` (`[device-wifi-tracker-dongle]`, `match-device=mac:…`, `managed=0`) keeps NetworkManager off it, and the network connection is bound to the other dongle's MAC. Never match by driver here (sdrpi's conf does): both dongles use `rtl8192cu`
- Database `/var/lib/wifitracker/tracker.db` (90 days; sdrpi's grew about 30 KB a day), `/run/wifitracker/data.json` each minute, served on port 8096 (`/data.json`) for sdrpi's page at http://sdrpi.local:8080/wifi/
- Deploy: copy `wifitracker.py` to `/opt/wifitracker`, then `sudo systemctl restart wifitracker`
- Remove: `sudo systemctl disable --now wifitracker`, delete `/etc/systemd/system/wifitracker.service`, `/etc/default/wifitracker` and the conf file, `sudo systemctl daemon-reload`, delete `/opt/wifitracker` and `/var/lib/wifitracker`

## Car counter (TPMS)

- The page, database and car logic live on sdrpi (`sdrpi/cars/`, http://sdrpi.local:8080/cars/); see sdrpi's CLAUDE.md. Port 8080 here only redirects there
- `tpms/forward.py` (deployed to `/opt/tpms`, unit `tpms`, runs as `pi`, journal log) runs `rtl_433 -M time:unix -F json` and POSTs every decoded message to `http://sdrpi.local:8095/cars/api/ingest` every 3 s with its radio state. Each reply names the band (315, 433 or both, chosen on the page); it saves it to `/var/lib/tpms/settings.json` and restarts rtl_433 on it
- If sdrpi can't be reached, messages go to `/var/lib/tpms/spool.jsonl` (capped at 200 MB, oldest half dropped beyond) and are delivered with their original times once it answers, retrying every 30 s. A batch that half got through is trimmed so nothing is counted twice
- `/var/lib/tpms/tpms.db.moved-to-sdrpi` and `key` are the pre-move copy from 11 Nov 2026; the live key is on sdrpi. Delete them once sdrpi's copy has been confirmed
- Bands: `315`, `433`, `both` (2 min each) and `315w` (the default since 12 Nov): 315 MHz for tires, with `scheduler` restarting rtl_433 for a 75-second weather check every 15 minutes on the frequencies sdrpi sends as `weather_freqs`, and for a station search on 433.92 and 915 MHz, 10 minutes each, on Sundays at 3 am local time (`last_search` kept in `/var/lib/tpms/settings.json`). Each post reports what the radio is doing and when the next check is
- Sensor IDs identify vehicles: the data stays on the LAN and out of git
- Deploy: copy `forward.py` to `/opt/tpms`, then `sudo systemctl restart tpms`
- Remove: `sudo systemctl disable --now tpms`, delete `/etc/systemd/system/tpms.service`, `sudo systemctl daemon-reload`, delete `/opt/tpms` and `/var/lib/tpms`
