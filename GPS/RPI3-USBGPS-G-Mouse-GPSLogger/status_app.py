import tkinter as tk
from tkinter import scrolledtext
from collections import deque

# Constants
MAX_MESSAGES = 10  # Number of messages to display

class StatusApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Status Message Display")

        # Create a text area with a scrollbar
        self.text_area = scrolledtext.ScrolledText(root, wrap=tk.WORD, height=20, width=50, state=tk.DISABLED)
        self.text_area.pack(padx=10, pady=10)

        # Initialize a deque to keep the last 10 messages
        self.message_log = deque(maxlen=MAX_MESSAGES)

    def append_message(self, message):
        """Append a new message to the message log and update the text area."""
        # Add message to the log
        self.message_log.append(message)

        # Update the text area
        self.text_area.configure(state=tk.NORMAL)
        self.text_area.delete(1.0, tk.END)  # Clear existing text
        for msg in self.message_log:
            self.text_area.insert(tk.END, msg + "\n")
        self.text_area.configure(state=tk.DISABLED)

def simulate_status_updates(app):
    """Simulate status updates for demonstration purposes."""
    import time
    import random

    status_messages = [
        "System starting...",
        "Connecting to GPS...",
        "GPS signal acquired.",
        "Logging data...",
        "Device stationary.",
        "Error: GPS connection lost.",
        "Reconnecting...",
        "Data saved.",
        "Checking USB connection...",
        "USB connected and ready."
    ]

    while True:
        # Simulate receiving a new status message
        message = random.choice(status_messages)
        app.append_message(message)
        time.sleep(3)  # Update every 3 seconds

if __name__ == "__main__":
    # Create the main window
    root = tk.Tk()
    app = StatusApp(root)

    # Start a thread to simulate status updates
    import threading
    status_thread = threading.Thread(target=simulate_status_updates, args=(app,), daemon=True)
    status_thread.start()

    # Start the Tkinter main loop
    root.mainloop()
