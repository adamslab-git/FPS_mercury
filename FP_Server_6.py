import cv2
import numpy as np
import os
import tkinter as tk
from tkinter import messagebox
from skimage.morphology import skeletonize
from skimage import util
from flask import Flask, request, Response
import threading
from datetime import datetime
from PIL import Image, ImageTk
import struct
import io
import json

# --- Flask Server Setup ---
app = Flask(__name__)

# --- BMP Image Parameters for the final output ---
# The total size of the data stream from the R502-A sensor is 18432 bytes
EXPECTED_DATA_SIZE = 18432
IMAGE_WIDTH = 96
IMAGE_HEIGHT = 96
# The raw pixel data size for a 96x96 grayscale image is 9216 bytes
PIXEL_DATA_SIZE = IMAGE_WIDTH * IMAGE_HEIGHT

# --- BMP Header and Palette Generation ---
def create_bmp_header_and_palette(width, height):
    """
    Generates a complete 8-bit grayscale BMP header with a color palette.
    """
    # File Header (14 bytes)
    file_header = b'BM'  # BMP signature
    file_size = 54 + 1024 + (width * height)  # Header (54) + Palette (1024) + Pixel Data
    file_header += struct.pack('<I', file_size)
    file_header += b'\x00\x00'  # Reserved
    file_header += b'\x00\x00'  # Reserved
    file_header += struct.pack('<I', 54 + 1024)  # Pixel data offset

    # Info Header (40 bytes)
    info_header = struct.pack('<I', 40)  # Info header size
    info_header += struct.pack('<i', width)  # Image width
    info_header += struct.pack('<i', height)  # Image height
    info_header += struct.pack('<H', 1)  # Planes
    info_header += struct.pack('<H', 8)  # Bits per pixel (8-bit grayscale)
    info_header += struct.pack('<I', 0)  # Compression method (0 = none)
    info_header += struct.pack('<I', width * height)  # Image size
    info_header += struct.pack('<i', 2835)  # Horizontal resolution (72 dpi)
    info_header += struct.pack('<i', 2835)  # Vertical resolution (72 dpi)
    info_header += struct.pack('<I', 256)  # Number of colors in the palette
    info_header += struct.pack('<I', 256)  # Important colors

    # Grayscale Palette (1024 bytes)
    palette = b''
    for i in range(256):
        palette += struct.pack('<BBBx', i, i, i)  # R, G, B, and padding byte

    return file_header + info_header + palette

# --- GUI Setup ---
class ImageEnhancerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Fingerprint Server-For image capture and enhancing")
        self.root.geometry("800x800")

        self.bmp_folder = os.path.join(os.getcwd(), 'bmp_files')
        if not os.path.exists(self.bmp_folder):
            os.makedirs(self.bmp_folder)

        self.output_folder = os.path.join(os.getcwd(), 'output_files')
        if not os.path.exists(self.output_folder):
            os.makedirs(self.output_folder)

        # Main frame
        main_frame = tk.Frame(root)
        main_frame.pack(padx=10, pady=10, fill="both", expand=True)

        # Controls and Preview frames
        controls_frame = tk.Frame(main_frame, borderwidth=2, relief="groove")
        controls_frame.pack(side="left", padx=10, pady=10, fill="y")

        # Create a frame for the two previews
        previews_frame = tk.Frame(main_frame, borderwidth=2, relief="groove")
        previews_frame.pack(side="right", padx=10, pady=10, fill="both", expand=True)
        
        # Original Image Preview
        original_preview_frame = tk.Frame(previews_frame)
        original_preview_frame.pack(side="left", padx=5, fill="both", expand=True)
        tk.Label(original_preview_frame, text="Original BMP Preview", font=("Arial", 12, "bold")).pack(pady=5)
        self.original_preview_label = tk.Label(original_preview_frame)
        self.original_preview_label.pack(fill="both", expand=True)

        # Enhanced Image Preview
        enhanced_preview_frame = tk.Frame(previews_frame)
        enhanced_preview_frame.pack(side="right", padx=5, fill="both", expand=True)
        tk.Label(enhanced_preview_frame, text="Enhanced Image Preview", font=("Arial", 12, "bold")).pack(pady=5)
        self.enhanced_preview_label = tk.Label(enhanced_preview_frame)
        self.enhanced_preview_label.pack(fill="both", expand=True)

        # --- Enhancement Parameters ---
        tk.Label(controls_frame, text="Enhancement Parameters", font=("Arial", 12, "bold")).pack(pady=10)

        # Denoising
        frame = tk.Frame(controls_frame)
        frame.pack(fill="x", padx=5, pady=5)
        self.denoise_enabled = tk.BooleanVar(value=True)
        check = tk.Checkbutton(frame, text="Denoising", variable=self.denoise_enabled)
        check.pack(side="left")
        self.denoise_h_value = tk.StringVar(value="10")
        entry = tk.Entry(frame, textvariable=self.denoise_h_value, width=10)
        entry.pack(side="right")
        check.bind('<Button-1>', lambda event, entry=entry, check_var=self.denoise_enabled: self.toggle_entry_state(event, entry, check_var))

        # Contrast (CLAHE)
        frame = tk.Frame(controls_frame)
        frame.pack(fill="x", padx=5, pady=5)
        self.contrast_enabled = tk.BooleanVar(value=True)
        check = tk.Checkbutton(frame, text="Contrast (CLAHE)", variable=self.contrast_enabled)
        check.pack(side="left")
        self.contrast_clip_limit = tk.StringVar(value="2.0")
        entry = tk.Entry(frame, textvariable=self.contrast_clip_limit, width=10)
        entry.pack(side="right")
        check.bind('<Button-1>', lambda event, entry=entry, check_var=self.contrast_enabled: self.toggle_entry_state(event, entry, check_var))
        
        frame = tk.Frame(controls_frame)
        frame.pack(fill="x", padx=5, pady=0)
        tk.Label(frame, text="Tile grid size", width=15, anchor="w").pack(side="left", padx=(25, 0))
        self.contrast_tile_size = tk.StringVar(value="8")
        entry = tk.Entry(frame, textvariable=self.contrast_tile_size, width=10)
        entry.pack(side="right")
        # Ensure the tile size entry is disabled/enabled with the Contrast checkbox
        check.bind('<Button-1>', lambda event, entry=entry, check_var=self.contrast_enabled: self.toggle_entry_state(event, entry, check_var))

        # Binarization
        frame = tk.Frame(controls_frame)
        frame.pack(fill="x", padx=5, pady=5)
        self.binarize_enabled = tk.BooleanVar(value=True)
        check = tk.Checkbutton(frame, text="Binarization", variable=self.binarize_enabled)
        check.pack(side="left")
        self.binarization_threshold = tk.StringVar(value="127")
        entry = tk.Entry(frame, textvariable=self.binarization_threshold, width=10)
        entry.pack(side="right")
        check.bind('<Button-1>', lambda event, entry=entry, check_var=self.binarize_enabled: self.toggle_entry_state(event, entry, check_var))
        
        # --- Skeletonization & Inversion ---
        tk.Label(controls_frame, text="Morphological Operations", font=("Arial", 12, "bold")).pack(pady=10)

        # Invert
        frame = tk.Frame(controls_frame)
        frame.pack(fill="x", padx=5, pady=5)
        self.invert_enabled = tk.BooleanVar(value=True)
        tk.Checkbutton(frame, text="Invert Image (for skeleton)", variable=self.invert_enabled).pack(side="left")

        # Skeletonize
        frame = tk.Frame(controls_frame)
        frame.pack(fill="x", padx=5, pady=5)
        self.skeletonize_enabled = tk.BooleanVar(value=True)
        tk.Checkbutton(frame, text="Skeletonize (Thinning)", variable=self.skeletonize_enabled).pack(side="left")


        # Output File Name
        tk.Label(controls_frame, text="Output File Name", font=("Arial", 12, "bold")).pack(pady=10)
        self.output_filename_var = tk.StringVar(value="enhanced_image")
        tk.Entry(controls_frame, textvariable=self.output_filename_var, width=25).pack(padx=5)

        # Output Path Label
        tk.Label(controls_frame, text="Output Folder:", font=("Arial", 10)).pack(pady=(10, 0))
        self.output_path_label = tk.Label(controls_frame, text=self.output_folder, wraplength=200, justify="center")
        self.output_path_label.pack(padx=5, pady=(0, 10))

        # --- Battery Percentage Display as a Bar Graph ---
        battery_frame = tk.Frame(controls_frame)
        battery_frame.pack(fill="x", padx=5, pady=10)
        tk.Label(battery_frame, text="Battery Level:", font=("Arial", 10, "bold")).pack(side="left")
        
        self.battery_bars = []
        bars_frame = tk.Frame(battery_frame)
        bars_frame.pack(side="right", fill="x", expand=True)
        for i in range(10):
            bar = tk.Label(bars_frame, width=2, relief="sunken", borderwidth=1, bg="grey")
            bar.pack(side="left", padx=1)
            self.battery_bars.append(bar)

        # Status Bar
        status_frame = tk.Frame(root, borderwidth=2, relief="sunken")
        status_frame.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="Waiting for POST data from Arduino...")
        self.status_label = tk.Label(status_frame, textvariable=self.status_var, anchor="w", padx=5)
        self.status_label.pack(fill="x")

    def toggle_entry_state(self, event, entry, check_var):
        # A small delay to allow the check_var to update
        self.root.after(1, lambda: self._toggle_entry_state_after_update(entry, check_var))

    def _toggle_entry_state_after_update(self, entry, check_var):
        # The check_var state will be the opposite of what is shown, as the click hasn't completed yet
        if not check_var.get():
            entry.config(state="disabled")
        else:
            entry.config(state="normal")

    def update_status(self, message):
        self.status_var.set(message)
        self.root.update_idletasks()

    def update_preview(self, img_path, label):
        try:
            pil_image = Image.open(img_path)

            # Define a maximum size for the previews
            max_preview_width = 350
            max_preview_height = 350
            
            img_width, img_height = pil_image.size

            ratio = min(max_preview_width / img_width, max_preview_height / img_height)
            new_size = (int(img_width * ratio), int(img_height * ratio))
            resized_image = pil_image.resize(new_size, Image.LANCZOS)

            tk_image = ImageTk.PhotoImage(resized_image)
            label.config(image=tk_image)
            label.image = tk_image
        except Exception as e:
            self.update_status(f"Error displaying image: {e}")

    def update_battery_bars(self, battery_percent):
        num_bars_to_light = int(battery_percent / 10)
        for i, bar in enumerate(self.battery_bars):
            if i < num_bars_to_light:
                bar.config(bg="green")
            else:
                bar.config(bg="grey")
        self.root.update_idletasks()

# --- Image Enhancement Logic ---
def enhance_image(input_image_path, h_value, clip_limit, tile_size, binarization_threshold, denoise_enabled, contrast_enabled, binarize_enabled, invert_enabled, skeletonize_enabled):
    """
    Performs a series of enhancements and optional thinning on a grayscale fingerprint image.
    """
    # 1. Read the input image in grayscale
    img = cv2.imread(input_image_path, 0)
    if img is None:
        raise ValueError(f"Could not read the image from {input_image_path}")

    current_img = img

    # 2. Apply Denoising with adjustable 'h' value (if enabled)
    if denoise_enabled:
        current_img = cv2.fastNlMeansDenoising(current_img, None, h=h_value, templateWindowSize=7, searchWindowSize=21)

    # 3. Improve Contrast using CLAHE with adjustable parameters (if enabled)
    if contrast_enabled:
        clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(tile_size, tile_size))
        current_img = clahe.apply(current_img)

    # 4. Upscale the image to 192x192
    upscaled_img = cv2.resize(current_img, (192, 192), interpolation=cv2.INTER_CUBIC)

    final_img = upscaled_img

    # 5. Apply Binarization (if enabled)
    if binarize_enabled:
        _, final_img = cv2.threshold(final_img, binarization_threshold, 255, cv2.THRESH_BINARY)
        
        # 6. Apply Inversion and Skeletonization (if enabled)
        if invert_enabled and skeletonize_enabled:
            # Invert the image so ridges are 1s and background is 0s
            inverted_binary_img = util.invert(final_img)
            # Perform thinning
            skeleton = skeletonize(inverted_binary_img)
            # Convert back to a displayable OpenCV image format
            final_img = (skeleton * 255).astype(np.uint8)
        elif invert_enabled:
            # If only invert is enabled, apply it
            final_img = util.invert(final_img)
        elif skeletonize_enabled:
            # If only skeletonize is enabled, it won't work well without binarization/inversion
            # Let's handle this case by simply not applying it and logging a warning
            print("Warning: Skeletonization requires a binary and inverted image. Skipping.")

    return final_img

# --- Flask Route to handle POST request ---
@app.route('/upload', methods=['POST'])
def upload_file():
    global gui
    gui.update_status("Received POST data...")

    try:
        # Read raw binary data from the request body
        image_data = request.data
        if not image_data:
            return Response("No image data received", status=400)

        received_size = len(image_data)
        gui.update_status(f"Received a POST request. Data size: {received_size} bytes.")

        if received_size != EXPECTED_DATA_SIZE:
            gui.update_status(f"Warning: Unexpected data size. Expected {EXPECTED_DATA_SIZE} bytes, but received {received_size} bytes.")

        # Correctly extract the 96x96 pixel data
        processed_pixel_data = bytearray()
        source_row_byte_width = 192
        for i in range(IMAGE_HEIGHT):
            start_index = i * source_row_byte_width
            end_index = start_index + IMAGE_WIDTH
            if end_index <= len(image_data):
                processed_pixel_data.extend(image_data[start_index:end_index])
            else:
                processed_pixel_data.extend(bytearray([0x00] * IMAGE_WIDTH))
        
        # Create the BMP header and palette
        bmp_header = create_bmp_header_and_palette(IMAGE_WIDTH, IMAGE_HEIGHT)

        # Combine the header, palette, and correctly processed pixel data
        bmp_file_data = bmp_header + processed_pixel_data
        
        # Save the received BMP file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        input_filename = f"arduino_image_{timestamp}.bmp"
        input_filepath = os.path.join(gui.bmp_folder, input_filename)

        with open(input_filepath, 'wb') as f:
            f.write(bmp_file_data)
        
        gui.update_status("Received BMP file. Processing...")

        # Read parameters and their enabled status from GUI
        h_value = int(gui.denoise_h_value.get())
        clip_limit = float(gui.contrast_clip_limit.get())
        tile_size = int(gui.contrast_tile_size.get())
        binarization_threshold = int(gui.binarization_threshold.get())
        
        denoise_enabled = gui.denoise_enabled.get()
        contrast_enabled = gui.contrast_enabled.get()
        binarize_enabled = gui.binarize_enabled.get()
        invert_enabled = gui.invert_enabled.get()
        skeletonize_enabled = gui.skeletonize_enabled.get()
        
        # Enhance the image
        enhanced_img = enhance_image(
            input_filepath, 
            h_value, 
            clip_limit, 
            tile_size, 
            binarization_threshold, 
            denoise_enabled, 
            contrast_enabled, 
            binarize_enabled,
            invert_enabled,
            skeletonize_enabled
        )
        
        # Save the enhanced image
        output_base_filename = gui.output_filename_var.get()
        output_filename = f"{output_base_filename}_{timestamp}.png"
        output_filepath = os.path.join(gui.output_folder, output_filename)
        cv2.imwrite(output_filepath, enhanced_img)
        
        gui.update_status(f"Saved enhanced image to: {output_filepath}")
        
        # Update both previews
        gui.update_preview(input_filepath, gui.original_preview_label)
        gui.update_preview(output_filepath, gui.enhanced_preview_label)
        
        return Response("Image processed and saved successfully", status=200)

    except Exception as e:
        gui.update_status(f"Error processing image: {e}")
        return Response(f"Error processing image: {e}", status=500)

# --- NEW Flask Route for Battery Percentage ---
@app.route('/battery', methods=['POST'])
def battery_status():
    global gui
    try:
        data = request.json
        if data and 'battery' in data:
            battery_percent = data['battery']
            gui.update_battery_bars(battery_percent)
            gui.update_status(f"Received battery status: {battery_percent}%")
            return Response("Battery status received", status=200)
        else:
            return Response("Invalid JSON data", status=400)
    except Exception as e:
        gui.update_status(f"Error receiving battery status: {e}")
        return Response(f"Error: {e}", status=500)

# --- Start Flask in a separate thread ---
def run_flask():
    app.run(host='0.0.0.0', port=8080)

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    root = tk.Tk()
    gui = ImageEnhancerGUI(root)
    root.mainloop()