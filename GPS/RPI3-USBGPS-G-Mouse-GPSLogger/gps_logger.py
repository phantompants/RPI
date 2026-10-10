from gps3 import gps3
import gpxpy
import gpxpy.gpx
import time
from datetime import datetime
import os
from geopy.distance import great_circle
import subprocess

# Initialize GPS socket and data stream
gps_socket = gps3.GPSDSocket()
gps_stream = gps3.DataStream()

# Connect to GPSD
try:
    gps_socket.connect()
    gps_socket.watch()
except Exception as e:
    print(f"Failed to connect to GPSD: {e}")
    exit(1)

# Define constants
LOG_DIRECTORY = "/home/gps/GPS_Logs"
LOG_INTERVAL_METERS = 1  # Log every 1 meters
STATIONARY_THRESHOLD_SECONDS = 60  # Time to consider as stationary before pausing

def create_gpx_file():
    now = datetime.now()
    filename = now.strftime("%Y%m%d") + "-GPS.gpx"
    directory = LOG_DIRECTORY  # Use the defined directory
    if not os.path.exists(directory):
        os.makedirs(directory)  # Create the directory if it does not exist
    filepath = os.path.join(directory, filename)
    gpx = gpxpy.gpx.GPX()
    return filepath, gpx

def log_gps_data(filepath, gpx):
    last_position = None
    last_log_time = datetime.now()
    stationary_start_time = datetime.now()
    logging = True

    while True:
        try:
            for new_data in gps_socket:
                if new_data:
                    gps_stream.unpack(new_data)

                    # Extract GPS data
                    latitude = gps_stream.TPV.get('lat')
                    longitude = gps_stream.TPV.get('lon')
                    elevation = gps_stream.TPV.get('alt', 0)  # Default to 0 if altitude is not available
                    speed = gps_stream.TPV.get('speed', 0)    # Default to 0 if speed is not available
                    date = gps_stream.TPV.get('time', 'n/a')  # Default to 'n/a' if time is not available

                    if latitude is not None and longitude is not None and date != 'n/a':
                        current_time = datetime.utcnow()

                        # Convert date to datetime object
                        try:
                            time_obj = datetime.strptime(date, "%Y-%m-%dT%H:%M:%S")
                        except ValueError:
                            time_obj = current_time

                        # Check if we need to log data
                        if last_position:
                            distance_moved = great_circle((last_position[0], last_position[1]), (latitude, longitude)).meters
                            if distance_moved >= LOG_INTERVAL_METERS:
                                # Log data
                                gpx.waypoints.append(gpxpy.gpx.GPXWaypoint(
                                    latitude=latitude,
                                    longitude=longitude,
                                    elevation=elevation,
                                    time=time_obj
                                ))

                                # Save to file
                                with open(filepath, 'a') as f:  # Append to the file
                                    f.write(gpx.to_xml())

                                # Update last position and time
                                last_position = (latitude, longitude)
                                last_log_time = current_time
                                stationary_start_time = current_time

                                print(f"Logged data at {latitude}, {longitude}.")

                        else:
                            # Initial position
                            last_position = (latitude, longitude)
                            last_log_time = current_time

                        # Check if the device is stationary
                        if speed == 0:
                            # Check if we've been stationary for too long
                            if (current_time - stationary_start_time).total_seconds() >= STATIONARY_THRESHOLD_SECONDS:
                                logging = False
                                print("Device is stationary. Pausing logging.")
                        else:
                            # Device is moving, reset stationary timer and resume logging
                            stationary_start_time = current_time
                            logging = True

                        if logging:
                            time.sleep(10)  # Small sleep to avoid excessive CPU usage
                        else:
                            time.sleep(60)  # Sleep longer when stationary

        except Exception as e:
            print(f"An error occurred: {e}")
            time.sleep(60)  # Wait before retrying in case of error

if __name__ == "__main__":
    # Start gpsmon in a separate process
    try:
        subprocess.Popen(['gpsmon'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print("gpsmon started.")
    except Exception as e:
        print(f"Failed to start gpsmon: {e}")

    filepath, gpx = create_gpx_file()
    log_gps_data(filepath, gpx)
