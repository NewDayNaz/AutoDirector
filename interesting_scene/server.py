import os
import json
from ultralytics import YOLO
import time
import base64
import obsws_python as obs
from PIL import Image
from io import BytesIO
import asyncio
import websockets

# Load configuration
config = None
with open("../config.json", "r") as file:
    config = json.load(file)

SCENE_LIST = config["obs"]["scenes"]["list"]

# Load YOLO model
model = YOLO("yolo11n.pt")

# Connect to OBS WebSocket
cl = obs.ReqClient(host=config["obs"]["websocket"]["host"], port=config["obs"]["websocket"]["port"], password=config["obs"]["websocket"]["password"], timeout=3)

# Global variables
person_in_current_scene = False
scenes_with_people = []
scene_pool_data = {} # Store the scene_pool data

# WebSocket client to update scene_interest data
async def update_scene_interest(person_in_current_scene, scenes_with_people):
    async with websockets.connect(config["coordinator"]["client"]) as websocket:
        message = json.dumps({
            "action": "update",
            "topic": "scene_interest",
            "data": {
                "person_in_current_scene": person_in_current_scene,
                "scenes_with_people": scenes_with_people
            }
        })
        await websocket.send(message)
        print(f"Updated scene_interest: person_in_current_scene={person_in_current_scene}, scenes_with_people={scenes_with_people}")


# Function to decode and check if an image has a person
def image_has_person_in_scene(base64_data):
    img_data = base64.b64decode(base64_data)
    img = Image.open(BytesIO(img_data))
    
    results = model(img)
    
    # Check if any person is detected with confidence above 0.50
    for result in results[0].boxes:
        if result.cls == 0 and result.conf > 0.55:  # Class 0 corresponds to 'person' in YOLO
            return True
    return False

# Check the current program scene for a person
async def check_current_program_scene():
    global person_in_current_scene, scenes_with_people, scene_pool_data
    current_scene = cl.get_current_program_scene()
    current_scene_name = current_scene.current_program_scene_name
    print(f"Processing current program scene from websocket: {current_scene_name}")
    
    if current_scene_name in scene_pool_data:
      base64_image = scene_pool_data[current_scene_name]
      if image_has_person_in_scene(base64_image):
          person_in_current_scene = True
      else:
          person_in_current_scene = False
    else:
      print(f"ERROR: Could not find {current_scene_name} in scene pool data")
      person_in_current_scene = False
    
    # Notify WebSocket server of the update
    await update_scene_interest(person_in_current_scene, scenes_with_people)
    await asyncio.sleep(0.05)

# Check other scenes for people
async def check_other_scenes():
    global scenes_with_people, scene_pool_data
    current_scene_name = cl.get_current_program_scene().current_program_scene_name
    for scene_name in SCENE_LIST:
        if scene_name == current_scene_name:
            continue
        
        print(f"Processing scene from websocket: {scene_name}")
        
        if scene_name in scene_pool_data:
          base64_image = scene_pool_data[scene_name]
          if image_has_person_in_scene(base64_image):
              if scene_name not in scenes_with_people:
                  scenes_with_people.append(scene_name)
          else:
              if scene_name in scenes_with_people:
                  scenes_with_people.remove(scene_name)
        else:
          print(f"ERROR: Could not find {scene_name} in scene pool data")

        # Notify WebSocket server of the update
        await update_scene_interest(person_in_current_scene, scenes_with_people)
        await asyncio.sleep(0.05)

# Function to run the checks and continuously update status
async def run_checks():
    global scene_pool_data
    uri = config["coordinator"]["client"]
    while True:
      try:
        async with websockets.connect(uri, ping_interval=None) as websocket:
          # Subscribe to the scene_pool topic
          await websocket.send(json.dumps({"action": "subscribe", "topic": "scene_pool"}))

          while True:
            await asyncio.sleep(0.1)
            try:
              message = await websocket.recv()
              data = json.loads(message)
              topic = data.get("topic")

              if topic == "scene_pool":
                  scene_name = data["data"]["key"]
                  scene_data = data["data"]["value"]
                  scene_pool_data[scene_name] = scene_data
                  print(f"Updated scene_pool data for {scene_name}")
                  await check_current_program_scene()
                  await check_other_scenes()
            except websockets.exceptions.ConnectionClosedError:
                print("Websocket connection closed, attempting to reconnect...")
                break
            except Exception as e:
              print(f"An unexpected error has occurred when receiving messages {e}, attempting to reconnect...")
              break

      except Exception as e:
        print(f"Error connecting to websocket, attempting to reconnect: {e}")
        await asyncio.sleep(2) # Sleep before reconnecting

# Main entry point
async def main():
    # Start the checks loop
    await run_checks()

# Run the script
if __name__ == "__main__":
    asyncio.run(main())
