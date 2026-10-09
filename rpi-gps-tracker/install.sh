#!/bin/bash
# RPi 3 GPS Tracker Installer & Setup Script

set -e

echo "============================================="
echo "   Raspberry Pi 4 / 3 GPS Tracker Installer  "
echo "============================================="

# 1. System Package Updates & Dependencies
echo "[1/5] Installing required packages (gpsd, python3, chromium)..."
sudo apt-get update
sudo apt-get install -y \
    gpsd \
    gpsd-clients \
    python3 \
    python3-pip \
    python3-venv \
    chromium-browser \
    xdotool \
    unclutter \
    curl

# 2. Configure GPSD daemon
echo "[2/5] Configuring gpsd for USB GPS mouse..."
sudo systemctl stop gpsd.socket || true
sudo systemctl disable gpsd.socket || true

# Write /etc/default/gpsd
cat << 'EOF' | sudo tee /etc/default/gpsd
# Default settings for gpsd
START_DAEMON="true"
USBAUTO="true"
DEVICES="/dev/ttyUSB0 /dev/ttyACM0"
GPSD_OPTIONS="-n -F /var/run/gpsd.sock"
EOF

sudo systemctl restart gpsd

# 3. Python Virtual Environment Setup
echo "[3/5] Setting up Python Virtual Environment..."
cd "$(dirname "$0")"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Create Systemd Service for App Backend
echo "[4/5] Creating background systemd service for GPS Tracker..."
APP_DIR=$(pwd)
USER_NAME=$(whoami)

cat << EOF | sudo tee /etc/systemd/system/rpi-gps-backend.service
[Unit]
Description=RPi GPS Tracker FastAPI Service
After=network.target gpsd.service

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable rpi-gps-backend.service
sudo systemctl restart rpi-gps-backend.service

# 5. Create Kiosk Mode Auto-Start for 3.5" Display
echo "[5/5] Creating Kiosk mode auto-start script..."
mkdir -p ~/.config/autostart

cat << EOF > ~/.config/autostart/rpi-gps-kiosk.desktop
[Desktop Entry]
Type=Application
Name=RPi GPS Kiosk
Exec=chromium-browser --kiosk --incognito --noerrdialogs --disable-infobars http://localhost:8000
X-GNOME-Autostart-enabled=true
EOF

echo ""
echo "============================================="
echo "  SUCCESS! RPi GPS Tracker setup complete.   "
echo "============================================="
echo "1. Plug in your USB GPS mouse."
echo "2. Test GPS hardware by running: cgps"
echo "3. Open browser on RPi or phone/laptop at: http://$(hostname -I | awk '{print $1}'):8000"
echo "4. Reboot your Pi to test full autostart kiosk!"
echo "============================================="
