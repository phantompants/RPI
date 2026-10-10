# GPS Logger for Raspberry Pi

This project sets up a GPS logger on a Raspberry Pi that records GPS data every 2 meters of movement and pauses logging when stationary. The data is saved in GPX format and stored in a specific directory. This README provides instructions on how to set up, configure, and run the GPS logger.

I used ChatGPS with this initial prompt, and many error reiterations to finish the job:
Explain how to setup a RPI3 and USB G-Mouse to create a GPS logger. The Logger must save a file each new day (Sydney, Australia time) in YYYYMMDD-GPS.gpx format.
The log file should include: Lat,Long,elevation,speed,date,time, satellites.

I also added the feature to copy the last 3 GPS files to USB upon mount of USB, and then unmount when complete.

When I finally got it working I even asked ChatGPT to write the stuff I needed for Github:

## Requirements

- Raspberry Pi 3 or later
- GPS receiver (e.g., USB G-Mouse)
- Raspbian OS or similar Debian-based system
- Python 3
- Virtual environment (optional but recommended)

## Setup Instructions

### 1. Prepare Your Raspberry Pi

1.a. **Update and Upgrade System Packages**:
   ```bash
   sudo apt update
   sudo apt upgrade

1.b  **Install Required Packages**:
Install Required Packages:
bash
Copy code
sudo apt install python3-pip python3-venv gpsd gpsd-clients python3-gps

2. Connect and Configure the GPS Receiver
Connect the GPS Receiver to the Raspberry Pi via USB.

Determine the Device Path:

bash
Copy code
dmesg | grep tty
Look for a device like /dev/ttyACM0.

Configure gpsd:
Edit the configuration file:

bash
Copy code
sudo nano /etc/default/gpsd
Ensure it contains:

bash
Copy code
START_DAEMON="true"
GPSD_OPTIONS="-n"
DEVICES="/dev/ttyACM0"  # Replace with your GPS device path
USBAUTO="true"
Restart the gpsd service:

bash
Copy code
sudo systemctl restart gpsd

3. Create and Activate a Virtual Environment
Create a Virtual Environment:

bash
Copy code
python3 -m venv myenv
Activate the Virtual Environment:

bash
Copy code
source myenv/bin/activate
Install Required Python Packages:

bash
Copy code
pip install gps3 gpxpy geopy

4. Create the Python Script
Save the following Python script as gps_logger.py:

5. Run the Script
Ensure Virtual Environment is Activated:

bash
Copy code
source myenv/bin/activate
Run the Script:

bash
Copy code
python3 gps_logger.py
Verify Output:

Check the directory /home/gps/GPS_Logs to ensure GPX files are being created and updated as expected.

Troubleshooting
gpsd Service Issues:

Ensure gpsd is properly configured in /etc/default/gpsd.
Restart the service with sudo systemctl restart gpsd.
Check status with systemctl status gpsd.
Python Package Errors:

Ensure all required packages are installed in the virtual environment.
Use pip to install missing packages.
GPS Data Issues:

Verify that the GPS receiver is properly connected and recognized.
Check the GPS data using cgps or gpsmon.
Contributing
Feel free to contribute by opening issues, suggesting improvements, or submitting pull requests.

License
This project is licensed under the MIT License. See the LICENSE file for details.

Acknowledgments
Thanks to the GPSD project for providing the GPS daemon and the libraries used in this project.

