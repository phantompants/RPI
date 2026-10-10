import os
import shutil
import time
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

# Define constants
LOG_DIRECTORY = "/home/gps/GPS Logs"
USB_MOUNT_POINT = "/media/usb"
NUMBER_OF_LOGS = 3
COPY_DELAY = 10  # Delay in seconds after USB insertion

# Function to show a popup message
def show_popup(message):
    root = tk.Tk()
    root.withdraw()  # Hide the root window
    messagebox.showinfo("USB Copy", message)
    root.destroy()

def get_latest_files(directory, number_of_files):
    """Get the latest `number_of_files` from the specified directory."""
    files = sorted(Path(directory).glob('*.gpx'), key=os.path.getmtime, reverse=True)
    return files[:number_of_files]

def copy_files_to_usb():
    """Copy the latest GPS log files to the USB stick."""
    if not os.path.ismount(USB_MOUNT_POINT):
        show_popup("USB is not mounted. Please check the connection.")
        return

    latest_files = get_latest_files(LOG_DIRECTORY, NUMBER_OF_LOGS)
    if not latest_files:
        show_popup("No log files found.")
        return

    files_copied = 0
    for file_path in latest_files:
        destination = os.path.join(USB_MOUNT_POINT, file_path.name)

        if os.path.exists(destination):
            show_popup(f"File {file_path.name} already exists on the USB. It will be overwritten.")

        try:
            shutil.copy(file_path, destination)
            files_copied += 1
            show_popup(f"Copied {file_path.name} to USB.")
        except Exception as e:
            show_popup(f"Error copying {file_path.name}: {e}")

    if files_copied > 0:
        show_popup("All files have been copied.")
        unmount_usb()

def unmount_usb():
    """Unmount the USB stick."""
    if os.path.ismount(USB_MOUNT_POINT):
        try:
            os.system(f"sudo umount {USB_MOUNT_POINT}")
            show_popup("USB stick is safe to remove.")
        except Exception as e:
            show_popup(f"Error unmounting USB stick: {e}")
    else:
        show_popup("USB stick is not mounted.")

def monitor_usb_insertion():
    """Monitor for USB insertion and handle file operations."""
    usb_detected = False
    while True:
        if os.path.ismount(USB_MOUNT_POINT):
            if not usb_detected:
                show_popup("USB detected. Waiting before copying files...")
                time.sleep(COPY_DELAY)  # Wait before copying files
                copy_files_to_usb()
                usb_detected = True
        else:
            usb_detected = False

        time.sleep(1)  # Check every second

if __name__ == "__main__":
    monitor_usb_insertion()
