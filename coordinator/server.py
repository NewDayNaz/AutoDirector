import asyncio
import json
import websockets
from collections import defaultdict

config = None
with open("../config.json", "r") as file:
    config = json.load(file)

# In-memory state store
state = {
    "bpm": 120,
    "ptz_moving": False,
    "lyrics_shown": True,
    "scene_pool": {},  # Store scene data as a dictionary
    "scene_interest": {
        "person_in_current_scene": False,
        "scenes_with_people": []
    }
}

# Keep track of connected clients per topic
subscriptions = defaultdict(set)

async def notify_subscribers(topic, data):
    if topic in subscriptions:
        message = json.dumps({"topic": topic, "data": data})
        to_remove = set()
        
        for ws in subscriptions[topic]:
          try:
            await ws.send(message)
          except websockets.exceptions.ConnectionClosedError as e:
            print(f"Error sending message to subscriber (ConnectionClosedError): {e}")
            to_remove.add(ws)
          except Exception as e:
            print(f"Error sending message to subscriber: {e}")
            to_remove.add(ws)
        
        if len(to_remove) > 0:
          subscriptions[topic] -= to_remove

async def handler(websocket):
  try:
    async for message in websocket:
        try:
            request = json.loads(message)
            action = request.get("action")
            topic = request.get("topic")

            if action == "update":
                if topic and topic in state:
                   if topic == "scene_pool":
                       # Update scene pool by merging the new data
                        state[topic][request["data"]["key"]] = request["data"]["value"]
                        # only send the new data down
                        await notify_subscribers(topic, request["data"])
                   else:
                        state[topic] = request["data"]
                        await notify_subscribers(topic, request["data"])

            elif action == "get":
                await websocket.send(json.dumps({"topic": topic, "data": state.get(topic)}))

            elif action == "subscribe":
                if topic:
                    subscriptions[topic].add(websocket)

        except Exception as e:
            print(f"Error handling message: {e}")
  except Exception as e:
     print(f"Connection closed with exception: {e}")
  finally:
    for topic, subs in subscriptions.items():
        if websocket in subs:
            subscriptions[topic].remove(websocket)
            print(f"Removed websocket from topic {topic}")

async def main():
    print("Starting coordinator server on ws://{}:{}".format(config["coordinator"]["host"], config["coordinator"]["port"]))
    async with websockets.serve(handler, config["coordinator"]["host"], config["coordinator"]["port"]):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
