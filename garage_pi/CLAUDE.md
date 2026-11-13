# garage_pi

Overall project state (what's built across the three Pis, what's next) is kept in `../sdrpi/CLAUDE.md` under Project state.

## Target device

- Raspberry Pi 3 A+: 512MB RAM, WiFi only (no Ethernet)
- Reach it with `ssh piaware`
- Runs PiAware, which must not be disrupted
- Raspbian 10 (buster), Python 3.7, no swap, no RTC. Don't run `apt` upgrades. The memory cgroup is disabled, so systemd `MemoryMax` has no effect.

## Site icon

`webroot/favicon.ico` and `webroot/apple-touch-icon.png` (the RF Rogue logo, shared with sdrpi and porch_pi) are installed in lighttpd's document root `/var/www/html/`, so every page on this Pi gets them. SkyAware's pages keep their own icon. Remove by deleting the two files there.

## Bluetooth tracker

Source is in `tracker/`. Dashboard: http://piaware.local/bluetooth/

| On the Pi | Purpose |
|---|---|
| `/opt/bletracker/` | Collector, `ids.db` lookup tables, `www/` dashboard |
| `/etc/systemd/system/bletracker.service` | Service (runs as a dynamic user with `CAP_NET_RAW`/`CAP_NET_ADMIN`) |
| `/etc/lighttpd/conf-available/90-bletracker.conf`, symlinked in `conf-enabled/` | Adds `/bluetooth` to the existing web server |
| `/var/lib/private/bletracker/tracker.db` | Devices and sessions, kept 90 days |
| `/run/bletracker/data.json` | Dashboard data, rewritten each minute (RAM only) |
| `piuptime.timer`, `piuptime.service` in `/etc/systemd/system/` | Records uptime every 10 minutes, independent of the tracker |
| `/var/lib/piuptime/uptime.json` | Seconds of uptime per boot ID. Total is the sum. Copy it when replacing the SD card |
| `/etc/rsyslog.d/10-bletracker.conf` | Drops the kernel's "advertising data len corrected" lines so they don't reach the SD card |

Deploy: copy changed files to `/opt/bletracker`, then `sudo systemctl restart bletracker`. Validate web config with `sudo /usr/sbin/lighttpd -tt -f /etc/lighttpd/lighttpd.conf` before `sudo systemctl reload lighttpd`. The `mod_setenv` warnings it prints come from PiAware's own config.

Rebuild `ids.db` with `tracker/build_ids.py` from the Bluetooth SIG `company_identifiers.yaml` and `member_uuids.yaml` plus Wireshark's `manuf`.

Remove: `sudo systemctl disable --now bletracker`, delete the service file and the lighttpd symlink and config, `sudo systemctl daemon-reload`, `sudo systemctl reload lighttpd`, delete `/opt/bletracker` and `/var/lib/private/bletracker`, delete the rsyslog rule and `sudo systemctl restart rsyslog`. For the uptime counter: `sudo systemctl disable --now piuptime.timer`, delete its two unit files and `/var/lib/piuptime`.

Device names are broadcast by anything in radio range, so the dashboard must only insert data with `textContent`.

Rotating addresses are linked across rotations only to count devices per manufacturer. Addresses and links stay in memory for about two hours; only hourly totals are saved (`rotating_counts`). Don't persist or display them.

## Aircraft log

Source is in `aircraft/`. Dashboard: http://piaware.local/aircraft/. Reads `/run/dump1090-fa/aircraft.json`; changes nothing in PiAware.

`cargo` names the cargo operator from `aircraft/cargo.json` (deployed to `/opt/aircraftlog/`; keep it the same as `sdrpi/flights/cargo.json`): callsign prefix, registered owner, or a freighter model. It is recomputed for the whole log at each start. `icao_type` keeps SkyAware's ICAO type designator, because `type` becomes the registry model when one is known.

Every visit list (Interesting aircraft, Full log, watch list, emergencies) has a "Seen before" column: `with_seen_before` counts earlier visits by the same hex, so it only reaches back the log's 30 days. Clicking the count runs the Full log search for that hex. Every visit list sorts by clicking a column header (`VISIT_COLUMNS` in `www/index.html`; a second click reverses it, blanks stay last); each list (`interesting`, `log`, `watch`, `emergency`) keeps its own sort in the browser's `localStorage` (`aircraft-sort`), and the note under it says the order. "Bank Of Utah Trustee" is a trust bank registered as owner of many US airliners (Endeavor Air's CRJ-900s among them), not one aircraft.

`/aircraft/api/export?after=<last_seen>&after_id=<id>&limit=100` hands finished visits (unheard for `VISIT_GAP_SECONDS`) with their tracks to sdrpi's flight store, which keeps them long term on its SSD. It only reads the log.

"In range now" counts aircraft heard in the last 60 seconds, to match SkyAware; "Heard in the last 30 min" counts live visits, which end after `VISIT_GAP_SECONDS`. The emergency box at the top turns red while an aircraft in range squawks 7500, 7600 or 7700, and otherwise lists emergency squawks from the last 30 days.

| On the Pi | Purpose |
|---|---|
| `/opt/aircraftlog/` | Logger, `tags.db` lookup tables, `www/` page |
| `/etc/systemd/system/aircraftlog.service` | Service; also serves the watch API on `127.0.0.1:8091` |
| `/etc/lighttpd/conf-available/90-aircraftlog.conf`, symlinked in `conf-enabled/` | Adds `/aircraft` and proxies `/aircraft/api/` |
| `/var/lib/private/aircraftlog/log.db` | All visits for 30 days; visits matching the watch list are kept until removed. `track_points` holds the position track of every flight and is pruned with its visit. Flights seen squawking 7500, 7600 or 7700 (`emergency` column) are kept past 30 days; once those exceed an estimated 500 MB, the oldest are deleted. The `private` column is a best guess from callsign, owner type and category (`is_private`) |
| `/opt/aircraftlog/www/ol/` | OpenLayers 6.3.1, copied from SkyAware so a PiAware update can't break the track map. Base map tiles load from OpenStreetMap in the viewer's browser, as SkyAware's do |

Rebuild `tags.db` with `aircraft/build_tags.py` from plane-alert-db's CSV and `industry_filter.txt` in the Jxck-S/FAA-LADD-Archive repository. The opt-out list changes weekly, so `tags.db` goes stale until rebuilt.

Rebuild `airports.json` (used to spot go-arounds) with `aircraft/build_airports.py` from OurAirports' https://davidmegginson.github.io/ourairports-data/airports.csv. It goes in `/opt/aircraftlog/`, and the logger won't start without it.

Rebuild `registry.db` with `aircraft/build_registry.py` from Transport Canada's `ccarcsdb.zip` (https://wwwapps.tc.gc.ca/Saf-Sec-Sur/2/CCARCS-RIACC/download/ccarcsdb.zip) and `MASTER.txt` and `ACFTREF.txt` from the FAA's https://registry.faa.gov/database/ReleasableAircraft.zip. It includes owner names but deliberately omits street addresses. The log stays on the local network. `registry.faa.gov` returns 403 to automated requests, so the FAA file has to be downloaded in a browser.

When a live airline go-around is detected within the last 5 minutes, the logger POSTs the airport and callsign to `http://sdrpi.local:8080/api/goaround` on a background thread, so sdrpi can record the radio traffic. Failures are only logged. Backfilled go-arounds never notify. Flights low near an airport in `GO_AROUND_WATCH_AIRPORTS` are checked every 15-second poll, other flights at the 60-second flush.

Remove: `sudo systemctl disable --now aircraftlog`, delete the service file and the lighttpd symlink and config, `sudo systemctl daemon-reload`, `sudo systemctl reload lighttpd`, delete `/opt/aircraftlog` and `/var/lib/private/aircraftlog`.

### Plane display

http://piaware.local/aircraft/display/ (`aircraft/www/display/index.html`, deployed to `/opt/aircraftlog/www/display/`, linked from the aircraft log and sdrpi's flights dashboard) draws a 128x64 dot-matrix LED panel on a canvas for a spare screen. It shows the nearest planes in turn (or the closest, or one followed flight): airline badge with a plane turned to the track, airline, route, type, then altitude, speed, track and vertical rate or the route's cities. It polls `/skyaware/data/aircraft.json` each second; `/aircraft/api/describe?hex=a,b` returns who each aircraft is from the logger's live visits (shared read-only with the API thread), else the log, else SkyAware's database; the browser then asks sdrpi's `http://sdrpi.local:8080/flights/api/describe` for the airline name, route and type name (the scanner sends `Access-Control-Allow-Origin: *` on `/flights/api/`). Without sdrpi it still shows callsign, registration and metrics. Settings live in the browser's `localStorage` (`plane-display`). The second line leads with the flight number (callsign) unless the top line shows it. "Within: Auto" uses the smallest of 3, 5, 10, 20, 50 and 100 km with a plane in it, else anywhere. Go-arounds (`/aircraft/api/search?group=go_around&field=go_around_at`, every 30 s) join the rotation until 30 minutes after, and sdrpi's diversions (`/flights/api/diversions`, every minute; found only once a flight is over) for 30 minutes from when the display first sees them (`plane-display-diversions` in `localStorage`), with "GO AROUND at <airport>" or "DIVERSION" flashing red; a plane no longer in the feed is drawn from its last report or the event. The bottom right shows a 12- or 24-hour clock and sdrpi's temperature (`/weather/api/status`) in a 3x5 font, each switchable Badges use the airline's colour and code, not airline logos

## Status page

Source is in `status/`, shared with `sdrpi/status/` (only `config.json`, the service and the lighttpd file differ). Page: http://piaware.local/status/, linked from the aircraft and Bluetooth pages, which also load `/status/alerts.js` for the alert banner.

| On the Pi | Purpose |
|---|---|
| `/opt/pistatus/` | Checker, `config.json`, `www/` page and banner script |
| `/etc/systemd/system/pistatus.service` | Runs as `pi` with `NoNewPrivileges`. Not `DynamicUser`: on systemd 241 a dynamic user is refused service and clock state over D-Bus |
| `/etc/lighttpd/conf-available/90-pistatus.conf`, symlinked in `conf-enabled/` | Adds `/status` and proxies `/status/api/` to `127.0.0.1:8093`. Relies on `90-aircraftlog.conf` loading `mod_proxy` |
| `/run/pistatus/status.json` | Rewritten each minute; history in `/var/lib/pistatus/history.json` every 10 minutes |
| `pistatus-power.path`, `pistatus-power.service`, `/usr/local/lib/pistatus/power-action.sh` | Root-only helper that reboots or shuts down when the page writes `/run/pistatus/power-request`. It accepts only `reboot`, `poweroff` or `test` |

Shutting down from the page needs `garage_pi` typed. Test with `{"action":"test"}`, never a real action: rebooting interrupts PiAware.

Remove: `sudo systemctl disable --now pistatus pistatus-power.path`, delete the three unit files, `/usr/local/lib/pistatus`, the lighttpd symlink and config, `/opt/pistatus` and `/var/lib/pistatus`, then `sudo systemctl daemon-reload` and `sudo systemctl reload lighttpd`. Remove the `alerts.js` script tag and Status link from the two pages.
