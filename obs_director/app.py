import asyncio
import random
import time
import websockets
import requests
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

async def fetch_scene_interest():
    """Fetch data from SCENE_INTEREST_API."""
    try:
        response = requests.get(config["scene_interest_api"]["url"])
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Failed to fetch scene interest data: {response.status_code}")
            return None
    except Exception as e:
        print(f"Error fetching scene interest data: {e}")
        return None

async def get_bpm():
    """Fetch BPM data from the web service."""
    try:
        response = requests.get(config["bpm_api"]["url"])
        if response.status_code == 200:
            bpm_data = response.json()
            return bpm_data["current_bpm"]
        else:
            print(f"Failed to fetch BPM data: {response.status_code}")
            return None
    except Exception as e:
        print(f"Error fetching BPM data: {e}")
        return None
    
async def get_ptz_moving():
    """Fetch PTZ moving data from the web service."""
    try:
        response = requests.get(config["ptz_moving_api"]["url"])
        if response.status_code == 200:
            data = response.json()
            return data["moving"]
        else:
            print(f"Failed to fetch PTZ moving data: {response.status_code}")
            return None
    except Exception as e:
        print(f"Error fetching PTZ moving data: {e}")
        return None

async def get_lyrics_shown():
    """Fetch lyrics_shown status from the lyrics API."""
    try:
        response = requests.get(config["lyrics_api"]["url"])
        if response.status_code == 200:
            data = response.json()
            return data["lyrics_shown"]
        else:
            print(f"Failed to fetch lyrics status: {response.status_code}")
            return None
    except Exception as e:
        print(f"Error fetching lyrics status: {e}")
        return None

async def set_preview_scene(cl, scene_name):
    """Set a scene as the preview scene in OBS."""
    print(f"Set preview scene: {scene_name}")
    cl.set_current_preview_scene(scene_name)

async def switch_preview_to_program(cl):
    """Switch the current preview scene to the program scene in OBS."""
    cl.trigger_studio_mode_transition()
    print("Switched preview scene to program scene")

async def main():
    ptzCameraMoving = False
    program_scene = None
    program_scene_name = ""
    next_switch_time = 0
    last_person_in_scene = None
    scenes_with_people = []

    # Connect to OBS WebSocket
    cl = obs.ReqClient(host=config["obs"]["websocket"]["host"], port=config["obs"]["websocket"]["port"], password=config["obs"]["websocket"]["password"], timeout=3)
    print("Connected to OBS WebSocket")

    last_ptz_moving_request_time = 0
    last_bpm_request_time = 0
    last_lyrics_request_time = 0
    last_scene_interest_request_time = 0
    bpm = 120  # Default BPM
    next_preview_scene = None
    last_lyrics_shown = None

    try:
        while True:
            current_time = time.time()
            program_scene = cl.get_current_program_scene()
            program_scene_name = program_scene.current_program_scene_name
            
            # Check the lyrics_shown status at the configured interval
            if current_time - last_lyrics_request_time >= config["lyrics_api"]["poll_interval"]:
                lyrics_shown = await get_lyrics_shown()
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
                last_lyrics_request_time = current_time

            # If lyrics_shown is False, skip scene switching
            if last_lyrics_shown is False:
                continue
            
            # Fetch scene interest data at the configured interval
            if current_time - last_scene_interest_request_time >= config["scene_interest_api"]["poll_interval"]:
                # Poll the SCENE_INTEREST_API
                scene_interest_data = await fetch_scene_interest()
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
                            continue

                    last_person_in_scene = person_in_current_scene
                last_scene_interest_request_time = current_time

            # If PTZ is moving, switch to another scene
            if current_time - last_ptz_moving_request_time >= config["ptz_moving_api"]["poll_interval"]:
                ptzCameraMoving = await get_ptz_moving()
                last_ptz_moving_request_time = current_time
                
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

            # Fetch BPM data at the configured interval
            # TODO: factor BPM into the random delay (switch scenes slower on slower songs)
            if current_time - last_bpm_request_time >= config["bpm_api"]["poll_interval"]:
                bpm = await get_bpm() or bpm
                print(f"Current BPM: {bpm}")
                last_bpm_request_time = current_time

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

            await asyncio.sleep(0.1)  # Prevent busy-waiting

    finally:
        await cl.disconnect()  # Make sure to disconnect the client when done

# --- Main Entrypoint ---

if __name__ == "__main__":
    asyncio.run(main())