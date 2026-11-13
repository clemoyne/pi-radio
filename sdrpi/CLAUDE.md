# sdrpi

## Project state (as of 12 Nov 2026)

Three Pis on the home LAN, all receive-only; sdrpi is the hub. Details of each piece are in the sections below and in `../garage_pi/CLAUDE.md` and `../porch_pi/CLAUDE.md`. The three are mirrored into `../pi-radio` by its `sync.sh`; after a commit there, its `push.sh` pushes to the private `clemoyne/pi-radio-private` (full history) and replays the commits onto the public `clemoyne/pi-radio` (clean history from 12 Nov 2026), refusing if anything in its gitignored `.leakcheck` would be published.

Built and running:
- sdrpi (HackRF, USB Wi-Fi dongle, SSD):
  - Scanner: live listening, auto-recorded clips, DMR/P25 voice, go-around recordings with the static cut out, and overnight collections (railway, business radio)
  - ACARS mode: classic ACARS or VDL2, with CPDLC and ADS-C explained in plain words
  - Flights dashboard: routes, go-around time added, diversions, VIP aircraft, a top-20 leaderboard and "seen before" counts
  - Car counter store and page, and the weather dashboard, both fed by porch_pi
  - Bluetooth and Wi-Fi trackers, the status page, satellite pass recording (satwx), and the night band survey with its signal identification tools
- garage_pi (PiAware): aircraft log (go-arounds sent to sdrpi, watch list, "seen before"), plane display, Bluetooth tracker, status page
- porch_pi (RTL-SDR): rtl_433 forwarder for tire and weather sensors, on band `315w`

What's next or open:
- Weather satellites: the scanner's vertical antenna can't hear Meteor-M. Nothing was decoded on the 12 Nov test passes (33° and 72° up). The fix is a V-dipole (two 53 cm elements, 120°, horizontal) or a QFH, and optionally a GPIO-driven RF switch so satwx flips antennas itself. The 13-14 Nov passes (20:00, 21:40, 02:32, 04:12) are scheduled and will come up empty without a new antenna. The page of decoded pictures isn't built yet
- Collections: the first overnight run is 12-13 Nov, 22:00-07:00. Check what it kept and the turned-away clips in `/mnt/data/scanner/collect-rejects`; the voice check was loosened for short railway calls the same day. Delete that folder once the check is right
- Diversions: none detected yet (8-12 Nov were calm). Check the first bad-weather day for false alarms or misses
- Weather: the LaCrosse sensor's battery is low. Readings come every 15 minutes (75 s away from tires each time, about 8% of the time); the first weekly station search runs Sunday 15 Nov at 3 am. A second RTL-SDR on porch_pi would allow continuous weather without costing tire time
- VIP list: the CC-330 Husky 330001's hex isn't in plane-alert-db; add it to `flights/vip.json` once it's seen
- porch_pi dropped off Wi-Fi on 11-12 Nov and again 19:40-22:06 on 12 Nov (the Pi kept running; spooled readings arrived after a power cycle). Since 12 Nov its Wi-Fi power saving is off, `netwatch` reconnects or reboots on its own and the journal is persistent: if it drops again, check `journalctl -b -1` and `-u netwatch` there. Its pre-move `/var/lib/tpms/tpms.db.moved-to-sdrpi` and `key` can be deleted
- garage_pi's "seen before" only reaches back its 30-day log; sdrpi's flights dashboard holds the full history
- Ideas offered but not started: AIS, gas and water meters, Weatheradio, APRS

## Target

- Raspberry Pi 4 (4GB)
- Raspberry Pi OS Lite 64-bit, booting from SD
- Access: `ssh sdrpi` (user `pi`)

- HackRF One on USB, firmware 2024.02.1 (matches the `hackrf` package); usable by `pi` without sudo

## Scanner

- Receive-only web interface at http://sdrpi.local:8080 (systemd unit `sdrpi-scanner`, no login, LAN only)
- The RF Rogue icon (`static/favicon.ico`, skull crop; `static/apple-touch-icon.png`, full logo) is served at the root, so every page on port 8080 shows it. The same files are on garage_pi and porch_pi
- Source in `scanner/`; deployed to `/mnt/data/scanner/app`; log at `/mnt/data/logs/scanner.log`; signal survey at `/mnt/data/scanner/survey.json`
- `scanner/build_channels.py` regenerates `channels.json` from `Toronto.csv`; GTA-only rows stay "unverified" because many look machine-generated. The two RadioReference CSVs and `channels.json` aren't in git (their terms bar redistribution), and nor is `scanner/places.json`, the rough distances to named places (`places.example.json` is the published placeholder)
- Digital voice: DSD-FME and mbelib built from source in `/mnt/data/scanner/decoders`; conventional DMR and P25 Phase 1 only, no trunk following; encrypted traffic stays muted and no keys are configured
- Auto-recorded clips (Recordings tab) in `/mnt/data/scanner/clips`, capped at 300, indexed in `/mnt/data/scanner/clips.json`
- Manual recordings: Record (10 min cap, in memory) and Continuous (1 GB cap, streamed to disk, survives the page closing, locks the HackRF until stopped). Neither counts toward the 300-clip limit
- "Only record transmissions" is on by default: audio is kept per transmission only if it passes an SNR check and looks like speech. If a manual Record ends with nothing kept, it saves everything that passed the squelch instead
- Go-arounds: garage_pi's aircraft log POSTs `{airport, callsign}` to `/api/goaround`. Records the full 5-minute window on the departures channel (Pearson 127.575 or 128.8, Billy Bishop 133.4) with squelch open and no transmission filter, because the filter kept only seconds of real go-arounds. When the window ends, `trim_static` cuts the static out: it keeps half-second blocks whose loudness varies (speech), shifts from the recording's usual level, or whose SNR is 6 dB above the recording's median, with 1 s before and 1.5 s after. The full recording stays beside it as `<name>-full.wav` (linked as "full recording", deleted with it). It takes over from live listening and auto-recording, but never from a recording already running
- Only one process can use the HackRF, so stop the service before running `hackrf_*` tools by hand
- ACARS mode: acarsdec 3.7 with libacars 2.2.1, built from source (github.com/TLeconte/acarsdec, github.com/szpajder/libacars) into `/mnt/data/scanner/decoders`; acarsdec needs `-DCMAKE_EXE_LINKER_FLAGS="-L.../lib -Wl,-rpath,.../lib"` to find libacars. It takes the HackRF through SoapySDR (`soapysdr-module-hackrf`) on `ACARS_FREQS` (six channels, 130.025-131.550 MHz) as the radio's job, so listening, the band monitor, auto-recording and go-around recordings wait. Started from the scanner or http://sdrpi.local:8080/acars/ with an end time; the scanner itself ends it and restarts the band monitor. Every message goes to `/mnt/data/scanner/acars.db`, capped at 5 GB (oldest deleted)
- Automatic switching, both on by default and set on the ACARS page (`/mnt/data/scanner/settings.json`): a go-around pauses ACARS mode, records, then `after_job()` resumes ACARS until its original end; an emergency squawk in garage_pi's `emergencies_now` (polled every 60 s) starts ACARS mode, kept running 10 minutes past the last sighting. It never interrupts a go-around or a user's continuous recording. Stopping emergency ACARS mode keeps it off until those emergencies clear. `SCANNER_EMERGENCY_URL` overrides the feed for testing
- VDL2: the ACARS page's "Decode VDL2" setting (`acars_vdl2`) makes ACARS mode run dumpvdl2 (`DUMPVDL2` in `/mnt/data/scanner/decoders/bin`, built from github.com/szpajder/dumpvdl2 against the same libacars) on `VDL2_FREQS` (136.650-136.975 MHz, HackRF at 2.1 MS/s on 136.9 MHz) instead of acarsdec. Frames carrying an ACARS message go in the same `messages` table; `decoded` holds `via: VDL2` and the AVLC addresses. Without the binary it falls back to classic ACARS. dumpvdl2 sets its rate with `--oversample` (×105 kS/s), not a sample rate. `AcarsExplainer` reads libacars' `arinc622` from either place (acarsdec nests it under `libacars`) and explains CPDLC: `FANS_IMI` names the message kind from the text (AT1, CR1, CC1, DR1, ADS), `ATC_CENTRES` the control centre from the ground address prefix, `CPDLC_MEANINGS` the common replies
- Collections (`COLLECTIONS`): `POST /api/collect {"plan": [{"group", "from", "to"}]}` runs a `CollectJob` per time slot: the HackRF sits on the group's 2 MHz block (railway: 15 kHz channels 159.81-161.61 around 160.7175 MHz; business: 6.25 kHz raster 462.15-463.85 around 463.003 MHz), watches every channel's power in each FFT, follows a keyed-up channel until 10 s of silence, and keeps only transmissions that pass `TransmissionGate` with `short_calls` (loudness varying 3.5 dB instead of 5 is enough when 300-1200 Hz outweighs 2.5-4 kHz hiss, since brief railway calls barely vary). Rejected conversations are saved to `/mnt/data/scanner/collect-rejects` (first 40) while that folder exists. Goals: railway 15 min, business 3 conversations or 5 min. A slot whose group is done hands over to any group still short. Clips carry `collection` and are never expired. It steps aside for a listener, go-around or ACARS mode and resumes; Stop on the banner ends it for the night. State in `/mnt/data/scanner/collect.json`. The band monitor's sweep can't drive this: it shows a comb of false activity across 160-161.6 MHz
- Before restarting `sdrpi-scanner`, check `curl localhost:8080/api/health`: `listening` true means someone is listening. A recent auto-recording line in the log does not prove the radio is idle

## Flights dashboard

- http://sdrpi.local:8080/flights/, linked from every page on both Pis. Source in `flights/`, deployed to `/mnt/data/flights/app`, unit `flightstore` (runs as `pi`), API on `127.0.0.1:8094` proxied by the scanner at `/flights/api/<name>`, log `/mnt/data/logs/flightstore.log`
- Every 10 minutes it pulls finished flights (unheard for 30 min) with their tracks from garage_pi's `/aircraft/api/export`, cursor `(last_seen, id)` kept in the `meta` table, into `/mnt/data/flights/flights.db`. garage_pi keeps 30 days, so sdrpi can be offline that long without losing flights
- Takeoffs and landings come from each track (starts or ends below 1,500 ft within 8 km of a scheduled airport, with a 1,000 ft climb or descent); "over the house" uses the closest point along each track segment to garage_pi's receiver position, within a 1 km radius (why 1 km and not 0.5 is in `CLAUDE.local.md`)
- Capped at 10 GB (`MAX_DB_BYTES`): above it the oldest tracks go first, then the oldest flights; interesting flights (tagged, private, emergency, go-around) are never removed. At about 2-3 MB a day the cap is roughly a decade away
- Go-arounds are also found in each saved track (`find_go_arounds`: a low point within 8 km of an airport, at most 1,500 ft above it, reached descending and followed by a 1,000 ft climb within 5 minutes), so a flight's page lists every one, not just garage_pi's single flag. Time added: until the aircraft is back within 2.5 km and 400 ft of the go-around point; else the landing time less a 140-knot approach from there; else "at least" until the track ends. The go-around list shows the total
- Diversions (`find_diversions`, rebuilt after each sync from the last 60 days of airline flights) come from what was seen, not the route list, whose listings are often stale: turned back (took off and landed at the same airport after 10+ minutes and 20+ km out); diverted here (landed where the flight number, seen on 3+ other days, only flew over above 8,000 ft); diverted away (a flight number that lands at an airport on 2+ other days came down towards it below 8,000 ft, then climbed 3,000+ ft and left or landed elsewhere; departures are excluded, since some numbers are used both ways). None fired on 8-12 Nov; a made-up test of each kind did
- VIP aircraft: `flights/vip.json` rules (regexes on hex, reg without dashes, callsign, type, operator, plane-alert tags; all of a rule's fields must match, first rule wins) set the `vip` label and make the flight interesting, so it's never pruned. They cover the RCAF's Polaris 15001, CC-330 Husky and 412 Squadron CC-144 Challengers (Prime Minister, Governor General, royal visits), the Royal Flight (KRF), RAF Voyager ZZ336 and Envoys, Air Force One and SAM flights, royal and presidential operators, and foreign "Government of …" operators (Canada's federal and provincial governments excluded). Civilian jets registered C-FC.. use callsigns starting CFC, so RCAF callsign rules need CFC plus digits and a type. Editing vip.json relabels every stored flight on the next start
- The Overview opens with the top 20 planes by times seen in the chosen period (`planes?sort=visits`, `TOP_PLANES`), linking to the full 200 on the Planes tab
- The Interesting tab's "Seen before" counts earlier flights by the same aircraft across all stored history and links to its plane page, which lists them all
- The Interesting tab's search (`interesting?q=`) covers every interesting flight ever stored, whatever the period; "VIP aircraft only" (`vip=1`) filters to VIP labels
- Go-arounds and emergency squawks have Overview tiles, their own list and a page per flight (`#flight=<id>`). "New" means a higher flight id than the browser last listed, kept in that browser's `localStorage` (`flights-notable-seen`); a first visit counts everything already logged as seen
- Plane pages draw up to 30 saved tracks with OpenLayers (`flights/www/ol/`, the same copy as garage_pi's, served by the scanner at `/flights/ol/`); map tiles load from OpenStreetMap in the viewer's browser
- Aircraft type (`actype`, an ICAO designator like B38M or A388) comes from the registry model via `TYPE_RULES` first, then SkyAware's type from garage_pi (`icao_type`), which is stale for many Canadian aircraft. Names come from `types.json` (`flights/build_types.py`, OpenFlights' `planes.dat` plus `OVERRIDES`)
- Cargo uses `flights/cargo.json`, the same file as `garage_pi/aircraft/cargo.json`: callsign prefix, then registered owner, then a Boeing, Airbus or McDonnell Douglas freighter model. Tail suffixes aren't used (N…UP is also Wheels Up's jets). Changing `cargo.json`, `types.json` or the type rules reclassifies every stored flight on the next start; bumping `REPULL_VERSION` refetches garage_pi's 30 days to fill new columns
- Routes (origin, destination) come from Virtual Radar Server's CC0 route list (github.com/vradarserver/standing-data, `routes/schema-01/<letter>/<prefix>-all.csv`), fetched per airline prefix heard in the last 30 days, at most weekly, 30 prefixes per sync, into the `routes` table. `pick_leg` chooses the leg of multi-stop routes from the takeoff or landing seen, else the track's climb or descent, else (overflights) the leg whose airports are nearest the track's first and last points; a route contradicting what was seen is dropped. Bump `ROUTE_LOGIC_VERSION` after changing it to recheck every stored flight. About a third of routed flights neither start nor end at Pearson or Billy Bishop; the Destinations tab filters by origin (CYYZ, CYTZ, or anywhere else) Flight type (`KIND_SQL`): cargo, government, private, business (by aircraft type, `BUSINESS_TYPES`), passenger (airline callsign), other
- `/flights/api/describe?callsign=&make=&model=&icao_type=&lat=&lon=&track=&vr=` names an aircraft in the air now for garage_pi's plane display: airline, type (same rules as stored flights) and the route leg it's on. A listed leg is only used if the aircraft is near its great-circle path and heading for its destination, since the route list can be stale. `route_airports` also tries the flight number without leading zeros (CPA075 → CPA75). `iata.json` (`flights/build_iata.py`, OurAirports, public domain) gives IATA codes and cities
- `airlines.json` is built by `flights/build_airlines.py` from OpenFlights' `airlines.dat`, with local fixes in `OVERRIDES`; `airports.json` is a copy of garage_pi's

## Car counter

- http://sdrpi.local:8080/cars/ (also http://sdrpi.local:8095/cars/ directly). Counts cars passing porch_pi from their tire pressure sensors (TPMS). porch_pi only runs rtl_433 and POSTs each decoded message, with its own timestamp, to `/cars/api/ingest` on port 8095; see porch_pi's CLAUDE.md
- Source in `cars/`, deployed to `/mnt/data/cars/app`, unit `carstore` (runs as `pi`), log `/mnt/data/logs/carstore.log`. The scanner proxies `/cars/` and `/cars/api/<name>` (GET, and POST for `band`) to `127.0.0.1:8095`. Ingest goes straight to port 8095 so it doesn't depend on the scanner
- `/mnt/data/cars/tpms.db` (vehicles, sensors, passes, readings, decodes, links) kept 365 days; `key` (HMAC key for sensor hashes; moved from porch_pi, never published); `settings.json` (band handed to porch_pi in each ingest reply)
- A pass is one sensor type's messages with gaps under `PASS_GAP_S` (60 s by message time; an open pass closes 60 s after its last message arrived). Sensors heard together become one car; two cars heard together on `MERGE_AFTER` (2) trips merge and `retally` redoes their passes. Passes heard 3 min or more are "stopped nearby" and not counted
- `model` is rtl_433's protocol name, not the car's make ("Sensor type" on the page); readings outside 15-90 psi or −40-80 °C are shown as –. Low tires: under `LOW_PSI` (28 psi), each tire's latest believable reading per period
- Sensors are keyed by a hash of type and ID with the raw ID in `sensors.ident` (kept since 11 Nov 2026 at the owner's request). IDs identify vehicles: the data stays on the LAN and out of git

## Weather

- http://sdrpi.local:8080/weather/ (proxied to the car store, GET `/weather/api/<name>` and POST `/weather/api/station`; `cars/www/weather.html`), linked from every page on both Pis and both status pages. The dashboard's name (see `CLAUDE.local.md`) is a setting in `/mnt/data/weather/settings.json`, not in the code, so the neighbourhood stays out of the repository; the default is "Weather"
- Main station: a LaCrosse TX141TH-Bv2 (ID 10, channel 0); its battery-low flag has been set since 11 Nov. It transmits every 50 s. With porch_pi on band `315w`, porch_pi stays on 315 MHz for tires and leaves every 15 minutes for a 75-second check on the stations' frequencies (`Weather.freqs`, sent in each ingest reply as `weather_freqs`; 433.92 MHz until a station elsewhere is confirmed), so one reading lands every 15 minutes. Once a week, Sunday at 3 am, it searches 433.92 and 915 MHz for 10 minutes each for new stations. porch_pi posts within 3 s; the page refreshes every 60 s and shows the schedule porch_pi last reported
- The temperature and humidity charts plot every reading of the main station (`/weather/api/readings?days=`, `[t, temp_c, humidity]`), one dot each, with the line broken after 45 minutes without one; the daily table still uses `daily`
- Readings are stored in °C; the °C/°F switch only changes the display and is remembered per browser (`localStorage` `weather-unit`). The chart's dashed line marks freezing in either unit
- `cars/weather.py`, run inside the car store: `ingest` hands every non-TPMS message carrying a weather measurement (`MEASURES`: temperature, humidity, wind, rain, pressure, UV, light) to `Weather.store`, so stations are discovered on their own. It keeps every reading (repeats within 3 s of the same sensor dropped; sensors send each one in a burst) in `/mnt/data/weather/weather.db` (`sensors` with `name` and `measures`, `readings` with the raw JSON), never pruned. The Stations card lists them all for naming and picking the dashboard's station; one heard fewer than `MIN_READINGS` (3) times is shown as waiting, since it's likely a misdecode. The main station is the chosen one, else the one with temperature heard most
- The car store runs `python3 -I`, which leaves its folder off the import path, so `carstore.py` adds it before `import weather`. Its unit may write `/mnt/data/weather` as well as `/mnt/data/cars`
- 11-12 Nov's readings were imported from porch_pi's night survey file

## Night band survey

- `nightsurvey/sweep.py OUT_DIR` runs `hackrf_sweep` over 24-1800 MHz in 100 kHz bins and appends each minute's peak and mean power (float16 rows, `peak.f16`, `mean.f16`, times in `minutes.txt`, layout in `bins.txt`). It needs the HackRF, so the scanner is stopped while it runs
- `nightsurvey/sigcap.py OUT_DIR` takes the radio for a few minutes to record short IQ files of chosen signals (`TARGETS`); `sigan.py` finds signals, widths, bursts and modulation in them; `sigdsd.py` runs DSD-FME over a channel to name the digital system. 12 Nov results: 410.2125 MHz is a P25 Phase 1 trunked control channel (WACN BEE00, system 1AC, NAC 1A1, Motorola) granting 411.1625, 413.4625 and 413.5875; the :02/:32 burst is at 410.0625, the Metrolinx GO Transit P25 entry in the channel list
- First run 11-12 Nov 2026 into `/mnt/data/nightsurvey/2026-11-11`, as transient units `night-sweep` (until 06:59:30) and `night-restore` (07:00, stops the sweep and starts `sdrpi-scanner`); porch_pi ran rtl_433 on 315/433.92/915 MHz into `/home/pi/night/` at the same time

## Weather satellites

- `satwx/satwx.py` (deployed to `/mnt/data/satwx/app`) records Meteor-M2 3 and M2 4 LRPT passes. NOAA-15/18/19 are retired, so these are the only 137 MHz weather satellites. SatDump 1.2.2 and python3-skyfield come from apt
- `satwx.py passes 24` lists passes; `satwx.py schedule FROM TO` (local `YYYY-MM-DDTHH:MM`) makes a transient `satwx-YYYYMMDD-HHMM` timer per pass peaking at 10° or more, firing 2 min before rise. Windows are kept in `/mnt/data/satwx/windows.json`, and the enabled `satwx-restore.service` (`satwx/satwx-restore.service`) sets the timers again at boot, since transient timers don't survive a reboot (a power blip at 10:00 on 12 Nov lost them)
- A pass skips itself if someone is listening or the scanner has a job still running 3 min after rise; otherwise it stops `sdrpi-scanner`, records horizon to horizon with `hackrf_transfer` (2 MS/s cs8 centred on 137.5 MHz, so 137.1 and 137.9 both fit), starts the scanner again, then makes `waterfall.png` and `levels.json` and runs SatDump on whichever channel rose 3 dB or more
- Each pass is a folder in `/mnt/data/satwx/passes` with `meta.json` (sky track, outcome); log `/mnt/data/satwx/satwx.log`. Raw recordings (about 240 MB a minute) are capped at 20 GB, oldest first
- The 137.1 MHz bursts in the 11-12 Nov night survey were VDL2 (136.975 MHz), not satellites: no Meteor pass was heard in that survey

## Status page

- http://sdrpi.local:8080/status/, linked from every page; `/status/alerts.js` adds a banner to every page while this Pi or garage_pi has an alert
- Source in `status/` (shared with `garage_pi/status/`; only `config.json` and the service differ). Deployed to `/mnt/data/pistatus/app`, unit `pistatus` (runs as `pi` with `NoNewPrivileges`), writes `/run/pistatus/status.json` each minute, history in `/mnt/data/pistatus/history.json`, log `/mnt/data/logs/pistatus.log`
- Reboot and shut down: the page POSTs to `/status/api/power`, which the scanner forwards to pistatus on `127.0.0.1:8093`. pistatus only writes `/run/pistatus/power-request`; `pistatus-power.path` runs `/usr/local/lib/pistatus/power-action.sh` as root, which accepts only `reboot`, `poweroff` or `test`. Shutting down needs the Pi's name typed. Test with `{"action":"test"}`, never a real action

## Bluetooth tracker

- Passive BLE tracker copied from `garage_pi/tracker`; source in `bluetooth/`, deployed to `/mnt/data/bletracker/app`, unit `bletracker`
- Page at http://sdrpi.local:8080/bluetooth/, served by the scanner; each page links to the other
- `/mnt/data/bletracker/tracker.db` keeps devices and sessions 90 days; `/run/bletracker/data.json` is rewritten each minute; log at `/mnt/data/logs/bletracker.log`
- Device names come from anything in radio range, so the page must only insert data with `textContent`
- Rotating addresses are only counted per manufacturer. Don't persist or display them

## Wi-Fi tracker

- Passive tracker on the USB dongle (RTL8188CUS, 2.4 GHz only); source in `wifi/`, deployed to `/mnt/data/wifitracker/app`, unit `wifitracker`
- Page at http://sdrpi.local:8080/wifi/, served by the scanner; all three pages link to each other
- Hops channels 1, 6 and 11 only, 2 s each: at 0.35 s the dongle silently stopped receiving within minutes while the service stayed active. After 90 s without a frame the tracker exits so systemd restarts it (logged as "no frames")
- `/mnt/data/wifitracker/tracker.db` kept 90 days; `/run/wifitracker/data.json` rewritten each minute; log at `/mnt/data/logs/wifitracker.log`
- `wlan0` (built-in) carries the Pi's network connection. Never put it in monitor mode. The dongle is found by driver and left unmanaged by `/etc/NetworkManager/conf.d/90-wifi-monitor-dongle.conf`
- Network names are broadcast by anything in range, so the page must only insert data with `textContent`
- Only frame headers and management data are read. SSIDs in probe requests are never read. Randomized client addresses stay in memory and are only counted

## Porch versus attic comparison

- Source in `comparison/`, deployed to `/mnt/data/comparison`. `attic-compare.timer` builds `report.txt` there once at 2026-11-10 17:05, comparing 05:00-17:00 on 9 Nov (porch) and 10 Nov (attic) using only minutes clean on both days
- `atis_reading.py <label>` appends a signal reading on two constant broadcasts to `atis.jsonl`; it briefly takes over the radio and skips itself if someone is listening
- Don't change scanner, Wi-Fi or Bluetooth settings until the report has run

## Storage

- Attached 60GB USB SSD is data storage, mounted at `/mnt/data`
- All databases, logs and recordings for this project go under `/mnt/data`
- Mounted by UUID in fstab with `nofail` (the UUID is in `CLAUDE.local.md`)
- log2ram keeps `/var/log` in RAM (128M) to reduce SD card writes
