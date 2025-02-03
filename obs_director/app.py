import asyncio
import random
import time
import websockets
import json
import obsws_python as obs
from aiohttp import web

config = None
with open("../config.json", "r") as file:
    config = json.load(file)

SCENE_LIST = config["obs"]["scenes"]["list"]
PTZ_SCENE = config["obs"]["scenes"]["ptz_scene"]
PROJECTOR_SCENE = config["obs"]["scenes"]["projector_scene"]

SLOW_BPM = config["obs"]["scenes"]["slow_bpm"]

# Global state to store data received from WebSocket
state = {
    "bpm": 120,
    "ptz_moving": False,
    "lyrics_shown": True,
    "scene_pool": {},
    "scene_interest": {
        "person_in_current_scene": False,
        "scenes_with_people": []
    }
}

async def websocket_client(subscriptions_ready):
    global state
    async with websockets.connect(config["coordinator"]["client"]) as websocket:
        print("Connected to WebSocket server")
        
        # Subscribe to topics
        await websocket.send(json.dumps({"action": "subscribe", "topic": "bpm"}))
        await websocket.send(json.dumps({"action": "subscribe", "topic": "ptz_moving"}))
        await websocket.send(json.dumps({"action": "subscribe", "topic": "lyrics_shown"}))
        await websocket.send(json.dumps({"action": "subscribe", "topic": "scene_interest"}))
        print("Subscribed to topics")
        subscriptions_ready.set() # Signal that the subscriptions are ready

        while True:
            message = await websocket.recv()
            data = json.loads(message)
            topic = data.get("topic")
            if topic in state:
                state[topic] = data["data"]
                # print(f"Updated {topic}: {data['data']}")

async def set_preview_scene(cl, scene_name):
    """Set a scene as the preview scene in OBS."""
    print(f"Set preview scene: {scene_name}")
    cl.set_current_preview_scene(scene_name)

async def switch_preview_to_program(cl):
    """Switch the current preview scene to the program scene in OBS."""
    cl.trigger_studio_mode_transition()
    print("Switched preview scene to program scene")

async def main():
    global state
    
    program_scene = None
    program_scene_name = ""
    next_switch_time = 0
    last_person_in_scene = None
    scenes_with_people = []

    # Connect to OBS WebSocket
    cl = obs.ReqClient(host=config["obs"]["websocket"]["host"], port=config["obs"]["websocket"]["port"], password=config["obs"]["websocket"]["password"], timeout=3)
    print("Connected to OBS WebSocket")

    # Create a asyncio.Event for the subscriptions
    subscriptions_ready = asyncio.Event()
    
    # Start WebSocket client as a task
    asyncio.create_task(websocket_client(subscriptions_ready))

    # Wait for the subscription process to complete before the rest of the script continues
    await subscriptions_ready.wait()
    
    last_bpm = 120  # Default BPM
    bpm = 120  # Default BPM
    next_preview_scene = None
    last_lyrics_shown = None

    try:
        while True:
            await asyncio.sleep(0.1)  # Prevent busy-waiting
            
            current_time = time.time()
            program_scene = cl.get_current_program_scene()
            program_scene_name = program_scene.current_program_scene_name
            
            # print(f"Current time is {current_time:.1f}, program scene is {program_scene_name}")
            # print(f"State data: {state}")
            
            # Check the lyrics_shown status
            lyrics_shown = state["lyrics_shown"]
            if lyrics_shown is not None and lyrics_shown != last_lyrics_shown:
                if not lyrics_shown and program_scene_name != PROJECTOR_SCENE:
                    # Transition to projector scene if lyrics_shown changes to false
                    await set_preview_scene(cl, PROJECTOR_SCENE)
                    await asyncio.sleep(0.1)
                    await switch_preview_to_program(cl)
                    if bpm < SLOW_BPM:
                        next_switch_time = current_time + random.uniform(config["obs"]["scenes"]["wait_min_slow"], config["obs"]["scenes"]["wait_max_slow"])
                    else:
                        next_switch_time = current_time + random.uniform(config["obs"]["scenes"]["wait_min"], config["obs"]["scenes"]["wait_max"])
                last_lyrics_shown = lyrics_shown

            # If lyrics_shown is False, skip scene switching
            if last_lyrics_shown is False:
                continue
            
            # Fetch scene interest data
            scene_interest_data = state["scene_interest"]
            if scene_interest_data:
                person_in_current_scene = scene_interest_data.get("person_in_current_scene", False)
                scenes_with_people = scene_interest_data.get("scenes_with_people", [])

                if last_person_in_scene is not None and last_person_in_scene and not person_in_current_scene:
                    # Transition to a random scene if the condition is met
                    if len(scenes_with_people) > 0:
                        next_scene = random.choice(scenes_with_people)
                        print(f"Switching to random scene because current scene has no person in it: {next_scene}")
                        await set_preview_scene(cl, next_scene)
                        await asyncio.sleep(0.1)
                        await switch_preview_to_program(cl)
                        if bpm < SLOW_BPM:
                            next_switch_time = current_time + random.uniform(config["obs"]["scenes"]["wait_min_slow"], config["obs"]["scenes"]["wait_max_slow"])
                        else:
                            next_switch_time = current_time + random.uniform(config["obs"]["scenes"]["wait_min"], config["obs"]["scenes"]["wait_max"])
                        await asyncio.sleep(1)
                        continue

                last_person_in_scene = person_in_current_scene

            # If PTZ is moving, switch to another scene
            ptzCameraMoving = state["ptz_moving"]
            if ptzCameraMoving:
                if program_scene_name == PTZ_SCENE:
                    print(f"Switch from {program_scene_name} because PTZ is moving!")
                    if len(scenes_with_people) > 0:
                        if len(scenes_with_people) > 1:
                            next_preview_scene = random.choice([scene for scene in scenes_with_people if scene != program_scene_name])
                        elif scenes_with_people[0] != program_scene_name:
                            next_preview_scene = scenes_with_people[0]
                        else:
                            next_preview_scene = program_scene_name
                    else:
                        next_preview_scene = random.choice([scene for scene in SCENE_LIST if scene != program_scene_name])

                    await set_preview_scene(cl, next_preview_scene)
                    await asyncio.sleep(0.1)
                    await switch_preview_to_program(cl)
                    if bpm < SLOW_BPM:
                        next_switch_time = current_time + random.uniform(config["obs"]["scenes"]["wait_min_slow"], config["obs"]["scenes"]["wait_max_slow"])
                    else:
                        next_switch_time = current_time + random.uniform(config["obs"]["scenes"]["wait_min"], config["obs"]["scenes"]["wait_max"])
                    continue

            # Fetch BPM data
            bpm = state["bpm"]
            if last_bpm != bpm:
                print(f"Updated BPM: {bpm}")
                last_bpm = bpm

            # Calculate time per beat
            seconds_per_beat = 60 / bpm
            if bpm < SLOW_BPM:
                next_switch_time = next_switch_time or current_time + random.uniform(config["obs"]["scenes"]["wait_min_slow"], config["obs"]["scenes"]["wait_max_slow"])
            else:
                next_switch_time = next_switch_time or current_time + random.uniform(config["obs"]["scenes"]["wait_min"], config["obs"]["scenes"]["wait_max"])

            # Pick and set a preview scene if none is set
            if next_preview_scene is None:
                if len(scenes_with_people) > 0:
                    if len(scenes_with_people) > 1:
                        next_preview_scene = random.choice([scene for scene in scenes_with_people if scene != program_scene_name])
                    elif scenes_with_people[0] != program_scene_name:
                        next_preview_scene = scenes_with_people[0]
                    else:
                        next_preview_scene = program_scene_name
                        
                else:
                    next_preview_scene = random.choice([scene for scene in SCENE_LIST if scene != program_scene_name])

                await set_preview_scene(cl, next_preview_scene)

            # Switch preview to program scene on beat
            if current_time >= next_switch_time:
                time_until_next_beat = seconds_per_beat - (current_time % seconds_per_beat)
                print(f"Waiting {time_until_next_beat:.1}s for next beat...")
                await asyncio.sleep(time_until_next_beat)  # Align with the beat
                await switch_preview_to_program(cl)
        
                next_preview_scene = None  # Reset for the next switch
                if bpm < SLOW_BPM:
                    next_switch_time = current_time + random.uniform(config["obs"]["scenes"]["wait_min_slow"], config["obs"]["scenes"]["wait_max_slow"])
                else:
                    next_switch_time = current_time + random.uniform(config["obs"]["scenes"]["wait_min"], config["obs"]["scenes"]["wait_max"])

    finally:
        await cl.disconnect()  # Make sure to disconnect the client when done

# --- Main Entrypoint ---

if __name__ == "__main__":
    asyncio.run(main())
