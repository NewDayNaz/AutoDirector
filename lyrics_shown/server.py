import os
import json
import time
import base64
import obsws_python as obs
from PIL import Image
from io import BytesIO
import cv2
import numpy as np
from collections import deque
from flask import Flask, jsonify

config = None
with open("../config.json", "r") as file:
    config = json.load(file)

# Flask app setup
app = Flask(__name__)

# Connect to OBS WebSocket
cl = obs.ReqClient(host=config["obs"]["websocket"]["host"], port=config["obs"]["websocket"]["port"], password=config["obs"]["websocket"]["password"], timeout=3)

# Function to decode and convert Base64 image to OpenCV format (numpy array)
def load_image_from_base64(base64_data):
    img_data = base64.b64decode(base64_data)
    img = Image.open(BytesIO(img_data))
    img_cv = np.array(img)
    img_cv = cv2.cvtColor(img_cv, cv2.COLOR_RGB2BGR)  # Convert PIL to OpenCV format
    return img_cv

# Function to compare images using OpenCV (motion detection) with multiplier for non-grayscale pixels
def compare_images(img1, img2, threshold=100000, color_multiplier=30):
    # Convert images to grayscale
    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
    
    # Compute absolute difference between images
    diff = cv2.absdiff(gray1, gray2)

    # Threshold the difference image to detect changes
    _, thresh = cv2.threshold(diff, 5, 255, cv2.THRESH_BINARY)  # Lowered threshold for more sensitivity

    # Identify non-grayscale pixels in the RGB difference
    diff_rgb = cv2.absdiff(img1, img2)

    # Create a mask where non-grayscale pixels have a non-zero difference
    non_grayscale_mask = np.any(diff_rgb != 0, axis=-1)  # Check if any of the RGB channels have a difference

    # Apply the color multiplier to non-grayscale pixels
    thresh[non_grayscale_mask] *= color_multiplier

    # Count non-zero pixels (which indicates motion/scene change)
    non_zero_count = np.count_nonzero(thresh)
    return non_zero_count > threshold

# Initialize variables
image_buffer = deque(maxlen=5)  # Rolling buffer to store last 5 images
lyrics_shown = True
projector_scene = config["obs"]["scenes"]["projector_scene"]
print(f"Processing scene: {projector_scene}")

# Main loop for capturing screenshots and detecting motion
def capture_loop():
    global lyrics_shown
    while True:
        # Get the screenshot (Base64 encoded)
        response = cl.get_source_screenshot(name=projector_scene, img_format="jpg", width=1280, height=720, quality=100)
        base64_image = response.image_data.split(',')[1]
        img = load_image_from_base64(base64_image)

        # Add image to buffer (rolling buffer automatically discards the oldest image if maxlen is reached)
        image_buffer.append(img)

        # Once the buffer has 5 images, compare the first and last
        if len(image_buffer) == 5:
            # Compare the first image (oldest) with the last image (most recent)
            motion_detected = compare_images(image_buffer[0], image_buffer[-1])

            # If motion/scene change is above the threshold, set lyrics_shown to False
            if motion_detected:
                lyrics_shown = False
                # print("Motion detected, lyrics_shown set to False.")
            else:
                lyrics_shown = True
                # print("No significant motion detected, lyrics_shown set to True.")

        # Delay between captures (adjust as necessary)
        time.sleep(1)  # Sleep for 1 second before capturing the next image

# API endpoint to get the current value of lyrics_shown
@app.route('/data', methods=['GET'])
def get_lyrics_shown():
    return jsonify({"lyrics_shown": lyrics_shown})

# Start the Flask app in a separate thread and the capture loop in the main thread
if __name__ == "__main__":
    from threading import Thread

    # Start the Flask server
    flask_thread = Thread(target=app.run, kwargs={"host": "0.0.0.0", "port": 4932, "debug": False})
    flask_thread.daemon = True
    flask_thread.start()

    # Start the capture loop
    capture_loop()
