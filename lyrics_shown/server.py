import os
import json
import time
import base64
from PIL import Image
from io import BytesIO
import cv2
import numpy as np
from collections import deque
import asyncio
import websockets

# Load configuration
config = None
with open("../config.json", "r") as file:
    config = json.load(file)

# Function to decode and convert Base64 image to OpenCV format (numpy array)
def load_image_from_base64(base64_data):
    img_data = base64.b64decode(base64_data)
    img = Image.open(BytesIO(img_data))
    img_cv = np.array(img)
    img_cv = cv2.cvtColor(img_cv, cv2.COLOR_RGB2BGR)  # Convert PIL to OpenCV format
    return img_cv

# Function to compare images using OpenCV (motion detection) with multiplier for non-grayscale pixels
def compare_images(img1, img2, threshold=80000, color_multiplier=30):
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
    # print(f"Non-zero count: {non_zero_count} = {non_zero_count > threshold}")
    return non_zero_count > threshold

# Function to check if the majority of the image is grayscale
def is_mostly_grayscale(img, saturation_threshold=40000000):
    # Convert image to HSV
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # Create a desaturated version by setting saturation to 0
    desaturated_hsv = hsv.copy()
    desaturated_hsv[:, :, 1] = 0
    desaturated_img = cv2.cvtColor(desaturated_hsv, cv2.COLOR_HSV2BGR)

    # Create a saturated version by maxing out the saturation
    saturated_hsv = hsv.copy()
    saturated_hsv[:,:,1] = 255
    saturated_img = cv2.cvtColor(saturated_hsv, cv2.COLOR_HSV2BGR)
    
    # Calculate the difference between saturated and desaturated
    diff = cv2.absdiff(saturated_img, desaturated_img)
    
    # Sum up all the color differences
    distance = np.sum(diff)
    
    # print(f"Saturation distance: {distance} = {distance < saturation_threshold}")
    return distance < saturation_threshold

# Initialize variables
image_buffer = deque(maxlen=6)  # Rolling buffer to store last 6 images
lyrics_shown = True

projector_scene = config["obs"]["scenes"]["projector_scene"]
print(f"Target scene: {projector_scene}")

# WebSocket client to update lyrics_shown value
async def update_lyrics_shown(value):
    async with websockets.connect(config["coordinator"]["client"]) as websocket:
        message = json.dumps({"action": "update", "topic": "lyrics_shown", "data": value})
        await websocket.send(message)
        print(f"Updated lyrics_shown to {value} on WebSocket server")

# Main loop for capturing screenshots and detecting motion
async def capture_loop():
    global lyrics_shown
    uri = config["coordinator"]["client"]
    
    await update_lyrics_shown(lyrics_shown)  # Set initial value
    
    async with websockets.connect(uri) as websocket:
        # Subscribe to scene_pool topic
        await websocket.send(json.dumps({"action": "subscribe", "topic": "scene_pool"}))

        while True:
            try:
                message = await websocket.recv()
                data = json.loads(message)
                topic = data.get("topic")
                
                if topic == "scene_pool":
                    scene_pool = data["data"]

                if projector_scene in scene_pool:
                    base64_image = scene_pool[projector_scene]
                    img = load_image_from_base64(base64_image)
                    
                    # Add image to buffer (rolling buffer automatically discards the oldest image if maxlen is reached)
                    image_buffer.append(img)
                    
                    # Once the buffer has 2 or more images, compare each with the previous one
                    if len(image_buffer) >= 2:
                        motion_detected = False
                        grayscale_content = True
                        for i in range(1, len(image_buffer)):
                            if compare_images(image_buffer[i-1], image_buffer[i]):
                                motion_detected = True
                                break

                        if not motion_detected:
                            grayscale_content = is_mostly_grayscale(img)

                        # If motion/scene change is above the threshold, set lyrics_shown to False
                        if motion_detected:
                            if lyrics_shown:  # Only update if the value changes
                                print(f"Lyrics shown: {lyrics_shown} -> False")
                                lyrics_shown = False
                                await update_lyrics_shown(lyrics_shown)
                        else:
                            if not lyrics_shown and grayscale_content:  # Only update if the value changes and it's grayscale
                                print(f"Lyrics shown: {lyrics_shown} -> True")
                                lyrics_shown = True
                                await update_lyrics_shown(lyrics_shown)
                            elif lyrics_shown and not grayscale_content:
                                print(f"Lyrics shown: {lyrics_shown} -> False")
                                lyrics_shown = False
                                await update_lyrics_shown(lyrics_shown)
                                
            except websockets.exceptions.ConnectionClosedError:
                print("Websocket connection closed, attempting to reconnect...")
                break
            except Exception as e:
                print(f"An unexpected error has occurred when receiving messages {e}")
                break

# Main entry point
async def main():
    # Start the capture loop
    await capture_loop()

# Run the script
if __name__ == "__main__":
    asyncio.run(main())
