import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import socket
import threading
import asyncio
import time
import re
import os

# Configuration
BROADCAST_PORT = 5001
TCP_PORT = 5000
TEMPLATES_FOLDER = "templates" # Define the templates folder name

# Global variables for async operations and device management
device_list = {}
current_client = None
loop = asyncio.new_event_loop()
app_running = True
download_file_path = None


def send_command_to_device(command, data=None):
    """Sends a command to the currently selected device and handles response."""
    global current_client

    if not current_client:
        result_text.set("ERROR: No device selected.")
        return

    def communicate():
        try:
            reader, writer = asyncio.run_coroutine_threadsafe(
                asyncio.open_connection(current_client, TCP_PORT),
                loop=loop
            ).result(timeout=5)

            if data is not None:
                full_command = f"{command},{data}\n"
            else:
                full_command = f"{command}\n"

            writer.write(full_command.encode('utf-8'))
            asyncio.run_coroutine_threadsafe(writer.drain(), loop=loop).result(timeout=5)

            # NOTE: For most commands (SEARCH, DELETE, EMPTY), the ESP32 sends a single line
            # starting with SUCCESS or ERROR, or an initial OK/INFO line followed by SUCCESS/ERROR.
            # We will read until we see a SUCCESS or ERROR to terminate, or until the connection closes.
            
            while True:
                line = asyncio.run_coroutine_threadsafe(
                    reader.readline(),
                    loop=loop
                ).result(timeout=5)

                if not line:
                    break
                response = line.decode('utf-8').strip()
                result_text.set(response)

                if response.startswith("SUCCESS") or response.startswith("ERROR"):
                    break

            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)

        except Exception as e:
            result_text.set(f"Communication ERROR: {e}")

    threading.Thread(target=communicate).start()

def cmd_enroll_and_upload():
    """Sends ENROLL command, then automatically triggers a template upload on success."""
    global current_client

    if not current_client:
        result_text.set("ERROR: No device selected.")
        return

    def enroll_and_upload():
        try:
            model_id = int(enroll_id_entry.get())
            result_text.set(f"Attempting ENROLL for ModelID {model_id}...")
            
            # Open a new connection for this operation
            reader, writer = asyncio.run_coroutine_threadsafe(
                asyncio.open_connection(current_client, TCP_PORT),
                loop=loop
            ).result(timeout=5)

            # Send the ENROLL command
            full_command = f"ENROLL,{model_id}\n"
            writer.write(full_command.encode('utf-8'))
            asyncio.run_coroutine_threadsafe(writer.drain(), loop=loop).result(timeout=5)

            # Read and display responses until success or error
            while True:
                line = asyncio.run_coroutine_threadsafe(
                    reader.readline(),
                    loop=loop
                ).result(timeout=60) # Increased timeout for user interaction

                if not line:
                    break
                response = line.decode('utf-8').strip()
                result_text.set(response)

                if response.startswith("SUCCESS"):
                    print(f"Enrollment successful. Initiating template upload for ModelID {model_id}...")
                    writer.close()
                    asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)
                    # Trigger the upload after a short delay
                    time.sleep(1) 
                    cmd_upload_template(model_id) 
                    return
                elif response.startswith("ERROR"):
                    writer.close()
                    asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)
                    return
            
            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)

        except Exception as e:
            result_text.set(f"Communication ERROR: {e}")

    threading.Thread(target=enroll_and_upload).start()

def cmd_search():
    send_command_to_device("SEARCH")


# --- START: Corrected cmd_listtemplates function ---
def cmd_listtemplates():
    """Sends LIST command and handles the multi-line response."""
    global current_client

    if not current_client:
        result_text.set("ERROR: No device selected.")
        return

    def communicate():
        try:
            reader, writer = asyncio.run_coroutine_threadsafe(
                asyncio.open_connection(current_client, TCP_PORT),
                loop=loop
            ).result(timeout=5)

            full_command = f"LIST\n"
            writer.write(full_command.encode('utf-8'))
            asyncio.run_coroutine_threadsafe(writer.drain(), loop=loop).result(timeout=5)
            
            all_responses = []
            
            # The ESP32 sends a multi-line response for LIST, terminating with a final OK message.
            while True:
                line = asyncio.run_coroutine_threadsafe(
                    reader.readline(),
                    loop=loop
                ).result(timeout=5)

                if not line:
                    break
                response = line.decode('utf-8').strip()
                all_responses.append(response)

                # Wait for the final confirmation message from the ESP32
                if response.startswith("OK: List templates command complete."):
                    break
            
            # Display the collected responses
            if all_responses:
                # Join the individual lines for a cleaner display
                final_output = "\n".join(all_responses)
                result_text.set(final_output)
            else:
                result_text.set("ERROR: No response received from device.")
            
            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)

        except Exception as e:
            result_text.set(f"Communication ERROR: {e}")

    threading.Thread(target=communicate).start()
# --- END: Corrected cmd_listtemplates function ---


def cmd_empty_device():
    """Initiates device empty by issuing a confirmation dialog and sending EMPTY command."""
    global current_client

    if not current_client:
        result_text.set("ERROR: No device selected.")
        return

    # 1. Confirmation Dialog
    if not messagebox.askyesno(
        "Confirm Device Empty", 
        "⚠️ WARNING: This action will erase the entire device flash memory. Are you sure?"
    ):
        result_text.set("Device empty operation cancelled by user.")
        return

    # 2. Send the EMPTY command
    send_command_to_device("EMPTY")

def cmd_deletechar():
    try:
        model_id = int(delete_id_entry.get())
        send_command_to_device("DELETE", model_id)
    except ValueError:
        result_text.set("Invalid ModelID. Please enter a number.")


def cmd_upload_template(model_id=None):
    """Sends UPLOAD_TEMPLATE command and saves the received file."""
    global current_client

    if not current_client:
        result_text.set("ERROR: No device selected.")
        return
        
    if model_id is None:
        try:
            model_id = int(upload_id_entry.get())
        except ValueError:
            result_text.set("Invalid ModelID. Please enter a number.")
            return

    def upload():
        try:
            # Create the 'templates' folder if it doesn't exist
            if not os.path.exists(TEMPLATES_FOLDER):
                os.makedirs(TEMPLATES_FOLDER)
                
            filename = os.path.join(TEMPLATES_FOLDER, f"template_{model_id}.mb")

            result_text.set(f"Attempting to upload template ID {model_id}...")

            reader, writer = asyncio.run_coroutine_threadsafe(
                asyncio.open_connection(current_client, TCP_PORT),
                loop=loop
            ).result(timeout=5)

            # Send the upload command
            full_command = f"UPLOAD_TEMPLATE,{model_id}\n"
            writer.write(full_command.encode('utf-8'))
            asyncio.run_coroutine_threadsafe(writer.drain(), loop=loop).result(timeout=5)

            # Wait for the "OK: File transfer commencing." message
            ack = asyncio.run_coroutine_threadsafe(reader.readline(), loop=loop).result(timeout=15)
            ack_msg = ack.decode('utf-8').strip()
            result_text.set(ack_msg)
            if not ack_msg.startswith("OK"):
                writer.close()
                asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)
                return

            # Read the exact amount of data
            template_data = b''
            expected_size = 1668
            while len(template_data) < expected_size:
                chunk = asyncio.run_coroutine_threadsafe(reader.read(expected_size - len(template_data)), loop=loop).result(timeout=60)
                if not chunk:
                    # Connection closed unexpectedly
                    break
                template_data += chunk

            # Save the received data
            with open(filename, 'wb') as f:
                f.write(template_data)

            result_text.set(
                f"SUCCESS: Template saved to {filename} ({len(template_data)} bytes received)"
            )

            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)

        except Exception as e:
            import traceback
            result_text.set(f"Communication ERROR: {e}")
            print("Detailed traceback:")
            traceback.print_exc()

    threading.Thread(target=upload).start()

def select_download_file():
    global download_file_path
    download_file_path = filedialog.askopenfilename(
        defaultextension=".mb",
        filetypes=[("Template files", "*.mb")]
    )
    if download_file_path:
        download_file_label.config(text=f"File selected: {download_file_path.split('/')[-1]}")
    else:
        download_file_label.config(text="No file selected.")

# Renamed old download function to be a sequence step, returning status
def _download_template_sequence(model_id, file_path):
    """Internal function for synchronous download steps, used by cmd_download_template and cmd_sync_device."""
    global current_client
    
    status = False
    
    try:
        result_text.set(f"Syncing: Downloading template ID {model_id} from file...")

        reader, writer = asyncio.run_coroutine_threadsafe(
            asyncio.open_connection(current_client, TCP_PORT),
            loop=loop
        ).result(timeout=5)

        # Send the download command with ModelID
        full_command = f"DOWNLOAD_TEMPLATE,{model_id}\n"
        writer.write(full_command.encode('utf-8'))
        asyncio.run_coroutine_threadsafe(writer.drain(), loop=loop).result(timeout=5)

        # Wait for "OK" acknowledgment from Arduino
        ack = asyncio.run_coroutine_threadsafe(reader.readline(), loop=loop).result(timeout=15)
        ack_msg = ack.decode('utf-8').strip()
        result_text.set(f"Syncing (ID {model_id}): {ack_msg}")
        
        if not ack_msg.startswith("OK"):
            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)
            return False
        
        # Read the binary data from the file
        with open(file_path, 'rb') as f:
            template_data = f.read()
        
        if len(template_data) != 1668:
            result_text.set(f"ERROR (ID {model_id}): Invalid file size ({len(template_data)} bytes). Skipping.")
            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)
            return False
            
        # Send the binary data
        writer.write(template_data)
        asyncio.run_coroutine_threadsafe(writer.drain(), loop=loop).result(timeout=5)
        
        # Wait for the final SUCCESS/ERROR response from Arduino
        final_response = asyncio.run_coroutine_threadsafe(reader.readline(), loop=loop).result(timeout=15)
        final_msg = final_response.decode('utf-8').strip()

        if final_msg.startswith("SUCCESS"):
            result_text.set(f"SUCCESS (ID {model_id}): Template successfully downloaded to device.")
            status = True
        else:
            result_text.set(f"ERROR (ID {model_id}): {final_msg}")
            status = False

        writer.close()
        asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)

    except Exception as e:
        import traceback
        result_text.set(f"Communication ERROR during sync (ID {model_id}): {e}")
        print(f"Detailed traceback for sync (ID {model_id}):")
        traceback.print_exc()
        status = False
        
    return status

# Original cmd_download_template now uses the synchronous helper
def cmd_download_template():
    """Sends DOWNLOAD_TEMPLATE command for a single selected file."""
    global current_client, download_file_path

    if not current_client:
        result_text.set("ERROR: No device selected.")
        return

    if not download_file_path:
        result_text.set("ERROR: No template file selected.")
        return
    
    # Extract ModelID from filename
    match = re.search(r"template_(\d+)\.mb", download_file_path)
    if not match:
        result_text.set("ERROR: Filename must be in the format 'template_[id].mb'")
        return
    model_id = int(match.group(1))

    # Run the synchronous download in a thread
    threading.Thread(target=lambda: _download_template_sequence(model_id, download_file_path)).start()


# Sync Device Button functionality
def cmd_sync_device():
    """Initiates device sync by downloading all templates from the local folder."""
    global current_client

    if not current_client:
        result_text.set("ERROR: No device selected.")
        return

    # 1. Confirmation Dialog
    if not messagebox.askyesno(
        "Confirm Device Sync", 
        f"Are you sure you want to download ALL templates from the '{TEMPLATES_FOLDER}' folder to the device at {current_client}? This will overwrite existing templates on the sensor."
    ):
        result_text.set("Device sync cancelled by user.")
        return

    def sync_templates():
        if not os.path.exists(TEMPLATES_FOLDER):
            result_text.set(f"ERROR: Templates folder '{TEMPLATES_FOLDER}' not found.")
            return

        template_files = [f for f in os.listdir(TEMPLATES_FOLDER) if f.endswith('.mb')]
        if not template_files:
            result_text.set(f"INFO: No template files found in '{TEMPLATES_FOLDER}'. Sync complete.")
            return

        result_text.set(f"Starting sync of {len(template_files)} templates...")
        
        success_count = 0
        total_count = len(template_files)

        for filename in template_files:
            file_path = os.path.join(TEMPLATES_FOLDER, filename)
            match = re.search(r"template_(\d+)\.mb", filename)
            
            if not match:
                result_text.set(f"Skipping file: {filename} (Invalid filename format).")
                continue
            
            model_id = int(match.group(1))
            
            # Perform the download synchronously
            if _download_template_sequence(model_id, file_path):
                success_count += 1
            
            # Wait a short moment between downloads to ensure the sensor is ready
            time.sleep(0.5)

        result_text.set(f"SYNC COMPLETE: {success_count} of {total_count} templates successfully downloaded to device.")

    # Run the sync process in a separate thread to keep the GUI responsive
    threading.Thread(target=sync_templates).start()


def discover_devices():
    global app_running
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    listener.settimeout(1)
    listener.bind(('', BROADCAST_PORT))

    while app_running:
        try:
            data, addr = listener.recvfrom(1024)
            message = data.decode('utf-8').strip().split(',')
            if len(message) == 2:
                device_ip = message[0]
                device_mac = message[1]
                if device_ip not in device_list:
                    device_list[device_ip] = device_mac
                    root.after(0, update_device_list)
        except socket.timeout:
            continue
        except Exception as e:
            print(f"UDP Error: {e}")


def update_device_list():
    device_listbox.delete(0, tk.END)
    for ip, mac in device_list.items():
        device_listbox.insert(tk.END, f"Fingerprint Sensor (IP: {ip}, MAC: {mac})")


def select_device(event):
    global current_client
    selection = device_listbox.curselection()
    if selection:
        selected_ip = list(device_list.keys())[selection[0]]
        current_client = selected_ip
        result_text.set(f"Selected device: {current_client}")
    else:
        current_client = None
        result_text.set("No device selected.")


# GUI Setup
root = tk.Tk()
root.title("FingerPrint Sensor Management Utility v1.0")
root.geometry("1000x700")

# Main frames
left_frame = tk.Frame(root, width=300, padx=10, pady=10)
right_frame = tk.Frame(root, padx=10, pady=10)
left_frame.pack(side="left", fill="y", expand=False)
right_frame.pack(side="right", fill="both", expand=True)

# Device Discovery Panel
discovery_panel = tk.LabelFrame(left_frame, text="Discovered Devices", padx=10, pady=10)
discovery_panel.pack(fill="x", pady=10)

device_listbox = tk.Listbox(discovery_panel, width=35, height=10)
device_listbox.pack(fill="x", expand=True)
device_listbox.bind('<<ListboxSelect>>', select_device)

# Control Panel
control_panel_frame = tk.LabelFrame(left_frame, text="Control Panel", padx=10, pady=10)
control_panel_frame.pack(fill="x", pady=10)

# Enroll button and input
enroll_frame = tk.Frame(control_panel_frame)
enroll_frame.pack(pady=10)
tk.Button(enroll_frame, text="Enroll", width=15, height=2, command=cmd_enroll_and_upload).pack(side="left", padx=5)
enroll_id_entry = tk.Entry(enroll_frame, width=10)
enroll_id_entry.insert(0, "1")
enroll_id_entry.pack(side="left", padx=5)
tk.Label(enroll_frame, text="ModelID").pack(side="left")

# Other commands
tk.Button(control_panel_frame, text="Search", width=15, height=2, command=cmd_search).pack(pady=5)
tk.Button(control_panel_frame, text="List Templates", width=15, height=2, command=cmd_listtemplates).pack(pady=5)

# --- NEW BUTTON ---
tk.Button(control_panel_frame, text="Empty Device", width=15, height=2, command=cmd_empty_device).pack(pady=15)
# ------------------

# Sync Device Button
tk.Button(control_panel_frame, text="Sync Device", width=15, height=2, command=cmd_sync_device).pack(pady=15)

# DeleteChar
delete_frame = tk.Frame(control_panel_frame)
delete_frame.pack(pady=5)
tk.Button(delete_frame, text="Delete", width=15, height=2, command=cmd_deletechar).pack(side="left", padx=5)
delete_id_entry = tk.Entry(delete_frame, width=10)
delete_id_entry.insert(0, "1")
delete_id_entry.pack(side="left", padx=5)
tk.Label(delete_frame, text="ModelID").pack(side="left")

# Template Upload
upload_frame = tk.Frame(control_panel_frame)
upload_frame.pack(pady=5)
tk.Button(upload_frame, text="Template Upload", width=15, height=2, command=lambda: cmd_upload_template()).pack(side="left", padx=5)
upload_id_entry = tk.Entry(upload_frame, width=10)
upload_id_entry.insert(0, "1")
upload_id_entry.pack(side="left", padx=5)
tk.Label(upload_frame, text="ModelID").pack(side="left")

# Template Download
download_frame = tk.Frame(control_panel_frame)
download_frame.pack(pady=5)
tk.Button(download_frame, text="Template Download", width=15, height=2, command=cmd_download_template).pack(side="left", padx=5)
tk.Button(download_frame, text="Select File", width=10, command=select_download_file).pack(side="left", padx=5)
download_file_label = tk.Label(download_frame, text="No file selected.", width=20, anchor="w")
download_file_label.pack(side="left")


# Communication Log
main_commands_frame = tk.LabelFrame(right_frame, text="Communication Log", padx=10, pady=10)
main_commands_frame.pack(fill="both", expand=True, pady=10)

tk.Label(main_commands_frame, text="Result:").pack()
result_text = tk.StringVar()
result_label = tk.Label(main_commands_frame, textvariable=result_text, fg="blue", font=("Arial", 12, "bold"))
result_label.pack(pady=10)
result_text.set("Ready. Looking for devices...")

# Start UDP discovery
discovery_thread = threading.Thread(target=discover_devices, daemon=True)
discovery_thread.start()


# Start asyncio event loop
def start_loop():
    global loop
    asyncio.set_event_loop(loop)
    loop.run_forever()


asyncio_thread = threading.Thread(target=start_loop, daemon=True)
asyncio_thread.start()


def on_closing():
    global app_running
    app_running = False
    loop.call_soon_threadsafe(loop.stop)
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_closing)
root.mainloop()