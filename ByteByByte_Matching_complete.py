from flask import Flask, request, jsonify
import os
import sqlite3
import struct

app = Flask(__name__)

DATABASE = 'fingerprint_data.db'

# Initialize the SQLite database
def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fingerprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            template BLOB NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

# Global variable to hold the mode (Enroll or Detect)
SERVER_MODE = None

# Extract fingerprint data from raw data (with trailing 0x00 removal)
def extract_fingerprint_data(raw_data):
    payloads = []
    offset = 0

    while offset < len(raw_data):
        # Check for the packet header
        if raw_data[offset:offset + 6] == b'\xef\x01\xff\xff\xff\xff':
            # Extract the package identifier and length
            try:
                package_id = raw_data[offset + 6]
                length = struct.unpack(">H", raw_data[offset + 7:offset + 9])[0]
            except struct.error:
                break  # Exit if the packet structure is incomplete

            # Debug: Print details about the packet
            print(f"Packet found: Header at offset {offset}, Package ID: {package_id}, Length: {length}")

            # Extract the payload (excluding header, length, and checksum)
            start = offset + 9  # Start of payload
            end = offset + 9 + length - 2  # Exclude 2-byte checksum
            payload = raw_data[start:end]

            # Remove trailing 0x00 padding bytes (only from the end of the payload)
            payload = payload.rstrip(b'\x00')

            # Append valid payload
            payloads.append(payload)

            # Move offset to the next packet
            offset = offset + 9 + length
        else:
            # Move to the next byte if no header is found
            offset += 1

    # Join all extracted payloads into one fingerprint template
    fingerprint_template = b''.join(payloads)
    print(f"Extracted fingerprint template ({len(fingerprint_template)} bytes): {fingerprint_template.hex()}")
    return fingerprint_template

# Calculate byte-by-byte similarity

def calculate_similarity(payload1, payload2):
    # Determine the shorter and longer payloads
    shorter, longer = (payload1, payload2) if len(payload1) <= len(payload2) else (payload2, payload1)
    # Count matching bytes
    matches = sum(1 for i in range(len(shorter)) if shorter[i] == longer[i])
    # Calculate match percentage
    return (matches / len(shorter)) * 100

@app.route('/get_mode', methods=['GET'])
def get_mode():
    return jsonify({'mode': SERVER_MODE})

@app.route('/upload', methods=['POST'])
def upload_fingerprint():
    try:
        if not request.data:
            return jsonify({'status': 'fail', 'message': 'No data received'}), 400

        # Extract the full fingerprint template
        fingerprint_template = extract_fingerprint_data(request.data)
        print(f"Template to save (size: {len(fingerprint_template)} bytes): {fingerprint_template.hex()}")  # Debugging

        # Save fingerprint template to database as BLOB
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO fingerprints (username, template) VALUES (?, ?)', ("", sqlite3.Binary(fingerprint_template)))
        conn.commit()

        # Prompt for username input
        username = input("Enter the person's username: ").strip()
        cursor.execute('UPDATE fingerprints SET username = ? WHERE id = (SELECT MAX(id) FROM fingerprints)', (username,))
        conn.commit()
        conn.close()

        print(f"Fingerprint saved for {username}, template size: {len(fingerprint_template)} bytes")

        return jsonify({'status': 'success', 'message': f'Fingerprint saved for {username}.'}), 200

    except Exception as e:
        return jsonify({'status': 'fail', 'message': str(e)}), 500

@app.route('/detect', methods=['POST'])
def detect_fingerprint():
    try:
        if not request.data:
            return jsonify({'status': 'fail', 'message': 'No data received'}), 400

        # Extract fingerprint template from request
        fingerprint_template = extract_fingerprint_data(request.data)

        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('SELECT username, template FROM fingerprints')
        rows = cursor.fetchall()
        conn.close()

        # Initialize variables for finding the best match
        best_match = None
        best_similarity = 0  # We want to maximize similarity percentage

        for username, stored_template in rows:
            # Compare the input template with the stored template
            similarity = calculate_similarity(fingerprint_template, stored_template)

            # Debugging output
            print(f"Similarity with {username}: {similarity:.2f}%")

            # Update the best match if the current one is better
            if similarity > best_similarity:
                best_match = username
                best_similarity = similarity

        if best_match and best_similarity > 80:  # Set a threshold for a valid match
            return jsonify({'status': 'success', 'message': f'Match found for {best_match} (Similarity: {best_similarity:.2f}%)'}), 200

        return jsonify({'status': 'fail', 'message': 'No match found'}), 404

    except Exception as e:
        return jsonify({'status': 'fail', 'message': str(e)}), 500

if __name__ == '__main__':
    init_db()
    while True:
        SERVER_MODE = input("Select server mode (enroll/detect): ").strip().lower()
        if SERVER_MODE in ['enroll', 'detect']:
            break
        print("Invalid input. Please enter 'enroll' or 'detect'.")

    print(f"Server started in {SERVER_MODE} mode.")
    app.run(host='0.0.0.0', port=5000)
