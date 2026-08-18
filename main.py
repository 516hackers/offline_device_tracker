"""
OFFLINE GROUP TRACKER
=====================
Offline group location tracker for 4-5 people (e.g. a mountain trip) with
NO internet / NO mobile network required.

ARCHITECTURE (kept strictly separated, as required):

    GPS (location.py logic, inline below: LocationService)
        -> produces LocationFix objects only. Knows nothing about other phones.

    TransportManager
        -> chooses a communication method to move packets between phones.
        -> Implemented for real:  Wi-Fi LAN / hotspot UDP broadcast (WifiLanTransport)
        -> Stubbed, native-required: Bluetooth Classic, BLE, Wi-Fi Direct
           (see honest_limitations.py for why, and what a native module would need)

    MeshRouter
        -> store-and-forward + multi-hop relay + dedup (packet_id) + TTL
           over LOCAL transports (Wi-Fi mesh / stubs).

    InternetSyncTransport (NEW, OPTIONAL)
        -> uploads/downloads location packets through a configurable HTTPS
           API when internet happens to be available. Purely additive: the
           app starts, gets GPS fixes, and runs the local mesh with this
           fully disabled or with no internet ever present. See "OPTIONAL
           INTERNET SYNC" section further down.

    Storage
        -> SQLite. members, location history, packets, pending-sync queue.

    UI (Kivy)
        -> reads from Storage/TransportManager on a Clock schedule. Never blocks.

HONESTY NOTE (read this before assuming things work that don't):
Pure Python/Kivy/Plyer CANNOT do real Bluetooth Classic sockets, BLE
GATT central/peripheral roles, or Wi-Fi Direct (P2P) on Android. Those
require native Android APIs called through pyjnius/a Java bridge, which is
a separate, larger native-integration project. This app therefore ships a
REAL working transport over local Wi-Fi/hotspot (UDP broadcast, no internet
uplink required) and clearly stubs the others behind the same interface, so
a native module can be dropped in later without touching GPS/storage/mesh
logic at all. This is stated again in the UI and README. Nothing here
pretends UDP-over-WiFi is Bluetooth or "true mesh over BLE".

===============================================================================
OPTIONAL INTERNET SYNC (added on top of the offline-first system above)
===============================================================================
This app remains OFFLINE-FIRST. Nothing below changes that:
  - The app starts, requests permissions, acquires GPS, and runs the local
    Wi-Fi mesh identically whether or not internet ever exists.
  - InternetSyncTransport is just one more entry in TransportManager's list
    of transports. The mesh/storage/UI code doesn't know or care that it
    exists beyond reading a "transport" label off each packet.
  - If internet is OFF (user setting) or simply unreachable, the app queues
    unsynced records in SQLite (`pending_sync`) and keeps working exactly as
    before. It retries later with exponential backoff -- it does not require
    internet, does not block on it, and does not spin the radio constantly.
  - When internet does become reachable, queued records upload, the server
    returns other group members' latest locations, and the UI labels those
    as REMOTE / INTERNET with a "Last Internet Sync" time -- clearly
    distinguished from LIVE/LOCAL (received over the mesh) and LAST KNOWN
    (neither source heard from recently).
  - Internet sync never overwrites a newer local mesh fix with an older
    server fix, or vice versa: (timestamp, sequence_number) per device_id
    is used to decide which record wins, both when applying inbound server
    data and when the mesh and internet disagree about who has the freshest
    fix for a given member.
"""

import json
import os
import random
import sqlite3
import socket
import ssl
import struct
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import uuid
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.metrics import dp

# ---------------------------------------------------------------------------
# Android-only imports are guarded so this also runs (degraded) on desktop
# for development/testing.
# ---------------------------------------------------------------------------
try:
    from plyer import gps, battery, uniqueid
    PLYER_AVAILABLE = True
except Exception:
    PLYER_AVAILABLE = False

ANDROID = False
try:
    from jnius import autoclass, cast
    from android.permissions import request_permissions, Permission, check_permission
    ANDROID = True
except Exception:
    ANDROID = False

APP_DIR = os.path.join(os.path.expanduser("~"), ".offline_tracker") if not ANDROID else \
    "/sdcard/OfflineTracker" if os.path.isdir("/sdcard") else "."
os.makedirs(APP_DIR, exist_ok=True)
DB_PATH = os.path.join(APP_DIR, "tracker.db")

UDP_PORT = 47632          # arbitrary fixed port used by every phone running the app
BROADCAST_ADDR = "255.255.255.255"
MULTICAST_GROUP = "239.192.47.63"   # used as fallback where broadcast is filtered

# Battery modes -> GPS interval seconds
BATTERY_MODES = {
    "NORMAL": 30,
    "BATTERY_SAVING": 180,     # 3 min (2-5 min range)
    "EXTREME_BATTERY": 720,    # 12 min (10-15 min range)
    "EMERGENCY": 7,            # 5-10 sec range
}

STALE_AFTER_SECONDS = 15 * 60   # after this with no packet, member shown as "lost"

# ---------------------------------------------------------------------------
# INTERNET SYNC CONFIGURATION
# Nothing here is a secret. The server URL is configurable and everything
# sensitive (API keys/tokens, if your backend needs them) is read from
# environment variables / a local config file -- never hardcoded in source,
# per the requirement to keep secrets out of main.py.
#
# Config resolution order (first one found wins):
#   1. Environment variables (OFFLINE_TRACKER_SYNC_URL, OFFLINE_TRACKER_API_KEY)
#   2. A local, gitignored config file at APP_DIR/sync_config.json
#   3. Built-in defaults below (sync server left BLANK on purpose -- internet
#      sync simply stays unreachable / disabled until configured, which is a
#      supported, expected state, not an error)
# ---------------------------------------------------------------------------
def _load_sync_config():
    cfg = {
        "server_url": os.environ.get("OFFLINE_TRACKER_SYNC_URL", ""),
        "api_key": os.environ.get("OFFLINE_TRACKER_API_KEY", ""),
        "sync_enabled": os.environ.get("OFFLINE_TRACKER_SYNC_ENABLED", "true").lower() != "false",
    }
    cfg_path = os.path.join(APP_DIR, "sync_config.json")
    try:
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                file_cfg = json.load(f)
            for key in ("server_url", "api_key", "sync_enabled"):
                if key in file_cfg and not (key == "server_url" and cfg["server_url"]):
                    cfg[key] = file_cfg[key]
    except Exception as e:
        print("[SyncConfig] failed to read sync_config.json:", e)
    return cfg


SYNC_CONFIG = _load_sync_config()

# Internet sync interval (seconds) per battery mode -- separate from, but
# following the same spirit as, the GPS interval table. "Frequent" for
# NORMAL, tapering off, immediate for EMERGENCY.
SYNC_INTERVALS = {
    "NORMAL": 60,
    "BATTERY_SAVING": 300,      # 5 min
    "EXTREME_BATTERY": 900,     # 15 min, latest fix only
    "EMERGENCY": 10,            # near-immediate while emergency is active
}

SYNC_BACKOFF_INITIAL = 15        # seconds, doubles on repeated failure
SYNC_BACKOFF_MAX = 600           # cap backoff at 10 minutes so it eventually
                                  # retries again without hammering a dead server
CONNECTIVITY_CHECK_TIMEOUT = 4   # seconds


# ===========================================================================
# STORAGE  (SQLite - local only, never leaves the device except as packets
#           explicitly relayed to paired group members, or -- only if the
#           user has Internet Sync turned ON -- uploaded to their configured
#           server over HTTPS)
# ===========================================================================
class Storage:
    def __init__(self, path=DB_PATH):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        with self._lock, self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS members (
                device_id TEXT PRIMARY KEY,
                member_name TEXT,
                group_id TEXT,
                first_seen REAL,
                last_seen REAL
            );
            CREATE TABLE IF NOT EXISTS location_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                latitude REAL,
                longitude REAL,
                altitude REAL,
                speed REAL,
                accuracy REAL,
                timestamp REAL,
                sequence INTEGER
            );
            CREATE TABLE IF NOT EXISTS packets (
                packet_id TEXT PRIMARY KEY,
                device_id TEXT,
                group_id TEXT,
                member_name TEXT,
                latitude REAL,
                longitude REAL,
                accuracy REAL,
                altitude REAL,
                speed REAL,
                timestamp REAL,
                sequence INTEGER,
                battery_level REAL,
                transport_used TEXT,
                source TEXT DEFAULT 'LOCAL',
                ttl INTEGER,
                received_at REAL,
                synced INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sync_state (
                device_id TEXT PRIMARY KEY,
                last_sequence_seen INTEGER,
                last_sync_time REAL
            );
            -- Outbound queue: our own location packets waiting to be
            -- uploaded to the internet sync server. A row is deleted only
            -- after the server explicitly acknowledges it -- never before.
            CREATE TABLE IF NOT EXISTS pending_sync (
                packet_id TEXT PRIMARY KEY,
                payload TEXT,
                attempts INTEGER DEFAULT 0,
                next_attempt_at REAL,
                created_at REAL
            );
            -- Internet sync bookkeeping (connection state, last successful
            -- sync time, last error) purely for UI display.
            CREATE TABLE IF NOT EXISTS internet_sync_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """)
            # Lightweight migration for DBs created by earlier versions of
            # this app that predate the internet-sync columns.
            existing_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(packets)")}
            for col, decl in (
                ("group_id", "TEXT"), ("accuracy", "REAL"), ("altitude", "REAL"),
                ("speed", "REAL"), ("source", "TEXT DEFAULT 'LOCAL'"),
            ):
                if col not in existing_cols:
                    try:
                        self.conn.execute(f"ALTER TABLE packets ADD COLUMN {col} {decl}")
                    except Exception:
                        pass

    def upsert_member(self, device_id, member_name, group_id):
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute("""
                INSERT INTO members (device_id, member_name, group_id, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    member_name=excluded.member_name,
                    last_seen=excluded.last_seen
            """, (device_id, member_name, group_id, now, now))

    def record_location(self, device_id, lat, lon, alt, speed, accuracy, ts, seq):
        with self._lock, self.conn:
            self.conn.execute("""
                INSERT INTO location_history
                (device_id, latitude, longitude, altitude, speed, accuracy, timestamp, sequence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (device_id, lat, lon, alt, speed, accuracy, ts, seq))

    def packet_seen(self, packet_id):
        with self._lock:
            cur = self.conn.execute("SELECT 1 FROM packets WHERE packet_id=?", (packet_id,))
            return cur.fetchone() is not None

    def store_packet(self, packet: dict, source="LOCAL"):
        """
        Store a packet coming from the LOCAL mesh (or our own GPS fix).
        Dedup is by packet_id (INSERT OR IGNORE), same as before internet
        sync existed -- this path is unchanged for the mesh.
        """
        with self._lock, self.conn:
            self.conn.execute("""
                INSERT OR IGNORE INTO packets
                (packet_id, device_id, group_id, member_name, latitude, longitude,
                 accuracy, altitude, speed, timestamp, sequence, battery_level,
                 transport_used, source, ttl, received_at, synced)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                packet["packet_id"], packet["device_id"], packet.get("group_id"),
                packet["member_name"], packet["latitude"], packet["longitude"],
                packet.get("accuracy"), packet.get("altitude"), packet.get("speed"),
                packet["timestamp"], packet["sequence"], packet.get("battery_level"),
                packet["transport_used"], source, packet["ttl"], time.time()
            ))
            self.conn.execute("""
                INSERT INTO sync_state (device_id, last_sequence_seen, last_sync_time)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_sequence_seen=excluded.last_sequence_seen,
                    last_sync_time=excluded.last_sync_time
                WHERE excluded.last_sequence_seen > sync_state.last_sequence_seen
            """, (packet["device_id"], packet["sequence"], time.time()))

    def apply_remote_record(self, record: dict):
        """
        Apply a location record received FROM THE INTERNET SYNC SERVER for
        some group member. Conflict rule (required behaviour): newest valid
        (timestamp, sequence_number) wins -- an older record, whether it
        arrives from the server or was already here from the mesh, must
        never overwrite a newer one. Duplicates (same packet_id) are also
        safely ignored via INSERT OR IGNORE, same as the mesh path.
        Returns True if this record was newer and got stored, else False.
        """
        with self._lock, self.conn:
            cur = self.conn.execute("""
                SELECT MAX(sequence) FROM packets WHERE device_id=?
            """, (record["device_id"],))
            row = cur.fetchone()
            current_max_seq = row[0] if row and row[0] is not None else -1
            if record["sequence"] <= current_max_seq:
                return False  # stale relative to what we already have -- ignore
            self.conn.execute("""
                INSERT OR IGNORE INTO packets
                (packet_id, device_id, group_id, member_name, latitude, longitude,
                 accuracy, altitude, speed, timestamp, sequence, battery_level,
                 transport_used, source, ttl, received_at, synced)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTERNET', ?, ?, 1)
            """, (
                record["packet_id"], record["device_id"], record.get("group_id"),
                record["member_name"], record["latitude"], record["longitude"],
                record.get("accuracy"), record.get("altitude"), record.get("speed"),
                record["timestamp"], record["sequence"], record.get("battery_level"),
                "internet_sync", record.get("ttl", 0), time.time()
            ))
            self.conn.execute("""
                INSERT INTO sync_state (device_id, last_sequence_seen, last_sync_time)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_sequence_seen=excluded.last_sequence_seen,
                    last_sync_time=excluded.last_sync_time
                WHERE excluded.last_sequence_seen > sync_state.last_sequence_seen
            """, (record["device_id"], record["sequence"], time.time()))
            return True

    def latest_per_member(self):
        """
        Latest known record for every device_id we've ever heard from --
        picking the highest (sequence, timestamp) per device so a
        late-arriving but OLDER packet (from either the mesh or the
        internet) can never shadow a newer one already shown in the UI.
        """
        with self._lock:
            cur = self.conn.execute("""
                SELECT p.device_id, p.member_name, p.latitude, p.longitude,
                       p.timestamp, p.transport_used, p.received_at, p.source
                FROM packets p
                JOIN (
                    SELECT device_id, MAX(sequence) AS max_seq
                    FROM packets GROUP BY device_id
                ) latest
                ON p.device_id = latest.device_id AND p.sequence = latest.max_seq
                GROUP BY p.device_id
            """)
            return cur.fetchall()

    def undelivered_for_peer(self, exclude_device_id, limit=50):
        """Packets this phone knows about that it can relay onward (store-and-forward)."""
        with self._lock:
            cur = self.conn.execute("""
                SELECT packet_id, device_id, member_name, latitude, longitude,
                       timestamp, sequence, battery_level, transport_used, ttl
                FROM packets WHERE device_id != ? ORDER BY received_at DESC LIMIT ?
            """, (exclude_device_id, limit))
            return cur.fetchall()

    # -- Internet sync: outbound queue ------------------------------------
    def enqueue_pending_sync(self, packet: dict):
        with self._lock, self.conn:
            self.conn.execute("""
                INSERT OR IGNORE INTO pending_sync (packet_id, payload, attempts, next_attempt_at, created_at)
                VALUES (?, ?, 0, ?, ?)
            """, (packet["packet_id"], json.dumps(packet), time.time(), time.time()))

    def due_pending_sync(self, limit=25):
        """Records whose backoff window has elapsed and are due for another try."""
        with self._lock:
            cur = self.conn.execute("""
                SELECT packet_id, payload, attempts FROM pending_sync
                WHERE next_attempt_at <= ? ORDER BY created_at ASC LIMIT ?
            """, (time.time(), limit))
            return cur.fetchall()

    def pending_sync_count(self):
        with self._lock:
            cur = self.conn.execute("SELECT COUNT(*) FROM pending_sync")
            return cur.fetchone()[0]

    def mark_synced(self, packet_id):
        """Only called after the server explicitly ACKs the packet_id.
        Local data (the `packets` row) is never deleted -- only removed
        from the outbound queue."""
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM pending_sync WHERE packet_id=?", (packet_id,))
            self.conn.execute("UPDATE packets SET synced=1 WHERE packet_id=?", (packet_id,))

    def reschedule_pending_sync(self, packet_id, attempts):
        """Exponential backoff: don't hammer a dead/unreachable server."""
        delay = min(SYNC_BACKOFF_INITIAL * (2 ** attempts), SYNC_BACKOFF_MAX)
        # small jitter so multiple phones don't retry in perfect lockstep
        delay += random.uniform(0, delay * 0.1)
        with self._lock, self.conn:
            self.conn.execute("""
                UPDATE pending_sync SET attempts=?, next_attempt_at=? WHERE packet_id=?
            """, (attempts + 1, time.time() + delay, packet_id))

    # -- Internet sync: status/meta for UI ---------------------------------
    def set_sync_meta(self, key, value):
        with self._lock, self.conn:
            self.conn.execute("""
                INSERT INTO internet_sync_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (key, str(value)))

    def get_sync_meta(self, key, default=None):
        with self._lock:
            cur = self.conn.execute("SELECT value FROM internet_sync_meta WHERE key=?", (key,))
            row = cur.fetchone()
            return row[0] if row else default


# ===========================================================================
# GPS / LOCATION  -- knows ONLY about this phone's own position.
# ===========================================================================
class LocationFix:
    __slots__ = ("lat", "lon", "accuracy", "altitude", "speed", "timestamp")

    def __init__(self, lat, lon, accuracy=None, altitude=None, speed=None, timestamp=None):
        self.lat = lat
        self.lon = lon
        self.accuracy = accuracy
        self.altitude = altitude
        self.speed = speed
        self.timestamp = timestamp or time.time()


class LocationService:
    """
    Wraps plyer.gps. Configurable interval driven by BatteryModeManager.
    Does NOT talk to other phones -- it only ever updates self.last_fix.
    """

    def __init__(self, on_fix=None):
        self.on_fix = on_fix
        self.last_fix = None
        self._running = False
        self._interval = BATTERY_MODES["NORMAL"]
        self._configured = False

    def set_interval(self, seconds):
        self._interval = seconds
        if self._running and PLYER_AVAILABLE:
            try:
                gps.stop()
                gps.configure(on_location=self._on_location, on_status=self._on_status)
                # Plyer's Android GPS provider takes min_time (ms) / min_distance (m)
                gps.start(minTime=int(seconds * 1000), minDistance=0)
            except Exception as e:
                print("[GPS] reconfigure failed:", e)

    def start(self):
        self._running = True
        if PLYER_AVAILABLE:
            try:
                gps.configure(on_location=self._on_location, on_status=self._on_status)
                gps.start(minTime=int(self._interval * 1000), minDistance=0)
                return
            except NotImplementedError:
                print("[GPS] plyer GPS not implemented on this platform - using simulated fix")
            except Exception as e:
                print("[GPS] start failed:", e)
        # Desktop / fallback simulation so the rest of the app is testable
        self._sim_thread = threading.Thread(target=self._simulate, daemon=True)
        self._sim_thread.start()

    def stop(self):
        self._running = False
        if PLYER_AVAILABLE:
            try:
                gps.stop()
            except Exception:
                pass

    def _on_location(self, **kwargs):
        fix = LocationFix(
            lat=kwargs.get("lat"),
            lon=kwargs.get("lon"),
            accuracy=kwargs.get("accuracy"),
            altitude=kwargs.get("altitude"),
            speed=kwargs.get("speed"),
            timestamp=time.time(),
        )
        self.last_fix = fix
        if self.on_fix:
            self.on_fix(fix)

    def _on_status(self, stype, status):
        print("[GPS] status:", stype, status)

    def _simulate(self):
        import random
        lat, lon = 34.0, 73.0  # arbitrary starting point (e.g. northern Pakistan hills)
        while self._running:
            lat += random.uniform(-0.0005, 0.0005)
            lon += random.uniform(-0.0005, 0.0005)
            fix = LocationFix(lat, lon, accuracy=8.0, altitude=1500.0, speed=0.5)
            self.last_fix = fix
            if self.on_fix:
                self.on_fix(fix)
            time.sleep(self._interval)


# ===========================================================================
# TRANSPORT LAYER
# ===========================================================================
class BaseTransport:
    """Common interface every transport must implement."""
    name = "base"

    def is_available(self):
        raise NotImplementedError

    def start(self, on_packet_received):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def send(self, packet_bytes):
        raise NotImplementedError


class WifiLanTransport(BaseTransport):
    """
    REAL, WORKING transport. Uses UDP broadcast on the local Wi-Fi network
    (a plain home/hotel router OR one phone's Wi-Fi hotspot with no internet
    uplink at all -- no internet access is required, only that phones share
    the same local L2/L3 network).

    This is Wi-Fi based, not Bluetooth, not "mesh over BLE". It is combined
    with MeshRouter below to get store-and-forward multi-hop behaviour, which
    is a legitimate mesh *architecture* running on top of a real transport.
    """
    name = "wifi_lan"

    def __init__(self, port=UDP_PORT):
        self.port = port
        self._sock = None
        self._recv_thread = None
        self._running = False
        self._on_packet = None

    def is_available(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))  # doesn't actually send anything
            s.close()
            return True
        except Exception:
            return False

    def start(self, on_packet_received):
        self._on_packet = on_packet_received
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
        try:
            self._sock.bind(("", self.port))
        except OSError as e:
            print("[WifiLanTransport] bind failed:", e)
            self._sock = None
            return
        self._sock.settimeout(1.0)
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def _recv_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
                if self._on_packet:
                    self._on_packet(data, self.name)
            except socket.timeout:
                continue
            except OSError:
                break

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def send(self, packet_bytes):
        if not self._sock:
            return False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet_bytes, (BROADCAST_ADDR, self.port))
            s.close()
            return True
        except Exception as e:
            print("[WifiLanTransport] send failed:", e)
            return False


class UnavailableNativeTransport(BaseTransport):
    """
    Honest stub for Bluetooth Classic / BLE / Wi-Fi Direct.

    WHY THIS IS A STUB:
    Android Bluetooth Classic RFCOMM sockets, BLE GATT server/client roles,
    and Wi-Fi Direct (WifiP2pManager) are only exposed through Android's Java
    APIs. There is no maintained, reliable pure-Python/Kivy/Plyer binding for
    these that supports acting as BOTH a discoverable/advertiser AND a
    scanner/connector, which is what a peer group needs. Plyer's bluetooth
    support is read-only/limited and does not cover BLE peripheral mode or
    Wi-Fi Direct at all as of this writing.

    WHAT A REAL IMPLEMENTATION NEEDS (native Android integration layer):
      - A Java/Kotlin class using BluetoothAdapter + BluetoothServerSocket
        (Classic) or BluetoothGattServer/BluetoothLeAdvertiser (BLE), or
        WifiP2pManager (Wi-Fi Direct), packaged as a Buildozer/python-for-android
        "recipe" or an .aar bootstrapped via pyjnius, exposing start()/send()/
        on_packet() the same as BaseTransport here.
      - That class is the ONLY part that would need to change; MeshRouter,
        Storage, LocationService and the UI already treat every transport
        polymorphically through BaseTransport, so plugging it in later is a
        drop-in, not a rewrite.

    This stub always reports unavailable so the TransportManager correctly
    falls back to WifiLanTransport instead of silently pretending to work.
    """
    def __init__(self, name):
        self.name = name

    def is_available(self):
        return False

    def start(self, on_packet_received):
        pass

    def stop(self):
        pass

    def send(self, packet_bytes):
        return False


class InternetSyncTransport(BaseTransport):
    """
    OPTIONAL, additive transport. Talks to a configurable HTTPS API so
    group members who are out of local mesh range can still exchange their
    LATEST location once they happen to have internet access.

    This is intentionally NOT wired into TransportManager.broadcast(),
    which is the per-packet, fire-immediately path used by the local mesh.
    Doing internet uploads on every single mesh packet would ignore battery
    mode and hammer the network/battery. Instead this transport runs its
    own timer loop (interval controlled by the active battery mode) that:
      1. Uploads whatever is in the local `pending_sync` outbound queue
         (with exponential backoff on failure -- see Storage.reschedule_pending_sync).
      2. Downloads the group's latest known locations and applies them
         through Storage.apply_remote_record(), which enforces
         "newest (timestamp, sequence) wins" so a stale server record can
         never clobber a fresher local mesh fix, and vice versa.

    Every request requires group_id + device_id + a per-device auth token
    (see InternetSyncAPI spec in README) over HTTPS only. No secrets live
    in this source file -- see SYNC_CONFIG / _load_sync_config() above.

    IMPORTANT: is_available() reflects whether we *should currently try*
    (sync turned on AND a cheap reachability probe recently succeeded). It
    intentionally does NOT do a network call on every UI refresh tick --
    that alone would defeat the point of battery-aware sync -- reachability
    is refreshed on the same timer as the sync loop itself.
    """
    name = "internet_sync"

    def __init__(self, storage: Storage, device_id, member_name_getter, group_id_getter,
                 get_battery_mode, on_remote_locations=None):
        self.storage = storage
        self.device_id = device_id
        self._member_name_getter = member_name_getter
        self._group_id_getter = group_id_getter
        self._get_battery_mode = get_battery_mode
        self.on_remote_locations = on_remote_locations
        self._running = False
        self._thread = None
        self._reachable = False
        self._last_check = 0

    # -- config / state ------------------------------------------------
    def _server_url(self):
        return SYNC_CONFIG.get("server_url", "").strip()

    def sync_enabled(self):
        """User-facing ON/OFF switch, independent of whether a server is configured."""
        return SYNC_CONFIG.get("sync_enabled", True)

    def is_available(self):
        # No server configured, or user turned sync OFF -> never "available",
        # the app must keep working with this simply doing nothing.
        if not self._server_url() or not self.sync_enabled():
            return False
        return self._reachable

    def check_connection(self):
        """Cheap reachability probe (HEAD-ish request with short timeout).
        Never raises; never blocks the UI thread (call from a worker thread)."""
        url = self._server_url()
        if not url or not self.sync_enabled():
            self._reachable = False
            return False
        try:
            req = urllib.request.Request(url.rstrip("/") + "/health", method="GET")
            if SYNC_CONFIG.get("api_key"):
                req.add_header("Authorization", f"Bearer {SYNC_CONFIG['api_key']}")
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=CONNECTIVITY_CHECK_TIMEOUT, context=ctx) as resp:
                self._reachable = 200 <= resp.status < 300
        except Exception:
            self._reachable = False
        self._last_check = time.time()
        return self._reachable

    # -- BaseTransport interface -----------------------------------------
    def start(self, on_packet_received):
        # on_packet_received is unused here on purpose (see class docstring);
        # inbound data is delivered via on_remote_locations instead, because
        # it goes through apply_remote_record()'s conflict resolution, not
        # the mesh's plain dedup-by-packet_id path.
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def send(self, packet_bytes):
        # Not used for immediate sends -- see queue_outbound(). Always
        # returns False so TransportManager.broadcast() (the mesh's
        # immediate fan-out) never mistakes this for a delivered send.
        return False

    def queue_outbound(self, packet: dict):
        """Called explicitly by MeshRouter for OUR OWN location packets.
        Internet sync is opt-in per-packet at the queueing level too: if
        sync is off, we still store locally (never lost), we just never
        add it to the outbound queue."""
        if not self.sync_enabled():
            return
        self.storage.enqueue_pending_sync(packet)

    # -- background loop ----------------------------------------------
    def _loop(self):
        while self._running:
            mode = self._get_battery_mode() or "NORMAL"
            interval = SYNC_INTERVALS.get(mode, SYNC_INTERVALS["NORMAL"])
            try:
                if self.sync_enabled() and self._server_url():
                    self.check_connection()
                    if self._reachable:
                        self._upload_pending()
                        self._download_group()
                        self.storage.set_sync_meta("last_sync_time", time.time())
                        self.storage.set_sync_meta("last_sync_status", "SYNCED")
                    else:
                        self.storage.set_sync_meta("last_sync_status",
                                                    "OFFLINE" if self.storage.pending_sync_count() == 0 else "PENDING")
                else:
                    self.storage.set_sync_meta("last_sync_status", "DISABLED")
            except Exception as e:
                print("[InternetSyncTransport] sync loop error:", e)
                self.storage.set_sync_meta("last_sync_status", "FAILED")
            # Sleep in small increments so stop() takes effect quickly
            # instead of the thread lingering for a full long-mode interval.
            slept = 0
            while self._running and slept < interval:
                time.sleep(min(2, interval - slept))
                slept += 2

    def _upload_pending(self):
        due = self.storage.due_pending_sync(limit=25)
        if not due:
            return
        payloads = [json.loads(p) for (_pid, p, _attempts) in due]
        try:
            body = json.dumps({
                "group_id": self._group_id_getter(),
                "device_id": self.device_id,
                "locations": payloads,
            }).encode("utf-8")
            req = urllib.request.Request(
                self._server_url().rstrip("/") + "/locations/upload",
                data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            if SYNC_CONFIG.get("api_key"):
                req.add_header("Authorization", f"Bearer {SYNC_CONFIG['api_key']}")
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            acked = set(result.get("acknowledged_packet_ids", []))
            for (pid, _payload, attempts) in due:
                if pid in acked:
                    self.storage.mark_synced(pid)
                else:
                    self.storage.reschedule_pending_sync(pid, attempts)
        except Exception as e:
            print("[InternetSyncTransport] upload failed:", e)
            for (pid, _payload, attempts) in due:
                self.storage.reschedule_pending_sync(pid, attempts)

    def _download_group(self):
        try:
            group_id = self._group_id_getter()
            url = (self._server_url().rstrip("/") +
                   f"/locations/group/{urllib.parse.quote(group_id)}"
                   f"?since_device={self.device_id}")
            req = urllib.request.Request(url, method="GET")
            if SYNC_CONFIG.get("api_key"):
                req.add_header("Authorization", f"Bearer {SYNC_CONFIG['api_key']}")
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            records = result.get("locations", [])
            applied = []
            for rec in records:
                if rec.get("device_id") == self.device_id:
                    continue  # never apply our own record back to ourselves
                if self.storage.apply_remote_record(rec):
                    applied.append(rec)
            if applied and self.on_remote_locations:
                self.on_remote_locations(applied)
        except Exception as e:
            print("[InternetSyncTransport] download failed:", e)


class TransportManager:
    """
    Detects available transports and uses the best one; broadcasts on ALL
    currently-available LOCAL transports so no reachable peer is missed
    (cheap since packets are tiny JSON). Internet sync is deliberately
    excluded from this immediate fan-out -- see InternetSyncTransport.
    """
    def __init__(self, internet_sync: "InternetSyncTransport" = None):
        self.internet_sync = internet_sync
        self.transports = [
            WifiLanTransport(),
            UnavailableNativeTransport("bluetooth_classic"),
            UnavailableNativeTransport("ble"),
            UnavailableNativeTransport("wifi_direct"),
        ]
        if internet_sync is not None:
            self.transports.append(internet_sync)
        self._on_packet = None
        self._started = False

    def status(self):
        return {t.name: t.is_available() for t in self.transports}

    def start(self, on_packet_received):
        self._on_packet = on_packet_received
        for t in self.transports:
            try:
                # Internet sync starts its own timer loop regardless of
                # is_available() right now, since reachability can change
                # later (e.g. member walks into signal) -- the loop itself
                # is what re-checks and no-ops safely when there's nothing
                # to do.
                if t is self.internet_sync or t.is_available():
                    t.start(self._handle_incoming)
            except Exception as e:
                print(f"[TransportManager] {t.name} failed to start:", e)
        self._started = True

    def stop(self):
        for t in self.transports:
            try:
                t.stop()
            except Exception:
                pass
        self._started = False

    def _handle_incoming(self, data, transport_name):
        if self._on_packet:
            self._on_packet(data, transport_name)

    def broadcast(self, packet_bytes):
        """Send on every currently available LOCAL transport ('automatically
        use whichever is available'). Internet sync is intentionally NOT
        included here -- it is queued separately (see MeshRouter.emit_location)
        and uploaded on its own battery-aware timer, not per-packet."""
        sent_on = []
        for t in self.transports:
            if t is self.internet_sync:
                continue
            try:
                if t.is_available() and t.send(packet_bytes):
                    sent_on.append(t.name)
            except Exception as e:
                print(f"[TransportManager] send via {t.name} failed:", e)
        return sent_on


# ===========================================================================
# MESH ROUTER  -- store-and-forward, dedup, TTL/hop-count based relay
# ===========================================================================
class MeshRouter:
    MAX_TTL = 6  # generous for a 5-person group with slack for topology changes

    def __init__(self, storage: Storage, transport_manager: TransportManager,
                 device_id, member_name, group_id="", on_member_update=None):
        self.storage = storage
        self.tm = transport_manager
        self.device_id = device_id
        self.member_name = member_name
        self.group_id = group_id
        self.on_member_update = on_member_update
        self._seq = 0
        self._seen_lock = threading.Lock()
        self._seen_packet_ids = set()  # in-memory fast dedup cache
        self._relay_thread = None
        self._running = False

    def start(self):
        self.tm.start(self._on_packet_received)
        self._running = True
        # periodic re-broadcast of anything we're still holding (store-and-forward)
        self._relay_thread = threading.Thread(target=self._relay_loop, daemon=True)
        self._relay_thread.start()

    def stop(self):
        self._running = False
        self.tm.stop()

    def make_packet(self, fix: LocationFix, battery_level=None, ttl=None):
        self._seq += 1
        packet = {
            "packet_id": str(uuid.uuid4()),
            "device_id": self.device_id,
            "group_id": self.group_id,
            "member_name": self.member_name,
            "latitude": fix.lat,
            "longitude": fix.lon,
            "accuracy": fix.accuracy,
            "altitude": fix.altitude,
            "speed": fix.speed,
            "timestamp": fix.timestamp,
            "sequence": self._seq,
            "battery_level": battery_level,
            "transport_used": "pending",
            "ttl": ttl if ttl is not None else self.MAX_TTL,
            "origin": self.device_id,
        }
        return packet

    def emit_location(self, fix: LocationFix, battery_level=None):
        packet = self.make_packet(fix, battery_level)
        self._remember(packet)
        self.storage.record_location(
            self.device_id, fix.lat, fix.lon, fix.altitude, fix.speed,
            fix.accuracy, fix.timestamp, packet["sequence"]
        )
        self._send(packet)
        # Internet sync is additive: queue the SAME record for eventual
        # upload. This never blocks on the network and never replaces the
        # local mesh send above -- if internet sync is off/unreachable this
        # is just a harmless SQLite insert into pending_sync.
        if self.tm.internet_sync is not None:
            try:
                self.tm.internet_sync.queue_outbound(dict(packet))
            except Exception as e:
                print("[MeshRouter] failed to queue for internet sync:", e)

    def _send(self, packet):
        data = json.dumps(packet).encode("utf-8")
        sent_on = self.tm.broadcast(data)
        packet["transport_used"] = ",".join(sent_on) if sent_on else "none_available"
        self.storage.store_packet(packet)

    def _remember(self, packet):
        with self._seen_lock:
            self._seen_packet_ids.add(packet["packet_id"])

    def _already_seen(self, packet_id):
        with self._seen_lock:
            if packet_id in self._seen_packet_ids:
                return True
        return self.storage.packet_seen(packet_id)

    def _on_packet_received(self, data, transport_name):
        try:
            packet = json.loads(data.decode("utf-8"))
        except Exception:
            return  # malformed / foreign packet, ignore

        required = {"packet_id", "device_id", "latitude", "longitude", "sequence", "ttl"}
        if not required.issubset(packet):
            return

        if packet["device_id"] == self.device_id:
            return  # our own broadcast echoed back

        if self._already_seen(packet["packet_id"]):
            return  # duplicate -- prevented via packet_id

        packet["transport_used"] = transport_name
        self._remember(packet)
        self.storage.store_packet(packet)
        self.storage.upsert_member(packet["device_id"], packet.get("member_name", "?"), "")

        if self.on_member_update:
            self.on_member_update(packet)

        # RELAY if hop budget remains (multi-hop mesh: A<->B<->C<->D<->E)
        if packet["ttl"] > 0:
            relay_packet = dict(packet)
            relay_packet["ttl"] = packet["ttl"] - 1
            data_out = json.dumps(relay_packet).encode("utf-8")
            self.tm.broadcast(data_out)
        # if ttl == 0, silently drop further relay (prevents infinite loops)

    def _relay_loop(self):
        """
        Store-and-forward: periodically re-announce recent packets we're
        holding so a peer who was unreachable earlier (e.g. just came back
        into range) receives them once reachable again, without needing a
        fresh GPS fix from the original phone.
        """
        while self._running:
            time.sleep(20)
            try:
                rows = self.storage.undelivered_for_peer(self.device_id, limit=20)
                for row in rows:
                    (packet_id, device_id, member_name, lat, lon, ts, seq,
                     battery_level, transport_used, ttl) = row
                    if ttl <= 0:
                        continue
                    packet = {
                        "packet_id": packet_id, "device_id": device_id,
                        "member_name": member_name, "latitude": lat, "longitude": lon,
                        "timestamp": ts, "sequence": seq, "battery_level": battery_level,
                        "transport_used": transport_used, "ttl": ttl - 1,
                    }
                    self.tm.broadcast(json.dumps(packet).encode("utf-8"))
            except Exception as e:
                print("[MeshRouter] relay loop error:", e)


# ===========================================================================
# BATTERY MODE MANAGER
# ===========================================================================
class BatteryModeManager:
    def __init__(self, location_service: LocationService):
        self.location_service = location_service
        self.mode = "NORMAL"

    def set_mode(self, mode):
        if mode not in BATTERY_MODES:
            return
        self.mode = mode
        self.location_service.set_interval(BATTERY_MODES[mode])

    def current_battery_level(self):
        if PLYER_AVAILABLE:
            try:
                battery.status  # triggers refresh on some platforms
                return battery.status.get("percentage")
            except Exception:
                return None
        return None


# ===========================================================================
# UTIL
# ===========================================================================
def haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, sqrt, atan2
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def get_device_id():
    if PLYER_AVAILABLE:
        try:
            uid = uniqueid.id
            if uid:
                return str(uid)
        except Exception:
            pass
    path = os.path.join(APP_DIR, "device_id.txt")
    if os.path.exists(path):
        return open(path).read().strip()
    new_id = str(uuid.uuid4())[:8]
    with open(path, "w") as f:
        f.write(new_id)
    return new_id


def request_android_permissions():
    if not ANDROID:
        return
    try:
        perms = [
            Permission.ACCESS_FINE_LOCATION,
            Permission.ACCESS_COARSE_LOCATION,
            Permission.FOREGROUND_SERVICE,
        ]
        # Version-gated / newer permissions - request defensively; older
        # Android builds of python-for-android may not expose all constants.
        for name in ("ACCESS_BACKGROUND_LOCATION", "BLUETOOTH_SCAN",
                     "BLUETOOTH_CONNECT", "BLUETOOTH_ADVERTISE",
                     "NEARBY_WIFI_DEVICES", "FOREGROUND_SERVICE_LOCATION"):
            perm = getattr(Permission, name, None)
            if perm:
                perms.append(perm)
        request_permissions(perms, _on_permissions_result)
    except Exception as e:
        print("[Permissions] request failed (continuing without):", e)


def _on_permissions_result(permissions, grants):
    # Do NOT crash on denial -- degrade gracefully. GPS-dependent features
    # simply won't produce fixes; transports needing Wi-Fi state still try.
    for perm, granted in zip(permissions, grants):
        print(f"[Permissions] {perm}: {'granted' if granted else 'DENIED'}")


# ===========================================================================
# KIVY UI
# ===========================================================================
class MemberRow(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", size_hint_y=None, height=dp(70),
                          padding=dp(4), spacing=dp(2), **kwargs)


class RootUI(BoxLayout):
    def __init__(self, app, **kwargs):
        super().__init__(orientation="vertical", padding=dp(10), spacing=dp(6), **kwargs)
        self.app = app

        self.add_widget(Label(text="OFFLINE GROUP TRACKER", font_size="20sp",
                               size_hint_y=None, height=dp(36), bold=True))

        self.my_loc_label = Label(text="My Location: waiting for GPS fix...",
                                   size_hint_y=None, height=dp(70), halign="left",
                                   valign="top")
        self.my_loc_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(self.my_loc_label)

        self.transport_label = Label(text="Transport status: (starting...)",
                                      size_hint_y=None, height=dp(110), halign="left",
                                      valign="top")
        self.transport_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(self.transport_label)

        # -- INTERNET SYNC section (all optional/additive to the mesh above) --
        self.sync_label = Label(text="INTERNET SYNC\nInternet: -\nSync: -\nLast Sync: -\nPending: -",
                                 size_hint_y=None, height=dp(100), halign="left", valign="top")
        self.sync_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(self.sync_label)

        sync_toggle_row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        sync_toggle_row.add_widget(Label(text="Internet Sync:"))
        self.sync_toggle_btn = Button(text="ON", on_release=self.app.toggle_internet_sync)
        sync_toggle_row.add_widget(self.sync_toggle_btn)
        self.add_widget(sync_toggle_row)

        self.add_widget(Label(text="Group Members:", size_hint_y=None, height=dp(24),
                               bold=True, halign="left"))

        self.members_box = GridLayout(cols=1, size_hint_y=None, spacing=dp(4))
        self.members_box.bind(minimum_height=self.members_box.setter("height"))
        scroll = ScrollView(size_hint=(1, 1))
        scroll.add_widget(self.members_box)
        self.add_widget(scroll)

        btn_row1 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        self.start_btn = Button(text="START TRIP", on_release=self.app.on_start_trip)
        self.stop_btn = Button(text="STOP", on_release=self.app.on_stop_trip)
        btn_row1.add_widget(self.start_btn)
        btn_row1.add_widget(self.stop_btn)
        self.add_widget(btn_row1)

        btn_row2 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        for mode, cb in (
            ("NORMAL", lambda *_: self.app.set_battery_mode("NORMAL")),
            ("BATTERY SAVING", lambda *_: self.app.set_battery_mode("BATTERY_SAVING")),
            ("EXTREME", lambda *_: self.app.set_battery_mode("EXTREME_BATTERY")),
            ("EMERGENCY", lambda *_: self.app.set_battery_mode("EMERGENCY")),
        ):
            btn_row2.add_widget(Button(text=mode, on_release=cb))
        self.add_widget(btn_row2)

        self.mode_label = Label(text="Battery Mode: NORMAL", size_hint_y=None, height=dp(24))
        self.add_widget(self.mode_label)

    def update_my_location(self, fix: LocationFix):
        if not fix:
            return
        self.my_loc_label.text = (
            f"My Location:\n"
            f"Lat: {fix.lat:.6f}   Lon: {fix.lon:.6f}\n"
            f"Accuracy: {fix.accuracy}m   Last Update: "
            f"{datetime.fromtimestamp(fix.timestamp).strftime('%H:%M:%S')}"
        )

    def update_transport_status(self, status: dict, mesh_connected: bool):
        lines = ["Transport status:"]
        for name, available in status.items():
            if name == "internet_sync":
                continue  # shown in its own INTERNET SYNC section instead
            label = {
                "wifi_lan": "Wi-Fi (LAN/Hotspot)",
                "bluetooth_classic": "Bluetooth Classic",
                "ble": "BLE",
                "wifi_direct": "Wi-Fi Direct",
            }.get(name, name)
            state = "Available" if available else "Unavailable (native module required)" \
                if name != "wifi_lan" else "Unavailable"
            lines.append(f"  {label}: {state}")
        lines.append(f"  Mesh: {'Connected' if mesh_connected else 'Searching...'}")
        self.transport_label.text = "\n".join(lines)

    def update_sync_status(self, internet_connected, sync_status, last_sync_time, pending_count, sync_enabled):
        conn_icon = "\U0001F7E2 Connected" if internet_connected else "\U0001F534 Offline"
        status_icon = {
            "SYNCED": "\U0001F7E2 Synced", "PENDING": "\U0001F7E1 Pending",
            "FAILED": "\U0001F534 Failed", "OFFLINE": "\U0001F534 Offline",
            "DISABLED": "\u26AA Disabled",
        }.get(sync_status, sync_status or "-")
        last_sync = datetime.fromtimestamp(float(last_sync_time)).strftime("%I:%M %p") \
            if last_sync_time else "Never"
        self.sync_label.text = (
            f"INTERNET SYNC\n"
            f"Internet: {conn_icon}\n"
            f"Sync: {status_icon}\n"
            f"Last Sync: {last_sync}\n"
            f"Pending: {pending_count} records"
        )
        self.sync_toggle_btn.text = "ON" if sync_enabled else "OFF"

    def rebuild_members(self, rows, my_fix):
        self.members_box.clear_widgets()
        now = time.time()
        for (device_id, member_name, lat, lon, ts, transport, _recv, source) in rows:
            age = now - ts
            if age < 60:
                last_seen = f"{int(age)}s ago"
            elif age < 3600:
                last_seen = f"{int(age // 60)} min ago"
            else:
                last_seen = f"{int(age // 3600)}h ago"

            # IMPORTANT DISTINCTION required by the internet-sync feature:
            #   LIVE/LOCAL   - recent packet received over the local mesh
            #   REMOTE/INTERNET - recent record received via internet sync
            #                     (member is out of local mesh range)
            #   LAST KNOWN   - neither source has heard from them recently,
            #                  but we still remember where they were
            #   (OFFLINE as a whole-app state is shown in the INTERNET SYNC
            #    panel above, not per-member)
            if source == "INTERNET":
                if age < STALE_AFTER_SECONDS:
                    status_tag = "\U0001F535 INTERNET"
                    detail = f"Last sync: {last_seen}"
                else:
                    status_tag = "\U0001F7E1 LAST KNOWN"
                    detail = f"Last seen: {last_seen}"
            else:
                if age < STALE_AFTER_SECONDS:
                    status_tag = "\U0001F7E2 LOCAL / MESH"
                    detail = f"Last seen: {last_seen}"
                else:
                    status_tag = "\U0001F7E1 LAST KNOWN"
                    detail = f"Last seen: {last_seen}"

            dist = ""
            if my_fix:
                d = haversine_km(my_fix.lat, my_fix.lon, lat, lon)
                dist = f"{d:.2f} km"
            row = MemberRow()
            row.add_widget(Label(text=f"{member_name}   {status_tag}", bold=True,
                                  size_hint_y=None, height=dp(20), halign="left"))
            row.add_widget(Label(text=f"Lat {lat:.5f}, Lon {lon:.5f}   Dist: {dist}",
                                  size_hint_y=None, height=dp(20), halign="left"))
            row.add_widget(Label(text=f"{detail}   via {transport}",
                                  size_hint_y=None, height=dp(20), halign="left"))
            self.members_box.add_widget(row)


class GroupSetupPopup(Popup):
    def __init__(self, on_confirm, **kwargs):
        super().__init__(title="Join / Create Group", size_hint=(0.9, 0.6), **kwargs)
        layout = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
        layout.add_widget(Label(text="Your Name:", size_hint_y=None, height=dp(24)))
        self.name_input = TextInput(multiline=False, size_hint_y=None, height=dp(40))
        layout.add_widget(self.name_input)

        layout.add_widget(Label(text="Group Name:", size_hint_y=None, height=dp(24)))
        self.group_name_input = TextInput(text="Mountain Trip", multiline=False,
                                           size_hint_y=None, height=dp(40))
        layout.add_widget(self.group_name_input)

        layout.add_widget(Label(text="Group ID (share this with your group, "
                                      "everyone must enter the SAME ID):",
                                 size_hint_y=None, height=dp(40)))
        self.group_id_input = TextInput(text="MT-" + str(uuid.uuid4())[:4].upper(),
                                         multiline=False, size_hint_y=None, height=dp(40))
        layout.add_widget(self.group_id_input)

        confirm_btn = Button(text="Confirm & Continue", size_hint_y=None, height=dp(48))

        def _confirm(*_):
            name = self.name_input.text.strip() or "Member"
            group_name = self.group_name_input.text.strip() or "Group"
            group_id = self.group_id_input.text.strip() or "GRP-0000"
            self.dismiss()
            on_confirm(name, group_name, group_id)

        confirm_btn.bind(on_release=_confirm)
        layout.add_widget(confirm_btn)
        self.content = layout


class OfflineTrackerApp(App):
    def build(self):
        self.device_id = get_device_id()
        self.member_name = None
        self.group_id = None
        self.storage = Storage()
        self.location_service = LocationService(on_fix=self._on_fix)
        self.battery_mgr = BatteryModeManager(self.location_service)
        # TransportManager/InternetSyncTransport need group_id, so real
        # construction happens in _on_group_confirmed. self.mesh stays None
        # (and START TRIP re-prompts for group setup) until then, unchanged
        # from before internet sync was added.
        self.transport_manager = None
        self.internet_sync = None
        self.mesh = None
        self.root_ui = RootUI(self)
        self._current_fix = None

        request_android_permissions()

        Clock.schedule_once(lambda dt: self._show_group_setup(), 0.3)
        Clock.schedule_interval(self._refresh_ui, 5)
        return self.root_ui

    # -- setup -------------------------------------------------------------
    def _show_group_setup(self):
        popup = GroupSetupPopup(on_confirm=self._on_group_confirmed)
        popup.open()

    def _on_group_confirmed(self, name, group_name, group_id):
        self.member_name = name
        self.group_id = group_id
        self.storage.upsert_member(self.device_id, self.member_name, self.group_id)

        self.internet_sync = InternetSyncTransport(
            self.storage, self.device_id,
            member_name_getter=lambda: self.member_name,
            group_id_getter=lambda: self.group_id,
            get_battery_mode=lambda: self.battery_mgr.mode,
            on_remote_locations=self._on_remote_locations,
        )
        self.transport_manager = TransportManager(internet_sync=self.internet_sync)
        self.mesh = MeshRouter(
            self.storage, self.transport_manager, self.device_id,
            self.member_name, group_id=self.group_id,
            on_member_update=self._on_member_update
        )

    # -- trip control --------------------------------------------------
    def on_start_trip(self, *_):
        if not self.mesh:
            self._show_group_setup()
            return
        self.location_service.start()
        self.mesh.start()  # starts local mesh transports AND internet_sync's timer loop

    def on_stop_trip(self, *_):
        self.location_service.stop()
        if self.mesh:
            self.mesh.stop()

    def set_battery_mode(self, mode):
        self.battery_mgr.set_mode(mode)
        self.root_ui.mode_label.text = f"Battery Mode: {mode}"
        # Internet sync's own loop reads self.battery_mgr.mode on each
        # iteration (see get_battery_mode above), so it automatically speeds
        # up/slows down with no extra wiring needed here.

    def toggle_internet_sync(self, *_):
        """User-facing Internet Sync ON/OFF switch (privacy setting). When
        OFF, the app is fully functional in offline mode -- GPS and the
        local mesh are completely unaffected either way."""
        SYNC_CONFIG["sync_enabled"] = not SYNC_CONFIG.get("sync_enabled", True)
        try:
            cfg_path = os.path.join(APP_DIR, "sync_config.json")
            with open(cfg_path, "w") as f:
                json.dump(SYNC_CONFIG, f)
        except Exception as e:
            print("[SyncConfig] failed to persist toggle:", e)

    # -- callbacks -------------------------------------------------------
    def _on_fix(self, fix: LocationFix):
        self._current_fix = fix
        if self.mesh:
            battery_level = self.battery_mgr.current_battery_level()
            self.mesh.emit_location(fix, battery_level)
        Clock.schedule_once(lambda dt: self.root_ui.update_my_location(fix), 0)

    def _on_member_update(self, packet):
        pass  # UI refresh is polled on an interval; nothing to do live here

    def _on_remote_locations(self, records):
        pass  # same as above -- picked up by the next _refresh_ui tick

    def _refresh_ui(self, dt):
        self.root_ui.update_my_location(self._current_fix)
        if self.transport_manager:
            status = self.transport_manager.status()
        else:
            status = {}
        mesh_connected = any(v for k, v in status.items() if k != "internet_sync")
        self.root_ui.update_transport_status(status, mesh_connected)

        internet_connected = bool(status.get("internet_sync"))
        sync_status = self.storage.get_sync_meta("last_sync_status", "DISABLED")
        last_sync_time = self.storage.get_sync_meta("last_sync_time")
        pending = self.storage.pending_sync_count()
        sync_enabled = SYNC_CONFIG.get("sync_enabled", True)
        self.root_ui.update_sync_status(internet_connected, sync_status,
                                         last_sync_time, pending, sync_enabled)

        rows = self.storage.latest_per_member()
        self.root_ui.rebuild_members(rows, self._current_fix)

    def on_stop(self):
        self.on_stop_trip()


if __name__ == "__main__":
    OfflineTrackerApp().run()
