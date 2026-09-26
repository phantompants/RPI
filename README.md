# 📡 Raspberry Pi 4 / Pi 3 USB GPS & IMU Tracker

A complete, beginner-friendly 4WD GPS & IMU tracking application designed for **Raspberry Pi 4 / 3** with a **USB GPS mouse**, **M5StickC Plus2 IMU**, and a **3.5-inch touchscreen LCD**.

---

## 🌟 Key Features
- 🚀 **Digital Speedometer**: Displays live speed with unit toggle (`KM/H` ⇄ `MPH`).
- 🗺️ **Live Interactive Map**: Dark-themed touch-optimized Leaflet map with auto-centering & position marker.
- 📊 **Telemetry Dashboard**: Live Lat/Lon, Altitude, Heading, Satellite Count, and GPS Fix Quality.
- 🔴 **Breadcrumb & Track Recorder**: Record routes with live path drawing.
- 🧭 **Trip History Overlay**: The map draws where you have been from the daily logs (tap the **1D / 3D / 7D / ALL / OFF** button, top-right of the map).
- ⏸️ **Stop Markers**: Every stop of 1 hour or more (lunch, camps, overnight with the Pi switched off) gets a gold marker showing how long you stayed. Tap for arrival/departure times.
- 🛰️ **Satellite Status Page**: Sky plot and signal-strength (SNR) bars per constellation (GPS, GLONASS, Galileo, BeiDou…). Tap **SATS** or the SATS/HDOP pill, or bookmark `http://<PI_IP>:8000/#sats`.
- 💾 **GPX Export**: Save and export `.gpx` files compatible with Strava, Google Earth, and Garmin.
- 📱 **Dual View**: Display on 3.5" Pi LCD screen (Kiosk Mode) **AND** view remotely from any smartphone/laptop on the same Wi-Fi.
- 🎮 **Built-in Simulation Mode**: Automatic demo mode if no physical GPS is attached—allows full testing on Windows/Mac/Linux!

---

## 📁 Project File Structure
```
rpi-gps-tracker/
├── app.py             # Python FastAPI server (GPS hardware reader + Simulation engine + API)
├── requirements.txt   # Python dependencies
├── install.sh         # One-click Raspberry Pi setup & installation script
├── rpi_remote.py      # Run commands on the Pi over SSH from your PC
├── m5stick_firmware/  # Arduino sketch: M5StickC Plus2 IMU → USB serial JSON
├── cyd_display/       # Arduino sketch: CYD (ESP32-2432S028) Wi-Fi remote screen
├── README.md          # Complete step-by-step beginner guide
└── static/
    ├── index.html     # Touchscreen UI layout optimized for 3.5" display (320x480 / 480x320)
    ├── style.css      # Dark modern glassmorphism UI styles
    └── app.js         # Leaflet map logic & telemetry updates
```

---

## 🛠️ Step-by-Step Beginner Walkthrough

### Step 1: Flashing Raspberry Pi OS
1. Download & open **[Raspberry Pi Imager](https://www.raspberrypi.com/software/)** on your computer.
2. Choose OS: **Raspberry Pi OS (64-bit or 32-bit with Desktop)**.
3. Choose Storage: Your MicroSD card.
4. Click the **Gear icon ⚙️ (Advanced Settings)**:
   - Enable **SSH** (Set password).
   - Configure **Wi-Fi** (SSID and Password).
   - Set Hostname (e.g. `raspberrypi`).
5. Click **Write** and insert the SD card into your RPi 3.

---

### Step 2: Setting Up the 3.5-inch Touchscreen Display
Most 3.5-inch displays (like Waveshare 3.5", KeDei, or generic LCD35) use GPIO pins and require driver installation.

1. SSH into your Pi or open Terminal:
   ```bash
   ssh pi@raspberrypi.local
   ```
2. Download and run `LCD-show` drivers (standard for 95% of 3.5" GPIO displays):
   ```bash
   git clone https://github.com/goodtft/LCD-show.git
   chmod -R 755 LCD-show
   cd LCD-show/
   sudo ./LCD35-show
   ```
   *(The Pi will automatically reboot with the display activated in landscape 480x320 resolution).*

---

### Step 3: Plugging in the USB GPS Mouse
1. Plug your USB GPS dongle (e.g. VK-162) into any USB port on the Raspberry Pi 3.
2. Verify the Pi sees the device by running:
   ```bash
   lsusb
   ```
   *You should see a device like `u-blox AG` or `Prolific PL2303` / `Silicon Labs CP210x`.*

3. Check if serial port `/dev/ttyUSB0` or `/dev/ttyACM0` is created:
   ```bash
   ls /dev/ttyUSB* /dev/ttyACM*
   ```

---

### Step 4: Installing & Running the App
1. Transfer this `rpi-gps-tracker` folder to your Pi (or download it directly).
2. Open terminal in the project directory:
   ```bash
   cd rpi-gps-tracker
   ```
3. Run the automated installer script:
   ```bash
   chmod +x install.sh
   ./install.sh
   ```
4. The installer will automatically:
   - Install `gpsd`, `gpsd-clients`, Python libraries, and Chromium.
   - Configure `/etc/default/gpsd` to listen to `/dev/ttyUSB0`.
   - Set up `systemd` background service (`rpi-gps-backend.service`).
   - Configure Chromium **Kiosk Mode** to start the GPS UI on boot!

---

### Step 5: How to Test Hardware Manually
Want to see raw GPS satellite data? Run:
```bash
cgps
```
or
```bash
gpsmon
```
*(Make sure your USB GPS is near a window or outdoors to acquire satellite fix! Indoor walls block GPS signals).*

---

### Step 6: Testing on PC / Laptop (Simulator Mode)
You don't even need the Pi to try out the app UI!
On your current computer:
1. Make sure Python 3 is installed.
2. Open terminal in `rpi-gps-tracker`:
   ```bash
   pip install -r requirements.txt
   python app.py
   ```
3. Open `http://localhost:8000` in your web browser.
4. The app will detect no GPS mouse attached and instantly start **Simulation Mode**, simulating a moving vehicle!

---

## 🎯 M5Stick IMU Datum (zero level)
Mount the M5StickC Plus2 at any angle. Park on level ground, then **hold the big front button (A) for 1 second** and keep still: that orientation becomes 0° pitch / 0° roll. The datum is stored on the stick and survives power cuts; the screen shows **DATUM** (or **RAW** if none). **Hold the side button (B) for 3 seconds** to clear it. The stick rotates all accelerometer and gyro readings into the datum frame before sending, so the Pi, logs and screens need no changes.

The stick can be re-flashed through the Pi without unplugging it: copy the compiled `.bin` to the Pi, stop `rpi-gps-backend`, then run `venv/bin/esptool --chip esp32 --port <M5Stick port> write_flash 0x10000 m5stick_firmware.ino.bin`.

---

## 📟 CYD Remote Screen (ESP32-2432S028 "Cheap Yellow Display")
A dash-mounted screen that shows live data from the Pi over the vehicle Wi-Fi. Tap the screen to cycle **Dashboard → Inclinometer → Satellites**.

1. Flash `cyd_display/` (board **ESP32 Dev Module**, partition **Huge APP**; libraries **LovyanGFX**, **WiFiManager**, **ArduinoJson**):
   ```bash
   arduino-cli compile --fqbn esp32:esp32:esp32:PartitionScheme=huge_app cyd_display
   arduino-cli upload -p COM6 --fqbn esp32:esp32:esp32:PartitionScheme=huge_app cyd_display
   ```
2. On first boot it creates the hotspot **GPS-Display-Setup**. Join it from your phone, choose the vehicle Wi-Fi (GL.iNet), enter its password and the Pi's name (`gps4.local`) or IP.
3. LED: green = live data, red = no link to the Pi, blue = Wi-Fi setup. Hold the screen while powering on to redo Wi-Fi setup.
4. Wrong orientation or mirrored? This USB-C CYD scans natively in landscape and uses orientation option 2 (the default). For another board, send `t` over serial (115200) to show a test pattern, then tap the screen to cycle orientation options 1–4 (saved on each tap) and send `t` again to return. Other serial commands: `i` invert colours, `f` lower SPI speed, `d` switch ILI9341/ST7789 driver, `s` status.

---

## ⚡ Quick Troubleshooting & Helpful Tips
- **No GPS Fix?** GPS dongles take 1–3 minutes on "cold start" outdoors to lock onto 4+ satellites. Give it line-of-sight to the sky.
- **Screen Touch Calibration**: If touch coordinates are inverted, run `sudo ./LCD35-show 90` or use `xinput-calibrator`.
- **Stuck on the Raspberry Pi splash screen?** The 3.5" LCD driver (LCD-show) turns off the `vc4-kms-v3d` graphics driver, so the desktop must not wait for it. Make sure `/etc/lightdm/lightdm.conf` has `logind-check-graphical=false` under `[LightDM]`, and check `~/.local/share/xorg/Xorg.0.log` for `(EE)` config errors in `/etc/X11/xorg.conf.d/`. X must draw on the LCD framebuffer, not HDMI: add `/etc/X11/xorg.conf.d/99-lcd35-fb1.conf` with a `Device` section using `Driver "fbdev"` and `Option "fbdev" "/dev/fb1"` (check with `cat /sys/class/graphics/fb1/name`, which should be `fb_ili9486`). Run only one X server: don't also `startx` from `~/.bash_profile`.
- **GPS and M5Stick swapped?** `ttyACM0`/`ttyACM1` numbering changes between boots. gpsd should point at the GPS by identity: `DEVICES="/dev/serial/by-id/usb-u-blox_..."` with `USBAUTO="false"` in `/etc/default/gpsd`. The app finds both devices by identity automatically.
- **Power**: `vcgencmd get_throttled` should print `0x0`. Anything else means undervoltage; use a proper 5.1V 3A supply. Sudden power loss can leave NUL bytes in the day's CSV (the app skips them).
- **Remote commands from your PC**: create `pi_config.json` (`{"host": "...", "user": "...", "password": "..."}`, git-ignored), `pip install paramiko`, then `python rpi_remote.py "uptime"`.
- **View on Smartphone**: Connect your phone to the same Wi-Fi, find your Pi's IP address (`hostname -I`), and navigate to `http://<YOUR_PI_IP>:8000`.

---
Happy Tracking! 🚀
