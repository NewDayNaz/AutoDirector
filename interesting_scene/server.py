import os
import json
from ultralytics import YOLO
import time
import base64
import obsws_python as obs
from PIL import Image
from io import BytesIO
import threading
from flask import Flask, jsonify

config = None
with open("../config.json", "r") as file:
    config = json.load(file)

SCENE_LIST = config["obs"]["scenes"]["list"]

model = YOLO("yolo11n.pt")

# Folder where snapshots will be saved
# snapshot_folder = "snapshots"
# if not os.path.exists(snapshot_folder):
#     os.makedirs(snapshot_folder)

# Connect to OBS WebSocket
cl = obs.ReqClient(host=config["obs"]["websocket"]["host"], port=config["obs"]["websocket"]["port"], password=config["obs"]["websocket"]["password"], timeout=3)

# Global variables
person_in_current_scene = False
scenes_with_people = []

# Flask app setup
app = Flask(__name__)

# Function to decode and save Base64 image
def image_has_person_in_scene(base64_data):
    img_data = base64.b64decode(base64_data)
    img = Image.open(BytesIO(img_data))
    
    results = model(img)
    
    # Save the image with detections
    # results[0].save(filename)
    
    # Check if any person is detected with confidence above 0.50
    for result in results[0].boxes:
        if result.cls == 0 and result.conf > 0.55:  # Class 0 corresponds to 'person' in YOLO
            return True
    return False

# Check the current program scene for a person
def check_current_program_scene():
    global person_in_current_scene
    global current_scene_name
    current_scene = cl.get_current_program_scene()
    current_scene_name = current_scene.current_program_scene_name
    # print(f"Processing current program scene: {current_scene.current_program_scene_name}")
    
    response = cl.get_source_screenshot(name=current_scene_name, img_format="jpg", width=1280, height=720, quality=100)
    base64_image = response.image_data.split(',')[1]
    # snapshot_filename = os.path.join(snapshot_folder, f"{current_scene_name}.jpg")
    
    if image_has_person_in_scene(base64_image):
        # print(f"    Person detected in current program scene: {current_scene_name}")
        person_in_current_scene = True
    else:
        person_in_current_scene = False
    #     print(f"    No person detected in current program scene: {current_scene_name}")
    
    # Sleep to prevent overwhelming the OBS WebSocket server
    time.sleep(1)

# Check other scenes for people
def check_other_scenes():
    global scenes_with_people
    for scene_name in SCENE_LIST:
        if scene_name == cl.get_current_program_scene():
            continue
        
        # print(f"Processing scene: {scene_name}")
        response = cl.get_source_screenshot(name=scene_name, img_format="jpg", width=1280, height=720, quality=100)
        base64_image = response.image_data.split(',')[1]
        # snapshot_filename = os.path.join(snapshot_folder, f"{scene_name}.jpg")
        
        if image_has_person_in_scene(base64_image):
            # print(f"    Person detected in scene: {scene_name}")
            if scene_name not in scenes_with_people:
                scenes_with_people.append(scene_name)
        else:
            if scene_name in scenes_with_people:
                scenes_with_people.remove(scene_name)
        #     print(f"    No person detected in scene: {scene_name}")
        
        # Sleep to prevent overwhelming the OBS WebSocket server
        time.sleep(1)

# Flask route to return the current status as JSON
@app.route('/status', methods=['GET'])
def get_status():
    return jsonify({
        'person_in_current_scene': person_in_current_scene,
        'scenes_with_people': scenes_with_people
    })

# Function to run the checks and continuously update status
def run_checks():
    while True:
        check_current_program_scene()
        check_other_scenes()
        # Sleep to prevent overwhelming the OBS WebSocket server
        time.sleep(5)  # Adjust the delay as needed

# Run the checks in a separate thread
thread = threading.Thread(target=run_checks)
thread.daemon = True
thread.start()

# Start Flask web server
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3853)  # Run on all interfaces, port 3853
