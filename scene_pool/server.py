import json
import obsws_python as obs
import asyncio
import websockets

# Load configuration
config = None
with open("../config.json", "r") as file:
    config = json.load(file)

# Connect to OBS WebSocket
cl = obs.ReqClient(
    host=config["obs"]["websocket"]["host"],
    port=config["obs"]["websocket"]["port"],
    password=config["obs"]["websocket"]["password"],
    timeout=3
)

# WebSocket client to send scene data
async def send_scene_data(scene_name, base64_image):
    async with websockets.connect(config["coordinator"]["client"]) as websocket:
        message = json.dumps({
            "action": "update",
            "topic": "scene_pool",
            "data": {
                "key": scene_name,
                "value": base64_image
            }
        })
        await websocket.send(message)

# Function to capture and send screenshots for all scenes
async def capture_and_send_scenes():
    # Get the current scene
    current_scene = cl.get_current_program_scene()
    current_scene_name = current_scene.current_program_scene_name
    print(f"Current scene: {current_scene_name}")

    # Combine the current scene with the scene list
    scene_list = config["obs"]["scenes"]["list"]
    all_scenes = [current_scene_name] + [scene for scene in scene_list if scene != current_scene_name]

    # Capture and send screenshots for each scene
    for scene_name in all_scenes:
        print(f"Capturing screenshot for scene: {scene_name}")
        response = cl.get_source_screenshot(name=scene_name, img_format="jpg", width=1280, height=720, quality=80)
        base64_image = response.image_data.split(',')[1]
        
        # Send the screenshot to the WebSocket server
        await send_scene_data(scene_name, base64_image)

        # Sleep to avoid overwhelming the OBS WebSocket server
        await asyncio.sleep(0.1)

# Main entry point
async def main():
  while True:
    # Capture and send screenshots for all scenes
    await capture_and_send_scenes()

# Run the script
if __name__ == "__main__":
    asyncio.run(main())
