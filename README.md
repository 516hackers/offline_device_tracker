# Offline Group Tracker

An Android app for a 4–5 person group (e.g. a mountain trip) to see each
other's GPS location **with zero internet / zero mobile signal**, using the
same APK on every phone.

## What's new: Optional Internet Sync

The app is still fully **offline-first** — everything below the original
section still works exactly as before, with zero internet, zero server, and
zero configuration. On top of that, it can now *optionally* sync through the
internet when a member has it, so someone who has wandered out of local
Wi-Fi mesh range can still show up for the rest of the group once they get
signal. See "OPTIONAL INTERNET SYNCHRONIZATION" further down for the full
explanation, and `INTERNET_SYNC_API.md` for the backend spec. Internet is
never required to start the app, get GPS, or use the local mesh.

## What actually works in this build (read this first)

This app is built with pure Python (Kivy + Plyer), which is a deliberate
constraint. Some things Android exposes are **only** reachable through native
Java APIs, not through Kivy/Plyer. Here's the honest breakdown:

| Feature | Status | Notes |
|---|---|---|
| GPS location acquisition | ✅ Real | via Plyer, configurable interval per battery mode |
| Local storage (SQLite) | ✅ Real | members, location history, packets, sync state |
| Wi-Fi LAN / hotspot transport (UDP broadcast) | ✅ Real | works over a plain router **or** one phone's hotspot with internet turned off — no internet uplink needed, just a shared local network |
| Store-and-forward, multi-hop relay, TTL, dedup | ✅ Real | runs on top of the Wi-Fi transport above |
| Foreground service + persistent notification | ✅ Real (Android side) | keeps tracking alive with screen locked |
| Permission handling | ✅ Real | requests at runtime, degrades gracefully on denial |
| **Internet Sync (optional)** | ✅ Real, additive | HTTPS upload/download against a server you configure; app works with this fully off |
| **Bluetooth Classic** | ❌ Not implemented (stub) | needs native `BluetoothServerSocket`/RFCOMM, not available in pure Python |
| **BLE (advertise + scan)** | ❌ Not implemented (stub) | needs native `BluetoothGattServer`/`BluetoothLeAdvertiser` |
| **Wi-Fi Direct** | ❌ Not implemented (stub) | needs native `WifiP2pManager` |
| Offline map tiles | ⚠️ Not included in v1 | architecture leaves a clean slot for it (see below) |

The app **does not** fake Bluetooth/BLE/Wi-Fi Direct with UDP and call it
"Bluetooth mesh." It has one real transport (Wi-Fi/hotspot UDP broadcast)
wired into a real mesh router (multi-hop relay + store-and-forward + dedup +
TTL). The three native transports are present in the code as a documented,
polymorphic stub (`UnavailableNativeTransport` in `main.py`) so that a future
native Android module can be dropped in later without touching GPS, storage,
or mesh logic — but as shipped, they always report "unavailable," and the
app correctly falls back to Wi-Fi only.

**Practical consequence for your mountain trip:** for phones to reach each
other, they need to be on the same local Wi-Fi network. The simplest way to
do this with no internet at all: one phone turns on its Wi-Fi hotspot (mobile
data/internet OFF), and every other phone joins that hotspot's Wi-Fi. No
internet connection is required or used — it's purely a local link-layer
network. Range is normal Wi-Fi hotspot range (roughly 20–50m depending on
terrain), extended by relaying through phones in between.

### If you truly need Bluetooth/BLE/Wi-Fi Direct range and hopping
That requires writing a small native Android module (Java/Kotlin) using
`BluetoothAdapter`/`BluetoothGattServer`/`WifiP2pManager`, compiled into the
APK as a python-for-android recipe or accessed via `pyjnius`, implementing
the same `BaseTransport` interface (`is_available`, `start`, `stop`, `send`)
used in `main.py`. That's a legitimate follow-up project — it's out of scope
for "simple Python/Kivy/Buildozer," which is what was requested here.

## Files in this repository

- `main.py` — all application logic (GPS, storage, transport, mesh, internet sync, battery modes, UI)
- `buildozer.spec` — Android build configuration (permissions, API levels, requirements) — unchanged, `INTERNET` permission was already present
- `requirements.txt` — Python build-time dependencies — unchanged; internet sync uses only the Python standard library (`urllib`, `ssl`), no new packages
- `.github/workflows/build-apk.yml` — GitHub Actions workflow that builds the APK — unchanged
- `INTERNET_SYNC_API.md` — backend API specification for the optional sync server
- `README.md` — this file

## 1. Create the GitHub repository

1. Create a new **public or private** GitHub repository, e.g. `offline-group-tracker`.
2. Upload these files preserving the folder structure:
   ```
   main.py
   buildozer.spec
   requirements.txt
   .github/workflows/build-apk.yml
   README.md
   ```
   (Add an `icon.png` — any 512x512 PNG — or remove the `icon.filename` line
   in `buildozer.spec` to use the default Kivy icon.)
3. Commit and push to the `main` branch.

## 2. How GitHub Actions builds the APK

Pushing to `main` triggers `.github/workflows/build-apk.yml`, which:

1. Checks out the repo.
2. Installs Java 17, Python, and native build tools Buildozer needs.
3. Installs Buildozer + Cython.
4. Runs `buildozer android debug` — this is the actual step that downloads
   the Android SDK/NDK, compiles a Python interpreter for the phone's ABI via
   `python-for-android`, bundles your code and dependencies, and produces a
   real `.apk` file. (Just running `main.py` does **not** produce an APK —
   that only happens on a real Android device/emulator with Kivy installed
   and even then only runs the app, it doesn't package one.)
5. Uploads the resulting `.apk` as a workflow artifact.

First build typically takes 15–30 minutes (downloading SDK/NDK); later builds
are faster due to caching.

## 3. Where to download the APK

- Go to your repo → **Actions** tab → click the latest successful **Build
  Android APK** run → scroll to **Artifacts** → download
  `offline-group-tracker-apk` (a zip containing the `.apk`).

## 4. Install it on 4–5 Android phones

1. Copy the `.apk` to each phone (via cable, Bluetooth file transfer, or a
   shared local folder — no internet needed for this step either).
2. On each phone: Settings → allow "Install unknown apps" for the file
   manager/app you're using to open the APK (only needed because it isn't
   from the Play Store).
3. Tap the APK file → Install.
4. Repeat for all 4–5 phones — **same APK file on every phone**.

## 5. Permissions users must allow

On first launch, allow:
- **Location** → "Allow all the time" (needed for background tracking with
  screen locked; the app still works with "While using the app" but will
  pause GPS in the background).
- **Nearby devices / Wi-Fi** related prompts (varies by Android version).
- **Notifications** (Android 13+) — needed to show the persistent "tracking
  active" notification required for the foreground service.

If a permission is denied, the app does **not** crash — the dependent
feature (e.g. GPS fixes, or background operation) simply won't function
until it's granted from Settings.

## 6. Create or join a group

On first launch each phone shows a **Join/Create Group** popup:
- Enter **Your Name** (e.g. "Ali").
- Enter a **Group Name** (e.g. "Mountain Trip 2026") — for your own reference.
- Enter a **Group ID** (e.g. `MT-8472`). **Everyone in the group must type
  the exact same Group ID** on their phone — this is the local, serverless
  equivalent of "joining a group." There is no central server; the Group ID
  is only used locally to label/filter your own view.

## 7. How offline communication works

1. Every phone's GPS produces its own location fix on a timer.
2. That fix is wrapped into a small JSON packet (device ID, name, lat/lon,
   timestamp, sequence number, battery %, transport used, packet ID, TTL).
3. The **Transport Manager** broadcasts that packet on every transport that
   is currently available (in this build: Wi-Fi LAN/hotspot UDP broadcast).
4. Any other phone on the same local Wi-Fi network receives it directly.
5. Phones **out of direct Wi-Fi range** don't receive it directly — but a
   phone that *is* in range of both will re-broadcast (relay) it, up to a
   hop limit (TTL), so it can reach further members through the chain
   (A↔B↔C↔D↔E).

## 8. What happens when phones are out of range

- GPS keeps working locally regardless — it doesn't need any other phone.
- If NO transport can currently reach anyone, the phone keeps recording its
  own location history in SQLite and keeps trying to broadcast; nothing is
  lost.
- Other phones' UI shows that member's **last known location** with a
  "Last seen X minutes ago" label instead of a live position, and marks them
  as "Lost contact" after 15 minutes with no packet.
- **Important:** GPS itself has no communication range — only the transport
  does. If two phones are far enough apart that no chain of in-range phones
  connects them, live location genuinely cannot be delivered until someone
  in between (or one of the two) moves back into range.

## 9. How store-and-forward works

Every phone stores every packet it has ever received (own SQLite database).
Every ~20 seconds it re-broadcasts a batch of the most recent packets it's
holding — including ones that originated from other members, not just its
own. If Phone C was out of range of Phone A when a location was first sent,
but later comes into range of A (directly or through a relay), C will
receive the missed packets and update A's view for that member the next time
A hears from C. Duplicate delivery is filtered by `packet_id`; infinite
relay loops are prevented by decrementing TTL/hop count on every relay and
dropping packets once it hits zero.

## 10. How battery-saving mode works

Four modes, changed from the UI buttons, each just changes the GPS polling
interval (it does not turn off transports):

| Mode | GPS interval |
|---|---|
| NORMAL | 30 seconds |
| BATTERY SAVING | 3 minutes |
| EXTREME BATTERY | 12 minutes |
| EMERGENCY | 7 seconds |

The app never keeps GPS polling faster than the selected mode, and never
forces Bluetooth/Wi-Fi to stay on beyond what Android itself needs for the
transport that's active — there's no continuous high-power scanning loop.

## Offline map (v1 status)

Not included in this version. The code is structured so a map layer can be
added later as an independent module (e.g. rendering pre-downloaded MBTiles
via `kivy-garden.mapview` with a local tile source) without touching GPS,
storage, transport, or mesh code — none of those modules know or care
whether a map is displayed.

## OPTIONAL INTERNET SYNCHRONIZATION

### The two situations, and hybrid mode

**1. Completely offline.** Nothing changes from the original app: GPS keeps
producing fixes on the configured interval, `WifiLanTransport` broadcasts and
relays over the local mesh, `SQLite` keeps every record, and the UI updates
from local data. No internet check ever blocks this path.

**2. Internet becomes available for one member.** Say Bilal walks out of
Wi-Fi mesh range of Ali and Ahmed, but later gets a mobile data signal.
His phone's `InternetSyncTransport` (running on its own background timer,
independent of the mesh) notices the server is reachable, uploads whatever
is sitting in his local `pending_sync` queue, and downloads the group's
latest known locations. When Ali's phone later also has internet (or is
still fully offline and only ever sees this via the mesh relay — both are
fine), it applies Bilal's newer record and shows him as `🔵 INTERNET` with a
"Last sync" time, instead of his old mesh-based `Last seen`.

**3. Hybrid.** If a phone has both local mesh peers in range *and* internet
at the same time, both keep running simultaneously — the mesh keeps doing
its normal immediate, low-latency broadcast/relay; internet sync keeps doing
its slower, battery-aware periodic upload/download in the background. The
`TransportManager` just reports both as available; nothing about the mesh
path changes because internet also happens to exist.

### Architecture

```
TransportManager
    ├── WiFiLanTransport        (existing, real)
    ├── UnavailableNativeTransport("bluetooth_classic")  (existing stub)
    ├── UnavailableNativeTransport("ble")                (existing stub)
    ├── UnavailableNativeTransport("wifi_direct")         (existing stub)
    └── InternetSyncTransport   (NEW, optional, additive)
```

`InternetSyncTransport` is intentionally **not** part of the mesh's
immediate per-packet `broadcast()` fan-out — internet uploads happen on
their own timer (interval driven by the active battery mode), not once per
GPS fix, so a live mesh session doesn't turn into a stream of HTTP requests.
`MeshRouter.emit_location()` does two independent things for every fix: (1)
broadcast it over local transports as before, and (2) queue it in
`pending_sync` for the sync loop to pick up later — nothing about (1)
depends on (2) succeeding, failing, or existing at all.

### Offline queue & retry behavior

- Every outbound location is queued into a SQLite `pending_sync` table.
- A local row (`packets` table) is **never deleted** on sync failure — only
  the `pending_sync` queue entry is removed, and only after the server
  explicitly returns its `packet_id` in `acknowledged_packet_ids`.
- Failed/unreachable uploads reschedule with **exponential backoff**
  (starts at 15s, doubles each failure, capped at 10 minutes, with a little
  random jitter) — the app does not hammer a dead server or drain the
  battery retrying constantly.
- The sync loop itself only runs once per battery-mode interval (see table
  below), and does a cheap `/health` reachability check before attempting
  any upload/download.

### Conflict resolution (newest wins)

Every record carries `timestamp` and a per-device `sequence` number.
`Storage.apply_remote_record()` only accepts an inbound server record if its
`sequence` is strictly higher than the highest one already stored locally
for that `device_id` — an older server record can never overwrite a newer
local (mesh-received) one, and vice versa: the same rule governs which
record `latest_per_member()` shows in the UI. Duplicate `packet_id`s
(re-delivered by the server, or re-relayed by the mesh) are ignored safely
either way.

### UI: LIVE/LOCAL vs REMOTE/INTERNET vs LAST KNOWN

Each group member row shows one of:
- **🟢 LOCAL / MESH** — a recent record arrived over the local Wi-Fi mesh.
- **🔵 INTERNET** — a recent record arrived via internet sync (member is out
  of local mesh range but reachable through the server).
- **🟡 LAST KNOWN** — neither source has heard from them recently (older
  than 15 minutes); their last known position is still shown.

The **INTERNET SYNC** panel additionally shows the phone's own state:
`Internet: 🟢 Connected / 🔴 Offline`, `Sync: 🟢 Synced / 🟡 Pending / 🔴 Failed
/ ⚪ Disabled`, `Last Sync` time, and how many `Pending` records are still
queued.

### Battery behavior

| Mode | GPS interval | Internet sync interval |
|---|---|---|
| NORMAL | 30s | 60s |
| BATTERY SAVING | 3 min | 5 min |
| EXTREME BATTERY | 12 min | 15 min (latest fix only) |
| EMERGENCY | 7s | 10s (near-immediate while active) |

Changing battery mode from the UI affects both GPS polling and the sync
loop's interval automatically (the sync loop reads the current mode on each
iteration) — there's no separate setting to keep in sync.

### Turning Internet Sync ON/OFF (privacy)

A toggle button in the UI (`Internet Sync: ON/OFF`) flips a persisted
setting (`sync_config.json`). When **OFF**, `InternetSyncTransport.is_available()`
always reports `False`, nothing is ever uploaded or downloaded, and the app
is — as always — fully functional offline. When **ON**, sync only actually
does anything once a server URL is configured *and* reachable; until then it
harmlessly reports `DISABLED`/`OFFLINE` and queues locally, same as if a
member simply never gets signal on the whole trip.

### Configuring the server URL (no secrets in source)

Nothing sensitive is hardcoded in `main.py`. Configuration is resolved in
this order:

1. **Environment variables** (useful for CI/testing or if you set them via
   `buildozer.spec`'s `[app] android.env` or a launcher script):
   ```bash
   export OFFLINE_TRACKER_SYNC_URL="https://your-sync-server.example.com"
   export OFFLINE_TRACKER_API_KEY="your-group-or-device-token"
   export OFFLINE_TRACKER_SYNC_ENABLED="true"
   ```
2. **A local config file** at `~/.offline_tracker/sync_config.json` on
   desktop, or `/sdcard/OfflineTracker/sync_config.json` on Android:
   ```json
   {
     "server_url": "https://your-sync-server.example.com",
     "api_key": "your-group-or-device-token",
     "sync_enabled": true
   }
   ```
3. **Built-in default**: `server_url` is blank, meaning internet sync stays
   disabled/unreachable until you configure one of the above — this is a
   normal, supported state, not an error.

See `INTERNET_SYNC_API.md` for the exact endpoints (`/health`,
`/locations/upload`, `/locations/group/{group_id}`) and JSON payloads a
backend needs to implement. No specific commercial service is required or
assumed — point it at anything that implements that spec.

### Security & privacy

- **HTTPS only** — `InternetSyncTransport` always builds requests through
  `ssl.create_default_context()`; there is no plaintext HTTP path.
- **Group + device authentication** — every request includes `group_id`,
  `device_id`, and a bearer token; see `INTERNET_SYNC_API.md` for how a
  backend should scope access so only paired group members can read/write a
  group's locations, and a removed/random device is rejected.
- **No third-party analytics** — the only outbound network calls this app
  ever makes are to the one server URL you configure, for the two purposes
  described above. Nothing is sent anywhere else.
- **Local data stays local by default** — with Internet Sync OFF (the
  privacy toggle), no location data ever leaves the device except over the
  local mesh to paired group members, exactly as in the original offline
  design.

### What happens when a member goes out of range, then gets internet, then loses it again

1. **Out of range:** their phone keeps recording GPS locally and keeps
   trying the local mesh; nothing is lost, nothing crashes, no internet
   check blocks this.
2. **Gets internet:** their `InternetSyncTransport` loop notices on its next
   tick (within one sync interval for the current battery mode), uploads the
   queued backlog, and downloads anything the rest of the group has posted.
   Other members see them flip from `🟡 LAST KNOWN` to `🔵 INTERNET` once
   their own sync loop (or a mesh relay, if back in range) picks it up.
3. **Loses internet again:** the sync loop's reachability check simply
   starts failing again; it backs off exponentially instead of retrying
   constantly, GPS and local mesh are completely unaffected, and the last
   synced position remains visible, aging toward `🟡 LAST KNOWN` after 15
   minutes with no update from either source.

## Testing on desktop before building for Android

`main.py` also runs directly with `python main.py` on a desktop with Kivy
installed (`pip install kivy`). Without Plyer's Android GPS, it falls back to
a simulated moving location so you can test the UI, storage, and mesh logic
(run two copies on the same machine/network to see them talk to each other
over the real Wi-Fi UDP transport).
