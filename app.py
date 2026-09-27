import os
import sys
import time
import math
import json
import socket
import csv
import threading
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
import uvicorn

sys.stdout.reconfigure(line_buffering=True)  # show thread messages in journalctl as they happen

app = FastAPI(title="RPi 3 4WD GPS & IMU Tracker")

# Global Telemetry & GPS State
telemetry_state = {
    # GPS Telemetry
    "lat": 0.0,
    "lon": 0.0,
    "alt": 0.0,           # meters
    "speed": 0.0,         # km/h
    "speed_mph": 0.0,     # mph
    "heading": 0.0,       # degrees
    "satellites": 0,      # satellites used in the fix
    "sats_visible": 0,    # satellites in view
    "fix_quality": 0,     # 0 = No fix, 1 = 2D/3D fix, 2 = DGPS
    "hdop": 99.9,         # Horizontal dilution of precision
    "vdop": 99.9,         # Vertical dilution of precision
    "status": "Initializing",
    "mode": "Hardware",   # "Hardware" or "Simulation"
    "timestamp": "",

    # IMU Data (from M5StickC Plus2 MPU6886)
    "acc_x": 0.0,         # g-force
    "acc_y": 0.0,
    "acc_z": 1.0,         # 1.0g gravity
    "gyro_x": 0.0,        # deg/s
    "gyro_y": 0.0,
    "gyro_z": 0.0,
    "pitch": 0.0,         # Vehicle incline (deg)
    "roll": 0.0,          # Vehicle tilt/side roll (deg)
    "g_total": 1.0,       # Total acceleration vector (g)
    "roughness": 0.0,     # Road corrugation index (vibration RMS)
    "imu_connected": False
}

# Per-satellite sky view (from gpsd SKY messages) for the Satellites page
sky_state = {
    "satellites": []      # [{prn, gnss, el, az, snr, used}]
}

GNSS_NAMES = {0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 4: "IMES", 5: "QZSS", 6: "GLONASS", 7: "NavIC"}

def gnss_name(sat: dict) -> str:
    """Constellation name from gpsd's gnssid, falling back to gpsd's PRN numbering ranges."""
    if "gnssid" in sat:
        return GNSS_NAMES.get(sat["gnssid"], "Other")
    prn = sat.get("PRN", 0)
    if 1 <= prn <= 32: return "GPS"
    if 33 <= prn <= 64 or 120 <= prn <= 158: return "SBAS"
    if 65 <= prn <= 96: return "GLONASS"
    if 193 <= prn <= 202: return "QZSS"
    if 201 <= prn <= 263 or 401 <= prn <= 437: return "BeiDou"
    if 301 <= prn <= 336: return "Galileo"
    return "Other"

# Daily Logger Configuration
LOG_DIR = os.environ.get("GPS_LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))
os.makedirs(LOG_DIR, exist_ok=True)

# Recording State
recording_state = {
    "is_recording": True,  # Auto-record daily logs by default
    "points_logged_today": 0,
    "current_log_file": None
}

DATA_LOCK = threading.Lock()

SERIAL_BY_ID = "/dev/serial/by-id"

def find_serial_port(want_gps: bool, fallbacks: List[str]) -> Optional[str]:
    """Pick the GPS or the M5Stick by USB identity. ttyACM0/ttyACM1 numbering swaps between boots,
    so fixed port names end up with gpsd and the IMU reader on each other's device."""
    if os.path.isdir(SERIAL_BY_ID):
        for name in sorted(os.listdir(SERIAL_BY_ID)):
            is_gps = "u-blox" in name.lower() or "gps" in name.lower() or "gnss" in name.lower()
            if is_gps == want_gps:
                return os.path.realpath(os.path.join(SERIAL_BY_ID, name))
        return None
    return next((p for p in fallbacks if os.path.exists(p)), None)


class M5StickIMUThread(threading.Thread):
    """Background listener for M5StickC Plus2 IMU data via USB Serial or UDP socket."""
    def __init__(self, serial_port='/dev/ttyACM1', baud=115200, udp_port=5005):
        super().__init__(daemon=True)
        self.serial_port = serial_port
        self.baud = baud
        self.udp_port = udp_port
        self.running = True

    def run(self):
        print("[IMU] Starting M5StickC Plus2 IMU Thread...")
        while self.running:
            connected = self.try_serial()
            if not connected:
                connected = self.try_udp()

            if not connected:
                # Simulate IMU movement if not connected to hardware
                self.simulate_imu()

            time.sleep(2)

    def try_serial(self) -> bool:
        try:
            import serial
            target = find_serial_port(want_gps=False, fallbacks=['/dev/ttyACM1', '/dev/ttyUSB1', 'COM4'])
            if not target or target == getattr(gps_thread, 'active_serial_port', None): return False

            # Hold DTR/RTS low: toggling them on open resets the M5Stick's ESP32 (auto-reset circuit).
            ser = serial.Serial()
            ser.port, ser.baudrate, ser.timeout = target, self.baud, 2
            ser.dtr = False
            ser.rts = False
            ser.open()
            print(f"[IMU] Connected to M5StickC Plus2 on {target}")
            with DATA_LOCK:
                telemetry_state["imu_connected"] = True

            empty_reads = 0
            while self.running:
                try:
                    raw = ser.readline()
                    empty_reads = 0
                except serial.SerialException:
                    # "readiness to read but returned no data": transient while the stick resets/reconnects
                    empty_reads += 1
                    if empty_reads > 20 or not os.path.exists(target):
                        raise
                    time.sleep(0.25)
                    continue
                line = raw.decode('utf-8', errors='ignore').strip()
                if line.startswith('{') and line.endswith('}'):
                    self.parse_imu_json(line)
            ser.close()
            return True
        except Exception as e:
            print(f"[IMU] Serial error: {type(e).__name__}: {e}")
            return False

    def try_udp(self) -> bool:
        """Receive UDP packets from M5StickC over Wi-Fi Hotspot."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(('0.0.0.0', self.udp_port))
            sock.settimeout(3.0)
            print(f"[IMU] Listening for M5StickC UDP packets on port {self.udp_port}...")
            
            while self.running:
                data, _ = sock.recvfrom(1024)
                line = data.decode('utf-8', errors='ignore').strip()
                if line.startswith('{'):
                    self.parse_imu_json(line)
            sock.close()
            return True
        except Exception:
            return False

    def parse_imu_json(self, line: str):
        try:
            data = json.loads(line)
            ax = data.get("ax", 0.0)
            ay = data.get("ay", 0.0)
            az = data.get("az", 1.0)
            gx = data.get("gx", 0.0)
            gy = data.get("gy", 0.0)
            gz = data.get("gz", 0.0)

            # Calculate Pitch & Roll from accelerometer
            pitch = math.degrees(math.atan2(ax, math.sqrt(ay**2 + az**2)))
            roll = math.degrees(math.atan2(ay, math.sqrt(ax**2 + az**2)))
            g_total = math.sqrt(ax**2 + ay**2 + az**2)
            
            # Vibration / Corrugation roughness score (deviation from 1g)
            roughness = abs(g_total - 1.0)

            with DATA_LOCK:
                telemetry_state["acc_x"] = round(ax, 3)
                telemetry_state["acc_y"] = round(ay, 3)
                telemetry_state["acc_z"] = round(az, 3)
                telemetry_state["gyro_x"] = round(gx, 2)
                telemetry_state["gyro_y"] = round(gy, 2)
                telemetry_state["gyro_z"] = round(gz, 2)
                telemetry_state["pitch"] = round(pitch, 1)
                telemetry_state["roll"] = round(roll, 1)
                telemetry_state["g_total"] = round(g_total, 2)
                telemetry_state["roughness"] = round(roughness, 3)
                telemetry_state["imu_connected"] = True
        except Exception:
            pass

    def simulate_imu(self):
        """Simulate realistic pitch/roll and corrugation g-forces for 4WD driving demo."""
        t = 0
        while self.running and not telemetry_state["imu_connected"]:
            t += 0.1
            ax = math.sin(t * 0.5) * 0.1
            ay = math.cos(t * 0.3) * 0.1
            az = 1.0 + (math.sin(t * 5.0) * 0.15) # Corrugation vibration

            pitch = math.degrees(math.atan2(ax, 1.0))
            roll = math.degrees(math.atan2(ay, 1.0))
            g_total = math.sqrt(ax**2 + ay**2 + az**2)

            with DATA_LOCK:
                telemetry_state["acc_x"] = round(ax, 3)
                telemetry_state["acc_y"] = round(ay, 3)
                telemetry_state["acc_z"] = round(az, 3)
                telemetry_state["pitch"] = round(pitch, 1)
                telemetry_state["roll"] = round(roll, 1)
                telemetry_state["g_total"] = round(g_total, 2)
                telemetry_state["roughness"] = round(abs(g_total - 1.0), 3)

            time.sleep(0.1)


class GPSReaderThread(threading.Thread):
    def __init__(self, gpsd_host="127.0.0.1", gpsd_port=2947):
        super().__init__(daemon=True)
        self.gpsd_host = gpsd_host
        self.gpsd_port = gpsd_port
        self.running = True
        self.active_serial_port = None

    def run(self):
        print("[GPS] Starting GPS Background Thread...")
        while self.running:
            connected = self.try_connect_gpsd()
            if not connected:
                connected = self.try_connect_serial()

            if not connected:
                with DATA_LOCK:
                    telemetry_state["mode"] = "Simulation"
                    telemetry_state["status"] = "Simulation Active"
                self.run_simulation()
            
            time.sleep(2)

    def try_connect_gpsd(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect((self.gpsd_host, self.gpsd_port))
            sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
            
            with DATA_LOCK:
                telemetry_state["mode"] = "Hardware (gpsd)"
                telemetry_state["status"] = "Connected"

            buffer = ""
            sock.settimeout(5.0)
            while self.running:
                data = sock.recv(4096).decode('utf-8', errors='ignore')
                if not data: break
                buffer += data
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if line.strip():
                        self.parse_gpsd_json(line.strip())
            sock.close()
            return True
        except Exception:
            return False

    def parse_gpsd_json(self, line: str):
        try:
            msg = json.loads(line)
            msg_type = msg.get("class")
            
            with DATA_LOCK:
                if msg_type == "TPV":
                    mode = msg.get("mode", 0)
                    telemetry_state["fix_quality"] = mode
                    if mode >= 2:
                        telemetry_state["lat"] = round(msg.get("lat", telemetry_state["lat"]), 6)
                        telemetry_state["lon"] = round(msg.get("lon", telemetry_state["lon"]), 6)
                        telemetry_state["alt"] = round(msg.get("alt", telemetry_state["alt"]), 1)
                        speed_m_s = msg.get("speed", 0.0)
                        telemetry_state["speed"] = round(speed_m_s * 3.6, 1)
                        telemetry_state["speed_mph"] = round(speed_m_s * 2.23694, 1)
                        telemetry_state["heading"] = round(msg.get("track", 0.0), 1)
                        telemetry_state["status"] = "3D Fix" if mode == 3 else "2D Fix"
                        telemetry_state["timestamp"] = msg.get("time", datetime.utcnow().isoformat())
                        
                        log_to_daily_file()
                    else:
                        telemetry_state["status"] = "Searching Satellites..."

                elif msg_type == "SKY":
                    # gpsd 3.2x sends some SKY messages with only DOPs/counts and no satellite list,
                    # so only update fields that are actually present.
                    if "satellites" in msg:
                        sats = msg["satellites"]
                        sky_state["satellites"] = [{
                            "prn": s.get("PRN"),
                            "gnss": gnss_name(s),
                            "el": s.get("el"),
                            "az": s.get("az"),
                            "snr": s.get("ss"),
                            "used": bool(s.get("used", False)),
                        } for s in sats]
                        telemetry_state["satellites"] = sum(1 for s in sats if s.get("used", False))
                        telemetry_state["sats_visible"] = len(sats)
                    elif "uSat" in msg:
                        telemetry_state["satellites"] = msg["uSat"]
                        telemetry_state["sats_visible"] = msg.get("nSat", telemetry_state["sats_visible"])
                    if "hdop" in msg:
                        telemetry_state["hdop"] = round(msg["hdop"], 2)
                    if "vdop" in msg:
                        telemetry_state["vdop"] = round(msg["vdop"], 2)
        except Exception:
            pass

    def try_connect_serial(self) -> bool:
        try:
            import serial
            self.active_serial_port = find_serial_port(want_gps=True, fallbacks=['/dev/ttyUSB0', '/dev/ttyACM0', 'COM3'])
            if not self.active_serial_port: return False

            ser = serial.Serial(self.active_serial_port, 9600, timeout=2)
            with DATA_LOCK:
                telemetry_state["mode"] = f"Serial ({self.active_serial_port})"
                telemetry_state["status"] = "Connected"

            while self.running:
                line = ser.readline().decode('ascii', errors='ignore')
                if line.startswith('$GPRMC') or line.startswith('$GNRMC'):
                    self.parse_nmea_rmc(line)
            ser.close()
            return True
        except Exception:
            return False

    def parse_nmea_rmc(self, line: str):
        try:
            parts = line.split(',')
            if len(parts) > 8 and parts[2] == 'A':
                lat = self.nmea_to_deg(parts[3], parts[4])
                lon = self.nmea_to_deg(parts[5], parts[6])
                speed_knots = float(parts[7]) if parts[7] else 0.0
                heading_deg = float(parts[8]) if parts[8] else 0.0

                with DATA_LOCK:
                    telemetry_state["lat"] = round(lat, 6)
                    telemetry_state["lon"] = round(lon, 6)
                    telemetry_state["speed"] = round(speed_knots * 1.852, 1)
                    telemetry_state["speed_mph"] = round(speed_knots * 1.15078, 1)
                    telemetry_state["heading"] = round(heading_deg, 1)
                    telemetry_state["fix_quality"] = 1
                    telemetry_state["status"] = "GPS Fix"
                    telemetry_state["timestamp"] = datetime.utcnow().isoformat()
                    log_to_daily_file()
        except Exception:
            pass

    def nmea_to_deg(self, raw: str, direction: str) -> float:
        if not raw: return 0.0
        dot_idx = raw.find('.')
        deg_digits = dot_idx - 2
        degrees = float(raw[:deg_digits])
        minutes = float(raw[deg_digits:])
        dec = degrees + (minutes / 60.0)
        if direction in ['S', 'W']: dec = -dec
        return dec

    def run_simulation(self):
        """Generates simulated 4WD trajectory across Australian Outback (Simpson Desert track demo)."""
        center_lat, center_lon = -25.8989, 137.9892  # Alice Springs / Outback Australia
        angle = 0
        speed_kmh = 55.0
        # Fixed demo constellation: (prn, gnss, elevation, azimuth, base SNR)
        sim_sky = [(2, "GPS", 72, 40, 42), (5, "GPS", 55, 150, 38), (12, "GPS", 30, 80, 33),
                   (19, "GPS", 18, 200, 24), (25, "GPS", 45, 260, 36), (29, "GPS", 8, 320, 16),
                   (31, "GPS", 62, 300, 40), (68, "GLONASS", 35, 110, 30), (84, "GLONASS", 50, 230, 34),
                   (85, "GLONASS", 12, 20, 19), (133, "SBAS", 40, 350, 28), (22, "GPS", 5, 170, 0)]

        while self.running:
            if os.path.exists('/dev/ttyUSB0') or os.path.exists('/dev/ttyACM0'):
                break

            angle += 0.02
            radius = 0.01
            sim_lat = center_lat + (radius * math.cos(angle))
            sim_lon = center_lon + (radius * math.sin(angle))
            heading = (math.degrees(math.atan2(math.sin(angle), math.cos(angle))) + 90) % 360

            with DATA_LOCK:
                telemetry_state["lat"] = round(sim_lat, 6)
                telemetry_state["lon"] = round(sim_lon, 6)
                telemetry_state["alt"] = 320.0 + math.sin(angle) * 15.0
                telemetry_state["speed"] = speed_kmh
                telemetry_state["speed_mph"] = round(speed_kmh * 0.621371, 1)
                telemetry_state["heading"] = round(heading, 1)
                sky_state["satellites"] = [{
                    "prn": prn, "gnss": gnss, "el": el,
                    "az": round((az + math.degrees(angle) * 0.05) % 360, 1),
                    "snr": max(0, round(snr + 3 * math.sin(angle * 3 + prn))) if snr else 0,
                    "used": snr >= 20,
                } for prn, gnss, el, az, snr in sim_sky]
                telemetry_state["satellites"] = sum(1 for s in sky_state["satellites"] if s["used"])
                telemetry_state["sats_visible"] = len(sim_sky)
                telemetry_state["hdop"] = 0.9
                telemetry_state["vdop"] = 1.2
                telemetry_state["fix_quality"] = 3
                telemetry_state["status"] = "Simulation Active"
                telemetry_state["timestamp"] = datetime.utcnow().isoformat()
                # Simulated positions are not written to the daily logs, so demo laps
                # never end up in the real trip history.

            time.sleep(1.0)


LAST_LOG_TIME = 0.0

# One log file per boot per day: telemetry_<day>.csv, then ...B.csv, ...C.csv after each reboot.
# Keyed on the kernel boot id, so restarting just the app service keeps appending to the same file.
def _read_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return f"pid-{os.getpid()}-{int(time.time())}"  # non-Linux (PC testing): one file per app run

BOOT_ID = _read_boot_id()
SESSIONS_FILE = ".sessions.json"   # per-day map of boot id -> log file name
_session_files: Dict[str, str] = {}


def file_suffix(index: int) -> str:
    """0 -> '', 1 -> 'B', ... 25 -> 'Z', 26 -> 'AA' (spreadsheet-style letters)."""
    if index == 0:
        return ""
    n, s = index + 1, ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def log_sort_key(name: str):
    """Order a day's files as base, B, C, ... Z, AA (plain string sort would put AA before B)."""
    stem = name.rsplit(".", 1)[0]
    suffix = stem[len("telemetry_YYYY-MM-DD"):] if stem.startswith("telemetry_") else stem
    return (len(suffix), suffix, name)


def session_log_path(day: str) -> str:
    """CSV path this boot writes to for the given UTC day, creating the next lettered file if needed."""
    if day in _session_files:
        return _session_files[day]
    daily_dir = os.path.join(LOG_DIR, day)
    os.makedirs(daily_dir, exist_ok=True)
    sessions_path = os.path.join(daily_dir, SESSIONS_FILE)
    try:
        with open(sessions_path, encoding="utf-8") as f:
            sessions = json.load(f)
    except (OSError, ValueError):
        sessions = {}
    name = sessions.get(BOOT_ID)
    if not name:
        taken = set(os.listdir(daily_dir)) | set(sessions.values())
        index = 0
        while f"telemetry_{day}{file_suffix(index)}.csv" in taken:
            index += 1
        name = f"telemetry_{day}{file_suffix(index)}.csv"
        sessions[BOOT_ID] = name
        try:
            with open(sessions_path, "w", encoding="utf-8") as f:
                json.dump(sessions, f, indent=1)
        except OSError as e:
            print(f"[LOGGER] Could not save {SESSIONS_FILE}: {e}")
        print(f"[LOGGER] This boot logs to {day}/{name}")
    _session_files[day] = os.path.join(daily_dir, name)
    return _session_files[day]

# Daily Auto-Rolling Logger Function
def log_to_daily_file():
    """Writes telemetry rows to date-partitioned CSV files for video overlays & ride analysis."""
    global LAST_LOG_TIME
    if not recording_state["is_recording"]:
        return

    now = time.time()
    # Testing Mode: Until October 1st 2026, log every 15 seconds. After Oct 1st 2026, log every 1 second for full 4WD trip.
    test_until = datetime(2026, 10, 1)
    log_interval = 15.0 if datetime.utcnow() < test_until else 1.0

    if (now - LAST_LOG_TIME) < log_interval:
        return

    LAST_LOG_TIME = now

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    csv_path = session_log_path(today_str)
    file_exists = os.path.exists(csv_path)

    headers = [
        "timestamp", "lat", "lon", "alt_m", "speed_kmh", "heading_deg",
        "satellites", "hdop", "vdop",
        "acc_x_g", "acc_y_g", "acc_z_g",
        "gyro_x_dps", "gyro_y_dps", "gyro_z_dps",
        "pitch_deg", "roll_deg", "g_total", "roughness_index"
    ]

    try:
        with open(csv_path, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(headers)
            
            writer.writerow([
                telemetry_state["timestamp"],
                telemetry_state["lat"],
                telemetry_state["lon"],
                telemetry_state["alt"],
                telemetry_state["speed"],
                telemetry_state["heading"],
                telemetry_state["satellites"],
                telemetry_state["hdop"],
                telemetry_state["vdop"],
                telemetry_state["acc_x"],
                telemetry_state["acc_y"],
                telemetry_state["acc_z"],
                telemetry_state["gyro_x"],
                telemetry_state["gyro_y"],
                telemetry_state["gyro_z"],
                telemetry_state["pitch"],
                telemetry_state["roll"],
                telemetry_state["g_total"],
                telemetry_state["roughness"]
            ])
            recording_state["points_logged_today"] += 1
            recording_state["current_log_file"] = csv_path
    except Exception as e:
        print(f"[LOGGER] Error writing to CSV: {e}")


# Track History & Stop Detection (reads the daily CSV logs)
TRACK_MIN_MOVE_M = 15.0     # keep a point once the vehicle has moved this far from the last kept one...
TRACK_HEARTBEAT_S = 300.0   # ...or every 5 min while parked, so stop durations stay accurate
STOP_RADIUS_M = 75.0        # GPS wander while parked stays well inside this
STOP_MIN_S = 3600.0         # stops of an hour or more become map markers
STOP_MAX_OUTLIERS = 2       # tolerate a couple of stray GPS jumps without ending a stop
GAP_SPLIT_S = 600.0         # break the drawn route across logging gaps longer than this...
GAP_SPLIT_M = 500.0         # ...if the vehicle also moved, so no straight lines are drawn across the map
MAX_DRAW_POINTS = 20000     # thin long histories further so the Pi's browser stays responsive

_track_cache: Dict[str, Dict[str, Any]] = {}
_track_cache_lock = threading.Lock()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


def parse_log_time(ts: str) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_track_points(csv_path: str) -> List[tuple]:
    """Thinned (epoch, lat, lon) points for one daily CSV. Only rows added since the last call are parsed."""
    with _track_cache_lock:
        entry = _track_cache.setdefault(csv_path, {"offset": 0, "points": [], "cols": None})
        try:
            size = os.path.getsize(csv_path)
        except OSError:
            return []
        if size < entry["offset"]:  # file was replaced
            entry.update(offset=0, points=[], cols=None)
        if size > entry["offset"]:
            with open(csv_path, "rb") as f:
                f.seek(entry["offset"])
                chunk = f.read()
            end = chunk.rfind(b"\n")  # leave a half-written last row for next time
            if end >= 0:
                entry["offset"] += end + 1
                # A power cut can leave runs of NUL bytes in the file; drop them.
                text = chunk[:end].replace(b"\0", b"").decode("utf-8", errors="ignore")
                points = entry["points"]
                for row in csv.reader(text.splitlines()):
                    if not row:
                        continue
                    if row[0] == "timestamp":
                        entry["cols"] = {name: i for i, name in enumerate(row)}
                        continue
                    cols = entry["cols"] or {"timestamp": 0, "lat": 1, "lon": 2}
                    try:
                        t = parse_log_time(row[cols["timestamp"]])
                        lat = float(row[cols["lat"]])
                        lon = float(row[cols["lon"]])
                    except (IndexError, ValueError):
                        continue
                    if t is None or (lat == 0 and lon == 0):
                        continue
                    if points:
                        last_t, last_lat, last_lon = points[-1]
                        if t - last_t < TRACK_HEARTBEAT_S and haversine_m(last_lat, last_lon, lat, lon) < TRACK_MIN_MOVE_M:
                            continue
                    points.append((t, lat, lon))
        return list(entry["points"])


def detect_stops(points: List[tuple]) -> List[Dict[str, Any]]:
    """Places where the vehicle stayed within STOP_RADIUS_M for at least STOP_MIN_S.
    A stop that spans a logging gap (e.g. Pi switched off overnight at camp) counts as one stop."""
    stops = []
    i, n = 0, len(points)
    while i < n:
        start_t, sum_lat, sum_lon = points[i][0], points[i][1], points[i][2]
        count, last_in, outliers = 1, i, 0
        j = i + 1
        while j < n:
            _, lat, lon = points[j]
            if haversine_m(sum_lat / count, sum_lon / count, lat, lon) <= STOP_RADIUS_M:
                sum_lat += lat
                sum_lon += lon
                count += 1
                last_in, outliers = j, 0
            else:
                outliers += 1
                if outliers > STOP_MAX_OUTLIERS:
                    break
            j += 1
        end_t = points[last_in][0]
        if end_t - start_t >= STOP_MIN_S:
            stops.append({
                "lat": round(sum_lat / count, 6),
                "lon": round(sum_lon / count, 6),
                "start": start_t,
                "end": end_t,
                "minutes": round((end_t - start_t) / 60),
                "ongoing": last_in == n - 1,
            })
            i = last_in + 1
        else:
            i += 1
    return stops


def split_segments(points: List[tuple]) -> List[List[tuple]]:
    segments: List[List[tuple]] = []
    for p in points:
        if segments:
            prev = segments[-1][-1]
            if p[0] - prev[0] > GAP_SPLIT_S and haversine_m(prev[1], prev[2], p[1], p[2]) > GAP_SPLIT_M:
                segments.append([p])
                continue
            segments[-1].append(p)
        else:
            segments.append([p])
    return segments


# Start Threads
imu_thread = M5StickIMUThread()
imu_thread.start()

gps_thread = GPSReaderThread()
gps_thread.start()


# REST API Routes
@app.get("/api/gps")
def get_telemetry():
    with DATA_LOCK:
        return {
            **telemetry_state,
            "points_logged_today": recording_state["points_logged_today"],
            "current_log_file": os.path.basename(recording_state["current_log_file"] or "")
        }

@app.get("/api/sats")
def get_satellites():
    with DATA_LOCK:
        return {
            "status": telemetry_state["status"],
            "fix_quality": telemetry_state["fix_quality"],
            "hdop": telemetry_state["hdop"],
            "vdop": telemetry_state["vdop"],
            "used": telemetry_state["satellites"],
            "visible": telemetry_state["sats_visible"],
            "satellites": list(sky_state["satellites"]),
        }

@app.get("/api/history")
def get_history(since: Optional[float] = None):
    """Route travelled (as line segments) and stops of an hour or more.
    `since` is a Unix time in seconds; omit it for the whole log history."""
    # Daily folders are named by UTC date, so skip whole days before `since` (keeping one extra day,
    # so an overnight camp that began the day before still gets its true start time).
    since_day = datetime.fromtimestamp(since - 86400, timezone.utc).strftime("%Y-%m-%d") if since else ""
    points: List[tuple] = []
    if os.path.exists(LOG_DIR):
        for day in sorted(os.listdir(LOG_DIR)):
            folder = os.path.join(LOG_DIR, day)
            if day < since_day or not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                if name.endswith(".csv"):
                    points.extend(load_track_points(os.path.join(folder, name)))
    points.sort(key=lambda p: p[0])

    # Find stops before trimming to `since`, so a camp that began earlier keeps its true start time.
    stops = [s for s in detect_stops(points) if not since or s["end"] >= since]
    if since:
        points = [p for p in points if p[0] >= since]

    step = max(1, math.ceil(len(points) / MAX_DRAW_POINTS))
    segments = []
    for seg in split_segments(points):
        thinned = seg[::step]
        if thinned[-1] is not seg[-1]:
            thinned.append(seg[-1])
        segments.append([[round(lat, 6), round(lon, 6)] for _, lat, lon in thinned])

    return {
        "segments": segments,
        "stops": stops,
        "first": points[0][0] if points else None,
        "last": points[-1][0] if points else None,
    }

LOG_LIST_DAYS = 10

@app.get("/api/logs/list")
def list_daily_logs():
    """Every log file from the last LOG_LIST_DAYS days (all boots: base, B, C, ...), newest day first."""
    oldest = (datetime.utcnow() - timedelta(days=LOG_LIST_DAYS - 1)).strftime("%Y-%m-%d")
    log_tree = []
    if os.path.exists(LOG_DIR):
        for date_folder in sorted(os.listdir(LOG_DIR), reverse=True):
            folder_path = os.path.join(LOG_DIR, date_folder)
            if date_folder < oldest or not os.path.isdir(folder_path):
                continue
            names = sorted((f for f in os.listdir(folder_path) if f.endswith((".csv", ".gpx"))), key=log_sort_key)
            files = [{"name": f, "size": os.path.getsize(os.path.join(folder_path, f))} for f in names]
            if files:
                log_tree.append({"date": date_folder, "files": files})
    return {"logs": log_tree, "days": LOG_LIST_DAYS}

@app.get("/api/logs/download/{date_str}/{filename}")
def download_log_file(date_str: str, filename: str):
    # Only serve files inside LOG_DIR: reject "..", separators and anything resolving elsewhere.
    for part in (date_str, filename):
        if part in ("", ".", "..") or "/" in part or "\\" in part or "\0" in part:
            raise HTTPException(status_code=400, detail="Invalid path")
    base = os.path.realpath(LOG_DIR)
    file_path = os.path.realpath(os.path.join(base, date_str, filename))
    if not file_path.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="Invalid path")
    if os.path.isfile(file_path):
        return FileResponse(file_path, filename=filename)
    raise HTTPException(status_code=404, detail="Log file not found")

# Serve static web frontend
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Make browsers (iOS Safari especially) revalidate the page and assets, so UI updates show on reload
@app.middleware("http")
async def no_cache_ui(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

@app.get("/")
def read_root():
    return FileResponse(os.path.join(static_dir, "index.html"))

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
