import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import socket
import threading
import asyncio
import time
import re
import os
from datetime import datetime

# Configuration
STATUS_REPORT_PORT = 5002  # NEW: PC acts as a server on this port for device status reports
TCP_PORT = 5000            # Device is a server on this port for commands/logs
TEMPLATES_FOLDER = "templates"  # Define the templates folder name

# Global variables for async operations and device management
# device_list structure: {ip: {'mac': mac, 'battery': percentage, 'tree_id': tree_id}}
device_list = {}
current_client = None  # IP of the device currently in MANAGE mode (set ONLY by cmd_manage)
selected_device_ip = None  # IP of the device currently highlighted in the list (set by select_device)
loop = asyncio.new_event_loop()
app_running = True
download_file_path = None
upload_file_path = None

# <<< GUI & LOG SETUP >>>
continuous_log_text = None
continuous_listener_thread = None
# Widgets defined later in GUI setup
device_listbox = None
manage_button = None
mac_address_label = None
result_text = None
control_panel_command_buttons = []
manage_mode_active = False
enroll_id_entry = None
delete_id_entry = None
upload_id_entry = None
download_file_label = None
root = None


def gui_log_continuous_message(message, color="black"):
    """Safely updates the continuous log area from a background thread."""
    global continuous_log_text, root

    def update_log():
        if continuous_log_text:
            try:
                # Add timestamp for clarity in continuous log
                full_message = f"{datetime.now().strftime('[%H:%M:%S]')} {message}"
                continuous_log_text.config(state=tk.NORMAL)
                continuous_log_text.insert(tk.END, full_message + "\n", color)
                continuous_log_text.see(tk.END)
                continuous_log_text.config(state=tk.DISABLED)
            except Exception:
                pass # Ignore if widget is destroyed

    if root:
        root.after(0, update_log)

# =================================================================================
# NEW: TCP STATUS LISTENER (REPLACES UDP DISCOVERY)
# =================================================================================

def update_device_list_and_gui(ip, mac, battery):
    """Updates the global device list and the Tkinter Treeview."""
    global device_list, device_listbox
    
    mac = mac.strip().upper()
    try:
        battery = int(battery.strip())
    except ValueError:
        battery = 0

    if device_listbox: # Ensure device_listbox widget exists
        if ip not in device_list:
            # New device discovered
            tree_id = device_listbox.insert('', 'end', ip, text=f"Fingerprint Sensor", 
                                            values=(ip, mac, f"{battery}%"))
            device_list[ip] = {'mac': mac, 'battery': battery, 'tree_id': tree_id}
            gui_log_continuous_message(f"DEVICE DISCOVERED (TCP): {ip} ({mac}). Battery: {battery}%", 'green')
        else:
            # Existing device, update status
            tree_id = device_list[ip]['tree_id']
            # Only update if the battery has changed to minimize GUI updates
            if device_list[ip]['battery'] != battery:
                device_listbox.item(tree_id, values=(ip, mac, f"{battery}%"))
                device_list[ip]['battery'] = battery
                gui_log_continuous_message(f"STATUS UPDATE: {ip} - Battery: {battery}%", 'blue')


def tcp_status_listener():
    """
    Runs in a dedicated thread as a TCP server to listen for status reports 
    (discovery simulation) from devices on STATUS_REPORT_PORT (5002).
    """
    global app_running
    try:
        listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Use '0.0.0.0' to listen on all interfaces
        listener_socket.bind(('0.0.0.0', STATUS_REPORT_PORT)) 
        listener_socket.listen(5)
        gui_log_continuous_message(f"Status Listener (TCP) started on port {STATUS_REPORT_PORT}", 'purple')

        while app_running:
            try:
                listener_socket.settimeout(0.5) # Non-blocking check
                conn, addr = listener_socket.accept()
                
                # Receive data until EOF or newline (robust read)
                data_parts = []
                while True:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data_parts.append(chunk)
                    if chunk.endswith(b'\n'):
                        break
                
                data = b''.join(data_parts).decode('utf-8', errors='ignore').strip()

                # Check 1: Handle Status Reports (STATUS|IP|MAC|Battery)
                if data.startswith("STATUS|"):
                    parts = data.split('|')
                    # Check for the 4 expected parts: STATUS, IP, MAC, Battery
                    if len(parts) == 4:
                        _type, ip, mac, battery = parts
                        update_device_list_and_gui(ip, mac, battery) 
                
                # Check 2: Handle Continuous Search Results (CONTINUOUS_SUCCESS:ID or CONTINUOUS_ERROR:...)
                elif data.startswith("CONTINUOUS_"):
                    # Log the continuous result to the GUI log
                    source_ip = addr[0]
                    gui_log_continuous_message(f"[{source_ip}] {data}", 'blue')
                    # Provide the acknowledgement (ACK) to unblock the device
                    ack_message = "ACK:\n"
                    # NOTE: Assuming 'client' is the active socket object to send data back
                    conn.sendall(ack_message.encode('utf-8'))


                elif data == "TIME_REQUEST":
                    # Get the current Unix timestamp (seconds since epoch)
                    current_unix_time = int(time.time())
                    response = f"TIME_RESPONSE:{current_unix_time}\n"

                    print(f"[TIME SYNC] Responding with: {response.strip()}")
                    conn.sendall(response.encode('utf-8'))
                

                # Optional: Log any unexpected messages for debugging
                elif data:
                    gui_log_continuous_message(f"Received unexpected TCP message: {data}", 'orange')
                # --- END CRITICAL FIX ---
                
                # ✅ FIX: Move connection close to the end of the request handling
                conn.close()
                        
            except socket.timeout:
                continue
            except Exception as e:
                # Handle connection-specific or processing errors
                gui_log_continuous_message(f"Status Listener Connection Error: {e}", 'red')
                
    except Exception as e:
        # Handle server-startup errors
        gui_log_continuous_message(f"FATAL: Failed to start Status Listener: {e}", 'red')
    finally:
        try:
            listener_socket.close()
        except:
            pass
            

# <<< END NEW TCP STATUS LISTENER >>>
# *** Note: The old discover_devices() (UDP) function is now REMOVED ***

# <<< CONTINUOUS LISTENER SETUP (Unchanged, relies on existing TCP_PORT 5000) >>>

def continuous_listener():
    """A single thread to listen for continuous updates from all unmanaged devices."""
    global app_running, device_list, current_client, loop

    local_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(local_loop)

    connections = {}  # {ip: (reader, writer)}

    async def manage_connections():
        nonlocal connections

        while app_running:
            # 1. Check for new devices and close connections for selected device
            current_ips = set(device_list.keys())

            # Identify devices that need a connection
            for ip in current_ips:
                # If device is not selected AND we don't have a connection
                if ip != current_client and ip not in connections:
                    try:
                        reader, writer = await asyncio.wait_for(
                            asyncio.open_connection(ip, TCP_PORT), timeout=1
                        )
                        connections[ip] = (reader, writer)
                        gui_log_continuous_message(f"[{ip}] Connected for Default Mode logs.", "green")
                    except Exception:
                        continue  # Failed to connect, try again later

            # 2. Check for connections that need to be closed (device now selected for Manage Mode)
            closed_ips = []
            for ip in list(connections.keys()):
                if ip == current_client:
                    closed_ips.append(ip)
                    gui_log_continuous_message(f"[{ip}] Disconnected (Selected for Manage Mode).", "gray")

            # 3. Read messages from existing, unmanaged connections
            for ip, (reader, writer) in connections.items():
                if ip == current_client:
                    continue  # Skip if it was just selected

                try:
                    line = await asyncio.wait_for(reader.readline(),
                                                  timeout=0.1)

                    if line:
                        response = line.decode('utf-8').strip()
                        if response.startswith("CONTINUOUS"):
                            # NOTE: This block is for continuous results sent on PORT 5000 
                            # (the command port), typically by older code versions.
                            # Devices are now sending these results on PORT 5002,
                            # which is handled by tcp_status_listener().
                            gui_log_continuous_message(f"[{ip}] (Legacy 5000 Log) {response}", "blue")
                    elif line == b'':
                        # Connection closed by server
                        closed_ips.append(ip)
                        gui_log_continuous_message(f"[{ip}] Disconnected (Server closed). Retrying...", "orange")

                except asyncio.TimeoutError:
                    continue
                except ConnectionResetError:
                    closed_ips.append(ip)
                    gui_log_continuous_message(f"[{ip}] Connection Reset. Retrying...", "red")
                except Exception:
                    closed_ips.append(ip)

            # 4. Clean up closed connections
            for ip in closed_ips:
                if ip in connections:
                    reader, writer = connections.pop(ip)
                    if not writer.is_closing():
                        writer.close()

            await asyncio.sleep(0.5)

    local_loop.run_until_complete(manage_connections())


# <<< END CONTINUOUS LISTENER SETUP >>>

def send_command_to_device(command, data=None):
    """Sends a command to the currently selected device and handles response."""
    global current_client, result_text, loop

    if not current_client:
        result_text.set("ERROR: No device selected and in Manage Mode.")
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

            all_responses = []

            while True:
                line = asyncio.run_coroutine_threadsafe(
                    reader.readline(),
                    loop=loop
                ).result(timeout=5)

                if not line:
                    break

                response = line.decode('utf-8').strip()
                all_responses.append(response)

                # Check for termination keywords
                if response.startswith("SUCCESS") or response.startswith("ERROR"):
                    break

            final_output = "\n".join(all_responses)
            if final_output:
                result_text.set(final_output)
            else:
                result_text.set(f"Command '{command}' sent. No response received.")

            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)

        except ConnectionRefusedError:
            result_text.set("ERROR: Connection refused. Device may be offline or unreachable.")
        except asyncio.TimeoutError:
            result_text.set(f"Communication ERROR: Timeout while waiting for response from {current_client}.")
        except Exception as e:
            result_text.set(f"Communication ERROR: {e}")

    threading.Thread(target=communicate).start()


def cmd_enroll_and_upload():
    """Sends ENROLL command, then automatically triggers a template upload on success."""
    global current_client, enroll_id_entry, result_text, loop
    if not current_client:
        result_text.set("ERROR: No device in Manage Mode.")
        return

    def enroll_and_upload():
        try:
            model_id = int(enroll_id_entry.get())
            result_text.set(f"Attempting ENROLL for ModelID {model_id}...")

            reader, writer = asyncio.run_coroutine_threadsafe(
                asyncio.open_connection(current_client, TCP_PORT),
                loop=loop
            ).result(timeout=5)

            full_command = f"ENROLL,{model_id}\n"
            writer.write(full_command.encode('utf-8'))
            asyncio.run_coroutine_threadsafe(writer.drain(), loop=loop).result(timeout=5)

            all_responses = []
            is_success = False

            while True:
                line = asyncio.run_coroutine_threadsafe(
                    reader.readline(),
                    loop=loop
                ).result(timeout=60)

                if not line:
                    break

                response = line.decode('utf-8').strip()
                all_responses.append(response)

                if response.startswith("SUCCESS"):
                    is_success = True
                    break
                elif response.startswith("ERROR"):
                    break

            final_output = "\n".join(all_responses)
            result_text.set(final_output)

            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)

            if is_success:
                print(f"Enrollment successful. Initiating template upload for ModelID {model_id}...")
                time.sleep(1)
                cmd_upload_template(model_id)

        except asyncio.TimeoutError:
            result_text.set(f"Communication ERROR: Timeout while waiting for enrollment response.")
        except Exception as e:
            result_text.set(f"Communication ERROR: {e}")

    threading.Thread(target=enroll_and_upload).start()


def cmd_search():
    send_command_to_device("SEARCH")


def cmd_listtemplates():
    """Sends LIST command and handles the multi-line response."""
    global current_client, result_text, loop
    if not current_client:
        result_text.set("ERROR: No device in Manage Mode.")
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

            while True:
                line = asyncio.run_coroutine_threadsafe(
                    reader.readline(),
                    loop=loop
                ).result(timeout=5)

                if not line:
                    break
                response = line.decode('utf-8').strip()
                all_responses.append(response)

                if response.startswith("OK: List templates command complete.") or response.startswith("ERROR"):
                    break

            if all_responses:
                final_output = "\n".join(all_responses)
                result_text.set(final_output)
            else:
                result_text.set("ERROR: No response received from device.")

            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)

        except asyncio.TimeoutError:
            result_text.set(f"Communication ERROR: Timeout while waiting for list response.")
        except Exception as e:
            result_text.set(f"Communication ERROR: {e}")

    threading.Thread(target=communicate).start()


def cmd_empty_device():
    """Initiates device empty by issuing a confirmation dialog and sending EMPTY command."""
    global current_client, result_text

    if not current_client:
        result_text.set("ERROR: No device in Manage Mode.")
        return

    if not messagebox.askyesno(
            "Confirm Device Empty",
            "⚠️ WARNING: This action will erase the entire device flash memory. Are you sure?"
    ):
        result_text.set("Device empty operation cancelled by user.")
        return

    send_command_to_device("EMPTY")


def cmd_deletechar():
    global result_text, delete_id_entry
    try:
        model_id = int(delete_id_entry.get())
        send_command_to_device("DELETE", model_id)
    except ValueError:
        result_text.set("Invalid ModelID. Please enter a number.")
    except TypeError:
        result_text.set("ERROR: No device in Manage Mode.")


def cmd_upload_template(model_id=None):
    """Sends UPLOAD_TEMPLATE command and saves the received file."""
    global current_client, result_text, upload_id_entry, loop

    if not current_client:
        result_text.set("ERROR: No device in Manage Mode.")
        return

    if model_id is None:
        try:
            model_id = int(upload_id_entry.get())
        except ValueError:
            result_text.set("Invalid ModelID. Please enter a number.")
            return

    def upload():
        try:
            if not os.path.exists(TEMPLATES_FOLDER):
                os.makedirs(TEMPLATES_FOLDER)

            filename = os.path.join(TEMPLATES_FOLDER, f"template_{model_id}.mb")

            result_text.set(f"Attempting to upload template ID {model_id}...")

            reader, writer = asyncio.run_coroutine_threadsafe(
                asyncio.open_connection(current_client, TCP_PORT),
                loop=loop
            ).result(timeout=5)

            full_command = f"UPLOAD_TEMPLATE,{model_id}\n"
            writer.write(full_command.encode('utf-8'))
            asyncio.run_coroutine_threadsafe(writer.drain(), loop=loop).result(timeout=5)

            ack = asyncio.run_coroutine_threadsafe(reader.readline(), loop=loop).result(timeout=15)
            ack_msg = ack.decode('utf-8').strip()
            result_text.set(ack_msg)
            if not ack_msg.startswith("OK"):
                writer.close()
                asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)
                return

            template_data = b''
            expected_size = 1668
            while len(template_data) < expected_size:
                chunk = asyncio.run_coroutine_threadsafe(reader.read(expected_size - len(template_data)),
                                                         loop=loop).result(timeout=60)
                if not chunk:
                    break
                template_data += chunk

            with open(filename, 'wb') as f:
                f.write(template_data)

            result_text.set(
                f"SUCCESS: Template saved to {filename} ({len(template_data)} bytes received)"
            )

            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)

        except Exception as e:
            result_text.set(f"Communication ERROR: {e}")

    threading.Thread(target=upload).start()


def select_download_file():
    global download_file_path, download_file_label

    filepath = filedialog.askopenfilename(
        defaultextension=".mb",
        filetypes=[("Template files", "*.mb")]
    )
    if filepath:
        download_file_path = filepath
        download_file_label.config(text=f"File selected: {os.path.basename(filepath)}")
    else:
        download_file_path = None
        download_file_label.config(text="No file selected.")


def _download_template_sequence(model_id, file_path):
    """Internal function for synchronous download steps (PC to Device)."""
    global current_client, result_text, loop
    status = False

    try:
        result_text.set(f"Downloading template ID {model_id} from PC to device...")

        reader, writer = asyncio.run_coroutine_threadsafe(
            asyncio.open_connection(current_client, TCP_PORT),
            loop=loop
        ).result(timeout=5)

        full_command = f"DOWNLOAD_TEMPLATE,{model_id}\n"
        writer.write(full_command.encode('utf-8'))
        asyncio.run_coroutine_threadsafe(writer.drain(), loop=loop).result(timeout=5)

        ack = asyncio.run_coroutine_threadsafe(reader.readline(), loop=loop).result(timeout=15)
        ack_msg = ack.decode('utf-8').strip()
        result_text.set(f"Download (ID {model_id}): {ack_msg}")

        if not ack_msg.startswith("OK"):
            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)
            return False

        with open(file_path, 'rb') as f:
            template_data = f.read()

        if len(template_data) != 1668:
            result_text.set(f"ERROR (ID {model_id}): Invalid file size ({len(template_data)} bytes). Skipping.")
            writer.close()
            asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)
            return False

        writer.write(template_data)
        asyncio.run_coroutine_threadsafe(writer.drain(), loop=loop).result(timeout=5)

        all_responses = []
        while True:
            final_response = asyncio.run_coroutine_threadsafe(reader.readline(), loop=loop).result(timeout=15)
            if not final_response:
                break
            final_msg = final_response.decode('utf-8').strip()
            all_responses.append(final_msg)

            if final_msg.startswith("SUCCESS") or final_msg.startswith("ERROR"):
                break

        final_output = "\n".join(all_responses)

        if final_output.startswith("SUCCESS"):
            result_text.set(f"SUCCESS (ID {model_id}): Template successfully downloaded to device.")
            status = True
        else:
            result_text.set(f"ERROR (ID {model_id}): {final_output}")
            status = False

        writer.close()
        asyncio.run_coroutine_threadsafe(writer.wait_closed(), loop=loop).result(timeout=5)

    except asyncio.TimeoutError:
        result_text.set(f"Communication ERROR: Timeout during template download (ID {model_id}).")
        status = False
    except Exception as e:
        result_text.set(f"Communication ERROR during sync (ID {model_id}): {e}")
        status = False

    return status


def cmd_download_template():
    """Sends DOWNLOAD_TEMPLATE command for a single selected file (PC to Device)."""
    global current_client, download_file_path, result_text, upload_id_entry

    if not current_client:
        result_text.set("ERROR: No device in Manage Mode.")
        return

    if not download_file_path:
        result_text.set("ERROR: No template file selected.")
        return

    match = re.search(r"template_(\d+)\.mb", download_file_path)
    if not match:
        try:
            model_id = int(upload_id_entry.get())
            result_text.set("Warning: Filename format not recognized. Using ModelID from input box.")
        except ValueError:
            result_text.set(
                "ERROR: Save filename must contain ID (e.g., 'template_[id].mb') or ModelID box must have a number.")
            return
    else:
        model_id = int(match.group(1))

    threading.Thread(target=lambda: _download_template_sequence(model_id, download_file_path)).start()


def cmd_sync_device():
    """Initiates device sync by downloading all templates from the local folder."""
    global current_client, result_text

    if not current_client:
        result_text.set("ERROR: No device in Manage Mode.")
        return

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

            if _download_template_sequence(model_id, file_path):
                success_count += 1

            time.sleep(0.5)

        result_text.set(f"SYNC COMPLETE: {success_count} of {total_count} templates successfully downloaded to device.")

    threading.Thread(target=sync_templates).start()


# --- START: MANAGE BUTTON IMPLEMENTATION (TOGGLE) ---

def cmd_manage():
    """Toggles Manage Mode (ON/OFF) and sends the command to the device."""
    global current_client, manage_mode_active, selected_device_ip, manage_button, mac_address_label, device_listbox, result_text

    if not selected_device_ip and not manage_mode_active:
        result_text.set("ERROR: Select a device from the list first.")
        return

    if not manage_mode_active:
        # --- TURN ON MANAGE MODE (only occurs when button is pressed and mode is OFF) ---
        try:
            # 1. PROMOTE THE SELECTED IP TO THE ACTIVE CLIENT
            current_client = selected_device_ip

            # Get MAC address for display
            selected_item = device_listbox.selection()[0]
            # Use the IP key to get the MAC from the global device_list, as tree index might be fragile
            mac_address = device_list[current_client]['mac']

            # 2. Send MANAGE command
            send_command_to_device("MANAGE")

            # 3. Update GUI state
            manage_button.config(bg="red", text="Manage (ON)")
            mac_address_label.config(text=f"MAC: {mac_address}")
            set_command_buttons_state(tk.NORMAL)
            manage_mode_active = True
            result_text.set(f"Device {current_client} set to Manage Mode. Control buttons ENABLED.")

        except IndexError:
            # Should not happen, but clean up if it does
            result_text.set("ERROR: Select a device from the list first.")
            current_client = None
            return

    else:
        # --- TURN OFF MANAGE MODE ---
        # 1. Send NORMAL command
        send_command_to_device("NORMAL")

        # 2. Reset the GUI state
        manage_button.config(bg="SystemButtonFace", text="Manage")
        mac_address_label.config(text="")
        set_command_buttons_state(tk.DISABLED)

        # 3. CRITICAL: Release control and clear flags
        manage_mode_active = False

        ip_to_release = current_client
        current_client = None  # Release the client for the continuous listener
        selected_device_ip = None  # Clear the selected target

        # Clear selection visually
        try:
            device_listbox.selection_remove(device_listbox.selection())
        except Exception:
            pass

        result_text.set(f"Device {ip_to_release} set to Normal Mode. Control buttons DISABLED. Select a new target.")


def set_command_buttons_state(state):
    """Enables or disables all command panel buttons."""
    global control_panel_command_buttons
    for button in control_panel_command_buttons:
        button.config(state=state)


def select_device(event):
    """
    Handles selection of a device from the Treeview.
    """
    global current_client, manage_mode_active, selected_device_ip, device_listbox, result_text, manage_button, mac_address_label

    selection = device_listbox.selection()
    if selection:
        # The key (IP) is stored as the first value in the Treeview item
        selected_ip = device_listbox.item(selection[0], 'values')[0]

        # 1. Store the selected IP in the temporary variable.
        selected_device_ip = selected_ip

        # 2. Reset the GUI state to NOT in Manage Mode
        manage_button.config(bg="SystemButtonFace", text="Manage")
        mac_address_label.config(text="")
        set_command_buttons_state(tk.DISABLED)
        manage_mode_active = False

        # Look up the device info in the dictionary
        info = device_list.get(selected_device_ip, {})
        battery_status = info.get('battery', 'N/A')

        result_text.set(
            f"Target selected: {selected_device_ip} (Battery: {battery_status}%) - Press 'Manage' to take control.")

    else:
        # Clear all state if selection is removed
        current_client = None
        selected_device_ip = None
        result_text.set("No device selected.")
        set_command_buttons_state(tk.DISABLED)


# GUI Setup
root = tk.Tk()
root.title("FingerPrint Sensor Management Console (TCP-ONLY)")
root.geometry("1000x700")

# Main frames
left_frame = tk.Frame(root, width=450, padx=10, pady=10)
right_frame = tk.Frame(root, padx=10, pady=10)
left_frame.pack(side="left", fill="y", expand=False)
right_frame.pack(side="right", fill="both", expand=True)

# Device Discovery Panel
discovery_panel = tk.LabelFrame(left_frame, text=f"Discovered Devices (Listening on TCP Port {STATUS_REPORT_PORT})", padx=10, pady=10)
discovery_panel.pack(fill="x", pady=10)

# === Treeview ===
columns = ('ip', 'mac', 'battery')
device_listbox = ttk.Treeview(discovery_panel, columns=columns, show='headings', selectmode='browse')

device_listbox.heading('ip', text='IP Address', anchor='center')
device_listbox.heading('mac', text='MAC Address', anchor='center')
device_listbox.heading('battery', text='Battery %', anchor='center')

device_listbox.column('ip', width=110, stretch=tk.NO, anchor='center')
device_listbox.column('mac', width=140, stretch=tk.NO, anchor='center')
device_listbox.column('battery', width=80, stretch=tk.NO, anchor='center')

device_listbox.pack(fill="x", expand=True)
device_listbox.bind('<<TreeviewSelect>>', select_device)
# =======================================================


# Control Panel
control_panel_frame = tk.LabelFrame(left_frame, text="Control Panel (Commands via TCP Port 5000)", padx=10, pady=10)
control_panel_frame.pack(fill="x", pady=10)

# MANAGE BUTTON and Label
manage_frame = tk.Frame(control_panel_frame)
manage_frame.pack(pady=5)
manage_button = tk.Button(manage_frame, text="Manage", width=15, height=2, command=cmd_manage, bg="SystemButtonFace")
manage_button.pack(side="left", padx=5)
mac_address_label = tk.Label(manage_frame, text="", fg="red", font=("Arial", 10, "bold"))
mac_address_label.pack(side="left", padx=5)

# Enroll button and input
enroll_frame = tk.Frame(control_panel_frame)
enroll_frame.pack(pady=10)
btn_enroll = tk.Button(enroll_frame, text="Enroll", width=15, height=2, command=cmd_enroll_and_upload)
btn_enroll.pack(side="left", padx=5)
enroll_id_entry = tk.Entry(enroll_frame, width=10)
enroll_id_entry.insert(0, "1")
enroll_id_entry.pack(side="left", padx=5)
tk.Label(enroll_frame, text="ModelID").pack(side="left")
control_panel_command_buttons.append(btn_enroll)

# Other commands
btn_search = tk.Button(control_panel_frame, text="Search", width=15, height=2, command=cmd_search)
btn_search.pack(pady=5)
control_panel_command_buttons.append(btn_search)

btn_list = tk.Button(control_panel_frame, text="List Templates", width=15, height=2, command=cmd_listtemplates)
btn_list.pack(pady=5)
control_panel_command_buttons.append(btn_list)

# Empty Device
btn_empty = tk.Button(control_panel_frame, text="Empty Device", width=15, height=2, command=cmd_empty_device)
btn_empty.pack(pady=15)
control_panel_command_buttons.append(btn_empty)

# Sync Device Button
btn_sync = tk.Button(control_panel_frame, text="Sync Device", width=15, height=2, command=cmd_sync_device)
btn_sync.pack(pady=15)
control_panel_command_buttons.append(btn_sync)

# DeleteChar
delete_frame = tk.Frame(control_panel_frame)
delete_frame.pack(pady=5)
btn_delete = tk.Button(delete_frame, text="Delete", width=15, height=2, command=cmd_deletechar)
btn_delete.pack(side="left", padx=5)
delete_id_entry = tk.Entry(delete_frame, width=10)
delete_id_entry.insert(0, "1")
delete_id_entry.pack(side="left", padx=5)
tk.Label(delete_frame, text="ModelID").pack(side="left")
control_panel_command_buttons.append(btn_delete)

# Template Upload (PC receives from device)
upload_frame = tk.Frame(control_panel_frame)
upload_frame.pack(pady=5)
btn_template_upload = tk.Button(upload_frame, text="Template Upload", width=15, height=2,
                                command=lambda: cmd_upload_template())
btn_template_upload.pack(side="left", padx=5)
upload_id_entry = tk.Entry(upload_frame, width=10)
upload_id_entry.insert(0, "1")
upload_id_entry.pack(side="left", padx=5)
tk.Label(upload_frame, text="ModelID").pack(side="left")
control_panel_command_buttons.append(btn_template_upload)

# Template Download (PC sends to device)
download_frame = tk.Frame(control_panel_frame)
download_frame.pack(pady=5)
btn_template_download = tk.Button(download_frame, text="Template Download", width=15, height=2,
                                  command=cmd_download_template)
btn_template_download.pack(side="left", padx=5)
control_panel_command_buttons.append(btn_template_download)

btn_select_file = tk.Button(download_frame, text="Select File", width=10, command=select_download_file)
btn_select_file.pack(side="left", padx=5)
control_panel_command_buttons.append(btn_select_file)

download_file_label = tk.Label(download_frame, text="No file selected.", width=20, anchor="w")
download_file_label.pack(side="left")

# Initially disable all command buttons
set_command_buttons_state(tk.DISABLED)

# Communication Log
main_commands_frame = tk.LabelFrame(right_frame, text="Communication Log", padx=10, pady=10)
main_commands_frame.pack(fill="x", pady=10)

tk.Label(main_commands_frame, text="Result:").pack()
result_text = tk.StringVar()
result_label = tk.Label(main_commands_frame, textvariable=result_text, fg="blue", font=("Arial", 12, "bold"))
result_label.pack(pady=10)
result_text.set("Ready. Starting TCP Status Listener...")

# New Frame for Continuous Search Log
cont_log_frame = tk.LabelFrame(right_frame, text="Default Mode Logs", padx=10, pady=10)
cont_log_frame.pack(fill="both", expand=True, pady=10)

# Use a Text widget for scrolling and multi-line logging
continuous_log_text = tk.Text(cont_log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED, bg="#F0F0F0")
continuous_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

# Scrollbar
scrollbar = tk.Scrollbar(cont_log_frame, command=continuous_log_text.yview)
scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
continuous_log_text.config(yscrollcommand=scrollbar.set)

# Define tags for colors
continuous_log_text.tag_config('red', foreground='red')
continuous_log_text.tag_config('blue', foreground='blue')
continuous_log_text.tag_config('green', foreground='green')
continuous_log_text.tag_config('purple', foreground='purple')
continuous_log_text.tag_config('orange', foreground='orange')
continuous_log_text.tag_config('gray', foreground='gray')

# 🚨 START NEW TCP STATUS LISTENER (Replaces UDP discovery)
status_thread = threading.Thread(target=tcp_status_listener, daemon=True)
status_thread.start()

# Start asyncio event loop
def start_loop():
    global loop
    asyncio.set_event_loop(loop)
    loop.run_forever()

asyncio_thread = threading.Thread(target=start_loop, daemon=True)
asyncio_thread.start()

# Start continuous listener thread (relies on asyncio loop)
continuous_listener_thread = threading.Thread(target=continuous_listener, daemon=True)
continuous_listener_thread.start()


def on_closing():
    global app_running
    app_running = False
    loop.call_soon_threadsafe(loop.stop)
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_closing)
root.mainloop()