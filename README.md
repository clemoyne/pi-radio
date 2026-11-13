# pi-radio

Three Raspberry Pis that listen to the radio around a house in Toronto and put what they hear on web pages on the local network. Everything is receive-only: nothing here transmits.

| Folder | Pi | What it does |
|---|---|---|
| [`sdrpi/`](sdrpi/) | Raspberry Pi 4 with a HackRF One, a USB Wi-Fi dongle and a USB SSD | Web scanner with live listening, auto-recorded clips, DMR/P25 decoding and ACARS messages; Bluetooth and Wi-Fi presence trackers; a long-term flight dashboard; a status page |
| [`garage_pi/`](garage_pi/) | Raspberry Pi 3 A+ running FlightAware's PiAware | Aircraft log built on PiAware's data (go-arounds, emergency squawks, cargo, watch list); Bluetooth tracker; status page |
| [`porch_pi/`](porch_pi/) | Raspberry Pi 2 with a Nooelec RTL-SDR facing the street | Decodes tire pressure sensors for the car counter, and wireless weather stations, and forwards them to sdrpi |

Each folder's `CLAUDE.md` is the detailed reference: what runs where, file locations, how to deploy and how to remove things. They were written as working notes for Claude Code, which built this, so they're terse but complete.

## Highlights

- **Scanner** (`sdrpi/scanner`): a band monitor sweeps the bands and ranks a channel list built from RadioReference exports; click a channel to listen in the browser. Auto-records short clips, manual and continuous recording, and an "only record transmissions" filter. Digital voice through DSD-FME (conventional DMR and P25 Phase 1, no trunking, encrypted stays muted).
- **Go-arounds**: the aircraft log spots airliners going around at Pearson or Billy Bishop and tells sdrpi, which records the departures frequency for five minutes, then cuts out the static between calls (the full recording is kept too).
- **ACARS mode**: acarsdec with libacars decodes the text messages airliners exchange with their airlines on six North American channels, or dumpvdl2 decodes VDL2, the digital datalink that now carries most of that traffic. Messages are explained in plain words, including position reports and pilots texting air traffic control (CPDLC). Emergency squawks start it automatically; go-arounds interrupt it. Both can be turned off.
- **Collections**: overnight, the HackRF sits on a 2 MHz block (railway or business radio), watches every channel in it at once and keeps whole conversations, only the transmissions that sound like speech, until a set amount is collected.
- **Weather satellites** (`sdrpi/satwx`): predicts Meteor-M passes, takes the radio for each one, then makes a waterfall and runs SatDump on the recording.
- **Signal hunting** (`sdrpi/nightsurvey`): an overnight sweep of 24-1800 MHz, plus tools to record unknown signals briefly, measure them and name digital systems with DSD-FME.
- **Flights dashboard** (`sdrpi/flights`): sdrpi pulls finished flights from the aircraft log onto its SSD (capped at 10 GB) and works out takeoffs, landings, flights over the house, aircraft types, cargo carriers and routes, with maps of each flight path. It times how much each go-around added, spots diversions (flights that turned back or went somewhere other than usual), and marks VIP aircraft such as the Prime Minister's and Governor General's RCAF jets, royal flights and Air Force One. A leaderboard shows the planes seen most, and every list says how many times each aircraft has been seen before.
- **Plane display** (`garage_pi/aircraft/www/display`): a full-screen dot-matrix LED panel for a spare screen, showing the planes passing nearby with airline, route, type, altitude, speed and heading.
- **Car counter** (`porch_pi/tpms` decodes, `sdrpi/cars` stores and shows): rtl_433 decodes the tire pressure sensors of passing cars; sensors heard together become one car, so it counts cars per hour and spots repeat trips. It keeps each sensor's ID with a pressure and temperature history, so trends like slow leaks or a car's usual times show up; that data stays on sdrpi's SSD. It also counts low tires. Tire sensors send about once a minute, so it counts a sample of the traffic, not all of it.
- **Weather** (`sdrpi/cars/weather.py`, page at `/weather/`): finds any wireless weather sensor or station in range on its own (temperature, humidity, wind, rain, pressure, UV, light), keeps every reading, and charts temperature and humidity by hour or day in °C or °F. Stations can be named, and the dashboard's name is set on the page. porch_pi checks the stations every 15 minutes between tire sensor listening, and searches 433.92 and 915 MHz for new ones weekly.
- **Trackers**: passive Bluetooth LE and 2.4 GHz Wi-Fi presence trackers, by device type and likely distance. Rotating and randomized addresses are only counted, never stored.
- **Status pages** on both Pis, with an alert banner on every page and guarded reboot and shutdown buttons.

## Setting it up for your own place

This was built for one house, so expect to adapt it:

1. **Your location**: copy `sdrpi/flights/local.example.json` to `local.json` next to it and set your receiver's position.
2. **Your channels**: `sdrpi/scanner/build_channels.py` turns RadioReference CSV exports into `sdrpi/scanner/channels.json`, which the scanner needs. No frequency list is included, since RadioReference's terms don't allow redistributing it: export your own area as `Toronto.csv` (the conventional list) and `GTA_Frequencies_Full_List.csv` (the wider one) in `sdrpi/`, or rename them in the builder. Copy `sdrpi/scanner/places.example.json` to `places.json` and list places named in your exports with their rough distance from your scanner.
3. **Host names and paths**: the Pis are `sdrpi.local` and `piaware.local`, with sdrpi's data on an SSD at `/mnt/data`. Search for those if yours differ, especially in the two `status/config.json` files.
4. **Decoders and lookup data**: DSD-FME, mbelib, acarsdec, libacars and dumpvdl2 are built from source; SatDump comes from Debian on the Pi, and the lookup databases are rebuilt with the `build_*.py` scripts. The `CLAUDE.md` files say where each comes from and how it's built.
5. **Services**: each component has a systemd unit next to its code. The `CLAUDE.md` files list what goes where.

There's no installer; it's a working project, not a product.

## Data and credits

- Frequency lists: bring your own export from [RadioReference](https://www.radioreference.com/); none is included.
- Airlines and aircraft type names: [OpenFlights](https://openflights.org/data.html), Open Database License (ODbL).
- Airports: [OurAirports](https://ourairports.com/data/), public domain.
- Flight routes: [Virtual Radar Server standing data](https://github.com/vradarserver/standing-data), CC0.
- Aircraft registries: Transport Canada and the FAA.
- Map: [OpenLayers](https://openlayers.org/) (BSD 2-clause), copied in `ol/`, with OpenStreetMap tiles loaded in the viewer's browser.
- Built on PiAware and dump1090-fa, DSD-FME and mbelib, acarsdec, libacars and dumpvdl2, SatDump, rtl_433, and the HackRF and RTL-SDR tools.
- RF Rogue icon: the project's own logo.

## Licence

The code is under the [MIT licence](LICENSE). The bundled data files keep their own licences, listed above: `airlines.json` and `types.json` are derived from OpenFlights under the ODbL, and OpenLayers is BSD 2-clause.

## The legal bit

Listening is generally legal in Canada, but the Radiocommunication Act bars sharing or using what you intercept from radio communications not meant for the public. Keep recordings and decoded messages to yourself. The Bluetooth, Wi-Fi and tire-sensor trackers only read what devices broadcast openly, and tire sensor IDs identify a vehicle, so keep that data private too; check your own local rules before running them.
